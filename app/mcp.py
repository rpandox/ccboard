"""The board's MCP tools: their schemas, the argument checks and the calls they make (issue #13).

The source of the six tools both MCP servers offer: the remote streamable-HTTP endpoint at /mcp (app/mcp_remote.py, a device token per
client, the calls made in-process) and the box's stdio shim scripts/ccboard_mcp.py. The shim keeps a copy of TOOLS and of tool_call,
because on a docker box it runs from a folder that holds scripts/ but not app/; tests/test_mcp_remote.py pins both copies equal.

tool_call(name, args, api) takes `api(method, path, body=None) -> dict`, which raises RuntimeError("ccboard <status>: <message>") for a
refused call: the shim's is HTTP over loopback with the hook token, the remote endpoint's calls the board's route functions directly.
Nothing here imports the web app, so this module is as plain as the shim.

Nodes (issue #152): every tool but list_nodes takes an optional `node` (a paired node's handle; `local` or nothing = this board). A call for another node goes to the board's
own hub routes (/api/nodes/<handle>/...), which relay it with the pair's token: the two copies of tool_call make the same calls. The board decides whether it may (CCBOARD_MCP_NODES,
the caller's scope, the pair's scope, the guard); an agent can read a node's projects, tasks and agents and create a task or start it in a NEW session there, nothing more.

What the tools can never do: set a permission mode, a sandbox, a bypass or a model. The schemas name no such argument and
check_args() refuses any argument a schema does not list (additionalProperties false), so a client cannot slip one in.
"""
from __future__ import annotations

import json
import re

PROTOCOL = "2025-06-18"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26")
SERVER_VERSION = "0.5"

# The longest value each string argument may have. The board cuts a title at 120 and refuses a prompt over 20000; here both are refused
# outright, so a client learns its title was too long instead of seeing it cut.
MAX_LEN = {"project": 64, "repo": 64, "title": 120, "prompt": 20000, "session": 200, "node": 31}

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
TOOL_NAMES = tuple(t["name"] for t in TOOLS)
_SCHEMAS = {t["name"]: t["inputSchema"] for t in TOOLS}

# Device-token scopes (the remote endpoint): read = the four readers, tasks = create a task or start one in a new session, sessions = hand a
# task to a session that is already running (its prompt is typed into it). A new token gets read and tasks; sessions is an explicit choice.
SCOPES = ("read", "tasks", "sessions")
DEFAULT_SCOPES = ("read", "tasks")


def required_scope(name: str, args: dict | None) -> str | None:
    """The scope a call needs, or None for a tool that does not exist (tool_call refuses it). On another node a dispatch is a lane (new session) start only: it needs `tasks`,
    and a `session` argument is refused by the tool itself (an agent never types into a running session of another node)."""
    if name in ("list_projects", "list_tasks", "get_task_status", "get_task_result", "list_nodes"):
        return "read"
    if name == "create_task":
        return "tasks"
    if name == "dispatch_task":
        remote = isinstance(args, dict) and args.get("node") not in (None, "", "local")
        return "sessions" if isinstance(args, dict) and args.get("session") not in (None, "") and not remote else "tasks"
    return None


def _type_ok(spec: dict, v) -> bool:
    t = spec.get("type")
    if t == "string":
        return isinstance(v, str)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "boolean":
        return isinstance(v, bool)
    return True


def check_args(name: str, args) -> dict:
    """The arguments of one call, checked against the tool's schema: an object, no argument the schema does not list (so no permission
    mode, sandbox, bypass or model can be passed), each value of its type and enum, strings within MAX_LEN. A null counts as not given.
    Raises RuntimeError with a message for the client; returns the arguments."""
    schema = _SCHEMAS.get(name)
    if schema is None:
        raise RuntimeError(f"unknown tool {name}")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise RuntimeError("arguments must be an object")
    props = schema["properties"]
    unknown = sorted(k for k in args if k not in props)
    if unknown:
        allowed = ", ".join(props) or "none"
        raise RuntimeError(f"unknown argument{'s' if len(unknown) > 1 else ''} {', '.join(unknown)}; {name} takes only: {allowed}")
    for k, v in args.items():
        if v is None:
            continue
        spec = props[k]
        if not _type_ok(spec, v):
            want = {"string": "a string", "integer": "an integer", "boolean": "true or false"}.get(spec.get("type"), spec.get("type"))
            raise RuntimeError(f"{k} must be {want}")
        if "enum" in spec and v not in spec["enum"]:
            raise RuntimeError(f"{k} must be {' or '.join(spec['enum'])}")
        if isinstance(v, str) and len(v) > MAX_LEN.get(k, 2000):
            raise RuntimeError(f"{k} is too long (at most {MAX_LEN.get(k, 2000)} characters)")
    return args


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


def tool_call(name: str, args: dict, api) -> dict:
    """Run one tool through `api`. The same calls, in the same order and with the same bodies, as the stdio shim's tool_call."""
    args = check_args(name, args)
    if name == "list_nodes":
        return api("GET", "/api/mcp/nodes")
    node = _node(args)
    if node is not None:
        return _remote(name, args, node, api)
    if name == "list_projects":
        st = api("GET", "/api/state")
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
        named = {"agent": agent} if agent else {}
        after, auto = args.get("after_task_id"), args.get("auto_close")
        if after is not None and (not isinstance(after, int) or isinstance(after, bool)):
            raise RuntimeError("after_task_id must be a task id")
        if auto is not None and not isinstance(auto, bool):
            raise RuntimeError("auto_close must be true or false")
        more = {**({"after_task_id": after} if after is not None else {}), **({"auto_close": auto} if auto is not None else {})}
        if after is not None:
            return api("POST", "/api/tasks", {"project": args["project"], "repo": args["repo"], "title": args["title"],
                                              "prompt": args["prompt"], "when": "later", **named, **more})
        if args.get("dispatch", True) is False:
            return api("POST", "/api/tasks", {"project": args["project"], "repo": args["repo"], "title": args["title"],
                                              "prompt": args["prompt"], "when": "later", **named, **more})
        if agent == "codex" or auto is not None:
            return api("POST", "/api/tasks", {"project": args["project"], "repo": args["repo"], "title": args["title"],
                                              "prompt": args["prompt"], "when": "now", **named, **more})
        return api("POST", f"/api/projects/{args['project']}/repos/{args['repo']}/tasks",
                   {"title": args["title"], "prompt": args["prompt"]})
    if name == "list_tasks":
        st = api("GET", "/api/state")
        out = [{"id": t["id"], "project": t["project"], "repo": t["repo"], "title": t["title"], "column": t["column"],
                "phase": t.get("phase"), "branch": t["branch"], "pr_url": t.get("pr_url"),
                "session_state": (t.get("session") or {}).get("state")}
               for t in st["tasks"] if not args.get("project") or t["project"] == args["project"]]
        return {"tasks": out}
    if name == "get_task_status":
        tid = args.get("task_id")
        st = api("GET", "/api/state")
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
        return api("POST", f"/api/tasks/{tid}/dispatch", body)
    if name == "get_task_result":
        tid = args.get("task_id")
        if not isinstance(tid, int) or isinstance(tid, bool):
            raise RuntimeError("task_id is required")
        t = api("GET", f"/api/tasks/{tid}")
        return {k: t.get(k) for k in ("id", "title", "phase", "column", "result", "result_at", "done_at", "closed_at", "parent_id", "chain")}
    raise RuntimeError(f"unknown tool {name}")


class Denied(Exception):
    """A tools/call the caller may not make (a scope it lacks): answered as a JSON-RPC error, never run."""


DENIED_CODE = -32001


def negotiate(requested) -> str:
    """The protocol version initialize answers: the client's when this server speaks it, else the newest one it does."""
    return requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL


def rpc(msg: dict, call) -> dict | None:
    """One JSON-RPC request -> its response (None for a notification). `call(name, arguments) -> dict` runs a tool; it raises Denied
    for a refused call (a JSON-RPC error) and any other exception for a failed one (a tool result with isError, the message as text)."""
    method = msg.get("method")
    mid = msg.get("id")
    if mid is None:
        return None
    if method == "initialize":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": negotiate(params.get("protocolVersion")),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "ccboard", "version": SERVER_VERSION}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        try:
            out = call(str(params.get("name") or ""), params.get("arguments"))
        except Denied as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": DENIED_CODE, "message": str(e)}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": str(e)}], "isError": True}}
        return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "isError": False}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
