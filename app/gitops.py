"""Git and GitHub operations for tasks: diff, AI PR description (claude -p), gh pr create/view/merge."""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from .config import settings

DIFF_CAP = 500_000       # bytes shown in the viewer
DESCRIBE_CAP = 200_000   # bytes sent to claude -p
PR_URL_RE = re.compile(r"https://github\.com/[^\s]+/pull/(\d+)")


class GitError(Exception):
    pass


def run(args: list[str], cwd: Path, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess:
    try:
        cp = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GitError(f"{args[0]} timed out")
    except FileNotFoundError:
        raise GitError(f"{args[0]} is not installed")
    if check and cp.returncode != 0:
        raise GitError((cp.stderr or cp.stdout).strip()[-400:] or f"{' '.join(args[:2])} failed")
    return cp


def base_ref(wt: Path, base: str) -> str:
    """origin/<base> when it exists (what a PR diffs against), else the local branch."""
    cp = run(["git", "rev-parse", "--verify", "-q", f"origin/{base}"], wt, check=False)
    return f"origin/{base}" if cp.returncode == 0 else base


def task_diff(wt: Path, base: str) -> dict:
    if not wt.is_dir():
        raise GitError("worktree does not exist (yet)")
    ref = base_ref(wt, base)
    committed = run(["git", "diff", f"{ref}...HEAD"], wt, check=False).stdout
    uncommitted = run(["git", "diff", "HEAD"], wt, check=False).stdout
    untracked = [ln for ln in run(["git", "ls-files", "--others", "--exclude-standard"], wt, check=False).stdout.splitlines() if ln]
    for f in untracked[:50]:  # show new files too: git diff HEAD ignores untracked paths
        try:
            if (wt / f).is_file() and (wt / f).stat().st_size < 200_000:
                uncommitted += run(["git", "diff", "--no-index", "--", "/dev/null", f], wt, check=False).stdout
        except OSError:
            continue
    commits = [ln for ln in run(["git", "log", "--oneline", f"{ref}..HEAD"], wt, check=False).stdout.splitlines() if ln]
    files = [ln for ln in run(["git", "diff", "--name-only", f"{ref}...HEAD"], wt, check=False).stdout.splitlines() if ln]
    files_uncommitted = [ln for ln in run(["git", "diff", "--name-only", "HEAD"], wt, check=False).stdout.splitlines() if ln]
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], wt, check=False).stdout.strip()
    return {
        "base": ref, "branch": branch, "commits": commits, "files": files,
        "files_uncommitted": sorted(set(files_uncommitted) | set(untracked)),
        "committed": committed[:DIFF_CAP], "uncommitted": uncommitted[:DIFF_CAP],
        "truncated": len(committed) > DIFF_CAP or len(uncommitted) > DIFF_CAP,
    }


DESCRIBE_PROMPT = (
    "You are writing a GitHub pull request for the change below. Do not use any tools.\n"
    "Reply with exactly this format and nothing else:\n"
    "Line 1: a concise PR title (max 70 characters, imperative mood).\n"
    "Line 2: empty.\n"
    "Then a Markdown body with the sections '## Summary' (2-5 bullets, what and why) and "
    "'## Test plan' (bullets). Mention breaking changes if any.\n\n"
)


def describe(wt: Path, base: str, title_hint: str, prompt_hint: str) -> dict:
    """Ask `claude -p` (bare, no tools, one turn) for a PR title and body from the branch diff."""
    exe = settings.claude_bin()
    if not exe:
        raise GitError("claude is not installed")
    d = task_diff(wt, base)
    if not d["commits"] and not d["committed"]:
        raise GitError("nothing committed on this branch yet (ask Claude to commit first)")
    material = (f"Task: {title_hint}\nRequest: {prompt_hint[:2000]}\n\nCommits:\n" + "\n".join(d["commits"][:50])
                + "\n\nDiff:\n" + d["committed"])[:DESCRIBE_CAP]
    cmd = [exe, "-p", "--bare", "--tools", "", "--max-turns", "1", "--output-format", "text", "--permission-mode", "dontAsk",
           DESCRIBE_PROMPT]
    try:
        cp = subprocess.run(cmd, cwd=str(wt), input=material, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        raise GitError("claude -p timed out")
    if cp.returncode != 0:
        raise GitError((cp.stderr or cp.stdout).strip()[-400:] or "claude -p failed")
    text = cp.stdout.strip()
    if not text:
        raise GitError("claude returned nothing")
    lines = text.splitlines()
    title = lines[0].strip().strip("#").strip().strip('"')[:120] or title_hint
    body = "\n".join(lines[1:]).strip()
    return {"title": title, "body": body, "commits": d["commits"], "files": d["files"]}


def push_branch(wt: Path, branch: str) -> None:
    run(["git", "push", "-u", "origin", branch], wt, timeout=180)


def pr_view(wt: Path, branch: str) -> dict | None:
    cp = run(["gh", "pr", "view", branch, "--json", "number,url,state,isDraft,title,mergeable,mergeStateStatus,reviewDecision,"
              "statusCheckRollup,headRefName,baseRefName"], wt, timeout=60, check=False)
    if cp.returncode != 0:
        return None
    import json
    try:
        return json.loads(cp.stdout or "null")
    except ValueError:
        return None


def remote_head(wt: Path, branch: str) -> str | None:
    cp = run(["git", "ls-remote", "--heads", "origin", branch], wt, timeout=60, check=False)
    line = (cp.stdout or "").strip().split("\n")[0]
    return line.split()[0] if line else None


def local_head(wt: Path) -> str:
    return run(["git", "rev-parse", "HEAD"], wt, check=False).stdout.strip()


def push_and_verify(wt: Path, branch: str) -> str:
    """Push the branch and confirm origin has exactly the local HEAD. Returns the sha."""
    push_branch(wt, branch)
    head = local_head(wt)
    remote = remote_head(wt, branch)
    if not head or remote != head:
        raise GitError(f"origin/{branch} ({(remote or 'missing')[:8]}) does not match the local HEAD ({head[:8]}); not merging")
    return head


def pr_create(wt: Path, branch: str, base: str, title: str, body: str, draft: bool = False) -> dict:
    push_and_verify(wt, branch)  # also syncs new commits when the PR already exists
    existing = pr_view(wt, branch)
    if existing and existing.get("state") == "OPEN":
        return {"url": existing["url"], "number": existing["number"], "existing": True}
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body or "")
        body_file = f.name
    try:
        args = ["gh", "pr", "create", "--title", title[:250], "--body-file", body_file, "--base", base, "--head", branch]
        if draft:
            args.append("--draft")
        cp = run(args, wt, timeout=120)
    finally:
        Path(body_file).unlink(missing_ok=True)
    m = PR_URL_RE.search(cp.stdout + cp.stderr)
    if not m:
        raise GitError("gh pr create gave no PR URL: " + (cp.stdout + cp.stderr).strip()[-200:])
    return {"url": m.group(0), "number": int(m.group(1)), "existing": False}


def pr_merge(wt: Path, number: int, method: str = "squash") -> str:
    if method not in ("squash", "merge", "rebase"):
        raise GitError("merge method must be squash, merge or rebase")
    cp = run(["gh", "pr", "merge", str(number), f"--{method}", "--delete-branch"], wt, timeout=120)
    return (cp.stdout + cp.stderr).strip()[-300:]
