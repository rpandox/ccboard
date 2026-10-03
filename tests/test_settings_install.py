"""scripts/claude_settings.py: the hooks ccboard registers in Claude Code's settings.json (v0.5.7 hooks v2) and the two hook wrappers."""
import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "claude_settings.py"
APP = Path("/opt/ccboard")


def _load():
    spec = importlib.util.spec_from_file_location("claude_settings_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cs = _load()

ASYNC_EVENTS = ["SessionStart", "UserPromptSubmit", "Notification", "Stop", "StopFailure", "SubagentStart", "SubagentStop",
                "PreCompact", "PostCompact", "PostModelSwitch", "TaskCreated", "TaskCompleted", "PostToolBatch", "ConfigChange"]
FOREIGN = {"type": "command", "command": "/usr/local/bin/mine"}


def cmds(data, ev):
    return [h["command"] for g in data["hooks"][ev] for h in g["hooks"]]


def entries(data, ev, suffix):
    return [h for g in data["hooks"][ev] for h in g["hooks"] if h["command"].endswith(suffix)]


def test_events_are_the_agreed_set_in_order():
    assert cs.EVENTS == ASYNC_EVENTS + ["SessionEnd"]
    assert cs.EVENTS[:2] == ["SessionStart", "UserPromptSubmit"]          # tests/test_agents_claude.py pins the head
    assert not [e for e in cs.EVENTS if e.startswith("Worktree")]


def test_the_adapters_fallback_list_is_the_same_set():
    from app.agents import claude
    assert claude.FALLBACK_EVENTS == cs.EVENTS, "FALLBACK_EVENTS drifted from scripts/claude_settings.py EVENTS"
    assert claude.ClaudeAgent()._required_events() == cs.EVENTS


def test_install_registers_every_event_with_the_right_wrapper():
    data = cs.install({}, APP, remote_approve=True, approve_timeout=90)
    for ev in ASYNC_EVENTS:
        (h,) = [h for g in data["hooks"][ev] for h in g["hooks"]]
        assert h == {"type": "command", "command": "/opt/ccboard/bin/ccboard-hook", "async": True, "timeout": 5}, ev
    (end,) = [h for g in data["hooks"]["SessionEnd"] for h in g["hooks"]]
    assert end == {"type": "command", "command": "/opt/ccboard/bin/ccboard-hook-fast", "timeout": 3}, "synchronous: no async key"
    assert "async" not in end
    (perm,) = [h for g in data["hooks"]["PermissionRequest"] for h in g["hooks"]]
    assert perm == {"type": "command", "command": "/opt/ccboard/bin/ccboard-permission", "timeout": 120}
    assert data["statusLine"]["command"] == "/opt/ccboard/bin/ccboard-statusline"
    assert set(data["hooks"]) == set(cs.EVENTS) | {"PermissionRequest"}


def test_worktree_hooks_are_never_registered():
    data = cs.install({}, APP)
    assert not [e for e in data["hooks"] if e.startswith("Worktree")]


def test_no_remote_approve_skips_the_permission_hook():
    assert "PermissionRequest" not in cs.install({}, APP, remote_approve=False)["hooks"]


def test_install_is_idempotent_and_keeps_foreign_hooks():
    start = {"model": "opus", "hooks": {"Stop": [{"hooks": [FOREIGN]}], "PreToolUse": [{"matcher": "Bash", "hooks": [FOREIGN]}]},
             "statusLine": {"type": "command", "command": "/my/statusline"}}
    once = cs.install(copy.deepcopy(start), APP)
    twice = cs.install(copy.deepcopy(once), APP)
    thrice = cs.install(copy.deepcopy(twice), APP)
    assert once == twice == thrice
    assert once["model"] == "opus" and once["statusLine"] == {"type": "command", "command": "/my/statusline"}
    assert cmds(once, "Stop").count("/usr/local/bin/mine") == 1 and len(entries(once, "Stop", "ccboard-hook")) == 1
    assert once["hooks"]["PreToolUse"] == start["hooks"]["PreToolUse"]
    # a foreign hook sharing a group with ours keeps its group
    shared = {"hooks": {"Stop": [{"matcher": "x", "hooks": [FOREIGN, {"type": "command", "command": "/old/bin/ccboard-hook"}]}]}}
    out = cs.install(copy.deepcopy(shared), APP)
    assert out["hooks"]["Stop"][0] == {"matcher": "x", "hooks": [FOREIGN]}
    assert len(entries(out, "Stop", "ccboard-hook")) == 1


def test_an_old_install_is_upgraded_in_place():
    """The container entrypoint re-runs `install` on every start: a box with the 6-event set gets the new events, not duplicates."""
    old = {"hooks": {ev: [{"hooks": [{"type": "command", "command": "/opt/ccboard/bin/ccboard-hook", "async": True, "timeout": 5}]}]
                     for ev in ("SessionStart", "UserPromptSubmit", "Notification", "Stop", "StopFailure", "SessionEnd")}}
    new = cs.install(old, APP)
    assert set(new["hooks"]) >= set(cs.EVENTS)
    assert len(new["hooks"]["SessionEnd"]) == 1 and new["hooks"]["SessionEnd"][0]["hooks"][0]["command"].endswith("ccboard-hook-fast")
    assert all(len(new["hooks"][ev]) == 1 for ev in cs.EVENTS)


def test_strip_ours_removes_only_ours():
    data = cs.install({"hooks": {"Stop": [{"hooks": [FOREIGN]}], "SessionEnd": [{"hooks": [FOREIGN]}]}}, APP)
    out = cs.strip_ours(copy.deepcopy(data))
    assert out["hooks"] == {"Stop": [{"hooks": [FOREIGN]}], "SessionEnd": [{"hooks": [FOREIGN]}]}
    assert "statusLine" not in out
    # fast wrapper and the permission hook alone, wherever they sit
    lone = {"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "/x/ccboard-hook-fast"}]}],
                      "PermissionRequest": [{"hooks": [{"type": "command", "command": "/x/ccboard-permission"}]}]}}
    assert cs.strip_ours(lone) == {}
    # nothing of ours: nothing changes
    foreign_only = {"hooks": {"Stop": [{"hooks": [FOREIGN]}]}, "statusLine": {"type": "command", "command": "/my/sl"}}
    assert cs.strip_ours(copy.deepcopy(foreign_only)) == foreign_only
    # a foreign statusLine is not ours to remove
    assert cs.strip_ours(cs.install({"statusLine": {"type": "command", "command": "/my/sl"}}, APP))["statusLine"]["command"] == "/my/sl"


def test_hooks_of_a_shape_we_do_not_know_survive_a_strip_and_stop_an_install():
    odd = {"hooks": {"Stop": [{"hooks": [FOREIGN]}, "not-a-group", {"hooks": "nope"}], "Weird": {"a": 1}}}
    out = cs.strip_ours(copy.deepcopy(odd))
    assert out == odd
    with pytest.raises(RuntimeError, match="hooks.*not an object"):
        cs.install({"hooks": ["x"]}, APP)
    with pytest.raises(RuntimeError, match="hooks.Stop is not a list"):
        cs.install({"hooks": {"Stop": {"a": 1}}}, APP)
    assert cs.install({"hooks": None}, APP)["hooks"]["Stop"]


def run_script(*args, home=None, env_extra=None):
    import os
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)


def test_cli_install_show_remove_on_a_settings_file(tmp_path):
    f = tmp_path / "x.json"
    f.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [FOREIGN]}]}, "model": "opus"}))
    r = run_script("install", "--settings", str(f), "--app-dir", str(APP))
    assert r.returncode == 0, r.stderr
    first = f.read_text()
    assert run_script("install", "--settings", str(f), "--app-dir", str(APP)).returncode == 0
    assert f.read_text() == first, "re-running changes nothing"
    shown = json.loads(run_script("show", "--settings", str(f)).stdout)
    assert set(shown["hooks"]) == set(cs.EVENTS) | {"PermissionRequest"} and shown["statusLine"]["command"].endswith("ccboard-statusline")
    r = run_script("remove", "--settings", str(f))
    assert r.returncode == 0
    assert json.loads(f.read_text()) == {"hooks": {"Stop": [{"hooks": [FOREIGN]}]}, "model": "opus"}


def test_cli_worktree_flag_is_parsed_and_ignored_with_a_note(tmp_path):
    f = tmp_path / "x.json"
    r = run_script("install", "--settings", str(f), "--app-dir", str(APP), "--with-worktree-hooks")
    assert r.returncode == 0 and "--with-worktree-hooks is ignored" in r.stderr
    assert not [e for e in json.loads(f.read_text())["hooks"] if e.startswith("Worktree")]


def test_cli_refuses_a_settings_file_it_cannot_read(tmp_path):
    f = tmp_path / "x.json"
    f.write_text("{not json")
    r = run_script("install", "--settings", str(f), "--app-dir", str(APP))
    assert r.returncode != 0 and "not valid JSON" in r.stderr and f.read_text() == "{not json"
    f.write_text("[]")
    assert run_script("install", "--settings", str(f), "--app-dir", str(APP)).returncode != 0
    f.write_text(json.dumps({"hooks": {"Stop": "x"}}))
    r = run_script("install", "--settings", str(f), "--app-dir", str(APP))
    assert r.returncode != 0 and "hooks.Stop is not a list" in r.stderr


def test_cli_default_path_follows_claude_config_dir(tmp_path):
    r = run_script("install", "--app-dir", str(APP), env_extra={"CLAUDE_CONFIG_DIR": str(tmp_path)})
    assert r.returncode == 0 and (tmp_path / "settings.json").is_file()


# ---------- the wrappers ----------

@pytest.mark.parametrize("script", ["ccboard-hook", "ccboard-hook-fast"])
def test_wrapper_is_valid_executable_sh(script):
    p = ROOT / "bin" / script
    assert p.stat().st_mode & 0o111, "executable"
    assert p.read_text().startswith("#!/bin/sh\n")
    assert subprocess.run(["bash", "-n", str(p)]).returncode == 0
    assert subprocess.run(["sh", "-n", str(p)]).returncode == 0


def test_wrapper_flags():
    slow = (ROOT / "bin" / "ccboard-hook").read_text()
    fast = (ROOT / "bin" / "ccboard-hook-fast").read_text()
    assert '-m "${CCBOARD_CURL_MAX:-3}"' in slow and "-m 2 " in fast
    for text in (slow, fast):
        assert 'X-CCBoard-Agent: ${CCBOARD_AGENT:-claude}' in text
        assert "X-CCBoard-Token: $tok" in text and "--data-binary @-" in text
        assert "exit 0" in text.splitlines()[-1]
    assert "&" not in "".join(ln for ln in fast.splitlines() if not ln.lstrip().startswith("#")).replace("&&", "")


def _serve_once(tmp_path):
    """A one-request loopback server that records the headers and body it gets."""
    import http.server
    import threading
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got["path"] = self.path
            got["headers"] = {k.lower(): v for k, v in self.headers.items()}
            got["body"] = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    return srv, got


@pytest.mark.parametrize("script", ["ccboard-hook", "ccboard-hook-fast"])
def test_wrapper_posts_the_payload_with_token_session_and_agent(tmp_path, script):
    import os
    srv, got = _serve_once(tmp_path)
    tok = tmp_path / "tok"
    tok.write_text("sekret\n")
    env = {**os.environ, "CCBOARD_URL": f"http://127.0.0.1:{srv.server_port}", "CCBOARD_HOOK_TOKEN_FILE": str(tok),
           "CCBOARD_SESSION": "shop--api--x", "CCBOARD_AGENT": "codex"}
    r = subprocess.run([str(ROOT / "bin" / script)], input=b'{"hook_event_name":"Stop"}', capture_output=True, env=env, timeout=10)
    srv.server_close()
    assert r.returncode == 0
    assert got["path"] == "/api/hook" and got["body"] == b'{"hook_event_name":"Stop"}'
    h = got["headers"]
    assert h["x-ccboard-token"] == "sekret" and h["x-ccboard-session"] == "shop--api--x" and h["x-ccboard-agent"] == "codex"


@pytest.mark.parametrize("script", ["ccboard-hook", "ccboard-hook-fast"])
def test_wrapper_defaults_the_agent_and_is_silent_when_the_board_is_down_or_the_token_is_missing(tmp_path, script):
    import os
    srv, got = _serve_once(tmp_path)
    tok = tmp_path / "tok"
    tok.write_text("t\n")
    base = {k: v for k, v in os.environ.items() if not k.startswith("CCBOARD_")}
    env = {**base, "CCBOARD_URL": f"http://127.0.0.1:{srv.server_port}", "CCBOARD_HOOK_TOKEN_FILE": str(tok)}
    assert subprocess.run([str(ROOT / "bin" / script)], input=b"{}", capture_output=True, env=env, timeout=10).returncode == 0
    srv.server_close()
    assert got["headers"]["x-ccboard-agent"] == "claude"
    down = {**base, "CCBOARD_URL": "http://127.0.0.1:9", "CCBOARD_HOOK_TOKEN_FILE": str(tok)}
    r = subprocess.run([str(ROOT / "bin" / script)], input=b"{}", capture_output=True, env=down, timeout=10)
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
    nothing = {**base, "CCBOARD_HOOK_TOKEN_FILE": str(tmp_path / "missing")}
    assert subprocess.run([str(ROOT / "bin" / script)], input=b"{}", capture_output=True, env=nothing, timeout=10).returncode == 0
