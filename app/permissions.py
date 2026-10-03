"""Remote approve/deny: the PermissionRequest hook long-polls here; decisions come from the board,
an ntfy action button or Web Push click. No decision in time -> the TUI prompt (unchanged flow)."""
from __future__ import annotations

import asyncio
import json
import threading

from . import notify

_waiters: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}
_lock = threading.Lock()


def summarize(tool_name: str, tool_input) -> str:
    ti = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "Bash":
        s = str(ti.get("command") or "")
    elif tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Read"):
        s = str(ti.get("file_path") or ti.get("notebook_path") or "")
    elif tool_name in ("WebFetch", "WebSearch"):
        s = str(ti.get("url") or ti.get("query") or "")
    else:
        try:
            s = json.dumps(ti, ensure_ascii=False)
        except (TypeError, ValueError):
            s = str(ti)
    s = " ".join(s.split())
    return f"{tool_name}: {s}"[:300]


def register_waiter(pid: int) -> asyncio.Event:
    ev = asyncio.Event()
    with _lock:
        _waiters[pid] = (asyncio.get_running_loop(), ev)
    return ev


def drop_waiter(pid: int) -> None:
    with _lock:
        _waiters.pop(pid, None)


def wake(pid: int) -> None:
    with _lock:
        w = _waiters.get(pid)
    if w:
        loop, ev = w
        loop.call_soon_threadsafe(ev.set)


def push_request(pid: int, name: str, summary: str) -> None:
    """The phone notice for one pending permission: Allow / Deny / Terminal and the `? Bash: npm test` line, on ntfy and Web Push.
    Never throttled (every request needs an answer), but it counts as the session's 'waiting' notice, so the Notification hook
    that follows when the TUI prompt shows does not buzz a second time."""
    try:
        row, task = notify.context(name)
        n = notify.build(row or {"tmux_name": name}, task, "waiting", "permission_prompt", summary,
                         {"id": pid, "summary": summary})
        notify.mark_sent(name, "waiting")
        notify.send(n)
    except Exception as e:          # the permission hook must still get its answer when a notice cannot be built
        notify.log.warning("permission push failed: %s", e)
