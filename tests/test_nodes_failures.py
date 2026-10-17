"""Nodes P16, issue #153 (failure modes) and the rest of #154 (the Mac and WSL2 as nodes): every way the fleet can go wrong ends in a clear state, and a test pins it.

Every scenario runs on two in-process boards (`two_nodes`) behind the chaos harness (`FlakyTransport` in tests/conftest.py, the `chaos` fixture): drop, timeout, lost answer,
delay, 5xx, truncated body, partition between named boards, per-board clock offsets and scripted sequences. Board a is the hub, board b the node. The epic scenarios add the fake
GitHub served from this process (tests/fake_github.py) and the work route of P14. Each scenario's test asserts at teardown that no thread, socket or temp file was left behind
(`no_leaks`). Nothing here reaches the real gh, claude, codex, tailscale, tmux or the network.

SCENARIOS lists the scenarios of #153's Scope and #154's routing, repos and sleep-and-wake items; `test_every_scenario_has_its_test` keeps the list and the tests in step.
"""
from __future__ import annotations

import re
import json
import threading
import time
from types import SimpleNamespace

import pytest

from app import doctor_nodes as dn, main, nodes, nodes_hub, nodes_relay as nr, nodes_route, nodes_run, nodes_work, nodes_epics
from app.config import settings
from tests.conftest import FlakyTransport
from tests.test_epics_route import ISSUE, T0, node as route_node
from tests.test_epics_work import (REPO, FakeHub, audit, b_task, beat, claims, git_init, progress, rec_of, run_route, stop_turn, w,  # noqa: F401  (w: the work-phase fixture)
                                   _no_reservation_left, _wide)

SCENARIOS = ("offline", "hub_restart", "skew", "duplicate_claim", "removed", "rotation", "identity_changed", "address_changed", "api_version", "github_trouble",
             "ambiguous_write", "registry_cap", "hostile_peer", "doctor", "idempotency", "sleep_and_wake", "mac_routing", "null_load", "repo_not_here")
PROBE = "/api/nodes/{h}/probe"


# ================================================================ the hub fixture: two paired boards behind the harness, a real hub on a with its own clock

@pytest.fixture
def fx(chaos, pair_up, probe_rows, monkeypatch):
    big = lambda: nodes._Limiter(10 ** 6, 60.0, 64)
    for n in ("node_read_limiter", "node_write_limiter", "relay_read_limiter", "relay_write_limiter", "relay_prompt_limiter"):
        monkeypatch.setattr(nodes, n, big())
    monkeypatch.setattr(main, "node_hub", None)
    reg = pair_up()
    t = [time.time()]
    with chaos.a.enter():
        hub = main._hub()
    hub._clock = lambda: t[0]

    def poll():
        with chaos.a.enter():
            return hub.poll_once()[0]

    def rec():
        with chaos.a.enter():
            return hub.records()[0]

    def tick(s):
        t[0] += s
    ns = SimpleNamespace(**vars(chaos), reg=reg, h=reg["handle"], hub=hub, poll=poll, rec=rec, tick=tick, t=t, probe=probe_rows)
    yield ns
    monkeypatch.setattr(main, "node_hub", None)


def calls_to_b(fx, path_part: str) -> int:
    return sum(1 for (_c, _m, board, path, kind) in fx.net.log if board == "node-b" and path_part in path and kind != "drop")


def b_task_add(board, title="Fix the login") -> int:
    with board.enter():
        return board.db.task_add(project="shop", repo="api", slug="fix-it", title=title, prompt="do it", branch="worktree-fix-it", base="main", worktree="x",
                                 tmux_name="shop--api--t-fix-it", claude_session_id="s-1")


# ================================================================ offline

@pytest.mark.parametrize("row", ["card", "probe"])
def test_offline_node_shows_its_last_state_with_the_age_refuses_every_action_and_queues_nothing(fx, row):
    tid = b_task_add(fx.b)
    first = fx.poll()
    assert first["status"] == "online" and first["state"] and any(t["id"] == tid for t in first["state"]["tasks"])
    fx.net.partition("node-a", "node-b")                                         # the node sleeps
    fx.tick(100)
    r = fx.poll()
    assert r["status"] == "stale" and r["error_kind"] == "refused" and r["age_s"] == 100 and r["state"] == first["state"], "stale keeps the last state, with its age"
    fx.tick(600)
    r = fx.poll()
    assert r["status"] == "offline" and r["age_s"] == 700 and r["state"] == first["state"]
    before = len(fx.net.log)
    resp = fx.a.post(PROBE.format(h=fx.h), json={}) if row == "probe" else fx.a.get(f"/api/nodes/{fx.h}/card")
    assert resp.status_code == 503 and resp.json()["reason"] == "offline" and resp.json()["age"] == 700, resp.text
    assert "node-b is offline" in resp.json()["error"] and "nothing was sent" in resp.json()["error"]
    assert len(fx.net.log) == before, "no call is made for an offline node"
    # routing skips it, and a pin to it waits visibly
    with fx.a.enter():
        recs = fx.hub.records()
    out = nodes_route.pick({"repo": "acme/api", "number": 5, "agent": "claude"}, recs, None, [], {"now": fx.t[0]})
    assert out["handle"] is None and out["refused"][0]["code"] == "offline"
    pinned = nodes_route.pick({"repo": "acme/api", "number": 5, "agent": "claude"}, recs, {"ui": fx.h}, [], {"now": fx.t[0]})
    assert pinned["waiting"]["pinned"] == fx.h and pinned["waiting"]["code"] == "offline" and "node-b" in pinned["waiting"]["text"]
    # it wakes: the first poll is online again, its task kept running there and shows, and nothing that was refused was queued
    fx.net.heal()
    fx.tick(5)
    r = fx.poll()
    assert r["status"] == "online" and r["age_s"] == 0 and any(t["id"] == tid and t["phase"] == "running" for t in r["state"]["tasks"])
    assert fx.probe.calls == [], "nothing was queued while it was offline"
    assert b_task(SimpleNamespace(b=fx.b), tid)["phase"] == "running"


# ================================================================ hub restart

def test_hub_restart_shows_node_last_with_the_true_age_and_the_nodes_keep_their_data(fx):
    first = fx.poll()
    fx.hub.persist(force=True)
    with fx.a.enter():
        again = nodes_hub.NodeHub(fx.a.db, clock=lambda: fx.t[0] + 300)           # the board restarted five minutes later and has not polled yet
        r = again.records()[0]
    assert r["status"] == "stale" and r["age_s"] == 300 and r["state"] == first["state"] and r["card"] == first["card"], "not a blank and not online"
    assert again.running() is False, "a restarted hub starts no thread by reading node_last"


# ================================================================ clock skew

@pytest.mark.parametrize("offset", [600, -600, 3])
def test_skew_is_measured_and_warned_above_five_seconds(fx, offset):
    fx.net.offset("node-b", offset)
    r = fx.poll()
    if abs(offset) > 120:
        assert r["status"] == "unauthorized" and r["error_kind"] == "clock"
        with fx.a.enter():
            out = dn._c_nodes_health(fx.a.db)
        assert "120 seconds" in out.detail and "automatic time" in out.fix["text"]
        return
    assert r["status"] == "online"
    assert abs(r["skew_ms"] - offset * 1000) < 2000 and r["skew_warn"] is (abs(offset) > 5)
    with fx.a.enter():
        out = dn._c_nodes_health(fx.a.db)
    if abs(offset) > 5:
        m = re.search(r"clock of node-b is (\d+) s (ahead|behind)", out.detail)   # the reading moves by a second while the test runs: allow 2 s, like the skew_ms check above
        assert out.status == "warn" and m and abs(int(m.group(1)) - abs(offset)) <= 2 and m.group(2) == ("ahead" if offset > 0 else "behind") and "automatic time" in out.fix["text"]
    else:
        assert out.status == "pass"


# ================================================================ removal

def test_removed_on_the_hub_deletes_row_and_token_at_once_and_the_peer_drops_its_pair(fx):
    tid = b_task_add(fx.b)
    fx.poll()
    pid = fx.reg["peer_id"]
    r = fx.a.delete(f"/api/nodes/{pid}")
    assert r.status_code == 200 and r.json()["peer_notified"] is True
    with fx.a.enter():
        assert nodes.peer(pid, fx.a.db) is None and not nodes.has_outgoing(pid)
        assert fx.hub.records() == []
    assert all(row["revoked_at"] for row in fx.b.db.node_pairs()), "the peer dropped its pair (best effort, it answered)"
    r = fx.a.post(PROBE.format(h=fx.h), json={})
    assert r.status_code == 404 and r.json()["reason"] == "unknown_node", "a relay after removal ends with a clear error and no call"
    assert b_task(SimpleNamespace(b=fx.b), tid)["phase"] == "running", "the peer's own tasks keep running"


def test_removed_while_a_relay_call_is_on_its_way_ends_that_call_with_re_pair(fx):
    fx.poll()

    def peer_removes():
        with fx.b.enter():
            for row in fx.b.db.node_pairs():
                nodes.revoke(row["peer_id"], db=fx.b.db, why="removed on the peer")
    fx.net.on(("call", peer_removes), match="/api/node/probe")
    r = fx.a.post(PROBE.format(h=fx.h), json={})
    assert r.status_code == 409 and r.json()["reason"] == "needs_repair" and fx.probe.calls == []


def test_removed_on_the_peer_makes_the_next_hub_call_401_with_re_pair(fx):
    fx.poll()
    [pair] = [p for p in fx.b.db.node_pairs() if not p["revoked_at"]]
    assert fx.b.delete(f"/api/nodes/{pair['peer_id']}").status_code == 200
    fx.tick(20)
    r = fx.poll()
    assert r["status"] == "unauthorized"
    with fx.a.enter():
        assert nodes.peer(fx.reg["peer_id"], fx.a.db)["needs_repair"] is True
    before = calls_to_b(fx, "/probe")
    resp = fx.a.post(PROBE.format(h=fx.h), json={})
    assert resp.status_code == 409 and resp.json()["reason"] == "needs_repair" and calls_to_b(fx, "/probe") == before
    with fx.a.enter():
        out = dn._c_nodes_health(fx.a.db)
    assert out.status == "warn" and "no longer takes this board's token" in out.detail and "Re-pair" in out.fix["text"]


# ================================================================ token rotation

def _bearer(tok: str) -> dict:
    return {"Authorization": "Bearer " + tok, "X-CCBoard": "1"}


def test_rotation_new_token_at_once_old_for_60_s_and_a_replay_after_is_401(fx, monkeypatch):
    with fx.a.enter():
        old = nodes._load_outgoing(fx.reg["peer_id"])
    r = fx.a.post(f"/api/nodes/{fx.reg['peer_id']}/rotate")
    assert r.status_code == 200 and r.json() == {"rotated": True, "grace_s": 60}
    with fx.a.enter():
        new = nodes._load_outgoing(fx.reg["peer_id"])
    assert new != old
    assert fx.b.get("/api/node", owner=False, headers=_bearer(new)).status_code == 200
    assert fx.b.get("/api/node", owner=False, headers=_bearer(old)).status_code == 200, "the old one works for 60 s"
    real = nodes._now
    monkeypatch.setattr(nodes, "_now", lambda: real() + 61)
    assert fx.b.get("/api/node", owner=False, headers=_bearer(old)).status_code == 401
    assert fx.b.post("/api/node/rotate", owner=False, headers=_bearer(old)).status_code == 401, "a replay of the rotate call with the old token after the grace"
    assert fx.b.get("/api/node", owner=False, headers=_bearer(new)).status_code == 200


def test_two_racing_rotations_leave_exactly_one_valid_token(fx, monkeypatch):
    with fx.a.enter():
        old = nodes._load_outgoing(fx.reg["peer_id"])
    [pair] = [p for p in fx.b.db.node_pairs() if not p["revoked_at"]]
    got, errs = [], []
    gate = threading.Barrier(2)

    def rotate():
        gate.wait(5)
        try:
            got.append(nodes.rotate_token(pair["peer_id"], db=fx.b.db, presented=old))
        except nodes.AlreadyRotated as e:
            errs.append(e)
    ts = [threading.Thread(target=rotate) for _ in range(2)]
    for th in ts:
        th.start()
    for th in ts:
        th.join(10)
    assert len(got) == 1 and len(errs) == 1, "one rotation, one refusal"
    real = nodes._now
    monkeypatch.setattr(nodes, "_now", lambda: real() + 61)
    with fx.b.enter():
        valid = [tok for tok in (old, got[0]) if nodes.verify_token(tok, db=fx.b.db)]
    assert valid == [got[0]]
    assert fx.b.post("/api/node/rotate", owner=False, headers=_bearer(old)).status_code == 401


def test_a_rotation_whose_answer_is_lost_marks_needs_repair_and_never_asks_again(fx, monkeypatch):
    fx.poll()
    fx.net.on("lost", match="/api/node/rotate")
    r = fx.a.post(f"/api/nodes/{fx.reg['peer_id']}/rotate")
    assert r.status_code == 504 and r.json()["reason"] == "outcome_unknown" and r.json()["needs_repair"] is True
    assert calls_to_b(fx, "/api/node/rotate") == 1, "never a second rotate"
    assert [p for p in fx.b.db.node_pairs() if p["rotated_at"]], "the peer did rotate: the hub could not know"
    with fx.a.enter():
        assert nodes.peer(fx.reg["peer_id"], fx.a.db)["needs_repair"] is True
        assert any(row["action"] == "rotate_unknown" for row in nodes.audit_list(50, db=fx.a.db))
    fx.tick(20)
    rec = fx.poll()
    assert rec["status"] == "online", "inside the grace the old token still reads"
    assert rec["needs_repair"] is True, "the hub record says so too, so the node page can offer Re-pair while the status is still online"
    with fx.a.enter():
        assert nodes.peer(fx.reg["peer_id"], fx.a.db)["needs_repair"] is True, "a good answer inside the grace does not clear it: no guessing"
    resp = fx.a.post(PROBE.format(h=fx.h), json={})
    assert resp.status_code == 409 and resp.json()["reason"] == "needs_repair" and fx.probe.calls == [], "Re-pair, not a guess"
    real = nodes._now
    monkeypatch.setattr(nodes, "_now", lambda: real() + 61)                      # past the grace the old token is gone there for good
    fx.tick(20)
    assert fx.poll()["status"] == "unauthorized"
    code = fx.b.post("/api/nodes/pair-code", json={"scopes": ["read", "tasks", "sessions"], "minutes": 10}).json()["code"]
    assert fx.a.post("/api/nodes", json={"url": fx.b.url, "code": code}).status_code == 201, "Re-pair: the same address again"
    with fx.a.enter():
        row = nodes.peer(fx.reg["peer_id"], fx.a.db)
    assert row["needs_repair"] is False and row["last_error"] is None and row["handle"] == fx.h


# ================================================================ identity changed

def test_identity_changed_merges_nothing_and_refuses_with_its_own_reason(fx):
    first = fx.poll()
    other = "n_" + "f" * 16
    with fx.a.enter():
        nodes.put_peer({"peer_id": fx.reg["peer_id"], "direction": "out", "url": fx.reg["url"], "node_id": other}, db=fx.a.db)   # the address now answers as another device
    b_task_add(fx.b, "A task of the other device")
    fx.tick(20)
    r = fx.poll()
    assert r["status"] == "unauthorized"
    assert r["state"] == first["state"] and "A task of the other device" not in json.dumps(r), "none of its data is merged"
    before = calls_to_b(fx, "/probe")
    resp = fx.a.post(PROBE.format(h=fx.h), json={})
    assert resp.status_code == 409 and resp.json()["reason"] == "needs_repair"
    assert calls_to_b(fx, "/probe") == before and fx.probe.calls == []
    with fx.a.enter():
        out = dn._c_nodes_health(fx.a.db)
    assert out.status == "warn" and "signature is not valid" in out.detail and "key update" in out.fix["text"]


# ================================================================ address changed

def test_address_changed_is_offered_after_the_old_address_is_probed_and_applied_only_by_update_address(fx):
    fx.poll()
    boards = fx.two.boards
    with fx.b.enter():
        b_id = nodes.node_id()
    found = [{"state": "found", "url": "https://100.64.0.9", "node_id": b_id}]
    boards[("100.64.0.9", 443)] = fx.b                                           # the node answers at a second address too
    with fx.a.enter():
        assert nodes.address_changes(fx.a.db, found) == [], "the old address still answers as the node: not a move"
    del boards[("100.64.0.2", 443)]                                               # now only at the new one
    with fx.a.enter():
        moved = nodes.address_changes(fx.a.db, found)
        assert nodes.peer(fx.reg["peer_id"], fx.a.db)["url"] == "https://100.64.0.2", "never applied silently"
    assert moved == [{"peer_id": fx.reg["peer_id"], "handle": fx.h, "name": "node-b", "from_url": "https://100.64.0.2", "to_url": "https://100.64.0.9"}]
    assert sum(1 for x in fx.net.log if x[3] == "/api/node/hello") >= 2, "the old address was probed once more"
    r = fx.a.post(f"/api/nodes/{fx.reg['peer_id']}/address", json={"url": "https://100.64.0.9"})
    assert r.status_code == 200 and r.json()["url"] == "https://100.64.0.9" and r.json()["handle"] == fx.h
    fx.tick(20)
    assert fx.poll()["status"] == "online", "the same token works at the new address"
    with fx.a.enter():
        assert any(row["action"] == "address_changed" for row in nodes.audit_list(50, db=fx.a.db))


def test_update_address_refuses_another_node_and_an_address_that_still_answers(fx):
    fx.poll()
    boards = fx.two.boards
    boards[("100.64.0.9", 443)] = fx.a                                            # the new address answers as ANOTHER node
    r = fx.a.post(f"/api/nodes/{fx.reg['peer_id']}/address", json={"url": "https://100.64.0.9"})
    assert r.status_code == 409 and r.json()["reason"] == "still_there", "the old address still answers as node-b"
    del boards[("100.64.0.2", 443)]
    r = fx.a.post(f"/api/nodes/{fx.reg['peer_id']}/address", json={"url": "https://100.64.0.9"})
    assert r.status_code == 409 and r.json()["reason"] == "other_node"
    r = fx.a.post(f"/api/nodes/{fx.reg['peer_id']}/address", json={"url": "https://example.com"})
    assert r.status_code == 400
    with fx.a.enter():
        assert nodes.peer(fx.reg["peer_id"], fx.a.db)["url"] == "https://100.64.0.2"


def test_discovery_offers_update_address_only_on_a_refresh(fx, monkeypatch):
    from app import nodes_discovery as nd
    with fx.b.enter():
        b_id = nodes.node_id()
    row = {"ts_id": "t1", "name": "node-b", "dns_name": "node-b.example.ts.net", "os": "linux", "online": True, "last_seen": None, "same_user": True, "tags": []}
    monkeypatch.setattr(nd, "read_tailscale", lambda fresh=False: ({"Self": {}}, {"ok": True, "reason": None, "variant": "linux"}))
    monkeypatch.setattr(nd, "magic_suffix", lambda s: "example.ts.net")
    monkeypatch.setattr(nd, "candidates", lambda *a: [row])
    monkeypatch.setattr(nd, "_probe_all", lambda rows, *a, on_idle=None, **k: (on_idle() if on_idle else None) or {"t1": {"state": "found", "url": "https://100.64.0.9", "node_id": b_id}})
    del fx.two.boards[("100.64.0.2", 443)]
    with fx.a.enter():
        assert "moved" not in nd.discover(fx.a.db)
        out = nd.discover(fx.a.db, refresh=True)
    assert out["moved"][0]["to_url"] == "https://100.64.0.9" and out["moved"][0]["peer_id"] == fx.reg["peer_id"]


# ================================================================ API version

@pytest.mark.parametrize("ours", [(2,), (0,)])
def test_api_version_mismatch_is_incompatible_the_card_reads_and_actions_are_refused(fx, monkeypatch, ours):
    monkeypatch.setattr(nodes_hub, "SUPPORTED_API", ours)                         # this board speaks another node API than the peer's 1
    r = fx.poll()
    assert r["status"] == "incompatible" and r["error_kind"] == "api_version" and r["peer_api"] == 1 and r["api"] == nodes.API_VERSION
    assert r["card"] and r["card"]["name"] == "node-b", "the card still reads"
    assert r["state"] is None, "its state is not merged"
    resp = fx.a.post(PROBE.format(h=fx.h), json={})
    assert resp.status_code == 409 and resp.json()["reason"] == "incompatible" and resp.json()["peer_api"] == 1 and fx.probe.calls == []
    assert "node API 1" in resp.json()["error"]
    with fx.a.enter():
        out = nodes_route.pick({"repo": "acme/api", "number": 5, "agent": "claude"}, fx.hub.records(), None, [], {"now": fx.t[0]})
        health = dn._c_nodes_health(fx.a.db)
    assert out["refused"][0]["code"] == "incompatible"
    assert health.status == "warn" and "speaks node API 1" in health.detail and "Update the older board" in health.fix["text"]
    monkeypatch.setattr(nodes_hub, "SUPPORTED_API", (nodes.API_VERSION,))
    fx.tick(20)
    assert fx.poll()["status"] == "online", "the next good poll clears it"


# ================================================================ GitHub trouble

@pytest.mark.parametrize("why", sorted(nodes_run.GITHUB_WHY))
def test_github_trouble_rests_the_node_with_a_reason_and_a_time_never_a_tight_loop(two_nodes, why, monkeypatch):
    clock = [T0]
    monkeypatch.setattr(nodes_run, "now", lambda: clock[0])
    monkeypatch.setattr(nodes_route, "clock", lambda: clock[0])
    runner = nodes_run.Runner(two_nodes.a.db, hub_fn=lambda: SimpleNamespace(records=lambda: []), repos_fn=lambda: [REPO])
    item = runner.reservations.add("box", T0 + 60, repo=REPO, issue=201)
    e = nr.RelayError(409, "work_refused", "box: " + nodes_epics.CLAIM_TEXT.get(why, "x"), why=why, **({"wait_s": 900} if why == "rate_limited" else {}))
    out = runner._refused({"key": "k"}, {"number": 201, "repo": REPO}, {"handle": "box", "name": "box"}, item, e)
    assert out["outcome"] == "refused" and out["code"] == "github_unavailable" and out["github"] == why
    assert out["until"] >= T0 + nodes_run.GITHUB_WAIT_S and (why != "rate_limited" or out["until"] >= T0 + 900)
    assert "GitHub is not available" in out["text"] and "trying again around" in out["text"]
    assert runner.blocked["box"]["code"] == "github_unavailable"
    later = nodes_route.pick(ISSUE, [route_node("box")], None, [], {"now": T0 + 30, "blocked": runner.blocked})
    assert later["handle"] is None and later["refused"][0]["code"] == "refused" and later["waiting"]["until"] == out["until"], "the next cycle does not ask it again"
    again = nodes_route.pick(ISSUE, [route_node("box")], None, [], {"now": out["until"] + 1, "blocked": runner.blocked})
    assert again["handle"] == "box"


# ================================================================ ambiguous writes

@pytest.mark.parametrize("action,status,reason,ran", [
    ("lost", 504, "unconfirmed", 1),
    (("delay", 99), 504, "unconfirmed", 1),
    (("truncate", 3), 502, "bad_answer", 1),
    (("status", 503), 502, "peer_error", 0),
    ("drop", 502, "unreachable", 0),
])
def test_ambiguous_write_is_never_retried_and_says_check_the_node(fx, action, status, reason, ran):
    fx.poll()
    fx.net.on(action, match="/api/node/probe", times=-1)
    r = fx.a.post(PROBE.format(h=fx.h), json={})
    assert r.status_code == status and r.json()["reason"] == reason, r.text
    assert len(fx.probe.calls) == ran and sum(1 for x in fx.net.log if x[3] == "/api/node/probe") == 1, "one call, never a second"
    if reason == "unconfirmed":
        assert r.json()["outcome"] == "unknown" and r.json()["check_url"] == f"#/n/{fx.h}" and "Check node-b" in r.json()["error"]


# ================================================================ registry cap

def _fill(db, n: int) -> None:
    for i in range(n):
        nodes.put_peer({"direction": "out", "url": f"https://100.64.1.{i + 1}", "name": f"filler-{i}", "node_id": "n_" + f"{i:016x}"}, db=db)


def test_registry_cap_refuses_the_13th_node_before_any_call(chaos, monkeypatch):
    with chaos.a.enter():
        _fill(chaos.a.db, nodes.MAX_PEERS)
        with pytest.raises(nodes.RegistryFull):
            nodes.put_peer({"direction": "out", "url": "https://100.64.2.1", "name": "one-more"}, db=chaos.a.db)
        nodes.put_peer({"direction": "out", "url": "https://100.64.3.1", "name": "old", "legacy": True}, db=chaos.a.db)        # a legacy row does not count
    nodes.reset()
    code = chaos.b.post("/api/nodes/pair-code", json={"scopes": ["read"], "minutes": 10}).json()["code"]
    r = chaos.a.post("/api/nodes", json={"url": chaos.b.url, "code": code})
    assert r.status_code == 409 and r.json()["reason"] == "registry_full" and "12 paired nodes" in r.json()["error"]
    assert not [x for x in chaos.net.log if x[2] == "node-b"], "nothing was sent to the 13th node, so its code is not spent"
    assert chaos.b.db.kv_get(nodes.KV_CODE) is not None, "the other board's code is still there"
    assert nodes_hub.NodeHub(chaos.a.db, workers=50).workers == nodes_hub.WORKERS == 4, "the poller never exceeds its worker count"


def test_registry_cap_on_the_accepting_side_keeps_the_code(chaos):
    with chaos.b.enter():
        for i in range(nodes.MAX_PEERS):
            nodes.add_incoming({"id": "n_" + f"{i:016x}", "name": f"caller-{i}", "url": f"https://100.64.1.{i + 1}"}, db=chaos.b.db)
    nodes.reset()
    code = chaos.b.post("/api/nodes/pair-code", json={"scopes": ["read"], "minutes": 10}).json()["code"]
    r = chaos.a.post("/api/nodes", json={"url": chaos.b.url, "code": code})
    assert r.status_code == 409 and r.json()["reason"] == "registry_full"
    with chaos.b.enter():
        assert chaos.b.db.kv_get(nodes.KV_CODE) is not None, "the code was not spent"
        assert len([p for p in chaos.b.db.node_pairs() if not p["revoked_at"]]) == nodes.MAX_PEERS


# ================================================================ hostile or broken peer, under the harness

@pytest.mark.parametrize("action,kind", [(("truncate", 40), "bad_body"), (("status", 502), "http_5xx"), ("timeout", "timeout"), ("drop", "refused"), ("lost", "timeout")])
def test_hostile_or_broken_peer_ends_in_its_error_kind_and_keeps_the_last_state(fx, action, kind):
    first = fx.poll()
    fx.hub._rec[fx.reg["peer_id"]]["etag"] = None                               # a full answer, not a 304, so a cut body is a cut body
    fx.net.on(action, match="/api/node/state")
    fx.tick(20)
    r = fx.poll()
    assert r["error_kind"] == kind and r["status"] == "online" and r["state"] == first["state"], "a failed poll changes the kind and the age only"
    fx.tick(20)
    assert fx.poll()["error_kind"] is None


def test_hostile_peer_oversize_state_is_too_large(fx, monkeypatch):
    fx.poll()
    monkeypatch.setattr(nodes_hub, "STATE_MAX", 50)
    fx.tick(20)
    with fx.a.enter():
        fx.hub._rec[fx.reg["peer_id"]]["etag"] = None
        fx.hub._rec[fx.reg["peer_id"]]["state"] = None
    assert fx.poll()["error_kind"] == "too_large"


# ================================================================ doctor nodes-health

def test_doctor_lists_unhealthy_nodes_worst_first_with_a_fix_each():
    regs = [{"peer_id": f"p{i}", "name": n, "needs_repair": False} for i, n in enumerate(["clock", "sleepy", "old", "stranger", "gone", "fine"])]
    recs = [{"peer_id": "p0", "status": "online", "skew_ms": -9000, "skew_warn": True}, {"peer_id": "p1", "status": "offline", "age_s": 7200},
            {"peer_id": "p2", "status": "incompatible", "peer_api": 2, "api": 1}, {"peer_id": "p3", "status": "unpaired", "error_kind": "identity_changed"},
            {"peer_id": "p4", "status": "unauthorized"}, {"peer_id": "p5", "status": "online", "skew_ms": 10, "skew_warn": False}]
    rows = dn._health_rows(regs, recs)
    assert [r[1] for r in rows] == ["gone", "stranger", "old", "sleepy", "clock"], "worst first; a healthy node is not listed"
    assert "last seen 2 h ago" in rows[3][2] and "9 s behind" in rows[4][2]
    assert all(r[3] and "—" not in r[3] and "—" not in r[2] for r in rows), "each with a fix, no em-dash"


def test_doctor_nodes_health_skips_with_no_pair_and_reads_no_node(two_nodes):
    with two_nodes.a.enter():
        assert dn._c_nodes_health(two_nodes.a.db).status == "skip"
    from app import doctor
    assert any(c[0] == "nodes-health" for c in doctor.CHECKS)


def test_doctor_nodes_health_offline_node_through_the_harness(fx):
    fx.poll()
    fx.net.partition("node-a", "node-b")
    fx.tick(900)
    fx.poll()
    n = len(fx.net.log)
    with fx.a.enter():
        out = dn._c_nodes_health(fx.a.db)
    assert out.status == "warn" and "node-b is offline, last seen 15 min ago" in out.detail and "awake" in out.fix["text"]
    assert len(fx.net.log) == n, "the doctor calls no node"


# ================================================================ the epic scenarios: the fake GitHub, the work route, sleep and wake (#153 Tests, #154)

@pytest.fixture
def ew(no_leaks, w, monkeypatch):
    """The work-phase boards (`w` of tests/test_epics_work.py) behind the harness, with the hub reading node-b online."""
    monkeypatch.setattr(main, "_hub", lambda: FakeHub())
    net = FlakyTransport(w.count).attach(w.two)
    monkeypatch.setattr(nodes, "peer_transport", net)
    w.net = net
    yield w
    net.detach()
    main._invalidate_scan()


def renew_beat(ew) -> None:
    """One beat of node-b's renew timer as nodes_epics._renew_loop runs it: renew every held claim (fenced), then the work hooks."""
    with ew.b.enter():
        actor = nodes_epics.board_actor()
        for v in [v for v in nodes_epics.held_claims(actor) if v["state"] == "held"]:
            nodes_epics.renew(v["claim_ref"], actor=actor)
            for hook in list(nodes_epics.RENEW_HOOKS):
                hook(actor, v)


def work_on_a(ew, issue: int = 201):
    with ew.a.enter():
        return nodes_work.work_issue(REPO, issue, epic=147)


def test_idempotency_the_same_node_twice_answers_the_same_task(ew):
    first = run_route(ew)
    second = run_route(ew)
    assert first.status_code == second.status_code == 200
    assert second.json()["data"]["reason"] == "already_held" and second.json()["data"]["id"] == first.json()["data"]["id"]
    assert len(ew.b.db.tasks()) == 1 and len(claims(ew)) == 1


def test_idempotency_two_nodes_give_one_winner_one_taken_one_task(ew):
    assert run_route(ew).status_code == 200
    with pytest.raises(nodes_work.Refused) as e:
        work_on_a(ew)
    assert e.value.reason in ("taken", "lost")
    assert len(ew.b.db.tasks()) == 1 and ew.a.db.tasks() == []
    live = [c for c in claims(ew) if c["status"] in ("claimed", "running")]
    assert len(live) == 1


def test_idempotency_a_hub_timeout_after_the_peer_started_it_and_a_manual_retry_hits_already_held(ew):
    ew.net.on("lost", match="/api/node/epics/work")
    r = run_route(ew)
    assert r.status_code == 504 and r.json()["reason"] == "unconfirmed" and r.json()["outcome"] == "unknown" and r.json()["check_url"] == f"#/n/{ew.h}"
    assert len(ew.b.db.tasks()) == 1, "the node did start it"
    assert sum(1 for x in ew.net.log if x[3] == "/api/node/epics/work") == 1, "the hub did not retry"
    again = run_route(ew)                                                          # the person retries by hand
    assert again.status_code == 200 and again.json()["data"]["reason"] == "already_held"
    assert again.json()["data"]["id"] == ew.b.db.tasks()[0]["id"] and len(ew.b.db.tasks()) == 1 and len(claims(ew)) == 1


@pytest.mark.parametrize("offset", [-600, 0, 600])
def test_duplicate_claim_and_skew_ten_minutes_either_way_change_no_claim_outcome(ew, offset):
    ew.net.offset("node-b", offset)                                               # node-b's own clock (nodes._now, nodes_work.clock) and what others read of it
    r = run_route(ew)
    if abs(offset) > 120:
        assert r.status_code == 409 and r.json()["reason"] == "needs_repair"
        assert ew.b.db.tasks() == [] and ew.a.db.tasks() == [] and claims(ew) == []
        return
    assert r.status_code == 200 and r.json()["data"]["reason"] == "won"
    with pytest.raises(nodes_work.Refused) as e:
        work_on_a(ew)
    assert e.value.reason in ("taken", "lost") and e.value.facts.get("winner") in (None, rec_of(ew)["alias"])
    assert len(ew.b.db.tasks()) == 1 and ew.a.db.tasks() == []


def test_a_partition_from_the_hub_does_not_stop_the_node_renewing_its_claim(ew):
    run_route(ew)
    ew.net.partition("node-a", "node-b")
    before = claims(ew)[0]["seq"]
    ew.gh.tick(300)
    renew_beat(ew)
    assert claims(ew)[0]["seq"] == before + 1, "the claim renews on the node, without the hub"
    assert rec_of(ew)["state"] == "active"


def test_sleep_and_wake_past_the_lease_the_other_node_reclaims_and_the_returning_node_is_fenced_and_parked(ew):
    tid = run_route(ew).json()["data"]["id"]
    ew.net.partition("node-a", "node-b")                                           # node-b sleeps: no poll, no beat, no renewal
    seq_asleep = claims(ew)[0]["seq"]
    ew.gh.tick(settings.epic_lease_min * 60 + 60)                                  # GitHub's clock passes the lease
    out = work_on_a(ew)                                                            # node-a reclaims
    assert out["claimed"] is True and out["reason"] == "won"
    writes = len(ew.gh.writes())
    ew.net.heal()                                                                  # node-b wakes
    renew_beat(ew)                                                                 # its next fenced write: the renewal
    assert len(ew.gh.writes()) == writes, "the returning node's write is refused by the fence: nothing is written"
    rec = rec_of(ew)
    assert rec["state"] == "lost" and rec["phase"] == "lost"
    t = b_task(ew, tid)
    with ew.b.enter():
        assert ew.b.db.open_row(t["tmux_name"])["flags"].get("epic_lost") == "lost-claim", "the task is parked"
    assert [r for r in audit(ew.b, action="epic-claim-lost")], "with the audit row"
    stop_turn(ew, tid, "Done.")
    assert progress(ew) == [], "a parked task posts nothing"
    mine = [c for c in claims(ew) if c["alias"] == rec["alias"]]
    theirs = [c for c in claims(ew) if c["alias"] != rec["alias"]]
    assert theirs and theirs[0]["status"] == "running", "node-a holds it"
    assert mine and mine[0]["seq"] == seq_asleep, "node-b's expired claim was not touched after it woke"


# ================================================================ #154: routing on a Mac, null load, repo_not_here wherever the clone is

def mac(handle="alice-mac", **kw):
    r = route_node(handle, **kw)
    r["card"]["accounts"] = {"supported": False, "items": [], "text": "saved logins are not available on a Mac"}
    r["card"]["load"] = {"load1": None, "cores": None, "cpu_pct": None, "mem_pct": None}
    r["card"]["os"] = {"system": "Darwin"}
    return r


def test_mac_routing_counts_one_account_and_is_never_excluded():
    alone = nodes_route.pick(ISSUE, [mac()], None, [], {"now": T0})
    assert alone["handle"] == "alice-mac" and alone["refused"] == []
    both = nodes_route.pick(ISSUE, [mac(sub=None), route_node("box", sub="k" * 12), route_node("box2", sub="k" * 12)], None, [], {"now": T0})
    mine = next(d for d in both["ranked"] if d["handle"] == "alice-mac")
    assert mine["shared_with"] == [], "the Mac's current account is a group of its own: one account"
    assert next(d for d in both["ranked"] if d["handle"] == "box")["shared_with"] == ["box2"]


def test_null_load_sorts_after_loaded_nodes_and_is_not_excluded():
    out = nodes_route.pick(ISSUE, [mac(), route_node("box", load1=3.9, cores=4)], None, [], {"now": T0})
    assert [d["handle"] for d in out["ranked"]] == ["box", "alice-mac"] and out["handle"] == "box"
    out = nodes_route.pick(ISSUE, [route_node("box", load1=3.9, cores=4), mac()], None, [], {"now": T0})
    assert [d["handle"] for d in out["ranked"]] == ["box", "alice-mac"], "the same for any order"
    unknown = route_node("zz", load1=None, cores=None)
    out = nodes_route.pick(ISSUE, [unknown, route_node("aa", load1=0.1, cores=8)], None, [], {"now": T0})
    assert [d["handle"] for d in out["ranked"]] == ["aa", "zz"]


def test_repo_not_here_wherever_the_clone_is(ew):
    """The clone is found by its `origin` remote under whatever projects dir the node has (a Mac's is not /srv/projects; here every board's is a temp dir), and
    a node without the clone says repo_not_here plainly, before any claim."""
    other = ew.tmp / "elsewhere" / "code"
    clone = git_init(other / "store" / "web", "https://github.com/acme/store.git")
    listed = [{"name": "store", "repos": [{"name": "web", "state": "ok", "path": str(clone)}]}]
    with ew.b.enter():
        assert nodes_work.local_repo("ACME/Store", listed) == ("store", "web"), "the origin remote decides, wherever the clone lives"
        assert nodes_work.local_repo("acme/shop", listed) is None
    import shutil
    shutil.rmtree(ew.repo_b)                                                       # node-b no longer has the clone of acme/shop
    main._invalidate_scan()
    r = run_route(ew)
    assert r.status_code == 404 and r.json()["reason"] == "repo_not_here" and "not cloned on this node" in r.json()["error"], r.text
    assert claims(ew) == [] and ew.b.db.tasks() == [], "nothing was claimed"


# ================================================================ review fixes (Rotate token, Update address, the caps, unknown writes)

class _FakeConn:
    """Stands in for nodes_discovery._Pinned so the REAL nodes._https runs without a socket: `connect_error` is raised by connect(), `answer_error` by getresponse()
    (after the request was sent). `log` records which steps ran."""

    def __init__(self, connect_error=None, answer_error=None):
        self.connect_error, self.answer_error = connect_error, answer_error
        self.log: list[str] = []
        self.sock = None

    def __call__(self, host, addr, port, timeout):
        return self

    def connect(self):
        self.log.append("connect")
        if self.connect_error:
            raise self.connect_error

    def request(self, method, path, body=None, headers=None):
        self.log.append("request")

    def getresponse(self):
        self.log.append("answer")
        raise self.answer_error

    def close(self):
        pass


def _row_a(fx) -> dict:
    with fx.a.enter():
        return nodes.peer(fx.reg["peer_id"], fx.a.db)


def test_rotate_connect_timeout_is_not_sent_the_pair_stays_healthy_and_the_old_token_valid(fx, monkeypatch):
    from app import nodes_discovery
    with fx.a.enter():
        old = nodes._load_outgoing(fx.reg["peer_id"])
    conn = _FakeConn(connect_error=TimeoutError("connect timed out"))            # the real _https: the node is asleep, the connection never comes up
    monkeypatch.setattr(nodes_discovery, "_Pinned", conn)
    monkeypatch.setattr(nodes, "peer_transport", None)
    r = fx.a.post(f"/api/nodes/{fx.reg['peer_id']}/rotate")
    assert r.status_code == 502 and r.json()["reason"] == "not_sent" and "nothing changed" in r.json()["error"] and "try again" in r.json()["error"], r.text
    assert conn.log == ["connect"], "the request never left"
    row = _row_a(fx)
    assert row["needs_repair"] is False and row["last_error"] != nodes.ROTATE_LOST
    with fx.a.enter():
        assert nodes._load_outgoing(fx.reg["peer_id"]) == old
        assert not any(x["action"] == "rotate_unknown" for x in nodes.audit_list(50, db=fx.a.db))
    assert fx.b.get("/api/node", owner=False, headers=_bearer(old)).status_code == 200, "the old token is still the valid one"
    monkeypatch.setattr(nodes, "peer_transport", fx.net)                          # it wakes: the same press now works
    assert fx.a.post(f"/api/nodes/{fx.reg['peer_id']}/rotate").status_code == 200


def test_rotate_connect_timeout_through_the_harness_is_not_sent_either(fx):
    def asleep():
        raise nodes.NotSent(TimeoutError("connect timed out"))
    fx.net.on(("call", asleep), match="/api/node/rotate")
    r = fx.a.post(f"/api/nodes/{fx.reg['peer_id']}/rotate")
    assert r.status_code == 502 and r.json()["reason"] == "not_sent", r.text
    assert _row_a(fx)["needs_repair"] is False and not [p for p in fx.b.db.node_pairs() if p["rotated_at"]]


def test_rotate_timeout_after_the_request_was_sent_still_marks_needs_repair(fx, monkeypatch):
    from app import nodes_discovery
    conn = _FakeConn(answer_error=TimeoutError("no answer"))                      # connected and sent, then silence
    monkeypatch.setattr(nodes_discovery, "_Pinned", conn)
    monkeypatch.setattr(nodes, "peer_transport", None)
    r = fx.a.post(f"/api/nodes/{fx.reg['peer_id']}/rotate")
    assert r.status_code == 504 and r.json()["reason"] == "outcome_unknown" and r.json()["needs_repair"] is True, r.text
    assert conn.log == ["connect", "request", "answer"]
    assert _row_a(fx)["needs_repair"] is True


def test_a_lost_rotation_stays_needs_repair_through_a_failed_poll_and_a_good_one(fx):
    fx.poll()
    fx.net.on("lost", match="/api/node/rotate")
    assert fx.a.post(f"/api/nodes/{fx.reg['peer_id']}/rotate").status_code == 504
    assert _row_a(fx)["needs_repair"] is True
    fx.net.partition("node-a", "node-b")
    fx.tick(20)
    fx.tick(80)
    assert fx.poll()["status"] == "stale"                                          # a failed poll overwrites last_error ...
    row = _row_a(fx)
    assert row["last_error"] != nodes.ROTATE_LOST and row["needs_repair"] is True
    fx.net.heal()
    b_task_add(fx.b, "Something new")                                              # so the answer is a 200, not a 304 on the saved ETag
    fx.tick(20)
    assert fx.poll()["status"] == "online", "... and a good one inside the 60 s grace answers on the old token"
    assert _row_a(fx)["last_error"] == nodes.ROTATE_LOST, "the standing problem is what the row says again"
    assert _row_a(fx)["needs_repair"] is True, "but neither clears the need to pair again"
    code = fx.b.post("/api/nodes/pair-code", json={"scopes": ["read", "tasks", "sessions"], "minutes": 10}).json()["code"]
    assert fx.a.post("/api/nodes", json={"url": fx.b.url, "code": code}).status_code == 201
    row = _row_a(fx)
    assert row["needs_repair"] is False and row["handle"] == fx.h, "only a new pairing clears it"


def test_two_rotate_presses_at_once_send_one_rotation_and_the_second_is_told_busy(fx, monkeypatch):
    sent: list = []
    release = threading.Event()
    results: list = []
    answer_token = nodes._new_token()

    def slow(target, method, path, headers, body, timeout):
        sent.append(path)
        release.wait(5)                                                           # bounded: a missing lock shows as a second call, not a hang
        return 200, {"content-type": "application/json"}, json.dumps({"token": answer_token, "grace_s": 60}).encode()
    monkeypatch.setattr(nodes, "peer_transport", slow)
    gate = threading.Barrier(2)

    def press():
        gate.wait(5)
        try:
            results.append(("ok", nodes.rotate_outgoing(fx.reg["peer_id"], db=fx.a.db)))
        except nodes.PeerError as e:
            results.append((e.reason, None))
    with fx.a.enter():
        ts = [threading.Thread(target=press) for _ in range(2)]
        for th in ts:
            th.start()
        deadline = time.monotonic() + 3
        while len(results) < 1 and len(sent) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        release.set()
        for th in ts:
            th.join(10)
        assert not any(th.is_alive() for th in ts)
        assert nodes._load_outgoing(fx.reg["peer_id"]) == answer_token
    assert sorted(r[0] for r in results) == ["busy", "ok"], results
    assert len(sent) == 1, "one rotate went to the node, so the loser's token cannot be refused as an old one"
    assert _row_a(fx)["needs_repair"] is False


# ---- Update address fails closed

def _moved_world(fx, first):
    """The node answers at a second address (100.64.0.9); the FIRST hello call (the old address) gets `first`, the rest go through."""
    fx.poll()
    fx.two.boards[("100.64.0.9", 443)] = fx.b
    fx.net.sequence(first, match="/api/node/hello")


@pytest.mark.parametrize("first", ["timeout", "lost", ("status", 429), ("status", 503), ("status", 302), ("truncate", 3), ("status", 200), ("delay", 99)],
                         ids=["timeout", "lost", "429", "503", "redirect", "broken", "not_a_hello", "slow"])
def test_update_address_refuses_when_the_old_address_could_not_be_checked(fx, first):
    _moved_world(fx, first)
    r = fx.a.post(f"/api/nodes/{fx.reg['peer_id']}/address", json={"url": "https://100.64.0.9"})
    assert r.status_code == 409 and r.json()["reason"] == "unchecked", r.text
    assert "still answers or could not be checked" in r.json()["error"] and "Try again" in r.json()["error"]
    assert _row_a(fx)["url"] == "https://100.64.0.2"
    with fx.a.enter():
        assert not any(x["action"] == "address_changed" for x in nodes.audit_list(50, db=fx.a.db))


@pytest.mark.parametrize("first", ["drop", ("status", 404)], ids=["refused", "not_found"])
def test_update_address_goes_ahead_when_the_old_address_is_clearly_gone(fx, first):
    _moved_world(fx, first)
    r = fx.a.post(f"/api/nodes/{fx.reg['peer_id']}/address", json={"url": "https://100.64.0.9"})
    assert r.status_code == 200 and r.json()["url"] == "https://100.64.0.9", r.text


def test_update_address_goes_ahead_when_the_old_address_now_answers_as_another_node(fx):
    fx.poll()
    fx.two.boards[("100.64.0.9", 443)] = fx.b
    fx.two.boards[("100.64.0.2", 443)] = fx.a                                     # the old address is another board's now: a hello with a different node id
    r = fx.a.post(f"/api/nodes/{fx.reg['peer_id']}/address", json={"url": "https://100.64.0.9"})
    assert r.status_code == 200 and r.json()["url"] == "https://100.64.0.9", r.text


def test_update_address_answers_and_records_the_device_name_when_discovery_had_one(fx):
    fx.poll()
    fx.two.boards[("100.64.0.9", 443)] = fx.b
    del fx.two.boards[("100.64.0.2", 443)]
    r = fx.a.post(f"/api/nodes/{fx.reg['peer_id']}/address", json={"url": "https://100.64.0.9", "device": "node-b-2"})
    assert r.status_code == 200 and r.json()["device"] == "node-b-2" and r.json()["url"] == "https://100.64.0.9", r.text
    with fx.a.enter():
        assert any(x["action"] == "address_changed" and "node-b-2" in (x["detail"] or "") for x in nodes.audit_list(50, db=fx.a.db))
    with fx.b.enter():
        b_id = nodes.node_id()
    with fx.a.enter():
        nodes.put_peer({"peer_id": fx.reg["peer_id"], "direction": "out", "url": "https://100.64.0.2"}, db=fx.a.db)
        found = [{"state": "found", "url": "https://100.64.0.9", "node_id": b_id, "name": "node-b-2"}]
        [offer] = nodes.address_changes(fx.a.db, found, probe=lambda u: None)
    assert offer["to_device"] == "node-b-2"


def test_update_address_does_not_make_a_row_that_was_removed_during_the_probes(fx):
    fx.poll()
    fx.two.boards[("100.64.0.9", 443)] = fx.b
    del fx.two.boards[("100.64.0.2", 443)]                                        # the old address refuses the connection ...

    def remove_meanwhile():                                                       # ... and the person presses Remove while that probe is running
        nodes.remove_peer(fx.reg["peer_id"], fx.a.db)
    fx.net.sequence(("call", remove_meanwhile), match="/api/node/hello")
    r = fx.a.post(f"/api/nodes/{fx.reg['peer_id']}/address", json={"url": "https://100.64.0.9"})
    assert r.status_code == 404, r.text
    with fx.a.enter():
        assert nodes.out_peers(fx.a.db) == [], "no tokenless row came back"
        assert nodes._load_outgoing(fx.reg["peer_id"]) is None
        assert not any(x["action"] == "address_changed" for x in nodes.audit_list(50, db=fx.a.db))


# ---- the incoming cap

def _fill_in(board, n: int, start: int = 0) -> None:
    with board.enter():
        for i in range(start, start + n):
            nodes.add_incoming({"id": "n_" + f"{i:016x}", "name": f"caller-{i}", "url": f"https://100.64.1.{i + 1}"}, db=board.db)


def _active_in(board) -> list:
    return [p for p in board.db.node_pairs() if not p["revoked_at"]]


def test_a_both_ways_pairing_is_refused_at_the_incoming_cap_before_anything_is_sent(chaos):
    _fill_in(chaos.a, nodes.MAX_PEERS)
    nodes.reset()
    code = chaos.b.post("/api/nodes/pair-code", json={"scopes": ["read"], "minutes": 10}).json()["code"]
    r = chaos.a.post("/api/nodes", json={"url": chaos.b.url, "code": code, "both_ways": True})
    assert r.status_code == 409 and r.json()["reason"] == "registry_full", r.text
    assert not [x for x in chaos.net.log if x[2] == "node-b"], "nothing was sent, so the other board's code is not spent"
    assert chaos.b.db.kv_get(nodes.KV_CODE) is not None
    assert len(_active_in(chaos.a)) == nodes.MAX_PEERS, "no 13th pair that calls this board"


def test_a_re_pair_that_replaces_a_pair_of_the_same_node_is_let_in_at_the_incoming_cap(fx):
    with fx.a.enter():
        a_id = nodes.node_id()
    _fill_in(fx.b, nodes.MAX_PEERS - 1)                                            # b: a's pair plus 11 others = 12
    assert len(_active_in(fx.b)) == nodes.MAX_PEERS
    code = fx.b.post("/api/nodes/pair-code", json={"scopes": ["read", "tasks", "sessions"], "minutes": 10}).json()["code"]
    r = fx.a.post("/api/nodes", json={"url": fx.b.url, "code": code})
    assert r.status_code == 201, r.text
    mine = [p for p in _active_in(fx.b) if p["peer_node_id"] == a_id]
    assert len(mine) == 1 and len(_active_in(fx.b)) == nodes.MAX_PEERS, "the old pair was replaced, the count did not grow"
    with fx.b.enter():                                                             # the same id from ANOTHER address, or another id, is a new pair: refused at the cap
        assert nodes.incoming_full(fx.b.db, {"id": a_id, "url": "https://100.64.9.9"}) is True
        assert nodes.incoming_full(fx.b.db, {"id": "n_" + "e" * 16, "url": fx.a.url}) is True
        assert nodes.incoming_full(fx.b.db, {"id": a_id, "url": fx.a.url}) is False


# ---- writes whose outcome is unknown

@pytest.mark.parametrize("action,status,reason", [(("truncate", 3), 502, "bad_answer"), (("status", 503), 502, "peer_error"), (("status", 500), 502, "peer_error")])
def test_a_write_with_a_server_error_or_a_broken_answer_is_an_unknown_outcome_and_never_retried(fx, action, status, reason):
    fx.poll()
    fx.net.on(action, match="/api/node/probe", times=-1)
    r = fx.a.post(PROBE.format(h=fx.h), json={})
    assert r.status_code == status and r.json()["reason"] == reason, r.text
    assert r.json()["outcome"] == "unknown" and r.json()["check_url"] == f"#/n/{fx.h}" and "check node-b" in r.json()["error"]
    assert sum(1 for x in fx.net.log if x[3] == "/api/node/probe") == 1, "the hub never retries"


def test_a_read_with_a_server_error_is_not_an_unknown_outcome(fx):
    fx.poll()
    fx.net.on(("status", 503), match="/api/node", times=-1)
    r = fx.a.get(f"/api/nodes/{fx.h}/card")
    assert r.status_code == 502 and r.json()["reason"] == "peer_error" and "outcome" not in r.json() and "check_url" not in r.json(), r.text


# ---- an unlisted reason is not GitHub trouble

def _run_refused(two_nodes, monkeypatch, **extra):
    clock = [T0]
    monkeypatch.setattr(nodes_run, "now", lambda: clock[0])
    monkeypatch.setattr(nodes_route, "clock", lambda: clock[0])
    runner = nodes_run.Runner(two_nodes.a.db, hub_fn=lambda: SimpleNamespace(records=lambda: []), repos_fn=lambda: [REPO])
    item = runner.reservations.add("box", T0 + 60, repo=REPO, issue=201)
    e = nr.RelayError(409, "work_refused", "box: x", why="error", **extra)
    return runner._refused({"key": "k"}, {"number": 201, "repo": REPO}, {"handle": "box", "name": "box"}, item, e), runner


def test_an_error_with_an_unlisted_reason_holds_the_issue_and_does_not_rest_the_node(two_nodes, monkeypatch):
    out, runner = _run_refused(two_nodes, monkeypatch, unknown_reason=nr.UNLISTED_WHY)
    assert out["outcome"] == "held" and out["code"] == "error" and "box" not in runner.blocked and ("k", 201) in runner.held


@pytest.mark.parametrize("extra", [{}, {"unknown_reason": "access_unknown"}], ids=["plain", "push_right_unreadable"])
def test_a_plain_error_and_an_unreadable_push_right_are_still_github_trouble(two_nodes, monkeypatch, extra):
    out, runner = _run_refused(two_nodes, monkeypatch, **extra)
    assert out["outcome"] == "refused" and out["code"] == "github_unavailable" and runner.blocked["box"]["code"] == "github_unavailable"


@pytest.mark.parametrize("name,wire", [("epic_work", "work_refused"), ("epic_claim", "claim_refused")])
def test_the_relay_marks_a_reason_it_does_not_list_and_leaves_a_listed_error_alone(name, wire):
    reg = {"name": "box", "handle": "box"}

    def mapped(why):
        reply = nodes.PeerReply(409, json.dumps({"reason": wire, "why": why}).encode(), {})
        with pytest.raises(nr.RelayError) as ei:
            nr._map_reply(reply, nr.BY_NAME[name], reg, {})
        return ei.value.extra
    assert mapped("a_reason_from_a_newer_peer") == {"why": "error", "unknown_reason": nr.UNLISTED_WHY}
    assert mapped("error") == {"why": "error"}


# ================================================================ the list and the tests stay in step

def test_every_scenario_has_its_test():
    import sys
    names = [n for n in dir(sys.modules[__name__]) if n.startswith("test_")]
    for s in SCENARIOS:
        assert any(s in n for n in names), f"no test for scenario {s}"
