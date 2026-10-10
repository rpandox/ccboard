"""Nodes epic P4, issue #138: the hub read model (app/nodes_hub.py) and the routes that serve it.

Peers are in-process fakes behind `nodes.peer_transport` (no socket, no other host); the clock is patched (`nodes._now`) so the staleness rules, the skew and the
age after a restart are exact. Pinned: parallel polling and the isolation of a slow node, the worker limit, the statuses and error kinds, that a failed poll keeps
the last good card and state, what a lying peer cannot change, skew, node_last (at most a write a minute, read back with the true age), the single-board and
legacy cases, and the routes (`GET /api/nodes`, `GET /api/nodes/state`, `state.nodes`, `state.nodes_enabled`).
"""
from __future__ import annotations

import hashlib
import json
import ssl
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import node_state, nodes, nodes_hub
from app.config import Settings, settings
from app.db import DB

ID = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
INTERVAL = 20.0
NID = "n_" + "b" * 16


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
    d.close()


def iso_ms(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds")


class Peer:
    """One fake board: answers GET /api/node/state and /api/node the way app/main.py does, or fails as `mode` says."""

    def __init__(self, clock, node_id=NID, skew=0.0, **state):
        self.clock, self.node_id, self.skew, self.mode = clock, node_id, skew, "ok"
        self.state = dict(state)
        self.gate = None                                   # a threading.Event the answer waits for
        self.calls: list[tuple[str, str | None]] = []      # (path, If-None-Match)
        self.card_extra: dict = {}
        self.name, self.url_claim = "evil-name", "https://evil.example"

    def body(self) -> dict:
        d = {"api": 1, "node": {"id": self.node_id, "name": self.name, "url": self.url_claim, "version": "0.5.36", "now": iso_ms(self.clock.t + self.skew)},
             "etag_base": "0" * 32, "projects": [{"name": "shop", "repos": [{"name": "api", "slug": "acme/api", "branch": "main", "dirty": False}]}],
             "sessions": [{"tmux": "shop--api--fix", "project": "shop", "repo": "api", "session": "fix", "agent": "claude", "state": "working", "needs_you": False,
                           "kind": "task", "since": "2026-10-10T10:00:00+00:00", "model": "opus"}],
             "tasks": [{"id": 1, "title": "Fix login", "phase": "running", "agent": "claude", "project": "shop", "repo": "api", "branch": "x", "tmux": "t",
                        "issue_ref": None, "updated_at": "2026-10-10T10:00:00+00:00"}],
             "needs_you": {"permissions": 0, "input": 0, "errors": 0}, "usage": {}, "lanes": {"cap": 3, "running": 1, "free": 2}, "login_problems": [], "truncated": False}
        d.update(self.state)
        return d

    def etag(self) -> str:
        b = self.body()
        if isinstance(b.get("node"), dict):
            b["node"] = {k: v for k, v in b["node"].items() if k != "now"}
        return 'W/"' + hashlib.sha256(json.dumps(b, sort_keys=True).encode()).hexdigest()[:32] + '"'

    def card(self) -> dict:
        return {"app": "ccboard", "api": 1, "node_id": self.node_id, "name": self.name, "url": self.url_claim, "version": "0.5.36",
                "load": {"load1": 0.5, "cores": 4, "cpu_pct": 12.0, "mem_pct": 40.0, "disk_pct": 50.0}, "sessions": {"live": 1, "working": 1, "needs_you": 0},
                "lanes": {"cap": 3, "running": 1, "free": 2}, "capabilities": ["state"], "now": iso_ms(self.clock.t + self.skew), **self.card_extra}

    def __call__(self, method, path, headers):
        inm = {k.lower(): v for k, v in headers.items()}.get("if-none-match")
        self.calls.append((path, inm))
        if self.gate is not None:
            assert self.gate.wait(10), "the test never released the slow node"
        m = self.mode
        if m == "timeout":
            raise TimeoutError("planned")
        if m == "refused":
            raise ConnectionRefusedError("planned")
        if m == "tls":
            raise ssl.SSLCertVerificationError("planned")
        if m in ("401", "403", "404", "500", "503"):
            return int(m), {}, b'{"error":"planned"}'
        if path == "/api/node":
            return 200, {}, json.dumps(self.card()).encode()
        if path != "/api/node/state":
            return 404, {}, b"{}"
        if m == "junk":
            return 200, {}, b"<html>not json</html>"
        if m == "list":
            return 200, {}, b"[1,2,3]"
        if m == "big":
            return 200, {"etag": self.etag()}, json.dumps({**self.body(), "pad": "x" * (nodes_hub.STATE_MAX + 10)}).encode()
        if m == "redirect":
            return 302, {"location": "https://elsewhere.example/"}, b""
        if inm == self.etag():
            return 304, {"etag": inm, "x-ccboard-now": iso_ms(self.clock.t + self.skew)}, b""
        return 200, {"etag": self.etag(), "content-type": "application/json"}, json.dumps(self.body()).encode()


class Net:
    """The fake network: host -> Peer. Installed as nodes.peer_transport; records every host that was called and the highest number of calls in flight."""

    def __init__(self, monkeypatch):
        self.peers: dict[str, Peer] = {}
        self.hosts: list[str] = []
        self.live = self.peak = 0
        self.pause = 0.0
        self.lock = threading.Lock()
        monkeypatch.setattr(nodes, "peer_transport", self)

    def __call__(self, target, method, path, headers, body, timeout):
        with self.lock:
            self.hosts.append(target.host)
            self.live += 1
            self.peak = max(self.peak, self.live)
        try:
            if self.pause:
                time.sleep(self.pause)
            return self.peers[target.host](method, path, headers)
        finally:
            with self.lock:
                self.live -= 1


@pytest.fixture
def net(monkeypatch, clock):
    return Net(monkeypatch)


def add(db, net, clock, n=1, name="node-b", node_id=NID, token=True, **peer_kw):
    """A registry row for the fake board at 100.64.0.<n+1> with a token in the 0600 file; returns (row view, Peer)."""
    host = f"100.64.0.{n + 1}"
    v = nodes.put_peer({"direction": "out", "url": "https://" + host, "name": name, "scopes": ["read", "tasks"], "node_id": node_id}, db=db)
    if token:
        nodes.save_outgoing(v["peer_id"], nodes._new_token())
    p = Peer(clock, node_id=node_id, **peer_kw)
    net.peers[host] = p
    return v, p


def rec_of(hub, handle=None):
    recs = hub.records(handle)
    return recs[0] if handle else recs


# ================================================================ a board with no paired node runs nothing

def test_no_registry_row_no_thread_no_request_no_row(db, net):
    hub = nodes_hub.NodeHub(db)
    assert hub.ensure() is False and hub.running() is False and hub.tick() is False
    assert hub.records() == [] and hub.list_rows() == {} and hub.summary_rows() is None and hub.enabled() is False
    assert hub.poll_once() == []
    assert not [t for t in threading.enumerate() if t.name.startswith("node-hub")]
    assert net.hosts == [] and db.kv_get("node_last") is None and db.kv_get("node_peers") is None


# ================================================================ the first reading, the 304, parallel polls

def test_the_first_reading_is_online_with_the_registry_name_url_and_a_card(db, net, clock):
    row, peer = add(db, net, clock, name="node-b")
    hub = nodes_hub.NodeHub(db, interval=INTERVAL)
    r = hub.poll_once()[0]
    assert r["status"] == "online" and r["error_kind"] is None and r["age_s"] == 0 and r["legacy"] is False
    assert r["handle"] == row["handle"] and r["node_id"] == NID and r["name"] == "node-b" and r["url"] == "https://100.64.0.2"
    assert r["polled_at"] == r["last_ok_at"] == nodes.iso(clock.t) and r["skew_ms"] is not None
    assert r["state"]["sessions"][0]["tmux"] == "shop--api--fix" and r["state"]["tasks"][0]["title"] == "Fix login"
    assert r["card"]["load"]["cores"] == 4 and r["card"]["name"] == "node-b" and r["etag"] == peer.etag()
    assert [c[0] for c in peer.calls] == ["/api/node/state", "/api/node"]


def test_the_second_poll_sends_if_none_match_and_a_304_costs_no_body_and_keeps_the_state(db, net, clock):
    _, peer = add(db, net, clock)
    hub = nodes_hub.NodeHub(db, interval=INTERVAL)
    first = hub.poll_once()[0]
    clock.t += 20
    again = hub.poll_once()[0]
    assert peer.calls[-1] == ("/api/node/state", peer.etag()), "the held ETag is sent"
    assert again["status"] == "online" and again["state"] == first["state"] and again["last_ok_at"] == nodes.iso(clock.t) and again["age_s"] == 0
    assert len([c for c in peer.calls if c[0] == "/api/node"]) == 1, "the card is not read again within five minutes"
    peer.state = {"tasks": []}
    clock.t += 20
    changed = hub.poll_once()[0]
    assert changed["state"]["tasks"] == [] and changed["etag"] == peer.etag()
    clock.t += nodes_hub.CARD_EVERY
    hub.poll_once()
    assert len([c for c in peer.calls if c[0] == "/api/node"]) == 2


def test_three_nodes_poll_in_parallel_and_a_slow_one_delays_nobody(db, net, clock):
    rows = [add(db, net, clock, n=i, name=f"node-{i}", node_id="n_" + str(i) * 16) for i in (1, 2, 3)]
    slow = rows[0][1]
    slow.gate = threading.Event()
    hub = nodes_hub.NodeHub(db, interval=INTERVAL)
    done = threading.Thread(target=hub.poll_once, daemon=True)
    t0 = time.monotonic()
    done.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not all(r["status"] == "online" for r in hub.records() if r["handle"] != rows[0][0]["handle"]):
        time.sleep(0.01)
    by = {r["handle"]: r for r in hub.records()}
    assert time.monotonic() - t0 < 2.5 and done.is_alive(), "the other two finished while the slow one still waits"
    assert by[rows[1][0]["handle"]]["status"] == by[rows[2][0]["handle"]]["status"] == "online"
    assert by[rows[0][0]["handle"]]["status"] == "offline" and by[rows[0][0]["handle"]]["state"] is None
    slow.gate.set()
    done.join(5)
    assert not done.is_alive() and all(r["status"] == "online" for r in hub.records())


def test_at_most_four_nodes_are_called_at_once(db, net, clock):
    for i in range(1, 8):
        add(db, net, clock, n=i, name=f"node-{i}", node_id="n_" + str(i) * 16)
    net.pause = 0.05
    hub = nodes_hub.NodeHub(db, interval=INTERVAL, workers=16)
    assert hub.workers == nodes_hub.WORKERS == 4
    recs = hub.poll_once()
    assert len(recs) == 7 and all(r["status"] == "online" for r in recs)
    assert 2 <= net.peak <= 4, net.peak


def test_the_scheduler_thread_keeps_a_stuck_node_to_one_call_and_the_others_on_their_cadence(db, net, clock):
    rows = [add(db, net, clock, n=i, name=f"node-{i}", node_id="n_" + str(i) * 16) for i in (1, 2, 3)]
    rows[0][1].gate = threading.Event()
    hub = nodes_hub.NodeHub(db, interval=0.1, jitter=0.0)
    try:
        assert hub.ensure() is True and hub.running() and hub.ensure() is False, "one thread, however often it is asked"
        time.sleep(0.8)
        stuck = [c for c in rows[0][1].calls if c[0] == "/api/node/state"]
        assert len(stuck) == 1, "a node whose poll is still running is not handed to the pool again"
        assert len([c for c in rows[1][1].calls if c[0] == "/api/node/state"]) >= 3 and len([c for c in rows[2][1].calls if c[0] == "/api/node/state"]) >= 3
    finally:
        rows[0][1].gate.set()
        hub.stop()
    assert not hub.running()


def test_the_next_poll_is_an_interval_away_plus_or_minus_ten_percent(db, net, clock):
    add(db, net, clock)
    m = [100.0]
    for r, lo, hi in ((0.0, 18.0, 18.1), (1.0, 22.0, 22.1), (0.5, 20.0, 20.1)):
        hub = nodes_hub.NodeHub(db, interval=INTERVAL, mono=lambda: m[0], rng=lambda r=r: r)
        hub._pool = type("P", (), {"submit": lambda self, fn, *a: None, "shutdown": lambda self, **k: None})()
        hub.tick()
        (due,) = hub._due.values()
        assert lo <= due - 100.0 <= hi, (r, due)


# ================================================================ statuses, error kinds, and a failed poll keeps the last good reading

def test_a_failed_poll_keeps_the_state_and_the_card_then_goes_stale_after_two_cycles_and_offline_after_ten_minutes(db, net, clock):
    _, peer = add(db, net, clock)
    hub = nodes_hub.NodeHub(db, interval=INTERVAL)
    good = hub.poll_once()[0]
    peer.mode = "timeout"
    clock.t += 20
    r = hub.poll_once()[0]
    assert r["status"] == "online" and r["error_kind"] == "timeout", "one missed cycle is still online, with the reason"
    assert r["state"] == good["state"] and r["card"] == good["card"] and r["etag"] == good["etag"] and r["last_ok_at"] == good["last_ok_at"] and r["age_s"] == 20
    assert r["polled_at"] == nodes.iso(clock.t), "polled_at is the last try, last_ok_at the last good one"
    clock.t += 21
    r = hub.poll_once()[0]
    assert r["status"] == "stale" and r["age_s"] == 41 and r["state"] == good["state"], "older than two cycles: stale, the last state is still there"
    clock.t = good_t(good) + 600
    assert hub.poll_once()[0]["status"] == "stale", "ten minutes exactly is still stale"
    clock.t += 1
    r = hub.poll_once()[0]
    assert r["status"] == "offline" and r["error_kind"] == "timeout" and r["state"] == good["state"] and r["age_s"] == 601
    peer.mode = "ok"
    clock.t += 5
    back = hub.poll_once()[0]
    assert back["status"] == "online" and back["error_kind"] is None and back["age_s"] == 0


def good_t(rec) -> float:
    return datetime.fromisoformat(rec["last_ok_at"]).timestamp()


def test_the_status_is_worked_out_when_it_is_read_not_only_when_it_is_polled(db, net, clock):
    add(db, net, clock)
    hub = nodes_hub.NodeHub(db, interval=INTERVAL)
    hub.poll_once()
    assert hub.records()[0]["status"] == "online"
    clock.t += 41
    assert hub.records()[0]["status"] == "stale" and hub.records()[0]["age_s"] == 41, "no poll ran, the clock moved"
    clock.t += 600
    assert hub.records()[0]["status"] == "offline"


def test_a_row_that_was_never_read_is_offline_never_online(db, net, clock):
    row, _ = add(db, net, clock)
    r = rec_of(nodes_hub.NodeHub(db, interval=INTERVAL), row["handle"])
    assert r["status"] == "offline" and r["age_s"] is None and r["last_ok_at"] is None and r["polled_at"] is None and r["state"] is None and r["card"] is None


@pytest.mark.parametrize("mode,status", [("401", "unauthorized"), ("403", "unauthorized"), ("404", "unpaired")])
def test_401_and_403_read_unauthorized_and_404_reads_unpaired_and_the_last_state_stays(db, net, clock, mode, status):
    _, peer = add(db, net, clock)
    hub = nodes_hub.NodeHub(db, interval=INTERVAL)
    good = hub.poll_once()[0]
    peer.mode = mode
    clock.t += 5
    r = hub.poll_once()[0]
    assert r["status"] == status and r["error_kind"] is None and r["state"] == good["state"] and r["card"] == good["card"]
    assert r["age_s"] == 5, "the age still counts from the last good reading"
    peer.mode = "ok"
    assert hub.poll_once()[0]["status"] == "online", "a good answer clears it"


def test_a_401_marks_the_registry_row_as_needing_a_new_pairing(db, net, clock):
    row, peer = add(db, net, clock)
    peer.mode = "401"
    nodes_hub.NodeHub(db, interval=INTERVAL).poll_once()
    assert nodes.peer(row["peer_id"], db=db)["needs_repair"] is True


@pytest.mark.parametrize("mode,kind", [("timeout", "timeout"), ("refused", "refused"), ("tls", "tls"), ("500", "http_5xx"), ("503", "http_5xx"), ("junk", "bad_body"),
                                      ("list", "bad_body"), ("big", "too_large"), ("redirect", "refused")])
def test_each_failure_has_its_error_kind_and_merges_nothing(db, net, clock, mode, kind):
    _, peer = add(db, net, clock)
    peer.mode = mode
    hub = nodes_hub.NodeHub(db, interval=INTERVAL)
    r = hub.poll_once()[0]
    assert r["error_kind"] == kind and r["state"] is None and r["card"] is None and r["status"] == "offline" and r["last_ok_at"] is None
    assert kind in nodes_hub.ERROR_KINDS


def test_a_body_over_the_cap_is_too_large_and_does_not_replace_the_old_state(db, net, clock):
    _, peer = add(db, net, clock)
    hub = nodes_hub.NodeHub(db, interval=INTERVAL)
    good = hub.poll_once()[0]
    peer.mode = "big"
    r = hub.poll_once()[0]
    assert r["error_kind"] == "too_large" and r["state"] == good["state"] and "pad" not in json.dumps(r)


def test_a_body_that_is_not_an_object_or_has_no_node_block_is_bad(db, net, clock):
    _, peer = add(db, net, clock)
    hub = nodes_hub.NodeHub(db, interval=INTERVAL)
    for state in ({"node": None}, {"node": {"id": "not an id"}}, {"node": {}}, {"node": {"id": 5}}):
        peer.state = state
        r = hub.poll_once()[0]
        assert r["error_kind"] == "bad_body" and r["state"] is None, state


# ================================================================ a peer's answer is untrusted

def test_a_peer_cannot_change_the_name_url_or_handle_the_hub_holds(db, net, clock):
    row, peer = add(db, net, clock, name="node-b")
    peer.name, peer.url_claim = "root", "https://evil.example"
    peer.card_extra = {"handle": "hijack", "node_id": NID}
    hub = nodes_hub.NodeHub(db, interval=INTERVAL)
    r = hub.poll_once()[0]
    assert (r["name"], r["url"], r["handle"], r["node_id"]) == ("node-b", "https://100.64.0.2", row["handle"], NID)
    assert r["card"]["name"] == "node-b" and r["card"]["url"] == "https://100.64.0.2" and "handle" not in r["card"]
    assert "name" not in r["state"]["node"] and "url" not in r["state"]["node"], "the body's own name and url are dropped"
    assert "evil" not in json.dumps(r)
    stored = nodes.peer(row["peer_id"], db=db)
    assert (stored["name"], stored["url"], stored["handle"]) == ("node-b", "https://100.64.0.2", row["handle"]), "and the registry row is untouched"
    assert set(net.hosts) == {"100.64.0.2"}, "the hub only ever calls the registry's address"


def test_a_peer_with_another_node_id_is_unpaired_identity_changed_and_not_merged(db, net, clock):
    _, peer = add(db, net, clock)
    hub = nodes_hub.NodeHub(db, interval=INTERVAL)
    good = hub.poll_once()[0]
    peer.node_id = "n_" + "e" * 16
    peer.state = {"sessions": [{"tmux": "x--y--z", "project": "x", "repo": "y", "session": "z", "state": "working"}]}
    clock.t += 3
    r = hub.poll_once()[0]
    assert r["status"] == "unpaired" and r["error_kind"] == "identity_changed"
    assert r["state"] == good["state"] and r["node_id"] == NID and "x--y--z" not in json.dumps(r), "the other node's state is not merged"
    assert hub.poll_once()[0]["status"] == "unpaired"
    fresh = nodes_hub.NodeHub(db, interval=INTERVAL)                      # a hub that never held this node's state: nothing to show at all
    r = fresh.poll_once()[0]
    assert r["status"] == "unpaired" and r["error_kind"] == "identity_changed" and r["state"] == good["state"], "the state loaded from node_last stays, the stranger's is not merged"


def test_unknown_keys_control_characters_and_long_strings_are_cleaned_and_lists_are_capped(db, net, clock):
    _, peer = add(db, net, clock)
    sep = chr(0x2028)
    peer.state = {"prompt": "SECRET-PROMPT", "token": "ccbnode_x",
                  "sessions": [{"tmux": "a--b--c" + str(i), "project": "a", "repo": "b", "session": "c", "state": "working", "last_prompt": "SECRET-PROMPT", "path": "/etc/passwd"}
                               for i in range(205)],
                  "tasks": [{"id": i, "title": "T\x07\x1b[31m" + "x" * 205 + sep + "end", "phase": "running", "extra": "SECRET"} for i in range(205)] + ["junk", 5, None],
                  "projects": [{"name": "p" * 500, "repos": [{"name": "r", "slug": "not a slug", "branch": "b\x00", "dirty": "yes", "path": "/x"}], "secret": 1}],
                  "needs_you": {"permissions": 2, "input": "many", "errors": True, "other": 9}, "usage": {"claude": {"pct": float("inf"), "known": 1, "x": 1}, "evil": {"pct": 1}},
                  "lanes": {"cap": 3, "running": "?", "free": None}, "login_problems": ["codex", "y" * 500, 7], "truncated": 1}
    peer.card_extra = {"accounts": {"items": [{"label": "L" * 500 + "\x07", "deep": {"a": {"b": {"c": {"d": {"e": 1}}}}}}]}, "surprise": "SECRET", "agents": list(range(80))}
    r = nodes_hub.NodeHub(db, interval=INTERVAL).poll_once()[0]
    assert r["error_kind"] is None, r["error_kind"]
    s, blob = r["state"], json.dumps(r)
    assert "SECRET" not in blob and "/etc/passwd" not in blob and "ccbnode_" not in blob and "\\u0007" not in blob and "\\u001b" not in blob and "\\u2028" not in blob
    assert set(s) == set(nodes_hub._STATE_KEYS) and len(s["sessions"]) == 200 and len(s["tasks"]) == 200
    assert all(set(x) == {"tmux", "project", "repo", "session", "agent", "state", "needs_you", "kind", "since", "model"} for x in s["sessions"])
    assert len(s["tasks"][0]["title"]) == 200 and s["tasks"][0]["title"].startswith("T [31m") and "\x07" not in s["tasks"][0]["title"]
    assert len(s["projects"][0]["name"]) == 200 and s["projects"][0]["repos"] == [{"name": "r", "slug": None, "branch": "b", "dirty": None}]
    assert s["needs_you"] == {"permissions": 2, "input": None, "errors": None} and s["lanes"] == {"cap": 3, "running": None, "free": None}
    assert set(s["usage"]) == {"claude"} and s["usage"]["claude"]["pct"] is None and s["usage"]["claude"]["known"] is True
    assert s["login_problems"] == ["codex", "y" * 20] and s["truncated"] is True
    card = r["card"]
    assert "surprise" not in card and len(card["agents"]) == 50 and len(card["accounts"]["items"][0]["label"]) == 200
    assert card["accounts"]["items"][0]["deep"] == {"a": None}, "nesting is cut at four levels from the top of the card"


def test_a_card_for_another_node_is_not_kept(db, net, clock):
    _, peer = add(db, net, clock)
    peer.card_extra = {"node_id": "n_" + "f" * 16}
    r = nodes_hub.NodeHub(db, interval=INTERVAL).poll_once()[0]
    assert r["status"] == "online" and r["card"] is None


def test_a_redirect_is_never_followed_and_only_registry_addresses_are_called(db, net, clock):
    _, peer = add(db, net, clock)
    peer.mode = "redirect"
    r = nodes_hub.NodeHub(db, interval=INTERVAL).poll_once()[0]
    assert r["error_kind"] == "refused" and set(net.hosts) == {"100.64.0.2"}


def test_a_pair_without_a_saved_token_is_unauthorized_and_calls_nobody(db, net, clock):
    add(db, net, clock, token=False)
    r = nodes_hub.NodeHub(db, interval=INTERVAL).poll_once()[0]
    assert r["status"] == "unauthorized" and net.hosts == []


# ================================================================ skew

@pytest.mark.parametrize("ahead,warn", [(8.0, True), (-8.0, True), (2.0, False), (0.0, False), (5.0, False), (5.5, True)])
def test_skew_is_the_peers_clock_minus_the_middle_of_the_request_and_warns_above_five_seconds(db, net, clock, ahead, warn):
    add(db, net, clock, skew=ahead)
    r = nodes_hub.NodeHub(db, interval=INTERVAL).poll_once()[0]
    assert abs(r["skew_ms"] - ahead * 1000) < 100 and r["skew_warn"] is warn


def test_skew_uses_half_the_round_trip(db, net, clock):
    _, peer = add(db, net, clock)
    m = [0.0]
    peer_call = peer.__call__

    def slow(method, path, headers):
        m[0] += 2.0                                           # the request takes two seconds
        return peer_call(method, path, headers)
    net.peers["100.64.0.2"] = slow
    peer.skew = 1.0                                            # the peer stamped its clock one second after we sent: that is the middle of a 2 s trip
    hub = nodes_hub.NodeHub(db, interval=INTERVAL, mono=lambda: m[0])
    r = hub.poll_once()[0]
    assert abs(r["skew_ms"]) < 5 and r["skew_warn"] is False


def test_a_304_refreshes_the_skew_from_the_header(db, net, clock):
    _, peer = add(db, net, clock, skew=0.0)
    hub = nodes_hub.NodeHub(db, interval=INTERVAL)
    hub.poll_once()
    peer.skew = 9.0
    clock.t += 20
    r = hub.poll_once()[0]
    assert peer.calls[-1][1] is not None and abs(r["skew_ms"] - 9000) < 100 and r["skew_warn"] is True


# ================================================================ node_last: a restart shows the last reading with its true age

def writes_of(db):
    seen = []
    real = db.kv_set
    db.kv_set = lambda key, value, at=None: (seen.append(key), real(key, value, at))[1]
    return seen


def test_node_last_is_written_at_most_once_a_minute_and_loaded_with_the_true_age(db, net, clock, monkeypatch):
    row, peer = add(db, net, clock)
    seen = writes_of(db)
    m = [1000.0]
    hub = nodes_hub.NodeHub(db, interval=INTERVAL, mono=lambda: m[0])
    good = hub.poll_once()[0]
    wrote = lambda: [k for k in seen if k == "node_last"]
    assert len(wrote()) == 1 and db.kv_get("node_last") is not None, "the first reading is written"
    for dt in (10, 25, 20):
        m[0] += dt
        clock.t += dt
        hub.poll_once()
    assert len(wrote()) == 1, "55 s later still the one write"
    m[0] += 10
    clock.t += 10
    hub.poll_once()
    assert len(wrote()) == 2, "a minute after the first"
    ok_at = clock.t
    hub.stop()
    assert len(wrote()) == 3, "and once at stop"
    clock.t = ok_at + 300                                      # the board was down for five minutes
    peer.mode = "refused"
    reborn = nodes_hub.NodeHub(db, interval=INTERVAL)
    r = reborn.records()[0]
    assert r["status"] == "stale" and r["age_s"] == 300 and r["last_ok_at"] == nodes.iso(ok_at), "the true age, not a fresh-looking reading"
    assert r["state"]["tasks"][0]["title"] == "Fix login" and r["card"]["load"]["cores"] == 4 and r["etag"] == good["etag"]
    clock.t = ok_at + 700
    assert nodes_hub.NodeHub(db, interval=INTERVAL).records()[0]["status"] == "offline"


def test_a_saved_online_status_is_not_carried_through_a_restart(db, net, clock):
    add(db, net, clock)
    hub = nodes_hub.NodeHub(db, interval=INTERVAL)
    hub.poll_once()
    hub.persist(force=True)
    saved = json.dumps(db.kv_get("node_last"))
    assert '"online"' not in saved and "status" not in saved, "only the times are stored; the status is worked out when it is read"
    clock.t += 1000
    assert nodes_hub.NodeHub(db, interval=INTERVAL).records()[0]["status"] == "offline"


def test_node_last_holds_no_token_and_a_forgotten_node_leaves_nothing(db, net, clock):
    row, _ = add(db, net, clock)
    token = nodes._load_outgoing(row["peer_id"])
    hub = nodes_hub.NodeHub(db, interval=INTERVAL)
    hub.poll_once()
    hub.persist(force=True)
    raw = json.dumps(db.kv_get("node_last")) + json.dumps(db.kv_get("node_peers"))
    assert token not in raw and nodes._digest(token) not in raw and "Bearer" not in raw
    nodes.remove_node(row["peer_id"], db=db)
    assert hub.tick() is False and db.kv_get("node_last") is None and hub.records() == [], "no row, no record, no node_last"


def test_a_stored_record_of_a_node_that_is_no_longer_in_the_registry_is_ignored(db, net, clock):
    row, _ = add(db, net, clock)
    hub = nodes_hub.NodeHub(db, interval=INTERVAL)
    hub.poll_once()
    hub.persist(force=True)
    add(db, net, clock, n=2, name="node-c", node_id="n_" + "c" * 16)
    nodes.remove_node(row["peer_id"], db=db)
    again = nodes_hub.NodeHub(db, interval=INTERVAL)
    assert [r["name"] for r in again.records()] == ["node-c"] and again.records()[0]["state"] is None


# ================================================================ nothing of a peer in the hub's data

def test_polling_writes_nothing_of_the_peer_into_the_hubs_tables(db, net, clock):
    add(db, net, clock)
    tables = [r[0] for r in db.conn.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
    before = {t: db.conn.execute(f"select count(*) from {t}").fetchone()[0] for t in tables}
    keys_before = {r[0] for r in db.conn.execute("select key from kv")}
    hub = nodes_hub.NodeHub(db, interval=INTERVAL)
    hub.poll_once()
    hub.persist(force=True)
    after = {t: db.conn.execute(f"select count(*) from {t}").fetchone()[0] for t in tables}
    assert {t for t in tables if before[t] != after[t]} <= {"kv"}
    assert {r[0] for r in db.conn.execute("select key from kv")} - keys_before == {"node_last"}
    assert db.conn.execute("select count(*) from tasks").fetchone()[0] == 0 and db.conn.execute("select count(*) from sessions").fetchone()[0] == 0


# ================================================================ legacy rows (CCBOARD_NODES) keep the old summary fetch

def test_a_legacy_row_polls_the_summary_with_the_hub_token_and_fills_only_summary_fields(db, net, clock, monkeypatch):
    monkeypatch.setattr(settings, "hub_token", "hub-secret")
    (legacy,) = nodes.import_legacy([{"name": "ubu", "url": "https://ubu.example.ts.net:8443"}], db=db)
    asked = []

    def fetch(url):
        asked.append(url)
        return {"node": "ubu", "sessions": 3, "attention": 1, "tasks": 2, "tmux_down": False, "url": "https://evil.example", "name": "evil", "online": False,
                "token": "SECRET", "health": {"host": "ubu", "cpu_pct": 7.5, "mem": {"pct": 30}}, "usage": {"five_hour": {"used_percentage": 40}}, "projects": [{"name": "x"}]}
    hub = nodes_hub.NodeHub(db, interval=INTERVAL, fetch_legacy=fetch)
    r = hub.poll_once()[0]
    assert asked == ["https://ubu.example.ts.net:8443"] and net.hosts == [], "the old summary fetch, and no node route"
    assert r["legacy"] is True and r["status"] == "online" and r["card"] is None and r["state"] is None and r["etag"] is None
    assert (r["name"], r["url"]) == ("ubu", "https://ubu.example.ts.net:8443")
    rows = hub.summary_rows()["value"]
    assert rows[0]["name"] == "ubu" and rows[0]["url"] == "https://ubu.example.ts.net:8443" and rows[0]["online"] is True and rows[0]["sessions"] == 3
    assert rows[0]["health"]["cpu_pct"] == 7.5 and "projects" not in rows[0] and "token" not in rows[0] and "SECRET" not in json.dumps(rows)
    clock.t += 50
    assert hub.summary_rows()["value"][0]["online"] is False, "offline, as the old strip showed it"


def test_a_legacy_row_without_a_hub_token_is_not_polled_and_a_failure_has_its_kind(db, net, clock, monkeypatch):
    nodes.import_legacy([{"name": "ubu", "url": "https://ubu.example.ts.net:8443"}], db=db)
    calls = []
    hub = nodes_hub.NodeHub(db, interval=INTERVAL, fetch_legacy=lambda url: calls.append(url) or {})
    monkeypatch.setattr(settings, "hub_token", "")
    assert hub.poll_once()[0]["status"] == "offline" and calls == [] and hub.summary_rows() is None
    monkeypatch.setattr(settings, "hub_token", "hub-secret")
    import urllib.error

    def boom(url):
        raise urllib.error.URLError(TimeoutError("slow"))
    hub._fetch_legacy = boom
    assert hub.poll_once()[0]["error_kind"] == "timeout"
    hub._fetch_legacy = lambda url: (_ for _ in ()).throw(urllib.error.HTTPError(url, 403, "no", {}, None))
    assert hub.poll_once()[0]["status"] == "unauthorized"
    hub._fetch_legacy = lambda url: ["not", "a", "dict"]
    assert hub.poll_once()[0]["error_kind"] == "bad_body"


def test_the_legacy_helper_keeps_the_old_poller_working_unchanged(db):
    from app import health
    p = health.Poller(db, health.parse_nodes("ubu=https://ubu.ts.net:8443"), fetch=lambda url: {"url": "https://evil.example", "name": "evil", "online": False, "sessions": 1})
    assert p.poll_once()[0]["name"] == "ubu" and health.fetch_node.__defaults__ == (10,)


# ================================================================ a node paired after the start starts the thread

def test_pairing_a_node_starts_the_thread_through_the_registry_listener(db, net, clock, monkeypatch):
    hub = nodes_hub.NodeHub(db, interval=0.05, jitter=0.0)
    monkeypatch.setattr(nodes, "registry_listener", hub.ensure)
    assert not hub.running()
    row, peer = add(db, net, clock)
    try:
        assert hub.running(), "the first saved row started it"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and hub.records()[0]["status"] != "online":
            time.sleep(0.01)
        assert hub.records()[0]["status"] == "online"
        nodes.remove_node(row["peer_id"], db=db)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and hub.running():
            time.sleep(0.01)
        assert not hub.running(), "the last row is gone: the thread ends by itself"
    finally:
        hub.stop()
    assert hub.ensure() is False, "a stopped hub does not start again"


# ================================================================ settings

@pytest.mark.parametrize("raw,want", [(None, 20.0), ("", 20.0), ("30", 30.0), ("5", 5.0), ("2", 5.0), ("0", 5.0), ("-4", 5.0), ("7.5", 7.5), ("abc", 20.0), ("nan", 20.0), ("inf", 20.0)])
def test_nodes_poll_defaults_to_20_and_is_never_below_5(raw, want):
    assert Settings({"CCBOARD_NODES_POLL": raw} if raw is not None else {}).nodes_poll == want


def test_the_hub_uses_the_setting_unless_it_was_given_an_interval(db, monkeypatch):
    monkeypatch.setattr(settings, "nodes_poll", 33.0)
    assert nodes_hub.NodeHub(db).interval == 33.0 and nodes_hub.NodeHub(db, interval=1.5).interval == 1.5


# ================================================================ the routes

@pytest.fixture
def board(lite_client, net, clock):
    from app import main
    main.node_hub = None
    yield SimpleNamespace(c=lite_client, db=main.db, main=main)
    main.node_hub = None


def test_a_single_board_answers_empty_lists_and_keeps_the_state_keys_plus_one_boolean(board, net):
    st = board.c.get("/api/state", headers=ID).json()
    demo = json.loads((__import__("pathlib").Path(__file__).resolve().parent.parent / "app" / "static" / "demo" / "state.json").read_text())
    assert set(st) - {"dev"} <= set(demo) and "nodes_enabled" in demo, "the demo fixture carries every live key, nodes_enabled included"
    assert "nodes_enabled" in st
    assert st["nodes_enabled"] is False and st["nodes"] is None
    assert board.c.get("/api/nodes", headers=ID).json()["nodes"] == [] and board.c.get("/api/nodes", headers=ID).json()["pairs"] == []
    r = board.c.get("/api/nodes/state", headers=ID)
    assert r.status_code == 200 and r.json()["nodes"] == []
    assert net.hosts == [] and not [t for t in threading.enumerate() if t.name.startswith("node-hub")] and board.db.kv_get("node_last") is None


def test_the_hub_routes_serve_the_merged_model_with_a_filter_and_an_etag(board, net, clock):
    a, _ = add(board.db, net, clock, n=1, name="node-a", node_id="n_" + "a" * 16)
    b, pb = add(board.db, net, clock, n=2, name="node-b", node_id="n_" + "b" * 16)
    hub = board.main._hub()
    hub.poll_once()
    pb.mode = "timeout"
    clock.t += 50
    hub.poll_once(handles={b["handle"]})
    r = board.c.get("/api/nodes/state", headers=ID)
    assert r.status_code == 200 and r.headers["etag"].startswith('W/"')
    by = {n["handle"]: n for n in r.json()["nodes"]}
    assert by[a["handle"]]["status"] == "stale" and by[b["handle"]]["status"] == "stale"
    one = board.c.get("/api/nodes/state", params={"handle": b["handle"]}, headers=ID).json()["nodes"]
    assert [n["handle"] for n in one] == [b["handle"]] and one[0]["error_kind"] == "timeout" and one[0]["state"]["tasks"][0]["title"] == "Fix login", "offline or not, the last state is shown"
    assert board.c.get("/api/nodes/state", params={"handle": "nope"}, headers=ID).status_code == 404
    assert board.c.get("/api/nodes/state", params={"handle": "Bad Handle"}, headers=ID).status_code == 400
    tag = r.headers["etag"]
    assert board.c.get("/api/nodes/state", headers={**ID, "If-None-Match": tag}).status_code == 304
    clock.t += 1
    assert board.c.get("/api/nodes/state", headers={**ID, "If-None-Match": tag}).status_code == 304, "the age ticking is not a change"
    clock.t += 600
    assert board.c.get("/api/nodes/state", headers={**ID, "If-None-Match": tag}).status_code == 200, "stale to offline is"
    assert board.c.get("/api/nodes/state").status_code == 403


def test_the_list_route_adds_status_age_and_counts_to_each_row(board, net, clock):
    a, _ = add(board.db, net, clock, n=1, name="node-a", node_id="n_" + "a" * 16)
    board.main._hub().poll_once()
    clock.t += 7
    rows = board.c.get("/api/nodes", headers=ID).json()["nodes"]
    assert len(rows) == 1 and rows[0]["handle"] == a["handle"] and rows[0]["scopes"] == ["read", "tasks"] and rows[0]["name"] == "node-a"
    assert rows[0]["status"] == "online" and rows[0]["age_s"] == 7 and rows[0]["counts"] == {"sessions": 1, "working": 1, "needs_you": 0, "tasks": 1} and rows[0]["error_kind"] is None
    assert "token" not in json.dumps(rows)


def test_state_nodes_keeps_its_shape_and_nodes_enabled_turns_true(board, net, clock):
    a, _ = add(board.db, net, clock, n=1, name="node-a", node_id="n_" + "a" * 16)
    st = board.c.get("/api/state", headers=ID).json()
    assert st["nodes_enabled"] is True
    assert st["nodes"]["value"] == [{"name": "node-a", "url": "https://100.64.0.2", "online": False, "status": "offline", "age_s": None, "handle": a["handle"]}], "never read: not online"
    board.main._hub().poll_once()
    st = board.c.get("/api/state", headers=ID).json()
    (row,) = st["nodes"]["value"]
    assert row["name"] == "node-a" and row["url"] == "https://100.64.0.2" and row["online"] is True and row["sessions"] == 1 and row["tasks"] == 1 and row["health"]["cores"] == 4
    assert st["nodes"]["at"]
    clock.t += 10_000
    (row,) = board.c.get("/api/state", headers=ID).json()["nodes"]["value"]
    assert row["online"] is False and row["status"] == "offline" and row["sessions"] == 1, "offline keeps the last numbers"


def test_a_node_token_cannot_read_the_hub_routes(board, net, clock):
    tok = nodes.add_incoming({"id": "n_" + "c" * 16, "name": "node-c", "url": "https://100.64.0.3"}, ["read", "tasks", "sessions", "permissions"], db=board.db)[1]
    for path in ("/api/nodes", "/api/nodes/state"):
        r = board.c.get(path, headers={"Authorization": f"Bearer {tok}", "X-CCBoard": "1"})
        assert r.status_code == 403 and r.json()["error"] == "this node token does not open that route", path


# ================================================================ two real boards: the hub reads the other board's real route

def test_a_hub_reads_a_second_board_through_the_real_route_with_its_token_and_gets_a_304_next_time(two_nodes):
    a, b = two_nodes.a, two_nodes.b
    code = b.post("/api/nodes/pair-code", json={"scopes": ["read", "tasks"], "minutes": 10}).json()["code"]
    assert a.post("/api/nodes", json={"url": b.url, "code": code}).status_code == 201
    with a.enter():
        hub = nodes_hub.NodeHub(a.db, interval=INTERVAL)
        (row,) = hub.rows()
        first = hub.poll_one(row)
        two_nodes.log.clear()
        second = hub.poll_one(row)
        b_id = None
    with b.enter():
        b_id = nodes.node_id()
    assert first["status"] == "online" and first["node_id"] == b_id == first["state"]["node"]["id"] and first["name"] == "node-b" and first["card"]["node_id"] == b_id
    assert first["etag"] and first["state"]["lanes"]["cap"] == 3 and first["scopes"] == ["read", "tasks"]
    assert ("node-a", "GET", "node-b", "/api/node/state", 304) in two_nodes.log, "the held ETag came back as 304 from the real route"
    assert second["state"] == first["state"] and second["status"] == "online"
    assert not [c for c in two_nodes.log if c[3] not in ("/api/node/state", "/api/node")], "only the two read routes were called"


# ---------------------------------------------------------------- a poll in flight and the database (a crash seen in the P7 test run)

def test_stop_waits_for_a_poll_in_flight_so_nothing_reads_the_database_afterwards(monkeypatch):
    """stop() used to return while a worker was still inside a poll; the worker then read kv from a database the caller was closing, which crashes
    sqlite3. Now stop() waits (bounded by STOP_WAIT) until no poll is in flight."""
    import threading, time
    from app import nodes_hub
    monkeypatch.setattr(nodes_hub, "STOP_WAIT", 3.0)
    hub = nodes_hub.NodeHub.__new__(nodes_hub.NodeHub)
    hub._stop, hub._thread, hub._pool, hub._lock, hub._inflight = threading.Event(), None, None, threading.RLock(), {"p1"}
    monkeypatch.setattr(hub, "persist", lambda force=False: order.append("persist"), raising=False)
    order = []
    def finish():
        time.sleep(0.25)
        with hub._lock:
            hub._inflight.discard("p1")
        order.append("poll ended")
    t = threading.Thread(target=finish); t.start()
    t0 = time.monotonic(); hub.stop(); took = time.monotonic() - t0
    t.join()
    assert order == ["poll ended", "persist"], "the last state is written only after the poll in flight ended"
    assert 0.2 <= took < 2.5


def test_stop_gives_up_after_the_wait_when_a_poll_never_ends(monkeypatch):
    import threading, time
    from app import nodes_hub
    monkeypatch.setattr(nodes_hub, "STOP_WAIT", 0.2)
    hub = nodes_hub.NodeHub.__new__(nodes_hub.NodeHub)
    hub._stop, hub._thread, hub._pool, hub._lock, hub._inflight = threading.Event(), None, None, threading.RLock(), {"stuck"}
    monkeypatch.setattr(hub, "persist", lambda force=False: None, raising=False)
    t0 = time.monotonic(); hub.stop()
    assert 0.15 <= time.monotonic() - t0 < 1.5, "bounded: a stuck node cannot hold the shutdown"


def test_closing_the_database_waits_for_a_query_and_a_later_call_is_an_error_not_a_crash(projects_dir):
    import sqlite3, threading, time
    d = DB(settings.db_path)
    d.kv_set("k", {"v": 1})
    seen = []
    def reader():
        with d.lock:                       # a thread in the middle of a query holds the lock
            seen.append("query started"); time.sleep(0.2); seen.append("query ended")
    t = threading.Thread(target=reader); t.start()
    while not seen:
        time.sleep(0.01)
    d.close(); seen.append("closed")
    t.join()
    assert seen == ["query started", "query ended", "closed"]
    with pytest.raises(sqlite3.ProgrammingError):
        d.kv_get("k")
