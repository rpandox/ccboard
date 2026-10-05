import json
import subprocess
import threading
import time

import pytest

from app import hooks, notify, permissions
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


PATCH = """*** Begin Patch
*** Update File: src/app/main.py
@@ def run():
-    old()
+    new()
*** Add File: docs/new notes.md
+hello
*** Delete File: legacy/old.txt
*** Update File: src/app/main.py
@@
-    again()
+    twice()
*** End Patch"""


def test_summarize_apply_patch_lists_the_files_not_the_patch():
    want = "apply_patch: src/app/main.py, docs/new notes.md, legacy/old.txt"                   # each file once, in patch order
    assert permissions.summarize("apply_patch", {"input": PATCH}) == want                         # the apply_patch tool's own key
    assert permissions.summarize("apply_patch", {"command": PATCH}) == want                       # a shell-style request
    assert permissions.summarize("apply_patch", {"command": ["apply_patch", PATCH]}) == want      # argv: ["apply_patch", <patch>]
    assert permissions.summarize("apply_patch", PATCH) == want                                    # the plain string the tool was given
    assert permissions.summarize("apply_patch", {"input": PATCH.replace("\n", "\r\n")}) == want   # CRLF does not stick to a name
    assert "@@" not in permissions.summarize("apply_patch", {"input": PATCH})
    many = "\n".join(f"*** Add File: dir/file-{i:03d}.txt" for i in range(60))
    assert len(permissions.summarize("apply_patch", many)) <= 300


def test_summarize_apply_patch_falls_back_to_what_there_is():
    assert permissions.summarize("apply_patch", {"input": "*** Begin Patch"}) == "apply_patch: *** Begin Patch"      # no file lines: the text
    assert permissions.summarize("apply_patch", "just some words") == "apply_patch: just some words"
    assert permissions.summarize("apply_patch", {}) == "apply_patch: {}" and permissions.summarize("apply_patch", None) == "apply_patch: {}"
    assert permissions.summarize("apply_patch", {"input": 5}) == 'apply_patch: {"input": 5}'
    assert permissions.summarize("Bash", PATCH) == "Bash: ", "only apply_patch reads a plain string as a patch"


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


@pytest.fixture
def ntfy(monkeypatch):
    """ntfy on with a public URL; every publish lands in the list as the JSON body the board POSTed."""
    from app import notify
    sent = []

    class R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(notify.urllib.request, "urlopen", lambda req, timeout=5: sent.append(json.loads(req.data)) or R())
    monkeypatch.setattr(settings, "ntfy_url", "http://127.0.0.1:2586")
    monkeypatch.setattr(settings, "ntfy_topic", "ccboard")
    monkeypatch.setattr(settings, "public_url", "https://box.ts.net:8443")
    notify._last.clear()
    notify._rl_last.clear()
    return sent


def test_push_request_body_carries_the_ask_and_the_three_buttons(ntfy, tmp_path, monkeypatch):
    from app import notify
    from app.db import DB
    d = DB(tmp_path / "p.db")
    d.add_session(tmux_name="shop--api--s1", project="shop", repo="api", name="s1", launcher="claude")
    d.set_state("shop--api--s1", "working", "UserPromptSubmit", prompt="run the tests and fix what breaks")
    d.task_add(project="shop", repo="api", slug="t", title="Green CI", prompt="p", tmux_name="shop--api--s1", worktree="/w", branch="b",
               session_row=d.open_row("shop--api--s1")["id"], phase="running")
    monkeypatch.setattr(notify, "_db", d)
    web = []
    monkeypatch.setattr(notify.push, "send_all", lambda db_, title, body, url="/", tag=None, extra=None: web.append((title, body, url, tag, extra)) or 1)
    d.set_state("shop--api--s1", "waiting", "PermissionRequest", message="permission: Bash: npm test", attention=True)    # what the permission route does first
    permissions.push_request(5, "shop--api--s1", "Bash: npm test")
    body = ntfy[-1]
    assert body["title"] == "◆ shop/api · s1: needs you" and body["priority"] == 4 and body["tags"] == ["bell", "key"]
    assert body["message"] == "Green CI\n› run the tests and fix what breaks\n? Bash: npm test"
    assert body["click"] == "https://box.ts.net:8443/#/s/shop--api--s1"
    assert [a["label"] for a in body["actions"]] == ["Allow", "Deny", "Terminal"]
    allow, deny, term = body["actions"]
    assert allow["url"] == "https://box.ts.net:8443/api/permission/5/allow" and allow["method"] == "POST"
    assert deny["url"].endswith("/api/permission/5/deny") and deny["headers"] == {"X-CCBoard": "1"}
    assert term == {"action": "view", "label": "Terminal", "url": "https://box.ts.net:8443/term/shop--api--s1"}
    title, wbody, url, tag, extra = web[-1]
    assert tag == "shop--api--s1" and url == "/#/s/shop--api--s1" and wbody == body["message"]
    assert extra["perm_id"] == 5 and extra["state"] == "waiting" and extra["agent"] == "claude" and extra["tmux"] == "shop--api--s1"
    assert extra["actions"] == [{"action": "allow", "title": "Allow"}, {"action": "deny", "title": "Deny"}, {"action": "terminal", "title": "Terminal"}]
    assert extra["renotify"] is True and extra["badge"] == 1 and isinstance(extra["ts"], int)          # the SW shows the first two buttons; the session is waiting


def test_push_request_without_a_row_still_asks(ntfy, monkeypatch):
    from app import notify
    monkeypatch.setattr(notify, "_db", None)
    permissions.push_request(9, "x--y--z", "Edit: /a/b.py")
    body = ntfy[-1]
    assert body["title"] == "x / y · z: needs you" and body["message"] == "? Edit: /a/b.py"
    assert [a["label"] for a in body["actions"]] == ["Allow", "Deny", "Terminal"] and body["actions"][0]["url"].endswith("/permission/9/allow")


def test_push_request_never_raises(ntfy, monkeypatch):
    from app import notify

    def boom(*a, **k):
        raise RuntimeError("no notice today")

    monkeypatch.setattr(notify, "build", boom)
    permissions.push_request(1, "x--y--z", "Bash: ls")          # the hook still gets its answer
    assert ntfy == []


def test_the_board_pushes_the_pending_request_with_its_id(lite_client, projects_dir, fake_tmux, monkeypatch, ntfy):
    monkeypatch.setattr(settings, "approve_timeout", 10.0)
    name = _session(lite_client, projects_dir)

    def decide_later():
        for _ in range(50):
            time.sleep(0.1)
            pend = lite_client.get("/api/state", headers=H).json()["pending_permissions"]
            if pend:
                lite_client.post(f"/api/permission/{pend[0]['id']}/allow", headers=H)
                return
        raise AssertionError("no pending permission appeared")

    t = threading.Thread(target=decide_later)
    t.start()
    assert _ask(lite_client, name).json()["behavior"] == "allow"
    t.join()
    body = ntfy[0]
    assert body["title"].endswith("· " + name.split("--")[2] + ": needs you") and "? Bash: npm test" in body["message"]
    assert [a["label"] for a in body["actions"]] == ["Allow", "Deny", "Terminal"]
    from app import main
    pid = int(body["actions"][0]["url"].split("/permission/")[1].split("/")[0])         # the button names the real request
    row = main.db.perm_get(pid)
    assert row["tool_name"] == "Bash" and row["decision"] == "allow" and row["tmux_name"] == name


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


def test_push_request_obeys_the_needs_you_switch(ntfy, tmp_path, monkeypatch):
    from app import notify
    from app.db import DB
    d = DB(tmp_path / "q.db")
    monkeypatch.setattr(notify, "_db", d)
    web = []
    monkeypatch.setattr(notify.push, "send_all", lambda db_, title, body, url="/", tag=None, extra=None: web.append(title) or 1)
    notify.set_prefs({"needs": False})
    permissions.push_request(3, "shop--api--s1", "Bash: ls")
    assert ntfy == [] and web == []                                                       # the request is still on the board; only the phone stays quiet
    notify.set_prefs({"needs": True})
    permissions.push_request(3, "shop--api--s1", "Bash: ls")
    assert len(ntfy) == 1 and len(web) == 1
