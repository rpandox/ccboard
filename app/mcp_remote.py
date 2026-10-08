"""The remote MCP endpoint: POST /mcp, MCP's streamable-HTTP transport without streaming (issue #13).

Who gets in (gate(), run by auth_middleware before anything else looks at the request):
  off (app/mcp_tokens.enabled())                      -> 404, whatever the request carries, so the endpoint does not advertise itself
  no allowed Tailscale identity (app/auth.identify)   -> 403; a tagged device gets no identity header from `tailscale serve`, so it is refused
  the hook token (X-CCBoard-Token) on the request     -> 403: it never opens /mcp
  no device token, a wrong, revoked or expired one    -> 401 with WWW-Authenticate: Bearer and what to do (mint one in Settings > Agents)
  an Origin header                                    -> 403: MCP clients are not browser pages (DNS rebinding, a page on an allowed laptop)
  any method but POST                                 -> 405 (no server-initiated stream, no session to DELETE)
  an MCP-Protocol-Version this server does not speak  -> 400 (none at all is fine: initialize carries none)
The device token replaces the X-CCBoard CSRF header here and nowhere else; a bearer on /api/* is refused by auth_middleware.

What one request may be (endpoint()): one JSON-RPC message of at most 256 KB (413), never a batch (400). A request gets an
application/json answer; a notification or a response gets 202 and no body. Limits (429 with Retry-After): 60 requests a minute and 4
in flight per token, 240 a minute and 16 in flight across all tokens, 10 create_task or dispatch_task calls a minute per token.

The tools are app/mcp.py's, run in-process: inproc_api() calls the board's own route functions as the person `mcp:<token name>`, never
the board over HTTP and never with the hook token. A tools/call needs the token's scope for it (app/mcp.required_scope); without it
the answer is a JSON-RPC error and nothing runs. Every tools/call lands in the audit (app/mcp_tokens.audit): token, tool, task id,
outcome, never the prompt or the result.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from . import mcp, mcp_tokens
from .auth import identify
from .config import settings
from .hooks import TOKEN_HEADER

log = logging.getLogger("ccboard")

PATH = "/mcp"
MAX_BODY = 256 * 1024
PER_TOKEN_PER_MIN = 60
PER_TOKEN_IN_FLIGHT = 4
WRITES_PER_MIN = 10
GLOBAL_PER_MIN = 240
GLOBAL_IN_FLIGHT = 16
WINDOW = 60.0
WRITE_TOOLS = ("create_task", "dispatch_task")
FIX = "mint a new token in Settings > Agents and put it in the client's Authorization header"
HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}

_clock = time.monotonic          # patched by tests


def _json(body: dict, status: int, **headers) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={**HEADERS, **headers})


def _err(status: int, message: str, **headers) -> JSONResponse:
    return _json({"error": message}, status, **headers)


def _rpc_err(status: int, code: int, message: str, mid=None) -> JSONResponse:
    return _json({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}, status)


# ------------------------------------------------------------------ the gate (auth_middleware)

def gate(request: Request) -> Response | None:
    """None when the request may reach the endpoint (request.state.user and .mcp_token set), else the refusal to send."""
    if not mcp_tokens.enabled():
        return _err(404, "Not Found")
    user = identify(request.headers, settings)
    if user is None:
        return _err(403, "no Tailscale identity or not in CCBOARD_ALLOWED_USERS")
    if request.headers.get(TOKEN_HEADER):
        return _err(403, "the box's hook token is not accepted at /mcp; use a device token from Settings > Agents")
    row, why = mcp_tokens.verify(mcp_tokens.bearer(request.headers.get("authorization")))
    if row is None:
        what = {"missing": "no device token", "expired": "this device token has expired"}.get(why, "this device token is wrong or was revoked")
        return _err(401, f"{what}: {FIX}",
                    **{"WWW-Authenticate": f'Bearer realm="ccboard", error="invalid_token", error_description="{what}"'})
    if request.headers.get("origin") is not None:
        return _err(403, "requests from a web page (an Origin header) are refused at /mcp")
    if request.method != "POST":
        return _err(405, "POST one JSON-RPC message; this server opens no stream", Allow="POST")
    ver = request.headers.get("mcp-protocol-version")
    if ver is not None and ver.strip() not in mcp.SUPPORTED_PROTOCOLS:
        return _err(400, f"unsupported MCP-Protocol-Version; this server speaks {' and '.join(mcp.SUPPORTED_PROTOCOLS)}")
    request.state.user = f"mcp:{row['name']}"
    request.state.mcp_token = row
    request.state.mcp_login = user
    return None


# ------------------------------------------------------------------ limits

class Limiter:
    """Sliding one-minute windows and in-flight counts, under one lock. A refused request consumes nothing."""

    def __init__(self):
        self.lock = threading.Lock()
        self.hits: dict[tuple, deque] = defaultdict(deque)
        self.inflight: dict[str, int] = defaultdict(int)

    def take(self, *keys: tuple[tuple, int]) -> int:
        """Count one hit on every (key, per-minute limit), or none: 0 when taken, else the seconds until the fullest window frees one."""
        now = _clock()
        with self.lock:
            wait = 0
            for key, limit in keys:
                q = self.hits[key]
                while q and q[0] <= now - WINDOW:
                    q.popleft()
                if len(q) >= limit:
                    wait = max(wait, max(1, math.ceil(q[0] + WINDOW - now)))
            if wait:
                return wait
            for key, _limit in keys:
                self.hits[key].append(now)
            return 0

    def enter(self, token_id: str) -> bool:
        with self.lock:
            if self.inflight[token_id] >= PER_TOKEN_IN_FLIGHT or sum(self.inflight.values()) >= GLOBAL_IN_FLIGHT:
                return False
            self.inflight[token_id] += 1
            return True

    def leave(self, token_id: str) -> None:
        with self.lock:
            self.inflight[token_id] = max(0, self.inflight[token_id] - 1)
            if not self.inflight[token_id]:
                del self.inflight[token_id]

    def reset(self) -> None:
        with self.lock:
            self.hits.clear()
            self.inflight.clear()


limiter = Limiter()


def _too_many(wait: int, what: str) -> JSONResponse:
    return _err(429, f"too many requests: {what}; try again in {wait} s", **{"Retry-After": str(wait)})


# ------------------------------------------------------------------ the tools, in-process

_LEGACY = re.compile(r"/api/projects/([^/]+)/repos/([^/]+)/tasks")
_DISPATCH = re.compile(r"/api/tasks/(\d{1,12})/dispatch")
_TASK = re.compile(r"/api/tasks/(\d{1,12})")


def inproc_api(user: str):
    """api(method, path, body) for app/mcp.tool_call that runs the board's route functions in this process, as `user`. Refusals come back
    as RuntimeError("ccboard <status>: <message>"), the way the stdio shim reports an HTTP error."""
    from . import gitops, github, main, previews, projects, tmux

    def call(method: str, path: str, body: dict | None = None):
        b = body or {}
        try:
            if method == "GET" and path == "/api/state":
                out = main.build_state(user)
            elif method == "POST" and path == "/api/tasks":
                out = main.api_tasks_create(main.TaskCreateIn(**b))
            elif method == "POST" and (m := _LEGACY.fullmatch(path)):
                out = main.api_create_task(m[1], m[2], main.TaskIn(**b))
            elif method == "POST" and (m := _DISPATCH.fullmatch(path)):
                out = main.api_task_dispatch(int(m[1]), main.DispatchIn(**b))
            elif method == "GET" and (m := _TASK.fullmatch(path)):
                out = main.api_task_get(int(m[1]))
            else:
                raise RuntimeError(f"ccboard: {method} {path} is not a call the MCP tools make")
        except RuntimeError:
            raise
        except projects.BadRequest as e:
            raise RuntimeError(f"ccboard 400: {e}") from None
        except projects.NotFound as e:
            raise RuntimeError(f"ccboard 404: {e}") from None
        except projects.Conflict as e:
            raise RuntimeError(f"ccboard 409: {e}") from None
        except projects.Forbidden as e:
            raise RuntimeError(f"ccboard 403: {e}") from None
        except HTTPException as e:
            raise RuntimeError(f"ccboard {e.status_code}: {e.detail}") from None
        except tmux.TmuxDown:
            raise RuntimeError("ccboard 503: ccboard-tmux is not running") from None
        except tmux.TmuxError as e:
            raise RuntimeError(f"ccboard 500: tmux: {e}") from None
        except github.GhError as e:
            raise RuntimeError(f"ccboard 502: {e}") from None
        except (gitops.GitError, previews.PreviewError) as e:
            raise RuntimeError(f"ccboard 422: {e}") from None
        except ValueError as e:              # a body the route's model refuses (pydantic)
            raise RuntimeError(f"ccboard 400: {e}") from None
        except Exception as e:
            log.warning("mcp in-process call %s %s failed: %s", method, path, e.__class__.__name__)
            raise RuntimeError("ccboard 500: the board could not do that") from None
        if isinstance(out, Response):        # a route that answers a refusal itself (a dispatch into a busy session: 409 {error, ...})
            try:
                data = json.loads(bytes(out.body or b"{}"))
            except ValueError:
                data = {}
            if out.status_code >= 400:
                raise RuntimeError(f"ccboard {out.status_code}: {data.get('error') if isinstance(data, dict) else data}")
            out = data
        return jsonable_encoder(out)
    return call


# ------------------------------------------------------------------ the endpoint

async def _read_body(request: Request) -> bytes | None:
    """The body, or None when it is over MAX_BODY (declared or streamed)."""
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > MAX_BODY:
                return None
        except ValueError:
            return None
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_BODY:
            return None
    return bytes(buf)


async def endpoint(request: Request) -> Response:
    row = getattr(request.state, "mcp_token", None)
    if not row:                                   # only gate() sets it: never reached without one, but never trusted either
        return _err(401, f"no device token: {FIX}")
    tid = row["id"]
    wait = limiter.take((("token", tid), PER_TOKEN_PER_MIN), (("global",), GLOBAL_PER_MIN))
    if wait:
        return _too_many(wait, "this device's or the board's request limit")
    if not limiter.enter(tid):
        return _too_many(1, f"at most {PER_TOKEN_IN_FLIGHT} requests at a time per device")
    try:
        return await _serve(request, row)
    finally:
        limiter.leave(tid)


async def _serve(request: Request, row: dict) -> Response:
    raw = await _read_body(request)
    if raw is None:
        return _err(413, f"the request body is over {MAX_BODY // 1024} KB")
    try:
        msg = json.loads(raw)
    except ValueError:
        return _rpc_err(400, -32700, "parse error")
    if isinstance(msg, list):
        return _rpc_err(400, -32600, "batches are not supported: send one JSON-RPC message per request")
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_err(400, -32600, "invalid request")
    login = getattr(request.state, "mcp_login", None)
    if "method" not in msg:
        if "result" in msg or "error" in msg:     # a response to a server request: this server sends none, so there is nothing to match
            mcp_tokens.touch(row["id"], login)
            return Response(status_code=202, headers=HEADERS)
        return _rpc_err(400, -32600, "invalid request", msg.get("id"))
    if not isinstance(msg.get("method"), str):
        return _rpc_err(400, -32600, "invalid request", msg.get("id"))
    if msg.get("id") is None:                     # a notification (notifications/initialized, cancelled): accepted, nothing to answer
        mcp_tokens.touch(row["id"], login)
        return Response(status_code=202, headers=HEADERS)
    tool = None
    if msg["method"] == "tools/call":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        tool = str(params.get("name") or "")
        args = params.get("arguments")
        need = mcp.required_scope(tool, args)
        if need and need not in (row.get("scopes") or []):
            mcp_tokens.audit(row, tool, args.get("task_id") if isinstance(args, dict) else None, "denied")
            what = "type into a running session" if need == "sessions" else ("start or create tasks" if need == "tasks" else "read the board")
            return _json({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": mcp.DENIED_CODE,
                          "message": f"this device token may not {what} (it lacks the {need} scope); mint one with that scope in Settings > Agents"}}, 200)
        if tool in WRITE_TOOLS:
            wait = limiter.take((("writes", row["id"]), WRITES_PER_MIN))
            if wait:
                mcp_tokens.audit(row, tool, args.get("task_id") if isinstance(args, dict) else None, "limited")
                return _too_many(wait, f"at most {WRITES_PER_MIN} create_task or dispatch_task calls a minute per device")
    mcp_tokens.touch(row["id"], login, tool)
    user = f"mcp:{row['name']}"
    api = inproc_api(user)

    def call(name, args):
        task_id = args.get("task_id") if isinstance(args, dict) else None
        try:
            out = mcp.tool_call(name, args, api)
        except Exception:
            mcp_tokens.audit(row, name, task_id, "error")
            raise
        if task_id is None and isinstance(out, dict):
            task_id = out.get("id")
        mcp_tokens.audit(row, name, task_id, "ok")
        return out

    resp = await run_in_threadpool(mcp.rpc, msg, call)
    return _json(resp, 200)
