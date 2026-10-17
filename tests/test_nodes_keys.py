"""Ed25519 identity and the wire contract, using temporary boards and no sockets."""
import builtins
import hashlib
import json
import re
import time

import pytest

from app import node_keys as keys, nodes
from app.config import settings
from tests.test_nodes_pairing import clock, db  # noqa: F401


@pytest.fixture
def keyed(two_nodes, clock):
    with two_nodes.b.enter():
        code = nodes.create_code(list(nodes.SCOPES))["code"]
    with two_nodes.a.enter():
        row = nodes.add_node(two_nodes.b.url, code, both_ways=True)
    return two_nodes, row


def headers(two, row, method="GET", target="/api/node", body=None, **kw):
    with two.a.enter():
        token = nodes._load_outgoing(row["peer_id"])
        h = {"Authorization": "Bearer " + token, "X-CCBoard": "1", "X-CCBoard-Node": nodes.node_id()}
        h.update(keys.sign_headers(method, target, body, recipient_id=kw.get("recipient", row["node_id"]),
                                   token=kw.get("token", token), acting_user=kw.get("user")))
        if kw.get("user"):
            h["X-CCBoard-Acting-User"] = kw["user"]
        return h


def test_key_survives_restart_and_has_private_permissions(db):
    pub = keys.public()
    assert keys.valid_public(pub) and len(pub) == 43
    p = settings.data_dir / "node-key"
    assert p.stat().st_mode & 0o777 == 0o600
    assert len(p.read_bytes()) == 52 and p.read_bytes().startswith(b"ed25519:")
    keys.reset()
    assert keys.public() == pub
    assert keys.key_id(pub) == hashlib.sha256(keys._raw(pub, 32)).hexdigest()[:16]
    assert keys.doctor(db)[0] == "pass"
    p.chmod(0o644)
    assert keys.doctor(db)[0] == "fail"


@pytest.mark.parametrize("content", [b"bad", b"\xff", b"ed25519:" + b"A" * 42 + b"=\n"])
def test_corrupt_key_is_quarantined_once(db, content):
    p = settings.data_dir / "node-key"
    p.write_bytes(content)
    assert keys.available()
    bad = p.with_name("node-key.bad")
    assert bad.read_bytes() == content and bad.stat().st_mode & 0o777 == 0o600
    assert "every pair" in keys.doctor(db)[1]
    keys.reset()
    assert keys.doctor(db)[0] == "fail"


def test_unwritable_storage_keeps_one_ephemeral_key(db, monkeypatch):
    def fail(*a):
        raise PermissionError()
    monkeypatch.setattr(nodes, "_write_id_file", fail)
    first = keys.public()
    assert first and first == keys.public()
    assert not (settings.data_dir / "node-key").exists()
    assert "this run only" in keys.doctor(db)[1]


def test_missing_library_and_off_make_no_key(db, monkeypatch):
    def missing():
        raise ImportError()
    monkeypatch.setattr(keys, "_ed", missing)
    assert not keys.available() and keys.card() is None
    assert keys.doctor(db)[0] == "fail"
    monkeypatch.setattr(settings, "node_keys", "off")
    assert not keys.available() and keys.doctor(db)[0] == "warn"
    assert not (settings.data_dir / "node-key").exists()


def test_import_nodes_never_imports_cryptography(projects_dir):
    import os
    import subprocess
    code = '''import builtins
real = builtins.__import__
def guarded(name, *args, **kw):
    if name.startswith("cryptography"):
        raise ImportError("blocked")
    return real(name, *args, **kw)
builtins.__import__ = guarded
from app import nodes, node_keys
assert not node_keys.available()
'''
    import sys
    r = subprocess.run([sys.executable, "-c", code], env={**os.environ, "CCBOARD_DATA_DIR": str(settings.data_dir)}, capture_output=True)
    assert r.returncode == 0


def test_pairing_pins_both_directions_and_card_and_hello(keyed):
    two, row = keyed
    for board in (two.a, two.b):
        with board.enter():
            peers = nodes.peers()
            assert len(peers) == 2 and all(p["key_source"] == "pairing" for p in peers)
            assert len({p["pubkey"] for p in peers}) == 1
            card = nodes.card(board.db)
            assert card["key"]["pub"] == keys.public() and card["key"]["id"] == keys.key_id(keys.public())
            assert set(nodes.hello()) == {"app", "api", "node_id"}
            assert "ed25519:" not in json.dumps(card) + json.dumps(nodes.audit_list())
    assert two.b.get("/api/node", owner=False, headers=headers(two, row)).status_code == 200


@pytest.mark.parametrize("pub", [None, "", "a" * 42, "a" * 44, "!" * 43, "A" * 42 + "B", 123])
def test_pair_body_rejects_bad_public_key_before_spending_code(two_nodes, pub):
    with two_nodes.a.enter():
        claim = nodes.own_claim()
    with two_nodes.b.enter():
        code = nodes.create_code()["code"]
        with pytest.raises(nodes.PairError) as e:
            nodes.handle_pair({"code": code, "node": {**claim, "pubkey": pub}}, "100.64.0.1")
        assert e.value.reason == "bad_request" and nodes.code_status()["active"]


def test_proof_binds_public_key_and_confirm_signature(keyed):
    two, row = keyed
    with two.a.enter():
        claim = nodes.own_claim()
        proof = nodes.confirm_proof("ABCDEFGHJK", "n" * 22, claim["id"], two.a.url, two.b.url, claim["pubkey"])
        old = nodes.confirm_proof("ABCDEFGHJK", "n" * 22, claim["id"], two.a.url, two.b.url)
        assert proof != old
        answer = keys.sign_confirm(proof)
        assert keys.verify_confirm(answer, proof, claim["id"], claim["pubkey"])
        assert not keys.verify_confirm(answer, old, claim["id"], claim["pubkey"])
        changed = nodes.confirm_proof("ABCDEFGHJK", "n" * 22, claim["id"], two.a.url, two.b.url, row["pubkey"])
        assert proof != changed


def test_wrong_confirm_signature_revokes_pair(two_nodes, monkeypatch):
    inner = nodes.peer_transport
    def wrong(target, method, path, hdr, body, timeout):
        status, rh, data = inner(target, method, path, hdr, body, timeout)
        if path == "/api/nodes/pair/confirm":
            answer = json.loads(data)
            answer["sig"] = "A" * 86
            data = json.dumps(answer).encode()
        return status, rh, data
    monkeypatch.setattr(nodes, "peer_transport", wrong)
    with two_nodes.b.enter():
        code = nodes.create_code()["code"]
    with two_nodes.a.enter(), pytest.raises(nodes.PairError) as e:
        nodes.add_node(two_nodes.b.url, code)
    assert e.value.reason == "callback_mismatch"
    assert all(p["revoked_at"] for p in two_nodes.b.db.node_pairs(True))


def test_previous_strict_body_check_retries_once_unkeyed(two_nodes, monkeypatch):
    inner, calls = nodes.peer_transport, []
    def previous(target, method, path, hdr, body, timeout):
        if path == "/api/nodes/pair":
            claim = json.loads(body)["node"]
            calls.append(set(claim))
            # The previous release rejects extra keys before redeem_code.
            if not set(claim) <= {"id", "name", "url", "version"}:
                return 400, {}, json.dumps(nodes.PairError("bad_request").payload()).encode()
        return inner(target, method, path, hdr, body, timeout)
    monkeypatch.setattr(nodes, "peer_transport", previous)
    with two_nodes.b.enter():
        code = nodes.create_code()["code"]
    with two_nodes.a.enter():
        row = nodes.add_node(two_nodes.b.url, code)
    assert len(calls) == 2 and "pubkey" in calls[0] and "pubkey" not in calls[1]
    assert row["pubkey"] is None


@pytest.mark.parametrize("change", ["method", "path", "query", "body", "recipient", "user", "token", "sender"])
def test_every_signed_field_is_bound(keyed, change):
    two, row = keyed
    method, target, body = "POST", "/api/node/tasks?x=1", b'{"title":"one"}'
    h = headers(two, row, method, target, body, user="person", **({"recipient": "n_" + "c" * 16} if change == "recipient" else {}),
                **({"token": nodes._new_token()} if change == "token" else {}))
    if change == "method": method = "PUT"
    if change == "path": target = "/api/node/agents?x=1"
    if change == "query": target = "/api/node/tasks?x=2"
    if change == "body": body = b'{"title":"two"}'
    if change == "user": h["X-CCBoard-Acting-User"] = "someone-else"
    if change == "sender": h["X-CCBoard-Node"] = "n_" + "c" * 16
    r = two.b.call(method, target, owner=False, headers=h, content=body)
    assert r.status_code == 401 and r.json()["reason"] == "bad_signature"


def test_signed_body_reaches_handler_unchanged(keyed, monkeypatch):
    from app import main
    two, row = keyed
    seen = []
    real = nodes.rotate_token
    def rotate(pid, db=None, presented=None):
        seen.append(presented)
        return real(pid, db=db, presented=presented)
    monkeypatch.setattr(nodes, "rotate_token", rotate)
    # The task handler reads the same bytes: malformed JSON gets its own 422, not a middleware failure.
    body = b'{"project":'
    r = two.b.post("/api/node/tasks", owner=False, headers=headers(two, row, "POST", "/api/node/tasks", body), content=body)
    assert r.status_code == 422
    # Exercise the middleware directly with a handler that reads raw body bytes.
    import asyncio
    from starlette.requests import Request
    from starlette.responses import Response
    body = b'{ "message": "exact bytes" }'
    h = headers(two, row, "POST", "/api/node/rotate", body)
    scope = {"type": "http", "method": "POST", "path": "/api/node/rotate", "raw_path": b"/api/node/rotate", "query_string": b"",
             "headers": [(k.lower().encode(), v.encode()) for k, v in {**h, "Content-Length": str(len(body))}.items()], "client": ("100.64.0.1", 1)}
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    async def route(req):
        assert await req.body() == body
        assert req.state.node_peer["key_verified"]
        return Response()
    with two.b.enter():
        assert asyncio.run(main._node_auth(Request(scope, receive), route)).status_code == 200


@pytest.mark.parametrize("mode", ["prefer", "require", "off"])
def test_pinned_pair_never_accepts_unsigned(keyed, monkeypatch, mode):
    two, row = keyed
    h = headers(two, row)
    h.pop("X-CCBoard-Signature")
    monkeypatch.setattr(settings, "node_keys", mode)
    r = two.b.get("/api/node", owner=False, headers=h)
    assert r.status_code == 401 and r.json()["reason"] == "signature_required"


def test_replay_and_restart(keyed, clock, monkeypatch):
    two, row = keyed
    h = headers(two, row)
    assert two.b.get("/api/node", owner=False, headers=h).status_code == 200
    r = two.b.get("/api/node", owner=False, headers=h)
    assert r.json()["reason"] == "replay"
    keys._nonces.clear()
    monkeypatch.setattr(keys, "STARTED", int(clock.t) + 1)
    assert two.b.get("/api/node", owner=False, headers=h).json()["reason"] == "stale"


@pytest.mark.parametrize("delta", [-121, 121])
def test_clock_window(keyed, clock, delta):
    two, row = keyed
    h = headers(two, row)
    clock.t += delta
    r = two.b.get("/api/node", owner=False, headers=h)
    assert r.status_code == 401 and r.json()["reason"] == "clock" and "clock" in r.json()["error"]


def test_new_key_without_rotation_and_id_claim_at_another_address_refused(keyed):
    two, row = keyed
    with two.a.enter():
        keys.reset()
        (settings.data_dir / "node-key").unlink()
        claim = nodes.own_claim()
    r = two.b.get("/api/node", owner=False, headers=headers(two, row))
    assert r.json()["reason"] == "key_changed"
    for url in (two.a.url, "https://100.64.0.9"):
        with two.b.enter():
            code = nodes.create_code()["code"]
            with pytest.raises(nodes.PairError) as e:
                nodes.handle_pair({"code": code, "node": {**claim, "url": url}}, "100.64.0.9")
            assert e.value.reason == "callback_mismatch"


def test_rotation_offline_catchup_acknowledgement_and_restart(keyed, clock):
    two, row = keyed
    with two.a.enter():
        old = keys.public()
        two.offline.add(two.b.name)
        new = keys.rotate()["pub"]
        assert new != old
        keys.reset()
        assert keys.public() == new
        h = headers(two, row)
        assert "X-CCBoard-Key-Rotation" in h
        two.offline.clear()
        reply = nodes.PeerClient(row).get("/api/node")
        assert reply.ok and reply.headers["x-ccboard-key-seen"] == keys.key_id(new)
        assert "X-CCBoard-Key-Rotation" not in headers(two, row)
    with two.b.enter():
        inc = next(p for p in nodes.peers() if p["direction"] == "in")
        assert inc["pubkey"] == new and inc["key_source"] == "rotation"
        assert any(r["action"] == "key_rotated" for r in nodes.audit_list())


@pytest.mark.parametrize("change", ["wrong_old", "old_statement", "wrong_new"])
def test_bad_rotation_never_changes_pin(keyed, clock, change):
    two, row = keyed
    with two.a.enter():
        keys.rotate()
        h = headers(two, row)
        if change == "wrong_old":
            stmt = keys._rotation()
            stmt["chain"][0]["old"] = row["pubkey"]
            h["X-CCBoard-Key-Rotation"] = keys._b64(json.dumps(stmt["chain"]).encode())
        if change == "wrong_new":
            keys.reset()
            (settings.data_dir / "node-key").unlink()
            other = headers(two, row)
            h["X-CCBoard-Signature"] = other["X-CCBoard-Signature"]
        if change == "old_statement":
            clock.t += keys.ROTATION_WINDOW + 1
            fresh = headers(two, row)
            h["X-CCBoard-Signature"] = fresh["X-CCBoard-Signature"]
    before = two.b.db.node_pairs()[0]["peer_pubkey"]
    r = two.b.get("/api/node", owner=False, headers=h)
    assert r.status_code == 401 and r.json()["reason"] == "key_changed"
    assert two.b.db.node_pairs()[0]["peer_pubkey"] == before


def test_upgrade_and_require(two_nodes, clock, monkeypatch):
    with two_nodes.a.enter():
        claim = nodes.own_claim()
    with two_nodes.b.enter():
        peer, token = nodes.add_incoming(claim)
        recipient = nodes.node_id()
        h = {"Authorization": "Bearer " + token, "X-CCBoard": "1"}
        assert two_nodes.b.get("/api/node", owner=False, headers=h).status_code == 200
        assert "node-a" in keys.doctor(two_nodes.b.db)[1]
    monkeypatch.setattr(settings, "node_keys", "require")
    assert two_nodes.b.get("/api/node", owner=False, headers=h).json()["reason"] == "signature_required"
    monkeypatch.setattr(settings, "node_keys", "prefer")
    with two_nodes.a.enter():
        h.update(keys.sign_headers("GET", "/api/node", None, recipient_id=recipient, token=token))
        h.update({"X-CCBoard-Pubkey": keys.public(), "X-CCBoard-Node": nodes.node_id()})
    assert two_nodes.b.get("/api/node", owner=False, headers=h).status_code == 200
    assert two_nodes.b.db.node_pair_get(peer["peer_id"])["peer_key_source"] == "upgrade"
    h.pop("X-CCBoard-Signature")
    assert two_nodes.b.get("/api/node", owner=False, headers=h).json()["reason"] == "signature_required"


def test_card_change_missing_key_and_upgrade(keyed):
    two, row = keyed
    with two.a.enter():
        for card in ({}, {"key": {"pub": keys.public()}}):
            assert keys.check_card(row, card, two.a.db) == "key_changed"
            assert nodes.peer(row["peer_id"])["needs_repair"]
        unkeyed = nodes.put_peer({**row, "pubkey": None, "key_source": None})
        assert keys.check_card(unkeyed, {"key": {"pub": row["pubkey"]}}, two.a.db) is None
        assert nodes.peer(row["peer_id"])["key_source"] == "upgrade"


def test_audit_scrubs_seed_signature_and_token(db):
    seed = "ed25519:" + "A" * 43
    sig = "v1." + "a" * 16 + ".1800000000." + "A" * 22 + "." + "A" * 86
    token = nodes._new_token()
    nodes.audit("in", "", "signature_refused", False, seed + " " + sig + " " + token, db=db)
    detail = nodes.audit_list(db=db)[0]["detail"]
    assert all(secret not in detail for secret in (seed, sig, token))


def test_thousand_sign_verify_rounds_under_one_second(db, clock):
    pub = keys.public()
    message = b"ccboard signature cost sanity check"
    started = time.perf_counter()
    for _ in range(1000):
        assert keys._verify(pub, keys._sign(message), message)
    assert time.perf_counter() - started < 1


def test_rotation_write_failure_keeps_old_key(db, monkeypatch):
    pub = keys.public()
    original = nodes._write_id_file
    def fail_seed(path, value):
        if path.name == "node-key":
            raise OSError()
        original(path, value)
    monkeypatch.setattr(nodes, "_write_id_file", fail_seed)
    with pytest.raises(OSError):
        keys.rotate()
    assert keys.public() == pub
    keys.reset()
    assert keys.public() == pub and keys.rotation_header() is None


def test_starlette_replays_exact_body_to_a_real_route(keyed, monkeypatch):
    from app import main
    from starlette.requests import Request
    from starlette.responses import Response
    two, row = keyed
    path = "/api/node/key-body-probe"
    async def echo(request: Request):
        return Response(await request.body(), media_type="application/octet-stream")
    before = list(main.app.router.routes)
    main.app.add_api_route(path, echo, methods=["POST"])
    monkeypatch.setitem(nodes.NODE_ROUTES, ("POST", path), "tasks")
    try:
        body = b'{ "bytes": "preserve spaces" }\n'
        r = two.b.post(path, owner=False, headers=headers(two, row, "POST", path, body), content=body)
        assert r.status_code == 200 and r.content == body
        assert r.headers["x-ccboard-key-seen"]
    finally:
        main.app.router.routes[:] = before


def test_swapped_pair_key_cannot_downgrade_to_unverified(two_nodes, monkeypatch):
    inner = nodes.peer_transport
    with two_nodes.b.enter():
        substitute = keys.public()
        code = nodes.create_code()["code"]
    def swapped(target, method, path, h, body, timeout):
        if path == "/api/nodes/pair":
            data = json.loads(body)
            data["node"]["pubkey"] = substitute
            body = json.dumps(data).encode()
        return inner(target, method, path, h, body, timeout)
    monkeypatch.setattr(nodes, "peer_transport", swapped)
    with two_nodes.a.enter(), pytest.raises(nodes.PairError) as e:
        nodes.add_node(two_nodes.b.url, code)
    assert e.value.reason == "callback_mismatch"
    assert not two_nodes.b.db.node_pairs()


def test_forbidden_device_cannot_pin_an_upgrade(two_nodes, monkeypatch):
    from app import nodes_whois
    with two_nodes.a.enter():
        claim = nodes.own_claim()
    with two_nodes.b.enter():
        peer, token = nodes.add_incoming(claim)
        recipient = nodes.node_id()
    with two_nodes.a.enter():
        h = {"Authorization": "Bearer " + token, "X-CCBoard": "1", "X-CCBoard-Node": nodes.node_id(), "X-CCBoard-Pubkey": keys.public()}
        h.update(keys.sign_headers("GET", "/api/node", None, recipient_id=recipient, token=token))
    monkeypatch.setattr(nodes_whois, "enabled", lambda: True)
    monkeypatch.setattr(nodes_whois, "pre_check", lambda *a, **k: None)
    from types import SimpleNamespace
    monkeypatch.setattr(nodes_whois, "check_call", lambda *a, **k: SimpleNamespace(status=403, reason="device_mismatch", message="wrong device", retry_after=0))
    assert two_nodes.b.get("/api/node", owner=False, headers=h).status_code == 403
    assert two_nodes.b.db.node_pair_get(peer["peer_id"])["peer_pubkey"] is None


def test_multiple_rotations_keep_offline_peer_chain_even_after_failed_write(keyed, monkeypatch):
    two, row = keyed
    with two.a.enter():
        first = keys.public()
        keys.rotate()
        second = keys.public()
        original = nodes._write_id_file
        def fail(path, value):
            if path.name == "node-key":
                raise OSError()
            original(path, value)
        monkeypatch.setattr(nodes, "_write_id_file", fail)
        with pytest.raises(OSError):
            keys.rotate()
        assert keys.rotated_public(first, keys.rotation_header()) == second
        monkeypatch.setattr(nodes, "_write_id_file", original)
        keys.rotate()
        third = keys.public()
        assert keys.rotated_public(first, keys.rotation_header()) == third
        assert nodes.PeerClient(row).get("/api/node").ok


def test_hub_card_missing_pin_is_unauthorized_and_valid_rotation_catches_up(keyed):
    from app.nodes_hub import NodeHub
    two, row = keyed
    with two.b.enter():
        keys.rotate()
        card = nodes.card(two.b.db)
    with two.a.enter():
        hub = NodeHub(two.a.db)
        hub._fetch = lambda *a: nodes.PeerReply(200, json.dumps(card).encode(), {})
        assert hub._card_update(row, {}).get("hard") is None
        assert nodes.peer(row["peer_id"])["pubkey"] == card["key"]["pub"]
        card.pop("key")
        changed = hub._card_update(row, {})
        assert changed["hard"] == "unauthorized" and changed["error_kind"] == "key_changed"
        assert nodes.peer(row["peer_id"])["needs_repair"]


def test_verified_calls_say_so_in_audit_and_never_store_signature(keyed):
    two, row = keyed
    h = headers(two, row, user="person")
    assert two.b.get("/api/node", owner=False, headers=h).status_code == 200
    with two.b.enter():
        audit = nodes.audit_list()
    assert any(r["action"] == "read_card" and "verified" in (r["detail"] or "") for r in audit)
    text = json.dumps(audit)
    assert h["X-CCBoard-Signature"] not in text and h["Authorization"][7:] not in text


def test_nonce_cache_drops_oldest_and_is_bounded(keyed, clock):
    from collections import OrderedDict
    two, row = keyed
    with two.b.enter():
        peer = next(p for p in nodes.peers() if p["direction"] == "in")
        cache_key = (str(keys._path()), peer["peer_id"])
    keys._nonces[cache_key] = OrderedDict((str(i), clock.t) for i in range(4096))
    assert two.b.get("/api/node", owner=False, headers=headers(two, row)).status_code == 200
    assert len(keys._nonces[cache_key]) == 4096 and "0" not in keys._nonces[cache_key]


def test_stream_and_remote_unpair_use_the_shared_signer(keyed, monkeypatch):
    two, row = keyed
    from app import main
    seen = []
    def stream(target, path, h, timeout):
        seen.append(h)
        with two.b.enter():
            peer = nodes.verify_token(h["Authorization"][7:])
            reason = keys.verify_request("GET", path, b"", headers={k.lower(): v for k, v in h.items()}, peer=peer,
                                         recipient_id=nodes.node_id(), token=h["Authorization"][7:], db=two.b.db)
            assert reason is None and peer["key_verified"]
        return 400, {}, None
    monkeypatch.setattr(nodes, "peer_stream_transport", stream)
    with two.a.enter():
        nodes.PeerClient(row).open_stream("/api/node/stream?names=a--b--c", acting_user="person")
        token = nodes._load_outgoing(row["peer_id"])
        assert nodes._remote_unpair(row["url"], token)
    assert len(seen) == 1
