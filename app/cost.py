"""Cost per project / repo / task from `ccusage session --json`, joined on the Claude session ids
ccboard recorded for its own sessions and tasks (sessions started outside the board are not attributed)."""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

from . import accounts, samples

log = logging.getLogger("ccboard.cost")
UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
KV_COST = "cost"
UNPRICED_TOP = 20          # how many unpriced sessions the kv record names (the full count and token total ride along)


def parse_sessions(raw: str) -> dict[str, dict]:
    """ccusage session JSON -> {session_id: {cost, tokens, last}}."""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    rows = data.get("session") if isinstance(data, dict) else data
    out: dict[str, dict] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        sid = str(r.get("period") or r.get("sessionId") or "")
        if not UUID_RE.match(sid):
            continue
        meta = r.get("metadata") or {}
        models = r.get("modelsUsed") or meta.get("modelsUsed") or []
        out[sid.lower()] = {"cost": float(r.get("totalCost") or 0), "tokens": int(r.get("totalTokens") or 0),
                            "last": meta.get("lastActivity") or r.get("lastActivity"),
                            "models": [m for m in models if isinstance(m, str)][:6] if isinstance(models, list) else []}
    return out


def fetch_sessions() -> dict[str, dict] | None:
    exe = shutil.which("ccusage")
    if not exe:
        return None
    try:
        cp = subprocess.run([exe, "session", "--json"], capture_output=True, text=True, timeout=240)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if cp.returncode != 0:
        return None
    return parse_sessions(cp.stdout)


def _day(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).date().isoformat()
    except ValueError:
        return None


def attribute(costs: dict[str, dict], session_rows: list[dict], task_rows: list[dict], now: datetime | None = None) -> dict:
    """Join costs to projects/repos/tasks. session_rows: [{project, repo, claude_session_id}] (all rows, open or ended);
    task_rows: [{id, claude_session_id}]. Returns {projects:{p:{total,today,week,repos:{r:total}}}, tasks:{id:cost}, attributed, total}."""
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    week_start = (now - timedelta(days=7)).date().isoformat()
    projects: dict[str, dict] = {}
    seen: set[str] = set()
    for row in session_rows:
        sid = (row.get("claude_session_id") or "").lower()
        c = costs.get(sid)
        if not c or sid in seen:
            continue
        seen.add(sid)
        p = projects.setdefault(row["project"], {"total": 0.0, "today": 0.0, "week": 0.0, "repos": {}})
        p["total"] += c["cost"]
        p["repos"][row["repo"]] = p["repos"].get(row["repo"], 0.0) + c["cost"]
        day = _day(c.get("last"))
        if day == today:
            p["today"] += c["cost"]
        if day and day > week_start:
            p["week"] += c["cost"]
    tasks = {}
    for t in task_rows:
        sid = (t.get("claude_session_id") or "").lower()
        if sid in costs:
            tasks[t["id"]] = round(costs[sid]["cost"], 4)
    for p in projects.values():
        for k in ("total", "today", "week"):
            p[k] = round(p[k], 4)
        p["repos"] = {r: round(v, 4) for r, v in p["repos"].items()}
    return {"projects": projects, "tasks": tasks, "attributed": len(seen), "sessions_known": len(costs),
            "total_all": round(sum(c["cost"] for c in costs.values()), 2)}


def session_samples(costs: dict[str, dict], session_rows: list[dict]) -> list[dict]:
    """One entry per ccusage session, for samples.record_cost: {agent, id, cost, tokens, last, project, repo, models}. project and repo
    are None for a session the board did not start. Kept out of the kv record on purpose: /api/state serves that record on every
    poll and ccusage lists thousands of sessions."""
    owner: dict[str, dict] = {}
    for row in session_rows:
        sid = (row.get("claude_session_id") or "").lower()
        if sid and sid not in owner:
            owner[sid] = row
    out = []
    for sid, c in costs.items():
        row = owner.get(sid) or {}
        out.append({"agent": "claude", "id": sid, "cost": c["cost"], "tokens": c["tokens"], "last": c.get("last"),
                    "project": row.get("project"), "repo": row.get("repo"), "models": list(c.get("models") or [])})
    return out


def unpriced(entries: list[dict]) -> dict:
    """Sessions with tokens but a zero price: ccusage has no rate for the model, so the dollars undercount. {sessions, tokens, top[]}
    (the price table that fills these in is v0.5.12; the Usage page shows them hatched meanwhile)."""
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
