import importlib.util
import json
import pathlib
import subprocess
import sys

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_shim():
    spec = importlib.util.spec_from_file_location("ccboard_mcp", str(ROOT / "scripts" / "ccboard_mcp.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_protocol_handshake_and_tools(monkeypatch):
    m = load_shim()
    init = m.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}})
    assert init["result"]["protocolVersion"] == "2025-06-18" and init["result"]["capabilities"] == {"tools": {}}
    assert m.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert m.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}
    tools = m.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == ["list_projects", "create_task", "list_tasks", "get_task_status"]
    assert m.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})["error"]["code"] == -32601
    # tool calls go through call_api; fake it
    calls = []
    def fake_api(method, path, body=None):
        calls.append((method, path, body))
        if path == "/api/state":
            return {"projects": [{"name": "shop", "repos": [{"name": "api", "branch": "main", "state": "ok", "sessions": [{"name": "s1", "state": "working", "launcher": "claude"}]}]}],
                    "tasks": [{"id": 7, "project": "shop", "repo": "api", "title": "T", "column": "pr", "branch": "b", "worktree": "/w", "pr_url": "u", "pr_state": "OPEN", "ci": None, "cost_usd": 1.0, "overlap": [], "session": {"state": "done"}}]}
        return {"id": 8, "slug": "x", "tmux": "shop--api--t-x"}
    monkeypatch.setattr(m, "call_api", fake_api)
    r = m.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "create_task", "arguments": {"project": "shop", "repo": "api", "title": "X", "prompt": "do"}}})
    assert not r["result"]["isError"] and json.loads(r["result"]["content"][0]["text"])["id"] == 8
    assert calls[-1] == ("POST", "/api/projects/shop/repos/api/tasks", {"title": "X", "prompt": "do"})
    r = m.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "get_task_status", "arguments": {"task_id": 7}}})
    assert json.loads(r["result"]["content"][0]["text"])["column"] == "pr"
    r = m.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "list_tasks", "arguments": {"project": "other"}}})
    assert json.loads(r["result"]["content"][0]["text"])["tasks"] == []
    r = m.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "create_task", "arguments": {"project": "shop"}}})
    assert r["result"]["isError"] and "required" in r["result"]["content"][0]["text"]


def test_stdio_roundtrip():
    lines = "\n".join(json.dumps(x) for x in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        "not json at all",
    ]) + "\nnot json at all\n"
    cp = subprocess.run([sys.executable, str(ROOT / "scripts" / "ccboard_mcp.py")], input=lines, capture_output=True, text=True, timeout=20)
    out = [json.loads(l) for l in cp.stdout.splitlines() if l.strip()]
    assert out[0]["id"] == 1 and out[0]["result"]["protocolVersion"] == "2025-03-26"
    assert out[1]["id"] == 2 and len(out[1]["result"]["tools"]) == 4
    assert any(o.get("error", {}).get("code") == -32700 for o in out)


def test_token_grants_local_owner(client, projects_dir, fake_tmux):
    from app import hooks
    T = {"X-CCBoard-Token": hooks.ensure_token()}
    st = client.get("/api/state", headers=T).json()
    assert st["user"] == "local-token"
    assert client.post("/api/projects", headers=T, json={"name": "viamcp"}).status_code == 201
    assert client.get("/api/state", headers={"X-CCBoard-Token": "wrong"}).status_code == 403
    assert client.get("/", headers=T).status_code == 403          # token only covers /api


def test_create_task_dispatch_flag(monkeypatch):
    """dispatch (default true) keeps the legacy start-now call byte for byte; false files a Backlog card through POST /api/tasks."""
    m = load_shim()
    schema = next(t for t in m.TOOLS if t["name"] == "create_task")["inputSchema"]
    assert schema["properties"]["dispatch"]["type"] == "boolean" and schema["properties"]["dispatch"]["default"] is True
    assert "dispatch" not in schema["required"] and len(m.TOOLS) == 4
    calls = []

    def fake_api(method, path, body=None):
        calls.append((method, path, body))
        if path == "/api/state":
            return {"tasks": [{"id": 3, "project": "shop", "repo": "api", "title": "T", "column": "backlog", "phase": "backlog",
                               "branch": "", "worktree": "", "session": None}]}
        return {"id": 9, "slug": "x", "phase": "backlog", "tmux": None}
    monkeypatch.setattr(m, "call_api", fake_api)

    def create(**extra):
        r = m.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "create_task", "arguments": {
            "project": "shop", "repo": "api", "title": "X", "prompt": "do", **extra}}})
        assert not r["result"]["isError"], r
        return json.loads(r["result"]["content"][0]["text"])
    assert create(dispatch=False)["phase"] == "backlog"
    assert calls[-1] == ("POST", "/api/tasks", {"project": "shop", "repo": "api", "title": "X", "prompt": "do", "when": "later"})
    create()
    assert calls[-1] == ("POST", "/api/projects/shop/repos/api/tasks", {"title": "X", "prompt": "do"})
    create(dispatch=True)
    assert calls[-1] == ("POST", "/api/projects/shop/repos/api/tasks", {"title": "X", "prompt": "do"})
    r = m.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_tasks", "arguments": {}}})
    assert json.loads(r["result"]["content"][0]["text"])["tasks"][0]["phase"] == "backlog"
    r = m.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_task_status", "arguments": {"task_id": 3}}})
    assert json.loads(r["result"]["content"][0]["text"])["phase"] == "backlog"


# ---------------------------------------------------------------- create_task agent passthrough (v0.5.11)

def _create(m, calls, **extra):
    r = m.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "create_task", "arguments": {
        "project": "shop", "repo": "api", "title": "X", "prompt": "do", **extra}}})
    return r["result"]


def test_create_task_agent_passthrough(monkeypatch):
    """The agent is sent only when the caller names one; claude (or none) keeps the legacy bodies byte for byte, and codex starts through
    the general POST /api/tasks (the per-repo route is Claude's)."""
    m = load_shim()
    schema = next(t for t in m.TOOLS if t["name"] == "create_task")["inputSchema"]
    assert schema["properties"]["agent"]["enum"] == ["claude", "codex"] and schema["properties"]["agent"]["default"] == "claude"
    assert "agent" not in schema["required"] and len(m.TOOLS) == 4
    calls = []
    monkeypatch.setattr(m, "call_api", lambda method, path, body=None: calls.append((method, path, body)) or {"id": 5, "slug": "x"})
    base = {"project": "shop", "repo": "api", "title": "X", "prompt": "do"}
    assert not _create(m, calls)["isError"]
    assert calls[-1] == ("POST", "/api/projects/shop/repos/api/tasks", {"title": "X", "prompt": "do"})
    assert not _create(m, calls, agent="claude")["isError"]
    assert calls[-1] == ("POST", "/api/projects/shop/repos/api/tasks", {"title": "X", "prompt": "do"}), "an explicit claude is the legacy call"
    assert not _create(m, calls, agent="codex")["isError"]
    assert calls[-1] == ("POST", "/api/tasks", {**base, "when": "now", "agent": "codex"})
    assert not _create(m, calls, agent="codex", dispatch=True)["isError"]
    assert calls[-1] == ("POST", "/api/tasks", {**base, "when": "now", "agent": "codex"})
    assert not _create(m, calls, agent="codex", dispatch=False)["isError"]
    assert calls[-1] == ("POST", "/api/tasks", {**base, "when": "later", "agent": "codex"})
    assert not _create(m, calls, agent="claude", dispatch=False)["isError"]
    assert calls[-1] == ("POST", "/api/tasks", {**base, "when": "later", "agent": "claude"})
    assert not _create(m, calls, dispatch=False)["isError"]
    assert calls[-1] == ("POST", "/api/tasks", {**base, "when": "later"}), "no agent named: the body is the one it always was"
    n = len(calls)
    for bad in ("gemini", "", "Codex", 3):
        r = _create(m, calls, agent=bad)
        assert r["isError"] and "agent must be claude or codex" in r["content"][0]["text"], bad
    assert len(calls) == n, "a bad agent never reaches the board"


def test_create_task_agent_codex_through_a_real_board(lite_client, projects_dir, fake_tmux, fake_codex, monkeypatch):
    """The shim's call_api routed into the board (token auth, as over loopback): codex starts in a managed worktree, or files a backlog
    card that remembers codex."""
    import json as _json
    import shlex
    from app import hooks, main
    m = load_shim()
    repo = projects_dir / "shop" / "api"
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@e.x", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"],
                   check=True)
    T = {"X-CCBoard-Token": hooks.ensure_token()}

    def via_board(method, path, body=None):
        r = lite_client.request(method, path, headers=T, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"ccboard {r.status_code}: {r.json().get('error')}")
        return r.json()
    monkeypatch.setattr(m, "call_api", via_board)
    r = _create(m, [], agent="codex")
    assert not r["isError"], r
    out = _json.loads(r["content"][0]["text"])
    assert out["phase"] == "running" and out["branch"] == "worktree-x" and (repo / ".ccboard" / "worktrees" / "x").is_dir()
    assert shlex.split(dict(fake_tmux["sent"])[out["tmux"]])[0] == "codex"
    assert main.db.task_get(out["id"])["agent"] == "codex"
    r = _create(m, [], agent="codex", dispatch=False, title="Later one")
    out = _json.loads(r["content"][0]["text"])
    assert out["phase"] == "backlog" and main.db.task_get(out["id"])["agent"] == "codex"
    r = m.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_task_status", "arguments": {"task_id": out["id"]}}})
    assert _json.loads(r["result"]["content"][0]["text"])["phase"] == "backlog"
    # bypass is not something a tool call can ask for: create_task's schema carries no permission fields
    assert "permission_mode" not in next(t for t in m.TOOLS if t["name"] == "create_task")["inputSchema"]["properties"]
