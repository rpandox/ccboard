from app import health

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


def test_snapshot_graceful():
    s = health.snapshot()
    assert "host" in s and "at" in s and s["cores"]
    assert s["disk"] is None or s["disk"]["pct"] >= 0


def test_parse_nodes():
    assert health.parse_nodes("ubu=https://ubu.ts.net:8443, box2=https://box2.ts.net/, bad, http=http://x") == [
        {"name": "ubu", "url": "https://ubu.ts.net:8443"}, {"name": "box2", "url": "https://box2.ts.net"}]


def test_node_summary_and_hub(lite_client, projects_dir, fake_tmux, monkeypatch):
    from app import main
    from app.config import settings
    monkeypatch.setattr(settings, "hub_token", "secret-token")
    monkeypatch.setattr(settings, "node_name", "ubu2")
    assert lite_client.get("/api/node/summary").status_code == 403
    assert lite_client.get("/api/node/summary", headers={"X-CCBoard-Hub": "wrong"}).status_code == 403
    s = lite_client.get("/api/node/summary", headers={"X-CCBoard-Hub": "secret-token"}).json()
    assert s["node"] == "ubu2" and "health" in s and s["sessions"] == 0
    assert lite_client.get("/api/health", headers=H).json()["node"] == "ubu2"
    st = lite_client.get("/api/state", headers=H).json()
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
    assert lite_client.get("/api/state", headers=H).json()["nodes"]["value"][0]["name"] == "ubu"
    monkeypatch.setattr(settings, "hub_token", "")
    assert lite_client.get("/api/node/summary", headers={"X-CCBoard-Hub": "secret-token"}).status_code == 403   # no token configured -> closed


# ---------------------------------------------------------------- #31: under_load() and the passes that back off with it

REAL_UNDER_LOAD = health.under_load      # conftest's autouse _quiet_box patches the module attribute; this is the real one


def _load(monkeypatch, load1, cores=4):
    monkeypatch.setattr(health.os, "getloadavg", lambda: (load1, 0.0, 0.0))
    monkeypatch.setattr(health.os, "cpu_count", lambda: cores)
    health.load_cache_clear()


def test_under_load_is_load1_over_the_cpu_count(monkeypatch):
    _load(monkeypatch, 4.5)
    assert REAL_UNDER_LOAD() is True
    _load(monkeypatch, 4.0)
    assert REAL_UNDER_LOAD() is False, "equal to the core count is not over it"
    _load(monkeypatch, 0.7)
    assert REAL_UNDER_LOAD() is False
    health.load_cache_clear()


def test_under_load_reads_the_load_at_most_every_5_seconds(monkeypatch):
    clock = [1000.0]
    reads = []
    monkeypatch.setattr(health.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(health.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(health.os, "getloadavg", lambda: reads.append(1) or (9.0, 0.0, 0.0))
    health.load_cache_clear()
    assert REAL_UNDER_LOAD() is True and len(reads) == 1
    monkeypatch.setattr(health.os, "getloadavg", lambda: reads.append(1) or (0.1, 0.0, 0.0))
    clock[0] += 4.9
    assert REAL_UNDER_LOAD() is True and len(reads) == 1, "cached: no second read inside 5 s"
    clock[0] += 0.2
    assert REAL_UNDER_LOAD() is False and len(reads) == 2, "read again after 5 s"
    health.load_cache_clear()


def test_under_load_never_raises_and_an_unreadable_load_is_not_busy(monkeypatch):
    def boom():
        raise OSError("no load average here")
    monkeypatch.setattr(health.os, "getloadavg", boom)
    health.load_cache_clear()
    assert REAL_UNDER_LOAD() is False
    monkeypatch.delattr(health.os, "getloadavg")
    health.load_cache_clear()
    assert REAL_UNDER_LOAD() is False
    monkeypatch.setattr(health.os, "getloadavg", lambda: (99.0, 0.0, 0.0), raising=False)
    monkeypatch.setattr(health.os, "cpu_count", lambda: None)
    health.load_cache_clear()
    assert REAL_UNDER_LOAD() is False, "no cpu count: not busy"
    health.load_cache_clear()


def test_cost_refresh_skips_while_busy_and_catches_up_on_the_next_quiet_tick(monkeypatch):
    from app import cost, usage
    busy = [True]
    refreshed = []
    monkeypatch.setattr(health, "under_load", lambda: busy[0])
    monkeypatch.setattr(cost, "refresh", lambda db: refreshed.append(1))
    kv = {}

    class FakeDB:
        def kv_set(self, k, v):
            kv[k] = v
    p = usage.Poller(FakeDB())
    p.tick()                                         # tick 0 is a cost tick: skipped, owed
    assert refreshed == [] and p.cost_owed and usage.KV_BLOCK in kv, "the ccusage block itself is never skipped"
    p.tick()                                         # still busy: still owed
    assert refreshed == []
    busy[0] = False
    p.tick()                                         # the first quiet tick catches up, not five ticks later
    assert refreshed == [1] and not p.cost_owed
    p.tick(); p.tick()
    assert refreshed == [1], "then back to every COST_EVERY ticks"
    p.n = usage.COST_EVERY * 10
    p.tick()
    assert refreshed == [1, 1]
    busy[0] = True                                   # a box that stays busy still gets a cost figure after COST_MAX_SKIPS
    p.n = usage.COST_EVERY * 20
    for _ in range(usage.COST_MAX_SKIPS):
        p.tick()
    assert refreshed == [1, 1]
    p.tick()
    assert refreshed == [1, 1, 1]


def test_the_indexer_skips_a_pass_while_busy_and_resumes(monkeypatch, tmp_path):
    from app import search
    busy = [True]
    passes = []
    monkeypatch.setattr(health, "under_load", lambda: busy[0])
    idx = search.Indexer.__new__(search.Indexer)     # no DB needed: only tick()'s rule is under test
    idx.skips = 0
    idx.index_once = lambda: passes.append(1) or 3
    assert idx.tick() is None and passes == []
    busy[0] = False
    assert idx.tick() == 3 and passes == [1], "the next quiet pass runs (it resumes from the stored offsets)"
    busy[0] = True
    for _ in range(search.MAX_SKIPS):
        assert idx.tick() is None
    assert idx.tick() == 3 and passes == [1, 1], "a box that stays busy still gets one pass after MAX_SKIPS"
