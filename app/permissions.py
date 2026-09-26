"""Remote approve/deny: the PermissionRequest hook long-polls here; decisions come from the board,
an ntfy action button or Web Push click. No decision in time -> the TUI prompt (unchanged flow)."""
from __future__ import annotations

import asyncio
import json
import threading

from . import notify
from .config import settings

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
    pub = (settings.public_url or "").rstrip("/")
    label = name.replace("--", " / ", 1).replace("--", " · ")
    actions = []
    if pub:
        actions = [
            {"action": "http", "label": "Allow", "url": f"{pub}/api/permission/{pid}/allow", "method": "POST",
             "headers": {"X-CCBoard": "1"}, "clear": True},
            {"action": "http", "label": "Deny", "url": f"{pub}/api/permission/{pid}/deny", "method": "POST",
             "headers": {"X-CCBoard": "1"}, "clear": True},
            {"action": "view", "label": "Terminal", "url": f"{pub}/term/{name}"},
        ]
    notify.web_push(f"{label}: allow?", summary, f"/#s={name}", tag=f"perm-{pid}")
    notify.publish(f"{label}: allow?", summary, click=f"{pub}/#s={name}" if pub else None, actions=actions,
                   priority=4, tags=["question"])
