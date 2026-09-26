"""Cost per project / repo / task from `ccusage session --json`, joined on the Claude session ids
ccboard recorded for its own sessions and tasks (sessions started outside the board are not attributed)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
KV_COST = "cost"


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
        out[sid.lower()] = {"cost": float(r.get("totalCost") or 0), "tokens": int(r.get("totalTokens") or 0),
                            "last": meta.get("lastActivity") or r.get("lastActivity")}
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


def refresh(db) -> dict | None:
    costs = fetch_sessions()
    if costs is None:
        return None
    result = attribute(costs, db.session_ids(), db.tasks(include_archived=True))
    db.kv_set(KV_COST, result)
    for tid, c in result["tasks"].items():
        db.task_update(tid, cost_usd=c)
    return result
