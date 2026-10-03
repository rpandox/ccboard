import json
import subprocess
import threading
import time

import pytest

from app import hooks, permissions
from app.config import settings

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


def _session(lite_client, projects_dir):
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)
    return lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]


def _ask(lite_client, name, tool="Bash", tool_input=None):
    return lite_client.post("/api/permission", headers={"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard-Session": name},
                       content=json.dumps({"hook_event_name": "PermissionRequest", "tool_name": tool,
                                           "tool_input": tool_input or {"command": "npm test"}}))


def test_summarize():
    assert permissions.summarize("Bash", {"command": "rm  -rf\n node_modules"}) == "Bash: rm -rf node_modules"
    assert permissions.summarize("Edit", {"file_path": "/a/b.py"}) == "Edit: /a/b.py"
    assert permissions.summarize("mcp__x__y", {"q": 1}) == 'mcp__x__y: {"q": 1}'


def test_timeout_falls_back_to_tui(lite_client, projects_dir, fake_tmux, monkeypatch):
    monkeypatch.setattr(settings, "approve_timeout", 1.0)
    name = _session(lite_client, projects_dir)
    r = _ask(lite_client, name).json()
    assert r["behavior"] is None and r["reason"] == "timeout"
    st = lite_client.get("/api/state", headers=H).json()
    s = st["projects"][0]["repos"][0]["sessions"][0]
    assert s["state"] == "waiting" and s["last_message"] == "permission: Bash: npm test" and st["pending_permissions"] == []


def test_attached_session_is_not_delayed(lite_client, projects_dir, fake_tmux):
    name = _session(lite_client, projects_dir)
    fake_tmux["sessions"][name]["attached"] = 1
    fake_tmux["clients"] = [{"session": name, "flags": {"attached", "focused", "UTF-8"}}]       # a full client: a person at the terminal
    t0 = time.monotonic()
    r = _ask(lite_client, name).json()
    assert r["behavior"] is None and r["reason"] == "attached" and time.monotonic() - t0 < 2


GRID_ONLY = [{"attached", "focused", "ignore-size"}]                                   # attach -f ignore-size: a quad/dock tile
RO_ONLY = [{"attached", "focused", "ignore-size", "read-only"}]                         # attach -r sets read-only AND ignore-size
CONTROL_ONLY = [{"attached", "control-mode"}]


@pytest.mark.parametrize("flag_sets", [GRID_ONLY, RO_ONLY, CONTROL_ONLY, GRID_ONLY + RO_ONLY], ids=["grid", "ro", "control", "grid+ro"])
def test_grid_and_read_only_clients_do_not_short_circuit_remote_approve(lite_client, projects_dir, fake_tmux, monkeypatch, flag_sets):
    """tmux counts every client in session_attached, but a tile or a read-only view is not someone who can answer the TUI prompt."""
    monkeypatch.setattr(settings, "approve_timeout", 1.0)
    name = _session(lite_client, projects_dir)
    fake_tmux["sessions"][name]["attached"] = len(flag_sets)
    fake_tmux["clients"] = [{"session": name, "flags": f} for f in flag_sets]
    r = _ask(lite_client, name).json()
    assert r["behavior"] is None and r["reason"] == "timeout", "waited for a remote answer instead of 'attached'"
    st = lite_client.get("/api/state", headers=H).json()
    assert st["pending_permissions"] == [], "the timed-out request is expired, not left pending"


def test_a_full_client_short_circuits_even_next_to_tiles_but_not_on_another_session(lite_client, projects_dir, fake_tmux, monkeypatch):
    monkeypatch.setattr(settings, "approve_timeout", 1.0)
    name = _session(lite_client, projects_dir)
    fake_tmux["sessions"][name]["attached"] = 3
    fake_tmux["clients"] = [{"session": name, "flags": f} for f in GRID_ONLY + RO_ONLY] + [{"session": name, "flags": {"attached"}}]
    t0 = time.monotonic()
    assert _ask(lite_client, name).json()["reason"] == "attached" and time.monotonic() - t0 < 2
    fake_tmux["clients"] = [{"session": "other--x--y", "flags": {"attached"}}]                    # someone else's terminal
    fake_tmux["sessions"][name]["attached"] = 0
    assert _ask(lite_client, name).json()["reason"] == "timeout"


def test_list_clients_failure_falls_back_to_the_attached_count(lite_client, projects_dir, fake_tmux, monkeypatch):
    """When in doubt the TUI prompt is shown at once (every client counts), never swallowed behind a remote-approve wait."""
    monkeypatch.setattr(settings, "approve_timeout", 1.0)
    name = _session(lite_client, projects_dir)
    fake_tmux["clients_error"] = True
    fake_tmux["sessions"][name]["attached"] = 1
    t0 = time.monotonic()
    r = _ask(lite_client, name).json()
    assert r["behavior"] is None and r["reason"] == "attached" and time.monotonic() - t0 < 2
    fake_tmux["sessions"][name]["attached"] = 0                  # nobody attached and list-clients down: the remote-approve path
    assert _ask(lite_client, name).json()["reason"] == "timeout"


def test_tmux_down_during_the_check_still_answers(lite_client, projects_dir, fake_tmux, monkeypatch):
    from app import tmux
    monkeypatch.setattr(settings, "approve_timeout", 1.0)
    name = _session(lite_client, projects_dir)

    def down(*a, **k):
        raise tmux.TmuxDown("no server running")
    monkeypatch.setattr(tmux, "real_clients", down)
    monkeypatch.setattr(tmux, "list_sessions", down)
    assert _ask(lite_client, name).json()["reason"] == "timeout"       # not a 503: the hook script would show nothing and Claude its prompt


def test_remote_allow_and_deny(lite_client, projects_dir, fake_tmux, monkeypatch):
    monkeypatch.setattr(settings, "approve_timeout", 10.0)
    sent = []
    monkeypatch.setattr(permissions, "push_request", lambda pid, name, summary: sent.append((pid, name, summary)))
    name = _session(lite_client, projects_dir)

    def decide_later(decision):
        for _ in range(50):
            time.sleep(0.1)
            pend = lite_client.get("/api/state", headers=H).json()["pending_permissions"]
            if pend:
                assert lite_client.post(f"/api/permission/{pend[0]['id']}/{decision}", headers=H).status_code == 200
                assert lite_client.post(f"/api/permission/{pend[0]['id']}/{decision}", headers=H).status_code == 409  # once
                return
        raise AssertionError("no pending permission appeared")

    for decision in ("allow", "deny"):
        t = threading.Thread(target=decide_later, args=(decision,))
        t.start()
        r = _ask(lite_client, name, tool="Edit", tool_input={"file_path": "/x.py"}).json()
        t.join()
        assert r["behavior"] == decision
        if decision == "deny":
            assert r["message"] == "Denied from ccboard"
    assert sent and sent[0][1] == name and sent[0][2] == "Edit: /x.py"
    assert lite_client.post("/api/permission/999999/allow", headers=H).status_code == 404
    assert lite_client.post("/api/permission/1/maybe", headers=H).status_code == 400


def test_hook_script_output_shape(tmp_path):
    """The shell script turns the board's answer into the hookSpecificOutput JSON, or nothing."""
    import pathlib
    script = pathlib.Path(__file__).resolve().parent.parent / "bin" / "ccboard-permission"
    py = script.read_text().split("python3 -c '")[1].split("' 2>/dev/null")[0]
    out = subprocess.run(["python3", "-c", py], input='{"behavior":"deny"}', capture_output=True, text=True).stdout
    assert json.loads(out)["hookSpecificOutput"]["decision"] == {"behavior": "deny", "message": "Denied from ccboard"}
    out = subprocess.run(["python3", "-c", py], input='{"behavior":"allow"}', capture_output=True, text=True).stdout
    assert json.loads(out)["hookSpecificOutput"]["decision"] == {"behavior": "allow"}
    assert subprocess.run(["python3", "-c", py], input='{"behavior":null,"reason":"timeout"}', capture_output=True, text=True).stdout == ""
    assert subprocess.run(["python3", "-c", py], input='not json', capture_output=True, text=True).stdout == ""


def test_settings_registers_permission_hook(tmp_path, monkeypatch):
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("cs", str(pathlib_root() / "scripts" / "claude_settings.py"))
    cs = importlib.util.module_from_spec(spec); spec.loader.exec_module(cs)
    data = cs.install({}, pathlib_root(), remote_approve=True, approve_timeout=90)
    perm = data["hooks"]["PermissionRequest"][0]["hooks"][0]
    assert perm["command"].endswith("bin/ccboard-permission") and perm["timeout"] == 120 and "async" not in perm
    data2 = cs.install(data, pathlib_root(), remote_approve=False)
    assert "PermissionRequest" not in data2["hooks"] and len(data2["hooks"]["Stop"]) == 1


def pathlib_root():
    import pathlib
    return pathlib.Path(__file__).resolve().parent.parent
