#!/usr/bin/env python3
"""ccboard as an MCP server (stdio). A dependency-free JSON-RPC shim that calls the board over loopback
with the local hook token. Register once with:
    claude mcp add --scope user ccboard -- /path/to/ccboard/.venv/bin/python /path/to/ccboard/scripts/ccboard_mcp.py
    codex mcp add ccboard --env CCBOARD_URL=http://127.0.0.1:8000 -- /path/to/ccboard/.venv/bin/python /path/to/ccboard/scripts/ccboard_mcp.py
Tools: list_projects, create_task (dispatch false = a Backlog card; after_task_id = a chain step), list_tasks, get_task_status,
dispatch_task (start a Backlog task in a new session or a running one), get_task_result (what a finished task said).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BOARD = os.environ.get("CCBOARD_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN_FILE = os.environ.get("CCBOARD_HOOK_TOKEN_FILE") or str(Path.home() / ".local" / "share" / "ccboard" / "hook-token")
PROTOCOL = "2025-06-18"

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


def token() -> str:
    try:
        return Path(TOKEN_FILE).read_text().strip()
    except OSError:
        return ""


def call_api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BOARD + path, data=data, method=method,
                                 headers={"X-CCBoard-Token": token(), "X-CCBoard": "1", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except Exception:
            msg = str(e)
        raise RuntimeError(f"ccboard {e.code}: {msg}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"ccboard unreachable at {BOARD}: {e.reason}")


def tool_call(name: str, args: dict) -> dict:
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
