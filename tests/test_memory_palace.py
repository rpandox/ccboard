"""memory_proxy.palace() and GET /api/memory/<project>/palace (issue #5) on a mixed project: three repos, a worktree key, noise keys,
every room, a workflow-subagent observation. The fake worker of tests/mem_fake.py only; the clock is patched for the 30 s cache."""
import json

import pytest

from app import memory_proxy as mp
from tests.mem_fake import closed_port, obs
from tests.test_memory_proxy import H, mkrepo, mkworktree, worker_queries

T0 = 1_791_000_000_000


@pytest.fixture(autouse=True)
def _iso(mem_home, monkeypatch):
    monkeypatch.delenv("CLAUDE_MEM_PROJECT_ENVIRONMENTS", raising=False)
    mp.reset()
    yield
    mp.reset()


@pytest.fixture
def mixed(projects_dir, mem_worker):
    p = projects_dir / "shop"
    api, web, _docs = mkrepo(p / "api"), mkrepo(p / "web"), mkrepo(p / "docs")
    mkworktree(api, ".claude/worktrees", "fix-login")
    w = mem_worker
    w.obs += [
        obs(1, "api", T0 + 1, type="bugfix", concepts=["gotcha", "problem-solution"], files_read=["a.py"], facts=["f1", "f2"]),
        obs(2, "api", T0 + 2, type="feature", concepts=["pattern"]),
        obs(3, "api/fix-login", T0 + 3, type="change", concepts=["gotcha"], files_modified=["b.py", "a.py"]),
        obs(4, "web", T0 + 4, type="decision", concepts=["how-it-works", "trade-off"]),
        obs(5, "web", T0 + 5, type="discovery", concepts=["trade-off", "weird concept"]),
        obs(6, "docs", T0 + 6, type="refactor", concepts=["gotcha"], narrative="n" * 5000),
        obs(7, "api", T0 + 7, type="bugfix", concepts=["gotcha"], agent_type="workflow-subagent"),
        obs(8, "api", T0 + 8, type="bugfix", concepts=["gotcha"], agent_type="Explore"),
        obs(9, "tmp", T0 + 9, type="bugfix", concepts=["gotcha"]),
        obs(10, "alice", T0 + 10, type="feature"),
        obs(11, ".hidden-agent", T0 + 11, type="feature"),
    ]
    return w


def test_wings_rooms_drawers_and_gotchas(mixed):
    out = mp.palace("shop")
    assert out["state"] == "ok" and out["subagent_filter"] == "applied" and out["excluded_subagents"] == 1
    wings = {w["name"]: w for w in out["wings"]}
    assert set(wings) == {"api", "web", "docs"}, "noise keys never appear; the worktree folds into api"
    assert wings["api"]["keys"] == ["api", "api/fix-login"] and wings["api"]["total"] == 4
    assert [w["name"] for w in out["wings"]] == ["api", "web", "docs"], "wings by size"
    assert wings["api"]["types"] == {"bugfix": 2, "feature": 1, "change": 1}
    rooms = [(r["name"], r["kind"], r["count"]) for r in wings["api"]["rooms"]]
    assert rooms == [("gotcha", "concept", 3), ("problem-solution", "concept", 1), ("pattern", "concept", 1),
                     ("bugfix", "type", 2), ("feature", "type", 1), ("change", "type", 1)]
    proj = [(r["name"], r["kind"]) for r in out["rooms"]]
    assert proj == [("gotcha", "concept"), ("problem-solution", "concept"), ("pattern", "concept"), ("how-it-works", "concept"),
                    ("trade-off", "concept"), ("weird concept", "concept"), ("bugfix", "type"), ("feature", "type"), ("change", "type"),
                    ("decision", "type"), ("discovery", "type"), ("refactor", "type")], "the stated order: four concepts, others by count, types"
    assert not any(r["count"] == 0 for r in out["rooms"])
    d = wings["api"]["drawers"]
    assert [x["id"] for x in d] == [8, 3, 2, 1], "newest first"
    assert set(d[0]) == {"id", "title", "subtitle", "type", "concepts", "files", "created_at", "created_at_epoch", "key", "narrative",
                         "facts", "agent_type"}
    assert d[1]["files"] == ["b.py", "a.py"] and d[3]["facts"] == ["f1", "f2"]
    assert len(wings["docs"]["drawers"][0]["narrative"]) == mp.NARRATIVE_MAX
    assert [g["id"] for g in out["gotchas"]] == [8, 6, 3] and len(out["gotchas"]) <= 3
    assert out["summaries"] == 170 and out["summaries_scope"] == "worker" and out["tokens_saved"] is None
    assert out["total"] == 7 and out["scanned"] == 8 and out["truncated"] is False      # scanned counts the subagent row it left out


def test_subagents_are_included_on_request(mixed):
    out = mp.palace("shop", subagents=True)
    assert out["excluded_subagents"] == 0 and out["total"] == 8
    assert 7 in [d["id"] for w in out["wings"] for d in w["drawers"]]


def test_drawers_per_wing_are_capped(mixed):
    mixed.obs += [obs(100 + i, "web", T0 + 100 + i) for i in range(30)]
    out = mp.palace("shop", drawers=20)
    assert len({w["name"]: w for w in out["wings"]}["web"]["drawers"]) == 20
    assert len({w["name"]: w for w in mp.palace("shop")["wings"]}["web"]["drawers"]) == mp.DRAWERS_DEFAULT
    with pytest.raises(Exception):
        mp.palace("shop", drawers=21)


def test_the_palace_asks_one_search_with_the_project_keys(mixed):
    mp.palace("shop")
    q = worker_queries(mixed, "/api/search")
    assert len(q) == 1 and set(q[0]["projects"].split(",")) == {"api", "web", "docs", "api/fix-login"}
    assert q[0]["format"] == "json" and q[0]["type"] == "observations" and q[0]["orderBy"] == "date_desc" and "query" not in q[0]


def test_the_30_second_cache(mixed, monkeypatch):
    now = [5000.0]
    monkeypatch.setattr(mp, "clock", lambda: now[0])
    first = mp.palace("shop")
    n = len(mixed.requests)
    now[0] += 29
    again = mp.palace("shop")
    assert len(mixed.requests) == n and again["cached"] is True and again["wings"] == first["wings"]
    now[0] += 2
    mp.palace("shop")
    assert len(mixed.requests) > n
    mp.palace("shop", subagents=True)
    assert len(worker_queries(mixed, "/api/search")) >= 2, "the subagent flag is part of the cache key"


def test_a_search_refusal_falls_back_to_the_list_route_without_the_subagent_filter(mixed):
    mixed.status["/api/search"] = 400
    out = mp.palace("shop")
    assert out["state"] == "ok" and out["subagent_filter"] == "unavailable" and out["excluded_subagents"] == 0
    assert {w["name"] for w in out["wings"]} == {"api", "web", "docs"} and out["total"] == 8


def test_worker_down_is_stale_from_cache_else_the_503(mixed, mem_home, monkeypatch, lite_client):
    now = [5000.0]
    monkeypatch.setattr(mp, "clock", lambda: now[0])
    mp.palace("shop")
    now[0] += 60                                                     # the palace cache is old, the per-URL last-good answers remain
    (mem_home / "worker.pid").write_text(json.dumps({"pid": 1, "port": closed_port()}))
    out = mp.palace("shop")
    assert out["stale"] is True and out["stale_at"] and out["reason_code"] == "refused" and out["wings"]
    mp.reset()
    r = lite_client.get("/api/memory/shop/palace", headers=H)
    assert r.status_code == 503 and r.json()["state"] == "down" and r.json()["up"] is False


def test_the_route(mixed, lite_client):
    r = lite_client.get("/api/memory/shop/palace?subagents=1&drawers=2", headers=H)
    assert r.status_code == 200 and r.json()["subagents"] is True and all(len(w["drawers"]) <= 2 for w in r.json()["wings"])
    assert lite_client.get("/api/memory/nope/palace", headers=H).status_code == 404
    assert lite_client.get("/api/memory/shop/palace?drawers=99", headers=H).status_code == 400
