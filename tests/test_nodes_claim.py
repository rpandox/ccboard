"""Nodes epic P13, issue #148: claim an epic issue across nodes (the core: the writer, the queue, the protocol, the fence). No route, no doctor row, no page here.

GitHub is tests/fake_github.py: an in-memory GitHub with a server clock the test controls (comment ids that grow, `created_at` with one-second resolution so ties happen,
`updated_at` that moves only when a body changes, a Date header, paging at 100, comments that stay hidden for a while, injected 403/429/5xx/timeouts, a write whose answer
is lost, another node's comment interleaved between two calls). Most tests serve it from this process (`use_memory`); the writer's own command line is also run through the
fake gh script. The protocol's waits are patched: nothing sleeps for real, the fake clock moves instead. No test reaches the real gh, GitHub or the network.

Pinned: writes are off by default and then nothing reaches gh, not even a read; the writer can build only its allow list; a comment body is a fixed template of validated
fields; the board edits and deletes only comments it created and recorded, after a read shows them still its own; writes go only to repositories of this board's own
projects and to issues of an epic it has read; unknown is never claimable and free means fresh; the order is GitHub's (created_at, then id) and a node wins only when it is
first in two uncached readings; the loser deletes its own comment and nothing else; a lost claim writes nothing again; the queue is serial, spaced and backs off; an unknown
outcome is read back before any new write; the alias and the nid carry no host name, path or token.
"""
from __future__ import annotations

import json
import logging
import random
import re
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from app import github, github_write, nodes, nodes_epics
from app.config import Settings, parse_epic_lease, settings
from app.db import DB
from tests.fake_github import SECRET, START, FakeGitHub, claim_body, iso

REPO = "acme/shop"
K = {n: f"{REPO}#{n}" for n in range(200, 1300)}
GW = github_write


# ================================================================ the world

class Env(SimpleNamespace):
    def actor(self, name="node-a", login="alice", **kw):
        db = DB(self.tmp / f"{name}.db")
        q = GW.Queue(mono=self.mono, sleep=self._qsleep)
        self.dbs.append(db)
        return nodes_epics.Actor(db, kw.pop("node_id", f"n_{name}"), login, kw.pop("alias", name), q, kw.pop("repos", [REPO]))

    def _qsleep(self, s):
        self.qsleeps.append(s)
        self.gh.world["now"] += s

    def claim(self, actor, n, **kw):
        return nodes_epics.claim_issue(REPO, n, actor=actor, **kw)

    def tick(self, seconds):
        self.gh.world["now"] += seconds

    def comments(self, n):
        return self.gh.thread(K[n])

    def claims(self, n):
        return self.gh.claims_on(K[n])

    def parsed(self, n):
        return [nodes_epics.parse_claim(c["body"]) for c in self.claims(n)]

    def foreign(self, n, alias="node-z", status="claimed", *, nid="deadbeef", seq=1, attempt=1, age=0, user="alice", assoc="COLLABORATOR", created_at=None, cid=None, hidden=None, **kw):
        """A claim comment of another node, `age` seconds old (created and updated then), with an id that follows the thread's."""
        w = self.gh.world
        if cid is None:
            w["next_cid"] = cid = w.get("next_cid", 100000) + 1
        t = iso(w["now"] - age)
        c = self.gh.comment(K[n], claim_body(alias, status, nid=nid, seq=seq, attempt=attempt, **kw), user=user, created_at=created_at or t, updated_at=t, cid=cid, assoc=assoc)
        if hidden:
            self.gh.hide(c, hidden)
        return c

    def kinds(self):
        return [(m, re.sub(r"issues/[0-9]+/", "", p.split("/repos/acme/shop/")[1])) for m, p, _ in self.gh.writes()]


def read_epic(children, epic=147):
    """Make the board's last good answer say these issues are children of an epic it has read (the claim writes only to such issues)."""
    nodes_epics._last_good[("acme", "shop", epic)] = (nodes_epics.mono(), {"epic": {"labelled": True}, "children": [{"repo": REPO, "number": n} for n in children]})


@pytest.fixture
def env(tmp_path, monkeypatch):
    gh = FakeGitHub(tmp_path).use_memory(monkeypatch)
    e = Env(gh=gh, tmp=tmp_path, sleeps=[], qsleeps=[], dbs=[], mono=lambda: gh.world["now"] - START + 1000.0)

    def sleep(s):
        e.sleeps.append(s)
        gh.world["now"] += s
    monkeypatch.setattr(nodes_epics, "sleep", sleep)
    monkeypatch.setattr(nodes_epics, "mono", e.mono)
    monkeypatch.setattr(github.reader, "mono", e.mono)
    monkeypatch.setattr(settings, "epic_writes", True)
    monkeypatch.setattr(settings, "epic_lease_min", 30)
    monkeypatch.setattr(settings, "epic_alias", "")
    monkeypatch.setattr(settings, "epic_authors", ())
    monkeypatch.setattr(nodes_epics, "RENEWER", False)
    monkeypatch.setattr(nodes_epics, "READ_CHILD_MAX_AGE", 10 ** 9)           # the fake clock jumps by hours in many tests; the age rule has its own test
    monkeypatch.setattr(nodes_epics, "host_names", lambda: {"mybox"})
    for n in range(201, 211):
        gh.add_issue(REPO, n, f"Issue {n}")
    gh.add_issue(REPO, 147, "The epic", labels=["ccboard:epic"])
    read_epic(range(201, 211))
    yield e
    nodes_epics.stop_renewals(2.0)
    for db in e.dbs:
        db.close()


# ================================================================ writes are off by default

def test_the_setting_is_off_unless_it_is_exactly_one():
    assert Settings({}).epic_writes is False
    for v in ("", "0", "true", "yes", "on", "2", "11", "one", " 0"):
        assert Settings({"CCBOARD_EPIC_WRITES": v}).epic_writes is False, repr(v)
    assert Settings({"CCBOARD_EPIC_WRITES": "1"}).epic_writes is True and Settings({"CCBOARD_EPIC_WRITES": " 1 "}).epic_writes is True


def test_the_lease_and_alias_settings():
    assert parse_epic_lease(None) == (30, False) and parse_epic_lease("") == (30, False)
    assert parse_epic_lease("10") == (10, False) and parse_epic_lease("120") == (120, False) and parse_epic_lease(" 45 ") == (45, False)
    for bad in ("9", "121", "0", "-5", "abc", "30m", "1e2", "٣٠", "99999"):
        assert parse_epic_lease(bad) == (30, True), bad
    s = Settings({"CCBOARD_EPIC_ALIAS": " my-node ", "CCBOARD_EPIC_LEASE_MIN": "20"})
    assert s.epic_alias == "my-node" and s.epic_lease_min == 20 and Settings({}).epic_alias == "" and Settings({}).epic_lease_min == 30


def test_with_writes_off_every_write_entry_point_refuses_and_gh_is_never_run(tmp_path, monkeypatch):
    """The fake gh script is installed, so any call WOULD be logged: none is (not even `gh api user` for the login, not even a read). subprocess.run is spied too."""
    gh = FakeGitHub(tmp_path).install(monkeypatch)
    ran = []
    real_run = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a) or real_run(*a, **k))
    assert settings.epic_writes is False
    db = DB(tmp_path / "off.db")
    a = nodes_epics.Actor(db, "n_off", "alice", "node-a", GW.Queue(), [REPO])
    read_epic([201])
    ref = "acme/shop#201:100001"
    req = GW.comment_create("acme", "shop", 201, GW.HELP_BODY)
    r = nodes_epics.claim_issue(REPO, 201, actor=a)
    assert r["claimed"] is False and r["reason"] == "writes_off" and r["claim_ref"] is None and "CCBOARD_EPIC_WRITES is off" in r["message"]
    nodes_epics.claim_issue(REPO, 201)                                         # no actor: the login is not even asked for
    assert nodes_epics.renew(ref, actor=a)["reason"] == "writes_off" and nodes_epics.start(ref, actor=a)["reason"] == "writes_off"
    assert nodes_epics.release(ref, "done", actor=a)["reason"] == "writes_off"
    assert nodes_epics.fence_ok(ref, actor=a) is False and nodes_epics.fence(ref, actor=a)["reason"] == "writes_off"
    assert nodes_epics.recover(a) == [] and nodes_epics.held_claims(a) == [] and nodes_epics.attach_task(ref, 5, actor=a) is False
    for call in (lambda: GW.argv(req), lambda: GW.send(req), lambda: GW.send(req, GW.Queue()), lambda: GW.queue.run(req), lambda: GW.transport(req),
                 lambda: GW.Queue().run(req)):
        with pytest.raises(GW.GhWriteError) as e:
            call()
        assert e.value.reason == "writes_off" and str(e.value) == "this board does not write to GitHub (CCBOARD_EPIC_WRITES is off)" and e.value.unknown is False
    assert gh.calls() == [], "the fake gh was never run: no write, no read, no login probe"
    assert not [x for x in ran if x and isinstance(x[0], list) and x[0][0].endswith("gh")]
    db.close()


def test_the_writer_refuses_before_it_builds_an_argv(monkeypatch):
    built = []
    real = GW.json.dumps
    monkeypatch.setattr(GW.json, "dumps", lambda *a, **k: built.append(a) or real(*a, **k))
    with pytest.raises(GW.GhWriteError):
        GW.argv(GW.comment_delete("acme", "shop", 5))
    assert built == [] and settings.epic_writes is False
    monkeypatch.setattr(settings, "epic_writes", True)
    assert GW.argv(GW.comment_delete("acme", "shop", 5))[:5] == ["gh", "api", "--include", "--method", "DELETE"]


# ================================================================ the writer: an allow list and nothing else

BODY = GW.claim_body("node-a", "a1b2c3d4", 30, 1, "claimed", 1)


def all_requests():
    o, r, n, c = "acme", "shop", 201, 123456
    return [GW.comment_create(o, r, n, BODY), GW.comment_create(o, r, n, GW.HELP_BODY), GW.comment_edit(o, r, c, BODY), GW.comment_delete(o, r, c),
            GW.assignee_add(o, r, n, "alice"), GW.assignee_remove(o, r, n, "alice"), GW.label_add(o, r, n), GW.label_remove(o, r, n), GW.label_create(o, r)]


def test_every_command_the_writer_can_build_is_on_the_allow_list(monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", True)
    shapes = set()
    for req in all_requests():
        assert GW.allowed_write(req.method, req.path), req
        a = GW.argv(req)
        assert a[:7] == ["gh", "api", "--include", "--method", req.method, "-H", "Accept: application/vnd.github+json"] and a[-1] == req.path
        flags = [x for x in a if x.startswith("-") and x != "-"]
        assert set(flags) <= {"--include", "--method", "-H", "--input"}, flags                    # no -f/-F/--field/--raw-field, no --paginate, --jq, --hostname, -X
        assert a.count("--method") == 1 and (("--input" in a) == (req.body is not None))
        if req.body is not None:
            assert a[a.index("--input") + 1] == "-"
        shapes.add((req.method, re.sub(r"/[0-9]+", "/N", req.path.replace("/repos/acme/shop", ""))))
    assert shapes == {("POST", "/issues/N/comments"), ("PATCH", "/issues/comments/N"), ("DELETE", "/issues/comments/N"), ("POST", "/issues/N/assignees"),
                      ("DELETE", "/issues/N/assignees"), ("POST", "/issues/N/labels"), ("DELETE", "/issues/N/labels/ccboard%3Ain-progress"), ("POST", "/labels")}


@pytest.mark.parametrize("method,path", [
    ("GET", "/repos/acme/shop/issues/5/comments"), ("PUT", "/repos/acme/shop/issues/5/comments"), ("PATCH", "/repos/acme/shop/issues/5"),
    ("PATCH", "/repos/acme/shop/issues/5/comments"), ("DELETE", "/repos/acme/shop/issues/5"), ("PATCH", "/repos/acme/shop"), ("DELETE", "/repos/acme/shop"),
    ("POST", "/repos/acme/shop/pulls"), ("POST", "/repos/acme/shop/issues/5/lock"), ("PUT", "/repos/acme/shop/issues/5/lock"), ("POST", "/repos/acme/shop/merges"),
    ("PUT", "/repos/acme/shop/collaborators/bob"), ("POST", "/repos/acme/shop/hooks"), ("POST", "/repos/acme/shop/git/refs"), ("PUT", "/repos/acme/shop/contents/x"),
    ("DELETE", "/repos/acme/shop/issues/5/labels"), ("DELETE", "/repos/acme/shop/issues/5/labels/bug"), ("DELETE", "/repos/acme/shop/issues/5/labels/ccboard:in-progress"),
    ("PUT", "/repos/acme/shop/issues/5/labels"), ("PATCH", "/repos/acme/shop/labels/x"), ("DELETE", "/repos/acme/shop/labels/ccboard%3Ain-progress"),
    ("POST", "/repos/acme/shop/issues/comments/5"), ("POST", "/repos/acme/shop/issues/comments/5/reactions"), ("POST", "/repos/acme/shop/issues/5/comments/6"),
    ("POST", "/repos/acme/shop/issues/5/comments?x=1"), ("POST", "/repos/acme/shop/issues/0/comments"), ("POST", "/repos/acme/shop/issues/05/comments"),
    ("PATCH", "/repos/acme/shop/issues/comments/0"), ("POST", "/repos/-x/shop/issues/5/comments"), ("POST", "/repos/acme/../issues/5/comments"),
    ("POST", "/repos/acme/./issues/5/comments"), ("POST", "/user/repos"), ("POST", "/graphql"), ("POST", "https://evil.example.com/repos/acme/shop/issues/5/comments"),
    ("POST", "/repos/acme/shop/issues/5/comments\n--hostname"), ("POST", "/repos/acme/shop/issues/5/comments --field x=1"), ("post", "/repos/acme/shop/issues/5/comments"),
    ("POST", "repos/acme/shop/issues/5/comments"), ("POST", None), (None, "/repos/acme/shop/labels"), ("POST", "/repos/acme/shop/issues/5/assignees/bob")])
def test_the_allow_list_refuses_everything_else(method, path):
    assert GW.allowed_write(method, path) is False


@pytest.mark.parametrize("bad_owner", ["", "-x", "a--b", "a/b", "a b", "../x", "x" * 40, "a;b", "a\nb", "%2e", None, 5, "a.b", "$(id)", "a`b`"])
def test_a_hostile_owner_never_reaches_a_command(bad_owner):
    for build in (lambda o: GW.comment_create(o, "shop", 5, BODY), lambda o: GW.comment_edit(o, "shop", 5, BODY), lambda o: GW.comment_delete(o, "shop", 5),
                  lambda o: GW.assignee_add(o, "shop", 5, "alice"), lambda o: GW.assignee_remove(o, "shop", 5, "alice"), lambda o: GW.label_add(o, "shop", 5),
                  lambda o: GW.label_remove(o, "shop", 5), lambda o: GW.label_create(o, "shop")):
        with pytest.raises(GW.GhWriteError) as e:
            build(bad_owner)
        assert e.value.reason == "bad_request"


@pytest.mark.parametrize("bad_repo", ["", ".", "..", "-r", "a/b", "a?x=1", "a#b", " ", "r\n", "x" * 101, None, "a b", "r;rm", "%2e%2e"])
def test_a_hostile_repository_never_reaches_a_command(bad_repo):
    for build in (lambda r: GW.comment_create("acme", r, 5, BODY), lambda r: GW.comment_delete("acme", r, 5), lambda r: GW.label_add("acme", r, 5), lambda r: GW.label_create("acme", r)):
        with pytest.raises(GW.GhWriteError) as e:
            build(bad_repo)
        assert e.value.reason == "bad_request"


@pytest.mark.parametrize("bad_n", [0, -1, True, False, "01", "1;2", 10 ** 9, 1.5, None, "1 2", "5/../6", "", " 5", "5\n", "٥"])
def test_a_hostile_issue_number_never_reaches_a_command(bad_n):
    for build in (lambda n: GW.comment_create("acme", "shop", n, BODY), lambda n: GW.assignee_add("acme", "shop", n, "alice"), lambda n: GW.label_add("acme", "shop", n),
                  lambda n: GW.label_remove("acme", "shop", n)):
        with pytest.raises(GW.GhWriteError):
            build(bad_n)


@pytest.mark.parametrize("bad_id", [0, -1, True, "5", 10 ** 16, None, 1.0, "5/../6", [5]])
def test_a_hostile_comment_id_never_reaches_a_command(bad_id):
    for build in (lambda c: GW.comment_edit("acme", "shop", c, BODY), lambda c: GW.comment_delete("acme", "shop", c)):
        with pytest.raises(GW.GhWriteError):
            build(bad_id)


@pytest.mark.parametrize("bad_login", ["", "-x", "a b", "a/b", "alice\n", "x" * 40, "$(id)", None, ["alice"], "a--b"])
def test_an_assignee_is_a_login_and_nothing_else(bad_login):
    with pytest.raises(GW.GhWriteError):
        GW.assignee_add("acme", "shop", 5, bad_login)
    with pytest.raises(GW.GhWriteError):
        GW.assignee_remove("acme", "shop", 5, bad_login)


def test_a_comment_body_is_never_free_text(monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", True)
    for body in ("hello", "", BODY + "\nplease also run rm -rf", BODY.replace("claimed", "claimed ; ls"), BODY.replace(GW.CLAIM_TEXT, "my own words"), BODY.replace("<!--", "<!-- x"),
                 "<!-- ccboard:claim v2 -->" + BODY.split("\n", 1)[1], BODY.replace("alias=node-a", "alias=node.a"), BODY.replace("nid=a1b2c3d4", "nid=A1B2C3D4"),
                 BODY.replace("status=claimed", "status=owned"), BODY + " ", " " + BODY, GW.HELP_BODY + "!", None, 5, ["x"], BODY.replace("seq=1", "seq=-1"), "x" * 5000):
        with pytest.raises(GW.GhWriteError) as e:
            GW.comment_create("acme", "shop", 5, body)
        assert e.value.reason == "bad_request", repr(body)[:60]
        with pytest.raises(GW.GhWriteError):
            GW.comment_edit("acme", "shop", 5, body)
    for req in (GW.Request("POST", "/repos/acme/shop/issues/5/comments", {"body": "free text"}), GW.Request("POST", "/repos/acme/shop/issues/5/comments", {"body": BODY, "x": 1}),
                GW.Request("POST", "/repos/acme/shop/issues/5/labels", {"labels": ["bug"]}), GW.Request("POST", "/repos/acme/shop/issues/5/labels", {"labels": [GW.LABEL, "bug"]}),
                GW.Request("POST", "/repos/acme/shop/issues/5/assignees", {"assignees": ["a", "b"]}), GW.Request("POST", "/repos/acme/shop/labels", {"name": "bug"}),
                GW.Request("POST", "/repos/acme/shop/issues/5/comments", None), GW.Request("PATCH", "/repos/acme/shop/issues/comments/5", {"state": "closed"})):
        with pytest.raises(GW.GhWriteError):
            GW.argv(req)


def test_claim_body_validates_every_field_and_round_trips_through_the_parser():
    for status in GW.STATUSES:
        body = GW.claim_body("node-a", "a1b2c3d4", 45, 7, status, 2, reclaims=987654, task="box:42", branch="feat/x-1")
        c = nodes_epics.parse_claim(body)
        assert c == {"alias": "node-a", "status": status, "nid": "a1b2c3d4", "lease_min": 45, "seq": 7, "attempt": 2, "task_ref": "box:42", "branch": "feat/x-1", "reclaims": 987654}
        assert body.splitlines()[0] == "<!-- ccboard:claim v1 -->" and GW.valid_body(body) and body.count("\n") == 3
    plain = nodes_epics.parse_claim(GW.claim_body("node-a", "a1b2c3d4", 30, 1, "claimed", 1))
    assert plain["reclaims"] is None and plain["task_ref"] is None and plain["branch"] is None
    secretish = "ghp_" + "A1b2C3d4" * 4
    for kw in ({"alias": "node.a"}, {"alias": "a b"}, {"alias": "a\nb"}, {"alias": "$(id)"}, {"alias": "/etc/passwd"}, {"alias": "node/a"}, {"alias": ""}, {"alias": "x" * 41},
               {"alias": "é"}, {"alias": secretish[:40]}, {"nid": "xyz"}, {"nid": "a1b2c3d"}, {"nid": "A1B2C3D4"}, {"lease_min": 9}, {"lease_min": 121}, {"lease_min": True},
               {"seq": -1}, {"seq": 1.5}, {"status": "owned"}, {"status": "claimed;x"}, {"attempt": 0}, {"attempt": 100}, {"reclaims": 0}, {"reclaims": "5"},
               {"task": "a b"}, {"task": "/tmp/x"}, {"task": "a;b"}, {"task": "x" * 41}, {"task": "$(x)"}, {"branch": "-x"}, {"branch": "a b"}, {"branch": "../x"}, {"branch": "a/../b"},
               {"branch": "/abs"}, {"branch": "a;b"}, {"branch": "a" * 41}, {"branch": secretish}):
        args = {"alias": "node-a", "nid": "a1b2c3d4", "lease_min": 30, "seq": 1, "status": "claimed", "attempt": 1, **kw}
        extra = {k: args.pop(k) for k in ("reclaims", "task", "branch") if k in args}
        with pytest.raises(ValueError):
            GW.claim_body(args["alias"], args["nid"], args["lease_min"], args["seq"], args["status"], args["attempt"], **extra)


def test_a_token_shaped_alias_or_branch_is_refused_even_when_it_fits_the_pattern():
    tok = "ghp_" + "A1b2C3d4" * 4
    pat = "github_pat_" + "B2c3D4e5" * 3
    assert GW.ALIAS_RE.fullmatch(tok) and GW.valid_body(GW.claim_body("node-a", "a1b2c3d4", 30, 1, "claimed", 1))
    for body in (f"<!-- ccboard:claim v1 -->\nccboard claim: alias={tok} nid=a1b2c3d4 lease=30m seq=1 status=claimed attempt=1\n\n{GW.CLAIM_TEXT}",
                 f"<!-- ccboard:claim v1 -->\nccboard claim: alias=node-a nid=a1b2c3d4 lease=30m seq=1 status=claimed attempt=1 branch={pat}\n\n{GW.CLAIM_TEXT}"):
        assert GW.valid_body(body) is False


# ================================================================ the writer through the real command line (the fake gh script)

def test_the_writer_runs_each_write_through_the_fake_gh_and_the_token_never_comes_back(tmp_path, monkeypatch):
    gh = FakeGitHub(tmp_path).install(monkeypatch)
    monkeypatch.setattr(settings, "epic_writes", True)
    gh.add_issue(REPO, 201, "One")
    q = GW.Queue(sleep=lambda s: None)
    st, h, d = GW.send(GW.comment_create("acme", "shop", 201, BODY), q)
    assert st == 201 and isinstance(d["id"], int) and d["body"] == BODY and d["user"]["login"] == "alice" and d["created_at"] == d["updated_at"]
    cid = d["id"]
    st, _, d2 = GW.send(GW.comment_edit("acme", "shop", cid, BODY.replace("seq=1", "seq=2")), q)
    assert st == 200 and "seq=2" in d2["body"] and d2["updated_at"] == d["updated_at"] and d2["created_at"] == d["created_at"]
    with pytest.raises(GW.GhWriteError) as e:
        GW.send(GW.label_add("acme", "shop", 201), q)                              # the label does not exist on the repository yet
    assert e.value.reason == "invalid" and e.value.unknown is False and e.value.status == 422
    assert GW.send(GW.label_create("acme", "shop"), q)[0] == 201
    with pytest.raises(GW.GhWriteError) as e:
        GW.send(GW.label_create("acme", "shop"), q)
    assert e.value.reason == "invalid"                                            # exists already
    assert GW.send(GW.label_add("acme", "shop", 201), q)[0] == 200
    assert GW.send(GW.assignee_add("acme", "shop", 201, "alice"), q)[2]["assignees"] == [{"login": "alice", "type": "User"}]
    assert GW.send(GW.assignee_remove("acme", "shop", 201, "alice"), q)[0] == 200
    assert GW.send(GW.label_remove("acme", "shop", 201), q)[0] == 200
    with pytest.raises(GW.GhWriteError) as e:
        GW.send(GW.label_remove("acme", "shop", 201), q)
    assert e.value.reason == "not_found"
    assert GW.send(GW.comment_delete("acme", "shop", cid), q)[0] == 204
    with pytest.raises(GW.GhWriteError) as e:
        GW.send(GW.comment_delete("acme", "shop", cid), q)
    assert e.value.reason == "not_found"
    w = gh.writes()
    assert [(m, p.replace("/repos/acme/shop", "")) for m, p, _ in w][:3] == [("POST", "/issues/201/comments"), ("PATCH", f"/issues/comments/{cid}"), ("POST", "/issues/201/labels")]
    assert w[0][2] == {"body": BODY} and w[2][2] == {"labels": ["ccboard:in-progress"]} and w[6][2] == {"assignees": ["alice"]}
    for c in gh.calls():
        a = c["argv"]
        assert a[0] == "api" and a[a.index("--method") + 1] in ("POST", "PATCH", "DELETE") and not set(a) & {"-f", "-F", "--field", "--raw-field", "-X", "--paginate", "--jq"}
        assert (c["stdin"] != "") == ("--input" in a) and SECRET not in " ".join(a) + c["stdin"]
    # an error carries a fixed sentence only: the leaky gh put the token on stderr and none of it may be in any text of the error
    gh.fail("/comments", 403, method="POST", body={"message": "Resource not accessible by integration"})
    with pytest.raises(GW.GhWriteError) as e:
        GW.send(GW.comment_create("acme", "shop", 201, BODY), q)
    assert e.value.reason == "no_access" and SECRET not in str(e.value) and SECRET not in json.dumps(e.value.as_dict()) and e.value.unknown is False


def test_a_missing_gh_and_a_missing_login_are_fixed_reasons(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", True)
    gh = FakeGitHub(tmp_path).install(monkeypatch)
    gh.add_issue(REPO, 201, "One")
    gh.set(login=None)
    with pytest.raises(GW.GhWriteError) as e:
        GW.send(GW.comment_create("acme", "shop", 201, BODY), GW.Queue(sleep=lambda s: None))
    assert e.value.reason == "no_login" and e.value.unknown is False and SECRET not in str(e.value)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    with pytest.raises(GW.GhWriteError) as e:
        GW.transport(GW.comment_create("acme", "shop", 201, BODY))
    assert e.value.reason == "no_gh"


def test_a_gh_that_never_answers_is_an_unknown_outcome_after_the_applied_write(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", True)
    monkeypatch.setattr(GW, "TIMEOUT", 0.6)
    gh = FakeGitHub(tmp_path).install(monkeypatch)
    gh.add_issue(REPO, 201, "One")
    gh.fail("/comments", 502, method="POST", apply=True, hang=True)
    with pytest.raises(GW.GhWriteError) as e:
        GW.send(GW.comment_create("acme", "shop", 201, BODY), GW.Queue(sleep=lambda s: None))
    assert e.value.reason == "timeout" and e.value.unknown is True
    assert len(gh.thread(K[201])) == 1, "the comment was created although gh never answered: this is why an unknown outcome is read back"


# ================================================================ the queue

class Clk:
    def __init__(self):
        self.t = 100.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def fake_transport(statuses, headers=None, bodies=None, log=None):
    seq = list(statuses)

    def tp(req):
        if log is not None:
            log.append(req)
        st = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(st, Exception):
            raise st
        return st, (headers or {}), (bodies or {}).get(st, {"id": 5} if st < 300 else {"message": "x"})
    return tp


def test_the_queue_spaces_writes_by_at_least_1_1_seconds(monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", True)
    clk = Clk()
    q = GW.Queue(mono=clk, sleep=clk.sleep)
    log = []
    monkeypatch.setattr(GW, "transport", fake_transport([201], log=log))
    starts = []
    for i in range(5):
        GW.send(GW.comment_create("acme", "shop", 201, BODY), q)
        starts.append(clk.t)
        clk.t += 0.3 if i % 2 else 0.0                                        # the call itself takes some time, or none
    assert len(log) == 5
    assert all(b - a >= 1.1 - 1e-9 for a, b in zip(starts, starts[1:])), starts
    assert q.slept > 0 and min(clk.slept) > 0


def test_the_queue_allows_30_writes_a_minute_and_no_more(monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", True)
    clk = Clk()
    q = GW.Queue(mono=clk, sleep=clk.sleep, gap=0.01)
    monkeypatch.setattr(GW, "transport", fake_transport([201]))
    starts = []
    for _ in range(31):
        GW.send(GW.comment_create("acme", "shop", 201, BODY), q)
        starts.append(clk.t)
    assert starts[29] - starts[0] < 60, "30 writes fit in a minute"
    assert starts[30] - starts[0] >= 60.0, "the 31st waits until the first has left the window"
    assert all(b - a >= 0.01 for a, b in zip(starts, starts[1:]))


def test_the_queue_runs_one_write_at_a_time(monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", True)
    q = GW.Queue(gap=0.0, sleep=lambda s: None)
    live, peak, total = [0], [0], [0]
    guard = threading.Lock()

    def tp(req):
        with guard:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            total[0] += 1
        time.sleep(0.002)
        with guard:
            live[0] -= 1
        return 201, {}, {"id": 5}
    monkeypatch.setattr(GW, "transport", tp)
    ts = [threading.Thread(target=lambda: [GW.send(GW.comment_create("acme", "shop", 201, BODY), q) for _ in range(5)]) for _ in range(6)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert total[0] == 30 and peak[0] == 1


def test_a_secondary_limit_answer_delays_the_next_write_by_60_seconds_and_is_not_retried(monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", True)
    clk = Clk()
    q = GW.Queue(mono=clk, sleep=clk.sleep)
    log = []
    msg = {"message": "You have exceeded a secondary rate limit. Please wait a few minutes before you try again."}
    monkeypatch.setattr(GW, "transport", fake_transport([403, 201], bodies={403: msg}, log=log))
    with pytest.raises(GW.GhWriteError) as e:
        GW.send(GW.comment_create("acme", "shop", 201, BODY), q)
    assert e.value.reason == "rate_limited" and e.value.wait_s == 60 and e.value.unknown is False and len(log) == 1, "no automatic retry"
    t0 = clk.t
    GW.send(GW.comment_create("acme", "shop", 201, BODY), q)
    assert clk.t - t0 >= 60 - 1e-6 and max(clk.slept) >= 59.9, "the next write waited out the minute"
    assert q.wait_left() == 0


def test_retry_after_on_a_429_is_honoured_and_a_long_one_is_raised_instead_of_slept(monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", True)
    clk = Clk()
    q = GW.Queue(mono=clk, sleep=clk.sleep)
    monkeypatch.setattr(GW, "transport", fake_transport([429, 201], headers={"retry-after": "17"}))
    with pytest.raises(GW.GhWriteError) as e:
        GW.send(GW.comment_create("acme", "shop", 201, BODY), q)
    assert e.value.reason == "rate_limited" and e.value.wait_s == 17
    t0 = clk.t
    GW.send(GW.comment_create("acme", "shop", 201, BODY), q)
    assert clk.t - t0 >= 17
    q2 = GW.Queue(mono=clk, sleep=clk.sleep)
    log = []
    monkeypatch.setattr(GW, "transport", fake_transport([429, 201], headers={"retry-after": "900"}, log=log))
    with pytest.raises(GW.GhWriteError):
        GW.send(GW.comment_create("acme", "shop", 201, BODY), q2)
    t1 = clk.t
    with pytest.raises(GW.GhWriteError) as e:
        GW.send(GW.comment_create("acme", "shop", 201, BODY), q2)
    assert e.value.reason == "rate_limited" and e.value.wait_s >= 800 and clk.t == t1 and len(log) == 1, "a back-off longer than the queue will wait is raised, nothing is sent"


def test_an_error_is_known_or_unknown_and_never_retried(monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", True)
    expect = {400: ("bad_answer", True), 401: ("no_login", False), 403: ("no_access", False), 404: ("not_found", False), 410: ("not_found", False), 422: ("invalid", False),
              409: ("invalid", False), 500: ("unavailable", True), 502: ("unavailable", True), 503: ("unavailable", True)}
    for status, (reason, unknown) in expect.items():
        log = []
        monkeypatch.setattr(GW, "transport", fake_transport([status], log=log))
        with pytest.raises(GW.GhWriteError) as e:
            GW.send(GW.comment_create("acme", "shop", 201, BODY), GW.Queue(sleep=lambda s: None))
        assert (e.value.reason, e.value.unknown, e.value.status) == (reason, unknown, status) and len(log) == 1, status
    for exc in (GW.GhWriteError("timeout", unknown=True), GW.GhWriteError("no_gh")):
        log = []
        monkeypatch.setattr(GW, "transport", fake_transport([exc], log=log))
        with pytest.raises(GW.GhWriteError) as e:
            GW.send(GW.comment_create("acme", "shop", 201, BODY), GW.Queue(sleep=lambda s: None))
        assert e.value is exc and len(log) == 1


def test_the_queue_checks_the_allow_list_before_it_waits_or_sends(monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", True)
    log = []
    clk = Clk()
    q = GW.Queue(mono=clk, sleep=clk.sleep)
    monkeypatch.setattr(GW, "transport", fake_transport([201], log=log))
    with pytest.raises(GW.GhWriteError) as e:
        q.run(GW.Request("PATCH", "/repos/acme/shop/issues/5", {"state": "closed"}))
    assert e.value.reason == "bad_request" and log == [] and clk.slept == [] and q.count == 0


# ================================================================ pure helpers: the parser, the order, the lease

def test_the_alias_is_a_neutral_word_derived_from_the_node_id_never_a_host_name(monkeypatch):
    monkeypatch.setattr(nodes_epics, "host_names", lambda: {"mybox", "laptop"})
    rep = nodes_epics.alias_report("ts:S123abc")
    assert rep["source"] == "derived" and rep["configured"] is None and rep["ignored"] is False and rep["alias"] == nodes_epics.derived_alias("ts:S123abc")
    assert re.fullmatch(r"[a-z]+-[0-9a-f]{2}", rep["alias"]) and rep["alias"].split("-")[0] in nodes_epics._WORDS and GW.ALIAS_RE.fullmatch(rep["alias"])
    assert nodes_epics.derived_alias("ts:S123abc") == rep["alias"] and nodes_epics.derived_alias("ts:S123abc") != nodes_epics.derived_alias("ts:S999zzz")
    assert len({nodes_epics.derived_alias(f"n_{i:016x}") for i in range(200)}) > 100, "different nodes mostly get different words"
    assert rep["nid"] == nodes_epics.node_nid("ts:S123abc") and re.fullmatch(r"[0-9a-f]{8}", rep["nid"])
    import hashlib
    assert rep["nid"] == hashlib.sha256(b"ts:S123abc").hexdigest()[:8]
    assert "S123abc" not in rep["alias"] and rep["nid"] != "S123abc"
    for host in ("mybox", "laptop", "MyBox", "LAPTOP"):
        monkeypatch.setattr(settings, "epic_alias", host)
        r = nodes_epics.alias_report("ts:S123abc")
        assert r["host_like"] is True and r["ignored"] is True and r["alias"] == rep["alias"] and r["source"] == "derived" and r["configured"] == host
    monkeypatch.setattr(settings, "epic_alias", "build.example.com")
    r = nodes_epics.alias_report("ts:S123abc")
    assert r["host_like"] is True and r["ignored"] is True and r["alias"] == rep["alias"]
    monkeypatch.setattr(settings, "epic_alias", "a b")
    assert nodes_epics.alias_report("ts:S123abc")["invalid"] is True and nodes_epics.alias_report("ts:S123abc")["alias"] == rep["alias"]
    monkeypatch.setattr(settings, "epic_alias", "workshop-2")
    r = nodes_epics.alias_report("ts:S123abc")
    assert r["alias"] == "workshop-2" and r["source"] == "setting" and r["host_like"] is False and r["ignored"] is False


def test_the_default_alias_never_comes_from_the_node_name_or_the_host_name(monkeypatch):
    monkeypatch.setattr(nodes, "display_name", lambda: "mybox")
    monkeypatch.setattr(nodes_epics, "host_names", lambda: {"mybox"})
    monkeypatch.setattr(nodes, "node_id", lambda: "n_" + "c" * 16)
    from app import main
    monkeypatch.setattr(main, "db", object())
    monkeypatch.setattr(nodes_epics, "_gh_login", lambda: "alice")
    a = nodes_epics.board_actor()
    assert a.alias != "mybox" and a.alias == nodes_epics.derived_alias("n_" + "c" * 16) and a.node_id == "n_" + "c" * 16


def test_ordering_is_created_at_then_id_and_a_claim_not_allowed_is_ignored():
    mk = lambda i, created, user="alice", assoc="COLLABORATOR": {"id": i, "body": claim_body(f"n{i}"), "user": {"login": user, "type": "User"}, "author_association": assoc,
                                                                    "created_at": created, "updated_at": created}
    cs = nodes_epics.parse_claims([mk(30, "2026-10-01T09:00:00Z"), mk(20, "2026-10-01T09:00:01Z"), mk(10, "2026-10-01T09:00:01Z"), mk(40, "2026-10-01T08:59:59Z")])
    assert [c["id"] for c in cs] == [40, 30, 10, 20], "oldest created_at first; equal created_at goes to the lower id"
    cs = nodes_epics.parse_claims([mk(5, "2026-10-01T09:00:00Z", "mallory", "NONE"), mk(6, "2026-10-01T09:00:00Z"), mk(4, "2026-10-01T09:00:00Z", "alice", "CONTRIBUTOR")])
    ok = nodes_epics.allowed_claims(cs, {"alice"})
    assert [c["id"] for c in ok] == [6], "a stranger, and an association that cannot write, are ignored for ordering"


def test_a_claim_is_live_by_github_clock_only_and_the_boundary_is_the_lease():
    c = {"status": "claimed", "updated_at": "2027-01-15T10:00:00Z", "lease_min": 30}
    upd = github.ts_epoch(c["updated_at"])
    assert nodes_epics.is_live(c, upd + 1799) is True and nodes_epics.is_live(c, upd + 1800) is False and nodes_epics.is_live(c, upd + 99999) is False
    assert nodes_epics.is_live(c, None) is None, "no server clock: not known, never assumed live"
    for st in ("done", "failed", "released", "cancelled"):
        assert nodes_epics.is_live({**c, "status": st}, upd + 1) is False
    assert nodes_epics.is_live({**c, "status": "running"}, upd + 1) is True


def test_the_parser_ignores_forgeries_without_the_marker_and_counts_a_copied_marker_line():
    forged = "ccboard claim: alias=evil nid=deadbeef lease=30m seq=1 status=claimed attempt=1"
    assert nodes_epics.parse_claim(forged) is None and nodes_epics.parse_claim("> " + claim_body()) is None and nodes_epics.parse_claim("hello\n" + claim_body()) is None
    assert nodes_epics.parse_claim(claim_body(version="v2")) is None and nodes_epics.parse_claim(claim_body(version="v10")) is None
    assert nodes_epics.parse_claim("<!-- ccboard:claim v1 -->\nccboard claim: alias=n1 nid=a1b2c3d4 lease=30m seq=1 status=bogus attempt=1") is None
    assert nodes_epics.parse_claim("<!-- ccboard:claim v1 -->\nnothing here") is None
    assert nodes_epics.parse_claim(claim_body("knoten-ä"))["alias"] == "knoten-ä"
    assert nodes_epics.parse_claim(claim_body(extra=" colour=blue evil=1"))["alias"] == "node-a"
    # a writer who copies a node's marker line writes a claim that counts: the documented limit
    copy = {"id": 9, "body": claim_body("node-a") + "\n\nmy own text", "user": {"login": "alice", "type": "User"}, "author_association": "COLLABORATOR",
            "created_at": "2026-10-01T09:00:00Z", "updated_at": "2026-10-01T09:00:00Z"}
    assert nodes_epics.allowed_claims(nodes_epics.parse_claims([copy]), {"alice"})[0]["alias"] == "node-a"


def test_a_claim_ref_names_one_comment_and_hostile_text_names_nothing():
    assert nodes_epics.parse_ref("acme/shop#201:100005") == ("acme", "shop", 201, 100005) and nodes_epics.make_ref("acme", "shop", 201, 100005) == "acme/shop#201:100005"
    for bad in ("", None, 5, "acme/shop#201", "acme/shop:5", "acme/shop#0:5", "acme/shop#201:0", "-x/shop#1:2", "acme/../x#1:2", "acme/shop#1:2 ", "acme/shop#1:2\n", "a/b/c#1:2",
                "acme/shop#1:2;rm", "acme/shop#1:" + "9" * 17, "acme/" + "r" * 101 + "#1:2", "x" * 300, "https://github.com/acme/shop#1:2", "acme/.#1:2"):
        assert nodes_epics.parse_ref(bad) is None, repr(bad)


# ================================================================ the protocol

def test_a_claim_is_posted_decided_twice_marked_and_recorded(env):
    gh = env.gh
    a = env.actor("node-a")
    r = env.claim(a, 201)
    assert r["claimed"] is True and r["reason"] == "won" and r["winner"] == "node-a" and r["attempt"] == 1 and r["notes"] == []
    owner, repo, n, cid = nodes_epics.parse_ref(r["claim_ref"])
    assert (owner, repo, n) == ("acme", "shop", 201)
    [c] = env.claims(201)
    assert c["id"] == cid and c["user"] == "alice"
    p = nodes_epics.parse_claim(c["body"])
    assert p["alias"] == "node-a" and p["nid"] == nodes_epics.node_nid(a.node_id) and p["status"] == "claimed" and p["seq"] == 1 and p["attempt"] == 1 and p["lease_min"] == 30
    assert p["reclaims"] is None and p["task_ref"] is None and p["branch"] is None
    assert gh.assignees_of(K[201]) == ["alice"] and gh.labels_of(K[201]) == ["ccboard:in-progress"]
    assert env.sleeps == [1.5, 1.5], "two waits of 1.5 s: one before each of the two uncached readings"
    kinds = env.kinds()
    assert kinds == [("POST", "comments"), ("POST", "assignees"), ("POST", "labels"), ("POST", "labels"), ("POST", "labels")]
    assert gh.writes()[3][1].endswith("/repos/acme/shop/labels") and gh.writes()[3][2]["name"] == "ccboard:in-progress"
    calls = [c["argv"][-1] if c["argv"][2:3] != ["POST"] else None for c in gh.calls()]
    post = next(i for i, c in enumerate(gh.calls()) if c["argv"][3:4] == ["POST"] or "POST" in c["argv"])
    mark = next(i for i, c in enumerate(gh.calls()) if c["argv"][-1].endswith("/assignees"))
    between = [c["argv"][-1] for c in gh.calls()[post:mark] if "/comments?" in c["argv"][-1]]
    assert len(between) >= 2, "the two uncached readings of the decision come before the mark (a loser must leave nothing to undo)"
    [view] = nodes_epics.held_claims(a)
    assert view["claim_ref"] == r["claim_ref"] and view["state"] == "held" and view["added_assignee"] is True and view["added_label"] is True and view["seq"] == 1
    assert "nid" not in view and "login" not in view
    assert env.qsleeps and min(env.qsleeps) >= 0.5, "the marks were spaced"


def test_an_issue_that_is_taken_is_not_claimed_and_the_winner_is_named(env):
    a, b = env.actor("node-a"), env.actor("node-b")
    assert env.claim(a, 201)["claimed"]
    n_writes = len(env.gh.writes())
    r = env.claim(b, 201)
    assert r["claimed"] is False and r["reason"] == "taken" and r["winner"] == "node-a" and r["claim_ref"] is None
    assert len(env.gh.writes()) == n_writes, "taken: not a single write"
    assert len(env.claims(201)) == 1


def test_claiming_again_is_idempotent_for_the_holder(env):
    a = env.actor("node-a")
    r1 = env.claim(a, 201)
    n = len(env.gh.writes())
    r2 = env.claim(a, 201)
    assert r2["claimed"] is True and r2["reason"] == "already_held" and r2["claim_ref"] == r1["claim_ref"] and len(env.gh.writes()) == n and len(env.claims(201)) == 1


def test_free_means_fresh_a_cached_none_is_never_believed(env):
    """The epic page read the issue as free (and cached it); another node claims; the claim step revalidates and sees it."""
    gh = env.gh
    a, b = env.actor("node-a"), env.actor("node-b")
    assert nodes_epics.fresh_claim_state("acme", "shop", 201, [REPO], "alice")["state"] == "none"
    github.reader.get(github.comments_path("acme", "shop", 201, 1, 100), 600.0)          # a cached page, as the epic view keeps it
    env.foreign(201, "node-z", age=5)
    assert env.claim(b, 201)["reason"] == "taken"
    assert gh.writes() == []


def test_unknown_is_never_claimable_and_nothing_is_written(env):
    gh = env.gh
    a = env.actor("node-a")
    gh.junk(K[201], 1)
    gh.fail("/comments", 429, headers={"retry-after": "30"}, times=-1, method="GET")
    r = env.claim(a, 201)
    assert r["claimed"] is False and r["reason"] == "claim_unknown" and r["unknown_reason"] == "rate_limited" and gh.writes() == []
    gh.world["overrides"].clear()
    github.reader.reset_state()
    gh.junk(K[203], 260)                                                                 # a thread longer than 200 comments is read from both ends, not whole
    r = env.claim(a, 203)
    assert r["claimed"] is False and r["reason"] == "claim_unknown" and r["unknown_reason"] == "too_long" and gh.writes() == []
    gh.fail("/issues/204", 500, times=-1, method="GET")
    r = env.claim(a, 204)
    assert r["reason"] == "claim_unknown" and r["unknown_reason"] == "error" and gh.writes() == []
    gh.world["overrides"].clear()
    r = env.claim(env.actor("node-c", login=None), 205)
    assert r["reason"] == "authors_unknown" and gh.writes() == []


def test_a_closed_issue_is_never_claimed(env):
    env.gh.world["issues"][K[201]]["state"] = "closed"
    r = env.claim(env.actor(), 201)
    assert r["claimed"] is False and r["reason"] == "closed" and env.gh.writes() == [] and env.claims(201) == []


def test_an_issue_closed_while_it_is_being_claimed_is_withdrawn(env, monkeypatch):
    gh = env.gh
    real = nodes_epics.sleep
    state = {"n": 0}

    def sleeper(s):
        real(s)
        state["n"] += 1
        if state["n"] == 1:
            gh.world["issues"][K[201]]["state"] = "closed"
    monkeypatch.setattr(nodes_epics, "sleep", sleeper)
    a = env.actor()
    r = env.claim(a, 201)
    assert r["claimed"] is False and r["reason"] == "closed"
    assert env.claims(201) == [] and gh.assignees_of(K[201]) == [] and gh.labels_of(K[201]) == [], "its own comment was deleted and no mark was made"
    assert [m for m, _, _ in gh.writes()] == ["POST", "DELETE"] and nodes_epics.held_claims(a) == []


def test_a_done_claim_is_not_claimed_again(env):
    env.foreign(201, "node-z", "done", age=5)
    r = env.claim(env.actor(), 201)
    assert r["reason"] == "done" and r["winner"] == "node-z" and env.gh.writes() == []


def test_a_claim_by_a_stranger_does_not_count_for_ordering_or_for_taking(env):
    gh = env.gh
    env.foreign(201, "node-z", age=5, user="mallory", assoc="NONE")                     # a live, earlier, claim-shaped comment of a stranger
    env.foreign(201, "node-y", age=5, user="alice", assoc="CONTRIBUTOR")
    a = env.actor()
    r = env.claim(a, 201)
    assert r["claimed"] is True, "ignored exactly as the reader ignores it"
    assert len(env.claims(201)) == 3 and sum(1 for c in env.claims(201) if c["id"] == nodes_epics.parse_ref(r["claim_ref"])[3]) == 1


def test_the_claim_reads_a_130_comment_thread_whole(env):
    gh = env.gh
    gh.junk(K[201], 129)
    env.foreign(201, "node-z", age=5)                                                    # comment 130: on the second page
    assert len(env.comments(201)) == 130
    a = env.actor()
    r = env.claim(a, 201)
    assert r["reason"] == "taken" and r["winner"] == "node-z", "the live claim on page 2 is found"
    assert any("page=2" in c["argv"][-1] for c in gh.calls())
    gh.junk(K[202], 130)
    r = env.claim(a, 202)
    assert r["claimed"] is True, "a whole 130 comment thread, then this board's own claim as the 131st"


def test_a_loser_deletes_its_own_comment_and_nothing_else(env):
    """B posts; a claim of node-z that was posted first (but shows late) appears: B withdraws. node-z's comment and every other comment stay."""
    gh = env.gh
    b = env.actor("node-b")
    other = env.foreign(201, "node-z", age=0, hidden=2.0)                              # posted before B's, invisible to B's step 1 and first reading
    keep = gh.comment(K[201], "a person says hello", user="bob", assoc="NONE", cid=other + 1)
    r = env.claim(b, 201)
    assert r["claimed"] is False and r["reason"] == "lost" and r["winner"] == "node-z" and r["claim_ref"] is None
    assert sorted(c["id"] for c in env.comments(201)) == [other, keep], "only B's own comment was removed"
    assert [m for m, _, _ in gh.writes()] == ["POST", "DELETE"] and gh.assignees_of(K[201]) == [] and gh.labels_of(K[201]) == [] and nodes_epics.held_claims(b) == []
    deleted = int(gh.writes()[1][1].rsplit("/", 1)[1])
    assert deleted not in (other, keep) and deleted > keep, "the DELETE named the comment this board had just created"


def test_a_node_never_wins_on_a_single_reading(env):
    """An earlier claim of node-z stays invisible for 2 s: the first reading (after 1.5 s) shows B first, the second (after 3 s) shows node-z first. B does not win."""
    gh = env.gh
    b = env.actor("node-b")
    env.foreign(201, "node-z", age=0, hidden=2.0)
    reads = []
    real = nodes_epics._read

    def spy(actor, o, r, n, repos):
        d = real(actor, o, r, n, repos)
        reads.append([c["alias"] for c in d.get("claims") or []])
        return d
    nodes_epics._read = spy
    try:
        r = env.claim(b, 201)
    finally:
        nodes_epics._read = real
    assert r["reason"] == "lost" and r["winner"] == "node-z" and env.sleeps == [1.5, 1.5]
    assert reads[-2:] == [["node-b"], ["node-z", "node-b"]], reads
    assert len(env.claims(201)) == 1 and env.parsed(201)[0]["alias"] == "node-z"


def test_a_node_whose_own_claim_is_not_visible_yet_does_not_believe_it_won_and_takes_it_back(env):
    """GitHub shows a new comment late (here 2 s): the first reading lacks the board's own claim, so it cannot win on it. The comment is deleted by its recorded id even
    though no reading showed it, and the answer is `claim_unknown`, not `lost` (nobody else was first)."""
    gh = env.gh
    gh.world["hide_new"] = 2.0
    a = env.actor("node-a")
    r = env.claim(a, 201)
    gh.world.pop("hide_new")
    assert r["claimed"] is False and r["reason"] == "claim_unknown" and r["winner"] is None
    assert env.claims(201) == [] and nodes_epics.held_claims(a) == [] and [m for m, _, _ in gh.writes()] == ["POST", "DELETE"] and gh.assignees_of(K[201]) == []


def test_ties_go_to_the_lower_comment_id(env):
    gh = env.gh
    now_iso = iso(gh.world["now"])
    a = env.actor("node-a")
    lower = env.foreign(201, "node-z", created_at=now_iso, cid=50, hidden=1.0)          # same second as A's claim, lower id, invisible to A's step 1
    r = env.claim(a, 201)
    assert r["reason"] == "lost" and r["winner"] == "node-z"
    # the other way round: a claim of node-y lands just after A's, in the same second, so it has the higher id and A is first
    gh.world.setdefault("next_cid", 100000)
    gh.mutate("/issues/202/comments?", K[202], "add", comment=claim_body("node-y", nid="cafef00d"), created_at=iso(gh.world["now"]), method="GET")
    r = env.claim(env.actor("node-b"), 202)                                              # a fresh queue: the claim is posted at once, in the second the other comment carries
    assert r["claimed"] is True, (r, "the same second but a higher id: this node is first")
    ids = [c["id"] for c in env.claims(202)]
    assert len(ids) == 2 and nodes_epics.parse_ref(r["claim_ref"])[3] == ids[0] < ids[1]
    assert {p["alias"] for p in env.parsed(202)} == {"node-b", "node-y"}


def test_the_verdicts_never_ask_the_local_clock(env, monkeypatch):
    """Claim, take, lose, renew, lapse, reclaim and release with the wall clock sabotaged: only the stamp on a lost record may read it (it is for pruning, not a decision)."""
    stamps = []
    real = time.time

    def clock():
        mod = sys._getframe(1).f_globals.get("__name__")
        if mod in ("app.nodes_epics", "app.github", "app.github_write"):
            stamps.append(1)
        return real() - 600
    monkeypatch.setattr(time, "time", clock)
    a, b = env.actor("node-a"), env.actor("node-b")
    env.foreign(202, "node-z", age=0, hidden=2.0)
    assert env.claim(b, 202)["reason"] == "lost"
    ra = env.claim(a, 201)
    assert ra["claimed"] and env.claim(b, 201)["reason"] == "taken"
    env.tick(300)
    assert nodes_epics.renew(ra["claim_ref"], actor=a)["renewed"]
    env.tick(31 * 60)
    rb = env.claim(b, 201)
    assert rb["claimed"] and rb["attempt"] == 2
    assert stamps == []
    assert nodes_epics.fence_ok(ra["claim_ref"], actor=a) is False
    assert len(stamps) == 1, "the one loss stamped its record"
    assert nodes_epics.release(rb["claim_ref"], "done", actor=b)["released"]
    assert len(stamps) == 1


def test_a_foreign_live_claim_can_never_be_overridden_by_an_earlier_looking_one_for_the_same_second(env):
    gh = env.gh
    a, b = env.actor("node-a"), env.actor("node-b")
    ra = env.claim(a, 201)
    assert ra["claimed"]
    assert env.claim(b, 201)["reason"] == "taken"
    assert env.claim(b, 202)["claimed"]


# ---- reclaim, lease, renewal, attempts

def test_a_claim_that_is_not_renewed_for_the_lease_is_not_live_and_a_second_node_reclaims(env):
    gh = env.gh
    a, b = env.actor("node-a"), env.actor("node-b")
    ra = env.claim(a, 201)
    cid_a = nodes_epics.parse_ref(ra["claim_ref"])[3]
    env.tick(29 * 60)
    assert env.claim(b, 201)["reason"] == "taken", "still live 29 minutes after the last update"
    env.tick(2 * 60)
    rb = env.claim(b, 201)
    assert rb["claimed"] is True and rb["attempt"] == 2 and rb["reclaims"] == cid_a
    pb = [p for p in env.parsed(201) if p["alias"] == "node-b"][0]
    assert pb["reclaims"] == cid_a and pb["attempt"] == 2 and pb["status"] == "claimed"
    assert len(env.claims(201)) == 2, "the dead node's comment is left alone: it is not live and it is not this board's to delete"


def test_the_first_nodes_next_fenced_write_is_refused_and_the_task_is_parked(env):
    gh = env.gh
    a, b = env.actor("node-a"), env.actor("node-b")
    seen = []
    hook = lambda view, reason: seen.append((view, reason))                       # noqa: E731
    nodes_epics.LOST_HOOKS.append(hook)
    try:
        ra = env.claim(a, 201)
        assert nodes_epics.attach_task(ra["claim_ref"], 42, actor=a)
        env.tick(31 * 60)
        assert env.claim(b, 201)["claimed"]
        n = len(gh.writes())
        assert nodes_epics.fence_ok(ra["claim_ref"], actor=a) is False
        assert seen and seen[0][1] == "expired" and seen[0][0]["claim_ref"] == ra["claim_ref"] and seen[0][0]["task_id"] == 42 and seen[0][0]["state"] == "lost"
        assert len(seen) == 1
        assert nodes_epics.renew(ra["claim_ref"], actor=a)["reason"] == "lost" and nodes_epics.start(ra["claim_ref"], actor=a)["reason"] == "lost"
        assert nodes_epics.release(ra["claim_ref"], "done", actor=a)["reason"] == "lost" and nodes_epics.fence_ok(ra["claim_ref"], actor=a) is False
        assert len(gh.writes()) == n, "after the loss not one more write reaches GitHub"
        assert len(seen) == 1, "the loss is announced once"
        [v] = nodes_epics.held_claims(a)
        assert v["state"] == "lost" and v["lost_reason"] == "expired"
        rows = [r for r in nodes.audit_list(20, db=a.db) if r["action"] == "epic-claim-lost"]
        assert len(rows) == 1 and rows[0]["target"] == "acme/shop#201" and rows[0]["status"] == "failed" and rows[0]["detail"] == "expired"
        assert not re.search(r"alice|node-a|ghp_|@|/home", json.dumps(rows)), rows
    finally:
        nodes_epics.LOST_HOOKS.remove(hook)                                       # only its own hook: clearing the list took the run phase's hook away from every later test


def test_renewal_edits_the_own_comment_moves_updated_at_raises_seq_and_keeps_the_claim_live(env):
    gh = env.gh
    a, b = env.actor("node-a"), env.actor("node-b")
    ra = env.claim(a, 201)
    ref = ra["claim_ref"]
    first = env.claims(201)[0]
    t0 = first["updated_at"]
    for i in range(7):                                                                  # 35 minutes of heartbeats, five minutes apart
        env.tick(300)
        r = nodes_epics.renew(ref, actor=a)
        assert r["renewed"] is True and r["seq"] == i + 2 and r["reason"] == "ok"
    c = env.claims(201)[0]
    p = nodes_epics.parse_claim(c["body"])
    assert c["id"] == first["id"] and c["created_at"] == first["created_at"] and c["updated_at"] != t0 and p["seq"] == 8
    assert (p["alias"], p["nid"], p["attempt"], p["lease_min"], p["status"]) == ("node-a", nodes_epics.node_nid(a.node_id), 1, 30, "claimed")
    assert env.claim(b, 201)["reason"] == "taken", "renewed: live well past the 30 minutes since it was posted"
    assert [m for m, _, _ in gh.writes()].count("PATCH") == 7 and nodes_epics.held_claims(a)[0]["seq"] == 8 and nodes_epics.held_claims(a)[0]["renewals"] == 7


def test_an_edit_with_the_same_body_would_not_move_updated_at_so_seq_always_changes(env):
    a = env.actor()
    ref = env.claim(a, 201)["claim_ref"]
    u0 = env.claims(201)[0]["updated_at"]
    env.tick(5)
    nodes_epics.renew(ref, actor=a)
    assert env.claims(201)[0]["updated_at"] != u0


def test_start_marks_running_with_the_task_and_branch_and_keeps_seq(env):
    a = env.actor()
    ref = env.claim(a, 201)["claim_ref"]
    env.tick(60)
    r = nodes_epics.start(ref, "box:42", "feat/epic-x", actor=a)
    assert r["renewed"] and r["seq"] == 1
    p = env.parsed(201)[0]
    assert (p["status"], p["seq"], p["task_ref"], p["branch"]) == ("running", 1, "box:42", "feat/epic-x")
    env.tick(300)
    nodes_epics.renew(ref, actor=a)
    p = env.parsed(201)[0]
    assert (p["status"], p["seq"], p["task_ref"], p["branch"]) == ("running", 2, "box:42", "feat/epic-x"), "a heartbeat keeps the status, the task and the branch"
    assert nodes_epics.renew(ref, actor=a, status="done")["reason"] == "bad_request" and nodes_epics.renew(ref, actor=a, branch="x y")["reason"] == "bad_request"


def test_attempt_rises_with_each_reclaim_and_stops_after_three_with_one_fixed_comment(env):
    gh = env.gh
    actors = [env.actor(f"node-{c}") for c in "abcd"]
    for i, a in enumerate(actors[:3]):
        r = env.claim(a, 201)
        assert r["claimed"] and r["attempt"] == i + 1, r
        env.tick(31 * 60)
    n = len(gh.writes())
    r = env.claim(actors[3], 201)
    assert r["claimed"] is False and r["reason"] == "attempts_exhausted" and r["asked_person"] is True and r["attempt"] == 3
    assert [c["body"] for c in env.comments(201)].count(GW.HELP_BODY) == 1 and len(gh.writes()) == n + 1
    help_c = [c for c in env.comments(201) if c["body"] == GW.HELP_BODY][0]
    assert nodes_epics.parse_claim(help_c["body"]) is None, "no marker: no node counts it as a claim"
    r2 = env.claim(actors[3], 201)
    assert r2["reason"] == "attempts_exhausted" and r2["asked_person"] is False and [c["body"] for c in env.comments(201)].count(GW.HELP_BODY) == 1
    assert len(env.claims(201)) == 3 and nodes_epics.held_claims(actors[3]) == []


def test_the_help_comment_text_is_fixed_and_plain():
    assert "three" in GW.HELP_BODY and "person" in GW.HELP_BODY and "—" not in GW.HELP_BODY and "—" not in GW.CLAIM_TEXT and "<!--" not in GW.HELP_BODY and "ccboard claim" not in GW.HELP_BODY


def test_a_failed_attempt_counts_and_a_released_one_can_be_reclaimed(env):
    a, b = env.actor("node-a"), env.actor("node-b")
    ra = env.claim(a, 201)
    assert nodes_epics.release(ra["claim_ref"], "failed", actor=a)["released"]
    rb = env.claim(b, 201)
    assert rb["claimed"] and rb["attempt"] == 2 and rb["reclaims"] == nodes_epics.parse_ref(ra["claim_ref"])[3]


# ---- release

def test_release_edits_the_status_and_takes_back_only_what_this_board_added(env):
    gh = env.gh
    a = env.actor()
    ref = env.claim(a, 201)["claim_ref"]
    env.tick(60)
    before = len(gh.writes())
    r = nodes_epics.release(ref, "done", actor=a)
    assert r == {"released": True, "reason": "ok", "cleanup": {"label": "removed", "assignee": "removed"}}
    assert env.kinds()[before:] == [("PATCH", f"issues/comments/{nodes_epics.parse_ref(ref)[3]}"), ("DELETE", "labels/ccboard%3Ain-progress"), ("DELETE", "assignees")]
    p = env.parsed(201)[0]
    assert p["status"] == "done" and p["seq"] == 2 and gh.labels_of(K[201]) == [] and gh.assignees_of(K[201]) == []
    assert nodes_epics.held_claims(a) == [] and nodes_epics.fence_ok(ref, actor=a) is False
    r = nodes_epics.release(ref, "done", actor=a)
    assert r["released"] is False and r["reason"] == "unknown_claim"


@pytest.mark.parametrize("status", ["done", "failed", "cancelled", "released"])
def test_each_release_status_is_written_into_the_comment(env, status):
    a = env.actor()
    ref = env.claim(a, 201)["claim_ref"]
    assert nodes_epics.release(ref, status, actor=a)["released"] is True
    assert env.parsed(201)[0]["status"] == status
    env.tick(10)
    assert nodes_epics.is_live({"status": status, "updated_at": env.claims(201)[0]["updated_at"], "lease_min": 30}, env.gh.world["now"]) is False


def test_release_does_not_remove_an_assignee_or_a_label_it_did_not_add(env):
    gh = env.gh
    gh.world["issues"][K[201]]["assignees"] = ["alice"]                                 # a person assigned the same login before
    gh.world["issues"][K[201]]["labels"] = ["ccboard:in-progress"]                      # and the label was there already (nobody's claim: a fresh claim does not own it)
    gh.world["repo_labels"] = {REPO: ["ccboard:in-progress"]}
    a = env.actor()
    ref = env.claim(a, 201)["claim_ref"]
    [v] = nodes_epics.held_claims(a)
    assert v["added_assignee"] is False and v["added_label"] is False
    r = nodes_epics.release(ref, "done", actor=a)
    assert r["released"] and r["cleanup"] == {"label": None, "assignee": None}
    assert gh.assignees_of(K[201]) == ["alice"] and gh.labels_of(K[201]) == ["ccboard:in-progress"]
    assert all(m != "DELETE" for m, _, _ in gh.writes()), gh.writes()


def test_a_board_that_takes_over_a_dead_holders_claim_also_takes_over_its_label(env):
    gh = env.gh
    a, b = env.actor("node-a"), env.actor("node-b")
    env.claim(a, 201)
    env.tick(31 * 60)
    rb = env.claim(b, 201)
    [v] = nodes_epics.held_claims(b)
    assert v["added_label"] is True and v["added_assignee"] is False, "the label is ccboard's own; the assignee may be a person's, so it is never inherited"
    nodes_epics.release(rb["claim_ref"], "done", actor=b)
    assert gh.labels_of(K[201]) == [] and gh.assignees_of(K[201]) == ["alice"]


def test_release_through_a_refused_assignee_and_label_still_works(env):
    gh = env.gh
    gh.world["no_push"] = [REPO]                                                        # a login without push access: the assignee is ignored, the label is refused
    a = env.actor()
    r = env.claim(a, 201)
    assert r["claimed"] is True and set(r["notes"]) == {"no_push_access", "label_no_access"}
    [v] = nodes_epics.held_claims(a)
    assert v["added_assignee"] is False and v["added_label"] is False and gh.assignees_of(K[201]) == [] and gh.labels_of(K[201]) == []
    assert nodes_epics.release(r["claim_ref"], "done", actor=a)["released"]


def test_a_refused_label_creation_does_not_stop_the_claim(env):
    gh = env.gh
    gh.world["label_create_forbidden"] = True
    r = env.claim(env.actor(), 201)
    assert r["claimed"] is True and "label_create_refused" in r["notes"] and gh.labels_of(K[201]) == [] and gh.assignees_of(K[201]) == ["alice"]


def test_a_label_that_already_exists_is_just_added(env):
    gh = env.gh
    gh.world["repo_labels"] = {REPO: ["ccboard:in-progress"]}
    r = env.claim(env.actor(), 201)
    assert r["claimed"] and [k for k in env.kinds()] == [("POST", "comments"), ("POST", "assignees"), ("POST", "labels")]


# ---- the fence

def held(env, name="node-a", n=201):
    a = env.actor(name)
    r = env.claim(a, n)
    assert r["claimed"], r
    return a, r["claim_ref"]


def test_the_fence_holds_while_the_claim_is_live_and_first(env):
    a, ref = held(env)
    for _ in range(3):
        env.tick(120)
        assert nodes_epics.fence_ok(ref, actor=a) is True
    f = nodes_epics.fence(ref, actor=a)
    assert f == {"ok": True, "lost": False, "reason": None, "winner": None}


def test_a_deleted_claim_comment_makes_the_holder_drop_out_and_write_nothing(env):
    gh = env.gh
    a, ref = held(env)
    cid = nodes_epics.parse_ref(ref)[3]
    gh.world["comments"][K[201]] = [c for c in gh.world["comments"][K[201]] if c["id"] != cid]
    n = len(gh.writes())
    f = nodes_epics.fence(ref, actor=a)
    assert f["ok"] is False and f["lost"] is True and f["reason"] == "deleted"
    assert nodes_epics.renew(ref, actor=a)["lost"] is True and nodes_epics.release(ref, "done", actor=a)["released"] is False and len(gh.writes()) == n


def test_a_claim_comment_edited_into_something_else_is_a_lost_claim(env):
    gh = env.gh
    for how, reason in (("text", "unparseable"), ("nid", "edited"), ("status", "status_changed"), ("author", "not_ours")):
        a, ref = held(env, f"node-{how}", 201 + ["text", "nid", "status", "author"].index(how))
        n = 201 + ["text", "nid", "status", "author"].index(how)
        c = next(c for c in gh.world["comments"][K[n]] if c["id"] == nodes_epics.parse_ref(ref)[3])
        if how == "text":
            c["body"] = "I took this out"
        elif how == "nid":
            c["body"] = c["body"].replace(nodes_epics.node_nid(a.node_id), "deadbeef")
        elif how == "status":
            c["body"] = c["body"].replace("status=claimed", "status=released")
        else:
            c["user"] = "mallory"
        w = len(gh.writes())
        f = nodes_epics.fence(ref, actor=a)
        assert f["ok"] is False and f["lost"] is True and f["reason"] == reason, (how, f)
        assert nodes_epics.renew(ref, actor=a)["renewed"] is False and len(gh.writes()) == w


def test_an_earlier_live_claim_that_appears_makes_the_holder_lose(env):
    gh = env.gh
    a, ref = held(env)
    env.foreign(201, "node-z", age=0, created_at="2020-01-01T00:00:00Z")                 # live (updated now) and earlier by created_at
    f = nodes_epics.fence(ref, actor=a)
    assert f["ok"] is False and f["lost"] is True and f["reason"] == "not_first" and f["winner"] == "node-z"


def test_the_fence_fails_closed_on_any_read_problem_without_calling_it_a_loss(env):
    gh = env.gh
    a, ref = held(env)
    n = len(gh.writes())
    for status, hdr in ((500, None), (429, {"retry-after": "30"}), (403, None), (502, None)):
        github.reader.reset_state()
        gh.fail("/issues/201", status, headers=hdr, times=-1, method="GET")
        f = nodes_epics.fence(ref, actor=a)
        assert f["ok"] is False and f["lost"] is False, (status, f)
        assert nodes_epics.fence_ok(ref, actor=a) is False and nodes_epics.renew(ref, actor=a)["renewed"] is False and nodes_epics.release(ref, "done", actor=a)["released"] is False
        gh.world["overrides"].clear()
    github.reader.reset_state()
    assert len(gh.writes()) == n and nodes_epics.held_claims(a)[0]["state"] == "held"
    assert nodes_epics.fence_ok(ref, actor=a) is True, "the problem passed: the holder was never marked lost"


def test_the_fence_does_not_call_a_hidden_middle_of_a_long_thread_a_loss(env):
    gh = env.gh
    a, ref = held(env)
    gh.junk(K[201], 400, start=gh.world["now"] + 1, first_id=10 ** 6)
    f = nodes_epics.fence(ref, actor=a)
    assert f["ok"] is False and f["lost"] is False


def test_a_claim_ref_this_board_never_made_is_not_a_held_claim(env):
    gh = env.gh
    a, ref = held(env)
    other = env.foreign(202, "node-z", age=5)
    n = len(gh.writes())
    for bad in (f"acme/shop#202:{other}", f"acme/shop#201:{other}", "acme/shop#203:100999", "acme/shop#201:1", "nonsense", None, "other/lib#201:" + str(nodes_epics.parse_ref(ref)[3])):
        assert nodes_epics.fence_ok(bad, actor=a) is False
        assert nodes_epics.renew(bad, actor=a)["renewed"] is False and nodes_epics.start(bad, actor=a)["renewed"] is False
        assert nodes_epics.release(bad, "done", actor=a)["released"] is False
    assert len(gh.writes()) == n, "a peer or a payload naming a comment id changes nothing on GitHub"


def test_a_tampered_record_cannot_make_the_board_edit_a_comment_that_is_not_its_own(env):
    gh = env.gh
    a, ref = held(env)
    other = env.foreign(201, "node-z", age=0, created_at="2030-01-01T00:00:00Z")
    rec = a.db.kv_get("claim:acme/shop#201")["value"]
    a.db.kv_set("claim:acme/shop#201", {**rec, "comment_id": other})
    n = len(gh.writes())
    forged_ref = nodes_epics.make_ref("acme", "shop", 201, other)
    r = nodes_epics.renew(forged_ref, actor=a)
    assert r["renewed"] is False and r["lost"] is True
    assert nodes_epics.release(forged_ref, "done", actor=a)["released"] is False and len(gh.writes()) == n
    assert next(c for c in env.comments(201) if c["id"] == other)["body"].startswith("<!-- ccboard:claim")


def test_every_edit_and_delete_goes_to_a_comment_this_board_created(env, monkeypatch):
    """Across a claim, a loss, a reclaim, a renewal, a release and a withdrawal: every PATCH and DELETE names an id that the same board's own POST returned."""
    created: dict[str, set] = {}
    touched: list[tuple[str, str, int]] = []
    real = nodes_epics._send

    def spy(actor, req):
        out = real(actor, req)
        m = re.search(r"/comments/([0-9]+)$", req.path)
        if req.method == "POST" and req.path.endswith("/comments"):
            created.setdefault(actor.alias, set()).add(out[2]["id"])
        elif m:
            touched.append((actor.alias, req.method, int(m.group(1))))
        return out
    monkeypatch.setattr(nodes_epics, "_send", spy)
    gh = env.gh
    a, b, c = env.actor("node-a"), env.actor("node-b"), env.actor("node-c")
    ra = env.claim(a, 201)
    env.claim(b, 201)                                                                   # taken
    nodes_epics.renew(ra["claim_ref"], actor=a)
    env.foreign(202, "node-z", age=0, hidden=2.0)
    env.claim(b, 202)                                                                   # lost: a withdrawal
    env.tick(31 * 60)
    rc = env.claim(c, 201)                                                              # a reclaim
    nodes_epics.renew(rc["claim_ref"], actor=c)
    nodes_epics.release(rc["claim_ref"], "done", actor=c)
    assert touched and all(i in created.get(who, set()) for who, _, i in touched), (touched, created)
    assert {m for _, m, _ in touched} == {"PATCH", "DELETE"}


def test_a_loser_whose_comment_was_meanwhile_replaced_by_somebody_is_not_deleted(env):
    """The withdrawal re-reads first: a comment at the recorded id that is no longer the board's own (another author) is never deleted."""
    gh = env.gh
    b = env.actor("node-b")
    env.foreign(201, "node-z", age=0, hidden=2.0)
    real = nodes_epics._decide

    def decide(actor, rec, repos):
        for c in gh.world["comments"][K[201]]:
            if c["id"] == rec["comment_id"]:
                c["user"] = "mallory"
        return real(actor, rec, repos)
    nodes_epics._decide = decide
    try:
        r = env.claim(b, 201)
    finally:
        nodes_epics._decide = real
    assert r["claimed"] is False and r["reason"] in ("withdraw_failed", "claim_unknown")
    assert all(m != "DELETE" for m, _, _ in gh.writes()), "somebody else's comment was not deleted"


# ---- write targets: only this board's own repositories and issues of an epic it has read

def test_a_repository_that_is_not_this_boards_own_is_refused_before_any_request(env):
    gh = env.gh
    a = env.actor()
    gh.add_issue("other/lib", 5, "Theirs")
    read_epic([5], epic=1)
    for repo in ("other/lib", "acme/other", "acme/SHOP/x", "acme/shop/../x", "acme", "", "/", "a b/c", "-x/y", "acme/shop;x", None, 5, "acme\\shop"):
        r = nodes_epics.claim_issue(repo, 201, actor=a)
        assert r["claimed"] is False and r["reason"] in ("other_repo", "bad_request"), (repo, r)
    assert gh.calls() == []


def test_the_repository_spelling_is_this_boards_own_and_case_does_not_choose_a_target(env):
    gh = env.gh
    a = env.actor(repos=["Acme/Shop"])
    gh.add_issue("Acme/Shop", 201, "Mixed case")
    nodes_epics._last_good[("acme", "shop", 147)] = (nodes_epics.mono(), {"epic": {"labelled": True}, "children": [{"repo": "acme/shop", "number": 201}]})
    r = nodes_epics.claim_issue("acme/shop", 201, actor=a)
    assert r["claimed"] is True and all("/repos/Acme/Shop/" in p for _, p, _ in gh.writes())


def test_an_issue_that_is_not_a_child_of_an_epic_this_board_read_is_refused(env):
    gh = env.gh
    a = env.actor()
    gh.add_issue(REPO, 300, "Not in any epic")
    for n in (300, 147, 999):
        r = env.claim(a, n)
        assert r["claimed"] is False and r["reason"] == "not_epic_child", (n, r)
    assert gh.calls() == []
    nodes_epics._last_good.clear()
    assert env.claim(a, 201)["reason"] == "not_epic_child", "an epic that was not read (a restart) makes nothing claimable"
    assert gh.calls() == []


def test_a_hostile_issue_number_is_refused_before_any_request(env):
    gh = env.gh
    a = env.actor()
    for n in (0, -1, True, False, "01", "1;2", 10 ** 9, 1.5, None, "1 2", "../5", "", [201], {"n": 201}):
        r = nodes_epics.claim_issue(REPO, n, actor=a)
        assert r["claimed"] is False and r["reason"] == "bad_request", (n, r)
    assert gh.calls() == []
    r = nodes_epics.claim_issue(REPO, "201", actor=a)
    assert r["claimed"] is True, "a decimal string is an issue number"


def test_free_text_for_the_task_or_branch_never_enters_a_comment(env):
    gh = env.gh
    a = env.actor()
    for kw in ({"task_ref": "x y; rm -rf /"}, {"branch": "$(evil)"}, {"task_ref": "/etc/passwd"}, {"branch": "../../x"}, {"task_ref": "ghp_" + "A1b2C3d4" * 4}):
        r = env.claim(a, 201, **kw)
        assert r["claimed"] is False and r["reason"] == "bad_request", (kw, r)
    assert gh.writes() == []
    r = env.claim(a, 201, task_ref="box:42", branch="feat/x")
    assert r["claimed"] and env.parsed(201)[0]["task_ref"] == "box:42"


def test_the_login_must_be_one_whose_claims_count(env, monkeypatch):
    gh = env.gh
    monkeypatch.setattr(settings, "epic_authors", ("bob",))
    r = env.claim(env.actor(), 201)
    assert r["reason"] == "login_not_allowed" and gh.writes() == [] and gh.calls() == []
    monkeypatch.setattr(settings, "epic_authors", ("alice",))
    assert env.claim(env.actor("node-b"), 201)["claimed"] is True


def test_too_many_held_claims_are_refused(env, monkeypatch):
    monkeypatch.setattr(nodes_epics, "HELD_MAX", 2)
    a = env.actor()
    assert env.claim(a, 201)["claimed"] and env.claim(a, 202)["claimed"]
    r = env.claim(a, 203)
    assert r["reason"] == "too_many_claims" and env.claims(203) == []


# ---- failures: the unknown outcome is read back, nothing half is left

def test_a_5xx_on_the_claim_comment_returns_error_and_leaves_nothing(env):
    gh = env.gh
    a = env.actor()
    gh.fail("/comments", 502, method="POST")
    r = env.claim(a, 201)
    assert r["claimed"] is False and r["reason"] == "error" and r["unknown_outcome"] is False and r["claim_ref"] is None
    assert env.claims(201) == [] and gh.assignees_of(K[201]) == [] and gh.labels_of(K[201]) == [] and nodes_epics.held_claims(a) == []
    assert [m for m, _, _ in gh.writes()] == ["POST"], "one attempt; nothing else written"
    assert a.db.kv_get("claim-pending:acme/shop#201") is None, "two whole readings showed nothing was written: no marker is left"
    r = env.claim(a, 201)
    assert r["claimed"] is True and len(env.claims(201)) == 1


def test_a_comment_created_but_the_answer_lost_is_adopted_and_never_posted_twice(env):
    gh = env.gh
    a = env.actor()
    gh.fail("/comments", 502, method="POST", apply=True)
    r = env.claim(a, 201)
    assert r["claimed"] is True and r["reason"] == "won"
    assert len([w for w in gh.writes() if w[1].endswith("/issues/201/comments")]) == 1, "one POST of the claim comment, never two"
    [c] = env.claims(201)
    assert nodes_epics.parse_ref(r["claim_ref"])[3] == c["id"] and nodes_epics.held_claims(a)[0]["state"] == "held"
    assert a.db.kv_get("claim-pending:acme/shop#201") is None


def test_a_timeout_after_the_write_is_read_back_the_same_way(env):
    gh = env.gh
    a = env.actor()
    gh.fail("/comments", 200, method="POST", apply=True, hang=True)
    r = env.claim(a, 201)
    assert r["claimed"] is True and len(env.claims(201)) == 1
    gh.fail("/comments", 200, method="POST", hang=True)                               # nothing applied this time
    r = env.claim(a, 202)
    assert r["claimed"] is False and r["reason"] == "error" and env.claims(202) == []


def test_an_unreadable_read_back_leaves_a_marker_and_the_next_call_sweeps_the_orphan(env, monkeypatch):
    gh = env.gh
    a = env.actor()
    gh.fail("/comments", 502, method="POST", apply=True)
    real = nodes_epics.fresh_claim_state
    calls = {"n": 0}

    def flaky(*args, **kw):                                                           # the step-1 read works; the two read-backs fail
        calls["n"] += 1
        if calls["n"] > 1:
            return {"state": "unknown", "reason": "error", "issue_state": None}
        return real(*args, **kw)
    monkeypatch.setattr(nodes_epics, "fresh_claim_state", flaky)
    r = env.claim(a, 201)
    monkeypatch.setattr(nodes_epics, "fresh_claim_state", real)
    assert r["claimed"] is False and r["reason"] == "outcome_unknown" and r["unknown_outcome"] is True
    assert len(env.claims(201)) == 1, "the comment is there on GitHub although this board could not see it"
    assert a.db.kv_get("claim-pending:acme/shop#201") is not None and nodes_epics.held_claims(a) == []
    r = env.claim(a, 201)
    assert r["claimed"] is True
    assert len(env.claims(201)) == 1, "the orphan was this board's own: it was deleted before the new claim, so one comment from this board for the attempt"
    assert [w[0] for w in gh.writes()].count("DELETE") == 1 and a.db.kv_get("claim-pending:acme/shop#201") is None


def test_no_second_claim_comment_from_one_board_for_one_attempt_under_concurrent_calls(env, monkeypatch):
    fake_wait = nodes_epics.sleep
    monkeypatch.setattr(nodes_epics, "sleep", lambda s: (time.sleep(0.02), fake_wait(s)))        # the calls really overlap: each protocol wait lets the others run
    a = env.actor()
    out = []
    ts = [threading.Thread(target=lambda: out.append(env.claim(a, 201))) for _ in range(4)]
    [t.start() for t in ts]
    [t.join(20) for t in ts]
    assert sorted(r["reason"] for r in out) == ["already_held", "already_held", "already_held", "won"]
    assert len(env.claims(201)) == 1


def test_the_secondary_limit_on_the_claim_comment_is_a_rate_limited_answer_and_the_next_write_waits(env):
    gh = env.gh
    a = env.actor()
    gh.fail("/comments", 403, method="POST", body={"message": "You have exceeded a secondary rate limit. Please wait a few minutes before you try again."})
    r = env.claim(a, 201)
    assert r["claimed"] is False and r["reason"] == "rate_limited" and r["wait_s"] == 60 and env.claims(201) == [] and len(gh.writes()) == 1
    t0 = gh.world["now"]
    r = env.claim(a, 202)
    assert r["claimed"] is True and gh.world["now"] - t0 >= 60, "the second claim's write waited out the minute on the queue"
    assert a.wq.slept >= 59


def test_a_refused_comment_is_a_fixed_reason(env):
    gh = env.gh
    a = env.actor()
    gh.fail("/comments", 403, method="POST", body={"message": "Resource not accessible by integration"})
    assert env.claim(a, 201)["reason"] == "no_access"
    gh.fail("/comments", 401, method="POST")
    assert env.claim(a, 202)["reason"] == "no_login"
    gh.fail("/comments", 404, method="POST")
    assert env.claim(a, 203)["reason"] == "error"


def test_a_refused_assignee_or_label_write_is_a_note_not_a_failure(env):
    gh = env.gh
    gh.fail("/assignees", 500, method="POST")
    gh.fail("/labels", 502, method="POST", times=3)
    a = env.actor()
    r = env.claim(a, 201)
    assert r["claimed"] is True and "assignee_error" in r["notes"] and any(x.startswith("label_") for x in r["notes"])
    assert len(env.claims(201)) == 1


# ---- the audit

def test_audit_rows_hold_the_repository_and_the_issue_number_and_a_reason_word_only(env):
    gh = env.gh
    a, b = env.actor("node-a"), env.actor("node-b")
    ra = env.claim(a, 201)
    env.foreign(202, "node-z", age=0, hidden=2.0)
    env.claim(b, 202)
    nodes_epics.release(ra["claim_ref"], "done", actor=a)
    rows = nodes.audit_list(50, db=a.db) + nodes.audit_list(50, db=b.db)
    assert {r["action"] for r in rows} == {"epic-claim", "epic-claim-release"}
    for r in rows:
        assert r["target"] in ("acme/shop#201", "acme/shop#202") and r["direction"] == "out" and r["peer"] == "" and not r["user"]
        assert not re.search(r"alice|node-a|node-b|ghp_|/home|@", json.dumps(r)), r
    assert [r["detail"] for r in nodes.audit_list(50, db=b.db)] == ["lost"]


# ---- the renew timer

def test_a_board_with_no_epic_and_no_claim_runs_nothing_in_the_background(env):
    a = env.actor()
    before = {t.name for t in threading.enumerate()}
    assert nodes_epics.renewer_running() is False
    nodes_epics.fresh_claim_state("acme", "shop", 201, [REPO], "alice")
    env.claim(a, 999)                                                                   # refused: not a child
    env.gh.world["issues"][K[202]]["state"] = "closed"
    env.claim(a, 202)
    assert nodes_epics.renewer_running() is False and {t.name for t in threading.enumerate()} == before and not any("renew" in t.name for t in threading.enumerate())


def test_the_renew_timer_exists_only_while_a_claim_is_held_and_stops_on_shutdown(env, monkeypatch):
    monkeypatch.setattr(nodes_epics, "RENEWER", True)
    monkeypatch.setattr(nodes_epics, "RENEW_S", 0.02)
    gh = env.gh
    a = env.actor()
    ref = env.claim(a, 201)["claim_ref"]
    assert nodes_epics.renewer_running() is True
    t_end = time.monotonic() + 5
    while time.monotonic() < t_end and nodes_epics.held_claims(a)[0]["renewals"] < 3:
        env.tick(30)                                                                   # the fake server clock moves; the thread waits milliseconds
        time.sleep(0.01)
    assert nodes_epics.held_claims(a)[0]["renewals"] >= 3 and [m for m, _, _ in gh.writes()].count("PATCH") >= 3
    nodes_epics.stop_renewals(5.0)
    assert nodes_epics.renewer_running() is False and not any(t.name == "ccboard-epic-renew" and t.is_alive() for t in threading.enumerate())
    touched = []
    real_get, real_set = a.db.kv_get, a.db.kv_set
    a.db.kv_get = lambda *x, **k: touched.append("get") or real_get(*x, **k)
    a.db.kv_set = lambda *x, **k: touched.append("set") or real_set(*x, **k)
    w = len(gh.writes())
    time.sleep(0.15)
    assert touched == [] and len(gh.writes()) == w, "after stop the timer never touches the database or GitHub again"
    a.db.kv_get, a.db.kv_set = real_get, real_set
    assert nodes_epics.fence_ok(ref, actor=a) is True


def test_the_renew_timer_ends_when_the_last_claim_is_released(env, monkeypatch):
    monkeypatch.setattr(nodes_epics, "RENEWER", True)
    monkeypatch.setattr(nodes_epics, "RENEW_S", 0.02)
    a = env.actor()
    ref = env.claim(a, 201)["claim_ref"]
    assert nodes_epics.release(ref, "done", actor=a)["released"]
    t_end = time.monotonic() + 3
    while time.monotonic() < t_end and nodes_epics.renewer_running():
        time.sleep(0.01)
    assert nodes_epics.renewer_running() is False


def test_the_timer_is_bounded_a_claim_is_renewed_a_fixed_number_of_times(env, monkeypatch):
    monkeypatch.setattr(nodes_epics, "RENEWER", True)
    monkeypatch.setattr(nodes_epics, "RENEW_S", 0.01)
    monkeypatch.setattr(nodes_epics, "RENEW_MAX", 2)
    gh = env.gh
    a = env.actor()
    env.claim(a, 201)
    t_end = time.monotonic() + 3
    while time.monotonic() < t_end and nodes_epics.held_claims(a)[0]["renewals"] < 2:
        env.tick(30)
        time.sleep(0.01)
    time.sleep(0.1)
    assert nodes_epics.held_claims(a)[0]["renewals"] == 2 and [m for m, _, _ in gh.writes()].count("PATCH") == 2
    nodes_epics.stop_renewals(5.0)


def test_the_timer_marks_a_lost_claim_and_stops_writing(env, monkeypatch):
    monkeypatch.setattr(nodes_epics, "RENEWER", True)
    monkeypatch.setattr(nodes_epics, "RENEW_S", 0.02)
    gh = env.gh
    a = env.actor()
    env.claim(a, 201)
    env.tick(40 * 60)                                                                  # the lease ran out before the next heartbeat
    t_end = time.monotonic() + 3
    while time.monotonic() < t_end and nodes_epics.held_claims(a)[0]["state"] != "lost":
        time.sleep(0.01)
    assert nodes_epics.held_claims(a)[0]["state"] == "lost"
    n = len(gh.writes())
    time.sleep(0.1)
    assert len(gh.writes()) == n and [m for m, _, _ in gh.writes()].count("PATCH") == 0


# ---- a restart: renew, release or drop what the record says

def test_a_restart_can_renew_release_or_drop_a_recorded_claim(env, monkeypatch):
    gh = env.gh
    a, ref = held(env)
    again = env.actor("node-a")                                                         # the same database file, a new process
    assert again.db is not a.db
    assert [v["claim_ref"] for v in nodes_epics.held_claims(again)] == [ref]
    monkeypatch.setattr(nodes_epics, "RENEWER", True)
    monkeypatch.setattr(nodes_epics, "RENEW_S", 3600)
    did = nodes_epics.recover(again)
    assert [d["did"] for d in did] == ["held"] and nodes_epics.renewer_running() is True
    nodes_epics.stop_renewals(5.0)
    env.tick(120)
    assert nodes_epics.renew(ref, actor=again)["renewed"] is True
    assert nodes_epics.release(ref, "released", actor=again)["released"] is True
    a2, ref2 = held(env, "node-b", 202)
    n = len(gh.writes())
    assert nodes_epics.drop_claim(ref2, actor=a2) is True and nodes_epics.held_claims(a2) == [] and len(gh.writes()) == n, "drop writes nothing"
    assert nodes_epics.drop_claim(ref2, actor=a2) is False


def test_recover_takes_back_an_unconfirmed_claim_and_forgets_old_lost_records(env, monkeypatch):
    gh = env.gh
    a = env.actor()
    real = nodes_epics._decide

    def crash(actor, rec, repos):
        raise RuntimeError("the process died between the post and the decision")
    monkeypatch.setattr(nodes_epics, "_decide", crash)
    with pytest.raises(RuntimeError):
        env.claim(a, 201)
    monkeypatch.setattr(nodes_epics, "_decide", real)
    assert len(env.claims(201)) == 1 and nodes_epics.held_claims(a)[0]["state"] == "claiming"
    again = env.actor("node-a")
    did = nodes_epics.recover(again)
    assert [d["did"] for d in did] == ["withdrawn"] and env.claims(201) == [] and nodes_epics.held_claims(again) == []
    ra = env.claim(again, 202)
    env.tick(40 * 60)
    nodes_epics.fence_ok(ra["claim_ref"], actor=again)
    assert nodes_epics.held_claims(again)[0]["state"] == "lost"
    rec = again.db.kv_get("claim:acme/shop#202")["value"]
    again.db.kv_set("claim:acme/shop#202", {**rec, "lost_at": time.time() - 8 * 86400})
    assert [d["did"] for d in nodes_epics.recover(again)] == ["forgotten"] and nodes_epics.held_claims(again) == []


def test_a_claim_that_died_between_post_and_decision_is_taken_back_by_the_next_claim(env, monkeypatch):
    a = env.actor()
    real = nodes_epics._decide
    monkeypatch.setattr(nodes_epics, "_decide", lambda *x: (_ for _ in ()).throw(RuntimeError("died")))
    with pytest.raises(RuntimeError):
        env.claim(a, 201)
    monkeypatch.setattr(nodes_epics, "_decide", real)
    r = env.claim(a, 201)
    assert r["claimed"] is True and len(env.claims(201)) == 1


# ================================================================ the races

class Sim:
    """A virtual-time scheduler for node threads that share one fake GitHub. Exactly one thread runs at a time; it hands the baton over only where it waits (the
    protocol's sleeps) or is about to send a write (after a random latency), and the server clock jumps to the wake time of the thread that goes next. The order of equal
    wake times is drawn from the seed, so a seed is one reproducible interleaving."""

    def __init__(self, gh, seed, skew=None):
        self.gh, self.rng, self.cv, self.turn, self.wake, self.skew = gh, random.Random(seed), threading.Condition(), None, {}, skew or {}

    def now(self):
        return self.gh.world["now"]

    def pause(self, delay):
        me = threading.current_thread().name
        with self.cv:
            self.wake[me] = self.now() + max(0.0, delay)
            self._next()
            while self.turn != me:
                self.cv.wait()

    def _next(self):
        if not self.wake:
            self.turn = None
            return
        t = min(self.wake.values())
        name = self.rng.choice(sorted(n for n, w in self.wake.items() if w == t))
        del self.wake[name]
        self.gh.world["now"] = max(self.now(), t)
        self.turn = name
        self.cv.notify_all()

    def hook(self, kind, method, path):
        if kind == "write":
            self.pause(self.rng.uniform(0.0, 2.4))                                       # the request is on its way: the other node may get in first

    def run(self, fns: dict):
        results, errors = {}, {}

        def body(name, fn):
            with self.cv:
                while self.turn != name:
                    self.cv.wait()
            try:
                results[name] = fn()
            except BaseException as e:                                                   # noqa: BLE001 the scheduler must always pass the baton on
                errors[name] = e
            with self.cv:
                self._next()
        ts = [threading.Thread(target=body, args=(n, f), name=n) for n, f in fns.items()]
        with self.cv:
            for n in fns:
                self.wake[n] = self.now() + self.rng.uniform(0.0, 3.0)
            self._next()
        [t.start() for t in ts]
        [t.join(60) for t in ts]
        assert not errors, errors
        assert not any(t.is_alive() for t in ts), "a node thread is stuck"
        return results


@pytest.fixture
def race(tmp_path, monkeypatch):
    """An in-memory GitHub with 200 claimable issues, a scheduler, and a factory for node actors whose queue sleeps on the scheduler's clock."""
    gh = FakeGitHub(tmp_path)
    sim = SimpleNamespace(s=None)
    gh.use_memory(monkeypatch, hook=lambda kind, m, p: sim.s.hook(kind, m, p) if sim.s is not None else None)
    monkeypatch.setattr(settings, "epic_writes", True)
    monkeypatch.setattr(settings, "epic_lease_min", 30)
    monkeypatch.setattr(settings, "epic_alias", "")
    monkeypatch.setattr(settings, "epic_authors", ())
    monkeypatch.setattr(nodes_epics, "RENEWER", False)
    monkeypatch.setattr(nodes_epics, "READ_CHILD_MAX_AGE", 10 ** 9)
    monkeypatch.setattr(nodes_epics, "host_names", lambda: {"mybox"})
    mono = lambda: gh.world["now"] - START + 1000.0
    monkeypatch.setattr(nodes_epics, "mono", mono)
    monkeypatch.setattr(github.reader, "mono", mono)
    monkeypatch.setattr(nodes_epics, "sleep", lambda s: sim.s.pause(s))
    gh.add_issue(REPO, 147, "The epic", labels=["ccboard:epic"])
    for n in range(1000, 1200):
        gh.add_issue(REPO, n, f"Issue {n}")
    read_epic(range(1000, 1200))
    dbs = []

    def actor(name):
        db = DB(tmp_path / f"{name}.db")
        dbs.append(db)
        return nodes_epics.Actor(db, f"n_{name}", "alice", name, GW.Queue(mono=mono, sleep=lambda s: sim.s.pause(s)), [REPO])
    yield SimpleNamespace(gh=gh, sim=sim, actor=actor, tmp=tmp_path)
    for db in dbs:
        db.close()


def run_races(r, names, seeds, skew=None, spy=None):
    gh = r.gh
    actors = {n: r.actor(n) for n in names}
    outcomes = []
    for i, seed in enumerate(seeds):
        issue = 1000 + i
        r.sim.s = Sim(gh, seed, skew)
        res = r.sim.s.run({n: (lambda n=n: nodes_epics.claim_issue(REPO, issue, actor=actors[n])) for n in names})
        winners = [n for n, x in res.items() if x["claimed"]]
        claims = gh.claims_on(K[issue])
        assert len(winners) == 1, (seed, res)
        w = winners[0]
        assert [c["id"] for c in claims] == [nodes_epics.parse_ref(res[w]["claim_ref"])[3]], (seed, "the loser's comment is gone and only the winner's stays")
        assert nodes_epics.parse_claim(claims[0]["body"])["alias"] == w
        for n, x in res.items():
            if n != w:
                assert x["reason"] in ("taken", "lost") and x["winner"] == w and x["claim_ref"] is None, (seed, n, x)
                assert not [v for v in nodes_epics.held_claims(actors[n]) if v["issue"] == issue], (seed, "a loser keeps no record")
        assert gh.assignees_of(K[issue]) == ["alice"] and gh.labels_of(K[issue]) == ["ccboard:in-progress"]
        assert sum(1 for m, p, _ in gh.writes() if m == "POST" and p.endswith(f"/issues/{issue}/comments")) <= len(names)
        nodes_epics.drop_claim(res[w]["claim_ref"], actor=actors[w])
        outcomes.append((w, tuple(sorted((n, x["reason"]) for n, x in res.items()))))
    return outcomes


def tally(outcomes):
    from collections import Counter
    return Counter(r for _, rs in outcomes for _, r in rs)


def test_two_nodes_race_for_the_same_issue_200_times_and_exactly_one_wins(race):
    outcomes = run_races(race, ["node-a", "node-b"], range(200))
    assert {w for w, _ in outcomes} == {"node-a", "node-b"}, "both nodes win sometimes: the interleavings differ"
    t = tally(outcomes)
    print("two nodes, 200 races:", dict(t))
    assert t["won"] == 200 and t["lost"] >= 20 and t["taken"] >= 20, ("the races are not all alike: some are decided by the readings (the loser had posted and deleted), some by the first check", t)


def test_three_nodes_race_for_the_same_issue_200_times_and_exactly_one_wins(race):
    outcomes = run_races(race, ["node-a", "node-b", "node-c"], range(1000, 1200))
    assert len({w for w, _ in outcomes}) == 3
    t = tally(outcomes)
    print("three nodes, 200 races:", dict(t))
    assert t["won"] == 200 and t["lost"] >= 40 and t["taken"] >= 40 and t["lost"] + t["taken"] == 400, t


def test_a_node_whose_clock_is_ten_minutes_off_behaves_the_same(race, monkeypatch):
    """The same 60 seeds twice, on fresh issues and from the same fractional second of server time: once with every wall clock right, once with node-a ten minutes behind
    and node-b ten minutes ahead. The winners and the reasons are the same, race by race."""
    gh = race.gh
    names = ["node-a", "node-b"]
    actors = {n: race.actor(n) for n in names}

    def one_pass(first_issue, skew):
        real = time.time
        if skew:
            monkeypatch.setattr(time, "time", lambda: real() + skew.get(threading.current_thread().name, 0.0))
        out = []
        for i in range(60):
            gh.world["now"] = START + 1_000_000 * (first_issue - 1000 + 1)               # the same fractional second every time, and the clock never goes back
            issue = first_issue + i
            gh.world["now"] += 1000 * i
            race.sim.s = Sim(gh, i)
            res = race.sim.s.run({n: (lambda n=n: nodes_epics.claim_issue(REPO, issue, actor=actors[n])) for n in names})
            w = [n for n, x in res.items() if x["claimed"]]
            assert len(w) == 1 and len(gh.claims_on(K[issue])) == 1
            out.append((w[0], tuple(sorted((n, x["reason"]) for n, x in res.items()))))
            nodes_epics.drop_claim(res[w[0]]["claim_ref"], actor=actors[w[0]])
        monkeypatch.setattr(time, "time", real) if skew else None
        return out
    plain = one_pass(1000, None)
    skewed = one_pass(1100, {"node-a": -600.0, "node-b": 600.0})
    assert plain == skewed and {w for w, _ in plain} == {"node-a", "node-b"}


def test_a_race_never_asks_the_local_wall_clock(race, monkeypatch):
    calls = []
    real = time.time

    def clock():
        if sys._getframe(1).f_globals.get("__name__") in ("app.nodes_epics", "app.github", "app.github_write"):
            calls.append(1)
        return real()
    monkeypatch.setattr(time, "time", clock)
    run_races(race, ["node-a", "node-b"], range(5))
    assert calls == [], "a race with no lost claim never reads the local clock"


# ================================================================ more pins: the seam, the privacy of the body, read before edit, one module writes

def test_without_the_fake_gh_even_an_enabled_writer_reaches_nothing(monkeypatch):
    """tests/conftest.py makes the writer's seam refuse (as it does the reader's): with writes on and no fake installed, a write is a refusal, not a gh process."""
    monkeypatch.setattr(settings, "epic_writes", True)
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))
    with pytest.raises(GW.GhWriteError) as e:
        GW.send(GW.comment_create("acme", "shop", 201, BODY), GW.Queue(sleep=lambda s: None))
    assert e.value.reason == "no_gh" and ran == []
    with pytest.raises(github.GhReadError):
        github.api("/repos/acme/shop/issues/5")


def test_only_the_writer_module_builds_a_gh_command_with_a_method_other_than_get():
    import pathlib
    root = pathlib.Path(github.__file__).parent
    users = {p.name for p in root.rglob("*.py") if '"--method"' in p.read_text(errors="replace")}
    assert users == {"github.py", "github_write.py"}, users
    reader_src = open(github.__file__).read()
    assert reader_src.count('"--method", "GET"') == 1 and '"POST"' not in reader_src and '"PATCH"' not in reader_src and '"DELETE"' not in reader_src
    for p in root.rglob("*.py"):
        if p.name not in ("github.py", "github_write.py", "prpoll.py", "gitops.py"):
            txt = p.read_text(errors="replace")
            assert not re.search(r"""["']api["'][^\n]{0,80}["'](?:POST|PATCH|DELETE|PUT)["']""", txt), p.name


def test_the_posted_comment_holds_the_template_fields_and_nothing_that_names_the_machine(env, monkeypatch):
    import hashlib
    gh = env.gh
    monkeypatch.setattr(nodes_epics, "host_names", lambda: {"mybox", "build01"})
    monkeypatch.setattr(settings, "epic_alias", "mybox")                                 # the owner set the alias to the machine's short name: it is not used
    nid_src = "ts:S9f8e7d6c5b"
    a = nodes_epics.Actor(env.actor("x").db, nid_src, "alice", nodes_epics.alias_report(nid_src)["alias"], GW.Queue(mono=env.mono, sleep=env._qsleep), [REPO])
    r = env.claim(a, 201, task_ref="box:42", branch="feat/epic-1")
    assert r["claimed"]
    [c] = env.claims(201)
    body = c["body"]
    derived = nodes_epics.derived_alias(nid_src)
    nid = hashlib.sha256(nid_src.encode()).hexdigest()[:8]
    assert body == (f"<!-- ccboard:claim v1 -->\nccboard claim: alias={derived} nid={nid} lease=30m seq=1 status=claimed attempt=1 task=box:42 branch=feat/epic-1\n\n{GW.CLAIM_TEXT}")
    for bad in ("mybox", "build01", nid_src, "S9f8e7d6c5b", "alice", "://", "@", "~", "/home", "/Users", "/tmp", "\\", "localhost", "127.0.0.1", "ghp_", "github_pat_", "token", "Bearer"):
        assert bad not in body, bad
    assert not re.search(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", body) and not re.search(r"(?:^|[\s=])/", body.replace("<!--", ""))
    assert set(re.findall(r"[a-z]+=", body.splitlines()[1])) == {"alias=", "nid=", "lease=", "seq=", "status=", "attempt=", "task=", "branch="}
    # an Actor handed a host-like alias directly is still not allowed to write it
    b = nodes_epics.Actor(env.actor("y").db, "n_other", "alice", "mybox", GW.Queue(mono=env.mono, sleep=env._qsleep), [REPO])
    assert env.claim(b, 202)["claimed"]
    assert env.parsed(202)[0]["alias"] == nodes_epics.derived_alias("n_other") != "mybox"
    c2 = nodes_epics.Actor(env.actor("z").db, "n_third", "alice", "build.example.com", GW.Queue(mono=env.mono, sleep=env._qsleep), [REPO])
    assert env.claim(c2, 203)["claimed"] and env.parsed(203)[0]["alias"] == nodes_epics.derived_alias("n_third")


def test_every_edit_is_preceded_by_a_fresh_read_of_the_thread(env):
    gh = env.gh
    a = env.actor()
    ref = env.claim(a, 201)["claim_ref"]
    env.tick(300)
    gh.clear_calls()
    assert nodes_epics.renew(ref, actor=a)["renewed"]
    argvs = [c["argv"] for c in gh.calls()]
    i = next(i for i, x in enumerate(argvs) if "PATCH" in x)
    before = [x[-1] for x in argvs[:i]]
    assert any("/comments?" in p for p in before) and before[-1].endswith("/issues/201"), before        # the pages, then the issue read again: the claim was seen to be the board's own just before
    gh.clear_calls()
    assert nodes_epics.release(ref, "done", actor=a)["released"]
    argvs = [c["argv"] for c in gh.calls()]
    first_write = next(i for i, x in enumerate(argvs) if "--method" in x and x[x.index("--method") + 1] != "GET")
    assert any("/comments?" in x[-1] for x in argvs[:first_write]), "the release is fenced by a read too"


def test_release_removes_this_boards_own_login_as_assignee_and_nobody_else(env):
    gh = env.gh
    gh.world["issues"][K[201]]["assignees"] = ["bob"]
    a = env.actor()
    ref = env.claim(a, 201)["claim_ref"]
    assert gh.assignees_of(K[201]) == ["bob", "alice"]
    nodes_epics.release(ref, "done", actor=a)
    assert gh.assignees_of(K[201]) == ["bob"]
    assert [b for m, p, b in gh.writes() if m == "DELETE" and p.endswith("/assignees")] == [{"assignees": ["alice"]}]


def test_a_stopped_timer_touches_neither_github_nor_the_database(env, monkeypatch):
    """The stop flag is checked before any work and again after the edit has been sent: once it is set, the record is not written and nothing more is asked of GitHub."""
    gh = env.gh
    a = env.actor()
    ref = env.claim(a, 201)["claim_ref"]
    env.tick(60)
    stop = threading.Event()
    stop.set()
    n_calls, seen = len(gh.calls()), []
    real_set, real_get = a.db.kv_set, a.db.kv_get
    monkeypatch.setattr(a.db, "kv_set", lambda *x, **k: seen.append(x[0]) or real_set(*x, **k))
    monkeypatch.setattr(a.db, "kv_get", lambda *x, **k: seen.append(x[0]) or real_get(*x, **k))
    out = nodes_epics.renew(ref, actor=a, stop=stop)
    assert out["renewed"] is False and out["reason"] == "stopped" and len(gh.calls()) == n_calls and seen == [], "set before the call: not even a database read"
    seen.clear()
    monkeypatch.setattr(a.db, "kv_get", real_get)
    # stopped while the edit is on its way: the edit was sent, the record is left alone
    stop2 = threading.Event()
    real_send = nodes_epics._send

    def send_then_stop(actor, req):
        res = real_send(actor, req)
        stop2.set()
        return res
    monkeypatch.setattr(nodes_epics, "_send", send_then_stop)
    out = nodes_epics.renew(ref, actor=a, stop=stop2)
    assert out["renewed"] is True and out["reason"] == "stopped" and [m for m, _, _ in gh.writes()].count("PATCH") == 1
    assert seen == [] and nodes_epics.held_claims(a)[0]["seq"] == 1, "the record was not written after the stop"
    monkeypatch.setattr(nodes_epics, "_send", real_send)
    env.tick(60)
    assert nodes_epics.renew(ref, actor=a)["seq"] == 3, "the next start corrects its seq from GitHub's own copy (which has seq 2)"


def test_a_board_that_has_not_read_the_epic_reads_it_on_demand_when_the_claim_names_it(env):
    """A node that runs a relayed claim has not had the epic page open: the claim names the epic, the node reads it itself (children only, no comments), then claims."""
    gh = env.gh
    nodes_epics._last_good.clear()
    gh.link(f"{REPO}#147", K[201])
    gh.link(f"{REPO}#147", K[202])
    a = env.actor()
    r = env.claim(a, 201)
    assert r["reason"] == "not_epic_child" and gh.calls() == [], "without the epic named, nothing is asked of GitHub"
    r = env.claim(a, 201, epic=147)
    assert r["claimed"] is True and r["reason"] == "won"
    paths = [c["argv"][-1] for c in gh.calls()]
    assert any(p.startswith("/repos/acme/shop/issues/147/sub_issues") for p in paths) and not any("/issues/202/comments" in p for p in paths), "the epic's children were listed, not their comments"
    assert nodes_epics.is_read_child("acme", "shop", 202) is True and nodes_epics.is_read_child("acme", "shop", 203) is False
    assert env.claim(a, 203, epic="acme/shop#147")["reason"] == "not_epic_child", "an issue the epic does not list is no child of it, whatever the claim says"
    assert env.claim(a, 202, epic="acme/shop#147")["claimed"] is True


def test_an_epic_named_by_a_claim_must_be_a_labelled_epic_of_this_boards_own_repositories(env):
    gh = env.gh
    nodes_epics._last_good.clear()
    gh.add_issue(REPO, 300, "Not an epic", labels=[])
    gh.link(K[300], K[203])
    gh.add_issue("other/lib", 1, "Their epic", labels=["ccboard:epic"])
    gh.link("other/lib#1", K[204])
    a = env.actor()
    gh.clear_calls()
    assert env.claim(a, 203, epic=300)["reason"] == "not_epic_child", "an issue with sub-issues is not an epic unless it carries the label"
    assert env.claim(a, 204, epic="other/lib#1")["reason"] == "other_repo"
    assert not any("other/lib" in c["argv"][-1] for c in gh.calls()), "nothing was asked about a repository that is not this board's own"
    for bad in (0, -1, True, "1;2", "acme/shop#0", "acme/shop#1x", "a b/c#1", "../x#1", "acme/shop#", [147], {"n": 147}, 10 ** 9, "-x/y#1"):
        assert env.claim(a, 204, epic=bad)["reason"] in ("bad_request", "other_repo"), bad
    assert env.claim(a, 204, epic=999)["reason"] == "not_epic_child", "an epic that does not exist reads as no epic"
    assert gh.writes() == []


def test_read_epic_now_reads_only_this_boards_own_labelled_epics(env):
    gh = env.gh
    nodes_epics._last_good.clear()
    gh.add_issue("other/lib", 1, "Their epic", labels=["ccboard:epic"])
    gh.clear_calls()
    assert nodes_epics.read_epic_now("other", "lib", 1, [REPO]) is False and gh.calls() == []
    assert nodes_epics.read_epic_now("acme", "shop", "01", [REPO]) is False and nodes_epics.read_epic_now("acme", "shop", True, [REPO]) is False and gh.calls() == []
    assert nodes_epics.read_epic_now("acme", "shop", 147, [REPO]) is True and ("acme", "shop", 147) in nodes_epics._last_good
    assert nodes_epics.read_epic_now("acme", "shop", 999, [REPO]) is False


# ================================================================ hardening: six reviewed defects

def test_a_board_that_does_not_know_its_login_settles_nothing_and_loses_nothing(env, monkeypatch):
    """The gh login can be empty for a moment (gh not answering). That is not a lost claim: nothing is written, nothing is marked lost, no hook runs, and a later call with the login behaves as before."""
    gh = env.gh
    monkeypatch.setattr(settings, "epic_authors", ("alice",))                          # who counts is configured, so the page can still be read without a login
    a = env.actor("node-a")
    ref = env.claim(a, 201)["claim_ref"]
    blind = a._replace(login="")
    seen = []
    monkeypatch.setattr(nodes_epics, "LOST_HOOKS", [lambda view, reason: seen.append(reason)])
    n = len(gh.writes())
    f = nodes_epics.fence(ref, actor=blind)
    assert f["ok"] is False and f["lost"] is False and f["reason"] == "login_unknown"
    r = nodes_epics.renew(ref, actor=blind)
    assert r["renewed"] is False and r["lost"] is False and r["reason"] == "login_unknown"
    r = nodes_epics.release(ref, "done", actor=blind)
    assert r["released"] is False and r["reason"] == "login_unknown" and "lost" not in r
    assert len(gh.writes()) == n, "fail closed: nothing was written"
    assert seen == [] and nodes_epics.held_claims(a)[0]["state"] == "held"
    assert not [x for x in nodes.audit_list(20, db=a.db) if x["action"] == "epic-claim-lost"]
    assert nodes_epics.fence_ok(ref, actor=a) is True
    assert nodes_epics.renew(ref, actor=a)["renewed"] is True and nodes_epics.held_claims(a)[0]["state"] == "held"


def test_a_login_that_cannot_push_posts_no_claim_comment_at_all(env):
    gh = env.gh
    a = env.actor()
    gh.world["repos"] = {REPO: {"push": False}}
    r = env.claim(a, 201)
    assert r["claimed"] is False and r["reason"] == "no_access" and "cannot push" in r["message"]
    assert gh.writes() == [] and env.claims(201) == [] and nodes_epics.held_claims(a) == []
    assert a.db.kv_get("claim-pending:acme/shop#201") is None
    assert "/repos/acme/shop" in gh.paths(), "the repository was read first"
    gh.clear_calls()
    gh.world["repos"] = {REPO: {"push": True}}
    r = env.claim(a, 201)
    assert r["claimed"] is True
    methods = [c["argv"][c["argv"].index("--method") + 1] if "--method" in c["argv"] else None for c in gh.calls()]
    paths = [c["argv"][-1] for c in gh.calls()]
    assert paths.index("/repos/acme/shop") < methods.index("POST")


def test_a_repository_whose_permissions_cannot_be_read_is_not_claimed_either(env, monkeypatch):
    gh = env.gh
    a = env.actor()
    gh.world["repos_default"] = False                                                 # GitHub answers 404 for the repository itself
    r = env.claim(a, 201)
    assert r["claimed"] is False and r["reason"] == "error" and r["unknown_reason"] == "access_unknown" and gh.writes() == []
    gh.world["repos_default"] = True
    real = github.reader.get
    monkeypatch.setattr(github.reader, "get", lambda path, ttl=60.0: real(path, ttl)._replace(data={"full_name": REPO}) if path == "/repos/acme/shop" else real(path, ttl))
    r = env.claim(a, 201)
    assert r["claimed"] is False and r["reason"] == "error" and r["unknown_reason"] == "access_unknown" and gh.writes() == [], "no permissions field: fail closed"
    assert nodes_epics.held_claims(a) == [] and env.claims(201) == []


def test_pages_that_were_not_confirmed_by_github_in_this_read_make_the_state_unknown(env):
    gh = env.gh
    a = env.actor()
    ref = env.claim(a, 201)["claim_ref"]
    gh.fail("/comments", 429, headers={"Retry-After": "60"}, method="GET")             # the comments are answered from the cache this time: the issue itself was read fresh
    d = nodes_epics.fresh_claim_state("acme", "shop", 201, [REPO], "alice", detail=True)
    assert d["state"] == "unknown" and d["reason"] == "not_recent" and d["claim"] is None
    env.tick(61)
    gh.fail("/comments", 429, headers={"Retry-After": "60"}, method="GET")
    f = nodes_epics.fence(ref, actor=a)
    assert f["ok"] is False and f["lost"] is False and f["reason"] == "not_recent", "a stale thread settles nothing, and loses nothing"
    assert nodes_epics.held_claims(a)[0]["state"] == "held"
    env.tick(61)
    assert nodes_epics.fence_ok(ref, actor=a) is True


def test_a_write_that_cannot_get_its_turn_in_the_queue_is_refused_as_busy_and_nothing_is_sent(env):
    gh = env.gh
    a = env.actor()
    a.wq.lock_timeout = 0.05
    sent = []
    req = GW.comment_create("acme", "shop", 201, GW.HELP_BODY)
    with a.wq.lock:                                                                    # another write holds the turn (for instance while it waits out a back-off)
        with pytest.raises(GW.GhWriteError) as e:
            a.wq.run(req, tport=lambda r: sent.append(r))
        assert e.value.reason == "busy" and e.value.unknown is False and sent == []
        r = env.claim(a, 201)
    assert r["claimed"] is False and r["reason"] == "busy" and gh.writes() == [] and nodes_epics.held_claims(a) == []
    assert a.db.kv_get("claim-pending:acme/shop#201") is None and a.wq.count == 0
    assert env.claim(a, 201)["claimed"] is True, "the lock was given back: the next claim goes through"


def test_a_reading_of_an_epic_makes_its_children_claimable_only_for_a_labelled_epic_and_only_for_a_while(env, monkeypatch):
    gh = env.gh
    a = env.actor()
    monkeypatch.setattr(nodes_epics, "READ_CHILD_MAX_AGE", 900.0)
    nodes_epics._last_good.clear()
    nodes_epics._last_good[("acme", "shop", 300)] = (nodes_epics.mono(), {"epic": {"labelled": False}, "children": [{"repo": REPO, "number": 202}]})
    assert nodes_epics.is_read_child("acme", "shop", 202) is False
    gh.clear_calls()
    assert env.claim(a, 202)["reason"] == "not_epic_child" and gh.calls() == []
    read_epic([203])
    assert nodes_epics.is_read_child("acme", "shop", 203) is True
    env.tick(899)
    assert nodes_epics.is_read_child("acme", "shop", 203) is True
    env.tick(2)
    assert nodes_epics.is_read_child("acme", "shop", 203) is False
    assert env.claim(a, 203)["reason"] == "not_epic_child", "an old reading is not enough; the page names the epic and it is read again"
    gh.link(f"{REPO}#147", K[203])
    assert env.claim(a, 203, epic=147)["reason"] == "won"


def _rec(n, state, **kw):
    return {"v": 1, "owner": "acme", "repo": "shop", "number": n, "comment_id": 9000 + n, "nid": "a1b2c3d4", "alias": "node-a", "login": "alice", "lease_min": 30, "seq": 1,
            "attempt": 1, "status": "claimed", "state": state, "added_assignee": False, "added_label": False, "renewals": 0, "notes": [], **kw}


def test_trimming_the_claim_index_never_drops_a_held_or_pending_claim(env, monkeypatch):
    monkeypatch.setattr(nodes_epics, "HELD_MAX", 2)                                    # the index keeps 6 keys
    a = env.actor()
    nodes_epics._rec_save(a, _rec(201, "held"))
    nodes_epics._rec_save(a, _rec(202, "claiming"))
    for n in range(301, 312):
        nodes_epics._rec_save(a, _rec(n, "lost", lost_at=1.0))
    keys = nodes_epics._index(a)
    assert len(keys) == 6 and "claim:acme/shop#201" in keys and "claim:acme/shop#202" in keys
    assert keys[2:] == [f"claim:acme/shop#{n}" for n in range(308, 312)], "the oldest lost records went first"
    assert {v["issue"] for v in nodes_epics.held_claims(a) if v["state"] in ("held", "claiming")} == {201, 202}
    assert a.db.kv_get("claim:acme/shop#301") is None, "a dropped record leaves no row behind"
    assert nodes_epics._rec_load(a, "acme", "shop", 201)["state"] == "held"
