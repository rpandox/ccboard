"""Tasks: one Claude session in its own git worktree + branch, created by `claude --worktree`."""
from __future__ import annotations

import re
import shlex
import subprocess
import uuid
from pathlib import Path

from . import projects, tmux

SLUG_RE = re.compile(r"[^a-z0-9]+")
WORKTREES = ".claude/worktrees"
STATUSES = ("open", "pr", "merged", "archived")


def slugify(title: str) -> str:
    s = SLUG_RE.sub("-", title.lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:40].strip("-")
    return s or "task"


def unique_slug(repo_path: Path, base: str, taken: set[str]) -> str:
    slug = base
    n = 2
    while slug in taken or (repo_path / WORKTREES / slug).exists():
        slug = f"{base[:36]}-{n}"
        n += 1
    return slug


def session_name_for(slug: str) -> str:
    return f"t-{slug}"


def ensure_excluded(repo_path: Path) -> None:
    """Keep Claude's worktree folder out of `git status` without touching tracked files."""
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
        if ".claude/worktrees/" not in current:
            with exclude.open("a") as f:
                f.write(("" if current.endswith("\n") or not current else "\n") + ".claude/worktrees/\n")
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
    cmd = ["claude", "--worktree", slug, "--session-id", session_id, *extra]
    if add_dirs:
        cmd += ["--add-dir", *add_dirs]
    cmd.append(prompt)
    return shlex.join(cmd)


def derive_status(task: dict, session: dict | None) -> str:
    """Kanban column from the task row plus its live session state."""
    if task.get("archived_at"):
        return "archived"
    if task.get("pr_state") == "MERGED" or task.get("status") == "merged":
        return "merged"
    if task.get("pr_url"):
        return "pr"
    st = (session or {}).get("state")
    if session is None:
        return "done"
    if st in ("waiting", "errored"):
        return "needs_you"
    if st in ("done", "ended"):
        return "done"
    return "in_progress"


def worktree_path(repo_path: Path, slug: str) -> Path:
    return repo_path / WORKTREES / slug


def remove_worktree(repo_path: Path, slug: str, force: bool = False) -> str | None:
    """git worktree remove (+ prune). Returns an error string or None."""
    wt = worktree_path(repo_path, slug)
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
