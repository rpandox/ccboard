"""app/memory_proxy.py (the Memory proxy, issue #4) and its routes in app/main.py, against the fake worker of tests/mem_fake.py.

Never the real worker, never the real ~/.claude-mem: `mem_home` points settings at temp dirs and `projects_dir` at a temp PROJECTS_DIR.
"""
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app import memory, memory_proxy as mp, projects
from app.config import settings
from tests.mem_fake import MemWorker, closed_port, obs, summary

H = {"Tailscale-User-Login": "alice@example.com"}
T0 = 1_791_000_000_000                        # ms
NOISE = ["tmp", "alice", "notes", ".hidden-agent"]


@pytest.fixture(autouse=True)
def _iso(mem_home, monkeypatch):
    monkeypatch.delenv("CLAUDE_MEM_PROJECT_ENVIRONMENTS", raising=False)
    mp.reset()
    yield
    mp.reset()


def mkrepo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)
    return path


def mkworktree(repo: Path, sub: str, slug: str) -> Path:
    wt = repo / sub / slug
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_text(f"gitdir: {repo}/.git/worktrees/{slug}\n")
    return wt


@pytest.fixture
def shop(projects_dir):
    """Project 'shop' (a folder of repos): api with a Claude worktree fix-login, web with a ccboard (Codex) worktree cx1."""
    p = projects_dir / "shop"
    api, web = mkrepo(p / "api"), mkrepo(p / "web")
    mkworktree(api, ".claude/worktrees", "fix-login")
    mkworktree(web, ".ccboard/worktrees", "cx1")
    return p


def fill(w: MemWorker, per_key: dict[str, int], start=T0, step=1000):
    """per_key {key: n}: n observations per key, interleaved in time (newest = highest id)."""
    i = 0
    rows = []
    for n in range(max(per_key.values())):
        for k, cnt in per_key.items():
            if n < cnt:
                i += 1
                rows.append(obs(i, k, start + i * step))
    w.obs += rows
    return rows


def worker_queries(w: MemWorker, path: str) -> list[dict]:
    out = []
    for m, p in w.requests:
        u = urlsplit(p)
        if m == "GET" and u.path == path:
            out.append({k: v[0] for k, v in parse_qs(u.query).items()})
    return out


# ------------------------------------------------------------------ constants pinned to the box check

def test_the_worker_names_are_the_ones_the_box_check_verified():
    assert mp.TESTED_WORKER == "13.35.0"
    assert mp.P_QUERY == "query", "q= is silently ignored by /api/search (box check V11 row 4)"
    assert (mp.P_PROJECT, mp.P_PROJECTS, mp.P_FORMAT, mp.FORMAT_JSON) == ("project", "projects", "format", "json")
    assert (mp.P_ANCHOR, mp.P_DEPTH_BEFORE, mp.P_DEPTH_AFTER) == ("anchor", "depth_before", "depth_after")
    assert mp.ENDPOINTS == {"projects": "/api/projects", "observations": "/api/observations", "summaries": "/api/summaries",
                            "search": "/api/search", "timeline": "/api/timeline", "observation": "/api/observation/", "stats": "/api/stats"}
    assert mp.WORKER_PAGE == 100 and mp.PROXY_CAP == 2 * 1024 * 1024 and memory.BODY_CAP == 256 * 1024
    assert mp.FANOUT == 4 and mp.SCAN_CAP == 300 and mp.FETCH_TIMEOUT <= mp.BUDGET <= mp.SEARCH_BUDGET    # a cold read on the box took > 2 s


# ------------------------------------------------------------------ fetch cap

def test_fetch_strict_cap_is_an_error_while_health_keeps_its_256_kb(mem_worker):
    base = f"http://127.0.0.1:{mem_worker.port}"
    mem_worker.body["/api/stats"] = b'{"x": "' + b"a" * (memory.BODY_CAP + 10) + b'"}'
    assert memory.fetch(base, "/api/stats") == (200, None)                           # health: over 256 KB is quietly None
    code, body = memory.fetch(base, "/api/stats", cap=mp.PROXY_CAP, strict=True)     # the proxy: 2 MB
    assert code == 200 and len(body["x"]) == memory.BODY_CAP + 10
    mem_worker.body["/api/stats"] = b'{"x": "' + b"a" * (mp.PROXY_CAP + 10) + b'"}'
    with pytest.raises(memory.TooLarge):
        memory.fetch(base, "/api/stats", cap=mp.PROXY_CAP, strict=True)


def test_a_body_over_two_megabytes_is_a_degraded_answer_never_an_empty_list(shop, mem_worker):
    fill(mem_worker, {"api": 1})
    mem_worker.body["/api/observations"] = b'{"items": [], "pad": "' + b"a" * (mp.PROXY_CAP + 10) + b'"}'
    with pytest.raises(mp.WorkerError) as e:
        mp.observations("shop", repo="api")
    assert e.value.state == "degraded" and e.value.code == "too_large"


# ------------------------------------------------------------------ keys

def test_keys_repo_folders_root_and_both_worktree_kinds_intersected_with_the_worker(shop, mem_worker):
    fill(mem_worker, {k: 1 for k in ["api", "web", "shop", "api/fix-login", "web/cx1", "api/old-slug", *NOISE, "other"]})
    kf = mp.keys_for("shop")
    got = {k["key"]: (k["wing"], k["kind"]) for k in kf["keys"]}
    assert got == {"api": ("api", "repo"), "web": ("web", "repo"), "shop": ("root", "root"), "api/fix-login": ("api", "worktree"),
                   "web/cx1": ("web", "worktree"), "api/old-slug": ("api", "worktree")}, "an ended worktree keeps its memory"
    assert kf["checked"] is True and kf["ambiguous"] == []
    assert not set(got) & set(NOISE)


def test_keys_without_the_worker_list_are_the_disk_keys_only(shop, mem_home):
    (mem_home / "worker.pid").write_text(json.dumps({"pid": 1, "port": closed_port()}))
    kf = mp.keys_for("shop")
    assert {k["key"] for k in kf["keys"]} == {"api", "web", "shop", "api/fix-login", "web/cx1"} and kf["checked"] is False


def test_keys_a_project_that_is_itself_a_repo_and_a_worktree_named_like_its_repo(projects_dir, mem_worker):
    solo = mkrepo(projects_dir / "solo")
    mkworktree(solo, ".ccboard/worktrees", "solo")                # collapses to plain <repo> (box check row 10)
    mkworktree(solo, ".claude/worktrees", "t1")
    fill(mem_worker, {"solo": 1, "solo/t1": 1})
    assert [(k["key"], k["wing"]) for k in mp.keys_for("solo")["keys"]] == [("solo", "solo"), ("solo/t1", "solo")]
    assert mp.worktree_key("api", "api") == "api" and mp.worktree_key("api", "x") == "api/x"


def test_keys_from_claude_mem_project_environments(shop, mem_worker, mem_home, monkeypatch):
    (mem_home / "settings.json").write_text(json.dumps({"CLAUDE_MEM_PROJECT_ENVIRONMENTS": json.dumps(
        [{"name": "shop-api", "patterns": [str(shop / "api") + "/**"]}, {"bad": 1}])}))
    fill(mem_worker, {"shop-api": 1, "web": 1})
    assert {k["key"]: k["kind"] for k in mp.keys_for("shop")["keys"]} == {"shop-api": "env", "web": "repo"}
    monkeypatch.setenv("CLAUDE_MEM_PROJECT_ENVIRONMENTS", "not json")          # the env var wins; invalid is ignored
    mp.reset()
    assert {k["key"] for k in mp.keys_for("shop")["keys"]} == {"web"}       # api is not a worker key here


def test_a_bad_or_unknown_project_is_refused(shop, mem_worker):
    with pytest.raises(projects.BadRequest):
        mp.keys_for("../etc")
    with pytest.raises(projects.NotFound):
        mp.keys_for("nope")


# ------------------------------------------------------------------ fan-out merge, limit, offset, cursor

def test_observations_merge_newest_first_with_limit_offset_and_parsed_lists(shop, mem_worker):
    rows = fill(mem_worker, {"api": 5, "web": 5, "shop": 3, "api/fix-login": 2, "tmp": 9})
    out = mp.observations("shop", limit=4)
    assert [r["id"] for r in out["items"]] == sorted((r["id"] for r in rows if r["project"] != "tmp"), reverse=True)[:4]
    assert out["state"] == "ok" and out["up"] is True and out["stale"] is False and out["partial"] == []
    assert out["has_more"] is True and out["next_before"] == out["items"][-1]["created_at_epoch"]
    it = out["items"][0]
    assert isinstance(it["concepts"], list) and isinstance(it["facts"], list) and it["key"] and it["repo"]
    out2 = mp.observations("shop", limit=4, offset=4)
    assert [r["id"] for r in out2["items"]] == sorted((r["id"] for r in rows if r["project"] != "tmp"), reverse=True)[4:8]
    asked = {q["project"] for q in worker_queries(mem_worker, "/api/observations")}
    assert asked == {"api", "web", "shop", "api/fix-login"}, "no request ever names a key outside keys_for(project)"


def test_the_before_cursor_pages_without_duplicates_to_the_end(shop, mem_worker):
    rows = fill(mem_worker, {"api": 40, "web": 25, "api/fix-login": 7})
    seen, before = [], None
    for _ in range(20):
        out = mp.observations("shop", limit=10, before=before)
        seen += [r["id"] for r in out["items"]]
        if not out["has_more"]:
            break
        before = out["next_before"]
    assert seen == sorted((r["id"] for r in rows), reverse=True) and len(seen) == len(set(seen))


def test_the_scan_cap_stops_a_short_page_and_says_so(shop, mem_worker):
    fill(mem_worker, {"api": 400})
    mem_worker.obs += [obs(10_000 + i, "web", T0 + 10_000_000 + i, type="bugfix") for i in range(2)]
    for r in mem_worker.obs:
        if r["project"] == "api" and r["id"] < 30:
            r["type"] = "bugfix"                                           # deep in the api list: past the 300-row cap
    out = mp.observations("shop", type_="bugfix", limit=10)
    assert [r["id"] for r in out["items"]] == [10_001, 10_000]
    assert out["scan_capped"] is True and out["has_more"] is True and out["next_before"] is not None
    n_api = [q for q in worker_queries(mem_worker, "/api/observations") if q["project"] == "api"]
    assert sum(int(q["limit"]) for q in n_api) <= mp.SCAN_CAP
    rest = mp.observations("shop", type_="bugfix", limit=10, before=out["next_before"])
    assert {r["id"] for r in rest["items"]} <= {r["id"] for r in mem_worker.obs if r["type"] == "bugfix"}


def test_filters_after_the_merge_and_the_repo_narrows_the_keys(shop, mem_worker):
    mem_worker.obs += [obs(1, "api", T0 + 1, type="bugfix", platform="claude", session="s-1"),
                       obs(2, "web", T0 + 2, type="feature", platform="claude"),
                       obs(3, "api/fix-login", T0 + 3, type="bugfix", platform="codex"),
                       obs(4, "api", T0 + 4, type="decision")]
    assert [r["id"] for r in mp.observations("shop", type_="bugfix")["items"]] == [3, 1]
    assert [r["id"] for r in mp.observations("shop", repo="api")["items"]] == [4, 3, 1]
    assert [r["id"] for r in mp.observations("shop", agent="codex")["items"]] == [3]
    assert [r["id"] for r in mp.observations("shop", session="s-1")["items"]] == [1]
    assert [r["id"] for r in mp.observations("shop", since=T0 + 2, until=T0 + 3)["items"]] == [3, 2]
    qs = worker_queries(mem_worker, "/api/observations")
    assert all(set(q) <= {"project", "limit", "offset", "platformSource", "contentSessionId"} for q in qs)
    assert any(q.get("platformSource") == "codex" for q in qs) and any(q.get("contentSessionId") == "s-1" for q in qs)
    with pytest.raises(projects.BadRequest):
        mp.observations("shop", agent="gpt")
    with pytest.raises(projects.BadRequest):
        mp.observations("shop", limit="500")


def test_summaries_merge_the_same_way(shop, mem_worker):
    mem_worker.summaries += [summary(1, "api", T0 + 1), summary(2, "web", T0 + 2), summary(3, "tmp", T0 + 3)]
    out = mp.summaries("shop")
    assert [r["id"] for r in out["items"]] == [2, 1] and out["items"][0]["learned"] == "learned 2"


# ------------------------------------------------------------------ degraded states

def test_a_refused_connection_is_503_down_and_a_hang_without_cache_is_degraded(shop, mem_home, mem_worker):
    fill(mem_worker, {"api": 2})
    mem_worker.delay_match.append(("/api/observations", mp.BUDGET + 2.0))     # past every cap, whatever the budgets are
    t0 = time.monotonic()
    with pytest.raises(mp.WorkerError) as e:
        mp.observations("shop", repo="api")
    assert e.value.state == "degraded" and e.value.code == "timeout" and "slow" in e.value.reason
    assert time.monotonic() - t0 < mp.BUDGET + 0.8
    (mem_home / "worker.pid").write_text(json.dumps({"pid": 1, "port": closed_port()}))
    mp.reset()
    with pytest.raises(mp.WorkerError) as e:
        mp.observations("shop")
    assert e.value.state == "down" and e.value.code == "refused" and e.value.body()["up"] is False


def test_the_last_good_answer_is_served_stale_and_errors_are_never_cached(shop, mem_worker, monkeypatch):
    fill(mem_worker, {"api": 3})
    now = [1000.0]
    monkeypatch.setattr(mp, "clock", lambda: now[0])
    fresh = mp.observations("shop", repo="api")
    assert fresh["stale"] is False and len(fresh["items"]) == 3
    n = len(mem_worker.requests)
    assert mp.observations("shop", repo="api")["items"] == fresh["items"] and len(mem_worker.requests) == n, "fresh cache: no request"
    now[0] += mp.CACHE_FRESH + 1
    mem_worker.status["/api/observations"] = 500
    out = mp.observations("shop", repo="api")
    assert out["stale"] is True and out["stale_at"] and "HTTP 500" in out["reason"] and len(out["items"]) == 3
    assert out["reason_code"] == "http"


def test_one_hanging_key_is_partial_and_the_rest_is_served(shop, mem_worker):
    fill(mem_worker, {"api": 2, "web": 2})
    mem_worker.delay_match.append(("project=web", mp.BUDGET + 2.0))
    t0 = time.monotonic()
    out = mp.observations("shop")
    assert out["partial"] == ["web"] and {r["key"] for r in out["items"]} == {"api"}
    assert time.monotonic() - t0 < mp.BUDGET + 0.8


def test_a_renamed_field_is_incompatible_never_a_half_parsed_list(shop, mem_worker):
    rows = fill(mem_worker, {"api": 2})
    mem_worker.body["/api/observations"] = {"items": [{**{k: v for k, v in r.items() if k != "created_at_epoch"}, "createdAt": 1}
                                                       for r in rows], "hasMore": False}
    out = mp.observations("shop", repo="api")
    assert out["state"] == "incompatible" and out["shape_error"] is True and out["items"] == []
    assert "created_at_epoch" in out["reason"]
    mem_worker.body["/api/observations"] = {"items": {"0": rows[0]}}
    mp.reset()
    assert mp.observations("shop", repo="api")["state"] == "incompatible"


# ------------------------------------------------------------------ ambiguity

def test_a_shared_folder_name_is_flagged_and_rows_are_filtered_by_their_paths(shop, projects_dir, mem_worker):
    mkrepo(projects_dir / "blog" / "api")
    mine, theirs = str(shop / "api" / "x.py"), str(projects_dir / "blog" / "api" / "y.py")
    mem_worker.obs += [obs(1, "api", T0 + 1, files_read=[mine]), obs(2, "api", T0 + 2, files_modified=[theirs]),
                       obs(3, "api", T0 + 3, files_read=["relative/only.py"]), obs(4, "web", T0 + 4, files_read=[theirs])]
    out = mp.observations("shop")
    assert out["ambiguous"] == [{"key": "api", "shared_with": ["blog/api"]}] and out["ambiguous_filter"] == "files"
    assert [r["id"] for r in out["items"]] == [4, 3, 1], "only the ambiguous key is path-filtered; a row without absolute paths stays"


# ------------------------------------------------------------------ search and timeline

def test_search_sends_query_and_projects_in_one_call_with_format_json(shop, mem_worker):
    mem_worker.obs += [obs(1, "api", T0 + 1, title="login bug"), obs(2, "tmp", T0 + 2, title="login noise"),
                       obs(3, "web/cx1", T0 + 3, title="Login page")]
    out = mp.search("shop", "login")
    assert [r["id"] for r in out["observations"]] == [3, 1] and out["query"] == "login"
    q = worker_queries(mem_worker, "/api/search")
    assert len(q) == 1 and q[0]["query"] == "login" and q[0]["format"] == "json" and "q" not in q[0]
    assert set(q[0]["projects"].split(",")) == {"api", "web/cx1"}         # only keys the worker has (tmp never)
    with pytest.raises(projects.BadRequest):
        mp.search("shop", "  ")


def test_search_falls_back_per_key_when_the_plural_is_refused(shop, mem_worker):
    mem_worker.obs += [obs(1, "api", T0 + 1, title="x"), obs(2, "web", T0 + 2, title="x")]
    orig = mem_worker.compute

    def compute(bare, q):
        if bare == "/api/search" and "projects" in q:
            return 400, {"error": "bad projects", "code": "INVALID_PROJECTS"}
        return orig(bare, q)
    mem_worker.compute = compute
    out = mp.search("shop", "x")
    assert out["fallback"] == "per_key" and [r["id"] for r in out["observations"]] == [2, 1]


def test_timeline_around_an_anchor_of_this_project_only(shop, mem_worker):
    mem_worker.obs += [obs(5, "api", T0 + 5), obs(6, "api", T0 + 6), obs(7, "tmp", T0 + 7)]
    out = mp.timeline("shop", "5")
    assert out["anchor"]["id"] == 5 and out["key"] == "api" and [r["id"] for r in out["items"]] == [5, 6]
    assert worker_queries(mem_worker, "/api/timeline")[-1] == {"anchor": "5", "depth_before": "5", "depth_after": "5", "project": "api"}
    with pytest.raises(projects.NotFound):
        mp.timeline("shop", "7")                       # another key's observation is never shown
    with pytest.raises(projects.NotFound):
        mp.timeline("shop", "99")
    with pytest.raises(projects.BadRequest):
        mp.timeline("shop", None)


def test_timeline_is_error_on_a_200_is_read(shop, mem_worker):
    mem_worker.obs += [obs(5, "api", T0 + 5)]
    mem_worker.body["/api/timeline"] = {"content": [{"type": "text", "text": "Error: Must provide either anchor"}], "isError": True}
    with pytest.raises(mp.WorkerError) as e:
        mp.timeline("shop", 5)
    assert e.value.code == "worker_error" and "Must provide" in e.value.reason


def test_parse_timeline_reads_the_fixture_rows():
    text = json.loads((Path(__file__).parent / "fixtures" / "claude_mem_timeline_anchor.json").read_text())["content"][0]["text"]
    rows = mp.parse_timeline(text)
    assert [r["id"] for r in rows] == [5930, 5931, 5932] and rows[0]["day"] == "Sep 27, 2026" and rows[0]["tokens"] == 95


# ------------------------------------------------------------------ compat

@pytest.mark.parametrize("ver,want", [("13.35.0", "ok"), ("13.34.2", "ok"), ("13.31.0", "ok"), ("13.35.1", "untested"), ("13.36.0", "untested"),
                                      ("14.0.0", "unknown"), ("12.9.0", "unknown"), (None, "unknown"), ("garbage", "unknown")])
def test_compat(ver, want):
    assert mp.compat(ver) == want


# ------------------------------------------------------------------ the routes

def test_routes_answer_the_shapes_behind_the_identity_check(shop, mem_worker, lite_client):
    fill(mem_worker, {"api": 3})
    for path in ("/api/memory/shop/observations", "/api/memory/shop/summaries", "/api/memory/shop/palace", "/api/memory/health",
                 "/api/memory/shop/search?q=title", "/api/memory/prefs"):
        assert lite_client.get(path).status_code == 403, path
        r = lite_client.get(path, headers=H)
        assert r.status_code == 200, (path, r.text)
    body = lite_client.get("/api/memory/shop/observations?limit=2&bogus=1", headers=H).json()
    assert len(body["items"]) == 2 and body["state"] == "ok" and body["compat"] == "unknown"     # nothing sampled yet: version unknown
    assert all("bogus" not in p for p in mem_worker.paths())
    assert lite_client.get("/api/memory/nope/observations", headers=H).status_code == 404
    assert lite_client.get("/api/memory/b%20d/observations", headers=H).status_code in (400, 404)
    assert lite_client.get("/api/memory/shop/timeline", headers=H).status_code == 400


def test_routes_503_down_with_up_false(shop, mem_home, lite_client):
    (mem_home / "worker.pid").write_text(json.dumps({"pid": 1, "port": closed_port()}))
    t0 = time.monotonic()
    r = lite_client.get("/api/memory/shop/observations", headers=H)
    assert r.status_code == 503 and r.json()["state"] == "down" and r.json()["up"] is False and r.json()["reason"]
    assert time.monotonic() - t0 < 3.5


def test_routes_off_when_claude_mem_is_off(shop, lite_client, monkeypatch):
    monkeypatch.setattr(settings, "claude_mem", False)
    assert lite_client.get("/api/memory/shop/observations", headers=H).json()["state"] == "off"
    assert lite_client.get("/api/memory/health", headers=H).json()["state"] == "off"


def test_health_route_reads_the_monitor_and_refresh_probes_once(mem_worker, lite_client):
    assert lite_client.get("/api/memory/health", headers=H).json()["state"] == "unknown"
    assert mem_worker.requests == []
    h = lite_client.get("/api/memory/health?refresh=1", headers=H).json()
    assert h["state"] == "up" and h["up"] is True and h["version"] == "13.29.0" and h["compat"] == "ok"
    assert h["tested_worker"] == mp.TESTED_WORKER and "rates" in h and "stale_sessions" in h
    assert mem_worker.paths().count("/health") == 1
    again = lite_client.get("/api/memory/health", headers=H).json()
    assert again["state"] == "up" and mem_worker.paths().count("/health") == 1


def test_no_worker_body_is_logged(shop, mem_worker, caplog):
    mem_worker.obs += [obs(1, "api", T0, narrative="SECRET-NARRATIVE")]
    caplog.set_level("DEBUG")
    mp.observations("shop")
    mp.search("shop", "title")
    assert "SECRET-NARRATIVE" not in caplog.text
