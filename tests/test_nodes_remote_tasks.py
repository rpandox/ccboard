"""Nodes epic P8, issue #141: create and dispatch a task, or open a session, on another node. The three write rows of the relay table
(task_create, task_dispatch, session_open) over two in-process boards.

`split_boards` (tests/conftest.py) gives each board its own projects dir and its own fake tmux, so "created on the peer, never on the hub" and "a repo that exists
only on the hub is not on the peer" are checked, not assumed. No real agent binary, tmux, git remote or network: claude is a fake path, tmux the conftest fake, the
git repos are empty temp repos, and the peer is the other board of the process.

1. Create now / later, dispatch lane, dispatch into a running session (the `sessions` scope), open a session: what the peer made, where, with which origin.
2. The answers: the shape, the ref built by the hub, nothing the hub should not show (path, prompt, attach url), and a hostile peer answer.
3. The guard matrix over HTTP against the real rows, on the hub (no call) and directly on the peer (nothing made).
4. The missing repo, the limit warning, offline / revoked / unconfirmed, the write rate.
5. The audit: both sides, the title cut at 80 characters, never the prompt, no secret.
"""
from __future__ import annotations

import json
import logging
import subprocess
from types import SimpleNamespace

import pytest

from app import nodes, nodes_relay as nr
from tests.test_nodes_guard import OK_BODIES, REFUSED_BODIES

ID = {"Tailscale-User-Login": "alice@example.com"}
PROMPT = "MARKER-prompt-4e7d19 add a login page"
SHOP = {"project": "shop", "repo": "api", "title": "Add login page", "prompt": PROMPT}


def bearer(token: str, **extra) -> dict:
    return {"Authorization": f"Bearer {token}", "X-CCBoard": "1", **extra}


class Count:
    def __init__(self, inner):
        self.inner, self.seen, self.paths = inner, 0, []

    def __call__(self, target, method, path, headers, body, timeout):
        self.seen += 1
        self.paths.append((method, path))
        return self.inner(target, method, path, headers, body, timeout)


class Raises:
    def __init__(self, exc):
        self.exc, self.seen = exc, 0

    def __call__(self, *a):
        self.seen += 1
        raise self.exc


class Answer:
    def __init__(self, body, status=200):
        self.body, self.status, self.seen = body, status, 0

    def __call__(self, target, method, path, headers, body, timeout):
        self.seen += 1
        return self.status, {"content-type": "application/json"}, json.dumps(self.body).encode()


class FakeHub:
    def __init__(self, **rec):
        self.rec = {"status": "online", "age_s": 3, "polled_at": "2026-10-10T00:00:00Z", "last_ok_at": "2026-10-10T00:00:00Z", **rec}

    def records(self, handle=None):
        return [self.rec]


@pytest.fixture(autouse=True)
def _hub_online(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "_hub", lambda: FakeHub())


@pytest.fixture(autouse=True)
def _fresh_scan():
    from app import main
    yield
    main._invalidate_scan()


@pytest.fixture(autouse=True)
def _wide(monkeypatch):
    big = lambda: nodes._Limiter(10 ** 6, 60.0, 64)
    for n in ("node_read_limiter", "node_write_limiter", "relay_read_limiter", "relay_write_limiter"):
        monkeypatch.setattr(nodes, n, big())


def git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)
    return path


@pytest.fixture
def world(split_boards, pair_up, monkeypatch):
    """Board a (the hub) paired with board b with every scope. b has shop/api; a has shop/api and shop/hubonly (with a GitHub remote); nobody has shop/ghost."""
    from app import claude_auth
    from app.agents import registry
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(claude_auth, "status", lambda: {"installed": True, "version": "2.1.287 (Claude Code)", "loggedIn": True, "authMethod": "claude.ai", "email": "me@example.com"})
    monkeypatch.setattr(claude_auth, "version", lambda: "2.1.287 (Claude Code)")
    monkeypatch.setattr(registry.plat, "ppid", lambda pid: None)
    registry.invalidate()
    git_init(split_boards.b_projects / "shop" / "api")
    git_init(split_boards.a_projects / "shop" / "api")
    hubonly = git_init(split_boards.a_projects / "shop" / "hubonly")
    subprocess.run(["git", "-C", str(hubonly), "remote", "add", "origin", "https://github.com/acme/hubonly.git"], check=True)
    reg = pair_up()
    count = Count(split_boards.transport)
    nodes.peer_transport = count
    yield SimpleNamespace(two=split_boards, a=split_boards.a, b=split_boards.b, reg=reg, h=reg["handle"], count=count, pair_up=pair_up)
    registry.invalidate()


def hub_post(w, path: str, body, **kw):
    return w.a.post(f"/api/nodes/{w.h}{path}", json=body, **kw)


def direct(w, method: str, path: str, body=None, token=None):
    """What the hub's transport would send, but by hand: a valid token on the peer's wrapper, so the hub's checks are bypassed."""
    if token is None:
        with w.a.enter():
            token = nodes._load_outgoing(w.reg["peer_id"])
    kw = {} if body is None else {"json": body}
    return w.b.call(method, path, owner=False, headers=bearer(token), **kw)


def task_on_b(w, tid: int) -> dict:
    return w.b.db.task_get(tid)


def audit(board, **kw):
    return board.db.node_audit_list(500, **kw)


def everything_audited(w) -> str:
    return json.dumps([audit(w.a), audit(w.b)])


# ================================================================ 1. what the peer makes, and where

def test_create_now_makes_the_task_on_the_peer_only_with_origin_and_a_ref(world):
    w = world
    r = hub_post(w, "/tasks", {**SHOP, "when": "now"})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert r.json()["node"] == "node-b" and d["phase"] == "running" and d["ref"] == f"node-b:{d['id']}" and d["tmux"] and d["branch"].startswith("worktree-")
    t = task_on_b(w, d["id"])
    assert t["worktree"].startswith(str(w.two.b_projects / "shop" / "api")), "the worktree path is under the peer's own project"
    assert json.loads(t["origin"]) == {"node": "node-a", "user": "alice@example.com"}, "the caller's node and the login it reported"
    assert t["phase"] == "running" and t["tmux_name"] == d["tmux"]
    assert [c[0] for c in w.two.tmux_b["created"]] == [d["tmux"]], "the session is on the peer's tmux"
    assert w.two.tmux_a["created"] == [] and w.a.db.tasks() == [], "the hub made no session, no task row, no worktree"
    assert not (w.two.a_projects / "shop" / "api" / ".ccboard").exists()


def test_create_later_makes_a_backlog_card_with_the_caller_in_origin(world):
    w = world
    r = hub_post(w, "/tasks", {**SHOP, "when": "later", "model": "sonnet", "auto_close": False, "issue_ref": "acme/api#12"})
    d = r.json()["data"]
    assert r.status_code == 200 and d["phase"] == "backlog" and d["tmux"] is None and d["ref"] == f"node-b:{d['id']}"
    t = task_on_b(w, d["id"])
    assert t["phase"] == "backlog" and json.loads(t["origin"])["node"] == "node-a" and t["issue_number"] == 12 and t["issue_url"] == "https://github.com/acme/api/issues/12"
    assert w.two.tmux_b["created"] == [] and w.two.tmux_a["created"] == []
    assert d["task"]["issue_ref"] == "#12" and d["task"]["phase"] == "backlog"


def test_when_defaults_to_now(world):
    r = hub_post(world, "/tasks", SHOP)
    assert r.json()["data"]["phase"] == "running"


def test_dispatch_lane_starts_the_card_on_the_peer(world):
    w = world
    card = hub_post(w, "/tasks", {**SHOP, "when": "later"}).json()["data"]
    r = hub_post(w, f"/tasks/{card['id']}/dispatch", {})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["phase"] == "running" and d["id"] == card["id"] and d["tmux"] and d["ref"] == card["ref"]
    assert [c[0] for c in w.two.tmux_b["created"]] == [d["tmux"]] and w.two.tmux_a["created"] == []
    assert json.loads(task_on_b(w, d["id"])["origin"])["node"] == "node-a"


def test_dispatch_stamps_the_caller_on_a_card_that_was_made_on_the_peer_itself_and_keeps_an_origin_that_is_there(world):
    w = world
    made = w.b.post("/api/tasks", json={**SHOP, "when": "later"}).json()
    assert task_on_b(w, made["id"])["origin"] is None
    assert hub_post(w, f"/tasks/{made['id']}/dispatch", {}).status_code == 200
    assert json.loads(task_on_b(w, made["id"])["origin"]) == {"node": "node-a", "user": "alice@example.com"}
    with w.b.enter():
        w.b.db.task_update(made["id"], origin={"node": "someone-else", "user": "bob"}, phase="backlog")
    w.two.tmux_b["sessions"].clear()                      # the first dispatch's session is gone, so the same slug can start again
    assert hub_post(w, f"/tasks/{made['id']}/dispatch", {}).status_code == 200
    assert json.loads(task_on_b(w, made["id"])["origin"])["node"] == "someone-else", "a card that has an origin keeps it"


def test_a_card_saved_on_the_peer_with_args_is_not_started_from_another_node(world):
    w = world
    made = w.b.post("/api/tasks", json={**SHOP, "when": "later", "args": "--verbose"}).json()
    r = hub_post(w, f"/tasks/{made['id']}/dispatch", {})
    assert r.status_code == 409 and "launch options" in r.json()["error"] and "start it on node-b" in r.json()["error"]
    assert w.two.tmux_b["created"] == [] and task_on_b(w, made["id"])["phase"] == "backlog"


def test_dispatch_of_a_task_that_is_not_there_is_404_and_a_started_one_is_409(world):
    w = world
    assert hub_post(w, "/tasks/999/dispatch", {}).status_code == 404
    now = hub_post(w, "/tasks", SHOP).json()["data"]
    r = hub_post(w, f"/tasks/{now['id']}/dispatch", {})
    assert r.status_code == 409 and "already dispatched" in r.json()["error"]


def make_session_on_b(w, name="s1", state="idle"):
    with w.b.enter():
        r = w.two.client.post("/api/projects/shop/repos/api/sessions", headers={**ID, "X-CCBoard": "1"}, json={"launcher": "claude", "name": name})
        assert r.status_code == 201, r.text
        tm = r.json()["tmux"]
        w.b.db.set_state(tm, state, "test")
    return tm


def test_dispatch_into_a_session_needs_the_sessions_scope_on_the_hub_with_no_call(world):
    w = world
    reg = w.pair_up(["read", "tasks"])
    count = Count(w.two.transport)
    nodes.peer_transport = count
    card = w.a.post(f"/api/nodes/{reg['handle']}/tasks", json={**SHOP, "when": "later"}).json()["data"]
    before = count.seen
    tm = "shop--api--s1"
    r = w.a.post(f"/api/nodes/{reg['handle']}/tasks/{card['id']}/dispatch", json={"mode": "session", "session": tm})
    assert r.status_code == 409 and r.json()["reason"] == "scope" and r.json()["error"] == "needs the sessions scope on node-b", r.text
    assert count.seen == before, "refused before any call"
    assert audit(w.a, action="dispatch_task")[0]["status"] == "refused"
    r = w.a.post(f"/api/nodes/{reg['handle']}/tasks/{card['id']}/dispatch", json={"mode": "lane"})
    assert r.status_code == 200, "the lane dispatch needs tasks only"


def test_dispatch_into_a_session_is_403_on_the_peer_for_a_token_without_sessions_even_if_the_hub_did_not_check(world):
    w = world
    reg = w.pair_up(["read", "tasks"])
    card = w.a.post(f"/api/nodes/{reg['handle']}/tasks", json={**SHOP, "when": "later"}).json()["data"]
    with w.a.enter():
        token = nodes._load_outgoing(reg["peer_id"])
    r = direct(w, "POST", f"/api/node/tasks/{card['id']}/dispatch", {"mode": "session", "session": "shop--api--s1"}, token)
    assert r.status_code == 403 and "sessions scope" in r.json()["error"]
    assert any(x["action"] == "dispatch_task" and x["status"] == "refused" and "sessions" in (x["detail"] or "") for x in audit(w.b))
    assert direct(w, "POST", f"/api/node/tasks/{card['id']}/dispatch", {"mode": "lane"}, token).status_code == 200


def test_dispatch_into_a_running_session_hands_the_prompt_over_and_a_busy_session_is_a_409(world):
    w = world
    tm = make_session_on_b(w)
    card = hub_post(w, "/tasks", {**SHOP, "when": "later"}).json()["data"]
    r = hub_post(w, f"/tasks/{card['id']}/dispatch", {"mode": "session", "session": tm})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["phase"] == "running" and d["tmux"] == tm and d["pasted"] is True and d["ref"] == card["ref"]
    assert [p[0] for p in w.two.tmux_b["pasted"]] == [tm] and PROMPT in w.two.tmux_b["pasted"][0][1], "the prompt went into the peer's session"
    assert w.two.tmux_a["pasted"] == []
    other = hub_post(w, "/tasks", {**SHOP, "title": "Second", "when": "later"}).json()["data"]
    with w.b.enter():
        w.b.db.set_state(tm, "working", "test")
    r = hub_post(w, f"/tasks/{other['id']}/dispatch", {"mode": "session", "session": tm})
    assert r.status_code == 409 and "working" in r.json()["error"], r.text
    assert task_on_b(w, other["id"])["phase"] == "backlog"


@pytest.mark.parametrize("body,fragment", [({"mode": "session"}, "session is required"), ({"mode": "lane", "session": "shop--api--s1"}, "a lane dispatch starts a new session"),
                                           ({"mode": "session", "session": "not-a-session"}, "not a ccboard session name"), ({"mode": "fly"}, "mode"),
                                           ({"session": "shop--api--s1"}, "a lane dispatch starts a new session"), ({"force": True}, "force"), ({"queue": True}, "queue")])
def test_the_dispatch_model_is_strict_on_both_sides(world, body, fragment):
    w = world
    card = hub_post(w, "/tasks", {**SHOP, "when": "later"}).json()["data"]
    n = w.count.seen
    r = hub_post(w, f"/tasks/{card['id']}/dispatch", body)
    assert r.status_code == 422 and fragment in r.json()["error"] and w.count.seen == n, r.text
    r = direct(w, "POST", f"/api/node/tasks/{card['id']}/dispatch", body)
    assert r.status_code == 422 and fragment in r.json()["error"], r.text
    assert task_on_b(w, card["id"])["phase"] == "backlog"


def test_after_task_id_is_a_422_that_says_chains_stay_inside_one_node(world):
    w = world
    first = hub_post(w, "/tasks", {**SHOP, "when": "later"}).json()["data"]
    n = w.count.seen
    r = hub_post(w, "/tasks", {**SHOP, "when": "later", "after_task_id": first["id"]})
    assert r.status_code == 422 and r.json()["reason"] == "invalid" and "chains stay inside one node" in r.json()["error"] and w.count.seen == n
    r = direct(w, "POST", "/api/node/tasks", {**SHOP, "when": "later", "after_task_id": first["id"]})
    assert r.status_code == 422 and "chains stay inside one node" in r.json()["error"]
    assert len(w.b.db.tasks()) == 1, "no queued step was made"


def test_open_a_session_creates_the_tmux_session_on_the_peer_only(world):
    w = world
    r = hub_post(w, "/sessions", {"project": "shop", "repo": "api", "name": "scratch", "model": "sonnet", "effort": "high", "permission_mode": "plan"})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d == {"ref": "node-b/shop--api--scratch", "tmux": "shop--api--scratch", "agent": "claude", "project": "shop", "repo": "api"}
    assert [c[0] for c in w.two.tmux_b["created"]] == ["shop--api--scratch"] and w.two.tmux_a["created"] == []
    line = w.two.tmux_b["sent"][0][1]
    assert "--permission-mode plan" in line and "--model sonnet" in line and "bypass" not in line.lower()
    with w.b.enter():
        row = w.b.db.open_rows()["shop--api--scratch"]
    assert row["flags"]["origin"] == {"node": "node-a", "user": "alice@example.com"}
    assert w.a.db.open_rows() == {}, "the hub has no row for it"


def test_open_a_session_without_a_name_gets_the_boards_own_free_name(world):
    d = hub_post(world, "/sessions", {"project": "shop", "repo": "api"}).json()["data"]
    assert d["ref"] == f"node-b/{d['tmux']}" and d["tmux"].startswith("shop--api--")


def test_a_session_name_that_is_taken_is_a_409_from_the_peer(world):
    w = world
    assert hub_post(w, "/sessions", {"project": "shop", "repo": "api", "name": "one"}).status_code == 200
    r = hub_post(w, "/sessions", {"project": "shop", "repo": "api", "name": "one"})
    assert r.status_code == 409 and "already exists" in r.json()["error"]


# ================================================================ 2. the answers

def test_the_answer_of_a_create_has_exactly_the_issues_fields_and_nothing_of_the_peers_paths_or_prompts(world):
    w = world
    r = hub_post(w, "/tasks", SHOP)
    d = r.json()["data"]
    assert set(d) == {"ref", "id", "slug", "tmux", "branch", "phase", "task"}
    assert set(d["task"]) == {"id", "title", "phase", "agent", "project", "repo", "branch", "tmux", "issue_ref", "updated_at"}
    text = r.text
    assert PROMPT not in text and "MARKER-prompt" not in text and str(w.two.b_projects) not in text and "attach_url" not in text and "worktree" not in text.replace(d["branch"], "")


def test_a_peer_answer_is_rebuilt_from_a_whitelist_and_redacted_and_the_ref_is_the_hubs(world):
    w = world
    secret = "xox" + "b-" + "1234567890-abcdefghijklmnop"
    nodes.peer_transport = Answer({"id": 7, "slug": "slug " + secret, "tmux": "shop--api--t", "branch": "worktree-x", "phase": "running", "ref": "other:99", "worktree": "/home/x/y",
                                   "prompt": PROMPT, "attach_url": "/term/x", "evil": {"a": 1}, "limit_warning": {"kind": "5h", "resets_at": 5, "pct": 90.5, "extra": 1},
                                   "task": {"id": 7, "title": "T", "prompt": PROMPT, "worktree": "/x", "phase": "running"}})
    r = hub_post(w, "/tasks", SHOP)
    d = r.json()["data"]
    assert d["ref"] == "node-b:7", "built from the registry's handle and the id, never the peer's `ref`"
    assert set(d) == {"ref", "id", "slug", "tmux", "branch", "phase", "task", "limit_warning"} and d["limit_warning"] == {"kind": "5h", "resets_at": 5, "pct": 90.5}
    assert secret not in r.text and "[redacted]" in d["slug"] and PROMPT not in r.text and "/home/x" not in r.text and set(d["task"]) >= {"id", "title", "phase"} and "prompt" not in d["task"]


def test_a_peer_answer_without_a_task_id_is_a_502_and_not_shown(world):
    w = world
    nodes.peer_transport = Answer({"slug": "x"})
    r = hub_post(w, "/tasks", SHOP)
    assert r.status_code == 502 and r.json()["reason"] == "bad_answer"
    nodes.peer_transport = Answer({"tmux": "not a session name"})
    assert hub_post(w, "/sessions", {"project": "shop", "repo": "api"}).status_code == 502


def test_a_handle_that_looks_like_a_secret_word_keeps_its_ref(world):
    from app import nodes_relay
    data = nodes_relay.redact_tree({"ref": "token:12", "other": "token:12", "id": 12})
    assert data["ref"] == "token:12" and data["other"] != "token:12", "only a ref the hub built is kept; the same text elsewhere is redacted"
    assert nodes_relay.redact_tree({"ref": "token/shop--api--sk-ant-aaaaaaaaaaaaaaaa"})["ref"] != "token/shop--api--sk-ant-aaaaaaaaaaaaaaaa"


# ================================================================ 3. the guard matrix over HTTP

def base_for(row: str) -> dict:
    return {"task_create": SHOP, "session_open": {"project": "shop", "repo": "api"}, "task_dispatch": {}}[row]


def url_for(row: str, tid: int = 1) -> tuple[str, str]:
    return {"task_create": ("/tasks", "/api/node/tasks"), "session_open": ("/sessions", "/api/node/sessions"),
            "task_dispatch": (f"/tasks/{tid}/dispatch", f"/api/node/tasks/{tid}/dispatch")}[row]


@pytest.mark.parametrize("row", ["task_create", "task_dispatch", "session_open"])
@pytest.mark.parametrize("extra,message", REFUSED_BODIES, ids=lambda x: x if isinstance(x, str) else ",".join(x))
def test_every_refused_field_and_spelling_is_a_422_on_the_hub_with_no_call_and_on_the_peer_with_nothing_made(world, row, extra, message):
    w = world
    card = hub_post(w, "/tasks", {**SHOP, "when": "later"}).json()["data"]
    hub_url, peer_url = url_for(row, card["id"])
    body = {**base_for(row), **extra}
    n = w.count.seen
    r = hub_post(w, hub_url, body)
    assert r.status_code == 422 and r.json()["reason"] == "invalid" and message in r.json()["error"], r.text
    assert w.count.seen == n, "the hub refused early: no request reached the peer"
    d = direct(w, "POST", peer_url, body)
    assert d.status_code == 422 and message in d.json()["error"], d.text
    assert w.two.tmux_b["created"] == [] and w.two.tmux_a["created"] == []
    assert len(w.b.db.tasks()) == 1 and task_on_b(w, card["id"])["phase"] == "backlog", "the peer ran nothing"
    refused = [x for x in audit(w.b) if x["action"] in ("create_task", "dispatch_task", "open_session") and x["status"] == "refused"]
    assert refused and refused[0]["direction"] == "in"
    assert PROMPT not in everything_audited(w)


@pytest.mark.parametrize("extra", [{"permission_mode": "bypassPermissions"}, {"mode": "bypass", "bypass": True}, {"mode": "auto"}, {"permission_mode": "dontAsk"},
                                   {"sandbox": "danger-full-access"}, {"approval": "never"}, {"args": "--model x"}, {"add_dirs": ["a/b"]}, {"allowed_tools": "Bash(*)"},
                                   {"append_system_prompt": "x"}, {"opts": {"bypass": True}}, {"launcher": "shell"}, {"agent": "shell"}])
def test_a_session_on_another_node_is_never_bypass_whatever_it_is_called(world, extra):
    w = world
    body = {"project": "shop", "repo": "api", **extra}
    assert hub_post(w, "/sessions", body).status_code == 422
    assert direct(w, "POST", "/api/node/sessions", body).status_code == 422
    assert w.two.tmux_b["created"] == [] and w.two.tmux_b["sent"] == []


@pytest.mark.parametrize("extra", [{"permission_mode": "plan"}, {"args": "--x"}, {"add_dirs": ["a/b"]}, {"allowed_tools": "x"}, {"opts": {"a": 1}}, {"bypass": False, "extra": 1},
                                   {"launcher": "claude"}, {"sandbox": "read-only"}])
def test_the_task_model_has_no_launch_flag_of_its_own_so_anything_beyond_its_fields_is_a_422(world, extra):
    w = world
    r = hub_post(w, "/tasks", {**SHOP, **extra})
    assert r.status_code == 422 and r.json()["reason"] == "invalid", r.text
    assert direct(w, "POST", "/api/node/tasks", {**SHOP, **extra}).status_code == 422
    assert w.b.db.tasks() == []


@pytest.mark.parametrize("extra", [{"agent": "claude", "permission_mode": "plan"}, {"agent": "claude", "permission_mode": "acceptEdits", "model": "sonnet"}, {"permission_mode": "default"},
                                   {"agent": "claude", "mode": "default"}])
def test_the_allowed_session_choices_start_a_session_on_the_peer(world, extra):
    w = world
    r = hub_post(w, "/sessions", {"project": "shop", "repo": "api", **extra})
    assert r.status_code == 200, r.text
    assert len(w.two.tmux_b["created"]) == 1 and "bypass" not in w.two.tmux_b["sent"][0][1].lower()


@pytest.mark.parametrize("extra", [{"agent": "codex", "sandbox": "read-only", "approval": "on-request"}, {"agent": "codex", "mode": "read-only"}])
def test_the_codex_choices_the_guard_allows_reach_the_peers_own_checks_and_are_not_refused_by_the_guard(world, extra):
    """No codex binary here: the peer says so (400), which is its own check after the guard let the body through."""
    w = world
    r = hub_post(w, "/sessions", {"project": "shop", "repo": "api", **extra})
    assert r.status_code == 400 and "codex is not installed" in r.json()["error"], r.text
    assert w.two.tmux_b["created"] == []


def test_the_guard_on_the_task_rows_lets_the_launcher_values_through(world):
    for body in ({**SHOP, "agent": "claude", "model": "sonnet", "effort": "high"}, {**SHOP, "effort": "medium", "when": "later"}):
        r = hub_post(world, "/tasks", body)
        assert r.status_code == 200, r.text


def test_the_dispatch_mode_word_is_not_read_as_the_launchers_mode(world):
    """The guard refuses a launcher `mode` that is not default, acceptEdits, plan or read-only; the dispatch `mode` (lane, session) is its own field."""
    assert nr.guard_launch({"mode": "lane"}) != []
    r = hub_post(world, "/tasks/1/dispatch", {"mode": "lane"})
    assert r.status_code != 422


def test_a_field_the_models_do_not_have_is_a_422_that_names_it(world):
    r = hub_post(world, "/tasks", {**SHOP, "surprise": 1})
    assert r.status_code == 422 and "surprise" in r.json()["error"]
    r = hub_post(world, "/sessions", {"project": "shop", "repo": "api", "prompt": "hello"})
    assert r.status_code == 422 and "prompt" in r.json()["error"], "a plain session takes no first message in this phase"


@pytest.mark.parametrize("body", [{**SHOP, "project": "../x"}, {**SHOP, "repo": "a--b"}, {**SHOP, "title": ""}, {**SHOP, "prompt": ""}, {**SHOP, "when": "never"},
                                  {**SHOP, "issue_ref": "not a ref"}, {**SHOP, "auto_close": "maybe"}, {**SHOP, "title": "x" * 301}, {**SHOP, "prompt": "x" * 20001}, {**SHOP, "agent": 5}])
def test_bad_values_are_422_and_never_repeated(world, body):
    r = hub_post(world, "/tasks", body)
    assert r.status_code == 422 and PROMPT not in r.text


# ================================================================ 4. a missing repo, the limit, the pair, the clock

def test_a_repo_that_exists_only_on_the_hub_is_404_with_the_clone_hint_and_nothing_is_made(world):
    w = world
    r = hub_post(w, "/tasks", {**SHOP, "repo": "hubonly"})
    assert r.status_code == 404 and r.json()["reason"] == "repo_missing"
    assert r.json()["error"] == "hubonly is not on node-b. It is acme/hubonly on GitHub: clone it in Settings > Projects on node-b. Nothing was cloned or created.", r.text
    assert not (w.two.b_projects / "shop" / "hubonly").exists() and w.two.tmux_b["created"] == [] and w.b.db.tasks() == [] and w.a.db.tasks() == []
    r = hub_post(w, "/sessions", {"project": "shop", "repo": "hubonly"})
    assert r.status_code == 404 and r.json()["reason"] == "repo_missing" and "hubonly is not on node-b" in r.json()["error"] and w.two.tmux_b["created"] == []
    assert audit(w.a, action="create_task")[0]["status"] == "failed" and "repo_missing" in audit(w.a, action="create_task")[0]["detail"]


def test_a_repo_or_project_nobody_has_is_404_without_a_clone_hint(world):
    w = world
    for body in ({**SHOP, "repo": "ghost"}, {**SHOP, "project": "nowhere"}):
        r = hub_post(w, "/tasks", body)
        assert r.status_code == 404 and r.json()["error"] == f"{body['repo']} is not on node-b. Nothing was created.", r.text
    r = hub_post(w, "/tasks", {**SHOP, "project": "nowhere", "repo": "root"})
    assert r.status_code == 404 and r.json()["error"] == "project nowhere is not on node-b. Nothing was created."
    assert w.two.tmux_b["created"] == [] and w.b.db.tasks() == []
    d = direct(w, "POST", "/api/node/tasks", {**SHOP, "repo": "ghost"})
    assert d.status_code == 404 and d.json()["reason"] == "repo_missing" and "ghost is not on node-b" in d.json()["error"]


def test_the_peers_limit_warning_comes_back_and_a_hand_start_is_not_held(world, monkeypatch):
    from app import taskflow
    w = world
    monkeypatch.setattr(taskflow, "limit_gate", lambda db, now=None, agent="claude": {"kind": "5h", "resets_at": 1900000000.0, "pct": 91.0})
    r = hub_post(w, "/tasks", SHOP)
    d = r.json()["data"]
    assert r.status_code == 200 and d["phase"] == "running" and d["limit_warning"] == {"kind": "5h", "resets_at": 1900000000.0, "pct": 91.0}
    assert [c[0] for c in w.two.tmux_b["created"]] == [d["tmux"]], "started although the window is nearly full"
    card = hub_post(w, "/tasks", {**SHOP, "title": "later one", "when": "later"}).json()["data"]
    assert "limit_warning" not in card or card["limit_warning"]
    d2 = hub_post(w, f"/tasks/{card['id']}/dispatch", {}).json()["data"]
    assert d2["phase"] == "running" and d2["limit_warning"]["kind"] == "5h"


def test_an_offline_or_unread_or_unauthorized_node_is_refused_before_any_call_on_all_three_rows(world, monkeypatch):
    from app import main
    w = world
    n = w.count.seen
    for rec, status, reason in (({"status": "offline", "age_s": 240}, 503, "offline"), ({"status": "unauthorized"}, 409, "needs_repair"), ({"status": "unpaired"}, 409, "unpaired"),
                                ({"polled_at": None, "last_ok_at": None, "status": "offline"}, 409, "not_read_yet")):
        monkeypatch.setattr(main, "_hub", lambda rec=rec: FakeHub(**rec))
        for hub_url, body in (("/tasks", SHOP), ("/tasks/1/dispatch", {}), ("/sessions", {"project": "shop", "repo": "api"})):
            r = hub_post(w, hub_url, body)
            assert r.status_code == status and r.json()["reason"] == reason, (rec, hub_url, r.text)
    assert w.count.seen == n
    r = hub_post(w, "/tasks", SHOP)
    monkeypatch.setattr(main, "_hub", lambda: FakeHub(status="offline", age_s=240))
    assert "offline" in hub_post(w, "/tasks", SHOP).json()["error"] and "4 min" in hub_post(w, "/tasks", SHOP).json()["error"]


def test_a_revoked_pair_is_a_409_re_pair_once_and_then_refused_before_any_call(world):
    w = world
    with w.b.enter():
        pid = w.b.db.node_pairs()[0]["peer_id"]
        nodes.revoke(pid, db=w.b.db)
    r = hub_post(w, "/tasks", SHOP)
    assert r.status_code == 409 and r.json()["reason"] == "needs_repair"
    n = w.count.seen
    r = hub_post(w, "/tasks", SHOP)
    assert r.status_code == 409 and r.json()["reason"] == "needs_repair" and w.count.seen == n
    assert w.two.tmux_b["created"] == []


def test_a_removed_pair_is_404_unknown_node(world):
    w = world
    with w.a.enter():
        nodes.remove_peer(w.reg["peer_id"], w.a.db)
    assert hub_post(w, "/tasks", SHOP).status_code == 404


@pytest.mark.parametrize("exc", [TimeoutError("slow"), ConnectionResetError("reset"), BrokenPipeError("pipe")])
def test_a_write_that_may_have_gone_out_is_unconfirmed_and_never_retried(world, exc):
    w = world
    t = Raises(exc)
    nodes.peer_transport = t
    r = hub_post(w, "/tasks", SHOP)
    assert r.status_code in (502, 504) and r.json()["reason"] == "unconfirmed", r.text
    assert "could not be confirmed whether it started" in r.json()["error"] and "not retried" in r.json()["error"] and "Check node-b before starting again" in r.json()["error"]
    assert t.seen == 1, "one request, no retry"
    row = audit(w.a, action="create_task")[0]
    assert row["status"] == "failed" and "unconfirmed" in row["detail"]
    t.seen = 0
    assert hub_post(w, "/sessions", {"project": "shop", "repo": "api"}).json()["reason"] == "unconfirmed" and t.seen == 1
    assert hub_post(w, "/tasks/3/dispatch", {}).json()["reason"] == "unconfirmed" and t.seen == 2


def test_a_refused_connection_is_unreachable_not_unconfirmed(world):
    nodes.peer_transport = Raises(ConnectionRefusedError("no"))
    r = hub_post(world, "/tasks", SHOP)
    assert r.status_code == 502 and r.json()["reason"] == "unreachable"


def test_a_peer_error_status_is_mapped_and_a_429_keeps_its_retry_after(world):
    w = world

    class Reply:
        def __init__(self, status, body, headers=None):
            self.status, self.body, self.headers, self.seen = status, body, headers or {}, 0

        def __call__(self, *a):
            self.seen += 1
            return self.status, self.headers, json.dumps(self.body).encode()
    nodes.peer_transport = Reply(429, {"error": "slow"}, {"Retry-After": "17"})
    r = hub_post(w, "/tasks", SHOP)
    assert r.status_code == 429 and r.headers["retry-after"] == "17"
    nodes.peer_transport = Reply(500, {"error": "boom " + PROMPT})
    r = hub_post(w, "/tasks", SHOP)
    assert r.status_code == 502 and PROMPT not in r.text
    nodes.peer_transport = Reply(400, {"error": "title and prompt are required"})
    r = hub_post(w, "/tasks", SHOP)
    assert r.status_code == 400 and r.json()["error"] == "title and prompt are required"


def test_the_hub_write_rate_is_30_a_minute_per_person(world, monkeypatch):
    w = world
    monkeypatch.setattr(nodes, "relay_write_limiter", nodes._Limiter(30, 60.0, 8))
    codes = [hub_post(w, "/tasks", {**SHOP, "when": "later", "title": f"t{i}"}).status_code for i in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429
    r = hub_post(w, "/tasks", SHOP)
    assert r.status_code == 429 and r.headers["retry-after"].isdigit()
    assert w.a.get(f"/api/nodes/{w.h}/card").status_code == 200, "reads are another bucket"


def test_the_peer_write_rate_is_30_a_minute_per_pair_on_these_rows(world, monkeypatch):
    w = world
    monkeypatch.setattr(nodes, "node_write_limiter", nodes._Limiter(30, 60.0, 8))
    reads = nodes._Limiter(10 ** 6, 60.0, 8)
    monkeypatch.setattr(nodes, "node_read_limiter", reads)
    codes = [direct(w, "POST", "/api/node/tasks", {**SHOP, "when": "later", "title": f"t{i}"}).status_code for i in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429
    assert reads._hits == {}, "a write row never draws on the read bucket"


# ================================================================ 5. the audit

def test_the_audit_names_the_title_cut_at_80_and_the_acting_user_and_never_the_prompt(world):
    w = world
    title = ("T" * 79) + "XYZ-the-rest-of-a-very-long-title-that-must-not-be-in-the-audit-" + "Q" * 50
    r = hub_post(w, "/tasks", {**SHOP, "title": title, "when": "later"})
    assert r.status_code == 200
    out = audit(w.a, action="create_task")[0]
    inn = audit(w.b, action="create_task")[0]
    assert out["direction"] == "out" and out["status"] == "ok" and out["user"] == "alice@example.com" and out["node_name"] == "node-b"
    assert inn["direction"] == "in" and inn["status"] == "ok" and inn["user"] == "for alice@example.com" and inn["node_name"] == "node-a"
    for row in (out, inn):
        assert row["target"] == "create task: " + title[:80], row
        assert "XYZ-the-rest" not in row["target"] and len(row["target"]) <= 120
    assert PROMPT not in everything_audited(w) and "MARKER-prompt" not in everything_audited(w)


def test_a_secret_shaped_title_is_replaced_in_both_audit_rows(world):
    w = world
    secret = "sk-ant-" + "A1b2C3d4E5f6G7h8I9j0"
    hub_post(w, "/tasks", {**SHOP, "title": f"use {secret} to log in", "when": "later"})
    for board in (w.a, w.b):
        row = audit(board, action="create_task")[0]
        assert secret not in json.dumps(row) and "[redacted]" in row["target"], row


def test_dispatch_and_session_rows_are_audited_both_sides_with_their_own_targets(world):
    w = world
    card = hub_post(w, "/tasks", {**SHOP, "when": "later"}).json()["data"]
    hub_post(w, f"/tasks/{card['id']}/dispatch", {})
    hub_post(w, "/sessions", {"project": "shop", "repo": "api", "name": "aud"})
    for board, direction in ((w.a, "out"), (w.b, "in")):
        d = audit(board, action="dispatch_task")[0]
        assert d["direction"] == direction and d["target"] == f"dispatch task {card['id']} (lane)" and d["status"] == "ok"
        s = audit(board, action="open_session")[0]
        assert s["direction"] == direction and s["target"] == "open session shop/api" and s["status"] == "ok"
    assert PROMPT not in everything_audited(w)


def test_a_refused_body_audits_with_the_plain_target_and_no_title(world):
    w = world
    hub_post(w, "/tasks", {**SHOP, "title": "SECRET-TITLE-77", "permission_mode": "bypassPermissions"})
    out = audit(w.a, action="create_task")[0]
    assert out["status"] == "refused" and out["target"] == "create task" and "SECRET-TITLE-77" not in json.dumps(out)


def test_no_log_line_holds_a_prompt_or_a_token(world, caplog):
    w = world
    caplog.set_level(logging.DEBUG)
    hub_post(w, "/tasks", SHOP)
    direct(w, "POST", "/api/node/tasks", {**SHOP, "permission_mode": "bypassPermissions"})
    with w.a.enter():
        token = nodes._load_outgoing(w.reg["peer_id"])
    assert PROMPT not in caplog.text and token not in caplog.text and "MARKER-prompt" not in caplog.text


def test_a_single_board_with_no_pair_adds_nothing_for_these_rows(lite_client):
    from app import main
    nodes.reset()
    for path, body in (("/api/nodes/node-b/tasks", SHOP), ("/api/nodes/node-b/sessions", {"project": "shop", "repo": "api"}), ("/api/nodes/node-b/tasks/1/dispatch", {})):
        r = lite_client.post(path, headers={**ID, "X-CCBoard": "1"}, json=body)
        assert r.status_code == 404 and r.json()["reason"] == "unknown_node"
    assert main.db.node_audit_list(50) == [] and nodes.registry(main.db) == []


# ================================================================ the board's own routes are unchanged

def test_the_boards_own_task_and_session_routes_keep_their_answers(world):
    w = world
    made = w.b.post("/api/tasks", json={**SHOP, "when": "now"})
    assert made.status_code == 201 and {"id", "slug", "tmux", "branch", "attach_url", "phase", "session_row", "task"} <= set(made.json())
    t = task_on_b(w, made.json()["id"])
    assert t["origin"] is None, "a task made on the board itself has no origin"
    s = w.b.post("/api/projects/shop/repos/api/sessions", json={"launcher": "claude", "name": "plain"})
    assert s.status_code == 201 and {"tmux", "attach_url", "agent", "cmd"} <= set(s.json())
    with w.b.enter():
        assert "origin" not in w.b.db.open_rows()["shop--api--plain"]["flags"]
