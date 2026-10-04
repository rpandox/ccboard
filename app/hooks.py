"""Claude Code hook intake: token check, session resolution, per-session state machine."""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
from pathlib import Path

from . import accounts, agents, notify, permissions, projects, samples, tmux
from .agents.claude import ELICITATION_DONE, SESSION_ID_RE, WAIT_KIND, WAITING_NOTIFICATIONS, parse_limit_message, statusline_stats
from .config import settings
from .db import SKIP_EVENTS, now as db_now

log = logging.getLogger("ccboard.hooks")

TOKEN_HEADER = "x-ccboard-token"
MAX_BODY = 512 * 1024

# WAITING_NOTIFICATIONS (the notification types that mean "waiting for you") and WAIT_KIND (what each waits for) live in the Claude
# adapter, imported above: one table. The dead agent_needs_input / input_needed never fire for tmux-launched sessions.
ATTENTION_STATES = {"waiting", "done", "errored"}
STATES = ("idle", "working", "waiting", "done", "errored", "ended")

# A UserPromptSubmit that is not something the person typed: the harness (task notifications, reminders), a paste placeholder or a
# slash/bash echo. It flips the row to working but never becomes `last_prompt` (flags.last_system_turn keeps it instead).
SYSTEM_TURNS = (("<task-notification>", "task-notification"), ("<system-reminder>", "system"), ("[SYSTEM NOTIFICATION", "system"),
                ("<pasted_content", "paste"), ("<command-name>", "command"), ("<command-message>", "command"),
                ("<local-command-", "local"), ("<bash-input>", "local"))
SYSTEM_HEAD = 160
STOP_HEAD = 300
STATUSLINE_SAMPLE_MAX = 8 * 1024                 # kv statusline_sample (the box's real statusline shape, for the doctor)

# Called as fn(db, session name, stats) right after a statusline's stats are stored (a failing one is logged and dropped).
# main.py appends its passive /command confirmation here at import time.
STATS_HOOKS: list = []

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
    """Return (tmux session name, how). Order: CCBOARD_SESSION env, the payload session_id of exactly one open row, $TMUX_PANE on
    our socket, cwd (the one open row of that repo). The Tailer bind of an unbound Codex row (rollout cwd + time) joins in v0.5.12
    (see bind_unbound_rows)."""
    env_name = (headers.get("x-ccboard-session") or "").strip()
    if env_name == "none":
        return None, "ignored"       # a one-shot claude -p the board ran itself (PR description)
    name = _valid(env_name)
    if name:
        return name, "env"
    sid = payload.get("session_id")
    if isinstance(sid, str) and SESSION_ID_RE.match(sid):
        # the agent's own conversation id (Codex: bound by its first SessionStart, or by the rollout Tailer in v0.5.12) names the row
        # when the hook lost its env (a Codex that did not inherit the tmux environment): only when exactly one open row owns it
        owners = [n for n, row in open_rows.items() if row.get("claude_session_id") == sid]
        if len(owners) == 1:
            return owners[0], "session_id"
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
    if cwd and isinstance(cwd, str):
        try:
            target = Path(cwd).resolve()
        except (OSError, ValueError, RuntimeError):
            target = None
        if target is not None:
            matches = []
            for n, row in open_rows.items():
                try:
                    where = {projects.repo_path(row["project"], row["repo"]).resolve()}
                    if isinstance(row.get("cwd"), str) and row["cwd"]:
                        where.add(Path(row["cwd"]).resolve())         # sessions.cwd: a task's own worktree (Codex tasks start in it)
                    if target in where:
                        matches.append(n)
                except (projects.BadRequest, OSError, ValueError, RuntimeError):
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


def hook_agent(row_agent: str | None, header_agent: str | None) -> str | None:
    """Which agent's adapter reads a hook: the row's own agent when it has an adapter (a Claude row is never relabelled by a header),
    else the X-CCBoard-Agent header the hook script sent (a shell row where the person ran `codex` by hand), else the row's value as
    is (shell, None: read the way Claude's are)."""
    known = agents.names()
    if row_agent in known:
        return row_agent
    header = (header_agent or "").strip().lower()
    return header if header in known else row_agent


def agent_mismatch(row_agent: str | None, header_agent: str | None) -> bool:
    """Does the hook name one adapter agent while the row it resolved to belongs to a different one? True means it is somebody else's
    event: a Codex run that is not the row's (Hermes, `codex exec`, Codex Desktop: ccboard's hooks sit in the global hooks.json, so
    every Codex process on the box fires at the board), or a Claude hook from a run that merely shares the row's cwd. Such a hook is
    ignored before anything is written. A shell row is not an adapter agent, so it keeps taking hooks from either (the person may run
    `claude` or `codex` by hand in it), and so does a hook that names no agent or one this build does not know."""
    known = agents.names()
    header = (header_agent or "").strip().lower()
    return row_agent in known and header in known and header != row_agent


def bind_unbound_rows(db) -> int:
    """Identity step 3 for Codex (plan v0.5.11 'Identity join order'), a seam until v0.5.12: bind open codex rows that have no
    conversation id yet to their rollout (cwd + start time, FIFO, ambiguity left unbound). The rollout Tailer lands in v0.5.12 with
    `agents.codex.bind_unbound_rows(db)`; until that function exists this returns 0 and binds nothing. Steps 1 (the CCBOARD_SESSION
    env header), 2 (the payload session_id, resolve_session) and 4 (the cwd match) are live. Never raises."""
    fn = getattr(getattr(agents, "codex", None), "bind_unbound_rows", None)
    if not callable(fn):
        return 0
    try:
        return int(fn(db) or 0)
    except Exception as e:
        log.warning("codex bind_unbound_rows failed: %s", e)
        return 0


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


def _limit_account(db, limit: dict) -> str | None:
    """The subscription account that hit this limit: the one whose remembered 5 h / 7 d reset the episode's reset time matches, else the
    current account. None when no account is known (and never an error: the episode is recorded either way)."""
    try:
        resets = limit.get("resets_at")
        window = {"5h": "five_hour", "7d": "seven_day"}.get(limit.get("kind"))
        # a lookup only: the reset time of a message is parsed wall-clock text, a good guess but not a fingerprint to remember
        key = accounts.for_reading(db, {window: {"resets_at": resets}} if window and resets else {}, remember=False)
        return None if key == accounts.UNKNOWN else key
    except Exception as e:
        log.warning("accounts.for_reading (limit) failed: %s", e)
        return None


def _record_limit(db, name: str, limit: dict) -> None:
    samples.record_limit(db, name, limit, db.open_row(name), account=_limit_account(db, limit))


def _attribute_reading(db, name: str, row: dict | None, rate_limits: dict) -> tuple[str | None, bool]:
    """(account key, is it the current account) for a statusline's rate_limits; also keeps the session row's `account` current. The
    attribution is best effort: any failure answers (None, True), which is exactly the behaviour without account tracking."""
    try:
        acct = accounts.for_reading(db, rate_limits)
        is_current = acct == (accounts.current(db) or accounts.UNKNOWN)
        if acct != accounts.UNKNOWN and not is_current:
            # the reading matches an account the board does not think is logged in: either a session still on the old token (the
            # file still says the other account: one stat) or a /login back to this one that the 15 s tick has not seen yet. Look
            # at the file now so the pills, the scheduler guard and the banner follow on the first reading, not up to a minute later
            accounts.observe(db, force=True, auth=False)
            is_current = acct == (accounts.current(db) or accounts.UNKNOWN)
    except Exception as e:
        log.warning("accounts.for_reading failed: %s", e)
        return None, True
    if acct == accounts.UNKNOWN:                                  # no identity known: nothing to attribute, the series stay as they were
        return None, True
    if (row or {}).get("account") != acct:
        _sample(db.set_session_account, name, acct)
    return acct, is_current


def is_system_turn(prompt) -> str | None:
    """Is this UserPromptSubmit text something the harness or a slash/bash echo wrote rather than the person? Returns the kind
    ('task-notification', 'system', 'paste', 'command', 'local') or 'empty' for a prompt that is only whitespace; None for a real
    prompt and for anything that is not text. Pure; the notification bodies use it too (a system turn is never shown as the prompt)."""
    if not isinstance(prompt, str):
        return None
    head = prompt.lstrip()
    if not head:
        return "empty"
    for prefix, kind in SYSTEM_TURNS:
        if head.startswith(prefix):
            return kind
    return None


def _event_name(event) -> str:
    return event[:64] if isinstance(event, str) and event else "unknown"


def _mem_dirs() -> list[str]:
    """Where claude-mem keeps its data: its observer sessions run `claude` there and their hooks are not ours."""
    dirs = {Path.home() / ".claude-mem", settings.claude_config_dir.parent / ".claude-mem"}
    if os.environ.get("CLAUDE_MEM_DATA_DIR"):
        dirs.add(Path(os.environ["CLAUDE_MEM_DATA_DIR"]))
    return [os.path.normpath(str(d)) for d in dirs] + [os.path.realpath(str(d)) for d in dirs]


def _under(path: str, roots: list[str]) -> bool:
    return any(path == r or path.startswith(r + os.sep) for r in roots)


def _observer_transcript(tp: str) -> bool:
    """A transcript of claude-mem's observer: a path segment `observer-sessions` (cwd form) or the project-dir slug of such a cwd
    (`-home-x--claude-mem-observer-sessions-NNN`; the segment check keeps an unrelated `-observer-sessions` dir out)."""
    return any(seg == "observer-sessions" or "claude-mem-observer-sessions" in seg for seg in tp.split("/"))


def _foreign(db, name: str, payload: dict, sid: str | None, row: dict | None) -> bool:
    """Is this hook from a session that is not the one in `name`'s row: claude-mem's observer (cwd under ~/.claude-mem or a transcript
    in observer-sessions/), or a conversation another open row already owns. A new id on the SAME row is fine (a resumed conversation:
    SessionStart rebinds it)."""
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        roots = _mem_dirs()
        try:
            if _under(os.path.normpath(cwd), roots) or _under(os.path.realpath(cwd), roots):
                return True
        except (OSError, ValueError):
            pass
    tp = payload.get("transcript_path")
    if isinstance(tp, str) and _observer_transcript(tp):
        return True
    if sid and (row is None or row.get("claude_session_id") != sid):
        return any(n != name and r.get("claude_session_id") == sid for n, r in db.open_rows().items())
    return False


def _rebind_session_id(db, name: str, sid: str) -> None:
    """SessionStart: the conversation in this tmux session is `sid` now (db.set_state only fills a NULL id; a /resume or /clear starts a
    new conversation under a new id on the same row). Belongs in db.py; kept here because the hook slice owns only hooks.py."""
    with db.lock:
        db.conn.execute("UPDATE sessions SET claude_session_id=? WHERE id=(SELECT id FROM sessions WHERE tmux_name=? AND"
                        " ended_at IS NULL ORDER BY id DESC LIMIT 1)", (sid, name))


def _statusline_sample(payload: dict) -> dict:
    """The statusline payload as the box sent it, plus `at`, for the kv 'statusline_sample'. Over 8 KB the largest top-level keys are
    dropped (named under `_truncated`) so the value stays valid JSON."""
    out = {**payload, "at": db_now()}
    if len(out) > 200:                                           # a pathological shape: keep the stamp, name nothing (no quadratic walk)
        return {"at": out["at"], "_truncated": ["*"]}
    sizes = {k: len(json.dumps(v, default=str)) + len(json.dumps(str(k))) + 2 for k, v in out.items()}   # each key measured once
    total = sum(sizes.values()) + 2
    if total <= STATUSLINE_SAMPLE_MAX:
        return out
    dropped: list[str] = []
    for k in sorted((k for k in out if k != "at"), key=lambda k: -sizes[k]):
        dropped.append(str(k)[:40])
        total -= sizes[k]
        out.pop(k)
        if total + 20 + 46 * len(dropped[:30]) <= STATUSLINE_SAMPLE_MAX:  # the running estimate fits: one exact check, then done
            out["_truncated"] = dropped[:30]
            if len(json.dumps(out, default=str)) <= STATUSLINE_SAMPLE_MAX:
                return out
            out.pop("_truncated", None)
    return {"at": out["at"], "_truncated": ["*"]}


def _stats_hooks(db, name: str, stats: dict) -> None:
    for fn in STATS_HOOKS:
        _sample(fn, db, name, stats)


def _tool_batch(db, name: str, event: str, sid: str | None, row: dict | None) -> dict:
    """PostToolBatch: never an event row and never a last_event stamp, but a liveness signal (flags.last_tool_at) and the proof that a
    permission prompt was answered in the TUI: a row waiting on a permission goes back to working. An idle wait is left alone."""
    out = {"session": name, "event": event, "skipped": True}
    patch = {"last_tool_at": db_now()}
    flip = bool(row) and row.get("state") == "waiting" and (row.get("flags") or {}).get("wait_kind") == "permission"
    if flip:
        patch["wait_kind"] = None
    db.update_flags(name, patch)
    if flip:
        db.set_state(name, "working", event, claude_session_id=sid)
        out["state"] = "working"
    return out


def _subthread(adapter, row: dict | None, sid: str | None, event: str, payload: dict) -> bool:
    """Is this event from another thread than the one the row follows? Codex runs the hooks for every thread of the process (a
    guardian reviewer, a thread_spawn subagent), each under its own session_id and in the same pane with the same env, so such an
    event arrives at the main row. The adapter owns the rule (`thread_relation`: own | rebind | subthread; a SessionStart that says
    clear / resume / fork, or any SessionStart while the row is idle, done or ended, is the same session's next thread, a rebind: the
    row's state goes in). An adapter without that method (Claude) has no sub-threads. A sub-thread event is counted
    (flags.subthreads), recorded, and never touches the state."""
    rel = getattr(adapter, "thread_relation", None)
    if not callable(rel) or not sid or not (row or {}).get("claude_session_id"):
        return False
    return rel(event, payload, row["claude_session_id"], row.get("state")) == "subthread"


def _expire_permissions(db, name: str, decision: str) -> int:
    """Close every pending permission request of a session (an Interrupt answered them: Codex dropped the prompt) and wake each long
    poll so the hook script returns at once. Returns how many."""
    n = 0
    for p in db.perm_pending():
        if p["tmux_name"] == name:
            db.perm_expire(p["id"], decision)
            permissions.wake(p["id"])
            n += 1
    return n


def apply(db, name: str, event: str, payload: dict, agent: str | None = None, child: bool = False) -> dict:
    """Update the session row for one hook event. Returns what changed. `agent` is the agent whose adapter reads the payload and is
    recorded on the stored event: the row's own agent, or for a shell row the X-CCBoard-Agent the hook script sent (hook_agent).

    Order: guard (foreign sessions write nothing) -> statusline -> PostToolBatch -> the adapter reads the payload (every other event)
    -> one flags write -> state -> event row -> notification. Flags and state are on disk before the notification is built."""
    event = _event_name(event)
    p = payload if isinstance(payload, dict) else {}
    raw_sid = p.get("session_id")
    sid = raw_sid if isinstance(raw_sid, str) and SESSION_ID_RE.match(raw_sid) else None
    row = db.open_row(name)
    if _foreign(db, name, p, sid, row):
        return {"session": name, "event": event, "ignored": "foreign"}
    if child and not (sid and row and sid == row.get("claude_session_id")):
        # a nested claude (CLAUDE_CODE_CHILD_SESSION=1: claude-mem's observer, a workflow or SDK subagent) inherits the parent's
        # CCBOARD_SESSION and TMUX_PANE; its hooks are not this row's unless it is the row's own conversation
        return {"session": name, "event": event, "ignored": "child"}

    if event == "statusline":
        stats = statusline_stats(p)
        db.set_stats(name, stats, claude_session_id=sid)
        _stats_hooks(db, name, stats)
        acct, is_current = None, True
        if stats["rate_limits"]:
            # whose windows are these? A session still running on account A's token keeps reporting A after the shared login moved to
            # B: such a reading belongs to A's own series, and the current-account views (the pills, the scheduler's quota guard, the
            # limit banner: kv rate_limits and the 'claude' series) keep B's numbers
            acct, is_current = _attribute_reading(db, name, row, stats["rate_limits"])
            if is_current:
                db.kv_set("rate_limits", stats["rate_limits"])
        _sample(db.kv_set, "statusline_sample", _statusline_sample(p))
        # the statusline is Claude Code's own: the rate-limit series are keyed 'claude' whatever the row's agent says
        _sample(samples.record_statusline, db, name, stats, "claude", account=acct, current=is_current)
        return {"session": name, "event": event, "stats": True}

    if event == "PostToolBatch":
        return _tool_batch(db, name, event, sid, row)
    if event in SKIP_EVENTS:
        return {"session": name, "event": event, "skipped": True}

    adapter = _adapter(agent)
    if _subthread(adapter, row, sid, event, p):
        db.update_flags(name, None, {"subthreads": 1})
        db.add_event(name, event, "subthread", None, p, agent=agent)           # recorded and counted, never a state change
        return {"session": name, "event": event, "ignored": "subthread"}
    n = adapter.normalise_hook(event, p)
    if n.ignored:
        return {"session": name, "event": event, "ignored": n.ignored}
    state, kind, message, prompt, attention = n.state, n.kind, n.message, n.prompt, n.attention
    flags, now = dict(n.flags), db_now()
    old_flags = (row or {}).get("flags") or {}

    if event == "SessionStart":
        if sid:
            _rebind_session_id(db, name, sid)
        if isinstance(flags.get("resumed"), dict):
            flags["resumed"] = {"at": now, **flags["resumed"]}
    elif event == "UserPromptSubmit":
        sys_kind = is_system_turn(prompt)
        if sys_kind:
            flags["last_system_turn"] = {"kind": sys_kind, "head": prompt.strip()[:SYSTEM_HEAD], "at": now}
            prompt = None                                       # still working, but last_prompt stays the person's own words
    elif event == "Notification":
        # a completion that closes the wait the row is in (an elicitation form or URL answered): back to working
        if kind in ELICITATION_DONE and old_flags.get("wait_kind") == "elicitation":
            flags["wait_kind"] = None
            if (row or {}).get("state") == "waiting":
                state = "working"
    elif event == "Stop":
        # the turn's own closing words when the payload has them, else the last line of the screen as before
        message = " ".join(n.message.split())[:STOP_HEAD] if n.message else ""
        message = message or _last_screen_line(name)
    elif event == "StopFailure":
        # The live payload carries the error TYPE in `error` ("rate_limit") and the text in last_assistant_message; older shapes
        # carry error_type / error_category / matcher with `error` as the text. The adapter reads both.
        kind = n.kind or "error"
        message = n.message or str(kind)
        limit = n.limit
        legacy_kind = str(p.get("error_type") or p.get("error_category") or p.get("matcher") or "").lower()
        if limit is None and "rate" in legacy_kind and "limit" in legacy_kind:      # the old reading, only as a fallback
            limit = {**parse_limit_message(message), "message": message}
        if limit is not None:
            _rate_limited(db, name, limit, message)

    if event == "Interrupt" and n.kind == "interrupt":
        _expire_permissions(db, name, "interrupt")            # the turn was cancelled: a prompt still on the board has nothing to answer
    if flags or n.incr:
        db.update_flags(name, flags, n.incr)
    db.set_state(name, state, event, message=message, prompt=prompt, claude_session_id=sid, attention=attention)
    if db.add_event(name, event, str(kind) if kind else None, message, p, agent=agent):
        try:
            project = tmux.split_name(name)[0]
        except ValueError:
            project = None
        if project:
            _sample(samples.bump_event, db, project)
    if attention and state:
        notify.notify_session(name, state, message, str(kind) if kind else None)
    return {"session": name, "event": event, "state": state, "kind": kind}
