from app import health

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


def test_snapshot_graceful():
    s = health.snapshot()
    assert "host" in s and "at" in s and s["cores"]
    assert s["disk"] is None or s["disk"]["pct"] >= 0


def test_parse_nodes():
    assert health.parse_nodes("ubu=https://ubu.ts.net:8443, box2=https://box2.ts.net/, bad, http=http://x") == [
        {"name": "ubu", "url": "https://ubu.ts.net:8443"}, {"name": "box2", "url": "https://box2.ts.net"}]


def test_node_summary_and_hub(client, projects_dir, fake_tmux, monkeypatch):
    from app import main
    from app.config import settings
    monkeypatch.setattr(settings, "hub_token", "secret-token")
    monkeypatch.setattr(settings, "node_name", "ubu2")
    assert client.get("/api/node/summary").status_code == 403
    assert client.get("/api/node/summary", headers={"X-CCBoard-Hub": "wrong"}).status_code == 403
    s = client.get("/api/node/summary", headers={"X-CCBoard-Hub": "secret-token"}).json()
    assert s["node"] == "ubu2" and "health" in s and s["sessions"] == 0
    assert client.get("/api/health", headers=H).json()["node"] == "ubu2"
    st = client.get("/api/state", headers=H).json()
    assert st["node_name"] == "ubu2" and st["health"]["host"]
    # hub poller with a fake fetch
    fetched = []
    def fake_fetch(url):
        fetched.append(url)
        if "down" in url:
            raise OSError("connection refused")
        return {"node": "ubu", "health": {"cpu_pct": 12.0}, "sessions": 3, "attention": 1}
    p = health.Poller(main.db, health.parse_nodes("ubu=https://ubu.ts.net:8443,down=https://down.ts.net"), fetch=fake_fetch)
    out = p.poll_once()
    assert out[0]["online"] and out[0]["attention"] == 1 and not out[1]["online"] and "refused" in out[1]["error"]
    assert client.get("/api/state", headers=H).json()["nodes"]["value"][0]["name"] == "ubu"
    monkeypatch.setattr(settings, "hub_token", "")
    assert client.get("/api/node/summary", headers={"X-CCBoard-Hub": "secret-token"}).status_code == 403   # no token configured -> closed
