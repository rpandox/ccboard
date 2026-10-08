import shlex
import subprocess


def git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)


H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


def test_session_launch_builds_claude_argv(lite_client, projects_dir, fake_tmux, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    git_init(projects_dir / "shop" / "api")
    git_init(projects_dir / "shop" / "web")
    r = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H,
                    json={"launcher": "claude", "args": "--permission-mode acceptEdits", "add_dirs": ["shop/web", "shop/api"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["tmux"] == "shop--api--s1" and body["attach_url"] == "/tty/?arg=shop--api--s1"
    name, cwd, env = fake_tmux["created"][0]
    assert cwd == str(projects_dir / "shop" / "api")
    assert env["CCBOARD_SESSION"] == "shop--api--s1" and env["CCBOARD_URL"].startswith("http://127.0.0.1:")
    argv = shlex.split(fake_tmux["sent"][0][1])
    assert argv[:3] == ["claude", "--session-id", body["claude_session_id"]]
    assert "--permission-mode" in argv and "acceptEdits" in argv
    assert argv[-2:] == ["--add-dir", str(projects_dir / "shop" / "web")]  # own repo skipped
    # second session gets s2; explicit names are validated
    assert lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"] == "shop--api--s2"
    assert lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell", "name": "bad name"}).status_code == 400
    assert lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell", "name": "s1"}).status_code == 409
    assert lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "resume", "resume_id": "-x"}).status_code == 400
    assert lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "claude", "args": "'unbalanced"}).status_code == 400
    # kill
    assert lite_client.delete("/api/sessions/shop--api--s1", headers=H).status_code == 200
    assert lite_client.delete("/api/sessions/_ccboard-login", headers=H).status_code == 400
    # state lists the remaining shell session
    st = lite_client.get("/api/state", headers=H).json()
    repos = {r["name"]: r for r in st["projects"][0]["repos"]}
    assert [s["tmux"] for s in repos["api"]["sessions"]] == ["shop--api--s2"]


def test_clone_and_delete_flow(lite_client, projects_dir, fake_tmux):
    r = lite_client.post("/api/projects", headers=H, json={"name": "shop", "url": "https://github.com/o/web.git"})
    assert r.status_code == 201 and r.json()["clone_session"] == "shop--web--clone"
    assert fake_tmux["sent"][0][1].startswith("git -c http.followRedirects=false ")      # the pin that may follow is tests/test_clone_url_guard.py's
    assert fake_tmux["sent"][0][1].endswith(" clone --progress -- https://github.com/o/web.git . && exit")
    st = lite_client.get("/api/state", headers=H).json()
    assert st["projects"][0]["repos"][0]["state"] == "clone-failed"  # fake pane runs the shell, not git
    assert lite_client.post("/api/projects/shop/repos", headers=H, json={"url": "https://github.com/o/web.git"}).status_code == 409
    assert lite_client.post("/api/projects/shop/repos", headers=H, json={"url": "--upload-pack=x"}).status_code == 400
    assert lite_client.post("/api/projects/shop/repos", headers=H, json={"name": "api"}).status_code == 201
    r = lite_client.delete("/api/projects/shop", headers=H)
    assert r.status_code == 200 and r.json()["killed_sessions"] == ["shop--web--clone"]
    assert not (projects_dir / "shop").exists()
    assert fake_tmux["sessions"] == {}


def test_login_flow(lite_client, fake_tmux, monkeypatch):
    from app import claude_auth
    from app.config import settings
    from app.platform import browser_stub
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    assert lite_client.post("/api/claude/login/code", headers=H, json={"code": "abc#def"}).status_code == 409
    assert lite_client.post("/api/claude/login", headers=H).status_code == 200
    assert fake_tmux["sent"][-1] == ("_ccboard-login", "claude auth login")
    assert fake_tmux["sessions"]["_ccboard-login"]["env"]["BROWSER"] == browser_stub()
    fake_tmux["screen"] = "Opening browser...\nIf the browser didn't open, visit: https://platform.claude.com/oauth/authorize?code=1&x=y\nPaste code here if prompted > "
    st = claude_auth.login_state()
    assert st["running"] and st["url"] == "https://platform.claude.com/oauth/authorize?code=1&x=y"
    assert lite_client.post("/api/claude/login/code", headers=H, json={"code": "abc def"}).status_code == 400
    assert lite_client.post("/api/claude/login/code", headers=H, json={"code": "abc\nrm -rf ~"}).status_code == 400
    assert lite_client.post("/api/claude/login/code", headers=H, json={"code": "abc123#state_1"}).status_code == 200
    assert fake_tmux["sent"][-1] == ("_ccboard-login", "abc123#state_1")


def test_send_keys_endpoint_and_term_page(lite_client, projects_dir, fake_tmux, monkeypatch):
    from app import tmux as t
    calls = []
    monkeypatch.setattr(t, "run", lambda *a, **k: calls.append(a if k.get("input") is None else a + (k["input"],)))
    monkeypatch.setattr(t.time, "sleep", lambda *_: None)
    git_init(projects_dir / "shop" / "api")
    name = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]
    assert lite_client.post(f"/api/sessions/{name}/keys", headers=H, json={"keys": ["Escape", "C-c"]}).status_code == 200
    assert calls[-1] == ("send-keys", "-t", f"={name}:", "Escape", "C-c")
    assert lite_client.post(f"/api/sessions/{name}/keys", headers=H, json={"keys": ["C-d"]}).status_code == 400
    assert lite_client.post(f"/api/sessions/{name}/keys", headers=H, json={"text": "yes", "enter": True}).status_code == 200
    assert calls[-2] == ("send-keys", "-t", f"={name}:", "-l", "--", "yes") and calls[-1][-1] == "Enter"
    assert lite_client.post(f"/api/sessions/{name}/keys", headers=H, json={"text": "rm\x03"}).status_code == 400
    # multi-line text (the composer's Shift+Enter) is one bracketed paste, CRLF normalised, then Enter: newlines never submit early
    assert lite_client.post(f"/api/sessions/{name}/keys", headers=H, json={"text": "first line\r\nsecond\n\tthird", "enter": True}).status_code == 200
    assert calls[-3] == ("load-buffer", "-b", "ccboard", "-", "first line\nsecond\n\tthird")
    assert calls[-2] == ("paste-buffer", "-p", "-d", "-b", "ccboard", "-t", f"={name}:")
    assert calls[-1] == ("send-keys", "-t", f"={name}:", "Enter")
    assert lite_client.post(f"/api/sessions/{name}/keys", headers=H, json={"text": "a\nb", "enter": False}).status_code == 200
    assert calls[-1][0] == "paste-buffer", "without enter the paste is left in the input"
    assert lite_client.post(f"/api/sessions/{name}/keys", headers=H, json={"text": "x" * 8001}).status_code == 400
    assert lite_client.post(f"/api/sessions/{name}/keys", headers=H, json={"text": "x" * 8000}).status_code == 200
    assert lite_client.post(f"/api/sessions/{name}/keys", headers=H, json={"text": "a\x1b[Ab"}).status_code == 400, "other control bytes stay out"
    assert lite_client.post("/api/sessions/nope--x--y/keys", headers=H, json={"keys": ["Enter"]}).status_code == 404
    assert lite_client.get(f"/term/{name}", headers=H).status_code == 200
    assert lite_client.get("/term/..%2Fetc", headers=H).status_code in (400, 404)


def test_stream_once(lite_client, projects_dir, fake_tmux, monkeypatch):
    from app import tmux as t
    git_init(projects_dir / "shop" / "api")
    name = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]
    fake_tmux["screen"] = "$ npm test\n\n12 passing\n\n\n"
    with lite_client.stream("GET", "/api/stream?once=1", headers=H) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert "event: lines" in body and '"name": "' + name + '"' in body.replace('"name":"', '"name": "') and "12 passing" in body
    assert "event: tick" in body and name in body.split("event: tick")[1]


def test_devcontainer_and_bypass_rules(lite_client, projects_dir, fake_tmux, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    git_init(projects_dir / "shop" / "api")
    git_init(projects_dir / "shop" / "web")
    (projects_dir / "shop" / "web" / ".devcontainer").mkdir()
    (projects_dir / "shop" / "web" / ".devcontainer" / "devcontainer.json").write_text("{}")
    st = lite_client.get("/api/state", headers=H).json()
    repos = {r["name"]: r for r in st["projects"][0]["repos"]}
    assert repos["web"]["devcontainer"] is True and repos["api"]["devcontainer"] is False
    # bypass on the host is an explicit choice (v0.4.13): every spelling is accepted and lands in the command
    for i, (body, flag) in enumerate((({"launcher": "claude", "bypass": True, "name": "b1"}, "--dangerously-skip-permissions"),
                                      ({"launcher": "claude", "args": "--dangerously-skip-permissions", "name": "b2"}, "--dangerously-skip-permissions"),
                                      ({"launcher": "claude", "args": "--permission-mode=bypassPermissions", "name": "b3"}, "--permission-mode=bypassPermissions"),
                                      ({"launcher": "claude", "permission_mode": "bypassPermissions", "name": "b4"}, "--permission-mode bypassPermissions"))):
        r = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json=body)
        assert r.status_code == 201 and flag in r.json()["cmd"], (body, r.text)
    # settings overrides stay out of extra args everywhere
    assert lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "claude", "args": "--settings /tmp/x.json"}).status_code == 400
    assert lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "claude", "devcontainer": True}).status_code == 400  # no devcontainer there
    assert lite_client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": "t", "prompt": "p", "args": "--dangerously-skip-permissions"}).status_code == 400
    # inside the devcontainer it is allowed and wrapped in devcontainer up/exec
    r = lite_client.post("/api/projects/shop/repos/web/sessions", headers=H, json={"launcher": "claude", "devcontainer": True, "bypass": True, "args": "--model opus"})
    assert r.status_code == 201, r.text
    cmd = r.json()["cmd"]
    wf = str(projects_dir / "shop" / "web")
    assert cmd.startswith(f"devcontainer up --workspace-folder {wf} && devcontainer exec --workspace-folder {wf} -- claude --session-id ")
    assert "--dangerously-skip-permissions --model opus" in cmd
    r = lite_client.post("/api/projects/shop/repos/web/sessions", headers=H, json={"launcher": "shell", "devcontainer": True}).json()
    assert r["cmd"].endswith("-- bash -l")
