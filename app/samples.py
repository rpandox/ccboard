"""Time series ("samples") of the board: what the box, the account and every session did over time.

One generic table (db.samples: series, key, at, value, meta) written by a few small hooks, a Sampler thread for the series nobody
triggers, and read-side helpers that turn rows into chart payloads. History only accrues from the day this ships, so every writer
is cheap, throttled and must never break the code path it rides on (callers wrap the writes; nothing here sleeps or shells out).

Write side   record() and the record_* wrappers (statusline, state changes, hook-event counter, rate-limit episodes, cost, health,
             counts) plus the Sampler.
Read side    parse_since / choose_step / downsample / series_payload / events_payload (pure over DB.samples_query).

Times are UTC ISO text with whole seconds (db.iso), so string order is time order. Day and hour bucketing in the viewer's zone is NOT
done here (a request parameter tz_min, see usage_summary); series_payload buckets on a UTC grid.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from .config import settings
from .db import _meta_obj, iso

log = logging.getLogger("ccboard.samples")

# series -> {agg, throttle: {delta, seconds}, retention_days}; the plan's "Series catalogue" plus 'lim'.
#   agg        how a chart bucket folds its samples: avg | last | sum | events (point events; charts read them as 'last' and the
#              events endpoint lists them). An events series is never throttled.
#   throttle   record() writes a sample when the value moved by at least `delta` OR the last sample is older than `seconds`.
#              delta None: no value trigger; delta 0: any change (value != last); seconds None: no time trigger. Both None = every
#              call writes (ev counts per event, state and lim are written by their own change/dedupe rules).
#   key        (documentation) rl_*: agent; ctx, ctx_tok, scost, stok, state: tmux session; ev: project; cost: '<agent>:<uuid>';
#              h_*: node; n_*: ''; lim: 5h | 7d | other.
CATALOGUE: dict[str, dict] = {
    "rl_5h":   {"agg": "avg",    "throttle": {"delta": 1,     "seconds": 300},  "retention_days": 90},
    "rl_7d":   {"agg": "avg",    "throttle": {"delta": 1,     "seconds": 300},  "retention_days": 90},
    "ctx":     {"agg": "avg",    "throttle": {"delta": 0.5,   "seconds": 120},  "retention_days": 14},
    "ctx_tok": {"agg": "avg",    "throttle": {"delta": 0.5,   "seconds": 120},  "retention_days": 14},
    "scost":   {"agg": "last",   "throttle": {"delta": 0.005, "seconds": None}, "retention_days": 30},
    "stok":    {"agg": "last",   "throttle": {"delta": 0,     "seconds": None}, "retention_days": 30},
    "state":   {"agg": "events", "throttle": {"delta": 0,     "seconds": None}, "retention_days": 90},
    "ev":      {"agg": "sum",    "throttle": {"delta": None,  "seconds": None}, "retention_days": 120},
    "cost":    {"agg": "last",   "throttle": {"delta": None,  "seconds": 600},  "retention_days": 120},
    "h_cpu":   {"agg": "avg",    "throttle": {"delta": None,  "seconds": 60},   "retention_days": 14},
    "h_mem":   {"agg": "avg",    "throttle": {"delta": None,  "seconds": 60},   "retention_days": 14},
    "h_load":  {"agg": "avg",    "throttle": {"delta": None,  "seconds": 60},   "retention_days": 14},
    "h_disk":  {"agg": "avg",    "throttle": {"delta": None,  "seconds": 900},  "retention_days": 30},
    "n_live":  {"agg": "avg",    "throttle": {"delta": None,  "seconds": 60},   "retention_days": 30},
    "n_work":  {"agg": "avg",    "throttle": {"delta": None,  "seconds": 60},   "retention_days": 30},
    "n_attn":  {"agg": "avg",    "throttle": {"delta": None,  "seconds": 60},   "retention_days": 30},
    "lim":     {"agg": "events", "throttle": {"delta": None,  "seconds": None}, "retention_days": 180},
}
RETENTION = {name: spec["retention_days"] for name, spec in CATALOGUE.items()}

STATE_CODES = {"idle": 0, "working": 1, "waiting": 2, "done": 3, "errored": 4, "ended": 5}
LIMIT_KINDS = ("5h", "7d", "other")
MAX_COMBOS = 8                  # series x key combinations one series_payload may carry
MAX_POINTS = 500
EVENTS_CAP = 5000
STEP_GRID = (10, 30, 60, 300, 900, 1800, 3600, 6 * 3600, 86400)       # seconds: the "nice" bucket widths
COST_META_MODELS = 3
LIM_LOOKBACK = timedelta(days=8)    # a reset time names the window (at most 7 days): same session, kind and reset time = one episode
_EPS = 1e-9
_lim_lock = threading.Lock()


def _num(v) -> float | None:
    """A finite number or None (bools, text and NaN/inf are not samples)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _epoch(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


def _clean(d: dict) -> dict | None:
    """A meta dict without its None values; None when nothing is left."""
    out = {k: v for k, v in d.items() if v is not None}
    return out or None


# ------------------------------------------------------------------ write side
def _due(throttle: dict, last: dict, value: float, ts: str) -> bool:
    delta, seconds = throttle.get("delta"), throttle.get("seconds")
    if delta is None and seconds is None:
        return True
    lv = _num(last.get("value"))
    if delta is not None and lv is not None:
        moved = abs(value - lv)
        if (delta > 0 and moved >= delta - _EPS) or (delta == 0 and value != lv):
            return True
    if seconds is not None and _epoch(ts) - _epoch(last["at"]) >= seconds:
        return True
    return False


def record(db, series: str, key: str, value, meta=None, at=None, force: bool = False) -> bool:
    """Write one sample of a catalogued series unless its throttle says the last sample still stands (see CATALOGUE). Events series
    and force=True always write. A value that is not a finite number is dropped (statusline fields are often null). `at` (datetime,
    epoch or ISO; default now) is also the clock the throttle's age is measured against. Returns whether a row was written. An
    unknown series raises ValueError (a programming error)."""
    spec = CATALOGUE.get(series)
    if spec is None:
        raise ValueError(f"unknown series {series!r}")
    v = _num(value)
    if v is None:
        return False
    key = "" if key is None else str(key)
    ts = iso(at)
    if not force and spec["agg"] != "events":
        last = db.sample_last(series, key)
        if last is not None and not _due(spec["throttle"], last, v, ts):
            return False
    db.sample(series, key, round(v, 6), meta, at=ts)
    return True


def record_statusline(db, tmux: str, stats: dict, agent: str = "claude", *, at=None) -> list[str]:
    """The samples one statusline event yields (stats is the dict hooks.apply builds): ctx (context %, meta model/window) and, when
    the window size is known, ctx_tok (used % x window; written together with ctx, so it follows ctx's throttle), scost (session USD)
    and, from stats['rate_limits'], rl_5h / rl_7d keyed by `agent` (value used %, meta resets_at). Returns the series written."""
    st = stats if isinstance(stats, dict) else {}
    wrote: list[str] = []
    pct, size = _num(st.get("context_pct")), _num(st.get("context_size"))
    if pct is not None:
        meta = _clean({"model": st.get("model_id") or st.get("model"), "window": int(size) if size else None})
        if record(db, "ctx", tmux, pct, meta, at):
            wrote.append("ctx")
            if size:
                if record(db, "ctx_tok", tmux, round(pct / 100.0 * size), None, at, force=True):
                    wrote.append("ctx_tok")
    if record(db, "scost", tmux, st.get("cost_usd"), None, at):
        wrote.append("scost")
    rl = st.get("rate_limits")
    if isinstance(rl, dict):
        for series, window in (("rl_5h", "five_hour"), ("rl_7d", "seven_day")):
            w = rl.get(window)
            if not isinstance(w, dict):
                continue
            used = w.get("used_percentage")          # the statusline's own name; used_percent is the Codex rollout's
            used = w.get("used_percent") if _num(used) is None else used
            meta = _clean({"resets_at": w.get("resets_at")})
            if record(db, series, agent or "claude", used, meta, at):
                wrote.append(series)
    return wrote


def record_state(db, tmux: str, old, new, event, row, *, at=None) -> bool:
    """One 'state' event per real state change (DB.on_state_change's consumer): value the state's code (0 idle 1 working 2 waiting
    3 done 4 errored 5 ended), meta {p: project, r: repo, s: session name, a: agent} from the session row. An unknown state or a
    repeat (old == new) writes nothing."""
    code = STATE_CODES.get(new)
    if code is None or old == new:
        return False
    r = row if isinstance(row, dict) else {}
    meta = _clean({"p": r.get("project"), "r": r.get("repo"), "s": r.get("name"), "a": r.get("agent") or "claude"})
    return record(db, "state", tmux, code, meta, at)


def bump_event(db, project: str, *, at=None) -> int:
    """Count one stored hook event into the 'ev' series: one row per (project, UTC hour), incremented in place (the row is stamped
    with the hour's first event). Returns the row id (0 for an empty project)."""
    if not project:
        return 0
    return db.sample_bump("ev", project, at=at)


def _resets_at(v):
    n = _num(v)
    return int(n) if n is not None else v


def record_limit(db, tmux: str, limit: dict, row=None, *, at=None) -> bool:
    """A rate-limit episode as a 'lim' event: key = the limit kind (5h | 7d | other), meta {session, resets_at, message[:120]}.
    At most one row per (session, kind, reset time): the repeats of one episode (a session retried 482 times, its subagents fail
    on their own) collapse. Without a reset time the dedupe window is the UTC hour. `row` (the session's row, may be None) is accepted
    for the callers' convenience and not stored. Returns whether a row was written."""
    lim = limit if isinstance(limit, dict) else {}
    kind = lim.get("kind") if lim.get("kind") in LIMIT_KINDS else "other"
    resets = _resets_at(lim.get("resets_at"))
    ts = iso(at)
    since = datetime.fromisoformat(ts) - (LIM_LOOKBACK if resets is not None else timedelta(hours=1))
    with _lim_lock:
        for t, _k, _v, raw in db.samples_query("lim", [kind], since):
            m = _meta_dict(raw)
            if m.get("session") != tmux:
                continue
            if resets is not None:
                if _resets_at(m.get("resets_at")) == resets:
                    return False
            elif m.get("resets_at") is None and t[:13] == ts[:13]:
                return False
        msg = lim.get("message")
        meta = _clean({"session": tmux, "resets_at": resets, "message": msg[:120] if isinstance(msg, str) and msg else None})
        db.sample("lim", kind, 1, meta, at=ts)
    return True


def _meta_dict(raw) -> dict:
    m = _meta_obj(raw)
    return m if isinstance(m, dict) else {}


def record_cost(db, cost_result: dict, now=None) -> int:
    """Cumulative USD per ccusage session into the 'cost' series, key '<agent>:<uuid>', meta {p: project, r: repo, a: agent,
    tok: tokens, m: models[:3]}. `cost_result['sessions']` is cost.session_samples()'s list of {agent, id, cost, tokens, last,
    project, repo, models}. A session's first sample is stamped at its lastActivity (not older than the series' retention); after
    that a row is written only when the cost or the token count changed AND the last row is at least 10 minutes old, so an idle
    session adds nothing and an active one adds at most 6 rows an hour. One transaction. Returns the rows written."""
    entries = (cost_result or {}).get("sessions") if isinstance(cost_result, dict) else None
    if not entries:
        return 0
    stamp = iso(now)
    cut = iso(datetime.fromisoformat(stamp) - timedelta(days=CATALOGUE["cost"]["retention_days"]))
    wait = CATALOGUE["cost"]["throttle"]["seconds"]
    rows = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        v = _num(e.get("cost"))
        sid = e.get("id")
        if v is None or not sid:
            continue
        agent = e.get("agent") or "claude"
        key = f"{agent}:{sid}"
        tok = int(_num(e.get("tokens")) or 0)
        models = [m for m in (e.get("models") or []) if isinstance(m, str)][:COST_META_MODELS]
        meta = _clean({"p": e.get("project"), "r": e.get("repo"), "a": agent, "tok": tok, "m": models or None})
        last = db.sample_last("cost", key)
        if last is None:
            try:
                at = iso(e.get("last")) if e.get("last") else stamp
            except (ValueError, TypeError):
                at = stamp
            if at < cut:
                continue                                        # last activity is older than the series keeps
            at = min(at, stamp)                                 # never in the future
        else:
            # compare what would be stored (6 dp) with what was stored: ccusage reports 7-8 dp, and an unrounded compare
            # would see every idle session as changed on every refresh
            same = abs(round(v, 6) - round(last["value"] or 0.0, 6)) <= _EPS and tok == int((last["meta"] or {}).get("tok") or 0)
            if same or _epoch(stamp) - _epoch(last["at"]) < wait:
                continue
            at = stamp
        rows.append(("cost", key, round(v, 6), meta, at))
    return db.sample_many(rows)


def record_health(db, snap: dict, *, at=None) -> list[str]:
    """h_cpu, h_mem, h_load (60 s) and h_disk (15 min) from health.snapshot(); key = the node name (settings.node_name, else the
    host name). Fields the box cannot report (cpu on the first reading, no /proc) are skipped. Returns the series written."""
    s = snap if isinstance(snap, dict) else {}
    key = str(s.get("node") or settings.node_name or ("local" if settings.runtime == "docker" else s.get("host")) or "node")   # a container's host name changes on every recreate
    mem, disk = s.get("mem"), s.get("disk")
    vals = (("h_cpu", s.get("cpu_pct")), ("h_mem", mem.get("pct") if isinstance(mem, dict) else None),
            ("h_load", s.get("load1")), ("h_disk", disk.get("pct") if isinstance(disk, dict) else None))
    return [series for series, v in vals if record(db, series, key, v, None, at)]


def record_counts(db, counts: dict, *, at=None) -> list[str]:
    """n_live / n_work / n_attn (key '') every 60 s from {live, work, attn} (the n_ prefix is accepted too)."""
    c = counts if isinstance(counts, dict) else {}
    out = []
    for series, name in (("n_live", "live"), ("n_work", "work"), ("n_attn", "attn")):
        v = c.get(name) if name in c else c.get(series)
        if record(db, series, "", v, None, at):
            out.append(series)
    return out


class Sampler(threading.Thread):
    """The series nobody triggers: every 15 s tick it stamps kv samples_heartbeat, records health and session counts (each by its own
    throttle) and, once per UTC day, prunes every series to its retention and truncates the WAL. A thread started in the lifespan,
    so it simply starts again after the box's daily reboot. `clock` is any object with time() (tests pass a fake one)."""
    TICK = 15.0

    def __init__(self, db, *, health_fn, counts_fn, clock=time):
        super().__init__(name="sampler", daemon=True)
        self.db, self.health_fn, self.counts_fn, self.clock = db, health_fn, counts_fn, clock
        self._halt = threading.Event()

    def sample_once(self) -> dict:
        """One tick (the whole body of run(); tests call it directly). Returns {'at', 'wrote': [series...], 'pruned': int | None}."""
        now = self.clock.time()
        at = datetime.fromtimestamp(now, tz=timezone.utc)
        self.db.kv_set("samples_heartbeat", {"ts": now, "at": iso(at)})
        wrote: list[str] = []
        for fn, rec in ((self.health_fn, record_health), (self.counts_fn, record_counts)):
            try:
                wrote += rec(self.db, fn(), at=at)
            except Exception as e:
                log.warning("sampler: %s failed: %s", getattr(fn, "__name__", "source"), e)
        pruned = None
        try:
            pruned = self._prune_daily(at)
        except Exception as e:
            log.warning("sampler: prune failed: %s", e)
        return {"at": iso(at), "wrote": wrote, "pruned": pruned}

    def _prune_daily(self, at: datetime) -> int | None:
        """Prune and checkpoint on the first tick of each UTC day (kv samples_pruned_at remembers the day, so a restart at noon does
        not prune again). Returns the rows deleted, None when today's prune already ran."""
        day = at.date().isoformat()
        done = self.db.kv_get("samples_pruned_at")
        if isinstance(done, dict) and isinstance(done.get("value"), dict) and done["value"].get("day") == day:
            return None
        n = self.db.samples_prune(RETENTION, now=at)
        self.db.wal_checkpoint()
        self.db.kv_set("samples_pruned_at", {"day": day, "deleted": n})
        return n

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                self.sample_once()
            except Exception as e:
                log.warning("sampler tick failed: %s", e)
            self._halt.wait(self.TICK)

    def stop(self) -> None:
        """Ask the thread to end and wait up to 2 s for it (the wait it sits in is an Event, so it returns at once)."""
        self._halt.set()
        if self.is_alive():
            self.join(timeout=2)


# ------------------------------------------------------------------ read side
_SINCE_RE = re.compile(r"^(\d{1,4})\s*([mhdw])$", re.I)
_UNIT = {"m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1), "w": timedelta(days=7)}


def _dt(v) -> datetime:
    """A datetime (aware, UTC) from a datetime, epoch seconds or ISO text."""
    return datetime.fromisoformat(iso(v))


def parse_since(s, now: datetime | None = None) -> datetime:
    """'1h' | '6h' | '24h' | '7d' | '30d' | '90d' (also any whole number of m, h, d or w) -> now minus that span; 'now' -> now; an ISO
    time ('2026-10-01', '2026-10-01T09:00:00Z', with an offset) -> that instant (naive = UTC). Always an aware UTC datetime. Raises
    ValueError for anything else."""
    now = _dt(now) if now is not None else datetime.now(timezone.utc).replace(microsecond=0)
    if isinstance(s, datetime):
        return _dt(s)
    t = (s or "").strip() if isinstance(s, str) else ""
    if not t:
        raise ValueError("empty time")
    if t.lower() == "now":
        return now
    m = _SINCE_RE.match(t)
    if m:
        return now - int(m.group(1)) * _UNIT[m.group(2).lower()]
    try:
        return _dt(t)
    except (ValueError, TypeError):
        raise ValueError(f"not a time span or ISO time: {s!r}") from None


def _buckets(since: float, until: float, step: int) -> tuple[int, int]:
    """(first bucket start, number of buckets) of the step-aligned grid covering since..until inclusive."""
    t0 = int(since // step) * step
    return t0, int(until // step) - int(since // step) + 1


def choose_step(since, until, points: int = MAX_POINTS) -> int:
    """The smallest width of STEP_GRID (10 s, 30 s, 1, 5, 15, 30 min, 1, 6 h, 1 d) for which since..until fits in `points` buckets on
    the step-aligned grid (at most MAX_POINTS). A span too long for a day per bucket takes whole days."""
    a, b = _dt(since).timestamp(), _dt(until).timestamp()
    points = max(1, min(int(points or MAX_POINTS), MAX_POINTS))
    for step in STEP_GRID:
        if _buckets(a, b, step)[1] <= points:
            return step
    step = STEP_GRID[-1]
    while _buckets(a, b, step)[1] > points:
        step += STEP_GRID[-1]
    return step


def downsample(rows, step: int, agg: str, since, until) -> dict:
    """Fold samples into step-aligned buckets over since..until (inclusive; buckets start at multiples of `step` from the epoch).
    rows: (at_iso, key, value, meta) as DB.samples_query returns them. agg: avg | last | sum ('events' reads as last). Returns
    {t: [bucket start epoch...], values: {key: [value | None per bucket]}} where an empty bucket is None (a gap, not zero);
    a key with no rows in range is absent. Rows outside since..until or with no value are ignored."""
    a, b = _dt(since).timestamp(), _dt(until).timestamp()
    t0, n = _buckets(a, b, step)
    acc: dict[str, list] = {}
    for at, key, value, _meta in sorted(rows, key=lambda r: r[0]):
        if value is None:
            continue
        e = _epoch(at)
        if e < a or e > b:
            continue
        cell = acc.setdefault(key, [None] * n)
        i = int((e - t0) // step)
        cur = cell[i]
        if agg == "sum":
            cell[i] = (cur or 0.0) + value
        elif agg == "avg":
            cell[i] = (value, 1) if cur is None else (cur[0] + value, cur[1] + 1)
        else:                                               # last (rows are oldest first)
            cell[i] = value
    values = {}
    for key, cell in acc.items():
        if agg == "avg":
            cell = [None if c is None else c[0] / c[1] for c in cell]
        values[key] = [None if c is None else round(c, 4) for c in cell]
    return {"t": [t0 + i * step for i in range(n)], "values": values}


def _series_spec(name: str) -> dict:
    spec = CATALOGUE.get(name)
    if spec is None:
        raise ValueError(f"unknown series {name!r}")
    return spec


def series_payload(db, series_list, keys, since, until=None, points: int = MAX_POINTS) -> dict:
    """The /api/series body: {since, until (ISO), step, t: [epoch...], series: {'rl_5h:claude': [v | None...]}, meta: {'ctx:<tmux>':
    last meta}}. `keys` None or ['*'] = every key a series has had in the window, most recently active first, cut at MAX_COMBOS in
    all (then 'capped': true is added); explicit keys that make more than MAX_COMBOS combinations raise ValueError, as do an unknown
    series, an empty series list or since >= until. since/until are datetimes, epochs or ISO text; until None = now. points is
    clamped to 1..MAX_POINTS. Explicit keys without data still appear (all None)."""
    names = [series_list] if isinstance(series_list, str) else list(series_list or [])
    if not names:
        raise ValueError("no series requested")
    specs = [_series_spec(n) for n in names]
    ks = [keys] if isinstance(keys, str) else (None if keys is None else list(keys))
    explicit = None if ks is None or "*" in ks else [str(k) for k in ks]
    if explicit is not None and len(names) * len(explicit) > MAX_COMBOS:
        raise ValueError(f"at most {MAX_COMBOS} series x key combinations")
    t_to = _dt(until) if until is not None else datetime.now(timezone.utc).replace(microsecond=0)
    t_from = _dt(since)
    if t_from >= t_to:
        raise ValueError("since must be before until")
    s_iso, u_iso = iso(t_from), iso(t_to)
    step = choose_step(t_from, t_to, points)
    out_series: dict[str, list] = {}
    out_meta: dict[str, dict] = {}
    t_axis = None
    capped = False
    budget = MAX_COMBOS
    for name, spec in zip(names, specs):
        use = explicit if explicit is not None else db.samples_distinct_keys(name, s_iso)
        if explicit is None and len(use) > budget:
            use, capped = use[:budget], True
        budget -= len(use)
        rows = db.samples_query(name, use, s_iso, u_iso) if use else []
        ds = downsample(rows, step, spec["agg"], t_from, t_to)
        t_axis = ds["t"]
        last_meta: dict[str, dict] = {}
        for at, key, _v, raw in rows:
            m = _meta_dict(raw)
            if m:
                last_meta[key] = m
        for k in use:
            label = f"{name}:{k}"
            out_series[label] = ds["values"].get(k) or [None] * len(t_axis)
            if k in last_meta:
                out_meta[label] = last_meta[k]
    body = {"since": s_iso, "until": u_iso, "step": step, "t": t_axis, "series": out_series, "meta": out_meta}
    if capped:
        body["capped"] = True
    return body


def events_payload(db, series: str, since, key: str | None = None) -> dict:
    """The /api/series/events body: {events: [{t: epoch, key, v, m: meta | None}] oldest first, truncated}. Past EVENTS_CAP rows the
    newest are kept and truncated is true. `key` None or '*' = every key. since: datetime, epoch or ISO text."""
    _series_spec(series)
    keys = None if key in (None, "", "*") else [key]
    rows = db.samples_query(series, keys, iso(since), None, limit=EVENTS_CAP + 1, newest=True)
    truncated = len(rows) > EVENTS_CAP
    rows = rows[-EVENTS_CAP:]
    return {"events": [{"t": int(_epoch(at)), "key": k, "v": v, "m": _meta_dict(raw) or None} for at, k, v, raw in rows],
            "truncated": truncated}
