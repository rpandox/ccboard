"""Nodes epic P7, issue #140: the allowlisted relay. Table first, everything else generated from it.

1. The table: RELAY rows are complete (scope, strict model, rate class, audit action), their routes exist, and NODE_ROUTES holds exactly the base routes and the rows.
2. A refusal test per row, generated from the table, on the peer: no credential 403, a token without the scope 403, an expired or revoked token 401, an over-size
   body 413, a full bucket 429 with Retry-After (the row's class, the other class untouched), a wrong method 405.
3. The hub side over two real boards (the in-process `two_nodes` fixture, nothing opens a socket): the order of the refusals with NO transport call, the mapping
   of every answer of the peer, the wrapped answer, redirects, sizes, timeouts, the target rule, no chaining, no self-relay.
4. What the peer's read rows show: a task's detail without the prompt, the screen tail (40 lines, control characters out), the agents without the account block.
5. The audit: one `out` row on the hub and one `in` row on the peer for each call, with the acting user, a failed call with its reason, and no token, code or
   prompt body anywhere; the filters of GET /api/nodes/audit.

Temp dirs and fakes only; the node id and the tokens land in the temp data dirs of tests/conftest.py (the real home is guarded by the autouse canary).
"""
from __future__ import annotations

import json
import logging
import re
import socket

import pytest
from pydantic import BaseModel

from app import nodes, nodes_relay as nr

ID = {"Tailscale-User-Login": "alice@example.com"}
H = {**ID, "X-CCBoard": "1"}
NO_ID = "no Tailscale identity or not in CCBOARD_ALLOWED_USERS"
CLOSED = "this node token does not open that route"
FILL = {"tid": "1", "name": "p--r--s"}
CALLER = {"id": "ts:nCALLER000001", "name": "caller", "url": "https://100.64.0.9"}
PROMPT_MARKER = "MARKER-prompt-9c41e0"
SCREEN_MARKER = "MARKER-screen-55ab12"
ROWS = list(nr.RELAY)
ids = lambda r: r.name


def fill(path: str, **more) -> str:
    return re.sub(r"\{([a-z_]+)\}", lambda m: {**FILL, **more}[m.group(1)], path)


def bearer(token: str, **extra) -> dict:
    return {"Authorization": f"Bearer {token}", "X-CCBoard": "1", **extra}


class Count:
    """A peer transport with a counter: every request a hub makes to a peer goes through `seen`."""

    def __init__(self, inner):
        self.inner, self.seen, self.paths = inner, 0, []

    def __call__(self, target, method, path, headers, body, timeout):
        self.seen += 1
        self.paths.append((method, path, timeout))
        return self.inner(target, method, path, headers, body, timeout)


class Answer:
    """A transport that answers with one fixed reply (or raises one), and counts."""

    def __init__(self, status=200, body=b"{}", headers=None, raises=None):
        self.status, self.body, self.headers, self.raises, self.seen = status, body, headers or {}, raises, 0

    def __call__(self, target, method, path, headers, body, timeout):
        self.seen += 1
        if self.raises:
            raise self.raises
        return self.status, self.headers, self.body


class FakeHub:
    """The hub read model as relay() asks for it: one record for the handle."""

    def __init__(self, **rec):
        self.rec = {"status": "online", "age_s": 3, "polled_at": "2026-10-10T00:00:00Z", "last_ok_at": "2026-10-10T00:00:00Z", **rec}

    def records(self, handle=None):
        return [self.rec]


@pytest.fixture(autouse=True)
def _fresh_scan():
    """GET /api/node/state shares the 2 s project scan cache of app/main.py; leave it empty so a test that runs next (tmux down, a different board) never reads this one's."""
    yield
    from app import main
    main._invalidate_scan()


@pytest.fixture(autouse=True)
def _hub_online(monkeypatch):
    """Right after pairing the hub has read nothing, and a relay waits for a reading; these tests ask about the relay itself, so the hub has read every node.
    The tests of that wait put their own record in (or use the real NodeHub)."""
    from app import main
    REAL_HUB["fn"] = REAL_HUB.get("fn") or main._hub
    monkeypatch.setattr(main, "_hub", lambda: FakeHub())


REAL_HUB: dict = {}


@pytest.fixture
def wide(monkeypatch):
    big = lambda: nodes._Limiter(10 ** 6, 60.0, 64)
    monkeypatch.setattr(nodes, "node_read_limiter", big())
    monkeypatch.setattr(nodes, "node_write_limiter", big())


@pytest.fixture
def lone(lite_client):
    """One board with its own database and no pair at all."""
    from app import main
    nodes.reset()
    return lite_client, main.db


def mint(db, scopes):
    return nodes.add_incoming(dict(CALLER), list(scopes), db=db)


def hub_path(row: nr.Row, handle: str = "node-b", **more) -> str:
    return fill(row.hub_path, handle=handle, **more)


SAMPLE = {"task_create": {"project": "shop", "repo": "api", "title": "A task", "prompt": "do it"}, "task_dispatch": {}, "session_open": {"project": "shop", "repo": "api"}}


def ask(board, row, handle: str = "node-b", **kw):
    """The hub route of a row as a signed-in person calls it: a GET, or a POST with the row's sample body."""
    if row.hub_method == "GET":
        return board.call("GET", hub_path(row, handle), **kw)
    kw.setdefault("json", SAMPLE[row.name])
    return board.call(row.hub_method, hub_path(row, handle), **kw)


def token_of(two, row) -> str:
    with two.a.enter():
        return nodes._load_outgoing(row["peer_id"])


def audit_rows(board, **kw):
    return board.db.node_audit_list(500, **kw)


# ================================================================ 1. the table

@pytest.mark.parametrize("row", ROWS, ids=ids)
def test_every_row_is_complete(row):
    assert row.scope in nodes.SCOPES and row.rate_class in ("read", "write") and row.audit_action and row.name
    assert issubclass(row.body_model, BaseModel) and row.body_model.model_config.get("extra") == "forbid", "a strict model: a field nobody asked for is an error"
    assert row.rate_class == ("read" if row.peer_method == "GET" else "write"), "the auth middleware's class and the row's agree"
    assert row.hub_method == row.peer_method
    assert row.hub_path.startswith("/api/nodes/{handle}/") and row.peer_path.startswith("/api/node")
    assert set(row.params) == set(re.findall(r"\{([a-z_]+)\}", row.peer_path)), "the same parameters on both sides"
    assert row.peer_method == "GET" or row.guard or row.human_only, "a row that changes something starts or steers an agent (guard) or is a human's answer (human_only)"
    assert callable(row.target) and row.target({"tid": "7", "name": "p--r--s"}) is not None


def test_names_actions_and_routes_are_unique_and_every_row_has_its_routes():
    from app import main
    assert len({r.name for r in ROWS}) == len(ROWS) == len({r.audit_action for r in ROWS})
    assert len({(r.peer_method, r.peer_path) for r in ROWS}) == len(ROWS)
    live = {(m, r.path) for r in main.app.routes if hasattr(r, "methods") for m in r.methods}
    for r in ROWS:
        assert (r.hub_method, r.hub_path) in live, r.name
        assert (r.peer_method, r.peer_path) in live, r.name


def test_node_routes_are_the_base_routes_and_the_rows_and_nothing_else():
    base = {("POST", "/api/node/rotate"): nodes.SCOPE_ANY, ("POST", "/api/node/unpair"): nodes.SCOPE_ANY, ("GET", "/api/node/summary"): "read",
            ("GET", "/api/node/stream"): "sessions"}          # the stream relay (issue #143, app/nodes_stream.py) is not a JSON row; screen text needs `sessions`
    assert dict(nodes.NODE_ROUTES) == {**base, **{(r.peer_method, r.peer_path): r.scope for r in ROWS}}


def test_the_rows_are_the_five_reads_and_the_three_writes_and_screen_text_needs_sessions_not_read():
    assert {r.name: (r.peer_method, r.peer_path, r.scope) for r in ROWS} == {
        "card": ("GET", "/api/node", "read"), "state": ("GET", "/api/node/state", "read"), "task": ("GET", "/api/node/tasks/{tid}", "read"),
        "pane": ("GET", "/api/node/sessions/{name}/pane", "sessions"), "agents": ("GET", "/api/node/agents", "read"),
        "task_create": ("POST", "/api/node/tasks", "tasks"), "task_dispatch": ("POST", "/api/node/tasks/{tid}/dispatch", "tasks"),
        "session_open": ("POST", "/api/node/sessions", "sessions")}
    assert {r.name: r.rate_class for r in ROWS if r.peer_method == "POST"} == {"task_create": "write", "task_dispatch": "write", "session_open": "write"}
    assert all(r.guard for r in ROWS if r.peer_method == "POST"), "every write row starts or steers an agent: the guard runs on both sides"


def test_every_row_is_human_only_until_a_phase_opens_one_on_purpose():
    """The hook token is held by every agent session on the box: no row lets it relay. A later phase (MCP across nodes) opens a row by setting human_only False."""
    assert ROWS and all(r.human_only for r in ROWS), [r.name for r in ROWS if not r.human_only]


def test_rate_class_is_the_rows_and_else_by_method():
    assert nr.rate_class("GET", "/api/node/tasks/12") == "read" and nr.rate_class("GET", "/api/node/sessions/a--b--c/pane") == "read"
    assert nr.rate_class("POST", "/api/node/rotate") == "write" and nr.rate_class("GET", "/api/state") == "read" and nr.rate_class("DELETE", "/api/x") == "write"


def test_a_row_cannot_list_a_path_with_another_scope(probe_rows):
    clash = nr.Row("clash", "GET", "/api/nodes/{handle}/clash", "GET", "/api/node/state", "tasks", nr.NoFields, nr.READ, "clash")
    with pytest.raises(RuntimeError):
        nr.register_scopes([clash])
    assert nodes.NODE_ROUTES[("GET", "/api/node/state")] == "read"


def test_install_refuses_a_row_without_a_handler():
    from fastapi import FastAPI
    with pytest.raises(RuntimeError, match="no peer handler"):
        nr.install(FastAPI(), {}, lambda: None, lambda: None, rows=[ROWS[2]])


# ================================================================ 2. a refusal test per row, on the peer (generated from the table)

@pytest.mark.parametrize("row", ROWS, ids=ids)
def test_no_credential_is_403(lone, row):
    c, db = lone
    path = fill(row.peer_path)
    assert c.request(row.peer_method, path).json()["error"] == NO_ID
    assert c.request(row.peer_method, path, headers={"X-CCBoard": "1"}).status_code == 403
    if not row.peer_exists:                                              # the card and the state also answer a signed-in person, as they always did
        r = c.request(row.peer_method, path, headers=H)
        assert r.status_code == 403 and "takes a node token" in r.json()["error"], "a person has the board's own routes"
        from app import hooks
        r = c.request(row.peer_method, path, headers={"X-CCBoard-Token": hooks.ensure_token()})
        assert r.status_code == 403 and "takes a node token" in r.json()["error"], "the hook token is local automation, not a node"


@pytest.mark.parametrize("row", ROWS, ids=ids)
def test_a_token_without_the_scope_is_403(lone, row):
    c, db = lone
    _, tok = mint(db, [s for s in nodes.SCOPES if s != row.scope] or ["read"])
    r = c.request(row.peer_method, fill(row.peer_path), headers=bearer(tok))
    assert r.status_code == 403 and f"does not hold the {row.scope} scope" in r.json()["error"]
    assert any(x["action"] == "scope_refused" and x["status"] == "refused" for x in db.node_audit_list(20))


@pytest.mark.parametrize("row", ROWS, ids=ids)
def test_an_expired_or_revoked_or_unknown_token_is_401(lone, row):
    c, db = lone
    pair, tok = mint(db, nodes.SCOPES)
    path = fill(row.peer_path)
    db.node_pair_update(pair["peer_id"], expires_at="2000-01-01T00:00:00Z")
    assert c.request(row.peer_method, path, headers=bearer(tok)).status_code == 401, "expired"
    db.node_pair_update(pair["peer_id"], expires_at=None)
    assert nodes.revoke(pair["peer_id"], db=db)
    assert c.request(row.peer_method, path, headers=bearer(tok)).status_code == 401, "revoked"
    assert c.request(row.peer_method, path, headers=bearer(nodes.TOKEN_PREFIX + "A" * 43)).status_code == 401, "unknown"


@pytest.mark.parametrize("row", ROWS, ids=ids)
def test_an_over_size_body_is_413(lone, row):
    c, db = lone
    _, tok = mint(db, nodes.SCOPES)
    r = c.request(row.peer_method, fill(row.peer_path), headers=bearer(tok), content=b"x" * (nodes.BODY_MAX + 1))
    assert r.status_code == 413, r.status_code


@pytest.mark.parametrize("row", ROWS, ids=ids)
def test_the_bucket_of_the_rows_class_answers_429_with_retry_after(lone, monkeypatch, row):
    c, db = lone
    _, tok = mint(db, nodes.SCOPES)
    mine, other = ("node_read_limiter", "node_write_limiter") if row.rate_class == "read" else ("node_write_limiter", "node_read_limiter")
    monkeypatch.setattr(nodes, mine, nodes._Limiter(2, 60.0, 8))
    monkeypatch.setattr(nodes, other, nodes._Limiter(2, 60.0, 8))
    path = fill(row.peer_path)
    first = [c.request(row.peer_method, path, headers=bearer(tok)).status_code for _ in range(2)]
    assert 429 not in first
    r = c.request(row.peer_method, path, headers=bearer(tok))
    assert r.status_code == 429 and r.headers["retry-after"].isdigit() and int(r.headers["retry-after"]) >= 1
    assert getattr(nodes, other)._hits == {}, "the other class's bucket was not touched"


def test_the_limits_are_120_reads_and_30_writes_a_minute():
    assert (nodes.READ_RATE, nodes.WRITE_RATE) == (120, 30)
    assert (nodes.node_read_limiter.limit, nodes.node_write_limiter.limit) == (120, 30)


def test_a_write_row_draws_on_the_write_bucket_even_though_the_method_is_not_get(lone, monkeypatch, probe_rows):
    c, db = lone
    _, tok = mint(db, nodes.SCOPES)
    reads, writes = nodes._Limiter(10 ** 6, 60.0, 8), nodes._Limiter(1, 60.0, 8)
    monkeypatch.setattr(nodes, "node_read_limiter", reads)
    monkeypatch.setattr(nodes, "node_write_limiter", writes)
    for _ in range(2):
        r = c.post("/api/node/probe", headers=bearer(tok), json={})
    assert r.status_code == 429 and reads._hits == {}


@pytest.mark.parametrize("row", ROWS, ids=ids)
def test_a_wrong_method_on_a_row_is_405(lone, wide, row):
    c, db = lone
    _, tok = mint(db, nodes.SCOPES)
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        if method == row.peer_method:
            continue
        r = c.request(method, fill(row.peer_path), headers=bearer(tok))
        assert r.status_code == 405 and row.peer_method in r.headers["allow"], (method, row.name)


@pytest.mark.parametrize("row", [r for r in ROWS if not r.peer_exists], ids=ids)
def test_a_token_with_the_scope_reaches_the_handler_and_gets_the_handlers_own_answer(lone, wide, fake_tmux, row):
    c, db = lone
    _, tok = mint(db, [row.scope])
    r = c.request(row.peer_method, fill(row.peer_path), headers=bearer(tok))
    assert r.status_code in ((200, 404) if row.peer_method == "GET" else (404, 422)) and r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("row", [r for r in ROWS if not r.peer_exists], ids=ids)
def test_the_wrapper_checks_the_scope_itself_when_the_middleware_did_not(lone, monkeypatch, row):
    """The peer applies every check itself: with the middleware's scope table opened by a mistake, the wrapper still refuses a pair without the scope."""
    c, db = lone
    _, tok = mint(db, [s for s in nodes.SCOPES if s != row.scope])
    monkeypatch.setattr(nodes, "scope_ok", lambda scopes, needed: True if needed != row.scope else False)
    monkeypatch.setitem(nodes.NODE_ROUTES, (row.peer_method, row.peer_path), nodes.SCOPE_ANY)
    r = c.request(row.peer_method, fill(row.peer_path), headers=bearer(tok))
    assert r.status_code == 403


def test_the_synthetic_rows_get_the_same_refusals(lone, probe_rows, wide):
    c, db = lone
    for row in probe_rows.rows:
        path = fill(row.peer_path)
        assert c.request(row.peer_method, path).status_code == 403
        _, tok = mint(db, [s for s in nodes.SCOPES if s != row.scope])
        assert c.request(row.peer_method, path, headers=bearer(tok), json={}).status_code == 403
        _, ok = mint(db, [row.scope])
        assert c.request(row.peer_method, path, headers=bearer(ok), json={}).status_code == 200
        assert c.request("GET", path, headers=bearer(ok)).status_code == 405
        assert c.request(row.peer_method, path, headers=bearer(ok), content=b"x" * (nodes.BODY_MAX + 1)).status_code == 413
    assert probe_rows.calls == [{}, {}]


# ================================================================ 3. the hub side, over two boards

@pytest.fixture
def hub(two_nodes, pair_up):
    """Boards a (the hub) and b paired with every scope; the transport counts. Returns (two_nodes, row, count)."""
    row = pair_up()
    count = Count(two_nodes.transport)
    nodes.peer_transport = count
    return two_nodes, row, count


@pytest.mark.parametrize("row", ROWS, ids=ids)
def test_a_pair_without_the_scope_is_409_before_any_call(two_nodes, pair_up, row):
    reg = pair_up([s for s in nodes.SCOPES if s != row.scope][:3] if row.scope != "read" else ["tasks"])
    count = Count(two_nodes.transport)
    nodes.peer_transport = count
    r = ask(two_nodes.a, row, reg["handle"])
    assert r.status_code == 409 and r.json()["reason"] == "scope" and r.json()["error"] == f"needs the {row.scope} scope on node-b", r.text
    assert r.json()["node"] == "node-b" and count.seen == 0
    out = audit_rows(two_nodes.a, action=row.audit_action)
    assert out and out[0]["direction"] == "out" and out[0]["status"] == "refused" and "scope" in out[0]["detail"]


@pytest.mark.parametrize("row", ROWS, ids=ids)
def test_an_offline_node_is_503_with_the_reason_and_the_age_and_no_call(hub, monkeypatch, row):
    two, reg, count = hub
    from app import main
    monkeypatch.setattr(main, "_hub", lambda: FakeHub(status="offline", age_s=1234))
    r = ask(two.a, row, reg["handle"])
    assert r.status_code == 503 and r.json()["reason"] == "offline" and r.json()["age"] == 1234 and "offline" in r.json()["error"] and "20 min" in r.json()["error"], r.text
    assert count.seen == 0, "no request was made"
    assert audit_rows(two.a, action=row.audit_action)[0]["status"] == "refused"


@pytest.mark.parametrize("status", ["unauthorized", "unpaired"])
def test_a_node_that_needs_a_new_pair_is_409_and_no_call(hub, monkeypatch, status):
    two, reg, count = hub
    from app import main
    monkeypatch.setattr(main, "_hub", lambda: FakeHub(status=status))
    r = two.a.get(hub_path(ROWS[0], reg["handle"]))
    assert r.status_code == 409 and count.seen == 0
    assert r.json()["reason"] == ("needs_repair" if status == "unauthorized" else "unpaired") and ("re-pair" in r.json()["error"] or "pair it again" in r.json()["error"])


def test_a_pair_marked_needs_repair_is_409_and_no_call(hub):
    two, reg, count = hub
    with two.a.enter():
        nodes._note(two.a.db, reg["peer_id"], ok=False, error="the peer answered 401", repair=True)
    r = two.a.get(hub_path(ROWS[0], reg["handle"]))
    assert r.status_code == 409 and r.json()["reason"] == "needs_repair" and count.seen == 0


@pytest.mark.parametrize("row", ROWS, ids=ids)
def test_a_node_the_hub_has_not_read_yet_is_409_not_read_yet_and_no_call(hub, monkeypatch, row):
    two, reg, count = hub
    from app import main
    for rec in ({"status": "offline", "age_s": None, "polled_at": None, "last_ok_at": None}, None):
        monkeypatch.setattr(main, "_hub", (lambda: FakeHub(**rec)) if rec else (lambda: type("H", (), {"records": lambda self, h=None: []})()))
        r = ask(two.a, row, reg["handle"])
        assert r.status_code == 409 and r.json()["reason"] == "not_read_yet" and "not been read yet" in r.json()["error"], r.text
    assert count.seen == 0
    assert audit_rows(two.a, action=row.audit_action)[0]["status"] == "refused"


def test_only_an_online_or_stale_reading_is_called_and_the_real_hub_gets_there_after_one_poll(two_nodes, pair_up, monkeypatch):
    from app import main
    reg = pair_up()
    count = Count(two_nodes.transport)
    nodes.peer_transport = count
    monkeypatch.setattr(main, "_hub", lambda: FakeHub(status="stale", age_s=400))
    assert two_nodes.a.get(hub_path(BY["card"], reg["handle"])).status_code == 200
    monkeypatch.setattr(main, "_hub", REAL_HUB["fn"])                       # the real hub, which has read nothing yet
    monkeypatch.setattr(main, "node_hub", None)
    seen = count.seen
    r = two_nodes.a.get(hub_path(BY["card"], reg["handle"]))
    assert r.status_code == 409 and r.json()["reason"] == "not_read_yet" and count.seen == seen
    with two_nodes.a.enter():
        main._hub().poll_once()
    r = two_nodes.a.get(hub_path(BY["card"], reg["handle"]))
    assert r.status_code == 200 and r.json()["data"]["name"] == "node-b", "one good poll later the node is called"


def test_the_order_of_the_refusals_scope_then_status_then_body(two_nodes, pair_up, monkeypatch, probe_rows):
    """Scope before offline before the body: a pair without the scope that is also offline says scope; offline with a bad body says offline; a good node says 422."""
    reg = pair_up(["read"])
    count = Count(two_nodes.transport)
    nodes.peer_transport = count
    from app import main
    monkeypatch.setattr(main, "_hub", lambda: FakeHub(status="offline", age_s=9))
    path = f"/api/nodes/{reg['handle']}/probe"
    assert two_nodes.a.post(path, json={"bypass": True}).json()["reason"] == "scope"
    with two_nodes.a.enter():
        nodes.put_peer({"peer_id": reg["peer_id"], "direction": "out", "url": reg["url"], "scopes": ["read", "sessions"]}, db=two_nodes.a.db)
    assert two_nodes.a.post(path, json={"bypass": True}).json()["reason"] == "offline"
    monkeypatch.setattr(main, "_hub", lambda: FakeHub(status="online"))
    assert two_nodes.a.post(path, json={"bypass": True}).json()["reason"] == "invalid"
    assert count.seen == 0


def test_an_unknown_handle_is_404_local_is_400_and_a_bad_handle_400(hub):
    two, reg, count = hub
    for handle, status, reason in (("nope", 404, "unknown_node"), ("local", 400, "local"), ("Not_A_Handle", 400, "bad_handle"), ("x" * 40, 400, "bad_handle")):
        r = two.a.get(f"/api/nodes/{handle}/card")
        assert r.status_code == status and r.json()["reason"] == reason, (handle, r.text)
    assert count.seen == 0


def test_a_registry_row_with_this_boards_own_id_or_address_is_a_400_and_no_call(two_nodes):
    count = Count(two_nodes.transport)
    nodes.peer_transport = count
    a = two_nodes.a
    with a.enter():
        me = nodes.node_id()
        nodes.put_peer({"direction": "out", "url": a.url, "name": "myself", "node_id": me, "scopes": ["read"]}, db=a.db)
        nodes.put_peer({"direction": "out", "url": "https://100.64.0.77", "name": "twin", "node_id": me, "scopes": ["read"]}, db=a.db)
        nodes.put_peer({"direction": "out", "url": a.url + ":443", "name": "alias", "node_id": "n_" + "d" * 16, "scopes": ["read"]}, db=a.db)
    for handle in ("myself", "twin", "alias"):
        r = a.get(f"/api/nodes/{handle}/card")
        assert r.status_code == 400 and r.json()["reason"] == "self", (handle, r.text)
    assert count.seen == 0


def test_a_legacy_row_has_no_token_and_is_409_before_any_call(two_nodes):
    count = Count(two_nodes.transport)
    nodes.peer_transport = count
    with two_nodes.a.enter():
        nodes.import_legacy([{"name": "old-box", "url": "https://100.64.0.2"}], db=two_nodes.a.db)
    r = two_nodes.a.get("/api/nodes/old-box/card")
    assert r.status_code == 409 and r.json()["reason"] == "legacy" and count.seen == 0


def test_a_node_token_on_a_hub_relay_route_is_403_there_is_no_chaining(hub, wide):
    two, reg, count = hub
    _, tok = nodes.add_incoming(dict(CALLER), list(nodes.SCOPES), db=two.a.db)
    for row in ROWS:
        r = two.a.call(row.hub_method, hub_path(row, reg["handle"]), owner=False, headers=bearer(tok))
        assert r.status_code == 403 and r.json()["error"] == CLOSED, row.name
    assert count.seen == 0


def test_a_caller_needs_identity_and_the_csrf_header_or_the_hook_token(hub, wide):
    two, reg, count = hub
    path = hub_path(BY["card"], reg["handle"])
    assert two.a.call("GET", path, owner=False).status_code == 403
    r = two.a.call("GET", path, owner=False, headers={"Tailscale-User-Login": "alice@example.com"})
    assert r.status_code == 403 and r.json()["error"] == "missing X-CCBoard header", "a GET too: it makes this board call another"
    assert two.a.call("GET", path, owner=False, headers={"Tailscale-User-Login": "mallory@example.com", "X-CCBoard": "1"}).status_code == 403
    assert count.seen == 0
    assert count.seen == 0


@pytest.mark.parametrize("row", ROWS, ids=ids)
def test_the_hook_token_every_agent_session_holds_cannot_relay_on_any_row(hub, wide, fake_tmux, row):
    two, reg, count = hub
    r = ask(two.a, row, reg["handle"], owner=False, headers={"X-CCBoard-Token": two.a.hook_token})
    assert r.status_code == 403 and r.json()["reason"] == "human_only" and "signed-in person" in r.json()["error"], r.text
    assert count.seen == 0, "nothing reached another node"
    r = ask(two.a, row, reg["handle"], owner=False, headers={"X-CCBoard-Token": two.a.hook_token, "X-CCBoard": "1", **ID})
    assert r.status_code == 403, "the hook token with a person's headers beside it is still the hook token"
    assert ask(two.a, row, reg["handle"]).status_code in (200, 404, 422), "the signed-in person still can (a write row with nothing on the peer says 404 or 422)"


def test_the_acting_user_is_the_authenticated_login_never_a_header_or_a_query_the_caller_sends(hub, wide):
    two, reg, count = hub
    seen = {}

    def spy(target, method, path, headers, body, timeout):
        seen.update(headers)
        seen["path"] = path
        return two.transport(target, method, path, headers, body, timeout)
    nodes.peer_transport = spy
    r = two.a.get(hub_path(BY["card"], reg["handle"]), headers={"X-CCBoard-Acting-User": "root", "X-CCBoard-Node": "ts:nFORGED"})
    assert r.status_code == 200
    assert seen["X-CCBoard-Acting-User"] == "alice@example.com" and seen["X-CCBoard-Node"] != "ts:nFORGED"
    r = two.a.get(hub_path(BY["card"], reg["handle"]) + "?acting_user=root")
    assert r.status_code == 422 and "acting_user" in r.json()["error"], "the query cannot carry it either"
    assert audit_rows(two.a, action="read_card")[0]["user"] == "alice@example.com"
    assert audit_rows(two.b, action="read_card")[0]["user"] == "for alice@example.com"


BY = nr.BY_NAME


def test_a_human_only_row_refuses_the_hook_token_but_not_a_person(hub, probe_rows, wide):
    two, reg, count = hub
    path = f"/api/nodes/{reg['handle']}/probe-answer"
    r = two.a.call("POST", path, owner=False, headers={"X-CCBoard-Token": two.a.hook_token}, json={})
    assert r.status_code == 403 and r.json()["reason"] == "human_only" and count.seen == 0
    assert two.a.post(path, json={}).status_code == 200


def test_the_wrapped_answer_carries_node_age_and_the_peers_data(hub):
    two, reg, count = hub
    r = two.a.get(hub_path(BY["card"], reg["handle"]))
    body = r.json()
    assert r.status_code == 200 and set(body) == {"node", "age", "data"} and body["node"] == "node-b" and body["age"] == 0
    assert body["data"]["app"] == "ccboard" and body["data"]["name"] == "node-b" and body["data"]["node_id"], "the card, rebuilt from the whitelist"
    s = two.a.get(hub_path(BY["state"], reg["handle"])).json()
    assert s["node"] == "node-b" and s["data"]["node"]["id"] == body["data"]["node_id"], "the state's own `node` key is inside `data`, not overwritten"
    assert r.headers["cache-control"] == "no-store"
    assert [p[:2] for p in count.paths] == [("GET", "/api/node"), ("GET", "/api/node/state")], "only the rows' peer paths were called"
    assert all(p[2] == nr.RELAY_TIMEOUT == 10.0 for p in count.paths), "the call timeout is 10 s"


def test_the_acting_user_and_the_nodes_own_id_travel_as_headers_never_the_token_in_an_answer(hub):
    two, reg, count = hub
    seen = {}

    def spy(target, method, path, headers, body, timeout):
        seen.update(headers)
        return two.transport(target, method, path, headers, body, timeout)
    nodes.peer_transport = spy
    r = two.a.get(hub_path(BY["card"], reg["handle"]))
    assert seen["X-CCBoard-Acting-User"] == "alice@example.com" and seen["X-CCBoard-Node"].startswith(("ts:", "n_"))
    assert token_of(two, reg) not in r.text


def test_a_redirect_from_the_peer_is_not_followed(two_nodes, pair_up):
    reg = pair_up()
    flaky = Answer(302, b"", {"location": "https://100.64.0.99/api/node"})
    nodes.peer_transport = flaky
    r = two_nodes.a.get(hub_path(BY["card"], reg["handle"]))
    assert r.status_code == 502 and r.json()["reason"] == "redirect" and flaky.seen == 1, "one request, no second one to the location"


def test_an_answer_over_512_kb_is_a_502(two_nodes, pair_up):
    reg = pair_up()
    nodes.peer_transport = Answer(200, b'{"x":"' + b"a" * (nodes.RESP_MAX + 10) + b'"}')
    r = two_nodes.a.get(hub_path(BY["agents"], reg["handle"]))
    assert r.status_code == 502 and r.json()["reason"] == "too_large"
    assert (nodes.RESP_MAX, nodes.BODY_MAX) == (512 * 1024, 256 * 1024)


def test_an_answer_that_is_no_json_object_or_the_wrong_node_is_a_502(two_nodes, pair_up):
    reg = pair_up()
    for body in (b"not json", b"[1,2]", b'"x"'):
        nodes.peer_transport = Answer(200, body)
        r = two_nodes.a.get(hub_path(BY["agents"], reg["handle"]))
        assert r.status_code == 502 and r.json()["reason"] == "bad_answer", body
    nodes.peer_transport = Answer(200, json.dumps({"app": "ccboard", "node_id": "n_" + "e" * 16, "name": "evil"}).encode())
    r = two_nodes.a.get(hub_path(BY["card"], reg["handle"]))
    assert r.status_code == 502 and r.json()["reason"] == "wrong_node", "a card of another node id is not shown as this node's"


@pytest.mark.parametrize("status,body,headers,want_status,reason", [
    (401, b'{"error":"wrong token"}', {}, 409, "needs_repair"),
    (403, b'{"error":"this node token does not hold the read scope"}', {}, 409, "scope"),
    (403, b'{"error":"this node token does not open that route"}', {}, 409, "scope"),
    (404, b'{"error":"no such task"}', {}, 404, "not_found"),
    (429, b'{"error":"too many requests from this node"}', {"Retry-After": "17"}, 429, "rate_limited"),
    (422, b'{"error":"lines: Input should be less than or equal to 40"}', {}, 422, "invalid"),
    (400, b'{"error":"bad"}', {}, 400, "refused"),
    (500, b"boom", {}, 502, "peer_error"),
    (503, b'{"error":"ccboard-tmux is not running"}', {}, 502, "peer_error"),
    (302, b"", {"location": "/x"}, 502, "redirect"),
])
def test_every_status_of_the_peer_has_a_mapping_and_a_stable_reason(two_nodes, pair_up, status, body, headers, want_status, reason):
    reg = pair_up()
    nodes.peer_transport = Answer(status, body, headers)
    r = two_nodes.a.get(hub_path(BY["task"], reg["handle"]))
    assert r.status_code == want_status and r.json()["reason"] == reason and r.json()["node"] == "node-b", r.text
    if status == 429:
        assert r.headers["retry-after"] == "17"
    if status == 401:
        with two_nodes.a.enter():
            assert nodes.peer(reg["peer_id"], two_nodes.a.db)["needs_repair"] is True, "the pair is marked, the next call is refused before it is made"
    assert audit_rows(two_nodes.a, action="read_task")[0]["status"] in ("failed", "refused")


def test_a_timeout_after_the_request_was_sent_is_504_could_not_confirm_and_is_not_retried(two_nodes, pair_up):
    reg = pair_up()
    gone = Answer(raises=TimeoutError("slow"))
    nodes.peer_transport = gone
    r = two_nodes.a.get(hub_path(BY["task"], reg["handle"]))
    assert r.status_code == 504 and r.json()["reason"] == "unconfirmed" and "not retried" in r.json()["error"] and gone.seen == 1
    gone2 = Answer(raises=socket.timeout("slow"))
    nodes.peer_transport = gone2
    assert two_nodes.a.get(hub_path(BY["task"], reg["handle"])).json()["reason"] == "unconfirmed" and gone2.seen == 1


def test_a_peer_that_refuses_connections_is_a_502_unreachable(two_nodes, pair_up):
    reg = pair_up()
    two_nodes.offline.add("node-b")
    r = two_nodes.a.get(hub_path(BY["card"], reg["handle"]))
    assert r.status_code == 502 and r.json()["reason"] == "unreachable"


def test_a_registry_row_edited_to_an_internal_address_fails_the_url_rule_at_call_time_and_no_call_is_made(hub):
    two, reg, count = hub
    rows = two.a.db.kv_get(nodes.KV_PEERS)["value"]
    for url in ("https://127.0.0.1", "https://169.254.169.254", "https://10.0.0.5:8443", "http://100.64.0.2", "https://evil.example.com", "https://100.64.0.2/x?y=1"):
        rows[0]["url"] = url
        two.a.db.kv_set(nodes.KV_PEERS, rows)
        r = two.a.get(hub_path(BY["card"], reg["handle"]))
        assert r.status_code == 502 and r.json()["reason"] == "bad_address", (url, r.text)
    assert count.seen == 0, "the rule runs before the transport"


def test_the_target_is_the_registry_row_never_something_the_request_carries(hub):
    two, reg, count = hub
    r = two.a.get(hub_path(BY["card"], reg["handle"]) + "?url=https://100.64.0.99&host=x")
    assert r.status_code == 422 and "url" in r.json()["error"], "an address in the query is an extra field, not a target"
    r = two.a.get(f"/api/nodes/{reg['handle']}%2F..%2F..%2Fapi%2Fstate/card")
    assert r.status_code in (400, 404) and count.seen == 0
    r = two.a.get(hub_path(BY["card"], "https:%2F%2F100.64.0.99"))
    assert r.status_code in (400, 404) and count.seen == 0


def test_no_registry_rows_means_every_hub_relay_route_is_404_and_no_peer_wrapper_is_reachable(lone, probe_rows, wide, fake_tmux):
    c, db = lone
    assert nodes.registry(db) == []
    for row in (*ROWS, *probe_rows.rows):
        for handle in ("node-b", "box", "x"):
            r = c.request(row.hub_method, hub_path(row, handle), headers=H, json={} if row.hub_method != "GET" else None)
            assert r.status_code == 404 and r.json()["reason"] == "unknown_node", (row.name, handle, r.text)
        r = c.request(row.peer_method, fill(row.peer_path), headers=bearer(nodes.TOKEN_PREFIX + "Z" * 43))
        assert r.status_code == 401, "no pair, no token, no way in"
    assert db.kv_get(nodes.KV_PEERS) is None and db.node_pairs() == []


def test_a_single_board_writes_no_audit_row_and_no_registry_row_for_a_relay_that_found_no_node(lone):
    c, db = lone
    for handle in ("box", "local", "Bad_Handle"):
        c.get(f"/api/nodes/{handle}/card", headers=H)
    assert db.kv_get(nodes.KV_PEERS) is None
    assert db.node_audit_list(5) == [], "nothing resolved, so nothing is recorded: a board with no paired node gets no new row"


def test_the_body_of_a_hub_post_is_checked_after_the_checks_and_capped(hub, probe_rows, wide):
    two, reg, count = hub
    path = f"/api/nodes/{reg['handle']}/probe"
    r = two.a.post(path, content=b"x" * (nodes.BODY_MAX + 1), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["reason"] == "too_large" and count.seen == 0
    r = two.a.post(path, content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["reason"] == "invalid" and count.seen == 0
    r = two.a.post(path, json=[1, 2])
    assert r.status_code == 422 and count.seen == 0


# ================================================================ 4. what the read rows show

def add_task(db, **kw):
    return db.task_add(project="shop", repo="api", slug="fix-it", title="Fix the login", prompt=kw.pop("prompt", "do the thing"), branch="worktree-fix-it", base="main",
                       worktree="/home/someone/secret/path", tmux_name="shop--api--t-fix-it", claude_session_id="sess-uuid-1234", **kw)


def test_a_tasks_detail_is_the_state_row_and_a_few_facts_and_never_the_prompt_or_the_result(hub):
    two, reg, count = hub
    tid = add_task(two.b.db, prompt=f"secret plan {PROMPT_MARKER}", result=f"the result {PROMPT_MARKER}")
    r = two.a.get(hub_path(BY["task"], reg["handle"], tid=str(tid)))
    d = r.json()["data"]
    assert r.status_code == 200 and d["id"] == tid and d["title"] == "Fix the login" and d["phase"] and d["project"] == "shop" and d["repo"] == "api"
    assert d["has_result"] is True and d["branch"] == "worktree-fix-it" and d["tmux"] == "shop--api--t-fix-it"
    assert PROMPT_MARKER not in r.text and "secret/path" not in r.text and "sess-uuid" not in r.text
    assert {"prompt", "result", "worktree", "claude_session_id"}.isdisjoint(d)
    direct = two.b.get(f"/api/node/tasks/{tid}", owner=False, headers=bearer(token_of(two, reg)))
    assert direct.status_code == 200 and PROMPT_MARKER not in direct.text, "nor does the peer's own answer hold it"


def test_a_missing_task_is_404_and_a_bad_task_id_is_422_on_both_sides(hub):
    two, reg, count = hub
    r = two.a.get(hub_path(BY["task"], reg["handle"], tid="9999"))
    assert r.status_code == 404 and r.json()["reason"] == "not_found" and "no such task" in r.json()["error"]
    r = two.a.get(hub_path(BY["task"], reg["handle"], tid="abc"))
    assert r.status_code == 422 and "tid is not valid" in r.json()["error"]
    seen = count.seen
    r = two.a.get(hub_path(BY["task"], reg["handle"], tid="1" * 13))
    assert r.status_code == 422 and count.seen == seen, "the hub refused it early"
    tok = token_of(two, reg)
    r = two.b.get("/api/node/tasks/abc", owner=False, headers=bearer(tok))
    assert r.status_code == 422, "and the peer does not trust the hub's check"


def screen(n: int, extra: str = "") -> str:
    return "\n".join(f"line {i:03d} {extra}" for i in range(n)) + "\n\n\n"


def test_the_pane_row_is_the_last_40_lines_with_control_characters_out_and_tokens_replaced(hub, fake_tmux):
    two, reg, count = hub
    name = "shop--api--s1"
    fake_tmux["sessions"][name] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "zsh", "path": "/x", "pid": 1, "env": {}}
    tok = nodes.TOKEN_PREFIX + "Q" * 43
    fake_tmux["screen"] = (screen(120) + f"\x1b[31mred\x1b[0m\x07 tab\there \x00nul \u202eRTL {tok} {SCREEN_MARKER}   \n" + "last line   \n\n")
    r = two.a.get(hub_path(BY["pane"], reg["handle"], name=name))
    d = r.json()["data"]
    assert r.status_code == 200 and d["name"] == name and d["cap"] == 40 and len(d["lines"]) == 40, len(d["lines"])
    assert d["lines"][-1] == "last line" and d["lines"][0].startswith("line 0")
    direct = two.b.get(fill(BY["pane"].peer_path, name=name), owner=False, headers=bearer(token_of(two, reg))).json()
    assert len(direct["lines"]) == 40 and direct["cap"] == 40, "the peer cuts the tail itself; the hub's cut is a second one"
    joined = "\n".join(d["lines"])
    assert tok not in joined and "[redacted]" in joined and SCREEN_MARKER in joined
    assert not re.search("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202e]", joined), "control and bidi characters are out"
    assert all(len(x) <= nr.PANE_LINE_MAX for x in d["lines"])
    fake_tmux["screen"] = "x" * 5000
    assert len(two.a.get(hub_path(BY["pane"], reg["handle"], name=name)).json()["data"]["lines"][0]) <= nr.PANE_LINE_MAX


def test_the_pane_row_takes_a_smaller_line_count_and_refuses_a_larger_one_or_anything_else(hub, fake_tmux):
    two, reg, count = hub
    name = "shop--api--s1"
    fake_tmux["sessions"][name] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "zsh", "path": "/x", "pid": 1, "env": {}}
    fake_tmux["screen"] = screen(100)
    base = hub_path(BY["pane"], reg["handle"], name=name)
    assert len(two.a.get(base + "?lines=5").json()["data"]["lines"]) == 5
    seen = count.seen
    for q in ("?lines=41", "?lines=0", "?lines=abc", "?lines=5&lines=6", "?cmd=ls", "?lines=5&x=1"):
        r = two.a.get(base + q)
        assert r.status_code == 422, (q, r.text)
    assert count.seen == seen, "the hub refused every one early"
    tok = token_of(two, reg)
    for q in ("?lines=41", "?lines=5&lines=6", "?cmd=ls"):
        assert two.b.get(fill(BY["pane"].peer_path, name=name) + q, owner=False, headers=bearer(tok)).status_code == 422, "the peer refuses them itself"
    assert len(two.b.get(fill(BY["pane"].peer_path, name=name) + "?lines=40", owner=False, headers=bearer(tok)).json()["lines"]) == 40


def test_the_pane_row_refuses_a_session_that_is_not_there_a_bad_name_and_the_login_session(hub, fake_tmux):
    two, reg, count = hub
    tok = token_of(two, reg)
    fake_tmux["sessions"]["_ccboard-login"] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "claude", "path": "/x", "pid": 1, "env": {}}
    fake_tmux["screen"] = "Paste code here: CODE-123456"
    r = two.a.get(hub_path(BY["pane"], reg["handle"], name="shop--api--nothere"))
    assert r.status_code == 404 and r.json()["reason"] == "not_found"
    for bad in ("_ccboard-login", "no_dashes", "a--b", "a--b--c--d", "a%20b--c--d"):
        r = two.b.get(f"/api/node/sessions/{bad}/pane", owner=False, headers=bearer(tok))
        assert r.status_code in (404, 422) and "CODE-123456" not in r.text, bad
    r = two.a.get(f"/api/nodes/{reg['handle']}/sessions/_ccboard-login/pane")
    assert r.status_code == 422 and "CODE-123456" not in r.text


def test_the_pane_function_itself_refuses_an_internal_session_even_if_a_name_got_past_the_checks(fake_tmux):
    from app import projects
    fake_tmux["sessions"]["_ccboard-login"] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "claude", "path": "/x", "pid": 1, "env": {}}
    fake_tmux["screen"] = "Paste code here: CODE-123456"
    with pytest.raises(projects.NotFound):
        nr.peer_pane(None, {"name": "_ccboard-login"}, {"lines": 40})
    fake_tmux["sessions"]["shop--api--s1"] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "zsh", "path": "/x", "pid": 1, "env": {}}
    assert nr.peer_pane(None, {"name": "shop--api--s1"}, {"lines": 40})["lines"] == ["Paste code here: CODE-123456"]


def test_the_agents_row_lists_the_installed_agents_without_the_account_block_or_a_dangerous_choice(hub, monkeypatch):
    from app import agents
    two, reg, count = hub
    for a in agents.all():
        real = a.auth_status
        monkeypatch.setattr(a, "auth_status", lambda real=real: {**(real() or {}), "installed": True, "loggedIn": True, "email": "me@example.com", "orgName": "Acme Corp"})
    r = two.a.get(hub_path(BY["agents"], reg["handle"]))
    d = r.json()["data"]
    assert r.status_code == 200 and [a["name"] for a in d["agents"]] == ["claude", "codex"]
    text = r.text
    assert "me@example.com" not in text and "Acme Corp" not in text and '"auth"' not in text and '"slash"' not in text
    values = json.dumps([[{k: v for k, v in o.items() if k != "help"} for o in a["options"]] for a in d["agents"]])
    assert "danger-full-access" not in values and "bypassPermissions" not in values, "the help sentences may name them; no choice and no control does"
    for a in d["agents"]:
        keys = {o["key"] for o in a["options"]}
        assert keys.isdisjoint({"bypass", "args", "add_dirs", "allowed_tools", "disallowed_tools", "append_system_prompt", "tools", "mcp_config", "config", "settings"}), a["name"]
        assert set(a["permission_modes"]) <= {"default", "acceptEdits", "plan"}
        assert a["logged_in"] is True and isinstance(a["installed"], bool)
        for o in a["options"]:
            for c in o.get("choices") or []:
                assert not str(c).startswith(("shell", "danger", "bypass")), (a["name"], o["key"], c)
    codex = next(a for a in d["agents"] if a["name"] == "codex")
    sandbox = next((o for o in codex["options"] if o["key"] == "sandbox"), None)
    approval = next((o for o in codex["options"] if o["key"] == "approval"), None)
    assert sandbox and set(sandbox["choices"]) <= {"read-only", "workspace-write"} and approval and set(approval["choices"]) <= {"on-request"}


# ================================================================ 5. the audit, on both sides

def test_each_call_writes_one_out_row_on_the_hub_and_one_in_row_on_the_peer_with_the_acting_user(hub):
    two, reg, count = hub
    tid = add_task(two.b.db)
    two.a.get(hub_path(BY["task"], reg["handle"], tid=str(tid)))
    out = audit_rows(two.a, action="read_task")
    inn = audit_rows(two.b, action="read_task")
    assert len(out) == 1 and len(inn) == 1
    assert (out[0]["direction"], out[0]["node_name"], out[0]["user"], out[0]["target"], out[0]["status"], out[0]["peer"]) == (
        "out", "node-b", "alice@example.com", f"task {tid}", "ok", reg["peer_id"])
    assert (inn[0]["direction"], inn[0]["node_name"], inn[0]["user"], inn[0]["target"], inn[0]["status"]) == ("in", "node-a", "for alice@example.com", f"task {tid}", "ok")
    assert out[0]["at"] and inn[0]["at"]


def test_card_and_state_are_audited_on_the_peer_only_when_a_person_is_acting_never_for_a_poll(hub):
    two, reg, count = hub
    from app import main, nodes_hub
    with two.a.enter():
        hubobj = nodes_hub.NodeHub(two.a.db, interval=20)
        hubobj.poll_one(hubobj.rows()[0])
    assert audit_rows(two.b, action="read_card") == [] and audit_rows(two.b, action="read_state") == [], "the hub's poll leaves no row, or the table fills"
    two.a.get(hub_path(BY["card"], reg["handle"]))
    two.a.get(hub_path(BY["state"], reg["handle"]))
    assert len(audit_rows(two.b, action="read_card")) == 1 and len(audit_rows(two.b, action="read_state")) == 1


def test_a_failed_call_writes_a_row_with_its_reason_on_both_sides(hub):
    two, reg, count = hub
    two.a.get(hub_path(BY["task"], reg["handle"], tid="4242"))
    out, inn = audit_rows(two.a, action="read_task")[0], audit_rows(two.b, action="read_task")[0]
    assert out["status"] == "failed" and out["detail"].startswith("not_found") and "no such task" in out["detail"]
    assert inn["status"] == "failed" and "no such task" in inn["detail"] and inn["target"] == "task 4242"


def test_a_refused_call_writes_a_row_with_the_reason_and_no_request(two_nodes, pair_up):
    reg = pair_up(["tasks"])
    two_nodes.a.get(hub_path(BY["card"], reg["handle"]))
    row = audit_rows(two_nodes.a, action="read_card")[0]
    assert row["status"] == "refused" and "scope" in row["detail"] and row["node_name"] == "node-b"
    assert audit_rows(two_nodes.b, action="read_card") == []


def test_no_row_log_line_or_answer_holds_a_token_a_code_or_the_prompt(hub, caplog, fake_tmux):
    two, reg, count = hub
    caplog.set_level(logging.DEBUG)
    token = token_of(two, reg)
    with two.b.enter():
        pair_token_digest = nodes._digest(token)
    code_text = "ABCDE-FGHJK"
    tid = add_task(two.b.db, prompt=f"{PROMPT_MARKER} sk-ant-whatever {code_text}")
    answers = []
    for row in ROWS:
        extra = {"tid": str(tid), "name": "shop--api--s1"}
        fake_tmux["sessions"]["shop--api--s1"] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "zsh", "path": "/x", "pid": 1, "env": {}}
        fake_tmux["screen"] = "hello"
        answers.append(two.a.get(hub_path(row, reg["handle"], **extra)).text)
    two.a.get(hub_path(BY["task"], reg["handle"], tid="999"))
    two.a.get(hub_path(BY["task"], "nope", tid="1"))
    blob = json.dumps(audit_rows(two.a) + audit_rows(two.b)) + "".join(answers) + caplog.text
    assert PROMPT_MARKER not in blob and token not in blob and pair_token_digest not in blob and code_text not in blob
    for board in (two.a, two.b):
        raw = b"".join(p.read_bytes() for p in board.data_dir.glob("ccboard.db*"))
        assert token.encode() not in raw, "a token is in no database file"
    rows_only = json.dumps(audit_rows(two.a) + audit_rows(two.b))
    assert PROMPT_MARKER not in rows_only and nodes.TOKEN_PREFIX not in rows_only


def test_a_prompt_in_a_request_to_a_guarded_row_is_in_no_audit_row_even_when_it_is_refused(hub, probe_rows, wide):
    two, reg, count = hub
    path = f"/api/nodes/{reg['handle']}/probe"
    two.a.post(path, json={"agent": "claude", "prompt": f"{PROMPT_MARKER} --yolo"})
    two.a.post(path, json={"agent": "claude", "prompt": f"{PROMPT_MARKER} fine"})
    blob = json.dumps(audit_rows(two.a) + audit_rows(two.b))
    assert PROMPT_MARKER not in blob
    assert [x["status"] for x in audit_rows(two.a, action="probe_launch")] == ["ok", "refused"]


def test_the_acting_user_is_a_claim_never_a_credential(hub):
    """A hub can claim any user; the peer records `for <name>` and decides nothing by it (the token and the scope decide)."""
    two, reg, count = hub
    tok = token_of(two, reg)
    for claim in ("root", "alice@example.com", "x" * 500, "bad\tuser"):
        r = two.b.get("/api/node/agents", owner=False, headers=bearer(tok, **{"X-CCBoard-Acting-User": claim}))
        assert r.status_code == 200
    users = [x["user"] for x in audit_rows(two.b, action="read_agents")]
    assert len(users) == 4 and all(u.startswith("for ") and len(u) <= 68 and "\t" not in u for u in users)
    r = two.b.get("/api/node/agents", owner=False, headers=bearer(nodes.TOKEN_PREFIX + "B" * 43, **{"X-CCBoard-Acting-User": "alice@example.com"}))
    assert r.status_code == 401, "naming an allowed login opens nothing"


# ---------------------------------------------------------------- the audit filters

def seed(db, rows):
    for r in rows:
        db.node_audit_add(at=r.get("at", "2026-10-10T00:00:00+00:00"), direction=r["d"], peer=r.get("peer", ""), action=r["a"], status=r.get("s", "ok"),
                          node_name=r.get("n"), user=r.get("u"), target=r.get("t"), detail=r.get("x"))


@pytest.fixture
def filled(lone):
    c, db = lone
    seed(db, [{"d": "out", "a": "read_task", "n": "box", "peer": "p_1111111111111111", "u": "alice@example.com", "t": "task 1"},
              {"d": "out", "a": "read_task", "n": "box", "peer": "p_1111111111111111", "s": "failed", "x": "not_found: no such task"},
              {"d": "out", "a": "read_pane", "n": "desk", "peer": "p_2222222222222222", "s": "refused", "x": "offline"},
              {"d": "in", "a": "read_task", "n": "box", "peer": "p_3333333333333333", "u": "for alice@example.com"},
              {"d": "in", "a": "scope_refused", "n": "desk", "peer": "p_4444444444444444", "s": "refused"},
              {"d": "in", "a": "paired", "n": "laptop", "peer": "p_5555555555555555"}])
    return c, db


def got(c, query: str) -> list[dict]:
    r = c.get("/api/nodes/audit" + query, headers=H)
    assert r.status_code == 200, r.text
    return r.json()["rows"]


def test_the_audit_list_has_no_filter_by_default_and_is_newest_first(filled):
    c, db = filled
    rows = got(c, "")
    assert len(rows) == 6 and [r["id"] for r in rows] == sorted((r["id"] for r in rows), reverse=True)


@pytest.mark.parametrize("query,want", [
    ("?direction=out", 3), ("?direction=in", 3), ("?node=box", 3), ("?node=desk", 2), ("?node=p_3333333333333333", 1), ("?action=read_task", 3),
    ("?action=paired", 1), ("?failures=1", 3), ("?failures=0", 6), ("?direction=out&failures=1", 2), ("?direction=in&node=box", 1), ("?node=box&action=read_task&failures=1", 1),
    ("?direction=in&action=read_pane", 0), ("?node=nobody", 0), ("?action=read_task&direction=out&node=box&failures=1", 1), ("?limit=2", 2), ("?limit=2&direction=in", 2),
])
def test_the_audit_filters_narrow_the_list_together(filled, query, want):
    c, db = filled
    assert len(got(c, query)) == want


def test_a_failure_is_any_status_but_ok(filled):
    c, db = filled
    assert {r["status"] for r in got(c, "?failures=1")} == {"failed", "refused"}


@pytest.mark.parametrize("query", ["?direction=sideways", "?direction=", "?node=" + "x" * 65, "?node=", "?action=Bad Action", "?action=a;b", "?action=" + "a" * 41, "?failures=2", "?failures=yes"])
def test_a_bad_filter_is_a_400_or_422_and_never_a_query(filled, query):
    c, db = filled
    r = c.get("/api/nodes/audit" + query, headers=H)
    assert r.status_code in (400, 422), (query, r.status_code)


def test_a_filter_value_is_a_bound_parameter(filled):
    c, db = filled
    assert got(c, "?node=" + "box' OR '1'='1") == [] and got(c, "?node=x%22%20OR%201%3D1") == []
    assert len(db.node_audit_list(50)) == 6


def test_the_audit_route_stays_a_persons_route(filled):
    c, db = filled
    _, tok = mint(db, nodes.SCOPES)
    assert c.get("/api/nodes/audit", headers=bearer(tok)).status_code == 403
    from app import hooks
    assert c.get("/api/nodes/audit", headers={"X-CCBoard-Token": hooks.ensure_token()}).status_code == 403


# ================================================================ review fixes: redaction, validation, caps, limits (issue #140)

SECRETS = {
    "anthropic": "sk-" + "ant-" + "api03-AbCdEf123456789_xyzQ",
    "openai": "sk-proj-AbCdEfGhIjKlMnOpQrStUv123",
    "github ghp": "ghp_" + "A1b2C3d4E5" * 3,
    "github gho": "gho_" + "Z9y8X7w6V5" * 3,
    "github ghs": "ghs_" + "Q1w2E3r4T5" * 3,
    "github pat": "github_" + "pat_" + "11ABCDEFG0abcdefghijkl_mnopqrstuvwxyz0123",
    "aws": "AKIA" + "IOSFODNN7" + "EXAMPLE",
    "slack": "xox" + "b-" + "123456789012-abcdefABCDEF1234",
    "jwt": "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + ".dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    "three-part base64": "AbCdEfGhIjKlMnOpQr.StUvWxYzAbCdEfGhIj.KlMnOpQrStUvWxYzAbCd",
    "ccbnode": nodes.TOKEN_PREFIX + "Q" * 43,
    "ccbmcp": "ccbmcp_" + "R" * 43,
}
KV_SECRETS = {
    "bearer": ("Authorization-less header Bearer ", "abcdefghijklmnopqrstuvwx"),
    "password": ("password=", "hunter2hunter2"),
    "password colon": ("db_password: ", "correct-horse-battery"),
    "token": ("token = ", "abcd1234efgh"),
    "secret quoted": ("client_secret=", '"has spaces inside"'),
    "api_key": ("api_key=", "k3y-v4lue-0000"),
    "json": ('{"apiKey": ', '"zzzz-1111-yyyy"}'),
    "long hex after =": ("digest=", "a" * 40),
    "long base64 after colon": ("blob: ", "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5"),
}
PEM = "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU\nAAAAB3NzaC1lZDI1NTE5AAAAIP\n-----END OPENSSH " + "PRIVATE KEY-----"


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_redact_replaces_each_secret_shape(name):
    secret = SECRETS[name]
    out = nr.redact(f"before {secret} after")
    assert secret not in out and "[redacted]" in out and out.startswith("before ") and out.endswith(" after"), out


@pytest.mark.parametrize("name", sorted(KV_SECRETS))
def test_redact_replaces_the_value_after_a_secret_looking_key(name):
    lead, value = KV_SECRETS[name]
    out = nr.redact(f"run: {lead}{value} done")
    core = value.strip('"} ')
    assert core not in out and "[redacted]" in out and "run:" in out, out


def test_redact_removes_an_authorization_header_line_and_a_whole_key_block_and_the_tail_of_one():
    assert "dXNlcjpwYXNz" not in nr.redact("Authorization: Basic dXNlcjpwYXNz\nnext line")
    assert nr.redact("Authorization: Basic dXNlcjpwYXNz\nnext line").endswith("next line")
    out = nr.redact(f"ls\n{PEM}\nafter")
    assert "b3BlbnNzaC1rZXk" not in out and "AAAAB3NzaC1lZDI1" not in out and out.startswith("ls\n") and out.endswith("\nafter")
    cut = nr.redact("AAAAB3NzaC1lZDI1NTE5AAAAIP\nmoreBase64Lines==\n-----END OPENSSH PRIVATE KEY-----\nprompt $")
    assert "AAAAB3NzaC1lZDI1" not in cut and cut.endswith("\nprompt $"), "a screen that starts inside a key"
    assert "b3Blbn" not in nr.redact("-----BEGIN RSA " + "PRIVATE KEY-----\nb3BlbnNzaC1r"), "a key whose end is below the screen"


def test_redact_replaces_the_values_this_process_knows_by_value_and_ignores_short_ones():
    out = nr.redact("echo HOOKVALUE-123456 and HUBVALUE-987654 but not abc", ["HOOKVALUE-123456", "HUBVALUE-987654", "abc", "", None])
    assert out == "echo [redacted] and [redacted] but not abc"


@pytest.mark.parametrize("text", ["git status", "commit 1a2b3c4 on main", "See https://example.com/docs/page for details", "error: file not found",
                                  "line 007 hello world", "tokens used: 5", "$ ls -la", "def secret_santa(): pass"])
def test_redact_leaves_ordinary_screen_text_alone(text):
    assert nr.redact(text) == text


def test_a_secret_cannot_hide_behind_an_invisible_character_or_a_control_character():
    zw, bell = chr(0x200B), chr(7)
    for shape in ("sk-ant-" + zw + "api03-abcdefghij", "ghp_" + bell + "A1b2C3d4E5" * 3, "AKIA" + chr(0x00AD) + "IOSFODNN7" + "EXAMPLE"):
        lines = nr.screen_lines(f"x {shape} y", 5)
        assert "[redacted]" in lines[0] and "abcdefghij" not in lines[0] and "IOSFODNN7" not in lines[0] and "A1b2C3d4E5" not in lines[0], lines


def test_the_pane_row_redacts_every_shape_the_hook_token_and_the_hub_token_on_both_boards(hub, fake_tmux, caplog):
    two, reg, count = hub
    caplog.set_level(logging.DEBUG)
    two.b.hub_token = "HUBSECRET-0123456789"
    name = "shop--api--s1"
    fake_tmux["sessions"][name] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "zsh", "path": "/x", "pid": 1, "env": {}}
    planted = [*SECRETS.values(), two.b.hook_token, "HUBSECRET-0123456789", *(v.strip('"} ') for _, v in KV_SECRETS.values()), "b3BlbnNzaC1rZXktdjEAAAAABG5vbmU"]
    fake_tmux["screen"] = "\n".join(["$ env", *SECRETS.values(), f"hook {two.b.hook_token}", "hub HUBSECRET-0123456789", *(a + b for a, b in KV_SECRETS.values()),
                                       PEM, f"visible {SCREEN_MARKER}"])
    via_hub = two.a.get(hub_path(BY["pane"], reg["handle"], name=name))
    direct = two.b.get(fill(BY["pane"].peer_path, name=name), owner=False, headers=bearer(token_of(two, reg)))
    assert via_hub.status_code == direct.status_code == 200
    for blob in (via_hub.text, direct.text, json.dumps(audit_rows(two.a) + audit_rows(two.b)), caplog.text):
        for secret in planted:
            assert secret not in blob, (secret, blob[:200])
    assert SCREEN_MARKER in via_hub.text and "[redacted]" in via_hub.text
    for board in (two.a, two.b):
        raw = b"".join(p.read_bytes() for p in board.data_dir.glob("ccboard.db*"))
        assert all(x.encode() not in raw for x in planted if len(x) > 12 and x != SCREEN_MARKER and x != two.b.hook_token), "a screen is stored nowhere"


def test_the_hub_redacts_again_what_a_careless_peer_sent(two_nodes, pair_up):
    reg = pair_up()
    leak = json.dumps({"name": "shop--api--s1", "lines": ["ok", "key " + "sk-" + "ant-api03-AbCdEf123456789_xyzQ", "password=hunter2hunter2", "x" * 900, "\x1b[31mred\x1b[0m\x00"]}).encode()
    nodes.peer_transport = Answer(200, leak)
    body = two_nodes.a.get(hub_path(BY["pane"], reg["handle"], name="shop--api--s1")).json()["data"]
    text = "\n".join(body["lines"])
    assert "sk-ant" not in text and "hunter2" not in text and all(len(x) <= nr.PANE_LINE_MAX for x in body["lines"]) and "\x1b" not in text and "\x00" not in text


# ---------------------------------------------------------------- what the read rows promise `read`

def test_the_task_and_agents_rows_carry_nothing_beyond_what_read_promises(hub):
    two, reg, count = hub
    tid = add_task(two.b.db, prompt=f"plan {PROMPT_MARKER}", result=f"result {PROMPT_MARKER}", origin=json.dumps({"node": "ts:nX", "user": "carol@example.com"}))
    task = two.a.get(hub_path(BY["task"], reg["handle"], tid=str(tid))).json()["data"]
    assert set(task) == {"id", "title", "phase", "agent", "project", "repo", "branch", "tmux", "issue_ref", "updated_at", "slug", "mode", "base", "created_at",
                         "assigned_at", "done_at", "pr_number", "pr_state", "has_result"}, "a fixed list: no origin or login, no path, no session id"
    assert "carol@example.com" not in json.dumps(task)
    direct = two.b.get(fill(BY["task"].peer_path), owner=False, headers=bearer(token_of(two, reg)))
    assert "carol@example.com" not in direct.text and "origin" not in direct.json()
    agents = two.a.get(hub_path(BY["agents"], reg["handle"])).json()["data"]
    assert set(agents) == {"agents"}
    for a in agents["agents"]:
        assert set(a) == {"name", "label", "glyph", "installed", "version", "logged_in", "hooks", "options", "permission_modes", "efforts", "models", "reasoning_by_model"}


def test_the_answer_envelope_never_reflects_the_peers_keys_at_the_top_level(two_nodes, pair_up):
    reg = pair_up()
    evil = {"id": 5, "title": "t", "node": "forged", "age": 99999, "error": "boom", "reason": "forged", "data": {"x": 1}, "__proto__": {"a": 1}, "prompt": "P"}
    for row, body in ((BY["task"], evil), (BY["agents"], {"agents": [], **evil})):
        nodes.peer_transport = Answer(200, json.dumps(body).encode())
        r = two_nodes.a.get(hub_path(row, reg["handle"], tid="5"))
        assert r.status_code == 200 and set(r.json()) == {"node", "age", "data"} and r.json()["node"] == "node-b" and r.json()["age"] == 0
        assert not {"node", "age", "error", "reason", "data", "__proto__", "prompt"} & set(r.json()["data"]), row.name


# ---------------------------------------------------------------- parameters, caps and text from elsewhere

def test_path_parameters_are_checked_on_both_sides_before_use(hub, fake_tmux):
    two, reg, count = hub
    tok = token_of(two, reg)
    seen = count.seen
    bad_tids = ["1%0A", "%D9%A3", "0x10", "1_0", "+1", "-1", "1.5", "1" * 13, "%20", "1%00"]
    bad_names = ["p--r--s%0A", "%EF%BD%90--r--s", "p--r", "p--r--s--t", "p%2F..--r--s", "p--r--s%00", "a%20b--c--d", "%E2%80%AE--r--s"]
    for t in bad_tids:
        assert two.a.get(f"/api/nodes/{reg['handle']}/tasks/{t}").status_code == 422, t
        assert two.b.get(f"/api/node/tasks/{t}", owner=False, headers=bearer(tok)).status_code in (404, 422), t
    for n in bad_names:
        assert two.a.get(f"/api/nodes/{reg['handle']}/sessions/{n}/pane").status_code in (404, 422), n       # a decoded slash is no route at all
        assert two.b.get(f"/api/node/sessions/{n}/pane", owner=False, headers=bearer(tok)).status_code in (403, 404, 422), n
    assert count.seen == seen, "the hub sent none of them"


@pytest.mark.parametrize("value", ["+5", " 5", "5 ", "0x10", "1_0", "5.0", "0", "41", "100", "-1", "%D9%A4%D9%A0", "", "5e1", "true"])
def test_lines_is_one_or_two_plain_ascii_digits_from_1_to_40(hub, fake_tmux, value):
    two, reg, count = hub
    name = "shop--api--s1"
    fake_tmux["sessions"][name] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "zsh", "path": "/x", "pid": 1, "env": {}}
    seen = count.seen
    r = two.a.get(hub_path(BY["pane"], reg["handle"], name=name) + "?lines=" + value.replace(" ", "%20"))
    assert r.status_code == 422 and count.seen == seen, (value, r.status_code)
    assert two.b.get(fill(BY["pane"].peer_path, name=name) + "?lines=" + value.replace(" ", "%20"), owner=False, headers=bearer(token_of(two, reg))).status_code == 422


def test_text_a_caller_makes_up_is_capped_and_cleaned_before_it_reaches_an_answer_or_an_audit_row(hub, fake_tmux):
    two, reg, count = hub
    tok = token_of(two, reg)
    key = "k" * 400 + "%07" + "ccbnode_" + "Z" * 43
    r = two.b.get(fill(BY["pane"].peer_path) + f"?{key}=1", owner=False, headers=bearer(tok))
    assert r.status_code == 422 and len(r.json()["error"]) <= 130 and "\x07" not in r.text and nodes.TOKEN_PREFIX not in r.text
    r = two.a.get(hub_path(BY["card"], reg["handle"]) + f"?{key}=1")
    assert r.status_code == 422 and len(r.json()["error"]) <= 130 and "\x07" not in r.text and nodes.TOKEN_PREFIX not in r.text
    r = two.b.get("/api/node/tasks/%E2%80%AEevil" + "A" * 300, owner=False, headers=bearer(tok))
    assert r.status_code == 422
    for board in (two.a, two.b):
        for row in audit_rows(board):
            for k in ("target", "user", "detail", "node_name", "action"):
                v = row[k] or ""
                assert v.isprintable() and nodes.TOKEN_PREFIX not in v and len(v) <= {"target": 120, "user": 64, "detail": 200, "node_name": 41, "action": 40}[k], (k, v)
    targets = [x["target"] for x in audit_rows(two.b, action="read_task")]
    assert targets and set(targets) == {"task ?"}, targets


def test_a_peers_error_text_is_capped_and_plain_text_when_it_reaches_the_browser_and_the_audit(two_nodes, pair_up):
    reg = pair_up()
    nasty = "<script>alert(1)</script>" + "x" * 1000 + chr(7) + chr(0x202E) + nodes.TOKEN_PREFIX + "Q" * 43
    nodes.peer_transport = Answer(400, json.dumps({"error": nasty}).encode())
    r = two_nodes.a.get(hub_path(BY["task"], reg["handle"], tid="1"))
    assert r.status_code == 400 and len(r.json()["error"]) <= 160 and chr(7) not in r.text and chr(0x202E) not in r.text and nodes.TOKEN_PREFIX not in r.text
    assert r.headers["content-type"].startswith("application/json") and r.headers["x-content-type-options"] == "nosniff"
    row = audit_rows(two_nodes.a, action="read_task")[0]
    assert len(row["detail"]) <= 200 and nodes.TOKEN_PREFIX not in row["detail"]
    nodes.peer_transport = Answer(404, json.dumps({"error": nasty}).encode())
    assert len(two_nodes.a.get(hub_path(BY["task"], reg["handle"], tid="1")).json()["error"]) <= 160


def test_the_hub_relay_routes_are_rate_limited_per_person_and_class_with_retry_after(hub, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "allowed_users", {"alice@example.com", "bob@example.com"})
    two, reg, count = hub
    monkeypatch.setattr(nodes, "relay_read_limiter", nodes._Limiter(2, 60.0, 8))
    path = hub_path(BY["card"], reg["handle"])
    assert [two.a.get(path).status_code for _ in range(2)] == [200, 200]
    seen = count.seen
    r = two.a.get(path)
    assert r.status_code == 429 and r.json()["reason"] == "rate_limited" and r.headers["retry-after"].isdigit() and count.seen == seen, r.text
    bob = two.a.call("GET", path, owner=False, headers={"Tailscale-User-Login": "bob@example.com", "X-CCBoard": "1"})
    assert bob.status_code == 200, "another person has their own bucket"
    assert nodes.relay_write_limiter.limit == nodes.WRITE_RATE == 30 and nr.BY_NAME["card"].rate_class == "read"
