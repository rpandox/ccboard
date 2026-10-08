import json
import os

import pytest

from app import push


def test_keys_and_public_format(projects_dir):
    push._public_key = None
    p = push.key_path()
    if p.exists():
        p.unlink()
    key = push.ensure_keys()
    assert oct(os.stat(p).st_mode & 0o777) == "0o600" and len(key) in (86, 87) and "=" not in key and "+" not in key
    push._public_key = None
    assert push.ensure_keys() == key  # reloaded from disk


def test_subscribe_send_and_prune(lite_client, projects_dir, monkeypatch):
    from app import main
    H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
    good = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "p", "auth": "a"}}
    dead = {"endpoint": "https://fcm.googleapis.com/fcm/send/dead", "keys": {"p256dh": "p", "auth": "a"}}
    assert lite_client.post("/api/push/subscribe", headers=H, json={"subscription": {"endpoint": "https://attacker.example/x", "keys": {"p256dh": "p", "auth": "a"}}}).status_code == 400
    assert lite_client.post("/api/push/subscribe", headers=H, json={"subscription": {"endpoint": "http://nope"}}).status_code == 400
    assert lite_client.post("/api/push/subscribe", headers=H, json={"subscription": good}).json()["count"] == 1
    assert lite_client.post("/api/push/subscribe", headers=H, json={"subscription": dead}).json()["count"] == 2
    assert lite_client.get("/api/push/vapid", headers=H).json()["key"]

    import pywebpush

    class Resp:
        status_code = 410

    def fake_webpush(subscription_info, data, vapid_private_key, vapid_claims, ttl=0):
        if subscription_info["endpoint"].endswith("/dead"):
            raise pywebpush.WebPushException("gone", response=Resp())
        fake_webpush.sent.append(json.loads(data))
    fake_webpush.sent = []
    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    r = lite_client.post("/api/push/test", headers=H).json()
    assert r["sent"] == 1 and r["subscriptions"] == 1 and fake_webpush.sent[0]["title"] == "ccboard test"
    assert lite_client.request("DELETE", "/api/push/subscribe", headers=H, json={"subscription": good}).json()["count"] == 0


def test_sw_and_manifest_served(lite_client):
    H = {"Tailscale-User-Login": "alice@example.com"}
    r = lite_client.get("/sw.js", headers=H)
    assert r.status_code == 200 and "javascript" in r.headers["content-type"] and "addEventListener('push'" in r.text
    assert "addEventListener('notificationclick'" in r.text
    for name in ("renotify", "requireInteraction", "timestamp", "actions", "setAppBadge", "postMessage", "X-CCBoard", "/api/permission/", "/ack"):
        assert name in r.text, f"sw.js lost {name}"
    assert "__ASSET_VERSION__" not in r.text and "__SHELL_JSON__" not in r.text
    m = lite_client.get("/static/manifest.webmanifest", headers=H)
    assert m.status_code == 200 and json.loads(m.text)["start_url"] == "/"
    assert lite_client.get("/static/icon-192.png", headers=H).headers["content-type"] == "image/png"


def test_endpoint_allowlist():
    assert push.allowed_endpoint("https://fcm.googleapis.com/fcm/send/abc")
    assert push.allowed_endpoint("https://updates.push.services.mozilla.com/wpush/v2/x")
    assert push.allowed_endpoint("https://web.push.apple.com/QW")
    assert push.allowed_endpoint("https://wns2-bl2p.notify.windows.com/w/?token=x")
    assert not push.allowed_endpoint("https://attacker.example/collect")
    assert not push.allowed_endpoint("http://fcm.googleapis.com/x")
    assert not push.allowed_endpoint("https://fcm.googleapis.com.evil.net/x")
    assert not push.allowed_endpoint("https://127.0.0.1:8000/api/hook")


def test_vapid_subject_is_acceptable_to_apple(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "public_url", "https://box.example.ts.net:8443/")
    assert push.vapid_claims() == {"sub": "mailto:ccboard@box.example.ts.net"}
    from py_vapid import _check_sub
    assert _check_sub(push.vapid_claims()["sub"])
    monkeypatch.setattr(settings, "public_url", "")
    sub = push.vapid_claims()["sub"]
    assert sub.startswith("mailto:") and "@" in sub and "." in sub.split("@")[1] and "localhost" not in sub


def test_payload_keeps_the_old_four_fields_and_adds_the_new_ones():
    old = push.payload_for("t", "b", "/#/s/a--b--c", "a--b--c")
    assert old == {"title": "t", "body": "b", "url": "/#/s/a--b--c", "tag": "a--b--c"}
    acts = [{"action": "allow", "title": "Allow", "junk": 1}, {"action": "deny", "title": "Deny"}, {"action": "terminal", "title": "Terminal"}, {"action": "x", "title": "y"}, "bad"]
    new = push.payload_for("t", "b", "/", "a--b--c", extra={"agent": "claude", "perm_id": 5}, actions=acts, renotify=True)
    assert new["agent"] == "claude" and new["perm_id"] == 5 and new["renotify"] is True
    assert new["actions"] == [{"action": "allow", "title": "Allow"}, {"action": "deny", "title": "Deny"}, {"action": "terminal", "title": "Terminal"}]   # at most three, only the two keys
    assert push.payload_for("t", "b", "/", None, renotify=True)["renotify"] is False                                  # renotify needs a tag
    via_extra = push.payload_for("t", "b", "/", "x", extra={"actions": acts[:2], "renotify": True})
    assert via_extra["actions"] == [{"action": "allow", "title": "Allow"}, {"action": "deny", "title": "Deny"}] and via_extra["renotify"] is True


def test_send_all_sends_the_notice_context_one_tag_per_session(lite_client, projects_dir, monkeypatch):
    from app import main
    import pywebpush
    main.db.push_sub_add({"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "p", "auth": "a"}})
    sent = []
    monkeypatch.setattr(pywebpush, "webpush", lambda subscription_info, data, vapid_private_key, vapid_claims, ttl=0: sent.append(json.loads(data)))
    n = push.send_all(main.db, "◆ shop/api · s1: needs you", "Fix login\n? Bash: npm test", "/#/s/shop--api--s1", "shop--api--s1",
                      extra={"agent": "claude", "state": "waiting", "tmux": "shop--api--s1", "perm_id": 5, "badge": 2, "ts": 1760000000000},
                      actions=[{"action": "allow", "title": "Allow"}, {"action": "deny", "title": "Deny"}], renotify=True)
    assert n == 1 and sent[0]["tag"] == "shop--api--s1" and sent[0]["renotify"] is True and sent[0]["badge"] == 2
    assert [a["action"] for a in sent[0]["actions"]] == ["allow", "deny"] and sent[0]["perm_id"] == 5 and sent[0]["ts"] == 1760000000000


def test_the_settings_test_push_shows_two_buttons_and_names_no_session(lite_client, projects_dir, monkeypatch):
    import pywebpush
    from app import main
    H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
    main.db.push_sub_add({"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "p", "auth": "a"}})
    sent = []
    monkeypatch.setattr(pywebpush, "webpush", lambda subscription_info, data, vapid_private_key, vapid_claims, ttl=0: sent.append(json.loads(data)))
    assert lite_client.post("/api/push/test", headers=H).json()["sent"] == 1
    p = sent[0]
    assert p["title"] == "ccboard test" and p["tag"] == "test" and p["tmux"] == "" and p["perm_id"] is None
    assert len(p["actions"]) == 2 and p["renotify"] is True
