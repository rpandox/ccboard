import subprocess

import pytest

from app import projects


def git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)


def test_scan_layouts(projects_dir):
    git_init(projects_dir / "single")                 # compat: project folder is the repo
    git_init(projects_dir / "multi" / "api")
    git_init(projects_dir / "multi" / "web")
    (projects_dir / "multi" / "plain").mkdir()        # no git
    (projects_dir / "empty").mkdir()
    (projects_dir / ".hidden").mkdir()
    (projects_dir / "bad.name").mkdir()
    (projects_dir / "link").symlink_to(projects_dir / "multi")
    out = {p["name"]: p for p in projects.scan({})}
    assert set(out) == {"single", "multi", "empty"}
    assert [r["name"] for r in out["single"]["repos"]] == ["single"]
    assert out["single"]["repos"][0]["path"] == str(projects_dir / "single")
    assert out["single"]["repos"][0]["state"] == "ok"
    names = {r["name"]: r for r in out["multi"]["repos"]}
    assert set(names) == {"api", "web", "plain"}
    assert names["plain"]["state"] == "nogit"
    assert names["api"]["branch"] == "main" and names["api"]["dirty"] is False
    assert out["empty"]["repos"] == []


def test_scan_groups_sessions_and_cloning(projects_dir):
    git_init(projects_dir / "shop" / "api")
    (projects_dir / "shop" / "web").mkdir()
    (projects_dir / "shop" / "bad").mkdir()
    sessions = {
        "shop--api--s1": {"created": 5, "attached": 1, "command": "claude", "path": "", "launcher": "claude"},
        "shop--web--clone": {"created": 6, "attached": 0, "command": "git", "path": "", "launcher": "clone"},
        "shop--bad--clone": {"created": 8, "attached": 0, "command": "zsh", "path": "", "launcher": "clone"},
        "shop--gone--s1": {"created": 7, "attached": 0, "command": "zsh", "path": "", "launcher": "shell"},
    }
    out = {p["name"]: p for p in projects.scan(sessions)}
    repos = {r["name"]: r for r in out["shop"]["repos"]}
    assert repos["api"]["sessions"][0]["tmux"] == "shop--api--s1"
    assert repos["web"]["state"] == "cloning"
    assert repos["bad"]["state"] == "clone-failed"
    assert out["shop"]["orphan_sessions"][0]["repo"] == "gone"


def test_create_and_remove(projects_dir):
    p = projects.create_project("shop")
    assert p.is_dir()
    with pytest.raises(projects.Conflict):
        projects.create_project("shop")
    r = projects.add_repo_blank("shop", "api")
    assert (r / ".git").is_dir()
    with pytest.raises(projects.Conflict):
        projects.add_repo_blank("shop", "api")
    name, path = projects.prepare_repo_clone("shop", None, "https://github.com/o/web.git")
    assert name == "web" and path.is_dir()
    projects.remove_tree(path)
    assert not path.exists()
    with pytest.raises(projects.BadRequest):
        projects.remove_tree(projects_dir)


def test_repo_path_containment(projects_dir):
    with pytest.raises(projects.BadRequest):
        projects.repo_path("..", "x")
    with pytest.raises(projects.BadRequest):
        projects.project_path("a/b")
    outside = projects_dir.parent / "outside"
    outside.mkdir()
    (projects_dir / "shop").mkdir()
    (projects_dir / "shop" / "evil").symlink_to(outside)
    with pytest.raises(projects.BadRequest):
        projects.remove_tree(projects_dir / "shop" / "evil")
    assert outside.exists()
