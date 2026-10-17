"""Opt-in browser sessions for direct loopback requests. Restart revokes everything."""
from __future__ import annotations

import hashlib
import ipaddress
import secrets
import threading
import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, Response

from . import hooks
from .auth import csrf_ok
from .config import settings

COOKIE = "ccboard_local"
TOKEN_HEADER = "X-CCBoard-Local-Link"
TTL = 60
_lock = threading.Lock()
_tokens: dict[str, tuple[float, float]] = {}
_sessions: dict[str, dict] = {}
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
           "X-Content-Type-Options": "nosniff",
           "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}


def digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def direct(request, config) -> bool:
    """Serve also connects from 127.0.0.1: peer alone cannot distinguish a local
    browser from a proxy. Exact Host and the absence of proxy headers are essential."""
    if not config.local_signin or not config.allowed_users or not request.client:
        return False
    try:
        if not ipaddress.ip_address(request.client.host).is_loopback:
            return False
    except ValueError:
        return False
    hosts = request.headers.getlist("host")
    if len(hosts) != 1 or hosts[0] not in {f"{h}:{config.port}" for h in ("127.0.0.1", "localhost", "[::1]")}:
        return False
    if any(k == "forwarded" or k.startswith(("x-forwarded-", "tailscale-")) for k in request.headers):
        return False
    origins = request.headers.getlist("origin")
    return not origins or origins == [f"http://{hosts[0]}"]


def _clock() -> tuple[float, float]:
    """Wall time covers sleep; monotonic time prevents rollback extending expiry."""
    return time.time(), time.monotonic()


def _expired(end: tuple[float, float], now: tuple[float, float]) -> bool:
    return end[0] <= now[0] or end[1] <= now[1]


def _prune(now):
    for key, end in list(_tokens.items()):
        if _expired(end, now):
            del _tokens[key]
    for key, rec in list(_sessions.items()):
        if _expired(rec["expires"], now):
            del _sessions[key]


def clear():
    with _lock:
        _tokens.clear()
        _sessions.clear()


def session_user(request, config) -> str | None:
    if not direct(request, config):
        return None
    raw = request.cookies.get(COOKIE, "")
    if not raw or len(raw) > 128:
        return None
    with _lock:
        now = _clock()
        _prune(now)
        rec = _sessions.get(digest(raw))
        if not rec or rec["user"] not in config.allowed_users:
            return None
        rec["last_seen"] = now[0]
        return rec["user"]


def refused():
    return JSONResponse({"error": "local sign-in refused"}, status_code=403, headers=HEADERS)


def page(request: Request):
    if not direct(request, settings):
        return refused()
    return FileResponse(Path(__file__).parent / "static" / "local-signin.html", headers=HEADERS)


def script(request: Request):
    if not direct(request, settings):
        return refused()
    return FileResponse(Path(__file__).parent / "static" / "local-signin.js", media_type="text/javascript", headers=HEADERS)


def mint(request: Request):
    # The sole credential delivery is this header, once, to the local command.
    # No credential enters a response body, URL query, audit row or log.
    if not direct(request, settings) or not hooks.check_token(request.headers.get(hooks.TOKEN_HEADER)):
        return refused()
    raw = secrets.token_urlsafe(32)
    with _lock:
        now = _clock()
        _prune(now)
        if len(_tokens) >= 128:
            return JSONResponse({"error": "too many pending links"}, status_code=429, headers=HEADERS)
        _tokens[digest(raw)] = (now[0] + TTL, now[1] + TTL)
    return Response(status_code=204, headers={**HEADERS, TOKEN_HEADER: raw})


async def redeem(request: Request):
    if not direct(request, settings) or not csrf_ok(request.method, request.headers):
        return refused()
    # Bounded raw body avoids validation errors ever reflecting the token.
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 128:
            return refused()
        body.extend(chunk)
    try:
        token = body.decode("ascii")
    except UnicodeDecodeError:
        return refused()
    user = settings.local_signin_user
    if not user or user not in settings.allowed_users:
        return refused()
    with _lock:
        now = _clock()
        _prune(now)
        if _tokens.pop(digest(token), None) is None:
            return refused()
        raw = secrets.token_urlsafe(32)
        lifetime = settings.local_signin_hours * 3600
        _sessions[digest(raw)] = {"user": user, "created": now[0], "last_seen": now[0],
                                  "expires": (now[0] + lifetime, now[1] + lifetime)}
    resp = Response(status_code=204, headers=HEADERS)
    resp.set_cookie(COOKIE, raw, max_age=settings.local_signin_hours * 3600,
                    httponly=True, samesite="strict", path="/")
    return resp


def person(request):
    return getattr(request.state, "user", None) not in (None, "local-token", "hub")


def sessions(request: Request):
    if not person(request):
        return refused()
    with _lock:
        _prune(_clock())
        rows = [{k: r[k] for k in ("created", "last_seen")} for r in _sessions.values()] if settings.local_signin else []
    return JSONResponse({"enabled": settings.local_signin, "sessions": rows}, headers=HEADERS)


def revoke(request: Request):
    if not person(request) or not csrf_ok(request.method, request.headers):
        return refused()
    clear()  # pending links are revoked too: sign out all cannot be undone by an old link
    resp = JSONResponse({"ok": True}, headers=HEADERS)
    resp.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
    return resp
