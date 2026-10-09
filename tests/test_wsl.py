"""Issue #118: Windows through WSL2. Everything runs on Linux or macOS CI; nothing needs Windows.

    * app/platform.py: WSL detection from a fake /proc/version, the networking mode from a fake `wslinfo`, interop, the mount table, the init
      identity from a fake /proc, the Tailscale placement;
    * install.sh's WSL branch (the block between its wsl branch markers, run in a throwaway bash with stubs, the pattern of
      tests/test_runtime_host.py), and that a plain Linux host gets exactly the old behaviour;
    * app/doctor_wsl.py: the wsl- checks with fakes for systemctl, ss, wslinfo, powershell.exe, the mount table and the start history;
    * scripts/windows/ccboard-wsl-keepalive.ps1: a static scan of its text (pwsh is not available here), plus a parse when pwsh is installed;
    * the README section.

Nothing here touches the real /proc/version, ~/.claude, ~/.codex, the tmux socket, a real tailscale, powershell.exe or wslinfo.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import config, doctor, doctor_wsl as dw
from app import platform as plat
from app import tailscale as ts
from app.config import settings

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
README = ROOT / "README.md"
PS1 = ROOT / "scripts" / "windows" / "ccboard-wsl-keepalive.ps1"

WSL_VERSION = "Linux version 5.15.153.1-microsoft-standard-WSL2 (root@build) (gcc 11.2.0) #1 SMP"
UBUNTU_VERSION = "Linux version 6.8.0-45-generic (buildd@lcy02) (x86_64-linux-gnu-gcc-13) #45-Ubuntu SMP"


# ================================================================== platform

@pytest.fixture
def proc_version(monkeypatch, tmp_path):
    """Point is_wsl() at a temp file instead of the real /proc/version (it reads a fixed path and caches the answer)."""
    real = plat.Path

    def write(text):
        f = tmp_path / "version"
        f.write_text(text)
        plat.is_wsl.cache_clear()

        def P(p, *a):
            return real(f) if p == "/proc/version" else real(p, *a)
        monkeypatch.setattr(plat, "Path", P)
        monkeypatch.setattr(plat, "IS_LINUX", True)
    yield write
    monkeypatch.undo()
    plat.is_wsl.cache_clear()


def test_is_wsl_reads_microsoft_from_the_kernel_version(proc_version):
    proc_version(WSL_VERSION)
    assert plat.is_wsl() is True
    proc_version(UBUNTU_VERSION)
    assert plat.is_wsl() is False
    proc_version("Linux version 4.4.0-19041-Microsoft (Microsoft@Microsoft.com)")      # WSL1: capital M
    assert plat.is_wsl() is True


def test_is_wsl_is_false_off_linux(monkeypatch):
    plat.is_wsl.cache_clear()
    monkeypatch.setattr(plat, "IS_LINUX", False)
    assert plat.is_wsl() is False
    plat.is_wsl.cache_clear()


def _fake_wslinfo(tmp_path, monkeypatch, body):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if body is not None:
        f = bin_dir / "wslinfo"
        f.write_text("#!/bin/sh\n" + body + "\n")
        f.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + ":/usr/bin:/bin")
    monkeypatch.setattr(plat, "is_wsl", lambda: True)


@pytest.mark.parametrize("body,expected", [
    ('echo mirrored', "mirrored"), ('echo NAT', "nat"), ('echo "nat extra words"', "nat"),
    ('echo something-new', "unknown"), ('echo nat; exit 1', "unknown"), ('exit 0', "unknown"), (None, "unknown"),
])
def test_wsl_networking_mode_from_wslinfo(tmp_path, monkeypatch, body, expected):
    _fake_wslinfo(tmp_path, monkeypatch, body)
    if body is None:
        monkeypatch.setattr(plat.shutil, "which", lambda name: None)      # a PATH that really has no wslinfo, even on a dev machine
    assert plat.wsl_networking_mode() == expected


def test_wsl_networking_mode_is_none_off_wsl_and_unknown_on_a_hung_wslinfo(tmp_path, monkeypatch):
    monkeypatch.setattr(plat, "is_wsl", lambda: False)
    assert plat.wsl_networking_mode() is None
    _fake_wslinfo(tmp_path, monkeypatch, "sleep 5")
    monkeypatch.setattr(plat, "WSLINFO_TIMEOUT", 0.2)
    assert plat.wsl_networking_mode() == "unknown"


def test_wsl_interop_needs_wsl_and_the_binfmt_entry(tmp_path, monkeypatch):
    entry = tmp_path / "WSLInterop"
    monkeypatch.setattr(plat, "WSL_INTEROP_FILES", (entry, tmp_path / "WSLInterop-late"))
    monkeypatch.setattr(plat, "is_wsl", lambda: True)
    assert plat.wsl_interop() is False
    entry.write_text("enabled")
    assert plat.wsl_interop() is True
    monkeypatch.setattr(plat, "is_wsl", lambda: False)
    assert plat.wsl_interop() is False


MOUNTS = r"""/dev/sdc / ext4 rw,relatime,discard 0 0
C:\134 /mnt/c 9p rw,noatime,dirsync,aname=drvfs;path=C:\;uid=1000;gid=1000,symlinkroot=/mnt/ 0 0
none /mnt/wslg tmpfs rw,relatime 0 0
D:\134 /data/win\040drive 9p rw,noatime,aname=drvfs;path=D:\;uid=1000 0 0
tools /srv/tools 9p rw,relatime,trans=virtio 0 0
"""


def test_windows_mount_finds_drives_in_the_mount_table(tmp_path, monkeypatch):
    table = tmp_path / "mounts"
    table.write_text(MOUNTS)
    monkeypatch.setattr(plat, "PROC_MOUNTS", table)
    monkeypatch.setattr(plat, "is_wsl", lambda: True)
    assert plat.windows_mount("/mnt/c/projects") == "/mnt/c"
    assert plat.windows_mount("/mnt/c") == "/mnt/c"
    assert plat.windows_mount("/data/win drive/work") == "/data/win drive"          # a drive mounted outside /mnt/
    assert plat.windows_mount("/home/dev/projects") is None                           # the ext4 root
    assert plat.windows_mount("/srv/tools/x") is None                                 # a 9p share that is no Windows drive
    assert plat.windows_mount("/mnt/wslg/x") is None
    assert plat.on_windows_drive("/data/win drive/work") is True
    assert plat.on_windows_drive("/mnt/anything") is True                             # under /mnt/ is enough for the quick look
    monkeypatch.setattr(plat, "is_wsl", lambda: False)
    assert plat.windows_mount("/mnt/c/projects") is None and plat.on_windows_drive("/mnt/c/x") is False


def test_windows_mount_survives_an_unreadable_table(tmp_path, monkeypatch):
    monkeypatch.setattr(plat, "PROC_MOUNTS", tmp_path / "missing")
    monkeypatch.setattr(plat, "is_wsl", lambda: True)
    assert plat.windows_mount("/mnt/c/x") is None


def _fake_init(root, boot="0123abcd-4567-89ab-cdef-0123456789ab", ticks=4321, uptime="5000.50 9000.00"):
    (root / "sys" / "kernel" / "random").mkdir(parents=True, exist_ok=True)
    (root / "sys" / "kernel" / "random" / "boot_id").write_text(boot + "\n")
    (root / "1").mkdir(exist_ok=True)
    fields = ["0"] * 49
    fields[19] = str(ticks)                    # field 22 of /proc/<pid>/stat, the start time, 19 after the state letter
    (root / "1" / "stat").write_text("1 (sys temd) S " + " ".join(fields[1:]) + "\n")
    (root / "uptime").write_text(uptime + "\n")


def test_init_identity_tells_a_vm_restart_from_a_distro_restart(tmp_path, monkeypatch):
    from tests.proc_fake import use_fake_proc
    use_fake_proc(monkeypatch, tmp_path)
    monkeypatch.setattr(os, "sysconf", lambda name: 100)
    _fake_init(tmp_path)
    first = plat.init_identity()
    assert first[0] == "0123abcd:4321"                                    # the kernel boot id's first 8 digits and PID 1's start tick: no clock involved
    assert abs(first[1] - (plat.time.time() - 5000.5 + 43.21)) < 5
    _fake_init(tmp_path, ticks=999)                                       # same virtual machine, the distro's init started again
    assert plat.init_identity()[0] == "0123abcd:999"
    _fake_init(tmp_path, boot="ffff0000-4567-89ab-cdef-0123456789ab")      # the virtual machine restarted
    assert plat.init_identity()[0] == "ffff0000:4321"


def test_init_identity_is_none_when_nothing_is_readable(tmp_path, monkeypatch):
    from tests.proc_fake import use_fake_proc
    use_fake_proc(monkeypatch, tmp_path)
    assert plat.init_identity() is None
    monkeypatch.setattr(plat, "IS_LINUX", False)
    assert plat.init_identity() is None


def test_tailscale_placement(monkeypatch):
    monkeypatch.setattr(plat, "is_wsl", lambda: True)
    monkeypatch.setattr(plat.shutil, "which", lambda n: "/usr/bin/tailscale" if n == "tailscale" else None)
    assert plat.tailscale_placement({"CCBOARD_TAILSCALE_PLACEMENT": "host"}) == "host"
    assert plat.tailscale_placement({"CCBOARD_TAILSCALE_PLACEMENT": " WSL "}) == "wsl"
    assert plat.tailscale_placement({}) == "wsl"                                       # unset: the distro has tailscale
    assert plat.tailscale_placement({"CCBOARD_TAILSCALE_PLACEMENT": "nonsense"}) == "wsl"
    assert plat.tailscale_placement({}, implied=False) is None
    monkeypatch.setattr(plat.shutil, "which", lambda n: None)
    assert plat.tailscale_placement({}) == "host"                                      # unset and no tailscale in the distro
    monkeypatch.setattr(plat, "is_wsl", lambda: False)
    assert plat.tailscale_placement({"CCBOARD_TAILSCALE_PLACEMENT": "host"}) is None   # not a concept anywhere else


# ================================================================== install.sh

def _text():
    return INSTALL.read_text()


def _block():
    m = re.search(r"^# >>> wsl branch\n(.*?)^# <<< wsl branch\n", _text(), re.S | re.M)
    assert m, "install.sh lost its wsl branch markers"
    return m.group(1)


STUB_SYSTEMCTL = """#!/bin/sh
echo "systemctl $*" >> "$T/calls.log"
[ -z "${FAKE_SYSTEMCTL_RC:-}" ] || { printf '%s\\n' "${FAKE_SYSTEMD:-}"; exit "$FAKE_SYSTEMCTL_RC"; }
printf '%s\\n' "${FAKE_SYSTEMD:-running}"
"""

PREAMBLE = r"""
set -euo pipefail
log()  { echo "== $*"; }
note() { echo "   $*"; }
warn() { echo "warning: $*" >&2; }
die()  { echo "error: $*" >&2; exit 1; }
have() { if [ "$1" = tailscale ]; then [ -n "${HAVE_TAILSCALE:-}" ]; else command -v "$1" >/dev/null 2>&1; fi; }
command() { if [ "${1:-}" = -v ] && [ -n "${FAKE_AGENT_PATH:-}" ] && [ "${2:-}" = claude ]; then echo "$FAKE_AGENT_PATH"; else builtin command "$@"; fi; }
mem_worker_port() { echo 37711; }
: "${PROJECTS_DIR:=/home/dev/projects}" "${CCBOARD_DATA_DIR:=/home/dev/.local/share/ccboard}" "${APP_DIR:=/home/dev/ccboard}"
: "${CCBOARD_RUNTIME:=systemd}" "${CCBOARD_ALLOWED_USERS:=}" "${CCBOARD_HTTPS_PORT:=443}" "${CODE_HTTPS_PORT:=8443}" "${NTFY_HTTPS_PORT:=8444}"
: "${CCBOARD_PORT:=8000}" "${TTYD_PORT:=7681}" "${CODE_SERVER_PORT:=8080}" "${NTFY_URL:=}" "${CCBOARD_MEM_HTTPS_PORT:=}" "${CCBOARD_PUBLIC_URL:=}"
: "${CCBOARD_TAILSCALE_PLACEMENT:=}" "${TS_FQDN:=}"
"""


def run_block(tmp_path, call, proc_version=WSL_VERSION, extra_env=None):
    """Run `call` after the wsl branch block in a throwaway bash. Returns (returncode, stdout, stderr, the values echoed by the call)."""
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    sc = fake / "systemctl"
    sc.write_text(STUB_SYSTEMCTL)
    sc.chmod(0o755)
    env = {"PATH": f"{fake}:/usr/bin:/bin", "T": str(tmp_path), "PROC_VERSION_TEXT": proc_version}
    env.update(extra_env or {})
    script = PREAMBLE + _block() + "\n" + call + "\n"
    p = subprocess.run(["bash", "-c", script], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
    return p.returncode, p.stdout, p.stderr


GUARD = 'wsl_guard "$PROC_VERSION_TEXT"; echo "PLACEMENT=[$CCBOARD_TAILSCALE_PLACEMENT] FQDN=[$TS_FQDN] URL=[$CCBOARD_PUBLIC_URL]"'
HOST = {"CCBOARD_TAILSCALE_PLACEMENT": "host", "CCBOARD_ALLOWED_USERS": "me@example.com", "CCBOARD_PUBLIC_URL": "https://pc.tail1234.ts.net"}


def test_the_wsl_block_does_nothing_on_a_plain_linux_host(tmp_path):
    """The pre-change baseline: no output, no exit, no systemctl call, nothing set (the placement stays empty)."""
    rc, out, err = run_block(tmp_path, GUARD, proc_version=UBUNTU_VERSION, extra_env={"FAKE_SYSTEMD": "offline", "PROJECTS_DIR": "/mnt/c/p"})
    assert (rc, err) == (0, "")
    assert out == "PLACEMENT=[] FQDN=[] URL=[]\n"
    assert not (tmp_path / "calls.log").exists()
    rc, out, err = run_block(tmp_path, GUARD, proc_version="", extra_env={"FAKE_SYSTEMD": "offline"})            # /proc/version unreadable
    assert (rc, out, err) == (0, "PLACEMENT=[] FQDN=[] URL=[]\n", "")


def test_a_placement_outside_wsl_is_ignored_with_a_warning_and_not_remembered(tmp_path):
    rc, out, err = run_block(tmp_path, GUARD, proc_version=UBUNTU_VERSION, extra_env={"CCBOARD_TAILSCALE_PLACEMENT": "host"})
    assert rc == 0 and "for WSL only; ignored" in err
    assert "PLACEMENT=[]" in out


def test_without_systemd_the_installer_refuses_early_with_the_recipe(tmp_path):
    for state in ("offline", "", "starting", "maintenance"):
        rc, out, err = run_block(tmp_path, GUARD, extra_env={"FAKE_SYSTEMD": state, "FAKE_SYSTEMCTL_RC": "1"})
        assert rc == 1, state
        assert "systemd is not running in this WSL distro" in err
        assert "[boot]" in err and "systemd=true" in err and "/etc/wsl.conf" in err and "wsl --shutdown" in err
        assert "systemctl is-system-running" in err
        assert "PLACEMENT" not in out                                                  # it stopped before anything else
    rc, out, err = run_block(tmp_path, GUARD, extra_env={"PATH": "/usr/bin:/bin", "FAKE_SYSTEMD": "running"})
    assert rc == 1 and "'nothing'" in err or "systemd is not running" in err          # no systemctl at all (a PATH without the stub)


@pytest.mark.parametrize("state,rc_", [("running", None), ("degraded", "1")])
def test_running_and_degraded_systemd_pass(tmp_path, state, rc_):
    env = {"FAKE_SYSTEMD": state, "HAVE_TAILSCALE": "1"}
    if rc_:
        env["FAKE_SYSTEMCTL_RC"] = rc_                    # systemctl exits 1 for degraded
    rc, out, err = run_block(tmp_path, GUARD, extra_env=env)
    assert rc == 0 and err == ""
    assert "PLACEMENT=[wsl]" in out


def test_paths_and_agents_on_a_windows_drive_are_warned_about_not_refused(tmp_path):
    rc, out, err = run_block(tmp_path, GUARD, extra_env={
        "HAVE_TAILSCALE": "1", "PROJECTS_DIR": "/mnt/c/work/projects", "CCBOARD_DATA_DIR": "/mnt/d/ccboard-data",
        "APP_DIR": "/mnt/c/src/ccboard", "FAKE_AGENT_PATH": "/mnt/c/tools/npm/claude"})
    assert rc == 0
    assert "PROJECTS_DIR is on a Windows drive (/mnt/c/work/projects)" in err
    assert "CCBOARD_DATA_DIR is on a Windows drive (/mnt/d/ccboard-data)" in err
    assert "the ccboard checkout is on a Windows drive (/mnt/c/src/ccboard)" in err
    assert "claude resolves to /mnt/c/tools/npm/claude" in err and "appendWindowsPath=false" in err
    assert err.count("warning:") == 4
    rc, out, err = run_block(tmp_path, GUARD, extra_env={"HAVE_TAILSCALE": "1"})        # the clean case prints no warning
    assert rc == 0 and err == ""


def test_the_placement_defaults_to_wsl_with_tailscale_in_the_distro_and_host_without(tmp_path):
    rc, out, err = run_block(tmp_path, GUARD, extra_env={"HAVE_TAILSCALE": "1"})
    assert rc == 0 and "PLACEMENT=[wsl]" in out and "FQDN=[]" in out                   # the old path derives the name from tailscale later
    rc, out, err = run_block(tmp_path, GUARD, extra_env={"CCBOARD_ALLOWED_USERS": "me@example.com", "CCBOARD_PUBLIC_URL": "https://pc.tail1234.ts.net"})
    assert rc == 0 and "PLACEMENT=[host]" in out
    rc, out, err = run_block(tmp_path, GUARD, extra_env={"CCBOARD_TAILSCALE_PLACEMENT": "wsl"})      # explicit wsl wins even without the command
    assert rc == 0 and "PLACEMENT=[wsl]" in out
    rc, out, err = run_block(tmp_path, GUARD, extra_env={"CCBOARD_TAILSCALE_PLACEMENT": "elsewhere"})
    assert rc == 1 and "must be wsl or host" in err


def test_host_placement_demands_the_login_and_the_address(tmp_path):
    rc, out, err = run_block(tmp_path, GUARD, extra_env={**HOST, "CCBOARD_ALLOWED_USERS": ""})
    assert rc == 1 and "CCBOARD_ALLOWED_USERS" in err and "Tailscale runs on Windows" in err
    rc, out, err = run_block(tmp_path, GUARD, extra_env={**HOST, "CCBOARD_PUBLIC_URL": ""})
    assert rc == 1 and "CCBOARD_PUBLIC_URL" in err and "tailscale status" in err
    for bad in ("http://pc.tail1234.ts.net", "https://pc.tail1234.ts.net/path", "pc.tail1234.ts.net", "https://"):
        rc, out, err = run_block(tmp_path, GUARD, extra_env={**HOST, "CCBOARD_PUBLIC_URL": bad})
        assert rc == 1 and "CCBOARD_PUBLIC_URL must look like" in err, bad


def test_host_placement_derives_the_name_and_skips_nothing_it_must_keep(tmp_path):
    rc, out, err = run_block(tmp_path, GUARD, extra_env=HOST)
    assert rc == 0 and err == ""
    assert "PLACEMENT=[host] FQDN=[pc.tail1234.ts.net] URL=[https://pc.tail1234.ts.net]" in out
    assert "tailscale checks and the serve mappings are skipped" in out
    rc, out, err = run_block(tmp_path, GUARD, extra_env={**HOST, "CCBOARD_PUBLIC_URL": "https://pc.tail1234.ts.net:8443/", "CCBOARD_HTTPS_PORT": "8443"})
    assert rc == 0 and err == "" and "URL=[https://pc.tail1234.ts.net:8443]" in out    # the port stays, the slash goes
    rc, out, err = run_block(tmp_path, GUARD, extra_env={**HOST, "CCBOARD_PUBLIC_URL": "https://pc.tail1234.ts.net:9443"})
    assert rc == 0 and "names port 9443 but CCBOARD_HTTPS_PORT is 443" in err           # a mismatch is said aloud
    rc, out, err = run_block(tmp_path, GUARD, extra_env={**HOST, "CCBOARD_RUNTIME": "docker"})
    assert rc == 1 and "cannot be combined with CCBOARD_TAILSCALE_PLACEMENT=host" in err


def test_host_placement_prints_one_powershell_line_per_mapping(tmp_path):
    rc, out, err = run_block(tmp_path, "wsl_host_serve_lines", extra_env={"CCBOARD_HTTPS_PORT": "8443", "CODE_HTTPS_PORT": "10000"})
    assert rc == 0
    assert out.splitlines() == [
        "  tailscale serve --bg --https=8443 http://127.0.0.1:8000",
        "  tailscale serve --bg --https=8443 --set-path /tty http://127.0.0.1:7681",
        "  tailscale serve --bg --https=10000 http://127.0.0.1:8080",
    ]
    rc, out, err = run_block(tmp_path, "wsl_host_serve_lines", extra_env={"NTFY_URL": "http://127.0.0.1:2586", "CCBOARD_MEM_HTTPS_PORT": "9500"})
    assert len(out.splitlines()) == 5
    assert "  tailscale serve --bg --https=8444 http://127.0.0.1:2586" in out.splitlines()
    assert "  tailscale serve --bg --https=9500 http://127.0.0.1:37711" in out.splitlines()
    assert "reset" not in out and "funnel" not in out


def test_the_serve_note_says_where_to_run_them(tmp_path):
    rc, out, err = run_block(tmp_path, "wsl_host_serve_note")
    assert rc == 0 and "PowerShell on Windows" in out and "nothing was mapped from this distro" in out
    assert out.count("tailscale serve --bg") == 3 and "tailscale serve status" in out


def test_install_sh_wires_the_branch_in_the_right_places():
    t = _text()
    assert t.count('wsl_guard "$(cat /proc/version 2>/dev/null || true)"') == 1
    i_docker = t.index('docker_host_guard "$(cat /proc/version')
    i_call = t.index('wsl_guard "$(cat /proc/version')
    i_head = t.index("# ---------------------------------------------------------------- tailscale checks (before touching anything)")
    i_tscheck = t.index("have tailscale || die")
    i_serve_head = t.index("# ---------------------------------------------------------------- tailscale serve")
    assert t.index("# >>> wsl branch") < i_call and i_docker < i_call < i_head < i_tscheck < i_serve_head
    # the Tailscale checks sit in the else of the host placement, untouched: the first line after `else` is the old first line
    assert t[i_head:i_tscheck].rstrip().endswith('else')
    assert t[t.index("mem_viewer_precheck\n"):].startswith("mem_viewer_precheck\nfi\n")
    # every mapping is skipped for host, and the old calls are still there, in order
    seg = t[t.index('if [ "${CCBOARD_TAILSCALE_PLACEMENT:-}" = host ]; then\n  wsl_host_serve_note'):]
    seg = seg[:seg.index("# ---------------------------------------------------------------- health")]
    assert "else\nserve_apply \"$CCBOARD_HTTPS_PORT\" / " in seg
    for call in ('serve_apply "$CCBOARD_HTTPS_PORT" /tty', 'serve_apply "$CODE_HTTPS_PORT" /', "mem_viewer_step\n", "serve_check $spec"):
        assert call in seg
    assert seg.rstrip().endswith("fi")


def test_the_placement_is_remembered_only_when_it_is_set():
    t = _text()
    m = re.search(r"^ENV_KEYS=\((.*)\)$", t, re.M)
    assert "CCBOARD_TAILSCALE_PLACEMENT" in m.group(1).split()
    assert 'CCBOARD_TAILSCALE_PLACEMENT' in config.ENV_FILE_KEYS
    loop = re.search(r'^env_body=""\n(for k in "\$\{ENV_KEYS\[@\]\}"; do.*?\ndone)\n', t, re.S | re.M).group(1)
    script = f'set -euo pipefail\nENV_KEYS=(A B CCBOARD_TAILSCALE_PLACEMENT C)\nA=1 B=2 C=3\nCCBOARD_TAILSCALE_PLACEMENT="$P"\nenv_body=""\n{loop}\nprintf %s "$env_body"\n'
    out = subprocess.run(["bash", "-c", script], env={"PATH": "/usr/bin:/bin", "P": ""}, capture_output=True, text=True).stdout
    assert out == "A=1\nB=2\nC=3\n"                                                    # empty: the env file is the one every other system always had
    out = subprocess.run(["bash", "-c", script], env={"PATH": "/usr/bin:/bin", "P": "host"}, capture_output=True, text=True).stdout
    assert out == "A=1\nB=2\nCCBOARD_TAILSCALE_PLACEMENT=host\nC=3\n"


def test_install_sh_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(INSTALL)], capture_output=True).returncode == 0


# ================================================================== doctor checks

class FakeDB:
    def __init__(self, rows=None):
        self.kv = {}
        self.sets = 0
        if rows is not None:
            self.kv[dw.KV_STARTS] = {"value": {"starts": rows}, "at": "x"}

    def kv_get(self, key):
        return self.kv.get(key)

    def kv_set(self, key, value, at=None):
        self.sets += 1
        self.kv[key] = {"value": value, "at": "x"}


@pytest.fixture
def wsl(monkeypatch, tmp_path):
    """A board inside WSL: the doctor's subprocess helper is a table, the platform answers are fakes, every folder is on the distro's disk."""
    class W:
        cmds = {}
        calls = []
    w = W()
    w.cmds = {"systemctl": doctor.Proc(0, "running\n", ""), "ss": doctor.Proc(0, "LISTEN 0 4096 127.0.0.1:8000 0.0.0.0:*\n"
                                                                                  "LISTEN 0 4096 127.0.0.1:7681 0.0.0.0:*\n"
                                                                                  "LISTEN 0 4096 [::1]:8080 [::]:*\n", "")}
    w.calls = []

    def fake_run(argv, timeout=doctor.CMD_TIMEOUT):
        w.calls.append(list(argv))
        r = w.cmds.get(Path(argv[0]).name)
        if isinstance(r, Exception):
            raise r
        if r is None:
            raise doctor.ToolMissing(Path(argv[0]).name)
        return r
    monkeypatch.setattr(doctor, "_run", fake_run)
    monkeypatch.setattr(plat, "is_wsl", lambda: True)
    monkeypatch.setattr(plat, "wsl_networking_mode", lambda: "nat")
    monkeypatch.setattr(plat, "wsl_interop", lambda: True)
    monkeypatch.setattr(plat, "PROC_MOUNTS", tmp_path / "mounts")
    (tmp_path / "mounts").write_text(MOUNTS)
    for name in ("projects_dir", "data_dir", "claude_config_dir", "codex_home"):
        monkeypatch.setattr(settings, name, tmp_path / name)
    monkeypatch.setattr(settings, "port", 8000)
    monkeypatch.setattr(settings, "ttyd_port", 7681)
    monkeypatch.setattr(settings, "code_server_port", 8080)
    monkeypatch.setattr(settings, "claude_bin", lambda: "/home/dev/.local/bin/claude")
    monkeypatch.setattr(settings, "codex_bin", lambda: "/home/dev/.local/bin/codex")
    monkeypatch.setattr(dw, "_powershell", lambda: "powershell.exe")
    monkeypatch.setattr(dw, "_seen_id", None)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    return w


def test_checks_register_on_wsl_only_in_group_box(monkeypatch):
    monkeypatch.setattr(doctor, "CHECKS", [])
    monkeypatch.setattr(doctor, "GROUPS", ["box"])
    from app import samples
    monkeypatch.setattr(samples, "TICK_HOOKS", [])
    dw.register_all()
    dw.register_all()                                                                   # twice: replaced, never duplicated
    ids = [c[0] for c in doctor.CHECKS]
    assert ids == ["wsl-systemd", "wsl-files", "wsl-path", "wsl-network", "wsl-keepalive", "wsl-restarts", "wsl-placement"]
    assert {c[1] for c in doctor.CHECKS} == {"box"} and all(c[2] for c in doctor.CHECKS)
    assert samples.TICK_HOOKS == [dw._tick]


def test_a_board_outside_wsl_lists_no_wsl_check():
    if plat.is_wsl():
        pytest.skip("this test machine is itself WSL")
    assert [c[0] for c in doctor.CHECKS if c[0].startswith("wsl-")] == []
    from app import samples
    assert dw._tick not in samples.TICK_HOOKS


def test_every_check_skips_off_wsl_without_running_anything(monkeypatch, wsl):
    monkeypatch.setattr(plat, "is_wsl", lambda: False)
    for check_id, _g, _l, fn in dw.CHECKS:
        out = fn(FakeDB())
        assert out.status == "skip" and "WSL" in out.detail, check_id
    assert wsl.calls == []


def test_systemd_check(wsl):
    assert dw._c_systemd(None).status == "pass"
    wsl.cmds["systemctl"] = doctor.Proc(1, "degraded\n", "")
    out = dw._c_systemd(None)
    assert out.status == "pass" and "degraded" in out.detail
    wsl.cmds["systemctl"] = doctor.Proc(1, "offline\n", "")
    out = dw._c_systemd(None)
    assert out.status == "fail" and "/etc/wsl.conf" in out.fix["text"] and "wsl --shutdown" in out.fix["text"]
    wsl.cmds["systemctl"] = doctor.Proc(1, "", "")
    assert dw._c_systemd(None).status == "fail"
    wsl.cmds["systemctl"] = doctor.Proc(1, "starting\n", "")
    assert dw._c_systemd(None).status == "warn"
    del wsl.cmds["systemctl"]
    assert dw._c_systemd(None).status == "skip"
    wsl.cmds["systemctl"] = doctor.ToolTimeout("systemctl")
    assert dw._c_systemd(None).status == "warn"


def test_files_check(wsl, monkeypatch, tmp_path):
    assert dw._c_files(None).status == "pass"
    monkeypatch.setattr(settings, "projects_dir", Path("/mnt/c/work/projects"))
    out = dw._c_files(None)
    assert out.status == "warn" and "the projects folder (/mnt/c/work/projects)" in out.detail and "ext4" in out.fix["text"]
    monkeypatch.setattr(settings, "projects_dir", tmp_path / "projects_dir")
    monkeypatch.setattr(settings, "data_dir", Path("/data/win drive/ccboard"))        # a drive mounted outside /mnt/, found in the mount table
    out = dw._c_files(None)
    assert out.status == "warn" and "the data folder" in out.detail and "projects" not in out.detail
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data_dir")
    monkeypatch.setattr(settings, "claude_config_dir", Path("/mnt/c/claude"))
    assert "the Claude config folder" in dw._c_files(None).detail


def test_path_check(wsl, tmp_path, monkeypatch):
    out = dw._c_path(None)
    assert out.status == "pass" and "claude at /home/dev/.local/bin/claude" in out.detail
    monkeypatch.setattr(settings, "claude_bin", lambda: "/mnt/c/tools/npm/claude")
    out = dw._c_path(None)
    assert out.status == "warn" and "claude resolve" in out.detail and "appendWindowsPath=false" in out.fix["text"]
    link = tmp_path / "claude"
    link.symlink_to("/mnt/c/tools/npm/claude")                                          # a link inside the distro that leads to Windows
    monkeypatch.setattr(settings, "claude_bin", lambda: str(link))
    assert dw._c_path(None).status == "warn"
    monkeypatch.setattr(settings, "claude_bin", lambda: None)
    monkeypatch.setattr(settings, "codex_bin", lambda: "/usr/local/bin/codex")
    assert dw._c_path(None).status == "pass"
    monkeypatch.setattr(settings, "codex_bin", lambda: None)
    assert dw._c_path(None).status == "skip"


def test_network_check(wsl, monkeypatch):
    out = dw._c_network(None)
    assert out.status == "pass" and "NAT networking" in out.detail and "loopback only" in out.detail
    monkeypatch.setattr(plat, "wsl_networking_mode", lambda: "mirrored")
    assert "mirrored networking" in dw._c_network(None).detail
    wsl.cmds["ss"] = doctor.Proc(0, "LISTEN 0 4096 0.0.0.0:8000 0.0.0.0:*\nLISTEN 0 4096 *:7681 *:*\nLISTEN 0 128 [::]:8080 [::]:*\nLISTEN 0 1 0.0.0.0:22 0.0.0.0:*\n", "")
    out = dw._c_network(None)
    assert out.status == "warn" and "0.0.0.0:8000" in out.detail and "*:7681" in out.detail and "[::]:8080" in out.detail and ":22" not in out.detail
    wsl.cmds["ss"] = doctor.Proc(0, "LISTEN 0 4096 127.0.0.1:8000 0.0.0.0:*\nLISTEN 0 1 0.0.0.0:22 0.0.0.0:*\n", "")
    assert dw._c_network(None).status == "pass"                                         # other programs' listeners are not ours to judge
    monkeypatch.setattr(plat, "wsl_networking_mode", lambda: "unknown")
    out = dw._c_network(None)
    assert out.status == "pass" and "not reported" in out.detail and "wsl --update" in out.detail
    monkeypatch.setattr(plat, "wsl_networking_mode", lambda: "bridged")
    assert dw._c_network(None).status == "warn"
    del wsl.cmds["ss"]
    monkeypatch.setattr(plat, "wsl_networking_mode", lambda: "nat")
    out = dw._c_network(None)
    assert out.status == "pass" and "could not be read" in out.detail


TASK_OUT = "\nTaskPath   TaskName                  State\n--------   --------                  -----\n\\          ccboard-wsl-keepalive     {state}\n\n"


def test_keepalive_asks_windows_through_interop_and_never_passes_blind(wsl, monkeypatch):
    wsl.cmds["powershell.exe"] = doctor.Proc(0, TASK_OUT.format(state="Running"), "")
    out = dw._c_keepalive(None)
    assert out.status == "pass" and "registered and running" in out.detail
    assert wsl.calls[-1] == ["powershell.exe", "-NoProfile", "-Command", "Get-ScheduledTask -TaskName ccboard-wsl-keepalive"]
    wsl.cmds["powershell.exe"] = doctor.Proc(0, TASK_OUT.format(state="Ready"), "")
    out = dw._c_keepalive(None)
    assert out.status == "warn" and "not running now" in out.detail and "Start-ScheduledTask" in out.fix["text"]
    wsl.cmds["powershell.exe"] = doctor.Proc(0, TASK_OUT.format(state="Disabled"), "")
    assert dw._c_keepalive(None).status == "warn"
    wsl.cmds["powershell.exe"] = doctor.Proc(1, "", "Get-ScheduledTask : No MSFT_ScheduledTask objects found with property 'TaskName' equal to 'ccboard-wsl-keepalive'.")
    out = dw._c_keepalive(None)
    assert out.status == "warn" and "nothing keeps the distro running" in out.detail
    assert "ccboard-wsl-keepalive.ps1" in out.fix["text"] and "-Distro Ubuntu" in out.fix["cmd"]
    wsl.cmds["powershell.exe"] = doctor.ToolTimeout("powershell.exe", 4.0)
    assert dw._c_keepalive(None).status == "warn"
    # interop off, or no powershell.exe to be found: unknown (a skip with the fix), never pass
    monkeypatch.setattr(plat, "wsl_interop", lambda: False)
    out = dw._c_keepalive(None)
    assert out.status == "skip" and "unknown" in out.detail and "[interop]" in out.fix["text"]
    monkeypatch.setattr(plat, "wsl_interop", lambda: True)
    monkeypatch.setattr(dw, "_powershell", lambda: None)
    assert dw._c_keepalive(None).status == "skip"


def test_powershell_is_found_on_path_or_at_the_system_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(dw.shutil, "which", lambda n: "/mnt/c/Windows/System32/powershell.exe" if n == "powershell.exe" else None)
    assert dw._powershell() == "/mnt/c/Windows/System32/powershell.exe"
    fallback = tmp_path / "powershell.exe"
    monkeypatch.setattr(dw.shutil, "which", lambda n: None)                              # appendWindowsPath=false keeps Windows off PATH
    monkeypatch.setattr(dw, "POWERSHELL_FALLBACK", fallback)
    assert dw._powershell() is None
    fallback.write_text("")
    assert dw._powershell() == str(fallback)


def test_the_starts_are_recorded_once_per_init_and_survive_garbage(monkeypatch):
    db = FakeDB()
    ids = iter([("aaaa:10", 1000.0), ("aaaa:10", 1000.0), ("aaaa:77", 5000.0), ("bbbb:10", 9000.0)])
    monkeypatch.setattr(plat, "init_identity", lambda: next(ids))
    monkeypatch.setattr(dw, "_seen_id", None)
    assert dw.record_start(db) is True                                                   # the first sighting
    assert dw.record_start(db) is False and db.sets == 1                                 # the same init: no write
    assert dw.record_start(db) is True                                                   # the distro restarted (new init, same VM)
    assert dw.record_start(db) is True                                                   # the VM restarted
    assert [e["id"] for e in dw._starts(db)] == ["aaaa:10", "aaaa:77", "bbbb:10"]
    # a new process (an empty memo) over a database that already holds the latest init writes nothing
    monkeypatch.setattr(plat, "init_identity", lambda: ("bbbb:10", 9000.0))
    monkeypatch.setattr(dw, "_seen_id", None)
    sets = db.sets
    assert dw.record_start(db) is False and db.sets == sets
    # rows of any other shape are ignored, never an error
    db.kv[dw.KV_STARTS] = {"value": {"starts": [{"id": 5, "at": 1}, "x", {"id": "k", "at": "no"}, {"id": "ok", "at": 7}]}, "at": "x"}
    assert dw._starts(db) == [{"id": "ok", "at": 7.0}]
    db.kv[dw.KV_STARTS] = {"value": "text", "at": "x"}
    assert dw._starts(db) == []
    assert dw._starts(None) == []


def test_the_history_is_capped(monkeypatch):
    db = FakeDB([{"id": f"x:{i}", "at": float(i)} for i in range(dw.KEEP_STARTS)])
    monkeypatch.setattr(plat, "init_identity", lambda: ("new:1", 99999.0))
    monkeypatch.setattr(dw, "_seen_id", None)
    assert dw.record_start(db) is True
    rows = dw._starts(db)
    assert len(rows) == dw.KEEP_STARTS and rows[-1]["id"] == "new:1" and rows[0]["id"] == "x:1"


def test_in_the_docker_runtime_only_a_vm_restart_counts(monkeypatch):
    """Inside the container PID 1 is the entrypoint: every image swap is a new PID 1 tick with the same boot id, and that is no restart of the distro."""
    monkeypatch.setattr(settings, "runtime", "docker")
    db = FakeDB()
    ids = iter([("aaaa:10", 1000.0), ("aaaa:55", 2000.0), ("bbbb:10", 3000.0)])
    monkeypatch.setattr(plat, "init_identity", lambda: next(ids))
    monkeypatch.setattr(dw, "_seen_id", None)
    assert dw.record_start(db) is True
    assert dw.record_start(db) is False                                                  # a container recreate
    assert dw.record_start(db) is True                                                   # the machine restarted
    assert [e["id"] for e in dw._starts(db)] == ["aaaa", "bbbb"]


def _import_as_wsl(tmp_path, code):
    """Run `code` in a fresh interpreter whose platform module says WSL, from the checkout, with a throwaway data folder."""
    prelude = "import app.platform as p; p.is_wsl.cache_clear(); p.is_wsl = lambda: True\n"
    env = {**os.environ, "CCBOARD_DATA_DIR": str(tmp_path / "data"), "PROJECTS_DIR": str(tmp_path / "projects"), "CCBOARD_TEST_NO_CLAUDE": "1",
           "HOME": str(tmp_path), "CCBOARD_ALLOWED_USERS": "a@example.com"}
    env.pop("CCBOARD_ENV_FILE", None)
    return subprocess.run([sys.executable, "-c", prelude + code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("first", ["import app.doctor as d, app.doctor_wsl as w", "import app.doctor_wsl as w, app.doctor as d", "import app.main; import app.doctor as d, app.doctor_wsl as w"])
def test_importing_the_board_as_wsl_registers_the_checks_once_in_any_order(tmp_path, first):
    code = (first + "\nassert w.d is d\nids = [c[0] for c in d.CHECKS if c[0].startswith('wsl-')]\n"
            "assert sorted(ids) == sorted(set(ids)) and len(ids) == 7, ids\nimport app.samples as s\nassert s.TICK_HOOKS.count(w._tick) == 1\nprint('ok')")
    r = _import_as_wsl(tmp_path, code)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr[-800:]


def test_the_tick_hook_never_raises_and_is_quiet_off_wsl(monkeypatch):
    def boom():
        raise RuntimeError("no")
    monkeypatch.setattr(plat, "is_wsl", lambda: True)
    monkeypatch.setattr(plat, "init_identity", boom)
    dw._tick(FakeDB(), 0)                                                                # swallowed
    monkeypatch.setattr(plat, "is_wsl", lambda: False)
    monkeypatch.setattr(plat, "init_identity", lambda: pytest.fail("read /proc off WSL"))
    dw._tick(FakeDB(), 0)


NOW = 1_800_000_000.0


@pytest.fixture
def clock(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(doctor, "_utcnow", lambda: datetime.fromtimestamp(NOW, tz=timezone.utc))


def test_restarts_check(wsl, clock):
    hour = 3600
    long_ago = {"id": "a:1", "at": NOW - 30 * hour}
    assert dw._c_restarts(FakeDB()).status == "skip"                                     # no history yet
    out = dw._c_restarts(FakeDB([long_ago]))
    assert out.status == "pass" and "no restart in the last 24 hours" in out.detail
    out = dw._c_restarts(FakeDB([long_ago, {"id": "a:2", "at": NOW - 5 * hour}]))
    assert out.status == "pass" and "1 restart in the last 24 hours" in out.detail and "5 h ago" in out.detail
    out = dw._c_restarts(FakeDB([long_ago, {"id": "a:2", "at": NOW - 40 * hour}]))       # older than a day: not counted
    assert out.status == "pass" and "no restart" in out.detail
    two = FakeDB([long_ago, {"id": "a:2", "at": NOW - 5 * hour}, {"id": "b:1", "at": NOW - 600}])
    out = dw._c_restarts(two)
    assert out.status == "warn" and "restarted 2 times" in out.detail and "10 min ago" in out.detail
    assert "cannot report its own stop" in out.detail and "the board stops with it" in out.detail      # the distro cannot report its own stop
    assert "keep-alive" in out.fix["text"] and "-Distro Ubuntu" in out.fix["cmd"]
    # the first sighting is not a restart even when it is recent: two rows in the last day are one restart
    out = dw._c_restarts(FakeDB([{"id": "a:1", "at": NOW - 2 * hour}, {"id": "a:2", "at": NOW - 1 * hour}]))
    assert out.status == "pass" and "1 restart" in out.detail


def test_restarts_check_without_a_database(wsl, clock):
    assert dw._c_restarts(None).status == "skip"


def test_every_row_keeps_the_stop_sentence_where_it_matters(wsl, clock):
    assert "cannot report its own stop" in dw._c_restarts(FakeDB()).detail
    assert "cannot report its own stop" in dw._c_restarts(FakeDB([{"id": "a:1", "at": NOW}])).detail


def test_placement_check(wsl, monkeypatch):
    monkeypatch.setattr(plat, "tailscale_placement", lambda *a, **k: "wsl")
    out = dw._c_placement(None)
    assert out.status == "pass" and "inside this distro" in out.detail
    monkeypatch.setattr(plat, "tailscale_placement", lambda *a, **k: "host")
    monkeypatch.setattr(ts, "find_cli", lambda: ts.Cli(["/usr/bin/tailscale"], {}))
    monkeypatch.setattr(plat, "IS_LINUX", True)
    monkeypatch.setattr(plat, "IS_MACOS", False)                                         # ts.variant() looks at the system flags first
    out = dw._c_placement(None)
    assert out.status == "skip" and out.detail.startswith("previews are unavailable") and "Windows host" in out.detail
    assert "CCBOARD_TAILSCALE_PLACEMENT=wsl" in out.fix["text"]
    monkeypatch.setattr(ts, "find_cli", lambda: ts.Cli(["/mnt/c/Program Files/Tailscale/tailscale.exe"], {}))
    out = dw._c_placement(None)
    assert out.status == "pass" and "tailscale.exe" in out.detail and "to verify" in out.detail


def test_the_tailscale_row_is_a_skip_for_a_host_placement_and_unchanged_elsewhere(wsl, monkeypatch):
    monkeypatch.setattr(ts, "find_cli", lambda: ts.Cli(["/usr/bin/tailscale"], {}))
    monkeypatch.setattr(doctor.plat, "IS_LINUX", True)
    wsl.cmds["tailscale"] = doctor.ToolMissing("tailscale")
    monkeypatch.setenv("CCBOARD_TAILSCALE_PLACEMENT", "host")
    out = doctor._c_tailscale(None)
    assert out.status == "skip" and "Windows host" in out.detail
    monkeypatch.delenv("CCBOARD_TAILSCALE_PLACEMENT")
    out = doctor._c_tailscale(None)
    assert out.status == "fail"                                                           # unset: a missing tailscale is still a failure
    monkeypatch.setenv("CCBOARD_TAILSCALE_PLACEMENT", "host")
    monkeypatch.setattr(plat, "is_wsl", lambda: False)
    assert doctor._c_tailscale(None).status == "fail"                                     # outside WSL the setting means nothing


def test_a_check_never_raises(wsl, monkeypatch):
    monkeypatch.setattr(plat, "wsl_networking_mode", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    out = dw._c_network(None)
    assert out.status == "warn" and "RuntimeError" in out.detail


# ================================================================== the PowerShell script (static: pwsh is not available here)

def ps():
    return PS1.read_text()


def test_the_script_exists_and_names_nothing_personal():
    t = ps()
    assert PS1.is_file() and len(t) > 2000
    assert not re.search(r"[A-Za-z]:\\Users\\", t), "a Windows user path"
    assert ("/Us" + "ers/") not in t and "/home/" not in t
    assert not re.search(r"[\w.+-]+@[\w-]+\.\w+", t), "an e-mail address"
    assert "%UserProfile%" in t or "$env:USERPROFILE" in t


def test_the_script_declares_the_documented_parameters():
    t = ps()
    param = t[t.index("param("):t.index("$ErrorActionPreference")]
    for name in ("$Distro", "$Method", "$Remove", "$WriteWslConfig", "$Mirrored"):
        assert name in param, name
    assert "[switch]$Remove" in param and "[switch]$WriteWslConfig" in param
    assert "ValidateSet('sleep', 'dbus')" in param and "$Method = 'sleep'" in param
    assert "ValidatePattern" in param                                                     # a distro name cannot carry command text into the task
    assert "-Remove" in t.split("#>")[0] and "Unregister-ScheduledTask" in t


def test_the_script_is_a_per_user_task_without_administrator_rights():
    t = ps()
    assert "-AtLogOn" in t and "-RunLevel Limited" in t and "-LogonType Interactive" in t
    assert "Highest" not in t and "RunAs" not in t and "-Verb" not in t and "Start-Process" not in t
    assert "#Requires -RunAsAdministrator" not in t and "IsInRole" not in t
    assert "ccboard-wsl-keepalive" in t
    assert "-WindowStyle Hidden" in t
    assert "sleep infinity" in t and "dbus-launch true" in t
    assert "Register-ScheduledTask" in t and "Remove-KeepAliveTask" in t


def test_the_script_is_idempotent_and_exits_zero_for_a_missing_distro():
    t = ps()
    reg = t.index("Register-ScheduledTask -TaskName")
    assert t.index("$replaced = Remove-KeepAliveTask") < reg                              # an existing task is removed first: never two
    missing = t[t.index("if ($distros -notcontains $Distro)"):reg]
    assert "exit 0" in missing and "Nothing was changed" in missing
    assert "wsl.exe was not found" in t and t.count("exit 0") >= 5
    assert "exit 1" not in t and "throw" not in t


def test_the_script_writes_wslconfig_only_inside_the_write_switch_after_a_backup():
    t = ps()
    branch = t.index("if ($WriteWslConfig) {")
    writers = [m.start() for m in re.finditer(r"Set-Content|Out-File|WriteAllText|Add-Content|\[IO\.File\]::Write|>\s*\$configPath", t)]
    assert writers, "the script writes nothing at all"
    assert all(w > branch for w in writers), "a write to .wslconfig outside the -WriteWslConfig branch"
    assert t.count("Set-Content") == 1
    assert t.index("Copy-Item -LiteralPath $configPath") < t.index("Set-Content -LiteralPath $configPath")    # backup first
    assert t.index("Copy-Item -LiteralPath $configPath") > branch
    assert "instanceIdleTimeout" in t and "vmIdleTimeout" in t and "networkingMode" in t
    assert "Mirrored" in t[t.index("$wanted = @("):branch]                                 # mirrored only when asked for
    # the -Remove path returns before any of it
    assert t.index("if ($Remove) {") < t.index("Get-WslDistros") < branch


def test_the_script_never_edits_anything_else():
    t = ps()
    for banned in ("Set-ItemProperty", "New-ItemProperty", "reg add", "Set-ExecutionPolicy", "Remove-Item", "netsh", "Enable-WindowsOptionalFeature",
                   "Set-NetFirewall", "New-NetFirewall", "Invoke-WebRequest", "Invoke-Expression", "iex ", "wsl.exe --shutdown", "--unregister", "--terminate"):
        assert banned not in t, banned
    # wsl.exe is only ever called to list distros, hold one open (inside the task's text) or print advice: no command starts a stop or an export
    assert not re.search(r"^\s*(&\s*)?wsl(\.exe)?\s+--(shutdown|terminate|unregister|export|import|install|update)", t, re.M)
    assert t.count("& wsl.exe -l -q") == 1


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh is not installed")
def test_the_script_parses_with_pwsh():
    cmd = f"[void][scriptblock]::Create((Get-Content -Raw '{PS1}'))"
    r = subprocess.run(["pwsh", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


# ================================================================== the README

def readme():
    return README.read_text()


def test_the_readme_section_states_the_support_and_the_recipe():
    t = readme()
    sec = t[t.index("## Windows (WSL2) (issue #118)"):t.index("## Run the board as a container")]
    for needle in ("Windows 11 22H2 or newer", "Windows 10 build 19041", "best effort", "NAT networking only", "UNVERIFIED",
                   "wsl --install -d Ubuntu", "wsl --update", "wsl -l -v", "systemd=true", "wsl --shutdown", "systemctl is-system-running",
                   "ccboard-wsl-keepalive.ps1", "-Remove", "-WriteWslConfig", "-Method dbus", "instanceIdleTimeout", "vmIdleTimeout",
                   "do NOT keep a WSL instance alive", "which claude codex", "bubblewrap", "wsl --export <distro> <file>.vhd --format vhd",
                   "CCBOARD_TAILSCALE_PLACEMENT", "networkingMode=mirrored", "netsh interface portproxy", "wsl-keepalive", "wsl-restarts",
                   "cannot say that the distro is stopped", "issue #124", "WSL1", "Docker Desktop", "/mnt/c", "auto-logon"):
        assert needle in sec, needle
    assert "8-hour" not in t and "8 hour" not in t and "eight-hour" not in t.lower()       # Microsoft documents no such timer
    assert "binds to `127.0.0.1` only" in sec or "bind to `127.0.0.1` only" in sec
    assert "http://localhost:<port>" in sec and "403" in sec and "issue #127" in sec
    assert "—" not in sec                                                                  # plain words, no em dashes


def test_the_readme_lists_the_placement_setting_and_the_doctor_rows():
    t = readme()
    assert "| `CCBOARD_TAILSCALE_PLACEMENT` |" in t[t.index("### Settings"):t.index("### What install.sh sets up")]
    for cid, _g, _l, _f in dw.CHECKS:
        assert f"`{cid}`" in t, cid


def test_the_script_and_the_readme_agree_on_names():
    t, sec = ps(), readme()
    assert dw.TASK_NAME == "ccboard-wsl-keepalive"
    assert f"$TaskName = '{dw.TASK_NAME}'" in t
    assert f"Get-ScheduledTask -TaskName {dw.TASK_NAME}" in sec
    assert Path(dw.KEEPALIVE_CMD.split("-File .\\")[1].split()[0]).name == PS1.name
