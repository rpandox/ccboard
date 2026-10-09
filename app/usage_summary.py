"""Usage summary for the Usage page (GET /api/usage/summary): every number comes from the samples table, bucketed in LOCAL time
from a `tz_min` offset (default 345 = Asia/Kathmandu, +5:45), never from the system zone and never from ccusage daily/blocks
(ccusage buckets in the system zone and its 5-hour blocks are UTC-floored, so they do not line up with +5:45).

    build(db, days=30, tz_min=345, now=None) -> {
      generated_at: ISO UTC, tz_min: int, source: 'samples',
      windows: {today|7d|30d: {total, by_agent: {agent: {total, tokens}}, by_project: [{project, total, hours, joined?}]}},   # joined (only when above 0): USD of the
                                            # project's total attributed by the folder a session ran in (issue #57), 0 when none
      daily: [{day: 'YYYY-MM-DD' (local), total, by_agent: {agent: usd}, by_project: {project: usd}, tokens, hours, zero}],
      hourly_profile: [24 ints],            # 'ev' hook events per local hour of day over the last `days` days
      heatmap: [[24 ints] x 7],             # heatmap[weekday][hour], weekday = Python weekday() (Mon=0 .. Sun=6)
      top_sessions: [{key, project, repo, agent, total, hours, tokens, models, via?}],   # at most 10, by cumulative USD (last value); via: 'folder' only when joined by folder
      active_hours: {project: hours},       # over the last `days` days
      rate_limits: {claude: {rl_5h: {at, value, meta}|None, rl_7d: ...},   # sample_last, meta parsed to a dict
                    by_account: {key: {rl_5h: {at, value, meta}|None, rl_7d: ...}}},   # series key 'acct:<key>'; real accounts only
      episodes: [{kind, at, resets_at, session, acct}],            # 'lim' events of the last 30 days, oldest first; acct = key|None
      accounts: [{key, email, name, label, plan, current, rl_5h: {value, resets_at, at}|None, rl_7d: ...,
                  windows: {today|7d|30d: {total, tokens, hours, sessions}}, episodes, last_seen}],
      total: {today|7d|30d: {total, tokens, hours, sessions}, accounts: n,
              headroom_5h: [{key, left_pct}], headroom_7d: [...]},
      unpriced: [{key, agent, project, repo, tokens, models}]}     # from the kv 'cost' record, [] when it names none

How the numbers are built (the contract the Usage page relies on):
  * `daily` has exactly `days` entries (1..365), oldest first, ending at the local day of `now`; a day without spend is present and
    flagged `zero: true`. The windows are today / the last 7 / the last 30 local days INCLUDING today and are summed from the same
    per-day numbers, so a window always equals the sum of its daily bars (when days < 30 the 30d window still reads 30 days).
  * The 'cost' series holds a CUMULATIVE USD figure per key ('<agent>:<uuid>'; meta p, r, a, tok, m). A key's spend on a local day is
    (its last value that day) - (its last value on the most recent earlier day with a sample, else 0), clamped at 0 because a
    re-priced session can dip. Summing the raw per-day last values would count a multi-day session once per day. The baseline is read
    from before the window (the series is retained 120 days), so a session that began before the window contributes only what it
    spent inside it. Tokens (meta.tok, also cumulative) follow the same rule. A key's project/agent come from its newest meta.
  * `hourly_profile` and `heatmap` bucket each 'ev' row by the local time of its `at`. A row is a per-(project, hour) counter that
    sits somewhere inside one UTC hour; at +5:45 that hour straddles two local hours, so a cell is accurate to about an hour.
  * `active_hours`: for each tmux key take the gaps between consecutive 'state' events; a gap of at most 15 minutes counts as active
    time (a longer gap is a pause), attributed to the project in the earlier event's meta and split at local midnight. Two sessions
    working at once both count (it is agent time, not wall clock).
  * `top_sessions.hours` uses the same 15-minute rule over that key's own cost samples where the value rose: the contract's state
    meta carries no session uuid, so a cost key cannot be joined to its tmux session's state events. It undercounts a session that
    works long without the cost series moving; adding the uuid to the state meta would make it exact.
  * `unpriced` is read from the kv 'cost' record (cost.refresh writes unpriced={sessions, tokens, top[]}); [] when it has none.

Accounts (one row per Claude subscription account; the subscription's real unit is the rl_5h / rl_7d window %, dollars are the
API-equivalent figure and stay secondary):
  * Scope: only Claude-agent usage (cost keys whose agent is 'claude', state events of agent 'claude'): a subscription account is
    Claude's, Codex is on its own plan and already has its by_agent row. So `total[w].total` equals windows[w].by_agent.claude.total
    (within the 4-decimal rounding of the per-account figures it is summed from), while `windows` keeps every agent.
  * Which account a sample belongs to: its meta `acct` (written by the sampler / cost.refresh / state hooks), else the account that the
    'acct' event series says was current at the sample's time (each event: key = the new account, meta {from, to}; before the first
    event the first event's `from`), else the bucket 'unknown' (name '(before account tracking)': history from before the tracking
    shipped). The bucket is listed last, only when it has activity, and is not counted in total.accounts.
  * Spend: the per-key daily delta (above) is split by the account of the cost samples that close each increment: every sample's step
    over the previous sample's value (its tokens likewise) goes to that sample's account; a day whose steps are not all positive (a
    re-priced dip) is scaled so the account shares add up to exactly the day's clamped delta. With one account on a day this is the
    closing sample's account; a mid-day /login splits the day instead of giving all of it to the later account.
  * hours: the 15-minute rule over 'state' events, attributed to the account of the earlier event of each gap. sessions: distinct cost
    keys with spend or tokens in the window (a key that straddled a switch counts once per account and once in total.sessions).
  * accounts[].rl_5h / rl_7d are the newest 'acct:<key>' readings ({value, resets_at, at, source: 'statusline' | 'cache'}); None until the account has one.
    headroom_*: real accounts with a reading, most room first (the current account first among equals; same rule as accounts.headroom); left_pct = 100 - value, and 100 once the reading's resets_at has passed
    (the window rolled over; nothing has been counted in the new one yet). An account without a reading is left out (unknown, not full).
  * `current` is the kv 'account_current' key (else the newest 'acct' event's key); rows sort current first, then by the 7d total.
    Identity fields (email, name, label, plan, last_seen) come from the kv 'accounts' record, None when it has none.
"""
from __future__ import annotations

import json
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone

from .accounts import PERIOD_5H, PERIOD_7D, SOURCE_CACHE, SOURCE_STATUSLINE, window_now

SOURCE = "samples"
DEFAULT_TZ_MIN = 345
ACTIVE_GAP_S = 15 * 60            # a state-event gap up to this counts as active time
COST_LOOKBACK_DAYS = 120          # cost retention: how far back a key's baseline can sit
EPISODE_DAYS = 30
EPISODE_CAP = 200
TOP_SESSIONS = 10
UNPRICED_CAP = 50
BASIS_REPORTED, BASIS_EST = "reported", "est"
UNATTRIBUTED = "(unattributed)"             # no known folder
OUTSIDE = "(outside projects)"              # a known folder that is not under the projects directory (issue #57; = cost.OUTSIDE)
UNKNOWN_ACCOUNT = "unknown"                 # the bucket for history from before account tracking (and an unreadable identity)
UNKNOWN_ACCOUNT_NAME = "(before account tracking)"
ACCOUNT_SERIES = "acct"
WINDOWS = (("today", 1), ("7d", 7), ("30d", 30))
ALWAYS_AGENTS = ("claude",)
_EPOCH_DAY = date(1970, 1, 1)


# ---------- small helpers ----------

def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _meta(raw) -> dict:
    """A samples meta (JSON text from samples_query, maybe already a dict from sample_last) -> dict; never raises."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)) and raw:
        try:
            v = json.loads(raw)
        except ValueError:
            return {}
        return v if isinstance(v, dict) else {}
    return {}


def _epoch(at) -> float | None:
    """ISO text ('...+00:00' or '...Z', naive = UTC) -> epoch seconds."""
    if not isinstance(at, str):
        return None
    try:
        d = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _day_str(day: int) -> str:
    return (_EPOCH_DAY + timedelta(days=day)).isoformat()


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


class _Clock:
    """Local-time bucketing from a fixed UTC offset in minutes: day indexes count local days since 1970-01-01."""

    def __init__(self, tz_min: int):
        self.off = tz_min * 60

    def day(self, ts: float) -> int:
        return int((ts + self.off) // 86400)

    def hour(self, ts: float) -> int:
        return int(((ts + self.off) % 86400) // 3600)

    def weekday(self, day: int) -> int:
        return (day + 3) % 7                      # 1970-01-01 was a Thursday; Monday = 0

    def day_start_utc(self, day: int) -> float:
        return day * 86400 - self.off

    def split(self, t0: float, t1: float):
        """(local day, seconds) pieces of the span t0..t1, cut at local midnight."""
        while t0 < t1:
            d = self.day(t0)
            end = min(t1, self.day_start_utc(d + 1))
            yield d, end - t0
            t0 = end


def _agent_of(key: str, meta: dict) -> str:
    a = meta.get("a")
    if isinstance(a, str) and a:
        return a
    return key.split(":", 1)[0] if ":" in key else "claude"


def _hours(seconds: float) -> float:
    return round(seconds / 3600.0, 2)


# ---------- accounts: who was current when ----------

class _Timeline:
    """The 'acct' event series as a lookup: at(ts) -> the account key current at ts, None when the events do not know."""

    def __init__(self, db):
        self.ts: list[float] = []
        self.keys: list[str] = []
        self.before: str | None = None                  # the account before the first event (its `from`), when it names one
        try:
            rows = db.samples_query(ACCOUNT_SERIES, None, 0)
        except Exception:                               # a read problem must not take the whole Usage page down
            rows = []
        for at, key, _value, meta_json in rows:
            m = _meta(meta_json)
            to = key if isinstance(key, str) and key else m.get("to")
            ts = _epoch(at)
            if ts is None or not isinstance(to, str) or not to:
                continue
            if not self.ts:
                frm = m.get("from")
                self.before = frm if isinstance(frm, str) and frm else None
            self.ts.append(ts)
            self.keys.append(to)

    def at(self, ts: float) -> str | None:
        i = bisect_right(self.ts, ts)
        return self.keys[i - 1] if i else self.before

    @property
    def last(self) -> str | None:
        return self.keys[-1] if self.keys else None


def _acct_of(meta: dict, ts: float, timeline: _Timeline) -> str:
    """The account of one sample: its own meta `acct`, else the timeline's account at its time, else the unknown bucket."""
    a = meta.get("acct")
    if isinstance(a, str) and a:
        return a
    return timeline.at(ts) or UNKNOWN_ACCOUNT


# ---------- cost series: cumulative USD per key -> per-day spend ----------

def _est_ratios(rows) -> dict[str, float]:
    """{key: USD the estimate adds per token}, read from the first sample of each key that carries an estimate (meta `est`, cumulative, with the key's tokens):
    the earlier samples of that key have no estimate of their own (they were taken before the estimate existed), so they are valued at their reported cost
    plus this rate times their tokens. A rough spread over the history, never negative."""
    out: dict[str, float] = {}
    for _at, key, value, meta_json in rows:
        if key in out or not isinstance(key, str):
            continue
        m = _meta(meta_json)
        e, v, tok = _num(m.get("est")), _num(value), _num(m.get("tok"))
        if e is not None and v is not None:
            out[key] = max(0.0, e - v) / tok if tok and tok > 0 else 0.0
    return out


def _cost_keys(rows, clock: _Clock, today: int, timeline: _Timeline, est: bool = False) -> dict[str, dict]:
    """{key: {days: {day: (last value, last tokens)}, meta: merged newest meta, points: [(ts, value)],
    split: {day: {account: [usd step, token step]}}}} for rows up to today. `split` sums, per day, each sample's step over the key's
    previous sample (any earlier day included) under that sample's account, so a day's steps add up to its raw delta.
    est=True values every sample at its estimated cumulative cost (meta `est`; a sample after the first estimate that lacks it is reported cost; a sample
    before it takes the key's per-token rate, see _est_ratios) instead of the reported one: the Estimated basis of the Usage page (issue #95)."""
    keys: dict[str, dict] = {}
    rows = list(rows)
    ratios = _est_ratios(rows) if est else {}
    started: set[str] = set()
    for at, key, value, meta_json in rows:
        ts, v = _epoch(at), _num(value)
        if ts is None or v is None or not isinstance(key, str):
            continue
        d = clock.day(ts)
        if d > today:
            continue
        m = _meta(meta_json)
        if est:
            e = _num(m.get("est"))
            if e is not None:
                started.add(key)
                v = max(v, e)
            elif key not in started and ratios.get(key):
                v = v + ratios[key] * (_num(m.get("tok")) or 0.0)
        k = keys.setdefault(key, {"days": {}, "meta": {}, "points": [], "tok": 0.0, "last": 0.0, "split": {}})
        prev_v, prev_t = k["last"], k["tok"]
        tok = _num(m.get("tok"))
        k["tok"] = tok if tok is not None else prev_t            # tokens carry forward over a sample that lacks them
        k["days"][d] = (v, k["tok"])
        step = k["split"].setdefault(d, {}).setdefault(_acct_of(m, ts, timeline), [0.0, 0.0])
        step[0] += v - prev_v
        step[1] += k["tok"] - prev_t
        k["last"] = v
        k["meta"].update({a: b for a, b in m.items() if b not in (None, "")})
        k["points"].append((ts, v))
    return keys


def _daily_deltas(k: dict) -> dict[int, tuple[float, float]]:
    """{day: (usd, tokens)} spent on each day of one key: last value of the day minus the last value of the previous sampled day."""
    out: dict[int, tuple[float, float]] = {}
    pv, pt = 0.0, 0.0
    for d in sorted(k["days"]):
        v, t = k["days"][d]
        out[d] = (max(0.0, v - pv), max(0.0, t - pt))
        pv, pt = v, t
    return out


def _share(delta: float, steps: dict[str, float]) -> dict[str, float]:
    """Split a day's clamped delta over the accounts by their positive steps (all of it to one account in the usual case; a dip
    under another account shrinks that account's share, never the day's total)."""
    if delta <= 0:
        return {}
    pos = {a: x for a, x in steps.items() if x > 0}
    total = sum(pos.values())
    return {a: delta * x / total for a, x in pos.items()} if total > 0 else {}


def _account_deltas(k: dict) -> dict[int, dict[str, tuple[float, float]]]:
    """{day: {account: (usd, tokens)}} of one key: _daily_deltas split by the accounts of the samples that closed each step."""
    out: dict[int, dict[str, tuple[float, float]]] = {}
    for d, (usd, tok) in _daily_deltas(k).items():
        steps = k["split"].get(d) or {}
        us, ts = _share(usd, {a: p[0] for a, p in steps.items()}), _share(tok, {a: p[1] for a, p in steps.items()})
        for a in us.keys() | ts.keys():
            out.setdefault(d, {})[a] = (us.get(a, 0.0), ts.get(a, 0.0))
    return out


def _rise_hours(points: list[tuple[float, float]]) -> float:
    """Hours of activity in one cost key: gaps of at most 15 min between consecutive samples at which the value rose."""
    rises, prev = [], 0.0
    for ts, v in points:
        if v > prev:
            rises.append(ts)
        prev = v
    return _hours(sum(b - a for a, b in zip(rises, rises[1:]) if b - a <= ACTIVE_GAP_S))


# ---------- state series: active time ----------

def _active_pieces(rows, clock: _Clock, timeline: _Timeline) -> tuple[dict[int, dict[str, float]], dict[int, dict[str, float]]]:
    """({local day: {project: seconds}}, {local day: {account: seconds}}) of active time: gaps of at most ACTIVE_GAP_S between
    consecutive state events of one tmux key. The project and the account are those of the earlier event of the gap; the account
    split covers Claude sessions only (state meta `a`, default claude)."""
    last: dict[str, tuple[float, str, str | None]] = {}
    by_project: dict[int, dict[str, float]] = {}
    by_account: dict[int, dict[str, float]] = {}
    for at, key, _value, meta_json in rows:
        ts = _epoch(at)
        if ts is None:
            continue
        prev = last.get(key)
        if prev is not None and 0 < ts - prev[0] <= ACTIVE_GAP_S:
            for d, secs in clock.split(prev[0], ts):
                bucket = by_project.setdefault(d, {})
                bucket[prev[1]] = bucket.get(prev[1], 0.0) + secs
                if prev[2] is not None:
                    acc = by_account.setdefault(d, {})
                    acc[prev[2]] = acc.get(prev[2], 0.0) + secs
        m = _meta(meta_json)
        p, agent = m.get("p"), m.get("a")
        claude = not (isinstance(agent, str) and agent and agent != "claude")
        last[key] = (ts, p if isinstance(p, str) and p else UNATTRIBUTED, _acct_of(m, ts, timeline) if claude else None)
    return by_project, by_account


# ---------- rate limits, episodes, unpriced ----------

def _rate_limit(db, series: str, agent: str):
    last = db.sample_last(series, agent)
    if not last:
        return None
    return {"at": last.get("at"), "value": last.get("value"), "meta": _meta(last.get("meta"))}


def _episodes(db, since_iso: str, timeline: _Timeline) -> list[dict]:
    out = []
    for at, key, _value, meta_json in db.samples_query("lim", None, since_iso, None):
        m = _meta(meta_json)
        ra = m.get("resets_at")
        ts = _epoch(at)
        acct = m.get("acct") if isinstance(m.get("acct"), str) and m.get("acct") else (timeline.at(ts) if ts is not None else None)
        out.append({"kind": key or "other", "at": at, "resets_at": ra if isinstance(ra, (int, float)) and not isinstance(ra, bool) else None,
                    "session": m.get("session") if isinstance(m.get("session"), str) else None, "acct": acct})
    return out[-EPISODE_CAP:]


def _estimate_info(db) -> dict:
    """The kv 'cost' record's `estimate` block ({date, source, sessions, usd, bases, cache_write_assumed, cache_write_x}) for the Usage page's caption and footer;
    the price table's own date and source when no refresh has written one yet."""
    from . import pricing
    rec = db.kv_get("cost")
    value = rec.get("value") if isinstance(rec, dict) else None
    est = value.get("estimate") if isinstance(value, dict) and isinstance(value.get("estimate"), dict) else {}
    info = pricing.info()
    bases = est.get("bases") if isinstance(est.get("bases"), dict) else {}
    return {"date": est.get("date") if isinstance(est.get("date"), str) else info["date"],
            "source": est.get("source") if isinstance(est.get("source"), str) else info["source"],
            "sessions": int(_num(est.get("sessions")) or 0), "usd": round(_num(est.get("usd")) or 0.0, 2),
            "bases": {k: int(v) for k, v in bases.items() if isinstance(k, str) and isinstance(v, (int, float))},
            "cache_write_assumed": bool(est.get("cache_write_assumed")), "cache_write_x": _num(est.get("cache_write_x")) or pricing.CACHE_WRITE_FALLBACK_X}


def _unpriced(db, basis: str = BASIS_REPORTED) -> list[dict]:
    """Sessions that have tokens but price 0, as the kv 'cost' record names them (unpriced={sessions, tokens, top[]} or a bare list). On the Estimated basis a
    session that has an estimate is no longer unpriced (the hatch stays for rows with none)."""
    rec = db.kv_get("cost")
    value = rec.get("value") if isinstance(rec, dict) else None
    raw = value.get("unpriced") if isinstance(value, dict) else None
    items = raw.get("top") if isinstance(raw, dict) else raw
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, str):
            it = {"id": it}
        if not isinstance(it, dict):
            continue
        ident = it.get("id") or it.get("key") or it.get("session")
        if not isinstance(ident, str) or not ident:
            continue
        if basis == BASIS_EST and it.get("est_basis"):
            continue
        agent = it.get("agent") if isinstance(it.get("agent"), str) and it.get("agent") else (ident.split(":", 1)[0] if ":" in ident else "claude")
        models = it.get("models") if isinstance(it.get("models"), list) else ([it["model"]] if isinstance(it.get("model"), str) else [])
        tokens = _num(it.get("tokens"))
        out.append({"key": ident if ":" in ident else f"{agent}:{ident}", "agent": agent, "project": it.get("project"),
                    "repo": it.get("repo"), "tokens": int(tokens or 0), "models": [m for m in models if isinstance(m, str)]})
    out.sort(key=lambda e: -e["tokens"])
    return out[:UNPRICED_CAP]


# ---------- accounts ----------

def _str(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _kv_dict(db, key: str) -> dict:
    """The dict a kv key holds, {} when it is missing, not a dict or unreadable."""
    try:
        rec = db.kv_get(key)
    except Exception:
        return {}
    v = rec.get("value") if isinstance(rec, dict) else None
    return v if isinstance(v, dict) else {}


def _reading(db, series: str, key: str) -> dict | None:
    """{value, resets_at, at, source}: the newest 'acct:<key>' reading of one window, None until the account has a numeric one.
    source is 'cache' when Claude Code's usage cache was the reading's origin (sample meta), else 'statusline'."""
    last = db.sample_last(series, f"acct:{key}")
    value = _num(last.get("value")) if last else None
    if value is None:
        return None
    meta = _meta(last.get("meta"))
    ra = _num(meta.get("resets_at"))
    return {"value": value, "resets_at": int(ra) if ra is not None else None, "at": last.get("at"),
            "source": SOURCE_CACHE if meta.get("source") == SOURCE_CACHE else SOURCE_STATUSLINE}


def _left_pct(reading: dict | None, now_ts: float, period: int) -> float | None:
    """What is left of a window: 100 - used %, and all of it once the reading's reset instant has passed (a new window, nothing
    counted yet: accounts.window_now, the rule the pills and gauges and accounts.headroom share). None without a reading: unknown is
    not the same as empty."""
    if reading is None:
        return None
    pct, _next, _rolled = window_now(reading["value"], reading["resets_at"], period, now_ts)
    return round(100.0 - pct, 1)


def _add(table: dict, account: str, day: int, amount: float) -> None:
    row = table.setdefault(account, {})
    row[day] = row.get(day, 0.0) + amount


def _accounts_section(db, timeline: _Timeline, now_ts: float, today: int, episodes: list[dict],
                      acc_usd: dict, acc_tok: dict, acc_sess: dict, acc_sec: dict) -> tuple[list[dict], dict, dict]:
    """(accounts[], total, rate_limits.by_account) from the per-account, per-day tables build() accumulated (account -> day -> value;
    acc_sess holds sets of cost keys) and the kv 'accounts' / 'account_current' records (shapes: app/accounts.py)."""
    known = {k: v for k, v in _kv_dict(db, "accounts").items() if isinstance(k, str) and isinstance(v, dict)}
    current = _str(_kv_dict(db, "account_current").get("key")) or timeline.last
    ep_count: dict[str, int] = {}
    for e in episodes:
        a = e.get("acct") or UNKNOWN_ACCOUNT
        ep_count[a] = ep_count.get(a, 0) + 1

    def window(a: str, n: int) -> tuple[dict, set]:
        rng = range(today - n + 1, today + 1)
        sess: set = set().union(*(acc_sess.get(a, {}).get(d, set()) for d in rng))
        return ({"total": round(sum(acc_usd.get(a, {}).get(d, 0.0) for d in rng), 4),
                 "tokens": int(round(sum(acc_tok.get(a, {}).get(d, 0.0) for d in rng))),
                 "hours": _hours(sum(acc_sec.get(a, {}).get(d, 0.0) for d in rng)), "sessions": len(sess)}, sess)

    keys = set(known) | set(timeline.keys) | set(acc_usd) | set(acc_tok) | set(acc_sess) | set(acc_sec) | set(ep_count)
    if current:
        keys.add(current)
    rows, sessions = [], {name: set() for name, _ in WINDOWS}
    for a in keys:
        wins = {}
        for name, n in WINDOWS:
            wins[name], sess = window(a, n)
            sessions[name] |= sess
        if a == UNKNOWN_ACCOUNT and not (ep_count.get(a) or any(w["total"] or w["tokens"] or w["hours"] or w["sessions"] for w in wins.values())):
            continue                                    # the bucket only exists while there is history in it
        rec = known.get(a, {})
        r5, r7 = _reading(db, "rl_5h", a), _reading(db, "rl_7d", a)
        seen = [r["at"] for r in (r5, r7) if r and r["at"]]
        rows.append({"key": a, "email": _str(rec.get("email")),
                     "name": _str(rec.get("name")) or (UNKNOWN_ACCOUNT_NAME if a == UNKNOWN_ACCOUNT else None),
                     "label": _str(rec.get("label")), "plan": _str(rec.get("plan")), "current": a == current,
                     "rl_5h": r5, "rl_7d": r7, "windows": wins, "episodes": ep_count.get(a, 0),
                     "last_seen": _str(rec.get("last_seen")) or (max(seen) if seen else None)})
    rows.sort(key=lambda r: (not r["current"], r["key"] == UNKNOWN_ACCOUNT, -r["windows"]["7d"]["total"], -r["windows"]["7d"]["tokens"], r["key"]))

    real = [r for r in rows if r["key"] != UNKNOWN_ACCOUNT]
    total: dict = {}
    for name, _n in WINDOWS:
        total[name] = {"total": round(sum(r["windows"][name]["total"] for r in rows), 4),
                       "tokens": sum(r["windows"][name]["tokens"] for r in rows),
                       "hours": round(sum(r["windows"][name]["hours"] for r in rows), 2), "sessions": len(sessions[name])}
    total["accounts"] = len(real)
    for field, series, period in (("headroom_5h", "rl_5h", PERIOD_5H), ("headroom_7d", "rl_7d", PERIOD_7D)):
        left = [(r["key"], r["current"], _left_pct(r[series], now_ts, period)) for r in real]
        total[field] = [{"key": k, "left_pct": v} for k, cur, v in sorted(((k, cur, v) for k, cur, v in left if v is not None),
                                                                         key=lambda t: (-t[2], not t[1], t[0]))]
    by_account = {r["key"]: {"rl_5h": _rate_limit(db, "rl_5h", f"acct:{r['key']}"), "rl_7d": _rate_limit(db, "rl_7d", f"acct:{r['key']}")}
                  for r in real}
    return rows, total, by_account


# ---------- the payload ----------

def build(db, days: int = 30, tz_min: int = DEFAULT_TZ_MIN, now: datetime | None = None, basis: str = BASIS_REPORTED) -> dict:
    """The usage summary (module docstring has the shape). `now` is injected by tests; naive = UTC. Never reads the system zone. `basis` is 'reported' (ccusage's
    own dollars, the default) or 'est' (the same plus list-price estimates for models ccusage prices at zero, issue #95); every dollar figure of one answer is
    on one basis, which the answer names."""
    basis = BASIS_EST if basis == BASIS_EST else BASIS_REPORTED
    days = _int(days, 30, 1, 365)
    tz_min = _int(tz_min, DEFAULT_TZ_MIN, -720, 840)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_ts = now.timestamp()
    clock = _Clock(tz_min)
    today = clock.day(now_ts)
    span = max(days, 30)                                           # the 30d window must stay right when days < 30
    first_day = today - span + 1
    since_ts = clock.day_start_utc(first_day)

    timeline = _Timeline(db)
    cost = _cost_keys(db.samples_query("cost", None, _iso(since_ts - COST_LOOKBACK_DAYS * 86400), None), clock, today, timeline, est=basis == BASIS_EST)
    active, active_acct = _active_pieces(db.samples_query("state", None, _iso(since_ts), None), clock, timeline)

    # per-day spend
    day_usd: dict[int, dict[str, float]] = {}        # day -> agent -> usd
    day_tok: dict[int, dict[str, float]] = {}
    day_proj: dict[int, dict[str, float]] = {}       # day -> project -> usd
    day_join: dict[int, dict[str, float]] = {}       # day -> project -> usd of it joined by folder (a part of day_proj, never added to it)
    agents = set(ALWAYS_AGENTS)
    for key, k in cost.items():
        meta = k["meta"]
        agent = _agent_of(key, meta)
        project = meta.get("p") if isinstance(meta.get("p"), str) and meta.get("p") else UNATTRIBUTED
        for d, (usd, tok) in _daily_deltas(k).items():
            if d < first_day:
                continue
            agents.add(agent)
            day_usd.setdefault(d, {})[agent] = day_usd.get(d, {}).get(agent, 0.0) + usd
            day_tok.setdefault(d, {})[agent] = day_tok.get(d, {}).get(agent, 0.0) + tok
            if usd > 0:
                day_proj.setdefault(d, {})[project] = day_proj.get(d, {}).get(project, 0.0) + usd
                if meta.get("j") and project != UNATTRIBUTED:
                    day_join.setdefault(d, {})[project] = day_join.get(d, {}).get(project, 0.0) + usd
    agent_list = sorted(agents, key=lambda a: (a not in ALWAYS_AGENTS, a))

    # the same deltas per Claude subscription account (account -> day -> usd / tokens / cost keys / active seconds)
    acc_usd: dict[str, dict[int, float]] = {}
    acc_tok: dict[str, dict[int, float]] = {}
    acc_sess: dict[str, dict[int, set]] = {}
    for key, k in cost.items():
        if _agent_of(key, k["meta"]) != "claude":
            continue
        for d, per in _account_deltas(k).items():
            if d < first_day:
                continue
            for a, (usd, tok) in per.items():
                _add(acc_usd, a, d, usd)
                _add(acc_tok, a, d, tok)
                if usd > 0 or tok > 0:
                    acc_sess.setdefault(a, {}).setdefault(d, set()).add(key)
    acc_sec: dict[str, dict[int, float]] = {a: {} for a in {a for per in active_acct.values() for a in per}}
    for d, per in active_acct.items():
        for a, secs in per.items():
            acc_sec[a][d] = secs

    def day_hours(d: int) -> float:
        return sum(active.get(d, {}).values())

    daily = []
    for d in range(today - days + 1, today + 1):
        by_agent = {a: round(day_usd.get(d, {}).get(a, 0.0), 4) for a in agent_list}
        total = round(sum(day_usd.get(d, {}).values()), 4)
        daily.append({"day": _day_str(d), "total": total, "by_agent": by_agent,
                      "by_project": {p: round(v, 4) for p, v in sorted(day_proj.get(d, {}).items(), key=lambda x: -x[1])},
                      "tokens": int(round(sum(day_tok.get(d, {}).values()))), "hours": _hours(day_hours(d)), "zero": total == 0})

    windows = {}
    for name, n in WINDOWS:
        rng = range(today - n + 1, today + 1)
        by_agent = {a: {"total": round(sum(day_usd.get(d, {}).get(a, 0.0) for d in rng), 4),
                        "tokens": int(round(sum(day_tok.get(d, {}).get(a, 0.0) for d in rng)))} for a in agent_list}
        proj_usd: dict[str, float] = {}
        proj_join: dict[str, float] = {}
        proj_secs: dict[str, float] = {}
        for d in rng:
            for p, v in day_proj.get(d, {}).items():
                proj_usd[p] = proj_usd.get(p, 0.0) + v
            for p, v in day_join.get(d, {}).items():
                proj_join[p] = proj_join.get(p, 0.0) + v
            for p, v in active.get(d, {}).items():
                proj_secs[p] = proj_secs.get(p, 0.0) + v
        by_project = [{"project": p, "total": round(proj_usd.get(p, 0.0), 4), "hours": _hours(proj_secs.get(p, 0.0)),
                       **({"joined": round(proj_join[p], 4)} if proj_join.get(p, 0.0) > 0 else {})} for p in set(proj_usd) | set(proj_secs)]
        by_project.sort(key=lambda r: (-r["total"], -r["hours"], r["project"]))
        windows[name] = {"total": round(sum(sum(day_usd.get(d, {}).values()) for d in rng), 4),
                         "by_agent": by_agent, "by_project": by_project}

    # activity by local hour (events per hour of day) over the last `days` days
    hourly = [0] * 24
    heat = [[0] * 24 for _ in range(7)]
    ev_since = clock.day_start_utc(today - days + 1)
    for at, _key, value, _meta_json in db.samples_query("ev", None, _iso(ev_since), None):
        ts, v = _epoch(at), _num(value)
        if ts is None or v is None or clock.day(ts) > today:
            continue
        n, h = int(round(v)), clock.hour(ts)
        hourly[h] += n
        heat[clock.weekday(clock.day(ts))][h] += n

    # top sessions by cumulative cost, among keys that sampled inside the window
    top = []
    window_first = today - days + 1
    for key, k in cost.items():
        if not any(d >= window_first for d in k["days"]):
            continue
        total = k["days"][max(k["days"])][0]
        if total <= 0:
            continue
        meta = k["meta"]
        top.append({"key": key, "project": meta.get("p") if isinstance(meta.get("p"), str) and meta.get("p") else UNATTRIBUTED,
                    "repo": meta.get("r") if isinstance(meta.get("r"), str) else None, "agent": _agent_of(key, meta),
                    **({"via": "folder"} if meta.get("j") and meta.get("p") else {}),
                    **({"est_basis": meta["eb"]} if isinstance(meta.get("eb"), str) and (basis == BASIS_EST or meta["eb"] == "statusline") else {}),   # a statusline figure is the cost on both bases, and says so
                    "total": round(total, 4), "hours": _rise_hours(k["points"]), "tokens": int(k["days"][max(k["days"])][1]),
                    "models": [m for m in meta.get("m") or [] if isinstance(m, str)] if isinstance(meta.get("m"), list) else []})
    top.sort(key=lambda r: (-r["total"], r["key"]))

    active_by_project: dict[str, float] = {}
    for d in range(today - days + 1, today + 1):
        for p, secs in active.get(d, {}).items():
            active_by_project[p] = active_by_project.get(p, 0.0) + secs

    episodes = _episodes(db, _iso(now_ts - EPISODE_DAYS * 86400), timeline)
    accounts, total, by_account = _accounts_section(db, timeline, now_ts, today, episodes, acc_usd, acc_tok, acc_sess, acc_sec)

    return {
        "generated_at": _iso(now_ts),
        "tz_min": tz_min,
        "windows": windows,
        "daily": daily,
        "hourly_profile": hourly,
        "heatmap": heat,
        "top_sessions": top[:TOP_SESSIONS],
        "active_hours": {p: _hours(s) for p, s in sorted(active_by_project.items(), key=lambda x: -x[1])},
        "rate_limits": {"claude": {"rl_5h": _rate_limit(db, "rl_5h", "claude"), "rl_7d": _rate_limit(db, "rl_7d", "claude")},
                        "by_account": by_account},
        "episodes": episodes,
        "accounts": accounts,
        "total": total,
        "unpriced": _unpriced(db, basis),
        "basis": basis,
        "estimate": _estimate_info(db),
        "source": SOURCE,
    }
