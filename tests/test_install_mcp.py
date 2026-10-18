"""Issue #121 (the MCP shim python): which python install.sh registers the `ccboard` MCP server with, on plain Linux, on WSL2 and in the
docker runtime. The systemd runtime (plain Linux and WSL2 alike: WSL2 is the Linux install, install.sh has no WSL branch around this step)
registers the board's own venv python; the docker runtime registers /usr/bin/python3 because claude spawns the shim on the host and the
container's python does not exist there. Nothing here runs a real claude, a real WSL or the real home: the two pieces of install.sh are
cut out of its text and run in a throwaway bash with a fake `claude`."""
import re
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "install.sh").read_text()

FAKE_CLAUDE = """#!/bin/sh
# argv log, and one entry in a state file the way `claude mcp get/add/remove` keep one
echo "claude $*" >> "$T/claude.log"
case "$1 $2" in
  "mcp get")    [ -s "$T/claude.state" ] && cat "$T/claude.state" || exit 1;;
  "mcp remove") rm -f "$T/claude.state";;
  "mcp add")    [ "${CLAUDE_ADD_RC:-0}" = 0 ] || exit 1
                [ ! -s "$T/claude.state" ] || { echo "duplicate" >> "$T/claude.dupes"; exit 1; }
                shift 4            # mcp add --scope user
                name=$1; shift 2   # name --
                printf '%s\\n  Command: %s\\n  Args: %s\\n' "$name" "$1" "$2" > "$T/claude.state";;
esac
"""


def _registration_block():
    m = re.search(r'^log "MCP server registration"\n(.*?)^# -+ Codex: hooks', TEXT, re.S | re.M)
    assert m, "install.sh lost its MCP server registration step"
    return m.group(1)


def _docker_register_fn():
    m = re.search(r"^docker_register_mcp\(\) \{.*?^\}\n", TEXT, re.S | re.M)
    assert m, "install.sh lost docker_register_mcp"
    return m.group(0)


def _run(tmp_path, body, runtime="systemd", state=None, add_rc=0):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    claude = bindir / "claude"
    claude.write_text(FAKE_CLAUDE)
    claude.chmod(0o755)
    if state is not None:
        (tmp_path / "claude.state").write_text(state)
    home, app = tmp_path / "home", tmp_path / "ccb app"
    docker_app = tmp_path / "data" / "app"
    pre = (
        'log() { :; }\nnote() { printf "note: %s\\n" "$*"; }\nwarn() { printf "warn: %s\\n" "$*" >&2; }\n'
        'have() { command -v "$1" >/dev/null 2>&1; }\n'
        f"HOME_DIR={shlex.quote(str(home))}\nAPP_DIR={shlex.quote(str(app))}\nDOCKER_APP={shlex.quote(str(docker_app))}\nCCBOARD_RUNTIME={runtime}\n"
    )
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "T": str(tmp_path), "CLAUDE_ADD_RC": str(add_rc)}
    p = subprocess.run(["bash", "-c", pre + body], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
    log = tmp_path / "claude.log"
    calls = log.read_text().splitlines() if log.exists() else []
    st = tmp_path / "claude.state"
    return p, calls, (st.read_text() if st.exists() else ""), app, docker_app


def test_the_systemd_runtime_registers_the_boards_venv_python(tmp_path):
    p, calls, state, app, _ = _run(tmp_path, _registration_block())
    assert p.returncode == 0, p.stderr
    assert calls[-1] == f"claude mcp add --scope user ccboard -- {app}/.venv/bin/python {app}/scripts/ccboard_mcp.py"
    assert state == f"ccboard\n  Command: {app}/.venv/bin/python\n  Args: {app}/scripts/ccboard_mcp.py\n"
    assert "/usr/bin/python3" not in " ".join(calls)
    assert "registered 'ccboard'" in p.stdout and not p.stderr


def test_wsl2_and_plain_linux_take_the_same_step():
    """WSL2 is the Linux install: the registration step has no WSL branch and names no system python, so a WSL2 distro gets the venv python
    exactly as an Ubuntu host does (the wsl branch of install.sh is entered before it and changes none of the variables the step reads)."""
    block = _registration_block()
    assert not re.search(r"wsl|WSL|is_wsl|/proc/version", block)
    assert "python3" not in block
    assert block.count('"$APP_DIR/.venv/bin/python"') == 1
    wsl_block = re.search(r"^# >>> wsl branch\n(.*?)^# <<< wsl branch", TEXT, re.S | re.M).group(1)
    assert "mcp" not in wsl_block.lower() and "python" not in wsl_block


def test_an_existing_registration_is_left_as_one_entry_not_added_twice(tmp_path):
    p, calls, state, app, _ = _run(tmp_path, _registration_block(), state="ccboard\n  Command: /whatever/python\n  Args: /whatever/ccboard_mcp.py\n")
    assert p.returncode == 0, p.stderr
    assert calls == ["claude mcp get ccboard"]
    assert "note: present" in p.stdout
    assert not (tmp_path / "claude.dupes").exists()


def test_a_second_run_over_the_first_runs_entry_changes_nothing(tmp_path):
    _, _, state, _, _ = _run(tmp_path, _registration_block())
    (tmp_path / "claude.log").unlink()
    p, calls, state2, _, _ = _run(tmp_path, _registration_block())
    assert p.returncode == 0 and calls == ["claude mcp get ccboard"] and state2 == state
    assert not (tmp_path / "claude.dupes").exists()


def test_a_failed_add_warns_with_the_manual_command_and_never_aborts(tmp_path):
    p, calls, _, app, _ = _run(tmp_path, _registration_block(), add_rc=1)
    assert p.returncode == 0
    assert f"claude mcp add --scope user ccboard -- {app}/.venv/bin/python {app}/scripts/ccboard_mcp.py" in p.stderr


def test_the_docker_runtime_registers_the_host_python_at_the_data_dir_copy(tmp_path):
    body = _docker_register_fn() + "\n" + _registration_block()
    p, calls, state, _, dapp = _run(tmp_path, body, runtime="docker")
    assert p.returncode == 0, p.stderr
    assert calls[-1] == f"claude mcp add --scope user ccboard -- /usr/bin/python3 {dapp}/scripts/ccboard_mcp.py"
    assert ".venv" not in state


def test_the_docker_runtime_replaces_a_registration_that_names_another_script(tmp_path):
    body = _docker_register_fn() + "\n" + _registration_block()
    old = "ccboard\n  Command: /old/.venv/bin/python\n  Args: /old/checkout/scripts/ccboard_mcp.py\n"
    p, calls, state, _, dapp = _run(tmp_path, body, runtime="docker", state=old)
    assert p.returncode == 0, p.stderr
    assert [c.split()[2] for c in calls] == ["get", "remove", "add"]
    assert state.count("ccboard") == 2 and f"Args: {dapp}/scripts/ccboard_mcp.py" in state and "Command: /usr/bin/python3" in state
    assert not (tmp_path / "claude.dupes").exists()


def test_the_docker_runtime_keeps_a_registration_that_is_already_right(tmp_path):
    body = _docker_register_fn() + "\n" + _registration_block()
    dapp = tmp_path / "data" / "app"
    right = f"ccboard\n  Command: /usr/bin/python3\n  Args: {dapp}/scripts/ccboard_mcp.py\n"
    p, calls, state, _, _ = _run(tmp_path, body, runtime="docker", state=right)
    assert calls == ["claude mcp get ccboard"] and state == right and "note: present" in p.stdout
