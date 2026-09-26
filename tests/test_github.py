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
