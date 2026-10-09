"""Nodes epic P2, issue #134: finding the other ccboard nodes on the tailnet.

Covers the pure candidate filter (issue #132, option C, parametrised over the tag setting so options A, B and C are all exercised), the address rule
`nodes.check_peer_url` / `valid_peer_url` (and the DNS-rebinding case), the probe with a fake transport (the state table, the port loop, offline and
invalid rows never probed, redirects refused, at most four in flight), the cache, `GET /api/nodes/discover`, the settings parsers and the three doctor
rows. The Tailscale reading is a fake behind one seam (`nodes._ts_status`), every name lookup and every connection is a fake, the data dir is a temp
dir, and nothing here opens a connection to another host (the one real socket is a listener on a loopback port).
"""
from __future__ import annotations

import copy
import json
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app import doctor, nodes, nodes_discovery as nd
from app import tailscale as ts
from app.config import parse_node_ports, parse_node_tags, settings
from app.db import DB

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
REAL_TS_STATUS = nodes._ts_status          # captured at import, before conftest's autouse fixture replaces it
ID = {"Tailscale-User-Login": "alice@example.com"}
H = {**ID, "X-CCBoard": "1"}
HELLO = (200, json.dumps({"app": "ccboard", "api": 1, "node_id": "ts:nPEER1CNTRL"}).encode())
ROW_KEYS = {"ts_id", "name", "dns_name", "os", "online", "last_seen", "owner", "tags", "state", "url", "node_id", "at", "age", "stale"}
SUFFIX = "example.ts.net"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def ids(rows) -> list[str]:
    return [r["ts_id"] for r in rows]


# ---------------------------------------------------------------- fakes

class Net:
    """A fake transport: `by_port` maps a port to (status, body) or an exception to raise; `delay` sleeps inside the call; records every call and the
    most calls that were in flight at once."""

    def __init__(self, by_port=None, default=None, delay=0.0):
        self.by_port = by_port or {}
        self.default = default if default is not None else HELLO
        self.delay = delay
        self.calls: list[tuple] = []
        self._lock = threading.Lock()
        self.inflight = 0
        self.peak = 0
        self.gate: threading.Event | None = None

    def __call__(self, host, addr, port, timeout):
        with self._lock:
            self.calls.append((host, addr, port, timeout))
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        try:
            if self.gate is not None:
                self.gate.wait(5)
            if self.delay:
                time.sleep(self.delay)
            r = self.by_port.get(port, self.default)
            if isinstance(r, BaseException):
                raise r
            return r
        finally:
            with self._lock:
                self.inflight -= 1


class Resolver:
    """A fake resolver: `table` maps a name to a list of addresses, or to a list of lists for successive answers (the last one repeats)."""

    def __init__(self, table=None, status=None):
        self.table = dict(table or {})
        if status is not None:
            for p in nd._peers(status):
                dns = (p.get("DNSName") or "").rstrip(".")
                if dns and p.get("TailscaleIPs"):
                    self.table.setdefault(dns, [a for a in p["TailscaleIPs"] if nodes.in_tailnet(a)] or p["TailscaleIPs"])
        self.calls: list[tuple] = []

    def __call__(self, host, port):
        self.calls.append((host, port))
        ans = self.table.get(host)
        if ans is None:
            raise socket.gaierror("no such name")
        if ans and isinstance(ans[0], list):
            return list(ans[min(len(self.calls_for(host)) - 1, len(ans) - 1)])
        return list(ans)

    def calls_for(self, host):
        return [c for c in self.calls if c[0] == host]


@pytest.fixture
def world(monkeypatch, projects_dir, tmp_path):
    """Tailscale answers with the tailnet fixture; the probe transport and the name lookup are fakes; the settings are the defaults."""
    status = load("ts_status_tailnet.json")
    w = {"status": status, "cli": ts.Cli(["/opt/fake/tailscale"], {}), "variant": "linux", "net": Net(), "res": Resolver(status=status)}
    monkeypatch.setattr(nodes, "_ts_status", lambda: w["status"])
    monkeypatch.setattr(ts, "find_cli", lambda: w["cli"])
    monkeypatch.setattr(ts, "variant", lambda cli=None: w["variant"])
    monkeypatch.setattr(settings, "node_ports", (443, 8443))
    monkeypatch.setattr(settings, "node_tags", ("tag:ccboard",))
    monkeypatch.setattr(settings, "node_ports_bad", False)
    monkeypatch.setattr(settings, "node_tags_bad", False)
    monkeypatch.setattr(nd, "pinned_transport", lambda *a, **k: w["net"](*a, **k))
    monkeypatch.setattr(nodes, "_resolve_all", lambda host, port: w["res"](host, port))
    monkeypatch.setattr(nd, "REFRESH_DEADLINE", 5.0)
    nodes.reset()
    (tmp_path / "d").mkdir()
    w["db"] = DB(tmp_path / "d" / "ccboard.db")
    w["clock"] = [1_800_000_000.0]
    w["discover"] = lambda **k: nd.discover(w["db"], clock=lambda: w["clock"][0], **k)
    yield w
    w["db"].conn.close()


@pytest.fixture(autouse=True)
def _no_real_lookup(monkeypatch):
    """No test in this file resolves a real name: the default resolver is replaced unless the `world` fixture put a fake in."""
    def boom(host, port):
        raise AssertionError("a real name lookup was attempted")
    monkeypatch.setattr(nodes, "_resolve_all", boom)


# ---------------------------------------------------------------- candidates (pure), issue #132 option C

TAILNET = load("ts_status_tailnet.json")
ONLINE_DEFAULT = ["nTAG01", "nDEV01", "nDEV02", "nSAME1", "nODD01", "nPUB01"]       # box, dev, dev, node-b, odd, wan; then alice-mac (offline)


@pytest.mark.parametrize("tags, expected", [
    ((), ["nDEV01", "nDEV02", "nSAME1", "nODD01", "nPUB01", "nMAC01"]),                                    # option A: the same user's devices only
    (("tag:ccboard",), ["nTAG01", "nDEV01", "nDEV02", "nSAME1", "nODD01", "nPUB01", "nMAC01"]),            # option C, the default
    (("tag:ccboard", "tag:other"), ["nTAG01", "nTAG02", "nDEV01", "nDEV02", "nSAME1", "nODD01", "nPUB01", "nMAC01"]),
    (("tag:nobody",), ["nDEV01", "nDEV02", "nSAME1", "nODD01", "nPUB01", "nMAC01"]),
])
def test_candidates_follow_the_tag_setting_online_first_then_name(tags, expected):
    rows = nd.candidates(TAILNET, tags, nd.self_user(TAILNET))
    assert ids(rows) == expected


def test_self_the_shared_in_peers_and_a_peer_without_a_dns_name_never_appear():
    for tags in ((), ("tag:ccboard",), ("tag:ccboard", "tag:other", "tag:nobody")):
        got = set(ids(nd.candidates(TAILNET, tags, 1001)))
        assert "nSELF1" not in got, "this device"
        assert not got & {"nSHR01", "nSHR02"}, "shared in from another user, even the one that carries a configured tag"
        assert "nNODNS" not in got, "no DNSName"


def test_a_peer_marked_as_a_sharee_is_dropped_even_with_a_configured_tag_and_the_right_suffix():
    st = copy.deepcopy(TAILNET)
    for p in st["Peer"].values():
        if p["ID"] == "nTAG01":
            p["ShareeNode"] = True
    assert "nTAG01" not in ids(nd.candidates(st, ("tag:ccboard",), 1001))


def test_two_peers_with_one_host_name_stay_two_rows_keyed_by_the_tailscale_id():
    rows = nd.candidates(TAILNET, (), 1001)
    dev = [r for r in rows if r["name"] == "dev"]
    assert [r["ts_id"] for r in dev] == ["nDEV01", "nDEV02"]
    assert [r["dns_name"] for r in dev] == ["dev.example.ts.net", "dev-1.example.ts.net"]
    assert len({r["ts_id"] for r in rows}) == len(rows)


def test_row_fields_online_state_last_seen_owner_and_addresses():
    rows = {r["ts_id"]: r for r in nd.candidates(TAILNET, ("tag:ccboard",), 1001)}
    assert set(rows["nSAME1"]) == {"ts_id", "name", "dns_name", "os", "online", "last_seen", "tags", "same_user", "ips"}
    assert rows["nSAME1"]["same_user"] is True and rows["nSAME1"]["tags"] == [] and rows["nSAME1"]["online"] is True
    assert rows["nTAG01"]["same_user"] is False and rows["nTAG01"]["tags"] == ["tag:ccboard"]
    assert rows["nMAC01"]["online"] is False and rows["nMAC01"]["last_seen"] == "2026-10-09T18:22:41Z" and rows["nMAC01"]["os"] == "macOS"
    assert rows["nSAME1"]["dns_name"] == "node-b.example.ts.net", "no trailing dot"
    assert rows["nSAME1"]["last_seen"] == "2026-10-10T08:00:00Z"
    st = copy.deepcopy(TAILNET)
    next(p for p in st["Peer"].values() if p["ID"] == "nSAME1")["LastSeen"] = "0001-01-01T00:00:00Z"
    assert next(r for r in nd.candidates(st, (), 1001) if r["ts_id"] == "nSAME1")["last_seen"] is None, "Tailscale's 'never' is null"


def test_addresses_outside_the_tailnet_ranges_are_dropped_from_a_row():
    row = next(r for r in nd.candidates(TAILNET, (), 1001) if r["ts_id"] == "nPUB01")
    assert row["ips"] == ["100.100.1.12"], "203.0.113.5 and 169.254.169.254 are not tailnet addresses"


def test_a_name_with_odd_characters_is_listed_without_a_name_so_it_can_be_shown_as_invalid():
    row = next(r for r in nd.candidates(TAILNET, (), 1001) if r["ts_id"] == "nODD01")
    assert row["dns_name"] is None and row["name"] == "odd"


def test_a_tagged_self_has_no_user_so_nothing_counts_as_the_same_user():
    st = load("ts_status_tagged_self.json")
    assert nd.self_user(st) is None, "a tagged device has no user"
    assert nd.self_user(TAILNET) == 1001
    got = nd.candidates(st, ("tag:ccboard",), nd.self_user(st))
    assert ids(got) == ["nTAG01"], "the other tagged device shares Self's pseudo user id but is not 'the same user', and the user-owned peer is not ours to claim"
    assert all(r["same_user"] is False for r in got)
    assert ids(nd.candidates(st, ("tag:ccboard", "tag:other"), None)) == ["nTAG01", "nTAG02"]
    assert nd.candidates(st, (), None) == []


def test_candidates_is_pure_and_tolerates_a_status_that_is_not_what_it_expects():
    before = copy.deepcopy(TAILNET)
    nd.candidates(TAILNET, ("tag:ccboard",), 1001)
    assert TAILNET == before
    for junk in (None, {}, [], "x", {"Peer": None}, {"Peer": 5}, {"Peer": {"k": "v"}}, {"Peer": {"k": {"ID": 5}}}, {"Peer": [None, {"ID": "x" * 80, "DNSName": "a.b"}]}):
        assert nd.candidates(junk, ("tag:ccboard",), 1) == []
    assert nd.self_user({"Self": {"UserID": True}}) is None and nd.self_user({"Self": {"UserID": 0}}) is None


def test_the_mac_app_bundle_shape_lists_the_linux_box_and_the_tagged_box():
    st = load("ts_status_mac_app.json")
    assert nd.magic_suffix(st) == SUFFIX
    assert [(r["name"], r["os"]) for r in nd.candidates(st, ("tag:ccboard",), nd.self_user(st))] == [("box", "linux"), ("node-a", "linux")]


# ---------------------------------------------------------------- the address rule

TS_A = "100.100.1.2"


@pytest.mark.parametrize("url, addrs", [
    ("https://node-a.example.ts.net", [TS_A]),
    ("https://node-a.example.ts.net:8443", [TS_A]),
    ("https://node-a.example.ts.net/", [TS_A]),
    ("https://NODE-A.Example.TS.NET.:8443", [TS_A]),
    ("https://a.b.example.ts.net", [TS_A]),
    ("https://100.100.1.2:8443", ["100.100.1.2"]),
    ("https://100.64.0.0", ["100.64.0.0"]),
    ("https://100.127.255.255", ["100.127.255.255"]),
    ("https://[fd7a:115c:a1e0::2]", ["fd7a:115c:a1e0::2"]),
    ("https://[fd7a:115c:a1e0:ffff::1]:8443", ["fd7a:115c:a1e0:ffff::1"]),
])
def test_valid_peer_url_accepts_a_magicdns_name_and_the_tailnet_ranges(url, addrs):
    res = Resolver({"node-a.example.ts.net": [TS_A], "a.b.example.ts.net": [TS_A]})
    assert nodes.valid_peer_url(url, SUFFIX, res) == addrs


@pytest.mark.parametrize("url", [
    "http://node-a.example.ts.net",                       # not https
    "ftp://node-a.example.ts.net", "node-a.example.ts.net", "//node-a.example.ts.net", "", None, 5, "https://",
    "https://user@node-a.example.ts.net", "https://user:pw@node-a.example.ts.net", "https://@node-a.example.ts.net",
    "https://node-a.example.ts.net@evil.example",
    "https://node-a.example.ts.net?x=1", "https://node-a.example.ts.net/?", "https://node-a.example.ts.net#frag", "https://node-a.example.ts.net/#",
    "https://node-a.example.ts.net/api/node/hello", "https://node-a.example.ts.net/;p",
    "https://node-a.example.ts.net:0", "https://node-a.example.ts.net:70000", "https://node-a.example.ts.net:abc",
    "https://node-a.example.ts.net\\@evil.example", "https://node-a.example.ts.net ", "https://node-a.example.ts.net\n", "https://nóde.example.ts.net",
    "https://8.8.8.8", "https://1.1.1.1:8443", "https://127.0.0.1", "https://127.0.0.1:8443", "https://[::1]", "https://169.254.169.254",
    "https://10.0.0.5", "https://192.168.1.1", "https://172.16.0.1", "https://0.0.0.0", "https://[fe80::1]", "https://[fe80::1%25eth0]",
    "https://100.63.255.255", "https://100.128.0.0", "https://[fd7a:115c:a1e1::1]", "https://[::ffff:100.100.1.2]", "https://[fd7a:115c:a1e0::1%25eth0]",
    "https://localhost", "https://example.com", "https://example.ts.net", "https://evilexample.ts.net", "https://node-a.example.ts.net.evil.example",
    "https://node-a.other-net.ts.net", "https://node-a.ts.net", "https://-a.example.ts.net", "https://a..example.ts.net",
    "https://2130706433", "https://0x7f.1", "https://127.1",
])
def test_valid_peer_url_refuses_everything_else(url):
    res = Resolver({"node-a.example.ts.net": [TS_A]})
    assert nodes.valid_peer_url(url, SUFFIX, res) is None


def test_a_name_needs_the_suffix_and_without_one_only_addresses_pass():
    res = Resolver({"node-a.example.ts.net": [TS_A]})
    for suffix in (None, "", ".", "bad suffix!"):
        assert nodes.valid_peer_url("https://node-a.example.ts.net", suffix, res) is None
        assert nodes.valid_peer_url("https://100.100.1.2", suffix, res) == ["100.100.1.2"]
    assert res.calls == [], "a name that fails the syntax is never looked up"


def test_a_name_that_resolves_to_a_private_address_is_refused():
    for bad in ("10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.0.9", "8.8.8.8", "::1", "fe80::1", "::ffff:127.0.0.1", "100.63.0.1"):
        assert nodes.valid_peer_url("https://node-a.example.ts.net", SUFFIX, Resolver({"node-a.example.ts.net": [bad]})) is None, bad


@pytest.mark.parametrize("answer", [
    [TS_A, "10.0.0.5"], ["10.0.0.5", TS_A], [TS_A, "169.254.169.254"], ["fd7a:115c:a1e0::2", "127.0.0.1"], [TS_A, "not an address"],
])
def test_a_name_that_resolves_to_a_tailnet_address_and_a_private_one_is_refused_in_either_order(answer):
    assert nodes.valid_peer_url("https://node-a.example.ts.net", SUFFIX, Resolver({"node-a.example.ts.net": answer})) is None


def test_every_address_of_a_good_name_is_returned_and_the_name_is_looked_up_once():
    res = Resolver({"node-a.example.ts.net": ["fd7a:115c:a1e0::2", TS_A]})
    t = nodes.check_peer_url("https://node-a.example.ts.net:8443", SUFFIX, res)
    assert t.host == "node-a.example.ts.net" and t.port == 8443 and t.url == "https://node-a.example.ts.net:8443"
    assert t.addrs == ("fd7a:115c:a1e0::2", TS_A) and res.calls == [("node-a.example.ts.net", 8443)]
    assert nodes.check_peer_url("https://[fd7a:115c:a1e0::2]", SUFFIX, res).url == "https://[fd7a:115c:a1e0::2]"
    assert len(res.calls) == 1, "an address literal needs no lookup"


def test_a_name_that_does_not_resolve_is_flagged_as_unresolved_not_as_wrong():
    with pytest.raises(nodes.PeerUrlError) as e:
        nodes.check_peer_url("https://gone.example.ts.net", SUFFIX, Resolver({}))
    assert e.value.unresolved is True
    with pytest.raises(nodes.PeerUrlError) as e:
        nodes.check_peer_url("https://gone.example.ts.net", SUFFIX, lambda h, p: [])
    assert e.value.unresolved is True
    with pytest.raises(nodes.PeerUrlError) as e:
        nodes.check_peer_url("https://gone.example.ts.net", SUFFIX, lambda h, p: 1 / 0)
    assert e.value.unresolved is False, "a resolver that breaks is a refusal, never a pass"
    with pytest.raises(nodes.PeerUrlError) as e:
        nodes.check_peer_url("http://gone.example.ts.net", SUFFIX, Resolver({}))
    assert e.value.unresolved is False


def test_in_tailnet_edges():
    for good in ("100.64.0.0", "100.100.100.100", "100.127.255.255", "fd7a:115c:a1e0::", "fd7a:115c:a1e0:ffff:ffff:ffff:ffff:ffff"):
        assert nodes.in_tailnet(good), good
    for bad in ("100.63.255.255", "100.128.0.0", "127.0.0.1", "169.254.169.254", "10.1.2.3", "::1", "fd7a:115c:a1e1::1", "fd7a:115c:a1df::1",
                "::ffff:100.100.1.2", "fe80::1", "fd7a:115c:a1e0::1%eth0", "", None, 5, "100.100.1"):
        assert not nodes.in_tailnet(bad), bad


# ---------------------------------------------------------------- the probe

def peer_row(online=True, dns="node-b.example.ts.net"):
    return {"ts_id": "nPEER1", "name": "node-b", "dns_name": dns, "online": online, "last_seen": None, "tags": [], "same_user": True, "ips": [], "os": "linux"}


NODE_B = {"node-b.example.ts.net": ["100.100.1.2"]}


def run_probe(net, ports=(443, 8443), res=None, row=None):
    return nd.probe(row or peer_row(), ports, SUFFIX, net, res or Resolver(NODE_B))


def test_a_200_hello_is_found_with_the_answering_url_and_the_node_id_and_connects_to_the_validated_address():
    net = Net()
    out = run_probe(net)
    assert out == {"state": "found", "url": "https://node-b.example.ts.net", "node_id": "ts:nPEER1CNTRL"}
    assert [(h, a, p) for h, a, p, _ in net.calls] == [("node-b.example.ts.net", "100.100.1.2", 443)]
    assert net.calls[0][3] == 2.0, "a two second timeout"


def test_the_second_port_answers_when_the_first_is_refused_and_its_port_is_in_the_url():
    net = Net({443: ConnectionRefusedError()})
    out = run_probe(net)
    assert out["state"] == "found" and out["url"] == "https://node-b.example.ts.net:8443"
    assert [c[2] for c in net.calls] == [443, 8443]


def test_the_ports_are_tried_in_the_order_given_and_stop_at_found():
    net = Net()
    assert run_probe(net, ports=(8443, 443))["url"] == "https://node-b.example.ts.net:8443"
    assert [c[2] for c in net.calls] == [8443]


@pytest.mark.parametrize("status", [401, 403])
def test_a_401_or_403_is_refuses_and_stops_the_loop(status):
    net = Net({443: (status, b'{"error":"no identity"}')})
    out = run_probe(net)
    assert out["state"] == "refuses" and out["node_id"] is None and out["url"] == "https://node-b.example.ts.net"
    assert len(net.calls) == 1


@pytest.mark.parametrize("answer, state", [
    (ConnectionRefusedError(), "no_ccboard"),
    (ssl.SSLError("handshake"), "no_tls"),
    (ssl.SSLCertVerificationError("bad certificate"), "no_tls"),
    (TimeoutError(), "unreachable"),
    (socket.timeout(), "unreachable"),
    (OSError("no route to host"), "unreachable"),
    (ConnectionResetError(), "unreachable"),
    (RuntimeError("anything at all"), "unreachable"),
    ((301, b""), "unreachable"),                                   # a redirect is never followed
    ((302, b""), "unreachable"), ((307, b""), "unreachable"), ((308, b""), "unreachable"),
    ((429, b""), "unreachable"),
    ((404, b"not found"), "no_ccboard"), ((500, b""), "no_ccboard"),
    ((200, b"<html>hello</html>"), "no_ccboard"),
    ((200, b'{"app":"other","api":1,"node_id":"ts:nX"}'), "no_ccboard"),
    ((200, b'{"app":"ccboard","api":"1","node_id":"ts:nX"}'), "no_ccboard"),
    ((200, b'{"app":"ccboard","api":true,"node_id":"ts:nX"}'), "no_ccboard"),
    ((200, b'{"app":"ccboard","api":1,"node_id":"../../etc"}'), "no_ccboard"),
    ((200, b'{"app":"ccboard","api":1}'), "no_ccboard"),
    ((200, b'[]'), "no_ccboard"), ((200, b'\xff\xfe'), "no_ccboard"),
])
def test_the_probe_state_table(answer, state):
    net = Net(default=answer)
    out = run_probe(net)
    assert out["state"] == state
    assert out["node_id"] is None and out["url"] is None
    assert [c[2] for c in net.calls] == [443, 8443], "no port answered, so both were tried"


def test_a_redirect_is_not_followed_not_even_to_another_tailnet_name():
    net = Net(default=(301, b""))
    assert run_probe(net)["state"] == "unreachable"
    assert len(net.calls) == 2, "one request per port, none for the Location"


def test_the_best_answer_over_the_ports_wins():
    assert run_probe(Net({443: TimeoutError(), 8443: ConnectionRefusedError()}))["state"] == "no_ccboard"
    assert run_probe(Net({443: ssl.SSLError("x"), 8443: ConnectionRefusedError()}))["state"] == "no_tls"
    assert run_probe(Net({443: ConnectionRefusedError(), 8443: TimeoutError()}))["state"] == "no_ccboard"
    assert run_probe(Net({443: TimeoutError(), 8443: (403, b"")}))["state"] == "refuses"
    assert run_probe(Net({443: ssl.SSLError("x"), 8443: HELLO}))["state"] == "found"


def test_only_the_hellos_three_keys_are_believed():
    body = json.dumps({"app": "ccboard", "api": 1, "node_id": "ts:nPEER1CNTRL", "name": "<script>", "url": "https://evil.example", "token": "x"}).encode()
    out = run_probe(Net(default=(200, body)))
    assert set(out) == {"state", "url", "node_id"} and out["url"] == "https://node-b.example.ts.net" and "evil" not in json.dumps(out)


def test_an_offline_peer_is_never_probed_and_never_looked_up():
    net, res = Net(), Resolver(NODE_B)
    assert nd.probe(peer_row(online=False), (443, 8443), SUFFIX, net, res) == {"state": "offline", "url": None, "node_id": None}
    assert net.calls == [] and res.calls == []


@pytest.mark.parametrize("dns", [None, "", "node-b.other-net.ts.net", "evil.example", "node-b.example.ts.net.evil.example", "127.0.0.1", "8.8.8.8"])
def test_a_row_whose_name_fails_the_rule_is_invalid_and_never_probed(dns):
    net, res = Net(), Resolver(NODE_B)
    out = nd.probe(peer_row(dns=dns), (443, 8443), SUFFIX, net, res)
    assert out["state"] == "invalid" and net.calls == []


def test_a_name_that_resolves_to_a_private_address_is_invalid_and_never_probed():
    for answer in (["10.0.0.5"], ["100.100.1.2", "10.0.0.5"], ["127.0.0.1"], ["169.254.169.254"]):
        net = Net()
        assert run_probe(net, res=Resolver({"node-b.example.ts.net": answer}))["state"] == "invalid"
        assert net.calls == []


def test_a_name_that_does_not_resolve_is_unreachable_not_invalid():
    net = Net()
    assert run_probe(net, res=Resolver({}))["state"] == "unreachable" and net.calls == []


def test_dns_rebinding_the_name_is_resolved_once_and_the_connection_goes_to_the_first_answer():
    """The first lookup is a tailnet address, every later one would be a private address; a second lookup must never be trusted or even made."""
    res = Resolver({"node-b.example.ts.net": [["100.100.1.2"], ["10.0.0.5"], ["127.0.0.1"]]})
    net = Net({443: ConnectionRefusedError()})
    out = run_probe(net, res=res)
    assert out["state"] == "found"
    assert len(res.calls) == 1, "one lookup for the whole probe, both ports"
    assert [c[1] for c in net.calls] == ["100.100.1.2", "100.100.1.2"], "every connection to the validated address, never the later answer"


def test_the_probe_never_raises_whatever_its_inputs_and_collaborators_do():
    assert nd.probe(peer_row(), (443,), SUFFIX, Net(default=RuntimeError("x")), Resolver(NODE_B))["state"] == "unreachable"
    assert nd.probe(peer_row(), (443,), SUFFIX, Net(), lambda h, p: 1 / 0)["state"] == "invalid"
    assert nd.probe({}, (443,), SUFFIX, Net(), Resolver(NODE_B))["state"] == "offline"
    assert nd.probe(None, (443,), SUFFIX, Net(), Resolver(NODE_B))["state"] == "unreachable"
    assert nd.probe(peer_row(), None, SUFFIX, Net(), Resolver(NODE_B))["state"] == "unreachable"


# ---------------------------------------------------------------- the real connection (pinned address, verified name)

def test_the_pinned_connection_dials_the_validated_address_and_checks_the_certificate_for_the_name(monkeypatch):
    dialled, wrapped = [], []

    class Ctx:
        def wrap_socket(self, sock, server_hostname=None):
            wrapped.append(server_hostname)
            raise ssl.SSLError("stop here")

    class Sock:
        closed = False

        def close(self):
            self.closed = True
    s = Sock()
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout=None, **k: (dialled.append((addr, timeout)), s)[1])
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: Ctx())
    with pytest.raises(ssl.SSLError):
        nd.pinned_transport("node-b.example.ts.net", "100.100.1.2", 8443, 2.0)
    assert dialled == [(("100.100.1.2", 8443), 2.0)], "the connection goes to the validated address, not to a name"
    assert wrapped == ["node-b.example.ts.net"], "the certificate is checked for the name"
    assert s.closed, "the socket is closed when the handshake fails"


def test_the_default_tls_context_verifies_the_certificate_and_the_host_name():
    c = nd._Pinned("node-b.example.ts.net", "100.100.1.2", 443, 2.0)
    assert c._context.verify_mode == ssl.CERT_REQUIRED and c._context.check_hostname is True


def test_a_server_that_is_not_speaking_tls_ends_in_a_tls_error_on_the_pinned_address():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        try:
            c, _ = srv.accept()
            c.sendall(b"HTTP/1.1 301 Moved Permanently\r\nLocation: https://evil.example/\r\n\r\n")
            time.sleep(0.2)
            c.close()
        except OSError:
            pass
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        with pytest.raises(ssl.SSLError):
            nd.pinned_transport("node-b.example.ts.net", "127.0.0.1", port, 2.0)
    finally:
        srv.close()
        t.join(2)
    assert nd._classify  # keep the import used
    assert nd._error_state(ssl.SSLError()) == "no_tls"


def test_the_request_has_no_header_of_its_own(monkeypatch):
    sent = []

    class FakeResp:
        status = 200

        def read(self, n=-1):
            return b""

    class FakeConn(nd._Pinned):
        def connect(self):
            self.sock = None

        def send(self, data):
            sent.append(bytes(data))

        def getresponse(self):
            return FakeResp()
    monkeypatch.setattr(nd, "_Pinned", FakeConn)
    nd.pinned_transport("node-b.example.ts.net", "100.100.1.2", 443, 2.0)
    text = b"".join(sent).decode()
    lines = [ln.split(":")[0].lower() for ln in text.split("\r\n")[1:] if ":" in ln]
    assert text.startswith("GET /api/node/hello HTTP/1.1")
    assert set(lines) <= {"host", "accept-encoding"}, lines


# ---------------------------------------------------------------- discover: states, cache, shape

def test_a_plain_read_sends_nothing_and_lists_every_candidate_with_what_it_can_say_without_asking(world):
    out = world["discover"]()
    assert out["tailscale"] == {"ok": True, "reason": "", "variant": "linux"}
    assert world["net"].calls == [] and world["res"].calls == []
    by = {r["ts_id"]: r for r in out["rows"]}
    assert by["nSAME1"]["state"] == "unchecked" and by["nMAC01"]["state"] == "offline" and by["nODD01"]["state"] == "invalid"
    assert by["nMAC01"]["last_seen"] == "2026-10-09T18:22:41Z"
    assert ids(out["rows"]) == ONLINE_DEFAULT + ["nMAC01"]
    assert world["db"].kv_get(nd.KV) is None, "a plain read writes nothing"


def test_a_refresh_probes_the_online_candidates_and_keeps_the_result_in_kv(world):
    out = world["discover"](refresh=True)
    by = {r["ts_id"]: r for r in out["rows"]}
    assert {k: v["state"] for k, v in by.items()} == {"nTAG01": "found", "nDEV01": "found", "nDEV02": "found", "nSAME1": "found", "nODD01": "invalid",
                                                   "nPUB01": "found", "nMAC01": "offline"}
    assert by["nSAME1"]["node_id"] == "ts:nPEER1CNTRL" and by["nSAME1"]["url"] == "https://node-b.example.ts.net"
    assert all(set(r) == ROW_KEYS for r in out["rows"])
    probed = {c[0] for c in world["net"].calls}
    assert "alice-mac.example.ts.net" not in probed and "odd name!.example.ts.net" not in probed, "offline and invalid rows are never probed"
    rec = world["db"].kv_get(nd.KV)["value"]
    assert set(rec["rows"]) >= {"nSAME1", "nTAG01"} and rec["rows"]["nSAME1"]["state"] == "found"
    again = world["discover"]()
    assert {r["ts_id"]: r["state"] for r in again["rows"]} == {k: v["state"] for k, v in by.items()}, "a plain read serves the cache"
    assert len(world["net"].calls) == len(probed) * 1, "and sends nothing"


def test_a_result_older_than_30_seconds_is_stale_with_its_age_and_is_never_deleted(world):
    world["discover"](refresh=True)
    world["clock"][0] += 29
    row = next(r for r in world["discover"]()["rows"] if r["ts_id"] == "nSAME1")
    assert row["stale"] is False and row["age"] == 29
    world["clock"][0] += 2
    row = next(r for r in world["discover"]()["rows"] if r["ts_id"] == "nSAME1")
    assert row["stale"] is True and row["age"] == 31 and row["state"] == "found" and row["at"]
    world["clock"][0] += 86400
    row = next(r for r in world["discover"]()["rows"] if r["ts_id"] == "nSAME1")
    assert row["state"] == "found" and row["stale"] is True, "never deleted for age"


def test_a_second_refresh_leaves_a_fresh_result_alone_and_a_later_one_probes_again(world):
    world["discover"](refresh=True)
    n = len(world["net"].calls)
    world["clock"][0] += 2
    world["discover"](refresh=True)
    assert len(world["net"].calls) == n, "two taps in two seconds probe once"
    world["clock"][0] += nd.MIN_REPROBE
    world["discover"](refresh=True)
    assert len(world["net"].calls) == 2 * n


def test_a_peer_that_goes_offline_shows_offline_and_a_peer_that_is_no_longer_a_candidate_drops_out_of_the_cache(world):
    world["discover"](refresh=True)
    for p in world["status"]["Peer"].values():
        if p["ID"] == "nSAME1":
            p["Online"] = False
        if p["ID"] == "nDEV02":
            p["UserID"] = 2002
    world["clock"][0] += 60
    out = world["discover"](refresh=True)
    by = {r["ts_id"]: r for r in out["rows"]}
    assert by["nSAME1"]["state"] == "offline" and by["nSAME1"]["stale"] is False
    assert "nDEV02" not in by
    assert "nDEV02" not in world["db"].kv_get(nd.KV)["value"]["rows"]


def test_the_answer_holds_no_address_and_nothing_outside_the_tailnet_ranges(world):
    out = world["discover"](refresh=True)
    text = json.dumps(out)
    for needle in ("203.0.113.5", "169.254.169.254", "127.0.0.1", "10.0.0.", "ips"):
        assert needle not in text, needle
    import ipaddress
    for tok in re.findall(r"[0-9a-fA-F:.]{3,}", text):
        try:
            ipaddress.ip_address(tok)
        except ValueError:
            continue                                    # a time stamp or a version, not an address
        assert nodes.in_tailnet(tok), tok
    assert set(out) == {"at", "tailscale", "rows"} and set(out["tailscale"]) == {"ok", "reason", "variant"}


def test_a_tag_setting_of_none_leaves_only_the_same_users_devices(world, monkeypatch):
    monkeypatch.setattr(settings, "node_tags", ())
    assert "nTAG01" not in ids(world["discover"]()["rows"])
    monkeypatch.setattr(settings, "node_tags", ("tag:ccboard", "tag:other"))
    got = ids(world["discover"]()["rows"])
    assert "nTAG01" in got and "nTAG02" in got


def test_owner_is_user_or_tag(world):
    by = {r["ts_id"]: r for r in world["discover"]()["rows"]}
    assert by["nSAME1"]["owner"] == "user" and by["nTAG01"]["owner"] == "tag" and by["nTAG01"]["tags"] == ["tag:ccboard"]


def test_at_most_four_probes_are_in_flight(world):
    st = world["status"]
    for i in range(12):
        p = dict(next(iter(st["Peer"].values())))
        p.update({"ID": f"nBULK{i:02d}", "PublicKey": f"nodekey:bulk{i}", "HostName": f"bulk{i}", "DNSName": f"bulk{i}.example.ts.net.", "Online": True,
                  "UserID": 1001, "Tags": None, "TailscaleIPs": [f"100.100.2.{i}"]})
        st["Peer"][p["PublicKey"]] = p
    world["res"] = Resolver(status=st)
    world["net"] = Net(delay=0.05)
    out = world["discover"](refresh=True)
    assert sum(1 for r in out["rows"] if r["state"] == "found") >= 12
    assert 2 <= world["net"].peak <= nd.MAX_IN_FLIGHT == 4, world["net"].peak


def test_the_pool_alone_holds_the_limit_without_the_global_slots(world, monkeypatch):
    st = world["status"]
    for i in range(12):
        p = dict(next(iter(st["Peer"].values())))
        p.update({"ID": f"nBULK{i:02d}", "PublicKey": f"nodekey:bulk{i}", "HostName": f"bulk{i}", "DNSName": f"bulk{i}.example.ts.net.", "Online": True,
                  "UserID": 1001, "Tags": None, "TailscaleIPs": [f"100.100.2.{i}"]})
        st["Peer"][p["PublicKey"]] = p
    world["res"] = Resolver(status=st)
    monkeypatch.setattr(nd, "_slots", threading.BoundedSemaphore(100))
    world["net"] = Net(delay=0.05)
    world["discover"](refresh=True)
    assert world["net"].peak <= 4, world["net"].peak


def test_probes_started_from_many_threads_at_once_still_share_four_slots(world):
    world["net"] = Net(delay=0.05)
    threads = [threading.Thread(target=nd.probe, args=(peer_row(), (443,), SUFFIX, world["net"], Resolver(NODE_B))) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(world["net"].calls) == 12 and 2 <= world["net"].peak <= 4, world["net"].peak


def test_a_tagged_peer_is_never_the_same_user_even_when_its_user_id_matches_ours(world):
    for p in world["status"]["Peer"].values():
        if p["ID"] == "nTAG02":
            p["UserID"] = 1001
    got = {r["ts_id"]: r for r in nd.candidates(world["status"], ("tag:ccboard",), 1001)}
    assert "nTAG02" not in got, "tag:other is not a configured tag, and a tagged device has no user"
    got = {r["ts_id"]: r for r in nd.candidates(world["status"], ("tag:other",), 1001)}
    assert got["nTAG02"]["same_user"] is False


def test_a_refresh_answers_at_its_deadline_with_unreachable_for_the_rows_still_waiting(world, monkeypatch):
    monkeypatch.setattr(nd, "REFRESH_DEADLINE", 0.2)
    world["net"].gate = threading.Event()
    t0 = time.monotonic()
    out = world["discover"](refresh=True)
    took = time.monotonic() - t0
    world["net"].gate.set()
    assert took < 3, took
    assert {r["state"] for r in out["rows"] if r["online"] and r["state"] != "invalid"} == {"unreachable"}


def test_stragglers_past_the_deadline_keep_the_refresh_lock_so_probes_never_pile_up(world, monkeypatch):
    """Security review: a refresh that answers at its deadline must not let the next refresh start more probes while its stragglers still run;
    otherwise pressing Refresh every few seconds against hanging peers grows the probe threads past MAX_IN_FLIGHT."""
    monkeypatch.setattr(nd, "REFRESH_DEADLINE", 0.2)
    monkeypatch.setattr(nd, "MIN_REPROBE", 0)
    world["net"].gate = threading.Event()
    world["discover"](refresh=True)                              # answers at the deadline; its probes hang on the gate
    first = len(world["net"].calls)
    assert first >= 1
    for _ in range(5):                                           # Refresh pressed again and again while the stragglers hang
        world["discover"](refresh=True)
    assert len(world["net"].calls) == first, "no new probe starts while the last refresh still has probes in flight"
    assert not nd._refresh_lock.acquire(blocking=False), "the lock stays held by the stragglers"
    assert world["net"].peak <= nd.MAX_IN_FLIGHT
    world["net"].gate.set()
    end = time.monotonic() + 10
    while time.monotonic() < end and not nd._refresh_lock.acquire(blocking=False):
        time.sleep(0.05)
    else:
        nd._refresh_lock.release()
    world["discover"](refresh=True)                              # the stragglers ended: a refresh probes again
    assert len(world["net"].calls) > first


def test_a_hanging_name_lookup_is_bounded_and_never_keeps_the_refresh_lock(world, monkeypatch):
    """Security review: getaddrinfo has no timeout. A lookup that never returns must not hold a probe (and so the refresh lock) forever:
    the row is unreachable after RESOLVE_TIMEOUT and the lock comes back."""
    monkeypatch.setattr(nd, "RESOLVE_TIMEOUT", 0.2)
    stuck = threading.Event()
    hang = lambda host, port: (stuck.wait(30), [])[1]           # a resolver that never answers on its own
    t0 = time.monotonic()
    out = world["discover"](refresh=True, resolver=hang)
    assert time.monotonic() - t0 < 5
    assert {r["state"] for r in out["rows"] if r["online"] and r["state"] != "invalid"} <= {"unreachable"}
    end = time.monotonic() + 5
    while time.monotonic() < end and not nd._refresh_lock.acquire(blocking=False):
        time.sleep(0.05)
    else:
        nd._refresh_lock.release()
    assert time.monotonic() < end, "the refresh lock came back although the lookups still hang"
    stuck.set()


def test_lookups_are_capped_and_a_full_cap_refuses_at_once(monkeypatch):
    monkeypatch.setattr(nd, "_lookups", threading.BoundedSemaphore(1))
    monkeypatch.setattr(nd, "RESOLVE_TIMEOUT", 0.2)
    stuck = threading.Event()
    look = nd.bounded_resolver(lambda h, p: (stuck.wait(30), ["100.64.0.1"])[1])
    with pytest.raises(OSError, match="timed out"):
        look("node-a.example.ts.net", 443)                    # takes the only slot and keeps it while it hangs
    t0 = time.monotonic()
    with pytest.raises(OSError, match="too many"):
        look("node-b.example.ts.net", 443)
    assert time.monotonic() - t0 < 0.1, "refused at once, not after another timeout"
    stuck.set()
    time.sleep(0.1)
    assert nd.bounded_resolver(lambda h, p: ["100.64.0.2"])("node-c.example.ts.net", 443) == ["100.64.0.2"], "the slot came back"


def test_a_second_refresh_while_one_runs_is_answered_from_the_cache(world):
    assert nd._refresh_lock.acquire(blocking=False)
    try:
        out = world["discover"](refresh=True)
    finally:
        nd._refresh_lock.release()
    assert world["net"].calls == [] and {r["state"] for r in out["rows"]} <= {"unchecked", "offline", "invalid"}


def test_discover_never_raises(world, monkeypatch):
    class Broken:
        def kv_get(self, k):
            raise RuntimeError("db")

        def kv_set(self, k, v):
            raise RuntimeError("db")
    out = nd.discover(Broken(), refresh=True, clock=lambda: 1.0)
    assert out["tailscale"]["ok"] is False and out["rows"] == [] and "RuntimeError" in out["tailscale"]["reason"]
    out = nd.discover(Broken(), refresh=False, clock=lambda: 1.0)
    assert out["tailscale"]["ok"] is True, "a plain read does not need the database to answer"


# ---------------------------------------------------------------- Tailscale missing, logged out, GUI only

def test_no_tailscale_command_is_a_readable_state_with_no_rows_and_no_request(world):
    world["cli"] = None
    out = world["discover"](refresh=True)
    assert out["tailscale"]["ok"] is False and "tailscale" in out["tailscale"]["reason"].lower() and out["rows"] == []
    assert world["net"].calls == [] and world["res"].calls == []


def test_a_daemon_that_does_not_answer_says_so(world):
    world["status"] = None
    out = world["discover"](refresh=True)
    assert out["tailscale"]["ok"] is False and "not running" in out["tailscale"]["reason"] and out["rows"] == []


def test_logged_out_says_to_run_tailscale_up(world):
    world["status"] = load("ts_status_logged_out.json")
    out = world["discover"](refresh=True)
    assert out["tailscale"]["ok"] is False and "run `tailscale up`" in out["tailscale"]["reason"] and "not logged in" in out["tailscale"]["reason"]
    assert out["rows"] == [] and world["net"].calls == []


def test_the_mac_gui_app_gets_its_own_words_and_its_status_shape_works(world):
    world["variant"] = "macos-standalone"
    world["cli"] = ts.Cli(["/Applications/Tailscale.app/Contents/MacOS/Tailscale"], {"TAILSCALE_BE_CLI": "1"})
    world["status"] = load("ts_status_logged_out.json")
    out = world["discover"]()
    assert "Tailscale app" in out["tailscale"]["reason"] and "tailscale up" not in out["tailscale"]["reason"] and out["tailscale"]["variant"] == "macos-standalone"
    world["status"] = load("ts_status_mac_app.json")
    world["res"] = Resolver(status=world["status"])
    out = world["discover"](refresh=True)
    assert out["tailscale"]["ok"] is True and sorted(r["name"] for r in out["rows"]) == ["box", "node-a"]
    assert {r["state"] for r in out["rows"]} == {"found"}


def test_magicdns_off_is_a_reason_not_a_list_of_invalid_rows(world):
    world["status"] = load("ts_status_no_magicdns.json")
    out = world["discover"](refresh=True)
    assert out["tailscale"]["ok"] is False and "MagicDNS" in out["tailscale"]["reason"] and out["rows"] == []


def test_the_status_reading_goes_through_the_tailscale_module(monkeypatch):
    """No fake of nodes._ts_status here: the reading is app.tailscale.call, so the Mac app bundle's command (TAILSCALE_BE_CLI) is what runs."""
    nodes.reset()
    calls = []
    cli = ts.Cli(["/Applications/Tailscale.app/Contents/MacOS/Tailscale"], {"TAILSCALE_BE_CLI": "1"})
    monkeypatch.setattr(nodes, "_ts_status", REAL_TS_STATUS)
    monkeypatch.setattr(ts, "find_cli", lambda: cli)
    monkeypatch.setattr(ts, "variant", lambda c=None: "macos-standalone")
    monkeypatch.setattr(ts, "_exec", lambda argv, env, timeout: (calls.append((argv, env)), subprocess.CompletedProcess(argv, 0, (FIX / "ts_status_mac_app.json").read_text(), ""))[1])
    monkeypatch.setattr(settings, "runtime", "systemd")
    st, info = nd.read_tailscale(fresh=True)
    assert info["ok"] is True and nd.magic_suffix(st) == SUFFIX
    assert calls == [([cli.exe, "status", "--json"], {"TAILSCALE_BE_CLI": "1"})]


# ---------------------------------------------------------------- the endpoint

def test_the_route_needs_an_identity_and_a_plain_read_needs_no_csrf_header(lite_client, world):
    assert lite_client.get("/api/nodes/discover").status_code == 403
    r = lite_client.get("/api/nodes/discover", headers=ID)
    assert r.status_code == 200 and r.json()["tailscale"]["ok"] is True and len(r.json()["rows"]) == len(ONLINE_DEFAULT) + 1
    assert world["net"].calls == []


def test_a_refresh_needs_the_csrf_header_because_it_sends_requests(lite_client, world):
    r = lite_client.get("/api/nodes/discover?refresh=1", headers=ID)
    assert r.status_code == 403 and world["net"].calls == []
    r = lite_client.get("/api/nodes/discover?refresh=1", headers=H)
    assert r.status_code == 200 and {x["state"] for x in r.json()["rows"]} == {"found", "invalid", "offline"}
    assert world["net"].calls


def test_the_endpoint_answers_200_with_a_reason_when_tailscale_is_missing_or_logged_out(lite_client, world):
    world["cli"] = None
    r = lite_client.get("/api/nodes/discover?refresh=1", headers=H)
    assert r.status_code == 200 and r.json()["tailscale"]["ok"] is False and r.json()["rows"] == []
    world["cli"] = ts.Cli(["/opt/fake/tailscale"], {})
    world["status"] = load("ts_status_logged_out.json")
    nodes.reset()
    r = lite_client.get("/api/nodes/discover?refresh=1", headers=H)
    assert r.status_code == 200 and "tailscale up" in r.json()["tailscale"]["reason"]


def test_a_board_with_no_tailscale_and_no_peers_makes_no_request(lite_client, monkeypatch):
    """The conftest default: Tailscale cannot be read. Nothing is looked up or connected, with or without a refresh."""
    def boom(*a, **k):
        raise AssertionError("a request left the board")
    monkeypatch.setattr(nd, "pinned_transport", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    for q in ("", "?refresh=1"):
        r = lite_client.get("/api/nodes/discover" + q, headers=H)
        assert r.status_code == 200 and r.json()["rows"] == []


def test_nothing_runs_at_import_or_at_start(lite_client, world):
    code = ("import sys, threading, socket\n"
            "sys.path.insert(0, %r)\n"
            "def boom(*a, **k): raise AssertionError('network at import')\n"
            "socket.create_connection = boom; socket.getaddrinfo = boom\n"
            "before = threading.active_count()\n"
            "import app.nodes_discovery as nd\n"
            "assert threading.active_count() == before, threading.enumerate()\n"
            "assert not [t for t in threading.enumerate() if t is not threading.main_thread()]\n"
            "print('ok')\n") % str(ROOT)
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr
    assert not [t for t in threading.enumerate() if t.name.startswith("ccboard-discover")], "the board's start launched no discovery thread"
    assert world["net"].calls == [] and world["res"].calls == []
    src = (ROOT / "app" / "nodes_discovery.py").read_text()
    # bounded_resolver starts a daemon lookup thread inside a probe (a name lookup with a deadline, security review); nothing else may
    lookup = re.search(r"^def bounded_resolver\(.*?^    return look\n", src, re.M | re.S)
    assert lookup and "daemon=True" in lookup.group(0) and 'name="ccboard-lookup"' in lookup.group(0)
    rest = src.replace(lookup.group(0), "")
    assert not re.search(r"^\s*(?:threading\.Timer|threading\.Thread)\(|\.start\(\)|schedule|sched\.", rest, re.M), "no timer, no thread of its own"


# ---------------------------------------------------------------- settings

@pytest.mark.parametrize("raw, ports, bad", [
    (None, (443, 8443), False), ("", (443, 8443), False), ("  ", (443, 8443), False),
    ("443,8443", (443, 8443), False), ("8443", (8443,), False), ("8443 , 443", (8443, 443), False), ("443,443,8443", (443, 8443), False),
    ("443,abc", (443,), True), ("0", (443, 8443), True), ("70000,8443", (8443,), True), ("-1", (443, 8443), True), ("abc", (443, 8443), True),
    ("1,2,3,4,5,6,7,8,9,10", (1, 2, 3, 4, 5, 6, 7, 8), False),
])
def test_node_ports_parse(raw, ports, bad):
    assert parse_node_ports(raw) == (ports, bad)


@pytest.mark.parametrize("raw, tags, bad", [
    (None, ("tag:ccboard",), False), ("", ("tag:ccboard",), False),
    ("tag:ccboard", ("tag:ccboard",), False), ("ccboard", ("tag:ccboard",), False), ("tag:a,tag:b", ("tag:a", "tag:b"), False),
    ("tag:a tag:b", ("tag:a", "tag:b"), False), ("none", (), False), ("NONE", (), False), ("tag:a,tag:a", ("tag:a",), False),
    ("tag:a,Bad Tag", ("tag:a",), True), ("tag:", (), True), ("tag:1abc", (), True), ("tag:ok,tag:UPPER", ("tag:ok",), True),
])
def test_node_tags_parse(raw, tags, bad):
    assert parse_node_tags(raw) == (tags, bad)


def test_the_settings_object_reads_both_keys():
    from app.config import Settings
    s = Settings({"CCBOARD_NODE_PORTS": "9443,443", "CCBOARD_NODE_TAGS": "tag:ccboard,tag:lab"})
    assert s.node_ports == (9443, 443) and s.node_tags == ("tag:ccboard", "tag:lab") and not s.node_ports_bad and not s.node_tags_bad
    d = Settings({})
    assert d.node_ports == (443, 8443) and d.node_tags == ("tag:ccboard",)


# ---------------------------------------------------------------- doctor rows

def test_the_three_rows_are_registered_in_the_box_group():
    by = {c[0]: c for c in doctor.CHECKS}
    for cid in ("tailscale-status", "nodes-port", "nodes-tagged-self"):
        assert by[cid][1] == "box", cid


def test_nodes_port_passes_on_a_covered_port_and_warns_with_the_fix_otherwise(monkeypatch, projects_dir):
    monkeypatch.setattr(settings, "node_ports", (443, 8443))
    monkeypatch.setattr(settings, "node_ports_bad", False)
    monkeypatch.setattr(settings, "ccboard_https_port", 8443)
    out = doctor._c_nodes_port(None)
    assert out.status == "pass" and "8443" in out.detail and out.fix is None
    monkeypatch.setattr(settings, "ccboard_https_port", 9443)
    out = doctor._c_nodes_port(None)
    assert out.status == "warn" and "9443" in out.detail and "CCBOARD_NODE_PORTS" in out.fix["text"] and out.fix["cmd"] == "CCBOARD_NODE_PORTS=443,8443,9443"
    monkeypatch.setattr(settings, "node_ports_bad", True)
    out = doctor._c_nodes_port(None)
    assert out.status == "warn" and "not a port" in out.detail


def test_nodes_tagged_self_warns_with_the_exact_words_when_this_device_is_tagged(world):
    world["status"] = load("ts_status_tagged_self.json")
    out = doctor._c_nodes_tagged_self(None)
    assert out.status == "warn" and "this device is tagged: browsers on it carry no identity" in out.detail and out.fix["text"]
    world["status"] = load("ts_status_tailnet.json")
    nodes.reset()
    out = doctor._c_nodes_tagged_self(None)
    assert out.status == "pass" and out.fix is None
    world["status"] = None
    nodes.reset()
    assert doctor._c_nodes_tagged_self(None).status == "skip"


def test_nodes_tagged_self_names_a_malformed_tag_list(world, monkeypatch):
    monkeypatch.setattr(settings, "node_tags_bad", True)
    out = doctor._c_nodes_tagged_self(None)
    assert out.status == "warn" and "CCBOARD_NODE_TAGS" in out.detail and "tag:ccboard" in out.fix["text"]


def test_tailscale_status_reports_what_discovery_would_see(world):
    out = doctor._c_tailscale_status(None)
    assert out.status == "pass" and SUFFIX in out.detail and "7 devices could be a node (6 online" in out.detail and "tag:ccboard" in out.detail
    world["status"]["CertDomains"] = []
    out = doctor._c_tailscale_status(None)
    assert out.status == "warn" and "HTTPS certificates" in out.detail and out.fix["text"]
    world["status"] = load("ts_status_logged_out.json")
    nodes.reset()
    out = doctor._c_tailscale_status(None)
    assert out.status == "warn" and "tailscale up" in out.detail
    world["cli"] = None
    assert doctor._c_tailscale_status(None).status == "skip"


def test_the_doctor_rows_send_no_request(world):
    for fn in (doctor._c_tailscale_status, doctor._c_nodes_port, doctor._c_nodes_tagged_self):
        fn(None)
    assert world["net"].calls == [] and world["res"].calls == []


# ---------------------------------------------------------------- the demo fixture

def test_the_demo_fixture_has_the_endpoint_shape_and_the_four_states():
    d = json.loads((ROOT / "app" / "static" / "demo" / "nodes-discover.json").read_text())
    assert set(d) == {"at", "tailscale", "rows"} and d["tailscale"]["ok"] is True
    assert all(set(r) == ROW_KEYS for r in d["rows"])
    assert {"found", "refuses", "offline", "no_ccboard"} <= {r["state"] for r in d["rows"]}
    assert all(r["state"] in nd.STATES for r in d["rows"])
    off = next(r for r in d["rows"] if r["state"] == "offline")
    assert off["online"] is False and off["last_seen"] and off["url"] is None
    assert all(r["node_id"] for r in d["rows"] if r["state"] == "found") and all(r["url"].startswith("https://") for r in d["rows"] if r["state"] == "found")
    assert {r["owner"] for r in d["rows"]} == {"user", "tag"}
    assert len({r["ts_id"] for r in d["rows"]}) == len(d["rows"])
