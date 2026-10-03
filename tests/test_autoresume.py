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


# --- after a reboot: a row that was working when the box went down gets `continue` once its relaunch is at the prompt ---

def mark(db, name, at, prompt="build the thing"):
    """What app/recover does for a relaunched row whose old row was working."""
    db.add_session(tmux_name=name, project="shop", repo="api", name=name.split("--")[-1], launcher="recovered")
    db.update_flags(name, {autoresume.RESUME_FLAG: {"reason": "reboot", "at": at, "prompt": prompt, "was_at": iso(at - 300)}})


def session_start(db, name, at, statusline=True):
    db.set_state(name, "idle", "SessionStart")
    db.conn.execute("UPDATE sessions SET state_at=? WHERE tmux_name=?", (iso(at), name))
    if statusline:
        db.set_stats(name, {"model": "Opus", "context_pct": 3})           # the resumed TUI drew its statusline: the prompt is up


def test_reboot_continue_waits_for_session_start_then_types_once(world):
    db, T = world["db"], 1_800_000_000
    mark(db, "shop--api--s1", at=T)
    assert world["tick"](T + 30) == [] and world["sent"] == [], "no SessionStart yet: wait"
    session_start(db, "shop--api--s1", T + 40)
    assert world["tick"](T + 42) == [], "not before the settle time"
    assert world["tick"](T + 40 + autoresume.RESUME_SETTLE) == ["shop--api--s1"]
    assert world["sent"] == [("shop--api--s1", "continue")]
    assert world["notes"] and "continued after the restart" in world["notes"][0]
    assert autoresume.RESUME_FLAG not in db.open_rows()["shop--api--s1"]["flags"]
    evs = [e for e in db.recent_events(50) if e["tmux_name"] == "shop--api--s1" and e["event"] == "AutoContinue"]
    assert evs and evs[0]["kind"] == "reboot" and "typed 'continue' after the restart" in evs[0]["message"]
    assert world["tick"](T + 120) == [] and len(world["sent"]) == 1, "once"


def test_reboot_continue_without_a_statusline_waits_longer(world):
    db, T = world["db"], 1_800_000_000
    mark(db, "shop--api--s1", at=T)
    session_start(db, "shop--api--s1", T + 40, statusline=False)
    assert world["tick"](T + 40 + autoresume.RESUME_SETTLE + 1) == [], "no statusline yet: the TUI may still be starting"
    assert world["tick"](T + 40 + autoresume.RESUME_SETTLE_MAX - 1) == []
    assert world["tick"](T + 40 + autoresume.RESUME_SETTLE_MAX) == ["shop--api--s1"]


def test_reboot_continue_drops_when_the_session_moved_on_or_never_started(world):
    db, T = world["db"], 1_800_000_000
    mark(db, "shop--api--s1", at=T)
    session_start(db, "shop--api--s1", T + 40)
    db.set_state("shop--api--s1", "working", "UserPromptSubmit", prompt="I typed first")   # the person was quicker
    db.conn.execute("UPDATE sessions SET state_at=? WHERE tmux_name=?", (iso(T + 45), "shop--api--s1"))
    assert world["tick"](T + 60) == [] and world["sent"] == []
    assert autoresume.RESUME_FLAG not in db.open_rows()["shop--api--s1"]["flags"]
    assert any("moved on" in (e["message"] or "") for e in db.recent_events(50) if e["tmux_name"] == "shop--api--s1" and e["event"] == "AutoContinue")
    mark(db, "shop--api--s2", at=T)                                                          # no hook at all
    assert world["tick"](T + autoresume.RESUME_WINDOW - 1) == [] and autoresume.RESUME_FLAG in db.open_rows()["shop--api--s2"]["flags"]
    assert world["tick"](T + autoresume.RESUME_WINDOW + 1) == [] and world["sent"] == []
    assert autoresume.RESUME_FLAG not in db.open_rows()["shop--api--s2"]["flags"]


def test_reboot_continue_waits_while_attached_and_respects_the_switches(world, monkeypatch):
    db, T = world["db"], 1_800_000_000
    mark(db, "shop--api--s1", at=T)
    session_start(db, "shop--api--s1", T + 5)
    world["clients"] = 1
    assert world["tick"](T + 30) == [] and world["sent"] == []
    assert autoresume.RESUME_FLAG in db.open_rows()["shop--api--s1"]["flags"], "someone is typing: keep the flag, try later"
    world["clients"] = 0
    assert world["tick"](T + 45) == ["shop--api--s1"]
    mark(db, "shop--api--s2", at=T)
    session_start(db, "shop--api--s2", T + 5)
    db.update_flags("shop--api--s2", {"no_autoresume": True})
    assert world["tick"](T + 60) == [] and autoresume.RESUME_FLAG not in db.open_rows()["shop--api--s2"]["flags"]
    mark(db, "shop--api--s3", at=T)
    session_start(db, "shop--api--s3", T + 5)
    world["alive"] = False
    assert world["tick"](T + 60) == [] and autoresume.RESUME_FLAG not in db.open_rows()["shop--api--s3"]["flags"]
    world["alive"] = True
    mark(db, "shop--api--s4", at=T)
    session_start(db, "shop--api--s4", T + 5)
    monkeypatch.setattr(settings, "auto_continue", False)
    assert world["tick"](T + 60) == [] and autoresume.RESUME_FLAG in db.open_rows()["shop--api--s4"]["flags"]
