"""Issue #123: the platform markers are registered (and --strict-markers is on), every marker used under tests/ is one of them, and
tests/conftest.py skips a marked test on a host that lacks what the marker names, with the reason in the report."""
import re
import shutil
import types
from pathlib import Path

import pytest

from app import platform

from . import conftest as ctf

ROOT = Path(__file__).resolve().parent.parent
OURS = {"linux_only", "needs_tmux", "needs_systemd", "needs_proc", "posix_sh", "needs_macos"}
BUILTIN = {"parametrize", "skip", "skipif", "xfail", "usefixtures", "filterwarnings", "tryfirst", "trylast", "timeout"}


def _registered(pytestconfig):
    return {line.split(":")[0].split("(")[0].strip() for line in pytestconfig.getini("markers")}


def test_the_six_markers_are_registered_and_strict(pytestconfig):
    assert OURS <= _registered(pytestconfig)
    assert "--strict-markers" in str(pytestconfig.getini("addopts"))


def test_pytest_is_configured_in_one_place_only():
    assert (ROOT / "pytest.ini").is_file()
    for other in ("pyproject.toml", "setup.cfg", "tox.ini"):
        assert not (ROOT / other).exists(), f"{other} would also configure pytest"


def test_every_marker_used_in_the_suite_is_registered(pytestconfig):
    known = _registered(pytestconfig) | BUILTIN | {"real_home"}
    used = {}
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        for name in re.findall(r"pytest\.mark\.(\w+)", path.read_text()):
            used.setdefault(name, path.name)
    unknown = {n: f for n, f in used.items() if n not in known}
    assert not unknown, f"markers used but not registered in pytest.ini: {unknown}"


def test_no_marker_is_used_that_the_conftest_does_not_know_how_to_skip():
    handled = set(re.findall(r'"(\w+)" in marks', Path(ctf.__file__).read_text()))
    assert handled == OURS


def test_linux_only_is_skipped_when_the_platform_flag_is_off(monkeypatch):
    monkeypatch.setattr(platform, "IS_LINUX", True)
    assert ctf.platform_skip_reason({"linux_only"}) is None
    monkeypatch.setattr(platform, "IS_LINUX", False)
    reason = ctf.platform_skip_reason({"linux_only"})
    assert reason and reason.startswith("linux_only: needs a Linux host")


def test_needs_macos_follows_the_flag(monkeypatch):
    monkeypatch.setattr(platform, "IS_MACOS", False)
    assert "needs_macos" in ctf.platform_skip_reason({"needs_macos"})
    monkeypatch.setattr(platform, "IS_MACOS", True)
    assert ctf.platform_skip_reason({"needs_macos"}) is None


def test_needs_proc_follows_the_proc_root_seam(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "PROC_ROOT", tmp_path / "no-proc")
    assert "needs_proc" in ctf.platform_skip_reason({"needs_proc"})
    (tmp_path / "proc" / "self").mkdir(parents=True)
    monkeypatch.setattr(platform, "PROC_ROOT", tmp_path / "proc")
    assert ctf.platform_skip_reason({"needs_proc"}) is None


def test_needs_tmux_and_posix_sh_follow_shutil_which(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    assert ctf.platform_skip_reason({"needs_tmux"}) == "needs_tmux: tmux is not on PATH"
    assert "posix_sh" in ctf.platform_skip_reason({"posix_sh"})
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    assert ctf.platform_skip_reason({"needs_tmux", "posix_sh"}) is None
    monkeypatch.setattr(platform, "IS_WINDOWS", True)
    assert "posix_sh" in ctf.platform_skip_reason({"posix_sh"})            # sh found under Git Bash is not the board's sh


def test_needs_systemd_wants_linux_a_systemctl_and_a_running_systemd(monkeypatch):
    monkeypatch.setattr(platform, "IS_LINUX", False)
    assert "needs_systemd" in ctf.platform_skip_reason({"needs_systemd"})
    monkeypatch.setattr(platform, "IS_LINUX", True)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    assert "systemd is not running" in ctf.platform_skip_reason({"needs_systemd"})


def test_an_unmarked_test_is_never_skipped(monkeypatch):
    monkeypatch.setattr(platform, "IS_LINUX", False)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    assert ctf.platform_skip_reason(set()) is None
    assert ctf.platform_skip_reason({"parametrize", "real_home"}) is None


def test_the_collection_hook_adds_a_skip_with_the_reason(monkeypatch):
    monkeypatch.setattr(platform, "IS_LINUX", False)

    class Item:
        def __init__(self, *names):
            self.names, self.added = names, []

        def iter_markers(self):
            return [types.SimpleNamespace(name=n) for n in self.names]

        def add_marker(self, mark):
            self.added.append(mark)
    marked, plain = Item("linux_only", "parametrize"), Item("parametrize")
    ctf.pytest_collection_modifyitems(None, [marked, plain])
    assert plain.added == []
    assert len(marked.added) == 1 and marked.added[0].name == "skip" and "needs a Linux host" in marked.added[0].kwargs["reason"]


@pytest.mark.posix_sh
def test_a_posix_sh_test_runs_here_because_this_host_has_sh():
    assert shutil.which("sh")
