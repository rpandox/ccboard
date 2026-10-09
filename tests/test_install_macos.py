"""Issue #117 (the macOS installer), #120 (the container runtime is refused on a Mac) and #121 (the MCP shim uses the board's venv python).

scripts/install-macos.sh and scripts/uninstall-macos.sh are run for real, under the host's bash, against fakes: every external tool (uname,
sw_vers, id, brew, launchctl, tailscale, tmux, ttyd, curl, claude, codex, python3) is a small script in a temp directory that is the WHOLE of
PATH, HOME is a temp directory, and the checkout is a folder of symlinks. Nothing touches the real ~/.claude, ~/.codex, ~/Library, launchctl,
Homebrew, Tailscale, restic or the tmux socket. Every fake logs its argv to one file, so the tests can say what was and was not run.

The venv is never built here (CCBOARD_MACOS_VENV_READY=1 points the installer at a wrapper around the test interpreter); one test builds it
through a fake `python3 -m venv` and a fake pip to pin the stamp that makes a rerun skip pip.
"""
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.posix_sh

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
SCRIPT = ROOT / "scripts" / "install-macos.sh"
UNINSTALL = ROOT / "scripts" / "uninstall-macos.sh"
BASH = "/bin/bash"          # macOS 3.2 on a Mac, 5.x on the CI runners: the script has to work under both

FQDN = "node.example.ts.net"
LOGIN = "user@example.com"
UID_N = "501"
# The real programs the installer uses and the fakes do not stand in for. Anything else is not on PATH, so a forgotten fake fails loudly.
REAL_TOOLS = ("sh", "cat", "sed", "grep", "tr", "head", "tail", "mkdir", "rm", "cp", "mv", "chmod", "dirname", "basename", "date", "sleep",
              "cmp", "mktemp", "touch", "env")


# ------------------------------------------------------------------ the fake world

def _script(path: Path, body: str, shebang: str = "#!/bin/sh") -> Path:
    path.write_text(shebang + "\n" + body)
    path.chmod(0o755)
    return path


class Mac:
    """A temp world: HOME, a PATH of fakes, a checkout of symlinks, and a state directory the fakes keep their files in."""

    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.bin = root / "bin"
        self.state = root / "state"
        self.app = root / "app"
        self.tmp = root / "tmp"
        for d in (self.home, self.bin, self.state, self.app, self.tmp):
            d.mkdir()
        self.log = self.state / "calls.log"
        self.log.write_text("")
        self.env = {}
        self.data = self.home / ".local" / "share" / "ccboard"
        self.la = self.home / "Library" / "LaunchAgents"
        for name in REAL_TOOLS:
            real = shutil.which(name)
            assert real, f"the host has no {name}"
            (self.bin / name).symlink_to(real)
        for part in ("app", "bin", "scripts", "launchd", "requirements.txt", "tmux.conf"):
            (self.app / part).symlink_to(ROOT / part)
        (self.app / ".venv" / "bin").mkdir(parents=True)
        self.venv_python = _script(self.app / ".venv" / "bin" / "python", f'exec {shlex.quote(sys.executable)} "$@"\n')
        self.python3 = _script(self.bin / "python3", f'exec {shlex.quote(sys.executable)} "$@"\n')
        (self.state / "brew-installed").write_text("git\ntmux\npython@3.12\nnode\ngh\nttyd\ncode-server\nrestic\nbun\nshellcheck\n")
        (self.state / "loaded").write_text("")
        (self.state / "ts-status.json").write_text(json.dumps(self.status_json()))
        (self.state / "ts-serve.json").write_text("{}")
        self._fakes()

    @staticmethod
    def status_json(state="Running", fqdn=FQDN, certs=1, login=LOGIN):
        return {"BackendState": state, "Self": {"DNSName": fqdn + ".", "UserID": 7}, "User": {"7": {"LoginName": login}},
                "CertDomains": [fqdn] * certs}

    def _fakes(self):
        s, q = str(self.state), shlex.quote
        log = f'echo "$(basename "$0") $*" >> {q(s)}/calls.log\n'
        _script(self.bin / "id", f'case "$1" in -u) echo "${{FAKE_UID:-{UID_N}}}";; -un) echo tester;; -gn) echo staff;; *) echo "uid=${{FAKE_UID:-{UID_N}}}";; esac\n')
        _script(self.bin / "uname", f'case "$1" in -s) echo Darwin;; -m) echo x86_64;; *) echo Darwin;; esac\n')
        _script(self.bin / "sw_vers", f'[ "$1" = -productVersion ] && cat {q(s)}/macos-version 2>/dev/null || echo 14.7.3\n')
        _script(self.bin / "hostname", 'echo testmac\n')
        _script(self.bin / "sudo", log + 'echo "sudo: must never be run" >&2\nexit 1\n')
        _script(self.bin / "launchctl", log + f'''S={q(s)}
case "$1" in
  print) grep -qxF "$2" "$S/loaded" && exit 0; echo "Could not find service" >&2; exit 113;;
  bootstrap) [ -f "$3" ] || {{ echo "no such plist $3" >&2; exit 5; }}
             [ ! -e "$S/bootstrap-fails" ] || {{ echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; }}
             echo "$2/$(basename "$3" .plist)" >> "$S/loaded"; exit 0;;
  bootout) grep -vxF "$2" "$S/loaded" > "$S/loaded.new"; mv "$S/loaded.new" "$S/loaded"; exit 0;;
esac
exit 0
''')
        _script(self.bin / "brew", log + f'''S={q(s)}
case "$1" in
  --prefix) echo "$S/prefix"; exit 0;;
  list) grep -qxF "$3" "$S/brew-installed"; exit $?;;
  info) echo '{{"formulae":[{{"name":"'"$3"'","bottle":{{"stable":{{"files":{{"sonoma":{{"cellar":":any"}}}}}}}}}}]}}'; exit 0;;
  bundle)
    for a in "$@"; do case "$a" in --file=*) f=${{a#--file=}};; esac; done
    names=$(sed -n 's/^brew "\\([^"]*\\)".*/\\1/p' "$f")
    if [ "$2" = check ]; then
      for n in $names; do grep -qxF "${{n##*/}}" "$S/brew-installed" || exit 1; done; exit 0
    fi
    [ ! -e "$S/brew-bundle-fails" ] || exit 1
    for n in $names; do echo "${{n##*/}}" >> "$S/brew-installed"; done; exit 0;;
esac
exit 0
''')
        _script(self.bin / "tmux", log + f'''S={q(s)}
case "$*" in
  "-V") echo "tmux 3.6b";;
  *"show-options"*) [ -e "$S/tmux-up" ] || exit 1;;
esac
exit 0
''')
        _script(self.bin / "ttyd", log.replace("$(basename \"$0\") $*", "ttyd $*") + '''case "$1" in
  --version) echo "ttyd version 1.7.7";;
  --help) printf '    -W, --writable   Allow clients to write to the TTY\\n    -O, --check-origin   no cross-origin\\n    -a, --url-arg   allow client args\\n';;
esac
exit 0
''')
        _script(self.bin / "code-server", 'echo 4.139.1\n')
        _script(self.bin / "restic", 'exit 0\n')
        _script(self.bin / "curl", log + f'[ ! -e {q(s)}/health-down ] || exit 22\nexit 0\n')
        _script(self.bin / "claude", log + f'''S={q(s)}
case "$1 $2" in
  "mcp get") [ -f "$S/claude-mcp" ] && {{ cat "$S/claude-mcp"; exit 0; }}; exit 1;;
  "mcp add") shift 6; printf 'ccboard\\n  Command: %s\\n  Args: %s\\n' "$1" "$2" > "$S/claude-mcp"; exit 0;;
  "mcp remove") rm -f "$S/claude-mcp"; exit 0;;
esac
exit 0
''')
        ts = f'''#!{sys.executable}
import json, os, sys
S = {s!r}
args = sys.argv[1:]
with open(S + "/calls.log", "a") as f:
    f.write("tailscale " + " ".join(args) + "\\n")
if args[:2] == ["status", "--json"]:
    sys.stdout.write(open(S + "/ts-status.json").read()); sys.exit(0)
if args[:3] == ["serve", "status", "--json"]:
    sys.stdout.write(open(S + "/ts-serve.json").read()); sys.exit(0)
if args and args[0] == "serve":
    if os.path.exists(S + "/serve-denied"):
        sys.stderr.write("Access denied: serve config denied\\n"); sys.exit(1)
    d = json.load(open(S + "/ts-serve.json"))
    port = path = target = None
    rest = args[1:]
    i = 0
    path = "/"
    while i < len(rest):
        a = rest[i]
        if a.startswith("--https="):
            port = a.split("=", 1)[1]
        elif a == "--set-path":
            path = rest[i + 1]; i += 1
        elif a.startswith("http://"):
            target = a
        i += 1
    key = "%s:%s" % ({FQDN!r}, port)
    if rest and rest[-1] == "off":
        if port:
            d.get("Web", {{}}).pop(key, None); d.get("TCP", {{}}).pop(port, None)
    elif port and target:
        d.setdefault("TCP", {{}})[port] = {{"HTTPS": True}}
        d.setdefault("Web", {{}}).setdefault(key, {{"Handlers": {{}}}})["Handlers"][path] = {{"Proxy": target}}
    json.dump(d, open(S + "/ts-serve.json", "w"))
sys.exit(0)
'''
        (self.bin / "tailscale").write_text(ts)
        (self.bin / "tailscale").chmod(0o755)

    # ---- running
    def _invariants(self, mark, uninstalling, board_https):
        new = self.calls()[mark:]
        assert not [c for c in new if c.startswith("sudo")], "no sudo anywhere"
        assert not [c for c in new if c.startswith("tailscale") and " reset" in c], "no serve reset"
        if board_https != "443":
            assert not [c for c in new if c.startswith("tailscale") and "--https=443" in c], "port 443 only when it is the board's own"
        tmux = [c for c in new if c.startswith("launchctl") and c.endswith("dev.ccboard.tmux")]
        assert not [c for c in tmux if c.startswith("launchctl kickstart")], "tmux is never kickstarted"
        if not uninstalling:
            assert not [c for c in tmux if c.startswith("launchctl bootout")], "the installer never boots tmux out"

    def run(self, script=None, args=(), env=None, check=None):
        mark = len(self.calls())
        r = self._run(script, args, env, check)
        board_https = {**self.env, **(env or {})}.get("CCBOARD_HTTPS_PORT", "8443")
        self._invariants(mark, script is not None and Path(script).name == "uninstall-macos.sh", board_https)
        return r

    def _run(self, script=None, args=(), env=None, check=None):
        e = {"PATH": str(self.bin), "HOME": str(self.home), "TMPDIR": str(self.tmp), "PYTHONDONTWRITEBYTECODE": "1",
             "CCBOARD_MACOS_VENV_READY": "1", "CCBOARD_MACOS_HEALTH_TRIES": "2", "CCBOARD_HTTPS_PORT": "8443", "CODE_HTTPS_PORT": "10000"}
        e.update(self.env)
        e.update(env or {})
        r = subprocess.run([str(script or self.app / "scripts" / "install-macos.sh"), *args], env=e, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=180)
        if check is not None:
            assert r.returncode == check, f"exit {r.returncode}\n--- stdout\n{r.stdout}\n--- stderr\n{r.stderr}"
        return r

    def install(self, env=None, args=()):
        return self.run(args=args, env=env, check=0)

    def uninstall(self, args=("--yes",), env=None):
        return self.run(script=self.app / "scripts" / "uninstall-macos.sh", args=args, env=env)

    # ---- reading what happened
    def calls(self):
        return self.log.read_text().splitlines()

    def mark(self):
        return len(self.calls())

    def since(self, mark):
        return self.calls()[mark:]

    def loaded(self):
        return (self.state / "loaded").read_text().splitlines()

    def plist(self, job):
        return plistlib.loads((self.la / f"dev.ccboard.{job}.plist").read_bytes())

    def plists(self):
        return sorted(p.name for p in self.la.glob("dev.ccboard.*.plist")) if self.la.exists() else []


def mutating(calls, tool):
    """The calls of one fake that change something (not a read)."""
    out = []
    for c in calls:
        w = c.split()
        if not w or w[0] != tool:
            continue
        if tool == "launchctl" and w[1] in ("bootstrap", "bootout", "kickstart"):
            out.append(c)
        elif tool == "tailscale" and w[1] == "serve" and w[2:4] != ["status", "--json"]:
            out.append(c)
        elif tool == "brew" and w[1] == "bundle" and w[2] != "check":
            out.append(c)
    return out


@pytest.fixture
def mac(tmp_path):
    return Mac(tmp_path)


def short_tmp():
    """A directory with a short path (a tmux socket path must stay under 100 bytes; the pytest temp dir may be long)."""
    return tempfile.mkdtemp(dir="/tmp", prefix="ccb")


# ------------------------------------------------------------------ static scans

SCRIPTS = [SCRIPT, UNINSTALL]


def code_lines(path):
    """The script's lines without whole-line comments and trailing ` # comment` text."""
    out = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        out.append((n, re.sub(r"\s#\s.*$", "", line)))
    return out


FORBIDDEN = [
    (r"\bdeclare\s+-[A-Za-z]*A", "declare -A (bash 4)"),
    (r"\bdeclare\s+-n\b|\blocal\s+-n\b", "a nameref (bash 4.3)"),
    (r"\bmapfile\b|\breadarray\b", "mapfile or readarray (bash 4)"),
    (r"\$\{[A-Za-z_0-9!]+(,,|\^\^|,|\^)[^}]*\}", "case-changing expansion (bash 4)"),
    (r"&>>|;;&|;&|\|&", "a bash 4 redirection or case terminator"),
    (r"\[\[\s+-v\b", "[[ -v (bash 4.2)"),
    (r"\binstall\s+-[A-Za-z]*D", "install -D (GNU)"),
    (r"\bsha256sum\b|\bsha1sum\b|\bmd5sum\b", "a GNU checksum tool"),
    (r"\breadlink\s+-f\b", "readlink -f (GNU)"),
    (r"(^|[\s;|&(])ss\s", "ss (Linux)"),
    (r"(^|[\s;|&(])timeout\s", "timeout (GNU coreutils)"),
    (r"\bsed\s+(-[A-Za-z]*\s+)*-i", "sed -i (differs between BSD and GNU)"),
    (r"\bdate\s+-d\b|\bstat\s+-c\b|\bgrep\s+-[A-Za-z]*P\b|\becho\s+-e\b|\bsort\s+-V\b", "a GNU-only flag"),
    (r"\bsystemctl\b|\bjournalctl\b", "systemd"),
    (r"\bapt(-get)?\b|\bdpkg\b|\bgetent\b", "a Debian tool"),
    (r"/etc/os-release", "/etc/os-release"),
    (r"\bsudo\b", "sudo"),
    (r"\bdscl\b", "dscl (the login shell comes from python's pwd module)"),
    (r"tailscale\s+serve\s+reset|\bserve\s+reset\b", "tailscale serve reset"),
]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_the_scripts_parse_under_bash_n(script):
    r = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_shellcheck_when_installed(script):
    sc = shutil.which("shellcheck")
    if not sc:
        pytest.skip("shellcheck is not installed")
    r = subprocess.run([sc, "-s", "bash", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_bash4_gnu_or_linux_only_construct(script):
    bad = []
    for n, line in code_lines(script):
        for rx, why in FORBIDDEN:
            if re.search(rx, line):
                bad.append(f"{script.name}:{n}: {why}: {line.strip()}")
    assert not bad, "\n".join(bad)


def test_the_scripts_start_with_bash_and_are_executable():
    for s in SCRIPTS:
        assert s.read_text().startswith("#!/bin/bash\n"), "the stock bash of a Mac, not whatever `bash` is first on PATH"
        assert s.stat().st_mode & stat.S_IXUSR


def test_no_personal_path_or_array_in_the_scripts():
    for s in SCRIPTS:
        text = s.read_text()
        assert "/Us" + "ers/" not in text
        assert not re.search(r"^\s*[A-Za-z_]+=\(", text, re.M), "an array assignment (an empty array breaks `set -u` on bash 3.2)"


# ------------------------------------------------------------------ the dispatch in install.sh

# sha256 of the first 30 lines of install.sh as committed before the dispatch line was added (the dispatch line sits further down), without the
# ENV_KEYS line: that line is the one place every new remembered setting is added to (issue #118 added CCBOARD_TAILSCALE_PLACEMENT), the rest is pinned
INSTALL_HEAD_SHA = "42bbfaf04a00c4b6642ca16ab155cd006d63352487e3d3a57f5c47dcaab8b967"


def test_install_sh_has_exactly_one_dispatch_line_before_the_first_guard():
    lines = INSTALL.read_text().splitlines()
    hits = [i for i, l in enumerate(lines) if "install-macos.sh" in l]
    assert len(hits) == 1, "the Linux installer changes by one dispatch line"
    first_guard = next(i for i, l in enumerate(lines) if l.startswith('[ "$(id -u)" -ne 0 ]'))
    tty_guard = next(i for i, l in enumerate(lines) if l.startswith("[ -t 0 ] ||"))
    sudo_guard = next(i for i, l in enumerate(lines) if l.startswith("have sudo ||"))
    assert hits[0] < first_guard < tty_guard < sudo_guard, "a Mac must reach its own installer before the interactive and sudo guards"
    assert 'uname -s' in lines[hits[0]] and "Darwin" in lines[hits[0]] and 'exec "$APP_DIR/scripts/install-macos.sh" "$@"' in lines[hits[0]]


def test_the_first_thirty_lines_of_install_sh_are_untouched():
    head = "\n".join(l for l in INSTALL.read_text().splitlines()[:30] if not l.startswith("ENV_KEYS=(")) + "\n"
    assert "install-macos" not in head
    assert hashlib.sha256(head.encode()).hexdigest() == INSTALL_HEAD_SHA


def stub_world(tmp_path, uname):
    """install.sh copied next to a stub macOS script, with a fake uname on a PATH that holds nothing else but the tools install.sh uses early."""
    app = tmp_path / "app"
    (app / "scripts").mkdir(parents=True)
    shutil.copy(INSTALL, app / "install.sh")
    marker = tmp_path / "stub-ran"
    _script(app / "scripts" / "install-macos.sh", f'echo "stub $*" > {shlex.quote(str(marker))}\n')
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name in ("env", "cat", "tr", "sort", "head", "dirname", "sh"):
        w = shutil.which(name)
        if w:
            (bin_ / name).symlink_to(w)
    _script(bin_ / "uname", f'echo {uname}\n')
    _script(bin_ / "id", 'echo 1000\n')
    (bin_ / "bash").symlink_to(BASH)
    return app, bin_, marker


def test_on_darwin_install_sh_hands_over_to_the_macos_script_with_its_arguments(tmp_path):
    app, bin_, marker = stub_world(tmp_path, "Darwin")
    r = subprocess.run([str(app / "install.sh"), "--runtime", "launchd"], env={"PATH": str(bin_)}, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    assert marker.read_text().strip() == "stub --runtime launchd"


def test_on_linux_install_sh_goes_on_to_its_own_guards(tmp_path):
    """Run without a terminal, the Linux installer stops at its own `run interactively` guard: that is the first guard after the dispatch
    line, so reaching it shows the line let a Linux host through. (The Ubuntu check itself needs a real Ubuntu and is not run here.)"""
    app, bin_, marker = stub_world(tmp_path, "Linux")
    r = subprocess.run([str(app / "install.sh")], env={"PATH": str(bin_)}, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert not marker.exists(), "the macOS script must not run on Linux"
    assert r.returncode == 1 and "run interactively" in r.stderr, r.stderr


# ------------------------------------------------------------------ refusals (#120) and guards

def docker_refusal_from_install_sh():
    """The text of install.sh's own docker_host_refusal macOS, run from the guard block exactly as committed."""
    text = INSTALL.read_text()
    block = text[text.index("# >>> docker host guard"):text.index("# <<< docker host guard")]
    prog = ("set -eu\ndie() { printf '\\033[1;31merror:\\033[0m %s\\n' \"$*\" >&2; exit 1; }\nwarn() { :; }\nhave() { command -v \"$1\" >/dev/null 2>&1; }\n"
            + block + "\ndocker_host_refusal macOS\n")
    return subprocess.run([BASH, "-c", prog], capture_output=True, text=True)


def test_docker_is_refused_on_a_mac_with_install_sh_text_word_for_word(mac):
    want = docker_refusal_from_install_sh()
    assert want.returncode == 1 and "use the launchd runtime" in want.stderr
    for r in (mac.run(env={"CCBOARD_RUNTIME": "docker"}), mac.run(args=("--runtime", "docker")), mac.run(args=("--runtime=docker",))):
        assert r.returncode == 1
        assert r.stderr == want.stderr
    assert mac.plists() == [] and not (mac.data / "env").exists(), "refused before any change"


def test_systemd_and_unknown_runtimes_are_refused(mac):
    r = mac.run(args=("--runtime", "systemd"))
    assert r.returncode == 1 and "systemd is Linux only" in r.stderr
    r = mac.run(env={"CCBOARD_RUNTIME": "podman"})
    assert r.returncode == 1 and "must be launchd" in r.stderr
    r = mac.run(args=("--bogus",))
    assert r.returncode == 1 and "unknown argument" in r.stderr
    assert mac.plists() == []


def test_the_runtime_flag_is_an_alias_for_the_variable(mac):
    r = mac.run(args=("--runtime", "launchd"), check=0)
    assert "ccboard is installed" in r.stdout
    assert "CCBOARD_RUNTIME=launchd" in (mac.data / "env").read_text().splitlines()


def test_root_old_macos_and_a_non_mac_are_refused_before_any_change(mac):
    r = mac.run(env={"FAKE_UID": "0"})
    assert r.returncode == 1 and "not as root" in r.stderr
    (mac.state / "macos-version").write_text("12.7.6\n")
    r = mac.run()
    assert r.returncode == 1 and "macOS 13 or newer" in r.stderr and "12.7.6" in r.stderr
    (mac.state / "macos-version").write_text("not-a-version\n")
    assert mac.run().returncode == 1
    assert mac.plists() == [] and not (mac.data / "env").exists()
    assert mutating(mac.calls(), "brew") == [] and mutating(mac.calls(), "launchctl") == []


def test_a_mac_without_homebrew_is_refused_with_the_address(mac):
    (mac.bin / "brew").unlink()
    r = mac.run()
    # a real Homebrew in /opt/homebrew or /usr/local on the host running the test would be found by the fallback; only assert when there is none
    if not any(Path(p).exists() for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")):
        assert r.returncode == 1 and "Homebrew is not installed" in r.stderr and "brew.sh" in r.stderr
    assert mac.plists() == []


def test_a_stopped_tailscale_refuses_before_any_change(mac):
    (mac.state / "ts-status.json").write_text(json.dumps(mac.status_json(state="Stopped")))
    r = mac.run()
    assert r.returncode == 1 and "Stopped" in r.stderr
    assert mac.plists() == [] and not mac.data.exists() and not (mac.home / "Library" / "Logs").exists(), "not even the data folder is created"
    assert mutating(mac.calls(), "brew") == []


def test_a_foreign_serve_handler_refuses_before_any_change_unless_replace_is_set(mac):
    (mac.state / "ts-serve.json").write_text(json.dumps({"TCP": {"8443": {"HTTPS": True}}, "Web": {f"{FQDN}:8443": {"Handlers": {"/other": {"Proxy": "http://127.0.0.1:3000"}}}}}))
    r = mac.run()
    assert r.returncode == 1 and "already used by something else" in r.stderr and "CCBOARD_REPLACE_SERVE=1" in r.stderr
    assert mac.plists() == [] and not (mac.data / "env").exists()
    assert mutating(mac.calls(), "tailscale") == []
    assert mac.run(env={"CCBOARD_REPLACE_SERVE": "1"}).returncode == 0
    assert mutating(mac.calls(), "tailscale")


def test_a_bad_backup_schedule_refuses_before_any_change(mac):
    for spec in ("Mon *-*-* 02:30", "hourly", "*:0/15"):
        r = mac.run(env={"CCBOARD_BACKUP_ONCALENDAR": spec})
        assert r.returncode == 1 and "CCBOARD_BACKUP_ONCALENDAR" in r.stderr, spec
    assert mac.plists() == [] and not (mac.data / "env").exists()
    assert mutating(mac.calls(), "brew") == [] and mutating(mac.calls(), "tailscale") == []


def test_missing_tools_stop_the_install_without_consent_and_install_with_it(mac):
    (mac.state / "brew-installed").write_text("git\ntmux\npython@3.12\nnode\ngh\n")           # no ttyd
    r = mac.run()
    assert r.returncode == 1 and "tools are missing: ttyd" in r.stderr and "CCBOARD_MACOS_INSTALL_TOOLS=1" in r.stderr
    assert mutating(mac.calls(), "brew") == [] and mac.plists() == []
    r = mac.run(env={"CCBOARD_MACOS_INSTALL_TOOLS": "1"}, check=0)
    bundles = mutating(mac.calls(), "brew")
    assert any("--no-upgrade" in b and b.endswith("scripts/macos/Brewfile") for b in bundles), bundles
    assert any("--no-upgrade" in b and "Brewfile.optional" in b for b in bundles), bundles


def test_optional_tools_can_be_skipped(mac):
    (mac.state / "brew-installed").write_text("git\ntmux\npython@3.12\nnode\ngh\nttyd\n")
    mac.install(env={"CCBOARD_MACOS_OPTIONAL": "none"})
    assert not any(" bundle " in c and "Brewfile.optional" in c for c in mac.calls()) and mutating(mac.calls(), "brew") == []
    assert "dev.ccboard.code-server.plist" not in mac.plists()


def test_ttyd_without_the_flags_ccboard_needs_is_refused(mac):
    _script(mac.bin / "ttyd", 'case "$1" in --version) echo "ttyd version 1.6.3";; --help) echo "    -p, --port";; esac\n')
    r = mac.run()
    assert r.returncode == 1 and "ttyd" in r.stderr and "-W" in r.stderr
    assert mac.plists() == []


# ------------------------------------------------------------------ the first run

def test_a_first_install_writes_the_jobs_the_settings_and_the_hooks(mac):
    r = mac.install(env={"PROJECTS_DIR": str(mac.home / "work")})
    out = r.stdout
    assert "ccboard is installed" in out
    assert f"https://{FQDN}:8443/" in out and f"https://{FQDN}:10000/" in out
    assert f"launchctl kickstart -k gui/{UID_N}/dev.ccboard.board" in out
    assert "Library/Logs/ccboard" in out and "git pull && ./install.sh" in out and "uninstall-macos.sh" in out
    assert mac.plists() == sorted(f"dev.ccboard.{j}.plist" for j in ("backup", "board", "code-server", "ttyd", "tmux"))
    # every plist parses and carries what the renderer promises
    for job in ("board", "tmux", "ttyd", "code-server", "backup"):
        p = mac.plist(job)
        assert p["Label"] == f"dev.ccboard.{job}"
        assert all(isinstance(a, str) for a in p["ProgramArguments"])
        assert p["EnvironmentVariables"]["HOME"] == str(mac.home)
        assert "~" not in p["EnvironmentVariables"]["PATH"]
        assert p["StandardOutPath"] == str(mac.home / "Library" / "Logs" / "ccboard" / f"{job}.log")
        assert (mac.home / "Library" / "Logs" / "ccboard").is_dir()
    board = mac.plist("board")
    assert board["ProgramArguments"][0] == str(mac.app / ".venv" / "bin" / "uvicorn")
    assert board["EnvironmentVariables"]["CCBOARD_RUNTIME"] == "launchd"
    assert board["EnvironmentVariables"]["CCBOARD_ENV_FILE"] == str(mac.data / "env")
    assert board["EnvironmentVariables"]["PATH"].split(":")[:2] == [str(mac.home / ".local" / "bin"), str(mac.state / "prefix" / "bin")]
    assert mac.plist("tmux")["ProgramArguments"][0] == str(mac.bin / "tmux")
    assert mac.plist("ttyd")["ProgramArguments"][:3] == [str(mac.bin / "ttyd"), "-i", "lo0"]
    assert mac.plist("backup")["StartCalendarInterval"] == {"Hour": 2, "Minute": 30}
    # tmux first, then ttyd, then the board; the optional jobs after; nothing kickstarted on a first run
    boots = [c.split()[3].replace(".plist", "").rsplit(".", 1)[1] for c in mac.calls() if c.startswith("launchctl bootstrap")]
    assert boots[:3] == ["tmux", "ttyd", "board"] and sorted(boots) == sorted(["tmux", "ttyd", "board", "code-server", "backup"])
    assert all(c.split()[2] == f"gui/{UID_N}" for c in mac.calls() if c.startswith("launchctl bootstrap"))
    assert not [c for c in mac.calls() if c.startswith("launchctl kickstart")]
    assert sorted(mac.loaded()) == sorted(f"gui/{UID_N}/dev.ccboard.{j}" for j in ("tmux", "ttyd", "board", "code-server", "backup"))
    # the settings file: mode 0600, remembered values, the runtime, a token
    env = mac.data / "env"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    lines = env.read_text().splitlines()
    assert f"PROJECTS_DIR={mac.home / 'work'}" in lines and "CCBOARD_RUNTIME=launchd" in lines and f"CCBOARD_ALLOWED_USERS={LOGIN}" in lines
    assert f"CCBOARD_DATA_DIR={mac.data}" in lines and "CCBOARD_NODE_NAME=testmac" in lines
    assert re.search(r"^CCBOARD_HUB_TOKEN=[0-9a-f]{48}$", env.read_text(), re.M)
    assert (mac.home / "work").is_dir()
    # tailscale serve through the helper: the board, the terminal, the editor; never port 443, never a reset
    serve = mutating(mac.calls(), "tailscale")
    assert serve == [f"tailscale serve --bg --https=8443 http://127.0.0.1:8000",
                     f"tailscale serve --bg --https=8443 --set-path /tty http://127.0.0.1:7681",
                     f"tailscale serve --bg --https=10000 http://127.0.0.1:8080"]
    assert not any("reset" in c or "--https=443" in c for c in mac.calls())
    # the Claude hooks point at this checkout; the MCP shim runs under the board's own venv python (#121)
    settings = (mac.home / ".claude" / "settings.json").read_text()
    assert str(mac.app / "bin" / "ccboard-hook") in settings
    assert (mac.state / "claude-mcp").read_text() == f"ccboard\n  Command: {mac.app / '.venv' / 'bin' / 'python'}\n  Args: {mac.app / 'scripts' / 'ccboard_mcp.py'}\n"
    assert not any("/usr/bin/python3" in c for c in mac.calls())
    # the restic password file: 0600 and the note
    pw = mac.data / "restic-password"
    assert pw.exists() and stat.S_IMODE(pw.stat().st_mode) == 0o600
    assert f"generated {pw}" in out and "copy it somewhere safe" in out
    assert (mac.home / ".config" / "code-server" / "config.yaml").read_text().startswith("# managed by ccboard\n")
    assert any(c.startswith("curl") and "/healthz" in c for c in mac.calls())


def test_the_run_invariants_hold_in_every_scenario(mac):
    """Mac.run checks, after EVERY installer or uninstaller run of every test in this file: no fake `sudo` was called, no `tailscale serve reset`
    or port 443 was used, and the tmux job was neither kickstarted nor booted out once it was loaded (only the uninstall boots it out)."""
    mac.install()
    mac.uninstall(("--yes",))
    tmux_calls = [c.split()[1] for c in mac.calls() if "dev.ccboard.tmux" in c and c.startswith("launchctl") and c.split()[1] != "print"]
    assert tmux_calls == ["bootstrap", "bootout"], "loaded once, booted out once, by the uninstall"


def test_a_rerun_changes_nothing(mac):
    mac.install()
    stamps = {p: p.stat().st_mtime_ns for p in [*mac.la.glob("*.plist"), mac.data / "env"]}
    settings = (mac.home / ".claude" / "settings.json").read_text()
    mark = mac.mark()
    r = mac.install()
    assert "nothing to load or restart" in r.stdout
    again = mac.since(mark)
    assert mutating(again, "launchctl") == [] and mutating(again, "tailscale") == [] and mutating(again, "brew") == []
    assert [c for c in again if c.startswith("claude mcp add") or c.startswith("claude mcp remove")] == []
    assert {p: p.stat().st_mtime_ns for p in stamps} == stamps, "unchanged files are not rewritten"
    assert (mac.home / ".claude" / "settings.json").read_text() == settings, "the hooks merge is idempotent"
    assert "unchanged" in r.stdout


def test_a_changed_setting_restarts_only_the_board(mac):
    mac.install()
    mark = mac.mark()
    r = mac.install(env={"CCBOARD_APPROVE_TIMEOUT": "45"})
    changes = mutating(mac.since(mark), "launchctl")
    assert changes == [f"launchctl kickstart -k gui/{UID_N}/dev.ccboard.board"], changes
    assert "CCBOARD_APPROVE_TIMEOUT=45" in (mac.data / "env").read_text().splitlines()
    # remembered: the next run without the variable keeps 45 and does nothing
    mark = mac.mark()
    mac.install()
    assert "CCBOARD_APPROVE_TIMEOUT=45" in (mac.data / "env").read_text().splitlines()
    assert mutating(mac.since(mark), "launchctl") == []


def test_new_code_restarts_the_board_and_a_requirements_change_does_too(mac):
    """The board is restarted when the checkout changed since the last run (git pull), not on every rerun."""
    mac.install()
    stamp = mac.data / ".macos-code-stamp"
    assert stamp.exists()
    stamp.write_text("an older checkout\n")
    mark = mac.mark()
    mac.install()
    assert mutating(mac.since(mark), "launchctl") == [f"launchctl kickstart -k gui/{UID_N}/dev.ccboard.board"]
    mark = mac.mark()
    mac.install()
    assert mutating(mac.since(mark), "launchctl") == []


def test_a_changed_plist_reloads_the_board_and_ttyd_but_only_warns_for_tmux(mac):
    mac.install()
    short = short_tmp()
    try:
        mark = mac.mark()
        r = mac.install(env={"CCBOARD_TMUX_TMPDIR": short})
    finally:
        shutil.rmtree(short, ignore_errors=True)
    changes = mutating(mac.since(mark), "launchctl")
    verbs = [(c.split()[1], c.split()[-1].rsplit("/", 1)[-1].replace(".plist", "").replace("gui/", "")) for c in changes]
    assert ("bootout", "dev.ccboard.ttyd") in verbs and ("bootstrap", "dev.ccboard.ttyd") in verbs
    assert ("bootout", "dev.ccboard.board") in verbs and ("bootstrap", "dev.ccboard.board") in verbs
    assert not [v for v in verbs if v[1] == "dev.ccboard.tmux"], "the tmux job keeps running"
    assert "dev.ccboard.tmux changed but is running" in r.stderr
    assert mac.plist("tmux")["EnvironmentVariables"]["TMUX_TMPDIR"] == short, "the file on disk is current; the running job loads it at its next start"


def test_a_bootstrap_that_is_still_finishing_is_retried_then_reported(mac):
    (mac.state / "bootstrap-fails").write_text("")
    r = mac.run()
    assert r.returncode == 1 and "launchctl bootstrap" in r.stderr and "Input/output error" in r.stderr
    assert len([c for c in mac.calls() if c.startswith("launchctl bootstrap")]) == 5


def test_an_unhealthy_board_fails_the_install_with_where_to_look(mac):
    (mac.state / "health-down").write_text("")
    r = mac.run()
    assert r.returncode == 1 and "not answering on 127.0.0.1:8000" in r.stderr and "board.log" in r.stderr


# ------------------------------------------------------------------ remembered settings

def test_previous_values_are_remembered_explicit_ones_win_and_hand_added_lines_are_kept_as_data(mac):
    mac.install(env={"CCBOARD_PORT": "8100", "NTFY_TOPIC": "mytopic"})
    env = mac.data / "env"
    env.write_text(env.read_text() + "AWS_ACCESS_KEY_ID=$(touch pwned)\nNOT A LINE\n")
    os.chmod(env, 0o600)
    mac.install(env={"NTFY_TOPIC": "other"})
    text = env.read_text().splitlines()
    assert "CCBOARD_PORT=8100" in text and "NTFY_TOPIC=other" in text
    assert "AWS_ACCESS_KEY_ID=$(touch pwned)" in text
    assert not (mac.root / "pwned").exists() and not (mac.app / "pwned").exists() and not (mac.home / "pwned").exists()
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert mac.plist("board")["ProgramArguments"][5] == "8100"


def test_a_world_readable_settings_file_is_made_private(mac):
    mac.install()
    env = mac.data / "env"
    os.chmod(env, 0o644)
    mac.install()
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_a_symlinked_settings_file_is_refused(mac):
    mac.data.mkdir(parents=True)
    target = mac.root / "elsewhere"
    target.write_text("CCBOARD_PORT=9\n")
    (mac.data / "env").symlink_to(target)
    r = mac.run()
    assert r.returncode == 1 and "symbolic link" in r.stderr
    assert target.read_text() == "CCBOARD_PORT=9\n"


def test_a_value_with_a_newline_or_a_bad_port_is_refused(mac):
    assert "port number" in mac.run(env={"CCBOARD_PORT": "80a"}).stderr
    assert "must differ" in mac.run(env={"CCBOARD_HTTPS_PORT": "8443", "CODE_HTTPS_PORT": "8443"}).stderr
    assert "443" in mac.run(env={"CCBOARD_HTTPS_PORT": "8443", "CODE_HTTPS_PORT": "443"}).stderr
    assert "control character" in mac.run(env={"CCBOARD_NODES": "a\nb"}).stderr
    assert mac.plists() == []


# ------------------------------------------------------------------ optional jobs, domains, backup

def test_optional_jobs_follow_their_switches_and_are_removed_when_switched_off(mac):
    mac.install(env={"CCBOARD_KEEP_AWAKE": "1", "CCBOARD_MEM_SERVICE": "1"})
    assert {"dev.ccboard.awake.plist", "dev.ccboard.mem.plist"} <= set(mac.plists())
    assert mac.plist("awake")["ProgramArguments"][0] == "/usr/bin/caffeinate"
    assert f"gui/{UID_N}/dev.ccboard.awake" in mac.loaded() and f"gui/{UID_N}/dev.ccboard.mem" in mac.loaded()
    mark = mac.mark()
    mac.install(env={"CCBOARD_KEEP_AWAKE": "0", "CCBOARD_MEM_SERVICE": "0", "CCBOARD_BACKUP": "0"})
    changes = mutating(mac.since(mark), "launchctl")
    # CCBOARD_MEM_SERVICE is a remembered setting, so the settings file changed and the board restarts once; the three jobs are booted out
    want = [f"launchctl bootout gui/{UID_N}/dev.ccboard.{j}" for j in ("awake", "mem", "backup")] + [f"launchctl kickstart -k gui/{UID_N}/dev.ccboard.board"]
    assert sorted(changes) == sorted(want)
    assert not {"dev.ccboard.awake.plist", "dev.ccboard.mem.plist", "dev.ccboard.backup.plist"} & set(mac.plists())


def test_the_backup_time_comes_from_the_schedule_and_the_job_can_be_left_out(mac):
    mac.install(env={"CCBOARD_BACKUP_ONCALENDAR": "*-*-* 03:15:00"})
    assert mac.plist("backup")["StartCalendarInterval"] == {"Hour": 3, "Minute": 15}
    assert "CCBOARD_BACKUP_ONCALENDAR=*-*-* 03:15:00" in (mac.data / "env").read_text().splitlines()
    mac.install(env={"CCBOARD_BACKUP_ONCALENDAR": "04:05"})
    assert mac.plist("backup")["StartCalendarInterval"] == {"Hour": 4, "Minute": 5}
    assert f"gui/{UID_N}/dev.ccboard.backup" in mac.loaded()


def test_backup_off_writes_no_job_and_no_password_file(mac):
    mac.install(env={"CCBOARD_BACKUP": "0"})
    assert "dev.ccboard.backup.plist" not in mac.plists() and not (mac.data / "restic-password").exists()


def test_a_missing_restic_is_a_warning_not_a_failure(mac):
    (mac.bin / "restic").unlink()
    (mac.state / "brew-installed").write_text("git\ntmux\npython@3.12\nnode\ngh\nttyd\ncode-server\n")
    r = mac.run(env={"CCBOARD_MACOS_OPTIONAL": "code-server"}, check=0)
    assert "restic is not installed" in r.stderr
    assert "dev.ccboard.backup.plist" in mac.plists()


def test_the_user_domain_uses_the_background_session_type(mac):
    r = mac.run(env={"CCBOARD_LAUNCHD_DOMAIN": "user"}, check=0)
    assert "experimental" in r.stderr
    assert mac.plist("board")["LimitLoadToSessionType"] == "Background"
    assert all(c.split()[2] == f"user/{UID_N}" for c in mac.calls() if c.startswith("launchctl bootstrap"))
    assert f"launchctl kickstart -k user/{UID_N}/dev.ccboard.board" in r.stdout
    assert mac.run(env={"CCBOARD_LAUNCHD_DOMAIN": "system"}).returncode == 1


def test_code_server_is_left_out_when_not_picked_and_a_foreign_config_is_protected(mac):
    mac.install(env={"CCBOARD_MACOS_OPTIONAL": "restic"})
    assert "dev.ccboard.code-server.plist" not in mac.plists()
    cfg = mac.home / ".config" / "code-server" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("bind-addr: 0.0.0.0:8080\n")
    r = mac.run()
    assert r.returncode == 1 and "not managed by ccboard" in r.stderr and cfg.read_text() == "bind-addr: 0.0.0.0:8080\n"
    assert mac.run(env={"CCBOARD_REPLACE_CODE_SERVER_CONFIG": "1"}).returncode == 0
    assert cfg.read_text().startswith("# managed by ccboard")
    assert list(cfg.parent.glob("config.yaml.ccboard-bak-*"))


# ------------------------------------------------------------------ the agents

def test_codex_gets_hooks_and_an_mcp_entry_only_when_it_is_installed(mac):
    r = mac.install()
    assert "codex is not installed" in r.stdout and not (mac.home / ".codex").exists()
    _script(mac.bin / "codex", f'''echo "codex $*" >> {shlex.quote(str(mac.log))}
case "$1 $2" in "mcp get") exit 1;; esac
exit 0
''')
    mac.install()
    assert (mac.home / ".codex" / "hooks.json").exists()
    adds = [c for c in mac.calls() if c.startswith("codex mcp add")]
    assert adds and str(mac.app / ".venv" / "bin" / "python") in adds[0] and "CCBOARD_URL=http://127.0.0.1:8000" in adds[0]


def test_a_stale_claude_mcp_entry_is_pointed_at_the_venv_python(mac):
    (mac.state / "claude-mcp").write_text("ccboard\n  Command: /usr/bin/python3\n  Args: /old/ccboard_mcp.py\n")
    mac.install()
    assert any(c.startswith("claude mcp remove") for c in mac.calls())
    assert str(mac.app / ".venv" / "bin" / "python") in (mac.state / "claude-mcp").read_text()


def test_without_claude_the_mcp_step_is_skipped_with_a_note(mac):
    (mac.bin / "claude").unlink()
    r = mac.install()
    assert "claude is not installed" in r.stdout
    assert not [c for c in mac.calls() if c.startswith("claude")]


# ------------------------------------------------------------------ the venv (built through fakes; the stamp makes a rerun skip pip)

def test_the_venv_is_built_once_and_a_rerun_skips_pip(mac):
    real = shlex.quote(sys.executable)
    mac.python3.write_text(f'''#!/bin/sh
if [ "$1" = -m ] && [ "$2" = venv ]; then
  echo "python3 -m venv $3" >> {shlex.quote(str(mac.log))}
  mkdir -p "$3/bin"
  printf '#!/bin/sh\\nexec %s "$@"\\n' {real} > "$3/bin/python"
  printf '#!/bin/sh\\necho "pip $*" >> %s\\n' {shlex.quote(str(mac.log))} > "$3/bin/pip"
  chmod 755 "$3/bin/python" "$3/bin/pip"; exit 0
fi
exec {real} "$@"
''')
    shutil.rmtree(mac.app / ".venv")
    env = {"CCBOARD_MACOS_VENV_READY": "0"}
    r = mac.install(env=env)
    assert "installed requirements" in r.stdout
    pips = [c for c in mac.calls() if c.startswith("pip install")]
    assert len(pips) == 1 and "-r requirements.txt" in pips[0]
    assert (mac.app / ".venv" / ".ccboard-stamp").read_text().startswith(hashlib.sha256((ROOT / "requirements.txt").read_bytes()).hexdigest())
    mark = mac.mark()
    r = mac.install(env=env)
    assert "up to date" in r.stdout and not [c for c in mac.since(mark) if c.startswith("pip ") or c.startswith("python3 -m venv")]
    assert mutating(mac.since(mark), "launchctl") == []
    # a changed requirements stamp installs again and restarts the board
    (mac.app / ".venv" / ".ccboard-stamp").write_text("old")
    mark = mac.mark()
    mac.install(env=env)
    assert [c for c in mac.since(mark) if c.startswith("pip install")]
    assert mutating(mac.since(mark), "launchctl") == [f"launchctl kickstart -k gui/{UID_N}/dev.ccboard.board"]
    # that remove-and-rebuild left the checkout's requirements.txt alone
    assert (ROOT / "requirements.txt").exists()


def test_a_venv_that_does_not_run_is_refused_when_the_seam_says_it_is_ready(mac):
    _script(mac.venv_python, "exit 1\n")
    r = mac.run()
    assert r.returncode == 1 and "does not run" in r.stderr


# ------------------------------------------------------------------ the uninstall

def test_the_uninstall_removes_the_jobs_tmux_last_and_keeps_the_data(mac):
    mac.install(env={"CCBOARD_KEEP_AWAKE": "1"})
    keep = [mac.data / "env", mac.data / "restic-password"]
    mark = mac.mark()
    r = mac.uninstall()
    assert r.returncode == 0, r.stderr
    outs = [c.split()[-1].rsplit(".", 1)[1] for c in mac.since(mark) if c.startswith("launchctl bootout")]
    assert outs[-1] == "tmux" and sorted(outs) == sorted(["tmux", "ttyd", "board", "awake", "code-server", "backup"])
    assert mac.plists() == [] and mac.loaded() == []
    assert all(p.exists() for p in keep), "the data dir and the env file stay"
    assert "ends the tmux server" in r.stderr and "every Claude and Codex session" in r.stderr
    assert str(mac.data) in r.stdout and "what was kept" in r.stdout and "claude_settings.py remove" in r.stdout
    assert not [c for c in mac.calls() if c.startswith("sudo")]
    again = mac.uninstall()
    assert again.returncode == 0 and "nothing to remove" in again.stdout


def test_the_uninstall_asks_first_and_needs_yes_without_a_terminal(mac):
    mac.install()
    r = mac.uninstall(args=())
    assert r.returncode == 1 and "--yes" in r.stderr
    assert len(mac.plists()) == 5 and len(mac.loaded()) == 5, "refused before any change"


def test_keep_tmux_leaves_the_sessions_running(mac):
    mac.install()
    r = mac.uninstall(args=("--yes", "--keep-tmux"))
    assert r.returncode == 0 and "kept dev.ccboard.tmux" in r.stdout
    assert mac.plists() == ["dev.ccboard.tmux.plist"] and mac.loaded() == [f"gui/{UID_N}/dev.ccboard.tmux"]
    assert not [c for c in mac.calls() if c.startswith("launchctl bootout") and c.endswith("dev.ccboard.tmux")]


def test_the_uninstall_follows_the_domain_a_job_was_installed_in(mac):
    mac.install(env={"CCBOARD_LAUNCHD_DOMAIN": "user"})
    mark = mac.mark()
    assert mac.uninstall().returncode == 0
    bootouts = [c for c in mac.since(mark) if c.startswith("launchctl bootout")]
    assert bootouts and all(f"user/{UID_N}/" in c for c in bootouts)


def test_the_uninstall_refuses_root(mac):
    r = mac.run(script=mac.app / "scripts" / "uninstall-macos.sh", args=("--yes",), env={"FAKE_UID": "0"})
    assert r.returncode == 1 and "not as root" in r.stderr


# ------------------------------------------------------------------ a rerun without the switches of the first run

def test_a_rerun_without_the_data_dir_finds_the_settings_of_the_first_run(mac):
    """The settings file lives in the data dir, so a rerun that does not repeat CCBOARD_DATA_DIR must still find it (no second token, no new plist)."""
    elsewhere = mac.root / "elsewhere"
    mac.install(env={"CCBOARD_DATA_DIR": str(elsewhere)})
    token = next(l for l in (elsewhere / "env").read_text().splitlines() if l.startswith("CCBOARD_HUB_TOKEN="))
    stamps = {p: p.stat().st_mtime_ns for p in [*mac.la.glob("*.plist"), elsewhere / "env"]}
    mark = mac.mark()
    r = mac.install()
    assert token in (elsewhere / "env").read_text().splitlines(), "the hub token is the one from the first run"
    assert not mac.data.exists() or not (mac.data / "env").exists(), "no second settings file in the default folder"
    assert mutating(mac.since(mark), "launchctl") == [] and "nothing to load or restart" in r.stdout
    assert {p: p.stat().st_mtime_ns for p in stamps} == stamps
    assert mac.plist("board")["EnvironmentVariables"]["CCBOARD_ENV_FILE"] == str(elsewhere / "env")


def test_a_rerun_without_the_user_domain_switch_stays_in_the_user_domain(mac):
    mac.install(env={"CCBOARD_LAUNCHD_DOMAIN": "user"})
    mark = mac.mark()
    r = mac.install()
    assert mutating(mac.since(mark), "launchctl") == [], "no duplicate jobs in gui/"
    assert all(f"/{UID_N}/" in l and l.startswith("user/") for l in mac.loaded())
    assert "keeping the user domain" in r.stdout
    assert mac.plist("board")["LimitLoadToSessionType"] == "Background"


# ------------------------------------------------------------------ values the regular expressions of bash 3.2 have to read

def test_model_and_number_settings_are_read_by_bash_3_2_regular_expressions(mac):
    ok = {"CCBOARD_SUBAGENT_MODEL": "opus[1m]", "CCBOARD_HEADLESS_FABLE_CAP": "12.5", "CCBOARD_AUTOCLOSE_GRACE": "0.5"}
    for name, bad, word in (("CCBOARD_SUBAGENT_MODEL", "bad model", "CCBOARD_SUBAGENT_MODEL"), ("CCBOARD_SUBAGENT_MODEL", "-opus", "CCBOARD_SUBAGENT_MODEL"),
                            ("CCBOARD_HEADLESS_FABLE_CAP", "12,5", "CCBOARD_HEADLESS_FABLE_CAP"), ("CCBOARD_AUTOCLOSE_GRACE", "soon", "CCBOARD_AUTOCLOSE_GRACE"),
                            ("CCBOARD_NODE_NAME", "-bad", "CCBOARD_NODE_NAME"), ("CCBOARD_MEM_PORT", "70000x", "CCBOARD_MEM_PORT")):
        r = mac.run(env={name: bad})
        assert r.returncode == 1 and word in r.stderr, (name, bad, r.stderr)
    assert mac.plists() == []
    mac.install(env=ok)
    lines = (mac.data / "env").read_text().splitlines()
    for k, v in ok.items():
        assert f"{k}={v}" in lines


# ------------------------------------------------------------------ port 443 and the help text

def test_port_443_is_never_replaced_even_with_replace_serve(mac):
    other = {"TCP": {"443": {"HTTPS": True}}, "Web": {f"{FQDN}:443": {"Handlers": {"/other": {"Proxy": "http://127.0.0.1:3000"}}}}}
    (mac.state / "ts-serve.json").write_text(json.dumps(other))
    r = mac.run(env={"CCBOARD_HTTPS_PORT": "443", "CCBOARD_REPLACE_SERVE": "1"})
    assert r.returncode == 1 and "port 443 carries something else" in r.stderr
    assert mutating(mac.calls(), "tailscale") == [] and mac.plists() == []
    # the board's own 443 mapping, with nothing else on it, is fine
    (mac.state / "ts-serve.json").write_text("{}")
    mac.install(env={"CCBOARD_HTTPS_PORT": "443"})
    assert "tailscale serve --bg --https=443 http://127.0.0.1:8000" in mac.calls()


def test_help_prints_the_header_and_changes_nothing(mac):
    for script in (mac.app / "scripts" / "install-macos.sh", mac.app / "scripts" / "uninstall-macos.sh"):
        r = mac.run(script=script, args=("--help",), check=0)
        assert r.stdout.lstrip().startswith(("ccboard installer for macOS", "Uninstall ccboard"))
        assert "#!/bin/bash" not in r.stdout and "set -euo" not in r.stdout
    assert mac.plists() == [] and not mac.data.exists()
