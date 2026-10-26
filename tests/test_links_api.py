"""Linked folder HTTP contracts; all targets and metadata stay in temporary directories."""
import pytest
from app import main, links
from app.config import Settings, settings

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


@pytest.fixture
def target(lite_client, tmp_path, monkeypatch):
    home = tmp_path / "external"
    home.mkdir()
    monkeypatch.setattr(settings, "link_roots", (home,))
    p = home / "my-folder"
    p.mkdir()
    (p / "hello.txt").write_text("hello")
    return p


def metadata(project, repo):
    db = main.db
    tid = db.task_add(project=project, repo=repo, slug="task", title="Task", prompt="hello")
    jid = db.job_add(project=project, repo=repo, name="job", prompt="hello")
    rid = db.run_start(jid)
    return tid, jid, rid


def assert_forgot(ids):
    tid, jid, rid = ids
    assert main.db.task_get(tid) is None
    assert main.db.job_get(jid) is None
    assert main.db.run_get(rid) is None


def test_preflight(lite_client, target):
    r = lite_client.post("/api/preflight/folder", headers=H, json={"path": str(target)})
    assert r.status_code == 200
    assert r.json() == dict(ok=True, path=str(target), git=False, branch=None, remote=None, repos=[], suggested_name="my-folder")
    bad = lite_client.post("/api/preflight/folder", headers=H, json={"path": "relative"})
    assert bad.status_code == 200 and bad.json()["ok"] is False
    assert lite_client.post("/api/preflight/folder", json={"path": str(target)}).status_code == 403
    assert lite_client.post("/api/preflight/folder", headers={"Tailscale-User-Login": H["Tailscale-User-Login"]}, json={"path": str(target)}).status_code == 403


@pytest.mark.parametrize("repo_link", [False, True])
def test_create_unlink_keeps_target_and_forgets_rows(lite_client, target, fake_tmux, repo_link):
    if repo_link:
        assert lite_client.post("/api/projects", headers=H, json={"name": "shop"}).status_code == 201
        url, body = "/api/projects/shop/repos", {"link": str(target)}
        delete = "/api/projects/shop/repos/my-folder"
        repo = "my-folder"
    else:
        url, body = "/api/projects", {"name": "shop", "link": str(target)}
        delete, repo = "/api/projects/shop", "root"
    r = lite_client.post(url, headers=H, json=body)
    assert r.status_code == 201, r.text
    ids = metadata("shop", repo)
    keep = metadata("other", "repo")
    r = lite_client.delete(delete, headers=H)
    assert r.status_code == 200 and r.json()["unlinked"] is True, r.text
    assert (target / "hello.txt").read_text() == "hello"
    assert_forgot(ids)
    assert main.db.task_get(keep[0]) is not None
    assert main.db.job_get(keep[1]) is not None
    assert main.db.run_get(keep[2]) is not None
    assert links.all() == {}


def test_delete_parent_unlinks_children(lite_client, target, fake_tmux):
    lite_client.post("/api/projects", headers=H, json={"name": "shop"})
    assert lite_client.post("/api/projects/shop/repos", headers=H, json={"name": "api", "link": str(target)}).status_code == 201
    ids = metadata("shop", "api")
    r = lite_client.delete("/api/projects/shop", headers=H)
    assert r.json()["unlinked"] is True
    assert (target / "hello.txt").is_file()
    assert not (settings.projects_dir / "shop").exists()
    assert links.all() == {}
    assert_forgot(ids)


def test_disabled_and_exclusive(lite_client, target, monkeypatch):
    for url in ("/api/projects", "/api/projects/shop/repos"):
        r = lite_client.post(url, headers=H, json={"name": "shop", "link": str(target), "url": "https://example.com/repo"})
        assert r.status_code == 400
    monkeypatch.setattr(settings, "runtime", "docker")
    monkeypatch.setattr(settings, "link_roots", Settings({"CCBOARD_RUNTIME": "docker"}).link_roots)
    for url, body in (("/api/preflight/folder", {"path": str(target)}), ("/api/projects", {"name": "shop", "link": str(target)}), ("/api/projects/shop/repos", {"link": str(target)})):
        r = lite_client.post(url, headers=H, json=body)
        assert r.status_code == 403 and r.json()["error"] == links.OFF


def test_forget_repo_scope(lite_client):
    a, b = metadata("shop", "a"), metadata("shop", "b")
    main.db.forget_repo("shop", "a")
    assert_forgot(a)
    assert main.db.task_get(b[0]) is not None
    main.db.forget_repo("shop")
    assert_forgot(b)


def test_preflight_git_and_sanitized_origin(lite_client, target):
    import subprocess
    subprocess.run(["git", "-C", str(target), "init", "-q", "-b", "main"], check=True)
    # Build userinfo from benign fixture labels; never embed a secret-shaped literal.
    userinfo = ":".join(("example-user", "example-value"))
    subprocess.run(["git", "-C", str(target), "remote", "add", "origin", "https://" + userinfo + "@example.com/team/repo.git"], check=True)
    r = lite_client.post("/api/preflight/folder", headers=H, json={"path": str(target)}).json()
    assert r["ok"] and r["git"] and r["branch"] == "main"
    assert r["remote"] == "https://example.com/team/repo.git"


@pytest.mark.parametrize("runtime", ["host", "launchd", "systemd", "docker"])
@pytest.mark.parametrize("value", [None, "", "  "])
def test_config_link_roots_runtime_defaults(monkeypatch, tmp_path, runtime, value):
    monkeypatch.setenv("HOME", str(tmp_path))
    env = {"CCBOARD_RUNTIME": runtime}
    if value is not None:
        env["CCBOARD_LINK_ROOTS"] = value
    assert Settings(env).link_roots == (() if runtime == "docker" else (str(tmp_path),))


@pytest.mark.parametrize("value", ["off", "OFF", " Off "])
def test_config_link_roots_disabled(monkeypatch, tmp_path, value):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Settings({"CCBOARD_RUNTIME": "host", "CCBOARD_LINK_ROOTS": value}).link_roots == ()


def test_config_explicit_link_roots_in_docker(tmp_path):
    roots = f"{tmp_path}/a:relative:{tmp_path}/b"
    assert Settings({"CCBOARD_RUNTIME": "docker", "CCBOARD_LINK_ROOTS": roots}).link_roots == (str(tmp_path / "a"), str(tmp_path / "b"))


@pytest.mark.parametrize("delete_parent", [True, False])
def test_delete_missing_child_link(lite_client, target, fake_tmux, delete_parent):
    from app import projects
    projects.create_project("shop")
    link = links.add("shop", "api", target)
    ids = metadata("shop", "api")
    link.unlink()
    assert next(p for p in projects.scan({}) if p["name"] == "shop")["repos"] == []
    url = "/api/projects/shop" + ("" if delete_parent else "/repos/api")
    r = lite_client.delete(url, headers=H)
    assert r.status_code == 200, r.text
    assert links.all() == {}
    assert (settings.projects_dir / "shop").exists() is (not delete_parent)
    assert (target / "hello.txt").read_text() == "hello"
    assert_forgot(ids)


@pytest.mark.parametrize("delete_parent", [True, False])
@pytest.mark.parametrize("replacement", ["symlink", "folder"])
def test_delete_changed_child_link_refused(lite_client, target, fake_tmux, delete_parent, replacement):
    from app import projects
    projects.create_project("shop")
    link = links.add("shop", "api", target)
    link.unlink()
    other = target.parent / "other"
    other.mkdir()
    if replacement == "symlink":
        link.symlink_to(other)
    else:
        link.mkdir()
    (link / "keep.txt").write_text("keep")
    ids = metadata("shop", "api")
    url = "/api/projects/shop" + ("" if delete_parent else "/repos/api")
    r = lite_client.delete(url, headers=H)
    assert r.status_code == 400, r.text
    assert (link / "keep.txt").read_text() == "keep"
    assert links.get("shop/api") is not None
    assert main.db.task_get(ids[0]) is not None


def test_repo_validations_before_link(lite_client, target, monkeypatch):
    from app import projects
    projects.create_project("shop")
    for name in ("root", "bad name"):
        r = lite_client.post("/api/projects/shop/repos", headers=H, json={"name": name, "link": str(target)})
        assert r.status_code == 400
    (settings.projects_dir / "shop/taken").mkdir()
    r = lite_client.post("/api/projects/shop/repos", headers=H, json={"name": "taken", "link": str(target)})
    assert r.status_code == 409
    monkeypatch.setattr(projects, "case_collision", lambda parent, name: "Existing")
    r = lite_client.post("/api/projects/shop/repos", headers=H, json={"name": "existing", "link": str(target)})
    assert r.status_code == 400
    assert links.all() == {}
    assert list(target.iterdir()) == [target / "hello.txt"]


def test_preflight_lists_at_most_fifty_child_repos(lite_client, target):
    for i in range(51):
        (target / f"repo{i:02d}" / ".git").mkdir(parents=True)
    (target / "stray").symlink_to(target / "repo00")
    r = lite_client.post("/api/preflight/folder", headers=H, json={"path": str(target)}).json()
    assert len(r["repos"]) == 50 and "stray" not in r["repos"]


def test_other_repo_consumers(lite_client, target, monkeypatch):
    from app import memory_proxy, projects
    from app.agents.codex import CodexAgent
    # Repo hooks are diagnostic reads; stray project links must not be traversed.
    (target / ".git").mkdir()
    (target / ".codex").mkdir()
    (target / ".codex/hooks.json").write_text("{}")
    link = links.add("alias", None, target)
    assert str(link / ".codex/hooks.json") in CodexAgent._repo_hook_files()
    (settings.projects_dir / "stray").symlink_to(target)
    assert not any("stray" in p for p in CodexAgent._repo_hook_files())
    monkeypatch.setattr(memory_proxy, "_env_entries", lambda: [])
    keys = {r["key"] for r in memory_proxy.candidate_keys("alias")}
    assert {"alias", "my-folder"} <= keys
    assert memory_proxy._belongs({"files_read": [str(target / "hello.txt")]}, "alias")
    assert memory_proxy._owners()["my-folder"] == ["alias"]
    assert projects.repo_for_cwd(str(target)) == ("alias", "alias")


def test_disabled_delete_has_no_session_side_effects(lite_client, target, monkeypatch):
    links.add("alias", None, target)
    monkeypatch.setattr(settings, "link_roots", ())
    def refuse(*args):
        raise AssertionError("disabled linking must be checked before killing sessions")
    monkeypatch.setattr(main.tmux, "kill_prefix", refuse)
    r = lite_client.delete("/api/projects/alias", headers=H)
    assert r.status_code == 403
    assert (settings.projects_dir / "alias").is_symlink()
    assert (target / "hello.txt").is_file()
