"""Session-only tuning through a TUI's own pickers (v0.5.21: issues #2, #78 and the box checks V8 / V8-Codex, #16 and #28).

Why pickers: Claude Code saves the person's default when `/model <x>` or `/effort <level>` is typed inline, and Codex takes no inline
argument at all (`/model gpt-6-luna` is sent to the model as a prompt). The session-only route is the picker: open it bare, move the
cursor, press `s` ("this session only"; Enter would save the default). Every key comes from the fixed tables here, never from user text.

A plan is a list of steps that `drive()` runs against a tiny io object (send_keys, send_text, capture, sleep; main.py binds it to tmux):

    ("type", "/model")                  C-u (clears a half-typed draft), the text, Enter
    ("keys", ["Down", "Down"])          named keys (tmux.KEY_ALLOW)
    ("text", "s")                       one literal key, no Enter
    ("wait",)                           STEP seconds for the TUI to redraw
    ("expect", regex, depth)            capture the pane; the regex must match (the picker is open), else `depth` Escapes and stop
    ("goto", labels, target, assume, depth)
                                        capture; find the numbered row of each label and the one the cursor marks; move Up/Down to
                                        `target`; capture again and require the mark on it. `assume` = where the cursor is when the
                                        screen shows no mark (None: not knowing means stop). On a mismatch: `depth` Escapes and stop.

What the box proved (and what it did not) is recorded per setting in `PROVEN`: the UI marks the rest "unverified", and the board
reports Confirmed only when it reads the change back (the pane or the statusline), never because the keys were sent.

Import rule (agents/): nothing from main, tmux, db or hooks: main.py passes the io.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

STEP = 0.45                     # s for a picker to draw after a key (the box redrew in well under that)
MAX_MOVES = 12                  # never more cursor moves than this in one list

# ---------------------------------------------------------------- tables (box check V8 / V8-Codex)

CLAUDE_SLIDER = ("low", "medium", "high", "xhigh", "max")      # `/effort` bare: a slider, the marker on the current level, Left/Right
CLAUDE_PICKER_RE = re.compile(r"\bs for this session only")    # its footer: "Enter to confirm · s for this session only · Esc to cancel"
ULTRA_ON_RE = re.compile(r"Ultracode on\b")                     # "Ultracode on (this session only): dynamic workflows on every task. ..."
ULTRA_OFF_RE = re.compile(r"Ultracode off\b")                   # "Ultracode off. Effort stays xhigh."

# The pickers' headings, case-sensitive (a model's answer that says "select the model" must not pass for an open picker)
CODEX_MODEL_RE = re.compile(r"Select Model and Effort")                   # step 1
CODEX_LEVEL_RE = re.compile(r"Select Reasoning Level")                    # step 2 "Select Reasoning Level for <model>"
CODEX_ADVANCED_RE = re.compile(r"Advanced Reasoning")                     # behind "More reasoning..."
CODEX_PERMS_RE = re.compile(r"Update Model Permissions")
CODEX_CHANGED_RE = re.compile(r"Model changed to\s+(\S+)(?:\s+(.+?))?\s+for this session only")
CODEX_LEVEL_LABEL = {"none": "None", "minimal": "Minimal", "low": "Low", "medium": "Medium", "high": "High", "xhigh": "Extra high",
                     "max": "Max", "ultra": "Ultra"}
CODEX_ADVANCED = ("max", "ultra")                               # listed under "More reasoning..." (Max; Ultra on GPT-6.1-Sol)
CODEX_MORE = "More reasoning"
# /permissions presets, in the picker's order. Full Access (danger-full-access, no approvals) is never a target of the Tune: it is a
# bypass-class change and has no gate here (the launcher's acknowledgement is per launch). The board never moves the cursor past it.
CODEX_PERMS = (("ask", "Ask for approval"), ("auto", "Approve for me"))
CODEX_PERMS_ALL = ("Ask for approval", "Approve for me", "Full Access")
CODEX_STATUS_PERMS = {"ask": re.compile(r"\(Ask for approval\)", re.I), "auto": re.compile(r"\(Approve for me\)", re.I)}

# What the box check ran end to end (True) or only in parts (False): shown as "unverified" next to the control.
PROVEN = {("claude", "effort"): False,         # bare /effort: the slider, its marker on the current level and Left/Right were seen (V8, V19), and
           #                                     its footer offers `s`; `s` itself was pressed on the box only in /model. The statusline confirms.
          ("claude", "ultracode"): True,       # /effort ultracode on | off, inline (V8, V19)
          ("codex", "model"): True,            # /model, the cursor on the current model, Up/Down, Enter, `s` (V8-Codex)
          ("codex", "reasoning"): False,       # step 2's starting row was not recorded; More reasoning > Max/Ultra was opened, not picked
          ("codex", "permissions"): False}     # an entry was picked, from a cursor position the probe did not record

_MARKS = "›>❯▶▸➤→*"
_ROW_RE = re.compile(r"^\s*(?P<mark>[" + re.escape(_MARKS) + r"])?\s*(?P<num>\d{1,2})[.)]?\s+(?P<label>\S.*?)\s*$")


class PickerError(Exception):
    """The picker did not look the way the plan expected: the plan stopped (after its Escapes) and nothing was chosen."""


class Refused(Exception):
    """The setting cannot be driven from what the board knows (a current value it cannot place the cursor from, a value not offered)."""


@dataclass
class Plan:
    agent: str
    setting: str
    value: str
    steps: list
    cmd: str                                   # flags.pending_cmd / last_cmd `cmd` (model, effort, reasoning, permissions, ultracode)
    arg: str
    confirm: Callable[[str], bool | None] | None = None   # reads the final screen: True seen, False contradicted, None not shown
    observe: Callable[[str], str | None] | None = None    # what the screen says was applied (for the message)
    verified: bool = False
    notes: list = field(default_factory=list)


# ---------------------------------------------------------------- screen reading

def _norm(s: str) -> str:
    s = re.sub(r"\((?:current|default)\)", "", s or "", flags=re.I)
    return re.sub(r"\s+", " ", s).strip().strip(".…").strip().lower()


def rows(screen: str) -> list[dict]:
    """The numbered rows of a picker on the screen: [{num, label, marked}] (label = the text up to a run of 2+ spaces)."""
    out = []
    for ln in (screen or "").splitlines():
        m = _ROW_RE.match(ln)
        if not m:
            continue
        label = re.split(r"\s{2,}", m.group("label"))[0]
        out.append({"num": int(m.group("num")), "label": label, "marked": bool(m.group("mark"))})
    return out


def _canon(s: str) -> str:
    """A label with case, spaces, '_' and '-' folded: the catalogue's "GPT-6.1 Sol", its slug gpt-6.1-sol and the row "GPT-6.1-Sol" agree."""
    return re.sub(r"[\s_-]+", "-", _norm(s))


def _label_matches(row_label: str, want) -> bool:
    """`want` is a label or a tuple of spellings of one entry (a model's slug and its display name)."""
    a = _norm(row_label)
    for w in (want if isinstance(want, tuple) else (want,)):
        b = _norm(w)
        if a == b or a.startswith(b + " ") or _canon(a) == _canon(b) or (b == _norm(CODEX_MORE) and a.startswith(b)):
            return True
    return False


def _key(label) -> str:
    return label[0] if isinstance(label, tuple) else label


def locate(screen: str, labels: list) -> tuple[dict, int | None]:
    """({label: row number}, the row number the cursor marks or None) for the LAST block of numbered rows that carries the labels. A label
    may be a tuple of spellings; it is keyed by its first one."""
    found: dict[str, int] = {}
    marked = None
    for r in reversed(rows(screen)):              # bottom up: a picker draws at the bottom; older text above may repeat a label
        for want in labels:
            k = _key(want)
            if k not in found and _label_matches(r["label"], want):
                found[k] = r["num"]
                if r["marked"] and marked is None:
                    marked = r["num"]
    return found, marked


# ---------------------------------------------------------------- plans

def claude_effort(value: str, current: str | None) -> Plan:
    """`/effort` bare, Left/Right from the current level to `value`, `s` (this session only). The statusline confirms it."""
    if value not in CLAUDE_SLIDER:
        raise Refused(f"effort must be one of {', '.join(CLAUDE_SLIDER)}")
    cur = (current or "").strip().lower()
    if cur not in CLAUDE_SLIDER:
        raise Refused("the current effort is not known yet (the statusline has not reported it), so the picker cannot be placed; "
                      "try again once the session has answered once")
    d = CLAUDE_SLIDER.index(value) - CLAUDE_SLIDER.index(cur)
    keys = ["Right" if d > 0 else "Left"] * abs(d)
    steps = [("type", "/effort"), ("wait",), ("expect", CLAUDE_PICKER_RE, 1)]
    if keys:
        steps.append(("keys", keys))
        steps.append(("wait",))
    steps.append(("text", "s"))
    return Plan("claude", "effort", value, steps, "effort", value, verified=PROVEN[("claude", "effort")])


def claude_ultracode(value: str) -> Plan:
    """`/effort ultracode on` or `/effort ultracode off`, inline (session only, the level stays). The pane says which one applied."""
    if value not in ("on", "off"):
        raise Refused("ultracode must be on or off")
    want, other = (ULTRA_ON_RE, ULTRA_OFF_RE) if value == "on" else (ULTRA_OFF_RE, ULTRA_ON_RE)

    def confirm(screen: str) -> bool | None:
        tail = _after_last(screen, "/effort ultracode")
        if want.search(tail):
            return True
        return False if other.search(tail) else None
    return Plan("claude", "ultracode", value, [("type", f"/effort ultracode {value}"), ("wait",)], "ultracode", value, confirm=confirm,
                observe=lambda s: ("on" if ULTRA_ON_RE.search(_after_last(s, "/effort ultracode")) else
                                   "off" if ULTRA_OFF_RE.search(_after_last(s, "/effort ultracode")) else None),
                verified=PROVEN[("claude", "ultracode")])


def _after_last(screen: str, marker: str) -> str:
    i = (screen or "").rfind(marker)
    return screen[i:] if i >= 0 else (screen or "")


def _model_labels(models: list[dict]) -> list[tuple]:
    """Each model's spellings, slug first: the picker row may show the slug or the catalogue's display name."""
    return [tuple(dict.fromkeys([str(m["slug"]), str(m.get("name") or m["slug"])])) for m in models]


def _model_index(models: list[dict], slug: str | None) -> int | None:
    s = (slug or "").strip().lower()
    for i, m in enumerate(models):
        if s and s in (str(m["slug"]).lower(), str(m.get("name") or "").lower()):
            return i
    return None


def _codex_changed(screen: str) -> tuple[str, str] | None:
    hits = CODEX_CHANGED_RE.findall(screen or "")
    if not hits:
        return None
    slug, level = hits[-1]
    return slug.strip().lower(), _level_word(level)


def _level_word(text: str) -> str:
    t = _norm(text)
    for k, label in CODEX_LEVEL_LABEL.items():
        if t in (k, label.lower()):
            return k
    return t


def codex_model(value: str, models: list[dict], current: str | None, current_effort: str | None) -> Plan:
    """/model; step 1: from the current model's row to `value`'s, Enter; step 2: the reasoning row the picker offers (left as it is), `s`."""
    return _codex_model_plan(value, None, models, current, current_effort, "model")


def codex_reasoning(value: str, models: list[dict], current: str | None, current_effort: str | None) -> Plan:
    """/model; step 1: Enter on the current model; step 2: from the highlighted level to `value` (through More reasoning... for Max and
    Ultra), `s`."""
    if not current or _model_index(models, current) is None:
        raise Refused("the session's current model is not known yet (or is not in the catalogue), so the /model picker cannot be placed")
    return _codex_model_plan(None, value, models, current, current_effort, "reasoning")


def _codex_model_plan(model: str | None, level: str | None, models: list[dict], current: str | None, current_effort: str | None,
                      setting: str) -> Plan:
    target = model or current
    ti = _model_index(models, target)
    if ti is None:
        raise Refused(f"model must be one of {', '.join(m['slug'] for m in models)}")
    labels = _model_labels(models)
    ci = _model_index(models, current)
    steps: list = [("type", "/model"), ("wait",), ("expect", CODEX_MODEL_RE, 1),
                   ("goto", labels, _key(labels[ti]), _key(labels[ci]) if ci is not None else None, 1),
                   ("keys", ["Enter"]), ("wait",), ("expect", CODEX_LEVEL_RE, 2)]
    want_level = None
    if level is not None:
        levels = [str(x) for x in (models[ti].get("reasoning") or [])]
        if level not in levels:
            raise Refused(f"reasoning for {models[ti]['slug']} must be one of {', '.join(levels)}")
        main_levels = [x for x in levels if x not in CODEX_ADVANCED]
        main_labels = [CODEX_LEVEL_LABEL.get(x, x.capitalize()) for x in main_levels]
        if any(x in CODEX_ADVANCED for x in levels):
            main_labels.append(CODEX_MORE)
        # where the cursor starts in step 2 was not recorded on the box: it is read off the screen (assumed: the current level)
        assume = CODEX_LEVEL_LABEL.get(current_effort or "", None) if (current_effort or "") in main_levels else None
        if level in CODEX_ADVANCED:
            steps += [("goto", main_labels, CODEX_MORE, assume, 2), ("keys", ["Enter"]), ("wait",), ("expect", CODEX_ADVANCED_RE, 3),
                      ("goto", [CODEX_LEVEL_LABEL[x] for x in levels if x in CODEX_ADVANCED], CODEX_LEVEL_LABEL[level], None, 3)]
        else:
            steps.append(("goto", main_labels, CODEX_LEVEL_LABEL.get(level, level.capitalize()), assume, 2))
        want_level = level
    steps += [("text", "s"), ("wait",)]
    want_slug = str(models[ti]["slug"]).lower()

    def confirm(screen: str) -> bool | None:
        got = _codex_changed(_after_last(screen, "/model"))
        if not got:
            return None
        return got[0] == want_slug and (want_level is None or got[1] == want_level)

    def observe(screen: str) -> str | None:
        got = _codex_changed(_after_last(screen, "/model"))
        return " ".join(x for x in got if x) if got else None
    value = model if setting == "model" else level
    return Plan("codex", setting, str(value), steps, setting, str(value), confirm=confirm, observe=observe,
                verified=PROVEN[("codex", setting)])


def codex_permissions(value: str) -> Plan:
    """/permissions; from the row the cursor marks (read off the screen: never assumed, Full Access sits one row away) to the preset,
    Enter; then /status, which prints the permissions inline: "(Approve for me)" confirms it."""
    names = dict(CODEX_PERMS)
    if value not in names:
        raise Refused(f"permissions must be one of {', '.join(names)} (Full Access is not offered here)")
    label = names[value]
    steps = [("type", "/permissions"), ("wait",), ("expect", CODEX_PERMS_RE, 1), ("goto", list(CODEX_PERMS_ALL), label, None, 1),
             ("keys", ["Enter"]), ("wait",), ("type", "/status"), ("wait",)]
    want = CODEX_STATUS_PERMS[value]

    def confirm(screen: str) -> bool | None:
        tail = _after_last(screen, "/status")
        if want.search(tail):
            return True
        return False if any(r.search(tail) for k, r in CODEX_STATUS_PERMS.items() if k != value) else None

    def observe(screen: str) -> str | None:
        tail = _after_last(screen, "/status")
        for k, r in CODEX_STATUS_PERMS.items():
            if r.search(tail):
                return names[k]
        return None
    return Plan("codex", "permissions", value, steps, "permissions", value, confirm=confirm, observe=observe,
                verified=PROVEN[("codex", "permissions")])


# ---------------------------------------------------------------- driving

class IO:
    """What drive() needs: main.py binds these to tmux for one session (tests pass fakes)."""

    def __init__(self, send_keys, send_text, capture, sleep=time.sleep):
        self.send_keys, self.send_text, self.capture, self.sleep = send_keys, send_text, capture, sleep


def _escape(io: IO, depth: int) -> None:
    for _ in range(max(0, depth)):
        io.send_keys(["Escape"])
        io.sleep(0.15)                          # Escape and the next key must not arrive together (ESC + key reads as Alt + key)


def drive(plan: Plan, io: IO) -> str:
    """Run the plan's steps; return the last capture of the pane. Raises PickerError (after backing out) when a picker does not look
    the way the plan expects, so a wrong row is never chosen."""
    screen = ""
    typed = ""
    look = lambda: _after_last(io.capture(), typed) if typed else io.capture()   # noqa: E731  only what the TUI drew after the command
    for step in plan.steps:
        kind = step[0]
        if kind == "type":
            io.send_keys(["C-u"])
            io.send_text(step[1], True)
            typed = step[1]
        elif kind == "keys":
            io.send_keys(list(step[1]))
        elif kind == "text":
            io.send_text(step[1], False)
        elif kind == "wait":
            io.sleep(STEP)
        elif kind == "expect":
            screen = look()
            if not step[1].search(screen or ""):
                _escape(io, step[2])
                raise PickerError("the picker did not open the way the box check showed; nothing was changed")
        elif kind == "goto":
            _, labels, target, assume, depth = step
            screen = look()
            found, marked = locate(screen, labels)
            if target not in found:
                _escape(io, depth)
                raise PickerError(f"{target} is not in the picker on this screen; nothing was changed")
            start = marked if marked is not None else found.get(assume) if assume else None
            if start is None:
                _escape(io, depth)
                raise PickerError("the board could not read where the picker's cursor is; nothing was changed")
            d = found[target] - start
            if abs(d) > MAX_MOVES:
                _escape(io, depth)
                raise PickerError("the picker is longer than the board expects; nothing was changed")
            if d:
                io.send_keys(["Down" if d > 0 else "Up"] * abs(d))
                io.sleep(STEP)
                if marked is not None:            # the screen showed the cursor before: it must show it on the target now
                    screen = look()
                    _, now = locate(screen, labels)
                    if now != found[target]:
                        _escape(io, depth)
                        raise PickerError(f"the cursor did not land on {target}; nothing was changed")
        else:                                     # pragma: no cover - a plan is built here, never from input
            raise ValueError(f"unknown step {kind!r}")
    io.sleep(STEP)
    return io.capture()


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def session_lock(name: str) -> threading.Lock:
    """One picker drive per session at a time (two would interleave their keys)."""
    with _locks_guard:
        return _locks.setdefault(name, threading.Lock())
