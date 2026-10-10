"""Nodes epic P3, issue #135: how a paired node gets in, and everything that must stay shut.

1. The closed route table. A node token opens only what `nodes.NODE_ROUTES` lists: the walk goes over every route of the running app (every method) and
   expects a 403 from the middleware for all but the listed ones, so a new /api route is closed to nodes until someone adds it to the table AND to this
   file's EXPECTED_NODE_ROUTES with a refusal test.
2. The token matrix, in both directions: a node token is no hook token, no hub token and no device token; those three are no node token; two of them
   together are refused.
3. The rules on `POST /api/nodes/pair`, the one route that answers with no identity: X-CCBoard, no Origin, no disallowed identity, a small body, a
   rate limit, nothing stored before the code is right.
4. The management routes (codes, lists, rotate, remove) belong to a signed-in person; the legacy hub poller steps aside for a paired address; a
   board with no pair adds nothing.
5. Secrets: no token or code in the database file, the logs, the audit rows or any answer after it was minted.

Temp dirs and fakes only. Pairing across two boards runs through the in-process `two_nodes` fixture (tests/conftest.py); nothing opens a connection.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Mount

from app import nodes

ID = {"Tailscale-User-Login": "alice@example.com"}
H = {**ID, "X-CCBoard": "1"}
UUID = "0b0e7c4c-1a2b-4c3d-8e9f-0123456789ab"
PEER = "p_0123456789abcdef"
FILL = {"agent": "claude", "decision": "allow", "jid": "1", "key": "k", "n:int": "1", "name": "p--r--s", "pid": "1", "project": "p",
        "repo": "r", "rid": "1", "sid": UUID, "tid": "1", "token_id": "t", "peer": PEER}
NO_ID = "no Tailscale identity or not in CCBOARD_ALLOWED_USERS"
NO_CSRF = "missing X-CCBoard header"
CLOSED = "this node token does not open that route"
EXPECTED_NODE_ROUTES = {
    ("GET", "/api/node"): "read",
    ("GET", "/api/node/summary"): "read",
    ("POST", "/api/node/rotate"): nodes.SCOPE_ANY,
    ("POST", "/api/node/unpair"): nodes.SCOPE_ANY,
}
CALLER = {"id": "ts:nCALLER000001", "name": "caller", "url": "https://100.64.0.9"}


def concrete(path: str) -> str:
    return re.sub(r"\{([^}]+)\}", lambda m: FILL[m.group(1)], path)


def live_routes() -> list[tuple[str, str]]:
    from app import main
    out = []
    for r in main.app.routes:
        if isinstance(r, APIRoute):
            out += [(m, r.path) for m in sorted(r.methods - {"HEAD"})]
    return out


def bearer(token: str, **extra) -> dict:
    """What a paired node's server process sends: the token, X-CCBoard and nothing else (a tagged node has no identity)."""
    return {"Authorization": f"Bearer {token}", "X-CCBoard": "1", **extra}


def mint(db, scopes=("read", "tasks"), node=CALLER):
    """A pair on this board (the accepting side) and its token's plaintext, as the pair endpoint would have made them."""
    return nodes.add_incoming(dict(node), list(scopes), db=db)


@pytest.fixture
def board(lite_client):
    from app import main
    return lite_client, main.db


@pytest.fixture
def wide_buckets(monkeypatch):
    big = lambda: nodes._Limiter(10 ** 6, 60.0, 64)
    monkeypatch.setattr(nodes, "node_read_limiter", big())
    monkeypatch.setattr(nodes, "node_write_limiter", big())


def rows(db):
    return db.node_audit_list(1000)


# ---------------------------------------------------------------- 1. the closed route table

def test_the_table_is_exactly_this_files_list():
    """Adding a route for nodes means changing this list on purpose, with its refusal test."""
    assert dict(nodes.NODE_ROUTES) == EXPECTED_NODE_ROUTES


@pytest.mark.parametrize("method,path", sorted(k for k, v in EXPECTED_NODE_ROUTES.items() if v != nodes.SCOPE_ANY))
def test_every_scoped_row_refuses_a_pair_without_that_scope(board, method, path):
    c, db = board
    _, tok = mint(db, ("tasks",))                                         # holds tasks, not read
    r = c.request(method, path, headers=bearer(tok))
    assert r.status_code == 403 and "read scope" in r.json()["error"], (method, path)
    assert any(x["action"] == "scope_refused" and x["target"] == f"{method} {path}" and x["status"] == "refused" for x in rows(db))


def test_a_node_token_opens_only_the_listed_routes(board, wide_buckets):
    """The walk: every route of the app, every method, with a token that holds all four scopes. Only NODE_ROUTES is answered by a handler."""
    c, db = board
    _, tok = mint(db, nodes.SCOPES)
    listed, closed = 0, 0
    for method, path in live_routes():
        if not path.startswith("/api/"):
            continue
        if (method, path) in EXPECTED_NODE_ROUTES:
            listed += 1
            if EXPECTED_NODE_ROUTES[(method, path)] != nodes.SCOPE_ANY:             # rotate and unpair change the pair: their own tests call them
                r = c.request(method, path, headers=bearer(tok))
                assert r.status_code == 200, (method, path)
            continue
        r = c.request(method, concrete(path), headers=bearer(tok), content=b"{}" if method != "GET" else None)
        closed += 1
        assert r.status_code == 403 and r.json()["error"] == CLOSED, (method, path, r.status_code, r.text[:120])
    assert listed == len(EXPECTED_NODE_ROUTES) and closed > 100          # the walk saw the whole app, not a short list


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_a_listed_path_with_an_unlisted_method_is_closed(board, wide_buckets, method):
    c, db = board
    _, tok = mint(db, nodes.SCOPES)
    for path in ("/api/node", "/api/node/summary", "/api/node/rotate", "/api/node/unpair"):
        if (method, path) in EXPECTED_NODE_ROUTES:
            continue
        r = c.request(method, path, headers=bearer(tok))
        assert r.status_code == 403 and r.json()["error"] == CLOSED, (method, path)


@pytest.mark.parametrize("path", ["/api/nope", "/api/node/hello/x", "/api/nodes/discover", "/api/nodes/pair", "/api/nodes/pair-code", "/api/nodes", "/api/nodes/pairs",
                                  "/api/nodes/audit", f"/api/nodes/{PEER}/rotate", f"/api/nodes/{PEER}", "/api/hook", "/api/permission", "/api/deploy/gate",
                                  "/api/state", "/api/tasks", "/api/mcp/tokens", "/api/node/../state", "/api//state"])
def test_a_node_token_is_refused_on_these_paths(board, wide_buckets, path):
    c, db = board
    _, tok = mint(db, nodes.SCOPES)
    for method in ("GET", "POST", "DELETE"):
        r = c.request(method, path, headers=bearer(tok), content=b"{}" if method != "GET" else None)
        assert r.status_code == 403 and r.json()["error"] == CLOSED, (method, path, r.status_code)


def test_a_node_token_is_nothing_outside_api(board, wide_buckets):
    """The node branch is for /api/ only: the page, the static files and the terminal still need an identity."""
    c, db = board
    _, tok = mint(db, nodes.SCOPES)
    for path in ("/", "/static/core.js", "/sw.js", "/term/a--b--c", "/healthz"):
        r = c.get(path, headers=bearer(tok))
        assert r.status_code == (200 if path == "/healthz" else 403), path
        if path != "/healthz":
            assert r.json()["error"] == NO_ID, path


def test_a_node_token_at_mcp_is_a_403_when_mcp_is_on_and_a_404_when_it_is_off(board):
    from app import mcp_tokens
    c, db = board
    _, tok = mint(db, nodes.SCOPES)
    assert c.post("/mcp", headers={**ID, **bearer(tok)}, json={}).status_code == 404            # off: the endpoint does not advertise itself
    mcp_tokens.set_enabled(True)
    r = c.post("/mcp", headers={**ID, **bearer(tok)}, json={})
    assert r.status_code == 403 and "node token" in r.json()["error"]
    r = c.post("/mcp", headers=bearer(tok), json={})                                              # and no identity: still no way in
    assert r.status_code == 403


# ---------------------------------------------------------------- 2. the token matrix

def test_a_good_token_reads_the_card_and_the_summary(board):
    c, db = board
    row, tok = mint(db, ("read",))
    r = c.get("/api/node", headers=bearer(tok))
    assert r.status_code == 200 and r.json()["app"] == "ccboard" and r.headers["cache-control"] == "no-store"
    assert c.get("/api/node/summary", headers=bearer(tok)).status_code == 200
    assert db.node_pair_get(row["peer_id"])["last_used_at"]                                         # observed use, not the time of saving


def test_unknown_malformed_revoked_and_expired_tokens_are_401(board):
    c, db = board
    row, tok = mint(db)
    for bad in (nodes.TOKEN_PREFIX + "A" * 43, nodes.TOKEN_PREFIX + "short", nodes.TOKEN_PREFIX, tok[:-1] + ("A" if tok[-1] != "A" else "B"), tok + "x"):
        r = c.get("/api/node", headers=bearer(bad))
        assert r.status_code == 401 and "WWW-Authenticate" in r.headers, bad
    assert c.get("/api/node", headers=bearer(tok)).status_code == 200
    db.node_pair_update(row["peer_id"], expires_at="2000-01-01T00:00:00Z")
    assert c.get("/api/node", headers=bearer(tok)).status_code == 401, "expired"
    db.node_pair_update(row["peer_id"], expires_at=None)
    assert c.get("/api/node", headers=bearer(tok)).status_code == 200
    assert nodes.revoke(row["peer_id"], db=db)
    assert c.get("/api/node", headers=bearer(tok)).status_code == 401, "revoked: the very next request"


def test_an_unknown_token_is_401_even_on_a_closed_route_and_says_nothing_else(board):
    c, db = board
    for path in ("/api/state", "/api/node", "/api/nope"):
        r = c.get(path, headers=bearer(nodes.TOKEN_PREFIX + "Z" * 43))
        assert r.status_code == 401 and set(r.json()) == {"error"}, path


def test_the_headers_a_node_request_must_and_must_not_carry(board):
    c, db = board
    _, tok = mint(db)
    ok = bearer(tok)
    assert c.get("/api/node", headers=ok).status_code == 200
    no_csrf = {"Authorization": ok["Authorization"]}
    r = c.get("/api/node", headers=no_csrf)
    assert r.status_code == 403 and r.json()["error"] == NO_CSRF, "a GET needs X-CCBoard too"
    assert c.get("/api/node", headers={**ok, "X-CCBoard": "true"}).status_code == 403
    assert c.get("/api/node", headers={**ok, "Origin": "https://evil.example"}).status_code == 403
    assert c.get("/api/node", headers={**ok, "Origin": ""}).status_code == 403, "a present, empty Origin is still an Origin"
    assert c.options("/api/node", headers={**ok, "Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}).status_code == 403


def test_two_authorization_values_are_refused(board):
    c, db = board
    _, tok = mint(db)
    r = c.get("/api/node", headers=[("Authorization", f"Bearer {tok}"), ("Authorization", "Bearer other"), ("X-CCBoard", "1")])
    assert r.status_code == 403 and "one Authorization header" in r.json()["error"]
    r = c.get("/api/node", headers=[("Authorization", "Bearer other"), ("Authorization", f"Bearer {tok}"), ("X-CCBoard", "1")])
    assert r.status_code == 403, "the node token in the second place is found too"


def test_a_node_token_with_any_other_credential_is_refused(board, monkeypatch):
    """Both together are refused, in every pairing: the hook token, the hub token, an identity does not matter."""
    from app import hooks, health
    from app.config import settings
    monkeypatch.setattr(settings, "hub_token", "hub-secret-value")
    c, db = board
    _, tok = mint(db)
    hook = hooks.ensure_token()
    for extra in ({hooks.TOKEN_HEADER: hook}, {hooks.TOKEN_HEADER: "wrong"}, {health.HUB_HEADER: "hub-secret-value"}, {health.HUB_HEADER: "wrong"}):
        r = c.get("/api/node/summary", headers=bearer(tok, **extra))
        assert r.status_code == 403 and "only credential" in r.json()["error"], extra
    assert c.get("/api/node/summary", headers=bearer(tok, **ID)).status_code == 200, "an identity header beside the token is just ignored"


def test_the_hook_token_is_never_a_node_credential(board):
    from app import hooks
    c, db = board
    hook = hooks.ensure_token()
    for h in ({"Authorization": f"Bearer {hook}", "X-CCBoard": "1"}, {"Authorization": f"Bearer {nodes.TOKEN_PREFIX}{hook[:43]}", "X-CCBoard": "1"}):
        r = c.get("/api/node", headers=h)
        assert r.status_code in (401, 403), h                                                       # never the node branch's 200
    r = c.get("/api/node", headers={"Authorization": f"Bearer {hook}", "X-CCBoard": "1"})
    assert r.status_code == 403 and "device tokens open /mcp only" in r.json()["error"]


def test_a_node_token_is_never_a_hook_token(board):
    c, db = board
    _, tok = mint(db, nodes.SCOPES)
    for path, method in (("/api/hook", "POST"), ("/api/permission", "POST"), ("/api/deploy/gate", "POST"), ("/api/state", "GET"), ("/api/tasks", "GET")):
        r = c.request(method, path, headers={"X-CCBoard-Token": tok, "X-CCBoard": "1"}, content=b"{}" if method == "POST" else None)
        assert r.status_code == 403, path
        r = c.request(method, path, headers=bearer(tok), content=b"{}" if method == "POST" else None)
        assert r.status_code == 403 and r.json()["error"] == CLOSED, path


def test_a_node_token_is_never_the_hub_token(board, monkeypatch):
    from app import health
    from app.config import settings
    monkeypatch.setattr(settings, "hub_token", "hub-secret-value")
    c, db = board
    _, tok = mint(db, nodes.SCOPES)
    r = c.get("/api/node/summary", headers={health.HUB_HEADER: tok, "X-CCBoard": "1"})
    assert r.status_code == 403 and r.json()["error"] == "bad hub token"
    # and the hub token still opens that one route and nothing else
    assert c.get("/api/node/summary", headers={health.HUB_HEADER: "hub-secret-value", "X-CCBoard": "1"}).status_code == 200
    for path in ("/api/node", "/api/state", "/api/nodes", "/api/nodes/pairs", "/api/nodes/audit", "/api/node/hello-not", "/api/tasks"):
        assert c.get(path, headers={health.HUB_HEADER: "hub-secret-value", "X-CCBoard": "1"}).status_code == 403, path
    assert c.post("/api/node/rotate", headers={health.HUB_HEADER: "hub-secret-value", "X-CCBoard": "1"}).status_code == 403
    assert c.get("/api/node", headers={"Authorization": "Bearer hub-secret-value", "X-CCBoard": "1"}).status_code == 403


def test_a_device_token_still_opens_mcp_only(board):
    c, db = board
    r = c.get("/api/node", headers={"Authorization": "Bearer ccbmcp_" + "A" * 43, "X-CCBoard": "1", **ID})
    assert r.status_code == 403 and r.json()["error"] == "device tokens open /mcp only"


def test_a_scope_the_pair_lacks_is_a_403_and_a_pair_may_always_manage_itself(board):
    c, db = board
    row, tok = mint(db, ("tasks",))
    r = c.get("/api/node", headers=bearer(tok))
    assert r.status_code == 403 and "read scope" in r.json()["error"]
    r = c.post("/api/node/rotate", headers=bearer(tok))                                          # SCOPE_ANY
    assert r.status_code == 200 and nodes.TOKEN_RE.fullmatch(r.json()["token"])


def test_the_caller_headers_go_to_the_audit_clean_and_short_and_never_decide_access(board):
    c, db = board
    _, tok = mint(db, ("tasks",))
    h = bearer(tok, **{"X-CCBoard-Node": "ts:nWHATEVER", "X-CCBoard-Acting-User": "carol\x01@ex" + "a" * 100})
    assert c.get("/api/state", headers=h).status_code == 403                                      # claiming to be anyone changes nothing
    row = next(x for x in rows(db) if x["action"] == "route_refused")
    assert "acting user carol@ex" in row["detail"] and "\x01" not in row["detail"]
    assert len(row["detail"]) <= nodes.AUDIT_DETAIL_MAX and "a" * 65 not in row["detail"]
    assert row["direction"] == "in" and row["target"] == "GET /api/state"


def test_the_bucket_answers_429_with_retry_after_and_is_per_pair(board, monkeypatch):
    c, db = board
    monkeypatch.setattr(nodes, "node_read_limiter", nodes._Limiter(3, 60.0, 16))
    monkeypatch.setattr(nodes, "node_write_limiter", nodes._Limiter(2, 60.0, 16))
    _, t1 = mint(db)
    _, t2 = mint(db, node={**CALLER, "id": "ts:nCALLER000002"})
    assert [c.get("/api/node", headers=bearer(t1)).status_code for _ in range(3)] == [200, 200, 200]
    r = c.get("/api/node", headers=bearer(t1))
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert c.get("/api/node", headers=bearer(t2)).status_code == 200, "another pair has its own bucket"
    # a closed route spends the bucket too: a hostile token holder cannot probe for free
    r = c.get("/api/state", headers=bearer(t1))
    assert r.status_code == 429
    # writes have their own, smaller bucket
    assert c.post("/api/state", headers=bearer(t2)).status_code == 403
    assert c.post("/api/state", headers=bearer(t2)).status_code == 403
    assert c.post("/api/state", headers=bearer(t2)).status_code == 429


def test_an_unknown_token_does_not_spend_a_bucket_or_write_an_audit_row(board):
    c, db = board
    before = len(rows(db))
    for _ in range(5):
        assert c.get("/api/node", headers=bearer(nodes.TOKEN_PREFIX + "Q" * 43)).status_code == 401
    assert len(rows(db)) == before, "a stranger cannot fill the audit"


def test_a_body_over_256_kb_is_413_and_a_chunked_body_is_411(board):
    c, db = board
    row, tok = mint(db)
    big = b"x" * (256 * 1024 + 1)
    r = c.post("/api/node/rotate", headers=bearer(tok), content=big)
    assert r.status_code == 413
    r = c.post("/api/node/rotate", headers=bearer(tok), content=iter([b"{}"]))                    # no Content-Length: chunked
    assert r.status_code == 411
    r = c.post("/api/node/rotate", headers=bearer(tok), content=b"x" * (256 * 1024))              # exactly the cap is accepted
    assert r.status_code == 200
    assert db.node_pair_get(row["peer_id"])["rotated_at"], "the refused ones rotated nothing; the accepted one did"


# ---------------------------------------------------------------- rotation and removal by the node itself

def test_rotate_gives_a_new_token_once_and_the_old_one_works_for_60_seconds(board, monkeypatch):
    c, db = board
    clock = [1_800_000_000.0]
    monkeypatch.setattr(nodes, "_now", lambda: clock[0])
    row, old = mint(db)
    r = c.post("/api/node/rotate", headers=bearer(old))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    new = r.json()["token"]
    assert nodes.TOKEN_RE.fullmatch(new) and new != old and r.json()["grace_s"] == 60
    assert c.get("/api/node", headers=bearer(new)).status_code == 200, "the new token works at once"
    assert c.get("/api/node", headers=bearer(old)).status_code == 200, "the old one is still good for a minute"
    clock[0] += 59
    assert c.get("/api/node", headers=bearer(old)).status_code == 200
    clock[0] += 2
    assert c.get("/api/node", headers=bearer(old)).status_code == 401, "and then it is not"
    assert c.get("/api/node", headers=bearer(new)).status_code == 200
    assert new not in json.dumps(c.get("/api/nodes", headers=H).json()), "the listing never carries the token"


def test_rotate_with_a_token_that_is_only_good_through_the_grace_is_refused_and_the_current_token_still_rotates(board, monkeypatch):
    c, db = board
    clock = [1_800_000_000.0]
    monkeypatch.setattr(nodes, "_now", lambda: clock[0])
    row, old = mint(db)
    new = c.post("/api/node/rotate", headers=bearer(old)).json()["token"]
    before = dict(db.node_pair_get(row["peer_id"]))
    r = c.post("/api/node/rotate", headers=bearer(old))                                         # the old token still reads, but cannot mint
    assert r.status_code == 401 and "current token" in r.json()["error"] and new not in r.text
    assert dict(db.node_pair_get(row["peer_id"])) == before, "a refused rotation changed nothing"
    assert c.get("/api/node", headers=bearer(new)).status_code == 200, "the current token was not replaced"
    assert c.get("/api/node", headers=bearer(old)).status_code == 200, "the old token still reads during its grace"
    r = c.post("/api/node/rotate", headers=bearer(new))
    assert r.status_code == 200 and r.json()["token"] not in (old, new)


def test_a_node_that_unpairs_cuts_its_pair_at_once(board):
    c, db = board
    row, tok = mint(db)
    r = c.post("/api/node/unpair", headers=bearer(tok))
    assert r.status_code == 200 and r.json() == {"unpaired": True}
    assert c.get("/api/node", headers=bearer(tok)).status_code == 401
    assert db.node_pair_get(row["peer_id"])["revoked_at"]
    assert any(x["action"] == "revoked" for x in rows(db))


def test_the_node_routes_need_a_node_token_even_for_the_owner(board):
    """The owner (identity) gets past the middleware to a handler that has no pair to act on, and says so."""
    c, db = board
    for path in ("/api/node/rotate", "/api/node/unpair"):
        r = c.post(path, headers=H)
        assert r.status_code == 403 and "takes a node token" in r.json()["error"], path
    from app import hooks
    r = c.post("/api/node/rotate", headers={"X-CCBoard-Token": hooks.ensure_token()})
    assert r.status_code == 403, "the hook token is not a node token either"


# ---------------------------------------------------------------- 3. POST /api/nodes/pair

def pair_body(code="ABCDE-12345", **extra):
    return {"code": code, "node": dict(CALLER), **extra}


def test_the_pair_endpoint_needs_x_ccboard_and_refuses_an_origin(board):
    c, db = board
    nodes.create_code(db=db)
    r = c.post("/api/nodes/pair", json=pair_body())
    assert r.status_code == 403 and r.json()["error"] == NO_CSRF
    for bad in ({"X-CCBoard": "true"}, {"X-CCBoard": "0"}):
        assert c.post("/api/nodes/pair", json=pair_body(), headers=bad).status_code == 403
    r = c.post("/api/nodes/pair", json=pair_body(), headers={"X-CCBoard": "1", "Origin": "https://evil.example"})
    assert r.status_code == 403 and "Origin" in r.json()["error"]
    r = c.post("/api/nodes/pair", json=pair_body(), headers={"X-CCBoard": "1", "Origin": ""})
    assert r.status_code == 403
    assert nodes.code_status(db=db)["active"], "none of those touched the code"
    assert not db.node_pairs(include_revoked=True) and all(x["action"] == "code_created" for x in rows(db))


def test_the_pair_endpoint_refuses_a_disallowed_identity_and_allows_none_or_an_allowed_one(board):
    c, db = board
    nodes.create_code(db=db)
    for who in ("mallory@example.com", "", "Alice@example.com.evil", "=?utf-8?b?AAAA?="):
        r = c.post("/api/nodes/pair", json=pair_body(), headers={"X-CCBoard": "1", "Tailscale-User-Login": who})
        assert r.status_code == 403 and r.json()["error"] == NO_ID, who
    assert nodes.code_status(db=db)["active"] and not any(x["action"] == "pair_refused" for x in rows(db)), "refused before the code was looked at"
    # no identity at all (a tagged node) and an allowed one both get as far as the code
    for h in ({"X-CCBoard": "1"}, H):
        r = c.post("/api/nodes/pair", json=pair_body(), headers=h)
        assert r.status_code == 403 and r.json()["reason"] == "wrong", h


def test_the_hook_token_and_a_device_token_are_nothing_at_the_pair_endpoint(board):
    from app import hooks
    c, db = board
    nodes.create_code(db=db)
    r = c.post("/api/nodes/pair", json=pair_body(), headers={"X-CCBoard": "1", "X-CCBoard-Token": hooks.ensure_token()})
    assert r.json()["reason"] == "wrong", "it is a pair request like any other: the hook token buys nothing"
    r = c.post("/api/nodes/pair", json=pair_body(), headers={"X-CCBoard": "1", "Authorization": "Bearer ccbmcp_" + "A" * 43})
    assert r.status_code == 403 and r.json()["error"] == "device tokens open /mcp only"
    _, tok = mint(db)
    r = c.post("/api/nodes/pair", json=pair_body(), headers=bearer(tok))
    assert r.status_code == 403 and r.json()["error"] == CLOSED, "a node token cannot pair a second time"


def test_the_pair_endpoint_caps_the_body_and_reads_only_three_keys(board):
    c, db = board
    nodes.create_code(db=db)
    h = {"X-CCBoard": "1"}
    assert c.post("/api/nodes/pair", content=b"x" * (16 * 1024 + 1), headers=h).status_code == 413
    assert c.post("/api/nodes/pair", content=iter([b"x" * 9000, b"y" * 9000]), headers=h).status_code == 413, "a chunked body is counted as it comes"
    assert c.post("/api/nodes/pair", content=b"not json", headers=h).status_code == 400
    assert c.post("/api/nodes/pair", content=b"[1]", headers=h).status_code == 400
    for extra in ({"permission_mode": "bypassPermissions"}, {"sandbox": "danger-full-access"}, {"scopes": ["permissions"]}, {"token": "x"}):
        r = c.post("/api/nodes/pair", json={**pair_body(), **extra}, headers=h)
        assert r.status_code == 400 and r.json()["reason"] == "bad_request", extra
    r = c.post("/api/nodes/pair", json={"code": "ABCDE-12345", "node": {**CALLER, "permission_mode": "x"}}, headers=h)
    assert r.status_code == 400
    r = c.post("/api/nodes/pair", json=pair_body(reverse={"token": "x", "scopes": ["read"]}), headers=h)
    assert r.status_code == 400
    r = c.post("/api/nodes/pair", json=pair_body(reverse={"token": nodes.TOKEN_PREFIX + "A" * 43, "scopes": ["read"], "permission_mode": "x"}), headers=h)
    assert r.status_code == 400
    assert nodes.code_status(db=db)["active"] and not db.node_pairs(include_revoked=True), "a malformed request spent nothing and made nothing"


def test_a_failed_pair_has_no_side_effect_beyond_the_attempt_count(board):
    c, db = board
    made = nodes.create_code(db=db)
    h = {"X-CCBoard": "1"}
    for i in range(4):
        r = c.post("/api/nodes/pair", json=pair_body("ZZZZZ-ZZZZZ"), headers=h)
        assert r.status_code == 403 and r.json()["reason"] == "wrong"
    assert not db.node_pairs(include_revoked=True) and nodes.code_status(db=db)["active"]
    r = c.post("/api/nodes/pair", json=pair_body("ZZZZZ-ZZZZZ"), headers=h)
    assert r.json()["reason"] == "burned"
    r = c.post("/api/nodes/pair", json=pair_body(made["code"]), headers=h)
    assert r.status_code == 403 and r.json()["reason"] == "none", "five wrong tries burn the code: the sixth, right one fails"
    assert not db.node_pairs(include_revoked=True)


def test_the_pair_endpoint_is_limited_to_20_a_minute_per_source(board):
    c, db = board
    nodes.create_code(db=db)
    h = {"X-CCBoard": "1"}
    seen = [c.post("/api/nodes/pair", json=pair_body(), headers=h) for _ in range(21)]
    assert [r.status_code for r in seen[:20]] == [403] * 20 and not any(r.status_code == 429 for r in seen[:20])
    assert seen[20].status_code == 429 and int(seen[20].headers["retry-after"]) >= 1 and seen[20].json()["reason"] == "rate_limited"
    nodes.pair_limiter.clear()
    assert c.post("/api/nodes/pair", json=pair_body(), headers=h).status_code == 403


def test_x_forwarded_for_is_not_believed_from_a_non_loopback_connection(board):
    """The limiter keys on the connection; a forged header cannot hand an attacker a fresh bucket per request."""
    c, db = board
    h = {"X-CCBoard": "1"}
    codes = [c.post("/api/nodes/pair", json=pair_body(), headers={**h, "X-Forwarded-For": f"100.64.0.{i}"}).status_code for i in range(22)]
    assert codes.count(429) >= 1


# ---------------------------------------------------------------- 4. management routes

def test_codes_lists_and_removal_belong_to_a_signed_in_person(board):
    from app import hooks
    c, db = board
    row, tok = mint(db)
    hook = {"X-CCBoard-Token": hooks.ensure_token()}
    calls = [("POST", "/api/nodes/pair-code", {"scopes": ["read"]}), ("DELETE", "/api/nodes/pair-code", None), ("GET", "/api/nodes", None),
             ("GET", "/api/nodes/pairs", None), ("GET", "/api/nodes/audit", None), ("POST", f"/api/nodes/{row['peer_id']}/rotate", None),
             ("DELETE", f"/api/nodes/{row['peer_id']}", None), ("POST", "/api/nodes", {"url": "https://100.64.0.2", "code": "ABCDE-12345"})]
    for method, path, body in calls:
        r = c.request(method, path, headers=hook, json=body)
        assert r.status_code == 403 and "Settings > Nodes" in r.json()["error"], (method, path, r.status_code)
        r = c.request(method, path, headers=bearer(tok), json=body)
        assert r.status_code == 403 and r.json()["error"] == CLOSED, (method, path)
        r = c.request(method, path, headers=ID, json=body)
        assert r.status_code == 403 and r.json()["error"] == NO_CSRF if method != "GET" else r.status_code == 200, (method, path)
    assert c.get("/api/node", headers=bearer(tok)).status_code == 200, "none of that touched the pair"


def test_make_and_cancel_a_code(board):
    c, db = board
    r = c.post("/api/nodes/pair-code", headers=H, json={})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    j = r.json()
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}", j["code"]) and j["scopes"] == ["read", "tasks"] and j["minutes"] == 10 and j["expires_at"]
    first = j["code"]
    j2 = c.post("/api/nodes/pair-code", headers=H, json={"scopes": ["permissions", "read"], "minutes": 30}).json()
    assert j2["scopes"] == ["read", "permissions"] and j2["code"] != first
    assert nodes.code_status(db=db)["scopes"] == ["read", "permissions"], "one active code: the new one replaced the old"
    assert c.delete("/api/nodes/pair-code", headers=H).json() == {"cancelled": True}
    assert c.delete("/api/nodes/pair-code", headers=H).json() == {"cancelled": False}
    assert not nodes.code_status(db=db)["active"]
    names = [x["action"] for x in rows(db)]
    assert names.count("code_created") == 2 and "code_cancelled" in names
    assert first not in json.dumps(rows(db)) and j2["code"] not in json.dumps(rows(db))


@pytest.mark.parametrize("body,status", [({"scopes": []}, 400), ({"scopes": ["root"]}, 400), ({"scopes": "read"}, 422), ({"minutes": 0}, 400), ({"minutes": 31}, 400),
                                         ({"minutes": "ten"}, 422), ({"permission_mode": "bypassPermissions"}, 422), ({"sandbox": "danger-full-access"}, 422),
                                         ({"scopes": ["read"], "extra": 1}, 422)])
def test_a_bad_code_request_is_refused_and_makes_no_code(board, body, status):
    c, db = board
    assert c.post("/api/nodes/pair-code", headers=H, json=body).status_code == status
    assert not nodes.code_status(db=db)["active"]


def test_a_single_board_with_no_pair_adds_nothing(board):
    c, db = board
    before = set(c.get("/api/state", headers=H).json())
    r = c.get("/api/nodes", headers=H)
    assert r.status_code == 200 and r.json()["nodes"] == [] and r.json()["pairs"] == [] and r.json()["at"]
    assert c.get("/api/nodes/pairs", headers=H).json()["pairs"] == []
    assert c.get("/api/nodes/audit", headers=H).json()["rows"] == []
    assert c.get("/api/node", headers=H).status_code == 200
    assert db.kv_get(nodes.KV_PEERS) is None and not db.node_pairs(include_revoked=True) and db.node_audit_list(5) == [], "reading wrote nothing"
    after = set(c.get("/api/state", headers=H).json())
    assert after == before and not {k for k in after if re.search(r"pair|peer|token|audit", k)}
    assert not (Path(nodes.tokens_path())).exists(), "no token file until a token is received"


def test_the_lists_never_hold_a_token_or_a_digest(board):
    c, db = board
    row, tok = mint(db)
    digest = db.node_pair_get(row["peer_id"])["token_sha256"]
    for path in ("/api/nodes", "/api/nodes/pairs", "/api/nodes/audit?limit=500"):
        text = c.get(path, headers=H).text
        assert tok not in text and digest not in text and "sha256" not in text and "prev_" not in text, path
    pairs = c.get("/api/nodes/pairs", headers=H).json()["pairs"]
    assert [p["peer_id"] for p in pairs] == [row["peer_id"]] and pairs[0]["direction"] == "in" and pairs[0]["scopes"] == ["read", "tasks"]
    assert c.get("/api/nodes/audit?limit=0", headers=H).status_code == 200


def test_remove_revokes_an_incoming_pair_and_404s_an_unknown_one(board):
    c, db = board
    row, tok = mint(db)
    assert c.delete("/api/nodes/p_ffffffffffffffff", headers=H).status_code == 404
    assert c.delete("/api/nodes/not-a-peer", headers=H).status_code == 404
    r = c.delete(f"/api/nodes/{row['peer_id']}", headers=H)
    assert r.status_code == 200 and r.json() == {"removed": True, "peer_notified": None}
    assert c.get("/api/node", headers=bearer(tok)).status_code == 401
    assert c.get("/api/nodes/pairs", headers=H).json()["pairs"] == [], "a revoked pair is no longer listed"
    assert c.delete(f"/api/nodes/{row['peer_id']}", headers=H).status_code == 404
    assert c.post(f"/api/nodes/{row['peer_id']}/rotate", headers=H).status_code == 404


def test_rotate_and_remove_of_a_pair_that_calls_this_board(board):
    c, db = board
    row, tok = mint(db)
    r = c.post(f"/api/nodes/{row['peer_id']}/rotate", headers=H)
    assert r.status_code == 409, "the board that holds a token rotates it"
    assert c.get("/api/node", headers=bearer(tok)).status_code == 200


def test_the_add_route_refuses_what_the_address_rule_refuses(board):
    c, db = board
    for url in ("http://100.64.0.2", "https://10.0.0.1", "https://169.254.169.254", "https://evil.example", "https://100.64.0.2/x", "https://u:p@100.64.0.2", "nope"):
        r = c.post("/api/nodes", headers=H, json={"url": url, "code": "ABCDE-12345"})
        assert r.status_code == 400, url
    r = c.post("/api/nodes", headers=H, json={"url": "https://100.64.0.2", "code": "short"})
    assert r.status_code == 400
    r = c.post("/api/nodes", headers=H, json={"url": "https://100.64.0.2", "code": "ABCDE-12345", "permission_mode": "x"})
    assert r.status_code == 422
    assert db.kv_get(nodes.KV_PEERS) is None and not rows(db) and not db.node_pairs(include_revoked=True), "nothing was stored or sent"


# ---------------------------------------------------------------- legacy rows and the hub poller

def test_the_hub_poller_leaves_a_paired_address_to_the_pair(board):
    from app import health, main
    c, db = board
    fetched = []

    def fetch(url):
        fetched.append(url)
        return {"node": "x", "health": {}, "sessions": 0, "attention": 0}
    listed = health.parse_nodes("node-c=https://node-c.example.ts.net:8443,node-d=https://100.64.0.2/")
    legacy = nodes.import_legacy(listed, db=db)
    assert [r["legacy"] for r in legacy] == [True, True] and [r["scopes"] for r in legacy] == [["read"], ["read"]]
    poller = health.Poller(db, listed, fetch=fetch, paired=main._paired_urls)
    poller.poll_once()
    assert fetched == ["https://node-c.example.ts.net:8443", "https://100.64.0.2"], "both are legacy rows: both are polled with the hub token"
    fetched.clear()
    nodes.put_peer({"direction": "out", "name": "node-d", "url": "https://100.64.0.2", "scopes": ["read", "tasks"], "legacy": False,
                    "peer_id": next(r["peer_id"] for r in legacy if r["name"] == "node-d")}, db=db)
    out = poller.poll_once()
    assert fetched == ["https://node-c.example.ts.net:8443"] and [r["name"] for r in out] == ["node-c"], "pairing the same address replaces the legacy row"


def test_the_legacy_rows_come_with_the_board_and_hold_no_token(board):
    c, db = board
    from app import health
    nodes.import_legacy(health.parse_nodes("node-c=https://node-c.example.ts.net:8443"), db=db)
    listing = c.get("/api/nodes", headers=H).json()["nodes"]
    assert len(listing) == 1 and listing[0]["legacy"] is True and listing[0]["direction"] == "out" and listing[0]["name"] == "node-c"
    r = c.post(f"/api/nodes/{listing[0]['peer_id']}/rotate", headers=H)
    assert r.status_code == 409, "a legacy row has no token to rotate"
    r = c.delete(f"/api/nodes/{listing[0]['peer_id']}", headers=H)
    assert r.status_code == 200 and r.json()["peer_notified"] is None or r.json()["peer_notified"] is False
    assert c.get("/api/nodes", headers=H).json()["nodes"] == []


def test_the_board_settles_its_node_id_at_start(projects_dir, caplog):
    """Regression: the start-up code used the name `nodes` for the CCBOARD_NODES list as well as for the module, so `nodes.node_id()` raised
    UnboundLocalError and was only logged. The first start must write the id file and log no such warning."""
    from fastapi.testclient import TestClient
    from app import main
    caplog.set_level(logging.DEBUG)
    with TestClient(main.app):
        pass
    assert "node id could not be read" not in caplog.text
    assert (Path(main.settings.data_dir) / nodes.ID_FILE).exists()


# ---------------------------------------------------------------- 5. two boards, end to end, and the secret markers

def pair_boards(two_nodes, scopes=("read", "tasks"), both_ways=False):
    """B makes a code, A adds B with it. Returns A's registry row."""
    a, b = two_nodes.a, two_nodes.b
    code = b.post("/api/nodes/pair-code", json={"scopes": list(scopes)}).json()["code"]
    r = a.post("/api/nodes", json={"url": b.url, "code": code, "both_ways": both_ways})
    assert r.status_code == 201, r.text
    return r.json(), code


def test_two_boards_pair_and_the_card_is_read_with_the_token_and_nothing_else(two_nodes, caplog):
    caplog.set_level(logging.DEBUG)
    a, b = two_nodes.a, two_nodes.b
    row, code = pair_boards(two_nodes)
    assert row["direction"] == "out" and row["name"] == "node-b" and row["scopes"] == ["read", "tasks"] and row["legacy"] is False
    with a.enter():
        from app import nodes as n
        client = n.PeerClient(row, db=a.db)
        card = client.get("/api/node")
        assert card.ok and card.json["name"] == "node-b"
        assert client.get("/api/state").status == 403 and client.get("/api/tasks").status == 403
        assert client.get("/api/node/summary").ok
        token = n._load_outgoing(row["peer_id"])
    assert token and nodes.TOKEN_RE.fullmatch(token)
    pairs = b.get("/api/nodes/pairs").json()["pairs"]
    assert len(pairs) == 1 and pairs[0]["name"] == "node-a" and pairs[0]["last_seen"], "B saw the use"
    # the secret markers: not in either database file, any log line, any audit row or any answer
    log_text = caplog.text
    for board in (a, b):
        raw = Path(board.db_path).read_bytes()
        for wal in (Path(str(board.db_path) + "-wal"),):
            if wal.exists():
                raw += wal.read_bytes()
        assert token.encode() not in raw and code.replace("-", "").encode() not in raw and code.encode() not in raw, board.name
        assert token not in json.dumps(board.db.node_audit_list(1000)) and code not in json.dumps(board.db.node_audit_list(1000))
        assert token not in json.dumps(board.get("/api/nodes").json()) and token not in json.dumps(board.get("/api/nodes/audit").json())
        assert token not in board.get("/api/state").text
    assert token not in log_text and code not in log_text and code.replace("-", "") not in log_text
    tokfile = Path(a.data_dir) / nodes.TOKEN_FILE
    assert tokfile.exists() and (tokfile.stat().st_mode & 0o777) == 0o600
    assert not (Path(b.data_dir) / nodes.TOKEN_FILE).exists(), "B holds only a digest"


def test_rotate_from_the_calling_board_and_remove_with_the_other_offline(two_nodes):
    a, b = two_nodes.a, two_nodes.b
    row, _ = pair_boards(two_nodes)
    with a.enter():
        old = nodes._load_outgoing(row["peer_id"])
    r = a.post(f"/api/nodes/{row['peer_id']}/rotate")
    assert r.status_code == 200 and r.json() == {"rotated": True, "grace_s": 60} and old not in r.text
    with a.enter():
        new = nodes._load_outgoing(row["peer_id"])
        assert new and new != old and nodes.PeerClient(row, db=a.db).get("/api/node").ok
    two_nodes.offline.add("node-b")
    r = a.delete(f"/api/nodes/{row['peer_id']}")
    assert r.status_code == 200 and r.json() == {"removed": True, "peer_notified": False}, "removal never fails because the other board is off"
    assert a.get("/api/nodes").json()["nodes"] == []
    with a.enter():
        assert nodes._load_outgoing(row["peer_id"]) is None
    assert b.get("/api/nodes/pairs").json()["pairs"], "B was not told, so it still lists the pair; the owner revokes it there"
    two_nodes.offline.discard("node-b")
    pid = b.get("/api/nodes/pairs").json()["pairs"][0]["peer_id"]
    assert b.delete(f"/api/nodes/{pid}").status_code == 200
    assert b.get("/api/nodes/pairs").json()["pairs"] == []


def test_removal_tells_the_other_board_when_it_is_up(two_nodes):
    a, b = two_nodes.a, two_nodes.b
    row, _ = pair_boards(two_nodes)
    r = a.delete(f"/api/nodes/{row['peer_id']}")
    assert r.json() == {"removed": True, "peer_notified": True}
    assert b.get("/api/nodes/pairs").json()["pairs"] == [], "B revoked the pair when A said it was leaving"


def test_both_ways_gives_the_other_board_a_working_token_with_read_and_tasks(two_nodes):
    a, b = two_nodes.a, two_nodes.b
    row, _ = pair_boards(two_nodes, scopes=("read",), both_ways=True)
    back = [r for r in b.get("/api/nodes").json()["nodes"] if r["name"] == "node-a"]
    assert len(back) == 1 and back[0]["scopes"] == ["read", "tasks"]
    with b.enter():
        card = nodes.PeerClient(back[0], db=b.db).get("/api/node")
    assert card.ok and card.json["name"] == "node-a"


def test_the_identity_header_a_user_owned_server_carries_does_not_change_a_node_call(two_nodes):
    """In user-owned mode a node's server process arrives with the owner's login: the node token still decides, and a scope it lacks stays a 403."""
    a, b = two_nodes.a, two_nodes.b
    two_nodes.identity = "alice@example.com"
    row, _ = pair_boards(two_nodes, scopes=("tasks",))
    with a.enter():
        r = nodes.PeerClient(row, db=a.db).get("/api/node")                                         # needs read, the pair holds tasks only
    assert r.status == 403 and "read scope" in r.json["error"]
    assert any(x["action"] == "scope_refused" for x in b.db.node_audit_list(50))


# ---------------------------------------------------------------- 6. the Doctor's row for pairs

def test_the_pairs_row_skips_with_no_pair_and_ignores_legacy_rows(board):
    from app import doctor, health
    c, db = board
    out = doctor._c_nodes_pairs(db)
    assert out.status == "skip" and "Settings > Nodes" in out.detail
    nodes.import_legacy(health.parse_nodes("node-c=https://node-c.example.ts.net:8443"), db=db)
    assert doctor._c_nodes_pairs(db).status == "skip", "a CCBOARD_NODES entry is not a pair"


def test_the_pairs_row_warns_for_an_unused_pair_an_old_token_and_a_pair_to_repair(board):
    from app import doctor
    c, db = board
    row, _ = mint(db)
    out = doctor._c_nodes_pairs(db)
    assert out.status == "pass" and "1 paired node" in out.detail
    old = "2000-01-01T00:00:00Z"
    db.node_pair_update(row["peer_id"], last_used_at="2999-01-01T00:00:00Z", created_at=old)
    out = doctor._c_nodes_pairs(db)
    assert out.status == "warn" and "180 days" in out.detail and "Rotate token" in out.fix["text"] and "caller" in out.detail
    db.node_pair_update(row["peer_id"], created_at=nodes.iso(nodes._now() - 100 * 86400), rotated_at=None, last_used_at=None)
    out = doctor._c_nodes_pairs(db)
    assert out.status == "warn" and "not been used for 90 days" in out.detail and "Revoke" in out.fix["text"], "made 100 days ago and never used"
    db.node_pair_update(row["peer_id"], last_used_at=nodes.iso(nodes._now() - 3600))
    assert doctor._c_nodes_pairs(db).status == "pass"
    db.node_pair_update(row["peer_id"], rotated_at=old)
    assert doctor._c_nodes_pairs(db).status == "warn", "a rotation that long ago is old too"
    db.node_pair_update(row["peer_id"], rotated_at=nodes.iso(nodes._now()))
    assert doctor._c_nodes_pairs(db).status == "pass", "rotating clears the warning"


def test_the_pairs_row_does_not_judge_the_age_of_a_token_this_board_holds_until_the_row_records_a_rotation(board, monkeypatch):
    """A row for a node this board calls has no `rotated_at` yet, so Rotate token could never clear an age warning: the age is not judged then."""
    from app import doctor
    c, db = board
    out_row = {"peer_id": PEER, "node_id": "ts:nOTHER0000001", "name": "node-b", "url": "https://100.64.0.2", "scopes": ["read"], "direction": "out",
               "created_at": "2000-01-01T00:00:00Z", "last_seen": nodes.iso(nodes._now() - 60), "needs_repair": False, "legacy": False}
    monkeypatch.setattr(nodes, "peers", lambda db=None: [dict(out_row)])
    assert doctor._c_nodes_pairs(db).status == "pass"
    monkeypatch.setattr(nodes, "peers", lambda db=None: [{**out_row, "rotated_at": "2000-02-01T00:00:00Z"}])
    out = doctor._c_nodes_pairs(db)
    assert out.status == "warn" and "180 days" in out.detail and "Rotate token" in out.fix["text"]
    monkeypatch.setattr(nodes, "peers", lambda db=None: [{**out_row, "rotated_at": nodes.iso(nodes._now())}])
    assert doctor._c_nodes_pairs(db).status == "pass"


def test_the_pairs_row_names_a_pair_that_needs_repair(two_nodes):
    from app import doctor
    a = two_nodes.a
    row, _ = pair_boards(two_nodes)
    with a.enter():
        nodes._note(a.db, row["peer_id"], ok=False, error="the peer answered 401", repair=True)
        out = doctor._c_nodes_pairs(a.db)
    assert out.status == "warn" and "node-b" in out.detail and "Remove" in out.fix["text"] and "Add node" in out.fix["text"]
    assert "ccbnode_" not in out.detail + out.fix["text"]


def test_the_pairs_row_is_registered_in_the_box_group():
    from app import doctor
    assert any(c[0] == "nodes-pairs" and c[1] == "box" for c in doctor.CHECKS)


def test_start_reads_ccboard_nodes_into_legacy_rows_and_starts_no_poller_without_a_hub_token(projects_dir, monkeypatch, caplog):
    """CCBOARD_NODES becomes `legacy` registry rows at start (read only, no token); with no hub token nothing polls and the board says so."""
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main.settings, "nodes_raw", "node-c=https://node-c.example.ts.net:8443")
    monkeypatch.setattr(main.settings, "hub_token", "")
    caplog.set_level(logging.INFO)
    started = []
    monkeypatch.setattr(main.health.Poller, "start", lambda self: started.append(self))
    with TestClient(main.app) as c:
        listing = c.get("/api/nodes", headers=H).json()["nodes"]
        assert [(r["name"], r["legacy"], r["direction"], r["scopes"]) for r in listing] == [("node-c", True, "out", ["read"])]
    assert not started, "no hub token: no poller"
    assert "CCBOARD_NODES is set but CCBOARD_HUB_TOKEN is empty" in caplog.text


def test_start_with_no_ccboard_nodes_writes_no_registry_row(projects_dir, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main.settings, "nodes_raw", "")
    with TestClient(main.app) as c:
        assert main.db.kv_get(nodes.KV_PEERS) is None and not main.db.node_pairs(include_revoked=True) and main.db.node_audit_list(5) == []
        assert c.get("/api/nodes", headers=H).json()["nodes"] == []
