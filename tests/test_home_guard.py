"""Issue #101: the real-home canary. tests/conftest.py snapshots the real home's Claude and Codex files (stat only) and fails the run if
any changed; every test gets HOME/USERPROFILE pointed at a temp dir. This file proves both, and statically requires every direct home
access in app/, scripts/ and bin/ to be in app/config.py or listed below with a reason.

The proof test runs a tiny mis-sandboxed test file in a subprocess against a FAKE "real" home (CCBOARD_GUARD_REAL_HOME), never the
owner's.
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# file -> why it may touch the home directly. app/config.py is allowed without an entry (it owns the path defaults).
HOME_USE_ALLOWED = {
    "app/backup.py": "expands a user-given backup path (~/x) typed into the settings form",
    "app/previews.py": "shows the user's name in a path hint only",
    "app/hooks.py": "lists claude-mem's data dirs to skip in hook payloads; reads nothing",
    "app/accounts.py": "tells the default ~/.claude apart from a custom config dir by comparison",
    "app/codex_accounts.py": "the cwd of the login tmux session (the login itself runs under a temp CODEX_HOME)",
    "app/doctor.py": "looks for code-server settings and the bun binary in their usual home locations (stat only)",
    "app/agents/claude.py": "expands ~ in a path the user typed",
    "app/memory_proxy.py": "expands ~ in CLAUDE_MEM_PROJECT_ENVIRONMENTS patterns the user wrote (matching only, reads nothing)",
    "app/agents/codex.py": "the test guard: refuses to run when codex_home is the real ~/.codex under pytest",
    "scripts/claude_settings.py": "the CLI default for CLAUDE_CONFIG_DIR (tests pass the dir explicitly)",
    "scripts/codex_hooks.py": "the CLI default for CODEX_HOME (tests pass the dir explicitly)",
    "scripts/ccboard_mcp.py": "the default hook-token path of the stdio MCP shim",
    "scripts/code_server_settings.py": "the default code-server settings path (tests pass CODE_SERVER_SETTINGS)",
    "scripts/ccboard-deploy-gate": "shell: default data dir for the hook token, overridden by CCBOARD_DATA_DIR / CCBOARD_HOOK_TOKEN_FILE",
    "scripts/ccboard-watchdog.sh": "shell: default data dir, overridden by CCBOARD_DATA_DIR",
    "scripts/deploy.sh": "shell: the box's compose file location (a deploy script, never run by tests)",
    "scripts/docker-entrypoint.sh": "shell: container entrypoint; derives HOME and the default data dir inside the image",
    "scripts/qa-ui.sh": "shell: default path of the gstack browse binary (QA tooling, not the board)",
    "scripts/qa_terminal.sh": "shell: default path of the gstack browse binary (QA tooling, not the board)",
    "bin/ccboard-mem-run": "shell wrapper: claude-mem cache and data dir defaults, overridden by CLAUDE_CONFIG_DIR / CLAUDE_MEM_DATA_DIR",
    "bin/ccboard-hook-fast": "hook token default path, overridden by CCBOARD_HOOK_TOKEN_FILE",
    "bin/ccboard-statusline": "hook token default path, overridden by CCBOARD_HOOK_TOKEN_FILE",
    "bin/ccboard-permission": "hook token default path, overridden by CCBOARD_HOOK_TOKEN_FILE",
    "bin/ccboard-hook": "hook token default path, overridden by CCBOARD_HOOK_TOKEN_FILE",
}
PATTERN = re.compile(r"Path\.home\(\)|expanduser\(|environ\[[\"']HOME[\"']\]|\$HOME\b|\$\{HOME\b")


def _scan():
    hits = {}
    for d in ("app", "scripts", "bin"):
        for p in sorted((ROOT / d).rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts or "fake_tty" in p.parts or p.suffix in (".pyc", ".json"):
                continue
            try:
                text = p.read_text()
            except UnicodeDecodeError:
                continue
            if PATTERN.search(text):
                hits[p.relative_to(ROOT).as_posix()] = len(PATTERN.findall(text))
    return hits


def test_every_direct_home_access_is_in_config_or_on_the_allowlist_with_a_reason():
    hits = _scan()
    new = sorted(f for f in hits if f != "app/config.py" and f not in HOME_USE_ALLOWED)
    assert not new, f"new direct home access in {new}: route it through app/config.py or add it to HOME_USE_ALLOWED with a reason"
    stale = sorted(f for f in HOME_USE_ALLOWED if f not in hits)
    assert not stale, f"allowlisted but no longer touching the home, drop them: {stale}"
    assert all(HOME_USE_ALLOWED.values()), "every allowlist entry needs a reason"


def test_each_test_gets_a_temp_home():
    assert pathlib.Path.home() == pathlib.Path(os.environ["HOME"]) == pathlib.Path(os.environ["USERPROFILE"])
    from tests.conftest import REAL_HOME
    assert pathlib.Path.home() != REAL_HOME
    assert pathlib.Path.home().is_dir() and not any(pathlib.Path.home().iterdir())


@pytest.mark.real_home("proves the opt-out keeps the process HOME")
def test_a_marked_test_keeps_the_real_home():
    from tests.conftest import REAL_HOME
    assert pathlib.Path.home() == REAL_HOME or os.environ.get("CCBOARD_GUARD_REAL_HOME")


def test_the_snapshot_is_stat_only_and_reports_the_changed_file(tmp_path):
    from tests.conftest import home_changes, home_snapshot
    (tmp_path / ".claude").mkdir()
    before = home_snapshot(tmp_path)
    assert home_changes(before, home_snapshot(tmp_path)) == []
    (tmp_path / ".claude" / "settings.json").write_text("{}")
    assert home_changes(before, home_snapshot(tmp_path)) == [".claude/settings.json"]
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_bytes(b"\x00opaque")
    assert home_changes(before, home_snapshot(tmp_path)) == [".claude/settings.json", ".codex/auth.json"]


BAD = '''
import os, pathlib

def test_writes_under_the_real_home():
    p = pathlib.Path(os.environ["CCBOARD_GUARD_REAL_HOME"]) / ".claude" / "settings.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text("{}")
'''
GOOD = '''
import pathlib

def test_writes_under_the_sandbox_home():
    (pathlib.Path.home() / ".claude").mkdir()
    (pathlib.Path.home() / ".claude" / "settings.json").write_text("{}")
'''


def _run_guard(tmp_path, body):
    fake_real = tmp_path / "fake_real_home"
    fake_real.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    shutil.copy(ROOT / "tests" / "conftest.py", proj / "conftest.py")
    (proj / "test_probe.py").write_text(body)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CCBOARD_", "PYTEST_"))}
    env["CCBOARD_GUARD_REAL_HOME"] = str(fake_real)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(ROOT)                  # the copied conftest's autouse fixtures import app
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(proj / "test_probe.py")], cwd=proj,
                       env=env, capture_output=True, text=True, timeout=120)
    return r, fake_real


def test_a_test_that_writes_to_the_real_home_fails_the_run_and_names_the_file(tmp_path):
    r, fake_real = _run_guard(tmp_path, BAD)
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "a test touched the real home" in out and "~/.claude/settings.json" in out, out
    assert str(fake_real) not in out, "the failure must not print an absolute home path"


def test_a_test_that_stays_in_its_sandbox_passes(tmp_path):
    r, fake_real = _run_guard(tmp_path, GOOD)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (fake_real / ".claude").exists()
