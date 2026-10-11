"""MCP across nodes (issue #152, part 1): a `node` argument on the six tools, the tool `list_nodes`, over the stdio shim and the remote /mcp endpoint.

What is pinned here, in the order of the checks a call goes through:

1. The table: exactly six RELAY rows say `mcp` (the reads card, state, task, agents and the writes task_create and task_dispatch); `Row` refuses to be built wrong; no tool schema
   or tool name holds a permission, a key, a sandbox, a bypass, args or an epic.
2. The gate: CCBOARD_MCP_NODES off (the default) -> the sentence and no call; on -> the caller's own scope, then the pair's, then the usual relay checks. The hook token (the shim)
   and a device token reach the six rows and NOTHING else (a walk of the whole table, over HTTP and in-process); a dispatch into a running session is never allowed for an agent.
3. The tools end to end, once through the shim's tool_call (hub routes over HTTP with the hook token) and once through /mcp (in process as the device token), on two real boards:
   what the peer made, with which origin, the refs, the audit on both boards, the guard, the safe launch options, unconfirmed and offline, redaction, rate limits.
4. list_nodes and GET /api/mcp/nodes: what is shown, what never is, unknown values as null.

Two in-process boards (tests/conftest.py), fake tmux, no real agent binary, no socket, no real home.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
from types import SimpleNamespace

import pytest

from app import mcp, nodes, nodes_relay as nr
from app.config import settings
from tests.test_nodes_relay import SAMPLE, ask, hub_path
from tests.test_nodes_remote_tasks import (SHOP, Answer, Count, FakeHub, Raises, _fresh_scan, _hub_online, _wide, audit, git_init, task_on_b, world)  # noqa: F401  (fixtures)

ROOT = pathlib.Path(__file__).resolve().parent.parent
ID = {"Tailscale-User-Login": "alice@example.com"}
OFF_TEXT = "this board does not let agents act on other nodes (CCBOARD_MCP_NODES is off)"
SIX = {"card", "state", "task", "agents", "task_create", "task_dispatch"}
FORBIDDEN_WORDS = ("permission", "bypass", "sandbox", "approval", "args", "allowed_tools", "allowedtools", "keys", "tools", "model", "opts", "dangerous", "yolo", "epic")


def load_shim():
    spec = importlib.util.spec_from_file_location("ccboard_mcp_for_nodes", str(ROOT / "scripts" / "ccboard_mcp.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def nodes_on(monkeypatch):
    monkeypatch.setattr(settings, "mcp_nodes", True)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """The /mcp limiter's clock: `clock["t"] += 61` opens a fresh minute."""
    from app import mcp_remote
    mcp_remote.limiter.reset()
    c = {"t": 1000.0}
    monkeypatch.setattr(mcp_remote, "_clock", lambda: c["t"])
    yield c
    mcp_remote.limiter.reset()


# ---------------------------------------------------------------- the two transports behind one interface

class Shim:
    """The stdio shim's tool_call, its call_api replaced by the board's HTTP routes through board a's test client with the hook token (what the real shim sends over loopback)."""
    kind, by = "shim", "local-token"

    def __init__(self, w):
        self.w, self.m = w, load_shim()
        self.m.call_api = self.call_api

    def call_api(self, method, path, body=None):
        w = self.w
        kw = {} if body is None else {"json": body}
        r = w.a.call(method, path, owner=False, headers={"X-CCBoard-Token": w.a.hook_token, "X-CCBoard": "1"}, **kw)
        if r.status_code >= 400:
            try:
                msg = r.json().get("error")
            except ValueError:
                msg = r.text
            raise RuntimeError(f"ccboard {r.status_code}: {msg}")
        return r.json()

    def call(self, name, **args):
        """(is_error, payload): the parsed answer, or the error text."""
        try:
            return False, self.m.tool_call(name, dict(args))
        except RuntimeError as e:
            return True, str(e)


class Remote:
    """The /mcp endpoint on board a with a device token named `laptop` that holds `scopes`."""
    kind = "mcp"

    def __init__(self, w, name="laptop", scopes=None):
        self.w, self.name = w, name
        self.by = f"mcp:{name}"
        assert w.a.call("PUT", "/api/mcp/remote", json={"enabled": True}).status_code == 200
        r = w.a.post("/api/mcp/tokens", json={"name": name, **({"scopes": scopes} if scopes else {})})
        assert r.status_code == 201, r.text
        self.token = r.json()["token"]

    def raw(self, name, **args):
        return self.w.a.call("POST", "/mcp", owner=False, headers={**ID, "Authorization": f"Bearer {self.token}"},
                             json={"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": name, "arguments": args}})

    def call(self, name, **args):
        r = self.raw(name, **args)
        assert r.status_code == 200, r.text
        j = r.json()
        if "error" in j:
            return True, j["error"]["message"]
        res = j["result"]
        text = res["content"][0]["text"]
        return res["isError"], (text if res["isError"] else json.loads(text))


@pytest.fixture(params=["shim", "mcp"])
def agent(request, world):
    return Shim(world) if request.param == "shim" else Remote(world)


def err(result, *needles):
    is_err, text = result
    assert is_err is True, text
    for n in needles:
        assert n in text, (n, text)
    return text


def ok(result):
    is_err, payload = result
    assert is_err is False, payload
    return payload


# ================================================================ 1. the table and the schemas

def test_exactly_six_rows_are_open_to_an_agent_and_none_of_them_is_a_persons():
    assert {r.name for r in nr.RELAY if r.mcp} == SIX
    assert all(r.human_only != r.mcp for r in nr.RELAY)
    for r in nr.RELAY:
        if r.mcp:
            assert r.scope in nr.MCP_SCOPES and not r.acting and not r.per_session and not r.dialog_scope and r.big_field is None, r.name
            assert r.peer_method == "GET" or r.guard, f"{r.name}: a write an agent may start runs the guard on both sides"
    for name in ("prompt", "keys", "ack", "close", "permissions", "permission_answer", "pane", "session_open"):
        assert not nr.BY_NAME[name].mcp and nr.BY_NAME[name].human_only, name


@pytest.mark.parametrize("make,why", [
    (dict(human_only=True), "a person's act"),
    (dict(acting=True), "a person's act by name"),
    (dict(scope="sessions"), "a scope an agent never holds"),
    (dict(scope="permissions"), "a scope an agent never holds"),
    (dict(per_session=True), "a prompt into one session"),
    (dict(dialog_scope="permissions"), "a dialog answer"),
    (dict(big_field="text"), "free text beyond a task"),
    (dict(peer_method="POST", hub_method="POST", rate_class=nr.WRITE), "a write that starts nothing guarded"),
], ids=lambda x: x if isinstance(x, str) else "")
def test_a_row_cannot_be_opened_to_an_agent_when_it_is_a_persons_act_a_prompt_a_dialog_or_a_scope_beyond_read_and_tasks(make, why):
    base = dict(name="x", hub_method="GET", hub_path="/api/nodes/{handle}/x", peer_method="GET", peer_path="/api/node/x", scope="read", body_model=nr.NoFields,
                rate_class=nr.READ, audit_action="x", mcp=True)
    with pytest.raises(ValueError, match=why):
        nr.Row(**{**base, **make})
    nr.Row(**base)                                          # and the plain read row is fine


def _names(schema):
    """Every property name at any depth of a JSON schema."""
    out = []
    if isinstance(schema, dict):
        for k, v in schema.items():
            if k == "properties" and isinstance(v, dict):
                for name, sub in v.items():
                    out.append(name)
                    out += _names(sub)
            else:
                out += _names(v)
    elif isinstance(schema, list):
        for v in schema:
            out += _names(v)
    return out


def test_no_tool_schema_has_a_permission_sandbox_bypass_args_tools_or_keys_property_and_no_tool_is_one():
    assert mcp.TOOL_NAMES == ("list_projects", "create_task", "list_tasks", "get_task_status", "dispatch_task", "get_task_result", "list_nodes")
    for t in mcp.TOOLS:
        assert t["inputSchema"]["additionalProperties"] is False
        for prop in _names(t["inputSchema"]):
            assert not any(w in prop.lower() for w in FORBIDDEN_WORDS), (t["name"], prop)
        for text in re.findall(r'"description": "([^"]*)"', json.dumps(t)):
            assert not re.search(r"permission|bypass|sandbox|approval|allowed_tools|keys\b|dangerous", text, re.I), (t["name"], text)
    assert not [n for n in mcp.TOOL_NAMES if re.search(r"permission|key|close|kill|answer|approve|epic|prompt|session", n)], "no tool answers, types, closes or runs an epic"
    assert mcp.required_scope("epic_status", {}) is None and mcp.required_scope("epic_run", {}) is None and mcp.required_scope("allow_permission", {}) is None


def test_every_tool_but_list_nodes_takes_an_optional_node_and_both_servers_list_the_same_tools():
    by = {t["name"]: t["inputSchema"] for t in mcp.TOOLS}
    for name, schema in by.items():
        if name == "list_nodes":
            assert schema["properties"] == {}
        else:
            assert schema["properties"]["node"]["type"] == "string" and schema["properties"]["node"]["default"] == "local" and "node" not in schema.get("required", []), name
    for t in mcp.TOOLS:
        assert "list_nodes" in t["description"] or t["name"] == "list_nodes", f"{t['name']}: the description says to call list_nodes first"
        assert "this board" in t["description"].lower() or t["name"] == "list_nodes", t["name"]
    assert load_shim().TOOLS == mcp.TOOLS


def test_the_setting_is_off_by_default_and_only_the_word_1_turns_it_on():
    from app.config import Settings
    assert Settings({}).mcp_nodes is False and Settings({"CCBOARD_MCP_NODES": ""}).mcp_nodes is False
    for v in ("0", "true", "yes", "on", "2", " 0 "):
        assert Settings({"CCBOARD_MCP_NODES": v}).mcp_nodes is False, v
    assert Settings({"CCBOARD_MCP_NODES": "1"}).mcp_nodes is True and Settings({"CCBOARD_MCP_NODES": " 1 "}).mcp_nodes is True


def test_settings_shows_the_setting_read_only_with_its_env_name(lite_client):
    r = lite_client.get("/api/mcp/tokens", headers={**ID, "X-CCBoard": "1"})
    assert r.json()["nodes"] == {"enabled": False, "env": "CCBOARD_MCP_NODES"}
    assert lite_client.put("/api/mcp/nodes", headers={**ID, "X-CCBoard": "1"}, json={"enabled": True}).status_code in (404, 405), "there is no switch: the environment only"


# ================================================================ 2. the gate

def test_off_by_default_every_agent_row_answers_the_sentence_and_nothing_is_sent(world):
    w = world
    for r in nr.RELAY:
        if not r.mcp:
            continue
        resp = ask(w.a, r, w.h, owner=False, headers={"X-CCBoard-Token": w.a.hook_token})
        assert resp.status_code == 403 and resp.json()["reason"] == "mcp_off" and resp.json()["error"] == OFF_TEXT, (r.name, resp.text)
    assert w.count.seen == 0
    assert audit(w.a, action="create_task")[0]["status"] == "refused" and audit(w.a, action="create_task")[0]["user"] == "local-token", "an agent's refused try is audited with who it was"


def test_with_the_setting_on_the_hook_token_reaches_the_six_rows_and_no_other_row(world, nodes_on):
    """The walk: every RELAY row, asked by the hook token (the shim). Six answer; every other row is a person's and nothing leaves this board for it."""
    w = world
    reached, shut = set(), set()
    for r in nr.RELAY:
        before = w.count.seen
        resp = ask(w.a, r, w.h, owner=False, headers={"X-CCBoard-Token": w.a.hook_token})
        if resp.status_code == 403 and resp.json().get("reason") == "human_only":
            shut.add(r.name)
            assert w.count.seen == before, f"{r.name}: nothing was sent"
        else:
            reached.add(r.name)
            assert resp.status_code in (200, 404, 409, 422), (r.name, resp.status_code, resp.text)
    assert reached == SIX and shut == {r.name for r in nr.RELAY} - SIX


def test_the_same_walk_in_process_as_a_device_token_that_holds_every_scope(world, nodes_on):
    from app import main
    w = world
    tok = {"id": "t1", "name": "laptop", "scopes": ["read", "tasks", "sessions", "permissions"]}
    standin = SimpleNamespace(state=SimpleNamespace(user="mcp:laptop", mcp_token=tok), headers={})
    reached = set()
    with w.a.enter():
        for r in nr.RELAY:
            before = w.count.seen
            params = {k: {"tid": "1", "name": "shop--api--s1", "pid": "1", "decision": "allow"}[k] for k in r.params}
            try:
                nr.relay(w.h, r, params, SAMPLE.get(r.name, {}) if r.hub_method != "GET" else {}, standin, db=w.a.db, hub=main._hub())
                reached.add(r.name)
            except nr.RelayError as e:
                if e.reason == "human_only":
                    assert w.count.seen == before, r.name
                else:
                    reached.add(r.name)
    assert reached == SIX


def test_other_hub_routes_stay_shut_to_the_hook_token(world, nodes_on):
    """Pairing, tokens, settings, the audit, the stream: nothing of the node management is an agent's, whatever CCBOARD_MCP_NODES says."""
    w = world
    hdr = {"X-CCBoard-Token": w.a.hook_token, "X-CCBoard": "1"}
    for method, path, body in (("POST", "/api/nodes/pair-code", {}), ("GET", "/api/nodes", None), ("GET", "/api/nodes/state", None), ("GET", "/api/nodes/audit", None),
                               ("GET", "/api/nodes/pairs", None), ("POST", f"/api/nodes/{w.reg['peer_id']}/rotate", None), ("DELETE", f"/api/nodes/{w.reg['peer_id']}", None),
                               ("POST", "/api/nodes", {"url": "https://x", "code": "y"}), ("POST", "/api/mcp/tokens", {"name": "x"}), ("PUT", "/api/mcp/remote", {"enabled": True}),
                               ("GET", f"/api/nodes/{w.h}/stream?names=a--b--c", None)):
        kw = {} if body is None else {"json": body}
        r = w.a.call(method, path, owner=False, headers=hdr, **kw)
        assert r.status_code in (403, 404, 405), (method, path, r.status_code)
    assert w.count.seen == 0


def test_a_device_tokens_own_scope_comes_first_then_the_pairs(world, nodes_on):
    from app import main
    w = world
    for scopes, row, want in ((["read"], "task_create", "tasks"), (["tasks"], "state", "read"), (["tasks"], "task", "read"), ([], "agents", "read")):
        tok = {"id": "t1", "name": "laptop", "scopes": scopes}
        standin = SimpleNamespace(state=SimpleNamespace(user="mcp:laptop", mcp_token=tok), headers={})
        with w.a.enter(), pytest.raises(nr.RelayError) as e:
            nr.relay(w.h, nr.BY_NAME[row], {"tid": "1"} if "tid" in nr.BY_NAME[row].params else {}, SAMPLE.get(row, {}) if nr.BY_NAME[row].hub_method != "GET" else {}, standin,
                     db=w.a.db, hub=main._hub())
        assert e.value.status == 403 and e.value.reason == "mcp_scope" and f"this device token does not hold the {want} scope" in e.value.message, (scopes, row)
    assert w.count.seen == 0
    reg = w.pair_up(["read"])                             # the pair has no tasks scope: the message names the pair
    tok = {"id": "t1", "name": "laptop", "scopes": ["read", "tasks"]}
    standin = SimpleNamespace(state=SimpleNamespace(user="mcp:laptop", mcp_token=tok), headers={})
    with w.a.enter(), pytest.raises(nr.RelayError) as e:
        nr.relay(reg["handle"], nr.BY_NAME["task_create"], {}, SAMPLE["task_create"], standin, db=w.a.db, hub=main._hub())
    assert e.value.status == 409 and e.value.message == f"needs the tasks scope on {reg['name']}" and e.value.reason == "scope"


def test_a_user_name_that_only_looks_like_a_device_token_is_refused(world, nodes_on):
    from app import main
    w = world
    for tok in (None, {}, {"id": "t1", "name": "someone-else", "scopes": ["read", "tasks"]}):
        standin = SimpleNamespace(state=SimpleNamespace(user="mcp:laptop", mcp_token=tok), headers={})
        with w.a.enter(), pytest.raises(nr.RelayError) as e:
            nr.relay(w.h, nr.BY_NAME["state"], {}, {}, standin, db=w.a.db, hub=main._hub())
        assert e.value.status == 403 and e.value.reason == "forbidden", tok
    assert w.count.seen == 0


def test_an_agent_never_hands_a_task_to_a_running_session_even_with_every_scope_on_both_sides(world, nodes_on):
    """The table says it, not the tool: crafted straight at the hub route (as the shim's hook token) and in process as a device token with `sessions`."""
    from app import main
    w = world
    card = w.a.post(f"/api/nodes/{w.h}/tasks", json={**SHOP, "when": "later"}).json()["data"]
    n = w.count.seen
    r = w.a.call("POST", f"/api/nodes/{w.h}/tasks/{card['id']}/dispatch", owner=False, headers={"X-CCBoard-Token": w.a.hook_token, "X-CCBoard": "1"},
                 json={"mode": "session", "session": "shop--api--s1"})
    assert r.status_code == 403 and r.json()["reason"] == "mcp_session" and "running session" in r.json()["error"], r.text
    tok = {"id": "t1", "name": "laptop", "scopes": ["read", "tasks", "sessions"]}
    standin = SimpleNamespace(state=SimpleNamespace(user="mcp:laptop", mcp_token=tok), headers={})
    with w.a.enter(), pytest.raises(nr.RelayError) as e:
        nr.relay(w.h, nr.BY_NAME["task_dispatch"], {"tid": str(card["id"])}, {"mode": "session", "session": "shop--api--s1"}, standin, db=w.a.db, hub=main._hub())
    assert e.value.status == 403 and e.value.reason == "mcp_session"
    assert w.count.seen == n, "nothing was sent to the peer"
    assert any(x["action"] == "dispatch_task" and x["status"] == "refused" and "mcp_session" in x["detail"] and x["user"] == "local-token" for x in audit(w.a))
    r = w.a.call("POST", f"/api/nodes/{w.h}/tasks/{card['id']}/dispatch", owner=False, headers={"X-CCBoard-Token": w.a.hook_token, "X-CCBoard": "1"}, json={"mode": "lane"})
    assert r.status_code == 200, "the lane start still works"
    assert w.a.post(f"/api/nodes/{w.h}/tasks/{card['id']}/dispatch", json={"mode": "session", "session": "shop--api--s1"}).status_code != 403, "a signed-in person still can"


# ================================================================ 3. the tools on two boards, through both transports

def test_create_task_on_a_node_makes_it_there_with_origin_a_ref_and_safe_options_and_the_audit_names_who(world, nodes_on, agent):
    w = world
    out = ok(agent.call("create_task", node=w.h, **SHOP, agent="claude"))
    tid = int(out["id"].split(":")[1])
    assert out["id"] == f"{w.h}:{tid}" and out["node"] == w.h and out["age"] == 0 and "ref" not in out and out["phase"] == "running" and out["task"]["id"] == out["id"]
    t = task_on_b(w, tid)
    assert json.loads(t["origin"]) == {"node": "node-a", "user": agent.by}, "the peer records who asked as a claim"
    assert json.loads(t["spec"])["permission_mode"] == "manual", "a remote launch carries the safe mode explicitly, never what a default would say"
    assert w.a.db.tasks() == [] and w.two.tmux_a["created"] == [], "nothing was made on the board the agent talks to"
    assert [c[0] for c in w.two.tmux_b["created"]] == [out["tmux"]]
    mine = audit(w.a, action="create_task")[0]
    theirs = audit(w.b, action="create_task")[0]
    assert (mine["direction"], mine["user"], mine["node_name"], mine["status"]) == ("out", agent.by, "node-b", "ok")
    assert (theirs["direction"], theirs["user"], theirs["status"]) == ("in", f"for {agent.by}", "ok")
    assert SHOP["prompt"] not in json.dumps([audit(w.a), audit(w.b)]), "never the prompt"


def test_create_task_later_is_a_backlog_card_there_and_dispatch_lane_starts_it_in_a_new_session(world, nodes_on, agent):
    w = world
    card = ok(agent.call("create_task", node=w.h, **SHOP, dispatch=False))
    assert card["phase"] == "backlog" and w.two.tmux_b["created"] == []
    tid = int(card["id"].split(":")[1])
    out = ok(agent.call("dispatch_task", node=w.h, task_id=tid))
    assert out["id"] == card["id"] and out["phase"] == "running" and out["tmux"] and [c[0] for c in w.two.tmux_b["created"]] == [out["tmux"]]
    assert json.loads(task_on_b(w, tid)["origin"])["user"] == agent.by
    again = agent.call("dispatch_task", node=w.h, task_id=tid)
    err(again, "already dispatched")


def test_dispatch_into_a_session_or_with_session_flags_is_a_tool_error_and_nothing_is_sent(world, nodes_on, agent):
    w = world
    card = ok(agent.call("create_task", node=w.h, **SHOP, dispatch=False))
    tid, n = int(card["id"].split(":")[1]), w.count.seen
    err(agent.call("dispatch_task", node=w.h, task_id=tid, session="shop--api--s1"), "running session on another node")
    err(agent.call("dispatch_task", node=w.h, task_id=tid, force=True), "force and queue")
    err(agent.call("dispatch_task", node=w.h, task_id=tid, queue=True), "force and queue")
    assert w.count.seen == n and task_on_b(w, tid)["phase"] == "backlog"


def test_after_task_id_with_a_node_is_refused_a_chain_stays_inside_one_node(world, nodes_on, agent):
    w = world
    n = w.count.seen
    err(agent.call("create_task", node=w.h, **SHOP, after_task_id=3), "chain")
    assert w.count.seen == n and w.b.db.tasks() == []


def test_get_task_status_and_result_read_through_with_a_ref_and_say_where_the_result_is(world, nodes_on, agent):
    w = world
    made = ok(agent.call("create_task", node=w.h, **SHOP))
    tid = int(made["id"].split(":")[1])
    with w.b.enter():
        w.b.db.task_update(tid, phase="done", result="THE-SECRET-RESULT-TEXT")
    st = ok(agent.call("get_task_status", node=w.h, task_id=tid))
    assert st["id"] == made["id"] and st["phase"] == "done" and st["node"] == w.h and st["age"] == 0 and st["project"] == "shop" and st["repo"] == "api"
    assert st["column"] is None and st["worktree"] is None and st["pr_url"] is None and st["ci"] is None and st["cost_usd"] is None, "what a node does not share is null, never a guess"
    res = ok(agent.call("get_task_result", node=w.h, task_id=tid))
    assert res["id"] == made["id"] and res["phase"] == "done" and res["has_result"] is True and res["result"] is None
    assert "read it on node-b itself" in res["note"] and "THE-SECRET-RESULT-TEXT" not in json.dumps(res), "the relay row carries no result text, and this phase does not add it"
    err(agent.call("get_task_status", node=w.h, task_id=424242), "404")
    assert audit(w.a, action="read_task")[0]["user"] == agent.by and audit(w.b, action="read_task")[0]["user"] == f"for {agent.by}"


def test_list_projects_and_list_tasks_read_the_nodes_state_with_refs_and_the_age(world, nodes_on, agent):
    w = world
    made = ok(agent.call("create_task", node=w.h, **SHOP))
    projects = ok(agent.call("list_projects", node=w.h))
    assert projects["node"] == w.h and projects["age"] == 0
    shop = next(p for p in projects["projects"] if p["name"] == "shop")
    api = next(r for r in shop["repos"] if r["name"] == "api")
    assert [s["name"] for s in api["sessions"]] == [made["tmux"]] and api["sessions"][0]["agent"] == "claude"
    tasks = ok(agent.call("list_tasks", node=w.h))
    assert [t["id"] for t in tasks["tasks"]] == [made["id"]] and tasks["tasks"][0]["column"] is None and tasks["tasks"][0]["pr_url"] is None and tasks["tasks"][0]["session_state"]
    assert ok(agent.call("list_tasks", node=w.h, project="other"))["tasks"] == []
    assert audit(w.a, action="read_state")[0]["user"] == agent.by


def test_local_calls_and_node_local_are_the_same_calls_as_before(world, nodes_on):
    """`node` absent, null or `local` runs on this board exactly as the six tools always did: the same board calls, the same bodies, no relay."""
    from app import mcp as m
    calls = []

    def api(method, path, body=None):
        calls.append((method, path, body))
        return {"projects": [], "tasks": [], "id": 1, "title": "T", "slug": "s"}
    for name, args in (("list_projects", {}), ("list_tasks", {"project": "shop"}), ("get_task_status", {"task_id": 1}), ("get_task_result", {"task_id": 1}),
                       ("create_task", {"project": "shop", "repo": "api", "title": "X", "prompt": "do"}), ("dispatch_task", {"task_id": 1}),
                       ("dispatch_task", {"task_id": 1, "session": "shop--api--s1", "force": True})):
        calls.clear()
        try:
            m.tool_call(name, dict(args), api)
        except RuntimeError:
            pass
        plain = list(calls)
        for spelled in ({"node": "local"}, {"node": None}):
            calls.clear()
            try:
                m.tool_call(name, {**args, **spelled}, api)
            except RuntimeError:
                pass
            assert calls == plain, (name, spelled)
        assert plain and not any("/api/nodes" in c[1] for c in plain), name


def test_a_bad_node_value_is_a_tool_error_before_any_call(world, nodes_on, agent):
    w = world
    for bad in ("", "Box", "../x", "a/b", "x" * 40, "node-b?x=1", " node-b", "node b"):
        is_err, text = agent.call("list_projects", node=bad)
        assert is_err and ("node must be local or the handle of a paired node" in text or "node is too long" in text), (bad, text)
    assert w.count.seen == 0


def test_an_unknown_node_is_a_tool_error_that_lists_the_paired_handles(world, nodes_on, agent):
    w = world
    text = err(agent.call("list_tasks", node="nowhere"), "no paired node has the handle nowhere", f"paired nodes: {w.h}")
    assert "100.64" not in text and "http" not in text, "handles only, never an address"
    assert w.count.seen == 0
    with w.a.enter():
        nodes.remove_peer(w.reg["peer_id"], w.a.db)
    err(agent.call("list_tasks", node=w.h), "this board has no paired node")


def test_with_the_setting_off_a_node_other_than_local_is_a_tool_error_and_local_still_works(world, agent, monkeypatch):
    from app import main
    w = world
    monkeypatch.setattr(main, "_hub", lambda: LiveHub())
    for name, args in (("list_projects", {}), ("list_tasks", {}), ("get_task_status", {"task_id": 1}), ("get_task_result", {"task_id": 1}), ("create_task", SHOP),
                       ("dispatch_task", {"task_id": 1})):
        err(agent.call(name, node=w.h, **args), OFF_TEXT)
        err(agent.call(name, node="not-a-node", **args), OFF_TEXT)               # the setting comes before the registry: an unknown handle learns nothing while it is off
    assert w.count.seen == 0 and w.b.db.tasks() == []
    assert agent.call("list_tasks")[0] is False and agent.call("list_nodes")[0] is False, "local calls and the list work with the setting off"


def test_a_device_token_without_the_scope_is_refused_by_name_and_nothing_is_sent(world, nodes_on):
    w = world
    reader = Remote(w, "reader", scopes=["read"])
    is_err, text = reader.call("create_task", node=w.h, **SHOP)
    assert is_err and "tasks scope" in text and "device token" in text
    assert reader.call("list_tasks", node=w.h)[0] is False
    writer = Remote(w, "writer", scopes=["tasks"])
    is_err, text = writer.call("list_tasks", node=w.h)
    assert is_err and "read scope" in text
    assert w.b.db.tasks() == [], "the reader created nothing"


def test_a_pair_without_the_scope_is_a_tool_error_that_names_the_node(world, nodes_on, agent):
    w = world
    w.pair_up(["read"])
    nodes.peer_transport = w.count = Count(w.two.transport)
    err(agent.call("create_task", node=w.h, **SHOP), "needs the tasks scope on node-b")
    assert w.count.seen == 0
    assert agent.call("list_tasks", node=w.h)[0] is False, "the read scope is enough for a read"


def test_the_guard_still_applies_to_every_text_an_agent_sends(world, nodes_on, agent, clock):
    w = world
    n = w.count.seen
    for bad, needle in (({"prompt": "/model opus now"}, "start with"), ({"prompt": "do it --dangerously-skip-permissions"}, "not allowed anywhere"),
                        ({"prompt": "bell \x07 inside"}, "control"), ({"title": "t‮title"}, "title"),
                        ({"project": "../x"}, "project"), ({"repo": "a b"}, "repo"), ({"agent": "shell"}, "agent")):
        clock["t"] += 61                                                   # /mcp counts a refused write against its ten a minute
        is_err, text = agent.call("create_task", node=w.h, **{**SHOP, **bad})
        assert is_err and needle in text, (bad, text)
    assert w.count.seen == n and w.b.db.tasks() == []
    for extra in ({"permission_mode": "bypassPermissions"}, {"args": "--x"}, {"sandbox": "danger-full-access"}, {"keys": "y"}):
        clock["t"] += 61
        is_err, res = agent.call("create_task", node=w.h, **SHOP, dispatch=False, **extra)
        if agent.kind == "mcp":                                           # /mcp checks every argument against the schema
            assert is_err and "unknown argument" in res, (extra, res)
        else:                                                             # the shim forwards only the arguments it knows: the extra one goes nowhere
            assert not is_err, res
            spec = json.loads(task_on_b(w, int(res["id"].split(":")[1]))["spec"])
            assert spec["permission_mode"] == "manual" and "bypass" not in json.dumps(spec) and "args" not in spec and "sandbox" not in spec, (extra, spec)
    assert agent.kind == "shim" or w.count.seen == n


def test_a_missing_repo_on_the_node_says_so_and_nothing_is_created(world, nodes_on, agent):
    w = world
    text = err(agent.call("create_task", node=w.h, **{**SHOP, "repo": "ghost"}), "ghost is not on node-b")
    assert "Nothing" in text and w.b.db.tasks() == []


@pytest.mark.parametrize("exc", [TimeoutError("slow"), ConnectionResetError("reset")])
def test_a_write_that_may_have_gone_out_is_unconfirmed_and_never_retried(world, nodes_on, agent, exc):
    w = world
    t = Raises(exc)
    nodes.peer_transport = t
    err(agent.call("create_task", node=w.h, **SHOP), "could not be confirmed", "not retried")
    assert t.seen == 1


def test_an_offline_node_is_refused_with_its_age_and_nothing_is_queued(world, nodes_on, agent, monkeypatch):
    from app import main
    w = world
    monkeypatch.setattr(main, "_hub", lambda: FakeHub(status="offline", age_s=240))
    n = w.count.seen
    err(agent.call("create_task", node=w.h, **SHOP), "node-b is offline", "4 min", "nothing was sent")
    err(agent.call("list_projects", node=w.h), "offline")
    monkeypatch.setattr(main, "_hub", lambda: FakeHub(polled_at=None, last_ok_at=None, status="offline"))
    err(agent.call("list_tasks", node=w.h), "has not been read yet")
    assert w.count.seen == n


def test_a_secret_shaped_string_in_a_peers_answer_never_reaches_the_agent(world, nodes_on, agent):
    w = world
    secret = "gh" + "p_" + "A1b2C3d4E5" * 4
    nodes.peer_transport = Answer({"id": 7, "title": "T " + secret, "phase": "done", "agent": "claude", "project": "shop", "repo": "api", "branch": "b", "tmux": None,
                                   "has_result": True, "prompt": "MARKER-never", "worktree": "/home/x/y"})
    out = ok(agent.call("get_task_status", node=w.h, task_id=7))
    dump = json.dumps(out)
    assert secret not in dump and "[redacted]" in out["title"] and "MARKER-never" not in dump and "/home/x" not in dump


def test_the_relay_write_rate_is_per_token_name_and_the_shim_has_its_own(world, nodes_on, monkeypatch):
    w = world
    monkeypatch.setattr(nodes, "relay_write_limiter", nodes._Limiter(2, 60.0, 64))
    a, b = Remote(w, "one"), Remote(w, "two")
    shim = Shim(w)
    for _ in range(2):
        ok(a.call("create_task", node=w.h, **SHOP, dispatch=False))
    text = err(a.call("create_task", node=w.h, **SHOP, dispatch=False), "429")
    assert "too many relayed requests" in text
    ok(b.call("create_task", node=w.h, **SHOP, dispatch=False))                     # another token has its own bucket
    ok(shim.call("create_task", node=w.h, **SHOP, dispatch=False))                  # so has the shim (`local-token`)
    assert w.a.post(f"/api/nodes/{w.h}/tasks", json={**SHOP, "when": "later"}).status_code == 200, "and so has a signed-in person"


def test_the_mcp_audit_keeps_the_node_and_no_text(world, nodes_on, monkeypatch):
    from app import main, mcp_tokens
    w = world
    monkeypatch.setattr(main, "_hub", lambda: LiveHub())
    r = Remote(w, "audited")
    ok(r.call("create_task", node=w.h, **SHOP, dispatch=False))
    ok(r.call("list_tasks"))
    with w.a.enter():
        rows = mcp_tokens.recent(10)
    mine = [x for x in rows if x["name"] == "audited"]
    assert {x["tool"]: x.get("node") for x in mine} == {"create_task": w.h, "list_tasks": None}
    assert SHOP["prompt"] not in json.dumps(rows) and all("node" not in x for x in mine if x["tool"] == "list_tasks")


# ================================================================ 4. list_nodes

def record(handle="node-b", **more):
    base = {"peer_id": "ts:nSECRETPEERID01", "handle": handle, "node_id": "ts:nSECRETNODEID01", "name": "Box", "url": "https://100.64.0.2", "status": "online", "age_s": 7,
            "polled_at": "x", "last_ok_at": "x", "scopes": ["read", "tasks"], "etag": "W/\"abc\"", "legacy": False,
            "card": {"agents": [{"id": "claude", "installed": True, "logged_in": True, "hooks": True, "login_problem": False},
                                {"id": "codex", "installed": True, "logged_in": False, "hooks": False, "login_problem": True}],
                     "accounts": {"items": [{"agent": "claude", "label": "PRIVATE-LABEL", "window": {"pct": 1}}]}, "url": "https://100.64.0.2", "node_id": "ts:nSECRETNODEID01"},
            "state": {"lanes": {"cap": 3, "running": 1, "free": 2}, "usage": {"claude": {"pct": 41.5, "resets_at": 1760000000, "known": True, "limited": False, "backoff_until": None},
                                                                              "codex": {"pct": None, "resets_at": None, "known": False, "limited": False}}}}
    return {**base, **more}


class RecHub:
    def __init__(self, *recs):
        self.recs = list(recs)

    def records(self, handle=None):
        return self.recs

    def rows(self):
        return []


class LiveHub(FakeHub):
    """FakeHub plus the registry read the board's own /api/state makes (a local call)."""

    def rows(self):
        return []


@pytest.mark.parametrize("on", [False, True])
def test_list_nodes_works_with_the_setting_off_and_shows_no_address_token_or_id(world, monkeypatch, agent, on):
    from app import main
    w = world
    monkeypatch.setattr(settings, "mcp_nodes", on)
    monkeypatch.setattr(main, "_hub", lambda: RecHub(record()))
    out = ok(agent.call("list_nodes"))
    assert out["enabled"] is on and out["env"] == "CCBOARD_MCP_NODES"
    (n,) = out["nodes"]
    assert set(n) == {"handle", "name", "status", "age_s", "agents", "lanes", "windows", "scopes"}
    assert n["handle"] == "node-b" and n["name"] == "Box" and n["status"] == "online" and n["age_s"] == 7 and n["scopes"] == ["read", "tasks"]
    assert n["agents"] == [{"name": "claude", "installed": True, "logged_in": True, "login_problem": False}, {"name": "codex", "installed": True, "logged_in": False, "login_problem": True}]
    assert n["lanes"] == {"cap": 3, "running": 1, "free": 2}
    assert n["windows"]["claude"] == {"pct": 41.5, "resets_at": 1760000000, "known": True, "limited": False} and n["windows"]["codex"]["pct"] is None
    dump = json.dumps(out)
    for secret in ("SECRETPEERID", "SECRETNODEID", "100.64", "http", "PRIVATE-LABEL", "token", "etag"):
        assert secret not in dump, secret
    assert w.count.seen == 0, "the list is the hub's own reading: nothing is asked of a node"


def test_list_nodes_says_null_for_what_the_hub_has_not_read_and_zero_nodes_for_a_single_board(world, monkeypatch, agent):
    from app import main
    monkeypatch.setattr(main, "_hub", lambda: RecHub(record(status="offline", age_s=None, card=None, state=None, scopes=[])))
    (n,) = ok(agent.call("list_nodes"))["nodes"]
    assert n["age_s"] is None and n["agents"] is None and n["lanes"] is None and n["windows"] is None and n["status"] == "offline" and n["scopes"] == []
    monkeypatch.setattr(main, "_hub", lambda: RecHub())
    assert ok(agent.call("list_nodes"))["nodes"] == []
    monkeypatch.setattr(main, "_hub", lambda: RecHub(record(state={"lanes": {"cap": 0, "running": None, "free": None}, "usage": {}})))
    (n,) = ok(agent.call("list_nodes"))["nodes"]
    assert n["lanes"] == {"cap": 0, "running": None, "free": None} and n["windows"] == {"claude": None, "codex": None}


def test_the_node_list_route_is_for_a_person_and_the_hook_token_not_a_node_token(world, monkeypatch):
    from app import main
    w = world
    monkeypatch.setattr(main, "_hub", lambda: RecHub(record()))
    assert w.a.get("/api/mcp/nodes").json()["nodes"][0]["handle"] == "node-b"
    r = w.a.call("GET", "/api/mcp/nodes", owner=False, headers={"X-CCBoard-Token": w.a.hook_token})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert w.a.call("GET", "/api/mcp/nodes", owner=False).status_code == 403
    with w.a.enter():
        token = nodes._load_outgoing(w.reg["peer_id"])
    assert w.b.call("GET", "/api/mcp/nodes", owner=False, headers={"Authorization": f"Bearer {token}", "X-CCBoard": "1"}).status_code == 403, "a node token opens the closed list of node routes only"


def test_a_board_with_no_paired_node_answers_an_empty_list_and_starts_nothing(lite_client):
    r = lite_client.get("/api/mcp/nodes", headers={**ID, "X-CCBoard": "1"})
    assert r.status_code == 200 and r.json() == {"enabled": False, "env": "CCBOARD_MCP_NODES", "nodes": []}


# ================================================================ the two servers stay in step

def test_the_shim_and_the_remote_tool_call_make_the_same_calls_for_a_node():
    shim = load_shim()
    state = {"data": {"projects": [{"name": "shop", "repos": [{"name": "api", "branch": "main", "dirty": False}]}],
                      "sessions": [{"tmux": "shop--api--t", "project": "shop", "repo": "api", "state": "idle", "agent": "claude", "kind": "task"}],
                      "tasks": [{"id": 3, "project": "shop", "repo": "api", "title": "T", "phase": "running", "tmux": "shop--api--t"}], "truncated": False}, "age": 0, "node": "box"}
    started = {"data": {"ref": "box:5", "id": 5, "tmux": "shop--api--t", "phase": "running", "task": {"id": 5, "title": "T"}}, "age": 0, "node": "box"}
    task = {"data": {"id": 3, "title": "T", "phase": "done", "has_result": True}, "age": 0, "node": "box"}
    cases = [("list_projects", {"node": "box"}), ("list_tasks", {"node": "box", "project": "shop"}), ("get_task_status", {"node": "box", "task_id": 3}),
             ("get_task_result", {"node": "box", "task_id": 3}), ("get_task_result", {"node": "box", "task_id": 0}), ("get_task_result", {"node": "box", "task_id": True}),
             ("create_task", {"node": "box", **SHOP}), ("create_task", {"node": "box", **SHOP, "dispatch": False, "agent": "codex", "auto_close": True}),
             ("create_task", {"node": "box", **SHOP, "after_task_id": 1}), ("create_task", {"node": "box", "project": "shop"}), ("dispatch_task", {"node": "box", "task_id": 3}),
             ("dispatch_task", {"node": "box", "task_id": 3, "agent": "codex", "auto_close": False}), ("dispatch_task", {"node": "box", "task_id": 3, "session": "shop--api--s"}),
             ("dispatch_task", {"node": "box", "task_id": 3, "force": True}), ("list_nodes", {}), ("list_tasks", {"node": "BAD"})]

    def run(fn):
        calls = []

        def api(method, path, body=None):
            calls.append((method, path, body))
            return state if path.endswith("/state") else task if "/tasks/" in path and not path.endswith("/dispatch") else started if path.startswith("/api/nodes/") else {"nodes": []}
        try:
            out = fn(api)
        except RuntimeError as e:
            out = ("error", str(e))
        return out, calls
    for name, args in cases:
        mine = run(lambda api: mcp.tool_call(name, dict(args), api))

        def theirs(api):
            shim.call_api = api
            return shim.tool_call(name, dict(args))
        other = run(theirs)
        if isinstance(mine[0], tuple) or isinstance(other[0], tuple):       # both refuse (the remote's words may be stricter), or neither does
            assert isinstance(mine[0], tuple) and isinstance(other[0], tuple) and mine[1] == other[1], (name, args, mine, other)
        else:
            assert mine == other, (name, args)
    assert run(lambda api: mcp.tool_call("list_nodes", {}, api))[1] == [("GET", "/api/mcp/nodes", None)]
    assert run(lambda api: mcp.tool_call("create_task", {"node": "box", **SHOP}, api))[1] == [
        ("POST", "/api/nodes/box/tasks", {"project": "shop", "repo": "api", "title": SHOP["title"], "prompt": SHOP["prompt"], "when": "now"})]
    assert run(lambda api: mcp.tool_call("dispatch_task", {"node": "box", "task_id": 3}, api))[1] == [("POST", "/api/nodes/box/tasks/3/dispatch", {"mode": "lane"})]


def test_neither_server_names_a_peer_route_a_permission_a_key_or_an_epic():
    for f in ("app/mcp.py", "scripts/ccboard_mcp.py", "app/mcp_remote.py"):
        text = (ROOT / f).read_text()
        assert not re.search(r"/api/node(?!s/)", text), f
        for word in ("/keys", "/ack", "/prompt", "/pane", "/stream", "/permissions", "/sessions", "epic_status", "epic_run"):
            assert word not in text.replace("tasks/{tid}/dispatch", ""), (f, word)
