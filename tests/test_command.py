"""POST /api/sessions/{name}/command, /prompt, /resize and the `tui` permission decision (v0.5.7).

lite_client + fake_tmux (tests/conftest.py): no tmux, no workers. The HTTP layer here: validation (400/404), the 409 matrix that keeps
the board from typing into a busy or dialog-showing pane, what reaches tmux (C-u, the typed text, the paste, the resize calls), the
BoardCommand / BoardPrompt events, flags.pending_cmd and its passive confirmation through the statusline hook.
"""
import json
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import hooks, main, permissions, tmux
from app.config import settings

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
NAME = "shop--api--s1"


@pytest.fixture
def clock(monkeypatch):
    """main._cmd_now frozen at a start time that `advance(seconds)` moves (pending_cmd.at and its 20 s rule read it)."""
    class Clock:
        t = datetime.fromisoformat("2026-10-03T10:00:00+00:00")

        def advance(self, seconds):
            self.t += timedelta(seconds=seconds)

    c = Clock()
    monkeypatch.setattr(main, "_cmd_now", lambda: c.t.isoformat(timespec="seconds"))
    return c


@pytest.fixture
def agent_row(lite_client, fake_tmux, clock):
    """A Claude row in tmux session NAME, idle, with stats from the statusline (model Sonnet)."""
    main.db.add_session(tmux_name=NAME, project="shop", repo="api", name="s1", launcher="claude")
    fake_tmux["sessions"][NAME] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "claude",
                                   "path": "/x", "pid": 1, "env": {}}
    main.db.set_state(NAME, "idle", "SessionStart")
    main.db.set_stats(NAME, {"model": "Sonnet 4.5", "model_id": "claude-sonnet-4-5", "effort": "medium", "fast": False})
    return NAME


@pytest.fixture
def shell(lite_client, projects_dir, fake_tmux):
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)
    return lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]


def command(client, name, body, headers=H):
    return client.post(f"/api/sessions/{name}/command", headers=headers, json=body)


def prompt(client, name, body, headers=H):
    return client.post(f"/api/sessions/{name}/prompt", headers=headers, json=body)


def row():
    return main.db.open_row(NAME)


def untouched(fake_tmux):
    """Nothing reached the pane."""
    return not (fake_tmux["keys"] or fake_tmux["texts"] or fake_tmux["pasted"])


# ================================================================ /command: validation

@pytest.mark.parametrize("cmd", ["exit", "/exit", "logout", "rm -rf /", "model; ls", "", "   ", None, 5, ["model"], {"a": 1}, "//model",
                                 "/model\n/exit", "MODELX"])
def test_a_command_outside_the_allowlist_is_400_and_types_nothing(lite_client, agent_row, fake_tmux, cmd):
    r = command(lite_client, agent_row, {"cmd": cmd, "arg": "x"})
    assert r.status_code == 400 and "unknown command" in r.json()["error"] and "/model" in r.json()["error"], (cmd, r.text)
    assert untouched(fake_tmux)


def test_missing_body_is_400(lite_client, agent_row, fake_tmux):
    assert lite_client.post(f"/api/sessions/{agent_row}/command", headers=H).status_code == 400
    assert untouched(fake_tmux)


@pytest.mark.parametrize("cmd", ["model", "/model", "MODEL", " /Model "])
def test_the_command_name_is_normalised(lite_client, agent_row, fake_tmux, cmd):
    r = command(lite_client, agent_row, {"cmd": cmd, "arg": "opus"})
    assert r.status_code == 200 and r.json()["sent"] == "/model opus"


def test_hidden_commands_are_still_allowed(lite_client, agent_row, fake_tmux):
    """weight 0 only hides a command from the default strip: cost and fast are on the allowlist. Bare /fast opens a dialog that swallows
    keys (V8, 2.1.290): the board only sends `/fast on` or `/fast off`."""
    assert command(lite_client, agent_row, {"cmd": "fast", "arg": "off"}).json()["sent"] == "/fast off"
    r = command(lite_client, agent_row, {"cmd": "fast"})
    assert r.status_code == 400 and "needs an argument" in r.json()["error"]
    r = command(lite_client, agent_row, {"cmd": "cost", "wait_ms": 0})
    assert r.status_code == 200 and r.json()["sent"] == "/cost"


@pytest.mark.parametrize("cmd", ["model", "effort", "rename"])
@pytest.mark.parametrize("arg", [None, "", "   "])
def test_a_command_that_takes_an_argument_needs_one(lite_client, agent_row, fake_tmux, cmd, arg):
    """/model and /effort without an argument open an interactive picker: the board never types them bare."""
    r = command(lite_client, agent_row, {"cmd": cmd, "arg": arg})
    assert r.status_code == 400 and "needs an argument" in r.json()["error"]
    assert untouched(fake_tmux)


@pytest.mark.parametrize("cmd", ["compact", "usage", "context", "status", "cost"])
def test_a_command_without_an_argument_refuses_one(lite_client, agent_row, fake_tmux, cmd):
    r = command(lite_client, agent_row, {"cmd": cmd, "arg": "now"})
    assert r.status_code == 400 and "takes no argument" in r.json()["error"]
    assert untouched(fake_tmux)


@pytest.mark.parametrize("arg", ["opus\n/clear", "op\rus", "a\tb", "a\x1b[31m", "a\x00b", "a\x7fb", "a b", "a b", "x" * 201, 5, True,
                                 ["opus"], {"m": "opus"}])
def test_an_argument_is_one_short_line_of_plain_text(lite_client, agent_row, fake_tmux, arg):
    r = command(lite_client, agent_row, {"cmd": "model", "arg": arg})
    assert r.status_code == 400, (arg, r.text)
    assert untouched(fake_tmux)


def test_an_argument_is_stripped_and_may_be_200_characters(lite_client, agent_row, fake_tmux):
    assert command(lite_client, agent_row, {"cmd": "rename", "arg": "  my session \n"}).json()["sent"] == "/rename my session"
    assert command(lite_client, agent_row, {"cmd": "rename", "arg": "x" * 200}).status_code == 200


@pytest.mark.parametrize("wait", [-1, 4001, "5", 1.5, True, [1]])
def test_wait_ms_is_an_integer_from_0_to_4000(lite_client, agent_row, fake_tmux, wait):
    assert command(lite_client, agent_row, {"cmd": "usage", "wait_ms": wait}).status_code == 400
    assert untouched(fake_tmux)


def test_a_bad_name_is_400_and_an_unknown_session_404(lite_client, fake_tmux):
    for bad in ("_ccboard-login", "nope", "a--b"):
        assert command(lite_client, bad, {"cmd": "compact"}).status_code == 400
        assert prompt(lite_client, bad, {"text": "hi"}).status_code == 400
        assert lite_client.post(f"/api/sessions/{bad}/resize", headers=H, json={"cols": 80, "rows": 24}).status_code == 400
    assert command(lite_client, "nope--x--y", {"cmd": "compact"}).status_code == 404
    assert prompt(lite_client, "nope--x--y", {"text": "hi"}).status_code == 404
    assert lite_client.post("/api/sessions/nope--x--y/resize", headers=H, json={"cols": 80, "rows": 24}).status_code == 404
    assert untouched(fake_tmux) and fake_tmux["resized"] == []


def test_the_new_routes_need_identity_and_the_csrf_header(lite_client, agent_row, fake_tmux):
    for path, body in (("command", {"cmd": "compact"}), ("prompt", {"text": "hi"}), ("resize", {"cols": 80, "rows": 24})):
        url = f"/api/sessions/{agent_row}/{path}"
        assert lite_client.post(url, headers={"X-CCBoard": "1"}, json=body).status_code == 403            # no identity
        assert lite_client.post(url, headers={"Tailscale-User-Login": "alice@example.com"}, json=body).status_code == 403   # no X-CCBoard
    assert untouched(fake_tmux) and fake_tmux["resized"] == []


# ================================================================ /command: the 409 matrix

def refused(r, code, retry, state=None, wait_kind=None):
    assert r.status_code == 409, r.text
    d = r.json()
    assert d["error"] == code and d["retry"] == retry, d
    assert "message" in d and "state" in d and "wait_kind" in d, d
    if state is not None:
        assert d["state"] == state
    assert d["wait_kind"] == wait_kind
    return d


def set_row(state, **flags):
    main.db.set_state(NAME, state, "Test")
    if flags:
        main.db.update_flags(NAME, flags)


@pytest.mark.parametrize("state,flags,code,retry,wait_kind", [
    ("working", {}, "working", 5, None),
    ("waiting", {"wait_kind": "permission"}, "permission", 10, "permission"),
    ("waiting", {"wait_kind": "elicitation"}, "elicitation", 10, "elicitation"),
    ("waiting", {}, "waiting", 5, None),                       # waiting with a kind the board cannot name: never typed into
    ("waiting", {"wait_kind": "something-new"}, "waiting", 5, "something-new"),
    ("idle", {"compacting": True}, "compacting", 15, None),
    ("working", {"compacting": True}, "compacting", 15, None),
    ("ended", {}, "ended", None, None),
])
def test_the_board_refuses_to_type_while_the_pane_is_busy(lite_client, agent_row, fake_tmux, state, flags, code, retry, wait_kind):
    set_row(state, **flags)
    refused(command(lite_client, agent_row, {"cmd": "compact"}), code, retry, state=state, wait_kind=wait_kind)
    if (state, code) != ("working", "working"):                  # queue=true is what lets a prompt into a working session
        refused(prompt(lite_client, agent_row, {"text": "go on", "queue": True}), code, retry, state=state, wait_kind=wait_kind)
    assert untouched(fake_tmux)


def test_working_carries_the_state_and_a_retry_hint(lite_client, agent_row, fake_tmux):
    set_row("working")
    assert refused(command(lite_client, agent_row, {"cmd": "model", "arg": "opus"}), "working", 5, state="working")
    assert row()["flags"].get("pending_cmd") is None, "a refused command leaves no pending_cmd"


def test_a_row_with_no_state_yet_is_not_typed_into(lite_client, fake_tmux):
    """Claude may still be booting behind the trust dialog, where Enter would accept it."""
    main.db.add_session(tmux_name=NAME, project="shop", repo="api", name="s1", launcher="claude")
    fake_tmux["sessions"][NAME] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "claude", "path": "/x",
                                   "pid": 1, "env": {}}
    refused(command(lite_client, NAME, {"cmd": "compact"}), "not_ready", 3)
    refused(prompt(lite_client, NAME, {"text": "hi"}), "not_ready", 3)
    assert untouched(fake_tmux)


@pytest.mark.parametrize("state,flags", [("idle", {}), ("done", {}), ("errored", {}), ("waiting", {"wait_kind": "idle"}),
                                         ("idle", {"compacting": False, "wait_kind": None})])
def test_the_board_types_when_the_composer_is_free(lite_client, agent_row, fake_tmux, state, flags):
    set_row(state, **flags)
    assert command(lite_client, agent_row, {"cmd": "compact"}).status_code == 200
    assert prompt(lite_client, agent_row, {"text": "go on"}).status_code == 200


def test_an_undecided_permission_row_blocks_until_it_is_decided(lite_client, agent_row, fake_tmux):
    pid = main.db.perm_add(NAME, "Bash", "Bash: npm test", {"command": "npm test"})
    d = refused(command(lite_client, agent_row, {"cmd": "compact"}), "permission_pending", 10)
    refused(prompt(lite_client, agent_row, {"text": "hi", "queue": True}), "permission_pending", 10)
    assert d["state"] == "idle" and untouched(fake_tmux)
    other = main.db.perm_add("shop--api--other", "Bash", "Bash: ls", {})            # another session's request does not block this one
    main.db.perm_decide(pid, "tui", "alice")
    assert command(lite_client, agent_row, {"cmd": "compact"}).status_code == 200
    assert main.db.perm_get(other)["decision"] is None


def test_a_permission_row_nobody_can_still_answer_does_not_block(lite_client, agent_row, fake_tmux, monkeypatch):
    """A restart leaves its undecided rows behind (their long-poll died with the process): older than approve_timeout + 15 s they
    are stale, not a reason to refuse for ever."""
    monkeypatch.setattr(settings, "approve_timeout", 90.0)
    pid = main.db.perm_add(NAME, "Bash", "Bash: npm test", {})

    def age(seconds):
        at = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
        with main.db.lock:
            main.db.conn.execute("UPDATE permissions SET created_at=? WHERE id=?", (at, pid))
    age(100)                                                     # inside 90 + 15
    refused(command(lite_client, agent_row, {"cmd": "compact"}), "permission_pending", 10)
    age(110)                                                     # past it: no waiter can be left
    assert command(lite_client, agent_row, {"cmd": "compact"}).status_code == 200
    assert main.db.perm_get(pid)["decision"] is None, "the board only ignores the stale row, it does not decide it"


def test_a_shell_row_is_not_an_agent(lite_client, shell, fake_tmux):
    refused(command(lite_client, shell, {"cmd": "compact"}), "not_an_agent", None)
    refused(prompt(lite_client, shell, {"text": "ls"}), "not_an_agent", None)
    assert untouched(fake_tmux)


def test_a_tmux_session_without_an_open_row_is_refused(lite_client, fake_tmux):
    fake_tmux["sessions"]["shop--api--manual"] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%9", "command": "claude",
                                                  "path": "/", "pid": 1, "env": {}}
    d = refused(command(lite_client, "shop--api--manual", {"cmd": "compact"}), "no_open_row", None)
    assert d["state"] is None
    refused(prompt(lite_client, "shop--api--manual", {"text": "hi"}), "no_open_row", None)
    assert untouched(fake_tmux)


def test_a_bad_command_is_400_even_while_the_session_is_busy(lite_client, agent_row, fake_tmux):
    set_row("working")
    assert command(lite_client, agent_row, {"cmd": "exit"}).status_code == 400
    assert prompt(lite_client, agent_row, {"text": ""}).status_code == 400


@pytest.mark.parametrize("confirm", [None, False, 0, "yes", 1])
def test_a_destructive_command_needs_confirm_true(lite_client, agent_row, fake_tmux, confirm):
    body = {"cmd": "clear"} if confirm is None else {"cmd": "clear", "confirm": confirm}
    r = command(lite_client, agent_row, body)
    if confirm is None or confirm is False:
        d = r.json()
        assert r.status_code == 409 and d["error"] == "confirm" and d["cmd"] == "/clear" and d["retry"] is None, r.text
    else:
        assert r.status_code == 400, r.text                     # not a boolean
    assert untouched(fake_tmux) and row()["flags"].get("pending_cmd") is None


def test_a_confirmed_destructive_command_is_typed(lite_client, agent_row, fake_tmux):
    r = command(lite_client, agent_row, {"cmd": "clear", "confirm": True})
    assert r.status_code == 200 and r.json()["sent"] == "/clear"
    assert fake_tmux["texts"] == [(NAME, "/clear", True)]


def test_confirm_does_not_lift_the_busy_guards(lite_client, agent_row, fake_tmux):
    set_row("working")
    refused(command(lite_client, agent_row, {"cmd": "clear", "confirm": True}), "working", 5)
    assert untouched(fake_tmux)


def test_a_busy_session_is_asked_before_the_confirm_question(lite_client, agent_row, fake_tmux):
    """No point asking whether to run /clear that cannot run."""
    set_row("working")
    assert command(lite_client, agent_row, {"cmd": "clear"}).json()["error"] == "working"


# ================================================================ /command: the happy path

def test_command_clears_the_composer_then_types_the_line(lite_client, agent_row, fake_tmux):
    r = command(lite_client, agent_row, {"cmd": "model", "arg": "opus"})
    assert r.status_code == 200
    # no `screen`: /model is not a read command; V8 typed it this way (verified) and saw it save the default for new sessions
    assert r.json() == {"ok": True, "sent": "/model opus", "verified": True, "saves_default": True}
    assert fake_tmux["keys"] == [(NAME, ["C-u"])]
    assert fake_tmux["texts"] == [(NAME, "/model opus", True)]
    sends = [a for a in fake_tmux["run"] if a and a[0] == "send-keys"]
    assert sends[0][-1] == "C-u" and sends[1][-2:] == ("--", "/model opus") and sends[2][-1] == "Enter"      # in this order
    assert fake_tmux["pasted"] == [] and fake_tmux["sent"] == []


def test_command_records_the_board_command_event_and_pending_cmd(lite_client, agent_row, fake_tmux, clock):
    command(lite_client, agent_row, {"cmd": "model", "arg": "opus"})
    ev = next(e for e in main.db.recent_events() if e["event"] == "BoardCommand")
    assert (ev["tmux_name"], ev["kind"], ev["message"]) == (NAME, "model", "/model opus")
    pend = row()["flags"]["pending_cmd"]
    assert pend["cmd"] == "model" and pend["arg"] == "opus" and pend["at"] == "2026-10-03T10:00:00+00:00"
    assert pend["before"]["model"] == "Sonnet 4.5" and pend["before"]["effort"] == "medium"
    assert row()["state"] == "idle" and row()["last_prompt"] is None, "the board does not set state or last_prompt itself"


def test_command_with_no_argument(lite_client, agent_row, fake_tmux):
    assert command(lite_client, agent_row, {"cmd": "compact"}).json() == {"ok": True, "sent": "/compact", "verified": True, "saves_default": False}
    assert fake_tmux["texts"] == [(NAME, "/compact", True)]
    assert row()["flags"]["pending_cmd"]["arg"] is None


def test_command_leaves_copy_mode_before_typing(lite_client, agent_row, fake_tmux):
    """Nothing is typed into a pane in copy-mode: the keys would be read as copy-mode commands."""
    fake_tmux["pane"][NAME] = {"in_mode": True, "scroll_pos": 40}
    assert command(lite_client, agent_row, {"cmd": "compact"}).status_code == 200
    assert NAME in fake_tmux["left_copy"] and fake_tmux["pane"][NAME]["in_mode"] is False
    assert fake_tmux["texts"] == [(NAME, "/compact", True)]


def test_a_read_command_returns_the_captured_screen(lite_client, agent_row, fake_tmux):
    fake_tmux["screen"] = "Current session  42% used\nResets 10pm"
    for cmd in ("usage", "context", "status", "cost"):
        r = command(lite_client, agent_row, {"cmd": cmd, "wait_ms": 0})
        assert r.status_code == 200 and r.json()["screen"] == "Current session  42% used\nResets 10pm", cmd
        assert r.json()["sent"] == "/" + cmd


def test_a_read_command_waits_wait_ms_and_defaults_to_1200(lite_client, agent_row, fake_tmux, monkeypatch):
    naps = []

    class Nap:
        def __getattr__(self, attr):
            return getattr(time, attr)

        @staticmethod
        def sleep(seconds):
            naps.append(seconds)
    monkeypatch.setattr(main, "time", Nap())
    command(lite_client, agent_row, {"cmd": "usage"})
    command(lite_client, agent_row, {"cmd": "usage", "wait_ms": 4000})
    command(lite_client, agent_row, {"cmd": "compact", "wait_ms": 4000})                    # not a read command: no wait, no screen
    assert naps == [1.2, 4.0]


def test_a_failing_tmux_leaves_no_pending_cmd(lite_client, agent_row, fake_tmux, monkeypatch):
    def boom(*a, **k):
        raise tmux.TmuxError("no space for characters")
    monkeypatch.setattr(tmux, "send_text", boom)
    r = command(lite_client, agent_row, {"cmd": "compact"})
    assert r.status_code == 500 and "tmux" in r.json()["error"]
    assert row()["flags"].get("pending_cmd") is None
    assert not [e for e in main.db.recent_events() if e["event"] == "BoardCommand"], "no event for a command that was not typed"


def test_an_earlier_unconfirmed_command_is_settled_by_the_next(lite_client, agent_row, fake_tmux, clock):
    command(lite_client, agent_row, {"cmd": "compact"})
    clock.advance(3)
    command(lite_client, agent_row, {"cmd": "model", "arg": "opus"})
    f = row()["flags"]
    assert f["pending_cmd"]["cmd"] == "model" and f["last_cmd"]["cmd"] == "compact" and f["last_cmd"]["confirmed"] is False


# ================================================================ /command: confirmation through the statusline

def statusline(client, name, **fields):
    """A statusline POST the way bin/ccboard-statusline sends it: hooks.apply stores the stats and runs hooks.STATS_HOOKS."""
    payload = {"model": {"display_name": "Sonnet 4.5", "id": "claude-sonnet-4-5"}, "context_window": {"used_percentage": 12}}
    payload.update(fields)
    r = client.post("/api/hook", headers={"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard-Session": name,
                                          "X-CCBoard-Event": "statusline"}, content=json.dumps(payload))
    assert r.status_code == 200 and r.json().get("stats") is True, r.text


def opus():
    return {"model": {"display_name": "Opus 4.5", "id": "claude-opus-4-5"}}


def test_the_confirmation_is_registered_on_the_stats_hooks():
    assert main._cmd_confirm in hooks.STATS_HOOKS
    assert sum(1 for f in hooks.STATS_HOOKS if f.__qualname__ == "_cmd_confirm") == 1
    main._register_stats_hook()                                  # registering again never doubles the entry
    assert sum(1 for f in hooks.STATS_HOOKS if f.__qualname__ == "_cmd_confirm") == 1


def test_the_statusline_calls_the_stats_hooks(lite_client, agent_row, monkeypatch):
    calls = []
    monkeypatch.setattr(hooks, "STATS_HOOKS", [lambda db, name, stats: calls.append((db, name, stats))])
    r = lite_client.post("/api/hook", headers={"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard-Session": agent_row,
                                               "X-CCBoard-Event": "statusline"},
                         content=json.dumps({"model": {"display_name": "Opus 4.5", "id": "claude-opus-4-5"}}))
    assert r.status_code == 200
    assert len(calls) == 1 and calls[0][0] is main.db and calls[0][1] == agent_row and calls[0][2]["model"] == "Opus 4.5"


def test_a_failing_stats_hook_never_breaks_the_statusline(lite_client, agent_row, monkeypatch):
    def boom(db, name, stats):
        raise RuntimeError("hook bug")
    monkeypatch.setattr(hooks, "STATS_HOOKS", [boom, main._cmd_confirm])
    command(lite_client, agent_row, {"cmd": "compact"})
    statusline(lite_client, agent_row)
    assert row()["flags"]["last_cmd"]["confirmed"] is True, "the hook after the failing one still ran"


def test_a_model_switch_is_confirmed_by_the_statusline_that_shows_it(lite_client, agent_row, fake_tmux, clock):
    command(lite_client, agent_row, {"cmd": "model", "arg": "opus"})
    clock.advance(1)
    statusline(lite_client, agent_row)                           # the old model is still on screen: nothing to say yet
    f = row()["flags"]
    assert "pending_cmd" in f and "last_cmd" not in f
    clock.advance(2)
    statusline(lite_client, agent_row, **opus())
    f = row()["flags"]
    assert "pending_cmd" not in f
    assert f["last_cmd"] == {"cmd": "model", "arg": "opus", "at": "2026-10-03T10:00:00+00:00", "confirmed": True}
    assert row()["stats"]["model"] == "Opus 4.5"


def test_a_model_switch_to_what_is_already_running_matches_by_name(lite_client, agent_row, fake_tmux, clock):
    command(lite_client, agent_row, {"cmd": "model", "arg": "SONNET"})
    statusline(lite_client, agent_row)
    assert row()["flags"]["last_cmd"]["confirmed"] is True


def test_a_model_switch_to_another_name_is_confirmed_by_the_change(lite_client, agent_row, fake_tmux, clock):
    command(lite_client, agent_row, {"cmd": "model", "arg": "default"})                 # no 'default' in the model name: the change counts
    statusline(lite_client, agent_row)
    assert "pending_cmd" in row()["flags"]
    statusline(lite_client, agent_row, model={"display_name": "Opus 4.5", "id": "claude-opus-4-5"})
    assert row()["flags"]["last_cmd"]["confirmed"] is True


def test_a_model_switch_that_never_shows_is_unconfirmed_after_20_seconds(lite_client, agent_row, fake_tmux, clock):
    command(lite_client, agent_row, {"cmd": "model", "arg": "opus"})
    clock.advance(20)
    statusline(lite_client, agent_row)                           # exactly 20 s: still inside the window
    assert "pending_cmd" in row()["flags"]
    clock.advance(1)
    statusline(lite_client, agent_row)
    f = row()["flags"]
    assert "pending_cmd" not in f and f["last_cmd"]["confirmed"] is False and f["last_cmd"]["cmd"] == "model"


def test_a_late_matching_statusline_is_not_a_confirmation(lite_client, agent_row, fake_tmux, clock):
    """After the 20 s window anything could have changed the model (a person at the terminal, a hook): the answer is 'unconfirmed'."""
    command(lite_client, agent_row, {"cmd": "model", "arg": "opus"})
    clock.advance(25)
    statusline(lite_client, agent_row, **opus())
    assert row()["flags"]["last_cmd"]["confirmed"] is False


def test_effort_is_confirmed_by_the_statusline_effort_level(lite_client, agent_row, fake_tmux, clock):
    command(lite_client, agent_row, {"cmd": "effort", "arg": "high"})                  # the fixture's baseline is medium
    statusline(lite_client, agent_row, effort={"level": "medium"})
    assert "pending_cmd" in row()["flags"] and "last_cmd" not in row()["flags"]
    statusline(lite_client, agent_row, effort={"level": "high"})
    assert row()["flags"]["last_cmd"] == {"cmd": "effort", "arg": "high", "at": "2026-10-03T10:00:00+00:00", "confirmed": True}
    assert "pending_cmd" not in row()["flags"]


def test_fast_is_confirmed_by_the_statusline_fast_mode(lite_client, agent_row, fake_tmux, clock):
    command(lite_client, agent_row, {"cmd": "fast", "arg": "on"})                      # baseline: off
    statusline(lite_client, agent_row, fast_mode=False)
    assert "pending_cmd" in row()["flags"]
    statusline(lite_client, agent_row, fast_mode=True)
    assert row()["flags"]["last_cmd"]["cmd"] == "fast" and row()["flags"]["last_cmd"]["confirmed"] is True


def test_other_commands_are_confirmed_by_the_next_statusline(lite_client, agent_row, fake_tmux, clock):
    command(lite_client, agent_row, {"cmd": "compact"})
    assert "pending_cmd" in row()["flags"]
    statusline(lite_client, agent_row)
    assert row()["flags"]["last_cmd"]["confirmed"] is True and "pending_cmd" not in row()["flags"]


def test_a_statusline_with_nothing_pending_writes_no_flags(lite_client, agent_row, fake_tmux):
    statusline(lite_client, agent_row)
    assert "last_cmd" not in row()["flags"] and "pending_cmd" not in row()["flags"]


def test_pending_cmd_keeps_the_baseline_for_effort_and_fast(lite_client, agent_row, fake_tmux, clock):
    """The statusline hook runs after set_stats has overwritten the row's stats, so 'changed' is judged against what was on screen
    when the command was typed."""
    command(lite_client, agent_row, {"cmd": "effort", "arg": "high"})
    assert row()["flags"]["pending_cmd"]["before"] == {"model": "Sonnet 4.5", "model_id": "claude-sonnet-4-5", "effort": "medium",
                                                       "fast": False}
    command(lite_client, agent_row, {"cmd": "fast", "arg": "on"})
    assert row()["flags"]["pending_cmd"]["cmd"] == "fast" and row()["flags"]["pending_cmd"]["before"]["fast"] is False


def pend(cmd, arg=None, before=None, at="2026-10-03T10:00:00+00:00"):
    return {"cmd": cmd, "arg": arg, "at": at, "before": before or {}}


NOW = datetime.fromisoformat("2026-10-03T10:00:05+00:00")


@pytest.mark.parametrize("pending,stats,expected", [
    # effort: equals the argument, or differs from the baseline; a statusline from before the change says nothing
    (pend("effort", "high", {"effort": "medium"}), {"effort": "high"}, True),
    (pend("effort", "HIGH", {"effort": "medium"}), {"effort": "high"}, True),
    (pend("effort", "high", {"effort": "medium"}), {"effort": "medium"}, None),
    (pend("effort", "high", {"effort": "high"}), {"effort": "high"}, True),            # already high: the argument matches
    (pend("effort", "auto", {"effort": "high"}), {}, True),                             # effort disappeared: it changed
    (pend("effort", "high", {}), {}, None),                                            # a Claude that reports no effort at all
    # fast: a toggle, an absent key counts as off
    (pend("fast", None, {"fast": False}), {"fast": True}, True),
    (pend("fast", None, {"fast": True}), {"fast": False}, True),
    (pend("fast", None, {"fast": True}), {}, True),
    (pend("fast", None, {"fast": False}), {"fast": False}, None),
    (pend("fast", None, {}), {}, None),
    # model
    (pend("model", "opus", {"model": "Sonnet 4.5", "model_id": "claude-sonnet-4-5"}), {"model": "Opus 4.5", "model_id": "claude-opus-4-5"}, True),
    (pend("model", "opus[1m]", {"model": "Opus 4.5", "model_id": "claude-opus-4-5[1m]"}), {"model": "Opus 4.5", "model_id": "claude-opus-4-5[1m]"}, True),
    (pend("model", "opus", {"model": "Sonnet 4.5", "model_id": "x"}), {"model": "Sonnet 4.5", "model_id": "x"}, None),
    (pend("model", "opus", {}), {"model": "Sonnet 4.5"}, None),                        # no baseline: only the name can confirm
    (pend("model", "opus", {}), {"model": "Opus 4.5"}, True),
    # every other command: the next statusline
    (pend("compact"), {}, True), (pend("clear"), {"model": "x"}, True), (pend("usage"), {}, True), (pend("rename", "x"), {}, True),
    # a pending_cmd with no usable time is unconfirmed, and so is anything past 20 s
    (pend("compact", at=None), {}, False), (pend("compact", at="garbage"), {}, False),
    (pend("compact", at="2026-10-03T09:59:44+00:00"), {}, False),
    (pend("compact", at="2026-10-03T09:59:45+00:00"), {}, True),
])
def test_cmd_outcome_rules(pending, stats, expected):
    assert main._cmd_outcome(pending, stats, NOW) is expected


def test_the_confirmation_never_raises_into_the_hook_path(lite_client, agent_row, monkeypatch):
    main._cmd_confirm(main.db, "nope--x--y", {"model": "x"})                 # no row
    main._cmd_confirm(main.db, agent_row, None)                              # no stats
    main.db.update_flags(agent_row, {"pending_cmd": "garbage"})
    main._cmd_confirm(main.db, agent_row, {"model": "x"})                    # a pending_cmd of the wrong shape
    main.db.update_flags(agent_row, {"pending_cmd": {"cmd": "compact", "at": "2026-10-03T10:00:00+00:00"}})
    monkeypatch.setattr(main.db, "update_flags", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db is down")))
    main._cmd_confirm(main.db, agent_row, {})


# ================================================================ /prompt

def test_prompt_pastes_the_text_and_presses_enter(lite_client, agent_row, fake_tmux):
    r = prompt(lite_client, agent_row, {"text": "fix the failing test\nthen run the suite"})
    assert r.status_code == 200 and r.json() == {"ok": True, "pasted": True, "queued": False}
    assert fake_tmux["pasted"] == [(NAME, "fix the failing test\nthen run the suite", True)]
    assert ["load-buffer", "-b", "ccboard", "-"] == list(next(a for a in fake_tmux["run"] if a[0] == "load-buffer")[:4])
    assert fake_tmux["keys"] == [], "no C-u: the composer is not cleared for a prompt"
    ev = next(e for e in main.db.recent_events() if e["event"] == "BoardPrompt")
    assert ev["tmux_name"] == NAME and ev["message"] == "fix the failing test\nthen run the suite"


def test_prompt_enter_false_leaves_the_text_in_the_composer(lite_client, agent_row, fake_tmux):
    r = prompt(lite_client, agent_row, {"text": "draft", "enter": False})
    assert r.status_code == 200 and fake_tmux["pasted"] == [(NAME, "draft", False)]
    assert not any(a[-1] == "Enter" for a in fake_tmux["run"] if a[0] == "send-keys")


def test_prompt_does_not_touch_last_prompt_or_state(lite_client, agent_row, fake_tmux):
    main.db.set_state(NAME, "idle", "UserPromptSubmit", prompt="the earlier prompt")
    prompt(lite_client, agent_row, {"text": "the next one"})
    assert row()["last_prompt"] == "the earlier prompt" and row()["state"] == "idle"


def test_prompt_event_message_is_the_first_200_characters(lite_client, agent_row, fake_tmux):
    prompt(lite_client, agent_row, {"text": "x" * 500})
    ev = next(e for e in main.db.recent_events() if e["event"] == "BoardPrompt")
    assert ev["message"] == "x" * 200
    assert fake_tmux["pasted"][0][1] == "x" * 500, "the whole text is pasted"


def test_prompt_normalises_crlf(lite_client, agent_row, fake_tmux):
    assert prompt(lite_client, agent_row, {"text": "a\r\nb\rc\td"}).status_code == 200
    assert fake_tmux["pasted"][0][1] == "a\nb\nc\td"


def test_a_working_session_accepts_a_prompt_only_when_queued(lite_client, agent_row, fake_tmux):
    set_row("working")
    d = refused(prompt(lite_client, agent_row, {"text": "also this"}), "working", 5, state="working")
    refused(prompt(lite_client, agent_row, {"text": "also this", "queue": False}), "working", 5)
    assert fake_tmux["pasted"] == []
    r = prompt(lite_client, agent_row, {"text": "also this", "queue": True})
    assert r.status_code == 200 and r.json() == {"ok": True, "pasted": True, "queued": True}
    assert fake_tmux["pasted"] == [(NAME, "also this", True)]
    assert row()["state"] == "working"


def test_queue_on_an_idle_session_is_not_queued(lite_client, agent_row, fake_tmux):
    assert prompt(lite_client, agent_row, {"text": "go", "queue": True}).json()["queued"] is False


@pytest.mark.parametrize("text", ["a\x1bb", "a\x00b", "a\x03b", "a\x7fb", "a\x85b", "\x1b[201~rm -rf /", "tab\ttab\x0bvt"])
def test_prompt_with_control_characters_is_400(lite_client, agent_row, fake_tmux, text):
    r = prompt(lite_client, agent_row, {"text": text})
    assert r.status_code == 400 and "control" in r.json()["error"], (text, r.text)
    assert untouched(fake_tmux)


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": "   \n\t "}, {"text": None}, {"text": 5}, {"text": ["a"]},
                                  {"text": "ok", "enter": "yes"}, {"text": "ok", "queue": 1}, {"text": "ok", "enter": None}])
def test_prompt_bad_bodies_are_400(lite_client, agent_row, fake_tmux, body):
    assert prompt(lite_client, agent_row, body).status_code == 400, body
    assert untouched(fake_tmux)


def test_prompt_is_capped_at_20000_characters(lite_client, agent_row, fake_tmux):
    assert prompt(lite_client, agent_row, {"text": "x" * 20001}).status_code == 400
    assert untouched(fake_tmux)
    assert prompt(lite_client, agent_row, {"text": "x" * 20000}).status_code == 200
    assert len(fake_tmux["pasted"][0][1]) == 20000


def test_prompt_leaves_copy_mode_first(lite_client, agent_row, fake_tmux):
    fake_tmux["pane"][NAME] = {"in_mode": True, "scroll_pos": 12}
    assert prompt(lite_client, agent_row, {"text": "hello"}).status_code == 200
    assert NAME in fake_tmux["left_copy"] and fake_tmux["pane"][NAME]["in_mode"] is False


def test_a_failing_paste_is_a_tmux_error_and_leaves_no_event(lite_client, agent_row, fake_tmux, monkeypatch):
    def boom(*a, **k):
        raise tmux.TmuxError("buffer gone")
    monkeypatch.setattr(tmux, "paste_text", boom)
    assert prompt(lite_client, agent_row, {"text": "hi"}).status_code == 500
    assert not [e for e in main.db.recent_events() if e["event"] == "BoardPrompt"]


# ================================================================ /resize

def resize(client, name, body, headers=H):
    return client.post(f"/api/sessions/{name}/resize", headers=headers, json=body)


def test_resize_sizes_the_window_and_unsets_the_pin(lite_client, shell, fake_tmux):
    r = resize(lite_client, shell, {"cols": 100, "rows": 30})
    assert r.status_code == 200 and r.json() == {"ok": True, "cols": 100, "rows": 30}
    assert fake_tmux["resized"] == [(shell, 100, 30)]
    calls = [a for a in fake_tmux["run"] if a[0] in ("resize-window", "set-window-option")]
    assert calls == [("resize-window", "-t", f"={shell}:", "-x", "100", "-y", "30"),
                     ("set-window-option", "-u", "-t", f"={shell}:", "window-size")]


def test_resize_shows_in_the_session_win(lite_client, shell, fake_tmux):
    fake_tmux["sessions"][shell].update(window_width=220, window_height=50)
    assert lite_client.get(f"/api/sessions/{shell}", headers=H).json()["win"] == [220, 50]
    assert resize(lite_client, shell, {"cols": 90, "rows": 28}).status_code == 200
    assert lite_client.get(f"/api/sessions/{shell}", headers=H).json()["win"] == [90, 28]
    st = lite_client.get("/api/state", headers=H).json()
    s = next(s for p in st["projects"] for r in p["repos"] for s in r["sessions"] if s["tmux"] == shell)
    assert s["win"] == [90, 28]
    assert lite_client.get(f"/api/sessions/{shell}/pane", headers=H).json()["win_cols"] == 90


def test_win_comes_from_the_fake_win_pair_too(lite_client, shell, fake_tmux):
    fake_tmux["sessions"][shell]["win"] = [132, 41]
    assert lite_client.get(f"/api/sessions/{shell}", headers=H).json()["win"] == [132, 41]


@pytest.mark.parametrize("cols,rows", [(40, 10), (400, 200), (40, 200), (400, 10)])
def test_resize_limits_are_inclusive(lite_client, shell, fake_tmux, cols, rows):
    assert resize(lite_client, shell, {"cols": cols, "rows": rows}).status_code == 200
    assert fake_tmux["resized"] == [(shell, cols, rows)]


@pytest.mark.parametrize("body", [{}, {"cols": 80}, {"rows": 24}, {"cols": 39, "rows": 24}, {"cols": 401, "rows": 24},
                                  {"cols": 80, "rows": 9}, {"cols": 80, "rows": 201}, {"cols": 0, "rows": 0}, {"cols": -80, "rows": 24},
                                  {"cols": "80", "rows": 24}, {"cols": 80.5, "rows": 24}, {"cols": 80.0, "rows": 24},
                                  {"cols": True, "rows": 24}, {"cols": 80, "rows": True}, {"cols": None, "rows": None},
                                  {"cols": [80], "rows": 24}, {"cols": 80, "rows": {"n": 24}}, {"cols": 10**30, "rows": 24}])
def test_resize_outside_the_limits_is_400_and_never_reaches_tmux(lite_client, shell, fake_tmux, body):
    r = resize(lite_client, shell, body)
    assert r.status_code == 400 and "error" in r.json(), (body, r.text)
    assert fake_tmux["resized"] == [] and not [a for a in fake_tmux["run"] if a[0] == "resize-window"]


def test_resize_without_a_body_is_400(lite_client, shell):
    assert lite_client.post(f"/api/sessions/{shell}/resize", headers=H).status_code == 400


def test_resize_is_409_while_a_full_client_is_attached(lite_client, shell, fake_tmux):
    fake_tmux["clients"] = [{"session": shell, "flags": {"attached", "focused"}}]
    r = resize(lite_client, shell, {"cols": 80, "rows": 24})
    assert r.status_code == 409
    d = r.json()
    assert d["viewers"] == {"full": 1, "grid": 0, "ro": 0} and d["error"]
    assert fake_tmux["resized"] == []


def test_grid_tiles_read_only_views_and_other_sessions_do_not_block_a_resize(lite_client, shell, fake_tmux):
    fake_tmux["clients"] = [{"session": shell, "flags": {"attached", "ignore-size"}},
                            {"session": shell, "flags": {"attached", "ignore-size", "read-only"}},
                            {"session": shell, "flags": {"attached", "control-mode"}},
                            {"session": "other--x--y", "flags": {"attached"}}]
    assert resize(lite_client, shell, {"cols": 80, "rows": 24}).status_code == 200


def test_resize_counts_every_attached_client_when_list_clients_fails(lite_client, shell, fake_tmux):
    fake_tmux["clients_error"] = True
    fake_tmux["sessions"][shell]["attached"] = 1
    r = resize(lite_client, shell, {"cols": 80, "rows": 24})
    assert r.status_code == 409 and r.json()["viewers"]["full"] == 1 and fake_tmux["resized"] == []
    fake_tmux["sessions"][shell]["attached"] = 0
    assert resize(lite_client, shell, {"cols": 80, "rows": 24}).status_code == 200


def test_resize_works_on_an_agent_row_in_any_state(lite_client, agent_row, fake_tmux):
    set_row("working")
    assert resize(lite_client, agent_row, {"cols": 80, "rows": 24}).status_code == 200


def test_a_tmux_failure_during_a_resize_is_reported(lite_client, shell, fake_tmux, monkeypatch):
    def boom(*a, **k):
        raise tmux.TmuxError("no such window")
    monkeypatch.setattr(tmux, "resize_window", boom)
    r = resize(lite_client, shell, {"cols": 80, "rows": 24})
    assert r.status_code == 500 and "tmux" in r.json()["error"]


def test_tmux_resize_window_is_two_commands_on_the_exact_session_window(monkeypatch):
    calls = []
    monkeypatch.setattr(tmux, "run", lambda *a, **k: calls.append(a))
    tmux.resize_window("shop--api--s1", 100, 30)
    assert calls == [("resize-window", "-t", "=shop--api--s1:", "-x", "100", "-y", "30"),
                     ("set-window-option", "-u", "-t", "=shop--api--s1:", "window-size")]


@pytest.mark.parametrize("cols,rows", [(39, 24), (80, 9), (401, 24), (80, 201), (True, 24), (80, True), (80.0, 24), ("80", 24), (None, 24)])
def test_tmux_resize_window_checks_its_own_arguments(monkeypatch, cols, rows):
    calls = []
    monkeypatch.setattr(tmux, "run", lambda *a, **k: calls.append(a))
    with pytest.raises(ValueError):
        tmux.resize_window("shop--api--s1", cols, rows)
    assert calls == []


# ================================================================ POST /api/permission/{pid}/tui

def _session(lite_client, projects_dir):
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)
    return lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]


def _ask(client, name):
    return client.post("/api/permission", headers={"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard-Session": name},
                       content=json.dumps({"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "npm test"}}))


def test_the_tui_decision_wakes_the_waiter_with_no_behavior(lite_client, projects_dir, fake_tmux, monkeypatch):
    monkeypatch.setattr(settings, "approve_timeout", 20.0)
    monkeypatch.setattr(permissions, "push_request", lambda pid, name, summary: None)
    name = _session(lite_client, projects_dir)
    seen = {}

    def decide_later():
        for _ in range(100):
            time.sleep(0.05)
            pend = lite_client.get("/api/state", headers=H).json()["pending_permissions"]
            if pend:
                seen["id"] = pend[0]["id"]
                seen["first"] = lite_client.post(f"/api/permission/{pend[0]['id']}/tui", headers=H)
                seen["second"] = lite_client.post(f"/api/permission/{pend[0]['id']}/allow", headers=H)
                seen["third"] = lite_client.post(f"/api/permission/{pend[0]['id']}/tui", headers=H)
                return

    t = threading.Thread(target=decide_later)
    t0 = time.monotonic()
    t.start()
    r = _ask(lite_client, name).json()
    t.join()
    assert time.monotonic() - t0 < 10, "the waiter was woken, not timed out"
    assert seen["first"].status_code == 200 and seen["first"].json() == {"ok": True, "id": seen["id"], "decision": "tui"}
    assert seen["second"].status_code == 409 and seen["third"].status_code == 409, "one decision per request"
    assert r["behavior"] is None and r["reason"] == "tui" and r["id"] == seen["id"], r
    p = main.db.perm_get(seen["id"])
    assert p["decision"] == "tui" and p["source"] == "alice@example.com"
    st = lite_client.get("/api/state", headers=H).json()
    assert st["pending_permissions"] == []
    s = st["projects"][0]["repos"][0]["sessions"][0]
    assert s["state"] == "waiting", "the TUI prompt is still up: the row stays waiting until the tool runs"


def test_tui_after_another_decision_is_409_and_unknown_is_404(lite_client, projects_dir, fake_tmux):
    pid = main.db.perm_add("shop--api--s1", "Bash", "Bash: ls", {})
    assert main.db.perm_decide(pid, "allow", "alice")
    assert lite_client.post(f"/api/permission/{pid}/tui", headers=H).status_code == 409
    assert lite_client.post("/api/permission/999999/tui", headers=H).status_code == 404
    bad = lite_client.post(f"/api/permission/{pid}/maybe", headers=H)
    assert bad.status_code == 400 and "tui" in bad.json()["error"]


def test_tui_with_no_waiter_just_records_the_decision(lite_client, fake_tmux):
    """A request nobody long-polls any more (the hook gave up): the decision is recorded, the wake is a no-op."""
    pid = main.db.perm_add("shop--api--s1", "Bash", "Bash: ls", {})
    r = lite_client.post(f"/api/permission/{pid}/tui", headers=H)
    assert r.status_code == 200 and main.db.perm_get(pid)["decision"] == "tui"
    assert lite_client.post(f"/api/permission/{pid}/tui", headers=H).status_code == 409
