"""Nodes epic, issue #153 (what stayed open): a peer's audit rows reach the hub. The relay row `audit` (GET /api/nodes/{handle}/audit on the hub, GET /api/node/audit on the node, scope read).

Two in-process boards (tests/conftest.py `two_nodes`, `pair_up`): board a is the hub, board b the node whose audit table is read. Nothing reaches the network, a real tailnet or the real home.

Pinned: the node answers only the rows that name the calling pair, newest first, at most `limit` (1 to 50, 25 by default); another pair's rows, the board's own rows and its own reads of this list
are never in the answer; the hub rebuilds every field (a hostile answer cannot add a field, a bad direction or an over-long text) and stamps its own `received_at`; a secret-shaped text in a row
is replaced; the route is a signed-in person's read (the hook token and a node token are refused), needs the `read` scope of the pair, and draws from the pair's read bucket.
"""
from __future__ import annotations

import json

import pytest

from app import nodes, nodes_relay as nr

ID = {"Tailscale-User-Login": "alice@example.com"}
SECRET = "ccb" + "node_" + "A" * 43                      # a node token's shape, built from fragments


class FakeHub:
    def __init__(self, **rec):
        self.rec = {"status": "online", "age_s": 3, "polled_at": "2026-10-10T00:00:00Z", "last_ok_at": "2026-10-10T00:00:00Z", **rec}

    def records(self, handle=None):
        return [self.rec]


@pytest.fixture(autouse=True)
def _hub_online(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "_hub", lambda: FakeHub())


@pytest.fixture(autouse=True)
def _wide(monkeypatch):
    big = lambda: nodes._Limiter(10 ** 6, 60.0, 64)
    for n in ("node_read_limiter", "node_write_limiter", "relay_read_limiter", "relay_write_limiter"):
        monkeypatch.setattr(nodes, n, big())


@pytest.fixture
def w(two_nodes, pair_up):
    reg = pair_up()
    from types import SimpleNamespace
    return SimpleNamespace(two=two_nodes, a=two_nodes.a, b=two_nodes.b, reg=reg, h=reg["handle"], pid=_pair_id_on_b(two_nodes.b))


def _pair_id_on_b(b) -> str:
    with b.enter():
        rows = [r for r in nodes.peers(db=b.db) if r.get("direction") == "in"]
    assert rows, "board b holds the incoming pair"
    return rows[0]["peer_id"]


def add(board, peer: str, n: int = 1, **kw) -> None:
    for i in range(n):
        board.db.node_audit_add(at=kw.get("at", f"2026-10-17T10:{i % 60:02d}:00Z"), direction=kw.get("direction", "in"), peer=peer, action=kw.get("action", "read_task"),
                                status=kw.get("status", "ok"), node_name=kw.get("node_name", "node-a"), user=kw.get("user"), target=kw.get("target", f"task {i}"), detail=kw.get("detail"))


def read(w, query: str = "", **kw):
    return w.a.get(f"/api/nodes/{w.h}/audit{query}", **kw)


def test_the_hub_reads_the_rows_a_node_keeps_about_this_pair_newest_first_and_stamps_its_own_receipt_time(w):
    add(w.b, w.pid, 3, action="read_state", target="state")
    r = read(w)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["node"] == w.h and j["age"] == 0 and set(j["data"]) == {"rows", "cap"} and j["data"]["cap"] == 50
    rows = j["data"]["rows"]
    assert rows, "the pairing itself left rows on the node"
    ids = [x["id"] for x in rows]
    assert ids == sorted(ids, reverse=True), "newest first"
    for x in rows:
        assert set(x) == {"id", "at", "received_at", "direction", "action", "status", "target", "user", "detail"}
        assert x["direction"] in ("in", "out") and x["received_at"].endswith(("Z", "+00:00"))
    assert [x["action"] for x in rows[:3]] == ["read_state"] * 3 or any(x["action"] == "read_state" for x in rows)


def test_only_rows_that_name_this_pair_come_not_another_pairs_nor_the_boards_own_and_not_its_own_reads(w):
    add(w.b, "p_" + "0" * 16, 2, action="other_pair_row", detail="another pair")
    add(w.b, "", 2, action="settings_set", detail="board setting")
    add(w.b, w.pid, 1, action="mine_row")
    first = read(w).json()["data"]["rows"]
    actions = {x["action"] for x in first}
    assert "mine_row" in actions and "other_pair_row" not in actions and "settings_set" not in actions
    assert "read_audit" not in actions, "a read of the list is not in the list"
    read(w)
    with w.b.enter():
        mine = w.b.db.node_audit_list(500, action="read_audit")
    assert len(mine) >= 2 and all(r["peer"] == w.pid for r in mine), "each read is one audited read on the node"
    assert "read_audit" not in {x["action"] for x in read(w).json()["data"]["rows"]}


def test_the_limit_is_capped_defaults_to_25_and_a_bad_value_is_a_422_on_the_hub_before_any_call(w):
    add(w.b, w.pid, 70)
    assert len(read(w).json()["data"]["rows"]) == 25
    assert len(read(w, "?limit=5").json()["data"]["rows"]) == 5
    assert len(read(w, "?limit=50").json()["data"]["rows"]) == 50
    calls = len(w.two.log)
    for bad in ("0", "51", "-1", "abc", "1.5", "%2B5", "", "5%20"):
        r = read(w, f"?limit={bad}")
        assert r.status_code == 422, (bad, r.status_code, r.text)
    assert read(w, "?limit=5&limit=6").status_code == 422 and read(w, "?lines=5").status_code == 422
    assert len(w.two.log) == calls, "nothing was sent for a bad query"


def test_a_secret_shaped_text_in_a_row_is_replaced_and_the_answer_holds_no_prompt_or_token(w):
    add(w.b, w.pid, 1, action="read_task", detail=f"saw {SECRET} in a note", target="task 7", user="alice")
    text = read(w).text
    assert SECRET not in text and "A" * 43 not in text
    row = next(x for x in read(w).json()["data"]["rows"] if x["target"] == "task 7")
    assert row["user"] == "alice" and row["detail"]


def test_a_hostile_answer_cannot_add_a_field_a_direction_or_a_long_text(w, monkeypatch):
    body = {"rows": [
        {"id": 1, "at": "2026-10-17T10:00:00Z", "direction": "in", "action": "read_task", "status": "ok", "target": "x" * 500, "user": "u" * 300, "detail": "d" * 900, "url": "https://example.test", "token": "t"},
        {"id": 2, "at": "2026-10-17T10:00:01Z", "direction": "sideways", "action": "read_task", "status": "ok"},
        {"id": 3, "at": "not a time <script>", "direction": "out", "action": "read_task", "status": "ok"},
        {"id": 4, "at": "2026-10-17T10:00:02Z", "direction": "out", "action": "Bad Action!", "status": "ok"},
        "not a row", None,
        *[{"id": 10 + i, "at": "2026-10-17T10:01:00Z", "direction": "in", "action": "read_state", "status": "ok"} for i in range(80)]]}
    monkeypatch.setattr(nodes.PeerClient, "request", lambda self, *a, **k: nodes.PeerReply(200, json.dumps(body).encode(), {"content-type": "application/json"}))
    rows = read(w).json()["data"]["rows"]
    assert len(rows) == nr.AUDIT_ROWS_MAX - 5, "only the first 50 entries are looked at, and the five that are no row are dropped"
    first = rows[0]
    assert set(first) == {"id", "at", "received_at", "direction", "action", "status", "target", "user", "detail"}
    assert len(first["target"]) <= 120 and len(first["user"]) <= 64 and len(first["detail"]) <= 160
    assert all(x["direction"] in ("in", "out") and x["action"] != "Bad Action!" and "script" not in x["at"] for x in rows)
    assert 2 not in [x["id"] for x in rows] and 3 not in [x["id"] for x in rows] and 4 not in [x["id"] for x in rows]


def test_a_row_dated_after_the_hub_read_it_is_clamped_to_the_receipt_time_and_keeps_the_nodes_value_as_peer_at(w, monkeypatch):
    body = {"rows": [
        {"id": 1, "at": "9999-12-31T00:00:00Z", "direction": "in", "action": "read_state", "status": "ok"},
        {"id": 2, "at": "2999-01-01T00:00:00+00:00", "direction": "out", "action": "read_state", "status": "ok"},
        {"id": 3, "at": "2026-10-17T10:00:00Z", "direction": "in", "action": "read_state", "status": "ok"},
        {"id": 4, "at": "2026-13-45T99:99:99Z", "direction": "in", "action": "read_state", "status": "ok"}]}
    monkeypatch.setattr(nodes.PeerClient, "request", lambda self, *a, **k: nodes.PeerReply(200, json.dumps(body).encode(), {"content-type": "application/json"}))
    rows = {x["id"]: x for x in read(w).json()["data"]["rows"]}
    for i, original in ((1, "9999-12-31T00:00:00Z"), (2, "2999-01-01T00:00:00+00:00"), (4, "2026-13-45T99:99:99Z")):
        assert rows[i]["at"] == rows[i]["received_at"] and rows[i]["peer_at"] == original, rows[i]
    assert rows[3]["at"] == "2026-10-17T10:00:00Z" and "peer_at" not in rows[3], "a row from the past is left as it is"
    assert all(nodes.iso(x["at"]) <= x["received_at"] for x in rows.values()), "no row is later than the hub's own receipt time"


def test_the_route_is_a_signed_in_persons_read_that_needs_the_read_scope_and_the_pairs_read_bucket(w, two_nodes, pair_up, monkeypatch):
    from app import hooks
    with w.a.enter():
        hook = {"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard": "1"}
    r = w.a.get(f"/api/nodes/{w.h}/audit", headers=hook)
    assert r.status_code == 403 and r.json()["reason"] == "human_only", "the hook token (every agent session) cannot read another board's activity"
    assert nr.BY_NAME["audit"].mcp is False and nr.BY_NAME["audit"].human_only is True
    assert nodes.NODE_ROUTES[("GET", "/api/node/audit")] == "read"
    monkeypatch.setattr(nodes, "node_read_limiter", nodes._Limiter(2, 60.0, 64))
    codes = [read(w).status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and 429 in codes[2:], codes


def test_a_pair_without_the_read_scope_is_not_asked(two_nodes, pair_up):
    reg = pair_up(("tasks",))
    before = len(two_nodes.log)
    r = two_nodes.a.get(f"/api/nodes/{reg['handle']}/audit")
    assert r.status_code == 409 and r.json()["reason"] == "scope" and len(two_nodes.log) == before
