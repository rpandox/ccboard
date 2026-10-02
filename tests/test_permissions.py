import json
import subprocess
import threading
import time

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
    t0 = time.monotonic()
    r = _ask(lite_client, name).json()
    assert r["behavior"] is None and r["reason"] == "attached" and time.monotonic() - t0 < 2


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
