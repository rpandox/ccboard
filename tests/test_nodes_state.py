"""Nodes epic P4, issue #138: `GET /api/node/state`, the trimmed read-only state a hub reads from every paired node (app/node_state.py).

Pinned here: the shape on a seeded database; that no prompt, reply, result, path or account label is in it (a marker in every place such a thing lives);
the caps (200 sessions and tasks, 200 characters, 100 KB) and the order things are dropped in; the weak ETag (no `now` in it) and the 304; who may call it
(a node token with scope read, a signed-in person, nobody else); that it rides the 2 s scan cache; and the slug of a repo's GitHub origin.
Temp dirs and fakes only: no tmux, no git process, no network.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import login_problem, node_state, nodes

ID = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
MARK = "MARKER-7f3a9c"
KEYS = {"api", "node", "etag_base", "projects", "sessions", "tasks", "needs_you", "usage", "lanes", "login_problems", "truncated"}


def session(state="working", **kw):
    """A session as the project scan holds it, with a marker in every field that must never leave the board."""
    return {"created": 1, "attached": 0, "command": "claude", "path": f"/home/x/{MARK}-path", "pane_id": "%1", "launcher": "task", "cmd": f"claude {MARK}-cmd",
            "state": state, "state_at": "2026-10-10T10:00:00+00:00", "last_event": "Stop", "last_message": f"{MARK}-reply", "last_prompt": f"{MARK}-prompt",
            "stats": {"model": "opus", "cost": 1.5, "cwd": f"/home/x/{MARK}-cwd"}, "needs_attention": False, "agent": "claude", "account": f"{MARK}-account",
            "claude_session_id": f"{MARK}-sid", "add_dirs": [f"/home/x/{MARK}-dir"], "flags": {"registry": {"cwd": f"/home/x/{MARK}-flag"}}, "task": None, **kw}


@pytest.fixture
def board(lite_client, projects_dir, monkeypatch):
    from app import main, projects
    for proj, repo in (("shop", "api"), ("shop", "web"), ("ops", "tools")):
        (projects_dir / proj / repo / ".git").mkdir(parents=True)
    sessions: dict = {}
    calls = SimpleNamespace(scans=0)

    def merged(rich=False):
        calls.scans += 1
        return dict(sessions), False
    monkeypatch.setattr(main, "_merged_sessions", merged)
    monkeypatch.setattr(projects, "git_info_reused", lambda path, max_age: {"branch": "feat/x" if path.name == "web" else "main", "dirty": path.name == "web", "state": "ok"})
    origins = {"api": "git@github.com:acme/api.git", "web": "https://gitlab.com/acme/web.git"}
    monkeypatch.setattr(node_state, "_origin_url", lambda path: origins.get(path.name))
    node_state.slug_cache_clear()
    main._invalidate_scan()
    b = SimpleNamespace(c=lite_client, db=main.db, sessions=sessions, calls=calls, main=main, origins=origins)
    b.get = lambda **kw: lite_client.get("/api/node/state", headers={**ID, **kw.pop("headers", {})}, **kw)
    return b


def task(db, title="Fix login", **kw):
    return db.task_add(project="shop", repo="api", slug=title.lower().replace(" ", "-")[:40] + str(db.conn.execute("select count(*) from tasks").fetchone()[0]),
                       title=title, prompt=f"{MARK}-task-prompt", branch="task/fix", tmux_name="shop--api--fix", phase="running", **kw)


def age_task(db, tid, hours):
    db.conn.execute("update tasks set updated_at=? where id=?", (node_state.iso(nodes._now() - hours * 3600), tid))


# ---------------------------------------------------------------- shape

def test_the_shape_on_a_seeded_board(board):
    board.sessions["shop--api--fix-login"] = session(needs_attention=True, state="waiting")
    board.sessions["shop--web--ui"] = session(agent="codex", launcher="user", stats={"model": "gpt"})
    board.sessions["shop--root--lead"] = session()
    board.sessions["shop--ghost--x"] = session()                                    # a session whose repo folder is gone: an orphan
    tid = task(board.db, "Fix login", issue_number=12, issue_url="https://example.com/i/12", result=f"{MARK}-result")
    board.db.perm_add("shop--api--fix-login", "Bash", f"{MARK}-summary", {"command": "ls"})
    r = board.get()
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    d = r.json()
    assert set(d) == KEYS and d["api"] == 1 and d["truncated"] is False
    assert set(d["node"]) == {"id", "name", "url", "version", "now"} and d["node"]["id"] == nodes.node_id() and d["node"]["now"].endswith("+00:00")
    assert [p["name"] for p in d["projects"]] == ["ops", "shop"]
    shop = next(p for p in d["projects"] if p["name"] == "shop")
    assert shop["repos"] == [{"name": "api", "slug": "acme/api", "branch": "main", "dirty": False},
                             {"name": "web", "slug": None, "branch": "feat/x", "dirty": True}], "slug is owner/name of a GitHub origin, else null"
    by = {s["tmux"]: s for s in d["sessions"]}
    assert set(by) == {"shop--api--fix-login", "shop--web--ui", "shop--root--lead", "shop--ghost--x"}
    assert by["shop--api--fix-login"] == {"tmux": "shop--api--fix-login", "project": "shop", "repo": "api", "session": "fix-login", "agent": "claude", "state": "waiting",
                                         "needs_you": True, "kind": "task", "since": "2026-10-10T10:00:00+00:00", "model": "opus"}
    assert by["shop--web--ui"]["agent"] == "codex" and by["shop--web--ui"]["kind"] == "user" and by["shop--web--ui"]["model"] == "gpt"
    assert by["shop--root--lead"]["repo"] == "root" and by["shop--ghost--x"]["repo"] == "ghost"
    assert d["tasks"] == [{"id": tid, "title": "Fix login", "phase": "running", "agent": "claude", "project": "shop", "repo": "api", "branch": "task/fix",
                           "tmux": "shop--api--fix", "issue_ref": "#12", "updated_at": d["tasks"][0]["updated_at"]}]
    assert d["needs_you"] == {"permissions": 1, "input": 1, "errors": 0}
    assert set(d["usage"]) == {"claude", "codex"} and set(d["usage"]["claude"]) == {"pct", "resets_at", "backoff_until", "known", "limited"}
    assert d["lanes"]["cap"] == 3 and d["lanes"]["running"] == 1 and d["login_problems"] == []


def test_a_login_problem_names_only_the_agent(board):
    login_problem.raise_(board.db, agent="codex", account=f"{MARK}-acct@example.com", session="shop--api--x", message=f"{MARK} 401 unauthorized")
    d = board.get().json()
    assert d["login_problems"] == ["codex"] and MARK not in json.dumps(d)


def test_no_prompt_reply_result_path_or_account_is_in_the_body(board):
    board.sessions["shop--api--fix-login"] = session()
    task(board.db, f"Fix login", result=f"{MARK}-result", spec=json.dumps({"prompt": MARK}))
    board.db.conn.execute("update tasks set worktree=?, claude_session_id=?", (f"/home/x/{MARK}-wt", f"{MARK}-claude"))
    board.db.perm_add("shop--api--fix-login", "Bash", f"{MARK}-summary", {"command": MARK})
    board.db.kv_set("rate_limits", {"five_hour": {"used_percentage": 12, "resets_at": 1800000000}, "account": f"{MARK}-label"})
    r = board.get()
    assert MARK not in r.text, "a marker from a prompt, a reply, a result, a path or an account label reached the payload"
    for forbidden in ("/home/x", str(board.main.settings.projects_dir), "last_prompt", "last_message", "prompt", "result", "transcript", "label", "email", '"path"'):
        assert forbidden not in r.text, forbidden
    assert board.get().json()["usage"]["claude"]["pct"] == 12.0


def test_strings_are_cut_to_200_characters_and_lose_control_characters(board):
    board.sessions["shop--api--fix-login"] = session(stats={"model": "m" * 500})
    tid = task(board.db, "T" * 500)
    board.db.conn.execute("update tasks set branch=? where id=?", ("b\x07\x1b[31m" + "x" * 400, tid))
    d = board.get().json()
    t = d["tasks"][0]
    assert len(t["title"]) == 200 and len(t["branch"]) <= 200 and "\x07" not in t["branch"] and "\x1b" not in t["branch"]
    assert len(d["sessions"][0]["model"]) <= 80
    assert node_state.clean("a b\x00c\x85d  e") == "a b c d e" and node_state.clean(5) is None and node_state.clean("  ") is None


def test_a_session_cap_of_200_and_a_task_cap_of_200_keep_the_live_and_unfinished_first(board):
    for i in range(230):
        board.sessions[f"shop--api--s{i:03d}"] = session("ended" if i < 100 else "working")
    for i in range(260):
        tid = task(board.db, f"done {i}")
        board.db.conn.execute("update tasks set phase='done' where id=?", (tid,))
    live = [task(board.db, f"active {i}") for i in range(5)]
    d = board.get().json()
    assert len(d["sessions"]) == 200 and d["truncated"] is True
    assert sum(1 for s in d["sessions"] if s["state"] == "ended") == 70, "130 live sessions all kept, the cut falls on ended ones"
    assert len(d["tasks"]) == 200 and [t["id"] for t in d["tasks"][:5]] == sorted(live, reverse=True), "unfinished tasks come first"


# ---------------------------------------------------------------- the 100 KB cap and the order things go in

def big_row(kind, i, **kw):
    s = "x" * 190
    if kind == "task":
        return {"id": i, "title": s, "phase": "done", "agent": "claude", "project": s, "repo": s, "branch": s, "tmux": s, "issue_ref": "#1",
                "updated_at": node_state.iso(nodes._now()), **kw}
    return {"tmux": s + str(i), "project": s, "repo": s, "session": s, "agent": "claude", "state": "working", "needs_you": False, "kind": "task",
            "since": "2026-10-10T10:00:00+00:00", "model": "opus", **kw}


def body_of(recent_finished=0, old_finished=0, ended=0, live=0, active=0):
    old = node_state.iso(nodes._now() - 3 * 86400)
    tasks = ([big_row("task", i, phase="running") for i in range(active)] + [big_row("task", 1000 + i) for i in range(recent_finished)]
             + [big_row("task", 2000 + i, updated_at=old) for i in range(old_finished)])
    sessions = [big_row("session", i) for i in range(live)] + [big_row("session", 500 + i, state="ended") for i in range(ended)]
    return {"api": 1, "node": {"id": "n_" + "a" * 16, "name": "a", "url": None, "version": None, "now": "2026-10-10T10:00:00.000+00:00"}, "etag_base": "0" * 32,
            "projects": [], "sessions": sessions, "tasks": tasks, "needs_you": {"permissions": 0, "input": 0, "errors": 0}, "usage": {}, "lanes": {}, "login_problems": [],
            "truncated": False}


def test_old_finished_tasks_go_first_and_only_as_many_steps_as_needed():
    b = body_of(recent_finished=20, old_finished=110, ended=30, live=10, active=10)
    without_old = {**b, "tasks": [t for t in b["tasks"] if t["id"] < 2000]}
    assert node_state._size(b) > node_state.BODY_MAX >= node_state._size(without_old), "the fixture needs the first step to be enough"
    out = node_state.fit(b)
    assert not [t for t in out["tasks"] if t["id"] >= 2000] and len(out["tasks"]) == 30
    assert len(out["sessions"]) == 40, "ended sessions stay while dropping old finished tasks is enough"
    assert out["truncated"] is True and node_state._size(out) <= node_state.BODY_MAX


def test_ended_sessions_go_second_and_recent_finished_tasks_stay():
    b = body_of(recent_finished=50, old_finished=100, ended=60, live=10, active=10)
    without_old = {**b, "tasks": [t for t in b["tasks"] if t["id"] < 2000]}
    assert node_state._size(without_old) > node_state.BODY_MAX, "the fixture needs the second step to be needed"
    out = node_state.fit(b)
    assert not [t for t in out["tasks"] if t["id"] >= 2000] and len(out["tasks"]) == 60
    assert [s for s in out["sessions"] if s["state"] == "ended"] == [] and len(out["sessions"]) == 10
    assert out["truncated"] is True and node_state._size(out) <= node_state.BODY_MAX


def test_when_that_is_still_not_enough_the_tail_of_the_lists_goes_and_the_cap_holds():
    b = body_of(active=200, live=200)
    assert node_state._size(b) > node_state.BODY_MAX
    out = node_state.fit(b)
    assert node_state._size(out) <= node_state.BODY_MAX and out["truncated"] is True and out["tasks"] and out["sessions"]
    small = node_state.fit(body_of(active=3, live=3))
    assert small["truncated"] is False and len(small["tasks"]) == 3


def test_500_tasks_on_the_wire_stay_under_100_kb_old_finished_go_first_and_ended_sessions_stay(board):
    for i in range(60):
        task(board.db, "A" * 190 + str(i))
    for i in range(440):
        tid = task(board.db, "F" * 190 + str(i))
        board.db.conn.execute("update tasks set phase='done' where id=?", (tid,))
        age_task(board.db, tid, 72)
    board.db.conn.execute("update tasks set branch=?, tmux_name=?, project=?, repo=?", ("b" * 190, "t" * 190, "p" * 190, "r" * 190))
    board.sessions["shop--api--gone"] = session("ended")
    r = board.get()
    d = r.json()
    assert len(r.content) <= node_state.BODY_MAX and d["truncated"] is True
    assert len(d["tasks"]) == 60 and {t["phase"] for t in d["tasks"]} == {"running"}, "the 200 kept rows were 60 unfinished and 140 old finished: the old finished went"
    assert [s["state"] for s in d["sessions"]] == ["ended"], "an ended session stays while that was enough"
    assert len(board.db.tasks()) == 500, "and nothing was removed from the board's own table"


# ---------------------------------------------------------------- the ETag and the 304

def test_the_etag_ignores_now_and_a_matching_if_none_match_answers_304_with_no_body(board):
    board.sessions["shop--api--x"] = session()
    task(board.db)
    r = board.get()
    tag = r.headers["etag"]
    d = r.json()
    assert tag == f'W/"{d["etag_base"]}"' and len(d["etag_base"]) == node_state.ETAG_LEN and r.headers["x-ccboard-now"] == d["node"]["now"]
    later = json.loads(json.dumps(d))
    later["node"]["now"] = "2031-01-01T00:00:00.000+00:00"
    assert node_state.digest(later) == d["etag_base"], "the clock is not part of the ETag"
    nm = board.get(headers={"If-None-Match": tag})
    assert nm.status_code == 304 and nm.content == b"" and nm.headers["etag"] == tag and nm.headers["x-ccboard-now"]
    assert board.get(headers={"If-None-Match": 'W/"0000"'}).status_code == 200
    assert board.get(headers={"If-None-Match": '"a", ' + tag}).status_code == 304 and board.get(headers={"If-None-Match": "*"}).status_code == 304
    task(board.db, "Another one")
    board.main._invalidate_scan()
    changed = board.get(headers={"If-None-Match": tag})
    assert changed.status_code == 200 and changed.headers["etag"] != tag


# ---------------------------------------------------------------- who may call it, and what it costs

def mint(board, scopes):
    return nodes.add_incoming({"id": "n_" + "c" * 16, "name": "node-c", "url": "https://100.64.0.3"}, list(scopes), db=board.db)[1]


def test_a_node_token_with_read_an_identity_and_nothing_else(board, monkeypatch):
    monkeypatch.setattr(nodes, "node_read_limiter", nodes._Limiter(10 ** 6, 60.0, 64))
    tok = mint(board, ("read",))
    node = {"Authorization": f"Bearer {tok}", "X-CCBoard": "1"}
    assert board.c.get("/api/node/state", headers=node).status_code == 200
    assert board.get().status_code == 200, "a signed-in person"
    r = board.c.get("/api/node/state")
    assert r.status_code == 403, "no credential"
    assert board.c.get("/api/node/state", headers={"X-CCBoard": "1"}).status_code == 403
    nr = mint(board, ("tasks",))
    r = board.c.get("/api/node/state", headers={"Authorization": f"Bearer {nr}", "X-CCBoard": "1"})
    assert r.status_code == 403 and "read scope" in r.json()["error"], "a pair without scope read"
    assert nodes.route_scope("GET", "/api/node/state") == "read" and nodes.route_scope("POST", "/api/node/state") is None


def test_it_rides_the_scan_cache_so_a_polling_hub_adds_no_scan(board):
    board.get()
    n = board.calls.scans
    assert n == 1
    for _ in range(5):
        board.get()
    board.c.get("/api/state", headers=ID)
    assert board.calls.scans == n, "the state poll and the node state share the 2 s scan"
    board.main._invalidate_scan()
    board.get()
    assert board.calls.scans == n + 1


def test_a_tmux_that_is_down_still_answers(board, monkeypatch):
    monkeypatch.setattr(board.main, "_merged_sessions", lambda rich=False: ({}, True))
    board.main._invalidate_scan()
    d = board.get().json()
    assert d["sessions"] == [] and [p["name"] for p in d["projects"]] == ["ops", "shop"]


# ---------------------------------------------------------------- the slug of a GitHub origin

@pytest.mark.parametrize("url,slug", [("git@github.com:acme/api.git", "acme/api"), ("https://github.com/acme/api", "acme/api"), ("https://github.com/acme/api.git/", "acme/api"),
                                      ("ssh://git@github.com/acme/api.git", "acme/api"), ("https://user:pw@github.com/acme/api.git", "acme/api"),
                                      ("https://gitlab.com/acme/api.git", None), ("git@example.com:acme/api.git", None), ("https://github.com.evil.example/acme/api", None),
                                      ("https://github.com/acme", None), ("", None), (None, None), ("/srv/git/acme/api", None)])
def test_github_slug(url, slug):
    assert node_state.github_slug(url) == slug


def test_the_slug_is_read_once_per_ten_minutes_and_only_for_git_repos(board, monkeypatch):
    seen = []
    monkeypatch.setattr(node_state, "_origin_url", lambda path: seen.append(path.name) or "git@github.com:acme/x.git")
    board.get()
    board.main._invalidate_scan()
    board.get()
    assert sorted(seen) == ["api", "tools", "web"], "one git call per repo, not per request"
    clock = [node_state.time.monotonic() + node_state.SLUG_TTL + 1]
    monkeypatch.setattr(node_state.time, "monotonic", lambda: clock[0])
    board.main._invalidate_scan()
    board.get()
    assert len(seen) == 6
