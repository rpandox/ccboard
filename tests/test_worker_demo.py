"""Worker mode, issue #157: the demo (`?demo=1&worker=<name>`) speaks the real shape and the real words of the worker.

demo/worker.json answers GET /api/epic-worker in the demo. These tests work each state out with the real model (app/nodes_worker.py Worker.view) on a fake key-value store and hold the fixture to
the same keys, the same sentences and the same words, so the demo cannot drift from the board. Temp objects only: no gh, no network, no real home, no thread.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import github_write, nodes_epics, nodes_route, nodes_work, nodes_worker
from app.config import settings

DEMO = Path(__file__).resolve().parents[1] / "app" / "static" / "demo"
FX = json.loads((DEMO / "worker.json").read_text(encoding="utf-8"))
EPOCH = float(FX["demo"]["epoch"])
KINDS = {"start", "skip", "refused", "cap", "rest", "wait", "park", "stop", "resume", "error"}
FLAGS = {"working", "idle", "waiting", "paused", "pausedday", "resting", "stopped", "refused", "refusedwrites", "refusedauthors"}


class KV:
    def __init__(self):
        self.d = {}

    def kv_get(self, k):
        return self.d.get(k)

    def kv_set(self, k, v):
        self.d[k] = json.loads(json.dumps(v))


def composed(name: str) -> dict:
    """base + the state's patch + its lead decisions + the shared ones, as nodes-worker.js demoView puts them together."""
    patch = FX["states"][name]
    rest = {k: v for k, v in patch.items() if k != "lead"}
    v = {**FX["base"], **rest}
    v["decisions"] = patch["decisions"] if "decisions" in patch else [*patch.get("lead", []), *FX["decisions"]][:nodes_worker.SHOWN]
    return v


def real(monkeypatch, name: str) -> dict:
    """The real view of the same state: the settings that make it true, the state the loop would hold, the row, the clock of the fixture."""
    monkeypatch.setattr(nodes_worker, "clock", lambda: EPOCH)
    monkeypatch.setattr(nodes_route, "clock", lambda: EPOCH)
    patch = FX["states"][name]
    monkeypatch.setattr(settings, "epic_worker", True)
    monkeypatch.setattr(settings, "epic_repos", tuple(patch.get("repos", FX["base"]["repos"])))
    monkeypatch.setattr(settings, "epic_authors", ("alice",))
    monkeypatch.setattr(settings, "epic_worker_poll", FX["base"]["poll_s"])
    monkeypatch.setattr(github_write, "writes_enabled", lambda: name != "refusedwrites")
    monkeypatch.setattr(nodes_epics, "allowed_authors", lambda login=None: set() if name == "refusedauthors" else {"alice"})
    db = KV()
    w = nodes_worker.Worker(db, repos_fn=lambda: [])
    w._probed = True                                      # the thread has asked gh for the login: an empty author list is then a refusal, not "still waiting to learn it"
    w.cycle_at = EPOCH + 60 * 0 if patch.get("cycle_at", FX["base"]["cycle_at"]) else None
    w.next_at = None
    w.state = {"state": patch.get("state", FX["base"]["state"]), "reason": patch.get("reason"), "until": (datetime.strptime(patch["until"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() if patch.get("until") else None)}
    monkeypatch.setattr(w, "_active_mine", lambda: list(patch.get("issues", [])))
    monkeypatch.setattr(w, "running", lambda: patch.get("running", FX["base"]["running"]))          # a thread is not started here
    if name == "stopped":
        row = nodes_worker._load(db)
        row["stopped"], row["stopped_at"] = True, EPOCH - 240
        nodes_worker._save(db, row)
    return w.view()


def test_the_fixture_has_a_patch_for_every_flag_and_an_off_view():
    assert set(FX["states"]) == FLAGS
    assert FX["off"] == nodes_worker.off_view()
    assert len(FX["decisions"]) == nodes_worker.SHOWN, "the list of 20 the page shows"


@pytest.mark.parametrize("name", sorted(FLAGS))
def test_each_state_has_the_keys_and_the_sentence_of_the_real_view(monkeypatch, name):
    mine, truth = composed(name), real(monkeypatch, name)
    assert set(mine) == set(truth), (set(mine) ^ set(truth))
    for k in ("on", "state", "reason", "text", "until", "issues", "repos", "authors", "poll_s", "running"):
        assert mine[k] == truth[k], (name, k, mine[k], truth[k])
    assert set(mine["caps"]) == set(truth["caps"]) and set(mine["caps"]["hour"]) == set(truth["caps"]["hour"])
    assert set(mine["skipped"].values()) <= set(nodes_worker.SKIP_TEXT)


def test_every_decision_is_a_real_kind_with_a_real_reason_and_its_own_sentence():
    seen = set()
    for d in [*FX["decisions"], *[x for p in FX["states"].values() for x in p.get("lead", [])]]:
        assert set(d) == {"at", "kind", "issue", "reason", "text"}
        assert d["kind"] in KINDS
        assert d["text"] == nodes_worker.reason_text(d["reason"]) and d["text"] != d["reason"], d
        assert d["issue"] is None or d["issue"].startswith("example/website#")
        seen.add(d["kind"])
    assert seen == KINDS, "the demo shows all ten kinds"
    refused = {d["reason"] for d in FX["decisions"] if d["kind"] == "refused"}
    assert refused <= set(nodes_work.REASONS), "a refused start carries the work route's own words"


def test_the_fixture_is_public_safe():
    text = json.dumps(FX)
    home = "/Us" + "ers/"           # built, so this file does not hold a home path itself
    assert "@" not in text and home not in text and ".ts.net" not in text
    assert "—" not in text and "–" not in text, "no em-dash in text a person reads"
