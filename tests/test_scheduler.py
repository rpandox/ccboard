import json
import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

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


# ---------------------------------------------------------------------------------------------------------------------------------
# v0.5.16: schedules and batches per agent, Codex headless runs (`codex exec --json`). Nothing here runs the real codex: the binary is a
# shell script that answers the probes like conftest's fake and, for `exec --json`, replays a fixture from tests/fixtures/codex_exec/.
# ---------------------------------------------------------------------------------------------------------------------------------
import pytest

from app import agents
from app.agents import codex as codex_agent
from tests.conftest import ROOT, write_fake_codex

FIX = ROOT / "tests" / "fixtures" / "codex_exec"
SID = "019a4f3c-7b1e-7c2a-9d55-3f0e2b6a1c44"

CODEX_EXEC = r'''#!/bin/sh
# `codex exec --json ... -o FILE -- <prompt>`: record the line, the cwd and two env vars, write the -o file, replay a fixture.
if [ "$1" = exec ] && [ "$2" = --json ]; then
  printf '%s\n' "$*" > ARGS
  printf '%s|%s\n' "$CCBOARD_SESSION" "$CODEX_HOME" > ENV
  out=""; prev=""
  for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
  case "$*" in
    *ratelimit*) cat "{fx}/rate_limit.jsonl"; exit 1;;
    *failrun*) cat "{fx}/turn_failed.jsonl"; exit 1;;
    *nofile*) cat "{fx}/success.jsonl"; exit 0;;
  esac
  [ -n "$out" ] && printf 'Final answer from the -o file.\n' > "$out"
  cat "{fx}/success.jsonl"; exit 0
fi
exec "{base}" "$@"
'''


def fake_codex_exec(tmp_path, **kw):
    """A codex stand-in: conftest's probe fake at fakebin/codex-base, wrapped by the exec replayer at fakebin/codex."""
    base = write_fake_codex(tmp_path / "fakebin" / "codex-base", **kw)
    p = tmp_path / "fakebin" / "codex"
    p.write_text(CODEX_EXEC.format(fx=FIX, base=base))
    p.chmod(0o755)
    return p


@pytest.fixture
def codex_box(tmp_path, monkeypatch, projects_dir, fake_tmux):
    """A board with a repo shop/api and a fake codex that is installed and logged in; yields (main, repo, path)."""
    from app import main
    repo = make_repo(projects_dir)
    path = fake_codex_exec(tmp_path)
    monkeypatch.setattr(main.settings, "codex_bin", lambda: str(path))
    monkeypatch.setattr(main.settings, "claude_bin", lambda: str(fake_claude(tmp_path)))
    codex_agent.reset_caches()
    monkeypatch.setattr(main, "sched", None)                      # the tests tick the worker by hand
    return main, repo, path


def run_all(w):
    for t in list(w.running.values()):
        t.join(timeout=30)


def post_job(client, **body):
    return client.post("/api/projects/shop/repos/api/jobs", headers=H, json={"name": "review", "prompt": "review the diff", **body})


def test_codex_headless_argv_per_permission_mode(codex_box, tmp_path):
    main, repo, _ = codex_box
    out = tmp_path / "runs" / "7.txt"
    base = {"name": "n", "prompt": "do it"}
    for mode, sandbox in (("default", "workspace-write"), ("acceptEdits", "workspace-write"), ("auto", "workspace-write"),
                          ("dontAsk", "workspace-write"), ("plan", "read-only")):
        argv = scheduler.codex_command({**base, "permission_mode": mode}, repo, out, [])
        assert argv[:5] == ["codex", "exec", "--json", "-s", sandbox], mode
        assert argv[argv.index("-o") + 1] == str(out)
        assert argv[-3:] == ["--skip-git-repo-check", "--", "do it"]
        for never in ("--ephemeral", "--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto", "--dangerously-bypass-hook-trust", "on-request", "-C"):
            assert never not in argv, (mode, never)
    full = scheduler.codex_command({**base, "permission_mode": "plan", "opts": json.dumps({"model": "gpt-5.5", "reasoning_effort": "high"}),
                                    "args": None}, repo, out, ["--oss"])
    assert full == ["codex", "exec", "--json", "-s", "read-only", "-o", str(out), "-m", "gpt-5.5", "-c", 'model_reasoning_effort="high"', "--oss",
                    "--skip-git-repo-check", "--", "do it"]
    assert scheduler.codex_command({**base, "permission_mode": "plan"}, repo, out, ["--skip-git-repo-check"]).count("--skip-git-repo-check") == 1
    assert scheduler.job_opts({"opts": "not json"}) == {} and scheduler.job_opts({"opts": '["x"]'}) == {} and scheduler.job_opts({}) == {}


def test_parse_headless_on_the_event_fixtures():
    ag = agents.get("codex")
    ok = ag.parse_headless((FIX / "success.jsonl").read_text(), "", 0)
    assert (ok["text"], ok["session_id"], ok["turns"], ok["is_error"], ok["subtype"], ok["rate_limited"], ok["cost"]) == (
        "Reviewed 3 files; no problems found (stream copy).", SID, 1, False, "success", False, None)
    assert ok["usage"] == {"input_tokens": 1200, "cached_input_tokens": 800, "output_tokens": 45}
    bad = ag.parse_headless((FIX / "turn_failed.jsonl").read_text(), "", 1)
    assert (bad["is_error"], bad["subtype"], bad["rate_limited"], bad["text"], bad["session_id"]) == (True, "turn_failed", False, "context window exceeded", SID)
    lim = ag.parse_headless((FIX / "rate_limit.jsonl").read_text(), "", 1)
    assert lim["is_error"] and lim["rate_limited"] and "usage limit" in lim["text"]
    assert scheduler._last_message(Path("/nonexistent/none.txt")) == ""                       # a missing -o file is '' (the stream's message is used)


def test_a_codex_job_runs_in_a_managed_worktree_and_resumes_by_thread_id(client, codex_box, tmp_path, fake_tmux):
    main, repo, _ = codex_box
    assert post_job(client, agent="codex", max_turns=5).status_code == 400
    assert "max_turns: not supported by codex" in post_job(client, agent="codex", max_turns=5).json()["error"]
    assert "max_budget_usd: not supported by codex" in post_job(client, agent="codex", max_budget_usd=1).json()["error"]
    for bad in ("-c x=1", "--profile p", "--dangerously-bypass-approvals-and-sandbox", "--yolo", "-m gpt-5.5", "--sandbox danger-full-access"):
        assert post_job(client, agent="codex", args=bad).status_code == 400, bad
    assert post_job(client, agent="codex", permission_mode="bypassPermissions").status_code == 400
    assert post_job(client, agent="codex", model="x y").status_code == 400
    assert post_job(client, agent="codex", reasoning_effort="ultra").status_code == 400
    assert post_job(client, agent="gemini").status_code == 400
    j = post_job(client, agent="codex", model="gpt-5.5", reasoning_effort="high", args="--oss", permission_mode="plan")
    assert j.status_code == 201 and j.json()["agent"] == "codex", j.text
    row = main.db.job_get(j.json()["id"])
    assert row["agent"] == "codex" and json.loads(row["opts"]) == {"model": "gpt-5.5", "reasoning_effort": "high"} and row["max_turns"] == 30
    w = scheduler.Worker(main.db)
    rid = w.tick()[0]
    run_all(w)
    run = main.db.run_get(rid)
    assert run["status"] == "ok" and run["error"] is None, run
    assert run["result"] == "Final answer from the -o file.", "the -o file is the authoritative last message, not the stream's agent_message"
    assert run["session_id"] == SID and run["num_turns"] == 1 and run["cost_usd"] is None
    slug = run["branch"][len("worktree-"):]
    wt = repo / ".ccboard" / "worktrees" / slug
    assert run["worktree"] == str(wt) and wt.is_dir() and not (repo / ".claude" / "worktrees").exists()
    line = (wt / "ARGS").read_text().strip()
    assert line.startswith("exec --json -s read-only -o ") and f"{tmp_path}/data/runs/{rid}.txt" in line
    assert "-m gpt-5.5" in line and 'model_reasoning_effort="high"' in line and "--oss" in line and "--skip-git-repo-check" in line
    assert line.endswith("-- review the diff") and "--ephemeral" not in line and "bypass" not in line and "--max-turns" not in line
    env_session, env_home = (wt / "ENV").read_text().strip().split("|")
    assert env_session == "none" and env_home == str(main.settings.codex_home), "a headless run is no board session, and it uses the board's CODEX_HOME"
    assert main.db.job_get(row["id"])["last_status"] == "ok"
    task = main.db.task_get(run["task_id"])
    assert (task["agent"], task["branch"], task["worktree"], task["claude_session_id"]) == ("codex", f"worktree-{slug}", str(wt), SID)
    assert task["title"].startswith("[review]") and task["tmux_name"] == f"shop--api--j-{slug}"
    st = client.get("/api/state", headers=H).json()
    assert st["jobs"][0]["agent"] == "codex" and st["jobs"][0]["opts"] == {"model": "gpt-5.5", "reasoning_effort": "high"}
    assert st["runs"][0]["agent"] == "codex" and client.get(f"/api/runs/{rid}", headers=H).json()["agent"] == "codex"
    res = client.post(f"/api/runs/{rid}/resume", headers=H).json()
    assert res["tmux"] == f"shop--api--j-{slug}" and res["attach_url"] == "/term/" + res["tmux"]
    typed = dict(fake_tmux["sent"])[res["tmux"]]
    assert typed.startswith("codex resume ") and typed.endswith(" " + SID) and "--ephemeral" not in typed, "the headless run's thread opens in a terminal"
    assert fake_tmux["created"][-1][1] == str(wt)
    assert main.db.open_rows()[res["tmux"]]["agent"] == "codex"


def test_a_codex_run_without_an_output_file_falls_back_to_the_stream_and_failures_say_why(client, codex_box):
    main, repo, _ = codex_box
    w = scheduler.Worker(main.db)
    results = {}
    for prompt in ("nofile please", "failrun please"):
        post_job(client, agent="codex", prompt=prompt, name=prompt.split()[0])
        rid = w.tick()[0]
        run_all(w)
        results[prompt] = main.db.run_get(rid)
    assert results["nofile please"]["status"] == "ok" and results["nofile please"]["result"] == "Reviewed 3 files; no problems found (stream copy)."
    failed = results["failrun please"]
    assert failed["status"] == "error" and failed["error"] == "turn_failed" and failed["result"] == "context window exceeded"
    assert failed["task_id"], "a failed run still leaves its worktree as a card to look at"


def test_a_codex_that_will_not_start_leaves_no_empty_worktree(client, codex_box, monkeypatch):
    main, repo, path = codex_box
    path.write_text("#!/nonexistent/interpreter\n")                                           # exec fails with OSError (the probes are cached)
    codex_agent.reset_caches()
    monkeypatch.setattr(agents.get("codex"), "auth_status", lambda *a, **k: {"installed": True, "loggedIn": True})
    post_job(client, agent="codex")
    w = scheduler.Worker(main.db)
    rid = w.tick()[0]
    run_all(w)
    run = main.db.run_get(rid)
    assert run["status"] == "error" and run["task_id"] is None and run["worktree"] is None and run["branch"] is None
    assert not (repo / ".ccboard" / "worktrees").exists() or not any((repo / ".ccboard" / "worktrees").iterdir())
    assert subprocess.run(["git", "-C", str(repo), "branch", "--list", "worktree-*"], capture_output=True, text=True).stdout.strip() == ""


def test_a_codex_rate_limit_backs_off_codex_only_until_its_window_resets(client, codex_box):
    main, repo, _ = codex_box
    reset = datetime.now(timezone.utc).timestamp() + 3600
    main.db.kv_set("rate_limits_codex", {"primary": {"used_percent": 40, "window_minutes": 300, "resets_at": reset}, "secondary": None})
    post_job(client, agent="codex", prompt="please ratelimit me")
    w = scheduler.Worker(main.db)
    rid = w.tick()[0]
    run_all(w)
    assert main.db.run_get(rid)["status"] == "rate_limited"
    until = main.db.kv_get("sched_backoff_until_codex")["value"]
    assert until[:16] == datetime.fromtimestamp(reset, tz=timezone.utc).isoformat()[:16], "back off until the Codex window resets"
    assert main.db.kv_get("sched_backoff_until") is None, "Claude's key is untouched"
    q = scheduler.quota_state(main.db, "codex")
    assert q["backoff_until"] == until and scheduler.quota_state(main.db)["backoff_until"] is None
    assert scheduler.quota_blocked(main.db, "codex").startswith("backing off until") and scheduler.quota_blocked(main.db) is None
    # a Claude job still runs while Codex is parked, a Codex job waits (and says why)
    post_job(client, agent="claude", name="claude-one", prompt="claude work")
    post_job(client, agent="codex", name="codex-two", prompt="codex work")
    started = w.tick()
    run_all(w)
    assert len(started) == 1 and main.db.run_get(started[0])["status"] == "ok"
    jobs = {j["name"]: j for j in main.db.jobs()}
    assert jobs["claude-one"]["last_status"] == "ok" and jobs["codex-two"]["last_status"].startswith("deferred: backing off until")
    # and the other way round: a Claude back-off leaves Codex alone
    main.db.kv_del("sched_backoff_until_codex")
    scheduler.set_backoff(main.db, None, "claude")
    assert scheduler.quota_blocked(main.db, "codex") is None and scheduler.quota_blocked(main.db).startswith("backing off")


def test_quota_and_login_branching_per_agent(client, codex_box, monkeypatch):
    main, repo, path = codex_box
    db = main.db
    now = datetime.now(timezone.utc).timestamp()
    assert scheduler.quota_state(db, "codex") == {"pct": None, "at": None, "resets_at": None, "backoff_until": None, "known": False}
    assert scheduler.quota_blocked(db, "codex") is None
    db.kv_set("rate_limits_codex", {"primary": {"used_percent": 84.9, "window_minutes": 300, "resets_at": now + 600}})
    assert scheduler.quota_blocked(db, "codex") is None, "just under 85 %"
    db.kv_set("rate_limits_codex", {"primary": {"used_percent": 91, "window_minutes": 300, "resets_at": now + 600}})
    q = scheduler.quota_state(db, "codex")
    assert q["known"] and q["pct"] == 91.0 and q["resets_at"] == now + 600
    assert scheduler.quota_blocked(db, "codex") == "Codex usage window at 91% (limit 85%)"
    assert scheduler.quota_blocked(db) is None, "Claude's own window decides Claude's runs"
    db.kv_set("rate_limits_codex", {"primary": {"used_percent": 91, "window_minutes": 300, "resets_at": now - 5}})
    assert scheduler.quota_blocked(db, "codex") is None and not scheduler.quota_state(db, "codex")["known"], "a window that already reset is not a reading"
    db.kv_set("rate_limits_codex", {"primary": {"used_percent": 10, "resets_at": now + 600}, "secondary": {"used_percent": 100, "resets_at": now + 5000}})
    assert scheduler.quota_blocked(db, "codex").endswith("(limit 85%)"), "a secondary window that reached 100 % holds too"
    db.kv_del("rate_limits_codex")
    db.kv_set("rate_limits", {"five_hour": {"used_percentage": 91, "resets_at": 1}})
    assert scheduler.quota_blocked(db).startswith("5-hour window at 91%") and scheduler.quota_blocked(db, "codex") is None
    st = client.get("/api/state", headers=H).json()["scheduler"]
    assert st["known"] is True and st["codex"]["known"] is False and st["codex"]["backoff_until"] is None
    # login: codex's own auth_status, not claude's
    assert scheduler.login_blocked("codex") is None and scheduler.login_blocked() is None
    write_fake_codex(path.parent / "codex-base", login="Not logged in")
    codex_agent.reset_caches()
    assert scheduler.login_blocked("codex") == "codex is not logged in on this box (open Settings and log in)"
    assert scheduler.login_blocked() is None, "Claude is still logged in"
    monkeypatch.setattr(main.settings, "codex_bin", lambda: None)
    codex_agent.reset_caches()
    assert scheduler.login_blocked("codex") == "codex is not installed on this box"


def test_a_logged_out_codex_defers_codex_jobs_alerts_once_and_leaves_claude_jobs_running(client, codex_box, monkeypatch):
    main, repo, path = codex_box
    write_fake_codex(path.parent / "codex-base", login="Not logged in")
    codex_agent.reset_caches()
    sent = []
    monkeypatch.setattr(scheduler.notify, "publish", lambda title, body, **kw: sent.append(title))
    post_job(client, agent="codex", name="cx", prompt="codex work")
    post_job(client, agent="claude", name="cl", prompt="claude work")
    w = scheduler.Worker(main.db)
    started = w.tick()
    run_all(w)
    assert len(started) == 1
    jobs = {j["name"]: j for j in main.db.jobs()}
    assert jobs["cl"]["last_status"] == "ok"
    assert jobs["cx"]["last_status"].startswith("deferred: codex is not logged in") and jobs["cx"]["next_run_at"] > main.db_now()
    assert sent == ["ccboard: Codex is logged out"] and main.db.kv_get("sched_login_alerted_codex")["value"] is True
    assert main.db.kv_get("sched_login_alerted") is None or main.db.kv_get("sched_login_alerted")["value"] is False
    main.db.job_update(jobs["cx"]["id"], next_run_at=main.db_now())
    w.tick()
    assert sent == ["ccboard: Codex is logged out"], "one push per outage"
    write_fake_codex(path.parent / "codex-base")
    codex_agent.reset_caches()
    main.db.job_update(jobs["cx"]["id"], next_run_at=main.db_now())
    assert len(w.tick()) == 1
    run_all(w)
    assert main.db.kv_get("sched_login_alerted_codex")["value"] is False


def test_a_batch_runs_with_the_chosen_agent(client, codex_box, projects_dir):
    main, repo, _ = codex_box
    for name in ("web", "docs"):
        r = projects_dir / "shop" / name
        r.mkdir()
        subprocess.run(["git", "-C", str(r), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(r), "-c", "user.email=t@x", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    body = {"prompt": "update the changelog", "repos": ["shop/api", "shop/web", "shop/docs"], "name": "log"}
    assert client.post("/api/batch", headers=H, json={**body, "agent": "gemini"}).status_code == 400
    assert client.post("/api/batch", headers=H, json={**body, "agent": "codex", "max_turns": 9}).status_code == 400
    assert client.post("/api/batch", headers=H, json={**body, "agent": "codex", "args": "-c x=1"}).status_code == 400
    r = client.post("/api/batch", headers=H, json={**body, "agent": "codex", "model": "gpt-5.5", "reasoning_effort": "low"})
    assert r.status_code == 201 and r.json()["agent"] == "codex" and len(r.json()["jobs"]) == 3
    jobs = [main.db.job_get(i) for i in r.json()["jobs"]]
    assert all(j["agent"] == "codex" and json.loads(j["opts"]) == {"model": "gpt-5.5", "reasoning_effort": "low"} and j["cron"] is None for j in jobs)
    w = scheduler.Worker(main.db)
    first = w.tick()
    assert len(first) == scheduler.CAP
    run_all(w)
    second = w.tick()
    run_all(w)
    assert sorted(main.db.run_get(x)["status"] for x in first + second) == ["ok"] * 3
    assert all(main.db.run_get(x)["worktree"].count(".ccboard/worktrees/") == 1 for x in first + second)
    plain = client.post("/api/batch", headers=H, json=body).json()                          # no agent: Claude, exactly as before
    assert plain["agent"] == "claude" and main.db.job_get(plain["jobs"][0])["agent"] == "claude" and main.db.job_get(plain["jobs"][0])["opts"] is None


def test_a_codex_job_needs_codex_installed_and_the_jobs_table_defaults_to_claude(client, codex_box, monkeypatch):
    main, repo, _ = codex_box
    monkeypatch.setattr(main.settings, "codex_bin", lambda: None)
    codex_agent.reset_caches()
    r = post_job(client, agent="codex")
    assert r.status_code == 400 and r.json()["error"] == "codex is not installed on this box"
    r = client.post("/api/batch", headers=H, json={"prompt": "p", "repos": ["shop/api"], "agent": "codex"})
    assert r.status_code == 400 and "codex is not installed" in r.json()["error"]
    jid = post_job(client).json()["id"]                                                      # no agent named: claude, no opts, max_turns 30
    row = main.db.job_get(jid)
    assert (row["agent"], row["opts"], row["max_turns"]) == ("claude", None, 30)
    legacy = main.db.job_add(project="shop", repo="api", name="old", prompt="p")            # a row written without the new columns
    assert main.db.job_get(legacy)["agent"] == "claude" and main.db.job_get(legacy)["opts"] is None
    assert client.get("/api/state", headers=H).json()["jobs"][0]["agent"] == "claude"
