import json

import pytest

from app import notify
from app.config import settings


@pytest.fixture
def capture(monkeypatch):
    sent = []

    class R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=5):
        sent.append((req.full_url, json.loads(req.data)))
        return R()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(settings, "ntfy_url", "http://127.0.0.1:2586")
    monkeypatch.setattr(settings, "ntfy_topic", "ccboard")
    monkeypatch.setattr(settings, "ntfy_public_url", "https://box.ts.net:8444")
    monkeypatch.setattr(settings, "public_url", "https://box.ts.net:8443")
    notify._last.clear()
    return sent


def test_session_push_shape_and_throttle(capture):
    assert notify.subscribe_url() == "https://box.ts.net:8444/ccboard"
    assert notify.notify_session("shop--api--s1", "waiting", "Claude needs permission to run npm test", "permission_prompt")
    url, body = capture[-1]
    assert url == "http://127.0.0.1:2586/" and body["topic"] == "ccboard" and body["priority"] == 4
    assert body["title"] == "shop / api · s1: needs you" and "npm test" in body["message"]
    assert body["click"] == "https://box.ts.net:8443/#s=shop--api--s1"
    acts = {a["label"]: a for a in body["actions"]}
    assert acts["Terminal"]["url"].endswith("/tty/?arg=shop--api--s1")
    assert acts["Ack"]["method"] == "POST" and acts["Ack"]["headers"]["X-CCBoard"] == "1"
    assert not notify.notify_session("shop--api--s1", "waiting", "again", None)      # throttled
    assert notify.notify_session("shop--api--s1", "done", "All tests pass", None)     # new state
    assert not notify.notify_session("shop--api--s1", "working", None, None)          # not an attention state


def test_disabled_without_url(monkeypatch):
    monkeypatch.setattr(settings, "ntfy_url", "")
    assert not notify.enabled() and notify.subscribe_url() is None
    assert not notify.notify_session("a--b--c", "done", "x", None)


def test_hook_triggers_push(client, projects_dir, fake_tmux, capture):
    import subprocess
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)
    H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
    name = client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]
    from app import hooks
    client.post("/api/hook", headers={"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard-Session": name},
                content=json.dumps({"hook_event_name": "StopFailure", "error_type": "rate_limit", "error": "limit"}))
    titles = [b["title"] for _, b in capture]
    assert "Claude rate limited" in titles and any(t.endswith(": error") for t in titles)
    assert client.post("/api/notify/test", headers=H).json()["ok"] is True
    assert capture[-1][1]["title"] == "ccboard test"
