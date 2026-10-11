#!/usr/bin/env python3
"""ccboard as an MCP server (stdio). A dependency-free JSON-RPC shim that calls the board over loopback
with the local hook token. It is box-only: the hook token opens the whole /api surface, so the shim refuses any
CCBOARD_URL that is not plain http to a loopback address (127.0.0.0/8, ::1 or the name localhost) and never follows
a redirect. Another device needs the board's remote MCP endpoint with a token of its own, not a copy of this shim.
Register once (on the box) with:
    claude mcp add --scope user ccboard -- /path/to/ccboard/.venv/bin/python /path/to/ccboard/scripts/ccboard_mcp.py
    codex mcp add ccboard --env CCBOARD_URL=http://127.0.0.1:8000 -- /path/to/ccboard/.venv/bin/python /path/to/ccboard/scripts/ccboard_mcp.py
Tools: list_projects, create_task (dispatch false = a Backlog card; after_task_id = a chain step), list_tasks, get_task_status,
dispatch_task (start a Backlog task in a new session or a running one), get_task_result (what a finished task said), list_nodes (the paired nodes).
Each tool but list_nodes takes an optional `node` (a paired node's handle): the shim then calls the board's own hub routes /api/nodes/<handle>/... with the same hook token over
loopback, and the board decides (CCBOARD_MCP_NODES, the pair's scopes, the guard); it can read a node and create or start a task there in a NEW session, nothing more.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BOARD = os.environ.get("CCBOARD_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN_FILE = os.environ.get("CCBOARD_HOOK_TOKEN_FILE") or str(Path.home() / ".local" / "share" / "ccboard" / "hook-token")
PROTOCOL = "2025-06-18"

# A copy of app/mcp.py's TOOLS and tool_call (the remote /mcp endpoint's): on a docker box this file runs from a folder without app/.
# tests/test_mcp_remote.py fails when the two drift apart; change both together.
NODE_PROP = {"type": "string", "default": "local",
             "description": "which board runs this: local (this board, the default) or the handle of a paired node from list_nodes. Another node works only when this board allows it (CCBOARD_MCP_NODES)"}
ON_NODES = " Runs on this board unless node names a paired one; call list_nodes first."

NODE_PROP = {"type": "string", "default": "local",
             "description": "which board runs this: local (this board, the default) or the handle of a paired node from list_nodes. Another node works only when this board allows it (CCBOARD_MCP_NODES)"}
ON_NODES = " Runs on this board unless node names a paired one; call list_nodes first."

TOOLS = [
    {"name": "list_projects", "description": "List ccboard projects with their repos, branches and live sessions." + ON_NODES,
     "inputSchema": {"type": "object", "properties": {"node": NODE_PROP}, "additionalProperties": False}},
    {"name": "create_task",
     "description": "Create a ccboard task for <project>/<repo> (repo 'root' = the project folder, when it is a git repo). By default Claude (or Codex, with agent codex) starts at once on <prompt> in a fresh git worktree + branch and the answer has the task id and tmux session; with dispatch false the task only goes to the Backlog column for a person to start later. Returns the task id." + ON_NODES + " On another node the task id comes back as <handle>:<id>, after_task_id is not available, and the node starts it with safe, fixed launch options.",
     "inputSchema": {"type": "object", "properties": {
         "node": NODE_PROP, "project": {"type": "string"}, "repo": {"type": "string"},
         "title": {"type": "string", "description": "short title; becomes the branch name"},
         "prompt": {"type": "string", "description": "what Claude should do"},
         "dispatch": {"type": "boolean", "default": True,
                      "description": "true (default): start a session now; false: add to the Backlog without starting anything"},
         "agent": {"type": "string", "enum": ["claude", "codex"], "default": "claude",
                   "description": "which coding agent runs the task: claude (default) or codex (it gets a git worktree under .ccboard/worktrees)"},
         "after_task_id": {"type": "integer",
                           "description": "queue this task behind another: when that task is done it starts in a new session of its own with the other task's result appended to its prompt (or put where {{result}} stands); the agent defaults to the other task's. Ignores dispatch."},
         "auto_close": {"type": "boolean",
                        "description": "close the agent's session when this task's turn ends (after a short grace period); not sent = the board's default"}},
         "required": ["project", "repo", "title", "prompt"], "additionalProperties": False}},
    {"name": "list_tasks", "description": "List ccboard tasks (optionally for one project) with their board column and PR state." + ON_NODES + " On another node the ids are <handle>:<id> and the column, PR link and CI are not shown.",
     "inputSchema": {"type": "object", "properties": {"node": NODE_PROP, "project": {"type": "string"}}, "additionalProperties": False}},
    {"name": "get_task_status", "description": "Status of one ccboard task: column, session state, last message, PR/CI, cost." + ON_NODES + " For another node task_id is the id on that node; only the fields a node shares are filled, the rest are null.",
     "inputSchema": {"type": "object", "properties": {"node": NODE_PROP, "task_id": {"type": "integer"}}, "required": ["task_id"], "additionalProperties": False}},
    {"name": "dispatch_task",
     "description": "Start a Backlog task. By default it gets a new session of its own (its agent, in a fresh git worktree); with session (a tmux name from list_projects) its prompt is pasted into that running session instead, which must be idle or done. Returns the tmux session; limit_warning is set when the Claude usage window is nearly full (the task starts anyway)." + ON_NODES + " On another node a task can only start in a new session there (no session argument).",
     "inputSchema": {"type": "object", "properties": {
         "node": NODE_PROP, "task_id": {"type": "integer"},
         "session": {"type": "string", "description": "tmux name of a running session to hand the task to; omit for a new session"},
         "force": {"type": "boolean", "description": "with session: send it even when that session works in another repo (the prompt then names the task's repo)"},
         "queue": {"type": "boolean", "description": "with session: also accept a Claude session that is working (the text is queued behind its current turn)"},
         "auto_close": {"type": "boolean", "description": "close the session when the task's turn ends; not sent = on for a new session, off for a running one"},
         "agent": {"type": "string", "enum": ["claude", "codex"], "description": "new session only: override the agent the task was created for"}},
         "required": ["task_id"], "additionalProperties": False}},
    {"name": "get_task_result",
     "description": "What a finished ccboard task said: its phase (backlog, queued, running, done, failed, cancelled), the full final message of its last turn (result), when it finished, and whether its session was closed after the stop. result is null until the task is done." + ON_NODES + " A node does not share the result text: for another node the answer has the phase and times, and the result is read on that node itself.",
     "inputSchema": {"type": "object", "properties": {"node": NODE_PROP, "task_id": {"type": "integer"}}, "required": ["task_id"], "additionalProperties": False}},
    {"name": "list_nodes",
     "description": "List the paired nodes this board can reach: handle, name, status and age of the last reading, agents with login state, free task lanes, usage windows and the scopes this board holds on each, and whether this board lets agents act on nodes (enabled). Read this first; its handles are the values of node on the other tools. Never shows an address or a token.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
]


REFUSED = ("ccboard: this MCP shim only talks to the board on this machine (http to 127.0.0.1, ::1 or localhost); "
           "the hook token stays on the box. To reach the board from another device, use the board's remote MCP endpoint "
           "with a token of its own instead of this shim.")


def loopback_url(url: str) -> bool:
    """True only for http://<loopback>[:port][/path]: scheme http, no userinfo, host 127.0.0.0/8, ::1 or 'localhost'."""
    try:
        u = urllib.parse.urlsplit(url)
        host, _port = u.hostname, u.port              # .port raises on a malformed port
    except ValueError:
        return False
    if u.scheme != "http" or not host or "@" in u.netloc:
        return False
    if host == "localhost":
        return True
    try:
        a = ipaddress.ip_address(host)
    except ValueError:
        return False
    return a.is_loopback and (a.version == 4 or a == ipaddress.IPv6Address("::1"))     # 127.0.0.0/8 or ::1 exactly (not ::ffff:127.0.0.1)


BOARD_OK = loopback_url(BOARD)                         # parsed once: CCBOARD_URL is read at start-up only


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is an error, never a second request carrying the hook token somewhere else."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def token() -> str:
    try:
        return Path(TOKEN_FILE).read_text().strip()
    except OSError:
        return ""


def call_api(method: str, path: str, body: dict | None = None) -> dict:
    if not BOARD_OK:                                   # before reading the token or opening a socket
        raise RuntimeError(REFUSED)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BOARD + path, data=data, method=method,
                                 headers={"X-CCBoard-Token": token(), "X-CCBoard": "1", "Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            raise RuntimeError(f"ccboard {e.code}: the board answered with a redirect; it is not followed (the hook token is never re-sent)")
        try:
            msg = json.loads(e.read()).get("error")
        except Exception:
            msg = str(e)
        raise RuntimeError(f"ccboard {e.code}: {msg}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"ccboard unreachable at {BOARD}: {e.reason}")


_HANDLE = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
NODE_HELP = "node must be local or the handle of a paired node (call list_nodes)"


def _node(args):
    """The node a call is for: None for this board (no node, null or `local`), else a handle checked for shape (the board checks that it is a paired one, and may refuse)."""
    n = args.get("node")
    if n is None or n == "local":
        return None
    if not isinstance(n, str) or not _HANDLE.fullmatch(n):
        raise RuntimeError(NODE_HELP)
    return n


def _unwrap(out):
    """(data, age) of a relayed answer {node, age, data}."""
    if not isinstance(out, dict) or not isinstance(out.get("data"), dict):
        raise RuntimeError("ccboard 502: the node's answer had an unexpected shape")
    return out["data"], out.get("age")


def _rid(v):
    if not isinstance(v, int) or isinstance(v, bool) or not 0 < v < 10 ** 12:
        raise RuntimeError("task_id is required")
    return v


def _started(h, out):
    """The answer of a relayed create or dispatch: the node's fields, `id` as the ref <handle>:<id> (never the bare number of another board)."""
    d, age = _unwrap(out)
    task = d.get("task") if isinstance(d.get("task"), dict) else None
    res = {k: v for k, v in d.items() if k not in ("ref", "id", "task")}
    res = {"id": d.get("ref") or f"{h}:{d.get('id')}", **res, "node": h, "age": age}
    if task is not None:
        res["task"] = {**task, "id": f"{h}:{task.get('id')}" if isinstance(task.get("id"), int) else None}
    return res


def _remote(name, args, h, api):
    """A tool call for the paired node `h`: the board's hub routes /api/nodes/<h>/... relay it (the board checks CCBOARD_MCP_NODES, this caller's scope, the pair's scope and the guard).
    Only what the relay rows carry comes back; the rest is null, and a note says so."""
    if name == "list_projects":
        d, age = _unwrap(api("GET", f"/api/nodes/{h}/state"))
        sess = d.get("sessions") or []
        return {"node": h, "age": age, "truncated": bool(d.get("truncated")),
                "projects": [{"name": p.get("name"), "repos": [{"name": r.get("name"), "branch": r.get("branch"), "dirty": r.get("dirty"),
                              "sessions": [{"name": s.get("tmux"), "state": s.get("state"), "launcher": s.get("kind"), "agent": s.get("agent")}
                                           for s in sess if s.get("project") == p.get("name") and s.get("repo") == r.get("name")]}
                              for r in p.get("repos") or []]} for p in d.get("projects") or []]}
    if name == "list_tasks":
        d, age = _unwrap(api("GET", f"/api/nodes/{h}/state"))
        states = {s.get("tmux"): s.get("state") for s in d.get("sessions") or [] if s.get("tmux")}
        return {"node": h, "age": age, "truncated": bool(d.get("truncated")),
                "tasks": [{"id": f"{h}:{t['id']}", "project": t.get("project"), "repo": t.get("repo"), "title": t.get("title"), "column": None, "phase": t.get("phase"),
                           "branch": t.get("branch"), "pr_url": None, "session_state": states.get(t.get("tmux"))}
                          for t in d.get("tasks") or [] if isinstance(t.get("id"), int) and (not args.get("project") or t.get("project") == args["project"])]}
    if name in ("get_task_status", "get_task_result"):
        tid = _rid(args.get("task_id"))
        t, age = _unwrap(api("GET", f"/api/nodes/{h}/tasks/{tid}"))
        ref = f"{h}:{tid}"
        if name == "get_task_result":
            return {"id": ref, "title": t.get("title"), "phase": t.get("phase"), "column": None, "result": None, "result_at": None, "done_at": t.get("done_at"),
                    "closed_at": None, "parent_id": None, "chain": None, "has_result": t.get("has_result"), "node": h, "age": age,
                    "note": f"a node does not share the result text with other nodes: read it on {h} itself (has_result says whether there is one)"}
        return {"id": ref, "title": t.get("title"), "column": None, "phase": t.get("phase"), "agent": t.get("agent"), "project": t.get("project"), "repo": t.get("repo"),
                "branch": t.get("branch"), "worktree": None, "session": {"tmux": t["tmux"]} if t.get("tmux") else None, "pr_url": None, "pr_state": t.get("pr_state"),
                "pr_number": t.get("pr_number"), "ci": None, "cost_usd": None, "overlap": None, "updated_at": t.get("updated_at"), "done_at": t.get("done_at"),
                "has_result": t.get("has_result"), "node": h, "age": age}
    if name == "create_task":
        for k in ("project", "repo", "title", "prompt"):
            if not isinstance(args.get(k), str) or not args[k].strip():
                raise RuntimeError(f"{k} is required")
        if args.get("after_task_id") is not None:
            raise RuntimeError("after_task_id cannot be used with node: a chain stays inside one node")
        agent, auto = args.get("agent"), args.get("auto_close")
        if agent not in (None, "claude", "codex"):
            raise RuntimeError("agent must be claude or codex")
        if auto is not None and not isinstance(auto, bool):
            raise RuntimeError("auto_close must be true or false")
        body = {"project": args["project"], "repo": args["repo"], "title": args["title"], "prompt": args["prompt"],
                "when": "later" if args.get("dispatch", True) is False else "now"}
        if agent:
            body["agent"] = agent
        if auto is not None:
            body["auto_close"] = auto
        return _started(h, api("POST", f"/api/nodes/{h}/tasks", body))
    if name == "dispatch_task":
        tid = _rid(args.get("task_id"))
        if args.get("session") not in (None, ""):
            raise RuntimeError("an agent cannot hand a task to a running session on another node: leave session out to start the task in a new session there")
        if args.get("force") or args.get("queue"):
            raise RuntimeError("force and queue are for a running session; they are not used with node")
        agent, auto = args.get("agent"), args.get("auto_close")
        if agent not in (None, "claude", "codex"):
            raise RuntimeError("agent must be claude or codex")
        if auto is not None and not isinstance(auto, bool):
            raise RuntimeError("auto_close must be true or false")
        body = {"mode": "lane"}
        if agent:
            body["agent"] = agent
        if auto is not None:
            body["auto_close"] = auto
        return _started(h, api("POST", f"/api/nodes/{h}/tasks/{tid}/dispatch", body))
    raise RuntimeError(f"unknown tool {name}")


def tool_call(name: str, args: dict) -> dict:
    if name == "list_nodes":
        return call_api("GET", "/api/mcp/nodes")
    node = _node(args)
    if node is not None:
        return _remote(name, args, node, call_api)
    if name == "list_projects":
        st = call_api("GET", "/api/state")
        return {"projects": [{"name": p["name"], "repos": [{"name": r["name"], "branch": r.get("branch"), "state": r.get("state"),
                             "sessions": [{"name": s["name"], "state": s.get("state"), "launcher": s.get("launcher")} for s in r["sessions"]]}
                             for r in p["repos"]]} for p in st["projects"]]}
    if name == "create_task":
        for k in ("project", "repo", "title", "prompt"):
            if not isinstance(args.get(k), str) or not args[k].strip():
                raise RuntimeError(f"{k} is required")
        agent = args.get("agent")
        if agent not in (None, "claude", "codex"):
            raise RuntimeError("agent must be claude or codex")
        named = {"agent": agent} if agent else {}         # sent only when the caller names one: the default bodies are unchanged
        after, auto = args.get("after_task_id"), args.get("auto_close")
        if after is not None and (not isinstance(after, int) or isinstance(after, bool)):
            raise RuntimeError("after_task_id must be a task id")
        if auto is not None and not isinstance(auto, bool):
            raise RuntimeError("auto_close must be true or false")
        more = {**({"after_task_id": after} if after is not None else {}), **({"auto_close": auto} if auto is not None else {})}
        if after is not None:                            # a chain step: it waits for that task, whatever dispatch says
            return call_api("POST", "/api/tasks", {"project": args["project"], "repo": args["repo"], "title": args["title"],
                                                   "prompt": args["prompt"], "when": "later", **named, **more})
        if args.get("dispatch", True) is False:          # a backlog card: nothing starts until someone dispatches it
            return call_api("POST", "/api/tasks", {"project": args["project"], "repo": args["repo"], "title": args["title"],
                                                   "prompt": args["prompt"], "when": "later", **named, **more})
        if agent == "codex" or auto is not None:         # the legacy start-now route is Claude's and takes no auto_close; the general one does
            return call_api("POST", "/api/tasks", {"project": args["project"], "repo": args["repo"], "title": args["title"],
                                                   "prompt": args["prompt"], "when": "now", **named, **more})
        return call_api("POST", f"/api/projects/{args['project']}/repos/{args['repo']}/tasks",
                        {"title": args["title"], "prompt": args["prompt"]})
    if name == "list_tasks":
        st = call_api("GET", "/api/state")
        out = [{"id": t["id"], "project": t["project"], "repo": t["repo"], "title": t["title"], "column": t["column"],
                "phase": t.get("phase"), "branch": t["branch"], "pr_url": t.get("pr_url"),
                "session_state": (t.get("session") or {}).get("state")}
               for t in st["tasks"] if not args.get("project") or t["project"] == args["project"]]
        return {"tasks": out}
    if name == "get_task_status":
        tid = args.get("task_id")
        st = call_api("GET", "/api/state")
        t = next((t for t in st["tasks"] if t["id"] == tid), None)
        if not t:
            raise RuntimeError(f"no task {tid}")
        return {"id": t["id"], "title": t["title"], "column": t["column"], "phase": t.get("phase"), "branch": t["branch"], "worktree": t["worktree"],
                "session": t.get("session"), "pr_url": t.get("pr_url"), "pr_state": t.get("pr_state"), "ci": t.get("ci"),
                "cost_usd": t.get("cost_usd"), "overlap": t.get("overlap")}
    if name == "dispatch_task":
        tid = args.get("task_id")
        if not isinstance(tid, int) or isinstance(tid, bool):
            raise RuntimeError("task_id is required")
        session = args.get("session")
        if session is not None and (not isinstance(session, str) or not session.strip()):
            raise RuntimeError("session must be a tmux session name")
        agent = args.get("agent")
        if agent not in (None, "claude", "codex"):
            raise RuntimeError("agent must be claude or codex")
        for k in ("force", "queue", "auto_close"):
            if args.get(k) is not None and not isinstance(args[k], bool):
                raise RuntimeError(f"{k} must be true or false")
        body = {"session": session.strip()} if session else {"mode": "lane"}
        body.update({k: args[k] for k in ("force", "queue", "auto_close") if args.get(k) is not None})
        if agent and not session:
            body["agent"] = agent
        return call_api("POST", f"/api/tasks/{tid}/dispatch", body)
    if name == "get_task_result":
        tid = args.get("task_id")
        if not isinstance(tid, int) or isinstance(tid, bool):
            raise RuntimeError("task_id is required")
        t = call_api("GET", f"/api/tasks/{tid}")
        return {k: t.get(k) for k in ("id", "title", "phase", "column", "result", "result_at", "done_at", "closed_at", "parent_id", "chain")}
    raise RuntimeError(f"unknown tool {name}")


def handle(msg: dict) -> dict | None:
    """One JSON-RPC message -> response (None for notifications)."""
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": msg.get("params", {}).get("protocolVersion") or PROTOCOL,
                "capabilities": {"tools": {}}, "serverInfo": {"name": "ccboard", "version": "0.4"}}}
    if method == "notifications/initialized" or mid is None:
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            out = tool_call(params.get("name", ""), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "isError": False}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": str(e)}], "isError": True}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
            sys.stdout.flush()
            continue
        resp = handle(msg) if isinstance(msg, dict) else None
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
