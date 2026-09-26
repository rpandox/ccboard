import os
import stat
import subprocess
from pathlib import Path

import pytest

from app import gitops

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


def sh(cwd, *args):
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True).stdout


def make_repo(tmp_path):
    """repo with a bare origin, one commit on main, and a worktree branch with one commit."""
    origin = tmp_path / "origin.git"
    sh(tmp_path, "git", "init", "-q", "--bare", str(origin))
    repo = tmp_path / "repo"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    sh(repo, "git", "config", "user.email", "t@example.com"); sh(repo, "git", "config", "user.name", "t")
    (repo / "README.md").write_text("hello\n")
    sh(repo, "git", "add", "."); sh(repo, "git", "commit", "-q", "-m", "init")
    sh(repo, "git", "remote", "add", "origin", str(origin)); sh(repo, "git", "push", "-q", "-u", "origin", "main")
    wt = repo / ".claude" / "worktrees" / "add-login"
    sh(repo, "git", "worktree", "add", "-q", "-b", "worktree-add-login", str(wt), "main")
    sh(wt, "git", "config", "user.email", "t@example.com"); sh(wt, "git", "config", "user.name", "t")
    (wt / "login.py").write_text("print('login')\n")
    sh(wt, "git", "add", "."); sh(wt, "git", "commit", "-q", "-m", "add login page")
    return repo, wt, origin


def fake_bin(tmp_path, name, script):
    d = tmp_path / "bin"; d.mkdir(exist_ok=True)
    p = d / name; p.write_text(script); p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def test_task_diff(tmp_path):
    repo, wt, _ = make_repo(tmp_path)
    d = gitops.task_diff(wt, "main")
    assert d["base"] == "origin/main" and d["branch"] == "worktree-add-login"
    assert d["commits"] and "add login page" in d["commits"][0] and d["files"] == ["login.py"]
    assert "+print('login')" in d["committed"] and d["uncommitted"] == "" and d["files_uncommitted"] == []
    (wt / "login.py").write_text("print('login2')\n"); (wt / "new.txt").write_text("x")
    d = gitops.task_diff(wt, "main")
    assert "login2" in d["uncommitted"] and d["files_uncommitted"] == ["login.py", "new.txt"] and "+x" in d["uncommitted"]
    with pytest.raises(gitops.GitError):
        gitops.task_diff(tmp_path / "nope", "main")


def test_describe_with_fake_claude(tmp_path, monkeypatch):
    repo, wt, _ = make_repo(tmp_path)
    fake = fake_bin(tmp_path, "claude", "#!/bin/sh\ncat > /dev/null\necho 'Add login page'\necho\necho '## Summary'\necho '- adds login.py'\n")
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: str(fake))
    r = gitops.describe(wt, "main", "Add login", "please")
    assert r["title"] == "Add login page" and r["body"].startswith("## Summary") and r["files"] == ["login.py"]


def test_pr_create_and_merge_with_fake_gh(tmp_path, monkeypatch):
    repo, wt, origin = make_repo(tmp_path)
    log = tmp_path / "gh.log"
    gh = fake_bin(tmp_path, "gh", f"#!/bin/sh\necho \"$@\" >> {log}\ncase \"$1 $2\" in\n 'pr view') exit 1;;\n 'pr create') echo 'https://github.com/o/r/pull/42';;\n 'pr merge') echo 'Merged pull request #42';;\nesac\n")
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    r = gitops.pr_create(wt, "worktree-add-login", "main", "Add login page", "body text")
    assert r == {"url": "https://github.com/o/r/pull/42", "number": 42, "existing": False}
    assert "pr create --title Add login page --body-file" in log.read_text() and "--base main --head worktree-add-login" in log.read_text()
    assert "worktree-add-login" in sh(origin, "git", "branch")            # pushed for real to the bare origin
    assert "Merged" in gitops.pr_merge(repo, 42, "squash") and "pr merge 42 --squash --delete-branch" in log.read_text()
    with pytest.raises(gitops.GitError):
        gitops.pr_merge(repo, 42, "fast-forward")


def test_task_routes(client, projects_dir, fake_tmux, tmp_path, monkeypatch):
    """diff/describe/pr/merge through the API on a real repo with fake claude and gh."""
    import shutil
    from app import main
    repo, wt, origin = make_repo(tmp_path)
    dest = projects_dir / "shop" / "api"
    dest.parent.mkdir()
    shutil.move(str(repo), str(dest))
    wt = dest / ".claude" / "worktrees" / "add-login"
    sh(dest, "git", "worktree", "repair", str(wt))
    fake_claude = fake_bin(tmp_path, "claude", "#!/bin/sh\ncat > /dev/null\necho 'PR title from claude'\necho\necho 'body'\n")
    fake_bin(tmp_path, "gh", "#!/bin/sh\ncase \"$1 $2\" in\n 'pr view') exit 1;;\n 'pr create') echo 'https://github.com/o/r/pull/7';;\n 'pr merge') echo merged;;\nesac\n")
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: str(fake_claude))
    tid = main.db.task_add(project="shop", repo="api", slug="add-login", title="Add login", prompt="p", branch="worktree-add-login",
                           base="main", worktree=str(wt), tmux_name="shop--api--t-add-login", claude_session_id=None)
    d = client.get(f"/api/tasks/{tid}/diff", headers=H).json()
    assert d["files"] == ["login.py"] and d["commits"]
    r = client.post(f"/api/tasks/{tid}/describe", headers=H).json()
    assert r["title"] == "PR title from claude"
    r = client.post(f"/api/tasks/{tid}/pr", headers=H, json={"title": r["title"], "body": r["body"]}).json()
    assert r["number"] == 7
    st = client.get("/api/state", headers=H).json()
    task = next(x for x in st["tasks"] if x["id"] == tid)
    assert task["column"] == "pr" and task["pr_url"].endswith("/pull/7")
    (wt / "login.py").write_text("dirty\n")
    assert client.post(f"/api/tasks/{tid}/merge", headers=H, json={"method": "squash", "force": False}).status_code == 409
    r = client.post(f"/api/tasks/{tid}/merge", headers=H, json={"method": "squash", "force": True}).json()
    assert r["merged"] == tid and r["worktree_removed"] and not wt.exists()
    assert all(x["id"] != tid for x in client.get("/api/state", headers=H).json()["tasks"])  # archived
    assert client.get("/api/tasks/999/diff", headers=H).status_code == 404
