"""A login the board has been told is no good: kv `login_problem`, learned from failure signals only.

Claude Code and Codex refresh their own access tokens with their refresh tokens whenever they run, and the board does nothing there. A saved
login that sits unused for a long time can outlive its refresh token: the first request after switching to it fails with an authentication
error and Claude Code asks to log in. The board never reads a credentials file to find that out (it copies them as opaque bytes and never
parses a token or an expiry out of them). It learns it the way the person would: a session reports an authentication failure (the
StopFailure hook), and so a flag is raised until a later sign of life from that account clears it:

  raise_   the StopFailure hook: kv login_problem = {at, agent, account, session, message[:300]}; for Codex (no such hook) the rollout
           Tailer, from an error item with no token usage after it that codex_auth_failure reads as a dead login, or a "■ " error line on the pane (agents/codex_rollout.py, codex_pane.rejected_login)
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


# Codex has no StopFailure hook: its failures are the `error` / `turn.failed` events of `codex exec --json` (and error items of a rollout tail,
# codex_rollout.parse_tail; Codex 0.145 to 0.161 does not write them there, see tests/fixtures/codex_auth/login_states_0161.json).
# OBSERVED (codex-cli 0.161.0, 2026-10-10, tests/fixtures/codex_exec/auth_failed_0161.jsonl): with no usable login `exec` retries ten times
# ("Reconnecting... N/5 (unexpected status 401 Unauthorized: Missing bearer or basic authentication in header ...)") and ends in turn.failed
# with that 401.
# FROM THE SOURCE, not observed (nobody has had a login the server rejects yet): each pattern below names the Codex file it comes from, read at
# openai/codex commit 979011409de0a60b52f179721948e65531d26144 (tag rust-v0.161.0), paths under codex-rs/. The texts are listed in
# tests/fixtures/codex_auth/login_states_0161.json (expired_revoked.from_source).
CODEX_AUTH_PATTERNS = (
    # 401 status line (observed); protocol/src/error.rs UnexpectedResponseError "unexpected status 401 Unauthorized: ..."; and the error kind
    # "unauthorized" (CodexErrorInfo::Unauthorized, which a failed token refresh carries)
    r"\bunauthori[sz]ed\b",
    # login/src/auth/manager.rs:206-210, REFRESH_TOKEN_{EXPIRED,REUSED,INVALIDATED,UNKNOWN}_MESSAGE: "Your access token could not be refreshed
    # because your refresh token has expired|was already used|was revoked. Please log out and sign in again." and, for an invalid_grant or an
    # unknown code, "Your access token could not be refreshed. Please log out and sign in again."
    r"access token could not be refreshed",
    r"refresh token (?:has expired|was (?:already used|revoked))",
    # manager.rs:1687-1689, the server's refresh error codes
    r"refresh_token_(?:expired|reused|invalidated)",
    # cloud-config/src/service.rs:44-46, "Your authentication session could not be refreshed automatically. Please log out and sign in again."
    r"auth(?:entication)? session could not be refreshed",
    # the same advice; "Please try signing in again." is the server's wording for a reused refresh token (login/src/auth/util.rs test);
    # cloud-tasks/src/lib.rs "Not signed in. Please run 'codex login' ..."
    r"log out and sign in again|signing in again|log ?in again|not signed in|\bcodex login\b",
    # protocol/src/error.rs:633 appends ", auth error code: token_expired" to a 401; OAuth error codes
    r"auth error code: token_|invalid[_ ](?:grant|token)|token[_ ](?:expired|revoked|invalidated)",
)
CODEX_AUTH_RE = re.compile("|".join(CODEX_AUTH_PATTERNS), re.IGNORECASE)

# NOT a dead login although it reads like one: manager.rs:211 REFRESH_TOKEN_ACCOUNT_MISMATCH_MESSAGE, "Your access token could not be refreshed
# because you have since logged out or signed in to another account. Please sign in again." A Codex process found auth.json changed under it,
# which is what the board's own account switch does to a process started before it. The live account is fine, so no banner.
CODEX_NOT_A_DEAD_LOGIN_RE = re.compile(r"logged out or signed in to another account", re.IGNORECASE)
# A usage limit ("You've hit your usage limit"), a full context window, a rate limit and a network error ("stream disconnected", "timeout")
# match nothing above, nor does the 0.161.0 TUI's "account/read failed during TUI bootstrap" (seen on the box, a slow network, not a login).
# MCP server logins have their own words (rmcp-client/: "MCP OAuth refresh token was rejected; reauthorization required", "enterprise IdP
# session has no refresh token; sign in again") and are not the Codex login, so the bare words "refresh token" and "sign in again" are not
# patterns.


def codex_auth_failure(*texts) -> bool:
    """Does a Codex error text (a `turn.failed` error, an exec `error` event, a rollout error item) or its error kind say the login is no good?
    AUTH_RE plus CODEX_AUTH_RE, case-insensitive; pure. False for a rate limit, a context overflow, a network error and the account-switch
    mismatch message. The exclusion is checked first, over every text (the message and its error kind arrive together): the mismatch message
    with an `unauthorized` kind is still the board's own account switch (#318)."""
    if any(isinstance(t, str) and CODEX_NOT_A_DEAD_LOGIN_RE.search(t) for t in texts):
        return False
    return any(isinstance(t, str) and t and (AUTH_RE.search(t) or CODEX_AUTH_RE.search(t)) for t in texts)


# What an interactive Codex pane may raise the problem on (codex_pane.rejected_login): a STRICT subset of the patterns above, because a pane
# also holds text the board does not trust (an agent's answer, a diff, a pasted log). Only the refresh-failure sentences of manager.rs:206-210,
# the cloud-config one and a 401's "auth error code: token_..." (error.rs:633), and only on a line the TUI drew as an error ("■ ", tui
# history_cell/notices.rs:331 new_error_event). The account-switch mismatch is left out here as well.
CODEX_PANE_RE = re.compile(r"access token could not be refreshed|authentication session could not be refreshed|auth error code: token_",
                           re.IGNORECASE)


def codex_pane_auth_failure(text) -> bool:
    """Is this error line from a Codex pane (prefix already checked by the caller) a login the server rejected? Strict; pure."""
    return isinstance(text, str) and not CODEX_NOT_A_DEAD_LOGIN_RE.search(text) and bool(CODEX_PANE_RE.search(text))


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
