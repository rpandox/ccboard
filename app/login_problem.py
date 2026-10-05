"""A login the board has been told is no good: kv `login_problem`, learned from failure signals only.

Claude Code and Codex refresh their own access tokens with their refresh tokens whenever they run, and the board does nothing there. A saved
login that sits unused for a long time can outlive its refresh token: the first request after switching to it fails with an authentication
error and Claude Code asks to log in. The board never reads a credentials file to find that out (it copies them as opaque bytes and never
parses a token or an expiry out of them). It learns it the way the person would: a session reports an authentication failure (the
StopFailure hook), and so a flag is raised until a later sign of life from that account clears it:

  raise_   the StopFailure hook: kv login_problem = {at, agent, account, session, message[:300]}
  clear    a statusline reading or a successful Stop of a session on that account, a login for that account finishing (account_store /
           codex_accounts finalize), or DELETE /api/accounts/problem

The state carries it as `state.accounts.problem` (account_store.decorate: the record plus the account's label and, when the failure follows
a switch made within SWITCH_WINDOW, the account to switch back to). Nothing here touches a file.
"""
from __future__ import annotations

import re

from .db import iso

KV = "login_problem"
MESSAGE_MAX = 300
SWITCH_WINDOW = 600            # a failure this soon after a switch is probably the switched-to login: "Switch back" is offered

# What an expired or revoked login looks like in a StopFailure: the error type (`authentication_failed` in the live payload) and the text
# ("Invalid API key · Please run /login", "OAuth token has expired", "API Error: 401 ...", "authentication_error").
AUTH_RE = re.compile(r"authentication[_ ]?(?:error|failed|failure)|\b401\b|oauth token|not logged in|/login\b|invalid api key|"
                     r"token\b.{0,60}?\b(?:expired|revoked)", re.IGNORECASE | re.DOTALL)


def is_auth_failure(*texts) -> bool:
    """Does the error type or the error text of a failed turn say the login is no good? Case-insensitive; pure."""
    return any(isinstance(t, str) and t and AUTH_RE.search(t) for t in texts)


def _rec(db):
    rec = db.kv_get(KV)
    v = rec.get("value") if isinstance(rec, dict) else None
    return v if isinstance(v, dict) and v.get("agent") else None


def get(db) -> dict | None:
    """The problem as stored ({at, agent, account, session, message}), None when there is none."""
    return _rec(db)


def raise_(db, *, agent: str, account: str | None, session: str | None, message: str | None, now=None) -> dict:
    """Record that `agent`'s login for `account` (None: nobody known) was reported invalid by `session`. Overwrites an earlier problem: the
    newest failure is the one shown."""
    v = {"at": iso(now), "agent": agent, "account": account or None, "session": session or None,
         "message": " ".join(str(message or "").split())[:MESSAGE_MAX]}
    db.kv_set(KV, v)
    return v


def clear(db, agent: str | None = None, account: str | None = None) -> bool:
    """Drop the problem. With `agent` (and `account`) only when it is that agent's (and that account's, or an account nobody could name):
    a sign of life from one account says nothing about another's. With neither it is dropped whatever it is. Returns whether one was dropped."""
    v = _rec(db)
    if v is None:
        return False
    if agent is not None and v.get("agent") != agent:
        return False
    if account is not None and v.get("account") not in (None, account):
        return False
    db.kv_del(KV)
    return True
