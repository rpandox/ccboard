"""Team P1, issue #197: the team reader (app/team_github.py) and the conditional-read wrapper (app/github.py), against the fake GitHub of tests/fake_github.py.

GitHub is a fake: an in-memory world with alice and bob, served in process (use_memory) for the many-step tests and through the fake `gh` script for the ones that look at the real
command lines and exit codes. Time is a virtual clock the poller, the reader and the fake share, so an hour of polling takes milliseconds. No test reaches the real gh, GitHub, git
over a network or the real home.

Pinned here: a gh that prints only `gh: HTTP 304` and exits 1 reads as "not modified" (and nothing else does); Bob's assignee, linked branch and draft pull request, and a pull request
from his fork, appear on Alice's board with no ccboard on Bob's side; SSO, no access, not found, a low remainder and a rate limit are each their own state; a board with nothing shared
makes no gh call, starts no thread and writes no row; only this board's own repositories can be read; every gh call is a GET on an allowed path (or the one constant GraphQL query);
GitHub text is capped and cleaned; the poll stays inside its hourly budget, with the points per hour for 1, 5 and 20 repositories measured.
"""
from __future__ import annotations

import json
import subprocess
import threading
from types import SimpleNamespace

import pytest

from app import github, node_state, nodes, team, team_github
from tests.fake_github import REAL_API, START, FakeGitHub, iso

ID = {"Tailscale-User-Login": "alice@example.com"}
H = {**ID, "X-CCBoard": "1"}
REPO = "acme/shop"
KEY = "github.com/acme/shop"


class Clock:
    """One virtual clock for the poller, its reader and the fake GitHub (its Date header and `now`)."""

    def __init__(self, gh):
        self.t = 1000.0
        self.gh = gh

    def mono(self):
        return self.t

    def wall(self):
        return self.gh.world["now"]

    def advance(self, s):
        self.t += s
        self.gh.world["now"] += s


def seed(gh, repo=REPO):
    """alice (the board's login) and bob: bob has issue 1 assigned and a linked branch, a draft pull request that closes it and a pushed branch; alice has issue 3."""
    gh.repo(repo, private=True)
    gh.add_issue(repo, 1, "Fix login", labels=["bug"])
    gh.assign(f"{repo}#1", "bob")
    gh.add_issue(repo, 2, "Unassigned thing")
    gh.add_issue(repo, 3, "Alice's own issue")
    gh.assign(f"{repo}#3", "alice")
    gh.pull(repo, 7, "WIP: fix login", user="bob", draft=True, head_ref="bob/fix-login", closes=[1])
    gh.linked(f"{repo}#1", "bob/fix-login", author="bob")
    gh.tip(repo, "bob/fix-login", author="bob")
    gh.tip(repo, "bob/spike", author="bob")
    gh.tip(repo, "main", author="alice")
    gh.repo(repo, default_branch="main")


@pytest.fixture
def tw(lite_client, projects_dir, tmp_path, monkeypatch):
    from app import main, projects
    for proj, repo in (("shop", "api"), ("shop", "web"), ("ops", "tools")):
        (projects_dir / proj / repo / ".git").mkdir(parents=True)
    origins = {"api": "git@github.com:acme/shop.git", "web": "https://gitlab.com/acme/web.git", "tools": "https://github.com/acme/tools.git"}
    upstreams: dict = {}
    sessions: dict = {}
    monkeypatch.setattr(main, "_merged_sessions", lambda rich=False: (dict(sessions), False))
    monkeypatch.setattr(projects, "git_info_reused", lambda path, max_age: {"branch": "main", "dirty": False, "state": "ok"})
    monkeypatch.setattr(node_state, "_origin_url", lambda path: origins.get(path.name))
    monkeypatch.setattr(node_state, "_upstream_url", lambda path: upstreams.get(path.name))
    node_state.slug_cache_clear()
    team.clear_hints()
    main._invalidate_scan()
    gh = FakeGitHub(tmp_path).install(monkeypatch).use_memory(monkeypatch)
    gh.world["login"] = "alice"
    clock = Clock(gh)
    poller = team_github.Poller(main.db, main._team_projects, mono=clock.mono, wall=clock.wall, reader=github.Reader(mono=clock.mono), pause=clock.advance)
    monkeypatch.setattr(team_github, "poller", poller)
    monkeypatch.setattr(github.reader, "mono", clock.mono)

    def settle(seconds=3600.0, until=None):
        """Run the poll in virtual time until `until()` is true (default: every shared repository has been read twice over, REST and GraphQL), or `seconds` passed."""
        end = clock.t + seconds
        done = until or (lambda: bool(poller.reads) and all(r["gql_at"] for r in poller.reads.values()))
        while clock.t < end:
            w = poller.step()
            if w is None or done():
                break
            clock.advance(max(w, 0.01))

    def run_for(seconds):
        end = clock.t + seconds
        while clock.t < end:
            w = poller.step()
            if w is None:
                break
            clock.advance(max(min(w, end - clock.t), 0.01))

    def share(slug_key=KEY):
        for k in ([slug_key] if isinstance(slug_key, str) else slug_key):
            main.db.team_share_set(k, True)

    def body():
        me = poller.me if poller.me[0] else team_github.identity_cached()
        return team.build(poller.entries(), poller.snapshot(), me[0], me[1], clock.wall())

    def block(slug=REPO):
        return next(r for r in body()["repos"] if r["slug"].lower() == slug.lower())

    yield SimpleNamespace(c=lite_client, main=main, db=main.db, gh=gh, clock=clock, poller=poller, settle=settle, run_for=run_for, share=share, body=body, block=block,
                          origins=origins, upstreams=upstreams, sessions=sessions, projects_dir=projects_dir)
    poller.stop(2.0)                                            # a route that switched a repository on may have started the thread


def person(block, login):
    return next((p for p in block["people"] if p["login"] == login), None)


# ================================================================ the 304 wrapper

NM = [(1, b"", "gh: HTTP 304\n", True), (1, b"", "gh: Not Modified (HTTP 304)\n", True), (1, b"", "\ngh: HTTP 304", True),
      (1, b"", "gh: HTTP 3040\n", False), (1, b"", "gh: HTTP 404\n", False), (1, b"", "gh: HTTP 500\n", False), (1, b"", "gh: HTTP 303\n", False), (1, b"", "gh: HTTP 30\n", False),
      (2, b"", "gh: HTTP 304\n", False), (4, b"", "gh: HTTP 304\n", False), (0, b"", "gh: HTTP 304\n", False), (1, b"", "", False), (1, b"", "error: HTTP 304\n", False),
      (1, b"HTTP/2.0 200 OK\r\n\r\n{}", "gh: HTTP 304\n", False)]


@pytest.mark.parametrize("rc,out,err,want", NM)
def test_only_exit_1_with_gh_saying_http_304_is_not_modified(rc, out, err, want):
    assert github.is_not_modified(rc, out, err) is want


def fake_run(monkeypatch, rc, out=b"", err=b""):
    ran = []

    def run(args, **kw):
        ran.append(args)
        return subprocess.CompletedProcess(args, rc, out, err)
    monkeypatch.setattr(github.subprocess, "run", run)
    monkeypatch.setattr(github.shutil, "which", lambda name: "/fake/gh")
    monkeypatch.setattr(github, "api", REAL_API)                       # tests/conftest.py refuses it; here the command is faked one level down
    return ran


PATH = github.open_pulls_path("acme", "shop")
TAG = 'W/"abc123"'


def test_a_gh_that_prints_only_http_304_and_exits_1_reads_as_not_modified_and_nothing_is_parsed(monkeypatch):
    ran = fake_run(monkeypatch, 1, b"", b"gh: HTTP 304\n")
    parsed = []
    monkeypatch.setattr(github, "json", SimpleNamespace(loads=lambda *a, **k: parsed.append(a) or {}))
    status, headers, data = github.api(PATH, TAG)
    assert (status, data) == (304, None) and github.server_time(headers) is not None, "no JSON, and a Date so the reader's clock moves on"
    assert parsed == [] and ran and "If-None-Match: " + TAG in ran[0]


@pytest.mark.parametrize("rc,err", [(1, b"gh: HTTP 404\n"), (1, b"gh: HTTP 502\n"), (1, b"gh: HTTP 3040\n"), (2, b"gh: HTTP 304\n"), (1, b""), (1, b"gh: Not Found")])
def test_any_other_non_zero_exit_without_an_http_head_is_still_an_error(monkeypatch, rc, err):
    fake_run(monkeypatch, rc, b"", err)
    with pytest.raises(github.GhReadError) as e:
        github.api(PATH, TAG)
    assert e.value.reason == "unavailable"


def test_a_304_nobody_asked_for_is_an_error(monkeypatch):
    fake_run(monkeypatch, 1, b"", b"gh: HTTP 304\n")
    with pytest.raises(github.GhReadError):
        github.api(PATH)                                    # no If-None-Match went out, so no 304 can be an answer


def test_the_real_head_of_a_304_still_works_and_the_reader_keeps_its_cache(tw):
    tw.gh.world["login"] = "alice"
    r = github.Reader(mono=tw.clock.mono)
    a = r.get(PATH, 0.0)
    b = r.get(PATH, 0.0)
    assert a.source == "network" and b.source == "revalidated" and b.data == a.data


def test_through_the_fake_gh_script_a_bare_304_is_not_modified(tmp_path, monkeypatch):
    gh = FakeGitHub(tmp_path).install(monkeypatch)
    gh.pull(REPO, 7, "A", user="bob")
    gh.set(bare_304=True)
    status, headers, data = github.api(PATH)
    assert status == 200 and isinstance(data, list)
    status2, _, data2 = github.api(PATH, headers["etag"])
    assert (status2, data2) == (304, None)
    assert any("If-None-Match" in " ".join(c["argv"]) for c in gh.calls())
    gh.pull(REPO, 8, "B", user="bob")
    status3, _, data3 = github.api(PATH, headers["etag"])
    assert status3 == 200 and len(data3) == 2
    gh.set(login=None)
    with pytest.raises(github.GhReadError) as e:
        github.api(PATH, headers["etag"])
    assert e.value.reason == "no_login"


# ================================================================ the paths and the one query

def test_the_team_paths_are_allowed_get_paths_and_nothing_else_changes():
    for path in (github.assigned_issues_path("acme", "shop"), github.assigned_issues_path("acme", "shop", 3, 50), github.open_pulls_path("a-b", "r.s_t-u", 2)):
        assert github.allowed_path(path), path
        a = github.argv(path, TAG)
        assert a[:5] == ["gh", "api", "--include", "--method", "GET"] and a[-1] == path and a.count("--method") == 1
        assert not set(a) & {"-X", "-f", "-F", "--field", "--raw-field", "--input", "--paginate", "--jq"}
    for bad in ("/repos/acme/shop/pulls?state=all&per_page=100&page=1", "/repos/acme/shop/pulls?state=open&per_page=100&page=1&x=1", "/repos/-a/shop/pulls?state=open&per_page=100&page=1",
                "/repos/acme/shop/issues?state=open&assignee=bob&per_page=100&page=1", "/repos/acme/../pulls?state=open&per_page=100&page=1"):
        assert not github.allowed_path(bad), bad
    for owner, repo in (("", "r"), ("a b", "r"), ("a", ".."), ("a", "r;x"), ("-a", "r")):
        with pytest.raises(github.GhReadError):
            github.open_pulls_path(owner, repo)


def test_the_graphql_query_is_one_constant_with_no_mutation_and_only_owner_and_name_vary():
    assert "mutation" not in github.GQL_TEAM.lower() and "subscription" not in github.GQL_TEAM.lower() and github.GQL_TEAM.startswith("query(")
    a = github.graphql_argv("acme", "shop", "gh")
    assert a[:4] == ["gh", "api", "graphql", "--include"] and a[4:6] == ["-f", "query=" + github.GQL_TEAM] and a[6:] == ["-f", "owner=acme", "-f", "name=shop"]
    assert "--method" not in a and "-F" not in a and "--input" not in a
    for owner, repo in (("a b", "r"), ("a", ".."), ("a", "-x"), ("", "r"), ("a", "r\nx")):
        with pytest.raises(github.GhReadError):
            github.graphql_argv(owner, repo)


def test_graphql_errors_map_to_fixed_reasons():
    r = github.graphql_reason
    assert r({"data": {}}) is None and r({"errors": []}) is None and r(None) is None
    assert r({"errors": [{"type": "FORBIDDEN", "message": "Resource protected by organization SAML enforcement."}]}) == "sso"
    assert r({"errors": [{"type": "FORBIDDEN", "message": "nope"}]}) == "no_access"
    assert r({"errors": [{"type": "NOT_FOUND", "message": "x"}]}) == "not_found"
    assert r({"errors": [{"type": "RATE_LIMITED", "message": "x"}]}) == "rate_limited"
    assert r({"errors": [{"type": "OTHER", "message": "boom"}]}) == "bad_answer"


# ================================================================ two people

def test_bobs_assignee_linked_branch_and_draft_pr_appear_on_alices_board_without_ccboard_on_his_side(tw):
    seed(tw.gh)
    tw.share()
    tw.settle()
    blk = tw.block()
    assert blk["state"] == "ok" and blk["shared"] is True and blk["stale"] is False and blk["graph_pending"] is False and blk["private"] is True
    assert tw.body()["me"] == {"login": "alice", "id": tw.gh.world.get("ids", {}).get("alice") or 1000 + sum(ord(c) * (i + 1) for i, c in enumerate("alice"))}
    bob, alice = person(blk, "bob"), person(blk, "alice")
    assert bob["you"] is False and alice["you"] is True and blk["people"][0]["login"] == "alice", "the owner is marked and listed first"
    assert [(i["number"], i["title"], i["branches"]) for i in bob["issues"]] == [(1, "Fix login", ["bob/fix-login"])]
    assert bob["issues"][0]["url"] == "https://github.com/acme/shop/issues/1"
    assert [(p["number"], p["draft"], p["state"], p["head"], p["closes"], p["from_fork"]) for p in bob["prs"]] == [(7, True, "draft", "bob/fix-login", [1], None)]
    assert {(b["name"], b["source"]) for b in bob["branches"]} == {("bob/fix-login", "linked"), ("bob/spike", "pushed")}
    assert [i["number"] for i in alice["issues"]] == [3] and blk["counts"]["teammates"] == 1
    assert "main" not in {b["name"] for p in blk["people"] for b in p["branches"]}, "the default branch is not work"


def test_people_are_keyed_on_the_numeric_id_so_a_rename_is_one_person(tw):
    seed(tw.gh)
    tw.gh.world["ids"] = {"bob": 4242, "bobby": 4242, "alice": 1}
    tw.gh.assign(f"{REPO}#1", "bob")
    tw.gh.pull(REPO, 9, "Later work", user="bobby", head_ref="bobby/later")
    tw.share()
    tw.settle()
    blk = tw.block()
    names = [p["login"] for p in blk["people"]]
    assert len([p for p in blk["people"] if p["id"] == 4242]) == 1 and len(names) == 2
    assert {i["number"] for i in next(p for p in blk["people"] if p["id"] == 4242)["issues"]} == {1}
    one = next(p for p in blk["people"] if p["id"] == 4242)
    assert [p["number"] for p in one["prs"]] == [7, 9] and [i["number"] for i in one["issues"]] == [1]


def test_a_teammate_working_from_a_fork_is_listed_under_the_shared_repo(tw):
    seed(tw.gh)
    tw.gh.pull(REPO, 8, "From my fork", user="bob", head_ref="feature", head_repo="bob/shop")
    tw.share()
    tw.settle()
    prs = {p["number"]: p for p in person(tw.block(), "bob")["prs"]}
    assert prs[8]["from_fork"] == "bob/shop" and prs[8]["head"] == "feature" and prs[8]["url"] == "https://github.com/acme/shop/pull/8"
    assert prs[7]["from_fork"] is None


def test_a_pull_request_whose_fork_was_deleted_is_still_listed(tw):
    seed(tw.gh)
    tw.gh.pull(REPO, 8, "Orphan", user="bob", head_ref="feature", head_repo=None)
    tw.share()
    tw.settle()
    pr = next(p for p in person(tw.block(), "bob")["prs"] if p["number"] == 8)
    assert pr["from_fork"] is None and pr["head"] == "feature"


def test_a_claim_comment_is_read_and_attributed_to_the_commenter_and_its_absence_is_fine(tw):
    from tests.fake_github import claim_body
    seed(tw.gh)
    tw.gh.add_issue(REPO, 5, "Epic child", labels=["ccboard:in-progress"])
    tw.gh.assign(f"{REPO}#5", "bob")
    tw.share()
    tw.settle()
    assert person(tw.block(), "bob")["issues"][-1]["claim"] is None, "an issue in progress with no claim comment is shown without a claim"
    tw.gh.comment(f"{REPO}#5", claim_body(alias="node-b", status="running"), user="bob", created_at=iso(tw.gh.world["now"] - 60))
    tw.gh.comment(f"{REPO}#5", "not a claim, just chatter", user="alice")
    tw.gh.comment(f"{REPO}#5", claim_body(alias="x", status="claimed").replace("ccboard claim:", "ccboard claim:", 1), user="mallory")
    tw.poller.wake()
    tw.settle(until=lambda: person(tw.block(), "mallory") is not None)
    claim = next(i for i in person(tw.block(), "bob")["issues"] if i["number"] == 5)["claim"]
    assert claim["status"] == "running" and claim["live"] is True
    assert "branch" in claim and "task_ref" not in json.dumps(tw.block()), "no task ref or node alias is carried on"


# ================================================================ access states

def test_sso_no_access_and_not_found_are_three_different_states_and_none_is_no_teammates(tw):
    for slug in ("acme/shop", "acme/tools"):
        tw.gh.repo(slug, private=True)
    tw.projects_dir  # noqa: B018
    tw.origins["tools"] = "https://github.com/acme/api.git"
    node_state.slug_cache_clear()
    tw.main._invalidate_scan()
    tw.share(["github.com/acme/shop", "github.com/acme/api"])
    tw.origins["web"] = "https://github.com/acme/gone.git"
    node_state.slug_cache_clear()
    tw.main._invalidate_scan()
    tw.share("github.com/acme/gone")
    tw.gh.world["sso"] = ["acme/shop"]
    tw.gh.world["forbidden"] = ["acme/api"]
    tw.gh.world["hidden"] = ["acme/gone"]
    tw.run_for(30)
    states = {r["slug"]: r for r in tw.body()["repos"] if r["shared"]}
    assert (states["acme/shop"]["state"], states["acme/api"]["state"], states["acme/gone"]["state"]) == ("sso", "no_access", "not_found")
    assert "authorised for the organization" in states["acme/shop"]["message"] and states["acme/shop"]["message"] != states["acme/api"]["message"]
    for r in states.values():
        assert r["people"] == [] and r["stale"] is True and r["read_at"] is None, "an access problem has no teammates list at all, not an empty one that reads as 'nobody'"
    assert "x-github-sso" not in json.dumps(tw.body()).lower() and "authorization_request" not in json.dumps(tw.body())
    n = len(tw.gh.calls())
    tw.run_for(60)
    assert len(tw.gh.calls()) == n, "a refused repository is not asked again every poll (it is retried every ten minutes)"
    tw.run_for(team_github.ERROR_RETRY_S + 5)
    assert len(tw.gh.calls()) > n


def test_an_access_problem_after_a_good_read_keeps_the_data_and_marks_it_stale(tw):
    seed(tw.gh)
    tw.share()
    tw.settle()
    assert tw.block()["stale"] is False
    tw.gh.world["sso"] = [REPO]
    tw.poller.wake()
    tw.run_for(30)
    blk = tw.block()
    assert blk["state"] == "sso" and blk["stale"] is True and person(blk, "bob") is not None, "the last good data stays, as old data"
    tw.gh.world["sso"] = []
    tw.poller.wake()
    tw.run_for(team_github.ERROR_RETRY_S + 30)
    assert tw.block()["state"] == "ok" and tw.block()["stale"] is False


def test_a_graphql_sso_error_is_the_same_state(tw, monkeypatch):
    seed(tw.gh)
    tw.share()
    monkeypatch.setattr(github, "graphql", lambda o, r: (200, {}, {"data": {"repository": None}, "errors": [{"type": "FORBIDDEN", "message": "Resource protected by organization SAML enforcement."}]}))
    tw.run_for(30)
    assert tw.block()["state"] == "sso"


def test_a_rate_limit_answer_is_its_own_state_with_the_wait_and_the_poll_stays_away(tw):
    seed(tw.gh)
    tw.share()
    tw.gh.fail("/pulls", 403, headers={"retry-after": "120"}, body={"message": "You have exceeded a secondary rate limit."}, times=1)
    tw.run_for(20)
    blk = tw.block()
    assert blk["state"] == "rate_limited" and blk["wait_s"] and "wait" in blk["message"] and blk["people"] == []
    calls = len(tw.gh.calls())
    tw.run_for(60)
    assert len(tw.gh.calls()) == calls, "no call while GitHub asked to wait"
    tw.run_for(200)
    assert tw.block()["state"] == "ok"


def test_a_low_remainder_stops_the_poll_until_the_limit_resets_and_it_says_so(tw):
    seed(tw.gh)
    tw.share()
    tw.gh.world["rate"] = {"limit": 5000, "remaining": 120, "reset": int(START + 900)}
    tw.run_for(20)
    blk = tw.block()
    assert blk["state"] == "low_budget" and "few GitHub requests" in blk["message"] and blk["wait_s"]
    assert blk["graph_pending"] is True and blk["stale"] is True, "the REST part was read before the floor was seen: shown, as old data, with the branch links not read"
    calls = len(tw.gh.calls())
    tw.run_for(120)
    assert len(tw.gh.calls()) == calls, "below the floor nothing is asked"
    tw.gh.world["rate"] = {"limit": 5000, "remaining": 4900, "reset": int(START + 4500)}
    tw.run_for(1200)
    assert tw.block()["state"] == "ok"


def test_the_intervals_stretch_as_the_remainder_falls(tw):
    seed(tw.gh)
    tw.share()
    tw.gh.world["rate"] = {"limit": 5000, "remaining": 4900, "reset": int(START + 3000)}
    tw.settle()
    base = tw.poller.sched[KEY]["rest"] - tw.clock.t
    tw.gh.world["rate"] = {"limit": 5000, "remaining": 1000, "reset": int(START + 3000)}
    tw.poller.reader.cache.clear()
    tw.poller.wake()
    tw.run_for(10)
    slow = tw.poller.sched[KEY]["rest"] - tw.clock.t
    assert 0 < base <= team_github.REST_S and slow > 3 * base - 10


def test_no_login_and_no_gh_are_states_not_crashes(tw, monkeypatch):
    seed(tw.gh)
    tw.share()
    tw.gh.world["login"] = None
    monkeypatch.setattr(github, "api", lambda p, e=None: (_ for _ in ()).throw(github.GhReadError("no_login")))
    tw.run_for(10)
    assert tw.block()["state"] == "no_login"
    monkeypatch.setattr(github, "api", lambda p, e=None: (_ for _ in ()).throw(github.GhReadError("no_gh")))
    tw.poller.wake()
    tw.run_for(team_github.ERROR_RETRY_S + 20)
    assert tw.block()["state"] == "no_gh"


# ================================================================ off means off

def test_with_nothing_shared_nothing_is_asked_started_or_written(tw, monkeypatch):
    seed(tw.gh)
    asked = []
    tw.poller.projects_fn = lambda: asked.append(1) or []
    assert tw.poller.step() is None and asked == [], "not even the project scan is asked for"
    assert tw.poller.ensure_running() is False and tw.poller.thread is None
    assert not [t for t in threading.enumerate() if t.name == "team-poll"]
    tw.poller.wake()
    tw.poller.forget(["x"])
    assert tw.gh.calls() == []
    rows = tw.db.conn.execute("select key from kv where key like 'team:%'").fetchall()
    assert rows == []
    st = tw.c.get("/api/state", headers=ID).json()
    assert not [k for k in st if "team" in k.lower()], "no new /api/state key"
    r = tw.c.get("/api/team/state", headers=ID)
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert tw.gh.calls() == [] and tw.db.conn.execute("select count(*) from kv where key like 'team:%'").fetchone()[0] == 0
    assert all(b["state"] == "off" and b["people"] == [] for b in r.json()["repos"])


def test_switching_off_again_removes_the_row_and_the_read_and_stops_asking(tw):
    seed(tw.gh)
    tw.share()
    tw.settle()
    assert tw.db.team_shared() and tw.poller.reads
    r = tw.c.put("/api/team/repos/acme/shop", headers=H, json={"shared": False})
    assert r.status_code == 200 and r.json()["state"] == "off" and r.json()["shared"] is False and r.json()["people"] == []
    assert tw.db.team_shared() == {} and tw.poller.reads == {}
    assert tw.db.conn.execute("select count(*) from kv where key like 'team:%'").fetchone()[0] == 0
    n = len(tw.gh.calls())
    tw.run_for(600)
    assert len(tw.gh.calls()) == n and tw.poller.step() is None


def test_only_the_repositories_of_this_boards_own_projects_can_be_shared_or_read(tw):
    seed(tw.gh)
    seed(tw.gh, "other/secret")
    r = tw.c.put("/api/team/repos/other/secret", headers=H, json={"shared": True})
    assert r.status_code == 404 and "no project" in r.json()["error"]
    assert tw.db.team_shared() == {} and tw.gh.calls() == []
    for bad in ("-x/shop", "acme/..", "acme/a b"):
        assert tw.c.put(f"/api/team/repos/{bad}", headers=H, json={"shared": True}).status_code in (400, 404, 422)
    assert tw.db.team_shared() == {}
    tw.db.kv_set("team:share:github.com/other/secret", {"on": True})           # even a hand-written switch row for a repository the board does not have reads nothing
    tw.run_for(60)
    assert [p for p in tw.gh.paths() if "other/secret" in p] == []


def test_a_payload_that_names_another_repository_never_makes_the_board_read_it(tw):
    seed(tw.gh)
    tw.gh.world["issues"][f"{REPO}#1"]["raw"] = {"repository_url": "https://api.github.com/repos/evil/other", "html_url": "https://github.com/evil/other/issues/1"}
    tw.gh.pull(REPO, 9, "Sneaky", user="bob", head_ref="x", head_repo="evil/other")
    tw.share()
    tw.settle()
    assert all("evil" not in p for p in tw.gh.paths()), tw.gh.paths()
    blk = tw.block()
    assert all("evil" not in (i["url"]) for p in blk["people"] for i in p["issues"] + p["prs"])


def test_the_switch_routes_start_the_poll_only_when_something_is_on(tw):
    seed(tw.gh)
    assert tw.c.post("/api/team/refresh", headers=H).json() == {"queued": False, "reason": "nothing_shared"}
    assert tw.gh.calls() == []
    r = tw.c.put("/api/team/repos/Acme/Shop", headers=H, json={"shared": True})                   # case does not matter
    assert r.status_code == 200 and r.json()["shared"] is True and r.json()["state"] in ("loading", "ok") and r.json()["slug"] == "acme/shop"
    assert tw.db.team_shared() == {KEY: tw.db.team_shared()[KEY]}
    assert tw.c.put("/api/team/repos/acme/shop", headers=H, json={"shared": "yes"}).status_code == 422
    assert tw.c.put("/api/team/repos/acme/shop", headers=H, json={"shared": True, "extra": 1}).status_code == 422
    tw.poller.stop(2.0)
    assert tw.c.post("/api/team/refresh", headers=H).json() == {"queued": True}
    tw.poller.stop(2.0)


def test_the_poll_thread_starts_with_a_shared_repo_reads_and_stops(tw, monkeypatch):
    import time
    seed(tw.gh)
    monkeypatch.setattr(team_github, "MIN_GAP", 0.0)
    real = team_github.Poller(tw.db, tw.main._team_projects, reader=github.Reader(), pause=lambda s: None)
    monkeypatch.setattr(team_github, "poller", real)
    tw.share()
    assert real.ensure_running() is True
    for _ in range(100):
        if real.reads.get(KEY, {}).get("gql_at"):
            break
        time.sleep(0.05)
    assert real.reads[KEY]["status"] == "ok" and real.thread.is_alive()
    real.stop(3.0)
    assert not real.thread.is_alive()


# ================================================================ conditional reads

def test_an_unchanged_repository_is_revalidated_with_its_etag_and_nothing_is_parsed_again(tw, monkeypatch):
    seed(tw.gh)
    tw.share()
    tw.settle()
    parsed = []
    real_issues, real_pulls = team_github.parse_issues, team_github.parse_pulls
    monkeypatch.setattr(team_github, "parse_issues", lambda *a, **k: parsed.append("issues") or real_issues(*a, **k))
    monkeypatch.setattr(team_github, "parse_pulls", lambda *a, **k: parsed.append("pulls") or real_pulls(*a, **k))
    before = tw.block()
    n = len(tw.poller.reader.cache)
    tw.poller.sched[KEY]["rest"] = 0.0                                  # the REST lists are due again, the GraphQL read is not
    tw.run_for(10)
    assert parsed == [] and len(tw.poller.reader.cache) == n
    assert tw.block()["read_at"] != before["read_at"] and tw.block()["people"] == before["people"], "confirmed again, the same people"
    tw.gh.pull(REPO, 9, "New one", user="bob", head_ref="n")
    tw.poller.sched[KEY]["rest"] = 0.0
    tw.run_for(10)
    assert "pulls" in parsed and 9 in [p["number"] for p in person(tw.block(), "bob")["prs"]]


def test_through_the_fake_gh_script_the_second_read_sends_if_none_match_and_gets_a_304(tmp_path, monkeypatch, projects_dir):
    gh = FakeGitHub(tmp_path).install(monkeypatch)
    gh.set(bare_304=True)
    seed(gh)
    r = github.Reader()
    p = github.assigned_issues_path("acme", "shop")
    assert r.get(p, 0.0).source == "network"
    assert r.get(p, 0.0).source == "revalidated"
    sent = [c["argv"] for c in gh.calls() if c["argv"][-1] == p]
    assert len(sent) == 2 and not any("If-None-Match" in a for a in sent[0]) and any(a.startswith("If-None-Match: W/") for a in sent[1])


# ================================================================ every call is a GET, or the one query

def test_every_gh_call_of_a_full_poll_is_a_get_on_an_allowed_path_or_the_one_graphql_query(tw):
    seed(tw.gh)
    tw.share()
    tw.settle()
    tw.poller.wake()
    tw.run_for(10)
    calls = tw.gh.calls()
    assert len(calls) > 6
    for c in calls:
        a = c["argv"]
        if a[:2] == ["api", "graphql"]:
            assert a == github.graphql_argv("acme", "shop", "gh")[1:]
            continue
        assert a[:2] == ["api", "--method"] and a[2] == "GET" and github.allowed_path(a[-1]), a
    assert [p for p in tw.gh.paths() if p.startswith("graphql:")] == ["graphql:acme/shop"] * len(tw.gh.graph_calls())
    assert tw.gh.writes() == []


def test_through_the_fake_gh_script_the_full_command_lines_have_no_write_flags(tmp_path, monkeypatch, projects_dir):
    gh = FakeGitHub(tmp_path).install(monkeypatch)
    seed(gh)
    r = github.Reader()
    for make in (github.assigned_issues_path, github.open_pulls_path):
        assert r.get(make("acme", "shop"), 0.0).source == "network"
    status, headers, data = github.graphql("acme", "shop")
    assert status == 200 and headers["x-ratelimit-resource"] == "graphql" and data["data"]["repository"]["nameWithOwner"] == REPO
    for c in gh.calls():
        a = c["argv"]
        assert not set(a) & {"-X", "-F", "--field", "--raw-field", "--input", "--paginate", "--jq", "--hostname"}, a
        assert "--method" not in a or a[a.index("--method") + 1] == "GET"
    assert gh.writes() == [] and github.GQL_TEAM in " ".join(gh.calls()[-1]["argv"])


def test_the_team_modules_never_run_git_or_a_subprocess_and_team_py_has_no_network():
    import ast
    import pathlib
    root = pathlib.Path(team_github.__file__).parent
    for name, banned in (("team_github.py", {"subprocess", "socket", "urllib.request", "requests", "http.client", "http.server"}),
                         ("team.py", {"subprocess", "socket", "urllib.request", "requests", "http.client", "os", "pathlib"})):
        tree = ast.parse((root / name).read_text())
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {(n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0}
        assert not imported & banned, (name, imported & banned)
        strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) < 40}
        assert not strings & {"fetch", "git", "pull", "clone", "checkout", "remote"}, "no git command is built: the owner's checkout is never changed"
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not calls & {"run", "Popen", "check_output", "urlopen", "connect", "system"}, (name, calls)


# ================================================================ untrusted text

def test_hostile_text_from_github_is_capped_and_plain(tw):
    seed(tw.gh)
    evil_title = "Fix\x07 <img src=x onerror=alert(1)>‮​ " + "A" * 10240
    tw.gh.add_issue(REPO, 11, evil_title)
    tw.gh.assign(f"{REPO}#11", "bob")
    tw.gh.world["issues"][f"{REPO}#11"]["labels"] = ["l\x1b[31mabel" + "z" * 500]
    tw.gh.pull(REPO, 12, "<script>x</script>" + "B" * 10240, user="bob", head_ref="evil\x00/<b>branch‮" + "c" * 400)
    tw.gh.tip(REPO, "../../etc/passwd; rm -rf /", author="bob")
    tw.gh.tip(REPO, "x" * 10240, author="bob")
    tw.gh.world["tips"][REPO].append({"name": "ok-branch", "author": "evil\nlogin <b>", "at": iso(tw.gh.world["now"] - 60)})
    tw.gh.world["issues"][f"{REPO}#11"]["assignees"] = ["bob", "bad login\x00<script>", "x" * 500]
    tw.share()
    tw.settle()
    blk = tw.block()
    dumped = json.dumps(blk)
    for bad in ("\x07", "‮", "​", "\x1b", "\x00", "\n"):
        assert bad not in dumped.replace("\\n", "").replace("\\u202e", "<BIDI>") or bad in ("\n",), repr(bad)
    assert "\\u202e" not in dumped and "\\u200b" not in dumped and "\\u0007" not in dumped and "\\u001b" not in dumped and "\\u0000" not in dumped
    bob = person(blk, "bob")
    titles = [i["title"] for i in bob["issues"]] + [p["title"] for p in bob["prs"]]
    assert all(len(t) <= 200 for t in titles) and any(t.startswith("Fix <img") for t in titles), "kept as text: the angle brackets are data, not markup, and the page uses textContent"
    assert all(len(b["name"]) <= 100 for b in bob["branches"]) and all(len(x) <= 50 for i in bob["issues"] for x in i["labels"])
    for p in blk["people"]:
        assert p["login"] is None or team.LOGIN_RE.fullmatch(p["login"]), p["login"]
    assert not [p for p in blk["people"] if p["login"] and ("script" in p["login"] or "login" in p["login"] and "evil" in p["login"])]
    urls = [b["url"] for p in blk["people"] for b in p["branches"] if b["url"]]
    assert all(u.startswith("https://github.com/acme/shop/tree/") and ".." not in u and " " not in u and ";" not in u for u in urls)
    assert not [b for p in blk["people"] for b in p["branches"] if "passwd" in b["name"] and b["url"]], "a branch name that is not a plain ref gets no link"
    assert len(dumped) < 60_000


# ================================================================ forks, renames and the canonical key

def test_a_fork_checkout_reads_the_parent_and_its_rows_carry_the_parents_key(tw):
    seed(tw.gh)
    tw.origins["api"] = "git@github.com:bob/shop.git"                    # this board's checkout is Bob's own fork
    node_state.slug_cache_clear()
    tw.main._invalidate_scan()
    tw.gh.repo("bob/shop", fork=True, parent=REPO, private=True)
    tw.db.team_share_set("github.com/bob/shop", True)
    before = tw.poller.entries()
    assert {e["key"] for e in before} == {"github.com/bob/shop", "github.com/acme/tools"}, "before GitHub has said anything the fork is its own key"
    tw.settle()
    assert team.hinted("github.com/bob/shop") == KEY
    blk = tw.block()
    assert blk["repo_slug"] == KEY and blk["slug"] == REPO and person(blk, "bob") is not None
    assert "bob/shop/issues" not in " ".join(tw.gh.paths()), "the fork's own lists were not read once its parent was known"
    assert tw.db.kv_get("team:meta:github.com/bob/shop")["value"] == {"canonical": REPO}
    tw.sessions["shop--api--s1"] = {"created": 1, "attached": 0, "command": "claude", "path": "/x", "pane_id": "%1", "launcher": "user", "cmd": "claude", "state": "working",
                                    "state_at": "2026-10-10T10:00:00+00:00", "last_event": "Stop", "stats": {}, "needs_attention": False, "agent": "claude", "task": None}
    tw.main._invalidate_scan()
    row = tw.c.get("/api/node/state", headers=ID).json()
    assert {r["repo_slug"] for r in row["sessions"]} == {KEY}


def test_a_renamed_repository_resolves_to_its_new_name_and_keeps_the_switch(tw):
    seed(tw.gh, "acme/store")
    tw.gh.repo(REPO, full_name="acme/store")                                # GitHub redirects the old name; the record says the new one
    tw.share()
    tw.settle()
    blk = tw.block("acme/store")
    assert blk["repo_slug"] == "github.com/acme/store" and blk["state"] == "ok" and blk["shared"] is True
    assert team.hinted(KEY) == "github.com/acme/store"


def test_hints_survive_a_restart_but_only_for_shared_repositories(tw):
    seed(tw.gh, "acme/store")
    tw.gh.repo(REPO, full_name="acme/store")
    tw.share()
    tw.settle()
    team.clear_hints()
    team_github._spelled.clear()
    p2 = team_github.Poller(tw.db, tw.main._team_projects, mono=tw.clock.mono, wall=tw.clock.wall, reader=github.Reader(mono=tw.clock.mono), pause=tw.clock.advance)
    p2.load_hints()
    assert team.hinted(KEY) == "github.com/acme/store" and [e["slug"] for e in p2.entries() if e["shared"]] == ["acme/store"]
    tw.db.team_share_set(KEY, False)
    team.clear_hints()
    p2.load_hints()
    assert team.hinted(KEY) == KEY, "an unshared repository keeps no hint"


# ================================================================ the budget

def test_the_budget_counts_a_rolling_hour_and_says_when_there_is_room_again():
    b = team_github.Budget(per_hour=10, window=3600)
    assert b.room(0, 10) and not b.room(0, 11)
    for t in (0, 100, 200, 300, 400):
        b.add(t, 2)
    assert b.spent(500) == 10 and not b.room(500, 1)
    assert b.wait(500, 2) == pytest.approx(3100) and b.wait(500, 4) == pytest.approx(3200)
    assert b.room(3601, 2) and b.spent(3601) == 8
    assert b.wait(3601, 1) == 0.0


def test_a_call_that_would_overspend_waits_and_the_refresh_button_skips_only_the_budget(tw, monkeypatch):
    seed(tw.gh)
    tw.share()
    monkeypatch.setattr(tw.poller, "budget", team_github.Budget(per_hour=3))
    tw.run_for(30)
    assert tw.gh.points() == 0, "the first cycle needs more than the budget: nothing was asked, the repository waits"
    assert tw.block()["state"] == "loading"
    tw.poller.wake(force=True)
    tw.run_for(30)
    assert tw.block()["state"] == "ok" and tw.gh.points() >= 4


def budget_run(tw, n_repos, hours=1.0):
    """Share `n_repos` repositories (each with two people, a draft pull request and two branches), run `hours` of virtual time and return the points spent."""
    from app import projects
    slugs = []
    for i in range(n_repos):
        proj, repo = f"p{i}", f"r{i}"
        (tw.projects_dir / proj / repo / ".git").mkdir(parents=True, exist_ok=True)
        tw.origins[repo] = f"git@github.com:acme/repo{i}.git"
        slugs.append(f"acme/repo{i}")
    node_state.slug_cache_clear()
    tw.main._invalidate_scan()
    for s in slugs:
        seed(tw.gh, s)
        tw.share("github.com/" + s)
    tw.gh.clear_calls()
    tw.run_for(3600 * hours)
    return slugs


@pytest.mark.parametrize("n", [1, 5, 20])
def test_the_team_poll_stays_inside_its_hourly_budget_for_1_5_and_20_shared_repos(tw, n, capsys):
    slugs = budget_run(tw, n)
    pts = tw.gh.points()
    gql = len(tw.gh.graph_calls())
    rest = len(tw.gh.paths()) - gql
    ok_status = sum(1 for c in tw.gh.calls() if c.get("status") == 200)
    free_304 = sum(1 for c in tw.gh.calls() if c.get("status") == 304)
    cycles = sum(1 for p in tw.gh.paths() if "/pulls?" in p) / n
    with capsys.disabled():
        print(f"\nPOINTS_PER_HOUR repos={n} points={pts} rest_calls={rest} graphql_calls={gql} (304s among the rest: {free_304}; points if a 304 is free: {pts - free_304}; "
              f"REST reads per repository: {cycles:.1f} an hour, one every {3600 / cycles:.0f} s)")
    assert pts <= team_github.BUDGET_PER_HOUR + 8, "the whole team poll, however many repositories, stays at its budget"
    assert pts <= 5000 * 0.13
    read = [r for r in tw.body()["repos"] if r["shared"]]
    assert len(read) == n and all(r["state"] == "ok" and r["read_at"] for r in read), "every shared repository was read at least once within the hour"
    if n == 1:
        assert pts < 120, "one repository costs well under a tenth of the login's hour"


def test_a_repo_that_changed_brings_its_graphql_read_forward_but_not_closer_than_the_minimum(tw):
    seed(tw.gh)
    tw.share()
    tw.settle()
    first = len(tw.gh.graph_calls())
    tw.gh.pull(REPO, 9, "Changed", user="bob", head_ref="n")
    tw.poller.sched[KEY]["rest"] = 0.0
    tw.run_for(20)
    assert len(tw.gh.graph_calls()) == first, "just read: the minimum gap holds"
    tw.run_for(team_github.GQL_MIN_S + team_github.REST_S + 5)
    assert len(tw.gh.graph_calls()) == first + 1 and team_github.GQL_MIN_S < team_github.GQL_S


def test_the_team_poll_stays_away_while_the_epic_reader_is_told_to_wait(tw):
    seed(tw.gh)
    tw.share()
    tw.gh.fail("/rate_limit", 403, headers={"retry-after": "300"}, body={"message": "secondary rate limit"})
    with pytest.raises(github.GhReadError):
        github.reader.get(github.RATE_PATH, 0.0)
    assert github.reader.wait_left() > 0
    tw.run_for(60)
    assert tw.gh.paths() == [p for p in tw.gh.paths() if p == github.RATE_PATH]


# ================================================================ the identity and the doctor

def settle_identity():
    t = team_github._ident_thread
    if t is not None:
        t.join(10)
        assert not t.is_alive()


def test_the_node_card_names_the_login_and_the_numeric_id(tw):
    assert tw.c.get("/api/node", headers=ID).json()["owner_login"] is None, "a cold cache: the card does not wait for gh"
    settle_identity()
    card = tw.c.get("/api/node", headers=ID).json()
    uid = gh_id("alice", tw.gh)
    assert card["owner_login"] == "alice" and card["owner_id"] == uid
    st = tw.c.get("/api/state", headers=ID).json()
    assert {"owner_login", "owner_id"} <= set(st["node"]), "the card inside /api/state has the two fields too (filled from the cache; this fake seam does not fill it)"


def gh_id(login, gh):
    from tests import gh_server
    return gh_server.uid(gh.world, login)


def test_the_card_login_is_null_when_unknown_and_the_page_poll_never_asks_gh(tmp_path, monkeypatch, lite_client):
    gh = FakeGitHub(tmp_path).install(monkeypatch)
    from app import prpoll
    prpoll._gh_login = None
    st = lite_client.get("/api/state", headers=ID).json()
    assert st["node"]["owner_login"] is None and st["node"]["owner_id"] is None and gh.calls() == [], "the 2 s page poll reads only what is cached"
    cold = lite_client.get("/api/node", headers=ID).json()
    assert cold["owner_login"] is None and cold["owner_id"] is None, "the card never waits on gh: the probe runs in the background"
    settle_identity()
    card = lite_client.get("/api/node", headers=ID).json()
    assert card["owner_login"] == "alice" and card["owner_id"] == gh_id("alice", gh)
    assert [c["argv"] for c in gh.calls()] == [["api", "user"]]
    st = lite_client.get("/api/state", headers=ID).json()
    assert st["node"]["owner_login"] == "alice"
    lite_client.get("/api/node", headers=ID)
    assert len(gh.calls()) == 1, "one probe per ten minutes"


def test_a_login_gh_sends_that_is_not_a_login_is_not_put_on_the_card(tmp_path, monkeypatch, lite_client):
    gh = FakeGitHub(tmp_path).install(monkeypatch)
    from app import prpoll
    prpoll._gh_login = None
    gh.set(login="evil login <b>")
    lite_client.get("/api/node", headers=ID)
    settle_identity()
    assert lite_client.get("/api/node", headers=ID).json()["owner_login"] is None


def test_the_doctor_rows_name_the_login_and_tell_sso_from_no_access(tw, monkeypatch):
    from app import doctor
    doctor._probes.pop("gh-login", None)
    doctor._probes.pop("team-gh", None)
    out = doctor._c_team_gh(tw.db)
    assert out.status == "skip" and "no repository is shared" in out.detail and "repo scope" in out.detail and "Issues, Pull requests and Contents" in out.detail
    assert tw.gh.calls() == [] and "team-gh" not in doctor._probes, "nothing shared: no gh call, no probe thread"
    seed(tw.gh)
    tw.share()
    monkeypatch.setattr(doctor, "_projects_source", tw.main._team_projects)          # restored after the test: no later test sees this board's scan
    out = doctor._c_team_gh(tw.db)
    assert out.status == "pass" and "1 of 1" in out.detail
    doctor._probes.pop("team-gh", None)
    tw.gh.world["sso"] = [REPO]
    sso = doctor._c_team_gh(tw.db)
    assert sso.status == "warn" and "authorise this token for the organization" in sso.detail and "not a missing permission" in sso.detail
    doctor._probes.pop("team-gh", None)
    tw.gh.world["sso"], tw.gh.world["forbidden"] = [], [REPO]
    denied = doctor._c_team_gh(tw.db)
    assert denied.status == "warn" and denied.detail.startswith("no access to acme/shop") and "authorise this token" not in denied.detail
    doctor._probes.pop("team-gh", None)
    tw.gh.world["forbidden"], tw.gh.world["hidden"] = [], [REPO]
    assert doctor._c_team_gh(tw.db).detail.startswith("no access to acme/shop")
    doctor._probes.pop("gh-login", None)
    me = doctor._c_gh_login(tw.db)
    assert me.status in ("pass", "skip", "warn")
    by = {c[0]: c for c in doctor.CHECKS}
    assert by["team-gh"][1] == "box" and by["team-gh"][2] == "Team GitHub access" and by["gh-login"][2] == "GitHub login of this board"


def test_the_login_doctor_row_says_which_account_and_warns_about_a_bot(monkeypatch):
    from app import doctor
    ok = doctor._login_outcome({"state": "ok", "login": "alice", "id": 5})
    assert ok.status == "pass" and "alice" in ok.detail and "user id 5" in ok.detail
    bot = doctor._login_outcome({"state": "ok", "login": "ci-helper[bot]", "id": 6})
    assert bot.status == "warn" and "bot" in bot.detail
    assert doctor._login_outcome({"state": "no_login"}).status == "warn" and doctor._login_outcome({"state": "no_gh"}).status == "skip"
    for text in (ok.detail, bot.detail, doctor.TEAM_MIN):
        assert chr(0x2014) not in text, "plain words, no em-dashes"


# ================================================================ review fixes: partial cycles, clocks, the card, forged claims, budget, refresh, shared back-off

def test_a_cycle_that_read_a_new_issue_list_and_then_failed_still_shows_it_and_a_later_304_keeps_it(tw):
    seed(tw.gh)
    tw.share()
    tw.settle()
    tw.gh.add_issue(REPO, 9, "New work")
    tw.gh.assign(f"{REPO}#9", "bob")
    tw.gh.fail("/pulls", 500, times=1)
    tw.poller.sched[KEY]["rest"] = 0.0
    tw.run_for(30)
    blk = tw.block()
    assert blk["state"] == "error" and 9 in [i["number"] for i in person(blk, "bob")["issues"]], "the new issue list was read, so it is shown, as old data, though the pull list failed"
    tw.run_for(team_github.SOFT_RETRY_S + 60)
    blk = tw.block()
    assert blk["state"] == "ok" and 9 in [i["number"] for i in person(blk, "bob")["issues"]], "the issue list answers 304 now: the new list must not be lost"


def test_the_rest_part_is_shown_on_the_first_read_when_the_graphql_read_keeps_failing_and_says_which_part_is_missing(tw, monkeypatch):
    seed(tw.gh)
    tw.share()
    real = github.graphql
    monkeypatch.setattr(github, "graphql", lambda o, r: (_ for _ in ()).throw(github.GhReadError("unavailable")))
    tw.run_for(30)
    blk = tw.block()
    assert blk["state"] == "ok" and blk["read_at"] and person(blk, "bob")["issues"], "not 'loading': the issues and pull requests were read"
    assert blk["graph_failed"] is True and blk["graph_pending"] is True and person(blk, "bob")["prs"], "the branch links are marked as not read"
    monkeypatch.setattr(github, "graphql", real)
    tw.run_for(team_github.SOFT_RETRY_S + 60)
    blk = tw.block()
    assert blk["graph_failed"] is False and blk["graph_pending"] is False and blk["state"] == "ok"


def fake_gh_replies(monkeypatch, replies):
    """Make the next gh runs answer from `replies` (each: (exit code, stdout, stderr)); the last one repeats."""
    ran = []

    def run(args, **kw):
        ran.append(args)
        rc, out, err = replies[min(len(ran), len(replies)) - 1]
        return subprocess.CompletedProcess(args, rc, out, err)
    monkeypatch.setattr(github.subprocess, "run", run)
    monkeypatch.setattr(github.shutil, "which", lambda name: "/fake/gh")
    monkeypatch.setattr(github, "api", REAL_API)
    return ran


def test_a_claim_renewed_long_ago_is_not_live_after_many_304s(monkeypatch):
    import email.utils
    from tests.fake_github import claim_body
    t = {"now": float(START)}
    monkeypatch.setattr(github, "_date_now", lambda: email.utils.formatdate(t["now"], usegmt=True))
    comment = {"id": 1, "body": claim_body(alias="node-b", status="running", lease="30m"), "user": {"id": 5, "login": "bob", "type": "User"}, "author_association": "COLLABORATOR",
               "created_at": iso(START), "updated_at": iso(START)}
    head = b'HTTP/2.0 200 OK\r\nEtag: W/"abc"\r\nDate: ' + email.utils.formatdate(START + 60, usegmt=True).encode() + b"\r\n\r\n"
    fake_gh_replies(monkeypatch, [(0, head + json.dumps([comment]).encode(), b""), (1, b"", b"gh: HTTP 304\n")])
    reader = github.Reader(mono=lambda: t["now"])
    path = github.comments_path("acme", "shop", 5, 1, 100)
    rep = reader.get(path, 0.0)
    assert rep.source == "network" and team_github.parse_claims(rep.data, 5, rep.server_now)[0]["live"] is True
    for _ in range(12):
        t["now"] += 600
        rep = reader.get(path, 0.0)
        assert rep.source == "revalidated"
    assert rep.server_now == START + 7200, "a 304 moves the reader's clock on"
    assert team_github.parse_claims(rep.data, 5, rep.server_now)[0]["live"] is False, "two hours after the last renewal a 30 minute lease is over"


def test_the_card_never_waits_on_gh_and_ten_cold_calls_start_one_probe(lite_client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    release = threading.Event()
    asked = []

    def hanging():
        asked.append(1)
        release.wait(10)                                  # a gh that does not answer (bounded, so a failing test cannot hang)
        return None, None
    monkeypatch.setattr(team_github, "identity", hanging)
    try:
        first = lite_client.get("/api/node", headers=ID)
        assert first.status_code == 200 and first.json()["owner_login"] is None and not release.is_set(), "answered at once, while the probe is still hanging"
        with ThreadPoolExecutor(10) as pool:
            replies = list(pool.map(lambda _: lite_client.get("/api/node", headers=ID).json()["owner_login"], range(10)))
        assert replies == [None] * 10 and not release.is_set()
        assert len(asked) == 1, "one gh for the eleven cold calls"
    finally:
        release.set()
        settle_identity()


def test_a_repository_switched_off_while_github_was_asked_gets_no_hint_and_no_row(tw):
    seed(tw.gh)
    tw.share()
    entry = next(e for e in tw.poller.entries() if e["key"] == KEY)
    tw.poller._retarget(entry, "acme/renamed")
    assert tw.db.kv_get(team_github.KV_META + KEY) is not None and "github.com/acme/renamed" in tw.poller.sched, "still shared: the move is recorded"
    team.clear_hints()
    tw.db.kv_del(team_github.KV_META + KEY)
    tw.poller.sched.pop("github.com/acme/renamed")
    tw.db.team_share_set(KEY, False)
    tw.poller._retarget(entry, "acme/renamed")
    assert tw.db.kv_get(team_github.KV_META + KEY) is None and team.hints() == {} and "github.com/acme/renamed" not in tw.poller.sched


def test_a_graphql_read_that_fails_is_still_charged_to_the_budget(tw, monkeypatch):
    seed(tw.gh)
    tw.share()
    monkeypatch.setattr(github, "graphql", lambda o, r: (_ for _ in ()).throw(github.GhReadError("unavailable")))
    tw.run_for(30)
    assert tw.block()["graph_failed"] is True
    assert tw.poller.budget.spent(tw.clock.mono()) == tw.gh.points() + team_github.GQL_COST_GUESS, "the REST calls and the reserved cost of the failed query"


def test_a_forced_read_ends_after_one_round_even_when_the_repository_is_refused(tw, monkeypatch):
    seed(tw.gh)
    tw.share()
    tw.settle()
    tw.gh.world["sso"] = [REPO]
    monkeypatch.setattr(tw.poller, "budget", team_github.Budget(per_hour=1))
    tw.poller.wake(force=True)
    assert tw.poller.force is True
    tw.run_for(30)
    assert tw.block()["state"] == "sso" and tw.poller.force is False, "no success happened, and the cap is back on"
    tw.gh.world["sso"] = []
    tw.poller.wake()
    tw.run_for(30)
    assert tw.block()["state"] == "sso", "refused again by the cap alone: a plain wake does not skip the budget"


def test_the_refresh_route_answers_once_a_minute(tw, monkeypatch):
    seed(tw.gh)
    tw.share()
    monkeypatch.setattr(tw.poller, "ensure_running", lambda: True)
    first = tw.c.post("/api/team/refresh", headers=H)
    assert first.status_code == 200 and first.json() == {"queued": True} and tw.poller.force is True
    tw.poller.force = False
    again = tw.c.post("/api/team/refresh", headers=H)
    assert again.status_code == 429 and 1 <= int(again.headers["retry-after"]) <= 61 and tw.poller.force is False, "refused: nothing was queued"
    assert "Try again in" in again.json()["error"] and chr(0x2014) not in again.json()["error"]
    tw.clock.advance(61)
    assert tw.c.post("/api/team/refresh", headers=H).status_code == 200


@pytest.mark.parametrize("assoc,counts", [("OWNER", True), ("MEMBER", True), ("COLLABORATOR", True), ("CONTRIBUTOR", False), ("FIRST_TIME_CONTRIBUTOR", False), ("NONE", False), (None, False), ("owner", False)])
def test_a_claim_comment_counts_only_from_an_owner_member_or_collaborator(assoc, counts):
    from tests.fake_github import claim_body
    c = {"id": 1, "body": claim_body(status="running"), "user": {"id": 7, "login": "mallory", "type": "User"}, "updated_at": iso(START)}
    if assoc is not None:
        c["author_association"] = assoc
    got = team_github.parse_claims([c], 5, START + 60)
    assert bool(got) is counts and (not got or got[0]["live"] is True)


def test_the_team_poll_shares_its_back_off_with_the_epic_reader(tw):
    seed(tw.gh)
    tw.share()
    tw.gh.fail("/pulls", 403, headers={"retry-after": "300"}, body={"message": "You have exceeded a secondary rate limit"})
    tw.run_for(10)
    assert tw.block()["state"] == "rate_limited" and github.reader.wait_left() > 0, "the epic reader is told to wait too"
    with pytest.raises(github.GhReadError) as e:
        github.reader.get(github.RATE_PATH, 0.0)
    assert e.value.reason == "rate_limited"


def test_a_graphql_rate_limit_is_shared_with_the_epic_reader_too(tw):
    seed(tw.gh)
    tw.share()
    tw.gh.world["gql_rate"] = {"limit": 5000, "remaining": 0, "reset": START + 3600}
    tw.run_for(30)
    assert tw.block()["state"] == "rate_limited" and github.reader.wait_left() > 0
