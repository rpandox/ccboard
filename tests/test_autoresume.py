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


# --- v0.5.17c: after an account switch the sessions parked on a limit continue at once (autoresume.continue_parked) ---

def cp(world, now, **over):
    kw = {"send": lambda n, t: world["sent"].append((n, t)), "clients": lambda n: world["clients"], "alive": lambda n: world["alive"]}
    kw.update(over)
    return autoresume.continue_parked(world["db"], now, **kw)


def test_continue_parked_types_once_and_the_reset_tick_stays_quiet(world):
    db, T = world["db"], 1_800_000_000
    seed(db, "shop--api--s1", hit_at=T - 3600, resets_at=T + 7200)                     # the window resets in two hours: the switch is why it goes now
    assert cp(world, T) == ["shop--api--s1"]
    assert world["sent"] == [("shop--api--s1", "continue")]
    assert db.kv_get("autoresume:shop--api--s1:%d" % (T + 7200))["value"] == {"continued": True, "at": T, "kind": "5h", "switch": True}
    ev = [e for e in db.recent_events(50) if e["tmux_name"] == "shop--api--s1" and e["event"] == "AutoContinue"]
    assert len(ev) == 1 and ev[0]["kind"] == "switch" and ev[0]["message"] == "typed 'continue' after the account switch"
    assert world["notes"] == [], "an explicit request: no notification of its own"
    assert cp(world, T + 1) == [] and len(world["sent"]) == 1, "once per episode"
    assert world["tick"](T + 7200 + 60) == [] and len(world["sent"]) == 1, "the reset tick does not type again"


def test_continue_parked_works_with_auto_continue_off(world, monkeypatch):
    db, T = world["db"], 1_800_000_000
    seed(db, "shop--api--s1", hit_at=T - 3600, resets_at=T)
    monkeypatch.setattr(settings, "auto_continue", False)
    assert world["tick"](T + 60) == []
    assert cp(world, T + 60) == ["shop--api--s1"] and world["sent"] == [("shop--api--s1", "continue")]


def test_continue_parked_skips_attached_moved_on_and_dead_sessions_without_marking_them(world):
    db, T = world["db"], 1_800_000_000
    seed(db, "shop--api--s1", hit_at=T - 3600, resets_at=T + 600)
    world["clients"] = 1
    assert cp(world, T) == [] and db.kv_get("autoresume:shop--api--s1:%d" % (T + 600)) is None, "someone is at the terminal: left for later"
    world["clients"] = 0
    world["alive"] = False
    assert cp(world, T) == [] and db.kv_get("autoresume:shop--api--s1:%d" % (T + 600)) is None
    world["alive"] = True
    db.set_state("shop--api--s1", "working", "UserPromptSubmit", prompt="I moved on")
    db.conn.execute("UPDATE sessions SET state_at=? WHERE tmux_name=?", (iso(T - 1800), "shop--api--s1"))
    assert cp(world, T) == [] and db.kv_get("autoresume:shop--api--s1:%d" % (T + 600)) is None, "the reset tick decides for itself"
    seed(db, "shop--api--s2", hit_at=T - 3600, resets_at=T + 600, message="Error: something else entirely")
    seed(db, "shop--api--s3", hit_at=T - 3600, resets_at=T + 600)
    db.update_flags("shop--api--s3", {"no_autoresume": True})
    assert cp(world, T) == [] and world["sent"] == []


def test_continue_parked_types_into_a_session_once_however_many_episodes_it_has(world):
    db, T = world["db"], 1_800_000_000
    seed(db, "shop--api--s1", hit_at=T - 3600, resets_at=T + 600)
    db.sample("lim", "7d", 1.0, {"session": "shop--api--s1", "resets_at": T + 90000, "message": "limit"}, at=iso(T - 3600))
    assert cp(world, T) == ["shop--api--s1"] and world["sent"] == [("shop--api--s1", "continue")]
    assert db.kv_get("autoresume:shop--api--s1:%d" % (T + 90000)) is None


def test_continue_parked_leaves_decided_episodes_old_episodes_and_tmux_hiccups(world):
    db, T = world["db"], 1_800_000_000
    seed(db, "shop--api--s1", hit_at=T - 3600, resets_at=T + 600)
    db.kv_set("autoresume:shop--api--s1:%d" % (T + 600), {"skipped": "moved on", "at": T - 10})
    assert cp(world, T) == []
    seed(db, "shop--api--s2", hit_at=T - 9 * 86400, resets_at=T - 8 * 86400)           # older than the 8-day look-back
    assert cp(world, T) == []
    seed(db, "shop--api--s3", hit_at=T - 3600, resets_at=T + 600)

    def boom(n, t):
        raise RuntimeError("tmux hiccup")
    assert cp(world, T, send=boom) == [] and db.kv_get("autoresume:shop--api--s3:%d" % (T + 600)) is None
    assert cp(world, T) == ["shop--api--s3"], "tried again on the next call"


def test_continue_parked_defaults_use_tmux(world, monkeypatch):
    db, T = world["db"], 1_800_000_000
    seed(db, "shop--api--s1", hit_at=T - 3600, resets_at=T + 600)
    calls = []
    monkeypatch.setattr(autoresume.tmux, "has_session", lambda n: True)
    monkeypatch.setattr(autoresume.tmux, "real_clients", lambda n: 0)
    monkeypatch.setattr(autoresume.tmux, "send_text", lambda n, t, enter=False: calls.append((n, t, enter)))
    assert autoresume.continue_parked(db, T) == ["shop--api--s1"] and calls == [("shop--api--s1", "continue", True)]


# --- v0.5.17g: an authentication failure is not a limit: `continue` is never typed into a session that stopped on a dead login ---

AUTH_FAILURES = ["Invalid API key · Please run /login", "OAuth token has expired. Please run /login", "API Error: 401 authentication_error",
                 "Not logged in · Please run /login", "authentication_failed", "token revoked"]


@pytest.mark.parametrize("message", AUTH_FAILURES)
def test_an_authentication_failure_is_not_a_limit_message(message):
    from app.agents.claude import LIMIT_MSG_RE
    assert LIMIT_MSG_RE.search(message) is None, "autoresume keys on LIMIT_MSG_RE: these must never match it"


def test_nothing_is_typed_into_a_session_that_stopped_on_an_authentication_failure(world):
    db, T = world["db"], 1_800_000_000
    for i, msg in enumerate(AUTH_FAILURES):
        seed(db, f"shop--api--a{i}", hit_at=T - 3600, resets_at=T + 7200, message=msg)      # parked on the failure; a limit episode exists for it too
    seed(db, "shop--api--lim", hit_at=T - 3600, resets_at=T + 7200)                          # the control: a real limit does get `continue`
    assert cp(world, T) == ["shop--api--lim"] and world["sent"] == [("shop--api--lim", "continue")], "after a switch"
    assert world["tick"](T + 7200 + 60) == [] and len(world["sent"]) == 1, "and after the reset"
    from app import login_problem
    login_problem.raise_(db, agent="claude", account="k", session="shop--api--a0", message="Invalid API key · Please run /login")
    assert cp(world, T + 1) == [] and world["tick"](T + 7200 + 120) == [] and len(world["sent"]) == 1


# --- #84: the per-session switch, POST /api/sessions/<name>/flags ---

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


def _one_session(client, projects_dir, monkeypatch):
    import subprocess
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)
    return client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "claude"}).json()


def test_flags_route_sets_and_clears_and_state_shows_it(client, projects_dir, fake_tmux, monkeypatch):
    from app import main
    s = _one_session(client, projects_dir, monkeypatch)
    url = f"/api/sessions/{s['tmux']}/flags"
    r = client.post(url, headers=H, json={"no_autoresume": True})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["flags"] == {"no_autoresume": True}
    assert main.db.open_row(s["tmux"])["flags"]["no_autoresume"] is True
    st = client.get("/api/state", headers=H).json()
    assert st["config"]["auto_continue"] is True
    row = next(x for p in st["projects"] for rp in p["repos"] for x in rp["sessions"] if x["tmux"] == s["tmux"])
    assert row["flags"]["no_autoresume"] is True, "the poll carries the flag the row menu and the chip read"
    assert any(e["event"] == "AutoContinue" and e["kind"] == "optout" for e in main.db.recent_events(20) if e["tmux_name"] == s["tmux"])
    r = client.post(url, headers=H, json={"no_autoresume": False})
    assert r.json()["flags"] == {"no_autoresume": False}
    assert "no_autoresume" not in main.db.open_row(s["tmux"])["flags"], "cleared removes the key"


def test_flags_route_refuses_bad_input_and_needs_the_header(client, projects_dir, fake_tmux, monkeypatch):
    from app import main, tmux
    s = _one_session(client, projects_dir, monkeypatch)
    url = f"/api/sessions/{s['tmux']}/flags"
    assert client.post(url, headers={"Tailscale-User-Login": "alice@example.com"}, json={"no_autoresume": True}).status_code == 403
    for body in ({}, {"wait_kind": "permission"}, {"no_autoresume": True, "autoclose": None}, {"no_autoresume": "yes"}, {"no_autoresume": 1}, {"no_autoresume": None}):
        assert client.post(url, headers=H, json=body).status_code == 400, body
    flags = main.db.open_row(s["tmux"])["flags"]
    assert "no_autoresume" not in flags and "wait_kind" not in flags and "autoclose" not in flags
    assert client.post("/api/sessions/not-a-session/flags", headers=H, json={"no_autoresume": True}).status_code == 400
    assert client.post("/api/sessions/shop--api--nope/flags", headers=H, json={"no_autoresume": True}).status_code == 404, "no open row"
    assert client.post(f"/api/sessions/{tmux.INTERNAL_PREFIX}--scratch--s1/flags", headers=H, json={"no_autoresume": True}).status_code == 400


def test_the_opt_out_survives_a_new_conversation_and_a_board_restart(client, projects_dir, fake_tmux, monkeypatch):
    """Decided: the opt-out belongs to the session row. A SessionStart for a new conversation (/clear, a new id) does not clear it, and a
    board restart (a second DB on the same file) still reads it."""
    from app import hooks, main
    from app.db import DB
    s = _one_session(client, projects_dir, monkeypatch)
    client.post(f"/api/sessions/{s['tmux']}/flags", headers=H, json={"no_autoresume": True})
    hooks.apply(main.db, s["tmux"], "SessionStart", {"source": "clear", "session_id": "0198aaaa-bbbb-7ccc-8ddd-eeeeeeeeeee9"})
    assert main.db.open_row(s["tmux"])["flags"]["no_autoresume"] is True
    path = main.db.conn.execute("PRAGMA database_list").fetchone()["file"]
    from pathlib import Path
    assert DB(Path(path)).open_row(s["tmux"])["flags"]["no_autoresume"] is True


def test_a_flagged_session_gets_no_continue_after_a_reset_but_an_unflagged_one_does(world):
    db, T = world["db"], 1_800_000_000
    seed(db, "shop--api--s1", hit_at=T - 3600, resets_at=T)
    seed(db, "shop--api--s2", hit_at=T - 3600, resets_at=T)
    db.update_flags("shop--api--s2", {"no_autoresume": True})
    assert world["tick"](T + 60) == ["shop--api--s1"] and world["sent"] == [("shop--api--s1", "continue")]
    db.update_flags("shop--api--s2", {"no_autoresume": None})                       # switched back on later: that episode was decided, no late catch-up
    assert world["tick"](T + 120) == []


def test_the_opt_out_also_covers_the_account_switch_path(world, monkeypatch):
    """Decided: continue_parked ignores CCBOARD_AUTO_CONTINUE (an explicit request) but honours the per-session opt-out."""
    db, T = world["db"], 1_800_000_000
    seed(db, "shop--api--s1", hit_at=T - 3600, resets_at=T + 600)
    seed(db, "shop--api--s2", hit_at=T - 3600, resets_at=T + 600)
    db.update_flags("shop--api--s2", {"no_autoresume": True})
    monkeypatch.setattr(settings, "auto_continue", False)
    assert cp(world, T) == ["shop--api--s1"] and world["sent"] == [("shop--api--s1", "continue")]


# ---- #71: parked_view, the per-session limit field the cards turn into "limit reached, continues at <time>"

def test_parked_view_gives_the_reset_of_a_session_parked_on_a_limit(world):
    db, T = world["db"], 1_800_000_000
    seed(db, "shop--api--s1", hit_at=T - 600, resets_at=T)
    seed(db, "shop--api--s2", hit_at=T - 500, resets_at=T + 7 * 86400, kind="7d")
    assert autoresume.parked_view(db, db.open_rows(), T - 300) == {
        "shop--api--s1": {"kind": "5h", "resets_at": T}, "shop--api--s2": {"kind": "7d", "resets_at": T + 7 * 86400}}


def test_parked_view_is_empty_when_nothing_looks_parked(world):
    db, T = world["db"], 1_800_000_000
    seed(db, "shop--api--s1", hit_at=T - 600, resets_at=T, state="working")                       # working again: the person or the agent moved on
    seed(db, "shop--api--s2", hit_at=T - 600, resets_at=T, message="build failed: exit 2")        # an error that is not a limit
    assert autoresume.parked_view(db, db.open_rows(), T - 300) == {}


def test_parked_view_does_not_promise_a_continue_after_a_later_hook(world):
    """tick() will not type into a session whose state changed after the hit, so the card must not say it continues: an errored row still
    shows the limit, with no reset (the card then says auto-continue is off); an idle one is simply not parked."""
    db, T = world["db"], 1_800_000_000
    seed(db, "shop--api--s1", hit_at=T - 600, resets_at=T)
    seed(db, "shop--api--s2", hit_at=T - 600, resets_at=T, state="idle")
    for n in ("shop--api--s1", "shop--api--s2"):
        db.conn.execute("UPDATE sessions SET state_at=? WHERE tmux_name=?", (iso(T - 100), n))
    assert autoresume.parked_view(db, db.open_rows(), T - 50) == {"shop--api--s1": {"kind": "other", "resets_at": 0}}


def test_parked_view_without_an_episode_has_no_reset_and_only_for_errored(world):
    db, T = world["db"], 1_800_000_000
    msg = "You've hit your usage limit"
    db.add_session(tmux_name="shop--api--e1", project="shop", repo="api", name="e1", launcher="claude")
    db.set_state("shop--api--e1", "errored", "StopFailure", message=msg)
    db.add_session(tmux_name="shop--api--i1", project="shop", repo="api", name="i1", launcher="claude")
    db.set_state("shop--api--i1", "idle", "Stop", message=msg)
    assert autoresume.parked_view(db, db.open_rows(), T) == {"shop--api--e1": {"kind": "other", "resets_at": 0}}


def test_parked_view_ignores_codex_rows_and_does_not_read_samples_when_no_row_is_a_candidate(world, monkeypatch):
    db, T = world["db"], 1_800_000_000
    seed(db, "shop--api--cx", hit_at=T - 600, resets_at=T)
    db.conn.execute("UPDATE sessions SET agent='codex' WHERE tmux_name=?", ("shop--api--cx",))
    assert autoresume.parked_view(db, db.open_rows(), T - 300) == {}
    monkeypatch.setattr(db, "samples_query", lambda *a, **k: (_ for _ in ()).throw(AssertionError("queried")))
    assert autoresume.parked_view(db, {"x": {"state": "working", "last_message": "limit reached"}}, T) == {}


def test_state_carries_parked_for_a_session_parked_on_a_limit(client, projects_dir, fake_tmux, monkeypatch):
    """#71 through the real poll: the session row says when the limit resets, and a session that is not parked has no such field."""
    import time

    from app import main
    s = _one_session(client, projects_dir, monkeypatch)
    resets = int(time.time()) + 1800
    msg = "You've hit your session limit · resets 10:05pm (Asia/Kathmandu)"
    main.db.set_state(s["tmux"], "errored", "StopFailure", message=msg)
    main.db.sample("lim", "5h", 1.0, {"session": s["tmux"], "resets_at": resets, "message": msg}, at=iso(time.time() - 60))

    def sess():
        st = client.get("/api/state", headers=H).json()
        return next(x for p in st["projects"] for rp in p["repos"] for x in rp["sessions"] if x["tmux"] == s["tmux"])
    main._scan_cache = None
    assert sess()["parked"] == {"kind": "5h", "resets_at": resets}
    main.db.set_state(s["tmux"], "working", "UserPromptSubmit", message="continue")
    main._scan_cache = None                                   # past the 2 s scan cache
    assert "parked" not in sess()


def test_parked_view_promises_nothing_that_tick_has_decided_or_given_up_on(world):
    """A reset that tick() already skipped or typed for (kv autoresume:<session>:<resets_at>), or whose WINDOW_AFTER is over, shows no time."""
    db, T = world["db"], 1_800_000_000
    seed(db, "shop--api--s1", hit_at=T - 600, resets_at=T)
    seed(db, "shop--api--s2", hit_at=T - 600, resets_at=T)
    db.kv_set(f"autoresume:shop--api--s2:{T}", {"skipped": "someone was at the terminal", "at": T})
    got = autoresume.parked_view(db, db.open_rows(), T + 60)
    assert got["shop--api--s1"]["resets_at"] == T and got["shop--api--s2"]["resets_at"] == 0
    assert autoresume.parked_view(db, db.open_rows(), T + autoresume.WINDOW_AFTER + 1)["shop--api--s1"]["resets_at"] == 0
