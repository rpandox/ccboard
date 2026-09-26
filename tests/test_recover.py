import subprocess

from app import recover

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


def test_plan_and_run(client, projects_dir, fake_tmux, monkeypatch):
    from app import main
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/web"], check=True)
    c1 = client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "claude", "add_dirs": ["shop/web"]}).json()
    c2 = client.post("/api/projects/shop/repos/web/sessions", headers=H, json={"launcher": "continue"}).json()
    sh = client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()
    # a "reboot": tmux forgets everything, the DB still has the rows
    fake_tmux["sessions"].clear()
    fake_tmux["created"].clear()
    rows = main.db.open_rows()
    todo = recover.plan(rows, set())
    by = {t["name"]: t for t in todo}
    assert set(by) == {c1["tmux"], c2["tmux"]}
    assert by[c1["tmux"]]["cmd"][:3] == ["claude", "--resume", c1["claude_session_id"]] and "--add-dir" in by[c1["tmux"]]["cmd"]
    assert by[c2["tmux"]]["cmd"] == ["claude", "--continue"]
    summary = recover.run(main.db, main._start_session)
    assert sorted(summary["recovered"]) == sorted([c1["tmux"], c2["tmux"]]) and summary["closed"] == [sh["tmux"]]
    names = [c[0] for c in fake_tmux["created"]]
    assert sorted(names) == sorted([c1["tmux"], c2["tmux"]])
    typed = dict(fake_tmux["sent"])
    assert typed[c1["tmux"]].startswith("claude --resume " + c1["claude_session_id"])
    rows = main.db.open_rows()
    assert rows[c1["tmux"]]["launcher"] == "recovered" and rows[c1["tmux"]]["claude_session_id"] == c1["claude_session_id"]
    st = client.get("/api/state", headers=H).json()
    assert sorted(st["last_recovery"]["value"]["recovered"]) == sorted([c1["tmux"], c2["tmux"]])
    assert client.post("/api/recovery/dismiss", headers=H).status_code == 200
    assert client.get("/api/state", headers=H).json()["last_recovery"] is None
    # second run: everything is live now -> nothing to do
    assert recover.run(main.db, main._start_session) == {"recovered": [], "closed": [], "skipped": []}


def test_disabled(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "recover", False)
    assert recover.run(None, None) == {"recovered": [], "closed": [], "skipped": []}


def test_task_session_recovers_in_worktree(client, projects_dir, fake_tmux, monkeypatch, tmp_path):
    from app import main
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)
    t = client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": "Fix bug", "prompt": "fix"}).json()
    wt = projects_dir / "shop" / "api" / ".claude" / "worktrees" / "fix-bug"
    wt.mkdir(parents=True)
    fake_tmux["sessions"].clear(); fake_tmux["created"].clear()
    summary = recover.run(main.db, main._start_session)
    assert summary["recovered"] == [t["tmux"]]
    name, cwd, env = fake_tmux["created"][-1]
    assert cwd == str(wt) and dict(fake_tmux["sent"])[name].startswith("claude --resume ")
    assert main.db.open_rows()[name]["launcher"] == "task"
    # a task whose worktree never appeared is closed, not relaunched
    t2 = client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": "Other", "prompt": "x"}).json()
    fake_tmux["sessions"].clear()
    summary = recover.run(main.db, main._start_session)
    assert t2["tmux"] in summary["closed"]
