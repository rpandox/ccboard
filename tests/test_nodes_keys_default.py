"""Issue #271: CCBOARD_NODE_KEYS is `require` by default and the default never cuts a pair that has no key yet.

The suite runs as if the setting were typed (tests/conftest.py `_node_keys_typed`); the tests here turn that off to look at the default."""
import pytest

from app import node_keys as keys, nodes
from app.config import Settings, settings
from tests.test_nodes_pairing import clock, db  # noqa: F401
from tests.test_nodes_keys import headers, keyed  # noqa: F401


@pytest.fixture
def untyped(monkeypatch):
    """CCBOARD_NODE_KEYS not set: the default, which looks at the pairs."""
    monkeypatch.setattr(settings, "node_keys", "require")
    monkeypatch.setattr(settings, "node_keys_explicit", False)


@pytest.fixture
def one_unkeyed(two_nodes, clock, untyped):
    """Board b has one finished pair that calls it with no signing key. Returns (two_nodes, its row, the request headers that call with its token)."""
    with two_nodes.a.enter():
        claim = nodes.own_claim()
    with two_nodes.b.enter():
        peer, token = nodes.add_incoming(claim)
        recipient = nodes.node_id()
    return two_nodes, peer, {"Authorization": "Bearer " + token, "X-CCBoard": "1"}, token, recipient


def test_the_setting_knows_whether_it_was_typed():
    assert Settings({}).node_keys == "require" and not Settings({}).node_keys_explicit
    assert not Settings({"CCBOARD_NODE_KEYS": "  "}).node_keys_explicit, "the installer stores an empty value"
    for value in ("require", "prefer", "off", "requrie"):
        assert Settings({"CCBOARD_NODE_KEYS": value}).node_keys_explicit


@pytest.mark.parametrize("configured, explicit, unkeyed, expect", [
    ("require", False, [], "require"),
    ("require", False, ["old-laptop"], "prefer"),
    ("require", True, ["old-laptop"], "require"),
    ("prefer", False, ["old-laptop"], "prefer"),
    ("prefer", True, [], "prefer"),
    ("off", True, ["old-laptop"], "off"),
])
def test_decide(configured, explicit, unkeyed, expect):
    assert keys.decide(configured, explicit, unkeyed) == expect


def test_no_pairs_is_require(db, untyped):
    assert keys.mode(db) == "require"
    assert keys.view(db) == {"mode": "require", "setting": "require", "explicit": False, "unkeyed": [], "unkeyed_count": 0}
    assert keys.doctor(db)[0] == "pass"


def test_every_pair_keyed_is_require(keyed, untyped):
    two, _ = keyed
    for board in (two.a, two.b):
        with board.enter():
            assert keys.mode(board.db) == "require"
            assert keys.view(board.db)["unkeyed_count"] == 0
            assert keys.doctor(board.db)[0] == "pass"


def test_one_unkeyed_pair_is_prefer_and_is_not_cut(one_unkeyed):
    two, peer, h, _, _ = one_unkeyed
    with two.b.enter():
        assert keys.mode(two.b.db) == "prefer"
        view = keys.view(two.b.db)
        assert view["mode"] == "prefer" and view["setting"] == "require" and view["explicit"] is False
        assert view["unkeyed"] == [peer["name"] or "a node"] and view["unkeyed_count"] == 1
        status, detail = keys.doctor(two.b.db)
        assert status == "warn", "a warning, not a failure"
        assert "1 node pair has no signing key: keys become required once it is re-paired" in detail
        assert (peer["name"] or "a node") in detail
    assert two.b.get("/api/node", owner=False, headers=h).status_code == 200, "the update does not stop a pair that has no key"
    state = two.b.get("/api/state").json()
    assert state["node_keys"] == {"mode": "prefer", "setting": "require", "explicit": False, "unkeyed": view["unkeyed"], "unkeyed_count": 1}
    assert state["node"]["key_mode"] == "prefer", "the card says what the board enforces"


def test_the_state_key_is_always_there(two_nodes, untyped):
    assert two_nodes.a.get("/api/state").json()["node_keys"] == {"mode": "require", "setting": "require", "explicit": False, "unkeyed": [], "unkeyed_count": 0}


def test_typed_require_refuses_an_unkeyed_pair_at_once(one_unkeyed, monkeypatch):
    two, peer, h, _, _ = one_unkeyed
    monkeypatch.setattr(settings, "node_keys_explicit", True)
    with two.b.enter():
        assert keys.mode(two.b.db) == "require"
        assert keys.view(two.b.db)["mode"] == "require"
        status, detail = keys.doctor(two.b.db)
        assert status == "warn" and keys.KEY_REQUIRED_MESSAGE in detail
    refused = two.b.get("/api/node", owner=False, headers=h)
    assert refused.status_code == 401 and refused.json()["reason"] == "key_required"


def test_typed_prefer_stays_prefer(two_nodes, clock, monkeypatch):
    monkeypatch.setattr(settings, "node_keys", "prefer")
    monkeypatch.setattr(settings, "node_keys_explicit", True)
    with two_nodes.b.enter():
        assert keys.mode(two_nodes.b.db) == "prefer"
    with two_nodes.a.enter():
        claim = nodes.own_claim()
    with two_nodes.b.enter():
        nodes.add_incoming(claim)
        assert keys.mode(two_nodes.b.db) == "prefer" and keys.view(two_nodes.b.db)["mode"] == "prefer"
        assert "still work" in keys.doctor(two_nodes.b.db)[1]


def test_keying_the_last_unkeyed_pair_flips_to_require_without_a_restart(one_unkeyed):
    two, peer, h, token, recipient = one_unkeyed
    with two.b.enter():
        assert keys.mode(two.b.db) == "prefer"
    with two.a.enter():                                      # the other board is updated: its first signed call carries its key, and the pair pins it
        h.update(keys.sign_headers("GET", "/api/node", None, recipient_id=recipient, token=token))
        h.update({"X-CCBoard-Pubkey": keys.public(), "X-CCBoard-Node": nodes.node_id()})
    assert two.b.get("/api/node", owner=False, headers=h).status_code == 200
    assert two.b.db.node_pair_get(peer["peer_id"])["peer_key_source"] == "upgrade"
    with two.b.enter():
        assert keys.mode(two.b.db) == "require", "read from the pairs on every call: no restart"
        assert keys.view(two.b.db)["unkeyed_count"] == 0 and keys.doctor(two.b.db)[0] == "pass"
    assert two.b.get("/api/state").json()["node_keys"]["mode"] == "require"


def test_removing_the_last_unkeyed_pair_flips_to_require_too(one_unkeyed):
    two, peer, _, _, _ = one_unkeyed
    with two.b.enter():
        assert keys.mode(two.b.db) == "prefer"
        two.b.db.node_pair_delete(peer["peer_id"])
        assert keys.mode(two.b.db) == "require"


def test_a_pair_that_is_not_finished_or_is_legacy_does_not_hold_the_default_back(two_nodes, clock, untyped):
    with two_nodes.a.enter():
        claim = nodes.own_claim()
    with two_nodes.b.enter():
        nodes.add_incoming(claim, pending=True)
        assert keys.mode(two_nodes.b.db) == "require" and keys.view(two_nodes.b.db)["unkeyed_count"] == 0


def test_a_unreadable_database_keeps_the_configured_mode(untyped):
    class Broken:
        def node_pairs(self, *a, **k):
            raise RuntimeError("no database")

        def kv_get(self, *a, **k):
            raise RuntimeError("no database")

    assert keys.mode(Broken()) == "require"
    assert keys.view(Broken())["mode"] == "require"


def _keyless_pairing(two, monkeypatch):
    """Board a pairs to board b with a code, while a has no signing key to send (a board without key support). Returns what happened."""
    with two.b.enter():
        code = nodes.create_code()["code"]
    monkeypatch.setattr(keys, "available", lambda: False)
    monkeypatch.setattr(keys, "public", lambda: None)
    with two.a.enter():
        return nodes.add_node(two.b.url, code)


def test_held_at_prefer_a_new_pairing_from_a_keyless_board_is_refused(one_unkeyed, monkeypatch):
    two, *_ = one_unkeyed
    with two.b.enter():
        assert keys.mode(two.b.db) == "prefer" and keys.pairing_mode() == "require"
    with two.a.enter():
        assert keys.mode(two.a.db) == "require"
    with pytest.raises(nodes.PairError) as err:
        _keyless_pairing(two, monkeypatch)
    assert err.value.reason == "key_required"
    with two.b.enter():
        assert keys.view(two.b.db)["unkeyed_count"] == 1, "the refused pairing left no pair behind; the old unkeyed one still works"


def test_the_acceptor_in_the_held_state_refuses_a_keyless_new_pairing_before_it_spends_the_code(one_unkeyed):
    two, *_ = one_unkeyed
    with two.b.enter():
        code = nodes.create_code()["code"]
        body = {"code": code, "node": {"id": "n_0123456789abcdef", "name": "old-board", "url": "https://100.64.0.9"}, "confirm": "x" * 22}
        with pytest.raises(nodes.PairError) as err:
            nodes.handle_pair(body, "100.64.0.9", two.b.db)
        assert err.value.reason == "key_required"
        assert keys.view(two.b.db)["unkeyed_count"] == 1


def test_typed_prefer_allows_a_new_unkeyed_pairing(two_nodes, clock, monkeypatch):
    monkeypatch.setattr(settings, "node_keys", "prefer")
    monkeypatch.setattr(settings, "node_keys_explicit", True)
    assert keys.pairing_mode() == "prefer"
    row = _keyless_pairing(two_nodes, monkeypatch)
    assert row["pubkey"] is None
