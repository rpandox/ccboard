"""Tailscale identity-header auth. Fail closed everywhere."""
from __future__ import annotations

from email.header import decode_header

LOGIN_HEADER = "tailscale-user-login"
CSRF_HEADER = "x-ccboard"


def decode_login(raw: str | None) -> str | None:
    """Header value -> login name, or None when missing, blank or undecodable."""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("=?"):  # RFC 2047 encoded (non-ASCII login)
        try:
            parts = decode_header(raw)
            raw = "".join(
                p.decode(enc or "utf-8") if isinstance(p, bytes) else p for p, enc in parts
            ).strip()
        except Exception:
            return None
        if not raw:
            return None
    return raw


def normalize(user: str) -> str:
    # Lower-case only ASCII: Unicode case folding can map distinct logins together.
    return user.lower() if user.isascii() else user


def identify(headers, settings) -> str | None:
    """The allowed user for this request, or None (=> 403)."""
    if settings.dev_bypass_user:
        return settings.dev_bypass_user
    user = decode_login(headers.get(LOGIN_HEADER))
    if user is None or not settings.allowed_users:
        return None
    return user if normalize(user) in settings.allowed_users else None


def csrf_ok(method: str, headers) -> bool:
    """Non-GET requests must carry X-CCBoard: 1. A cross-site page cannot add it
    without a CORS preflight, and the board never answers preflights."""
    if method in ("GET", "HEAD"):
        return True
    return headers.get(CSRF_HEADER) == "1"
