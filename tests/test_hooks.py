import json
import subprocess

from app import hooks


def git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)


H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


def _hook(lite_client, payload, session=None, event=None, token=None, extra=None):
    hdr = {"X-CCBoard-Token": token if token is not None else hooks.ensure_token()}
    if session:
        hdr["X-CCBoard-Session"] = session
    if event:
        hdr["X-CCBoard-Event"] = event
    hdr.update(extra or {})
    return lite_client.post("/api/hook", headers=hdr, content=json.dumps(payload))


def test_token_and_resolution(lite_client, projects_dir, fake_tmux, monkeypatch):
    git_init(projects_dir / "shop" / "api")
    r = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"})
    name = r.json()["tmux"]
    assert lite_client.post("/api/hook", content="{}").status_code == 403                       # no token
    assert _hook(lite_client, {}, token="nope").status_code == 403
    assert _hook(lite_client, {"hook_event_name": "Stop"}).json()["ignored"] == "unresolved"     # nothing to map
    assert _hook(lite_client, {"hook_event_name": "Stop"}, session="x--y--z").json()["ignored"] == "unknown session"
    # cwd fallback maps a single live session in that repo
    out = _hook(lite_client, {"hook_event_name": "UserPromptSubmit", "prompt": "fix it", "cwd": str(projects_dir / "shop" / "api")}).json()
    assert out["session"] == name and out["how"] == "cwd" and out["state"] == "working"
    # env header wins
    out = _hook(lite_client, {"hook_event_name": "SessionStart", "session_id": "11111111-1111-4111-8111-111111111111"}, session=name).json()
    assert out["how"] == "env" and out["state"] == "idle"


def test_state_machine(lite_client, projects_dir, fake_tmux):
    git_init(projects_dir / "shop" / "api")
    name = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]

    def sess():
        st = lite_client.get("/api/state", headers=H).json()
        return st["projects"][0]["repos"][0]["sessions"][0], st

    _hook(lite_client, {"hook_event_name": "SessionStart", "source": "startup", "session_id": "22222222-2222-4222-8222-222222222222"}, session=name)
    s, _ = sess()
    assert s["state"] == "idle" and s["claude_session_id"] == "22222222-2222-4222-8222-222222222222" and not s["needs_attention"]
    _hook(lite_client, {"hook_event_name": "UserPromptSubmit", "prompt": "write tests"}, session=name)
    s, _ = sess()
    assert s["state"] == "working" and s["last_prompt"] == "write tests"
    _hook(lite_client, {"hook_event_name": "Notification", "notification_type": "permission_prompt", "message": "Claude needs permission to run npm test"}, session=name)
    s, _ = sess()
    assert s["state"] == "waiting" and s["needs_attention"] and "permission" in s["last_message"]
    _hook(lite_client, {"hook_event_name": "Notification", "notification_type": "auth_success", "message": "ok"}, session=name)
    s, _ = sess()
    assert s["state"] == "waiting"                                    # informational notification keeps the state
    fake_tmux["screen"] = "╭──────╮\n│ All 12 tests pass. │\n╰──────╯\n? for shortcuts\n"
    _hook(lite_client, {"hook_event_name": "Stop"}, session=name)
    s, _ = sess()
    assert s["state"] == "done" and s["needs_attention"] and "12 tests pass" in s["last_message"]
    assert lite_client.post(f"/api/sessions/{name}/ack", headers=H).status_code == 200
    s, _ = sess()
    assert s["state"] == "done" and not s["needs_attention"]
    _hook(lite_client, {"hook_event_name": "StopFailure", "error_type": "rate_limit", "error": "You have hit your limit"}, session=name)
    s, st = sess()
    assert s["state"] == "errored" and s["needs_attention"] and st["rate_limited"]["value"]["session"] == name
    _hook(lite_client, {"hook_event_name": "SessionEnd", "reason": "logout"}, session=name)
    s, _ = sess()
    assert s["state"] == "ended"


def test_statusline_stats_and_usage(lite_client, projects_dir, fake_tmux):
    git_init(projects_dir / "shop" / "api")
    name = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]
    payload = {"session_id": "33333333-3333-4333-8333-333333333333", "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
               "context_window": {"used_percentage": 41.2, "context_window_size": 200000},
               "cost": {"total_cost_usd": 1.25, "total_lines_added": 10, "total_lines_removed": 2},
               "rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 1738425600}, "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600}}}
    assert _hook(lite_client, payload, session=name, event="statusline").json()["stats"] is True
    st = lite_client.get("/api/state", headers=H).json()
    s = st["projects"][0]["repos"][0]["sessions"][0]
    assert s["stats"]["model"] == "Opus" and s["stats"]["context_pct"] == 41.2 and s["claude_session_id"].startswith("3333")
    assert st["usage"]["value"]["five_hour"]["used_percentage"] == 23.5


def test_token_file_is_private(projects_dir):
    import os
    hooks._token = None
    p = hooks.token_path()
    if p.exists():
        p.unlink()
    t = hooks.ensure_token()
    assert len(t) == 64 and oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert hooks.check_token(t) and not hooks.check_token(t[:-1] + "x") and not hooks.check_token(None)


def test_last_screen_line_skips_chrome_and_prompts(monkeypatch):
    from app import tmux as t
    screens = {
        "shell": "➜ api git:(main) echo hi\nAdded login page and 4 passing tests.\n➜ api git:(main) \n",
        "tui": "╭────────────────╮\n│ I updated login.html and added tests. All green. │\n╰────────────────╯\n╭─────╮\n│ >  │\n╰─────╯\n  ? for shortcuts    ⏵⏵ accept edits on (shift+tab to cycle)\n",
        "empty": "\n\n",
    }
    for key, expect in (("shell", "Added login page and 4 passing tests."), ("tui", "I updated login.html and added tests. All green."), ("empty", None)):
        monkeypatch.setattr(t, "capture", lambda name, lines=200, join=True, escapes=False, _k=key: screens[_k])
        assert hooks._last_screen_line("x--y--z") == expect


def test_malformed_statusline_does_not_500(lite_client, projects_dir, fake_tmux):
    git_init(projects_dir / "shop" / "api")
    name = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]
    r = _hook(lite_client, {"model": "opus", "context_window": "x", "cost": None, "rate_limits": "no"}, session=name, event="statusline")
    assert r.status_code == 200 and r.json().get("stats") is True
    r = _hook(lite_client, {"hook_event_name": "Notification", "message": {"nested": 1}}, session=name)
    assert r.status_code == 200
