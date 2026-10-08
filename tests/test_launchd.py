"""Issue #117/#122/#125/#128: the launchd job templates under launchd/ and scripts/launchd_render.py, the renderer the macOS installer calls.

Nothing here needs a Mac, launchctl or the real home: every template is rendered with sample values (a home with a space, an `&` and a `<`
in it) and parsed with plistlib; `plutil -lint` runs only on macOS. The Linux units under systemd/ are read, never changed.
"""
import ast
import importlib.util
import plistlib
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from app import platform as plat

ROOT = Path(__file__).resolve().parents[1]
LAUNCHD = ROOT / "launchd"
SYSTEMD = ROOT / "systemd"
SCRIPT = ROOT / "scripts" / "launchd_render.py"
spec = importlib.util.spec_from_file_location("launchd_render", SCRIPT)
lr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lr)

HOME = "/srv/mac home & co/a<b>"
APP = "/srv/app dir & 1"
VALUES = {
    "HOME": HOME, "APP_DIR": APP, "SHELL": "/bin/zsh", "CCBOARD_PORT": "8000", "TTYD_PORT": "7681", "ENV_FILE": HOME + "/.local/share/ccboard/env",
    "TMUX_BIN": "/opt/homebrew/bin/tmux", "TTYD_BIN": "/opt/homebrew/bin/ttyd", "CODE_SERVER_BIN": "/opt/homebrew/bin/code-server",
    "BACKUP_HOUR": "2", "BACKUP_MINUTE": "30",
}
JOBS = list(plat.LAUNCHD_JOBS)
SCHEDULED = ("backup",)                          # calendar jobs: run at a time, not kept alive, not started at load
REQUIRED = ["Label", "ProgramArguments", "WorkingDirectory", "EnvironmentVariables", "RunAtLoad", "KeepAlive", "ThrottleInterval",
            "StandardOutPath", "StandardErrorPath"]
REQUIRED_SCHEDULED = ["Label", "ProgramArguments", "WorkingDirectory", "EnvironmentVariables", "RunAtLoad", "StartCalendarInterval",
                      "StandardOutPath", "StandardErrorPath"]


def template(job: str) -> Path:
    return LAUNCHD / f"{plat.launchd_label(job)}.plist.in"


def render(job: str, **over) -> dict:
    vals = dict(VALUES)
    vals.update(over)
    text = lr.render(template(job).read_text(encoding="utf-8"), vals)
    return plistlib.loads(text.encode("utf-8"))


def exec_start(unit: str) -> list[str]:
    line = next(ln for ln in (SYSTEMD / unit).read_text().splitlines() if ln.startswith("ExecStart="))
    return shlex.split(line[len("ExecStart="):])


# ---------------------------------------------------------------- the templates


def test_one_template_per_job_and_the_label_is_the_platform_label():
    assert sorted(p.name for p in LAUNCHD.glob("*.plist.in")) == sorted(f"{plat.launchd_label(j)}.plist.in" for j in JOBS)
    for job in JOBS:
        assert render(job)["Label"] == plat.launchd_label(job) == f"dev.ccboard.{job}"


@pytest.mark.parametrize("job", JOBS)
def test_every_job_has_the_required_keys_and_absolute_arguments(job):
    p = render(job)
    for key in (REQUIRED_SCHEDULED if job in SCHEDULED else REQUIRED):
        assert key in p, f"{job}: {key} missing"
    args = p["ProgramArguments"]
    assert isinstance(args, list) and args and all(isinstance(a, str) and a for a in args)
    assert args[0].startswith("/"), "the program is an absolute path (a job has no useful PATH to find it on)"
    assert p["WorkingDirectory"].startswith("/")
    if job in SCHEDULED:
        assert p["RunAtLoad"] is False and "KeepAlive" not in p, "a calendar job runs at its time, not at load and not forever"
    else:
        assert p["RunAtLoad"] is True
        assert p["KeepAlive"] in (True, {"SuccessfulExit": False})
        assert isinstance(p["ThrottleInterval"], int) and p["ThrottleInterval"] >= 2
    env = p["EnvironmentVariables"]
    assert env["HOME"] == HOME, "an explicit HOME (launchd's own is not the user's in every session), and a path with a space, & and < survives"
    assert env["LANG"] and env["PATH"]


@pytest.mark.parametrize("job", JOBS)
def test_no_tilde_and_no_token_left_and_logs_are_absolute_under_library_logs(job):
    text = lr.render(template(job).read_text(encoding="utf-8"), VALUES)
    assert "~" not in text, "launchd does not expand ~"
    assert not lr.TOKEN_RE.search(text), "a token without a value never reaches the plist"
    p = plistlib.loads(text.encode("utf-8"))
    log = f"{HOME}/Library/Logs/ccboard/{job}.log"
    assert p["StandardOutPath"] == log and p["StandardErrorPath"] == log, "stdout and stderr share one log per job"
    assert p["WorkingDirectory"] == (APP if job in ("board", "backup") else HOME)
    for k, v in p["EnvironmentVariables"].items():
        assert "~" not in v, k


@pytest.mark.parametrize("job", JOBS)
def test_path_is_expanded_and_holds_the_required_directories(job):
    path = render(job)["EnvironmentVariables"]["PATH"].split(":")
    for want in (f"{HOME}/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        assert want in path, (job, want)
    assert all(d.startswith("/") for d in path)
    assert len(path) == len(set(path)), "no duplicate directory"


def test_brew_prefix_joins_the_path_without_a_duplicate():
    intel = render("board", BREW_PREFIX="/usr/local")["EnvironmentVariables"]["PATH"].split(":")
    assert intel.count("/usr/local/bin") == 1
    odd = render("board", BREW_PREFIX="/srv/brew")["EnvironmentVariables"]["PATH"].split(":")
    assert odd[1] == "/srv/brew/bin" and odd[0] == f"{HOME}/.local/bin"


def test_an_explicit_path_wins_over_the_default():
    assert render("board", PATH="/a:/b")["EnvironmentVariables"]["PATH"] == "/a:/b"


# ---------------------------------------------------------------- per job


def test_board_runs_the_systemd_command_and_reads_its_settings_from_the_env_file():
    p = render("board")
    unit = exec_start("ccboard.service.in")
    unit = [{"__APP_DIR__/.venv/bin/uvicorn": f"{APP}/.venv/bin/uvicorn", "${CCBOARD_PORT}": "8000"}.get(a, a) for a in unit]
    assert p["ProgramArguments"] == unit, "the board's argv is the Linux unit's ExecStart with the port and the checkout filled in"
    env = p["EnvironmentVariables"]
    assert env["CCBOARD_RUNTIME"] == "launchd" and env["CCBOARD_ENV_FILE"] == VALUES["ENV_FILE"]
    assert env["SHELL"] == "/bin/zsh"
    assert "CCBOARD_PORT" not in env and "TTYD_PORT" not in env, "ports stay in the env file; an explicit variable would beat it"


def test_tmux_runs_in_the_foreground_with_the_systemd_socket_and_config():
    p = render("tmux")
    unit = exec_start("ccboard-tmux.service.in")           # tmux -L ccboard -f <app>/tmux.conf start-server
    assert unit[-1] == "start-server"
    want = ["/opt/homebrew/bin/tmux"] + [a.replace("__APP_DIR__", APP) for a in unit[1:-1]] + ["-D"]
    assert p["ProgramArguments"] == want
    assert p["ProgramArguments"][-1] == "-D", "tmux.1: -D keeps the server in the foreground and allows no command"
    assert p["KeepAlive"] is True
    env = p["EnvironmentVariables"]
    assert not [k for k in env if k.startswith("CCBOARD_")], "the tmux server's environment is every pane's: no board setting goes into it"
    assert "AbandonProcessGroup" not in p, "with -D the server does not fork"


def test_ttyd_runs_the_systemd_command_on_the_mac_loopback_interface():
    p = render("ttyd")
    unit = exec_start("ccboard-ttyd.service.in")
    unit = [{"__TTYD_BIN__": "/opt/homebrew/bin/ttyd", "lo": "lo0", "${TTYD_PORT}": "7681", "__APP_BIN__/ccboard-attach": f"{APP}/bin/ccboard-attach"}.get(a, a)
            for a in unit]
    assert p["ProgramArguments"] == unit, "the same flags as the Linux unit; only the interface name differs (lo0)"
    assert p["ProgramArguments"][1:3] == ["-i", "lo0"]
    assert render("ttyd", TTYD_IFACE="127.0.0.1")["ProgramArguments"][2] == "127.0.0.1", "the interface can be switched without editing the template"
    assert p["EnvironmentVariables"]["CCBOARD_TMUX_SOCKET"] == "ccboard", "bin/ccboard-attach reads the socket name from here"


def test_awake_is_caffeinate_idle_and_ac_power():
    p = render("awake")
    assert p["ProgramArguments"] == ["/usr/bin/caffeinate", "-i", "-s"]
    assert set(p["EnvironmentVariables"]) == {"HOME", "LANG", "PATH"}


def test_code_server_job_has_no_arguments_and_the_login_shell():
    p = render("code-server")
    assert p["ProgramArguments"] == ["/opt/homebrew/bin/code-server"], "bind address and auth come from the managed config.yaml"
    assert p["EnvironmentVariables"]["SHELL"] == "/bin/zsh"
    assert "Nice" not in p and "ProcessType" not in p and "LowPriorityIO" not in p, "no priority tuning on macOS"
    assert not [k for k in p["EnvironmentVariables"] if k.startswith("CCBOARD_")]


def test_mem_job_environment_is_exactly_home_lang_path_and_never_a_session_marker():
    p = render("mem")
    env = p["EnvironmentVariables"]
    assert set(env) == {"HOME", "LANG", "PATH"}, "launchd starts the worker with only these names: that is the clean environment"
    assert not [k for k in env if k.startswith(("CCBOARD_", "CLAUDE")) or k in ("TMUX", "TMUX_PANE")]
    assert p["ProgramArguments"] == [f"{APP}/bin/ccboard-mem-run"], "APP_BIN defaults to <app dir>/bin"
    assert render("mem", APP_BIN="/srv/data/app/bin")["ProgramArguments"] == ["/srv/data/app/bin/ccboard-mem-run"]
    path = env["PATH"].split(":")
    assert path[:2] == [f"{HOME}/.bun/bin", f"{HOME}/.local/bin"]
    for want in ("/opt/homebrew/bin", "/usr/local/bin"):
        assert want in path
    assert p["KeepAlive"] == {"SuccessfulExit": False}, "a worker that exits 0 stays stopped; exit 78 (plugin or bun missing) is retried"
    assert p["ThrottleInterval"] >= 30, "a worker that cannot boot must not spin"


def test_the_mem_template_never_grows_a_session_key():
    """A key added to the template's EnvironmentVariables by hand fails here, whatever its value."""
    for key in plistlib.loads(lr.render(template("mem").read_text(encoding="utf-8"), VALUES).encode())["EnvironmentVariables"]:
        assert key in ("HOME", "LANG", "PATH"), key


# ---------------------------------------------------------------- the backup job (issue #129)


def test_backup_runs_the_systemd_command_at_the_given_time_and_not_at_load():
    p = render("backup")
    assert p["Label"] == "dev.ccboard.backup" and p["ProgramArguments"] == [f"{APP}/.venv/bin/python", "-m", "app.backup"]
    assert 'BACKUP_EXEC="$APP_DIR/.venv/bin/python -m app.backup"' in (ROOT / "install.sh").read_text(), "the Linux unit runs the same command"
    assert p["WorkingDirectory"] == APP
    assert p["StartCalendarInterval"] == {"Hour": 2, "Minute": 30}
    assert render("backup", BACKUP_HOUR="0", BACKUP_MINUTE="0")["StartCalendarInterval"] == {"Hour": 0, "Minute": 0}
    assert render("backup", BACKUP_HOUR="23", BACKUP_MINUTE="59")["StartCalendarInterval"] == {"Hour": 23, "Minute": 59}
    assert p["RunAtLoad"] is False and "KeepAlive" not in p and "ThrottleInterval" not in p


def test_backup_mirrors_the_systemd_priority_and_never_waits_for_a_credential():
    p = render("backup")
    unit = (SYSTEMD / "ccboard-backup.service.in").read_text()
    assert "Nice=10" in unit and "IOSchedulingClass=idle" in unit
    assert p["Nice"] == 10 and p["LowPriorityIO"] is True and p["ProcessType"] == "Background"
    env = p["EnvironmentVariables"]
    assert set(env) == {"HOME", "LANG", "PATH", "GIT_TERMINAL_PROMPT", "CCBOARD_ENV_FILE"}
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["CCBOARD_ENV_FILE"] == VALUES["ENV_FILE"]
    assert "/opt/homebrew/bin" in env["PATH"].split(":") and f"{HOME}/.local/bin" in env["PATH"].split(":"), "restic, git and gh are Homebrew tools"
    assert "~" not in lr.render(template("backup").read_text(encoding="utf-8"), VALUES)


def test_backup_template_takes_only_the_agreed_tokens():
    assert set(lr.tokens(template("backup").read_text(encoding="utf-8"))) == {
        "APP_DIR", "HOME", "PATH", "LOG_DIR", "ENV_FILE", "BACKUP_HOUR", "BACKUP_MINUTE"}


@pytest.mark.parametrize("name,value", [("BACKUP_HOUR", "24"), ("BACKUP_HOUR", "-1"), ("BACKUP_HOUR", "02"), ("BACKUP_HOUR", "x"),
                                        ("BACKUP_HOUR", "2.5"), ("BACKUP_MINUTE", "60"), ("BACKUP_MINUTE", "05"), ("BACKUP_MINUTE", " 5"),
                                        ("BACKUP_MINUTE", ""), ("BACKUP_HOUR", "\u0662")])
def test_backup_hour_and_minute_must_be_whole_numbers_in_range(name, value):
    with pytest.raises(lr.RenderError, match=name):
        render("backup", **{name: value})


def test_backup_without_a_time_is_an_error_and_the_cli_renders_it(tmp_path):
    text = template("backup").read_text(encoding="utf-8")
    with pytest.raises(lr.RenderError, match="BACKUP_HOUR"):
        lr.render(text, {k: v for k, v in VALUES.items() if k != "BACKUP_HOUR"})
    out = tmp_path / "dev.ccboard.backup.plist"
    r = cli("--template", str(template("backup")), "--out", str(out), *_sets(BACKUP_HOUR="3", BACKUP_MINUTE="5"))
    assert r.returncode == 0 and plistlib.loads(out.read_bytes())["StartCalendarInterval"] == {"Hour": 3, "Minute": 5}
    r = cli("--template", str(template("backup")), "--out", str(out), *_sets(BACKUP_HOUR="25"))
    assert r.returncode == 2 and "BACKUP_HOUR" in r.stderr and r.stderr.count("\n") == 1


# ---------------------------------------------------------------- one TMUX_TMPDIR (issue #125)


def test_board_tmux_and_ttyd_carry_the_same_tmux_tmpdir_and_socket():
    for tmp in ("/tmp", "/srv/tmux dir"):
        vals = {j: render(j, TMUX_TMPDIR=tmp)["EnvironmentVariables"] for j in ("board", "tmux", "ttyd")}
        assert {v["TMUX_TMPDIR"] for v in vals.values()} == {tmp}
    assert render("board")["EnvironmentVariables"]["TMUX_TMPDIR"] == "/tmp", "the default is /tmp, as the systemd units pin it"
    sock = {render(j, TMUX_SOCKET="ccboard-x")["EnvironmentVariables"].get("CCBOARD_TMUX_SOCKET") for j in ("board", "ttyd")}
    assert sock == {"ccboard-x"}
    assert render("tmux", TMUX_SOCKET="ccboard-x")["ProgramArguments"][1:3] == ["-L", "ccboard-x"]


def test_a_tmux_tmpdir_that_makes_the_socket_path_over_100_bytes_is_refused():
    fixed = len("/tmux-501/ccboard")
    ok_dir = "/" + "d" * (100 - fixed - 1)
    assert lr.socket_path_length(ok_dir, 501, "ccboard") == 100
    assert render("tmux", TMUX_TMPDIR=ok_dir, UID="501")["EnvironmentVariables"]["TMUX_TMPDIR"] == ok_dir, "exactly 100 bytes is allowed"
    with pytest.raises(lr.RenderError, match=r"101 bytes.*100 byte limit"):
        render("tmux", TMUX_TMPDIR=ok_dir + "d", UID="501")
    with pytest.raises(lr.RenderError, match="100 byte"):
        render("board", TMUX_TMPDIR="/" + "d" * 120, UID="501")


def test_the_socket_length_counts_bytes_not_characters():
    n = lr.socket_path_length("/ü", 501, "ccboard")
    assert n == len("/ü/tmux-501/ccboard".encode("utf-8")) == len("/ü/tmux-501/ccboard") + 1


def test_the_socket_directory_is_resolved_the_way_tmux_resolves_it(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert lr.socket_path_length(str(link), 501, "ccboard") == lr.socket_path_length(str(real), 501, "ccboard")


# ---------------------------------------------------------------- refusals


def test_a_missing_or_bad_value_is_an_error_not_a_broken_plist():
    text = template("board").read_text(encoding="utf-8")
    for drop in ("HOME", "APP_DIR", "SHELL", "CCBOARD_PORT", "ENV_FILE"):
        vals = {k: v for k, v in VALUES.items() if k != drop}
        with pytest.raises(lr.RenderError, match=drop):
            lr.render(text, vals)
    bad = [("HOME", "~/x", "~"), ("HOME", "relative/home", "absolute"), ("CCBOARD_PORT", "80a", "port"), ("CCBOARD_PORT", "70000", "port"),
           ("SHELL", "zsh", "absolute"), ("HOME", "/a\nb", "control"), ("PATH", "/a:~/bin", "PATH"), ("ENV_FILE", "", "empty"),
           ("TMUX_SOCKET", "a/b", "socket name")]
    for name, value, why in bad:
        with pytest.raises(lr.RenderError, match=why):
            lr.render(text, {**VALUES, name: value})


def test_values_are_xml_escaped_so_markup_in_a_path_cannot_break_out():
    text = lr.render(template("board").read_text(encoding="utf-8"), {**VALUES, "HOME": "/h/</string><key>X</key>&amp;"})
    p = plistlib.loads(text.encode("utf-8"))
    assert p["EnvironmentVariables"]["HOME"] == "/h/</string><key>X</key>&amp;" and "X" not in p["EnvironmentVariables"]


def test_a_session_type_adds_the_experimental_background_key():
    text = template("board").read_text(encoding="utf-8")
    assert "LimitLoadToSessionType" not in plistlib.loads(lr.render(text, VALUES).encode())
    assert plistlib.loads(lr.render(text, VALUES, session_type="Background").encode())["LimitLoadToSessionType"] == "Background"
    with pytest.raises(lr.RenderError):
        lr.render(text, VALUES, session_type="Nope")


# ---------------------------------------------------------------- the command line the installer calls


def cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd=cwd)


def _sets(**over):
    vals = {**VALUES, **over}
    return [a for k, v in vals.items() for a in ("--set", f"{k}={v}")]


def test_cli_writes_a_parseable_0644_plist_and_replaces_atomically(tmp_path):
    (tmp_path / "agents").mkdir()
    out = tmp_path / "agents" / "dev.ccboard.board.plist"
    out.write_text("old")
    r = cli("--template", str(template("board")), "--out", str(out), *_sets())
    assert r.returncode == 0 and r.stderr == "", r.stderr
    assert plistlib.loads(out.read_bytes())["Label"] == "dev.ccboard.board"
    assert stat.S_IMODE(out.stat().st_mode) == 0o644
    assert [p.name for p in out.parent.iterdir()] == ["dev.ccboard.board.plist"], "no temp file is left behind"


def test_cli_refuses_with_exit_2_one_line_and_leaves_the_target_alone(tmp_path):
    out = tmp_path / "x.plist"
    out.write_text("keep")
    r = cli("--template", str(template("tmux")), "--out", str(out), *_sets(TMUX_TMPDIR="/" + "d" * 120))
    assert r.returncode == 2 and r.stderr.startswith("launchd_render: ") and r.stderr.count("\n") == 1 and "100 byte" in r.stderr
    assert out.read_text() == "keep"
    r = cli("--template", str(template("board")), "--out", str(out), "--set", "HOME=/h")
    assert r.returncode == 2 and "APP_DIR" in r.stderr
    r = cli("--template", str(template("board")), "--out", str(out), "--set", "home=/h")
    assert r.returncode == 2 and "NAME=VALUE" in r.stderr
    assert out.read_text() == "keep"


def test_cli_prints_to_stdout_and_lists_tokens():
    r = cli("--template", str(template("ttyd")), "--out", "-", *_sets())
    assert r.returncode == 0 and plistlib.loads(r.stdout.encode())["Label"] == "dev.ccboard.ttyd"
    r = cli("--template", str(template("mem")), "--list-tokens")
    assert r.returncode == 0 and r.stdout.split() == ["APP_BIN", "HOME", "PATH", "LOG_DIR"]


def test_cli_session_type_flag(tmp_path):
    out = tmp_path / "b.plist"
    r = cli("--template", str(template("tmux")), "--out", str(out), "--session-type", "Background", *_sets())
    assert r.returncode == 0 and plistlib.loads(out.read_bytes())["LimitLoadToSessionType"] == "Background"


def test_the_renderer_and_the_helper_run_on_the_system_python_of_a_mac():
    """macOS 13 and 14 ship python3 3.9 with the Command Line Tools: no syntax newer than 3.9, and standard library imports only."""
    for name in ("launchd_render.py", "macos_tools.py"):
        src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        tree = ast.parse(src, feature_version=(3, 9))
        assert "from __future__ import annotations" in src
        mods = {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | \
               {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert mods <= set(sys.stdlib_module_names), mods - set(sys.stdlib_module_names)


@pytest.mark.needs_macos
@pytest.mark.parametrize("job", JOBS)
def test_plutil_lints_every_rendered_plist(job, tmp_path):
    out = tmp_path / f"{plat.launchd_label(job)}.plist"
    lr.render_file(str(template(job)), str(out), VALUES)
    r = subprocess.run(["plutil", "-lint", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
