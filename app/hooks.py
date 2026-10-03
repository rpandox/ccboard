"""Claude Code hook intake: token check, session resolution, per-session state machine."""
from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
from pathlib import Path

from . import agents, notify, projects, samples, tmux
from .agents.claude import parse_limit_message
from .config import settings
from .db import SKIP_EVENTS, now as db_now

log = logging.getLogger("ccboard.hooks")

TOKEN_HEADER = "x-ccboard-token"
MAX_BODY = 512 * 1024

# Hook events we register, mapped to the session state they imply (None = keep the state).
WAITING_NOTIFICATIONS = {"permission_prompt", "idle_prompt", "elicitation_dialog", "agent_needs_input", "input_needed"}
ATTENTION_STATES = {"waiting", "done", "errored"}
STATES = ("idle", "working", "waiting", "done", "errored", "ended")

_token: str | None = None


def token_path() -> Path:
    return settings.data_dir / "hook-token"


def ensure_token() -> str:
    """Create (0600) or read the shared secret the hook scripts send back."""
    global _token
    if _token:
        return _token
    p = token_path()
    try:
        _token = p.read_text().strip()
    except OSError:
        _token = ""
    if not _token:
        p.parent.mkdir(parents=True, exist_ok=True)
        _token = secrets.token_hex(32)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(_token + "\n")
    return _token


def check_token(given: str | None) -> bool:
    if not given:
        return False
    return hmac.compare_digest(given.strip(), ensure_token())


def _our_socket(tmux_env: str | None) -> bool:
    # $TMUX looks like /tmp/tmux-1000/ccboard,12345,0
    if not tmux_env:
        return False
    return f"/tmux-{os.getuid()}/{settings.tmux_socket}," in tmux_env


def _valid(name: str | None) -> str | None:
    if not name:
        return None
    try:
        tmux.split_name(name)
        return name
    except ValueError:
        return None


def resolve_session(headers, payload: dict, open_rows: dict[str, dict]) -> tuple[str | None, str]:
    """Return (tmux session name, how). Order: CCBOARD_SESSION env, $TMUX_PANE on our socket, cwd."""
    env_name = (headers.get("x-ccboard-session") or "").strip()
    if env_name == "none":
        return None, "ignored"       # a one-shot claude -p the board ran itself (PR description)
    name = _valid(env_name)
    if name:
        return name, "env"
    pane = headers.get("x-ccboard-pane") or ""
    if re.fullmatch(r"%\d+", pane) and _our_socket(headers.get("x-ccboard-tmux")):
        try:
            cp = tmux.run("display-message", "-p", "-t", pane, "#{session_name}", check=False)
            name = _valid(cp.stdout.strip()) if cp.returncode == 0 else None
            if name:
                return name, "pane"
        except tmux.TmuxError:
            pass
    cwd = payload.get("cwd")
    if cwd:
        try:
            target = Path(cwd).resolve()
        except OSError:
            target = None
        if target is not None:
            matches = []
            for n, row in open_rows.items():
                try:
                    if projects.repo_path(row["project"], row["repo"]).resolve() == target:
                        matches.append(n)
                except projects.BadRequest:
                    continue
            if len(matches) == 1:
                return matches[0], "cwd"
    return None, "unresolved"


CHROME_CHARS = set("─━│┃┌┐└┘├┤╭╮╯╰-=_ >·•")
CHROME_HINTS = ("? for shortcuts", "shift+tab", "esc to interrupt", "accept edits", "plan mode", "bypass permissions",
                "auto-accept", "ctrl+", "⏵⏵", "press enter", "Press Enter")
PROMPT_RE = re.compile(r"^(➜|\$|%|#|>)\s|[$%#]\s*$|git:\(")


def _last_screen_line(name: str) -> str | None:
    """Last visible line that looks like Claude's own output: skips box drawing, TUI footer hints,
    the input box and shell prompts (the fullscreen TUI lives on the alt screen, so the visible
    screen is what the user sees)."""
    try:
        text = tmux.capture(name, lines=0, join=True)
    except tmux.TmuxError:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        bare = ln.strip("─━│┃┌┐└┘├┤╭╮╯╰ ")
        if not bare or set(ln) <= CHROME_CHARS:
            continue
        if any(h.lower() in bare.lower() for h in CHROME_HINTS) or PROMPT_RE.search(bare):
            continue
        return bare[:300]
    return None


def _sample(fn, *args, **kw) -> None:
    """Time-series writes ride along on the hook path and must never break it: a failure is logged and dropped."""
    try:
        fn(*args, **kw)
    except Exception as e:
        log.warning("samples.%s failed: %s", getattr(fn, "__name__", "write"), e)


def _adapter(agent: str | None):
    """The adapter that reads this row's hook payloads; a shell or unknown agent is read the way Claude's are."""
    try:
        return agents.get(agent or "claude")
    except KeyError:
        return agents.get("claude")


def _same_episode(prev: dict | None, name: str, kind: str, resets_at) -> bool:
    """Is this rate-limit failure the episode the kv 'rate_limited' record already describes? A reset time names the account-wide
    window, so the same kind and reset time is the same episode whichever session reports it; without one it is the same session
    in the same UTC hour. Subagents fail on their own and one session retried 482 times in a single episode: the person is told
    once."""
    v = prev.get("value") if isinstance(prev, dict) else None
    if not isinstance(v, dict) or v.get("kind") != kind:
        return False
    if resets_at:
        return v.get("resets_at") == resets_at
    return v.get("session") == name and str(prev.get("at") or "")[:13] == db_now()[:13]


def _rate_limited(db, name: str, limit: dict, message: str) -> None:
    kind = str(limit.get("kind") or "other")
    resets_at = limit.get("resets_at")
    fresh = not _same_episode(db.kv_get("rate_limited"), name, kind, resets_at)
    db.kv_set("rate_limited", {"session": name, "message": message[:500], "kind": kind, "resets_at": resets_at})
    if fresh:
        notify.notify_rate_limit(name, message)
    _sample(_record_limit, db, name, {**limit, "kind": kind, "message": message[:500]})


def _record_limit(db, name: str, limit: dict) -> None:
    samples.record_limit(db, name, limit, db.open_row(name))


def apply(db, name: str, event: str, payload: dict, agent: str | None = None) -> dict:
    """Update the session row for one hook event. Returns what changed. `agent` (the row's agent) is recorded on the stored event."""
    sid = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
    kind = None
    message = None
    state: str | None = None
    attention = False
    prompt = None

    if event == "statusline":
        def d(key):
            v = payload.get(key)
            return v if isinstance(v, dict) else {}
        model, cw, cost = d("model"), d("context_window"), d("cost")
        stats = {
            "model": model.get("display_name"), "model_id": model.get("id"),
            "context_pct": cw.get("used_percentage"), "context_size": cw.get("context_window_size"),
            "cost_usd": cost.get("total_cost_usd"), "lines_added": cost.get("total_lines_added"),
            "lines_removed": cost.get("total_lines_removed"),
            "version": payload.get("version") if isinstance(payload.get("version"), str) else None,
            "rate_limits": d("rate_limits") or None,
        }
        db.set_stats(name, stats, claude_session_id=sid)
        if stats["rate_limits"]:
            db.kv_set("rate_limits", stats["rate_limits"])
        # the statusline is Claude Code's own: the rate-limit series are keyed 'claude' whatever the row's agent says
        _sample(samples.record_statusline, db, name, stats, "claude")
        return {"session": name, "event": event, "stats": True}

    if event in SKIP_EVENTS:
        # PostToolBatch: db.add_event never stores it (statusline is handled above), and it must not stamp last_event either
        return {"session": name, "event": event, "skipped": True}

    if event == "SessionStart":
        state = "idle"
        kind = payload.get("source") or payload.get("matcher")
    elif event == "UserPromptSubmit":
        state = "working"
        p = payload.get("prompt")
        prompt = p if isinstance(p, str) else None
    elif event == "Notification":
        kind = payload.get("notification_type") or payload.get("type") or payload.get("matcher")
        m = payload.get("message")
        message = m if isinstance(m, str) else None
        if kind in WAITING_NOTIFICATIONS:
            state, attention = "waiting", True
    elif event == "Stop":
        state, attention = "done", True
        message = _last_screen_line(name)
    elif event == "StopFailure":
        state, attention = "errored", True
        # The live payload carries the error TYPE in `error` ("rate_limit") and the text in last_assistant_message; older shapes
        # carry error_type / error_category / matcher with `error` as the text. The adapter reads both.
        n = _adapter(agent).normalise_hook(event, payload)
        kind = n.kind or "error"
        message = n.message or str(kind)
        limit = n.limit
        legacy_kind = str(payload.get("error_type") or payload.get("error_category") or payload.get("matcher") or "").lower()
        if limit is None and "rate" in legacy_kind and "limit" in legacy_kind:      # the old reading, only as a fallback
            limit = {**parse_limit_message(message), "message": message}
        if limit is not None:
            _rate_limited(db, name, limit, message)
    elif event == "SessionEnd":
        state = "ended"
        kind = payload.get("reason") or payload.get("matcher")
    else:
        kind = payload.get("matcher")

    db.set_state(name, state, event, message=message, prompt=prompt, claude_session_id=sid, attention=attention)
    if db.add_event(name, event, str(kind) if kind else None, message, payload, agent=agent):
        try:
            project = tmux.split_name(name)[0]
        except ValueError:
            project = None
        if project:
            _sample(samples.bump_event, db, project)
    if attention and state:
        notify.notify_session(name, state, message, str(kind) if kind else None)
    return {"session": name, "event": event, "state": state, "kind": kind}
