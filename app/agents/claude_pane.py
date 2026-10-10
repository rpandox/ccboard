"""What a Claude pane shows after a permission dialog was dismissed with Esc (issue 193, Claude Code 2.1.296). Pure text rules, no tmux here.

Claude fires no hook when a dialog is rejected with Esc: the row stays `waiting` (wait_kind permission) for good. The pane does say so:
Claude prints "Interrupted · What should Claude do instead?" under the rejected tool call and goes back to its idle prompt. That line
counts only while it is the CURRENT screen, i.e. nothing that belongs to a dialog, a running turn or a new tool call was drawn after
it; a stale "Interrupted" line higher up in the scrollback, with a newer dialog or tool call below it, proves nothing.
"""
from __future__ import annotations

import re

TAIL_LINES = 40                    # the interrupted line and the prompt under it are the bottom of the screen

INTERRUPTED_RE = re.compile(r"^\W*Interrupted\b\W{0,4}What should Claude do instead", re.I)
# Anything drawn after the interrupted line that means Claude is not at its idle prompt: a permission or other dialog (its question,
# its numbered choices, its cancel hint), a running turn (the spinner's "esc to interrupt") or the next tool call / reply bullet.
AFTER_RE = re.compile(r"do you want to|esc to cancel|esc to interrupt|tab to amend|\(esc\)|^\W*\d\.\s+(?:yes|no)\b|^\s*[⏺●]", re.I)


def interrupted_at_prompt(text: str) -> bool:
    """Does the screen show Claude back at its idle prompt right after an Esc-rejected dialog? True when its last "Interrupted · What
    should Claude do instead?" line has nothing after it that belongs to a dialog, a running turn or a new tool call."""
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()][-TAIL_LINES:]
    hit = next((i for i in range(len(lines) - 1, -1, -1) if INTERRUPTED_RE.search(lines[i].strip())), None)
    if hit is None:
        return False
    return not any(AFTER_RE.search(ln) for ln in lines[hit + 1:])
