"""Issue #291: the full doctor never starts a program from the machine the suite runs on.

`GET /api/doctor` and `doctor.run()` with every check reach _c_tailscale (/usr/local/bin/tailscale on a Mac), gh, ttyd, tmux and more. Their
answers then depended on the developer's real Tailscale, GitHub and Homebrew state. tests/conftest.py (_hermetic_binaries) finds a machine
tool only on a temp PATH directory; this file runs the real check set in the normal test environment and fails if any process was started
from outside the temp dirs, so a new check that shells out to something else is caught the day it is added.
"""
import os
import stat
import subprocess
import sys

import pytest

from app import doctor
from tests.conftest import MACHINE_TOOLS, SPAWNED, under_temp

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}

# System programs the full doctor may still start: version probes of tools every developer box and CI runner has (the Git row, the hook
# helpers row), which read no account, network or service state. Anything else outside the temp dirs fails the test below.
SYSTEM_OK = {"git": "the Git row: git --version", "curl": "the hook helpers row: curl --version", "python3": "the hook helpers row: python3 --version"}


def outside_temp() -> list[str]:
    return sorted({p for p in SPAWNED if not under_temp(p) and os.path.basename(p) not in SYSTEM_OK})


def test_the_guard_helpers_know_a_temp_dir_from_the_machine(tmp_path):
    assert under_temp(tmp_path) and under_temp(tmp_path / "bin" / "tailscale")
    assert not under_temp("/usr/local/bin/tailscale") and not under_temp("/opt/homebrew/bin")
    assert {"tailscale", "gh", "ttyd", "claude", "codex", "restic", "docker", "systemctl", "launchctl", "bun", "code-server"} <= MACHINE_TOOLS


def test_the_full_doctor_starts_nothing_from_outside_the_temp_dirs(lite_client, fake_tmux, monkeypatch):
    import app.main                                               # noqa: F401  (registers every check)
    doctor.invalidate()
    SPAWNED.clear()
    direct = doctor.run(refresh=True, db=None)
    doctor.invalidate()
    wire = lite_client.get("/api/doctor?refresh=1", headers=H)
    doctor.invalidate()
    assert wire.status_code == 200
    ids = {c["id"] for c in wire.json()["checks"]}
    assert {"tmux", "ttyd", "tailscale", "gh", "claude-bin"} <= ids and {c["id"] for c in direct["checks"]} >= ids - {"git"}, "the real check set ran"
    assert outside_temp() == [], "the doctor started a program from this machine: " + ", ".join(outside_temp())


def test_a_tool_on_the_temp_path_is_found_and_the_machines_copy_is_not(tmp_path, monkeypatch):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    fake = bin_ / "tailscale"
    fake.write_text("#!/bin/sh\necho 1.2.3\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}/usr/local/bin{os.pathsep}/opt/homebrew/bin{os.pathsep}/usr/bin{os.pathsep}/bin")
    import shutil
    assert shutil.which("tailscale") == str(fake)
    assert shutil.which("gh") is None and shutil.which("restic") is None, "a machine tool outside the temp PATH is invisible"
    assert shutil.which("sh"), "an ordinary system program is still found"
    out = subprocess.run(["tailscale"], capture_output=True, text=True, timeout=5)
    assert out.stdout.strip() == "1.2.3" and str(fake) in SPAWNED
    for path in ("/usr/local/bin/tailscale", "/opt/homebrew/bin/gh", "gh"):
        with pytest.raises(FileNotFoundError):
            subprocess.run([path, "--version"], capture_output=True, timeout=5)
    assert not [p for p in SPAWNED if p != str(fake)] or all(os.path.basename(p) not in MACHINE_TOOLS for p in SPAWNED if p != str(fake))
    assert sys.executable                                        # (the interpreter itself is never a machine tool)
