"""scripts/ccboard-watchdog.sh with a fake docker on PATH: start when not running, restart after two unhealthy runs, nothing otherwise."""
import os
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.posix_sh      # runs the shell scripts under sh

ROOT = Path(__file__).resolve().parents[1]
WD = ROOT / "scripts" / "ccboard-watchdog.sh"
FAKE = """#!/bin/sh
echo "$@" >> "$FAKE_DOCKER_LOG"
case "$1" in
  inspect) [ -n "$FAKE_STATE" ] || exit 1; printf '%s\\n' "$FAKE_STATE" ;;
  *) exit "${FAKE_RC:-0}" ;;
esac
"""


def run(tmp_path, state, env=None):
    bin_dir = tmp_path / "bin"; bin_dir.mkdir(exist_ok=True)
    fd = bin_dir / "docker"; fd.write_text(FAKE); fd.chmod(fd.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "docker.log"
    e = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_DOCKER_LOG": str(log), "FAKE_STATE": state,
         "CCBOARD_DATA_DIR": str(tmp_path / "data"), "HOME": str(tmp_path / "home"),      # never the real home: the watchdog also runs ccboard-mem-env
         "CLAUDE_MEM_DATA_DIR": str(tmp_path / "home" / ".claude-mem"), "CCBOARD_PROC_ROOT": str(tmp_path / "proc"), **(env or {})}
    r = subprocess.run(["sh", str(WD)], env=e, capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return log.read_text().splitlines() if log.exists() else []


def test_starts_a_created_or_exited_container(tmp_path):
    assert run(tmp_path, "created none")[-1] == "start ccboard"
    assert run(tmp_path, "exited none")[-1] == "start ccboard"
    assert "docker start ccboard" in (tmp_path / "data" / "watchdog.log").read_text()


def test_running_healthy_does_nothing_and_clears_the_flag(tmp_path):
    (tmp_path / "data").mkdir(); (tmp_path / "data" / ".watchdog-unhealthy").write_text("")
    calls = run(tmp_path, "running healthy")
    assert calls == ["inspect -f {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} ccboard"]
    assert not (tmp_path / "data" / ".watchdog-unhealthy").exists()


def test_unhealthy_restarts_only_on_the_second_consecutive_run(tmp_path):
    calls = run(tmp_path, "running unhealthy")
    assert all(c.startswith("inspect") for c in calls), "first sighting: wait"
    assert (tmp_path / "data" / ".watchdog-unhealthy").exists()
    calls = run(tmp_path, "running unhealthy")
    assert calls[-1] == "restart -t 15 ccboard"
    assert not (tmp_path / "data" / ".watchdog-unhealthy").exists()


def test_missing_container_or_restarting_state_is_left_alone(tmp_path):
    assert all(c.startswith("inspect") for c in run(tmp_path, ""))          # inspect fails: no such container (systemd mode)
    assert all(c.startswith("inspect") for c in run(tmp_path, "restarting none"))
    assert not (tmp_path / "data" / "watchdog.log").exists()


# ---------------------------------------------------------------- scripts/ccboard-mem-env (issue #38)
import json

MEM_ENV = ROOT / "scripts" / "ccboard-mem-env"


def fake_worker(tmp_path, environ: bytes, pidfile=None, pid=4242):
    mem = tmp_path / "home" / ".claude-mem"; mem.mkdir(parents=True, exist_ok=True)
    (mem / "worker.pid").write_text(pidfile if pidfile is not None else json.dumps({"pid": pid, "port": 37777, "startedAt": "x"}))
    if environ is not None:
        (tmp_path / "proc" / str(pid)).mkdir(parents=True, exist_ok=True)
        (tmp_path / "proc" / str(pid) / "environ").write_bytes(environ)


def run_mem_env(tmp_path):
    e = {**os.environ, "HOME": str(tmp_path / "home"), "CLAUDE_MEM_DATA_DIR": str(tmp_path / "home" / ".claude-mem"),
         "CCBOARD_PROC_ROOT": str(tmp_path / "proc"), "CCBOARD_DATA_DIR": str(tmp_path / "data")}
    r = subprocess.run(["sh", str(MEM_ENV)], env=e, capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return tmp_path / "data" / "mem-env.json"


def test_mem_env_writes_names_only_never_values_mode_0600(tmp_path):
    secret = b"SENTINEL-SECRET-VALUE"
    fake_worker(tmp_path, b"PATH=/usr/bin\0CCBOARD_SESSION=" + secret + b"\0CCBOARD_URL=http://x\0TMUX=/tmp/t,1,0\0TMUX_PANE=%0\0"
                b"CLAUDECODE=1\0CLAUDE_CODE_ENTRYPOINT=cli\0HOME=/home/u\0OTHER=1\0NOT_CCBOARD_X=2\0")
    out = run_mem_env(tmp_path)
    text = out.read_text()
    d = json.loads(text)
    assert d["pid"] == 4242 and d["at"].endswith("Z")
    assert d["names"] == ["CCBOARD_SESSION", "CCBOARD_URL", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "TMUX", "TMUX_PANE"]
    assert secret.decode() not in text and "/usr/bin" not in text and "http://x" not in text, "values are never written"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert not [p for p in out.parent.iterdir() if ".tmp" in p.name], "written atomically: no temp file left"


def test_mem_env_clean_worker_writes_an_empty_list(tmp_path):
    fake_worker(tmp_path, b"PATH=/usr/bin\0HOME=/home/u\0")
    assert json.loads(run_mem_env(tmp_path).read_text())["names"] == []


def test_mem_env_accepts_a_bare_pid_file(tmp_path):
    fake_worker(tmp_path, b"TMUX_PANE=%3\0", pidfile="4242\n")
    assert json.loads(run_mem_env(tmp_path).read_text()) ["names"] == ["TMUX_PANE"]


def test_mem_env_writes_nothing_without_a_worker_pid_or_environment(tmp_path):
    assert not run_mem_env(tmp_path).exists()                                          # no worker.pid at all
    fake_worker(tmp_path, None)                                                        # a pid, but no such process
    assert not run_mem_env(tmp_path).exists()
    fake_worker(tmp_path, b"", pidfile="not a pid")
    assert not run_mem_env(tmp_path).exists()


def test_the_watchdog_runs_mem_env_after_the_container_check(tmp_path):
    fake_worker(tmp_path, b"CCBOARD_SESSION=s1\0")
    run(tmp_path, "running healthy")
    assert json.loads((tmp_path / "data" / "mem-env.json").read_text())["names"] == ["CCBOARD_SESSION"]
