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
    git_init(projects_dir / "shop" / "web")   # git creates .git before it fetches: still "cloning"
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


def test_root_project_folder(projects_dir):
    git_init(projects_dir / "multi" / "api")
    git_init(projects_dir / "single")
    (projects_dir / "multi" / "root").mkdir()          # a stray folder with the reserved name is not listed as a repo
    assert projects.repo_path("multi", "root") == projects_dir / "multi"
    with pytest.raises(projects.BadRequest):
        projects.add_repo_blank("multi", "root")
    with pytest.raises(projects.BadRequest):
        projects.prepare_repo_clone("multi", "root", "https://example.com/y.git")
    sess = {"multi--root--s1": {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "claude", "path": str(projects_dir / "multi"), "pid": 1}}
    out = {p["name"]: p for p in projects.scan(sess)}
    root = out["multi"]["root"]
    assert root["root"] is True and root["name"] == "root" and root["state"] == "project" and root["path"] == str(projects_dir / "multi")
    assert [s["name"] for s in root["sessions"]] == ["s1"] and [r["name"] for r in out["multi"]["repos"]] == ["api"]
    assert out["multi"]["orphan_sessions"] == []
    assert out["single"]["root"] is None                 # a folder that is itself the repo needs none


@pytest.fixture
def linked_home(projects_dir, tmp_path, monkeypatch):
    from app.config import settings
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(settings, "link_roots", (home,))
    return home


def test_linked_projects_scan_and_cwds(projects_dir, linked_home):
    from app import links
    target = linked_home / "mono"
    target.mkdir()
    (target / ".git").mkdir()
    p = links.add("mono", None, target)
    other = linked_home / "api"
    other.mkdir()
    projects.create_project("shop")
    r = links.add("shop", "api", other)
    rows = {x["name"]: x for x in projects.scan({})}
    assert rows["mono"]["linked"] and rows["mono"]["target"] == str(target)
    assert rows["shop"]["linked"] is False and rows["shop"]["target"] is None
    assert rows["shop"]["repos"][0]["linked"]
    assert projects.contained(p) == target
    assert projects.contained(r / "sub") == other / "sub"
    assert projects.repo_for_cwd(str(target)) == ("mono", "mono")
    assert projects.repo_for_cwd(str(target / ".claude/worktrees/task")) == ("mono", "mono")
    assert projects.repo_for_cwd(str(other / "sub")) == ("shop", "api")
    assert projects.repo_for_cwd(str(other / ".claude/worktrees/task")) == ("shop", "api")
    with pytest.raises(projects.BadRequest):
        projects.remove_tree(p)
    assert target.is_dir() and (target / ".git").is_dir()
    with pytest.raises(projects.BadRequest):
        projects.remove_tree(r / "sub")
    links.remove("mono", None)
    assert target.is_dir() and not p.exists()


def test_linked_container_cwds_and_nested_links(projects_dir, linked_home):
    from app import links
    target = linked_home / "container"
    (target / "api").mkdir(parents=True)
    p = links.add("group", None, target)
    assert projects.repo_for_cwd(str(target)) == ("group", projects.ROOT)
    assert projects.repo_for_cwd(str(target / "api/sub")) == ("group", "api")
    (target / "escape").symlink_to(linked_home, target_is_directory=True)
    assert [r["name"] for r in projects.scan({})[0]["repos"]] == ["api"]
    with pytest.raises(projects.BadRequest, match="path escapes its linked folder"):
        projects.contained(p / "escape/file")
    with pytest.raises(projects.BadRequest):
        projects.add_repo_blank("group", "new")
    assert not (target / "new").exists()


def test_link_target_rules_and_overlap(projects_dir, linked_home):
    from app import links
    from app.config import settings
    folder = linked_home / "ok"
    folder.mkdir()
    file = linked_home / "file"
    file.write_text("ordinary text")
    denied = linked_home / ".ssh"
    denied.mkdir()
    for path in ("/", str(linked_home), str(denied), str(projects_dir), str(file), "relative", str(linked_home / "missing")):
        with pytest.raises(projects.BadRequest):
            links.check_target(path)
    assert links.check_target("~/ok") == folder
    (folder / "child").mkdir()
    links.add("one", None, folder / "child")
    for path in (folder, folder / "child", folder / "child/deeper"):
        path.mkdir(exist_ok=True)
        with pytest.raises(projects.Conflict):
            links.check_target(str(path))
    assert settings.projects_dir == projects_dir


def test_stray_and_repointed_links_are_untrusted(projects_dir, linked_home):
    from app import links
    a, b = linked_home / "a", linked_home / "b"
    a.mkdir()
    b.mkdir()
    (projects_dir / "stray").symlink_to(a)
    assert projects.scan({}) == []
    p = links.add("registered", None, a)
    p.unlink()
    p.symlink_to(b)
    assert links.trusted(p) is None
    assert projects.scan({}) == []
    assert links.owner_of_real(b) is None
    with pytest.raises(projects.BadRequest):
        projects.contained(p / "file")
    with pytest.raises(projects.BadRequest):
        links.remove("registered", None)
    assert p.is_symlink() and b.is_dir()


def test_target_protected_paths_within_allowed_root(projects_dir, linked_home, tmp_path, monkeypatch):
    from app import links
    from app.config import settings
    monkeypatch.setattr(settings, "link_roots", (tmp_path,))
    for path in (linked_home, projects_dir, projects_dir / "child", settings.data_dir, settings.codex_home):
        # CODEX_HOME comes from the hermetic test bootstrap.
        if path == settings.codex_home:
            path = tmp_path / "custom-codex"
            monkeypatch.setattr(settings, "codex_home", path)
        path.mkdir(parents=True, exist_ok=True)
        with pytest.raises(projects.BadRequest):
            links.check_target(str(path))
    for name in (".ssh", ".gnupg", ".claude", ".codex", ".config", ".local", "Library", ".aws", ".kube", ".docker"):
        path = linked_home / name / "child"
        path.mkdir(parents=True)
        with pytest.raises(projects.BadRequest):
            links.check_target(str(path))


def test_registry_read_is_cached_and_rechecks_link(projects_dir, linked_home, monkeypatch):
    from app import links
    target = linked_home / "target"
    target.mkdir()
    p = links.add("linked", None, target)
    calls = []
    original = links._db().kv_get
    monkeypatch.setattr(links._db(), "kv_get", lambda key: calls.append(key) or original(key))
    assert links.trusted(p) == str(target)
    assert links.trusted(p) == str(target)
    assert calls == ["linked_folders"]
    p.unlink()
    p.symlink_to(linked_home)
    assert links.trusted(p) is None
