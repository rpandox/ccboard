"""Issue #116 slice 7: every Linux hint the board prints, pinned byte for byte, and the same places on macOS and elsewhere.

The first half pins what the call sites in app/doctor.py and app/main.py say on Linux (written against the code before the hints moved
into app/platform.py hint()); the second half is hint() and service_manager() per system. The flags are patched, so the file gives the
same answers on any host.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import backup, doctor, main, memory, tmux
from app import platform as plat
from app.config import settings


def _system(monkeypatch, name):
    """Make the board believe it runs on `name` (linux | macos | windows | other), whatever the host is."""
    monkeypatch.setattr(plat, "IS_LINUX", name == "linux")
    monkeypatch.setattr(plat, "IS_MACOS", name == "macos")
    monkeypatch.setattr(plat, "IS_WINDOWS", name == "windows")
    plat.is_wsl.cache_clear()
    monkeypatch.setattr(plat, "OS_RELEASE", Path("/nonexistent/os-release"), raising=False)     # unreadable: Debian family, as before


@pytest.fixture
def linux(monkeypatch):
    _system(monkeypatch, "linux")


@pytest.fixture
def mac(monkeypatch):
    _system(monkeypatch, "macos")


@pytest.fixture
def elsewhere(monkeypatch):
    _system(monkeypatch, "other")


@pytest.fixture
def doc(monkeypatch):
    """The doctor with nothing real behind it: no binaries, no sockets, no network."""
    state = {"cmds": {}, "ports": set(), "http": (200, '{"healthy":true}')}

    def fake_run(argv, timeout=doctor.CMD_TIMEOUT):
        v = state["cmds"].get(Path(argv[0]).name)
        if v is None:
            raise doctor.ToolMissing(Path(argv[0]).name)
        return v

    def fake_http(url, timeout=2.0):
        if isinstance(state["http"], Exception):
            raise state["http"]
        return state["http"]
    monkeypatch.setattr(doctor, "_run", fake_run)
    monkeypatch.setattr(doctor, "_which", lambda name: None)
    monkeypatch.setattr(doctor, "_port_open", lambda host, port, t=1.0: port in state["ports"])
    monkeypatch.setattr(doctor, "_http_get", fake_http)
    monkeypatch.setattr(tmux, "server_up", lambda: False)
    return state


def _fix(outcome):
    return outcome.fix


# ---------------------------------------------------------------- Linux literals, pinned

def test_linux_missing_tool_fixes(linux, doc):
    assert doctor._missing("tmux").fix == {"text": "Install tmux 3.2 or newer", "cmd": "sudo apt-get install -y tmux"}
    assert doctor._missing("git").fix == {"text": "Install git 2.15 or newer", "cmd": "sudo apt-get install -y git"}
    assert doctor._missing("gh").fix == {"text": "Install the GitHub CLI, then log in", "cmd": "sudo apt-get install -y gh && gh auth login"}
    assert doctor._missing("ccusage").fix == {"text": "Install ccusage (needs node and npm)", "cmd": "npm install -g --prefix ~/.local ccusage"}
    assert doctor._missing("claude").fix == {"text": "Install Claude Code on the box", "cmd": "curl -fsSL https://claude.ai/install.sh | bash"}
    assert doctor._missing("ttyd").fix == {"text": "Re-run the installer: it installs ttyd", "cmd": "./install.sh"}
    assert doctor._missing("whatever").fix == {"text": "Install whatever"}


def test_linux_old_tmux_and_git_fixes(linux, doc):
    doc["cmds"]["tmux"] = doctor.Proc(0, "tmux 3.0\n", "")
    assert doctor._c_tmux(None).fix == {"text": "Upgrade tmux to 3.2 or newer", "cmd": "sudo apt-get install -y --only-upgrade tmux"}
    doc["cmds"]["git"] = doctor.Proc(0, "git version 2.10.0\n", "")
    assert doctor._c_git(None).fix == {"text": "Upgrade git to 2.15 or newer", "cmd": "sudo apt-get install -y --only-upgrade git"}


def test_linux_gh_reinstall_fix(linux, doc):
    doc["cmds"]["gh"] = doctor.Proc(1, "", "boom")
    out = doctor._c_gh(None)
    assert out.fix == {"text": "Reinstall the GitHub CLI", "cmd": "sudo apt-get install -y --reinstall gh"}


def test_linux_service_fixes(linux, doc):
    assert doctor._c_tmux_server(None).fix == {"text": "Start the tmux service that owns the sessions (never restart it while sessions run)",
                                               "cmd": "sudo systemctl start ccboard-tmux"}
    assert doctor._c_code_server(None).fix == {"text": "Start code-server", "cmd": "sudo systemctl restart code-server@$USER"}
    doc["cmds"]["ttyd"] = doctor.Proc(0, "ttyd version 1.7.7\n", "")
    doc["ports"] = set()
    import os
    exe = Path("/usr/local/bin/ttyd")
    doctor_which = doctor._which
    doctor._which = lambda name: str(exe) if name == "ttyd" else None
    try:
        assert doctor._c_ttyd(None).fix == {"text": "Restart the ttyd service", "cmd": "sudo systemctl restart ccboard-ttyd"}
    finally:
        doctor._which = doctor_which


def test_linux_ntfy_fixes(linux, doc, monkeypatch):
    monkeypatch.setattr(settings, "ntfy_url", "http://127.0.0.1:2586")
    doc["http"] = OSError("refused")
    assert doctor._c_ntfy(None).fix == {"text": "Start the ntfy server", "cmd": "sudo systemctl start ntfy"}
    doc["http"] = (503, "")
    assert doctor._c_ntfy(None).fix == {"text": "Check the ntfy service, then send a test", "cmd": "sudo journalctl -u ntfy -n 30 --no-pager",
                                        "action": "notify_test"}


def test_linux_memory_fixes(linux, monkeypatch):
    down = {"health": {"state": "down", "reason": None}}
    out = doctor._mem_worker(None, down)
    assert out.fix == {"text": "Start any Claude session: the plugin's hooks start the worker. With CLAUDE_MEM_WORKER_AUTOSTART=false only "
                               "ccboard-mem.service does: systemctl status ccboard-mem"}
    monkeypatch.setattr(memory, "env_leaks", lambda pid: ["CLAUDE_CODE_SESSION"])
    out = doctor._mem_env(None, {"health": {"state": "up", "pid": 4242}})
    assert out.fix == {"text": "Install the unit, then hand over: the plugin's `worker-service.cjs stop`, then `sudo systemctl start ccboard-mem`. "
                               "An update undoes it unless CLAUDE_MEM_WORKER_AUTOSTART=false (README, claude-mem)",
                       "cmd": "CCBOARD_MEM_SERVICE=1 ./install.sh"}


def test_linux_backup_fixes(linux, tmp_path, monkeypatch):
    now_text = "Run one now (Back up now in the bell panel, or sudo systemctl start ccboard-backup), then read journalctl -u ccboard-backup"
    timer = {"text": "Check that the nightly timer is running", "cmd": "systemctl list-timers ccboard-backup.timer --no-pager"}
    status = tmp_path / "backup-status.json"
    monkeypatch.setattr(backup, "status_path", lambda: status)
    monkeypatch.setattr(doctor, "_uptime", lambda: 10 ** 9)
    assert doctor._c_backup_last(None).fix == {"text": now_text, "cmd": "systemctl status ccboard-backup.timer --no-pager"}
    at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    status.write_text(json.dumps({"at": at, "status": "failed", "errors": ["restic: no space"]}))
    assert doctor._c_backup_last(None).fix == {"text": "Fix what it names, then run one now (Back up now in the bell panel, or sudo systemctl start "
                                                       "ccboard-backup), then read journalctl -u ccboard-backup"}
    old = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
    status.write_text(json.dumps({"at": old, "status": "ok"}))
    assert doctor._c_backup_last(None).fix == timer
    warn_old = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()
    status.write_text(json.dumps({"at": warn_old, "status": "ok"}))
    assert doctor._c_backup_last(None).fix == timer


def test_linux_main_messages(linux, monkeypatch):
    import asyncio
    resp = asyncio.run(main._down(None, tmux.TmuxDown("x")))
    assert json.loads(resp.body) == {"error": "ccboard-tmux is not running (sudo systemctl start ccboard-tmux)"}
    assert resp.status_code == 503
    monkeypatch.setattr(backup, "running", lambda: False)
    monkeypatch.setattr(backup, "start_detached", lambda: "systemd")
    assert main.api_backup_run()["log"] == "journalctl -u ccboard-backup"
    monkeypatch.setattr(backup, "start_detached", lambda: "process")
    assert main.api_backup_run()["log"] == str(settings.data_dir / backup.LOG_FILE)


# ---------------------------------------------------------------- hint() per system

def test_hint_linux_strings(linux):
    assert plat.hint("install", "tmux") == "sudo apt-get install -y tmux"
    assert plat.hint("upgrade", "tmux") == "sudo apt-get install -y --only-upgrade tmux"
    assert plat.hint("reinstall", "gh") == "sudo apt-get install -y --reinstall gh"
    assert plat.hint("start", "ccboard-tmux") == "sudo systemctl start ccboard-tmux"
    assert plat.hint("restart", "code-server@$USER") == "sudo systemctl restart code-server@$USER"
    assert plat.hint("status", "ccboard-mem", sudo=False) == "systemctl status ccboard-mem"
    assert plat.hint("status", "ccboard-backup.timer", sudo=False, flags="--no-pager") == "systemctl status ccboard-backup.timer --no-pager"
    assert plat.hint("timers", "ccboard-backup.timer") == "systemctl list-timers ccboard-backup.timer --no-pager"
    assert plat.hint("logs", "ntfy", flags="-n 30 --no-pager") == "sudo journalctl -u ntfy -n 30 --no-pager"
    assert plat.hint("logs", "ccboard-backup", sudo=False) == "journalctl -u ccboard-backup"


def test_hint_linux_other_distribution_gets_a_plain_install(monkeypatch, tmp_path):
    _system(monkeypatch, "linux")
    rel = tmp_path / "os-release"
    monkeypatch.setattr(plat, "OS_RELEASE", rel)
    rel.write_text('NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\n')
    assert plat.hint("install", "tmux") == "sudo apt-get install -y tmux"
    rel.write_text('NAME="Linux Mint"\nID=linuxmint\nID_LIKE="ubuntu debian"\n')
    assert plat.hint("install", "tmux") == "sudo apt-get install -y tmux"
    rel.write_text('NAME="Fedora"\nID=fedora\n')
    assert plat.hint("install", "tmux") == "install tmux"
    assert plat.hint("start", "ccboard-tmux") == "sudo systemctl start ccboard-tmux"       # systemd wording does not depend on the distribution


def test_hint_macos_never_names_a_linux_tool(mac):
    cases = [plat.hint(k, n) for k in ("install", "upgrade", "reinstall") for n in ("tmux", "gh")]
    cases += [plat.hint(k, n, sudo=False) for k in ("start", "restart", "status", "timers", "logs") for n in ("ccboard-tmux", "ntfy", "code-server@$USER", "ccboard-backup.timer")]
    for text in cases:
        assert "apt" not in text and "systemctl" not in text and "journalctl" not in text and "sudo" not in text, text
    assert plat.hint("install", "tmux") == "brew install tmux"
    assert plat.hint("upgrade", "git") == "brew upgrade git"
    assert plat.hint("reinstall", "gh") == "brew reinstall gh"
    assert plat.hint("start", "ccboard-tmux") == "launchctl kickstart gui/$(id -u)/dev.ccboard.tmux"
    assert plat.hint("restart", "ccboard-ttyd") == "launchctl kickstart -k gui/$(id -u)/dev.ccboard.ttyd"
    assert plat.hint("start", "ntfy") == "brew services start ntfy"
    assert plat.hint("restart", "code-server@$USER") == "launchctl kickstart -k gui/$(id -u)/dev.ccboard.code-server"


def test_hint_elsewhere_is_a_plain_sentence(elsewhere):
    assert plat.hint("install", "tmux") == "install tmux"
    assert plat.hint("start", "ccboard-tmux") == "start ccboard-tmux"
    for kind in ("install", "upgrade", "reinstall", "start", "restart", "status", "timers", "logs"):
        text = plat.hint(kind, "thing")
        assert "sudo" not in text and "apt" not in text and "systemctl" not in text and "launchctl" not in text and "brew" not in text


def test_hint_unknown_kind_is_a_programming_error(linux):
    with pytest.raises(ValueError):
        plat.hint("frobnicate", "tmux")


def test_service_manager_follows_the_runtime(monkeypatch):
    for env, want in (({"CCBOARD_RUNTIME": "docker"}, "docker"), ({"INVOCATION_ID": "abc"}, "systemd"),
                      ({"CCBOARD_RUNTIME": "launchd"}, "launchd"), ({"XPC_SERVICE_NAME": "dev.ccboard.web"}, "launchd"),
                      ({}, "none")):
        monkeypatch.setattr(plat.os, "environ", env)
        assert plat.service_manager() == want, env


def test_macos_doctor_fixes_say_brew_and_launchctl(mac, doc):
    assert doctor._missing("tmux").fix["cmd"] == "brew install tmux"
    assert doctor._missing("gh").fix["cmd"] == "brew install gh && gh auth login"
    assert doctor._c_tmux_server(None).fix["cmd"] == "launchctl kickstart gui/$(id -u)/dev.ccboard.tmux"
    assert doctor._c_code_server(None).fix["cmd"] == "launchctl kickstart -k gui/$(id -u)/dev.ccboard.code-server"
    doc["cmds"]["tmux"] = doctor.Proc(0, "tmux 3.0\n", "")
    assert doctor._c_tmux(None).fix["cmd"] == "brew upgrade tmux"


def test_macos_main_messages(mac, monkeypatch):
    import asyncio
    resp = asyncio.run(main._down(None, tmux.TmuxDown("x")))
    body = json.loads(resp.body)["error"]
    assert "systemctl" not in body and "launchctl kickstart" in body
