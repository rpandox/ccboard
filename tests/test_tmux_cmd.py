import shlex
import subprocess


def git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)


H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


def test_session_launch_builds_claude_argv(client, projects_dir, fake_tmux, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    git_init(projects_dir / "shop" / "api")
    git_init(projects_dir / "shop" / "web")
    r = client.post("/api/projects/shop/repos/api/sessions", headers=H,
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
    assert client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"] == "shop--api--s2"
    assert client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell", "name": "bad name"}).status_code == 400
    assert client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell", "name": "s1"}).status_code == 409
    assert client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "resume", "resume_id": "-x"}).status_code == 400
    assert client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "claude", "args": "'unbalanced"}).status_code == 400
    # kill
    assert client.delete("/api/sessions/shop--api--s1", headers=H).status_code == 200
    assert client.delete("/api/sessions/_ccboard-login", headers=H).status_code == 400
    # state lists the remaining shell session
    st = client.get("/api/state", headers=H).json()
    repos = {r["name"]: r for r in st["projects"][0]["repos"]}
    assert [s["tmux"] for s in repos["api"]["sessions"]] == ["shop--api--s2"]


def test_clone_and_delete_flow(client, projects_dir, fake_tmux):
    r = client.post("/api/projects", headers=H, json={"name": "shop", "url": "https://github.com/o/web.git"})
    assert r.status_code == 201 and r.json()["clone_session"] == "shop--web--clone"
    assert fake_tmux["sent"][0][1] == "git clone --progress -- https://github.com/o/web.git . && exit"
    st = client.get("/api/state", headers=H).json()
    assert st["projects"][0]["repos"][0]["state"] == "clone-failed"  # fake pane runs the shell, not git
    assert client.post("/api/projects/shop/repos", headers=H, json={"url": "https://github.com/o/web.git"}).status_code == 409
    assert client.post("/api/projects/shop/repos", headers=H, json={"url": "--upload-pack=x"}).status_code == 400
    assert client.post("/api/projects/shop/repos", headers=H, json={"name": "api"}).status_code == 201
    r = client.delete("/api/projects/shop", headers=H)
    assert r.status_code == 200 and r.json()["killed_sessions"] == ["shop--web--clone"]
    assert not (projects_dir / "shop").exists()
    assert fake_tmux["sessions"] == {}


def test_login_flow(client, fake_tmux, monkeypatch):
    from app import claude_auth
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    assert client.post("/api/claude/login/code", headers=H, json={"code": "abc#def"}).status_code == 409
    assert client.post("/api/claude/login", headers=H).status_code == 200
    assert fake_tmux["sent"][-1] == ("_ccboard-login", "claude auth login")
    assert fake_tmux["sessions"]["_ccboard-login"]["env"]["BROWSER"] == "/bin/true"
    fake_tmux["screen"] = "Opening browser...\nIf the browser didn't open, visit: https://platform.claude.com/oauth/authorize?code=1&x=y\nPaste code here if prompted > "
    st = claude_auth.login_state()
    assert st["running"] and st["url"] == "https://platform.claude.com/oauth/authorize?code=1&x=y"
    assert client.post("/api/claude/login/code", headers=H, json={"code": "abc def"}).status_code == 400
    assert client.post("/api/claude/login/code", headers=H, json={"code": "abc\nrm -rf ~"}).status_code == 400
    assert client.post("/api/claude/login/code", headers=H, json={"code": "abc123#state_1"}).status_code == 200
    assert fake_tmux["sent"][-1] == ("_ccboard-login", "abc123#state_1")


def test_send_keys_endpoint_and_term_page(client, projects_dir, fake_tmux, monkeypatch):
    from app import tmux as t
    calls = []
    monkeypatch.setattr(t, "run", lambda *a, **k: calls.append(a))
    git_init(projects_dir / "shop" / "api")
    name = client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]
    assert client.post(f"/api/sessions/{name}/keys", headers=H, json={"keys": ["Escape", "C-c"]}).status_code == 200
    assert calls[-1] == ("send-keys", "-t", f"={name}:", "Escape", "C-c")
    assert client.post(f"/api/sessions/{name}/keys", headers=H, json={"keys": ["C-d"]}).status_code == 400
    assert client.post(f"/api/sessions/{name}/keys", headers=H, json={"text": "yes", "enter": True}).status_code == 200
    assert calls[-2] == ("send-keys", "-t", f"={name}:", "-l", "--", "yes") and calls[-1][-1] == "Enter"
    assert client.post(f"/api/sessions/{name}/keys", headers=H, json={"text": "rm\x03"}).status_code == 400
    assert client.post("/api/sessions/nope--x--y/keys", headers=H, json={"keys": ["Enter"]}).status_code == 404
    assert client.get(f"/term/{name}", headers=H).status_code == 200
    assert client.get("/term/..%2Fetc", headers=H).status_code in (400, 404)
