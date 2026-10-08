"""#97: the Doctor's `codex-update-path` check (agents/codex.update_path_check): where Codex is installed and whether its own update prompt
(`npm install -g` against npm's global prefix) can work. Temp prefixes only; the check never runs npm or any process (asserted) and reads
nothing of an npmrc but its `prefix` key (asserted with a token line beside it).
"""
import os
import stat
from pathlib import Path

import pytest

from app import agents
from app.agents import codex
from app.agents.base import Check


def npm_install(prefix: Path) -> Path:
    """An npm global install of Codex under `prefix`: lib/node_modules/@openai/codex/bin/codex.js and the bin/codex symlink to it."""
    pkg = prefix / "lib" / "node_modules" / "@openai" / "codex" / "bin"
    pkg.mkdir(parents=True)
    (pkg / "codex.js").write_text("#!/usr/bin/env node\n")
    (prefix / "bin").mkdir(exist_ok=True)
    link = prefix / "bin" / "codex"
    link.symlink_to(pkg / "codex.js")
    return link


def node_at(prefix: Path) -> str:
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    n = prefix / "bin" / "node"
    n.write_text("#!/bin/sh\n")
    return str(n)


# os.access says what the BOARD's user may write; root may write a 0500 directory, so the read-only cases mean nothing there
ROOT_SKIP = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can write a read-only directory")


def read_only(*dirs: Path):
    for d in dirs:
        os.chmod(d, stat.S_IRUSR | stat.S_IXUSR)


@pytest.fixture
def no_process(monkeypatch):
    """Any process the check would start fails the test (npm above all)."""
    import subprocess
    def boom(*a, **k):
        raise AssertionError(f"the update-path check started a process: {a!r}")
    monkeypatch.setattr(codex, "_run", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


@pytest.fixture
def restore_modes(tmp_path):
    yield
    for p in tmp_path.rglob("*"):
        if p.is_dir() and not p.is_symlink():
            os.chmod(p, 0o755)


def test_writable_npm_install_whose_prefix_npm_uses_passes(tmp_path, no_process):
    local = tmp_path / "home" / ".local"
    exe = npm_install(local)
    home = tmp_path / "home"
    (home / ".npmrc").write_text("//registry.npmjs.org/:_authToken=npm_SECRETTOKEN\nprefix=~/.local\n")
    c = codex.update_path_check(str(exe), env={}, home=home, node=None)
    assert isinstance(c, Check) and c.id == "codex-update-path" and c.group == "codex"
    assert c.status == "pass", c.detail
    assert str(local) in c.detail and "writable" in c.detail and "npm_SECRETTOKEN" not in c.detail and "_authToken" not in c.detail


@ROOT_SKIP
def test_a_read_only_install_prefix_warns_with_the_safe_command(tmp_path, no_process, restore_modes):
    usr = tmp_path / "usr"
    exe = npm_install(usr)
    read_only(usr / "lib" / "node_modules", usr / "bin")
    c = codex.update_path_check(str(exe), env={}, home=tmp_path / "home", node=None)
    assert c.status == "warn" and "not writable" in c.detail and "will fail" in c.detail
    assert c.fix == {"text": "Update Codex with `npm install -g --prefix ~/.local @openai/codex@latest`. Do not accept Codex's own update prompt.",
                     "cmd": "npm install -g --prefix ~/.local @openai/codex@latest"}


@ROOT_SKIP
def test_the_box_trap_codex_in_local_but_npm_defaults_to_a_system_prefix(tmp_path, no_process, restore_modes):
    """The 2026-10-04 accident: Codex lives in ~/.local (writable), npm's default prefix is node's (/usr, read-only): the self-update fails."""
    home = tmp_path / "home"
    exe = npm_install(home / ".local")
    usr = tmp_path / "usr"
    node = node_at(usr)
    (usr / "lib" / "node_modules").mkdir(parents=True)
    read_only(usr / "lib" / "node_modules", usr / "bin")
    c = codex.update_path_check(str(exe), env={}, home=home, node=node)
    assert c.status == "warn" and str(usr) in c.detail and "the default beside node" in c.detail and "will fail" in c.detail
    assert c.fix["cmd"] == codex.SAFE_UPDATE_CMD


def test_npm_prefix_elsewhere_but_writable_warns_that_an_update_lands_elsewhere(tmp_path, no_process):
    home = tmp_path / "home"
    exe = npm_install(home / ".local")
    other = tmp_path / "nvm" / "v22"
    other.mkdir(parents=True)
    c = codex.update_path_check(str(exe), env={"NPM_CONFIG_PREFIX": str(other)}, home=home, node=None)
    assert c.status == "warn" and "second copy" in c.detail and "NPM_CONFIG_PREFIX" in c.detail


def test_missing_binary_skips_and_an_unlearnable_npm_prefix_is_unknown_not_healthy(tmp_path, no_process):
    assert codex.update_path_check(None).status == "skip"
    home = tmp_path / "home"
    exe = npm_install(home / ".local")
    c = codex.update_path_check(str(exe), env={}, home=home, node=None)
    assert c.status == "skip" and "unknown" in c.detail and c.fix["cmd"] == codex.SAFE_UPDATE_CMD


def test_npmrc_prefix_reads_the_prefix_key_only(tmp_path):
    rc = tmp_path / ".npmrc"
    rc.write_text("; comment\n//registry.npmjs.org/:_authToken=npm_SECRET\nemail=a@b.c\n  prefix = \"${HOME}/.local\" \n")
    assert codex._npmrc_prefix(rc, tmp_path) == f"{tmp_path}/.local"
    rc.write_text("//registry.npmjs.org/:_authToken=npm_SECRET\n")
    assert codex._npmrc_prefix(rc, tmp_path) is None
    assert codex._npmrc_prefix(tmp_path / "missing", tmp_path) is None


def test_a_standalone_binary_is_judged_by_its_own_directory(tmp_path, no_process):
    d = tmp_path / "opt" / "codex" / "bin"
    d.mkdir(parents=True)
    exe = d / "codex"
    exe.write_text("x")
    c = codex.update_path_check(str(exe), env={"NPM_CONFIG_PREFIX": str(tmp_path / "opt" / "codex")}, home=tmp_path, node=None)
    assert "not an npm install" in c.detail and c.status == "pass"


def test_the_doctor_group_carries_the_row_and_no_remedy_says_codex_update(tmp_path, monkeypatch, no_process):
    monkeypatch.setattr(codex.settings, "codex_bin", lambda: None)
    ids = [c.id for c in agents.get("codex").doctor_checks()]
    assert ids[-1] == "codex-update-path"
    src = Path(codex.__file__).read_text()
    assert '"cmd": "codex update"' not in src, "Codex's own update is the trap: no remedy may offer it"


def test_no_unprefixed_global_install_is_suggested_anywhere():
    root = Path(__file__).resolve().parent.parent
    hits = []
    for p in [*(root / "app").rglob("*.py"), *(root / "app" / "static").rglob("*.js"), root / "README.md"]:
        if "vendor" in p.parts:
            continue
        for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if "npm install -g @openai/codex" in line or "npm i -g @openai/codex" in line:
                hits.append(f"{p.name}:{i}")
    assert hits == []
