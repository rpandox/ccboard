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
    assert cmd == ["claude", "--model", "opus", "--add-dir", "/x", "--worktree", "fix-2", "--session-id", "sid", "do it"]


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


def test_create_and_archive_task(lite_client, projects_dir, fake_tmux, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    git_init(projects_dir / "shop" / "api")
    git_init(projects_dir / "shop" / "web")
    r = lite_client.post("/api/projects/shop/repos/api/tasks", headers=H,
                    json={"title": "Add login page", "prompt": "Add a login page with tests.", "add_dirs": ["shop/web"]})
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["slug"] == "add-login-page" and t["tmux"] == "shop--api--t-add-login-page" and t["branch"] == "worktree-add-login-page"
    name, cwd, env = fake_tmux["created"][-1]
    assert cwd == str(projects_dir / "shop" / "api")
    argv = shlex.split(fake_tmux["sent"][-1][1])
    assert argv[0] == "claude" and argv[-1] == "Add a login page with tests." and argv[-5:-1] == ["--worktree", "add-login-page", "--session-id", t["claude_session_id"]] if "claude_session_id" in t else True
    assert "--add-dir" in argv and argv.index("--add-dir") < argv.index("--worktree")      # variadic flags come before the prompt's neighbours
    assert (projects_dir / "shop" / "api" / ".git" / "info" / "exclude").read_text().strip().endswith(".claude/worktrees/")
    st = lite_client.get("/api/state", headers=H).json()
    assert st["tasks"][0]["column"] == "in_progress" and st["tasks"][0]["session"]["state"] == "unknown"
    # same title again -> unique slug
    r2 = lite_client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": "Add login page", "prompt": "again"}).json()
    assert r2["slug"] == "add-login-page-2"
    # validation
    assert lite_client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": " ", "prompt": "x"}).status_code == 400
    assert lite_client.post("/api/projects/shop/repos/nope/tasks", headers=H, json={"title": "a", "prompt": "x"}).status_code == 404
    # archive: no worktree exists (fake claude never made one) -> prune path, session killed, row archived
    a = lite_client.post(f"/api/tasks/{t['id']}/archive", headers=H, json={"force": False}).json()
    assert a["archived"] == t["id"] and t["tmux"] not in fake_tmux["sessions"]
    assert all(x["id"] != t["id"] for x in lite_client.get("/api/state", headers=H).json()["tasks"])
    assert lite_client.post("/api/tasks/999/archive", headers=H, json={}).status_code == 404


# ---- v0.5.4: tasks v2 (phases, unassigned tasks, per-agent worktrees) ------------------------------------------------
def commit_init(path):
    git_init(path)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)


def test_derive_status_phase():
    base = {"pr_url": None, "archived_at": None}
    waiting, working, errored, done = {"state": "waiting"}, {"state": "working"}, {"state": "errored"}, {"state": "done"}
    for phase in ("backlog", "queued"):                      # no session yet (or none that matters): the Backlog column
        for s in (None, working, waiting):
            assert tasks.derive_status({**base, "phase": phase}, s) == "backlog"
    for s in (None, working, done):                          # failed is always the user's problem
        assert tasks.derive_status({**base, "phase": "failed"}, s) == "needs_you"
    for s in (None, working, waiting, errored):              # cancelled is over, whatever the session shows
        assert tasks.derive_status({**base, "phase": "cancelled"}, s) == "done"
    assert tasks.derive_status({**base, "phase": "done"}, None) == "done"
    assert tasks.derive_status({**base, "phase": "done"}, done) == "done"
    assert tasks.derive_status({**base, "phase": "done"}, working) == "done"
    assert tasks.derive_status({**base, "phase": "done"}, waiting) == "needs_you"
    assert tasks.derive_status({**base, "phase": "done"}, errored) == "needs_you"


def test_derive_status_running_and_legacy_rows_use_the_session_logic():
    base = {"pr_url": None, "archived_at": None}
    for phase in ("running", None, ""):                      # legacy rows (no phase key, or the column default) behave as before
        t = {**base, "phase": phase}
        assert tasks.derive_status(t, None) == "done"
        assert tasks.derive_status(t, {"state": "working"}) == "in_progress"
        assert tasks.derive_status(t, {"state": "unknown"}) == "in_progress"
        assert tasks.derive_status(t, {"state": "waiting"}) == "needs_you"
        assert tasks.derive_status(t, {"state": "errored"}) == "needs_you"
        assert tasks.derive_status(t, {"state": "ended"}) == "done"


def test_derive_status_archived_merged_and_pr_beat_phase():
    for phase in ("backlog", "queued", "running", "done", "failed", "cancelled"):
        t = {"phase": phase, "pr_url": None, "archived_at": None}
        assert tasks.derive_status({**t, "archived_at": "x", "pr_url": "u", "pr_state": "MERGED"}, None) == "archived"
        assert tasks.derive_status({**t, "pr_url": "u", "pr_state": "MERGED"}, {"state": "waiting"}) == "merged"
        assert tasks.derive_status({**t, "status": "merged"}, None) == "merged"
        assert tasks.derive_status({**t, "pr_url": "u"}, {"state": "waiting"}) == "pr"
    assert set(tasks.PHASES) == {"backlog", "queued", "running", "done", "failed", "cancelled"}


def test_has_worktree_and_task_worktree(tmp_path):
    assert tasks.has_worktree({"worktree": "/p/r/.claude/worktrees/x"})
    for t in ({"worktree": ""}, {"worktree": None}, {"worktree": "  "}, {}, None):
        assert not tasks.has_worktree(t), t
        assert tasks.task_worktree(t) is None
    assert tasks.task_worktree({"worktree": str(tmp_path / "w")}) == tmp_path / "w"
    # why the guard exists: Path('') is the current directory, so is_dir() is true and git would run in the board's cwd
    from pathlib import Path
    assert Path("").is_dir()


def test_worktree_path_per_agent(tmp_path):
    assert tasks.worktree_path(tmp_path, "fix") == tmp_path / ".claude" / "worktrees" / "fix"
    assert tasks.worktree_path(tmp_path, "fix", "claude") == tmp_path / ".claude" / "worktrees" / "fix"
    assert tasks.worktree_path(tmp_path, "fix", "codex") == tmp_path / ".ccboard" / "worktrees" / "fix"
    assert tasks.worktree_path(tmp_path, "fix", agent="anything-else") == tmp_path / ".ccboard" / "worktrees" / "fix"
    assert tasks.worktree_path(tmp_path, "fix", None) == tmp_path / ".claude" / "worktrees" / "fix"      # legacy rows have no agent


def test_unique_slug_sees_both_worktree_folders(tmp_path):
    (tmp_path / ".ccboard" / "worktrees" / "fix").mkdir(parents=True)
    assert tasks.unique_slug(tmp_path, "fix", set()) == "fix-2"
    (tmp_path / ".claude" / "worktrees" / "fix-2").mkdir(parents=True)
    assert tasks.unique_slug(tmp_path, "fix", set()) == "fix-3"


def test_ensure_excluded_writes_both_folders_once(tmp_path):
    git_init(tmp_path / "r")
    tasks.ensure_excluded(tmp_path / "r")
    tasks.ensure_excluded(tmp_path / "r")
    text = (tmp_path / "r" / ".git" / "info" / "exclude").read_text()
    assert text.count(".claude/worktrees/") == 1 and text.count(".ccboard/worktrees/") == 1
    assert text.strip().endswith(".claude/worktrees/")
    # a repo that only has the old entry gets the new one added, without duplicating the old
    excl = tmp_path / "r2" / ".git" / "info" / "exclude"
    git_init(tmp_path / "r2")
    excl.write_text("*.log\n.claude/worktrees/\n")
    tasks.ensure_excluded(tmp_path / "r2")
    assert excl.read_text() == "*.log\n.claude/worktrees/\n.ccboard/worktrees/\n"
    # a file without a trailing newline is not glued onto
    excl.write_text("*.log")
    tasks.ensure_excluded(tmp_path / "r2")
    assert excl.read_text().splitlines() == ["*.log", ".ccboard/worktrees/", ".claude/worktrees/"]


def test_remove_worktree_per_agent(tmp_path):
    repo = tmp_path / "r"
    commit_init(repo)
    wt = tasks.worktree_path(repo, "x", "codex")
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "br-x", str(wt)], check=True)
    assert wt.is_dir()
    assert tasks.remove_worktree(repo, "x") is None            # the default only knows Claude's folder: nothing there, nothing removed
    assert wt.is_dir()
    assert tasks.remove_worktree(repo, "x", agent="codex") is None
    assert not wt.exists()
    wt2 = tasks.worktree_path(repo, "y", "codex")              # or hand over the stored path
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "br-y", str(wt2)], check=True)
    assert tasks.remove_worktree(repo, "y", force=True, path=wt2) is None
    assert not wt2.exists()
    wt3 = tasks.worktree_path(repo, "z")                       # Claude's folder through the default, as before
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "br-z", str(wt3)], check=True)
    assert tasks.remove_worktree(repo, "z") is None and not wt3.exists()


def test_tasks_module_does_not_import_agents():
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path(tasks.__file__).read_text())
    mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
           {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m == "agents" or m.startswith(("agents.", "app.agents")) for m in mods), mods
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 1 and not n.module for a in n.names}
    assert "agents" not in imported, imported
