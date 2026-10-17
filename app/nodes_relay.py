"""The one way a hub asks a peer to do something (nodes epic, issue #140, phase P7): a closed table, thin wrappers on the peer, one guard, audit on both sides.

Nothing here starts a thread or makes a request by itself. A board with no paired node has no registry row, so every hub route answers 404 and no peer
wrapper can be reached (a node token needs a pair).

The table: RELAY
  One `Row` per action: name, hub_method, hub_path, peer_method, peer_path, scope, body_model, rate_class, audit_action (plus `guard`, `human_only`,
  `peer_exists`, `target`, `shape`). The table is data. Importing this module puts every peer row into `nodes.NODE_ROUTES` (the closed map the auth middleware
  reads), and `install()` (called once by app/main.py) adds a hub route and a peer wrapper per row, so adding a row adds exactly those and their refusal
  tests (tests/test_nodes_relay.py is generated from the table). Nothing else makes a route reachable for a node token: tests/test_nodes_auth.py walks every
  /api route and expects 403 for all but NODE_ROUTES, and 405 for a listed route with another method.
  Adding a row is a security change: a scope, a guard review (`guard=True` for any row that starts or steers an agent), a refusal test and an audit action,
  in the same pull request.

The hub side: relay(handle, row, params, body, request) checks, in this order, and makes no call when one refuses
  1 who asks     a signed-in person with X-CCBoard: 1, or the local hook token (user `local-token`, the MCP shim); never a node token (that is already a 403 from
                 the middleware, no chaining) and never the legacy hub; a `human_only` row refuses the hook token too. An agent (the hook token = the stdio shim, or a /mcp
                 device token) is let in only on the four rows that say `mcp` (state, task, task_create, task_dispatch; no tool reads the card or the agents), and only after: CCBOARD_MCP_NODES
                 is not off (403 `mcp_off`), its level grants the row's scope (`read`: the reads; `tasks`: also the two writes; else 403 `mcp_level`, nothing sent) and the caller's own
                 scope holds it too (403 `mcp_scope`; the shim holds only what the level grants, never every scope); the audit names it `local-token` or `mcp:<token name>` and the peer
                 records that as the acting user's claim. An agent's body is read with the row's `agent_model`, which has only the fields its tool sends (mcp.NODE_SEND; anything else is
                 a 422 at the route), the hub adds the row's constants (`agent_force`: a lane dispatch, so an agent never gets a row's `extra_scope`), and its answer is the row's reduced
                 `agent_shape`. An unknown handle is told the paired handles (and only the handles).
  2 which node   `local` is 400, text that is no handle 400, a handle the registry does not hold 404 (the target comes from the registry only, never from the
                 request), a registry row with this board's own node id or address 400 (no self-relay), a legacy row (no token) 409
  3 the scope    the pair's granted scope must hold the row's scope: 409 `needs the <scope> scope on <node>`
  4 the status   the hub's reading: `unauthorized` or a pair marked needs_repair is 409 `re-pair`, `unpaired` 409, `offline` (a poll was tried and the last good
                 reading is older than 10 minutes, or there never was one) 503 with the age. A node the hub has not tried yet (right after pairing) is called.
  5 the body     validate() with the row's model (extra="forbid") and, for a row with `guard`, guard_launch(): 422 with the same sentences the peer gives
  6 the call     nodes.PeerClient: the address rule at the call, timeout 10 s, request body at most 256 KB, answer at most 512 KB, no redirect. A 401 is
                 `needs_repair` (409), a 403 a scope message (409), 404 stays 404, 429 stays 429 with its Retry-After, a 5xx or a transport failure is 502, a
                 timeout after the request was sent is 504 `unconfirmed` (never retried: the peer may have done it); a write that got a 5xx or a broken
                 answer carries outcome `unknown` and `check_url` too
  7 the answer   {node: <handle>, age: 0, data: <the peer's JSON, rebuilt from a whitelist by the row's `shape`>}. `age` is how old the answer is (a live relay
                 is 0; an offline refusal carries the age of the last good reading instead). The peer's JSON is nested because the state already has a `node` key.
  Every outcome after the caller is known writes one `out` audit row: time, node, the acting user, action, target, status (ok, failed, refused) and a short reason
  code. Errors are {error, reason, node}: `reason` is a stable word the page turns into a sentence.

The peer side: a wrapper per row, registered under /api/node/ with the row's scope
  The auth middleware has already verified the token, taken the pair's bucket (reads 120 a minute, writes 30, the row's `rate_class`; 429 with Retry-After),
  checked the scope and the size. The wrapper checks again (a node token, the scope), reads the query or the JSON body with the row's model (extra="forbid") and
  the guard, calls an internal function of this board (never an HTTP self-call), and writes one `in` audit row with the caller's node name, the acting user the
  caller reported (`for <login>`, a claim that is recorded and never used for authorization), the action and target, never a prompt or a token. The peer
  trusts nothing the hub checked: it runs every check itself. A signed-in person or the hook token reaches a wrapper and is refused with 403 (they use the
  board's own routes). The card and state rows reuse the routes that already exist; they audit a call only when it carries an acting user (a hub poll does
  not, or the table would fill with 4000 reads a day per pair).

What a peer's reads show (README): `read` shows task titles, session names and screen tails. The pane row is the last 40 lines at most, control characters out,
anything shaped like a node or device token replaced; a person may have typed a secret into a pane, so give `read` only to a board you trust.

guard_launch(fields) -> [refusal]: see its text. Three rows start or steer an agent (issue #141): `task_create` (scope tasks), `task_dispatch` (scope tasks; a
dispatch into a running session also needs `sessions`, `Row.extra_scope`) and `session_open` (scope sessions). The hub runs the guard and the strict model before the
call, the peer runs both again and calls the same internal function the board's own route calls (never an HTTP call to itself). A launch from another node can
never widen permissions: permission_mode default, acceptEdits or plan, no bypass, no args, tools, directories or system prompt, Codex sandbox read-only or
workspace-write with approval on-request. The peer never inherits a default for what the request leaves out: `safe_launch` writes the explicit permissions (Claude
`--permission-mode manual`, Codex `-s workspace-write -a on-request`, no subagent-model override) into the options of the task, the card and the session, the final options go
through `guard_final`, and the line the board is about to type must pass `launch_line_refusal` (`require_remote_launch`, called by main._start_session_row while CALLER is set).
A dispatch into a running session is checked against the TARGET (`session_target_refusal`: one of this board's own sessions, in the task's repo, whose recorded line is inside the
set; a line that says no mode cannot be told and is refused). The task row on the peer carries `origin` {node, user} (the caller's node and the login it reported, cut to a plain
label by `_claim`); a session keeps the same in its flags. A write that timed out or lost its connection after the request was sent is `unconfirmed`: the peer may have done it, nothing is retried.

Steering a session and answering a permission (issue #142, six more rows, every one human only): `prompt`, `keys`, `ack` and `close` (scope sessions), `permissions` (the pending
list, scope permissions, read class) and `permission_answer` (allow or deny, scope permissions). Typing into a session is launching work in it: `prompt` and `keys` are refused
on the PEER unless the target passes `steer_refusal` (session_target_refusal for a session of its own project and repo: one of the peer's own agent sessions whose recorded launch
line is inside the allowed set; a bypass or unreadable mode fails closed). The text is free text (8 KB; a control, format or separator character is a 422, nothing is stripped), a
key comes from the closed list STEER_KEYS (C-c only with confirm true), a decision is allow or deny. The peer refuses a request that names no acting user. Each action writes one
`out` row and one `in` row and stores neither the text, nor a key sequence beyond its name, nor a permission summary. The peer's refusals keep their own status: 409 `busy` (with the
typing refusal's code, retry seconds and state), 409 `answered` or `expired`, 404 `gone`, 413 for text over 8 KB, 429 for the 20 prompts a minute one session takes. No relay row, MCP
tool or scheduled job reaches the board's own permission route (tests/test_nodes_steer.py reads the source to say so).
Who may touch which session, and what a key may answer: EVERY row that names a session runs `owned_session_refusal` on the peer (a ccboard name that is not internal, a row this
board opened, an agent session of a launcher the board writes, in a project and repo whose folder is here); `prompt`, `keys` and a dispatch add `launch_line_refusal`, while `ack`,
`close` and a permission answer run nothing and may touch a session started with wider permissions. While a session is asking a question in its terminal (`asking_reason`: a live
permission request, a state other than working, idle, done, errored or waiting at the idle prompt, a Codex pane dialog, or anything unreadable) `Row.dialog_scope` (`permissions`)
is wanted for `keys` and `prompt` (403 `asking`), and even with it `y`, `n`, 1 to 9 and Enter are refused (409 `busy`, code `asking`): a permission is answered by the permission
row only, once, allow or deny, for a request `permission_refusal` accepts (an ordinary tool of an owned session with a summary; the list shows exactly those).
"""
from __future__ import annotations

import asyncio
import contextvars
import http.client
import json
import logging
import re
import secrets
import shlex
import socket
import threading
import time
import unicodedata
from types import SimpleNamespace
from datetime import datetime
from dataclasses import dataclass, field
from functools import cached_property
from typing import Callable, Literal
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator, model_validator

from . import github, mcp as mcp_tools, nodes, projects, tmux
from .config import settings

log = logging.getLogger("ccboard.nodes.relay")

RELAY_TIMEOUT = 10.0                       # seconds one relayed call may take
CLAIM_TIMEOUT = 90.0                       # load check: 31–39 s through the write queue; over twice that with headroom. Unknown answers are read back, never retried.
CLAIM_PENDING_MAX_AGE = CLAIM_TIMEOUT + 30.0  # allow a final local result after the relay times out
WORK_TIMEOUT = CLAIM_TIMEOUT + 30.0        # running a claimed issue (issue #149) is the claim, a second read of the issue and the start of a task: a worktree, a session, two more writes to the claim
PANE_LINES = 40                            # the most lines of a screen a peer ever sends
PANE_LINE_MAX = 400                        # characters kept of one line
NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
READ, WRITE = "read", "write"
MCP_SCOPES = ("read", "tasks")            # the only scopes an agent's MCP tool ever asks a node for (a prompt needs sessions, a permission answer permissions: never from an agent)
MCP_LEVELS = {"read": ("read",), "tasks": ("read", "tasks")}      # CCBOARD_MCP_NODES: the scopes an agent holds at each level (off holds none; the stdio shim holds exactly these)
MCP_OFF = "this board does not let agents act on other nodes (CCBOARD_MCP_NODES is off)"
MCP_READ_ONLY = "this board lets agents read other nodes but not start tasks on them (CCBOARD_MCP_NODES is read; tasks allows it); nothing was sent"


def mcp_level() -> str:
    """The level CCBOARD_MCP_NODES grants agents: "" (off), "read" or "tasks". Anything else the setting holds is off."""
    return settings.mcp_nodes if settings.mcp_nodes in MCP_LEVELS else ""


# ================================================================ the guard

PERMISSION_MODES = ("default", "acceptEdits", "plan")          # the only permission_mode a request from another node may carry
SESSION_MODES = ("default", "acceptEdits", "plan", "read-only")  # the launcher's `mode` field: never auto, bypass or custom
SANDBOXES = ("read-only", "workspace-write")                   # Codex -s: never danger-full-access
APPROVALS = ("on-request",)                                    # Codex -a: never, on-failure and untrusted widen or break the review
SPELLINGS = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox", "--yolo",
             "danger-full-access", "bypassPermissions")        # the argv and value spellings refused in any string, any case
# Fields that are not part of any remote model, by their name with case and punctuation ignored (allowedTools, allowed_tools and allowed-tools are one).
NEVER_FIELDS = {"args": "args", "extraargs": "args", "argv": "args", "adddirs": "add_dirs", "allowedtools": "allowed_tools",
                "disallowedtools": "disallowed_tools", "appendsystemprompt": "append_system_prompt", "tools": "tools", "mcpconfig": "mcp_config",
                "config": "config", "settings": "settings"}
_SPELL_KEYS = ("bypass", "yolo", "dangerous", "skippermission", "acknowledg")        # a field named like a bypass flag or its acknowledgement
_DASHES = re.compile(r"[\u2010-\u2015\u2212\ufe58\ufe63\uff0d]")
_INVISIBLE = re.compile(r"[\u00ad\u200b-\u200f\u2028-\u202e\u2060-\u2064\ufeff]")


def _squash(text: str) -> str:
    """A string as the spelling check reads it: compatibility form (full-width letters become plain), no invisible or control characters, one kind of dash,
    lower case. A refused spelling is still found with a zero-width space, a soft hyphen, an en dash or capital letters inside it."""
    t = unicodedata.normalize("NFKC", text)
    t = _DASHES.sub("-", _INVISIBLE.sub("", t))
    return "".join(c for c in t if unicodedata.category(c) not in ("Cc", "Cf")).casefold()


_SQUASHED = tuple((s, _squash(s).lstrip("-") if "dangerously" in s else _squash(s)) for s in SPELLINGS)   # the long flags are found with or without their dashes


def _norm_key(k) -> str:
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def _present(v) -> bool:
    return v is not None and v is not False and v != "" and v != [] and v != {} and v != 0


def _agent_names() -> list[str]:
    from . import agents
    return agents.names()


def guard_launch(fields) -> list[str]:
    """The reasons a request from another node that would start or steer an agent is refused, in plain words; [] when it may go on. One function for every
    row that starts or steers an agent, run by the hub before the call (so a person sees it at once) and again by the peer, which never trusts the hub.
    It reads the whole body at every depth (`opts` included), and it never repeats a value back (a prompt is never echoed). It refuses:
      permission_mode      anything but default, acceptEdits or plan (bypassPermissions, dontAsk, auto, a number ...)
      mode                 anything but default, acceptEdits, plan or read-only (the launcher's bypass, auto and custom choices)
      bypass               any field named like a bypass flag or its acknowledgement (bypass, yolo, dangerously_*, skip_permissions, acknowledged) that is set,
                           and anything recover._is_bypass reads as one (the board's own rule for tasks)
      args, add_dirs, allowed_tools, disallowed_tools, append_system_prompt, tools, mcp_config, config, settings   (set: no remote model carries them)
      sandbox              (Codex) anything but read-only or workspace-write
      approval             (Codex) anything but on-request
      spellings            --dangerously-skip-permissions, --allow-dangerously-skip-permissions, --dangerously-bypass-approvals-and-sandbox, --yolo,
                           danger-full-access and bypassPermissions in any string, any case, with zero-width characters and look-alike dashes removed
      agent                anything but an installed adapter (claude, codex); the launcher `shell`, which would run commands, is refused too"""
    if not isinstance(fields, dict):
        return ["the request must be a JSON object"]
    from . import recover
    out: list[str] = []

    def add(msg: str) -> None:
        if msg not in out:
            out.append(msg)

    def spell(text: str) -> None:
        sq = _squash(text)
        for shown, want in _SQUASHED:
            if want in sq:
                add(f"{shown} is not allowed anywhere in a request from another node")

    def visit(d: dict, depth: int) -> None:
        if depth > 6:
            add("the request is nested too deeply")
            return
        for k, v in d.items():
            nk = _norm_key(k)
            if isinstance(k, str):
                spell(k)
            if nk in NEVER_FIELDS and _present(v):
                add(f"{NEVER_FIELDS[nk]} is not part of a request from another node")
            if nk == "permissionmode" and v not in (None, "") and not (isinstance(v, str) and v in PERMISSION_MODES):
                add("permission_mode must be default, acceptEdits or plan")
            elif nk == "mode" and v not in (None, "") and not (isinstance(v, str) and v in SESSION_MODES):
                add("mode must be default, acceptEdits, plan or read-only")
            elif nk == "sandbox" and v not in (None, "") and not (isinstance(v, str) and v in SANDBOXES):
                add("sandbox must be read-only or workspace-write")
            elif nk in ("approval", "approvalpolicy") and v not in (None, "") and not (isinstance(v, str) and v in APPROVALS):
                add("approval must be on-request")
            elif nk == "agent" and v not in (None, ""):
                if not (isinstance(v, str) and v in _agent_names()):
                    add(f"unknown agent: use {' or '.join(_agent_names())}")
            elif nk == "launcher" and isinstance(v, str) and _squash(v).startswith("shell"):
                add("a shell session cannot be started from another node")
            if any(w in nk for w in _SPELL_KEYS) and _present(v):
                add("a bypass flag or acknowledgement is not allowed in a request from another node")
            if isinstance(k, str) and recover._is_bypass(k, v):
                add("a bypass flag or acknowledgement is not allowed in a request from another node")
            scan(v, depth + 1)

    def scan(v, depth: int) -> None:
        if isinstance(v, str):
            spell(v)
        elif isinstance(v, dict):
            visit(v, depth)
        elif isinstance(v, (list, tuple)):
            if depth > 6:
                add("the request is nested too deeply")
                return
            for x in v:
                scan(x, depth + 1)

    visit(fields, 0)
    return out


# ================================================================ the launch line: what a remote launch runs with, and what a session already runs with

SAFE_CLAUDE = "manual"                                         # the adapter's ask-as-usual mode (the CLI word `default`); sent explicitly, so settings.json cannot choose
CLAUDE_MODE_WORDS = ("manual", "default", "acceptEdits", "plan")          # the --permission-mode values of a line that is inside the remote-allowed set
WIDE_SESSION = "that session runs with wider permissions than another node may use, or its mode cannot be read; start it from its own board"


def safe_launch(agent: str | None, mode=None, permission_mode=None, sandbox=None, approval=None) -> dict:
    """The explicit permissions of a launch for another node: what the body asked for (already inside the allowed set), else the safe constant. Never what this
    board's own defaults say: Claude Code reads its settings.json and Codex its config.toml when a line carries no flag, so a launch always carries them.
      claude  -> {"permission_mode": "manual" | "acceptEdits" | "plan"}
      codex   -> {"sandbox": "read-only" | "workspace-write", "approval": "on-request"}   (default and acceptEdits: workspace-write; plan and read-only: read-only)
    Raises BadRequest for a word outside the allowed set (the guard has refused it before; this is the second look)."""
    word = mode if mode not in (None, "") else permission_mode
    if word not in (None, "", "default", "manual", "acceptEdits", "plan", "read-only"):
        raise projects.BadRequest("permission mode: default, acceptEdits or plan")
    if (agent or "claude") == "claude":
        if word in ("read-only", "plan"):
            return {"permission_mode": "plan"}
        return {"permission_mode": "acceptEdits" if word == "acceptEdits" else SAFE_CLAUDE}
    sb = "read-only" if word in ("read-only", "plan") else "workspace-write"
    if sandbox not in (None, ""):
        if sandbox not in SANDBOXES:
            raise projects.BadRequest("sandbox must be read-only or workspace-write")
        sb = sandbox
    if approval not in (None, "") and approval not in APPROVALS:
        raise projects.BadRequest("approval must be on-request")
    return {"sandbox": sb, "approval": "on-request"}


FINAL_KEYS = ("agent", "model", "effort", "reasoning_effort", "permission_mode", "subagent_model", "subagent_force", "opts", "sandbox", "approval", "name", "auto_close")
FINAL_OPTS = ("sandbox", "approval")


def guard_final(fields: dict) -> list[str]:
    """The options a launch ends up with, checked by an ALLOW list on top of guard_launch: only the keys of FINAL_KEYS may be set (a copy; `manual`, the adapter's word
    for default, is read as default), `opts` holds a sandbox and an approval and nothing else, and each string has the rule of its kind (check_field). Empty values are not options."""
    f = {k: v for k, v in fields.items() if v not in (None, "", [], {}, False)}
    if f.get("permission_mode") == "manual":
        f["permission_mode"] = "default"
    out = guard_launch(f)

    def add(msg: str) -> None:
        if msg not in out:
            out.append(msg)

    for k, v in f.items():
        if k not in FINAL_KEYS:
            add(f"{k} is not part of a request from another node")
        elif k == "opts":
            if not isinstance(v, dict) or any(x not in FINAL_OPTS and _present(y) for x, y in v.items()):
                add("opts may hold a sandbox and an approval only")
            else:
                for x, y in v.items():
                    try:
                        check_field(x, y) if _present(y) else None
                    except ValueError:
                        add(f"opts {x} is not allowed")
        elif k in ("agent", "model", "effort", "reasoning_effort", "permission_mode", "sandbox", "approval", "name"):
            try:
                check_field("effort" if k == "reasoning_effort" else k, v)
            except ValueError:
                add(f"{k} is not allowed in a request from another node")
        elif k == "subagent_model":
            try:
                check_field("model", v)
            except ValueError:
                add("subagent_model is not allowed in a request from another node")
    return out


CARD_KEYS = ("model", "effort", "reasoning_effort", "permission_mode", "subagent_model", "subagent_force", "auto_close", "opts")


def card_refusal(task: dict, spec: dict, typed: bool = False) -> str | None:
    """Why a stored card may not be started from another node (None: it may), by an allow list: its title and prompt are text without control, format or line-separator
    characters, its spec holds only the keys of CARD_KEYS, and each string in it has the rule of its kind. The same check_field the hub's models use. `typed`: the prompt
    may not start like a terminal command (it is typed into a running session, or is a launch argument Claude Code reads as a slash command): "command"."""
    try:
        free_text(task.get("title") or "", TASK_TITLE_IN)
        free_text(task.get("prompt") or "", TASK_PROMPT_IN)
    except ValueError:
        return "text"
    if typed and starts_like_a_command(task.get("prompt") or ""):
        return "command"
    for k, v in (spec or {}).items():
        if not _present(v):
            continue
        if k not in CARD_KEYS:
            return "options"
        try:
            if k in ("model", "effort", "reasoning_effort"):
                check_field("effort" if k == "reasoning_effort" else k, v)
            elif k == "permission_mode":
                if v not in CLAUDE_MODE_WORDS:
                    return "options"
            elif k == "subagent_model":
                check_field("model", v)
            elif k == "opts":
                if not isinstance(v, dict):
                    return "options"
                for x, y in v.items():
                    if _present(y):
                        if x not in FINAL_OPTS:
                            return "options"
                        check_field(x, y)
        except ValueError:
            return "options"
    return None


# The allow list. A typed launch line is accepted only when it is exactly the shape the board's own adapters write (app/agents/claude.py and codex.py launch_plan,
# tasks.build_command): shlex.join of its tokens gives the line back, an optional `env` prefix of the two names the board sets itself, the agent, then flags from a
# table of known-safe flags with a value rule each, then at most `-- <prompt>`. A flag that is not in the table is refused, so a flag an adapter adds later fails
# closed until it is added here (tests/test_nodes_guard.py generates the adapters' lines and checks that every one passes).
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_WTREE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}(?:\[1m\])?")
_CODEX_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,79}")
_CODEX_REASONING_C = re.compile(r'model_reasoning_effort="[a-z][a-z0-9-]{1,19}"')


def _ok_model(v: str) -> bool:
    return bool(_MODEL_ID.fullmatch(v))


def _ok_effort(v: str) -> bool:
    return v in ("low", "medium", "high", "xhigh", "max", "ultracode")


def _ok_fallbacks(v: str) -> bool:
    items = v.split(",")
    return 1 <= len(items) <= 3 and all(_ok_model(x) for x in items)


def _ok_session_name(v: str) -> bool:
    return len(v) <= 63 and tmux.valid_name(v)


# flag -> (kind, value rule): kind val = takes one value, bool = takes none, optuuid = an optional uuid, once = may appear once only (all flags may)
_CLAUDE_FLAGS = {
    "--session-id": ("val", lambda v: bool(_UUID.fullmatch(v))),
    "--resume": ("optuuid", None),
    "--continue": ("bool", None),
    "--fork-session": ("bool", None),
    "--name": ("val", _ok_session_name),
    "--model": ("val", _ok_model),
    "--effort": ("val", _ok_effort),
    "--permission-mode": ("val", lambda v: v in CLAUDE_MODE_WORDS),
    "--fallback-model": ("val", _ok_fallbacks),
    "--worktree": ("val", lambda v: bool(_WTREE.fullmatch(v)) and ".." not in v),
}
_CODEX_FLAGS = {
    "--no-daemon": ("bool", None),
    "--no-alt-screen": ("bool", None),
    "--dangerously-bypass-hook-trust": ("hooktrust", None),          # only while the board's own CCBOARD_CODEX_HOOK_TRUST=bypass writes it on every line
    "-m": ("val", lambda v: bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}", v))),
    "-s": ("val", lambda v: v in SANDBOXES),
    "-a": ("val", lambda v: v in APPROVALS),
    "-c": ("val", lambda v: v == "check_for_update_on_startup=false" or bool(_CODEX_REASONING_C.fullmatch(v))),
    "--last": ("bool", None),
}
_ENV_NAMES = {"CLAUDE_CODE_SUBAGENT_MODEL": lambda v: v in ("inherit", "haiku", "sonnet", "opus") or _ok_model(v),
              "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": lambda v: v == "1"}


def _line_tokens(cmd):
    """The tokens of a typed line when it is in the board's own canonical form (shlex.join of its tokens is the line itself, so there is no unquoted `;`, `&&`, `|`,
    backtick, `$()`, redirection or odd spacing), else None."""
    if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 400000:
        return None
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return None
    return toks if toks and shlex.join(toks) == cmd.strip() else None


def _parse_line(agent: str, cmd):
    """-> (flags: {name: [values]}, why). `why` is None when the line is entirely inside the table, else the plain reason (and flags is None)."""
    toks = _line_tokens(cmd)
    if toks is None:
        return None, "its launch line is not in the form the board writes"
    i, seen_env = 0, set()
    if toks[0] == "env":
        i = 1
        while i < len(toks) and "=" in toks[i] and not toks[i].startswith("-"):
            name, _, val = toks[i].partition("=")
            rule = _ENV_NAMES.get(name)
            if agent != "claude" or rule is None or name in seen_env or not rule(val):
                return None, f"{name} is set on its launch line"
            seen_env.add(name)
            i += 1
        if not seen_env:
            return None, "its launch line starts with an env prefix the board does not write"
    if i >= len(toks) or toks[i] != agent:
        return None, "its launch line does not start with the agent the board starts"
    rest = toks[i + 1:]
    prompt: list[str] = []
    if "--" in rest:
        k = rest.index("--")
        rest, prompt = rest[:k], rest[k + 1:]
        if len(prompt) != 1:
            return None, "its launch line holds more than one prompt"
    table = _CLAUDE_FLAGS if agent == "claude" else _CODEX_FLAGS
    flags: dict[str, list[str]] = {}
    sub = None
    if agent == "codex" and rest and rest[0] in ("resume", "fork"):
        sub, rest = rest[0], rest[1:]
    j, rid = 0, None
    while j < len(rest):
        t = rest[j]
        if not t.startswith("-"):
            if sub and rid is None and j == len(rest) - 1 and (_UUID.fullmatch(t) or _CODEX_NAME.fullmatch(t)):
                rid = t
                j += 1
                continue
            return None, "its launch line holds an argument the board does not write"
        name, eq, val = t.partition("=") if t.startswith("--") else (t, "", "")
        if name not in table:
            return None, f"{name} is on its launch line"
        kind, rule = table[name]
        if kind == "hooktrust":
            from .config import settings
            if settings.codex_hook_trust != "bypass":
                return None, f"{name} is on its launch line"
            kind = "bool"
        if name == "--last" and sub != "resume":
            return None, "--last is on its launch line"
        if kind == "bool":
            if eq:
                return None, f"{name} takes no value"
            flags.setdefault(name, []).append("")
        elif kind == "optuuid":
            if not eq and j + 1 < len(rest) and _UUID.fullmatch(rest[j + 1]):
                val, j = rest[j + 1], j + 1
            elif not eq:
                val = ""
            if val and not _UUID.fullmatch(val):
                return None, f"{name} holds something that is not a session id"
            flags.setdefault(name, []).append(val)
        else:
            if not eq:
                if j + 1 >= len(rest):
                    return None, f"{name} has no value"
                val, j = rest[j + 1], j + 1
            if not rule(val):
                return None, f"{name} holds a value the board does not write"
            flags.setdefault(name, []).append(val)
        j += 1
    for name, vals in flags.items():
        if len(vals) > 1 and not (agent == "codex" and name == "-c"):
            return None, f"{name} is on its launch line twice"
    if agent == "codex" and len(flags.get("-c", [])) != len(set(flags.get("-c", []))):
        return None, "-c is repeated on its launch line"
    return flags, None


def launch_line_refusal(agent: str | None, cmd, opts=None) -> str | None:
    """None when the typed launch line of an agent is exactly a line the board writes and runs with a permission mode inside what another node may use, else the plain
    reason. ONE function for the final plan of a remote launch and for the target session of a dispatch, so they cannot drift. The line must tokenise, be the
    canonical shlex.join of its tokens, start with the agent (after at most the `env` of the two names the board sets itself), and hold only flags of the tables above
    with values of the shapes the board writes, each once. It must also SAY its mode:
      claude   --permission-mode manual, default, acceptEdits or plan
      codex    -s read-only|workspace-write and -a on-request
    A line that leaves the mode to the CLI's own settings cannot be told and is refused. Only the part before the prompt (`--`) is read; the prompt is one token."""
    agent = agent or "claude"
    if agent not in ("claude", "codex"):
        return "its launch line cannot be read"
    flags, why = _parse_line(agent, cmd)
    if why:
        return why
    toks = _line_tokens(cmd) or []
    sq = _squash(" ".join(toks[:toks.index("--")] if "--" in toks else toks))
    for shown, want in _SQUASHED:
        if want in sq:
            return f"{shown} is on its launch line"
    if isinstance(opts, dict):
        pm = opts.get("permission_mode")
        if pm not in (None, "") and pm not in (CLAUDE_MODE_WORDS if agent == "claude" else PERMISSION_MODES):
            return "its stored permission mode is outside the allowed set"
    if agent == "claude":
        return None if flags.get("--permission-mode") else "its launch line sets no permission mode"
    if not flags.get("-s") or not flags.get("-a"):
        return "its launch line sets no sandbox or no approval policy"
    return None


def require_remote_launch(agent: str | None, cmd_line, opts=None) -> None:
    """Called by the board's session start while a request of a paired node is being served (CALLER is set): the typed line is the FINAL plan of the launch, and it
    must say its permissions and stay inside the allowed set, whatever the body, the card or the board's defaults said. Raises BadRequest before any session exists."""
    why = launch_line_refusal(agent, cmd_line, opts)
    if why:
        raise projects.BadRequest(f"this launch would not stay inside what another node may use ({why}); nothing was started")


def owned_session_refusal(db, name: str) -> str | None:
    """Why the session `name` is not one this board's own agent sessions another node may act on (None: it is). ONE function for every relay row that names a session
    (prompt, keys, ack, close, a dispatch into a session) and for the session of a permission request: a ccboard name that is not internal (the login session), a row
    this board opened (not a tmux session the owner started outside the board), an agent of this board started by a launcher it writes itself (recover.RECOVERABLE: not
    a shell, not a clone), in a project and repo whose folder is on this board. What it does NOT ask is the launch line: typing into a session also wants
    launch_line_refusal (session_target_refusal), while an ack, a close and a permission answer run nothing and may touch a session started with wider permissions."""
    try:
        check_field("session", name)
        sp, sr, _ = tmux.split_name(name)
    except ValueError:
        return "not a ccboard session name"
    if tmux.is_internal(name):
        return "not a session another node may use"
    row = db.open_rows().get(name)
    if not row:
        return "that is not a session this board started"
    agent = row.get("agent") or "claude"
    from . import recover                                    # the launchers the board itself writes for an agent session (an allow list: anything else is not one)
    if agent not in _agent_names() or row.get("launcher") not in recover.RECOVERABLE:
        return "that is not an agent session this board started"
    if (row.get("project"), row.get("repo")) != (sp, sr):
        return "this session works in another repo"
    try:
        if not projects.repo_path(sp, sr).is_dir():
            return "the project or repo of that session is not on this board"
    except (ValueError, projects.BadRequest, projects.NotFound):
        return "the project or repo of that session is not on this board"
    return None


def session_target_refusal(db, task: dict, name: str) -> str | None:
    """Why a task of another node may not be typed into the running session `name` (None: it may go on to the board's own checks). The target must be one of this
    board's own sessions (a row with a ccboard name; not internal, not a tmux session the board did not start) in the task's own project and repo, and its recorded
    launch line must be inside the allowed set (launch_line_refusal). A session that does not exist is left to the board (404)."""
    try:
        check_field("session", name)
        sp, sr, _ = tmux.split_name(name)
    except ValueError:
        return "not a ccboard session name"
    if tmux.is_internal(name):
        return "not a session another node may use"
    if not tmux.has_session(name):
        return None
    why = owned_session_refusal(db, name)
    if why:
        return why
    row = db.open_rows().get(name) or {}
    if (sp, sr) != (task.get("project"), task.get("repo")):
        return "this session works in another repo"
    return WIDE_SESSION if launch_line_refusal(row.get("agent") or "claude", row.get("cmd"), row.get("opts")) else None


# ================================================================ the table

class _Strict(BaseModel):
    """Every body model of a row: a field nobody asked for is an error, so no route can carry a permission, a sandbox or an argument by accident."""
    model_config = ConfigDict(extra="forbid")


class NoFields(_Strict):
    """The read rows that take nothing."""


class PaneQuery(_Strict):
    lines: int = Field(PANE_LINES, ge=1, le=PANE_LINES)

    @field_validator("lines", mode="before")
    @classmethod
    def _plain_digits(cls, v):
        """A query value is text: only one or two ASCII digits (no sign, space, underscore, hex, other-script digits, float or bool)."""
        if isinstance(v, bool) or not isinstance(v, (int, str)) or (isinstance(v, str) and not re.fullmatch(r"[0-9]{1,2}", v)):
            raise ValueError("lines is a whole number from 1 to 40")
        return v


_TID = re.compile(r"[0-9]{1,12}")


def _valid_param(name: str, value) -> bool:
    if not isinstance(value, str):
        return False
    if name in ("tid", "pid"):
        return bool(_TID.fullmatch(value))
    if name == "decision":
        return value in DECISIONS
    if name == "name":
        try:
            check_field("session", value)
        except ValueError:
            return False
        return True
    return False


def _pattern(path: str) -> re.Pattern:
    return re.compile(re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", re.escape(path)))


@dataclass(frozen=True)
class Row:
    name: str
    hub_method: str
    hub_path: str                       # /api/nodes/{handle}/...
    peer_method: str
    peer_path: str                      # /api/node/...
    scope: str                          # read | tasks | sessions | permissions
    body_model: type[BaseModel]
    rate_class: str                     # read | write
    audit_action: str
    guard: bool = False                 # starts or steers an agent: guard_launch() runs on the hub and again on the peer
    human_only: bool = False            # a signed-in person only: the hook token (the MCP shim) is refused too
    peer_exists: bool = False           # the peer path is a route that already exists (card, state); no new wrapper is made
    target: Callable[[dict], str] = field(default=lambda p: "", compare=False)
    shape: Callable | None = field(default=None, compare=False)   # rebuilds the peer's answer on the hub from a whitelist (raises Bad)
    answer_max: int = 64 * 1024                                   # bytes of the peer's answer the hub accepts for this row (nodes.RESP_MAX is the transport's limit)
    guard_skip: tuple = ()                                        # top-level fields the guard does not read because the row's model checks them itself (the dispatch `mode`)
    extra_scope: Callable[[dict], str | None] | None = field(default=None, compare=False)   # the scope the validated body needs besides `scope` (dispatch into a session: sessions)
    body_target: Callable[[dict, dict], str] | None = field(default=None, compare=False)    # the audit target from (safe path parameters, validated body): a task title, cut, when the path does not say enough
    acting: bool = False                # the peer refuses a request that names no acting user (a person's act, relayed by the hub)
    timeout: float = RELAY_TIMEOUT      # seconds the hub waits for the peer's answer to this row (a write is never retried after it)
    big_field: str | None = None        # a string field whose length over its cap is a 413, not a 422 (the text of a prompt)
    per_session: bool = False           # nodes.PROMPT_RATE a minute into one session, counted on the hub and again on the peer
    dialog_scope: str | None = field(default=None, compare=False)     # the scope the PEER also wants while the target session is asking a question in its terminal (a permission, a dialog,
                                                                      # a state it cannot read): a key or a line typed then answers it, which only `permissions` may do (peer_keys, peer_prompt)
    fill: Callable[[dict, object], dict] | None = field(default=None, compare=False)       # (validated body, the hub's database) -> the body that is sent: values the hub holds itself (an epic's stored settings), never the page's
    mcp: bool = False                   # an agent's MCP tool may use this row (issue #152): the stdio shim with the hook token, or a /mcp device token. Off by default, and only when
                                        # CCBOARD_MCP_NODES grants the row's scope (read: the reads; tasks: also the two writes); an agent never gets `extra_scope` (a dispatch into a running
                                        # session), and a row that is a person's act, types into a session or answers a permission can never be mcp (see __post_init__)
    agent_model: type[BaseModel] | None = field(default=None, compare=False)    # what an AGENT may send on an mcp row: exactly the fields its tool sends (mcp.NODE_SEND), extra="forbid"
    agent_shape: Callable | None = field(default=None, compare=False)           # what an AGENT receives from an mcp row: a reduced answer built field by field for what the tools return
    agent_force: dict = field(default_factory=dict, compare=False)              # the constants the hub adds to an agent's validated body (a lane dispatch)

    def __post_init__(self):
        if self.mcp:
            why = ("a person's act" if self.human_only else "a person's act by name" if self.acting else "a prompt into one session" if self.per_session
                   else "free text beyond a task" if self.big_field else "a dialog answer" if self.dialog_scope else "a scope an agent never holds" if self.scope not in MCP_SCOPES
                   else "a write that starts nothing guarded" if self.rate_class == WRITE and not self.guard else None)
            if why:
                raise ValueError(f"relay row {self.name} cannot be opened to an agent: {why}")
            if self.agent_model is None or self.agent_shape is None:
                raise ValueError(f"relay row {self.name} cannot be opened to an agent: it needs its own agent body and agent shape")

    @cached_property
    def params(self) -> tuple[str, ...]:
        return tuple(p for p in re.findall(r"\{([a-z_]+)\}", self.hub_path) if p != "handle")

    @cached_property
    def peer_re(self) -> re.Pattern:
        return _pattern(self.peer_path)


def _shape_card(body, reg):
    from . import nodes_hub
    return nodes_hub.clean_card(body, reg)


def _shape_state(body, reg):
    from . import nodes_hub
    return nodes_hub.clean_state(body, reg)


def _shape_any(body, reg):
    from . import nodes_hub
    if not isinstance(body, dict):
        raise nodes_hub.Bad("not an object")
    return nodes_hub.scrub(body, 8)


def _strs(v, n: int = 60, cap: int = 60) -> list[str]:
    from . import node_state
    return [t for t in (node_state.clean(x, n) for x in (v[:cap] if isinstance(v, list) else []) if isinstance(x, str)) if t]


def _shape_agents(body, reg):
    """The peer's agents rebuilt field by field: a launcher control keeps its key, label, kind, choices, default, help (400 characters), group and `when`."""
    from . import node_state, nodes_hub
    c = node_state.clean
    if not isinstance(body, dict) or not isinstance(body.get("agents"), list):
        raise nodes_hub.Bad("not an agents list")
    out = []
    for a in body["agents"][:8]:
        if not isinstance(a, dict):
            continue
        opts = []
        for o in (a.get("options") if isinstance(a.get("options"), list) else [])[:80]:
            if not isinstance(o, dict) or not isinstance(o.get("key"), str):
                continue
            dflt = o.get("default")
            opts.append({"key": c(o["key"], 60), "label": c(o.get("label"), 80), "kind": c(o.get("kind"), 20), "choices": _strs(o["choices"]) if isinstance(o.get("choices"), list) else None,
                         "default": dflt if isinstance(dflt, (bool, int, float)) or dflt is None else c(dflt, 80), "help": c(o.get("help"), 400),
                         "group": c(o.get("group"), 20), "danger": bool(o.get("danger")), "when": nodes_hub.scrub(o.get("when"), 3) if isinstance(o.get("when"), dict) else None})
        rbm = a.get("reasoning_by_model") if isinstance(a.get("reasoning_by_model"), dict) else {}
        out.append({"name": c(a.get("name"), 20), "label": c(a.get("label"), 40), "glyph": c(a.get("glyph"), 8), "installed": bool(a.get("installed")),
                    "version": c(a.get("version"), 40), "logged_in": bool(a.get("logged_in")), "hooks": bool(a.get("hooks")), "options": opts,
                    "permission_modes": _strs(a.get("permission_modes"), 40, 12), "efforts": _strs(a.get("efforts"), 40, 12), "models": _strs(a.get("models"), 80, 80),
                    "reasoning_by_model": {c(k, 80): _strs(v, 40, 12) for k, v in list(rbm.items())[:80] if isinstance(k, str) and c(k, 80)}})
    return {"agents": out}


def _shape_task(body, reg):
    """One task's detail rebuilt field by field: a key the peer adds is dropped, a string is cut and cleaned."""
    from . import nodes_hub
    if not isinstance(body, dict):
        raise nodes_hub.Bad("not an object")
    s, i = nodes_hub._s, nodes_hub._i
    return {"id": i(body.get("id")), "title": s(body.get("title")), "phase": s(body.get("phase"), 20), "agent": s(body.get("agent"), 20), "project": s(body.get("project")),
            "repo": s(body.get("repo")), "branch": s(body.get("branch")), "tmux": s(body.get("tmux")), "issue_ref": s(body.get("issue_ref"), 20),
            "updated_at": s(body.get("updated_at"), 40), "slug": s(body.get("slug"), 80), "mode": s(body.get("mode"), 20), "base": s(body.get("base")),
            "created_at": s(body.get("created_at"), 40), "assigned_at": s(body.get("assigned_at"), 40), "done_at": s(body.get("done_at"), 40),
            **({"failure_reason": reason} if s(body.get("phase"), 20) == "failed" and (reason := nodes_hub._trust_reason(body.get("failure_reason"), body.get("project"), body.get("repo"))) else {}),
            "pr_number": i(body.get("pr_number")), "pr_state": s(body.get("pr_state"), 20), "has_result": bool(body.get("has_result"))}


def _shape_pane(body, reg):
    from . import nodes_hub
    if not isinstance(body, dict) or not isinstance(body.get("lines"), list):
        raise nodes_hub.Bad("not a screen tail")
    lines = screen_lines_from(body["lines"], PANE_LINES)                  # the caps first, then the hub redacts again: the peer's own pass is not trusted
    return {"name": nodes_hub.node_state.clean(body.get("name"), 120), "lines": lines, "cap": PANE_LINES}


# ---- the write rows (issue #141): strict models with exactly the fields the launcher sends

TASK_TITLE_IN = 300                                                          # the peer cuts a stored title at 120; the model only keeps a runaway out
TASK_PROMPT_IN = 20000                                                       # main.TASK_PROMPT_MAX
_ISSUE_REF = re.compile(r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/((?![-.])[A-Za-z0-9_.-]{1,100})#([0-9]{1,9})")      # a GitHub owner, a repo name without a leading dot or dash
_REPO_SLUG = re.compile(r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/((?![-.])[A-Za-z0-9_.-]{1,100})")
_EPIC_NUM = re.compile(r"[1-9][0-9]{0,8}")
_EPIC_REF = re.compile(r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/((?![-.])[A-Za-z0-9_.-]{1,100})#([1-9][0-9]{0,8})")


# ---- typed text: ONE rule for every string a remote caller can send. The hub's models, the peer wrapper and the check of a stored card all call check_field(), so the
# paths cannot differ. A string that reaches a typed command line, tmux send-keys, a path or a branch name has an anchored pattern and a length cap (an enum where it
# is one); only a title and a prompt are free text, and those refuse every control, format and line-separator character (U+2028/2029, bidi controls, zero-width
# characters ...), looked at as sent AND after NFKC, so a look-alike cannot carry one in. A patterned value must be NFKC-stable, so full-width letters are refused.
_FREE_BAD = ("Cc", "Cf", "Zl", "Zp")
FREE_TEXT_MESSAGE = "no control characters, format characters or line separators (tab and newline are text)"
_RX = {
    "agent": re.compile(r"[a-z][a-z0-9_-]{0,19}"),
    "model": re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}(?:\[1m\])?"),
    "effort": re.compile(r"[a-z][a-z0-9-]{1,19}"),
}
STEER_MAX = 8192                                                             # the text a person sends into a remote session: 8 KB
STEER_KEYS = ("Enter", "Escape", "Up", "Down", "Tab", "y", "n", "1", "2", "3", "4", "5", "6", "7", "8", "9", "C-c")      # the closed list; C-c only with confirm true
CONFIRMED_KEYS = ("C-c",)
LITERAL_KEYS = ("y", "n", "1", "2", "3", "4", "5", "6", "7", "8", "9")       # typed as one character (tmux's own key names do not include them)
DECISIONS = ("allow", "deny")                                                # never `tui`, `always` or a rule
_ENUMS = {"permission_mode": PERMISSION_MODES, "mode": SESSION_MODES, "sandbox": SANDBOXES, "approval": APPROVALS, "key": STEER_KEYS, "decision": DECISIONS}
FIELD_KINDS = ("agent", "model", "effort", "name", "session", "issue_ref", "repo_slug", "epic_ref", "permission_mode", "mode", "sandbox", "approval", "title", "prompt", "text", "typed_text", "typed_prompt", "key", "decision")
_CAPS = {"title": TASK_TITLE_IN, "prompt": TASK_PROMPT_IN, "text": STEER_MAX, "typed_text": STEER_MAX, "typed_prompt": TASK_PROMPT_IN}
# Text that is TYPED into a running agent session (a steer prompt; the prompt of a stored card handed to a running session) is read by the agent's own input line, where a
# first character of `/` is a slash command (model, permission mode, settings ...), `!` runs a shell line in bash mode without a permission prompt and `#` writes to the
# memory files (Codex reads `/` and `!` the same way). The board pastes it as ONE bracketed paste (tmux.paste_text), so the first character of the whole text is the one
# that counts, not the first of each line. `@file` is a mention, not a command, and passes. The first prompt of a NEW task (`typed_prompt`, the task prompt cap) is an argv
# argument after `--`, but Claude Code runs a leading `/` of a launch argument as a slash command too, so it has the same rule and fails closed.
TYPED_COMMAND_CHARS = "/!#"
TYPED_COMMAND_MESSAGE = "a remote prompt cannot start with / ! or # (those are commands in the terminal); start it from its own board"


def starts_like_a_command(v: str) -> bool:
    """Does this text, as the terminal will read it, start with `/`, `!` or `#`? Looked at after NFKC (full-width forms) and past any leading whitespace or newlines."""
    return unicodedata.normalize("NFKC", v).lstrip()[:1] in tuple(TYPED_COMMAND_CHARS)


def free_text(v, cap: int):
    """A title or a prompt: text with tab and newline (CRLF is a newline), no control, format or line-separator character, at most `cap` characters. ValueError."""
    if not isinstance(v, str):
        raise ValueError("must be text")
    v = v.replace("\r\n", "\n")
    if len(v) > cap:
        raise ValueError(f"at most {cap} characters")
    for form in (v, unicodedata.normalize("NFKC", v)):
        if any(unicodedata.category(c) in _FREE_BAD and c not in "\t\n" for c in form):
            raise ValueError(FREE_TEXT_MESSAGE)
    return v


def check_field(kind: str, v):
    """Validate one string field of a remote request by its kind (FIELD_KINDS) and return it unchanged; ValueError says what is wrong and never repeats the value."""
    if kind in ("title", "prompt", "text", "typed_text", "typed_prompt"):
        v = free_text(v, _CAPS[kind])
        if kind in ("typed_text", "typed_prompt") and starts_like_a_command(v):
            raise ValueError(TYPED_COMMAND_MESSAGE)
        return v
    if not isinstance(v, str) or unicodedata.normalize("NFKC", v) != v:
        raise ValueError("use plain ASCII letters, digits and the usual marks")
    if kind in _ENUMS:
        if v not in _ENUMS[kind]:
            raise ValueError("one of " + ", ".join(_ENUMS[kind]))
    elif kind == "name":
        if len(v) > 63 or not tmux.valid_name(v):
            raise ValueError("use letters, digits, '-' or '_' (no '--')")
    elif kind == "session":
        try:
            if len(v) > 190:
                raise ValueError
            tmux.split_name(v)
        except ValueError:
            raise ValueError("not a ccboard session name") from None
    elif kind == "issue_ref":
        if len(v) > 250 or not _ISSUE_REF.fullmatch(v):
            raise ValueError("an issue reference is owner/name#number")
    elif kind == "repo_slug":
        m = _REPO_SLUG.fullmatch(v) if len(v) <= 140 else None
        if not m or not github.valid_slug(*m.groups()):
            raise ValueError("a repository is owner/name")
    elif kind == "epic_ref":
        m = _EPIC_REF.fullmatch(v) if len(v) <= 160 else None
        if not (len(v) <= 9 and _EPIC_NUM.fullmatch(v)) and not (m and github.valid_slug(m.group(1), m.group(2))):
            raise ValueError("an epic is its issue number, or owner/name#number")
    elif kind in _RX:
        if not _RX[kind].fullmatch(v):
            raise ValueError({"agent": "an agent name", "model": "a model id", "effort": "an effort level"}[kind] + " (letters, digits, '.', '_', ':', '-'; no leading dash)")
    else:
        raise ValueError("unknown field")
    return v


def stream_names_error(names) -> str | None:
    """Why the `names` of a stream request from or to another node is refused (None: every name is a ccboard session name exactly as written, with no space, control
    character or look-alike around it, and there are at most 20). The same check_field a launch uses for a session."""
    if not isinstance(names, str):
        return "names must be text"
    parts = names.split(",")
    if len(parts) > 20:
        return "names: at most 20 sessions per stream"
    for raw in parts:
        try:
            check_field("session", raw)
        except ValueError:
            return "names: one of them is not a ccboard session name"
    return None


def _plain_name(v):
    return check_field("name", v)


class _Launch(_Strict):
    """The launch choices a node may send, alone (never the flags, tools, directories or prompts of the board's own launch bodies)."""
    agent: str | None = Field(None, max_length=20)
    model: str | None = Field(None, max_length=80)
    effort: str | None = Field(None, max_length=40)
    reasoning_effort: str | None = Field(None, max_length=40)

    @field_validator("agent", "model", "effort", "reasoning_effort")
    @classmethod
    def _launch_fields(cls, v, info):
        return None if v is None else check_field("effort" if info.field_name == "reasoning_effort" else info.field_name, v)


class TaskCreateBody(_Launch):
    project: str
    repo: str
    title: str = Field(min_length=1, max_length=TASK_TITLE_IN)
    prompt: str = Field(min_length=1, max_length=TASK_PROMPT_IN)
    when: Literal["now", "later"] = "now"
    auto_close: bool | None = None
    issue_ref: str | None = Field(None, max_length=250)

    @model_validator(mode="before")
    @classmethod
    def _no_chain(cls, data):
        if isinstance(data, dict) and "after_task_id" in data:
            raise ValueError("chains stay inside one node")
        return data

    @field_validator("project", "repo")
    @classmethod
    def _names(cls, v):
        return check_field("name", v)

    @field_validator("title", "prompt")
    @classmethod
    def _typed_text(cls, v, info):
        """The prompt is typed into a shell line of the peer (tmux send-keys -l) and the title names a card: see free_text. The prompt also may not start like a terminal command
        (`typed_prompt`): a launch argument that starts with `/` is a slash command in Claude Code."""
        return check_field("typed_prompt" if info.field_name == "prompt" else info.field_name, v)

    @field_validator("issue_ref")
    @classmethod
    def _ref(cls, v):
        return None if v is None else check_field("issue_ref", v)


class DispatchBody(_Launch):
    mode: Literal["lane", "session"] = "lane"
    session: str | None = Field(None, max_length=190)
    auto_close: bool | None = None

    @field_validator("session")
    @classmethod
    def _session(cls, v):
        return None if v is None else check_field("session", v)

    @model_validator(mode="after")
    def _consistent(self):
        if self.mode == "session" and not self.session:
            raise ValueError("session is required to hand a task to a session")
        if self.mode == "lane" and self.session:
            raise ValueError("a lane dispatch starts a new session: drop session, or use mode session")
        return self


# ---- what an AGENT may send (issue #152): the body of an mcp write row, narrower than the person's. Exactly the fields the tool sends (mcp.NODE_SEND, checked below at import), nothing
# else: no model, effort, reasoning, issue reference, mode, session, force or queue, and the sizes of the tool's own schema. The model and the effort of the launch are never the agent's
# choice: the peer writes the explicit safe launch options for everything that is left out (safe_launch), and a dispatch is always a lane start (Row.agent_force).

class AgentTaskCreateBody(_Strict):
    project: str = Field(min_length=1, max_length=mcp_tools.MAX_LEN["project"])
    repo: str = Field(min_length=1, max_length=mcp_tools.MAX_LEN["repo"])
    title: str = Field(min_length=1, max_length=mcp_tools.MAX_LEN["title"])
    prompt: str = Field(min_length=1, max_length=mcp_tools.MAX_LEN["prompt"])
    when: Literal["now", "later"] = "now"
    agent: Literal["claude", "codex"] | None = None
    auto_close: StrictBool | None = None

    @field_validator("project", "repo")
    @classmethod
    def _names(cls, v):
        return check_field("name", v)

    @field_validator("title", "prompt")
    @classmethod
    def _typed_text(cls, v, info):
        return check_field("typed_prompt" if info.field_name == "prompt" else info.field_name, v)


class AgentDispatchBody(_Strict):
    agent: Literal["claude", "codex"] | None = None
    auto_close: StrictBool | None = None


def _check_agent_models() -> None:
    """A tool and its agent body cannot drift: the fields of each body are exactly the values of mcp.NODE_SEND, and the agent names are the tool schema's enum."""
    for tool, model in (("create_task", AgentTaskCreateBody), ("dispatch_task", AgentDispatchBody)):
        if set(model.model_fields) != set(mcp_tools.NODE_SEND[tool].values()):
            raise RuntimeError(f"the agent body of {tool} does not match mcp.NODE_SEND")
    enum = next(t for t in mcp_tools.TOOLS if t["name"] == "create_task")["inputSchema"]["properties"]["agent"]["enum"]
    if tuple(enum) != ("claude", "codex"):
        raise RuntimeError("the agent names of the tools changed: update the agent bodies")


_check_agent_models()


class SessionOpenBody(_Launch):
    project: str
    repo: str
    name: str | None = Field(None, max_length=63)
    permission_mode: str | None = Field(None, max_length=30)
    mode: str | None = Field(None, max_length=20)
    sandbox: str | None = Field(None, max_length=30)
    approval: str | None = Field(None, max_length=30)

    @field_validator("project", "repo", "name")
    @classmethod
    def _names(cls, v):
        return None if v is None else check_field("name", v)

    @field_validator("permission_mode", "mode", "sandbox", "approval")
    @classmethod
    def _choices(cls, v, info):
        return None if v is None else check_field(info.field_name, v)


class PromptBody(_Strict):
    """Text for a remote session's composer (issue #142). Free text by the one rule (check_field `text`): at most 8 KB, tab and newline are text, no control, format or
    line-separator character (a 422, never stripped: the text is typed into a terminal). `queue` is the local flag: also accept a session that is working."""
    text: str = Field(min_length=1, max_length=STEER_MAX)
    queue: StrictBool = False

    @field_validator("text")
    @classmethod
    def _typed_text(cls, v):
        v = check_field("typed_text", v)
        if not v.strip():
            raise ValueError("text is empty")
        return v


class KeysBody(_Strict):
    """One key for a remote session: a name from STEER_KEYS, nothing else. C-c needs `confirm` true (the UI's second tap), and `confirm` means nothing for another key."""
    key: str = Field(max_length=8)
    confirm: StrictBool = False

    @field_validator("key")
    @classmethod
    def _key(cls, v):
        return check_field("key", v)

    @model_validator(mode="after")
    def _confirmed(self):
        if self.key in CONFIRMED_KEYS and not self.confirm:
            raise ValueError(f"{self.key} needs confirm true")
        if self.confirm and self.key not in CONFIRMED_KEYS:
            raise ValueError("confirm is only for " + ", ".join(CONFIRMED_KEYS))
        return self


class EpicClaimBody(_Strict):
    """Claim one issue of an epic on the node that takes this request (issue #148). Three plain values and nothing else: `repo` (owner/name), `issue` (a whole number) and `epic`
    (the epic's issue number in that repository, or owner/name#number), which the node reads itself before it believes the issue is a child. No text from a person, a peer, an
    agent or an issue ever reaches the claim comment: the node builds it from its own validated fields. The node still checks that the repository is one of its own projects."""
    repo: str = Field(max_length=140)
    issue: StrictInt = Field(ge=1, lt=10 ** 9)
    epic: str | int | None = None

    @field_validator("repo")
    @classmethod
    def _repo(cls, v):
        return check_field("repo_slug", v)

    @field_validator("epic", mode="before")
    @classmethod
    def _epic(cls, v):
        if v is None:
            return None
        if isinstance(v, int) and not isinstance(v, bool):
            v = str(v)
        if not isinstance(v, str):
            raise ValueError("an epic is its issue number, or owner/name#number")
        return check_field("epic_ref", v)


class EpicClaimRequest(EpicClaimBody):
    """The hub adds a correlation token; it is never a GitHub claim fencing token."""
    claim_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class EpicClaimQuery(_Strict):
    repo: str = Field(max_length=140)
    issue: str = Field(pattern=r"^[1-9][0-9]{0,8}$")

    @field_validator("repo")
    @classmethod
    def _repo(cls, v):
        return check_field("repo_slug", v)


class EpicWorkBody(EpicClaimBody):
    """Run one issue of an epic on the node that takes this request (issue #149): the claim's three values, and the choices of the person and of the epic. `force` starts past the
    node's advisory lane cap. `report` (phase, summary or none), `auto_pr` and `close` are the epic's per-epic settings, which the hub writes into the body from its own stored choice
    (Row.fill) and every one starts off or at its plain default; the node reads exactly these and nothing else. Still no text from a person, a peer, an agent or an issue: the task
    prompt is built on the node from the issue as GitHub holds it."""
    force: StrictBool = False
    report: Literal["phase", "summary", "none"] = "phase"
    auto_pr: StrictBool = False
    close: StrictBool = False


def _title80(clean: dict) -> str:
    """A task title for an audit target: whitespace collapsed, anything shaped like a secret replaced, at most 80 characters. The prompt never comes near."""
    t = _text(" ".join(str(clean.get("title") or "").split()), TASK_TITLE_IN)
    return redact(t, known_secrets())[:80]


_SLUG_OUT = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
_BRANCH_OUT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,119}")
_WORD_OUT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,19}")
_NAME_OUT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?")
_ISSUE_OUT = re.compile(r"#[0-9]{1,9}|[A-Za-z0-9._-]{1,40}/[A-Za-z0-9._-]{1,40}#[0-9]{1,9}")
_TIME_OUT = re.compile(r"[0-9T:.+Z -]{1,40}")


def _shaped(v, rx: re.Pattern, n: int = 200):
    """A peer's string that has the shape `rx` (the whole of it), else None: a slug, a branch, a phase or a session name is not free text."""
    from . import nodes_hub
    t = nodes_hub._s(v, n)
    return t if t is not None and rx.fullmatch(t) and ".." not in t else None


def _shaped_tmux(v):
    t = _shaped(v, re.compile(r"[A-Za-z0-9_-]{1,190}"), 200)
    try:
        tmux.split_name(t or "")
    except ValueError:
        return None
    return t


def _shape_row(r) -> dict | None:
    from . import nodes_hub
    if not isinstance(r, dict):
        return None
    s, i = nodes_hub._s, nodes_hub._i
    return {"id": i(r.get("id")), "title": s(r.get("title")), "phase": _shaped(r.get("phase"), _WORD_OUT, 20), "agent": _shaped(r.get("agent"), _WORD_OUT, 20),
            "project": _shaped(r.get("project"), _NAME_OUT, 63), "repo": _shaped(r.get("repo"), _NAME_OUT, 63), "branch": _shaped(r.get("branch"), _BRANCH_OUT, 120),
            "tmux": _shaped_tmux(r.get("tmux")), "issue_ref": _shaped(r.get("issue_ref"), _ISSUE_OUT, 60), "updated_at": _shaped(r.get("updated_at"), _TIME_OUT, 40)}


def _shape_started(body, reg):
    """The answer of a task create or dispatch: {ref, id, slug, tmux, branch, phase, task, limit_warning?} rebuilt field by field. `ref` is built here from the
    registry's handle and the id, never taken from the peer."""
    from . import nodes_hub
    if not isinstance(body, dict):
        raise nodes_hub.Bad("not an object")
    s, i, n = nodes_hub._s, nodes_hub._i, nodes_hub._n
    tid = i(body.get("id"))
    if tid is None or tid < 1:
        raise nodes_hub.Bad("no task id")
    out = {"ref": nodes.format_ref(reg["handle"], str(tid), "task"), "id": tid, "slug": _shaped(body.get("slug"), _SLUG_OUT, 80), "tmux": _shaped_tmux(body.get("tmux")),
           "branch": _shaped(body.get("branch"), _BRANCH_OUT, 120), "phase": _shaped(body.get("phase"), _WORD_OUT, 20), "task": _shape_row(body.get("task"))}
    for k in ("pasted", "queued", "held"):
        if isinstance(body.get(k), bool):
            out[k] = body[k]
    lw = body.get("limit_warning")
    if isinstance(lw, dict):
        out["limit_warning"] = {"kind": _shaped(lw.get("kind"), _WORD_OUT, 20), "resets_at": n(lw.get("resets_at")), "pct": n(lw.get("pct"))}
    return out


def _shape_session_started(body, reg):
    """The answer of an opened session: {ref, tmux, agent, project, repo}; `ref` is `<handle>/<tmux>`, built here."""
    from . import nodes_hub
    if not isinstance(body, dict):
        raise nodes_hub.Bad("not an object")
    s = nodes_hub._s
    name = s(body.get("tmux"))
    try:
        tmux.split_name(name or "")
    except ValueError:
        raise nodes_hub.Bad("no session name") from None
    return {"ref": nodes.format_ref(reg["handle"], name, "session"), "tmux": name, "agent": _shaped(body.get("agent"), _WORD_OUT, 20),
            "project": _shaped(body.get("project"), _NAME_OUT, 63), "repo": _shaped(body.get("repo"), _NAME_OUT, 63)}


# ---- the steering rows (issue #142): the peer's answers rebuilt from a whitelist. A write row's answer is tolerant (a 2xx already says the peer did it); what the hub shows
# of it is built here, never copied.

SUMMARY_MAX = 300
_TOOL_OUT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,59}")
_PERMISSIONS_MAX = 50


def _shape_prompt(body, reg):
    from . import nodes_hub
    if not isinstance(body, dict):
        raise nodes_hub.Bad("not an object")
    return {"ok": True, "pasted": body.get("pasted") is True, "queued": body.get("queued") is True}


def _shape_keys(body, reg):
    from . import nodes_hub
    if not isinstance(body, dict):
        raise nodes_hub.Bad("not an object")
    return {"ok": True, "key": body["key"] if body.get("key") in STEER_KEYS else None}


def _shape_acked(body, reg):
    from . import nodes_hub
    if not isinstance(body, dict):
        raise nodes_hub.Bad("not an object")
    return {"acked": _shaped_tmux(body.get("acked"))}


def _shape_closed(body, reg):
    from . import nodes_hub
    if not isinstance(body, dict):
        raise nodes_hub.Bad("not an object")
    return {"killed": _shaped_tmux(body.get("killed"))}


_ALIAS_OUT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")
CLAIM_PEER_WHY = ("writes_off", "bad_request", "other_repo", "not_epic_child", "authors_unknown", "login_not_allowed", "claim_unknown", "taken", "lost", "closed", "done",
                  "attempts_exhausted", "too_many_claims", "rate_limited", "busy", "no_access", "no_login", "error", "outcome_unknown", "withdraw_failed")      # nodes_epics.CLAIM_TEXT without the two good answers
CLAIM_GOOD = ("won", "already_held")
UNLISTED_WHY = "unlisted_reason"          # the `unknown_reason` of a refusal whose `why` this board does not list: it reads as `error`, but is not known to be GitHub's


def _claim_facts(src: dict) -> dict:
    """The few facts of a claim answer, each rebuilt: the winner's alias (a plain word), the attempt (0 to 99), the wait (seconds, at most a day) and the unknown reason (a word).
    The random claim_id correlates relay attempts only. Nothing else is kept; never the peer sentence, a claim_ref, a login or a comment id."""
    out: dict = {}
    claim_id = src.get("claim_id")
    if isinstance(claim_id, str) and re.fullmatch(r"[a-f0-9]{32}", claim_id):
        out["claim_id"] = claim_id
    w = src.get("winner")
    if isinstance(w, str) and _ALIAS_OUT.fullmatch(w):
        out["winner"] = w
    for k, hi in (("attempt", 99), ("wait_s", 86400), ("claims_ahead", 100)):
        v = src.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= hi:
            out[k] = v
    u = src.get("unknown_reason")
    if isinstance(u, str) and _WORD_OUT.fullmatch(u):
        out["unknown_reason"] = u
    for k in ("unknown_outcome", "asked_person"):
        if src.get(k) is True:
            out[k] = True
    return out


def _shape_epic_claim(body, reg):
    """The answer of a claim that was made: {claimed: true, reason: won | already_held, repo, issue, winner?, attempt?}. A refusal never comes through here (it is a 409 `claim_refused`)."""
    from . import nodes_hub
    if not isinstance(body, dict) or body.get("claimed") is not True or body.get("reason") not in CLAIM_GOOD:
        raise nodes_hub.Bad("not a claim")
    repo = _shaped(body.get("repo"), re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}"), 140)
    issue = body.get("issue")
    if not repo or not isinstance(issue, int) or isinstance(issue, bool) or not 0 < issue < 10 ** 9:
        raise nodes_hub.Bad("not a claim")
    return {"claimed": True, "reason": body["reason"], "repo": repo, "issue": issue, **_claim_facts(body)}


WORK_PEER_WHY = CLAIM_PEER_WHY + ("author_not_allowed", "epic_author_not_allowed", "unreadable", "bypass_refused", "posture_refused", "lanes_full", "launch_failed", "changed")     # nodes_work.REASONS without the two good answers


def _shape_claim_status(body, reg):
    from . import nodes_hub
    if isinstance(body, dict) and body.get("claimed") is True:
        return _shape_epic_claim(body, reg)
    if not isinstance(body, dict) or body.get("claimed") is not False or body.get("reason") not in (*CLAIM_PEER_WHY, "pending", "unknown", "released"):
        raise nodes_hub.Bad("not a claim outcome")
    return {"claimed": False, "reason": body["reason"], **_claim_facts(body)}


def _fill_work(clean: dict, db) -> dict:
    """The hub's side of Run: the epic's `report`, `auto_pr` and `close` are the person's choices on THIS board (kv `epic:<repo#n>`), so they are written into the body here and a body
    that came from a page or an agent cannot choose them. Without a known epic the plain defaults go (phase comments, no pull request, no Closes)."""
    from . import nodes_epics
    owner, _, name = clean["repo"].partition("/")
    ep = nodes_epics._epic_ref(clean.get("epic"), owner, name)
    st = nodes_epics.load_settings(db, *ep) if ep else dict(nodes_epics.DEFAULTS)
    return {**clean, "report": st["report"], "auto_pr": st["auto_pr"], "close": st["close"]}


def _work_facts(src: dict) -> dict:
    """The claim's few facts, and the login of the author a refusal names (a plain GitHub login; nothing else of the peer's answer is kept)."""
    out = _claim_facts(src)
    a = src.get("author")
    if isinstance(a, str) and github.OWNER_RE.fullmatch(a):
        out["author"] = a
    return out


def _work_text(name: str, why: str, facts: dict) -> str:
    from . import nodes_epics, nodes_work
    if why in ("author_not_allowed", "epic_author_not_allowed"):
        who = "the epic" if why == "epic_author_not_allowed" else "the issue"
        return f"{name}: {who} is authored by {facts['author']}, not on the allowed list." if facts.get("author") else f"{name}: {nodes_work.WORK_TEXT[why]}"
    base = nodes_work.WORK_TEXT.get(why) or nodes_epics.CLAIM_TEXT[why]
    if why == "busy" and "claims_ahead" in facts:
        base += f" {facts['claims_ahead']} claims ahead; try again in about {facts.get('wait_s', int(CLAIM_TIMEOUT))} s."
    return f"{name}: {base}" + (f" It is {facts['winner']}." if facts.get("winner") and why in ("taken", "lost") else "")


def _shape_epic_work(body, reg):
    """The answer of a run that started: {claimed: true, reason: won | already_held, task_ref, id, branch, tmux, repo, issue}. `task_ref` is built here from the registry's handle and
    the id, never taken from the peer. A refusal never comes through here (it is a 409 `work_refused`, or a 404 `repo_not_here`)."""
    from . import nodes_hub
    if not isinstance(body, dict) or body.get("claimed") is not True or body.get("reason") not in CLAIM_GOOD:
        raise nodes_hub.Bad("not a started task")
    repo = _shaped(body.get("repo"), re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}"), 140)
    issue, tid = body.get("issue"), nodes_hub._i(body.get("id"))
    if not repo or not isinstance(issue, int) or isinstance(issue, bool) or not 0 < issue < 10 ** 9 or tid is None or tid < 1:
        raise nodes_hub.Bad("not a started task")
    return {"claimed": True, "reason": body["reason"], "task_ref": nodes.format_ref(reg["handle"], str(tid), "task"), "id": tid, "branch": _shaped(body.get("branch"), _BRANCH_OUT, 120),
            "tmux": _shaped_tmux(body.get("tmux")), "repo": repo, "issue": issue}


def _shape_permissions(body, reg):
    """The pending requests rebuilt field by field: {permissions: [{id, tmux, tool, summary (300 characters), since}]}. A row without an id or a session name is dropped."""
    from . import nodes_hub
    if not isinstance(body, dict) or not isinstance(body.get("permissions"), list):
        raise nodes_hub.Bad("not a permission list")
    out = []
    for r in body["permissions"][:_PERMISSIONS_MAX]:
        if not isinstance(r, dict):
            continue
        pid, tm = nodes_hub._i(r.get("id")), _shaped_tmux(r.get("tmux"))
        if pid is None or pid < 1 or tm is None:
            continue
        out.append({"id": pid, "tmux": tm, "tool": _shaped(r.get("tool"), _TOOL_OUT, 60) or "tool", "summary": nodes_hub._s(r.get("summary"), SUMMARY_MAX) or "",
                    "since": _shaped(r.get("since"), _TIME_OUT, 40)})
    return {"permissions": out}


def _shape_permission_answer(body, reg):
    from . import nodes_hub
    if not isinstance(body, dict):
        raise nodes_hub.Bad("not an object")
    pid = nodes_hub._i(body.get("id"))
    return {"ok": True, "id": pid if pid and pid > 0 else None, "decision": body["decision"] if body.get("decision") in DECISIONS else None}


# ---- what an AGENT receives (issue #152): the reduced answers of the mcp rows, built field by field for what the tools return and nothing else. No account or login detail, no usage
# window, lane count or scope, no address, node id, version or OS detail, no audit data and no screen state beyond the state word. A free-text field is cut to 120 characters, on one line,
# without control or format characters and with its angle brackets and backticks replaced (mcp.untrusted_text); a field with a shape (a name, a branch, a phase, a session name, a time)
# is that shape or null (mcp.untrusted_shaped). The tool layer applies the same helpers again, so a transport cannot return more than this.

_AG_NAME, _AG_BRANCH, _AG_WORD, _AG_TMUX, _AG_TIME = mcp_tools._R_NAME, mcp_tools._R_BRANCH, mcp_tools._R_WORD, mcp_tools._R_TMUX, mcp_tools._R_TIME


def _ag_sh(v, rx, n: int = 200):
    return mcp_tools.untrusted_shaped(v, rx, n)


def _agent_state(body, reg):
    """The state for an agent: projects (name, repos: name, branch, dirty), sessions (tmux, project, repo, agent, state, kind), tasks (id, title, phase, project, repo, branch, tmux), truncated.
    The whitelist, the sizes and the node id check of a person's state run first."""
    from . import nodes_hub
    st = nodes_hub.clean_state(body, reg)
    return {"projects": [{"name": _ag_sh(p.get("name"), _AG_NAME, 63),
                          "repos": [{"name": _ag_sh(r.get("name"), _AG_NAME, 63), "branch": _ag_sh(r.get("branch"), _AG_BRANCH, 120), "dirty": r.get("dirty") if isinstance(r.get("dirty"), bool) else None}
                                    for r in p.get("repos") or []]} for p in st["projects"]],
            "sessions": [{"tmux": _ag_sh(s.get("tmux"), _AG_TMUX, 190), "project": _ag_sh(s.get("project"), _AG_NAME, 63), "repo": _ag_sh(s.get("repo"), _AG_NAME, 63),
                          "agent": _ag_sh(s.get("agent"), _AG_WORD, 20), "state": _ag_sh(s.get("state"), _AG_WORD, 20), "kind": _ag_sh(s.get("kind"), _AG_WORD, 20)} for s in st["sessions"]],
            "tasks": [{"id": t.get("id"), "title": mcp_tools.untrusted_text(t.get("title"), mcp_tools.TITLE_MAX), "phase": _ag_sh(t.get("phase"), _AG_WORD, 20),
                       "project": _ag_sh(t.get("project"), _AG_NAME, 63), "repo": _ag_sh(t.get("repo"), _AG_NAME, 63), "branch": _ag_sh(t.get("branch"), _AG_BRANCH, 120),
                       "tmux": _ag_sh(t.get("tmux"), _AG_TMUX, 190)} for t in st["tasks"]],
            "truncated": bool(st.get("truncated"))}


def _agent_task(body, reg):
    """One task for an agent: id, title, phase, agent, project, repo, branch, tmux, pr_state, pr_number, updated_at, done_at, has_result."""
    from . import nodes_hub
    if not isinstance(body, dict):
        raise nodes_hub.Bad("not an object")
    i = nodes_hub._i
    return {"id": i(body.get("id")), "title": mcp_tools.untrusted_text(body.get("title"), mcp_tools.TITLE_MAX), "phase": _ag_sh(body.get("phase"), _AG_WORD, 20),
            "agent": _ag_sh(body.get("agent"), _AG_WORD, 20), "project": _ag_sh(body.get("project"), _AG_NAME, 63), "repo": _ag_sh(body.get("repo"), _AG_NAME, 63),
            "branch": _ag_sh(body.get("branch"), _AG_BRANCH, 120), "tmux": _ag_sh(body.get("tmux"), _AG_TMUX, 190), "pr_state": _ag_sh(body.get("pr_state"), _AG_WORD, 20),
            "pr_number": i(body.get("pr_number")), "updated_at": _ag_sh(body.get("updated_at"), _AG_TIME, 40), "done_at": _ag_sh(body.get("done_at"), _AG_TIME, 40),
            "has_result": body.get("has_result") is True}


def _agent_started(body, reg):
    """The answer of a create or dispatch for an agent: ref, id, tmux, branch, phase, task (id, title, phase, agent, project, repo, branch, tmux, updated_at), limit_warning (kind, resets_at, pct)."""
    full = _shape_started(body, reg)
    t = full.get("task")
    out = {"ref": full["ref"], "id": full["id"], "tmux": full["tmux"], "branch": full["branch"], "phase": full["phase"],
           "task": {"id": t["id"], "title": mcp_tools.untrusted_text(t.get("title"), mcp_tools.TITLE_MAX), "phase": t["phase"], "agent": t["agent"], "project": t["project"],
                    "repo": t["repo"], "branch": t["branch"], "tmux": t["tmux"], "updated_at": t["updated_at"]} if isinstance(t, dict) else None}
    if "limit_warning" in full:
        out["limit_warning"] = full["limit_warning"]
    return out


RELAY: tuple[Row, ...] = (
    # card and agents: a person's rows. No MCP tool reads either (list_nodes is built from the hub's own reading), so an agent has no use for them and does not get them.
    Row("card", "GET", "/api/nodes/{handle}/card", "GET", "/api/node", "read", NoFields, READ, "read_card", human_only=True, peer_exists=True,
        target=lambda p: "card", shape=_shape_card),
    Row("state", "GET", "/api/nodes/{handle}/state", "GET", "/api/node/state", "read", NoFields, READ, "read_state", mcp=True, peer_exists=True, answer_max=128 * 1024,
        agent_model=NoFields, agent_shape=_agent_state, target=lambda p: "state", shape=_shape_state),
    Row("task", "GET", "/api/nodes/{handle}/tasks/{tid}", "GET", "/api/node/tasks/{tid}", "read", NoFields, READ, "read_task", mcp=True, answer_max=16 * 1024,
        agent_model=NoFields, agent_shape=_agent_task, target=lambda p: f"task {p.get('tid')}", shape=_shape_task),
    # Screen text is session content, not names and counts: the pane row needs `sessions`, never `read`.
    Row("pane", "GET", "/api/nodes/{handle}/sessions/{name}/pane", "GET", "/api/node/sessions/{name}/pane", "sessions", PaneQuery, READ, "read_pane", human_only=True,
        target=lambda p: f"session {p.get('name')}", shape=_shape_pane),
    Row("agents", "GET", "/api/nodes/{handle}/agents", "GET", "/api/node/agents", "read", NoFields, READ, "read_agents", human_only=True, answer_max=256 * 1024,
        target=lambda p: "agents", shape=_shape_agents),
    # The write rows (issue #141). A task is a worktree, a branch and a tmux session on the node that runs it: the hub never makes one.
    Row("task_create", "POST", "/api/nodes/{handle}/tasks", "POST", "/api/node/tasks", "tasks", TaskCreateBody, WRITE, "create_task", guard=True, mcp=True,
        agent_model=AgentTaskCreateBody, agent_shape=_agent_started, answer_max=16 * 1024, target=lambda p: "create task",
        body_target=lambda p, c: f"create task: {_title80(c)}", shape=_shape_started),
    Row("task_dispatch", "POST", "/api/nodes/{handle}/tasks/{tid}/dispatch", "POST", "/api/node/tasks/{tid}/dispatch", "tasks", DispatchBody, WRITE, "dispatch_task",
        guard=True, guard_skip=("mode",), mcp=True, agent_model=AgentDispatchBody, agent_shape=_agent_started, agent_force={"mode": "lane"}, answer_max=16 * 1024,
        extra_scope=lambda c: "sessions" if c.get("mode") == "session" else None,
        target=lambda p: f"dispatch task {p.get('tid')}", body_target=lambda p, c: f"dispatch task {p.get('tid')} ({c.get('mode')})", shape=_shape_started),
    Row("session_open", "POST", "/api/nodes/{handle}/sessions", "POST", "/api/node/sessions", "sessions", SessionOpenBody, WRITE, "open_session", guard=True,
        human_only=True, answer_max=4 * 1024, target=lambda p: "open session", body_target=lambda p, c: f"open session {c.get('project')}/{c.get('repo')}",
        shape=_shape_session_started),
    # The steering rows (issue #142). Text and keys typed into a session are launching work in it (guard=True; the peer also checks the target's recorded launch line).
    # The prompt text is never in a target, an audit row or an answer; a key is, by its name from the closed list.
    Row("prompt", "POST", "/api/nodes/{handle}/sessions/{name}/prompt", "POST", "/api/node/sessions/{name}/prompt", "sessions", PromptBody, WRITE, "send_prompt", guard=True,
        human_only=True, acting=True, big_field="text", per_session=True, dialog_scope="permissions", answer_max=4 * 1024, target=lambda p: f"prompt {p.get('name')}", shape=_shape_prompt),
    Row("keys", "POST", "/api/nodes/{handle}/sessions/{name}/keys", "POST", "/api/node/sessions/{name}/keys", "sessions", KeysBody, WRITE, "send_keys", guard=True,
        human_only=True, acting=True, dialog_scope="permissions", answer_max=4 * 1024, target=lambda p: f"keys {p.get('name')}", body_target=lambda p, c: f"keys {p.get('name')} ({c.get('key')})",
        shape=_shape_keys),
    Row("ack", "POST", "/api/nodes/{handle}/sessions/{name}/ack", "POST", "/api/node/sessions/{name}/ack", "sessions", NoFields, WRITE, "ack_session", human_only=True,
        acting=True, answer_max=4 * 1024, target=lambda p: f"ack {p.get('name')}", shape=_shape_acked),
    Row("close", "DELETE", "/api/nodes/{handle}/sessions/{name}", "DELETE", "/api/node/sessions/{name}", "sessions", NoFields, WRITE, "close_session", human_only=True,
        acting=True, answer_max=4 * 1024, target=lambda p: f"close {p.get('name')}", shape=_shape_closed),
    # A permission request is the agent asking its owner: only a signed-in person answers it, and only allow or deny (no `always`, no rule). The list shows what the agent
    # wants to do, so it needs `permissions`, never `read`.
    Row("permissions", "GET", "/api/nodes/{handle}/permissions", "GET", "/api/node/permissions", "permissions", NoFields, READ, "read_permissions", human_only=True,
        acting=True, answer_max=64 * 1024, target=lambda p: "permissions", shape=_shape_permissions),
    Row("permission_answer", "POST", "/api/nodes/{handle}/permissions/{pid}/{decision}", "POST", "/api/node/permissions/{pid}/{decision}", "permissions", NoFields, WRITE,
        "answer_permission", human_only=True, acting=True, answer_max=4 * 1024, target=lambda p: f"permission {p.get('pid')} {p.get('decision')}",
        shape=_shape_permission_answer),
    # Claim one issue of an epic on that node (issue #148): a write to GitHub made by the node, so it needs `tasks`, a signed-in person (human only, and the person is named: acting),
    # never an agent (mcp stays off: the agent path for epics is a later phase). The audit target is the repository and the issue number and nothing else.
    Row("epic_claim", "POST", "/api/nodes/{handle}/epics/claim", "POST", "/api/node/epics/claim", "tasks", EpicClaimRequest, WRITE, "claim_issue", human_only=True, acting=True,
        timeout=CLAIM_TIMEOUT, answer_max=4 * 1024, target=lambda p: "claim", body_target=lambda p, c: f"claim {c.get('repo')}#{c.get('issue')}", shape=_shape_epic_claim),
    # Database-only recovery of an unknown claim: tasks scope, person only, no launch and no GitHub write.
    Row("epic_claim_status", "GET", "/api/nodes/{handle}/epics/claim/status", "GET", "/api/node/epics/claim/status", "tasks", EpicClaimQuery, READ, "claim_status", human_only=True, acting=True,
        answer_max=4 * 1024, target=lambda p: "claim", body_target=lambda p, c: f"claim {c.get('repo')}#{c.get('issue')}", shape=_shape_claim_status),
    # Run a claimed issue as a task on that node and report back on the issue (issue #149): the claim, a task with the issue's own model line, and comments. It starts an agent, so it is
    # guarded like the other launching rows (`guard=True`), a person's act (human only, acting), never an agent's (mcp stays closed: no tool starts epic work). The audit target is the
    # repository and the issue number and nothing else; the body carries no text, only values the node validates again.
    Row("epic_work", "POST", "/api/nodes/{handle}/epics/work", "POST", "/api/node/epics/work", "tasks", EpicWorkBody, WRITE, "work_issue", guard=True, human_only=True, acting=True,
        timeout=WORK_TIMEOUT, answer_max=4 * 1024, target=lambda p: "work", body_target=lambda p, c: f"work {c.get('repo')}#{c.get('issue')}", shape=_shape_epic_work, fill=_fill_work),
)
# A row is human only unless it says `mcp=True`: the hub relay routes take a signed-in allowed person with X-CCBoard and nothing else. The local hook token is held
# by every agent session on the box, so letting it relay would let any agent read other nodes through the hub. MCP across nodes (issue #152) opens exactly four rows to
# an agent on purpose and nothing else: the reads state and task, and the writes task_create and task_dispatch (a new session only: an agent never gets a row's
# `extra_scope`). Even those need CCBOARD_MCP_NODES to grant the row's scope (read, or tasks for the writes), the device token's own scope (the hook token, the stdio shim,
# holds only what the level grants) and the pair's, and an agent sends and receives the row's narrower body and reduced answer (agent_model, agent_shape).
# Never an agent: prompt, keys, ack, close, permissions, permission_answer, pane, session_open, the stream, pairing, tokens, settings. Row.__post_init__ refuses
# `mcp=True` on a human-only row, a named-person row, a prompt, a dialog answer, a scope beyond read and tasks and an unguarded write.
BY_NAME = {r.name: r for r in RELAY}


def rate_class(method: str, path: str) -> str:
    """The bucket a node-token request on (method, path) draws from: the row's rate class, else by method (a read is GET or HEAD). The auth middleware calls it."""
    m = str(method or "").upper()
    for r in RELAY:
        if r.peer_method == m and r.peer_re.fullmatch(path):
            return r.rate_class
    return READ if m in ("GET", "HEAD") else WRITE


def register_scopes(rows=RELAY) -> None:
    """Put the peer rows into nodes.NODE_ROUTES. A path already there must carry the same scope (the card and the state were listed first)."""
    for r in rows:
        key = (r.peer_method, r.peer_path)
        if nodes.NODE_ROUTES.get(key, r.scope) != r.scope:
            raise RuntimeError(f"{key} is already listed with another scope")
        nodes.NODE_ROUTES[key] = r.scope


def forget_scopes(rows) -> None:
    for r in rows:
        nodes.NODE_ROUTES.pop((r.peer_method, r.peer_path), None)


register_scopes()


def methods_for(path: str) -> list[str]:
    """The methods NODE_ROUTES lists for `path` (a route a token may call with another method answers 405, not 403)."""
    out = []
    for (m, pat), _scope in nodes.NODE_ROUTES.items():
        if pat == path or ("{" in pat and _pattern(pat).fullmatch(path)):
            out.append(m)
    return sorted(set(out))


# ================================================================ checking a request: one function for both sides

def _text(v, n: int) -> str:
    """Plain printable text of at most n characters (control characters out, a token-shaped word replaced)."""
    return nodes._scrub(v, n) or ""


class Invalid(Exception):
    """A request body or parameter the row does not take. `messages` are plain sentences with no value of the request in them."""

    def __init__(self, messages: list[str], status: int = 422):
        self.messages, self.status = list(messages), status           # 422, or 413 for text over its cap (Row.big_field)
        super().__init__("; ".join(self.messages))


def _model_messages(e: ValidationError) -> list[str]:
    out = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err.get("loc", ())) or "request"
        msg = str(err.get("msg") or "not valid")
        msg = msg[len("Value error, "):] if msg.startswith("Value error, ") else msg
        out.append(_text(f"{loc}: {msg[:80]}", 120))     # `loc` can be a key the caller made up: cap it and drop control characters
    return out[:6]


def validate(row: Row, params: dict, body, *, agent: bool = False) -> dict:
    """The body of a request as the row's model reads it (`{}` for no body), or Invalid. Path parameters are checked first, then the guard (for a row that
    has one), then the model, so a refusal always names a field and never repeats a value. `agent` (the caller is an MCP agent, the hook token or a device token) reads the body with
    the row's agent model instead, which has only the fields the tool sends, and adds the row's constants (a lane dispatch)."""
    if agent and row.agent_model is None:
        raise Invalid(["this row takes no request from an agent"])
    for name in row.params:
        if not _valid_param(name, (params or {}).get(name)):
            raise Invalid([f"{name} is not valid"])
    data = {} if body is None else body
    if not isinstance(data, dict):
        raise Invalid(["the request must be a JSON object"])
    if row.guard:
        refusals = guard_launch({k: v for k, v in data.items() if k not in row.guard_skip} if row.guard_skip else data)
        if refusals:
            raise Invalid(refusals)
    try:
        model = (row.agent_model if agent else row.body_model).model_validate(data)
    except ValidationError as e:
        big = row.big_field is not None and any(err.get("type") == "string_too_long" and tuple(err.get("loc", ())) == (row.big_field,) for err in e.errors())
        raise Invalid(_model_messages(e), 413 if big else 422) from None
    clean = model.model_dump(exclude_none=True)
    return {**clean, **row.agent_force} if agent else clean


def safe_target(row: Row, params: dict, clean: dict | None = None) -> str:
    """The audit target of a request: the row's wording with every path parameter that is not valid replaced by `?`, so text a caller made up never reaches a row.
    `clean` is the validated body: a row with a `body_target` (a task's title) uses it once the body is known; before that the plain wording stands."""
    safe = {k: (v if _valid_param(k, v) else "?") for k, v in (params or {}).items()}
    if clean is not None and row.body_target is not None:
        return row.body_target(safe, clean)
    return row.target(safe)


def peer_path(row: Row, params: dict, clean: dict) -> str:
    """The path on the peer: the row's peer path with the validated parameters in, and, for a GET, the validated query."""
    path = row.peer_path
    for name in row.params:
        path = path.replace("{" + name + "}", params[name])
    if row.peer_method == "GET" and clean:
        path += "?" + urlencode(sorted(clean.items()))
    return path


# ================================================================ the hub side

class RelayError(Exception):
    """A relayed call that did not happen. `reason` is a stable word the page switches on, str() the sentence, `status` the HTTP code."""

    def __init__(self, status: int, reason: str, message: str, *, kind: str = "failed", headers: dict | None = None, **extra):
        super().__init__(message)
        self.status, self.reason, self.message = status, reason, message
        self.kind = kind                    # the audit status: `refused` (nothing was sent) or `failed` (the call was made)
        self.headers = dict(headers or {})
        self.extra = extra

    def response(self, node: str | None = None) -> JSONResponse:
        body = {"error": self.message, "reason": self.reason, **({"node": node} if node else {}), **self.extra}
        return JSONResponse(body, status_code=self.status, headers={**NO_STORE, **self.headers})


def mcp_identity(request) -> dict | None:
    """The agent behind a request, or None when it is a person's: {by, scopes, kind}. `by` is what the audit names (`local-token` for the stdio shim, which holds the hook token,
    and `mcp:<token name>` for a /mcp device token); `scopes` are the scopes that caller holds on other nodes. The shim holds exactly what CCBOARD_MCP_NODES grants (read: read; tasks:
    read and tasks; off: nothing), never every scope; a device token holds the ones it was minted with, as far as the level grants them. Only the auth middleware sets `state.user`
    to `local-token`, and only mcp_remote.gate (or the in-process call it makes) sets `state.mcp_token`: a name that merely looks like one is refused (RelayError 403)."""
    user = getattr(request.state, "user", None)
    granted = MCP_LEVELS.get(mcp_level(), ())
    if user == "local-token":
        return {"by": user, "scopes": granted, "kind": "shim"}
    if isinstance(user, str) and user.startswith("mcp:"):
        tok = getattr(request.state, "mcp_token", None)
        if not isinstance(tok, dict) or not tok.get("id") or user != "mcp:" + str(tok.get("name")):
            raise RelayError(403, "forbidden", "that caller is not a device token", kind="refused")
        return {"by": user, "scopes": tuple(s for s in (tok.get("scopes") or []) if isinstance(s, str) and s in granted), "kind": "token"}
    return None


def _caller(request, row: Row) -> str:
    """Who asked, as the audit names them: the signed-in login, `local-token` (the MCP shim) or `mcp:<token name>` (a /mcp device token). Refuses a node token, the legacy hub
    and a request that is no person's without X-CCBoard. An agent (the hook token or a device token) gets in only on a row that says `mcp`, in this order: the board's
    CCBOARD_MCP_NODES is not off (403 mcp_off), its level grants the row's scope (read: the reads; tasks: also the writes; else 403 mcp_level), the caller's own scope holds
    it too (403 mcp_scope); the pair's scope and the rest follow in relay(). Any other row is a person's: 403 human_only."""
    user = getattr(request.state, "user", None)
    if not isinstance(user, str) or not user or user == "hub" or user.startswith("node:"):
        raise RelayError(403, "forbidden", "a node token cannot ask a board to relay", kind="refused")
    agent = mcp_identity(request)
    if agent is not None:
        if not row.mcp:
            raise RelayError(403, "human_only", "this action must be done by a signed-in person on this board", kind="refused")
        level = mcp_level()
        if not level:
            raise RelayError(403, "mcp_off", MCP_OFF, kind="refused")
        if row.scope not in MCP_LEVELS[level]:
            raise RelayError(403, "mcp_level", MCP_READ_ONLY, kind="refused")
        if row.scope not in agent["scopes"]:
            raise RelayError(403, "mcp_scope", f"this device token does not hold the {row.scope} scope: mint one that does in Settings > Agents", kind="refused")
        return agent["by"]
    if request.headers.get("x-ccboard") != "1":
        raise RelayError(403, "csrf", "missing X-CCBoard header", kind="refused")
    return user


def _registry_row(handle: str, db) -> dict:
    if handle == nodes.LOCAL:
        raise RelayError(400, "local", "that is this board: use its own routes", kind="refused")
    if not nodes.valid_handle(handle):
        raise RelayError(400, "bad_handle", "not a node handle", kind="refused")
    reg = next((r for r in nodes.registry(db) if r.get("handle") == handle), None)
    if reg is None:
        raise RelayError(404, "unknown_node", "no paired node has that handle", kind="refused")
    return reg


def _self_check(reg: dict) -> None:
    mine = nodes.node_id()
    if (reg.get("node_id") and reg["node_id"] == mine) or nodes._same_address(reg.get("url"), nodes.public_url()):
        raise RelayError(400, "self", "that node is this board: a relay to itself is refused", kind="refused")


def _hub_record(hub, handle: str) -> dict | None:
    try:
        recs = hub.records(handle)
    except Exception as e:
        log.debug("hub record could not be read: %s", e.__class__.__name__)
        return None
    return recs[0] if recs else None


def _status_check(reg: dict, rec: dict | None, name: str) -> None:
    if reg.get("needs_repair") or (rec or {}).get("status") == "unauthorized":
        raise RelayError(409, "needs_repair", f"{name} no longer takes this board's token: re-pair", kind="refused")
    status = (rec or {}).get("status")
    if status == "unpaired" and (rec or {}).get("error_kind") == "identity_changed":          # issue #153: another device answers at the registered address; nothing of it was merged
        raise RelayError(409, "identity_changed", f"This address now belongs to another device, not {name}: remove it or pair again; nothing was sent", kind="refused")
    if status == "unpaired":
        raise RelayError(409, "unpaired", f"{name} answers as another node or no longer knows this board: remove it and pair it again", kind="refused")
    if status == "incompatible":                                                             # issue #153: its card still reads, its actions are refused
        theirs = (rec or {}).get("peer_api")
        theirs = theirs if isinstance(theirs, int) and not isinstance(theirs, bool) else None
        raise RelayError(409, "incompatible", f"{name} speaks node API {theirs if theirs is not None else 'unknown'} and this board speaks {nodes.API_VERSION}: update the older "
                                              f"board; nothing was sent", kind="refused", peer_api=theirs, api=nodes.API_VERSION)
    if not rec or not (rec.get("polled_at") or rec.get("last_ok_at")):
        raise RelayError(409, "not_read_yet", f"{name} has not been read yet: wait for the first reading (a few seconds after pairing); nothing was sent", kind="refused")
    if status not in ("online", "stale"):
        age = rec.get("age_s")
        when = f"last answered {_span(age)} ago" if age is not None else "has not answered yet"
        raise RelayError(503, "offline", f"{name} is offline ({when}); nothing was sent", age=age, kind="refused")


def _not_installed(rec: dict | None, agent: str) -> bool:
    """Does the hub's last reading of the node's card say the node has no usable `agent`? False (go on, the node checks again) when the card was not read or lists no agent at all."""
    card = (rec or {}).get("card")
    listed = card.get("agents") if isinstance(card, dict) else None
    if not isinstance(listed, list) or not listed:
        return False
    return agent not in {a.get("id") for a in listed if isinstance(a, dict) and a.get("installed") is True}


def _span(s) -> str:
    s = int(s or 0)
    return f"{s} s" if s < 90 else f"{s // 60} min" if s < 5400 else f"{s // 3600} h"


def _cut(v, n: int = 160) -> str:
    from . import node_state
    t = node_state.clean(v, n) or ""
    return redact(nodes._scrub(t, n) or "", known_secrets()) if t else ""


def _missing_repo_text(clean: dict, name: str) -> str:
    """"<repo> is not on <node>", from the hub's own validated request (never the peer's words). When this board has the same repo with a GitHub remote, the clone
    path on that node is named; the hub never clones for the peer."""
    project, repo = str(clean.get("project") or ""), str(clean.get("repo") or "")
    what = f"project {project}" if repo == projects.ROOT else repo
    text = f"{what} is not on {name}"
    slug = None
    try:
        from . import node_state
        path = projects.repo_path(project, repo)
        slug = node_state.repo_slug(path) if path.is_dir() else None
    except Exception as e:
        log.debug("local slug unknown: %s", e.__class__.__name__)
    if slug:
        text += f". It is {slug} on GitHub: clone it in Settings > Projects on {name}. Nothing was cloned or created."
    else:
        text += ". Nothing was created."
    return redact(text, known_secrets())[:300]


_WORD = re.compile(r"[a-z][a-z0-9_-]{0,29}")


def _busy_extra(j: dict) -> dict:
    """The parts of a peer's 409 `busy` the page may use, each rebuilt: `code` (why: working, compacting, permission ...), `retry` (seconds, or null when waiting will not help),
    `state` and `wait_kind` (words). Anything else the peer put there is dropped."""
    def word(v):
        return v if isinstance(v, str) and _WORD.fullmatch(v) else None
    retry = j.get("retry")
    return {"code": word(j.get("code")), "retry": retry if isinstance(retry, int) and not isinstance(retry, bool) and 0 <= retry <= 86400 else None,
            "state": word(j.get("state")), "wait_kind": word(j.get("wait_kind"))}


def _known_why(why, known: tuple, facts: dict) -> str:
    """A peer's refusal reason from this board's closed list. One it does not know (a newer peer) reads as `error`, and `facts` says so with
    `unknown_reason` UNLISTED_WHY, so the run loop does not take it for a GitHub error (nodes_run)."""
    if isinstance(why, str) and why in known:
        return why
    facts["unknown_reason"] = UNLISTED_WHY
    return "error"


def _map_reply(r, row: Row, reg: dict, clean: dict | None = None) -> dict:
    """The peer's answer as a body, or a RelayError for every status that is not a good answer."""
    name = reg.get("name") or reg.get("handle")
    if r.ok:
        if not isinstance(r.json, dict):
            extra = _unknown_if_write(row, reg)
            raise RelayError(502, "bad_answer", f"{name} answered something that is not a JSON object" + _check_note(extra, name), kind="failed", **extra)
        return r.json
    msg = _cut((r.json or {}).get("error") if isinstance(r.json, dict) else "")
    if r.status == 401:
        raise RelayError(409, "needs_repair", f"{name} no longer takes this board's token: re-pair", kind="failed")
    peer_reason = r.json.get("reason") if isinstance(r.json, dict) else None
    if r.status == 403 and peer_reason == "asking" and row.dialog_scope:          # the session asks a question in its terminal: the pair needs the scope that may answer it
        raise RelayError(409, "scope", f"needs the {row.dialog_scope} scope on {name}: this session is asking a question in its terminal", kind="failed")
    if r.status == 403:
        raise RelayError(409, "scope", f"needs the {row.scope} scope on {name}" if "scope" in msg.lower() or not msg else f"{name} refused: {msg}", kind="failed")
    if r.status == 404:
        if peer_reason == "repo_not_here" and row.name == "epic_work":          # no clone of that repository on the node: nothing was claimed
            from . import nodes_work
            raise RelayError(404, "repo_not_here", f"{name}: {nodes_work.WORK_TEXT['repo_not_here']}", kind="failed", why="repo_not_here")
        if peer_reason == "repo_missing" and clean and "project" in clean:
            raise RelayError(404, "repo_missing", _missing_repo_text(clean, name), kind="failed")
        raise RelayError(404, "gone" if peer_reason == "gone" else "not_found", msg or f"{name} has no such thing", kind="failed")
    if r.status == 409 and peer_reason == "claim_refused" and row.name == "epic_claim":          # the node did not take the issue: the reason comes from a closed list, the sentence is this board's
        why = r.json.get("why") if isinstance(r.json, dict) else None
        facts = _claim_facts(r.json if isinstance(r.json, dict) else {})
        why = _known_why(why, CLAIM_PEER_WHY, facts)
        from . import nodes_epics
        text = f"{name}: {nodes_epics.CLAIM_TEXT[why]}" + (f" It is {facts['winner']}." if facts.get("winner") and why in ("taken", "lost") else "")
        if why == "busy" and "claims_ahead" in facts:
            text += f" {facts['claims_ahead']} claims ahead; try again in about {facts.get('wait_s', int(CLAIM_TIMEOUT))} s."
        raise RelayError(409, "claim_refused", text, kind="failed", why=why, **facts)
    if r.status == 409 and peer_reason == "work_refused" and row.name == "epic_work":          # the node did not run the issue: the reason comes from a closed list, the sentence is this board's
        why = r.json.get("why") if isinstance(r.json, dict) else None
        facts = _work_facts(r.json if isinstance(r.json, dict) else {})
        why = _known_why(why, WORK_PEER_WHY, facts)
        raise RelayError(409, "work_refused", _work_text(name, why, facts), kind="failed", why=why, **facts)
    if r.status == 409 and peer_reason in ("busy", "answered", "expired"):          # the peer's own state answers, passed on with the words it chose from a closed list
        extra = _busy_extra(r.json) if peer_reason == "busy" else {}
        raise RelayError(409, peer_reason, msg or f"{name} cannot take this now", kind="failed", **extra)
    if r.status == 429:
        wait = str(max(1, min(3600, int(r.headers.get("retry-after", "1") or 1)))) if str(r.headers.get("retry-after", "1")).isdigit() else "1"
        raise RelayError(429, "rate_limited", f"{name} is receiving too many requests from this board; wait {wait} s", headers={"Retry-After": wait}, kind="failed")
    if r.status == 422:
        raise RelayError(422, "invalid", msg or f"{name} did not accept the request", kind="failed")
    if r.status == 413:
        raise RelayError(413, "too_large", msg or f"{name} did not accept the request", kind="failed")
    if r.status in (400, 409, 411):
        raise RelayError(r.status, "refused", msg or f"{name} did not accept the request", kind="failed")
    extra = _unknown_if_write(row, reg) if r.status >= 500 else {}
    raise RelayError(502, "peer_error", f"{name} answered with an error ({r.status if r.status >= 500 else 'unexpected'})" + _check_note(extra, name), kind="failed", **extra)


_MAYBE_SENT = (TimeoutError, socket.timeout, ConnectionResetError, ConnectionAbortedError, BrokenPipeError, http.client.HTTPException, EOFError)


def _check_note(extra: dict, name: str) -> str:
    """The sentence that goes with `_unknown_if_write`: nothing for a read."""
    return f"; it may have acted, so check {name} before starting again (it was not retried)" if extra else ""


def _unknown_if_write(row: Row, reg: dict) -> dict:
    """The extra fields of a write whose answer was a server error or a broken one (issue #153): the peer may have acted before it failed, so the outcome is unknown and a person
    looks at the node. A read gets none. The hub never retries either way."""
    return {"outcome": "unknown", "check_url": _check_url(reg)} if row.rate_class == WRITE or row.peer_method == "POST" else {}


def _check_url(reg: dict) -> str | None:
    """Where a person looks after a write whose outcome is unknown (issue #153): the node's own page on this board (`#/n/<handle>`, its Tasks), built from the registry handle."""
    h = reg.get("handle")
    return f"#/n/{h}" if nodes.valid_handle(h) else None


def _map_peer_error(e: nodes.PeerError, reg: dict, row: Row | None = None) -> RelayError:
    name = reg.get("name") or reg.get("handle")
    if e.reason == "no_token":
        return RelayError(409, "needs_repair", f"{name}: this board holds no token for it: re-pair", kind="refused")
    if e.reason in ("url", "bad_path"):
        return RelayError(502, "bad_address", f"the address of {name} is not one this board may call; nothing was sent", kind="refused")
    if e.reason == "redirect":
        return RelayError(502, "redirect", f"{name} answered with a redirect, which is never followed", kind="failed")
    if e.reason == "too_large":
        return RelayError(502, "too_large", f"the request or the answer was over the size limit", kind="failed")
    if isinstance(e.cause, (TimeoutError, socket.timeout)):
        if row is not None and row.rate_class == WRITE:
            return RelayError(504, "unconfirmed", f"{name} did not answer in {int(row.timeout)} s: it could not be confirmed whether it started, and it was not retried. "
                                                  f"Check {name} before starting again.", kind="failed", outcome="unknown", check_url=_check_url(reg))
        return RelayError(504, "unconfirmed", f"{name} did not answer in {int(row.timeout if row is not None else RELAY_TIMEOUT)} s: it could not be confirmed and it was not retried", kind="failed")
    if row is not None and row.rate_class == WRITE and isinstance(e.cause, _MAYBE_SENT):          # the connection broke after the request went out: the peer may have acted
        return RelayError(502, "unconfirmed", f"the connection to {name} broke while it was answering: it could not be confirmed whether it started, and it was not "
                                              f"retried. Check {name} before starting again.", kind="failed", outcome="unknown", check_url=_check_url(reg))
    return RelayError(502, "unreachable", f"{name} could not be reached", kind="failed")


def relay(handle, row: Row, params: dict, body, request, *, db=None, hub=None) -> dict:
    """Ask the peer `handle` to do `row`. Returns {node, age, data}; raises RelayError. See the module text for the order of the checks; none of them makes a
    call, and the one call is made by nodes.PeerClient to the address the registry holds."""
    d = nodes._db(db)
    if hub is None:
        from . import main
        hub = main._hub()
    reg: dict | None = None
    user: str | None = None
    agent: dict | None = None
    target = safe_target(row, params or {})
    claim_id = None
    try:
        user = _caller(request, row)
        agent = mcp_identity(request)
        # The bucket is keyed by who asked: a person's login, a device token's name, or `local-token`, which is every agent session on the box together (the hook token names no
        # session). An agent never draws from a person's bucket, and the shim's own is shared among the box's agents, which only makes it the stricter.
        ok, wait = (nodes.relay_write_limiter if row.rate_class == WRITE else nodes.relay_read_limiter).allow(user)
        if not ok:
            raise RelayError(429, "rate_limited", f"too many relayed requests from you; wait {wait} s", headers={"Retry-After": str(wait)}, kind="refused")
        try:
            reg = _registry_row(handle, d)
        except RelayError as e:
            if agent is not None and e.reason == "unknown_node":          # an agent is told which handles exist (a handle is a short label, never an address)
                handles = [r["handle"] for r in nodes.registry(d) if r.get("handle")]
                raise RelayError(404, "unknown_node", f"no paired node has the handle {handle}; " + (f"paired nodes: {', '.join(handles)}" if handles else "this board has no paired node"),
                                 kind="refused") from None
            raise
        _self_check(reg)
        name = reg.get("name") or handle
        if reg.get("legacy"):
            raise RelayError(409, "legacy", f"{name} was added with CCBOARD_NODES and holds no token: pair it to act on it", kind="refused")
        if row.scope not in (nodes._scopes_of(reg.get("scopes")) or []):
            raise RelayError(409, "scope", f"needs the {row.scope} scope on {name}", kind="refused")
        rec = _hub_record(hub, handle)
        _status_check(reg, rec, name)
        if isinstance(body, (bytes, bytearray)):
            if len(body) > nodes.BODY_MAX:
                raise RelayError(413, "too_large", f"the body is over {nodes.BODY_MAX // 1024} KB", kind="refused")
            try:
                body = json.loads(body.decode("utf-8")) if body.strip() else {}
            except (ValueError, UnicodeDecodeError):
                raise RelayError(422, "invalid", "the body is not JSON", kind="refused") from None
        try:
            clean = validate(row, params or {}, body, agent=agent is not None)
        except Invalid as e:
            raise RelayError(e.status, "invalid" if e.status == 422 else "too_large", "; ".join(e.messages), kind="refused") from None
        if row.name == "epic_claim":
            claim_id = secrets.token_hex(16)
            clean["claim_id"] = claim_id
        if row.fill is not None:                                   # what the hub itself holds (an epic's stored settings) replaces whatever the caller put in those fields
            clean = row.fill(clean, d)
        if agent is not None and clean.get("agent") and _not_installed(rec, clean["agent"]):          # an agent names an agent the node has: the hub's own reading of its card says so
            raise RelayError(422, "invalid", f"{clean['agent']} is not installed on {name}; nothing was sent", kind="refused")
        target = safe_target(row, params or {}, clean)            # the body is known and valid now: a task's title (cut, redacted) joins the target, the prompt never does
        extra = row.extra_scope(clean) if row.extra_scope else None
        if extra and agent is not None:                                # an agent never types into a running session of another node: no scope of the token or the pair opens it
            raise RelayError(403, "mcp_session", "an agent cannot hand a task to a running session on another node; it can start the task in a new session there", kind="refused")
        if extra and extra not in (nodes._scopes_of(reg.get("scopes")) or []):
            raise RelayError(409, "scope", f"needs the {extra} scope on {name}", kind="refused")
        if row.per_session:                                         # nodes.PROMPT_RATE a minute into one session; the peer counts again
            ok, wait = nodes.relay_prompt_limiter.allow(f"{handle}/{(params or {}).get('name')}")
            if not ok:
                raise RelayError(429, "rate_limited", f"too many prompts to this session; wait {wait} s", headers={"Retry-After": str(wait)}, kind="refused")
        path = peer_path(row, params or {}, clean)
        try:
            reply = nodes.PeerClient(reg, db=d).request(row.peer_method, path, clean if row.peer_method != "GET" and clean else None,
                                                        acting_user=user, timeout=row.timeout)
        except nodes.PeerError as e:
            raise _map_peer_error(e, reg, row) from None
        if len(reply.body) > row.answer_max:                       # cut by size before the answer is read any further
            raise RelayError(502, "too_large", f"{name} answered more than this row ever needs; nothing was shown", kind="failed")
        data = _map_reply(reply, row, reg, clean)
        shape = row.agent_shape if agent is not None else row.shape      # an agent gets the reduced answer, a person the full one
        if shape is not None:
            from . import nodes_hub
            try:
                data = redact_tree(shape(data, reg))                  # every free-text field of the peer's answer goes through the redaction
            except nodes_hub.Bad as e:
                wrong = "another node" in str(e)
                extra = {} if wrong else _unknown_if_write(row, reg)
                raise RelayError(502, "wrong_node" if wrong else "bad_answer", f"{name} answered with something this board will not show" + _check_note(extra, name), kind="failed",
                                 **extra) from None
    except RelayError as e:
        if claim_id is not None:
            e.extra["claim_id"] = claim_id
        if agent is not None:                                         # the sentence an agent reads can hold the node's name and its own words: cut, cleaned and redacted
            e.message = _cut(e.message, mcp_tools.ERROR_MAX) or e.reason
        if reg is None and e.reason in ("human_only", "forbidden", "csrf", "mcp_off", "mcp_level", "mcp_scope"):             # a caller that may not relay (the hook token, a node user): a row when the handle is a paired node
            try:
                reg = _registry_row(handle, d)
            except RelayError:
                reg = None
            who = getattr(request.state, "user", None)
            user = user or (who if isinstance(who, str) else None)
        if reg is not None:                       # a refusal before any node was found (a bad caller, local, an unknown handle) leaves no row: a single board gets none
            nodes.audit("out", reg.get("peer_id") or "", row.audit_action, False, f"{e.reason}: {e.message}"[:200], node_name=reg.get("name"),
                        user=user, target=target, status=e.kind, db=d)
        raise
    nodes.audit("out", reg["peer_id"], row.audit_action, True, None, node_name=reg.get("name"), user=user, target=target, status="ok", db=d)
    return {"node": handle, "age": 0, "data": data}


def _claimed_user(request) -> str | None:
    u = getattr(request.state, "user", None)
    return u if isinstance(u, str) and not u.startswith("node:") else None


# ================================================================ the peer side

def _acting(request) -> str:
    return nodes._scrub(request.headers.get("x-ccboard-acting-user"), 64) or ""


CALLER: contextvars.ContextVar = contextvars.ContextVar("ccboard_node_caller", default=None)     # {node, user} of the request a peer handler is serving
CALLER_SCOPES: contextvars.ContextVar = contextvars.ContextVar("ccboard_node_scopes", default=None)     # the scopes the pair of that request holds (None: unknown, which holds nothing)


def caller_has(scope: str) -> bool:
    """Does the pair whose request a peer handler is serving hold `scope`? Unknown (no request) holds nothing."""
    return nodes.scope_ok(CALLER_SCOPES.get(), scope)


_CLAIM_BAD = re.compile(r"[^A-Za-z0-9 ._@+:-]")


def _claim(v, n: int) -> str:
    """A name a caller reported, as it may be stored: ASCII letters, digits and ` . _ @ + : -` only (no slash, quote, bracket or control character, no `..`), at most
    `n` characters. It is a label: nothing reads it as a path, a command or a permission."""
    t = _CLAIM_BAD.sub("", str(v or "")[:4 * n])
    t = re.sub(r"\.{2,}", ".", " ".join(t.split()))
    return t.strip(" .")[:n]


def caller_origin() -> dict | None:
    """Who asked, as a peer handler stamps it on the task or session it makes: {node: the paired node's name, user: the login that node reported}. The login is a
    claim (the pair token proves the node, not the person): it is cut to a plain label here and never used to decide anything."""
    c = CALLER.get()
    if not c:
        return None
    node, user = _claim(c.get("node"), 41), _claim(c.get("user"), 64)
    return {k: v for k, v in (("node", node), ("user", user)) if v}


class Refusal(Exception):
    """An answer a peer handler gives with its own status and a stable `reason` word (and a few small facts in `extra`): 409 busy / answered / expired, 404 gone,
    429 rate_limited. serve_peer turns it into the JSON {error, reason, ...}; the hub passes the words from a closed list on (_map_reply)."""

    def __init__(self, status: int, reason: str, message: str, *, headers: dict | None = None, **extra):
        super().__init__(message)
        self.status, self.reason, self.message, self.headers, self.extra = status, reason, message, dict(headers or {}), extra


class RepoMissing(projects.NotFound):
    """The project or repo a node asked for is not on this board (404 with reason `repo_missing`, so the hub can say it in its own words)."""


def _inbound_audit(db, request, row: Row, params: dict, ok: bool, status: str, detail: str | None, clean: dict | None = None) -> None:
    peer = getattr(request.state, "node_peer", None) or {}
    pid = getattr(request.state, "node_pair", None) or ""
    claim = nodes._scrub(request.headers.get("x-ccboard-node"), 64)
    if claim and peer.get("node_id") and claim != peer["node_id"]:
        detail = ((detail + "; ") if detail else "") + "the caller names another node id"
    acting = _acting(request)
    nodes.audit("in", pid, row.audit_action, ok, detail, node_name=peer.get("name"), user=f"for {acting}" if acting else None,
                target=safe_target(row, params or {}, clean), status=status, db=db)


def note_inbound(request, row_name: str, db) -> None:
    """The card and state routes (which already exist) call this: a call that carries an acting user (a hub relaying for a person) is audited, a poll is not."""
    if getattr(request.state, "node_pair", None) and _acting(request):
        _inbound_audit(db, request, BY_NAME[row_name], {}, True, "ok", None)


def serve_peer(row: Row, handler: Callable, request, raw: bytes, db) -> JSONResponse:
    """One request on a peer wrapper (see the module text). Raises the board's own exceptions (Forbidden 403, Unprocessable 422, NotFound 404 ...)."""
    peer = getattr(request.state, "node_peer", None)
    if not peer or not getattr(request.state, "node_pair", None):
        raise projects.Forbidden("this route takes a node token")
    if not nodes.scope_ok(peer.get("scopes"), row.scope):
        _inbound_audit(db, request, row, {}, False, "refused", f"scope {row.scope} not granted")
        raise projects.Forbidden(f"this node token does not hold the {row.scope} scope")
    params = dict(request.path_params)
    if row.acting and not _acting(request):                  # a person's act, relayed by the hub: the hub names the person (a claim the peer cannot verify, but must have)
        _inbound_audit(db, request, row, params, False, "refused", "no acting user named")
        raise projects.Forbidden("this request must name the person acting (X-CCBoard-Acting-User); a hub sends it")
    try:
        if row.peer_method == "GET":
            items = list(request.query_params.multi_items())
            if len({k for k, _ in items}) != len(items):
                raise Invalid(["a query field is given twice"])
            body: object = dict(items)
        elif len(raw) > nodes.BODY_MAX:
            raise projects.BadRequest("the body is too large")
        else:
            try:
                body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except (ValueError, UnicodeDecodeError):
                raise Invalid(["the body is not JSON"]) from None
        clean = validate(row, params, body)
    except Invalid as e:
        _inbound_audit(db, request, row, params, False, "refused", str(e)[:160])
        if e.status == 413:
            return JSONResponse({"error": str(e), "reason": "too_large"}, status_code=413, headers=NO_STORE)
        raise projects.Unprocessable(str(e)) from None
    extra = row.extra_scope(clean) if row.extra_scope else None
    if extra and not nodes.scope_ok(peer.get("scopes"), extra):
        _inbound_audit(db, request, row, params, False, "refused", f"scope {extra} not granted", clean)
        raise projects.Forbidden(f"this node token does not hold the {extra} scope")
    token = CALLER.set({"node": nodes._scrub(peer.get("name"), 41) or "", "user": _acting(request) or None})
    scopes_token = CALLER_SCOPES.set(peer.get("scopes"))
    try:
        out = handler(db, params, clean)
    except RepoMissing as e:
        _inbound_audit(db, request, row, params, False, "failed", "repo_missing", clean)
        return JSONResponse({"error": str(e), "reason": "repo_missing"}, status_code=404, headers=NO_STORE)
    except Refusal as e:
        why = e.extra.get("why")
        _inbound_audit(db, request, row, params, False, "refused" if e.status == 429 else "failed", f"{e.reason}: {why}" if isinstance(why, str) and _WORD.fullmatch(why) else e.reason, clean)
        return JSONResponse({"error": e.message, "reason": e.reason, **e.extra}, status_code=e.status, headers={**NO_STORE, **e.headers})
    except Exception as e:
        _inbound_audit(db, request, row, params, False, "failed", (str(e) if isinstance(e, (projects.NotFound, projects.BadRequest, projects.Conflict)) else e.__class__.__name__)[:120],
                       clean)
        raise
    finally:
        CALLER_SCOPES.reset(scopes_token)
        CALLER.reset(token)
    _inbound_audit(db, request, row, params, True, "ok", None, clean)
    return JSONResponse(out, headers=NO_STORE)


def peer_endpoint(row: Row, handler: Callable, get_db: Callable):
    async def endpoint(request: Request):
        raw = await request.body() if row.peer_method != "GET" else b""
        return await asyncio.to_thread(serve_peer, row, handler, request, raw, get_db())
    endpoint.__name__ = f"node_{row.name}"
    return endpoint


def hub_endpoint(row: Row, get_db: Callable, get_hub: Callable):
    async def endpoint(request: Request):
        params = dict(request.path_params)
        handle = params.pop("handle", "")
        body: object
        if row.hub_method == "GET":
            items = list(request.query_params.multi_items())
            body = {"query": "a query field is given twice"} if len({k for k, _ in items}) != len(items) else dict(items)
        else:
            try:
                size = int(request.headers.get("content-length") or 0)
            except ValueError:
                size = 0
            if size > nodes.BODY_MAX:                         # not even read: the caller is a person, but the cap is the same everywhere
                return RelayError(413, "too_large", f"the body is over {nodes.BODY_MAX // 1024} KB").response(handle if nodes.valid_handle(handle) else None)
            body = await request.body()
        try:
            out = await asyncio.to_thread(relay, handle, row, params, body, request, db=get_db(), hub=get_hub())
        except RelayError as e:
            return e.response(handle if nodes.valid_handle(handle) else None)
        return JSONResponse(out, headers=NO_STORE)
    endpoint.__name__ = f"relay_{row.name}"
    return endpoint


def install(app, handlers: dict[str, Callable], get_db: Callable, get_hub: Callable, rows=RELAY) -> list:
    """Add the hub route and the peer wrapper of every row to `app` (a FastAPI app); returns the routes added. A row without a handler is an error, so a row
    cannot ship half done. Called once by app/main.py with this module's handlers."""
    before = len(app.router.routes)
    for r in rows:
        if not r.peer_exists and r.name not in handlers:
            raise RuntimeError(f"relay row {r.name} has no peer handler")
        app.add_api_route(r.hub_path, hub_endpoint(r, get_db, get_hub), methods=[r.hub_method], name=f"relay_{r.name}", include_in_schema=False)
        if not r.peer_exists:
            app.add_api_route(r.peer_path, peer_endpoint(r, handlers[r.name], get_db), methods=[r.peer_method], name=f"node_{r.name}", include_in_schema=False)
    return app.router.routes[before:]


# ================================================================ MCP across nodes (issue #152): what an agent may see of the paired nodes

def mcp_nodes_view(hub) -> dict:
    """What `list_nodes` answers (an agent's view, issue #152): {level, allows: {read, tasks}, nodes: [{handle, name, status, age_s, free_lanes, agents: [{name, ready}]}]}, and nothing else.
    Built field by field from the hub read model (nothing is asked of a node); never an address, a node id, a peer id, a token, an account label or login detail, a usage window, a version,
    a scope of the pair or a lane cap. `ready` is true when the agent is installed and logged in on that node. A value the hub has not read is null, never 0: `agents` is null until a card
    was read, `free_lanes` until a state was. Works with CCBOARD_MCP_NODES off (it only lists); `level` is off, read or tasks, and `allows` says what a tool call may do on a node."""
    level = mcp_level()
    granted = MCP_LEVELS.get(level, ())
    out = []
    for r in hub.records():
        h = r.get("handle")
        if not nodes.valid_handle(h):
            continue
        card, st = r.get("card") if isinstance(r.get("card"), dict) else None, r.get("state") if isinstance(r.get("state"), dict) else None
        agents = None
        if card is not None and isinstance(card.get("agents"), list):
            agents = []
            for a in card["agents"][:8]:
                if not isinstance(a, dict):
                    continue
                inst, login, problem = a.get("installed"), a.get("logged_in"), a.get("login_problem")
                agents.append({"name": mcp_tools.untrusted_shaped(a.get("id"), _AG_WORD, 20),
                               "ready": (inst is True and login is True and problem is not True) if isinstance(inst, bool) and isinstance(login, bool) else None})
        free = ((st.get("lanes") or {}).get("free") if isinstance(st.get("lanes"), dict) else None) if st is not None else None
        out.append({"handle": h, "name": mcp_tools.untrusted_text(r.get("name"), mcp_tools.NAME_MAX), "status": mcp_tools.untrusted_shaped(r.get("status"), _AG_WORD, 20),
                    "age_s": int(r["age_s"]) if isinstance(r.get("age_s"), (int, float)) and not isinstance(r.get("age_s"), bool) and 0 <= r["age_s"] < 10 ** 9 else None, "free_lanes": free if isinstance(free, int) and not isinstance(free, bool) else None, "agents": agents})
    return {"level": level or "off", "allows": {"read": "read" in granted, "tasks": "tasks" in granted}, "nodes": out}


# ================================================================ the peer's internal functions (what the wrappers call)

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2064\ufeff]")


REDACTED = "[redacted]"
MARGIN = 128                                                   # a secret that straddles a line's 400th character is redacted whole before the final cut
LINE_IN = PANE_LINE_MAX + MARGIN
REDACT_MAX = PANE_LINES * (LINE_IN + 1)                        # the most text any regex of this module is given, whoever asks (the pane's 40 lines)
# Every quantifier below is bounded and no two unbounded ones overlap, so no input can make a pattern backtrack more than a few hundred steps per start
# position; and no peer text reaches one before it was cut to PANE_LINES lines of LINE_IN characters (redact() cuts again, whoever calls it).
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]{1,40}-----[\s\S]*?(?:-----END [A-Z0-9 ]{1,40}-----|\Z)")
_PEM_TAIL = re.compile(r"\A[\s\S]*?-----END [A-Z0-9 ]{1,40}-----")           # the tail of a screen can start inside a key block
_OWN_TOKEN = re.compile(r"cc(?:bnode|bmcp)_[A-Za-z0-9_-]{6,200}")
# Shapes of a token that is one word: found on a line, and again on the lines joined (a token tmux wrapped over two lines).
_TOKEN_SHAPES = (
    _OWN_TOKEN,
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,200}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,200}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,200}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,200}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,200}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    re.compile(r"\b[spr]k_(?:live|test)_[0-9A-Za-z]{10,100}"),
    re.compile(r"\bwhsec_[0-9A-Za-z]{10,100}"),
    re.compile(r"\bnpm_[A-Za-z0-9]{30,100}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,100}"),
    re.compile(r"\bhf_[A-Za-z0-9]{30,100}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{6,200}\.[A-Za-z0-9_-]{6,200}\.[A-Za-z0-9_-]{6,200}"),
)
# The same shapes without their word boundaries, for the lines joined end to end (a token that starts a line has a letter right before it there).
_WRAP_SHAPES = tuple(re.compile(rx.pattern.replace(r"\b", "")) for rx in _TOKEN_SHAPES)
_NAME = r"[A-Za-z0-9_.-]{0,40}"
_KEYWORD = (r"(?:password|passwd|passphrase|pwd|token|secret|credential|api[_-]?key|apikey|access[_-]?key|private[_-]?key|auth[_-]?key"
            r"|(?:(?<=[_.-])|\b)key(?![A-Za-z]))")
_VALUE = r"(\"[^\"\n]{0,300}\"|'[^'\n]{0,300}'|[^\s\"',;]{1,300})"
_FLAG = (r"(?<![A-Za-z0-9])(--?[a-z0-9-]{0,30}(?:token|password|passwd|secret|api-?key|apikey|credential|auth-?key)[a-z0-9-]{0,30})([ \t]{1,8}|=)"
         r"((?![-=:])\"[^\"\n]{0,300}\"|(?![-=:])'[^'\n]{0,300}'|(?![-=:])[^\s\"']{1,300})")


def _basic(m):
    """`Basic <base64>` only when the word after it looks like base64 and not like a plain English word ("Basic configuration")."""
    v = m.group(1)
    if any(c.isdigit() or c in "+/=" for c in v) or any(c.isupper() for c in v[1:]):
        return "Basic " + REDACTED
    return m.group(0)


_KW = re.compile(_KEYWORD, re.I)
_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
_AFTER = re.compile(r"([\"']?[ \t]{0,8}[=:][ \t]{0,8})(\"[^\"\n]{0,300}\"|'[^'\n]{0,300}'|[^\s\"',;]{1,300})")


def _redact_kv(t: str) -> str:
    """`NAME=VALUE`, `export NAME=VALUE`, `NAME: VALUE`, `"name": "value"` for a NAME that holds password, token, secret, credential or key as a word of its own.
    A scan, not one big pattern: find the word, widen the name by at most 40 characters each way, then read the value; every step is bounded, so it is linear."""
    out, pos = [], 0
    for m in _KW.finditer(t):
        if m.start() < pos:
            continue
        b, limit = m.end(), min(len(t), m.end() + 40)
        while b < limit and t[b] in _NAME_CHARS:
            b += 1
        after = _AFTER.match(t, b)
        if after is None:
            continue
        out.append(t[pos:b])
        out.append(after.group(1))
        out.append(REDACTED)
        pos = after.end()
    if not out:
        return t
    out.append(t[pos:])
    return "".join(out)


class _KVRule:
    """Stands in for a compiled pattern in _SHAPES: `.sub(repl, text)` runs the scan; `.pattern` names the words the hint check looks for."""
    pattern = "password token secret credential key (the NAME=VALUE scan)"

    @staticmethod
    def sub(repl, t):
        return _redact_kv(t)


_KV = _KVRule()
_URL_USER = re.compile(r"([A-Za-z][A-Za-z0-9+.-]{1,20}://)[^\s/@?#]{1,200}@")
_SHAPES = (
    (re.compile(r"(?im)\b(?:proxy-)?authorization[ \t]{0,4}[:=][^\n]*"), "Authorization: " + REDACTED),
    (re.compile(r"(?im)\b(?:set-)?cookie[ \t]{0,4}:[^\n]*"), "Cookie: " + REDACTED),
    (re.compile(r"(?im)\bx-[a-z0-9-]{0,40}(?:key|token|secret|auth|password|credential|signature)[a-z0-9-]{0,40}[ \t]{0,4}:[^\n]*"), "X-header: " + REDACTED),
    (re.compile(r"(?i)\bbearer[ \t]{1,4}[A-Za-z0-9._~+/=-]{12,300}"), "Bearer " + REDACTED),
    (re.compile(r"(?i)\bbasic[ \t]{1,4}([A-Za-z0-9+/]{8,200}={0,2})"), _basic),
    (_URL_USER, lambda m: m.group(1) + REDACTED + "@"),       # user:password@host, token@host
    *((rx, REDACTED) for rx in _TOKEN_SHAPES[1:]),
    (re.compile(r"\b[A-Za-z0-9_-]{16,200}\.[A-Za-z0-9_-]{16,200}\.[A-Za-z0-9_-]{16,200}\b"), REDACTED),
    (re.compile(r"(?i)" + _FLAG), lambda m: m.group(1) + m.group(2) + REDACTED),
    (_KV, None),
    (re.compile(r"([=:][ \t]{0,8})[A-Za-z0-9+_-]{32,400}={0,2}"), lambda m: m.group(1) + REDACTED),
)


_SECRET_WORDS = ("pass", "pwd", "token", "secret", "credential", "key")
# A pattern runs only when the (lower-case) text holds one of its words: most screens never reach the heavy ones.
_HINT: dict = {}
for _rx, _ in _SHAPES:
    _p = _rx.pattern
    _HINT[_rx] = (("authorization",) if "authorization" in _p else ("cookie",) if "cookie" in _p else ("x-",) if _p.startswith("(?im)\\bx-") else ("bearer",) if "bearer" in _p
                  else ("basic",) if "basic" in _p else ("://",) if "://" in _p else (".",) if _p.startswith("\\b[A-Za-z0-9_-]{16,200}\\.") else ("=", ":") if _p.startswith("([=:]")
                  else ("-",) if "(?<![A-Za-z0-9])(--?" in _p else _SECRET_WORDS if "password" in _p else None)


class _Secret(str):
    """A secret value this process knows, as a string that never shows itself in a repr, a traceback or a log line."""

    def __repr__(self) -> str:
        return "<secret>"


def redact(text: str, known=(), strict: bool = True) -> str:
    """A screen's text with secrets replaced by `[redacted]`. The input is cut to REDACT_MAX characters first, so no regex ever sees more. In order: key blocks of
    any label, the values in `known` (this process's own tokens, compared by value with str.replace, never logged), this board's node and device tokens, URL
    credentials (`scheme://user:password@host`, connection strings), `Authorization`, `Cookie`, `Set-Cookie` and `X-...-Key` header lines, `Bearer` and `Basic`
    values, the common key shapes (Anthropic, OpenAI-style, GitHub, AWS, Google, Stripe, npm, GitLab, Hugging Face, Slack, JWT), `--token VALUE` flags, the
    value after a name that holds password, token, secret, credential or key (`NAME=VALUE`, `export NAME=VALUE`, `NAME: VALUE`, quoted or not), and 32 or more
    key characters after `=` or `:`. Bare git hashes stay. `strict=False` keeps only key blocks, the known values, token shapes and URL credentials (for help
    text). Best effort, not a guarantee: a secret in a shape that is not here goes through, which is why the pane row needs the `sessions` scope and a signed-in
    person, and why `read` never shows screen text."""
    t = str(text or "")[:REDACT_MAX]
    if strict:
        t = _PEM_TAIL.sub(REDACTED, _PEM.sub(REDACTED, t))
    else:
        t = _PEM.sub(REDACTED, t)
    for k in known:
        if isinstance(k, str) and len(k) >= 8:
            t = t.replace(k, REDACTED)
    if not strict:
        t = _URL_USER.sub(lambda m: m.group(1) + REDACTED + "@", t)
        for rx in _TOKEN_SHAPES:
            t = rx.sub(REDACTED, t)
        return t
    t = _OWN_TOKEN.sub(REDACTED, t)
    low = t.lower()
    for rx, repl in _SHAPES:
        hint = _HINT.get(rx)
        if hint is not None and not any(h in low for h in hint):
            continue
        t = rx.sub(repl, t)
        low = t.lower() if hint is not None else low
    return t


def known_secrets() -> list[str]:
    """The secret values this process holds that could land on a screen: the hook token and the hub token. Read from memory, never logged, returned in a
    repr or put in an exception (they are `_Secret`s)."""
    out: list[str] = []
    try:
        from . import hooks
        out.append(hooks._token or "")
    except Exception:
        pass
    try:
        from .config import settings
        out.append(settings.hub_token or "")
    except Exception:
        pass
    return [_Secret(k) for k in out if k]


def redact_tree(v, known=None, depth: int = 6, loose: tuple = ("help",)):
    """A peer's answer after the hub rebuilt it: every string in it goes through redact(), so a secret a person typed into a task title or a branch name does not
    reach the browser. Values under a key in `loose` (launcher help text, which is prose) get the lighter pass."""
    k = known_secrets() if known is None else known
    if isinstance(v, str):
        return redact(v, k) if len(v) >= 6 else v
    if depth <= 0:
        return v
    if isinstance(v, list):
        return [redact_tree(x, k, depth - 1, loose) for x in v]
    if isinstance(v, dict):
        return {key: (x if key == "ref" and _is_ref(x) else redact(x, k, strict=False) if key in loose and isinstance(x, str) else redact_tree(x, k, depth - 1, loose))
                for key, x in v.items()}
    return v


def _is_ref(v) -> bool:
    """A string in the ref grammar (`<handle>:<id>`, `<handle>/<session>`) whose part after the handle holds nothing redact() would change: the hub builds these
    itself, and a handle such as `token` (which the registry chose, not the peer) must not be mistaken for a secret."""
    try:
        r = nodes.parse(v) if isinstance(v, str) else None
    except ValueError:
        return False
    return r is not None and r.handle is not None and redact(r.rest, known_secrets()) == r.rest


def _hide_wrapped(lines: list[str], known) -> list[str]:
    """A secret that tmux wrapped over two or more lines: look for the known values and the token shapes in the lines joined end to end, and hide every part of a
    match that crosses a line break (each fragment becomes `[redacted]`). tmux wraps at the pane's width, so only a line as long as the longest line of the screen
    is joined to the next one; every other break is a real one and no match crosses it."""
    if len(lines) < 2:
        return lines
    width = max(len(x) for x in lines)
    parts, starts, bounds, pos = [], [], [], 0
    for i, x in enumerate(lines):
        starts.append(pos)
        parts.append(x)
        pos += len(x)
        if i < len(lines) - 1:
            if len(x) >= width:
                bounds.append(pos)                      # a wrapped line: the next one continues it
            else:
                parts.append("\x00")                    # a real line break: nothing matches across it
                pos += 1
    if not bounds:
        return lines
    joined = "".join(parts)
    spans: list[tuple[int, int]] = []
    for k in known:
        if isinstance(k, str) and len(k) >= 8:
            i = joined.find(k)
            n = 0
            while i >= 0 and n < 20:
                spans.append((i, i + len(k)))
                i, n = joined.find(k, i + 1), n + 1
    for rx in _WRAP_SHAPES:
        spans.extend(m.span() for m in rx.finditer(joined))
    hidden = bytearray(len(joined))
    for a, b in spans:
        if any(a < x < b for x in bounds):
            hidden[a:b] = b"\x01" * (b - a)
    if not any(hidden):
        return lines
    out = []
    for x, off in zip(lines, starts):
        seg, buf, run = hidden[off:off + len(x)], [], False
        for ch, h in zip(x, seg):
            if h:
                if not run:
                    buf.append(REDACTED)
                run = True
            else:
                run = False
                buf.append(ch)
        out.append("".join(buf))
    return out


def _clean_screen_line(x: str) -> str:
    x = str(x)[:LINE_IN].replace("\t", "    ")
    return "".join(c for c in x if unicodedata.category(c) not in ("Cc", "Cf"))[:LINE_IN]      # control, format (zero-width, soft hyphen, bidi) characters


def screen_lines_from(lines, n: int = PANE_LINES) -> list[str]:
    """Screen lines as a peer may send them. The caps come FIRST (the last n <= 40 lines, each cut to LINE_IN characters), then the invisible characters go (so a
    secret cannot hide behind one), then the redaction, then the wrapped-secret pass, then the final cut of each line to PANE_LINE_MAX."""
    n = max(1, min(int(n), PANE_LINES))
    keep = [_clean_screen_line(x) for x in list(lines)[-n:] if isinstance(x, str)]
    known = known_secrets()
    keep = _hide_wrapped(keep, known)                   # first: the per-line pass would redact one fragment and leave the rest of a wrapped token looking like a word
    return [clean_line(x) for x in redact("\n".join(keep), known).split("\n")][-n:]


def screen_lines(text: str, n: int) -> list[str]:
    """The last `n` lines of a captured screen: tail_lines first (no regex), then screen_lines_from."""
    t = str(text or "")[-262144:].replace(chr(0x2028), "\n").replace(chr(0x2029), "\n")
    return screen_lines_from(tail_lines(t, n), n)


def tail_lines(text: str, n: int) -> list[str]:
    """The last `n` visible lines of a captured pane: trailing blanks dropped, each line stripped of its right-hand blanks. The board's live stream and the
    node pane row both use it."""
    tail = [ln.rstrip() for ln in (text or "").splitlines()]
    while tail and not tail[-1]:
        tail.pop()
    return tail[-n:]


def clean_line(line: str) -> str:
    """One screen line as a peer may send it: tabs become spaces, control and invisible characters go, anything shaped like a node or device token is replaced,
    at most PANE_LINE_MAX characters."""
    t = _CTRL.sub("", str(line).replace("\t", "    "))
    return nodes._TOKENISH.sub("[token]", t)[:PANE_LINE_MAX].rstrip()


def peer_task(db, params: dict, body: dict) -> dict:
    """A task's detail for a paired node: the state row (title, phase, agent, project, repo, branch, tmux, issue, updated) plus a few facts. Never the prompt,
    the result, a worktree path or a session id."""
    from . import main, node_state
    main._merged_sessions()
    t = db.task_get(int(params["tid"]))
    if not t:
        raise projects.NotFound("no such task")
    c = node_state.clean
    out = node_state._task_row(t)
    out.update({"slug": c(t.get("slug"), 80), "mode": c(t.get("mode") or "worktree", 20), "base": c(t.get("base")), "created_at": c(t.get("created_at"), 40),
                "assigned_at": c(t.get("assigned_at"), 40), "done_at": c(t.get("done_at"), 40),
                "pr_number": t["pr_number"] if isinstance(t.get("pr_number"), int) and not isinstance(t.get("pr_number"), bool) else None,
                "pr_state": c(t.get("pr_state"), 20), "has_result": bool(t.get("result"))})
    return out


def peer_pane(db, params: dict, body: dict) -> dict:
    """The last lines (at most PANE_LINES) of one session's screen, cleaned. 400 for a name that is no ccboard session, 404 for one tmux does not have and for
    an internal one (the login session shows codes)."""
    name = params["name"]
    if tmux.is_internal(name) or not tmux.has_session(name):
        raise projects.NotFound(f"session {name} not found")
    n = int(body.get("lines", PANE_LINES))
    text = tmux.capture(name, lines=n, join=False)
    return {"name": name, "lines": screen_lines(text, n), "cap": PANE_LINES}


_ONLY = {"permissionmode": PERMISSION_MODES, "mode": SESSION_MODES, "sandbox": SANDBOXES, "approval": APPROVALS, "approvalpolicy": APPROVALS}


def _remote_option(f: dict) -> dict | None:
    """One launcher control as a node may use it: None for a control the guard refuses (a bypass, the arguments, the tools, the config, the directories),
    and the choices of the others cut to the values the guard lets through (and no `shell` launcher)."""
    key = _norm_key(f.get("key"))
    if f.get("danger") or key in NEVER_FIELDS or key in ("profile",) or any(w in key for w in _SPELL_KEYS) or f.get("kind") in ("args", "dirs"):
        return None
    f = dict(f)
    if isinstance(f.get("choices"), list):
        only = _ONLY.get(key)
        f["choices"] = [c for c in f["choices"] if (c in only if only is not None else not (isinstance(c, str) and _squash(c).startswith(("shell", "danger", "bypass"))))]
    return f


def peer_agents(db, params: dict, body: dict) -> dict:
    """The agents this board can launch, built field by field (the adapter's own `auth` block can hold an e-mail address and never leaves): name, label,
    glyph, installed, version, logged_in, hooks, and the launcher's option schema with the controls and values a node may not use taken out."""
    from . import agents
    out = []
    for a in agents.all():
        d = a.describe()
        auth = d.get("auth") if isinstance(d.get("auth"), dict) else {}
        out.append({"name": d.get("name"), "label": d.get("label"), "glyph": d.get("glyph"), "installed": bool(d.get("installed")), "version": d.get("version"),
                    "logged_in": bool(auth.get("loggedIn")), "hooks": bool((d.get("hooks") or {}).get("installed")),
                    "options": [o for o in (_remote_option(f) for f in d.get("options") or []) if o is not None],
                    "permission_modes": [m for m in (d.get("permission_modes") or []) if m in PERMISSION_MODES],
                    "efforts": d.get("efforts") or [], "models": d.get("models") or [], "reasoning_by_model": d.get("reasoning_by_model") or {}})
    return {"agents": out}


def _repo_here(project: str, repo: str) -> None:
    """RepoMissing unless the project folder (repo `root`) or the repo's folder exists on this board. Nothing is created or cloned for the caller."""
    rpath = projects.repo_path(project, repo)
    if not rpath.is_dir():
        what = f"project {project}" if repo == projects.ROOT else repo
        raise RepoMissing(f"{what} is not on {nodes.display_name()}")


def _task_answer(db, res: dict) -> dict:
    """What a create or dispatch tells the hub: {id, slug, tmux, branch, phase, task, limit_warning?} read back from the task row (the board's own answer holds a
    worktree path, a session row id and the head of a backlog prompt, none of which leave this board), plus the session dispatch flags."""
    from . import node_state
    t = db.task_get(int(res["id"])) or {}
    out = {"id": t.get("id"), "slug": t.get("slug"), "tmux": t.get("tmux_name") or None, "branch": t.get("branch") or None, "phase": t.get("phase") or "running",
           "task": node_state._task_row(t)}
    for k in ("pasted", "queued", "held"):
        if isinstance(res.get(k), bool):
            out[k] = res[k]
    if isinstance(res.get("limit_warning"), dict):
        out["limit_warning"] = res["limit_warning"]
    return out


def _task_launch_kw(agent: str, spec: dict | None = None) -> dict:
    """The permission options a task launched for another node always carries, as TaskCreateIn / DispatchIn fields. The card's own choice when it is inside the
    allowed set, else the safe constant (safe_launch); never this board's settings, the person's remembered choices or the CLI's own config. No subagent model
    either: CCBOARD_SUBAGENT_MODEL is the owner's choice for the owner's launches, so a remote launch says `inherit` itself."""
    spec = spec or {}
    opts = spec.get("opts") if isinstance(spec.get("opts"), dict) else {}
    safe = safe_launch(agent, None, spec.get("permission_mode"), opts.get("sandbox"), opts.get("approval"))
    if agent == "claude":
        return {**safe, "subagent_model": "inherit", "subagent_force": False, "opts": {}}
    return {"permission_mode": "default", "opts": safe}


def _require_final(main, req, extra: dict | None = None) -> None:
    """The guard on the options a launch ENDS UP with (the body, the card and the safe constants merged), as well as on the body: a refusal here is a bug or a
    card the owner saved with more than another node may use. Raises Conflict (409) naming what, never a value of the prompt."""
    fields = req.model_dump(include=set(main.TASK_SPEC_KEYS) | {"agent"})          # (a DispatchIn's own `mode` is lane or session: not a permission word)
    merged = {**fields, **(extra or {})}
    bad = guard_final(merged)
    if merged.get("subagent_model") not in (None, "", "inherit") or merged.get("subagent_force"):
        bad.append("a subagent model override is the owner's own choice")
    if bad:
        raise projects.Conflict("this launch would go beyond what another node may use: " + "; ".join(bad)[:200])


def peer_task_create(db, params: dict, body: dict) -> dict:
    """POST /api/node/tasks: the board's own task create (main._tasks_create), with `origin` set to the caller. A missing project or repo is a 404 and nothing is made."""
    from . import main
    _repo_here(body["project"], body["repo"])
    extra = {}
    if body.get("issue_ref"):
        owner, name, num = _ISSUE_REF.fullmatch(body["issue_ref"]).groups()
        extra = {"issue_number": int(num), "issue_url": f"https://github.com/{owner}/{name}/issues/{int(num)}"}
    req = main.TaskCreateIn(project=body["project"], repo=body["repo"], title=body["title"], prompt=body["prompt"], when=body.get("when", "now"),
                            agent=body.get("agent"), model=body.get("model"), effort=body.get("effort"), reasoning_effort=body.get("reasoning_effort"),
                            auto_close=body.get("auto_close"), **extra, **_task_launch_kw(body.get("agent") or "claude"))
    _require_final(main, req)
    return _task_answer(db, main._tasks_create(req, origin=caller_origin()))


def peer_task_dispatch(db, params: dict, body: dict) -> dict:
    """POST /api/node/tasks/{tid}/dispatch: the board's own dispatch (main._task_dispatch). A card saved on this board with options a request from another node may
    not carry (extra args, tools, directories, a permission mode outside the three) is not started from here: its owner starts it on this node."""
    from . import main
    tid = int(params["tid"])
    t = db.task_get(tid)
    kw: dict = {}
    # the stored card's own strings and options, by the same rules as a request body; its prompt may not start with / ! or # in either mode (typed into a session, or a launch argument)
    why_card = card_refusal(t, main._task_spec(t), typed=True) if t else None
    if t and not why_card and t.get("phase") == "queued" and t.get("parent_id") is not None:       # a chain step starts as composed with its parent's result
        parent = db.task_get(t["parent_id"])
        if parent and starts_like_a_command(main.taskflow.compose_prompt(t["prompt"], parent)):
            why_card = "command"
    if why_card == "text":
        raise projects.Conflict(f"this card holds a control character or an unusual one (format or line separator), which is not typed into a terminal from another node: start it on {nodes.display_name()}")
    if why_card == "command":
        raise projects.Conflict(f"this card's prompt starts with / ! or #, which the terminal reads as a command: start it on {nodes.display_name()}")
    if t and body.get("mode") == "session":
        why = session_target_refusal(db, t, body.get("session") or "")      # the TARGET's launch options, not the card's: the prompt runs with the session's permissions
        if why:
            raise projects.Conflict(why)
    if t and body.get("mode") == "lane":
        spec = {k: v for k, v in main._task_spec(t).items() if k in main.TASK_SPEC_KEYS}
        if why_card or guard_final(spec):
            raise projects.Conflict(f"this card was saved with launch options that a request from another node may not use: start it on {nodes.display_name()}")
        kw = _task_launch_kw(body.get("agent") or t.get("agent") or "claude", spec)
    req = main.DispatchIn(mode=body.get("mode"), session=body.get("session"), agent=body.get("agent"), model=body.get("model"), effort=body.get("effort"),
                          reasoning_effort=body.get("reasoning_effort"), auto_close=body.get("auto_close"), **kw)
    if kw:
        _require_final(main, req, {**{k: v for k, v in (main._task_spec(t) if t else {}).items() if k in main.TASK_SPEC_KEYS}, **kw})
    res = main._task_dispatch(tid, req, origin=caller_origin())
    if isinstance(res, JSONResponse):                        # the session dispatch answers its refusals (busy, waiting, another repo) as a 409 body, not an exception
        try:
            msg = json.loads(res.body).get("error")
        except (ValueError, AttributeError):
            msg = None
        raise projects.Conflict(msg if isinstance(msg, str) and msg else "the session cannot take the task now")
    return _task_answer(db, res)


def peer_session_open(db, params: dict, body: dict) -> dict:
    """POST /api/node/sessions: the board's own new-session route (main._session_create) for a plain session of the launcher; the origin goes in its flags."""
    from . import main
    _repo_here(body["project"], body["repo"])
    agent = body.get("agent") or "claude"
    # The permissions are always on the line: what the body chose (inside the allowed set) or the safe constant. A line without them leaves the mode to Claude Code's
    # settings.json (defaultMode) or Codex's config.toml, which can say bypass; `default` is Claude's `manual` here (the adapter's own word, accepted by every claude).
    safe = safe_launch(agent, body.get("mode"), body.get("permission_mode"), body.get("sandbox"), body.get("approval"))
    kw = {**safe, "subagent_model": "inherit", "subagent_force": False} if agent == "claude" else safe
    req = main.SessionIn(launcher="claude", agent=body.get("agent"), name=body.get("name"), model=body.get("model"), effort=body.get("effort"),
                         reasoning_effort=body.get("reasoning_effort"), **kw)
    bad = guard_final({k: v for k, v in req.model_dump().items() if k != "launcher"})
    if bad:
        raise projects.Conflict("this session would go beyond what another node may use: " + "; ".join(bad)[:200])
    with _OPEN_LOCK:                                        # the free name and the tmux session are one step: two opens cannot choose the same `auto` name
        out = main._session_create(body["project"], body["repo"], req, origin=caller_origin())
    return {"tmux": out["tmux"], "agent": out["agent"], "project": body["project"], "repo": body["repo"]}


_OPEN_LOCK = threading.Lock()


# ---- steering a session and answering a permission (issue #142). Each handler calls the board's own internal function; nothing here is an HTTP call to itself.

GONE = "This request is gone. Open terminal to see the current prompt."
ANSWERED = "This request was already answered. Open terminal to see the current prompt."
EXPIRED = "This request has expired. Open terminal to see the current prompt."


def steer_refusal(db, name: str) -> str | None:
    """Why text or a key may not be typed into the session `name` from another node (None: it may go on to the board's own checks). The same check a dispatch makes of its
    target (session_target_refusal: one of this board's own agent sessions, with a recorded launch line inside the allowed set; a bypass or unreadable mode fails closed), with the
    project and repo taken from the name itself. A session that does not exist is left to the board (404)."""
    try:
        sp, sr, _ = tmux.split_name(name)
    except ValueError:
        return "not a ccboard session name"
    return session_target_refusal(db, {"project": sp, "repo": sr}, name)


def _alive(name: str) -> bool:
    try:
        return bool(name) and tmux.has_session(name)
    except (tmux.TmuxError, tmux.TmuxDown):
        return False


def _session_here(name: str) -> None:
    """404 for a session tmux does not have and for an internal one (the login session shows codes); the name was checked already."""
    if tmux.is_internal(name) or not tmux.has_session(name):
        raise projects.NotFound(f"session {name} not found")


def _by() -> str:
    """Who asked, as this board's event log and permission row name them: `node <name> for <login>`, from the claims `caller_origin` cleaned."""
    o = caller_origin() or {}
    return (f"node {o.get('node') or '?'}" + (f" for {o['user']}" if o.get("user") else ""))[:100]


def _standin() -> SimpleNamespace:
    """The `request` the board's own route functions read (request.state.user), for a call made in-process on behalf of a node."""
    return SimpleNamespace(state=SimpleNamespace(user=_by()))


def _typing_busy(res: JSONResponse) -> Refusal:
    """The board's 409 for a pane it will not type into now ({error: code, message, state, wait_kind, retry}) as a Refusal: the peer's own words and retry seconds."""
    try:
        b = json.loads(res.body)
    except (ValueError, AttributeError):
        b = {}
    b = b if isinstance(b, dict) else {}
    msg = b.get("message") if isinstance(b.get("message"), str) else "the session cannot take this now"     # a Codex pane's exit reason is read off the screen: clean and redact it
    msg = redact(_text(msg, 200), known_secrets()) or "the session cannot take this now"
    retry = b.get("retry") if isinstance(b.get("retry"), int) and not isinstance(b.get("retry"), bool) else None
    return Refusal(409, "busy", msg, code=b.get("error") if isinstance(b.get("error"), str) else None, retry=retry,
                   state=b.get("state") if isinstance(b.get("state"), str) else None, wait_kind=b.get("wait_kind") if isinstance(b.get("wait_kind"), str) else None)


def _steerable(db, name: str) -> None:
    """Every check before text or a key reaches a session: the session is there (404), not internal, and one of this board's own agent sessions inside the allowed set (409)."""
    from . import main
    main._terminal_session(name)
    _session_here(name)
    why = steer_refusal(db, name)
    if why:
        raise projects.Conflict(why)


ANSWER_KEYS = ("Enter", *LITERAL_KEYS)                       # the keys that choose an option of a dialog: y, n, 1 to 9 and Enter
ASKING_MESSAGE = "this session is asking a question in its terminal"


def asking_reason(db, name: str) -> str | None:
    """Is the session `name` asking a question in its terminal, so that a key or a line typed now would ANSWER it? A word for what is known, None when it is at its prompt
    or working. Every signal the board has, and a state that cannot be read counts as asking (fail closed): a permission request a hook is still waiting on (`permission`);
    a session that is waiting on anything but its idle prompt (a permission or elicitation dialog, a kind the board cannot name), one with no state yet (it may be behind the
    trust dialog) or one that has ended (`dialog`); a Codex pane that shows its update or trust dialog, or has left for the shell (`pane`); a row, a flag or a pane that cannot
    be read (`unknown`)."""
    from . import main
    try:
        row = db.open_row(name)
        if row is None:
            return "unknown"
        if main._permission_pending(name):
            return "permission"
        flags = row.get("flags") or {}
        state = row.get("state")
        if state == "waiting":
            if flags.get("wait_kind") != "idle":
                return "dialog"
        elif state not in ("working", "idle", "done", "errored"):
            return "dialog"
        if main._pane_block(name, row) is not None:
            return "pane"
    except Exception:
        return "unknown"
    return None


def _dialog_gate(row_name: str, name: str, reason: str) -> None:
    """The session asks a question in its terminal: the row's `dialog_scope` (permissions) is the only scope that may act on it, and a pair without it is refused (403 `asking`;
    the hub says it needs that scope). A pair WITH it goes on to the row's own rule about what may be typed."""
    need = BY_NAME[row_name].dialog_scope
    if need and not caller_has(need):
        raise Refusal(403, "asking", f"needs the {need} scope on {nodes.display_name()}: {ASKING_MESSAGE}")


def peer_prompt(db, params: dict, body: dict) -> dict:
    """POST /api/node/sessions/{name}/prompt: the board's own prompt route (paste into the composer, Enter) for a session that passes steer_refusal, with the same state
    refusals (a working session takes it only with `queue`). The text is typed once and not kept: the BoardPrompt event says how long it was, not what."""
    from . import main
    name = params["name"]
    _steerable(db, name)
    asking = asking_reason(db, name)
    if asking:                                              # a line pasted into a question answers it: without `permissions` a 403, and even with it the board's own busy 409 below
        _dialog_gate("prompt", name, asking)
        if asking == "unknown":                             # a row or a state that cannot be read: nothing is typed (the board's own checks below would need that row)
            raise Refusal(409, "busy", f"{ASKING_MESSAGE}, or its state cannot be read: nothing was typed", code="asking", retry=None)
    ok, wait = nodes.node_prompt_limiter.allow(name)
    if not ok:
        raise Refusal(429, "rate_limited", f"this session takes {nodes.PROMPT_RATE} prompts a minute; wait {wait} s", headers={"Retry-After": str(wait)}, retry=wait)
    row, _, refusal = main._agent_row(name)
    if refusal:
        raise _typing_busy(refusal)
    refusal = main._typing_refusal(name, row, queue=bool(body.get("queue")))
    if refusal:
        raise _typing_busy(refusal)
    text = body["text"]
    out = main._paste_prompt(name, row, text, True, _by(), event_text=f"prompt from another node ({len(text)} characters)")
    return {"ok": True, "pasted": out["pasted"], "queued": out["queued"]}


def peer_keys(db, params: dict, body: dict) -> dict:
    """POST /api/node/sessions/{name}/keys: one key of STEER_KEYS through the board's own keys route. y, n and 1 to 9 are typed as a character (tmux's key names do not
    list them); the rest are tmux key names the board's allow list knows. Nothing else can reach send-keys."""
    from . import main
    name, key = params["name"], body["key"]
    _steerable(db, name)
    asking = asking_reason(db, name)
    if asking:
        # While the session asks a question the pair needs `permissions` (403 `asking` without it). With it, a key that CHOOSES an answer (y, n, 1 to 9, Enter) is still refused:
        # a permission is answered by the permission row (a person, allow or deny, once), never by a key. Escape, Up, Down, Tab and C-c (with confirm) move or cancel only.
        _dialog_gate("keys", name, asking)
        if key in ANSWER_KEYS:
            raise Refusal(409, "busy", f"{ASKING_MESSAGE}: answer it with Allow or Deny, not with a key", code="asking", retry=None)
    main.api_send_keys(name, main.KeysIn(text=key) if key in LITERAL_KEYS else main.KeysIn(keys=[key]))
    return {"ok": True, "key": key}


def _owned(db, name: str) -> None:
    """409 unless the session is one of this board's own agent sessions (owned_session_refusal). For an ack and a close that is all: they run nothing, so a session started
    with wider permissions may be acknowledged or ended; one the owner started outside the board, a shell, a clone and the login session may not."""
    why = owned_session_refusal(db, name)
    if why:
        raise projects.Conflict(why)


def peer_ack(db, params: dict, body: dict) -> dict:
    """POST /api/node/sessions/{name}/ack: the board's own ack (clears the attention mark; answers no request)."""
    from . import main
    main._terminal_session(params["name"])
    _session_here(params["name"])
    _owned(db, params["name"])
    return main.api_ack(params["name"])


def peer_close(db, params: dict, body: dict) -> dict:
    """DELETE /api/node/sessions/{name}: the board's own kill (the tmux session ends, its row closes). It ends here only; no other node is told."""
    from . import main
    main._terminal_session(params["name"])
    _session_here(params["name"])
    _owned(db, params["name"])                              # the same ownership as the local DELETE (which kills whatever is attached) plus the board's own-agent-session rule
    return main.api_kill_session(params["name"])


def _perm_summary(v, known) -> str:
    """A permission request's summary as it may leave this board: control, format and separator characters out, secrets replaced, at most SUMMARY_MAX characters. It is
    shown to the person and kept nowhere on the hub."""
    t = " ".join(str(v or "")[:2000].split())
    return redact("".join(c for c in t if unicodedata.category(c) not in _FREE_BAD), known)[:SUMMARY_MAX]


# The requests another node may answer: an ordinary tool asking to run ONCE. Everything else (a plan approval, a question to the person, a switch of mode or worktree, a
# tool the board does not know) is answered in its terminal, and the list does not offer it.
ANSWERABLE_TOOLS = ("Bash", "Edit", "MultiEdit", "Write", "NotebookEdit", "Read", "Glob", "Grep", "WebFetch", "WebSearch", "apply_patch")
_MCP_TOOL = re.compile(r"mcp__[A-Za-z0-9_-]{1,50}")


def permission_refusal(db, p: dict) -> str | None:
    """Why the permission request row `p` may not be answered from another node (None: it may). ONE function for the list and the answer, so the page is never offered a
    button that is refused: the session is one of this board's own agent sessions (owned_session_refusal) and is there; the request has a tool and a summary; and the tool is an
    ordinary one in ANSWERABLE_TOOLS or an MCP tool. A plan approval, a question, a mode or worktree switch and any tool not listed stay with the terminal."""
    name = p.get("tmux_name") or ""
    if not _alive(name):
        return "the session is gone"
    why = owned_session_refusal(db, name)
    if why:
        return why
    tool = str(p.get("tool_name") or "")
    if not tool or not str(p.get("summary") or "").strip():
        return "this request does not say what it asks"
    if tool not in ANSWERABLE_TOOLS and not _MCP_TOOL.match(tool):
        return "this kind of request is answered in its terminal"
    return None


def peer_permissions(db, params: dict, body: dict) -> dict:
    """GET /api/node/permissions: the requests a hook is still waiting on, {id, tmux, tool, summary, since}, for sessions that are still there and requests the answer row
    would accept (permission_refusal)."""
    from . import main
    known = known_secrets()
    out = []
    for p in main._live_permissions():
        name = p.get("tmux_name") or ""
        if permission_refusal(db, db.perm_get(int(p["id"])) or p):
            continue
        tool = str(p.get("tool_name") or "")
        out.append({"id": int(p["id"]), "tmux": name, "tool": tool if _TOOL_OUT.fullmatch(tool) else "tool", "summary": _perm_summary(p.get("summary"), known),
                    "since": str(p.get("created_at") or "")[:40]})
    return {"permissions": out[:_PERMISSIONS_MAX]}


def peer_permission_answer(db, params: dict, body: dict) -> dict:
    """POST /api/node/permissions/{pid}/{decision}: allow or deny one request, once, through the board's own route function. A request that is gone is 404 `gone`, one that was
    answered (here, in the terminal, or by a timeout) is 409 `answered`, one nobody is waiting for any more is 409 `expired`; none of these writes anything."""
    from . import main
    pid, decision = int(params["pid"]), params["decision"]
    row = db.perm_get(pid)
    if not row or not _alive(row.get("tmux_name") or ""):
        raise Refusal(404, "gone", GONE)
    if row.get("decision") is not None:
        raise Refusal(409, "answered", ANSWERED)
    if pid not in {p["id"] for p in main._live_permissions()}:
        raise Refusal(409, "expired", EXPIRED)
    why = permission_refusal(db, row)                       # the session is the peer's own agent session and the request is an ordinary one: else it stays with the terminal
    if why:
        raise Refusal(409, "refused", f"{why}: answer it on {nodes.display_name()}")
    try:
        main.api_permission_decide(pid, decision, _standin())
    except projects.Conflict:
        raise Refusal(409, "answered", ANSWERED) from None
    except projects.NotFound:
        raise Refusal(404, "gone", GONE) from None
    return {"ok": True, "id": pid, "decision": decision}


def _claim_result_key(body):
    return f"claim-result:{body['repo'].lower()}#{int(body['issue'])}"


_claim_result_lock = threading.RLock()


def _prune_claim_results(db):
    """Bound recovery storage without another worker; callers hold _claim_result_lock."""
    cutoff = time.time() - 86400
    for key, value, at in db.kv_list_prefix("claim-result:"):
        started = value.get("started_at") if isinstance(value, dict) else None
        if not isinstance(started, (int, float)):
            started = datetime.fromisoformat(at).timestamp()
        if started < cutoff:
            db.kv_del(key)


def peer_epic_claim_status(db, params: dict, body: dict) -> dict:
    """Read only local state. The latest marker takes precedence over earlier claim records."""
    from . import nodes_epics
    with _claim_result_lock:
        _prune_claim_results(db)
        row = db.kv_get(_claim_result_key(body))
        result = row.get("value") if row else None
        if not isinstance(result, dict):
            return {"claimed": False, "reason": "unknown"}
        identity = {"claim_id": result["claim_id"]} if result.get("claim_id") else {}
        if result.get("reason") == "pending":
            if time.time() - result.get("started_at", 0) > CLAIM_PENDING_MAX_AGE:
                return {"claimed": False, "reason": "unknown", **identity}
            return result
        if result.get("claimed") is True:
            records = nodes_epics.own_claims(db, [(body["repo"], int(body["issue"]))])
            attempt = result.get("attempt")                   # already_held carries no attempt: any held record of this issue is the one it meant
            if not any(r["state"] == "held" and (attempt is None or r["attempt"] == attempt) for r in records):
                return {"claimed": False, "reason": "released", **identity}
        return result


def peer_epic_claim(db, params: dict, body: dict) -> dict:
    """POST /api/node/epics/claim {repo, issue, epic?}: this board's own claim of one issue of an epic (nodes_epics.claim_issue, issue #148). It works only when this board's
    CCBOARD_EPIC_WRITES is on, only for a repository that is one of this board's own projects and an issue that is a child of an epic this board has read (`epic` lets it read
    that epic first, in its own repositories only). A claim that was made is {claimed: true, reason: won | already_held, repo, issue, winner, attempt}; every other outcome is a
    409 `claim_refused` with `why` (a word of nodes_epics.CLAIM_TEXT) and a few facts. The claim reference stays on this board (it is the fencing token), and nothing from the
    request or from GitHub is written into a comment: the comment is built from this board's own alias and node id."""
    from . import nodes_epics
    with _claim_result_lock:
        _prune_claim_results(db)
    why = None
    if not nodes_epics.github_write.writes_enabled():
        why = "writes_off"
    elif nodes_epics._target(body.get("repo"), body.get("issue")) is None:
        why = "bad_request"
    elif body["repo"].lower() not in {r.lower() for r in nodes_epics.board_repos() if isinstance(r, str)}:
        why = "other_repo"
    if why:
        raise Refusal(409, "claim_refused", nodes_epics.CLAIM_TEXT[why], why=why)
    if not nodes_epics.claim_slots.acquire(blocking=False):          # a few claims at once; the rest are told to try again, with nothing started
        raise Refusal(409, "claim_refused", nodes_epics.CLAIM_TEXT["busy"], why="busy", claims_ahead=nodes_epics.CLAIMS_IN_FLIGHT, wait_s=int(CLAIM_TIMEOUT))
    try:
        started_at = time.time()
        claim_id = body.get("claim_id") or secrets.token_hex(16)
        with _claim_result_lock:
            db.kv_set(_claim_result_key(body), {"claimed": False, "reason": "pending", "started_at": started_at, "claim_id": claim_id})
        try:
            res = nodes_epics.claim_issue(body["repo"], body["issue"], epic=body.get("epic"))
        except Exception:
            res = {"claimed": False, "reason": "error"}
        res["claim_id"] = claim_id
        with _claim_result_lock:
            current = db.kv_get(_claim_result_key(body))
            if current and current["value"].get("claim_id") == claim_id:
                db.kv_set(_claim_result_key(body), {"started_at": started_at, "claimed": res.get("claimed") is True, "reason": res.get("reason"), "repo": body["repo"], "issue": body["issue"], **_claim_facts(res)})
    finally:
        nodes_epics.claim_slots.release()
    if res.get("claimed"):
        return {"claimed": True, "reason": res.get("reason"), "repo": body["repo"], "issue": body["issue"], **_claim_facts(res)}
    why = res.get("reason") if res.get("reason") in CLAIM_PEER_WHY else "error"
    raise Refusal(409, "claim_refused", nodes_epics.CLAIM_TEXT[why], why=why, **_claim_facts(res))


def peer_epic_work(db, params: dict, body: dict) -> dict:
    """POST /api/node/epics/work {repo, issue, epic?, force, report, auto_pr, close}: this board runs one issue of an epic (nodes_work.work_issue, issue #149): the author check first, then
    the claim, then the task with the issue's own model line. It works only when this board's CCBOARD_EPIC_WRITES is on. A run that started is {claimed: true, reason, id, branch, tmux,
    repo, issue}; a repository that is not cloned here is a 404 `repo_not_here`; every other refusal is a 409 `work_refused` with `why` (a word of nodes_work.REASONS) and a few facts.
    The task, its launch and the comments are built on this board from the issue as GitHub holds it; nothing from the request is a command, a path or a comment."""
    from . import nodes_epics, nodes_work
    if not nodes_epics.claim_slots.acquire(blocking=False):          # a few runs at once; the rest are told to try again, with nothing started
        raise Refusal(409, "work_refused", nodes_epics.CLAIM_TEXT["busy"], why="busy", claims_ahead=nodes_epics.CLAIMS_IN_FLIGHT, wait_s=int(CLAIM_TIMEOUT))
    try:
        res = nodes_work.work_issue(body["repo"], body["issue"], epic=body.get("epic"), report=body.get("report", "phase"), auto_pr=body.get("auto_pr", False),
                                    close=body.get("close", False), force=body.get("force", False))
    except nodes_work.Refused as e:
        if e.reason == "repo_not_here":
            raise Refusal(404, "repo_not_here", e.message, why="repo_not_here") from None
        raise Refusal(409, "work_refused", e.message, why=e.reason, **_work_facts(e.facts)) from None
    finally:
        nodes_epics.claim_slots.release()
    return {"claimed": True, "reason": res.get("reason"), "id": res["id"], "branch": res.get("branch"), "tmux": res.get("tmux"), "repo": res["repo"], "issue": res["issue"]}


HANDLERS = {"task": peer_task, "pane": peer_pane, "agents": peer_agents, "task_create": peer_task_create, "task_dispatch": peer_task_dispatch,
            "session_open": peer_session_open, "prompt": peer_prompt, "keys": peer_keys, "ack": peer_ack, "close": peer_close, "permissions": peer_permissions,
            "permission_answer": peer_permission_answer, "epic_claim": peer_epic_claim, "epic_claim_status": peer_epic_claim_status, "epic_work": peer_epic_work}
