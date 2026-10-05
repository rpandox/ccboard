"""Web Push (VAPID) for the installed PWA. Keys live in the data dir; subscriptions in SQLite."""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

from .config import settings

log = logging.getLogger("ccboard.push")
_public_key: str | None = None


def key_path() -> Path:
    return settings.data_dir / "vapid.pem"


def ensure_keys() -> str:
    """Create the VAPID key pair on first use (0600). Returns the applicationServerKey (URL-safe base64)."""
    global _public_key
    if _public_key:
        return _public_key
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid
    p = key_path()
    if p.exists():
        v = Vapid.from_file(str(p))
    else:
        v = Vapid()
        v.generate_keys()
        pem = v.private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption())
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
    raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    _public_key = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return _public_key


PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com", ".push.services.mozilla.com",
              "web.push.apple.com", ".push.apple.com", ".notify.windows.com", "push.samsungosp.com")


def allowed_endpoint(url: str) -> bool:
    """Only real browser push services: the board POSTs to this URL on every event."""
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return url.startswith("https://") and bool(host) and any(host == h or (h.startswith(".") and host.endswith(h)) for h in PUSH_HOSTS)


def valid_subscription(sub) -> bool:
    return (isinstance(sub, dict) and isinstance(sub.get("endpoint"), str) and allowed_endpoint(sub["endpoint"])
            and isinstance(sub.get("keys"), dict) and isinstance(sub["keys"].get("p256dh"), str)
            and isinstance(sub["keys"].get("auth"), str))


def vapid_claims() -> dict:
    """VAPID `sub`: py-vapid insists on a mailto: address and Apple's push service rejects the JWT (403 BadJwtToken)
    unless that address is well-formed with a real-looking domain; 'ccboard@localhost' is not. Use the board's host."""
    from urllib.parse import urlsplit
    host = ""
    try:
        host = (urlsplit(settings.public_url or "").hostname or "").strip(".")
    except ValueError:
        host = ""
    if "." not in host:
        host = "example.com"
    return {"sub": f"mailto:ccboard@{host}"}


def payload_for(title: str, body: str, url: str = "/", tag: str | None = None, extra: dict | None = None,
                actions: list | None = None, renotify: bool | None = None) -> dict:
    """The JSON the service worker's push handler reads: {title, body, url, tag} always, and when the notice has them renotify (only with
    a tag: the browser refuses it otherwise), actions ({action, title}, the SW shows two), agent, state, tmux, perm_id, badge, ts."""
    out = {"title": title[:200], "body": body[:1000], "url": url, "tag": tag, **(extra or {})}
    acts = actions if actions is not None else out.get("actions")
    if isinstance(acts, list):
        out["actions"] = [{"action": str(a.get("action") or "")[:16], "title": str(a.get("title") or "")[:24]} for a in acts if isinstance(a, dict)][:3]
    if renotify is not None:
        out["renotify"] = bool(renotify and tag)
    return out


def send_all(db, title: str, body: str, url: str = "/", tag: str | None = None, extra: dict | None = None,
             actions: list | None = None, renotify: bool | None = None) -> int:
    """Push to every stored subscription; drops the ones the push service reports gone. Returns sent count. One tag per session, so a
    session's notices replace each other on the phone (renotify makes the replacement buzz again)."""
    subs = db.push_subs()
    if not subs:
        return 0
    from pywebpush import WebPushException, webpush
    payload = json.dumps(payload_for(title, body, url, tag, extra, actions, renotify))
    sent = 0
    for row in subs:
        try:
            webpush(subscription_info=row["sub"], data=payload, vapid_private_key=str(key_path()),
                    vapid_claims=vapid_claims(), ttl=3600)
            sent += 1
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                db.push_sub_del(row["endpoint"])
                log.info("dropped dead push subscription (%s)", status)
            else:
                log.warning("web push failed (%s): %s", status, e)
        except Exception as e:
            log.warning("web push error: %s", e)
    return sent
