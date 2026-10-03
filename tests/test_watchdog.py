"""scripts/ccboard-watchdog.sh with a fake docker on PATH: start when not running, restart after two unhealthy runs, nothing otherwise."""
import os
import stat
import subprocess
from pathlib import Path

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
         "CCBOARD_DATA_DIR": str(tmp_path / "data"), **(env or {})}
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
