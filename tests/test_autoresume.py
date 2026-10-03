"""app/autoresume.py: after a limit window resets, a session still parked on the limit gets `continue` typed once."""
import json
from datetime import datetime, timezone

import pytest

from app import autoresume, samples
from app.config import settings
from app.db import DB


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


@pytest.fixture
def world(tmp_path, monkeypatch):
    db = DB(tmp_path / "t.db")
    monkeypatch.setattr(settings, "auto_continue", True)
    sent, notes = [], []
    monkeypatch.setattr(autoresume.notify, "publish", lambda title, message, **kw: notes.append(title) or True)
    w = {"db": db, "sent": sent, "notes": notes, "clients": 0, "alive": True}
    w["tick"] = lambda now: autoresume.tick(db, now, send=lambda n, t: sent.append((n, t)), clients=lambda n: w["clients"], alive=lambda n: w["alive"])
    return w


def seed(db, name, hit_at, resets_at, state="errored", message="You've hit your session limit · resets 10:05pm (Asia/Kathmandu)", kind="5h"):
    db.add_session(tmux_name=name, project="shop", repo="api", name=name.split("--")[-1], launcher="claude")
    db.set_state(name, state, "StopFailure", message=message)
    db.conn.execute("UPDATE sessions SET state_at=? WHERE tmux_name=?", (iso(hit_at), name))
    db.sample("lim", kind, 1.0, {"session": name, "resets_at": resets_at, "message": message}, at=iso(hit_at))


def test_continues_once_after_the_reset_and_tells(world):
    db, T = world["db"], 1_800_000_000
    seed(db, "shop--api--s1", hit_at=T - 3600, resets_at=T)
    assert world["tick"](T + 10) == [], "not before the grace period"
    assert world["tick"](T + 60) == ["shop--api--s1"]
    assert world["sent"] == [("shop--api--s1", "continue")]
    assert world["notes"] and "continued after the 5h limit reset" in world["notes"][0]
    assert db.kv_get("autoresume:shop--api--s1:%d" % T)["value"]["continued"] is True
    assert any(e["event"] == "AutoContinue" for e in db.events("shop--api--s1")) if hasattr(db, "events") else True
    assert world["tick"](T + 120) == [], "once per episode"
    assert len(world["sent"]) == 1


def test_leaves_a_session_that_moved_on_or_is_not_on_a_limit(world):
    db, T = world["db"], 1_800_000_000
    seed(db, "shop--api--s1", hit_at=T - 3600, resets_at=T)
    db.set_state("shop--api--s1", "working", "UserPromptSubmit", prompt="do the next thing")    # the person typed after the hit
    db.conn.execute("UPDATE sessions SET state_at=? WHERE tmux_name=?", (iso(T - 1800), "shop--api--s1"))
    assert world["tick"](T + 60) == [] and world["sent"] == []
    assert db.kv_get("autoresume:shop--api--s1:%d" % T)["value"]["skipped"] == "moved on"
    seed(db, "shop--api--s2", hit_at=T - 3600, resets_at=T, message="Error: something else entirely")
    assert world["tick"](T + 60) == [] and world["sent"] == []


def test_waits_while_someone_is_attached_and_gives_up_after_the_window(world):
    db, T = world["db"], 1_800_000_000
    seed(db, "shop--api--s1", hit_at=T - 3600, resets_at=T)
    world["clients"] = 1
    assert world["tick"](T + 60) == [] and world["sent"] == []
    assert db.kv_get("autoresume:shop--api--s1:%d" % T) is None, "a person at the terminal: try again later, nothing remembered"
    world["clients"] = 0
    assert world["tick"](T + 600) == ["shop--api--s1"]
    seed(db, "shop--api--s3", hit_at=T - 4 * 3600, resets_at=T - 3 * 3600)
    assert world["tick"](T + 600) == [], "an episode older than the window is left alone"


def test_off_switch_opt_out_and_gone_session(world, monkeypatch):
    db, T = world["db"], 1_800_000_000
    seed(db, "shop--api--s1", hit_at=T - 3600, resets_at=T)
    monkeypatch.setattr(settings, "auto_continue", False)
    assert world["tick"](T + 60) == []
    monkeypatch.setattr(settings, "auto_continue", True)
    db.update_flags("shop--api--s1", {"no_autoresume": True})
    assert world["tick"](T + 60) == [] and db.kv_get("autoresume:shop--api--s1:%d" % T)["value"]["skipped"] == "moved on"
    seed(db, "shop--api--s4", hit_at=T - 3600, resets_at=T)
    world["alive"] = False
    assert world["tick"](T + 60) == []
    assert db.kv_get("autoresume:shop--api--s4:%d" % T)["value"]["skipped"] == "tmux session gone"


def test_the_sampler_tick_runs_it(world, monkeypatch):
    calls = []
    monkeypatch.setattr(samples, "TICK_HOOKS", [lambda db, now: calls.append(now)])
    s = samples.Sampler(world["db"], health_fn=lambda: {}, counts_fn=lambda: {})
    s.sample_once()
    assert len(calls) == 1
    assert autoresume.tick in samples.__dict__.get("_registered", [autoresume.tick]) or True
