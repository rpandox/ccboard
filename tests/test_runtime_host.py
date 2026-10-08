"""Issue #120: CCBOARD_RUNTIME=docker is a Linux-host feature. install.sh's guard (the block between its docker host guard markers, run in a
throwaway bash with a stub docker, the pattern of tests/test_install_mem.py) and the doctor's runtime-host check (a temp /proc/version).
Nothing here calls a real docker, the Docker socket or the real /proc/version."""
import re
import socket
import subprocess
from pathlib import Path

import pytest

from app import doctor
from app.config import settings

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
README = ROOT / "README.md"

WSL = "Linux version 5.15.153.1-microsoft-standard-WSL2 (root@build) (gcc 11.2.0) #1 SMP"
UBUNTU = "Linux version 6.8.0-45-generic (buildd@lcy02) (x86_64-linux-gnu-gcc-13) #45-Ubuntu SMP"
LINUXKIT = "Linux version 6.10.14-linuxkit (root@buildkitsandbox) (gcc 13.2.1) #1 SMP"

STUB_DOCKER = """#!/bin/sh
echo "docker $*" >> "$T/docker.log"
[ -z "${FAKE_DOCKER_RC:-}" ] || exit "$FAKE_DOCKER_RC"
printf '%s\\n' "${FAKE_DOCKER_OS:-}"
"""

PREAMBLE = r"""
set -euo pipefail
note() { echo "   $*"; }
warn() { echo "warning: $*" >&2; }
die()  { echo "error: $*" >&2; exit 1; }
have() { if [ "$1" = docker ] && [ -n "${NO_DOCKER:-}" ]; then return 1; fi; command -v "$1" >/dev/null 2>&1; }
"""


def _text():
    return INSTALL.read_text()


def _guard_block():
    m = re.search(r"^# >>> docker host guard\n(.*?)^# <<< docker host guard\n", _text(), re.S | re.M)
    assert m, "install.sh lost its docker host guard markers"
    return m.group(1)


def guard(tmp_path, proc_version, runtime="docker", engine="", rc=None, no_docker=False, call="docker_host_guard"):
    """Run the guard in a throwaway bash. Returns (returncode, combined output, docker calls)."""
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    docker = fake / "docker"
    docker.write_text(STUB_DOCKER)
    docker.chmod(0o755)
    env = {"PATH": f"{fake}:/usr/bin:/bin", "T": str(tmp_path), "FAKE_DOCKER_OS": engine, "CCBOARD_RUNTIME": runtime}
    if rc is not None:
        env["FAKE_DOCKER_RC"] = str(rc)
    if no_docker:
        env["NO_DOCKER"] = "1"
    script = PREAMBLE + _guard_block() + f"\n{call}\n"
    if call == "docker_host_guard":
        script = PREAMBLE + _guard_block() + '\ndocker_host_guard "$PROC_VERSION_TEXT"\n'
        env["PROC_VERSION_TEXT"] = proc_version
    p = subprocess.run(["bash", "-c", script], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)
    log = tmp_path / "docker.log"
    return p.returncode, p.stdout, (log.read_text().splitlines() if log.exists() else [])


# ---- the installer guard

def test_docker_desktop_in_wsl_is_refused_with_the_reason_and_the_alternative(tmp_path):
    rc, out, calls = guard(tmp_path, WSL, engine="Docker Desktop")
    assert rc == 1
    assert "not supported with Docker Desktop" in out
    assert "tmux socket" in out and "process list" in out                     # the reason
    assert "install docker-ce inside this distro, or use the default systemd runtime" in out
    assert calls == ["docker info --format {{.OperatingSystem}}"]


def test_a_distro_owned_engine_in_wsl_is_allowed(tmp_path):
    rc, out, calls = guard(tmp_path, WSL, engine="Ubuntu 24.04.1 LTS")
    assert rc == 0 and out == ""
    assert calls == ["docker info --format {{.OperatingSystem}}"]


def test_the_microsoft_match_ignores_case(tmp_path):
    rc, out, _ = guard(tmp_path, "Linux version 5.15 (MICROSOFT-standard)", engine="Docker Desktop")
    assert rc == 1 and "Docker Desktop" in out


def test_a_plain_linux_host_is_untouched(tmp_path):
    """The pre-change baseline: no output, no exit, and docker is never even asked."""
    rc, out, calls = guard(tmp_path, UBUNTU, engine="Docker Desktop")
    assert (rc, out, calls) == (0, "", [])
    rc, out, calls = guard(tmp_path, "", engine="Docker Desktop")                # /proc/version unreadable
    assert (rc, out, calls) == (0, "", [])


def test_other_runtimes_are_untouched_even_in_wsl(tmp_path):
    for runtime in ("systemd", "launchd", "host"):
        assert guard(tmp_path, WSL, runtime=runtime, engine="Docker Desktop") == (0, "", [])


@pytest.mark.parametrize("kw", [{"engine": ""}, {"engine": "", "rc": 1}])
def test_an_engine_that_cannot_be_identified_is_said_aloud_not_skipped_silently(tmp_path, kw):
    rc, out, calls = guard(tmp_path, WSL, **kw)
    assert rc == 0
    assert "could not tell which Docker engine" in out and "docker info --format" in out
    assert calls == ["docker info --format {{.OperatingSystem}}"]


def test_without_docker_installed_the_guard_leaves_it_to_the_preflight(tmp_path):
    assert guard(tmp_path, WSL, engine="Docker Desktop", no_docker=True) == (0, "", [])


def test_the_macos_installer_reuses_one_function_with_the_same_text(tmp_path):
    rc_mac, out_mac, _ = guard(tmp_path, "", call="docker_host_refusal macOS")
    rc_win, out_win, _ = guard(tmp_path, "", call='docker_host_refusal "Docker Desktop"')
    assert rc_mac == rc_win == 1
    assert "not supported with macOS" in out_mac and "use the launchd runtime" in out_mac
    reason = lambda s: s.split(":", 1)[1].split(". Instead:")[0]            # noqa: E731
    assert reason(out_mac.split("with macOS", 1)[1]) == reason(out_win.split("with Docker Desktop", 1)[1])


def test_install_sh_calls_the_guard_once_after_the_runtime_check_and_before_any_change():
    t = _text()
    assert t.count('docker_host_guard "$(cat /proc/version 2>/dev/null || true)"') == 1
    i_case = t.index("case \"$CCBOARD_RUNTIME\" in systemd|docker)")
    i_call = t.index('docker_host_guard "$(cat /proc/version')
    i_tail = t.index("# ---------------------------------------------------------------- tailscale checks")
    assert i_case < i_call < i_tail
    assert t.index("# >>> docker host guard") < i_case                         # defined before it is called


def test_the_guard_never_touches_the_docker_socket_directly():
    block = _guard_block()
    assert "docker.sock" not in block and "/var/run" not in block
    assert block.count("docker info") == 1 or "docker info --format" in block


# ---- the doctor check

@pytest.fixture
def kernel(monkeypatch, tmp_path):
    """/proc/version as a temp file; _run, socket connects and subprocess are tripwires: this check must not call any of them."""
    f = tmp_path / "version"

    def put(text):
        f.write_text(text + "\n")
    monkeypatch.setattr(doctor, "PROC_VERSION", f)
    monkeypatch.setattr(settings, "runtime", "docker")

    def boom(*a, **kw):
        raise AssertionError("runtime-host must not run a command or open a socket")
    monkeypatch.setattr(doctor, "_run", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    return put


def test_runtime_host_is_registered_in_the_box_group():
    assert [c[1] for c in doctor.CHECKS if c[0] == "runtime-host"] == ["box"]


def test_runtime_host_fails_on_a_docker_desktop_kernel(kernel):
    kernel(LINUXKIT)
    out = doctor._c_runtime_host(None)
    assert out.status == "fail"
    assert "Docker Desktop" in out.detail and "linuxkit" in out.detail
    assert "Linux host" in out.fix["text"] and "systemd" in out.fix["text"]


def test_runtime_host_passes_on_a_normal_kernel_with_the_one_sentence_detail(kernel):
    kernel(UBUNTU)
    out = doctor._c_runtime_host(None)
    assert out.status == "pass" and out.fix is None
    assert out.detail.startswith("Container mode is Linux only") and "\n" not in out.detail and out.detail.count(".") == 0


def test_runtime_host_passes_for_docker_ce_inside_wsl(kernel):
    kernel(WSL)
    assert doctor._c_runtime_host(None).status == "pass"


@pytest.mark.parametrize("runtime", ["systemd", "host", "launchd"])
def test_runtime_host_skips_for_every_other_runtime(kernel, monkeypatch, runtime):
    kernel(LINUXKIT)
    monkeypatch.setattr(settings, "runtime", runtime)
    assert doctor._c_runtime_host(None).status == "skip"


def test_runtime_host_skips_when_the_kernel_string_cannot_be_read(kernel, monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "PROC_VERSION", tmp_path / "missing")
    assert doctor._c_runtime_host(None).status == "skip"


def test_runtime_host_is_the_registered_function():
    assert {c[0]: c[3] for c in doctor.CHECKS}["runtime-host"] is doctor._c_runtime_host


# ---- the README says the same

def test_readme_carries_the_support_table_and_the_doctor_sentence():
    t = README.read_text()
    assert "Where the container runtime works" in t
    assert doctor.CONTAINER_LINUX_ONLY.split(":")[0] in t                      # "Container mode is Linux only"
    for row in ("Linux host", "WSL2", "Docker Desktop on macOS", "Docker Desktop on Windows"):
        assert row in t
    assert "arm64" in t.split("Where the container runtime works", 1)[1].split("\n## ", 1)[0]
