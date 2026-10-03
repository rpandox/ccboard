"""Regression tests for the v0.4 review findings (scheduler, previews, fleet, permission gating)."""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

from app import health, scheduler
from app.db import DB, now as db_now
from tests.test_scheduler import H, make_repo

JOB = dict(project="shop", repo="api", name="once", prompt="p", permission_mode="acceptEdits", max_turns=5,
           max_budget_usd=None, args=None, timeout_s=None, batch_id=None)


def _db():
    from app.config import settings
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return DB(settings.db_path)


def test_stale_running_runs_closed_on_startup(projects_dir, fake_tmux):
    from fastapi.testclient import TestClient
    from app import main
    pre = _db()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
    jid = pre.job_add(**{**JOB, "name": "nightly"}, cron="0 2 * * *", enabled=1, next_run_at=future)
    rid = pre.run_start(jid)                    # the previous process died here, before run_finish()
    far = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(timespec="seconds")
    assert pre.jobs_due(far) == []              # the stale 'running' row hides the job
    with TestClient(main.app):
        r = main.db.run_get(rid)
        assert r["status"] == "error" and "interrupted" in r["error"] and r["finished_at"]
        assert [j["id"] for j in main.db.jobs_due(far)] == [jid]
        assert main.db.job_get(jid)["last_status"] == "interrupted"


def test_tick_is_serialised(projects_dir, monkeypatch):
    db = _db()
    jid = db.job_add(**JOB, cron=None, enabled=1, next_run_at=db_now())
    real_due = db.jobs_due

    def slow_due(at):                           # widen the read-then-write window a concurrent tick could slip into
        rows = real_due(at)
        time.sleep(0.3)
        return rows
    monkeypatch.setattr(db, "jobs_due", slow_due)
    monkeypatch.setattr(scheduler, "run_job", lambda d, job, rid: d.run_finish(rid, status="ok", result=""))
    monkeypatch.setattr(scheduler.claude_auth, "status", lambda: {"installed": True, "loggedIn": True})
    w = scheduler.Worker(db)
    started = []
    ts = [threading.Thread(target=lambda: started.extend(w.tick())) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(started) == 1 and len(db.runs(job_id=jid)) == 1


def test_quota_unknown_and_backoff(projects_dir):
    db = _db()
    q = scheduler.quota_state(db)
    assert q["known"] is False and q["pct"] is None and scheduler.quota_blocked(db) is None
    until = scheduler.set_backoff(db)
    assert scheduler.quota_blocked(db).startswith("backing off until " + until[:13])
    soon = time.time() + 3600                   # a reset within the cap extends the back-off to it
    assert scheduler.set_backoff(db, soon)[:16] == datetime.fromtimestamp(soon, tz=timezone.utc).isoformat()[:16]
    far = time.time() + 30 * 3600               # a bogus far-future reset does not
    assert scheduler.set_backoff(db, far) < datetime.fromtimestamp(far, tz=timezone.utc).isoformat()
    db.kv_set(scheduler.KV_BACKOFF, "2000-01-01T00:00:00+00:00")
    assert scheduler.quota_blocked(db) is None  # expired
    db.kv_set("rate_limits", {"five_hour": {"used_percentage": 90, "resets_at": soon}})
    assert "90%" in scheduler.quota_blocked(db) and scheduler.quota_state(db)["known"]


def test_parse_result_mid_run_limit():
    filler = "Refactored the module and reran the suite. " * 30
    base = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 6, "session_id": "s", "total_cost_usd": 1.0}
    r = scheduler.parse_result(json.dumps({**base, "result": filler + "You've hit your usage limit. Resets 4pm."}))
    assert r["rate_limited"] and r["turns"] == 6
    r = scheduler.parse_result(json.dumps({**base, "result": "Added a rate limit middleware to the API. " + filler + "All tests pass."}))
    assert not r["rate_limited"]


def test_toggle_reenables_fired_oneoff(client, projects_dir):
    from app import main
    jid = main.db.job_add(**JOB, cron=None, enabled=0, next_run_at=None)     # a one-off that already fired
    r = client.post(f"/api/jobs/{jid}/toggle", headers=H)
    assert r.status_code == 200 and r.json()["enabled"] == 1
    j = main.db.job_get(jid)
    assert j["enabled"] == 1 and j["next_run_at"]


def test_settings_overrides_rejected_on_host(client, projects_dir, fake_tmux, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    make_repo(projects_dir)
    for args in ("--settings /tmp/evil.json", '--settings={"permissions":{"defaultMode":"bypassPermissions"}}',
                 "--setting-sources user", "--permission-prompt none"):
        r = client.post("/api/projects/shop/repos/api/tasks", json={"title": "t", "prompt": "p", "args": args}, headers=H)
        assert r.status_code == 400 and "not allowed" in r.text, args
        r = client.post("/api/projects/shop/repos/api/sessions", json={"launcher": "claude", "args": args}, headers=H)
        assert r.status_code == 400 and "overrides" in r.text, args
    # the bypass flag itself: never for tasks, an explicit choice for interactive sessions
    assert client.post("/api/projects/shop/repos/api/tasks", json={"title": "t", "prompt": "p", "args": "--permission-mode bypassPermissions"}, headers=H).status_code == 400
    r = client.post("/api/projects/shop/repos/api/sessions", json={"launcher": "claude", "args": "--permission-mode bypassPermissions"}, headers=H)
    assert r.status_code == 201 and "--permission-mode bypassPermissions" in r.json()["cmd"]
    r = client.post("/api/projects/shop/repos/api/sessions", json={"launcher": "claude", "args": "--model opus"}, headers=H)
    assert "bypass" not in r.text.lower()


def test_kill_session_drops_preview(client, projects_dir, fake_tmux, monkeypatch):
    from app import main, previews, tmux
    name = tmux.tmux_name("shop", "api", "t-fix")
    tid = main.db.task_add(project="shop", repo="api", slug="fix", title="t", prompt="p", branch="worktree-fix", base="main",
                           worktree=str(projects_dir / "shop/api/.claude/worktrees/fix"), tmux_name=name, claude_session_id=None)
    main.db.task_update(tid, preview_port=3000, preview_https=9100)
    fake_tmux["sessions"][name] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "claude", "path": "/", "pid": 1, "env": {}}
    off = []
    monkeypatch.setattr(previews, "serve_off", lambda p: off.append(p))
    r = client.delete(f"/api/sessions/{name}", headers=H)
    assert r.status_code == 200 and off == [9100]
    t = main.db.task_get(tid)
    assert t["preview_https"] is None and t["preview_port"] is None
    assert 9100 not in main.db.preview_ports_in_use()


def test_preview_port_allocation_is_atomic(client, projects_dir, fake_tmux, monkeypatch):
    from app import main, previews, tmux
    monkeypatch.setattr(previews, "public_host", lambda: "box.tailnet.ts.net")
    barrier = threading.Barrier(2, timeout=5)

    def serve_on(hp, port):                     # both requests are "inside tailscale serve" at the same time
        barrier.wait()
        return f"https://box.tailnet.ts.net:{hp}/"
    monkeypatch.setattr(previews, "serve_on", serve_on)
    tids = [main.db.task_add(project="shop", repo="api", slug=f"s{i}", title="t", prompt="p", branch=f"worktree-s{i}", base="main",
                             worktree=str(projects_dir / f"shop/api/.claude/worktrees/s{i}"),
                             tmux_name=tmux.tmux_name("shop", "api", f"t-s{i}"), claude_session_id=None) for i in range(2)]
    results = {}

    def go(tid):
        results[tid] = client.post(f"/api/tasks/{tid}/preview", json={"port": 3000 + tid}, headers=H).json()
    ts = [threading.Thread(target=go, args=(t,)) for t in tids]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len({results[t]["https_port"] for t in tids}) == 2, results
    assert {main.db.task_get(t)["preview_https"] for t in tids} == {results[t]["https_port"] for t in tids}


def test_hub_poller_keeps_configured_identity(projects_dir):
    db = _db()
    nodes = health.parse_nodes("ubu=https://ubu.ts.net:8443")
    p = health.Poller(db, nodes, fetch=lambda url: {"url": "https://evil.example/x", "name": "evil", "online": False, "sessions": 1})
    out = p.poll_once()
    assert out[0]["url"] == "https://ubu.ts.net:8443" and out[0]["name"] == "ubu" and out[0]["online"] is True and out[0]["sessions"] == 1
    assert health.Poller(db, nodes, fetch=lambda url: ["not", "a", "dict"]).poll_once()[0]["online"] is False


def test_runs_deferred_while_logged_out(projects_dir, monkeypatch):
    db = _db()
    jid = db.job_add(**JOB, cron=None, enabled=1, next_run_at=db_now())
    monkeypatch.setattr(scheduler.claude_auth, "status", lambda: {"installed": True, "loggedIn": False})
    w = scheduler.Worker(db)
    assert w.tick() == [] and db.runs(job_id=jid) == []
    j = db.job_get(jid)
    assert j["last_status"].startswith("deferred: claude is not logged in") and j["next_run_at"] > db_now()
    monkeypatch.setattr(scheduler.claude_auth, "status", lambda: {"installed": True, "loggedIn": True})
    monkeypatch.setattr(scheduler, "run_job", lambda d, job, rid: d.run_finish(rid, status="ok", result=""))
    db.job_update(jid, next_run_at=db_now())
    assert len(w.tick()) == 1


def test_describe_uses_login_not_bare():
    from app import gitops, hooks
    assert "--bare" not in gitops.DESCRIBE_FLAGS and "--setting-sources" in gitops.DESCRIBE_FLAGS and "--strict-mcp-config" in gitops.DESCRIBE_FLAGS
    assert hooks.resolve_session({"x-ccboard-session": "none"}, {"cwd": "/"}, {}) == (None, "ignored")


def test_project_folder_session(client, projects_dir, fake_tmux):
    from app import hooks, main
    make_repo(projects_dir)                                              # shop/api
    r = client.post("/api/projects/shop/repos/root/sessions", json={"launcher": "shell"}, headers=H)
    assert r.status_code == 201, r.text
    name = r.json()["tmux"]
    assert name == "shop--root--s1" and fake_tmux["created"][0][1] == str(projects_dir / "shop")
    st = client.get("/api/state", headers=H).json()
    proj = next(p for p in st["projects"] if p["name"] == "shop")
    assert proj["root"]["root"] and [s["name"] for s in proj["root"]["sessions"]] == ["s1"] and [x["name"] for x in proj["repos"]] == ["api"]
    rows = main.db.open_rows()
    assert rows[name]["repo"] == "root"
    assert hooks.resolve_session({}, {"cwd": str(projects_dir / "shop")}, rows) == (name, "cwd")
    assert client.post("/api/projects/shop/repos", json={"name": "root"}, headers=H).status_code == 400
    t = client.post("/api/projects/shop/repos/root/tasks", json={"title": "t", "prompt": "p"}, headers=H)   # v0.5.14a: in place, no worktree
    assert t.status_code == 201 and t.json()["branch"] == "" and main.db.task_get(t.json()["id"])["mode"] == "attached", t.text
    d = client.delete("/api/projects/shop", headers=H)
    assert d.status_code == 200 and name in d.json()["killed_sessions"]


def test_launch_options(client, projects_dir, fake_tmux, monkeypatch):
    import shlex
    from app.config import settings
    make_repo(projects_dir)
    monkeypatch.setattr(settings, "claude_bin", lambda: "/usr/bin/claude")
    body = {"launcher": "claude", "name": "dev", "model": "fable", "effort": "high", "permission_mode": "acceptEdits",
            "allowed_tools": "Bash(npm test), Read", "disallowed_tools": "WebFetch", "append_system_prompt": "Be terse."}
    r = client.post("/api/projects/shop/repos/api/sessions", json=body, headers=H)
    assert r.status_code == 201, r.text
    argv = shlex.split(r.json()["cmd"])
    assert argv[:3] == ["claude", "--session-id", r.json()["claude_session_id"]] and argv[3:5] == ["--name", "dev"]
    for part in (["--model", "fable"], ["--effort", "high"], ["--permission-mode", "acceptEdits"],
                 ["--allowedTools", "Bash(npm test)", "Read"], ["--disallowedTools", "WebFetch"], ["--append-system-prompt", "Be terse."]):
        i = argv.index(part[0])
        assert argv[i:i + len(part)] == part, part
    assert client.post("/api/projects/shop/repos/api/sessions", json={"launcher": "claude", "effort": "extreme"}, headers=H).status_code == 400
    assert client.post("/api/projects/shop/repos/api/sessions", json={"launcher": "claude", "model": "opus; rm -rf /"}, headers=H).status_code == 400
    r = client.post("/api/projects/shop/repos/api/sessions", json={"launcher": "claude", "permission_mode": "bypassPermissions", "name": "byp"}, headers=H)
    assert r.status_code == 201 and "--permission-mode bypassPermissions" in r.json()["cmd"]
    assert client.post("/api/projects/shop/repos/api/sessions", json={"launcher": "claude", "allowed_tools": "Bash(`id`)"}, headers=H).status_code == 400
    r = client.post("/api/projects/shop/repos/api/tasks", json={"title": "t1", "prompt": "do it", "model": "sonnet", "effort": "low", "permission_mode": "plan"}, headers=H)
    assert r.status_code == 201, r.text
    sent = shlex.split(fake_tmux["sent"][-1][1])
    assert sent[1:7] == ["--model", "sonnet", "--effort", "low", "--permission-mode", "plan"] and sent[-1] == "do it" and sent[-2] == "--" and sent[-4] == "--session-id"
    assert client.post("/api/projects/shop/repos/api/tasks", json={"title": "t2", "prompt": "p", "permission_mode": "bypassPermissions"}, headers=H).status_code == 400


def test_state_carries_shell_version(client, projects_dir):
    from app import main
    v = client.get("/api/state", headers=H).json()["version"]
    assert isinstance(v, str) and len(v) == 12 and v == main.ASSET_VERSION == main.asset_version()
