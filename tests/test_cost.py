import json
from datetime import datetime, timedelta, timezone
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


# ---------------------------------------------------------------- sessions started outside the board: the folder join (issue #57)

from app import projects as projects_mod  # noqa: E402
from app.agents import codex_discovery  # noqa: E402
from app.config import settings  # noqa: E402

FOLDER_IN = "ffffffff-ffff-4fff-8fff-fffffffffff1"      # a plain terminal session inside a repo of the projects folder
FOLDER_WT = "ffffffff-ffff-4fff-8fff-fffffffffff2"      # ran in a task worktree of that repo
FOLDER_OUT = "ffffffff-ffff-4fff-8fff-fffffffffff3"     # ran outside the projects folder
FOLDER_NONE = "ffffffff-ffff-4fff-8fff-fffffffffff4"    # no folder anywhere
FOLDER_BAD = "ffffffff-ffff-4fff-8fff-fffffffffff5"     # a transcript with only corrupt lines


def _costs(*ids, agent="claude"):
    return {i: {"agent": agent, "cost": 1.0 + n, "tokens": 100 * (n + 1), "last": "2026-09-26T10:00:00.000Z", "models": ["claude-sonnet-5"], "subs": 0,
                "unpriced": False} for n, i in enumerate(ids)}


def _transcript(cfg, sid, lines, folder="-srv-whatever", age=0):
    p = cfg / "projects" / folder / f"{sid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n")
    if age:
        import os
        t = p.stat().st_mtime - age
        os.utime(p, (t, t))
    return p


def _registry(cfg, pid, sid, cwd, **extra):
    d = cfg / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": sid, "cwd": cwd, "kind": "interactive", **extra}))


@pytest.fixture
def folders(projects_dir, tmp_path):
    from app.db import DB
    cfg = tmp_path / "claude"
    cfg.mkdir()
    (projects_dir / "shop" / "api").mkdir(parents=True)
    (projects_dir / "shop" / "api" / ".claude" / "worktrees" / "t-1").mkdir(parents=True)
    (projects_dir / "shop" / ".claude" / "worktrees" / "p-1").mkdir(parents=True)
    db = DB(tmp_path / "cost.db")
    return type("F", (), {"db": db, "cfg": cfg, "projects": projects_dir, "tmp": tmp_path})


def test_repo_for_cwd_maps_folders_worktrees_and_refuses_the_rest(projects_dir):
    f = projects_mod.repo_for_cwd
    root = projects_dir
    assert f(str(root / "shop" / "api")) == ("shop", "api")
    assert f(str(root / "shop" / "api" / "src" / "deep")) == ("shop", "api"), "a subfolder belongs to its repo"
    assert f(str(root / "shop" / "api" / ".claude" / "worktrees" / "t-1")) == ("shop", "api"), "a Claude task worktree"
    assert f(str(root / "shop" / "api" / ".ccboard" / "worktrees" / "t-2")) == ("shop", "api"), "a worktree ccboard made for another agent"
    assert f(str(root / "shop" / ".claude" / "worktrees" / "p-1")) == ("shop", "root"), "a worktree of the project folder itself"
    assert f(str(root / "shop")) == ("shop", "root")
    assert f(str(root)) is None and f(str(root.parent)) is None and f("/elsewhere/shop/api") is None
    assert f("") is None and f(None) is None and f("relative/path") is None
    assert f(str(root / "bad name" / "api")) is None, "names the board would refuse are not projects"
    assert f(str(root / "shop" / ".." / ".." / "x")) is None, "a path that climbs out is outside"
    assert codex_discovery.project_repo(str(root / "shop" / "api")) == ("shop", "api"), "the Codex alias answers the same"
    assert codex_discovery.project_repo(str(root / "shop"), root) == ("shop", "root")


def test_the_registry_folder_is_learned_and_kept_after_the_file_is_gone(folders):
    _registry(folders.cfg, 4242, FOLDER_IN, str(folders.projects / "shop" / "api"))
    _registry(folders.cfg, 4243, FOLDER_OUT, str(folders.cfg / "projects" / "claude-mem" / "observer-sessions"))     # claude-mem's observer: not the user's
    owned: set = set()
    st = cost.learn_folders(folders.db, _costs(FOLDER_IN, FOLDER_OUT), owned, folders.cfg, NOW)
    assert st["registry"] == 1 and folders.db.session_cwds()[FOLDER_IN][0] == str(folders.projects / "shop" / "api")
    assert FOLDER_OUT not in folders.db.session_cwds() or folders.db.session_cwds()[FOLDER_OUT][0] == ""
    (folders.cfg / "sessions" / "4242.json").unlink()                               # the process ended: Claude removes its registry file
    rows, outside = cost.folder_rows(folders.db, _costs(FOLDER_IN), owned)
    assert [(r["project"], r["repo"], r["via"]) for r in rows] == [("shop", "api", "folder")] and outside == set()


def test_the_transcript_cwd_comes_from_the_first_lines_and_skips_corrupt_ones(folders):
    cwd = str(folders.projects / "shop" / "api" / ".claude" / "worktrees" / "t-1")
    _transcript(folders.cfg, FOLDER_WT, ["not json at all", '{"type":"summary","summary":"x"}', "[1,2]", json.dumps({"type": "user", "cwd": cwd, "message": "hi"})])
    _transcript(folders.cfg, FOLDER_BAD, ["{broken", "}{", ""])
    st = cost.learn_folders(folders.db, _costs(FOLDER_WT, FOLDER_BAD), set(), folders.cfg, NOW)
    assert st["scanned"] == 2
    known = folders.db.session_cwds()
    assert known[FOLDER_WT][0] == cwd and known[FOLDER_BAD][0] == "", "looked and found none is remembered as '', not retried at once"
    rows, _ = cost.folder_rows(folders.db, _costs(FOLDER_WT), set())
    assert [(r["project"], r["repo"]) for r in rows] == [("shop", "api")], "a task worktree maps to its repo"


def test_a_pass_reads_only_the_first_lines_of_a_capped_number_of_files_newest_first(folders, monkeypatch):
    far = json.dumps({"type": "user", "cwd": str(folders.projects / "shop" / "api")})
    ids = [f"eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee{n}" for n in range(1, 5)]
    for n, sid in enumerate(ids):
        _transcript(folders.cfg, sid, [far], age=1000 * n)                              # ids[0] is the newest
    deep = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee9"
    _transcript(folders.cfg, deep, ["{}"] * (cost.HEAD_LINES + 5) + [far])
    monkeypatch.setattr(cost, "FILES_PER_PASS", 2)
    reads = []
    real = cost.head_cwd
    monkeypatch.setattr(cost, "head_cwd", lambda p: reads.append(p.name) or real(p))
    st = cost.learn_folders(folders.db, _costs(*ids), set(), folders.cfg, NOW)
    assert st["scanned"] == 2 and st["remaining"] == 2
    assert reads == [f"{ids[0]}.jsonl", f"{ids[1]}.jsonl"], "newest first"
    assert set(folders.db.session_cwds()) == {ids[0], ids[1]}
    reads.clear()
    cost.learn_folders(folders.db, _costs(*ids), set(), folders.cfg, NOW)
    assert reads == [f"{ids[2]}.jsonl", f"{ids[3]}.jsonl"], "the next pass takes the next ones; a known id is never read again"
    reads.clear()
    monkeypatch.setattr(cost, "FILES_PER_PASS", 10)
    cost.learn_folders(folders.db, _costs(deep), set(), folders.cfg, NOW)
    assert folders.db.session_cwds()[deep][0] == "", "a cwd past line 20 is not looked for"


def test_an_empty_answer_is_retried_after_a_while_and_a_real_folder_replaces_it(folders):
    sid = FOLDER_IN
    p = _transcript(folders.cfg, sid, ["{}"])
    cost.learn_folders(folders.db, _costs(sid), set(), folders.cfg, NOW)
    assert folders.db.session_cwds()[sid][0] == ""
    p.write_text(json.dumps({"cwd": str(folders.projects / "shop" / "api")}) + "\n")
    cost.learn_folders(folders.db, _costs(sid), set(), folders.cfg, NOW)
    assert folders.db.session_cwds()[sid][0] == "", "not again within the retry window"
    later = datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc) + timedelta(seconds=cost.RETRY_EMPTY_S + 60)
    cost.learn_folders(folders.db, _costs(sid), set(), folders.cfg, later)
    assert folders.db.session_cwds()[sid][0] == str(folders.projects / "shop" / "api")
    folders.db.session_cwd_put([(sid, "/somewhere/else")], later)
    assert folders.db.session_cwds()[sid][0] == str(folders.projects / "shop" / "api"), "the first folder stands"


def test_a_board_owned_session_is_never_touched_by_the_join(folders):
    _registry(folders.cfg, 7, FOLDER_IN, str(folders.projects / "other" / "x"))
    owned = {FOLDER_IN}
    cost.learn_folders(folders.db, _costs(FOLDER_IN), owned, folders.cfg, NOW)
    assert folders.db.session_cwds() == {}
    assert cost.folder_rows(folders.db, _costs(FOLDER_IN), owned) == ([], set())


def test_codex_rows_are_left_to_the_codex_mapping(folders):
    folders.db.session_cwd_put([(FOLDER_IN, str(folders.projects / "shop" / "api"))], NOW)
    rows, outside = cost.folder_rows(folders.db, _costs(FOLDER_IN, agent="codex"), set())
    assert rows == [] and outside == set()


def _attributed(costs, rows, folder=None, outside=None):
    return cost.attribute(costs, rows, [], NOW, folder, outside)


def test_the_join_moves_grouping_and_never_the_total(folders):
    costs = {**_costs(C1, C2), **_costs(FOLDER_IN, FOLDER_WT, FOLDER_OUT, FOLDER_NONE)}
    board = [{"project": "shop", "repo": "web", "claude_session_id": C1, "agent": "claude"}]
    before = _attributed(costs, board)
    folder = [{"project": "shop", "repo": "api", "claude_session_id": FOLDER_IN, "agent": "claude", "via": "folder"},
              {"project": "shop", "repo": "api", "claude_session_id": FOLDER_WT, "agent": "claude", "via": "folder"}]
    after = _attributed(costs, board, folder, {FOLDER_OUT})
    assert after["total_all"] == before["total_all"] and after["by_agent"] == before["by_agent"], "the sum before equals the sum after"
    assert after["sessions_known"] == before["sessions_known"] == 6
    shop = after["projects"]["shop"]
    assert shop["repos"]["api"] == pytest.approx(costs[FOLDER_IN]["cost"] + costs[FOLDER_WT]["cost"]) and shop["repos"]["web"] == costs[C1]["cost"]
    assert after["attributed"] == 3 and after["joined_by_folder"] == 2 and after["outside_sessions"] == 1
    assert before["attributed"] == 1 and before["joined_by_folder"] == 0
    named = sum(p["total"] for p in after["projects"].values())
    left = sum(costs[i]["cost"] for i in (C2, FOLDER_OUT, FOLDER_NONE))
    assert named + left == pytest.approx(after["total_all"]), "unmatched work stays visible instead of being merged into a project"
    assert after["tasks"] == before["tasks"] == {}, "joined cost is never task cost"


def test_a_board_row_wins_over_the_folder_for_the_same_session(folders):
    costs = _costs(C1)
    board = [{"project": "shop", "repo": "web", "claude_session_id": C1, "agent": "claude"}]
    folder = [{"project": "blog", "repo": "site", "claude_session_id": C1, "agent": "claude", "via": "folder"}]
    r = _attributed(costs, board, folder)
    assert list(r["projects"]) == ["shop"] and r["joined_by_folder"] == 0 and r["attributed"] == 1


def test_session_samples_label_joined_outside_and_unknown_sessions(folders):
    costs = _costs(C1, FOLDER_IN, FOLDER_OUT, FOLDER_NONE)
    board = [{"project": "shop", "repo": "web", "claude_session_id": C1, "agent": "claude"}]
    folder = [{"project": "shop", "repo": "api", "claude_session_id": FOLDER_IN, "agent": "claude", "via": "folder"}]
    got = {e["id"]: e for e in cost.session_samples(costs, board, folder, {FOLDER_OUT})}
    assert (got[C1]["project"], got[C1]["repo"], got[C1]["via"]) == ("shop", "web", None)
    assert (got[FOLDER_IN]["project"], got[FOLDER_IN]["repo"], got[FOLDER_IN]["via"]) == ("shop", "api", "folder")
    assert (got[FOLDER_OUT]["project"], got[FOLDER_OUT]["repo"], got[FOLDER_OUT]["via"]) == (cost.OUTSIDE, None, "folder")
    assert (got[FOLDER_NONE]["project"], got[FOLDER_NONE]["repo"], got[FOLDER_NONE]["via"]) == (None, None, None), "no cwd: stays unattributed"
    assert sum(e["cost"] for e in got.values()) == pytest.approx(sum(c["cost"] for c in costs.values()))
    plain = cost.session_samples(costs, board)
    assert [e["cost"] for e in plain] == [e["cost"] for e in cost.session_samples(costs, board, folder, {FOLDER_OUT})]


def test_refresh_joins_by_folder_end_to_end_and_keeps_it_after_the_registry_file_is_gone(client, monkeypatch, tmp_path):
    from app import main
    cfg = settings.claude_config_dir
    (settings.projects_dir / "shop" / "api").mkdir(parents=True, exist_ok=True)
    costs = _costs(FOLDER_IN, FOLDER_OUT, FOLDER_NONE, C1)
    monkeypatch.setattr(cost, "fetch_sessions", lambda: dict(costs))
    _registry(cfg, 501, FOLDER_IN, str(settings.projects_dir / "shop" / "api" / "src"))
    _transcript(cfg, FOLDER_OUT, [json.dumps({"cwd": "/somewhere/else/entirely"})])
    _transcript(cfg, FOLDER_NONE, ['{"type":"summary"}'])
    total = sum(c["cost"] for c in costs.values())
    r = cost.refresh(main.db)
    assert r["total_all"] == round(total, 2) and r["joined_by_folder"] == 1 and r["outside_sessions"] == 1
    assert r["projects"]["shop"]["repos"]["api"] == costs[FOLDER_IN]["cost"]
    m = {sid: (main.db.sample_last("cost", f"claude:{sid}") or {}).get("meta") for sid in costs}
    assert (m[FOLDER_IN]["p"], m[FOLDER_IN]["r"], m[FOLDER_IN]["j"]) == ("shop", "api", 1)
    assert (m[FOLDER_OUT]["p"], m[FOLDER_OUT]["j"]) == (cost.OUTSIDE, 1) and "r" not in m[FOLDER_OUT]
    assert "p" not in m[FOLDER_NONE] and "j" not in m[FOLDER_NONE], "no folder: unattributed, nothing guessed"
    assert "p" not in m[C1], "a session no board row and no folder knows"
    (cfg / "sessions" / "501.json").unlink()
    again = cost.refresh(main.db)
    assert again["projects"]["shop"]["repos"]["api"] == costs[FOLDER_IN]["cost"], "kept after the registry file is gone"
    assert again["total_all"] == r["total_all"]
    for t in main.db.tasks(include_archived=True):
        assert t["cost_usd"] in (None, 0, 0.0), "joined cost never reaches a task"


def test_a_session_attributed_later_gets_one_new_row_even_though_its_cost_did_not_change(folders):
    sid = FOLDER_IN
    costs = _costs(sid)
    entries = cost.session_samples(costs, [])
    assert samples.record_cost(folders.db, {"sessions": entries}, NOW) == 1
    assert "p" not in (folders.db.sample_last("cost", f"claude:{sid}")["meta"] or {})
    assert samples.record_cost(folders.db, {"sessions": entries}, NOW + timedelta(hours=1)) == 0, "nothing changed: nothing written"
    folder = [{"project": "shop", "repo": "api", "claude_session_id": sid, "agent": "claude", "via": "folder"}]
    entries = cost.session_samples(costs, [], folder)
    assert samples.record_cost(folders.db, {"sessions": entries}, NOW + timedelta(hours=1, minutes=1)) == 1
    meta = folders.db.sample_last("cost", f"claude:{sid}")["meta"]
    assert (meta["p"], meta["r"], meta["j"]) == ("shop", "api", 1)
    assert samples.record_cost(folders.db, {"sessions": entries}, NOW + timedelta(hours=2)) == 0, "and then quiet again"


def test_the_usage_summary_counts_the_joined_part_and_names_the_outside_bucket(folders):
    from app import usage_summary
    db = folders.db
    day = NOW
    entries = cost.session_samples(_costs(C1, FOLDER_IN, FOLDER_OUT, FOLDER_NONE),
                                   [{"project": "shop", "repo": "web", "claude_session_id": C1, "agent": "claude"}],
                                   [{"project": "shop", "repo": "api", "claude_session_id": FOLDER_IN, "agent": "claude", "via": "folder"}], {FOLDER_OUT})
    samples.record_cost(db, {"sessions": entries}, day)
    s = usage_summary.build(db, days=30, tz_min=0, now=day)
    rows = {r["project"]: r for r in s["windows"]["30d"]["by_project"]}
    assert rows["shop"]["total"] == pytest.approx(1.0 + 2.0) and rows["shop"]["joined"] == pytest.approx(2.0), "the part of the project joined by folder"
    assert cost.OUTSIDE in rows and "joined" in rows[cost.OUTSIDE] and rows[cost.OUTSIDE]["joined"] > 0
    assert usage_summary.UNATTRIBUTED in rows and "joined" not in rows[usage_summary.UNATTRIBUTED]
    assert usage_summary.OUTSIDE == cost.OUTSIDE
    total = sum(r["total"] for r in rows.values())
    assert total == pytest.approx(s["windows"]["30d"]["total"]), "only grouping: the buckets add up to the window"
    top = {t["key"]: t for t in s["top_sessions"]}
    assert top[f"claude:{FOLDER_IN}"]["via"] == "folder" and "via" not in top[f"claude:{C1}"]


def test_corrupt_or_odd_map_rows_cannot_break_a_refresh(folders, monkeypatch):
    class Broken:
        def session_cwds(self):
            raise RuntimeError("db is down")
    assert cost.folder_rows(Broken(), _costs(FOLDER_IN), set()) == ([], set())
    assert cost.learn_folders(Broken(), _costs(FOLDER_IN), set(), folders.cfg, NOW) == {"registry": 0, "scanned": 0, "remaining": 0}
