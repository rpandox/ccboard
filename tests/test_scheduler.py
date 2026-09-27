import json
import os
import stat
import subprocess
from datetime import datetime, timezone

from app import scheduler

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}

FAKE_CLAUDE = r'''#!/bin/sh
# fake `claude -p ... --worktree <slug> ...`: create the worktree like the real one and print a JSON result
case "$1" in
  auth) echo '{"loggedIn": true, "email": "t@x", "subscriptionType": "max", "authMethod": "oauth"}'; exit 0;;
  --version) echo "9.9.9 (Claude Code)"; exit 0;;
esac
slug=""; prev=""
for a in "$@"; do [ "$prev" = "--worktree" ] && slug="$a"; prev="$a"; done
git worktree add -q -b "worktree-$slug" ".claude/worktrees/$slug" HEAD >/dev/null 2>&1 || true
printf '%s\n' "$*" > ".claude/worktrees/$slug/ARGS"
case "$*" in
  *ratelimit*) echo '{"type":"result","subtype":"success","is_error":false,"result":"You have hit your usage limit (rate limit)","session_id":"11111111-1111-4111-8111-111111111111","total_cost_usd":0.01,"num_turns":1}';;
  *) echo '{"type":"result","subtype":"success","is_error":false,"result":"Added tests; all green.","session_id":"22222222-2222-4222-8222-222222222222","total_cost_usd":0.42,"num_turns":7}';;
esac
'''


def make_repo(projects_dir):
    r = projects_dir / "shop" / "api"
    r.mkdir(parents=True)
    subprocess.run(["git", "-C", str(r), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(r), "-c", "user.email=t@x", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    return r


def fake_claude(tmp_path):
    d = tmp_path / "bin"; d.mkdir(exist_ok=True)
    p = d / "claude"; p.write_text(FAKE_CLAUDE); p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def test_cron_and_command():
    assert scheduler.valid_cron("30 2 * * *") and not scheduler.valid_cron("every day")
    assert scheduler.next_fire("30 2 * * *", datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)).startswith("2026-09-28T02:30")
    cmd = scheduler.build_command("do it", "job-1", "acceptEdits", 20, 2.5, ["--model", "opus"])
    assert cmd == ["claude", "-p", "do it", "--worktree", "job-1", "--output-format", "json", "--permission-mode", "acceptEdits",
                   "--max-turns", "20", "--max-budget-usd", "2.50", "--model", "opus"]
    r = scheduler.parse_result('noise\n{"result":"ok","session_id":"s","total_cost_usd":1,"num_turns":2,"is_error":false,"subtype":"success"}')
    assert r["text"] == "ok" and r["cost"] == 1 and not r["is_error"]
    assert scheduler.parse_result('{"result":"rate limit hit","is_error":true,"subtype":"error_during_execution"}')["rate_limited"]
    assert scheduler.parse_result("garbage")["is_error"]


def test_job_lifecycle(client, projects_dir, fake_tmux, tmp_path, monkeypatch):
    from app import main
    repo = make_repo(projects_dir)
    monkeypatch.setattr(main.settings, "claude_bin", lambda: str(fake_claude(tmp_path)))
    bad = client.post("/api/projects/shop/repos/api/jobs", headers=H, json={"name": "x", "prompt": "p", "cron": "nope"})
    assert bad.status_code == 400
    assert client.post("/api/projects/shop/repos/api/jobs", headers=H, json={"name": "x", "prompt": "p", "permission_mode": "bypassPermissions"}).status_code == 400
    j = client.post("/api/projects/shop/repos/api/jobs", headers=H, json={"name": "Nightly tests", "prompt": "run tests", "cron": "30 2 * * *"}).json()
    assert j["next_run_at"] > main.db_now()                       # scheduled in the future, not run
    w = scheduler.Worker(main.db)
    assert w.tick() == []
    # run now -> one run, result stored, worktree became a task card
    r = client.post(f"/api/jobs/{j['id']}/run", headers=H).json()
    assert len(r["started"]) == 1 if r["started"] else True
    rid = r["started"][0] if r["started"] else w.tick()[0]
    for t in list(main.sched.running.values() if main.sched else []) + list(w.running.values()):
        t.join(timeout=30)
    run = main.db.run_get(rid)
    assert run["status"] == "ok" and run["cost_usd"] == 0.42 and run["num_turns"] == 7 and "all green" in run["result"]
    assert run["task_id"] and (repo / ".claude" / "worktrees").is_dir()
    task = main.db.task_get(run["task_id"])
    assert task["claude_session_id"].startswith("2222") and task["branch"].startswith("worktree-nightly-tests-") and task["cost_usd"] == 0.42
    args = (repo / ".claude" / "worktrees" / task["slug"] / "ARGS").read_text()
    assert "--permission-mode acceptEdits" in args and "--max-turns 30" in args and "--output-format json" in args
    job = main.db.job_get(j["id"])
    assert job["last_status"] == "ok" and job["next_run_at"] > main.db_now()   # cron rescheduled
    st = client.get("/api/state", headers=H).json()
    assert st["jobs"][0]["name"] == "Nightly tests" and st["runs"][0]["status"] == "ok"
    # resume the run's worktree in a terminal
    res = client.post(f"/api/runs/{rid}/resume", headers=H).json()
    assert res["tmux"].startswith("shop--api--j-nightly-tests-") and dict(fake_tmux["sent"])[res["tmux"]].startswith("claude --resume 2222")
    # one-off job disables itself after running; rate-limit text is detected
    j2 = client.post("/api/projects/shop/repos/api/jobs", headers=H, json={"name": "once", "prompt": "please ratelimit me"}).json()
    rid2 = w.tick()[0]
    for t in list(w.running.values()):
        t.join(timeout=30)
    assert main.db.run_get(rid2)["status"] == "rate_limited" and main.db.job_get(j2["id"])["enabled"] == 0
    # toggle + delete
    assert client.post(f"/api/jobs/{j['id']}/toggle", headers=H).json()["enabled"] == 0
    assert client.delete(f"/api/jobs/{j['id']}", headers=H).status_code == 200


def test_quota_defers(client, projects_dir, fake_tmux, tmp_path, monkeypatch):
    from app import main
    make_repo(projects_dir)
    monkeypatch.setattr(main.settings, "claude_bin", lambda: str(fake_claude(tmp_path)))
    main.db.kv_set("rate_limits", {"five_hour": {"used_percentage": 91, "resets_at": 1}})
    j = client.post("/api/projects/shop/repos/api/jobs", headers=H, json={"name": "later", "prompt": "p"}).json()
    w = scheduler.Worker(main.db)
    assert w.tick() == []
    job = main.db.job_get(j["id"])
    assert job["last_status"].startswith("deferred") and job["next_run_at"] > main.db_now() and job["enabled"] == 1


def test_batch_respects_cap(client, projects_dir, fake_tmux, tmp_path, monkeypatch):
    from app import main
    make_repo(projects_dir)
    for name in ("web", "infra", "docs"):
        r = projects_dir / "shop" / name
        r.mkdir()
        subprocess.run(["git", "-C", str(r), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(r), "-c", "user.email=t@x", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    monkeypatch.setattr(main.settings, "claude_bin", lambda: str(fake_claude(tmp_path)))
    monkeypatch.setattr(main, "sched", None)                     # control ticking from the test
    assert client.post("/api/batch", headers=H, json={"prompt": "p", "repos": []}).status_code == 400
    assert client.post("/api/batch", headers=H, json={"prompt": "p", "repos": ["shop/nope"]}).status_code == 404
    r = client.post("/api/batch", headers=H, json={"prompt": "update deps", "repos": ["shop/api", "shop/web", "shop/infra", "shop/docs"], "name": "deps"}).json()
    assert len(r["jobs"]) == 4 and r["started"] == []
    jobs = main.db.jobs()
    assert all(j["batch_id"] == r["batch_id"] and j["cron"] is None and j["enabled"] == 1 for j in jobs[:4])
    w = scheduler.Worker(main.db)
    first = w.tick()
    assert len(first) == scheduler.CAP                            # only 2 run at once
    for t in list(w.running.values()):
        t.join(timeout=30)
    second = w.tick()
    assert len(second) == 2
    for t in list(w.running.values()):
        t.join(timeout=30)
    assert w.tick() == []
    assert sorted(main.db.run_get(x)["status"] for x in first + second) == ["ok"] * 4
    assert all(j["enabled"] == 0 for j in main.db.jobs()[:4])


def test_extra_args_cannot_escalate(client, projects_dir, fake_tmux, tmp_path, monkeypatch):
    from app import main
    make_repo(projects_dir)
    monkeypatch.setattr(main.settings, "claude_bin", lambda: str(fake_claude(tmp_path)))
    for bad in ("--permission-mode=bypassPermissions", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
                "--settings {\"permissions\":{\"defaultMode\":\"bypassPermissions\"}}", "--permission-mode auto", "--PERMISSION-MODE=auto"):
        r = client.post("/api/projects/shop/repos/api/jobs", headers=H, json={"name": "x", "prompt": "p", "args": bad})
        assert r.status_code == 400, bad
    assert client.post("/api/projects/shop/repos/api/jobs", headers=H, json={"name": "x", "prompt": "p", "cron": "0 3 * * *", "args": "--model opus --add-dir /tmp"}).status_code == 201
    assert scheduler.check_extra_args("--model opus") == ["--model", "opus"]
