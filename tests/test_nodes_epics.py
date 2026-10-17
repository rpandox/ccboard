"""Nodes epic P12, issue #147: the epic view, server side (app/github.py reader, app/nodes_epics.py, the three /api/epics routes, the doctor row epic-gh).

GitHub is a fake `gh` on a temp PATH (tests/fake_github.py): issues, sub-issues, comments, labels, ETag and 304, paging, rate-limit answers, a missing sub-issues
endpoint, no login, no access, and a stderr that leaks a secret-shaped GH_TOKEN on every error. No test reaches the real gh, GitHub or the network.

Pinned: the reader is GET only and only for the allowed paths (every builder walked, every logged call checked), owner, repository and number are validated before an argv
exists, an ETag is only echoed back when it is a plain ETag, a 304 keeps the cache, the back-off is kept in memory and never looped on, the token never reaches an answer,
a log or an error, GitHub text is capped and cleaned and its URLs are never used, a claim-shaped comment of a stranger is never a claim, pins and pause stay on this board,
and a board with no epic label and no paired node does nothing in the background.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from types import SimpleNamespace

import pytest

from app import github, nodes, nodes_epics
from app.config import Settings, parse_epic_authors, settings
from tests.fake_github import SECRET, START, FakeGitHub, claim_body, iso

ID = {"Tailscale-User-Login": "alice@example.com"}
H = {**ID, "X-CCBoard": "1"}
REPO = "acme/shop"
NODE_B = "n_" + "b" * 16


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(github.reader, "mono", c)
    monkeypatch.setattr(nodes_epics, "mono", c)
    return c


@pytest.fixture
def gh(tmp_path, monkeypatch, clock):
    return FakeGitHub(tmp_path).install(monkeypatch)


def set_repos(main, monkeypatch, slugs) -> None:
    """The GitHub repositories of this board's own projects, without a git repository on disk: a fake path per slug that `node_state.repo_slug` answers; any other
    path (the list tests build real repositories) goes to the real lookup."""
    from app import node_state
    real = getattr(node_state.repo_slug, "real", node_state.repo_slug)
    table = {f"/fake/{s.replace('/', '--')}": s for s in slugs}

    def fake(path):
        return table.get(str(path)) or real(path)
    fake.real = real
    monkeypatch.setattr(node_state, "repo_slug", fake)
    projs = [{"name": "p", "repos": [{"name": s.split("/")[1], "path": p, "state": "ok"} for p, s in table.items()]}]
    monkeypatch.setattr(main, "_cached_projects", lambda: projs)


@pytest.fixture
def board(lite_client, gh, monkeypatch):
    from app import main
    monkeypatch.setattr(nodes, "registry_listener", None)           # a pair saved here must not start the hub's thread
    monkeypatch.setattr(nodes, "display_name", lambda: "node-a")
    main.node_hub = None
    set_repos(main, monkeypatch, ["acme/shop", "acme/api"])                  # this board's own projects: two repositories with a GitHub remote
    yield SimpleNamespace(c=lite_client, db=main.db, main=main, gh=gh, repos=lambda *slugs: set_repos(main, monkeypatch, list(slugs)))
    main.node_hub = None


def get(board, owner="acme", repo="shop", n=147, headers=ID):
    return board.c.get(f"/api/epics/{owner}/{repo}/{n}", headers=headers)


def put(board, body, owner="acme", repo="shop", n=147, headers=H):
    return board.c.put(f"/api/epics/{owner}/{repo}/{n}/settings", json=body, headers=headers)


def world(gh: FakeGitHub, *, claims=True) -> str:
    """The epic acme/shop#147 with six children and one in another repository: closed and done by node-a, running (live claim) on node-a, claimed (live) by node-b,
    failed on node-b, unclaimed, and a stale claim of node-b; acme/api#9 is done by node-b. The server clock is START."""
    gh.add_issue(REPO, 147, "Checkout rework", labels=["ccboard:epic"])
    kids = [(201, "Cart totals", "closed"), (202, "Tax rules", "open"), (203, "Coupon codes", "open"), (204, "Receipts", "open"), (205, "Refund flow", "open"),
            (206, "Gift cards", "open")]
    for n, title, state in kids:
        gh.add_issue(REPO, n, title, state=state, labels=["ccboard:in-progress"] if n in (202, 203, 204, 206) else [])
        gh.link(f"{REPO}#147", f"{REPO}#{n}")
    gh.add_issue("acme/api", 9, "Tax API", state="open")
    gh.link(f"{REPO}#147", "acme/api#9")
    if claims:
        live, old = iso(START - 120), iso(START - 7200)
        gh.comment(f"{REPO}#201", claim_body("node-a", "done"), created_at=iso(START - 9000), updated_at=iso(START - 8000))
        gh.comment(f"{REPO}#202", claim_body("node-a", "running", task="42", branch="worktree-tax"), created_at=iso(START - 900), updated_at=live)
        gh.comment(f"{REPO}#203", claim_body("node-b", "claimed"), created_at=iso(START - 900), updated_at=live)
        gh.comment(f"{REPO}#204", claim_body("node-b", "failed"), created_at=iso(START - 900), updated_at=live)
        gh.comment(f"{REPO}#206", claim_body("node-b", "running"), created_at=iso(START - 9000), updated_at=old)
        gh.comment("acme/api#9", claim_body("node-b", "done"), created_at=iso(START - 900), updated_at=live)
    return f"{REPO}#147"


def child(d, n, repo=REPO):
    return next(c for c in d["children"] if c["number"] == n and c["repo"] == repo)


def add_node(board, name="node-b", n=2, node_id=NODE_B):
    return nodes.put_peer({"direction": "out", "url": f"https://100.64.0.{n}", "name": name, "scopes": ["read", "tasks"], "node_id": node_id}, db=board.db)


# ================================================================ the reader: GET only, allowed paths only, validated parts

OWNERS_BAD = ["", "-a", "a-", "a--b", "a b", "a/b", "../x", "a" * 40, "a\nb", "a;b", "$(x)", "é", "a.b", "--help", " a", "a "]
REPOS_BAD = ["", ".", "..", "-r", ".hidden", "a" * 101, "a/b", "a b", "a\nb", "a?b", "a#b", "r;x", "--output", "é", "a\x00b"]
NUMS_BAD = ["", "0", "01", "-1", "1.5", "abc", "1" * 10, "1 2", "1\n", "1/../2", "٣", True, None, 0, -3, 10 ** 9, 1.0]


def allowed_samples():
    return [github.issue_path("acme", "shop", 5), github.sub_issues_path("acme", "shop", 5), github.sub_issues_path("acme", "shop", "5", 3, 50),
            github.comments_path("acme", "shop", 5), github.comments_path("acme", "shop", 5, 2), github.labeled_path("acme", "shop", "ccboard:epic"),
            github.labeled_path("acme", "shop", "epic:147", "all", 2), github.labeled_path("a-b", "r.s_t-u", "ccboard:epic", "closed"), github.RATE_PATH]


def test_every_builder_makes_an_allowed_get_path_and_argv_has_no_other_method_or_option():
    for path in allowed_samples():
        assert github.allowed_path(path), path
        a = github.argv(path, 'W/"abc123"')
        assert a[:5] == ["gh", "api", "--include", "--method", "GET"] and a[-1] == path
        flags = [x for x in a if x.startswith("-")]
        assert set(flags) <= {"--include", "--method", "-H"}, flags              # no -f, -F, --input, --paginate, --jq, --hostname, -X
        assert a.count("--method") == 1 and a[a.index("--method") + 1] == "GET"
        assert not any(x in a for x in ("-X", "-f", "-F", "--field", "--raw-field", "--input", "--paginate", "--jq", "--template", "--slurp"))
        assert [h for h in a if h.startswith("If-None-Match")] == ['If-None-Match: W/"abc123"']


@pytest.mark.parametrize("owner", OWNERS_BAD)
def test_a_hostile_owner_never_reaches_an_argv(owner):
    for build in (lambda: github.issue_path(owner, "shop", 5), lambda: github.sub_issues_path(owner, "shop", 5), lambda: github.comments_path(owner, "shop", 5),
                  lambda: github.labeled_path(owner, "shop", "ccboard:epic")):
        with pytest.raises(github.GhReadError) as e:
            build()
        assert e.value.reason == "bad_request"
    assert not github.valid_slug(owner, "shop")


@pytest.mark.parametrize("repo", REPOS_BAD)
def test_a_hostile_repository_never_reaches_an_argv(repo):
    for build in (lambda: github.issue_path("acme", repo, 5), lambda: github.sub_issues_path("acme", repo, 5), lambda: github.comments_path("acme", repo, 5),
                  lambda: github.labeled_path("acme", repo, "ccboard:epic")):
        with pytest.raises(github.GhReadError):
            build()
    assert not github.valid_slug("acme", repo)


@pytest.mark.parametrize("num", NUMS_BAD)
def test_a_hostile_issue_number_never_reaches_an_argv(num):
    for build in (lambda: github.issue_path("acme", "shop", num), lambda: github.sub_issues_path("acme", "shop", num), lambda: github.comments_path("acme", "shop", num)):
        with pytest.raises(github.GhReadError):
            build()
    assert not github.valid_number(num)


def test_hostile_labels_pages_and_states_are_refused():
    for label in ("", "a b", "a&state=all", "x/../y", "x\n", "-x", "é", "a" * 51, "a=b", None, 5):
        with pytest.raises(github.GhReadError):
            github.labeled_path("acme", "shop", label)
    for state in ("", "ALL", "open&per_page=1000", "x"):
        with pytest.raises(github.GhReadError):
            github.labeled_path("acme", "shop", "ccboard:epic", state)
    for page, per in ((0, 100), (1, 0), (1, 101), (1000, 100), (True, 100), (1, True), ("1", 100), (1, "100")):
        with pytest.raises(github.GhReadError):
            github.comments_path("acme", "shop", 5, page, per)


@pytest.mark.parametrize("path", [
    "/user", "/repos/acme/shop/", "/repos/acme/shop/issues", "/repos/acme/shop/issues/5/labels", "/repos/acme/shop/issues/5/comments", "/repos/acme/shop/pulls/5",
    "/repos/acme/../issues/5", "/repos/acme/shop/issues/5/../../x", "/repos/acme/shop/issues/05", "/repos/-a/shop/issues/5", "/repos/acme/.git/issues/5",
    "https://api.github.com/repos/acme/shop/issues/5", "repos/acme/shop/issues/5", "/repos/acme/shop/issues/5?per_page=1&page=1&x=1", "--hostname", "-X",
    "/repos/acme/shop/issues/5/sub_issues?per_page=100&page=1\n/user", "/graphql", "/rate_limit?x=1", "/rate_limit/", "/repos/acme/shop/issues?labels=a b&state=open&per_page=1&page=1",
    "/repos/acme/shop/issues?labels=x&state=all&per_page=100&page=1&--input=/etc/passwd", "", None, 5])
def test_api_refuses_every_path_outside_the_allowed_shapes_before_it_runs_anything(path, gh):
    assert not github.allowed_path(path)
    with pytest.raises(github.GhReadError) as e:
        github.api(path)
    assert e.value.reason == "bad_request"
    assert gh.calls() == [], "refused before a process was started"


def test_an_etag_that_is_not_a_plain_etag_is_never_sent():
    path = github.issue_path("acme", "shop", 5)
    for bad in ('W/"a"\r\nX-Evil: 1', '"a"\nX: 1', 'a', '"a b"', '"a"; x', '"' + "a" * 201 + '"', "", None, 5, '""', "*", '"a"\x00'):
        assert not any(x.startswith("If-None-Match") for x in github.argv(path, bad)), repr(bad)
    for ok in ('"abc"', 'W/"abc"', 'W/"a1b2c3d4e5f60718"'):
        assert f"If-None-Match: {ok}" in github.argv(path, ok)


def test_the_reader_sends_get_with_if_none_match_after_a_200_and_keeps_the_cache_on_304(gh):
    gh.add_issue(REPO, 5, "Five")
    path = github.issue_path("acme", "shop", 5)
    r1 = github.reader.get(path, 0)
    assert not r1.cached and r1.data["number"] == 5 and r1.server_now == START
    r2 = github.reader.get(path, 0)                                           # ttl 0: asks again, with the ETag
    calls = [c["argv"] for c in gh.calls()]
    assert len(calls) == 2 and not any("If-None-Match" in " ".join(calls[0]) for _ in [0])
    tag = github.reader.cache[path].etag
    assert re.fullmatch(r'W/"[0-9a-f]{16}"', tag) and f"If-None-Match: {tag}" in calls[1]
    assert r2.cached and r2.data == r1.data and not r2.stale, "a 304 keeps the cached body"
    gh.set(salt="changed")                                                    # the same body now has another ETag only by salt: a 200
    gh.world["issues"][f"{REPO}#5"]["title"] = "Five, renamed"
    gh.save()
    r3 = github.reader.get(path, 0)
    assert not r3.cached and r3.data["title"] == "Five, renamed"
    assert github.reader.calls == 3


def test_a_fresh_entry_costs_no_call_and_an_old_one_is_revalidated(gh, clock):
    gh.add_issue(REPO, 5)
    path = github.issue_path("acme", "shop", 5)
    github.reader.get(path, 60)
    clock.t += 59
    github.reader.get(path, 60)
    assert len(gh.calls()) == 1
    clock.t += 2
    r = github.reader.get(path, 60)
    assert len(gh.calls()) == 2 and r.cached, "past the ttl: one revalidation, answered 304"


def test_the_date_header_is_kept_as_server_time_and_not_the_local_clock(gh, monkeypatch):
    gh.add_issue(REPO, 5)
    monkeypatch.setattr("time.time", lambda: 12345.0)
    rep = github.reader.get(github.issue_path("acme", "shop", 5), 0)
    assert rep.server_now == START and github.server_time({"date": "garbage"}) is None and github.server_time({}) is None


def test_two_pages_are_read_and_the_page_loop_stops_at_a_short_page(gh, monkeypatch):
    epic = world(gh, claims=False)
    for i in range(300, 370):
        gh.add_issue(REPO, i, f"Extra {i}")
        gh.link(epic, f"{REPO}#{i}")
    gh.set(page_size=40)                                                      # GitHub sends 40 per page whatever is asked: Link says there is more
    items, trunc, srv, stale, *_ = nodes_epics._paged(lambda p: github.sub_issues_path("acme", "shop", 147, p), 100, 60)
    assert len(items) == 77 - 0 and not trunc, "7 listed + 70 extra, over two pages"
    paths = [p for p in gh.paths() if "sub_issues" in p]
    assert paths == [github.sub_issues_path("acme", "shop", 147, 1), github.sub_issues_path("acme", "shop", 147, 2)]


def test_the_page_loop_follows_the_page_number_not_a_url_from_the_headers(gh):
    epic = world(gh, claims=False)
    gh.set(page_size=3)
    gh.fail("sub_issues?per_page=100&page=1", 200, headers={"Link": '<https://evil.example/x>; rel="next"'}, body=[], times=1)
    items, *_ = nodes_epics._paged(lambda p: github.sub_issues_path("acme", "shop", 147, p), 100, 60)
    assert items == [] and all(p.startswith("/repos/acme/shop/") for p in gh.paths())
    assert not any("evil" in " ".join(c["argv"]) for c in gh.calls())


def test_the_100_child_cap(board):
    gh = board.gh
    gh.add_issue(REPO, 147, "Big epic", labels=["ccboard:epic"])
    for i in range(1, 131):
        gh.add_issue(REPO, 1000 + i, f"Child {i}")
        gh.link(f"{REPO}#147", f"{REPO}#{1000 + i}")
    d = get(board).json()
    assert len(d["children"]) == 100 and d["truncated"] is True
    assert [c["number"] for c in d["children"][:2]] == [1001, 1002] and d["progress"]["total"] == 100


def test_the_100_comment_cap_never_makes_a_cut_thread_look_free(board):
    """Finding 3: 100 junk comments, then a real claim. The old reader kept the first 100 and called the issue unclaimed."""
    gh = board.gh
    world(gh, claims=False)
    gh.junk(f"{REPO}#202", 100)
    gh.comment(f"{REPO}#202", claim_body("node-a", "running"), created_at=iso(START - 100), updated_at=iso(START - 100))
    d = get(board).json()
    assert child(d, 202)["claim"]["alias"] == "node-a" and child(d, 202)["claim_state"] == "claimed", "101 comments are read whole (page 1 and page 2)"
    assert len([p for p in gh.paths() if "/202/comments" in p]) == 2


# ================================================================ rate limits: honour Retry-After and remaining 0, in memory, never loop

def test_a_403_with_retry_after_backs_off_and_surfaces_the_wait_without_asking_again(board, clock):
    gh = board.gh
    world(gh)
    gh.fail("/issues/147", 403, headers={"Retry-After": "40"}, body={"message": "You have exceeded a secondary rate limit"}, times=1)
    d = get(board).json()
    assert d["error"] == {"reason": "rate_limited", "message": "GitHub asked us to wait 40 s", "wait_s": 40} and d["children"] == []
    n = len(gh.calls())
    clock.t += 10
    d2 = get(board).json()
    assert len(gh.calls()) == n, "while GitHub asks us to wait, nothing is asked"
    assert d2["error"]["wait_s"] == 30 and d2["rate"]["wait_s"] == 30 and d2["error"]["message"] == "GitHub asked us to wait 30 s"
    clock.t += 31
    d3 = get(board).json()
    assert d3["error"] is None and len(d3["children"]) == 7


def test_remaining_zero_on_a_403_waits_until_the_reset_by_githubs_own_clock(gh, clock):
    gh.add_issue(REPO, 5)
    gh.fail("/issues/5", 403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(START + 95), "x-ratelimit-limit": "5000"}, body={"message": "API rate limit exceeded"})
    with pytest.raises(github.GhReadError) as e:
        github.reader.get(github.issue_path("acme", "shop", 5), 0)
    assert e.value.reason == "rate_limited" and e.value.wait_s == 95 and e.value.message == "GitHub asked us to wait 95 s"
    assert github.reader.rate()["wait_s"] == 95 and github.reader.remaining == 0


def test_an_exhausted_budget_on_a_good_answer_holds_back_the_next_read(gh, clock):
    gh.add_issue(REPO, 5)
    gh.add_issue(REPO, 6)
    gh.set(rate={"limit": 5000, "remaining": 0, "reset": START + 30})
    assert github.reader.get(github.issue_path("acme", "shop", 5), 0).data["number"] == 5
    with pytest.raises(github.GhReadError) as e:
        github.reader.get(github.issue_path("acme", "shop", 6), 0)
    assert e.value.reason == "rate_limited" and e.value.wait_s == 30 and len(gh.calls()) == 1
    clock.t += 31
    gh.set(rate={"limit": 5000, "remaining": 4000, "reset": START + 3600})
    assert github.reader.get(github.issue_path("acme", "shop", 6), 0).data["number"] == 6


def test_a_429_and_a_secondary_limit_message_wait_a_minute_and_a_huge_wait_is_capped(gh, clock):
    gh.add_issue(REPO, 5)
    gh.fail("/issues/5", 429, times=1)
    with pytest.raises(github.GhReadError) as e:
        github.reader.get(github.issue_path("acme", "shop", 5), 0)
    assert e.value.wait_s == 60
    clock.t += 61
    gh.fail("/issues/5", 403, body={"message": "You have exceeded a secondary rate limit."}, times=1)
    with pytest.raises(github.GhReadError) as e:
        github.reader.get(github.issue_path("acme", "shop", 5), 0)
    assert e.value.wait_s == 60
    clock.t += 61
    gh.fail("/issues/5", 403, headers={"Retry-After": "999999"}, times=1)
    with pytest.raises(github.GhReadError) as e:
        github.reader.get(github.issue_path("acme", "shop", 5), 0)
    assert e.value.wait_s == 3600


def test_a_plain_403_is_no_access_and_not_a_back_off(gh):
    gh.add_issue(REPO, 5)
    gh.set(forbidden=[REPO])
    with pytest.raises(github.GhReadError) as e:
        github.reader.get(github.issue_path("acme", "shop", 5), 0)
    assert e.value.reason == "no_access" and github.reader.wait_left() == 0


def test_while_backing_off_an_old_entry_is_served_as_stale(gh, clock):
    gh.add_issue(REPO, 5)
    path = github.issue_path("acme", "shop", 5)
    github.reader.get(path, 60)
    clock.t += 100
    gh.fail("/issues/5", 429, headers={"Retry-After": "30"})
    rep = github.reader.get(path, 60)
    assert rep.stale and rep.cached and rep.data["number"] == 5
    n = len(gh.calls())
    assert github.reader.get(path, 60).stale and len(gh.calls()) == n


def test_the_back_off_is_not_a_loop(board, clock):
    gh = board.gh
    world(gh)
    gh.fail("/issues/147", 429, times=-1)
    for _ in range(5):
        d = get(board).json()
        assert d["error"]["reason"] == "rate_limited"
    assert len(gh.paths()) == 1, "one request, then the wait; five page loads made no more"


# ================================================================ states: no gh, no login, no access, not found, errors; never a 5xx

def test_no_gh_on_the_path_is_a_readable_state(board, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    r = get(board)
    assert r.status_code == 200 and r.json()["error"]["reason"] == "no_gh" and "not installed" in r.json()["error"]["message"] and r.json()["children"] == []


def test_no_gh_login_is_a_readable_state_and_the_secret_is_not_in_it(board):
    board.gh.set(login=None)
    r = get(board)
    d = r.json()
    assert r.status_code == 200 and d["error"] == {"reason": "no_login", "message": "gh is not logged in on this board: run gh auth login there", "wait_s": None}
    assert SECRET not in r.text and d["epic"]["url"] == "https://github.com/acme/shop/issues/147"


def test_no_access_not_found_and_server_errors_are_states_with_plain_messages(board):
    gh = board.gh
    world(gh)
    gh.set(forbidden=[REPO])
    d = get(board).json()
    assert d["error"]["reason"] == "no_access" and d["error"]["message"] == "this gh login cannot read that repository"
    gh.set(forbidden=[])
    assert get(board, n=999).json()["error"] == {"reason": "not_found", "message": "GitHub has no such issue, or this login cannot see it", "wait_s": None}
    gh.fail("/issues/147", 502, times=1)
    r = get(board)
    assert r.status_code == 200 and r.json()["error"]["reason"] == "unavailable"
    gh.fail("/issues/147", 200, body="not a list or object", times=1)
    assert get(board).json()["error"]["reason"] == "bad_answer"


def test_a_failed_read_keeps_the_last_good_data_with_its_age(board, clock):
    gh = board.gh
    world(gh)
    good = get(board).json()
    assert good["error"] is None and good["stale_age_s"] is None
    clock.t += 500
    gh.fail("/issues/147", 502, times=-1)
    d = get(board).json()
    assert d["error"]["reason"] == "unavailable" and d["stale_age_s"] == 500
    assert [c["number"] for c in d["children"]] == [c["number"] for c in good["children"]] and d["epic"]["title"] == "Checkout rework"


def test_the_token_never_reaches_an_answer_a_log_or_an_error(board, caplog):
    gh = board.gh
    world(gh)
    caplog.set_level(logging.DEBUG)
    texts = [get(board).text]
    for status in (403, 404, 429, 500, 502):
        nodes_epics.reset()
        gh.fail("/issues/147", status, times=1)
        texts.append(get(board).text)
    gh.set(login=None)
    nodes_epics.reset()
    texts.append(get(board).text)
    texts.append(board.c.get("/api/epics", headers=ID).text)
    gh.set(login="alice")
    texts.append(json.dumps(github.GhReadError("no_login").as_dict()))
    assert gh.calls() and all(SECRET not in t and "ghp_" not in t for t in texts)
    assert SECRET not in caplog.text and "ghp_" not in caplog.text


def test_the_reader_reduces_gh_failures_to_a_fixed_reason_without_stderr(gh):
    gh.add_issue(REPO, 5)
    gh.fail("/issues/5", 500, body={"message": "boom " + SECRET})
    with pytest.raises(github.GhReadError) as e:
        github.reader.get(github.issue_path("acme", "shop", 5), 0)
    assert e.value.reason == "unavailable" and SECRET not in str(e.value) and SECRET not in repr(e.value.as_dict()) and "boom" not in str(e.value)


def test_an_answer_over_the_cap_is_too_large_and_a_timeout_is_a_state(gh, monkeypatch):
    gh.add_issue(REPO, 5, body="x" * 3000)
    monkeypatch.setattr(github, "OUT_CAP", 500)
    with pytest.raises(github.GhReadError) as e:
        github.reader.get(github.issue_path("acme", "shop", 5), 0)
    assert e.value.reason == "too_large"
    monkeypatch.setattr(github, "OUT_CAP", 2 * 1024 * 1024)

    def slow(*a, **k):
        raise subprocess.TimeoutExpired("gh", 1)
    monkeypatch.setattr(github.subprocess, "run", slow)
    with pytest.raises(github.GhReadError) as e:
        github.api(github.issue_path("acme", "shop", 5))
    assert e.value.reason == "timeout" and e.value.message == "gh did not answer in time"


# ================================================================ claims: the parser and the author rule

def test_parse_claim_reads_the_documented_line_and_tolerates_extra_fields():
    c = nodes_epics.parse_claim(claim_body("node-a", "running", task="42", branch="worktree-x", extra=" colour=blue reclaims=77"))
    assert c == {"alias": "node-a", "status": "running", "nid": "a1b2c3d4", "lease_min": 30, "seq": 1, "attempt": 1, "task_ref": "42", "branch": "worktree-x", "reclaims": 77}
    c = nodes_epics.parse_claim("<!-- ccboard:claim v1 -->\nccboard claim: alias=n1 status=claimed lease=999m seq=x attempt=0 nid=ZZ task=$(rm)\n")
    assert c["lease_min"] == 120 and c["seq"] == 0 and c["attempt"] == 1 and c["nid"] is None and c["task_ref"] is None
    assert nodes_epics.parse_claim(claim_body(lease="1m"))["lease_min"] == 10 and nodes_epics.parse_claim(claim_body(lease="soon"))["lease_min"] == 30


def test_parse_claim_ignores_what_is_not_a_v1_claim():
    for body in (None, 5, "", "ccboard claim: alias=a status=claimed", claim_body(version="v2"), claim_body(version="v10"), claim_body(version="v0"),
                 "hello\n" + claim_body(), "<!-- ccboard:claim v1 -->\nno fields here", claim_body(status="owned"), claim_body(status=""),
                 "<!-- ccboard:claim v1 -->\nccboard claim: status=claimed", claim_body(alias="x" * 41), claim_body(alias="-"),
                 "<!-- ccboard:claim v1 -->\nccboard claim: alias=a‮b status=claimed", "<!-- ccboard:claim v1 -->\nccboard claim: alias=a​b status=claimed",
                 "<!-- ccboard:claim v1 --> ccboard claim: alias=a status=claimed"):
        assert nodes_epics.parse_claim(body) is None, repr(body)


def test_a_unicode_alias_is_kept_and_the_marker_may_follow_leading_blank_lines():
    assert nodes_epics.parse_claim("\n \n" + claim_body(alias="knoten-ä"))["alias"] == "knoten-ä"


def test_a_comment_that_copies_a_marker_line_is_counted_as_a_claim_which_is_the_documented_limit():
    forged = claim_body("node-a", "running")
    claims = nodes_epics.parse_claims([{"id": 1, "body": forged, "user": {"login": "owner1", "type": "User"}, "author_association": "OWNER", "created_at": "2026-10-01T09:00:00Z",
                                        "updated_at": "2026-10-01T09:00:00Z"}])
    assert len(claims) == 1 and claims[0]["author"] == "owner1" and claims[0]["association"] == "OWNER"


def test_parse_claims_drops_comments_without_id_or_timestamps_and_orders_by_created_then_id():
    base = {"user": {"login": "alice"}, "body": claim_body()}
    cs = [{**base, "id": 9, "created_at": "2026-10-01T09:00:02Z", "updated_at": "2026-10-01T09:00:02Z"},
          {**base, "id": 8, "created_at": "2026-10-01T09:00:02Z", "updated_at": "2026-10-01T09:00:02Z"},
          {**base, "id": 7, "created_at": "2026-10-01T09:00:01Z", "updated_at": "2026-10-01T09:00:09Z"},
          {**base, "id": True, "created_at": "2026-10-01T09:00:01Z", "updated_at": "2026-10-01T09:00:01Z"},
          {**base, "id": 5, "created_at": "yesterday", "updated_at": "2026-10-01T09:00:01Z"},
          {**base, "created_at": "2026-10-01T09:00:01Z", "updated_at": "2026-10-01T09:00:01Z"}, "junk", None,
          {**base, "id": 6, "user": {"login": "bad login!"}, "created_at": "2026-10-01T09:00:05Z", "updated_at": "2026-10-01T09:00:05Z"}]
    out = nodes_epics.parse_claims(cs)
    assert [c["id"] for c in out] == [7, 8, 9, 6] and out[3]["author"] is None


def test_liveness_uses_the_lease_and_githubs_clock():
    c = nodes_epics.parse_claim(claim_body("a", "running"))
    c["updated_at"] = iso(START - 29 * 60)
    assert nodes_epics.is_live(c, START) is True
    c["updated_at"] = iso(START - 31 * 60)
    assert nodes_epics.is_live(c, START) is False
    assert nodes_epics.is_live(c, None) is None
    for status in ("done", "failed", "released", "cancelled"):
        c["status"] = status
        c["updated_at"] = iso(START)
        assert nodes_epics.is_live(c, START) is False


def test_a_claim_that_is_not_renewed_is_not_live_by_githubs_clock_whatever_the_local_clock_says(board, monkeypatch):
    gh = board.gh
    world(gh)
    monkeypatch.setattr("time.time", lambda: 5.0)                              # the local wall clock is far away
    monkeypatch.setattr("time.monotonic", lambda: 5.0)
    d = get(board).json()
    assert child(d, 202)["claim"]["live"] is True and child(d, 206)["claim"]["live"] is False
    nodes_epics.reset()
    gh.tick(40 * 60)                                                           # 40 minutes of server time later nothing was renewed
    d = get(board).json()
    assert child(d, 202)["claim"]["live"] is False and d["server_now"] == iso(START + 2400)


def test_a_claim_shaped_comment_from_a_stranger_is_not_a_claim(board):
    gh = board.gh
    world(gh, claims=False)
    gh.comment(f"{REPO}#202", claim_body("node-evil", "running"), user="mallory", created_at=iso(START - 600), updated_at=iso(START - 60))
    d = get(board).json()
    c = child(d, 202)
    assert c["claim"] is None and c["claim_rejected"] == 1 and c["claim_note"] == "not from an allowed author"
    assert c["task"] is None and d["progress"]["by_node"] == {} and "node-evil" not in json.dumps(d), "a stranger's alias appears nowhere"
    assert d["progress"]["unclaimed"] == {"open": 6, "closed": 1}


def test_the_repository_owner_is_not_trusted_by_being_the_owner(board):
    """Finding 1: the owner segment of the slug used to be trusted. A clone of someone else's repository must not take its owner's claim-shaped comments as claims."""
    gh = board.gh
    world(gh, claims=False)
    gh.comment(f"{REPO}#202", claim_body("node-o", "running"), user="acme", assoc="OWNER", created_at=iso(START - 900), updated_at=iso(START - 60))
    d = get(board).json()
    c = child(d, 202)
    assert c["claim"] is None and c["claim_rejected"] == 1 and c["claim_note"] == "not from an allowed author" and "node-o" not in json.dumps(d)
    assert c["claim_state"] == "none", "all comments were read and no allowed claim exists"


def test_the_configured_authors_count_and_by_default_the_gh_login(board, monkeypatch):
    gh = board.gh
    world(gh, claims=False)
    gh.comment(f"{REPO}#203", claim_body("node-c", "claimed"), user="Carol", created_at=iso(START - 900), updated_at=iso(START - 60))
    gh.comment(f"{REPO}#204", claim_body("node-g", "claimed"), user="alice", created_at=iso(START - 900), updated_at=iso(START - 60))
    d = get(board).json()
    assert child(d, 204)["claim"]["alias"] == "node-g", "empty CCBOARD_EPIC_AUTHORS: the login gh uses on this board (api user -> alice)"
    assert child(d, 203)["claim"] is None and child(d, 203)["claim_rejected"] == 1
    monkeypatch.setattr(settings, "epic_authors", ("carol",))
    d = get(board).json()
    assert child(d, 203)["claim"]["alias"] == "node-c", "a configured login counts, whatever its case"
    assert child(d, 204)["claim"] is None, "with authors set, the gh login is not added on top"


@pytest.mark.parametrize("assoc,counts", [("OWNER", True), ("MEMBER", True), ("COLLABORATOR", True), ("CONTRIBUTOR", False), ("FIRST_TIMER", False),
                                          ("FIRST_TIME_CONTRIBUTOR", False), ("NONE", False), ("MANNEQUIN", False), ("owner", False), (None, False), ("", False)])
def test_a_claim_counts_only_from_someone_who_can_write_to_the_repository(board, monkeypatch, assoc, counts):
    """The login matches (alice, the gh login; also bob, a configured name) but GitHub's author_association must still be OWNER, MEMBER or COLLABORATOR."""
    gh = board.gh
    world(gh, claims=False)
    gh.comment(f"{REPO}#202", claim_body("node-a", "running"), user="alice", assoc=assoc, created_at=iso(START - 900), updated_at=iso(START - 60))
    c = child(get(board).json(), 202)
    assert (c["claim"] is not None) is counts and c["claim_rejected"] == (0 if counts else 1)
    monkeypatch.setattr(settings, "epic_authors", ("alice",))
    nodes_epics.reset()
    c = child(get(board).json(), 202)
    assert (c["claim"] is not None) is counts, "a configured name is no exception"


def test_a_comment_with_no_user_a_bot_or_a_login_that_is_not_one_never_counts(board, monkeypatch):
    gh = board.gh
    world(gh, claims=False)
    monkeypatch.setattr(settings, "epic_authors", ("alice", "ghost", "dependabot"))
    gh.comment(f"{REPO}#202", claim_body("node-a", "running"), user=None, created_at=iso(START - 900), updated_at=iso(START - 60))
    gh.comment(f"{REPO}#203", claim_body("node-a", "running"), user="dependabot", user_type="Bot", created_at=iso(START - 900), updated_at=iso(START - 60))
    gh.comment(f"{REPO}#204", claim_body("node-a", "running"), user="alice", user_type="Organization", created_at=iso(START - 900), updated_at=iso(START - 60))
    gh.comment(f"{REPO}#205", claim_body("node-a", "running"), user="al ice;x", created_at=iso(START - 900), updated_at=iso(START - 60))
    d = get(board).json()
    for n in (202, 203, 204, 205):
        assert child(d, n)["claim"] is None and child(d, n)["claim_rejected"] == 1 and child(d, n)["claim_state"] == "none", n
    assert nodes_epics.parse_claims([{"id": 1, "body": claim_body(), "user": {"login": "alice"}, "author_association": "OWNER", "created_at": "2026-10-01T09:00:00Z",
                                      "updated_at": "2026-10-01T09:00:00Z"}])[0]["author"] is None, "a user object with no type is not a user"


def test_the_login_compare_ignores_case_and_a_squatter_on_a_renamed_login_is_refused_by_the_association(board):
    gh = board.gh
    world(gh, claims=False)
    gh.comment(f"{REPO}#202", claim_body("node-a", "running"), user="ALICE", assoc="MEMBER", created_at=iso(START - 900), updated_at=iso(START - 60))
    gh.comment(f"{REPO}#203", claim_body("node-b", "running"), user="alice", assoc="NONE", created_at=iso(START - 900), updated_at=iso(START - 60))
    d = get(board).json()
    assert child(d, 202)["claim"]["alias"] == "node-a", "the same login in another case, from a writer"
    assert child(d, 203)["claim"] is None and child(d, 203)["claim_rejected"] == 1, "the same login string from someone who cannot write (a new owner of a freed name)"


def test_when_gh_cannot_say_who_it_is_nobody_counts_and_the_page_says_it_cannot_tell(board, monkeypatch):
    """Fail closed: no CCBOARD_EPIC_AUTHORS and no gh login. No claim counts, no child is reported free (every child is unknown), and no comment is even read."""
    gh = board.gh
    world(gh)
    monkeypatch.setattr(nodes_epics, "_gh_login", lambda: None)
    d = get(board).json()
    assert d["claims_note"] == "cannot tell who may claim: gh login unknown"
    assert all(c["claim"] is None and c["claim_state"] == "unknown" and c["claim_unknown"] == {"reason": "authors_unknown", "text": "cannot tell who may claim: gh login unknown"}
               for c in d["children"])
    assert not any("/comments" in p for p in gh.paths()), "nothing is read that could not be told apart"
    p = d["progress"]
    assert p["unclaimed"] == {"open": 0, "closed": 1} and p["unknown"] == 6, "the six open children are unknown, none is unclaimed (the closed one is closed)"
    assert sum(1 for c in d["children"] if c["bucket"]["state"] == "open") == 0, "never a bucket that reads as free"
    monkeypatch.setattr(settings, "epic_authors", ("alice",))
    d = get(board).json()
    assert d["claims_note"] is None and child(d, 202)["claim"]["alias"] == "node-a", "a configured author needs no gh login"


def test_allowed_authors_is_the_configured_names_or_the_gh_login_never_the_owner():
    assert nodes_epics.allowed_authors(None, ()) == set() and nodes_epics.allowed_authors("", ()) == set()
    assert nodes_epics.allowed_authors("Alice", ()) == {"alice"}
    assert nodes_epics.allowed_authors("alice", ("bob", "Carol")) == {"bob", "carol"}
    assert nodes_epics.allowed_authors("bad login;", ()) == set() and nodes_epics.allowed_authors("-x", ()) == set()


def test_the_first_live_claim_by_created_then_id_holds_the_issue_and_an_older_stale_one_is_shown_only_when_nothing_is_live(board):
    gh = board.gh
    world(gh, claims=False)
    live = iso(START - 60)
    gh.comment(f"{REPO}#202", claim_body("node-a", "claimed"), user="alice", created_at=iso(START - 500), updated_at=live, cid=10)          # ids increase in the order GitHub lists them
    gh.comment(f"{REPO}#202", claim_body("node-b", "claimed"), user="alice", created_at=iso(START - 500), updated_at=live, cid=20)
    gh.comment(f"{REPO}#202", claim_body("node-c", "claimed"), user="alice", created_at=iso(START - 900), updated_at=iso(START - 9000), cid=30)
    gh.comment(f"{REPO}#203", claim_body("node-b", "running"), user="alice", created_at=iso(START - 9000), updated_at=iso(START - 9000), cid=31)
    gh.comment(f"{REPO}#203", claim_body("node-a", "released"), user="alice", created_at=iso(START - 8000), updated_at=iso(START - 7000), cid=32)
    d = get(board).json()
    assert child(d, 202)["claim"]["alias"] == "node-a" and child(d, 202)["claim"]["live"] is True, "equal created_at: the lower comment id"
    assert child(d, 203)["claim"]["alias"] == "node-a" and child(d, 203)["claim"]["status"] == "released" and child(d, 203)["claim"]["live"] is False


# ================================================================ the detail answer

def test_the_detail_answer_shape_children_claims_progress_and_links(board):
    gh = board.gh
    world(gh)
    r = get(board)
    d = r.json()
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert d["epic"] == {"repo": REPO, "owner": "acme", "name": "shop", "number": 147, "title": "Checkout rework", "url": "https://github.com/acme/shop/issues/147",
                         "state": "open", "labelled": True, "updated_at": "2026-10-01T09:00:00Z", "author": "alice", "edit_state": None, "edited_by": None}
    assert d["source"] == "sub_issues" and d["note"] is None and d["error"] is None and d["empty"] is False and d["partial"] is False and d["truncated"] is False
    assert [(c["repo"], c["number"]) for c in d["children"]] == [(REPO, n) for n in range(201, 207)] + [("acme/api", 9)], "the sub-issue order is the queue order"
    assert [c["order"] for c in d["children"]] == [1, 2, 3, 4, 5, 6, 7]
    c202 = child(d, 202)
    assert c202["url"] == "https://github.com/acme/shop/issues/202" and c202["state"] == "open" and c202["labels"] == ["ccboard:in-progress"]
    assert c202["claim"] == {"alias": "node-a", "node": "local", "status": "running", "since": iso(START - 900), "renewed_at": iso(START - 120), "live": True, "attempt": 1,
                             "seq": 1, "task_ref": "42", "branch": "worktree-tax"}
    assert child(d, 201)["claim"]["status"] == "done" and child(d, 201)["state"] == "closed"
    assert child(d, 203)["claim"]["alias"] == "node-b" and child(d, 203)["claim"]["live"] is True and child(d, 203)["claim"]["node"] is None
    assert child(d, 205)["claim"] is None and child(d, 205)["claim_rejected"] == 0 and child(d, 205)["claim_note"] is None
    assert child(d, 206)["claim"]["live"] is False and child(d, 206)["claim"]["status"] == "running"
    other = child(d, 9, "acme/api")
    assert other["url"] == "https://github.com/acme/api/issues/9" and other["claim"]["alias"] == "node-b", "a child in another repository"
    assert d["server_now"] == iso(START) and d["rate"] == {"remaining": 4990, "limit": 5000, "reset": iso(START + 1800), "wait_s": None}


def test_the_progress_numbers_add_up_and_a_closed_child_counts_as_done(board):
    gh = board.gh
    world(gh)
    p = get(board).json()["progress"]
    assert p["by_node"] == {"node-a": {"done": 1, "running": 1, "queued": 0, "failed": 0, "handle": "local"},
                            "node-b": {"done": 1, "running": 0, "queued": 1, "failed": 1, "handle": None}}
    assert p["unclaimed"] == {"open": 2, "closed": 0} and p["open"] == 6 and p["closed"] == 1 and p["total"] == 7
    per_node = sum(v[k] for v in p["by_node"].values() for k in ("done", "running", "queued", "failed"))
    assert per_node + p["unclaimed"]["open"] + p["unclaimed"]["closed"] == p["total"] == p["open"] + p["closed"]


def test_each_child_carries_the_bucket_the_progress_strip_counted_it_in(board):
    """The page groups rows by `bucket`, so the rows and the strip cannot disagree: counting the buckets of the children gives exactly `progress`."""
    gh = board.gh
    world(gh)
    d = get(board).json()
    by, un = {}, {"open": 0, "closed": 0}
    for c in d["children"]:
        b = c["bucket"]
        assert set(b) == {"node", "state"} and b["state"] in ("done", "running", "queued", "failed", "open", "closed")
        if b["node"] is None:
            un["closed" if b["state"] == "closed" else "open"] += 1
        else:
            by.setdefault(b["node"], {"done": 0, "running": 0, "queued": 0, "failed": 0})[b["state"]] += 1
    assert {k: {s: v[s] for s in by[k]} for k, v in d["progress"]["by_node"].items()} == by
    assert d["progress"]["unclaimed"] == un
    assert child(d, 201)["bucket"] == {"node": "node-a", "state": "done"}, "a closed child is done for the node that held it"


def test_a_closed_child_with_no_claim_counts_as_closed_for_nobody_and_still_adds_up(board):
    gh = board.gh
    world(gh, claims=False)
    p = get(board).json()["progress"]
    assert p["by_node"] == {} and p["unclaimed"] == {"open": 6, "closed": 1} and p["total"] == 7 == p["open"] + p["closed"]


def test_an_empty_epic_says_so_and_a_pull_request_in_the_label_list_is_not_a_child(board):
    gh = board.gh
    gh.add_issue(REPO, 147, "Empty one", labels=["ccboard:epic"])
    d = get(board).json()
    assert d["children"] == [] and d["empty"] is True and d["error"] is None and d["progress"]["total"] == 0


def test_the_sub_issues_endpoint_404_falls_back_to_the_label_list_and_says_so(board):
    gh = board.gh
    gh.add_issue(REPO, 147, "Epic", labels=["ccboard:epic"])
    gh.add_issue(REPO, 12, "Second", labels=["epic:147"])
    gh.add_issue(REPO, 11, "First", labels=["epic:147"], state="closed")
    gh.add_issue(REPO, 13, "A pull request", labels=["epic:147"], pull_request=True)
    gh.add_issue(REPO, 14, "Other epic's child", labels=["epic:999"])
    gh.set(sub_issues_missing=True)
    d = get(board).json()
    assert d["source"] == "labels" and d["note"] == "This repository has no sub-issues endpoint, so the children are the issues labelled epic:147."
    assert [c["number"] for c in d["children"]] == [11, 12] and [c["order"] for c in d["children"]] == [1, 2], "issue-number order, no pull request"
    assert d["error"] is None and d["progress"]["open"] == 1 and d["progress"]["closed"] == 1
    assert any("labels=epic%3A147" in p for p in gh.paths())


def test_depends_on_blocked_by_and_a_cycle_are_read_from_the_child_bodies(board):
    gh = board.gh
    world(gh, claims=False)
    gh.world["issues"][f"{REPO}#202"]["body"] = "Notes\n\nDepends on #201\n- depends on: #203, acme/api#9 and other/thing#4 and #202\ntext #9 depends on #1"
    gh.world["issues"][f"{REPO}#203"]["body"] = "> Depends on #202"
    gh.world["issues"][f"{REPO}#204"]["body"] = "depends on #1; depends on ../../etc#5 and a/b/c#6 and -x/r#7 " + "#8 " * 40
    gh.save()
    d = get(board).json()
    c202, c203, c204 = child(d, 202), child(d, 203), child(d, 204)
    assert c202["depends_on"] == ["#201", "#203", "acme/api#9", "other/thing#4"] and c202["blocked_by"] == ["#203", "acme/api#9"]
    assert c202["dependency_cycle"] is True and c203["dependency_cycle"] is True and child(d, 201)["dependency_cycle"] is False
    assert c203["depends_on"] == ["#202"] and len(c204["depends_on"]) <= 20 and "#1" in c204["depends_on"] and not any(x.startswith(("../", "-x", "a/b/c")) for x in c204["depends_on"])
    assert child(d, 205)["depends_on"] == [] and child(d, 205)["blocked_by"] == []


# ================================================================ untrusted text: capped, cleaned, whitelisted; URLs never used

def test_hostile_titles_labels_and_comments_are_capped_and_cleaned(board):
    gh = board.gh
    gh.add_issue(REPO, 147, "Epic‮​ title\x00\x1b[31m" + "A" * 500, labels=["ccboard:epic"])
    gh.add_issue(REPO, 201, "<img src=x onerror=alert(1)>   line\nbreak", labels=["bug‮", "x" * 200, "\x00", "a\tb", 5, None] + ["l%d" % i for i in range(60)])
    gh.link(f"{REPO}#147", f"{REPO}#201")
    gh.comment(f"{REPO}#201", claim_body("node-a", "running") + "<script>x</script>" * 1000, user="alice", created_at=iso(START - 900), updated_at=iso(START - 60))
    gh.comment(f"{REPO}#201", "z" * 500_000, user="mallory")
    d = get(board).json()
    assert d["error"] is None, d["error"]
    assert len(d["epic"]["title"]) <= 200 and not re.search(r"[\x00-\x1f‮​  ]", d["epic"]["title"])
    c = child(d, 201)
    assert c["title"] == "<img src=x onerror=alert(1)> line break", "markup stays text for textContent; control, separator and bidi characters are gone"
    assert len(c["labels"]) <= 30 and all(len(x) <= 50 and x.isprintable() for x in c["labels"]) and "bug" in c["labels"]
    assert c["claim"]["alias"] == "node-a" and "script" not in json.dumps(c)


def test_urls_in_the_payload_are_never_used_or_returned(board):
    gh = board.gh
    gh.add_issue(REPO, 147, "Epic", labels=["ccboard:epic"], raw={"html_url": "https://evil.example/phish", "url": "https://evil.example/api"})
    gh.add_issue(REPO, 201, "Child", raw={"html_url": "https://evil.example/c", "url": "https://evil.example/c", "repository_url": "https://evil.example/repos/x/y",
                                          "comments_url": "https://evil.example/comments", "events_url": "https://evil.example/e"})
    gh.add_issue(REPO, 202, "Child two", raw={"repository_url": "https://api.github.com/repos/acme/../../etc"})
    gh.add_issue(REPO, 203, "Child three", raw={"repository_url": "https://api.github.com/repos/other/lib"})
    for n in (201, 202, 203):
        gh.link(f"{REPO}#147", f"{REPO}#{n}")
    r = get(board)
    d = r.json()
    assert "evil.example" not in r.text and "phish" not in r.text
    assert d["epic"]["url"] == "https://github.com/acme/shop/issues/147"
    assert {c["number"] for c in d["children"]} == {203}, "a repository_url that is not exactly the API form places nothing: those two are not shown, and the epic says it is not complete"
    assert d["truncated"] is True
    c203 = child(d, 203, "other/lib")
    assert c203["url"] == "https://github.com/other/lib/issues/203", "a well-formed repository_url names the repo, and only its validated parts are used"
    assert c203["claim_state"] == "unknown" and c203["claim_unknown"]["text"] == "in another repository"
    assert not any("evil" in " ".join(c["argv"]) for c in gh.calls())
    assert all(re.fullmatch(r"/repos/acme/shop/issues/[0-9]+(/sub_issues\?per_page=100&page=1)?", p) for p in gh.paths()), "no request names a repository this board has no project for"


def test_a_pull_request_url_is_built_from_the_validated_repo_and_number(board):
    gh = board.gh
    world(gh, claims=False)
    tid = board.db.task_add(project="shop", repo="shop", slug="tax", title="Tax rules", prompt="p", tmux_name="shop--shop--t-tax", branch="b", worktree="/w",
                            issue_number=202, issue_url="https://github.com/acme/shop/issues/202")
    board.db.task_update(tid, pr_number=77, pr_url="https://evil.example/pull/77", pr_state="OPEN", pr_json=json.dumps({"is_draft": True}))
    d = get(board).json()
    assert child(d, 202)["pr"] == {"number": 77, "url": "https://github.com/acme/shop/pull/77", "state": "open", "draft": True}
    board.db.task_update(tid, pr_state="WEIRD")
    assert child(get(board).json(), 202)["pr"] is None


@pytest.mark.parametrize("owner,repo,n", [("-bad", "shop", "147"), ("acme", ".hidden", "147"), ("acme", "shop", "0"), ("acme", "shop", "1x"), ("a" * 40, "shop", "5"),
                                          ("acme", "..", "5"), ("acme", "shop", "9" * 10), ("ac%20me", "shop", "5"), ("acme", "s%0Ahop", "5"), ("acme", "shop", "٣")])
def test_the_routes_refuse_a_hostile_owner_repository_or_number_before_any_gh_call(board, owner, repo, n):
    r = board.c.get(f"/api/epics/{owner}/{repo}/{n}", headers=ID)
    assert r.status_code in (400, 404) and board.gh.calls() == []
    r = board.c.put(f"/api/epics/{owner}/{repo}/{n}/settings", json={"paused": True}, headers=H)
    assert r.status_code in (400, 404) and board.gh.calls() == []
    assert board.db.kv_get(nodes_epics.settings_key("acme", "shop", 147)) is None


# ================================================================ tasks and pull requests: the join with the hub read model

def make_task(board, **kw):
    row = dict(project="shop", repo="shop", slug="t", title="Tax rules", prompt="p", tmux_name="shop--shop--t-x", branch="b", worktree="/w", issue_number=202,
               issue_url="https://github.com/acme/shop/issues/202")
    row.update(kw)
    return board.db.task_add(**row)


def record(handle="box", name="node-b", status="online", age_s=0, tasks=(), projects=None, legacy=False):
    return {"peer_id": "p_" + "1" * 16, "handle": handle, "node_id": NODE_B, "name": name, "status": status, "age_s": age_s, "legacy": legacy,
            "state": {"projects": projects if projects is not None else [{"name": "shop", "repos": [{"name": "shop", "slug": "acme/shop", "branch": "main", "dirty": False}]}],
                      "tasks": list(tasks)}}


def test_the_task_joins_by_issue_number_and_repository_for_a_local_task(board):
    world(board.gh)
    tid = make_task(board)
    make_task(board, issue_number=203, issue_url="https://github.com/other/shop/issues/203", title="Elsewhere")        # another repository's #203: no match
    make_task(board, issue_number=205, issue_url=None, repo="shop", title="No url, same repo name")
    d = get(board).json()
    assert child(d, 202)["task"] == {"ref": str(tid), "phase": "running", "node": "local", "node_name": "node-a", "status": "online", "age_s": 0,
                                     "updated_at": child(d, 202)["task"]["updated_at"]}
    assert child(d, 203)["task"] is None
    assert child(d, 205)["task"]["node"] == "local", "no issue_url: the repository name tells"
    assert d["progress"]["by_node"]["node-a"]["running"] == 2, "202 (claim and task) and 205 (task only) run on node-a"


def test_a_remote_task_joins_by_issue_ref_and_the_nodes_project_slug_and_an_offline_node_keeps_its_last_phase_with_the_age(board, monkeypatch):
    world(board.gh)
    recs = [record(tasks=[{"id": 9, "phase": "running", "project": "shop", "repo": "shop", "issue_ref": "#205", "updated_at": "2026-10-10T10:00:00+00:00"},
                          {"id": 10, "phase": "queued", "project": "shop", "repo": "shop", "issue_ref": "#204", "updated_at": "2026-10-10T10:00:00+00:00"},
                          {"id": 11, "phase": "done", "project": "other", "repo": "lib", "issue_ref": "#202", "updated_at": "2026-10-10T10:00:00+00:00"},
                          {"id": 12, "phase": "failed", "project": "shop", "repo": "shop", "issue_ref": "acme/shop#206", "updated_at": "2026-10-10T11:00:00+00:00"}],
                   status="offline", age_s=7200)]
    monkeypatch.setattr(board.main, "_hub_records", lambda: recs)
    d = get(board).json()
    t = child(d, 205)["task"]
    assert t == {"ref": "box:9", "phase": "running", "node": "box", "node_name": "node-b", "status": "offline", "age_s": 7200, "updated_at": "2026-10-10T10:00:00+00:00"}
    assert child(d, 204)["task"]["phase"] == "queued" and child(d, 206)["task"]["phase"] == "failed"
    assert child(d, 202)["task"] is None, "a task of another repository with the same number does not match"
    p = d["progress"]["by_node"]
    assert p["node-b"]["running"] == 1 and p["node-b"]["failed"] == 2, "node-b: 205 running (task), 204 failed (its claim), 206 failed (task) ; 203 is queued by its claim"
    assert d["progress"]["total"] == 7 == sum(v[k] for v in p.values() for k in ("done", "running", "queued", "failed")) + sum(d["progress"]["unclaimed"].values())


def test_with_several_tasks_for_an_issue_the_active_newest_one_is_shown(board):
    world(board.gh)
    old = make_task(board, title="old")
    board.db.task_update(old, phase="failed")
    new = make_task(board, title="new")
    d = get(board).json()
    assert child(d, 202)["task"]["ref"] == str(new)
    board.db.task_update(new, phase="done")
    assert child(get(board).json(), 202)["task"]["phase"] in ("done", "failed")


def test_the_hub_text_is_cleaned_and_the_phase_is_one_of_the_known_words(board, monkeypatch):
    world(board.gh)
    recs = [record(name="evil‮node", tasks=[{"id": 9, "phase": "<b>x</b>", "project": "shop", "repo": "shop", "issue_ref": "#205", "updated_at": "x" * 500}])]
    monkeypatch.setattr(board.main, "_hub_records", lambda: recs)
    t = child(get(board).json(), 205)["task"]
    assert t["phase"] == "unknown" and "‮" not in t["node_name"] and len(t["updated_at"]) <= 40


# ================================================================ pins and pause: this board only

def test_pins_and_pause_are_stored_on_this_board_and_change_nothing_on_github(board):
    gh = board.gh
    world(gh)
    get(board)                                                                 # the page reads the epic first: a pin may name only an issue read as its child
    b = add_node(board)
    before = len(gh.calls())
    r = put(board, {"pins": {"202": b["handle"], "205": "local"}, "epic_pin": b["handle"], "paused": True, "report": "summary", "close": True})
    assert r.status_code == 200 and len(gh.calls()) == before, "settings never reach GitHub"
    s = r.json()
    assert s["pins"]["202"] == {"handle": b["handle"], "node_name": "node-b", "source": "page", "stale": False}
    assert s["pins"]["205"]["handle"] == "local" and s["epic_pin"]["handle"] == b["handle"] and s["paused"] is True and s["report"] == "summary" and s["auto_pr"] is False and s["close"] is True
    assert [n["handle"] for n in s["nodes"]] == ["local", b["handle"]]
    stored = board.db.kv_get("epic:acme/shop#147")["value"]
    assert stored == {"pins": {"202": b["handle"], "205": "local"}, "epic_pin": b["handle"], "paused": True, "report": "summary", "auto_pr": False, "close": True}
    d = get(board).json()
    assert d["paused"] is True and d["pins"]["202"]["handle"] == b["handle"] and child(d, 202)["pin"] == {"handle": b["handle"], "node_name": "node-b", "source": "page", "stale": False}
    assert child(d, 203)["pin"] is None


def test_settings_merge_per_key_and_a_null_removes_and_defaults_remove_the_row(board):
    world(board.gh)
    get(board)
    b = add_node(board)
    put(board, {"pins": {"202": b["handle"], "203": "local"}, "paused": True})
    r = put(board, {"pins": {"202": None, "204": b["handle"]}})
    assert sorted(r.json()["pins"]) == ["203", "204"] and r.json()["paused"] is True, "pins merge; paused was not sent so it stays"
    r = put(board, {"pins": {"203": "", "204": None}, "paused": False})
    assert r.json()["pins"] == {} and board.db.kv_get("epic:acme/shop#147") is None, "back to the defaults: no row is kept"
    put(board, {"epic_pin": "local"})
    assert put(board, {"epic_pin": None}).json()["epic_pin"] is None and board.db.kv_get("epic:acme/shop#147") is None


def test_settings_keys_are_case_folded_so_one_epic_has_one_row(board):
    put(board, {"paused": True}, owner="Acme", repo="Shop")
    assert board.db.kv_get("epic:acme/shop#147")["value"]["paused"] is True
    assert get(board, owner="ACME", repo="shop").json()["paused"] is True


def test_a_pin_must_name_local_or_a_paired_node(board):
    b = add_node(board)
    for bad in ("nobody", "Local", "box/../x", "a b", "x" * 40, 5):
        r = put(board, {"pins": {"202": bad}})
        assert r.status_code in (400, 422), (bad, r.text)
    assert put(board, {"epic_pin": "nobody"}).status_code == 400
    r = put(board, {"pins": {"202": "nobody"}})
    assert r.status_code == 400 and "not paired" in r.json()["error"]
    assert board.db.kv_get("epic:acme/shop#147") is None, "a refused request stores nothing, not even its valid half"
    r = put(board, {"pins": {"202": b["handle"], "203": "nobody"}})
    assert r.status_code == 400 and board.db.kv_get("epic:acme/shop#147") is None
    for key in ("abc", "0", "-1", "1.5", "1e3", "٣", " 5", "5 ", "9" * 10):
        assert put(board, {"pins": {key: "local"}}).status_code == 400, key


def test_flags_must_be_booleans_and_unknown_keys_are_refused(board):
    for body in ({"paused": "true"}, {"paused": 1}, {"paused": None}, {"report": "yes"}, {"auto_pr": 0}, {"mode": "bypassPermissions"}, {"pins": []}, {"pins": {"1": 5}},
                 {"epic_pin": 5}, {"pins": {str(i): "local" for i in range(1, 202)}}):
        r = put(board, body)
        assert r.status_code in (400, 422), (body, r.text)
    assert board.db.kv_get("epic:acme/shop#147") is None


def test_a_pin_to_a_removed_node_is_stale_and_ignored_and_the_ui_pin_wins_over_the_label(board):
    gh = board.gh
    world(gh)
    gh.world["issues"][f"{REPO}#202"]["labels"] = ["ccboard:in-progress", "ccboard:node:node-b"]
    gh.world["issues"][f"{REPO}#203"]["labels"] = ["ccboard:node:node-b", "ccboard:node:evil‮"]
    gh.world["issues"][f"{REPO}#204"]["labels"] = ["ccboard:node:node-gone"]
    gh.world["issues"][f"{REPO}#205"]["labels"] = ["ccboard:node:node-b"]
    gh.save()
    b = add_node(board)
    get(board)
    put(board, {"pins": {"202": "local", "205": b["handle"]}})
    d = get(board).json()
    assert child(d, 202)["pin"] == {"handle": "local", "node_name": "node-a", "source": "page", "stale": False}, "the page pin wins on conflict"
    assert child(d, 202)["label_pin"] == {"alias": "node-b", "handle": b["handle"], "stale": False}
    assert child(d, 203)["pin"] == {"handle": b["handle"], "node_name": "node-b", "source": "label", "stale": False}
    assert child(d, 204)["pin"] == {"handle": None, "node_name": None, "source": "label", "stale": True} and child(d, 204)["label_pin"]["stale"] is True
    nodes.remove_peer(b["peer_id"], db=board.db)
    d = get(board).json()
    assert d["pins"]["205"] == {"handle": b["handle"], "node_name": None, "source": "page", "stale": True}
    assert child(d, 205)["pin"]["stale"] is True and child(d, 205)["label_pin"]["stale"] is True and child(d, 202)["pin"]["source"] == "page" and child(d, 202)["pin"]["stale"] is False
    assert child(d, 203)["pin"]["stale"] is True


def test_a_stale_page_pin_falls_back_to_a_good_label_pin(board):
    gh = board.gh
    world(gh)
    gh.world["issues"][f"{REPO}#202"]["labels"] = ["ccboard:node:node-a"]
    gh.save()
    b = add_node(board)
    get(board)
    put(board, {"pins": {"202": b["handle"]}})
    nodes.remove_peer(b["peer_id"], db=board.db)
    assert child(get(board).json(), 202)["pin"] == {"handle": "local", "node_name": "node-a", "source": "label", "stale": False}


def test_settings_need_a_signed_in_person_with_the_csrf_header_and_not_the_hook_token_or_a_node(board):
    from app import hooks
    assert board.c.put("/api/epics/acme/shop/147/settings", json={"paused": True}).status_code == 403
    assert board.c.put("/api/epics/acme/shop/147/settings", json={"paused": True}, headers=ID).status_code == 403, "no X-CCBoard header"
    tok = {hooks.TOKEN_HEADER: hooks.ensure_token()}
    r = board.c.put("/api/epics/acme/shop/147/settings", json={"paused": True}, headers=tok)
    assert r.status_code == 403 and "signed-in person" in r.json()["error"]
    assert board.db.kv_get("epic:acme/shop#147") is None
    assert put(board, {"paused": True}).status_code == 200


def test_the_read_routes_need_an_identity(board):
    assert board.c.get("/api/epics").status_code == 403
    assert board.c.get("/api/epics/acme/shop/147").status_code == 403
    assert board.gh.calls() == []


# ================================================================ the list

def git_repo(projects_dir, name, remote):
    path = projects_dir / "shop" / name
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    if remote:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote], check=True)
    return path


def with_projects(board, monkeypatch, repos):
    from app import node_state
    node_state.slug_cache_clear()
    projs = [{"name": "shop", "repos": [{"name": p.name, "path": str(p), "state": "ok"} for p in repos]}]
    monkeypatch.setattr(board.main, "_cached_projects", lambda: projs)


def test_the_list_finds_epics_in_the_repos_the_projects_remotes_name_with_child_counts(board, projects_dir, monkeypatch):
    gh = board.gh
    a = git_repo(projects_dir, "api", "https://github.com/acme/api.git")
    s = git_repo(projects_dir, "shop", "git@github.com:acme/shop.git")
    git_repo(projects_dir, "local", None)
    git_repo(projects_dir, "gitlab", "https://gitlab.com/acme/other.git")
    b = projects_dir / "shop" / "local"
    c = projects_dir / "shop" / "gitlab"
    with_projects(board, monkeypatch, [a, s, b, c])
    world(gh)
    gh.add_issue("acme/api", 3, "API epic", labels=["ccboard:epic"], summary={"total": 4, "completed": 1, "percent_completed": 25}, updated_at="2026-10-05T09:00:00Z")
    gh.add_issue("acme/api", 4, "A pull request", labels=["ccboard:epic"], pull_request=True)
    gh.add_issue("acme/api", 5, "Closed epic", labels=["ccboard:epic"], state="closed")
    gh.add_issue(REPO, 150, "Another epic", labels=["ccboard:epic"], updated_at="2026-10-08T09:00:00Z")
    r = board.c.get("/api/epics", headers=ID)
    d = r.json()
    assert r.status_code == 200 and d["repos"] == ["acme/api", "acme/shop"] and d["error"] is None and d["errors"] == []
    assert [(e["repo"], e["number"]) for e in d["epics"]] == [(REPO, 150), ("acme/api", 3), (REPO, 147)], "newest update first"
    by = {(e["repo"], e["number"]): e for e in d["epics"]}
    assert by[("acme/api", 3)]["children"] == {"open": 3, "closed": 1, "total": 4, "more": False}, "from the parent's own summary: no extra call"
    assert by[(REPO, 147)]["children"] == {"open": 6, "closed": 1, "total": 7, "more": False}, "no summary: counted from the sub-issues list"
    assert by[(REPO, 150)]["children"] == {"open": 0, "closed": 0, "total": 0, "more": False}
    assert by[(REPO, 147)] == {"repo": REPO, "owner": "acme", "name": "shop", "number": 147, "title": "Checkout rework", "url": "https://github.com/acme/shop/issues/147",
                               "state": "open", "children": {"open": 6, "closed": 1, "total": 7, "more": False}, "updated_at": "2026-10-01T09:00:00Z", "labels": ["ccboard:epic"], "paused": False}
    assert not any("gitlab" in p or "other" in p for p in gh.paths())


def test_the_list_is_cached_for_60_seconds_and_pause_is_read_fresh(board, projects_dir, monkeypatch, clock):
    gh = board.gh
    with_projects(board, monkeypatch, [git_repo(projects_dir, "shop", "https://github.com/acme/shop.git")])
    world(gh)
    d1 = board.c.get("/api/epics", headers=ID).json()
    n = len(gh.calls())
    put(board, {"paused": True})
    clock.t += 59
    d2 = board.c.get("/api/epics", headers=ID).json()
    assert len(gh.calls()) == n and d2["epics"][0]["paused"] is True and d1["epics"][0]["paused"] is False
    clock.t += 2
    board.c.get("/api/epics", headers=ID)
    assert len(gh.calls()) > n, "after 60 s it asks again (with the ETag)"


def test_the_list_states_no_login_rate_limit_and_a_repo_without_access_never_a_5xx(board, projects_dir, monkeypatch, clock):
    gh = board.gh
    a = git_repo(projects_dir, "api", "https://github.com/acme/api.git")
    s = git_repo(projects_dir, "shop", "https://github.com/acme/shop.git")
    with_projects(board, monkeypatch, [a, s])
    world(gh)
    gh.set(forbidden=["acme/api"])
    d = board.c.get("/api/epics", headers=ID).json()
    assert d["errors"] == [{"repo": "acme/api", "reason": "no_access", "message": "this gh login cannot read that repository", "wait_s": None}] and len(d["epics"]) == 1
    gh.set(forbidden=[], login=None)
    nodes_epics.reset()
    r = board.c.get("/api/epics", headers=ID)
    assert r.status_code == 200 and r.json()["error"]["reason"] == "no_login" and r.json()["epics"] == []
    gh.set(login="alice")
    nodes_epics.reset()
    gh.fail("labels=ccboard%3Aepic", 429, headers={"Retry-After": "40"})
    d = board.c.get("/api/epics", headers=ID).json()
    assert d["error"] == {"reason": "rate_limited", "message": "GitHub asked us to wait 40 s", "wait_s": 40} and d["rate"]["wait_s"] == 40


def test_a_failed_list_read_keeps_the_last_list_with_its_age(board, projects_dir, monkeypatch, clock):
    gh = board.gh
    with_projects(board, monkeypatch, [git_repo(projects_dir, "shop", "https://github.com/acme/shop.git")])
    world(gh)
    assert len(board.c.get("/api/epics", headers=ID).json()["epics"]) == 1
    clock.t += 300
    gh.set(login=None)
    d = board.c.get("/api/epics", headers=ID).json()
    assert d["error"]["reason"] == "no_login" and d["stale_age_s"] == 300 and len(d["epics"]) == 1


def test_no_github_remote_means_nothing_is_asked_of_github(board):
    board.repos()
    d = board.c.get("/api/epics", headers=ID).json()
    assert d == {**d, "epics": [], "repos": [], "error": None, "errors": []} and "GitHub remote" in d["note"] and board.gh.calls() == []


def test_known_repos_are_this_boards_own_projects_deduplicated_and_validated(monkeypatch):
    from app import node_state
    monkeypatch.setattr(node_state, "repo_slug", lambda path: {"/p/a": "Acme/Shop", "/p/b": "acme/api", "/p/d": "acme/shop", "/p/e": "-x/y", "/p/f": "a/b/c", "/p/g": "o/.."}.get(path))
    projs = [{"name": "p", "repos": [{"path": "/p/a", "state": "ok"}, {"path": "/p/b", "state": "ok"}, {"path": "/p/c", "state": "ok"}, {"path": "/p/a", "state": "error"},
                                     {"path": "/p/d", "state": "ok"}, {"path": "/p/e", "state": "ok"}, {"path": "/p/f", "state": "ok"}, {"path": "/p/g", "state": "ok"}]}]
    assert nodes_epics.known_repos(projs) == ["Acme/Shop", "acme/api"]
    carried = [{"name": "p", "repos": [{"path": "/p/c", "state": "ok", "slug": "evil/repo"}], "slug": "evil/other"}]
    assert nodes_epics.known_repos(carried) == [], "a slug carried inside a project entry (the shape of a node's reading) is not a remote of this board"
    assert nodes_epics.known_repos(None) == [] and nodes_epics.known_repos([{"repos": "x"}, 5]) == []
    import inspect
    assert list(inspect.signature(nodes_epics.known_repos).parameters) == ["projects_list"], "no node's reading can be passed in"


# ================================================================ nothing runs in the background

def test_a_single_board_with_no_epic_does_nothing_in_the_background(board):
    before = {t.name for t in threading.enumerate()}
    board.c.get("/api/state", headers=ID)
    board.c.get("/api/epics", headers=ID)
    board.c.get("/api/epics/acme/shop/147", headers=ID)
    after = {t.name for t in threading.enumerate()}
    assert after - before <= {t for t in after if t.startswith(("AnyIO", "asyncio"))}, after - before
    assert not [t for t in after if "epic" in t.lower() or t.startswith(("node-hub", "pr-poller"))]
    assert board.db.kv_get("epic:acme/shop#147") is None and board.db.kv_get("node_peers") is None


def test_the_epic_modules_define_no_thread_and_no_timer():
    """The reader and the epic view start nothing. The only thread of the module is the claim's renew timer (issue #148), below the claim marker, started by
    `_ensure_renewer` and only while this board holds a claim (tests/test_nodes_claim.py pins that)."""
    full = open(nodes_epics.__file__).read()
    marker = "# ---------------------------------------------------------------- the claim (nodes epic, issue #148)"
    assert full.count(marker) == 1
    src = full.split(marker)[0] + open(github.__file__).read()
    assert "threading.Thread" not in src and "Timer(" not in src and "ThreadPool" not in src and "asyncio" not in src
    tail = full.split(marker)[1]
    assert tail.count("threading.Thread(") == 1 and "Timer(" not in tail and "ThreadPool" not in tail and "asyncio" not in tail


# ================================================================ GET only, end to end

def test_every_gh_call_of_a_full_run_is_a_get_on_an_allowed_path(board, projects_dir, monkeypatch):
    from app import doctor
    gh = board.gh
    with_projects(board, monkeypatch, [git_repo(projects_dir, "shop", "https://github.com/acme/shop.git")])
    world(gh)
    put(board, {"paused": True})
    board.c.get("/api/epics", headers=ID)
    get(board)
    doctor._epic_probe()
    calls = gh.calls()
    assert len(calls) > 10
    for c in calls:
        a = c["argv"]
        if a == ["--version"] or a == ["api", "user"]:
            continue                                                         # the version note, and the existing login probe (prpoll.gh_login: `gh api user`, a GET)
        assert a[0] == "api" and a[a.index("--method") + 1] == "GET" and not set(a) & {"-X", "-f", "-F", "--field", "--raw-field", "--input", "--paginate", "--jq"}, a
        assert github.allowed_path(a[-1]), a[-1]


# ================================================================ cost: ETag on every refresh, a budget for comments, a longer ttl for closed children

def test_a_second_refresh_after_the_ttl_revalidates_with_etags_and_costs_no_new_body(board, clock):
    gh = board.gh
    world(gh)
    get(board)
    first = len(gh.calls())
    clock.t += 61
    d = get(board).json()
    again = [c["argv"] for c in gh.calls()[first:] if c["argv"][-1] != "user"]
    assert d["error"] is None and len(d["children"]) == 7
    assert any(a[-1].endswith("/issues/147") for a in again) and any("sub_issues" in a[-1] for a in again)
    assert all(any(x.startswith("If-None-Match: W/") for x in a) for a in again), "every refresh carries the ETag it was given"


def test_comments_of_an_open_child_are_revalidated_in_every_refresh_and_a_closed_childs_every_10_minutes(board, clock):
    gh = board.gh
    world(gh)
    get(board)
    n0 = len(gh.paths())
    clock.t += 31
    get(board)
    new = gh.paths()[n0:]
    assert any("/202/comments" in p for p in new) and not any("/201/comments" in p for p in new), "open: revalidated in every refresh; closed: not before 10 min"
    clock.t += 700
    get(board)
    assert any("/201/comments" in p for p in gh.paths()[n0 + len(new):])


def test_a_child_with_no_comments_is_not_asked_for_them(board):
    gh = board.gh
    world(gh)
    get(board)
    assert not any("/205/comments" in p for p in gh.paths()), "205 has no comment: the list says so, no request"


def test_the_comment_budget_per_refresh_leaves_the_rest_for_the_next_one(board, monkeypatch, clock):
    gh = board.gh
    world(gh)
    monkeypatch.setattr(nodes_epics, "COMMENT_CALLS", 2)
    d = get(board).json()
    assert d["partial"] is True and d["comments_pending"] > 0
    assert len([p for p in gh.paths() if "/comments" in p]) == 1 and len([p for p in gh.paths() if p.endswith("/issues/202")]) == 1, "one child: its page and the issue read after it"
    clock.t += 1
    monkeypatch.setattr(nodes_epics, "COMMENT_CALLS", 40)
    d3 = get(board).json()
    assert d3["partial"] is False and d3["comments_pending"] == 0 and child(d3, 206)["claim"] is not None
    assert len([p for p in gh.paths() if "/comments" in p]) == 1 + 6, "every child's page is revalidated in each refresh (a 304 when nothing moved)"


def test_a_rate_limit_in_the_middle_of_the_comments_keeps_what_was_read_and_says_so(board, clock):
    gh = board.gh
    world(gh)
    gh.fail("/203/comments", 429, headers={"Retry-After": "40"})
    d = get(board).json()
    assert d["error"] == {"reason": "rate_limited", "message": "GitHub asked us to wait 40 s", "wait_s": 40} and d["partial"] is True
    assert len(d["children"]) == 7 and child(d, 202)["claim"]["alias"] == "node-a", "open children are read in order: 202 came before the limit"


# ================================================================ the doctor row

def probe_reset():
    from app import doctor
    doctor._probes.pop("epic-gh", None)
    return doctor


def test_the_doctor_row_reports_login_scopes_access_budget_and_the_gh_version_note(gh, projects_dir):
    from app import node_state
    doctor = probe_reset()
    node_state.slug_cache_clear()
    path = projects_dir / "shop" / "shop"
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", "https://github.com/acme/shop.git"], check=True)
    doctor.set_projects_source(lambda: [{"name": "shop", "repos": [{"name": "shop", "path": str(path), "state": "ok"}]}])
    try:
        out = doctor._c_epic_gh(None)
        assert out.status == "pass", out
        assert "scopes: read:org, repo" in out.detail and "4990 of 5000 requests left" in out.detail and "1 of 1 repositories checked" in out.detail
        assert "gh 2.99.0: gh issue create --parent is to verify" in out.detail
        assert SECRET not in out.detail and "ghp_" not in out.detail
        assert gh.paths() == ["/rate_limit", github.labeled_path("acme", "shop", "ccboard:epic", "open", 1, 1)]
        assert all(c["argv"][0] in ("--version", "api") for c in gh.calls())
    finally:
        doctor.set_projects_source(None)


def test_the_doctor_row_warns_for_a_missing_repo_scope_a_repository_it_cannot_read_and_a_wait(gh, monkeypatch):
    doctor = probe_reset()
    monkeypatch.setattr(nodes_epics, "known_repos", lambda p: ["acme/shop", "acme/api"])
    gh.add_issue("acme/shop", 1)
    gh.add_issue("acme/api", 1)
    gh.set(scopes="read:org")
    out = doctor._epic_outcome(doctor._epic_probe())
    assert out.status == "warn" and "no repo scope" in out.detail and out.fix["cmd"] == "gh auth refresh -s repo"
    gh.set(scopes="repo", forbidden=["acme/api"])
    out = doctor._epic_outcome(doctor._epic_probe())
    assert out.status == "warn" and "no read access to acme/api" in out.detail and out.fix["cmd"] == "gh auth login"
    gh.set(scopes=None, forbidden=[])
    out = doctor._epic_outcome(doctor._epic_probe())
    assert out.status == "pass" and "scopes not reported" in out.detail, "a fine-grained token reports no scopes: not a failure"
    nodes_epics.reset()
    gh.fail("/rate_limit", 429, headers={"Retry-After": "40"})
    out = doctor._epic_outcome(doctor._epic_probe())
    assert out.status == "warn" and "wait 40 s" in out.detail


def test_the_doctor_row_skips_without_gh_or_a_login(gh, monkeypatch):
    doctor = probe_reset()
    gh.set(login=None)
    out = doctor._epic_outcome(doctor._epic_probe())
    assert out.status == "skip" and "no gh login" in out.detail and out.fix["cmd"] == "gh auth login"
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    assert doctor._epic_outcome(doctor._epic_probe()).status == "skip"


def test_the_doctor_row_is_registered_in_the_box_group_and_a_slow_github_gives_the_last_answer_with_its_age(gh, monkeypatch):
    doctor = probe_reset()
    by = {c[0]: c for c in doctor.CHECKS}
    assert by["epic-gh"][1] == "box" and by["epic-gh"][2] == "Epics from GitHub"
    assert doctor._c_epic_gh(None).status == "pass"
    gate = threading.Event()
    real = doctor._epic_probe
    monkeypatch.setattr(doctor, "_epic_probe", lambda: (gate.wait(5), real())[1])
    monkeypatch.setattr(doctor, "GH_WAIT", 0.05)
    doctor._probes["epic-gh"].thread.join(2)
    out = doctor._c_epic_gh(None)
    assert out.status == "pass" and "checked" in out.detail and "still running" in out.detail
    gate.set()


# ================================================================ settings

def test_epic_authors_parse_to_lower_case_logins_and_bad_items_are_dropped():
    assert parse_epic_authors("") == ((), False) and parse_epic_authors(None) == ((), False)
    assert parse_epic_authors("Alice, bob  carol,alice") == (("alice", "bob", "carol"), False)
    assert parse_epic_authors("ok bad_login -x a--b a/b " + "z" * 40) == (("ok",), True)
    got, bad = parse_epic_authors(",".join(f"u{i}" for i in range(30)))
    assert len(got) == 20 and bad is True
    assert Settings({"CCBOARD_EPIC_AUTHORS": "Alice,bob"}).epic_authors == ("alice", "bob") and Settings({}).epic_authors == ()


def test_the_last_good_answers_kept_in_memory_are_bounded(board, monkeypatch):
    monkeypatch.setattr(nodes_epics, "GOOD_MAX", 2)
    for n in (147, 148, 149):
        board.gh.add_issue(REPO, n, f"Epic {n}", labels=["ccboard:epic"])
        get(board, n=n)
    assert sorted(k[2] for k in nodes_epics._last_good) == [148, 149], "the oldest went first"


def test_the_reader_cache_holds_a_whole_100_child_epic():
    assert github.CACHE_MAX >= nodes_epics.CHILD_CAP + 3, "the parent, its list and one comments path per child, or the oldest comments are evicted on every refresh"


def test_the_scopes_of_a_304_are_the_scopes_of_that_answer_not_of_the_merged_cache(gh):
    path = github.RATE_PATH
    assert github.reader.get(path, 0).scopes == "repo, read:org"
    gh.set(scopes=None)
    rep = github.reader.get(path, 0)
    assert rep.cached and rep.scopes is None, "the 304 carried no scopes: the old ones are not claimed for it"
    gh.set(scopes="repo")
    assert github.reader.get(path, 0).scopes == "repo" and github.reader.get(path, 60).scopes == "repo"


# ================================================================ finding 3: what is not read is unknown, never unclaimed

def comments_of(gh, n):
    return [p for p in gh.paths() if f"/{n}/comments" in p]


def test_a_real_claim_behind_150_or_250_junk_comments_is_found(board):
    gh = board.gh
    world(gh, claims=False)
    gh.junk(f"{REPO}#202", 150)
    gh.comment(f"{REPO}#202", claim_body("node-a", "running"))
    gh.junk(f"{REPO}#203", 250)
    gh.comment(f"{REPO}#203", claim_body("node-b", "claimed"))
    d = get(board).json()
    assert child(d, 202)["claim_state"] == "claimed" and child(d, 202)["claim"]["alias"] == "node-a", "151 comments are read whole"
    assert child(d, 203)["claim_state"] == "claimed" and child(d, 203)["claim"]["alias"] == "node-b", "the newest comments are read too: page 3 of 3"
    assert any("page=3" in p for p in comments_of(gh, 203)) and not any("page=4" in p for p in comments_of(gh, 203))


def test_the_newest_100_comments_are_read_wherever_they_fall_on_the_pages(board):
    gh = board.gh
    world(gh, claims=False)
    gh.junk(f"{REPO}#202", 250)                                                 # 251 + 49 = 300: the claim is comment 251, inside the last 100 (201 to 300), page 3
    gh.comment(f"{REPO}#202", claim_body("node-a", "running"))
    gh.junk(f"{REPO}#202", 49, start=START - 500)
    gh.junk(f"{REPO}#203", 159)                                                 # 160 + 91 = 251: the newest 100 are 152 to 251, across pages 2 and 3; the claim is on page 2
    gh.comment(f"{REPO}#203", claim_body("node-b", "claimed"))
    gh.junk(f"{REPO}#203", 91, start=START - 500)
    d = get(board).json()
    assert child(d, 202)["claim_state"] == "claimed" and child(d, 202)["claim"]["alias"] == "node-a"
    assert child(d, 203)["claim_state"] == "claimed" and child(d, 203)["claim"]["alias"] == "node-b"


def test_a_claim_in_the_middle_of_300_comments_is_unknown_not_unclaimed(board):
    gh = board.gh
    world(gh, claims=False)
    gh.junk(f"{REPO}#202", 150)
    gh.comment(f"{REPO}#202", claim_body("node-a", "running"))
    gh.junk(f"{REPO}#202", 149, start=START - 1000)
    d = get(board).json()
    c = child(d, 202)
    assert len(gh.world["comments"][f"{REPO}#202"]) == 300
    assert c["claim"] is None and c["claim_state"] == "unknown" and c["claim_unknown"] == {"reason": "too_long", "text": "too many comments to read"}
    assert c["bucket"] == {"node": None, "state": "unknown"}
    p = d["progress"]
    assert p["unknown"] == 1 and p["unclaimed"] == {"open": 5, "closed": 1}, "kept apart from the unclaimed ones"
    assert p["total"] == p["unknown"] + sum(p["unclaimed"].values()) + sum(v[k] for v in p["by_node"].values() for k in ("done", "running", "queued", "failed"))
    assert not any("page=2" in x for x in comments_of(gh, 202)), "100 from each end, nothing between"


def test_none_needs_every_comment_read_so_a_long_thread_without_a_claim_is_unknown(board):
    gh = board.gh
    world(gh, claims=False)
    for n, count in ((202, 100), (203, 200), (204, 201), (205, 300)):
        gh.junk(f"{REPO}#{n}", count)
    gh.junk(f"{REPO}#206", 100)
    gh.comment(f"{REPO}#206", claim_body("node-b", "claimed"), created_at=iso(START - 100), updated_at=iso(START - 100))      # comment 101 of 101
    d = get(board).json()
    got = {n: child(d, n)["claim_state"] for n in (202, 203, 204, 205, 206)}
    assert got == {202: "none", 203: "none", 204: "unknown", 205: "unknown", 206: "claimed"}, got
    assert child(d, 204)["claim_unknown"]["text"] == "too many comments to read" and child(d, 204)["bucket"]["state"] == "unknown"


def test_a_claim_found_in_a_thread_that_was_cut_counts_only_while_it_is_live(board):
    """The unread middle may hold a live claim; a stale one at an end must not make the issue look takeable."""
    gh = board.gh
    world(gh, claims=False)
    gh.junk(f"{REPO}#202", 250)
    gh.comment(f"{REPO}#202", claim_body("node-a", "claimed"), created_at=iso(START - 9000), updated_at=iso(START - 8000))        # not live
    gh.junk(f"{REPO}#203", 250)
    gh.comment(f"{REPO}#203", claim_body("node-a", "released"))
    d = get(board).json()
    assert child(d, 202)["claim_state"] == "unknown" and child(d, 202)["claim"] is None
    assert child(d, 203)["claim_state"] == "unknown", "a released claim is not live either"


def test_a_comments_count_that_disagrees_with_the_pages_is_never_a_whole_read(board):
    gh = board.gh
    world(gh, claims=False)
    for n, over in ((202, 10), (203, 500), (204, 900)):
        gh.junk(f"{REPO}#{n}", 150)
        gh.comment(f"{REPO}#{n}", claim_body("node-b", "claimed"))
        gh.world["issues"][f"{REPO}#{n}"]["raw"] = {"comments": over}
    gh.save()
    d = get(board).json()
    for n in (202, 203):
        c = child(d, n)
        assert c["claim_state"] == "unknown" and c["claim"] is None and c["claim_unknown"] == {"reason": "changed", "text": "changed while it was read"}, n
    assert child(d, 204)["claim_state"] == "unknown" and child(d, 204)["claim_unknown"]["reason"] == "too_long", "the end of the thread was not found"


def test_a_short_page_that_says_more_follows_is_not_a_whole_read(board):
    gh = board.gh
    world(gh, claims=False)
    gh.junk(f"{REPO}#202", 120)
    gh.set(page_size=50)
    d = get(board).json()
    assert child(d, 202)["claim_state"] == "unknown", "page 1 came back with 50 of 100 and a next link: nothing is concluded from it"


def test_the_comment_budget_leaves_unread_children_unknown_never_unclaimed(board, monkeypatch, clock):
    gh = board.gh
    world(gh, claims=False)
    for n in (202, 203, 204, 205):
        gh.comment(f"{REPO}#{n}", "just a note", user="bob", assoc="NONE")
    monkeypatch.setattr(nodes_epics, "COMMENT_CALLS", 2)
    d = get(board).json()
    states = {n: child(d, n)["claim_state"] for n in (202, 203, 204, 205, 206)}
    assert states == {202: "none", 203: "unknown", 204: "unknown", 205: "unknown", 206: "none"}, "206 has no comment: the list, confirmed in this refresh, says so"
    assert child(d, 204)["claim_unknown"] == {"reason": "budget", "text": "not read yet in this refresh"}
    assert d["partial"] is True and d["comments_pending"] == 3 and d["progress"]["unknown"] == 3
    assert d["progress"]["total"] == 7 and d["progress"]["unclaimed"] == {"open": 3, "closed": 1}, d["progress"]
    clock.t += 1
    monkeypatch.setattr(nodes_epics, "COMMENT_CALLS", 40)
    d = get(board).json()
    assert d["partial"] is False and d["progress"]["unknown"] == 0 and child(d, 204)["claim_state"] == "none"


def test_a_budget_that_runs_out_in_the_middle_of_a_thread_is_unknown(board, monkeypatch):
    gh = board.gh
    world(gh, claims=False)
    gh.junk(f"{REPO}#202", 150)
    gh.comment(f"{REPO}#202", claim_body("node-a", "running"))
    monkeypatch.setattr(nodes_epics, "COMMENT_CALLS", 1)
    c = child(get(board).json(), 202)
    assert c["claim_state"] == "unknown" and c["claim_unknown"]["reason"] == "budget" and c["claim"] is None
    assert len(comments_of(gh, 202)) == 1


def test_the_deadline_leaves_the_comments_unknown(board, monkeypatch):
    gh = board.gh
    world(gh)
    monkeypatch.setattr(nodes_epics, "DEADLINE", -1.0)
    d = get(board).json()
    assert comments_of(gh, 202) == [] and child(d, 202)["claim_state"] == "unknown" and child(d, 202)["claim_unknown"]["reason"] == "budget"
    assert child(d, 205)["claim_state"] == "none", "205 has no comment at all"


def test_a_back_off_in_the_middle_leaves_the_rest_unknown_with_the_reason(board):
    gh = board.gh
    world(gh)
    gh.fail("/203/comments", 429, headers={"Retry-After": "40"})
    d = get(board).json()
    assert child(d, 202)["claim_state"] == "claimed"
    for n in (203, 204, 206):
        c = child(d, n)
        assert c["claim_state"] == "unknown" and c["claim"] is None and c["claim_unknown"] == {"reason": "rate_limited", "text": "GitHub asked us to wait"}, n
    assert child(d, 205)["claim_state"] == "none" and d["progress"]["unknown"] >= 3
    assert d["progress"]["unclaimed"]["open"] == 1, "only 205, read whole by the list's count of 0"


def test_an_error_reading_one_issue_makes_that_issue_unknown_and_the_others_carry_on(board):
    gh = board.gh
    world(gh)
    gh.fail("/203/comments", 500)
    d = get(board).json()
    assert child(d, 203)["claim_state"] == "unknown" and child(d, 203)["claim_unknown"] == {"reason": "error", "text": "the comments could not be read"}
    assert child(d, 204)["claim_state"] == "claimed" and child(d, 202)["claim_state"] == "claimed" and d["partial"] is True and d["comments_pending"] == 1


def test_a_stale_issue_list_cannot_vouch_for_a_comments_count_of_zero(board, clock):
    gh = board.gh
    world(gh, claims=False)
    gh.comment(f"{REPO}#202", "a note", user="bob", assoc="NONE")
    assert child(get(board).json(), 205)["claim_state"] == "none"
    gh.comment(f"{REPO}#205", claim_body("node-b", "claimed"))                  # posted after the list was cached: the cached count for 205 is still 0
    clock.t += 61
    gh.fail("/147/sub_issues", 429, headers={"Retry-After": "60"})
    d = get(board).json()
    c = child(d, 205)
    assert c["claim_state"] == "unknown" and c["claim_unknown"]["reason"] == "rate_limited"
    assert c["bucket"]["state"] == "unknown" and d["progress"]["unknown"] >= 1


def test_an_unread_child_of_a_stale_last_good_answer_is_still_unknown(board, clock):
    gh = board.gh
    world(gh)
    gh.fail("/203/comments", 500)
    get(board)
    clock.t += 100
    gh.set(login=None)
    d = get(board).json()
    assert d["error"]["reason"] == "no_login" and d["stale_age_s"] == 100
    assert child(d, 203)["claim_state"] == "unknown", "the stale answer keeps what it knew, including what it did not know"


# ================================================================ finding 2: this board's own repositories are the only ones it asks about

def tasks_for(*refs, project="shop", repo="shop"):
    return [{"id": 100 + i, "phase": "running", "project": project, "repo": repo, "issue_ref": ref, "updated_at": "2026-10-10T10:00:00+00:00"} for i, ref in enumerate(refs)]


def test_a_hostile_node_state_costs_no_gh_call_and_attaches_nothing_it_did_not_name_correctly(board, monkeypatch):
    gh = board.gh
    world(gh)
    d0 = get(board).json()
    clean = gh.paths()
    nodes_epics.reset()
    gh.clear_calls()
    hostile = record(
        projects=[{"name": "shop", "repos": [{"name": "shop", "slug": "evil/repo"}, {"name": "lib", "slug": "other/lib"}, {"name": "x", "slug": "acme/new"}]}],
        tasks=tasks_for("#202", "evil/repo#203", "acme/shop#9999", "#999", "other/lib#9", "#0", "#01", "../x#204", "acme/shop/../x#205", "#2" + "0" * 30)
        + tasks_for("#204", project="nope", repo="shop") + tasks_for("acme/api#9", project="nope", repo="zzz"))
    monkeypatch.setattr(board.main, "_hub_records", lambda: [hostile])
    d = get(board).json()
    assert gh.paths() == clean, "the same requests as with no node at all: not one more"
    assert not any(x in " ".join(c["argv"]) for c in gh.calls() for x in ("evil", "other/lib", "acme/new"))
    assert [(c["repo"], c["number"]) for c in d["children"]] == [(c["repo"], c["number"]) for c in d0["children"]], "a node cannot make a child appear"
    for n in (202, 203, 204):
        assert child(d, n)["task"] is None, f"#{n}: the node named another repository, or none that is this board's: no join"
    assert child(d, 9, "acme/api")["task"]["node"] == "box", "the one task that names a repository of this board with the exact number is joined"
    assert [(c["state"], c["url"]) for c in d["children"]] == [(c["state"], c["url"]) for c in d0["children"]], "a node cannot change a child's state or supply a link"
    r = board.c.get("/api/epics", headers=ID).json()
    assert r["repos"] == ["acme/shop", "acme/api"] and not any("evil" in p or "other/lib" in p or "acme/new" in p for p in gh.paths())


def test_a_remote_task_with_only_a_repository_name_is_never_joined(board, monkeypatch):
    """The old join fell back to the repository NAME for a task that named no slug: a node could name its repository `shop` and land on acme/shop."""
    world(board.gh)
    recs = [record(projects=[{"name": "p", "repos": [{"name": "shop"}]}], tasks=tasks_for("#205", project="p"))]
    monkeypatch.setattr(board.main, "_hub_records", lambda: recs)
    assert child(get(board).json(), 205)["task"] is None
    make_task(board, issue_number=205, issue_url=None, repo="shop", title="this board's own task")
    assert child(get(board).json(), 205)["task"]["node"] == "local", "this board's own database may tell by name"


def test_a_route_for_a_repository_that_is_not_a_project_here_is_a_404_and_asks_github_nothing(board):
    gh = board.gh
    world(gh)
    gh.add_issue("other/lib", 5, "Third party epic", labels=["ccboard:epic"])
    r = board.c.get("/api/epics/other/lib/5", headers=ID)
    assert r.status_code == 404 and r.json()["error"] == "this board has no project for other/lib" and gh.calls() == []
    r = board.c.put("/api/epics/other/lib/5/settings", json={"paused": True}, headers=H)
    assert r.status_code == 404 and gh.calls() == [] and board.db.kv_get("epic:other/lib#5") is None
    board.repos()
    assert board.c.get("/api/epics/acme/shop/147", headers=ID).status_code == 404 and gh.calls() == []
    board.repos("Other/Lib")
    r = board.c.get("/api/epics/other/lib/5", headers=ID)
    assert r.status_code == 200 and r.json()["epic"]["repo"] == "Other/Lib", "the compare ignores case; the request uses the spelling of this board's own remote"


def test_a_sub_issue_in_another_repository_is_shown_from_the_list_and_causes_no_request(board):
    gh = board.gh
    world(gh, claims=False)
    gh.add_issue("other/lib", 7, "Their issue", body="depends on third/party#5\nDepends on: other/lib#8, evil/x#1", raw={"comments": 3})
    gh.comment("other/lib#7", claim_body("node-a", "running"))
    gh.link(f"{REPO}#147", "other/lib#7")
    d = get(board).json()
    c = child(d, 7, "other/lib")
    assert c["title"] == "Their issue" and c["url"] == "https://github.com/other/lib/issues/7"
    assert c["claim_state"] == "unknown" and c["claim"] is None and c["claim_unknown"] == {"reason": "other_repo", "text": "in another repository"}
    assert c["bucket"] == {"node": None, "state": "unknown"} and d["progress"]["unknown"] == 1
    assert c["depends_on"] == ["third/party#5", "#8", "evil/x#1"], "shown as text (#8 is in the issue's own repository)"
    assert not any(x in p for p in gh.paths() for x in ("other/lib", "third/party", "evil")), gh.paths()


def test_a_node_task_does_not_join_a_child_of_a_repository_this_board_has_no_project_for(board, monkeypatch):
    gh = board.gh
    world(gh, claims=False)
    gh.add_issue("other/lib", 7, "Their issue")
    gh.link(f"{REPO}#147", "other/lib#7")
    recs = [record(projects=[{"name": "lib", "repos": [{"name": "lib", "slug": "other/lib"}]}], tasks=tasks_for("other/lib#7", "#7", project="lib", repo="lib"))]
    monkeypatch.setattr(board.main, "_hub_records", lambda: recs)
    c = child(get(board).json(), 7, "other/lib")
    assert c["task"] is None and c["bucket"] == {"node": None, "state": "unknown"}, "the node named the repository exactly, but it is not one of this board's own"


def test_a_sub_issue_in_a_repository_of_this_board_is_read_and_a_node_cannot_move_a_pin_or_a_task_to_it_by_number(board, monkeypatch):
    gh = board.gh
    world(gh, claims=False)
    gh.add_issue("other/lib", 202, "Same number elsewhere")
    gh.link(f"{REPO}#147", "other/lib#202")
    monkeypatch.setattr(board.main, "_hub_records", lambda: [record(tasks=tasks_for("acme/shop#202"))])
    d = get(board).json()
    assert child(d, 202)["task"]["node"] == "box" and child(d, 202, "other/lib")["task"] is None
    b = add_node(board)
    assert put(board, {"pins": {"202": b["handle"]}}).status_code == 200
    d = get(board).json()
    assert child(d, 202)["pin"]["handle"] == b["handle"] and child(d, 202, "other/lib")["pin"] is None, "a pin is by issue number: it belongs to the epic's own repository"


def test_a_label_list_places_every_issue_in_the_repository_it_was_asked_of(board):
    gh = board.gh
    world(gh, claims=False)
    gh.set(sub_issues_missing=True)
    gh.add_issue(REPO, 300, "Labelled", labels=["epic:147"], raw={"repository_url": "https://api.github.com/repos/other/lib", "comments": 1})
    gh.comment(f"{REPO}#300", "note", user="bob", assoc="NONE")
    d = get(board).json()
    assert d["source"] == "labels" and child(d, 300)["url"] == "https://github.com/acme/shop/issues/300"
    assert child(d, 300)["claim_state"] == "none" and not any("other/lib" in p for p in gh.paths())


# ---- pins

def test_a_pin_names_only_an_issue_of_the_epic_this_board_has_read(board):
    gh = board.gh
    world(gh)
    assert put(board, {"pins": {"202": "local"}}).status_code == 400, "nothing read yet"
    get(board)
    assert put(board, {"pins": {"202": "local"}}).status_code == 200
    r = put(board, {"pins": {"999": "local"}})
    assert r.status_code == 400 and "not one of the epic's issues" in r.json()["error"]
    assert put(board, {"pins": {"9": "local"}}).status_code == 400, "#9 is a child in acme/api, not in the epic's repository: a pin is by number only"
    assert put(board, {"pins": {"202": None}}).status_code == 200, "removing a pin needs no read"
    assert put(board, {"pins": {"999": None}}).status_code == 200
    assert board.db.kv_get("epic:acme/shop#147") is None
    assert put(board, {"paused": True}).status_code == 200


def test_a_pin_stored_by_hand_for_a_foreign_issue_is_not_applied(board):
    world(board.gh, claims=False)
    board.gh.add_issue("other/lib", 9, "Foreign")
    board.gh.link(f"{REPO}#147", "other/lib#9")
    board.db.kv_set("epic:acme/shop#147", {"pins": {"9": "local", "205": "local"}})
    d = get(board).json()
    assert child(d, 9, "other/lib")["pin"] is None and child(d, 205)["pin"]["handle"] == "local"


# ---- the other caps

def test_an_epic_with_more_children_than_were_read_never_presents_itself_as_complete(board):
    gh = board.gh
    gh.add_issue(REPO, 147, "Big epic", labels=["ccboard:epic"])
    for i in range(1, 100):
        gh.add_issue(REPO, 1000 + i, f"Child {i}")
        gh.link(f"{REPO}#147", f"{REPO}#{1000 + i}")
    assert get(board).json()["truncated"] is False
    gh.add_issue(REPO, 1100, "The hundredth")
    gh.link(f"{REPO}#147", f"{REPO}#1100")
    nodes_epics.reset()
    d = get(board).json()
    assert len(d["children"]) == 100 and d["truncated"] is True, "a full page may have a next one: it says so rather than claim the whole"
    gh.add_issue(REPO, 1101, "One more")
    gh.link(f"{REPO}#147", f"{REPO}#1101")
    nodes_epics.reset()
    d = get(board).json()
    assert len(d["children"]) == 100 and d["truncated"] is True


def test_the_epics_list_says_when_it_shows_less_than_there_is(board, projects_dir, monkeypatch):
    gh = board.gh
    with_projects(board, monkeypatch, [git_repo(projects_dir, "shop", "https://github.com/acme/shop.git")])
    gh.add_issue(REPO, 1, "Big", labels=["ccboard:epic"])
    for i in range(130):
        gh.add_issue(REPO, 1000 + i, f"c{i}")
        gh.link(f"{REPO}#1", f"{REPO}#{1000 + i}")
    d = board.c.get("/api/epics", headers=ID).json()
    assert d["epics"][0]["children"] == {"open": 100, "closed": 0, "total": 100, "more": True} and d["truncated"] is False and d["repos_truncated"] is False
    for i in range(60):
        gh.add_issue(REPO, 3000 + i, f"epic {i}", labels=["ccboard:epic"], summary={"total": 1, "completed": 0})
    nodes_epics.reset()
    d = board.c.get("/api/epics", headers=ID).json()
    assert len(d["epics"]) == nodes_epics.EPIC_CAP and d["truncated"] is True


def test_more_repositories_than_the_list_reads_are_said_and_the_extra_ones_are_not_asked(board):
    gh = board.gh
    slugs = [f"acme/r{i:02d}" for i in range(nodes_epics.REPO_CAP + 3)]
    board.repos(*slugs)
    d = board.c.get("/api/epics", headers=ID).json()
    assert d["repos_truncated"] is True and d["repos"] == slugs[:nodes_epics.REPO_CAP]
    assert not any(slugs[-1] in p for p in gh.paths()) and len(gh.paths()) == nodes_epics.REPO_CAP
    assert board.c.get(f"/api/epics/acme/r{nodes_epics.REPO_CAP + 2:02d}/5", headers=ID).status_code == 200, "a repository past the list's cap is still one of this board's own"


def add_uncounted_epics(gh, repo: str, numbers) -> None:
    """Epics with no sub_issues_summary, so counting each one costs a gh call; each has one child."""
    for n in numbers:
        gh.add_issue(repo, n, f"epic {n}", labels=["ccboard:epic"], updated_at=f"2026-10-0{n % 9 + 1}T09:00:00Z")
        gh.add_issue(repo, n + 1000, f"child {n}")
        gh.link(f"{repo}#{n}", f"{repo}#{n + 1000}")


def test_a_refresh_of_the_epics_list_stops_at_its_call_budget_and_marks_the_epics_it_did_not_count_as_not_read_never_as_empty(board, clock, monkeypatch):
    gh = board.gh
    board.repos(REPO)
    add_uncounted_epics(gh, REPO, range(1, 7))
    monkeypatch.setattr(nodes_epics, "LIST_CALLS", 4)
    d = board.c.get("/api/epics", headers=ID).json()
    assert len(gh.paths()) <= 4, gh.paths()
    assert len(d["epics"]) == 6 and d["partial"] is True and d["deferred_counts"] == 3 and d["deferred_repos"] == [] and d["budget"] == {"calls": 4, "max": 4}
    read = [e for e in d["epics"] if e["children"] is not None]
    unread = [e for e in d["epics"] if e["children"] is None]
    assert len(read) == 3 and len(unread) == 3
    assert all(e["children_state"] == "budget" for e in unread), "marked: not read this time"
    assert all(e["children"] == {"open": 1, "closed": 0, "total": 1, "more": False} for e in read)
    assert not any("children_state" in e for e in read)
    assert all(e["children"] != {"open": 0, "closed": 0, "total": 0, "more": False} for e in d["epics"]), "an unread epic is never shown as having no issues"


def test_the_next_refresh_carries_on_where_the_budget_stopped_and_the_cache_pays_for_what_was_read(board, clock, monkeypatch):
    gh = board.gh
    board.repos(REPO)
    add_uncounted_epics(gh, REPO, range(1, 7))
    monkeypatch.setattr(nodes_epics, "LIST_CALLS", 4)
    first = board.c.get("/api/epics", headers=ID).json()
    before = len(gh.paths())
    assert board.c.get("/api/epics", headers=ID).json()["deferred_counts"] == 3 and len(gh.paths()) == before, "a stopped refresh is reused for a few seconds, not asked again at once"
    clock.t += nodes_epics.LIST_PARTIAL_TTL + 1
    second = board.c.get("/api/epics", headers=ID).json()
    assert second["partial"] is False and second["deferred_counts"] == 0 and all(e["children"] is not None for e in second["epics"])
    assert len(gh.paths()) - before <= 4
    asked_first = {e["number"] for e in first["epics"] if e["children"] is None}
    assert asked_first == {e["number"] for e in second["epics"]} - {e["number"] for e in first["epics"] if e["children"] is not None}
    done = len(gh.paths())
    clock.t += 30
    assert board.c.get("/api/epics", headers=ID).json()["partial"] is False and len(gh.paths()) == done, "a complete read is cached for the usual minute"


def test_repositories_the_budget_did_not_reach_are_named_and_go_first_next_time(board, clock, monkeypatch):
    gh = board.gh
    slugs = [f"acme/r{i}" for i in range(6)]
    board.repos(*slugs)
    for i, slug in enumerate(slugs):
        gh.add_issue(slug, 10 + i, f"epic in {slug}", labels=["ccboard:epic"], summary={"total": 2, "completed": 1})
    monkeypatch.setattr(nodes_epics, "LIST_CALLS", 3)
    d = board.c.get("/api/epics", headers=ID).json()
    assert len(gh.paths()) == 3 and d["partial"] is True and d["deferred_repos"] == slugs[3:]
    assert {e["repo"] for e in d["epics"]} == set(slugs[:3]), "the repositories not reached have no rows, and are said"
    gh.clear_calls()
    clock.t += nodes_epics.LIST_TTL + 1                                   # the three read before have gone stale, so reading them again would cost calls
    d2 = board.c.get("/api/epics", headers=ID).json()
    assert {e["repo"] for e in d2["epics"]} >= set(slugs[3:]), "the repositories left over were read first"
    assert d2["partial"] is True and d2["deferred_repos"] and set(d2["deferred_repos"]) <= set(slugs[:3])
    assert len(gh.paths()) <= 3


def test_pages_the_cache_answers_cost_nothing_so_the_budget_never_cuts_them(board, clock, monkeypatch):
    gh = board.gh
    slugs = [f"acme/r{i}" for i in range(6)]
    board.repos(*slugs)
    for i, slug in enumerate(slugs):
        gh.add_issue(slug, 10 + i, f"epic in {slug}", labels=["ccboard:epic"], summary={"total": 2, "completed": 1})
    monkeypatch.setattr(nodes_epics, "LIST_CALLS", 3)
    d = board.c.get("/api/epics", headers=ID).json()
    assert d["partial"] is True and d["deferred_repos"] == slugs[3:] and d["budget"]["calls"] == 3
    gh.clear_calls()
    clock.t += nodes_epics.LIST_PARTIAL_TTL + 1                           # the first three are still fresh in the cache (60 s)
    d2 = board.c.get("/api/epics", headers=ID).json()
    assert {e["repo"] for e in d2["epics"]} == set(slugs), "the three left over cost the budget; the three in the cache cost nothing and are read too"
    assert d2["partial"] is False and d2["deferred_repos"] == [] and len(gh.paths()) == 3 and d2["budget"]["calls"] == 3


def test_the_budget_counts_only_this_refreshs_own_calls_not_other_readers_in_the_process(board, monkeypatch):
    gh = board.gh
    board.repos(REPO)
    add_uncounted_epics(gh, REPO, range(1, 7))
    real = github.api

    def noisy(path, etag=None):
        github.reader.calls += 50                                         # other threads (a detail or comments refresh) read GitHub meanwhile
        return real(path, etag)
    monkeypatch.setattr(github.reader, "fetch", noisy)
    d = board.c.get("/api/epics", headers=ID).json()
    assert d["partial"] is False and d["deferred_counts"] == 0 and all(e["children"] is not None for e in d["epics"]), "their calls do not eat this list's 40"
    assert d["budget"] == {"calls": 7, "max": nodes_epics.LIST_CALLS}, "one page of epics and six counts, and nothing else"


def test_a_list_within_its_budget_is_not_partial_and_a_budget_is_a_named_constant(board):
    gh = board.gh
    board.repos(REPO)
    gh.add_issue(REPO, 3, "epic", labels=["ccboard:epic"], summary={"total": 2, "completed": 0})
    d = board.c.get("/api/epics", headers=ID).json()
    assert d["partial"] is False and d["deferred_repos"] == [] and d["deferred_counts"] == 0 and d["budget"]["max"] == nodes_epics.LIST_CALLS == 40 and d["budget"]["calls"] == 1
    assert d["epics"][0]["children"] == {"open": 2, "closed": 0, "total": 2, "more": False}


# ================================================================ `none` is a statement about a moment, and a thread is read as one reading

ISSUE202 = "=/repos/acme/shop/issues/202"


def calls_for(gh, n, repo="acme/shop"):
    return [p for p in gh.paths() if p.startswith(f"/repos/{repo}/issues/{n}") and (p.endswith(f"/{n}") or f"/{n}/comments" in p)]


def test_a_claim_posted_between_two_page_reads_is_found_by_the_retry(board):
    gh = board.gh
    world(gh, claims=False)
    gh.junk(f"{REPO}#202", 150)
    gh.mutate("/202/comments?per_page=100&page=2", f"{REPO}#202", "add", comment=claim_body("node-a", "running"))
    d = get(board).json()
    c = child(d, 202)
    assert c["claim_state"] == "claimed" and c["claim"]["alias"] == "node-a", "the first reading was torn (150 before, 151 after): read again"
    got = calls_for(gh, 202)
    assert got.count("/repos/acme/shop/issues/202/comments?per_page=100&page=1") == 2 and got.count("/repos/acme/shop/issues/202") == 2, got


def test_a_comment_deleted_between_page_reads_cannot_hide_a_claim(board):
    """Page 1 is read, a comment before the claim is deleted, page 2 is read: the claim moved from position 101 to 100 and is on neither page. Counting the comments
    before and after is what tells."""
    gh = board.gh
    world(gh, claims=False)
    gh.junk(f"{REPO}#202", 100)
    gh.comment(f"{REPO}#202", claim_body("node-a", "running"))                  # comment 101: the first of page 2
    gh.junk(f"{REPO}#202", 49, start=START - 500)
    gh.mutate("/202/comments?per_page=100&page=2", f"{REPO}#202", "delete", index=0)
    c = child(get(board).json(), 202)
    assert c["claim_state"] == "claimed" and c["claim"]["alias"] == "node-a"


def test_a_count_that_changes_between_the_before_and_the_after_read_makes_a_retry_that_can_succeed(board):
    gh = board.gh
    world(gh, claims=False)
    gh.junk(f"{REPO}#202", 5)
    gh.mutate(ISSUE202, f"{REPO}#202", "add")
    c = child(get(board).json(), 202)
    assert c["claim_state"] == "none", "the second reading is whole and steady"
    got = calls_for(gh, 202)
    assert got.count("/repos/acme/shop/issues/202") == 2 and got.count("/repos/acme/shop/issues/202/comments?per_page=100&page=1") == 2, "one retry, no more"


def test_a_thread_that_moves_again_in_the_retry_is_unknown_changed_while_it_was_read_and_never_loops(board):
    gh = board.gh
    world(gh, claims=False)
    gh.junk(f"{REPO}#202", 5)
    gh.comment(f"{REPO}#202", claim_body("node-a", "running"))
    gh.mutate(ISSUE202, f"{REPO}#202", "add", times=5)
    d = get(board).json()
    c = child(d, 202)
    assert c["claim_state"] == "unknown" and c["claim"] is None and c["claim_unknown"] == {"reason": "changed", "text": "changed while it was read"}
    assert c["bucket"] == {"node": None, "state": "unknown"} and d["progress"]["unknown"] == 1
    got = calls_for(gh, 202)
    assert got.count("/repos/acme/shop/issues/202") == 2 and got.count("/repos/acme/shop/issues/202/comments?per_page=100&page=1") == 2, "two readings, then it stops"


def test_a_live_claim_in_a_thread_longer_than_the_cap_must_come_from_a_steady_reading(board):
    gh = board.gh
    world(gh, claims=False)
    gh.junk(f"{REPO}#202", 250)
    gh.comment(f"{REPO}#202", claim_body("node-a", "running"))
    gh.mutate(ISSUE202, f"{REPO}#202", "add", times=5)
    c = child(get(board).json(), 202)
    assert c["claim_state"] == "unknown" and c["claim_unknown"]["reason"] == "changed", "the claim is in the newest 100, but the thread moved while it was read"
    nodes_epics.reset()
    gh.clear_mutations()
    assert child(get(board).json(), 202)["claim_state"] == "claimed"


def test_none_is_not_kept_from_an_earlier_refresh_a_cached_page_is_unknown_not_read_recently(board, clock):
    """A closed child's comments are kept 10 minutes. What the cache holds is shown (a claim in it, with its age) but never as free."""
    gh = board.gh
    world(gh, claims=False)
    gh.comment(f"{REPO}#201", "a note", user="bob", assoc="NONE")
    gh.comment(f"{REPO}#202", "a note", user="bob", assoc="NONE")
    d = get(board).json()
    assert child(d, 201)["claim_state"] == "none" and child(d, 202)["claim_state"] == "none"
    clock.t += 10
    gh.clear_calls()
    d = get(board).json()
    assert child(d, 201)["claim_state"] == "unknown" and child(d, 201)["claim_unknown"] == {"reason": "not_recent", "text": "not read recently"}
    assert not any("/201/comments" in p for p in gh.paths()), "from the cache: no call"
    assert child(d, 202)["claim_state"] == "none"
    again = [c["argv"] for c in gh.calls() if c["argv"][-1].startswith("/repos/acme/shop/issues/202")]
    assert again and all(any(x.startswith("If-None-Match: ") for x in a) for a in again), "an open child's pages and issue are revalidated with their ETags, and a 304 keeps none"
    clock.t += 700
    d = get(board).json()
    assert child(d, 201)["claim_state"] == "none", "revalidated (a 304) after the ten minutes"


def test_a_child_that_was_reopened_is_never_none_from_its_closed_days(board, clock):
    gh = board.gh
    world(gh, claims=False)
    gh.comment(f"{REPO}#201", "a note", user="bob", assoc="NONE")
    assert child(get(board).json(), 201)["claim_state"] == "none"
    clock.t += 10
    gh.world["issues"][f"{REPO}#201"]["state"] = "open"
    gh.save()
    gh.comment(f"{REPO}#201", claim_body("node-b", "claimed"))
    c = child(get(board).json(), 201)
    assert c["state"] == "open" and c["claim_state"] == "claimed" and c["claim"]["alias"] == "node-b", "open now: its comments are revalidated, not the ten-minute cache"


def test_a_back_off_never_yields_none(board, clock):
    gh = board.gh
    world(gh, claims=False)
    gh.comment(f"{REPO}#202", "a note", user="bob", assoc="NONE")
    d = get(board).json()
    assert child(d, 202)["claim_state"] == "none" and child(d, 205)["claim_state"] == "none"
    clock.t += 61
    gh.fail("/147/sub_issues", 429, headers={"Retry-After": "60"})
    d = get(board).json()
    for c in d["children"]:
        assert c["claim_state"] != "none", (c["number"], c["claim_state"])
    assert child(d, 202)["claim_unknown"]["reason"] in ("not_recent", "rate_limited") and child(d, 205)["claim_unknown"]["reason"] in ("not_recent", "rate_limited")


def test_the_last_good_answer_never_yields_none_and_an_old_live_claim_ages_out(board, clock):
    gh = board.gh
    world(gh)
    gh.comment(f"{REPO}#205", "a note", user="bob", assoc="NONE")
    d = get(board).json()
    assert child(d, 202)["claim"]["live"] is True and child(d, 205)["claim_state"] == "none"
    clock.t += 2400
    gh.set(login=None)
    d = get(board).json()
    assert d["error"]["reason"] == "no_login" and d["stale_age_s"] == 2400
    assert child(d, 205)["claim_state"] == "unknown" and child(d, 205)["claim_unknown"]["reason"] == "not_recent"
    assert child(d, 202)["claim_state"] == "claimed" and child(d, 202)["claim"]["live"] is False, "the lease (30 min) ran out by the time that has passed since the read"
    assert all(c["claim_state"] != "none" for c in d["children"])


def test_a_cached_claim_is_shown_with_the_time_it_was_read_and_is_not_live_past_its_lease(board, clock):
    gh = board.gh
    world(gh, claims=False)
    gh.comment(f"{REPO}#201", claim_body("node-a", "claimed"), updated_at=iso(START - 60))      # a closed child: its comments are cached for ten minutes
    d = get(board).json()
    c = child(d, 201)
    assert c["claims_read_at"] == iso(START) and c["claim"]["live"] is True
    gh.tick(2000)
    clock.t += 10
    d = get(board).json()
    c = child(d, 201)
    assert c["claims_read_at"] == iso(START), "the cache is shown with the time GitHub's clock had when it was read"
    assert d["server_now"] == iso(START + 2000) and c["claim"]["live"] is False, "judged by GitHub's clock now, not the clock of the reading"
    assert child(d, 202)["claims_read_at"] == iso(START + 2000), "an open child is revalidated in each refresh"


def test_the_pages_of_a_thread_must_be_one_reading_the_checks():
    ok = {"flat": [{"id": 1}, {"id": 2}], "complete": True, "end": 2, "ids_ok": True, "last_next": False}
    b = {"count": 2, "updated": "2026-10-01T09:00:00Z", "fresh": True}
    assert nodes_epics._consistent(ok, b, dict(b)) is True
    assert nodes_epics._consistent(ok, b, dict(b, count=3)) is False, "the count moved"
    assert nodes_epics._consistent(ok, dict(b, count=3), dict(b, count=3)) is False, "the count is not what the pages show"
    assert nodes_epics._consistent(ok, b, dict(b, updated="2026-10-01T09:00:01Z")) is False, "updated_at moved"
    assert nodes_epics._consistent(ok, b, dict(b, count=None)) is False and nodes_epics._consistent(ok, dict(b, count=None), b) is False
    assert nodes_epics._consistent(dict(ok, ids_ok=False), b, dict(b)) is False, "ids that repeat or go back"
    assert nodes_epics._consistent(dict(ok, last_next=True), b, dict(b)) is False, "a next link beyond the last page read"
    assert nodes_epics._consistent(dict(ok, flat=[{"id": 1}]), b, dict(b)) is False, "fewer comments in hand than the thread has"
    cut = dict(ok, complete=False, flat=[{"id": 1}], end=2)
    assert nodes_epics._consistent(cut, b, dict(b)) is True, "a longer thread is not held to having every comment, only to a steady count"


def test_the_ids_of_a_thread_must_strictly_increase(board):
    gh = board.gh
    world(gh, claims=False)
    gh.comment(f"{REPO}#202", "a", cid=50)
    gh.comment(f"{REPO}#202", "b", cid=40)
    c = child(get(board).json(), 202)
    assert c["claim_state"] == "unknown" and c["claim_unknown"]["reason"] == "changed", "an id that goes back is a reading that cannot be trusted"
    nodes_epics.reset()
    gh.world["comments"][f"{REPO}#202"] = [{**x, "id": i} for x, i in zip(gh.world["comments"][f"{REPO}#202"], (7, 7))]
    gh.save()
    assert child(get(board).json(), 202)["claim_state"] == "unknown", "the same id twice"


def test_the_extra_reads_cost_one_issue_read_per_child_and_stay_inside_the_budget(board, monkeypatch):
    gh = board.gh
    world(gh, claims=False)
    gh.comment(f"{REPO}#202", "a note", user="bob", assoc="NONE")
    get(board)
    assert calls_for(gh, 202) == ["/repos/acme/shop/issues/202/comments?per_page=100&page=1", "/repos/acme/shop/issues/202"], "one page, then the issue again: the list was the 'before'"
    assert calls_for(gh, 205) == [], "no comment: no call (the list, confirmed in this refresh, says so)"
    nodes_epics.reset()
    gh.clear_calls()
    monkeypatch.setattr(nodes_epics, "COMMENT_CALLS", 3)
    gh.comment(f"{REPO}#203", "a note", user="bob", assoc="NONE")
    gh.comment(f"{REPO}#204", "a note", user="bob", assoc="NONE")
    d = get(board).json()
    assert len([p for p in gh.paths() if "/comments" in p or p.endswith(("/202", "/203", "/204"))]) <= 4, "the budget of 3 stops a child at its next call"
    assert d["partial"] is True and child(d, 204)["claim_state"] == "unknown"


# ---- fresh_claim_state: the one answer a later phase may act on

def fresh(board, n, repo="acme/shop"):
    o, _, r = repo.partition("/")
    return nodes_epics.fresh_claim_state(o, r, n, ["acme/shop", "acme/api"])


def test_fresh_claim_state_says_claimed_none_or_unknown_and_ignores_every_cache(board, clock):
    gh = board.gh
    world(gh, claims=False)
    gh.comment(f"{REPO}#201", "a note", user="bob", assoc="NONE")
    gh.comment(f"{REPO}#202", claim_body("node-a", "running"))
    gh.junk(f"{REPO}#204", 300)
    d = get(board).json()
    assert child(d, 205)["claim_state"] == "none" and child(d, 201)["claim_state"] == "none"
    r = fresh(board, 202)
    assert r["state"] == "claimed" and r["claim"]["alias"] == "node-a" and r["reason"] is None and r["read_at"] == iso(START) and r["issue_state"] == "open"
    gh.clear_calls()
    r = fresh(board, 205)
    assert r["state"] == "none" and gh.paths() == ["/repos/acme/shop/issues/205"], "no comment: the issue read alone"
    r = fresh(board, 204)
    assert r["state"] == "unknown" and r["reason"] == "too_long" and r["text"] == "too many comments to read"
    # a claim posted after the board's last reading, on a child whose comments the board keeps for ten minutes: the board's answer is behind, this one is not
    gh.comment(f"{REPO}#201", claim_body("node-b", "claimed"))
    clock.t += 10
    shown = child(get(board).json(), 201)
    assert shown["claim_state"] != "none" and shown["claim"] is None, "the board still shows what its cache holds, and not as free"
    r = fresh(board, 201)
    assert r["state"] == "claimed" and r["claim"]["alias"] == "node-b" and r["issue_state"] == "closed"
    gh.comment(f"{REPO}#205", claim_body("node-c", "claimed"))
    assert fresh(board, 205)["state"] == "claimed", "the list had said 0 comments a moment ago"


def test_fresh_claim_state_asks_nothing_for_a_repository_or_a_login_it_cannot_vouch_for(board, monkeypatch):
    gh = board.gh
    world(gh)
    assert nodes_epics.fresh_claim_state("other", "lib", 5, ["acme/shop"])["reason"] == "other_repo"
    assert nodes_epics.fresh_claim_state("acme", "shop", "0", ["acme/shop"])["reason"] == "other_repo"
    assert nodes_epics.fresh_claim_state("-x", "shop", 5, ["acme/shop"])["state"] == "unknown"
    monkeypatch.setattr(nodes_epics, "_gh_login", lambda: None)
    r = nodes_epics.fresh_claim_state("acme", "shop", 202, ["acme/shop"])
    assert r["state"] == "unknown" and r["reason"] == "authors_unknown" and gh.calls() == []


def test_fresh_claim_state_is_unknown_during_a_back_off_and_when_the_budget_is_spent(board, monkeypatch):
    gh = board.gh
    world(gh)
    get(board)
    gh.fail("/issues/202", 429, headers={"Retry-After": "60"})
    r = fresh(board, 202)
    assert r["state"] == "unknown" and r["reason"] == "not_recent", "a stale entry is not a reading"
    nodes_epics.reset()
    monkeypatch.setattr(nodes_epics, "COMMENT_CALLS", 1)
    r = fresh(board, 203)
    assert r["state"] == "unknown" and r["reason"] == "budget"


def test_in_a_long_thread_a_live_claim_counts_only_when_the_count_agrees_with_the_pages(board):
    gh = board.gh
    world(gh, claims=False)
    gh.junk(f"{REPO}#202", 300)
    gh.comment(f"{REPO}#202", claim_body("node-a", "running"))                  # comment 301
    assert child(get(board).json(), 202)["claim_state"] == "claimed"
    nodes_epics.reset()
    gh.world["issues"][f"{REPO}#202"]["raw"] = {"comments": 350}                  # the issue says 350 (before and after alike); the pages end at 301
    gh.save()
    c = child(get(board).json(), 202)
    assert c["claim_state"] == "unknown" and c["claim_unknown"]["reason"] == "changed" and c["claim"] is None
