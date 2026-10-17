"""Expired hook requests remain answerable through the current, rechecked Claude dialog. All panes are fakes."""
from pathlib import Path

import pytest

from app import main, nodes_work
from app.agents.claude_pane import approval_dialog
from .test_command import H, NAME, agent_row as command_agent_row, clock  # noqa: F401
from .test_nodes_steer import hub, peer, sess, world, _fresh_scan  # noqa: F401

@pytest.fixture
def agent_row(command_agent_row):
    main.db.set_state(NAME, "waiting", "PermissionRequest", attention=True)
    main.db.update_flags(NAME, {"wait_kind": "permission"})
    return command_agent_row


DIALOG = (Path(__file__).parent / "fixtures/agents/claude/approval_dialog.txt").read_text()


@pytest.fixture(autouse=True)
def online_for_hub_tests(request, monkeypatch):
    if "world" in request.fixturenames:
        from .test_nodes_remote_tasks import FakeHub
        monkeypatch.setattr(main, "_hub", lambda: FakeHub())


def pending(client):
    return client.get("/api/state", headers=H).json()["pending_permissions"]


@pytest.mark.parametrize("decision,number", [("allow", "2"), ("deny", "4")])
def test_expired_hook_is_answerable_locally(lite_client, agent_row, fake_tmux, decision, number):
    main.db.set_state(NAME, "waiting", "PermissionRequest", attention=True)
    main.db.update_flags(NAME, {"wait_kind": "permission"})
    pid = main.db.perm_add(NAME, "Bash", "Bash: git commit", {})
    main.db.perm_expire(pid)
    fake_tmux["screen"] = DIALOG
    p, = pending(lite_client)
    assert p["kind"] == "terminal_prompt" and p["summary"] == "Bash command: git add src && git commit -m fix"
    assert [c["number"] for c in p["choices"]] == ["1", "2", "3", "4"]
    assert pending(lite_client)[0]["id"] == p["id"]
    assert lite_client.get(f"/api/sessions/{NAME}", headers=H).json()["pending"] == [p]
    assert lite_client.get("/api/node/state", headers=H).json()["needs_you"]["permissions"] == 1
    assert nodes_work.asking_since(main.db, {"tmux_name": NAME}) is not None
    r = lite_client.post(f"/api/permission/{p['id']}/{decision}", headers=H)
    assert r.status_code == 200, r.text
    assert fake_tmux["texts"] == [(NAME, number, False)]
    assert fake_tmux["keys"] == [] and fake_tmux["pasted"] == []
    assert lite_client.post(f"/api/permission/{p['id']}/{decision}", headers=H).status_code == 409
    assert pending(lite_client) == []


@pytest.mark.parametrize("changed", [DIALOG.replace("git add src", "git add other"), DIALOG.replace("2. Yes ", "5. Yes "), "idle", ""])
def test_changed_pane_sends_nothing(lite_client, agent_row, fake_tmux, changed):
    fake_tmux["screen"] = DIALOG
    p, = pending(lite_client)
    fake_tmux["screen"] = changed
    r = lite_client.post(f"/api/permission/{p['id']}/allow", headers=H)
    assert r.status_code == 409 and "changed" in r.text and "nothing was sent" in r.text
    assert not fake_tmux["texts"]


@pytest.mark.parametrize("body", [{"text": "1"}, {"text": "3"}, {"keys": ["Enter"]}, {"text": "y"}])
def test_free_keys_cannot_choose_a_terminal_permission(lite_client, agent_row, fake_tmux, body):
    fake_tmux["screen"] = DIALOG
    r = lite_client.post(f"/api/sessions/{NAME}/keys", headers=H, json=body)
    assert r.status_code == 409 and r.json()["error"] == "asking"
    assert not fake_tmux["texts"] and not fake_tmux["keys"]


def test_live_hook_wins_and_terminal_only_accepts_allow_or_deny(lite_client, agent_row, fake_tmux):
    fake_tmux["screen"] = DIALOG
    pid = main.db.perm_add(NAME, "Bash", "Bash: command", {})
    assert [p["id"] for p in pending(lite_client)] == [pid]
    main.db.perm_expire(pid)
    p, = pending(lite_client)
    for decision in ("tui", "always", "1", "3"):
        assert lite_client.post(f"/api/permission/{p['id']}/{decision}", headers=H).status_code == 400
    assert not fake_tmux["texts"]


@pytest.mark.parametrize("text", [DIALOG.replace("2. Yes ", "2. Yes, always "), DIALOG.replace("Bash command", "Plan approval"),
                                 DIALOG + "\n❯ \n? for shortcuts", DIALOG.replace("4. No,", "2. No,"),
                                 DIALOG.replace("2. Yes ", "2. Yes, switch to auto mode ")])
def test_unrecognized_or_persistent_choices_are_not_answerable(text):
    assert approval_dialog(text) is None


@pytest.mark.parametrize("decision,number", [("allow", "2"), ("deny", "4")])
def test_hub_answers_the_same_dialog(world, decision, number):
    w = world
    name = sess(w, state="waiting")
    w.two.tmux_b["screen"] = DIALOG
    r = hub(w, "GET", "/permissions")
    assert r.status_code == 200, r.text
    p, = r.json()["data"]["permissions"]
    assert p["kind"] == "terminal_prompt" and len(p["choices"]) == 4
    assert p["summary"] == "Bash command: git add src && git commit -m fix"
    r = hub(w, "POST", f"/permissions/{p['id']}/{decision}")
    assert r.status_code == 200, r.text
    assert w.two.tmux_b["texts"][-1] == (name, number, False)
    assert hub(w, "POST", f"/permissions/{p['id']}/{decision}").status_code == 409


def test_hub_changed_pane_and_keys_are_refused(world):
    w = world
    name = sess(w, state="waiting")
    w.two.tmux_b["screen"] = DIALOG
    p, = hub(w, "GET", "/permissions").json()["data"]["permissions"]
    before = list(w.two.tmux_b["texts"])
    for key in ("1", "2", "3", "4", "Enter"):
        r = hub(w, "POST", f"/sessions/{name}/keys", {"key": key})
        assert r.status_code == 409, r.text
    w.two.tmux_b["screen"] = DIALOG.replace("git add src", "git add other")
    r = hub(w, "POST", f"/permissions/{p['id']}/allow")
    assert r.status_code == 409 and r.json()["reason"] == "changed" and "nothing was sent" in r.text
    assert w.two.tmux_b["texts"] == before


def test_hub_still_requires_permissions_scope_and_an_acting_person(world):
    w = world
    sess(w, state="waiting")
    w.two.tmux_b["screen"] = DIALOG
    p, = hub(w, "GET", "/permissions").json()["data"]["permissions"]
    assert peer(w, "POST", f"/api/node/permissions/{p['id']}/allow", acting=None).status_code == 403
    w.reg = w.pair_up(["read", "sessions"])
    w.h = w.reg["handle"]
    assert hub(w, "POST", f"/permissions/{p['id']}/allow").status_code == 409


def test_choice_labels_are_scrubbed_and_wrapped_qualifiers_cannot_be_one_time(lite_client, agent_row, fake_tmux):
    secret = "sk-ant-" + "api03-" + "Ab1" * 20
    fake_tmux["screen"] = DIALOG.replace("don't ask again for git commands", "don't ask again for TOKEN=" + secret)
    p, = pending(lite_client)
    assert secret not in str(p) and "[redacted]" in str(p["choices"])
    assert approval_dialog(DIALOG.replace("2. Yes ", "2. Yes\n│ and don't ask again ")) is None
    wrapped = DIALOG.replace("don't ask again for git commands", "don't ask again\n│ for git commands")
    assert approval_dialog(wrapped)["allow"] == "2"


def test_pane_replacement_and_a_new_hook_invalidate_an_answer(lite_client, agent_row, fake_tmux):
    fake_tmux["screen"] = DIALOG
    p, = pending(lite_client)
    fake_tmux["sessions"][NAME]["pane_id"] = "%2"
    assert lite_client.post(f"/api/permission/{p['id']}/allow", headers=H).status_code == 409
    p, = pending(lite_client)
    main.db.perm_add(NAME, "Bash", "Bash: new request", {})
    assert lite_client.post(f"/api/permission/{p['id']}/deny", headers=H).status_code == 409
    assert not fake_tmux["texts"]


def test_unreadable_pane_fails_closed(lite_client, agent_row, fake_tmux, monkeypatch):
    from app import tmux
    fake_tmux["screen"] = DIALOG
    p, = pending(lite_client)

    def down(*args, **kwargs):
        raise tmux.TmuxDown("no server")

    monkeypatch.setattr(tmux, "list_sessions", down)
    assert main._pending_permissions() == []
    r = lite_client.post(f"/api/permission/{p['id']}/allow", headers=H)
    assert r.status_code == 409 and "nothing was sent" in r.text
    assert not fake_tmux["texts"]


@pytest.mark.parametrize("decision", ["allow", "deny", "tui"])
@pytest.mark.parametrize("terminal", [False, True])
def test_hook_token_cannot_answer_any_permission(lite_client, agent_row, fake_tmux, decision, terminal):
    from app import hooks, permissions
    if terminal:
        fake_tmux["screen"] = DIALOG
        p, = pending(lite_client)
        pid = p["id"]
    else:
        pid = main.db.perm_add(NAME, "Bash", "Bash: command", {})
    r = lite_client.post(f"/api/permission/{pid}/{decision}", headers={"X-CCBoard-Token": hooks.ensure_token()})
    assert r.status_code == 403, r.text
    assert not fake_tmux["texts"] and not fake_tmux["keys"] and not fake_tmux["pasted"]
    if terminal:
        assert permissions.terminal_get(main.db, pid) == p
        assert pending(lite_client) == [p]
    else:
        assert main.db.perm_get(pid)["decision"] is None
    # The same record is still answerable by a person (terminal menus have no tui decision).
    person_decision = "deny" if terminal and decision == "tui" else decision
    assert lite_client.post(f"/api/permission/{pid}/{person_decision}", headers=H).status_code == 200


@pytest.mark.parametrize("screen", [
    "Bash command\n" + DIALOG.replace("Bash command", "WebFetch"),
    "Bash command\n" + DIALOG.replace("Bash command", "MCP tool"),
    "Bash command\n" + DIALOG.replace("Bash command", "Enter worktree"),
    DIALOG.replace("│ Do you want", "╰────────────────────────╯\n│ Do you want"),
    DIALOG.replace("│ Do you want", "│ Do you want to fetch?\n│ Do you want"),
    DIALOG.replace("│ Do you want", "│ 1. Yes\n│ Do you want"),
    DIALOG.replace("│ Do you want", "│ Edit file\n│ Do you want"),
])
def test_heading_must_belong_to_this_dialog(screen):
    assert approval_dialog(screen) is None


def test_terminal_summary_scrubs_body_caps_after_scrubbing_and_fingerprints_full_screen(lite_client, agent_row, fake_tmux):
    secret = "sk-ant-api03-" + "Ab1" * 20
    body = "git add src\n│   TOKEN=" + secret + "\n│   " + "x" * 400 + " tail"
    fake_tmux["screen"] = DIALOG.replace("git add src && git commit -m fix", body)
    p, = pending(lite_client)
    assert p["summary"].startswith("Bash command: git add src TOKEN=[redacted] ")
    assert len(p["summary"]) == 300 and "\n" not in p["summary"] and secret not in str(p)
    fake_tmux["screen"] = fake_tmux["screen"].replace("tail", "changed beyond the summary")
    assert lite_client.post(f"/api/permission/{p['id']}/allow", headers=H).status_code == 409
    assert not fake_tmux["texts"]


@pytest.mark.parametrize("command", ["zsh", "bash", "python", "", None])
def test_only_foreground_claude_can_be_listed_or_answered(lite_client, agent_row, fake_tmux, command):
    fake_tmux["screen"] = DIALOG
    p, = pending(lite_client)
    fake_tmux["sessions"][NAME]["command"] = command
    assert lite_client.post(f"/api/permission/{p['id']}/allow", headers=H).status_code == 409
    assert pending(lite_client) == []
    assert not fake_tmux["texts"] and not fake_tmux["keys"]


def test_native_claude_command_is_supported_and_fingerprinted(lite_client, agent_row, fake_tmux):
    fake_tmux["screen"] = DIALOG
    fake_tmux["sessions"][NAME]["command"] = "2.1.296"
    p, = pending(lite_client)
    fake_tmux["sessions"][NAME]["command"] = "2.1.297"
    assert lite_client.post(f"/api/permission/{p['id']}/allow", headers=H).status_code == 409
    assert not fake_tmux["texts"]
    p, = pending(lite_client)
    assert lite_client.post(f"/api/permission/{p['id']}/deny", headers=H).status_code == 200


@pytest.mark.parametrize("key", ["Space", "Right", "C-o", "0", "1", "9", "y", "n", "Enter", "C-c"])
def test_terminal_dialog_keys_use_a_closed_allowlist(lite_client, agent_row, fake_tmux, key):
    fake_tmux["screen"] = DIALOG
    r = lite_client.post(f"/api/sessions/{NAME}/keys", headers=H, json={"keys": ["Up", key]})
    assert r.status_code == 409 and r.json()["error"] == "asking"
    assert not fake_tmux["keys"] and not fake_tmux["texts"]


@pytest.mark.parametrize("key", ["Escape", "Up", "Down", "Tab", "C-c"])
def test_terminal_dialog_navigation_and_confirmed_interrupt(lite_client, agent_row, fake_tmux, key):
    fake_tmux["screen"] = DIALOG
    r = lite_client.post(f"/api/sessions/{NAME}/keys", headers=H, json={"keys": [key], "confirm": key == "C-c"})
    assert r.status_code == 200
    assert fake_tmux["keys"] == [(NAME, [key])]


@pytest.mark.parametrize("kind", ["elicitation", "question"])
def test_other_waiting_kinds_keep_their_keys_behavior(lite_client, agent_row, fake_tmux, kind):
    main.db.set_state(NAME, "waiting", "Notification")
    main.db.update_flags(NAME, {"wait_kind": kind})
    fake_tmux["screen"] = "Which option would you like?"
    r = lite_client.post(f"/api/sessions/{NAME}/keys", headers=H, json={"keys": ["Space", "Right", "C-o", "C-c"]})
    assert r.status_code == 200


@pytest.mark.parametrize("state,attention,expected", [("working", False, 0), ("idle", False, 0), ("waiting", False, 1), ("done", True, 1)])
def test_terminal_scan_only_captures_waiting_or_attention_rows(lite_client, agent_row, fake_tmux, monkeypatch, state, attention, expected):
    from app import permissions, tmux
    main.db.set_state(NAME, state, "test", attention=attention)
    fake_tmux["screen"] = DIALOG
    captures = []
    monkeypatch.setattr(tmux, "capture", lambda *a, **kw: captures.append(a[0]) or DIALOG)
    permissions.terminal_pending(main.db, [], max_age=main.SCAN_TTL)
    assert len(captures) == expected


def test_terminal_scan_reuses_captures_for_scan_ttl_but_answers_reread(lite_client, agent_row, fake_tmux, monkeypatch):
    from app import permissions, tmux
    main.db.set_state(NAME, "waiting", "PermissionRequest")
    fake_tmux["screen"] = DIALOG
    captures = []
    monkeypatch.setattr(tmux, "capture", lambda *a, **kw: captures.append(a[0]) or fake_tmux["screen"])
    t = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: t[0])
    p, = main._pending_permissions()
    assert main._pending_permissions() == [p]
    assert captures == [NAME]
    t[0] += main.SCAN_TTL
    assert main._pending_permissions() == [p]
    assert captures == [NAME, NAME]
    fake_tmux["screen"] = "idle"
    assert lite_client.post(f"/api/permission/{p['id']}/allow", headers=H).status_code == 409
    assert captures == [NAME, NAME, NAME]
    assert not fake_tmux["texts"]
    assert main._pending_permissions() == []
    assert main._pending_permissions() == []
    assert captures == [NAME] * 4  # negative captures are cached too


def test_local_answered_prompt_stays_gone_after_a_transient_empty_read(lite_client, agent_row, fake_tmux, monkeypatch):
    """A tmux list error between two reads must not re-show a prompt that was already answered (device polling re-arms; local does not)."""
    from app import tmux
    main.db.set_state(NAME, "waiting", "PermissionRequest")
    fake_tmux["screen"] = DIALOG
    monkeypatch.setattr(tmux, "capture", lambda *a, **kw: fake_tmux["screen"])
    t = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: t[0])
    p, = main._pending_permissions()
    assert lite_client.post(f"/api/permission/{p['id']}/allow", headers=H).status_code == 200
    fake_tmux["screen"] = "idle"
    t[0] += main.SCAN_TTL
    assert main._pending_permissions() == []
    fake_tmux["screen"] = DIALOG
    t[0] += main.SCAN_TTL
    assert main._pending_permissions() == []
