"""The remote MCP endpoint /mcp and its device tokens (issue #13): app/mcp.py, app/mcp_tokens.py, app/mcp_remote.py and the routes in
app/main.py. The board without its workers (lite_client), temp data and projects dirs, a fake tmux; no real claude or codex."""
import importlib.util
import json
import logging
import pathlib
import subprocess
from datetime import timedelta

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
ID = {"Tailscale-User-Login": "alice@example.com"}
H = {**ID, "X-CCBoard": "1"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}}


def load_shim():
    spec = importlib.util.spec_from_file_location("ccboard_mcp_for_remote", str(ROOT / "scripts" / "ccboard_mcp.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(autouse=True)
def _fresh_limits(monkeypatch):
    from app import mcp_remote
    mcp_remote.limiter.reset()
    clock = {"t": 1000.0}
    monkeypatch.setattr(mcp_remote, "_clock", lambda: clock["t"])
    yield clock
    mcp_remote.limiter.reset()


def on(c):
    r = c.put("/api/mcp/remote", headers=H, json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True


def mint(c, name="laptop", **body):
    r = c.post("/api/mcp/tokens", headers=H, json={"name": name, **body})
    assert r.status_code == 201, r.text
    return r.json()["token"], r.json()["record"]


def auth(tok, **more):
    return {**ID, "Authorization": f"Bearer {tok}", **more}


def rpc(c, tok, msg, **headers):
    return c.post("/mcp", headers=auth(tok, **headers), json=msg)


def call(c, tok, name, **args):
    r = rpc(c, tok, {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": name, "arguments": args}})
    assert r.status_code == 200, r.text
    return r.json()


def git_repo(projects_dir, project="shop", repo="api"):
    p = projects_dir / project / repo
    p.mkdir(parents=True)
    subprocess.run(["git", "-C", str(p), "init", "-q", "-b", "main"], check=True)
    return p


# ---------------------------------------------------------------- who gets in

def test_off_by_default_is_404_with_or_without_a_token(lite_client):
    tok, _ = mint(lite_client)                                        # minting works while off; the endpoint does not
    assert lite_client.get("/api/mcp/tokens", headers=H).json()["enabled"] is False
    for headers in ({}, ID, auth(tok), auth("ccbmcp_x")):
        r = lite_client.post("/mcp", headers=headers, json=INIT)
        assert r.status_code == 404, headers
    assert lite_client.get("/mcp", headers=auth(tok)).status_code == 404


def test_env_turns_it_on_until_settings_says_otherwise(lite_client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "mcp_remote", "1")
    tok, _ = mint(lite_client)
    assert lite_client.get("/api/mcp/tokens", headers=H).json()["enabled"] is True
    assert rpc(lite_client, tok, INIT).status_code == 200
    assert lite_client.put("/api/mcp/remote", headers=H, json={"enabled": False}).json()["enabled"] is False
    assert rpc(lite_client, tok, INIT).status_code == 404, "the stored choice wins over the env default"


def test_identity_then_token(lite_client):
    on(lite_client)
    tok, _ = mint(lite_client)
    assert lite_client.post("/mcp", headers={"Authorization": f"Bearer {tok}"}, json=INIT).status_code == 403, "a token without identity (a tagged device)"
    r = lite_client.post("/mcp", headers=ID, json=INIT)
    assert r.status_code == 401 and "Settings > Agents" in r.json()["error"]
    assert r.headers["www-authenticate"].startswith("Bearer ")
    for bad in ("ccbmcp_" + "A" * 43, tok[:-1] + ("A" if tok[-1] != "A" else "B"), "", "Basic abc"):
        r = lite_client.post("/mcp", headers={**ID, "Authorization": bad if bad.startswith("Basic") else f"Bearer {bad}"}, json=INIT)
        assert r.status_code == 401 and "Settings > Agents" in r.json()["error"], bad
    r = rpc(lite_client, tok, INIT)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.json()["result"]["protocolVersion"] == "2025-06-18" and r.json()["result"]["capabilities"] == {"tools": {}}


def test_revoked_and_expired_tokens_fail_on_the_next_request(lite_client, monkeypatch):
    from app import mcp_tokens
    on(lite_client)
    tok, rec = mint(lite_client)
    assert rpc(lite_client, tok, INIT).status_code == 200
    assert lite_client.delete(f"/api/mcp/tokens/{rec['id']}", headers=H).status_code == 200
    r = rpc(lite_client, tok, INIT)
    assert r.status_code == 401 and "Settings > Agents" in r.json()["error"]
    assert lite_client.delete(f"/api/mcp/tokens/{rec['id']}", headers=H).status_code == 404
    tok2, rec2 = mint(lite_client, "tablet", expires_days=30)
    assert rpc(lite_client, tok2, INIT).status_code == 200
    real = mcp_tokens._now
    monkeypatch.setattr(mcp_tokens, "_now", lambda: real() + timedelta(days=31))
    r = rpc(lite_client, tok2, INIT)
    assert r.status_code == 401 and "expired" in r.json()["error"] and "Settings > Agents" in r.json()["error"]
    listed = lite_client.get("/api/mcp/tokens", headers=H).json()["tokens"]
    assert [t["expired"] for t in listed if t["id"] == rec2["id"]] == [True], "an expired token stays listed so Settings can say so"


def test_the_hook_token_opens_nothing_at_mcp(lite_client):
    from app import hooks
    on(lite_client)
    hook = hooks.ensure_token()
    tok, _ = mint(lite_client)
    assert lite_client.post("/mcp", headers={"X-CCBoard-Token": hook}, json=INIT).status_code == 403
    assert lite_client.post("/mcp", headers={**ID, "X-CCBoard-Token": hook}, json=INIT).status_code == 403
    assert lite_client.post("/mcp", headers=auth(tok, **{"X-CCBoard-Token": hook}), json=INIT).status_code == 403, "not even next to a device token"
    assert lite_client.post("/mcp", headers=auth(hook), json=INIT).status_code == 401, "the hook token is not a device token"


def test_a_device_token_opens_nothing_under_api(lite_client):
    on(lite_client)
    tok, rec = mint(lite_client)
    bearer = {"Authorization": f"Bearer {tok}"}
    for headers in (bearer, {**bearer, **H}):
        assert lite_client.get("/api/state", headers=headers).status_code == 403, headers
        assert lite_client.get("/api/mcp/tokens", headers=headers).status_code == 403
        assert lite_client.post("/api/mcp/tokens", headers=headers, json={"name": "x"}).status_code == 403
        assert lite_client.delete(f"/api/mcp/tokens/{rec['id']}", headers=headers).status_code == 403
        assert lite_client.put("/api/mcp/remote", headers=headers, json={"enabled": False}).status_code == 403
        assert lite_client.post("/api/tasks", headers=headers, json={}).status_code == 403
    assert lite_client.get("/api/mcp/tokens", headers=H).json()["enabled"] is True, "nothing changed"


def test_management_routes_need_identity_and_the_csrf_header_and_refuse_the_hook_token(lite_client):
    from app import hooks
    assert lite_client.get("/api/mcp/tokens").status_code == 403
    assert lite_client.post("/api/mcp/tokens", headers=ID, json={"name": "x"}).status_code == 403, "no X-CCBoard"
    assert lite_client.put("/api/mcp/remote", headers=ID, json={"enabled": True}).status_code == 403
    assert lite_client.delete("/api/mcp/tokens/abc", headers=ID).status_code == 403
    hook = {"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard": "1"}
    assert lite_client.post("/api/mcp/tokens", headers=hook, json={"name": "x"}).status_code == 403
    assert lite_client.put("/api/mcp/remote", headers=hook, json={"enabled": True}).status_code == 403
    assert lite_client.get("/api/mcp/tokens", headers=H).json() == {**lite_client.get("/api/mcp/tokens", headers=H).json(), "enabled": False, "tokens": []}


def test_transport_rules(lite_client):
    on(lite_client)
    tok, _ = mint(lite_client)
    assert rpc(lite_client, tok, INIT, Origin="https://evil.example").status_code == 403
    assert rpc(lite_client, tok, INIT, Origin="null").status_code == 403
    for method in ("GET", "DELETE", "PUT"):
        r = lite_client.request(method, "/mcp", headers=auth(tok))
        assert r.status_code == 405 and r.headers.get("allow") == "POST", method
    assert rpc(lite_client, tok, INIT, **{"MCP-Protocol-Version": "2099-01-01"}).status_code == 400
    for v in ("2025-06-18", "2025-03-26"):
        assert rpc(lite_client, tok, {"jsonrpc": "2.0", "id": 2, "method": "ping"}, **{"MCP-Protocol-Version": v}).json()["result"] == {}
    big = {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {"pad": "x" * (256 * 1024)}}
    assert rpc(lite_client, tok, big).status_code == 413
    r = lite_client.post("/mcp", headers=auth(tok), json=[INIT, {"jsonrpc": "2.0", "id": 2, "method": "ping"}])
    assert r.status_code == 400 and r.json()["error"]["code"] == -32600, "no batches"
    r = lite_client.post("/mcp", headers={**auth(tok), "Content-Type": "application/json"}, content=b"{not json")
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700
    for note in ({"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": 5, "result": {}}):
        r = rpc(lite_client, tok, note)
        assert r.status_code == 202 and r.content == b"", note
    r = rpc(lite_client, tok, {"jsonrpc": "2.0", "id": 6, "method": "resources/list"})
    assert r.json()["error"]["code"] == -32601
    r = rpc(lite_client, tok, {"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}})
    assert r.json()["result"]["protocolVersion"] == "2025-06-18", "an unknown version is answered with the newest this server speaks"


# ---------------------------------------------------------------- the tools

def test_round_trip_creates_a_backlog_task_as_the_device(lite_client, projects_dir, fake_tmux):
    from app import main, mcp
    git_repo(projects_dir)
    on(lite_client)
    tok, rec = mint(lite_client)
    assert rpc(lite_client, tok, INIT).status_code == 200
    tools = rpc(lite_client, tok, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
    assert tools == mcp.TOOLS == load_shim().TOOLS
    out = call(lite_client, tok, "create_task", project="shop", repo="api", title="From the laptop", prompt="do it", dispatch=False)
    assert out["result"]["isError"] is False, out
    task = json.loads(out["result"]["content"][0]["text"])
    assert task["phase"] == "backlog" and isinstance(task["id"], int)
    assert main.db.task_get(task["id"])["phase"] == "backlog"
    listed = json.loads(call(lite_client, tok, "list_tasks")["result"]["content"][0]["text"])["tasks"]
    assert [t["id"] for t in listed] == [task["id"]]
    res = json.loads(call(lite_client, tok, "get_task_result", task_id=task["id"])["result"]["content"][0]["text"])
    assert res["phase"] == "backlog" and res["result"] is None
    projects = json.loads(call(lite_client, tok, "list_projects")["result"]["content"][0]["text"])["projects"]
    assert [p["name"] for p in projects] == ["shop"]
    missing = call(lite_client, tok, "get_task_result", task_id=999)["result"]
    assert missing["isError"] and "404" in missing["content"][0]["text"]
    st = lite_client.get("/api/mcp/tokens", headers=H).json()
    row = next(t for t in st["tokens"] if t["id"] == rec["id"])
    assert row["last_used_at"] and row["last_user"] == "alice@example.com" and row["last_tool"] == "get_task_result"
    assert [(a["tool"], a["outcome"]) for a in st["recent"][:2]] == [("get_task_result", "error"), ("list_projects", "ok")]
    assert any(a["tool"] == "create_task" and a["task_id"] == task["id"] and a["outcome"] == "ok" for a in st["recent"])


def test_tools_run_in_process_as_the_device_never_over_http(lite_client, projects_dir, fake_tmux, monkeypatch):
    from app import main
    import urllib.request
    seen = []
    real = main.build_state
    monkeypatch.setattr(main, "build_state", lambda user: seen.append(user) or real(user))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("the endpoint called the board over HTTP"))
    on(lite_client)
    tok, _ = mint(lite_client, "my laptop")
    assert call(lite_client, tok, "list_tasks")["result"]["isError"] is False
    assert seen == ["mcp:my laptop"]


def test_unknown_arguments_are_refused_and_nothing_is_created(lite_client, projects_dir, fake_tmux, _fresh_limits):
    from app import main
    git_repo(projects_dir)
    on(lite_client)
    tok, _ = mint(lite_client, scopes=["read", "tasks", "sessions"])
    base = {"project": "shop", "repo": "api", "title": "T", "prompt": "p", "dispatch": False}
    for extra in ({"permission_mode": "bypassPermissions"}, {"bypass": True}, {"sandbox": "danger-full-access"}, {"model": "opus"},
                  {"args": "--dangerously-skip-permissions"}, {"opts": {"sandbox": "danger-full-access"}}):
        _fresh_limits["t"] += 61                                      # a refused write still counts against the ten a minute
        r = call(lite_client, tok, "create_task", **base, **extra)["result"]
        assert r["isError"] and "unknown argument" in r["content"][0]["text"], extra
        r = call(lite_client, tok, "dispatch_task", task_id=1, **extra)["result"]
        assert r["isError"] and "unknown argument" in r["content"][0]["text"], extra
    assert main.db.tasks() == []
    for bad in ({"title": "x" * 121}, {"prompt": "x" * 20001}):
        r = call(lite_client, tok, "create_task", **{**base, **bad})["result"]
        assert r["isError"] and "too long" in r["content"][0]["text"], bad
    assert main.db.tasks() == []


def test_no_schema_names_a_permission_sandbox_bypass_or_model_argument():
    from app import mcp
    words = ("permission", "sandbox", "bypass", "model", "approval", "dangerous", "args", "opts")
    for t in mcp.TOOLS:
        assert t["inputSchema"]["additionalProperties"] is False
        for prop in t["inputSchema"]["properties"]:
            assert not any(w in prop.lower() for w in words), (t["name"], prop)


def test_the_tools_are_the_shims_and_make_the_same_calls():
    """The shim keeps its own copy (a docker box runs it from a folder without app/): the schemas and the calls must not drift."""
    from app import mcp
    shim = load_shim()
    assert shim.TOOLS == mcp.TOOLS and shim.PROTOCOL == mcp.PROTOCOL
    cases = [("list_projects", {}), ("list_tasks", {"project": "shop"}), ("get_task_status", {"task_id": 7}), ("get_task_result", {"task_id": 7}),
             ("create_task", {"project": "shop", "repo": "api", "title": "X", "prompt": "do"}),
             ("create_task", {"project": "shop", "repo": "api", "title": "X", "prompt": "do", "dispatch": False, "agent": "codex"}),
             ("create_task", {"project": "shop", "repo": "api", "title": "X", "prompt": "do", "after_task_id": 3, "auto_close": False}),
             ("create_task", {"project": "shop", "repo": "api", "title": "X", "prompt": "do", "auto_close": True}),
             ("create_task", {"project": "shop"}), ("dispatch_task", {"task_id": 3}),
             ("dispatch_task", {"task_id": 3, "session": " shop--api--s1 ", "force": True, "queue": True}),
             ("dispatch_task", {"task_id": 3, "agent": "codex", "auto_close": False}), ("dispatch_task", {"task_id": "3"})]
    state = {"projects": [{"name": "shop", "repos": [{"name": "api", "branch": "main", "state": "ok", "sessions": [{"name": "s1", "state": "idle", "launcher": "claude"}]}]}],
             "tasks": [{"id": 7, "project": "shop", "repo": "api", "title": "T", "column": "pr", "branch": "b", "worktree": "/w", "phase": "done"}]}

    def run(fn):
        calls = []

        def api(method, path, body=None):
            calls.append((method, path, body))
            return state if path == "/api/state" else {"id": 7, "title": "T"}
        try:
            out = fn(api)
        except RuntimeError as e:
            out = ("error", str(e))
        return out, calls
    for name, args in cases:
        mine = run(lambda api: mcp.tool_call(name, dict(args), api))
        def theirs(api, m=shim):
            m.call_api = api
            return m.tool_call(name, dict(args))
        other = run(theirs)
        assert mine[1] == other[1], (name, args)                       # the same board calls, in order, with the same bodies
        if isinstance(mine[0], tuple) or isinstance(other[0], tuple):   # both refuse (the remote's words may be stricter), or neither does
            assert isinstance(mine[0], tuple) and isinstance(other[0], tuple), (name, args, mine, other)
        else:
            assert mine[0] == other[0], (name, args)


def test_scopes(lite_client, projects_dir, fake_tmux):
    from app import main
    git_repo(projects_dir)
    on(lite_client)
    rtok, rrec = mint(lite_client, "reader", scopes=["read"])
    r = call(lite_client, rtok, "create_task", project="shop", repo="api", title="T", prompt="p", dispatch=False)
    assert "result" not in r and r["error"]["code"] == -32001 and "tasks" in r["error"]["message"]
    assert main.db.tasks() == []
    assert call(lite_client, rtok, "list_tasks")["result"]["isError"] is False
    ttok, trec = mint(lite_client, "default")
    assert trec["scopes"] == ["read", "tasks"], "a new token gets read and tasks"
    made = json.loads(call(lite_client, ttok, "create_task", project="shop", repo="api", title="T", prompt="p", dispatch=False)["result"]["content"][0]["text"])
    r = call(lite_client, ttok, "dispatch_task", task_id=made["id"], session="shop--api--s1")
    assert r["error"]["code"] == -32001 and "sessions" in r["error"]["message"], "typing into a running session needs sessions"
    stok, _ = mint(lite_client, "full", scopes=["read", "tasks", "sessions"])
    r = call(lite_client, stok, "dispatch_task", task_id=made["id"], session="shop--api--s1")
    assert "error" not in r and r["result"]["isError"] and "404" in r["result"]["content"][0]["text"], "past the scope, the board says the session is not there"
    assert main.db.task_get(made["id"])["phase"] == "backlog"
    listed = {t["id"]: t for t in lite_client.get("/api/mcp/tokens", headers=H).json()["tokens"]}
    assert listed[rrec["id"]]["last_tool"] == "list_tasks", "the refused create_task never moved it"


def test_a_refused_call_moves_no_last_used(lite_client, projects_dir, fake_tmux):
    on(lite_client)
    tok, rec = mint(lite_client, "reader", scopes=["read"])
    call(lite_client, tok, "create_task", project="shop", repo="api", title="T", prompt="p")
    rpc(lite_client, tok, INIT, Origin="https://x.example")
    rpc(lite_client, tok, INIT, **{"MCP-Protocol-Version": "1999-01-01"})
    row = lite_client.get("/api/mcp/tokens", headers=H).json()["tokens"][0]
    assert row["last_used_at"] is None and row["last_tool"] is None, "never used: Settings can say so truthfully"
    assert rpc(lite_client, tok, INIT).status_code == 200
    assert lite_client.get("/api/mcp/tokens", headers=H).json()["tokens"][0]["last_used_at"]


# ---------------------------------------------------------------- limits

def test_rate_limits_answer_429_with_retry_after(lite_client, _fresh_limits):
    on(lite_client)
    tok, _ = mint(lite_client)
    other, _ = mint(lite_client, "other")
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    for _ in range(60):
        assert rpc(lite_client, tok, ping).status_code == 200
    r = rpc(lite_client, tok, ping)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert rpc(lite_client, other, ping).status_code == 200, "per token"
    _fresh_limits["t"] += 61
    assert rpc(lite_client, tok, ping).status_code == 200, "the window moved on"


def test_create_and_dispatch_are_limited_to_ten_a_minute(lite_client, projects_dir, fake_tmux, _fresh_limits):
    git_repo(projects_dir)
    on(lite_client)
    tok, _ = mint(lite_client)
    for i in range(10):
        assert call(lite_client, tok, "create_task", project="shop", repo="api", title=f"T{i}", prompt="p", dispatch=False)["result"]["isError"] is False
    r = rpc(lite_client, tok, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "dispatch_task", "arguments": {"task_id": 1}}})
    assert r.status_code == 429 and r.headers["retry-after"]
    assert call(lite_client, tok, "list_tasks")["result"]["isError"] is False, "reads go on"
    _fresh_limits["t"] += 60.5
    assert call(lite_client, tok, "create_task", project="shop", repo="api", title="later", prompt="p", dispatch=False)["result"]["isError"] is False


def test_global_ceiling_and_in_flight(lite_client, monkeypatch):
    from app import mcp_remote
    on(lite_client)
    a, arec = mint(lite_client, "a")
    b, _ = mint(lite_client, "b")
    monkeypatch.setattr(mcp_remote, "GLOBAL_PER_MIN", 3)
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert [rpc(lite_client, t, ping).status_code for t in (a, b, a, b)] == [200, 200, 200, 429]
    mcp_remote.limiter.reset()
    for _ in range(mcp_remote.PER_TOKEN_IN_FLIGHT):
        assert mcp_remote.limiter.enter(arec["id"])
    r = rpc(lite_client, a, ping)
    assert r.status_code == 429 and r.headers["retry-after"] == "1"
    assert rpc(lite_client, b, ping).status_code == 200
    mcp_remote.limiter.leave(arec["id"])
    assert rpc(lite_client, a, ping).status_code == 200


# ---------------------------------------------------------------- the store and its secrets

def test_mint_rules(lite_client):
    from app import mcp_tokens
    tok, rec = mint(lite_client, "  my   laptop ")
    assert tok.startswith("ccbmcp_") and mcp_tokens.TOKEN_RE.match(tok) and rec["name"] == "my laptop"
    days = (mcp_tokens._parse(rec["expires_at"]) - mcp_tokens._parse(rec["created_at"])).days
    assert days == 90, "default expiry"
    assert lite_client.post("/api/mcp/tokens", headers=H, json={"name": "MY LAPTOP"}).status_code == 409, "names are unique"
    for body in ({"name": ""}, {"name": "x" * 41}, {"name": "ok", "scopes": ["admin"]}, {"name": "ok", "scopes": []}, {"name": "ok", "expires_days": 0},
                 {"name": "ok", "expires_days": 400}, {"name": "a\x00b"}):
        assert lite_client.post("/api/mcp/tokens", headers=H, json=body).status_code == 400, body
    for i in range(9):
        mint(lite_client, f"d{i}")
    r = lite_client.post("/api/mcp/tokens", headers=H, json={"name": "eleventh"})
    assert r.status_code == 409 and "10" in r.json()["error"]


def test_the_plaintext_is_shown_once_and_stored_nowhere(lite_client, projects_dir, fake_tmux, caplog):
    from app import main, mcp_tokens
    caplog.set_level(logging.DEBUG)
    git_repo(projects_dir)
    on(lite_client)
    r = lite_client.post("/api/mcp/tokens", headers=H, json={"name": "laptop"})
    assert r.headers["cache-control"] == "no-store"
    tok = r.json()["token"]
    dig = mcp_tokens.digest(tok)
    assert dig not in r.text, "the mint answer carries the token, never its digest"
    call(lite_client, tok, "create_task", project="shop", repo="api", title="Secret title", prompt="the secret prompt", dispatch=False)
    call(lite_client, tok, "list_tasks")
    rpc(lite_client, "ccbmcp_" + "Z" * 43, INIT)
    answers = [lite_client.get("/api/mcp/tokens", headers=H).text, lite_client.get("/api/state", headers=H).text,
               lite_client.put("/api/mcp/remote", headers=H, json={"enabled": True}).text]
    for text in answers:
        assert tok not in text and dig not in text and '"digest"' not in text
    rows = main.db.conn.execute("SELECT key, value FROM kv").fetchall()
    dump = json.dumps([list(r) for r in rows])
    assert tok not in dump and tok[len("ccbmcp_"):] not in dump, "the store holds no plaintext"
    assert dump.count(dig) == 1, "only the digest, once"
    audit = json.loads(next(r[1] for r in rows if r[0] == "mcp_audit"))
    assert all(set(a) == {"at", "token_id", "name", "tool", "task_id", "outcome"} for a in audit)
    assert "secret prompt" not in json.dumps(audit)
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert tok not in logs and dig not in logs and tok[len("ccbmcp_"):len("ccbmcp_") + 12] not in logs
    assert "laptop" in logs, "the log names the token"
    events = json.dumps([dict(r) for r in main.db.conn.execute("SELECT * FROM events").fetchall()])
    assert tok not in events and dig not in events


def test_no_token_shaped_literal_ships_in_app_static():
    """The page builds the token dialog from the mint answer only: no device token (or its prefix with a body) is ever a literal in app/static."""
    import re
    rx = re.compile(r"ccbmcp_[A-Za-z0-9_-]{8,}")
    hits = [str(p.relative_to(ROOT)) for p in (ROOT / "app" / "static").rglob("*") if p.is_file() and p.suffix in (".js", ".html", ".json", ".css")
            and rx.search(p.read_text(encoding="utf-8", errors="replace"))]
    assert hits == []
