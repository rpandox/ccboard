"""Team P4, issue #200: one place to see the whole team. The cross-repository view (`overview`), the free issues, the change detection behind "a teammate started on an issue I hold"
and the demo contract.

app/team.py takes plain data and has no network, so most tests hand it hand-made reads, including hostile ones. The last tests run the real poller against the in-process fake GitHub of
tests/fake_github.py (alice is the board's owner, bob a teammate) on a virtual clock, with the notification senders replaced by recorders. The notification kind itself (off by default,
the toggle, the collapse) is pinned in tests/test_notify.py. No test reaches the real gh, GitHub, claude, codex, tmux or the real home; every test starts from empty module memory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app import notify, team
from app.config import settings
from tests.fake_github import iso
from tests.test_team import ALICE, BOB, CAROL, NOW, RECENT, block, entry, issue, read
from tests.test_team_conflicts import PR7, pline, presence, task
from tests.test_team_github import H, ID, KEY, REPO, seed, tw  # noqa: F401 (tw is a fixture)

DAVE = {"id": 4, "login": "dave"}
ERIN = {"id": 5, "login": "erin"}
FRANK = {"id": 6, "login": "frank"}
GRACE = {"id": 7, "login": "grace"}
TOOLS = "github.com/acme/tools"
DEMO = Path(__file__).resolve().parents[1] / "app" / "static" / "demo"

ROW_KEYS = {"person", "login", "you", "repo_slug", "slug", "repo_url", "read_at", "stale", "kind", "issue", "issue_url", "title", "branch", "branch_url", "pr", "pr_url", "draft", "agent",
            "state", "live", "since", "last", "last_seen_s", "claim", "collides"}
PERSON_KEYS = {"key", "id", "login", "you", "live", "last_seen_s", "repos", "rows"}
COUNT_KEYS = {"shared", "ok", "loading", "problem", "teammates", "live", "active_repos", "rows", "free", "truncated"}
FREE_KEYS = {"state", "reason", "message", "held", "issues", "count", "truncated"}


@pytest.fixture(autouse=True)
def fresh_memory():
    team.watch.forget()
    notify._team_told.clear()
    yield
    team.watch.forget()
    notify._team_told.clear()


def tools(**kw):
    return entry(key=TOOLS, slug="acme/tools", local=[{"project": "ops", "repo": "tools"}], **kw)


def free_of_read(rd, me=("alice", 1), local=None, now=NOW):
    return team.repo_state(entry(), rd, me[0], me[1], now, local)["free"]


def rows_of(body, login=None, slug=None):
    return [r for r in body["overview"]["rows"] if (login is None or r["login"] == login) and (slug is None or r["slug"] == slug)]


# ================================================================ hidden when nothing is shared

def test_with_nothing_shared_the_overview_is_empty_and_the_prefs_are_the_old_five(tw):
    body = team.build([entry(shared=False), tools(shared=False)], {}, "alice", 1, NOW)
    ov = body["overview"]
    assert body["enabled"] is False and set(ov) == {"api", "counts", "people", "rows"} and ov["people"] == [] and ov["rows"] == []
    assert set(ov["counts"]) == COUNT_KEYS and ov["counts"]["shared"] == 0 and ov["counts"]["teammates"] == 0 and ov["counts"]["live"] == 0
    assert ov["counts"]["free"] == {"count": 0, "ok": 0, "unread": 0, "unknown": 0}
    assert all(r["free"] is None and r["warnings"] is None for r in body["repos"])
    r = tw.c.get("/api/team/state", headers=ID)
    assert r.status_code == 200 and r.json()["overview"]["counts"]["shared"] == 0 and r.json()["overview"]["rows"] == []
    assert tw.gh.calls() == [] and tw.poller.thread is None and tw.poller.reads == {}, "asking for the view asks GitHub for nothing and starts no poll"
    assert "team" not in tw.c.get("/api/state", headers=ID).json(), "no new /api/state key while nothing is shared"
    prefs = tw.c.get("/api/notify/prefs", headers=ID).json()
    assert set(prefs["prefs"]) == {"needs", "done", "limit", "error", "login"} and set(prefs["samples"]) == set(prefs["prefs"]), "the team toggle is not listed while nothing is shared"
    assert notify.team_enabled() is False and notify.kind_enabled("team_started") is False


def test_the_team_toggle_is_listed_off_once_a_repository_is_shared(tw):
    tw.share()
    got = tw.c.get("/api/notify/prefs", headers=ID).json()
    assert got["prefs"]["team"] is False and got["prefs"]["needs"] is True and "team" in got["samples"], "listed, and off until the person switches it on"
    assert got["samples"]["team"]["kind"] == "team_started" and got["samples"]["team"]["title"].endswith("bob started on #12")
    put = tw.c.put("/api/notify/prefs", headers=H, json={"team": True}).json()
    assert put["prefs"]["team"] is True and put["prefs"]["done"] is True
    assert tw.c.put("/api/notify/prefs", headers=H, json={"team": "maybe"}).status_code == 422
    assert tw.c.put("/api/notify/prefs", headers=H, json={"done": False}).json()["prefs"]["team"] is True, "another toggle leaves the team toggle alone"


def test_only_a_signed_in_person_switches_the_team_notice_on(tw):
    from types import SimpleNamespace
    from app import hooks, projects
    tw.share()
    token = {"X-CCBoard-Token": hooks.ensure_token()}
    refused = tw.c.put("/api/notify/prefs", headers=token, json={"team": True})
    assert refused.status_code == 403 and "signed-in person" in refused.json()["error"]
    assert tw.c.get("/api/notify/prefs", headers=ID).json()["prefs"]["team"] is False, "the notice is still off"
    assert tw.c.put("/api/notify/prefs", headers=token, json={"done": False}).status_code == 200, "the other toggles keep their behaviour"
    assert tw.c.put("/api/notify/prefs", headers=token, json={"team": False}).status_code == 200, "turning it off is anybody's"
    for who in (None, "", "hub", "local-token", "node:node-b", "mcp:laptop"):
        with pytest.raises(projects.Forbidden):
            tw.main.api_notify_prefs_set(tw.main.NotifyPrefsIn(team=True), SimpleNamespace(state=SimpleNamespace(user=who)))
    assert tw.c.put("/api/notify/prefs", headers=H, json={"team": True}).json()["prefs"]["team"] is True


# ================================================================ the cross-repository view

def two_repos():
    carol_login_only = {"id": None, "login": "carol"}
    shop = read(issues=[issue(1, BOB), issue(3, ALICE)], pulls=[{**PR7, "closes": [1]}], linked={1: [{"name": "bob/login", "at": RECENT, "author": BOB}]},
                tips=[{"name": "bob/login", "at": RECENT, "author": BOB}, {"name": "bob/spike", "at": RECENT, "author": BOB}],
                presence=presence(pline(BOB, 1, branch="bob/login"), pline(CAROL, 2, state="needs_you"), pline(ALICE, 3, nid="a1b2c3d5")))
    ops = read(slug="acme/tools", issues=[issue(9, {"id": None, "login": "bob"}), issue(10, carol_login_only)], tips=[{"name": "ops/x", "at": RECENT, "author": {"id": None, "login": "bob"}}])
    return [entry(), tools()], {KEY: shop, TOOLS: ops}


def test_a_person_is_one_person_across_repositories_and_the_owner_is_never_a_teammate():
    entries, reads = two_repos()
    body = team.build(entries, reads, "alice", 1, NOW)
    ov = body["overview"]
    people = {p["login"]: p for p in ov["people"]}
    assert set(people) == {"alice", "bob", "carol"} and all(set(p) == PERSON_KEYS for p in ov["people"])
    assert people["bob"]["id"] == 2 and people["bob"]["key"] == "id:2" and sorted(people["bob"]["repos"]) == ["acme/shop", "acme/tools"], "bob without an id in the second repository is the same bob"
    assert people["carol"]["key"] == "id:3" and people["carol"]["repos"] == ["acme/shop", "acme/tools"]
    assert people["alice"]["you"] is True and ov["people"][0]["login"] == "alice", "the owner is listed first"
    c = ov["counts"]
    assert c["shared"] == 2 and c["ok"] == 2 and c["problem"] == 0 and c["teammates"] == 2, "bob and carol; alice is not a teammate"
    assert c["live"] == 2 and c["active_repos"] == 1, "bob (live) and carol (needs you) are live in one repository; alice's own live line does not count"
    assert c["rows"] == len(ov["rows"]) and c["truncated"] is False
    assert {r["person"] for r in ov["rows"]} == {"id:1", "id:2", "id:3"}


def test_every_row_has_the_same_keys_and_says_repo_issue_branch_agent_state_since_and_last_seen():
    entries, reads = two_repos()
    body = team.build(entries, reads, "alice", 1, NOW)
    assert all(set(r) == ROW_KEYS for r in body["overview"]["rows"])
    live = next(r for r in rows_of(body, "bob", "acme/shop") if r["kind"] == "presence")
    assert (live["issue"], live["branch"], live["agent"], live["state"], live["live"]) == (1, "bob/login", "claude", "working", "live")
    assert live["title"] == "Issue 1" and live["since"] and live["last"] and live["last_seen_s"] == 60 and live["pr"] == 7 and live["draft"] is True
    assert live["issue_url"] == "https://github.com/acme/shop/issues/1" and live["branch_url"] == "https://github.com/acme/shop/tree/bob/login"
    assert live["repo_slug"] == KEY and live["slug"] == "acme/shop" and live["repo_url"] == "https://github.com/acme/shop" and live["read_at"].endswith("Z") and live["stale"] is False
    spike = next(r for r in rows_of(body, "bob", "acme/shop") if r["branch"] == "bob/spike")
    assert spike["kind"] == "branch" and spike["state"] == "github" and spike["agent"] is None and spike["last_seen_s"] is None and spike["last"]
    assert not [r for r in rows_of(body, "bob", "acme/shop") if r["kind"] in ("issue", "pr") and r["issue"] == 1], "the issue, the branch and the pull request of one piece of work are one row"
    carol = next(r for r in rows_of(body, "carol", "acme/shop") if r["kind"] == "presence")
    assert carol["state"] == "waiting" and carol["live"] == "live"
    assert [r["kind"] for r in rows_of(body, "carol", "acme/tools")] == ["issue"] and rows_of(body, "carol", "acme/tools")[0]["state"] == "github"


def test_every_state_has_a_row():
    people = [(BOB, pline(BOB, 1, nid="a0000001")), (CAROL, pline(CAROL, 2, state="needs_you", nid="a0000002")), (DAVE, pline(DAVE, 3, state="idle", nid="a0000003")),
              (ERIN, pline(ERIN, 4, moved=False, nid="a0000004")), (FRANK, pline(FRANK, 5, age=3000, nid="a0000005")), (GRACE, pline(GRACE, 6, state="ended", nid="a0000006"))]
    rd = read(issues=[issue(n, u) for n, (u, _) in enumerate(people, 1)] + [issue(7, {"id": 8, "login": "heidi"})], presence=presence(*[ln for _, ln in people]))
    body = team.build([entry()], {KEY: rd}, "alice", 1, NOW)
    states = {r["login"]: r["state"] for r in body["overview"]["rows"]}
    assert states == {"bob": "working", "carol": "waiting", "dave": "idle", "erin": "unconfirmed", "frank": "stale", "grace": "ended", "heidi": "github"}
    gone = read(issues=[issue(1, BOB)], presence=presence(pline(BOB, 1, age=90000)))
    assert [r["state"] for r in team.build([entry()], {KEY: gone}, "alice", 1, NOW)["overview"]["rows"]] == ["github"], "a gone line is hidden, the issue GitHub shows stays"
    unknown = read(issues=[issue(1, BOB)], presence=presence(pline(BOB, 1), server_now=None))
    assert [r["state"] for r in team.build([entry()], {KEY: unknown}, "alice", 1, NOW)["overview"]["rows"]] == ["unknown"]
    assert set(states.values()) | {"unknown"} == set(team.ROW_STATES), "every state the model can say has a row in this test"


def test_an_old_or_failed_read_shows_stale_rows_and_counts_nobody_as_live():
    rd = read(issues=[issue(1, BOB)], presence=presence(pline(BOB, 1)), read_at=NOW - 2000)
    old = team.build([entry()], {KEY: rd}, "alice", 1, NOW)
    assert old["repos"][0]["stale"] is True and [r["state"] for r in old["overview"]["rows"]] == ["stale"] and old["overview"]["rows"][0]["stale"] is True
    assert old["overview"]["counts"]["live"] == 0 and old["overview"]["counts"]["active_repos"] == 0 and old["overview"]["counts"]["teammates"] == 1
    failed = team.build([entry()], {KEY: dict(read(issues=[issue(1, BOB)], presence=presence(pline(BOB, 1))), status="rate_limited", wait_s=60)}, "alice", 1, NOW)
    assert failed["overview"]["counts"]["problem"] == 1 and failed["overview"]["counts"]["live"] == 0 and failed["overview"]["counts"]["teammates"] == 1, "a refusal is not 'no teammates'"
    assert [r["state"] for r in failed["overview"]["rows"]] == ["stale"]
    loading = team.build([entry()], {}, "alice", 1, NOW)
    assert loading["overview"]["counts"]["loading"] == 1 and loading["overview"]["rows"] == []


def test_the_overview_does_not_move_the_etag_between_two_reads_of_the_same_state():
    entries, reads = two_repos()
    a = team.build(entries, reads, "alice", 1, NOW)
    b = team.build(entries, {k: dict(v) for k, v in reads.items()}, "alice", 1, NOW + 5)
    assert a["overview"] == b["overview"] and team.etag(a) == team.etag(b)
    reads[KEY]["issues"].append(issue(30, DAVE))
    assert team.etag(team.build(entries, reads, "alice", 1, NOW)) != team.etag(a)


def test_hostile_text_in_the_overview_is_capped_plain_text_and_links_are_built_not_copied():
    evil = "Fix\x07 <img src=x onerror=alert(1)>‮​ " + "A" * 5000
    bad_login = {"id": 9, "login": "x" * 500}
    rd = read(issues=[issue(1, BOB, title=evil, labels=["l\x1b[31m" + "z" * 500])], pulls=[{**PR7, "title": evil, "head_ref": "evil\x00/<b>" + "c" * 400}], tips=[{"name": "../../etc/passwd; rm", "at": RECENT, "author": BOB}])
    rd["issues"].append(issue(2, bad_login))
    rd["presence"] = presence(pline(BOB, 1, branch="bob/login"))
    body = team.build([entry()], {KEY: rd}, "alice", 1, NOW)
    dumped = json.dumps(body["overview"])
    for bad in ("\\u202e", "\\u200b", "\\u0007", "\\u001b", "\\u0000"):
        assert bad not in dumped, bad
    for r in body["overview"]["rows"]:
        assert r["title"] is None or len(r["title"]) <= team.TEXT_MAX
        assert r["login"] is None or team.LOGIN_RE.fullmatch(r["login"]), r["login"]
        assert r["branch"] is None or len(r["branch"]) <= team.BRANCH_MAX
        for u in (r["issue_url"], r["pr_url"], r["branch_url"], r["repo_url"]):
            assert u is None or (u.startswith("https://github.com/acme/shop") and " " not in u and ";" not in u and ".." not in u), u
    assert not [r for r in body["overview"]["rows"] if r["branch"] and ("passwd" in r["branch"] or "\x00" in r["branch"]) and r["branch_url"]], "an odd branch name is shown as text, never linked"


# ================================================================ free issues: nobody holds them, and unknown is never free

def open_issue(n, **kw):
    return {"number": n, "title": f"Open {n}", "labels": [], "assignees": [], "updated_at": RECENT, **kw}


def test_free_issues_are_the_open_ones_nobody_holds_by_the_p3_rules():
    rd = read(issues=[issue(1, BOB), issue(3, ALICE)],
              pulls=[{**PR7, "number": 7, "closes": [5]}, {**PR7, "number": 8, "author": {"id": 9, "login": "mallory"}, "association": "NONE", "closes": [6]}],
              linked={7: [{"name": "carol/seven", "at": RECENT, "author": CAROL}]}, claims=[{"issue": 8, "user": DAVE, "status": "running", "branch": None, "live": True, "updated_at": RECENT}],
              presence=presence(pline(ERIN, 9, nid="a0000004"), pline(FRANK, 10, age=3000, nid="a0000005"), pline(GRACE, 11, age=90000, nid="a0000006")),
              open_issues=[open_issue(n) for n in range(1, 16)] + [open_issue(20, assignees=[BOB]), open_issue(21, labels=[team.PRESENCE_LABEL]), {"number": 22, "pull_request": {}},
                                                                    {"number": "x"}, "junk", open_issue(23, title="<b>" + "T" * 900)])
    f = free_of_read(rd, local=[task(1, issue_number=2)])
    assert set(f) == FREE_KEYS and f["state"] == "ok" and f["reason"] is None and f["message"] == ""
    free = {i["number"] for i in f["issues"]}
    # 1 bob assigned, 3 alice assigned, 5 bob's trusted pull request, 7 carol's linked branch, 8 dave's live claim, 9 erin's live line, 10 frank's stale line (somebody touched it lately), 2 the owner's own task
    assert free == {4, 6, 11, 12, 13, 14, 15, 23}, "6 (a pull request of somebody who may not write) and 11 (a gone line) hold nothing; 20 is assigned, 21 and 22 are not work"
    assert f["held"] == [1, 2, 3, 5, 7, 8, 9, 10] and f["count"] == 8 and f["truncated"] is False
    one = next(i for i in f["issues"] if i["number"] == 23)
    assert set(one) == {"number", "title", "url", "labels", "updated_at"} and len(one["title"]) <= team.TEXT_MAX and one["url"] == "https://github.com/acme/shop/issues/23"


def test_free_issues_are_capped_and_say_so():
    rd = read(open_issues=[open_issue(n) for n in range(1, 120)])
    f = free_of_read(rd)
    assert f["state"] == "ok" and len(f["issues"]) == team.FREE_MAX and f["count"] == 119 and f["truncated"] is True


def test_an_unassigned_issue_with_a_linked_branch_and_no_status_line_is_not_free_whoever_linked_it():
    """Issue #200: linked branches used to be read only for assigned issues, so an unassigned issue with a linked branch and no status line showed as free."""
    rd = read(linked={4: [{"name": "carol/four", "at": RECENT, "author": CAROL}], 5: [{"name": "mystery/five", "at": RECENT, "author": None}],
                      6: [{"name": "alice/six", "at": RECENT, "author": ALICE}]},
              open_issues=[open_issue(n) for n in range(1, 8)])
    f = free_of_read(rd)
    assert [i["number"] for i in f["issues"]] == [1, 2, 3, 7], "4 (a teammate's branch), 5 (a branch of nobody known) and 6 (the owner's own) are taken"
    assert f["held"] == [4, 5, 6] and f["count"] == 4 and f["state"] == "ok"
    body = team.build([entry()], {KEY: rd}, "alice", 1, NOW)
    w = body["repos"][0]["warnings"]["issues"]
    assert "4" in w and w["4"][0]["person"]["login"] == "carol" and "5" not in w, "a branch with a known author is a warning that names her; one with none still holds the issue"
    assert any(r["login"] == "carol" and r["kind"] == "branch" and r["issue"] == 4 for r in body["overview"]["rows"]), "the team view shows it as carol's branch"


def test_a_full_page_of_unassigned_issues_in_the_link_read_lists_only_the_ones_it_covered():
    open_ = [open_issue(n) for n in range(1, 6)]
    full = free_of_read(read(open_issues=open_, open_cover={"numbers": [1, 2, 3], "full": True}))
    assert [i["number"] for i in full["issues"]] == [1, 2, 3] and full["truncated"] is True, "4 and 5 may have a linked branch the read never reached: not free"
    part = free_of_read(read(open_issues=open_, open_cover={"numbers": [1, 2, 3], "full": False}))
    assert [i["number"] for i in part["issues"]] == [1, 2, 3, 4, 5] and part["truncated"] is False, "a page that was not full saw every unassigned issue"
    old = free_of_read(read(open_issues=open_))
    assert [i["number"] for i in old["issues"]] == [1, 2, 3, 4, 5], "a read with no cover (made before this check) is judged as before"


def test_a_read_with_no_list_of_open_issues_yet_makes_free_unread_with_the_held_numbers():
    f = free_of_read(read(issues=[issue(1, BOB), issue(3, ALICE)]))
    assert f["state"] == "unread" and f["issues"] == [] and f["count"] == 0 and f["held"] == [1, 3] and f["reason"] == "unread" and "have not been read yet" in f["message"]


def test_a_full_page_of_open_issues_is_listed_and_says_it_may_be_longer():
    f = free_of_read(read(open_issues=[open_issue(n) for n in range(1, 4)], open_truncated=True))
    assert f["state"] == "ok" and f["count"] == 3 and f["truncated"] is True and len(f["issues"]) == 3 and f["message"] == ""
    f = free_of_read(read(open_issues=[open_issue(n) for n in range(1, 4)], open_truncated=False))
    assert f["state"] == "ok" and f["truncated"] is False
    f = free_of_read(read(open_issues=[open_issue(n) for n in range(1, 4)]))
    assert f["truncated"] is False, "a read from before the flag existed is not cut"
    body = team.build([entry()], {KEY: read(open_issues=[open_issue(1)], open_truncated=True)}, "alice", 1, NOW)
    assert body["repos"][0]["truncated"] == {"issues": False, "pulls": False, "tips": False}, "the repository's own cut flags are not the list of open issues' flag"
    assert body["overview"]["counts"]["free"] == {"count": 1, "ok": 1, "unread": 0, "unknown": 0}


def test_an_old_list_of_open_issues_is_unknown_never_free():
    old = read(open_issues=[open_issue(1)], open_at=NOW - team.OPEN_OLD_S - 1)
    f = free_of_read(old)
    assert f["state"] == "unknown" and f["reason"] == "open_old" and f["issues"] == [] and f["count"] == 0 and "old" in f["message"]
    fresh = free_of_read(read(open_issues=[open_issue(1)], open_at=NOW - team.OPEN_OLD_S + 60))
    assert fresh["state"] == "ok" and [i["number"] for i in fresh["issues"]] == [1]
    assert team.build([entry()], {KEY: old}, "alice", 1, NOW)["overview"]["counts"]["free"]["unknown"] == 1


@pytest.mark.parametrize("name,rd,me,reason", [
    ("rate", lambda: read(open_issues=[open_issue(1)], status="rate_limited", wait_s=60), ("alice", 1), "rate_limited"),
    ("sso", lambda: read(open_issues=[open_issue(1)], status="sso"), ("alice", 1), "sso"),
    ("no_access", lambda: read(open_issues=[open_issue(1)], status="no_access"), ("alice", 1), "no_access"),
    ("old", lambda: read(open_issues=[open_issue(1)], read_at=NOW - 5000), ("alice", 1), "stale"),
    ("no_login", lambda: read(open_issues=[open_issue(1)]), (None, None), "identity"),
    ("graph_pending", lambda: read(open_issues=[open_issue(1)], gql_at=None), ("alice", 1), "graph_pending"),
    ("graph_failed", lambda: read(open_issues=[open_issue(1)], graph_error="rate_limited"), ("alice", 1), "graph_failed"),
    ("presence_error", lambda: read(open_issues=[open_issue(1)], presence={"status": "error", "lines": [], "server_now": NOW, "editor_check": "none", "issue": None, "truncated": False}), ("alice", 1), "presence"),
    ("presence_unchecked", lambda: read(open_issues=[open_issue(1)], presence={**presence(), "editor_check": "failed"}), ("alice", 1), "presence"),
    ("cut", lambda: read(open_issues=[open_issue(1)], truncated={"issues": False, "pulls": True, "tips": False}), ("alice", 1), "truncated"),
])
def test_unknown_is_not_free(name, rd, me, reason):
    f = free_of_read(rd(), me=me)
    assert f["state"] == "unknown" and f["reason"] == reason and f["issues"] == [] and f["count"] == 0 and f["message"], name
    body = team.build([entry()], {KEY: rd()}, me[0], me[1], NOW)
    assert body["overview"]["counts"]["free"]["unknown"] == 1 and body["overview"]["counts"]["free"]["count"] == 0 and body["overview"]["counts"]["free"]["ok"] == 0


def test_unknown_still_says_which_issues_are_held_but_calls_nothing_free():
    rd = read(issues=[issue(1, BOB)], open_issues=[open_issue(1), open_issue(2)], status="rate_limited", wait_s=60)
    f = free_of_read(rd)
    assert f["state"] == "unknown" and f["held"] == [1] and f["issues"] == []
    assert "free" not in json.dumps({k: v for k, v in f.items() if k not in ("state", "reason", "message")}).lower()


def pull(n, **kw):
    return {**PR7, "number": n, "closes": [], **kw}


@pytest.mark.parametrize("name,rd", [
    ("presence_cut", lambda: read(open_issues=[open_issue(1)], presence={**presence(), "truncated": True})),
    ("more_pulls_than_the_window", lambda: read(open_issues=[open_issue(1)], pulls=[pull(n) for n in range(100, 100 + team.GQL_WINDOW + 1)])),
    ("a_pull_without_links", lambda: read(open_issues=[open_issue(1)], pulls=[pull(7, closes=None)])),
    ("assigned_window_full", lambda: read(open_issues=[open_issue(1)], gql_full={"issues": True, "pulls": False})),
    ("pull_window_full", lambda: read(open_issues=[open_issue(1)], gql_full={"issues": False, "pulls": True})),
])
def test_a_cut_holder_check_makes_free_unknown_and_never_lists_an_issue_somebody_may_hold(name, rd):
    f = free_of_read(rd())
    assert f["state"] == "unknown" and f["issues"] == [] and f["count"] == 0 and f["message"], name
    assert f["reason"] == ("truncated" if name == "presence_cut" else "graph_window"), name
    assert team.build([entry()], {KEY: rd()}, "alice", 1, NOW)["overview"]["counts"]["free"] == {"count": 0, "ok": 0, "unread": 0, "unknown": 1}


def test_a_full_window_of_pull_requests_with_their_links_and_a_pull_request_nobody_trusts_do_not_cut_the_list():
    ok = free_of_read(read(open_issues=[open_issue(1)], pulls=[pull(n) for n in range(100, 100 + team.GQL_WINDOW)], gql_full={"issues": False, "pulls": False}))
    assert ok["state"] == "ok" and [i["number"] for i in ok["issues"]] == [1], "30 pull requests that all have their links fit the window"
    stranger = {"id": 9, "login": "mallory"}
    ok = free_of_read(read(open_issues=[open_issue(1)], pulls=[pull(8, author=stranger, association="NONE", closes=None)]))
    assert ok["state"] == "ok", "a pull request of somebody who may not write warns nobody, so its missing links hide nothing"


def test_a_presence_line_with_no_clock_claims_its_issue_but_an_ended_one_or_a_gone_one_with_a_clock_does_not():
    lines = (pline(ERIN, 9, nid="a0000004"), pline(FRANK, 10, nid="a0000005", state="ended"), pline(GRACE, 11, age=90000, nid="a0000006"))
    rd = read(open_issues=[open_issue(n) for n in (9, 10, 11, 12)], presence={**presence(*lines), "server_now": None})
    f = free_of_read(rd)
    assert f["state"] == "ok" and {9, 11} <= set(f["held"]), "no clock from GitHub: a line may be live and even a day-old one is not known to be gone, so 9 and 11 are held"
    assert {i["number"] for i in f["issues"]} == {10, 12}, "an ended session holds nothing"
    rd = read(open_issues=[open_issue(n) for n in (9, 11)], presence=presence(pline(ERIN, 9, nid="a0000004"), pline(GRACE, 11, age=90000, nid="a0000006")))
    assert {i["number"] for i in free_of_read(rd)["issues"]} == {11}, "with a clock, a live line holds and a gone one does not"


def test_a_repository_that_is_not_shared_has_no_free_block_and_a_loading_one_is_unknown():
    assert team.build([entry(shared=False)], {}, "alice", 1, NOW)["repos"][0]["free"] is None
    loading = team.build([entry()], {}, "alice", 1, NOW)["repos"][0]["free"]
    assert loading["state"] == "unknown" and loading["reason"] == "loading" and loading["issues"] == []
    first_failed = team.build([entry()], {KEY: {"slug": REPO, "status": "sso", "read_at": None}}, "alice", 1, NOW)["repos"][0]["free"]
    assert first_failed["state"] == "unknown" and first_failed["reason"] == "sso"


def test_the_overview_counts_free_issues_across_repositories():
    a = read(open_issues=[open_issue(1), open_issue(2)], issues=[issue(2, BOB)])
    b = read(slug="acme/tools")
    body = team.build([entry(), tools()], {KEY: a, TOOLS: b}, "alice", 1, NOW)
    assert body["overview"]["counts"]["free"] == {"count": 1, "ok": 1, "unread": 1, "unknown": 0}


# ================================================================ a teammate started on an issue the owner holds: the change between two reads

SHOP = "github.com/acme/shop"
MINE = [task(1, issue_number=5)]


def lc():
    return team._local_clean(MINE)


def step(w, rd, me=("alice", 1), now=NOW, local=None):
    return w.step(SHOP, rd, me[0], me[1], "acme", "shop", now, lc() if local is None else local)


def at(n, **kw):
    return read(read_at=NOW - 30 + n * 120, **kw)


def test_the_first_read_is_a_baseline_and_a_start_after_it_is_told_once():
    w = team.Watch()
    assert step(w, at(0, issues=[issue(5, BOB)])) == [], "bob was already on issue 5: nothing is told for what was there"
    assert step(w, at(1, issues=[issue(5, BOB), issue(6, [])])) == []
    got = step(w, at(2, issues=[issue(5, BOB), issue(6, CAROL)]), local=team._local_clean([task(1, issue_number=5), task(2, issue_number=6)]))
    assert [(e["login"], e["issue"]) for e in got] == [("carol", 6)] and set(got[0]) == {"login", "issue", "person"}
    assert step(w, at(3, issues=[issue(5, BOB), issue(6, CAROL)]), local=team._local_clean([task(1, issue_number=5), task(2, issue_number=6)])) == [], "the same read is no new start"


def test_a_teammate_who_starts_on_an_issue_the_owner_holds_is_told_only_when_the_owner_holds_it():
    w = team.Watch()
    step(w, at(0))
    assert step(w, at(1, issues=[issue(9, BOB)])) == [], "issue 9 is not the owner's"
    assert [e["issue"] for e in step(w, at(2, issues=[issue(9, BOB), issue(5, CAROL)]))] == [5]
    # the owner holds an issue by assignment too, not only by a task
    w2 = team.Watch()
    step(w2, at(0, issues=[issue(7, ALICE)]), local=[])
    assert [e["issue"] for e in step(w2, at(1, issues=[issue(7, [ALICE, BOB])]), local=[])] == [7]


def test_the_same_pair_is_never_told_twice_even_when_the_teammate_leaves_and_comes_back():
    w = team.Watch()
    step(w, at(0))
    assert len(step(w, at(1, issues=[issue(5, BOB)]))) == 1
    assert step(w, at(2)) == []
    assert step(w, at(3, issues=[issue(5, BOB)])) == [], "back on the same issue: already told"


def test_the_owners_own_logins_and_boards_are_never_told():
    w = team.Watch()
    step(w, at(0))
    renamed = {"id": 1, "login": "alice-new"}
    nameless = {"id": None, "login": "ALICE"}
    assert step(w, at(1, issues=[issue(5, [renamed, nameless])], presence=presence(pline(ALICE, 5, nid="a1b2c3d9")))) == []
    pairs = {((("id", 1)), 5): "alice-old", (("login", "alice"), 5): "Alice"}
    assert team.starts_from(set(), pairs, {5}, "alice", 1) == [], "the guard itself: the owner's id and the owner's login in any case"
    assert team.starts_from(set(), {(("login", "alice"), 5): "Alice"}, {5}, "alice", None) == [], "the owner's login alone, with no id known"
    assert team.starts_from(set(), {(("id", 2), 5): "bob"}, {5}, "alice", 1)[0]["login"] == "bob", "the guard lets a teammate through"


def test_nothing_is_told_without_a_known_owner_and_the_baseline_waits_for_it():
    w = team.Watch()
    assert step(w, at(0), me=(None, None)) == []
    assert step(w, at(1, issues=[issue(5, BOB)]), me=(None, None)) == [], "an unknown owner cannot tell its own work from a teammate's"
    assert step(w, at(2, issues=[issue(5, BOB)])) == [], "the first read with a known owner is the baseline: no burst for what was already there"
    assert [e["issue"] for e in step(w, at(3, issues=[issue(5, [BOB, CAROL])]))] == [5]


def test_a_start_needs_a_trusted_signal_not_a_forged_or_non_collaborator_line():
    w = team.Watch()
    step(w, at(0))
    stranger = {"id": 9, "login": "mallory"}
    forged = {**PR7, "number": 8, "author": stranger, "association": "NONE", "closes": [5]}
    bot = {**PR7, "number": 9, "author": {"id": 10, "login": "robot[bot]"}, "association": "MEMBER", "closes": [5]}
    got = step(w, at(1, pulls=[forged, bot], claims=[{"issue": 5, "user": stranger, "status": "running", "branch": None, "live": False, "updated_at": RECENT}],
                     presence=presence(pline(stranger, 5, age=90000, nid="a0000009"))))
    assert got == [], "a pull request of somebody who may not write, a bot, an expired claim and a gone line hold nothing"
    w2 = team.Watch()
    step(w2, at(0))
    assert step(w2, at(1, presence=presence(pline(CAROL, 5, nid="a0000008"), server_now=None))) == [], "a presence line with no clock from GitHub holds nothing"
    ok = step(w, at(2, pulls=[{**PR7, "number": 10, "closes": [5]}]))
    assert [(e["login"], e["issue"]) for e in ok] == [("bob", 5)], "a trusted pull request of a collaborator does"


def test_an_app_login_and_a_nameless_user_are_not_told_about():
    assert team.starts_from(set(), {(("id", 77), 5): "dependabot[bot]", (("id", 78), 5): None, (("id", 79), 5): "x" * 80}, {5}, "alice", 1) == []


def test_reads_further_apart_than_an_hour_are_not_successive():
    w = team.Watch()
    step(w, at(0))
    assert step(w, read(read_at=NOW - 30 + team.START_GAP_S + 500, issues=[issue(5, BOB)])) == [], "after an outage this read is a new baseline"


def test_a_read_that_is_not_ok_or_has_no_graph_read_yet_moves_nothing():
    w = team.Watch()
    step(w, at(0))
    assert step(w, at(1, issues=[issue(5, BOB)], gql_at=None)) == []
    assert step(w, at(2, issues=[issue(5, BOB)], graph_error="rate_limited")) == []
    assert step(w, dict(at(3, issues=[issue(5, BOB)]), status="rate_limited")) == []
    assert [e["issue"] for e in step(w, at(4, issues=[issue(5, BOB)]))] == [5], "the baseline never moved on a read that was not complete, so the start is still a start"


LOST = {
    "editor_check_failed": lambda: dict(presence={**presence(), "editor_check": "failed"}),
    "presence_unreadable": lambda: dict(presence={**presence(), "status": "error"}),
    "presence_cut": lambda: dict(presence={**presence(), "truncated": True}),
    "pulls_cut": lambda: dict(truncated={"issues": False, "pulls": True, "tips": False}),
    "link_window_full": lambda: dict(gql_full={"issues": False, "pulls": True}),
}


@pytest.mark.parametrize("name", sorted(LOST))
def test_a_read_that_lost_presence_lines_is_not_a_baseline_so_the_recovered_read_is_no_start(name):
    w = team.Watch()
    bob = presence(pline(BOB, 5))
    assert step(w, at(0, presence=bob)) == [], "bob was already there"
    kept = (set(w.prev[SHOP]), w.at[SHOP])
    assert step(w, at(1, **LOST[name]())) == [] and (set(w.prev[SHOP]), w.at[SHOP]) == kept, "bob's line is missing from this read: it moves nothing"
    assert step(w, at(2, presence=bob)) == [], "bob is back, not new"
    both = presence(pline(BOB, 5), pline(CAROL, 5, nid="a0000009"))
    assert [e["login"] for e in step(w, at(3, presence=both))] == ["carol"], "a real start after the recovery is still told"


def test_a_burst_is_capped_and_a_repository_switched_off_starts_over():
    w = team.Watch()
    local = team._local_clean([task(i, issue_number=i) for i in range(1, 12)])
    step(w, at(0), local=local)
    crowd = [issue(i, {"id": 100 + i, "login": f"mate{i}"}) for i in range(1, 9)]
    assert len(step(w, at(1, issues=crowd), local=local)) == team.START_MAX
    w.forget(SHOP)
    assert step(w, at(2, issues=crowd), local=local) == [], "switched off and on again: a new baseline"
    assert w.prev[SHOP] and SHOP in w.at


def test_the_watch_reads_nothing_and_writes_nothing():
    w = team.Watch()
    rd = at(0, issues=[issue(5, BOB)])
    before = json.dumps(rd, sort_keys=True)
    step(w, rd)
    assert json.dumps(rd, sort_keys=True) == before, "the read is not changed"


# ================================================================ the real poller, the fake GitHub, the recorders

@pytest.fixture
def sink(tw, monkeypatch):
    """ntfy and Web Push as recorders; the poll's change detection wired as the app does."""
    from app import main
    ntfy, web = [], []
    monkeypatch.setattr(notify, "publish", lambda title, message, **k: ntfy.append({"title": title, "message": message, **k}) or True)
    monkeypatch.setattr(notify, "web_push", lambda title, body, url, tag=None, extra=None: web.append({"title": title, "body": body, "url": url, "tag": tag}) or 1)
    monkeypatch.setattr(settings, "ntfy_url", "http://127.0.0.1:2586")
    monkeypatch.setattr(settings, "ntfy_topic", "ccboard")
    monkeypatch.setattr(settings, "public_url", "")
    monkeypatch.setattr(notify, "_db", main.db)
    tw.poller.on_read = main._team_watch
    tw.ntfy, tw.web = ntfy, web
    return tw


def world(tw):
    seed(tw.gh)
    tw.gh.add_issue(REPO, 5, "Secret title of five")
    tw.gh.assign(f"{REPO}#5", "alice")
    tw.gh.add_issue(REPO, 6, "Secret title of six")
    tw.gh.assign(f"{REPO}#6", "alice")
    tw.share()
    tw.settle()
    tw.run_for(10)


def test_end_to_end_nothing_is_sent_until_the_kind_is_switched_on_and_then_once_per_person_and_issue(sink):
    tw = sink
    world(tw)
    assert tw.ntfy == [] and tw.web == []
    tw.gh.assign(f"{REPO}#5", "alice", "bob")
    tw.poller.wake()
    tw.run_for(400)
    assert tw.ntfy == [] and tw.web == [], "bob started on issue 5, which alice holds: the kind is off by default, so nothing is sent"
    tw.db.kv_set("notify_prefs", {"needs": True, "done": True, "limit": True, "error": True, "login": True, "team": True})
    tw.gh.assign(f"{REPO}#6", "alice", "bob")
    tw.poller.wake()
    tw.run_for(400)
    assert len(tw.ntfy) == 1 and len(tw.web) == 1, "switched on: the next start is told on both channels"
    n = tw.ntfy[0]
    assert n["title"] == "bob started on #6" and n["message"] == "acme/shop\nbob started on issue #6, which you hold.\nOpen Team to see who is working on what."
    assert "Secret" not in json.dumps(tw.ntfy + tw.web), "no title or text from GitHub, only a login and an issue number"
    tw.poller.wake()
    tw.run_for(900)
    assert len(tw.ntfy) == 1 and len(tw.web) == 1, "more reads of the same state tell nothing more"
    assert tw.gh.writes() == [], "the watch wrote nothing to GitHub"


def test_end_to_end_the_owner_other_boards_and_non_collaborators_never_cause_a_notice(sink):
    tw = sink
    world(tw)
    tw.db.kv_set("notify_prefs", {"team": True})
    tw.gh.pull(REPO, 20, "from a stranger", user="mallory", head_ref="m/x", closes=[5], assoc="NONE")
    tw.gh.assign(f"{REPO}#6", "alice")
    tw.gh.tip(REPO, "alice/other-board", author="alice")
    tw.poller.wake()
    tw.run_for(600)
    assert tw.ntfy == [] and tw.web == [], "a pull request of somebody who may not write, and the owner's own branch, tell the owner nothing"


def test_end_to_end_switching_a_repository_off_and_on_makes_a_new_baseline(sink):
    tw = sink
    world(tw)
    tw.db.kv_set("notify_prefs", {"team": True})
    tw.poller.forget([KEY])
    tw.gh.assign(f"{REPO}#5", "alice", "bob")
    tw.poller.wake()
    tw.run_for(400)
    assert tw.ntfy == [], "the first read after a new start is a baseline"


def test_the_route_wires_the_watch_and_a_switch_off_forgets_it(tw):
    from app import main
    tw.share()
    assert main._team_poller() is tw.poller and tw.poller.on_read == main._team_watch
    team.watch.prev[KEY], team.watch.at[KEY] = {((("id", 2)), 5)}, 1.0
    tw.c.put("/api/team/repos/acme/shop", headers=H, json={"shared": False})
    assert KEY not in team.watch.prev and KEY not in team.watch.at


# ================================================================ the demo speaks the live shape

def _live_body():
    entries, reads = two_repos()
    reads[KEY]["open_issues"] = [open_issue(40)]
    off = entry(key="github.com/acme/off", slug="acme/off", local=[{"project": "x", "repo": "y"}], shared=False)
    return team.build(entries + [off], reads, "alice", 1, NOW)


def _keys_problems(what, want, got):
    out = []
    if set(want) - set(got):
        out.append(f"{what}: missing in the demo: {sorted(set(want) - set(got))}")
    if set(got) - set(want):
        out.append(f"{what}: only in the demo: {sorted(set(got) - set(want))}")
    return out


def test_the_demo_fixture_has_every_key_the_live_answer_has():
    fx = json.loads((DEMO / "team.json").read_text(encoding="utf-8"))
    live = _live_body()
    base = fx["base"]
    ok_block = next(r for r in live["repos"] if r["shared"] and r["state"] == "ok")
    off_block = next(r for r in live["repos"] if not r["shared"])
    assert set(ok_block) == set(off_block), "the live blocks of a shared and an unshared repository have one key set"
    person_keys = set(ok_block["people"][0])
    problems = _keys_problems("body", live, base)
    for r in base["repos"]:
        problems += _keys_problems(f"repo {r['slug']}", ok_block, r)
        for p in r["people"]:
            problems += _keys_problems(f"person {p['login']} of {r['slug']}", person_keys, p)
        if r["shared"]:
            problems += _keys_problems(f"free of {r['slug']}", ok_block["free"], r.get("free") or {})
    for name, v in fx["variants"].items():
        if "repo" in v:
            problems += _keys_problems(f"variant {name}", ok_block, {**base["repos"][0], **v["repo"]})
    ov = base.get("overview") or {}
    problems += _keys_problems("overview", live["overview"], ov)
    problems += _keys_problems("overview counts", live["overview"]["counts"], ov.get("counts") or {})
    problems += _keys_problems("overview free counts", live["overview"]["counts"]["free"], (ov.get("counts") or {}).get("free") or {})
    for r in ov.get("rows") or []:
        problems += _keys_problems(f"overview row {r.get('login')}/{r.get('issue')}", ROW_KEYS, r)
    for q in ov.get("people") or []:
        problems += _keys_problems(f"overview person {q.get('login')}", PERSON_KEYS, q)
    assert not problems, "\n".join(problems)


def test_the_demo_overview_is_what_the_model_makes_of_the_demo_blocks():
    fx = json.loads((DEMO / "team.json").read_text(encoding="utf-8"))
    base = fx["base"]
    assert "overview" in base, "demo/team.json base needs the overview the live answer carries: team.overview(base['repos'])"
    assert base["overview"] == team.overview(base["repos"])
    states = {r["state"] for r in base["overview"]["rows"]}
    assert states <= set(team.ROW_STATES) and {"working", "github"} <= states, "the demo shows live work and work GitHub shows"


OVERVIEWS = Path(__file__).resolve().parent / "fixtures" / "team_demo_overviews.json"


def laid_over(fx, name):
    """The blocks the demo answers for a variant: the lay-over team.js demoTeam makes (a repo patch and the presence by login on example/website, a patch by repo_slug on the others)."""
    import copy
    v = fx["variants"].get(name) or {} if name else {}
    repos = copy.deepcopy(fx["base"]["repos"])
    if "repo" in v:
        repos[0].update(copy.deepcopy(v["repo"]))
    if v.get("lines") and isinstance(repos[0].get("people"), list):
        people = [{**x, "presence": v["lines"][x["login"]]} if x["login"] in v["lines"] else x for x in repos[0]["people"]]
        live = sum(1 for x in people if not x["you"] and x.get("presence") and x["presence"]["live"] in ("live", "idle"))
        repos[0] = {**repos[0], "people": people, "counts": {**repos[0]["counts"], "live": live}}
    for slug, patch in (v.get("others") or {}).items():
        i = next(k for k, b in enumerate(repos) if b["repo_slug"] == slug)
        repos[i] = {**repos[i], **copy.deepcopy(patch)}
    return repos


def test_the_overview_of_every_demo_variant_is_what_the_model_makes():
    """tests/fixtures/team_demo_overviews.json holds what team.overview() makes of every variant of demo/team.json; tests/js/team-view.test.mjs holds team.js (demoOverview, which
    the demo answers with) to the same file, so the demo cannot drift from the model."""
    fx = json.loads((DEMO / "team.json").read_text(encoding="utf-8"))
    made = {name: team.overview(laid_over(fx, None if name == "base" else name)) for name in ["base", *fx["variants"]]}
    if os.environ.get("CCBOARD_WRITE_FIXTURES") == "1":                       # after a change to demo/team.json or to the model: CCBOARD_WRITE_FIXTURES=1 pytest tests/test_team_overview.py -k variant
        OVERVIEWS.write_text("{\n" + ",\n".join(f"{json.dumps(k)}: {json.dumps(o, ensure_ascii=False, separators=(',', ':'), sort_keys=True)}" for k, o in made.items()) + "\n}\n", encoding="utf-8")
    want = json.loads(OVERVIEWS.read_text(encoding="utf-8"))
    assert set(want) == set(fx["variants"]) | {"base"}, "one expected overview for the base and for each variant"
    for name in want:
        assert made[name] == want[name], f"variant {name}: the saved overview is not what team.overview makes of its blocks (CCBOARD_WRITE_FIXTURES=1 writes it again)"


def test_every_demo_free_block_follows_the_rules_of_the_model():
    """A repository that is not shared has no free block; a shared one that is not ok, old, loading or without a clock says unknown (never free); the fixture's ok list carries the real keys."""
    fx = json.loads((DEMO / "team.json").read_text(encoding="utf-8"))
    for name in ["base", *fx["variants"]]:
        for b in laid_over(fx, None if name == "base" else name):
            f = b["free"]
            if not b["shared"]:
                assert f is None, (name, b["slug"])
                continue
            assert set(f) == FREE_KEYS, (name, b["slug"])
            if b["state"] != "ok" or b["stale"]:
                assert f["state"] == "unknown" and f["issues"] == [] and f["count"] == 0, (name, b["slug"], f)
            if f["state"] == "ok":
                assert b["state"] == "ok" and not b["stale"] and all(i["number"] not in f["held"] for i in f["issues"]), (name, b["slug"])
            if f["state"] == "unknown":
                assert f["issues"] == [] and f["reason"] and f["message"], (name, b["slug"])


# ================================================================ the route carries it, from the cache

def test_the_route_carries_the_overview_and_the_free_block_from_the_cache_without_asking_github(tw):
    seed(tw.gh)
    tw.share()
    tw.settle()
    tw.run_for(60)
    tw.poller.stop(2.0)
    tw.gh.clear_calls()
    r = tw.c.get("/api/team/state", headers=ID)
    body = r.json()
    ov = body["overview"]
    assert r.status_code == 200 and set(ov) == {"api", "counts", "people", "rows"} and set(ov["counts"]) == COUNT_KEYS
    assert ov["counts"]["shared"] == 1 and ov["counts"]["ok"] == 1 and ov["counts"]["teammates"] == 1 and ov["counts"]["live"] == 0
    assert {p["login"] for p in ov["people"]} == {"alice", "bob"} and all(set(r_) == ROW_KEYS for r_ in ov["rows"])
    bob = rows_of(body, "bob")
    assert {(r_["issue"], r_["branch"], r_["pr"]) for r_ in bob} == {(1, "bob/fix-login", 7), (None, "bob/spike", None)} and all(r_["state"] == "github" for r_ in bob)
    free = body["repos"][0]["free"]
    assert set(free) == FREE_KEYS and free["state"] == "ok" and free["held"] == [1, 3] and [i["number"] for i in free["issues"]] == [2] and free["reason"] is None, "alice and bob hold 1 and 3; 2 is free"
    assert ov["counts"]["free"] == {"count": 1, "ok": 1, "unread": 0, "unknown": 0}
    again = tw.c.get("/api/team/state", headers={**ID, "If-None-Match": r.headers["ETag"]})
    assert again.status_code == 304
    assert tw.gh.calls() == [], "building the view reads no GitHub and writes none"
    assert "node-" not in json.dumps(ov), "people are shown as people, never as boards or nodes"


def test_a_failing_watch_never_stops_the_poll(tw):
    seed(tw.gh)

    def boom(key, entry):
        raise RuntimeError("watch broke")

    tw.poller.on_read = boom
    tw.share()
    tw.settle()
    assert tw.block()["state"] == "ok" and tw.block()["read_at"], "the repository was read although the watcher raised"


def test_a_board_that_shares_nothing_runs_no_watch(tw, monkeypatch):
    from app import main
    spy = []
    monkeypatch.setattr(team.watch, "step", lambda *a, **k: spy.append(a) or [])
    seed(tw.gh)
    assert main._team_poller() is tw.poller
    tw.poller.step()
    tw.c.get("/api/team/state", headers=ID)
    assert spy == [] and tw.gh.calls() == [] and tw.poller.reads == {}


def test_two_polls_with_nothing_new_build_the_answer_once_and_keep_the_etag(tw, monkeypatch):
    from app import main
    seed(tw.gh)
    tw.share()
    tw.settle()
    tw.run_for(60)
    tw.poller.stop(2.0)
    built = []
    real = team.build
    monkeypatch.setattr(team, "build", lambda *a, **k: built.append(1) or real(*a, **k))
    main._team_cache.update(poller=None, sig=None, body=None, tag=None)
    first = tw.c.get("/api/team/state", headers=ID)
    second = tw.c.get("/api/team/state", headers=ID)
    assert first.status_code == second.status_code == 200 and len(built) == 1, "the second call reused the first build"
    assert first.headers["ETag"] == second.headers["ETag"] and first.json() == second.json()
    assert tw.c.get("/api/team/state", headers={**ID, "If-None-Match": first.headers["ETag"]}).status_code == 304 and len(built) == 1
    tw.poller.reads[KEY]["read_at"] += 5                                   # a new read landed
    tw.c.get("/api/team/state", headers=ID)
    assert len(built) == 2
    main._team_cache["at"] -= main.TEAM_CACHE_S + 1                        # nothing new, but the cache is old
    tw.c.get("/api/team/state", headers=ID)
    assert len(built) == 3


# ================================================================ free issues end to end: the real poller against the fake GitHub

def free_block(tw):
    return tw.block()["free"]


def test_free_is_ok_on_a_live_board_with_alice_bob_and_an_unassigned_issue(tw):
    seed(tw.gh)
    tw.gh.add_issue(REPO, 4, "Four", labels=["help wanted"])
    tw.gh.add_issue(REPO, 5, "A pull request listed as an issue", pull_request=True)
    tw.gh.add_issue(REPO, 6, "Presence", labels=[team.PRESENCE_LABEL])
    tw.share()
    tw.settle()
    f = free_block(tw)
    assert set(f) == FREE_KEYS and f["state"] == "ok" and f["reason"] is None and f["message"] == "" and f["truncated"] is False
    assert f["held"] == [1, 3], "bob holds 1 (assigned, a linked branch, his pull request), alice holds 3"
    assert [i["number"] for i in f["issues"]] == [2, 4], "6 is the status issue, 5 and the pull request 7 are pull requests"
    assert f["issues"][1] == {"number": 4, "title": "Four", "url": "https://github.com/acme/shop/issues/4", "labels": ["help wanted"], "updated_at": "2026-10-01T09:00:00Z"}
    assert tw.body()["overview"]["counts"]["free"] == {"count": 2, "ok": 1, "unread": 0, "unknown": 0}


def test_an_issue_somebody_takes_leaves_the_free_list_and_one_that_opens_joins_it(tw):
    seed(tw.gh)
    tw.share()
    tw.settle()
    assert [i["number"] for i in free_block(tw)["issues"]] == [2]
    tw.gh.assign(f"{REPO}#2", "bob")
    tw.run_for(130)
    f = free_block(tw)
    assert f["state"] == "ok" and f["issues"] == [] and f["held"] == [1, 2, 3], "the assigned-issue read (every 120 s) takes it out before the open issues are read again"
    tw.gh.add_issue(REPO, 8, "Eight")
    tw.run_for(700)
    assert [i["number"] for i in free_block(tw)["issues"]] == [8]


def test_an_unassigned_issue_with_a_linked_branch_is_not_free_on_a_live_board(tw):
    seed(tw.gh)
    tw.gh.add_issue(REPO, 9, "Nine, nobody assigned")
    tw.gh.linked(f"{REPO}#9", "bob/nine", author="bob")
    tw.gh.add_issue(REPO, 10, "Ten, nobody assigned, branch of nobody known")
    tw.gh.linked(f"{REPO}#10", "ghost/ten", author=None)
    tw.share()
    tw.settle()
    f = free_block(tw)
    assert f["state"] == "ok" and [i["number"] for i in f["issues"]] == [2], "9 and 10 have a linked branch and no assignee or status line: taken"
    assert f["held"] == [1, 3, 9, 10]
    bob = next(r for r in tw.body()["overview"]["rows"] if r["login"] == "bob" and r["issue"] == 9)
    assert bob["kind"] == "branch" and bob["branch"] == "bob/nine", "the team view says whose branch it is"


def test_free_says_so_when_the_unassigned_page_was_full(tw):
    seed(tw.gh)
    for n in range(10, 10 + 105):
        tw.gh.add_issue(REPO, n, f"Open {n}")
    tw.share()
    tw.settle()
    f = free_block(tw)
    assert f["state"] == "ok" and f["truncated"] is True and f["count"] == 100 and len(f["issues"]) == team.FREE_MAX
    assert tw.body()["overview"]["counts"]["free"]["ok"] == 1


def test_free_is_unread_until_the_open_issues_were_read_and_again_when_that_read_fails(tw):
    seed(tw.gh)
    tw.gh.fail("assignee=none", 500, times=-1)
    tw.share()
    tw.settle()
    f = free_block(tw)
    assert f["state"] == "unread" and f["held"] == [1, 3] and f["issues"] == [] and tw.block()["state"] == "ok"


def test_the_route_after_a_live_read_lists_the_free_issue_and_asks_github_for_nothing(tw):
    seed(tw.gh)
    tw.share()
    tw.settle()
    tw.run_for(60)
    tw.poller.stop(2.0)
    tw.gh.clear_calls()
    body = tw.c.get("/api/team/state", headers=ID).json()
    assert body["repos"][0]["free"]["state"] == "ok" and [i["number"] for i in body["repos"][0]["free"]["issues"]] == [2]
    assert tw.gh.calls() == []
