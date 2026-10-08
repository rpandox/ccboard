"""The board's side of the macOS port (issue #117): the settings file a launchd job reads, resolve_bin, case-colliding names and the macOS
doctor checks. Everything runs on Linux too: fake launchctl, fdesetup, defaults, pmset and login shells are scripts on a temp PATH, the
home is a temp directory (conftest), and nothing here touches the real launchd, Tailscale, tmux socket or ~/Library."""
import logging
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import config, doctor, doctor_macos as dm, projects
from app import platform as plat
from app.config import ENV_FILE_KEYS, Settings, settings

ROOT = Path(__file__).resolve().parent.parent
REAL_LOGIN_LOOKUP = plat._login_shell_lookup      # conftest swaps the module attribute for None in every test; these tests put it back when they need it


# ------------------------------------------------------------------ the settings file

def write_env(tmp_path, text, mode=0o600, name="env"):
    f = tmp_path / name
    f.write_text(text)
    f.chmod(mode)
    return f


def test_env_file_fills_what_the_environment_leaves_open(tmp_path):
    f = write_env(tmp_path, "CCBOARD_PORT=9100\nTTYD_PORT=7700\nCCBOARD_ALLOWED_USERS=a@example.com\n")
    s = Settings({"CCBOARD_ENV_FILE": str(f)})
    assert (s.port, s.ttyd_port, s.allowed_users) == (9100, 7700, {"a@example.com"})


def test_explicit_environment_wins_over_the_file(tmp_path):
    f = write_env(tmp_path, "CCBOARD_PORT=9100\nTTYD_PORT=7700\n")
    s = Settings({"CCBOARD_ENV_FILE": str(f), "CCBOARD_PORT": "9200"})
    assert (s.port, s.ttyd_port) == (9200, 7700)


def test_an_empty_environment_value_does_not_beat_the_file(tmp_path):
    """install.sh: only a non-empty caller value wins over the remembered one."""
    f = write_env(tmp_path, "CCBOARD_PORT=9100\n")
    assert Settings({"CCBOARD_ENV_FILE": str(f), "CCBOARD_PORT": ""}).port == 9100


def test_an_empty_value_in_the_file_reads_as_unset(tmp_path):
    f = write_env(tmp_path, "CCBOARD_PORT=\nPROJECTS_DIR=\nNTFY_URL=\n")
    s = Settings({"CCBOARD_ENV_FILE": str(f)})
    assert s.port == 8000 and s.projects_dir == plat.default_projects_dir() and s.ntfy_url == ""


def test_lines_are_data_never_shell(tmp_path):
    marker = tmp_path / "pwned"
    body = (f"PROJECTS_DIR=$(touch {marker})\n"
            f"CCBOARD_PUBLIC_URL=`touch {marker}`; touch {marker}\n"
            f"CCBOARD_NODE_NAME=a b $HOME \"q\" 'r'\n")
    f = write_env(tmp_path, body)
    s = Settings({"CCBOARD_ENV_FILE": str(f)})
    assert not marker.exists()
    assert s.projects_dir == Path(f"$(touch {marker})")             # the text itself, nothing expanded or run
    assert s.public_url == f"`touch {marker}`; touch {marker}"
    assert s.node_name == "a b $HOME \"q\" 'r'"                     # quotes are part of the value, as install.sh treats them


def test_lines_split_at_the_first_equals_and_comments_are_skipped(tmp_path):
    f = write_env(tmp_path, "# CCBOARD_PORT=1\n\nCCBOARD_NODES=a=b=c\nnot a line\nCCBOARD_PORT\n  CCBOARD_TTYD=1\n")
    got = config.read_env_file(f)
    assert got == {"CCBOARD_NODES": "a=b=c"}


def test_unknown_keys_are_ignored(tmp_path):
    f = write_env(tmp_path, "PATH=/evil\nHOME=/evil\nAWS_SECRET_ACCESS_KEY=x\nCCBOARD_ENV_FILE=/evil\nCCBOARD_DEV_BYPASS_USER=root\nCCBOARD_PORT=9100\n")
    env = {"CCBOARD_ENV_FILE": str(f)}
    merged = config.apply_env_file(env)
    assert set(merged) == {"CCBOARD_ENV_FILE", "CCBOARD_PORT"}
    assert Settings(env).dev_bypass_user is None


def test_the_file_must_be_private(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="ccboard")
    for mode in (0o644, 0o640, 0o660, 0o604):
        f = write_env(tmp_path, "CCBOARD_PORT=9100\n", mode=mode)
        assert Settings({"CCBOARD_ENV_FILE": str(f)}).port == 8000, oct(mode)
        assert re.search(r"readable by group or others \(mode %04o\)" % mode, caplog.text)
        assert "chmod 600" in caplog.text
        caplog.clear()
    for mode in (0o600, 0o400):
        f = write_env(tmp_path, "CCBOARD_PORT=9100\n", mode=mode)
        assert Settings({"CCBOARD_ENV_FILE": str(f)}).port == 9100, oct(mode)


def test_the_file_must_be_mine(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="ccboard")
    f = write_env(tmp_path, "CCBOARD_PORT=9100\n")
    monkeypatch.setattr(plat, "current_uid", lambda: os.stat(f).st_uid + 1)
    assert Settings({"CCBOARD_ENV_FILE": str(f)}).port == 8000
    assert "not owned by the user the board runs as" in caplog.text


def test_without_a_uid_the_owner_and_mode_checks_are_skipped(tmp_path, monkeypatch):
    f = write_env(tmp_path, "CCBOARD_PORT=9100\n", mode=0o644)
    monkeypatch.setattr(plat, "current_uid", lambda: None)
    assert Settings({"CCBOARD_ENV_FILE": str(f)}).port == 9100


def test_a_symlink_or_a_directory_is_refused(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="ccboard")
    real = write_env(tmp_path, "CCBOARD_PORT=9100\n", name="real")
    link = tmp_path / "link"
    link.symlink_to(real)
    assert Settings({"CCBOARD_ENV_FILE": str(link)}).port == 8000 and "symbolic link" in caplog.text
    caplog.clear()
    assert Settings({"CCBOARD_ENV_FILE": str(tmp_path)}).port == 8000 and "not a regular file" in caplog.text


def test_a_named_file_that_is_missing_is_logged_the_default_one_is_not(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="ccboard")
    Settings({"CCBOARD_ENV_FILE": str(tmp_path / "nope")})
    assert "does not exist" in caplog.text
    caplog.clear()
    monkeypatch.setattr(os, "environ", {"CCBOARD_DATA_DIR": str(tmp_path)})         # no CCBOARD_ENV_FILE: <data dir>/env, absent
    Settings()
    assert caplog.text == ""


def test_the_process_environment_reads_the_data_dir_file_and_exports_it(tmp_path, monkeypatch):
    write_env(tmp_path, "CCBOARD_PORT=9100\nCCBOARD_HUB_TOKEN=tok\nCCBOARD_RUNTIME=launchd\n")
    fake = {"CCBOARD_DATA_DIR": str(tmp_path), "CCBOARD_HUB_TOKEN": "from-env"}
    monkeypatch.setattr(os, "environ", fake)
    s = Settings()
    assert s.port == 9100 and s.runtime == "launchd" and s.hub_token == "from-env"
    assert fake["CCBOARD_PORT"] == "9100" and fake["CCBOARD_HUB_TOKEN"] == "from-env"      # exported for modules that read os.environ; the environment still wins


def test_CCBOARD_ENV_FILE_names_the_file_and_empty_switches_it_off(tmp_path, monkeypatch):
    other = write_env(tmp_path, "CCBOARD_PORT=9100\n", name="elsewhere")
    write_env(tmp_path, "CCBOARD_PORT=9300\n")
    monkeypatch.setattr(os, "environ", {"CCBOARD_DATA_DIR": str(tmp_path), "CCBOARD_ENV_FILE": str(other)})
    assert Settings().port == 9100
    monkeypatch.setattr(os, "environ", {"CCBOARD_DATA_DIR": str(tmp_path), "CCBOARD_ENV_FILE": ""})
    assert Settings().port == 8000


def test_a_mapping_a_test_passes_never_reads_the_default_file(tmp_path):
    write_env(tmp_path, "CCBOARD_PORT=9300\n")
    env = {"CCBOARD_DATA_DIR": str(tmp_path)}
    assert config.apply_env_file(env) is env
    assert Settings(env).port == 8000


def test_no_file_changes_nothing(tmp_path, monkeypatch):
    """Linux, and every system without the file: the environment and every setting are exactly what they were."""
    env = {"CCBOARD_DATA_DIR": str(tmp_path / "data"), "CCBOARD_PORT": "8123", "PATH": "/usr/bin"}
    monkeypatch.setattr(os, "environ", dict(env))
    before = dict(os.environ)
    s = Settings()
    assert os.environ == before == env
    assert (s.port, s.runtime, s.data_dir) == (8123, "host", tmp_path / "data")
    assert config.apply_env_file(os.environ) is os.environ


def test_the_whitelist_is_install_shs_list():
    text = (ROOT / "install.sh").read_text()
    keys = re.search(r"^ENV_KEYS=\((.*?)\)$", text, re.M).group(1).split()
    assert list(ENV_FILE_KEYS) == keys
    assert len(set(ENV_FILE_KEYS)) == len(ENV_FILE_KEYS)


def test_the_command_line_helpers(tmp_path):
    env = {**os.environ, "CCBOARD_ENV_FILE": ""}
    run = lambda *a: subprocess.run([sys.executable, "-m", "app.config", *a], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    keys = run("keys")
    assert keys.returncode == 0 and keys.stdout.split() == list(ENV_FILE_KEYS)
    good, bad = write_env(tmp_path, "A=1\n", name="good"), write_env(tmp_path, "A=1\n", mode=0o644, name="bad")
    assert run("check", str(good)).returncode == 0
    assert run("check", str(tmp_path / "absent")).returncode == 0
    refused = run("check", str(bad))
    assert refused.returncode == 1 and "readable by group or others" in refused.stdout
    assert run("nonsense").returncode == 2


def test_the_launchd_runtime_from_the_file_still_refuses_the_dev_bypass(tmp_path):
    f = write_env(tmp_path, "CCBOARD_RUNTIME=launchd\n")
    s = Settings({"CCBOARD_ENV_FILE": str(f), "CCBOARD_DEV_BYPASS_USER": "x"})
    assert s.runtime == "launchd" and s.dev_bypass_user is None


# ------------------------------------------------------------------ resolve_bin

def make_exe(path, body="#!/bin/sh\nexit 0\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def fake_shell(tmp_path, output, name="fakeshell", sleep=0):
    """A login shell that logs its arguments, prints a banner, then `output`."""
    log = tmp_path / f"{name}.log"
    body = f'#!/bin/sh\necho "$@" >> "{log}"\n{"sleep %s" % sleep if sleep else ":"}\necho "welcome to my profile"\n{output}\n'
    return make_exe(tmp_path / name, body), log


@pytest.fixture
def bare(tmp_path, monkeypatch):
    """A PATH with nothing on it, Homebrew folders and the login-shell lookup out of play: resolve_bin sees only what a test puts down."""
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(plat, "BREW_BIN_DIRS", ())
    monkeypatch.setattr(plat, "IS_LINUX", False)       # the login-shell step runs off Linux only; these tests model a Mac on every runner
    plat._login_lookups.clear()
    return empty


def test_linux_never_asks_the_login_shell(tmp_path, bare, monkeypatch):
    monkeypatch.setattr(plat, "IS_LINUX", True)
    monkeypatch.setattr(plat, "_login_shell_lookup", lambda name: pytest.fail("a login shell was started on Linux"))
    assert plat.resolve_bin("claude") is None


def test_resolve_bin_prefers_path_then_local_bin_and_starts_no_shell(tmp_path, bare, monkeypatch):
    monkeypatch.setattr(plat, "_login_shell_lookup", lambda name: pytest.fail("a shell was started although PATH or ~/.local/bin had it"))
    local = make_exe(Path.home() / ".local" / "bin" / "claude")
    assert plat.resolve_bin("claude") == str(local)
    onpath = make_exe(tmp_path / "pathbin" / "claude")
    monkeypatch.setenv("PATH", f"{onpath.parent}{os.pathsep}{bare}")
    assert plat.resolve_bin("claude") == str(onpath)


def test_resolve_bin_asks_the_login_shell_after_path_and_local_bin(tmp_path, bare, monkeypatch):
    nvm = make_exe(tmp_path / "nvm" / "bin" / "claude")
    sh, log = fake_shell(tmp_path, f"echo {nvm}")
    monkeypatch.setattr(plat, "_login_shell_lookup", REAL_LOGIN_LOOKUP)
    monkeypatch.setattr(plat, "login_shell", lambda: str(sh))
    assert plat.resolve_bin("claude") == str(nvm)
    assert log.read_text().strip() == "-lic command -v claude"             # a login, interactive shell running `command -v`
    assert plat.resolve_bin("claude") == str(nvm) and len(log.read_text().splitlines()) == 1     # cached


def test_resolve_bin_falls_back_to_the_known_folders_after_the_login_shell(tmp_path, bare, monkeypatch):
    brew = tmp_path / "brewbin"
    monkeypatch.setattr(plat, "BREW_BIN_DIRS", (str(tmp_path / "nothere"), str(brew)))
    make_exe(brew / "gh")
    sh, _log = fake_shell(tmp_path, "echo gh: not found")
    monkeypatch.setattr(plat, "_login_shell_lookup", REAL_LOGIN_LOOKUP)
    monkeypatch.setattr(plat, "login_shell", lambda: str(sh))
    assert plat.resolve_bin("gh") == str(brew / "gh")
    assert plat.resolve_bin("nothing-like-it") is None
    # and the login shell's answer beats the folder
    nvm = make_exe(tmp_path / "nvm" / "gh")
    sh2, _ = fake_shell(tmp_path, f"echo {nvm}", name="shell2")
    plat._login_lookups.clear()
    monkeypatch.setattr(plat, "login_shell", lambda: str(sh2))
    assert plat.resolve_bin("gh") == str(nvm)


def test_the_login_shell_lookup_only_trusts_an_absolute_executable(tmp_path, bare, monkeypatch):
    monkeypatch.setattr(plat, "_login_shell_lookup", REAL_LOGIN_LOOKUP)
    for output in ("echo 'claude: aliased to /x/claude'", "echo claude", "echo ./claude", f"echo {tmp_path}/not-there", f"echo {tmp_path}", ""):
        sh, _log = fake_shell(tmp_path, output)
        monkeypatch.setattr(plat, "login_shell", lambda sh=sh: str(sh))
        plat._login_lookups.clear()
        assert plat.resolve_bin("claude") is None, output


def test_a_slow_or_broken_login_shell_gives_none(tmp_path, bare, monkeypatch):
    monkeypatch.setattr(plat, "_login_shell_lookup", REAL_LOGIN_LOOKUP)
    monkeypatch.setattr(plat, "LOGIN_SHELL_TIMEOUT", 0.3)
    slow, _ = fake_shell(tmp_path, "echo /x", name="slow", sleep=3)
    monkeypatch.setattr(plat, "login_shell", lambda: str(slow))
    t0 = time.monotonic()
    assert plat.resolve_bin("claude") is None and time.monotonic() - t0 < 2.5
    for shell in (str(tmp_path / "missing-shell"), "relative-shell", ""):
        plat._login_lookups.clear()
        monkeypatch.setattr(plat, "login_shell", lambda shell=shell: shell)
        assert plat.resolve_bin("claude") is None


def test_a_miss_is_remembered_for_a_while_and_then_asked_again(tmp_path, bare, monkeypatch):
    nvm = tmp_path / "nvm" / "claude"
    sh, log = fake_shell(tmp_path, f"echo {nvm}")
    monkeypatch.setattr(plat, "_login_shell_lookup", REAL_LOGIN_LOOKUP)
    monkeypatch.setattr(plat, "login_shell", lambda: str(sh))
    assert plat.resolve_bin("claude") is None                       # not installed yet
    make_exe(nvm)
    assert plat.resolve_bin("claude") is None and len(log.read_text().splitlines()) == 1
    monkeypatch.setattr(plat, "LOGIN_LOOKUP_MISS_TTL", 0.0)
    assert plat.resolve_bin("claude") == str(nvm) and len(log.read_text().splitlines()) == 2


def test_a_cached_path_that_vanished_is_looked_up_again(tmp_path, bare, monkeypatch):
    a, b = make_exe(tmp_path / "a" / "claude"), make_exe(tmp_path / "b" / "claude")
    sh, _log = fake_shell(tmp_path, f'[ -x "{a}" ] && echo "{a}" || echo "{b}"')
    monkeypatch.setattr(plat, "_login_shell_lookup", REAL_LOGIN_LOOKUP)
    monkeypatch.setattr(plat, "login_shell", lambda: str(sh))
    assert plat.resolve_bin("claude") == str(a)
    a.unlink()
    assert plat.resolve_bin("claude") == str(b)


def test_only_plain_program_names_reach_the_shell(tmp_path, bare, monkeypatch):
    monkeypatch.setattr(plat, "_login_shell_lookup", lambda name: pytest.fail("shell started for " + repr(name)))
    for bad in ("a;touch x", "$(id)", "../claude", "a b", "", "-lic"):
        assert plat.resolve_bin(bad) is None


def old_bin(name):
    """The lookup claude_bin and codex_bin had before resolve_bin (kept here to compare against)."""
    import shutil
    found = shutil.which(name)
    if found:
        return found
    local = Path.home() / ".local" / "bin" / name
    return str(local) if local.exists() else None


@pytest.mark.parametrize("which", ["claude", "codex"])
def test_claude_and_codex_bin_answer_as_before(tmp_path, bare, monkeypatch, which):
    monkeypatch.setattr(plat, "_login_shell_lookup", lambda name: pytest.fail("the login shell was asked although PATH or ~/.local/bin had it") if old_bin(name) else None)
    get = getattr(Settings({}), f"{which}_bin")
    assert get() is None and old_bin(which) is None                # nothing anywhere: None, as before
    local = make_exe(Path.home() / ".local" / "bin" / which)
    assert get() == old_bin(which) == str(local)                   # ~/.local/bin
    onpath = make_exe(tmp_path / "pathbin" / which)
    monkeypatch.setenv("PATH", str(onpath.parent))
    assert get() == old_bin(which) == str(onpath)                  # PATH beats ~/.local/bin


def test_claude_bin_finds_a_tool_only_the_login_shell_knows(tmp_path, bare, monkeypatch):
    nvm = make_exe(tmp_path / "nvm" / "claude")
    sh, _log = fake_shell(tmp_path, f'case "$2" in *claude) echo {nvm};; esac')
    monkeypatch.setattr(plat, "_login_shell_lookup", REAL_LOGIN_LOOKUP)
    monkeypatch.setattr(plat, "login_shell", lambda: str(sh))
    assert Settings({}).claude_bin() == str(nvm)
    assert Settings({}).codex_bin() is None


# ------------------------------------------------------------------ launchd domain helpers

def test_launchd_domain_and_target(monkeypatch):
    monkeypatch.setattr(plat, "current_uid", lambda: 501)
    assert plat.launchd_domain({}) == "gui" and plat.launchd_target("board", {}) == "gui/501/dev.ccboard.board"
    assert plat.launchd_domain({"CCBOARD_LAUNCHD_DOMAIN": " User "}) == "user"
    assert plat.launchd_target("tmux", {"CCBOARD_LAUNCHD_DOMAIN": "user"}) == "user/501/dev.ccboard.tmux"
    assert plat.launchd_domain({"CCBOARD_LAUNCHD_DOMAIN": "system"}) == "gui"       # only the two layouts exist
    monkeypatch.setattr(plat, "current_uid", lambda: None)
    assert plat.launchd_target("board", {}) is None


# ------------------------------------------------------------------ names that differ only by case

def test_check_name_refuses_a_case_collision_and_names_the_entry(projects_dir, monkeypatch):
    (projects_dir / "Foo").mkdir()
    monkeypatch.setattr(plat, "fs_case_insensitive", lambda d: True)
    with pytest.raises(projects.BadRequest) as e:
        projects.check_name("project", "foo")
    assert "'Foo'" in str(e.value) and "letter case" in str(e.value)
    assert projects.check_name("project", "Foo") == "Foo"                # the entry's own spelling is fine
    assert projects.check_name("project", "other") == "other"
    with pytest.raises(projects.BadRequest):
        projects.project_path("FOO")


def test_check_name_is_unchanged_on_a_case_sensitive_volume(projects_dir, monkeypatch):
    (projects_dir / "Foo").mkdir()
    monkeypatch.setattr(plat, "fs_case_insensitive", lambda d: False)
    assert projects.check_name("project", "foo") == "foo"
    assert projects.check_name("project", "FOO") == "FOO"


def test_only_project_names_are_checked_against_the_disk(projects_dir, monkeypatch):
    monkeypatch.setattr(plat, "fs_case_insensitive", lambda d: pytest.fail("the volume was probed for a name that is not a project"))
    assert projects.check_name("repo", "x") == "x" and projects.check_name("session", "s") == "s"


def test_create_project_refuses_a_case_collision_and_creates_nothing(projects_dir, monkeypatch):
    (projects_dir / "Foo").mkdir()
    monkeypatch.setattr(plat, "fs_case_insensitive", lambda d: True)
    with pytest.raises(projects.BadRequest, match="'Foo'"):
        projects.create_project("foo")
    assert sorted(os.listdir(projects_dir)) == ["Foo"]


def test_new_repos_refuse_a_case_collision(projects_dir, monkeypatch):
    (projects_dir / "shop" / "Api").mkdir(parents=True)
    monkeypatch.setattr(plat, "fs_case_insensitive", lambda d: True)
    with pytest.raises(projects.BadRequest, match="'Api'"):
        projects.add_repo_blank("shop", "api")
    with pytest.raises(projects.BadRequest, match="'Api'"):
        projects.prepare_repo_clone("shop", "api", "https://github.com/o/api.git")
    assert sorted(os.listdir(projects_dir / "shop")) == ["Api"]


def test_linux_outside_a_windows_drive_never_probes_the_volume(tmp_path, monkeypatch):
    monkeypatch.setattr(plat, "IS_LINUX", True)
    monkeypatch.setattr(plat, "under_drvfs", lambda p: False)
    d = tmp_path / "vol"
    d.mkdir()
    plat.fs_case_insensitive.cache_clear()
    try:
        assert plat.fs_case_insensitive(str(d)) is False and list(d.iterdir()) == []
    finally:
        plat.fs_case_insensitive.cache_clear()


def test_case_collision_helper_does_not_raise_on_a_missing_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(plat, "fs_case_insensitive", lambda d: True)
    assert projects.case_collision(tmp_path / "gone", "x") is None


# ------------------------------------------------------------------ the macOS doctor checks

@pytest.fixture
def mac(tmp_path, monkeypatch):
    """A Mac as far as the doctor can tell: macOS hints, launchd runtime, uid 501, and fake launchctl, fdesetup, defaults and pmset first on PATH.
    Each fake answers from files/variables a test sets: STATES/<label> (the job state), FDESETUP, AUTOLOGIN, PMSET."""
    monkeypatch.setattr(plat, "IS_MACOS", True)
    monkeypatch.setattr(plat, "_hint_family", lambda: "macos")
    monkeypatch.setattr(plat, "current_uid", lambda: 501)
    monkeypatch.setattr(settings, "runtime", "launchd")
    monkeypatch.delenv("CCBOARD_LAUNCHD_DOMAIN", raising=False)
    bindir, states = tmp_path / "fakebin", tmp_path / "states"
    states.mkdir()
    log = tmp_path / "calls.log"
    make_exe(bindir / "launchctl", f'''#!/bin/sh
echo "launchctl $*" >> "{log}"
label=${{2##*/}}
[ -f "{states}/$label" ] || {{ echo "Could not find service \\"$label\\" in domain" >&2; exit 113; }}
printf '%s = {{\\n\\tactive count = 1\\n\\tstate = %s\\n\\tpid = 99\\n}}\\n' "$2" "$(cat "{states}/$label")"
''')
    make_exe(bindir / "fdesetup", f'#!/bin/sh\necho "fdesetup $*" >> "{log}"\ncat "{tmp_path}/fdesetup" 2>/dev/null\n')
    make_exe(bindir / "defaults", f'''#!/bin/sh
echo "defaults $*" >> "{log}"
if [ -f "{tmp_path}/autologin" ]; then cat "{tmp_path}/autologin"; exit 0; fi
echo "The domain/default pair of (/Library/Preferences/com.apple.loginwindow, autoLoginUser) does not exist" >&2
exit 1
''')
    make_exe(bindir / "pmset", f'#!/bin/sh\necho "pmset $*" >> "{log}"\ncat "{tmp_path}/pmset" 2>/dev/null\n')
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    for job in dm.JOBS:
        (states / f"dev.ccboard.{job}").write_text("running")

    class Mac:
        pass
    m = Mac()
    m.bin, m.states, m.log, m.dir = bindir, states, log, tmp_path
    m.set_job = lambda job, state: (states / f"dev.ccboard.{job}").write_text(state) if state else (states / f"dev.ccboard.{job}").unlink()
    m.fdesetup = lambda text: (tmp_path / "fdesetup").write_text(text + "\n")
    m.autologin = lambda user: (tmp_path / "autologin").write_text(user + "\n")
    m.pmset = lambda text: (tmp_path / "pmset").write_text(text)
    return m


def test_jobs_all_running(mac):
    out = dm._c_jobs(None)
    assert out.status == "pass" and "board running, tmux running, ttyd running" in out.detail and "gui domain" in out.detail
    calls = mac.log.read_text().splitlines()
    assert calls == ["launchctl print gui/501/dev.ccboard.board", "launchctl print gui/501/dev.ccboard.tmux", "launchctl print gui/501/dev.ccboard.ttyd"]


def test_jobs_one_not_loaded_fails_with_the_bootstrap_command(mac):
    mac.set_job("ttyd", None)
    out = dm._c_jobs(None)
    assert out.status == "fail" and "dev.ccboard.ttyd is not loaded" in out.detail and "ttyd not loaded" in out.detail
    assert out.fix["cmd"] == "launchctl bootstrap gui/501 ~/Library/LaunchAgents/dev.ccboard.ttyd.plist"


def test_jobs_loaded_but_not_running_warns_with_kickstart(mac):
    mac.set_job("tmux", "waiting")
    out = dm._c_jobs(None)
    assert out.status == "warn" and "tmux waiting" in out.detail
    assert out.fix["cmd"] == "launchctl kickstart gui/$(id -u)/dev.ccboard.tmux"
    assert "tail -n 30 ~/Library/Logs/ccboard/tmux.log" in out.fix["text"]


def test_jobs_follow_the_chosen_domain(mac, monkeypatch):
    monkeypatch.setenv("CCBOARD_LAUNCHD_DOMAIN", "user")
    assert dm._c_jobs(None).status == "pass"
    assert mac.log.read_text().splitlines()[0] == "launchctl print user/501/dev.ccboard.board"


def test_the_macos_checks_skip_off_a_mac(monkeypatch, mac):
    monkeypatch.setattr(plat, "IS_MACOS", False)
    for _id, _g, _l, fn in dm.CHECKS:
        out = fn(None)
        assert out.status == "skip" and "macOS" in out.detail, _id
    assert not mac.log.exists()                                    # and nothing was run


def test_the_launchd_checks_skip_when_the_board_is_not_a_launchd_job(mac, monkeypatch):
    monkeypatch.setattr(settings, "runtime", "host")
    for fn in (dm._c_jobs, dm._c_survival):
        out = fn(None)
        assert out.status == "skip" and "runtime host" in out.detail
    assert not mac.log.exists()


def test_the_jobs_check_never_hangs_or_raises(mac, monkeypatch):
    def boom(kind):
        def run(argv, timeout=4.0):
            raise kind
        return run
    monkeypatch.setattr(doctor, "_run", boom(doctor.ToolMissing("launchctl")))
    assert dm._c_jobs(None).status == "skip"
    monkeypatch.setattr(doctor, "_run", boom(doctor.ToolTimeout("launchctl", 4.0)))
    assert dm._c_jobs(None).status == "warn"
    monkeypatch.setattr(doctor, "_run", boom(RuntimeError("odd")))
    out = dm._c_jobs(None)
    assert out.status == "warn" and "RuntimeError" in out.detail


def test_every_probe_goes_through_the_doctors_run_with_a_timeout(mac, monkeypatch):
    seen = []
    real = doctor._run

    def spy(argv, timeout=doctor.CMD_TIMEOUT):
        seen.append((argv[0], timeout))
        return real(argv, timeout)
    monkeypatch.setattr(doctor, "_run", spy)
    (Path.home() / "Library" / "Logs" / "ccboard").mkdir(parents=True)
    for _id, _g, _l, fn in dm.CHECKS:
        fn(None)
    assert {a for a, _t in seen} == {"launchctl", "fdesetup", "defaults", "pmset"}
    assert all(t and t <= doctor.CMD_TIMEOUT for _a, t in seen)


def test_survival_names_the_reboot_limit(mac):
    mac.fdesetup("FileVault is On.")
    out = dm._c_survival(None)
    assert out.status == "warn"
    assert "FileVault on; automatic login not set; the jobs run in the gui domain" in out.detail
    assert "after a reboot nothing starts until you log in" in out.detail
    assert out.fix and "logged in" in out.fix["text"]


def test_survival_with_automatic_login_and_no_filevault_passes_but_says_logout_stops_the_jobs(mac):
    mac.fdesetup("FileVault is Off.")
    mac.autologin("secretname")
    out = dm._c_survival(None)
    assert out.status == "pass" and "automatic login set" in out.detail and "stop when that user logs out" in out.detail
    assert "secretname" not in out.detail                          # the account name is never shown


def test_survival_filevault_off_without_automatic_login_still_warns(mac):
    mac.fdesetup("FileVault is Off.")
    out = dm._c_survival(None)
    assert out.status == "warn" and "after a reboot nothing starts until you log in" in out.detail


def test_survival_in_the_background_layout_is_marked_unverified(mac, monkeypatch):
    mac.fdesetup("FileVault is Off.")
    mac.autologin("u")
    monkeypatch.setenv("CCBOARD_LAUNCHD_DOMAIN", "user")
    out = dm._c_survival(None)
    assert out.status == "warn" and "user domain" in out.detail and "UNVERIFIED" in out.detail


def test_survival_unreadable_probes_read_as_unknown(mac, monkeypatch):
    def missing(argv, timeout=4.0):
        raise doctor.ToolMissing(argv[0])
    monkeypatch.setattr(doctor, "_run", missing)
    out = dm._c_survival(None)
    assert out.status == "warn" and "FileVault unknown; automatic login unknown" in out.detail


def test_the_path_check_finds_every_tool(mac, tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    for name, _req in dm.PATH_TOOLS:
        make_exe(tools / name)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setattr(plat, "BREW_BIN_DIRS", ())
    out = dm._c_path(None)
    assert out.status == "pass" and out.detail == "the board finds claude, codex, tmux, git, gh"


def test_the_path_check_fails_for_a_missing_required_tool_and_warns_for_an_optional_one(mac, tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    for name, _req in dm.PATH_TOOLS:
        make_exe(tools / name)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setattr(plat, "BREW_BIN_DIRS", ())
    (tools / "git").unlink()
    out = dm._c_path(None)
    assert out.status == "fail" and "not found: git" in out.detail and out.fix["cmd"] == "brew install git"
    (tools / "git").write_text("#!/bin/sh\n")
    (tools / "git").chmod(0o755)
    (tools / "gh").unlink()
    out = dm._c_path(None)
    assert out.status == "warn" and "not found: gh" in out.detail and "brew install gh" in out.fix["cmd"]


def test_the_path_check_separates_off_path_from_missing(mac, tmp_path, monkeypatch):
    """claude and codex are started by full path, so ~/.local/bin is fine; tmux, git and gh are started by name, so they must be on the board's PATH."""
    tools, brew = tmp_path / "tools", tmp_path / "brew"
    for name in ("git", "gh"):
        make_exe(tools / name)
    make_exe(brew / "tmux")
    make_exe(Path.home() / ".local" / "bin" / "claude")
    make_exe(Path.home() / ".local" / "bin" / "codex")
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setattr(plat, "BREW_BIN_DIRS", (str(brew),))
    out = dm._c_path(None)
    assert out.status == "warn" and "found only outside the board's PATH: tmux" in out.detail and "not found" not in out.detail
    assert "PATH of the board's launchd job" in out.fix["text"] and out.fix["cmd"] == "launchctl kickstart -k gui/$(id -u)/dev.ccboard.board"


def test_the_path_check_uses_the_login_shell_for_claude(mac, tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    for name in ("codex", "tmux", "git", "gh"):
        make_exe(tools / name)
    nvm = make_exe(tmp_path / "nvm" / "claude")
    sh, _log = fake_shell(tmp_path, f"echo {nvm}")
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setattr(plat, "BREW_BIN_DIRS", ())
    monkeypatch.setattr(plat, "_login_shell_lookup", REAL_LOGIN_LOOKUP)
    monkeypatch.setattr(plat, "login_shell", lambda: str(sh))
    assert dm._c_path(None).status == "pass"


@pytest.mark.parametrize("sub", ["Desktop", "Documents/work", "Downloads", "Library/Mobile Documents/com~apple~CloudDocs/p"])
def test_privacy_warns_for_a_folder_macos_guards(mac, monkeypatch, sub):
    target = Path.home() / sub
    target.mkdir(parents=True)
    monkeypatch.setattr(settings, "projects_dir", target)
    out = dm._c_privacy(None)
    assert out.status == "warn" and "Files and Folders" in out.detail
    assert "~/projects" in out.fix["text"] and "Privacy & Security" in out.fix["text"]


def test_privacy_fails_when_macos_refuses_the_listing(mac, monkeypatch):
    target = Path.home() / "Documents" / "p"
    target.mkdir(parents=True)
    monkeypatch.setattr(settings, "projects_dir", target)

    def denied(path):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(dm, "_listdir", denied)
    out = dm._c_privacy(None)
    assert out.status == "fail" and "refuses" in out.detail


def test_privacy_passes_elsewhere(mac, monkeypatch):
    target = Path.home() / "projects"
    target.mkdir()
    monkeypatch.setattr(settings, "projects_dir", target)
    assert dm._c_privacy(None).status == "pass"
    (Path.home() / "Documents").mkdir()
    monkeypatch.setattr(settings, "projects_dir", Path.home() / "DocumentsOther")       # a name that merely starts with Documents
    (Path.home() / "DocumentsOther").mkdir()
    assert dm._c_privacy(None).status == "pass"


def test_privacy_follows_a_link_into_a_guarded_folder(mac, monkeypatch):
    guarded = Path.home() / "Desktop" / "real"
    guarded.mkdir(parents=True)
    link = Path.home() / "projects"
    link.symlink_to(guarded)
    monkeypatch.setattr(settings, "projects_dir", link)
    assert dm._c_privacy(None).status == "warn"


def test_case_check(mac, monkeypatch, tmp_path):
    pdir = tmp_path / "projects"
    pdir.mkdir()
    monkeypatch.setattr(settings, "projects_dir", pdir)
    monkeypatch.setattr(plat, "fs_case_insensitive", lambda d: False)
    assert dm._c_case(None).status == "pass"
    monkeypatch.setattr(plat, "fs_case_insensitive", lambda d: True)
    assert dm._c_case(None).status == "pass" and "ignores letter case" in dm._c_case(None).detail
    monkeypatch.setattr(dm, "_names", lambda folder: ["Foo", "bar", "foo"] if folder == pdir else [])
    out = dm._c_case(None)
    assert out.status == "warn" and "'Foo' and 'foo' differ only by letter case" in out.detail
    monkeypatch.setattr(settings, "projects_dir", tmp_path / "missing")
    assert dm._c_case(None).status == "skip"


def test_case_check_looks_one_level_into_each_project(mac, monkeypatch, tmp_path):
    pdir = tmp_path / "projects"
    (pdir / "shop").mkdir(parents=True)
    monkeypatch.setattr(settings, "projects_dir", pdir)
    monkeypatch.setattr(plat, "fs_case_insensitive", lambda d: True)
    monkeypatch.setattr(dm, "_names", lambda folder: ["shop"] if folder == pdir else ["Api", "api", "web"])
    out = dm._c_case(None)
    assert out.status == "warn" and "'shop/Api' and 'shop/api'" in out.detail


PMSET_CAFFEINATE = """Assertion status system-wide:
   PreventUserIdleSystemSleep     1
   PreventSystemSleep             0
Listed by owning process:
   pid 4242(caffeinate): [0x0000000100000001] 00:12:01 PreventUserIdleSystemSleep named: "caffeinate command-line tool"
"""
PMSET_OTHER = PMSET_CAFFEINATE.replace("caffeinate command-line tool", "Amphetamine").replace("(caffeinate)", "(Amphetamine)")
PMSET_NONE = """Assertion status system-wide:
   PreventUserIdleSystemSleep     0
   PreventSystemSleep             0
Listed by owning process:
   pid 77(powerd): [0x0000000100000002] 00:00:03 NoDisplaySleepAssertion named: "x"
"""


def test_sleep_check(mac):
    mac.pmset(PMSET_CAFFEINATE)
    out = dm._c_sleep(None)
    assert out.status == "pass" and "caffeinate holds idle sleep off" in out.detail and "UNVERIFIED" in out.detail
    mac.pmset(PMSET_OTHER)
    out = dm._c_sleep(None)
    assert out.status == "pass" and "held off by Amphetamine" in out.detail
    mac.pmset(PMSET_NONE)
    out = dm._c_sleep(None)
    assert out.status == "warn" and "nothing holds idle sleep off" in out.detail and "CCBOARD_KEEP_AWAKE=1" in out.fix["text"]
    assert mac.log.read_text().splitlines()[-1] == "pmset -g assertions"


def test_sleep_check_without_a_pmset_answer(mac, monkeypatch):
    monkeypatch.setattr(doctor, "_run", lambda argv, timeout=4.0: doctor.Proc(1, "", "pmset: error"))
    assert dm._c_sleep(None).status == "warn"


def test_log_folder_size(mac, monkeypatch):
    folder = Path.home() / "Library" / "Logs" / "ccboard"
    assert dm._c_logs(None).status == "skip"                       # not created yet
    folder.mkdir(parents=True)
    (folder / "board.log").write_bytes(b"x" * 2000)
    assert dm._c_logs(None).status == "pass"
    monkeypatch.setattr(dm, "LOG_WARN_BYTES", 1000)
    (folder / "ttyd.log").write_bytes(b"x" * 500)
    out = dm._c_logs(None)
    assert out.status == "warn" and "never rotates" in out.detail
    assert out.fix["cmd"] == f": > {folder / 'board.log'}"
    (folder / "sub").mkdir()                                       # a folder inside is not a log and is not counted
    assert dm._c_logs(None).status == "warn"


def test_the_log_limit_is_fifty_megabytes():
    assert dm.LOG_WARN_BYTES == 50 * 1024 * 1024


def test_the_checks_register_on_the_box_group_once(monkeypatch):
    monkeypatch.setattr(doctor, "CHECKS", list(doctor.CHECKS))
    monkeypatch.setattr(doctor, "GROUPS", list(doctor.GROUPS))
    dm.register_all()
    dm.register_all()
    ids = [c[0] for c in doctor.CHECKS if c[0].startswith("macos-")]
    assert sorted(ids) == sorted(c[0] for c in dm.CHECKS) and len(ids) == 7
    assert {c[1] for c in doctor.CHECKS if c[0].startswith("macos-")} == {"box"}
    assert {c[0]: c[3] for c in doctor.CHECKS}["macos-jobs"] is dm._c_jobs


@pytest.mark.skipif(plat.IS_MACOS, reason="a Mac registers these checks on purpose")
def test_a_linux_board_lists_none_of_them():
    assert not [c for c in doctor.CHECKS if c[0].startswith("macos-")]
    assert "macos" not in doctor.GROUPS


def test_the_module_imports_before_the_doctor_does():
    code = "import app.doctor_macos as m, app.doctor as d; assert m.d is d; print(len(m.CHECKS))"
    cp = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**os.environ, "CCBOARD_ENV_FILE": ""}, capture_output=True, text=True, timeout=120)
    assert cp.returncode == 0 and cp.stdout.strip() == "7", cp.stderr


# ------------------------------------------------------------------ the doctor's tool lookups

def test_run_retries_a_missing_tool_through_resolve_bin_off_linux(tmp_path, monkeypatch):
    tool = make_exe(tmp_path / "elsewhere" / "mytool77", "#!/bin/sh\necho ran $1\n")
    monkeypatch.setenv("PATH", str(tmp_path / "emptybin"))
    monkeypatch.setattr(plat, "IS_LINUX", False)
    monkeypatch.setattr(plat, "resolve_bin", lambda name: str(tool) if name == "mytool77" else None)
    assert doctor._run(["mytool77", "x"]) == doctor.Proc(0, "ran x\n", "")
    with pytest.raises(doctor.ToolMissing):
        doctor._run(["othertool77"])
    assert doctor._which("mytool77") == str(tool) and doctor._which("othertool77") is None


def test_run_and_which_never_ask_resolve_bin_on_linux(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "emptybin"))
    monkeypatch.setattr(plat, "IS_LINUX", True)
    monkeypatch.setattr(plat, "resolve_bin", lambda name: pytest.fail("resolve_bin asked on Linux"))
    with pytest.raises(doctor.ToolMissing):
        doctor._run(["mytool77"])
    assert doctor._which("mytool77") is None
    with pytest.raises(doctor.ToolMissing):
        doctor._run(["/nonexistent/dir/tool"])


def test_run_does_not_retry_a_path_or_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(plat, "IS_LINUX", False)
    monkeypatch.setattr(plat, "resolve_bin", lambda name: pytest.fail("a full path was resolved"))
    with pytest.raises(doctor.ToolMissing):
        doctor._run([str(tmp_path / "nope")])
    monkeypatch.setattr(plat, "resolve_bin", lambda name: str(tmp_path / "also-nope"))        # resolves to a path that does not run either
    with pytest.raises(doctor.ToolMissing):
        doctor._run(["mytool77"])
