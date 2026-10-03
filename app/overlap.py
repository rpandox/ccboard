"""Cross-worktree overlap: warn when two open tasks of the same repo touch the same files."""
from __future__ import annotations

import json
import logging
from itertools import combinations
from pathlib import Path

from . import gitops, tasks as tasks_mod

log = logging.getLogger("ccboard.overlap")


def changed_files(wt: Path, base: str) -> set[str]:
    """Files changed on the branch (vs origin/<base>) plus uncommitted and untracked ones."""
    if not wt.is_dir():
        return set()
    ref = gitops.base_ref(wt, base)
    out: set[str] = set()
    for args in (["git", "diff", "--name-only", f"{ref}...HEAD"], ["git", "diff", "--name-only", "HEAD"],
                 ["git", "ls-files", "--others", "--exclude-standard"]):
        try:
            out.update(ln for ln in gitops.run(args, wt, check=False).stdout.splitlines() if ln)
        except gitops.GitError:
            continue
    return out


def compute(db) -> int:
    """Store on each open task the other tasks (same repo) that touch the same files. Returns #tasks with overlap."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for t in db.tasks():
        if t.get("status") in ("merged", "archived"):
            continue
        groups.setdefault((t["project"], t["repo"]), []).append(t)
    n = 0
    for tasks in groups.values():
        # a task without a worktree (backlog, queued, session mode) touches no files: Path('') is Path('.') and would diff the board's own cwd
        files = {t["id"]: changed_files(tasks_mod.task_worktree(t), t.get("base") or "main") if tasks_mod.has_worktree(t) else set()
                 for t in tasks}
        overlaps: dict[int, list[dict]] = {t["id"]: [] for t in tasks}
        for a, b in combinations(tasks, 2):
            common = sorted(files[a["id"]] & files[b["id"]])
            if common:
                overlaps[a["id"]].append({"task": b["id"], "title": b["title"], "files": common[:20]})
                overlaps[b["id"]].append({"task": a["id"], "title": a["title"], "files": common[:20]})
        for t in tasks:
            new = json.dumps(overlaps[t["id"]]) if overlaps[t["id"]] else None
            if new != t.get("overlap"):
                db.task_update(t["id"], overlap=new)
            if new:
                n += 1
    return n
