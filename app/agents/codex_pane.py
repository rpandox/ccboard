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
TRUST_RE = re.compile(r"trust this folder|do you trust the contents of this directory|continue only if you trust these files", re.I)
COMPOSER_RE = re.compile(r"context left|for shortcuts|esc to interrupt|reply wit", re.I)     # Codex's own footer once it is at its prompt
# Codex 0.161's footer in a narrow pane has none of those words: "GPT-6.1-Sol default · <path> · Reply wit…" (model and mode, the folder, a
# hint, cut at the pane width). Two " · " separators on one line, which no dialog line has.
FOOTER_RE = re.compile(r"^\s*\S+(?:\s\S+)?\s+·\s+\S.*\s·\s+\S")
# What Codex draws once it runs: its header box, a reply or tool bullet, the prompt line "› text" (not the dialogs' numbered "› 1. Yes").
SESSION_RE = re.compile(r"^\s*(?:[╭│╰]|•\s|›\s+(?!\d+\.)\S|>_\s|openai codex\b)", re.I)
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


def _codex_drawn(line: str) -> bool:
    """Is this line Codex's own running screen (composer footer, prompt line, reply, header) rather than part of a dialog?"""
    return bool(COMPOSER_RE.search(line) or FOOTER_RE.match(line) or SESSION_RE.match(line))


def dialog(text: str) -> str | None:
    """'update' or 'trust' while the screen shows that dialog, else None. The update dialog wins when both are on screen."""
    lines = _tail(text)
    for kind, rx in (("update", UPDATE_RE), ("trust", TRUST_RE)):
        hit = next((i for i in range(len(lines) - 1, -1, -1) if rx.search(lines[i])), None)
        if hit is None:
            continue
        if any(_codex_drawn(ln) for ln in lines[hit + 1:]):
            continue                                    # Codex drew its own screen after it: answered
        return kind
    return None


def exit_reason(text: str) -> str:
    """Why codex is gone, for the errored row: its last `Error: ...` line on the screen, else the plain fact. At most 300 characters,
    one line, no control characters."""
    for ln in reversed(_tail(text)):
        if ERROR_RE.match(ln):
            return "codex exited: " + re.sub(r"[\x00-\x1f\x7f]", " ", ln.strip())[:280]
    return "codex is not running in this pane: its login shell is (the launch exited before it started)"
