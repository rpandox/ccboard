"""Auto routing: which paired node takes an epic issue (or a launcher task) now (nodes epic, issue #150).

This module only CHOOSES. It starts nothing, builds no launch option and asks no node anything: `pick` is a pure function over the hub's readings of the nodes (the records of
`NodeHub.records()`: status, pair scopes, card, state) and the board's own bookkeeping (pins, reservations, the epic's settings). What it chooses is then sent through the existing relay
row `epic_work`, which runs the claim, the author check and the guard on the node (app/nodes_run.py does the sending, one call at a time).

THE HARD FILTERS, in this order, each with its reason (a node that fails one is never chosen, whatever the ranking or a pin says):
  1 online   the hub's status is `online` (not stale, offline, unauthorized or unpaired)          2 scope    the pair holds `tasks`
  3 repo     the repository is cloned there (a slug in its last reading); no reading = not there   4 agent    the issue's agent is installed and logged in, no login problem
  5 backoff  the agent is not in a back-off after a rate-limited run                             6 window   the agent's window is under the epic's limit (CCBOARD_EPIC_MAX_WINDOW_PCT, 85)
  7 lane     a free lane is left after the lanes this board reserved (a cap of 0 means no cap)
  8 refused  this board's own last experience with the node (it said its lanes are full, its writes are off, it was not reachable): `blocked`, kept for two poll cycles
UNKNOWN IS NOT ZERO and not free: no reading of the card or the projects, an agent with no known login, a lane count that was not read, all refuse. The one exception is spelled out: a
window percentage that is not known (never read, or its reset time has passed so the number is old) counts as 50 and says "window unknown".

THE RANKING of the nodes that pass: more headroom (100 minus the window percentage) first, then more free lanes, then a lower load per core, then the node least recently assigned,
then name and handle. The rate-limit window belongs to the SUBSCRIPTION: nodes that share a subscription (the same `subscription_key` in their cards for that agent, or a group the
person set by hand in the epic's settings) are one group. A group has ONE window number, the highest one any online member reports; a member does not add headroom of its own. Their free
lanes add up for the tie-break only. The result is the same for any order of the input.

A PIN (the page's pin of the issue, then the label `ccboard:node:<alias>`, then the epic's pin) replaces the ranking and never the filters: a pinned node that fails one makes the issue
WAIT, and the answer says why. It is never sent to another node on its own. A pin that names a node that is not paired, or `local` (this board is not a routing target), waits too.

RESERVATIONS: dispatching to a node reserves one of its lanes until its next reading shows the task or two poll cycles have passed, so two ready issues do not both take the last lane of
one node between two readings. They live in memory on the hub and expire by themselves; none is kept across a restart (the node's own lane check is the last word).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from . import nodes, nodes_hub
from .config import settings as config

clock = time.time                       # epoch seconds; tests patch it
UNKNOWN_PCT = 50.0
RESERVE_CYCLES = 2                      # poll cycles a reservation lives
NO_LIMIT = 10 ** 6                      # free lanes of a node with no lane cap, for the tie-break
UNKNOWN_PIN = "?"                       # a pin that names no paired node (a label whose alias matches none)
WINDOW_WORD = {"claude": "5-hour window", "codex": "weekly window"}
AGENTS = ("claude", "codex")
FILTERS = ("online", "scope", "repo", "agent", "backoff", "window", "lane", "refused")
STATUS_TEXT = {"offline": "is offline", "stale": "has not answered lately (stale)", "unauthorized": "no longer accepts this board, pair it again", "unpaired": "is not paired any more",
               "incompatible": "speaks another node API, update the older board"}


# ---------------------------------------------------------------- reading a node's record

def _epoch(v) -> float | None:
    """A time as epoch seconds: a number (milliseconds are recognised) or an ISO string; None for anything else."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if v != v or abs(v) > 1e15:
            return None
        return float(v) / 1000.0 if v > 1e11 else float(v)
    return nodes_hub.parse_time(v) if isinstance(v, str) else None


def hhmm(ts: float) -> str:
    """A time of day in UTC for a sentence ("14:10 UTC"); a time more than a day away carries its date."""
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    return d.strftime("%H:%M UTC") if abs(ts - clock()) < 20 * 3600 else d.strftime("%b %d %H:%M UTC")


def node_view(rec: dict) -> dict:
    """One hub record as the plain facts routing reads. A fact that was not read is None (never 0 and never free)."""
    card = rec.get("card") if isinstance(rec.get("card"), dict) else None
    st = rec.get("state") if isinstance(rec.get("state"), dict) else None
    slugs = None
    if st is not None:
        slugs = {r["slug"].lower() for p in st.get("projects") or [] if isinstance(p, dict) for r in p.get("repos") or [] if isinstance(r, dict) and isinstance(r.get("slug"), str)}
    agents = None
    if card is not None and isinstance(card.get("agents"), list):
        agents = {a["id"]: {"installed": a.get("installed"), "logged_in": a.get("logged_in"), "problem": a.get("login_problem") is True}
                  for a in card["agents"] if isinstance(a, dict) and isinstance(a.get("id"), str)}
    sub: dict[str, str] = {}
    items = ((card or {}).get("accounts") or {}).get("items") if isinstance((card or {}).get("accounts"), dict) else None
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and it.get("current") is True and isinstance(it.get("agent"), str) and isinstance(it.get("subscription_key"), str) and it["subscription_key"]:
            sub.setdefault(it["agent"], it["subscription_key"])
    usage = st.get("usage") if st is not None and isinstance(st.get("usage"), dict) else None
    lanes = st.get("lanes") if st is not None and isinstance(st.get("lanes"), dict) else None
    load = card.get("load") if card is not None and isinstance(card.get("load"), dict) else {}
    l1, cores = load.get("load1"), load.get("cores")
    per_core = (float(l1) / cores) if isinstance(l1, (int, float)) and not isinstance(l1, bool) and isinstance(cores, int) and not isinstance(cores, bool) and cores > 0 else None
    problems = st.get("login_problems") if st is not None and isinstance(st.get("login_problems"), list) else []
    scopes = nodes._scopes_of(rec.get("scopes"))                       # a registry row keeps its scopes as a list or as JSON text
    return {"handle": rec.get("handle"), "name": rec.get("name") if isinstance(rec.get("name"), str) and rec.get("name") else rec.get("handle"), "status": rec.get("status"),
            "scopes": scopes, "slugs": slugs, "agents": agents, "login_problems": [p for p in problems if isinstance(p, str)],
            "usage": usage, "lanes": lanes, "load": per_core, "sub": sub, "ok": _epoch(rec.get("last_ok_at"))}


def _window(v: dict, agent: str, now: float) -> tuple[float | None, float | None]:
    """(the agent's window percentage on this node, when the window resets); the percentage is None when it is not a reading: never read, or the reset time has passed (the number is of
    the window before)."""
    u = (v["usage"] or {}).get(agent) if v["usage"] else None
    if not isinstance(u, dict):
        return None, None
    pct = u.get("pct")
    pct = float(pct) if isinstance(pct, (int, float)) and not isinstance(pct, bool) and 0 <= pct <= 100 else None          # outside 0..100 (or not a number) is not a reading
    reset = _epoch(u.get("resets_at"))
    if pct is not None and reset is not None and reset <= now:
        pct = None
    return pct, reset


def _groups(views: list[dict], agent: str, manual) -> dict[str, int]:
    """{handle: group number}: nodes that share a subscription for `agent` (the same key in their cards) or that the person grouped by hand are one group (union-find, so the answer
    does not depend on the order). Every node is in some group; a node of its own is a group of one."""
    parent = {v["handle"]: v["handle"] for v in views}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def join(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    by_key: dict[str, str] = {}
    for v in views:
        key = v["sub"].get(agent)
        if key:
            if key in by_key:
                join(v["handle"], by_key[key])
            else:
                by_key[key] = v["handle"]
    for g in manual or ():
        members = [h for h in g if h in parent] if isinstance(g, (list, tuple)) else []
        for h in members[1:]:
            join(members[0], h)
    return {h: find(h) for h in parent}


# ---------------------------------------------------------------- reservations

class Reservations:
    """The lanes this board has promised and the node's reading has not shown yet. In memory; an entry is {handle, until, n, at, ok, repo, issue, kind}."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: list[dict] = []

    def add(self, handle: str, until: float, *, n: int = 1, repo: str = "", issue: int = 0, ok: float | None = None, at: float | None = None, kind: str = "dispatch") -> dict:
        """`kind` is `dispatch` (a lane promised to an issue, released when a later reading shows its task) or `full` (the node said it has no lane: held until it expires)."""
        item = {"handle": handle, "until": float(until), "n": int(n), "at": clock() if at is None else float(at), "ok": ok, "repo": repo, "issue": issue, "kind": kind}
        with self._lock:
            self._items.append(item)
        return item

    def drop(self, item: dict) -> None:
        with self._lock:
            self._items = [x for x in self._items if x is not item]

    def items(self, now: float | None = None) -> list[dict]:
        """The live reservations (an expired one is gone for good)."""
        t = clock() if now is None else now
        with self._lock:
            self._items = [x for x in self._items if x["until"] > t]
            return [dict(x) for x in self._items]

    def release_seen(self, records: list[dict], now: float | None = None) -> int:
        """Drop the reservations whose task a LATER reading of the node now shows (its tasks hold the issue, not finished). Returns how many went."""
        t = clock() if now is None else now
        by = {r.get("handle"): r for r in records if isinstance(r, dict)}
        gone = 0
        with self._lock:
            keep = []
            for x in self._items:
                if x["until"] <= t:
                    gone += 1
                    continue
                r = by.get(x["handle"]) or {}
                ok = _epoch(r.get("last_ok_at"))
                tasks = ((r.get("state") or {}).get("tasks") if isinstance(r.get("state"), dict) else None) or []
                shown = any(isinstance(k, dict) and k.get("issue_ref") == f"#{x['issue']}" and k.get("phase") not in ("done", "failed", "cancelled") for k in tasks) if x["issue"] else False
                if x.get("kind") != "full" and shown and ok is not None and (x["ok"] is None or ok > x["ok"]):
                    gone += 1
                    continue
                keep.append(x)
            self._items = keep
        return gone

    def clear(self) -> None:
        with self._lock:
            self._items = []


# ---------------------------------------------------------------- pick

def _refuse(code: str, text: str, until: float | None = None) -> dict:
    return {"code": code, "text": text, "until": until}


def _check(v: dict, ctx: dict) -> dict | None:
    """The first hard filter node `v` fails, as {code, text, until}; None when it passes all seven."""
    now, agent, slug = ctx["now"], ctx["agent"], ctx["slug"]
    if v["status"] != "online":
        return _refuse(v["status"] if v["status"] in STATUS_TEXT else "offline", STATUS_TEXT.get(v["status"], "is offline"))
    if "tasks" not in v["scopes"]:
        return _refuse("scope", "has no tasks scope for this board")
    if v["slugs"] is None:
        return _refuse("repo_unknown", "has not reported its projects yet, so the repository is not known to be there")
    if not slug or slug not in v["slugs"]:
        return _refuse("repo", "does not have the repository cloned")
    if v["agents"] is None:
        return _refuse("agent_unknown", f"has not reported its agents yet, so {agent} is not known to be ready")
    a = v["agents"].get(agent)
    if not isinstance(a, dict) or a["installed"] is None:
        return _refuse("agent_unknown", f"does not report {agent} as installed")
    if a["installed"] is not True:
        return _refuse("agent_missing", f"does not have {agent} installed")
    if a["logged_in"] is not True or a["problem"] or agent in v["login_problems"]:
        return _refuse("agent_login", f"has {agent} logged out or in need of a new login")
    u = (v["usage"] or {}).get(agent) if v["usage"] else None
    back = u.get("backoff_until") if isinstance(u, dict) else None
    if back:
        t = _epoch(back)
        if t is None:
            return _refuse("backoff", f"is in a back-off for {agent}")
        if t > now:
            return _refuse("backoff", f"is in a back-off for {agent} until {hhmm(t)}", t)
    pct, unknown, reset = ctx["window"][v["handle"]]
    if pct >= ctx["max_pct"]:
        word = WINDOW_WORD[agent]
        if unknown:
            return _refuse("window", f"has no {word} reading and counts as {int(UNKNOWN_PCT)} percent, which is not under the limit of {ctx['max_pct']} percent")
        return _refuse("window", f"{word} is at {round(pct)} percent" + (f" until {hhmm(reset)}" if reset is not None and reset > now else ""), reset if reset is not None and reset > now else None)
    lanes = v["lanes"]
    cap = lanes.get("cap") if lanes else None
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:          # a negative cap is not "no cap": only 0 is
        return _refuse("lane_unknown", "has not reported its task lanes, so a free lane is not known")
    if cap > 0:
        free, running = lanes.get("free"), lanes.get("running")
        if not isinstance(free, int) or isinstance(free, bool) or not isinstance(running, int) or isinstance(running, bool):
            return _refuse("lane_unknown", "has not counted its running tasks, so a free lane is not known")
        if free - ctx["reserved"].get(v["handle"], 0) < 1:
            return _refuse("lane", "has no free lane" + (f" ({ctx['reserved'][v['handle']]} reserved by this board)" if ctx["reserved"].get(v["handle"]) else ""))
    b = ctx["blocked"].get(v["handle"])
    if isinstance(b, dict) and float(b.get("until") or 0) > now:
        return _refuse("refused", str(b.get("text") or "refused the last request"), float(b["until"]))
    return None


def _free(v: dict, reserved: dict) -> int:
    lanes = v["lanes"] or {}
    if lanes.get("cap") == 0:
        return NO_LIMIT
    return max(0, int(lanes.get("free") or 0) - reserved.get(v["handle"], 0))


def _lanes_text(free: int) -> str:
    return "no lane cap" if free >= NO_LIMIT else f"{free} free lane{'s' if free != 1 else ''}"


def pick(issue: dict, nodes: list[dict], pins: dict | None, reservations: list[dict] | None, settings: dict | None) -> dict:
    """Choose the node for one issue. `issue` is {repo: "owner/name", number, agent: claude | codex}; `nodes` the hub's records; `pins` {ui, label, epic} (a handle, UNKNOWN_PIN or None
    each); `reservations` the live ones (Reservations.items()); `settings` {max_window_pct, group: [[handle, ...]], assigned: {handle: epoch of its last assignment}, blocked: {handle: {until, text}}, now}.
    Returns {handle, name, why, waiting, pin, refused, ranked}: `handle` is the chosen node or None; `why` is one plain sentence either way; `waiting` is None or {code, reason, until,
    until_iso, text, pinned} (the earliest time any refusal names, or None); `refused` is [{handle, name, code, text, until}] for every node that failed a filter; `ranked` the nodes
    that passed, best first."""
    cfg = settings or {}
    now = float(cfg["now"]) if cfg.get("now") is not None else clock()
    agent = issue.get("agent") if issue.get("agent") in AGENTS else "claude"
    max_pct = cfg.get("max_window_pct") or int(getattr(config, "epic_max_window_pct", 85) or 85)
    views = sorted((node_view(r) for r in nodes or () if isinstance(r, dict) and isinstance(r.get("handle"), str) and r["handle"]), key=lambda x: x["handle"])
    reserved: dict[str, int] = {}
    for r in reservations or ():
        if isinstance(r, dict) and float(r.get("until") or 0) > now:
            reserved[r["handle"]] = reserved.get(r["handle"], 0) + int(r.get("n") or 1)
    group = _groups(views, agent, cfg.get("group"))
    # one window number per subscription group: the highest reading any ONLINE member has; none known = unknown (50)
    read = {v["handle"]: _window(v, agent, now) for v in views}
    gpct: dict[str, float | None] = {}
    for v in views:
        p = read[v["handle"]][0]
        if v["status"] == "online" and p is not None:
            gpct[group[v["handle"]]] = max(gpct.get(group[v["handle"]], 0.0), p)
    window = {}
    for v in views:
        p = gpct.get(group[v["handle"]])
        window[v["handle"]] = (UNKNOWN_PCT, True, read[v["handle"]][1]) if p is None else (p, False, read[v["handle"]][1])
    ctx = {"now": now, "agent": agent, "slug": str(issue.get("repo") or "").lower(), "max_pct": max_pct, "reserved": reserved, "window": window,
           "blocked": cfg.get("blocked") if isinstance(cfg.get("blocked"), dict) else {}}
    refused, passed = [], []
    for v in views:
        r = _check(v, ctx)
        if r is None:
            passed.append(v)
        else:
            refused.append({"handle": v["handle"], "name": v["name"], **r})
    assigned = cfg.get("assigned") or {}
    # the lane count of a group, for the tie-break only: the free lanes of its members that passed
    glanes: dict[str, int] = {}
    for v in passed:
        glanes[group[v["handle"]]] = min(NO_LIMIT, glanes.get(group[v["handle"]], 0) + _free(v, reserved))

    def key(v):
        h = v["handle"]
        return (-(100.0 - window[h][0]), -glanes[group[h]], (0, v["load"]) if v["load"] is not None else (1, 0.0), float(assigned.get(h) or 0.0), str(v["name"]).lower(), h)
    ranked = sorted(passed, key=key)

    def detail_of(v):
        h = v["handle"]
        p, unknown, _ = window[h]
        return {"handle": h, "name": v["name"], "free": _free(v, reserved), "pct": None if unknown else p, "window_unknown": unknown, "headroom": 100.0 - p,
                "shared_with": sorted(x["name"] for x in views if x["handle"] != h and group[x["handle"]] == group[h])}
    out = {"handle": None, "name": None, "why": "", "waiting": None, "pin": None, "refused": refused, "ranked": [detail_of(v) for v in ranked]}
    pin_source, pin = next(((s, pins[s]) for s in ("ui", "label", "epic") if pins and pins.get(s)), (None, None))
    if pin is not None:
        out["pin"] = {"handle": None if pin == UNKNOWN_PIN else pin, "source": pin_source}
        wanted = next((v for v in views if v["handle"] == pin), None)
        src = {"ui": "the issue's pin", "label": "the label ccboard:node", "epic": "the epic's pin"}[pin_source]
        if pin == UNKNOWN_PIN or wanted is None:
            what = "a node that is not paired with this board" if pin != "local" else "this board, which routing does not use"
            return _wait(out, "pin_unpaired", f"{src} names {what}", None, pin if pin not in (UNKNOWN_PIN, "local") else None)
        bad = next((r for r in refused if r["handle"] == pin), None)
        if bad is not None:
            return _wait(out, bad["code"], f"{wanted['name']} (pinned by {src}) {bad['text']}", bad["until"], pin)
        d = next(x for x in out["ranked"] if x["handle"] == pin)
        return _chose(out, d, agent, f", pinned by {src}")
    if not ranked:
        if not views:
            return _wait(out, "no_nodes", "no node is paired with this board", None, None)
        reasons = "; ".join(f"{r['name']} {r['text']}" for r in refused)
        times = [r["until"] for r in refused if r["until"] is not None]
        return _wait(out, refused[0]["code"] if len({r["code"] for r in refused}) == 1 else "none_ready", reasons[:400], min(times) if times else None, None)
    return _chose(out, out["ranked"][0], agent, "")


def _chose(out: dict, d: dict, agent: str, extra: str) -> dict:
    win = f"{WINDOW_WORD[agent]} unknown" if d["window_unknown"] else f"{WINDOW_WORD[agent]} {round(d['pct'])} percent"
    share = f", shares a subscription with {', '.join(d['shared_with'])}" if d["shared_with"] else ""
    out.update(handle=d["handle"], name=d["name"], why=f"chose {d['name']}: {_lanes_text(d['free'])}, {win}{share}{extra}")
    return out


def _wait(out: dict, code: str, reason: str, until: float | None, pinned: str | None) -> dict:
    iso = datetime.fromtimestamp(until, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if until is not None else None
    text = "waiting: " + reason + (f", resume around {hhmm(until)}" if until is not None and f"until {hhmm(until)}" not in reason else "")
    out["waiting"] = {"code": code, "reason": reason, "until": until, "until_iso": iso, "text": text, "pinned": pinned}
    out["why"] = text
    return out
