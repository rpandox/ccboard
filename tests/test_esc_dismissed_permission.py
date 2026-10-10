"""Issue 193: a Claude session answered "In terminal" whose dialog was dismissed with Esc left `waiting / permission` for good, because
Claude fires no hook for a rejected dialog. The scan now reads the pane of exactly such a row (waiting, wait_kind permission, no request
of its own pending) and, when it shows "Interrupted · What should Claude do instead?" at the idle prompt, moves the row to idle.
lite_client + fake_tmux: no tmux, no workers; the pane is store["screen"]."""
import threading
import time

import pytest

from app import hooks, main, permissions
from app.agents import claude_pane
from app.config import settings

from .test_command import H, NAME, _ask, agent_row, clock  # noqa: F401  (fixtures)

DIALOG = """\
● Bash(printf hello)
╭──────────────────────────────────────────────╮
│ Bash command                                 │
│   printf hello                               │
│ Do you want to proceed?                      │
│ ❯ 1. Yes                                     │
│   2. Yes, and don't ask again for printf     │
│   3. No, and tell Claude what to do differently (esc) │
╰──────────────────────────────────────────────╯
 Esc to cancel · Tab to amend
"""
INTERRUPTED = """\
● Bash(printf hello)
  ⎿  Interrupted · What should Claude do instead?

╭──────────────────────────────────────────────╮
│ >                                            │
╰──────────────────────────────────────────────╯
  ? for shortcuts
"""


def ask_and_answer_in_terminal(client, name):
    """PermissionRequest -> the board's "In terminal" button (POST /tui) -> the hook returns; Claude's own dialog is up, no further hook."""
    seen = {}

    def decide_later():
        for _ in range(100):
            time.sleep(0.05)
            pend = client.get("/api/state", headers=H).json()["pending_permissions"]
            if pend:
                seen["id"] = pend[0]["id"]
                seen["tui"] = client.post(f"/api/permission/{pend[0]['id']}/tui", headers=H)
                return

    t = threading.Thread(target=decide_later)
    t.start()
    out = _ask(client, name).json()
    t.join()
    assert seen["tui"].status_code == 200 and out["reason"] == "tui", (seen, out)
    return seen["id"]


def view(client, name=NAME):
    main._invalidate_scan()
    return client.get(f"/api/sessions/{name}", headers=H).json()


def needs_you(name=NAME):
    return main._merged_sessions()[0][name]["needs_attention"]


@pytest.fixture
def asked(lite_client, agent_row, fake_tmux, monkeypatch):
    monkeypatch.setattr(settings, "approve_timeout", 20.0)
    monkeypatch.setattr(permissions, "push_request", lambda pid, name, summary: None)
    fake_tmux["screen"] = DIALOG
    ask_and_answer_in_terminal(lite_client, agent_row)
    return lite_client


def test_an_esc_dismissed_dialog_leaves_waiting_within_one_scan(asked, fake_tmux):
    s = view(asked)
    assert s["state"] == "waiting" and s["flags"]["wait_kind"] == "permission" and s["pending"] == [], "the dialog is still up"
    assert needs_you()
    fake_tmux["screen"] = INTERRUPTED                          # Esc: no hook, only the pane changes
    s = view(asked)                                            # one scan
    assert s["state"] == "idle" and "wait_kind" not in s["flags"], s
    assert s["last_message"] == "Interrupted in the terminal"
    assert not needs_you(), "the Needs-you count drops"
    assert main.db.last_event(NAME)["event"] == "Interrupt"
    assert view(asked)["state"] == "idle", "and stays there"


def test_a_pane_still_showing_the_dialog_stays_waiting(asked, fake_tmux):
    for screen in (DIALOG, "", "   \n",
                   INTERRUPTED + DIALOG,                       # an older Interrupted line above a NEW dialog
                   INTERRUPTED + "● Bash(ls)\n  ⎿  running\n"):  # an older one above the next tool call
        fake_tmux["screen"] = screen
        s = view(asked)
        assert s["state"] == "waiting" and s["flags"]["wait_kind"] == "permission", (screen, s)
        assert needs_you()


def test_a_still_pending_permission_stays_waiting_even_if_the_pane_says_interrupted(lite_client, agent_row, fake_tmux):
    main.db.set_state(NAME, "waiting", "PermissionRequest", message="permission: Bash: ls", attention=True)
    main.db.update_flags(NAME, {"wait_kind": "permission"})
    pid = main.db.perm_add(NAME, "Bash", "Bash: ls", {})
    fake_tmux["screen"] = INTERRUPTED
    s = view(lite_client)
    assert s["state"] == "waiting" and s["flags"]["wait_kind"] == "permission" and [p["id"] for p in s["pending"]] == [pid]
    main.db.perm_decide(pid, "tui", "alice")                   # answered: now the same pane counts
    assert view(lite_client)["state"] == "idle"


@pytest.mark.parametrize("kind", ["idle", "elicitation"])
def test_only_a_permission_wait_is_touched(lite_client, agent_row, fake_tmux, kind):
    main.db.set_state(NAME, "waiting", "Notification", message="Claude is waiting", attention=True)
    main.db.update_flags(NAME, {"wait_kind": kind})
    fake_tmux["screen"] = INTERRUPTED
    s = view(lite_client)
    assert s["state"] == "waiting" and s["flags"]["wait_kind"] == kind


def test_a_working_or_done_row_is_not_touched(lite_client, agent_row, fake_tmux):
    fake_tmux["screen"] = INTERRUPTED
    for state in ("working", "done", "idle"):
        main.db.set_state(NAME, state, "UserPromptSubmit")
        assert view(lite_client)["state"] == state


def test_the_next_hook_still_moves_a_waiting_row_without_the_pane(asked, fake_tmux):
    """No Interrupted text on the pane (an older Claude): the row waits for its next hook, as before."""
    fake_tmux["screen"] = "> \n"
    assert view(asked)["state"] == "waiting"
    asked.post("/api/hook", headers={"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard-Session": NAME},
               json={"hook_event_name": "UserPromptSubmit", "prompt": "again"})
    assert view(asked)["state"] == "working"


@pytest.mark.parametrize("screen,expected", [
    (INTERRUPTED, True),
    ("  ⎿  Interrupted · What should Claude do instead?\n", True),
    ("Interrupted by user\n", False),
    ("the log says Interrupted · What should Claude do instead? here\n", False),
    (DIALOG, False),
    ("", False),
    (INTERRUPTED + "✻ Thinking… (esc to interrupt)\n", False),
])
def test_interrupted_at_prompt_rules(screen, expected):
    assert claude_pane.interrupted_at_prompt(screen) is expected
