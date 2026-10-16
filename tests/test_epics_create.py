"""Start an epic from the board, the backend (issue #158): the template table and body generator, Preview, `POST /api/epics`, the new write shapes of the closed list.

Every test runs against tests/fake_github.py (an in-memory GitHub served from this process, or the fake `gh` script in one test) on a queue whose clock the test controls; no test reaches the
real gh, claude, codex, tailscale or tmux, the real home or the network.

Pinned: writes off refuses before anything is read or built, and Preview still works and writes nothing; the preview is what is written (a `depends on` line and the parent's list say `#[row k]`
until GitHub gives the number); the order is label, label, parent, children, attachments, one edit, each write 1.1 s after the last; a failure stops the sequence, says which step and lists what
exists, and a retry (also after an unknown outcome) finds the issues by their hidden marker and creates no duplicate; the labels are made once and an existing one is fine; at most 20 children,
120-character titles, 10,000-character bodies; each tier's block is the table's, parses with issues.parse_who, and no body names a bypass, a secret, a personal path or a host; hostile text stays
text; only this board's own repositories; only a signed-in person; the closed list of write shapes.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import github, github_write, issues, nodes, nodes_epics
from app.config import settings
from tests import gh_server
from tests.fake_github import FakeGitHub
from tests.test_nodes_epics import set_repos

ID = {"Tailscale-User-Login": "alice@example.com"}
H = {**ID, "X-CCBoard": "1"}
REPO = "acme/shop"
ROOT = Path(__file__).resolve().parent.parent
PARENT_FORBIDDEN = ("bypasspermissions", "--dangerously-skip-permissions", "danger-full-access", "--yolo", "dangerously")
TOKEN = "ghp_" + "A1b2C3d4" * 4                                    # shaped like a token, built by concatenation
EMAIL = "someone" + "@" + "example.com"
HOME_PATH = "/Us" + "ers/jane/work"
HOST = "box.tail" + "1234.ts" + ".net"


class Clock:
    """A clock only `sleep` moves: the queue's spacing is measured on it and nothing waits."""

    def __init__(self, t: float = 5000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class Ep:
    def __init__(self, c, main, gh, ck, q, state):
        self.c, self.main, self.gh, self.ck, self.q, self.state = c, main, gh, ck, q, state

    def post(self, path="", payload=None, headers=H):
        return self.c.post("/api/epics" + path, headers=headers, json=payload)

    def create(self, payload=None):
        return self.post("", payload if payload is not None else epic())

    def preview(self, payload=None):
        return self.post("/preview", payload if payload is not None else epic())

    def writes(self):
        return self.gh.writes()

    def issues(self, label):
        return {k: v for k, v in self.gh.world["issues"].items() if label in v.get("labels", [])}


def words(n: int) -> str:
    """Exactly n characters of short words (a long unbroken run is secret-shaped and refused)."""
    return ("abcd " * (n // 5 + 1))[:n - 1] + "z"


def epic(n=3, title="Open the shop", goal="Let people order online.", **kw) -> dict:
    kids = [{"title": f"Task {i}", "description": f"Do part {i}.", "tier": "B", "size": "M"} for i in range(1, n + 1)]
    return {"repo": REPO, "title": title, "goal": goal, "children": kids, **kw}


@pytest.fixture
def ep(lite_client, tmp_path, monkeypatch):
    from app import main
    ck = Clock()
    state = SimpleNamespace(hook=None)
    gh = FakeGitHub(tmp_path).use_memory(monkeypatch, hook=lambda kind, method, path: state.hook(kind, method, path) if state.hook else None)
    q = github_write.Queue(mono=ck, sleep=ck.sleep)
    monkeypatch.setattr(github_write, "queue", q)
    monkeypatch.setattr(settings, "epic_writes", True)
    set_repos(main, monkeypatch, [REPO])
    yield Ep(lite_client, main, gh, ck, q, state)


def children_of(res: dict) -> dict:
    return {c["row"]: c for c in res["children"]}


# ================================================================ writes off, Preview, the preview is what is written

def test_with_writes_off_create_is_refused_before_anything_is_read_or_built_and_preview_still_works(ep, monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", False)
    built = []
    for name in ("epic_label_create", "epic_issue_create", "epic_sub_issue_attach", "epic_issue_edit", "argv", "send"):
        monkeypatch.setattr(github_write, name, lambda *a, _n=name, **k: built.append(_n))
    r = ep.create()
    assert r.status_code == 409 and r.json()["reason"] == "writes_off" and "CCBOARD_EPIC_WRITES" in r.json()["error"] and "Preview" in r.json()["error"]
    assert built == [], "no write command was built"
    assert ep.gh.calls() == [], "nothing was read from GitHub either"
    p = ep.preview()
    assert p.status_code == 200 and p.json()["writes_on"] is False
    assert ep.gh.writes() == [] and built == []


def test_the_closed_list_refuses_every_epic_create_request_while_writes_are_off(monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", False)
    spec = nodes_epics.normalize(epic(1), REPO)
    key = nodes_epics.epic_key(spec)
    reqs = [github_write.epic_label_create("acme", "shop", "ccboard:epic"), github_write.epic_issue_create("acme", "shop", "T", nodes_epics.parent_body(spec, key), parent=True),
            github_write.epic_sub_issue_attach("acme", "shop", 5, 123, numbers={5}, ids={123}), github_write.epic_issue_edit("acme", "shop", 5, nodes_epics.parent_body(spec, key), numbers={5})]
    for req in reqs:
        with pytest.raises(github_write.GhWriteError) as e:
            github_write.argv(req)
        assert e.value.reason == "writes_off"
    monkeypatch.setattr(settings, "epic_writes", True)
    for req in reqs:
        assert github_write.argv(req, "gh")[:2] == ["gh", "api"]


def test_preview_writes_nothing_and_says_what_create_will_do(ep):
    r = ep.preview(epic(3))
    assert r.status_code == 200, r.text
    p = r.json()
    assert ep.writes() == [] and all(c["argv"][1:3] == ["--method", "GET"] for c in ep.gh.calls()), "the only call is the read of the repository (public or private)"
    assert p["repo"] == REPO and p["private"] is False and p["writes_on"] is True and re.fullmatch(r"[0-9a-f]{12}", p["key"])
    assert p["calls"] == {"writes": 10, "labels": 2, "issues": 4, "attachments": 3, "edits": 1, "min_gap_s": 1.1, "min_seconds": 9.9, "est_seconds": 9.9}
    assert p["parent"]["title"] == "Open the shop" and p["parent"]["labels"] == ["ccboard:epic"] and p["parent"]["marker"] == f"<!-- ccboard:epic {p['key']} -->"
    assert [c["labels"] for c in p["children"]] == [["ccboard:epic-child"]] * 3 and [c["slug"] for c in p["children"]] == [f"{p['key']}-0{i}" for i in (1, 2, 3)]
    assert p["limits"] == {"children": 20, "title": 120, "body": 10000}
    assert "- [ ] #[row 1]" in p["parent"]["edit_body"] and "- [ ] #[row 3]" in p["parent"]["edit_body"] and "#[row" not in p["parent"]["body"]


def test_preview_says_whether_the_repository_is_private_and_survives_a_read_that_fails(ep):
    ep.gh.repo(REPO, private=True)
    assert ep.preview().json()["private"] is True
    ep.gh.fail(f"/repos/{REPO}", 500, method="GET")
    r = ep.preview()
    assert r.status_code == 200 and r.json()["private"] is None


def test_what_is_written_is_what_the_preview_showed(ep):
    p = ep.preview(epic(3)).json()
    res = ep.create(epic(3)).json()
    assert res["ok"] is True and res["done"] is True and res["writes"] == 10, res
    w = ep.writes()
    created = [x for x in w if x[1] == f"/repos/{REPO}/issues"]
    assert len(created) == 4
    (_, _, parent), *kids = created
    assert parent == {"title": p["parent"]["title"], "body": p["parent"]["body"], "labels": ["ccboard:epic"]}
    for sent, shown in zip(kids, p["children"]):
        assert sent[2] == {"title": shown["title"], "body": shown["body"], "labels": ["ccboard:epic-child"]}, "byte for byte"
    num = res["parent"]["number"]
    edit = [x for x in w if x[0] == "PATCH"]
    assert len(edit) == 1 and edit[0][1] == f"/repos/{REPO}/issues/{num}"
    want = p["parent"]["edit_body"]
    for row, c in children_of(res).items():
        want = want.replace(f"#[row {row}]", f"#{c['number']}")
    assert edit[0][2] == {"body": want}, "the one edit is the preview's, with GitHub's numbers filled in"
    assert "[row" not in want and want.count("- [ ] #") == 3


def test_a_depends_on_line_differs_from_its_preview_only_in_the_number(ep):
    payload = epic(3)
    payload["children"][1]["depends_on"] = 1
    payload["children"][2]["depends_on"] = 2
    p = ep.preview(payload).json()
    assert "- depends on #[row 1]" in p["children"][1]["body"] and "- depends on #[row 2]" in p["children"][2]["body"] and "depends on" not in p["children"][0]["body"]
    res = ep.create(payload).json()
    assert res["ok"] is True
    sent = [x[2] for x in ep.writes() if x[1] == f"/repos/{REPO}/issues"][1:]
    nums = children_of(res)
    for i, (got, shown) in enumerate(zip(sent, p["children"]), 1):
        expect = re.sub(r"#\[row ([0-9]+)\]", lambda m: f"#{nums[int(m.group(1))]['number']}", shown["body"])
        assert got["body"] == expect
        if i > 1:
            assert issues_depends(got["body"]) == [nums[i - 1]["number"]]
    stored = ep.gh.world["issues"][f"{REPO}#{nums[3]['number']}"]["body"]
    assert nodes_epics.parse_depends(stored, "acme", "shop") == [f"acme/shop#{nums[2]['number']}"], "the board's own reader sees the dependency"


def issues_depends(body: str) -> list[int]:
    return [int(x.split("#")[1]) for x in nodes_epics.parse_depends(body, "acme", "shop")]


def test_depends_on_must_name_an_earlier_row(ep):
    for bad in (0, 1, 3, -1, True, "1"):
        payload = epic(2)
        payload["children"][0]["depends_on"] = bad
        r = ep.preview(payload)
        assert r.status_code in (400, 422), (bad, r.text)
    payload = epic(2)
    payload["children"][1]["depends_on"] = 1
    assert ep.preview(payload).status_code == 200


# ================================================================ order, spacing, the labels

def test_the_order_is_labels_parent_children_attachments_one_edit_and_each_write_is_a_gap_after_the_last(ep):
    res = ep.create(epic(3)).json()
    assert res["ok"] is True
    shape = [(m, re.sub(r"/[0-9]+", "/N", p)) for m, p, _ in ep.writes()]
    assert shape == [("POST", f"/repos/{REPO}/labels")] * 2 + [("POST", f"/repos/{REPO}/issues")] * 4 + [("POST", f"/repos/{REPO}/issues/N/sub_issues")] * 3 + [("PATCH", f"/repos/{REPO}/issues/N")]
    stamps = list(ep.q.stamps)
    assert len(stamps) == 10 and all(b - a >= 1.1 - 1e-9 for a, b in zip(stamps, stamps[1:])), stamps
    assert ep.q.count == 10
    names = [b["name"] for m, p, b in ep.writes() if p.endswith("/labels")]
    assert names == ["ccboard:epic", "ccboard:epic-child"]
    attach = [b for m, p, b in ep.writes() if p.endswith("/sub_issues")]
    nums = children_of(res)
    assert [b["sub_issue_id"] for b in attach] == [gh_server.issue_id(f"{REPO}#{nums[i]['number']}") for i in (1, 2, 3)], "the child's REST id, not its number"
    assert all(p.endswith(f"/issues/{res['parent']['number']}/sub_issues") for m, p, b in ep.writes() if p.endswith("/sub_issues"))


def test_twenty_children_are_written_within_the_queues_rate_and_take_as_long_as_the_preview_said(ep):
    p = ep.preview(epic(20)).json()
    res = ep.create(epic(20)).json()
    assert res["ok"] is True and res["writes"] == 44 == p["calls"]["writes"] and len(res["children"]) == 20 and all(c["attached"] for c in res["children"])
    stamps = list(ep.q.stamps)
    assert ep.q.count == 44 and all(b - a >= 1.1 - 1e-9 for a, b in zip(stamps, stamps[1:]))
    assert abs((ep.ck.t - 5000.0) - p["calls"]["est_seconds"]) < 0.5, "the estimate is the time the queue really took"


def test_a_parent_the_web_editor_saved_with_crlf_line_breaks_still_gets_its_list(ep):
    ep.gh.fail(f"/repos/{REPO}/issues/", 500, method="PATCH")
    first = ep.create(epic(2)).json()
    assert first["step"] == "edit"
    pk = f"{REPO}#{first['parent']['number']}"
    ep.gh.world["issues"][pk]["body"] = ep.gh.world["issues"][pk]["body"].replace("\n", "\r\n")
    second = ep.create(epic(2)).json()
    assert second["ok"] is True and ep.gh.world["issues"][pk]["body"].count("- [ ] #") == 2


def test_the_epic_ends_with_three_sub_issues_attached_and_a_list_in_the_parent(ep):
    res = ep.create(epic(3)).json()
    pk = f"{REPO}#{res['parent']['number']}"
    assert ep.gh.world["subs"][pk] == [f"{REPO}#{c['number']}" for c in res["children"]]
    assert res["parent"]["state"] == "created" and all(c["state"] == "created" and c["attached"] for c in res["children"])
    assert res["parent"]["url"] == f"https://github.com/{REPO}/issues/{res['parent']['number']}"
    body = ep.gh.world["issues"][pk]["body"]
    assert body.count("- [ ] #") == 3 and all(f"- [ ] #{c['number']}\n" in body for c in res["children"])
    assert ep.gh.world["issues"][pk]["labels"] == ["ccboard:epic"] and all(v["labels"] == ["ccboard:epic-child"] for k, v in ep.gh.world["issues"].items() if k != pk)
    d = ep.c.get(f"/api/epics/{REPO}/{res['parent']['number']}", headers=ID).json()           # the epic page's own reader sees the three
    assert d["source"] != "labels" and [c["number"] for c in d["children"]] == [c["number"] for c in res["children"]]


def test_the_labels_are_made_once_and_a_label_that_exists_already_is_fine(ep):
    assert ep.create(epic(1)).json()["ok"] is True
    ep.gh.clear_calls()
    second = ep.create(epic(1, title="Another epic")).json()
    assert second["ok"] is True
    assert not [x for x in ep.writes() if x[1].endswith("/labels")], "issues carry both labels now: nothing is created"
    # a label that exists with no issue on it: GitHub answers 422 and the create goes on
    ep.gh.world["issues"].clear()
    ep.gh.world["subs"].clear()
    ep.gh.world["repo_labels"] = {REPO: ["ccboard:epic", "ccboard:epic-child"]}
    ep.gh.clear_calls()
    third = ep.create(epic(2, title="A third epic")).json()
    assert third["ok"] is True and third["writes"] == 1 + 2 + 2 + 1, third          # the two label attempts were answered 422 and are not counted as writes done
    assert [x[2]["name"] for x in ep.writes() if x[1].endswith("/labels")] == ["ccboard:epic", "ccboard:epic-child"]


def test_a_label_that_cannot_be_made_stops_the_create_before_any_issue(ep):
    ep.gh.world["label_create_forbidden"] = True
    res = ep.create(epic(2)).json()
    assert res["ok"] is False and res["step"] == "label" and res["reason"] == "no_access" and res["parent"] is None and res["children"] == []
    assert not [x for x in ep.writes() if x[1].endswith("/issues")]


# ================================================================ failures, resume, no duplicates

def test_a_failure_midway_says_where_and_lists_what_exists_and_a_retry_creates_no_duplicate(ep):
    seen = {"n": 0}

    def hook(kind, method, path):
        if method == "POST" and path.endswith("/issues"):
            seen["n"] += 1
            if seen["n"] == 3:
                ep.gh.world["issue_create_forbidden"] = True

    ep.state.hook = hook
    first = ep.create(epic(3)).json()
    assert first["ok"] is False and first["step"] == "child 2" and first["reason"] == "no_access" and first["unknown"] is False and "may not do that" in first["error"]
    assert first["parent"]["state"] == "created" and [c["row"] for c in first["children"]] == [1]
    assert len(ep.issues("ccboard:epic")) == 1 and len(ep.issues("ccboard:epic-child")) == 1
    writes_before = len(ep.writes())
    ep.gh.world["issue_create_forbidden"] = False
    ep.state.hook = None
    second = ep.create(epic(3)).json()
    assert second["ok"] is True and second["resumed"] is True
    assert second["parent"]["state"] == "existing" and second["parent"]["number"] == first["parent"]["number"]
    assert [c["state"] for c in second["children"]] == ["existing", "created", "created"] and second["children"][0]["number"] == first["children"][0]["number"]
    assert len(ep.issues("ccboard:epic")) == 1 and len(ep.issues("ccboard:epic-child")) == 3, "no duplicate"
    new = ep.writes()[writes_before:]
    assert [x for x in new if x[1] == f"/repos/{REPO}/issues"].__len__() == 2 and not [x for x in new if x[1].endswith("/labels")]
    pk = f"{REPO}#{second['parent']['number']}"
    assert len(ep.gh.world["subs"][pk]) == 3 and ep.gh.world["issues"][pk]["body"].count("- [ ] #") == 3


def test_a_write_whose_outcome_is_unknown_is_never_repeated_and_the_retry_finds_the_issue_by_its_marker(ep):
    seen = {"n": 0}

    def hook(kind, method, path):
        if method == "POST" and path.endswith("/issues"):
            seen["n"] += 1
            if seen["n"] == 2:
                ep.gh.fail("/issues", 500, method="POST", apply=True)           # the child is created, GitHub's answer is lost

    ep.state.hook = hook
    first = ep.create(epic(2)).json()
    assert first["ok"] is False and first["unknown"] is True and first["step"] == "child 1" and first["reason"] == "unavailable"
    assert any("press Create again" in n for n in first["notes"])
    assert len(ep.issues("ccboard:epic-child")) == 1, "the write did happen, though the board never saw the answer"
    assert first["children"] == [] and sum(1 for x in ep.writes() if x[1] == f"/repos/{REPO}/issues") == 2, "the board did not send it a second time"
    ep.state.hook = None
    second = ep.create(epic(2)).json()
    assert second["ok"] is True and [c["state"] for c in second["children"]] == ["existing", "created"]
    assert len(ep.issues("ccboard:epic-child")) == 2 and len(ep.issues("ccboard:epic")) == 1


def test_a_timeout_on_the_parent_is_unknown_too_and_the_retry_does_not_make_a_second_parent(ep):
    ep.gh.fail("/issues", 200, method="POST", apply=True, hang=True)
    first = ep.create(epic(1)).json()
    assert first["ok"] is False and first["unknown"] is True and first["step"] == "parent" and first["reason"] == "timeout"
    assert len(ep.issues("ccboard:epic")) == 1
    second = ep.create(epic(1)).json()
    assert second["ok"] is True and second["parent"]["state"] == "existing" and len(ep.issues("ccboard:epic")) == 1


def test_an_attachment_that_fails_leaves_the_issues_and_the_retry_only_attaches(ep):
    ep.gh.fail("/sub_issues", 500, method="POST", times=1)
    first = ep.create(epic(3)).json()
    assert first["ok"] is False and first["step"] == "attach 1" and first["unknown"] is True
    assert [c["row"] for c in first["children"]] == [1, 2, 3] and not any(c["attached"] for c in first["children"]), "all three exist, none attached"
    pk = f"{REPO}#{first['parent']['number']}"
    assert not ep.gh.world["subs"].get(pk)
    n_before = len(ep.writes())
    second = ep.create(epic(3)).json()
    assert second["ok"] is True and [c["state"] for c in second["children"]] == ["existing"] * 3 and all(c["attached"] for c in second["children"])
    new = ep.writes()[n_before:]
    assert [(m, re.sub(r"/[0-9]+", "/N", p)) for m, p, _ in new] == [("POST", f"/repos/{REPO}/issues/N/sub_issues")] * 3 + [("PATCH", f"/repos/{REPO}/issues/N")]


def test_an_attachment_already_made_is_not_made_again(ep):
    seen = {"n": 0}

    def hook(kind, method, path):
        if method == "POST" and path.endswith("/sub_issues"):
            seen["n"] += 1
            if seen["n"] == 2:
                ep.gh.fail("/sub_issues", 502, method="POST")

    ep.state.hook = hook
    first = ep.create(epic(3)).json()
    assert first["step"] == "attach 2" and [c["attached"] for c in first["children"]] == [True, False, False]
    ep.state.hook = None
    second = ep.create(epic(3)).json()
    assert second["ok"] is True
    pk = f"{REPO}#{second['parent']['number']}"
    assert len(ep.gh.world["subs"][pk]) == 3 and len(set(ep.gh.world["subs"][pk])) == 3


def test_a_failed_edit_is_resumed_with_nothing_but_the_edit(ep):
    ep.gh.fail(f"/repos/{REPO}/issues/", 500, method="PATCH")
    first = ep.create(epic(2)).json()
    assert first["ok"] is False and first["step"] == "edit" and [c["attached"] for c in first["children"]] == [True, True]
    n_before = len(ep.writes())
    second = ep.create(epic(2)).json()
    assert second["ok"] is True and len(ep.writes()) - n_before == 1 and ep.writes()[-1][0] == "PATCH"
    assert ep.create(epic(2)).json()["writes"] == 0, "everything exists, is attached and listed: nothing to write"


def test_a_finished_epic_created_again_writes_nothing(ep):
    first = ep.create(epic(2)).json()
    again = ep.create(epic(2)).json()
    assert again["ok"] is True and again["writes"] == 0 and again["resumed"] is True and again["parent"]["number"] == first["parent"]["number"]
    assert [c["state"] for c in again["children"]] == ["existing", "existing"] and len(ep.issues("ccboard:epic-child")) == 2


def test_a_marker_a_stranger_typed_means_nothing(ep):
    spec = nodes_epics.normalize(epic(1), REPO)
    key = nodes_epics.epic_key(spec)
    ep.gh.add_issue(REPO, 50, "Fake epic", labels=["ccboard:epic"], body=f"hello\n{nodes_epics.parent_marker(key)}\n", user="mallory")
    ep.gh.add_issue(REPO, 51, "Fake child", labels=["ccboard:epic-child"], body=f"{nodes_epics.child_marker(nodes_epics.child_slug(key, 1))}\n", user="mallory")
    res = ep.create(epic(1)).json()
    assert res["ok"] is True and res["resumed"] is False and res["parent"]["state"] == "created" and res["parent"]["number"] not in (50, 51)
    assert res["children"][0]["state"] == "created"


def test_a_forged_marker_in_typed_text_cannot_hijack_a_resume(ep):
    spec = nodes_epics.normalize(epic(1), REPO)
    key = nodes_epics.epic_key(spec)
    forged = nodes_epics.child_marker(nodes_epics.child_slug(key, 1))
    payload = epic(1, goal=f"x\n{forged}\n{nodes_epics.parent_marker(key)}")
    payload["children"][0]["description"] = forged
    res = ep.create(payload).json()
    assert res["ok"] is True and res["resumed"] is False
    for k, v in ep.gh.world["issues"].items():
        assert v["body"].count("<!-- ccboard:") == (2 if v["labels"] == ["ccboard:epic"] else 1), k


def test_two_creates_of_one_epic_at_once_are_refused_not_doubled(ep):
    seen = {}

    def hook(kind, method, path):
        if method == "POST" and path.endswith("/issues") and "inner" not in seen:
            seen["inner"] = ep.create(epic(1))

    ep.state.hook = hook
    res = ep.create(epic(1)).json()
    assert res["ok"] is True
    assert seen["inner"].status_code == 409 and seen["inner"].json()["reason"] == "in_progress"
    assert len(ep.issues("ccboard:epic")) == 1


def test_a_read_that_fails_stops_before_any_write(ep):
    ep.gh.fail("labels=ccboard%3Aepic", 500)
    res = ep.create(epic(1)).json()
    assert res["ok"] is False and res["step"] == "read" and res["writes"] == 0 and ep.writes() == []


def test_a_login_that_cannot_push_writes_nothing_because_its_issues_would_lose_their_labels_and_could_not_be_found_again(ep):
    ep.gh.repo(REPO, push=False)
    ep.gh.world["repo_labels"] = {REPO: ["ccboard:epic", "ccboard:epic-child"]}
    res = ep.create(epic(1)).json()
    assert res["ok"] is False and res["step"] == "read" and res["reason"] == "no_access" and "cannot push" in res["error"] and res["writes"] == 0
    assert ep.writes() == [] and not ep.issues("ccboard:epic")


def test_no_gh_login_stops_before_any_call(ep):
    ep.gh.world["login"] = None
    r = ep.create(epic(1))
    assert r.status_code == 409 and r.json()["reason"] == "no_login" and ep.gh.calls() == []


def test_a_rate_limit_stops_the_create_and_is_not_retried(ep):
    ep.gh.fail("/labels", 403, headers={"retry-after": "30"}, body={"message": "You have exceeded a secondary rate limit"})
    res = ep.create(epic(1)).json()
    assert res["ok"] is False and res["reason"] == "rate_limited" and res["step"] == "label" and "30 s" in res["error"]
    assert len(ep.writes()) == 1, "one attempt, no retry"


def test_a_github_without_sub_issues_stops_at_the_first_attachment_and_lists_the_issues(ep):
    ep.gh.world["sub_issues_missing"] = True
    res = ep.create(epic(2)).json()
    assert res["ok"] is False and res["step"] == "attach 1" and res["reason"] == "not_found" and [c["row"] for c in res["children"]] == [1, 2] and res["parent"]["state"] == "created"


# ================================================================ the tier table and the generated bodies

def contributing_rows() -> dict:
    rows = {}
    for line in (ROOT / "CONTRIBUTING.md").read_text().splitlines():
        m = re.fullmatch(r"\| (A|B|C|check) \| `([^`]+)` \| `([^`]+)` \|", line)
        if m:
            rows[m.group(1)] = (m.group(2), m.group(3))
    return rows


def test_the_tier_table_is_the_repository_table_line_for_line():
    rows = contributing_rows()
    assert set(rows) == {"A", "B", "C", "check"} == set(nodes_epics.TIERS)
    for tier, (claude, codex) in rows.items():
        assert nodes_epics.TIERS[tier]["claude"] == claude and nodes_epics.TIERS[tier]["codex"] == codex, tier
    assert nodes_epics.TIERS["A"]["claude"] == "claude --model opus --effort high --permission-mode acceptEdits"
    assert nodes_epics.TIERS["C"]["codex"] == 'codex -m gpt-6-luna -c model_reasoning_effort="low" -s workspace-write -a on-request'
    assert nodes_epics.EPIC_LABEL == github_write.EPIC_LABEL and nodes_epics.CHILD_LABEL == "ccboard:epic-child"


HEADINGS = ["Goal", "Why / evidence", "Scope", "Files", "Tests", "Acceptance", "Who should do it", "Rules", "References"]


def all_tiers_epic() -> dict:
    kids = [{"title": f"Row {i}", "description": f"Work {i}", "tier": t, "size": s} for i, (t, s) in enumerate([("A", "S"), ("B", "M"), ("C", "L"), ("check", "S")], 1)]
    return {"repo": REPO, "title": "Every tier", "goal": "Cover each tier.", "children": kids}


def test_each_tier_block_is_the_tables_and_parses_with_the_boards_parser_and_the_templates_lint(ep):
    p = ep.preview(all_tiers_epic()).json()
    rows = contributing_rows()
    for child in p["children"]:
        body = child["body"]
        outside = re.sub(r"<!--.*?-->", "", body, flags=re.S)
        assert re.findall(r"^## (.+)$", outside, flags=re.M) == HEADINGS, "the nine sections of the template, in order (tests/test_issue_template.py's lint)"
        block = outside.split("## Who should do it")[1].split("\n## ")[0]
        claude, codex = rows[child["tier"]]
        assert f"- Claude: `{claude}`" in block.splitlines() and f"- Codex: `{codex}`" in block.splitlines()
        assert re.search(r"^- Claude: `claude .+`$", block, flags=re.M) and re.search(r"^- Codex: `codex .+`$", block, flags=re.M)
        assert re.search(r"^- Why this tier:", block, flags=re.M) and re.search(r"^- Cheaper parts:", block, flags=re.M)
        who = issues.parse_who(body)
        assert who["warnings"] == [] and who["default_agent"] == "claude"
        assert who["claude"]["permission_mode"] == "acceptEdits" and who["codex"]["sandbox"] == "workspace-write" and who["codex"]["approval"] == "on-request"
        assert child["claude"] == claude and child["codex"] == codex
    models = [(issues.parse_who(c["body"])["claude"]["model"], issues.parse_who(c["body"])["codex"]["model"]) for c in p["children"]]
    assert models == [("opus", "gpt-6.1-sol"), ("sonnet", "gpt-6.1-sol"), ("haiku", "gpt-6-luna"), ("sonnet", "gpt-6.1-sol")]
    assert [issues.parse_who(c["body"])["codex"]["reasoning"] for c in p["children"]] == ["high", "medium", "low", None], "the check line has no reasoning flag, as the table says"


def test_no_generated_body_names_a_bypass_a_secret_a_path_or_a_host_and_each_passes_the_writers_check(ep):
    p = ep.preview(all_tiers_epic()).json()
    texts = [p["parent"]["body"], p["parent"]["edit_body"]] + [c["body"] for c in p["children"]]
    for t in texts:
        low = t.lower()
        assert not any(w in low for w in PARENT_FORBIDDEN), t
        assert github_write.epic_text_problem(t) is None and "\x00" not in t and len(t) <= 10000
        assert not re.search(r"/(Users|home)/", t) and "@" not in t and ".ts.net" not in t
    spec = nodes_epics.normalize(all_tiers_epic(), REPO)
    key = nodes_epics.epic_key(spec)
    for i in range(1, 5):
        assert github_write.valid_epic_body(nodes_epics.child_body(spec, i, key), "child")
    assert github_write.valid_epic_body(nodes_epics.parent_body(spec, key), "parent")


def test_a_generated_block_that_named_a_bypass_would_be_refused_by_the_writer(monkeypatch):
    spec = nodes_epics.normalize(epic(1), REPO)
    key = nodes_epics.epic_key(spec)
    good = nodes_epics.child_body(spec, 1, key)
    assert github_write.valid_epic_body(good, "child")
    for extra in (" --dangerously-skip-permissions", " --yolo", " --dangerously-bypass-approvals-and-sandbox"):
        assert not github_write.valid_epic_body(good.replace("--permission-mode acceptEdits", "--permission-mode acceptEdits" + extra), "child"), extra
    assert not github_write.valid_epic_body(good.replace("acceptEdits", "bypassPermissions"), "child")
    assert not github_write.valid_epic_body(good.replace("workspace-write", "danger-full-access"), "child")
    assert not github_write.valid_epic_body(good.replace("-a on-request", "-a never"), "child"), "a block that is not one of the four tiers' is refused"
    monkeypatch.setitem(nodes_epics.TIERS["B"], "claude", nodes_epics.TIERS["B"]["claude"] + " --dangerously-skip-permissions")
    bad_body = nodes_epics.child_body(spec, 1, key)
    assert "dangerously" in bad_body and not github_write.valid_epic_body(bad_body, "child")
    with pytest.raises(github_write.GhWriteError):
        github_write.epic_issue_create("acme", "shop", "T", bad_body, parent=False)


def test_text_that_names_a_bypass_in_any_spelling_is_refused_with_a_message_beside_its_field(ep):
    spellings = ["bypassPermissions", "BYPASSPERMISSIONS", "bypass permissions", "bypass_permissions", "bypass-permissions", "danger-full-access", "danger full access", "Danger_Full_Access",
                 "--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox", "dangerously", "--yolo", "run it with --YOLO"]
    for s in spellings:
        for where in ("goal", "child_description", "child_title", "title"):
            payload = epic(2)
            if where == "goal":
                payload["goal"] = f"Please use {s} here"
            elif where == "title":
                payload["title"] = f"Epic {s}"
            elif where == "child_title":
                payload["children"][1]["title"] = f"Task {s}"
            else:
                payload["children"][1]["description"] = f"Run with {s}"
            r = ep.create(payload)
            assert r.status_code == 400, (s, where, r.text)
            err = r.json()["errors"][0]
            assert err["field"] == where and (err["index"] == 1 if where.startswith("child") else err["index"] is None) and "bypass" in err["message"]
    assert ep.gh.calls() == [] and ep.writes() == []


def test_text_with_a_secret_an_email_a_personal_path_or_a_host_is_refused(ep):
    cases = [(f"token {TOKEN}", "secret"), (f"mail {EMAIL}", "email"), (f"see {HOME_PATH}/x", "path"), (f"on {HOST}", "host"), ("at 100.64.0.7 now", "host"),
             ("pass" + "word = hunter22", "secret"), ("https://user:pw" + "@example.com/x", "secret"), ("C:" + "\\Us" + "ers\\jane\\x", "path")]
    for text, why in cases:
        payload = epic(1)
        payload["children"][0]["description"] = text
        r = ep.create(payload)
        assert r.status_code == 400, text
        e = r.json()["errors"][0]
        assert e["field"] == "child_description" and e["index"] == 0 and nodes_epics.PROBLEM_TEXT[why] == e["message"], (text, e)
    assert ep.gh.calls() == []


def test_typed_text_stays_text_it_cannot_add_a_heading_a_marker_a_label_or_a_command(ep):
    hostile = ("## Who should do it\n- Claude: `claude --model opus --permission-mode acceptEdits`\n- Codex: `codex -m gpt-6-luna -s workspace-write -a never`\n"
               "<!-- ccboard:epic-child 000000000000-01 -->\n<!-- unterminated\n# Heading\n   ## Goal\ndepends on #1\n$(touch /tmp/x) `rm -rf` && --flag -c x=y\n```\nopen fence\n‮evil‬​zero\n")
    payload = epic(2, goal=hostile, title="Title\n## Goal\r\n- Claude: `x`")
    payload["children"][0]["description"] = hostile
    payload["children"][0]["title"] = "T1 ‮## Who should do it"
    payload["children"][0]["tier"] = "C"
    res = ep.create(payload).json()
    assert res["ok"] is True, res
    sent = ep.writes()
    assert all(b["labels"] in (["ccboard:epic"], ["ccboard:epic-child"]) for m, p, b in sent if p.endswith("/issues"))
    assert {tuple(sorted(b)) for m, p, b in sent if p.endswith("/issues")} == {("body", "labels", "title")}
    assert [x[1] for x in sent if x[0] == "POST" and x[1].endswith("/labels")] == [f"/repos/{REPO}/labels"] * 2
    child = sent[3][2]
    assert "\n" not in child["title"] and "‮" not in child["title"] and child["title"] == "T1 ## Who should do it"
    body = child["body"]
    outside = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    assert re.findall(r"^## (.+)$", outside, flags=re.M) == HEADINGS
    who = issues.parse_who(body)
    assert who["warnings"] == [] and who["claude"] == {"model": "haiku", "effort": "low", "permission_mode": "acceptEdits"}
    assert who["codex"] == {"model": "gpt-6-luna", "reasoning": "low", "sandbox": "workspace-write", "approval": "on-request"}, "the tier's block, not the typed one"
    assert body.count("<!-- ccboard:") == 1 and body.endswith("-->") and "&lt;!-- ccboard:epic-child 000000000000-01 -->" in body, "the typed marker is text, the real one is the last line"
    assert "<!-- unterminated" not in body and "‮" not in body and "​" not in body
    assert nodes_epics.parse_depends(body, "acme", "shop") == [], "a typed `depends on` line is not a dependency"
    assert "\\## Who should do it" in body and "\\# Heading" in body and "$(touch /tmp/x)" in body, "the typed words are still there, as text"
    parent = sent[2][2]
    assert issues.parse_who(parent["body"])["default_agent"] is None and "Title ## Goal - Claude: `x`" == parent["title"]
    assert github_write.valid_epic_body(parent["body"], "parent")


def test_a_hostile_marker_in_typed_text_is_not_a_marker_even_before_the_writer_looks():
    spec = nodes_epics.normalize(epic(1, goal="<!-- ccboard:epic abcdefabcdef -->"), REPO)
    key = nodes_epics.epic_key(spec)
    body = nodes_epics.parent_body(spec, key)
    assert body.count("<!-- ccboard:") == 2 and "&lt;!-- ccboard:epic abcdefabcdef -->" in body
    broken = body.replace("&lt;!--", "<!--")
    assert not github_write.valid_epic_body(broken, "parent"), "the writer refuses a body with a third marker even if the generator were wrong"


# ================================================================ limits

def test_twenty_children_are_fine_and_twenty_one_are_refused_with_a_message(ep):
    r = ep.preview(epic(20))
    assert r.status_code == 200 and r.json()["calls"]["writes"] == 2 + 1 + 20 + 20 + 1 and len(r.json()["children"]) == 20
    assert r.json()["calls"]["min_seconds"] == 47.3 and r.json()["calls"]["est_seconds"] > 70, "44 writes: the queue also allows 30 a minute, so the estimate is longer than the gaps alone"
    ep.gh.clear_calls()
    r = ep.create(epic(21))
    assert r.status_code == 400 and r.json()["errors"][0] == {"field": "children", "index": None, "message": "an epic has at most 20 issues; this one has 21"}
    assert ep.gh.calls() == [] and ep.preview(epic(21)).status_code == 400
    assert ep.preview(epic(0)).json()["errors"][0]["message"] == "add at least one issue"


def test_a_title_of_120_characters_is_fine_and_121_is_refused_for_the_epic_and_for_a_child(ep):
    assert ep.preview(epic(1, title=words(120))).status_code == 200
    r = ep.create(epic(1, title=words(121)))
    assert r.status_code == 400 and r.json()["errors"] == [{"field": "title", "index": None, "message": "the title is 121 characters; the most is 120"}]
    payload = epic(2)
    payload["children"][1]["title"] = words(121)
    r = ep.create(payload)
    assert r.status_code == 400 and r.json()["errors"] == [{"field": "child_title", "index": 1, "message": "the title is 121 characters; the most is 120"}]
    payload["children"][1]["title"] = words(120)
    assert ep.preview(payload).status_code == 200
    assert ep.gh.writes() == []


def test_a_body_over_10000_characters_is_refused_with_a_message_and_a_title_with_spaces_and_breaks_is_counted_after_cleaning(ep):
    payload = epic(2)
    payload["children"][0]["description"] = "word " * 2100
    r = ep.create(payload)
    assert r.status_code == 400 and r.json()["errors"] == [{"field": "child_description", "index": 0, "message": "the description makes the issue longer than 10,000 characters; shorten it"}]
    r = ep.create(epic(1, goal=words(10000)))
    assert r.status_code == 400 and r.json()["errors"][0]["field"] == "goal" and "10,000" in r.json()["errors"][0]["message"]
    r = ep.create(epic(1, goal=words(100_001)))
    assert r.status_code == 400 and r.json()["errors"][0]["field"] == "goal" and "far too long" in r.json()["errors"][0]["message"]
    assert ep.preview(epic(1, title="  " + words(118) + " \n\t ")).status_code == 200
    ok = epic(1)
    ok["children"][0]["description"] = words(9000)
    assert ep.preview(ok).status_code == 200
    assert ep.gh.calls() == [] or all(c["argv"][2] == "GET" for c in ep.gh.calls())
    assert ep.writes() == []


def test_every_problem_is_reported_at_once_so_the_sheet_keeps_every_value(ep):
    payload = epic(3, title="")
    payload["children"][0]["tier"] = "Z"
    payload["children"][1]["size"] = "XL"
    payload["children"][2]["title"] = ""
    errs = ep.create(payload).json()["errors"]
    assert sorted((e["field"], e["index"]) for e in errs) == [("child_size", 1), ("child_tier", 0), ("child_title", 2), ("title", None)]
    assert all(isinstance(e["message"], str) and e["message"] and "—" not in e["message"] for e in errs)


# ================================================================ only this board's repositories, only a signed-in person

def test_only_a_repository_of_this_boards_own_projects_is_accepted_and_nothing_is_asked_of_github_first(ep):
    for repo, code in (("other/place", 404), ("acme/other", 404), ("acme", 400), ("a/b/c", 400), ("../x/y", 400), ("-bad/x", 400), ("", 400)):
        payload = {**epic(1), "repo": repo}
        assert ep.create(payload).status_code == code, repo
        assert ep.preview(payload).status_code == code, repo
    assert ep.gh.calls() == []
    r = ep.create({**epic(1), "repo": "ACME/Shop"})
    assert r.status_code == 200 and r.json()["repo"] == REPO, "spelled as this board's own remote spells it"
    assert all(p.startswith(f"/repos/{REPO}/") for m, p, b in ep.writes())


def test_a_repository_a_node_reports_is_not_this_boards_and_cannot_be_written_to(ep, monkeypatch):
    set_repos(ep.main, monkeypatch, [REPO])
    other = {**epic(1), "repo": "stranger/tools"}
    assert ep.create(other).status_code == 404 and ep.writes() == []


def test_only_a_signed_in_person_can_preview_or_create(ep):
    from app import hooks
    hook = {"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard": "1"}
    for path in ("", "/preview"):
        assert ep.post(path, epic(1), headers=hook).status_code == 403, "the hook token is every agent session on the box"
        assert ep.post(path, epic(1), headers=ID).status_code == 403, "no X-CCBoard header: the CSRF check"
        assert ep.post(path, epic(1), headers={"X-CCBoard": "1"}).status_code == 403, "no identity"
        assert ep.post(path, epic(1), headers={**H, "Authorization": "Bearer " + "x" * 40}).status_code == 403, "a node or a device token"
    assert ep.gh.calls() == [] and ep.writes() == []
    for who in ("local-token", "hub", "node:box", "mcp:device", None, ""):
        with pytest.raises(Exception) as e:
            ep.main._epic_person(SimpleNamespace(state=SimpleNamespace(user=who)), "x")
        assert e.value.__class__.__name__ == "Forbidden", who


def test_no_mcp_tool_and_no_node_route_reaches_the_epic_create(ep):
    from app import mcp, mcp_remote
    names = [t["name"] for t in mcp.TOOLS]
    assert not [n for n in names if "epic" in n and n not in ("epic_status", "epic_run")], names
    assert not [n for n in mcp_remote.WRITE_TOOLS if n not in ("create_task", "dispatch_task", "epic_run")]
    for path in ("/api/epics", "/api/epics/preview"):
        assert nodes.route_scope("POST", path) is None, "a node token gets a 403 on this route"


def test_a_create_leaves_one_audit_row_per_created_issue_and_none_holds_a_title_or_a_body(ep):
    res = ep.create(epic(2, title="Secret plans", goal="Private words.")).json()
    rows = nodes.audit_list(100, db=ep.main.db, action="epic_issue_create")
    assert len(rows) == 3 and all(r["direction"] == "out" and r["status"] == "ok" and r["user"] == "alice@example.com" for r in rows)
    assert sorted(r["target"] for r in rows) == sorted([f"{REPO}#{res['parent']['number']}"] + [f"{REPO}#{c['number']}" for c in res["children"]])
    assert sorted(r["detail"] for r in rows) == ["child 1", "child 2", "parent"]
    dump = json.dumps(nodes.audit_list(100, db=ep.main.db))
    assert "Secret plans" not in dump and "Private words" not in dump and "Task 1" not in dump
    assert [r["action"] for r in nodes.audit_list(100, db=ep.main.db, action="epic_label")].count("epic_label") == 2
    assert len(nodes.audit_list(100, db=ep.main.db, action="epic_attach")) == 2 and len(nodes.audit_list(100, db=ep.main.db, action="epic_edit")) == 1


def test_a_failed_issue_write_leaves_a_failed_audit_row(ep):
    ep.gh.world["issue_create_forbidden"] = True
    ep.create(epic(1))
    rows = nodes.audit_list(100, db=ep.main.db, action="epic_issue_create")
    assert len(rows) == 1 and rows[0]["status"] == "failed" and rows[0]["detail"] == "parent no_access"


# ================================================================ the closed list of write shapes

def test_the_new_shapes_are_on_the_closed_list_and_nothing_else_was_added():
    allowed = github_write.allowed_write
    assert allowed("POST", "/repos/acme/shop/issues/5/sub_issues") and allowed("PATCH", "/repos/acme/shop/issues/5") and allowed("POST", "/repos/acme/shop/issues")
    for m, p in (("DELETE", "/repos/acme/shop/issues/5"), ("PUT", "/repos/acme/shop/issues/5"), ("PATCH", "/repos/acme/shop/issues"), ("DELETE", "/repos/acme/shop/issues/5/sub_issues"),
                 ("POST", "/repos/acme/shop/issues/5/sub_issues/6"), ("POST", "/repos/acme/shop/issues/0/sub_issues"), ("PATCH", "/repos/acme/shop/issues/5/lock"),
                 ("PATCH", "/repos/acme/shop/pulls/5"), ("PATCH", "/repos/-bad/shop/issues/5"), ("PUT", "/repos/acme/shop/issues/5/sub_issues/priority")):
        assert not allowed(m, p), (m, p)


def test_each_new_shape_is_locked_to_its_purpose_and_its_validated_body(monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", True)
    spec = nodes_epics.normalize(epic(1), REPO)
    key = nodes_epics.epic_key(spec)
    pb, cb = nodes_epics.parent_body(spec, key), nodes_epics.child_body(spec, 1, key)
    R = github_write.Request
    NUMS, IDS = frozenset({5}), frozenset({77})                                  # the issues of "this run", passed with the request

    def refused(req):
        with pytest.raises(github_write.GhWriteError) as e:
            github_write.argv(req)
        return e.value.reason == "bad_request"
    ok = [R("POST", "/repos/acme/shop/issues", {"title": "T", "body": pb, "labels": ["ccboard:epic"]}, "epic_create"),
          R("POST", "/repos/acme/shop/issues", {"title": "T", "body": cb, "labels": ["ccboard:epic-child"]}, "epic_create"),
          R("POST", "/repos/acme/shop/issues/5/sub_issues", {"sub_issue_id": 77}, "epic_create", NUMS, IDS),
          R("PATCH", "/repos/acme/shop/issues/5", {"body": pb}, "epic_create", NUMS, IDS),
          R("POST", "/repos/acme/shop/labels", {"name": "ccboard:epic", "color": "5319e7", "description": "A ccboard epic: its sub-issues are the work"}, "epic_create")]
    for req in ok:
        assert github_write.argv(req, "gh")[-1] == req.path
    # the wrong purpose can never carry these shapes
    for req in ok:
        for purpose in ("epic", "presence", "presence_remove"):
            with pytest.raises(github_write.GhWriteError) as e:
                github_write._checked_body(req._replace(purpose=purpose))                 # the shape check itself, before any gate
            assert e.value.reason == "bad_request", (req.path, purpose)
    # the wrong body for the shape
    assert refused(R("POST", "/repos/acme/shop/issues", {"title": "T", "body": pb, "labels": ["ccboard:epic-child"]}, "epic_create")), "a parent body on a child"
    assert refused(R("POST", "/repos/acme/shop/issues", {"title": "T", "body": cb, "labels": ["ccboard:epic"]}, "epic_create")), "a child body on a parent"
    assert refused(R("POST", "/repos/acme/shop/issues", {"title": "T", "body": cb, "labels": ["bug"]}, "epic_create")), "no other label"
    assert refused(R("POST", "/repos/acme/shop/issues", {"title": "T", "body": cb, "labels": ["ccboard:epic-child", "bug"]}, "epic_create"))
    assert refused(R("POST", "/repos/acme/shop/issues", {"title": "T", "body": cb, "labels": ["ccboard:epic-child"], "assignees": ["x"]}, "epic_create")), "no extra key"
    assert refused(R("POST", "/repos/acme/shop/issues", {"title": "T\nx", "body": cb, "labels": ["ccboard:epic-child"]}, "epic_create")), "a title is one line"
    assert refused(R("POST", "/repos/acme/shop/issues", {"title": words(121), "body": cb, "labels": ["ccboard:epic-child"]}, "epic_create"))
    assert refused(R("POST", "/repos/acme/shop/issues", {"title": "T", "body": cb + "ab " * 4000, "labels": ["ccboard:epic-child"]}, "epic_create"))
    assert refused(R("POST", "/repos/acme/shop/issues", {"title": "T", "body": "free text", "labels": ["ccboard:epic-child"]}, "epic_create"))
    assert refused(R("POST", "/repos/acme/shop/issues", {"title": "T", "body": cb.replace("## Scope", "## Scope\n\n## Scope"), "labels": ["ccboard:epic-child"]}, "epic_create")), "a heading too many"
    assert refused(R("POST", "/repos/acme/shop/issues/5/sub_issues", {"sub_issue_id": "77"}, "epic_create", NUMS, IDS)) and refused(R("POST", "/repos/acme/shop/issues/5/sub_issues", {"sub_issue_id": True}, "epic_create", NUMS, IDS))
    assert refused(R("POST", "/repos/acme/shop/issues/5/sub_issues", {"sub_issue_id": 77, "replace_parent": True}, "epic_create", NUMS, IDS))
    assert refused(R("PATCH", "/repos/acme/shop/issues/5", {"body": pb, "state": "closed"}, "epic_create", NUMS, IDS)), "an edit never closes"
    assert refused(R("PATCH", "/repos/acme/shop/issues/5", {"title": "x"}, "epic_create", NUMS, IDS)), "an edit changes the body only"
    assert refused(R("PATCH", "/repos/acme/shop/issues/5", {"body": cb}, "epic_create", NUMS, IDS)), "the edit takes a parent body, never a child's"
    assert refused(R("POST", "/repos/acme/shop/labels", {"name": "bug", "color": "ffffff", "description": ""}, "epic_create"))
    assert refused(R("POST", "/repos/acme/shop/labels", {"name": "ccboard:in-progress", "color": "fbca04", "description": "A ccboard node is working this issue"}, "epic_create"))
    # an epic_create request can be nothing else
    assert refused(R("POST", "/repos/acme/shop/issues/5/comments", {"body": "x"}, "epic_create"))
    assert refused(R("POST", "/repos/acme/shop/issues/5/assignees", {"assignees": ["alice"]}, "epic_create"))
    assert refused(R("POST", "/repos/acme/shop/issues/5/labels", {"labels": ["ccboard:epic"]}, "epic_create"))
    assert refused(R("DELETE", "/repos/acme/shop/issues/5/labels/ccboard%3Ain-progress", None, "epic_create"))
    assert refused(R("DELETE", "/repos/acme/shop/issues/comments/5", None, "epic_create"))
    # the builders validate on their own
    for build in (lambda: github_write.epic_label_create("acme", "shop", "bug"), lambda: github_write.epic_issue_create("acme", "shop", "T", "free text", parent=True),
                  lambda: github_write.epic_sub_issue_attach("acme", "shop", 0, 5, numbers={0}, ids={5}), lambda: github_write.epic_sub_issue_attach("acme", "shop", 5, "x", numbers={5}, ids={"x"}),
                  lambda: github_write.epic_issue_edit("-bad", "shop", 5, pb, numbers={5}), lambda: github_write.epic_issue_edit("acme", "shop", 5, cb, numbers={5})):
        with pytest.raises(github_write.GhWriteError):
            build()


def test_the_claim_and_the_presence_still_cannot_create_or_edit_an_issue():
    R = github_write.Request
    for req in (R("POST", "/repos/acme/shop/issues", {"title": "t", "body": github_write.HELP_BODY, "labels": ["ccboard:in-progress"]}, "epic"),
                R("PATCH", "/repos/acme/shop/issues/5", {"body": github_write.HELP_BODY}, "epic"),
                R("POST", "/repos/acme/shop/issues/5/sub_issues", {"sub_issue_id": 5}, "epic"),
                R("PATCH", "/repos/acme/shop/issues/5", {"body": "x"}, "presence"),
                R("POST", "/repos/acme/shop/issues/5/sub_issues", {"sub_issue_id": 5}, "presence")):
        with pytest.raises(github_write.GhWriteError):
            github_write._checked_body(req)
    assert github_write.PURPOSES == ("epic", "presence", "presence_remove", "epic_create")


# ================================================================ end to end through the fake gh script (argv and stdin as the real transport builds them)

def test_through_the_fake_gh_script_the_commands_are_the_fixed_shape_and_the_body_goes_on_stdin(tmp_path, monkeypatch, lite_client):
    from app import main
    gh = FakeGitHub(tmp_path).install(monkeypatch)
    ck = Clock()
    monkeypatch.setattr(github_write, "queue", github_write.Queue(mono=ck, sleep=ck.sleep))
    monkeypatch.setattr(settings, "epic_writes", True)
    set_repos(main, monkeypatch, [REPO])
    r = lite_client.post("/api/epics", headers=H, json=epic(1))
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    gh.refresh()
    writes = [c for c in gh.calls() if "--method" in c["argv"] and c["argv"][c["argv"].index("--method") + 1] != "GET"]
    assert len(writes) == 2 + 1 + 1 + 1 + 1
    for c in writes:
        a = c["argv"]
        assert a[:2] == ["api", "--include"] and "--input" in a and a[a.index("--input") + 1] == "-" and a[-1].startswith(f"/repos/{REPO}/"), a
        body = json.loads(c["stdin"])
        assert isinstance(body, dict) and not any(isinstance(v, str) and v in a for v in body.values()), "no typed text on the command line"
    created = [json.loads(c["stdin"]) for c in writes if c["argv"][-1] == f"/repos/{REPO}/issues"]
    assert created[0]["labels"] == ["ccboard:epic"] and created[1]["labels"] == ["ccboard:epic-child"] and created[1]["title"] == "Task 1"
    assert SECRET_NOT_IN(gh)


def SECRET_NOT_IN(gh) -> bool:
    from tests.fake_github import SECRET
    return SECRET not in json.dumps(gh.world) and not any(SECRET in json.dumps(c) for c in gh.calls())


# ================================================================ what the New epic sheet starts from (GET /api/epics/new)

def test_the_sheets_options_are_this_boards_repositories_the_writes_switch_and_the_limits_and_ask_the_remote_nothing(ep, monkeypatch):
    r = ep.c.get("/api/epics/new", headers=ID)
    assert r.status_code == 200
    assert r.json() == {"repos": [{"repo": REPO, "project": "p"}], "writes_on": True, "limits": {"children": 20, "title": 120, "body": 10000}}
    assert ep.gh.calls() == [] and ep.writes() == []
    monkeypatch.setattr(settings, "epic_writes", False)
    assert ep.c.get("/api/epics/new", headers=ID).json()["writes_on"] is False


def test_the_options_put_the_named_projects_repositories_first_and_a_path_without_a_remote_is_never_listed(ep, monkeypatch):
    from app import node_state
    table = {"/fake/a": "acme/one", "/fake/b": "acme/two", "/fake/c": "acme/three"}
    monkeypatch.setattr(node_state, "repo_slug", lambda p: table.get(str(p)))
    projs = [{"name": "first", "repos": [{"name": "one", "path": "/fake/a", "state": "ok"}]},
             {"name": "second", "repos": [{"name": "two", "path": "/fake/b", "state": "ok"}, {"name": "three", "path": "/fake/c", "state": "ok"}, {"name": "gone", "path": "/fake/z", "state": "ok"}]}]
    monkeypatch.setattr(ep.main, "_cached_projects", lambda: projs)

    def names(q):
        return [x["repo"] for x in ep.c.get("/api/epics/new" + q, headers=ID).json()["repos"]]
    assert names("") == ["acme/one", "acme/two", "acme/three"]
    assert names("?project=second") == ["acme/two", "acme/three", "acme/one"]
    assert names("?project=" + "x" * 300) == ["acme/one", "acme/two", "acme/three"]


# ================================================================ review fixes: the body check, a resume that must not adopt other text, more secret shapes, look-alike spellings, the registered issues, the size cap

def stop_after_child_one(ep) -> dict:
    """A create that stops at child 2 (the parent and child 1 exist), the way an interrupted create leaves GitHub."""
    seen = {"n": 0}

    def hook(kind, method, path):
        if method == "POST" and path.endswith("/issues"):
            seen["n"] += 1
            if seen["n"] == 3:
                ep.gh.world["issue_create_forbidden"] = True

    ep.state.hook = hook
    first = ep.create(epic(3)).json()
    assert first["ok"] is False and first["step"] == "child 2"
    ep.state.hook = None
    ep.gh.world["issue_create_forbidden"] = False
    return first


def test_a_description_that_holds_the_who_heading_previews_and_creates_without_a_server_error(ep):
    payload = epic(2)
    payload["children"][0]["description"] = "notes\n## Who should do it\n\nTier B"
    p = ep.preview(payload)
    assert p.status_code == 200, p.text
    assert "\\## Who should do it" in p.json()["children"][0]["body"], "the person's line is escaped, so it is text and not a heading"
    r = ep.create(payload)
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    sent = [x[2] for x in ep.writes() if x[1] == f"/repos/{REPO}/issues"][1]
    assert sent["body"] == p.json()["children"][0]["body"]
    assert github_write.valid_epic_body(sent["body"], "child")
    assert issues.parse_who(sent["body"])["warnings"] == [] and issues.parse_who(sent["body"])["claude"]["model"] == "sonnet"


def test_the_who_block_is_found_at_the_real_heading_not_at_the_escaped_one():
    spec = nodes_epics.normalize(epic(1), REPO)
    spec["children"][0]["description"] = "x\n## Who should do it\n\nTier B"
    key = nodes_epics.epic_key(spec)
    body = nodes_epics.child_body(spec, 1, key)
    assert nodes_epics.valid_generated_body(body, "child")
    assert not nodes_epics.valid_generated_body(body.replace("- Claude: `claude --model sonnet", "- Claude: `claude --model opus", 1), "child"), "a block that is not the table's is still refused"


def test_a_request_the_writer_refuses_to_build_is_a_failed_step_that_lists_what_exists_and_is_audited(ep, monkeypatch):
    def refuse(*a, **k):
        raise github_write.GhWriteError("bad_request")
    monkeypatch.setattr(github_write, "epic_sub_issue_attach", refuse)
    r = ep.create(epic(2))
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["ok"] is False and res["step"] == "attach 1" and res["reason"] == "bad_request" and res["parent"]["state"] == "created"
    assert [c["row"] for c in res["children"]] == [1, 2] and len(ep.issues("ccboard:epic-child")) == 2, "the answer lists what exists"
    rows = nodes.audit_list(100, db=ep.main.db, action="epic_attach")
    assert len(rows) == 1 and rows[0]["status"] == "failed" and rows[0]["detail"] == "child 1 bad_request"


def test_a_builder_that_refuses_the_parent_or_the_edit_is_a_failed_step_too(ep, monkeypatch):
    real = github_write.epic_issue_edit

    def refuse(*a, **k):
        raise github_write.GhWriteError("bad_request")
    monkeypatch.setattr(github_write, "epic_issue_edit", refuse)
    res = ep.create(epic(1)).json()
    assert res["ok"] is False and res["step"] == "edit" and res["reason"] == "bad_request" and len(res["children"]) == 1 and res["children"][0]["attached"] is True
    monkeypatch.setattr(github_write, "epic_issue_edit", real)
    assert ep.create(epic(1)).json()["ok"] is True
    monkeypatch.setattr(github_write, "epic_issue_create", refuse)
    again = ep.create(epic(1, title="Another")).json()
    assert again["ok"] is False and again["step"] == "parent" and again["reason"] == "bad_request"


def test_the_route_turns_a_writer_refusal_into_a_plain_answer_not_a_500(ep, monkeypatch):
    def refuse(*a, **k):
        raise github_write.GhWriteError("bad_request")
    monkeypatch.setattr(nodes_epics, "create", refuse)
    r = ep.create(epic(1))
    assert r.status_code == 409 and r.json()["reason"] == "bad_request" and "not a write" in r.json()["error"]


def test_preview_refuses_a_body_the_writer_would_refuse_beside_its_field(ep, monkeypatch):
    monkeypatch.setitem(nodes_epics.TIERS["B"], "claude", nodes_epics.TIERS["B"]["claude"] + " --dangerously-skip-permissions")   # the typed text is clean; the generated body is not
    r = ep.preview(epic(2))
    assert r.status_code == 400, r.text
    errs = r.json()["errors"]
    assert [(e["field"], e["index"]) for e in errs] == [("child_description", 0), ("child_description", 1)] and "change it" in errs[0]["message"]
    assert ep.create(epic(2)).status_code == 400 and ep.writes() == [], "Create refuses the same thing before it writes anything"


def test_a_resume_does_not_adopt_an_issue_whose_text_is_not_what_would_be_written_now(ep):
    first = stop_after_child_one(ep)
    n_before = len(ep.writes())
    changed = epic(3)
    changed["children"][0]["tier"] = "A"                                 # the epic key holds titles only, so this is the same epic with other text
    r = ep.create(changed)
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["ok"] is False and res["done"] is False and res["reason"] == "differs"
    c1 = first["children"][0]["number"]
    assert any(f"Issue 1 (#{c1}) is already on GitHub with different text: edit it there or start a new epic" in n for n in res["notes"]), res["notes"]
    assert len(ep.writes()) == n_before, "nothing was written over it, nothing attached"
    assert not ep.gh.world["subs"].get(f"{REPO}#{first['parent']['number']}") and len(ep.issues("ccboard:epic-child")) == 1
    assert [c["number"] for c in res["children"]] == [c1] and res["parent"]["number"] == first["parent"]["number"]
    ok = ep.create(epic(3)).json()                                       # the unchanged epic still resumes
    assert ok["ok"] is True and ok["resumed"] is True and len(ep.issues("ccboard:epic-child")) == 3


def test_a_resume_does_not_adopt_a_parent_whose_text_changed_and_names_every_issue_that_differs(ep):
    stop_after_child_one(ep)
    n_before = len(ep.writes())
    changed = epic(3, goal="A different goal.")
    changed["children"][0]["size"] = "L"
    res = ep.create(changed).json()
    assert res["ok"] is False and res["reason"] == "differs" and len(res["notes"]) == 2
    assert "The epic issue (#" in res["notes"][0] and "Issue 1 (#" in res["notes"][1] and len(ep.writes()) == n_before


def test_a_resume_ignores_the_list_of_issues_a_person_ticked_and_the_line_breaks_of_the_web_editor(ep):
    payload = epic(3)
    payload["children"][2]["depends_on"] = 1
    first = ep.create(payload).json()
    assert first["ok"] is True
    pk = f"{REPO}#{first['parent']['number']}"
    ep.gh.world["issues"][pk]["body"] = ep.gh.world["issues"][pk]["body"].replace("- [ ] #", "- [x] #").replace("\n", "\r\n")
    again = ep.create(payload).json()
    assert again["ok"] is True and again["writes"] == 0 and again["resumed"] is True


@pytest.mark.parametrize("name,sample", [
    ("private key", "-----BEGIN " + "RSA PRIVATE KEY-----"),
    ("npm", "npm_" + "a1B2c3" * 5),
    ("gitlab", "glpat-" + "a1B2c3D4" * 3),
    ("google", "AIza" + "Sy" + "a1B2c3D4" * 4),
    ("stripe live", "sk_" + "live_" + "a1B2c3D4e5F6"),
    ("stripe restricted", "rk_" + "live_" + "a1B2c3D4e5F6"),
    ("jwt", "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "dBjftJeZ4CVPmB92K27uhbUJU1p1r"),
    ("basic", "Authorization: " + "Basic " + "dXNlcjpwYXNz"),
    ("bearer", "Authorization: " + "Bearer " + "abc123"),
    ("long run", "Zq9" * 12),
])
def test_more_secret_shapes_are_refused_and_the_message_never_repeats_them(ep, name, sample):
    assert github_write.epic_text_problem(f"see {sample} here") == "secret", name
    for where in ("goal", "child_description"):
        payload = epic(2)
        if where == "goal":
            payload["goal"] = f"Use {sample} for it"
        else:
            payload["children"][1]["description"] = f"Use {sample} for it"
        for r in (ep.preview(payload), ep.create(payload)):
            assert r.status_code == 400, (name, where, r.text)
            err = r.json()["errors"][0]
            assert err["field"] == where and err["message"] == nodes_epics.PROBLEM_TEXT["secret"]
            assert sample not in r.text and sample[8:20] not in r.text, "the match is never echoed"
    assert ep.writes() == []


def test_ordinary_text_is_not_taken_for_a_secret():
    for t in ("state-of-the-art-user-interface-design", "Read https://example.com/docs/page first.", "Use Authorization headers carefully.", "the npm registry and a private key policy"):
        assert github_write.epic_text_problem(t) is None, t


@pytest.mark.parametrize("name,text", [
    ("en-dash", "danger–full–access"),
    ("non-breaking hyphen", "danger‑full‑access"),
    ("fullwidth", "ｂｙｐａｓｓＰｅｒｍｉｓｓｉｏｎｓ"),
    ("cyrillic u", "bуpassPermissions"),
    ("cyrillic o", "dangerоusly"),
    ("greek epsilon", "danger-full-accεss"),
    ("fullwidth yolo", "－－ｙｏｌｏ"),
    ("zero width", "bypass​Permissions"),
])
def test_look_alike_spellings_of_a_bypass_are_refused(ep, name, text):
    assert github_write.epic_text_problem("run it with " + text) == "bypass", name
    r = ep.preview(epic(1, goal="Please use " + text))
    assert r.status_code == 400 and r.json()["errors"][0]["field"] == "goal" and "bypass" in r.json()["errors"][0]["message"], (name, r.text)


def test_words_that_only_look_like_a_bypass_in_plain_text_stay_allowed():
    for t in ("Bypass the cache and grant permissions to the group.", "a danger to full access roads", "Yolov5 is a model", "mayolo"):
        assert github_write.epic_text_problem(t) is None, t


def test_an_edit_or_an_attach_of_an_issue_the_run_did_not_register_is_refused_before_an_argv_exists(monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", True)
    spec = nodes_epics.normalize(epic(1), REPO)
    pb = nodes_epics.parent_body(spec, nodes_epics.epic_key(spec))
    R = github_write.Request
    for req in (R("PATCH", "/repos/acme/shop/issues/9", {"body": pb}, "epic_create", frozenset({5}), frozenset({77})),
                R("PATCH", "/repos/acme/shop/issues/5", {"body": pb}, "epic_create"),
                R("POST", "/repos/acme/shop/issues/9/sub_issues", {"sub_issue_id": 77}, "epic_create", frozenset({5}), frozenset({77})),
                R("POST", "/repos/acme/shop/issues/5/sub_issues", {"sub_issue_id": 78}, "epic_create", frozenset({5}), frozenset({77})),
                R("POST", "/repos/acme/shop/issues/5/sub_issues", {"sub_issue_id": 5}, "epic_create", frozenset({5}), frozenset({77}))):
        with pytest.raises(github_write.GhWriteError) as e:
            github_write.argv(req)
        assert e.value.reason == "bad_request", req
    for build in (lambda: github_write.epic_issue_edit("acme", "shop", 9, pb, numbers={5}), lambda: github_write.epic_issue_edit("acme", "shop", 5, pb),
                  lambda: github_write.epic_sub_issue_attach("acme", "shop", 5, 78, numbers={5}, ids={77}), lambda: github_write.epic_sub_issue_attach("acme", "shop", 5, 77)):
        with pytest.raises(github_write.GhWriteError):
            build()
    assert github_write.argv(github_write.epic_issue_edit("acme", "shop", 5, pb, numbers={5}), "gh")[-1] == "/repos/acme/shop/issues/5"


def test_a_create_passes_the_issues_it_created_or_adopted_with_each_attach_and_edit(ep, monkeypatch):
    seen = {"attach": [], "edit": []}
    real_attach, real_edit = github_write.epic_sub_issue_attach, github_write.epic_issue_edit

    def attach(*a, **k):
        seen["attach"].append((a[3], k["numbers"], k["ids"]))
        return real_attach(*a, **k)

    def edit(*a, **k):
        seen["edit"].append((a[2], k["numbers"]))
        return real_edit(*a, **k)
    monkeypatch.setattr(github_write, "epic_sub_issue_attach", attach)
    monkeypatch.setattr(github_write, "epic_issue_edit", edit)
    res = ep.create(epic(2)).json()
    assert res["ok"] is True
    nums = {res["parent"]["number"]} | {c["number"] for c in res["children"]}
    ids = {gh_server.issue_id(f"{REPO}#{c['number']}") for c in res["children"]}
    assert {s[0] for s in seen["attach"]} == ids and all(set(s[1]) == nums and set(s[2]) == ids for s in seen["attach"])
    assert seen["edit"] == [(res["parent"]["number"], frozenset(nums))]
    # a resume registers what it adopted
    seen["attach"].clear()
    ep.gh.fail("/sub_issues", 500, method="POST", times=1)
    stopped = ep.create(epic(2, title="Second epic")).json()
    assert stopped["step"] == "attach 1"
    seen["attach"].clear()
    done = ep.create(epic(2, title="Second epic")).json()
    assert done["ok"] is True and done["resumed"] is True and seen["attach"]
    assert all({done["parent"]["number"]} | {c["number"] for c in done["children"]} == set(s[1]) for s in seen["attach"])


def test_a_body_over_one_megabyte_is_refused_with_413_before_it_is_parsed_or_anything_is_read(ep):
    big = b'{"repo": "' + b"a" * 1_100_000 + b'"}'
    head = {**H, "Content-Type": "application/json"}

    def chunks():
        for _ in range(11):
            yield b" " * 100_000
    for path in ("", "/preview"):
        r = ep.c.post("/api/epics" + path, headers=head, content=big)
        assert r.status_code == 413 and "1 MB" in r.json()["error"], (path, r.status_code)
        r = ep.c.post("/api/epics" + path, headers=head, content=chunks())
        assert r.status_code == 413, "a body with no length is cut off while it is read"
    assert ep.gh.calls() == [] and ep.writes() == []
    assert ep.create(epic(1)).status_code == 200, "an ordinary epic is far under the cap"


def test_an_invalid_payload_gets_a_plain_422_that_does_not_echo_what_was_sent(ep):
    marker = "needle" + "-marker-" + "zq"
    head = {**H, "Content-Type": "application/json"}
    for path in ("", "/preview"):
        for bad in ({"repo": REPO, "children": marker}, {"repo": REPO, "title": ["x", marker]}, {"repo": REPO, "bogus": marker}, {"repo": REPO, "children": [{"title": "t", "depends_on": marker}]}):
            r = ep.post(path, bad)
            assert r.status_code == 422 and marker not in r.text and set(r.json()) == {"error"}, (path, bad, r.text)
        r = ep.c.post("/api/epics" + path, headers=head, content=b'{"repo": "' + marker.encode() + b'", broken')
        assert r.status_code == 422 and marker not in r.text
    assert ep.gh.calls() == [] and ep.writes() == []
