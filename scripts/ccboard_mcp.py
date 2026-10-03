#!/usr/bin/env python3
"""ccboard as an MCP server (stdio). A dependency-free JSON-RPC shim that calls the board over loopback
with the local hook token. Register once with:
    claude mcp add --scope user ccboard -- /path/to/ccboard/.venv/bin/python /path/to/ccboard/scripts/ccboard_mcp.py
Tools: list_projects, create_task (dispatch false = a Backlog card), list_tasks, get_task_status.
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
     "description": "Create a ccboard task for <project>/<repo> (repo 'root' = the project folder, when it is a git repo). By default Claude starts at once on <prompt> in a fresh git worktree + branch and the answer has the task id and tmux session; with dispatch false the task only goes to the Backlog column for a person to start later. Returns the task id.",
     "inputSchema": {"type": "object", "properties": {
         "project": {"type": "string"}, "repo": {"type": "string"},
         "title": {"type": "string", "description": "short title; becomes the branch name"},
         "prompt": {"type": "string", "description": "what Claude should do"},
         "dispatch": {"type": "boolean", "default": True,
                      "description": "true (default): start a Claude session now; false: add to the Backlog without starting anything"}},
         "required": ["project", "repo", "title", "prompt"], "additionalProperties": False}},
    {"name": "list_tasks", "description": "List ccboard tasks (optionally for one project) with their board column and PR state.",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}, "additionalProperties": False}},
    {"name": "get_task_status", "description": "Status of one ccboard task: column, session state, last message, PR/CI, cost.",
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
        if args.get("dispatch", True) is False:          # a backlog card: nothing starts until someone dispatches it
            return call_api("POST", "/api/tasks", {"project": args["project"], "repo": args["repo"], "title": args["title"],
                                                   "prompt": args["prompt"], "when": "later"})
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
