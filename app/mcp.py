"""The board's MCP tools: their schemas, the argument checks and the calls they make (issue #13).

The source of the six tools both MCP servers offer: the remote streamable-HTTP endpoint at /mcp (app/mcp_remote.py, a device token per
client, the calls made in-process) and the box's stdio shim scripts/ccboard_mcp.py. The shim keeps a copy of TOOLS and of tool_call,
because on a docker box it runs from a folder that holds scripts/ but not app/; tests/test_mcp_remote.py pins both copies equal.

tool_call(name, args, api) takes `api(method, path, body=None) -> dict`, which raises RuntimeError("ccboard <status>: <message>") for a
refused call: the shim's is HTTP over loopback with the hook token, the remote endpoint's calls the board's route functions directly.
Nothing here imports the web app, so this module is as plain as the shim.

What the tools can never do: set a permission mode, a sandbox, a bypass or a model. The schemas name no such argument and
check_args() refuses any argument a schema does not list (additionalProperties false), so a client cannot slip one in.
"""
from __future__ import annotations

import json

PROTOCOL = "2025-06-18"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26")
SERVER_VERSION = "0.5"

# The longest value each string argument may have. The board cuts a title at 120 and refuses a prompt over 20000; here both are refused
# outright, so a client learns its title was too long instead of seeing it cut.
MAX_LEN = {"project": 64, "repo": 64, "title": 120, "prompt": 20000, "session": 200}

TOOLS = [
    {"name": "list_projects", "description": "List ccboard projects with their repos, branches and live sessions.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "create_task",
     "description": "Create a ccboard task for <project>/<repo> (repo 'root' = the project folder, when it is a git repo). By default Claude (or Codex, with agent codex) starts at once on <prompt> in a fresh git worktree + branch and the answer has the task id and tmux session; with dispatch false the task only goes to the Backlog column for a person to start later. Returns the task id.",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "repo": {"type": "string"},
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
    {"name": "list_tasks", "description": "List ccboard tasks (optionally for one project) with their board column and PR state.",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}, "additionalProperties": False}},
    {"name": "get_task_status", "description": "Status of one ccboard task: column, session state, last message, PR/CI, cost.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"], "additionalProperties": False}},
    {"name": "dispatch_task",
     "description": "Start a Backlog task. By default it gets a new session of its own (its agent, in a fresh git worktree); with session (a tmux name from list_projects) its prompt is pasted into that running session instead, which must be idle or done. Returns the tmux session; limit_warning is set when the Claude usage window is nearly full (the task starts anyway).",
     "inputSchema": {"type": "object", "properties": {
         "task_id": {"type": "integer"},
         "session": {"type": "string", "description": "tmux name of a running session to hand the task to; omit for a new session"},
         "force": {"type": "boolean", "description": "with session: send it even when that session works in another repo (the prompt then names the task's repo)"},
         "queue": {"type": "boolean", "description": "with session: also accept a Claude session that is working (the text is queued behind its current turn)"},
         "auto_close": {"type": "boolean", "description": "close the session when the task's turn ends; not sent = on for a new session, off for a running one"},
         "agent": {"type": "string", "enum": ["claude", "codex"], "description": "new session only: override the agent the task was created for"}},
         "required": ["task_id"], "additionalProperties": False}},
    {"name": "get_task_result",
     "description": "What a finished ccboard task said: its phase (backlog, queued, running, done, failed, cancelled), the full final message of its last turn (result), when it finished, and whether its session was closed after the stop. result is null until the task is done.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"], "additionalProperties": False}},
]
TOOL_NAMES = tuple(t["name"] for t in TOOLS)
_SCHEMAS = {t["name"]: t["inputSchema"] for t in TOOLS}

# Device-token scopes (the remote endpoint): read = the four readers, tasks = create a task or start one in a new session, sessions = hand a
# task to a session that is already running (its prompt is typed into it). A new token gets read and tasks; sessions is an explicit choice.
SCOPES = ("read", "tasks", "sessions")
DEFAULT_SCOPES = ("read", "tasks")


def required_scope(name: str, args: dict | None) -> str | None:
    """The scope a call needs, or None for a tool that does not exist (tool_call refuses it)."""
    if name in ("list_projects", "list_tasks", "get_task_status", "get_task_result"):
        return "read"
    if name == "create_task":
        return "tasks"
    if name == "dispatch_task":
        return "sessions" if isinstance(args, dict) and args.get("session") not in (None, "") else "tasks"
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


def tool_call(name: str, args: dict, api) -> dict:
    """Run one tool through `api`. The same calls, in the same order and with the same bodies, as the stdio shim's tool_call."""
    args = check_args(name, args)
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
