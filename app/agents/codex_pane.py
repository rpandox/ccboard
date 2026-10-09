"""What a Codex pane shows when the board must not type into it (box checks 86 and 92, codex-cli 0.161.0). Pure text rules, no tmux here.

  update dialog   "Update available 0.161.0 -> 0.162.0" with "1. Update now (runs npm install -g @openai/codex)" as the default: one Enter
                  typed by the board would run npm on the host. Shown at launch on nearly every start once a newer version exists.
  trust dialog    "Trust this folder?" on the first launch in a new repo (and "Continue only if you trust these files"). It swallows typed
                  text and blocks a task's prompt in argv until a person answers.
  login shell     codex exited (one box launch printed "account/read failed during TUI bootstrap ... workspace routing discovery timed out"
                  and went back to the shell): the pane's foreground process is the login shell, and a prompt typed there runs as a command.

The board never answers a dialog and never types a prompt into any of these; main.py refuses the typing (409), shows the row as needing a
person (dialog) or marks it errored with the reason (exit). The strings below are the ones the box findings quote; they are matched on the
last lines of the visible screen only, and a screen that shows Codex's normal composer footer after them is a dialog that was answered.
"""
from __future__ import annotations

import re

SHELLS = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh"})
TAIL_LINES = 40                    # the dialog is the bottom of the screen; older scrollback may hold one that was answered

UPDATE_RE = re.compile(r"update available|update now|skip until next version|npm install -g @openai/codex", re.I)
TRUST_RE = re.compile(r"trust this folder|do you trust the contents of this directory|continue only if you trust these files", re.I)
COMPOSER_RE = re.compile(r"context left|for shortcuts|esc to interrupt", re.I)     # Codex's own footer once it is at its prompt
ERROR_RE = re.compile(r"^\s*(error|fatal)\b[:\s].{3,}", re.I)

NOTES = {
    "update": "Codex is asking whether to update itself; its default answer runs npm install -g. Answer it in the terminal (choose Skip).",
    "trust": "Codex is asking whether to trust this folder. Answer it in the terminal.",
}


def shell_foreground(command) -> bool:
    """Is the pane's foreground process (tmux pane_current_command) a login shell? An empty or unknown command is not."""
    return isinstance(command, str) and command.strip().lstrip("-").lower() in SHELLS


def _tail(text: str) -> list[str]:
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-TAIL_LINES:]


def dialog(text: str) -> str | None:
    """'update' or 'trust' while the screen shows that dialog, else None. The update dialog wins when both are on screen."""
    lines = _tail(text)
    for kind, rx in (("update", UPDATE_RE), ("trust", TRUST_RE)):
        hit = next((i for i in range(len(lines) - 1, -1, -1) if rx.search(lines[i])), None)
        if hit is None:
            continue
        if any(COMPOSER_RE.search(ln) for ln in lines[hit + 1:]):
            continue                                    # the composer was drawn after it: answered
        return kind
    return None


def exit_reason(text: str) -> str:
    """Why codex is gone, for the errored row: its last `Error: ...` line on the screen, else the plain fact. At most 300 characters,
    one line, no control characters."""
    for ln in reversed(_tail(text)):
        if ERROR_RE.match(ln):
            return "codex exited: " + re.sub(r"[\x00-\x1f\x7f]", " ", ln.strip())[:280]
    return "codex is not running in this pane: its login shell is (the launch exited before it started)"
