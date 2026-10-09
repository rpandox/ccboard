"""Nodes epic P1, issue #133: the node id, GET /api/node/hello, GET /api/node (the card), state.node and the lane advice.

The Tailscale client is a fake reading behind one seam (app/nodes.py _ts_status; tests/conftest.py gives every test "Tailscale cannot be
read" unless it patches that), every data dir is a temp dir, nothing opens a connection to another host and the credentials files are traps."""
from __future__ import annotations

import builtins
import hashlib
import io
import json
import logging
import os
import re
import socket
import stat
import threading
import urllib.request
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import doctor, main, nodes
from app.config import settings
from tests.test_tasks_v2 import backlog, board, db  # noqa: F401 (board is a fixture)

ID = {"Tailscale-User-Login": "alice@example.com"}
H = {**ID, "X-CCBoard": "1"}
HELLO_KEYS = {"app", "api", "node_id"}
CARD_KEYS = {"app", "api", "node_id", "name", "url", "version", "os", "runtime", "now", "agents", "accounts", "lanes", "load", "sessions",
             "capabilities", "tailscale"}


def fake_ts(sid="nDEMO1CNTRL", dns="node-a.example.ts.net.", tags=None, os_name="linux"):
    return {"BackendState": "Running", "Self": {"ID": sid, "DNSName": dns, "OS": os_name, "UserID": 7, "Tags": tags}}


@pytest.fixture
def ts(monkeypatch):
    """Make Tailscale answer with a fake reading; the returned dict's "reading" is what the next fresh read sees."""
    box = {"reading": fake_ts(), "calls": 0}

    def status():
        box["calls"] += 1
        return box["reading"]
    monkeypatch.setattr(nodes, "_ts_status", status)
    nodes.reset()
    return box


def id_file() -> Path:
    return settings.data_dir / "node-id"


def walk(v):
    """Every key and every string value of a JSON value."""
    if isinstance(v, dict):
        for k, x in v.items():
            yield k
            yield from walk(x)
    elif isinstance(v, list):
        for x in v:
            yield from walk(x)
    elif isinstance(v, str):
        yield v


# ---------------------------------------------------------------- the node id

def test_the_first_read_with_tailscale_saves_ts_id_at_0600_and_every_later_read_returns_it(projects_dir, ts):
    assert nodes.node_id() == "ts:nDEMO1CNTRL"
    assert id_file().read_text().strip() == "ts:nDEMO1CNTRL"
    assert stat.S_IMODE(os.stat(id_file()).st_mode) == 0o600
    ts["reading"] = fake_ts("nOTHER2CNTRL")
    nodes.reset()                                              # a restart: nothing in memory
    assert nodes.node_id() == "ts:nDEMO1CNTRL", "read back from the file, never asked of Tailscale again"
    assert [p.name for p in settings.data_dir.iterdir() if p.name.startswith("node-id")] == ["node-id"], "no temp file is left behind"


def test_without_tailscale_the_id_is_n_and_sixteen_hex_and_stays(projects_dir):
    nid = nodes.node_id()
    assert re.fullmatch(r"n_[0-9a-f]{16}", nid)
    assert stat.S_IMODE(os.stat(id_file()).st_mode) == 0o600
    nodes.reset()
    assert nodes.node_id() == nid
    assert nodes.hello()["node_id"] == nid


def test_a_random_id_is_not_upgraded_when_tailscale_turns_up_later(projects_dir, ts):
    ts["reading"] = None
    nid = nodes.node_id()
    assert nid.startswith("n_")
    ts["reading"] = fake_ts()
    nodes.reset()
    assert nodes.node_id() == nid, "the id never changes by itself"


def test_the_id_never_comes_from_the_host_name(projects_dir, ts, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "Some-Box.local")
    ts["reading"] = None
    nid = nodes.node_id()
    assert "some" not in nid.lower() and "box" not in nid.lower()
    assert re.fullmatch(r"n_[0-9a-f]{16}", nid)


def test_a_changed_self_id_keeps_the_files_id_and_the_doctor_warns(projects_dir, ts):
    assert nodes.node_id() == "ts:nDEMO1CNTRL"
    ts["reading"] = fake_ts("nREREGCNTRL")
    nodes.reset()
    assert nodes.node_id() == "ts:nDEMO1CNTRL", "kept, not silently renamed"
    rep = nodes.id_report()
    assert rep["drifted"] is True and rep["ts_id"] == "nREREGCNTRL" and rep["id"] == "ts:nDEMO1CNTRL"
    out = doctor._c_node(None)
    assert out.status == "warn" and "registered again" in out.detail
    assert "every pair must be made again" in out.fix["text"] and "Reset the node id" in out.fix["text"]


def test_the_doctor_passes_for_a_steady_id_and_names_it(projects_dir, ts):
    out = doctor._c_node(None)
    assert out.status == "pass" and "ts:nDEMO1CNTRL" in out.detail and "node-a" in out.detail and out.fix is None


def test_a_corrupt_id_file_is_replaced_and_logged_once(projects_dir, ts, caplog):
    id_file().parent.mkdir(parents=True, exist_ok=True)
    id_file().write_text("not an id at all\n")
    with caplog.at_level(logging.WARNING, logger="ccboard.nodes"):
        assert nodes.node_id() == "ts:nDEMO1CNTRL"
        assert id_file().read_text().strip() == "ts:nDEMO1CNTRL"
        id_file().write_text("broken again")
        nodes._id_cache.clear()                                 # same process, the file broke again
        assert nodes.node_id() == "ts:nDEMO1CNTRL"
    assert [r.getMessage() for r in caplog.records].count("the node id file is not an id; replacing it") == 1


def test_an_unwritable_data_dir_still_answers_and_does_not_raise(projects_dir, ts, monkeypatch):
    def boom(*a, **k):
        raise PermissionError("read-only")
    monkeypatch.setattr(nodes, "_write_id_file", boom)
    assert nodes.node_id() == "ts:nDEMO1CNTRL"


# ---------------------------------------------------------------- the name and the address

def test_the_name_is_the_setting_then_the_magicdns_name_then_the_short_host_name(projects_dir, ts, monkeypatch):
    monkeypatch.setattr(settings, "node_name", "kitchen")
    assert nodes.display_name() == "kitchen"
    monkeypatch.setattr(settings, "node_name", "")
    assert nodes.display_name() == "node-a"
    ts["reading"] = None
    nodes.reset()
    monkeypatch.setattr(socket, "gethostname", lambda: "Some Host.local")
    assert nodes.display_name() == "Some-Host"
    monkeypatch.setattr(settings, "node_name", "bad name!")
    assert nodes.display_name() == "bad-name" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,40}", nodes.display_name())
    monkeypatch.setattr(settings, "node_name", "x" * 80)
    assert len(nodes.display_name()) == 41


def test_the_url_is_the_public_url_else_built_from_the_magicdns_name_else_null(projects_dir, ts, monkeypatch):
    monkeypatch.setattr(settings, "public_url", "https://board.example.ts.net:8443/")
    assert nodes.public_url() == "https://board.example.ts.net:8443"
    monkeypatch.setattr(settings, "public_url", "")
    monkeypatch.setattr(settings, "ccboard_https_port", 443)
    assert nodes.public_url() == "https://node-a.example.ts.net"
    monkeypatch.setattr(settings, "ccboard_https_port", 8443)
    assert nodes.public_url() == "https://node-a.example.ts.net:8443"
    ts["reading"] = None
    nodes.reset()
    assert nodes.public_url() is None


# ---------------------------------------------------------------- hello

def test_hello_answers_without_identity_with_exactly_three_keys(lite_client, ts):
    r = lite_client.get("/api/node/hello")
    assert r.status_code == 200 and set(r.json()) == HELLO_KEYS
    assert r.json() == {"app": "ccboard", "api": 1, "node_id": "ts:nDEMO1CNTRL"}
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    assert lite_client.get("/api/node/hello", headers=H).json() == r.json(), "an identity changes nothing"


def test_hello_is_rate_limited_to_30_a_minute_per_source_with_retry_after(lite_client, ts):
    for _ in range(30):
        assert lite_client.get("/api/node/hello").status_code == 200
    r = lite_client.get("/api/node/hello")
    assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= 61
    assert set(r.json()) == {"error"}, "a refusal says nothing about the node"
    assert lite_client.get("/api/node", headers=ID).status_code == 200, "the limit is on hello only"


def test_a_forged_forwarded_for_from_a_non_loopback_peer_buys_no_new_bucket(lite_client, ts):
    for i in range(30):
        assert lite_client.get("/api/node/hello", headers={"X-Forwarded-For": f"100.64.0.{i}"}).status_code == 200
    assert lite_client.get("/api/node/hello", headers={"X-Forwarded-For": "100.64.9.9"}).status_code == 429


def test_behind_tailscale_serve_each_caller_has_its_own_bucket(lite_client, ts):
    behind = TestClient(main.app, client=("127.0.0.1", 50000))       # the connection is from loopback, the caller is in X-Forwarded-For
    for _ in range(30):
        assert behind.get("/api/node/hello", headers={"X-Forwarded-For": "100.64.0.1"}).status_code == 200
    assert behind.get("/api/node/hello", headers={"X-Forwarded-For": "100.64.0.1"}).status_code == 429
    assert behind.get("/api/node/hello", headers={"X-Forwarded-For": "100.64.0.2"}).status_code == 200


def test_caller_addr_believes_forwarded_for_only_from_loopback():
    assert nodes.caller_addr("127.0.0.1", "100.64.0.7") == "100.64.0.7"
    assert nodes.caller_addr("::1", "1.2.3.4, 100.64.0.7") == "100.64.0.7", "the last hop, the one the nearest proxy appended"
    assert nodes.caller_addr("100.64.0.3", "9.9.9.9") == "100.64.0.3", "from anywhere else the header could be forged"
    assert nodes.caller_addr("127.0.0.1", "not an address") == "127.0.0.1"
    assert nodes.caller_addr("127.0.0.1", None) == "127.0.0.1"
    assert nodes.caller_addr(None, "100.64.0.7") == "unknown" and nodes.caller_addr("", None) == "unknown"


def test_the_limiter_frees_a_slot_when_the_oldest_hit_leaves_the_window():
    lim = nodes._Limiter(3, 60.0, 8)
    assert [lim.allow("a", t) for t in (0.0, 1.0, 2.0)] == [(True, 0)] * 3
    ok, wait = lim.allow("a", 10.0)
    assert ok is False and wait == 51
    assert lim.allow("b", 10.0) == (True, 0), "another source is not affected"
    assert lim.allow("a", 60.5) == (True, 0), "the first hit is out of the window"
    for i in range(20):                                             # many sources: the table stays bounded
        lim.allow(f"s{i}", 100.0 + i)
    assert len(lim._hits) <= 8


def test_every_other_node_route_without_identity_is_still_403(lite_client, ts):
    for path in ("/api/node", "/api/node/summary", "/api/node/nope", "/api/node/hello/x"):
        assert lite_client.get(path).status_code == 403, path
    assert lite_client.post("/api/node/hello", headers=ID).status_code in (403, 405)
    assert lite_client.get("/api/node/hello", headers={"Authorization": "Bearer ccbmcp_abc"}).status_code == 403, "a device token opens /mcp only"


def test_the_legacy_hub_token_still_opens_the_summary_and_nothing_else(lite_client, ts, monkeypatch):
    monkeypatch.setattr(settings, "hub_token", "hub-secret-for-the-test")
    hub = {"X-CCBoard-Hub": "hub-secret-for-the-test"}
    assert lite_client.get("/api/node/summary", headers=hub).status_code == 200
    assert lite_client.get("/api/node", headers=hub).status_code == 403
    assert lite_client.get("/api/state", headers=hub).status_code == 403


def test_a_single_board_starts_no_thread_and_makes_no_request(lite_client, ts, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a single board made an outbound connection")
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    before = {t.name for t in threading.enumerate()}
    assert lite_client.get("/api/node/hello").status_code == 200
    assert lite_client.get("/api/node", headers=ID).status_code == 200
    assert lite_client.get("/api/state", headers=ID).status_code == 200
    assert nodes.registry(main.db) == [] and main.db.kv_get(nodes.KV_PEERS) is None, "reading the registry writes nothing"
    assert {t.name for t in threading.enumerate()} - before - {"AnyIO worker thread"} == set(), "the request pool is the server's own; nothing of ours starts"


# ---------------------------------------------------------------- the card

FAKE_AGENTS = {
    "claude": {"installed": True, "version": "2.1.288", "loggedIn": True, "authMethod": "claude.ai", "email": "owner@example.com", "glyph": "x",
               "hooks": {"installed": True}},
    "codex": {"installed": True, "version": "0.160.0", "loggedIn": False, "glyph": "y", "hooks": {"installed": False, "trust": "review"}},
    "shell": {"installed": True, "version": None, "loggedIn": True, "glyph": "z", "hooks": {"installed": False}},
}
HOME_SNAP = {"host": "secret-host-name", "cpu_pct": 12.34, "load1": 0.456, "mem": {"total": 8, "used": 2, "pct": 25.04}, "disk": {"total": 8, "used": 4, "pct": 50.0},
             "uptime_s": 99, "cores": 4, "at": 1.0}
MAC_SNAP = {"host": "mac", "cpu_pct": None, "load1": None, "mem": None, "disk": None, "uptime_s": None, "cores": None, "at": 1.0}


@pytest.fixture
def cardenv(lite_client, ts, monkeypatch):
    from app import agents
    monkeypatch.setattr(agents, "status_all", lambda: json.loads(json.dumps(FAKE_AGENTS)))
    return lite_client


def seed_claude(n, current=0, org=None):
    accts = {}
    for i in range(n):
        key = str(uuid.UUID(int=0x1000 + i))
        accts[key] = {"key": key, "email": f"person{i}@example.com", "name": f"Person {i}", "org": "Org", "org_id": org or str(uuid.UUID(int=0x9000 + i)),
                      "plan": "max", "tier": "t", "config_dir": "/home/someone/.claude", "first_seen": "2026-10-01T00:00:00+00:00",
                      "last_seen": "2026-10-02T00:00:00+00:00", "label": f"Account {i}", "resets_5h": 1791006000 + i, "resets_7d": 1791606000}
    main.db.kv_set("accounts", accts)
    main.db.kv_set("account_current", {"key": list(accts)[current], "since": "2026-10-01T00:00:00+00:00"})
    return list(accts)


def test_the_card_has_every_key_for_a_fake_agent_set(cardenv, fake_tmux):
    c = cardenv.get("/api/node", headers=ID).json()
    assert set(c) == CARD_KEYS
    assert (c["app"], c["api"], c["node_id"], c["name"]) == ("ccboard", 1, "ts:nDEMO1CNTRL", "node-a")
    assert c["capabilities"] == ["state", "tasks", "sessions", "stream"]
    assert c["agents"] == [
        {"id": "claude", "installed": True, "version": "2.1.288", "logged_in": True, "hooks": True, "login_problem": False},
        {"id": "codex", "installed": True, "version": "0.160.0", "logged_in": False, "hooks": False, "login_problem": False},
        {"id": "shell", "installed": True, "version": None, "logged_in": True, "hooks": False, "login_problem": False}]
    assert c["tailscale"] == {"dns_name": "node-a.example.ts.net", "tags": [], "user_owned": True}
    assert c["os"]["tailscale_os"] == "linux" and c["os"]["system"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00", c["now"])
    assert c["sessions"] == {"live": 0, "working": 0, "needs_you": 0}
    assert set(c["lanes"]) == {"cap", "running", "free"} and set(c["load"]) == {"load1", "cores", "cpu_pct", "mem_pct", "disk_pct"}


def test_a_login_problem_is_a_flag_on_its_agent_without_its_text(cardenv):
    from app import login_problem
    login_problem.raise_(main.db, agent="codex", account=None, session=None, message="auth failed for owner@example.com")
    c = cardenv.get("/api/node", headers=ID).json()
    assert {a["id"]: a["login_problem"] for a in c["agents"]} == {"claude": False, "codex": True, "shell": False}
    assert "owner@example.com" not in json.dumps(c)


def test_a_tagged_device_says_so(cardenv, ts):
    ts["reading"] = fake_ts(tags=["tag:ccboard"])
    nodes.reset()
    assert cardenv.get("/api/node", headers=ID).json()["tailscale"] == {"dns_name": "node-a.example.ts.net", "tags": ["tag:ccboard"], "user_owned": False}


def test_session_counts_are_null_while_tmux_is_down(cardenv):
    c = cardenv.get("/api/node", headers=ID).json()
    assert c["sessions"] == {"live": None, "working": None, "needs_you": None}, "unknown is not zero"
    assert cardenv.get("/api/state", headers=ID).json()["node"]["sessions"] == c["sessions"]


def test_nothing_unreadable_is_zero_or_an_error(cardenv, ts, monkeypatch):
    ts["reading"] = None
    nodes.reset()
    c = nodes.card(main.db, health_snap=MAC_SNAP, sessions=None)
    assert c["load"] == {"load1": None, "cores": None, "cpu_pct": None, "mem_pct": None, "disk_pct": None}
    assert c["sessions"] == {"live": None, "working": None, "needs_you": None}
    assert c["tailscale"] == {"dns_name": None, "tags": None, "user_owned": None} and c["os"]["tailscale_os"] is None
    assert c["url"] is None and c["node_id"].startswith("n_")
    assert nodes.card(main.db, health_snap=None)["load"] == c["load"]
    assert nodes.card(main.db, health_snap=HOME_SNAP)["load"] == {"load1": 0.46, "cores": 4, "cpu_pct": 12.3, "mem_pct": 25.0, "disk_pct": 50.0}


def test_accounts_supported_follows_the_platform_gate(cardenv, monkeypatch):
    from app import account_store
    monkeypatch.setattr(account_store, "supported", lambda: False)
    assert cardenv.get("/api/node", headers=ID).json()["accounts"]["supported"] is False
    monkeypatch.setattr(account_store, "supported", lambda: True)
    assert cardenv.get("/api/node", headers=ID).json()["accounts"]["supported"] is True


def test_account_rows_carry_label_window_and_a_one_way_key_and_no_email(cardenv):
    keys = seed_claude(2, current=1)
    main.db.kv_set("rate_limits", {"five_hour": {"used_percentage": 100, "resets_at": 1791006500}})
    c = cardenv.get("/api/node", headers=ID).json()
    items = c["accounts"]["items"]
    assert [i["agent"] for i in items] == ["claude", "claude"] and [i["current"] for i in items] == [True, False], "the current account first"
    cur, other = items
    assert cur["label"] == "Account 1" and cur["limited"] is True and cur["window"]["pct"] == 100.0 and cur["window"]["known"] is True
    assert cur["window"]["resets_at"] == 1791006500 and cur["window"]["backoff_until"] is None
    assert other["window"] == {"pct": None, "resets_at": 1791006000, "backoff_until": None, "known": False} and other["limited"] is False
    want = hashlib.sha256(("ccboard-subscription:" + keys[1]).encode()).hexdigest()[:12]
    assert re.fullmatch(r"[0-9a-f]{12}", cur["subscription_key"]) and cur["subscription_key"] == want
    assert keys[1] not in json.dumps(c) and "person" not in json.dumps(c).lower()


def test_a_backoff_marks_the_current_account_limited(cardenv):
    seed_claude(1)
    from app import scheduler
    main.db.kv_set(scheduler.KV_BACKOFF, "2099-01-01T00:00:00+00:00")
    cur = cardenv.get("/api/node", headers=ID).json()["accounts"]["items"][0]
    assert cur["window"]["backoff_until"] == "2099-01-01T00:00:00+00:00" and cur["limited"] is True


def test_subscription_key_is_null_unless_an_account_id_is_stored(cardenv):
    uid, org = str(uuid.UUID(int=5)), str(uuid.UUID(int=6))
    main.db.kv_set("accounts", {
        uid: {"key": uid, "email": "a@example.com", "org_id": org, "label": "has id"},
        "b@example.com": {"key": "b@example.com", "email": "b@example.com", "org_id": None, "label": "email only"},
        org: {"key": org, "email": "c@example.com", "org_id": org, "label": "org only"},
        "unknown": {"key": "unknown", "label": "unknown"}})
    main.db.kv_set("codex_accounts", {
        "a" * 24: {"key": "a" * 24, "label": "codex with id", "account_id": "acct-123", "added_at": "2026-10-01"},
        "b" * 24: {"key": "b" * 24, "label": "codex without", "account_id": None, "added_at": "2026-10-02"}})
    main.db.kv_set("codex_account_current", {"key": "b" * 24})
    items = {i["label"]: i for i in cardenv.get("/api/node", headers=ID).json()["accounts"]["items"]}
    assert re.fullmatch(r"[0-9a-f]{12}", items["has id"]["subscription_key"])
    assert items["email only"]["subscription_key"] is None and items["org only"]["subscription_key"] is None and items["unknown"]["subscription_key"] is None
    assert items["codex with id"]["subscription_key"] == nodes.subscription_key("acct-123") and items["codex without"]["subscription_key"] is None
    assert items["codex without"]["current"] is True and nodes.subscription_key("") is None and nodes.subscription_key(None) is None
    assert nodes.subscription_key("acct-123") == nodes.subscription_key(" acct-123 "), "the same login on two nodes gives the same key"


def test_an_email_shaped_label_is_dropped(cardenv):
    keys = seed_claude(1)
    rec = main.db.kv_get("accounts")["value"]
    rec[keys[0]]["label"] = "me@example.com"
    main.db.kv_set("accounts", rec)
    assert cardenv.get("/api/node", headers=ID).json()["accounts"]["items"][0]["label"] is None


def test_no_field_holds_the_home_directory_an_email_or_the_word_token(cardenv, projects_dir, tmp_path):
    seed_claude(3)
    main.db.kv_set("codex_accounts", {"c" * 24: {"key": "c" * 24, "label": "Codex", "account_id": "acct-9", "added_at": "x"}})
    c = cardenv.get("/api/node", headers=ID).json()
    text = json.dumps(c)
    for needle in (str(Path.home()), str(settings.data_dir), str(tmp_path), str(settings.projects_dir)):
        assert needle not in text, needle
    assert "@" not in text and "token" not in text.lower() and "secret" not in text.lower()
    assert "owner@example.com" not in text and "secret-host-name" not in text
    for s in walk(c):
        assert "token" not in s.lower(), s


def test_the_card_stays_under_8_kb_with_ten_accounts(cardenv):
    seed_claude(10, current=3)
    main.db.kv_set("codex_accounts", {f"{i:024x}": {"key": f"{i:024x}", "label": f"Codex {i}", "account_id": f"acct-{i}", "added_at": f"2026-10-0{i}"} for i in range(1, 4)})
    c = cardenv.get("/api/node", headers=ID).json()
    assert len(c["accounts"]["items"]) == 13
    assert len(json.dumps(c, separators=(",", ":"))) < 8 * 1024


def test_the_credentials_files_are_never_opened(cardenv, tmp_path, monkeypatch):
    claude_dir, codex_dir, slot = tmp_path / "claude", tmp_path / "codex", settings.data_dir / "accounts" / "slot1"
    for d, name in ((claude_dir, ".credentials.json"), (codex_dir, "auth.json"), (slot, ".credentials.json")):
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text('{"accessToken": "SENTINEL-DO-NOT-READ"}')
    monkeypatch.setattr(settings, "claude_config_dir", claude_dir)
    monkeypatch.setenv("CODEX_HOME", str(codex_dir))
    seed_claude(2)
    main.db.kv_set("codex_accounts", {"d" * 24: {"key": "d" * 24, "label": "Codex", "account_id": "acct-1", "added_at": "x"}})
    real_open, opened = io.open, []

    def trap(file, *a, **k):
        name = os.fspath(file) if isinstance(file, (str, bytes, os.PathLike)) else ""
        if os.path.basename(os.fsdecode(name)) in (".credentials.json", ".credentials.json.prev", "auth.json"):
            opened.append(name)
            raise AssertionError(f"a credentials file was opened: {name}")
        return real_open(file, *a, **k)
    monkeypatch.setattr(builtins, "open", trap)
    monkeypatch.setattr(io, "open", trap)
    c = cardenv.get("/api/node", headers=ID).json()
    st = cardenv.get("/api/state", headers=ID).json()
    assert opened == [] and "SENTINEL" not in json.dumps(c) + json.dumps(st["node"]) and len(c["accounts"]["items"]) == 3


# ---------------------------------------------------------------- state.node and the summary

def test_state_node_is_the_card_without_agents_and_accounts_plus_handle_local(cardenv):
    st = cardenv.get("/api/state", headers=ID).json()
    n = st["node"]
    assert n["handle"] == "local" and "agents" not in n and "accounts" not in n
    assert set(n) == (CARD_KEYS - {"agents", "accounts"}) | {"handle"}
    card = cardenv.get("/api/node", headers=ID).json()
    assert {k: n[k] for k in ("app", "api", "node_id", "name", "url", "capabilities", "tailscale")} == {k: card[k] for k in ("app", "api", "node_id", "name", "url", "capabilities", "tailscale")}
    assert st["node_name"] == (settings.node_name or st["health"]["host"]), "state.node_name is as it was"


def test_state_node_counts_the_live_sessions(board):
    from tests.test_tasks_v2 import make_session
    make_session(board, name="s1", state="idle")
    make_session(board, name="s2", state="working")
    make_session(board, name="s3", state="waiting")
    main._invalidate_scan()
    n = board.client.get("/api/state", headers=ID).json()["node"]["sessions"]
    assert n["live"] == 3 and n["working"] == 1 and n["needs_you"] >= 1
    assert board.client.get("/api/node", headers=ID).json()["sessions"] == n


def test_the_node_summary_is_as_it_was(cardenv):
    s = cardenv.get("/api/node/summary", headers=ID).json()
    assert set(s) == {"node", "health", "tmux_down", "sessions", "attention", "tasks", "usage", "backup", "url"}
    assert "node_id" not in s and "ts:nDEMO1CNTRL" not in json.dumps(s)
    assert cardenv.get("/api/health", headers=ID).json().keys() == s.keys()


# ---------------------------------------------------------------- lanes

def running(slug, **kw):
    main.db.task_add(project="shop", repo="api", slug=slug, title=slug, prompt="p", branch=f"worktree-{slug}", worktree=f"/w/{slug}",
                     tmux_name=f"shop--api--t-{slug}", **kw)


def test_two_running_lanes_under_a_cap_of_three_leave_one_free(lite_client, ts, monkeypatch):
    monkeypatch.setattr(settings, "node_lanes", 3)
    running("a")
    running("b")
    main.db.task_add(project="shop", repo="api", slug="queued", title="q", prompt="p", phase="queued")
    main.db.task_add(project="shop", repo="api", slug="done", title="d", prompt="p", tmux_name="shop--api--t-done", phase="done")
    main.db.task_add(project="shop", repo="api", slug="hand", title="h", prompt="p", tmux_name="shop--api--s9", mode="session", phase="running")
    main.db.task_add(project="shop", repo="api", slug="gone", title="g", prompt="p", tmux_name="shop--api--t-gone", archived_at="2026-10-01T00:00:00+00:00")
    assert nodes.lanes_view(main.db) == {"cap": 3, "running": 2, "free": 1}
    assert lite_client.get("/api/node", headers=ID).json()["lanes"] == {"cap": 3, "running": 2, "free": 1}
    running("c")
    running("d")
    assert nodes.lanes_view(main.db) == {"cap": 3, "running": 4, "free": 0}, "over the cap the free count stops at 0"


def test_a_cap_of_zero_means_no_advice(lite_client, ts, monkeypatch):
    monkeypatch.setattr(settings, "node_lanes", 0)
    for s in "abcde":
        running(s)
    assert nodes.lanes_view(main.db) == {"cap": 0, "running": 5, "free": None} and nodes.lane_warning(main.db) is None


def test_the_lane_setting_reads_a_whole_number_else_the_default(monkeypatch):
    from app.config import Settings
    def lanes(v):
        s = Settings({"CCBOARD_NODE_LANES": v} if v is not None else {})
        return s.node_lanes, s.node_lanes_bad
    assert lanes(None) == (3, False) and lanes("") == (3, False) and lanes("5") == (5, False) and lanes("0") == (0, False)
    assert lanes("many") == (3, True) and lanes("-1") == (3, True) and lanes("2.5") == (3, True) and lanes("1000") == (3, True)


def test_a_hand_dispatch_over_the_cap_succeeds_and_carries_a_warning(board, monkeypatch):
    monkeypatch.setattr(settings, "node_lanes", 1)
    a, b = backlog(board), backlog(board, title="Second task")
    r1 = board.client.post(f"/api/tasks/{a}/dispatch", headers=H, json={"mode": "lane"})
    assert r1.status_code == 200 and "lane_warning" not in r1.json(), "one lane under a cap of one: no advice"
    r2 = board.client.post(f"/api/tasks/{b}/dispatch", headers=H, json={"mode": "lane"})
    assert r2.status_code == 200 and r2.json()["phase"] == "running", "the cap never refuses a hand start"
    assert r2.json()["lane_warning"] == {"cap": 1, "running": 2}


def test_a_dispatch_with_no_cap_never_warns(board, monkeypatch):
    monkeypatch.setattr(settings, "node_lanes", 0)
    a, b = backlog(board), backlog(board, title="Second task")
    for t in (a, b):
        r = board.client.post(f"/api/tasks/{t}/dispatch", headers=H, json={"mode": "lane"})
        assert r.status_code == 200 and "lane_warning" not in r.json()


def test_a_dispatch_into_a_running_session_is_not_a_lane(board, monkeypatch):
    from tests.test_tasks_v2 import make_session
    monkeypatch.setattr(settings, "node_lanes", 1)
    a = backlog(board)
    board.client.post(f"/api/tasks/{a}/dispatch", headers=H, json={"mode": "lane"})
    name = make_session(board, name="idle1", state="idle")
    b = backlog(board, title="Second task")
    r = board.client.post(f"/api/tasks/{b}/dispatch", headers=H, json={"mode": "session", "session": name})
    assert r.status_code == 200 and "lane_warning" not in r.json()
    assert nodes.lanes_view(main.db)["running"] == 1


def test_task_origin_never_reaches_the_state_rows(board):
    tid = backlog(board)
    main.db.task_update(tid, origin={"node": "n_0123456789abcdef", "user": "alice"})
    row = [t for t in board.client.get("/api/state", headers=H).json()["tasks"] if t["id"] == tid][0]
    assert "origin" not in row
