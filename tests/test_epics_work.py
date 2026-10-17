"""Nodes epic P14, issue #149: run a claimed epic issue as a task on its node and report back on the issue (app/nodes_work.py, the relay row `epic_work`, the writer's progress
comments, the task result hook of app/taskflow.py).

Two in-process boards (`split_boards`, each with its own projects and its own fake tmux): board a is the hub, board b the node that works the issue. GitHub is tests/fake_github.py
served from this process (no gh, no network); the claim protocol's waits and the write queue run on the fake server clock, so nothing sleeps for real and `nodes_work.clock` is that
clock too. claude is a fake path, tmux the conftest fake, git repos are empty temp repos whose `origin` is a GitHub remote (SSH spelling by default). The report runs synchronously
(`nodes_work.spawn` is replaced by a direct call). Nothing here reaches the real gh, claude, codex, tailscale, tmux or the network.

Pinned: with CCBOARD_EPIC_WRITES off the route refuses before any claim, task or gh command; the author check (the issue AND its epic) is the first guard and no claim comment exists when
it refuses; only repositories cloned on the node are worked (SSH and HTTPS spellings of `origin` both match); the launch is built from the validated model line through the safe launch
path, a bypass spelling is refused with a comment and the claim is released as failed, an issue with no model line gets the node's defaults; the issue text is data in the prompt;
comments are fixed templates, at most one per five minutes and only on a change of phase, each fenced, and carry no path, host name or token; a result is posted only for a `summary`
report, once redacted and cut; a lost claim posts nothing and parks the task; a failure frees the issue, the third asks for a person; `auto_pr` pushes only the task's own branch.
"""
from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import claude_auth, doctor, github, github_write, gitops, issues, main, mcp, node_state, nodes, nodes_epics, nodes_relay as nr, nodes_work, taskflow
from app.config import settings
from tests.fake_github import SECRET, START, FakeGitHub

REPO = "acme/shop"
MAC = "/Us" + "ers/"                                           # a macOS home prefix, built from fragments: the repository holds no such path as text
EPIC = f"{REPO}#147"
FIX = Path(__file__).parent / "fixtures"
ROW = nr.BY_NAME["epic_work"]
WORK_ROUTE = "/api/nodes/{handle}/epics/work"
PROMPT_MARK = "MARKER-prompt-91c2e4"


def fixture(name: str) -> dict:
    return json.loads((FIX / f"issue_work_{name}.json").read_text())


class Count:
    def __init__(self, inner):
        self.inner, self.seen, self.paths = inner, 0, []

    def __call__(self, target, method, path, headers, body, timeout):
        self.seen += 1
        self.paths.append((method, path, timeout))
        return self.inner(target, method, path, headers, body, timeout)


class FakeHub:
    def __init__(self, **rec):
        self.rec = {"status": "online", "age_s": 3, "polled_at": "2026-10-10T00:00:00Z", "last_ok_at": "2026-10-10T00:00:00Z", **rec}

    def records(self, handle=None):
        return [self.rec]

    def rows(self):
        return [self.rec]


@pytest.fixture(autouse=True)
def _hub_online(monkeypatch):
    monkeypatch.setattr(main, "_hub", lambda: FakeHub())
    yield
    main._invalidate_scan()


@pytest.fixture(autouse=True)
def _no_reservation_left(monkeypatch):
    monkeypatch.setattr(nodes_work, "_starting", 0)


@pytest.fixture(autouse=True)
def _wide(monkeypatch):
    big = lambda: nodes._Limiter(10 ** 6, 60.0, 64)
    for n in ("node_read_limiter", "node_write_limiter", "relay_read_limiter", "relay_write_limiter"):
        monkeypatch.setattr(nodes, n, big())


def git_init(path: Path, origin: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


def add_child(gh, n: int, fix: str = "full", **kw) -> str:
    f = fixture(fix)
    k = gh.add_issue(REPO, n, f["title"], body=f["body"], user=kw.pop("user", f["user"]), **kw)
    gh.link(EPIC, k)
    return k


@pytest.fixture
def w(split_boards, pair_up, tmp_path, monkeypatch):
    """Boards a (hub) and b (the node) paired with every scope. b has shop/api, a clone of acme/shop (an SSH `origin`), a fake claude and a fake tmux. GitHub in memory with one epic (#147)
    by alice (gh's login, so the only allowed author) and the children #201 to #204 (alice, the full model line). Writes ON, the clock the fake server's."""
    gh = FakeGitHub(tmp_path).use_memory(monkeypatch)
    clock = lambda: gh.world["now"] - START + 1000.0
    slept = []

    def sleep(s):
        slept.append(s)
        gh.world["now"] += s
    monkeypatch.setattr(nodes_epics, "sleep", sleep)
    monkeypatch.setattr(nodes_epics, "mono", clock)
    monkeypatch.setattr(github.reader, "mono", clock)
    monkeypatch.setattr(github_write, "queue", github_write.Queue(mono=clock, sleep=sleep))
    monkeypatch.setattr(nodes_work, "clock", lambda: float(gh.world["now"]))
    monkeypatch.setattr(nodes_work, "spawn", lambda fn: fn())
    monkeypatch.setattr(settings, "epic_writes", True)
    monkeypatch.setattr(settings, "epic_alias", "")
    monkeypatch.setattr(settings, "epic_authors", ())
    monkeypatch.setattr(settings, "epic_lease_min", 30)
    monkeypatch.setattr(settings, "node_lanes", 3)
    monkeypatch.setattr(nodes_epics, "RENEWER", False)
    monkeypatch.setattr(nodes_epics, "READ_CHILD_MAX_AGE", 10 ** 9)
    monkeypatch.setattr(nodes_epics, "host_names", lambda: {"mybox"})
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(claude_auth, "status", lambda: {"installed": True, "version": "2.1.287 (Claude Code)", "loggedIn": True, "authMethod": "claude.ai", "email": "me@example.com"})
    monkeypatch.setattr(claude_auth, "version", lambda: "2.1.287 (Claude Code)")
    from app.agents import registry
    monkeypatch.setattr(registry.plat, "ppid", lambda pid: None)
    registry.invalidate()
    node_state.slug_cache_clear()
    repo_b = git_init(split_boards.b_projects / "shop" / "api", "git@github.com:acme/shop.git")
    git_init(split_boards.a_projects / "shop" / "api", "git@github.com:acme/shop.git")          # the hub has a clone too: its page saves the epic's settings
    gh.add_issue(REPO, 147, "The epic", labels=["ccboard:epic"], user="alice")
    for n in range(201, 205):
        add_child(gh, n)
    reg = pair_up()
    count = Count(split_boards.transport)
    nodes.peer_transport = count
    nodes_epics.reset()
    posted = []
    yield SimpleNamespace(two=split_boards, a=split_boards.a, b=split_boards.b, reg=reg, h=reg["handle"], count=count, gh=gh, slept=slept, repo_b=repo_b, posted=posted, clock=clock,
                          mp=monkeypatch, tmp=tmp_path)
    nodes_epics.stop_renewals(2.0)
    registry.invalidate()
    node_state.slug_cache_clear()


# ---- helpers

def run_route(w, issue: int = 201, body: dict | None = None, **kw):
    """Run: the hub's route as a signed-in person calls it."""
    return w.a.post(WORK_ROUTE.format(handle=w.h), json=body if body is not None else {"repo": REPO, "issue": issue, "epic": 147}, **kw)


def work(w, issue: int = 201, **kw):
    """The library call, on board b (what the peer route runs)."""
    with w.b.enter():
        return nodes_work.work_issue(REPO, issue, epic=kw.pop("epic", 147), **kw)


def b_task(w, tid: int) -> dict:
    return w.b.db.task_get(tid)


def thread(w, n: int = 201) -> list[dict]:
    return w.gh.thread(f"{REPO}#{n}")


def progress(w, n: int = 201) -> list[str]:
    return [c["body"] for c in thread(w, n) if c["body"].startswith(github_write.PROGRESS_MARKER)]


def claims(w, n: int = 201) -> list[dict]:
    return [nodes_epics.parse_claim(c["body"]) for c in w.gh.claims_on(f"{REPO}#{n}")]


def writes_of(w, kind: str) -> list[tuple]:
    return [x for x in w.gh.writes() if x[0] == kind]


def stop_turn(w, tid: int, text: str = "All done.", failed: bool = False):
    """The agent's turn ends (the Stop hook): the board's own runtime credits the task, which calls the result hooks."""
    with w.b.enter():
        t = w.b.db.task_get(tid)
        row = w.b.db.open_row(t["tmux_name"])
        return main._taskflow().on_turn_end(t["tmux_name"], row, text if not failed else None, failed=failed, message=text if failed else None)


def beat(w, **kw):
    """One beat of the renew timer for every claim board b holds (what nodes_epics._renew_loop calls)."""
    with w.b.enter():
        actor = nodes_epics.board_actor()
        for v in nodes_epics.held_claims(actor):
            if v["state"] == "held":
                nodes_work.on_beat(actor, v)


def rec_of(w, n: int = 201) -> dict | None:
    return nodes_work._load(w.b.db, "acme", "shop", n)


def audit(board, **kw):
    return board.db.node_audit_list(500, **kw)


# ================================================================ the happy path

def test_a_run_claims_starts_the_task_and_answers_the_four_facts(w):
    r = run_route(w)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert r.json()["node"] == "node-b" and d["claimed"] is True and d["reason"] == "won" and d["repo"] == REPO and d["issue"] == 201
    assert d["task_ref"] == f"node-b:{d['id']}" and d["branch"].startswith("worktree-") and d["tmux"]
    assert set(d) == {"claimed", "reason", "task_ref", "id", "branch", "tmux", "repo", "issue"}
    t = b_task(w, d["id"])
    assert t["issue_number"] == 201 and t["issue_url"] == "https://github.com/acme/shop/issues/201" and t["phase"] == "running" and t["branch"] == d["branch"]
    assert json.loads(t["origin"])["node"] == "node-a", "a task a paired node asked for says so"
    assert w.two.tmux_b["created"] and not w.two.tmux_a["created"], "the session is on the node, never on the hub"
    [c] = claims(w)
    assert c["status"] == "running" and c["task_ref"] == str(d["id"]) and c["branch"] == d["branch"], "the claim says running, with the task and the branch"
    assert w.gh.labels_of(f"{REPO}#201") == ["ccboard:in-progress"]
    assert progress(w) == [], "started is the claim comment itself: no extra comment"
    rec = rec_of(w)
    assert rec["state"] == "active" and rec["task_id"] == d["id"] and nodes_epics.parse_ref(rec["claim_ref"]) and rec["report"] == "phase" and rec["auto_pr"] is False and rec["close"] is False
    assert w.count.paths == [("POST", "/api/node/epics/work", nr.WORK_TIMEOUT)]


def test_a_finished_task_posts_done_once_sets_the_claim_done_and_takes_the_label_off(w):
    tid = run_route(w).json()["data"]["id"]
    out = stop_turn(w, tid, "Everything is in place.")
    assert out["phase"] == "done"
    assert b_task(w, tid)["phase"] == "done"
    [body] = progress(w)
    assert re.fullmatch(rf"{re.escape(github_write.PROGRESS_MARKER)}\nccboard [a-z]+-[0-9a-f]{{2}}: done\nBranch: worktree-[a-z0-9-]+", body), body
    assert "Everything is in place" not in body, "the default report posts phases only: no result text"
    [c] = claims(w)
    assert c["status"] == "done"
    assert w.gh.labels_of(f"{REPO}#201") == [] and w.gh.assignees_of(f"{REPO}#201") == []
    rec = rec_of(w)
    assert rec["state"] == "finished" and rec["phase"] == "done"
    stop_turn(w, tid, "Again.")                                                  # a second Stop of the same task credits nobody: no second comment
    assert len(progress(w)) == 1
    assert nodes_epics.held_claims(nodes_epics.Actor(w.b.db, "", None, "")) == [], "the claim record is gone with the release"


def test_both_boards_audit_the_repository_and_the_issue_number_only(w):
    assert run_route(w).status_code == 200
    [out] = audit(w.a, action="work_issue")
    assert out["direction"] == "out" and out["status"] == "ok" and out["target"] == "work acme/shop#201"
    [inn] = audit(w.b, action="work_issue")
    assert inn["direction"] == "in" and inn["status"] == "ok" and inn["target"] == "work acme/shop#201"
    blob = json.dumps(audit(w.a) + audit(w.b), default=str)
    for secret in (SECRET, "ghp_", "ccbnode_", "Authorization", "<!--", "Login page", "login page", "sonnet"):
        assert secret not in blob


# ================================================================ writes off: nothing at all

def test_with_writes_off_the_route_refuses_before_any_claim_task_or_gh_command(w, monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", False)
    ran = []
    real_run = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a) or real_run(*a, **k))
    w.gh.clear_calls()
    r = run_route(w)
    assert r.status_code == 409 and r.json()["reason"] == "work_refused" and r.json()["why"] == "writes_off", r.text
    assert "CCBOARD_EPIC_WRITES" in r.json()["error"] and "node-b" in r.json()["error"]
    assert w.gh.calls() == [], "not even a read, not even the login probe"
    assert w.b.db.tasks() == [] and w.two.tmux_b["created"] == [] and nodes_work.has_records(w.b.db) is False
    assert not [x for x in ran if x and isinstance(x[0], list) and x[0][:2] == ["git", "worktree"]], "no worktree was made"
    [inn] = audit(w.b, action="work_issue")
    assert inn["status"] == "failed" and inn["detail"] == "verified; work_refused: writes_off" and inn["target"] == "work acme/shop#201"


def test_the_library_refuses_first_when_writes_are_off(w, monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", False)
    with pytest.raises(nodes_work.Refused) as e:
        work(w)
    assert e.value.reason == "writes_off" and w.gh.calls() == []


# ================================================================ the author check: the first guard

def test_an_issue_by_a_login_off_the_list_is_refused_before_any_claim_comment_exists(w):
    add_child(w.gh, 210, user="mallory")
    r = run_route(w, 210)
    assert r.status_code == 409 and r.json()["why"] == "author_not_allowed" and r.json()["author"] == "mallory", r.text
    assert r.json()["error"] == "node-b: the issue is authored by mallory, not on the allowed list."
    assert w.gh.claims_on(f"{REPO}#210") == [] and w.gh.thread(f"{REPO}#210") == [] and w.gh.writes() == [], "no comment of any kind, no write"
    assert w.b.db.tasks() == [] and w.two.tmux_b["created"] == [] and nodes_epics.held_claims(nodes_epics.Actor(w.b.db, "", None, "")) == []


def test_an_epic_by_a_login_off_the_list_refuses_all_its_children_before_any_claim(w):
    w.gh.world["issues"][EPIC]["user"] = "mallory"
    r = run_route(w, 201)
    assert r.status_code == 409 and r.json()["why"] == "epic_author_not_allowed" and r.json()["author"] == "mallory", r.text
    assert "the epic is authored by mallory, not on the allowed list" in r.json()["error"]
    assert w.gh.writes() == [] and w.b.db.tasks() == []


@pytest.mark.parametrize("kind", ["Bot", "Organization"])
def test_an_author_that_is_not_a_user_is_not_an_allowed_author(w, kind):
    w.gh.world["issues"][f"{REPO}#202"]["user_type"] = kind
    r = run_route(w, 202)
    assert r.status_code == 409 and r.json()["why"] == "author_not_allowed" and w.gh.writes() == []


def test_the_default_list_holds_only_the_nodes_own_login_and_the_configured_list_replaces_it(w, monkeypatch):
    add_child(w.gh, 211, user="bob")
    assert run_route(w, 211).json()["why"] == "author_not_allowed", "a collaborator is not on the default list"
    assert nodes_epics.allowed_authors("alice") == {"alice"}
    monkeypatch.setattr(settings, "epic_authors", ("alice", "bob"))
    assert run_route(w, 211).status_code == 200, "CCBOARD_EPIC_AUTHORS names who may write issues the nodes run"
    monkeypatch.setattr(settings, "epic_authors", ("bob",))
    assert run_route(w, 201).json()["why"] == "login_not_allowed", "this node's own login must count too, or its claims would not"


def test_an_unreadable_issue_is_refused_and_nothing_is_written(w):
    w.gh.fail("/repos/acme/shop/issues/201", 500, times=-1, method="GET")
    r = run_route(w)
    assert r.status_code == 409 and r.json()["why"] == "unreadable" and w.gh.writes() == []


def test_the_epic_page_marks_children_not_by_an_allowed_author_before_run(w):
    add_child(w.gh, 212, user="mallory")
    add_child(w.gh, 213, user="mallory", state="closed")
    with w.b.enter():
        d = nodes_epics.detail(w.b.db, "acme", "shop", 147, [], repos=[REPO], login="alice")
    kids = {c["number"]: c for c in d["children"]}
    assert kids[201]["author"] == "alice" and kids[201]["author_ok"] is True and kids[212]["author"] == "mallory" and kids[212]["author_ok"] is False
    assert d["authors"]["allowed"] == ["alice"] and d["authors"]["epic"] == "alice" and d["authors"]["epic_ok"] is True
    assert d["authors"]["blocked"] == [{"number": 212, "author": "mallory"}], "an open child only: a closed one does not run"
    assert d["authors"]["text"] == "These issues are not by an allowed author and will be skipped: #212."
    w.gh.world["issues"][EPIC]["user"] = "mallory"
    nodes_epics.reset()
    with w.b.enter():
        d = nodes_epics.detail(w.b.db, "acme", "shop", 147, [], repos=[REPO], login="alice")
    assert d["authors"]["epic_ok"] is False and d["authors"]["text"] == "The epic is not by an allowed author, so nothing in it will run."


# ================================================================ the repository on this node

@pytest.mark.parametrize("origin", ["git@github.com:acme/shop.git", "https://github.com/acme/shop.git", "https://github.com/acme/shop", "ssh://git@github.com/acme/shop.git"])
def test_ssh_and_https_spellings_of_origin_both_match(w, origin):
    subprocess.run(["git", "-C", str(w.repo_b), "remote", "set-url", "origin", origin], check=True)
    node_state.slug_cache_clear()
    main._invalidate_scan()
    r = run_route(w)
    assert r.status_code == 200, (origin, r.text)


def test_local_repo_matches_the_remote_by_owner_and_name_whatever_the_case_and_the_spelling(w):
    projs = [{"name": "shop", "repos": [{"name": "api", "path": str(w.repo_b), "state": "ok"}, {"name": "gone", "path": str(w.repo_b), "state": "missing"}]},
             {"name": "root-only", "repos": [{"name": "root", "path": str(w.repo_b), "state": "project"}]}]
    for origin in ("https://github.com/Acme/Shop.git", "git@github.com:acme/SHOP.git"):
        subprocess.run(["git", "-C", str(w.repo_b), "remote", "set-url", "origin", origin], check=True)
        node_state.slug_cache_clear()
        assert nodes_work.local_repo("acme/shop", projs) == ("shop", "api"), origin
    assert nodes_work.local_repo("acme/other", projs) is None and nodes_work.local_repo("acme/shop", []) is None


def test_a_repository_that_is_not_cloned_here_is_404_repo_not_here_and_no_claim_exists(w):
    w.gh.add_issue("acme/other", 5, "Elsewhere")
    w.gh.clear_calls()
    r = run_route(w, body={"repo": "acme/other", "issue": 5, "epic": 147})
    assert r.status_code == 404 and r.json()["reason"] == "repo_not_here" and "not cloned on this node" in r.json()["error"], r.text
    assert w.gh.writes() == [] and w.gh.thread("acme/other#5") == []
    subprocess.run(["git", "-C", str(w.repo_b), "remote", "remove", "origin"], check=True)
    node_state.slug_cache_clear()
    main._invalidate_scan()
    r = run_route(w)
    assert r.status_code == 404 and r.json()["reason"] == "repo_not_here" and w.gh.writes() == [] and w.gh.thread(f"{REPO}#201") == [], "no clone, no claim"
    assert w.b.db.tasks() == []
    assert audit(w.b, action="work_issue")[0]["detail"] == "verified; repo_not_here: repo_not_here"


def test_what_a_peer_or_github_names_never_chooses_another_repository(w):
    for body in ({"repo": "acme/shop", "issue": 201, "epic": "elsewhere/repo#9"}, {"repo": "acme/shop", "issue": 299, "epic": 147}, {"repo": "acme/shop", "issue": 147, "epic": 147}):
        r = run_route(w, body=body)
        assert r.status_code == 409 and r.json()["why"] in ("other_repo", "not_epic_child"), (body, r.text)
    assert w.gh.writes() == [] and w.b.db.tasks() == []

# ================================================================ the model line: agent, model, effort and posture

def typed_line(w, tid: int | None = None) -> list[str]:
    """The tokens of the launch line the node typed into its fake tmux (the last agent line), as a shell would read them."""
    import shlex
    lines = [t for _n, t in w.two.tmux_b["sent"] if t.startswith(("claude", "codex"))]
    return shlex.split(lines[-1])


def flag(tokens: list[str], name: str):
    return tokens[tokens.index(name) + 1] if name in tokens else None


def test_a_claude_line_maps_to_model_effort_and_the_permission_mode_on_the_launch_line(w):
    tid = run_route(w).json()["data"]["id"]
    toks = typed_line(w)
    assert toks[0] == "claude" and flag(toks, "--model") == "sonnet" and flag(toks, "--effort") == "medium" and flag(toks, "--permission-mode") == "acceptEdits"
    assert "--dangerously-skip-permissions" not in toks[:toks.index("--")] and "bypassPermissions" not in toks[:toks.index("--")]
    spec = json.loads(b_task(w, tid)["spec"])
    assert spec["model"] == "sonnet" and spec["effort"] == "medium" and spec["permission_mode"] == "acceptEdits" and spec["subagent_model"] == "inherit"


def test_an_issue_with_no_model_line_gets_the_nodes_defaults_and_says_its_permissions_explicitly(w):
    add_child(w.gh, 220, "none")
    tid = run_route(w, 220).json()["data"]["id"]
    toks = typed_line(w)
    assert toks[0] == "claude" and "--model" not in toks and flag(toks, "--permission-mode") == "manual", "the launch never inherits its mode from settings.json"
    spec = json.loads(b_task(w, tid)["spec"])
    assert "model" not in spec and spec["permission_mode"] == "manual"


def test_a_codex_line_maps_to_model_reasoning_sandbox_and_approval(w, monkeypatch):
    monkeypatch.setattr(nodes_work, "_installed", lambda a: True)
    agent, fields, kw = nodes_work.launch_of(fixture("codex_only")["body"])
    assert agent == "codex" and fields == {"model": "gpt-6.1-sol", "reasoning_effort": "low"}
    assert kw["opts"] == {"sandbox": "workspace-write", "approval": "on-request"} and kw["permission_mode"] == "default"
    agent, fields, kw = nodes_work.launch_of(fixture("full")["body"])
    assert agent == "claude", "both lines: Claude first"
    monkeypatch.setattr(nodes_work, "_installed", lambda a: a == "codex")
    agent, fields, kw = nodes_work.launch_of(fixture("full")["body"])
    assert agent == "codex" and fields == {"model": "gpt-6.1-sol", "reasoning_effort": "medium"}, "Claude is not installed on this node: the Codex line runs"
    with w.b.enter():                                                           # the request it makes passes the guard on the final options, as a remote launch does
        req = main.TaskCreateIn(project="shop", repo="api", title="x", prompt="y", when="now", agent=agent, **fields, **kw)
        nr._require_final(main, req)
        assert nr.guard_final(req.model_dump(include=set(main.TASK_SPEC_KEYS) | {"agent"})) == []


def test_an_agent_that_is_not_installed_falls_back_to_the_other_and_then_to_claude(w, monkeypatch):
    monkeypatch.setattr(nodes_work, "_installed", lambda a: False)
    assert nodes_work.launch_of(fixture("codex_only")["body"])[0] == "claude"
    assert nodes_work.launch_of(None)[0] == "claude"


@pytest.mark.parametrize("name", ["bypass_claude", "bypass_mode", "bypass_codex", "bypass_yolo", "bypass_bare", "bypass_second_line"])
def test_a_model_line_naming_bypass_is_refused_the_claim_is_released_as_failed_and_the_refusal_is_commented(w, name):
    add_child(w.gh, 230, name)
    r = run_route(w, 230)
    assert r.status_code == 409 and r.json()["why"] == "bypass_refused" and "asks for bypass permissions" in r.json()["error"], r.text
    [c] = claims(w, 230)
    assert c["status"] == "failed" and c["attempt"] == 1, "the claim is released as failed"
    [body] = progress(w, 230)
    assert re.fullmatch(rf"{re.escape(github_write.PROGRESS_MARKER)}\nccboard [a-z]+-[0-9a-f]{{2}}: refused: the issue asks for bypass", body), body
    assert w.gh.labels_of(f"{REPO}#230") == [] and w.gh.assignees_of(f"{REPO}#230") == []
    assert w.b.db.tasks() == [] and w.two.tmux_b["created"] == [], "no task, no session, no worktree"
    assert nodes_work.has_records(w.b.db) is False
    assert any(a["target"] == "acme/shop#230" and a["detail"] == "bypass_refused" for a in audit(w.b, action="epic-work"))


@pytest.mark.parametrize("spelling", ["--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox", "--yolo", "danger-full-access",
                                      "bypassPermissions", "BYPASSPERMISSIONS", "--Dangerously-Skip-Permissions", "--dangerously‑skip-permissions", "bypass​Permissions",
                                      "danger–full–access", "ｂypassPermissions"])
def test_every_refused_spelling_is_found_on_a_model_line_whatever_the_dashes_case_and_invisible_characters(spelling):
    body = f"## Who should do it\n\n- Claude Code: `claude --model sonnet {spelling}`\n"
    with pytest.raises(nodes_work.Refused) as e:
        nodes_work.launch_of(body)
    assert e.value.reason == "bypass_refused"
    with pytest.raises(nodes_work.Refused):
        nodes_work.launch_of(f"## Who should do it\n\n- Codex: use {spelling} here\n")


def test_prose_in_the_section_that_forbids_bypass_is_not_a_model_line():
    nodes_work.launch_of(fixture("full")["body"])                              # "Never `bypassPermissions`, never `danger-full-access`" stands in this repository's own issues
    nodes_work.launch_of(fixture("hostile")["body"])                           # a spelling in the prose outside the section is text for the agent to read


@pytest.mark.parametrize("line", ["claude --permission-mode dontAsk", "claude --permission-mode auto", "claude --model sonnet --permission-mode acceptAll",
                                  "codex -m gpt-6.1-sol -s workspace-write -a never", "codex -m gpt-6.1-sol -a untrusted", "codex -s read-write"])
def test_a_permission_setting_outside_what_another_node_may_use_is_refused_with_its_own_word(line, monkeypatch):
    monkeypatch.setattr(nodes_work, "_installed", lambda a: True)
    head = "Claude Code" if line.startswith("claude") else "Codex"
    with pytest.raises(nodes_work.Refused) as e:
        nodes_work.launch_of(f"## Who should do it\n\n- {head}: `{line}`\n")
    assert e.value.reason == "posture_refused"


def test_a_posture_refusal_comments_and_releases_the_claim_as_failed(w):
    add_child(w.gh, 231, "posture")
    r = run_route(w, 231)
    assert r.status_code == 409 and r.json()["why"] == "posture_refused", r.text
    assert claims(w, 231)[0]["status"] == "failed"
    [body] = progress(w, 231)
    assert body.endswith("refused: the issue asks for a permission setting this node does not use") and w.b.db.tasks() == []


def test_a_model_or_effort_that_is_not_a_plain_word_is_not_launched():
    for line in ("claude --model 'a b'", "claude --model $(id)", "claude --effort 'x;y'"):
        agent, fields, kw = nodes_work.launch_of(f"## Who should do it\n\n- Claude Code: `{line}`\n")
        assert all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]*", v) for v in fields.values()), (line, fields)


# ================================================================ the issue text is data

def test_the_issue_title_and_body_go_into_the_prompt_as_data_never_into_a_shell_or_a_name(w):
    import shlex
    add_child(w.gh, 240, "hostile")
    r = run_route(w, 240)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    line = [t for _n, t in w.two.tmux_b["sent"] if t.startswith("claude")][-1]
    toks = shlex.split(line)
    t = b_task(w, d["id"])
    assert toks[-1] == t["prompt"] and toks[toks.index("--") + 1:] == [t["prompt"]], "one argument after `--`: the shell reads the prompt as one word"
    assert toks[:toks.index("--")][0] == "claude" and not any(x in toks[:toks.index("--")] for x in ("$(touch", "rm", ";")), "nothing of the issue is in the options"
    assert t["prompt"].startswith("Work on GitHub issue #240: ") and "$(touch /tmp/pwned)" in t["prompt"], "the text is there, as text"
    assert "HIDDEN" not in t["prompt"] and "curl http://example.com" not in t["prompt"], "a hidden HTML comment is not what a person saw, so the agent does not get it"
    assert "​" not in t["prompt"] and "‮" not in t["prompt"] and "\u0007" not in t["prompt"]
    assert re.fullmatch(r"[a-z0-9-]+", t["slug"]) and re.fullmatch(r"worktree-[a-z0-9-]+", t["branch"]) and re.fullmatch(r"shop--api--t-[a-z0-9-]+", t["tmux_name"])
    assert not Path("/tmp/pwned").exists()
    for bad in ("node-a", "node-b", str(w.two.b_projects), "100.64", SECRET, "alice@"):
        assert bad not in t["prompt"], bad
    assert t["prompt"].endswith("Work only in this worktree. Commit your changes to this branch when you are done. Do not push unless asked.")
    assert "epic #147" in t["prompt"] and re.search(r"the claim is [a-z]+-[0-9a-f]{2}\.", t["prompt"])


def test_the_prompt_never_starts_like_a_command_and_is_capped_whatever_the_body(w):
    f = fixture("none")
    w.gh.add_issue(REPO, 241, "/etc/passwd", body="!rm -rf\n#note\n" + "x" * 40000, user="alice")
    w.gh.link(EPIC, f"{REPO}#241")
    r = run_route(w, 241)
    assert r.status_code == 200, r.text
    t = b_task(w, r.json()["data"]["id"])
    assert t["prompt"].startswith("Work on GitHub issue #241: /etc/passwd") and len(t["prompt"]) <= nr.TASK_PROMPT_IN
    assert t["prompt"].endswith("Do not push unless asked.")


def test_a_long_title_still_gives_a_branch_a_claim_comment_can_name(w):
    w.gh.add_issue(REPO, 242, "A very long title that goes on and on about the checkout flow and its many details " * 3, user="alice", body="x")
    w.gh.link(EPIC, f"{REPO}#242")
    r = run_route(w, 242)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert len(d["branch"]) <= 40 and re.fullmatch(github_write.BRANCH_RE, d["branch"])
    [c] = claims(w, 242)
    assert c["status"] == "running" and c["branch"] == d["branch"] and c["task_ref"] == str(d["id"]), "the claim names the branch: start() did not refuse it"
    assert b_task(w, d["id"])["title"].startswith("Issue #242: A very long title")


# ================================================================ the comments: a few, short, fenced, on a change of phase, once in five minutes

def ask_person(w, tid: int, age_s: float) -> int:
    """A permission request of the task's session that nobody has answered, opened `age_s` seconds ago on the fake clock."""
    with w.b.enter():
        t = w.b.db.task_get(tid)
        pid = w.b.db.perm_add(t["tmux_name"], "Bash", "ls", {})
        with w.b.db.lock:
            w.b.db.conn.execute("UPDATE permissions SET created_at=? WHERE id=?", (taskflow._iso(w.gh.world["now"] - age_s), pid))
    return pid


def answer_person(w, pid: int) -> None:
    with w.b.enter():
        assert w.b.db.perm_decide(pid, "allow", "test")


def test_waiting_for_a_person_is_commented_after_ten_minutes_of_an_open_request_not_before(w):
    tid = run_route(w).json()["data"]["id"]
    ask_person(w, tid, 9 * 60)
    beat(w)
    assert progress(w) == [] and rec_of(w)["phase"] == "running", "nine minutes is not ten"
    w.gh.tick(61)
    beat(w)
    [body] = progress(w)
    assert re.fullmatch(rf"{re.escape(github_write.PROGRESS_MARKER)}\nccboard [a-z]+-[0-9a-f]{{2}}: waiting for a person\nBranch: worktree-[a-z0-9-]+", body), body
    assert rec_of(w)["phase"] == "waiting" and nodes_work.work_views(w.b.db, [(REPO, 201)])[(REPO, 201)]["waiting"] is True
    assert claims(w)[0]["status"] == "running", "the issue is not released while it waits: nothing answers for the person"
    beat(w)
    assert len(progress(w)) == 1, "the same phase is not said twice"


def test_comments_are_at_most_one_per_five_minutes_and_only_on_a_change_of_phase(w):
    tid = run_route(w).json()["data"]["id"]
    pid = ask_person(w, tid, 11 * 60)
    beat(w)
    assert len(progress(w)) == 1
    answer_person(w, pid)
    beat(w)
    assert rec_of(w)["phase"] == "running" and len(progress(w)) == 1, "running again is not worth a comment"
    ask_person(w, tid, 20 * 60)                                                  # a new request, open long enough, 2 minutes after the last comment
    w.gh.tick(120)
    beat(w)
    assert len(progress(w)) == 1 and rec_of(w)["phase"] == "running", "the cadence holds the second comment back"
    w.gh.tick(179)
    beat(w)
    assert len(progress(w)) == 1, "299 seconds after the first comment is not five minutes"
    w.gh.tick(2)
    beat(w)
    assert len(progress(w)) == 2 and rec_of(w)["phase"] == "waiting"
    gaps = [c["created_at"] for c in thread(w) if c["body"].startswith(github_write.PROGRESS_MARKER)]
    assert github.ts_epoch(gaps[1]) - github.ts_epoch(gaps[0]) >= 300


def test_the_end_of_the_task_waits_for_the_cadence_and_the_claim_is_held_until_it_is_said(w):
    tid = run_route(w).json()["data"]["id"]
    ask_person(w, tid, 11 * 60)
    beat(w)
    w.gh.tick(100)
    stop_turn(w, tid, "Done it.")
    assert len(progress(w)) == 1 and claims(w)[0]["status"] == "running" and rec_of(w)["state"] == "ending", "done is not posted within five minutes of the last comment"
    w.gh.tick(150)
    beat(w)
    assert len(progress(w)) == 1 and claims(w)[0]["status"] == "running"
    w.gh.tick(60)
    beat(w)
    assert len(progress(w)) == 2 and progress(w)[1].split("\n")[1].endswith(": done") and claims(w)[0]["status"] == "done"
    assert w.gh.labels_of(f"{REPO}#201") == [] and rec_of(w)["state"] == "finished"


def test_a_pull_request_that_appears_is_commented_once_with_a_link_built_from_the_repository(w):
    tid = run_route(w).json()["data"]["id"]
    with w.b.enter():
        w.b.db.task_update(tid, pr_url="https://evil.example.net/steal", pr_number=12, pr_state="OPEN", status="pr")
    beat(w)
    [body] = progress(w)
    assert "ccboard" in body and ": pull request opened https://github.com/acme/shop/pull/12\nBranch: worktree-" in body and "evil.example.net" not in body, "the link is built from owner, repository and number"
    beat(w)
    assert len(progress(w)) == 1 and rec_of(w)["phase"] == "pr" and rec_of(w)["pr_number"] == 12


def test_asking_since_reads_the_pending_permission_and_a_session_waiting_on_a_dialog_not_the_idle_prompt(w):
    tid = run_route(w).json()["data"]["id"]
    with w.b.enter():
        t = w.b.db.task_get(tid)
        assert nodes_work.asking_since(w.b.db, t) is None
        with w.b.db.lock:
            w.b.db.conn.execute("UPDATE sessions SET state='waiting', state_at=? WHERE tmux_name=?", (taskflow._iso(1_700_000_000), t["tmux_name"]))
        assert nodes_work.asking_since(w.b.db, t) == 1_700_000_000.0, "waiting on a dialog"
        w.b.db.update_flags(t["tmux_name"], {"wait_kind": "idle"})
        assert nodes_work.asking_since(w.b.db, t) is None, "the idle prompt is not a question"
        assert nodes_work.asking_since(w.b.db, {"tmux_name": ""}) is None


def test_no_comment_holds_a_path_a_host_name_or_a_token(w):
    tid = run_route(w).json()["data"]["id"]
    ask_person(w, tid, 11 * 60)
    beat(w)
    w.gh.tick(301)
    stop_turn(w, tid, "I edited " + MAC + "x/secret/app.py with " + "ghp_" + "A1b2C3d4" * 4 + " on node-b at 100.64.0.2")
    beat(w)
    for body in progress(w):
        for bad in ("/Users", "/home", str(w.two.b_projects), "node-a", "node-b", "100.64", "ghp_", SECRET, "secret", "app.py", "alice@", "worktrees"):
            assert bad not in body, (bad, body)
        assert len(body) < 400


# ================================================================ the result report: phase (default), summary, none

def set_epic(w, **settings_):
    """The person's choices for the epic, on the HUB (board a), as the page saves them."""
    r = w.a.call("PUT", "/api/epics/acme/shop/147/settings", json=settings_)
    assert r.status_code == 200, r.text
    return r.json()


SECRET_RESULT = ("Changed " + MAC + "x/secret/app.py and /home/dev/tool.py, home is ~. The key is " + "ghp_" + "A1b2C3d4" * 4 + " and the other " + "sk-" + "abcdefgh12345678" +
                 " and " + "AKIA" + "ABCDEFGH12345678" + " and " + "xoxb-" + "123456789012-abcdef" + " and Bearer " + "abcdefghijklmnop" + " and a hash " + "0123456789abcdef" * 3 +
                 " and a run " + "Zm9vYmFyMTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1ub3A" + ". `code` stays safe.")


def test_the_summary_report_posts_the_result_once_redacted_and_says_text_was_removed(w):
    set_epic(w, report="summary")
    tid = run_route(w).json()["data"]["id"]
    assert rec_of(w)["report"] == "summary", "the hub wrote the epic's setting into the body"
    stop_turn(w, tid, SECRET_RESULT)
    [body] = progress(w)
    assert github_write.valid_body(body)
    text = body.split("```text\n")[1].split("\n```")[0]
    assert "~/secret/app.py" in text and "~/tool.py" in text and "/Users" not in text and "/home" not in text
    assert text.count("[removed]") >= 6 and "ghp_" not in body and "sk-abc" not in body and "AKIA" not in body and "xoxb" not in body and "Bearer abcd" not in body and "0123456789abcdef" not in body
    assert "Zm9vYmFy" not in body and SECRET not in body
    assert "`code`" not in text and "'code' stays safe" in text, "a backtick cannot close the fence"
    assert body.endswith("```\nSome text was removed before posting.")
    assert body.index("done") < body.index("Result (first 1500 characters, redacted):")


def test_a_clean_result_is_posted_without_the_note_and_the_phase_report_posts_no_result_text(w):
    set_epic(w, report="summary")
    tid = run_route(w).json()["data"]["id"]
    stop_turn(w, tid, "Added the form and two tests.")
    [body] = progress(w)
    assert body.endswith("Added the form and two tests.\n```") and "removed" not in body
    w.gh.tick(10)
    set_epic(w, report="phase")
    tid = run_route(w, 202).json()["data"]["id"]
    stop_turn(w, tid, "A SENTENCE THAT MUST STAY ON THE NODE")
    [body] = progress(w, 202)
    assert "MUST STAY" not in body and "Result" not in body and "```" not in body and rec_of(w, 202)["report"] == "phase"


def test_a_summary_is_cut_at_1500_characters_after_the_redaction_and_the_input_is_capped_first(w):
    set_epic(w, report="summary")
    tid = run_route(w).json()["data"]["id"]
    stop_turn(w, tid, "word " * 5000)
    [body] = progress(w)
    text = body.split("```text\n")[1].split("\n```")[0]
    assert 1490 <= len(text) <= 1500 and github_write.valid_body(body)
    assert 1499 <= len(github_write.redact_result("x " * 100000)[0]) <= 1500 and github_write.redact_result("x" * 100000) == ("", False), "one unbroken run longer than the cap is dropped, not cut"
    t, removed = github_write.redact_result("a " * 1990 + "ghp_" + "Q1w2E3r4" * 3 + "z" * 3000)       # a token at the 4000 character cap: cut at the last white space, never left as a stub
    assert "ghp_" not in t and "Q1w2" not in t


def test_report_none_posts_no_phase_comment_but_the_claim_is_still_released(w):
    set_epic(w, report="none")
    tid = run_route(w).json()["data"]["id"]
    stop_turn(w, tid, "All done.")
    assert progress(w) == [] and claims(w)[0]["status"] == "done" and w.gh.labels_of(f"{REPO}#201") == []
    ask_person(w, run_route(w, 202).json()["data"]["id"], 20 * 60)
    beat(w)
    assert progress(w, 202) == [], "none silences the waiting comment too"


def test_the_hub_writes_the_epics_settings_into_the_body_and_a_caller_cannot_choose_them(w):
    set_epic(w, report="summary", auto_pr=True, close=True)
    seen = []
    inner = w.count.inner
    w.count.inner = lambda target, method, path, headers, body, timeout: seen.append(json.loads(body)) or inner(target, method, path, headers, body, timeout)
    run_route(w, body={"repo": REPO, "issue": 201, "epic": 147, "report": "none", "auto_pr": False, "close": False, "force": True})
    assert seen[-1] == {"repo": REPO, "issue": 201, "epic": "147", "force": True, "report": "summary", "auto_pr": True, "close": True}
    rec = rec_of(w)
    assert (rec["report"], rec["auto_pr"], rec["close"]) == ("summary", True, True)
    other = run_route(w, 202, body={"repo": REPO, "issue": 202, "report": "summary"})                  # no epic named: the defaults, never what the page said
    assert seen[-1]["report"] == "phase" and seen[-1]["auto_pr"] is False and seen[-1]["close"] is False and other.status_code == 200


def test_a_peer_that_sends_no_settings_gets_the_defaults_that_post_and_push_the_least(w):
    with w.b.enter():
        nodes_epics.read_epic_now("acme", "shop", 147, [REPO])
    body = nr.validate(ROW, {}, {"repo": REPO, "issue": 201, "epic": 147})
    assert body == {"repo": REPO, "issue": 201, "epic": "147", "force": False, "report": "phase", "auto_pr": False, "close": False}
    r = work(w)
    assert rec_of(w)["report"] == "phase" and rec_of(w)["auto_pr"] is False and rec_of(w)["close"] is False and r["claimed"] is True


# ================================================================ the fence: a lost claim posts nothing and parks the task

def foreign_first(w, n: int = 201) -> int:
    """Another node's claim that GitHub orders before ours (created earlier, live, with an id that follows the thread's): from now on this node's claim is not first."""
    from tests.fake_github import claim_body, iso
    world = w.gh.world
    world["next_cid"] = cid = world.get("next_cid", 100000) + 1
    return w.gh.comment(f"{REPO}#{n}", claim_body("node-z", "running", nid="deadbeef", seq=3), created_at=iso(world["now"] - 3600), updated_at=iso(world["now"] - 30), cid=cid)


def test_a_lost_claim_posts_nothing_and_parks_the_task_and_keeps_its_worktree(w):
    tid = run_route(w).json()["data"]["id"]
    k = f"{REPO}#201"
    wt = Path(b_task(w, tid)["worktree"])
    wt.mkdir(parents=True, exist_ok=True)
    foreign_first(w)                                                              # another node is first
    before = len(w.gh.writes())
    stop_turn(w, tid, "Done.")
    assert len(w.gh.writes()) == before and progress(w) == [], "no comment, no edit, no label change: the fence refused every write"
    rec = rec_of(w)
    assert rec["state"] == "lost" and rec["phase"] == "lost"
    t = b_task(w, tid)
    assert t["phase"] == "done", "the task's own result stands"
    assert wt.is_dir(), "the worktree is kept"
    with w.b.enter():
        assert w.b.db.open_row(t["tmux_name"])["flags"].get("epic_lost") == "lost-claim"
        assert [e for e in w.b.db.events_for(t["tmux_name"]) if e["event"] == "EpicClaimLost"] if hasattr(w.b.db, "events_for") else True
        card = [x for x in main._tasks_view() if x["id"] == tid][0]
    assert card["epic"]["phase"] == "lost" and card["epic"]["state"] == "lost"
    assert audit(w.b, action="epic-claim-lost")
    assert len(progress(w)) == 0 and w.gh.claims_on(k)[-1]["body"] != ""


def test_a_claim_lost_between_beats_parks_the_task_through_the_lost_hook(w):
    tid = run_route(w).json()["data"]["id"]
    k = f"{REPO}#201"
    foreign_first(w)
    with w.b.enter():
        actor = nodes_epics.board_actor()
        [v] = nodes_epics.held_claims(actor)
        assert nodes_epics.renew(v["claim_ref"], actor=actor)["lost"] is True
    assert rec_of(w)["state"] == "lost" and b_task(w, tid)["phase"] == "running", "parked, not ended"
    stop_turn(w, tid, "Done.")
    assert progress(w) == [], "nothing is posted for a lost claim, even when the task finishes later"


def test_a_fence_that_cannot_be_checked_writes_nothing_marks_no_loss_and_the_next_beat_finishes(w):
    tid = run_route(w).json()["data"]["id"]
    w.gh.fail("/comments", 500, times=-1, method="GET")
    before = len(w.gh.writes())
    stop_turn(w, tid, "Done.")
    assert len(w.gh.writes()) == before and progress(w) == []
    rec = rec_of(w)
    assert rec["state"] == "ending" and rec["end_tries"] == 1, "waiting for GitHub, not lost"
    with w.b.enter():
        [v] = nodes_epics.held_claims(nodes_epics.Actor(w.b.db, "", None, ""))
        assert v["state"] == "held" and v["lost_reason"] is None
    w.gh.world["overrides"].clear()
    beat(w)
    assert len(progress(w)) == 1 and claims(w)[0]["status"] == "done" and rec_of(w)["state"] == "finished"


def test_a_finished_task_that_cannot_reach_github_for_ever_is_abandoned_and_the_claim_left_to_lapse(w, monkeypatch):
    monkeypatch.setattr(nodes_work, "END_TRIES", 3)
    tid = run_route(w).json()["data"]["id"]
    w.gh.fail("/comments", 500, times=-1, method="GET")
    stop_turn(w, tid, "Done.")
    beat(w)
    beat(w)
    rec = rec_of(w)
    assert rec["state"] == "abandoned" and progress(w) == []
    with w.b.enter():
        assert nodes_epics.held_claims(nodes_epics.Actor(w.b.db, "", None, "")) == [], "nobody renews it any more"
    assert any(a["detail"] == "abandoned" for a in audit(w.b, action="epic-work"))


# ================================================================ failure and retry

def test_a_failed_task_posts_failed_sets_the_claim_failed_frees_the_issue_and_the_third_failure_asks_for_a_person(w):
    k = f"{REPO}#201"
    for attempt in (1, 2):
        tid = run_route(w).json()["data"]["id"]
        stop_turn(w, tid, "It broke.", failed=True)
        assert b_task(w, tid)["phase"] == "failed"
        c = claims(w)[-1]
        assert c["status"] == "failed" and c["attempt"] == attempt
        assert progress(w)[-1].split("\n")[1].endswith(": failed")
        assert w.gh.labels_of(k) == [] and w.gh.assignees_of(k) == [] and w.gh.world["issues"][k]["state"] == "open", "the issue is free and open for another attempt"
        assert rec_of(w)["state"] == "finished" and rec_of(w)["phase"] == "failed"
    assert len(progress(w)) == 2
    tid = run_route(w).json()["data"]["id"]
    assert claims(w)[-1]["attempt"] == 3
    stop_turn(w, tid, "It broke again.", failed=True)
    helps = [c for c in thread(w) if c["body"] == github_write.HELP_BODY]
    assert len(helps) == 1, "the third failure asks for a person"
    assert len(progress(w)) == 2, "and says it once: the help comment stands in for a third `failed`"
    assert claims(w)[-1]["status"] == "failed"
    r = run_route(w)
    assert r.status_code == 409 and r.json()["why"] == "attempts_exhausted", r.text
    assert len([c for c in thread(w) if c["body"] == github_write.HELP_BODY]) == 1, "never twice"


def test_a_cancelled_task_posts_cancelled_and_sets_the_claim_cancelled(w):
    tid = run_route(w).json()["data"]["id"]
    with w.b.enter():
        t = w.b.db.task_get(tid)
        w.b.db.end(t["tmux_name"], "killed")
        assert tid in main._taskflow().sweep()
    assert b_task(w, tid)["phase"] == "cancelled"
    assert progress(w)[0].split("\n")[1].endswith(": cancelled") and claims(w)[0]["status"] == "cancelled" and w.gh.labels_of(f"{REPO}#201") == []


def test_a_refused_launch_releases_the_claim_and_leaves_no_task(w, monkeypatch):
    def boom(*a, **k):
        raise main.projects.Conflict("the worktree is somewhere at " + MAC + "x/private")
    monkeypatch.setattr(main, "_tasks_create", boom)
    r = run_route(w)
    assert r.status_code == 409 and r.json()["why"] == "launch_failed" and "/Users" not in r.text, r.text
    assert claims(w)[0]["status"] == "released" and w.gh.labels_of(f"{REPO}#201") == [] and progress(w) == [] and nodes_work.has_records(w.b.db) is False


# ================================================================ auto_pr and close: only the task's own branch, only when asked

class FakeGit:
    """gitops.run over a script: every git and gh command the pull request flow makes is recorded and answered, nothing runs."""

    def __init__(self, branch="", dirty=True, ahead="1"):
        self.calls, self.bodies, self.dirty, self.ahead, self.branch = [], [], dirty, ahead, branch
        self.head = "0123abc"

    def __call__(self, args, cwd, timeout=30, check=True):
        full = list(args)
        self.calls.append(full)
        a = full[:1] + full[3:] if full[1:2] == ["-c"] else full                 # the verb of the command, whatever config switch comes first
        out, rc = "", 0
        if a[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            out = self.branch + "\n"
        elif a[:3] == ["git", "status", "--porcelain"]:
            out = " M f.py\n" if self.dirty else ""
        elif a[:2] == ["git", "rev-parse"] and "--verify" in a:
            rc = 1
        elif a[:2] == ["git", "rev-list"]:
            out = self.ahead + "\n"
        elif a[:3] == ["git", "rev-parse", "HEAD"]:
            out = self.head + "\n"
        elif a[:3] == ["git", "ls-remote", "--heads"]:
            out = f"{self.head}\trefs/heads/{a[-1]}\n"
        elif a[:3] == ["gh", "pr", "view"]:
            rc = 1
        elif a[:3] == ["gh", "pr", "create"]:
            self.bodies.append(Path(a[a.index("--body-file") + 1]).read_text())
            out = "https://github.com/acme/shop/pull/12\n"
        return subprocess.CompletedProcess(a, rc, out, "")

    def pushes(self):
        return [c for c in self.calls if "push" in c[:4] and c[0] == "git"]

    def commits(self):
        return [c for c in self.calls if "commit" in c[:4] and c[0] == "git"]


@pytest.fixture
def git(w, monkeypatch):
    fake = FakeGit()
    monkeypatch.setattr(gitops, "run", fake)
    return fake


def finished_task(w, **settings_):
    if settings_:
        set_epic(w, **settings_)
    d = run_route(w).json()["data"]
    Path(b_task(w, d["id"])["worktree"]).mkdir(parents=True, exist_ok=True)
    return d


def test_with_auto_pr_off_nothing_is_pushed_committed_or_opened(w, git):
    d = finished_task(w)
    stop_turn(w, d["id"], "Done.")
    assert git.calls == [] and "pull request" not in " ".join(progress(w)) and b_task(w, d["id"])["pr_url"] is None


def test_auto_pr_pushes_the_task_branch_never_main_and_opens_a_pull_request_that_says_nothing_about_closing(w, git):
    d = finished_task(w, auto_pr=True)
    git.branch = d["branch"]
    stop_turn(w, d["id"], "Done.")
    [push] = git.pushes()
    assert push == ["git", "-c", "core.hooksPath=/dev/null", "push", "--no-verify", "-u", "origin", d["branch"]] and d["branch"].startswith("worktree-")
    assert not any(x in ("main", "master", "--force", "-f", "--force-with-lease", "--mirror", "--all", "--delete") or ":" in x for x in push)
    create = [c for c in git.calls if c[:3] == ["gh", "pr", "create"]]
    assert len(create) == 1 and create[0][create[0].index("--head") + 1] == d["branch"] and create[0][create[0].index("--base") + 1] == "main"
    assert ["git", "add", "-A"] in git.calls and len(git.commits()) == 1, "what was left in the worktree is committed on the task's branch"
    [body] = git.bodies
    assert "Closes" not in body and "#201" in body
    t = b_task(w, d["id"])
    assert t["pr_url"] == "https://github.com/acme/shop/pull/12" and t["pr_number"] == 12 and t["pr_state"] == "OPEN"
    [c] = progress(w)
    assert ": done https://github.com/acme/shop/pull/12\nBranch: " + d["branch"] in c, "the PR link goes in the one done comment; the card has it too"
    assert claims(w)[0]["status"] == "done"


def test_close_adds_closes_to_the_pull_request_only_when_it_is_on(w, git):
    d = finished_task(w, auto_pr=True, close=True)
    git.branch = d["branch"]
    stop_turn(w, d["id"], "Done.")
    assert git.bodies[0].count("Closes #201") == 1
    assert "Closes" not in " ".join(progress(w)) and "Closes" not in json.dumps(w.gh.writes()), "no comment on the issue says it"


def test_close_without_auto_pr_writes_nothing_anywhere(w, git):
    d = finished_task(w, close=True)
    stop_turn(w, d["id"], "Done.")
    assert git.calls == [] and "Closes" not in json.dumps(w.gh.writes())


def test_auto_pr_does_nothing_for_a_failed_task_a_branch_that_is_not_the_tasks_own_or_a_task_with_nothing_to_show(w, git):
    d = finished_task(w, auto_pr=True)
    stop_turn(w, d["id"], "It broke.", failed=True)
    assert git.calls == [], "a failed task opens no pull request"
    k = run_route(w, 202).json()["data"]
    Path(b_task(w, k["id"])["worktree"]).mkdir(parents=True, exist_ok=True)
    with w.b.enter():
        w.b.db.task_update(k["id"], branch="main")
    stop_turn(w, k["id"], "Done.")
    assert git.calls == [], "the branch is checked against the claim's before any command is built"
    git.dirty, git.ahead = False, "0"
    j = run_route(w, 203).json()["data"]
    git.branch = j["branch"]
    Path(b_task(w, j["id"])["worktree"]).mkdir(parents=True, exist_ok=True)
    stop_turn(w, j["id"], "Nothing needed.")
    assert git.pushes() == [] and not [c for c in git.calls if c[:3] == ["gh", "pr", "create"]], "no commit ahead of the base: no push, no pull request"
    assert claims(w, 203)[0]["status"] == "done"


def test_auto_pr_is_fenced_a_lost_claim_pushes_nothing(w, git):
    d = finished_task(w, auto_pr=True)
    foreign_first(w)
    stop_turn(w, d["id"], "Done.")
    assert git.pushes() == [] and git.calls == [] and progress(w) == []


# ================================================================ the advisory lane cap

def test_a_node_at_its_lane_cap_refuses_the_next_run_unless_a_person_forces_it(w, monkeypatch):
    monkeypatch.setattr(settings, "node_lanes", 1)
    assert run_route(w, 201).status_code == 200
    w.gh.clear_calls()
    r = run_route(w, 202)
    assert r.status_code == 409 and r.json()["why"] == "lanes_full" and "lane cap" in r.json()["error"] and "force" in r.json()["error"], r.text
    assert w.gh.writes() == [] and w.gh.thread(f"{REPO}#202") == [], "refused before the claim"
    r = run_route(w, body={"repo": REPO, "issue": 202, "epic": 147, "force": True})
    assert r.status_code == 200, r.text
    assert len(w.b.db.tasks()) == 2


def test_cap_zero_is_no_cap_and_two_runs_at_once_cannot_both_take_the_last_lane(w, monkeypatch):
    monkeypatch.setattr(settings, "node_lanes", 0)
    assert run_route(w, 201).status_code == 200 and run_route(w, 202).status_code == 200 and run_route(w, 203).status_code == 200
    monkeypatch.setattr(settings, "node_lanes", 2)
    with w.b.enter():
        assert nodes_work._reserve(w.b.db, False) is False, "two tasks run already"
    monkeypatch.setattr(settings, "node_lanes", 4)
    with w.b.enter():
        assert nodes_work._reserve(w.b.db, False) is True and nodes_work._reserve(w.b.db, False) is False, "the second request counts the first one's reservation"
        nodes_work._unreserve()
        nodes_work._unreserve()
        assert nodes_work._starting == 0


# ================================================================ the row: a tasks write, a person's act, never an agent's

class Answer:
    """A peer transport with one fixed reply (or one that raises), and a counter."""

    def __init__(self, status=200, body=b"{}", headers=None, raises=None):
        self.status, self.body, self.headers, self.raises, self.seen = status, body, headers or {}, raises, 0

    def __call__(self, target, method, path, headers, body, timeout):
        self.seen += 1
        self.timeout = timeout
        if self.raises:
            raise self.raises
        return self.status, self.headers, self.body


def peer_says(w, status, body, headers=None):
    w.count = Answer(status, json.dumps(body).encode() if not isinstance(body, bytes) else body, headers or {})
    nodes.peer_transport = w.count
    return run_route(w)


def test_the_row_is_a_tasks_write_a_person_only_a_guarded_launch_and_never_an_agents():
    assert (ROW.peer_method, ROW.peer_path, ROW.hub_path, ROW.scope, ROW.rate_class, ROW.audit_action) == ("POST", "/api/node/epics/work", WORK_ROUTE, "tasks", "write", "work_issue")
    assert ROW.human_only and ROW.acting and ROW.guard and not ROW.mcp and ROW.agent_model is None and ROW.agent_shape is None
    assert ROW.timeout == nr.WORK_TIMEOUT and ROW.timeout > nr.CLAIM_TIMEOUT > nr.RELAY_TIMEOUT
    assert ROW.body_model.model_config.get("extra") == "forbid" and set(ROW.body_model.model_fields) == {"repo", "issue", "epic", "force", "report", "auto_pr", "close"}
    assert ROW.target({}) == "work" and ROW.body_target({}, {"repo": REPO, "issue": 205}) == "work acme/shop#205" and ROW.fill is nr._fill_work
    assert nr.HANDLERS["epic_work"] is nr.peer_epic_work and nr.BY_NAME["epic_work"] is ROW


def test_no_mcp_tool_and_no_agent_body_reaches_the_row():
    assert "epic_work" not in set(mcp.NODE_ROW.values()) and not any("work" in t["name"] for t in mcp.TOOLS)
    assert {t["name"] for t in mcp.TOOLS if "epic" in t["name"]} == {"epic_status", "epic_run"}, "the two epic tools start the loop (guarded by the epic's own switch); none relays the work row itself"
    with pytest.raises(ValueError, match="a person's act"):
        dataclasses.replace(ROW, mcp=True, agent_model=nr.NoFields, agent_shape=nr._agent_task)
    with pytest.raises(nr.Invalid):
        nr.validate(ROW, {}, {"repo": REPO, "issue": 205}, agent=True)


def test_the_hook_token_and_a_device_token_get_a_403_and_nothing_is_sent(w):
    r = run_route(w, owner=False, headers={"X-CCBoard-Token": w.a.hook_token})
    assert r.status_code == 403 and r.json()["reason"] == "human_only" and w.count.seen == 0
    standin = SimpleNamespace(state=SimpleNamespace(user="mcp:laptop", mcp_token={"id": "t1", "name": "laptop", "scopes": ["read", "tasks", "sessions", "permissions"]}), headers={})
    with w.a.enter():
        with pytest.raises(nr.RelayError) as e:
            nr.relay(w.h, ROW, {}, {"repo": REPO, "issue": 205, "epic": 147}, standin, db=w.a.db, hub=main._hub())
    assert e.value.reason in ("human_only", "mcp_off", "mcp_level", "mcp_scope") and w.count.seen == 0 and w.gh.calls() == []


def test_the_node_side_route_wants_a_named_person_the_tasks_scope_and_a_pair(w):
    with w.b.enter():
        _, tok_r = nodes.add_incoming({"id": "ts:nCALLER000001", "name": "caller", "url": "https://100.64.0.9"}, ["read"], db=w.b.db)
        _, tok_t = nodes.add_incoming({"id": "ts:nCALLER000002", "name": "caller2", "url": "https://100.64.0.8"}, ["tasks"], db=w.b.db)
    bearer = lambda t, **x: {"Authorization": f"Bearer {t}", "X-CCBoard": "1", **x}
    body = {"repo": REPO, "issue": 201, "epic": 147}
    assert w.b.call("POST", ROW.peer_path, owner=False, headers=bearer(tok_r, **{"X-CCBoard-Acting-User": "alice"}), json=body).status_code == 403, "a read token cannot run an issue"
    r = w.b.call("POST", ROW.peer_path, owner=False, headers=bearer(tok_t), json=body)
    assert r.status_code == 403 and "acting" in r.json()["error"], "no named person"
    assert w.b.call("POST", ROW.peer_path, headers={}, json=body).status_code == 403, "a signed-in person uses the board's own routes, not the node route"
    assert w.gh.calls() == [] and w.b.db.tasks() == []
    r = w.b.call("POST", ROW.peer_path, owner=False, headers=bearer(tok_t, **{"X-CCBoard-Acting-User": "alice"}), json=body)
    assert r.status_code == 200 and r.json()["claimed"] is True, "a tasks token with a named person runs it"


def test_a_pair_without_the_tasks_scope_is_409_on_the_hub_before_any_call(two_nodes, pair_up):
    reg = pair_up(("read",))
    nodes.peer_transport = Count(two_nodes.transport)
    r = two_nodes.a.post(WORK_ROUTE.format(handle=reg["handle"]), json={"repo": REPO, "issue": 205, "epic": 147})
    assert r.status_code == 409 and r.json()["reason"] == "scope" and "tasks" in r.json()["error"] and nodes.peer_transport.seen == 0
    assert two_nodes.a.post(WORK_ROUTE.format(handle="local"), json={"repo": REPO, "issue": 5}).status_code == 400
    assert two_nodes.a.post(WORK_ROUTE.format(handle="nobody"), json={"repo": REPO, "issue": 5}).status_code == 404


@pytest.mark.parametrize("body", [
    {}, {"repo": REPO}, {"issue": 5}, {"repo": "acme", "issue": 5}, {"repo": "../etc/passwd", "issue": 5}, {"repo": "acme/shop\n", "issue": 5}, {"repo": REPO, "issue": 0},
    {"repo": REPO, "issue": "205"}, {"repo": REPO, "issue": True}, {"repo": REPO, "issue": 5, "epic": "0"}, {"repo": REPO, "issue": 5, "epic": "acme/shop"},
    {"repo": REPO, "issue": 5, "force": "yes"}, {"repo": REPO, "issue": 5, "force": 1}, {"repo": REPO, "issue": 5, "auto_pr": "true"}, {"repo": REPO, "issue": 5, "close": 1},
    {"repo": REPO, "issue": 5, "report": "everything"}, {"repo": REPO, "issue": 5, "report": True}, {"repo": REPO, "issue": 5, "report": ""},
    {"repo": REPO, "issue": 5, "prompt": "do this"}, {"repo": REPO, "issue": 5, "title": "t"}, {"repo": REPO, "issue": 5, "body": "free text"}, {"repo": REPO, "issue": 5, "args": "--x"},
    {"repo": REPO, "issue": 5, "model": "opus"}, {"repo": REPO, "issue": 5, "agent": "codex"}, {"repo": REPO, "issue": 5, "alias": "x"}, {"repo": REPO, "issue": 5, "login": "alice"},
    {"repo": REPO, "issue": 5, "permission_mode": "acceptEdits"}, {"repo": REPO, "issue": 5, "permission_mode": "bypassPermissions"}, {"repo": REPO, "issue": 5, "mode": "bypass"},
    {"repo": REPO, "issue": 5, "bypass": True}, {"repo": REPO, "issue": 5, "yolo": True}, {"repo": REPO, "issue": 5, "dangerously_skip_permissions": True},
    {"repo": REPO, "issue": 5, "sandbox": "danger-full-access"}, {"repo": REPO, "issue": 5, "claim_ref": "acme/shop#5:7"}, {"repo": REPO, "issue": 5, "branch": "main"},
    {"repo": REPO, "issue": 5, "extra": {"--dangerously-skip-permissions": 1}}, {"repo": "acme/shop", "issue": 5, "epic": "--yolo"},
], ids=lambda b: json.dumps(b)[:60])
def test_a_hostile_body_is_a_422_on_the_hub_with_nothing_sent_and_again_on_the_node(w, body):
    r = run_route(w, body=body)
    assert r.status_code == 422 and r.json()["reason"] == "invalid", r.text
    assert w.count.seen == 0 and w.gh.calls() == []
    with w.b.enter():
        _, tok = nodes.add_incoming({"id": "ts:nCALLER000009", "name": "caller9", "url": "https://100.64.0.7"}, ["tasks"], db=w.b.db)
    r = w.b.call("POST", ROW.peer_path, owner=False, json=body, headers={"Authorization": f"Bearer {tok}", "X-CCBoard": "1", "X-CCBoard-Acting-User": "alice"})
    assert r.status_code == 422, "the node checks the same rules again"
    assert w.gh.calls() == [] and w.b.db.tasks() == []


def test_a_good_body_is_read_as_the_three_values_and_the_choices_with_their_defaults():
    assert nr.validate(ROW, {}, {"repo": REPO, "issue": 205}) == {"repo": REPO, "issue": 205, "force": False, "report": "phase", "auto_pr": False, "close": False}
    assert nr.validate(ROW, {}, {"repo": REPO, "issue": 205, "epic": f"{REPO}#147", "force": True, "report": "none", "auto_pr": True, "close": True}) == {
        "repo": REPO, "issue": 205, "epic": f"{REPO}#147", "force": True, "report": "none", "auto_pr": True, "close": True}


# ================================================================ the hub rebuilds what the node says

def test_a_good_answer_keeps_only_the_whitelist_and_the_ref_is_built_by_the_hub(w):
    r = peer_says(w, 200, {"claimed": True, "reason": "won", "id": 7, "branch": "worktree-x", "tmux": "shop--api--t-x", "repo": REPO, "issue": 201, "task_ref": "evil:9", "worktree": MAC + "x/y",
                           "prompt": "SECRET", "login": "alice", "claim_ref": "acme/shop#201:9"})
    assert r.status_code == 200
    assert r.json()["data"] == {"claimed": True, "reason": "won", "task_ref": "node-b:7", "id": 7, "branch": "worktree-x", "tmux": "shop--api--t-x", "repo": REPO, "issue": 201}


@pytest.mark.parametrize("body", [
    {"claimed": False, "reason": "won", "id": 7, "repo": REPO, "issue": 5}, {"claimed": True, "reason": "taken", "id": 7, "repo": REPO, "issue": 5}, {"claimed": True, "reason": "won", "repo": REPO, "issue": 5},
    {"claimed": True, "reason": "won", "id": 0, "repo": REPO, "issue": 5}, {"claimed": True, "reason": "won", "id": 7, "repo": "../x", "issue": 5}, {"claimed": True, "reason": "won", "id": 7, "repo": REPO},
    [], {"claimed": 1, "reason": "won", "id": 7, "repo": REPO, "issue": 5},
])
def test_an_answer_that_is_not_a_started_task_is_refused_by_the_hub(w, body):
    r = peer_says(w, 200, body)
    assert r.status_code == 502 and r.json()["reason"] in ("bad_answer", "wrong_node"), r.text


def test_every_reason_the_node_may_give_is_mapped_to_this_boards_own_sentence(w):
    seen = set()
    for why in nr.WORK_PEER_WHY:
        r = peer_says(w, 409, {"error": "PEER SENTENCE", "reason": "work_refused", "why": why, "winner": "willow-3f", "attempt": 2, "author": "mallory"})
        assert r.status_code == 409 and r.json()["reason"] == "work_refused" and r.json()["why"] == why, (why, r.text)
        assert "PEER SENTENCE" not in r.json()["error"] and r.json()["error"].startswith("node-b: ")
        if why in ("author_not_allowed", "epic_author_not_allowed"):
            assert "authored by mallory, not on the allowed list" in r.json()["error"]
        seen.add(why)
    assert seen | {"repo_not_here"} == set(nodes_work.REASONS), "the closed list is the node's reasons; a missing clone is its own 404"


def test_a_reason_the_node_invents_is_an_error_and_its_extra_fields_are_dropped(w):
    r = peer_says(w, 409, {"reason": "work_refused", "why": "anything\nelse", "winner": "x" * 80, "author": "a b", "claim_ref": "acme/shop#5:77", "login": "alice", "wait_s": -1})
    b = r.json()
    assert b["why"] == "error" and "winner" not in b and "author" not in b and "claim_ref" not in b and "login" not in b and "wait_s" not in b


def test_repo_not_here_is_a_404_in_the_hubs_words_and_a_timeout_is_unconfirmed_and_never_retried(w):
    r = peer_says(w, 404, {"error": "PEER", "reason": "repo_not_here"})
    assert r.status_code == 404 and r.json()["reason"] == "repo_not_here" and "not cloned on this node" in r.json()["error"] and "PEER" not in r.json()["error"]
    w.count = Answer(raises=TimeoutError("slow"))
    nodes.peer_transport = w.count
    r = run_route(w)
    assert r.status_code == 504 and r.json()["reason"] == "unconfirmed" and "75 s" in r.json()["error"] and w.count.seen == 1 and w.count.timeout == nr.WORK_TIMEOUT


def test_only_a_few_runs_at_once_on_a_node_and_the_next_is_told_busy_before_anything_starts(w):
    held = [nodes_epics.claim_slots.acquire(blocking=False) for _ in range(nodes_epics.CLAIMS_IN_FLIGHT)]
    try:
        assert all(held)
        w.gh.clear_calls()
        r = run_route(w)
        assert r.status_code == 409 and r.json()["why"] == "busy", r.text
        assert w.gh.calls() == [] and w.b.db.tasks() == []
    finally:
        for _ in held:
            nodes_epics.claim_slots.release()
    assert run_route(w).status_code == 200
    assert nodes_epics.claim_slots._value == nodes_epics.CLAIMS_IN_FLIGHT, "a run gives its place back"


def test_the_reasons_are_one_list_in_the_node_and_in_the_relay():
    assert set(nr.WORK_PEER_WHY) | {"repo_not_here"} == set(nodes_work.REASONS) and "repo_not_here" not in nr.WORK_PEER_WHY
    assert set(nodes_work.WORK_TEXT) <= set(nodes_work.REASONS) and all(isinstance(v, str) and v for v in nodes_work.WORK_TEXT.values())
    for text in list(nodes_work.WORK_TEXT.values()) + list(nodes_epics.CLAIM_TEXT.values()):
        assert "—" not in text and "/Users" not in text, text


def test_the_peer_handler_calls_the_work_library_and_nothing_else_makes_a_claim():
    import inspect
    src = inspect.getsource(nr.peer_epic_work)
    assert "nodes_work.work_issue(" in src and "claim_issue(" not in src and "_tasks_create" not in src
    assert "claim_slots.acquire(blocking=False)" in src


# ================================================================ the writer: a progress comment is a template, and a result is redacted before it gets there

GW = github_write
TOKENS = {
    "classic": "ghp_" + "A1b2C3d4" * 3, "oauth": "gho_" + "A1b2C3d4" * 3, "server": "ghs_" + "A1b2C3d4" * 3, "fine": "github_pat_" + "11AAAAAA0" + "x" * 20, "sk": "sk-" + "ab12" * 8,
    "aws": "AKIA" + "ABCD1234" * 2, "slack": "xoxb-" + "1234567890-abcdefghij", "bearer": "Bearer " + "abcdefgh12345678", "hex": "0123456789abcdef" * 2, "sha": "a1b2c3d4e5" * 4,
    "b64": "Zm9vYmFyMTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1ub3A", "b64slash": "AbCd1234/" * 5,
}


@pytest.mark.parametrize("name", sorted(TOKENS))
def test_every_token_shape_becomes_removed_and_the_note_is_wanted(name):
    text, removed = GW.redact_result(f"before {TOKENS[name]} after")
    assert removed is True and TOKENS[name].replace("Bearer ", "") not in text
    assert GW.redact_result(text) == (text, True), "applied to its own output it changes nothing"


def test_home_prefixes_become_a_tilde_and_a_plain_text_is_left_alone():
    text, removed = GW.redact_result("see " + MAC + "alex/work/app.py and /home/dev/.config/x and " + str(Path.home()) + "/proj, then ~/ok")
    assert text == "see ~/work/app.py and ~/.config/x and ~/proj, then ~/ok" and removed is False
    plain = "Added the login form. Tests pass: 12 passed. See src/components/forms/LoginForm.tsx and the docs."
    assert GW.redact_result(plain) == (plain, False)
    assert GW.redact_result("ThisIsAVeryLongIdentifierWithoutAnyDigitsAtAllInIt and a-very-long-hyphenated-name-with-no-digits")[1] is False, "a long word is not a token: it needs a digit and a letter"
    assert GW.redact_result(None) == ("", False) and GW.redact_result(5) == ("", False)
    t, _ = GW.redact_result("a`b\x00c‮d​e\r\nf")
    assert t == "a'bcde\nf", "backticks cannot close the fence, controls and format characters are gone"


def test_a_progress_comment_is_built_only_from_validated_fields_and_the_writer_takes_nothing_else(monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", True)
    for phase in ("waiting", "done", "failed", "cancelled", "refused_bypass", "refused_posture"):
        body = GW.progress_body("willow-3f", phase, branch="worktree-x")
        assert GW.valid_body(body) and GW.argv(GW.comment_create("acme", "shop", 5, body))[-1] == "/repos/acme/shop/issues/5/comments"
    body = GW.progress_body("willow-3f", "pr", branch="worktree-x", pr=("acme", "shop", 12))
    assert body.splitlines()[1] == "ccboard willow-3f: pull request opened https://github.com/acme/shop/pull/12"
    good = GW.progress_body("willow-3f", "done")
    bad = [good + "\nMore words", good.replace("done", "finished"), good.replace("willow-3f", "will ow"), good.replace("willow-3f", "mybox.example.net"), good + "\nBranch: ../x",
           good + "\nBranch: a b", good + "\nBranch: " + "x" * 80, good.replace("done", "pull request opened https://evil.example.net/pull/1"),
           GW.progress_body("willow-3f", "pr", pr=("acme", "shop", 12)).replace("github.com/acme", "github.com.evil.net/acme"), good.replace("<!-- ccboard:progress v1 -->", "<!-- ccboard:claim v1 -->"),
           good + "\n\nResult (first 1500 characters, redacted):\n```text\nghp_" + "A1b2C3d4" * 3 + "\n```", good + "\n\nResult (first 1500 characters, redacted):\n```text\nhi\n```\n```text\nmore\n```",
           good + "\n\nResult (first 1500 characters, redacted):\n```text\n" + "word " * 400 + "\n```", good + "\n\nResult (first 1500 characters, redacted):\n```text\n" + MAC + "x/secret\n```",
           good + "\n\nResult (first 1500 characters, redacted):\n```text\n[removed]\n```", "free text from a caller", "<!-- ccboard:progress v1 -->\nccboard willow-3f: done\n" + "x" * 3000]
    for b in bad:
        assert not GW.valid_body(b), b[:120]
        with pytest.raises(GW.GhWriteError) as e:
            GW.comment_create("acme", "shop", 5, b)
        assert e.value.reason == "bad_request"
    for kw in (dict(alias="a b", phase="done"), dict(alias="willow-3f", phase="nonsense"), dict(alias="willow-3f", phase="pr"), dict(alias="willow-3f", phase="waiting", pr=("acme", "shop", 3)),
               dict(alias="willow-3f", phase="done", branch="a b"), dict(alias="willow-3f", phase="done", pr=("acme", "../x", 3)), dict(alias="willow-3f", phase="done", pr=("acme", "shop", 0)),
               dict(alias="willow-3f", phase="done", text=MAC + "x/secret")):
        with pytest.raises(ValueError):
            GW.progress_body(**kw)


def test_a_result_that_still_looks_like_a_token_is_dropped_and_the_phase_is_still_posted(w):
    set_epic(w, report="summary")
    tid = run_route(w).json()["data"]["id"]
    with w.mp.context() as mp:
        mp.setattr(GW, "redact_result", lambda raw, limit=GW.RESULT_MAX: ("ghp_" + "A1b2C3d4" * 3 + " leaked", False))      # a redaction that failed: the writer refuses the text
        stop_turn(w, tid, "whatever")
    [body] = progress(w)
    assert body.splitlines()[1].endswith(": done") and "ghp_" not in body and "leaked" not in body and "```" not in body


def test_the_writer_gained_no_new_request_shape_for_the_run():
    shapes = {(m, rx.pattern) for m, rx in GW._PATHS}
    assert len(shapes) == 11, "the eight shapes of the claim (the comments of the run use the comment path, the release uses the label path), the one of team presence (issue #198) and the two of a new epic (issue #158)"
    assert {m for m, p in shapes if p.endswith("/issues")} == {"POST"}, "the ninth is the presence status issue, which a claim request can never use (tests/test_team_presence.py); an epic's issues are purpose epic_create (tests/test_epics_create.py)"


# ================================================================ settings: report is phase, summary or none; close is new

def test_a_stored_boolean_report_is_migrated_without_losing_the_value(w):
    db = w.a.db
    for stored, want in ((True, "summary"), (False, "phase"), ("summary", "summary"), ("none", "none"), ("phase", "phase"), ("loud", "phase"), (1, "phase"), ("True", "phase"), ([], "phase")):
        db.kv_set("epic:acme/shop#147", {"paused": True, "report": stored})
        assert nodes_epics.load_settings(db, "acme", "shop", 147)["report"] == want, stored
    db.kv_set("epic:acme/shop#147", {"paused": True})
    st = nodes_epics.load_settings(db, "acme", "shop", 147)
    assert st["report"] == "phase" and st["auto_pr"] is False and st["close"] is False
    db.kv_set("epic:acme/shop#147", {"auto_pr": "yes", "close": 1})
    st = nodes_epics.load_settings(db, "acme", "shop", 147)
    assert st["auto_pr"] is False and st["close"] is False, "only a real true counts"
    assert nodes_epics.DEFAULTS == {"pins": {}, "epic_pin": None, "paused": False, "report": "phase", "auto_pr": False, "close": False}


def test_the_settings_route_takes_the_three_words_and_the_old_booleans_and_close(w):
    assert set_epic(w, report="summary")["report"] == "summary"
    assert set_epic(w, report=True)["report"] == "summary" and set_epic(w, report=False)["report"] == "phase"
    r = set_epic(w, report="none", auto_pr=True, close=True)
    assert (r["report"], r["auto_pr"], r["close"]) == ("none", True, True)
    assert w.a.db.kv_get("epic:acme/shop#147")["value"] == {"pins": {}, "epic_pin": None, "paused": False, "report": "none", "auto_pr": True, "close": True}
    set_epic(w, report="phase", auto_pr=False, close=False)
    assert w.a.db.kv_get("epic:acme/shop#147") is None, "back to the defaults: no row is kept"
    for bad in ({"report": "bogus"}, {"report": 1}, {"report": None, "close": "yes"}, {"close": 1}, {"auto_pr": "true"}, {"report": ["phase"]}):
        assert w.a.call("PUT", "/api/epics/acme/shop/147/settings", json=bad).status_code in (400, 422), bad
    with pytest.raises(main.projects.BadRequest):
        nodes_epics.save_settings(w.a.db, "acme", "shop", 147, {"report": "loud"})


def test_the_settings_answer_says_in_words_what_run_will_post_and_push(w):
    plain = set_epic(w, paused=False)["plan"]
    assert "phase comments" in plain["report"] and "No result text leaves the node" in plain["report"] and plain["auto_pr"] == "Pushes nothing and opens no pull request." and plain["close"] == "Nothing says Closes #N."
    p = set_epic(w, report="summary", auto_pr=True, close=True)["plan"]
    assert "first 1500 characters" in p["report"] and "removed" in p["report"] and "own branch" in p["auto_pr"] and p["close"] == "The pull request says Closes #N."
    assert "none" not in set_epic(w, report="none")["plan"]["report"].split(".")[0] and "only applies with pull requests" in set_epic(w, auto_pr=False)["plan"]["close"]
    for v in p.values():
        assert "—" not in v


# ================================================================ the epic view: the phase, the settings, the work

def test_the_epic_view_carries_the_phase_of_the_work_per_child(w):
    run_route(w, 201)
    with w.b.enter():
        d = nodes_epics.detail(w.b.db, "acme", "shop", 147, [], repos=[REPO], login="alice")
    kids = {c["number"]: c for c in d["children"]}
    assert kids[201]["phase"] == "running" and kids[201]["work"]["state"] == "active" and kids[201]["work"]["task_id"] and kids[201]["work"]["branch"].startswith("worktree-")
    assert set(kids[201]["work"]) == {"phase", "state", "task_id", "branch", "alias", "attempt", "report", "auto_pr", "close", "pr_number", "waiting", "since", "issue_url", "claim_url"}
    assert kids[201]["work"]["issue_url"] == "https://github.com/acme/shop/issues/201" and re.fullmatch(r"https://github\.com/acme/shop/issues/201#issuecomment-[0-9]+", kids[201]["work"]["claim_url"])
    assert kids[202]["work"] is None and kids[202]["phase"] == kids[202]["bucket"]["state"] == "open"
    assert {"report", "auto_pr", "close", "plan"} <= set(d)
    tid = kids[201]["work"]["task_id"]
    ask_person(w, tid, 11 * 60)
    beat(w)
    with w.b.enter():
        kids = {c["number"]: c for c in nodes_epics.detail(w.b.db, "acme", "shop", 147, [], repos=[REPO], login="alice")["children"]}
    assert kids[201]["phase"] == "waiting" and kids[201]["work"]["waiting"] is True
    stop_turn(w, tid, "Done.")
    with w.b.enter():
        kids = {c["number"]: c for c in nodes_epics.detail(w.b.db, "acme", "shop", 147, [], repos=[REPO], login="alice")["children"]}
    assert kids[201]["phase"] == "done" and kids[201]["work"]["state"] == "ending", "the work is over; the comment waits for the cadence"
    w.gh.tick(301)
    beat(w)
    with w.b.enter():
        kids = {c["number"]: c for c in nodes_epics.detail(w.b.db, "acme", "shop", 147, [], repos=[REPO], login="alice")["children"]}
    assert kids[201]["phase"] == "done" and kids[201]["work"]["state"] == "finished"


def test_the_task_card_of_an_epic_task_says_so_and_no_other_card_does(w):
    d = run_route(w).json()["data"]
    plain = w.b.post("/api/tasks", json={"project": "shop", "repo": "api", "title": "Other", "prompt": "x", "when": "later", "issue_number": 7, "issue_url": "https://github.com/acme/shop/issues/7"}).json()
    other = w.b.post("/api/tasks", json={"project": "shop", "repo": "api", "title": "No issue", "prompt": "x", "when": "later"}).json()
    with w.b.enter():
        cards = {c["id"]: c for c in main._tasks_view()}
    assert cards[d["id"]]["epic"]["phase"] == "running" and cards[d["id"]]["issue_number"] == 201 and cards[d["id"]]["issue_url"].endswith("/issues/201")
    assert "epic" not in cards[plain["id"]] and "epic" not in cards[other["id"]]


def test_a_board_that_never_ran_epic_work_has_no_record_no_thread_no_key_and_the_hook_costs_nothing(w):
    import threading
    assert nodes_work.has_records(w.a.db) is False and nodes_work.work_views(w.a.db, [(REPO, 201)]) == {} and w.a.db.kv_get(nodes_work.INDEX) is None
    made = w.a.post("/api/tasks", json={"project": "shop", "repo": "api", "title": "From an issue", "prompt": "x", "when": "later", "issue_number": 201,
                                        "issue_url": "https://github.com/acme/shop/issues/201"}).json()
    w.gh.clear_calls()
    with w.a.enter():
        w.a.db.task_update(made["id"], phase="running")
        taskflow._ended(w.a.db, made["id"], "done")
        taskflow._ended(w.a.db, 99999, "failed")
    assert w.gh.calls() == [] and nodes_work.has_records(w.a.db) is False and not [t for t in threading.enumerate() if t.name == "ccboard-epic-report"]
    with w.a.enter(), w.mp.context() as mp:
        mp.setattr(nodes_work, "task_work", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a board with no work record must not even look")))
        assert [c["id"] for c in main._tasks_view()] == [made["id"]] and "epic" not in main._tasks_view()[0], "the poll pays nothing for a task that has an issue"
    assert taskflow.RESULT_HOOKS.count(nodes_work.on_task_result) == 1 and nodes_epics.LOST_HOOKS.count(nodes_work._on_lost) == 1 and nodes_epics.RENEW_HOOKS.count(nodes_work.on_beat) == 1
    nodes_work.register()
    assert taskflow.RESULT_HOOKS.count(nodes_work.on_task_result) == 1, "registering twice is once"


# ================================================================ held, restarted and idempotent

def test_a_second_run_of_an_issue_this_node_already_holds_answers_the_same_task(w):
    first = run_route(w).json()["data"]
    second = run_route(w)
    assert second.status_code == 200 and second.json()["data"]["reason"] == "already_held" and second.json()["data"]["id"] == first["id"]
    assert len(w.b.db.tasks()) == 1 and len(claims(w)) == 1


def test_a_beat_finishes_a_task_that_ended_without_the_hook(w):
    tid = run_route(w).json()["data"]["id"]
    with w.b.enter():
        w.b.db.task_update(tid, phase="failed", result="boom")
    beat(w)
    assert progress(w)[0].split("\n")[1].endswith(": failed") and claims(w)[0]["status"] == "failed" and rec_of(w)["state"] == "finished"


def test_a_restart_finishes_the_report_of_a_task_that_ended_while_the_board_was_down(w):
    a = run_route(w, 201).json()["data"]["id"]
    b = run_route(w, 202).json()["data"]["id"]
    with w.b.enter():
        w.b.db.task_update(a, phase="done", result="ok")
        rec = nodes_work._load(w.b.db, "acme", "shop", 202)
        rec.update(state="ending", ended="done", ended_at=1.0)
        nodes_work._save(w.b.db, rec)
        w.b.db.task_update(b, phase="done", result="ok")
        assert sorted(nodes_work.recover(w.b.db)) == ["ending", "ending"]
    assert claims(w, 201)[0]["status"] == "done" and claims(w, 202)[0]["status"] == "done"
    assert len(progress(w, 201)) == 1 and len(progress(w, 202)) == 1


def test_finished_records_are_forgotten_after_a_week(w):
    tid = run_route(w).json()["data"]["id"]
    stop_turn(w, tid, "Done.")
    with w.b.enter():
        assert nodes_work.recover(w.b.db) == []
    w.gh.tick(8 * 86400)
    with w.b.enter():
        assert nodes_work.recover(w.b.db) == ["forgotten"]
        assert nodes_work.has_records(w.b.db) is False


# ================================================================ hardening after review: one comment per phase, no hooks, one task per Run, wider redaction

def test_a_release_that_fails_on_every_beat_never_repeats_the_phase_comment(w, monkeypatch):
    tid = run_route(w).json()["data"]["id"]
    real = nodes_epics.release
    monkeypatch.setattr(nodes_epics, "release", lambda *a, **k: {"released": False, "reason": "unknown"})
    stop_turn(w, tid, "Done.")
    assert len(progress(w)) == 1 and rec_of(w)["state"] == "ending" and rec_of(w)["posted_phase"] == "done", "said once, and kept before the release"
    for _ in range(3):
        w.gh.tick(nodes_work.CADENCE_S + 1)                                      # past the cadence each time: only the saved phase stops a second comment
        beat(w)
    assert len(progress(w)) == 1
    monkeypatch.setattr(nodes_epics, "release", real)
    beat(w)
    assert len(progress(w)) == 1 and claims(w)[0]["status"] == "done" and rec_of(w)["state"] == "finished"


def test_auto_pr_commits_and_pushes_without_running_any_repository_hook(w, git):
    d = finished_task(w, auto_pr=True)
    git.branch = d["branch"]
    stop_turn(w, d["id"], "Done.")
    [commit] = git.commits()
    [push] = git.pushes()
    for argv in (commit, push):
        assert argv[:3] == ["git", "-c", "core.hooksPath=/dev/null"] and "--no-verify" in argv, argv


def test_the_person_triggered_pull_request_flow_keeps_its_hooks_and_its_old_commands(w, git, tmp_path):
    gitops.pr_create(tmp_path, "worktree-x", "main", "Title", "Body")
    assert git.pushes() == [["git", "push", "-u", "origin", "worktree-x"]]
    [create] = [c for c in git.calls if c[:3] == ["gh", "pr", "create"]]
    assert "--repo" not in create
    assert all("--repo" not in c for c in git.calls if c[:3] == ["gh", "pr", "view"])


def test_auto_pr_names_the_issues_own_repository_for_gh(w, git):
    d = finished_task(w, auto_pr=True, close=True)
    git.branch = d["branch"]
    stop_turn(w, d["id"], "Done.")
    [create] = [c for c in git.calls if c[:3] == ["gh", "pr", "create"]]
    assert create[create.index("--repo") + 1] == REPO
    [view] = [c for c in git.calls if c[:3] == ["gh", "pr", "view"]]
    assert view[view.index("--repo") + 1] == REPO
    assert re.findall(r"Closes #(\d+)", git.bodies[0]) == ["201"], "it names the worked issue's number and nothing else"


@pytest.mark.parametrize("real", ["main", "master", "HEAD", "worktree-someone-elses-task", ""])
def test_auto_pr_refuses_a_worktree_whose_real_branch_is_not_the_tasks_own(w, git, real):
    d = finished_task(w, auto_pr=True)
    git.branch = real
    stop_turn(w, d["id"], "Done.")
    assert git.commits() == [] and git.pushes() == [] and ["git", "add", "-A"] not in git.calls
    assert not [c for c in git.calls if c[:3] == ["gh", "pr", "create"]]
    [c] = progress(w)
    assert ": done\nBranch: " + d["branch"] in c and "https://" not in c, "the done comment is posted, without a link"
    assert claims(w)[0]["status"] == "done" and b_task(w, d["id"])["pr_url"] is None


def test_two_runs_of_one_issue_at_once_start_one_task(w, monkeypatch):
    import threading
    real, seen, other = main._tasks_create, [], {}

    def launch(*a, **k):
        seen.append(1)
        if len(seen) == 1:
            def second():
                try:
                    other["out"] = nodes_work.work_issue(REPO, 201, epic=147)
                except Exception as e:
                    other["err"] = e
            other["th"] = threading.Thread(target=second)
            other["th"].start()
            other["th"].join(0.5)                                                # the second Run arrives while the first is still launching
        return real(*a, **k)
    monkeypatch.setattr(main, "_tasks_create", launch)
    with w.b.enter():
        first = nodes_work.work_issue(REPO, 201, epic=147)
    other["th"].join(20)
    assert not other["th"].is_alive() and "err" not in other, other
    assert len(seen) == 1 and len(w.b.db.tasks()) == 1, "one worktree and one session"
    assert other["out"]["id"] == first["id"] and other["out"]["reason"] == "already_held"
    assert rec_of(w)["task_id"] == first["id"]


def test_the_stop_hook_does_not_wait_for_the_issue_lock(w, monkeypatch):
    import threading
    tid = run_route(w).json()["data"]["id"]
    ran = []
    monkeypatch.setattr(nodes_work, "spawn", lambda fn: ran.append(fn))
    done = threading.Event()
    with w.b.enter():
        db = w.b.db
        db.task_update(tid, phase="done", result="ok")
        with nodes_work._lock_for(db, "acme", "shop", 201):                      # a beat or a report holds it across GitHub and git
            th = threading.Thread(target=lambda: (nodes_work.on_task_result(db, tid, "done"), done.set()))
            th.start()
            assert done.wait(2.0), "the hook returned while the lock was held"
        th.join(5)
        assert len(ran) == 1, "the marking and the report run on a thread of their own"
        ran[0]()
    assert rec_of(w)["state"] == "finished" and len(progress(w)) == 1


def test_a_body_of_repeated_comment_openers_is_scanned_in_linear_time():
    import time
    body = "<!--" * 6000                                                          # 24 KB, none of them closed
    t = time.perf_counter()
    assert issues._comment_free(body) == ""
    assert issues._comment_free("a<!-- b -->c<!--" * 3000).startswith("ac")
    assert "<!--" not in nodes_work.build_prompt(5, "t", body + "text", "alice", "node-b", 1)
    assert time.perf_counter() - t < 1.0
    assert issues._comment_free("x<!-- y --> z <!--> w -->v") == "x z v", "same reading as before: `<!-->` does not close itself"


def test_a_hidden_reference_comment_is_not_in_the_prompt():
    body = "Fix the form.\n[//]: # (" + "secret plan one)\n[x]: <> \"secret plan two\"\n   [note]: #\n[link]: https://example.com \"shown title\"\nTail."
    p = nodes_work.build_prompt(5, "t", body, "alice", "node-b", 1)
    assert "secret plan" not in p and "Fix the form." in p and "Tail." in p
    assert "[link]: https://example.com" in p, "a real link definition stays"


SHAPES = {
    "url_credentials": ("clone https://bob:" + "hunter2@example.com/x.git ok", ("hunter2", "bob:")),
    "root_home": ("edited /root/proj/a.py", ("/root",)),
    "windows_home": ("opened C:\\" + "Users\\Ann Lee\\work\\a.py and C:/" + "Users/bob/x.py", ("Ann", "bob", "Users")),
    "private_var": ("wrote /private/var/ab12/out.txt", ("/private", "ab12")),
    "var_folders": ("wrote /var/folders/zz/ab12/T/out.txt", ("/var", "ab12")),
    "home_with_spaces": ("saw " + MAC + "Ann Lee/work/app.py and /ho" + "me/Bo Day/x.py", ("Ann", "Lee", "Bo Day")),
    "assignments": ("pass" + "word=hunter2 TOKEN='two words' api_" + "key = zzz sec" + "ret=\"a b\"", ("hunter2", "two words", "zzz", "a b")),
    "email": ("mail alex@example.com or alex.o+x@mail.example.org", ("alex", "example.com")),
    "tailnet_host": ("on node-a.tailnet" + ".ts.net:8080 and mybox.local and srv.internal/x and b.lan.", ("tailnet", "mybox", "srv.", "b.lan")),
}


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_the_wider_redaction_shapes_are_removed_and_a_second_pass_changes_nothing(name):
    raw, gone = SHAPES[name]
    text, removed = GW.redact_result(raw)
    assert not any(g in text for g in gone), text
    assert GW.redact_result(text) == (text, removed)
    assert removed is (REMOVED_WORD in text), "the note is wanted exactly when a [removed] is in the text"
    assert GW.valid_body(GW.progress_body("node-b", "done", text=text, removed=removed)), "the writer takes what the redaction left"


REMOVED_WORD = "[removed]"


def test_the_wider_redaction_keeps_github_links_and_plain_words():
    plain = "Opened https://github.com/acme/shop/pull/3 and https://github.com/acme/shop/issues/201. Plan: edit config.json, the planner, src/root/x.py, tokenizer.py and the local setup."
    assert GW.redact_result(plain) == (plain, False)
    assert GW.redact_result("home ~/x and ~")[0] == "home ~/x and ~"
    assert GW.progress_body("node-b", "done", branch="worktree-x", pr=("acme", "shop", 3))


def test_the_wider_redaction_is_linear_on_long_runs():
    import time
    t = time.perf_counter()
    for raw in ("a." * 2000, "//a:" * 1000, "a" * 3990 + "@", "tok" + "en=" * 1300, MAC + "a " * 1900, "/var/folders/" * 300, "a@" * 2000):
        GW.redact_result(raw)
    assert time.perf_counter() - t < 1.0


# ==== END ====
