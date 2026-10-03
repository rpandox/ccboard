"""Tasks: one agent session in its own git worktree + branch (Claude: `claude --worktree`; other agents: a worktree
ccboard manages under .ccboard/worktrees), or an unassigned backlog task with no session and no worktree yet.

This module must not import app.agents: that keeps the import graph acyclic."""
from __future__ import annotations

import re
import shlex
import subprocess
import uuid
from pathlib import Path

from . import projects, tmux

SLUG_RE = re.compile(r"[^a-z0-9]+")
WORKTREES = ".claude/worktrees"              # where `claude --worktree` puts them
MANAGED_WORKTREES = ".ccboard/worktrees"     # created by ccboard for every other agent
STATUSES = ("open", "pr", "merged", "archived")
PHASES = ("backlog", "queued", "running", "done", "failed", "cancelled")   # tasks.phase; legacy rows are 'running'


def has_worktree(t: dict | None) -> bool:
    """Does this task have a worktree on disk to act on? An unassigned (backlog/queued) task and a session-mode task
    store '' (or None on a hand-built dict). Path('') is Path('.'), which is_dir() and would point git at the board's
    own cwd, so every consumer of t['worktree'] must go through this (or task_worktree) first."""
    return bool(t and str(t.get("worktree") or "").strip())


def task_worktree(t: dict | None) -> Path | None:
    """Path(t['worktree']) when the task has a worktree, else None."""
    return Path(str(t["worktree"]).strip()) if has_worktree(t) else None


def slugify(title: str) -> str:
    s = SLUG_RE.sub("-", title.lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:40].strip("-")
    return s or "task"


def unique_slug(repo_path: Path, base: str, taken: set[str]) -> str:
    slug = base
    n = 2
    while slug in taken or any((repo_path / d / slug).exists() for d in (WORKTREES, MANAGED_WORKTREES)):
        slug = f"{base[:36]}-{n}"
        n += 1
    return slug


def session_name_for(slug: str) -> str:
    return f"t-{slug}"


# Both worktree folders are excluded. The Claude entry goes last so a fresh exclude file still ends with it.
EXCLUDES = (MANAGED_WORKTREES + "/", WORKTREES + "/")


def ensure_excluded(repo_path: Path) -> None:
    """Keep both worktree folders (.claude/worktrees, .ccboard/worktrees) out of `git status` without touching
    tracked files."""
    git_dir = repo_path / ".git"
    if git_dir.is_file():  # this repo is itself a worktree: the common dir holds info/exclude
        try:
            common = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "--git-common-dir"],
                                    capture_output=True, text=True, timeout=5).stdout.strip()
            git_dir = (repo_path / common).resolve() if common else git_dir
        except (subprocess.TimeoutExpired, OSError):
            return
    exclude = git_dir / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text() if exclude.exists() else ""
        missing = [e for e in EXCLUDES if e not in current]
        if missing:
            with exclude.open("a") as f:
                f.write(("" if current.endswith("\n") or not current else "\n") + "".join(e + "\n" for e in missing))
    except OSError:
        pass


def default_branch(repo_path: Path) -> str:
    for args in (["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],):
        try:
            cp = subprocess.run(["git", "-C", str(repo_path), *args], capture_output=True, text=True, timeout=5)
            if cp.returncode == 0 and cp.stdout.strip():
                return cp.stdout.strip().split("/", 1)[-1]
        except (subprocess.TimeoutExpired, OSError):
            pass
    try:
        cp = subprocess.run(["git", "-C", str(repo_path), "symbolic-ref", "--short", "-q", "HEAD"],
                            capture_output=True, text=True, timeout=5)
        if cp.returncode == 0 and cp.stdout.strip():
            return cp.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return "main"


def build_command(slug: str, session_id: str, prompt: str, extra: list[str], add_dirs: list[str]) -> str:
    # Variadic options (--add-dir, --allowedTools, anything in extra) go first: the prompt is a positional and
    # would be swallowed by a variadic list placed right before it. --session-id ends the list.
    cmd = ["claude", *extra]
    if add_dirs:
        cmd += ["--add-dir", *add_dirs]
    cmd += ["--worktree", slug, "--session-id", session_id, prompt]
    return shlex.join(cmd)


def derive_status(task: dict, session: dict | None) -> str:
    """Kanban column from the task row plus its live session state.

    Order: archived > merged > pr > phase > session state. Phase comes after archived/merged/pr on purpose (a done task
    whose PR merged must sit in 'merged'), and before the session logic. A missing/None phase (legacy rows, hand-built
    dicts) and 'running' fall through to the session logic. Columns: backlog, in_progress, needs_you, done, pr, merged,
    archived."""
    if task.get("archived_at"):
        return "archived"
    if task.get("pr_state") == "MERGED" or task.get("status") == "merged":
        return "merged"
    if task.get("pr_url"):
        return "pr"
    st = (session or {}).get("state")
    phase = task.get("phase")
    if phase in ("backlog", "queued"):
        return "backlog"
    if phase == "failed":
        return "needs_you"
    if phase == "cancelled":
        return "done"
    if phase == "done":
        return "needs_you" if st in ("waiting", "errored") else "done"
    if session is None:
        return "done"
    if st in ("waiting", "errored"):
        return "needs_you"
    if st in ("done", "ended"):
        return "done"
    return "in_progress"


def worktree_path(repo_path: Path, slug: str, agent: str | None = "claude") -> Path:
    """Where a task's worktree lives: .claude/worktrees/<slug> for Claude, .ccboard/worktrees/<slug> for any other agent."""
    return repo_path / (WORKTREES if (agent or "claude") == "claude" else MANAGED_WORKTREES) / slug


def remove_worktree(repo_path: Path, slug: str, force: bool = False, agent: str | None = "claude",
                    path: Path | None = None) -> str | None:
    """git worktree remove (+ prune). Returns an error string or None. Pass the task's `agent` (or, better, its stored
    worktree as `path`): the default only knows Claude's folder."""
    wt = path if path is not None else worktree_path(repo_path, slug, agent)
    if not wt.exists():
        subprocess.run(["git", "-C", str(repo_path), "worktree", "prune"], capture_output=True, timeout=10)
        return None
    args = ["git", "-C", str(repo_path), "worktree", "remove"] + (["--force"] if force else []) + [str(wt)]
    try:
        cp = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "git worktree remove timed out"
    if cp.returncode != 0:
        return (cp.stderr or cp.stdout).strip()[-300:] or "git worktree remove failed"
    return None
