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


RULE = "─" * 40
NEW_UI = f"""\
● Bash(printf hello)
  ⎿  Interrupted · What should Claude do instead?

{RULE}
❯ 
{RULE}
  ? for shortcuts
"""


@pytest.mark.parametrize("screen,expected", [
    (INTERRUPTED, True),
    (NEW_UI, True),
    (NEW_UI + "  my custom statusline 12%\n", True),
    ("  ⎿  Interrupted · What should Claude do instead?\n", False),                       # last line of the screen: no input box under it
    ("Interrupted by user\n", False),
    ("the log says Interrupted · What should Claude do instead? here\n", False),
    (DIALOG, False),
    ("", False),
    (INTERRUPTED + "✻ Thinking… (esc to interrupt)\n", False),
    # program output: the line is followed by more output before the input box, or by a frame with no input line, or by no frame at all
    ("● Bash(cat README)\n  ⎿  Interrupted · What should Claude do instead?\n     more output\n" + RULE + "\n❯ \n" + RULE + "\n", False),
    ("● Bash(cat README)\n  ⎿  Interrupted · What should Claude do instead?\n" + RULE + "\n  ? for shortcuts\n", False),
    ("● Bash(cat README)\n  ⎿  Interrupted · What should Claude do instead?\n❯ \n", False),
])
def test_interrupted_at_prompt_rules(screen, expected):
    assert claude_pane.interrupted_at_prompt(screen) is expected


# ---- the pane alone never clears an attention state (security review of the first fix)

def waiting_row(decision, *, event="PermissionRequest"):
    """The row waits on a permission; its newest permission row has `decision` (None: no permission row at all)."""
    main.db.set_state(NAME, "waiting", event, message="Claude needs your permission", attention=True)
    main.db.update_flags(NAME, {"wait_kind": "permission"})
    if decision is not None:
        pid = main.db.perm_add(NAME, "Bash", "Bash: ls", {})
        main.db.perm_decide(pid, decision, "alice")


@pytest.mark.parametrize("decision", ["allow", "deny", "timeout", "interrupt", None])
def test_without_a_tui_answer_the_pane_never_clears_the_wait(lite_client, agent_row, fake_tmux, decision):
    """Anything that can print the Interrupted line must not hide a real needs-you: only the person's own "In terminal" counts."""
    waiting_row(decision)
    fake_tmux["screen"] = INTERRUPTED
    s = view(lite_client)
    assert s["state"] == "waiting" and s["flags"]["wait_kind"] == "permission", (decision, s)
    assert needs_you()


def test_a_notification_only_wait_is_not_cleared_by_the_pane(lite_client, agent_row, fake_tmux):
    main.db.set_state(NAME, "waiting", "Notification", message="Claude needs your permission", attention=True)
    main.db.update_flags(NAME, {"wait_kind": "permission"})
    fake_tmux["screen"] = INTERRUPTED
    assert view(lite_client)["state"] == "waiting"


def test_a_tui_answer_older_than_the_wait_does_not_count(lite_client, agent_row, fake_tmux):
    waiting_row("tui")                                          # request 1, answered in the terminal
    main.db.add_event(NAME, "UserPromptSubmit", None, "go on", {})
    main.db.set_state(NAME, "working", "UserPromptSubmit")
    main.db.set_state(NAME, "waiting", "Notification", message="Claude needs your permission", attention=True)     # a later wait, no request of its own
    main.db.update_flags(NAME, {"wait_kind": "permission"})
    fake_tmux["screen"] = INTERRUPTED
    assert view(lite_client)["state"] == "waiting"


def test_a_tool_batch_after_the_tui_answer_means_the_dialog_was_answered_and_a_later_wait_is_new(lite_client, agent_row, fake_tmux):
    waiting_row("tui", event="Notification")
    main.db.update_flags(NAME, {"last_tool_at": "2999-01-01T00:00:00+00:00"})
    fake_tmux["screen"] = INTERRUPTED
    assert view(lite_client)["state"] == "waiting"


def test_a_wait_set_before_the_tui_answered_request_does_not_count(lite_client, agent_row, fake_tmux):
    waiting_row("tui")
    main.db.conn.execute("UPDATE sessions SET state_at='2999-01-01T00:00:00+00:00' WHERE tmux_name=?", (NAME,))     # waiting is NEWER than the answered request
    fake_tmux["screen"] = INTERRUPTED
    assert view(lite_client)["state"] == "waiting"


def test_a_tui_answer_with_the_notification_after_the_dialog_drew_counts(lite_client, agent_row, fake_tmux):
    """The live order: PermissionRequest, the person answers "In terminal", Claude draws its dialog and sends Notification(permission_prompt)."""
    waiting_row("tui")
    main.db.set_state(NAME, "waiting", "Notification", message="Claude needs your permission", attention=True)
    main.db.update_flags(NAME, {"wait_kind": "permission"})
    fake_tmux["screen"] = INTERRUPTED
    assert view(lite_client)["state"] == "idle"


def test_program_output_that_looks_interrupted_does_not_clear_a_tui_answered_wait(asked, fake_tmux):
    fake_tmux["screen"] = ("● Bash(cat README.md)\n  ⎿  Interrupted · What should Claude do instead?\n")       # a tool's output, no input box under it
    s = view(asked)
    assert s["state"] == "waiting" and s["flags"]["wait_kind"] == "permission"


def test_a_permission_request_landing_between_the_pane_read_and_the_write_stays_waiting(asked, fake_tmux, monkeypatch):
    """TOCTOU: the capture is slow; a new PermissionRequest arrives meanwhile. The write is one conditional UPDATE, so it is a no-op."""
    real = main.tmux.capture
    landed = {}

    def capture_then_request(name, lines=200, **kw):
        out = real(name, lines=lines, **kw)
        if not landed:
            main.db.set_state(NAME, "waiting", "PermissionRequest", message="permission: Bash: rm", attention=True)
            main.db.update_flags(NAME, {"wait_kind": "permission"})
            landed["id"] = main.db.perm_add(NAME, "Bash", "Bash: rm", {})
        return out

    fake_tmux["screen"] = INTERRUPTED
    monkeypatch.setattr(main.tmux, "capture", capture_then_request)
    s = view(asked)
    assert landed, "the capture ran"
    assert s["state"] == "waiting" and s["flags"]["wait_kind"] == "permission", s
    assert [p["id"] for p in s["pending"]] == [landed["id"]], "the new request is still pending"
    assert main.db.last_event(NAME)["event"] != "Interrupt"


def test_the_release_is_one_conditional_write(asked, fake_tmux):
    """db.release_esc_dismissed refuses a stale state_at and writes nothing, then does the move once."""
    row = main.db.open_row(NAME)
    assert main.db.release_esc_dismissed(NAME, "1999-01-01T00:00:00+00:00", "x") is False
    assert main.db.open_row(NAME)["state"] == "waiting"
    assert main.db.release_esc_dismissed(NAME, row["state_at"], "x") is True
    assert main.db.release_esc_dismissed(NAME, row["state_at"], "x") is False, "already moved"
    assert main.db.open_row(NAME)["state"] == "idle"


def test_a_notification_older_than_the_tui_answered_request_does_not_count(lite_client, agent_row, fake_tmux):
    waiting_row("tui", event="Notification")
    main.db.conn.execute("UPDATE sessions SET state_at='1999-01-01T00:00:00+00:00' WHERE tmux_name=?", (NAME,))     # the wait is older than the request
    fake_tmux["screen"] = INTERRUPTED
    assert view(lite_client)["state"] == "waiting"


def test_any_undecided_permission_of_the_session_blocks_the_release(asked, fake_tmux):
    """The write itself refuses while a request of the session is undecided, whatever the earlier checks saw."""
    row = main.db.open_row(NAME)
    main.db.perm_add(NAME, "Bash", "Bash: stale", {})
    assert main.db.release_esc_dismissed(NAME, row["state_at"], "x") is False
    assert main.db.open_row(NAME)["state"] == "waiting"
