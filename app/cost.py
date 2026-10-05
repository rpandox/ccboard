"""Cost per project / repo / task from `ccusage session --json`, joined on the agent session ids ccboard recorded for its own
sessions and tasks (sessions started outside the board are not attributed). Claude and Codex both: ccusage's unified output lists a
row per session for each agent, and `ccusage codex session --json --offline` fills the Codex half in when the unified output has none.

The join key is a uuid in the last path component of the row's `period` / `sessionId` / `sessionFile`: a bare uuid (Claude) or
`YYYY/MM/DD/rollout-<ts>-<uuid>` (Codex). A workflow or subagent transcript (`<parent-uuid>/subagents/workflows/wf_<id>/...`) has no
uuid of its own: its cost rolls up into the parent session's entry, so the parent and its children are one session here."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

from . import accounts, samples
from .config import settings

log = logging.getLogger("ccboard.cost")
UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_UUID_FULL = re.compile(f"^{_UUID}$")
KV_COST = "cost"
AGENTS = ("claude", "codex")
UNPRICED_TOP = 20          # how many unpriced sessions the kv record names (the full count and token total ride along)
CODEX_FALLBACK_TIMEOUT = 240
_TOKEN_FIELDS = ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens", "cachedInputTokens", "reasoningOutputTokens")
_PATH_FIELDS = ("period", "sessionId", "sessionFile", "projectPath", "project", "directory", "cwd")


def join_key(value) -> tuple[str, bool] | None:
    """The session uuid a ccusage row name stands for: (uuid lower case, is_child). A uuid ending the name (bare, or after
    `rollout-<ts>-` / a path) is the session itself; a name that has a uuid path component followed by `subagents` or a `wf_*` run
    is a child of that session (its parent's uuid, is_child True). None when the name has neither."""
    s = str(value or "").strip().replace("\\", "/")
    if s.endswith(".jsonl"):
        s = s[:-6]
    s = s.rstrip("/")
    if len(s) >= 36 and _UUID_FULL.match(s[-36:]) and (len(s) == 36 or s[-37] in "/-_"):
        return s[-36:].lower(), False
    parts = s.split("/")
    for i, seg in enumerate(parts):
        if _UUID_FULL.match(seg):
            if any(p == "subagents" or p.startswith("wf_") for p in parts[i + 1:]):
                return seg.lower(), True
            break
    return None


def _is_observer(row: dict) -> bool:
    """claude-mem's observer sessions (a path segment `observer-sessions`, or the project slug of such a cwd) are not the user's work:
    kept out of the join and the totals, like the hooks and the registry do."""
    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    for src in (row, meta):
        for k in _PATH_FIELDS:
            v = src.get(k)
            if isinstance(v, str) and any(seg == "observer-sessions" or "claude-mem-observer-sessions" in seg
                                          for seg in v.replace("\\", "/").split("/")):
                return True
    return False


def _agent_of(row: dict, default: str | None) -> str | None:
    a = row.get("agent")
    if isinstance(a, str) and a.strip():
        a = a.strip().lower()
        return "codex" if "codex" in a else "claude" if "claude" in a else a
    if any("rollout-" in str(row.get(k) or "") for k in ("period", "sessionId", "sessionFile")):
        return "codex"
    return default or "claude"


def _num(*vals) -> float:
    for v in vals:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return 0.0


def _tokens(r: dict, meta: dict) -> int:
    t = r.get("totalTokens", meta.get("totalTokens"))
    if isinstance(t, (int, float)) and not isinstance(t, bool):
        return int(t)
    return int(sum(_num(r.get(k)) for k in _TOKEN_FIELDS))


def _models(r: dict, meta: dict) -> list[str]:
    models = r.get("modelsUsed") or meta.get("modelsUsed") or r.get("models") or meta.get("models") or []
    if isinstance(models, dict):                      # @ccusage/codex: {"gpt-5": {...usage}}
        models = list(models)
    return [m for m in models if isinstance(m, str)][:6] if isinstance(models, list) else []


def parse_sessions(raw: str, default_agent: str | None = None) -> dict[str, dict]:
    """ccusage session JSON -> {session uuid: {agent, cost, tokens, last, models, unpriced, subs}}. Rows for other agents (ccusage knows
    more than Claude and Codex), observer sessions and rows with no uuid are left out; workflow / subagent rows add into their parent's
    entry (`subs` counts them); `unpriced` = tokens but a zero price (ccusage has no rate for the model). `default_agent` names the
    agent of rows that do not say (the `ccusage codex` fallback passes 'codex')."""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    if isinstance(data, dict):
        rows = data.get("session") if isinstance(data.get("session"), list) else data.get("sessions")
    else:
        rows = data
    out: dict[str, dict] = {}
    children: list[tuple[str, dict]] = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or _is_observer(r):
            continue
        agent = _agent_of(r, default_agent)
        if agent not in AGENTS:
            continue
        found = None
        for k in ("period", "sessionId", "sessionFile"):
            found = join_key(r.get(k))
            if found:
                break
        if not found:
            continue
        sid, child = found
        meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
        e = {"agent": agent, "cost": _num(r.get("totalCost"), r.get("costUSD"), r.get("cost"), r.get("total_cost")),
             "tokens": _tokens(r, meta), "last": meta.get("lastActivity") or r.get("lastActivity"),
             "models": _models(r, meta), "subs": 0}
        if child:
            children.append((sid, e))
        else:
            out[sid] = e                                  # a repeated plain row: the later one stands, as before
    for sid, e in children:                               # after the plain rows, so a parent listed later never overwrites its children
        p = out.get(sid)
        if p is None:
            e["subs"] = 1
            out[sid] = e
            continue
        p["cost"] += e["cost"]
        p["tokens"] += e["tokens"]
        p["subs"] += 1
        if e["last"] and (not p["last"] or str(e["last"]) > str(p["last"])):
            p["last"] = e["last"]
        p["models"] = (p["models"] + [m for m in e["models"] if m not in p["models"]])[:6]
    for e in out.values():
        e["unpriced"] = e["tokens"] > 0 and e["cost"] <= 0
    return out


def _run_ccusage(exe: str, args: list[str], env: dict | None = None) -> str | None:
    try:
        cp = subprocess.run([exe, *args], capture_output=True, text=True, timeout=CODEX_FALLBACK_TIMEOUT, env=env)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return cp.stdout if cp.returncode == 0 else None


def collect(run=None, which=None) -> dict[str, dict] | None:
    """The ccusage sessions of both agents, or None when ccusage is missing or its unified listing fails. When the unified listing has no
    Codex row but this box has Codex rollouts, `ccusage codex session --json --offline` (with CODEX_HOME set) is asked for them; a failing
    fallback only leaves the Codex half empty. `run(exe, args, env=None) -> stdout | None` and `which(name)` are the test seams."""
    run = run or _run_ccusage
    exe = (which or shutil.which)("ccusage")
    if not exe:
        return None
    out = run(exe, ["session", "--json"])
    if out is None:
        return None
    costs = parse_sessions(out)
    if not any(c["agent"] == "codex" for c in costs.values()) and (settings.codex_home / "sessions").is_dir():
        extra = run(exe, ["codex", "session", "--json", "--offline"], {**os.environ, "CODEX_HOME": str(settings.codex_home)})
        for sid, e in parse_sessions(extra or "", "codex").items():
            costs.setdefault(sid, e)
    return costs


def fetch_sessions() -> dict[str, dict] | None:
    return collect()


def _day(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).date().isoformat()
    except ValueError:
        return None


def _bucket() -> dict:
    return {"total": 0.0, "today": 0.0, "week": 0.0, "tokens": 0}


def _by_agent() -> dict[str, dict]:
    return {a: _bucket() for a in AGENTS}


def _add(buckets: dict[str, dict], c: dict, today: str, week_start: str) -> None:
    b = buckets.setdefault(c.get("agent") or "claude", _bucket())
    b["total"] += c["cost"]
    b["tokens"] += int(c.get("tokens") or 0)
    day = _day(c.get("last"))
    if day == today:
        b["today"] += c["cost"]
    if day and day > week_start:
        b["week"] += c["cost"]


def _rounded(buckets: dict[str, dict]) -> dict[str, dict]:
    return {a: {"total": round(b["total"], 4), "today": round(b["today"], 4), "week": round(b["week"], 4), "tokens": int(b["tokens"])}
            for a, b in buckets.items()}


def attribute(costs: dict[str, dict], session_rows: list[dict], task_rows: list[dict], now: datetime | None = None) -> dict:
    """Join costs to projects/repos/tasks. session_rows: [{project, repo, claude_session_id (the agent session id, either agent),
    agent}] (all rows, open or ended); task_rows: [{id, claude_session_id}]. Returns {projects:{p:{total,today,week,repos:{r:total},
    by_agent}}, tasks:{id:cost}, attributed, sessions_known, total_all, by_agent}. by_agent is {claude, codex}: {total, today, week,
    tokens}, over every ccusage session globally (ccusage is the total: never a sum of per-thread counters) and over the attributed
    sessions of each project. today / week count a session whole by its last-activity day."""
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    week_start = (now - timedelta(days=7)).date().isoformat()
    projects: dict[str, dict] = {}
    seen: set[str] = set()
    for row in session_rows:
        sid = (row.get("claude_session_id") or row.get("agent_session_id") or "").lower()
        c = costs.get(sid)
        if not c or sid in seen:
            continue
        seen.add(sid)
        p = projects.setdefault(row["project"], {"total": 0.0, "today": 0.0, "week": 0.0, "repos": {}, "by_agent": _by_agent()})
        p["total"] += c["cost"]
        p["repos"][row["repo"]] = p["repos"].get(row["repo"], 0.0) + c["cost"]
        day = _day(c.get("last"))
        if day == today:
            p["today"] += c["cost"]
        if day and day > week_start:
            p["week"] += c["cost"]
        _add(p["by_agent"], c, today, week_start)
    tasks = {}
    for t in task_rows:
        sid = (t.get("claude_session_id") or "").lower()
        if sid in costs:
            tasks[t["id"]] = round(costs[sid]["cost"], 4)
    for p in projects.values():
        for k in ("total", "today", "week"):
            p[k] = round(p[k], 4)
        p["repos"] = {r: round(v, 4) for r, v in p["repos"].items()}
        p["by_agent"] = _rounded(p["by_agent"])
    overall = _by_agent()
    for c in costs.values():
        _add(overall, c, today, week_start)
    return {"projects": projects, "tasks": tasks, "attributed": len(seen), "sessions_known": len(costs),
            "total_all": round(sum(c["cost"] for c in costs.values()), 2), "by_agent": _rounded(overall)}


def session_samples(costs: dict[str, dict], session_rows: list[dict]) -> list[dict]:
    """One entry per ccusage session, for samples.record_cost: {agent, id, cost, tokens, last, project, repo, models, unpriced}. project
    and repo are None for a session the board did not start. Kept out of the kv record on purpose: /api/state serves that record on
    every poll and ccusage lists thousands of sessions."""
    owner: dict[str, dict] = {}
    for row in session_rows:
        sid = (row.get("claude_session_id") or row.get("agent_session_id") or "").lower()
        if sid and sid not in owner:
            owner[sid] = row
    out = []
    for sid, c in costs.items():
        row = owner.get(sid) or {}
        out.append({"agent": c.get("agent") or "claude", "id": sid, "cost": c["cost"], "tokens": c["tokens"], "last": c.get("last"),
                    "project": row.get("project"), "repo": row.get("repo"), "models": list(c.get("models") or []),
                    "unpriced": c["tokens"] > 0 and c["cost"] <= 0})
    return out


def unpriced(entries: list[dict]) -> dict:
    """Sessions with tokens but a zero price, either agent: ccusage has no rate for the model, so the dollars undercount.
    {sessions, tokens, top[]} (each top entry names its agent; the Usage page badges them 'unpriced')."""
    rows = sorted((e for e in entries if e["tokens"] > 0 and e["cost"] <= 0), key=lambda e: -e["tokens"])
    return {"sessions": len(rows), "tokens": sum(e["tokens"] for e in rows),
            "top": [{"id": e["id"], "agent": e["agent"], "project": e["project"], "repo": e["repo"], "tokens": e["tokens"],
                     "models": e["models"]} for e in rows[:UNPRICED_TOP]]}


def _tag_accounts(db, entries: list[dict]) -> None:
    """Add `acct` (the subscription account key) to each Claude entry the cost samples will carry: the account its board session last
    ran under (sessions.account, kept current by the statusline attribution), else the account that is current now. Left off when
    neither is known (history from before account tracking). Only Claude entries are tagged: the accounts are Claude subscriptions
    and a Codex session is not billed to one. Best effort: a failure leaves the entries untagged."""
    try:
        by_sid, cur = db.session_accounts(), accounts.current(db)
        for e in entries:
            if (e.get("agent") or "claude") != "claude":
                continue
            acct = by_sid.get(str(e.get("id") or "").lower()) or cur
            if acct:
                e["acct"] = acct
    except Exception as e:
        log.warning("account tags for cost samples failed: %s", e)


def _now() -> datetime:
    return datetime.now(timezone.utc)                 # patched by tests


def refresh(db) -> dict | None:
    costs = fetch_sessions()
    if costs is None:
        return None
    now = _now()
    rows = db.session_ids()
    result = attribute(costs, rows, db.tasks(include_archived=True), now)
    entries = session_samples(costs, rows)
    _tag_accounts(db, entries)
    result["unpriced"] = unpriced(entries)
    db.kv_set(KV_COST, result)
    for tid, c in result["tasks"].items():
        db.task_update(tid, cost_usd=c)
    try:                                              # history is a bonus: a sampling failure never fails the refresh
        samples.record_cost(db, {**result, "sessions": entries}, now)
    except Exception as e:
        log.warning("cost samples failed: %s", e)
    return result
