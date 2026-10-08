"""app/doctor.py: every check in every status, the 5 s cap, the 20 s cache, and that no secret ever reaches a detail.

Nothing here touches a real binary, the network or ~/.claude: the autouse `world` fixture patches the subprocess helper
(doctor._run), socket.create_connection (for the fake ports only), the ntfy HTTP helper, claude_auth.status, tmux.server_up
and settings.claude_bin, and starts from an all-healthy box that tests then break one piece at a time.
"""
import http.server
import json
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import doctor, hooks
from app.config import settings

REAL_RUN = doctor._run          # captured before any fixture patches it
REAL_HTTP_GET = doctor._http_get
REAL_CREATE_CONNECTION = socket.create_connection

TOKEN = "ab12cd34" * 8                                   # 64 hex characters, like hooks.ensure_token()
OAUTH = "sk-ant-oat01-SENTINELSENTINELSENTINEL"
GH_TOKEN = "gho_SENTINEL1234567890abcdefghij"
ENV_KEY = "SENTINEL-ENV-API-KEY-VALUE"
ALL_EVENTS = list(doctor.HOOK_EVENTS)
BUILTIN_IDS = {"tmux", "tmux-server", "tmux-conf", "ttyd", "attach-wrapper", "code-server", "projects-dir", "git", "gh",
               "ccusage", "identity", "samples-heartbeat", "ntfy", "push", "claude-bin", "claude-auth", "claude-hooks"}
BUILTIN_GROUPS = ["box", "claude", "notify", "terminal"]


class World:
    """A healthy ubu2, mutable. Commands are keyed by (basename, *args)."""

    def __init__(self, root: Path):
        self.root = root
        self.cmds = {
            ("tmux", "-V"): doctor.Proc(0, "tmux 3.4\n", ""),
            ("git", "--version"): doctor.Proc(0, "git version 2.43.0\n", ""),
            ("gh", "--version"): doctor.Proc(0, "gh version 2.40.1 (2024-01-01)\nhttps://github.com/cli/cli/releases/tag/v2.40.1\n", ""),
            ("gh", "auth", "status"): doctor.Proc(0, f"github.com\n  Logged in to github.com account octo (keyring)\n  - Token: {GH_TOKEN}\n", ""),
            ("ccusage", "--version"): doctor.Proc(0, "20.0.24\n", ""),
            ("claude", "--version"): doctor.Proc(0, "2.1.287 (Claude Code)\n", ""),
            ("ttyd", "--version"): doctor.Proc(0, "ttyd version 1.7.7-abcdef\n", ""),
        }
        self.run_calls: list = []
        self.ports = {7681: True, 8080: True}            # fake ports; anything else goes to the real socket layer
        self.connects: list = []
        self.which = {"ttyd": "/usr/local/bin/ttyd"}
        self.claude_exe = "/usr/local/bin/claude"
        self.tmux_up = True
        self.auth = {"installed": True, "version": "2.1.287", "loggedIn": True, "email": "someone@example.com",
                     "subscriptionType": "max", "authMethod": "claude.ai"}
        self.http: object = (200, '{"healthy":true}')
        self.http_calls: list = []

    def fake_run(self, argv, timeout=doctor.CMD_TIMEOUT):
        key = (Path(argv[0]).name, *argv[1:])
        self.run_calls.append((key, timeout))
        v = self.cmds.get(key, doctor.ToolMissing(key[0]))
        if isinstance(v, Exception):
            raise v
        return v

    def fake_connect(self, addr, timeout=None, *a, **kw):
        host, port = addr
        if port in self.ports:
            self.connects.append((host, port, timeout))
            if not self.ports[port]:
                raise ConnectionRefusedError(111, "refused")

            class S:
                closed = False

                def close(self):
                    self.closed = True
            return S()
        return REAL_CREATE_CONNECTION(addr, timeout, *a, **kw)

    def fake_http(self, url, timeout=2.0):
        self.http_calls.append((url, timeout))
        if isinstance(self.http, Exception):
            raise self.http
        return self.http


@pytest.fixture(autouse=True)
def world(projects_dir, monkeypatch, tmp_path):
    w = World(tmp_path)
    monkeypatch.setattr(doctor, "_run", w.fake_run)
    monkeypatch.setattr(doctor, "_which", lambda n: w.which.get(n))
    monkeypatch.setattr(doctor, "_http_get", w.fake_http)
    monkeypatch.setattr(doctor, "TTYD_BIN", str(tmp_path / "no-such-ttyd"))
    cs = tmp_path / "code-server-settings.json"; cs.write_text(json.dumps({"files.watcherExclude": {"**/node_modules/**": True}}))
    monkeypatch.setattr(doctor, "CODE_SERVER_SETTINGS", cs)         # the managed settings exist: the check passes
    monkeypatch.setattr(socket, "create_connection", w.fake_connect)
    monkeypatch.setattr(doctor.claude_auth, "status", lambda: w.auth)
    monkeypatch.setattr(doctor.tmux, "server_up", lambda: w.tmux_up)
    monkeypatch.setattr(settings, "claude_bin", lambda: w.claude_exe)
    monkeypatch.setattr(settings, "runtime", "docker")
    monkeypatch.setattr(settings, "ttyd_port", 7681)
    monkeypatch.setattr(settings, "code_server_port", 8080)
    monkeypatch.setattr(settings, "ntfy_url", "http://127.0.0.1:2586")
    monkeypatch.setattr(settings, "ntfy_topic", "ccboard")
    # Pin the baseline: whatever else registered at import time (app.main, later phases) must not change these tests, and
    # what a test registers is undone with the patch.
    monkeypatch.setattr(doctor, "CHECKS", [c for c in doctor.CHECKS if c[0] in BUILTIN_IDS])
    monkeypatch.setattr(doctor, "GROUPS", list(BUILTIN_GROUPS))
    monkeypatch.setattr(doctor, "PROVIDERS", [])
    # the attach wrapper, v2
    wrapper = settings.data_dir / "app" / "bin" / "ccboard-attach"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\n# ccboard-attach v2\nexec true\n")
    wrapper.chmod(0o755)
    write_claude_settings(hooks_events=ALL_EVENTS, statusline=True)
    doctor.invalidate()
    yield w
    doctor.invalidate()


def write_claude_settings(*, hooks_events=ALL_EVENTS, statusline=True, extra=None):
    d = Path(settings.claude_config_dir)
    d.mkdir(parents=True, exist_ok=True)
    data = {"hooks": {ev: [{"hooks": [{"type": "command", "command": "/home/u/.local/share/ccboard/app/bin/ccboard-hook",
                                       "async": True, "timeout": 5}]}] for ev in hooks_events}}
    if statusline is True:
        data["statusLine"] = {"type": "command", "command": "/home/u/.local/share/ccboard/app/bin/ccboard-statusline"}
    elif statusline:
        data["statusLine"] = statusline
    data.update(extra or {})
    (d / "settings.json").write_text(json.dumps(data))


class DB:
    def __init__(self, n=2):
        self.n = n

    def push_subs(self):
        return [{"endpoint": f"https://web.push.apple.com/{i}", "sub": {}} for i in range(self.n)]


def checks(group=None, **kw) -> dict:
    out = doctor.run(group, refresh=True, **kw)
    return {c["id"]: c for c in out["checks"]}


def one(check_id, **kw) -> dict:
    return checks(**kw)[check_id]


# ------------------------------------------------------------------ the whole report

def test_healthy_box_report_shape_and_summary():
    out = doctor.run(refresh=True, db=DB())
    assert set(out) == {"generated_at", "ok", "summary", "checks"}
    assert out["ok"] is True
    by = {c["id"]: c for c in out["checks"]}
    assert set(by) == BUILTIN_IDS and len(by) == len(out["checks"]) == 17      # ids are unique
    assert {c["status"] for c in out["checks"]} == {"pass", "skip"}
    assert {i for i, c in by.items() if c["status"] == "skip"} == {"tmux-conf", "samples-heartbeat"}
    assert out["summary"] == {"pass": 15, "warn": 0, "fail": 0, "skip": 2}
    for c in out["checks"]:
        assert set(c) == {"id", "group", "label", "status", "detail", "fix"}
        assert c["group"] in ("box", "claude", "notify", "terminal") and c["label"]
        assert isinstance(c["detail"], str) and "\n" not in c["detail"] and 0 < len(c["detail"]) <= doctor.DETAIL_MAX
        assert c["fix"] is None or (c["fix"]["text"] and set(c["fix"]) <= {"text", "cmd", "action"})
        if c["status"] == "pass":
            assert c["fix"] is None
    assert time.strptime(out["generated_at"][:19], "%Y-%m-%dT%H:%M:%S")


def test_groups_and_unknown_group():
    assert {c["id"] for c in doctor.run("terminal", refresh=True)["checks"]} == {"tmux", "tmux-server", "tmux-conf", "ttyd", "attach-wrapper"}
    assert {c["id"] for c in doctor.run("claude", refresh=True)["checks"]} == {"claude-bin", "claude-auth", "claude-hooks"}
    assert {c["id"] for c in doctor.run("notify", refresh=True)["checks"]} == {"ntfy", "push"}
    assert {c["id"] for c in doctor.run("box", refresh=True)["checks"]} == {
        "code-server", "projects-dir", "git", "gh", "ccusage", "identity", "samples-heartbeat"}
    with pytest.raises(ValueError):
        doctor.run("codex")
    with pytest.raises(ValueError):
        doctor.run("nope", refresh=True)


def test_summary_ok_and_fail_semantics(world):
    world.cmds[("ccusage", "--version")] = doctor.ToolMissing("ccusage")          # a fail
    world.cmds[("gh", "auth", "status")] = doctor.Proc(1, "", "not logged in")    # a warn
    out = doctor.run("box", refresh=True)
    assert out["ok"] is False and out["summary"] == {"pass": 4, "warn": 1, "fail": 1, "skip": 1}
    world.cmds[("ccusage", "--version")] = doctor.Proc(0, "20.0.24", "")
    out = doctor.run("box", refresh=True)
    assert out["ok"] is True and out["summary"]["warn"] == 1                      # a warn alone keeps ok


# ------------------------------------------------------------------ terminal

@pytest.mark.parametrize("version,status,detail", [
    ("tmux 3.4", "pass", "tmux 3.4"), ("tmux 3.3a", "pass", "tmux 3.3"), ("tmux next-3.5", "pass", "tmux 3.5"),
    ("tmux 3.2", "pass", "tmux 3.2"), ("tmux 3.1c", "fail", "older than 3.2"), ("tmux 2.9a", "fail", "older than 3.2"),
    ("tmux 10.0", "pass", "tmux 10.0"), ("what", "warn", "could not read"),
])
def test_tmux_version(world, version, status, detail):
    world.cmds[("tmux", "-V")] = doctor.Proc(0, version + "\n", "")
    c = one("tmux")
    assert c["status"] == status and detail in c["detail"]
    if status == "fail":
        assert "tmux" in c["fix"]["cmd"]


def test_tmux_missing_and_timeout_and_error_exit(world):
    world.cmds[("tmux", "-V")] = doctor.ToolMissing("tmux")
    c = one("tmux")
    assert c["status"] == "fail" and "not installed" in c["detail"] and "apt-get install" in c["fix"]["cmd"]
    world.cmds[("tmux", "-V")] = doctor.ToolTimeout("tmux")
    c = one("tmux")
    assert c["status"] == "warn" and c["detail"] == "timed out"
    world.cmds[("tmux", "-V")] = doctor.Proc(1, "", "boom")
    assert one("tmux")["status"] == "warn"


def test_tmux_server(world):
    assert one("tmux-server")["status"] == "pass"
    world.tmux_up = False
    c = one("tmux-server")
    assert c["status"] == "fail" and "ccboard-tmux" in c["fix"]["cmd"] and "ccboard" in c["detail"]


def test_tmux_conf_is_skipped_until_v057():
    c = one("tmux-conf")
    assert (c["status"], c["detail"]) == ("skip", "verified in v0.5.7")


def test_ttyd_states(world, tmp_path, monkeypatch):
    c = one("ttyd")
    assert c["status"] == "pass" and "127.0.0.1:7681" in c["detail"] and "1.7.7" in c["detail"]
    assert ("127.0.0.1", 7681, 1.0) in world.connects                       # 1 s connect timeout, loopback
    world.which.clear()
    c = one("ttyd")                                                           # listening, binary not visible (container): fine
    assert c["status"] == "pass" and "1.7.7" not in c["detail"]
    world.ports[7681] = False
    c = one("ttyd")
    assert c["status"] == "fail" and "not installed" in c["detail"] and c["fix"]["cmd"] == "./install.sh"
    world.which["ttyd"] = "/usr/local/bin/ttyd"
    c = one("ttyd")
    assert c["status"] == "fail" and "installed but nothing listens" in c["detail"] and "ccboard-ttyd" in c["fix"]["cmd"]
    world.ports[7681] = True
    world.which.clear()
    fallback = tmp_path / "ttyd-bin"                                          # the installer's fixed path counts as installed
    fallback.write_text("#!/bin/sh\n")
    fallback.chmod(0o755)
    monkeypatch.setattr(doctor, "TTYD_BIN", str(fallback))
    world.ports[7681] = False
    assert "installed but nothing listens" in one("ttyd")["detail"]


def test_ttyd_uses_the_configured_port(world, monkeypatch):
    monkeypatch.setattr(settings, "ttyd_port", 7799)
    world.ports[7799] = True
    assert "127.0.0.1:7799" in one("ttyd")["detail"]


def test_attach_wrapper_states(world):
    wrapper = settings.data_dir / "app" / "bin" / "ccboard-attach"
    c = one("attach-wrapper")
    assert c["status"] == "pass" and str(wrapper) in c["detail"]
    wrapper.write_text("#!/bin/sh\nexec tmux attach\n")                    # v1: no marker
    c = one("attach-wrapper")
    assert c["status"] == "warn" and c["detail"] == "v1 wrapper" and c["fix"]["text"]
    wrapper.chmod(0o644)
    c = one("attach-wrapper")
    assert c["status"] == "fail" and "not executable" in c["detail"] and "chmod" in c["fix"]["cmd"]
    wrapper.unlink()
    c = one("attach-wrapper")
    assert c["status"] == "fail" and "does not exist" in c["detail"]


def test_attach_wrapper_candidates_depend_on_runtime(monkeypatch):
    docker = doctor._attach_paths()
    assert docker == [settings.data_dir / "app" / "bin" / "ccboard-attach"]
    monkeypatch.setattr(settings, "runtime", "systemd")
    systemd = doctor._attach_paths()
    assert systemd[0] == docker[0] and systemd[1] == Path(doctor.__file__).resolve().parent.parent / "bin" / "ccboard-attach"
    assert systemd[1].is_file()


# ------------------------------------------------------------------ box

def test_code_server(world, monkeypatch):
    c = one("code-server")
    assert c["status"] == "pass" and ("127.0.0.1", 8080, 1.0) in world.connects
    world.ports[8080] = False
    c = one("code-server")
    assert c["status"] == "fail" and "8080" in c["detail"] and "code-server@" in c["fix"]["cmd"]
    monkeypatch.setattr(settings, "code_server_port", 10000)                 # the configured port, not a literal
    world.ports[10000] = True
    c = one("code-server")
    assert c["status"] == "pass" and "10000" in c["detail"]
    # listening but without ccboard's user settings (no watcher excludes): a warning with the merge command, never a failure
    Path(doctor.CODE_SERVER_SETTINGS).unlink()
    c = one("code-server")
    assert c["status"] == "warn" and "watcher" in c["detail"] and "code_server_settings.py" in c["fix"]["cmd"]
    Path(doctor.CODE_SERVER_SETTINGS).write_text("{not json")
    assert one("code-server")["status"] == "warn"


def test_projects_dir(projects_dir):
    assert one("projects-dir")["status"] == "pass"
    if os.geteuid() != 0:
        projects_dir.chmod(0o500)
        try:
            c = one("projects-dir")
            assert c["status"] == "fail" and "not writable" in c["detail"] and "chown" in c["fix"]["cmd"]
        finally:
            projects_dir.chmod(0o700)
    projects_dir.rmdir()
    c = one("projects-dir")
    assert c["status"] == "fail" and "not a directory" in c["detail"] and "mkdir" in c["fix"]["cmd"]


@pytest.mark.parametrize("out,status,detail", [
    ("git version 2.43.0", "pass", "git 2.43.0"), ("git version 2.15.0", "pass", "git 2.15.0"),
    ("git version 2.39.3 (Apple Git-146)", "pass", "git 2.39.3"), ("git version 2.14.6", "fail", "older than 2.15"),
    ("git version 1.8.3.1", "fail", "older than 2.15"), ("fatal: nope", "warn", "could not read"),
])
def test_git_version(world, out, status, detail):
    world.cmds[("git", "--version")] = doctor.Proc(0, out, "")
    c = one("git")
    assert c["status"] == status and detail in c["detail"]


def test_git_missing(world):
    world.cmds[("git", "--version")] = doctor.ToolMissing("git")
    c = one("git")
    assert c["status"] == "fail" and "apt-get install" in c["fix"]["cmd"]


def test_gh_states_and_auth_uses_the_exit_code_only(world):
    c = one("gh")
    assert c["status"] == "pass" and c["detail"] == "gh 2.40.1, logged in"
    assert (("gh", "auth", "status"), doctor.CMD_TIMEOUT) in world.run_calls
    world.cmds[("gh", "auth", "status")] = doctor.Proc(1, "", f"You are not logged into any GitHub hosts. token {GH_TOKEN}")
    c = one("gh")
    assert c["status"] == "warn" and "not logged in" in c["detail"] and c["fix"]["cmd"] == "gh auth login"
    assert GH_TOKEN not in json.dumps(c)
    world.cmds[("gh", "--version")] = doctor.ToolMissing("gh")
    c = one("gh")
    assert c["status"] == "fail" and "gh auth login" in c["fix"]["cmd"]
    world.cmds[("gh", "--version")] = doctor.Proc(2, "", "")
    assert one("gh")["status"] == "warn"


def test_ccusage_states(world):
    assert one("ccusage")["detail"] == "ccusage 20.0.24"
    world.cmds[("ccusage", "--version")] = doctor.Proc(1, "", "node: bad")
    assert one("ccusage")["status"] == "warn"
    world.cmds[("ccusage", "--version")] = doctor.ToolMissing("ccusage")
    c = one("ccusage")
    assert c["status"] == "fail" and "npm install" in c["fix"]["cmd"]


def test_identity_states(monkeypatch):
    c = one("identity")
    assert c["status"] == "pass" and "runtime docker" in c["detail"] and "Tailscale-User-Login" in c["detail"] and "1 allowed user" in c["detail"]
    assert "alice" not in c["detail"]                                        # a count, never the logins
    monkeypatch.setattr(settings, "allowed_users", {"a@x.io", "b@x.io"})
    assert "2 allowed users" in one("identity")["detail"]
    monkeypatch.setattr(settings, "dev_bypass_user", "dev@example.com")
    c = one("identity")
    assert c["status"] == "warn" and "bypass" in c["detail"] and "dev@example.com" not in c["detail"]
    monkeypatch.setattr(settings, "dev_bypass_user", None)
    monkeypatch.setattr(settings, "allowed_users", set())
    c = one("identity")
    assert c["status"] == "fail" and "denied" in c["detail"]
    monkeypatch.setattr(settings, "runtime", "systemd")
    monkeypatch.setattr(settings, "allowed_users", {"a@x.io"})
    assert "runtime systemd" in one("identity")["detail"]


# ------------------------------------------------------------------ samples heartbeat (v0.5.4 commit B)

HB_NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)


class HbDB:
    """Only what the check reads: kv_get('samples_heartbeat') -> {value, at} | None (the real DB.kv_get shape)."""

    def __init__(self, age_s=None, at=None, value=None):
        self.asked = []
        if at is None and age_s is not None:
            at = (HB_NOW - timedelta(seconds=age_s)).isoformat(timespec="seconds")
        self.row = None if at is None else {"value": value if value is not None else 1790000000, "at": at}

    def kv_get(self, key):
        self.asked.append(key)
        return self.row


@pytest.fixture
def clocks(monkeypatch):
    """Freeze the doctor's wall clock and its notion of how long the board has been up."""
    state = {"uptime": 3600.0}
    monkeypatch.setattr(doctor, "_utcnow", lambda: HB_NOW)
    monkeypatch.setattr(doctor, "_uptime", lambda: state["uptime"])
    return state


def heartbeat(db) -> dict:
    return one("samples-heartbeat", db=db)


@pytest.mark.parametrize("age,status", [
    (0, "pass"), (14, "pass"), (59, "pass"),
    (60, "warn"), (61, "warn"), (300, "warn"), (599, "warn"),
    (600, "fail"), (601, "fail"), (3600, "fail"), (86400, "fail"),
])
def test_samples_heartbeat_by_age(clocks, age, status):
    db = HbDB(age_s=age)
    c = heartbeat(db)
    assert c["status"] == status, (age, c)
    assert [k for k in db.asked if k != "login_problem"] == ["samples_heartbeat"], "the heartbeat check reads its one kv (the claude-auth check reads login_problem)"
    assert c["group"] == "box" and "sampler" in c["detail"]
    if status == "pass":
        assert c["fix"] is None and f"{age} s ago" in c["detail"]
    else:
        assert c["fix"]["text"] and "restart" in c["fix"]["text"].lower()
    assert "\n" not in c["detail"]


def test_samples_heartbeat_detail_reads_in_minutes_when_old(clocks):
    assert "5 min ago" in heartbeat(HbDB(age_s=300))["detail"]
    assert "2 min ago" in heartbeat(HbDB(age_s=125))["detail"]
    assert "90 s ago" in heartbeat(HbDB(age_s=90))["detail"]


def test_samples_heartbeat_absent_is_skip_right_after_start_then_fail(clocks):
    db = HbDB()
    clocks["uptime"] = 0.0
    c = heartbeat(db)
    assert c["status"] == "skip" and "just started" in c["detail"]
    clocks["uptime"] = 29.9
    assert heartbeat(db)["status"] == "skip"
    clocks["uptime"] = 30.0
    c = heartbeat(db)
    assert c["status"] == "fail" and "has not written" in c["detail"] and c["fix"]["text"]
    clocks["uptime"] = 86400.0
    assert heartbeat(db)["status"] == "fail"


def test_samples_heartbeat_a_stale_row_is_a_failure_even_when_the_board_is_young(clocks):
    clocks["uptime"] = 5.0                  # only an absent heartbeat gets the start-up grace
    assert heartbeat(HbDB(age_s=7200))["status"] == "fail"


def test_samples_heartbeat_unreadable_time_counts_as_absent(clocks):
    for bad in ("", "not a time", "2026-13-99T99:99:99"):
        db = HbDB(at=bad)
        clocks["uptime"] = 5.0
        assert heartbeat(db)["status"] == "skip"
        clocks["uptime"] = 500.0
        assert heartbeat(db)["status"] == "fail"


def test_samples_heartbeat_accepts_z_and_naive_times_and_clock_skew(clocks):
    assert heartbeat(HbDB(at="2026-10-03T11:59:50Z"))["status"] == "pass"
    assert heartbeat(HbDB(at="2026-10-03T11:59:50"))["status"] == "pass"                   # naive = UTC, like db.now() without a zone
    future = heartbeat(HbDB(at="2026-10-03T12:05:00+00:00"))                                  # a clock stepped back: never negative
    assert future["status"] == "pass" and "0 s ago" in future["detail"]


def test_samples_heartbeat_without_a_database_is_a_skip(clocks):
    assert heartbeat(None)["status"] == "skip"
    assert heartbeat(DB())["status"] == "skip"                                                # a handle with no kv_get


def test_samples_heartbeat_reads_the_real_kv_row(clocks, tmp_path, monkeypatch):
    from app.db import DB as RealDB
    db = RealDB(tmp_path / "hb.db")
    monkeypatch.setattr(doctor, "_utcnow", lambda: datetime.now(timezone.utc))                # kv_set stamps `at` with the real clock
    monkeypatch.setattr(doctor, "_uptime", lambda: 3600.0)
    assert heartbeat(db)["status"] == "fail"                                                  # nothing written yet
    db.kv_set("samples_heartbeat", time.time())
    c = heartbeat(db)
    assert c["status"] == "pass" and "sampler last ticked" in c["detail"]


def test_samples_heartbeat_never_leaks_the_value(clocks):
    c = heartbeat(HbDB(age_s=30, value="sk-ant-oat01-SENTINELSENTINEL"))
    assert "SENTINEL" not in json.dumps(c)


# ------------------------------------------------------------------ notify

def test_ntfy_not_configured_is_a_skip(world, monkeypatch):
    monkeypatch.setattr(settings, "ntfy_url", "")
    c = one("ntfy")
    assert c["status"] == "skip" and world.http_calls == []


def test_ntfy_probe_is_loopback_only(world, monkeypatch):
    c = one("ntfy")
    assert c["status"] == "pass" and "127.0.0.1:2586" in c["detail"] and "ccboard" in c["detail"]
    assert world.http_calls == [("http://127.0.0.1:2586/v1/health", 2.0)]
    for url in ("http://ntfy.example.com:2586", "https://ntfy.sh", "http://192.168.1.5:2586", "http://127.0.0.1.evil.com", "ftp://127.0.0.1", "not a url", "http://[::1"):
        world.http_calls.clear()
        monkeypatch.setattr(settings, "ntfy_url", url)
        c = one("ntfy")
        assert c["status"] == "warn" and "loopback" in c["detail"], url
        assert world.http_calls == [], url                                  # never probed
    for url, base in (("http://localhost:2586/", "http://localhost:2586"), ("http://[::1]:2586", "http://[::1]:2586"),
                      ("https://127.0.0.1", "https://127.0.0.1")):
        world.http_calls.clear()
        monkeypatch.setattr(settings, "ntfy_url", url)
        assert one("ntfy")["status"] == "pass"
        assert world.http_calls[0][0] == base + "/v1/health"


def test_ntfy_failure_modes(world):
    world.http = OSError("connection refused")
    c = one("ntfy")
    assert c["status"] == "fail" and "does not answer" in c["detail"] and "ntfy" in c["fix"]["cmd"]
    world.http = (200, '{"healthy": false}')
    assert one("ntfy")["status"] == "warn"
    world.http = (503, "")
    assert one("ntfy")["status"] == "warn"
    world.http = (404, "not found")                                          # some answer on that port: reachable
    assert one("ntfy")["status"] == "pass"
    world.http = (200, "<html>not json</html>")
    assert one("ntfy")["status"] == "pass"


def test_push_states():
    assert one("push", db=None)["status"] == "skip"
    c = one("push", db=DB(2))
    assert c["status"] == "pass" and c["detail"].startswith("2 subscriptions")
    assert one("push", db=DB(1))["detail"].startswith("1 subscription") and "subscriptions" not in one("push", db=DB(1))["detail"]
    c = one("push", db=DB(0))
    assert c["status"] == "warn" and "no device" in c["detail"] and c["fix"]["text"]

    class Broken:
        def push_subs(self):
            raise RuntimeError("db closed")
    c = one("push", db=Broken())
    assert c["status"] == "warn" and "RuntimeError" in c["detail"]


def test_push_without_pywebpush(monkeypatch):
    real = doctor.importlib.util.find_spec
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name, *a: None if name == "pywebpush" else real(name, *a))
    c = one("push", db=DB(1))
    assert c["status"] == "fail" and "requirements" in c["fix"]["cmd"]


# ------------------------------------------------------------------ claude

def test_claude_bin(world):
    c = one("claude-bin")
    assert c["status"] == "pass" and c["detail"] == "claude 2.1.287 at /usr/local/bin/claude"
    world.cmds[("claude", "--version")] = doctor.Proc(1, "", "")
    assert one("claude-bin")["status"] == "warn"
    world.cmds[("claude", "--version")] = doctor.ToolMissing("claude")
    assert one("claude-bin")["status"] == "fail"
    world.claude_exe = None
    c = one("claude-bin")
    assert c["status"] == "fail" and "not installed" in c["detail"] and "claude.ai/install.sh" in c["fix"]["cmd"]
    world.claude_exe = "/usr/local/bin/claude"
    world.cmds[("claude", "--version")] = doctor.ToolTimeout("claude")
    assert (one("claude-bin")["status"], one("claude-bin")["detail"]) == ("warn", "timed out")


def test_claude_auth(world):
    c = one("claude-auth")
    assert c["status"] == "pass" and c["detail"] == "logged in (claude.ai, max)"
    assert "someone@example.com" not in json.dumps(c)                         # no email either
    world.auth = {"installed": True, "version": "2.1.287", "loggedIn": True}
    assert one("claude-auth")["detail"] == "logged in"
    world.auth = {"installed": True, "version": "2.1.287", "loggedIn": False}
    c = one("claude-auth")
    assert c["status"] == "fail" and c["fix"]["action"] == "claude_login" and c["fix"]["cmd"] == "claude auth login"
    world.auth = {"installed": True, "version": None, "loggedIn": False, "error": "claude auth status failed: TimeoutExpired"}
    c = one("claude-auth")
    assert c["status"] == "warn" and "TimeoutExpired" in c["detail"]
    world.auth = {"installed": False, "version": None, "loggedIn": False}
    assert one("claude-auth")["status"] == "skip"


class ProblemDB:
    """Only what the claude-auth check reads: kv_get('login_problem') -> {value, at} | None."""

    def __init__(self, value=None):
        self.value = value
        self.asked = []

    def kv_get(self, key):
        self.asked.append(key)
        return {"value": self.value, "at": "2026-10-05T09:00:00+00:00"} if key == "login_problem" and self.value else None


def test_claude_auth_warns_while_the_board_has_been_told_the_login_is_invalid():
    prob = {"at": "2026-10-05T09:41:12+00:00", "agent": "claude", "account": "k", "session": "shop--api--s1", "message": "Invalid API key · Please run /login"}
    c = one("claude-auth", db=ProblemDB(prob))
    assert c["status"] == "warn" and c["detail"] == "Claude reported its login invalid at 2026-10-05 09:41 UTC: log in again in Settings"
    assert c["fix"]["action"] == "claude_login" and "Settings" in c["fix"]["text"]
    assert "Invalid API key" not in json.dumps(c), "the failure text itself is not quoted"
    assert one("claude-auth", db=ProblemDB(None))["status"] == "pass" and one("claude-auth", db=ProblemDB({**prob, "agent": "codex"}))["status"] == "pass"
    assert one("claude-auth")["status"] == "pass", "no db to ask: the plain verdict"


def test_claude_hooks_states():
    c = one("claude-hooks")
    from app.doctor import HOOK_EVENTS
    assert c["status"] == "pass" and f"{len(HOOK_EVENTS)} events" in c["detail"] and "statusLine" in c["detail"]
    # statusLine is somebody else's (claude_settings.py keeps an existing one)
    write_claude_settings(statusline={"type": "command", "command": "/usr/bin/my-statusline"})
    c = one("claude-hooks")
    assert c["status"] == "warn" and "statusLine" in c["detail"] and c["fix"]["text"]
    write_claude_settings(statusline=False)
    assert one("claude-hooks")["status"] == "warn"
    # some of our events missing
    write_claude_settings(hooks_events=["SessionStart", "Stop"])
    c = one("claude-hooks")
    assert c["status"] == "warn" and "UserPromptSubmit" in c["detail"] and "SessionStart" not in c["detail"]
    assert "claude_settings.py install" in c["fix"]["cmd"]
    # none of our hooks (somebody else's only)
    d = Path(settings.claude_config_dir)
    (d / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/other"}]}]}}))
    assert one("claude-hooks")["status"] == "fail"
    (d / "settings.json").write_text(json.dumps({"hooks": "weird", "statusLine": 5}))
    assert one("claude-hooks")["status"] == "fail"
    (d / "settings.json").write_text(json.dumps({"hooks": {"Stop": "weird", "Notification": [None, {"hooks": "x"}, {"hooks": [None]}]}}))
    assert one("claude-hooks")["status"] == "fail"
    (d / "settings.json").write_text("{not json")
    c = one("claude-hooks")
    assert c["status"] == "fail" and "not valid JSON" in c["detail"]
    (d / "settings.json").write_text("[1]")
    assert one("claude-hooks")["status"] == "fail"
    (d / "settings.json").unlink()
    c = one("claude-hooks")
    assert c["status"] == "fail" and "does not exist" in c["detail"] and "claude_settings.py install" in c["fix"]["cmd"]


# ------------------------------------------------------------------ the 5 s cap

def test_spec_constants():
    assert doctor.CHECK_TIMEOUT == 5.0 and doctor.CACHE_TTL == 20.0
    assert doctor.CMD_TIMEOUT < doctor.CHECK_TIMEOUT


def test_a_slow_check_becomes_a_timed_out_warning_without_holding_the_rest(monkeypatch):
    monkeypatch.setattr(doctor, "CHECK_TIMEOUT", 0.2)
    started = threading.Event()

    def slow(db):
        started.set()
        time.sleep(1.0)
        return doctor._pass("finally")

    doctor.register("slow-one", "slowgroup", "A slow check", slow)
    doctor.register("fast-one", "slowgroup", "A fast check", lambda db: doctor._pass("quick"))
    t0 = time.monotonic()
    out = doctor.run("slowgroup", refresh=True)
    assert time.monotonic() - t0 < 0.9
    by = {c["id"]: c for c in out["checks"]}
    assert by["slow-one"]["status"] == "warn" and by["slow-one"]["detail"] == "timed out" and by["slow-one"]["fix"] is None
    assert by["fast-one"]["status"] == "pass"
    assert [c["id"] for c in out["checks"]] == ["slow-one", "fast-one"]       # registration order, not completion order
    assert out["ok"] is True and out["summary"] == {"pass": 1, "warn": 1, "fail": 0, "skip": 0}


def test_cap_is_per_check_not_summed(monkeypatch):
    monkeypatch.setattr(doctor, "CHECK_TIMEOUT", 0.5)
    for i in range(6):
        doctor.register(f"s{i}", "parallel", f"S{i}", lambda db: (time.sleep(0.3), doctor._pass("ok"))[1])
    t0 = time.monotonic()
    out = doctor.run("parallel", refresh=True)
    assert time.monotonic() - t0 < 0.6                                        # run in parallel, not 6 x 0.3 s
    assert [c["status"] for c in out["checks"]] == ["pass"] * 6


def test_a_check_that_raises_or_returns_junk_never_breaks_the_report():
    def boom(db):
        raise RuntimeError("secret detail that must not leak: " + OAUTH)

    def missing(db):
        raise doctor.ToolMissing("ccusage")

    doctor.register("boom", "robust", "Boom", boom)
    doctor.register("missing", "robust", "Missing", missing)
    doctor.register("junk", "robust", "Junk", lambda db: ("great", "x", None))
    doctor.register("tuple", "robust", "Tuple", lambda db: ("pass", "plain tuple", {"text": "t"}))
    by = checks("robust")
    assert by["boom"]["status"] == "warn" and by["boom"]["detail"] == "check error: RuntimeError"
    assert by["missing"]["status"] == "fail" and "npm install" in by["missing"]["fix"]["cmd"]
    assert by["junk"]["status"] == "warn" and "unknown status" in by["junk"]["detail"]
    assert by["tuple"]["status"] == "pass" and by["tuple"]["fix"] == {"text": "t"}
    assert OAUTH not in json.dumps(by)


# ------------------------------------------------------------------ cache

def test_cache_serves_for_20_s_per_group_and_refresh_bypasses(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(doctor, "_clock", lambda: clock[0])
    calls = {"n": 0, "m": 0}

    def counted(db):
        calls["n"] += 1
        return doctor._pass(f"run {calls['n']}")

    def other(db):
        calls["m"] += 1
        return doctor._pass("other")

    doctor.register("counted", "cachegroup", "Counted", counted)
    doctor.register("other", "othergroup", "Other", other)
    first = doctor.run("cachegroup")
    assert calls["n"] == 1
    again = doctor.run("cachegroup")
    assert calls["n"] == 1 and again["generated_at"] == first["generated_at"] and again == first
    assert doctor.run("othergroup")["checks"][0]["detail"] == "other" and calls["m"] == 1      # keyed by group
    clock[0] += 19.9
    assert doctor.run("cachegroup")["generated_at"] == first["generated_at"] and calls["n"] == 1
    time.sleep(0.01)
    forced = doctor.run("cachegroup", refresh=True)
    assert calls["n"] == 2 and forced["generated_at"] != first["generated_at"]
    assert forced["checks"][0]["detail"] == "run 2"
    clock[0] += 19.9                                                          # refresh restarted the 20 s window
    assert doctor.run("cachegroup")["checks"][0]["detail"] == "run 2" and calls["n"] == 2
    clock[0] += 0.2
    assert doctor.run("cachegroup")["checks"][0]["detail"] == "run 3" and calls["n"] == 3
    assert calls["m"] == 1


def test_all_groups_cache_is_separate_from_a_single_group_and_db_aware():
    n = {"v": 0}

    def counted(db):
        n["v"] += 1
        return doctor._pass("db" if db is not None else "nodb")

    doctor.register("counted", "dbgroup", "Counted", counted)
    assert doctor.run("dbgroup")["checks"][0]["detail"] == "nodb"
    assert doctor.run("dbgroup", db=DB())["checks"][0]["detail"] == "db"      # a push check must not be served a db-less answer
    assert doctor.run("dbgroup")["checks"][0]["detail"] == "nodb" and n["v"] == 2
    doctor.run(refresh=True)
    before = n["v"]
    doctor.run()
    assert n["v"] == before                                                   # None is its own key


def test_cached_result_is_a_copy_callers_cannot_poison():
    first = doctor.run("claude", refresh=True)
    first["checks"].clear()
    first["ok"] = "mutated"
    again = doctor.run("claude")
    assert len(again["checks"]) == 3 and again["ok"] is True
    again["checks"][0]["status"] = "mutated"
    assert doctor.run("claude")["checks"][0]["status"] != "mutated"


# ------------------------------------------------------------------ secrets

def test_no_secret_reaches_any_detail_or_fix(world, monkeypatch):
    hooks._token = None
    token_file = settings.data_dir / "hook-token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(TOKEN + "\n")
    assert hooks.ensure_token() == TOKEN
    d = Path(settings.claude_config_dir)
    (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": OAUTH, "refreshToken": OAUTH + "R"}}))
    write_claude_settings(extra={"env": {"ANTHROPIC_API_KEY": ENV_KEY}})
    (settings.data_dir / "vapid.pem").write_text("-----BEGIN PRIVATE KEY-----\n" + "A" * 64 + "\n")
    # tools that echo secrets on every stream; auth status with a token in it; an ntfy body with one
    for key in list(world.cmds):
        world.cmds[key] = doctor.Proc(0, world.cmds[key].out + f"\nTOKEN={TOKEN}\n{OAUTH}\n{GH_TOKEN}\n", f"Authorization: Bearer {OAUTH}")
    world.auth = {**world.auth, "token": OAUTH, "accessToken": OAUTH}
    world.http = (200, json.dumps({"healthy": True, "token": TOKEN, "k": OAUTH}))
    monkeypatch.setattr(settings, "ntfy_topic", "ccboard")
    for scenario in ("healthy", "broken"):
        if scenario == "broken":
            world.ports.update({7681: False, 8080: False})
            world.tmux_up = False
            world.auth = {**world.auth, "loggedIn": False}
            world.cmds[("gh", "auth", "status")] = doctor.Proc(1, f"{GH_TOKEN} {OAUTH}", f"{TOKEN}")
            world.http = OSError(f"refused {OAUTH}")
        out = doctor.run(refresh=True, db=DB())
        blob = json.dumps(out)
        for secret in (TOKEN, OAUTH, GH_TOKEN, ENV_KEY, "Bearer", "A" * 32, "refresh"):
            assert secret not in blob, (scenario, secret)
        for c in out["checks"]:
            assert "\n" not in c["detail"]


def test_clean_redacts_secret_shapes_and_flattens():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    cases = [(OAUTH, OAUTH), (GH_TOKEN, GH_TOKEN), ("github_pat_11ABCDEFG0123456789_abcdefghij", "11ABCDEFG0123456789"),
             ("Bearer abcdef1234567890", "abcdef1234567890"), (TOKEN, TOKEN), (jwt, "dBjftJeZ4CVPmB92K27uhbUJU1p1r"),
             ("token: abcdef", "abcdef"), ("api_key=zzzzzz", "zzzzzz"), ("PASSWORD = hunter2", "hunter2")]
    for text, payload in cases:
        out = doctor._clean(f"before {text} after")
        assert payload not in out and "[redacted]" in out and out.startswith("before "), text
    assert doctor._clean("a\n b\t c\r\n d") == "a b c d"
    assert doctor._clean(None) == ""
    assert doctor._clean("x" * 500).endswith("…") and len(doctor._clean("x" * 500)) == doctor.DETAIL_MAX
    plain = "tmux 3.4, 7 events, uuid c7725076-aa65-4020-9087-97c18a3d922b, /usr/local/bin/claude"
    assert doctor._clean(plain) == plain                                      # ordinary details pass through untouched


def test_a_leaky_check_is_scrubbed_by_the_runner():
    doctor.register("leaky", "leakgroup", "Leaky", lambda db: doctor._fail(f"oops {OAUTH}\nline two {TOKEN}", doctor.fix(f"use {GH_TOKEN}", "echo hi")))
    c = checks("leakgroup")["leaky"]
    blob = json.dumps(c)
    assert OAUTH not in blob and TOKEN not in blob and GH_TOKEN not in blob
    assert c["fix"]["cmd"] == "echo hi" and "\n" not in c["detail"]


def test_doctor_never_reads_the_hook_token_file(monkeypatch):
    opened = []
    real_open = open

    def spy(file, *a, **kw):
        opened.append(str(file))
        return real_open(file, *a, **kw)
    monkeypatch.setattr("builtins.open", spy)
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **kw: opened.append(str(self)) or "{}")
    doctor.run(refresh=True, db=DB())
    assert not [p for p in opened if p.endswith("hook-token") or p.endswith(".credentials.json") or p.endswith("vapid.pem")]


# ------------------------------------------------------------------ the helpers themselves

def test_real_run_helper_maps_errors():
    p = REAL_RUN([sys.executable, "-c", "import sys; print('out'); sys.stderr.write('err'); sys.exit(3)"])
    assert (p.rc, p.out.strip(), p.err) == (3, "out", "err")
    with pytest.raises(doctor.ToolMissing) as e:
        REAL_RUN(["/definitely/not/a/binary-xyz", "--version"])
    assert e.value.name == "binary-xyz"
    t0 = time.monotonic()
    with pytest.raises(doctor.ToolTimeout):
        REAL_RUN([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3)
    assert time.monotonic() - t0 < 3
    notexec = Path(settings.data_dir) / "not-exec"
    notexec.parent.mkdir(parents=True, exist_ok=True)
    notexec.write_text("#!/bin/sh\n")
    notexec.chmod(0o644)
    with pytest.raises(doctor.ToolMissing):
        REAL_RUN([str(notexec)])


def test_port_open_closes_the_socket_and_maps_refusal(world):
    assert doctor._port_open("127.0.0.1", 7681, 1.0) is True
    world.ports[7681] = False
    assert doctor._port_open("127.0.0.1", 7681, 1.0) is False
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert doctor._port_open("127.0.0.1", port, 1.0) is True            # the real socket layer (port not faked)
    finally:
        srv.close()
    assert doctor._port_open("127.0.0.1", port, 0.5) is False


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/v1/health":
            body = b'{"healthy":true}'
            self.send_response(200)
        elif self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/never")
            body = b""
        else:
            self.send_response(404)
            body = b"nope"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_http_get_helper_against_a_loopback_server(monkeypatch):
    real_http = REAL_HTTP_GET
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")               # a proxy in the environment must be ignored
        assert real_http(base + "/v1/health", 2.0) == (200, '{"healthy":true}')
        assert real_http(base + "/nothing", 2.0) == (404, "")                 # an HTTP error is an answer
        assert real_http(base + "/redir", 2.0)[0] == 302                      # redirects are not followed
    finally:
        srv.shutdown()
        srv.server_close()
    with pytest.raises(OSError):
        real_http(base + "/v1/health", 0.5)


def test_check_dataclass_and_fix_helper():
    c = doctor.Check("x", "box", "X", "pass", "fine")
    assert c.to_dict() == {"id": "x", "group": "box", "label": "X", "status": "pass", "detail": "fine", "fix": None}
    assert doctor.fix("t") == {"text": "t"}
    assert doctor.fix("t", "cmd") == {"text": "t", "cmd": "cmd"}
    assert doctor.fix("t", None, "act") == {"text": "t", "action": "act"}
    assert doctor._ver("tmux next-3.5") == (3, 5) and doctor._ver("nothing") is None and doctor._vstr("ttyd version 1.7.7-x") == "1.7.7"


def test_register_replaces_by_id_and_adds_groups():
    n0 = len(doctor.CHECKS)
    doctor.register("again", "newgroup", "One", lambda db: doctor._pass("1"))
    doctor.register("again", "newgroup", "Two", lambda db: doctor._pass("2"))
    assert len(doctor.CHECKS) == n0 + 1 and "newgroup" in doctor.GROUPS
    c = doctor.run("newgroup", refresh=True)["checks"]
    assert [(x["label"], x["detail"]) for x in c] == [("Two", "2")]


# ------------------------------------------------------------------ providers (Agent.doctor_checks() shape)

class Foreign:
    """Stands in for app.agents.base.Check: same fields, different class."""

    def __init__(self, id, group, label, status, detail="", fix=None):
        self.id, self.group, self.label, self.status, self.detail, self.fix = id, group, label, status, detail, fix


def test_provider_checks_are_merged_scrubbed_and_override_same_id(world):
    calls = {"n": 0}

    def provider(db):
        calls["n"] += 1
        return [Foreign("codex-bin", "codex", "Codex CLI", "pass", f"codex 0.145.0 {OAUTH}"),
                doctor.Check("codex-auth", "codex", "Codex login", "fail", "not logged in", doctor.fix("Log in", "codex login", "codex_login")),
                {"id": "claude-bin", "group": "claude", "label": "Claude Code binary", "status": "warn", "detail": "from the provider"},
                Foreign("odd", "codex", "Odd", "great", "x")]

    doctor.register_provider("codex", "codex", provider)
    assert "codex" in doctor.GROUPS
    out = doctor.run("codex", refresh=True, db=DB())
    by = {c["id"]: c for c in out["checks"]}
    assert set(by) == {"codex-bin", "codex-auth", "odd"}                    # claude-bin belongs to another group: not asked for
    assert by["codex-bin"]["status"] == "pass" and OAUTH not in json.dumps(by["codex-bin"])
    assert by["codex-auth"]["fix"] == {"text": "Log in", "cmd": "codex login", "action": "codex_login"}
    assert by["odd"]["status"] == "warn" and "unknown status" in by["odd"]["detail"]
    assert out["ok"] is False and out["summary"] == {"pass": 1, "warn": 1, "fail": 1, "skip": 0}
    # the all-groups report carries them too, and a same-id provider check replaces the built-in one in place
    allc = doctor.run(refresh=True, db=DB())["checks"]
    ids = [c["id"] for c in allc]
    assert len(ids) == len(set(ids)) and {"codex-bin", "codex-auth"} <= set(ids)
    assert next(c for c in allc if c["id"] == "claude-bin")["detail"] == "from the provider"
    assert ids.index("claude-bin") < ids.index("codex-bin")
    assert calls["n"] == 2


def test_provider_timeout_and_crash_become_single_warnings(monkeypatch):
    monkeypatch.setattr(doctor, "CHECK_TIMEOUT", 0.2)
    doctor.register_provider("slowprov", "provgroup", lambda db: (time.sleep(1.0), [])[1])
    doctor.register_provider("crashprov", "provgroup2", lambda db: 1 / 0)
    doctor.register_provider("toolprov", "provgroup3", lambda db: (_ for _ in ()).throw(doctor.ToolTimeout("x")))
    t0 = time.monotonic()
    c1 = doctor.run("provgroup", refresh=True)["checks"]
    assert time.monotonic() - t0 < 0.9
    assert [(c["id"], c["status"], c["detail"]) for c in c1] == [("slowprov", "warn", "timed out")]
    c2 = doctor.run("provgroup2", refresh=True)["checks"]
    assert [(c["id"], c["status"], c["detail"]) for c in c2] == [("crashprov", "warn", "check error: ZeroDivisionError")]
    c3 = doctor.run("provgroup3", refresh=True)["checks"]
    assert [(c["status"], c["detail"]) for c in c3] == [("warn", "timed out")]


def test_register_provider_replaces_by_name():
    doctor.register_provider("p", "pgroup", lambda db: [doctor.Check("a", "pgroup", "A", "pass", "one")])
    doctor.register_provider("p", "pgroup", lambda db: [doctor.Check("a", "pgroup", "A", "pass", "two")])
    assert len([p for p in doctor.PROVIDERS if p[0] == "p"]) == 1
    assert doctor.run("pgroup", refresh=True)["checks"][0]["detail"] == "two"


def test_app_dir_prefers_the_seeded_copy_and_falls_back_to_the_checkout(monkeypatch, tmp_path):
    seeded = settings.data_dir / "app"
    assert doctor._app_dir() == seeded                                        # docker runtime
    monkeypatch.setattr(settings, "runtime", "systemd")
    assert doctor._app_dir() == seeded                                        # seeded bin/ exists (the fixture made it)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "empty-data")
    assert doctor._app_dir() == doctor.CHECKOUT
    c = one("claude-hooks")
    assert c["status"] == "pass"
    (Path(settings.claude_config_dir) / "settings.json").unlink()
    assert str(doctor.CHECKOUT / "scripts" / "claude_settings.py") in one("claude-hooks")["fix"]["cmd"]


def test_hook_events_match_the_installer():
    """doctor.HOOK_EVENTS is what the doctor expects in settings.json; scripts/claude_settings.py EVENTS is what install writes."""
    import importlib.util, pathlib
    from app import doctor
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "claude_settings.py"
    spec = importlib.util.spec_from_file_location("claude_settings_for_doctor", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    assert set(mod.EVENTS) == set(doctor.HOOK_EVENTS), (sorted(set(mod.EVENTS) ^ set(doctor.HOOK_EVENTS)))


# ------------------------------------------------------------------ claude-mem: the `memory` group (v0.5.10)
#
# One provider (doctor.memory_checks) runs seven checks off ONE probe. Here memory.health() is canned, the plugin files and /proc are
# temp files, HOME is a temp dir (so ~/.bun is never looked at); the real probe against a fake worker is in tests/test_memory.py.

IMPORT_PROVIDERS = [p[0] for p in doctor.PROVIDERS]       # captured at import, before the autouse `world` fixture empties them
IMPORT_GROUPS = list(doctor.GROUPS)
MEM_BEHIND = "the observer is behind: it shares your Claude subscription window"
MEM_NOW = datetime(2026, 10, 4, 6, 0, 0, tzinfo=timezone.utc)
MEM_HEALTHY = {"state": "up", "version": "13.29.0", "port": 37700, "port_source": "worker.pid", "pid": 4242, "observations": 9684,
               "sessions": 270, "summaries": 170, "db_size": 123456789, "queue_depth": 3, "processing": False, "active_sessions": 1,
               "last_error": None, "reason": None, "at": "2026-10-04T06:00:00+00:00"}


class Mem:
    def __init__(self, tmp_path):
        self.health = dict(MEM_HEALTHY)
        self.calls = 0
        self.proc = tmp_path / "proc"

    def environ(self, pid, text: bytes):
        (self.proc / str(pid)).mkdir(parents=True, exist_ok=True)
        (self.proc / str(pid) / "environ").write_bytes(text)

    def plugin(self, installed=True, enabled=True):
        d = Path(settings.claude_config_dir) / "plugins"
        d.mkdir(parents=True, exist_ok=True)
        plugins = {"claude-mem@thedotmack": [{"scope": "user", "version": "13.29.0"}]} if installed else {}
        (d / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": plugins}))
        write_claude_settings(extra={"enabledPlugins": {"claude-mem@thedotmack": enabled}} if enabled is not None else None)


@pytest.fixture
def mem(world, monkeypatch, tmp_path):
    """The memory group on a healthy box: plugin installed and enabled, worker up with a clean environment, nothing queued."""
    from copy import deepcopy
    m = Mem(tmp_path)

    def fake_health(*a, **k):
        m.calls += 1
        return deepcopy(m.health)
    monkeypatch.setattr(doctor.memory, "health", fake_health)
    monkeypatch.setattr(doctor.memory, "PROC_ROOT", m.proc)
    monkeypatch.setattr(settings, "claude_mem", True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for var in ("CCBOARD_MEM_BUN", "BUN", "BUN_PATH", "BUN_INSTALL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CCBOARD_MEM_BUN_DIRS", "")                                 # a dev box may have a bun in /usr/local/bin
    monkeypatch.setattr(doctor, "PROVIDERS", [("memory", "memory", doctor.memory_checks)])
    monkeypatch.setattr(doctor, "GROUPS", BUILTIN_GROUPS + ["memory"])
    monkeypatch.setattr(doctor, "_utcnow", lambda: MEM_NOW)
    m.sample = (200, json.loads((Path(__file__).parent / "fixtures" / "claude_mem_observations.json").read_text()))
    monkeypatch.setattr(doctor, "_mem_api_sample", lambda: m.sample if not isinstance(m.sample, Exception) else (_ for _ in ()).throw(m.sample))
    m.plugin()
    m.environ(4242, b"PATH=/usr/bin\0HOME=/home/u\0")
    return m


def mem_report():
    doctor.invalidate()
    out = doctor.run("memory", refresh=True)
    doctor.invalidate()
    return out, {c["id"]: c for c in out["checks"]}


def mc(check_id):
    return mem_report()[1][check_id]


def test_memory_group_is_registered_at_import():
    assert "memory" in IMPORT_PROVIDERS and "memory" in IMPORT_GROUPS
    assert [i for i, _ in doctor.MEM_IDS] == ["memory-plugin", "memory-worker", "memory-queue", "memory-error", "memory-projects",
                                              "memory-env", "memory-bun", "memory-api"]          # memory-api (issue #20) appended last


def test_memory_healthy_box_passes_every_check_off_one_probe(mem):
    out, by = mem_report()
    assert list(by) == [i for i, _ in doctor.MEM_IDS]
    assert {c["status"] for c in out["checks"]} == {"pass"} and out["summary"] == {"pass": 8, "warn": 0, "fail": 0, "skip": 0}
    assert out["ok"] is True and {c["group"] for c in out["checks"]} == {"memory"}
    assert mem.calls == 1, "eight checks, one probe of the worker (memory-api adds one limit=1 read)"
    assert by["memory-worker"]["detail"] == "claude-mem 13.29.0 is up on 127.0.0.1:37700 (worker.pid), 9,684 observations"
    assert by["memory-plugin"]["detail"] == "claude-mem 13.29.0 installed and enabled"
    for c in out["checks"]:
        assert c["fix"] is None and 0 < len(c["detail"]) <= doctor.DETAIL_MAX


def test_memory_group_is_not_part_of_the_builtin_groups(mem, monkeypatch):
    monkeypatch.setattr(doctor, "PROVIDERS", [])
    monkeypatch.setattr(doctor, "GROUPS", list(BUILTIN_GROUPS))
    doctor.invalidate()
    assert not any(c["group"] == "memory" for c in doctor.run(refresh=True)["checks"])


def test_memory_off_skips_every_check_and_never_probes(mem, monkeypatch):
    monkeypatch.setattr(settings, "claude_mem", False)
    out, by = mem_report()
    assert {c["status"] for c in out["checks"]} == {"skip"} and len(by) == 8
    assert all("CCBOARD_CLAUDE_MEM=0" in c["detail"] for c in out["checks"]) and mem.calls == 0 and out["ok"] is True


def test_memory_plugin_missing_warns_and_the_rest_skip(mem):
    mem.plugin(installed=False)
    out, by = mem_report()
    assert by["memory-plugin"]["status"] == "warn"
    assert by["memory-plugin"]["fix"]["cmd"] == ("claude plugin marketplace add thedotmack/claude-mem && "
                                                 "claude plugin install claude-mem@thedotmack")
    assert {c["status"] for i, c in by.items() if i != "memory-plugin"} == {"skip"}
    assert mem.calls == 0, "nothing to probe without the plugin"
    assert out["ok"] is True
    (Path(settings.claude_config_dir) / "plugins" / "installed_plugins.json").unlink()
    assert mc("memory-plugin")["status"] == "warn"                                 # no file at all


def test_memory_plugin_disabled_and_enable_state_unknown(mem):
    mem.plugin(enabled=False)
    c = mc("memory-plugin")
    assert c["status"] == "warn" and "disabled" in c["detail"] and c["fix"]["cmd"] == "claude plugin enable claude-mem@thedotmack"
    mem.plugin(enabled=None)
    c = mc("memory-plugin")
    assert c["status"] == "pass" and c["detail"] == "claude-mem 13.29.0 installed"


def test_memory_worker_states(mem):
    c = mc("memory-worker")
    assert c["status"] == "pass"
    mem.health.update(state="degraded", reason="the port is open but /health did not answer within 2 s")
    c = mc("memory-worker")
    assert c["status"] == "warn" and "127.0.0.1:37700 (worker.pid)" in c["detail"] and "did not answer within 2 s" in c["detail"] and c["fix"]
    mem.health.update(state="down", reason="connection refused (worker.pid is stale or the worker stopped)")
    c = mc("memory-worker")
    assert c["status"] == "warn", "down is a hint, never a failure: the worker starts with the first session"
    assert "no worker on 127.0.0.1:37700 (worker.pid)" in c["detail"] and "stale" in c["detail"] and "first" not in c["detail"]
    assert "plugin's hooks start the worker" in c["fix"]["text"]
    assert "CLAUDE_MEM_WORKER_AUTOSTART=false" in c["fix"]["text"] and "ccboard-mem" in c["fix"]["text"], "with that setting only the unit starts one"
    out, _ = mem_report()
    assert out["ok"] is True and out["summary"]["fail"] == 0


@pytest.mark.parametrize("source,label", [("env", "CCBOARD_MEM_PORT"), ("worker.pid", "worker.pid"), ("settings", "settings.json"), ("default", "default")])
def test_memory_worker_names_where_the_port_came_from(mem, source, label):
    mem.health.update(port=37701, port_source=source)
    assert f"127.0.0.1:37701 ({label})" in mc("memory-worker")["detail"]


def test_memory_worker_up_without_numbers(mem):
    mem.health.update(version=None, observations=None)
    assert mc("memory-worker")["detail"] == "claude-mem is up on 127.0.0.1:37700 (worker.pid)"


@pytest.mark.parametrize("depth,status", [(0, "pass"), (1, "pass"), (200, "pass"), (201, "warn"), (435, "warn")])
def test_memory_queue_threshold_is_above_200(mem, depth, status):
    mem.health.update(queue_depth=depth, processing=True)
    c = mc("memory-queue")
    assert c["status"] == status
    if status == "warn":
        assert MEM_BEHIND in c["detail"] and f"{depth:,}" in c["detail"] and c["fix"]
    else:
        assert c["fix"] is None and "queued" in c["detail"]


def test_memory_queue_other_states(mem):
    mem.health.update(queue_depth=None)
    assert mc("memory-queue")["status"] == "pass"
    mem.health.update(state="down", queue_depth=None)
    assert mc("memory-queue")["status"] == "skip"
    mem.health.update(state="degraded")
    assert mc("memory-queue")["status"] == "skip"
    mem.health.update(state="up", queue_depth=1, processing=False)
    assert mc("memory-queue")["detail"] == "1 observation queued, idle"


def err(minutes_ago, **kw):
    at = (MEM_NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    return {"at": at, "message": "Provider reported the inference allowance exhausted", "provider": "claude", "failures": 0, **kw}


def test_memory_error_within_an_hour_warns(mem):
    mem.health["last_error"] = err(23)
    c = mc("memory-error")
    assert c["status"] == "warn"
    assert c["detail"] == "the observer's provider failed 23 min ago (claude): Provider reported the inference allowance exhausted"
    assert c["fix"]
    mem.health["last_error"] = err(0.5)
    assert mc("memory-error")["detail"].startswith("the observer's provider failed 30 s ago")
    mem.health["last_error"] = err(59, failures=4)
    assert "and is still failing" in mc("memory-error")["detail"]
    mem.health["last_error"] = err(-5)                                              # a clock a little ahead is "just now", not an error
    assert mc("memory-error")["status"] == "warn"


def ok_at(minutes_ago):
    return (MEM_NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def test_memory_error_the_observer_got_past_is_recovered_not_a_warning(mem):
    """The real shape on a healthy box: lastErrorAt older than lastSuccessAt and consecutiveFailures 0 (23 min ago: still inside the hour)."""
    mem.health["last_error"] = err(23, last_success_at=ok_at(10))
    c = mc("memory-error")
    assert c["status"] == "pass" and c["fix"] is None
    assert c["detail"] == "the observer's provider failed 23 min ago (claude) and has recovered (a success followed)"
    assert "Provider reported" not in c["detail"], "a recovered error does not repeat its message"
    mem.health["last_error"] = err(23, last_success_at=ok_at(23))              # a success at the error's own time is not earlier than it
    assert mc("memory-error")["status"] == "pass"
    mem.health["last_error"] = err(23, last_success_at=ok_at(10).replace("+00:00", "Z"))
    assert mc("memory-error")["status"] == "pass"
    mem.health["last_error"] = err(23, last_success_at=ok_at(10).replace("+00:00", ""))   # a naive time is read as UTC
    assert mc("memory-error")["status"] == "pass"
    out, by = mem_report()
    assert out["ok"] is True and by["memory-error"]["status"] == "pass"


def test_memory_error_still_warns_without_a_later_success_or_with_failures_counted(mem):
    mem.health["last_error"] = err(23, last_success_at=ok_at(10), failures=2)  # a success after the error, but it is failing again now
    c = mc("memory-error")
    assert c["status"] == "warn" and "and is still failing" in c["detail"] and "Provider reported" in c["detail"] and c["fix"]
    mem.health["last_error"] = err(23, last_success_at=ok_at(40))              # the last success is earlier than the error
    assert mc("memory-error")["status"] == "warn"
    mem.health["last_error"] = err(23, last_success_at=None)                   # no success on record
    assert mc("memory-error")["status"] == "warn"
    mem.health["last_error"] = err(23)                                         # no key at all (an older stored value)
    assert mc("memory-error")["status"] == "warn"
    mem.health["last_error"] = err(23, last_success_at="garbage")
    assert mc("memory-error")["status"] == "warn"


def test_memory_error_older_than_an_hour_or_absent_passes(mem):
    mem.health["last_error"] = err(61)
    c = mc("memory-error")
    assert c["status"] == "pass" and c["detail"] == "the last observer provider error was 1 h 1 min ago (claude)"
    mem.health["last_error"] = err(60 * 30)
    assert mc("memory-error")["status"] == "pass"
    mem.health["last_error"] = None
    assert mc("memory-error")["detail"] == "no observer provider error recorded"
    mem.health["last_error"] = {"at": None, "message": "x", "provider": None, "failures": 0}
    assert mc("memory-error")["status"] == "pass"
    mem.health["last_error"] = {"at": "garbage", "message": "x", "provider": None, "failures": 0}
    assert mc("memory-error")["status"] == "pass"


def test_memory_error_is_read_even_when_the_worker_is_down(mem):
    mem.health.update(state="down", last_error=err(5))
    assert mc("memory-error")["status"] == "warn"


def test_memory_error_message_is_scrubbed_like_every_detail(mem):
    mem.health["last_error"] = err(5, message="auth failed token=" + "ab" * 40 + " for sk-ant-oat01-SENTINELSENTINEL")
    c = mc("memory-error")
    assert "ab" * 20 not in c["detail"] and "sk-ant-oat01-SENTINEL" not in c["detail"] and "[redacted]" in c["detail"]


def make_repos(projects_dir, layout):
    for project, repos in layout.items():
        for r in repos:
            (projects_dir / project / r / ".git").mkdir(parents=True)


def test_memory_projects_unique_basenames_pass(mem, projects_dir):
    make_repos(projects_dir, {"acme": ["api", "web"], "petroit": ["backend"]})
    (projects_dir / "solo" / ".git").mkdir(parents=True)                           # a project folder that is itself the repo
    c = mc("memory-projects")
    assert c["status"] == "pass" and c["fix"] is None


def test_memory_projects_shared_basename_warns_and_names_both(mem, projects_dir):
    make_repos(projects_dir, {"acme": ["api", "web"], "petroit": ["api"], "zed": ["web"]})
    c = mc("memory-projects")
    assert c["status"] == "warn"
    assert "'api' = acme/api and petroit/api" in c["detail"] and "'web' = acme/web and zed/web" in c["detail"]
    assert "memories merge" in c["detail"] and c["fix"]


def test_memory_projects_a_single_repo_project_collides_with_a_repo_elsewhere(mem, projects_dir):
    (projects_dir / "shop" / ".git").mkdir(parents=True)                           # the repo is `shop` (folder = repo)
    make_repos(projects_dir, {"acme": ["shop"]})
    c = mc("memory-projects")
    assert c["status"] == "warn" and "'shop' = acme/shop and shop" in c["detail"]


def test_memory_projects_hint_carries_the_project_environments_snippet(mem, projects_dir):
    make_repos(projects_dir, {"acme": ["api"], "petroit": ["api"]})
    c = mc("memory-projects")
    cmd = c["fix"]["cmd"]
    assert cmd.startswith('"CLAUDE_MEM_PROJECT_ENVIRONMENTS": ')
    entries = json.loads(cmd.split(": ", 1)[1])
    assert entries == [{"name": "acme-api", "patterns": [f"{projects_dir}/acme/api/**"]},
                       {"name": "petroit-api", "patterns": [f"{projects_dir}/petroit/api/**"]}]
    shape = json.loads((Path(__file__).parent / "fixtures" / "claude_mem_project_environments.json").read_text())["value"][0]
    assert all(set(e) == set(shape) for e in entries), "the plugin's own shape (box check V11 row 13)"
    assert "ccboard" not in cmd and "settings.json" in c["fix"]["text"]


def test_memory_api_pass_untested_unknown_and_drift(mem):
    c = mc("memory-api")
    assert c["status"] == "pass" and "13.31.0" in c["detail"]
    mem.health["version"] = "13.34.2"
    c = mc("memory-api")
    assert c["status"] == "warn" and "newer than 13.31.0" in c["detail"]
    assert c["fix"]["text"] == "the Memory page may be wrong until the board is updated; report it"
    mem.health["version"] = "14.0.0"
    assert mc("memory-api")["status"] == "warn" and "13.x line" in mc("memory-api")["detail"]
    mem.health["version"] = None
    assert mc("memory-api")["status"] == "warn"
    mem.health["version"] = "13.31.0"
    mem.sample = (200, {"items": [{"id": 1, "createdAt": 5}]})
    c = mc("memory-api")
    assert c["status"] == "warn" and "shape" in c["detail"] and "created_at_epoch" in c["detail"]
    mem.sample = (500, None)
    assert "HTTP 500" in mc("memory-api")["detail"]
    mem.sample = OSError("boom")
    assert mc("memory-api")["status"] == "warn"
    mem.health["state"] = "down"
    assert mc("memory-api")["status"] == "skip"


def test_memory_projects_more_than_two_collisions_are_counted(mem, projects_dir):
    make_repos(projects_dir, {"a": ["x", "y", "z"], "b": ["x", "y", "z"]})
    c = mc("memory-projects")
    assert c["status"] == "warn" and "(+1 more)" in c["detail"]


def test_memory_projects_ignores_hidden_and_non_directories(mem, projects_dir):
    make_repos(projects_dir, {"a": ["x"]})
    (projects_dir / "b").mkdir()
    (projects_dir / "b" / ".x").mkdir()
    (projects_dir / "b" / "x").write_text("a file, not a repo")
    assert mc("memory-projects")["status"] == "pass"


def test_memory_projects_needs_the_directory(mem, projects_dir):
    import shutil
    shutil.rmtree(projects_dir)
    assert mc("memory-projects")["status"] == "skip"


def test_memory_env_inherited_session_variables_warn_with_the_hint(mem):
    mem.environ(4242, b"PATH=/usr/bin\0CCBOARD_SESSION=SENTINEL-SESSION-VALUE\0TMUX_PANE=%0\0CLAUDECODE=1\0")
    out, by = mem_report()
    c = by["memory-env"]
    assert c["status"] == "warn"
    assert "the memory worker inherited a session's environment; install ccboard-mem.service" in c["detail"]
    assert "CCBOARD_SESSION, TMUX_PANE" in c["detail"] and c["fix"]["cmd"] == "CCBOARD_MEM_SERVICE=1 ./install.sh"
    ft = c["fix"]["text"]
    assert "worker-service.cjs stop" in ft and "sudo systemctl start ccboard-mem" in ft and "CLAUDE_MEM_WORKER_AUTOSTART=false" in ft
    assert len(ft) < doctor.DETAIL_MAX - 1, "the fix text is capped like a detail: nothing of the recovery is cut off"
    assert "SENTINEL-SESSION-VALUE" not in json.dumps(out), "variable names only, never values"
    mem.environ(4242, b"PATH=/usr/bin\0TMUX_PANE=%0\0")
    assert mc("memory-env")["detail"].endswith("(it holds TMUX_PANE)")


def test_memory_env_is_fail_soft(mem):
    mem.health["pid"] = 31337                                                       # no /proc/31337
    c = mc("memory-env")
    assert c["status"] == "skip" and "could not read" in c["detail"]
    mem.health["pid"] = None
    assert mc("memory-env")["status"] == "skip"
    mem.environ(4242, b"")
    mem.health["pid"] = 4242
    assert mc("memory-env")["status"] == "skip"
    mem.environ(4242, b"PATH=/x\0")
    assert mc("memory-env")["status"] == "pass"


def test_memory_env_is_checked_for_a_degraded_worker_too(mem):
    mem.health.update(state="degraded")
    mem.environ(4242, b"TMUX_PANE=%1\0")
    assert mc("memory-env")["status"] == "warn"


def make_bun(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


def test_memory_bun_found_says_the_worker_runs_on_it(mem, world):
    world.which["bun"] = "/usr/local/bin/bun"
    c = mc("memory-bun")
    assert c["status"] == "pass" and c["fix"] is None
    assert c["detail"] == "bun found at /usr/local/bin/bun; the plugin's worker runs on it"
    assert "does not need" not in c["detail"] and "bundles" not in c["detail"], "the plugin needs bun (13.29.0 exits 1: Bun not found)"


def test_memory_bun_found_in_the_home_dir(mem, tmp_path):
    exe = make_bun(tmp_path / "home" / ".bun" / "bin" / "bun")
    assert mc("memory-bun")["detail"] == f"bun found at {exe}; the plugin's worker runs on it"


def test_memory_bun_a_file_that_is_not_executable_is_not_a_bun(mem, tmp_path):
    f = tmp_path / "home" / ".bun" / "bin" / "bun"
    f.parent.mkdir(parents=True)
    f.write_text("#!/bin/sh\n")                                                  # mode 644
    assert mc("memory-bun")["status"] == "pass" and "not visible from here" in mc("memory-bun")["detail"]
    mem.health.update(state="down")
    assert mc("memory-bun")["status"] == "warn"


def test_memory_bun_looks_where_the_launcher_and_the_plugin_look(mem, tmp_path, monkeypatch):
    own, env_bun, bun_path = (make_bun(tmp_path / "b" / n / "bun") for n in ("own", "env", "path"))
    install = tmp_path / "b" / "install"
    make_bun(install / "bin" / "bun")
    sysdir = tmp_path / "b" / "sys"
    make_bun(sysdir / "bun")

    def found():
        return mc("memory-bun")["detail"].split(" found at ")[1].split(";")[0]

    monkeypatch.setenv("CCBOARD_MEM_BUN", own)
    monkeypatch.setenv("BUN", env_bun)
    monkeypatch.setenv("BUN_PATH", bun_path)
    assert found() == own
    monkeypatch.delenv("CCBOARD_MEM_BUN")
    assert found() == env_bun
    monkeypatch.delenv("BUN")
    assert found() == bun_path
    monkeypatch.delenv("BUN_PATH")
    monkeypatch.setenv("BUN_INSTALL", str(install))
    assert found() == str(install / "bin" / "bun")
    (install / "bin" / "bun").unlink()
    make_bun(install / "bun")
    assert found() == str(install / "bun")
    monkeypatch.delenv("BUN_INSTALL")
    monkeypatch.setenv("CCBOARD_MEM_BUN_DIRS", f"/nonexistent {sysdir}")           # a set list replaces the default one, as in the launcher
    assert found() == str(sysdir / "bun")
    monkeypatch.delenv("CCBOARD_MEM_BUN_DIRS")
    monkeypatch.setattr(doctor, "MEM_BUN_DIRS", (str(sysdir),))                    # unset: the default list
    assert found() == str(sysdir / "bun")


def test_memory_bun_default_dirs_are_the_launchers(mem):
    src = (Path(__file__).resolve().parents[1] / "bin" / "ccboard-mem-run").read_text()
    m = re.search(r"CCBOARD_MEM_BUN_DIRS-([^}]+)\}", src)
    assert m and tuple(m.group(1).split()) == doctor.MEM_BUN_DIRS


def test_memory_bun_not_found_with_a_worker_running_is_still_a_pass(mem):
    for state in ("up", "degraded"):
        mem.health.update(state=state)
        c = mc("memory-bun")
        assert c["status"] == "pass" and c["fix"] is None
        assert c["detail"].startswith("bun is not visible from here, but a worker is running, so the plugin found its own")
        assert "bundles" not in c["detail"] and "nothing to do" not in c["detail"]


def test_memory_bun_not_found_and_no_worker_warns_and_names_where_it_looked(mem, monkeypatch):
    monkeypatch.delenv("CCBOARD_MEM_BUN_DIRS")                                     # the default directory list ...
    monkeypatch.setattr(doctor, "_executable", lambda p: False)                    # ... with no bun in any of it
    mem.health.update(state="down", reason="connection refused (no worker.pid: the worker starts with the first Claude session)")
    out, by = mem_report()
    c = by["memory-bun"]
    assert c["status"] == "warn" and out["ok"] is True, "a hint, never a failure"
    assert c["detail"].startswith("bun not found and no worker is running, so hooks cannot start one; looked in ~/.bun/bin, ")
    for place in ("/usr/local/bin", "/opt/homebrew/bin", "/home/linuxbrew/.linuxbrew/bin", "/usr/bin", "/snap/bin"):
        assert place in c["detail"], place
    assert c["detail"].endswith(", PATH")
    assert "https://bun.sh" in c["fix"]["text"] and "BUN" in c["fix"]["text"] and "installs none" in c["fix"]["text"]
    assert "bundles" not in c["detail"] + c["fix"]["text"]


def test_memory_bun_warning_names_the_directories_actually_searched(mem, monkeypatch):
    mem.health.update(state="down")
    monkeypatch.setenv("CCBOARD_MEM_BUN_DIRS", "/opt/mine /srv/tools")
    assert "~/.bun/bin, /opt/mine, /srv/tools, PATH" in mc("memory-bun")["detail"]


def test_memory_a_check_that_raises_does_not_take_the_group_down(mem, monkeypatch):
    def boom(db, ctx):
        raise RuntimeError("bug")
    monkeypatch.setitem(doctor._MEM_FNS, "memory-queue", boom)
    out, by = mem_report()
    assert by["memory-queue"]["status"] == "warn" and by["memory-queue"]["detail"] == "check error: RuntimeError"
    assert by["memory-worker"]["status"] == "pass" and by["memory-bun"]["status"] == "pass" and len(by) == 8


def test_memory_group_filter_and_caching(mem):
    doctor.invalidate()
    a = doctor.run("memory")
    b = doctor.run("memory")
    assert a == b and mem.calls == 1, "cached for 20 s like every group"
    with pytest.raises(ValueError):
        doctor.run("nope")
    doctor.invalidate()


# ------------------------------------------------------------------ v0.5.19: every failing check says what to do; the fix kinds the Doctor page draws

import builtins as _builtins  # noqa: E402
open_real = _builtins.open
FIX_ACTIONS = {"claude_login", "codex_login", "notify_test"}      # what fix.action may be: the page maps each to a button (a hyphenated spelling is accepted there)


def test_a_broken_box_gives_every_warning_and_failure_a_fix_with_words(world, monkeypatch, tmp_path):
    world.cmds[("tmux", "-V")] = doctor.Proc(0, "tmux 3.0\n", "")
    world.cmds[("git", "--version")] = doctor.ToolMissing("git")
    world.cmds[("gh", "--version")] = doctor.ToolMissing("gh")
    world.cmds[("ccusage", "--version")] = doctor.ToolMissing("ccusage")
    world.ports = {7681: False, 8080: False}
    world.tmux_up = False
    world.auth = {"installed": True, "loggedIn": False}
    world.http = OSError("refused")
    monkeypatch.setattr(settings, "projects_dir", tmp_path / "gone")
    monkeypatch.setattr(settings, "allowed_users", set())
    (Path(settings.claude_config_dir) / "settings.json").unlink()
    out = doctor.run(refresh=True, db=DB(0))
    bad = [c for c in out["checks"] if c["status"] in ("warn", "fail")]
    assert len(bad) >= 12, [c["id"] for c in bad]
    for c in bad:
        assert c["fix"] and c["fix"]["text"], f"{c['id']} ({c['status']}) has no fix text"
        assert set(c["fix"]) <= {"text", "cmd", "action"}
        assert c["fix"].get("action", "claude_login") in FIX_ACTIONS
    assert all(c["fix"] is None for c in out["checks"] if c["status"] == "pass")


def test_unreadable_wrapper_and_unreadable_subscriptions_carry_a_fix(world, monkeypatch):
    monkeypatch.setattr(doctor.os, "access", lambda p, mode: False if str(p).endswith("ccboard-attach") and mode == os.R_OK else True)

    def unreadable(*a, **k):
        raise OSError("denied")
    monkeypatch.setattr("builtins.open", lambda p, *a, **k: unreadable() if str(p).endswith("ccboard-attach") else open_real(p, *a, **k))
    c = one("attach-wrapper")
    assert c["status"] == "warn" and "not readable" in c["detail"] and "chmod" in c["fix"]["cmd"]

    class Broken:
        def push_subs(self):
            raise RuntimeError("db closed")
    c = one("push", db=Broken())
    assert c["status"] == "warn" and c["fix"]["text"]


def test_the_notify_test_action_sits_on_the_ntfy_warnings_only(world, monkeypatch):
    world.http = (200, '{"healthy": false}')
    c = one("ntfy")
    assert c["status"] == "warn" and c["fix"]["action"] == "notify_test" and "journalctl" in c["fix"]["cmd"]
    monkeypatch.setattr(settings, "ntfy_url", "https://ntfy.sh")
    c = one("ntfy")
    assert c["status"] == "warn" and "loopback" in c["detail"] and c["fix"]["action"] == "notify_test"
    world.http = (200, '{"healthy": true}')
    monkeypatch.setattr(settings, "ntfy_url", "http://127.0.0.1:2586")
    assert one("ntfy")["fix"] is None                                       # a pass has nothing to fix


def test_the_claude_login_action_is_on_both_login_failures(world):
    world.auth = {"installed": True, "loggedIn": False}
    c = one("claude-auth")
    assert c["status"] == "fail" and c["fix"]["action"] == "claude_login" and c["fix"]["cmd"] == "claude auth login"


def test_tmux_conf_stays_a_skip_with_no_fix_to_offer():
    c = one("tmux-conf")
    assert c["status"] == "skip" and c["fix"] is None


def test_codex_saved_models_warns_only_against_a_catalogue_codex_answered(monkeypatch):
    """#17: a Codex schedule naming a model the live catalogue no longer lists is a warning with the fix "pick another model"; an unread
    catalogue (only the built-in fallback) is a skip, never a failure; Claude schedules are not judged."""
    from app import agents

    class Jobs:
        def jobs(self):
            return [{"id": 1, "name": "nightly", "agent": "codex", "opts": {"model": "gpt-5.5"}},
                    {"id": 2, "name": "review", "agent": "codex", "opts": {"model": "gpt-6-sol"}},
                    {"id": 3, "name": "claude one", "agent": "claude", "opts": {"model": "opus"}},
                    {"id": 4, "name": "default", "agent": "codex", "opts": {}}]

    ag = agents.get("codex")
    monkeypatch.setattr(type(ag), "live_models", lambda self: None)
    st, detail, f = doctor._c_codex_saved_models(Jobs())
    assert st == "skip" and "not been read" in detail and f is None
    monkeypatch.setattr(type(ag), "live_models", lambda self: [{"slug": "gpt-6.1-sol"}, {"slug": "gpt-6-sol"}])
    st, detail, f = doctor._c_codex_saved_models(Jobs())
    assert st == "warn" and "nightly (gpt-5.5)" in detail and "review" not in detail and "claude" not in detail
    assert f["text"] == "pick another model"
    monkeypatch.setattr(type(ag), "live_models", lambda self: [{"slug": "gpt-5.5"}, {"slug": "gpt-6-sol"}])
    assert doctor._c_codex_saved_models(Jobs())[0] == "pass"
