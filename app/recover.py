"""Reboot recovery: after a reboot the tmux server comes back empty while the sessions table still
has open rows. On startup, every open Claude row whose tmux session is gone is relaunched in its
repo with `claude --resume <id>` (or --continue when the id is unknown); shell rows are closed."""
from __future__ import annotations

import logging
import shlex

from pathlib import Path

from . import projects, tmux
from .config import settings

log = logging.getLogger("ccboard.recover")
RECOVERABLE = {"claude", "resume", "continue", "recovered", "task"}


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
        cmd = ["claude"]
        if row.get("claude_session_id"):
            cmd += ["--resume", row["claude_session_id"]]
        else:
            cmd += ["--continue"]
        add_dirs = [d for d in (row.get("add_dirs") or []) if isinstance(d, str)]
        if add_dirs:
            cmd += ["--add-dir", *add_dirs]
        out.append({"name": name, "row": row, "cwd": str(rpath), "cmd": cmd, "add_dirs": add_dirs})
    return out


def run(db, start_session) -> dict:
    """Relaunch what plan() returns; close everything else that is gone. Returns a summary."""
    summary = {"recovered": [], "closed": [], "skipped": []}
    if not settings.recover:
        return summary
    try:
        live = set(tmux.list_sessions().keys())
    except tmux.TmuxDown:
        log.warning("recovery skipped: tmux server is down")
        return summary
    rows = db.open_rows()
    worktrees = {t["tmux_name"]: t["worktree"] for t in db.tasks() if t.get("worktree")}
    todo = plan(rows, live, worktrees)
    todo_names = {t["name"] for t in todo}
    for name, row in rows.items():
        if name not in live and name not in todo_names:
            db.end(name)
            summary["closed"].append(name)
    for t in todo:
        row = t["row"]
        db.end(t["name"])  # the old row is over; the relaunch gets a fresh row
        try:
            start_session(t["name"], row["project"], row["repo"], row["name"], "task" if row.get("launcher") == "task" else "recovered", t["cwd"],
                          cmd_line=shlex.join(t["cmd"]), claude_session_id=row.get("claude_session_id"),
                          add_dirs=t["add_dirs"])
            summary["recovered"].append(t["name"])
            log.info("recovered %s with %s", t["name"], " ".join(t["cmd"][:3]))
        except Exception as e:
            summary["skipped"].append(f"{t['name']}: {e}")
            log.warning("could not recover %s: %s", t["name"], e)
    if summary["recovered"] or summary["closed"]:
        db.kv_set("last_recovery", summary)
    return summary
