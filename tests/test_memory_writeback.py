"""The claude-mem write-back of a finished task (issue #10): memory.save_note, memory_proxy.writeback_task, the taskflow hook and the
/api/memory/prefs pair. Off by default; the only POST the board ever makes to the worker, and only to the fake one of tests/mem_fake.py."""
import json
import time
from pathlib import Path

import pytest

from app import memory, memory_proxy as mp, taskflow
from app.config import settings
from app.db import DB
from app.taskflow import Runtime
from tests.mem_fake import closed_port

H = {"Tailscale-User-Login": "alice@example.com"}


@pytest.fixture(autouse=True)
def _iso(mem_home, monkeypatch):
    monkeypatch.delenv("CLAUDE_MEM_PROJECT_ENVIRONMENTS", raising=False)
    mp.reset()


def install_plugin(enabled=True):
    d = Path(settings.claude_config_dir)
    (d / "plugins").mkdir(parents=True, exist_ok=True)
    (d / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {memory.PLUGIN_KEY: [{"version": "13.31.0"}]}}))
    (d / "settings.json").write_text(json.dumps({"enabledPlugins": {memory.PLUGIN_KEY: enabled}}))


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def rig(projects_dir, mem_home, tmp_path):
    api = projects_dir / "shop" / "api"
    (api / ".git").mkdir(parents=True)
    wt = api / ".claude" / "worktrees" / "fix-login"
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: x\n")
    db = DB(tmp_path / "wb.db")
    rt = Runtime(db, start_session=lambda *a, **k: None, end_session=lambda *a, **k: True, send_text=lambda *a: None,
                 real_clients=lambda n: 0, perm_pending=lambda n: False, mem_processing=lambda: False, clock=lambda: 1_800_000_000.0, grace=45.0)
    install_plugin()

    class R:
        pass
    r = R()
    r.db, r.rt, r.wt = db, rt, wt
    return r


def finish(r, text="The task found the bug.", *, failed=False, slug="fix-login", worktree=True, title="Fix the login", agent="claude"):
    name = f"shop--api--t-{slug}"
    rid = r.db.add_session(tmux_name=name, project="shop", repo="api", name=f"t-{slug}", launcher="task", cmd="x", agent=agent)
    r.db.set_state(name, "working", "test")
    tid = r.db.task_add(project="shop", repo="api", slug=slug, title=title, prompt="SECRET PROMPT TEXT", tmux_name=name, session_row=rid,
                        phase="running", auto_close=0, worktree=str(r.wt) if worktree else "", agent=agent)
    row = r.db.open_row(name)
    r.db.set_state(name, "done", "Stop", message=(text or "")[:300])
    return tid, r.rt.on_turn_end(name, row, text, failed=failed, message="boom" if failed else None)


def posts(w):
    return [json.loads(b) for _, b in w.posts]


# ------------------------------------------------------------------ off by default

def test_off_by_default_nothing_is_sent(rig, mem_worker):
    assert memory.writeback_on(rig.db) is False
    tid, out = finish(rig)
    assert out["phase"] == "done"
    time.sleep(0.2)
    assert mem_worker.posts == [] and not any(m == "POST" for m, _ in mem_worker.requests)
    assert rig.db.kv_get(memory.KV_WRITEBACK_DONE + str(tid)) is None


def test_on_sends_exactly_one_note_with_the_worktree_key_and_no_prompt(rig, mem_worker):
    memory.set_writeback(rig.db, True)
    tid, out = finish(rig, "Line one.\nLine two.")
    assert wait_for(lambda: len(mem_worker.posts) == 1)
    path, raw = mem_worker.posts[0]
    body = json.loads(raw)
    assert path == "/api/memory/save" and set(body) == {"text", "title", "project", "metadata"}
    assert body == {"text": "Line one.\nLine two.", "title": "Task: Fix the login", "project": "api/fix-login",
                    "metadata": {"source": "ccboard", "task_id": tid, "agent": "claude"}}
    assert b"SECRET PROMPT" not in raw
    assert mp.writeback_task(rig.db, rig.db.task_get(tid), "again") is False, "once per task id"
    time.sleep(0.2)
    assert len(mem_worker.posts) == 1


def test_a_task_outside_a_worktree_uses_the_repo_key(rig, mem_worker):
    memory.set_writeback(rig.db, True)
    finish(rig, slug="inplace", worktree=False)
    assert wait_for(lambda: len(mem_worker.posts) == 1)
    assert posts(mem_worker)[0]["project"] == "api"


def test_failed_or_empty_results_send_nothing(rig, mem_worker):
    memory.set_writeback(rig.db, True)
    finish(rig, failed=True, slug="f1")
    finish(rig, "   ", slug="f2")
    time.sleep(0.2)
    assert mem_worker.posts == []


@pytest.mark.parametrize("why", ["claude_mem_off", "no_plugin", "plugin_disabled"])
def test_nothing_is_sent_when_claude_mem_is_off_or_missing(rig, mem_worker, monkeypatch, why):
    memory.set_writeback(rig.db, True)
    if why == "claude_mem_off":
        monkeypatch.setattr(settings, "claude_mem", False)
    elif why == "no_plugin":
        (Path(settings.claude_config_dir) / "plugins" / "installed_plugins.json").unlink()
    else:
        install_plugin(enabled=False)
    finish(rig)
    time.sleep(0.2)
    assert mem_worker.posts == []


# ------------------------------------------------------------------ the cap, and failures never reach the task

def test_the_text_is_capped_at_a_line_break_and_marked():
    long = "\n".join(f"line {i} " + "x" * 60 for i in range(400))
    out = memory.cap_note(long)
    assert len(out.encode()) <= memory.SAVE_TEXT_MAX and out.endswith(memory.SAVE_CUT_MARK)
    assert out[: -len(memory.SAVE_CUT_MARK)].endswith("x"), "cut at a line end, not mid-line"
    assert memory.cap_note("short") == "short"


def test_a_hanging_worker_never_delays_the_task(rig, mem_worker):
    memory.set_writeback(rig.db, True)
    mem_worker.delay["/api/memory/save"] = 3.0
    t0 = time.monotonic()
    tid, out = finish(rig)
    assert out["phase"] == "done" and time.monotonic() - t0 < 0.5
    assert rig.db.task_get(tid)["phase"] == "done"


def test_a_refused_or_failing_worker_never_raises(rig, mem_worker, mem_home):
    memory.set_writeback(rig.db, True)
    mem_worker.status["/api/memory/save"] = 500
    calls = []
    assert mp.writeback_task(rig.db, {"id": 1, "project": "shop", "repo": "api", "title": "t", "worktree": ""}, "x", run=lambda f: calls.append(f() or 1))
    assert calls == [1]
    (mem_home / "worker.pid").write_text(json.dumps({"pid": 1, "port": closed_port()}))
    assert mp.writeback_task(rig.db, {"id": 2, "project": "shop", "repo": "api", "title": "t", "worktree": ""}, "x", run=lambda f: calls.append(f() or 2))
    assert calls == [1, 2]
    with pytest.raises(memory.Refused):
        memory.save_note("t", "x", "api")


def test_save_note_refuses_a_non_loopback_host(mem_home):
    (mem_home / "settings.json").write_text(json.dumps({"CLAUDE_MEM_WORKER_HOST": "10.0.0.5"}))
    with pytest.raises(memory.NotLoopback):
        memory.save_note("t", "x", "api")


# ------------------------------------------------------------------ the preference

def test_prefs_get_and_put(lite_client, mem_home):
    r = lite_client.get("/api/memory/prefs", headers=H).json()
    assert r["prefs"] == {"writeback": False} and r["available"] is False and "not installed" in r["reason"]
    assert r["writeback_sends"]["never"] == ["the prompt"]
    assert lite_client.put("/api/memory/prefs", json={"writeback": True}, headers=H).status_code == 403     # no X-CCBoard
    install_plugin()
    r = lite_client.put("/api/memory/prefs", json={"writeback": True}, headers={**H, "X-CCBoard": "1"}).json()
    assert r["prefs"] == {"writeback": True} and r["available"] is True and r["reason"] is None
    assert lite_client.get("/api/memory/prefs", headers=H).json()["prefs"] == {"writeback": True}
    r = lite_client.put("/api/memory/prefs", json={"writeback": False}, headers={**H, "X-CCBoard": "1"}).json()
    assert r["prefs"] == {"writeback": False}
