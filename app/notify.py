"""Push notifications through a self-hosted ntfy server on the tailnet (loopback from the board)."""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request

from .config import settings

from . import push

log = logging.getLogger("ccboard.notify")
_db = None


def set_db(db) -> None:
    global _db
    _db = db


def web_push(title: str, body: str, url: str, tag: str | None = None) -> int:
    if _db is None:
        return 0
    try:
        return push.send_all(_db, title, body, url, tag)
    except Exception as e:
        log.warning("web push failed: %s", e)
        return 0
THROTTLE_SECONDS = 60
_last: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()

TITLES = {"waiting": "needs you", "done": "done", "errored": "error"}
PRIORITY = {"waiting": 4, "errored": 4, "done": 3}
TAGS = {"waiting": ["bell"], "done": ["white_check_mark"], "errored": ["rotating_light"]}


def enabled() -> bool:
    return bool(settings.ntfy_url and settings.ntfy_topic)


def any_channel() -> bool:
    return enabled() or (_db is not None and bool(_db.push_subs()))


def subscribe_url() -> str | None:
    if not enabled():
        return None
    base = settings.ntfy_public_url or settings.ntfy_url
    return f"{base.rstrip('/')}/{settings.ntfy_topic}"


def publish(title: str, message: str, *, click: str | None = None, actions: list[dict] | None = None,
            priority: int = 3, tags: list[str] | None = None) -> bool:
    """ntfy publish (no-op when ntfy is not configured)."""
    if not enabled():
        return False
    body = {"topic": settings.ntfy_topic, "title": title[:250], "message": message[:3000], "priority": priority}
    if click:
        body["click"] = click
    if actions:
        body["actions"] = actions[:3]
    if tags:
        body["tags"] = tags
    req = urllib.request.Request(settings.ntfy_url.rstrip("/") + "/", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return 200 <= r.status < 300
    except Exception as e:  # never break a hook because ntfy is down
        log.warning("ntfy publish failed: %s", e)
        return False


def notify_session(name: str, state: str, message: str | None, kind: str | None = None) -> bool:
    """Push for a session entering waiting / done / errored, throttled per session+state."""
    if state not in TITLES or not any_channel():
        return False
    now = time.monotonic()
    with _lock:
        prev = _last.get(name)
        if prev and prev[0] == state and now - prev[1] < THROTTLE_SECONDS:
            return False
        _last[name] = (state, now)
    pub = (settings.public_url or "").rstrip("/")
    click = f"{pub}/#s={name}" if pub else None
    actions = []
    if pub:
        actions.append({"action": "view", "label": "Terminal", "url": f"{pub}/term/{name}"})
        actions.append({"action": "http", "label": "Ack", "url": f"{pub}/api/sessions/{name}/ack", "method": "POST",
                        "headers": {"X-CCBoard": "1"}, "clear": True})
    label = f"{name.replace('--', ' / ', 1).replace('--', ' · ')}"
    msg = (message or "").strip() or (kind or state)
    title = f"{label}: {TITLES[state]}"
    web_push(title, msg, f"/#s={name}", tag=name)
    return publish(title, msg, click=click, actions=actions, priority=PRIORITY[state], tags=TAGS[state])


def notify_rate_limit(name: str, message: str | None) -> bool:
    pub = (settings.public_url or "").rstrip("/")
    web_push("Claude rate limited", (message or "rate limit hit") + f" ({name})", "/", tag="rate-limit")
    return publish("Claude rate limited", (message or "rate limit hit") + f" ({name})", click=f"{pub}/" if pub else None,
                   priority=5, tags=["no_entry"])
