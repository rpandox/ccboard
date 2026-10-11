"""Nodes epic P9, issue #142: steer a session on another node (prompt, keys, ack, close) and answer its permission request (the pending list, allow or deny). The six
steering rows of the relay table, over two in-process boards (the `world` fixture of tests/test_nodes_remote_tasks.py: each board has its own projects dir and its own fake tmux).

1. The table and the one route: every row is human only, one row answers a permission, no setting, MCP tool or scheduled job reaches it (read from the source).
2. Prompt: typed once on the peer only, the text rules (422, never stripped), the 8 KB cap (413), busy and queue, the target check on the PEER, 20 a minute per session.
3. Keys: the closed list, C-c with confirm, nothing reaches the terminal on a refusal, the same target check.
4. Ack and close: the session ends only on the peer; audit rows on both sides.
5. Permissions: the list (300 characters, redacted), allow and deny resolve the waiting hook, every other decision is a 422, answered / gone / expired pass through.
6. Who may: the hook token, a node token and a node:* user get a 403 and an audit row on every row; the peer wants an acting user and the scope itself.
7. Truth and storage: unconfirmed writes, a hostile peer answer, and no prompt, key sequence or permission summary in either database, any audit row or the log.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import nodes, nodes_relay as nr
from tests.test_nodes_remote_tasks import (Answer, Raises, WIDER_SESSIONS, _fresh_scan, _hub_online, _wide, audit, bearer, git_init, make_session_on_b, world)  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
ACTING = "alice@example.com"
PROMPT = "MARKER-steer-prompt-71c0de please add a login page"
SUMMARY_MARKER = "MARKER-steer-summary-5be2a9"
SECRET = "sk-ant-" + "api03-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3"           # shaped like a key, built here, never a literal
SESSION = "shop--api--s1"
NEW_ROWS = ("prompt", "keys", "ack", "close", "permissions", "permission_answer")
WIDE = "wider permissions than another node may use"


def hub(w, method: str, path: str, body=None, **kw):
    """A signed-in person on the hub board (board a) calling its relay route for the peer."""
    return w.a.call(method, f"/api/nodes/{w.h}{path}", **({} if body is None else {"json": body}), **kw)


def peer(w, method: str, path: str, body=None, *, token=None, acting: str | None = ACTING):
    """What the hub's transport would send, by hand: a valid token on the peer's wrapper (the hub's checks bypassed), with or without an acting user."""
    if token is None:
        with w.a.enter():
            token = nodes._load_outgoing(w.reg["peer_id"])
    extra = {"X-CCBoard-Acting-User": acting} if acting else {}
    return w.b.call(method, path, owner=False, headers=bearer(token, **extra), **({} if body is None else {"json": body}))


def sess(w, name: str = "s1", state: str = "idle", **body) -> str:
    return make_session_on_b(w, name, state, **body)


def dump(board) -> str:
    return "\n".join(board.db.conn.iterdump())


def tmux_effects(w) -> tuple:
    """Everything that reached the peer's fake terminal: pasted text, typed text, named keys, lines."""
    s = w.two.tmux_b
    return (list(s["pasted"]), list(s["texts"]), list(s["keys"]), list(s["sent"]))


def audit_all(w) -> str:
    return json.dumps([audit(w.a), audit(w.b)])


def seed_permission(w, tm: str = SESSION, summary: str = "Bash: npm test", tool: str = "Bash", created_at: str | None = None) -> int:
    with w.b.enter():
        pid = w.b.db.perm_add(tm, tool, summary, {"command": "x"})
        if created_at:
            w.b.db.conn.execute("UPDATE permissions SET created_at=? WHERE id=?", (created_at, pid))
        return pid


@pytest.fixture
def waiter():
    """A hook that is long-polling on the peer: the board's own waiter table with an event the answer must set (the loop is a stand-in that runs the callback at once)."""
    from app import permissions
    made: list = []

    def add(pid: int):
        ev = threading.Event()
        permissions._waiters[pid] = (SimpleNamespace(call_soon_threadsafe=lambda fn, *a: fn(*a)), ev)
        made.append(pid)
        return ev
    yield add
    for pid in made:
        permissions._waiters.pop(pid, None)


# ================================================================ 1. the table, and the one route that answers a permission

def test_every_new_row_is_human_only_names_an_acting_user_and_only_these_two_rows_touch_permissions():
    rows = [nr.BY_NAME[n] for n in NEW_ROWS]
    assert all(r.human_only and r.acting for r in rows)
    assert {r.name for r in nr.RELAY if r.scope == "permissions"} == {"permissions", "permission_answer"}
    assert nr.BY_NAME["permission_answer"].peer_path == "/api/node/permissions/{pid}/{decision}" and nr.BY_NAME["permissions"].rate_class == "read"
    assert {r.name for r in nr.RELAY if r.scope == "sessions" and r.peer_method != "GET"} == {"session_open", "prompt", "keys", "ack", "close"}


def test_no_relay_row_has_a_command_and_no_row_reaches_the_boards_own_permission_route():
    for r in nr.RELAY:
        assert "/command" not in r.peer_path and "/command" not in r.hub_path, "slash commands are not relayed in v1"
        assert not r.peer_path.startswith("/api/permission") and not r.hub_path.startswith("/api/permission"), r.name
    assert "command" not in nr.HANDLERS and "tune" not in nr.HANDLERS and "restart" not in nr.HANDLERS


def test_the_only_code_that_decides_a_permission_is_the_boards_route_the_expiry_and_the_one_peer_handler():
    """Read from the source: who may write a decision. A new caller (a relay row, an MCP tool, a scheduled job, a poller) fails this test until someone reviews it."""
    callers = {}
    for f in sorted((ROOT / "app").glob("*.py")):
        text = f.read_text()
        for name in ("perm_decide(", "api_permission_decide(", "permissions.wake("):
            n = len(re.findall(re.escape(name), text))
            if n:
                callers[(f.name, name)] = n
    assert set(callers) == {("db.py", "perm_decide("), ("main.py", "perm_decide("), ("main.py", "permissions.wake("), ("main.py", "api_permission_decide("),
                            ("hooks.py", "permissions.wake("), ("nodes_relay.py", "api_permission_decide(")}, callers
    relay = (ROOT / "app" / "nodes_relay.py").read_text()
    assert relay.count("api_permission_decide(") == 1 and "perm_decide(" not in relay and "/api/permission" not in relay.replace("/api/permission_", "")
    for f in ("nodes_hub.py", "nodes_stream.py", "nodes_discovery.py", "scheduler.py", "taskflow.py", "mcp.py", "mcp_remote.py", "nodes.py", "node_state.py"):
        text = (ROOT / "app" / f).read_text()
        assert "perm_decide" not in text and "api_permission_decide" not in text and "/api/permission/" not in text and "permissions.wake" not in text, f


def test_the_mcp_tools_have_no_permission_tool_and_no_call_of_theirs_reaches_a_permission_session_or_node_route():
    from app import mcp
    assert not [n for n in mcp.TOOL_NAMES if re.search(r"permission|allow|approve|answer|node|keys|prompt|ack|close|kill", n)], mcp.TOOL_NAMES
    assert mcp.required_scope("allow_permission", {}) is None and mcp.required_scope("answer_permission", {}) is None, "no such tool exists to hold a scope"
    paths: list[str] = []

    def api(method, path, body=None):
        paths.append(f"{method} {path}")
        return {"projects": [], "tasks": [], "task": {"id": 1, "phase": "backlog"}, "id": 1, "tmux": "p--r--s", "result": None}
    samples = {"project": "p", "repo": "r", "title": "t", "prompt": "x", "task_id": 1, "session": "p--r--s", "dispatch": True, "force": True, "queue": True}
    for tool in mcp.TOOLS:
        args = {k: samples[k] for k in tool["inputSchema"]["properties"] if k in samples}
        try:
            mcp.tool_call(tool["name"], args, api)
        except RuntimeError:
            pass
    assert len(paths) >= len(mcp.TOOLS) - 1, paths
    assert not [p for p in paths if "permission" in p or "/api/node" in p or "/keys" in p or "/ack" in p or p.startswith("DELETE")], paths
    for text in ((ROOT / "scripts" / "ccboard_mcp.py").read_text(), (ROOT / "app" / "mcp_remote.py").read_text()):
        assert "/api/permission" not in text and "/api/node" not in text and "perm_decide" not in text


def test_there_is_no_auto_allow_setting_for_remote_requests():
    from app.config import Settings
    names = " ".join(Settings.model_fields) if hasattr(Settings, "model_fields") else " ".join(vars(Settings))
    assert not re.search(r"auto[_-]?(allow|approve|answer)|allow[_-]?remote|remote[_-]?(allow|answer|approve)", names, re.I), names
    for r in nr.RELAY:
        assert not re.search(r"auto|always|remember", r.name + r.audit_action, re.I)


# ================================================================ 2. prompt

def test_a_prompt_arrives_at_the_peers_terminal_exactly_once_and_the_hub_types_nothing(world):
    w = world
    tm = sess(w)
    r = hub(w, "POST", f"/sessions/{tm}/prompt", {"text": PROMPT})
    assert r.status_code == 200, r.text
    assert r.json() == {"node": "node-b", "age": 0, "data": {"ok": True, "pasted": True, "queued": False}}
    assert [p for p in w.two.tmux_b["pasted"] if p[0] == tm] == [(tm, PROMPT, True)], "once, with Enter"
    assert w.two.tmux_a["pasted"] == [] and w.two.tmux_a["texts"] == [] and w.two.tmux_a["keys"] == []
    assert [c for c in w.count.paths if "/prompt" in c[1]] == [("POST", f"/api/node/sessions/{tm}/prompt")], "one request to the peer"


def test_the_text_is_not_stored_the_event_says_how_long_it_was_and_who_sent_it(world, caplog):
    w = world
    tm = sess(w)
    caplog.set_level(logging.DEBUG)
    assert hub(w, "POST", f"/sessions/{tm}/prompt", {"text": PROMPT}).status_code == 200
    assert PROMPT not in dump(w.a) and PROMPT not in dump(w.b) and "MARKER-steer" not in dump(w.a) + dump(w.b)
    assert "MARKER-steer" not in audit_all(w) and "MARKER-steer" not in caplog.text
    with w.b.enter():
        ev = [e for e in w.b.db.conn.execute("SELECT message, payload FROM events WHERE event='BoardPrompt'").fetchall()]
    assert len(ev) == 1 and ev[0][0] == f"prompt from another node ({len(PROMPT)} characters)"
    assert json.loads(ev[0][1])["by"] == "node node-a for alice@example.com"


def test_a_prompt_writes_an_out_row_and_an_in_row_with_the_acting_user_and_no_text(world):
    w = world
    tm = sess(w)
    hub(w, "POST", f"/sessions/{tm}/prompt", {"text": PROMPT})
    out, inn = audit(w.a, action="send_prompt")[0], audit(w.b, action="send_prompt")[0]
    assert (out["direction"], out["status"], out["user"], out["target"]) == ("out", "ok", ACTING, f"prompt {tm}")
    assert (inn["direction"], inn["status"], inn["user"], inn["target"]) == ("in", "ok", f"for {ACTING}", f"prompt {tm}")


@pytest.mark.parametrize("bad", ["a\x03b", "a\x1b[2Jb", "a\x00b", "a\x7fb", "a\rb", "a\x85b", "a‮b", "a​b", "a b", "a b", "a﻿b", "a­b", "a⁠b"])
def test_a_control_format_or_separator_character_is_a_422_on_the_hub_and_the_peer_and_nothing_is_typed(world, bad):
    w = world
    tm = sess(w)
    n = w.count.seen
    r = hub(w, "POST", f"/sessions/{tm}/prompt", {"text": "start " + bad + " end"})
    assert r.status_code == 422 and r.json()["reason"] == "invalid" and "no control characters, format characters" in r.json()["error"], r.text
    assert w.count.seen == n, "refused before any call"
    p = peer(w, "POST", f"/api/node/sessions/{tm}/prompt", {"text": "start " + bad + " end"})
    assert p.status_code == 422 and "no control characters, format characters" in p.json()["error"], p.text
    assert w.two.tmux_b["pasted"] == [] and w.two.tmux_b["texts"] == []
    assert bad not in r.text and bad not in p.text, "the refusal never repeats the text"


def test_tab_and_newline_are_text_and_a_crlf_is_one_newline(world):
    w = world
    tm = sess(w)
    assert hub(w, "POST", f"/sessions/{tm}/prompt", {"text": "one\ttwo\r\nthree\nfour"}).status_code == 200
    assert w.two.tmux_b["pasted"][-1][1] == "one\ttwo\nthree\nfour"


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": "   \n\t "}, {"text": 5}, {"text": ["a"]}, {"text": "x", "queue": "yes"}, {"text": "x", "queue": 1}, {"text": "x", "enter": False},
                                  {"text": "x", "keys": ["Enter"]}, {"text": "x", "permission_mode": "plan"}, {"text": "x", "agent": "claude"}])
def test_the_prompt_model_is_strict_on_both_sides(world, body):
    w = world
    tm = sess(w)
    n = w.count.seen
    assert hub(w, "POST", f"/sessions/{tm}/prompt", body).status_code == 422 and w.count.seen == n
    assert peer(w, "POST", f"/api/node/sessions/{tm}/prompt", body).status_code == 422
    assert w.two.tmux_b["pasted"] == []


def test_text_over_8_kb_is_a_413_on_both_sides_and_exactly_8_kb_goes_through(world):
    w = world
    tm = sess(w)
    n = w.count.seen
    r = hub(w, "POST", f"/sessions/{tm}/prompt", {"text": "x" * (nr.STEER_MAX + 1)})
    assert r.status_code == 413 and r.json()["reason"] == "too_large" and "text" in r.json()["error"], r.text
    assert w.count.seen == n
    p = peer(w, "POST", f"/api/node/sessions/{tm}/prompt", {"text": "x" * (nr.STEER_MAX + 1)})
    assert p.status_code == 413 and p.json()["reason"] == "too_large"
    assert w.two.tmux_b["pasted"] == []
    assert nr.STEER_MAX == 8192 and hub(w, "POST", f"/sessions/{tm}/prompt", {"text": "x" * nr.STEER_MAX}).status_code == 200
    assert len(w.two.tmux_b["pasted"][-1][1]) == 8192


@pytest.mark.parametrize("spelling", ["--dangerously-skip-permissions", "bypassPermissions", "danger-full-access", "--yolo"])
def test_the_guard_reads_the_text_too_so_a_spelling_of_a_bypass_is_refused(world, spelling):
    w = world
    tm = sess(w)
    assert hub(w, "POST", f"/sessions/{tm}/prompt", {"text": f"run it with {spelling}"}).status_code == 422
    assert peer(w, "POST", f"/api/node/sessions/{tm}/prompt", {"text": f"run it with {spelling}"}).status_code == 422
    assert w.two.tmux_b["pasted"] == []


def test_a_working_session_is_a_409_busy_with_the_peers_reason_and_retry_and_queue_is_accepted(world):
    w = world
    tm = sess(w, state="working")
    r = hub(w, "POST", f"/sessions/{tm}/prompt", {"text": PROMPT})
    assert r.status_code == 409 and r.json()["reason"] == "busy", r.text
    j = r.json()
    assert j["error"] == "the session is working" and j["code"] == "working" and j["retry"] == 5 and j["state"] == "working" and j["node"] == "node-b"
    assert w.two.tmux_b["pasted"] == []
    row = audit(w.a, action="send_prompt")[0]
    assert row["status"] == "failed" and PROMPT not in json.dumps(row)
    q = hub(w, "POST", f"/sessions/{tm}/prompt", {"text": PROMPT, "queue": True})
    assert q.status_code == 200 and q.json()["data"] == {"ok": True, "pasted": True, "queued": True}
    assert len(w.two.tmux_b["pasted"]) == 1


@pytest.mark.parametrize("kind,retry", [("compacting", 15), ("permission_pending", 10)])
def test_a_compacting_session_or_one_with_a_permission_pending_says_why_and_when_to_try_again(world, kind, retry):
    w = world
    tm = sess(w)
    with w.b.enter():
        if kind == "compacting":
            w.b.db.update_flags(tm, {"compacting": True})
        else:
            w.b.db.perm_add(tm, "Bash", "Bash: ls", {})
    r = hub(w, "POST", f"/sessions/{tm}/prompt", {"text": "hi"})
    assert r.status_code == 409 and r.json()["reason"] == "busy" and r.json()["code"] == kind and r.json()["retry"] == retry, r.text


def test_a_session_that_is_gone_is_the_peers_404_and_a_name_that_is_no_session_is_a_422(world):
    w = world
    r = hub(w, "POST", f"/sessions/{SESSION}/prompt", {"text": "hi"})
    assert r.status_code == 404 and r.json()["reason"] == "not_found", r.text
    n = w.count.seen
    for bad in ("not-a-session", "_ccboard-login", "shop--api--s1;id", "a--b"):
        assert hub(w, "POST", f"/sessions/{bad}/prompt", {"text": "hi"}).status_code in (404, 422)
    assert hub(w, "POST", "/sessions/shop--api--s1;id/prompt", {"text": "hi"}).status_code == 422 and w.count.seen == n


def test_a_pair_without_the_sessions_scope_is_409_on_the_hub_with_no_call_and_403_on_the_peer(world):
    w = world
    tm = sess(w)
    reg = w.pair_up(["read", "tasks", "permissions"])
    n = w.count.seen
    r = w.a.post(f"/api/nodes/{reg['handle']}/sessions/{tm}/prompt", json={"text": "hi"})
    assert r.status_code == 409 and r.json()["reason"] == "scope" and r.json()["error"] == "needs the sessions scope on node-b", r.text
    assert w.count.seen == n
    with w.a.enter():
        token = nodes._load_outgoing(reg["peer_id"])
    p = peer(w, "POST", f"/api/node/sessions/{tm}/prompt", {"text": "hi"}, token=token)
    assert p.status_code == 403 and "sessions scope" in p.json()["error"]
    assert w.two.tmux_b["pasted"] == []
    assert any(x["action"] == "scope_refused" and x["status"] == "refused" for x in audit(w.b))


def test_the_peer_takes_20_prompts_a_minute_into_one_session_and_the_hub_counts_too(world):
    w = world
    tm, other = sess(w), sess(w, "s2")
    for i in range(nodes.PROMPT_RATE):
        assert hub(w, "POST", f"/sessions/{tm}/prompt", {"text": f"p{i}"}).status_code == 200, i
    seen = w.count.seen
    r = hub(w, "POST", f"/sessions/{tm}/prompt", {"text": "one more"})
    assert r.status_code == 429 and r.json()["reason"] == "rate_limited" and r.headers["retry-after"].isdigit(), r.text
    assert w.count.seen == seen, "the hub refused it itself: nothing reached the peer"
    assert audit(w.a, action="send_prompt")[0]["status"] == "refused"
    assert len([p for p in w.two.tmux_b["pasted"] if p[0] == tm]) == nodes.PROMPT_RATE
    assert hub(w, "POST", f"/sessions/{other}/prompt", {"text": "another session"}).status_code == 200, "the count is per session"
    # the peer does not trust the hub's count: reset the hub's, ask the peer directly
    nodes.relay_prompt_limiter.clear()
    d = peer(w, "POST", f"/api/node/sessions/{tm}/prompt", {"text": "direct"})
    assert d.status_code == 429 and d.json()["reason"] == "rate_limited" and d.headers["retry-after"].isdigit() and nodes.PROMPT_RATE == 20
    assert len([p for p in w.two.tmux_b["pasted"] if p[0] == tm]) == nodes.PROMPT_RATE, "the peer typed nothing"
    assert any(x["action"] == "send_prompt" and x["status"] == "refused" and x["detail"] == "rate_limited" for x in audit(w.b))
    nodes.relay_prompt_limiter.clear()
    h = hub(w, "POST", f"/sessions/{tm}/prompt", {"text": "hub sees the peer's 429"})
    assert h.status_code == 429 and h.json()["reason"] == "rate_limited" and "Retry-After" in h.headers


# ---- the target is checked on the PEER: steering a session is launching work in it

@pytest.mark.parametrize("body", WIDER_SESSIONS, ids=lambda b: ",".join(f"{k}={v}" for k, v in b.items() if k != "launcher") or "no-mode")
def test_a_session_that_runs_wider_than_the_remote_set_or_says_no_mode_takes_neither_text_nor_keys_even_with_a_valid_token_and_scope(world, body):
    w = world
    tm = sess(w, "wide", **body)
    before = tmux_effects(w)
    for method, path, payload in (("POST", f"/sessions/{tm}/prompt", {"text": PROMPT}), ("POST", f"/sessions/{tm}/keys", {"key": "Enter"}),
                                  ("POST", f"/sessions/{tm}/keys", {"key": "y"})):
        h = hub(w, method, path, payload)
        d = peer(w, method, "/api/node" + path, payload)
        for r in (h, d):
            assert r.status_code == 409 and WIDE in r.json()["error"], (path, r.status_code, r.text)
    assert tmux_effects(w) == before, "nothing reached the terminal"
    assert PROMPT not in audit_all(w) and PROMPT not in dump(w.a) + dump(w.b)


def test_there_is_no_command_route_for_a_node_token_and_nothing_is_typed(world):
    w = world
    tm = sess(w)
    before = tmux_effects(w)
    r = peer(w, "POST", f"/api/node/sessions/{tm}/command", {"cmd": "/model", "arg": "opus"})
    assert r.status_code == 403 and r.json()["error"] == "this node token does not open that route"
    assert hub(w, "POST", f"/sessions/{tm}/command", {"cmd": "/model"}).status_code in (404, 405)
    assert peer(w, "POST", f"/api/sessions/{tm}/command", {"cmd": "/model"}).status_code == 403, "the board's own command route is closed to a node token"
    assert tmux_effects(w) == before
    assert any(x["action"] == "route_refused" and x["status"] == "refused" for x in audit(w.b))


def test_a_tmux_session_the_board_did_not_start_and_a_shell_row_are_not_targets(world):
    w = world
    foreign, shell = "shop--api--foreign", "shop--api--sh"
    for name, cmd in ((foreign, "claude"), (shell, "zsh")):
        w.two.tmux_b["sessions"][name] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%7", "command": cmd, "path": "/x", "pid": 1, "env": {}}
    with w.b.enter():                                       # the foreign one has no row at all; this one is the board's own shell session
        w.b.db.add_session(tmux_name=shell, project="shop", repo="api", name="sh", launcher="shell", cmd="zsh", agent="shell")
    before = tmux_effects(w)
    for target, why in ((foreign, "not a session this board started"), (shell, "not an agent session")):
        for path, payload in ((f"/sessions/{target}/prompt", {"text": "hi"}), (f"/sessions/{target}/keys", {"key": "Enter"})):
            r = hub(w, "POST", path, payload)
            assert r.status_code == 409 and why in r.json()["error"], (target, r.text)
            assert peer(w, "POST", "/api/node" + path, payload).status_code == 409
    assert tmux_effects(w) == before


# ================================================================ 3. keys

@pytest.mark.parametrize("key", list(nr.STEER_KEYS))
def test_each_key_of_the_list_reaches_the_peers_terminal_and_the_hubs_never(world, key):
    w = world
    tm = sess(w)
    body = {"key": key, **({"confirm": True} if key == "C-c" else {})}
    r = hub(w, "POST", f"/sessions/{tm}/keys", body)
    assert r.status_code == 200 and r.json()["data"] == {"ok": True, "key": key}, r.text
    if key in nr.LITERAL_KEYS:
        assert w.two.tmux_b["texts"] == [(tm, key, False)] and w.two.tmux_b["keys"] == [], "typed as one character, no Enter"
    else:
        assert w.two.tmux_b["keys"] == [(tm, [key])] and w.two.tmux_b["texts"] == []
    assert w.two.tmux_a["keys"] == [] and w.two.tmux_a["texts"] == []
    assert audit(w.a, action="send_keys")[0]["target"] == f"keys {tm} ({key})" and audit(w.b, action="send_keys")[0]["user"] == f"for {ACTING}"


def test_the_list_is_exactly_the_issues_and_c_c_is_the_only_confirmed_key():
    assert nr.STEER_KEYS == ("Enter", "Escape", "Up", "Down", "Tab", "y", "n", "1", "2", "3", "4", "5", "6", "7", "8", "9", "C-c")
    assert nr.CONFIRMED_KEYS == ("C-c",) and nr.DECISIONS == ("allow", "deny")


@pytest.mark.parametrize("key", ["Left", "Right", "BSpace", "Space", "PageUp", "C-u", "C-z", "C-l", "Home", "End", "BTab", "enter", "ENTER", "Enter ", " y", "Y", "N", "0", "10", "yy", "1,2",
                                  "C-C", "c-c", "Ctrl-c", "^C", "Escape;", "Enter\n", "\x03", "\x1b", "ｙ", "１", "", "-l", "a", "q", "/"])
def test_a_key_outside_the_list_is_a_422_on_both_sides_and_reaches_no_terminal(world, key):
    w = world
    tm = sess(w)
    n = w.count.seen
    before, runs = tmux_effects(w), len(w.two.tmux_b["run"])
    r = hub(w, "POST", f"/sessions/{tm}/keys", {"key": key})
    assert r.status_code == 422 and r.json()["reason"] == "invalid" and w.count.seen == n, r.text
    p = peer(w, "POST", f"/api/node/sessions/{tm}/keys", {"key": key})
    assert p.status_code == 422, p.text
    assert tmux_effects(w) == before and len(w.two.tmux_b["run"]) == runs, "no send-keys, no paste"


def test_c_c_without_confirm_is_a_422_and_a_confirm_on_any_other_key_is_too(world):
    w = world
    tm = sess(w)
    n = w.count.seen
    for body in ({"key": "C-c"}, {"key": "C-c", "confirm": False}):
        r = hub(w, "POST", f"/sessions/{tm}/keys", body)
        assert r.status_code == 422 and "C-c needs confirm true" in r.json()["error"], r.text
        assert peer(w, "POST", f"/api/node/sessions/{tm}/keys", body).status_code == 422
    for body in ({"key": "Enter", "confirm": True}, {"key": "y", "confirm": True}):
        r = hub(w, "POST", f"/sessions/{tm}/keys", body)
        assert r.status_code == 422 and "confirm is only for C-c" in r.json()["error"], r.text
    assert w.count.seen == n and w.two.tmux_b["keys"] == [] and w.two.tmux_b["texts"] == []
    assert hub(w, "POST", f"/sessions/{tm}/keys", {"key": "C-c", "confirm": True}).status_code == 200 and w.two.tmux_b["keys"] == [(tm, ["C-c"])]


@pytest.mark.parametrize("body", [{}, {"keys": ["Enter"]}, {"key": ["Enter"]}, {"key": 1}, {"key": "Enter", "text": "rm -rf"}, {"key": "Enter", "enter": True}, {"key": "Enter", "confirm": "yes"},
                                  {"key": "Enter", "confirm": 1}, {"text": "x"}])
def test_the_keys_model_is_strict_one_key_and_a_real_boolean(world, body):
    w = world
    tm = sess(w)
    before = tmux_effects(w)
    assert hub(w, "POST", f"/sessions/{tm}/keys", body).status_code == 422
    assert peer(w, "POST", f"/api/node/sessions/{tm}/keys", body).status_code == 422
    assert tmux_effects(w) == before


def test_a_key_needs_the_sessions_scope_like_a_prompt(world):
    w = world
    tm = sess(w)
    reg = w.pair_up(["read", "tasks", "permissions"])
    assert w.a.post(f"/api/nodes/{reg['handle']}/sessions/{tm}/keys", json={"key": "Enter"}).json()["reason"] == "scope"
    with w.a.enter():
        token = nodes._load_outgoing(reg["peer_id"])
    assert peer(w, "POST", f"/api/node/sessions/{tm}/keys", {"key": "Enter"}, token=token).status_code == 403
    assert w.two.tmux_b["keys"] == []


# ================================================================ 4. ack and close

def test_ack_clears_attention_on_the_peer_only_and_answers_no_request(world):
    w = world
    tm = sess(w, state="waiting")
    pid = seed_permission(w, tm)
    before = tmux_effects(w)
    r = hub(w, "POST", f"/sessions/{tm}/ack")
    assert r.status_code == 200 and r.json()["data"] == {"acked": tm}, r.text
    with w.b.enter():
        assert w.b.db.conn.execute("SELECT acked_at FROM sessions WHERE tmux_name=?", (tm,)).fetchone()[0], "the peer's row is acked"
        assert w.b.db.perm_get(pid)["decision"] is None, "ack answers no request"
    assert tmux_effects(w) == before
    assert audit(w.a, action="ack_session")[0]["user"] == ACTING and audit(w.b, action="ack_session")[0]["user"] == f"for {ACTING}"


def test_close_ends_the_session_on_the_peer_only_with_audit_rows_on_both_sides(world):
    w = world
    tm = sess(w)
    w.two.tmux_a["sessions"][tm] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%3", "command": "claude", "path": "/a", "pid": 1, "env": {}}
    r = hub(w, "DELETE", f"/sessions/{tm}")
    assert r.status_code == 200 and r.json()["data"] == {"killed": tm}, r.text
    assert tm not in w.two.tmux_b["sessions"] and tm in w.two.tmux_a["sessions"], "it ended on the peer; the hub's own session of that name is untouched"
    with w.b.enter():
        assert tm not in w.b.db.open_rows()
    out, inn = audit(w.a, action="close_session")[0], audit(w.b, action="close_session")[0]
    assert (out["direction"], out["status"], out["user"], out["target"]) == ("out", "ok", ACTING, f"close {tm}")
    assert (inn["direction"], inn["status"], inn["user"]) == ("in", "ok", f"for {ACTING}")
    again = hub(w, "DELETE", f"/sessions/{tm}")
    assert again.status_code == 404 and again.json()["reason"] == "not_found"


def test_close_and_ack_need_the_sessions_scope(world):
    w = world
    tm = sess(w)
    reg = w.pair_up(["read", "tasks", "permissions"])
    with w.a.enter():
        token = nodes._load_outgoing(reg["peer_id"])
    for method, path in (("DELETE", f"/sessions/{tm}"), ("POST", f"/sessions/{tm}/ack")):
        r = w.a.call(method, f"/api/nodes/{reg['handle']}{path}")
        assert r.status_code == 409 and r.json()["reason"] == "scope" and r.json()["error"] == "needs the sessions scope on node-b"
        assert peer(w, method, "/api/node" + path, token=token).status_code == 403
    assert tm in w.two.tmux_b["sessions"]


def test_the_peers_functions_themselves_refuse_the_internal_login_session_even_if_a_name_got_past_the_checks(world):
    w = world
    internal = "_ccboard-login"
    w.two.tmux_b["sessions"][internal] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%8", "command": "claude", "path": "/x", "pid": 1, "env": {}}
    before = tmux_effects(w)
    with w.b.enter():
        for fn, body in ((nr.peer_close, {}), (nr.peer_ack, {}), (nr.peer_prompt, {"text": "hi"}), (nr.peer_keys, {"key": "Enter"})):
            with pytest.raises(Exception):
                fn(w.b.db, {"name": internal}, body)
    assert internal in w.two.tmux_b["sessions"] and tmux_effects(w) == before


def test_close_and_ack_want_an_acting_user_and_a_real_ccboard_session_on_the_peer(world):
    w = world
    tm = sess(w)
    for method, path in (("DELETE", f"/api/node/sessions/{tm}"), ("POST", f"/api/node/sessions/{tm}/ack")):
        r = peer(w, method, path, acting=None)
        assert r.status_code == 403 and "acting" in r.json()["error"], r.text
    assert tm in w.two.tmux_b["sessions"]
    assert any(x["action"] == "close_session" and x["status"] == "refused" and x["detail"] == "no acting user named" for x in audit(w.b))
    for bad in ("_ccboard-login", "not-a-session", "shop--api--s1;id"):
        for method, path in (("DELETE", f"/api/node/sessions/{bad}"), ("POST", f"/api/node/sessions/{bad}/ack")):
            assert peer(w, method, path).status_code == 422, (method, bad)
    assert peer(w, "DELETE", "/api/node/sessions/shop--api--ghost").status_code == 404
    assert tm in w.two.tmux_b["sessions"]


# ================================================================ 5. permissions

def test_the_list_carries_id_session_tool_summary_and_since_and_nothing_else(world):
    w = world
    tm = sess(w)
    pid = seed_permission(w, tm, "Bash: npm test")
    r = hub(w, "GET", "/permissions")
    assert r.status_code == 200, r.text
    rows = r.json()["data"]["permissions"]
    assert len(rows) == 1 and set(rows[0]) == {"id", "tmux", "tool", "summary", "since"}
    assert (rows[0]["id"], rows[0]["tmux"], rows[0]["tool"], rows[0]["summary"]) == (pid, tm, "Bash", "Bash: npm test") and rows[0]["since"]
    assert r.json()["node"] == "node-b" and r.json()["age"] == 0


def test_the_summary_is_cut_to_300_characters_and_redacted_on_the_peer_and_again_on_the_hub(world):
    w = world
    tm = sess(w)
    seed_permission(w, tm, f"Bash: echo {SUMMARY_MARKER} " + "x" * 600 + f" {SECRET}")
    seed_permission(w, tm, f"Bash: export TOKEN={SECRET} {SUMMARY_MARKER}")
    for r in (hub(w, "GET", "/permissions"), peer(w, "GET", "/api/node/permissions")):
        assert r.status_code == 200, r.text
        rows = r.json()["data"]["permissions"] if "data" in r.json() else r.json()["permissions"]
        assert len(rows) == 2 and all(len(x["summary"]) <= 300 for x in rows)
        assert SECRET not in r.text and "A1b2C3d4E5f6G7h8I9j0" not in r.text
        assert "[redacted]" in rows[1]["summary"]
    # a careless peer: the hub cuts and redacts again whatever the peer sent
    nodes.peer_transport = Answer({"permissions": [{"id": 4, "tmux": tm, "tool": "Bash", "summary": "y" * 900 + " " + SECRET, "since": "2026-10-10T00:00:00Z", "extra": "no", "input": {"a": 1}},
                                                   {"id": "x", "tmux": tm, "tool": "Bash", "summary": "bad id"}, {"id": 5, "tmux": "not a session", "summary": "bad name"},
                                                   {"id": 6, "tmux": tm, "tool": "a b;c", "summary": "weird tool\x03\x1b", "since": "never"}, "junk", None], "token": SECRET})
    r = hub(w, "GET", "/permissions")
    d = r.json()["data"]
    assert r.status_code == 200 and set(d) == {"permissions"} and [x["id"] for x in d["permissions"]] == [4, 6]
    assert all(len(x["summary"]) <= 300 for x in d["permissions"]) and SECRET not in r.text and d["permissions"][1]["tool"] == "tool" and d["permissions"][1]["since"] is None
    assert set(d["permissions"][0]) == {"id", "tmux", "tool", "summary", "since"} and "\x03" not in r.text


def test_the_summary_is_in_no_audit_row_no_log_line_and_not_in_the_hubs_database(world, caplog):
    w = world
    tm = sess(w)
    pid = seed_permission(w, tm, f"Bash: cat {SUMMARY_MARKER}")
    caplog.set_level(logging.DEBUG)
    assert SUMMARY_MARKER in hub(w, "GET", "/permissions").text, "the person is shown it"
    assert hub(w, "POST", f"/permissions/{pid}/deny").status_code == 200
    assert SUMMARY_MARKER not in audit_all(w) and SUMMARY_MARKER not in dump(w.a) and SUMMARY_MARKER not in caplog.text
    with w.b.enter():
        lines = [ln for ln in w.b.db.conn.iterdump() if SUMMARY_MARKER in ln]
    assert len(lines) == 1 and lines[0].startswith('INSERT INTO "permissions"'), "on the peer it is only in the request the agent made, as before"
    for row in audit(w.a) + audit(w.b):
        assert SUMMARY_MARKER not in json.dumps(row)


def test_the_list_hides_a_request_nobody_is_waiting_for_and_one_whose_session_is_gone(world):
    w = world
    tm = sess(w)
    keep = seed_permission(w, tm, "Bash: keep")
    seed_permission(w, tm, "Bash: old", created_at="2020-01-01T00:00:00+00:00")
    seed_permission(w, "shop--api--ghost", "Bash: ghost")
    done = seed_permission(w, tm, "Bash: done")
    with w.b.enter():
        w.b.db.perm_decide(done, "tui", "test")
    assert [x["id"] for x in hub(w, "GET", "/permissions").json()["data"]["permissions"]] == [keep]


def test_the_list_needs_the_permissions_scope_not_read_or_sessions(world):
    w = world
    sess(w)
    reg = w.pair_up(["read", "tasks", "sessions"])
    n = w.count.seen
    r = w.a.get(f"/api/nodes/{reg['handle']}/permissions")
    assert r.status_code == 409 and r.json()["reason"] == "scope" and r.json()["error"] == "needs the permissions scope on node-b" and w.count.seen == n
    with w.a.enter():
        token = nodes._load_outgoing(reg["peer_id"])
    p = peer(w, "GET", "/api/node/permissions", token=token)
    assert p.status_code == 403 and "permissions scope" in p.json()["error"]
    assert peer(w, "GET", "/api/node/permissions", acting=None).status_code == 403, "even with the scope the peer wants a named person"


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_allow_and_deny_resolve_the_waiting_hook_on_the_peer_once(world, waiter, decision):
    w = world
    tm = sess(w)
    pid = seed_permission(w, tm)
    ev = waiter(pid)
    r = hub(w, "POST", f"/permissions/{pid}/{decision}")
    assert r.status_code == 200 and r.json()["data"] == {"ok": True, "id": pid, "decision": decision}, r.text
    assert ev.is_set(), "the hook that was long-polling is woken"
    with w.b.enter():
        row = w.b.db.perm_get(pid)
    assert row["decision"] == decision and row["source"] == "node node-a for alice@example.com" and row["decided_at"]
    out, inn = audit(w.a, action="answer_permission")[0], audit(w.b, action="answer_permission")[0]
    assert (out["direction"], out["status"], out["user"], out["target"]) == ("out", "ok", ACTING, f"permission {pid} {decision}")
    assert (inn["direction"], inn["status"], inn["user"], inn["target"]) == ("in", "ok", f"for {ACTING}", f"permission {pid} {decision}")
    again = hub(w, "POST", f"/permissions/{pid}/{'deny' if decision == 'allow' else 'allow'}")
    assert again.status_code == 409 and again.json()["reason"] == "answered", "answered once, not twice"
    with w.b.enter():
        assert w.b.db.perm_get(pid)["decision"] == decision


@pytest.mark.parametrize("decision", ["always", "tui", "ALLOW", "Allow", "allow ", "yes", "allow-always", "allow;id", "1", "deny\n"])
def test_any_decision_but_allow_or_deny_is_a_422_on_the_hub_and_the_peer_and_decides_nothing(world, decision):
    w = world
    tm = sess(w)
    pid = seed_permission(w, tm)
    n = w.count.seen
    from urllib.parse import quote
    seg = quote(decision, safe="%")
    h = hub(w, "POST", f"/permissions/{pid}/{seg}")
    p = peer(w, "POST", f"/api/node/permissions/{pid}/{seg}")
    assert h.status_code == 422 and h.json()["reason"] == "invalid" and "decision is not valid" in h.json()["error"], h.text
    assert p.status_code == 422 and w.count.seen == n
    with w.b.enter():
        assert w.b.db.perm_get(pid)["decision"] is None


@pytest.mark.parametrize("body", [{"always": True}, {"rule": "Bash(*)"}, {"remember": True}, {"decision": "allow"}, {"x": 1}, [], "allow"])
def test_an_extra_field_or_a_body_that_is_no_object_is_a_422_and_no_rule_is_made(world, body):
    w = world
    tm = sess(w)
    pid = seed_permission(w, tm)
    n = w.count.seen
    assert hub(w, "POST", f"/permissions/{pid}/allow", body).status_code == 422 and w.count.seen == n
    assert peer(w, "POST", f"/api/node/permissions/{pid}/allow", body).status_code == 422
    with w.b.enter():
        assert w.b.db.perm_get(pid)["decision"] is None


@pytest.mark.parametrize("pid", ["x", "1x", "-1", "1.5", "٣", "1" * 13, "0x1"])
def test_a_request_id_is_digits_on_both_sides(world, pid):
    w = world
    n = w.count.seen
    assert hub(w, "POST", f"/permissions/{pid}/allow").status_code == 422 and w.count.seen == n
    assert peer(w, "POST", f"/api/node/permissions/{pid}/allow").status_code == 422


def test_an_answered_gone_or_expired_request_is_the_peers_409_or_404_passed_through_and_writes_nothing(world):
    w = world
    tm = sess(w)
    sentence = "This request was already answered. Open terminal to see the current prompt."
    pid = seed_permission(w, tm)
    with w.b.enter():
        w.b.db.perm_decide(pid, "tui", "terminal")
    r = hub(w, "POST", f"/permissions/{pid}/allow")
    assert r.status_code == 409 and r.json()["reason"] == "answered" and r.json()["error"] == sentence, r.text
    r = hub(w, "POST", "/permissions/9999/allow")
    assert r.status_code == 404 and r.json()["reason"] == "gone" and r.json()["error"] == "This request is gone. Open terminal to see the current prompt.", r.text
    old = seed_permission(w, tm, created_at="2020-01-01T00:00:00+00:00")
    r = hub(w, "POST", f"/permissions/{old}/allow")
    assert r.status_code == 409 and r.json()["reason"] == "expired" and r.json()["error"].startswith("This request has expired."), r.text
    ghost = seed_permission(w, "shop--api--ghost")
    assert hub(w, "POST", f"/permissions/{ghost}/allow").json()["reason"] == "gone"
    with w.b.enter():
        assert w.b.db.perm_get(old)["decision"] is None and w.b.db.perm_get(ghost)["decision"] is None and w.b.db.perm_get(pid)["decision"] == "tui"
    for x in audit(w.a, action="answer_permission"):
        assert x["status"] == "failed"


def test_the_permission_answer_needs_the_scope(world):
    w = world
    tm = sess(w)
    pid = seed_permission(w, tm)
    reg = w.pair_up(["read", "tasks", "sessions"])
    n = w.count.seen
    r = w.a.post(f"/api/nodes/{reg['handle']}/permissions/{pid}/allow")
    assert r.status_code == 409 and r.json()["reason"] == "scope" and w.count.seen == n
    with w.a.enter():
        token = nodes._load_outgoing(reg["peer_id"])
    p = peer(w, "POST", f"/api/node/permissions/{pid}/allow", token=token)
    assert p.status_code == 403 and "permissions scope" in p.json()["error"]
    with w.b.enter():
        assert w.b.db.perm_get(pid)["decision"] is None


def test_the_peer_wants_a_named_person_for_an_answer_even_with_the_scope_and_a_good_request(world):
    w = world
    tm = sess(w)
    pid = seed_permission(w, tm)
    nobody = peer(w, "POST", f"/api/node/permissions/{pid}/allow", acting=None)
    assert nobody.status_code == 403 and "acting" in nobody.json()["error"], nobody.text
    assert peer(w, "POST", f"/api/node/permissions/{pid}/allow", acting="   ").status_code == 403
    assert any(x["action"] == "answer_permission" and x["status"] == "refused" and x["detail"] == "no acting user named" for x in audit(w.b))
    with w.b.enter():
        assert w.b.db.perm_get(pid)["decision"] is None
    assert peer(w, "POST", f"/api/node/permissions/{pid}/allow").status_code == 200


# ================================================================ 6. who may: a signed-in person and nobody else

def concrete(row: nr.Row, w) -> tuple[str, str, dict | None]:
    path = row.hub_path.replace("{handle}", w.h).replace("{name}", SESSION).replace("{pid}", "1").replace("{decision}", "allow")
    return row.hub_method, path, {"text": "hi"} if row.name == "prompt" else {"key": "Enter"} if row.name == "keys" else None


@pytest.mark.parametrize("name", NEW_ROWS)
def test_the_hook_token_gets_a_403_and_an_audit_row_on_every_new_row_and_nothing_reaches_the_peer(world, name):
    w = world
    sess(w)
    row = nr.BY_NAME[name]
    method, path, body = concrete(row, w)
    before, n = tmux_effects(w), w.count.seen
    r = w.a.call(method, path, owner=False, headers={"X-CCBoard-Token": w.a.hook_token}, **({} if body is None else {"json": body}))
    assert r.status_code == 403 and r.json()["reason"] == "human_only" and "signed-in person" in r.json()["error"], r.text
    r2 = w.a.call(method, path, owner=False, headers={"X-CCBoard-Token": w.a.hook_token, "X-CCBoard": "1", **{"Tailscale-User-Login": ACTING}},
                  **({} if body is None else {"json": body}))
    assert r2.status_code == 403, "the hook token with a person's headers beside it is still the hook token"
    assert w.count.seen == n and tmux_effects(w) == before
    rows = audit(w.a, action=row.audit_action)
    assert len(rows) == 2 and all(x["direction"] == "out" and x["status"] == "refused" and x["user"] == "local-token" and x["detail"].startswith("human_only") for x in rows), rows
    assert audit(w.b) == [] or all(x["action"] not in (row.audit_action,) for x in audit(w.b))


@pytest.mark.parametrize("name", NEW_ROWS)
def test_a_node_token_gets_a_403_and_an_audit_row_on_the_hubs_route_of_every_new_row(world, name):
    w = world
    sess(w)
    row = nr.BY_NAME[name]
    method, path, body = concrete(row, w)
    with w.a.enter():
        _, tok = nodes.add_incoming({"id": "ts:nCALLER000001", "name": "caller", "url": "https://100.64.0.9"}, list(nodes.SCOPES), db=w.a.db)
    n = w.count.seen
    r = w.a.call(method, path, owner=False, headers=bearer(tok), **({} if body is None else {"json": body}))
    assert r.status_code == 403 and r.json()["error"] == "this node token does not open that route", r.text
    assert w.count.seen == n
    rows = [x for x in audit(w.a) if x["action"] == "route_refused" and x["target"] == f"{method} {path}"[:120]]
    assert rows and rows[0]["direction"] == "in" and rows[0]["status"] == "refused"


@pytest.mark.parametrize("name", NEW_ROWS)
def test_a_node_user_that_got_past_the_middleware_is_refused_by_the_relay_itself_with_an_audit_row(world, name):
    """Defence in depth: the middleware refuses a node token before a handler, and relay() refuses a `node:*` user too if one ever got there."""
    w = world
    sess(w)
    row = nr.BY_NAME[name]
    _, path, body = concrete(row, w)
    params = {"name": SESSION, "pid": "1", "decision": "allow"}
    req = SimpleNamespace(state=SimpleNamespace(user="node:caller"), headers={"x-ccboard": "1"})
    n = w.count.seen
    with w.a.enter():
        with pytest.raises(nr.RelayError) as e:
            nr.relay(w.h, row, {k: v for k, v in params.items() if k in row.params}, json.dumps(body or {}).encode() if row.peer_method != "GET" else {}, req, db=w.a.db)
    assert e.value.status == 403 and e.value.reason == "forbidden"
    assert w.count.seen == n
    rows = audit(w.a, action=row.audit_action)
    assert rows and rows[0]["status"] == "refused" and rows[0]["user"] == "node:caller"


def test_the_relay_needs_the_csrf_header_even_for_the_list_and_calls_nothing_without_it(world):
    w = world
    sess(w)
    n = w.count.seen
    for name in NEW_ROWS:
        method, path, body = concrete(nr.BY_NAME[name], w)
        r = w.a.call(method, path, headers={"X-CCBoard": ""}, **({} if body is None else {"json": body}))
        assert r.status_code == 403 and r.json()["error"] == "missing X-CCBoard header", (name, r.text)
    assert w.count.seen == n


def test_a_board_with_no_pair_answers_404_and_writes_no_audit_row_for_the_new_routes(lite_client, fake_tmux):
    from app import hooks, main
    for method, path, body in (("POST", "/api/nodes/node-b/sessions/shop--api--s1/prompt", {"text": "hi"}), ("DELETE", "/api/nodes/node-b/sessions/shop--api--s1", None),
                               ("POST", "/api/nodes/node-b/permissions/1/allow", None), ("GET", "/api/nodes/node-b/permissions", None)):
        r = lite_client.request(method, path, headers={"Tailscale-User-Login": ACTING, "X-CCBoard": "1"}, **({} if body is None else {"json": body}))
        assert r.status_code == 404 and r.json()["reason"] == "unknown_node"
        h = lite_client.request(method, path, headers={"X-CCBoard-Token": hooks.ensure_token()}, **({} if body is None else {"json": body}))
        assert h.status_code == 403 and h.json()["reason"] == "human_only"
    assert main.db.node_audit_list(50) == []


def test_every_peer_wrapper_refuses_a_person_and_the_hook_token_but_the_owner_still_has_the_boards_own_routes(world):
    w = world
    tm = sess(w)
    pid = seed_permission(w, tm)
    before = tmux_effects(w)
    for row in (nr.BY_NAME[n] for n in NEW_ROWS):
        path = row.peer_path.replace("{name}", tm).replace("{pid}", str(pid)).replace("{decision}", "allow")
        for headers in ({"Tailscale-User-Login": ACTING, "X-CCBoard": "1"}, {"X-CCBoard-Token": w.b.hook_token}):
            with w.b.enter():
                r = w.two.client.request(row.peer_method, path, headers=headers)
            assert r.status_code == 403 and "takes a node token" in r.json()["error"], (row.name, r.status_code)
    with w.b.enter():
        assert w.b.db.perm_get(pid)["decision"] is None
    assert tmux_effects(w) == before
    assert w.b.post(f"/api/permission/{pid}/allow").status_code == 200, "the owner on the peer's own board answers as before"


def test_the_local_routes_behave_as_shipped(world):
    """Regression: the board's own prompt, keys, ack, kill and permission routes (no node involved) are what they were."""
    w = world
    tm = sess(w)
    assert w.b.post(f"/api/sessions/{tm}/prompt", json={"text": "local prompt"}).json() == {"ok": True, "pasted": True, "queued": False}
    assert w.two.tmux_b["pasted"][-1] == (tm, "local prompt", True)
    with w.b.enter():
        ev = w.b.db.conn.execute("SELECT message FROM events WHERE event='BoardPrompt'").fetchone()[0]
    assert ev == "local prompt", "a local prompt keeps its text in the event, as before"
    assert w.b.post(f"/api/sessions/{tm}/keys", json={"keys": ["Enter"]}).json() == {"ok": True}
    assert w.b.post(f"/api/sessions/{tm}/ack").json() == {"acked": tm}
    pid = seed_permission(w, tm)
    assert w.b.post(f"/api/permission/{pid}/tui").json() == {"ok": True, "id": pid, "decision": "tui"}, "the board's own route still takes tui"
    assert w.b.delete(f"/api/sessions/{tm}").json() == {"killed": tm}


# ================================================================ 7. truth: unconfirmed writes, hostile peers

@pytest.mark.parametrize("exc", [TimeoutError("slow"), ConnectionResetError("reset"), BrokenPipeError("pipe")])
@pytest.mark.parametrize("name", ["prompt", "keys", "ack", "close", "permission_answer"])
def test_a_write_that_may_have_gone_out_is_unconfirmed_and_never_retried(world, name, exc):
    w = world
    row = nr.BY_NAME[name]
    method, path, body = concrete(row, w)
    t = Raises(exc)
    nodes.peer_transport = t
    r = hub(w, method, path.replace(f"/api/nodes/{w.h}", ""), body)
    assert r.status_code in (502, 504) and r.json()["reason"] == "unconfirmed", r.text
    assert "not retried" in r.json()["error"] and t.seen == 1
    x = audit(w.a, action=row.audit_action)[0]
    assert x["status"] == "failed" and "unconfirmed" in x["detail"] and "MARKER" not in json.dumps(x)


def test_a_hostile_busy_answer_keeps_only_a_word_a_number_and_the_peers_capped_sentence(world):
    w = world
    tm = sess(w)
    nodes.peer_transport = Answer({"error": "busy " + SECRET + " " + "z" * 500, "reason": "busy", "code": "working; rm -rf", "retry": 10 ** 9, "state": "X" * 80,
                                   "wait_kind": "idle", "html": "<script>"}, 409)
    r = hub(w, "POST", f"/sessions/{tm}/prompt", {"text": "hi"})
    j = r.json()
    assert r.status_code == 409 and j["reason"] == "busy" and j["code"] is None and j["retry"] is None and j["state"] is None and j["wait_kind"] == "idle"
    assert len(j["error"]) <= 160 and SECRET not in r.text and "html" not in j and "script" not in r.text


def test_a_reason_the_hub_does_not_know_is_not_passed_on_and_a_peers_answer_is_rebuilt(world):
    w = world
    nodes.peer_transport = Answer({"error": "nope", "reason": "root_shell"}, 409)
    r = hub(w, "POST", f"/permissions/3/allow")
    assert r.status_code == 409 and r.json()["reason"] == "refused"
    nodes.peer_transport = Answer({"ok": True, "id": 3, "decision": "allow", "shell": "x", "summary": SUMMARY_MARKER})
    r = hub(w, "POST", f"/permissions/3/allow")
    assert r.json()["data"] == {"ok": True, "id": 3, "decision": "allow"} and SUMMARY_MARKER not in r.text
    nodes.peer_transport = Answer({"ok": True, "id": 3, "decision": "always"})
    assert hub(w, "POST", f"/permissions/3/allow").json()["data"] == {"ok": True, "id": 3, "decision": None}
    nodes.peer_transport = Answer({"ok": True, "key": "C-u", "x": 1})
    assert hub(w, "POST", f"/sessions/{SESSION}/keys", {"key": "Enter"}).json()["data"] == {"ok": True, "key": None}
    nodes.peer_transport = Answer({"killed": "x; id", "acked": {"a": 1}})
    assert hub(w, "DELETE", f"/sessions/{SESSION}").json()["data"] == {"killed": None}


def test_a_node_the_hub_has_not_read_or_that_is_offline_is_refused_before_any_call_on_every_new_row(world, monkeypatch):
    from app import main
    from tests.test_nodes_remote_tasks import FakeHub
    w = world
    sess(w)
    for rec, reason in (({"status": "offline", "age_s": 700}, "offline"), ({"polled_at": None, "last_ok_at": None}, "not_read_yet"), ({"status": "unauthorized"}, "needs_repair")):
        monkeypatch.setattr(main, "_hub", lambda rec=rec: FakeHub(**rec))
        n = w.count.seen
        for name in NEW_ROWS:
            method, path, body = concrete(nr.BY_NAME[name], w)
            r = w.a.call(method, path, **({} if body is None else {"json": body}))
            assert r.status_code in (409, 503) and r.json()["reason"] == reason, (name, r.text)
        assert w.count.seen == n, "nothing was sent"
        assert all(x["status"] == "refused" for x in audit(w.a) if x["action"] in {nr.BY_NAME[n_].audit_action for n_ in NEW_ROWS})


def test_no_log_line_holds_a_prompt_a_permission_summary_a_secret_or_a_token(world, caplog, waiter):
    w = world
    tm = sess(w)
    pid = seed_permission(w, tm, f"Bash: cat {SUMMARY_MARKER} {SECRET}")
    waiter(pid)
    caplog.set_level(logging.DEBUG)
    with w.a.enter():
        token = nodes._load_outgoing(w.reg["peer_id"])
    hub(w, "POST", f"/sessions/{tm}/prompt", {"text": PROMPT + " " + SECRET})
    hub(w, "POST", f"/sessions/{tm}/keys", {"key": "y"})
    hub(w, "GET", "/permissions")
    hub(w, "POST", f"/permissions/{pid}/allow")
    hub(w, "POST", f"/sessions/{tm}/ack")
    hub(w, "DELETE", f"/sessions/{tm}")
    for needle in (PROMPT, "MARKER-steer", SECRET, token):
        assert needle not in caplog.text, needle[:12]
        assert needle not in audit_all(w), needle[:12]
        assert needle not in dump(w.a), needle[:12]


# ================================================================ 2b. text that is typed never starts like a terminal command

COMMAND_STARTS = ["/model opus", "/permissions", "!rm -rf x", "!ls", "# remember this", "#note", "／model opus", "！ls", "＃note",            # full-width / ! #
                  "  /model opus", "\n/model opus", "\n\n  !ls", "\t#note", "　/model opus", " ！ls", "\r\n/model"]                    # past spaces, blank lines, an ideographic space


@pytest.mark.parametrize("bad", COMMAND_STARTS)
def test_a_prompt_that_starts_like_a_terminal_command_is_a_422_on_the_hub_and_on_the_peer_and_nothing_is_typed(world, bad):
    w = world
    tm = sess(w)
    n, before = w.count.seen, tmux_effects(w)
    r = hub(w, "POST", f"/sessions/{tm}/prompt", {"text": bad})
    assert r.status_code == 422 and r.json()["reason"] == "invalid" and "cannot start with / ! or #" in r.json()["error"], r.text
    assert w.count.seen == n, "refused on the hub before any call to the peer"
    p = peer(w, "POST", f"/api/node/sessions/{tm}/prompt", {"text": bad})
    assert p.status_code == 422 and "cannot start with / ! or #" in p.json()["error"], p.text
    assert tmux_effects(w) == before and w.two.tmux_b["pasted"] == [] and w.two.tmux_b["texts"] == [], "nothing was pasted, typed or sent on the peer"
    assert w.two.tmux_a["pasted"] == [] and w.two.tmux_a["texts"] == []


@pytest.mark.parametrize("ok", ["run tests / fix", "wow!", "fix issue #12", "@src/app.py please read it", "line one\n/not a command because it is a later line\n!neither", "a#b", "what? /help",
                                "¡hola!", "-- not an option", "1. /notes"])
def test_text_with_those_characters_later_or_an_at_mention_passes(world, ok):
    w = world
    tm = sess(w)
    assert hub(w, "POST", f"/sessions/{tm}/prompt", {"text": ok}).status_code == 200
    assert [p for p in w.two.tmux_b["pasted"] if p[0] == tm][-1][1] == ok.replace("\r\n", "\n")


def test_the_rule_is_one_helper_in_check_field_and_used_by_the_prompt_model_and_nothing_else_types_text():
    assert "typed_text" in nr.FIELD_KINDS
    for bad in COMMAND_STARTS:
        with pytest.raises(ValueError, match="cannot start with / ! or #"):
            nr.check_field("typed_text", bad)
        assert nr.check_field("text", bad) == bad.replace("\r\n", "\n"), "plain text keeps its old rule"
    assert nr.check_field("typed_text", "@a/b.py") == "@a/b.py"
    relay = (ROOT / "app" / "nodes_relay.py").read_text()
    assert relay.count('check_field("typed_text"') == 1 and "starts_like_a_command(" in relay


# ================================================================ 8. a session that is asking a question in its terminal: a key or a line would ANSWER it

def make_it_ask(w, tm: str, how: str) -> None:
    """Put the peer's session `tm` in one of the states in which it is asking a question in its terminal."""
    if how == "pending_permission":
        seed_permission(w, tm)
        return
    with w.b.enter():
        if how == "waiting_permission":
            w.b.db.set_state(tm, "waiting", "PermissionRequest")
            w.b.db.update_flags(tm, {"wait_kind": "permission"})
        elif how == "waiting_elicitation":
            w.b.db.set_state(tm, "waiting", "Notification")
            w.b.db.update_flags(tm, {"wait_kind": "elicitation"})
        elif how == "waiting_unnamed":
            w.b.db.set_state(tm, "waiting", "Notification")
            w.b.db.update_flags(tm, {"wait_kind": None})
        elif how == "no_state_yet":
            w.b.db.set_state(tm, "starting", "test")
        else:
            raise AssertionError(how)


ASKING = ["pending_permission", "waiting_permission", "waiting_elicitation", "waiting_unnamed", "no_state_yet"]
ASK_SENTENCE = "needs the permissions scope on node-b: this session is asking a question in its terminal"


def sessions_only(w):
    """A pair that holds `sessions` and not `permissions`: (the registry row, its token)."""
    reg = w.pair_up(["read", "tasks", "sessions"])
    with w.a.enter():
        return reg, nodes._load_outgoing(reg["peer_id"])


def test_the_table_declares_the_scope_a_row_needs_while_its_session_is_asking():
    assert {r.name: r.dialog_scope for r in nr.RELAY if r.dialog_scope} == {"prompt": "permissions", "keys": "permissions"}


@pytest.mark.parametrize("how", ASKING)
@pytest.mark.parametrize("key", ["Enter", "y", "n", "1", "Escape", "Up", "Down", "Tab"])
def test_a_pair_with_sessions_and_without_permissions_cannot_press_a_key_while_the_session_asks(world, how, key):
    """y, n, 1 to 9, Enter, Escape, the arrows and Tab all answer or move through a permission, trust or plan dialog: that is `permissions` business, whatever the key."""
    w = world
    tm = sess(w)
    make_it_ask(w, tm, how)
    reg, token = sessions_only(w)
    before = tmux_effects(w)
    p = peer(w, "POST", f"/api/node/sessions/{tm}/keys", {"key": key}, token=token)
    assert p.status_code == 403 and p.json()["reason"] == "asking" and "permissions scope" in p.json()["error"] and "asking a question in its terminal" in p.json()["error"], p.text
    h = w.a.post(f"/api/nodes/{reg['handle']}/sessions/{tm}/keys", json={"key": key})
    assert h.status_code == 409 and h.json()["reason"] == "scope" and h.json()["error"] == ASK_SENTENCE, h.text
    assert tmux_effects(w) == before, "no key reached the terminal"
    assert any(x["action"] == "send_keys" and x["status"] == "failed" and x["detail"] == "asking" for x in audit(w.b))


@pytest.mark.parametrize("how", ASKING)
@pytest.mark.parametrize("key", ["y", "n", "1", "5", "9", "Enter"])
def test_with_permissions_a_key_that_chooses_an_answer_is_still_refused_while_the_session_asks(world, how, key):
    """A permission is answered by the permission row (a person, allow or deny, once); a key that picks an option is never a way round it."""
    w = world
    tm = sess(w)
    make_it_ask(w, tm, how)
    before = tmux_effects(w)
    for r in (hub(w, "POST", f"/sessions/{tm}/keys", {"key": key}), peer(w, "POST", f"/api/node/sessions/{tm}/keys", {"key": key})):
        assert r.status_code == 409 and r.json()["reason"] == "busy" and r.json()["code"] == "asking" and "Allow or Deny" in r.json()["error"], r.text
    assert tmux_effects(w) == before


@pytest.mark.parametrize("how", ASKING)
@pytest.mark.parametrize("key", ["Escape", "Up", "Down", "Tab"])
def test_with_permissions_the_keys_that_only_move_or_cancel_pass_while_the_session_asks(world, how, key):
    w = world
    tm = sess(w)
    make_it_ask(w, tm, how)
    r = hub(w, "POST", f"/sessions/{tm}/keys", {"key": key})
    assert r.status_code == 200, r.text
    assert w.two.tmux_b["keys"][-1][1:] == ([key],) or key in json.dumps(w.two.tmux_b["keys"][-1])


def test_a_session_at_its_prompt_or_working_takes_every_key_as_before(world):
    w = world
    tm = sess(w)
    for state in ("idle", "working", "done"):
        with w.b.enter():
            w.b.db.set_state(tm, state, "test")
        for key in ("Enter", "y", "1", "Escape"):
            assert hub(w, "POST", f"/sessions/{tm}/keys", {"key": key}).status_code == 200, (state, key)
    with w.b.enter():
        w.b.db.set_state(tm, "waiting", "Notification")
        w.b.db.update_flags(tm, {"wait_kind": "idle"})
    assert hub(w, "POST", f"/sessions/{tm}/keys", {"key": "Enter"}).status_code == 200, "waiting at the idle prompt is not a question"


@pytest.mark.parametrize("how", ASKING)
def test_a_line_pasted_while_the_session_asks_answers_nothing(world, how):
    w = world
    tm = sess(w)
    make_it_ask(w, tm, how)
    before = tmux_effects(w)
    for r in (hub(w, "POST", f"/sessions/{tm}/prompt", {"text": "y", "queue": True}), peer(w, "POST", f"/api/node/sessions/{tm}/prompt", {"text": "y", "queue": True})):
        assert r.status_code == 409 and r.json()["reason"] == "busy", "even with permissions a line is not typed into a question"
    reg, token = sessions_only(w)
    p = peer(w, "POST", f"/api/node/sessions/{tm}/prompt", {"text": "1", "queue": True}, token=token)
    assert p.status_code == 403 and p.json()["reason"] == "asking" and "permissions scope" in p.json()["error"], p.text
    h = w.a.post(f"/api/nodes/{reg['handle']}/sessions/{tm}/prompt", json={"text": "1", "queue": True})
    assert h.status_code == 409 and h.json()["reason"] == "scope" and h.json()["error"] == ASK_SENTENCE, h.text
    assert tmux_effects(w) == before


def test_a_session_whose_wait_state_cannot_be_read_counts_as_asking(world, monkeypatch):
    from app import main
    w = world
    tm = sess(w)

    def boom(name):
        raise RuntimeError("cannot read")
    monkeypatch.setattr(main, "_permission_pending", boom)
    before = tmux_effects(w)
    assert peer(w, "POST", f"/api/node/sessions/{tm}/keys", {"key": "Enter"}).status_code == 409, "with every scope: the answer-capable keys are refused"
    reg, token = sessions_only(w)
    assert peer(w, "POST", f"/api/node/sessions/{tm}/keys", {"key": "Escape"}, token=token).status_code == 403
    assert peer(w, "POST", f"/api/node/sessions/{tm}/prompt", {"text": "hi"}, token=token).status_code == 403
    assert tmux_effects(w) == before
    with w.b.enter():
        assert nr.asking_reason(w.b.db, tm) == "unknown"


def test_a_codex_pane_showing_a_dialog_counts_as_asking(world, monkeypatch):
    from app import main
    w = world
    tm = sess(w)
    monkeypatch.setattr(main, "_pane_block", lambda name, row, *a, **k: ("dialog", "Codex shows its update dialog"))
    before = tmux_effects(w)
    assert peer(w, "POST", f"/api/node/sessions/{tm}/keys", {"key": "y"}).status_code == 409
    reg, token = sessions_only(w)
    assert peer(w, "POST", f"/api/node/sessions/{tm}/keys", {"key": "Enter"}, token=token).status_code == 403
    with w.b.enter():
        assert nr.asking_reason(w.b.db, tm) == "pane"
    assert tmux_effects(w) == before


# ================================================================ 9. one ownership check for every row that names a session

def external_session(w, name: str = "shop--api--ext") -> str:
    """A tmux session with a ccboard-shaped name that the board did not start (no row): the owner's own, started outside the board."""
    w.two.tmux_b["sessions"][name] = {"created": 1, "attached": 1, "windows": 1, "pane_id": "%9", "command": "claude", "path": "/x", "pid": 2, "env": {}}
    return name


ROWS_NAMING_A_SESSION = [("POST", "/sessions/{n}/prompt", {"text": "hi"}), ("POST", "/sessions/{n}/keys", {"key": "Escape"}), ("POST", "/sessions/{n}/ack", None), ("DELETE", "/sessions/{n}", None)]


@pytest.mark.parametrize("method,path,body", ROWS_NAMING_A_SESSION)
def test_every_row_refuses_a_session_the_board_did_not_start_on_the_peer(world, method, path, body):
    w = world
    name = external_session(w)
    before = tmux_effects(w)
    h = hub(w, method, path.format(n=name), body)
    p = peer(w, method, "/api/node" + path.format(n=name), body)
    for r in (h, p):
        assert r.status_code == 409 and "not a session this board started" in r.json()["error"], (path, r.status_code, r.text)
    assert name in w.two.tmux_b["sessions"] and tmux_effects(w) == before
    with w.b.enter():
        assert w.b.db.open_rows().get(name) is None, "nothing was acked or closed in the database either"


@pytest.mark.parametrize("method,path,body", ROWS_NAMING_A_SESSION)
def test_every_row_refuses_a_shell_and_a_clone_row(world, method, path, body):
    w = world
    for launcher in ("shell", "clone"):
        tm = sess(w, "o" + launcher)
        with w.b.enter():
            w.b.db.conn.execute("UPDATE sessions SET launcher=? WHERE tmux_name=?", (launcher, tm))
        r = peer(w, method, "/api/node" + path.format(n=tm), body)
        assert r.status_code == 409 and "not an agent session this board started" in r.json()["error"], (launcher, path, r.text)
        assert tm in w.two.tmux_b["sessions"]


def test_ack_and_close_may_touch_a_session_started_with_wider_permissions_because_they_run_nothing(world):
    w = world
    tm = sess(w, "wide", launcher="claude", mode="bypass", bypass=True)
    assert hub(w, "POST", f"/sessions/{tm}/ack").status_code == 200
    r = hub(w, "DELETE", f"/sessions/{tm}")
    assert r.status_code == 200 and tm not in w.two.tmux_b["sessions"]
    other = sess(w, "w2", launcher="claude", mode="bypass", bypass=True)
    with w.b.enter():
        assert nr.owned_session_refusal(w.b.db, other) is None
        assert nr.session_target_refusal(w.b.db, {"project": "shop", "repo": "api"}, other) == nr.WIDE_SESSION


def test_close_ends_a_session_with_a_terminal_attached_just_as_the_local_delete_does(world):
    w = world
    tm = sess(w)
    w.two.tmux_b["sessions"][tm]["attached"] = 2
    assert hub(w, "DELETE", f"/sessions/{tm}").status_code == 200 and tm not in w.two.tmux_b["sessions"]


def test_the_ack_and_close_handlers_themselves_run_the_ownership_check(world):
    w = world
    name = external_session(w)
    with w.b.enter():
        for fn in (nr.peer_ack, nr.peer_close, nr.peer_prompt, nr.peer_keys):
            with pytest.raises(Exception, match="not a session this board started"):
                fn(w.b.db, {"name": name}, {"text": "hi", "key": "Escape"})


# ================================================================ 10. a permission answer acts on a request the board itself would let a person answer from afar

def listed(w) -> list[int]:
    return [x["id"] for x in hub(w, "GET", "/permissions").json()["data"]["permissions"]]


@pytest.mark.parametrize("tool,summary", [("ExitPlanMode", "ExitPlanMode: {}"), ("AskUserQuestion", "AskUserQuestion: {}"), ("EnterPlanMode", "EnterPlanMode: {}"),
                                          ("EnterWorktree", "EnterWorktree: {}"), ("Frobnicate", "Frobnicate: x"), ("Task", "Task: x"), ("bash", "bash: ls"), ("Bash ", "Bash : ls"),
                                          ("mcp", "mcp: x"), ("mcp__", "mcp__: x"), ("", "something"), ("Bash", ""), ("Bash", "   ")])
def test_a_request_of_a_kind_that_belongs_to_the_terminal_is_neither_listed_nor_answered(world, tool, summary):
    w = world
    tm = sess(w)
    with w.b.enter():
        pid = w.b.db.perm_add(tm, tool, summary, {})
    assert pid not in listed(w), "the page is never offered a button that is refused"
    for decision in ("allow", "deny"):
        for r in (hub(w, "POST", f"/permissions/{pid}/{decision}"), peer(w, "POST", f"/api/node/permissions/{pid}/{decision}")):
            assert r.status_code == 409 and r.json()["reason"] == "refused", r.text
    with w.b.enter():
        assert w.b.db.perm_get(pid)["decision"] is None, "nothing was decided"


@pytest.mark.parametrize("tool", ["Bash", "Edit", "MultiEdit", "Write", "NotebookEdit", "Read", "Glob", "Grep", "WebFetch", "WebSearch", "apply_patch", "mcp__github__create_issue"])
def test_an_ordinary_tool_request_is_listed_and_answered(world, tool):
    w = world
    tm = sess(w)
    with w.b.enter():
        pid = w.b.db.perm_add(tm, tool, f"{tool}: x", {"command": "x"})
    assert pid in listed(w)
    assert hub(w, "POST", f"/permissions/{pid}/deny").status_code == 200


def test_the_list_and_the_answer_agree_about_every_request(world):
    w = world
    tm = sess(w)
    ext = external_session(w)
    ids = {}
    with w.b.enter():
        for tool, name in (("Bash", tm), ("ExitPlanMode", tm), ("Bash", ext), ("Frobnicate", tm), ("Edit", tm)):
            ids[w.b.db.perm_add(name, tool, f"{tool}: x", {})] = (tool, name)
    shown = set(listed(w))
    for pid in ids:
        r = peer(w, "POST", f"/api/node/permissions/{pid}/deny")
        assert (r.status_code == 200) == (pid in shown), (ids[pid], r.status_code, r.text)
    assert {ids[p] for p in shown} == {("Bash", tm), ("Edit", tm)}


def test_a_request_of_a_session_the_board_did_not_start_is_neither_listed_nor_answered(world):
    w = world
    ext = external_session(w)
    with w.b.enter():
        pid = w.b.db.perm_add(ext, "Bash", "Bash: ls", {})
    assert pid not in listed(w)
    for r in (hub(w, "POST", f"/permissions/{pid}/allow"), peer(w, "POST", f"/api/node/permissions/{pid}/allow")):
        assert r.status_code == 409 and "not a session this board started" in r.json()["error"], r.text
    with w.b.enter():
        assert w.b.db.perm_get(pid)["decision"] is None


def test_the_internal_login_session_and_a_shell_row_have_no_answerable_request(world):
    w = world
    w.two.tmux_b["sessions"]["_ccboard-login"] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%8", "command": "claude", "path": "/x", "pid": 1, "env": {}}
    tm = sess(w, "sh")
    with w.b.enter():
        w.b.db.conn.execute("UPDATE sessions SET launcher='shell' WHERE tmux_name=?", (tm,))
        a, b = w.b.db.perm_add("_ccboard-login", "Bash", "Bash: ls", {}), w.b.db.perm_add(tm, "Bash", "Bash: ls", {})
    assert listed(w) == []
    for pid in (a, b):
        assert peer(w, "POST", f"/api/node/permissions/{pid}/allow").status_code == 409


def test_allow_is_a_one_time_allow_in_the_local_function_and_nothing_wider_exists(world, monkeypatch):
    """What the local function is called with: the id, the word allow (or deny) and a stand-in request that names the node. No `always`, no rule, no mode."""
    from app import main
    w = world
    tm = sess(w)
    calls, decided = [], []
    real_decide, real_perm = main.api_permission_decide, w.b.db.perm_decide
    monkeypatch.setattr(main, "api_permission_decide", lambda pid, decision, request: (calls.append((pid, decision, request.state.user)), real_decide(pid, decision, request))[1])
    monkeypatch.setattr(w.b.db, "perm_decide", lambda pid, decision, source: (decided.append((pid, decision, source)), real_perm(pid, decision, source))[1])
    pid = seed_permission(w, tm)
    assert hub(w, "POST", f"/permissions/{pid}/allow").status_code == 200
    assert calls == [(pid, "allow", "node node-a for alice@example.com")] and decided == [(pid, "allow", "node node-a for alice@example.com")]
    assert set(nr.DECISIONS) == {"allow", "deny"}
    # the hook answers the agent with the behavior alone: no updated permissions, no rule, no mode
    hook = (ROOT / "bin" / "ccboard-permission").read_text()
    assert '"behavior": b' in hook and "updatedPermissions" not in hook and "always" not in hook.lower() and "setMode" not in hook
    main_src = (ROOT / "app" / "main.py").read_text()
    assert "updatedPermissions" not in main_src and "permission_suggestions" not in main_src
    relay = (ROOT / "app" / "nodes_relay.py").read_text()
    assert relay.count("api_permission_decide(") == 1 and "perm_decide(" not in relay


@pytest.mark.parametrize("unreadable", ["raises", "no_row"])
def test_a_session_whose_row_cannot_be_read_takes_no_answer_capable_key_and_no_prompt_with_or_without_permissions(world, monkeypatch, unreadable):
    """The test that a stub `asking_reason` (always None) would fail: fail closed on a row that cannot be read."""
    w = world
    tm = sess(w)
    before = tmux_effects(w)

    def broken(name):
        raise RuntimeError("cannot read the row")
    monkeypatch.setattr(w.b.db, "open_row", broken if unreadable == "raises" else (lambda name: None))
    for key in ("y", "n", "1", "9", "Enter"):
        r = peer(w, "POST", f"/api/node/sessions/{tm}/keys", {"key": key})
        assert r.status_code == 409 and r.json()["code"] == "asking" and "Allow or Deny" in r.json()["error"], (key, r.text)
    assert peer(w, "POST", f"/api/node/sessions/{tm}/prompt", {"text": "1"}).status_code == 409
    reg, token = sessions_only(w)
    for key in ("y", "n", "1", "Enter", "Escape"):
        r = peer(w, "POST", f"/api/node/sessions/{tm}/keys", {"key": key}, token=token)
        assert r.status_code == 403 and r.json()["reason"] == "asking" and "permissions scope" in r.json()["error"], (key, r.text)
    assert peer(w, "POST", f"/api/node/sessions/{tm}/prompt", {"text": "1"}, token=token).status_code == 403
    assert tmux_effects(w) == before
    with w.b.enter():
        assert nr.asking_reason(w.b.db, tm) in ("unknown",)


def test_asking_reason_names_what_it_found(world):
    w = world
    tm = sess(w)
    with w.b.enter():
        assert nr.asking_reason(w.b.db, tm) is None, "idle at its prompt"
    for i, (how, want) in enumerate((("pending_permission", "permission"), ("waiting_permission", "dialog"), ("waiting_elicitation", "dialog"),
                                     ("waiting_unnamed", "dialog"), ("no_state_yet", "dialog"))):
        t2 = sess(w, f"q{i}")
        make_it_ask(w, t2, how)
        with w.b.enter():
            assert nr.asking_reason(w.b.db, t2) == want, how
    import inspect
    assert "_unused" not in inspect.getsource(nr) and inspect.getsource(nr.asking_reason).count("return None") == 1, "no stub, no dead twin"
