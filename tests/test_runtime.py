"""Runtime detection (docker / systemd / host), what it switches off, and the container-only branches."""
import logging
import subprocess
from types import SimpleNamespace

import pytest

from app import backup, previews
from app.config import Settings, settings

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


def test_runtime_detection():
    assert Settings({"CCBOARD_RUNTIME": "docker"}).runtime == "docker"
    assert Settings({"CCBOARD_RUNTIME": " Docker "}).runtime == "docker"
    assert Settings({"INVOCATION_ID": "abc"}).runtime == "systemd"
    assert Settings({}).runtime == "host"
    # the explicit value wins over the systemd marker; a typo never becomes a runtime of its own
    assert Settings({"CCBOARD_RUNTIME": "docker", "INVOCATION_ID": "abc"}).runtime == "docker"
    assert Settings({"CCBOARD_RUNTIME": "podman"}).runtime == "host"
    assert Settings({"CCBOARD_RUNTIME": "podman", "INVOCATION_ID": "abc"}).runtime == "systemd"


def test_dev_bypass_only_on_a_dev_host():
    assert Settings({"CCBOARD_RUNTIME": "docker", "CCBOARD_DEV_BYPASS_USER": "x"}).dev_bypass_user is None
    assert Settings({"INVOCATION_ID": "abc", "CCBOARD_DEV_BYPASS_USER": "x"}).dev_bypass_user is None
    assert Settings({"CCBOARD_RUNTIME": "host", "INVOCATION_ID": "abc", "CCBOARD_DEV_BYPASS_USER": "x"}).dev_bypass_user is None
    assert Settings({"CCBOARD_DEV_BYPASS_USER": "x"}).dev_bypass_user == "x"
    assert Settings({"CCBOARD_RUNTIME": "host", "CCBOARD_DEV_BYPASS_USER": "x"}).dev_bypass_user == "x"


def test_image_version():
    assert Settings({}).image_version == ""
    assert Settings({"CCBOARD_IMAGE_VERSION": " v0.5.2 "}).image_version == "v0.5.2"


def test_state_config_runtime(lite_client, monkeypatch):
    assert lite_client.get("/api/state", headers=H).json()["config"]["runtime"] == settings.runtime
    monkeypatch.setattr(settings, "runtime", "docker")
    assert lite_client.get("/api/state", headers=H).json()["config"]["runtime"] == "docker"


def test_startup_logs_runtime_and_image(projects_dir, monkeypatch, caplog):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(settings, "runtime", "docker")
    monkeypatch.setattr(settings, "image_version", "v9.9.9")
    caplog.set_level(logging.INFO, logger="ccboard")
    with TestClient(main.app):
        pass
    assert "runtime docker, image v9.9.9" in caplog.text


@pytest.fixture
def docker_serve(tmp_path, monkeypatch):
    """settings.runtime == 'docker', a (fake) tailscaled socket present, subprocess.run recorded; returns (calls, set_result)."""
    sock = tmp_path / "tailscaled.sock"
    sock.touch()
    monkeypatch.setattr(previews, "TAILSCALE_SOCK", sock)
    monkeypatch.setattr(settings, "runtime", "docker")
    monkeypatch.setenv("HOME", "/home/example")
    monkeypatch.setattr(previews.shutil, "which", lambda x: f"/usr/bin/{x}")
    calls, result = [], {"cp": SimpleNamespace(returncode=0, stdout="", stderr="")}
    monkeypatch.setattr(previews.subprocess, "run", lambda argv, **kw: calls.append(argv) or result["cp"])
    return calls, result


def test_docker_serve_never_uses_sudo(docker_serve):
    calls, result = docker_serve
    previews.serve_on(9100, 5173)
    assert calls == [["/usr/bin/tailscale", "serve", "--bg", "--https=9100", "http://127.0.0.1:5173"]]
    result["cp"] = SimpleNamespace(returncode=1, stdout="", stderr="Access denied: serve config denied\nUse 'sudo tailscale serve'. To not require root, use 'sudo tailscale set --operator=$USER' once.")
    calls.clear()
    with pytest.raises(previews.PreviewError) as ei:
        previews.serve_on(9100, 5173)
    assert str(ei.value) == "tailscale serve failed; on the box run: sudo tailscale set --operator=example"
    assert len(calls) == 1 and "sudo" not in calls[0]        # one attempt as the operator, no sudo fallback
    calls.clear()
    result["cp"] = SimpleNamespace(returncode=1, stdout="", stderr="open /var/run/tailscale/tailscaled.sock: permission denied")
    with pytest.raises(previews.PreviewError, match="--operator=example"):
        previews._run_serve(["--https=9100", "--yes", "off"])
    assert all("sudo" not in c for c in calls)


def test_docker_serve_other_failures_pass_through(docker_serve):
    calls, result = docker_serve
    result["cp"] = SimpleNamespace(returncode=1, stdout="", stderr="Serve is not enabled on your tailnet.")
    with pytest.raises(previews.PreviewError, match="Serve is not enabled"):
        previews.serve_on(9100, 5173)
    assert len(calls) == 1 and "sudo" not in calls[0]


def test_docker_serve_without_socket(docker_serve, tmp_path, monkeypatch):
    calls, _ = docker_serve
    monkeypatch.setattr(previews, "TAILSCALE_SOCK", tmp_path / "missing.sock")
    with pytest.raises(previews.PreviewError, match="socket .* is not mounted"):
        previews.serve_on(9100, 5173)
    assert calls == []


@pytest.mark.parametrize("runtime", ["host", "systemd"])
def test_serve_keeps_the_sudo_fallback_outside_docker(monkeypatch, runtime):
    monkeypatch.setattr(settings, "runtime", runtime)
    monkeypatch.setattr(previews.shutil, "which", lambda x: f"/usr/bin/{x}")
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=1 if argv[0] != "sudo" else 0, stdout="", stderr="Access denied")
    monkeypatch.setattr(previews.subprocess, "run", fake_run)
    previews.serve_on(9100, 5173)
    assert calls[1][:3] == ["sudo", "-n", "/usr/bin/tailscale"]


def test_backup_start_detached_in_docker_skips_systemd(tmp_path, monkeypatch, projects_dir):
    unit = tmp_path / "ccboard-backup.service"
    unit.write_text("[Unit]\n")
    monkeypatch.setattr(backup, "SYSTEMD_UNIT", unit)
    monkeypatch.setattr(backup.shutil, "which", lambda x: f"/usr/bin/{x}")
    monkeypatch.setattr(settings, "runtime", "docker")
    ran, spawned = [], []
    monkeypatch.setattr(backup.subprocess, "run", lambda argv, **kw: ran.append(argv))
    monkeypatch.setattr(backup.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    assert backup.start_detached() == "process"
    assert ran == [] and spawned[0][-2:] == ["-m", "app.backup"]
