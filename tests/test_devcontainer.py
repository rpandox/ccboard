"""Devcontainer sessions in docker mode (issue #104, box check of 2026-10-09).

What the check found: the line the board types, `devcontainer up ... && devcontainer exec ... -- claude ...`, goes into the HOST's tmux, so it
runs on the host. The limit is therefore the host's docker, docker group and `devcontainer` CLI, not a docker socket in the board's container
(the board's process never calls docker). The exact typed line is pinned in tests/test_api_v054.py (both launchers through the API) and here
(the adapter, with a path that needs quoting); this file pins the doctor check and the wording of the docs.

Nothing here runs docker, the devcontainer CLI, claude or tmux: the probes are patched, and ~/.local/bin is a temp directory.
"""
import re
import shlex
from pathlib import Path

import pytest

from app import agents, doctor
from app.agents.base import LaunchReq
from app.config import settings

ROOT = Path(__file__).resolve().parent.parent
SID = "11111111-2222-4333-8444-555555555555"


def scan(*repos):
    return [{"name": "shop", "repos": [{"name": name, "devcontainer": has} for name, has in repos]}]


# ------------------------------------------------------------------ the typed line

def test_the_adapter_types_up_then_exec_on_the_host_with_the_path_quoted(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "claude_bin", lambda: None)
    repo = tmp_path / "my repo"
    (repo / ".devcontainer").mkdir(parents=True)
    (repo / ".devcontainer" / "devcontainer.json").write_text("{}")
    p = agents.get("claude").launch_plan(LaunchReq(kind="new", session_name="s1", cwd=str(repo), session_id=SID, opts={"devcontainer": True}))
    wf = shlex.quote(str(repo))
    assert p.cmd_line == (f"devcontainer up --workspace-folder {wf} && devcontainer exec --workspace-folder {wf} -- "
                          f"claude --session-id {SID} --name s1")
    assert shlex.split(p.cmd_line)[:2] == ["devcontainer", "up"], "a plain command line for a shell to run: no docker call in the board's process"


# ------------------------------------------------------------------ the doctor check

@pytest.fixture
def box(monkeypatch, tmp_path):
    """A board on the host (systemd), one repo with a devcontainer, both probes healthy; tests break one piece at a time."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(settings, "runtime", "systemd")
    monkeypatch.setattr(doctor, "_projects_source", lambda: scan(("api", True), ("web", False)))
    monkeypatch.setattr(doctor, "_hook_path", lambda: "/nonexistent-bin")
    monkeypatch.setattr(doctor.plat, "_hint_family", lambda: "linux")
    probes = {"docker": "ok", "cli": "ok", "cli_path": None}
    monkeypatch.setattr(doctor, "_probe_docker", lambda: probes["docker"])

    def helper(name, tail, path):
        probes["cli_path"] = (name, tail, path)
        return probes["cli"]
    monkeypatch.setattr(doctor, "_probe_helper", helper)
    return probes


def run():
    out = doctor._c_devcontainer(None)
    assert out.status in ("pass", "warn", "skip") and "\n" not in out.detail
    assert len(out.detail) <= doctor.DETAIL_MAX and len(doctor._clean(out.detail)) == len(out.detail), "one short line, nothing cut off"
    return out


def test_it_is_a_registered_box_check():
    assert [(c[1], c[2]) for c in doctor.CHECKS if c[0] == "devcontainer"] == [("box", "Devcontainer prerequisites (host)")]
    assert {c[0]: c[3] for c in doctor.CHECKS}["devcontainer"] is doctor._c_devcontainer


def test_it_skips_with_a_reason_when_nothing_asks_for_a_devcontainer(box, monkeypatch):
    monkeypatch.setattr(doctor, "_projects_source", lambda: scan(("api", False)))
    out = run()
    assert out.status == "skip" and "no repo has a .devcontainer" in out.detail
    monkeypatch.setattr(doctor, "_projects_source", lambda: None)
    out = run()
    assert out.status == "skip" and "scan is not ready" in out.detail
    monkeypatch.setattr(doctor, "_projects_source", None)
    assert run().status == "skip"


def test_on_the_host_it_asks_docker_and_the_cli_directly(box, tmp_path):
    out = run()
    assert out.status == "pass" and out.fix is None and "1 repo with a devcontainer" in out.detail
    name, tail, path = box["cli_path"]
    assert (name, tail) == ("devcontainer", ["--version"]) and path.endswith(str(tmp_path / ".local" / "bin")), "~/.local/bin is where install.sh puts it"


def test_on_the_host_a_missing_cli_is_a_warning_with_the_install_line(box):
    box["cli"] = "missing"
    out = run()
    assert out.status == "warn" and "devcontainer CLI is not installed" in out.detail and "docker" not in out.detail.split("but", 1)[1].split("CLI")[0]
    assert "@devcontainers/cli" in out.fix["text"] and "CCBOARD_DEVCONTAINER=1" in out.fix["text"] and "--prefix ~/.local" in out.fix["cmd"]
    box["cli"] = "broken"
    assert "does not run (it needs node)" in run().detail


@pytest.mark.parametrize("docker,words,fix_has", [
    ("missing", "docker is not installed", "apt-get install -y docker.io"),
    ("denied", "docker did not answer for this user", "usermod -aG docker"),
])
def test_on_the_host_docker_problems_are_named_with_their_fix(box, docker, words, fix_has):
    box["docker"] = docker
    out = run()
    assert out.status == "warn" and words in out.detail and fix_has in out.fix["cmd"]
    box["cli"] = "missing"                                            # both at once: both are named, the docker fix comes first
    out = run()
    assert words in out.detail and "devcontainer CLI is not installed" in out.detail and fix_has in out.fix["cmd"]


def test_on_the_host_a_slow_probe_reads_as_unknown_not_as_broken(box):
    box["docker"], box["cli"] = "timeout", "timeout"
    out = run()
    assert out.status == "warn" and out.detail.startswith("unknown: docker info and devcontainer --version did not answer")


@pytest.fixture
def in_container(box, monkeypatch, tmp_path):
    """The board runs in the container: it cannot ask the host's docker, and must not try (no docker binary there)."""
    monkeypatch.setattr(settings, "runtime", "docker")

    def boom(*a, **kw):
        raise AssertionError("in a container the check must not run docker or the devcontainer CLI")
    monkeypatch.setattr(doctor, "_probe_docker", boom)
    monkeypatch.setattr(doctor, "_probe_helper", boom)
    monkeypatch.setattr(doctor, "_run", boom)
    return tmp_path / ".local" / "bin"


def test_in_a_container_it_says_what_it_cannot_see_and_looks_where_the_installer_puts_the_cli(in_container):
    out = run()
    assert out.status == "warn" and "no devcontainer CLI in ~/.local/bin" in out.detail and "cannot be seen from the container" in out.detail
    assert "CCBOARD_DEVCONTAINER=1 ./install.sh" in out.fix["text"] and "ignore this if it is installed elsewhere" in out.fix["text"]
    in_container.mkdir(parents=True)
    cli = in_container / "devcontainer"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o644)
    assert run().status == "warn", "present but not executable"
    cli.chmod(0o755)
    out = run()
    assert out.status == "pass" and "the host's tmux" in out.detail and "out of sight" in out.detail


def test_in_a_container_with_no_devcontainer_repo_it_still_skips(in_container, monkeypatch):
    monkeypatch.setattr(doctor, "_projects_source", lambda: scan(("api", False)))
    assert run().status == "skip"


# ------------------------------------------------------------------ the wording of the docs

def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_the_old_claim_is_gone_and_the_host_prerequisites_are_stated():
    old = ("socket inside the board's container", "docker CLI and socket inside", "they stay a systemd-mode feature")
    for rel in ("README.md", "ROADMAP.md", "install.sh"):
        for needle in old:
            assert needle not in read(rel), f"{rel}: {needle!r} was wrong about the cause (box check, issue #104)"
    readme = read("README.md")
    assert "Devcontainer sessions work in docker mode; what they need is on the host" in readme
    assert "devcontainer up --workspace-folder <repo> && devcontainer exec --workspace-folder <repo> -- claude" in readme
    assert "`docker` group" in readme and "the `devcontainer` CLI" in readme and "tests/test_devcontainer.py" in readme
    assert "no docker CLI and no docker socket" in readme, "the socket stays out of the board's container"
    assert "host's tmux" in read("ROADMAP.md") and "host's docker" in read("install.sh")
    # the install step itself is unchanged and works in docker mode: npm --prefix ~/.local, no sudo
    assert re.search(r'npm install -g --prefix "\$HOME_DIR/\.local" @devcontainers/cli', read("install.sh"))
