"""Cross-worktree overlap: warn when two open tasks of the same repo touch the same files."""
from __future__ import annotations

import json
import logging
import threading
import time
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


# ---------------------------------------------------------------- the cross-person input (issue #199, Team P3)
#
# The changed files of a teammate's open pull request (app/team_github.py reads them, app/team.py compares them) meet the files of this board's own open tasks here. The task's files
# are the ones `compute` already uses; they are looked at again at most every FILES_TTL seconds, so a page that polls every few seconds does not run the diff every time. Local file
# names stay on this board: they go to the signed-in owner's page and nowhere else, and never into a presence line.

FILES_TTL = 60.0
FILES_MAX = 1000
FILES_CACHE_MAX = 500                                       # tasks kept; past this the oldest entry goes, never the whole cache
FILES_WAIT_S = 30.0                                         # how long a second caller waits for the first one's diff of the same task
_files_cache: dict[int, tuple[float, str, str, list[str] | None]] = {}
_files_lock = threading.Lock()
_files_inflight: dict[int, threading.Event] = {}            # task id -> set once the diff running for it has been stored


def task_files(t: dict, mono=time.monotonic) -> list[str] | None:
    """The sorted files an open task touches (its branch against its base, uncommitted and untracked files), at most FILES_MAX, or None when the task has no worktree on disk (not
    known: that is not the same as no files). Cached for FILES_TTL seconds per task and worktree. One diff runs at a time for a task: a second caller (another poller of the same
    page) waits for the first one's answer and reuses it instead of running the diff again."""
    wt = tasks_mod.task_worktree(t)
    if wt is None:
        return None
    key, base = str(wt), t.get("base") or "main"
    mine: threading.Event | None = None
    while mine is None:
        with _files_lock:
            hit = _files_cache.get(t["id"])
            if hit is not None and hit[1] == key and hit[2] == base and mono() - hit[0] < FILES_TTL:
                return hit[3]
            other = _files_inflight.get(t["id"])
            if other is None:
                mine = _files_inflight[t["id"]] = threading.Event()
        if other is not None and not other.wait(FILES_WAIT_S):
            break                                           # the first one is stuck: do not wait on it again, run the diff here
    try:
        files: list[str] | None = sorted(changed_files(wt, base))[:FILES_MAX] if wt.is_dir() else None
    except Exception as e:                                  # a worktree that cannot be read is "not known", never a failed page
        log.debug("task files could not be read: %s", e.__class__.__name__)
        files = None
    with _files_lock:
        _files_cache.pop(t["id"], None)                     # re-inserted last, so the oldest entry is the first one
        _files_cache[t["id"]] = (mono(), key, base, files)
        while len(_files_cache) > FILES_CACHE_MAX:
            del _files_cache[next(iter(_files_cache))]
        if mine is not None and _files_inflight.get(t["id"]) is mine:
            del _files_inflight[t["id"]]
    if mine is not None:
        mine.set()
    return files


def forget_files() -> None:
    with _files_lock:
        _files_cache.clear()
