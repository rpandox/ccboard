"""app/platform.py, issue #116 slices 1 to 4 (imports and guards, files, defaults, runtime). The hints are in tests/test_platform_hints.py.

Flags are patched (`IS_LINUX`, `IS_MACOS`, `IS_WINDOWS`) so every system's answer is checked on whatever host runs the suite. Nothing here
touches the real home: HOME and the data dir point at a temp dir, and the uid, the pwd module and fcntl are faked by deleting or blocking them.
"""
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import backup, projects, tree
from app import platform as plat
from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def work(tmp_path):
    """An empty directory of our own (conftest puts a codex home in tmp_path)."""
    d = tmp_path / "work"
    d.mkdir()
    return d


def _system(monkeypatch, name):
    monkeypatch.setattr(plat, "IS_LINUX", name == "linux")
    monkeypatch.setattr(plat, "IS_MACOS", name == "macos")
    monkeypatch.setattr(plat, "IS_WINDOWS", name == "windows")


# ---------------------------------------------------------------- imports and guards

def test_the_board_imports_without_pwd_and_fcntl(tmp_path):
    """Native Windows has neither module: app.config, app.backup and app.tree must still import (a None entry in sys.modules makes
    `import pwd` raise ImportError)."""
    code = ("import sys\nsys.modules['pwd'] = None\nsys.modules['fcntl'] = None\n"
            "import app.config, app.backup, app.tree\nprint('imported')\n")
    env = {**os.environ, "HOME": str(tmp_path), "CCBOARD_DATA_DIR": str(tmp_path / "data"), "PROJECTS_DIR": str(tmp_path)}
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "imported" in r.stdout


def test_current_uid_is_none_without_getuid(monkeypatch, tmp_path):
    assert plat.current_uid() == os.getuid()
    monkeypatch.delattr(os, "getuid")
    assert plat.current_uid() is None
    assert plat.owned_by_me(tmp_path) is None            # "no uid" is never "not yours"
    assert plat.passwd_home() is None


def test_owned_by_me_answers_with_a_uid(monkeypatch, tmp_path):
    assert plat.owned_by_me(tmp_path) is True
    monkeypatch.setattr(plat, "current_uid", lambda: plat.os.stat(tmp_path).st_uid + 1)
    assert plat.owned_by_me(tmp_path) is False


def test_login_shell_falls_back_without_pwd(monkeypatch):
    monkeypatch.setitem(sys.modules, "pwd", None)
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    assert plat.login_shell() == "/usr/bin/fish"
    monkeypatch.delenv("SHELL")
    assert plat.login_shell() == "/bin/sh"
    assert plat.passwd_home() is None


def test_login_shell_without_a_uid(monkeypatch):
    monkeypatch.delattr(os, "getuid")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert plat.login_shell() == "/bin/zsh"


def test_login_shell_reads_passwd_first(monkeypatch):
    fake = SimpleNamespace(getpwuid=lambda uid: SimpleNamespace(pw_shell="/bin/ksh", pw_dir="/home/example", pw_name="example"))
    monkeypatch.setitem(sys.modules, "pwd", fake)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert plat.login_shell() == "/bin/ksh"
    assert plat.passwd_home() == Path("/home/example")
    assert Settings({}).login_shell == "/bin/ksh"
    fake.getpwuid = lambda uid: SimpleNamespace(pw_shell="", pw_dir="", pw_name="example")
    assert plat.login_shell() == "/bin/sh"               # an empty shell field: the old fallback
    assert plat.passwd_home() is None


def test_whoami(monkeypatch):
    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwuid=lambda uid: SimpleNamespace(pw_name="example")))
    assert plat.whoami() == "example"

    def nobody(uid):
        raise KeyError(uid)
    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwuid=nobody))
    assert plat.whoami() == f"uid {os.geteuid()}"
    monkeypatch.delattr(os, "geteuid")
    assert plat.whoami() == "unknown"


def test_validate_skips_the_owner_check_without_a_uid(monkeypatch, tmp_path):
    proj = tmp_path / "projects"
    proj.mkdir()
    s = Settings({"PROJECTS_DIR": str(proj), "CCBOARD_DATA_DIR": str(tmp_path / "data"), "CCBOARD_ALLOWED_USERS": "a@example.com"})
    monkeypatch.setattr(plat, "owned_by_me", lambda p: None)
    s.validate()                                                         # no SystemExit
    monkeypatch.setattr(plat, "owned_by_me", lambda p: False)
    with pytest.raises(SystemExit, match="must be owned by the ccboard user"):
        s.validate()


def test_validate_warns_about_a_windows_drive(monkeypatch, tmp_path, caplog):
    proj = tmp_path / "projects"
    proj.mkdir()
    s = Settings({"PROJECTS_DIR": str(proj), "CCBOARD_DATA_DIR": str(tmp_path / "data"), "CCBOARD_ALLOWED_USERS": "a@example.com"})
    caplog.set_level(logging.WARNING, logger="ccboard")
    s.validate()
    assert "Windows drive" not in caplog.text
    monkeypatch.setattr(plat, "under_drvfs", lambda p: True)
    s.validate()                                                         # a warning, never a refusal
    assert "Windows drive" in caplog.text and str(proj) in caplog.text


def test_under_drvfs_needs_wsl_and_mnt(monkeypatch):
    monkeypatch.setattr(plat, "is_wsl", lambda: False)
    assert plat.under_drvfs("/mnt/c/projects") is False
    monkeypatch.setattr(plat, "is_wsl", lambda: True)
    assert plat.under_drvfs("/mnt/c/projects") is True
    assert plat.under_drvfs("/home/example/projects") is False


def test_tree_checks_for_a_link_itself_where_there_is_no_o_nofollow(monkeypatch, tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("secret")
    link = tmp_path / "link.txt"
    os.symlink(real, link)
    monkeypatch.setattr(tree, "_resolve", lambda project, repo, path: SimpleNamespace(parts=["link.txt"], target=str(link)))
    assert plat.o_nofollow() == getattr(os, "O_NOFOLLOW", 0)
    with pytest.raises(projects.BadRequest):                             # the kernel refuses it
        tree.read_file("p", "r", "link.txt")
    monkeypatch.setattr(plat, "o_nofollow", lambda: 0)                   # a system without the flag: the open follows, the lstat refuses
    with pytest.raises(projects.BadRequest, match="symbolic link"):
        tree.read_file("p", "r", "link.txt")
    monkeypatch.setattr(tree, "_resolve", lambda project, repo, path: SimpleNamespace(parts=["real.txt"], target=str(real)))
    assert tree.read_file("p", "r", "real.txt")["text"] == "secret"      # a plain file is still read


def test_backup_lock_is_held_through_the_platform(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    first = backup.try_lock()
    assert first is not None
    assert backup.try_lock() is None                                     # the second holder gets None, not an exception
    first.close()
    again = backup.try_lock()
    assert again is not None
    again.close()


def test_backup_lock_treats_a_held_lock_error_as_held(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    def held(f):
        raise PermissionError(13, "locked")                              # what msvcrt.locking raises
    monkeypatch.setattr(plat, "lock_nb", held)
    assert backup.try_lock() is None


# ---------------------------------------------------------------- files

def test_atomic_replace_is_one_plain_replace_on_posix(monkeypatch, tmp_path):
    _system(monkeypatch, "linux")
    calls = []

    def boom(src, dst):
        calls.append((src, dst))
        raise PermissionError(13, "held open")
    monkeypatch.setattr(plat.os, "replace", boom)
    with pytest.raises(PermissionError):
        plat.atomic_replace("a", "b")
    assert len(calls) == 1                                               # no retry off Windows


def test_atomic_replace_retries_on_windows_only(monkeypatch, tmp_path):
    _system(monkeypatch, "windows")
    slept = []
    monkeypatch.setattr(plat.time, "sleep", slept.append)
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_text("new")
    real, calls = os.replace, []

    def flaky(a, b):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(13, "held open")
        real(a, b)
    monkeypatch.setattr(plat.os, "replace", flaky)
    plat.atomic_replace(src, dst)
    assert len(calls) == 3 and len(slept) == 2 and dst.read_text() == "new"
    calls.clear()
    monkeypatch.setattr(plat.os, "replace", lambda a, b: calls.append(1) or (_ for _ in ()).throw(PermissionError(13, "held")))
    with pytest.raises(PermissionError):
        plat.atomic_replace(src, dst)
    assert len(calls) == 3                                               # three tries, then the error


def test_atomic_replace_does_not_retry_other_errors(monkeypatch):
    _system(monkeypatch, "windows")
    calls = []

    def missing(a, b):
        calls.append(1)
        raise FileNotFoundError(2, "gone")
    monkeypatch.setattr(plat.os, "replace", missing)
    with pytest.raises(FileNotFoundError):
        plat.atomic_replace("a", "b")
    assert len(calls) == 1


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644])
def test_secure_write_makes_the_same_file_the_old_routine_did(work, mode):
    dest = work / "creds.json"
    dest.write_bytes(b"old")
    os.chmod(dest, 0o644)
    data = b'{"opaque": "\xff\x00bytes"}'
    plat.secure_write(dest, data, mode)
    assert dest.read_bytes() == data
    assert stat.S_IMODE(dest.stat().st_mode) == mode
    assert [p.name for p in work.iterdir()] == ["creds.json"]        # no temp file left behind


def test_secure_write_creates_the_file_private_before_any_chmod(tmp_path, monkeypatch):
    seen = []
    real_open = os.open

    def spy(path, flags, mode=0o777, **kw):
        seen.append((Path(path).name, flags & os.O_EXCL, mode))
        return real_open(path, flags, mode, **kw)
    monkeypatch.setattr(plat.os, "open", spy)
    plat.secure_write(tmp_path / "x", b"1", 0o644)
    name, excl, mode = seen[0]
    assert name.startswith(".x.") and name.endswith(".tmp") and excl and mode == 0o600


def test_secure_write_still_false_replaces_nothing(work):
    dest = work / "state.json"
    dest.write_bytes(b"theirs")
    with pytest.raises(plat.WriteRaced):
        plat.secure_write(dest, b"ours", 0o600, still=lambda: False)
    assert dest.read_bytes() == b"theirs"
    assert [p.name for p in work.iterdir()] == ["state.json"]


def test_secure_write_removes_its_temp_file_when_the_replace_fails(work, monkeypatch):
    def boom(a, b):
        raise OSError(5, "io")
    monkeypatch.setattr(plat.os, "replace", boom)
    with pytest.raises(OSError):
        plat.secure_write(work / "x", b"1")
    assert list(work.iterdir()) == []


def test_secure_write_without_fchmod_uses_chmod(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "fchmod")
    plat.secure_write(tmp_path / "x", b"1", 0o640)
    assert stat.S_IMODE((tmp_path / "x").stat().st_mode) == 0o640


def test_account_store_writes_through_the_platform(tmp_path):
    from app import account_store
    assert account_store._Changed is plat.WriteRaced
    dest = tmp_path / ".credentials.json"
    account_store._write_atomic(dest, b"abc", 0o600)
    assert dest.read_bytes() == b"abc" and stat.S_IMODE(dest.stat().st_mode) == 0o600
    with pytest.raises(account_store._Changed):
        account_store._write_atomic(dest, b"def", 0o600, still=lambda: False)
    assert dest.read_bytes() == b"abc"


def test_spawn_detached_posix_kwargs(monkeypatch, tmp_path):
    _system(monkeypatch, "linux")
    seen = {}

    def fake_popen(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return "proc"
    monkeypatch.setattr(plat.subprocess, "Popen", fake_popen)
    with open(tmp_path / "log", "ab") as logf:
        assert plat.spawn_detached(["prog", "-m", "x"], logf, cwd="/work") == "proc"
        assert seen["argv"] == ["prog", "-m", "x"]
        assert seen["kw"] == {"stdin": subprocess.DEVNULL, "stdout": logf, "stderr": subprocess.STDOUT, "start_new_session": True, "cwd": "/work"}


def test_spawn_detached_windows_flags(monkeypatch, tmp_path):
    _system(monkeypatch, "windows")
    seen = {}
    monkeypatch.setattr(plat.subprocess, "Popen", lambda argv, **kw: seen.update(kw))
    monkeypatch.setattr(plat.subprocess, "DETACHED_PROCESS", 8, raising=False)
    monkeypatch.setattr(plat.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    with open(tmp_path / "log", "ab") as logf:
        plat.spawn_detached(["prog"], logf)
    assert seen["creationflags"] == 520 and "start_new_session" not in seen


def test_backup_starts_detached_through_the_platform(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "runtime", "docker")
    seen = {}
    monkeypatch.setattr(backup.subprocess, "Popen", lambda argv, **kw: seen.update(argv=argv, kw=kw))
    assert backup.start_detached() == "process"
    assert seen["argv"][-2:] == ["-m", "app.backup"]
    assert seen["kw"]["start_new_session"] is True and seen["kw"]["stdin"] == subprocess.DEVNULL
    assert seen["kw"]["stderr"] == subprocess.STDOUT and Path(seen["kw"]["cwd"]) == ROOT


def test_write_status_replaces_through_the_platform(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    seen = []
    real = plat.atomic_replace
    monkeypatch.setattr(plat, "atomic_replace", lambda a, b: seen.append(Path(b).name) or real(a, b))
    backup.write_status({"status": "ok"})
    assert seen == ["backup-status.json"] and (tmp_path / "backup-status.json").exists()


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="POSIX")
def test_o_nofollow_is_the_os_flag():
    assert plat.o_nofollow() == os.O_NOFOLLOW


def test_fs_case_insensitive_returns_a_bool(work):
    plat.fs_case_insensitive.cache_clear()
    got = plat.fs_case_insensitive(str(work))
    assert isinstance(got, bool)
    assert plat.fs_case_insensitive(str(work)) is got                # cached per directory
    assert list(work.iterdir()) == []                                # the probe file is gone
    assert plat.fs_case_insensitive(str(work / "missing")) is False  # a probe that cannot run says "they differ"


# ---------------------------------------------------------------- defaults

def test_defaults_per_system(monkeypatch):
    home = Path.home()
    data = home / ".local" / "share" / "ccboard"
    for name, projects_dir in (("linux", Path("/srv/projects")), ("macos", home / "projects"), ("windows", home / "projects")):
        _system(monkeypatch, name)
        assert plat.default_projects_dir() == projects_dir, name
        assert plat.default_data_dir() == data, name
        s = Settings({})
        assert s.projects_dir == projects_dir and s.data_dir == data, name


def test_environment_still_wins_over_the_defaults(monkeypatch, tmp_path):
    _system(monkeypatch, "macos")
    s = Settings({"PROJECTS_DIR": str(tmp_path / "p"), "CCBOARD_DATA_DIR": str(tmp_path / "d")})
    assert s.projects_dir == tmp_path / "p" and s.data_dir == tmp_path / "d"


def test_the_data_dir_default_is_the_one_the_hook_scripts_use():
    text = (ROOT / "bin" / "ccboard-hook").read_text()
    assert ".local/share/ccboard" in text


# ---------------------------------------------------------------- runtime

def test_runtime_markers():
    assert plat.resolve_runtime({"CCBOARD_RUNTIME": "launchd"}) == ("launchd", False)
    assert plat.resolve_runtime({"XPC_SERVICE_NAME": "dev.ccboard.web"}) == ("launchd", True)
    assert plat.resolve_runtime({"XPC_SERVICE_NAME": "com.apple.Terminal"}) == ("host", False)   # every Mac login shell has some XPC name
    assert plat.resolve_runtime({"INVOCATION_ID": "x"}) == ("systemd", True)
    assert plat.resolve_runtime({"CCBOARD_RUNTIME": "docker", "INVOCATION_ID": "x"}) == ("docker", False)


def test_launchd_refuses_the_dev_bypass(caplog):
    caplog.set_level(logging.WARNING, logger="ccboard")
    s = Settings({"CCBOARD_RUNTIME": "launchd", "CCBOARD_DEV_BYPASS_USER": "x"})
    assert s.runtime == "launchd" and s.dev_bypass_user is None
    assert "ignoring CCBOARD_DEV_BYPASS_USER" in caplog.text and "launchd" in caplog.text


def test_a_detected_launchd_service_refuses_even_an_explicit_host():
    assert Settings({"CCBOARD_RUNTIME": "host", "XPC_SERVICE_NAME": "dev.ccboard.web", "CCBOARD_DEV_BYPASS_USER": "x"}).dev_bypass_user is None
    assert Settings({"XPC_SERVICE_NAME": "dev.ccboard.web", "CCBOARD_DEV_BYPASS_USER": "x"}).dev_bypass_user is None
    assert Settings({"CCBOARD_RUNTIME": "host", "XPC_SERVICE_NAME": "com.apple.Terminal", "CCBOARD_DEV_BYPASS_USER": "x"}).dev_bypass_user == "x"


def test_a_refused_bypass_is_logged_and_an_unset_one_is_not(caplog):
    caplog.set_level(logging.WARNING, logger="ccboard")
    Settings({"INVOCATION_ID": "abc"})
    assert "CCBOARD_DEV_BYPASS_USER" not in caplog.text
    Settings({"INVOCATION_ID": "abc", "CCBOARD_DEV_BYPASS_USER": "x"})
    assert "ignoring CCBOARD_DEV_BYPASS_USER" in caplog.text


def test_an_unknown_runtime_is_ignored(caplog):
    caplog.set_level(logging.WARNING, logger="ccboard")
    assert Settings({"CCBOARD_RUNTIME": "podman"}).runtime == "host"
    assert "ignoring unknown CCBOARD_RUNTIME" in caplog.text
    assert Settings({"CCBOARD_RUNTIME": "podman", "CCBOARD_DEV_BYPASS_USER": "x"}).dev_bypass_user == "x"      # still a dev shell


# ---------------------------------------------------------------- naming

def test_import_platform_is_the_standard_library():
    """app/platform.py shadows nothing: only the package-relative name is ours."""
    import platform as stdlib
    assert hasattr(stdlib, "system") and hasattr(stdlib, "python_version")
    assert Path(stdlib.__file__).resolve().parent != (ROOT / "app").resolve()
    assert sys.modules["platform"] is stdlib and sys.modules["app.platform"] is plat and stdlib is not plat


def test_nothing_puts_app_on_sys_path():
    assert str(ROOT / "app") not in [str(Path(p).resolve()) for p in sys.path if p]
