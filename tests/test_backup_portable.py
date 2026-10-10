"""Issue #129: the nightly backup without systemd. The launchd job's schedule parser, "Back up now" per runtime (systemd argv unchanged,
launchd kickstart without sudo, the detached process otherwise), the restic repository classifier, the doctor's backup-job row and the log hint.

Fakes only: `sudo`, `systemctl` and `launchctl` are shell scripts on a temp PATH that write what they were asked to a log file; restic is the fake
of tests/test_backup.py; every path is under tmp_path and HOME is the sandbox. Nothing here touches launchd, a real repository or a remote.
"""
import importlib.util
import json
import os
import plistlib
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import backup, doctor, main
from app import platform as plat
from app.config import settings
from tests.test_backup import H, backup_env  # noqa: F401  (backup_env is a fixture)

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "scripts" / "macos_tools.py"
_spec = importlib.util.spec_from_file_location("macos_tools", TOOLS)
mt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mt)
NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)

pytestmark = pytest.mark.posix_sh


# ---------------------------------------------------------------- fakes


def _script(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def tools(tmp_path, monkeypatch):
    """A temp PATH holding only fakes. Returns a helper: tools.add(name) installs a fake; tools.calls() is what the fakes were asked, in order.
    `loaded` and `kick_fail` files in tools.state switch the fake launchctl's answers."""
    bin_dir, state = tmp_path / "fakebin", tmp_path / "fakestate"
    bin_dir.mkdir()
    state.mkdir()
    log = state / "calls.log"
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_STATE", str(state))

    class T:
        pass
    t = T()
    t.state = state

    def add(name):
        body = {
            "sudo": 'printf "sudo %s\\n" "$*" >> "$FAKE_LOG"\nexit "${FAKE_SUDO_RC:-0}"\n',
            "systemctl": 'printf "systemctl %s\\n" "$*" >> "$FAKE_LOG"\ncase "$1" in is-active) echo "${FAKE_TIMER:-active}"; [ "${FAKE_TIMER:-active}" = active ] || exit 3;; esac\nexit 0\n',
            "launchctl": 'printf "launchctl %s\\n" "$*" >> "$FAKE_LOG"\ncase "$1" in\n'
                         '  print) [ -f "$FAKE_STATE/loaded" ] || { echo "Could not find service" >&2; exit 113; } ;;\n'
                         '  kickstart) [ -f "$FAKE_STATE/kick_fail" ] && { echo "kickstart failed" >&2; exit 1; } ;;\nesac\nexit 0\n',
        }[name]
        _script(bin_dir / name, body)

    t.add = add
    t.calls = lambda: log.read_text().splitlines() if log.exists() else []
    return t


@pytest.fixture
def spawned(monkeypatch):
    out = []
    monkeypatch.setattr(backup.plat, "spawn_detached", lambda argv, logf, **kw: out.append(argv))
    return out


@pytest.fixture
def system(tmp_path, monkeypatch, projects_dir):
    """Not a Mac, no systemd unit, a board run by a hand: each test sets what it needs."""
    monkeypatch.setattr(plat, "IS_MACOS", False)
    monkeypatch.setattr(plat, "IS_LINUX", True)
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(settings, "runtime", "host")
    monkeypatch.setattr(backup, "SYSTEMD_UNIT", tmp_path / "no-such-unit.service")
    monkeypatch.delenv("CCBOARD_LAUNCHD_DOMAIN", raising=False)


def _unit(tmp_path, monkeypatch):
    unit = tmp_path / "ccboard-backup.service"
    unit.write_text("[Unit]\n")
    monkeypatch.setattr(backup, "SYSTEMD_UNIT", unit)
    return unit


def _mac(monkeypatch):
    monkeypatch.setattr(plat, "IS_MACOS", True)
    monkeypatch.setattr(plat, "IS_LINUX", False)
    monkeypatch.setattr(settings, "runtime", "launchd")


# ---------------------------------------------------------------- start_detached per runtime


def test_systemd_path_runs_the_old_sudo_argv_and_returns_systemd(tools, system, tmp_path, monkeypatch, spawned):
    _unit(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "runtime", "systemd")
    for n in ("sudo", "systemctl", "launchctl"):
        tools.add(n)
    (tools.state / "loaded").touch()
    assert backup.start_detached() == "systemd"
    assert tools.calls() == ["sudo -n systemctl start --no-block ccboard-backup.service"], "exactly the argv of before; launchctl is never asked on Linux"
    assert spawned == []


def test_systemd_refused_falls_back_to_the_detached_process_and_still_skips_launchd(tools, system, tmp_path, monkeypatch, spawned):
    _unit(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_SUDO_RC", "1")
    for n in ("sudo", "systemctl", "launchctl"):
        tools.add(n)
    (tools.state / "loaded").touch()
    assert backup.start_detached() == "process"
    assert tools.calls() == ["sudo -n systemctl start --no-block ccboard-backup.service"]
    assert spawned and spawned[0][-2:] == ["-m", "app.backup"]


def test_launchd_job_loaded_is_kickstarted_without_sudo_and_without_k(tools, system, monkeypatch, spawned):
    _mac(monkeypatch)
    for n in ("sudo", "systemctl", "launchctl"):
        tools.add(n)
    (tools.state / "loaded").touch()
    target = plat.launchd_target("backup")
    assert target == f"gui/{os.getuid()}/dev.ccboard.backup"
    assert backup.start_detached() == "launchd"
    assert tools.calls() == [f"launchctl print {target}", f"launchctl kickstart {target}"]
    assert not [c for c in tools.calls() if c.startswith(("sudo", "systemctl"))] and " -k" not in " ".join(tools.calls())
    assert spawned == []


def test_launchd_user_domain_names_the_user_target(tools, system, monkeypatch):
    _mac(monkeypatch)
    monkeypatch.setenv("CCBOARD_LAUNCHD_DOMAIN", "user")
    tools.add("launchctl")
    (tools.state / "loaded").touch()
    assert backup.start_detached() == "launchd"
    assert tools.calls()[-1] == f"launchctl kickstart user/{os.getuid()}/dev.ccboard.backup"


def test_launchd_job_not_loaded_runs_the_detached_process(tools, system, monkeypatch, spawned):
    _mac(monkeypatch)
    tools.add("launchctl")
    assert backup.start_detached() == "process"
    assert [c.split()[1] for c in tools.calls()] == ["print"], "a job that is not loaded is not kickstarted"
    assert spawned and spawned[0][-2:] == ["-m", "app.backup"]


def test_launchd_kickstart_failure_falls_back_to_the_detached_process(tools, system, monkeypatch, spawned):
    _mac(monkeypatch)
    tools.add("launchctl")
    (tools.state / "loaded").touch()
    (tools.state / "kick_fail").touch()
    assert backup.start_detached() == "process"
    assert [c.split()[1] for c in tools.calls()] == ["print", "kickstart"] and spawned


def test_no_launchctl_and_no_systemd_runs_the_detached_process(tools, system, monkeypatch, spawned):
    _mac(monkeypatch)
    assert backup.start_detached() == "process"
    assert tools.calls() == [] and spawned


def test_a_container_never_asks_launchctl_or_sudo(tools, system, tmp_path, monkeypatch, spawned):
    _unit(tmp_path, monkeypatch)
    _mac(monkeypatch)
    monkeypatch.setattr(settings, "runtime", "docker")
    for n in ("sudo", "systemctl", "launchctl"):
        tools.add(n)
    (tools.state / "loaded").touch()
    assert backup.start_detached() == "process"
    assert tools.calls() == [] and spawned


def test_a_board_started_by_hand_never_touches_the_launchd_job(tools, system, monkeypatch, spawned):
    _mac(monkeypatch)
    monkeypatch.setattr(settings, "runtime", "host")
    tools.add("launchctl")
    (tools.state / "loaded").touch()
    assert backup.start_detached() == "process"
    assert tools.calls() == []


def test_linux_never_asks_launchctl_even_when_one_is_on_the_path(tools, system, monkeypatch, spawned):
    tools.add("launchctl")
    (tools.state / "loaded").touch()
    assert backup.start_detached() == "process"
    assert tools.calls() == []


def test_launchd_loaded_answers_true_false_or_none(tools, system, monkeypatch):
    assert plat.launchd_loaded("backup") is None, "no launchctl: cannot be asked"
    tools.add("launchctl")
    assert plat.launchd_loaded("backup") is False
    (tools.state / "loaded").touch()
    assert plat.launchd_loaded("backup") is True
    monkeypatch.setattr(plat, "current_uid", lambda: None)
    assert plat.launchd_loaded("backup") is None, "no uid, no domain"


def test_the_backup_label_is_a_known_launchd_job():
    assert "backup" in plat.LAUNCHD_JOBS and plat.launchd_label("backup") == "dev.ccboard.backup"
    assert plat.launchd_job("ccboard-backup") == "backup" and plat.launchd_job("ccboard-backup.timer") is None


def test_the_macos_hints_for_the_backup_unit_name_the_launchd_job(monkeypatch):
    monkeypatch.setattr(plat, "_hint_family", lambda: "macos")
    assert plat.hint("start", "ccboard-backup") == "launchctl kickstart gui/$(id -u)/dev.ccboard.backup"
    assert plat.hint("logs", "ccboard-backup", sudo=False) == "tail -n 30 ~/Library/Logs/ccboard/backup.log"


# ---------------------------------------------------------------- /api/backup/run: the answer, the log hint, the 409


@pytest.mark.parametrize("via,want", [("systemd", "journalctl -u ccboard-backup"), ("launchd", "Library/Logs/ccboard/backup.log"),
                                       ("process", "backup.log")])
def test_the_run_answer_carries_the_log_hint_of_the_mechanism(via, want, monkeypatch, projects_dir):
    monkeypatch.setattr(plat, "_hint_family", lambda: "linux")
    monkeypatch.setattr(backup, "running", lambda: False)
    monkeypatch.setattr(backup, "start_detached", lambda: via)
    out = main.api_backup_run()
    assert out["started"] is True and out["via"] == via
    if via == "systemd":
        assert out["log"] == want
    elif via == "launchd":
        assert out["log"] == str(Path(os.path.expanduser("~")) / "Library" / "Logs" / "ccboard" / "backup.log") and out["log"].endswith(want)
    else:
        assert out["log"] == str(settings.data_dir / backup.LOG_FILE)


def test_a_second_start_while_the_lock_is_held_answers_409_whatever_the_mechanism(client, monkeypatch):
    called = []
    monkeypatch.setattr(backup, "start_detached", lambda: called.append(1) or "launchd")
    held = backup.try_lock()
    try:
        r = client.post("/api/backup/run", headers=H)
    finally:
        held.close()
    assert r.status_code == 409 and "already running" in r.json()["detail"] and called == []
    assert client.post("/api/backup/run", headers=H).status_code == 202 and called == [1]


# ---------------------------------------------------------------- restic_is_local


LOCAL = ["/var/backups/ccb", "/Volumes/backup/ccb", "/srv/restic", "/home/x/.local/share/ccboard/restic", "/mnt/c/restic", "/"]
REMOTE = ["sftp:host:/p", "sftp:bk@nas:/srv/restic", "sftp://bk@nas:2222/srv/restic", "rclone:remote:path", "s3:https://host/bucket", "s3:s3.amazonaws.com/b",
          "rest:https://host/", "b2:bucket:path", "azure:container:/p", "gs:bucket:/p", "swift:container:/p"]
REFUSED = ["backups", "./backups", "../backups", "~/backups", "restic-repo", "local:/p", "https://host/repo", "S3:bucket", "relative/dir", "x"]


@pytest.mark.parametrize("spec", LOCAL)
def test_absolute_paths_are_local(spec, monkeypatch):
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    assert backup.restic_is_local(spec) is True


@pytest.mark.parametrize("spec", REMOTE)
def test_known_remote_prefixes_are_remote(spec):
    assert backup.restic_is_local(spec) is False


def test_linux_answers_are_those_of_the_old_rule_for_every_spec_in_use_today():
    """The old rule: a spec starting with / is a local folder (created), everything else is handed to restic as a remote. For the specs the
    README documents the classifier agrees; the strings it now refuses are the ones restic could not have opened anyway."""
    for spec in LOCAL:
        assert backup.restic_is_local(spec) is spec.startswith("/")
    for spec in REMOTE:
        assert backup.restic_is_local(spec) is spec.startswith("/")
    assert backup.restic_is_local(str(settings.data_dir / "restic")) is True, "the default repository"


@pytest.mark.parametrize("spec", REFUSED)
def test_relative_paths_and_other_strings_are_refused_with_a_reason(spec):
    with pytest.raises(ValueError) as e:
        backup.restic_is_local(spec)
    msg = str(e.value)
    assert "absolute path" in msg and "sftp:" in msg and "rclone:" in msg and "off" in msg, "names the accepted forms"


def test_empty_keeps_the_default_and_off_is_off():
    assert backup.restic_is_local("") is True and backup.restic_is_local("   ") is True
    for off in ("off", "OFF", "none", "0"):
        assert backup.restic_is_local(off) is False


@pytest.mark.parametrize("spec", ["C:\\backups", "C:/backups", "d:\\x\\y", "C:backups"])
def test_a_windows_drive_form_is_local_only_where_the_windows_flag_is_set(spec, monkeypatch):
    monkeypatch.setattr(plat, "IS_WINDOWS", True)
    assert backup.restic_is_local(spec) is True
    for linux, mac in ((True, False), (False, True)):
        monkeypatch.setattr(plat, "IS_WINDOWS", False)
        monkeypatch.setattr(plat, "IS_LINUX", linux)
        monkeypatch.setattr(plat, "IS_MACOS", mac)
        with pytest.raises(ValueError) as e:
            backup.restic_is_local(spec)
        assert "native Windows is not supported" in str(e.value) and "#124" in str(e.value) and "WSL2" in str(e.value)


def test_a_url_with_a_password_is_not_echoed_in_the_refusal():
    with pytest.raises(ValueError) as e:
        backup.restic_is_local("https://user:hunter2@host/repo")
    assert "hunter2" not in str(e.value) and "host" not in str(e.value)


# ---------------------------------------------------------------- the run: a refused repository never reaches restic


@pytest.mark.parametrize("spec", ["backups", "~/backups", "C:\\backups"])
def test_a_refused_repository_stops_the_run_before_restic_and_shows_in_the_status(backup_env, monkeypatch, spec):  # noqa: F811
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(settings, "restic_repo", spec)
    st = backup.run(push=False)
    assert st["status"] == "failed" and len(st["errors"]) == 1 and st["errors"][0].startswith("restic: the restic repository setting")
    assert not (backup_env["repo"] / "calls.log").exists(), "fake restic was never started"
    assert not (Path.cwd() / spec).exists(), "no folder was made from the refused setting"
    assert json.loads(backup.status_path().read_text())["errors"] == st["errors"], "the refusal is in the record the Backup status line shows"


def test_a_local_repository_is_still_created_and_a_remote_is_not(backup_env, monkeypatch):  # noqa: F811
    st = backup.run(push=False)                      # the fixture's repository is an absolute path that does not exist yet
    assert st["status"] == "ok" and backup_env["repo"].is_dir() and (backup_env["repo"] / "calls.log").exists()
    created = []
    monkeypatch.setattr(backup.Path, "mkdir", lambda self, *a, **kw: created.append(str(self)))
    monkeypatch.setattr(settings, "restic_repo", "sftp:bk@nas:/srv/restic")
    monkeypatch.setattr(backup, "_restic", lambda args, env, timeout=3600: subprocess.CompletedProcess(args, 0, "", ""))
    backup.restic_backup([Path("/x")])
    assert not [c for c in created if "srv/restic" in c], "a remote spec never makes a folder"


# ---------------------------------------------------------------- doctor: backup-job


def _check():
    return next(c for c in doctor.CHECKS if c[0] == "backup-job")


def test_backup_job_is_registered_in_the_box_group():
    cid, group, label, fn = _check()
    assert (cid, group) == ("backup-job", "box") and fn is doctor._c_backup_job and label


class Mgr:
    """The service manager the doctor asks (launchctl print, systemctl is-active) through its own _run seam."""
    def __init__(self):
        self.loaded = True
        self.calls = []
        self.raise_missing = False

    def __call__(self, argv, timeout=4.0):
        self.calls.append(argv)
        if self.raise_missing:
            raise doctor.ToolMissing(Path(argv[0]).name)
        if argv[0] == "launchctl":
            return doctor.Proc(0 if self.loaded else 113, "state = waiting\n" if self.loaded else "", "")
        return doctor.Proc(0 if self.loaded else 3, "active\n" if self.loaded else "inactive\n", "")


@pytest.fixture
def dj(tmp_path, monkeypatch, projects_dir):
    """The doctor's backup-job row on a Mac with the job loaded, restic on the job's PATH and a fresh ok run; tests break one piece."""
    mgr = Mgr()
    monkeypatch.setattr(doctor, "_run", mgr)
    monkeypatch.setattr(doctor, "_utcnow", lambda: NOW)
    monkeypatch.setattr(doctor.plat, "_hint_family", lambda: "linux")
    monkeypatch.setattr(plat, "IS_MACOS", True)
    monkeypatch.setattr(plat, "IS_LINUX", False)
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(plat, "is_wsl", lambda: False)
    monkeypatch.setattr(settings, "runtime", "launchd")
    monkeypatch.setattr(settings, "restic_repo", str(tmp_path / "restic-repo"))
    rbin = tmp_path / "jobbin"
    rbin.mkdir()
    _script(rbin / "restic", "exit 0\n")

    class D:
        pass
    d = D()
    d.mgr, d.rbin, d.tmp = mgr, rbin, tmp_path

    def plist(path_dirs):
        p = Path.home() / "Library" / "LaunchAgents" / "dev.ccboard.backup.plist"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(plistlib.dumps({"Label": "dev.ccboard.backup", "EnvironmentVariables": {"PATH": ":".join(path_dirs)}}))
    d.plist = plist

    def record(hours_ago=1.0, status="ok", **extra):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        (settings.data_dir / "backup-status.json").write_text(
            json.dumps({"at": (NOW - timedelta(hours=hours_ago)).isoformat(), "status": status, "errors": [], "warnings": [], "push": [], **extra}))
    d.record = record
    d.run = lambda: doctor._c_backup_job(None)
    plist([str(rbin), "/usr/bin"])
    record()
    return d


def test_launchd_job_loaded_with_restic_and_a_fresh_run_passes(dj):
    out = dj.run()
    assert out.status == "pass" and "dev.ccboard.backup is loaded" in out.detail and "last run ok 1 h" in out.detail and "restic is on the job's PATH" in out.detail
    assert dj.mgr.calls == [["launchctl", "print", plat.launchd_target("backup")]]


def test_launchd_job_not_loaded_warns_with_the_bootstrap_fix(dj):
    dj.mgr.loaded = False
    out = dj.run()
    assert out.status == "warn" and "dev.ccboard.backup is not loaded" in out.detail
    assert out.fix["cmd"] == f"launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.ccboard.backup.plist" and "CCBOARD_BACKUP=0" in out.fix["text"]


def test_a_loaded_job_whose_last_run_is_old_warns_and_a_fresh_or_missing_record_does_not(dj):
    dj.record(hours_ago=80)
    out = dj.run()
    assert out.status == "warn" and "last run is 3 d old" in out.detail and "next wake" in out.detail
    dj.record(hours_ago=30)
    assert dj.run().status == "pass", "an old-ish run is the Last backup row's business; this row warns only when the run is stale for days"
    (settings.data_dir / "backup-status.json").unlink()
    out = dj.run()
    assert out.status == "pass" and "last run unknown" in out.detail, "unknown is shown as unknown, not as a failure"
    (settings.data_dir / "backup-status.json").write_text("{not json")
    assert "last run unknown" in dj.run().detail


def test_restic_missing_from_the_jobs_path_warns_even_when_the_board_finds_it(dj, monkeypatch):
    dj.plist(["/usr/bin", "/bin"])
    monkeypatch.setenv("PATH", f"{dj.rbin}{os.pathsep}{os.environ['PATH']}")        # the board itself could run restic: the job could not
    out = dj.run()
    assert out.status == "warn" and "restic is not on the job's PATH" in out.detail
    assert "rerun the installer" in out.fix["text"] and out.fix["cmd"] == "sudo apt-get install -y restic", "the hint is the system's own words (Linux words here)"
    monkeypatch.setattr(doctor.plat, "_hint_family", lambda: "macos")
    assert dj.run().fix["cmd"] == "brew install restic"


def test_an_unreadable_job_file_shows_the_path_as_unknown(dj):
    (Path.home() / "Library" / "LaunchAgents" / "dev.ccboard.backup.plist").unlink()
    out = dj.run()
    assert out.status == "pass" and "restic on the job's PATH unknown" in out.detail


def test_restic_off_skips_the_path_question(dj, monkeypatch):
    monkeypatch.setattr(settings, "restic_repo", "off")
    dj.plist(["/usr/bin"])
    out = dj.run()
    assert out.status == "pass" and "restic" not in out.detail


def test_a_refused_repository_setting_fails_with_the_reason(dj, monkeypatch):
    monkeypatch.setattr(settings, "restic_repo", "backups")
    out = dj.run()
    assert out.status == "fail" and "neither an absolute path nor a remote" in out.detail and "backups" not in out.fix["text"]


def test_the_ssh_agent_note_appears_only_after_a_push_failure_on_a_mac(dj, monkeypatch):
    dj.record(push=[{"repo": "a/b", "pushed": [], "rejected": [], "error": "git push: Permission denied (publickey)"}])
    out = dj.run()
    assert out.status == "warn" and "ssh-agent" in out.detail and "ssh-add --apple-use-keychain" in out.fix["text"]
    # the README's measured result (2026-10-10, Intel Mac, macOS 14): a gui-domain job sees SSH_AUTH_SOCK, a user-domain Background job does not; whether the agent holds the key is still open
    assert "UNVERIFIED" not in out.detail and "SSH_AUTH_SOCK" in out.detail and "gui domain" in out.detail and "user domain" in out.detail
    assert "measured 2026-10-10" in out.detail and "still to verify" in out.detail
    assert "gui domain" in out.fix["text"] and "CCBOARD_LAUNCHD_DOMAIN=user" in out.fix["text"]
    dj.record(push=[{"repo": "a/b", "pushed": ["x"], "rejected": ["y: remote rejected"]}])
    assert dj.run().status == "pass", "a branch the remote refused is not an agent problem"
    dj.record(push=[{"repo": "a/b", "skipped": "no origin"}])
    assert "ssh-agent" not in dj.run().detail
    dj.record(push=[{"repo": "a/b", "error": "git fetch: failed"}])
    monkeypatch.setattr(plat, "IS_MACOS", False)
    monkeypatch.setattr(settings, "runtime", "systemd")
    monkeypatch.setattr(plat, "IS_LINUX", True)
    out = dj.run()
    assert "ssh-agent" not in out.detail, "the macOS note is for a Mac"


def test_wsl_repository_under_mnt_warns_and_ext4_does_not(dj, monkeypatch):
    monkeypatch.setattr(plat, "IS_MACOS", False)
    monkeypatch.setattr(plat, "IS_LINUX", True)
    monkeypatch.setattr(settings, "runtime", "systemd")
    unit = dj.tmp / "ccboard-backup.service"
    unit.write_text(f"[Service]\nEnvironment=PATH=/home/x/.local/bin:{dj.rbin}:/usr/bin\nEnvironment=HOME=/home/x\n")
    monkeypatch.setattr(backup, "SYSTEMD_UNIT", unit)
    monkeypatch.setattr(plat, "is_wsl", lambda: True)
    monkeypatch.setattr(settings, "restic_repo", "/mnt/c/restic")
    out = dj.run()
    assert out.status == "warn" and "/mnt/" in out.detail and "ext4" in out.fix["text"] and "wsl --export" in out.fix["text"]
    assert dj.mgr.calls == [["systemctl", "is-active", "ccboard-backup.timer"]]
    monkeypatch.setattr(settings, "restic_repo", "/home/x/restic")
    assert dj.run().status == "pass"
    monkeypatch.setattr(settings, "restic_repo", "/mnt/c/restic")
    monkeypatch.setattr(plat, "is_wsl", lambda: False)
    assert dj.run().status == "pass", "/mnt/ only matters under WSL"


def test_systemd_timer_active_passes_and_inactive_warns(dj, monkeypatch):
    monkeypatch.setattr(plat, "IS_MACOS", False)
    monkeypatch.setattr(plat, "IS_LINUX", True)
    monkeypatch.setattr(settings, "runtime", "systemd")
    unit = dj.tmp / "ccboard-backup.service"
    unit.write_text(f"[Service]\nEnvironment=PATH=/home/x/.local/bin:/usr/local/bin:/usr/bin:/bin\n")
    monkeypatch.setattr(backup, "SYSTEMD_UNIT", unit)
    out = dj.run()
    assert out.status == "warn" and "restic is not on the job's PATH" in out.detail and "ccboard-backup.timer is active" in out.detail
    (dj.tmp / "usrbin").mkdir()
    _script(dj.tmp / "usrbin" / "restic", "exit 0\n")
    unit.write_text(f"[Service]\nEnvironment=PATH={dj.tmp / 'usrbin'}:/usr/bin\n")
    assert dj.run().status == "pass"
    dj.mgr.loaded = False
    out = dj.run()
    assert out.status == "warn" and "ccboard-backup.timer is not active" in out.detail
    assert out.fix["cmd"] == "sudo systemctl start ccboard-backup.timer"


def test_off_its_runtime_the_row_skips_with_a_reason(dj, monkeypatch):
    for runtime, mac, linux in (("docker", False, True), ("host", True, False), ("systemd", True, False), ("launchd", False, True)):
        monkeypatch.setattr(settings, "runtime", runtime)
        monkeypatch.setattr(plat, "IS_MACOS", mac)
        monkeypatch.setattr(plat, "IS_LINUX", linux)
        out = dj.run()
        assert out.status == "skip" and out.detail, runtime
    assert dj.mgr.calls == [], "a skipped row asks the service manager nothing"
    monkeypatch.setattr(settings, "runtime", "docker")
    assert "container" in dj.run().detail and "host" in dj.run().detail


def test_the_box_in_a_container_never_fails_this_row(dj, monkeypatch):
    """The box runs the board in a container: its backup timer is the host's, which the board cannot see. Whatever else is off, the row skips."""
    monkeypatch.setattr(plat, "IS_MACOS", False)
    monkeypatch.setattr(plat, "IS_LINUX", True)
    monkeypatch.setattr(settings, "runtime", "docker")
    monkeypatch.setattr(settings, "restic_repo", "backups")
    dj.mgr.loaded = False
    assert dj.run().status == "skip"


def test_a_missing_service_manager_is_a_skip_and_a_row_always_comes_back(dj):
    dj.mgr.raise_missing = True
    out = dj.run()
    assert out.status == "skip" and "launchctl" in out.detail


def test_the_row_runs_through_the_doctor_runner(dj, monkeypatch):
    monkeypatch.setattr(doctor, "CHECKS", [_check()])
    res = doctor.run("box", refresh=True, db=None)
    assert [c["id"] for c in res["checks"]] == ["backup-job"] and res["checks"][0]["status"] == "pass"


# ---------------------------------------------------------------- the schedule subcommand


def cli(*args):
    return subprocess.run([sys.executable, str(TOOLS), *args], capture_output=True, text=True)


@pytest.mark.parametrize("spec,want", [("*-*-* 02:30:00", "2 30"), ("*-*-* 02:30", "2 30"), ("03:05", "3 5"), ("7:05", "7 5"), ("0:00", "0 0"),
                                       ("23:59", "23 59"), ("*-*-* 4:10:59", "4 10"), ("  *-*-*   01:15  ", "1 15")])
def test_accepted_schedule_forms_print_hour_and_minute(spec, want):
    r = cli("backup-schedule", spec)
    assert r.returncode == 0 and r.stdout == want + "\n" and r.stderr == ""
    assert mt.backup_schedule(spec) == tuple(int(x) for x in want.split())


@pytest.mark.parametrize("spec", ["Mon *-*-* 02:30", "hourly", "*:0/15", "", "daily", "24:00", "02:60", "*-*-* 02:30:61", "2026-10-08 02:30", "*-*-01 02:30",
                                  "02", "02:3", "*-*-* 02:30:00 UTC", "Mon..Fri 02:30", "\u0662:30"])
def test_every_other_form_exits_2_with_one_line_naming_the_accepted_forms(spec):
    r = cli("backup-schedule", spec)
    assert r.returncode == 2 and r.stdout == "" and r.stderr.count("\n") == 1
    assert "CCBOARD_BACKUP_ONCALENDAR" in r.stderr and "*-*-* HH:MM[:SS]" in r.stderr and "HH:MM" in r.stderr


def test_the_default_schedule_parses_and_the_rule_is_macos_only():
    assert mt.backup_schedule(mt.BACKUP_SCHEDULE_DEFAULT) == (2, 30)
    assert mt.BACKUP_SCHEDULE_DEFAULT == "*-*-* 02:30:00", "the setting keeps its default"
    install = (ROOT / "install.sh").read_text()
    assert "systemd-analyze calendar" in install, "the Linux installer still validates the spec with systemd-analyze"


def test_the_schedule_rule_feeds_the_plist(tmp_path):
    lr = importlib.util.spec_from_file_location("lr", ROOT / "scripts" / "launchd_render.py")
    mod = importlib.util.module_from_spec(lr)
    lr.loader.exec_module(mod)
    hour, minute = mt.backup_schedule("*-*-* 04:07:00")
    text = mod.render((ROOT / "launchd" / "dev.ccboard.backup.plist.in").read_text(encoding="utf-8"),
                      {"HOME": "/home/x", "APP_DIR": "/home/x/ccboard", "ENV_FILE": "/home/x/.local/share/ccboard/env",
                       "BACKUP_HOUR": str(hour), "BACKUP_MINUTE": str(minute)})
    assert plistlib.loads(text.encode())["StartCalendarInterval"] == {"Hour": 4, "Minute": 7}
