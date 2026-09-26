import shlex
import subprocess

from app import tasks

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


def git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)


def test_slug_and_command(tmp_path):
    assert tasks.slugify("Add login page (with tests)!") == "add-login-page-with-tests"
    assert tasks.slugify("---") == "task"
    assert len(tasks.slugify("x" * 100)) <= 40
    (tmp_path / ".claude" / "worktrees" / "fix").mkdir(parents=True)
    assert tasks.unique_slug(tmp_path, "fix", set()) == "fix-2"
    assert tasks.unique_slug(tmp_path, "new", {"new", "new-2"}) == "new-3"
    cmd = shlex.split(tasks.build_command("fix-2", "sid", "do it", ["--model", "opus"], ["/x"]))
    assert cmd == ["claude", "--worktree", "fix-2", "--session-id", "sid", "--model", "opus", "--add-dir", "/x", "do it"]


def test_exclude_and_default_branch(tmp_path):
    git_init(tmp_path / "r")
    tasks.ensure_excluded(tmp_path / "r")
    tasks.ensure_excluded(tmp_path / "r")
    assert (tmp_path / "r" / ".git" / "info" / "exclude").read_text().count(".claude/worktrees/") == 1
    assert tasks.default_branch(tmp_path / "r") == "main"


def test_derive_status():
    t = {"pr_url": None, "archived_at": None}
    assert tasks.derive_status(t, None) == "done"
    assert tasks.derive_status(t, {"state": "working"}) == "in_progress"
    assert tasks.derive_status(t, {"state": "waiting"}) == "needs_you"
    assert tasks.derive_status(t, {"state": "done"}) == "done"
    assert tasks.derive_status({**t, "pr_url": "u"}, {"state": "working"}) == "pr"
    assert tasks.derive_status({**t, "pr_url": "u", "pr_state": "MERGED"}, None) == "merged"
    assert tasks.derive_status({**t, "archived_at": "x"}, None) == "archived"


def test_create_and_archive_task(client, projects_dir, fake_tmux, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    git_init(projects_dir / "shop" / "api")
    git_init(projects_dir / "shop" / "web")
    r = client.post("/api/projects/shop/repos/api/tasks", headers=H,
                    json={"title": "Add login page", "prompt": "Add a login page with tests.", "add_dirs": ["shop/web"]})
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["slug"] == "add-login-page" and t["tmux"] == "shop--api--t-add-login-page" and t["branch"] == "worktree-add-login-page"
    name, cwd, env = fake_tmux["created"][-1]
    assert cwd == str(projects_dir / "shop" / "api")
    argv = shlex.split(fake_tmux["sent"][-1][1])
    assert argv[:3] == ["claude", "--worktree", "add-login-page"] and argv[-1] == "Add a login page with tests." and "--add-dir" in argv
    assert (projects_dir / "shop" / "api" / ".git" / "info" / "exclude").read_text().strip().endswith(".claude/worktrees/")
    st = client.get("/api/state", headers=H).json()
    assert st["tasks"][0]["column"] == "in_progress" and st["tasks"][0]["session"]["state"] == "unknown"
    # same title again -> unique slug
    r2 = client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": "Add login page", "prompt": "again"}).json()
    assert r2["slug"] == "add-login-page-2"
    # validation
    assert client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": " ", "prompt": "x"}).status_code == 400
    assert client.post("/api/projects/shop/repos/nope/tasks", headers=H, json={"title": "a", "prompt": "x"}).status_code == 404
    # archive: no worktree exists (fake claude never made one) -> prune path, session killed, row archived
    a = client.post(f"/api/tasks/{t['id']}/archive", headers=H, json={"force": False}).json()
    assert a["archived"] == t["id"] and t["tmux"] not in fake_tmux["sessions"]
    assert all(x["id"] != t["id"] for x in client.get("/api/state", headers=H).json()["tasks"])
    assert client.post("/api/tasks/999/archive", headers=H, json={}).status_code == 404
