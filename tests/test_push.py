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


def test_subscribe_send_and_prune(client, projects_dir, monkeypatch):
    from app import main
    H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
    good = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "p", "auth": "a"}}
    dead = {"endpoint": "https://fcm.googleapis.com/fcm/send/dead", "keys": {"p256dh": "p", "auth": "a"}}
    assert client.post("/api/push/subscribe", headers=H, json={"subscription": {"endpoint": "https://attacker.example/x", "keys": {"p256dh": "p", "auth": "a"}}}).status_code == 400
    assert client.post("/api/push/subscribe", headers=H, json={"subscription": {"endpoint": "http://nope"}}).status_code == 400
    assert client.post("/api/push/subscribe", headers=H, json={"subscription": good}).json()["count"] == 1
    assert client.post("/api/push/subscribe", headers=H, json={"subscription": dead}).json()["count"] == 2
    assert client.get("/api/push/vapid", headers=H).json()["key"]

    import pywebpush

    class Resp:
        status_code = 410

    def fake_webpush(subscription_info, data, vapid_private_key, vapid_claims, ttl=0):
        if subscription_info["endpoint"].endswith("/dead"):
            raise pywebpush.WebPushException("gone", response=Resp())
        fake_webpush.sent.append(json.loads(data))
    fake_webpush.sent = []
    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    r = client.post("/api/push/test", headers=H).json()
    assert r["sent"] == 1 and r["subscriptions"] == 1 and fake_webpush.sent[0]["title"] == "ccboard test"
    assert client.request("DELETE", "/api/push/subscribe", headers=H, json={"subscription": good}).json()["count"] == 0


def test_sw_and_manifest_served(client):
    H = {"Tailscale-User-Login": "alice@example.com"}
    r = client.get("/sw.js", headers=H)
    assert r.status_code == 200 and "javascript" in r.headers["content-type"] and "addEventListener('push'" in r.text
    m = client.get("/static/manifest.webmanifest", headers=H)
    assert m.status_code == 200 and json.loads(m.text)["start_url"] == "/"
    assert client.get("/static/icon-192.png", headers=H).headers["content-type"] == "image/png"


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
    monkeypatch.setattr(settings, "public_url", "https://ubu2.example.ts.net:8443/")
    assert push.vapid_claims() == {"sub": "https://ubu2.example.ts.net:8443"}
    monkeypatch.setattr(settings, "public_url", "")
    sub = push.vapid_claims()["sub"]
    assert sub.startswith("mailto:") and "@" in sub and "." in sub.split("@")[1] and "localhost" not in sub
