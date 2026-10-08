"""Device tokens for the remote MCP endpoint (issue #13): mint, list, revoke, verify, and the on/off switch.

A token is `ccbmcp_` + 256 random bits (url-safe base64). It is returned once, by mint(), and never stored: the kv row `mcp_tokens` holds
its SHA-256 digest with {id, name, scopes, created_at, last_used_at, last_user, last_tool, expires_at}. verify() hashes what a request
carries and compares it with every stored digest in constant time (hmac.compare_digest, no early exit). A revoked token is gone from
the row, so it fails on its very next request; an expired one stays listed (so Settings can say so) and fails too.

Nothing here logs or returns a token or a digest: public() is the only shape that leaves the module, and the log lines name the token's
id and name. The switch: kv `mcp_remote` {enabled} once someone used Settings, else CCBOARD_MCP_REMOTE (1 = on; anything else, and
unset, = off). The audit (kv `mcp_audit`) keeps the last AUDIT_MAX tool calls: token id and name, tool, task id, outcome, time, never a
prompt or a result.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import sys
import threading
from datetime import datetime, timedelta, timezone

from . import mcp
from .config import settings

log = logging.getLogger("ccboard")

PREFIX = "ccbmcp_"
MAX_TOKENS = 10
NAME_MAX = 40
DEFAULT_DAYS = 90
MAX_DAYS = 365
AUDIT_MAX = 50
KV_TOKENS = "mcp_tokens"
KV_REMOTE = "mcp_remote"
KV_AUDIT = "mcp_audit"
TOKEN_RE = re.compile(r"^ccbmcp_[A-Za-z0-9_-]{43}$")
NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]+$")

_lock = threading.RLock()
_db = None


class Refused(Exception):
    """A management request the store refuses (bad name, duplicate, too many): the message is for the person, the status for the route."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def set_db(handle) -> None:
    """A DB for a caller without the web app (the app's own DB, app.main.db, wins whenever app.main is loaded)."""
    global _db
    _db = handle


def _store():
    m = sys.modules.get("app.main")
    handle = getattr(m, "db", None) if m is not None else None
    return handle if handle is not None else _db


def _now() -> datetime:          # patched by tests
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


def _rows(handle=None) -> list[dict]:
    db = handle if handle is not None else _store()
    if db is None:
        return []
    rec = db.kv_get(KV_TOKENS)
    v = rec.get("value") if isinstance(rec, dict) else None
    rows = v.get("tokens") if isinstance(v, dict) else None
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("digest"), str)] if isinstance(rows, list) else []


def _save(rows: list[dict]) -> None:
    db = _store()
    if db is None:
        raise Refused("the board's database is not open", 503)
    db.kv_set(KV_TOKENS, {"tokens": rows})


def public(row: dict) -> dict:
    """What Settings may see of a token: never its digest."""
    exp = _parse(row.get("expires_at"))
    return {"id": row.get("id"), "name": row.get("name"), "scopes": list(row.get("scopes") or []), "created_at": row.get("created_at"),
            "last_used_at": row.get("last_used_at"), "last_user": row.get("last_user"), "last_tool": row.get("last_tool"),
            "expires_at": row.get("expires_at"), "expired": bool(exp and exp <= _now())}


# ------------------------------------------------------------------ the switch

def env_default() -> bool:
    return (getattr(settings, "mcp_remote", "") or "").strip() == "1"


def enabled(handle=None) -> bool:
    """On when Settings turned it on; before anyone used Settings, when CCBOARD_MCP_REMOTE=1. Never raises: a dead DB reads as off.
    `handle`: read this DB instead of the app's (the doctor passes the one it was given)."""
    try:
        db = handle if handle is not None else _store()
        rec = db.kv_get(KV_REMOTE) if db is not None else None
    except Exception as e:
        log.warning("mcp remote switch unreadable: %s", e.__class__.__name__)
        return False
    v = rec.get("value") if isinstance(rec, dict) else None
    if isinstance(v, dict) and isinstance(v.get("enabled"), bool):
        return v["enabled"]
    return env_default()


def set_enabled(on: bool) -> bool:
    db = _store()
    if db is None:
        raise Refused("the board's database is not open", 503)
    db.kv_set(KV_REMOTE, {"enabled": bool(on)})
    log.info("mcp remote endpoint switched %s", "on" if on else "off")
    return enabled()


# ------------------------------------------------------------------ management

def listing(handle=None) -> list[dict]:
    with _lock:
        return [public(r) for r in _rows(handle)]


def _clean_name(name) -> str:
    n = " ".join(str(name or "").split())
    if not n or len(n) > NAME_MAX or not NAME_RE.match(n):
        raise Refused(f"name the device: 1 to {NAME_MAX} characters")
    return n


def _clean_scopes(scopes) -> list[str]:
    if scopes is None:
        return list(mcp.DEFAULT_SCOPES)
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise Refused("scopes must be a list of read, tasks, sessions")
    bad = [s for s in scopes if s not in mcp.SCOPES]
    if bad:
        raise Refused(f"unknown scope {bad[0]!r}; use read, tasks or sessions")
    out = [s for s in mcp.SCOPES if s in scopes]
    if not out:
        raise Refused("pick at least one scope")
    return out


def mint(name, scopes=None, expires_days=None) -> tuple[str, dict]:
    """A new token: (the plaintext, shown once; its public row). Refused: a bad or duplicate name, an unknown scope, an expiry outside
    1 to 365 days, or MAX_TOKENS already stored."""
    n = _clean_name(name)
    sc = _clean_scopes(scopes)
    days = DEFAULT_DAYS if expires_days is None else expires_days
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= MAX_DAYS:
        raise Refused(f"expiry must be 1 to {MAX_DAYS} days")
    with _lock:
        rows = _rows()
        if any(str(r.get("name", "")).lower() == n.lower() for r in rows):
            raise Refused(f"a device called {n} already has a token; revoke it first or pick another name", 409)
        if len(rows) >= MAX_TOKENS:
            raise Refused(f"at most {MAX_TOKENS} device tokens; revoke one first", 409)
        token = PREFIX + secrets.token_urlsafe(32)
        now = _now()
        row = {"id": secrets.token_hex(6), "name": n, "digest": digest(token), "scopes": sc, "created_at": _iso(now),
               "last_used_at": None, "last_user": None, "last_tool": None, "expires_at": _iso(now + timedelta(days=days))}
        rows.append(row)
        _save(rows)
    log.info("mcp device token %s (%s) minted, scopes %s, %d days", row["id"], n, ",".join(sc), days)
    return token, public(row)


def revoke(token_id: str) -> bool:
    with _lock:
        rows = _rows()
        keep = [r for r in rows if r.get("id") != token_id]
        if len(keep) == len(rows):
            return False
        gone = next(r for r in rows if r.get("id") == token_id)
        _save(keep)
    log.info("mcp device token %s (%s) revoked", token_id, gone.get("name"))
    return True


# ------------------------------------------------------------------ the request side

def bearer(header) -> str | None:
    """The token of an `Authorization: Bearer <token>` header, or None."""
    if not isinstance(header, str):
        return None
    parts = header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        return None
    return parts[1].strip()


def verify(token: str | None) -> tuple[dict | None, str]:
    """(the stored row, 'ok') for a live token; (None, 'missing' | 'wrong' | 'expired') otherwise. Every stored digest is compared,
    in constant time, whatever matched first."""
    if not token:
        return None, "missing"
    d = digest(token)
    hit = None
    with _lock:
        for r in _rows():
            if hmac.compare_digest(d, str(r.get("digest") or "")) and hit is None:
                hit = r
    if hit is None:
        return None, "wrong"
    exp = _parse(hit.get("expires_at"))
    if exp is None or exp <= _now():
        return None, "expired"
    return hit, "ok"


def touch(token_id: str, user: str | None, tool: str | None = None) -> None:
    """An accepted request: move last_used_at (and who, and the tool when there was one). A token revoked meanwhile is not brought back."""
    with _lock:
        rows = _rows()
        for r in rows:
            if r.get("id") == token_id:
                r["last_used_at"] = _iso(_now())
                r["last_user"] = user
                if tool:
                    r["last_tool"] = tool
                _save(rows)
                return


def audit(row: dict, tool: str, task_id, outcome: str) -> None:
    """One tools/call in the bounded log. Never the arguments' text, never a result."""
    entry = {"at": _iso(_now()), "token_id": row.get("id"), "name": row.get("name"), "tool": tool,
             "task_id": task_id if isinstance(task_id, int) and not isinstance(task_id, bool) else None, "outcome": outcome}
    log.info("mcp %s by token %s (%s): %s%s", tool, entry["token_id"], entry["name"], outcome,
             f" (task {entry['task_id']})" if entry["task_id"] is not None else "")
    db = _store()
    if db is None:
        return
    with _lock:
        rec = db.kv_get(KV_AUDIT)
        v = rec.get("value") if isinstance(rec, dict) else None
        items = v if isinstance(v, list) else []
        items = (items + [entry])[-AUDIT_MAX:]
        db.kv_set(KV_AUDIT, items)


def recent(limit: int = 20) -> list[dict]:
    db = _store()
    if db is None:
        return []
    rec = db.kv_get(KV_AUDIT)
    v = rec.get("value") if isinstance(rec, dict) else None
    return list(reversed(v[-limit:])) if isinstance(v, list) else []
