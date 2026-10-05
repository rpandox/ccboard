"""A user-initiated usage refresh (v0.5.17f, part 2): ask one idle Claude session of the board for /usage, so Claude Code fetches the
official 5-hour / 7-day numbers with its own login and writes them to its state file, where accounts.poll_usage_cache already reads them.

Nothing here touches a credential file, nothing runs on a timer and nothing stays running: one tap (or the Usage page opening on stale
numbers) is one /usage typed into one pane, one Escape a few seconds later on a short-lived thread, and one more read of the cache.

    panes(live, rows, viewers)      the board's live Claude panes: [{name, row, full, at}]  (pure)
    choose(panes, typeable, ...)    the pane to ask: an unattached one first, then the most recently active  (pure)
    busy() / claim() / release()    one refresh at a time (a 20 s lock that frees itself)
    later(delay, fn)                the one place a thread is started (tests run `fn` at once instead)
    view(db)                        state.usage_refresh: {running, last}
    record(db, session, at, ...)    the kv `usage_refresh` = {at, session, ok, error?}

The guards (state, permission, compaction) are the command route's own: main.py hands `choose` its `_typing_refusal` as `typeable`, and
`/usage` goes through the same typing helper the route uses (`_type_command`), so the pane sees exactly what cmd=usage would send."""
from __future__ import annotations

import logging
import threading
import time

from . import claude_auth, tmux

log = logging.getLogger("ccboard.usage_refresh")

KV = "usage_refresh"
LOCK_S = 20.0                      # one refresh at a time; the lock frees itself after this long even if its thread never reported back
ESCAPE_AFTER_S = 4.0               # /usage is typed, Claude Code fetches the numbers and draws its panel; this long after, Escape closes the panel
NO_SESSION = "no Claude session is at its prompt; start one to refresh"
BUSY = "a refresh is already running"

_lock = threading.Lock()
_flight = {"until": 0.0}           # monotonic time at which the lock lapses (0 = free)


def _clock() -> float:
    """The one monotonic clock of the lock (tests replace it)."""
    return time.monotonic()


def busy() -> bool:
    """Is a refresh in flight (claimed and neither released nor past its 20 s)?"""
    with _lock:
        return _flight["until"] > _clock()


def claim() -> bool:
    """Take the lock for LOCK_S seconds; False when a refresh already holds it."""
    with _lock:
        now = _clock()
        if _flight["until"] > now:
            return False
        _flight["until"] = now + LOCK_S
        return True


def release() -> None:
    with _lock:
        _flight["until"] = 0.0


def later(delay: float, fn) -> None:
    """Run fn() once, `delay` seconds from now, on a daemon thread that ends with it. The only thread this module starts."""
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


def panes(live: dict, rows: dict, viewers: dict | None) -> list[dict]:
    """The live Claude panes: [{name, row, full, at}] over tmux's sessions `live` (list_sessions) and the open rows `rows` (db.open_rows).
    A pane counts when it is a board session (never an internal `_ccboard*` one) whose open row belongs to the Claude adapter (a Codex
    or shell row never does; a row with no agent is Claude's, like the command route reads it) and whose pane runs something other than
    a login shell (claude's process name is its version string, as claude_auth.login_state reads it). `full` is the number of people
    attached with a window of their own (`viewers`, tmux.viewers(); None = list-clients failed: every attached client counts as one);
    `at` is the row's state_at, the 'most recently active' of the choice."""
    out = []
    for name, s in live.items():
        if tmux.is_internal(name):
            continue
        row = rows.get(name)
        if not row or (row.get("agent") or "claude") != "claude":
            continue
        cmd = str((s or {}).get("command") or "").lstrip("-")
        if not cmd or cmd in claude_auth.SHELLS:
            continue
        full = int((viewers.get(name) or {}).get("full") or 0) if viewers is not None else int((s or {}).get("attached") or 0)
        out.append({"name": name, "row": row, "full": full, "at": str(row.get("state_at") or "")})
    return out


def choose(candidates: list[dict], typeable, unattached_only: bool = False) -> dict | None:
    """The pane to ask for /usage, or None. `typeable(pane) -> bool` is the command route's guard (idle, done, errored or idle-waiting,
    no permission pending, not compacting). Panes with no full viewer come first, then the most recently active; `unattached_only` (the
    Usage page opening on its own) drops every pane somebody is attached to: never into a terminal a person is looking at."""
    ok = [p for p in candidates if typeable(p) and not (unattached_only and p["full"] > 0)]
    ok.sort(key=lambda p: p["at"], reverse=True)         # newest first; the sort below is stable, so it keeps that order inside each group
    ok.sort(key=lambda p: p["full"] > 0)
    return ok[0] if ok else None


def record(db, session: str, at: str, ok: bool = True, error: str | None = None) -> dict:
    """kv `usage_refresh` = {at, session, ok, error?}: the newest refresh this board asked for (at = when it was typed, ISO UTC)."""
    rec = {"at": at, "session": session, "ok": bool(ok)}
    if error:
        rec["error"] = str(error)[:200]
    db.kv_set(KV, rec)
    return rec


def view(db) -> dict:
    """state.usage_refresh: {running: a refresh is in flight, last: the kv record or None}. The Usage page greys its button while one runs
    (it may have been asked from another device) and reads `last` for its tooltip."""
    try:
        last = db.kv_get(KV)
    except Exception as e:
        log.warning("usage_refresh view failed: %s", e.__class__.__name__)
        last = None
    last = last.get("value") if isinstance(last, dict) and isinstance(last.get("value"), dict) else last
    return {"running": busy(), "last": last if isinstance(last, dict) else None}
