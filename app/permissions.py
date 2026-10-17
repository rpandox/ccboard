"""Remote approve/deny: the PermissionRequest hook long-polls here; decisions come from the board,
an ntfy action button or Web Push click. No decision in time -> the TUI prompt; recognized Claude tool dialogs remain answerable here."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import threading
import time
from weakref import WeakKeyDictionary

from . import notify

_waiters: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}
_lock = threading.Lock()


PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File:[ \t]*(.+?)[ \t\r]*$", re.M)     # the file lines of an apply_patch envelope


def _patch_text(tool_input) -> str:
    """The patch text of a Codex apply_patch request: the `input` or `command` of a dict (a command may be argv, ["apply_patch", text]),
    or the plain string the tool was given."""
    if isinstance(tool_input, str):
        return tool_input
    ti = tool_input if isinstance(tool_input, dict) else {}
    for key in ("input", "command"):
        v = ti.get(key)
        if isinstance(v, (list, tuple)):
            v = "\n".join(str(x) for x in v)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def summarize(tool_name: str, tool_input) -> str:
    ti = tool_input if isinstance(tool_input, dict) else {}
    patch = _patch_text(tool_input) if tool_name == "apply_patch" else ""
    if patch:
        # the files the patch touches, not its body; with no file lines (a malformed patch) whatever text there is, cut below
        s = ", ".join(dict.fromkeys(PATCH_FILE_RE.findall(patch))) or patch
    elif tool_name == "Bash":
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


def push_request(pid: int, name: str, summary: str, *, context_row=None) -> None:
    """The phone notice for one pending permission: Allow / Deny / Terminal and the `? Bash: npm test` line, on ntfy and Web Push.
    Never throttled (every request needs an answer), but it counts as the session's 'waiting' notice, so the Notification hook
    that follows when the TUI prompt shows does not buzz a second time."""
    try:
        row, task = (context_row, None) if context_row else notify.context(name)
        n = notify.build(row or {"tmux_name": name}, task, "waiting", "permission_prompt", summary,
                         {"id": pid, "summary": summary})
        notify.mark_sent(name, "waiting")
        notify.send(n)
    except Exception as e:          # the permission hook must still get its answer when a notice cannot be built
        notify.log.warning("permission push failed: %s", e)


# Terminal requests live only in this process. A restart invalidates their ids; no captured screen is stored in the database.
_terminal = WeakKeyDictionary()
_terminal_reads = WeakKeyDictionary()
_terminal_lock = threading.RLock()
TERMINAL_ID_MIN = 100_000_000_000
TERMINAL_CHANGED = "the terminal prompt changed or is no longer available; nothing was sent. Refresh and check it again"


def terminal_observe(cache, key, name, read, *, rearm=False, **context):
    """Shared one-shot lifecycle and public redaction for local and device screens. Caller holds its lock.
    Local callers keep the original rule: an answered prompt stays suppressed while the same screen returns, even after a transient
    empty read. Device polling opts in with rearm=True, where an empty read ends the prompt and the same screen is a new one."""
    from . import nodes_relay
    from .db import now
    old = cache.get(key)
    if not read:
        if old:
            old["active"] = False
        return None
    dialog, fingerprint = read
    if old and old["answered"] and old["fingerprint"] == fingerprint and (old["active"] or not rearm):
        return None
    if not old or old["fingerprint"] != fingerprint or not old["active"]:
        known = nodes_relay.known_secrets()
        public = {"id": TERMINAL_ID_MIN + secrets.randbelow(900_000_000_000), "kind": "terminal_prompt",
                  "tmux_name": name, "tool_name": dialog["tool_name"],
                  "summary": nodes_relay._perm_summary(dialog["summary"], known), "created_at": now(),
                  "choices": [{"number": c["number"], "label": nodes_relay._perm_summary(c["label"], known)}
                              for c in dialog["choices"]], **context}
        old = cache[key] = {"public": public, "fingerprint": fingerprint, "active": True, "answered": False}
    return dict(old["public"]) if not old["answered"] else None


def terminal_consume(old, read):
    """A fresh matching screen is required; consume before a transport can lose its acknowledgement."""
    from . import projects
    if not old or not old["active"] or old["answered"]:
        raise projects.Conflict(TERMINAL_CHANGED)
    if not read or read[1] != old["fingerprint"]:
        old["active"] = False
        raise projects.Conflict(TERMINAL_CHANGED)
    old["answered"] = True
    return read[0]


def _terminal_read(db, name, *, row=None, panes=None):
    from . import tmux
    from .agents import claude_pane
    row = row if row is not None else db.open_row(name)
    if not row or (row.get("agent") or "claude") != "claude" or tmux.is_internal(name):
        return None
    try:
        pane = (panes if panes is not None else tmux.list_sessions()).get(name)
        if not pane or not claude_pane.foreground(pane.get("command")):
            return None
        text = tmux.capture(name, lines=0)     # current screen only, never an old dialog in scrollback
        dialog = claude_pane.approval_dialog(text)
    except (tmux.TmuxError, tmux.TmuxDown, OSError):
        return None
    if not dialog:
        return None
    fingerprint = hashlib.sha256(json.dumps([row.get("row_id"), row.get("id"), row.get("state_at"), pane.get("pane_id"), pane.get("command"), text]).encode()).hexdigest()
    return dialog, fingerprint


def terminal_pending(db, live: list[dict], *, max_age: float) -> list[dict]:
    from . import hooks, tmux
    with _terminal_lock:
        cache = _terminal.setdefault(db, {})
        reads = _terminal_reads.setdefault(db, {})
        blocked = {p["tmux_name"] for p in live}
        rows = db.open_rows()
        try:
            panes = tmux.list_sessions()
        except (tmux.TmuxError, tmux.TmuxDown, OSError):
            panes = {}
        for name in list(cache):
            if name not in rows:
                del cache[name]
        for name in list(reads):
            if name not in rows:
                del reads[name]
        out = []
        for name, row in rows.items():
            attention = row.get("state") in hooks.ATTENTION_STATES and not row.get("acked_at")
            eligible = name not in blocked and (row.get("state") == "waiting" or attention)
            pane = panes.get(name) or {}
            stamp = (row.get("id"), row.get("state"), row.get("state_at"), row.get("acked_at"), row.get("agent"), pane.get("pane_id"), pane.get("command"))
            prior = reads.get(name)
            if not eligible:
                reads.pop(name, None)
                read = None
            elif prior and prior[1] == stamp and time.monotonic() - prior[0] < max_age:
                read = prior[2]
            else:
                read = _terminal_read(db, name, row=row, panes=panes)
                reads[name] = (time.monotonic(), stamp, read)
            public = terminal_observe(cache, name, name, read)
            if public:
                out.append(public)
        return out


def terminal_get(db, pid: int) -> dict | None:
    with _terminal_lock:
        return next((dict(r["public"]) for r in _terminal.get(db, {}).values() if r["public"]["id"] == pid), None)


def terminal_answer(db, pid: int, decision: str, source: str, live) -> None:
    from . import projects, tmux
    with _terminal_lock:
        old = next((r for r in _terminal.get(db, {}).values() if r["public"]["id"] == pid), None)
        if not old or not old["active"] or old["answered"]:
            raise projects.Conflict(TERMINAL_CHANGED)
        name = old["public"]["tmux_name"]
        if decision not in ("allow", "deny"):
            raise projects.BadRequest("a terminal prompt accepts only allow or deny")
        if any(p["tmux_name"] == name for p in live()):
            raise projects.Conflict(TERMINAL_CHANGED)
        _terminal_reads.get(db, {}).pop(name, None)
        read = _terminal_read(db, name)
        terminal_consume(old, read)
        tmux.send_text(name, read[0][decision], enter=False)
        db.add_event(name, "TerminalPermission", read[0]["tool_name"], decision, {"id": pid, "source": source}, agent="claude")
