import json
import os
import stat
import subprocess

from app import prpoll

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


def fake_gh(tmp_path, script):
    d = tmp_path / "bin"; d.mkdir(exist_ok=True)
    p = d / "gh"; p.write_text(script); p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return d


GH = r'''#!/bin/sh
case "$1 $2" in
  "pr view") echo '{"state":"OPEN","url":"https://github.com/o/r/pull/7","isDraft":false,"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","reviewDecision":"APPROVED","title":"t"}';;
  "pr checks") echo '[{"name":"build","state":"SUCCESS","bucket":"pass","link":"x"},{"name":"test","state":"FAILURE","bucket":"fail","link":"y"}]'; exit 0;;
  "run list") echo '[{"databaseId":11,"status":"completed","conclusion":"success","name":"ok"},{"databaseId":12,"status":"completed","conclusion":"failure","name":"CI","workflowName":"CI"}]';;
  "run view") echo "FAIL tests/test_x.py::test_y - AssertionError";;
  "issue list") echo '[{"number":3,"title":"Login broken","body":"steps...","url":"https://github.com/o/r/issues/3","labels":[{"name":"bug"}]}]';;
esac
'''


def test_pr_status_and_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(fake_gh(tmp_path, GH)) + os.pathsep + os.environ["PATH"])
    st = prpoll.pr_status(tmp_path, 7)
    assert st["state"] == "OPEN" and st["review"] == "APPROVED" and st["ci"]["bucket"] == "fail" and len(st["ci"]["checks"]) == 2
    name, log = prpoll.failed_log(tmp_path, "worktree-x")
    assert name == "CI" and "AssertionError" in log
    assert "Closes" not in prpoll.fix_ci_prompt("b", name, log) and "worktree" not in prpoll.fix_ci_prompt("b", name, log).split("\n")[0].replace("worktree-x", "")
    issues = prpoll.list_issues(tmp_path)
    assert issues[0]["number"] == 3 and issues[0]["labels"] == ["bug"]


def test_poller_and_fix_ci_routes(client, projects_dir, fake_tmux, tmp_path, monkeypatch):
    from app import main, tmux as t
    monkeypatch.setenv("PATH", str(fake_gh(tmp_path, GH)) + os.pathsep + os.environ["PATH"])
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)
    wt = projects_dir / "shop" / "api" / ".claude" / "worktrees" / "fix"
    wt.mkdir(parents=True)
    tid = main.db.task_add(project="shop", repo="api", slug="fix", title="Fix", prompt="p", branch="worktree-fix", base="main",
                           worktree=str(wt), tmux_name="shop--api--t-fix", claude_session_id="abc", pr_number=None)
    main.db.task_update(tid, pr_number=7, pr_url="https://github.com/o/r/pull/7", pr_state="OPEN", status="pr")
    poller = prpoll.Poller(main.db, lambda p, r: projects_dir / p / r)
    assert poller.poll_once() == 1
    task = next(x for x in client.get("/api/state", headers=H).json()["tasks"] if x["id"] == tid)
    assert task["ci"]["bucket"] == "fail" and task["pr"]["review"] == "APPROVED" and task["column"] == "pr"
    # fix CI: session gone -> relaunch claude --continue in the worktree, then paste the logs
    pasted = []
    monkeypatch.setattr(t, "paste_text", lambda name, text, enter=True: pasted.append((name, text)))
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main.settings, "claude_bin", lambda: "/fake/claude")
    r = client.post(f"/api/tasks/{tid}/fix-ci", headers=H).json()
    assert r["relaunched"] and r["run"] == "CI"
    assert fake_tmux["created"][-1][0] == "shop--api--t-fix" and fake_tmux["created"][-1][1] == str(wt)
    assert fake_tmux["sent"][-1][1] == "claude --continue" and "AssertionError" in pasted[-1][1]
    assert client.post(f"/api/tasks/{tid}/refresh", headers=H).json()["ci"]["bucket"] == "fail"
    assert client.get("/api/projects/shop/repos/api/issues", headers=H).json()["issues"][0]["title"] == "Login broken"
