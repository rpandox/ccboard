import json
import subprocess

from app import clonequeue, github

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
SAMPLE = [{"name": "api", "url": "https://github.com/o/api", "sshUrl": "git@github.com:o/api.git", "isPrivate": True, "isFork": False, "isArchived": False, "updatedAt": "2026-01-01T00:00:00Z", "description": "the api"},
          {"name": "web", "url": "https://github.com/o/web", "sshUrl": "git@github.com:o/web.git", "isPrivate": False, "isFork": True, "isArchived": False, "updatedAt": None, "description": None}]


def test_list_repos_protocols(monkeypatch):
    calls = []
    def fake_gh(*args, timeout=60):
        calls.append(args)
        if args[:3] == ("config", "get", "git_protocol"):
            return fake_gh.proto + "\n"
        return json.dumps(SAMPLE)
    fake_gh.proto = "https"
    monkeypatch.setattr(github, "_gh", fake_gh)
    r = github.list_repos("o")
    assert [x["url"] for x in r] == ["https://github.com/o/api.git", "https://github.com/o/web.git"] and r[0]["private"] and r[1]["fork"]
    assert calls[0][:2] == ("repo", "list") and "o" in calls[0] and "--no-archived" in calls[0]
    fake_gh.proto = "ssh"
    assert github.list_repos()[0]["url"] == "git@github.com:o/api.git"


def test_gh_missing(monkeypatch):
    monkeypatch.setattr(github.shutil, "which", lambda _: None)
    try:
        github.list_repos()
        assert False
    except github.GhError as e:
        assert "not installed" in str(e)


def test_bulk_queue_cap(client, projects_dir, fake_tmux, monkeypatch):
    from app import main
    monkeypatch.setattr(github, "_gh", lambda *a, **k: "https\n" if a[:2] == ("config", "get") else json.dumps(SAMPLE))
    r = client.get("/api/github/repos?owner=o", headers=H).json()
    assert len(r["repos"]) == 2 and r["protocol"] == "https"
    assert client.get("/api/github/repos?owner=bad/owner", headers=H).status_code == 400
    repos = [{"name": f"r{i}", "url": f"https://github.com/o/r{i}.git"} for i in range(5)]
    r = client.post("/api/projects/imported/repos/bulk", headers=H, json={"repos": repos})
    assert r.status_code == 202 and (projects_dir / "imported").is_dir()
    live = [n for n in fake_tmux["sessions"] if n.endswith("--clone")]
    assert len(live) == clonequeue.CAP                                  # cap respected
    st = client.get("/api/state", headers=H).json()["clone_queue"]
    assert len(st["queued"]) == 2 and len([d for d in st["done"] if d["status"] == "started"]) == 3
    # clones finish -> next ones start
    for n in live:
        fake_tmux["sessions"].pop(n)
    clonequeue.step(main._launch_clone)
    assert len([n for n in fake_tmux["sessions"] if n.endswith("--clone")]) == 2
    assert client.post("/api/clone-queue/clear", headers=H).status_code == 200
    # bad url in a batch -> 400, nothing queued
    assert client.post("/api/projects/imported/repos/bulk", headers=H, json={"repos": [{"url": "--evil"}]}).status_code == 400


def test_edit_history_query_is_constant_and_only_passes_checked_strings():
    import pytest
    args = github.graphql_edit_history_argv("I_kw1234", "Y3Vyc29yOjEwMA==", "fake-gh")
    assert args == ["fake-gh", "api", "graphql", "--include", "-f", "query=" + github.GQL_EDIT_HISTORY,
                    "-f", "id=I_kw1234", "-f", "cursor=Y3Vyc29yOjEwMA=="]
    assert all(w not in github.GQL_EDIT_HISTORY.lower() for w in github.GQL_WORDS)
    for node, cursor in [("bad id", None), ("I_kw1234", "x\nquery"), ("I_kw1234", "x&y"), ("I_kw1234", "x" * 257), ("I_kw1234", 1)]:
        with pytest.raises(github.GhReadError):
            github.graphql_edit_history_argv(node, cursor)


def test_edit_history_query_runs_through_fake_gh_with_pagination(tmp_path, monkeypatch):
    from tests.fake_github import FakeGitHub
    from tests.gh_server import issue_id
    fake = FakeGitHub(tmp_path).install(monkeypatch)
    k = fake.add_issue("acme/shop", 1)
    for _ in range(101):
        fake.edit_issue(k, by="alice")
    node = "I_kw%d" % issue_id(k)
    status, _, data = github.graphql_edit_history(node)
    assert status == 200 and github.graphql_reason(data) is None
    page = github.parse_edit_history(data, issue_id(k))
    assert page["complete"] and len(page["edits"]) == 100 and page["has_next"]
    status, _, data = github.graphql_edit_history(node, page["next_cursor"])
    page = github.parse_edit_history(data, issue_id(k))
    assert page["complete"] and len(page["edits"]) == 1 and not page["has_next"]
    assert not fake.writes()
