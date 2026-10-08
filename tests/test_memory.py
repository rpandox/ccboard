"""app/memory.py (claude-mem worker health), app/agents/monitor.py (the 20 s kv writer) and their wiring: state.memory and the doctor group.

Nothing here touches the real ~/.claude-mem or ~/.claude: the `mem_home` fixture (tests/conftest.py, autouse in this module) points
settings at temp dirs, and every worker is `mem_worker`, the fake worker of tests/mem_fake.py on a loopback port that answers the way
the box's worker does (/health, /api/readiness, /api/stats, /api/processing-status here; the port lives in worker.pid).
"""
import http.client
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import doctor, memory, platform
from app.agents import monitor
from app.config import Settings, settings
from app.db import DB
from tests.mem_fake import PROCESSING, STATS, MemWorker, closed_port   # noqa: F401  (the fake worker, shared with the proxy tests)

H = {"Tailscale-User-Login": "alice@example.com"}


@pytest.fixture(autouse=True)
def _mem_home_everywhere(mem_home):
    """Every test here runs in the temp claude-mem home (tests/conftest.py mem_home)."""
    return mem_home


def write_pid(mem_home, **kw):
    (mem_home / "worker.pid").write_text(json.dumps(kw))


def write_cfg(mem_home, **kw):
    (mem_home / "settings.json").write_text(json.dumps(kw))


# ------------------------------------------------------------------ settings

def test_settings_defaults_and_overrides():
    s = Settings(env={})
    assert s.claude_mem is True and s.mem_port is None
    assert s.claude_mem_dir == Path.home() / ".claude-mem"
    s = Settings(env={"CCBOARD_CLAUDE_MEM": "0", "CCBOARD_MEM_PORT": " 37700 ", "CLAUDE_MEM_DATA_DIR": "/data/mem"})
    assert s.claude_mem is False and s.mem_port == 37700 and s.claude_mem_dir == Path("/data/mem")
    assert Settings(env={"CCBOARD_CLAUDE_MEM": "1"}).claude_mem is True
    assert Settings(env={"CCBOARD_CLAUDE_MEM": ""}).claude_mem is True        # empty = the default


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "70000", "12.5", "3770x"])
def test_settings_ignore_a_bad_mem_port(raw):
    assert Settings(env={"CCBOARD_MEM_PORT": raw}).mem_port is None


def test_default_port_is_the_plugins_own_37700_plus_uid_modulo_100():
    """worker-service.cjs: CLAUDE_MEM_WORKER_PORT default `37700+(process.getuid?.()??77)%100` (uid 1000, a Mac uid 501)."""
    assert memory.default_port(1000) == 37700 and memory.default_port(501) == 37701
    assert memory.default_port(0) == 37700 and memory.default_port(1099) == 37799 and memory.default_port(1100) == 37700
    assert memory.default_port() == 37700 + os.getuid() % 100
    assert 37700 <= memory.default_port() <= 37799


def test_default_port_without_a_uid_is_37777(monkeypatch):
    """The plugin's `?? 77` fallback (13.34.2 on a Mac and 13.29.0 on a Linux box both read `String(37700+(process.getuid?.()??77)%100)`)."""
    monkeypatch.setattr(memory.plat, "current_uid", lambda: None)             # no getuid on this platform
    assert memory.default_port() == 37777 and memory.default_port(None) == 37777
    assert memory.default_port(1000) == 37700, "a uid that is given is used as is"


def test_default_port_is_pinned_for_the_uids_that_matter(monkeypatch):
    monkeypatch.setattr(memory.plat, "current_uid", lambda: None)
    assert [memory.default_port(u) for u in (0, 501, 1000, 1099)] == [37700, 37701, 37700, 37799]
    assert memory.default_port(None) == 37777
    monkeypatch.setattr(memory.plat, "current_uid", lambda: 501)
    assert memory.default_port() == 37701, "a Mac's first account"


def test_the_module_constant_is_computed_at_import_not_hardcoded(tmp_path):
    """(the autouse fixture replaces DEFAULT_PORT with a dead port, so a fresh interpreter reads the real one)"""
    root = Path(__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, "-c", "from app import memory; print(memory.DEFAULT_PORT)"], cwd=root, capture_output=True,
                         text=True, timeout=60, env={**os.environ, "CCBOARD_DATA_DIR": str(tmp_path / "data")})
    assert out.returncode == 0, out.stderr[-400:]
    assert int(out.stdout.strip().splitlines()[-1]) == 37700 + os.getuid() % 100


def test_spec_constants():
    assert memory.TIMEOUT == 2.0 and memory.BODY_CAP == 256 * 1024 and memory.DEFAULT_PORT != 0
    assert monitor.INTERVAL_SECONDS == 20 and memory.KV_HEALTH == "mem_health"
    assert memory.BUDGET < doctor.CHECK_TIMEOUT, "a whole probe must fit inside the doctor's cap per check"


# ------------------------------------------------------------------ discovery

def test_discovery_order_env_then_worker_pid_then_settings_then_default(mem_home, monkeypatch):
    assert memory.discover()["source"] == "default" and memory.discover()["port"] == memory.DEFAULT_PORT
    write_cfg(mem_home, CLAUDE_MEM_WORKER_PORT="37888")
    d = memory.discover()
    assert (d["port"], d["source"], d["pid"]) == (37888, "settings", None)
    write_pid(mem_home, pid=321, port=37700, startedAt="x", startToken="y")
    d = memory.discover()
    assert (d["port"], d["source"], d["pid"]) == (37700, "worker.pid", 321)
    monkeypatch.setattr(settings, "mem_port", 40001)
    d = memory.discover()
    assert (d["port"], d["source"], d["pid"]) == (40001, "env", 321)
    assert memory.worker_base() == "http://127.0.0.1:40001"


def test_discovery_falls_through_a_missing_or_corrupt_source(mem_home):
    (mem_home / "worker.pid").write_text("{not json")
    write_cfg(mem_home, CLAUDE_MEM_WORKER_PORT=37888)
    assert memory.discover()["source"] == "settings"
    (mem_home / "worker.pid").write_text(json.dumps({"pid": 5, "port": "nope"}))
    assert (memory.discover()["source"], memory.discover()["pid"]) == ("settings", 5)       # the pid survives a bad port
    for bad in (0, 65536, -1, True, None, [37700], {"a": 1}, "37700x", ""):
        write_pid(mem_home, pid=5, port=bad)
        assert memory.discover()["source"] == "settings", bad
    (mem_home / "settings.json").write_text("[1, 2]")
    assert memory.discover()["source"] == "default"


def test_worker_pid_file_shapes(mem_home):
    p = mem_home / "worker.pid"
    assert memory.worker_pid_file() == {"pid": None, "port": None, "started_at": None}
    p.write_text(json.dumps({"pid": 77, "port": "37700", "startedAt": 1759536000000, "startToken": "t"}))
    assert memory.worker_pid_file() == {"pid": 77, "port": 37700, "started_at": 1759536000000}      # a string port is read
    p.write_text("1234\n")
    assert memory.worker_pid_file() == {"pid": 1234, "port": None, "started_at": None}              # a bare number is a pid
    for junk in ("[]", "null", "true", '{"pid": -3}', '{"pid": "7"}', "x" * 100, "{" * 70_000):
        p.write_text(junk)
        assert memory.worker_pid_file()["pid"] is None, junk


def test_a_non_loopback_host_in_settings_is_refused_before_any_connection(mem_home, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a socket was opened for a non-loopback host")
    monkeypatch.setattr(http.client.HTTPConnection, "connect", boom)
    for host in ("10.0.0.5", "example.com", "0.0.0.0", "192.168.1.2", "::"):
        write_cfg(mem_home, CLAUDE_MEM_WORKER_HOST=host, CLAUDE_MEM_WORKER_PORT=37700)
        with pytest.raises(memory.NotLoopback):
            memory.worker_base()
        h = memory.health()
        assert h["state"] == "down" and "non-loopback" in h["reason"], host


@pytest.mark.parametrize("host,base", [("127.0.0.1", "http://127.0.0.1:37700"), ("localhost", "http://127.0.0.1:37700"),
                                       ("127.0.0.2", "http://127.0.0.2:37700"), ("::1", "http://[::1]:37700"), ("[::1]", "http://[::1]:37700")])
def test_loopback_hosts_in_settings_are_accepted(mem_home, host, base):
    write_cfg(mem_home, CLAUDE_MEM_WORKER_HOST=host, CLAUDE_MEM_WORKER_PORT=37700)
    assert memory.worker_base() == base


@pytest.mark.parametrize("base", ["http://10.0.0.5:37701", "http://example.com:37701", "https://127.0.0.1:37701", "http://127.0.0.1",
                                  "ftp://127.0.0.1:21", "http://0.0.0.0:37701", "127.0.0.1:37701", "", "http://user@10.0.0.5:80"])
def test_fetch_refuses_anything_but_a_loopback_http_url(monkeypatch, base):
    def boom(*a, **k):
        raise AssertionError("a socket was opened")
    monkeypatch.setattr(http.client.HTTPConnection, "connect", boom)
    with pytest.raises(memory.NotLoopback):
        memory.fetch(base, "/health")


# ------------------------------------------------------------------ fetch

def test_fetch_is_get_only_and_parses_json(mem_worker):
    code, body = memory.fetch(f"http://127.0.0.1:{mem_worker.port}", "/health")
    assert code == 200 and body["pid"] == 4242
    assert mem_worker.requests == [("GET", "/health")]


def test_fetch_non_json_body_is_none_and_status_is_kept(mem_worker):
    mem_worker.body["/health"] = b"<html>nope</html>"
    mem_worker.status["/health"] = 502
    assert memory.fetch(f"http://127.0.0.1:{mem_worker.port}", "/health") == (502, None)


def test_fetch_caps_the_body(mem_worker):
    mem_worker.body["/api/stats"] = b'{"x": "' + b"a" * (memory.BODY_CAP + 5000) + b'"}'
    assert memory.fetch(f"http://127.0.0.1:{mem_worker.port}", "/api/stats") == (200, None)
    mem_worker.body["/api/stats"] = b'{"x": "' + b"a" * (memory.BODY_CAP - 100) + b'"}'          # just under the cap still parses
    code, body = memory.fetch(f"http://127.0.0.1:{mem_worker.port}", "/api/stats")
    assert code == 200 and len(body["x"]) == memory.BODY_CAP - 100


def test_fetch_error_classes(mem_worker):
    with pytest.raises(memory.Refused):
        memory.fetch(f"http://127.0.0.1:{closed_port()}", "/health")
    mem_worker.delay["*"] = 1.0
    t0 = time.monotonic()
    with pytest.raises(memory.NoAnswer):
        memory.fetch(f"http://127.0.0.1:{mem_worker.port}", "/health", timeout=0.2)
    assert time.monotonic() - t0 < 0.9


def test_fetch_a_worker_that_hangs_up_is_no_answer():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]

    def accept_and_close():
        c, _ = s.accept()
        c.close()
    threading.Thread(target=accept_and_close, daemon=True).start()
    try:
        with pytest.raises(memory.NoAnswer):
            memory.fetch(f"http://127.0.0.1:{port}", "/health", timeout=1.0)
    finally:
        s.close()


# ------------------------------------------------------------------ health: the three states

def test_up_reads_every_number(mem_worker):
    h = memory.health()
    assert h["state"] == "up" and h["reason"] is None
    assert (h["version"], h["port"], h["port_source"], h["pid"]) == ("13.29.0", mem_worker.port, "worker.pid", 4242)
    assert (h["observations"], h["sessions"], h["summaries"], h["db_size"]) == (9684, 270, 170, 123456789)
    assert (h["queue_depth"], h["processing"], h["active_sessions"]) == (435, True, 2)
    assert h["last_error"] is None
    assert datetime.fromisoformat(h["at"]).tzinfo is not None
    assert set(h) == {"state", "version", "port", "port_source", "pid", "observations", "sessions", "summaries", "db_size",
                      "queue_depth", "processing", "active_sessions", "last_error", "reason", "at"}
    json.dumps(h)                                                  # it goes into the kv and the state
    assert set(mem_worker.paths()) == {"/health", "/api/readiness", "/api/stats", "/api/processing-status"}
    assert {m for m, _ in mem_worker.requests} == {"GET"}


def test_a_macs_health_answer_is_parsed(mem_worker):
    """tests/fixtures/claude_mem_health_darwin.json is a real worker 13.34.2 answer from a Mac (platform darwin, managed false, a pid, no
    activeSessions, many extra keys); the parser takes the pid and ignores the rest, and the missing session count falls back to /api/stats."""
    from tests.mem_fake import fixture
    mac = fixture("health_darwin")
    assert (mac["platform"], mac["managed"], mac["version"], mac["status"]) == ("darwin", False, "13.34.2", "ok")
    mem_worker.body["/health"] = mac
    h = memory.health()
    assert h["state"] == "up" and h["pid"] == mac["pid"] == 2216 and h["active_sessions"] == 2
    assert "darwin" not in json.dumps(h), "nothing of the worker's own answer is passed on"
    assert mac["workerPath"].startswith("/home/<user>/"), "the fixture holds no real account name"


def test_the_default_port_on_a_mac_is_found_before_the_first_start(mem_home, monkeypatch):
    monkeypatch.setattr(memory, "DEFAULT_PORT", memory.default_port(501))
    d = memory.discover()
    assert (d["port"], d["source"], d["pid"]) == (37701, "default", None)
    write_pid(mem_home, pid=2216, port=37702, startedAt="x", startToken="y")
    assert memory.discover()["port"] == 37702, "a running worker is found by its own file, not by the uid rule"


def test_up_through_the_env_port_without_a_worker_pid(mem_worker, mem_home, monkeypatch):
    (mem_home / "worker.pid").unlink()
    monkeypatch.setattr(settings, "mem_port", mem_worker.port)
    h = memory.health()
    assert h["state"] == "up" and h["port_source"] == "env" and h["pid"] == 4242      # the pid comes from /health when the file is gone


def test_up_through_the_settings_port(mem_worker, mem_home):
    (mem_home / "worker.pid").unlink()
    write_cfg(mem_home, CLAUDE_MEM_WORKER_PORT=str(mem_worker.port))
    h = memory.health()
    assert h["state"] == "up" and h["port_source"] == "settings"


def test_not_ready_is_degraded_and_the_numbers_are_not_asked_for(mem_worker):
    mem_worker.status["/api/readiness"] = 503
    mem_worker.body["/api/readiness"] = {"status": "initializing", "message": "still starting"}
    h = memory.health()
    assert h["state"] == "degraded" and "not ready" in h["reason"] and "503" in h["reason"]
    assert h["observations"] is None and h["version"] is None
    assert "/api/stats" not in mem_worker.paths() and "/api/processing-status" not in mem_worker.paths()


@pytest.mark.parametrize("body", [{"status": "initializing"}, {"status": "starting", "mcpReady": False}, {"status": "ready", "ready": False}])
def test_a_200_readiness_that_says_not_ready_is_degraded(mem_worker, body):
    mem_worker.body["/api/readiness"] = body
    assert memory.health()["state"] == "degraded"


def test_health_non_200_is_degraded(mem_worker):
    mem_worker.status["/health"] = 503
    h = memory.health()
    assert h["state"] == "degraded" and "503" in h["reason"] and "/api/readiness" not in mem_worker.paths()


def test_a_refused_connection_is_down(mem_home):
    write_pid(mem_home, pid=999999, port=closed_port())
    h = memory.health()
    assert h["state"] == "down" and h["port_source"] == "worker.pid" and h["pid"] == 999999
    assert "refused" in h["reason"] and "stale" in h["reason"]
    assert all(h[k] is None for k in ("version", "observations", "queue_depth", "db_size"))


def test_no_worker_pid_and_nothing_on_the_default_port_is_down(mem_home):
    h = memory.health()
    assert h["state"] == "down" and h["port_source"] == "default" and h["pid"] is None
    assert "worker.pid" in h["reason"]


def test_a_hanging_worker_is_degraded_after_two_seconds(mem_worker):
    mem_worker.delay["*"] = 3.0
    t0 = time.monotonic()
    h = memory.health()
    took = time.monotonic() - t0
    assert h["state"] == "degraded" and "/health" in h["reason"] and "2 s" in h["reason"]
    assert 1.8 <= took < 3.0, took


def test_a_readiness_that_hangs_is_degraded(mem_worker, monkeypatch):
    monkeypatch.setattr(memory, "TIMEOUT", 0.3)
    mem_worker.delay["/api/readiness"] = 1.0
    h = memory.health()
    assert h["state"] == "degraded" and "readiness" in h["reason"]


def test_enrichment_failures_never_demote_an_up_worker(mem_worker, monkeypatch):
    monkeypatch.setattr(memory, "TIMEOUT", 0.3)
    mem_worker.status["/api/stats"] = 500
    mem_worker.delay["/api/processing-status"] = 1.0
    h = memory.health()
    assert h["state"] == "up"
    assert h["observations"] is None and h["version"] is None and h["queue_depth"] is None and h["processing"] is None
    assert h["active_sessions"] == 2                                # from /health


def test_garbage_numbers_are_dropped(mem_worker):
    mem_worker.body["/api/stats"] = {"worker": {"version": ["x"], "activeSessions": "many"},
                                     "database": {"observations": -4, "sessions": True, "summaries": "170", "size": None}}
    mem_worker.body["/api/processing-status"] = {"isProcessing": "yes", "queueDepth": 1.5}
    mem_worker.body["/health"] = {"status": "ok", "pid": "4242", "activeSessions": None}
    h = memory.health()
    assert h["state"] == "up"
    assert (h["version"], h["observations"], h["sessions"], h["summaries"], h["db_size"]) == (None,) * 5
    assert (h["queue_depth"], h["processing"], h["active_sessions"]) == (None, None, None)
    assert h["pid"] == os.getpid()                                  # the bad /health pid is ignored; worker.pid's stays


def test_an_oversized_stats_body_leaves_the_numbers_empty_but_the_worker_up(mem_worker):
    mem_worker.body["/api/stats"] = b'{"database": {"observations": 1}, "pad": "' + b"a" * (memory.BODY_CAP + 10) + b'"}'
    h = memory.health()
    assert h["state"] == "up" and h["observations"] is None and h["queue_depth"] == 435


def test_the_probe_budget_bounds_the_whole_call(mem_worker):
    mem_worker.delay["*"] = 5.0
    t0 = time.monotonic()
    h = memory.health(budget=0.3)
    assert h["state"] == "degraded" and time.monotonic() - t0 < 1.0


def test_four_slow_answers_stay_inside_the_budget(mem_worker, monkeypatch):
    mem_worker.delay["*"] = 0.35
    monkeypatch.setattr(memory, "TIMEOUT", 1.0)
    t0 = time.monotonic()
    h = memory.health(budget=0.8)                                    # /health + readiness fit, the enrichment runs out of budget
    assert h["state"] in ("up", "degraded") and time.monotonic() - t0 < 1.6
    assert h["queue_depth"] is None


def test_health_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("disk gone")
    monkeypatch.setattr(memory, "discover", boom)
    h = memory.health()
    assert h["state"] == "down" and h["reason"] == "probe error: RuntimeError"


def test_the_real_error_class_is_never_leaked_as_a_path(mem_home, monkeypatch):
    monkeypatch.setattr(memory, "discover", lambda: (_ for _ in ()).throw(OSError(f"/secret/{mem_home}")))
    assert "/secret" not in json.dumps(memory.health())


# ------------------------------------------------------------------ observer-health.json

def write_obs(mem_home, **kw):
    (mem_home / "observer-health.json").write_text(json.dumps(kw))


def test_observer_error_from_epoch_milliseconds(mem_home):
    ms = int(datetime(2026, 10, 3, 17, 23, 0, tzinfo=timezone.utc).timestamp() * 1000)
    write_obs(mem_home, consecutiveFailures=3, failingSinceAt=ms - 60_000, lastErrorAt=ms, lastSuccessAt=ms - 600_000,
              lastErrorMessage="Provider reported the inference allowance exhausted", lastErrorProvider="claude")
    assert memory.observer_error() == {"at": "2026-10-03T17:23:00+00:00", "message": "Provider reported the inference allowance exhausted",
                                       "provider": "claude", "failures": 3, "last_success_at": "2026-10-03T17:13:00+00:00"}


def test_observer_error_carries_the_last_success_so_a_recovery_is_visible(mem_home):
    """The shape of the real file on a recovered observer: the error is older than the last success and nothing is failing now."""
    write_obs(mem_home, consecutiveFailures=0, failingSinceAt=None, lastErrorAt=1791060544321, lastSuccessAt=1791076801109,
              lastErrorMessage="Provider reported the inference allowance exhausted", lastErrorProvider="claude")
    e = memory.observer_error()
    assert e["failures"] == 0 and e["at"] < e["last_success_at"]
    assert e["last_success_at"] == datetime.fromtimestamp(1791076801.109, timezone.utc).isoformat(timespec="seconds")


@pytest.mark.parametrize("v", [None, "", "never", True, [1], 1e30])
def test_observer_error_last_success_is_none_when_missing_or_unusable(mem_home, v):
    write_obs(mem_home, lastErrorAt=1759512180000, lastErrorMessage="m", lastSuccessAt=v)
    assert memory.observer_error()["last_success_at"] is None
    write_obs(mem_home, lastErrorAt=1759512180000, lastErrorMessage="m")
    assert memory.observer_error()["last_success_at"] is None


@pytest.mark.parametrize("v", [1759512180, 1759512180.0, "1759512180", "1759512180000", "2025-10-03T17:23:00Z", "2025-10-03T17:23:00+00:00",
                               "2025-10-03T17:23:00", 1759512180000])
def test_observer_error_accepts_every_time_shape(mem_home, v):
    write_obs(mem_home, lastErrorAt=v, lastErrorMessage="m")
    assert memory.observer_error()["at"] == "2025-10-03T17:23:00+00:00"


def test_observer_error_none_when_there_is_none(mem_home):
    assert memory.observer_error() is None                                     # no file
    write_obs(mem_home, consecutiveFailures=0, failingSinceAt=None, lastErrorAt=None, lastErrorMessage=None, lastErrorProvider=None,
              lastSuccessAt=1759512180000)
    assert memory.observer_error() is None
    for junk in ("{", "[]", "null", "5", '"x"'):
        (mem_home / "observer-health.json").write_text(junk)
        assert memory.observer_error() is None, junk


def test_observer_error_message_is_flattened_capped_and_typed(mem_home):
    write_obs(mem_home, lastErrorAt=1759512180000, lastErrorMessage="line one\n\tline   two " + "x" * 900, lastErrorProvider="p" * 200,
              consecutiveFailures=True)
    e = memory.observer_error()
    assert e["message"].startswith("line one line two xxx") and len(e["message"]) == memory.MESSAGE_MAX and e["message"].endswith("…")
    assert len(e["provider"]) == 60 and e["failures"] == 0 and "\n" not in e["message"]
    write_obs(mem_home, lastErrorAt="not a time", lastErrorMessage=["a"])
    assert memory.observer_error() is None                                     # nothing usable
    write_obs(mem_home, lastErrorAt=None, lastErrorMessage="late message only")
    assert memory.observer_error() == {"at": None, "message": "late message only", "provider": None, "failures": 0, "last_success_at": None}
    write_obs(mem_home, lastErrorAt=1e30, lastErrorMessage="m")
    assert memory.observer_error()["at"] is None                               # an impossible time is dropped, not raised


def test_health_carries_the_observer_error_even_when_the_worker_is_down(mem_home):
    write_pid(mem_home, pid=1, port=closed_port())
    write_obs(mem_home, lastErrorAt=1759512180000, lastErrorMessage="allowance exhausted", lastErrorProvider="claude", consecutiveFailures=1)
    h = memory.health()
    assert h["state"] == "down" and h["last_error"]["message"] == "allowance exhausted"


def test_an_unreadable_observer_file_never_fails_the_probe(mem_worker, monkeypatch):
    monkeypatch.setattr(memory, "observer_error", lambda: (_ for _ in ()).throw(OSError("denied")))
    h = memory.health()
    assert h["state"] == "up" and h["last_error"] is None


# ------------------------------------------------------------------ plugin and worker environment

def write_plugins(data):
    d = Path(settings.claude_config_dir) / "plugins"
    d.mkdir(parents=True, exist_ok=True)
    (d / "installed_plugins.json").write_text(json.dumps(data))


def write_claude_cfg(data):
    d = Path(settings.claude_config_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text(json.dumps(data))


def test_plugin_status_v2_list_and_enabled_plugins():
    assert memory.plugin_status() == {"installed": False, "version": None, "enabled": None}
    write_plugins({"version": 2, "plugins": {"claude-mem@thedotmack": [{"scope": "user", "version": "13.29.0", "installPath": "/x"}]}})
    assert memory.plugin_status() == {"installed": True, "version": "13.29.0", "enabled": None}
    write_claude_cfg({"enabledPlugins": {"claude-mem@thedotmack": True}})
    assert memory.plugin_status()["enabled"] is True
    write_claude_cfg({"enabledPlugins": {"claude-mem@thedotmack": False}})
    assert memory.plugin_status()["enabled"] is False
    write_claude_cfg({"enabledPlugins": {"other@x": True}})
    assert memory.plugin_status()["enabled"] is None


def test_plugin_status_other_shapes():
    write_plugins({"version": 2, "plugins": {"other@x": [{"version": "1"}]}})
    assert memory.plugin_status()["installed"] is False
    write_plugins({"claude-mem@thedotmack": {"version": "12.0.1", "enabled": False}})              # v1: a top-level key
    assert memory.plugin_status() == {"installed": True, "version": "12.0.1", "enabled": False}
    write_plugins({"plugins": {"claude-mem@thedotmack": []}})
    assert memory.plugin_status()["installed"] is False
    write_plugins({"plugins": {"claude-mem@thedotmack": "yes"}})
    assert memory.plugin_status()["installed"] is False
    (Path(settings.claude_config_dir) / "plugins" / "installed_plugins.json").write_text("{nope")
    assert memory.plugin_status()["installed"] is False
    write_plugins([1, 2])
    assert memory.plugin_status()["installed"] is False


def write_environ(pid, text: bytes):
    d = platform.PROC_ROOT / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "environ").write_bytes(text)


def test_env_leaks_returns_names_never_values():
    write_environ(10, b"PATH=/usr/bin\0CCBOARD_SESSION=proj--repo--s1\0TMUX_PANE=%0\0HOME=/home/u\0")
    assert memory.env_leaks(10) == ["CCBOARD_SESSION", "TMUX_PANE"]
    write_environ(11, b"PATH=/usr/bin\0TMUX_PANE=%3\0")
    assert memory.env_leaks(11) == ["TMUX_PANE"]
    write_environ(12, b"PATH=/usr/bin\0CCBOARD_SESSION_X=1\0MY_TMUX_PANE=1\0HOME=/h\0")
    assert memory.env_leaks(12) == [], "only the exact names count"
    write_environ(13, b"FOO\0BAR=\0=odd\0\0")
    assert memory.env_leaks(13) == []


def test_env_leaks_is_none_when_it_cannot_read():
    assert memory.env_leaks(99) is None                      # no such process
    write_environ(14, b"")
    assert memory.env_leaks(14) is None                      # an unreadable (or zombie) process reads as empty
    for bad in (None, 0, -1, "10", True, 1.5):
        assert memory.env_leaks(bad) is None


# ------------------------------------------------------------------ the monitor and state.memory

def test_sample_once_writes_kv_mem_health(mem_worker, tmp_path):
    db = DB(tmp_path / "m.db")
    mon = monitor.Monitor(db)
    h = mon.sample_once()
    rec = db.kv_get("mem_health")
    assert rec["value"] == h and h["state"] == "up" and h["observations"] == 9684
    mem_worker.stop()                                         # the worker goes away: the next sample replaces the record
    time.sleep(0.05)
    h2 = mon.sample_once()
    assert h2["state"] == "down" and db.kv_get("mem_health")["value"] == h2


def test_state_view_is_the_kv_value_and_never_probes(mem_worker, tmp_path, monkeypatch):
    db = DB(tmp_path / "m.db")
    assert memory.state_view(db) is None                      # nothing sampled yet
    assert mem_worker.requests == []
    db.kv_set("mem_health", {"state": "up", "version": "13.29.0"})
    assert memory.state_view(db) == {"state": "up", "version": "13.29.0"}
    assert mem_worker.requests == [], "the state never probes the worker"
    monkeypatch.setattr(settings, "claude_mem", False)
    assert memory.state_view(db) is None                      # CCBOARD_CLAUDE_MEM=0: no stale record is shown
    monkeypatch.setattr(settings, "claude_mem", True)
    db.kv_set("mem_health", "garbage")
    assert memory.state_view(db) is None


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_monitor_thread_samples_repeatedly_and_stops(tmp_path, monkeypatch):
    db = DB(tmp_path / "m.db")
    calls = []

    def fake():
        calls.append(time.monotonic())
        return {"state": "up", "n": len(calls)}
    monkeypatch.setattr(memory, "health", fake)
    mon = monitor.Monitor(db, interval=0.05, start_delay=0)
    assert mon.daemon and mon.name == "mem-monitor" and isinstance(mon.stop, threading.Event)
    mon.start()
    assert wait_for(lambda: len(calls) >= 3)
    mon.stop.set()
    mon.join(2)
    assert not mon.is_alive()
    n = len(calls)
    time.sleep(0.15)
    assert len(calls) == n, "no sample after stop"
    assert db.kv_get("mem_health")["value"]["state"] == "up"


def test_monitor_survives_a_failing_sample(tmp_path, monkeypatch):
    db = DB(tmp_path / "m.db")
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"state": "down"}
    monkeypatch.setattr(memory, "health", flaky)
    mon = monitor.Monitor(db, interval=0.05, start_delay=0)
    mon.start()
    assert wait_for(lambda: db.kv_get("mem_health") is not None)
    mon.stop.set()
    mon.join(2)
    assert len(calls) >= 2 and not mon.is_alive()


def test_monitor_stopped_during_its_start_delay_never_probes(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(memory, "health", lambda: calls.append(1) or {"state": "up"})
    mon = monitor.Monitor(DB(tmp_path / "m.db"), interval=0.05)
    assert mon.start_delay == monitor.START_DELAY == 2.0
    mon.start()
    mon.stop.set()
    mon.join(2)
    assert not mon.is_alive() and calls == []


def test_state_carries_memory(lite_client, monkeypatch):
    from app import main
    assert lite_client.get("/api/state", headers=H).json()["memory"] is None
    value = {"state": "degraded", "version": None, "port": 37700, "reason": "the port is open but /health did not answer within 2 s"}
    main.db.kv_set("mem_health", value)
    st = lite_client.get("/api/state", headers=H).json()
    assert st["memory"] == {**value, "stale_sessions": None}            # the record as stored, plus the stale-session estimate
    main.db.kv_set("mem_health", {**value, "state": "up", "active_sessions": 3})
    assert lite_client.get("/api/state", headers=H).json()["memory"]["stale_sessions"] == 3   # no live Claude session on the board
    main.db.kv_set("mem_health", {**value, "active_sessions": 0})
    assert lite_client.get("/api/state", headers=H).json()["memory"]["stale_sessions"] == 0
    monkeypatch.setattr(settings, "claude_mem", False)
    assert lite_client.get("/api/state", headers=H).json()["memory"] is None


def test_the_stale_session_estimate_never_goes_below_zero():
    from app import main
    projs = [{"repos": [{"sessions": [{"agent": "claude", "state": "working"}, {"agent": "codex", "state": "idle"},
                                      {"agent": "claude", "state": "ended"}]}],
              "root": {"sessions": [{"state": "idle"}]}, "orphan_sessions": [{"agent": "claude", "state": "done"}]}]
    assert main._live_claude_sessions(projs) == 3
    assert main._live_claude_sessions([]) == 0


def test_the_full_lifespan_runs_the_monitor_into_the_state(projects_dir, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(monitor, "START_DELAY", 0)
    monkeypatch.setattr(monitor, "INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(memory, "health", lambda: {"state": "up", "version": "9.9.9", "port": 1})
    with TestClient(main.app) as c:
        assert wait_for(lambda: (c.get("/api/state", headers=H).json().get("memory") or {}).get("version") == "9.9.9")
        assert any(t.name == "mem-monitor" and t.is_alive() for t in threading.enumerate())
    assert wait_for(lambda: not any(t.name == "mem-monitor" and t.is_alive() for t in threading.enumerate()), 3), "the lifespan stops it"


def test_the_full_lifespan_skips_the_monitor_when_claude_mem_is_off(projects_dir, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(settings, "claude_mem", False)
    monkeypatch.setattr(monitor, "START_DELAY", 0)
    monkeypatch.setattr(memory, "health", lambda: (_ for _ in ()).throw(AssertionError("probed although CCBOARD_CLAUDE_MEM=0")))
    with TestClient(main.app) as c:
        time.sleep(0.3)
        assert not any(t.name == "mem-monitor" and t.is_alive() for t in threading.enumerate())
        assert c.get("/api/state", headers=H).json()["memory"] is None
        assert main.db.kv_get("mem_health") is None


# ------------------------------------------------------------------ the doctor group against a real (fake) worker

def test_doctor_memory_group_end_to_end(mem_worker, mem_home):
    write_plugins({"version": 2, "plugins": {"claude-mem@thedotmack": [{"version": "13.29.0"}]}})
    write_claude_cfg({"enabledPlugins": {"claude-mem@thedotmack": True}})
    write_environ(4242, b"PATH=/usr/bin\0HOME=/home/u\0")                  # /health says the worker is pid 4242
    doctor.invalidate()
    out = doctor.run("memory", refresh=True)
    doctor.invalidate()
    by = {c["id"]: c for c in out["checks"]}
    assert list(by) == [i for i, _ in doctor.MEM_IDS]
    assert by["memory-plugin"]["status"] == "pass" and "13.29.0" in by["memory-plugin"]["detail"]
    assert by["memory-worker"]["status"] == "pass" and f"127.0.0.1:{mem_worker.port} (worker.pid)" in by["memory-worker"]["detail"]
    assert by["memory-queue"]["status"] == "warn" and "the observer is behind" in by["memory-queue"]["detail"]      # 435 queued
    assert by["memory-env"]["status"] == "pass"
    assert out["ok"] is True
    assert mem_worker.paths().count("/health") == 1, "one probe feeds the whole group"


def test_doctor_memory_group_with_a_dead_worker_is_warn_not_fail(mem_home):
    write_plugins({"plugins": {"claude-mem@thedotmack": [{"version": "13.29.0"}]}})
    write_pid(mem_home, pid=1, port=closed_port())
    doctor.invalidate()
    out = doctor.run("memory", refresh=True)
    doctor.invalidate()
    by = {c["id"]: c for c in out["checks"]}
    assert by["memory-worker"]["status"] == "warn" and "refused" in by["memory-worker"]["detail"]
    assert out["ok"] is True and out["summary"]["fail"] == 0


# ---------- the optional viewer link (v0.5.20, issue #9: settings and link only) ----------

def test_mem_viewer_url_is_none_unless_a_port_and_a_public_url_are_set():
    assert Settings(env={}).mem_https_port is None
    assert Settings(env={}).mem_viewer_url() is None
    assert Settings(env={"CCBOARD_MEM_HTTPS_PORT": "10443"}).mem_viewer_url() is None, "no public URL to take the host from"
    s = Settings(env={"CCBOARD_MEM_HTTPS_PORT": " 10443 ", "CCBOARD_PUBLIC_URL": "https://box.example.ts.net:8443/path?x=1"})
    assert s.mem_https_port == 10443
    assert s.mem_viewer_url() == "https://box.example.ts.net:10443/", "host and port only: no path, query or token"


@pytest.mark.parametrize("raw", ["443", "0", "65536", "-1", "abc", "8443", "8444", "10000", "1.5"])
def test_mem_https_port_refuses_443_the_board_ports_and_junk(raw):
    env = {"CCBOARD_MEM_HTTPS_PORT": raw, "CCBOARD_PUBLIC_URL": "https://box.example.ts.net", "CCBOARD_HTTPS_PORT": "8443", "CODE_HTTPS_PORT": "10000"}
    s = Settings(env=env)
    assert s.mem_https_port is None
    assert s.mem_viewer_url() is None
