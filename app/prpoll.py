"""PR / CI status for tasks with an open PR, polled with gh; "fix CI" re-dispatch; issues list."""
from __future__ import annotations

import json
import logging
import subprocess
import threading
from pathlib import Path

from . import gitops, overlap, tasks as tasks_mod

log = logging.getLogger("ccboard.prpoll")
POLL_SECONDS = 60
LOG_CAP = 20_000


def _gh_json(args: list[str], cwd: Path, timeout: int = 60):
    try:
        cp = subprocess.run(["gh", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise gitops.GitError(f"gh failed: {e.__class__.__name__}")
    # gh pr checks exits 8 while checks are pending but still prints JSON
    if cp.returncode not in (0, 8) or not cp.stdout.strip():
        raise gitops.GitError((cp.stderr or cp.stdout).strip()[-300:] or "gh failed")
    try:
        return json.loads(cp.stdout)
    except ValueError:
        raise gitops.GitError("gh returned no JSON")


def pr_status(cwd: Path, number: int) -> dict:
    """{state, url, is_draft, mergeable, merge_state, review, ci:{bucket, checks:[...]}}"""
    view = _gh_json(["pr", "view", str(number), "--json", "state,url,isDraft,mergeable,mergeStateStatus,reviewDecision,title"], cwd)
    checks = []
    try:
        for c in _gh_json(["pr", "checks", str(number), "--json", "name,state,bucket,link"], cwd) or []:
            checks.append({"name": c.get("name"), "bucket": c.get("bucket"), "state": c.get("state"), "link": c.get("link")})
    except gitops.GitError as e:  # no checks configured -> gh errors; that is not a failure
        if "no checks" not in str(e).lower():
            log.debug("pr checks: %s", e)
    buckets = {c["bucket"] for c in checks}
    bucket = "fail" if "fail" in buckets else "pending" if "pending" in buckets else "pass" if checks else "none"
    return {"state": view.get("state"), "url": view.get("url"), "is_draft": view.get("isDraft"), "mergeable": view.get("mergeable"),
            "merge_state": view.get("mergeStateStatus"), "review": view.get("reviewDecision"), "title": view.get("title"),
            "ci": {"bucket": bucket, "checks": checks}}


def failed_log(cwd: Path, branch: str) -> tuple[str, str]:
    """(run name, tail of the failed job logs) for the latest failed run on the branch."""
    runs = _gh_json(["run", "list", "--branch", branch, "--json", "databaseId,status,conclusion,name,workflowName", "--limit", "10"], cwd)
    failed = next((r for r in runs or [] if r.get("conclusion") == "failure"), None)
    if not failed:
        raise gitops.GitError("no failed run found for this branch")
    try:
        cp = subprocess.run(["gh", "run", "view", str(failed["databaseId"]), "--log-failed"], cwd=str(cwd),
                            capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        raise gitops.GitError("gh run view timed out")
    text = (cp.stdout or cp.stderr).strip()
    if len(text) > LOG_CAP:
        text = "…\n" + text[-LOG_CAP:]
    return failed.get("name") or failed.get("workflowName") or "CI", text


def fix_ci_prompt(branch: str, run_name: str, log_text: str) -> str:
    return (f"CI failed on branch {branch} (workflow: {run_name}). Failing job logs:\n\n{log_text}\n\n"
            "Find the cause, fix it, run the relevant checks locally, commit, and push to update the PR.")


def list_issues(cwd: Path, limit: int = 50) -> list[dict]:
    data = _gh_json(["issue", "list", "--state", "open", "--limit", str(limit), "--json", "number,title,body,url,labels"], cwd)
    return [{"number": i.get("number"), "title": i.get("title"), "body": (i.get("body") or "")[:8000], "url": i.get("url"),
             "labels": [lb.get("name") for lb in (i.get("labels") or []) if isinstance(lb, dict)]} for i in data or []]


class Poller(threading.Thread):
    def __init__(self, db, repo_path_of):
        super().__init__(name="pr-poller", daemon=True)
        self.db = db
        self.repo_path_of = repo_path_of
        self.stop = threading.Event()

    def poll_once(self) -> int:
        n = 0
        for t in self.db.tasks():
            if not t.get("pr_number"):
                continue
            try:
                rpath = self.repo_path_of(t["project"], t["repo"])
                wt = tasks_mod.task_worktree(t)          # None for a task without a worktree: Path('') would be the board's own cwd
                cwd = wt if wt is not None and wt.is_dir() else rpath
                st = pr_status(cwd, int(t["pr_number"]))
            except Exception as e:
                log.debug("pr poll %s: %s", t["id"], e)
                continue
            fields = {"pr_state": st["state"], "pr_json": json.dumps(st), "ci": json.dumps(st["ci"])}
            if st["state"] == "MERGED" and t.get("status") != "merged":
                fields["status"] = "merged"
            self.db.task_update(t["id"], **fields)
            n += 1
        return n

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.poll_once()
            except Exception as e:
                log.warning("pr poll failed: %s", e)
            try:
                overlap.compute(self.db)
            except Exception as e:
                log.warning("overlap check failed: %s", e)
            self.stop.wait(POLL_SECONDS)
