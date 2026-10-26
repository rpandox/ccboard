"""What a Codex pane shows when the board must not type into it (box checks 86 and 92, codex-cli 0.161.0). Pure text rules, no tmux here.

  update dialog   "Update available 0.161.0 -> 0.162.0" with "1. Update now (runs a global npm install of Codex)" as the default: one Enter
                  typed by the board would run npm on the host. Shown at launch on nearly every start once a newer version exists.
  trust dialog    "Trust this folder?" on the first launch in a new repo (and "Continue only if you trust these files"). It swallows typed
                  text and blocks a task's prompt in argv until a person answers.
  login shell     codex exited (one box launch printed "account/read failed during TUI bootstrap ... workspace routing discovery timed out"
                  and went back to the shell): the pane's foreground process is the login shell, and a prompt typed there runs as a command.

The board never answers a dialog and never types a prompt into any of these; main.py refuses the typing (409), shows the row as needing a
person (dialog) or marks it errored with the reason (exit). The strings below are the ones the box findings quote; they are matched on the
last lines of the screen only, and a dialog counts only while it is the CURRENT screen: once Codex has drawn anything of its own after
it (its composer footer, the prompt line, a reply, its header box) the dialog was answered, however few lines have scrolled by since
(live check, issue 2 and 86: the answered trust dialog kept refusing prompt and restart for several turns on codex-cli 0.161).
"""
from __future__ import annotations

import re

SHELLS = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh"})
TAIL_LINES = 40                    # the dialog is the bottom of the screen; older scrollback may hold one that was answered

UPDATE_RE = re.compile(r"update available|update now|skip until next version|npm install -g\s@openai/codex", re.I)   # \s: the repo-wide scan forbids the literal install line
COMPOSER_RE = re.compile(r"context left|for shortcuts|esc to interrupt|reply wit", re.I)     # Codex's own footer once it is at its prompt
# Codex 0.161's footer in a narrow pane has none of those words: "GPT-6.1-Sol default · <path> · Reply wit…" (model and mode, the folder, a
# hint, cut at the pane width). Two " · " separators on one line, which no dialog line has.
FOOTER_RE = re.compile(r"^\s*\S+(?:\s\S+)?\s+·\s+\S.*\s·\s+\S")
# What Codex draws once it runs: its header box, a reply or tool bullet, the prompt line "› text" (not the dialogs' numbered "› 1. Yes").
SESSION_RE = re.compile(r"^\s*(?:[╭│╰]|•\s|›\s+(?!\d+\.)\S|>_\s|openai codex\b)", re.I)
ERROR_RE = re.compile(r"^\s*(error|fatal)\b[:\s].{3,}", re.I)

NOTES = {
    "update": "Codex is asking whether to update itself; its default answer runs npm install -g. Answer it in the terminal (choose Skip).",
    "trust": "Codex asks to trust this folder (it applies to the repository root)",
}
TRUST_FAILURE = NOTES["trust"] + ". An unattended task cannot answer this dialog: open a Codex session, accept trust, then run the task again."
COMMAND_NOTE = "Codex asks to run a command: answer in the terminal"


def shell_foreground(command) -> bool:
    """Is the pane's foreground process (tmux pane_current_command) a login shell? An empty or unknown command is not."""
    return isinstance(command, str) and command.strip().lstrip("-").lower() in SHELLS


def _tail(text: str) -> list[str]:
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-TAIL_LINES:]


def _codex_drawn(line: str) -> bool:
    """Is this line Codex's own running screen (composer footer, prompt line, reply, header) rather than part of a dialog?"""
    return bool(COMPOSER_RE.search(line) or FOOTER_RE.match(line) or SESSION_RE.match(line))


def _screen_lines(text: str, width: int | None = None, *, redact: bool = False) -> list[str]:
    """Logical lines in the bottom screen, unwrapped using original physical widths (before redaction or prefix removal)."""
    from .. import nodes_relay
    raw = nodes_relay.tail_lines(text or "", TAIL_LINES)
    safe = nodes_relay.screen_lines(text, TAIL_LINES, width=width) if redact else raw
    out, continuing = [], False
    for original, line in zip(raw, safe):
        framed = original.startswith(("│ ", "┃ ")) and original.endswith((" │", " ┃"))
        if framed:
            original, line = original[2:-2], line[2:-2]
        elif original.strip().startswith(("╭", "╰", "┌", "└")):
            # A frame edge is a real line boundary, not a full-width text continuation.
            continuing = False
            continue
        if continuing:
            out[-1] += line
        else:
            out.append(line)
        available = width - 4 if framed and width else width
        continuing = bool(available and len(original.rstrip()) == available)
    return [ln.strip() for ln in out if ln.strip()]


def dialog(text: str, width: int | None = None) -> str | None:
    """A current update or structurally complete folder-access dialog. Quoted questions are never trust evidence."""
    lines = _screen_lines(text, width)
    hit = next((i for i in range(len(lines) - 1, -1, -1) if UPDATE_RE.search(lines[i])), None)
    if hit is not None and not any(_codex_drawn(ln) for ln in lines[hit + 1:]):
        return "update"
    # An approval menu can quote any of the trust text; it must never fail the task as a trust dialog.
    if approval_dialog(text, width):
        return None
    start = next((i for i in range(len(lines) - 1, -1, -1) if lines[i] == "Folder access"), None)
    if start is None:
        return None
    tail = [re.sub(r"^[›>]\s*", "", ln) for ln in lines[start + 1:]]
    # The question immediately follows the heading. Only the repository-root note may precede the numbered choices.
    if not tail or tail[0] != "Trust this folder?":
        return None
    rest = tail[1:]
    if rest and rest[0].startswith("Note: "):
        rest = rest[1:]
    if rest == ["1. Trust and continue", "2. Quit"]:
        return "trust"
    return None


def exit_reason(text: str) -> str:
    """Why codex is gone, for the errored row: its last `Error: ...` line on the screen, else the plain fact. At most 300 characters,
    one line, no control characters."""
    for ln in reversed(_tail(text)):
        if ERROR_RE.match(ln):
            return "codex exited: " + re.sub(r"[\x00-\x1f\x7f]", " ", ln.strip())[:280]
    return "codex is not running in this pane: its login shell is (the launch exited before it started)"


def approval_dialog(text: str, width: int | None = None) -> dict | None:
    """Codex's command permission menu, with exact one-time labels only."""
    lines = _screen_lines(text, width, redact=True)
    if not lines or lines[-1].strip().lower() != "press enter to confirm or esc to cancel":
        return None
    start = next((i for i in range(len(lines) - 1, -1, -1)
                  if lines[i].strip() == "Would you like to run the following command?"), None)
    if start is None:
        return None
    choices, body = [], []
    for line in lines[start + 1:-1]:
        m = re.fullmatch(r"\s*(?:›\s*)?([1-9])\. (.+)", line)
        if m:
            choices.append({"number": m[1], "label": m[2].strip()})
        elif choices:
            choices[-1]["label"] += " " + line.strip()
        else:
            body.append(line.strip())
    yes = [c for c in choices if c["label"] == "Yes, proceed (y)"]
    no = [c for c in choices if c["label"] == "No, and tell Codex what to do differently (esc)"]
    if len(yes) != 1 or len(no) != 1 or len({c["number"] for c in choices}) != len(choices):
        return None
    command_at = next((i for i, line in enumerate(body) if line.startswith("$ ")), None)
    command = "\n".join(body[command_at:])[2:] if command_at is not None else ""
    return {"tool_name": "command", "summary": "command: " + " ".join(body), "command": command[:2000], "choices": choices,
            "allow": yes[0]["number"], "deny": no[0]["number"], "enter": True}


def screen_state(text: str) -> str:
    if dialog(text) or approval_dialog(text):
        return "dialog"
    lines = _tail(text)
    if any("esc to interrupt" in s.lower() for s in lines[-5:]):
        return "working"
    if any(re.match(r"^\s*›(?:\s|$)", s) or COMPOSER_RE.search(s) or FOOTER_RE.match(s) for s in lines[-5:]):
        return "waiting"
    return "unknown"


# ---- a login the server rejected, on screen (#35; from the Codex source at rust-v0.161.0, not observed)
# The TUI draws every error event as ONE history line "■ {message}" in red (tui/src/history_cell/notices.rs:331 new_error_event, used by
# chatwidget/turn_runtime.rs:372 on_error for a failed turn); an agent's reply starts with "•", the user's prompt with "›". The message can be
# wrapped over several pane lines. Codex 0.145 to 0.161 keeps error events out of the rollout, so for an interactive session this line is the
# only trace of a refresh token that expired, was used or revoked.
ERROR_GLYPH = "■"
PANE_LINES = 60                    # scrollback + screen looked at
CONTINUATION_LINES = 3             # a wrapped message continues on at most this many following lines
_STOP_START = tuple("■•›>─│╭╰└┌┐╮╯▌")


def error_messages(text: str) -> list[str]:
    """The messages of the lines the TUI drew as an error, oldest first: the text after "■ " plus up to CONTINUATION_LINES following lines (a wrapped
    message), joined with single spaces. A continuation stops at a blank line or at a line Codex draws itself (a bullet, the prompt, a box)."""
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    out = []
    for i, ln in enumerate(lines):
        body = ln.lstrip()
        if not body.startswith(ERROR_GLYPH + " "):
            continue
        parts = [body[2:].strip()]
        for nxt in lines[i + 1:i + 1 + CONTINUATION_LINES]:
            if not nxt.strip() or nxt.lstrip().startswith(_STOP_START):
                break
            parts.append(nxt.strip())
        out.append(" ".join(parts))
    return out


def rejected_login(text: str) -> str | None:
    """The newest error line on the screen that says the login was rejected (login_problem.codex_pane_auth_failure), else None. Text with no
    "■ " prefix never counts, wherever it stands: an agent's quote, a prompt, a diff."""
    from .. import login_problem
    for msg in reversed(error_messages(text)):
        if login_problem.codex_pane_auth_failure(msg):
            return re.sub(r"[\x00-\x1f\x7f]", " ", msg)[:300]
    return None
