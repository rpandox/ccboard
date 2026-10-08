"""v0.5.10 claude-mem on the box: install.sh's claude-mem step and unit, systemd/ccboard-mem.service.in, bin/ccboard-mem-run, the README.

Nothing here touches the real ~/.claude, ~/.claude-mem or systemd: the launcher runs against a fake plugin cache and a fake bun in a
temp HOME, and install.sh's claude-mem helpers (the block between its two marker lines) run in a throwaway bash with stubbed sudo,
systemctl and claude.
"""
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
UNIT = ROOT / "systemd" / "ccboard-mem.service.in"
RUN = ROOT / "bin" / "ccboard-mem-run"
README = ROOT / "README.md"
KEY = "claude-mem@thedotmack"
NEW_KEYS = ["CCBOARD_CLAUDE_MEM", "CCBOARD_MEM_PORT", "CCBOARD_MEM_SERVICE"]
UNSET_IN_UNIT = ["CCBOARD_SESSION", "CCBOARD_URL", "CCBOARD_APPROVE_TIMEOUT", "TMUX", "TMUX_PANE", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION"]
DIRTY = {"CCBOARD_SESSION": "work-1", "CCBOARD_URL": "http://127.0.0.1:8000", "CCBOARD_APPROVE_TIMEOUT": "90", "CCBOARD_HUB_TOKEN": "secret",
         "CCBOARD_MEM_PORT": "37700", "TMUX": "/tmp/tmux-1000/ccboard,1,0", "TMUX_PANE": "%3", "CLAUDECODE": "1",
         "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_CODE_SESSION": "abc", "CLAUDE_CODE_ENTRYPOINT": "cli", "MCP_SESSION_ID": "m1"}

FAKE_BUN = '#!/bin/sh\n{ echo "argv=$*"; echo "pwd=$(pwd)"; env; } > "$FAKE_BUN_OUT"\nexit "${FAKE_BUN_RC:-0}"\n'
FAKE_CLAUDE = """#!/bin/sh
echo "$*" >> "$FAKE_CLAUDE_LOG"
case "$*" in
  "plugin marketplace add"*) [ -z "${FAKE_CLAUDE_FAIL_ADD:-}" ] || exit 1 ;;
  "plugin install "*)
    [ -z "${FAKE_CLAUDE_FAIL_INSTALL:-}" ] || exit 1
    mkdir -p "$CLAUDE_CONFIG_DIR/plugins"
    printf '{"version":2,"plugins":{"claude-mem@thedotmack":[{"scope":"user","version":"13.29.0"}]}}' > "$CLAUDE_CONFIG_DIR/plugins/installed_plugins.json" ;;
esac
exit 0
"""

# the bash the claude-mem helpers run in: the stubs install.sh would get from its own functions and from sudo/systemctl
PREAMBLE = r"""
set -euo pipefail
log()  { echo "== $*"; }
note() { echo "   $*"; }
warn() { echo "warning: $*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }
HOME_DIR=$HOME
APP_DIR=$TEST_APP_DIR
APP_BIN=$APP_DIR/bin
USER_NAME=tester SHELL_PATH=/bin/bash TTYD_BIN=/usr/local/bin/ttyd BACKUP_EXEC=/bin/true CCBOARD_BACKUP_ONCALENDAR='*-*-* 02:30:00'
sudo() { echo "sudo $*" >> "$T/calls.log"; case "$1" in install) shift; command install "$@";; esac; return 0; }
systemctl() {
  echo "systemctl $*" >> "$T/calls.log"
  case "$1" in
    show) echo "${FAKE_MAINPID:-0}";;
    is-enabled) return "${FAKE_ENABLED_RC:-1}";;
    is-active) if [ -n "${FAKE_ACTIVE:-}" ]; then return 0; fi; grep -qE '^sudo systemctl (start|restart) ' "$T/calls.log" 2>/dev/null;;
  esac
}
"""


def _install_text():
    return INSTALL.read_text()


def _block():
    m = re.search(r"^# >>> claude-mem helpers\n(.*?)^# <<< claude-mem helpers\n", _install_text(), re.S | re.M)
    assert m, "install.sh lost its claude-mem helper markers"
    return m.group(1)


def _render_unit_fn():
    t = _install_text()
    esc = re.search(r"^esc\(\) .*\n", t, re.M)
    fn = re.search(r"^render_unit\(\) \{.*?^\}\n", t, re.S | re.M)
    assert esc and fn
    return esc.group(0) + fn.group(0)


class Box:
    """A temp HOME with a fake plugin cache, a fake bun and a fake claude, and an environment that sees nothing else."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.home = tmp / "home"
        self.home.mkdir()
        self.fakebin = tmp / "fakebin"
        self.fakebin.mkdir()
        (self.fakebin / "python3").symlink_to(sys.executable)
        self.cfg = self.home / ".claude"
        self.cache = self.cfg / "plugins" / "cache" / "thedotmack" / "claude-mem"
        self.bun_out = tmp / "bun.out"
        self.claude_log = tmp / "claude.log"

    def script(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)
        return path

    def bun(self, path=None):
        return self.script(path or self.fakebin / "bun", FAKE_BUN)

    def claude(self):
        return self.script(self.fakebin / "claude", FAKE_CLAUDE)

    def plugin(self, version, orphaned=False, sub=False):
        d = self.cache / version
        w = d / ("plugin" if sub else "") / "scripts" / "worker-service.cjs"
        w.parent.mkdir(parents=True, exist_ok=True)
        w.write_text("// fake worker\n")
        if orphaned:
            (d / ".orphaned_at").write_text("1790000000000")
        return d

    def installed(self, enabled=True):
        (self.cfg / "plugins").mkdir(parents=True, exist_ok=True)
        (self.cfg / "plugins" / "installed_plugins.json").write_text(
            '{"version":2,"plugins":{"%s":[{"scope":"user","version":"13.29.0"}]}}' % KEY)
        if not enabled:
            (self.cfg / "settings.json").write_text('{"enabledPlugins":{"%s":false}}' % KEY)

    def env(self, **extra):
        e = {"PATH": f"{self.fakebin}:/usr/bin:/bin", "HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.cfg),
             "CCBOARD_MEM_BUN_DIRS": "", "FAKE_BUN_OUT": str(self.bun_out), "FAKE_CLAUDE_LOG": str(self.claude_log), "T": str(self.tmp),
             "TEST_APP_DIR": str(ROOT)}
        e.update(extra)
        return e

    def launcher(self, *args, **env):
        return subprocess.run(["sh", str(RUN), *args], env=self.env(**env), capture_output=True, text=True, timeout=30)

    def bash(self, body, **env):
        script = PREAMBLE + _render_unit_fn() + _block() + "\n" + body + "\n"
        return subprocess.run(["bash", "-c", script], env=self.env(**env), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              timeout=60)

    def calls(self):
        f = self.tmp / "calls.log"
        return f.read_text().splitlines() if f.exists() else []

    def sudo_calls(self):
        return [c for c in self.calls() if c.startswith("sudo ")]

    def claude_calls(self):
        return self.claude_log.read_text().splitlines() if self.claude_log.exists() else []

    def recorded(self):
        lines = self.bun_out.read_text().splitlines()
        env = dict(l.split("=", 1) for l in lines[2:] if "=" in l)
        return lines[0].removeprefix("argv="), lines[1].removeprefix("pwd="), env


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def _printed(r):
    return dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)


# ------------------------------------------------------------------------------------------------ syntax and static checks
def test_shell_syntax():
    assert subprocess.run(["bash", "-n", str(INSTALL)], capture_output=True, text=True).returncode == 0
    r = subprocess.run(["sh", "-n", str(RUN)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert os.access(RUN, os.X_OK), "bin/ccboard-mem-run must be executable (the unit runs it directly)"
    assert stat.S_IMODE(RUN.stat().st_mode) & 0o755 == 0o755, "mode 755: it is committed as 100755 and the entrypoint chmods the copy"


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_shellcheck():
    r = subprocess.run(["shellcheck", "-S", "warning", str(RUN)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
    r = subprocess.run(["shellcheck", "-S", "error", str(INSTALL)], capture_output=True, text=True)   # install.sh predates it: errors only
    assert r.returncode == 0, r.stdout


def test_new_env_keys_are_remembered_and_every_env_key_is_bound_before_the_env_file_is_written():
    t = _install_text()
    keys = re.search(r"^ENV_KEYS=\((.*)\)$", t, re.M).group(1).split()
    for k in NEW_KEYS:
        assert k in keys, k
    body = "\n".join(l for l in t.splitlines() if not l.startswith("ENV_KEYS="))
    # `${!k}` under set -u dies on an unbound name, so each key needs a default, an assignment or a generated value
    for k in keys:
        assert re.search(rf'(: "\$\{{{k}:[=-]|^\s*{k}=|\b{k}=\$\()', body, re.M), f"{k} is never bound in install.sh"
    assert ': "${CCBOARD_AUTO_CONTINUE:=1}"' in t, "an unbound ENV_KEYS name dies under set -u when the env file is written"
    assert ': "${CCBOARD_CLAUDE_MEM:=1}"' in t and ': "${CCBOARD_MEM_SERVICE:=0}"' in t and ': "${CCBOARD_MEM_PORT:=}"' in t


def test_values_are_validated():
    t = _install_text()
    assert 'case "$CCBOARD_CLAUDE_MEM" in 0|1) ;;' in t
    assert 'case "$CCBOARD_MEM_SERVICE" in 0|1) ;;' in t
    assert re.search(r'\[ -z "\$CCBOARD_MEM_PORT" \] \|\| \[\[ "\$CCBOARD_MEM_PORT" =~ \^\[0-9\]\{1,5\}\$ \]\]', t)


def test_call_sites_never_abort_and_sit_in_the_right_order():
    t = _install_text()
    for call in ("mem_plugin_step ||", "mem_service_setup ||"):
        assert t.count("\n" + call) == 1, call
    assert t.index('log "Claude Code"') < t.index("\nmem_plugin_step ||"), "the plugin step needs the claude binary installed first"
    assert t.index("\nrender_unit() {") < t.index("\nmem_service_setup ||"), "the unit step renders through render_unit"
    assert t.index("\nmem_service_setup ||") < t.index('log "tmux.conf (live)"')
    assert not re.search(r"curl|npm |npx |bun install|bun\.sh", _block()), "install.sh must never install bun (the plugin looks for it)"


def test_the_docker_entrypoint_does_not_install_the_unit():
    e = (ROOT / "scripts" / "docker-entrypoint.sh").read_text()
    assert "ccboard-mem" not in e and "claude plugin" not in e


# ------------------------------------------------------------------------------------------------ the unit template
def test_unit_placeholders_are_all_rendered_by_install_sh():
    text = UNIT.read_text()
    used = set(re.findall(r"__[A-Z_]+__", text))
    handled = set(re.findall(r"__[A-Z_]+__", _render_unit_fn()))
    assert used and used <= handled, used - handled


def _directives():
    out = {}
    for line in UNIT.read_text().splitlines():
        if line.strip() and not line.startswith(("#", "[")) and "=" in line:
            k, v = line.split("=", 1)
            out.setdefault(k, []).append(v)
    return out


def test_unit_directives():
    d = _directives()
    assert d["Type"] == ["simple"] and d["User"] == ["__USER__"]
    assert d["Restart"] == ["on-failure"], "an exit 0 means 'a healthy worker already runs'; looping on it would only spin"
    assert d["Nice"] == ["5"] and d["OOMScoreAdjust"] == ["200"]
    assert d["IOSchedulingClass"] == ["best-effort"] and d["IOSchedulingPriority"] == ["7"], "never idle: the observer has to keep up"
    assert "EnvironmentFile" not in d, "/etc/ccboard/env carries the hub token and CCBOARD_*: it must not reach the worker"
    assert any(".bun/bin" in v for v in d["Environment"] if v.startswith("PATH="))
    assert d["WantedBy"] == ["multi-user.target"] and d["StartLimitBurst"] == ["5"]
    assert "ProtectHome" not in d and "PrivateTmp" not in d


def test_the_unit_is_ordered_before_the_units_that_recover_the_sessions_and_requires_nothing():
    """After a boot the board relaunches Claude sessions; a hook that wins the race against the worker starts a leaky one. Ordering only."""
    d = _directives()
    assert d["Before"] == ["ccboard-tmux.service ccboard.service"]
    for name in d["Before"][0].split():
        assert (ROOT / "systemd" / f"{name}.in").is_file(), name
    for k in ("Requires", "Wants", "BindsTo", "Requisite", "PartOf", "Conflicts"):
        assert k not in d, f"{k}: a failed or disabled ccboard-mem must never block the board or tmux"
    text = UNIT.read_text()
    assert text.index("Before=") < text.index("[Service]") and "memory-env" in text


def test_unit_execstart_unsets_the_session_markers_then_runs_the_launcher():
    (line,) = _directives()["ExecStart"]
    argv = shlex.split(line)
    assert argv[0] == "/usr/bin/env"
    unset = [argv[i + 1] for i, a in enumerate(argv) if a == "-u"]
    assert sorted(unset) == sorted(UNSET_IN_UNIT)
    assert argv[-1] == "__APP_BIN__/ccboard-mem-run"


def test_the_rendered_execstart_gives_the_worker_a_clean_environment(box):
    box.plugin("13.29.0")
    box.bun()
    (line,) = _directives()["ExecStart"]
    argv = shlex.split(line.replace("__APP_BIN__", str(ROOT / "bin")))
    r = subprocess.run(argv, env=box.env(**DIRTY), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    _, _, env = box.recorded()
    for k in DIRTY:
        assert k not in env, k
    assert env["HOME"] == str(box.home)


# ------------------------------------------------------------------------------------------------ bin/ccboard-mem-run
def test_launcher_runs_the_newest_cached_version_that_is_not_orphaned(box):
    for v in ("13.9.0", "13.28.0", "13.29.0"):
        box.plugin(v)
    box.plugin("13.30.0", orphaned=True)            # an update's leftovers are marked, not removed
    box.plugin("13.31.0-beta.1")                    # a plain release outranks a pre-release
    box.bun()
    r = box.launcher("--print")
    assert r.returncode == 0, r.stderr
    p = _printed(r)
    assert p["plugin"] == str(box.cache / "13.29.0")
    assert p["worker"] == str(box.cache / "13.29.0" / "scripts" / "worker-service.cjs")
    assert p["bun"] == str(box.fakebin / "bun")
    assert not box.bun_out.exists(), "--print must not start anything"


def test_launcher_falls_back_to_a_prerelease_and_reads_the_plugin_subdir_layout(box):
    box.plugin("13.31.0-beta.1", sub=True)
    box.bun()
    p = _printed(box.launcher("--print"))
    assert p["plugin"] == str(box.cache / "13.31.0-beta.1" / "plugin")


def test_launcher_plugin_dir_override(box):
    box.plugin("13.29.0")
    mine = box.plugin("13.5.0")
    box.bun()
    assert _printed(box.launcher("--print", CCBOARD_MEM_PLUGIN_DIR=str(mine)))["plugin"] == str(mine)
    r = box.launcher("--print", CCBOARD_MEM_PLUGIN_DIR=str(box.tmp / "nowhere"))
    assert r.returncode == 78 and "CCBOARD_MEM_PLUGIN_DIR" in r.stderr


def test_launcher_execs_the_foreground_daemon_in_the_data_dir_with_a_clean_environment(box):
    d = box.plugin("13.29.0")
    box.bun()
    r = box.launcher(**DIRTY, KEEP_ME="1", FAKE_BUN_RC="3")
    assert r.returncode == 3, "bun is exec'd: its exit status is the unit's"
    argv, pwd, env = box.recorded()
    assert argv == f"{d}/scripts/worker-service.cjs --daemon", "the plugin's own start child (setsid <bun> worker-service.cjs --daemon), not bun-runner"
    assert Path(pwd).resolve() == (box.home / ".claude-mem").resolve()
    for k in DIRTY:
        assert k not in env, k
    assert not [k for k in env if k.startswith("CCBOARD_")]
    assert env["HOME"] == str(box.home) and env["KEEP_ME"] == "1" and "PATH" in env


def test_launcher_data_dir_follows_the_plugins_setting(box):
    box.plugin("13.29.0")
    box.bun()
    data = box.tmp / "elsewhere"
    assert box.launcher(CLAUDE_MEM_DATA_DIR=str(data)).returncode == 0
    assert Path(box.recorded()[1]).resolve() == data.resolve()


def test_launcher_bun_lookup_order(box):
    box.plugin("13.29.0")
    paths = {k: box.bun(box.tmp / "bins" / k / "bun") for k in ("own", "env", "bunpath", "install", "dirs")}
    home_bun = box.bun(box.home / ".bun" / "bin" / "bun")
    sysdir = box.tmp / "bins" / "sys"
    box.bun(sysdir / "bun")
    path_bun = box.bun()
    install_root = box.tmp / "bins" / "install_root"
    box.bun(install_root / "bin" / "bun")

    def bun(**env):
        r = box.launcher("--print", **env)
        assert r.returncode == 0, r.stderr
        return _printed(r)["bun"]

    assert bun(CCBOARD_MEM_BUN=str(paths["own"]), BUN=str(paths["env"]), BUN_PATH=str(paths["bunpath"])) == str(paths["own"])
    assert bun(BUN=str(paths["env"]), BUN_PATH=str(paths["bunpath"])) == str(paths["env"])
    assert bun(BUN_PATH=str(paths["bunpath"]), BUN_INSTALL=str(install_root)) == str(paths["bunpath"])
    assert bun(BUN_INSTALL=str(install_root)) == str(install_root / "bin" / "bun")
    assert bun() == str(home_bun)                                                       # ~/.bun/bin/bun before the system dirs and PATH
    home_bun.unlink()
    assert bun(CCBOARD_MEM_BUN_DIRS=f"/nonexistent {sysdir}") == str(sysdir / "bun")   # the plugin's list of system directories
    assert bun() == str(path_bun)                                                       # PATH last: a systemd PATH is short


def test_the_doctor_finds_the_same_bun_as_the_launcher(box, monkeypatch):
    """doctor._mem_find_bun is the launcher's find_bun in Python: same candidates, same order, same answer in every environment."""
    from app import doctor
    from app import platform as plat
    monkeypatch.setattr(plat, "resolve_bin", lambda name: None)    # the doctor's macOS fallback (login shell, Homebrew) is out of play: only PATH is compared
    box.plugin("13.29.0")
    b = box.tmp / "bins"
    own, env_bun, bun_path, install_root, install_flat, sysdir = (str(b / n / "bun") for n in
                                                                  ("own", "env", "bunpath", "install_root/bin", "install_flat", "sys"))
    for path in (own, env_bun, bun_path, install_root, install_flat, sysdir):
        box.bun(Path(path))
    home_bun = box.bun(box.home / ".bun" / "bin" / "bun")
    path_bun = box.bun()                                                                # <fakebin>/bun, on PATH
    plain = b / "plain" / "bun"
    plain.parent.mkdir(parents=True)
    plain.write_text("not executable")

    def both(**env):
        r = subprocess.run(["sh", str(RUN), "--print"], env=box.env(**env), capture_output=True, text=True, timeout=30)
        assert r.returncode in (0, 78), r.stderr
        monkeypatch.setattr(os, "environ", dict(box.env(**env)))
        found, where = doctor._mem_find_bun()
        assert where[:5] == ["CCBOARD_MEM_BUN", "BUN", "BUN_PATH", "BUN_INSTALL", "~/.bun/bin"] and where[-1] == "PATH"
        launcher = _printed(r)["bun"] if r.returncode == 0 else None
        assert found == launcher, (env, found, launcher)
        return found

    assert both(CCBOARD_MEM_BUN=own, BUN=env_bun, BUN_PATH=bun_path) == own
    assert both(BUN=env_bun, BUN_PATH=bun_path) == env_bun
    assert both(BUN_PATH=bun_path, BUN_INSTALL=str(b / "install_root")) == bun_path
    assert both(BUN_INSTALL=str(b / "install_root")) == install_root                    # $BUN_INSTALL/bin/bun
    assert both(BUN_INSTALL=str(b / "install_flat")) == install_flat                    # $BUN_INSTALL/bun
    assert both(CCBOARD_MEM_BUN=str(plain)) == str(home_bun)                            # not executable: skipped by both
    assert both(BUN=str(b / "missing" / "bun")) == str(home_bun)
    assert both() == str(home_bun)
    home_bun.unlink()
    assert both(CCBOARD_MEM_BUN_DIRS=f"/nonexistent {sysdir[:-4]}") == sysdir          # a set list replaces the default one
    assert both() == str(path_bun)                                                      # the box's empty list, then PATH
    path_bun.unlink()
    assert both() is None
    assert both(CCBOARD_MEM_BUN_DIRS=sysdir[:-4]) == sysdir


def test_launcher_exits_78_with_the_fix_when_the_plugin_or_bun_is_missing(box):
    box.bun()
    r = box.launcher("--print")
    assert r.returncode == 78 and "claude plugin install claude-mem@thedotmack" in r.stderr
    box.plugin("13.29.0")
    box.plugin("13.30.0", orphaned=True)
    (box.fakebin / "bun").unlink()
    r = box.launcher()
    assert r.returncode == 78 and "bun not found" in r.stderr and "CCBOARD_MEM_BUN" in r.stderr
    assert not box.bun_out.exists()
    r = box.launcher(HOME="")
    assert r.returncode == 78 and "HOME" in r.stderr


# ------------------------------------------------------------------------------------------------ install.sh: the plugin step
def _plugin_step(box, mem="1", **env):
    return box.bash(f'CCBOARD_CLAUDE_MEM={mem}\nmem_plugin_step || warn "step failed"\necho "rc=$?"', **env)


def _runtime_ready(box):
    box.plugin("13.29.0")
    box.bun()


def test_plugin_present_changes_nothing(box):
    box.installed()
    box.claude()
    _runtime_ready(box)
    r = _plugin_step(box)
    assert "rc=0" in r.stdout and "present: claude-mem 13.29.0 (enabled)" in r.stdout
    assert f"worker runtime: {box.fakebin / 'bun'}" in r.stdout
    assert box.claude_calls() == []


def test_plugin_present_but_disabled_says_how_to_enable_it(box):
    box.installed(enabled=False)
    box.claude()
    _runtime_ready(box)
    r = _plugin_step(box)
    assert "claude plugin enable claude-mem@thedotmack" in r.stdout and "rc=0" in r.stdout
    assert box.claude_calls() == []


def test_plugin_present_without_bun_warns_and_does_not_fail(box):
    box.installed()
    box.plugin("13.29.0")
    r = _plugin_step(box)
    assert "cannot be launched on this box yet" in r.stdout and "bun not found" in r.stdout and "rc=0" in r.stdout


def test_missing_plugin_prints_the_two_commands_first_then_runs_them(box):
    box.claude()
    _runtime_ready(box)
    r = _plugin_step(box)
    out = r.stdout
    assert "rc=0" in out
    add, inst = "claude plugin marketplace add thedotmack/claude-mem", f"claude plugin install {KEY}"
    assert out.index(add) < out.index(inst) < out.index("installed claude-mem 13.29.0")
    assert box.claude_calls() == ["plugin marketplace add thedotmack/claude-mem", f"plugin install {KEY}"]


def test_a_marketplace_that_is_already_added_does_not_stop_the_install(box):
    box.claude()
    _runtime_ready(box)
    r = _plugin_step(box, FAKE_CLAUDE_FAIL_ADD="1")
    assert "continuing with the install" in r.stdout and "installed claude-mem 13.29.0" in r.stdout
    assert len(box.claude_calls()) == 2


def test_a_failed_install_is_a_warning_never_an_abort(box):
    box.claude()
    r = _plugin_step(box, FAKE_CLAUDE_FAIL_INSTALL="1")
    assert "rc=0" in r.stdout and "failed" in r.stdout and "still not in installed_plugins.json" in r.stdout


def test_missing_plugin_without_claude_prints_the_commands_and_goes_on(box):
    r = _plugin_step(box)
    assert "rc=0" in r.stdout and "claude is not available" in r.stdout and "claude plugin install" in r.stdout


def test_the_plugin_step_is_skipped_when_asked(box):
    box.claude()
    r = _plugin_step(box, mem="0")
    assert "skipped (CCBOARD_CLAUDE_MEM=0)" in r.stdout and "rc=0" in r.stdout
    assert box.claude_calls() == []


# ------------------------------------------------------------------------------------------------ install.sh: the unit step
def _service(box, mem="1", svc="1", **env):
    return box.bash(f'CCBOARD_CLAUDE_MEM={mem} CCBOARD_MEM_SERVICE={svc}\nMEM_UNIT_DIR=$T/units; mkdir -p "$MEM_UNIT_DIR"; MEM_SETTLE=0\n'
                    f'mem_service_setup || warn "step failed"\necho "rc=$?"', **env)


def _unit_file(box):
    return box.tmp / "units" / "ccboard-mem.service"


def test_off_by_default_installs_nothing(box):
    r = _service(box, svc="0")
    assert "rc=0" in r.stdout and "ccboard-mem.service is off" in r.stdout
    assert box.sudo_calls() == [] and not _unit_file(box).exists()


def test_turning_it_off_disables_an_installed_unit_and_leaves_the_file(box):
    (box.tmp / "units").mkdir()
    _unit_file(box).write_text("[Service]\n")
    r = _service(box, svc="0", FAKE_ENABLED_RC="0")
    assert box.sudo_calls() == ["sudo systemctl disable --now ccboard-mem.service"]
    assert "started by Claude's hooks again" in r.stdout and _unit_file(box).exists()


def test_no_plugin_means_no_service(box):
    r = _service(box)
    assert "ccboard-mem.service not installed" in r.stdout and "claude-mem plugin is not installed" in r.stdout
    assert box.sudo_calls() == [] and not _unit_file(box).exists()
    _runtime_ready(box)
    r = _service(box, mem="0")                                       # no plugin step, no monitor, so no unit either
    assert box.sudo_calls() == [] and not _unit_file(box).exists()


def test_service_is_written_enabled_and_started(box):
    _runtime_ready(box)
    r = _service(box, CCBOARD_MEM_BUN=str(box.fakebin / "bun"))
    assert "rc=0" in r.stdout and "ccboard-mem.service running" in r.stdout, r.stdout
    assert "CLAUDE_MEM_WORKER_AUTOSTART" in r.stdout and "Optional, your decision" in r.stdout, "the opt-in is printed, never applied"
    assert "a plugin update can undo this" in r.stdout and "worker-service.cjs stop, then sudo systemctl start ccboard-mem.service" in r.stdout
    assert not (box.home / ".claude-mem" / "settings.json").exists(), "ccboard must never write the plugin's settings"
    unit = _unit_file(box).read_text()
    assert "__" not in unit and "User=tester" in unit and f"WorkingDirectory={box.home}" in unit
    assert f"{ROOT / 'bin'}/ccboard-mem-run" in unit
    assert box.sudo_calls() == [f"sudo install -m 0644 {box.sudo_calls()[0].split()[-2]} {_unit_file(box)}",
                                "sudo systemctl daemon-reload", "sudo systemctl enable ccboard-mem.service",
                                "sudo systemctl start ccboard-mem.service"]


def test_a_changed_unit_restarts_a_running_one_and_an_unchanged_one_does_not(box):
    _runtime_ready(box)
    (box.tmp / "units").mkdir()
    _unit_file(box).write_text("[Service]\n# an older render\n")
    _service(box, FAKE_ACTIVE="1")
    assert "sudo systemctl restart ccboard-mem.service" in box.sudo_calls()
    assert "sudo systemctl start ccboard-mem.service" not in box.sudo_calls()
    (box.tmp / "calls.log").unlink()
    r = _service(box, FAKE_ACTIVE="1")
    assert "wrote" not in r.stdout
    assert not [c for c in box.sudo_calls() if " install " in c or "restart" in c]
    assert "sudo systemctl start ccboard-mem.service" in box.sudo_calls()   # a no-op for a unit that is already active


def test_a_worker_a_hook_started_is_handed_over_by_hand_never_killed(box):
    _runtime_ready(box)
    p = subprocess.Popen(["sleep", "60"])
    try:
        (box.home / ".claude-mem").mkdir()
        (box.home / ".claude-mem" / "worker.pid").write_text('{"pid": %d, "port": 37700}' % p.pid)
        r = _service(box)
        out = r.stdout
        assert "rc=0" in out and f"started by a hook is running (pid {p.pid})" in out
        assert f"{box.fakebin / 'bun'} {box.cache / '13.29.0' / 'scripts' / 'worker-service.cjs'} stop" in out
        assert "sudo systemctl start ccboard-mem.service" in out and "grep -c '^CCBOARD_SESSION='" in out
        assert "a plugin update can undo this" in out and f"{box.fakebin / 'bun'} {box.cache / '13.29.0' / 'scripts' / 'worker-service.cjs'} stop, then" in out
        assert f'"CLAUDE_MEM_WORKER_AUTOSTART": "false" in {box.home / ".claude-mem"}/settings.json (plugin 13.29.0)' in out
        assert "memory-worker check is what tells you" in out
        assert not (box.home / ".claude-mem" / "settings.json").exists(), "install.sh only prints the setting, it never writes it"
        assert "sudo systemctl enable ccboard-mem.service" in box.sudo_calls()
        assert not [c for c in box.sudo_calls() if c.endswith(("start ccboard-mem.service", "restart ccboard-mem.service"))]
        assert p.poll() is None, "the running worker is left alone"
        # the same pid as the unit's MainPID is our own worker: nothing to hand over
        (box.tmp / "calls.log").unlink()
        _service(box, FAKE_MAINPID=str(p.pid))
        assert "sudo systemctl start ccboard-mem.service" in box.sudo_calls()
    finally:
        p.kill()
        p.wait()


def test_a_stale_worker_pid_file_is_not_a_running_worker(box):
    _runtime_ready(box)
    p = subprocess.Popen(["true"])
    p.wait()
    (box.home / ".claude-mem").mkdir()
    (box.home / ".claude-mem" / "worker.pid").write_text('{"pid": %d, "port": 37700}' % p.pid)
    r = _service(box)
    assert "sudo systemctl start ccboard-mem.service" in box.sudo_calls() and "started by a hook" not in r.stdout


# ------------------------------------------------------------------------------------------------ README
def test_readme_documents_the_step_the_unit_and_the_handover():
    t = README.read_text()
    assert "\n## claude-mem\n" in t
    for k in NEW_KEYS:
        assert re.search(rf"^\| `{k}` \|", t, re.M), f"{k} is missing from the settings table"
    sec = t.split("\n## claude-mem\n", 1)[1].split("\n## ", 1)[0]
    for needle in ("claude plugin marketplace add thedotmack/claude-mem", f"claude plugin install {KEY}", "CCBOARD_MEM_SERVICE=1 ./install.sh",
                   "worker-service.cjs --daemon", "ccboard-mem-run", ".orphaned_at", "bun-runner.js", "CAPTURE_BROKEN", "reset-failed",
                   "CCBOARD_SESSION", "/etc/ccboard/env", "systemctl edit ccboard-mem", "Restart=on-failure", "OOMScoreAdjust=200",
                   "Before=ccboard-tmux.service ccboard.service", "`memory-env` check is the safety net"):
        assert needle in sec, needle


def test_readme_says_what_a_plugin_update_does_and_documents_the_autostart_switch():
    t = README.read_text()
    sec = t.split("\n## claude-mem\n", 1)[1].split("\n## ", 1)[0]
    flat = " ".join(sec.split())
    assert "keeps running until you do" not in flat and "the old version keeps running" not in flat, "13.29.0 recycles a stale worker from a hook"
    for needle in ("Worker version mismatch", "kills the unit's worker and spawns a replacement itself, with that hook's environment",
                   "exits 0, so the unit sits inactive", "<bun> <plugin>/scripts/worker-service.cjs stop", "then `sudo systemctl start ccboard-mem`",
                   "CLAUDE_MEM_WORKER_AUTOSTART", "~/.claude-mem/settings.json", "Per the plugin's code (13.29.0)", "using it as is",
                   'the string `"false"`', "a JSON boolean has no such method", "inside the top-level object",
                   "nothing starts a worker unless the unit runs", "`memory-worker`", "ccboard never writes it", "only prints this hint"):
        assert needle in flat, needle
    row = next(l for l in t.splitlines() if l.startswith("| `CCBOARD_MEM_PORT` |"))
    assert "37700 plus your uid modulo 100" in row and "then 37701)" not in row
    assert "ccboard-mem" in t.split("### Uninstall", 1)[1]
