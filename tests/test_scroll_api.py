"""POST /api/sessions/{name}/scroll, GET /api/sessions/{name}/pane, the viewers/win fields, and the dev-only fake /tty mount.

lite_client + fake_tmux (tests/conftest.py): no tmux, no workers. What the real tmux commands are is tests/test_tmux_clients.py's job;
here the HTTP layer: validation (400/404), shapes, what reaches tmux.scroll, and the headers around /tty/.
"""
import subprocess

import pytest

from app.config import settings

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
IDENT = {"Tailscale-User-Login": "alice@example.com"}
CSP = "default-src 'self'; frame-ancestors 'none'"
PANE_KEYS = {"alt", "in_mode", "scroll_pos", "history", "cols", "rows", "win_cols", "win_rows", "viewers", "cmd"}


@pytest.fixture
def name(lite_client, projects_dir, fake_tmux):
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)
    return lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]


def scroll(client, name, body, headers=H):
    return client.post(f"/api/sessions/{name}/scroll", headers=headers, json=body)


# ---------------------------------------------------------------- POST /scroll: validation

@pytest.mark.parametrize("body", [{}, {"dir": "sideways"}, {"dir": ""}, {"dir": None}, {"dir": 5}, {"dir": ["up"]}, {"dir": "UP"},
                                  {"n": 3}, {"dir": "up", "n": 0}, {"dir": "up", "n": 11}, {"dir": "up", "n": -1},
                                  {"dir": "up", "n": "3"}, {"dir": "up", "n": 1.5}, {"dir": "up", "n": None}, {"dir": "up", "n": True},
                                  {"dir": "up", "n": [1]}])
def test_bad_dir_or_n_is_400_and_never_reaches_tmux(lite_client, name, fake_tmux, body):
    r = scroll(lite_client, name, body)
    assert r.status_code == 400 and "error" in r.json(), (body, r.text)
    assert fake_tmux["scrolled"] == []


def test_missing_body_is_400(lite_client, name):
    assert lite_client.post(f"/api/sessions/{name}/scroll", headers=H).status_code == 400


@pytest.mark.parametrize("bad", ["_ccboard-login", "_ccboard-login-codex", "_ccboard-x", "nope", "a--b", "a--b--c--d", "a--b--"])
def test_names_that_are_not_ccboard_sessions_are_400(lite_client, fake_tmux, bad):
    assert scroll(lite_client, bad, {"dir": "up"}).status_code == 400
    assert lite_client.get(f"/api/sessions/{bad}/pane", headers=H).status_code == 400
    assert fake_tmux["scrolled"] == []


def test_unknown_session_is_404(lite_client, fake_tmux):
    assert scroll(lite_client, "nope--x--y", {"dir": "up"}).status_code == 404
    assert lite_client.get("/api/sessions/nope--x--y/pane", headers=H).status_code == 404
    assert fake_tmux["scrolled"] == []


def test_scroll_needs_identity_and_the_csrf_header(lite_client, name):
    assert scroll(lite_client, name, {"dir": "up"}, headers={"X-CCBoard": "1"}).status_code == 403          # no identity
    assert scroll(lite_client, name, {"dir": "up"}, headers=IDENT).status_code == 403                       # no X-CCBoard
    assert lite_client.get(f"/api/sessions/{name}/pane", headers={}).status_code == 403


# ---------------------------------------------------------------- POST /scroll: shapes

def test_scroll_shape_and_what_reaches_tmux(lite_client, name, fake_tmux):
    r = scroll(lite_client, name, {"dir": "up"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"ok", "mode", "alt", "pos"} and body["ok"] is True
    assert body["mode"] == "copy" and body["alt"] is False and body["pos"] > 0
    assert fake_tmux["scrolled"] == [(name, "up", 1, "shell")], "n defaults to 1; the agent comes from the session's row"
    r = scroll(lite_client, name, {"dir": "exit", "n": 10})
    assert r.json() == {"ok": True, "mode": "normal", "alt": False, "pos": 0}
    assert fake_tmux["scrolled"][-1] == (name, "exit", 10, "shell")


def test_every_direction_and_the_limits_are_accepted(lite_client, name, fake_tmux):
    for d in ("up", "down", "top", "bottom", "exit"):
        assert scroll(lite_client, name, {"dir": d}).status_code == 200
    for n in (1, 10):
        assert scroll(lite_client, name, {"dir": "down", "n": n}).status_code == 200
    assert [s[1] for s in fake_tmux["scrolled"]] == ["up", "down", "top", "bottom", "exit", "down", "down"]


def test_alternate_screen_pane_answers_app_mode(lite_client, name, fake_tmux):
    fake_tmux["pane"][name] = {"alt": True}
    assert scroll(lite_client, name, {"dir": "up", "n": 3}).json() == {"ok": True, "mode": "app", "alt": True, "pos": 0}


def test_agent_defaults_to_claude_for_a_session_the_board_has_no_row_for(lite_client, name, fake_tmux):
    fake_tmux["sessions"]["shop--api--manual"] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%9", "command": "zsh",
                                                  "path": "/", "pid": 1, "env": {}}
    assert scroll(lite_client, "shop--api--manual", {"dir": "down"}).status_code == 200
    assert fake_tmux["scrolled"][-1] == ("shop--api--manual", "down", 1, "claude")


# ---------------------------------------------------------------- GET /pane

def test_pane_shape(lite_client, name, fake_tmux):
    fake_tmux["sessions"][name].update(window_width=120, window_height=40)
    fake_tmux["pane"][name] = {"alt": False, "in_mode": True, "scroll_pos": 48, "history": 900, "cols": 118, "rows": 39, "cmd": "zsh"}
    fake_tmux["clients"] = [{"session": name, "flags": {"attached"}}, {"session": name, "flags": {"attached", "ignore-size"}},
                            {"session": name, "flags": {"attached", "ignore-size", "read-only"}}]
    r = lite_client.get(f"/api/sessions/{name}/pane", headers=H)
    assert r.status_code == 200
    assert r.json() == {"alt": False, "in_mode": True, "scroll_pos": 48, "history": 900, "cols": 118, "rows": 39,
                        "win_cols": 120, "win_rows": 40, "cmd": "zsh", "viewers": {"full": 1, "grid": 1, "ro": 1}}
    assert set(r.json()) == PANE_KEYS


def test_pane_without_clients_has_zero_viewers(lite_client, name):
    d = lite_client.get(f"/api/sessions/{name}/pane", headers=H).json()
    assert set(d) == PANE_KEYS and d["viewers"] == {"full": 0, "grid": 0, "ro": 0} and d["in_mode"] is False


def test_pane_follows_a_scroll(lite_client, name):
    scroll(lite_client, name, {"dir": "up"})
    assert lite_client.get(f"/api/sessions/{name}/pane", headers=H).json()["in_mode"] is True
    scroll(lite_client, name, {"dir": "exit"})
    d = lite_client.get(f"/api/sessions/{name}/pane", headers=H).json()
    assert d["in_mode"] is False and d["scroll_pos"] == 0


# ---------------------------------------------------------------- viewers and win on the session endpoints

def test_session_and_state_carry_viewers_and_win(lite_client, name, fake_tmux):
    fake_tmux["sessions"][name].update(attached=3, window_width=120, window_height=40)
    fake_tmux["clients"] = [{"session": name, "flags": {"attached"}}, {"session": name, "flags": {"attached", "ignore-size"}},
                            {"session": name, "flags": {"attached", "ignore-size", "read-only"}}]
    d = lite_client.get(f"/api/sessions/{name}", headers=H).json()
    assert d["viewers"] == {"full": 1, "grid": 1, "ro": 1} and d["win"] == [120, 40]
    st = lite_client.get("/api/state", headers=H).json()
    s = next(s for p in st["projects"] for r in p["repos"] for s in r["sessions"] if s["tmux"] == name)
    assert s["viewers"] == {"full": 1, "grid": 1, "ro": 1} and s["win"] == [120, 40] and s["attached"] == 3


def test_win_is_null_when_tmux_does_not_say_and_viewers_default_to_zero(lite_client, name):
    d = lite_client.get(f"/api/sessions/{name}", headers=H).json()
    assert d["win"] is None and d["viewers"] == {"full": 0, "grid": 0, "ro": 0}


def test_viewers_fall_back_to_the_attached_count_when_list_clients_fails(lite_client, name, fake_tmux):
    fake_tmux["sessions"][name]["attached"] = 2
    fake_tmux["clients_error"] = True
    d = lite_client.get(f"/api/sessions/{name}", headers=H).json()
    assert d["viewers"] == {"full": 2, "grid": 0, "ro": 0}
    assert lite_client.get("/api/state", headers=H).status_code == 200, "the 3 s poll never depends on list-clients"


def test_keys_endpoint_leaves_copy_mode_except_for_the_scroll_keys(lite_client, name, fake_tmux):
    assert lite_client.post(f"/api/sessions/{name}/keys", headers=H, json={"keys": ["PageUp", "Up", "Escape"]}).status_code == 200
    assert fake_tmux["left_copy"] == [] and fake_tmux["keys"] == [(name, ["PageUp", "Up", "Escape"])]
    assert lite_client.post(f"/api/sessions/{name}/keys", headers=H, json={"keys": ["BTab"]}).status_code == 200
    assert fake_tmux["left_copy"] == [name]
    assert lite_client.post(f"/api/sessions/{name}/keys", headers=H, json={"text": "yes", "enter": True}).status_code == 200
    assert fake_tmux["texts"][-1] == (name, "yes", True) and fake_tmux["left_copy"] == [name, name]
    assert lite_client.post(f"/api/sessions/{name}/keys", headers=H, json={"text": "a\nb", "enter": False}).status_code == 200
    assert fake_tmux["pasted"][-1] == (name, "a\nb", False) and fake_tmux["left_copy"] == [name, name, name]
    assert lite_client.post(f"/api/sessions/{name}/keys", headers=H, json={"keys": ["C-d"]}).status_code == 400
    assert fake_tmux["keys"][-1] == (name, ["BTab"]), "a refused key is not recorded as sent"
    assert lite_client.post(f"/api/sessions/{name}/keys", headers=H, json={"keys": ["C-Home", "C-End", "C-o"]}).status_code == 200


# ---------------------------------------------------------------- the dev fake /tty

@pytest.fixture
def fake_dir(tmp_path, monkeypatch):
    """A stand-in for scripts/dev/fake_tty so these tests do not depend on that directory's contents."""
    from app import main
    d = tmp_path / "fake_tty"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>fake tty</title><script src=\"fake_tty.js\"></script>\n")
    (d / "fake_tty.js").write_text("window.term = {};\n")
    monkeypatch.setattr(main, "DEV_TTY_DIR", d)
    return d


def test_the_fake_tty_is_mounted_at_tty():
    from starlette.routing import Mount
    from app import main
    assert any(isinstance(r, Mount) and r.path == "/tty" for r in main.app.routes)
    assert main.DEV_TTY_DIR.parts[-3:] == ("scripts", "dev", "fake_tty")


def test_without_the_dev_bypass_tty_is_404_and_keeps_the_csp(lite_client, fake_dir):
    assert settings.dev_bypass_user is None
    for path in ("/tty/", "/tty/index.html", "/tty/fake_tty.js", "/tty/nope"):
        r = lite_client.get(path, headers=IDENT)
        assert r.status_code == 404, path
        assert r.headers["content-security-policy"] == CSP, path
        assert "fake tty" not in r.text
    assert lite_client.get("/tty/", headers={}).status_code == 403                  # and without identity nothing is answered


def test_with_the_dev_bypass_the_fake_is_served_and_only_it_goes_without_the_csp(lite_client, fake_dir, monkeypatch):
    monkeypatch.setattr(settings, "dev_bypass_user", "dev")
    r = lite_client.get("/tty/", headers={})                                         # the bypass supplies the identity
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and "fake tty" in r.text
    assert "content-security-policy" not in r.headers
    assert r.headers["x-content-type-options"] == "nosniff", "only the CSP is skipped"
    js = lite_client.get("/tty/fake_tty.js", headers={})
    assert js.status_code == 200 and "javascript" in js.headers["content-type"] and "content-security-policy" not in js.headers
    assert lite_client.get("/tty/missing.js", headers={}).status_code == 404
    for path in ("/", "/static/core.js", "/ttyx/", "/tty-x/fake_tty.js", "/nope", "/term/shop--api--s1", "/api/agents"):
        r = lite_client.get(path, headers={})
        assert r.headers["content-security-policy"] == CSP, path


def test_the_gate_follows_the_setting_per_request(lite_client, fake_dir, monkeypatch):
    monkeypatch.setattr(settings, "dev_bypass_user", "dev")
    assert lite_client.get("/tty/", headers={}).status_code == 200
    monkeypatch.setattr(settings, "dev_bypass_user", None)
    r = lite_client.get("/tty/", headers=IDENT)
    assert r.status_code == 404 and r.headers["content-security-policy"] == CSP


def test_with_the_bypass_but_no_fake_directory_tty_is_404(lite_client, fake_dir, monkeypatch):
    monkeypatch.setattr(settings, "dev_bypass_user", "dev")
    (fake_dir / "index.html").unlink()
    (fake_dir / "fake_tty.js").unlink()
    fake_dir.rmdir()
    assert lite_client.get("/tty/", headers={}).status_code == 404
