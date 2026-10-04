"""Reboot recovery: after a reboot the tmux server comes back empty while the sessions table still
has open rows. On startup, every open agent row whose tmux session is gone is relaunched in its
repo with the agent's own resume line (`claude --resume <id>` / `codex resume <id>`, or `--continue` / `codex resume --last` when the
id is unknown; the argv comes from the row's adapter, with the row's stored launch options re-passed and never a bypass); shell rows
are closed. A task row resumes inside its worktree (Claude's under .claude/worktrees, Codex's under .ccboard/worktrees), so a Codex
`resume --last` there finds the task's own conversation. A Codex SESSION row with no conversation id (hooks not trusted yet, so nothing
bound one) shares its directory with whatever else ran there, and `resume --last` filters by directory only: it is resumed that way
only when it is the one open Codex row of its directory, otherwise it starts a fresh `codex` (a note in the summary says so; the
rollout Tailer of v0.5.12 binds the ids and ends the guess).

A row that was `working` when the box went down (a prompt was running, no Stop recorded) is marked with
flags.continue_after_resume on its relaunched row: once the resumed session reports SessionStart and sits at its prompt,
app/autoresume types `continue` into it, so a power cut does not leave a half-done task waiting for someone to notice."""
from __future__ import annotations

import logging
import os
import shlex
import time

from pathlib import Path

from . import agents, projects, tasks, tmux
from .agents.base import LaunchReq
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


def _is_bypass(k, v) -> bool:
    """Does one stored launch option skip the approval prompts or the sandbox? By key (bypass, yolo, dangerously_*) with a truthy
    value, or by value (permission_mode bypassPermissions, sandbox danger-full-access). Free text such as a system prompt is never
    read for the words."""
    key = str(k).lower()
    if any(w in key for w in ("bypass", "yolo", "dangerous")) and v:
        return True
    return (k == "permission_mode" and v == "bypassPermissions") or (k == "sandbox" and str(v).lower() == "danger-full-access")


def _without_bypass(opts: dict) -> dict:
    """The stored launch options of a row, minus anything that skips the approval prompts or the sandbox: a relaunch after a reboot has
    nobody at the keyboard, so it never inherits a bypass (adapters do not store one, this holds even for a hand-edited row)."""
    return {k: v for k, v in opts.items() if not _is_bypass(k, v)}


def _where(path) -> str:
    try:
        return os.path.realpath(str(path))
    except (OSError, ValueError):
        return str(path)


def _row_dir(row: dict, worktree: str | None) -> str | None:
    """Where a row's agent runs: its task worktree, else the cwd it was launched in, else its repo's path."""
    if worktree:
        return _where(worktree)
    if isinstance(row.get("cwd"), str) and row["cwd"]:
        return _where(row["cwd"])
    try:
        return _where(projects.repo_path(row["project"], row["repo"]))
    except (projects.BadRequest, KeyError):
        return None


def _lone_codex_row(name: str, here: str, rows: dict[str, dict], worktrees: dict[str, str]) -> bool:
    """Is `name` the only open Codex row that runs in `here`? Then `codex resume --last` there can only be its own conversation (as far
    as the board can tell: another program's Codex run in the same directory is invisible to it)."""
    for other, orow in rows.items():
        if other != name and (orow.get("agent") or "claude") == "codex" and _row_dir(orow, worktrees.get(other)) == here:
            return False
    return True


def plan(rows: dict[str, dict], live: set[str], worktrees: dict[str, str] | None = None) -> list[dict]:
    """Which open rows to relaunch (missing from tmux, Claude launcher, repo still there).
    `worktrees` maps a task's tmux name to its worktree path, so task sessions resume in the worktree. A Codex session row with no
    conversation id is `resume --last` only when it is alone in its directory; otherwise its entry is `fresh` (a new `codex`, no
    resume) with a `note` for the summary."""
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
        opts = _without_bypass(row.get("opts") if isinstance(row.get("opts"), dict) else {})
        if ag.name == "codex" and row.get("launcher") == "task" and not opts.get("permission_mode"):
            # a task never takes its sandbox / approval from config.toml, whatever its stored options say (a row written before the
            # task default existed, or edited by hand): the relaunch line carries both -s and -a
            opts = {**opts, "permission_mode": "default"}
        sid = row.get("claude_session_id")
        fresh, note = False, None
        if (ag.name == "codex" and not sid and row.get("launcher") != "task" and not wt
                and not _lone_codex_row(name, _where(rpath), rows, worktrees)):
            # `codex resume --last` filters by directory only: with another Codex row here it could pick that row's thread
            fresh, note = True, "no conversation id and another Codex session shares its directory: started a fresh codex, not resume --last"
        try:
            if fresh:
                cmd = ag.launch_plan(LaunchReq(kind="new", session_name=row.get("name") or name, cwd=str(rpath), opts=opts,
                                               add_dirs=add_dirs)).argv
            else:
                cmd = ag.resume_argv(sid, opts=opts, add_dirs=add_dirs) if sid else ag.continue_argv(str(rpath), opts, add_dirs)
        except projects.BadRequest as e:
            # a stored id that is not a resumable UUID, or opts the adapter no longer accepts: do not let one row stop the rest
            log.warning("recover %s: %s; relaunching without the stored options", name, e)
            try:
                cmd = (ag.launch_plan(LaunchReq(kind="new", session_name=row.get("name") or name, cwd=str(rpath), add_dirs=add_dirs)).argv
                       if fresh else ag.continue_argv(str(rpath), None, add_dirs))
            except projects.BadRequest:
                continue
        out.append({"name": name, "row": row, "cwd": str(rpath), "cmd": cmd, "add_dirs": add_dirs, "opts": opts, "fresh": fresh, "note": note})
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
                                 add_dirs=t["add_dirs"], agent=row.get("agent") or "claude", opts=t["opts"] or None,
                                 task_id=task_ids.get(t["name"]) if row.get("launcher") == "task" else None)
            real = real if isinstance(real, str) and real else t["name"]
            summary["recovered"].append(t["name"])
            log.info("recovered %s with %s", t["name"], " ".join(t["cmd"][:3]))
            if t.get("note"):
                summary.setdefault("notes", []).append(f"{t['name']}: {t['note']}")
                log.warning("recover %s: %s", t["name"], t["note"])
            if wants_continue(row, now) and not t.get("fresh"):          # a fresh codex has no turn to continue: `continue` would be its first prompt
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
