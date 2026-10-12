"""Nodes epic P13, issue #148: the surfaces of the claim (the relay row, the doctor row, the epic detail's claim facts, the Settings read-out and the shutdown and startup
wiring). The core (the writer, the queue, the protocol) is tests/test_nodes_claim.py; the page is tests/js/epic.test.mjs.

Two in-process boards (`two_nodes`): board a is the hub, board b the node that claims. GitHub is tests/fake_github.py served from this process (no gh, no network); the
protocol's waits and the queue's spacing run on the fake server clock, so nothing sleeps for real. Pinned: the row is `tasks`, write class, human only, acting, never an
agent (mcp stays closed); the body is repo, issue and epic and nothing else; the hub rebuilds the answer from a whitelist and uses its own words; a timeout is `unconfirmed` and
is never retried; with CCBOARD_EPIC_WRITES off nothing at all reaches gh on the node; both boards leave an audit row with the repository and the issue number only.
"""
from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

import pytest

from app import doctor, github, github_write, main, mcp, nodes, nodes_epics, nodes_relay as nr
from app.config import settings
from tests.fake_github import SECRET, START, FakeGitHub

REPO = "acme/shop"
EPIC = f"{REPO}#147"
ROW = nr.BY_NAME["epic_claim"]
H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
CLAIM_ROUTE = "/api/nodes/{handle}/epics/claim"


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


@pytest.fixture(autouse=True)
def _hub_online(monkeypatch):
    monkeypatch.setattr(main, "_hub", lambda: FakeHub())
    yield
    main._invalidate_scan()


@pytest.fixture
def wide(monkeypatch):
    big = lambda: nodes._Limiter(10 ** 6, 60.0, 64)
    for n in ("node_read_limiter", "node_write_limiter", "relay_read_limiter", "relay_write_limiter"):
        monkeypatch.setattr(nodes, n, big())


@pytest.fixture
def world(two_nodes, pair_up, wide, tmp_path, monkeypatch):
    """Boards a (hub) and b (the node), paired with every scope; GitHub in memory with one epic (#147) and five children (#201 to #205) of acme/shop, which is board b's own
    project; writes ON on this process (the tests that need them off switch them off). `count` counts the hub's calls to b."""
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
    monkeypatch.setattr(settings, "epic_writes", True)
    monkeypatch.setattr(settings, "epic_alias", "")
    monkeypatch.setattr(settings, "epic_authors", ())
    monkeypatch.setattr(settings, "epic_lease_min", 30)
    monkeypatch.setattr(nodes_epics, "RENEWER", False)
    monkeypatch.setattr(nodes_epics, "host_names", lambda: {"mybox"})
    monkeypatch.setattr(main, "_epic_repos", lambda: [REPO])
    gh.add_issue(REPO, 147, "The epic", labels=["ccboard:epic"])
    for n in range(201, 206):
        gh.add_issue(REPO, n, f"Issue {n}")
        gh.link(EPIC, f"{REPO}#{n}")
    reg = pair_up()
    count = Count(two_nodes.transport)
    nodes.peer_transport = count
    nodes_epics.reset()
    yield SimpleNamespace(two=two_nodes, a=two_nodes.a, b=two_nodes.b, reg=reg, handle=reg["handle"], count=count, gh=gh, slept=slept)
    nodes_epics.stop_renewals(2.0)


def hub_path(handle="node-b"):
    return CLAIM_ROUTE.format(handle=handle)


def claim(w, body=None, **kw):
    return w.a.post(hub_path(w.handle), json=body if body is not None else {"repo": REPO, "issue": 205, "epic": 147}, **kw)


def audit(board, **kw):
    return board.db.node_audit_list(500, **kw)


# ================================================================ the row is in the table, and it is closed to agents

def test_the_row_is_a_tasks_write_a_person_only_and_never_an_agents():
    assert (ROW.peer_method, ROW.peer_path, ROW.hub_path, ROW.scope, ROW.rate_class, ROW.audit_action) == ("POST", "/api/node/epics/claim", CLAIM_ROUTE, "tasks", "write", "claim_issue")
    assert ROW.human_only and ROW.acting and not ROW.mcp and ROW.agent_model is None and ROW.agent_shape is None and not ROW.guard
    assert ROW.timeout == nr.CLAIM_TIMEOUT == 45.0 and all(r.timeout == nr.RELAY_TIMEOUT for r in nr.RELAY if r is not ROW), "only the claim waits longer than 10 s"
    assert ROW.body_model.model_config.get("extra") == "forbid" and set(ROW.body_model.model_fields) == {"repo", "issue", "epic"}
    assert ROW.target({}) == "claim" and ROW.body_target({}, {"repo": REPO, "issue": 205}) == "claim acme/shop#205"


def test_no_mcp_tool_and_no_agent_body_reaches_the_row():
    assert "epic_claim" not in set(mcp.NODE_ROW.values()) and not any("claim" in t["name"] or "epic" in t["name"] for t in mcp.TOOLS)
    with pytest.raises(ValueError, match="a person's act"):
        nr.Row(**{**ROW.__dict__, "mcp": True, "agent_model": nr.NoFields, "agent_shape": nr._agent_task})
    with pytest.raises(nr.Invalid):
        nr.validate(ROW, {}, {"repo": REPO, "issue": 205}, agent=True)


def test_the_hook_token_and_a_device_token_get_a_403_and_nothing_is_sent(world):
    w = world
    r = claim(w, owner=False, headers={"X-CCBoard-Token": w.a.hook_token})
    assert r.status_code == 403 and r.json()["reason"] == "human_only" and w.count.seen == 0
    standin = SimpleNamespace(state=SimpleNamespace(user="mcp:laptop", mcp_token={"id": "t1", "name": "laptop", "scopes": ["read", "tasks", "sessions", "permissions"]}), headers={})
    with w.a.enter():
        with pytest.raises(nr.RelayError) as e:
            nr.relay(w.handle, ROW, {}, {"repo": REPO, "issue": 205, "epic": 147}, standin, db=w.a.db, hub=main._hub())
    assert e.value.reason in ("human_only", "mcp_off", "mcp_level", "mcp_scope") and w.count.seen == 0


def test_the_node_side_route_wants_a_named_person_the_tasks_scope_and_a_pair(world):
    w = world
    with w.b.enter():
        _, tok = nodes.add_incoming({"id": "ts:nCALLER000001", "name": "caller", "url": "https://100.64.0.9"}, ["read"], db=w.b.db)
        _, tok_t = nodes.add_incoming({"id": "ts:nCALLER000002", "name": "caller2", "url": "https://100.64.0.8"}, ["tasks"], db=w.b.db)
    bearer = lambda t, **x: {"Authorization": f"Bearer {t}", "X-CCBoard": "1", **x}
    body = {"repo": REPO, "issue": 205, "epic": 147}
    assert w.b.call("POST", ROW.peer_path, owner=False, headers=bearer(tok, **{"X-CCBoard-Acting-User": "alice"}), json=body).status_code == 403, "a read token cannot claim"
    r = w.b.call("POST", ROW.peer_path, owner=False, headers=bearer(tok_t), json=body)
    assert r.status_code == 403 and "acting" in r.json()["error"], "no named person"
    assert w.b.call("POST", ROW.peer_path, headers={}, json=body).status_code == 403, "a signed-in person uses the board's own routes, not the node route"
    assert w.gh.calls() == [], "none of these reached GitHub"


# ================================================================ the body is three plain values

@pytest.mark.parametrize("body", [
    {}, {"repo": REPO}, {"issue": 5}, {"repo": "acme", "issue": 5}, {"repo": "acme/shop/extra", "issue": 5}, {"repo": "../etc/passwd", "issue": 5}, {"repo": "acme/shop#5", "issue": 5},
    {"repo": "acme/.hidden", "issue": 5}, {"repo": "-acme/shop", "issue": 5}, {"repo": "acme/shop\n", "issue": 5}, {"repo": "acme/sh op", "issue": 5}, {"repo": "ａcme/shop", "issue": 5},
    {"repo": "acme/shop;rm -rf", "issue": 5}, {"repo": "a" * 141, "issue": 5}, {"repo": 5, "issue": 5}, {"repo": REPO, "issue": 0}, {"repo": REPO, "issue": -1},
    {"repo": REPO, "issue": 10 ** 9}, {"repo": REPO, "issue": "205"}, {"repo": REPO, "issue": True}, {"repo": REPO, "issue": 2.5}, {"repo": REPO, "issue": None},
    {"repo": REPO, "issue": 5, "epic": "0"}, {"repo": REPO, "issue": 5, "epic": "007"}, {"repo": REPO, "issue": 5, "epic": -3}, {"repo": REPO, "issue": 5, "epic": True},
    {"repo": REPO, "issue": 5, "epic": "acme/shop"}, {"repo": REPO, "issue": 5, "epic": "acme/shop#0"}, {"repo": REPO, "issue": 5, "epic": "x/../y#1"}, {"repo": REPO, "issue": 5, "epic": [1]},
    {"repo": REPO, "issue": 5, "epic": "1 2"}, {"repo": REPO, "issue": 5, "epic": 10 ** 9},
    {"repo": REPO, "issue": 5, "body": "free text"}, {"repo": REPO, "issue": 5, "alias": "x"}, {"repo": REPO, "issue": 5, "login": "alice"}, {"repo": REPO, "issue": 5, "labels": ["bug"]},
    {"repo": REPO, "issue": 5, "comment": "hello"}, {"repo": REPO, "issue": 5, "comment_id": 7}, {"repo": REPO, "issue": 5, "title": "t"}, {"repo": REPO, "issue": 5, "token": "x"},
    {"repo": REPO, "issue": 5, "assignee": "mallory"}, {"repo": REPO, "issue": 5, "state": "closed"}, {"repo": REPO, "issue": 5, "claim_ref": "acme/shop#5:7"},
], ids=lambda b: json.dumps(b)[:60])
def test_a_hostile_body_is_a_422_on_the_hub_with_nothing_sent_and_again_on_the_node(world, body):
    w = world
    r = claim(w, body)
    assert r.status_code == 422 and r.json()["reason"] == "invalid", r.text
    assert w.count.seen == 0, "nothing was sent to the node"
    assert w.gh.calls() == []
    with w.b.enter():
        _, tok = nodes.add_incoming({"id": "ts:nCALLER000009", "name": "caller9", "url": "https://100.64.0.7"}, ["tasks"], db=w.b.db)
    r = w.b.call("POST", ROW.peer_path, owner=False, json=body, headers={"Authorization": f"Bearer {tok}", "X-CCBoard": "1", "X-CCBoard-Acting-User": "alice"})
    assert r.status_code == 422, "the node checks the same rules again"
    assert w.gh.calls() == []


def test_a_good_body_is_read_as_repo_issue_and_an_epic_given_as_a_number_or_a_reference():
    for epic, want in ((None, None), (147, "147"), ("147", "147"), (f"{REPO}#147", f"{REPO}#147")):
        got = nr.validate(ROW, {}, {"repo": REPO, "issue": 205, **({} if epic is None else {"epic": epic})})
        assert got == {"repo": REPO, "issue": 205, **({} if want is None else {"epic": want})}


def test_the_field_kinds_are_listed_and_checked_by_the_one_function():
    assert "repo_slug" in nr.FIELD_KINDS and "epic_ref" in nr.FIELD_KINDS
    assert nr.check_field("repo_slug", REPO) == REPO and nr.check_field("epic_ref", "12") == "12" and nr.check_field("epic_ref", "a/b#12") == "a/b#12"
    for kind, bad in (("repo_slug", "a"), ("repo_slug", "a/b/c"), ("repo_slug", "a/.."), ("repo_slug", "a/"), ("repo_slug", "/b"), ("epic_ref", "a/b"), ("epic_ref", "0"), ("epic_ref", ""),
                      ("epic_ref", "a/b#"), ("epic_ref", "a/b#01")):
        with pytest.raises(ValueError):
            nr.check_field(kind, bad)


# ================================================================ a claim through the hub

def test_a_claim_through_the_hub_is_made_on_the_node_and_answered_from_a_whitelist(world):
    w = world
    r = claim(w)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["claimed"] is True and data["reason"] == "won" and data["repo"] == REPO and data["issue"] == 205 and data["attempt"] == 1 and data["winner"]
    assert set(data) <= {"claimed", "reason", "repo", "issue", "winner", "attempt"}, "no claim_ref, no login, no comment id leaves the node"
    assert w.count.paths == [("POST", "/api/node/epics/claim", nr.CLAIM_TIMEOUT)], "one call, with the claim's own timeout (not 10 s)"
    [c] = w.gh.claims_on(f"{REPO}#205")
    assert c["body"].startswith("<!-- ccboard:claim v1 -->") and "alias=" + data["winner"] in c["body"]
    assert all(s not in c["body"] for s in ("node-a", "node-b", "100.64", "alice@", SECRET)), "the comment holds the alias and the hash, no host name or address"
    assert w.gh.assignees_of(f"{REPO}#205") == ["alice"] and w.gh.labels_of(f"{REPO}#205") == ["ccboard:in-progress"]
    [mine] = nodes_epics.held_claims(nodes_epics.Actor(w.b.db, "", None, ""))
    assert mine["issue"] == 205 and mine["state"] == "held"


def test_both_boards_leave_an_audit_row_with_the_repository_and_the_issue_number_only(world):
    w = world
    assert claim(w).status_code == 200
    [out] = audit(w.a, action="claim_issue")
    assert out["direction"] == "out" and out["status"] == "ok" and out["target"] == "claim acme/shop#205" and out["user"]
    [inn] = audit(w.b, action="claim_issue")
    assert inn["direction"] == "in" and inn["status"] == "ok" and inn["target"] == "claim acme/shop#205"
    core = audit(w.b, action="epic-claim")
    assert core and core[0]["target"] == "acme/shop#205"
    blob = json.dumps(audit(w.a) + audit(w.b), default=str)
    for secret in (SECRET, "ghp_", "ccbnode_", "Authorization", "<!--", "alias="):
        assert secret not in blob


def test_with_github_writes_off_on_the_node_nothing_reaches_gh_and_the_hub_says_why(world, monkeypatch):
    w = world
    monkeypatch.setattr(settings, "epic_writes", False)
    r = claim(w)
    assert r.status_code == 409 and r.json()["reason"] == "claim_refused" and r.json()["why"] == "writes_off", r.text
    assert "CCBOARD_EPIC_WRITES" in r.json()["error"] and "node-b" in r.json()["error"]
    assert w.gh.calls() == [], "not even a read, not even the login probe"
    [out] = audit(w.a, action="claim_issue")
    assert out["status"] == "failed" and "claim_refused" in out["detail"]
    [inn] = audit(w.b, action="claim_issue")
    assert inn["direction"] == "in" and inn["status"] == "failed" and inn["detail"] == "claim_refused: writes_off" and inn["target"] == "claim acme/shop#205"


def test_an_issue_another_node_holds_is_taken_with_the_winners_alias_and_nothing_is_written(world):
    w = world
    k = f"{REPO}#205"
    w.gh.comment(k, "<!-- ccboard:claim v1 -->\nccboard claim: alias=willow-3f nid=deadbeef lease=30m seq=2 status=running attempt=1\n\nx", created_at="2027-01-15T07:30:00Z",
                 updated_at="2027-01-15T07:33:20Z")
    w.gh.set(now=START)          # the server clock: a claim renewed a minute and some ago is live
    w.gh.world["comments"][k][-1].update(created_at=github.epoch_iso(START - 300), updated_at=github.epoch_iso(START - 60))
    w.gh.write_count = len(w.gh.writes())
    r = claim(w)
    assert r.status_code == 409 and r.json()["reason"] == "claim_refused" and r.json()["why"] == "taken" and r.json()["winner"] == "willow-3f", r.text
    assert "willow-3f" in r.json()["error"] and len(w.gh.writes()) == w.gh.write_count, "no write"


@pytest.mark.parametrize("body,why", [
    ({"repo": REPO, "issue": 205}, "not_epic_child"),                          # the node has not read the epic and was not told which
    ({"repo": REPO, "issue": 147, "epic": 147}, "not_epic_child"),             # the epic is not a child of itself
    ({"repo": REPO, "issue": 299, "epic": 147}, "not_epic_child"),             # not listed by the epic
    ({"repo": "acme/other", "issue": 5, "epic": 147}, "other_repo"),           # not one of the node's own projects
    ({"repo": REPO, "issue": 205, "epic": "elsewhere/repo#9"}, "other_repo"),
])
def test_an_issue_the_node_cannot_tie_to_an_epic_it_reads_is_refused_and_nothing_is_written(world, body, why):
    w = world
    r = claim(w, body)
    assert r.status_code == 409 and r.json()["why"] == why, r.text
    assert w.gh.writes() == []


def test_a_closed_issue_is_refused_as_closed(world):
    w = world
    w.gh.add_issue(REPO, 205, "Issue 205", state="closed")
    w.gh.link(EPIC, f"{REPO}#205")
    r = claim(w)
    assert r.status_code == 409 and r.json()["why"] == "closed" and w.gh.writes() == []


def test_a_claim_whose_state_is_not_known_is_not_claimed(world):
    w = world
    w.gh.junk(f"{REPO}#205", 260)                  # a thread too long to read whole
    r = claim(w)
    assert r.status_code == 409 and r.json()["why"] == "claim_unknown" and r.json().get("unknown_reason") and w.gh.writes() == []


# ================================================================ the hub rebuilds what the node says

def peer_says(w, status, body, headers=None):
    w.count = Answer(status, json.dumps(body).encode() if not isinstance(body, bytes) else body, headers or {})
    nodes.peer_transport = w.count
    return claim(w)


def test_every_reason_the_node_may_give_is_mapped_to_this_boards_own_sentence(world):
    w = world
    seen = set()
    for why in nr.CLAIM_PEER_WHY:
        r = peer_says(w, 409, {"error": "PEER SENTENCE", "reason": "claim_refused", "why": why, "winner": "willow-3f", "attempt": 2})
        assert r.status_code == 409 and r.json()["reason"] == "claim_refused" and r.json()["why"] == why, (why, r.text)
        assert "PEER SENTENCE" not in r.json()["error"] and nodes_epics.CLAIM_TEXT[why] in r.json()["error"]
        assert r.json()["winner"] == "willow-3f" and r.json()["attempt"] == 2
        seen.add(why)
    assert seen == set(nodes_epics.CLAIM_TEXT) - set(nr.CLAIM_GOOD), "the closed list is the core's reasons without the two good answers"


def test_a_reason_the_node_invents_is_an_error_and_its_extra_fields_are_dropped(world):
    w = world
    r = peer_says(w, 409, {"reason": "claim_refused", "why": "anything\nelse", "winner": "x" * 80, "claim_ref": "acme/shop#5:77", "login": "alice", "comment_id": 7, "attempt": "9",
                           "wait_s": -1, "unknown_reason": "has space"})
    b = r.json()
    assert b["why"] == "error" and "winner" not in b and "claim_ref" not in b and "login" not in b and "comment_id" not in b and "attempt" not in b and "wait_s" not in b
    assert "unknown_reason" not in b


@pytest.mark.parametrize("winner", ["a b", "x" * 41, "-x", "../x", "x\ny", "<b>", "wil.low"])
def test_a_winner_that_is_not_a_plain_alias_is_not_shown(world, winner):
    r = peer_says(world, 409, {"reason": "claim_refused", "why": "taken", "winner": winner})
    assert "winner" not in r.json() and r.json()["why"] == "taken"


@pytest.mark.parametrize("body", [
    {"claimed": False, "reason": "won", "repo": REPO, "issue": 5}, {"claimed": True, "reason": "taken", "repo": REPO, "issue": 5}, {"claimed": True, "reason": "won", "repo": "../x", "issue": 5},
    {"claimed": True, "reason": "won", "repo": REPO, "issue": "5"}, {"claimed": True, "reason": "won", "repo": REPO}, [], {"claimed": 1, "reason": "won", "repo": REPO, "issue": 5},
])
def test_a_good_answer_that_is_not_a_claim_is_refused_by_the_hub(world, body):
    r = peer_says(world, 200, body)
    assert r.status_code == 502 and r.json()["reason"] in ("bad_answer", "wrong_node"), r.text


def test_a_good_answer_keeps_only_the_whitelist(world):
    r = peer_says(world, 200, {"claimed": True, "reason": "already_held", "repo": REPO, "issue": 205, "winner": "willow-3f", "attempt": 1, "claim_ref": "acme/shop#205:9", "login": "alice",
                               "notes": ["x"], "message": "hello"})
    assert r.status_code == 200 and r.json()["data"] == {"claimed": True, "reason": "already_held", "repo": REPO, "issue": 205, "winner": "willow-3f", "attempt": 1}


def test_a_timeout_is_unconfirmed_names_the_claim_wait_and_is_not_retried(world):
    w = world
    w.count = Answer(raises=TimeoutError("slow"))
    nodes.peer_transport = w.count
    r = claim(w)
    assert r.status_code == 504 and r.json()["reason"] == "unconfirmed" and "45 s" in r.json()["error"] and "not retried" in r.json()["error"], r.text
    assert w.count.seen == 1 and w.count.timeout == nr.CLAIM_TIMEOUT
    [out] = audit(w.a, action="claim_issue")
    assert out["status"] == "failed" and "unconfirmed" in out["detail"]


def test_a_connection_that_breaks_after_the_request_went_out_is_unconfirmed_too(world):
    w = world
    w.count = Answer(raises=ConnectionResetError("reset"))
    nodes.peer_transport = w.count
    r = claim(w)
    assert r.status_code == 502 and r.json()["reason"] == "unconfirmed" and w.count.seen == 1


def test_the_other_refusals_of_the_hub_are_the_relays_usual_words(two_nodes, pair_up, wide):
    reg = pair_up(("read",))
    nodes.peer_transport = Count(two_nodes.transport)
    r = two_nodes.a.post(hub_path(reg["handle"]), json={"repo": REPO, "issue": 205, "epic": 147})
    assert r.status_code == 409 and r.json()["reason"] == "scope" and "tasks" in r.json()["error"] and nodes.peer_transport.seen == 0
    assert two_nodes.a.post(hub_path("local"), json={"repo": REPO, "issue": 5}).status_code == 400
    assert two_nodes.a.post(hub_path("nobody"), json={"repo": REPO, "issue": 5}).status_code == 404
    assert two_nodes.a.post(hub_path(reg["handle"]), json={"repo": REPO, "issue": 5, "epic": 1}, owner=False).status_code in (401, 403)


# ================================================================ the node end: claim_issue is the only thing it calls

def test_the_peer_handler_calls_the_claim_and_is_the_rows_handler():
    src = inspect.getsource(nr.peer_epic_claim)
    assert "nodes_epics.claim_issue(" in src and "_claim_facts(" in src
    assert nr.HANDLERS["epic_claim"] is nr.peer_epic_claim


def test_the_claim_comment_is_built_from_this_boards_own_fields_whatever_the_request_says(world):
    w = world
    r = claim(w, {"repo": REPO, "issue": 204, "epic": 147})
    assert r.status_code == 200
    [c] = w.gh.claims_on(f"{REPO}#204")
    import re
    assert re.fullmatch(r"<!-- ccboard:claim v1 -->\nccboard claim: alias=[a-z]+-[0-9a-f]{2} nid=[0-9a-f]{8} lease=30m seq=1 status=claimed attempt=1\n\n[^\n]+", c["body"]), c["body"]


# ================================================================ the epic detail carries the claim facts the page needs

def test_node_list_says_which_nodes_hold_the_tasks_scope():
    out = nodes_epics.node_list([{"handle": "box", "name": "node-b", "status": "online", "age_s": 1, "scopes": ["read", "tasks"]},
                                 {"handle": "dev", "name": "node-c", "status": "online", "age_s": 1, "scopes": ["read"]},
                                 {"handle": "old", "name": "node-d", "status": "online", "legacy": True, "scopes": ["tasks"]}])
    by = {n["handle"]: n for n in out}
    assert by["box"]["tasks"] is True and by["dev"]["tasks"] is False and "old" not in by and "tasks" not in by["local"]


def test_the_epic_detail_says_whether_writes_are_on_and_lists_this_boards_own_claims(world, monkeypatch):
    w = world
    assert claim(w).status_code == 200                              # board b now holds 205
    with w.b.enter():
        d = nodes_epics.detail(w.b.db, "acme", "shop", 147, [], repos=[REPO], login="alice")
        assert d["writes"] is True
        [mine] = d["own_claims"]
        assert mine["repo"] == REPO and mine["issue"] == 205 and mine["state"] == "held" and mine["status"] == "claimed"
        assert not ({"claim_ref", "login", "nid", "node_id", "comment_id"} & set(mine)), "the fencing token and the login stay on the board"
    monkeypatch.setattr(settings, "epic_writes", False)
    with w.b.enter():
        d = nodes_epics.detail(w.b.db, "acme", "shop", 147, [], repos=[REPO], login="alice")
        assert d["writes"] is False and len(d["own_claims"]) == 1, "a held record is still shown with writes off"
    with w.a.enter():
        assert nodes_epics.detail(w.a.db, "acme", "shop", 147, [], repos=[REPO], login="alice")["own_claims"] == [], "a board that never claimed shows none"


def test_own_claims_asks_github_nothing_and_filters_to_the_epics_issues(world):
    w = world
    assert claim(w, {"repo": REPO, "issue": 204, "epic": 147}).status_code == 200
    w.gh.clear_calls()
    assert nodes_epics.own_claims(w.b.db, [(REPO, 204)])[0]["issue"] == 204
    assert nodes_epics.own_claims(w.b.db, [(REPO, 203), ("acme/other", 204)]) == [] and nodes_epics.own_claims(w.b.db, []) == []
    assert w.gh.calls() == []


def test_the_settings_readout_of_the_two_switches(world, monkeypatch):
    v = nodes_epics.switches_view()
    assert v["writes"] is True and v["writes_env"] == "CCBOARD_EPIC_WRITES" and v["alias_env"] == "CCBOARD_EPIC_ALIAS" and v["lease_env"] == "CCBOARD_EPIC_LEASE_MIN"
    assert v["alias_source"] == "derived" and v["alias_note"] is None and v["lease_min"] == 30
    monkeypatch.setattr(settings, "epic_alias", "mybox")
    v = nodes_epics.switches_view()
    assert v["alias_note"] == "host name" and v["alias"] != "mybox" and v["alias_source"] == "derived"
    monkeypatch.setattr(settings, "epic_alias", "has space")
    assert nodes_epics.switches_view()["alias_note"] == "not valid"
    monkeypatch.setattr(settings, "epic_alias", "river-9")
    v = nodes_epics.switches_view()
    assert v["alias"] == "river-9" and v["alias_source"] == "setting"
    monkeypatch.setattr(settings, "epic_writes", False)
    assert nodes_epics.switches_view()["writes"] is False
    assert set(v) == {"writes", "writes_env", "alias", "alias_source", "alias_env", "alias_note", "lease_min", "lease_env"}, "no node id, no hash, no host name"


def test_the_mcp_tokens_answer_carries_the_switches_read_only(two_nodes, monkeypatch):
    monkeypatch.setattr(settings, "epic_writes", True)
    r = two_nodes.a.get("/api/mcp/tokens")
    assert r.status_code == 200 and r.json()["epics"]["writes"] is True and r.json()["epics"]["writes_env"] == "CCBOARD_EPIC_WRITES"
    monkeypatch.setattr(settings, "epic_writes", False)
    assert two_nodes.a.get("/api/mcp/tokens").json()["epics"]["writes"] is False


# ================================================================ startup and shutdown

def test_the_lifespan_recovers_claims_only_with_writes_on_and_a_record_and_stops_the_timer_before_the_database():
    src = inspect.getsource(main.lifespan)
    assert "github_write.writes_enabled() and nodes_epics.has_records(db)" in src and "_recover_claims" in src and "nodes_epics.recover()" in inspect.getsource(main._recover_claims)
    assert src.index("nodes_epics.stop_renewals") < src.index("hub.stop"), "the renew timer stops before the hub and the database do"


def test_a_board_that_never_claimed_has_no_records_and_recover_does_nothing(world, monkeypatch):
    w = world
    assert nodes_epics.has_records(w.a.db) is False
    monkeypatch.setattr(settings, "epic_writes", False)
    assert nodes_epics.recover(nodes_epics.Actor(w.a.db, "n_a", "alice", "x", None, [REPO])) == []
    assert not nodes_epics.renewer_running() and w.gh.calls() == []


def test_a_board_that_claimed_has_records_and_the_timer_runs_only_while_it_holds_one(world, monkeypatch):
    w = world
    monkeypatch.setattr(nodes_epics, "RENEWER", True)
    assert claim(w).status_code == 200
    assert nodes_epics.has_records(w.b.db) is True and nodes_epics.renewer_running() is True
    nodes_epics.stop_renewals(5.0)
    assert nodes_epics.renewer_running() is False


# ================================================================ the doctor row

def probe_reset():
    doctor._probes.pop("epic-claim", None)
    return doctor


@pytest.fixture
def gh_script(tmp_path, monkeypatch):
    return FakeGitHub(tmp_path).install(monkeypatch)


def with_repos(monkeypatch, repos=(REPO,)):
    monkeypatch.setattr(nodes_epics, "known_repos", lambda p: list(repos))


def test_the_doctor_row_is_registered_in_the_box_group():
    by = {c[0]: c for c in doctor.CHECKS}
    assert by["epic-claim"][1] == "box" and by["epic-claim"][2] == "Claiming epic issues"


def test_with_writes_off_the_row_says_so_in_plain_words_and_gh_is_never_run(gh_script, monkeypatch):
    probe_reset()
    monkeypatch.setattr(settings, "epic_writes", False)
    monkeypatch.setattr(settings, "epic_alias", "")
    monkeypatch.setattr(nodes_epics, "host_names", lambda: {"mybox"})
    out = doctor._c_epic_claim(None)
    assert out.status == "skip" and "does not write to GitHub" in out.detail and "CCBOARD_EPIC_WRITES is off" in out.detail and "alias" in out.detail and "lease 30 min" in out.detail
    assert gh_script.calls() == [] and "epic-claim" not in doctor._probes, "no probe thread, no gh"


def test_a_host_like_alias_is_a_warning_even_with_writes_off(gh_script, monkeypatch):
    probe_reset()
    monkeypatch.setattr(settings, "epic_writes", False)
    monkeypatch.setattr(settings, "epic_alias", "mybox.example.net")
    out = doctor._c_epic_claim(None)
    assert out.status == "skip" and "looks like a host name" in out.detail and "public" in out.detail and "mybox.example.net" not in out.detail.split("instead")[0].replace("looks like", "")


def test_with_writes_on_the_row_reports_login_scopes_push_access_alias_and_lease(gh_script, monkeypatch):
    probe_reset()
    with_repos(monkeypatch)
    monkeypatch.setattr(settings, "epic_writes", True)
    monkeypatch.setattr(settings, "epic_alias", "river-9")
    out = doctor._c_epic_claim(None)
    assert out.status == "pass", out
    for part in ("CCBOARD_EPIC_WRITES=1", "scopes: read:org, repo", "push access to 1 of 1 repositories", "alias river-9 (from CCBOARD_EPIC_ALIAS)", "lease 30 min", "comments on public repositories are public"):
        assert part in out.detail, part
    assert SECRET not in out.detail and "ghp_" not in out.detail
    paths = gh_script.paths()
    assert paths == ["/rate_limit", "/repos/acme/shop"], paths
    assert all(c["argv"][0] in ("--version", "api") and "--method" not in c["argv"] or c["argv"][c["argv"].index("--method") + 1] == "GET" for c in gh_script.calls()), "reads only"


def test_without_push_access_the_row_warns_and_says_the_claim_still_works(gh_script, monkeypatch):
    probe_reset()
    with_repos(monkeypatch)
    monkeypatch.setattr(settings, "epic_writes", True)
    gh_script.set(repos={REPO: {"push": False, "private": True}})
    out = doctor._claim_outcome(doctor._claim_probe())
    assert out.status == "warn" and "cannot push to acme/shop" in out.detail and "assignee and the label will be refused" in out.detail and "claim comment itself still works" in out.detail


def test_the_row_warns_for_a_missing_repo_scope_an_unreadable_repository_and_a_wait(gh_script, monkeypatch):
    probe_reset()
    with_repos(monkeypatch, (REPO, "acme/api"))
    monkeypatch.setattr(settings, "epic_writes", True)
    gh_script.set(scopes="read:org")
    out = doctor._claim_outcome(doctor._claim_probe())
    assert out.status == "warn" and "no repo scope" in out.detail and out.fix["cmd"] == "gh auth refresh -s repo"
    gh_script.set(scopes="repo", forbidden=["acme/api"])
    out = doctor._claim_outcome(doctor._claim_probe())
    assert out.status == "warn" and "could not read acme/api" in out.detail and out.fix["cmd"] == "gh auth login"
    gh_script.set(scopes=None, forbidden=[])
    out = doctor._claim_outcome(doctor._claim_probe())
    assert out.status == "pass" and "scopes not reported" in out.detail
    nodes_epics.reset()
    gh_script.fail("/rate_limit", 429, headers={"Retry-After": "40"})
    out = doctor._claim_outcome(doctor._claim_probe())
    assert out.status == "warn" and "wait 40 s" in out.detail


def test_the_row_warns_for_a_bad_lease_and_an_invalid_alias_with_writes_on(gh_script, monkeypatch):
    probe_reset()
    with_repos(monkeypatch)
    monkeypatch.setattr(settings, "epic_writes", True)
    monkeypatch.setattr(settings, "epic_lease_bad", True)
    monkeypatch.setattr(settings, "epic_alias", "not valid!")
    out = doctor._claim_outcome(doctor._claim_probe())
    assert out.status == "warn" and "CCBOARD_EPIC_LEASE_MIN is not a whole number from 10 to 120" in out.detail and "CCBOARD_EPIC_ALIAS is not letters, digits" in out.detail


def test_the_row_warns_when_gh_has_no_login_or_is_missing(gh_script, monkeypatch):
    probe_reset()
    with_repos(monkeypatch)
    monkeypatch.setattr(settings, "epic_writes", True)
    gh_script.set(login=None)
    out = doctor._claim_outcome(doctor._claim_probe())
    assert out.status == "warn" and "no claim can be made" in out.detail and out.fix["cmd"] == "gh auth login"
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    out = doctor._claim_outcome(doctor._claim_probe())
    assert out.status == "warn" and "gh is not installed" in out.detail


def test_the_reader_has_one_more_get_shape_the_repository_and_nothing_else_new():
    assert github.repo_path("acme", "shop") == "/repos/acme/shop" and github.allowed_path("/repos/acme/shop")
    for bad in ("/repos/acme", "/repos/acme/shop/", "/repos/acme/shop/issues/1/labels", "/repos/acme/../x", "/repos/-x/shop", "/repos/acme/shop?x=1", "/repos/acme/shop/hooks", "/user"):
        assert not github.allowed_path(bad), bad
    for o, r in (("a b", "x"), ("acme", "../x"), ("acme", ".x"), ("", "x")):
        with pytest.raises(github.GhReadError):
            github.repo_path(o, r)
