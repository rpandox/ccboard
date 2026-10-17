"""Nodes epic P15, issue #150 (auto routing of epic issues, the run loop, Run / Pause / Resume, Run on a node, the Auto preview) and the routes behind issue #152's epic tools.

Three layers, none of which reaches the real gh, claude, codex, tailscale, tmux or the network:
  1. `pick` (app/nodes_route.py) is a pure function: one case per hard filter and per ranking step, the pins, the shared subscription, the reservations, the same answer for any input order.
  2. The run loop (app/nodes_run.py) on a board whose GitHub is tests/fake_github.py served from this process and whose hub is a list of readings the test controls; the relay is a fake that
     records the calls and writes the claim comment a node would. The clock is a fake one that `sleep` advances, so the one-second spacing is measured and nothing waits.
  3. One end-to-end run on the two in-process boards of tests/test_epics_work.py: the hub board routes issues, the node board claims them and starts real (fake-tmux) tasks.

Pinned: the filters in the issue's order each with a reason; unknown is not zero (a window percentage that is not known counts as 50 and says so, an unknown lane count or status is never free);
a stale, offline or unauthorized node is never chosen; a pin to a node that fails a filter waits and is never rerouted; nodes on one subscription are counted once; two ready issues do not
both take one lane; never more calls than free lanes; one call at a time and a second apart; Pause is read before every call; a depends-on chain holds a child; a window that reset releases a
waiting issue; with writes off or no node paired Run stores nothing and no thread starts; a crash in a cycle does not end the loop; Run and the routing settings are a signed-in person's.
"""
from __future__ import annotations

import itertools
import json
import random
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import github, nodes, nodes_epics, nodes_relay as nr, nodes_route, nodes_run
from app.config import settings
from tests.fake_github import FakeGitHub, claim_body
from tests.test_nodes_epics import set_repos
from tests.test_epics_work import w, _no_reservation_left, _wide  # noqa: F401  (fixtures: the two in-process boards of the work phase)

REAL_ENSURE = nodes_run.Runner.ensure

REPO = "acme/shop"
EPIC = f"{REPO}#147"
ID = {"Tailscale-User-Login": "alice@example.com"}
H = {**ID, "X-CCBoard": "1"}
T0 = 1_800_000_000.0
ISSUE = {"repo": REPO, "number": 201, "agent": "claude"}


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def node(handle: str = "box", *, name: str | None = None, status: str = "online", scopes=("read", "tasks"), slugs=(REPO,), state: bool = True, card: bool = True, pct=31.0, resets_at=None,
         backoff=None, free: int = 2, cap: int = 3, lanes: bool = True, claude=(True, True, False), codex=(True, True, False), problems=(), load1=1.0, cores=4, sub=None, sub_codex=None,
         tasks=(), ok: float = T0, codex_pct=20.0) -> dict:
    """One reading of a paired node as NodeHub.records() holds it: a record with the card and the state a good node reports. Every argument changes one fact."""
    usage = {"claude": {"pct": pct, "resets_at": resets_at, "backoff_until": backoff, "known": pct is not None, "limited": False},
             "codex": {"pct": codex_pct, "resets_at": None, "backoff_until": None, "known": codex_pct is not None, "limited": False}}
    st = {"projects": [{"name": "shop", "repos": [{"name": "api", "slug": s, "branch": "main", "dirty": False} for s in slugs]}], "sessions": [], "tasks": list(tasks), "usage": usage,
          "lanes": {"cap": cap, "running": (cap - free) if cap else 1, "free": free if cap else None} if lanes else {"cap": None, "running": None, "free": None},
          "login_problems": list(problems), "needs_you": {}, "truncated": False}
    items = [{"agent": "claude", "label": None, "current": True, "subscription_key": sub, "window": {}, "limited": False}] if sub else []
    items += [{"agent": "codex", "label": None, "current": True, "subscription_key": sub_codex, "window": {}, "limited": False}] if sub_codex else []
    cd = {"agents": [{"id": "claude", "installed": claude[0], "logged_in": claude[1], "login_problem": claude[2], "version": None, "hooks": True},
                     {"id": "codex", "installed": codex[0], "logged_in": codex[1], "login_problem": codex[2], "version": None, "hooks": True}],
          "accounts": {"supported": True, "items": items}, "load": {"load1": load1, "cores": cores}}
    return {"peer_id": "p_" + handle, "handle": handle, "node_id": "n_" + handle.ljust(16, "0")[:16], "name": name or handle, "url": "https://100.64.0.9", "status": status,
            "polled_at": iso(ok), "last_ok_at": iso(ok), "age_s": 1, "skew_ms": 0, "skew_warn": False, "error_kind": None, "card": cd if card else None, "state": st if state else None,
            "etag": None, "legacy": False, "scopes": list(scopes)}


def settings_of(**kw) -> dict:
    return {"now": T0, "max_window_pct": 85, **kw}


def pick(nodes_, issue=None, pins=None, res=None, **kw) -> dict:
    return nodes_route.pick(issue or ISSUE, nodes_, pins or {}, res or [], settings_of(**kw))


# ================================================================ 1. pick: the hard filters, in the issue's order, each with its reason

FILTERS = [
    ("offline", dict(status="offline"), "is offline"),
    ("stale", dict(status="stale"), "stale"),
    ("unauthorized", dict(status="unauthorized"), "pair it again"),
    ("unpaired", dict(status="unpaired"), "not paired any more"),
    ("scope", dict(scopes=("read",)), "no tasks scope"),
    ("repo", dict(slugs=("acme/api",)), "does not have the repository cloned"),
    ("repo_unknown", dict(state=False), "has not reported its projects"),
    ("agent_unknown", dict(card=False), "has not reported its agents"),
    ("agent_missing", dict(claude=(False, False, False)), "does not have claude installed"),
    ("agent_login", dict(claude=(True, False, False)), "logged out"),
    ("agent_login", dict(claude=(True, True, True)), "logged out or in need of a new login"),
    ("agent_login", dict(problems=("claude",)), "logged out or in need of a new login"),
    ("backoff", dict(backoff=iso(T0 + 900)), "back-off for claude until"),
    ("window", dict(pct=90.0), "5-hour window is at 90 percent"),
    ("lane", dict(free=0), "no free lane"),
    ("lane_unknown", dict(lanes=False), "has not reported its task lanes"),
]


@pytest.mark.parametrize("code,kw,words", FILTERS, ids=[f"{c}-{i}" for i, (c, _, _) in enumerate(FILTERS)])
def test_each_hard_filter_keeps_a_node_out_and_says_why(code, kw, words):
    r = pick([node("bad", **kw), node("good")])
    assert r["handle"] == "good" and "chose good" in r["why"], "the node that passes is chosen"
    [refusal] = r["refused"]
    assert refusal["handle"] == "bad" and refusal["code"] == code and words in refusal["text"], refusal
    alone = pick([node("bad", **kw)])
    assert alone["handle"] is None and alone["waiting"]["code"] == code and words in alone["waiting"]["reason"] and alone["why"].startswith("waiting: ")


def test_a_node_that_fails_several_filters_is_reported_for_the_first_one_in_the_issues_order():
    worst = dict(status="offline", scopes=("read",), slugs=(), claude=(False, False, False), backoff=iso(T0 + 9), pct=99.0, free=0)
    order = [("offline", {}), ("scope", dict(status="online")), ("repo", dict(status="online", scopes=("read", "tasks"))),
             ("agent_missing", dict(status="online", scopes=("read", "tasks"), slugs=(REPO,))),
             ("backoff", dict(status="online", scopes=("read", "tasks"), slugs=(REPO,), claude=(True, True, False))),
             ("window", dict(status="online", scopes=("read", "tasks"), slugs=(REPO,), claude=(True, True, False), backoff=None)),
             ("lane", dict(status="online", scopes=("read", "tasks"), slugs=(REPO,), claude=(True, True, False), backoff=None, pct=10.0))]
    for code, fixes in order:
        assert pick([node("n", **{**worst, **fixes})])["waiting"]["code"] == code, code


def test_the_pairs_scopes_may_be_a_list_or_the_json_text_a_registry_row_keeps():
    as_text = {**node("n"), "scopes": json.dumps(["read", "tasks"])}
    assert pick([as_text])["handle"] == "n"
    assert pick([{**node("n"), "scopes": json.dumps(["read"])}])["waiting"]["code"] == "scope"
    assert pick([{**node("n"), "scopes": None}])["waiting"]["code"] == "scope" and pick([{**node("n"), "scopes": "not json"}])["waiting"]["code"] == "scope"


def test_a_cap_of_zero_means_no_cap_and_not_no_lane():
    r = pick([node("big", cap=0)])
    assert r["handle"] == "big" and "no lane cap" in r["why"]
    unknown = pick([node("n", lanes=False)])
    assert unknown["handle"] is None and unknown["waiting"]["code"] == "lane_unknown", "an unread lane count is never free"


def test_an_unknown_window_counts_as_50_and_says_so_and_is_not_zero():
    r = pick([node("n", pct=None)])
    assert r["handle"] == "n" and "window unknown" in r["why"] and r["ranked"][0]["headroom"] == 50.0
    assert pick([node("known", pct=60.0), node("unknown", pct=None)])["handle"] == "unknown", "unknown is 50 used: better than 60, so it is not treated as full"
    assert pick([node("known", pct=40.0), node("unknown", pct=None)])["handle"] == "known", "and worse than 40, so it is not treated as empty either"
    over = pick([node("n", pct=None)], max_window_pct=40)
    assert over["handle"] is None and over["waiting"]["code"] == "window" and "no 5-hour window reading" in over["waiting"]["reason"] and "50 percent" in over["waiting"]["reason"]
    assert pick([node("n", state=True)], issue={**ISSUE, "agent": "codex"})["why"].endswith("weekly window 20 percent"), "Codex has a weekly window"


def test_a_window_whose_reset_time_has_passed_is_not_a_reading_any_more():
    stuck = node("n", pct=90.0, resets_at=int(T0 - 60))
    r = pick([stuck])
    assert r["handle"] == "n" and "window unknown" in r["why"], "the 90 is of the window before; the node has not reported the new one yet"
    waiting = pick([node("n", pct=90.0, resets_at=int(T0 + 600))])
    assert waiting["waiting"]["until"] == T0 + 600 and waiting["waiting"]["until_iso"] == iso(T0 + 600) and "until" in waiting["waiting"]["text"]


def test_the_epics_limit_decides_where_the_window_stops():
    n = node("n", pct=70.0)
    assert pick([n])["handle"] == "n" and pick([n], max_window_pct=70)["waiting"]["code"] == "window", "at the limit is over it"


def test_a_backoff_that_has_ended_or_cannot_be_read_does_not_open_the_node_by_accident():
    assert pick([node("n", backoff=iso(T0 - 5))])["handle"] == "n"
    r = pick([node("n", backoff="soon")])
    assert r["handle"] is None and r["waiting"]["code"] == "backoff", "a back-off that cannot be read is a back-off"


# ================================================================ pick: the ranking, one step at a time

def ranked(nodes_, **kw) -> list[str]:
    return [x["handle"] for x in pick(nodes_, **kw)["ranked"]]


def test_the_ranking_steps_in_order_headroom_lanes_load_least_recently_assigned_name():
    assert ranked([node("a", pct=40.0), node("b", pct=20.0)]) == ["b", "a"], "more headroom first"
    assert ranked([node("a", free=1), node("b", free=2)]) == ["b", "a"], "then more free lanes"
    assert ranked([node("a", load1=3.0), node("b", load1=1.0)]) == ["b", "a"], "then lower load per core"
    assert ranked([node("a", load1=2.0, cores=2), node("b", load1=3.0, cores=8)]) == ["b", "a"], "per core, not the raw load"
    assert ranked([node("a", load1=None), node("b", load1=9.0)]) == ["b", "a"], "an unknown load ranks last, it is not zero"
    assert ranked([node("a"), node("b")], assigned={"a": T0 - 10, "b": T0 - 100}) == ["b", "a"], "then the node least recently assigned"
    assert ranked([node("a"), node("b")], assigned={"a": T0 - 10}) == ["b", "a"], "a node never assigned goes before one that was"
    assert ranked([node("n2", name="Zed"), node("n1", name="amy")]) == ["n1", "n2"], "then name"
    assert ranked([node("b", name="same"), node("a", name="same")]) == ["a", "b"], "then handle"
    a_wins = [node("a", pct=20.0, free=1, load1=9.0), node("b", pct=30.0, free=3, load1=0.1)]
    assert ranked(a_wins) == ["a", "b"], "a step only decides when the one before it is a tie"


def test_the_answer_is_the_same_for_any_order_of_the_input():
    pool = [node("a", pct=40.0, free=1), node("b", pct=40.0, free=2, sub="s" * 12), node("c", pct=40.0, free=2, sub="s" * 12, load1=2.0), node("d", pct=10.0, free=0),
            node("e", status="stale"), node("f", pct=40.0, free=1, load1=0.1)]
    base = pick(pool, assigned={"f": T0 - 5})
    seen = {json.dumps(base, sort_keys=True)}
    rng = random.Random(7)
    for _ in range(60):
        shuffled = pool[:]
        rng.shuffle(shuffled)
        seen.add(json.dumps(pick(shuffled, assigned={"f": T0 - 5}), sort_keys=True))
    assert len(seen) == 1
    assert len({json.dumps(pick(list(p), group=[["a", "f"]]), sort_keys=True) for p in itertools.islice(itertools.permutations(pool[:4]), 24)}) == 1


# ================================================================ pick: nodes on one subscription are counted once

def test_nodes_that_share_a_subscription_key_share_one_window_number_and_add_their_lanes_for_the_tie_break_only():
    one, two = "k" * 12, "q" * 12
    a, b = node("a", free=1, sub=one), node("b", free=1, sub=one)
    c = node("c", free=2, sub=two)
    r = pick([a, b, c])
    assert [x["handle"] for x in r["ranked"]] == ["a", "b", "c"], "a and b together have 2 lanes, as many as c: the tie goes on to name"
    assert r["ranked"][0]["shared_with"] == ["b"] and "shares a subscription with b" in r["why"]
    alone = pick([node("a", free=1), node("b", free=1), node("c", free=2)])
    assert [x["handle"] for x in alone["ranked"]] == ["c", "a", "b"], "without the key each is on its own: c has the most lanes"


def test_the_shared_window_is_the_highest_any_online_member_reports_so_one_stale_reading_cannot_open_it():
    one = "k" * 12
    r = pick([node("a", pct=60.0, sub=one), node("b", pct=90.0, sub=one)])
    assert r["handle"] is None and {x["handle"] for x in r["refused"]} == {"a", "b"} and all(x["code"] == "window" for x in r["refused"])
    assert "90 percent" in r["waiting"]["reason"], "a at 60 is held back by b's reading of the same window"
    other_agent = pick([node("a", pct=60.0, sub=one), node("b", pct=90.0, sub=one)], issue={**ISSUE, "agent": "codex"})
    assert other_agent["handle"] == "a", "the subscription is the agent's: the Codex window is a different one (and a has no Codex key, so it is on its own)"
    offline_member = pick([node("a", pct=60.0, sub=one), node("b", pct=90.0, sub=one, status="offline")])
    assert offline_member["handle"] == "a", "a node that is not online contributes no reading"


def test_a_group_set_by_hand_works_like_a_key():
    nodes_ = [node("a", pct=60.0), node("b", pct=90.0), node("c", pct=10.0)]
    r = pick(nodes_, group=[["a", "b"]])
    assert r["handle"] == "c" and {x["handle"] for x in r["refused"]} == {"a", "b"}
    assert pick(nodes_)["handle"] == "c", "without the group a is fine at 60, c still wins on headroom"
    assert [x["handle"] for x in pick(nodes_)["ranked"]][:2] == ["c", "a"]


# ================================================================ pick: pins

def test_a_pin_on_a_fine_node_overrides_the_ranking():
    r = pick([node("a", pct=10.0), node("b", pct=50.0)], pins={"ui": "b"})
    assert r["handle"] == "b" and "pinned by the issue's pin" in r["why"] and r["pin"] == {"handle": "b", "source": "ui"}


def test_a_pin_on_a_node_that_fails_a_filter_waits_with_the_reason_and_is_never_rerouted():
    r = pick([node("a"), node("b", pct=95.0, resets_at=int(T0 + 1200))], pins={"ui": "b"})
    assert r["handle"] is None, "a is fine and is not used"
    w_ = r["waiting"]
    assert w_["code"] == "window" and w_["pinned"] == "b" and w_["until"] == T0 + 1200 and "pinned by the issue's pin" in w_["reason"] and "95 percent" in w_["reason"]
    off = pick([node("a"), node("b", status="offline")], pins={"epic": "b"})
    assert off["handle"] is None and off["waiting"]["code"] == "offline" and off["waiting"]["until"] is None


def test_the_pins_go_in_order_the_page_then_the_label_then_the_epic_and_a_conflict_is_won_by_the_page():
    ns = [node("a"), node("b"), node("c")]
    assert pick(ns, pins={"ui": "a", "label": "b", "epic": "c"})["handle"] == "a"
    assert pick(ns, pins={"label": "b", "epic": "c"})["handle"] == "b" and pick(ns, pins={"epic": "c"})["handle"] == "c"
    gone = pick(ns, pins={"ui": "a", "label": "b"}) ["pin"]
    assert gone == {"handle": "a", "source": "ui"}


def test_a_pin_to_a_node_that_is_not_paired_or_to_this_board_waits_and_says_so():
    for pin, words in (("ghost", "not paired"), (nodes_route.UNKNOWN_PIN, "not paired"), ("local", "this board")):
        r = pick([node("a")], pins={"ui": pin})
        assert r["handle"] is None and r["waiting"]["code"] == "pin_unpaired" and words in r["waiting"]["reason"], pin


def test_with_no_paired_node_pick_waits_and_names_it():
    r = pick([])
    assert r["handle"] is None and r["waiting"]["code"] == "no_nodes" and "no node is paired" in r["why"]


# ================================================================ reservations

def test_a_reservation_takes_a_lane_until_it_expires_or_the_nodes_next_reading_shows_the_task():
    one = [node("a", free=1)]
    held = [{"handle": "a", "until": T0 + 40, "n": 1}]
    r = pick(one, res=held)
    assert r["handle"] is None and r["waiting"]["code"] == "lane" and "1 reserved by this board" in r["waiting"]["reason"]
    assert pick(one, res=[{"handle": "a", "until": T0 - 1, "n": 1}])["handle"] == "a", "an expired reservation counts for nothing"
    res = nodes_route.Reservations()
    gone = nodes_route.Reservations()
    gone.add("a", T0 + 40)
    assert len(gone.items(T0 + 1)) == 1 and gone.items(T0 + 41) == [] and gone.items(T0) == [], "gone for good after its time"
    res.add("a", T0 + 40, repo=REPO, issue=201, ok=T0, at=T0)
    i202 = res.add("a", T0 + 40, repo=REPO, issue=202, ok=T0, at=T0)
    same_reading = [node("a", ok=T0, tasks=[{"id": 1, "issue_ref": "#201", "phase": "running"}])]
    assert res.release_seen(same_reading, T0 + 5) == 0, "the same reading that was there at dispatch shows nothing new"
    later = [node("a", ok=T0 + 20, tasks=[{"id": 1, "issue_ref": "#201", "phase": "running"}])]
    assert res.release_seen(later, T0 + 25) == 1 and [x["issue"] for x in res.items(T0 + 26)] == [202], "only the issue the reading shows is released"
    res.drop(i202)
    res.drop({"handle": "x"})                                        # dropping an item that is not there is harmless
    assert res.items(T0 + 26) == []
    res.clear()
    assert res.items(T0) == []


def test_two_ready_issues_with_one_free_lane_on_each_of_two_nodes_go_to_the_two_nodes_and_a_third_waits():
    ns = [node("a", free=1), node("b", free=1)]
    res = nodes_route.Reservations()
    first = pick(ns, res=res.items(T0))
    assert first["handle"] == "a"
    res.add(first["handle"], T0 + 40)
    second = pick(ns, res=res.items(T0))
    assert second["handle"] == "b"
    res.add(second["handle"], T0 + 40)
    third = pick(ns, res=res.items(T0))
    assert third["handle"] is None and third["waiting"]["code"] == "lane" and third["why"].count("no free lane") == 2


def test_with_a_lane_on_one_node_only_the_second_issue_waits_for_a_lane():
    ns = [node("a", free=1), node("b", free=0)]
    res = nodes_route.Reservations()
    res.add(pick(ns)["handle"], T0 + 40)
    again = pick(ns, res=res.items(T0))
    assert again["handle"] is None and "no free lane" in again["why"]


# ================================================================ settings: the routing keys and who may set them

def test_new_settings_are_kept_only_when_they_are_not_at_their_default_so_an_old_row_looks_as_it_did(lite_client):
    from app import main
    db = main.db
    nodes_epics.save_settings(db, "acme", "shop", 147, {"paused": True})
    assert db.kv_get("epic:acme/shop#147")["value"] == {"pins": {}, "epic_pin": None, "paused": True, "report": "phase", "auto_pr": False, "close": False}
    nodes_epics.save_settings(db, "acme", "shop", 147, {"max_window_pct": 70, "mcp_run": True})
    assert db.kv_get("epic:acme/shop#147")["value"]["max_window_pct"] == 70 and db.kv_get("epic:acme/shop#147")["value"]["mcp_run"] is True
    assert nodes_epics.max_window_pct(nodes_epics.load_settings(db, "acme", "shop", 147)) == 70
    nodes_epics.save_settings(db, "acme", "shop", 147, {"max_window_pct": None, "mcp_run": False, "paused": False})
    assert db.kv_get("epic:acme/shop#147") is None, "back to the defaults: no row"
    assert nodes_epics.max_window_pct(nodes_epics.load_settings(db, "acme", "shop", 147)) == settings.epic_max_window_pct == 85


def test_the_board_wide_limit_comes_from_the_environment_setting_and_a_bad_value_is_the_default():
    from app.config import parse_epic_max_window
    assert parse_epic_max_window(None) == (85, False) and parse_epic_max_window("70") == (70, False) and parse_epic_max_window(" 100 ") == (100, False)
    for bad in ("9", "101", "abc", "-5", "7.5", "٧٠", "1" * 9):
        assert parse_epic_max_window(bad) == (85, True), bad


# ================================================================ 2. the run loop, on a board whose hub and relay are fakes the test controls

class Clock:
    """A clock that only `sleep` moves: the loop's one-second spacing is measured on it, and nothing waits."""

    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class Hub:
    """The hub's readings of the paired nodes: a list the test replaces or changes. Same two methods the loop and the relay read."""
    interval = 20.0

    def __init__(self):
        self.recs: list[dict] = []

    def put(self, *recs) -> None:
        self.recs = list(recs)

    def update(self, handle: str, **kw) -> None:
        old = next(r for r in self.recs if r["handle"] == handle)
        self.recs = [node(handle, name=old["name"], **kw) if r is old else r for r in self.recs]

    def records(self, handle=None):
        return [dict(r) for r in self.recs if handle in (None, r["handle"])]

    def rows(self):
        return list(self.recs)


class Relay:
    """Stands in for nodes_relay.relay: records each call, raises what the test planned for (handle, issue), and otherwise does what a node does: writes its claim comment."""

    def __init__(self, gh, ck):
        self.gh, self.ck, self.calls, self.plan, self.hook = gh, ck, [], {}, None

    def __call__(self, handle, row, params, body, request, *, db=None, hub=None):
        assert row.name == "epic_work" and request.headers.get("x-ccboard") == "1"
        self.calls.append({"handle": handle, "issue": body["issue"], "body": dict(body), "user": request.state.user, "t": self.ck.t})
        if self.hook:
            self.hook(self, handle, body)
        err = self.plan.pop((handle, body["issue"]), None) or self.plan.pop((None, body["issue"]), None)
        if err is not None:
            raise err
        self.gh.comment(f"{REPO}#{body['issue']}", claim_body(handle, "running", task=str(len(self.calls)), branch=f"worktree-t{body['issue']}"))
        return {"node": handle, "age": 0, "data": {"claimed": True, "reason": "won", "task_ref": f"{handle}:{len(self.calls)}", "id": len(self.calls), "branch": f"worktree-t{body['issue']}",
                                                     "tmux": "shop--api--t", "repo": body["repo"], "issue": body["issue"]}}


def refusal(why: str, status: int = 409, reason: str = "work_refused", **extra) -> nr.RelayError:
    return nr.RelayError(status, reason, f"box: {why}", kind="failed", why=why, **extra)


class Rt:
    def __init__(self, c, main, gh, ck, hub, relay):
        self.c, self.main, self.gh, self.ck, self.hub, self.relay = c, main, gh, ck, hub, relay

    @property
    def runner(self) -> "nodes_run.Runner":
        return self.main._runner()

    @property
    def db(self):
        return self.main.db

    def api(self, method: str, path: str = "", headers=H, **kw):
        return self.c.request(method, f"/api/epics/acme/shop/147{path}", headers=headers, **kw)

    def run(self, headers=H):
        return self.api("POST", "/run", headers=headers)

    def epic(self) -> dict:
        r = self.api("GET", "", headers=ID)
        assert r.status_code == 200, r.text
        return r.json()

    def child(self, n: int) -> dict:
        return next(c for c in self.epic()["children"] if c["number"] == n)

    def cycle(self) -> bool:
        """One cycle of the loop, a hundred seconds after the last one (the reader's cache and the cooldowns have moved on)."""
        self.ck.t += 100
        return self.runner.tick()

    def calls(self) -> list[tuple]:
        return [(c["handle"], c["issue"]) for c in self.relay.calls]

    def close(self, *numbers: int) -> None:
        for n in numbers:
            self.gh.world["issues"][f"{REPO}#{n}"]["state"] = "closed"

    def pair(self, *names: str) -> list[str]:
        out = []
        for i, name in enumerate(names):
            row = nodes.put_peer({"direction": "out", "url": f"https://100.64.0.{20 + i}", "name": name, "scopes": ["read", "tasks"], "node_id": "n_" + (name.encode().hex() + "0" * 16)[:16]},
                                 db=self.db)
            out.append(row["handle"])
        return out

    def audit(self, action: str) -> list[dict]:
        return self.db.node_audit_list(100, action=action)


def epic(rt: Rt, count: int = 3, bodies: dict | None = None, first: int = 201) -> None:
    rt.gh.add_issue(REPO, 147, "The epic", labels=["ccboard:epic"], user="alice")
    for i in range(count):
        n = first + i
        rt.gh.add_issue(REPO, n, f"Task {n}", user="alice", body=(bodies or {}).get(n, "Do the work."))
        rt.gh.link(EPIC, f"{REPO}#{n}")


@pytest.fixture
def rt(lite_client, tmp_path, monkeypatch):
    from app import main
    ck = Clock()
    gh = FakeGitHub(tmp_path).use_memory(monkeypatch)
    for target, name in ((nodes_route, "clock"), (github.reader, "mono"), (nodes_epics, "mono")):
        monkeypatch.setattr(target, name, ck)
    monkeypatch.setattr(nodes_run, "sleep", ck.sleep)
    monkeypatch.setattr(nodes, "registry_listener", None)
    monkeypatch.setattr(nodes, "display_name", lambda: "node-a")
    monkeypatch.setattr(settings, "epic_writes", True)
    hub, relay = Hub(), Relay(gh, ck)
    monkeypatch.setattr(main, "_hub", lambda: hub)
    monkeypatch.setattr(main, "node_runner", None)
    monkeypatch.setattr(nodes_run.Runner, "ensure", lambda self: False)          # no thread: the tests run the cycles themselves
    monkeypatch.setattr(nr, "relay", relay)
    nodes_run.agent_limiter.clear()
    set_repos(main, monkeypatch, [REPO])
    t = Rt(lite_client, main, gh, ck, hub, relay)
    yield t
    nodes_run.agent_limiter.clear()


def test_run_sends_each_ready_issue_to_the_node_pick_chose_one_at_a_time_a_second_apart_and_writes_why_and_an_audit_row(rt):
    epic(rt, 3)
    rt.hub.put(node("box", free=1, pct=31.0), node("mac", free=2, pct=60.0))
    r = rt.run()
    assert r.status_code == 200 and r.json()["route"]["running"] is True and r.json()["route"]["by"] == "alice@example.com", r.text
    assert rt.relay.calls == [], "the route only marks the run; the loop sends"
    assert rt.runner.tick() is True
    assert rt.calls() == [("box", 201), ("mac", 202), ("mac", 203)], "box has the most headroom but one lane; the reservation sends the next two to mac, and not one more than its two lanes"
    times = [c["t"] for c in rt.relay.calls]
    assert all(b - a >= 1.0 for a, b in zip(times, times[1:])), times
    assert {c["user"] for c in rt.relay.calls} == {"alice@example.com"}
    assert rt.relay.calls[0]["body"] == {"repo": REPO, "issue": 201, "epic": EPIC}, "routing sends the three values of the work row and nothing it chose itself"
    a = rt.runner.view("acme", "shop", 147)["assigned"]
    assert a["201"]["handle"] == "box" and a["201"]["why"] == "chose box: 1 free lane, 5-hour window 31 percent" and a["201"]["task_ref"] == "box:1"
    assert a["202"]["why"] == "chose mac: 2 free lanes, 5-hour window 60 percent"
    rows = rt.audit("epic_route")
    assert len(rows) == 3 and {r_["direction"] for r_ in rows} == {"out"} and {r_["user"] for r_ in rows} == {"alice@example.com"}
    assert {r_["target"] for r_ in rows} == {f"route {REPO}#{n}" for n in (201, 202, 203)} and {r_["node_name"] for r_ in rows} == {"box", "mac"}
    assert any("chose box" in (r_["detail"] or "") for r_ in rows)
    d = rt.epic()
    assert d["route"]["running"] is True and d["route"]["assigned"]["201"]["handle"] == "box" and d["children"][0]["route"]["state"] == "assigned"
    n_calls = len(rt.relay.calls)
    assert rt.cycle() is True and len(rt.relay.calls) == n_calls, "the next cycle finds all three held by a node: nothing is sent twice, and the run goes on watching"


def test_when_everything_is_closed_the_run_ends_and_leaves_no_index_entry(rt):
    epic(rt, 2)
    rt.hub.put(node("box", cap=0))
    rt.run()
    rt.runner.tick()
    assert rt.db.kv_get("epic-run-index")["value"] == ["acme/shop#147"]
    rt.close(201, 202)
    assert rt.cycle() is False
    v = rt.runner.view("acme", "shop", 147)
    assert v["running"] is False and v["ended"] == "finished" and v["ended_at"] and rt.db.kv_get("epic-run-index") is None and set(v["assigned"]) == {"201", "202"}


def test_never_more_calls_than_free_lanes_and_the_rest_wait_for_a_lane(rt):
    epic(rt, 5)
    rt.hub.put(node("box", free=1), node("mac", free=2))
    rt.run()
    rt.runner.tick()
    assert len(rt.relay.calls) == 3
    v = rt.runner.view("acme", "shop", 147)
    assert sorted(v["waiting"]) == ["204", "205"] and all(w_["code"] == "lane" for w_ in v["waiting"].values())
    assert "box has no free lane (1 reserved by this board)" in v["waiting"]["204"]["reason"] and v["waiting"]["204"]["text"].startswith("waiting: ")
    assert {x["handle"] for x in v["waiting"]["204"]["refused"]} == {"box", "mac"}
    page = rt.epic()
    assert next(c for c in page["children"] if c["number"] == 204)["route"]["state"] == "waiting" and "no free lane" in next(c for c in page["children"] if c["number"] == 204)["route"]["text"]


def test_a_reservation_holds_the_lane_for_two_poll_cycles_and_then_expires_by_itself(rt):
    epic(rt, 2)
    rt.hub.put(node("box", free=1))
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [("box", 201)] and rt.runner.view("acme", "shop", 147)["waiting"]["202"]["code"] == "lane"
    assert rt.runner.reservations.items(rt.ck.t)[0]["handle"] == "box"
    rt.runner.tick()
    assert rt.calls() == [("box", 201)], "the node's reading still says one free lane, but this board promised it"
    rt.ck.t += 41
    assert rt.runner.reservations.items(rt.ck.t) == [], "two poll cycles later the reservation has expired by itself"
    rt.runner.tick()
    assert rt.calls() == [("box", 201), ("box", 202)]


def test_taken_moves_on_to_the_next_issue_and_the_issue_is_read_again_next_cycle(rt):
    epic(rt, 3)
    rt.hub.put(node("box", free=2), node("mac", free=0))
    rt.relay.plan[("box", 201)] = refusal("taken", winner="amber-ab")
    rt.relay.hook = lambda r, h, b: r.gh.comment(f"{REPO}#201", claim_body("amber-ab", "running")) if b["issue"] == 201 else None
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [("box", 201), ("box", 202), ("box", 203)], "the refusal released its lane: the next two issues still had two lanes between them"
    v = rt.runner.view("acme", "shop", 147)
    assert v["held"]["201"]["code"] == "taken" and "box: taken" in v["held"]["201"]["text"] and sorted(v["assigned"]) == ["202", "203"]
    rt.cycle()
    assert [c for c in rt.calls() if c[1] == 201] == [("box", 201)], "it is held by the node that won it now: it is not sent again"
    assert rt.child(201)["claim"]["alias"] == "amber-ab"


def test_lanes_full_blocks_that_node_for_two_cycles_and_the_next_issue_goes_to_the_other_one(rt):
    epic(rt, 2)
    rt.hub.put(node("box", free=2, pct=10.0), node("mac", free=2, pct=50.0))
    rt.relay.plan[("box", 201)] = refusal("lanes_full")
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [("box", 201), ("mac", 202)], "box said its lanes are full: 202 does not go there"
    assert rt.runner.blocked["box"]["code"] == "lanes_full"
    rt.cycle()
    assert rt.calls()[2] == ("box", 201), "201 is tried again after the block"


def test_a_call_whose_outcome_is_not_known_is_not_repeated_until_its_claim_shows_or_ten_minutes_pass(rt):
    epic(rt, 1)
    rt.hub.put(node("box", free=2))
    rt.relay.plan[("box", 201)] = nr.RelayError(504, "unconfirmed", "box did not answer in 75 s: it could not be confirmed whether it started, and it was not retried.", kind="failed")
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [("box", 201)]
    assert rt.runner.view("acme", "shop", 147)["held"]["201"]["code"] == "unclear"
    assert rt.runner.reservations.items(rt.ck.t), "the lane stays reserved: the node may have started it"
    assert rt.cycle() is True and rt.cycle() is True and rt.calls() == [("box", 201)], "still free on GitHub, still not sent again"
    rt.ck.t += 700
    rt.runner.tick()
    assert rt.calls() == [("box", 201), ("box", 201)]


def test_an_unexpected_failure_of_a_call_is_treated_as_unknown_and_does_not_end_the_cycle(rt):
    epic(rt, 2)
    rt.hub.put(node("box", free=2))
    rt.relay.plan[("box", 201)] = RuntimeError("boom")
    rt.run()
    assert rt.runner.tick() is True
    assert rt.calls() == [("box", 201), ("box", 202)] and rt.runner.view("acme", "shop", 147)["held"]["201"]["code"] == "unclear"


def test_pause_stops_new_assignments_at_once_and_resume_goes_on(rt):
    epic(rt, 3)
    rt.hub.put(node("box", free=3))
    rt.relay.hook = lambda r, h, b: rt.api("POST", "/pause") if len(r.calls) == 1 else None          # a person presses Pause while the first issue is on its way
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [("box", 201)], "the second call is not made: Pause is read again before every call"
    v = rt.runner.view("acme", "shop", 147)
    assert v["paused"] is True and v["running"] is True and v["assigned"]["201"]["handle"] == "box", "the claim that was made stays; the run is paused, not ended"
    rt.cycle()
    assert rt.calls() == [("box", 201)]
    r = rt.api("POST", "/resume")
    assert r.status_code == 200 and r.json()["paused"] is False
    rt.cycle()
    assert rt.calls() == [("box", 201), ("box", 202), ("box", 203)]


def test_run_lifts_a_pause_and_a_second_run_does_not_double_the_run(rt):
    epic(rt, 1)
    rt.hub.put(node("box"))
    assert rt.api("POST", "/pause").json()["route"]["paused"] is True
    assert rt.run().json()["route"]["paused"] is False
    rt.run()
    assert rt.db.kv_get("epic-run-index")["value"] == ["acme/shop#147"]


def test_a_depends_on_chain_holds_each_child_until_its_parent_is_closed(rt):
    epic(rt, 3, bodies={202: "Needs the first.\n\ndepends on #201", 203: "depends on #202"})
    rt.hub.put(node("box", cap=0))
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [("box", 201)], "only the head of the chain is ready"
    v = rt.runner.view("acme", "shop", 147)
    assert v["held"]["202"] == {"code": "depends", "text": "waits for #201", "until": None} and v["held"]["203"]["text"] == "waits for #202"
    assert rt.cycle() and rt.calls() == [("box", 201)]
    rt.close(201)
    rt.cycle()
    assert rt.calls() == [("box", 201), ("box", 202)], "201 closed: 202 is ready, 203 still waits for 202"
    rt.close(202)
    rt.cycle()
    assert rt.calls() == [("box", 201), ("box", 202), ("box", 203)]


def test_a_dependency_outside_the_epic_must_be_read_and_closed(rt):
    epic(rt, 1, bodies={201: "depends on #300"})
    rt.gh.add_issue(REPO, 300, "Elsewhere", user="alice")
    rt.hub.put(node("box", cap=0))
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [] and rt.runner.view("acme", "shop", 147)["held"]["201"]["text"] == "waits for #300"
    rt.close(300)
    rt.cycle()
    assert rt.calls() == [("box", 201)]


def test_a_dependency_in_a_repository_this_board_does_not_have_is_not_read_and_is_not_met(rt):
    epic(rt, 1, bodies={201: "depends on other/place#5"})
    rt.gh.add_issue("other/place", 5, "Closed there", state="closed", user="alice")
    rt.hub.put(node("box", cap=0))
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [] and "could not be checked" in rt.runner.view("acme", "shop", 147)["held"]["201"]["text"]
    assert not any("other/place" in p for p in rt.gh.paths()), "no request was made for a repository that is not this board's"


def test_a_window_reset_releases_the_waiting_issues(rt):
    epic(rt, 3)
    rt.hub.put(node("box", free=2, pct=90.0, resets_at=int(T0 + 600)), node("mac", free=2, pct=92.0, resets_at=int(T0 + 1200)))
    rt.run()
    rt.runner.tick()
    assert rt.calls() == []
    w_ = rt.runner.view("acme", "shop", 147)["waiting"]
    assert sorted(w_) == ["201", "202", "203"] and w_["201"]["code"] == "window"
    assert w_["201"]["until"] == T0 + 600 and w_["201"]["until_iso"] == iso(T0 + 600), "the earliest time any node names"
    assert "box 5-hour window is at 90 percent until" in w_["201"]["reason"] and "mac 5-hour window is at 92 percent" in w_["201"]["reason"]
    assert rt.epic()["children"][0]["route"] == {"state": "waiting", "text": w_["201"]["text"], "until": iso(T0 + 600), "code": w_["201"]["code"]}
    rt.ck.t = T0 + 700                                                      # box's window reset (its reading is still the old 90, from before the reset)
    rt.runner.tick()
    assert rt.calls() == [("box", 201), ("box", 202)], "the reset window is not a reading: unknown counts as 50, and box has two lanes"
    assert sorted(rt.runner.view("acme", "shop", 147)["waiting"]) == ["203"]


def test_a_node_that_reports_a_lower_window_after_the_reset_takes_work_too(rt):
    epic(rt, 1)
    rt.hub.put(node("box", pct=95.0, resets_at=int(T0 + 5000)))
    rt.run()
    rt.runner.tick()
    assert rt.calls() == []
    rt.hub.update("box", pct=4.0, resets_at=int(T0 + 18000))
    rt.cycle()
    assert rt.calls() == [("box", 201)]


@pytest.mark.parametrize("kw", [dict(status="offline"), dict(status="stale"), dict(status="unauthorized"), dict(slugs=("acme/api",)), dict(claude=(True, False, False)),
                                dict(backoff=iso(T0 + 3600)), dict(pct=90.0), dict(scopes=("read",)), dict(state=False), dict(card=False)])
def test_a_node_that_cannot_take_the_issue_is_never_chosen_by_the_loop(rt, kw):
    epic(rt, 2)
    rt.hub.put(node("bad", **kw), node("good", free=1))
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [("good", 201)], "the one good node takes what its lane allows; the bad one is not asked even for the second issue"
    assert not [c for c in rt.relay.calls if c["handle"] == "bad"]


def test_a_pin_to_a_node_that_cannot_take_it_waits_and_is_never_sent_elsewhere(rt):
    epic(rt, 2)
    box, mac = rt.pair("box", "mac")
    rt.hub.put(node(box, status="offline"), node(mac, free=3))
    rt.epic()
    assert rt.api("PUT", "/settings", json={"pins": {"201": box}}).status_code == 200
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [(mac, 202)], "the pinned issue waits for its node; the other one is not held up"
    w_ = rt.runner.view("acme", "shop", 147)["waiting"]["201"]
    assert w_["code"] == "offline" and w_["pinned"] == box and "pinned by the issue's pin" in w_["reason"]
    rt.hub.put(node(box, free=3), node(mac, free=3))
    rt.cycle()
    assert rt.calls() == [(mac, 202), (box, 201)], "the node is back: the pin is honoured"


def test_a_label_pin_and_an_epic_pin_are_honoured_and_a_pin_to_a_node_that_was_unpaired_waits(rt):
    epic(rt, 3)
    rt.gh.world["issues"][f"{REPO}#202"]["labels"] = ["ccboard:node:mac"]
    rt.gh.world["issues"][f"{REPO}#203"]["labels"] = ["ccboard:node:ghost"]
    box, mac = rt.pair("box", "mac")
    rt.hub.put(node(box, free=3, pct=1.0), node(mac, free=3, pct=70.0))
    rt.epic()
    rt.api("PUT", "/settings", json={"epic_pin": mac})
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [(mac, 201), (mac, 202)], "the epic pin sends 201 to mac though box is emptier; the label pins 202 to mac; the label naming a node that is not paired waits"
    assert rt.runner.view("acme", "shop", 147)["waiting"]["203"]["code"] == "pin_unpaired"


def test_the_page_pin_wins_over_the_label_in_the_loop(rt):
    epic(rt, 1)
    rt.gh.world["issues"][f"{REPO}#201"]["labels"] = ["ccboard:node:mac"]
    box, mac = rt.pair("box", "mac")
    rt.hub.put(node(box), node(mac))
    rt.epic()
    rt.api("PUT", "/settings", json={"pins": {"201": box}})
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [(box, 201)]


def test_an_issue_the_loop_will_not_send_says_why_and_the_run_keeps_watching_what_clears_by_itself(rt):
    epic(rt, 4)
    rt.gh.add_issue(REPO, 205, "Not by us", user="mallory")
    rt.gh.link(EPIC, f"{REPO}#205")
    rt.gh.comment(f"{REPO}#202", claim_body("amber-ab", "failed"))
    rt.gh.comment(f"{REPO}#203", claim_body("amber-ab", "done"))
    rt.hub.put(node("box", cap=0))
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [("box", 201), ("box", 204)]
    h = rt.runner.view("acme", "shop", 147)["held"]
    assert h["202"]["code"] == "failed" and "needs a person" in h["202"]["text"] and h["203"]["code"] == "done_open" and h["205"]["code"] == "author" and "mallory" in h["205"]["text"]


def test_the_loop_reads_the_claim_first_an_unknown_claim_is_never_free(rt):
    epic(rt, 1)
    rt.hub.put(node("box"))
    rt.gh.comment(f"{REPO}#201", "just a note", user="bob")                 # an issue with no comment has nothing to read; this one has
    rt.gh.fail("/repos/acme/shop/issues/201/comments", 500, times=-1, method="GET")
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [], "a claim that could not be read is not a free issue"
    v = rt.runner.view("acme", "shop", 147)
    assert v["held"]["201"]["code"] == "claim_unknown" and v["running"] is True


def test_a_rate_limit_stops_the_cycle_and_the_next_call_waits_for_it(rt):
    epic(rt, 3)
    rt.hub.put(node("box", cap=0))
    rt.relay.plan[("box", 201)] = nr.RelayError(429, "rate_limited", "too many relayed requests from you; wait 30 s", headers={"Retry-After": "30"}, kind="refused")
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [("box", 201)], "the rest of the cycle does not hammer a node that said wait"
    rt.runner.tick()
    assert rt.calls() == [("box", 201), ("box", 202), ("box", 203)] and rt.relay.calls[1]["t"] - rt.relay.calls[0]["t"] >= 30, "the next call waited the 30 s it was told (201 is still in its cooldown)"


def test_a_node_that_cannot_be_reached_is_left_alone_for_two_cycles(rt):
    epic(rt, 2)
    rt.hub.put(node("box", pct=10.0), node("mac", pct=50.0))
    rt.relay.plan[("box", 201)] = nr.RelayError(502, "unreachable", "box could not be reached", kind="failed")
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [("box", 201), ("mac", 202)], "the next issue goes to the node that can be reached"
    assert rt.runner.blocked["box"]["code"] == "unreachable"
    rt.cycle()
    assert rt.calls()[2] == ("box", 201), "after the block the node is tried again"


def test_an_issue_the_node_refused_for_its_own_reasons_is_held_and_shown(rt):
    epic(rt, 2)
    rt.hub.put(node("box", cap=0))
    rt.relay.plan[("box", 201)] = refusal("bypass_refused")
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [("box", 201), ("box", 202)]
    assert rt.runner.view("acme", "shop", 147)["held"]["201"]["code"] == "bypass_refused"
    rt.cycle()
    assert rt.calls() == [("box", 201), ("box", 202)], "held for five minutes: not sent to another node to try again"


def test_a_crash_inside_a_cycle_is_logged_and_does_not_end_the_loop_or_leave_a_lane_reserved_forever(rt, monkeypatch):
    epic(rt, 1)
    rt.hub.put(node("box", cap=0))
    rt.run()
    boom = {"n": 0}
    real = nodes_epics.detail

    def exploding(*a, **k):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("boom")
        return real(*a, **k)
    monkeypatch.setattr(nodes_epics, "detail", exploding)
    assert rt.runner.tick() is True, "the failed epic is counted and the cycle ends normally"
    assert "RuntimeError" in rt.runner.view("acme", "shop", 147)["error"]
    rt.cycle()
    assert rt.calls() == [("box", 201)]
    rt.ck.t += 1000
    assert rt.runner.reservations.items(rt.ck.t) == []


# ---- Run, Pause and Resume are a person's, and Run is off without writes or without a node

def test_run_is_refused_without_github_writes_and_stores_nothing_and_starts_nothing(rt, monkeypatch):
    epic(rt, 1)
    rt.hub.put(node("box"))
    monkeypatch.setattr(settings, "epic_writes", False)
    r = rt.run()
    assert r.status_code == 409 and r.json()["reason"] == "writes_off" and "CCBOARD_EPIC_WRITES" in r.json()["error"], r.text
    assert rt.db.kv_get("epic-run-index") is None and rt.db.kv_get("epic-run:acme/shop#147") is None and rt.relay.calls == []
    assert not [t for t in threading.enumerate() if t.name == "epic-run"] and rt.runner.running() is False
    d = rt.epic()["route"]
    assert d["can_run"] is False and d["blocked"]["code"] == "writes_off" and d["running"] is False
    assert rt.api("POST", "/issues/201/run", json={"node": "auto"}).json()["reason"] == "writes_off", "one issue on a node is Run too"


def test_run_is_refused_when_no_node_is_paired_and_stores_nothing(rt):
    epic(rt, 1)
    r = rt.run()
    assert r.status_code == 409 and r.json()["reason"] == "no_nodes" and "No node is paired" in r.json()["error"]
    assert rt.db.kv_get("epic-run-index") is None and rt.db.kv_get("epic-run:acme/shop#147") is None
    assert rt.epic()["route"]["blocked"]["code"] == "no_nodes"


def test_the_hook_token_and_a_missing_csrf_header_cannot_run_pause_or_resume(rt):
    from app import hooks
    epic(rt, 1)
    rt.hub.put(node("box"))
    hook = {"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard": "1"}
    for method, path, kw in (("POST", "/run", {}), ("POST", "/pause", {}), ("POST", "/resume", {}), ("POST", "/issues/201/run", {"json": {"node": "auto"}})):
        assert rt.api(method, path, headers=hook, **kw).status_code == 403, path
        assert rt.api(method, path, headers=ID, **kw).status_code == 403, "no X-CCBoard header: " + path
    assert rt.db.kv_get("epic-run-index") is None and rt.db.kv_get("epic:acme/shop#147") is None and rt.relay.calls == []


def test_a_repository_this_board_does_not_have_is_a_404_and_bad_ids_are_a_400(rt):
    assert rt.c.post("/api/epics/other/place/5/run", headers=H).status_code == 404
    assert rt.c.post("/api/epics/acme/shop/abc/run", headers=H).status_code == 400
    assert rt.c.post("/api/epics/acme/shop/147/issues/zero/run", headers=H, json={"node": "auto"}).status_code == 400


# ---- one issue on a node, and the preview

def test_run_one_issue_on_auto_or_on_a_named_node_and_the_filters_still_apply(rt):
    epic(rt, 3)
    box, mac = rt.pair("box", "mac")
    rt.hub.put(node(box, pct=10.0), node(mac, pct=95.0, resets_at=int(T0 + 900)))
    rt.epic()
    ok = rt.api("POST", "/issues/201/run", json={"node": "auto"})
    assert ok.status_code == 200, ok.text
    j = ok.json()
    assert j["ok"] is True and j["handle"] == box and j["why"].startswith(f"chose {box}:") and j["data"]["claimed"] is True and rt.calls() == [(box, 201)]
    waiting = rt.api("POST", "/issues/202/run", json={"node": mac})
    assert waiting.status_code == 409 and waiting.json()["reason"] == "waiting" and waiting.json()["waiting"]["code"] == "window" and waiting.json()["waiting"]["pinned"] == mac
    assert waiting.json()["waiting"]["until_iso"] == iso(T0 + 900) and rt.calls() == [(box, 201)], "a node you name does not skip the window limit, and nothing was sent"
    named = rt.api("POST", "/issues/202/run", json={"node": box})
    assert named.status_code == 200 and rt.calls() == [(box, 201), (box, 202)] and named.json()["why"].endswith("pinned by the issue's pin")
    assert rt.api("POST", "/issues/202/run", json={"node": box}).status_code == 409, "it is claimed now"
    assert rt.api("POST", "/issues/203/run", json={"node": "../x"}).status_code == 400
    assert rt.api("POST", "/issues/203/run", json={"node": "auto", "force": True}).status_code == 422, "no extra field"
    assert rt.api("POST", "/issues/999/run", json={"node": "auto"}).status_code == 404
    spaced = [c["t"] for c in rt.relay.calls]
    assert spaced[1] - spaced[0] >= 1.0


def test_the_auto_preview_names_the_node_and_the_reason_and_sends_nothing(rt):
    box, mac = rt.pair("box", "mac")
    rt.hub.put(node(box, pct=10.0, free=2), node(mac, pct=60.0, free=3))
    r = rt.c.get("/api/route/auto?slug=acme/shop&agent=claude", headers=ID)
    assert r.status_code == 200
    j = r.json()
    assert j["handle"] == box and j["why"] == f"chose {box}: 2 free lanes, 5-hour window 10 percent" and j["agent"] == "claude" and j["slug"] == REPO and j["waiting"] is None
    assert [x["handle"] for x in j["ranked"]] == [box, mac] and rt.relay.calls == []
    rt.hub.put(node(box, free=0), node(mac, status="offline"))
    j = rt.c.get("/api/route/auto?slug=acme/shop", headers=ID).json()
    assert j["handle"] is None and j["waiting"]["code"] == "none_ready" and "no free lane" in j["waiting"]["reason"] and "offline" in j["waiting"]["reason"]
    assert rt.c.get("/api/route/auto?slug=acme/shop&agent=bash", headers=ID).status_code == 400
    assert rt.c.get("/api/route/auto?slug=acme", headers=ID).status_code == 400
    assert rt.c.get("/api/route/auto", headers=ID).status_code == 400
    rt.hub.put(node(box, codex_pct=33.0), node(mac, status="offline"))
    assert rt.c.get("/api/route/auto?slug=acme/shop&agent=codex", headers=ID).json()["why"] == f"chose {box}: 2 free lanes, weekly window 33 percent"


def test_the_epic_route_preview_applies_the_epics_pins_and_limits(rt):
    epic(rt, 1)
    box, mac = rt.pair("box", "mac")
    rt.hub.put(node(box, pct=10.0), node(mac, pct=60.0))
    rt.epic()
    rt.api("PUT", "/settings", json={"pins": {"201": mac}, "max_window_pct": 50})
    j = rt.api("GET", "/route?issue=201", headers=ID).json()
    assert j["route"]["running"] is False and j["preview"]["handle"] is None and j["preview"]["waiting"]["pinned"] == mac and "60 percent" in j["preview"]["waiting"]["reason"]
    assert rt.api("GET", "/route", headers=ID).json()["preview"] is None
    assert rt.api("GET", "/route?issue=999", headers=ID).status_code == 404


def test_the_routing_settings_are_checked_and_only_a_person_sets_them(rt):
    from app import hooks
    box, mac = rt.pair("box", "mac")
    put = lambda body, headers=H: rt.api("PUT", "/settings", json=body, headers=headers)
    j = put({"max_window_pct": 60, "group": [[box, mac]], "mcp_run": True}).json()
    assert j["max_window_pct"] == 60 and j["max_window_pct_set"] is True and j["group"] == [[box, mac]] and j["mcp_run"] is True
    for bad in ({"max_window_pct": 5}, {"max_window_pct": 101}, {"max_window_pct": True}, {"max_window_pct": "70"}, {"max_window_pct": 70.5}, {"group": [[box]]}, {"group": [[box, box]]},
                {"group": [[box, "ghost"]]}, {"group": [["local", box]]}, {"group": [[box, mac], [mac, "x"]]}, {"group": "all"}, {"group": [list("abcdefghi")]}, {"mcp_run": "yes"},
                {"mcp_run": 1}, {"permission_mode": "bypassPermissions"}, {"running": True}):
        assert put(bad).status_code in (400, 422), bad
    assert put({"max_window_pct": None, "group": [], "mcp_run": False}).json()["max_window_pct"] == 85
    hook = {"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard": "1"}
    assert put({"mcp_run": True}, headers=hook).status_code == 403, "the hook token is every agent session on the box: it never sets the switch that lets an agent run an epic"
    assert rt.db.kv_get("epic:acme/shop#147") is None
    assert nodes_epics.load_settings(rt.db, "acme", "shop", 147)["mcp_run"] is False


def test_the_page_answer_carries_the_new_settings_and_the_agent_of_each_issue(rt):
    epic(rt, 2, bodies={202: "## Who should do it\n\n- Codex: `codex -m gpt-6.1-sol -s workspace-write -a on-request`\n"})
    rt.hub.put(node("box"))
    d = rt.epic()
    assert d["max_window_pct"] == 85 and d["max_window_pct_set"] is False and d["group"] == [] and d["mcp_run"] is False
    assert [c["agent"] for c in d["children"]] == ["claude", "codex"]
    assert d["route"]["running"] is False and d["route"]["assigned"] == {} and d["route"]["waiting"] == {} and d["route"]["can_run"] is True
    assert d["children"][0]["route"] is None


def test_a_codex_issue_is_routed_by_the_codex_login_and_window(rt):
    epic(rt, 1, bodies={201: "## Who should do it\n\n- Codex: `codex -m gpt-6.1-sol -s workspace-write -a on-request`\n"})
    rt.hub.put(node("claude-only", codex=(False, False, False)), node("both", codex_pct=40.0))
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [("both", 201)] and "weekly window 40 percent" in rt.runner.view("acme", "shop", 147)["assigned"]["201"]["why"]


# ---- the thread: nothing without a run, one with, and it survives a crash

def test_a_board_with_no_run_starts_nothing(rt, monkeypatch):
    monkeypatch.setattr(nodes_run.Runner, "ensure", REAL_ENSURE)
    rt.hub.put(node("box"))
    r = nodes_run.Runner(rt.db, hub_fn=lambda: rt.hub, repos_fn=lambda: [REPO])
    assert r.resume() is False and r.ensure() is False and r.running() is False
    assert not [t for t in threading.enumerate() if t.name == "epic-run"]
    assert rt.db.kv_get("epic-run-index") is None, "no row, no thread, no timer"
    assert r.tick() is False and r.mem == {}


def test_the_thread_starts_with_a_run_ends_with_it_and_a_crash_does_not_end_it(rt, monkeypatch):
    epic(rt, 1)
    rt.hub.put(node("box", cap=0))
    runner = nodes_run.Runner(rt.db, hub_fn=lambda: rt.hub, repos_fn=lambda: [REPO])
    monkeypatch.setattr(rt.main, "node_runner", runner)
    monkeypatch.setattr(nodes_run.Runner, "cycle_s", lambda self: 0.02)
    monkeypatch.setattr(nodes_run.Runner, "ensure", REAL_ENSURE)
    real_tick, state = nodes_run.Runner.tick, {"n": 0}

    def tick(self, only=None, deadline=None):
        state["n"] += 1
        rt.ck.t += 100
        if state["n"] == 1:
            raise RuntimeError("boom")
        if state["n"] == 3:
            rt.close(201)
        return real_tick(self, only, deadline)
    monkeypatch.setattr(nodes_run.Runner, "tick", tick)
    try:
        assert rt.run().status_code == 200
        end = time.time() + 15
        while time.time() < end and (runner.running() or runner.view("acme", "shop", 147)["running"]):
            time.sleep(0.02)
        assert state["n"] >= 3 and rt.calls() == [("box", 201)], "the first cycle crashed, the loop went on and sent the issue"
        v = runner.view("acme", "shop", 147)
        assert v["running"] is False and v["ended"] == "finished"
        end = time.time() + 5
        while time.time() < end and runner.running():
            time.sleep(0.02)
        assert runner.running() is False and rt.db.kv_get("epic-run-index") is None, "no run is active: the thread has ended"
    finally:
        runner.stop(5.0)


def test_stop_ends_the_thread_even_while_it_waits_between_cycles(rt, monkeypatch):
    epic(rt, 1)
    rt.hub.put(node("box", free=0))
    runner = nodes_run.Runner(rt.db, hub_fn=lambda: rt.hub, repos_fn=lambda: [REPO])
    monkeypatch.setattr(rt.main, "node_runner", runner)
    monkeypatch.setattr(nodes_run.Runner, "ensure", REAL_ENSURE)
    assert rt.run().status_code == 200
    end = time.time() + 5
    while time.time() < end and not runner.running():
        time.sleep(0.01)
    assert runner.running() is True
    t0 = time.time()
    runner.stop(5.0)
    assert runner.running() is False and time.time() - t0 < 4


# ================================================================ 3. end to end on the two in-process boards of the work phase

def test_run_on_two_boards_routes_to_the_node_that_holds_the_repository_and_the_node_does_the_work(w, monkeypatch):
    from app import main
    ck = Clock(time.time())
    monkeypatch.setattr(nodes_route, "clock", ck)
    monkeypatch.setattr(nodes_run, "sleep", ck.sleep)
    monkeypatch.setattr(nodes_run.Runner, "ensure", lambda self: False)
    hub = Hub()
    hub.put(node(w.h, name="node-b", ok=ck.t, cap=3, free=3, pct=12.0), node("ghost", name="ghost", slugs=("acme/api",), ok=ck.t))
    monkeypatch.setattr(main, "_hub", lambda: hub)
    monkeypatch.setattr(main, "node_runner", None)
    times = []
    real = nr.relay
    monkeypatch.setattr(nr, "relay", lambda *a, **k: (times.append(ck.t), real(*a, **k))[1])
    with w.a.enter():
        runner = main._runner()
        runner.start("acme", "shop", 147, "alice@example.com", via="page")
        assert runner.tick() is True
        v = runner.view("acme", "shop", 147)
    assert sorted(v["assigned"]) == ["201", "202", "203"], "the node reports three free lanes: the fourth issue waits for one"
    assert "no free lane" in v["waiting"]["204"]["reason"] and "does not have the repository cloned" in v["waiting"]["204"]["reason"]
    assert all(a["handle"] == w.h for a in v["assigned"].values()), "the node that has the repository; the other one is not even asked"
    assert len(times) == len(v["assigned"]) and all(b - a >= 1.0 for a, b in zip(times, times[1:]))
    tasks = w.b.db.tasks()
    assert sorted(t["issue_number"] for t in tasks) == sorted(int(k) for k in v["assigned"]) and all(t["issue_url"].startswith("https://github.com/acme/shop/issues/") for t in tasks)
    assert w.two.tmux_b["created"] and not w.two.tmux_a["created"], "the sessions are on the node, never on the hub"
    first = v["assigned"]["201"]
    assert first["task_ref"].startswith(f"{w.h}:") and first["why"].startswith(f"chose {w.h}: ")
    out = [r for r in w.a.db.node_audit_list(100) if r["direction"] == "out" and r["action"] != "paired"]
    assert {r["action"] for r in out} == {"work_issue", "epic_route"} and all(r["user"] == "alice@example.com" for r in out)
    assert len([r for r in out if r["action"] == "epic_route"]) == len(v["assigned"])


def test_one_issue_sent_by_hand_does_not_spend_an_agents_allowance_and_a_persons_run_takes_the_run_over(rt):
    epic(rt, 4)
    rt.hub.put(node("box", cap=0))
    rt.runner.start("acme", "shop", 147, "mcp:laptop", via="mcp", max_issues=3)
    assert rt.runner.view("acme", "shop", 147)["left"] == 3
    r = rt.api("POST", "/issues/201/run", json={"node": "auto"})
    assert r.status_code == 200 and rt.relay.calls[0]["user"] == "alice@example.com", "by hand it is the person who asked"
    v = rt.runner.view("acme", "shop", 147)
    assert v["left"] == 3 and v["by"] == "mcp:laptop" and v["via"] == "mcp" and v["assigned"]["201"]["handle"] == "box", "the agent's run is as it was; the issue is on its row"
    rt.run()
    v = rt.runner.view("acme", "shop", 147)
    assert v["by"] == "alice@example.com" and v["via"] == "page" and v["left"] is None, "a person's Run is the run now, with no limit of an agent's"


def test_what_the_loop_remembers_is_forgotten_after_an_hour(rt):
    epic(rt, 1)
    rt.hub.put(node("box", cap=0))
    rt.run()
    runner = rt.runner
    runner.recent[("acme/shop#147", 9)] = rt.ck.t - 4000
    runner.unclear[("acme/shop#147", 9)] = rt.ck.t - 4000
    runner.held[("acme/shop#147", 9)] = {"until": rt.ck.t - 4000, "code": "taken", "text": "x"}
    runner.blocked["ghost"] = {"until": rt.ck.t - 4000, "text": "x", "code": "x"}
    runner.recent[("acme/shop#147", 8)] = rt.ck.t - 10
    runner.tick()
    assert ("acme/shop#147", 9) not in runner.recent and not runner.unclear and not runner.held and "ghost" not in runner.blocked and ("acme/shop#147", 8) in runner.recent


def test_a_run_on_an_issue_that_is_not_there_or_is_not_an_epic_ends_at_its_first_cycle(rt):
    rt.hub.put(node("box"))
    assert rt.run().status_code == 200
    rt.runner.tick()
    v = rt.runner.view("acme", "shop", 147)
    assert v["running"] is False and v["ended"] == "stopped" and "no such issue" in v["error"] and rt.db.kv_get("epic-run-index") is None
    rt.gh.add_issue(REPO, 147, "Just an issue", user="alice")
    assert rt.run().status_code == 200
    rt.cycle()
    v = rt.runner.view("acme", "shop", 147)
    assert v["running"] is False and v["ended"] == "stopped" and "ccboard:epic" in v["error"] and rt.relay.calls == []


# ================================================================ 4. what a person takes back stops an agent's run, one peer's rate limit stops only that peer, the numbers a peer reports are checked

def agent_run(rt, monkeypatch, by: str = "local-token", level: str = "tasks") -> None:
    """An epic run an agent started the way epic_run does: the board's MCP level, the epic's own switch, then the run row (the agent's guard itself is tested with the tools)."""
    monkeypatch.setattr(settings, "mcp_nodes", level)
    nodes_epics.save_settings(rt.db, "acme", "shop", 147, {"mcp_run": True})
    rt.runner.start("acme", "shop", 147, by, via="mcp", max_issues=10)


def stopped(rt, word: str, runner=None) -> None:
    v = (runner or rt.runner).view("acme", "shop", 147)
    assert v["running"] is False and v["ended"] == "stopped" and rt.db.kv_get("epic-run-index") is None and v["error"], v
    [row] = [r for r in rt.audit("epic_run") if r["status"] == "stopped"]
    assert row["detail"] == f"stopped: {word}" and row["direction"] == "out" and row["target"] == f"run {REPO}#147", row


def test_an_agents_run_ends_stopped_when_a_person_turns_off_mcp_run_and_sends_nothing_more(rt, monkeypatch):
    epic(rt, 3)
    rt.hub.put(node("box", free=0))
    agent_run(rt, monkeypatch)
    rt.runner.tick()
    assert rt.calls() == [] and rt.runner.view("acme", "shop", 147)["running"] is True, "with the permission in place the run waits for a lane"
    rt.hub.put(node("box", free=3))
    nodes_epics.save_settings(rt.db, "acme", "shop", 147, {"mcp_run": False})
    rt.cycle()
    assert rt.calls() == []
    stopped(rt, "mcp_run_off")
    rt.cycle()
    assert rt.calls() == [], "an ended run stays ended"


def test_the_permission_is_read_again_before_every_call_not_only_at_the_top_of_the_cycle(rt, monkeypatch):
    epic(rt, 3)
    rt.hub.put(node("box", cap=0))
    agent_run(rt, monkeypatch)
    rt.relay.hook = lambda r, h, b: nodes_epics.save_settings(rt.db, "acme", "shop", 147, {"mcp_run": False}) if len(r.calls) == 1 else None
    rt.runner.tick()
    assert rt.calls() == [("box", 201)], "a person turned the switch off while the first issue was on its way: the second is not sent"
    stopped(rt, "mcp_run_off")


@pytest.mark.parametrize("level,word", [("read", "mcp_level"), ("", "mcp_off")])
def test_an_agents_run_ends_stopped_when_the_mcp_level_no_longer_allows_tasks(rt, monkeypatch, level, word):
    epic(rt, 2)
    rt.hub.put(node("box", cap=0))
    agent_run(rt, monkeypatch)
    monkeypatch.setattr(settings, "mcp_nodes", level)
    rt.runner.tick()
    assert rt.calls() == []
    stopped(rt, word)


def test_an_agents_run_ends_stopped_when_github_writes_go_off_and_a_persons_run_only_waits(rt, monkeypatch):
    epic(rt, 2)
    rt.hub.put(node("box", cap=0))
    agent_run(rt, monkeypatch)
    monkeypatch.setattr(settings, "epic_writes", False)
    rt.runner.tick()
    assert rt.calls() == []
    stopped(rt, "writes_off")
    monkeypatch.setattr(settings, "epic_writes", True)
    assert rt.run().status_code == 200
    rt.relay.hook = lambda r, h, b: monkeypatch.setattr(settings, "epic_writes", False) if len(r.calls) == 1 else None
    rt.runner.tick()
    assert rt.calls() == [("box", 201)], "writes went off while the first issue was on its way: the person's run sends no second one"
    assert rt.runner.view("acme", "shop", 147)["running"] is True, "a person's run is not ended by it, it goes on when the writes are back"
    monkeypatch.setattr(settings, "epic_writes", True)
    rt.cycle()
    assert rt.calls() == [("box", 201), ("box", 202)]


def test_an_agents_run_ends_stopped_when_its_device_token_is_revoked(rt, monkeypatch):
    from app import mcp_tokens
    epic(rt, 2)
    rt.hub.put(node("box", free=0))
    _, row = mcp_tokens.mint("laptop")
    agent_run(rt, monkeypatch, by="mcp:laptop")
    rt.runner.tick()
    assert rt.runner.view("acme", "shop", 147)["running"] is True, "a live token keeps the run going"
    assert mcp_tokens.revoke(row["id"]) is True
    rt.hub.put(node("box", free=3))
    rt.cycle()
    assert rt.calls() == []
    stopped(rt, "token_revoked")


def test_a_restart_does_not_bring_back_a_run_whose_permission_was_taken_back(rt, monkeypatch):
    epic(rt, 2)
    rt.hub.put(node("box", cap=0))
    agent_run(rt, monkeypatch)
    nodes_epics.save_settings(rt.db, "acme", "shop", 147, {"mcp_run": False})
    monkeypatch.setattr(nodes_run.Runner, "ensure", REAL_ENSURE)
    again = nodes_run.Runner(rt.db, hub_fn=lambda: rt.hub, repos_fn=lambda: [REPO])
    try:
        assert again.resume() is False and again.running() is False
        stopped(rt, "mcp_run_off", again)
        assert rt.calls() == []
    finally:
        again.stop(5.0)


def test_a_run_a_person_started_does_not_need_mcp_run_or_the_mcp_level(rt, monkeypatch):
    epic(rt, 2)
    rt.hub.put(node("box", cap=0))
    monkeypatch.setattr(settings, "mcp_nodes", "")
    assert nodes_epics.load_settings(rt.db, "acme", "shop", 147)["mcp_run"] is False
    assert rt.run().status_code == 200
    rt.runner.tick()
    assert rt.calls() == [("box", 201), ("box", 202)] and not rt.audit("epic_run")


def test_a_node_that_answers_429_is_left_alone_until_its_retry_after_and_the_next_issue_goes_to_another_node(rt):
    epic(rt, 2)
    rt.hub.put(node("a", pct=10.0, cap=0), node("b", pct=50.0, cap=0))
    rt.relay.plan[("a", 201)] = nr.RelayError(429, "rate_limited", "a is receiving too many requests from this board; wait 3600 s", headers={"Retry-After": "3600"}, kind="failed")
    rt.run()
    rt.runner.tick()
    assert rt.calls() == [("a", 201), ("b", 202)], "the rest of the cycle goes on with the node that did not say wait"
    assert 1.0 <= rt.relay.calls[1]["t"] - rt.relay.calls[0]["t"] < 5.0, "the usual second between calls, not the hour a asked for"
    assert rt.runner.blocked["a"]["code"] == "rate_limited" and rt.runner.blocked["a"]["until"] >= rt.ck.t + 3000
    rt.cycle()
    assert rt.calls()[2] == ("b", 201), "a is skipped until its time, the issue it refused goes to b"


def test_stop_ends_the_wait_between_two_calls_at_once(rt, monkeypatch):
    runner = rt.runner
    runner._not_before = rt.ck.t + 3600                                        # a rate limit says: wait an hour
    monkeypatch.setattr(nodes_run, "sleep", lambda s: time.sleep(0.01))        # the clock stands still, so the wait would never end by itself
    t = threading.Thread(target=runner._gap, daemon=True)
    t.start()
    time.sleep(0.1)
    assert t.is_alive(), "the gap is pending"
    runner.stop(1.0)
    t.join(2.0)
    assert not t.is_alive(), "a stopping board does not wait out the rate limit"


@pytest.mark.parametrize("cap", [-1, -100, 2.5])
def test_a_lane_cap_that_is_negative_or_not_a_whole_number_is_not_unlimited(cap):
    r = pick([node("n", cap=cap, free=2)])
    assert r["handle"] is None and r["waiting"]["code"] == "lane_unknown" and "has not reported its task lanes" in r["waiting"]["reason"]
    assert pick([node("n", cap=cap, free=2), node("ok")])["handle"] == "ok"


@pytest.mark.parametrize("pct", [-1.0, 100.5, 250.0, float("inf"), float("nan")])
def test_a_window_percentage_outside_0_to_100_counts_as_unknown_and_50(pct):
    r = pick([node("n", pct=pct)])
    assert r["handle"] == "n" and "window unknown" in r["why"] and r["ranked"][0]["headroom"] == 50.0
    assert pick([node("n", pct=pct)], max_window_pct=40)["waiting"]["code"] == "window", "so an unreadable number is not full and not empty either"
    assert pick([node("edge0", pct=0.0)])["ranked"][0]["headroom"] == 100.0 and pick([node("edge100", pct=100.0)])["waiting"]["code"] == "window", "0 and 100 are readings"


@pytest.mark.parametrize("via", ["page", "mcp"])
@pytest.mark.parametrize("subject", ["issue", "epic"])
def test_run_loop_and_epic_run_cannot_claim_untrusted_edit_history(w, monkeypatch, via, subject):
    from app import main
    ck = Clock(time.time())
    monkeypatch.setattr(nodes_route, "clock", ck)
    monkeypatch.setattr(nodes_run, "sleep", ck.sleep)
    monkeypatch.setattr(nodes_run.Runner, "ensure", lambda self: False)
    hub = Hub()
    hub.put(node(w.h, name="node-b", ok=ck.t, cap=3, free=3, pct=12.0))
    monkeypatch.setattr(main, "_hub", lambda: hub)
    monkeypatch.setattr(main, "node_runner", None)
    for n in ([147] if subject == "epic" else [201, 202, 203, 204]):
        w.gh.edit_issue(f"{REPO}#{n}", by="mallory")
        w.gh.edit_issue(f"{REPO}#{n}", by="alice")
    with w.a.enter():
        runner = main._runner()
        if via == "mcp":
            monkeypatch.setattr(settings, "mcp_nodes", "tasks")
            monkeypatch.setattr(nodes_run, "_caller", lambda request: ("local-token", {"scopes": ["tasks"]}))
            nodes_epics.save_settings(w.a.db, "acme", "shop", 147, {"mcp_run": True})
            result = nodes_run.agent_run(runner, None, "acme", "shop", 147)
            assert result["assigned"] == []
        else:
            runner.start("acme", "shop", 147, "person", via="page")
            runner.tick()
        view = runner.view("acme", "shop", 147)
    assert view["assigned"] == {}
    assert view["held"]
    assert all(h["code"] == ("epic_edit_not_allowed" if subject == "epic" else "edit_not_allowed") for h in view["held"].values())
    assert w.gh.writes() == [] and w.b.db.tasks() == []
