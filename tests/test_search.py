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


# ---------------------------------------------------------------- Codex rollouts and claude-mem observer sessions

T1 = "019e1234-aaaa-7bbb-8ccc-0123456789ab"            # a Codex thread id
META = {"timestamp": "2026-10-04T01:00:00.000Z", "type": "session_meta",
        "payload": {"id": T1, "cwd": "/srv/projects/shop/api", "originator": "codex-tui", "source": "cli", "cli_version": "0.160.0",
                    "base_instructions": {"text": "SECRET-BASE-INSTRUCTIONS " * 40}, "creator_account_id": "acct-1"}}


def ev(kind, message, ts="2026-10-04T01:00:05.000Z"):
    return {"timestamp": ts, "type": "event_msg", "payload": {"type": kind, "message": message}}


def test_rollout_text_reads_messages_only():
    assert search.rollout_text(ev("user_message", "Fix the flaky checkout test")) == ("user", "Fix the flaky checkout test")
    assert search.rollout_text(ev("agent_message", "Patched checkout.py")) == ("assistant", "Patched checkout.py")
    for skip in (ev("agent_reasoning", "thinking about checkout"), ev("token_count", "x"), ev("user_message", "   "), META,
                 {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "# AGENTS.md instructions"}]}},
                 {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": "secret-tool-call"}},
                 {"type": "event_msg", "payload": "nope"}, {"type": "event_msg", "payload": {"type": "user_message", "message": 5}}):
        assert search.rollout_text(skip) is None
    assert len(search.rollout_text(ev("user_message", "x" * 50_000))[1]) == search.MAX_TEXT


def test_codex_rollouts_are_indexed_incrementally_under_the_thread_id(client, codex_home, tmp_path):
    from app import main
    f = codex_home / "sessions" / "2026" / "10" / "04" / f"rollout-2026-10-04T01-00-00-{T1}.jsonl"
    write_lines(f, [META, ev("user_message", "Refactor the checkout flow"),
                    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Refactor the checkout flow"}]}},
                    {"timestamp": "x", "type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": "secret-tool-call checkout"}},
                    ev("agent_message", "Checkout refactor done in checkout.py")])
    idx = search.Indexer(main.db, tmp_path / "no-claude-projects")
    assert idx.index_once() == 2 and idx.index_once() == 0
    hits = search.search(main.db, "checkout")
    assert len(hits) == 2 and {h["kind"] for h in hits} == {"user", "assistant"}
    assert all(h["session_id"] == T1 and h["cwd"] == "/srv/projects/shop/api" and h["file"] == str(f) for h in hits)
    assert not any("secret" in h["snippet"].lower() or "instructions" in h["snippet"].lower() for h in hits)
    assert search.search(main.db, "SECRET-BASE-INSTRUCTIONS") == [], "the first line's base_instructions are never indexed"
    # an append after the first pass starts mid-file: the id still comes from the file name, the cwd from the cached first line
    write_lines(f, [ev("user_message", "Also add a checkout retry", ts="2026-10-04T01:05:00.000Z")])
    assert idx.index_once() == 1
    new = [h for h in search.search(main.db, "retry")]
    assert len(new) == 1 and new[0]["session_id"] == T1 and new[0]["cwd"] == "/srv/projects/shop/api"
    assert search.agent_of(str(f)) == "codex" and search.agent_of("/home/u/.claude/projects/x/y.jsonl") == "claude"
    # a file that is not a rollout under the sessions dir is not read as one
    write_lines(codex_home / "sessions" / "2026" / "10" / "04" / "notes.jsonl", [ev("user_message", "stray checkout note")])
    idx.index_once()
    assert len(search.search(main.db, "stray")) == 0


def test_api_search_names_the_agent_and_the_project_of_a_codex_hit(client, codex_home, projects_dir, tmp_path, fake_tmux):
    from app import main
    (projects_dir / "shop" / "api").mkdir(parents=True)
    f = codex_home / "sessions" / "2026" / "10" / "04" / f"rollout-2026-10-04T01-00-00-{T1}.jsonl"
    write_lines(f, [{**META, "payload": {**META["payload"], "cwd": str(projects_dir / "shop" / "api" / "src")}}, ev("user_message", "Investigate the pricing bug")])
    pdir = tmp_path / "claude" / "projects" / "-srv-projects-shop-api"
    write_lines(pdir / "11111111-1111-4111-8111-111111111111.jsonl",
                [{"type": "user", "message": {"role": "user", "content": "Investigate the pricing page"}, "sessionId": "11111111-1111-4111-8111-111111111111"}])
    idx = search.Indexer(main.db, tmp_path / "claude" / "projects")
    assert idx.index_once() == 2
    main.indexer = idx
    r = client.get("/api/search?q=pricing", headers=H).json()["results"]
    by = {x["agent"]: x for x in r}
    assert set(by) == {"codex", "claude"}
    assert by["codex"]["project"] == "shop" and by["codex"]["repo"] == "api" and by["codex"]["session_id"] == T1
    assert by["claude"]["project"] is None, "a Claude hit the board did not start is left as it was"
    # a thread the board owns resolves through its row, whatever its cwd
    main.db.add_session(tmux_name="blog--site--s1", project="blog", repo="site", name="s1", launcher="resume", claude_session_id=T1, agent="codex")
    again = {x["agent"]: x for x in client.get("/api/search?q=pricing", headers=H).json()["results"]}
    assert again["codex"]["project"] == "blog" and again["codex"]["tmux"] == "blog--site--s1"


def test_observer_sessions_are_not_indexed_and_old_entries_are_pruned(client, tmp_path):
    from app import main
    pdir = tmp_path / "claude" / "projects"
    real = pdir / "-srv-projects-shop-api" / "11111111-1111-4111-8111-111111111111.jsonl"
    obs1 = pdir / "-home-x--claude-mem-observer-sessions-12" / "99999999-9999-4999-8999-999999999999.jsonl"
    obs2 = pdir / "observer-sessions" / "88888888-8888-4888-8888-888888888888.jsonl"
    line = lambda text: {"type": "user", "message": {"role": "user", "content": text}, "timestamp": "2026-10-04T01:00:00Z"}
    write_lines(real, [line("observed work: fix checkout")])
    write_lines(obs1, [line("observer chatter checkout one")])
    write_lines(obs2, [line("observer chatter checkout two")])
    idx = search.Indexer(main.db, pdir)
    assert sorted(p.name for p in idx.files()) == [real.name]
    assert idx.index_once() == 1 and [h["snippet"] for h in search.search(main.db, "chatter")] == []
    # an index built by an older version that did take them is cleaned once, on the first pass
    with main.db.lock:
        main.db.conn.execute("INSERT INTO transcript_fts(text, session_id, cwd, ts, kind, file, line) VALUES (?,?,?,?,?,?,?)",
                             ("old observer chatter checkout", "9", "", "", "user", str(obs1), 1))
        main.db.conn.execute("INSERT INTO fts_files(path, offset, mtime) VALUES (?,?,?)", (str(obs1), 10, 1.0))
    assert len(search.search(main.db, "chatter")) == 1
    again = search.Indexer(main.db, pdir)
    again.index_once()
    assert search.search(main.db, "chatter") == [] and len(search.search(main.db, "checkout")) == 1
