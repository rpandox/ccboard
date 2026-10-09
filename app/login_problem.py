"""A login the board has been told is no good: kv `login_problem`, learned from failure signals only.

Claude Code and Codex refresh their own access tokens with their refresh tokens whenever they run, and the board does nothing there. A saved
login that sits unused for a long time can outlive its refresh token: the first request after switching to it fails with an authentication
error and Claude Code asks to log in. The board never reads a credentials file to find that out (it copies them as opaque bytes and never
parses a token or an expiry out of them). It learns it the way the person would: a session reports an authentication failure (the
StopFailure hook), and so a flag is raised until a later sign of life from that account clears it:

  raise_   the StopFailure hook: kv login_problem = {at, agent, account, session, message[:300]}; for Codex (no such hook) the rollout
           Tailer, from an error item with no token usage after it that codex_auth_failure reads as a dead login (agents/codex_rollout.py)
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
EVENT_KIND = "auth"            # the kind of the LoginProblem event a raise writes (hooks: Claude; codex_rollout: Codex)
SWITCH_WINDOW = 600            # a failure this soon after a switch is probably the switched-to login: "Switch back" is offered

# What an expired or revoked login looks like in a StopFailure: the error type (`authentication_failed` in the live payload) and the text
# ("Invalid API key · Please run /login", "OAuth token has expired", "API Error: 401 ...", "authentication_error").
AUTH_RE = re.compile(r"authentication[_ ]?(?:error|failed|failure)|\b401\b|oauth token|not logged in|/login\b|invalid api key|"
                     r"token\b.{0,60}?\b(?:expired|revoked)", re.IGNORECASE | re.DOTALL)


def is_auth_failure(*texts) -> bool:
    """Does the error type or the error text of a failed turn say the login is no good? Case-insensitive; pure."""
    return any(isinstance(t, str) and t and AUTH_RE.search(t) for t in texts)


# Codex has no StopFailure hook: its failures are error items in a rollout's tail (codex_rollout.parse_tail) or the `turn.failed` / `error`
# events of `codex exec --json`. Besides AUTH_RE's words, a Codex login that is no good says "401 Unauthorized" (an HTTP status line), that
# its refresh token expired, was revoked or was already used and to "log out and sign in again", or names `codex login`. The exact text a
# dead login gives was NOT captured on the box (codex-cli 0.161.0; see README, Login expiry): the words come from Codex's source and docs.
# A usage limit ("You've hit your usage limit"), a full context window and a network error ("stream disconnected", "timeout") never match,
# nor does the 0.161.0 TUI's "account/read failed during TUI bootstrap" (seen on the box, a slow network, not a login).
CODEX_AUTH_RE = re.compile(r"\bunauthori[sz]ed\b|refresh[_ ]token|sign in again|log ?in again|\bcodex login\b|not signed in|"
                           r"invalid[_ ](?:grant|token)|token[_ ](?:expired|revoked|invalidated)", re.IGNORECASE)


def codex_auth_failure(*texts) -> bool:
    """Does a Codex error text (a rollout error item, a `turn.failed` error, an exec `error` event) or its error kind say the login is no good?
    AUTH_RE plus CODEX_AUTH_RE, case-insensitive; pure. False for a rate limit, a context overflow or a network error."""
    return any(isinstance(t, str) and t and (AUTH_RE.search(t) or CODEX_AUTH_RE.search(t)) for t in texts)


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
