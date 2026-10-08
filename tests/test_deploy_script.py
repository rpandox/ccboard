"""scripts/deploy.sh --local-build (CI outage fallback) and scripts/ci-smoke.sh, run for real against stub binaries.

Fake git, ssh, docker, curl, sleep, nice and ionice sit first on PATH and append every call to a log, so the tests assert
the order of what the scripts do (push, gate, build, override, up, health) without a network, a box or a Docker daemon.
The "box" is a temp HOME; nothing outside tmp_path is touched."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.posix_sh      # runs the shell scripts under sh

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "scripts" / "deploy.sh"
SMOKE = ROOT / "scripts" / "ci-smoke.sh"
FULL = "1234567" + "a" * 33
SHA7 = "1234567"

STUBS = {
    "git": r'''#!/bin/bash
echo "git $*" >> "$LOG"
case "$1 $2" in
  "rev-parse origin/main") echo "$FULL" ;;
  "rev-parse HEAD") echo "${STUB_HEAD:-$FULL}" ;;
  "log --oneline") echo "abc1234 a subject" ;;
esac
exit 0
''',
    "ssh": r'''#!/bin/bash
echo "ssh $*" >> "$LOG"
shift 3                       # -o BatchMode=yes <host>
cd "$HOME"
exec bash -c "$1"
''',
    "docker": r'''#!/bin/bash
echo "docker $*" >> "$LOG"
case "$1" in
  ps) case "$*" in *--filter*) echo "ccboard-local:x (Up 1 second)";; *) echo ccboard;; esac ;;
  exec) exit "${GATE_RC:-0}" ;;
  build) exit "${BUILD_RC:-0}" ;;
  manifest) exit "${MANIFEST_RC:-0}" ;;
  compose)
    case "$*" in *" up "*) f=""; prev=""; for a in "$@"; do [ "$prev" = -f ] && f=$a; prev=$a; done
      if [ -n "${OVERRIDE_COPY:-}" ] && [ -f "$f" ]; then cp "$f" "$OVERRIDE_COPY"; fi ;; esac ;;
esac
exit 0
''',
    "curl": '#!/bin/bash\necho "curl $*" >> "$LOG"\nexit "${CURL_RC:-0}"\n',
    "sleep": "#!/bin/bash\nexit 0\n",
    "nice": '#!/bin/bash\necho "nice $*" >> "$LOG"\nshift 2\nexec "$@"\n',
    "ionice": '#!/bin/bash\necho "ionice $*" >> "$LOG"\nshift 1\nexec "$@"\n',
}


@pytest.fixture
def box(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in STUBS.items():
        p = bindir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "home"
    (home / "ccboard").mkdir(parents=True)
    compose_dir = home / ".local" / "share" / "ccboard" / "compose"
    compose_dir.mkdir(parents=True)
    (compose_dir / "docker-compose.yml").write_text("name: ccboard\n")
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(home), "LOG": str(log), "FULL": FULL,
           "OVERRIDE_COPY": str(tmp_path / "override.seen")}
    for k in ("GATE_RC", "BUILD_RC", "MANIFEST_RC", "CURL_RC", "STUB_HEAD"):
        env.pop(k, None)

    class Box:
        pass

    b = Box()
    b.env, b.log, b.home, b.compose_dir, b.tmp = env, log, home, compose_dir, tmp_path
    b.override = compose_dir / "docker-compose.local.yml"
    b.modefile = home / ".local" / "share" / "ccboard" / "deploy-mode"

    def run(*args, **extra):
        return subprocess.run(["bash", str(DEPLOY), *args], env={**env, **extra}, capture_output=True, text=True, timeout=60)

    def calls():
        return log.read_text().splitlines()

    b.run, b.calls = run, calls
    return b


def _first(calls, prefix):
    for i, c in enumerate(calls):
        if c.startswith(prefix):
            return i
    raise AssertionError(f"no call starting with {prefix!r} in {calls}")


def test_local_build_runs_push_gate_build_override_up_health_in_order(box):
    r = box.run("--local-build", "somehost")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = box.calls()
    order = [_first(calls, p) for p in ("git push", "ssh ", "docker exec ccboard /opt/ccboard/scripts/ccboard-deploy-gate",
                                         "docker build", "docker compose", "curl")]
    assert order == sorted(order) and len(set(order)) == len(order), calls
    assert not any(c.startswith("docker manifest") or "prod pull" in c for c in calls), "no wait for the registry tag, no pull"
    build = calls[_first(calls, "docker build")]
    assert f"--build-arg CCBOARD_VERSION=local-{SHA7}" in build and f"--build-arg CCBOARD_REVISION={FULL}" in build
    assert f"-t ccboard-local:{SHA7}" in build
    up = calls[_first(calls, "docker compose")]
    assert "-f " in up and "docker-compose.local.yml" in up and "--profile prod up -d ccboard" in up
    assert any(c.startswith("nice ") for c in calls), "the build runs under nice"
    seen = (box.tmp / "override.seen").read_text()
    assert f"image: ccboard-local:{SHA7}" in seen and 'com.centurylinklabs.watchtower.enable: "false"' in seen
    assert f"running local build {SHA7}" in r.stdout
    assert box.modefile.read_text().strip() == f"running local build {SHA7}"
    assert box.override.exists()


def test_held_gate_stops_before_the_build(box):
    r = box.run("--local-build", "somehost", GATE_RC="75")
    assert r.returncode != 0 and "HOLD" in r.stderr and "--force" in r.stderr
    calls = box.calls()
    assert any("ccboard-deploy-gate" in c for c in calls)
    assert not any(c.startswith("docker build") or c.startswith("docker compose") for c in calls)
    assert not box.override.exists() and not box.modefile.exists()


def test_force_goes_on_despite_a_held_gate(box):
    r = box.run("--local-build", "--force", "somehost", GATE_RC="75")
    assert r.returncode == 0, r.stdout + r.stderr
    assert any(c.startswith("docker build") for c in box.calls())
    assert "because of --force" in r.stdout


def test_failed_build_leaves_the_running_container_alone(box):
    r = box.run("--local-build", "somehost", BUILD_RC="1")
    assert r.returncode != 0 and "docker build" in r.stderr
    assert not any(c.startswith("docker compose") for c in box.calls())
    assert not box.override.exists() and not box.modefile.exists()


def test_checkout_not_at_the_pushed_commit_builds_nothing(box):
    r = box.run("--local-build", "somehost", STUB_HEAD="f" * 40)
    assert r.returncode != 0 and "not the pushed" in r.stderr
    assert not any(c.startswith("docker build") for c in box.calls())


def test_default_path_removes_the_override_and_never_builds(box):
    box.override.write_text("services:\n  ccboard:\n    image: ccboard-local:old\n")
    r = box.run("somehost")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = box.calls()
    assert not box.override.exists()
    assert not any(c.startswith("docker build") for c in calls)
    assert _first(calls, "docker manifest") < _first(calls, "docker compose")
    assert any("--profile prod pull" in c for c in calls) and any("--profile prod up -d" in c for c in calls)
    assert not any("docker-compose.local.yml" in c for c in calls)
    assert box.modefile.read_text().strip() == f"running ghcr.io/rpandox/ccboard:sha-{SHA7}"
    assert not any("ccboard-deploy-gate" in c for c in calls), "the registry path is unchanged: no new gate call"


def test_default_path_with_a_missing_tag_keeps_the_local_build_running(box):
    box.override.write_text("services:\n  ccboard:\n    image: ccboard-local:old\n")
    r = box.run("somehost", MANIFEST_RC="1")
    assert r.returncode != 0 and "still running its local build" in r.stderr
    assert box.override.exists(), "the override stays until the registry tag exists"
    assert not any(c.startswith("docker compose") for c in box.calls())


def test_options_are_validated(box):
    assert box.run("--force", "somehost").returncode == 2
    assert box.run("--bogus", "somehost").returncode == 2
    assert box.run("--local-build").returncode != 0


def test_scripts_parse():
    for s in (DEPLOY, SMOKE):
        assert subprocess.run(["bash", "-n", str(s)], capture_output=True).returncode == 0, s


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_scripts_shellcheck():
    for s in (DEPLOY, SMOKE):
        r = subprocess.run(["shellcheck", "-S", "warning", str(s)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout


# ---------------------------------------------------------------- scripts/ci-smoke.sh
SMOKE_DOCKER = r'''#!/bin/bash
echo "docker $*" >> "$LOG"
case "$1" in
  run) case "$*" in *"--user 0"*) rm -rf "$SMOKE_FORCE_RM" ;; esac ;;
  exec) case "$*" in *"${SMOKE_FAIL_EXEC:-@@none@@}"*) exit 1 ;; esac ;;
  inspect) case "$*" in *ExitCode*) echo 3 ;; *) echo "${SMOKE_RUNNING:-true}" ;; esac ;;
  logs) echo "container log line" ;;
esac
exit 0
'''


@pytest.fixture
def smoke(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in {"docker": SMOKE_DOCKER, "curl": STUBS["curl"], "sleep": STUBS["sleep"]}.items():
        p = bindir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "calls.log"
    log.write_text("")
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "LOG": str(log), "TMPDIR": str(tmpdir),
           "CCBOARD_SMOKE_WAIT": "3", "CCBOARD_SMOKE_PORT": "18999"}

    def run(**extra):
        r = subprocess.run(["bash", str(SMOKE), "ccboard:ci"], env={**env, **extra}, capture_output=True, text=True, timeout=60)
        return r, log.read_text().splitlines(), list(tmpdir.iterdir())

    return run


def test_smoke_passes_runs_the_four_checks_and_cleans_up(smoke):
    r, calls, left = smoke()
    assert r.returncode == 0, r.stdout + r.stderr
    run = calls[_first(calls, "docker run -d")]
    assert "--user 1000:1000" in run and "CCBOARD_PORT=18999" in run and "--network host" in run and run.rstrip().endswith("ccboard:ci")
    assert "443" not in run
    # every directory the board validates at startup points at a mount (the first main run failed: PROJECTS_DIR stayed /srv/projects)
    for env, mount in (("HOME", "/work/home"), ("CCBOARD_DATA_DIR", "/work/data"), ("PROJECTS_DIR", "/work/projects")):
        assert f"{env}={mount}" in run and f":{mount} " in run, env
    # the mounts belong to uid 1000 before the board starts (it refuses a PROJECTS_DIR it does not own; a CI runner is not uid 1000)
    own = _first(calls, "docker run --rm --user 0")
    assert own < _first(calls, "docker run -d") and "chown -R 1000:1000" in calls[own] and "chmod 0700 /w/tmux" in calls[own]
    execs = [c for c in calls if c.startswith("docker exec")]
    assert [e.split(" ", 3)[3] for e in execs] == ["python -c import app.main", "ccusage --version", "tmux -V", "gh --version"]
    assert any(c.startswith("docker rm -f ccboard-smoke") for c in calls[_first(calls, "docker run -d"):])
    assert not any(c.startswith("docker logs") for c in calls), "logs are for failures only"
    assert left == [], "the temp directories are removed"


def test_smoke_fails_with_logs_when_healthz_never_answers(smoke):
    r, calls, left = smoke(CURL_RC="22")
    assert r.returncode != 0 and "did not answer" in r.stderr
    assert "container log line" in r.stdout
    assert any(c.startswith("docker rm -f ccboard-smoke") for c in calls[_first(calls, "docker run -d"):]) and left == []


def test_smoke_fails_with_logs_when_the_container_exits(smoke):
    r, calls, left = smoke(CURL_RC="22", SMOKE_RUNNING="false")
    assert r.returncode != 0 and "exited before" in r.stderr and "exit code 3" in r.stderr
    assert "container log line" in r.stdout and left == []


def test_smoke_fails_when_a_cli_is_missing(smoke):
    r, calls, left = smoke(SMOKE_FAIL_EXEC="tmux -V")
    assert r.returncode != 0 and "FAILED: tmux -V" in r.stderr
    assert "container log line" in r.stdout and left == []
    assert not any("gh --version" in c for c in calls), "stops at the first failed check"


def test_ci_and_deploy_scripts_are_executable_in_the_checkout():
    # CI checks files out with their git mode: a script committed as 100644 fails the workflow with exit 126 (it happened to ci-smoke.sh)
    for name in ("scripts/ci-smoke.sh", "scripts/deploy.sh", "scripts/docker-entrypoint.sh", "scripts/qa-ui.sh", "scripts/qa_terminal.sh"):
        p = ROOT / name
        if p.exists():
            assert os.access(p, os.X_OK), f"{name} must be executable (git update-index --chmod=+x {name})"
