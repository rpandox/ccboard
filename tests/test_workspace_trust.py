"""Workspace trust refusals: read-only pane checks and a history-based warning."""
from datetime import datetime, timedelta, timezone

import pytest

from app import main, node_state, nodes_hub, nodes_relay, tasks
from app.config import settings
from tests.test_tasks import H, git_init

TRUST_ERROR = ("Error creating worktree: Workspace trust not yet accepted. Run 'claude' once in this directory "
               "and accept the trust dialog, then retry with --worktree.")
REASON = ("Claude has not been trusted in this folder yet: open a session in shop/api once and accept the trust dialog, "
          "then run the task again")
WARNING = ("Claude has not been trusted in this folder yet: open a session in shop/api once and accept the trust dialog "
           "before its first task")


def age_session(db, name, seconds=25):
    at = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with db.lock:
        db.conn.execute("UPDATE sessions SET created_at=? WHERE tmux_name=?", (at, name))


@pytest.fixture
def launched(lite_client, projects_dir, fake_tmux, monkeypatch):
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    git_init(projects_dir / "shop" / "api")
    main._invalidate_scan()
    response = lite_client.post("/api/projects/shop/repos/api/tasks", headers=H,
                                json={"title": "Check trust", "prompt": "Check the project"})
    assert response.status_code == 201, response.text
    task = response.json()
    age_session(main.db, task["tmux"])
    fake_tmux["sent"].clear()
    yield task
    main._invalidate_scan()


def test_trust_refusal_fails_task_and_session_once(launched, fake_tmux, monkeypatch, lite_client):
    notices = []
    monkeypatch.setattr(main.notify, "notify_session", lambda *a: notices.append(a))
    name = launched["tmux"]
    fake_tmux["sessions"][name]["command"] = "-zsh"
    fake_tmux["screen"] = "  " + TRUST_ERROR
    before = main.db.open_row(name)
    response = lite_client.get(f"/api/sessions/{name}", headers=H)
    assert response.status_code == 200
    row = main.db.open_row(name)
    assert (row["state"], row["last_message"], row["last_event"]) == ("errored", REASON, "AgentExited")
    task = main.db.task_get(launched["id"])
    assert task["phase"] == "failed" and task["result"] == REASON and task["done_at"]
    assert tasks.derive_status(task, row) == "needs_you"
    for _ in range(2):
        sessions, down = main._merged_sessions(rich=True)
        assert not down and sessions[name]["needs_attention"]
    main._mark_exited(name, before, REASON)  # another poll may already hold the old row
    events = [e for e in main.db.recent_events() if e["event"] == "AgentExited"]
    assert len(events) == 1 and events[0]["message"] == REASON
    assert len(notices) == 1 and notices[0][2] == REASON
    assert not any(fake_tmux[k] for k in ("sent", "keys", "texts", "pasted"))
    scan = main._scan_entry()
    remote = node_state.build(main.db, scan)
    assert remote["needs_you"]["errors"] == 1
    assert remote["tasks"][0]["failure_reason"] == REASON
    assert remote["sessions"][0]["state"] == "errored" and remote["sessions"][0]["needs_you"]
    cleaned = nodes_hub.clean_state(remote, {})
    assert cleaned["tasks"][0]["failure_reason"] == REASON
    assert cleaned["projects"][0]["repos"][0]["claude_trust_warning"] == WARNING


@pytest.mark.parametrize("command,screen", [
    ("zsh", "Error creating worktree: branch already exists"),
    ("claude", TRUST_ERROR),
    ("claude", "Do you trust the files in this folder?\n1. Yes, I trust this folder\n2. No, exit"),
])
def test_other_exit_or_live_dialog_keeps_state_and_never_types(launched, fake_tmux, lite_client, command, screen):
    name = launched["tmux"]
    fake_tmux["sessions"][name]["command"] = command
    fake_tmux["screen"] = screen
    sessions, _ = main._merged_sessions(rich=True)
    assert sessions[name]["state"] == "unknown"
    assert main.db.task_get(launched["id"])["phase"] == "running"
    response = lite_client.post(f"/api/sessions/{name}/prompt", headers=H, json={"text": "Continue"})
    assert response.status_code == 409
    assert not any(fake_tmux[k] for k in ("sent", "keys", "texts", "pasted"))
    assert not any(e["event"] == "AgentExited" for e in main.db.recent_events())


def test_launch_grace_then_failure_within_one_minute(launched, fake_tmux):
    name = launched["tmux"]
    fake_tmux["sessions"][name]["command"] = "bash"
    fake_tmux["screen"] = TRUST_ERROR
    age_session(main.db, name, 5)
    main._merged_sessions()
    assert main.db.task_get(launched["id"])["phase"] == "running"
    age_session(main.db, name, 21)
    main._merged_sessions()
    assert main.db.task_get(launched["id"])["phase"] == "failed"


def warning():
    main._invalidate_scan()
    p = next(p for p in main._scan_entry()["projects"] if p["name"] == "shop")
    return next(r for r in p["repos"] if r["name"] == "api")["claude_trust_warning"]


def test_warning_uses_ended_interactive_claude_history(launched):
    assert warning() == WARNING  # starting a task does not count as accepting trust
    for agent, launcher in (("shell", "shell"), ("codex", "codex")):
        main.db.add_session(tmux_name=f"shop--api--{agent}", project="shop", repo="api", name=agent, launcher=launcher, agent=agent)
    assert warning() == WARNING
    main.db.add_session(tmux_name="shop--api--first", project="shop", repo="api", name="first", launcher="claude")
    main.db.end("shop--api--first", "exited")
    assert warning() is None


def test_warning_clears_after_done_claude_task(launched):
    assert warning() == WARNING
    main.db.task_update(launched["id"], phase="done")
    assert warning() is None


@pytest.mark.parametrize("launcher", ["task", "headless"])
def test_warning_clears_after_claude_hook_state(launched, launcher):
    name = "shop--api--hooked"
    main.db.add_session(tmux_name=name, project="shop", repo="api", name="hooked", launcher=launcher, agent="claude")
    assert warning() == WARNING
    main.db.set_state(name, "working", "UserPromptSubmit")
    assert warning() is None


def test_trust_failure_and_done_codex_task_do_not_clear_warning(launched):
    main.db.set_state(launched["tmux"], "errored", "AgentExited", message=REASON)
    main.db.task_update(launched["id"], phase="failed", result=REASON)
    assert warning() == WARNING
    main.db.task_update(launched["id"], phase="done", agent="codex")
    assert warning() == WARNING


@pytest.mark.parametrize("phase,state", [("done", "done"), ("running", "working")])
def test_prompt_does_not_fail_a_task_with_old_trust_text(launched, fake_tmux, lite_client, phase, state):
    name = launched["tmux"]
    main.db.task_update(launched["id"], phase=phase)
    main.db.set_state(name, state, "Stop" if state == "done" else "UserPromptSubmit")
    fake_tmux["sessions"][name]["command"] = "zsh"
    fake_tmux["screen"] = TRUST_ERROR
    response = lite_client.post(f"/api/sessions/{name}/prompt", headers=H,
                                json={"text": "Continue", "queue": state == "working"})
    assert main.db.task_get(launched["id"])["phase"] == phase
    assert main.db.open_row(name)["state"] == state
    assert response.status_code == 200
    assert not any(e["event"] == "AgentExited" for e in main.db.recent_events())


@pytest.mark.parametrize("phase,age", [("done", 25), ("running", 90)])
def test_poll_ignores_finished_or_old_launches(launched, fake_tmux, phase, age):
    name = launched["tmux"]
    main.db.task_update(launched["id"], phase=phase)
    age_session(main.db, name, age)
    fake_tmux["sessions"][name]["command"] = "zsh"
    fake_tmux["screen"] = TRUST_ERROR
    main._merged_sessions()
    assert not main.db.open_row(name)["state"]
    assert main.db.task_get(launched["id"])["phase"] == phase


def test_echoed_launch_with_quoted_trust_sentence_is_not_a_failure(launched, fake_tmux):
    name = launched["tmux"]
    fake_tmux["sessions"][name]["command"] = "zsh"
    fake_tmux["screen"] = f"claude --worktree x -- '{TRUST_ERROR}'\nzsh: command not found: claude"
    main._merged_sessions()
    assert main.db.task_get(launched["id"])["phase"] == "running"
    assert not main.db.open_row(name)["state"]
    assert not any(e["event"] == "AgentExited" for e in main.db.recent_events())


@pytest.mark.parametrize("shape", ["state", "detail"])
@pytest.mark.parametrize("phase,reason,expected", [
    ("failed", "private peer output", None),
    ("failed", REASON, REASON),
    ("running", REASON, None),
    ("failed", tasks.workspace_trust_reason("other", "api"), None),
    ("failed", REASON + "\n", None),
])
def test_peer_failure_reason_is_exact_and_scoped(shape, phase, reason, expected):
    row = {"id": 1, "project": "shop\x00", "repo": "api", "phase": phase, "failure_reason": reason}
    if shape == "detail":
        cleaned = nodes_relay._shape_task(row, {})
    else:
        cleaned = nodes_hub.clean_state({"node": {"id": "n_" + "a" * 16}, "tasks": [row]}, {})["tasks"][0]
    assert cleaned["project"] == "shop"
    if expected is None:
        assert "failure_reason" not in cleaned
    else:
        assert cleaned["failure_reason"] == expected


@pytest.mark.parametrize("reason,expected", [
    ("private peer output", None),
    (WARNING, WARNING),
    (REASON, None),
    (tasks.workspace_trust_warning("other", "api"), None),
    (WARNING + "\n", None),
])
def test_peer_repo_warning_is_exact_and_scoped(reason, expected):
    body = {"node": {"id": "n_" + "a" * 16}, "projects": [{"name": "shop\x00", "repos": [
        {"name": "api", "claude_trust_warning": reason}]}]}
    repo = nodes_hub.clean_state(body, {})["projects"][0]["repos"][0]
    if expected is None:
        assert "claude_trust_warning" not in repo
    else:
        assert repo["claude_trust_warning"] == expected
