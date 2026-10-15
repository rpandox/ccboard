"""Team P3, issue #199: warn before starting work a teammate already holds. The model (app/team.py `conflicts`), the changed-files read (app/team_github.py), the one cross-person input
of app/overlap.py and the warnings carried on GET /api/team/state.

The model takes plain data and has no network, so the first half hands it hand-made reads, including hostile ones. The second half runs the real reader against the in-process fake
GitHub of tests/fake_github.py (alice is the board's owner, bob a teammate) on a virtual clock. A warning is advice: no test here lets a warning change a dispatch, and every test that
looks at GitHub asserts that nothing was written. No test reaches the real gh, GitHub, claude, codex, tmux or the real home.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest

from app import github, overlap, team, team_github
from tests.fake_github import iso
from tests.test_team import ALICE, BOB, CAROL, NOW, RECENT, block, entry, issue, read
from tests.test_team_github import H, ID, KEY, REPO, person, seed, tw  # noqa: F401 (tw is a fixture)

SINCE = "2027-01-15T06:10Z"                     # a board's session start, as a presence line spells it (minutes)


def pline(user, n, *, state="running", age=60, moved=True, lease=30, branch=None, server_now=NOW, nid="a1b2c3d4", seq=5, **kw):
    """One presence line as the reader hands it over: `age` seconds old on GitHub's clock."""
    return {"user": user, "node": "node-a", "nid": nid, "seq": seq, "lease_min": lease, "state": state, "agent": "claude", "since": SINCE, "last": SINCE, "issue": n, "branch": branch,
            "phase": "running", "updated_at": iso(server_now - age), "seq_seen_at": server_now - age, "moved": moved, **kw}


def presence(*lines, server_now=NOW):
    return {"status": "ok", "issue": 900, "lines": list(lines), "server_now": server_now, "truncated": False, "editor_check": "ok"}


def warn(rd, me=("alice", 1), local=None):
    """The warnings of a read as the block carries them."""
    return block(rd, me=me, **({} if local is None else {}))["warnings"] if local is None else team.repo_state(entry(), rd, me[0], me[1], NOW, local)["warnings"]


def one(w, n):
    got = w["issues"].get(str(n)) or []
    assert len(got) == 1, got
    return got[0]


def task(tid=1, *, issue_number=None, branch=None, files=None, title="Local work"):
    return {"id": tid, "title": title, "issue_number": issue_number, "branch": branch, "files": files}


def walk(o):
    """Every key and every string of a nested body."""
    if isinstance(o, dict):
        for k, v in o.items():
            yield k
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)
    elif isinstance(o, str):
        yield o


# ================================================================ same issue: the four forms, each once, with the person and the time

def test_an_assignee_warns_once_with_the_person():
    w = warn(read(issues=[issue(212, BOB)]))
    got = one(w, 212)
    assert got["person"] == BOB and got["status"] == "taken" and [s["kind"] for s in got["signals"]] == ["assigned"]
    assert got["url"] == "https://github.com/acme/shop/issues/212" and got["since"] is None and got["since_from"] is None, "an assignment has no time on GitHub, so none is claimed"


def test_a_live_presence_line_warns_once_with_the_person_and_since_when():
    w = warn(read(presence=presence(pline(BOB, 212, age=120))))
    got = one(w, 212)
    sig = got["signals"][0]
    assert got["person"] == BOB and got["status"] == "taken" and sig["kind"] == "presence" and sig["live"] == "live" and sig["state"] == "running" and sig["agent"] == "claude"
    assert got["since"] == "2027-01-15T06:10:00Z" and got["since_from"] == "presence" and got["age_s"] == 120


def test_a_linked_branch_warns_once_with_the_person_and_the_time():
    w = warn(read(issues=[], linked={212: [{"name": "bob/fix-212", "at": RECENT, "author": BOB}]}))
    got = one(w, 212)
    assert got["person"] == BOB and got["status"] == "taken"
    assert got["signals"] == [{"kind": "branch", "name": "bob/fix-212", "url": "https://github.com/acme/shop/tree/bob/fix-212", "at": RECENT}]


def test_a_live_claim_warns_once_and_an_expired_or_unknown_one_does_not():
    claim = {"issue": 212, "user": BOB, "status": "running", "branch": "bob/c", "updated_at": RECENT}
    got = one(warn(read(claims=[{**claim, "live": True}])), 212)
    assert got["person"] == BOB and got["status"] == "taken" and got["signals"][0] == {"kind": "claim", "status": "running", "branch": "bob/c", "at": RECENT}
    for live in (False, None):
        assert warn(read(claims=[{**claim, "live": live}]))["issues"] == {}, "an expired claim, or one with no clock to judge it by, holds nothing"


def test_a_pull_request_that_closes_the_issue_is_a_signal_too():
    pr = {"number": 7, "title": "WIP", "draft": True, "author": BOB, "association": "COLLABORATOR", "head_ref": "bob/x", "head_repo": REPO, "fork": False, "updated_at": RECENT, "closes": [212]}
    got = one(warn(read(pulls=[pr])), 212)
    assert got["signals"][0]["kind"] == "pr" and got["signals"][0]["number"] == 7 and got["signals"][0]["draft"] is True


def test_two_signals_from_one_person_give_one_warning_and_two_people_give_two():
    claim = {"issue": 212, "user": BOB, "status": "running", "branch": None, "updated_at": RECENT, "live": True}
    rd = read(issues=[issue(212, [BOB, CAROL])], claims=[claim], linked={212: [{"name": "bob/x", "at": RECENT, "author": BOB}]}, presence=presence(pline(BOB, 212)))
    got = warn(rd)["issues"]["212"]
    assert [(w["person"]["login"], [s["kind"] for s in w["signals"]]) for w in got] == [("bob", ["assigned", "claim", "presence", "branch"]), ("carol", ["assigned"])]
    assert len({w["person"]["id"] for w in got}) == 2


def test_two_boards_of_one_teammate_naming_the_issue_are_one_warning():
    rd = read(presence=presence(pline(BOB, 212, nid="aaaaaaaa", age=900), pline(BOB, 212, nid="bbbbbbbb", age=30)))
    got = one(warn(rd), 212)
    assert len(got["signals"]) == 1 and got["signals"][0]["last_seen_s"] == 30, "the board seen most recently speaks for the person"


def test_a_person_known_by_id_and_by_login_only_is_one_warning():
    rd = read(issues=[issue(212, {"id": None, "login": "bob"})], presence=presence(pline(BOB, 212)))
    assert len(warn(rd)["issues"]["212"]) == 1


# ================================================================ the owner's own boards never warn each other

def test_the_owners_own_login_never_warns_on_any_signal():
    claim = {"issue": 5, "user": ALICE, "status": "running", "branch": "alice/x", "updated_at": RECENT, "live": True}
    pr = {"number": 7, "title": "mine", "draft": False, "author": ALICE, "association": "COLLABORATOR", "head_ref": "alice/x", "head_repo": REPO, "fork": False, "updated_at": RECENT, "closes": [5]}
    rd = read(issues=[issue(5, ALICE)], claims=[claim], pulls=[pr], tips=[{"name": "alice/x", "at": RECENT, "author": ALICE}], linked={5: [{"name": "alice/x", "at": RECENT, "author": ALICE}]},
              presence=presence(pline(ALICE, 5, nid="aaaaaaaa"), pline(ALICE, 5, nid="bbbbbbbb")), pull_files={7: {"files": ["a.py"], "more": False}})
    w = warn(rd, local=[task(1, issue_number=5, branch="alice/x", files=["a.py"])])
    assert w["issues"] == {} and w["branches"] == {} and w["tasks"] == {}, "a second board of the same owner is the owner"


def test_the_owner_is_known_by_id_so_a_renamed_login_is_still_the_owner_and_a_namesake_is_not():
    rd = read(issues=[issue(5, {"id": 1, "login": "alice-renamed"})])
    assert warn(rd, me=("alice", 1))["issues"] == {}
    rd = read(issues=[issue(5, {"id": 99, "login": "alice"})])
    assert list(warn(rd, me=("alice", 1))["issues"]) == ["5"], "the same login with another id is somebody else"
    rd = read(issues=[issue(5, {"id": None, "login": "Alice"})])
    assert warn(rd, me=("alice", None))["issues"] == {}, "without ids the login decides, ignoring case"


def test_an_unknown_identity_warns_nobody_and_says_so():
    w = warn(read(issues=[issue(5, BOB), issue(6, ALICE)]), me=(None, None))
    assert w["identity_known"] is False and w["issues"] == {} and w["branches"] == {} and w["tasks"] == {}


# ================================================================ stale warns with its age, gone and unknown do not

def test_a_stale_line_alone_warns_with_its_age_and_says_stale():
    got = one(warn(read(presence=presence(pline(BOB, 212, age=3 * 3600)))), 212)
    sig = got["signals"][0]
    assert got["status"] == "stale" and got["age_s"] == 3 * 3600 and sig["live"] == "stale" and sig["last_seen_s"] == 3 * 3600


def test_a_stale_line_beside_a_solid_signal_is_one_warning_that_is_still_taken():
    got = one(warn(read(issues=[issue(212, BOB)], presence=presence(pline(BOB, 212, age=3 * 3600)))), 212)
    assert got["status"] == "taken" and [s["kind"] for s in got["signals"]] == ["assigned", "presence"] and got["signals"][1]["live"] == "stale" and got["age_s"] == 3 * 3600


def test_a_gone_line_does_not_warn():
    rd = read(presence=presence(pline(BOB, 212, age=25 * 3600), pline(CAROL, 213, age=24 * 3600 - 60)))
    w = warn(rd)
    assert "212" not in w["issues"] and [x["person"]["login"] for x in w["issues"]["213"]] == ["carol"], "a line older than 24 hours is hidden, one a minute short of that is stale"


def test_a_line_with_no_clock_from_github_is_unknown_and_claims_nothing():
    assert warn(read(presence={**presence(pline(BOB, 212)), "server_now": None}))["issues"] == {}


def test_a_line_not_yet_seen_moving_is_recent_not_taken():
    got = one(warn(read(presence=presence(pline(BOB, 212, age=300, moved=False)))), 212)
    assert got["status"] == "recent" and got["signals"][0]["live"] == "unconfirmed"


def test_an_ended_session_holds_nothing_but_an_idle_one_does():
    assert warn(read(presence=presence(pline(BOB, 212, state="ended"))))["issues"] == {}
    got = one(warn(read(presence=presence(pline(BOB, 212, state="idle")))), 212)
    assert got["status"] == "taken" and got["signals"][0]["live"] == "idle"


def test_a_gone_word_is_refused_by_the_warnings_own_rule_not_only_by_the_presence_filter():
    lines = team._presence_person(read(presence=presence(pline(BOB, 212, age=25 * 3600))), "acme", "shop", keep_gone=True)
    assert [b["live"] for _, b in lines] == ["gone"], "the model can be handed a gone line ..."
    assert warn(read(presence=presence(pline(BOB, 212, age=25 * 3600))))["issues"] == {}, "... and the warnings still do not use it"


# ================================================================ same branch

def test_a_branch_that_exists_under_a_teammate_warns_once_naming_the_person():
    rd = read(tips=[{"name": "bob/fix-login", "at": RECENT, "author": BOB}])
    got = warn(rd)["branches"]["bob/fix-login"]
    assert len(got) == 1 and got[0]["person"] == BOB and got[0]["sources"] == ["pushed"] and got[0]["at"] == RECENT and got[0]["old"] is False
    assert got[0]["url"] == "https://github.com/acme/shop/tree/bob/fix-login"


def test_a_branch_linked_to_an_issue_by_someone_else_warns_and_names_the_issue():
    rd = read(issues=[issue(1, BOB)], linked={1: [{"name": "fix-1", "at": RECENT, "author": None}]})
    got = warn(rd)["branches"]["fix-1"]
    assert [(b["person"]["login"], b["sources"], b["issue"]) for b in got] == [("bob", ["linked"], 1)], "no known author: the issue's assignee holds it"


def test_a_branch_pushed_linked_and_on_a_pull_request_is_one_warning_with_all_three_sources():
    pr = {"number": 7, "title": "WIP", "draft": True, "author": BOB, "association": "COLLABORATOR", "head_ref": "bob/x", "head_repo": REPO, "fork": False, "updated_at": RECENT, "closes": None}
    rd = read(issues=[issue(1, BOB)], pulls=[pr], tips=[{"name": "bob/x", "at": RECENT, "author": BOB}], linked={1: [{"name": "bob/x", "at": RECENT, "author": BOB}]})
    got = warn(rd)["branches"]["bob/x"]
    assert len(got) == 1 and got[0]["sources"] == ["linked", "pr", "pushed"] and got[0]["issue"] == 1 and got[0]["pr"] == 7


def test_the_owners_branches_the_default_branch_machinery_forks_and_unknown_authors_do_not_warn():
    fork = {"number": 8, "title": "x", "draft": False, "author": BOB, "association": "COLLABORATOR", "head_ref": "forked", "head_repo": "bob/shop", "fork": True, "updated_at": RECENT, "closes": None}
    rd = read(tips=[{"name": "alice/mine", "at": RECENT, "author": ALICE}, {"name": "main", "at": RECENT, "author": BOB}, {"name": "ccboard-backup/box/x", "at": RECENT, "author": BOB},
                    {"name": "anon", "at": RECENT, "author": None}], pulls=[fork])
    assert warn(rd)["branches"] == {}, "a fork's branch is not a branch of this repository, and a commit nobody can be named for claims nothing"


def test_an_old_branch_still_warns_because_it_still_exists_but_says_it_is_old():
    got = warn(read(tips=[{"name": "bob/old", "at": "2026-09-01T00:00:00Z", "author": BOB}]))["branches"]["bob/old"]
    assert got[0]["old"] is True


# ================================================================ same files

PR7 = {"number": 7, "title": "Rework login", "draft": True, "author": BOB, "association": "COLLABORATOR", "head_ref": "bob/login", "head_repo": REPO, "fork": False, "updated_at": RECENT, "closes": None}


def test_a_pull_request_that_also_changes_the_local_tasks_files_warns_with_the_pr_and_the_names():
    rd = read(pulls=[PR7], pull_files={7: {"files": ["app/login.py", "app/auth.py", "docs/a.md"], "more": False}})
    w = warn(rd, local=[task(4, files=["app/auth.py", "app/login.py", "app/other.py"])])
    got = w["tasks"]["4"]["files"]
    assert len(got) == 1 and got[0]["files"] == ["app/auth.py", "app/login.py"] and got[0]["count"] == 2 and got[0]["more"] is False
    assert got[0]["pr"] == {"number": 7, "title": "Rework login", "url": "https://github.com/acme/shop/pull/7", "draft": True, "branch": "bob/login"}
    assert got[0]["person"] == BOB and got[0]["task"] == 4
    assert w["files"] == {"checked": 1, "pending": 0, "capped": False}


def test_each_pull_request_warns_once_per_task_and_a_task_with_no_common_file_does_not_appear():
    p8 = {**PR7, "number": 8, "author": CAROL}
    rd = read(pulls=[PR7, p8], pull_files={7: {"files": ["a.py", "b.py"], "more": False}, 8: {"files": ["b.py", "c.py"], "more": False}})
    w = warn(rd, local=[task(1, files=["b.py"]), task(2, files=["z.py"])])
    assert [(f["pr"]["number"], f["person"]["login"], f["files"]) for f in w["tasks"]["1"]["files"]] == [(7, "bob", ["b.py"]), (8, "carol", ["b.py"])]
    assert "2" not in w["tasks"]


def test_a_task_whose_files_are_not_known_is_not_the_same_as_a_task_with_none():
    rd = read(pulls=[PR7], pull_files={7: {"files": ["a.py"], "more": False}})
    assert warn(rd, local=[task(1, files=None)])["tasks"] == {}
    assert warn(rd, local=[task(1, files=[])])["tasks"] == {}


def test_a_pull_request_whose_files_are_not_read_yet_is_pending_not_clean():
    p8 = {**PR7, "number": 8}
    w = warn(read(pulls=[PR7, p8], pull_files={7: {"files": ["a.py"], "more": True}}), local=[task(1, files=["a.py"])])
    assert w["files"] == {"checked": 1, "pending": 1, "capped": True}
    assert w["tasks"]["1"]["files"][0]["more"] is True, "the list was cut, so there may be more files in common"


def test_a_pull_request_with_3000_changed_files_is_capped_and_the_warning_names_at_most_20():
    names = [f"src/gen/file{i:04d}.py" for i in range(3000)]
    w = warn(read(pulls=[PR7], pull_files={7: {"files": names, "more": True}}), local=[task(1, files=names[:300])])
    f = w["tasks"]["1"]["files"][0]
    assert len(f["files"]) == team.FILES_SHOWN == 20 and f["count"] == team.FILES_PER_PR == 200 and f["more"] is True
    assert len(json.dumps(w)) < 20_000, "the body stays small however large the pull request"


def test_the_owners_own_pull_request_files_never_warn():
    mine = {**PR7, "author": ALICE}
    w = warn(read(pulls=[mine], pull_files={7: {"files": ["a.py"], "more": False}}), local=[task(1, files=["a.py"])])
    assert w["tasks"] == {} and w["files"] == {"checked": 0, "pending": 0, "capped": False}


def test_the_task_lookup_carries_the_issue_the_branch_and_the_files_together():
    rd = read(issues=[issue(212, BOB)], tips=[{"name": "fix-212", "at": RECENT, "author": CAROL}], pulls=[PR7], pull_files={7: {"files": ["a.py"], "more": False}})
    w = warn(rd, local=[task(9, issue_number=212, branch="fix-212", files=["a.py"], title="My start")])
    t = w["tasks"]["9"]
    assert t["title"] == "My start" and t["issue"][0]["person"]["login"] == "bob" and t["branch"][0]["person"]["login"] == "carol" and t["files"][0]["pr"]["number"] == 7


# ================================================================ the Team block marks the rows that collide with the owner's own open tasks

def test_the_team_block_marks_a_teammates_rows_that_collide_with_an_open_task():
    pr = {**PR7, "closes": [212]}
    rd = read(issues=[issue(212, BOB)], pulls=[pr], tips=[{"name": "fix-212", "at": RECENT, "author": BOB}], pull_files={7: {"files": ["a.py", "b.py"], "more": False}})
    b = team.repo_state(entry(), rd, "alice", 1, NOW, [task(9, issue_number=212, branch="fix-212", files=["b.py", "z.py"], title="My start")])
    bob = person(b, "bob")
    assert bob["issues"][0]["collides"] == [{"task": 9, "title": "My start", "kind": "issue"}]
    assert bob["branches"][0]["collides"] == [{"task": 9, "title": "My start", "kind": "branch"}]
    assert bob["prs"][0]["collides"] == [{"task": 9, "title": "My start", "kind": "issue"}, {"task": 9, "title": "My start", "kind": "files", "files": ["b.py"], "count": 1, "more": False}]


def test_rows_without_a_collision_have_no_mark_and_the_owners_own_rows_are_never_marked():
    rd = read(issues=[issue(212, BOB), issue(5, ALICE)])
    b = team.repo_state(entry(), rd, "alice", 1, NOW, [task(9, issue_number=5), task(10, issue_number=999)])
    assert "collides" not in person(b, "bob")["issues"][0] and "collides" not in person(b, "alice")["issues"][0]
    assert "collides" not in team.repo_state(entry(), rd, "alice", 1, NOW)["people"][0]["issues"][0], "no local task, no mark"


# ================================================================ advice only

def test_a_warning_is_sealed_as_advice_and_refuses_any_key_that_could_drive_a_control():
    ok = team._advice({"issue": 1, "signals": [{"kind": "assigned"}]})
    assert ok["advice"] is True
    for key in team.ADVICE_FORBIDDEN:
        with pytest.raises(ValueError):
            team._advice({"issue": 1, key: True})
        with pytest.raises(ValueError):
            team._advice({"issue": 1, "signals": [{"kind": "x", key: True}]})


def test_every_warning_of_a_full_body_is_advice_and_no_key_anywhere_could_drive_a_control():
    claim = {"issue": 212, "user": BOB, "status": "running", "branch": "bob/c", "updated_at": RECENT, "live": True}
    rd = read(issues=[issue(212, BOB)], claims=[claim], pulls=[{**PR7, "closes": [212]}], tips=[{"name": "bob/x", "at": RECENT, "author": BOB}], presence=presence(pline(BOB, 212)),
              linked={212: [{"name": "bob/x", "at": RECENT, "author": BOB}]}, pull_files={7: {"files": ["a.py"], "more": False}})
    body = team.build([entry()], {KEY: rd}, "alice", 1, NOW, {KEY: [task(1, issue_number=212, branch="bob/x", files=["a.py"])]})
    w = body["repos"][0]["warnings"]
    assert w["issues"] and w["branches"] and w["tasks"]["1"]["files"]
    found = [x for x in walk(w["issues"]) if x == "advice"] + [x for x in walk(w["branches"]) if x == "advice"] + [x for x in walk(w["tasks"]) if x == "advice"]
    assert found, "every warning carries advice"
    for lst in list(w["issues"].values()) + list(w["branches"].values()) + [t["files"] for t in w["tasks"].values()]:
        assert all(x["advice"] is True for x in lst)
    assert not {k for k in walk(w) if k in team.ADVICE_FORBIDDEN}, "no key could disable a button, block a dispatch, assign, label, comment or write"


def test_the_team_modules_cannot_write_to_github():
    import ast
    root = Path(team.__file__).parent
    for name in ("team.py", "team_github.py", "overlap.py"):
        tree = ast.parse((root / name).read_text())
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {(n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "github_write" not in imported | names, name
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not calls & {"transport", "post", "put", "patch", "delete", "assign", "add_labels", "comment"}, (name, calls)


# ================================================================ untrusted text

def test_hostile_text_is_capped_stripped_and_kept_as_text_in_every_warning():
    evil = "<img src=x onerror=alert(1)>" + "‮" + "\x00" + "A" * 5000
    pr = {**PR7, "title": evil, "head_ref": "bob/<script>", "closes": [212]}
    rd = read(issues=[issue(212, {"id": 2, "login": "bob"})], pulls=[pr], pull_files={7: {"files": ["ok.py", "evil‮gnp.exe", "ctl\x07.py", "/etc/passwd", "../../x", "a/../b", "A" * 400, " lead.py", "tab\t.py"], "more": False}})
    w = warn(rd, local=[task(1, issue_number=212, files=["ok.py", "evil‮gnp.exe", "/etc/passwd"], title=evil)])
    text = json.dumps(w)
    assert "‮" not in text and "\\u0000" not in text and "\\u0007" not in text
    f = w["tasks"]["1"]["files"][0]
    assert f["files"] == ["ok.py"], "only a clean, relative path is compared or shown"
    assert len(f["pr"]["title"]) <= team.TEXT_MAX and len(w["tasks"]["1"]["title"]) <= 100
    assert all(len(x) <= team.TEXT_MAX for x in walk(w["issues"]) if isinstance(x, str) and not x.startswith("https://"))


def test_a_login_with_html_in_it_names_no_one_and_a_name_with_an_id_is_kept_only_as_a_valid_login():
    bad = {"id": 5, "login": "<b>bob</b>"}
    got = warn(read(issues=[issue(212, bad)]))["issues"]["212"]
    assert got[0]["person"] == {"id": 5, "login": None}, "the id names the person; the text is not a login, so it is dropped"
    assert warn(read(issues=[issue(212, {"id": None, "login": "<b>bob</b>"})]))["issues"] == {}


def test_path_of_takes_a_relative_clean_path_and_nothing_else():
    assert team.path_of("src/app.py") == "src/app.py" and team.path_of("with space/x.md") == "with space/x.md" and team.path_of("café/über.py") == "café/über.py"
    for bad in ("", "/abs", "../x", "a/../b", "a\nb", "a‮b", "a​b", "a b", "ab", " x", "x ", "x" * 301, None, 5, b"x"):
        assert team.path_of(bad) is None, bad


def test_the_local_task_titles_and_files_are_cleaned_before_use():
    lc = team._local_clean([{"id": 1, "title": "t\x00‮" + "x" * 500, "issue_number": 5, "branch": "b", "files": ["ok", "/bad", 7]}, {"id": True}, {"id": 0}, "x", {"id": 2, "files": "no"}])
    assert [t["id"] for t in lc] == [1, 2] and lc[0]["files"] == ["ok"] and len(lc[0]["title"]) == 100 and "\x00" not in lc[0]["title"] and lc[1]["files"] is None


# ================================================================ the changed-files read: allowed shape, parsing, caps

def test_the_changed_files_path_is_one_allowed_get_shape_and_nothing_near_it_is():
    path = github.pull_files_path("acme", "shop", 7, 2, 100)
    assert path == "/repos/acme/shop/pulls/7/files?per_page=100&page=2" and github.allowed_path(path)
    a = github.argv(path, 'W/"abc"')
    assert a[:5] == ["gh", "api", "--include", "--method", "GET"] and a[-1] == path and a.count("--method") == 1 and "If-None-Match: W/\"abc\"" in a
    assert not set(a) & {"-X", "-f", "-F", "--field", "--raw-field", "--input", "--paginate"}
    for bad in ("/repos/acme/shop/pulls/7/files", "/repos/acme/shop/pulls/7/files?per_page=100&page=1&x=1", "/repos/acme/shop/pulls/0/files?per_page=100&page=1",
                "/repos/acme/shop/pulls/7/merge?per_page=100&page=1", "/repos/acme/shop/pulls/7/comments?per_page=100&page=1", "/repos/acme/shop/pulls/7/files/../x?per_page=100&page=1",
                "/repos/-a/shop/pulls/7/files?per_page=100&page=1", "/repos/acme/shop/pulls/7/reviews?per_page=100&page=1", "/repos/acme/shop/pulls/a/files?per_page=100&page=1",
                "/repos/acme/shop/pulls/1234567890/files?per_page=100&page=1", "/repos/acme/shop/pulls/7/files?per_page=100&page=1\n"):
        assert not github.allowed_path(bad), bad
        with pytest.raises(github.GhReadError):
            github.argv(bad)
    for owner, repo, n, page in (("", "r", 1, 1), ("a b", "r", 1, 1), ("a", "..", 1, 1), ("a", "r", 0, 1), ("a", "r", "x", 1), ("a", "r", True, 1), ("a", "r", 1, 0), ("a", "r", 1, 1000)):
        with pytest.raises(github.GhReadError):
            github.pull_files_path(owner, repo, n, page)


def test_a_page_of_files_is_parsed_into_clean_names_and_a_rename_counts_under_both():
    data = [{"filename": "a.py"}, {"filename": "new.py", "previous_filename": "old.py"}, {"filename": "ctl\x07"}, {"filename": 5}, "x", {"filename": "a.py"}, {"filename": "/abs"}]
    names, full = team_github.parse_pull_files(data)
    assert names == ["a.py", "new.py", "old.py"] and full is False
    assert team_github.parse_pull_files([{"filename": f"f{i}"} for i in range(100)])[1] is True
    assert team_github.parse_pull_files({"message": "x"}) == ([], False)
    big, _ = team_github.parse_pull_files([{"filename": f"f{i}"} for i in range(500)])
    assert len(big) == github.PER_PAGE, "a page is looked at up to its size and no further"


# ================================================================ the poller reads the files: conditional, capped, slow, inside the budget

def local_task(tw, **kw):
    wt = tw.projects_dir.parent / "worktrees" / f"t{kw.get('slug', 'x')}"
    wt.mkdir(parents=True, exist_ok=True)
    row = {"project": "shop", "repo": "api", "slug": "x", "title": "Local work", "prompt": "p", "branch": "alice/local", "tmux_name": "shop--api--x", "phase": "running", "worktree": str(wt)}
    row.update(kw)
    return tw.db.task_add(**row)


def files_calls(tw, n=None):
    tail = f"/pulls/{n}/files" if n else "/files?"
    return [p for p in tw.gh.paths() if "/pulls/" in p and tail in p]


def state(tw):
    r = tw.c.get("/api/team/state", headers=ID)
    assert r.status_code == 200
    return next(b for b in r.json()["repos"] if b["slug"] == REPO)


def test_a_teammates_pull_request_files_are_read_through_the_state_and_warn(tw, monkeypatch):
    seed(tw.gh)
    tw.gh.pull_files(REPO, 7, ["app/login.py", "app/auth.py", "docs/x.md"])
    tid = local_task(tw, issue_number=1)
    monkeypatch.setattr(overlap, "changed_files", lambda wt, base: {"app/login.py", "app/other.py"})
    overlap.forget_files()
    tw.share()
    tw.settle()
    tw.run_for(30)
    b = state(tw)
    w = b["warnings"]
    assert w["identity_known"] is True and w["files"] == {"checked": 1, "pending": 0, "capped": False}
    ent = w["tasks"][str(tid)]
    assert [x["person"]["login"] for x in ent["issue"]] == ["bob"] and ent["issue"][0]["status"] == "taken"
    assert ent["files"][0]["files"] == ["app/login.py"] and ent["files"][0]["pr"]["number"] == 7
    assert [x["person"]["login"] for x in w["issues"]["1"]] == ["bob"] and "bob/fix-login" in w["branches"]
    bob = person(b, "bob")
    assert {c["kind"] for c in bob["issues"][0]["collides"]} == {"issue"} and [c["kind"] for c in bob["prs"][0]["collides"]] == ["issue", "files"]
    assert tw.gh.writes() == [], "reading and warning write nothing to GitHub"


def test_only_teammates_pull_requests_are_read_and_the_owners_own_never_costs_a_call(tw):
    seed(tw.gh)
    tw.gh.pull(REPO, 8, "Mine", user="alice", head_ref="alice/x")
    tw.gh.pull_files(REPO, 7, ["a.py"])
    tw.gh.pull_files(REPO, 8, ["b.py"])
    tw.share()
    tw.settle()
    tw.run_for(60)
    assert files_calls(tw, 7) and not files_calls(tw, 8), "the owner's own pull request is not read"
    assert state(tw)["warnings"]["files"]["checked"] == 1


def test_a_fork_pull_request_files_are_read_through_the_base_repository(tw):
    seed(tw.gh)
    tw.gh.pull(REPO, 9, "From a fork", user="carol", head_ref="feature", head_repo="carol/shop")
    tw.gh.pull_files(REPO, 9, ["f.py"])
    tw.share()
    tw.settle()
    tw.run_for(60)
    assert files_calls(tw, 9) and all(p.startswith("/repos/acme/shop/") for p in files_calls(tw))


def test_a_list_is_read_once_then_only_revalidated_slowly_and_read_again_when_the_pull_request_moves(tw):
    seed(tw.gh)
    tw.gh.pull_files(REPO, 7, ["a.py"])
    tw.share()
    tw.settle()
    tw.run_for(120)
    first = len(files_calls(tw, 7))
    assert first == 1
    tw.run_for(team_github.FILES_REVALIDATE_S - 600)
    assert len(files_calls(tw, 7)) == 1, "nothing is asked while the pull request is unchanged and the revalidation time has not come"
    tw.run_for(900)
    assert len(files_calls(tw, 7)) == 2
    last = [c for c in tw.gh.calls() if c["argv"][-1].startswith("/repos/acme/shop/pulls/7/files")][-1]
    assert last["status"] == 304, "the revalidation is a conditional GET and GitHub answers 304"
    tw.gh.pull(REPO, 7, "WIP: fix login", user="bob", draft=True, head_ref="bob/fix-login", closes=[1], updated_at="2027-01-15T07:00:00Z")
    tw.gh.pull_files(REPO, 7, ["a.py", "new.py"])
    tw.run_for(400)
    assert len(files_calls(tw, 7)) == 3, "a moved updated_at is read again at once"
    assert state(tw)["warnings"]["files"]["checked"] == 1


def test_a_pull_request_with_3000_files_costs_at_most_two_calls_and_is_marked_cut(tw, monkeypatch):
    seed(tw.gh)
    names = [f"gen/f{i:04d}.py" for i in range(3000)]
    tw.gh.pull_files(REPO, 7, names)
    local_task(tw, issue_number=1)
    monkeypatch.setattr(overlap, "changed_files", lambda wt, base: set(names[:50]) | {"app/x.py"})
    overlap.forget_files()
    tw.share()
    tw.settle()
    tw.run_for(60)
    assert len(files_calls(tw, 7)) == team_github.FILES_PAGES == 2
    rd = tw.poller.snapshot()[KEY]["pull_files"][7]
    assert len(rd["files"]) == 200 and rd["more"] is True
    w = state(tw)["warnings"]
    f = next(iter(w["tasks"].values()))["files"][0]
    assert f["more"] is True and f["count"] == 50 and len(f["files"]) == 20 and w["files"]["capped"] is True


def test_at_most_a_dozen_pull_requests_of_one_repository_have_their_files_read_and_a_few_pages_a_cycle(tw):
    seed(tw.gh)
    for n in range(20, 50):
        tw.gh.pull(REPO, n, f"PR {n}", user="bob", head_ref=f"b{n}", updated_at=f"2027-01-{(n % 28) + 1:02d}T00:00:00Z")
        tw.gh.pull_files(REPO, n, [f"f{n}.py"])
    tw.share()
    tw.settle()
    tw.gh.clear_calls()
    tw.poller.sched[KEY]["rest"] = 0.0
    tw.run_for(1)
    assert len(files_calls(tw)) <= team_github.FILES_PER_CYCLE, "a cycle reads a few pages, not all of them"
    tw.run_for(1800)
    read_prs = {p for p in files_calls(tw)}
    assert 0 < len({re.search(r"/pulls/(\d+)/", p).group(1) for p in read_prs}) <= team_github.FILES_PRS_MAX
    assert len(tw.poller.snapshot()[KEY]["pull_files"]) <= team_github.FILES_PRS_MAX


def test_a_closed_pull_request_loses_its_list_and_a_missing_one_is_unknown_not_empty(tw):
    seed(tw.gh)
    tw.gh.pull_files(REPO, 7, ["a.py"])
    tw.share()
    tw.settle()
    tw.run_for(60)
    assert 7 in tw.poller.snapshot()[KEY]["pull_files"]
    tw.gh.world["pulls"].pop(f"{REPO}#7")
    tw.poller.sched[KEY]["rest"] = 0.0
    tw.run_for(300)
    assert 7 not in tw.poller.snapshot()[KEY]["pull_files"]
    tw.gh.pull(REPO, 10, "No files known", user="bob", head_ref="b10")                  # the fake answers 404 for a pull request with no file list
    tw.poller.sched[KEY]["rest"] = 0.0
    tw.run_for(300)
    w = state(tw)["warnings"]
    assert w["files"]["pending"] == 1 and w["files"]["checked"] == 0, "a list that could not be read is pending, which is not the same as clean"


def test_the_files_read_waits_when_github_asked_for_a_pause_and_shares_the_back_off(tw):
    seed(tw.gh)
    tw.gh.pull_files(REPO, 7, ["a.py"])
    tw.gh.fail("/pulls/7/files", 403, headers={"retry-after": "300"}, body={"message": "secondary rate limit"})
    tw.share()
    tw.settle()
    tw.run_for(30)
    assert tw.block()["state"] == "rate_limited", "a rate limit on the files read is the repository's state like any other"
    assert github.reader.wait_left() > 0, "and it binds the epic reader on the same login too"
    n = len(tw.gh.paths())
    tw.run_for(60)
    assert len(tw.gh.paths()) == n, "nothing is asked while GitHub said wait"


def test_with_the_identity_not_known_no_files_are_read(tw):
    seed(tw.gh)
    tw.gh.pull_files(REPO, 7, ["a.py"])
    tw.gh.world["login"] = None
    tw.share()
    tw.run_for(120)
    assert files_calls(tw) == [], "without a login nothing is read, files included"


def test_a_low_remainder_stops_the_files_read_before_the_rest(tw):
    seed(tw.gh)
    tw.gh.pull_files(REPO, 7, ["a.py"])
    tw.share()
    tw.settle()
    tw.run_for(30)
    assert files_calls(tw, 7)
    tw.poller.reader.remaining = team_github.STOP_BELOW - 1
    assert tw.poller._files_room() is False


def points_for(tw, n_repos, prs_per_repo=6, hours=1.0):
    slugs = []
    for i in range(n_repos):
        proj, repo = f"p{i}", f"r{i}"
        (tw.projects_dir / proj / repo / ".git").mkdir(parents=True, exist_ok=True)
        tw.origins[repo] = f"git@github.com:acme/repo{i}.git"
        slugs.append(f"acme/repo{i}")
    from app import node_state
    node_state.slug_cache_clear()
    tw.main._invalidate_scan()
    for s in slugs:
        seed(tw.gh, s)
        for n in range(20, 20 + prs_per_repo):
            tw.gh.pull(s, n, f"PR {n}", user="bob", head_ref=f"b{n}")
            tw.gh.pull_files(s, n, [f"f{n}.py", "shared.py"])
        tw.gh.pull_files(s, 7, ["x.py"])
        tw.share("github.com/" + s)
    tw.gh.clear_calls()
    tw.run_for(3600 * hours)
    return slugs


@pytest.mark.parametrize("n", [1, 5, 20])
def test_the_poll_with_the_files_read_stays_inside_the_hourly_budget_for_1_5_and_20_repos(tw, n, capsys):
    points_for(tw, n)
    pts = tw.gh.points()
    files = sum(1 for p in tw.gh.paths() if "/files?" in p)
    gql = len(tw.gh.graph_calls())
    with capsys.disabled():
        print(f"\nPOINTS_PER_HOUR_P3 repos={n} points={pts} file_reads={files} graphql_calls={gql} (7 open pull requests a repository, 6 of them bob's with files)")
    assert pts <= team_github.BUDGET_PER_HOUR + 8, "the whole team poll, files included, stays at its budget however many repositories are shared"
    assert files <= team_github.FILES_BUDGET + 8, "the changed-files reads keep to their own share of it"
    read_ok = [r for r in tw.body()["repos"] if r["shared"]]
    assert len(read_ok) == n and all(r["state"] == "ok" and r["read_at"] for r in read_ok), "every repository was still read within the hour"
    if n == 1:
        assert files >= 7, "one repository has its few lists read in the first hour"


# ================================================================ off means off, and opening the launcher asks GitHub for nothing

def test_with_nothing_shared_the_state_has_no_warnings_no_task_is_read_no_call_is_made(tw, monkeypatch):
    seed(tw.gh)
    local_task(tw, issue_number=1)
    spy = []
    monkeypatch.setattr(overlap, "task_files", lambda t, **kw: spy.append(t) or ["x"])
    monkeypatch.setattr(tw.db, "tasks", lambda *a, **k: spy.append("tasks") or [])
    r = tw.c.get("/api/team/state", headers=ID)
    body = r.json()
    assert r.status_code == 200 and body["enabled"] is False and all(x["warnings"] is None and x["shared"] is False for x in body["repos"])
    assert spy == [] and tw.gh.calls() == [] and tw.poller.thread is None and tw.poller.reads == {}
    assert "team" not in json.dumps(tw.c.get("/api/state", headers=ID).json()).lower().replace("teammate", ""), "no team key in the page state"


def test_opening_the_state_again_and_again_makes_no_github_call_and_no_write(tw, monkeypatch):
    seed(tw.gh)
    tw.gh.pull_files(REPO, 7, ["a.py"])
    local_task(tw, issue_number=1)
    monkeypatch.setattr(overlap, "changed_files", lambda wt, base: {"a.py"})
    overlap.forget_files()
    tw.share()
    tw.settle()
    tw.run_for(60)
    tw.poller.stop(2.0)
    tw.gh.clear_calls()
    tags = set()
    for _ in range(5):
        r = tw.c.get("/api/team/state", headers=ID)
        assert r.status_code == 200 and r.json()["repos"][0]["warnings"]["tasks"]
        tags.add(r.headers["ETag"])
    assert tw.gh.calls() == [], "the launcher reads the cached state: no GitHub call, no write"
    assert len(tags) == 1, "the same state has the same ETag, so a poll is a 304"
    r = tw.c.get("/api/team/state", headers={**ID, "If-None-Match": tags.pop()})
    assert r.status_code == 304


def test_the_warnings_are_for_a_signed_in_person_only(tw):
    seed(tw.gh)
    tw.share()
    for headers in ({}, {"Authorization": "Bearer " + "ccbnode_" + "x" * 30}, {"X-CCBoard-Hook": "x" * 20}):
        assert tw.c.get("/api/team/state", headers=headers).status_code in (401, 403)
    assert tw.c.get("/api/team/state", headers=ID).status_code == 200


# ================================================================ a warning never blocks a dispatch and nothing is written

def test_with_every_warning_present_the_task_is_created_and_nothing_is_written(tw, monkeypatch):
    seed(tw.gh)
    tw.gh.pull_files(REPO, 7, ["app/login.py"])
    tw.gh.comment(f"{REPO}#1", "<!-- ccboard:claim v1 -->\nccboard claim: alias=node-b nid=b1b2c3d4 lease=30m seq=1 status=running attempt=1", user="bob")
    monkeypatch.setattr(overlap, "changed_files", lambda wt, base: {"app/login.py"})
    overlap.forget_files()
    local_task(tw, issue_number=1, branch="bob/fix-login")
    tw.share()
    tw.settle()
    tw.run_for(60)
    w = state(tw)["warnings"]
    assert w["issues"]["1"] and w["branches"]["bob/fix-login"] and any(t["files"] for t in w["tasks"].values()), "every kind of warning is present"
    tw.poller.stop(2.0)
    tw.gh.clear_calls()
    r = tw.c.post("/api/tasks", headers=H, json={"project": "shop", "repo": "api", "title": "Same issue anyway", "prompt": "do it", "when": "later", "issue_number": 1,
                                                  "issue_url": "https://github.com/acme/shop/issues/1"})
    assert r.status_code == 201 and r.json()["task"]["issue_number"] == 1, "Start anyway is the same primary action as before"
    again = tw.c.get("/api/team/state", headers=ID)
    assert again.status_code == 200
    assert tw.gh.writes() == [] and all(c["argv"][-1] != "graphql" for c in tw.gh.calls() if c["argv"][:2] != ["api", "graphql"]), "no write of any kind reached GitHub"
    assert tw.gh.calls() == [], "creating the task and reading the warnings again asked GitHub for nothing"


def test_the_whole_poll_with_every_warning_wrote_nothing_to_github(tw):
    seed(tw.gh)
    tw.gh.pull_files(REPO, 7, ["a.py"])
    tw.gh.comment(f"{REPO}#1", "<!-- ccboard:claim v1 -->\nccboard claim: alias=node-b nid=b1b2c3d4 lease=30m seq=1 status=running attempt=1", user="bob")
    tw.share()
    tw.settle()
    tw.run_for(3600)
    assert tw.gh.writes() == [] and tw.gh.calls()
    for c in tw.gh.calls():
        a = c["argv"]
        assert a[:2] == ["api", "graphql"] or (a[:2] == ["api", "--method"] and a[2] == "GET" and github.allowed_path(a[-1])), a


# ================================================================ local file names stay on this board

def test_local_file_names_are_in_the_owners_state_and_nowhere_in_what_the_board_publishes(tw, monkeypatch):
    seed(tw.gh)
    tw.gh.pull_files(REPO, 7, ["secret/plan.py"])
    tid = local_task(tw, issue_number=1)
    monkeypatch.setattr(overlap, "changed_files", lambda wt, base: {"secret/plan.py", "secret/only-local.py"})
    overlap.forget_files()
    tw.share()
    tw.settle()
    tw.run_for(60)
    assert "secret/plan.py" in json.dumps(state(tw)["warnings"])
    rows = json.dumps(tw.main._presence_rows())
    assert tid and "secret" not in rows and "only-local" not in rows, "the rows the presence publisher picks from carry no local file name"
    import inspect
    from app import team_presence
    src = inspect.getsource(team_presence)
    assert "overlap" not in src and "team.conflicts" not in src and "path_of" not in src


def test_the_files_of_a_task_are_cached_a_minute_and_a_task_without_a_worktree_is_unknown(tmp_path, monkeypatch):
    overlap.forget_files()
    calls = []
    monkeypatch.setattr(overlap, "changed_files", lambda wt, base: calls.append((wt, base)) or {"b.py", "a.py"})
    wt = tmp_path / "wt"
    wt.mkdir()
    t = {"id": 1, "worktree": str(wt), "base": "main"}
    clock = [100.0]
    assert overlap.task_files(t, mono=lambda: clock[0]) == ["a.py", "b.py"]
    overlap.task_files(t, mono=lambda: clock[0] + 30)
    assert len(calls) == 1
    overlap.task_files(t, mono=lambda: clock[0] + 61)
    assert len(calls) == 2
    assert overlap.task_files({"id": 2, "worktree": ""}) is None and overlap.task_files({"id": 3, "worktree": str(tmp_path / "gone")}) is None
    overlap.forget_files()


# ================================================================ review fixes: one bad files page, patch text, who may warn, the cap, a stale line, the key range, the reserve

@pytest.mark.parametrize("status", [422, 502])
def test_one_pull_request_whose_files_fail_does_not_block_the_others_and_is_not_asked_every_cycle(tw, status):
    seed(tw.gh)
    for n in (8, 9):
        tw.gh.pull(REPO, n, f"PR {n}", user="bob", head_ref=f"b{n}")
        tw.gh.pull_files(REPO, n, [f"f{n}.py"])
    tw.gh.pull_files(REPO, 7, ["a.py"])
    tw.gh.fail("/pulls/7/files", status, times=-1)                            # the lowest number is the first one asked, and it always fails
    tw.share()
    tw.settle()
    tw.run_for(120)
    pf = tw.poller.snapshot()[KEY]["pull_files"]
    assert pf[8]["files"] == ["f8.py"] and pf[9]["files"] == ["f9.py"], "the pull requests after the failing one still get their files"
    assert pf[7]["files"] is None and pf[7]["checked"] > 0, "the failing one is unknown (not empty) and has a checked time"
    assert len(files_calls(tw, 7)) == 1
    tw.run_for(team_github.FILES_REVALIDATE_S - 600)
    assert len(files_calls(tw, 7)) == 1, "not asked again before the revalidation time"
    tw.run_for(900)
    assert len(files_calls(tw, 7)) == 2, "asked again after it"


def test_a_files_page_is_kept_as_names_only_and_a_304_still_returns_them():
    patch = "+" + "x" * 50000
    page = [{"filename": "a.py", "previous_filename": "o.py", "status": "renamed", "patch": patch, "sha": "1"}, {"filename": "b.py", "patch": patch}, "junk"]
    path = github.pull_files_path("acme", "shop", 7)
    asked = []

    def fetch(p, etag):
        asked.append(etag)
        return (304, {"etag": 'W/"t1"'}, {}) if etag else (200, {"etag": 'W/"t1"'}, page)

    clock = [100.0]
    r = github.Reader(fetch=fetch, mono=lambda: clock[0])
    rep = r.get(path)
    assert rep.data == [{"filename": "a.py", "previous_filename": "o.py"}, {"filename": "b.py"}], "what the caller gets holds no patch text either"
    assert r.cache[path].data == rep.data and "patch" not in json.dumps(r.cache[path].data)
    assert team_github.parse_pull_files(rep.data)[0] == ["a.py", "o.py", "b.py"]
    clock[0] += 120
    again = r.get(path)
    assert again.source == "revalidated" and asked == [None, 'W/"t1"'] and [f["filename"] for f in again.data] == ["a.py", "b.py"]
    r2 = github.Reader(fetch=lambda p, e: (200, {}, [{"filename": "a", "patch": "keep"}]), mono=lambda: 0.0)
    assert r2.get(github.issue_path("acme", "shop", 7)).data == [{"filename": "a", "patch": "keep"}], "any other path is cached whole"


def _assoc_pull(n, **kw):
    return {"number": n, "title": "x", "draft": False, "author": BOB, "association": "COLLABORATOR", "head_ref": f"b{n}", "head_repo": REPO, "fork": False, "updated_at": RECENT,
            "closes": [212], **kw}


STRANGER = {"id": 9, "login": "mallory"}
BOT = {"id": 10, "login": "ci-helper[bot]"}


@pytest.mark.parametrize("pr", [_assoc_pull(7, association="NONE", author=STRANGER), _assoc_pull(7, association="CONTRIBUTOR"), _assoc_pull(7, association=None),
                                _assoc_pull(7, association="FIRST_TIMER"), {k: v for k, v in _assoc_pull(7).items() if k != "association"},
                                _assoc_pull(7, author=BOT), _assoc_pull(7, author={"id": 10, "login": "CI-Helper[BOT]"})])
def test_a_pull_request_from_a_stranger_or_a_bot_makes_no_taken_issue_and_no_file_warning(pr):
    rd = read(pulls=[pr], pull_files={7: {"files": ["a.py"], "more": False}})
    w = warn(rd, local=[task(1, files=["a.py"])])
    assert w["issues"] == {} and w["tasks"] == {} and w["files"]["checked"] == 0 and w["files"]["pending"] == 0
    assert not team.trusted_pull(pr)
    people = block(rd)["people"]
    assert [p["prs"][0]["number"] for p in people if p["prs"]] == [7], "the read-only Team block still shows it"


@pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_a_pull_request_from_someone_who_can_write_still_warns(assoc):
    rd = read(pulls=[_assoc_pull(7, association=assoc)], pull_files={7: {"files": ["a.py"], "more": False}})
    w = warn(rd, local=[task(1, files=["a.py"])])
    assert one(w, 212)["signals"][0]["kind"] == "pr" and w["tasks"]["1"]["files"][0]["files"] == ["a.py"]


def test_parse_pulls_keeps_the_author_association_as_clean_text():
    bob = {"id": 2, "login": "bob"}
    got = team_github.parse_pulls([{"number": 1, "user": bob, "author_association": "MEMBER"}, {"number": 2, "user": bob},
                                   {"number": 3, "user": bob, "author_association": "NONE\x00" + "Z" * 100}], "acme/shop")
    assert [g["association"] for g in got] == ["MEMBER", None, "NONE" + "Z" * 26]


def test_only_pull_requests_of_people_who_can_write_have_their_files_read(tw):
    seed(tw.gh)
    tw.gh.pull(REPO, 8, "From a stranger", user="mallory", head_ref="m8", assoc="NONE")
    tw.gh.pull(REPO, 9, "From a bot", user="ci-helper[bot]", head_ref="b9", assoc="COLLABORATOR")
    for n in (7, 8, 9):
        tw.gh.pull_files(REPO, n, [f"f{n}.py"])
    tw.share()
    tw.settle()
    tw.run_for(120)
    assert files_calls(tw, 7) and not files_calls(tw, 8) and not files_calls(tw, 9), "a stranger's or a bot's pull request costs no call"
    assert {7, 8, 9} <= {p["number"] for p in tw.poller.snapshot()[KEY]["pulls"]}, "but it is still listed for the read-only block"


def _slow_first_diff(tmp_path, monkeypatch):
    """A diff whose first call waits for `gate` (at most 5 s) and every later call answers at once; returns (task, gate, calls)."""
    gate, calls = threading.Event(), []

    def changed(wt, base):
        calls.append(wt)
        if len(calls) == 1:
            gate.wait(5)
        return {"a.py"}

    monkeypatch.setattr(overlap, "changed_files", changed)
    overlap.forget_files()
    wt = tmp_path / "wt"
    wt.mkdir()
    return {"id": 1, "worktree": str(wt), "base": "main"}, gate, calls


def test_two_pollers_of_the_same_task_run_the_diff_once(tmp_path, monkeypatch):
    t, gate, calls = _slow_first_diff(tmp_path, monkeypatch)
    out: list = []
    threads = [threading.Thread(target=lambda: out.append(overlap.task_files(t)), daemon=True) for _ in range(4)]
    for th in threads:
        th.start()
    for _ in range(300):
        if calls:
            break
        time.sleep(0.01)
    time.sleep(0.2)                                              # the others have had time to arrive while the first diff is still running
    gate.set()
    for th in threads:
        th.join(5)
        assert not th.is_alive()
    assert out == [["a.py"]] * 4 and len(calls) == 1, "one diff, every caller got its answer"
    assert overlap._files_inflight == {}
    overlap.forget_files()


def test_a_diff_that_never_ends_is_not_waited_on_forever(tmp_path, monkeypatch):
    t, gate, calls = _slow_first_diff(tmp_path, monkeypatch)
    monkeypatch.setattr(overlap, "FILES_WAIT_S", 0.2)
    first = threading.Thread(target=lambda: overlap.task_files(t), daemon=True)
    first.start()
    for _ in range(300):
        if calls:
            break
        time.sleep(0.01)
    assert overlap.task_files(t) == ["a.py"] and len(calls) == 2, "the second caller gave up waiting after FILES_WAIT_S and ran the diff itself"
    gate.set()
    first.join(5)
    assert not first.is_alive()
    overlap.forget_files()


def test_the_files_cache_drops_its_oldest_entries_not_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(overlap, "changed_files", lambda wt, base: {"a.py"})
    monkeypatch.setattr(overlap, "FILES_CACHE_MAX", 3)
    overlap.forget_files()
    wt = tmp_path / "wt"
    wt.mkdir()
    for i in range(1, 6):
        overlap.task_files({"id": i, "worktree": str(wt), "base": "main"})
    assert list(overlap._files_cache) == [3, 4, 5], "the oldest went, the newest stayed"
    overlap.forget_files()


def test_one_request_looks_at_the_files_of_the_newest_hundred_tasks_only(tw, monkeypatch):
    seed(tw.gh)
    tw.gh.pull_files(REPO, 7, ["a.py"])
    ids = [tw.db.task_add(project="shop", repo="api", slug=f"s{i}", title=f"T{i}", prompt="p", branch=f"alice/b{i}", tmux_name=f"shop--api--s{i}", phase="running") for i in range(130)]
    seen: list = []
    monkeypatch.setattr(overlap, "task_files", lambda t, **kw: seen.append(t["id"]) or ["a.py"])
    tw.share()
    tw.settle()
    tw.run_for(60)
    seen.clear()
    assert tw.c.get("/api/team/state", headers=ID).status_code == 200
    assert len(seen) == team.LOCAL_TASKS_MAX == 100 and set(seen) == set(sorted(ids)[-100:]), "the newest hundred, nothing older is scanned"


def test_a_taken_issue_with_an_old_status_line_says_how_old_and_not_since_when():
    stale = pline(BOB, 212, age=6 * 3600)                                   # past the lease: stale
    got = one(warn(read(issues=[issue(212, BOB)], presence=presence(stale))), 212)
    assert got["status"] == "taken" and got["since"] is None and got["since_from"] is None, "an old line is not a start time"
    assert got["stale_age_s"] == got["age_s"] and 6 * 3600 - 5 <= got["stale_age_s"] <= 6 * 3600 + 5
    live = one(warn(read(issues=[issue(212, BOB)], presence=presence(pline(BOB, 212, age=60)))), 212)
    assert live["since_from"] == "presence" and live["since"] and live["stale_age_s"] is None
    none = one(warn(read(issues=[issue(212, BOB)])), 212)
    assert none["stale_age_s"] is None and none["age_s"] is None
    only_stale = one(warn(read(presence=presence(stale))), 212)
    assert only_stale["status"] == "stale" and only_stale["since"] is None and only_stale["stale_age_s"] is None, "a stale-only warning already says its age through age_s"


def test_the_kv_prefix_read_is_a_key_range_that_uses_the_index(tw):
    for k in ("team:share:b", "team:share:a", "team:share;x", "team:sharf", "team:shar", "TEAM:share:z", "team:share", "team:share:\U0001F600"):
        tw.db.kv_set(k, {"on": True})
    got = [k for k, _, _ in tw.db.kv_list_prefix("team:share:")]
    assert got == ["team:share:a", "team:share:b", "team:share:\U0001F600"], "exactly the keys under the prefix, in key order"
    sql, args = tw.db._prefix_query("team:share:")
    assert "key >= ?" in sql and "key < ?" in sql and "substr" not in sql and args == ("team:share:", "team:share;")
    plan = " ".join(str(r[-1]) for r in tw.db.conn.execute("EXPLAIN QUERY PLAN " + sql, args).fetchall())
    assert "SEARCH" in plan and "SCAN" not in plan, plan
    every = {k for k, _, _ in tw.db.kv_list_prefix("")}
    assert {"TEAM:share:z", "team:sharf", "team:share"} <= every, "an empty prefix is every row"


def test_the_files_read_stops_when_the_core_reads_would_lose_their_reserve(tw):
    now = tw.poller.mono()
    tw.poller.budget.add(now, team_github.BUDGET_PER_HOUR - team_github.FILES_RESERVE - 1)
    assert tw.poller._files_room() is True, "one more page still leaves the reserve whole"
    tw.poller.budget.add(now, 1)
    assert tw.poller._files_room() is False, "the next page would eat into the core reads' share"
    assert tw.poller.budget.room(now, 1), "the core reads themselves still have room"


def test_the_public_repository_hygiene_of_the_new_code():
    root = Path(team.__file__).parent.parent
    dash = chr(0x2014)
    for f in ("app/team.py", "app/team_github.py", "app/overlap.py", "tests/test_team_conflicts.py"):
        text = (root / f).read_text()
        mails = [m for m in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text) if not m.endswith(("@github.com", "@example.com"))]
        assert not mails, (f, mails)
        assert dash not in text, f"{f}: no em-dash"
        assert "/" + "Users/" not in text and "/" + "home/" not in text, f
