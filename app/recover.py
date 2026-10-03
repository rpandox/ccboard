"""Reboot recovery: after a reboot the tmux server comes back empty while the sessions table still
has open rows. On startup, every open agent row whose tmux session is gone is relaunched in its
repo with the agent's own resume line (`claude --resume <id>`, or --continue when the id is unknown; the argv comes from the
adapter, with the row's stored launch options re-passed and never a bypass); shell rows are closed.

A row that was `working` when the box went down (a prompt was running, no Stop recorded) is marked with
flags.continue_after_resume on its relaunched row: once the resumed session reports SessionStart and sits at its prompt,
app/autoresume types `continue` into it, so a power cut does not leave a half-done task waiting for someone to notice."""
from __future__ import annotations

import logging
import shlex
import time

from pathlib import Path

from . import agents, projects, tasks, tmux
from .config import settings

log = logging.getLogger("ccboard.recover")
RECOVERABLE = {"claude", "resume", "continue", "recovered", "task"}
RECENT_WORK = 24 * 3600    # a row 'working' for longer than this most likely missed its Stop hook: no continue for it
CONTINUE_FLAG = "continue_after_resume"


def _epoch(v) -> float:
    from datetime import datetime
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def wants_continue(row: dict, now: float | None = None) -> bool:
    """Was this row in the middle of a turn when the box went down? (state working, recently, an agent row.)"""
    if (row.get("state") or "") != "working" or (row.get("agent") or "claude") == "shell":
        return False
    if (row.get("flags") or {}).get("no_autoresume"):
        return False
    at = _epoch(row.get("state_at"))
    now = time.time() if now is None else now
    return bool(at) and now - at <= RECENT_WORK


def plan(rows: dict[str, dict], live: set[str], worktrees: dict[str, str] | None = None) -> list[dict]:
    """Which open rows to relaunch (missing from tmux, Claude launcher, repo still there).
    `worktrees` maps a task's tmux name to its worktree path, so task sessions resume in the worktree."""
    out = []
    worktrees = worktrees or {}
    for name, row in rows.items():
        if name in live or row.get("launcher") not in RECOVERABLE:
            continue
        try:
            rpath = projects.repo_path(row["project"], row["repo"])
        except projects.BadRequest:
            continue
        wt = worktrees.get(name)
        if wt and Path(wt).is_dir():
            rpath = Path(wt)
        elif row.get("launcher") == "task" or not rpath.is_dir():
            continue  # a task without its worktree cannot be resumed; the row is closed below
        try:
            ag = agents.get(row.get("agent") or "claude")
        except KeyError:
            continue  # an agent this build has no adapter for cannot be resumed; the row is closed below
        add_dirs = [d for d in (row.get("add_dirs") or []) if isinstance(d, str)]
        opts = row.get("opts") if isinstance(row.get("opts"), dict) else {}
        sid = row.get("claude_session_id")
        try:
            cmd = ag.resume_argv(sid, opts=opts, add_dirs=add_dirs) if sid else ag.continue_argv(str(rpath), opts, add_dirs)
        except projects.BadRequest as e:
            # a stored id that is not a resumable UUID, or opts the adapter no longer accepts: do not let one row stop the rest
            log.warning("recover %s: %s; continuing the last conversation without stored options", name, e)
            try:
                cmd = ag.continue_argv(str(rpath), None, add_dirs)
            except projects.BadRequest:
                continue
        out.append({"name": name, "row": row, "cwd": str(rpath), "cmd": cmd, "add_dirs": add_dirs})
    return out


def run(db, start_session) -> dict:
    """Relaunch what plan() returns; close everything else that is gone. Returns a summary."""
    summary = {"recovered": [], "closed": [], "skipped": [], "continue": []}
    if not settings.recover:
        return summary
    now = time.time()
    try:
        live = set(tmux.list_sessions().keys())
    except tmux.TmuxDown:
        log.warning("recovery skipped: tmux server is down")
        return summary
    rows = db.open_rows()
    live_tasks = [t for t in db.tasks() if tasks.has_worktree(t)]
    worktrees = {t["tmux_name"]: t["worktree"] for t in live_tasks}
    task_ids = {t["tmux_name"]: t["id"] for t in live_tasks}
    todo = plan(rows, live, worktrees)
    todo_names = {t["name"] for t in todo}
    for name, row in rows.items():
        if name not in live and name not in todo_names:
            db.end(name, "reconciled")
            summary["closed"].append(name)
    for t in todo:
        row = t["row"]
        db.end(t["name"], "reconciled")  # the old row is over; the relaunch gets a fresh row
        try:
            real = start_session(t["name"], row["project"], row["repo"], row["name"], "task" if row.get("launcher") == "task" else "recovered", t["cwd"],
                                 cmd_line=shlex.join(t["cmd"]), claude_session_id=row.get("claude_session_id"),
                                 add_dirs=t["add_dirs"], agent=row.get("agent") or "claude", opts=row.get("opts") or None,
                                 task_id=task_ids.get(t["name"]) if row.get("launcher") == "task" else None)
            real = real if isinstance(real, str) and real else t["name"]
            summary["recovered"].append(t["name"])
            log.info("recovered %s with %s", t["name"], " ".join(t["cmd"][:3]))
            if wants_continue(row, now):
                # the turn that was running is gone with the process; autoresume types `continue` once the session is back
                db.update_flags(real, {CONTINUE_FLAG: {"reason": "reboot", "at": now, "prompt": (row.get("last_prompt") or "")[:200],
                                                       "was_at": row.get("state_at")}})
                summary["continue"].append(real)
        except Exception as e:
            summary["skipped"].append(f"{t['name']}: {e}")
            log.warning("could not recover %s: %s", t["name"], e)
    if summary["recovered"] or summary["closed"]:
        db.kv_set("last_recovery", {k: v for k, v in summary.items() if k != "continue" or v})
    return summary
