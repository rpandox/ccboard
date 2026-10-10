import subprocess

from app import previews

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


def test_ports_under_tree(monkeypatch):
    tree = {100: [200], 200: [300, 301], 300: [], 301: []}
    monkeypatch.setattr(previews, "children_of", lambda pid: tree.get(pid, []))
    monkeypatch.setattr(previews, "listening", lambda pids=None: {301: {5173, 8000}, 999: {3000}})
    assert previews.ports_under(100) == [5173]      # 8000 is the board's own port, 999 is not in the tree
    assert previews.ports_under(0) == []


def test_ss_parse(monkeypatch):
    sample = ('LISTEN 0 511 127.0.0.1:5173 0.0.0.0:* users:(("node",pid=4242,fd=23))\n'
              'LISTEN 0 4096 *:3000 *:* users:(("python3",pid=77,fd=5),("python3",pid=78,fd=5))\n')
    class CP:
        stdout = sample
    monkeypatch.setattr(previews.plat, "IS_LINUX", True)
    monkeypatch.setattr(previews.shutil, "which", lambda _: "/usr/bin/ss")
    monkeypatch.setattr(previews.subprocess, "run", lambda *a, **k: CP())
    assert previews.listening() == {4242: {5173}, 77: {3000}, 78: {3000}}


def test_allocate(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "preview_https_base", 9100)
    monkeypatch.setattr(settings, "ccboard_https_port", 9101)
    assert previews.allocate_https_port({9100}) == 9102


def test_preview_routes(client, projects_dir, fake_tmux, monkeypatch):
    from app import main
    from app.config import settings
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(settings, "public_url", "https://box.ts.net:8443")
    t = client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": "ui", "prompt": "p"}).json()
    calls = []
    monkeypatch.setattr(previews, "_run_serve", lambda args: calls.append(args))
    monkeypatch.setattr(previews, "ports_under", lambda pid: [5173] if pid else [])
    fake_tmux["sessions"][t["tmux"]]["pid"] = 4242
    assert client.get(f"/api/tasks/{t['id']}/ports", headers=H).json()["ports"] == [5173]
    r = client.post(f"/api/tasks/{t['id']}/preview", headers=H, json={}).json()
    assert r == {"url": "https://box.ts.net:9100/", "https_port": 9100, "port": 5173}
    assert calls[-1] == ["--bg", "--https=9100", "http://127.0.0.1:5173"]
    st = client.get("/api/state", headers=H).json()
    assert st["tasks"][0]["preview_url"] == "https://box.ts.net:9100/"
    # second task gets the next port; explicit port accepted
    t2 = client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": "api2", "prompt": "p"}).json()
    r2 = client.post(f"/api/tasks/{t2['id']}/preview", headers=H, json={"port": 3000}).json()
    assert r2["https_port"] == 9101 and calls[-1] == ["--bg", "--https=9101", "http://127.0.0.1:3000"]
    # no port found and none given -> 409; archive turns the preview off
    monkeypatch.setattr(previews, "ports_under", lambda pid: [])
    t3 = client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": "api3", "prompt": "p"}).json()
    assert client.post(f"/api/tasks/{t3['id']}/preview", headers=H, json={}).status_code == 409
    client.post(f"/api/tasks/{t['id']}/archive", headers=H, json={"force": True})
    assert calls[-1] == ["--https=9100", "--yes", "off"]
    assert client.delete(f"/api/tasks/{t2['id']}/preview", headers=H).status_code == 200 and calls[-1] == ["--https=9101", "--yes", "off"]


def test_preview_route_docker_without_operator(lite_client, projects_dir, fake_tmux, monkeypatch, tmp_path):
    """Inside the container a denied `tailscale serve` is answered with the one-time fix and never retried through sudo."""
    from types import SimpleNamespace
    from app.config import settings
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(settings, "public_url", "https://box.ts.net:8443")
    monkeypatch.setattr(settings, "runtime", "docker")
    sock = tmp_path / "tailscaled.sock"
    sock.touch()
    monkeypatch.setattr(previews, "TAILSCALE_SOCK", sock)
    monkeypatch.setenv("HOME", "/home/example")
    calls, real_run = [], subprocess.run

    def fake_run(argv, **kw):       # previews shares the subprocess module with git: only tailscale/sudo are faked
        if argv[0] not in ("sudo", "tailscale") and not argv[0].endswith("/tailscale"):
            return real_run(argv, **kw)
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout="", stderr="Access denied: serve config denied")
    monkeypatch.setattr(previews.subprocess, "run", fake_run)
    monkeypatch.setattr(previews.shutil, "which", lambda x: f"/usr/bin/{x}")
    t = lite_client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": "ui", "prompt": "p"}).json()
    r = lite_client.post(f"/api/tasks/{t['id']}/preview", headers=H, json={"port": 5173})
    assert r.status_code == 422
    assert r.json()["error"] == "tailscale serve failed; on the box run: sudo tailscale set --operator=example"
    assert len(calls) == 1 and "sudo" not in calls[0]


# ---- #126: capability(), what state.preview tells the Preview control before any click

import pytest  # noqa: E402

from app import nodes, tailscale as ts  # noqa: E402
from app.config import settings  # noqa: E402


@pytest.fixture
def cap(monkeypatch, tmp_path):
    """A Linux box with a command, a public url and a signed-in Tailscale reading (the seam nodes._ts), each piece changeable by a test."""
    exe = tmp_path / "tailscale"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(settings, "public_url", "https://box.example.ts.net:8443")
    monkeypatch.setattr(ts, "find_cli", lambda: ts.Cli([str(exe)], {}))
    monkeypatch.setattr(nodes, "windows_side", lambda: False)
    reading = {"d": {"BackendState": "Running"}}
    monkeypatch.setattr(nodes, "_ts", lambda: reading["d"])
    previews._refused.update(at=0.0, why="")
    yield type("Cap", (), {"reading": reading, "exe": exe})
    previews._refused.update(at=0.0, why="")


def test_capability_is_available_when_everything_is_in_place(cap):
    assert previews.capability() == {"available": True, "code": "ok", "reason": None}


@pytest.mark.parametrize("what, code, words", [
    ("no_url", "no_public_url", "CCBOARD_PUBLIC_URL is not set"),
    ("wsl", "wsl_host", "Windows side"),
    ("no_cli", "no_cli", "tailscale"),
    ("down", "not_running", "not running or cannot be reached"),
    ("logged_out", "not_signed_in", "not logged in"),
])
def test_capability_names_each_reason(cap, monkeypatch, what, code, words):
    if what == "no_url":
        monkeypatch.setattr(settings, "public_url", "")
    elif what == "wsl":
        monkeypatch.setattr(nodes, "windows_side", lambda: True)
    elif what == "no_cli":
        monkeypatch.setattr(ts, "find_cli", lambda: None)
    elif what == "down":
        cap.reading["d"] = None
    else:
        cap.reading["d"] = {"BackendState": "NeedsLogin"}
    got = previews.capability()
    assert got["available"] is False and got["code"] == code and words in got["reason"], got


def test_capability_no_cli_also_when_linux_names_a_command_that_is_not_there(cap, monkeypatch, tmp_path):
    """On Linux find_cli() always answers (the fallback path); a command that does not exist is still 'no CLI'."""
    monkeypatch.setattr(ts, "find_cli", lambda: ts.Cli([str(tmp_path / "nowhere" / "tailscale")], {}))
    monkeypatch.setattr(previews.shutil, "which", lambda _: None)
    assert previews.capability()["code"] == "no_cli"


def test_a_refused_serve_turns_the_control_off_until_it_expires_or_a_serve_works(cap, monkeypatch):
    def refuse(args, sock=None):
        raise ts.TailscaleError("tailscale serve failed; on the box run: sudo tailscale set --operator=alice")
    monkeypatch.setattr(ts, "run_serve", refuse)
    with pytest.raises(previews.PreviewError):
        previews.serve_on(9100, 5173)
    got = previews.capability()
    assert got["available"] is False and got["code"] == "denied" and "--operator=alice" in got["reason"]
    # a port or network failure is not a denial
    previews._refused.update(at=0.0, why="")
    monkeypatch.setattr(ts, "run_serve", lambda args, sock=None: (_ for _ in ()).throw(ts.TailscaleError("port 9100 is in use")))
    with pytest.raises(previews.PreviewError):
        previews.serve_on(9100, 5173)
    assert previews.capability()["available"] is True
    # it expires, so one more try is possible (nothing else could clear it while the button is off)
    previews._refused.update(at=previews.time.monotonic() - previews.DENIED_TTL - 1, why="denied once")
    assert previews.capability()["available"] is True
    # a serve that works clears it at once
    previews._refused.update(at=previews.time.monotonic(), why="denied once")
    monkeypatch.setattr(ts, "run_serve", lambda args, sock=None: None)
    previews.serve_on(9100, 5173)
    assert previews.capability()["available"] is True


def test_capability_runs_nothing_and_never_raises(cap, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("ran a process")
    monkeypatch.setattr(previews.subprocess, "run", boom)
    monkeypatch.setattr(ts, "_exec", boom)
    assert previews.capability()["available"] is True
    monkeypatch.setattr(nodes, "_ts", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert previews.capability() == {"available": True, "code": "unknown", "reason": None}


def test_state_carries_the_preview_capability(client, monkeypatch):
    monkeypatch.setattr(settings, "public_url", "")
    st = client.get("/api/state", headers=H).json()
    assert st["preview"]["available"] is False and st["preview"]["code"] == "no_public_url"
    assert "CCBOARD_PUBLIC_URL" in st["preview"]["reason"]


def test_variant_reads_the_file_layout_only_and_runs_no_command(monkeypatch):
    """#126: variant() does not read `tailscale version` (dropped from the scope: what it prints per variant is not measured, and a command per
    poll would cost more than it tells); it must never start a process."""
    def boom(*a, **k):
        raise AssertionError("variant ran a process")
    monkeypatch.setattr(ts.subprocess, "run", boom)
    for kind in ("IS_MACOS", "IS_LINUX", "IS_WINDOWS"):
        for other in ("IS_MACOS", "IS_LINUX", "IS_WINDOWS"):
            monkeypatch.setattr(ts.plat, other, other == kind)
        assert isinstance(ts.variant(ts.Cli(["/x/tailscale"], {})), str)
