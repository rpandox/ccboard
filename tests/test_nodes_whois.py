"""Issue #136: the optional device binding (Tailscale whois) and app-capability check for calls from paired nodes.

A fake Tailscale (`ts.call` replaced: whois, version, serve status) and a test client with a loopback peer on the board's loopback address (`serve`),
behind uvicorn's own ProxyHeadersMiddleware with uvicorn's default trusted hosts, stand in for `tailscale serve` in front of `uvicorn app.main:app`;
the `direct` client (peer 100.64.0.50 on the board's tailnet address) stands in for a caller that reached the board directly. Nothing runs the real
tailscale, nothing opens a connection, the whois clock is a variable.

  off by default   token mode makes no whois call, reads no forwarded header, adds no audit row or pair column value
  binding          a pair made under whois stores the device; the right device is 200, another is 403 device_mismatch with an audit row
  tags             a tagged caller needs a tag from CCBOARD_NODE_TAGS
  fail closed      whois down, timing out or raising: 503 tailscale_unavailable and an audit row, never the token alone
  header trust     a non-loopback peer is refused before X-Forwarded-For, the login header or the capability header is read
  capabilities     plain JSON, RFC 2047, two grants merged, a missing op, an unparseable value, an empty header
  cache            60 s per address, 20 uncached lookups a minute (429 above)
  board-bind       a board bound to 0.0.0.0 keeps the whois modes off and says why
  doctor           board-bind and node-auth in each mode; the grant snippet names no real domain or host
"""
from __future__ import annotations

import base64
import contextlib
import email.charset
import email.header
import json
import re
import threading

import pytest

from app import nodes, nodes_whois
from app import tailscale as ts
from app.config import Settings, parse_node_auth, parse_node_cap, settings, uvicorn_host

DEV_A, DEV_B, DEV_T, DEV_X = "100.64.0.11", "100.64.0.12", "100.64.0.13", "100.64.0.14"
ID_A, ID_B, ID_T, ID_X = "nAAAA1CNTRL", "nBBBB2CNTRL", "nTTTT3CNTRL", "nXXXX4CNTRL"
CAP = "example.com/cap/ccboard"
CALLER = {"id": "ts:nCALLER000001", "name": "caller", "url": "https://100.64.0.9"}
WHOIS_ACTIONS = set(nodes_whois.REASONS)


class FakeTailscale:
    """`ts.call` for whois, version and serve status; records every argv."""

    def __init__(self):
        self.devices = {DEV_A: (ID_A, "node-a", []), DEV_B: (ID_B, "node-b", []), DEV_T: (ID_T, "node-t", ["tag:ccboard"]), DEV_X: (ID_X, "node-x", ["tag:other"])}
        self.calls: list[list[str]] = []
        self.whois_mode = "ok"
        self.version = "1.104.1"
        self.serve = {"Web": {"node-a.example.ts.invalid:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8000", "AcceptAppCaps": [CAP]}}}}}

    def whois_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "whois"]

    def __call__(self, args, *, timeout=None, sudo=False):
        args = list(args)
        self.calls.append(args)
        if args[0] == "whois":
            if self.whois_mode == "down":
                return ts.Result(-1, "", "failed to connect to local tailscaled", "not-running")
            if self.whois_mode == "timeout":
                return ts.Result(-1, "", "timed out", "timeout")
            if self.whois_mode == "raise":
                raise RuntimeError("boom")
            if self.whois_mode == "junk":
                return ts.Result(0, "not json", "", "ok")
            dev = self.devices.get(args[2])
            if dev is None:
                return ts.Result(1, "", "peer not found", "failed")
            sid, name, tags = dev
            return ts.Result(0, json.dumps({"Node": {"StableID": sid, "Name": name + ".example.ts.invalid.", "Tags": tags},
                                            "UserProfile": {"LoginName": "tagged-devices" if tags else "owner"}}), "", "ok")
        if args[0] == "version":
            return ts.Result(0, f"{self.version}\n  tailscale commit: x\n", "", "ok") if self.version else ts.Result(-1, "", "missing", "not-installed")
        if args[:2] == ["serve", "status"]:
            return ts.Result(0, json.dumps(self.serve), "", "ok") if self.serve is not None else ts.Result(-1, "", "denied", "denied")
        return ts.Result(-1, "", "not faked", "failed")


@pytest.fixture
def fake_ts(monkeypatch):
    fake = FakeTailscale()
    clock = [1000.0]
    monkeypatch.setattr(ts, "call", fake)
    monkeypatch.setattr(ts, "_whois_clock", lambda: clock[0])
    monkeypatch.setattr(nodes_whois, "listeners", lambda port: None)
    monkeypatch.setattr(settings, "bind_host", "127.0.0.1")
    monkeypatch.setattr(settings, "node_auth", "token")
    monkeypatch.setattr(settings, "node_auth_bad", False)
    monkeypatch.setattr(settings, "node_cap", "")
    monkeypatch.setattr(settings, "node_cap_bad", False)
    monkeypatch.setattr(settings, "node_tags", ("tag:ccboard",))
    monkeypatch.setattr(settings, "forwarded_allow_argv", None, raising=False)
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    monkeypatch.setattr(nodes, "_ts", lambda: {"Self": {"Tags": []}})          # this board's own status: readable, untagged
    ts.whois_forget()
    nodes_whois.reset()
    fake.clock = clock
    yield fake
    ts.whois_forget()
    nodes_whois.reset()


def set_mode(monkeypatch, mode, cap=""):
    monkeypatch.setattr(settings, "node_auth", mode)
    monkeypatch.setattr(settings, "node_cap", cap)
    nodes_whois.reset()


def uvicorn_default_stack(app):
    """The app as `uvicorn app.main:app` runs it: wrapped in uvicorn's own ProxyHeadersMiddleware with the trusted hosts uvicorn's Config picks by
    default (proxy headers on, FORWARDED_ALLOW_IPS unset: 127.0.0.1 and ::1)."""
    from uvicorn.config import Config
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    cfg = Config(app, log_config=None, forwarded_allow_ips=None)
    assert cfg.proxy_headers is True and cfg.forwarded_allow_ips == "127.0.0.1,::1", "uvicorn's defaults changed"
    return ProxyHeadersMiddleware(app, trusted_hosts=cfg.forwarded_allow_ips)


@contextlib.contextmanager
def _board(monkeypatch, client, local="127.0.0.1", proxy=True):
    """The board behind uvicorn's default proxy headers (proxy=False: as --no-proxy-headers runs it). `client` is the TCP peer, `local` the address the
    connection arrived on (the ASGI `server`)."""
    from fastapi.testclient import TestClient
    from app import hooks, main, notify
    from app.db import DB
    # Device checks are exercised with the original unsigned token protocol.
    monkeypatch.setattr(settings, "node_keys", "prefer")

    @contextlib.asynccontextmanager
    async def lite_lifespan(app):
        settings.validate()
        main.db = DB(settings.db_path)
        hooks.ensure_token()
        notify.set_db(main.db)
        yield

    monkeypatch.setattr(main.app.router, "lifespan_context", lite_lifespan)
    monkeypatch.setattr(main, "sched", None)
    monkeypatch.setattr(main, "indexer", None)
    with TestClient(uvicorn_default_stack(main.app) if proxy else main.app, base_url=f"http://{local}:8000", client=client) as c:
        yield c, main.db


@pytest.fixture
def serve(projects_dir, monkeypatch, fake_ts):
    """The board as tailscale serve reaches it: the TCP peer is loopback, on the board's loopback address, through uvicorn's default proxy headers."""
    with _board(monkeypatch, ("127.0.0.1", 40000)) as (c, db):
        yield c, db, fake_ts


@pytest.fixture
def direct(projects_dir, monkeypatch, fake_ts):
    """The board reached directly, not through Serve: the TCP peer is a tailnet address and so is the board's address it arrived on."""
    with _board(monkeypatch, ("100.64.0.50", 40000), local="100.64.0.10") as (c, db):
        yield c, db, fake_ts


@pytest.fixture(autouse=True)
def _wide_buckets(monkeypatch):
    big = lambda: nodes._Limiter(10 ** 6, 60.0, 64)
    monkeypatch.setattr(nodes, "node_read_limiter", big())
    monkeypatch.setattr(nodes, "node_write_limiter", big())


def bearer(token: str, frm: str | None = None, **extra) -> dict:
    h = {"Authorization": f"Bearer {token}", "X-CCBoard": "1", **extra}
    if frm:
        h["X-Forwarded-For"] = frm
    return h


def mint(db, scopes=("read", "tasks"), device: str | None = ID_A, tags=None):
    binding = {"peer_ts_id": device, "peer_tags": json.dumps(tags or [])} if device else None
    return nodes.add_incoming(dict(CALLER), list(scopes), binding=binding, db=db)


def actions(db) -> list[str]:
    return [r["action"] for r in db.node_audit_list(1000)]


def pair(c, db, frm: str | None, **headers):
    made = nodes.create_code(db=db)
    h = {"X-CCBoard": "1", **headers}
    if frm:
        h["X-Forwarded-For"] = frm
    reply = c.post("/api/nodes/pair", json={"code": made["code"], "node": dict(CALLER)}, headers=h)
    if reply.status_code == 200:
        ack = c.post("/api/node/pair/ack", json={}, headers={**h, "Authorization": "Bearer " + reply.json()["token"]})
        assert ack.status_code == 200, ack.text
    return reply


# ---------------------------------------------------------------- settings

def test_the_settings_parse_and_token_is_the_default():
    assert Settings({}).node_auth == "token" and not Settings({}).node_auth_bad
    assert parse_node_auth(" WHOIS+CAP ") == ("whois+cap", False) and parse_node_auth("whois") == ("whois", False)
    mode, bad = parse_node_auth("whoami")
    assert bad and mode == "whoami", "a typo is never read as token"
    assert parse_node_cap("example.com/cap/ccboard") == ("example.com/cap/ccboard", False)
    for wrong in ("tailscale.com/cap/ccboard", "login.tailscale.com/cap/x", "ccboard", "example.com/ccboard", "https://example.com/cap/x"):
        assert parse_node_cap(wrong) == ("", True), wrong
    assert parse_node_cap("") == ("", False)


@pytest.mark.parametrize("argv,host", [
    (["/srv/app/.venv/bin/uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"], "127.0.0.1"),
    (["/srv/app/.venv/bin/uvicorn", "app.main:app", "--host=0.0.0.0"], "0.0.0.0"),
    (["/srv/app/.venv/bin/uvicorn", "app.main:app", "--port", "8000"], "127.0.0.1"),        # uvicorn's default
    (["/srv/app/.venv/lib/uvicorn/__main__.py", "app.main:app", "--host", "::"], "::"),     # python -m uvicorn
    (["/srv/app/.venv/bin/uvicorn", "app.main:app", "--uds", "/run/x.sock"], None),
    (["/usr/bin/pytest", "-q"], None),
    ([], None),
])
def test_the_bind_host_is_read_from_the_uvicorn_command_line(argv, host):
    assert uvicorn_host(argv) == host


# ---------------------------------------------------------------- off by default

def test_token_mode_runs_nothing_new(serve, monkeypatch):
    """Mutation guard 4 (token mode makes no whois call): a forged forwarded address and capability header from loopback change nothing, no whois runs,
    no parser is called, no new audit row and no device column is written; a pair made in token mode is not bound."""
    c, db, fake = serve
    called = []
    for name in ("caller", "parse_caps", "check_device", "bind_pair", "bind_reverse", "readiness", "bind_state"):
        orig = getattr(nodes_whois, name)
        monkeypatch.setattr(nodes_whois, name, lambda *a, _n=name, _o=orig, **k: (called.append(_n), _o(*a, **k))[1])
    assert not nodes_whois.enabled()
    _, tok = mint(db, device=None)
    r = c.get("/api/node", headers=bearer(tok, DEV_B, **{"Tailscale-App-Capabilities": "{not json", "Tailscale-User-Login": "someone"}))
    assert r.status_code == 200
    r = pair(c, db, DEV_B)
    assert r.status_code == 200, r.text
    assert fake.calls == [] and called == []
    assert not WHOIS_ACTIONS & set(actions(db))
    assert all(row["peer_ts_id"] is None and row["peer_tags"] is None for row in db.node_pairs())
    assert nodes_whois.last_denial() is None


def test_token_mode_is_untouched_by_a_dead_tailscale(serve):
    c, db, fake = serve
    fake.whois_mode = "down"
    _, tok = mint(db, device=None)
    assert c.get("/api/node", headers=bearer(tok, DEV_A)).status_code == 200
    assert fake.whois_calls() == []


def test_token_mode_doctor_and_settings_ask_tailscale_nothing(serve):
    from app import doctor
    c, db, fake = serve
    assert doctor._c_node_auth(db).status == "skip"
    assert doctor._c_board_bind(db).status == "pass"
    v = c.get("/api/nodes/auth", headers={"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}).json()
    assert v["mode"] == "token" and v["ready"] and v["banner"] is None and v["host"]["checked"] is False
    assert fake.calls == []


# ---------------------------------------------------------------- binding at pairing and on every call

def test_a_pair_under_whois_stores_the_device_and_only_that_device_may_call(serve, monkeypatch):
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    r = pair(c, db, DEV_A)
    assert r.status_code == 200, r.text
    row = db.node_pairs()[0]
    assert row["peer_ts_id"] == ID_A and json.loads(row["peer_tags"]) == []
    view = nodes.peers(db=db)[0]
    assert view["device_bound"] is True and view["device_tags"] == []
    assert view["device_id"] == "nAAA...NTRL", "the pair row carries the short form, never the whole id"
    tok = r.json()["token"]
    assert c.get("/api/node", headers=bearer(tok, DEV_A)).status_code == 200
    r = c.get("/api/node", headers=bearer(tok, DEV_B))
    assert r.status_code == 403 and r.json()["reason"] == "device_mismatch"
    au = [x for x in db.node_audit_list(1000) if x["action"] == "device_mismatch"]
    assert au and au[0]["status"] == "refused" and au[0]["peer"] == row["peer_id"] and tok not in json.dumps(au)
    assert nodes_whois.last_denial()["reason"] == "device_mismatch"


def test_a_stable_id_mismatch_is_refused(serve, monkeypatch):
    """Mutation guard 3 (StableID mismatch refused): the address answers with another device's StableID."""
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    _, tok = mint(db, device=ID_A)
    fake.devices[DEV_A] = (ID_B, "imposter", [])
    r = c.get("/api/node", headers=bearer(tok, DEV_A))
    assert r.status_code == 403 and r.json()["reason"] == "device_mismatch"
    assert "device_mismatch" in actions(db)


def test_an_address_tailscale_does_not_know_is_a_mismatch(serve, monkeypatch):
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    _, tok = mint(db)
    r = c.get("/api/node", headers=bearer(tok, "100.64.0.99"))
    assert r.status_code == 403 and r.json()["reason"] == "device_mismatch"


@pytest.mark.parametrize("xff,reason", [
    (None, "not_via_serve"),                          # no X-Forwarded-For at all: a local process that reached the board directly
    ("127.0.0.1", "no_caller_address"),               # every hop trusted: uvicorn leaves a loopback peer, the hop is loopback
    ("192.168.1.5", "no_caller_address"),
    ("not an address", "no_caller_address"),
    ("100.64.0.11, 10.0.0.1", "no_caller_address"),   # uvicorn takes the rightmost untrusted hop, which is not a tailnet address
])
def test_a_missing_or_foreign_forwarded_address_is_refused_without_a_lookup(serve, monkeypatch, xff, reason):
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    _, tok = mint(db)
    r = c.get("/api/node", headers=bearer(tok, xff))
    assert r.status_code == 403 and r.json()["reason"] == reason
    assert fake.whois_calls() == []


def test_the_last_forwarded_hop_is_the_caller(serve, monkeypatch):
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    _, tok = mint(db)
    assert c.get("/api/node", headers=bearer(tok, f"{DEV_B}, {DEV_A}")).status_code == 200
    assert fake.whois_calls() == [["whois", "--json", DEV_A]]


def test_a_pair_made_before_the_check_was_on_is_not_bound(serve, monkeypatch):
    c, db, fake = serve
    _, tok = mint(db, device=None)
    set_mode(monkeypatch, "whois")
    r = c.get("/api/node", headers=bearer(tok, DEV_A))
    assert r.status_code == 403 and r.json()["reason"] == "not_bound" and "pair" in r.json()["error"]
    assert "not_bound" in actions(db) and fake.whois_calls() == []


def test_a_tag_outside_the_node_tags_is_refused_in_tag_mode(serve, monkeypatch):
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    r = pair(c, db, DEV_X)
    assert r.status_code == 403 and r.json()["reason"] == "device_refused" and "CCBOARD_NODE_TAGS" in r.json()["error"]
    assert not db.node_pairs() and nodes.code_status(db=db)["active"], "the code is not spent"
    nodes.cancel_code(db=db)
    r = pair(c, db, DEV_T)
    assert r.status_code == 200
    assert json.loads(db.node_pairs()[0]["peer_tags"]) == ["tag:ccboard"]
    tok = r.json()["token"]
    assert c.get("/api/node", headers=bearer(tok, DEV_T)).status_code == 200
    fake.devices[DEV_T] = (ID_T, "node-t", ["tag:other"])                 # the policy took the tag away
    ts.whois_forget()
    r = c.get("/api/node", headers=bearer(tok, DEV_T))
    assert r.status_code == 403 and r.json()["reason"] == "tag_refused"


def test_on_a_tagged_board_an_untagged_caller_is_refused(serve, monkeypatch):
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    monkeypatch.setattr(nodes, "_ts", lambda: {"Self": {"Tags": ["tag:ccboard"]}})
    r = pair(c, db, DEV_A)
    assert r.status_code == 403 and r.json()["reason"] == "device_refused"


THEIR_ID = "n_" + "b" * 16


def _both_ways(c, db, monkeypatch, confirm_from: str | None):
    """This board pairs with another one both ways (add_node). The other board's pair route is faked; while it answers, it calls this board's real
    confirm route through the `serve` client from `confirm_from` (None: it never calls back). Returns the reverse token the other board was given."""
    monkeypatch.setattr(settings, "public_url", "https://100.64.0.9")
    got = {}

    def fake_peer_call(url, method, path, *, headers=None, body=None, timeout=None):
        if (method, path) != ("POST", "/api/nodes/pair"):
            raise nodes.PeerError("unreachable", "not faked")
        got["reverse"] = body["reverse"]["token"]
        if confirm_from:
            proof = nodes.confirm_proof(nodes.normalize_code(body["code"]), body["confirm"], body["node"]["id"], body["node"]["url"], url)
            r = c.post("/api/nodes/pair/confirm", json={"proof": proof}, headers={"X-CCBoard": "1", "X-Forwarded-For": confirm_from})
            got["confirm"] = r.status_code
        answer = {"token": nodes._new_token(), "node": {"id": THEIR_ID, "name": "node-b"}, "scopes": ["read", "tasks"], "reverse": True}
        return nodes.PeerReply(200, json.dumps(answer).encode(), {})
    monkeypatch.setattr(nodes, "peer_call", fake_peer_call)
    nodes.add_node("https://" + DEV_B, "ABCDE-12345", both_ways=True, db=db)
    return got


def _reverse_row(db) -> dict:
    (row,) = [r for r in db.node_pairs() if r["peer_node_id"] == THEIR_ID]
    return row


def test_both_ways_binds_the_reverse_pair_to_the_board_that_confirmed(serve, monkeypatch):
    """The other board calls this one's confirm route during the pairing; in a whois mode its device is bound to the reverse pair, so its calls with
    the reverse token pass from its own device and are refused from any other."""
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    got = _both_ways(c, db, monkeypatch, confirm_from=DEV_B)
    assert got["confirm"] == 200
    row = _reverse_row(db)
    assert row["peer_ts_id"] == ID_B and json.loads(row["peer_tags"]) == [] and not row["callback_unverified"]
    assert c.get("/api/node", headers=bearer(got["reverse"], DEV_B)).status_code == 200
    r = c.get("/api/node", headers=bearer(got["reverse"], DEV_A))
    assert r.status_code == 403 and r.json()["reason"] == "device_mismatch"
    assert nodes._pending_bind == {} and nodes._pending == {}


def test_both_ways_without_a_confirm_leaves_the_reverse_pair_unbound(serve, monkeypatch):
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    got = _both_ways(c, db, monkeypatch, confirm_from=None)
    assert _reverse_row(db)["peer_ts_id"] is None
    r = c.get("/api/node", headers=bearer(got["reverse"], DEV_B))
    assert r.status_code == 403 and r.json()["reason"] == "not_bound"


def test_both_ways_with_a_confirm_tailscale_cannot_place_stays_unbound(serve, monkeypatch):
    """A confirm whose device whois cannot answer still confirms (the pairing is not broken by it) but binds nothing: the reverse pair fails closed."""
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    fake.whois_mode = "down"
    got = _both_ways(c, db, monkeypatch, confirm_from=DEV_B)
    assert got["confirm"] == 200 and _reverse_row(db)["peer_ts_id"] is None
    fake.whois_mode = "ok"
    assert c.get("/api/node", headers=bearer(got["reverse"], DEV_B)).json()["reason"] == "not_bound"


def test_both_ways_in_token_mode_binds_nothing_and_asks_tailscale_nothing(serve, monkeypatch):
    c, db, fake = serve
    got = _both_ways(c, db, monkeypatch, confirm_from=DEV_B)
    assert got["confirm"] == 200 and _reverse_row(db)["peer_ts_id"] is None and fake.calls == []


@pytest.mark.parametrize("status", [None, {}, {"Self": None}, "garbage"])
def test_an_unreadable_own_status_refuses_an_untagged_caller(serve, monkeypatch, status):
    """Unknown is never read as "this board is not tagged": an untagged caller is refused with a plain reason and the code stays; a caller with an
    allowed tag needs no reading of this board and still pairs."""
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    monkeypatch.setattr(nodes, "_ts", lambda: status)
    r = pair(c, db, DEV_A)
    assert r.status_code == 503 and r.json()["reason"] == "tailscale_unavailable" and "own Tailscale status" in r.json()["error"]
    assert not db.node_pairs() and nodes.code_status(db=db)["active"]
    nodes.cancel_code(db=db)
    r = pair(c, db, DEV_T)
    assert r.status_code == 200 and db.node_pairs()[0]["peer_ts_id"] == ID_T


# ---------------------------------------------------------------- fail closed

@pytest.mark.parametrize("how", ["down", "timeout", "raise", "junk"])
def test_whois_that_cannot_answer_is_503_and_audited(serve, monkeypatch, how):
    """Mutation guard 2 (fail closed when whois cannot answer)."""
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    _, tok = mint(db)
    fake.whois_mode = how
    r = c.get("/api/node", headers=bearer(tok, DEV_A))
    assert r.status_code == 503 and r.json()["reason"] == "tailscale_unavailable"
    assert "tailscale_unavailable" in actions(db)
    fake.whois_mode = "ok"                                     # an unavailable answer is not cached: the next call asks again
    assert c.get("/api/node", headers=bearer(tok, DEV_A)).status_code == 200


def test_a_pair_is_refused_and_the_code_kept_when_whois_cannot_answer(serve, monkeypatch):
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    fake.whois_mode = "down"
    r = pair(c, db, DEV_A)
    assert r.status_code == 503 and r.json()["reason"] == "tailscale_unavailable" and "Tailscale" in r.json()["error"]
    assert not db.node_pairs(include_revoked=True) and nodes.code_status(db=db)["active"]


def test_a_mode_with_a_typo_refuses_everything(serve, monkeypatch):
    c, db, fake = serve
    _, tok = mint(db)
    monkeypatch.setattr(settings, "node_auth", "whoami")
    monkeypatch.setattr(settings, "node_auth_bad", True)
    r = c.get("/api/node", headers=bearer(tok, DEV_A))
    assert r.status_code == 503 and r.json()["reason"] == "bad_mode"
    assert fake.whois_calls() == []


# ---------------------------------------------------------------- header trust

def test_a_non_loopback_peer_is_refused_before_any_forwarded_header_is_read(direct, monkeypatch):
    """Mutation guard 1 (headers read only from a loopback peer): a caller that reached the board directly, with a forged X-Forwarded-For pointing at the
    paired device, a forged capability header and a login header, is refused; no reader of those headers ran and no whois was started."""
    from app import main
    c, db, fake = direct
    set_mode(monkeypatch, "whois+cap", CAP)
    _, tok = mint(db, nodes.SCOPES)
    touched = []
    for mod, name in ((nodes_whois, "parse_caps"), (nodes_whois, "cap_check"), (nodes_whois, "check_device"),
                      (nodes_whois, "bind_pair"), (nodes, "caller_addr"), (main, "identify")):
        monkeypatch.setattr(mod, name, lambda *a, _n=name, **k: touched.append(_n) or (_ for _ in ()).throw(AssertionError(_n)))
    forged = {"Tailscale-App-Capabilities": json.dumps({CAP: [{"ops": list(nodes_whois.OPS.values())}]}), "Tailscale-User-Login": "alice@example.com"}
    r = c.get("/api/node", headers=bearer(tok, DEV_A, **forged))
    assert r.status_code == 403 and r.json()["reason"] == "not_via_serve"
    r = pair(c, db, DEV_A, **forged)
    assert r.status_code == 403 and r.json()["reason"] == "not_via_serve"
    assert touched == [] and fake.calls == []
    assert actions(db).count("not_via_serve") == 2


class _NoTouch:
    """Headers that fail the test if anything reads them."""

    def __getattr__(self, name):
        raise AssertionError(f"headers.{name} was read")

    def get(self, *a, **k):
        raise AssertionError("headers.get was read")


@pytest.mark.parametrize("local", ["100.64.0.50", "192.168.1.5", "testserver", None, ""])
@pytest.mark.parametrize("peer", ["127.0.0.1", "100.64.0.11"])
def test_no_code_path_reads_headers_from_a_connection_not_on_loopback(fake_ts, monkeypatch, local, peer, tmp_path):
    """A connection that did not arrive on a loopback address of the board did not come through Serve: refused before any header is read, whatever
    the peer says."""
    from app.db import DB
    db = DB(tmp_path / "t.db")
    set_mode(monkeypatch, "whois+cap", CAP)
    row, _tok = mint(db)
    assert nodes_whois.pre_check(peer, local, _NoTouch(), db=db).reason == "not_via_serve"
    assert nodes_whois.check_call(peer, local, _NoTouch(), row, "read", db=db).reason == "not_via_serve"
    with pytest.raises(nodes.PairError):
        nodes_whois.bind_pair(peer, local, _NoTouch())
    assert fake_ts.calls == []


@pytest.mark.parametrize("host", ["100.64.0.50", "192.168.1.5", "testclient", None, ""])
def test_the_rate_limit_address_ignores_the_header_from_a_non_loopback_peer(host):
    assert nodes.caller_addr(host, "100.64.0.11") != "100.64.0.11"


def test_uvicorn_rewrites_the_peer_to_the_serve_hop(monkeypatch):
    """What the board receives from uvicorn's default stack: the loopback peer replaced by the forwarded hop; from any other peer, the real peer."""
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    seen = []

    async def app(scope, receive, send):
        seen.append((scope["client"], scope["server"]))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    from fastapi.testclient import TestClient
    c = TestClient(uvicorn_default_stack(app), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 40000))       # no lifespan: http only
    c.get("/", headers={"X-Forwarded-For": DEV_A})
    c.get("/")
    c = TestClient(uvicorn_default_stack(app), base_url="http://127.0.0.1:8000", client=("100.64.0.50", 40000))
    c.get("/", headers={"X-Forwarded-For": DEV_A})
    assert [s[0][0] for s in seen] == [DEV_A, "127.0.0.1", "100.64.0.50"]
    assert all(s[1][0] == "127.0.0.1" for s in seen), "the local address is never rewritten"


def test_through_uvicorn_proxy_headers_a_pair_and_a_call_work_end_to_end(serve, monkeypatch):
    """The regression of the first version (it required a loopback request.client, which uvicorn's default proxy headers never leave behind Serve, so
    every call failed closed): through the real ProxyHeadersMiddleware, a pair binds DEV_A, a call from DEV_A passes and one from DEV_B is refused."""
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    r = pair(c, db, DEV_A)
    assert r.status_code == 200, r.text
    assert db.node_pairs()[0]["peer_ts_id"] == ID_A
    tok = r.json()["token"]
    assert c.get("/api/node", headers=bearer(tok, DEV_A)).status_code == 200
    r = c.get("/api/node", headers=bearer(tok, DEV_B))
    assert r.status_code == 403 and r.json()["reason"] == "device_mismatch"
    assert [a[2] for a in fake.whois_calls()] == [DEV_A, DEV_B]


def test_without_proxy_headers_the_last_forwarded_hop_is_the_caller(projects_dir, monkeypatch, fake_ts):
    """uvicorn --no-proxy-headers: the peer stays loopback and the hop Serve added is read from X-Forwarded-For."""
    with _board(monkeypatch, ("127.0.0.1", 40000), proxy=False) as (c, db):
        set_mode(monkeypatch, "whois")
        r = pair(c, db, DEV_A)
        assert r.status_code == 200, r.text
        tok = r.json()["token"]
        assert c.get("/api/node", headers=bearer(tok, DEV_A)).status_code == 200
        assert c.get("/api/node", headers=bearer(tok, DEV_B)).json()["reason"] == "device_mismatch"
        assert c.get("/api/node", headers=bearer(tok, None)).json()["reason"] == "not_via_serve"


def test_a_forged_forwarded_address_from_a_non_loopback_peer_is_not_trusted(projects_dir, monkeypatch, fake_ts):
    """Even on a loopback local address, uvicorn believes X-Forwarded-For only from a loopback peer: a peer at 100.64.0.50 naming the paired device
    is looked up as itself and refused; the forged address is never asked about."""
    with _board(monkeypatch, ("100.64.0.50", 40000)) as (c, db):
        set_mode(monkeypatch, "whois")
        _, tok = mint(db)
        r = c.get("/api/node", headers=bearer(tok, DEV_A))
        assert r.status_code == 403 and r.json()["reason"] == "device_mismatch"
        assert fake_ts.whois_calls() == [["whois", "--json", "100.64.0.50"]]


@pytest.mark.parametrize("env,argv,ok", [
    (None, None, True),
    ("127.0.0.1,::1", None, True),
    ("127.0.0.0/8", "::1", True),
    ("*", None, False),
    (None, "100.64.0.0/10", False),
    ("127.0.0.1, 10.0.0.1", None, False),
    (None, "localhost", False),
])
def test_a_widened_proxy_trust_keeps_the_whois_modes_off(serve, monkeypatch, env, argv, ok):
    from app import doctor
    c, db, fake = serve
    if env is not None:
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", env)
    monkeypatch.setattr(settings, "forwarded_allow_argv", argv, raising=False)
    set_mode(monkeypatch, "whois")
    _, tok = mint(db)
    r = c.get("/api/node", headers=bearer(tok, DEV_A))
    if ok:
        assert r.status_code == 200 and nodes_whois.readiness()["ready"]
        return
    assert r.status_code == 503 and r.json()["reason"] == "proxy_trust"
    out = doctor._c_node_auth(db)
    assert out.status == "fail" and "FORWARDED_ALLOW_IPS" in out.fix["text"]
    v = c.get("/api/nodes/auth", headers={"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}).json()
    assert v["reason"] == "proxy_trust" and "forwarded" in v["banner"]
    assert fake.whois_calls() == []


def test_the_forwarded_allow_ips_argument_is_read_from_the_uvicorn_command_line():
    from app.config import uvicorn_forwarded_allow
    assert uvicorn_forwarded_allow(["/srv/app/.venv/bin/uvicorn", "app.main:app", "--forwarded-allow-ips", "*"]) == "*"
    assert uvicorn_forwarded_allow(["/srv/app/.venv/bin/uvicorn", "app.main:app", "--forwarded-allow-ips=10.0.0.1"]) == "10.0.0.1"
    assert uvicorn_forwarded_allow(["/srv/app/.venv/bin/uvicorn", "app.main:app", "--host", "127.0.0.1"]) is None
    assert uvicorn_forwarded_allow(["/usr/bin/pytest", "--forwarded-allow-ips", "*"]) is None


def test_token_mode_still_ignores_a_forwarded_address_from_a_non_loopback_peer(direct):
    c, db, fake = direct
    _, tok = mint(db, device=None)
    assert c.get("/api/node", headers=bearer(tok, DEV_A)).status_code == 200
    assert fake.calls == [] and nodes.caller_addr("100.64.0.50", DEV_A) == "100.64.0.50"


# ---------------------------------------------------------------- capabilities

ALL_OPS = list(nodes_whois.OPS.values())


@pytest.fixture
def caps(fake_ts, monkeypatch):
    set_mode(monkeypatch, "whois+cap", CAP)


def rfc2047(text: str, enc: str = "q") -> str:
    """`text` as RFC 2047 encoded words: base64 in one word, or quoted-printable split over several words (as a long header is)."""
    if enc == "b":
        return "=?utf-8?b?" + base64.b64encode(text.encode()).decode() + "?="
    cs = email.charset.Charset("utf-8")
    cs.header_encoding = email.charset.QP
    return email.header.Header(text, cs, maxlinelen=60).encode().replace("\n", "")


@pytest.mark.parametrize("raw,scope,ok", [
    (json.dumps({CAP: [{"ops": ["node.read"]}]}), "read", True),
    (json.dumps({CAP: [{"ops": ["node.read"]}]}), "tasks", False),                                     # a missing op
    (json.dumps({CAP: [{"ops": ["node.read"]}, {"ops": ["node.tasks"]}]}), "tasks", True),             # two grants merged
    (json.dumps({CAP: [{"ops": ["node.read"]}, {"ops": ["node.tasks"]}]}), "sessions", False),
    (json.dumps({"other.example/cap/x": [{"ops": ALL_OPS}]}), "read", False),                          # another capability only
    (json.dumps({CAP: [{"ops": ALL_OPS}], "other.example/cap/x": [{}]}), "permissions", True),
    ("{not json", "read", False),
    ("", "read", False),
    ("   ", "read", False),
    (json.dumps([CAP]), "read", False),
    (json.dumps({CAP: {"ops": ALL_OPS}}), "read", False),
    (json.dumps({CAP: [{"ops": "node.read"}]}), "read", False),
    (json.dumps({CAP: ["node.read"]}), "read", False),
])
def test_capability_parsing_plain(caps, raw, scope, ok):
    assert (nodes_whois.cap_check([raw], scope) is None) is ok


@pytest.mark.parametrize("enc", ["q", "b"])
def test_capability_parsing_rfc2047(caps, enc):
    raw = rfc2047(json.dumps({CAP: [{"ops": ["node.read", "node.sessions"], "note": "zażółć"}]}), enc)
    assert raw.startswith("=?utf-8?")
    got = nodes_whois.parse_caps(raw, CAP)
    assert got == {"ok": True, "ops": ["node.read", "node.sessions"]}
    assert nodes_whois.cap_check([raw], "sessions") is None and nodes_whois.cap_check([raw], "tasks").reason == "cap_op_missing"


def test_capability_reasons_and_the_size_cap(caps, monkeypatch):
    assert nodes_whois.parse_caps(None, CAP)["reason"] == "cap_missing"
    assert nodes_whois.parse_caps("", CAP)["reason"] == "cap_missing"
    assert nodes_whois.parse_caps("{x", CAP)["reason"] == "cap_invalid"
    assert nodes_whois.parse_caps("=?bogus-charset?q?abc?=", CAP)["reason"] == "cap_invalid"
    assert nodes_whois.parse_caps(json.dumps({"x": 1}), CAP)["reason"] == "cap_missing"
    seen = []
    monkeypatch.setattr(nodes_whois.json, "loads", lambda *a, **k: seen.append(1))
    huge = json.dumps({CAP: [{"ops": ["node.read"], "pad": "x" * (nodes_whois.CAP_HEADER_MAX + 1)}]})
    assert nodes_whois.parse_caps(huge, CAP) == {"ok": False, "reason": "cap_invalid", "detail": "the header is too large"}
    assert seen == [], "too large is refused before it is parsed"
    assert nodes_whois.cap_check(["{}", "{}"], "read").reason == "cap_invalid"
    assert nodes_whois.cap_check([], "read").reason == "cap_missing"


def test_whois_cap_end_to_end(serve, monkeypatch):
    c, db, fake = serve
    set_mode(monkeypatch, "whois+cap", CAP)
    _, tok = mint(db, ("read", "tasks"))
    grant = lambda ops: {"Tailscale-App-Capabilities": json.dumps({CAP: [{"ops": ops}]})}
    assert c.get("/api/node", headers=bearer(tok, DEV_A, **grant(["node.read"]))).status_code == 200
    r = c.get("/api/node", headers=bearer(tok, DEV_A, **grant(["node.tasks"])))
    assert r.status_code == 403 and r.json()["reason"] == "cap_op_missing"
    r = c.get("/api/node", headers=bearer(tok, DEV_A))
    assert r.status_code == 403 and r.json()["reason"] == "cap_missing"
    r = c.get("/api/node", headers=bearer(tok, DEV_A, **{"Tailscale-App-Capabilities": "{oops"}))
    assert r.status_code == 403 and r.json()["reason"] == "cap_invalid"
    r = c.get("/api/node", headers=bearer(tok, DEV_B, **grant(["node.read"])))
    assert r.status_code == 403 and r.json()["reason"] == "device_mismatch", "the device check comes first"
    assert {"cap_op_missing", "cap_missing", "cap_invalid", "device_mismatch"} <= set(actions(db))
    r = c.get("/api/node/permissions", headers=bearer(tok, DEV_A, **grant(ALL_OPS)))
    assert r.status_code == 403 and "permissions scope" in r.json()["error"], "the capability never widens the token's scopes"
    r = c.post("/api/node/rotate", headers=bearer(tok, DEV_A, **grant([])))
    assert r.status_code == 200, "rotate needs a readable grant but no op: a pair may always manage itself"
    r = c.post("/api/node/rotate", headers=bearer(r.json()["token"], DEV_A))
    assert r.status_code == 403 and r.json()["reason"] == "cap_missing"


def test_whois_cap_without_a_capability_name_refuses_to_switch_on(serve, monkeypatch):
    c, db, fake = serve
    set_mode(monkeypatch, "whois+cap", "")
    _, tok = mint(db)
    r = c.get("/api/node", headers=bearer(tok, DEV_A))
    assert r.status_code == 503 and r.json()["reason"] == "no_cap_name"
    assert fake.whois_calls() == []


# ---------------------------------------------------------------- cache and rate cap

def test_whois_is_cached_for_60_seconds(serve, monkeypatch):
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    _, tok = mint(db)
    assert c.get("/api/node", headers=bearer(tok, DEV_A)).status_code == 200
    fake.clock[0] += 59
    assert c.get("/api/node", headers=bearer(tok, DEV_A)).status_code == 200
    assert len(fake.whois_calls()) == 1
    fake.clock[0] += 2
    assert c.get("/api/node", headers=bearer(tok, DEV_A)).status_code == 200
    assert len(fake.whois_calls()) == 2


def test_more_than_20_uncached_lookups_a_minute_is_429(serve, monkeypatch):
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    _, tok = mint(db)
    for i in range(20):
        assert ts.whois(f"100.64.1.{i}")["reason"] == "not_found"
    assert len(fake.whois_calls()) == 20
    r = c.get("/api/node", headers=bearer(tok, DEV_A))
    assert r.status_code == 429 and r.json()["reason"] == "whois_rate_limited" and int(r.headers["retry-after"]) >= 1
    assert len(fake.whois_calls()) == 20, "no process above the cap"
    assert "whois_rate_limited" in actions(db)
    assert ts.whois("100.64.1.3")["reason"] == "not_found", "a cached answer is not counted"
    fake.clock[0] += 61
    assert c.get("/api/node", headers=bearer(tok, DEV_A)).status_code == 200


def _count_waiters(monkeypatch) -> list:
    """Count the callers that wait on an in-flight lookup (the Event whois() makes for it), so a test knows they are waiting before it lets go."""
    count, lock = [0], threading.Lock()

    class Counting(threading.Event):
        def wait(self, timeout=None):
            if timeout == ts.WHOIS_TIMEOUT + 2:              # whois()'s wait only (Thread.start waits on an Event too, with no timeout)
                with lock:
                    count[0] += 1
            return super().wait(timeout)
    monkeypatch.setattr(ts.threading, "Event", Counting)
    return count


def _until(cond, tries: int = 500) -> bool:
    import time
    for _ in range(tries):
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_concurrent_lookups_of_one_address_share_one_process_and_one_rate_slot(fake_ts, monkeypatch):
    """A cold cache and five calls at once from one address: one `tailscale whois`, one slot of the 20, the same answer for all five."""
    inside, release = threading.Event(), threading.Event()
    real = ts.call

    def slow(args, **k):
        inside.set()
        release.wait(5)
        return real(args, **k)
    monkeypatch.setattr(ts, "call", slow)
    waiting = _count_waiters(monkeypatch)
    answers = []
    threads = [threading.Thread(target=lambda: answers.append(ts.whois(DEV_A)), daemon=True) for _ in range(5)]
    threads[0].start()
    assert inside.wait(5)
    for t in threads[1:]:
        t.start()
    assert _until(lambda: waiting[0] >= 4), "every follower waits on the leader's lookup"
    release.set()
    for t in threads:
        t.join(5)
    assert not any(t.is_alive() for t in threads)
    assert len(fake_ts.whois_calls()) == 1 and len(ts._whois_spawns) == 1
    assert len(answers) == 5 and all(a == answers[0] and a["stable_id"] == ID_A for a in answers)
    assert ts._whois_inflight == {}


def test_a_failed_shared_lookup_is_shared_and_not_cached(fake_ts, monkeypatch):
    inside, release = threading.Event(), threading.Event()
    fake_ts.whois_mode = "down"
    real = ts.call

    def slow(args, **k):
        inside.set()
        release.wait(5)
        return real(args, **k)
    monkeypatch.setattr(ts, "call", slow)
    waiting = _count_waiters(monkeypatch)
    answers = []
    first = threading.Thread(target=lambda: answers.append(ts.whois(DEV_A)), daemon=True)
    first.start()
    assert inside.wait(5)
    second = threading.Thread(target=lambda: answers.append(ts.whois(DEV_A)), daemon=True)
    second.start()
    assert _until(lambda: waiting[0] >= 1)
    release.set()
    first.join(5)
    second.join(5)
    assert [a["reason"] for a in answers] == ["unavailable", "unavailable"] and len(fake_ts.whois_calls()) == 1
    fake_ts.whois_mode = "ok"
    assert ts.whois(DEV_A)["ok"] is True, "an unavailable answer is not cached"


def test_whois_never_raises_and_never_runs_for_a_foreign_address(fake_ts):
    for addr in ("127.0.0.1", "10.0.0.1", "8.8.8.8", "fe80::1", "garbage", None, "100.64.0.11%eth0"):
        assert ts.whois(addr) == {"ok": False, "reason": "not_tailnet"}
    assert fake_ts.calls == []
    assert ts.whois(DEV_A)["stable_id"] == ID_A
    assert ts.whois("fd7a:115c:a1e0::5")["reason"] == "not_found"
    fake_ts.whois_mode = "raise"
    assert ts.whois(DEV_B)["reason"] == "unavailable"
    assert ts.parse_whois("{}") == {"ok": False, "reason": "bad_answer"}
    assert ts.parse_whois(json.dumps({"Node": {"StableID": "  "}}))["ok"] is False
    for args in fake_ts.calls:
        assert args[0] in ("whois", "version") and "reset" not in args and "up" not in args and "set" not in args


# ---------------------------------------------------------------- board-bind

def test_a_board_bound_to_all_addresses_keeps_the_whois_modes_off(serve, monkeypatch):
    from app import doctor
    c, db, fake = serve
    _, tok = mint(db)
    monkeypatch.setattr(settings, "bind_host", "0.0.0.0")
    for mode in ("whois", "whois+cap"):
        set_mode(monkeypatch, mode, CAP)
        r = nodes_whois.readiness()
        assert r["ready"] is False and r["reason"] == "board_bind"
        resp = c.get("/api/node", headers=bearer(tok, DEV_A))
        assert resp.status_code == 503 and resp.json()["reason"] == "board_bind"
        assert pair(c, db, DEV_A).json()["reason"] == "board_bind"
        nodes.cancel_code(db=db)
        out = doctor._c_board_bind(db)
        assert out.status == "fail" and "0.0.0.0" in out.detail and "--host 127.0.0.1" in out.fix["text"]
        assert doctor._c_node_auth(db).status == "fail"
        v = c.get("/api/nodes/auth", headers={"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}).json()
        assert v["ready"] is False and v["reason"] == "board_bind" and v["bind"]["ok"] is False and v["banner"]
    assert fake.whois_calls() == [] and "board_bind" in actions(db)
    set_mode(monkeypatch, "token")
    assert doctor._c_board_bind(db).status == "warn", "token mode only warns"
    assert c.get("/api/node", headers=bearer(tok, DEV_A)).status_code == 200


def test_the_socket_table_overrules_the_settings(fake_ts, monkeypatch, tmp_path):
    monkeypatch.setattr(nodes_whois, "listeners", lambda port: ["127.0.0.1", "0.0.0.0"])
    assert nodes_whois.bind_state(fresh=True)["ok"] is False
    monkeypatch.setattr(nodes_whois, "listeners", lambda port: ["127.0.0.1", "::1"])
    assert nodes_whois.bind_state(fresh=True)["ok"] is True
    monkeypatch.setattr(settings, "bind_host", None)
    monkeypatch.setattr(nodes_whois, "listeners", lambda port: None)
    st = nodes_whois.bind_state(fresh=True)
    assert st["ok"] is None and st["hosts"] == []
    set_mode(monkeypatch, "whois")
    assert nodes_whois.readiness()["ready"] is True, "an unknown bind is shown as unknown, not refused"
    assert nodes_whois.doctor_bind()[0] == "warn"


def test_proc_listeners_reads_the_listen_rows(tmp_path):
    hdr = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    (tmp_path / "tcp").write_text(hdr + "   0: 0100007F:1F40 00000000:0000 0A 0 0 0 0 0 1000 0 1\n"
                                        "   1: 00000000:1F40 0B00400A:C350 01 0 0 0 0 0 1000 0 2\n"       # an established row on the port: not a listener
                                        "   2: 00000000:0016 00000000:0000 0A 0 0 0 0 0 0 0 3\n")
    (tmp_path / "tcp6").write_text(hdr + "   0: 00000000000000000000000000000000:1F40 00000000000000000000000000000000:0000 0A 0 0 0 0 0 1000 0 4\n")
    assert nodes_whois.proc_listeners(8000, tmp_path) == ["127.0.0.1", "::"]
    assert nodes_whois.proc_listeners(22, tmp_path) == ["0.0.0.0"]
    assert nodes_whois.proc_listeners(8000, tmp_path / "absent") is None


# ---------------------------------------------------------------- doctor and Settings

def test_doctor_node_auth_in_each_mode(serve, monkeypatch):
    from app import doctor
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    monkeypatch.setattr(nodes, "_ts", lambda: {"Self": {"TailscaleIPs": [DEV_A]}})
    out = doctor._c_node_auth(db)
    assert out.status == "pass" and "CCBOARD_NODE_AUTH=whois" in out.detail and "1.104.1" in out.detail and "no refusal" in out.detail
    set_mode(monkeypatch, "whois+cap", CAP)
    out = doctor._c_node_auth(db)
    assert out.status == "pass" and "Serve passes the capability" in out.detail
    fake.serve = {"Web": {"node-a.example.ts.invalid:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8000"}}}}}
    out = doctor._c_node_auth(db)
    assert out.status == "fail" and "--accept-app-caps=" + CAP in out.fix["text"]
    fake.serve = None
    assert doctor._c_node_auth(db).status == "warn"
    fake.version = "1.90.2"
    ts.whois_forget()
    out = doctor._c_node_auth(db)
    assert out.status == "fail" and "1.92" in out.detail
    fake.version = "1.104.1"
    ts.whois_forget()
    fake.whois_mode = "down"
    assert doctor._c_node_auth(db).status == "fail"
    fake.whois_mode = "ok"
    _, tok = mint(db)
    c.get("/api/node", headers=bearer(tok, DEV_B))
    set_mode(monkeypatch, "whois")
    nodes_whois._last_denial = {"at": "2026-10-16T00:00:00Z", "reason": "device_mismatch", "node": "caller"}
    assert "last refusal: device_mismatch" in doctor._c_node_auth(db).detail
    set_mode(monkeypatch, "whois+cap", "")
    out = doctor._c_node_auth(db)
    assert out.status == "fail" and "CCBOARD_NODE_CAP" in out.fix["text"]
    for args in fake.calls:
        assert args[:1] in (["whois"], ["version"]) or args[:2] == ["serve", "status"], "read only"


def test_the_settings_view_has_the_shape_the_page_needs(serve, monkeypatch):
    c, db, fake = serve
    set_mode(monkeypatch, "whois+cap", CAP)
    monkeypatch.setattr(nodes, "_ts", lambda: {"Self": {"TailscaleIPs": [DEV_A]}})
    v = c.get("/api/nodes/auth", headers={"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}).json()
    assert set(v) == {"mode", "modes", "ready", "reason", "message", "bind", "cap", "tags", "host", "banner", "last_denial", "snippet"}
    assert v["mode"] == "whois+cap" and v["ready"] and v["banner"] is None
    assert v["host"] == {"checked": True, "version": "1.104.1", "version_ok": True, "serve_cap": True, "whois": True}
    assert set(v["snippet"]) == {"user", "tag", "serve", "cap", "placeholder", "version_min", "version_cmd"}
    fake.serve = {"Web": {"node-a.example.ts.invalid:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8000"}}}}}
    v = c.get("/api/nodes/auth", headers={"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}).json()
    assert v["banner"] and "accept-app-caps" in v["banner"]
    _, tok = mint(db, nodes.SCOPES)
    r = c.get("/api/nodes/auth", headers=bearer(tok, DEV_A, **{"Tailscale-App-Capabilities": json.dumps({CAP: [{"ops": ALL_OPS}]})}))
    assert r.status_code == 403, "a node token cannot read it"


def test_the_snippet_names_no_real_domain_or_host(fake_ts, monkeypatch):
    texts = [nodes_whois.grant_snippet("user"), nodes_whois.grant_snippet("tag"), nodes_whois.serve_command()]
    blob = "\n".join(texts)
    domains = set(re.findall(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", blob.lower())) - set(ALL_OPS)
    assert domains == {"example.com"}, domains
    assert "@" not in blob and "ts.net" not in blob and "tailscale.com" not in blob
    user, tag = (json.loads(t)["grants"][0] for t in texts[:2])
    assert user["src"] == ["<owner login>"] and user["dst"] == ["autogroup:self"]
    assert tag["src"] == tag["dst"] == ["tag:ccboard"]
    for g in (user, tag):
        assert g["ip"] == ["tcp:443"] and g["app"] == {nodes_whois.PLACEHOLDER_CAP: [{"ops": ALL_OPS}]}, "the network line sits next to the capability"
    assert texts[2] == "tailscale serve --bg --https=443 --accept-app-caps=example.com/cap/ccboard http://127.0.0.1:8000"


def test_versions_and_the_serve_flag_read():
    assert ts.caps_supported("1.92.0") is True and ts.caps_supported("1.104.1") is True and ts.caps_supported("1.91.9") is False
    assert ts.caps_supported(None) is None and ts.parse_version("garbage") is None
    d = {"Web": {"h.example.ts.invalid:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8000", "AcceptAppCaps": [CAP]}}},
                 "h.example.ts.invalid:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}}}}
    assert ts.serve_accepts_caps(d, 443, CAP) is True
    assert ts.serve_accepts_caps(d, 8443, CAP) is False
    assert ts.serve_accepts_caps(d, 9999, CAP) is None and ts.serve_accepts_caps(None, 443, CAP) is None


def test_no_thread_is_left_behind(serve, monkeypatch):
    before = {t.ident for t in threading.enumerate()}
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    _, tok = mint(db)
    for _ in range(3):
        c.get("/api/node", headers=bearer(tok, DEV_A))
    extra = [t for t in threading.enumerate() if t.ident not in before and t.is_alive() and not t.daemon and "AnyIO" not in t.name]
    assert extra == []


# ---------------------------------------------------------------- the demo fixture of the Device check (app/static/demo/nodes-auth.json, ?demo=1&authstate=<name>)

def _demo(name):
    from pathlib import Path
    return json.loads((Path(__file__).resolve().parent.parent / "app" / "static" / "demo" / name).read_text(encoding="utf-8"))


def test_the_demo_fixture_is_held_to_the_board(monkeypatch):
    """The demo's states have the keys of the real answer, its banners are the words banner() writes for the same readings, and its grant and Serve texts are the real ones."""
    fx = _demo("nodes-auth.json")
    monkeypatch.setattr(settings, "ccboard_https_port", 8443)
    monkeypatch.setattr(settings, "port", 8000)
    monkeypatch.setattr(settings, "node_tags", ("tag:ccboard",))
    monkeypatch.setattr(settings, "node_cap", "")
    real = nodes_whois.settings_view()
    keys = set(real) - {"snippet"}
    assert fx["snippet"] == real["snippet"], "regenerate the snippet of demo/nodes-auth.json"
    assert {"token", "token-checked", "whois", "whois-cap", "tagged", "bind", "old", "noserve", "down", "nocap", "badmode", "unknown"} <= set(fx["states"])
    for name, st in fx["states"].items():
        assert set(st) - {"snippet"} == keys, name
        assert st["modes"] == list(nodes_whois.NODE_AUTH_MODES)
        assert st["mode"] in nodes_whois.NODE_AUTH_MODES or name == "badmode", name
        assert set(st["host"]) == set(real["host"]) and set(st["bind"]) == set(real["bind"]) and set(st["cap"]) == set(real["cap"]), name
        r = {"mode": st["mode"], "ready": st["ready"], "reason": st["reason"], "message": st["message"]}
        assert st["banner"] == nodes_whois.banner(r, st["host"]), f"{name}: the banner is not what the board would say"
        assert st["reason"] in (None, "bad_mode", "board_bind", "no_cap_name"), name
        assert st["ready"] is (st["reason"] is None), name
        if st["last_denial"]:
            assert set(st["last_denial"]) == {"at", "reason", "node"} and st["last_denial"]["reason"] in nodes_whois.REASONS, name
    monkeypatch.setattr(settings, "node_cap", "example.com/cap/ccboard")
    assert fx["states"]["whois-cap"]["snippet"] == nodes_whois.settings_view()["snippet"], "a state with a capability name carries the snippet without the placeholder note"
    for text in (json.dumps(fx), json.dumps(_demo("nodes.json")["pairs"])):
        assert "@" not in text and "/" + "Users/" not in text
        assert all(d == "example.com" or d.endswith("example.ts.net") for d in re.findall(r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|dev)\b", text.lower()))


def test_the_demo_pairs_use_the_board_s_short_device_form():
    for p in _demo("nodes.json")["pairs"]:
        if p.get("device_bound"):
            assert len(p["device_id"]) == 11 and p["device_id"][4:7] == "...", "4 characters, three dots, 4 characters"
            assert isinstance(p["device_tags"], list)
        else:
            assert not p.get("device_id") and not p.get("device_tags")
    assert nodes._device_short(None) is None and nodes._device_short("") is None and nodes._device_short("short") == "short"


def test_pending_ack_checks_bound_device(serve, monkeypatch):
    c, db, fake = serve
    set_mode(monkeypatch, "whois")
    code = nodes.create_code(db=db)["code"]
    reply = c.post("/api/nodes/pair", json={"code": code, "node": dict(CALLER)},
                   headers={"X-CCBoard": "1", "X-CCBoard-Pair-Ack": "1", "X-Forwarded-For": DEV_A})
    token = reply.json()["token"]
    assert c.post("/api/node/pair/ack", json={}, headers=bearer(token, DEV_B)).status_code == 403
    assert db.node_pairs()[0]["pending_until"] and nodes.peers(db) == []
    assert c.post("/api/node/pair/ack", json={}, headers=bearer(token, DEV_A)).status_code == 200
    assert db.node_pairs()[0]["pending_until"] is None
