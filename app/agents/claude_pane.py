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


BOX_CHARS = set("─━│┃┌┐└┘├┤╭╮╯╰ ")
RULE_RE = re.compile(r"[─━]{8,}")
PROMPT_RE = re.compile(r"^[│┃]?\s*[>❯](?:\s|$)")       # Claude's input line: "> " or "❯ ", inside the old box's side bar or between two rules


def _box_only(line: str) -> bool:
    return bool(line.strip()) and set(line) <= BOX_CHARS


def interrupted_at_prompt(text: str) -> bool:
    """Does the screen show Claude back at its idle prompt right after an Esc-rejected dialog? True when the last "Interrupted · What
    should Claude do instead?" line is followed ONLY by the frame of Claude's input box (rule or box-top lines), then the input line
    itself ("> " / "❯ ", possibly inside the box's side bars), and nothing after that belongs to a dialog, a running turn or a new tool
    call. Text between the line and the input box (more tool output) means the line was program output, not Claude's.

    This is evidence about the screen only, and a program can print the same text; the caller (main._esc_dismissed_permissions) acts on
    it only for a row whose permission the person answered "In terminal" (db.release_esc_dismissed)."""
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()][-TAIL_LINES:]
    hit = next((i for i in range(len(lines) - 1, -1, -1) if INTERRUPTED_RE.search(lines[i].strip())), None)
    if hit is None:
        return False
    j, rule = hit + 1, False
    while j < len(lines) and _box_only(lines[j]):
        rule = rule or bool(RULE_RE.search(lines[j]))
        j += 1
    if not rule or j >= len(lines) or not PROMPT_RE.match(lines[j].strip()):
        return False
    return not any(AFTER_RE.search(ln) for ln in lines[j + 1:])


APPROVAL_HEADINGS = {"Bash command": "Bash", "Edit file": "Edit", "Write file": "Write", "Create file": "Write", "Read file": "Read"}
CHOICE_RE = re.compile(r"^\s*(?:❯\s*)?([1-9])\. (.+)$")


def foreground(command: str | None) -> bool:
    """Claude's native executable reports its version as the process name."""
    return command == "claude" or bool(re.fullmatch(r"\d+\.\d+\.\d+", command or ""))


def screen_state(text: str) -> str:
    """Screen evidence only; unknown output is never evidence that typing is safe."""
    if approval_dialog(text):
        return "dialog"
    lines = [s.strip() for s in text.splitlines() if s.strip()][-TAIL_LINES:]
    if lines and "esc to cancel" in lines[-1].lower():
        return "dialog"
    if any("esc to interrupt" in s.lower() for s in lines[-5:]):
        return "working"
    if any(PROMPT_RE.match(s) for s in lines[-5:]) or interrupted_at_prompt(text):
        return "waiting"
    return "unknown"


def approval_dialog(text: str) -> dict | None:
    """Only Claude's tool approval menu, ending at its cancel hint. Exact one-time labels; never infer a choice from its position."""
    # Keep frame boundaries until the heading/body association is established. A heading in
    # scrollback must never lend its tool type to a later, unsupported dialog.
    raw = [s for s in (text or "").splitlines() if s.strip()]
    lines = [s.strip().strip("│┃").strip() for s in raw]
    if not lines or not re.fullmatch(r"Esc to cancel(?:\s*[·•].*)?", lines[-1], re.I):
        return None
    question = next((i for i in range(len(lines) - 1, -1, -1) if re.fullmatch(r"Do you want to .+\?", lines[i])), None)
    if question is None:
        return None
    border = next((i for i in range(question - 1, -1, -1) if _box_only(raw[i]) and RULE_RE.search(raw[i])), None)
    if border is None or any(c in raw[border] for c in "╰╯└┘"):
        return None
    start = border + 1
    if start >= question or lines[start] not in APPROVAL_HEADINGS:
        return None
    def indent(line):
        content = line.lstrip().lstrip("│┃") if line.lstrip().startswith(("│", "┃")) else line
        return len(content) - len(content.lstrip())
    for i in range(start + 1, question):
        if not lines[i]:
            continue
        if (_box_only(raw[i]) or lines[i] in APPROVAL_HEADINGS or re.search(r"Do you want to", lines[i], re.I)
                or re.match(r"(?:❯\s*)?\d+[.)]\s*", lines[i]) or indent(raw[i]) <= indent(raw[start])):
            return None
    tail = [lines[i] for i in range(question + 1, len(lines) - 1) if lines[i] and not _box_only(raw[i])]
    choices = []
    for line in tail:
        m = CHOICE_RE.fullmatch(line)
        if m:
            choices.append({"number": m[1], "label": m[2]})
        elif choices:
            choices[-1]["label"] += " " + line       # a wrapped option is one label, never a bare Yes followed by a hidden qualifier
        else:
            return None
    if len(choices) < 2 or len({c["number"] for c in choices}) != len(choices):
        return None
    yes = [c for c in choices if c["label"] == "Yes"]
    no = [c for c in choices if c["label"] in ("No", "No, and tell Claude what to do differently", "No, and tell Claude what to do differently (esc)")]
    if len(yes) != 1 or len(no) != 1:
        return None
    body = " ".join(" ".join(lines[start + 1:question]).split())
    # The public permission boundary scrubs secrets before applying its 300-character cap.
    return {"tool_name": APPROVAL_HEADINGS[lines[start]], "summary": lines[start] + (": " + body if body else ""), "choices": choices,
            "allow": yes[0]["number"], "deny": no[0]["number"]}
