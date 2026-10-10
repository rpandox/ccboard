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


def make_session_on_b(w, name="s1", state="idle", **body):
    """A session started on the peer's own board (as its owner would). It says its permission mode (`mode: default`) unless a test passes its own body fields: a
    session whose line says nothing is undeterminable and another node may not type into it."""
    with w.b.enter():
        r = w.two.client.post("/api/projects/shop/repos/api/sessions", headers={**ID, "X-CCBoard": "1"}, json={"launcher": "claude", "name": name, **({"mode": "default"} if not body else body)})
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
    nodes.peer_transport = Answer({"id": 7, "slug": "slug-ok", "tmux": "shop--api--t", "branch": "worktree-x", "phase": "running", "ref": "other:99", "worktree": "/home/x/y",
                                   "prompt": PROMPT, "attach_url": "/term/x", "evil": {"a": 1}, "limit_warning": {"kind": "5h", "resets_at": 5, "pct": 90.5, "extra": 1},
                                   "task": {"id": 7, "title": "T " + secret, "prompt": PROMPT, "worktree": "/x", "phase": "running"}})
    r = hub_post(w, "/tasks", SHOP)
    d = r.json()["data"]
    assert d["ref"] == "node-b:7", "built from the registry's handle and the id, never the peer's `ref`"
    assert set(d) == {"ref", "id", "slug", "tmux", "branch", "phase", "task", "limit_warning"} and d["limit_warning"] == {"kind": "5h", "resets_at": 5, "pct": 90.5}
    assert secret not in r.text and d["slug"] == "slug-ok" and "[redacted]" in d["task"]["title"] and PROMPT not in r.text and "/home/x" not in r.text and set(d["task"]) >= {"id", "title", "phase"} and "prompt" not in d["task"]


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


# ================================================================ 6. review of P8: the target session's permissions, no inherited defaults, the final plan, the typed text

WIDE = "wider permissions than another node may use"


def later_card(w, **kw) -> dict:
    r = hub_post(w, "/tasks", {**SHOP, "when": "later", **kw})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def typed_lines(w) -> list[tuple[str, str]]:
    return list(w.two.tmux_b["sent"])


def dispatch_both_ways(w, card_id: int, body: dict):
    """The hub's route and the peer's wrapper with a valid token (the hub's checks bypassed): both answers."""
    return hub_post(w, f"/tasks/{card_id}/dispatch", body), direct(w, "POST", f"/api/node/tasks/{card_id}/dispatch", body)


# the ways a session on the peer can run wider than a remote launch may, each started the way its owner would start it on the peer's own board
WIDER_SESSIONS = [
    {"launcher": "claude", "mode": "bypass", "bypass": True},
    {"launcher": "claude", "permission_mode": "bypassPermissions"},
    {"launcher": "claude", "bypass": True},
    {"launcher": "claude", "mode": "auto"},
    {"launcher": "claude", "permission_mode": "dontAsk"},
    {"launcher": "claude", "mode": "default", "args": "--permission-mode bypassPermissions"},
    {"launcher": "claude", "mode": "default", "args": "--dangerously-skip-permissions"},
    {"launcher": "claude", "mode": "default", "allowed_tools": "Bash"},
    {"launcher": "claude"},                                   # says nothing: Claude Code's own settings.json picks the mode, so it cannot be told
]


@pytest.mark.parametrize("body", WIDER_SESSIONS, ids=lambda b: ",".join(f"{k}={v}" for k, v in b.items() if k != "launcher") or "no-mode")
def test_a_task_is_never_typed_into_a_session_that_runs_wider_than_the_remote_set_even_with_a_valid_token_and_both_scopes(world, body):
    w = world
    tm = make_session_on_b(w, "wide", **body)
    card = later_card(w)
    hub, peer = dispatch_both_ways(w, card["id"], {"mode": "session", "session": tm})
    for r in (hub, peer):
        assert r.status_code == 409 and WIDE in r.json()["error"], r.text
    assert w.two.tmux_b["pasted"] == [] and task_on_b(w, card["id"])["phase"] == "backlog", "nothing was typed, the card did not move"
    assert PROMPT not in everything_audited(w)


@pytest.mark.parametrize("body", [{"launcher": "claude", "mode": "default"}, {"launcher": "claude", "mode": "acceptEdits"}, {"launcher": "claude", "mode": "read-only"},
                                  {"launcher": "claude", "permission_mode": "plan", "model": "sonnet"}])
def test_a_session_inside_the_allowed_set_takes_the_task(world, body):
    w = world
    tm = make_session_on_b(w, "fine", **body)
    card = later_card(w)
    r = hub_post(w, f"/tasks/{card['id']}/dispatch", {"mode": "session", "session": tm})
    assert r.status_code == 200 and r.json()["data"]["pasted"] is True, r.text


def test_a_session_row_with_no_recorded_line_is_undeterminable_and_refused(world):
    w = world
    name = "shop--api--bare"
    with w.b.enter():
        w.two.tmux_b["sessions"][name] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%9", "command": "claude", "path": "/x", "pid": 1, "env": {}}
        w.b.db.add_session(tmux_name=name, project="shop", repo="api", name="bare", launcher="claude", cmd=None, agent="claude")
        w.b.db.set_state(name, "idle", "test")
    card = later_card(w)
    for r in dispatch_both_ways(w, card["id"], {"mode": "session", "session": name}):
        assert r.status_code == 409 and WIDE in r.json()["error"], r.text
    assert w.two.tmux_b["pasted"] == []


def test_a_foreign_session_one_in_another_repo_and_an_internal_name_are_refused_on_the_peer(world):
    w = world
    git_init(w.two.b_projects / "shop" / "other")
    with w.b.enter():
        w.two.tmux_b["sessions"]["shop--api--foreign"] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%8", "command": "zsh", "path": "/x", "pid": 1, "env": {}}
        r = w.two.client.post("/api/projects/shop/repos/other/sessions", headers={**ID, "X-CCBoard": "1"}, json={"launcher": "claude", "name": "elsewhere", "mode": "default"})
        assert r.status_code == 201, r.text
        w.b.db.set_state(r.json()["tmux"], "idle", "test")
    card = later_card(w)
    hub, peer = dispatch_both_ways(w, card["id"], {"mode": "session", "session": "shop--api--foreign"})
    for r in (hub, peer):
        assert r.status_code == 409 and "not a session this board started" in r.json()["error"], r.text
    hub, peer = dispatch_both_ways(w, card["id"], {"mode": "session", "session": "shop--other--elsewhere"})
    for r in (hub, peer):
        assert r.status_code == 409 and "another repo" in r.json()["error"], r.text
    for name in ("_ccboard-login", "_ccboard--x--y"):
        hub, peer = dispatch_both_ways(w, card["id"], {"mode": "session", "session": name})
        assert hub.status_code == 422 and peer.status_code == 422, (hub.text, peer.text)
    assert w.two.tmux_b["pasted"] == [] and task_on_b(w, card["id"])["phase"] == "backlog"


def test_a_session_that_does_not_exist_is_still_a_404_from_the_board(world):
    card = later_card(world)
    r = hub_post(world, f"/tasks/{card['id']}/dispatch", {"mode": "session", "session": "shop--api--nobody"})
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------- a remote launch never inherits what this board, the person or the CLI would default to

def line_of(w, tmux_name: str) -> str:
    return next(t for n, t in typed_lines(w) if n == tmux_name)


def test_an_omitted_permission_mode_is_the_explicit_ask_mode_on_the_line_of_a_task_a_card_and_a_session(world, monkeypatch):
    """The board has no default permission mode of its own for a task, but the CLI has one: a line without --permission-mode is Claude Code's settings.json
    (defaultMode, which can say bypassPermissions). So the line of a remote launch always carries the flag. The board's subagent-model default is the second
    local default that must not ride along."""
    from app.config import settings
    w = world
    monkeypatch.setattr(settings, "subagent_model", "haiku")
    d = hub_post(w, "/tasks", SHOP).json()["data"]
    line = line_of(w, d["tmux"])
    assert "--permission-mode manual" in line and "CLAUDE_CODE_SUBAGENT_MODEL" not in line and "bypass" not in line.lower(), line
    spec = json.loads(task_on_b(w, d["id"])["spec"])
    assert spec["permission_mode"] == "manual" and spec["subagent_model"] == "inherit", "the explicit choice is stored on the card"
    card = later_card(w, title="Second")
    assert json.loads(task_on_b(w, card["id"])["spec"])["permission_mode"] == "manual"
    d2 = hub_post(w, f"/tasks/{card['id']}/dispatch", {}).json()["data"]
    assert "--permission-mode manual" in line_of(w, d2["tmux"]) and "CLAUDE_CODE_SUBAGENT_MODEL" not in line_of(w, d2["tmux"])
    for body in ({}, {"permission_mode": "default"}, {"mode": "default"}):
        s = hub_post(w, "/sessions", {"project": "shop", "repo": "api", **body}).json()["data"]
        assert "--permission-mode manual" in line_of(w, s["tmux"]) and "CLAUDE_CODE_SUBAGENT_MODEL" not in line_of(w, s["tmux"]), body
    with w.b.enter():
        assert w.b.db.open_rows()[d["tmux"]]["opts"].get("subagent_model") in (None, "inherit"), "the stored options of the task carry no subagent model either"
        local = w.two.client.post("/api/projects/shop/repos/api/sessions", headers={**ID, "X-CCBoard": "1"}, json={"launcher": "claude", "name": "mine"}).json()
    assert "CLAUDE_CODE_SUBAGENT_MODEL=haiku" in line_of(w, local["tmux"]) and "--permission-mode" not in line_of(w, local["tmux"]), "the owner's own launch still uses the defaults"


def test_a_card_saved_on_the_peer_keeps_its_own_allowed_mode_and_gets_the_ask_mode_when_it_has_none(world, monkeypatch):
    from app.config import settings
    w = world
    monkeypatch.setattr(settings, "subagent_model", "opus")
    plan = w.b.post("/api/tasks", json={**SHOP, "title": "Plan card", "when": "later", "permission_mode": "plan", "subagent_model": "sonnet"}).json()
    bare = w.b.post("/api/tasks", json={**SHOP, "title": "Bare card", "when": "later"}).json()
    p = hub_post(w, f"/tasks/{plan['id']}/dispatch", {}).json()["data"]
    assert "--permission-mode plan" in line_of(w, p["tmux"]) and "--permission-mode manual" not in line_of(w, p["tmux"]), "a card's narrower choice is not widened"
    assert "CLAUDE_CODE_SUBAGENT_MODEL" not in line_of(w, p["tmux"]), "the card's subagent model is the owner's: a remote launch says inherit"
    b = hub_post(w, f"/tasks/{bare['id']}/dispatch", {}).json()["data"]
    assert "--permission-mode manual" in line_of(w, b["tmux"]) and "CLAUDE_CODE_SUBAGENT_MODEL" not in line_of(w, b["tmux"])


def test_a_card_with_a_control_character_in_its_prompt_is_not_typed_from_another_node(world):
    w = world
    made = w.b.post("/api/tasks", json={**SHOP, "when": "later", "prompt": "fix it" + chr(3) + "; echo hi"}).json()
    r = hub_post(w, f"/tasks/{made['id']}/dispatch", {})
    assert r.status_code == 409 and "control character" in r.json()["error"], r.text
    assert w.two.tmux_b["created"] == [] and task_on_b(w, made["id"])["phase"] == "backlog"


@pytest.fixture
def cx(world, fake_codex, monkeypatch):
    from app.agents import codex
    monkeypatch.setattr(codex.CodexAgent, "_warm_models", lambda self, exe: None)
    return world


def test_omitted_codex_choices_are_both_flags_on_the_line_and_never_the_config_toml(cx):
    w = cx
    for body, want in (({}, "-s workspace-write -a on-request"), ({"sandbox": "read-only"}, "-s read-only -a on-request"), ({"mode": "read-only"}, "-s read-only -a on-request"),
                       ({"approval": "on-request"}, "-s workspace-write -a on-request"), ({"permission_mode": "plan"}, "-s read-only -a on-request")):
        r = hub_post(w, "/sessions", {"project": "shop", "repo": "api", "agent": "codex", **body})
        assert r.status_code == 200, (body, r.text)
        line = line_of(w, r.json()["data"]["tmux"])
        assert want in line and "never" not in line and "dangerously" not in line and "--yolo" not in line, (body, line)
    subprocess.run(["git", "-C", str(w.two.b_projects / "shop" / "api"), "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "--allow-empty", "-q", "-m", "x"], check=True)
    t = hub_post(w, "/tasks", {**SHOP, "agent": "codex"})
    assert t.status_code == 200, t.text
    assert "-s workspace-write -a on-request" in line_of(w, t.json()["data"]["tmux"])
    spec = json.loads(task_on_b(w, t.json()["data"]["id"])["spec"])
    assert spec["opts"] == {"sandbox": "workspace-write", "approval": "on-request"}


@pytest.mark.parametrize("body", [{"launcher": "claude", "agent": "codex", "mode": "bypass", "bypass": True},
                                  {"launcher": "claude", "agent": "codex", "mode": "custom", "sandbox": "workspace-write", "approval": "never"},
                                  {"launcher": "claude", "agent": "codex", "mode": "auto"},
                                  {"launcher": "claude", "agent": "codex", "mode": "custom", "sandbox": "read-only"},
                                  {"launcher": "claude", "agent": "codex"}])
def test_a_codex_session_wider_than_the_set_or_with_no_flags_is_not_a_target(cx, body):
    w = cx
    tm = make_session_on_b(w, "cx", **body)
    card = later_card(w, agent="codex")
    for r in dispatch_both_ways(w, card["id"], {"mode": "session", "session": tm}):
        assert r.status_code == 409 and WIDE in r.json()["error"], (body, r.text)
    assert w.two.tmux_b["pasted"] == []


def test_a_codex_session_inside_the_set_takes_the_task(cx):
    w = cx
    tm = make_session_on_b(w, "cx", launcher="claude", agent="codex", mode="default")
    card = later_card(w, agent="codex")
    r = hub_post(w, f"/tasks/{card['id']}/dispatch", {"mode": "session", "session": tm})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- the final plan is guarded too

@pytest.fixture
def no_flag(monkeypatch):
    """The Claude adapter forgets to put the permission flag on the line: the defence in depth must catch what the body-level guard cannot see."""
    from app.agents import claude
    monkeypatch.setattr(claude.ClaudeAgent, "_opt_args", lambda self, full: [])


def test_a_final_line_without_its_permission_flag_is_refused_before_a_session_exists_on_every_launch_row(world, no_flag):
    w = world
    card = later_card(w)
    for r in (hub_post(w, "/tasks", SHOP), hub_post(w, "/sessions", {"project": "shop", "repo": "api"}), hub_post(w, f"/tasks/{card['id']}/dispatch", {})):
        assert r.status_code == 400 and "would not stay inside what another node may use" in r.json()["error"] and "nothing was started" in r.json()["error"], r.text
    assert w.two.tmux_b["created"] == [] and w.two.tmux_b["sent"] == [] and w.b.db.open_rows() == {}
    assert task_on_b(w, card["id"])["phase"] == "backlog" and len(w.b.db.tasks()) == 1


def test_the_same_line_is_fine_for_the_owner_on_the_peers_own_board(world, no_flag):
    r = world.b.post("/api/tasks", json={**SHOP, "title": "Local"})
    assert r.status_code == 201 and "--permission-mode" not in line_of(world, r.json()["tmux"]), "the check applies to a request of a paired node only"


def test_a_line_with_a_bypass_spelling_in_it_is_refused_whatever_put_it_there(world, monkeypatch):
    from app.agents import claude
    w = world
    monkeypatch.setattr(claude.ClaudeAgent, "_opt_args", lambda self, full: ["--permission-mode", "manual", "--dangerously-" + "skip-permissions"])
    r = hub_post(w, "/tasks", SHOP)
    assert r.status_code == 400 and "dangerously-skip-permissions" in r.json()["error"], r.text
    assert w.two.tmux_b["created"] == []


def test_the_guard_runs_on_the_options_the_launch_ends_up_with(world, monkeypatch):
    w = world
    made = w.b.post("/api/tasks", json={**SHOP, "when": "later"}).json()
    monkeypatch.setattr(nr, "safe_launch", lambda *a, **k: {"permission_mode": "auto"})
    for r in (hub_post(w, "/tasks", SHOP), hub_post(w, "/sessions", {"project": "shop", "repo": "api"}), hub_post(w, f"/tasks/{made['id']}/dispatch", {})):
        assert r.status_code == 409 and "would go beyond what another node may use" in r.json()["error"], r.text
    assert w.two.tmux_b["created"] == [] and len(w.b.db.tasks()) == 1 and task_on_b(w, made["id"])["phase"] == "backlog"


# ---------------------------------------------------------------- the typed text, the names and the claims

def test_the_prompt_is_typed_into_a_shell_so_a_control_character_is_a_422_on_both_sides(world):
    w = world
    for field, value in (("prompt", "a" + chr(3) + "; echo x"), ("prompt", "a" + chr(4)), ("prompt", chr(27) + "[2J"), ("prompt", "a\rb"), ("prompt", "a" + chr(0)),
                         ("prompt", "a" + chr(127)), ("prompt", "a" + chr(0x85)), ("title", "t" + chr(3)), ("title", "t" + chr(27))):
        n = w.count.seen
        r = hub_post(w, "/tasks", {**SHOP, field: value})
        assert r.status_code == 422 and "control characters" in r.json()["error"] and w.count.seen == n, (field, r.text)
        assert chr(3) not in r.text and chr(27) not in r.text
        assert direct(w, "POST", "/api/node/tasks", {**SHOP, field: value}).status_code == 422
    assert w.b.db.tasks() == [] and w.two.tmux_b["created"] == []


def test_tab_newline_and_crlf_are_text_and_a_crlf_is_one_newline(world):
    w = world
    r = hub_post(w, "/tasks", {**SHOP, "when": "later", "prompt": "one\r\ntwo\n\tthree"})
    assert r.status_code == 200, r.text
    assert task_on_b(w, r.json()["data"]["id"])["prompt"] == "one\ntwo\n\tthree"


@pytest.mark.parametrize("ref", ["-x/api#1", "acme/..#1", "acme/.hidden#1", "acme/-x#1", "a_b/api#1", "acme/api#", "acme/api#1x", "acme/api #1", "../api#1", "acme/a/b#1",
                                 "x" * 40 + "/api#1", "acme/api#1234567890"])
def test_an_issue_reference_has_the_shape_of_an_owner_and_a_repo(world, ref):
    w = world
    assert hub_post(w, "/tasks", {**SHOP, "when": "later", "issue_ref": ref}).status_code == 422
    assert direct(w, "POST", "/api/node/tasks", {**SHOP, "when": "later", "issue_ref": ref}).status_code == 422
    assert w.b.db.tasks() == []


def test_an_issue_reference_of_a_real_owner_and_repo_is_kept(world):
    r = hub_post(world, "/tasks", {**SHOP, "when": "later", "issue_ref": "Acme-Co/my.repo_x-1#99999"})
    t = task_on_b(world, r.json()["data"]["id"])
    assert r.status_code == 200 and t["issue_number"] == 99999 and t["issue_url"] == "https://github.com/Acme-Co/my.repo_x-1/issues/99999"


@pytest.mark.parametrize("name", ["../x", "a--b", "-x", "x" * 64, "a/b", "a b", "a\nb", ".hidden", "x_", ""])
def test_a_session_name_from_another_node_has_the_boards_own_shape(world, name):
    w = world
    assert hub_post(w, "/sessions", {"project": "shop", "repo": "api", "name": name}).status_code == 422
    assert direct(w, "POST", "/api/node/sessions", {"project": "shop", "repo": "api", "name": name}).status_code == 422
    assert w.two.tmux_b["created"] == []


def test_the_reserved_clone_name_is_refused_by_the_board(world):
    r = hub_post(world, "/sessions", {"project": "shop", "repo": "api", "name": "clone"})
    assert r.status_code == 400 and "reserved" in r.json()["error"] and world.two.tmux_b["created"] == []


@pytest.mark.parametrize("project,repo", [("..", "api"), ("shop", ".."), ("shop/api", "x"), ("shop", "api/x"), ("sh op", "api"), ("-shop", "api"), ("shop", "")])
def test_a_project_or_repo_that_is_not_a_plain_name_never_reaches_a_path(world, project, repo):
    w = world
    for call in (lambda: hub_post(w, "/sessions", {"project": project, "repo": repo}), lambda: hub_post(w, "/tasks", {**SHOP, "project": project, "repo": repo})):
        assert call().status_code == 422
    assert w.two.tmux_b["created"] == []


def test_two_opens_at_once_never_choose_the_same_automatic_name(world, monkeypatch):
    import threading
    import time as _t
    from app import main
    w = world
    real = main._free_session_name

    def slow(project, repo):
        n = real(project, repo)
        _t.sleep(0.15)                                  # the window in which a second open would choose the same name
        return n
    monkeypatch.setattr(main, "_free_session_name", slow)
    got, errs = [], []

    def go():
        try:
            got.append(nr.peer_session_open(w.b.db, {}, {"project": "shop", "repo": "api"})["tmux"])
        except Exception as e:                           # noqa: BLE001
            errs.append(repr(e))
    with w.b.enter():
        ts = [threading.Thread(target=go) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
    assert errs == [] and len(set(got)) == 2, (got, errs)


def test_the_origin_is_a_plain_label_whatever_the_caller_reported(world):
    w = world
    with w.a.enter():
        token = nodes._load_outgoing(w.reg["peer_id"])
    hostile = "../../etc/passwd; rm -rf <b>x</b> " + "y" * 500
    r = w.b.call("POST", "/api/node/tasks", owner=False, headers=bearer(token, **{"X-CCBoard-Acting-User": hostile}), json={**SHOP, "when": "later"})
    assert r.status_code == 200, r.text
    origin = json.loads(task_on_b(w, r.json()["id"])["origin"])
    assert set(origin) == {"node", "user"} and origin["node"] == "node-a"
    assert len(origin["user"]) <= 64 and not set(origin["user"]) & set("/\\<>;\"'`$|&()[]{}=*?!#%^~") and ".." not in origin["user"], origin


def test_a_claimed_name_is_cut_to_letters_digits_and_a_few_marks():
    ok = nr._claim("alice@example.com", 64)
    assert ok == "alice@example.com"
    for bad in ("a/b", "..", "../x", "a\x00b", "a\x1bb", "<script>", "a`b", "a$(b)", "é", "‮txt", "x" * 500, "  ", None, 7):
        got = nr._claim(bad, 41)
        assert len(got) <= 41 and not set(got) & set("/\\<>;\"'`$|&()[]{}=\x00\x1b‮") and ".." not in got and got == got.strip(), (bad, got)


# ---------------------------------------------------------------- the missing-repo hint shows a GitHub slug and nothing else

def hub_repo(w, name: str, remote: str | None):
    p = git_init(w.two.a_projects / "shop" / name)
    if remote:
        subprocess.run(["git", "-C", str(p), "remote", "add", "origin", remote], check=True)
    return p


def test_a_remote_url_with_credentials_never_reaches_the_answer(world):
    w = world
    user, tok = "ci-bot", "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    hub_repo(w, "credrepo", f"https://{user}:{tok}@github.com/acme/credrepo.git")
    r = hub_post(w, "/tasks", {**SHOP, "repo": "credrepo"})
    assert r.status_code == 404 and r.json()["reason"] == "repo_missing"
    assert "acme/credrepo" in r.json()["error"] and "Nothing was cloned or created" in r.json()["error"]
    assert tok not in r.text and user not in r.text and "@" not in r.json()["error"] and "https://" not in r.json()["error"]
    assert tok not in everything_audited(w) and user not in everything_audited(w)


@pytest.mark.parametrize("remote", ["https://ci:" + "tok" + "en123@git.internal.example/srv/private/privrepo.git", "ssh://git@git.internal.example:2222/srv/secret-path/privrepo.git",
                                    "/srv/private/privrepo.git", "https://github.com.evil.example/acme/privrepo.git", "https://u:p/ss@github.com/acme/privrepo.git"])
def test_a_remote_that_is_not_a_github_slug_is_not_shown_at_all(world, remote):
    w = world
    hub_repo(w, "privrepo", remote)
    r = hub_post(w, "/tasks", {**SHOP, "repo": "privrepo"})
    err = r.json()["error"]
    assert r.status_code == 404 and err == "privrepo is not on node-b. Nothing was created.", err


def test_the_missing_repo_text_goes_through_the_redaction(world, monkeypatch):
    w = world
    secret = "xox" + "b-" + "1234567890-abcdefghijklmnop"
    from app import node_state
    monkeypatch.setattr(node_state, "repo_slug", lambda path: "acme/" + secret)
    hub_repo(w, "oddrepo", None)
    r = hub_post(w, "/tasks", {**SHOP, "repo": "oddrepo"})
    assert r.status_code == 404 and secret not in r.text, r.text


# ---------------------------------------------------------------- what a peer says about what it made is checked, not trusted

def test_a_hostile_peer_answer_loses_every_field_that_is_not_the_shape_it_should_be(world):
    w = world
    secret = "xox" + "b-" + "1234567890-abcdefghijklmnop"
    nodes.peer_transport = Answer({"id": 7, "slug": "../../etc/passwd", "tmux": "not a session name", "branch": "worktree-x;rm -rf /", "phase": "<script>",
                                   "limit_warning": {"kind": "<img src=x onerror=1>", "resets_at": 5, "pct": 90},
                                   "task": {"id": 7, "title": "T " + secret, "phase": "run ning", "agent": "../x", "project": "a/b", "repo": "..", "branch": "x y",
                                            "tmux": "a--b", "issue_ref": "javascript:1", "updated_at": "<now>"}})
    d = hub_post(w, "/tasks", SHOP).json()["data"]
    assert d["slug"] is None and d["tmux"] is None and d["branch"] is None and d["phase"] is None and d["limit_warning"]["kind"] is None
    t = d["task"]
    assert all(t[k] is None for k in ("phase", "agent", "project", "repo", "branch", "tmux", "issue_ref", "updated_at")) and secret not in json.dumps(d) and "[redacted]" in t["title"]
    nodes.peer_transport = Answer({"tmux": "shop--api--ok", "agent": "claude<", "project": "shop", "repo": "../api"})
    s = hub_post(w, "/sessions", {"project": "shop", "repo": "api"}).json()["data"]
    assert s == {"ref": "node-b/shop--api--ok", "tmux": "shop--api--ok", "agent": None, "project": "shop", "repo": None}


def test_a_peers_error_text_is_capped_plain_and_redacted_in_the_hubs_answer(world):
    w = world
    secret = "xox" + "b-" + "1234567890-abcdefghijklmnop"
    nodes.peer_transport = Answer({"error": "boom " + secret + " \x1b[31m" + "z" * 600}, status=409)
    r = hub_post(w, "/tasks", SHOP)
    err = r.json()["error"]
    assert r.status_code == 409 and secret not in err and "\x1b" not in err and len(err) <= 200, err
