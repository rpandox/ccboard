"""Bulk clones: a small in-memory queue drained by a worker that keeps at most CAP clone sessions running."""
from __future__ import annotations

import logging
import threading
import time

from . import projects, tmux

log = logging.getLogger("ccboard.clonequeue")
CAP = 3
_lock = threading.Lock()
_queue: list[dict] = []          # {project, repo, url}
_done: list[dict] = []           # {project, repo, status, error?}
_inflight = 0                    # clones being launched right now (not yet visible in tmux)


def enqueue(items: list[dict]) -> int:
    with _lock:
        _queue.extend(items)
        return len(_queue)


def status() -> dict:
    with _lock:
        return {"queued": [dict(i) for i in _queue], "done": [dict(d) for d in _done[-50:]], "cap": CAP}


def clear_done() -> None:
    with _lock:
        _done.clear()


def running_clones() -> int:
    try:
        return sum(1 for n in tmux.list_sessions() if n.endswith("--clone"))
    except tmux.TmuxError:
        return CAP  # tmux down: launch nothing


def step(launch) -> int:
    """Launch as many queued clones as the cap allows. `launch(project, repo, path, url, cleanup)` starts one."""
    global _inflight
    started = 0
    while True:
        with _lock:
            if not _queue:
                return started
            if running_clones() + _inflight >= CAP:
                return started
            item = _queue.pop(0)
            _inflight += 1
        try:
            rname, rpath = projects.prepare_repo_clone(item["project"], item.get("repo"), item["url"])
            launch(item["project"], rname, rpath, item["url"], [rpath])
            with _lock:
                _done.append({"project": item["project"], "repo": rname, "status": "started"})
            started += 1
        except Exception as e:
            with _lock:
                _done.append({"project": item["project"], "repo": item.get("repo") or item["url"], "status": "failed", "error": str(e)[:200]})
            log.warning("bulk clone %s/%s failed: %s", item["project"], item.get("repo"), e)
        finally:
            with _lock:
                _inflight -= 1


class Worker(threading.Thread):
    def __init__(self, launch):
        super().__init__(name="clone-queue", daemon=True)
        self.launch = launch
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                with _lock:
                    empty = not _queue
                if not empty:
                    step(self.launch)
            except Exception as e:
                log.warning("clone queue step failed: %s", e)
            self.stop.wait(2.0)
