"""Issue #121: the hook scripts and their registration are portable. Claude hook paths are quoted (a checkout under a path with a space
works), an earlier unquoted entry is still found and replaced, the shell scripts use only POSIX sh, and the doctor names a missing
curl or python3 and what happens without them. Temp dirs and fakes only: no real claude, curl or python3 is started from here."""
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "claude_settings.py"


def _load():
    spec = importlib.util.spec_from_file_location("claude_settings_portable", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cs = _load()
PLAIN = Path("/opt/ccboard")
ASYNC = cs.ASYNC_EVENTS


def _golden(app: str) -> dict:
    """What install() wrote for a path without a space before #121: the bare path in every command (the pin)."""
    hooks = {}
    for ev in ASYNC:
        hooks[ev] = [{"hooks": [{"type": "command", "command": f"{app}/bin/ccboard-hook", "async": True, "timeout": 5}]}]
    hooks["SessionEnd"] = [{"hooks": [{"type": "command", "command": f"{app}/bin/ccboard-hook-fast", "timeout": 3}]}]
    hooks["PermissionRequest"] = [{"hooks": [{"type": "command", "command": f"{app}/bin/ccboard-permission", "timeout": 120}]}]
    return {"hooks": hooks, "statusLine": {"type": "command", "command": f"{app}/bin/ccboard-statusline", "refreshInterval": 30}}


# ---------------------------------------------------------------- the Claude hook command strings (#121)
def test_plain_path_writes_the_same_json_as_before():
    """Linux pin: for a path of safe characters shlex.quote returns it unchanged, so the file is byte for byte what it was."""
    out = cs.install({}, PLAIN)
    assert out == _golden("/opt/ccboard")
    assert json.dumps(out, indent=2) == json.dumps(_golden("/opt/ccboard"), indent=2)
    assert list(out["hooks"]) == [*ASYNC, "SessionEnd", "PermissionRequest"]       # the event order is part of the bytes


@pytest.mark.parametrize("name", ["with space", "it's", "a&b", "x y's & $z", "semi;colon", "tab\there"])
def test_odd_paths_parse_back_to_the_original(name):
    app = Path("/tmp") / name / "ccboard"
    out = cs.install({}, app)
    commands = [h["command"] for groups in out["hooks"].values() for g in groups for h in g["hooks"]] + [out["statusLine"]["command"]]
    assert len(commands) == len(cs.EVENTS) + 2
    for command in commands:
        argv = shlex.split(command)
        assert len(argv) == 1 and argv[0].startswith(str(app) + "/bin/ccboard-"), command


def test_each_of_the_four_scripts_is_quoted_with_a_space():
    app = Path("/srv/my app")
    out = cs.install({}, app)
    assert out["hooks"]["Stop"][0]["hooks"][0]["command"] == "'/srv/my app/bin/ccboard-hook'"
    assert out["hooks"]["SessionEnd"][0]["hooks"][0]["command"] == "'/srv/my app/bin/ccboard-hook-fast'"
    assert out["hooks"]["PermissionRequest"][0]["hooks"][0]["command"] == "'/srv/my app/bin/ccboard-permission'"
    assert out["statusLine"]["command"] == "'/srv/my app/bin/ccboard-statusline'"


def test_an_old_unquoted_entry_is_found_replaced_once_and_then_left_alone():
    """Settings written by an earlier install (bare paths, even one with a space) are stripped by their marks and rewritten once."""
    old = _golden("/srv/my app")
    old["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "/usr/local/bin/mine"}]})
    first = cs.install(json.loads(json.dumps(old)), Path("/srv/my app"))
    stop = [h["command"] for g in first["hooks"]["Stop"] for h in g["hooks"]]
    assert stop.count("'/srv/my app/bin/ccboard-hook'") == 1 and "/usr/local/bin/mine" in stop     # replaced, the foreign one kept
    assert "/srv/my app/bin/ccboard-hook" not in stop
    assert len(first["hooks"]["PermissionRequest"]) == 1
    assert first["statusLine"]["command"] == "'/srv/my app/bin/ccboard-statusline'"
    second = cs.install(json.loads(json.dumps(first)), Path("/srv/my app"))
    assert second == first


def test_plain_path_rerun_changes_nothing():
    once = cs.install({}, PLAIN)
    assert cs.install(json.loads(json.dumps(once)), PLAIN) == once


def test_is_ours_still_matches_a_quoted_command():
    quoted = {"type": "command", "command": "'/srv/my app/bin/ccboard-permission'"}
    assert cs.is_ours(quoted) and cs.is_ours({"type": "command", "command": "'/x y/bin/ccboard-hook-fast'"})
    assert cs.is_ours({"type": "command", "command": "'/x y/bin/ccboard-statusline'"}, cs.STATUS_MARK)


# ---------------------------------------------------------------- the shell scripts stay POSIX sh (#121)
# macOS /bin/sh is bash 3.2 in POSIX mode and has BSD userland: no bash-only syntax and no GNU-only flag in the scripts a hook runs.
BIN_SCRIPTS = sorted(p for p in (ROOT / "bin").iterdir() if p.is_file())
DOCKER_ONLY = [ROOT / "scripts" / "ccboard-deploy-gate", ROOT / "scripts" / "ccboard-watchdog.sh"]      # Linux hosts only: syntax only
BASHISMS = {
    "[[ ... ]]": r"\[\[",
    "here-string <<<": r"<<<",
    "array assignment": r"(?:^|[\s;])\w+=\(",
    "declare/typeset": r"\b(?:declare|typeset)\s",
    "${var,,} / ${var^^}": r"\$\{[^}]*(?:,,|\^\^)",
    "${var//a/b} / ${var:1:2}": r"\$\{\w+(?://|:[0-9])",
    "echo -e": r"\becho\s+-[a-zA-Z]*e",
    "local outside a function": r"^local\s",
    "function keyword": r"^\s*function\s+\w+",
    "source": r"(?:^|[;&|]\s*)source\s",
    "&> / |&": r"&>|\|&",
    "process substitution": r"<\(|>\(",
    "$'...'": r"\$'",
}
GNU_ONLY = {
    "sed -i without a suffix": r"\bsed\s+(?:-\S+\s+)*-i(?:\s|$)",
    "date -d": r"\bdate\s+(?:-\S+\s+)*-d\b",
    "stat -c": r"\bstat\s+(?:-\S+\s+)*-c\b",
    "readlink -f": r"\breadlink\s+(?:-\S+\s+)*-f\b",
    "grep -P": r"\bgrep\s+(?:-\S+\s+)*-\w*P",
    "install -D": r"\binstall\s+(?:-\S+\s+)*-\w*D",
}


def _shell_code(text: str) -> str:
    """The script without comment lines and without the Python programs inside `python3 -c '...'` (their `[[`, `//` and `(` are Python)."""
    text = re.sub(r"python3 -c '.*?'", "python3 -c ''", text, flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _violations(text: str) -> list[str]:
    code = _shell_code(text)
    return [name for name, rx in {**BASHISMS, **GNU_ONLY}.items() if re.search(rx, code, re.M)]


@pytest.mark.parametrize("script", BIN_SCRIPTS, ids=lambda p: p.name)
def test_bin_scripts_use_only_posix_sh_and_portable_flags(script):
    assert script.read_text().startswith("#!/bin/sh\n"), "bin/* run under sh, not bash"
    assert _violations(script.read_text()) == []


def test_there_are_hook_scripts_to_scan():
    assert {p.name for p in BIN_SCRIPTS} >= {"ccboard-hook", "ccboard-hook-fast", "ccboard-permission", "ccboard-statusline", "ccboard-attach"}


@pytest.mark.posix_sh
@pytest.mark.parametrize("script", [*BIN_SCRIPTS, *DOCKER_ONLY], ids=lambda p: p.name)
def test_scripts_parse_under_sh(script):
    r = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_bin_scripts_pass_shellcheck_as_sh():
    r = subprocess.run(["shellcheck", "-s", "sh", "-S", "error", *map(str, BIN_SCRIPTS)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("bad,name", [
    ('if [[ -n "$x" ]]; then :; fi', "[[ ... ]]"), ("cat <<< hi", "here-string <<<"), ("a=(1 2 3)", "array assignment"),
    ('echo "${v,,}"', "${var,,} / ${var^^}"), ('x=${v//a/b}', "${var//a/b} / ${var:1:2}"), ("echo -e 'a\\tb'", "echo -e"),
    ("local x=1", "local outside a function"), ("source ./x", "source"), ("cmd &> /dev/null", "&> / |&"), ("sed -i 's/a/b/' f", "sed -i without a suffix"),
    ("sed -n -i 's/a/b/' f", "sed -i without a suffix"), ("date -d yesterday", "date -d"), ("stat -c %s f", "stat -c"),
    ("readlink -f x", "readlink -f"), ("grep -oP 'a' f", "grep -P"), ("install -D a b", "install -D"),
])
def test_the_scan_catches_each_forbidden_feature(bad, name):
    assert name in _violations(bad + "\n")


def test_the_scan_leaves_python_and_comments_alone():
    ok = "# local x [[ here ]]\npython3 -c '\nm=(d.get(1) or {})[[0]]\nprint(a // b)\n'\nsed -i.bak s/a/b/ f\nx=${y:-0}\nf() { local z=1; }\n"
    assert _violations(ok) == []


# ---------------------------------------------------------------- BROWSER stub for the login sessions (#121)
def test_no_login_code_hardcodes_a_bin_true():
    for rel in ("app/claude_auth.py", "app/codex_accounts.py"):
        assert "/bin/true" not in (ROOT / rel).read_text(), rel
        assert "platform.browser_stub()" in (ROOT / rel).read_text(), rel


def test_browser_stub_is_an_executable_on_the_test_host_and_keeps_bin_true_where_it_exists():
    from app import platform
    stub = platform.browser_stub()
    assert os.access(stub, os.X_OK) or shutil.which(stub), stub
    if os.path.exists("/bin/true"):
        assert stub == "/bin/true"                      # Linux: the login env is byte for byte what it was


def test_the_claude_login_session_gets_the_stub(monkeypatch):
    from app import claude_auth, platform, tmux
    from app.config import settings
    seen = {}
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(platform, "browser_stub", lambda: "/stub/true")
    monkeypatch.setattr(tmux, "kill_session", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "new_session", lambda name, cwd, env=None, **k: seen.update(env=env))
    monkeypatch.setattr(tmux, "send_line", lambda *a, **k: None)
    monkeypatch.setattr(claude_auth, "invalidate", lambda: None)
    claude_auth.start_login()
    assert seen["env"] == {"BROWSER": "/stub/true"}


# ---------------------------------------------------------------- the scripts under /bin/sh with fakes on a temp PATH (#121)
def _fake_path(tmp_path, curl_body="", python=True, extra=()):
    """A PATH of one temp directory: a fake curl that records its argv and prints $FAKE_CURL_OUT, `cat` and `sleep` linked from the
    host, and (python=True) a python3 that runs this interpreter. Nothing else is reachable, so a missing helper is really missing."""
    d = tmp_path / "fakebin"
    d.mkdir(exist_ok=True)
    (d / "curl").write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$FAKE_CURL_LOG"\nprintf "%s" "${FAKE_CURL_OUT:-}"\n' + curl_body)
    (d / "curl").chmod(0o755)
    for tool in ("cat", "sleep", *extra):
        if not (d / tool).exists():
            (d / tool).symlink_to(shutil.which(tool))
    if python:
        (d / "python3").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        (d / "python3").chmod(0o755)
    return d


def _env(tmp_path, path, out="", token="sekret"):
    tok = tmp_path / "tok"
    if token is not None:
        tok.write_text(token + "\n")
    return {"PATH": str(path), "HOME": str(tmp_path), "CCBOARD_HOOK_TOKEN_FILE": str(tok), "CCBOARD_URL": "http://127.0.0.1:9",
            "FAKE_CURL_LOG": str(tmp_path / "curl.log"), "FAKE_CURL_OUT": out}


def _sh(script, env, stdin=b"{}"):
    return subprocess.run(["/bin/sh", str(ROOT / "bin" / script)], input=stdin, capture_output=True, env=env, timeout=15)


@pytest.mark.posix_sh
def test_permission_hook_answers_with_curl_and_python3(tmp_path):
    env = _env(tmp_path, _fake_path(tmp_path), out='{"behavior":"deny","message":"no"}')
    r = _sh("ccboard-permission", env)
    assert r.returncode == 0
    assert json.loads(r.stdout)["hookSpecificOutput"]["decision"] == {"behavior": "deny", "message": "no"}
    log = (tmp_path / "curl.log").read_text()
    for header in ("X-CCBoard-Token: sekret", "X-CCBoard-Agent: claude", "Content-Type: application/json"):
        assert header in log, header


@pytest.mark.posix_sh
def test_permission_hook_without_python3_prints_nothing_and_exits_0(tmp_path):
    """The board answered allow, but python3 cannot build the reply: the hook is silent and the TUI prompt appears."""
    env = _env(tmp_path, _fake_path(tmp_path, python=False), out='{"behavior":"allow"}')
    r = _sh("ccboard-permission", env)
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""


@pytest.mark.posix_sh
def test_permission_hook_with_a_stub_python3_prints_nothing_and_exits_0(tmp_path):
    path = _fake_path(tmp_path, python=False)
    (path / "python3").write_text("#!/bin/sh\necho 'xcode-select: note: no developer tools were found' >&2\nexit 1\n")
    (path / "python3").chmod(0o755)
    r = _sh("ccboard-permission", _env(tmp_path, path, out='{"behavior":"allow"}'))
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""


@pytest.mark.posix_sh
@pytest.mark.parametrize("script", ["ccboard-permission", "ccboard-hook", "ccboard-hook-fast", "ccboard-statusline"])
def test_a_missing_token_file_exits_0_and_posts_nothing(tmp_path, script):
    env = _env(tmp_path, _fake_path(tmp_path), token=None)
    env["CCBOARD_HOOK_TOKEN_FILE"] = str(tmp_path / "no-such-token")
    r = _sh(script, env)
    assert r.returncode == 0
    assert not (tmp_path / "curl.log").exists()


@pytest.mark.posix_sh
def test_statusline_prints_the_line_with_python3(tmp_path):
    env = _env(tmp_path, _fake_path(tmp_path))
    r = _sh("ccboard-statusline", env, stdin=json.dumps({"model": {"display_name": "Opus"}, "context_window": {"used_percentage": 12.4}}).encode())
    assert r.returncode == 0 and r.stdout.decode().strip() == "Opus · ctx 12%"


@pytest.mark.posix_sh
def test_statusline_without_python3_prints_nothing_and_exits_0(tmp_path):
    env = _env(tmp_path, _fake_path(tmp_path, python=False))
    r = _sh("ccboard-statusline", env, stdin=b'{"model":{"display_name":"Opus"}}')
    assert r.returncode == 0 and r.stdout.strip() == b"" and r.stderr == b""


@pytest.mark.posix_sh
def test_a_hook_without_curl_exits_0_silently(tmp_path):
    path = _fake_path(tmp_path)
    (path / "curl").unlink()
    r = _sh("ccboard-hook", _env(tmp_path, path))
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""


# ---------------------------------------------------------------- doctor check hook-helpers (#121)
from app import doctor as _doctor    # noqa: E402

REAL_LOGIN_PATH = _doctor._login_path          # captured before the fixture below replaces it


@pytest.fixture
def helpers(tmp_path, monkeypatch):
    """The real doctor._run under a PATH of one temp directory (fake curl, fake python3), the login shell's PATH not read."""
    from app import doctor, platform
    path = tmp_path / "hbin"
    path.mkdir()

    def put(name, body):
        (path / name).write_text("#!/bin/sh\n" + body)
        (path / name).chmod(0o755)
    put("curl", "exit 0\n")
    put("python3", "exit 0\n")
    for tool in ("sleep",):
        (path / tool).symlink_to(shutil.which(tool))
    monkeypatch.setenv("PATH", str(path))
    monkeypatch.setattr(doctor, "_login_path", lambda: "")
    monkeypatch.setattr(platform, "IS_MACOS", False)
    monkeypatch.setattr(platform, "IS_LINUX", True)
    monkeypatch.setattr(doctor, "HELPER_TIMEOUT", 0.4)

    class H:
        pass
    h = H()
    h.doctor, h.platform, h.dir, h.put = doctor, platform, path, put
    h.run = lambda: doctor._c_hook_helpers(None)

    def mac():
        monkeypatch.setattr(platform, "IS_MACOS", True)
        monkeypatch.setattr(platform, "IS_LINUX", False)
    h.mac = mac
    return h


def test_hook_helpers_is_a_registered_claude_check():
    from app import doctor
    assert [c for c in doctor.CHECKS if c[0] == "hook-helpers"][0][1] == "claude"
    ids = [c[0] for c in doctor.CHECKS if c[1] == "claude"]
    assert ids.index("hook-helpers") > ids.index("claude-hooks")


def test_hook_helpers_pass_when_both_run(helpers):
    out = helpers.run()
    assert out.status == "pass" and out.fix is None


def test_hook_helpers_missing_python3_warns_with_the_consequence_and_a_fix(helpers):
    (helpers.dir / "python3").unlink()
    out = helpers.run()
    assert out.status == "warn"
    assert "python3 is not found" in out.detail and "terminal prompt" in out.detail and "statusline shows nothing" in out.detail
    assert out.fix and out.fix["text"] == "Install python3" and "python3" in out.fix["cmd"]


def test_hook_helpers_missing_python3_on_a_mac_names_brew_not_apt(helpers):
    helpers.mac()
    (helpers.dir / "python3").unlink()
    out = helpers.run()
    assert out.status == "warn" and "apt" not in (out.fix["text"] + str(out.fix.get("cmd"))).lower()
    assert out.fix["cmd"] == "brew install python"


def test_hook_helpers_stub_python3_on_a_mac_is_the_command_line_tools(helpers):
    helpers.put("python3", "echo 'xcode-select: note: no developer tools were found' >&2\nexit 1\n")
    helpers.put("xcode-select", "exit 2\n")
    helpers.mac()
    out = helpers.run()
    assert out.status == "warn" and "Command Line Tools" in out.detail and "terminal prompt" in out.detail
    assert out.fix["cmd"] == "xcode-select --install" and "brew install python" in out.fix["text"]
    assert "apt" not in (out.fix["text"] + out.fix["cmd"]).lower()


def test_hook_helpers_python3_that_exits_nonzero_on_a_mac_with_the_tools_is_just_broken(helpers):
    helpers.put("python3", "exit 1\n")
    helpers.put("xcode-select", "echo /Library/Developer/CommandLineTools\nexit 0\n")
    helpers.mac()
    out = helpers.run()
    assert out.status == "warn" and "python3 does not run" in out.detail and "Command Line Tools" not in out.detail


def test_hook_helpers_stub_check_is_macos_only(helpers):
    helpers.put("python3", "exit 1\n")
    helpers.put("xcode-select", "exit 2\n")
    out = helpers.run()                                                     # IS_MACOS is False: Linux never asks xcode-select
    assert out.status == "warn" and "Command Line Tools" not in out.detail and "python3 does not run" in out.detail


def test_hook_helpers_missing_curl_fails_and_says_no_hook_reaches_the_board(helpers):
    (helpers.dir / "curl").unlink()
    out = helpers.run()
    assert out.status == "fail" and "curl is not found" in out.detail and "no hook can reach the board" in out.detail
    assert "terminal prompt" in out.detail and out.fix and "curl" in out.fix["text"]


def test_hook_helpers_both_missing_names_both_and_fails(helpers):
    (helpers.dir / "curl").unlink()
    (helpers.dir / "python3").unlink()
    out = helpers.run()
    assert out.status == "fail" and "curl is not found and python3 is not found" in out.detail


def test_hook_helpers_a_probe_that_times_out_reads_as_unknown(helpers):
    helpers.put("python3", "exec sleep 5\n")
    out = helpers.run()
    assert out.status == "warn" and out.detail.startswith("unknown:") and "python3 -c pass" in out.detail and "could not be judged" in out.detail


def test_hook_helpers_probes_run_with_the_3_second_limit_by_default():
    from app import doctor
    assert doctor.HELPER_TIMEOUT == 3.0 and doctor.LOGIN_PATH_TIMEOUT + doctor.HELPER_TIMEOUT < doctor.CHECK_TIMEOUT


def test_hook_helpers_finds_a_helper_only_the_login_shell_path_has(helpers, tmp_path, monkeypatch):
    """Under a service the board's PATH is short; a hook started from a login shell also sees the login shell's directories."""
    other = tmp_path / "login-bin"
    other.mkdir()
    (helpers.dir / "python3").rename(other / "python3")
    assert helpers.run().status == "warn"
    monkeypatch.setattr(helpers.doctor, "_login_path", lambda: str(other))
    assert helpers.run().status == "pass"


def test_hook_path_joins_the_board_path_and_the_login_path_once(helpers, monkeypatch):
    monkeypatch.setenv("PATH", os.pathsep.join(["/a", "/b"]))
    monkeypatch.setattr(helpers.doctor, "_login_path", lambda: os.pathsep.join(["/b", "/c"]))
    assert helpers.doctor._hook_path() == os.pathsep.join(["/a", "/b", "/c"])


def test_login_path_reads_the_last_line_and_survives_a_missing_shell(helpers, monkeypatch):
    d = helpers.doctor

    def run_ok(argv, timeout=3.0):
        assert argv[1] == "-lc" and timeout == d.LOGIN_PATH_TIMEOUT
        return d.Proc(0, "welcome banner\n/usr/bin:/bin", "")
    monkeypatch.setattr(d, "_run", run_ok)
    assert REAL_LOGIN_PATH() == "/usr/bin:/bin"
    monkeypatch.setattr(d, "_run", lambda argv, timeout=3.0: d.Proc(1, "/x", ""))
    assert REAL_LOGIN_PATH() == ""

    def gone(argv, timeout=3.0):
        raise d.ToolMissing("sh")
    monkeypatch.setattr(d, "_run", gone)
    assert REAL_LOGIN_PATH() == ""


# ---------------------------------------------------------------- install.sh refuses an APP_DIR with a space (#121)
INSTALL = ROOT / "install.sh"


def _guard_block() -> str:
    m = re.search(r"^for k in PROJECTS_DIR [^\n]*; do\n.*?^done\n", INSTALL.read_text(), re.S | re.M)
    assert m, "the whitespace and quote guard loop was not found in install.sh"
    return m.group(0)


def _run_guard(**values):
    script = ("set -euo pipefail\ndie() { printf 'error: %s\\n' \"$*\" >&2; exit 1; }\n"
              "PROJECTS_DIR=/srv/projects CCBOARD_DATA_DIR=/d CODE_SERVER_VERSION= CCBOARD_ALLOWED_USERS= CODEX_HOME= CCBOARD_PRICE_TABLE=\n"
              + "".join(f"{k}={shlex.quote(v)}\n" for k, v in values.items()) + _guard_block() + "echo accepted\n")
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)


def test_the_guard_covers_app_dir():
    assert re.search(r"^for k in PROJECTS_DIR [^\n]*\bAPP_DIR\b[^\n]*; do$", INSTALL.read_text(), re.M)


@pytest.mark.parametrize("path", ["/home/u/my app/ccboard", "/srv/a\tb", '/srv/a"b', "/srv/a$b", "/srv/a\\b"])
def test_install_refuses_an_app_dir_with_whitespace_quotes_dollar_or_backslash(path):
    r = _run_guard(APP_DIR=path)
    assert r.returncode == 1 and "accepted" not in r.stdout
    assert f"APP_DIR must not contain whitespace, quotes, $ or backslashes (got '{path}')" in r.stderr


def test_install_accepts_a_plain_app_dir_and_the_message_matches_the_other_guarded_paths():
    ok = _run_guard(APP_DIR="/home/u/ccboard")
    assert ok.returncode == 0 and "accepted" in ok.stdout
    other = _run_guard(APP_DIR="/home/u/ccboard", PROJECTS_DIR="/srv/my projects")
    assert other.returncode == 1 and "PROJECTS_DIR must not contain whitespace, quotes, $ or backslashes" in other.stderr


def test_the_guard_runs_before_the_hooks_are_registered():
    text = INSTALL.read_text()
    assert text.index("for k in PROJECTS_DIR") < text.index('python3 "$APP_DIR/scripts/claude_settings.py" install')
