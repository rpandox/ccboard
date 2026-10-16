"""Team P1, issue #197: the team model (app/team.py), the canonical repository key on the node state rows (app/node_state.py), the identity of the board, and the team routes.

app/team.py takes plain data and has no network: these tests hand it hand-made reads, including hostile ones, and look at the body of GET /api/team/state. The reader that makes the
reads is tested in tests/test_team_github.py against the fake GitHub; the pinned route table and the refusal of tokens are in tests/test_security_surface.py.
Temp dirs and fakes only: no gh, no git process, no network, no real home.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import node_state, team, team_github
from tests.test_team_github import (H, ID, KEY, REPO, Clock, person, seed, tw)  # noqa: F401 (tw is a fixture)

NOW = 1_800_000_000.0
T = "2026-10-10T09:00:00Z"
RECENT = "2027-01-14T10:00:00Z"            # a day before NOW


def read(**kw):
    base = {"slug": REPO, "status": "ok", "read_at": NOW - 30, "gql_at": NOW - 30, "private": True, "issues": [], "pulls": [], "linked": {}, "tips": [], "claims": [],
            "truncated": {"issues": False, "pulls": False, "tips": False}, "default_branch": "main"}
    base.update(kw)
    return base


def entry(**kw):
    return {"key": KEY, "slug": REPO, "local": [{"project": "shop", "repo": "api"}], "shared": True, **kw}


def block(rd, me=("alice", 1), **kw):
    return team.repo_state(entry(**kw), rd, me[0], me[1], NOW)


def issue(n, who, **kw):
    return {"number": n, "title": f"Issue {n}", "labels": [], "assignees": [who] if isinstance(who, dict) else list(who), "updated_at": RECENT, **kw}


BOB = {"id": 2, "login": "bob"}
ALICE = {"id": 1, "login": "alice"}
CAROL = {"id": 3, "login": "carol"}


# ================================================================ the repository key

class Probe:
    def __init__(self, monkeypatch, origin=None, upstream=None):
        self.origin, self.upstream = origin, upstream
        monkeypatch.setattr(node_state, "_origin_url", lambda path: self.origin)
        monkeypatch.setattr(node_state, "_upstream_url", lambda path: self.upstream)
        node_state.slug_cache_clear()
        team.clear_hints()

    def key(self, name="x"):
        node_state.slug_cache_clear()
        return node_state.repo_key(Path("/nowhere") / name)


@pytest.fixture
def probe(monkeypatch):
    p = Probe(monkeypatch)
    yield p
    team.clear_hints()
    node_state.slug_cache_clear()


def test_a_fork_origin_with_an_upstream_gives_the_parents_key(probe):
    probe.origin, probe.upstream = "git@github.com:bob/shop.git", "https://github.com/Acme/Shop.git"
    assert probe.key() == "github.com/acme/shop"


def test_letter_case_ssh_https_and_a_trailing_dot_git_give_one_key(probe):
    keys = set()
    for url in ("git@github.com:Acme/Shop.git", "https://github.com/acme/shop", "https://github.com/ACME/SHOP.git", "ssh://git@github.com/acme/shop.git", "https://user:pw@github.com/Acme/Shop/",
                "git@github.com:acme/shop"):
        probe.origin, probe.upstream = url, None
        keys.add(probe.key())
    assert keys == {"github.com/acme/shop"}


def test_a_renamed_repository_gives_the_same_key_once_github_said_so(probe):
    probe.origin = "git@github.com:acme/shop.git"
    assert probe.key() == "github.com/acme/shop"
    team.set_hint("github.com/acme/shop", "github.com/acme/store")
    assert probe.key() == "github.com/acme/store"
    probe.origin = "git@github.com:acme/store.git"
    assert probe.key() == "github.com/acme/store", "the board that already uses the new name agrees"
    team.set_hint("github.com/acme/shop", None)
    probe.origin = "git@github.com:acme/shop.git"
    assert probe.key() == "github.com/acme/shop"


@pytest.mark.parametrize("origin,upstream", [("https://gitlab.com/acme/shop.git", None), ("git@example.com:acme/shop.git", None), ("/srv/git/shop", None), (None, None), ("", None),
                                              ("https://github.com.evil.example/acme/shop", None), ("https://gitlab.com/acme/shop.git", "https://gitlab.com/acme/shop.git")])
def test_a_remote_that_is_not_on_github_gives_no_key(probe, origin, upstream):
    probe.origin, probe.upstream = origin, upstream
    assert probe.key() is None


def test_the_key_never_asks_github_and_is_remembered_for_ten_minutes(monkeypatch):
    calls = []
    monkeypatch.setattr(node_state, "_origin_url", lambda path: calls.append(("origin", path.name)) or "git@github.com:Acme/Shop.git")
    monkeypatch.setattr(node_state, "_upstream_url", lambda path: calls.append(("upstream", path.name)) or None)
    node_state.slug_cache_clear()
    for _ in range(3):
        assert node_state.repo_key(Path("/nowhere/a")) == "github.com/acme/shop"
    assert calls == [("origin", "a"), ("upstream", "a")]
    team.clear_hints()


def test_the_key_helpers_check_github_rules():
    assert team.key_of("Acme", "Shop") == "github.com/acme/shop"
    assert team.split_key("github.com/acme/shop") == ("acme", "shop")
    for bad in ("gitlab.com/acme/shop", "github.com/acme", "github.com/acme/..", "github.com/-a/b", "github.com/acme/shop/x", None, 5, ""):
        assert team.split_key(bad) is None, bad


# ================================================================ the rows of /api/node/state

def test_every_session_and_task_row_carries_repo_slug_and_loses_no_key(tw):
    tw.origins["web"] = "https://gitlab.com/acme/web.git"
    tw.sessions["shop--api--s1"] = session_row()
    tw.sessions["shop--web--s2"] = session_row()
    tw.sessions["shop--root--s3"] = session_row()
    tw.sessions["shop--ghost--s4"] = session_row()
    tw.db.task_add(project="shop", repo="api", slug="t1", title="T", prompt="p", branch="b", tmux_name="shop--api--t1", phase="running")
    tw.db.task_add(project="shop", repo="web", slug="t2", title="T2", prompt="p", branch="b", tmux_name="shop--web--t2", phase="running")
    tw.db.task_add(project="shop", repo="nope", slug="t3", title="T3", prompt="p", branch="b", tmux_name="shop--nope--t3", phase="running")
    tw.main._invalidate_scan()
    d = tw.c.get("/api/node/state", headers=ID).json()
    old_sessions = {"tmux", "project", "repo", "session", "agent", "state", "needs_you", "kind", "since", "model"}
    old_tasks = {"id", "title", "phase", "agent", "project", "repo", "branch", "tmux", "issue_ref", "updated_at"}
    assert d["sessions"] and d["tasks"]
    for r in d["sessions"]:
        assert set(r) == old_sessions | {"repo_slug"}
    for r in d["tasks"]:
        assert set(r) == old_tasks | {"repo_slug"}
    by = {r["tmux"]: r["repo_slug"] for r in d["sessions"]}
    assert by == {"shop--api--s1": "github.com/acme/shop", "shop--web--s2": None, "shop--root--s3": None, "shop--ghost--s4": None}, "a non-GitHub repository, the project folder and an orphan have none"
    assert {r["repo"]: r["repo_slug"] for r in d["tasks"]} == {"api": "github.com/acme/shop", "web": None, "nope": None}
    assert d["tasks"][0]["repo_slug"] != d["tasks"][0]["repo"]


def session_row():
    return {"created": 1, "attached": 0, "command": "claude", "path": "/x", "pane_id": "%1", "launcher": "user", "cmd": "claude", "state": "working", "state_at": "2026-10-10T10:00:00+00:00",
            "last_event": "Stop", "stats": {}, "needs_attention": False, "agent": "claude", "task": None}


def test_two_boards_that_spell_one_repository_differently_get_one_key_on_their_rows(tw):
    keys = set()
    for url, up in (("git@github.com:Acme/Shop.git", None), ("https://github.com/bob/shop", "git@github.com:acme/SHOP.git"), ("https://github.com/acme/shop.git", None)):
        tw.origins["api"], tw.upstreams["api"] = url, up
        node_state.slug_cache_clear()
        tw.main._invalidate_scan()
        keys.add(tw.c.get("/api/node/state", headers=ID).json()["projects"][1]["repos"][0]["slug"] and node_state.repo_key(tw.projects_dir / "shop" / "api"))
    assert keys == {"github.com/acme/shop"}


def test_the_body_stays_inside_100_kb_with_the_new_key_on_every_row(tw):
    for i in range(210):
        tw.sessions[f"shop--api--s{i}"] = session_row()
    for i in range(210):
        tw.db.task_add(project="shop", repo="api", slug=f"t{i}", title="T" * 150, prompt="p", branch="b" * 150, tmux_name=f"shop--api--t{i}", phase="running")
    tw.main._invalidate_scan()
    r = tw.c.get("/api/node/state", headers=ID)
    d = r.json()
    assert len(r.content) <= node_state.BODY_MAX and d["truncated"] is True
    assert all(x["repo_slug"] == "github.com/acme/shop" for x in d["sessions"] + d["tasks"])
    assert len(d["sessions"]) <= node_state.SESSIONS_MAX and len(d["tasks"]) <= node_state.TASKS_MAX


def test_tasks_slug_keeps_its_meaning_and_state_needs_no_gh(tw):
    tw.db.task_add(project="shop", repo="api", slug="my-worktree-slug", title="T", prompt="p", branch="b", tmux_name="shop--api--t1", phase="running")
    tw.main._invalidate_scan()
    row = tw.db.tasks()[0]
    assert row["slug"] == "my-worktree-slug"
    tw.c.get("/api/node/state", headers=ID)
    assert tw.gh.calls() == [], "building the rows is local git facts only"


# ================================================================ the model: two people

def test_people_are_grouped_by_numeric_id_and_the_owner_is_you():
    rd = read(issues=[issue(1, BOB), issue(3, ALICE)], pulls=[{"number": 7, "title": "WIP", "draft": True, "author": BOB, "head_ref": "bob/x", "head_repo": REPO, "fork": False,
                                                              "updated_at": RECENT, "closes": [1]}],
              tips=[{"name": "bob/x", "at": RECENT, "author": BOB}, {"name": "alice/y", "at": RECENT, "author": ALICE}], linked={1: [{"name": "bob/x", "at": RECENT, "author": BOB}]})
    b = block(rd)
    assert b["state"] == "ok" and [p["login"] for p in b["people"]] == ["alice", "bob"] and [p["you"] for p in b["people"]] == [True, False]
    bob = person(b, "bob")
    assert bob["issues"][0]["branches"] == ["bob/x"] and bob["prs"][0]["state"] == "draft" and bob["prs"][0]["closes"] == [1]
    assert [(x["name"], x["source"], x["issue"]) for x in bob["branches"]] == [("bob/x", "linked", 1)], "a branch that is linked is not listed a second time as pushed"
    assert b["counts"] == {"people": 2, "teammates": 1, "issues": 2, "branches": 2, "prs": 1, "unattributed_branches": 0, "live": 0}


def test_you_is_decided_by_the_id_not_the_login_and_a_rename_is_still_you():
    rd = read(issues=[issue(3, {"id": 1, "login": "alice-new-name"})])
    b = block(rd, me=("alice", 1))
    assert b["people"][0]["you"] is True
    b = block(rd, me=("alice-new-name", 999))
    assert b["people"][0]["you"] is False, "same login, another id: not you"
    rd2 = read(issues=[issue(3, {"id": None, "login": "Alice"})])
    assert block(rd2, me=("alice", None))["people"][0]["you"] is True, "without ids the login decides, ignoring case"
    assert block(rd2, me=(None, None))["people"][0]["you"] is False and team.build([entry()], {KEY: rd2}, None, None, NOW)["me"] is None


def test_one_person_with_two_logins_over_time_is_one_person():
    rd = read(issues=[issue(1, {"id": 2, "login": "bob"})], pulls=[{"number": 8, "title": "x", "draft": False, "author": {"id": 2, "login": "robert"}, "head_ref": "r", "head_repo": REPO,
                                                                    "fork": False, "updated_at": RECENT, "closes": None}])
    b = block(rd)
    assert len(b["people"]) == 1 and b["people"][0]["id"] == 2 and len(b["people"][0]["issues"]) == 1 and len(b["people"][0]["prs"]) == 1


def test_a_linked_branch_with_no_known_author_goes_to_the_issues_assignees():
    rd = read(issues=[issue(1, [BOB, CAROL])], linked={1: [{"name": "fix-1", "at": RECENT, "author": None}]})
    b = block(rd)
    assert [x["name"] for x in person(b, "bob")["branches"]] == ["fix-1"] and [x["name"] for x in person(b, "carol")["branches"]] == ["fix-1"]


def test_pushed_branches_belong_to_the_tip_author_and_machinery_is_left_out():
    old = "2026-09-01T00:00:00Z"
    rd = read(tips=[{"name": "bob/new", "at": RECENT, "author": BOB}, {"name": "bob/old", "at": old, "author": BOB}, {"name": "main", "at": RECENT, "author": ALICE},
                    {"name": "ccboard-backup/box/feature", "at": RECENT, "author": ALICE}, {"name": "anon", "at": RECENT, "author": None}, {"name": "bad-date", "at": "yesterday", "author": BOB}])
    b = block(rd)
    assert [x["name"] for x in person(b, "bob")["branches"]] == ["bob/new"], "an old tip, the default branch and the backup namespace are not work"
    assert b["counts"]["unattributed_branches"] == 1 and person(b, "alice") is None


def test_a_pull_request_from_a_fork_says_where_from_and_links_the_base_repository():
    rd = read(pulls=[{"number": 8, "title": "x", "draft": False, "author": BOB, "head_ref": "feature", "head_repo": "bob/shop", "fork": True, "updated_at": RECENT, "closes": None}])
    pr = block(rd)["people"][0]["prs"][0]
    assert pr["from_fork"] == "bob/shop" and pr["url"] == "https://github.com/acme/shop/pull/8" and pr["closes"] is None and pr["state"] == "open"


def test_a_claim_is_attached_to_the_commenters_issue_and_carries_no_task_or_node_name():
    rd = read(issues=[issue(5, BOB)], claims=[{"issue": 5, "user": BOB, "status": "running", "branch": "bob/five", "live": True, "updated_at": RECENT, "task_ref": "box:42", "alias": "node-a"}])
    it = person(block(rd), "bob")["issues"][0]
    assert it["claim"] == {"status": "running", "branch": "bob/five", "live": True}


def test_the_lists_are_capped_and_the_order_is_you_then_the_latest_activity():
    rd = read(issues=[issue(i, {"id": 100 + i, "login": f"user{i}"}, updated_at=f"2027-01-{(i % 28) + 1:02d}T00:00:00Z") for i in range(1, 80)])
    b = block(rd)
    assert len(b["people"]) == team.PEOPLE_MAX
    many = read(issues=[issue(i, BOB) for i in range(1, 80)], pulls=[{"number": i, "title": "t", "draft": False, "author": BOB, "head_ref": "h", "head_repo": REPO, "fork": False,
                                                                      "updated_at": RECENT, "closes": None} for i in range(1, 80)])
    bob = person(block(many), "bob")
    assert len(bob["issues"]) == team.ITEMS_MAX and len(bob["prs"]) == team.ITEMS_MAX
    order = block(read(issues=[issue(1, BOB, updated_at="2027-01-01T00:00:00Z"), issue(2, CAROL, updated_at="2027-01-10T00:00:00Z"), issue(3, ALICE, updated_at="2026-01-01T00:00:00Z")]))
    assert [p["login"] for p in order["people"]] == ["alice", "carol", "bob"]


# ================================================================ the model: states

def test_off_loading_ok_and_every_failure_have_their_own_state_and_words():
    assert block(None, shared=False)["state"] == "off" and "Nothing is read" in block(None, shared=False)["message"]
    assert block(None)["state"] == "loading" and block({"status": "pending"})["state"] == "loading"
    seen = {}
    for st in ("sso", "no_access", "not_found", "rate_limited", "low_budget", "no_login", "no_gh", "error"):
        b = block(read(status=st, read_at=None, gql_at=None))
        seen[st] = b["message"]
        assert b["state"] == st and b["stale"] is True and b["people"] == [] and b["age_s"] is None and b["message"]
    assert len(set(seen.values())) == len(seen), "no two states share a sentence"
    assert "authorised for the organization" in seen["sso"] and "cannot read" in seen["no_access"] and "authorise" not in seen["no_access"]
    assert block(read(status="rate_limited", wait_s=90))["message"] == "GitHub asked this board to wait 90 s." and block(read(status="rate_limited", wait_s=90))["wait_s"] == 90
    assert block(read(status="made-up"))["state"] == "error"


def test_an_old_read_is_stale_and_keeps_its_people_and_a_good_one_is_not():
    rd = read(issues=[issue(1, BOB)], read_at=NOW - 10)
    assert block(rd)["stale"] is False and block(rd)["age_s"] == 10
    old = read(issues=[issue(1, BOB)], read_at=NOW - team.STALE_S - 5)
    b = block(old)
    assert b["stale"] is True and b["state"] == "ok" and person(b, "bob") is not None and b["age_s"] == team.STALE_S + 5
    failed = read(issues=[issue(1, BOB)], status="sso")
    assert block(failed)["stale"] is True and person(block(failed), "bob") is not None, "the last good data is shown as old data, not dropped"
    assert block(read(gql_at=None))["graph_pending"] is True and block(read())["graph_pending"] is False


def test_no_teammates_is_a_count_not_a_guess():
    b = block(read(issues=[issue(3, ALICE)]))
    assert b["state"] == "ok" and b["counts"]["teammates"] == 0 and b["people"][0]["you"] is True
    b = block(read())
    assert b["state"] == "ok" and b["people"] == [] and b["counts"]["people"] == 0


def test_the_body_has_one_block_per_own_repository_with_shared_ones_first():
    other = entry(key="github.com/acme/tools", slug="acme/tools", shared=False, local=[{"project": "ops", "repo": "tools"}])
    body = team.build([other, entry()], {KEY: read(issues=[issue(1, BOB)])}, "alice", 1, NOW)
    assert body["api"] == 1 and body["enabled"] is True and body["me"] == {"login": "alice", "id": 1}
    assert [r["slug"] for r in body["repos"]] == ["acme/shop", "acme/tools"] and [r["state"] for r in body["repos"]] == ["ok", "off"]
    assert body["repos"][1]["people"] == [] and body["repos"][0]["local"] == [{"project": "shop", "repo": "api"}]
    assert team.build([other], {}, None, None, NOW)["enabled"] is False
    assert set(body) == {"api", "enabled", "me", "repos", "overview", "at"}          # issue #200: overview
    assert set(body["repos"][0]) == {"repo_slug", "slug", "url", "local", "shared", "state", "message", "wait_s", "stale", "read_at", "age_s", "gql_at", "graph_pending", "graph_failed", "private", "people", "counts",
                                     "truncated", "presence", "warnings", "free"}
    assert set(body["repos"][0]["people"][0]) == {"id", "login", "you", "issues", "branches", "prs", "last_active", "presence"}


def test_the_etag_ignores_the_clock_and_the_age_and_changes_with_the_content():
    rd = read(issues=[issue(1, BOB)], read_at=NOW - 10)
    a = team.build([entry()], {KEY: rd}, "alice", 1, NOW)
    b = team.build([entry()], {KEY: dict(rd)}, "alice", 1, NOW + 1)
    assert a["at"] != b["at"] and a["repos"][0]["age_s"] != b["repos"][0]["age_s"] and team.etag(a) == team.etag(b) and team.etag(a).startswith('W/"')
    c = team.build([entry()], {KEY: read(issues=[issue(1, BOB), issue(2, BOB)], read_at=NOW - 10)}, "alice", 1, NOW)
    assert team.etag(c) != team.etag(a)
    d = team.build([entry()], {KEY: dict(rd, read_at=NOW - 5)}, "alice", 1, NOW)
    assert team.etag(d) != team.etag(a), "a fresher read is a change: the page shows its age"


# ================================================================ hostile text

def test_text_from_github_is_capped_stripped_and_never_markup():
    wild = "T\x00\x07itle <script>alert(1)</script>‮​⁦x" + "A" * 10240
    rd = read(issues=[issue(1, {"id": 2, "login": "bob"}, title=wild, labels=["l\x1b[31m" + "z" * 500, "<b>"])], default_branch="main",
              tips=[{"name": "bad\x00‮branch<script>" + "c" * 400, "at": RECENT, "author": BOB}, {"name": "../../etc/passwd", "at": RECENT, "author": BOB},
                    {"name": "has space", "at": RECENT, "author": BOB}, {"name": "a;rm -rf /", "at": RECENT, "author": BOB}],
              pulls=[{"number": 7, "title": wild, "draft": "yes", "author": {"id": 2, "login": "bob\x00<img>"}, "head_ref": "h‮", "head_repo": "bob/shop<x>", "fork": True,
                      "updated_at": "not a date", "closes": ["1", 2, {"x": 1}, True]}])
    b = block(rd)
    dumped = json.dumps(b, ensure_ascii=True)
    for bad in ("\\u0000", "\\u0007", "\\u202e", "\\u200b", "\\u2066", "\\u001b"):
        assert bad not in dumped, bad
    bob = person(b, "bob")
    assert bob is not None and bob["issues"][0]["title"].startswith("Title <script>alert(1)</script>") and len(bob["issues"][0]["title"]) == 200, "kept as text: cut, and the page uses textContent"
    assert all(len(x) <= 50 for x in bob["issues"][0]["labels"])
    assert all(len(x["name"]) <= 100 for x in bob["branches"])
    linked = {x["name"]: x["url"] for x in bob["branches"]}
    assert linked["../../etc/passwd"] is None and linked["has space"] is None and linked["a;rm -rf /"] is None, "a name that is not a plain ref gets no link"
    assert all(u is None or (u.startswith("https://github.com/acme/shop/tree/") and ".." not in u and " " not in u) for u in linked.values())
    pr = bob["prs"][0]
    assert pr["draft"] is False and pr["updated_at"] is None and pr["closes"] == [2] and pr["from_fork"] is None, "a head repository that is not a repository name is dropped"
    assert pr["url"] == "https://github.com/acme/shop/pull/7" and len(pr["title"]) == 200


def test_a_login_that_is_not_a_login_does_not_name_a_person_unless_it_has_an_id():
    rd = read(issues=[issue(1, {"id": None, "login": "bad login"}), issue(2, {"id": None, "login": "<b>x"}), issue(3, {"id": 9, "login": "a\x00b c"}), issue(4, {"id": None, "login": "x" * 500})])
    b = block(rd)
    assert [p["login"] for p in b["people"]] == [None] and b["people"][0]["id"] == 9, "an id with no usable name is kept as a person without a login"
    for name in ("bob", "Bob-1", "a", "ci[bot]", "dependabot[bot]"):
        assert team.login_of(name) == name
    for name in ("", "-a", "a-", "a--b", "a b", "a/b", "a.b", "x" * 40, 5, None, "a\nb", "ａｂ"):
        assert team.login_of(name) is None, repr(name)


def test_urls_are_built_from_validated_parts_never_copied():
    rd = read(issues=[issue(1, BOB, url="https://evil.example/phish", html_url="https://evil.example/x")], pulls=[{"number": 7, "title": "t", "draft": False, "author": BOB, "head_ref": "h",
                                                                                                                   "head_repo": REPO, "fork": False, "updated_at": RECENT, "closes": None,
                                                                                                                   "url": "https://evil.example/pr"}])
    assert "evil" not in json.dumps(block(rd))
    forged = entry(key="github.com/acme/shop", slug="evil.example/x/y")
    assert team.repo_state(forged, read(), "alice", 1, NOW)["url"] == "https://github.com/acme/shop"
    bad_key = entry(key="gitlab.com/acme/shop", slug="acme/shop")
    assert team.repo_state(bad_key, read(), "alice", 1, NOW)["url"] == "https://github.com/acme/shop"


# ================================================================ the routes

def test_the_state_route_has_an_etag_and_a_304_and_names_the_board(tw):
    seed(tw.gh)
    r = tw.c.get("/api/team/state", headers=ID)
    assert r.status_code == 200 and r.headers["etag"].startswith('W/"') and r.headers["cache-control"] == "no-cache"
    d = r.json()
    assert d["enabled"] is False and [x["slug"] for x in d["repos"]] == ["acme/shop", "acme/tools"] and all(x["state"] == "off" for x in d["repos"])
    assert tw.c.get("/api/team/state", headers={**ID, "If-None-Match": r.headers["etag"]}).status_code == 304
    assert tw.c.get("/api/team/state", headers={**ID, "If-None-Match": '"other"'}).status_code == 200
    again = tw.c.get("/api/team/state", headers=ID)
    assert again.headers["etag"] == r.headers["etag"] and tw.gh.calls() == []


def test_switching_a_repository_on_shows_loading_then_the_people(tw):
    seed(tw.gh)
    on = tw.c.put("/api/team/repos/acme/shop", headers=H, json={"shared": True}).json()
    assert on["shared"] is True and on["state"] in ("loading", "ok")
    tw.poller.stop(2.0)
    tw.settle()
    d = tw.c.get("/api/team/state", headers=ID).json()
    shop = next(x for x in d["repos"] if x["slug"] == "acme/shop")
    assert d["enabled"] is True and shop["state"] == "ok" and person(shop, "bob") is not None and d["me"]["login"] == "alice"
    assert next(x for x in d["repos"] if x["slug"] == "acme/tools")["state"] == "off"


def test_the_switch_is_stored_on_the_board_default_off_and_survives_a_new_poller(tw):
    assert tw.db.team_shared() == {}
    tw.c.put("/api/team/repos/acme/shop", headers=H, json={"shared": True})
    tw.poller.stop(2.0)
    assert list(tw.db.team_shared()) == [KEY]
    assert tw.c.put("/api/team/repos/acme/shop", headers=H, json={"shared": True}).status_code == 200 and list(tw.db.team_shared()) == [KEY]
    tw.c.put("/api/team/repos/acme/shop", headers=H, json={"shared": False})
    assert tw.db.team_shared() == {} and tw.db.kv_get("team:share:" + KEY) is None
    assert tw.db.team_share_set(KEY, False) is False, "off when it is already off writes nothing"


def test_nothing_in_the_team_routes_changes_a_session_a_task_or_github(tw):
    seed(tw.gh)
    tid = tw.db.task_add(project="shop", repo="api", slug="t", title="T", prompt="p", branch="b", tmux_name="shop--api--t", phase="running")
    before = tw.db.tasks()
    tw.c.put("/api/team/repos/acme/shop", headers=H, json={"shared": True})
    tw.poller.stop(2.0)
    tw.settle()
    tw.c.get("/api/team/state", headers=ID)
    tw.c.post("/api/team/refresh", headers=H)
    tw.poller.stop(2.0)
    assert tw.db.tasks() == before and tw.gh.writes() == [] and tid
    assert all(c["argv"][0] == "api" for c in tw.gh.calls())


def test_the_public_repository_hygiene_of_the_new_files():
    root = Path(team.__file__).parent.parent
    import re
    dash = chr(0x2014)
    for f in ("app/team.py", "app/team_github.py", "tests/test_team.py", "tests/test_team_github.py"):
        text = (root / f).read_text()
        mails = [m for m in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text) if not m.endswith(("@github.com", "@example.com"))]
        assert not mails, (f, mails)
        assert dash not in text, f"{f}: no em-dash"
        assert "/" + "Users/" not in text and "/" + "home/" not in text, f
