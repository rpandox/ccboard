"""Nodes epic P3, issue #135: pairing two boards (app/nodes.py, the node_pairs and node_audit tables).

Covers the token store (digest only, the 60 s rotation window, revoke, expiry), the pairing code (50 bits, one active, five wrong tries burn it,
20 attempts a minute per source, one winner among concurrent redeems), the 0600 token file (atomic, a failed write keeps the old one), the registry
(the address rule at save and at call, handles, legacy rows), the outgoing client, the audit (no secret, 90 day prune) and the handshake glue
(handle_pair, add_node, rotate_outgoing, remove_node) over the `two_nodes` fixture: two boards in one process, no socket.

The HTTP routes and the auth middleware branch belong to another area (tests/test_nodes_auth.py and
tests/test_security_surface.py); the handshake tests here put a thin stand-in for those routes (`serve`) in front of each board, so what they prove
is the logic in app/nodes.py, with the same wire shape the routes use. The last test of the file runs the real routes of app/main.py end to end.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading

import pytest

from app import nodes
from app.config import settings
from app.db import DB

SUFFIX = "example.ts.net"
B_URL = "https://100.64.0.2"
CODE_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}")


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(nodes, "_now", c)
    return c


@pytest.fixture
def db(projects_dir):
    d = DB(settings.db_path)
    yield d
    d.conn.close()


def files_text(directory) -> bytes:
    """Every byte of every file under a data dir (the SQLite file, its WAL, the token file), for a secret scan."""
    out = b""
    for p in sorted(directory.rglob("*")):
        if p.is_file():
            out += p.read_bytes()
    return out


def out_peer(db, url=B_URL, name="node-b", scopes=("read", "tasks"), token=True, **kw):
    """A registry row for another board plus (by default) a token for it in the 0600 file; returns (view, token)."""
    v = nodes.put_peer({"direction": "out", "url": url, "name": name, "scopes": list(scopes), "node_id": "n_" + "b" * 16, **kw}, db=db)
    tk = None
    if token:
        tk = nodes._new_token()
        nodes.save_outgoing(v["peer_id"], tk)
    return v, tk


class Recorder:
    """A peer transport that records each call and answers from a script (a callable or a fixed (status, headers, body))."""

    def __init__(self, answer=(200, {}, b"{}")):
        self.answer = answer
        self.calls = []

    def __call__(self, target, method, path, headers, body, timeout):
        self.calls.append({"target": target, "method": method, "path": path, "headers": dict(headers), "body": body, "timeout": timeout})
        return self.answer(self.calls[-1]) if callable(self.answer) else self.answer


# ================================================================ the token store

def test_a_token_is_256_bits_with_a_prefix_and_only_its_digest_is_stored(db, clock):
    row, token = nodes.add_incoming({"id": "n_" + "a" * 16, "name": "node-a", "url": "https://100.64.0.1"}, db=db)
    assert nodes.TOKEN_RE.fullmatch(token) and token.startswith("ccbnode_") and len(token) == len("ccbnode_") + 43
    stored = db.node_pair_get(row["peer_id"])
    assert stored["token_sha256"] == nodes._digest(token) and len(stored["token_sha256"]) == 64
    assert token not in json.dumps(stored) and token not in json.dumps(row) and token not in json.dumps(nodes.peers(db))
    assert nodes._new_token() != nodes._new_token()


def test_verify_token_answers_the_pair_for_a_good_token_and_none_for_everything_else(db, clock):
    row, token = nodes.add_incoming({"id": "n_" + "a" * 16, "name": "node-a"}, db=db)
    got = nodes.verify_token(token, ip="100.64.0.1", db=db)
    assert got["peer_id"] == row["peer_id"] and got["name"] == "node-a" and got["scopes"] == ["read", "tasks"] and got["via_previous"] is False
    assert "token_sha256" not in got and "prev_sha256" not in got
    other = nodes._new_token()
    for bad in (None, "", 5, b"x", "ccbnode_", token[:-1], token + "x", token.replace("ccbnode_", "ccbmcp__"), other, token.upper()):
        assert nodes.verify_token(bad, db=db) is None, bad


def test_verify_token_walks_every_pair_without_stopping_early(db, clock, monkeypatch):
    rows = [nodes.add_incoming({"id": f"n_{i:016x}", "name": f"n{i}"}, db=db) for i in range(4)]
    seen = []
    real = nodes.hmac.compare_digest
    monkeypatch.setattr(nodes.hmac, "compare_digest", lambda a, b: seen.append(1) or real(a, b))
    assert nodes.verify_token(rows[0][1], db=db)["peer_id"] == rows[0][0]["peer_id"]      # the first pair matches ...
    assert len(seen) >= 4                                                                  # ... and all four were still compared


def test_last_used_is_written_when_first_seen_and_then_at_most_every_30_seconds(db, clock):
    row, token = nodes.add_incoming({"id": "n_" + "a" * 16, "name": "node-a"}, db=db)
    assert db.node_pair_get(row["peer_id"])["last_used_at"] is None
    nodes.verify_token(token, ip="100.64.0.1", db=db)
    first = db.node_pair_get(row["peer_id"])
    assert first["last_used_at"] and first["last_ip_hint"] == "100.64.0.1"
    clock.t += 10
    nodes.verify_token(token, ip="100.64.0.7", db=db)
    assert db.node_pair_get(row["peer_id"])["last_used_at"] == first["last_used_at"]       # inside 30 s: no write for the time
    clock.t += 25
    nodes.verify_token(token, db=db)
    assert db.node_pair_get(row["peer_id"])["last_used_at"] > first["last_used_at"]


def test_rotation_new_token_works_at_once_old_one_for_60_seconds(db, clock):
    row, old = nodes.add_incoming({"id": "n_" + "a" * 16, "name": "node-a"}, db=db)
    new = nodes.rotate_token(row["peer_id"], db=db)
    assert nodes.TOKEN_RE.fullmatch(new) and new != old
    assert nodes.verify_token(new, db=db)["via_previous"] is False
    assert nodes.verify_token(old, db=db)["via_previous"] is True
    clock.t += 59
    assert nodes.verify_token(old, db=db) is not None
    clock.t += 2                                                                            # 61 s after the rotation
    assert nodes.verify_token(old, db=db) is None
    assert nodes.verify_token(new, db=db) is not None
    assert db.node_pair_get(row["peer_id"])["rotated_at"]


def test_rotating_again_forgets_the_token_before_the_last_one(db, clock):
    row, t1 = nodes.add_incoming({"id": "n_" + "a" * 16, "name": "node-a"}, db=db)
    t2 = nodes.rotate_token(row["peer_id"], db=db)
    t3 = nodes.rotate_token(row["peer_id"], db=db)
    assert nodes.verify_token(t1, db=db) is None
    assert nodes.verify_token(t2, db=db) and nodes.verify_token(t3, db=db)


def test_revoke_stops_the_token_at_once_and_blanks_the_digests(db, clock):
    row, token = nodes.add_incoming({"id": "n_" + "a" * 16, "name": "node-a"}, db=db)
    nodes.rotate_token(row["peer_id"], db=db)
    assert nodes.revoke(row["peer_id"], db=db) is True
    assert nodes.verify_token(token, db=db) is None
    r = db.node_pair_get(row["peer_id"])
    assert r["revoked_at"] and r["token_sha256"] == "" and r["prev_sha256"] is None
    assert nodes.revoke(row["peer_id"], db=db) is False and nodes.revoke("p_" + "0" * 16, db=db) is False
    assert nodes.peers(db) == []
    with pytest.raises(LookupError):
        nodes.rotate_token(row["peer_id"], db=db)
    with pytest.raises(LookupError):
        nodes.mint_token(row["peer_id"], db=db)


def test_an_expired_pair_does_not_verify(db, clock):
    row, token = nodes.add_incoming({"id": "n_" + "a" * 16, "name": "node-a"}, db=db)
    db.node_pair_update(row["peer_id"], expires_at=nodes.iso(clock.t + 100))
    assert nodes.verify_token(token, db=db) is not None
    clock.t += 101
    assert nodes.verify_token(token, db=db) is None


def test_mint_token_replaces_the_token_and_the_old_one_stops_at_once(db, clock):
    row, old = nodes.add_incoming({"id": "n_" + "a" * 16, "name": "node-a"}, db=db)
    new = nodes.mint_token(row["peer_id"], db=db)
    assert nodes.verify_token(new, db=db) and nodes.verify_token(old, db=db) is None


def test_pairing_the_same_node_again_replaces_its_older_pair(db, clock):
    node = {"id": "n_" + "a" * 16, "name": "node-a"}
    first, t1 = nodes.add_incoming(node, db=db)
    second, t2 = nodes.add_incoming(node, db=db)
    assert nodes.verify_token(t1, db=db) is None and nodes.verify_token(t2, db=db)["peer_id"] == second["peer_id"]
    assert [p["peer_id"] for p in nodes.peers(db)] == [second["peer_id"]]


def test_scopes_are_a_closed_set_in_a_fixed_order(db):
    assert nodes.SCOPES == ("read", "tasks", "sessions", "permissions") and nodes.DEFAULT_SCOPES == ("read", "tasks")
    assert nodes.clean_scopes(["permissions", "read", "read"]) == ["read", "permissions"]
    for bad in ([], None, "read", ["read", "admin"], [1], ["READ"]):
        with pytest.raises(ValueError):
            nodes.clean_scopes(bad)


def test_node_routes_is_a_closed_table_and_route_scope_reads_it(monkeypatch):
    assert nodes.route_scope("GET", "/api/node") == "read" and nodes.route_scope("get", "/api/node") == "read"
    assert nodes.route_scope("GET", "/api/node/summary") == "read"
    assert nodes.route_scope("POST", "/api/node/rotate") == nodes.SCOPE_ANY and nodes.route_scope("POST", "/api/node/unpair") == nodes.SCOPE_ANY
    for method, path in (("POST", "/api/node"), ("GET", "/api/state"), ("GET", "/api/tasks"), ("POST", "/api/hook"), ("POST", "/api/permission"),
                         ("POST", "/api/deploy/gate"), ("GET", "/mcp"), ("GET", "/api/node/hello/x"), ("GET", "/api/node/"), ("DELETE", "/api/node"),
                         ("GET", "/api/nodes"), ("GET", "/api/nodes/pairs"), ("POST", "/api/nodes/pair"), ("GET", "/api/nodes/audit")):
        assert nodes.route_scope(method, path) is None, (method, path)
    assert all(m in ("GET", "POST") and p.startswith("/api/node") for m, p in nodes.NODE_ROUTES)
    assert all(s in nodes.SCOPES or s == nodes.SCOPE_ANY for s in nodes.NODE_ROUTES.values())
    monkeypatch.setitem(nodes.NODE_ROUTES, ("POST", "/api/node/tasks/{id}/cancel"), "tasks")      # a path pattern matches one segment
    assert nodes.route_scope("POST", "/api/node/tasks/42/cancel") == "tasks"
    assert nodes.route_scope("POST", "/api/node/tasks/4/2/cancel") is None
    assert nodes.route_scope("GET", "/api/node/tasks/42/cancel") is None
    assert nodes.scope_ok(["read"], "read") and not nodes.scope_ok(["read"], "tasks") and nodes.scope_ok([], nodes.SCOPE_ANY) and not nodes.scope_ok(["read"], None)


def test_per_pair_buckets_allow_120_reads_and_30_writes_a_minute_then_say_how_long(clock, monkeypatch):
    t = [100.0]
    monkeypatch.setattr(nodes.time, "monotonic", lambda: t[0])
    assert all(nodes.rate_check("p_x")[0] for _ in range(120))
    ok, wait = nodes.rate_check("p_x")
    assert not ok and 1 <= wait <= 61
    assert all(nodes.rate_check("p_x", write=True)[0] for _ in range(30)) and not nodes.rate_check("p_x", write=True)[0]
    assert nodes.rate_check("p_y")[0] and nodes.rate_check("p_y", write=True)[0]               # another pair has its own bucket
    t[0] += 61
    assert nodes.rate_check("p_x")[0]


# ================================================================ pairing codes

def test_a_code_is_ten_crockford_characters_shown_as_two_groups_of_five(db, clock):
    seen = set()
    for _ in range(30):
        c = nodes.create_code(["read"], db=db)
        assert CODE_RE.fullmatch(c["code"])
        seen.add(c["code"])
    assert len(seen) == 30
    assert nodes.CODE_ALPHABET == "0123456789ABCDEFGHJKMNPQRSTVWXYZ" and len(nodes.CODE_ALPHABET) == 32 and nodes.CODE_LEN * 5 == 50


def test_create_code_keeps_a_digest_an_expiry_the_scopes_and_a_counter_and_never_the_code(db, clock):
    c = nodes.create_code(["read", "sessions"], 15, db=db)
    assert c["scopes"] == ["read", "sessions"] and c["expires_at"] == nodes.iso(clock.t + 900)
    rec = db.kv_get("node_pair_code")["value"]
    assert set(rec) == {"sha256", "expires_at", "scopes", "attempts"} and rec["attempts"] == 0 and rec["expires_at"] == clock.t + 900
    flat = c["code"].replace("-", "")
    blob = json.dumps(db.kv_get("node_pair_code")) + json.dumps(db.node_audit_list(50))
    assert c["code"] not in blob and flat not in blob
    assert nodes.code_status(db) == {"active": True, "expires_at": c["expires_at"], "scopes": ["read", "sessions"]}


def test_default_scopes_are_read_and_tasks_and_minutes_are_one_to_thirty(db, clock):
    c = nodes.create_code(db=db)
    assert c["scopes"] == ["read", "tasks"] and c["expires_at"] == nodes.iso(clock.t + 600)
    nodes.create_code(["read"], 1, db=db)
    nodes.create_code(["read"], 30, db=db)
    for bad in (0, 31, -1, "10", 1.5, None, True):
        with pytest.raises(ValueError):
            nodes.create_code(["read"], bad, db=db)
    with pytest.raises(ValueError):
        nodes.create_code(["root"], db=db)
    with pytest.raises(ValueError):
        nodes.create_code([], db=db)


def test_a_new_code_replaces_the_old_one_and_cancel_removes_it(db, clock):
    c1 = nodes.create_code(db=db)
    c2 = nodes.create_code(db=db)
    with pytest.raises(nodes.PairError) as e:
        nodes.redeem_code(c1["code"], "100.64.0.1", db=db)
    assert e.value.reason == "wrong"
    assert nodes.cancel_code(db=db) is True and nodes.cancel_code(db=db) is False
    assert nodes.code_status(db)["active"] is False
    with pytest.raises(nodes.PairError) as e:
        nodes.redeem_code(c2["code"], "100.64.0.1", db=db)
    assert e.value.reason == "none"


def test_redeem_mints_a_pair_with_the_scopes_asked_and_spends_the_code(db, clock):
    c = nodes.create_code(["read", "tasks", "sessions"], db=db)
    r = nodes.redeem_code(c["code"], "100.64.0.1", node={"id": "n_" + "a" * 16, "name": "node-a", "url": "https://100.64.0.1"}, db=db)
    assert r["scopes"] == ["read", "tasks", "sessions"] and PEER_RE.fullmatch(r["peer_id"]) and nodes.TOKEN_RE.fullmatch(r["token"])
    v = nodes.verify_token(r["token"], db=db)
    assert v["scopes"] == ["read", "tasks", "sessions"] and v["node_id"] == "n_" + "a" * 16 and v["url"] == "https://100.64.0.1"
    assert db.kv_get("node_pair_code") is None
    with pytest.raises(nodes.PairError) as e:                                               # a reused code finds none
        nodes.redeem_code(c["code"], "100.64.0.1", db=db)
    assert e.value.reason == "none" and e.value.status == 403


PEER_RE = re.compile(r"p_[0-9a-f]{16}")


def test_a_typed_code_may_be_lower_case_without_dash_and_with_look_alike_letters(db, clock, monkeypatch):
    monkeypatch.setattr(nodes.secrets, "randbits", lambda n: 0)                              # the code 0000000000
    c = nodes.create_code(db=db)
    assert c["code"] == "00000-00000"
    assert nodes.normalize_code("OOOOO-OOOOO") == "0000000000" and nodes.normalize_code(" 00000 00000 ") == "0000000000"
    assert nodes.normalize_code("IL000-00000") == "1100000000" and nodes.normalize_code("abcde-12345") == "ABCDE12345"
    for bad in (None, 5, "", "ABCDE-1234", "ABCDE-123456", "ABCDU-12345", "ABC!E-12345", "x" * 100):
        assert nodes.normalize_code(bad) is None, bad
    r = nodes.redeem_code("ooooo0oooo", "100.64.0.1", db=db)
    assert r["peer_id"]


def test_five_wrong_tries_burn_the_code_and_the_sixth_with_the_right_one_fails(db, clock):
    c = nodes.create_code(db=db)
    for n in range(1, 5):
        with pytest.raises(nodes.PairError) as e:
            nodes.redeem_code("ZZZZZ-ZZZZZ", "100.64.0.1", db=db)
        assert e.value.reason == "wrong"
        assert db.kv_get("node_pair_code")["value"]["attempts"] == n
    with pytest.raises(nodes.PairError) as e:
        nodes.redeem_code("ZZZZZ-ZZZZZ", "100.64.0.1", db=db)
    assert e.value.reason == "burned"
    assert db.kv_get("node_pair_code") is None
    with pytest.raises(nodes.PairError) as e:
        nodes.redeem_code(c["code"], "100.64.0.1", db=db)
    assert e.value.reason == "none"
    assert db.node_pairs() == []


def test_four_wrong_tries_then_the_right_code_still_pairs(db, clock):
    c = nodes.create_code(db=db)
    for _ in range(4):
        with pytest.raises(nodes.PairError):
            nodes.redeem_code("ZZZZZ-ZZZZZ", "100.64.0.1", db=db)
    assert nodes.redeem_code(c["code"], "100.64.0.1", db=db)["peer_id"]


def test_a_malformed_code_counts_as_a_wrong_try(db, clock):
    c = nodes.create_code(db=db)
    for n, bad in enumerate(("", "short", None, 12345), 1):
        with pytest.raises(nodes.PairError) as e:
            nodes.redeem_code(bad, "100.64.0.1", db=db)
        assert e.value.reason == "wrong" and db.kv_get("node_pair_code")["value"]["attempts"] == n
    with pytest.raises(nodes.PairError) as e:
        nodes.redeem_code("x" * 100, "100.64.0.1", db=db)                                    # the fifth, still a shape no code has
    assert e.value.reason == "burned" and db.kv_get("node_pair_code") is None


def test_an_expired_code_fails_and_is_deleted(db, clock):
    c = nodes.create_code(["read"], 10, db=db)
    clock.t += 599
    assert nodes.code_status(db)["active"] is True
    clock.t += 2
    assert nodes.code_status(db)["active"] is False
    with pytest.raises(nodes.PairError) as e:
        nodes.redeem_code(c["code"], "100.64.0.1", db=db)
    assert e.value.reason == "expired" and db.kv_get("node_pair_code") is None and db.node_pairs() == []


def test_twenty_attempts_a_minute_per_source_and_the_limit_is_checked_before_the_code(db, clock, monkeypatch):
    t = [500.0]
    monkeypatch.setattr(nodes.time, "monotonic", lambda: t[0])
    for _ in range(20):                                                                     # no code at all: each answer is "none", each counts
        with pytest.raises(nodes.PairError) as e:
            nodes.redeem_code("ZZZZZ-ZZZZZ", "100.64.0.9", db=db)
        assert e.value.reason == "none"
    c = nodes.create_code(db=db)
    with pytest.raises(nodes.PairError) as e:
        nodes.redeem_code(c["code"], "100.64.0.9", db=db)                                   # the RIGHT code, from the limited source
    assert e.value.reason == "rate_limited" and e.value.status == 429 and e.value.retry_after >= 1
    assert db.kv_get("node_pair_code")["value"]["attempts"] == 0                           # and it did not touch the code
    assert nodes.redeem_code(c["code"], "100.64.0.10", db=db)["peer_id"]                    # another source is not limited
    t[0] += 61
    c = nodes.create_code(db=db)
    assert nodes.redeem_code(c["code"], "100.64.0.9", db=db)["peer_id"]                     # the window moved on


def test_concurrent_redeems_of_one_code_have_exactly_one_winner(db, clock):
    c = nodes.create_code(db=db)
    results, gate = [], threading.Barrier(8)

    def go(i):
        gate.wait()
        try:
            results.append(("ok", nodes.redeem_code(c["code"], f"100.64.1.{i}", db=db)))
        except nodes.PairError as e:
            results.append((e.reason, None))
    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert [r for r, _ in results].count("ok") == 1 and len(db.node_pairs()) == 1


def test_pair_error_has_a_reason_a_status_a_sentence_and_a_payload():
    for reason, status in (("wrong", 403), ("expired", 403), ("burned", 403), ("none", 403), ("rate_limited", 429), ("bad_request", 400),
                           ("callback_mismatch", 409)):
        e = nodes.PairError(reason)
        assert e.reason == reason and e.status == status and str(e) and e.payload() == {"error": str(e), "reason": reason}
    assert nodes.PairError("anything else").reason == "refused"


# ================================================================ the token file (outgoing tokens)

def test_the_token_file_is_0600_and_holds_the_token_only_there(db, projects_dir):
    v, tk = out_peer(db)
    p = nodes.tokens_path()
    assert p == settings.data_dir / "node-tokens.json"
    assert p.stat().st_mode & 0o777 == 0o600
    assert tk.encode() in p.read_bytes()
    assert nodes.has_outgoing(v["peer_id"]) and nodes._load_outgoing(v["peer_id"]) == tk
    assert tk.encode() not in b"".join(f.read_bytes() for f in p.parent.iterdir() if f.is_file() and f != p)      # nowhere else under the data dir
    assert tk not in json.dumps(nodes.peers(db)) and tk not in json.dumps(nodes.peer(v["peer_id"], db))


def test_a_failed_write_keeps_the_old_token_file_whole_and_leaves_no_temp_file(db, projects_dir, monkeypatch):
    v1, t1 = out_peer(db)
    v2, t2 = out_peer(db, url="https://100.64.0.3", name="node-c")
    p = nodes.tokens_path()
    before, mode = p.read_bytes(), p.stat().st_mode & 0o777
    real_replace = os.replace

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        nodes.save_outgoing("p_" + "2" * 16, nodes._new_token())
    assert p.read_bytes() == before and p.stat().st_mode & 0o777 == mode
    assert [f.name for f in p.parent.iterdir() if f.name.endswith(".tmp")] == []
    assert nodes.drop_outgoing(v1["peer_id"]) is False                                      # a failed delete reports False and keeps the token
    assert p.read_bytes() == before
    monkeypatch.setattr(os, "replace", real_replace)
    assert nodes.drop_outgoing(v1["peer_id"]) is True and not nodes.has_outgoing(v1["peer_id"]) and nodes.has_outgoing(v2["peer_id"])
    assert nodes.drop_outgoing(v2["peer_id"]) is True and not p.exists()                          # the last token takes the file with it


def test_save_outgoing_refuses_what_is_not_a_peer_id_and_a_token(projects_dir):
    for pid, tk in (("x", nodes._new_token()), ("p_" + "1" * 16, "ccbnode_short"), ("p_" + "1" * 16, "hook-token")):
        with pytest.raises(ValueError):
            nodes.save_outgoing(pid, tk)
    assert not nodes.tokens_path().exists()


def test_an_unreadable_token_file_is_set_aside_not_overwritten(projects_dir):
    p = nodes.tokens_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"{ not json")
    assert nodes._load_outgoing("p_" + "1" * 16) is None
    assert not p.exists() and p.with_name(p.name + ".bad").read_bytes() == b"{ not json"
    nodes.save_outgoing("p_" + "1" * 16, nodes._new_token())
    assert nodes.has_outgoing("p_" + "1" * 16)


def test_tokens_in_the_file_that_are_not_tokens_are_ignored(projects_dir):
    p = nodes.tokens_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    good = nodes._new_token()
    p.write_text(json.dumps({"v": 1, "tokens": {"p_" + "1" * 16: good, "p_" + "2" * 16: "hook-token", "p_" + "3" * 16: 5}}))
    assert nodes._load_outgoing("p_" + "1" * 16) == good and nodes._load_outgoing("p_" + "2" * 16) is None


# ================================================================ the registry and the address rule

BAD_URLS = ["http://100.64.0.2", "https://8.8.8.8", "https://169.254.169.254", "https://127.0.0.1", "https://localhost", "https://10.0.0.5",
            "https://user@100.64.0.2", "https://100.64.0.2/x", "https://100.64.0.2/?a=1", "https://100.64.0.2/#f", "https://evil.example.com",
            "https://[::ffff:100.64.0.2]", "ftp://100.64.0.2", "", "https://100.64.0.2:0", "https://100.64.0.2:99999", "https://100.64.0.2\\@8.8.8.8",
            "https://100.64.0.2 ", None, 5]


@pytest.mark.parametrize("url", BAD_URLS, ids=[repr(u)[:30] for u in BAD_URLS])
def test_the_address_rule_refuses_at_save_and_writes_nothing(db, url):
    with pytest.raises(nodes.PeerUrlError):
        nodes.put_peer({"direction": "out", "url": url, "name": "x", "scopes": ["read"]}, db=db)
    assert db.kv_get("node_peers") is None and nodes.peers(db) == []


def test_a_tailnet_address_and_a_magic_dns_name_are_saved_normalised(db, monkeypatch):
    monkeypatch.setattr(nodes, "tailnet_suffix", lambda: SUFFIX)
    monkeypatch.setattr(nodes, "_resolve_all", lambda h, p: ["100.64.0.5"])
    a = nodes.put_peer({"direction": "out", "url": "https://100.64.0.2:8443/", "name": "a", "scopes": ["read"]}, db=db)
    b = nodes.put_peer({"direction": "out", "url": "https://Box.EXAMPLE.ts.net./", "name": "b", "scopes": ["read"]}, db=db)
    assert a["url"] == "https://100.64.0.2:8443" and b["url"] == "https://box.example.ts.net"


def test_a_name_that_resolves_outside_the_tailnet_is_refused_at_save(db, monkeypatch):
    monkeypatch.setattr(nodes, "tailnet_suffix", lambda: SUFFIX)
    for answer in (["8.8.8.8"], ["100.64.0.5", "169.254.169.254"], ["127.0.0.1"], []):
        monkeypatch.setattr(nodes, "_resolve_all", lambda h, p, a=answer: a)
        with pytest.raises(nodes.PeerUrlError):
            nodes.put_peer({"direction": "out", "url": f"https://box.{SUFFIX}", "name": "b", "scopes": ["read"]}, db=db)
    assert db.kv_get("node_peers") is None


def test_the_rule_runs_again_at_every_call_a_name_that_now_resolves_elsewhere_is_never_called(db, monkeypatch):
    monkeypatch.setattr(nodes, "tailnet_suffix", lambda: SUFFIX)
    answers = [["100.64.0.5"]]
    monkeypatch.setattr(nodes, "_resolve_all", lambda h, p: answers[0])
    v, tk = out_peer(db, url=f"https://box.{SUFFIX}")
    rec = Recorder((200, {}, b"{}"))
    monkeypatch.setattr(nodes, "peer_transport", rec)
    assert nodes.PeerClient(v, db=db).get("/api/node").ok
    assert rec.calls[0]["target"].addrs == ("100.64.0.5",) and rec.calls[0]["target"].host == f"box.{SUFFIX}"
    answers[0] = ["169.254.169.254"]                                                         # the name now points at the metadata address
    with pytest.raises(nodes.PeerError) as e:
        nodes.PeerClient(v, db=db).get("/api/node")
    assert e.value.reason == "url" and len(rec.calls) == 1                                  # refused before any connection
    assert nodes.peer(v["peer_id"], db)["last_error"] == "url"


def test_a_redirect_answer_is_an_error_and_is_never_followed(db, monkeypatch):
    v, tk = out_peer(db)
    rec = Recorder((302, {"location": "https://169.254.169.254/"}, b""))
    monkeypatch.setattr(nodes, "peer_transport", rec)
    with pytest.raises(nodes.PeerError) as e:
        nodes.PeerClient(v, db=db).get("/api/node")
    assert e.value.reason == "redirect" and len(rec.calls) == 1
    assert nodes.peer(v["peer_id"], db)["last_error"] == "redirect"


def test_handles_come_from_the_name_get_a_suffix_when_taken_and_refuse_local_and_self(db):
    a = nodes.put_peer({"direction": "out", "url": "https://100.64.0.2", "name": "Node_B", "scopes": ["read"]}, db=db)
    b = nodes.put_peer({"direction": "out", "url": "https://100.64.0.3", "name": "Node_B", "scopes": ["read"]}, db=db)
    c = nodes.put_peer({"direction": "out", "url": "https://100.64.0.4", "name": "x", "handle": "node-b", "scopes": ["read"]}, db=db)
    assert (a["handle"], b["handle"], c["handle"]) == ("node_b".replace("_", "-"), "node-b-2", "node-b-3")
    d = nodes.put_peer({"direction": "out", "url": "https://100.64.0.5", "name": "local", "scopes": ["read"]}, db=db)
    assert d["handle"] == "node"                                                              # a name that is a reserved word becomes `node`
    for bad in ("local", "self", "Upper", "-x", "a b", "x" * 32, "a/b"):
        with pytest.raises(ValueError):
            nodes.put_peer({"direction": "out", "url": "https://100.64.0.9", "name": "x", "handle": bad, "scopes": ["read"]}, db=db)
    assert all(nodes.valid_handle(p["handle"]) for p in nodes.peers(db))
    assert {r["handle"] for r in nodes.registry(db)} == {p["handle"] for p in nodes.peers(db)}   # the P1 reader sees every row


def test_peer_finds_a_pair_by_peer_id_or_by_handle(db):
    v, _ = out_peer(db)
    assert nodes.peer(v["peer_id"], db)["handle"] == "node-b" and nodes.peer("node-b", db)["peer_id"] == v["peer_id"]
    assert nodes.peer("nope", db) is None and nodes.peer("", db) is None and nodes.peer(None, db) is None


def test_put_peer_drops_keys_it_does_not_know_so_a_token_never_reaches_the_registry(db):
    secret = nodes._new_token()
    nodes.put_peer({"direction": "out", "url": B_URL, "name": "b", "scopes": ["read"], "token": secret, "token_sha256": "ab" * 32, "authorization": secret}, db=db)
    assert secret not in json.dumps(db.kv_get("node_peers")) and "token" not in json.dumps(db.kv_get("node_peers"))


def test_put_peer_updates_a_row_in_place_and_keeps_its_id_handle_and_time(db):
    v, _ = out_peer(db)
    again = nodes.put_peer({"peer_id": v["peer_id"], "direction": "out", "url": "https://100.64.0.3", "name": "node-b", "scopes": ["read"]}, db=db)
    assert again["peer_id"] == v["peer_id"] and again["handle"] == v["handle"] and again["created_at"] == v["created_at"] and again["url"] == "https://100.64.0.3"
    assert len(nodes.peers(db)) == 1


def test_an_incoming_pair_can_be_changed_but_not_created_through_put_peer(db, clock):
    row, _ = nodes.add_incoming({"id": "n_" + "a" * 16, "name": "node-a"}, db=db)
    v = nodes.put_peer({"peer_id": row["peer_id"], "direction": "in", "url": "https://100.64.0.1", "name": "renamed", "scopes": ["read"],
                        "callback_unverified": True, "token": "x"}, db=db)
    assert v["name"] == "renamed" and v["url"] == "https://100.64.0.1" and v["scopes"] == ["read"] and v["callback_unverified"] is True
    with pytest.raises(ValueError):
        nodes.put_peer({"peer_id": "p_" + "0" * 16, "direction": "in", "name": "ghost"}, db=db)
    with pytest.raises(ValueError):
        nodes.put_peer({"direction": "sideways", "url": B_URL}, db=db)


def test_remove_peer_deletes_the_row_and_the_token_locally_and_empties_the_kv(db):
    v, tk = out_peer(db)
    assert nodes.remove_peer(v["peer_id"], db=db) is True
    assert db.kv_get("node_peers") is None and not nodes.has_outgoing(v["peer_id"]) and nodes.peers(db) == []
    assert nodes.remove_peer(v["peer_id"], db=db) is False and nodes.remove_peer("nope", db=db) is False
    row, token = nodes.add_incoming({"id": "n_" + "a" * 16, "name": "node-a"}, db=db)
    assert nodes.remove_peer(row["peer_id"], db=db) is True and nodes.verify_token(token, db=db) is None


def test_a_board_with_no_pair_has_no_row_no_file_and_no_request(db, projects_dir, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(nodes, "peer_transport", rec)
    assert nodes.peers(db) == [] and nodes.registry(db) == [] and nodes.audit_list(10, db=db) == []
    assert nodes.code_status(db)["active"] is False and nodes.cancel_code(db=db) is False
    assert nodes.import_legacy([], db=db) == []
    assert db.kv_get("node_peers") is None and db.kv_get("node_pair_code") is None
    assert not nodes.tokens_path().exists() and rec.calls == []
    assert db.conn.execute("SELECT (SELECT COUNT(*) FROM node_pairs) + (SELECT COUNT(*) FROM node_audit)").fetchone()[0] == 0


def test_legacy_rows_are_read_only_have_no_token_and_follow_the_environment(db):
    got = nodes.import_legacy([{"name": "ubu", "url": "https://ubu.tailnet.ts.net:8443"}, {"name": "box2", "url": "https://box2.tailnet.ts.net/"}], db=db)
    assert [(r["handle"], r["legacy"], r["scopes"]) for r in got] == [("ubu", True, ["read"]), ("box2", True, ["read"])]
    assert got[1]["url"] == "https://box2.tailnet.ts.net"
    again = nodes.import_legacy([{"name": "ubu", "url": "https://ubu.tailnet.ts.net:8443"}, {"name": "box2", "url": "https://box2.tailnet.ts.net"}], db=db)
    assert [r["peer_id"] for r in again] == [r["peer_id"] for r in got]                     # idempotent
    assert nodes.import_legacy([{"name": "ubu", "url": "https://ubu.tailnet.ts.net:8443"}], db=db)[0]["peer_id"] == got[0]["peer_id"]
    assert [p["handle"] for p in nodes.peers(db)] == ["ubu"]                                  # the removed entry is gone
    assert not nodes.tokens_path().exists()
    with pytest.raises(nodes.PeerError) as e:
        nodes.PeerClient(nodes.peers(db)[0], db=db).get("/api/node")
    assert e.value.reason == "no_token"


# ================================================================ the outgoing client

def test_a_call_carries_the_bearer_the_header_and_the_node_id_and_nothing_that_is_an_identity_or_an_origin(db, projects_dir, monkeypatch):
    v, tk = out_peer(db)
    rec = Recorder((200, {"Content-Type": "application/json"}, b'{"node_id":"n_x"}'))
    monkeypatch.setattr(nodes, "peer_transport", rec)
    r = nodes.PeerClient(v, db=db).get("/api/node", acting_user="alice")
    assert r.ok and r.json == {"node_id": "n_x"} and r.headers["content-type"] == "application/json"
    c = rec.calls[0]
    assert c["method"] == "GET" and c["path"] == "/api/node" and c["body"] is None and c["target"].addrs == ("100.64.0.2",) and c["target"].port == 443
    h = c["headers"]
    assert h["Authorization"] == "Bearer " + tk and h["X-CCBoard"] == "1" and h["X-CCBoard-Node"] == nodes.node_id() and h["X-CCBoard-Acting-User"] == "alice"
    assert not {k.lower() for k in h} & {"origin", "tailscale-user-login", "x-ccboard-token", "cookie"}


def test_a_post_sends_json_within_the_cap_and_a_big_body_is_refused_before_sending(db, monkeypatch):
    v, _ = out_peer(db)
    rec = Recorder()
    monkeypatch.setattr(nodes, "peer_transport", rec)
    nodes.PeerClient(v, db=db).post("/api/node/rotate", {"a": 1})
    assert rec.calls[0]["body"] == b'{"a":1}' and rec.calls[0]["headers"]["Content-Type"] == "application/json"
    with pytest.raises(nodes.PeerError) as e:
        nodes.PeerClient(v, db=db).post("/api/node/rotate", {"x": "y" * (nodes.BODY_MAX + 1)})
    assert e.value.reason == "too_large" and len(rec.calls) == 1


@pytest.mark.parametrize("path", ["api/node", "/api/../x", "//evil/x", "/api/node?a b", "http://100.64.0.9/api", "/api/node\r\nX: y", "", None, "/a#b"])
def test_only_a_plain_path_on_the_peer_is_called(db, monkeypatch, path):
    v, _ = out_peer(db)
    rec = Recorder()
    monkeypatch.setattr(nodes, "peer_transport", rec)
    with pytest.raises(nodes.PeerError) as e:
        nodes.PeerClient(v, db=db).get(path)
    assert e.value.reason == "bad_path" and rec.calls == []


def test_an_answer_over_the_cap_is_an_error(db, monkeypatch):
    v, _ = out_peer(db)
    monkeypatch.setattr(nodes, "peer_transport", Recorder((200, {}, b"x" * (nodes.RESP_MAX + 1))))
    with pytest.raises(nodes.PeerError) as e:
        nodes.PeerClient(v, db=db).get("/api/node")
    assert e.value.reason == "too_large"


def test_a_transport_failure_is_a_peer_error_that_names_the_class_and_not_the_message(db, monkeypatch):
    v, tk = out_peer(db)

    def boom(*a):
        raise ConnectionRefusedError(f"Authorization: Bearer {tk}")
    monkeypatch.setattr(nodes, "peer_transport", boom)
    with pytest.raises(nodes.PeerError) as e:
        nodes.PeerClient(v, db=db).get("/api/node")
    assert e.value.reason == "unreachable" and tk not in str(e.value) and "ConnectionRefusedError" in str(e.value)
    assert nodes.peer(v["peer_id"], db)["last_error"] == "unreachable"


def test_a_401_marks_the_pair_needs_repair_and_a_good_answer_clears_it_and_records_last_seen(db, clock, monkeypatch):
    v, _ = out_peer(db)
    answers = [(401, {}, b"{}"), (200, {}, b"{}")]
    monkeypatch.setattr(nodes, "peer_transport", lambda *a: answers.pop(0))
    nodes.PeerClient(v, db=db).get("/api/node")
    p = nodes.peer(v["peer_id"], db)
    assert p["needs_repair"] is True and p["last_error"] == "the peer answered 401" and p["last_seen"] is None
    nodes.PeerClient(v, db=db).get("/api/node")
    p = nodes.peer(v["peer_id"], db)
    assert p["needs_repair"] is False and p["last_error"] is None and p["last_seen"] == nodes.iso(clock.t)


def test_a_403_or_a_500_is_a_last_error_but_not_a_repair(db, monkeypatch):
    v, _ = out_peer(db)
    monkeypatch.setattr(nodes, "peer_transport", Recorder((403, {}, b"{}")))
    r = nodes.PeerClient(v, db=db).get("/api/node")
    assert r.status == 403 and not r.ok
    p = nodes.peer(v["peer_id"], db)
    assert p["needs_repair"] is False and p["last_error"] == "the peer answered 403"


def test_last_seen_is_written_at_most_every_30_seconds(db, clock, monkeypatch):
    v, _ = out_peer(db)
    monkeypatch.setattr(nodes, "peer_transport", Recorder())
    writes = []
    real = db.kv_set
    monkeypatch.setattr(db, "kv_set", lambda *a, **k: writes.append(a[0]) or real(*a, **k))
    nodes.PeerClient(v, db=db).get("/api/node")
    n = len(writes)
    clock.t += 5
    nodes.PeerClient(v, db=db).get("/api/node")
    assert len(writes) == n                                                                  # a poll inside 30 s writes nothing
    clock.t += 30
    nodes.PeerClient(v, db=db).get("/api/node")
    assert len(writes) == n + 1


def test_a_peer_with_no_token_in_the_file_is_not_called(db, monkeypatch):
    v, _ = out_peer(db, token=False)
    rec = Recorder()
    monkeypatch.setattr(nodes, "peer_transport", rec)
    with pytest.raises(nodes.PeerError) as e:
        nodes.PeerClient(v, db=db).get("/api/node")
    assert e.value.reason == "no_token" and rec.calls == []


def test_the_real_transport_connects_to_the_validated_address_and_checks_the_name(monkeypatch):
    seen = {}

    class Resp:
        status = 200

        def __init__(self):
            self.chunks = [b'{"ok":', b'true}']

        def read(self, n):
            return self.chunks.pop(0) if self.chunks else b""

        def getheaders(self):
            return [("Content-Type", "application/json"), ("Retry-After", "3")]

    class Conn:
        sock = None

        def __init__(self, host, addr, port, timeout):
            seen["ctor"] = (host, addr, port)

        def request(self, method, path, body=None, headers=None):
            seen["req"] = (method, path, body, dict(headers))

        def getresponse(self):
            return Resp()

        def close(self):
            seen["closed"] = True
    from app import nodes_discovery
    monkeypatch.setattr(nodes_discovery, "_Pinned", Conn)
    target = nodes.PeerTarget("https://box.example.ts.net:8443", "box.example.ts.net", 8443, ("100.64.0.5", "100.64.0.6"))
    status, headers, body = nodes._https(target, "POST", "/api/node/rotate", {"X-CCBoard": "1"}, b"{}", 5.0)
    assert (status, body) == (200, b'{"ok":true}') and headers == {"content-type": "application/json", "retry-after": "3"}
    assert seen["ctor"] == ("box.example.ts.net", "100.64.0.5", 8443) and seen["req"][0:2] == ("POST", "/api/node/rotate") and seen["closed"]


# ================================================================ the audit

def test_audit_writes_a_row_with_both_directions_and_never_raises(db, clock):
    nodes.audit("in", "p_1", "code_used", True, "scopes read", node_name="node-a", user="alice", target="x", db=db)
    nodes.audit("out", "p_2", "paired", False, "nope", db=db)
    nodes.audit("sideways", "p_2", "x", True, db=db)
    rows = nodes.audit_list(10, db=db)
    assert [(r["direction"], r["action"], r["status"]) for r in rows] == [("in", "x", "ok"), ("out", "paired", "failed"), ("in", "code_used", "ok")]
    assert rows[2]["at"] == nodes.iso(clock.t) and rows[2]["node_name"] == "node-a" and rows[2]["user"] == "alice"
    nodes.audit("in", "p", "x", True, "d", db=object())                                      # a broken db: no exception
    assert nodes.audit_list(5, db=object()) == []


def test_audit_replaces_anything_that_looks_like_a_token_a_code_or_a_digest(db):
    tk = nodes._new_token()
    nodes.audit("in", "p_1", "x", True, f"got {tk} and 7K3QM-ZX9AB and {nodes._digest(tk)} and ccbmcp_abcdefghijkl", user="a\x00b\nc", db=db)
    (r,) = nodes.audit_list(5, db=db)
    assert tk not in r["detail"] and "7K3QM-ZX9AB" not in r["detail"] and nodes._digest(tk) not in r["detail"] and "ccbmcp_abcdefghijkl" not in r["detail"]
    assert r["detail"] == "got [token] and [code] and [digest] and [token]" and r["user"] == "abc"
    nodes.audit("in", "p_1", "x", True, "y" * 500, db=db)
    assert len(nodes.audit_list(1, db=db)[0]["detail"]) == nodes.AUDIT_DETAIL_MAX


def test_rows_older_than_90_days_are_pruned_on_a_write_at_most_once_an_hour(db, clock, monkeypatch):
    old = nodes.iso(clock.t - 91 * 86400)
    new = nodes.iso(clock.t - 89 * 86400)
    db.node_audit_add(at=old, direction="in", peer="p", action="old", status="ok")
    db.node_audit_add(at=new, direction="in", peer="p", action="recent", status="ok")
    t = [1000.0]
    monkeypatch.setattr(nodes.time, "monotonic", lambda: t[0])
    nodes.audit("in", "p", "now", True, db=db)
    assert [r["action"] for r in nodes.audit_list(10, db=db)] == ["now", "recent"]
    db.node_audit_add(at=old, direction="in", peer="p", action="old2", status="ok")
    t[0] += 60
    nodes.audit("in", "p", "later", True, db=db)
    assert "old2" in [r["action"] for r in nodes.audit_list(10, db=db)]                     # not pruned again inside the hour
    t[0] += 3600
    nodes.audit("in", "p", "much later", True, db=db)
    assert "old2" not in [r["action"] for r in nodes.audit_list(10, db=db)]


def test_every_pair_event_on_the_accepting_side_writes_a_row(db, clock):
    c = nodes.create_code(db=db)
    nodes.cancel_code(db=db)
    nodes.create_code(db=db)
    with pytest.raises(nodes.PairError):
        nodes.redeem_code("ZZZZZ-ZZZZZ", "100.64.0.1", db=db)
    for _ in range(4):
        with pytest.raises(nodes.PairError):
            nodes.redeem_code("ZZZZZ-ZZZZZ", "100.64.0.1", db=db)
    c = nodes.create_code(db=db)
    r = nodes.redeem_code(c["code"], "100.64.0.1", node={"id": "n_" + "a" * 16, "name": "node-a"}, db=db)
    nodes.rotate_token(r["peer_id"], db=db)
    nodes.revoke(r["peer_id"], db=db)
    got = {x["action"] for x in nodes.audit_list(100, db=db)}
    assert {"code_created", "code_cancelled", "pair_refused", "code_burned", "code_used", "rotated", "revoked"} <= got
    assert all(x["status"] in ("ok", "refused") for x in nodes.audit_list(100, db=db))


def test_audit_rows_hold_no_token_no_code_and_no_digest_after_a_full_cycle(db, clock):
    c = nodes.create_code(db=db)
    r = nodes.redeem_code(c["code"], "100.64.0.1", node={"id": "n_" + "a" * 16, "name": "node-a", "url": "https://100.64.0.1"}, db=db)
    new = nodes.rotate_token(r["peer_id"], db=db)
    blob = json.dumps(nodes.audit_list(100, db=db)) + json.dumps(nodes.peers(db)) + json.dumps(db.kv_get("node_pair_code"))
    for secret in (r["token"], new, c["code"], c["code"].replace("-", ""), nodes._digest(r["token"]), nodes._digest(new)):
        assert secret not in blob


# ================================================================ the handshake over two boards

def serve(two, board, method, path, headers, body):
    """What the routes of app/main.py do, in a few lines: the wire shape the glue under test talks to. Runs inside `board.enter()`."""
    h = {k.lower(): v for k, v in headers.items()}
    j = json.loads(body) if body else None

    def out(status, data, extra=None):
        return status, {"content-type": "application/json", **(extra or {})}, json.dumps(data).encode()

    def authed(need):
        a = h.get("authorization", "")
        pair = nodes.verify_token(a[7:], ip="100.64.0.9") if a.startswith("Bearer ") else None
        if pair is None:
            return None, out(401, {"error": "bad token"})
        if not nodes.scope_ok(pair["scopes"], nodes.route_scope(method, path)):
            return None, out(403, {"error": "scope"})
        return pair, None
    if (method, path) == ("GET", "/api/node/hello"):
        return out(200, nodes.hello())
    if (method, path) == ("POST", "/api/nodes/pair"):
        if "origin" in h or h.get("x-ccboard") != "1":
            return out(403, {"error": "refused"})
        try:
            return out(200, nodes.handle_pair(j, "100.64.0.9"))
        except nodes.PairError as e:
            return out(e.status, e.payload(), {"retry-after": str(e.retry_after)} if e.retry_after else None)
    if (method, path) == ("GET", "/api/node"):
        pair, err = authed("read")
        return err or out(200, {"node_id": nodes.node_id(), "name": nodes.display_name()})
    if (method, path) == ("POST", "/api/node/rotate"):
        pair, err = authed(None)
        return err or out(200, {"token": nodes.rotate_token(pair["peer_id"])})
    if (method, path) == ("POST", "/api/node/unpair"):
        pair, err = authed(None)
        return err or out(200, {"ok": nodes.unpair_incoming(pair["peer_id"])})
    return out(404, {"error": "no such route"})


@pytest.fixture
def pair2(two_nodes, monkeypatch, caplog):
    """two_nodes with `serve` standing in for the routes."""
    caplog.set_level(logging.DEBUG)

    def transport(target, method, path, headers, body, timeout):
        board = two_nodes.boards.get((target.host, target.port))
        if board is None or board.name in two_nodes.offline:
            raise ConnectionRefusedError("no board there")
        caller = settings.node_name
        with board.enter():
            status, hdrs, data = serve(two_nodes, board, method, path, headers, body)
        two_nodes.log.append((caller, method, board.name, path, status))
        return status, hdrs, data
    two_nodes.transport = transport
    monkeypatch.setattr(nodes, "peer_transport", transport)
    return two_nodes


def claim_a(two) -> dict:
    """The card A sends when it pairs: its real node id, name and address."""
    with two.a.enter():
        return {"id": nodes.node_id(), "name": "node-a", "url": two.a.url, "version": "1"}


def pair(two, scopes=("read", "tasks"), **kw):
    """B makes the code, A adds B. Returns (A's registry view, the code)."""
    with two.b.enter():
        code = nodes.create_code(list(scopes))["code"]
    with two.a.enter():
        return nodes.add_node(two.b.url, code, **kw), code


def test_the_fixture_gives_two_boards_with_their_own_ids_data_dirs_and_databases(two_nodes):
    a, b = two_nodes.a, two_nodes.b
    assert a.data_dir != b.data_dir and a.db is not b.db and a.hook_token != b.hook_token
    with a.enter():
        ida = nodes.node_id()
        assert settings.node_name == "node-a" and settings.data_dir == a.data_dir
    with b.enter():
        idb = nodes.node_id()
    assert ida != idb and nodes.ID_RE.fullmatch(ida) and (a.data_dir / "node-id").read_text().strip() == ida
    assert settings.data_dir not in (a.data_dir, b.data_dir)                                    # the swap was undone
    assert a.get("/api/node/hello").json()["node_id"] == ida and b.get("/api/node/hello").json()["node_id"] == idb


def test_the_fixture_transport_goes_to_the_board_named_and_refuses_an_offline_or_unknown_one(two_nodes, monkeypatch):
    from app import nodes as n
    r = n.peer_call(two_nodes.b.url, "GET", "/api/node/hello")
    assert r.ok and r.json["app"] == "ccboard" and two_nodes.log[-1][2:] == ("node-b", "/api/node/hello", 200)
    two_nodes.offline.add("node-b")
    with pytest.raises(n.PeerError) as e:
        n.peer_call(two_nodes.b.url, "GET", "/api/node/hello")
    assert e.value.reason == "unreachable"
    with pytest.raises(n.PeerError):
        n.peer_call("https://100.64.0.77", "GET", "/api/node/hello")


def test_flaky_transport_fails_the_first_calls_then_lets_them_through(two_nodes, monkeypatch):
    from tests.conftest import FlakyTransport
    flaky = FlakyTransport(two_nodes.transport, fail_first=2, match="hello")
    monkeypatch.setattr(nodes, "peer_transport", flaky)
    for _ in range(2):
        with pytest.raises(nodes.PeerError):
            nodes.peer_call(two_nodes.b.url, "GET", "/api/node/hello")
    assert nodes.peer_call(two_nodes.b.url, "GET", "/api/node/hello").ok
    assert (flaky.seen, flaky.failed) == (3, 2)


def test_pairing_gives_a_token_that_reads_the_card_and_the_registry_rows_on_both_sides(pair2):
    view, code = pair(pair2)
    assert view["direction"] == "out" and view["handle"] == "node-b" and view["url"] == pair2.b.url and view["scopes"] == ["read", "tasks"]
    assert view["last_seen"] and view["last_error"] is None and view["needs_repair"] is False and view["legacy"] is False
    with pair2.a.enter():
        assert nodes.has_outgoing(view["peer_id"])
        assert nodes.PeerClient(view).get("/api/node").json["name"] == "node-b"
        assert nodes.peers()[0]["peer_id"] == view["peer_id"]
    with pair2.b.enter():
        (inc,) = nodes.peers()
        assert inc["direction"] == "in" and inc["name"] == "node-a" and inc["url"] == pair2.a.url and inc["scopes"] == ["read", "tasks"]
        assert inc["callback_unverified"] is False and inc["last_seen"]
        assert nodes.code_status()["active"] is False
    assert [c[1:] for c in pair2.log if c[0] == "node-a"][:2] == [("POST", "node-b", "/api/nodes/pair", 200), ("GET", "node-b", "/api/node", 200)]
    assert ("node-b", "GET", "node-a", "/api/node/hello", 200) in pair2.log                       # B checked the callback


def test_the_scopes_asked_on_the_code_are_the_scopes_granted(pair2):
    view, _ = pair(pair2, scopes=("read", "sessions", "permissions"))
    assert view["scopes"] == ["read", "sessions", "permissions"]
    with pair2.b.enter():
        assert nodes.peers()[0]["scopes"] == ["read", "sessions", "permissions"]


def test_a_wrong_code_a_burned_code_an_expired_code_and_a_reused_code_never_pair(pair2, clock):
    with pair2.b.enter():
        good = nodes.create_code()["code"]
    with pair2.a.enter():
        for n in range(4):
            with pytest.raises(nodes.PairError) as e:
                nodes.add_node(pair2.b.url, "ZZZZZ-ZZZZZ")
            assert e.value.reason == "wrong"
        with pytest.raises(nodes.PairError) as e:
            nodes.add_node(pair2.b.url, "ZZZZZ-ZZZZZ")
        assert e.value.reason == "burned"
        with pytest.raises(nodes.PairError) as e:
            nodes.add_node(pair2.b.url, good)                                                  # the sixth try, with the right code
        assert e.value.reason == "none"
        assert nodes.peers() == [] and not nodes.tokens_path().exists()
    with pair2.b.enter():
        good = nodes.create_code(minutes=1)["code"]
    clock.t += 61
    with pair2.a.enter():
        with pytest.raises(nodes.PairError) as e:
            nodes.add_node(pair2.b.url, good)
        assert e.value.reason == "expired"
    view, code = pair(pair2)
    with pair2.a.enter():
        with pytest.raises(nodes.PairError) as e:
            nodes.add_node(pair2.b.url, code)                                                  # a used code
        assert e.value.reason == "none"
    with pair2.b.enter():
        assert db_pairs(pair2.b) == 1


def db_pairs(board) -> int:
    return len(board.db.node_pairs())


def test_a_rate_limited_pair_attempt_carries_retry_after_to_the_caller(pair2, monkeypatch):
    with pair2.b.enter():
        nodes.create_code()
    t = [10.0]
    monkeypatch.setattr(nodes.time, "monotonic", lambda: t[0])
    with pair2.a.enter():
        for _ in range(20):
            with pytest.raises(nodes.PairError):
                nodes.add_node(pair2.b.url, "ZZZZZ-ZZZZZ")
        with pytest.raises(nodes.PairError) as e:
            nodes.add_node(pair2.b.url, "ZZZZZ-ZZZZZ")
        assert e.value.reason == "rate_limited" and e.value.retry_after >= 1


def test_a_bad_code_shape_or_address_or_handle_is_refused_before_anything_is_sent(pair2):
    with pair2.a.enter():
        for code, url, kw in (("short", pair2.b.url, {}), ("ABCDE-12345", "http://100.64.0.2", {}), ("ABCDE-12345", "https://8.8.8.8", {}),
                              ("ABCDE-12345", pair2.b.url, {"handle": "local"}), ("ABCDE-12345", pair2.b.url, {"handle": "Bad Handle"})):
            with pytest.raises(ValueError):
                nodes.add_node(url, code, **kw)
    assert pair2.log == []


def test_the_pair_request_is_read_strictly_and_a_bad_one_neither_spends_the_code_nor_makes_a_request(pair2):
    with pair2.b.enter():
        code = nodes.create_code()["code"]
        me = claim_a(pair2)
        bad_bodies = [
            None, [], {}, {"code": code}, {"code": code, "node": me, "permission_mode": "bypassPermissions"},
            {"code": code, "node": {**me, "sandbox": "danger-full-access"}}, {"code": code, "node": {**me, "id": "root"}},
            {"code": code, "node": {**me, "name": "a b!"}}, {"code": 5, "node": me}, {"code": code, "node": {**me, "version": 3}},
            {"code": code, "node": me, "reverse": {"token": "hook-token"}}, {"code": code, "node": me, "reverse": {"token": nodes._new_token(), "scopes": ["root"]}},
            {"code": code, "node": me, "reverse": {"token": nodes._new_token(), "mode": "x"}}, {"code": code, "node": me, "scopes": ["read", "permissions"]},
        ]
        for body in bad_bodies:
            with pytest.raises(nodes.PairError) as e:
                nodes.handle_pair(body, "100.64.0.9")
            assert e.value.reason == "bad_request", body
        for url in ("http://100.64.0.1", "https://8.8.8.8", "https://evil.example.com", "https://100.64.0.1/x"):
            with pytest.raises(nodes.PairError) as e:
                nodes.handle_pair({"code": code, "node": {**me, "url": url}}, "100.64.0.9")
            assert e.value.reason == "bad_url", url
        assert nodes.code_status()["active"] is True and nodes.audit_list(20) == [x for x in nodes.audit_list(20) if x["action"] == "code_created"]
    assert pair2.log == [] and db_pairs(pair2.b) == 0
    with pair2.b.enter():
        assert nodes.handle_pair({"code": code, "node": me}, "100.64.0.9")["token"]            # and the code is still good


def test_nothing_leaves_the_accepting_board_before_the_code_is_right(pair2):
    with pair2.b.enter():
        nodes.create_code()
        me = claim_a(pair2)
        with pytest.raises(nodes.PairError):
            nodes.handle_pair({"code": "ZZZZZ-ZZZZZ", "node": me}, "100.64.0.9")
    assert pair2.log == [] and db_pairs(pair2.b) == 0


def test_a_callback_that_names_another_node_refuses_the_pair_and_revokes_it(pair2):
    with pair2.b.enter():
        code = nodes.create_code()["code"]
        liar = {"id": "n_" + "c" * 16, "name": "node-c", "url": pair2.a.url}                      # the address belongs to A, the claim says c
        with pytest.raises(nodes.PairError) as e:
            nodes.handle_pair({"code": code, "node": liar}, "100.64.0.9")
        assert e.value.reason == "callback_mismatch" and e.value.status == 409
        assert nodes.peers() == [] and len(pair2.b.db.node_pairs()) == 0
        assert pair2.b.db.node_pairs(include_revoked=True)[0]["revoked_at"] and pair2.b.db.node_pairs(include_revoked=True)[0]["token_sha256"] == ""
        assert nodes.code_status()["active"] is False
        assert "pair_refused" in {r["action"] for r in nodes.audit_list(20)}


def test_an_unreachable_callback_pairs_with_the_flag_and_nothing_breaks(pair2):
    pair2.offline.add("node-a")                                                                   # A is the caller, and its address does not answer B
    with pair2.b.enter():
        code = nodes.create_code()["code"]
        me = claim_a(pair2)
        r = nodes.handle_pair({"code": code, "node": me}, "100.64.0.9")
        assert r["callback_unverified"] is True and nodes.verify_token(r["token"])
        (inc,) = nodes.peers()
        assert inc["callback_unverified"] is True
        assert "callback_unverified" in {x["action"] for x in nodes.audit_list(20)}


def test_a_callback_address_that_breaks_the_rule_at_call_time_refuses_the_pair(pair2, monkeypatch):
    monkeypatch.setattr(nodes, "tailnet_suffix", lambda: SUFFIX)
    monkeypatch.setattr(nodes, "_resolve_all", lambda h, p: ["100.64.0.1"])
    with pair2.b.enter():
        code = nodes.create_code()["code"]
        me = {**claim_a(pair2), "url": f"https://node-a.{SUFFIX}"}
        monkeypatch.setattr(nodes, "_resolve_all", lambda h, p: ["8.8.8.8"])                      # it resolves elsewhere now
        with pytest.raises(nodes.PairError) as e:
            nodes.handle_pair({"code": code, "node": me}, "100.64.0.9")
        assert e.value.reason == "bad_url" and nodes.peers() == []


def test_a_second_pairing_of_the_same_node_replaces_the_first_and_keeps_the_handle(pair2):
    first, _ = pair(pair2)
    second, _ = pair(pair2)
    assert second["peer_id"] == first["peer_id"] and second["handle"] == first["handle"]
    with pair2.a.enter():
        assert len(nodes.peers()) == 1
    with pair2.b.enter():
        assert len(nodes.peers()) == 1 and len(pair2.b.db.node_pairs(include_revoked=True)) == 2          # the older one is revoked, not listed


def test_a_taken_handle_gets_a_suffix(pair2):
    with pair2.a.enter():
        nodes.import_legacy([{"name": "node-b", "url": "https://other.tailnet.ts.net"}])
    view, _ = pair(pair2)
    assert view["handle"] == "node-b-2" and view["legacy"] is False
    view2, _ = pair(pair2, handle="mine")
    assert view2["handle"] == "node-b-2"                                                            # re-pairing the same node keeps the row, handle included


def test_pairing_an_address_that_is_a_legacy_row_replaces_that_row(pair2):
    with pair2.a.enter():
        (legacy,) = nodes.import_legacy([{"name": "b", "url": pair2.b.url}])
        assert legacy["legacy"] is True and legacy["scopes"] == ["read"]
    view, _ = pair(pair2)
    with pair2.a.enter():
        (only,) = nodes.peers()
        assert only["peer_id"] == legacy["peer_id"] == view["peer_id"] and only["legacy"] is False and only["handle"] == "b"
        assert only["scopes"] == ["read", "tasks"] and nodes.has_outgoing(only["peer_id"])


def test_both_ways_makes_a_working_reverse_token_with_read_and_tasks(pair2):
    view, _ = pair(pair2, scopes=("read", "tasks", "sessions"), both_ways=True)
    with pair2.a.enter():
        ins = [p for p in nodes.peers() if p["direction"] == "in"]
        assert len(ins) == 1 and ins[0]["scopes"] == ["read", "tasks"] and ins[0]["name"] == "node-b" and ins[0]["url"] == pair2.b.url
        assert ins[0]["node_id"] == view["node_id"]
    with pair2.b.enter():
        outs = [p for p in nodes.peers() if p["direction"] == "out"]
        assert len(outs) == 1 and outs[0]["name"] == "node-a" and outs[0]["scopes"] == ["read", "tasks"] and outs[0]["url"] == pair2.a.url
        assert nodes.PeerClient(outs[0]).get("/api/node").json["name"] == "node-a"               # B reads A's card with the reverse token
    with pair2.a.enter():
        assert nodes.PeerClient(view).get("/api/node").json["name"] == "node-b"                  # and A still reads B's


def test_both_ways_that_cannot_be_kept_still_pairs_one_way(pair2, monkeypatch):
    with pair2.b.enter():
        code = nodes.create_code()["code"]
        monkeypatch.setattr(nodes, "save_outgoing", lambda *a: (_ for _ in ()).throw(OSError("full")))
        r = nodes.handle_pair({"code": code, "node": claim_a(pair2),
                               "reverse": {"token": nodes._new_token(), "scopes": ["read", "tasks"]}}, "100.64.0.9")
        assert r["reverse"] is False and r["token"] and [p["direction"] for p in nodes.peers()] == ["in"]


def test_both_ways_that_fails_to_pair_revokes_the_reverse_token(pair2):
    with pair2.a.enter():
        with pytest.raises(nodes.PairError):
            nodes.add_node(pair2.b.url, "ZZZZZ-ZZZZZ", both_ways=True)
        assert nodes.peers() == []
        assert all(r["revoked_at"] for r in pair2.a.db.node_pairs(include_revoked=True))
        assert pair2.a.db.node_pairs() == []


def test_if_this_board_cannot_keep_the_token_nothing_is_paired_and_the_other_side_is_told(pair2, monkeypatch):
    with pair2.b.enter():
        code = nodes.create_code()["code"]
    with pair2.a.enter():
        monkeypatch.setattr(nodes, "_write_tokens", lambda d: (_ for _ in ()).throw(OSError("read-only")))
        with pytest.raises(nodes.PairError) as e:
            nodes.add_node(pair2.b.url, code)
        assert e.value.reason == "store" and nodes.peers() == []
    with pair2.b.enter():
        assert nodes.peers() == []                                                                 # B was told to drop the pair it had made


def test_a_dead_other_board_is_an_error_that_says_so_and_leaves_no_row(pair2):
    with pair2.b.enter():
        code = nodes.create_code()["code"]
    pair2.offline.add("node-b")
    with pair2.a.enter():
        with pytest.raises(nodes.PairError) as e:
            nodes.add_node(pair2.b.url, code)
        assert e.value.reason == "unreachable" and nodes.peers() == [] and not nodes.tokens_path().exists()


def test_rotating_from_the_calling_side_swaps_the_token_and_the_old_one_works_60_seconds(pair2, clock):
    view, _ = pair(pair2)
    with pair2.a.enter():
        old = nodes._load_outgoing(view["peer_id"])
        v2 = nodes.rotate_outgoing(view["peer_id"])
        new = nodes._load_outgoing(view["peer_id"])
        assert new != old and v2["peer_id"] == view["peer_id"]
        assert nodes.PeerClient(v2).get("/api/node").ok                                            # the new token works at once
    with pair2.b.enter():
        assert nodes.verify_token(old) is not None and nodes.verify_token(new) is not None
        clock.t += 61
        assert nodes.verify_token(old) is None and nodes.verify_token(new) is not None
    with pair2.a.enter():
        nodes.save_outgoing(view["peer_id"], old)                                                  # a board that kept the old one is refused: needs repair
        assert nodes.PeerClient(v2).get("/api/node").status == 401 and nodes.peer(view["peer_id"])["needs_repair"] is True
        actions = {r["action"] for r in nodes.audit_list(50)}
        assert {"paired", "rotated"} <= actions


def test_rotating_when_the_new_token_cannot_be_saved_marks_the_pair_needs_repair(pair2, monkeypatch):
    view, _ = pair(pair2)
    with pair2.a.enter():
        monkeypatch.setattr(nodes, "_write_tokens", lambda d: (_ for _ in ()).throw(OSError("full")))
        with pytest.raises(nodes.PairError) as e:
            nodes.rotate_outgoing(view["peer_id"])
        assert e.value.reason == "store" and nodes.peer(view["peer_id"])["needs_repair"] is True


def test_rotating_an_unknown_or_an_incoming_pair_from_here_is_a_lookup_error(pair2):
    view, _ = pair(pair2, both_ways=True)
    with pair2.a.enter():
        inc = next(p for p in nodes.peers() if p["direction"] == "in")
        for ident in ("nope", inc["peer_id"]):
            with pytest.raises(LookupError):
                nodes.rotate_outgoing(ident)


def test_removing_a_node_tells_the_other_board_which_drops_its_pair_both_ways(pair2):
    view, _ = pair(pair2, both_ways=True)
    with pair2.a.enter():
        res = nodes.remove_node(view["handle"])
        assert res == {"removed": True, "peer_told": True}
        assert nodes.peers() == [] and not nodes.has_outgoing(view["peer_id"])                      # the row, the token and the reverse pair
    with pair2.b.enter():
        assert nodes.peers() == []                                                                 # B dropped the pair A held and its own token for A
    assert "unpair" in {r["action"] for r in nodes.audit_list(20, db=pair2.a.db)}


def test_removing_a_node_while_the_other_board_is_off_still_removes_everything_here(pair2):
    view, _ = pair(pair2, both_ways=True)
    pair2.offline.add("node-b")
    with pair2.a.enter():
        old = nodes._load_outgoing(view["peer_id"])
        res = nodes.remove_node(view["peer_id"])
        assert res == {"removed": True, "peer_told": False}
        assert nodes.peers() == [] and not nodes.has_outgoing(view["peer_id"]) and not nodes.tokens_path().exists()
        assert nodes.remove_peer(view["peer_id"]) is False
        rows = [r for r in nodes.audit_list(20) if r["action"] == "unpair"]
        assert rows and rows[0]["status"] == "failed" and "not told" in rows[0]["detail"]
    pair2.offline.clear()
    with pair2.b.enter():
        assert nodes.verify_token(old) is not None                                                # B still has the pair: it is only cut on this side
    with pair2.a.enter():
        with pytest.raises(LookupError):
            nodes.remove_node(view["peer_id"])


def test_a_legacy_row_is_removed_without_calling_anyone(pair2):
    with pair2.a.enter():
        (legacy,) = nodes.import_legacy([{"name": "ubu", "url": "https://ubu.tailnet.ts.net"}])
        assert nodes.remove_node("ubu") == {"removed": True, "peer_told": False}
    assert pair2.log == []


def test_unpair_incoming_cuts_the_pair_and_the_outgoing_pair_to_the_same_node(pair2):
    view, _ = pair(pair2, both_ways=True)
    with pair2.b.enter():
        (inc,) = [p for p in nodes.peers() if p["direction"] == "in"]
        assert nodes.unpair_incoming(inc["peer_id"]) is True
        assert nodes.peers() == [] and nodes.unpair_incoming(inc["peer_id"]) is False and nodes.unpair_incoming("p_" + "0" * 16) is False


# ================================================================ nothing secret in the database, the logs or the answers

def test_after_a_full_cycle_no_token_code_or_digest_is_in_a_database_a_log_an_audit_row_or_an_answer(pair2, caplog):
    with pair2.b.enter():
        code = nodes.create_code(["read", "tasks"])
    with pair2.a.enter():
        view = nodes.add_node(pair2.b.url, code["code"], both_ways=True)
        tok_a = nodes._load_outgoing(view["peer_id"])
        nodes.rotate_outgoing(view["peer_id"])
        tok_a2 = nodes._load_outgoing(view["peer_id"])
        answers = json.dumps(nodes.peers()) + json.dumps(nodes.audit_list(200)) + json.dumps(view)
    with pair2.b.enter():
        (inc,) = [p for p in nodes.peers() if p["direction"] == "in"]
        reverse_tok = nodes._load_outgoing(next(p for p in nodes.peers() if p["direction"] == "out")["peer_id"])
        answers += json.dumps(nodes.peers()) + json.dumps(nodes.audit_list(200)) + json.dumps(nodes.code_status())
    secrets = [tok_a, tok_a2, reverse_tok, code["code"], code["code"].replace("-", ""), nodes._digest(tok_a), nodes._digest(tok_a2), nodes._digest(reverse_tok)]
    for s in secrets:
        assert s not in answers, "an answer holds a secret"
        assert s not in caplog.text, "a log line holds a secret"
        assert s not in repr(pair2.log), "the call log holds a secret"
    a_bytes, b_bytes = files_text(pair2.a.data_dir), files_text(pair2.b.data_dir)
    for s in (tok_a, tok_a2, reverse_tok, code["code"], code["code"].replace("-", "")):
        s = s.encode()
        db_a = b"".join(p.read_bytes() for p in pair2.a.data_dir.glob("ccboard.db*"))
        db_b = b"".join(p.read_bytes() for p in pair2.b.data_dir.glob("ccboard.db*"))
        assert s not in db_a and s not in db_b, "a database holds a secret"
    assert tok_a2.encode() in a_bytes and tok_a.encode() not in (pair2.a.data_dir / "node-tokens.json").read_bytes()    # A's file holds only the current token
    assert reverse_tok.encode() in b_bytes
    assert nodes._digest(tok_a2).encode() in b"".join(p.read_bytes() for p in pair2.b.data_dir.glob("ccboard.db*"))      # B's database holds the digest
    for board in (pair2.a, pair2.b):
        f = board.data_dir / "node-tokens.json"
        if f.exists():
            assert f.stat().st_mode & 0o777 == 0o600


# ================================================================ over the real routes (app/main.py)

def test_two_boards_pair_over_the_real_routes_and_the_token_opens_the_card_and_nothing_else(two_nodes):
    a, b = two_nodes.a, two_nodes.b
    made = b.post("/api/nodes/pair-code", json={"scopes": ["read", "tasks"], "minutes": 10})
    assert made.status_code == 200
    code = made.json()["code"]
    added = a.post("/api/nodes", json={"url": b.url, "code": code})
    assert added.status_code == 201
    row = added.json()
    assert row["handle"] == "node-b" and row["scopes"] == ["read", "tasks"] and "token" not in json.dumps(row)
    with a.enter():
        client = nodes.PeerClient(nodes.peers()[0])
        assert client.get("/api/node").json["name"] == "node-b"                                  # the card, with the token
        assert client.get("/api/state").status == 403                                             # and nothing else
        assert client.get("/api/tasks").status == 403
        token = nodes._load_outgoing(row["peer_id"])
    reuse = a.post("/api/nodes", json={"url": b.url, "code": code})
    assert reuse.status_code == 403 and reuse.json()["reason"] == "none"
    pairs = b.get("/api/nodes/pairs").json()["pairs"]
    listed = json.dumps(pairs) + a.get("/api/nodes").text + b.get("/api/nodes").text
    assert len(pairs) == 1 and pairs[0]["name"] == "node-a"
    assert token not in listed and nodes._digest(token) not in listed                           # no token and no digest in any list answer
    assert a.delete(f"/api/nodes/{row['peer_id']}").status_code == 200
    assert b.get("/api/nodes/pairs").json()["pairs"] == []
    for board in (a, b):
        blob = b"".join(p.read_bytes() for p in board.data_dir.glob("ccboard.db*"))
        assert token.encode() not in blob and code.replace("-", "").encode() not in blob


def test_a_flood_of_refused_pair_attempts_cannot_fill_the_audit_but_a_burned_code_is_always_written(db, clock):
    for i in range(60):
        with pytest.raises(nodes.PairError):
            nodes.redeem_code("ZZZZZ-ZZZZZ", f"100.64.2.{i}", db=db)                       # no code at all, 60 different sources
    refused = [r for r in nodes.audit_list(500, db=db) if r["action"] == "pair_refused"]
    assert len(refused) == 30
    nodes.create_code(db=db)
    for _ in range(5):
        with pytest.raises(nodes.PairError):
            nodes.redeem_code("ZZZZZ-ZZZZZ", "100.64.3.1", db=db)
    assert [r["action"] for r in nodes.audit_list(500, db=db)].count("code_burned") == 1
