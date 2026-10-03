"""Usage summary for the Usage page (GET /api/usage/summary): every number comes from the samples table, bucketed in LOCAL time
from a `tz_min` offset (default 345 = Asia/Kathmandu, +5:45), never from the system zone and never from ccusage daily/blocks
(ccusage buckets in the system zone and its 5-hour blocks are UTC-floored, so they do not line up with +5:45).

    build(db, days=30, tz_min=345, now=None) -> {
      generated_at: ISO UTC, tz_min: int, source: 'samples',
      windows: {today|7d|30d: {total, by_agent: {agent: {total, tokens}}, by_project: [{project, total, hours}]}},
      daily: [{day: 'YYYY-MM-DD' (local), total, by_agent: {agent: usd}, by_project: {project: usd}, tokens, hours, zero}],
      hourly_profile: [24 ints],            # 'ev' hook events per local hour of day over the last `days` days
      heatmap: [[24 ints] x 7],             # heatmap[weekday][hour], weekday = Python weekday() (Mon=0 .. Sun=6)
      top_sessions: [{key, project, repo, agent, total, hours, tokens, models}],   # at most 10, by cumulative USD (last value)
      active_hours: {project: hours},       # over the last `days` days
      rate_limits: {claude: {rl_5h: {at, value, meta}|None, rl_7d: ...}},   # sample_last, meta parsed to a dict
      episodes: [{kind, at, resets_at, session}],                  # 'lim' events of the last 30 days, oldest first
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
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

SOURCE = "samples"
DEFAULT_TZ_MIN = 345
ACTIVE_GAP_S = 15 * 60            # a state-event gap up to this counts as active time
COST_LOOKBACK_DAYS = 120          # cost retention: how far back a key's baseline can sit
EPISODE_DAYS = 30
EPISODE_CAP = 200
TOP_SESSIONS = 10
UNPRICED_CAP = 50
UNATTRIBUTED = "(unattributed)"
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


# ---------- cost series: cumulative USD per key -> per-day spend ----------

def _cost_keys(rows, clock: _Clock, today: int) -> dict[str, dict]:
    """{key: {days: {day: (last value, last tokens)}, meta: merged newest meta, points: [(ts, value)]}} for rows up to today."""
    keys: dict[str, dict] = {}
    for at, key, value, meta_json in rows:
        ts, v = _epoch(at), _num(value)
        if ts is None or v is None or not isinstance(key, str):
            continue
        d = clock.day(ts)
        if d > today:
            continue
        m = _meta(meta_json)
        k = keys.setdefault(key, {"days": {}, "meta": {}, "points": [], "tok": 0.0})
        tok = _num(m.get("tok"))
        k["tok"] = tok if tok is not None else k["tok"]          # tokens carry forward over a sample that lacks them
        k["days"][d] = (v, k["tok"])
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


def _rise_hours(points: list[tuple[float, float]]) -> float:
    """Hours of activity in one cost key: gaps of at most 15 min between consecutive samples at which the value rose."""
    rises, prev = [], 0.0
    for ts, v in points:
        if v > prev:
            rises.append(ts)
        prev = v
    return _hours(sum(b - a for a, b in zip(rises, rises[1:]) if b - a <= ACTIVE_GAP_S))


# ---------- state series: active time ----------

def _active_pieces(rows, clock: _Clock) -> dict[int, dict[str, float]]:
    """{local day: {project: seconds}} of active time: gaps of at most ACTIVE_GAP_S between consecutive state events of one tmux key."""
    last: dict[str, tuple[float, str]] = {}
    out: dict[int, dict[str, float]] = {}
    for at, key, _value, meta_json in rows:
        ts = _epoch(at)
        if ts is None:
            continue
        prev = last.get(key)
        if prev is not None and 0 < ts - prev[0] <= ACTIVE_GAP_S:
            for d, secs in clock.split(prev[0], ts):
                bucket = out.setdefault(d, {})
                bucket[prev[1]] = bucket.get(prev[1], 0.0) + secs
        p = _meta(meta_json).get("p")
        last[key] = (ts, p if isinstance(p, str) and p else UNATTRIBUTED)
    return out


# ---------- rate limits, episodes, unpriced ----------

def _rate_limit(db, series: str, agent: str):
    last = db.sample_last(series, agent)
    if not last:
        return None
    return {"at": last.get("at"), "value": last.get("value"), "meta": _meta(last.get("meta"))}


def _episodes(db, since_iso: str) -> list[dict]:
    out = []
    for at, key, _value, meta_json in db.samples_query("lim", None, since_iso, None):
        m = _meta(meta_json)
        ra = m.get("resets_at")
        out.append({"kind": key or "other", "at": at, "resets_at": ra if isinstance(ra, (int, float)) and not isinstance(ra, bool) else None,
                    "session": m.get("session") if isinstance(m.get("session"), str) else None})
    return out[-EPISODE_CAP:]


def _unpriced(db) -> list[dict]:
    """Sessions that have tokens but price 0, as the kv 'cost' record names them (unpriced={sessions, tokens, top[]} or a bare list)."""
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
        agent = it.get("agent") if isinstance(it.get("agent"), str) and it.get("agent") else (ident.split(":", 1)[0] if ":" in ident else "claude")
        models = it.get("models") if isinstance(it.get("models"), list) else ([it["model"]] if isinstance(it.get("model"), str) else [])
        tokens = _num(it.get("tokens"))
        out.append({"key": ident if ":" in ident else f"{agent}:{ident}", "agent": agent, "project": it.get("project"),
                    "repo": it.get("repo"), "tokens": int(tokens or 0), "models": [m for m in models if isinstance(m, str)]})
    out.sort(key=lambda e: -e["tokens"])
    return out[:UNPRICED_CAP]


# ---------- the payload ----------

def build(db, days: int = 30, tz_min: int = DEFAULT_TZ_MIN, now: datetime | None = None) -> dict:
    """The usage summary (module docstring has the shape). `now` is injected by tests; naive = UTC. Never reads the system zone."""
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

    cost = _cost_keys(db.samples_query("cost", None, _iso(since_ts - COST_LOOKBACK_DAYS * 86400), None), clock, today)
    active = _active_pieces(db.samples_query("state", None, _iso(since_ts), None), clock)

    # per-day spend
    day_usd: dict[int, dict[str, float]] = {}        # day -> agent -> usd
    day_tok: dict[int, dict[str, float]] = {}
    day_proj: dict[int, dict[str, float]] = {}       # day -> project -> usd
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
    agent_list = sorted(agents, key=lambda a: (a not in ALWAYS_AGENTS, a))

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
        proj_secs: dict[str, float] = {}
        for d in rng:
            for p, v in day_proj.get(d, {}).items():
                proj_usd[p] = proj_usd.get(p, 0.0) + v
            for p, v in active.get(d, {}).items():
                proj_secs[p] = proj_secs.get(p, 0.0) + v
        by_project = [{"project": p, "total": round(proj_usd.get(p, 0.0), 4), "hours": _hours(proj_secs.get(p, 0.0))}
                      for p in set(proj_usd) | set(proj_secs)]
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
                    "total": round(total, 4), "hours": _rise_hours(k["points"]), "tokens": int(k["days"][max(k["days"])][1]),
                    "models": [m for m in meta.get("m") or [] if isinstance(m, str)] if isinstance(meta.get("m"), list) else []})
    top.sort(key=lambda r: (-r["total"], r["key"]))

    active_by_project: dict[str, float] = {}
    for d in range(today - days + 1, today + 1):
        for p, secs in active.get(d, {}).items():
            active_by_project[p] = active_by_project.get(p, 0.0) + secs

    return {
        "generated_at": _iso(now_ts),
        "tz_min": tz_min,
        "windows": windows,
        "daily": daily,
        "hourly_profile": hourly,
        "heatmap": heat,
        "top_sessions": top[:TOP_SESSIONS],
        "active_hours": {p: _hours(s) for p, s in sorted(active_by_project.items(), key=lambda x: -x[1])},
        "rate_limits": {"claude": {"rl_5h": _rate_limit(db, "rl_5h", "claude"), "rl_7d": _rate_limit(db, "rl_7d", "claude")}},
        "episodes": _episodes(db, _iso(now_ts - EPISODE_DAYS * 86400)),
        "unpriced": _unpriced(db),
        "source": SOURCE,
    }
