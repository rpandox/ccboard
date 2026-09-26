import json

from app import search

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


def write_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for ln in lines:
            f.write(json.dumps(ln) + "\n")


def test_index_and_search(client, projects_dir, tmp_path, fake_tmux):
    from app import main
    pdir = tmp_path / "claude" / "projects"
    f = pdir / "-srv-projects-shop-api" / "11111111-1111-4111-8111-111111111111.jsonl"
    write_lines(f, [
        {"type": "user", "message": {"role": "user", "content": "Please add a login page with OAuth"}, "sessionId": "11111111-1111-4111-8111-111111111111", "cwd": "/srv/projects/shop/api", "timestamp": "2026-09-27T01:00:00Z"},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "I added login.py with an OAuth callback."}, {"type": "tool_use", "name": "Bash", "input": {"command": "secret-tool-call"}}]}, "sessionId": "11111111-1111-4111-8111-111111111111", "cwd": "/srv/projects/shop/api", "timestamp": "2026-09-27T01:01:00Z"},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "tool output with OAuth token"}]}, "sessionId": "11111111-1111-4111-8111-111111111111"},
        {"type": "progress", "data": "x"},
    ])
    idx = search.Indexer(main.db, pdir)
    assert idx.available
    assert idx.index_once() == 2 and idx.index_once() == 0        # incremental: nothing new the second time
    hits = search.search(main.db, "OAuth")
    assert len(hits) == 2 and all("secret-tool-call" not in h["snippet"] for h in hits)
    assert not any("tool output" in h["snippet"] for h in hits)
    write_lines(f, [{"type": "assistant", "message": {"role": "assistant", "content": "Done: OAuth works now"}, "sessionId": "11111111-1111-4111-8111-111111111111", "timestamp": "2026-09-27T01:02:00Z"}])
    assert idx.index_once() == 1 and len(search.search(main.db, "OAuth")) == 3
    assert search.search(main.db, 'unbalanced "quote') != None                # bad syntax falls back to a phrase, no exception
    assert search.search(main.db, "") == []
    # API joins the ccboard session when the id is known
    import subprocess
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)
    name = client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]
    main.db.set_state(name, None, "SessionStart", claude_session_id="11111111-1111-4111-8111-111111111111")
    main.indexer = idx
    r = client.get("/api/search?q=login", headers=H).json()
    assert r["results"] and r["results"][0]["project"] == "shop" and r["results"][0]["tmux"] == name
