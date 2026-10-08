"""The task runtime (v0.5.14b): app/taskflow.py, the TURN_HOOKS call in hooks.apply, and the routes that sit on it (reopen, detach,
close-session, keep-open, chains, the dispatch gate).

Two layers. The Runtime tests build one over a real DB with fake callables (start_session, end_session, send_text, real_clients,
perm_pending, mem_processing) and an injected clock, and drive it with run_due(now): nothing sleeps. The route tests use the board of
test_tasks_v2 (the conftest fake tmux, a fake claude binary, hooks posted through the real /api/hook) with main.taskflow_rt set to a
Runtime that has no ticker thread, so a test says when time passes."""
import json
import shlex
import subprocess
import time
from types import SimpleNamespace

import pytest

from app import hooks, main, projects, taskflow
from app.config import settings
from app.db import DB
from app.taskflow import Runtime
from tests.test_tasks_v2 import H, _fresh_scan, backlog, board, db, make_session, post_task, tasks_in_state  # noqa: F401 (_fresh_scan and board are fixtures)


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
        return self.t


def iso(epoch):
    return taskflow._iso(epoch)


# ---------------------------------------------------------------- the Runtime over a plain DB

@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "claude_config_dir", tmp_path / "claude")
    handle = DB(tmp_path / "rt.db")
    clock = Clock()
    r = SimpleNamespace(db=handle, clock=clock, started=[], ended=[], sent=[], clients={}, perms=set(), mem=[False], start_error=[None])

    def start_session(task, *, prompt=None, auto_close=None):
        if r.start_error[0]:
            raise r.start_error[0]
        name = f"shop--api--t-{task['slug']}"
        rid = handle.add_session(tmux_name=name, project="shop", repo="api", name=f"t-{task['slug']}", launcher="task", cmd="x",
                                 agent=task.get("agent") or "claude")
        handle.task_update(task["id"], phase="running", tmux_name=name, session_row=rid, assigned_at=taskflow.db_now())
        r.started.append({"task": task["id"], "prompt": prompt, "auto_close": auto_close, "agent": task.get("agent"), "tmux": name})
        return {"id": task["id"], "tmux": name}

    def end_session(name, reason):
        r.ended.append((name, reason))
        handle.update_flags(name, {"autoclose": None})
        handle.end(name, reason)
        return True

    r.rt = Runtime(handle, start_session=start_session, end_session=end_session, send_text=lambda n, t: r.sent.append((n, t)),
                   real_clients=lambda n: r.clients.get(n, 0), perm_pending=lambda n: n in r.perms, mem_processing=lambda: r.mem[0],
                   clock=clock, grace=45.0)
    return r


def session(rig, slug="a", agent="claude", state="working", launcher="task", flags=None):
    name = f"shop--api--t-{slug}"
    rid = rig.db.add_session(tmux_name=name, project="shop", repo="api", name=f"t-{slug}", launcher=launcher, cmd="x", agent=agent, flags=flags)
    rig.db.set_state(name, state, "test")
    return name, rid


def task(rig, rid=None, name="", slug="a", title="Task A", phase="running", auto_close=1, **kw):
    return rig.db.task_add(project="shop", repo="api", slug=slug, title=title, prompt=kw.pop("prompt", "do it"), tmux_name=name,
                           session_row=rid, phase=phase, auto_close=auto_close, **kw)


def stop(rig, name, text="All done.", event_state="done"):
    """What hooks.apply does for a Stop: the state is written first, then the turn hook sees the row as it was before."""
    row = rig.db.open_row(name)
    rig.db.set_state(name, event_state, "Stop", message=(text or "")[:300])
    return rig.rt.on_turn_end(name, row, text)


def flag(rig, name, key):
    return (rig.db.open_row(name)["flags"]).get(key)


def tick(rig, seconds=0):
    return rig.rt.run_due(rig.clock.advance(seconds))


# ---- a turn ends

def test_a_stop_stores_the_result_and_finishes_the_newest_running_task_of_the_row(rig):
    name, rid = session(rig)
    old = task(rig, rid, name, slug="old", title="Old", auto_close=0)
    new = task(rig, rid, name, slug="new", title="New", auto_close=0)
    out = stop(rig, name, "  The final words.\n")
    assert out["task"] == new and out["phase"] == "done"
    row = rig.db.task_get(new)
    assert (row["phase"], row["result"]) == ("done", "The final words.") and row["result_at"] and row["done_at"]
    assert rig.db.task_get(old)["phase"] == "running", "one Stop is one turn: only the active task finishes"
    assert stop(rig, name, "Second turn.")["task"] == old, "the next Stop finishes the next newest running task"


def test_a_stop_without_a_running_task_on_the_row_does_nothing(rig):
    name, rid = session(rig)
    assert stop(rig, name) is None
    done = task(rig, rid, name, phase="done")
    other_name, other_rid = session(rig, "b")
    task(rig, other_rid, other_name, slug="b")
    assert stop(rig, name) is None and rig.db.task_get(done)["phase"] == "done"
    assert rig.rt.on_turn_end(name, None, "x") is None and rig.rt.on_turn_end(name, {}, "x") is None


def test_the_result_is_capped_and_an_empty_stop_reads_the_transcript_tail(rig, tmp_path):
    name, rid = session(rig)
    tid = task(rig, rid, name, auto_close=0)
    stop(rig, name, "x" * 30000)
    assert len(rig.db.task_get(tid)["result"]) == 20000
    # no last_assistant_message: the newest assistant entry with text in the transcript, thinking and tool calls skipped
    tid2 = task(rig, rid, name, slug="two", title="Two", auto_close=0)
    tp = tmp_path / "claude" / "projects" / "p" / "11111111-1111-4111-8111-111111111111.jsonl"
    tp.parent.mkdir(parents=True)
    lines = [{"type": "user", "message": {"role": "user", "content": "go"}},
             {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "earlier text"}]}},
             {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "The real ending."},
                                                                               {"type": "text", "text": "Second paragraph?"}]}},
             {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash"}]}}]
    tp.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    rig.db.update_flags(name, {"transcript_path": str(tp)})
    stop(rig, name, "")
    assert rig.db.task_get(tid2)["result"] == "The real ending.\nSecond paragraph?"
    tid3 = task(rig, rid, name, slug="three", title="Three", auto_close=0)
    rig.db.update_flags(name, {"transcript_path": str(tmp_path / "elsewhere.jsonl")})        # outside the config dir: not trusted, not read
    stop(rig, name, "")
    assert rig.db.task_get(tid3)["phase"] == "done" and rig.db.task_get(tid3)["result"] is None


def test_stop_failure_fails_the_task_and_never_closes(rig):
    name, rid = session(rig)
    tid = task(rig, rid, name)
    rig.rt.pending[name] = {"task": tid, "row_id": rid, "state_at": None, "stage": "grace", "due": rig.clock() + 10, "started": rig.clock()}
    rig.db.update_flags(name, {"autoclose": {"task": tid, "due": iso(rig.clock() + 10)}})
    row = rig.db.open_row(name)
    out = rig.rt.on_turn_end(name, row, None, failed=True, message="API Error: overloaded")
    assert out == {"task": tid, "phase": "failed"}
    t = rig.db.task_get(tid)
    assert (t["phase"], t["result"]) == ("failed", "API Error: overloaded") and t["done_at"]
    assert name not in rig.rt.pending and flag(rig, name, "autoclose") is None
    tick(rig, 3600)
    assert rig.ended == [] and rig.sent == []


def test_a_rate_limit_only_parks_the_task_the_session_continues_after_the_reset(rig):
    name, rid = session(rig)
    tid = task(rig, rid, name)
    out = rig.rt.on_turn_end(name, rig.db.open_row(name), None, failed=True, message="You've hit your limit", limit={"kind": "5h"})
    assert out["parked"] and rig.db.task_get(tid)["phase"] == "running"
    stop(rig, name, "Finished after the reset.")
    assert rig.db.task_get(tid)["phase"] == "done"


def test_a_legacy_task_without_session_row_is_found_by_name_and_bound(rig):
    name, rid = session(rig, "legacy")
    tid = task(rig, None, name, slug="legacy", auto_close=0)
    assert rig.db.task_get(tid)["session_row"] is None
    assert stop(rig, name, "ok")["task"] == tid
    assert rig.db.task_get(tid)["session_row"] == rid and rig.db.task_get(tid)["phase"] == "done"
    # a session that is not a task session never claims a legacy row by its name
    name2, rid2 = session(rig, "plain", launcher="claude")
    t2 = task(rig, None, name2, slug="plain", auto_close=0)
    assert stop(rig, name2, "ok") is None and rig.db.task_get(t2)["phase"] == "running"


# ---- auto-close

def finished(rig, text="Everything is in place.", auto_close=1, **kw):
    name, rid = session(rig, **kw)
    tid = task(rig, rid, name, auto_close=auto_close)
    out = stop(rig, name, text)
    return name, rid, tid, out


def test_auto_close_waits_for_the_grace_then_sends_exit_waits_eight_seconds_then_ends_the_row_as_auto_close(rig):
    name, rid, tid, out = finished(rig)
    assert out["close"] == "pending"
    af = flag(rig, name, "autoclose")
    assert af == {"task": tid, "due": iso(rig.clock() + 45)}
    tick(rig, 44)
    assert rig.sent == [] and rig.ended == [], "not before the grace"
    res = tick(rig, 1)
    assert rig.sent == [(name, "/exit")] and res["exit_sent"] == [name] and rig.ended == [], "the agent's own exit command first"
    assert flag(rig, name, "autoclose") == {"task": tid, "closing": True}
    tick(rig, 7)
    assert rig.ended == []
    res = tick(rig, 1)
    assert rig.ended == [(name, "auto_close")] and res["closed"] == [name]
    assert rig.db.open_row(name) is None and name not in rig.rt.pending
    ev = [e for e in rig.db.recent_events(10) if e["event"] == "AutoClose"]
    assert ev and ev[0]["tmux_name"] == name
    assert rig.db.task_get(tid)["phase"] == "done", "closing the session does not touch the task"


def test_codex_sessions_get_their_own_exit_command(rig):
    name, rid, tid, out = finished(rig, agent="codex")
    tick(rig, 45)
    assert rig.sent == [(name, "/quit")]


def test_auto_close_is_off_unless_the_task_asked_for_it(rig):
    name, rid, tid, out = finished(rig, auto_close=0)
    assert out["close"] is None and name not in rig.rt.pending and flag(rig, name, "autoclose") is None
    tick(rig, 3600)
    assert rig.sent == [] and rig.ended == []


@pytest.mark.parametrize("event,norm", [
    ("UserPromptSubmit", None), ("PermissionRequest", None), ("SubagentStart", None), ("PostToolBatch", None), ("Interrupt", None),
    ("PreCompact", None), ("Notification", SimpleNamespace(flags={"wait_kind": "permission"})),
    ("Notification", SimpleNamespace(flags={"wait_kind": "elicitation"}))])
def test_activity_cancels_the_countdown(rig, event, norm):
    name, rid, tid, out = finished(rig)
    row = rig.db.open_row(name)
    assert rig.rt.on_activity(name, event, row, norm) is True
    assert name not in rig.rt.pending and flag(rig, name, "autoclose") is None
    tick(rig, 3600)
    assert rig.sent == [] and rig.ended == []


@pytest.mark.parametrize("event,norm", [("Notification", SimpleNamespace(flags={"wait_kind": "idle"})), ("SessionStart", None), ("Stop", None),
                                         ("statusline", None), ("SubagentStop", None)])
def test_other_events_leave_the_countdown_alone(rig, event, norm):
    name, rid, tid, out = finished(rig)
    assert rig.rt.on_activity(name, event, rig.db.open_row(name), norm) is False
    assert name in rig.rt.pending and flag(rig, name, "autoclose")["task"] == tid


def test_a_session_that_moved_on_is_never_closed(rig):
    for how in ("state", "row", "permission", "task"):
        name, rid, tid, out = finished(rig)
        if how == "state":
            rig.db.set_state(name, "working", "UserPromptSubmit")            # a hook the runtime did not hear about
        elif how == "row":
            rig.db.end(name, "killed")
            session(rig, slug="a")                                            # the same tmux name, a new row
        elif how == "permission":
            rig.perms.add(name)
        else:
            rig.db.task_update(tid, auto_close=0)
        tick(rig, 60)
        assert rig.sent == [] and rig.ended == [], how
        assert name not in rig.rt.pending, how
        rig.perms.clear()
        rig.db.end(name, "killed")


@pytest.mark.parametrize("guard", ["clients", "subagents", "compacting"])
def test_a_busy_guard_defers_the_close_and_the_countdown_follows(rig, guard):
    name, rid, tid, out = finished(rig)
    if guard == "clients":
        rig.clients[name] = 1
    elif guard == "subagents":
        rig.db.update_flags(name, None, {"subagents": 2})
    else:
        rig.db.update_flags(name, {"compacting": True})
    tick(rig, 45)
    assert rig.sent == [] and name in rig.rt.pending
    af = flag(rig, name, "autoclose")
    assert af["task"] == tid and af["due"] == iso(rig.clock() + 15) and af["waiting"], "the card's countdown moves to the next look"
    tick(rig, 15)
    assert rig.sent == []
    rig.clients.clear()
    rig.db.update_flags(name, {"compacting": None, "subagents": None})
    tick(rig, 15)
    assert rig.sent == [(name, "/exit")], "the first look with nothing in the way closes it"


def test_a_session_somebody_keeps_open_is_given_up_after_ten_minutes_with_a_reason(rig):
    name, rid, tid, out = finished(rig)
    rig.clients[name] = 1
    tick(rig, 45)
    for _ in range(50):
        tick(rig, 15)
    assert rig.sent == [] and rig.ended == [] and name not in rig.rt.pending
    assert flag(rig, name, "autoclose") is None
    assert flag(rig, name, "autoclose_skipped")["reason"] == "someone is at the terminal"
    assert [e for e in rig.db.recent_events(10) if e["event"] == "AutoCloseSkipped"]


def test_a_busy_claude_mem_is_waited_for_up_to_a_minute_then_the_close_goes_ahead(rig):
    name, rid, tid, out = finished(rig)
    rig.mem[0] = True
    tick(rig, 45)
    assert rig.sent == []
    for _ in range(11):
        tick(rig, 5)
    assert rig.sent == [], "still within the 60 s"
    for _ in range(3):
        tick(rig, 5)
    assert rig.sent == [(name, "/exit")], "past 60 s it closes anyway"


def test_a_claude_mem_that_goes_quiet_releases_the_close_at_the_next_look(rig):
    name, rid, tid, out = finished(rig)
    rig.mem[0] = True
    tick(rig, 45)
    tick(rig, 5)
    assert rig.sent == []
    rig.mem[0] = False
    tick(rig, 5)
    assert rig.sent == [(name, "/exit")]


@pytest.mark.parametrize("answer,busy", [
    ((200, {"isProcessing": True, "queueDepth": 435, "parkedSessions": 1}), True),
    ((200, {"isProcessing": False, "queueDepth": 435, "parkedSessions": 1}), False),     # a parked backlog is not work in progress
    ((200, {"isProcessing": "yes"}), False), ((200, None), False), ((503, {"isProcessing": True}), False)])
def test_taskflow_reads_claude_mems_processing_status(monkeypatch, answer, busy):
    from app import memory
    seen = []
    monkeypatch.setattr(settings, "claude_mem", True)
    monkeypatch.setattr(memory, "worker_base", lambda: "http://127.0.0.1:37700")
    monkeypatch.setattr(memory, "fetch", lambda base, path, timeout=memory.TIMEOUT: seen.append((base, path, timeout)) or answer)
    assert taskflow.mem_processing() is busy
    assert seen == [("http://127.0.0.1:37700", "/api/processing-status", 2.0)], "one GET with the 2 s timeout"


def test_an_absent_or_unreachable_claude_mem_never_holds_a_close(monkeypatch):
    from app import memory
    monkeypatch.setattr(settings, "claude_mem", True)
    monkeypatch.setattr(memory, "worker_base", lambda: "http://127.0.0.1:37700")

    def refuse(base, path, timeout=2.0):
        raise memory.Refused("ConnectionRefusedError")
    monkeypatch.setattr(memory, "fetch", refuse)
    assert taskflow.mem_processing() is False
    monkeypatch.setattr(memory, "worker_base", lambda: (_ for _ in ()).throw(memory.NotLoopback("elsewhere")))
    assert taskflow.mem_processing() is False
    monkeypatch.setattr(settings, "claude_mem", False)
    monkeypatch.setattr(memory, "fetch", lambda *a, **k: pytest.fail("claude-mem is off: nothing to ask"))
    assert taskflow.mem_processing() is False


@pytest.mark.parametrize("text,held", [
    ("I changed the schema. Should I also migrate the old rows?", True),
    ("Done.\n\nWant me to open the PR?\n\n", True),
    ("**Shall I go ahead?**", True),
    ("Which one do you prefer? (a) or (b)", False),
    ("It works? Yes. All green.", False),
    ("All done.", False)])
def test_a_final_message_that_asks_a_question_holds_the_session(rig, text, held):
    name, rid, tid, out = finished(rig, text=text)
    if held:
        assert out["close"] == "held" and name not in rig.rt.pending
        assert flag(rig, name, "autoclose") == {"task": tid, "held": "question"}
        tick(rig, 3600)
        assert rig.sent == [] and rig.ended == []
    else:
        assert out["close"] == "pending"


def test_a_question_hold_is_cleared_when_the_person_answers(rig):
    name, rid, tid, out = finished(rig, text="Proceed?")
    assert rig.rt.on_activity(name, "UserPromptSubmit", rig.db.open_row(name), None) is True
    assert flag(rig, name, "autoclose") is None


def test_several_tasks_on_one_session_close_it_only_after_the_last(rig):
    name, rid = session(rig)
    a = task(rig, rid, name, slug="a", title="A")
    b = task(rig, rid, name, slug="b", title="B")
    assert stop(rig, name, "B first.")["close"] is None, "task A is still running on this session"
    assert name not in rig.rt.pending
    out = stop(rig, name, "Then A.")
    assert out["close"] == "pending" and rig.db.task_get(a)["phase"] == "done" and rig.db.task_get(b)["phase"] == "done"
    # a queued task aimed at the same session also keeps it open
    name2, rid2 = session(rig, "c")
    c = task(rig, rid2, name2, slug="c", title="C")
    task(rig, rid2, name2, slug="d", title="D", phase="queued")
    assert stop(rig, name2, "C is done.")["close"] is None


def test_keep_open_cancels_the_countdown_and_switches_auto_close_off(rig):
    name, rid, tid, out = finished(rig)
    assert rig.rt.keep_open(tid) == {"kept": True, "held": False}
    assert name not in rig.rt.pending and flag(rig, name, "autoclose") is None and rig.db.task_get(tid)["auto_close"] == 0
    tick(rig, 3600)
    assert rig.sent == [] and rig.ended == []
    assert rig.rt.keep_open(tid) == {"kept": False, "held": False}, "idempotent"
    assert rig.rt.keep_open(9999) == {"kept": False, "held": False}


def test_keep_open_on_a_held_question_clears_the_hold(rig):
    name, rid, tid, out = finished(rig, text="Ready?")
    assert rig.rt.keep_open(tid) == {"kept": False, "held": True}
    assert flag(rig, name, "autoclose") is None and rig.db.task_get(tid)["auto_close"] == 0


def test_close_now_sends_exit_and_kills_eight_seconds_later(rig):
    name, rid = session(rig, state="done")
    tid = task(rig, rid, name, phase="done", auto_close=0)
    info = rig.rt.close_now(tid)
    assert info["name"] == name and rig.sent == [(name, "/exit")]
    assert flag(rig, name, "autoclose") == {"task": tid, "closing": True}
    tick(rig, 7)
    assert rig.ended == []
    tick(rig, 1)
    assert rig.ended == [(name, "auto_close")]
    assert rig.rt.close_now(tid) is None, "no open session left"
    assert rig.rt.close_now(424242) is None


def test_the_session_ending_on_its_own_during_the_countdown_drops_it(rig):
    name, rid, tid, out = finished(rig)
    rig.db.end(name, "killed")
    res = tick(rig, 60)
    assert res["dropped"] == [name] and rig.sent == [] and rig.ended == [] and name not in rig.rt.pending


def test_start_clears_the_countdown_flags_a_previous_process_left_behind(rig):
    name, rid = session(rig, flags={"autoclose": {"task": 1, "due": iso(rig.clock())}, "hook_seen": True})
    assert rig.rt.clear_stale_flags() == 1
    assert flag(rig, name, "autoclose") is None and flag(rig, name, "hook_seen") is True
    assert rig.rt.clear_stale_flags() == 0


def test_the_ticker_thread_starts_and_stops(rig):
    rig.rt.tick = 0.01
    rig.rt.start()
    try:
        assert rig.rt._thread.is_alive()
        name, rid, tid, out = finished(rig)
        rig.rt.grace = 0
        rig.clock.advance(100)
        deadline = time.time() + 5
        while not rig.sent and time.time() < deadline:
            time.sleep(0.02)
        assert rig.sent == [(name, "/exit")], "the thread acted on a due close by itself"
    finally:
        rig.rt.stop()
    assert not rig.rt._thread.is_alive()


# ---- chains

def chain(rig, n=3, agents=None, prompts=None, auto_close=1):
    cid = "c1"
    ids = []
    for i in range(n):
        ids.append(rig.db.task_add(project="shop", repo="api", slug=f"s{i}", title=f"Step {i + 1}", prompt=(prompts or {}).get(i, f"step {i + 1} prompt"),
                                   phase="backlog" if i == 0 else "queued", parent_id=ids[-1] if ids else None, chain_id=cid,
                                   auto_close=auto_close, agent=(agents or {}).get(i, "claude")))
    return ids


def run_step(rig, tid, text):
    """The task `tid` runs on a session (as the runtime or a person would have started it) and its turn ends with `text`."""
    t = rig.db.task_get(tid)
    name = f"shop--api--t-{t['slug']}"
    if not t["tmux_name"]:
        rid = rig.db.add_session(tmux_name=name, project="shop", repo="api", name=f"t-{t['slug']}", launcher="task", cmd="x", agent=t["agent"])
        rig.db.task_update(tid, phase="running", tmux_name=name, session_row=rid)
        rig.db.set_state(name, "working", "UserPromptSubmit")
    return stop(rig, name, text)


def test_a_chain_step_starts_when_its_parent_is_done_with_the_result_appended(rig):
    a, b, c = chain(rig)
    run_step(rig, a, "Found 3 issues: x, y, z.")
    assert [s["task"] for s in rig.started] == [b], "the stop that finished step 1 started step 2 at once"
    s = rig.started[0]
    assert s["prompt"] == "step 2 prompt\n\n---\nResult of the previous step (Step 1):\nFound 3 issues: x, y, z."
    assert s["auto_close"] is True and rig.db.task_get(c)["phase"] == "queued", "step 3 waits for step 2"
    run_step(rig, b, "Fixed x and y.")
    assert [s["task"] for s in rig.started] == [b, c]
    assert rig.started[1]["prompt"].endswith("Result of the previous step (Step 2):\nFixed x and y.")


def test_the_result_placeholder_is_replaced_wherever_it_stands(rig):
    a, b = chain(rig, 2, prompts={1: "Review this:\n{{result}}\nand then compare {{result}} with main."})
    run_step(rig, a, "THE-RESULT")
    assert rig.started[0]["prompt"] == "Review this:\nTHE-RESULT\nand then compare THE-RESULT with main."


def test_a_long_result_is_cut_to_what_a_prompt_may_hold(rig):
    long = {"title": "T", "result": "\u00a7" * 30000}
    assert taskflow.compose_prompt("p", long).count("\u00a7") == 12000
    big = "t" * 19000
    out = taskflow.compose_prompt(big, long)
    assert len(out) <= 20000 and out.startswith(big)
    out = taskflow.compose_prompt("head {{result}} tail", long)
    assert out == "head " + "\u00a7" * 12000 + " tail"
    assert len(taskflow.compose_prompt("{{result}}" + "t" * 19990, long)) <= 20000
    assert len(taskflow.compose_prompt("{{result}} and {{result}}" + "t" * 15000, long)) <= 20000
    assert taskflow.compose_prompt("p {{result}}", {"title": "T", "result": None}) == "p "


def test_a_chain_may_cross_agents_and_inherits_the_agent_of_its_row(rig):
    a, b = chain(rig, 2, agents={0: "claude", 1: "codex"})
    run_step(rig, a, "The review input.")
    assert rig.started[0]["agent"] == "codex" and rig.started[0]["task"] == b


def test_each_agents_chain_steps_wait_on_that_agents_own_window(rig):
    """v0.5.16: a Claude limit never holds a Codex step and a Codex limit never holds a Claude step."""
    now = rig.clock()
    gate = lambda agent: taskflow.limit_gate(rig.db, rig.clock(), agent)   # noqa: E731
    rig.db.kv_set("rate_limits", {"five_hour": {"used_percentage": 91, "resets_at": now + 600}, "seven_day": {"used_percentage": 10}})
    assert gate("claude")["kind"] == "5h" and gate("codex") is None
    rig.db.kv_del("rate_limits")
    rig.db.kv_set("rate_limits_codex", {"primary": {"used_percent": 88, "window_minutes": 300, "resets_at": now + 900}})
    assert gate("codex") == {"kind": "codex", "resets_at": now + 900, "pct": 88.0} and gate("claude") is None
    rig.db.kv_set("rate_limits_codex", {"primary": {"used_percent": 88, "window_minutes": 300, "resets_at": now - 1}})
    assert gate("codex") is None, "a window that already reset is open again"
    rig.db.kv_set("sched_backoff_until_codex", iso(now + 1200))
    assert gate("codex") == {"kind": "backoff", "resets_at": now + 1200, "pct": None} and gate("claude") is None
    rig.db.kv_del("sched_backoff_until_codex")
    # the runtime: a Claude -> Codex chain with the Claude window full still starts its Codex step
    a, b = chain(rig, 2, agents={0: "claude", 1: "codex"})
    rig.db.kv_set("rate_limits", {"five_hour": {"used_percentage": 95, "resets_at": now + 3600}, "seven_day": {"used_percentage": 10}})
    run_step(rig, a, "Review this.")
    assert [s["task"] for s in rig.started] == [b], "Codex has its own room"
    c, d = chain(rig, 2, agents={0: "codex", 1: "claude"})
    rig.db.kv_set("rate_limits_codex", {"primary": {"used_percent": 90, "window_minutes": 300, "resets_at": rig.clock() + 3600}})
    rig.db.kv_del("rate_limits")
    rig.started.clear()
    run_step(rig, c, "Codex result.")
    assert [s["task"] for s in rig.started] == [d], "a full Codex window does not hold a Claude step"
    e, f = chain(rig, 2, agents={0: "claude", 1: "codex"})
    rig.started.clear()
    run_step(rig, e, "Claude result.")
    assert rig.started == [] and rig.db.task_get(f)["phase"] == "queued", "the Codex step waits for the Codex window"


def test_a_failed_or_cancelled_parent_cancels_the_steps_behind_it_all_the_way_down(rig):
    a, b, c = chain(rig)
    name, rid = session(rig, "s0")
    rig.db.task_update(a, phase="running", tmux_name=name, session_row=rid)
    rig.rt.on_turn_end(name, rig.db.open_row(name), None, failed=True, message="boom")
    assert rig.db.task_get(a)["phase"] == "failed"
    assert rig.db.task_get(b)["phase"] == "cancelled" and rig.db.task_get(b)["result"] == "blocked: step failed"
    tick(rig)
    assert rig.db.task_get(c)["phase"] == "cancelled" and rig.db.task_get(c)["result"] == "blocked: step failed", "the tick carries it down"
    assert rig.started == []
    # a cancelled parent blocks too, and a deleted one
    d, e = chain(rig, 2)
    rig.db.task_update(d, phase="cancelled")
    tick(rig)
    assert rig.db.task_get(e)["phase"] == "cancelled"
    f = rig.db.task_add(project="shop", repo="api", slug="orphan", title="Orphan", prompt="p", phase="queued", parent_id=777777)
    tick(rig)
    assert rig.db.task_get(f)["phase"] == "cancelled"


def test_fan_out_starts_every_child_of_one_parent(rig):
    a, b = chain(rig, 2)
    c = rig.db.task_add(project="shop", repo="api", slug="s9", title="Other child", prompt="x {{result}}", phase="queued", parent_id=a, chain_id="c1")
    run_step(rig, a, "R")
    assert sorted(s["task"] for s in rig.started) == sorted([b, c])


def test_a_queued_task_without_a_parent_is_left_alone(rig):
    q = rig.db.task_add(project="shop", repo="api", slug="q", title="Q", prompt="p", phase="queued")
    tick(rig, 60)
    assert rig.db.task_get(q)["phase"] == "queued" and rig.started == []


def test_the_limit_gate_holds_chain_steps_until_the_window_resets_and_never_hand_dispatch(rig):
    a, b = chain(rig, 2)
    rig.db.kv_set("rate_limits", {"five_hour": {"used_percentage": 91, "resets_at": rig.clock() + 3600}, "seven_day": {"used_percentage": 10}})
    run_step(rig, a, "R")
    assert rig.started == [] and rig.db.task_get(b)["phase"] == "queued", "held, not cancelled"
    tick(rig, 1800)
    assert rig.started == []
    tick(rig, 1801)                                           # the window reset: the stale 91 % is not a hold any more
    assert [s["task"] for s in rig.started] == [b]


def test_limit_gate_reasons(rig):
    now = rig.clock()
    gate = lambda: taskflow.limit_gate(rig.db, rig.clock())   # noqa: E731
    assert gate() is None
    rig.db.kv_set("rate_limits", {"five_hour": {"used_percentage": 84.9, "resets_at": now + 100}, "seven_day": {"used_percentage": 84}})
    assert gate() is None, "just under 85 %"
    rig.db.kv_set("rate_limits", {"five_hour": {"used_percentage": 85, "resets_at": now + 100}, "seven_day": {"used_percentage": 84}})
    assert gate() == {"kind": "5h", "resets_at": now + 100, "pct": 85.0}
    rig.db.kv_set("rate_limits", {"five_hour": {"used_percentage": 97, "resets_at": now - 5}, "seven_day": {"used_percentage": 90, "resets_at": now + 500}})
    assert gate() == {"kind": "7d", "resets_at": now + 500, "pct": 90.0}, "the 5 h window already reset"
    rig.db.kv_set("rate_limits", {"five_hour": {"used_percentage": 90, "resets_at": now + 100}, "seven_day": {"used_percentage": 90, "resets_at": now + 900}})
    assert gate()["kind"] == "7d", "several holds: the one that clears last"
    rig.db.kv_del("rate_limits")
    # a limit episode that has not reset yet holds; one that has, or another account's, does not
    from app import accounts
    rig.db.sample("lim", "5h", 1, {"session": "s1", "resets_at": int(now + 300)}, at=iso(now - 60))
    assert gate() == {"kind": "limit", "resets_at": int(now + 300), "pct": None}
    assert taskflow.limit_gate(rig.db, now + 301) is None
    rig.db.kv_set(accounts.KV_CURRENT, {"key": "acct-b"})
    rig.db.sample("lim", "7d", 1, {"session": "s2", "resets_at": int(now + 9000), "acct": "acct-a"}, at=iso(now - 30))
    assert gate()["resets_at"] == int(now + 300), "acct-a's episode says nothing about acct-b's room"
    # the scheduler's back-off after a rate-limited run
    rig.db.kv_set("sched_backoff_until", iso(now + 1200))
    assert gate() == {"kind": "backoff", "resets_at": now + 1200, "pct": None}


def test_a_step_that_cannot_start_fails_with_the_reason_and_blocks_its_children(rig):
    a, b, c = chain(rig)
    rig.start_error[0] = projects.BadRequest("claude is not installed on this box")
    run_step(rig, a, "R")
    t = rig.db.task_get(b)
    assert t["phase"] == "failed" and t["result"] == "could not start: claude is not installed on this box" and t["done_at"]
    tick(rig)
    assert rig.db.task_get(c)["phase"] == "cancelled"


def test_a_tmux_hiccup_retries_the_step_later(rig):
    from app import tmux
    a, b = chain(rig, 2)
    rig.start_error[0] = tmux.TmuxError("no server")
    run_step(rig, a, "R")
    assert rig.db.task_get(b)["phase"] == "queued"
    rig.start_error[0] = None
    tick(rig, 10)
    assert rig.started == [], "not before the retry delay"
    tick(rig, 25)
    assert [s["task"] for s in rig.started] == [b]


def test_a_step_is_never_started_twice(rig):
    a, b = chain(rig, 2)
    rig.db.task_update(a, phase="done", result="R")
    assert rig.rt.advance(a) == [b]
    assert rig.rt.advance(a) == [], "it left the queue: nothing to start"
    assert len(rig.started) == 1


def test_chain_positions(rig):
    a, b, c = chain(rig)
    d = rig.db.task_add(project="shop", repo="api", slug="fan", title="Fan", prompt="p", phase="queued", parent_id=a, chain_id="c1")
    solo = rig.db.task_add(project="shop", repo="api", slug="solo", title="Solo", prompt="p")
    pos = taskflow.chain_positions(rig.db, [rig.db.task_get(c), rig.db.task_get(solo)])
    assert pos == {a: {"i": 1, "n": 3}, b: {"i": 2, "n": 3}, c: {"i": 3, "n": 3}, d: {"i": 2, "n": 3}}
    assert solo not in pos and taskflow.chain_positions(rig.db, []) == {}


# ---- the sweep

def test_the_sweep_cancels_running_tasks_whose_session_row_ended_without_a_result(rig):
    name, rid = session(rig)
    killed = task(rig, rid, name, slug="k")
    rig.db.end(name, "killed")
    alive_name, alive_rid = session(rig, "live")
    alive = task(rig, alive_rid, alive_name, slug="live")
    finished_ = task(rig, rid, name, slug="f", phase="done")
    legacy = task(rig, None, "shop--api--t-gone", slug="legacy")
    res = tick(rig)
    assert res["swept"] == [killed]
    assert rig.db.task_get(killed)["phase"] == "cancelled" and rig.db.task_get(killed)["done_at"]
    assert rig.db.task_get(alive)["phase"] == "running" and rig.db.task_get(finished_)["phase"] == "done"
    assert rig.db.task_get(legacy)["phase"] == "running", "a legacy row with no session_row is left to the board's name binding"


def test_the_sweep_also_cancels_a_task_whose_agent_quit_and_never_came_back(rig):
    rig.clock.t = time.time()                                                # state_at is stamped by the real clock
    name, rid = session(rig, "quit")
    tid = task(rig, rid, name, slug="quit")
    rig.db.set_state(name, "ended", "SessionEnd")
    assert tick(rig, 0)["swept"] == [] and rig.db.task_get(tid)["phase"] == "running", "a SessionEnd may be followed by the next session"
    rig.clock.advance(31)
    rig.db.set_state(name, "idle", "SessionStart")                          # it did come back: not swept
    assert tick(rig, 10)["swept"] == [] and rig.db.task_get(tid)["phase"] == "running"
    rig.db.conn.execute("UPDATE sessions SET state='ended', state_at=? WHERE id=?", (iso(rig.clock() - 60), rid))
    assert tick(rig, 10)["swept"] == [tid] and rig.db.task_get(tid)["phase"] == "cancelled"
    assert rig.db.open_row(name) is not None, "the row is left alone: only the task is cancelled (so it can be reopened)"


def test_a_task_handed_to_a_session_that_recovery_resumed_follows_it_to_the_new_row(rig):
    name, rid = session(rig, "s1", launcher="claude")
    tid = task(rig, rid, name, slug="handed", auto_close=0)
    rig.db.end(name, "reconciled")                                          # the reboot: the old row is over ...
    new_rid = rig.db.add_session(tmux_name=name, project="shop", repo="api", name="t-s1", launcher="recovered", cmd="claude --resume x", agent="claude")
    rig.db.set_state(name, "working", "SessionStart")                       # ... and recovery relaunched the conversation in a new row
    assert tick(rig)["swept"] == [], "not cancelled: its session came back"
    assert rig.db.task_get(tid)["session_row"] == new_rid and rig.db.task_get(tid)["phase"] == "running"
    # the same through a Stop that arrives before any sweep
    name3, rid3 = session(rig, "s3", launcher="claude")
    tid3 = task(rig, rid3, name3, slug="handed3", auto_close=0)
    rig.db.end(name3, "reconciled")
    new3 = rig.db.add_session(tmux_name=name3, project="shop", repo="api", name="t-s3", launcher="recovered", cmd="claude --resume y", agent="claude")
    rig.db.set_state(name3, "working", "SessionStart")
    assert stop(rig, name3, "Done after the reboot.")["task"] == tid3
    assert rig.db.task_get(tid3)["session_row"] == new3 and rig.db.task_get(tid3)["result"] == "Done after the reboot."
    # a session the person killed and then started again under the same name is not the task's
    name2, rid2 = session(rig, "s2", launcher="claude")
    gone = task(rig, rid2, name2, slug="gone", auto_close=0)
    rig.db.end(name2, "killed")
    rig.db.add_session(tmux_name=name2, project="shop", repo="api", name="s2", launcher="claude", cmd="claude", agent="claude")
    assert rig.db.task_get(gone)["phase"] == "running"
    assert tick(rig, 10)["swept"] == [gone] and rig.db.task_get(gone)["phase"] == "cancelled"


def test_the_sweep_runs_every_ten_seconds(rig):
    name, rid = session(rig)
    task(rig, rid, name)
    tick(rig)                                                  # the first tick sweeps
    rig.db.end(name, "killed")
    assert tick(rig, 5)["swept"] == []
    assert len(tick(rig, 5)["swept"]) == 1


# ---------------------------------------------------------------- hooks.apply -> TURN_HOOKS

def test_turn_hooks_see_every_applied_event_and_nothing_else(lite_client, projects_dir, fake_tmux):
    calls = []
    fn = lambda handle, name, event, norm, row: calls.append((name, event, getattr(norm, "result", "n/a"), (row or {}).get("row_id")))   # noqa: E731
    hooks.TURN_HOOKS.append(fn)
    try:
        h = main.db
        rid = h.add_session(tmux_name="shop--api--s1", project="shop", repo="api", name="s1", launcher="claude", cmd="claude", agent="claude")
        sid = "33333333-3333-4333-8333-333333333333"
        hooks.apply(h, "shop--api--s1", "SessionStart", {"session_id": sid})
        hooks.apply(h, "shop--api--s1", "Stop", {"session_id": sid, "last_assistant_message": "Full final text."})
        hooks.apply(h, "shop--api--s1", "PostToolBatch", {"session_id": sid})
        hooks.apply(h, "shop--api--s1", "statusline", {"model": {"display_name": "Opus 5", "id": "claude-opus-5"}})
        assert hooks.apply(h, "shop--api--s1", "Stop", {"session_id": "44444444-4444-4444-8444-444444444444",
                                                          "transcript_path": "/x/observer-sessions/a.jsonl"})["ignored"] == "foreign"
        assert hooks.apply(h, "shop--api--s1", "Stop", {"session_id": "55555555-5555-4555-8555-555555555555"}, child=True)["ignored"] == "child"
    finally:
        hooks.TURN_HOOKS.remove(fn)
    assert calls == [("shop--api--s1", "SessionStart", None, rid), ("shop--api--s1", "Stop", "Full final text.", rid),
                     ("shop--api--s1", "PostToolBatch", "n/a", rid)]


def test_a_failing_turn_hook_never_breaks_the_hook_path(lite_client, projects_dir, fake_tmux):
    def boom(*a):
        raise RuntimeError("boom")
    hooks.TURN_HOOKS.append(boom)
    try:
        h = main.db
        h.add_session(tmux_name="shop--api--s1", project="shop", repo="api", name="s1", launcher="claude", cmd="claude", agent="claude")
        assert hooks.apply(h, "shop--api--s1", "UserPromptSubmit", {"prompt": "hi"})["state"] == "working"
    finally:
        hooks.TURN_HOOKS.remove(boom)


def test_main_registers_one_turn_hook_and_a_reimport_does_not_double_it():
    assert [f for f in hooks.TURN_HOOKS if f.__qualname__ == "_taskflow_turn_hook"] == [main._taskflow_turn_hook]
    main._register_turn_hook()
    assert len([f for f in hooks.TURN_HOOKS if f.__qualname__ == "_taskflow_turn_hook"]) == 1


# ---------------------------------------------------------------- the board: the whole path through /api/hook

@pytest.fixture
def flow(board, monkeypatch):
    """The board plus a Runtime on a fake clock bound to the board's DB (main.taskflow_rt), no ticker thread."""
    clock = Clock()
    rt = Runtime(main.db, start_session=main._taskflow_start, end_session=main._end_session, perm_pending=main._permission_pending,
                 mem_processing=lambda: False, clock=clock, grace=45.0)
    monkeypatch.setattr(main, "taskflow_rt", rt)
    board.rt, board.clock = rt, clock
    return board


def hook(board, tmux_name, event, **payload):
    row = db().open_row(tmux_name)
    body = {"hook_event_name": event, **payload}
    if row and row.get("claude_session_id") and "session_id" not in body:
        body["session_id"] = row["claude_session_id"]
    r = board.client.post("/api/hook", headers={"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard-Session": tmux_name},
                          content=json.dumps(body))
    assert r.status_code == 200, r.text
    return r.json()


def dispatch(board, tid, **body):
    r = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"mode": "lane", **body})
    assert r.status_code == 200, r.text
    return r.json()


def test_the_whole_lane_path_prompt_stop_result_countdown_exit_close_and_closed_at(flow):
    tid = backlog(flow)
    out = dispatch(flow, tid)
    name = out["tmux"]
    assert db().task_get(tid)["auto_close"] == 1, "a lane dispatch closes after the stop by default"
    hook(flow, name, "SessionStart", source="startup")
    hook(flow, name, "UserPromptSubmit", prompt="Add a login page with tests.")
    assert tasks_in_state(flow)[tid]["column"] == "in_progress"
    final = "I added the login page and the tests. All 12 pass."
    hook(flow, name, "Stop", last_assistant_message=final)
    row = db().task_get(tid)
    assert (row["phase"], row["result"]) == ("done", final) and row["result_at"] and row["done_at"]
    v = tasks_in_state(flow)[tid]
    assert v["phase"] == "done" and v["column"] == "done" and v["result"] == final and v["result_at"] == row["result_at"]
    assert v["autoclose"] == {"task": tid, "due": iso(flow.clock() + 45)} and v["closed_at"] is None and v["chain"] is None and v["limit_hold"] is None
    sess = [s for p in flow.client.get("/api/state", headers=H).json()["projects"] for r in p["repos"] for s in r["sessions"] if s["tmux"] == name][0]
    assert sess["flags"]["autoclose"]["task"] == tid and "last_result" not in sess["flags"]
    flow.clock.advance(45)
    flow.rt.run_due(flow.clock())
    assert flow.tmux["texts"][-1] == (name, "/exit", True)
    assert name in flow.tmux["sessions"], "the session is still there while the agent quits"
    flow.clock.advance(8)
    assert flow.rt.run_due(flow.clock())["closed"] == [name]
    assert name not in flow.tmux["sessions"]
    ended = main.db.conn.execute("SELECT ended_reason, ended_at FROM sessions WHERE id=?", (row["session_row"],)).fetchone()
    assert ended["ended_reason"] == "auto_close"
    v = tasks_in_state(flow)[tid]
    assert v["closed_at"] == ended["ended_at"] and v["autoclose"] is None and v["column"] == "done" and v["phase"] == "done"


def test_taskflow_stamps_the_planned_close_before_the_done_notice_is_built(flow, monkeypatch):
    """Issue #37: the notice reads flags.autoclose, so the Stop path must stamp it first (a spy records what the row held when the push went out)."""
    from app import notify
    seen = []
    monkeypatch.setattr(notify, "notify_session", lambda name, state, message, kind=None: seen.append((state, db().open_row(name)["flags"].get("autoclose"))) or True)
    tid = backlog(flow)
    name = dispatch(flow, tid)["tmux"]
    hook(flow, name, "Stop", last_assistant_message="All done.")
    assert seen and seen[-1][0] == "done"
    assert seen[-1][1] == {"task": tid, "due": iso(flow.clock() + 45)}, "the close was planned before the notice was built"
    # a question holds the close: the notice sees the hold, not a due
    tid2 = backlog(flow, title="Second")
    name2 = dispatch(flow, tid2)["tmux"]
    hook(flow, name2, "Stop", last_assistant_message="Shall I also update the docs?")
    assert seen[-1][1] == {"task": tid2, "held": "question"}


def test_a_prompt_typed_during_the_countdown_cancels_it_through_the_hook(flow):
    tid = backlog(flow)
    name = dispatch(flow, tid)["tmux"]
    hook(flow, name, "Stop", last_assistant_message="Done.")
    assert flow.rt.pending
    hook(flow, name, "UserPromptSubmit", prompt="one more thing")
    assert not flow.rt.pending and tasks_in_state(flow)[tid]["autoclose"] is None
    flow.clock.advance(3600)
    flow.rt.run_due(flow.clock())
    assert name in flow.tmux["sessions"] and flow.tmux["texts"] == []


def test_a_permission_request_and_a_dialog_cancel_it_too(flow):
    tid = backlog(flow)
    name = dispatch(flow, tid)["tmux"]
    hook(flow, name, "Stop", last_assistant_message="Done.")
    hook(flow, name, "Notification", notification_type="idle_prompt", message="waiting")
    assert flow.rt.pending, "the idle notification is not activity"
    hook(flow, name, "Notification", notification_type="permission_prompt", message="needs permission")
    assert not flow.rt.pending
    tid2 = backlog(flow, title="Second")
    name2 = dispatch(flow, tid2)["tmux"]
    hook(flow, name2, "Stop", last_assistant_message="Done.")
    assert flow.rt.pending
    r = flow.client.post("/api/permission", headers={"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard-Session": name2},
                         content=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": None}))
    assert r.status_code == 200
    assert not flow.rt.pending, "the PermissionRequest route cancels the countdown"


def test_a_question_keeps_the_session_and_the_card_wants_you_within_one_poll(flow):
    tid = backlog(flow)
    name = dispatch(flow, tid)["tmux"]
    hook(flow, name, "UserPromptSubmit", prompt="go")
    hook(flow, name, "Stop", last_assistant_message="I changed the schema.\nShould I also migrate the old rows?")
    v = tasks_in_state(flow)[tid]
    assert v["phase"] == "done" and v["autoclose"] == {"task": tid, "held": "question"}
    assert v["column"] == "needs_you", "derive_status says done; the hold puts the card where the question is"
    flow.clock.advance(3600)
    flow.rt.run_due(flow.clock())
    assert name in flow.tmux["sessions"] and flow.tmux["texts"] == []


def test_a_stop_failure_fails_the_task_through_the_hook_and_a_rate_limit_only_parks_it(flow):
    tid = backlog(flow)
    name = dispatch(flow, tid)["tmux"]
    hook(flow, name, "StopFailure", error="rate_limit", last_assistant_message="You've hit your session limit · resets 10:05pm (UTC)")
    assert db().task_get(tid)["phase"] == "running", "parked on the limit: autoresume continues the session"
    tid2 = backlog(flow, title="Second")
    name2 = dispatch(flow, tid2)["tmux"]
    hook(flow, name2, "StopFailure", error="server_error", last_assistant_message="API Error: 500")
    t = db().task_get(tid2)
    assert t["phase"] == "failed" and t["result"] == "API Error: 500"
    assert tasks_in_state(flow)[tid2]["column"] == "needs_you" and not flow.rt.pending


def test_a_codex_interrupt_never_finishes_or_closes_a_task(flow, fake_codex):
    subprocess.run(["git", "-C", str(flow.projects / "shop" / "api"), "-c", "user.email=t@e.x", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)
    tid = backlog(flow, agent="codex")
    name = dispatch(flow, tid)["tmux"]
    hook(flow, name, "SessionStart", source="startup", session_id="66666666-6666-4666-8666-666666666666")
    hook(flow, name, "UserPromptSubmit", prompt="go")
    out = hook(flow, name, "Interrupt")
    assert out["state"] == "idle" and out["kind"] == "interrupt"
    assert db().task_get(tid)["phase"] == "running" and not flow.rt.pending, "the turn was cancelled, not finished"
    hook(flow, name, "UserPromptSubmit", prompt="try again")
    hook(flow, name, "Stop", last_assistant_message="Now it is done.")
    assert db().task_get(tid)["phase"] == "done" and db().task_get(tid)["result"] == "Now it is done."
    assert flow.rt.pending, "a real Stop of a lane task starts the countdown for Codex too"
    flow.clock.advance(45)
    flow.rt.run_due(flow.clock())
    assert flow.tmux["texts"][-1] == (name, "/quit", True), "Codex gets its own exit command"
    flow.clock.advance(8)
    assert flow.rt.run_due(flow.clock())["closed"] == [name]


def test_a_session_dropped_task_does_not_close_the_session_by_default(flow):
    s1 = make_session(flow, state="idle")
    tid = backlog(flow)
    r = flow.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1})
    assert r.status_code == 200 and db().task_get(tid)["auto_close"] == 0
    hook(flow, s1, "SessionStart", source="startup")
    hook(flow, s1, "UserPromptSubmit", prompt="x")
    hook(flow, s1, "Stop", last_assistant_message="Handled.")
    assert db().task_get(tid)["phase"] == "done" and db().task_get(tid)["result"] == "Handled."
    assert not flow.rt.pending, "the session is the person's own"
    # asked to, a session drop closes too
    t2 = backlog(flow, title="Second")
    s2 = make_session(flow, name="s2", state="done")
    assert flow.client.post(f"/api/tasks/{t2}/dispatch", headers=H, json={"session": s2, "auto_close": True}).status_code == 200
    hook(flow, s2, "UserPromptSubmit", prompt="y")
    hook(flow, s2, "Stop", last_assistant_message="Handled too.")
    assert s2 in flow.rt.pending


def test_the_lifespan_starts_the_runtime_with_a_live_ticker(client):
    rt = main.taskflow_rt
    assert isinstance(rt, Runtime) and rt.db is main.db and rt._thread.is_alive()


# ---------------------------------------------------------------- keep-open, close-session, detach

def test_keep_open_route(flow):
    tid = backlog(flow)
    name = dispatch(flow, tid)["tmux"]
    hook(flow, name, "Stop", last_assistant_message="Done.")
    r = flow.client.post(f"/api/tasks/{tid}/keep-open", headers=H)
    assert r.status_code == 200 and r.json()["kept"] is True and r.json()["task"]["auto_close"] is False and r.json()["task"]["autoclose"] is None
    assert not flow.rt.pending and db().task_get(tid)["auto_close"] == 0
    assert flow.client.post(f"/api/tasks/{tid}/keep-open", headers=H).json()["kept"] is False
    assert flow.client.post("/api/tasks/9999/keep-open", headers=H).status_code == 404
    assert flow.client.post(f"/api/tasks/{tid}/keep-open").status_code in (401, 403), "needs an identity"


def test_close_session_route_closes_gracefully_now_and_refuses_a_busy_session(flow):
    tid = backlog(flow)
    name = dispatch(flow, tid)["tmux"]
    hook(flow, name, "UserPromptSubmit", prompt="go")
    r = flow.client.post(f"/api/tasks/{tid}/close-session", headers=H)
    assert r.status_code == 409 and r.json()["error"] == "working" and flow.tmux["texts"] == []
    hook(flow, name, "Stop", last_assistant_message="Done.")
    r = flow.client.post(f"/api/tasks/{tid}/close-session", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["closing"] is True and r.json()["tmux"] == name and r.json()["kill_at"]
    assert flow.tmux["texts"][-1] == (name, "/exit", True) and name in flow.tmux["sessions"]
    flow.clock.advance(8)
    assert flow.rt.run_due(flow.clock())["closed"] == [name]
    assert tasks_in_state(flow)[tid]["closed_at"], "'closed after stop' shows on the card"
    r = flow.client.post(f"/api/tasks/{tid}/close-session", headers=H)
    assert r.status_code == 409 and "no open session" in r.json()["error"]
    assert flow.client.post("/api/tasks/9999/close-session", headers=H).status_code == 404
    assert flow.client.post(f"/api/tasks/{backlog(flow, title='Never started')}/close-session", headers=H).status_code == 409


def test_detach_puts_a_session_task_back_in_the_backlog_and_refuses_the_rest(flow):
    s1 = make_session(flow, state="idle")
    tid = backlog(flow)
    flow.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1, "auto_close": True})
    hook(flow, s1, "UserPromptSubmit", prompt="x")
    hook(flow, s1, "Stop", last_assistant_message="Handled.")
    assert flow.rt.pending
    r = flow.client.post(f"/api/tasks/{tid}/detach", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["phase"] == "backlog" and r.json()["task"]["column"] == "backlog" and r.json()["task"]["tmux"] == ""
    row = db().task_get(tid)
    assert (row["phase"], row["tmux_name"], row["session_row"], row["mode"], row["result"], row["done_at"], row["assigned_at"]) == \
           ("backlog", "", None, "worktree", None, None, None)
    assert not flow.rt.pending and s1 in flow.tmux["sessions"], "the countdown went, the session stays"
    assert flow.client.post(f"/api/tasks/{tid}/detach", headers=H).status_code == 409, "it is not assigned any more"
    lane = post_task(flow, title="Own worktree").json()["id"]
    r = flow.client.post(f"/api/tasks/{lane}/detach", headers=H)
    assert r.status_code == 409 and "archive" in r.json()["error"]
    assert flow.client.post("/api/tasks/9999/detach", headers=H).status_code == 404
    assert flow.client.post(f"/api/tasks/{backlog(flow, title='B2')}/detach", headers=H).status_code == 409
    # the project folder that is not a git repo: its detached card runs in place again
    (flow.projects / "notes").mkdir()
    ip = post_task(flow, project="notes", repo="root", title="In place", when="later").json()["id"]
    s2 = make_session(flow, project="notes", repo="root", name="s1", state="idle")
    assert flow.client.post(f"/api/tasks/{ip}/dispatch", headers=H, json={"session": s2}).status_code == 200
    assert db().task_get(ip)["mode"] == "session"
    assert flow.client.post(f"/api/tasks/{ip}/detach", headers=H).status_code == 200
    assert db().task_get(ip)["mode"] == "attached"


# ---------------------------------------------------------------- reopen

def finish_task(flow, tid, text="Done."):
    name = db().task_get(tid)["tmux_name"]
    hook(flow, name, "UserPromptSubmit", prompt="go")
    hook(flow, name, "Stop", last_assistant_message=text)
    return name


def test_reopen_starts_a_new_session_in_the_existing_worktree(flow):
    tid = backlog(flow, model="opus")
    out = dispatch(flow, tid)
    name = out["tmux"]
    wt = flow.projects / "shop" / "api" / ".claude" / "worktrees" / "add-login-page"
    wt.mkdir(parents=True)                                       # what `claude --worktree` made
    finish_task(flow, tid, "First round.")
    r = flow.client.post(f"/api/tasks/{tid}/reopen", headers=H)
    assert r.status_code == 409 and "still open" in r.json()["error"] and r.json()["tmux"] == name, "the old session is still there"
    flow.client.post(f"/api/tasks/{tid}/close-session", headers=H)
    flow.clock.advance(8)
    flow.rt.run_due(flow.clock())
    assert name not in flow.tmux["sessions"]
    old_row = db().task_get(tid)["session_row"]
    r = flow.client.post(f"/api/tasks/{tid}/reopen", headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reopened"] == "worktree" and body["phase"] == "running" and body["tmux"] == name and body["attach_url"] == f"/term/{name}"
    assert body["session_row"] != old_row and body["task"]["column"] == "in_progress" and body["task"]["result"] is None
    created = flow.tmux["created"][-1]
    assert created[0] == name and created[1] == str(wt), "the session starts in the worktree"
    argv = shlex.split(flow.tmux["sent"][-1][1])
    row = db().task_get(tid)
    assert argv[:3] == ["claude", "--model", "opus"] and "--worktree" not in argv and argv[argv.index("--session-id") + 1] == row["claude_session_id"]
    assert argv[-2] == "--" and argv[-1].startswith("This task is being reopened") and argv[-1].endswith("Add a login page with tests.")
    assert (row["phase"], row["result"], row["result_at"], row["done_at"], row["auto_close"]) == ("running", None, None, None, 1)
    assert row["branch"] == "worktree-add-login-page" and row["worktree"] == str(wt) and row["session_row"] == body["session_row"]
    sess = db().open_rows()[name]
    assert sess["launcher"] == "task" and sess["cwd"] == str(wt) and sess["opts"]["model"] == "opus"
    # the Stop of the reopened session finishes the same task again
    finish_task(flow, tid, "Second round.")
    assert db().task_get(tid)["result"] == "Second round."


def test_reopen_with_a_follow_up_prompt_and_without_the_countdown(flow):
    tid = backlog(flow)
    dispatch(flow, tid)
    wt = flow.projects / "shop" / "api" / ".claude" / "worktrees" / "add-login-page"
    wt.mkdir(parents=True)
    name = finish_task(flow, tid)
    flow.client.delete(f"/api/sessions/{name}", headers=H)
    r = flow.client.post(f"/api/tasks/{tid}/reopen", headers=H, json={"prompt": "Also add a logout button.", "auto_close": False})
    assert r.status_code == 200, r.text
    assert shlex.split(flow.tmux["sent"][-1][1])[-1].endswith("Also add a logout button.")
    assert db().task_get(tid)["prompt"] == "Add a login page with tests.", "the card keeps its own prompt"
    assert db().task_get(tid)["auto_close"] == 0


def test_reopen_without_the_worktree_starts_a_fresh_one(flow):
    tid = backlog(flow)
    name = dispatch(flow, tid)["tmux"]
    finish_task(flow, tid)
    flow.client.delete(f"/api/sessions/{name}", headers=H)       # killed; its worktree was never created on disk in this test
    r = flow.client.post(f"/api/tasks/{tid}/reopen", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["reopened"] == "fresh" and r.json()["tmux"] == name
    argv = shlex.split(flow.tmux["sent"][-1][1])
    assert argv[argv.index("--worktree") + 1] == "add-login-page", "claude makes the worktree again"
    assert db().task_get(tid)["phase"] == "running" and db().task_get(tid)["result"] is None and len(db().tasks()) == 1


def test_reopen_rules(flow):
    assert flow.client.post("/api/tasks/9999/reopen", headers=H).status_code == 404
    tid = backlog(flow)
    r = flow.client.post(f"/api/tasks/{tid}/reopen", headers=H)
    assert r.status_code == 409 and r.json()["error"] == "only a finished task can be reopened"
    out = dispatch(flow, tid)
    assert flow.client.post(f"/api/tasks/{tid}/reopen", headers=H).status_code == 409, "still running"
    flow.client.delete(f"/api/sessions/{out['tmux']}", headers=H)
    for phase in ("done", "failed", "cancelled"):
        db().task_update(tid, phase=phase, archived_at=None)
        assert flow.client.post(f"/api/tasks/{tid}/reopen", headers=H, json={"auto_close": False}).status_code == 200, phase
        flow.client.delete(f"/api/sessions/{out['tmux']}", headers=H)
    db().task_update(tid, phase="done", pr_state="MERGED")
    assert flow.client.post(f"/api/tasks/{tid}/reopen", headers=H).status_code == 409, "a merged task is over"
    db().task_update(tid, pr_state=None, archived_at="2026-10-04T00:00:00+00:00")
    assert flow.client.post(f"/api/tasks/{tid}/reopen", headers=H).status_code == 409, "an archived one too"


def test_reopen_a_codex_task_runs_codex_in_its_worktree(flow, fake_codex):
    subprocess.run(["git", "-C", str(flow.projects / "shop" / "api"), "-c", "user.email=t@e.x", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)          # a managed worktree needs a commit to start from
    tid = backlog(flow, agent="codex")
    out = dispatch(flow, tid)
    name = out["tmux"]
    wt = db().task_get(tid)["worktree"]
    assert wt.endswith(".ccboard/worktrees/add-login-page")
    finish_task(flow, tid)
    flow.client.delete(f"/api/sessions/{name}", headers=H)
    r = flow.client.post(f"/api/tasks/{tid}/reopen", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["reopened"] == "worktree"
    assert flow.tmux["created"][-1][1] == wt
    line = flow.tmux["sent"][-1][1]
    assert line.startswith(("codex", "/")) or "codex" in line.split()[0]
    assert "This task is being reopened" in line and db().task_get(tid)["agent"] == "codex"


# ---------------------------------------------------------------- chains: the route and the whole path

def post_chain(board, steps=None, project="shop", repo="api", **body):
    steps = steps if steps is not None else [{"title": "Audit the cache", "prompt": "Audit it."},
                                             {"title": "Fix the findings", "prompt": "Fix:\n{{result}}", "agent": "claude"},
                                             {"title": "Review the fixes", "prompt": "Review the diff."}]
    return board.client.post(f"/api/projects/{project}/repos/{repo}/chains", headers=H, json={"steps": steps, **body})


def test_a_chain_is_one_backlog_card_and_queued_steps_behind_it(flow):
    r = post_chain(flow, auto_close=None)
    assert r.status_code == 201, r.text
    body = r.json()
    a, b, c = body["ids"]
    assert body["chain_id"] and body["started"] is None and [t["id"] for t in body["tasks"]] == [a, b, c]
    rows = [db().task_get(i) for i in (a, b, c)]
    assert [r["phase"] for r in rows] == ["backlog", "queued", "queued"]
    assert [r["parent_id"] for r in rows] == [None, a, b] and {r["chain_id"] for r in rows} == {body["chain_id"]}
    assert [r["slug"] for r in rows] == ["audit-the-cache", "fix-the-findings", "review-the-fixes"] and [r["auto_close"] for r in rows] == [1, 1, 1]
    assert [r["agent"] for r in rows] == ["claude", "claude", "claude"] and all(r["tmux_name"] == "" and r["worktree"] == "" for r in rows)
    assert rows[1]["prompt"] == "Fix:\n{{result}}", "the template is kept as written"
    assert [t["chain"] for t in body["tasks"]] == [{"i": 1, "n": 3}, {"i": 2, "n": 3}, {"i": 3, "n": 3}]
    assert [t["column"] for t in body["tasks"]] == ["backlog"] * 3 and flow.tmux["created"] == []
    v = tasks_in_state(flow)
    assert v[b]["parent_id"] == a and v[c]["chain"] == {"i": 3, "n": 3} and v[b]["limit_hold"] is None


def test_a_chain_inherits_agents_down_the_steps_and_validates_before_it_writes(flow, fake_codex):
    r = post_chain(flow, [{"title": "One", "prompt": "p"}, {"title": "Two", "prompt": "p", "agent": "codex"}, {"title": "Three", "prompt": "p"}])
    assert r.status_code == 201, r.text
    assert [db().task_get(i)["agent"] for i in r.json()["ids"]] == ["claude", "codex", "codex"], "unset = the step before's agent"
    n = len(db().tasks())
    bad = [
        ([], "at least one step"), ([{"title": "x", "prompt": "p"}] * 11, "at most 10"),
        ([{"title": "ok", "prompt": "p"}, {"title": "", "prompt": "p"}], "step 2: title and prompt are required"),
        ([{"title": "ok", "prompt": "p"}, {"title": "bad", "prompt": "p", "agent": "gemini"}], "unknown agent"),
        ([{"title": "ok", "prompt": "p"}, {"title": "bypass", "prompt": "p", "permission_mode": "bypassPermissions"}], "step 2"),
        ([{"title": "ok", "prompt": "p", "mode": "teleport"}], "step 1: mode"),
    ]
    for steps, msg in bad:
        r = post_chain(flow, steps)
        assert r.status_code == 400 and msg in r.json()["error"], (steps, r.text)
    assert len(db().tasks()) == n, "nothing is written when a step is wrong"
    assert post_chain(flow, repo="nope").status_code == 404


def test_a_chain_keeps_each_steps_launch_choices_and_an_explicit_no_auto_close(flow):
    r = post_chain(flow, [{"title": "One", "prompt": "p", "model": "opus", "effort": "high"}, {"title": "Two", "prompt": "q", "args": "--verbose"}], auto_close=False)
    a, b = r.json()["ids"]
    assert json.loads(db().task_get(a)["spec"]) == {"model": "opus", "effort": "high", "auto_close": False}
    assert json.loads(db().task_get(b)["spec"]) == {"args": "--verbose", "auto_close": False}
    assert db().task_get(a)["auto_close"] == 0 and db().task_get(b)["auto_close"] == 0
    out = dispatch(flow, a)
    assert db().task_get(a)["auto_close"] == 0, "an explicit off survives a lane dispatch's default"
    assert shlex.split(flow.tmux["sent"][-1][1])[1:5] == ["--model", "opus", "--effort", "high"]


def test_a_chain_with_dispatch_starts_step_one_and_the_stop_starts_step_two_with_the_result(flow):
    r = post_chain(flow, dispatch=True)
    assert r.status_code == 201, r.text
    a, b, c = r.json()["ids"]
    assert r.json()["started"]["id"] == a and r.json()["started"]["phase"] == "running"
    name_a = db().task_get(a)["tmux_name"]
    assert db().task_get(a)["auto_close"] == 1
    hook(flow, name_a, "UserPromptSubmit", prompt="Audit it.")
    hook(flow, name_a, "Stop", last_assistant_message="Three findings: cache, fonts, version.")
    assert db().task_get(a)["phase"] == "done"
    assert db().task_get(b)["phase"] == "running" and db().task_get(c)["phase"] == "queued", "the stop started step 2 in a lane of its own"
    argv = shlex.split(flow.tmux["sent"][-1][1])
    assert argv[-1] == "Fix:\nThree findings: cache, fonts, version." and "--worktree" in argv and argv[argv.index("--worktree") + 1] == "fix-the-findings"
    assert db().task_get(b)["prompt"] == "Fix:\nThree findings: cache, fonts, version.", "what was sent is what the card now holds"
    assert db().task_get(b)["auto_close"] == 1 and db().task_get(b)["tmux_name"] != name_a
    v = tasks_in_state(flow)
    assert v[b]["column"] == "in_progress" and v[b]["chain"] == {"i": 2, "n": 3}


def test_a_chain_crosses_from_claude_to_codex_through_the_board(flow, fake_codex):
    subprocess.run(["git", "-C", str(flow.projects / "shop" / "api"), "-c", "user.email=t@e.x", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)          # a managed worktree needs a commit to start from
    r = post_chain(flow, [{"title": "Write it", "prompt": "Write the thing."},
                          {"title": "Review it", "prompt": "Review this work:\n{{result}}", "agent": "codex"}], dispatch=True)
    assert r.status_code == 201, r.text
    a, b = r.json()["ids"]
    name_a = db().task_get(a)["tmux_name"]
    hook(flow, name_a, "UserPromptSubmit", prompt="Write the thing.")
    hook(flow, name_a, "Stop", last_assistant_message="I wrote the thing in thing.py.")
    row = db().task_get(b)
    assert (row["phase"], row["agent"]) == ("running", "codex") and row["worktree"].endswith(".ccboard/worktrees/review-it")
    line = dict(flow.tmux["sent"])[row["tmux_name"]]
    assert shlex.split(line)[0] == "codex" and "Review this work:\nI wrote the thing in thing.py." in shlex.split(line)[-1]
    assert flow.tmux["created"][-1][1] == row["worktree"], "Codex starts in the worktree ccboard made for it"
    assert db().open_rows()[row["tmux_name"]]["agent"] == "codex" and row["auto_close"] == 1


def test_a_codex_chain_step_dispatches_with_its_own_permission_map(flow, fake_codex):
    subprocess.run(["git", "-C", str(flow.projects / "shop" / "api"), "-c", "user.email=t@e.x", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)
    r = post_chain(flow, [{"title": "Write it", "prompt": "Write the thing."},
                          {"title": "Review it", "prompt": "Review:\n{{result}}", "agent": "codex", "permission_mode": "plan",
                           "model": "gpt-5.5", "reasoning_effort": "high"},
                          {"title": "Summarise", "prompt": "Summarise:\n{{result}}"}], dispatch=True)
    assert r.status_code == 201, r.text
    a, b, c = r.json()["ids"]
    assert [db().task_get(i)["agent"] for i in (a, b, c)] == ["claude", "codex", "codex"]
    assert json.loads(db().task_get(b)["spec"])["permission_mode"] == "plan"
    name_a = db().task_get(a)["tmux_name"]
    hook(flow, name_a, "UserPromptSubmit", prompt="Write the thing.")
    hook(flow, name_a, "Stop", last_assistant_message="Done: thing.py.")
    line = shlex.split(dict(flow.tmux["sent"])[db().task_get(b)["tmux_name"]])
    assert line[0] == "codex" and line[line.index("-s") + 1] == "read-only" and line[line.index("-a") + 1] == "on-request"
    assert line[line.index("-m") + 1] == "gpt-5.5" and 'model_reasoning_effort="high"' in line
    assert "--dangerously-bypass-approvals-and-sandbox" not in line and line[-1] == "Review:\nDone: thing.py."
    bad = post_chain(flow, [{"title": "x", "prompt": "p", "agent": "codex", "permission_mode": "bypassPermissions"}])
    assert bad.status_code == 400 and "step 1" in bad.text


def test_the_limit_window_holds_chain_steps_and_the_card_says_so(flow):
    a, b = post_chain(flow, [{"title": "One", "prompt": "p"}, {"title": "Two", "prompt": "q"}]).json()["ids"]
    name = dispatch(flow, a)["tmux"]
    now = time.time()
    main.db.kv_set("rate_limits", {"five_hour": {"used_percentage": 93, "resets_at": int(now + 1200)}, "seven_day": {"used_percentage": 20}})
    flow.rt.clock = time.time
    hook(flow, name, "Stop", last_assistant_message="Done one.")
    assert db().task_get(b)["phase"] == "queued", "held while the 5 h window is at 93 %"
    v = tasks_in_state(flow)[b]
    assert v["limit_hold"] == {"kind": "5h", "resets_at": int(now + 1200), "pct": 93.0} and v["column"] == "backlog"
    assert tasks_in_state(flow)[a]["limit_hold"] is None, "only a queued step is held"
    # the hand dispatch is never blocked: it starts with the parent's result and a warning
    r = flow.client.post(f"/api/tasks/{b}/dispatch", headers=H, json={})
    assert r.status_code == 200, r.text
    assert r.json()["limit_warning"]["kind"] == "5h" and r.json()["limit_warning"]["pct"] == 93.0
    assert shlex.split(flow.tmux["sent"][-1][1])[-1] == "q\n\n---\nResult of the previous step (One):\nDone one."
    assert db().task_get(b)["phase"] == "running"


def test_a_queued_step_cannot_be_started_by_hand_before_its_parent_is_done(flow):
    a, b = post_chain(flow, [{"title": "One", "prompt": "p"}, {"title": "Two", "prompt": "q"}]).json()["ids"]
    r = flow.client.post(f"/api/tasks/{b}/dispatch", headers=H, json={})
    assert r.status_code == 409 and r.json()["error"] == "this task is queued behind another step"
    assert flow.tmux["created"] == []


def test_a_failed_first_step_cancels_the_rest_on_the_next_tick(flow):
    a, b, c = post_chain(flow).json()["ids"]
    name = dispatch(flow, a)["tmux"]
    hook(flow, name, "StopFailure", error="server_error", last_assistant_message="API Error: 500")
    assert db().task_get(a)["phase"] == "failed" and db().task_get(b)["phase"] == "cancelled"
    flow.rt.run_due(flow.clock())
    assert db().task_get(c)["phase"] == "cancelled" and db().task_get(c)["result"] == "blocked: step failed"


def test_create_with_after_task_id_queues_the_task_behind_another(flow):
    parent = backlog(flow, title="Parent")
    r = post_task(flow, title="Child", after_task_id=parent)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert r.json()["phase"] == "queued" and r.json()["task"]["column"] == "backlog" and flow.tmux["created"] == []
    child, par = db().task_get(cid), db().task_get(parent)
    assert child["phase"] == "queued" and child["parent_id"] == parent and child["chain_id"] and child["chain_id"] == par["chain_id"]
    assert child["agent"] == "claude"
    later = post_task(flow, title="Grandchild", after_task_id=cid, when="later", auto_close=False)
    assert db().task_get(later.json()["id"])["chain_id"] == child["chain_id"] and db().task_get(later.json()["id"])["parent_id"] == cid
    assert json.loads(db().task_get(later.json()["id"])["spec"]) == {"auto_close": False}
    assert post_task(flow, title="Now", after_task_id=parent, when="now").status_code == 400
    assert post_task(flow, title="Ghost", after_task_id=9999).status_code == 404
    assert tasks_in_state(flow)[cid]["chain"] == {"i": 2, "n": 3}


def test_after_task_id_inherits_the_agent_of_the_parent(flow, fake_codex):
    parent = backlog(flow, title="Parent", agent="codex")
    cid = post_task(flow, title="Child", after_task_id=parent).json()["id"]
    assert db().task_get(cid)["agent"] == "codex"
    other = post_task(flow, title="Other", after_task_id=parent, agent="claude").json()["id"]
    assert db().task_get(other)["agent"] == "claude", "named: not inherited"


def test_a_step_made_with_after_task_id_closes_like_any_lane_session_unless_it_said_no(flow):
    """A queued step that never mentioned auto_close gets the lane default (on) when the runtime starts it; an explicit off is kept."""
    parent = backlog(flow, title="Parent")
    plain = post_task(flow, title="Plain step", after_task_id=parent).json()["id"]
    off = post_task(flow, title="Quiet step", after_task_id=parent, auto_close=False).json()["id"]
    on = post_task(flow, title="Eager step", after_task_id=parent, auto_close=True).json()["id"]
    assert [db().task_get(t)["auto_close"] for t in (plain, off, on)] == [0, 0, 1]
    name = dispatch(flow, parent)["tmux"]
    hook(flow, name, "UserPromptSubmit", prompt="go")
    hook(flow, name, "Stop", last_assistant_message="Parent is done.")
    assert [db().task_get(t)["phase"] for t in (plain, off, on)] == ["running"] * 3, "fan-out: every child started"
    assert [db().task_get(t)["auto_close"] for t in (plain, off, on)] == [1, 0, 1]


# ---------------------------------------------------------------- dispatch: auto_close defaults, queue, warning

def test_dispatch_auto_close_defaults_and_the_cards_own_switch(flow):
    on = backlog(flow, title="Switch on", auto_close=True)
    off = backlog(flow, title="Switch off", auto_close=False)
    plain = backlog(flow, title="Plain")
    assert db().task_get(on)["auto_close"] == 1 and db().task_get(off)["auto_close"] == 0 and json.loads(db().task_get(off)["spec"]) == {"auto_close": False}
    for tid in (on, off, plain):
        dispatch(flow, tid)
    assert [db().task_get(t)["auto_close"] for t in (on, off, plain)] == [1, 0, 1], "on stays on, explicit off stays off, unset = the lane default"
    forced_off = backlog(flow, title="Forced off")
    dispatch(flow, forced_off, auto_close=False)
    assert db().task_get(forced_off)["auto_close"] == 0
    s1 = make_session(flow, state="idle")
    sess_on = backlog(flow, title="Session on", auto_close=True)
    flow.client.post(f"/api/tasks/{sess_on}/dispatch", headers=H, json={"session": s1})
    assert db().task_get(sess_on)["auto_close"] == 1, "the card's own switch survives a session drop"


def test_the_start_now_route_and_the_legacy_route_keep_auto_close_off_unless_asked(flow):
    legacy = flow.client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": "Legacy", "prompt": "p"}).json()["id"]
    now = post_task(flow, title="Now").json()["id"]
    asked = post_task(flow, title="Asked", auto_close=True).json()["id"]
    assert [db().task_get(t)["auto_close"] for t in (legacy, now, asked)] == [0, 0, 1]


def test_queue_hands_a_task_to_a_working_claude_session(flow):
    s1 = make_session(flow, state="working")
    tid = backlog(flow)
    r = flow.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1})
    assert r.status_code == 409 and r.json()["state"] == "working", "without queue a working session is refused as before"
    r = flow.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1, "queue": True})
    assert r.status_code == 200 and r.json()["queued"] is True and r.json()["pasted"] is True
    assert flow.tmux["pasted"][-1][:2] == (s1, "Add a login page with tests.") and db().task_get(tid)["phase"] == "running"
    db().update_flags(s1, {"compacting": True})
    t2 = backlog(flow, title="Second")
    assert flow.client.post(f"/api/tasks/{t2}/dispatch", headers=H, json={"session": s1, "queue": True}).status_code == 409, "never into a compaction"
    idle = make_session(flow, name="s2", state="idle")
    t3 = backlog(flow, title="Third")
    r = flow.client.post(f"/api/tasks/{t3}/dispatch", headers=H, json={"session": idle, "queue": True})
    assert r.status_code == 200 and r.json()["queued"] is False


def test_a_hand_dispatch_is_warned_never_blocked(flow):
    tid = backlog(flow)
    main.db.kv_set("rate_limits", {"five_hour": {"used_percentage": 97, "resets_at": int(time.time() + 600)}, "seven_day": {"used_percentage": 99, "resets_at": int(time.time() + 86400)}})
    r = flow.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={})
    assert r.status_code == 200 and r.json()["phase"] == "running"
    assert r.json()["limit_warning"]["kind"] == "7d" and r.json()["limit_warning"]["pct"] == 99.0
    now = post_task(flow, title="Start now")
    assert now.status_code == 201 and now.json()["limit_warning"]["kind"] == "7d"
    legacy = flow.client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": "Legacy", "prompt": "p"})
    assert legacy.status_code == 201 and set(legacy.json()) == {"id", "slug", "tmux", "branch", "attach_url"}, "the legacy answer is unchanged"
    main.db.kv_del("rate_limits")
    ok = flow.client.post(f"/api/tasks/{backlog(flow, title='Later')}/dispatch", headers=H, json={})
    assert ok.status_code == 200 and "limit_warning" not in ok.json()


# ---------------------------------------------------------------- the task view

def test_the_task_view_carries_the_runtime_fields(flow):
    tid = backlog(flow)
    v = tasks_in_state(flow)[tid]
    for key in ("result_at", "done_at", "closed_at", "autoclose", "chain", "limit_hold"):
        assert key in v and v[key] is None, key
    name = dispatch(flow, tid)["tmux"]
    hook(flow, name, "UserPromptSubmit", prompt="go")
    hook(flow, name, "Stop", last_assistant_message="r" * 500)
    v = tasks_in_state(flow)[tid]
    assert v["result"] == "r" * 300, "the poll carries the head; GET /api/tasks/{id} the rest"
    assert flow.client.get(f"/api/tasks/{tid}", headers=H).json()["result"] == "r" * 500
    assert v["autoclose"]["task"] == tid
    # another session's countdown is not this task's
    other = backlog(flow, title="Other")
    assert tasks_in_state(flow)[other]["autoclose"] is None


def test_a_task_whose_session_was_killed_by_hand_reads_as_cancelled_after_the_sweep(flow):
    tid = backlog(flow)
    name = dispatch(flow, tid)["tmux"]
    assert flow.client.delete(f"/api/sessions/{name}", headers=H).status_code == 200
    res = flow.rt.run_due(flow.clock())
    assert res["swept"] == [tid]
    v = tasks_in_state(flow)[tid]
    assert v["phase"] == "cancelled" and v["column"] == "done" and v["done_at"] and v["closed_at"] is None, "killed, not closed after a stop"


def test_the_new_routes_need_an_identity_and_the_csrf_header(flow):
    tid = backlog(flow)
    for path in (f"/api/tasks/{tid}/reopen", f"/api/tasks/{tid}/detach", f"/api/tasks/{tid}/close-session", f"/api/tasks/{tid}/keep-open",
                 "/api/projects/shop/repos/api/chains"):
        assert flow.client.post(path, json={}).status_code in (401, 403), path
        assert flow.client.post(path, headers={"Tailscale-User-Login": "alice@example.com"}, json={}).status_code in (401, 403), f"{path} without X-CCBoard"
