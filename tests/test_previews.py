import subprocess

from app import previews

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


def test_ports_under_tree(monkeypatch):
    tree = {100: [200], 200: [300, 301], 300: [], 301: []}
    monkeypatch.setattr(previews, "children_of", lambda pid: tree.get(pid, []))
    monkeypatch.setattr(previews, "listening", lambda: {301: {5173, 8000}, 999: {3000}})
    assert previews.ports_under(100) == [5173]      # 8000 is the board's own port, 999 is not in the tree
    assert previews.ports_under(0) == []


def test_ss_parse(monkeypatch):
    sample = ('LISTEN 0 511 127.0.0.1:5173 0.0.0.0:* users:(("node",pid=4242,fd=23))\n'
              'LISTEN 0 4096 *:3000 *:* users:(("python3",pid=77,fd=5),("python3",pid=78,fd=5))\n')
    class CP:
        stdout = sample
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
