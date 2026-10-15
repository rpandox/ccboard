"""Team P3, issue #199: the demo (`?demo=1`) speaks the real shape of the warnings.

demo/team.json answers GET /api/team/state in the demo. These tests work one real read out with the real model (app/team.py), with every form of warning in it, and hold the fixture to
the same keys, the same words and the same links; they also check that the demo has the issues and tasks its warnings talk about. Temp files only: no gh, no network, no real home.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from app import team
from tests.test_team import BOB, CAROL, NOW, RECENT, entry, read
from tests.test_team_conflicts import PR7, pline, presence, task

DEMO = Path(__file__).resolve().parents[1] / "app" / "static" / "demo"


def _real():
    """One real block with every form of warning, worked out by the real model."""
    claim = {"issue": 1, "user": BOB, "status": "running", "branch": "bob/c", "updated_at": RECENT, "live": True}
    pr = {**PR7, "closes": [1]}
    rd = read(issues=[{"number": 1, "title": "One", "labels": [], "assignees": [BOB, CAROL], "updated_at": RECENT}, {"number": 2, "title": "Two", "labels": [], "assignees": [], "updated_at": RECENT}],
              claims=[claim], pulls=[pr], tips=[{"name": "bob/old", "at": "2026-10-01T10:00:00Z", "author": BOB}, {"name": "bob/login", "at": RECENT, "author": BOB}],
              linked={1: [{"name": "bob/login", "at": RECENT, "author": BOB}]}, presence=presence(pline(BOB, 1, age=120), pline(CAROL, 2, age=900, moved=False)),
              pull_files={7: {"files": ["a.py", "b.py"], "more": False}})
    return team.repo_state(entry(), rd, "alice", 1, NOW, [task(4, issue_number=1, branch="bob/login", files=["a.py", "z.py"], title="Mine")])


def _fixture():
    fx = json.loads((DEMO / "team.json").read_text(encoding="utf-8"))
    return fx, fx["base"]["repos"][0]


def test_the_demo_warnings_have_the_keys_of_the_real_ones_in_every_form():
    block = _real()
    real = block["warnings"]
    fx, web = _fixture()
    w = web["warnings"]
    assert set(w) == set(real) and w["api"] == 1 and set(w["files"]) == set(real["files"])
    real_issue = real["issues"]["1"][0]
    shapes = {s["kind"]: set(s) for ws in real["issues"].values() for x in ws for s in x["signals"]}
    assert set(shapes) == {"assigned", "claim", "presence", "branch", "pr"}, "the real read made every kind of signal"
    seen_kinds, seen_status, nameless = set(), set(), False
    for ws in w["issues"].values():
        for x in ws:
            assert set(x) == set(real_issue) and x["advice"] is True and x["status"] in ("taken", "recent", "stale")
            seen_status.add(x["status"])
            nameless = nameless or x["person"]["login"] is None
            for s in x["signals"]:
                seen_kinds.add(s["kind"])
                assert set(s) == shapes[s["kind"]], (x["issue"], s["kind"], set(s) ^ shapes[s["kind"]])
    assert seen_kinds == set(shapes) and seen_status == {"taken", "recent", "stale"} and nameless, "every form is in the base"
    real_branch = next(iter(real["branches"].values()))[0]
    olds = set()
    for ws in w["branches"].values():
        for x in ws:
            assert set(x) == set(real_branch) and x["advice"] is True
            olds.add(x["old"])
    assert olds == {True, False}
    real_task = real["tasks"]["4"]
    for tid, ent in w["tasks"].items():
        assert set(ent) == set(real_task), tid
        for f in ent["files"]:
            assert set(f) == set(real_task["files"][0]) and f["advice"] is True and set(f["pr"]) == set(real_task["files"][0]["pr"])
        for b in ent["branch"]:
            assert set(b) == set(real_branch)


def test_the_demo_rows_that_collide_with_a_task_have_the_real_marks():
    block = _real()
    fx, web = _fixture()
    real = {}
    for p in block["people"]:
        for kind in ("issues", "branches", "prs"):
            for row in p[kind]:
                for c in row.get("collides") or []:
                    real[c["kind"]] = set(c)
    assert set(real) == {"issue", "branch", "files"}
    marks = [c for p in web["people"] for kind in ("issues", "branches", "prs") for row in p[kind] for c in row.get("collides") or []]
    assert {c["kind"] for c in marks} == {"issue", "branch", "files"}
    assert all(set(c) == real[c["kind"]] for c in marks)
    assert all(not any("collides" in row for kind in ("issues", "branches", "prs") for row in p[kind]) for p in web["people"] if p["you"]), "the owner's own rows are never marked"


def test_the_demo_warnings_point_at_things_the_demo_has():
    """The launcher demo can only show what it can pick: the issues of demo/issues.json, the tasks of demo/state.json, and https://github.com links built from the repository."""
    fx, web = _fixture()
    issues = {str(i["number"]) for i in json.loads((DEMO / "issues.json").read_text(encoding="utf-8"))["issues"]}
    state = json.loads((DEMO / "state.json").read_text(encoding="utf-8"))
    tasks = {str(t["id"]): t for t in state["tasks"] if t["repo"] == "website"}
    assert state["team"] == {"shared": True}
    w = web["warnings"]
    assert set(w["issues"]) <= issues and set(w["tasks"]) <= set(tasks)
    for tid, ent in w["tasks"].items():
        assert ent["title"] == tasks[tid]["title"]
        for x in ent["issue"]:
            assert x["issue"] == tasks[tid]["issue_number"], "a task's issue warning is for the issue the task carries"
    for u in re.findall(r'"url": "([^"]*)"', json.dumps(w)):
        assert re.fullmatch(r"https://github\.com/example/website/(issues/\d+|pull/\d+|tree/[A-Za-z0-9._/-]+)", u), u
    assert all(r.get("warnings") is None for r in fx["base"]["repos"][1:]), "a repository that is not shared has no warnings"
    for name in ("off", "loading", "warnnone"):
        assert fx["variants"][name]["repo"]["warnings"] is None
    assert fx["variants"]["warnnoid"]["repo"]["warnings"]["identity_known"] is False
    marks = [c for p in web["people"] for kind in ("issues", "branches", "prs") for row in p[kind] for c in row.get("collides") or []]
    assert {c["task"] for c in marks} <= set(map(int, tasks)), "a mark names a task the demo has"
