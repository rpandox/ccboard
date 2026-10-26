"""#330/#331: fake panes and synthetic hook payloads; no agent, git or tmux process."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time

import pytest

from app import hooks, main, permissions
from app.agents import codex_pane
from app.config import settings
from .test_tasks import H

FIX = Path(__file__).parent / "fixtures" / "agents" / "codex"


def pane(file):
    return (FIX / file).read_text()


@pytest.fixture
def lane(lite_client, fake_tmux, projects_dir, monkeypatch):
    repo = projects_dir / "shop" / "api"
    repo.mkdir(parents=True)
    name = "shop--api--report"
    sid = main.db.add_session(tmux_name=name, project="shop", repo="api", name="report", launcher="task",
                              agent="codex", cwd=str(repo / ".ccboard/worktrees/report"))
    tid = main.db.task_add(project="shop", repo="api", slug="report", title="Update report", prompt="Update report",
                           tmux_name=name, session_row=sid, agent="codex", phase="running", mode="lane")
    with main.db.lock:
        main.db.conn.execute("UPDATE sessions SET created_at=? WHERE id=?",
                             ((datetime.now(timezone.utc) - timedelta(minutes=7)).isoformat(), sid))
    fake_tmux["sessions"][name] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "codex",
                                   "path": str(repo), "pid": 1, "env": {}}
    monkeypatch.setattr(main.notify, "notify_session", lambda *a: None)
    monkeypatch.setattr(permissions, "push_request", lambda *a: None)
    return name, tid


def assert_no_typing(fake_tmux):
    assert not any(fake_tmux[k] for k in ("sent", "keys", "texts", "pasted"))


def test_folder_access_fingerprint_and_answered_screen():
    text = pane("folder_trust.txt")
    assert codex_pane.dialog(text) == "trust"
    framed = "\n".join("│ " + ln + " │" for ln in text.splitlines())
    assert codex_pane.dialog(framed) == "trust"
    for after in ("\n› Continue\n97% context left", "\n• Working\nGPT-6.1-Sol default · /workspace · Reply wit…"):
        assert codex_pane.dialog(text + after) is None
        assert codex_pane.dialog(framed + after) is None
    assert codex_pane.dialog("• The documentation asks: Trust this folder?\n› Continue") is None


@pytest.mark.parametrize("rich", [False, True])
def test_unattended_trust_fails_once_even_after_seven_minutes(lane, fake_tmux, rich):
    name, tid = lane
    fake_tmux["screen"] = pane("folder_trust.txt")
    first = main._merged_sessions(rich=rich)[0][name]
    assert first["state"] == "waiting" and main.db.task_get(tid)["phase"] == "running"
    for _ in range(2):
        sessions, _ = main._merged_sessions(rich=rich)
        assert sessions[name]["state"] == "errored"
        assert sessions[name]["pane_dialog"] == codex_pane.NOTES["trust"]
        task = main.db.task_get(tid)
        assert task["phase"] == "failed" and task["result"] == codex_pane.TRUST_FAILURE
    assert len([e for e in main.db.recent_events() if e["event"] == "AgentExited"]) == 1
    card = main._tasks_view()[0]
    assert card["column"] == "needs_you" and card["session"]["pane_dialog"] == codex_pane.NOTES["trust"]
    assert_no_typing(fake_tmux)


def test_interactive_trust_waits_and_clears_without_storing_screen_state(lane, fake_tmux):
    name, tid = lane
    with main.db.lock:
        main.db.conn.execute("UPDATE sessions SET launcher='codex' WHERE tmux_name=?", (name,))
    fake_tmux["screen"] = pane("folder_trust.txt")
    session = main._merged_sessions()[0][name]
    assert (session["state"], session["wait_kind"]) == ("waiting", "dialog")
    assert main._tasks_view()[0]["column"] == "needs_you"
    assert main.db.task_get(tid)["phase"] == "running"
    assert main.db.open_row(name)["state"] is None
    fake_tmux["screen"] += "\n› Continue\n97% context left"
    assert "pane_dialog" not in main._merged_sessions()[0][name]
    assert_no_typing(fake_tmux)


def test_task_list_fails_trust_on_second_poll_and_routes_never_answer(lane, fake_tmux, lite_client):
    name, tid = lane
    fake_tmux["screen"] = pane("folder_trust.txt")
    first = main._tasks_view()[0]
    assert first["phase"] == "running" and first["column"] == "needs_you"
    card = main._tasks_view()[0]
    assert card["id"] == tid and card["phase"] == "failed" and card["column"] == "needs_you"
    assert card["result"] == codex_pane.TRUST_FAILURE
    for route, body in (("prompt", {"text": "continue"}), ("keys", {"keys": ["Enter"]}),
                        ("keys", {"text": "1", "enter": True})):
        response = lite_client.post(f"/api/sessions/{name}/{route}", headers=H, json=body)
        assert response.status_code == 409
    assert_no_typing(fake_tmux)


@pytest.mark.parametrize("state", [None, "working", "waiting"])
def test_command_without_hook_has_explicit_notice_and_no_remote_choices(lane, fake_tmux, lite_client, state):
    name, tid = lane
    if state:
        main.db.set_state(name, state, "UserPromptSubmit" if state == "working" else "PermissionRequest")
    fake_tmux["screen"] = pane("command_approval.txt")
    card = main._tasks_view()[0]
    assert card["column"] == "needs_you" and card["phase"] == "running"
    note = card["session"]["pane_dialog"]
    assert note == codex_pane.COMMAND_NOTE + "\ngit add -- report.txt && git commit -m 'Update report'"
    assert main.db.perm_pending() == []
    response = lite_client.post(f"/api/sessions/{name}/prompt", headers=H, json={"text": "continue"})
    assert response.status_code == 409
    assert_no_typing(fake_tmux)
    fake_tmux["screen"] += "\n• Command completed\n› Continue"
    assert "pane_dialog" not in main._tasks_view()[0]["session"]


def test_command_is_capped_and_redacted(lane, fake_tmux, monkeypatch):
    secret = "private value for redaction"
    monkeypatch.setattr(main.nodes_relay, "known_secrets", lambda: [secret])
    fake_tmux["screen"] = pane("command_approval.txt").replace("git add -- report.txt && git commit -m 'Update report'",
                                                            "echo " + secret + " x" * 2000)
    note = main._tasks_view()[0]["session"]["pane_dialog"]
    assert secret not in note and "[redacted]" in note
    assert len(note) <= len(codex_pane.COMMAND_NOTE) + 301


@pytest.mark.parametrize("decision", ["allow", "deny"])
@pytest.mark.parametrize("bound", [False, True])
def test_exec_permission_hook_in_lane_before_or_after_first_hook(lane, fake_tmux, lite_client, monkeypatch, decision, bound):
    name, tid = lane
    payload = json.loads(pane("permission_exec.json"))
    payload["cwd"] = main.db.open_row(name)["cwd"]
    if bound:
        main.db.set_state(name, "working", "UserPromptSubmit", claude_session_id=payload["session_id"])
    monkeypatch.setattr(settings, "approve_timeout", 5)
    headers = {"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard-Session": name, "X-CCBoard-Agent": "codex"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(lite_client.post, "/api/permission", headers=headers, json=payload)
        pending = []
        for _ in range(100):
            pending = main._live_permissions()
            if pending:
                break
            time.sleep(0.01)
        assert len(pending) == 1
        assert pending[0]["tool_name"] == "Bash"
        assert pending[0]["summary"] == "Bash: " + payload["tool_input"]["command"]
        assert main._tasks_view()[0]["column"] == "needs_you"
        response = lite_client.post(f"/api/permission/{pending[0]['id']}/{decision}", headers=H)
        assert response.status_code == 200
        assert request.result(timeout=6).json()["behavior"] == decision
    assert main.db.task_get(tid)["phase"] == "running"
    assert_no_typing(fake_tmux)


@pytest.mark.parametrize("width", [40, 60])
@pytest.mark.parametrize("border", [False, True])
def test_wrapped_trust_dialog(lane, fake_tmux, width, border):
    name, tid = lane
    text = pane(f"folder_trust_{width}{'_border' if border else ''}.txt")
    assert max(map(len, text.splitlines())) == width
    fake_tmux["sessions"][name].update(window_width=width, window_height=24)
    fake_tmux["screen"] = text
    first = main._tasks_view()[0]
    assert first["column"] == "needs_you" and first["phase"] == "running"
    second = main._tasks_view()[0]
    assert second["phase"] == "failed" and second["result"] == codex_pane.TRUST_FAILURE
    assert_no_typing(fake_tmux)


@pytest.mark.parametrize("text", [
    "Trust this folder?",
    "echo 'Trust this folder?'",
    "Folder access\nTrust this folder?\n2. Quit\n1. Trust and continue",
    "Folder access\nTrust this folder?\n1. Trust and continue",
    "Folder access\n1. Trust and continue\nTrust this folder?\n2. Quit",
    "Folder access\nTrust this folder?\n1. Trust and continue\n2. Quit\nordinary output",
])
def test_trust_requires_complete_ordered_bottom_dialog(text):
    assert codex_pane.dialog(text) is None


def test_quoted_trust_command_is_approval_and_keeps_chain(lane, fake_tmux):
    name, tid = lane
    child = main.db.task_add(project="shop", repo="api", slug="next", title="Next", prompt="Next",
                             agent="codex", phase="queued", parent_id=tid, chain_id="review")
    fake_tmux["screen"] = pane("command_approval.txt").replace(
        "git add -- report.txt && git commit -m 'Update report'", "rg 'Trust this folder?' README.md")
    for _ in range(3):
        card = next(c for c in main._tasks_view() if c["id"] == tid)
        assert card["phase"] == "running"
        assert card["session"]["pane_dialog"] == codex_pane.COMMAND_NOTE + "\nrg 'Trust this folder?' README.md"
        assert main.db.task_get(child)["phase"] == "queued"
    assert not [e for e in main.db.recent_events() if e["event"] == "AgentExited"]
    assert_no_typing(fake_tmux)


@pytest.mark.parametrize("interruption", ["clear", "partial", "capture_error", "tmux_down", "typing"])
def test_trust_needs_consecutive_monitor_polls(lane, fake_tmux, monkeypatch, interruption):
    name, tid = lane
    fake_tmux["screen"] = pane("folder_trust.txt")
    main._merged_sessions()
    assert main.db.task_get(tid)["phase"] == "running"
    if interruption == "typing":
        main._pane_block(name, main.db.open_row(name))
        assert main.db.task_get(tid)["phase"] == "running"
    else:
        with monkeypatch.context() as m:
            if interruption in ("capture_error", "tmux_down"):
                def fail(*a, **kw):
                    error = main.tmux.TmuxDown if interruption == "tmux_down" else main.tmux.TmuxError
                    raise error("synthetic poll failure")
                m.setattr(main.tmux, "list_sessions" if interruption == "tmux_down" else "capture", fail)
            else:
                fake_tmux["screen"] = "› Continue" if interruption == "clear" else "Folder access\nTrust this folder?"
            main._merged_sessions()
        fake_tmux["screen"] = pane("folder_trust.txt")
        main._merged_sessions()
        assert main.db.task_get(tid)["phase"] == "running"
    main._merged_sessions()
    assert main.db.task_get(tid)["phase"] == "failed"


@pytest.mark.parametrize("wrap_menu", [False, True])
def test_wrapped_hook_token_never_reaches_card_api_or_logs(lane, fake_tmux, lite_client, monkeypatch, caplog, wrap_menu):
    name, _ = lane
    token = "a1b2c3d4" * 4 + "e5f60789" * 4
    monkeypatch.setattr(hooks, "_token", token)
    fake_tmux["sessions"][name].update(window_width=41, window_height=24)
    fake_tmux["pane"][name] = {"cols": 41}
    fake_tmux["screen"] = pane("command_approval.txt").replace(
        "git add -- report.txt && git commit -m 'Update report'", "echo " + token[:32] + "\n  " + token[32:] + " && xxx")
    if wrap_menu:
        fake_tmux["screen"] = "\n".join(
            line[i:i + 41] for line in fake_tmux["screen"].splitlines() for i in range(0, max(1, len(line)), 41))
    card = main._tasks_view()[0]
    responses = [lite_client.get("/api/tasks", headers=H),
                 lite_client.post(f"/api/sessions/{name}/prompt", headers=H, json={"text": "continue"})]
    assert [r.status_code for r in responses] == [200, 409]
    for output in [json.dumps(card), *(r.text for r in responses), caplog.text, json.dumps(main.db.recent_events())]:
        assert token[:32] not in output and token[32:] not in output
    assert "[redacted]" in card["session"]["pane_dialog"]
    assert_no_typing(fake_tmux)
