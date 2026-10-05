import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import cost, samples

MIXED = (Path(__file__).parent / "fixtures" / "ccusage_session_mixed.json").read_text()

C1 = "11111111-1111-4111-8111-111111111111"
C2 = "22222222-2222-4222-8222-222222222222"
C3 = "33333333-3333-4333-8333-333333333333"
C4 = "44444444-4444-4444-8444-444444444444"          # a Claude model ccusage has no price for
C5 = "55555555-5555-4555-8555-555555555555"          # only its workflow run is listed
X1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"          # a Codex session, unified shape (period)
X2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"          # a Codex session, legacy shape (sessionFile, no agent field), unpriced
NOW = datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)


def test_parse_mixed_dump_joins_on_the_trailing_uuid():
    costs = cost.parse_sessions(MIXED)
    # observer sessions, another agent's rows and a Codex name with no uuid are not sessions here
    assert set(costs) == {C1, C2, C3, C4, C5, X1, X2}
    assert {k: v["agent"] for k, v in costs.items()} == {C1: "claude", C2: "claude", C3: "claude", C4: "claude", C5: "claude",
                                                         X1: "codex", X2: "codex"}
    # the Codex row's trailing uuid wins over the YYYY/MM/DD/rollout-<ts> prefix; the legacy sessionFile shape (no agent) is Codex by name
    assert costs[X1]["cost"] == 4.0 and costs[X1]["tokens"] == 9000 and costs[X1]["models"] == ["gpt-5.6-sol"]
    assert costs[X2]["agent"] == "codex" and costs[X2]["last"] == "2026-09-10T08:00:00.000Z"
    assert cost.parse_sessions("garbage") == {} and cost.parse_sessions("[]") == {} and cost.parse_sessions("") == {}


def test_workflow_runs_roll_up_into_the_parent_session():
    costs = cost.parse_sessions(MIXED)
    # C1: its own row plus two wf_* runs (one under a project slug); cost and tokens add, the newest activity and the models merge
    c1 = costs[C1]
    assert c1["cost"] == pytest.approx(2.25) and c1["tokens"] == 150 and c1["subs"] == 2
    assert c1["last"] == "2026-09-27T02:30:00.000Z" and c1["models"] == ["claude-opus-4-7", "claude-sonnet-5"]
    # a parent that has no row of its own still gets an entry from its workflow run, keyed by the parent uuid
    assert costs[C5]["cost"] == pytest.approx(0.1) and costs[C5]["subs"] == 1 and costs[C5]["tokens"] == 20
    # the order of the rows does not matter: a parent listed after its child must not overwrite it
    rows = json.loads(MIXED)["session"]
    flipped = cost.parse_sessions(json.dumps({"session": list(reversed(rows))}))
    assert flipped[C1]["cost"] == pytest.approx(2.25) and flipped[C1]["subs"] == 2


def test_join_key_shapes():
    uid = "0123abcd-4567-4def-8123-456789abcdef"
    assert cost.join_key(uid) == (uid, False)
    assert cost.join_key(uid.upper()) == (uid, False)
    assert cost.join_key(f"2026/10/04/rollout-2026-10-04T01-02-03-{uid}") == (uid, False)
    assert cost.join_key(f"rollout-2026-10-04T01-02-03-{uid}.jsonl") == (uid, False)
    assert cost.join_key(f"-srv-projects-shop-api/{uid}") == (uid, False)
    assert cost.join_key(f"{uid}/subagents/workflows/wf_1dae1434-2f1") == (uid, True)
    assert cost.join_key(f"-slug/{uid}/subagents/agent-x") == (uid, True)
    assert cost.join_key("2026/09/03/rollout-codex") is None and cost.join_key("") is None and cost.join_key(None) is None
    assert cost.join_key(f"x{uid}") is None, "a longer hex run is not a uuid"
    assert cost.join_key(f"{uid}/notes/other") is None, "a path that merely follows a uuid is not a workflow run"


def test_unpriced_flag_covers_both_agents():
    costs = cost.parse_sessions(MIXED)
    assert {k for k, v in costs.items() if v["unpriced"]} == {C4, X2}          # tokens > 0 and cost 0, Claude's unpriced model included
    entries = cost.session_samples(costs, [])
    u = cost.unpriced(entries)
    assert u["sessions"] == 2 and u["tokens"] == 1200
    assert [t["id"] for t in u["top"]] == [C4, X2] and u["top"][0]["agent"] == "claude" and u["top"][1]["agent"] == "codex"
    assert {e["id"]: e["agent"] for e in entries} == {C1: "claude", C2: "claude", C3: "claude", C4: "claude", C5: "claude", X1: "codex", X2: "codex"}
    assert next(e for e in entries if e["id"] == X2)["unpriced"] is True and next(e for e in entries if e["id"] == C1)["unpriced"] is False


def test_attribute_by_agent_globally_and_per_project():
    costs = cost.parse_sessions(MIXED)
    sessions = [{"project": "shop", "repo": "api", "claude_session_id": C1, "agent": "claude"},
                {"project": "shop", "repo": "web", "claude_session_id": C2, "agent": "claude"},
                {"project": "shop", "repo": "web", "claude_session_id": C2, "agent": "claude"},     # relaunched row, same id
                {"project": "blog", "repo": "site", "claude_session_id": C3, "agent": "claude"},
                {"project": "blog", "repo": "site", "claude_session_id": X1.upper(), "agent": "codex"},   # the Codex thread id lives in the same column
                {"project": "blog", "repo": "site", "claude_session_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd", "agent": "claude"}]  # unknown to ccusage
    tasks = [{"id": 5, "claude_session_id": C3}, {"id": 6, "claude_session_id": None}, {"id": 7, "claude_session_id": X1}]
    r = cost.attribute(costs, sessions, tasks, NOW)
    shop, blog = r["projects"]["shop"], r["projects"]["blog"]
    assert {k: shop[k] for k in ("total", "today", "week", "repos")} == {"total": 4.25, "today": 2.25, "week": 2.25, "repos": {"api": 2.25, "web": 2.0}}
    assert shop["by_agent"] == {"claude": {"total": 4.25, "today": 2.25, "week": 2.25, "tokens": 200},
                                "codex": {"total": 0.0, "today": 0.0, "week": 0.0, "tokens": 0}}
    assert blog["total"] == 4.25 and blog["today"] == 4.0 and blog["week"] == 4.25 and blog["repos"] == {"site": 4.25}
    assert blog["by_agent"]["claude"] == {"total": 0.25, "today": 0.0, "week": 0.25, "tokens": 5}
    assert blog["by_agent"]["codex"] == {"total": 4.0, "today": 4.0, "week": 4.0, "tokens": 9000}
    assert r["tasks"] == {5: 0.25, 7: 4.0} and r["attributed"] == 4 and r["sessions_known"] == 7
    # the global split covers every ccusage session, attributed or not (ccusage is the total)
    assert r["total_all"] == 8.6
    assert r["by_agent"]["claude"] == {"total": 4.6, "today": 2.25, "week": 2.6, "tokens": 925}
    assert r["by_agent"]["codex"] == {"total": 4.0, "today": 4.0, "week": 4.0, "tokens": 9500}


def test_attribute_without_codex_rows_still_lists_both_agents():
    r = cost.attribute({}, [], [], NOW)
    assert r["by_agent"] == {"claude": {"total": 0.0, "today": 0.0, "week": 0.0, "tokens": 0},
                             "codex": {"total": 0.0, "today": 0.0, "week": 0.0, "tokens": 0}}
    assert r["projects"] == {} and r["total_all"] == 0 and r["sessions_known"] == 0


def test_session_ids_select_by_agent(client):
    from app import main
    main.db.add_session(tmux_name="shop--api--s1", project="shop", repo="api", name="s1", launcher="claude", claude_session_id=C1)
    main.db.add_session(tmux_name="shop--api--s2", project="shop", repo="api", name="s2", launcher="resume", claude_session_id=X1, agent="codex")
    main.db.add_session(tmux_name="shop--api--s3", project="shop", repo="api", name="s3", launcher="shell")
    assert {r["claude_session_id"]: r["agent"] for r in main.db.session_ids()} == {C1: "claude", X1: "codex"}
    assert [r["claude_session_id"] for r in main.db.session_ids("codex")] == [X1]
    assert [r["claude_session_id"] for r in main.db.session_ids("claude")] == [C1]


def test_refresh_route(client, fake_tmux, monkeypatch):
    from app import main
    monkeypatch.setattr(cost, "fetch_sessions", lambda: cost.parse_sessions(MIXED))
    H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
    tid = main.db.task_add(project="shop", repo="api", slug="s", title="T", prompt="p", branch="b", base="main", worktree="/nope",
                           tmux_name="shop--api--t-s", claude_session_id=C1)
    main.db.add_session(tmux_name="blog--site--s1", project="blog", repo="site", name="s1", launcher="resume", claude_session_id=X1, agent="codex")
    r = client.post("/api/cost/refresh", headers=H).json()
    assert r["tasks"][str(tid)] == 2.25 or r["tasks"][tid] == 2.25
    st = client.get("/api/state", headers=H).json()
    v = st["cost"]["value"]
    assert v["sessions_known"] == 7 and v["by_agent"]["codex"]["total"] == 4.0
    assert v["projects"]["blog"]["by_agent"]["codex"]["tokens"] == 9000
    assert v["unpriced"]["sessions"] == 2 and [(t["agent"], t["tokens"]) for t in v["unpriced"]["top"]] == [("claude", 700), ("codex", 500)]
    assert next(t for t in st["tasks"] if t["id"] == tid)["cost_usd"] == 2.25
    # the cost series carries both agents: '<agent>:<uuid>' keys, Codex sessions included, the Codex one tagged with its project
    last = main.db.sample_last("cost", f"codex:{X1}")
    assert last and last["value"] == 4.0 and last["meta"]["a"] == "codex" and last["meta"]["p"] == "blog" and last["meta"]["tok"] == 9000
    assert main.db.sample_last("cost", f"claude:{C1}")["value"] == 2.25
    assert main.db.sample_last("cost", f"codex:{X2}")["value"] == 0
    assert "acct" not in (last["meta"] or {}), "a Codex session is not billed to a Claude subscription account"
    monkeypatch.setattr(cost, "fetch_sessions", lambda: None)
    assert client.post("/api/cost/refresh", headers=H).status_code == 400


# ---------------------------------------------------------------- fetching: the unified listing and the ccusage codex fallback

CODEX_ONLY = json.dumps({"sessions": [
    {"sessionId": "2026/09/03/rollout-2026-09-03T01-02-03-cccccccc-cccc-4ccc-8ccc-cccccccccccc", "lastActivity": "2026-09-03T01:05:00.000Z",
     "totalTokens": 1234, "costUSD": 0.5, "models": {"gpt-5.6-sol": {"inputTokens": 1000, "isFallback": False}}},
    {"sessionId": "2026/09/04/rollout-2026-09-04T01-02-03-dddddddd-dddd-4ddd-8ddd-dddddddddddd", "totalTokens": 77, "costUSD": 0},
], "totals": {"costUSD": 0.5}})
UNIFIED_CLAUDE_ONLY = json.dumps({"session": [r for r in json.loads(MIXED)["session"] if r.get("agent") == "claude"]})


class FakeCcusage:
    """run(exe, args, env) for cost.collect: answers by subcommand and records the calls."""

    def __init__(self, unified=UNIFIED_CLAUDE_ONLY, codex=CODEX_ONLY):
        self.unified, self.codex, self.calls = unified, codex, []

    def __call__(self, exe, args, env=None):
        self.calls.append((exe, list(args), (env or {}).get("CODEX_HOME")))
        if args[:2] == ["codex", "session"]:
            return self.codex
        return self.unified


def test_collect_falls_back_to_ccusage_codex_when_the_unified_listing_has_no_codex_rows(codex_home):
    (codex_home / "sessions").mkdir()
    run = FakeCcusage()
    costs = cost.collect(run=run, which=lambda n: "/fake/ccusage")
    assert run.calls[0][1] == ["session", "--json"]
    assert run.calls[1] == ("/fake/ccusage", ["codex", "session", "--json", "--offline"], str(codex_home))
    cx = {k: v for k, v in costs.items() if v["agent"] == "codex"}
    assert set(cx) == {"cccccccc-cccc-4ccc-8ccc-cccccccccccc", "dddddddd-dddd-4ddd-8ddd-dddddddddddd"}
    assert cx["cccccccc-cccc-4ccc-8ccc-cccccccccccc"]["cost"] == 0.5 and cx["cccccccc-cccc-4ccc-8ccc-cccccccccccc"]["models"] == ["gpt-5.6-sol"]
    assert cx["dddddddd-dddd-4ddd-8ddd-dddddddddddd"]["unpriced"] is True
    assert C1 in costs and costs[C1]["agent"] == "claude"


def test_collect_does_not_ask_for_codex_twice(codex_home):
    (codex_home / "sessions").mkdir()
    run = FakeCcusage(unified=MIXED)
    costs = cost.collect(run=run, which=lambda n: "/fake/ccusage")
    assert len(run.calls) == 1 and X1 in costs


def test_collect_fallback_is_skipped_without_codex_rollouts_and_never_fails_the_listing(codex_home):
    run = FakeCcusage()
    costs = cost.collect(run=run, which=lambda n: "/fake/ccusage")                 # no CODEX_HOME/sessions on this box
    assert len(run.calls) == 1 and not any(v["agent"] == "codex" for v in costs.values())
    (codex_home / "sessions").mkdir()
    broken = FakeCcusage(codex=None)                                              # `ccusage codex` fails: the Claude half stands
    costs = cost.collect(run=broken, which=lambda n: "/fake/ccusage")
    assert len(broken.calls) == 2 and C1 in costs and not any(v["agent"] == "codex" for v in costs.values())
    junk = FakeCcusage(codex="not json")
    assert C1 in cost.collect(run=junk, which=lambda n: "/fake/ccusage")


def test_collect_none_when_ccusage_is_missing_or_the_unified_listing_fails():
    assert cost.collect(run=FakeCcusage(), which=lambda n: None) is None
    assert cost.collect(run=FakeCcusage(unified=None), which=lambda n: "/fake/ccusage") is None


def test_collect_resolves_ccusage_and_the_runner_at_call_time(monkeypatch, codex_home):
    monkeypatch.setattr(cost.shutil, "which", lambda n: None)
    assert cost.collect() is None
    monkeypatch.setattr(cost.shutil, "which", lambda n: "/fake/ccusage")
    monkeypatch.setattr(cost, "_run_ccusage", lambda exe, args, env=None: UNIFIED_CLAUDE_ONLY)
    assert C1 in cost.collect()


def test_run_ccusage_wraps_subprocess(monkeypatch):
    class Done:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return Done(0, "{}")
    monkeypatch.setattr(cost.subprocess, "run", fake_run)
    assert cost._run_ccusage("/x/ccusage", ["codex", "session"], {"CODEX_HOME": "/h"}) == "{}"
    assert seen["argv"] == ["/x/ccusage", "codex", "session"] and seen["kw"]["env"] == {"CODEX_HOME": "/h"} and seen["kw"]["timeout"] == 240
    monkeypatch.setattr(cost.subprocess, "run", lambda argv, **kw: Done(1, "x"))
    assert cost._run_ccusage("/x/ccusage", ["session"]) is None

    def boom(argv, **kw):
        raise cost.subprocess.TimeoutExpired(argv, 1)
    monkeypatch.setattr(cost.subprocess, "run", boom)
    assert cost._run_ccusage("/x/ccusage", ["session"]) is None


def test_record_cost_writes_codex_keys_for_the_mixed_dump(client):
    from app import main
    costs = cost.parse_sessions(MIXED)
    entries = cost.session_samples(costs, [{"project": "blog", "repo": "site", "claude_session_id": X1, "agent": "codex"}])
    assert samples.record_cost(main.db, {"sessions": entries}, NOW) == 7
    assert main.db.sample_last("cost", f"codex:{X1}")["meta"]["p"] == "blog"
    assert main.db.sample_last("cost", f"codex:{X2}")["meta"]["tok"] == 500
