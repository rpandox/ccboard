"""ccboard: a status-and-attention layer over Claude Code CLI sessions in tmux."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import account_store, accounts, agents, autoresume, backup, claude_auth, clonequeue, codex_accounts, cost, deploy, doctor, github, gitops, health, hooks, login_problem, memory, memory_proxy, nodes, nodes_discovery, notify, permissions, preflight, previews, projects, prpoll, push, recover, samples, scheduler, search, skills, taskflow, tasks, tmux, tree, usage, usage_refresh, usage_summary
from .agents import codex_discovery, codex_pane
from .agents import monitor as mem_monitor
from .agents import registry
from .agents.base import LaunchReq
from .agents import claude as claude_adapter
from .agents.claude import BYPASS_PARTS, OVERRIDE_PARTS, WORKTREE_RE
from .agents.codex import NAME_RE as CODEX_NAME_RE
from . import issues as issues_mod
from . import platform as plat
from . import mcp, mcp_remote, mcp_tokens
from .auth import csrf_ok, identify
from .config import settings
from .staticgz import GzipStaticFiles
from .devguard import require_real_launch_ok
from .db import DB, now as db_now

log = logging.getLogger("ccboard")
STATIC = Path(__file__).parent / "static"
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Every launcher the board knows by name: claude / resume / continue / shell, and the three codex ones (v0.5.11).
LAUNCHERS = ("claude", "resume", "continue", "shell", "codex", "codex-resume", "codex-continue")
_LAUNCHER_AGENT = {"claude": ("claude", "claude"), "resume": ("claude", "resume"), "continue": ("claude", "continue"),
                   "shell": ("shell", "shell"),
                   "codex": ("codex", "claude"), "codex-resume": ("codex", "resume"), "codex-continue": ("codex", "continue")}
STARTABLE_AGENTS = ("claude", "codex", "shell")
MAX_ARGS = 1024

db: DB | None = None
indexer = None
sched = None
sampler = None
taskflow_rt = None          # the task runtime (app/taskflow.py): started by the lifespan, created on demand by the routes that need it
_scan_lock = threading.Lock()
_scan_cache: tuple[float, dict] | None = None
SCAN_TTL = 2.0


def _state_sampler(handle: DB):
    """The closure DB.on_state_change calls: one 'state' sample per transition. The row the DB hands over may be a closed one
    (end/reconcile), so project, repo, name and agent are filled from it and, failing that, from the session name; internal
    sessions are never sampled."""
    def on_state_change(tmux_name: str, old: str | None, new: str, event: str, row: dict | None) -> None:
        if tmux.is_internal(tmux_name):
            return
        r = dict(row or {})
        try:
            project, repo, session = tmux.split_name(tmux_name)
        except ValueError:
            project = repo = session = None
        r["project"] = r.get("project") or project
        r["repo"] = r.get("repo") or repo
        r["name"] = r.get("name") or session
        r["agent"] = r.get("agent") or "claude"
        try:           # the account the session runs under, else the current one, for Claude rows only (accounts are Claude subscriptions)
            acct = r.get("account") or (accounts.current(handle) if r["agent"] == "claude" else None)
        except Exception as e:
            log.debug("state sample account lookup failed: %s", e)
            acct = None
        samples.record_state(handle, tmux_name, old, new, event, r, acct=acct)
    return on_state_change


def _sampler_health() -> dict:
    """The Sampler's own cpu delta: it must not steal the one the hub poller and /api/health read."""
    return health.snapshot(consumer="sampler")


def _sampler_counts() -> dict:
    """What the n_live / n_work / n_attn series record: live = not ended (a tmux session the board sees), work = working,
    attn = needs attention."""
    try:
        sessions, down = _merged_sessions()
    except tmux.TmuxError as e:                    # tmux missing or confused: skip this tick rather than record a false zero
        log.debug("sampler counts skipped: %s", e)
        return {}
    if down:                                        # _merged_sessions swallows TmuxDown into ({}, True): a gap, not a zero
        return {}
    vals = list(sessions.values())
    return {"live": len(vals), "work": sum(1 for s in vals if s["state"] == "working"),
            "attn": sum(1 for s in vals if s["needs_attention"])}


def startup_line() -> str:
    """The startup log line. It counts the allowed logins and never names them: a login is usually an email, and the journal or
    `docker logs` gets pasted into bug reports (#44 F-06)."""
    n = len(settings.allowed_users)
    who = (f"{n} allowed login{'s' if n != 1 else ''}" if n else ("DEV BYPASS" if settings.dev_bypass_user else "EMPTY allowlist"))
    return f"ccboard on {settings.loopback_url()}, projects in {settings.projects_dir}, {who}"


def _paired_urls() -> list[str]:
    """The addresses of the nodes this board has paired with (not the legacy rows): the hub poller leaves those to the pair."""
    return [r["url"] for r in nodes.peers(db=db) if r.get("direction") != "in" and not r.get("legacy") and r.get("url")]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, sampler
    settings.validate()
    db = DB(settings.db_path)
    hooks.ensure_token()
    notify.set_db(db)
    deploy.clear(db)                                      # a new container is up: the update that was pending has happened
    try:                                                  # the live login gets its saved copy at once: a bad switch is then one tap from recovery
        account_store.seed_current(db)
    except account_store.Unsupported:
        pass
    except Exception as e:
        log.warning("saved-login seed failed: %s", e.__class__.__name__)
    try:                                                  # the same for the live Codex login (a slot per account, kept off /tmp)
        codex_accounts.seed_current(db)
    except codex_accounts.Unsupported:
        pass
    except Exception as e:
        log.warning("saved Codex login seed failed: %s", e.__class__.__name__)
    db.on_state_change = _state_sampler(db)              # state series: one sample per real transition (set_state/end/reconcile)
    try:
        push.ensure_keys()
    except Exception as e:  # pywebpush missing or unwritable data dir: the board still works
        log.warning("web push disabled: %s", e)
    try:                                                  # the node id is settled at the first start, not by the first poll that happens to ask (nodes epic, #133)
        nodes.node_id()
    except Exception as e:
        log.warning("node id could not be read: %s", e.__class__.__name__)
    poller = usage.Poller(db)
    poller.start()
    sampler = samples.Sampler(db, health_fn=_sampler_health, counts_fn=_sampler_counts)
    sampler.start()
    cloner = clonequeue.Worker(_launch_clone)
    cloner.start()
    prp = prpoll.Poller(db, projects.repo_path)
    prp.start()
    mem_mon = None
    if settings.claude_mem:                               # claude-mem health: kv mem_health every 20 s (CCBOARD_CLAUDE_MEM=0 skips it)
        mem_mon = mem_monitor.Monitor(db)
        mem_mon.start()
    global indexer, sched
    indexer = search.Indexer(db)
    indexer.start()
    stale = db.runs_interrupt_stale()
    if stale:
        log.warning("closed %d scheduled run(s) the previous process left 'running': %s", len(stale), stale)
    sched_worker = scheduler.Worker(db)
    sched = sched_worker
    sched_worker.start()
    hub = None
    hub_nodes = health.parse_nodes(settings.nodes_raw)       # not `nodes`: that name is the module, and the node id above is read through it
    if hub_nodes:
        try:                                                  # the CCBOARD_NODES entries are the registry's legacy rows (read only until paired, issue #135)
            nodes.import_legacy(hub_nodes, db=db)
        except Exception as e:
            log.warning("CCBOARD_NODES could not be added to the node registry: %s", e.__class__.__name__)
    if hub_nodes and settings.hub_token:
        hub = health.Poller(db, hub_nodes, paired=_paired_urls)
        hub.start()
    elif hub_nodes:
        log.warning("CCBOARD_NODES is set but CCBOARD_HUB_TOKEN is empty: hub polling disabled")
    try:
        rec = recover.run(db, _start_session)
        if rec["recovered"] or rec["closed"]:
            log.info("recovery: relaunched %s, closed %s", rec["recovered"], rec["closed"])
    except Exception as e:
        log.warning("recovery failed: %s", e)
    global taskflow_rt
    taskflow_rt = taskflow.Runtime(db, start_session=_taskflow_start, end_session=_end_session, perm_pending=_permission_pending)
    taskflow_rt.start()                                   # after the recovery: its sweep must not see a session the reboot has not relaunched yet
    log.info("%s", startup_line())
    threading.Thread(target=static_mount.warm, name="static-gzip", daemon=True).start()          # the gzip of every script and style, before the first page load asks for it
    log.info("runtime %s, image %s", settings.runtime, settings.image_version or "-")
    yield
    taskflow_rt.stop()
    poller.stop.set()
    sampler.stop()
    cloner.stop.set()
    prp.stop.set()
    if mem_mon:
        mem_mon.stop.set()
    indexer.stop.set()
    sched_worker.stop.set()
    if hub:
        hub.stop.set()


app = FastAPI(title="ccboard", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
STATIC_DIR = Path(__file__).resolve().parent / "static"


# Every file type the page or the offline shell can load. The version and the SW shell list are both generated from
# one glob over STATIC_DIR, so adding a file anywhere under app/static can neither be missed by the cache nor
# leave open pages on stale code.
STATIC_EXTS = {".js", ".css", ".html", ".webmanifest", ".woff2", ".png", ".svg"}
SHELL_EXTS = {".js", ".css", ".woff2", ".webmanifest", ".png"}   # precached; html files are navigations, svg is not used yet
SHELL_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")                 # what may sit inside the single quotes of the SW template


def static_files() -> list[Path]:
    """Every shippable file under STATIC_DIR (vendor included), sorted by relative path; demo/ and __pycache__ are skipped."""
    out = []
    for p in STATIC_DIR.rglob("*"):
        rel = p.relative_to(STATIC_DIR)
        if p.suffix in STATIC_EXTS and "demo" not in rel.parts and "__pycache__" not in rel.parts and p.is_file():
            out.append(p)
    return sorted(out, key=lambda p: p.relative_to(STATIC_DIR).as_posix())


def asset_version() -> str:
    """Hash of every static file (name and bytes, vendor and the sw.js template included); changes on every deploy that touches the UI, so open pages reload themselves."""
    h = hashlib.sha256()
    for p in static_files():
        h.update(p.relative_to(STATIC_DIR).as_posix().encode() + b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:12]


def shell_paths() -> list[str]:
    """URLs the service worker precaches: the board page plus every static script, style, font, manifest and icon.
    screenshots/ (the manifest's store previews) stays out: the browser reads them from the manifest, never through the
    worker, so precaching them would cost every install about 230 KB for no offline benefit (they still count towards the
    asset version through static_files())."""
    paths = []
    for p in static_files():
        rel = p.relative_to(STATIC_DIR).as_posix()
        if p.suffix not in SHELL_EXTS or rel == "sw.js" or rel.startswith("screenshots/"):
            continue
        if not SHELL_PATH_RE.match(rel):
            log.warning("static file %r has characters the service worker shell cannot carry; not precached", rel)
            continue
        paths.append("/static/" + rel)
    return ["/"] + sorted(paths)


def render_sw(version: str | None = None) -> bytes:
    """The /sw.js body: the sw.js template with the build id and the shell list filled in."""
    js = (STATIC_DIR / "sw.js").read_text(encoding="utf-8")
    js = js.replace("__ASSET_VERSION__", version or asset_version())
    return js.replace("__SHELL_JSON__", json.dumps(shell_paths(), separators=(",", ":"))).encode("utf-8")


ASSET_VERSION = asset_version()
SW_JS = render_sw(ASSET_VERSION)

IMMUTABLE_CACHE = "public, max-age=31536000, immutable"


def is_immutable_static(path: str) -> bool:
    """True for URLs whose bytes never change under their name: /static/vendor/** and any /static/**/*.woff2.

    Their bytes are hashed into the asset version and the service worker cache name. The browser's HTTP cache is keyed by URL
    alone, though: a vendored file that changes must also change its file name (blueprint.css -> blueprint-5.x.css), or open
    browsers keep the old bytes for a year. Dot segments are refused because StaticFiles resolves them after the URL is
    decoded ('/static/vendor/%2e%2e/core.js' would serve core.js); HTML and JS of the app stay no-cache.
    """
    if not path.startswith("/static/") or ".." in path.split("/") or "//" in path:
        return False
    return path.startswith("/static/vendor/") or path.endswith(".woff2")


PAIR_BODY_MAX = 16 * 1024           # the pair endpoint answers anyone on the tailnet, so its body is held much smaller
NODE_SECURITY_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _node_bearer(request: Request) -> bool:
    """Does any Authorization header carry a node token (`Bearer ccbnode_...`)? Looks at every value, so a second header cannot hide one."""
    for v in request.headers.getlist("authorization"):
        t = mcp_tokens.bearer(v)
        if t is not None and t.startswith(nodes.TOKEN_PREFIX):
            return True
    return False


def _node_refused(status: int, message: str, **headers) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers={**NODE_SECURITY_HEADERS, **headers})


def _clean_text(v, n: int) -> str:
    """Header text for the audit: control characters out, at most n characters."""
    return "".join(c for c in str(v or "") if c.isprintable())[:n].strip()


def _node_note(request: Request) -> str:
    """The caller's own words about itself (X-CCBoard-Node, X-CCBoard-Acting-User) for an audit row. Recorded, never trusted for access."""
    node, user = _clean_text(request.headers.get("x-ccboard-node"), 64), _clean_text(request.headers.get("x-ccboard-acting-user"), 64)
    return " ".join(x for x in (f"node {node}" if node else "", f"acting user {user}" if user else "") if x)


async def _node_auth(request: Request, call_next):
    """A request with `Authorization: Bearer ccbnode_...` (issue #135): one paired node calling this board. It carries that token and nothing else (no
    hook token, no hub token, one Authorization header), X-CCBoard: 1, and no Origin. The token is looked up by its digest (nodes.verify_token, constant
    time), the pair's rate bucket is taken, and only a route in nodes.NODE_ROUTES whose scope the pair holds is answered; every other /api route is a 403
    for it, whatever it asks. The identity check is not used: in user-owned mode the caller arrives with the owner's login, so identity cannot tell nodes apart."""
    if len(request.headers.getlist("authorization")) != 1:
        return _node_refused(403, "send exactly one Authorization header")
    if request.headers.get(hooks.TOKEN_HEADER) or request.headers.get(health.HUB_HEADER):
        return _node_refused(403, "a node token is the only credential a node request carries")
    if request.headers.get("origin") is not None:
        return _node_refused(403, "requests from a web page (an Origin header) are refused")
    if request.headers.get("x-ccboard") != "1":
        return _node_refused(403, "missing X-CCBoard header")
    addr = nodes.caller_addr(request.client.host if request.client else None, request.headers.get("x-forwarded-for"))
    peer = nodes.verify_token(mcp_tokens.bearer(request.headers.get("authorization")), ip=addr, db=db)
    if peer is None:
        return _node_refused(401, "this node token is wrong, expired or was revoked", **{"WWW-Authenticate": 'Bearer realm="ccboard", error="invalid_token"'})
    pid = peer["peer_id"]
    ok, wait = nodes.rate_check(pid, write=request.method not in ("GET", "HEAD"))
    if not ok:
        return _node_refused(429, "too many requests from this node", **{"Retry-After": str(wait)})
    scope = nodes.route_scope(request.method, request.url.path)
    note = _node_note(request)
    if not nodes.scope_ok(peer.get("scopes"), scope):
        nodes.audit("in", pid, "route_refused" if scope is None else "scope_refused", False, (f"scope {scope} not granted " if scope else "") + note,
                    node_name=peer.get("name"), target=f"{request.method} {request.url.path}"[:120], status="refused", db=db)
        return _node_refused(403, "this node token does not open that route" if scope is None else f"this node token does not hold the {scope} scope")
    if request.headers.get("transfer-encoding"):
        return _node_refused(411, "send a Content-Length")
    try:
        size = int(request.headers.get("content-length") or 0)
    except ValueError:
        return _node_refused(400, "bad Content-Length")
    if size > nodes.BODY_MAX:
        return _node_refused(413, f"the body is over {nodes.BODY_MAX // 1024} KB")
    request.state.user = "node:" + _clean_text(peer.get("name") or pid, 60)
    request.state.node_pair = pid
    request.state.node_peer = peer
    request.state.node_note = note
    resp = await call_next(request)
    for k, v in NODE_SECURITY_HEADERS.items():
        resp.headers[k] = v
    return resp


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path == "/healthz":
        return PlainTextResponse("ok")
    if request.url.path == mcp_remote.PATH and mcp_tokens.enabled() and _node_bearer(request):
        return _node_refused(403, "a node token is not accepted at /mcp; use a device token from Settings > Agents")
    if request.url.path == mcp_remote.PATH:
        # The remote MCP endpoint (issue #13): 404 while off, then identity AND a device token, never the hook token (app/mcp_remote.py).
        refused = mcp_remote.gate(request)
        return refused if refused is not None else await call_next(request)
    if request.url.path.startswith("/api/") and _node_bearer(request):
        return await _node_auth(request, call_next)
    if request.url.path.startswith("/api/") and mcp_tokens.bearer(request.headers.get("authorization")) is not None:
        # A device token opens /mcp and nothing else: a bearer is refused on every /api route, whatever else the request carries.
        return JSONResponse({"error": "device tokens open /mcp only"}, status_code=403)
    if request.url.path == "/api/node/summary" and request.headers.get(health.HUB_HEADER):
        if not health.check_hub_token(request.headers.get(health.HUB_HEADER)):
            return JSONResponse({"error": "bad hub token"}, status_code=403)
        request.state.user = "hub"
        return await call_next(request)
    if request.url.path == "/api/nodes/pair" and request.method == "POST":
        # The other board's pairing call (issue #135): a tagged board has no identity, so none is asked for. The handler checks X-CCBoard, Origin, a
        # disallowed identity header, the size, the rate and the code before it does anything.
        request.state.user = None
        return await call_next(request)
    if request.url.path == "/api/nodes/pair/confirm" and request.method == "POST":
        # The callback of the board that is being called (issue #135): it asks whether THIS board is the one redeeming its code right now. No identity (a
        # tagged board has none); the handler checks X-CCBoard, Origin, the size and the rate, and answers only while this board's own add_node is in flight.
        request.state.user = None
        return await call_next(request)
    if request.url.path == "/api/node/hello" and request.method == "GET":
        # The one route that answers with no identity (issue #133): three fixed keys, rate limited per source in the handler. Every other /api/node* route
        # (the card, the summary) still needs an identity, a hook token or the legacy hub token (the summary only).
        request.state.user = None
        return await call_next(request)
    if request.url.path in ("/api/hook", "/api/permission", "/api/deploy/gate"):
        # Hooks run on the box itself (no Tailscale identity); they carry the local token instead.
        if request.method != "POST" or not hooks.check_token(request.headers.get(hooks.TOKEN_HEADER)):
            return JSONResponse({"error": "bad hook token"}, status_code=403)
        return await call_next(request)
    if request.url.path.startswith("/api/") and request.headers.get(hooks.TOKEN_HEADER):
        # Local automation (the MCP shim, scripts on the box) proves itself with the 0600 token file.
        if not hooks.check_token(request.headers.get(hooks.TOKEN_HEADER)):
            return JSONResponse({"error": "bad token"}, status_code=403)
        request.state.user = "local-token"
        return await call_next(request)
    user = identify(request.headers, settings)
    if user is None:
        return JSONResponse({"error": "no Tailscale identity or not in CCBOARD_ALLOWED_USERS"}, status_code=403)
    if not csrf_ok(request.method, request.headers):
        return JSONResponse({"error": "missing X-CCBoard header"}, status_code=403)
    request.state.user = user
    resp = await call_next(request)
    if not (settings.dev_bypass_user and request.url.path.startswith("/tty/")):
        # Only the dev harness's fake terminal (served under /tty/ while the dev bypass is on) goes without: the real /tty/ is
        # ttyd's and never passes through the board, and frame-ancestors 'none' would block the fake from the terminal page's iframe.
        resp.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        # Always revalidate the shell and its assets (ETag), so an update never mixes old HTML with new JS. Only vendored
        # files and fonts are immutable, and only when they were found: a 404 must not be cached for a year.
        if is_immutable_static(request.url.path) and resp.status_code in (200, 206, 304):
            resp.headers["Cache-Control"] = IMMUTABLE_CACHE
        else:
            resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.exception_handler(projects.BadRequest)
async def _bad(_, e):
    return JSONResponse({"error": str(e)}, status_code=400)


@app.exception_handler(projects.Unprocessable)
async def _unprocessable(request, e):
    return JSONResponse({"error": str(e)}, status_code=422)


@app.exception_handler(projects.Conflict)
async def _conflict(_, e):
    return JSONResponse({"error": str(e)}, status_code=409)


@app.exception_handler(projects.NotFound)
async def _notfound(_, e):
    return JSONResponse({"error": str(e)}, status_code=404)


@app.exception_handler(projects.Forbidden)
async def _forbidden(_, e):
    return JSONResponse({"error": str(e)}, status_code=403)


@app.exception_handler(github.GhError)
async def _gh(_, e):
    return JSONResponse({"error": str(e)}, status_code=502)


@app.exception_handler(previews.PreviewError)
async def _preverr(_, e):
    return JSONResponse({"error": str(e)}, status_code=422)


@app.exception_handler(gitops.GitError)
async def _giterr(_, e):
    return JSONResponse({"error": str(e)}, status_code=422)


@app.exception_handler(tmux.TmuxDown)
async def _down(_, e):
    return JSONResponse({"error": f"ccboard-tmux is not running ({plat.hint('start', 'ccboard-tmux')})"}, status_code=503)


@app.exception_handler(tmux.TmuxError)
async def _tmuxerr(_, e):
    return JSONResponse({"error": f"tmux: {e}"}, status_code=500)


# ---------- state ----------

def _registry_snapshot(pane_pids: dict[str, int] | None = None) -> dict | None:
    """Claude's own session registry (file reads, its own 30 s ttl). Never raises: the 3 s poll must not depend on it."""
    try:
        return registry.snapshot(db, settings.claude_config_dir, pane_pids=pane_pids or {}, projects_dir=settings.projects_dir)
    except Exception as e:
        log.debug("registry snapshot failed: %s", e)
        return None


def _wait_kind_of(last: dict | None) -> str | None:
    """What a 'waiting' session is waiting on, from its newest stored event: the Notification kind ('idle_prompt': Claude is idle at
    its prompt; 'permission_prompt' / 'elicitation_dialog': a dialog is open), else the name of the newer event (e.g.
    'PermissionRequest'), None without any event. Only 'idle_prompt' means the TUI is at its prompt, so a caller that types into the
    session treats everything else, None included, as a dialog."""
    if not last:
        return None
    return (last.get("kind") or "Notification") if last.get("event") == "Notification" else last.get("event")


def _wait_kinds(rows: dict[str, dict]) -> dict[str, str | None]:
    """{tmux name: wait kind} for the sessions whose state is 'waiting' (one grouped query for all of them); {} when none is."""
    names = [n for n, r in rows.items() if (r.get("state") or "") == "waiting"]
    if not names:
        return {}
    last = db.last_events(names)
    return {n: _wait_kind_of(last.get(n)) for n in names}


def _hooks_missing(row: dict) -> str | None:
    """agents/codex.hooks_missing for a session row; None for anything else, and never an exception on the 3 s poll."""
    if (row or {}).get("agent") != "codex":
        return None
    try:
        from .agents import codex as codex_agent
        return codex_agent.hooks_missing(row)
    except Exception as e:
        log.debug("hooks_missing failed: %s", e.__class__.__name__)
        return None


def _codex_launch_view(name: str, row: dict, command: str | None, view: dict) -> None:
    """A Codex row that has sent no hook yet (Codex's first hook comes with its first turn) is either still starting, waiting behind a
    dialog nobody answered (update, trust: view.pane_dialog, shown as needing a person, never answered by the board), or gone back to its
    login shell (LAUNCH_GRACE s after the row was made: the row becomes errored with the reason, once). Never raises into the 3 s poll."""
    try:
        made = _parse_at(row.get("created_at"))
        if made is not None and (datetime.now(timezone.utc) - made).total_seconds() < LAUNCH_GRACE:
            return
        block = _pane_block(name, row, command, invalidate=False)
        if block is None:
            return
        if block[0] == "agent_exited":
            fresh = db.open_row(name) or {}
            view.update(state=fresh.get("state") or "errored", state_at=fresh.get("state_at"), last_event=fresh.get("last_event"),
                        last_message=fresh.get("last_message"), needs_attention=True)
        else:
            view.update(pane_dialog=block[1], needs_attention=True, last_message=block[1])
    except Exception as e:
        log.debug("codex launch check failed for %s: %s", name, e.__class__.__name__)


def _merged_sessions(rich: bool = False) -> tuple[dict[str, dict], bool]:
    """tmux sessions (non-internal) merged with DB rows. Returns (sessions, tmux_down).

    Every session carries agent/agent_session_id/row_id and its flags (minus transcript_path). `rich` adds what costs a
    query or a file read: the task chip (one db.active_tasks_by_session() query) and flags.registry (Claude's own registry,
    cached 30 s inside registry.snapshot). It is on for /api/state and /api/sessions/{name}; the task list and the hub
    summary only need states."""
    snapshot_at = db_now()
    try:
        live = tmux.list_sessions()
    except tmux.TmuxDown:
        return {}, True
    assert db is not None
    db.reconcile(set(live.keys()), before=snapshot_at)
    rows = db.open_rows()
    chips = db.active_tasks_by_session() if rich else {}
    waits = _wait_kinds({n: rows.get(n, {}) for n in live if not tmux.is_internal(n)})
    try:                                             # one list-clients per scan; the 3 s poll must not depend on it
        viewers = tmux.viewers()
    except (tmux.TmuxError, tmux.TmuxDown) as e:
        log.debug("list-clients failed: %s", e)
        viewers = None
    out: dict[str, dict] = {}
    for name, s in live.items():
        if tmux.is_internal(name):
            continue
        row = rows.get(name, {})
        chip = (chips.get(row.get("row_id")) or [None])[0]
        out[name] = {
            "created": s["created"], "attached": s["attached"], "command": s["command"], "path": s["path"],
            "pane_id": s["pane_id"], "launcher": row.get("launcher", "external"), "cmd": row.get("cmd"),
            # who is looking: full clients (a person at the terminal), grid tiles (ignore-size) and read-only views; without
            # list-clients every attached client counts as a full one. win = the session window's [cols, rows] or None.
            "viewers": (viewers.get(name) or {"full": 0, "grid": 0, "ro": 0}) if viewers is not None
                       else {"full": s["attached"], "grid": 0, "ro": 0},
            "win": [s["window_width"], s["window_height"]] if s.get("window_width") and s.get("window_height") else None,
            "claude_session_id": row.get("claude_session_id"), "add_dirs": row.get("add_dirs", []),
            "state": row.get("state") or "unknown", "state_at": row.get("state_at"), "last_event": row.get("last_event"),
            "last_message": row.get("last_message"), "last_prompt": row.get("last_prompt"), "stats": row.get("stats"),
            "needs_attention": bool(row.get("state") in hooks.ATTENTION_STATES and not row.get("acked_at")),
            # #96: 'untrusted' | 'bypass' for a Codex session that took a prompt 30 s ago and sent no hook (agents/codex.hooks_missing), else None
            "hooks_missing": _hooks_missing(row),
            # None for a tmux session the board has no row for (the UI guesses from the launcher and the pane command)
            "agent": row.get("agent"), "agent_session_id": row.get("agent_session_id"), "row_id": row.get("row_id"),
            "account": row.get("account"),           # the subscription account key the session last ran under (accounts.py), None = unknown
            "flags": {k: v for k, v in (row.get("flags") or {}).items() if k not in ("transcript_path", "last_result")},   # last_result is up to 20 KB: GET /api/sessions/{name} has it
            "task": chip,
        }
        if name in waits:                            # only a 'waiting' session has one: what it waits on (see _wait_kind_of)
            out[name]["wait_kind"] = waits[name]
        if rich and row.get("agent") == "codex" and not row.get("state"):
            _codex_launch_view(name, row, s["command"], out[name])
    if rich and out:
        snap = _registry_snapshot({n: s["pid"] for n, s in live.items() if s.get("pid")})
        if snap:
            try:
                registry.enrich(out, snap)           # display only: sets flags.registry on our own sessions
            except Exception as e:
                log.debug("registry enrich failed: %s", e)
    return out, False


RATE_LIMITED_MESSAGE_MAX = 200


def _rate_limited_view() -> dict | None:
    """state.rate_limited: the kv 'rate_limited' record ({value: {session, message, kind, resets_at}, at}, written by the StopFailure
    hook) with the message cut to 200 characters (the kv keeps 500; the state is polled every 3 s), or None. Always present."""
    rl = db.kv_get("rate_limited")
    v = rl.get("value") if rl else None
    if isinstance(v, dict) and isinstance(v.get("message"), str):
        rl = {**rl, "value": {**v, "message": v["message"][:RATE_LIMITED_MESSAGE_MAX]}}
    return rl


def _live_claude_sessions(projs: list[dict]) -> int:
    """Claude sessions the board has running (any state but ended), over every project's repos, root and orphans."""
    n = 0
    for p in projs or []:
        groups = [r.get("sessions") or [] for r in p.get("repos") or []] + [(p.get("root") or {}).get("sessions") or [], p.get("orphan_sessions") or []]
        n += sum(1 for ss in groups for s in ss if (s.get("agent") or "claude") == "claude" and s.get("state") != "ended")
    return n


def _mem_state(projs: list[dict]) -> dict | None:
    """state.memory: the monitor's record (memory.state_view; agents/monitor.enrich adds rates, plugin_version, compat) plus
    `stale_sessions`, an ESTIMATE of sessions the worker still counts as active that the board no longer runs: the worker's
    active_sessions minus the board's live Claude sessions, never below zero (None when the worker does not say)."""
    m = memory.state_view(db)
    if m is None:
        return None
    return {**m, "stale_sessions": _stale_estimate(m, projs)}


def _stale_estimate(m: dict, projs: list[dict] | None) -> int | None:
    act = m.get("active_sessions")
    if projs is None or not isinstance(act, int) or isinstance(act, bool):
        return None
    return max(0, act - _live_claude_sessions(projs))


def _cached_projects() -> list[dict] | None:
    """The project scan the state last built (None before the first one): the doctor's uncommitted-work check reads it, so it never scans."""
    with _scan_lock:
        return _scan_cache[1].get("projects") if _scan_cache else None


doctor.set_projects_source(_cached_projects)


def build_state(user: str) -> dict:
    global _scan_cache
    with _scan_lock:
        if _scan_cache and time.monotonic() - _scan_cache[0] < SCAN_TTL:
            st = dict(_scan_cache[1])
        else:
            sessions, down = _merged_sessions(rich=True)
            # #31: on a busy box the scan reuses each repo's git answer for up to 10 s (branch and dirty may be that old) and says so
            # in state.scan_slow; sessions, hooks and user actions are never slowed (_invalidate_scan drops the reused answers too)
            slow = health.under_load()
            st = {"tmux_down": down, "projects": projects.scan(sessions, git_max_age=projects.GIT_TTL_LOADED if slow else 0.0),
                  "scan_slow": slow}
            _scan_cache = (time.monotonic(), st)
    st["user"] = user
    st["config"] = {"code_https_port": settings.code_https_port, "projects_dir": str(settings.projects_dir),
                    "runtime": settings.runtime, "auto_continue": settings.auto_continue,
                    "ntfy": {"enabled": notify.enabled(), "subscribe_url": notify.subscribe_url(), "topic": settings.ntfy_topic},
                    "public_url": settings.public_url, "mem_viewer_url": settings.mem_viewer_url(),
                    "backup": {"restic": backup.restic_enabled(), "repo": settings.restic_repo if backup.restic_enabled() else None,
                               "restic_installed": shutil.which("restic") is not None, "push": settings.backup_push, "ns": backup.backup_ns()}}
    st["claude"] = claude_auth.status()
    st["agents"] = agents.status_all()
    st["login"] = _login_payload()
    st["pending_permissions"] = db.perm_pending()
    st["tasks"] = _tasks_view()
    st["jobs"] = _jobs_view()
    by_job = {j["id"]: j["agent"] for j in st["jobs"]}
    st["runs"] = [{**{k: (v[:400] if k == "result" and isinstance(v, str) else v) for k, v in r.items()}, "agent": by_job.get(r["job_id"], "claude")}
                  for r in db.runs(30)]
    st["clone_queue"] = clonequeue.status()
    st["last_recovery"] = db.kv_get("last_recovery")
    st["deploy"] = deploy.view(db)                        # an update waiting for the terminals to close (None when nothing is pending)
    st["memory"] = _mem_state(st["projects"])            # claude-mem worker health as the monitor last saw it (None: off, or not sampled yet)
    st["usage"] = _usage_view()                           # the kv rate_limits record; a window it lacks comes from the account's last reading (usage.rate_limits_view)
    st["usage_refresh"] = usage_refresh.view(db)          # {running, last}: a /usage refresh in flight (Usage page button) and the newest one asked for
    st["usage_codex"] = db.kv_get("rate_limits_codex")     # the Codex account's windows (agents/codex_rollout.py): {value{limit_id, plan_type, primary, secondary, credits, reached, observed_at, account}, at} | None
    st["accounts"] = account_store.decorate(_accounts_view(), db)
    st["codex_accounts"] = _codex_accounts_view(tail=False)
    st["block"] = db.kv_get(usage.KV_BLOCK)
    st["cost"] = db.kv_get(cost.KV_COST)
    st["health"] = health.snapshot()
    st["nodes"] = db.kv_get(health.KV_NODES)
    st["backup"] = health.backup_status()
    st["node_name"] = settings.node_name or st["health"]["host"]
    st["node"] = nodes.card(db, health_snap=st["health"], sessions=None if st.get("tmux_down") else _state_sessions(st["projects"]), full=False)    # the node card without agents and accounts (issue #133)
    st["claude_defaults"] = {"subagent_model": settings.subagent_model or "inherit", "fable_cap": settings.headless_fable_cap}     # CCBOARD_SUBAGENT_MODEL, CCBOARD_HEADLESS_FABLE_CAP: shown in Settings > Box
    st["rate_limited"] = _rate_limited_view()
    st["scheduler"] = {**scheduler.quota_state(db), "codex": scheduler.quota_state(db, "codex")}      # Codex's own window and back-off ride along: a Codex job never waits on Claude's
    st["version"] = ASSET_VERSION
    st.pop("dev", None)                           # the scan cache hands back the dict a previous call filled in
    if settings.dev_bypass_user:                  # present only under the dev bypass (issue #100): scripts/qa-ui.sh refuses a board that says false
        st["dev"] = {"sandboxed": settings.dev_sandboxed()}
    st["setup"] = _setup_state(st)
    return st


def _state_sessions(projs) -> list[dict]:
    """Every live session of the project scan (repo sessions, the project folder's, and the orphans), for the node card's counts."""
    out = []
    for p in projs or []:
        for r in p.get("repos") or []:
            out += r.get("sessions") or []
        out += (p.get("root") or {}).get("sessions") or []
        out += p.get("orphan_sessions") or []
    return out


def _fable_view(j: dict) -> dict | None:
    """A Claude job that resolves to Fable: {state: 'held' | 'acknowledged', at, cap, reason}; None for every other job."""
    if (j.get("agent") or "claude") != "claude":
        return None
    try:
        parts = shlex.split(j.get("args") or "")
    except ValueError:
        return None
    if not claude_adapter.resolves_to_fable(parts, os.environ):
        return None
    held = scheduler.fable_hold(j)
    ack = scheduler.job_opts(j).get("fable_ack") or {}
    return {"state": "held" if held else "acknowledged", "at": None if held else ack.get("at"), "cap": None if held else ack.get("cap"),
            "reason": held, "max": settings.headless_fable_cap}


def _jobs_view() -> list[dict]:
    """db.jobs() for the board: `agent` always set and `opts` (stored as JSON text) as an object, so the page reads a Codex job's model and
    reasoning without parsing."""
    out = []
    for j in db.jobs():
        out.append({**j, "agent": j.get("agent") or "claude", "opts": scheduler.job_opts(j) or None, "fable": _fable_view(j)})
    return out


def _login_payload() -> dict:
    """state.login and GET /api/accounts login: the login tmux session ({running, url, tail}) plus what Settings is adding through it
    ({adding, email, started_at, result}, app/account_store.py)."""
    return {**claude_auth.login_state(), **account_store.login_view()}


def _codex_accounts_view(tail: bool = True) -> dict:
    """state.codex_accounts and GET /api/codex-accounts: the saved Codex accounts (app/codex_accounts.py). The state's copy leaves the login's terminal
    output out. A failure answers the empty, unsupported shape, never an error."""
    try:
        return codex_accounts.view(db, tail=tail)
    except Exception as e:
        log.warning("codex accounts view failed: %s", e.__class__.__name__)
        return {"current": None, "list": [], "store": {"supported": False, "add": False, "reason": codex_accounts.REASON_NOT_INSTALLED, "count": 0},
                "login": {"running": False, "adding": False, "label": None, "replace_key": None, "started_at": None, "url": None, "code": None, "result": None,
                          **({"tail": []} if tail else {})}}


def _usage_view():
    """state.usage (see usage.rate_limits_view); a failure answers the plain kv record, never an error."""
    try:
        return usage.rate_limits_view(db)
    except Exception as e:
        log.warning("usage view failed: %s", e.__class__.__name__)
        return db.kv_get("rate_limits")


def _accounts_view(full: bool = False) -> dict:
    """state.accounts: {current: key | None, list: [{key, email, name, label, plan, rl_5h, rl_7d, resets_5h, resets_7d, current}]}, always
    present. Built from the kv and the sample cache only (app/accounts.py); a failure answers the empty shape, never an error."""
    try:
        return accounts.view(db, full=full)
    except Exception as e:
        log.warning("accounts view failed: %s", e)
        return {"current": None, "list": []}


def _setup_state(st: dict) -> dict:
    """first_run: the board has never been used (no project folder and no session row, ever); ok: tmux is up and at least
    one agent is installed and logged in. The full box checks are GET /api/doctor."""
    first_run = not st.get("projects") and not db.any_session_ever()
    ready = any(a.get("installed") and a.get("loggedIn") for a in (st.get("agents") or {}).values())
    return {"first_run": first_run, "ok": bool(ready and not st.get("tmux_down"))}


def _invalidate_scan() -> None:
    global _scan_cache
    with _scan_lock:
        _scan_cache = None
    projects.git_cache_clear()                    # a user action shows its repo change at once, busy box or not


STATE_GZIP_MIN = 1024                             # bytes; a smaller answer is sent as is


@app.get("/api/state")
def api_state(request: Request):
    """The 3 s poll. Gzipped here (not by an app-wide middleware: the live tail is a server-sent event stream) when the client
    accepts it: on the box the answer shrinks about 3.7x for about 0.3 ms of CPU (#46). Headers otherwise as JSONResponse."""
    st = build_state(request.state.user)
    if "gzip" not in (request.headers.get("accept-encoding") or "").lower():
        return st
    body = json.dumps(jsonable_encoder(st), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")   # what JSONResponse sends
    if len(body) < STATE_GZIP_MIN:
        return Response(body, media_type="application/json")
    return Response(gzip.compress(body, 6), media_type="application/json", headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})


# ---------- agents, doctor, session detail, external sessions (read only) ----------

@app.get("/api/agents")
def api_agents():
    """Every agent the board can launch: identity, install/auth/hooks state, the launcher's option schema, the slash registry."""
    return {"agents": {a.name: a.describe() for a in agents.all()}}


@app.get("/api/skills")
def api_skills():
    """The skills installed on the box (the user's own and the installed plugins'), most used first: {agent, skills: [{name, description, source, uses?}]}.
    Front matter only, cached 60 s (app/skills.py). `uses` is what this board saw typed, absent for a skill never seen used."""
    return skills.listing(db)


@app.get("/api/doctor")
def api_doctor(group: str | None = None, refresh: str | None = None):
    """Box and agent checks, each pass|warn|fail|skip with a fix hint. A plain `def`: a check may block for up to its 5 s cap."""
    try:
        return doctor.run(group or None, refresh == "1", db=db)
    except ValueError as e:                       # unknown group
        raise projects.BadRequest(str(e))


# ---------- memory: the claude-mem proxy (v0.5.20, read only; docs/memory-api.md) ----------

def _mem_health_view(h: dict | None) -> dict:
    """GET /api/memory/health: the monitor's record (memory.health() plus rates, plugin version and compat; agents/monitor.enrich) with
    `up` and the stale-session estimate of state.memory, or {state: 'off'} when CCBOARD_CLAUDE_MEM=0, or {state: 'unknown'} before the
    first sample."""
    if not settings.claude_mem:
        return {"state": "off", "up": False, "reason": "claude-mem is turned off (CCBOARD_CLAUDE_MEM=0)"}
    if not isinstance(h, dict):
        return {"state": "unknown", "up": False, "reason": "not sampled yet: the board probes the worker every 20 s", **memory_proxy.compat_info(None)}
    projs = _scan_cache[1].get("projects") if _scan_cache else None          # the state poll's last scan; None before the first poll
    return {**h, "up": h.get("state") == "up", "stale_sessions": _stale_estimate(h, projs), **memory_proxy.compat_info(h)}


@app.get("/api/memory/health")
def api_memory_health(refresh: str | None = None):
    """The claude-mem worker's health as the monitor last saw it (every 20 s); ?refresh=1 runs one live probe (at most 3.5 s), stores it
    the way the monitor does and answers it. A plain `def`: the live probe blocks."""
    if refresh == "1" and settings.claude_mem:
        return _mem_health_view(mem_monitor.sample(db))
    return _mem_health_view(memory.state_view(db))


def _mem_answer(fn, *args, **kw):
    """A proxy answer, or its 503 body ({error, state: down|degraded, up: false, reason, reason_code}) when the worker could not answer
    and nothing was cached. An incompatible shape is a 200 with state 'incompatible' (the page shows one line; the worker is running)."""
    if not settings.claude_mem:
        return JSONResponse({"error": "claude-mem is turned off (CCBOARD_CLAUDE_MEM=0)", "state": "off", "up": False,
                             "reason": "claude-mem is turned off (CCBOARD_CLAUDE_MEM=0)", "reason_code": "off"}, status_code=503)
    h = memory.state_view(db)
    try:
        return fn(*args, health=h, **kw)
    except memory_proxy.WorkerError as e:
        return JSONResponse({**e.body(), **memory_proxy.compat_info(h)}, status_code=503)


@app.get("/api/memory/{project}/observations")
def api_memory_observations(project: str, limit: str | None = None, offset: str | None = None, before: str | None = None,
                            repo: str | None = None, type: str | None = None, agent: str | None = None, session: str | None = None,
                            since: str | None = None, until: str | None = None):
    """One project's observations, newest first, merged over its claude-mem keys (docs/memory-api.md). A plain `def`: it blocks."""
    return _mem_answer(memory_proxy.observations, project, limit=limit, offset=offset, before=before, repo=repo, type_=type, agent=agent,
                       session=session, since=since, until=until)


@app.get("/api/memory/{project}/summaries")
def api_memory_summaries(project: str, limit: str | None = None, offset: str | None = None, before: str | None = None,
                         repo: str | None = None, agent: str | None = None, session: str | None = None, since: str | None = None,
                         until: str | None = None):
    return _mem_answer(memory_proxy.summaries, project, limit=limit, offset=offset, before=before, repo=repo, agent=agent, session=session,
                       since=since, until=until)


@app.get("/api/memory/{project}/search")
def api_memory_search(project: str, q: str | None = None, type: str | None = None, obs_type: str | None = None, limit: str | None = None,
                      offset: str | None = None, agent: str | None = None, order: str | None = None):
    return _mem_answer(memory_proxy.search, project, q, type_=type, obs_type=obs_type, limit=limit, offset=offset, agent=agent, order=order)


@app.get("/api/memory/{project}/timeline")
def api_memory_timeline(project: str, anchor: str | None = None, depth_before: str | None = None, depth_after: str | None = None):
    return _mem_answer(memory_proxy.timeline, project, anchor, depth_before=depth_before, depth_after=depth_after)


@app.get("/api/memory/{project}/palace")
def api_memory_palace(project: str, subagents: str | None = None, drawers: str | None = None):
    """Wings (repos), rooms (concepts, types), drawers and gotchas over the project's newest observations; cached 30 s. The shape is in
    memory_proxy.palace's docstring and docs/memory-api.md."""
    return _mem_answer(memory_proxy.palace, project, subagents=subagents == "1", drawers=drawers)


class MemoryPrefsIn(BaseModel):
    writeback: bool | None = None


def _mem_prefs() -> dict:
    pl = memory.plugin_status() if settings.claude_mem else {"installed": False, "enabled": None}
    reason = ("claude-mem is turned off (CCBOARD_CLAUDE_MEM=0)" if not settings.claude_mem else
              "the claude-mem plugin is not installed" if not pl["installed"] else
              "the claude-mem plugin is disabled in Claude's settings" if pl["enabled"] is False else None)
    return {"prefs": {"writeback": memory.writeback_on(db)}, "available": reason is None, "reason": reason,
            "writeback_sends": {"title": "Task: <title>",
                                "text": f"the task's result, at most {memory.SAVE_TEXT_MAX // 1024} KB, cut at a line break and marked",
                                "project": "the repo's claude-mem key (<repo>, or <repo>/<worktree> for a task in a worktree)",
                                "metadata": ["source", "task_id", "agent"], "never": ["the prompt"]}}


@app.get("/api/memory/prefs")
def api_memory_prefs():
    """{prefs: {writeback}, available, reason, writeback_sends}: the write-back switch (off by default) and what it sends."""
    return _mem_prefs()


@app.put("/api/memory/prefs")
def api_memory_prefs_set(body: MemoryPrefsIn):
    if body.writeback is not None:
        memory.set_writeback(db, body.writeback)
    return _mem_prefs()


class PreflightIn(BaseModel):
    url: str


@app.post("/api/preflight/clone")
def api_preflight_clone(body: PreflightIn):
    """What a clone URL would meet, before it is queued (the new-project wizard's chips): `git ls-remote --symref` that never prompts, speaks only https and ssh,
    follows no redirect and is killed (its whole process group) after 12 s. {reachable, default_branch, needs_auth, heads: [branch], name: the repo name a clone would
    derive | null, error: one line | null}. 400 for a URL a clone would refuse (projects.check_url: https, ssh and git@host: only, never an IP literal, localhost or a
    local, private or tailnet name unless CCBOARD_CLONE_ALLOWED_HOSTS lists it); a remote that is down or private is a 200 with reachable false. A plain `def`: it blocks
    for up to 12 s."""
    return preflight.preflight_clone(body.url)


@app.get("/api/sessions/{name}")
def api_session(name: str):
    try:
        project, repo, session = tmux.split_name(name)
    except ValueError:
        raise projects.BadRequest("not a ccboard session name")
    sessions, down = _merged_sessions(rich=True)
    if down:
        raise tmux.TmuxDown("tmux server is down")
    s = sessions.get(name)
    if s is None:
        raise projects.NotFound(f"session {name} not found")
    return {"tmux": name, "project": project, "repo": repo, "name": session, "agent": s["agent"], "state": s["state"],
            "state_at": s["state_at"], "last_prompt": s["last_prompt"], "last_message": s["last_message"], "stats": s["stats"],
            "flags": s["flags"], "agent_session_id": s["agent_session_id"], "account": s["account"], "task": s["task"],
            "hooks_missing": s.get("hooks_missing"),
            "pending": [p for p in db.perm_pending() if p["tmux_name"] == name],
            # viewers and win come from tmux (list-clients, the session window's size); shell_version (the attach wrapper's) is
            # still unset
            "viewers": s["viewers"], "win": s["win"], "shell_version": ASSET_VERSION}


@app.get("/api/external")
def api_external(agent: str | None = None, project: str | None = None):
    """Agent sessions running on this box that the board did not start: Claude's own registry (file reads, cached 30 s) and Codex's thread
    index plus rollouts (read-only, cached 30 s; the originator of a thread that is not codex-tui rides along as `badge`).
    {claude: [...], codex: [...], at}: `at` is the registry's scan time (the Codex scan's when only Codex is asked)."""
    if agent not in (None, "", "claude", "codex"):
        raise projects.BadRequest("agent must be claude or codex")
    if project:
        projects.check_name("project", project)
    want_claude, want_codex = agent in (None, "", "claude"), agent in (None, "", "codex")
    claude_ext: list = []
    at = None
    if want_claude:
        try:
            pane_pids = {n: s["pid"] for n, s in tmux.list_sessions().items() if s.get("pid")}
        except tmux.TmuxDown:
            pane_pids = {}
        snap = _registry_snapshot(pane_pids) or {"external": [], "scanned_at": None}
        claude_ext, at = snap["external"], snap["scanned_at"]
        if project:
            base = str(settings.projects_dir / project)
            claude_ext = [e for e in claude_ext if (e.get("cwd") or "") == base or (e.get("cwd") or "").startswith(base + "/")]
    codex_ext: list = []
    if want_codex:
        codex_ext, codex_at = codex_discovery.external(db, project)
        if not want_claude:
            at = codex_at
    return {"claude": claude_ext, "codex": codex_ext, "at": at}


def _external_target(agent: str, sid: str) -> dict:
    """The external session `sid` of `agent` that POST /api/external/<agent>/<sid>/open acts on, as {cwd, project, repo, name}. 404 when the
    board's last scan does not list it (or it is one of the board's own sessions), 400 when its directory is not a project directory."""
    if agent == "codex":
        e = codex_discovery.find(db, sid)
        cwd = e.get("cwd") if e else None
    else:
        try:
            pane_pids = {n: s["pid"] for n, s in tmux.list_sessions().items() if s.get("pid")}
        except tmux.TmuxDown:
            pane_pids = {}
        snap = _registry_snapshot(pane_pids) or {"external": []}
        e = next((x for x in snap["external"] if (x.get("session_id") or "").lower() == sid), None)
        cwd = e.get("cwd") if e else None
    if e is None:
        raise projects.NotFound(f"no external {agent} session {sid} (it may have been opened on the board already)")
    pr = codex_discovery.project_repo(cwd)
    if not pr or not cwd or not Path(cwd).is_dir():
        raise projects.BadRequest(f"this session's directory ({cwd or 'unknown'}) is not under the projects folder, so the board cannot open it")
    projects.contained(Path(cwd))                      # a symlink, or a path that resolves outside PROJECTS_DIR, is refused like everywhere else
    return {"cwd": cwd, "project": pr[0], "repo": pr[1], "name": e.get("name") or e.get("title")}


@app.post("/api/external/{agent}/{sid}/open", status_code=201)
def api_external_open(agent: str, sid: str):
    """Open an external session on the board: a new board session in its directory that resumes it (`codex resume <id>` or
    `claude --resume <id>`, built by the adapter). Only sessions the last scan lists and whose directory is under PROJECTS_DIR; the
    resumed conversation is then the board's own (its id is bound to the new row, so it leaves the external list)."""
    require_real_launch_ok()   # issue #100: refuses (409) on a dev board whose directories could reach the real home
    if agent not in ("claude", "codex"):
        raise projects.BadRequest("agent must be claude or codex")
    if not UUID_RE.match(sid or ""):
        raise projects.BadRequest("session id must be a UUID")
    sid = sid.lower()
    ag = agents.get(agent)
    if not ag.bin():
        raise projects.BadRequest(f"{agent} is not installed on this box")
    for name, row in db.open_rows().items():                    # already running on the board: go there, never a second resume
        if (row.get("agent_session_id") or "").lower() == sid:
            raise projects.Conflict(f"session {sid} is already open on the board as {name}")
    t = _external_target(agent, sid)
    project, repo, cwd = t["project"], t["repo"], t["cwd"]
    session = _free_session_name(project, repo)
    name = tmux.tmux_name(project, repo, session)
    plan = ag.launch_plan(LaunchReq(kind="resume", session_name=session, cwd=cwd, resume_id=sid))
    real = _start_session(name, project, repo, session, "resume", cwd, cmd_line=plan.cmd_line, claude_session_id=sid, agent=agent,
                          opts=plan.opts_clean)
    _invalidate_scan()                                          # the next poll drops the thread from the external list: the new row owns its id
    return {"tmux": real, "attach_url": f"/tty/?arg={real}", "agent": agent, "agent_session_id": sid, "claude_session_id": sid,
            "cmd": plan.cmd_line, "project": project, "repo": repo}


# ---------- time series (samples) ----------

SERIES_MAX_COMBOS = 8            # series x keys one request may ask for
SERIES_MAX_POINTS = 500
SERIES_MAX_BACK = timedelta(days=400)     # past the longest retention (180 d) there is nothing to read


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)            # the one clock the time-series endpoints read (tests freeze it)


def _int_param(name: str, raw, lo: int, hi: int, default: int, clamp: bool = False) -> int:
    """A query integer in [lo, hi]; absent means the default. A non-number is a 400 (not FastAPI's 422), and so is a value outside
    the range unless `clamp`."""
    if raw is None or str(raw).strip() == "":
        return default
    try:
        v = int(str(raw).strip())
    except ValueError:
        raise projects.BadRequest(f"{name} must be an integer")
    if clamp:
        return max(lo, min(hi, v))
    if not lo <= v <= hi:
        raise projects.BadRequest(f"{name} must be between {lo} and {hi}")
    return v


def _csv(raw: str | None) -> list[str]:
    out: list[str] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def _series_names(raw: str | None, events_only: bool = False) -> list[str]:
    names = _csv(raw)
    if not names:
        raise projects.BadRequest("series is required (comma-separated: " + ", ".join(sorted(samples.CATALOGUE)) + ")")
    for n in names:
        spec = samples.CATALOGUE.get(n)
        if spec is None:
            raise projects.BadRequest(f"unknown series {n!r}")
        if events_only and spec.get("agg") != "events":
            raise projects.BadRequest(f"{n!r} is not an event series (use state or lim)")
    return names


def _window(since: str | None, until: str | None, now: datetime) -> tuple[datetime, datetime]:
    try:
        t0 = samples.parse_since(since or "24h", now)
        t1 = samples.parse_since(until, now) if until else now
    except (ValueError, TypeError):
        raise projects.BadRequest("since/until must be 1h, 6h, 24h, 7d, 30d, 90d or an ISO time")
    t0 = max(t0, now - SERIES_MAX_BACK)
    if t0 >= t1:
        raise projects.BadRequest("since must be before until")
    return t0, t1


@app.get("/api/series")
def api_series(series: str = "", key: str | None = None, since: str = "24h", until: str | None = None,
               points: str | None = None):
    """Downsampled time series: ?series=rl_5h,rl_7d&key=claude|*&since=24h&until=&points=500 -> {since, until, step, t, series, meta}."""
    names = _series_names(series)
    asked = _csv(key)
    keys = None if not asked or "*" in asked else asked              # None = every key the series has (the payload caps the combos)
    if len(names) * (len(keys) if keys is not None else 1) > SERIES_MAX_COMBOS:
        raise projects.BadRequest(f"at most {SERIES_MAX_COMBOS} series x key combinations per request")
    n = _int_param("points", points, 2, SERIES_MAX_POINTS, SERIES_MAX_POINTS, clamp=True)
    t0, t1 = _window(since, until, _utcnow())
    try:
        body = samples.series_payload(db, names, keys, t0, t1, n)
    except ValueError as e:                              # the payload's own limits (combos, window): the same 400 the checks above give
        raise projects.BadRequest(str(e))
    return JSONResponse(body, headers={"Cache-Control": "private, max-age=10"})


@app.get("/api/series/events")
def api_series_events(series: str = "state", since: str = "24h", key: str | None = None):
    """Event series (state transitions, rate-limit episodes): {events: [{t, key, v, m}], truncated} (cap 5000)."""
    names = _series_names(series, events_only=True)
    if len(names) != 1:
        raise projects.BadRequest("ask for one event series at a time")
    t0, _ = _window(since, None, _utcnow())
    k = (key or "").strip()
    try:
        return samples.events_payload(db, names[0], t0, None if k in ("", "*") else k)
    except ValueError as e:
        raise projects.BadRequest(str(e))


USAGE_SUMMARY_TTL = 30.0                              # seconds; the Usage page polls every 60 s and the payload is a full rebuild
_usage_summary_cache: dict[tuple[int, int, int, str], tuple[float, dict]] = {}
_usage_summary_lock = threading.Lock()


@app.get("/api/usage/summary")
def api_usage_summary(days: str | None = None, tz_min: str | None = None, basis: str | None = None):
    """Cost, hours and limit episodes bucketed from the samples table in the viewer's zone (tz_min, default 345 = Asia/Kathmandu). basis=est values every
    dollar at ccusage's reported cost plus a list-price estimate for models it prices at zero (issue #95); anything else is the reported basis."""
    d = _int_param("days", days, 1, 365, 30)
    tz = _int_param("tz_min", tz_min, -720, 840, 345)
    bs = usage_summary.BASIS_EST if (basis or "").strip().lower() == usage_summary.BASIS_EST else usage_summary.BASIS_REPORTED
    now = time.monotonic()
    with _usage_summary_lock:
        hit = _usage_summary_cache.get((id(db), d, tz, bs))
        if hit and now - hit[0] < USAGE_SUMMARY_TTL:
            return hit[1]
    out = usage_summary.build(db, d, tz, _utcnow(), bs)
    with _usage_summary_lock:
        _usage_summary_cache[(id(db), d, tz, bs)] = (now, out)
        for k in [k for k in _usage_summary_cache if k[0] != id(db)]:   # a previous app's entries (tests) never linger
            _usage_summary_cache.pop(k, None)
    return out


class AccountPatchIn(BaseModel):
    label: str | None = None


@app.get("/api/accounts")
def api_accounts():
    """The subscription accounts the board has seen: {current, list: [{key, email, name, label, plan, org, org_id, tier, config_dir,
    first_seen, last_seen, rl_5h, rl_7d, rl_5h_at, rl_7d_at, resets_5h, resets_7d, current, saved}], store: {supported, reason, count},
    login: {running, url, tail, adding, email, started_at, result}} (the kv accounts record plus each account's newest window readings;
    rl_* are used percentages, resets_* epoch seconds; `saved` = a login is saved for the account, so it can be switched to)."""
    out = account_store.decorate(accounts.view(db, full=True), db)
    out["login"] = _login_payload()
    return out


@app.patch("/api/accounts/{key}")
def api_account_patch(key: str, body: AccountPatchIn):
    """Rename an account: {label} (at most 60 characters; '' or null clears it). The label is the only editable field."""
    if "label" not in body.model_fields_set:
        raise projects.BadRequest("label is required")
    try:
        accounts.set_label(db, key, body.label)
    except KeyError:
        raise projects.NotFound("no such account")
    return next(r for r in accounts.view(db, full=True)["list"] if r["key"] == key)


# ---- saved logins and the account switch (app/account_store.py). The error body carries the message as both `detail` and `error`.

def _refuse(status: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message, "error": message}, status_code=status)


class AccountLoginIn(BaseModel):
    email: str | None = None
    restart: bool = False


class AccountSwitchIn(BaseModel):
    continue_parked: bool = False


class AccountCodeIn(BaseModel):                       # same body as CodeIn (below): a model used in an annotation must exist before the route
    code: str


@app.post("/api/accounts/login", status_code=202)
def api_account_login(body: AccountLoginIn | None = None):
    """Add an account: `claude auth login` runs in the login tmux session against an empty config dir of its own, so the live login is
    untouched. {email?: str | null, restart?: bool}. 202 {ok}; 400 for a bad email; 409 when saved logins are not supported (the per-system
    text of account_store.support_reason(): macOS, Windows, a config dir on a mount that ignores permissions) or a
    login is already running (restart: true replaces it). The sign-in link is state.login.url; the code goes to /api/accounts/login/code."""
    require_real_launch_ok()   # issue #100: refuses (409) on a dev board whose directories could reach the real home
    body = body or AccountLoginIn()
    if not account_store.supported():
        return _refuse(409, account_store.why_not())
    try:
        email = claude_auth.clean_email(body.email)
    except ValueError as e:
        return _refuse(400, str(e))
    try:
        account_store.start_login(db, email, restart=body.restart)
    except account_store.StoreError as e:
        return _refuse(409, str(e))
    return {"ok": True}


@app.post("/api/accounts/login/code")
def api_account_login_code(body: AccountCodeIn):
    """The code the browser showed after signing in ('code#state'). Same validation and errors as /api/claude/login/code; then the login is
    finished at once (a short poll in the background) instead of at the next 15 s tick: state.login.result says how it ended."""
    try:
        claude_auth.submit_code(body.code)
    except ValueError as e:
        return _refuse(400, str(e))
    except LookupError as e:
        return _refuse(409, str(e))
    account_store.finalize_soon(db)
    return {"ok": True}


@app.delete("/api/accounts/login")
def api_account_login_cancel():
    """Give up the login that was started from Settings: its session and its pending directory go."""
    account_store.cancel_login()
    return {"ok": True}


@app.delete("/api/accounts/problem")
def api_account_problem_clear():
    """Dismiss the 'login not valid' notice (state.accounts.problem): the board was told by a failed turn, and nothing but a sign of life from
    that account (or a login for it finishing) takes it down by itself. {ok, cleared: whether there was one}."""
    return {"ok": True, "cleared": login_problem.clear(db)}


@app.post("/api/accounts/{key}/switch")
def api_account_switch(key: str, body: AccountSwitchIn | None = None):
    """Make an account the live login (its saved copy replaces the live credentials; running sessions follow on their next request).
    {continue_parked?: bool} types `continue` into sessions parked on a limit after a real switch. 200 {ok, already, from, to, continued:
    [session names], accounts}; 404 unknown account; 409 {detail} when there is no saved login, a login is in progress, the current login
    could not be saved first, or saved logins are not supported."""
    try:
        res = account_store.switch(db, key)
    except account_store.UnknownAccount as e:
        return _refuse(404, str(e))
    except account_store.StoreError as e:
        return _refuse(409, str(e))
    continued: list[str] = []
    if body and body.continue_parked and not res.get("already"):
        try:
            continued = autoresume.continue_parked(db)
        except Exception as e:
            log.warning("continue after the switch failed: %s", e.__class__.__name__)
    _invalidate_scan()
    return {**res, "continued": continued, "accounts": account_store.decorate(accounts.view(db, full=True), db)}


@app.delete("/api/accounts/{key}/saved")
def api_account_forget(key: str):
    """Delete an account's saved login (its usage history stays). 404 unknown account; 409 for the live account."""
    try:
        res = account_store.forget(db, key)
    except account_store.UnknownAccount as e:
        return _refuse(404, str(e))
    except account_store.StoreError as e:
        return _refuse(409, str(e))
    return {**res, "accounts": account_store.decorate(accounts.view(db, full=True), db)}


# ---- saved Codex logins (app/codex_accounts.py): the same shapes under /api/codex-accounts. Errors carry {detail, error} like the Claude ones.

class CodexAccountLoginIn(BaseModel):
    label: str | None = None
    restart: bool = False
    replace_key: str | None = None


class CodexAccountPatchIn(BaseModel):
    label: str | None = None


@app.get("/api/codex-accounts")
def api_codex_accounts():
    """The saved Codex accounts: {current, list: [{key, label, account_id, plan, saved, current, added_at, last_seen}], store: {supported, add, reason,
    count}, login: {running, adding, label, started_at, url, code, tail, result}}. `saved` = a login is saved for the account, so it can be switched
    to; account_id and plan are learned from the rollouts of sessions run while the account was live (null until then); `store.add` is false
    where `codex login --device-auth` is missing (`store.reason` says what to do); url and code are the sign-in link and the one-time code
    of the login being added."""
    return _codex_accounts_view(tail=True)


@app.post("/api/codex-accounts/login", status_code=202)
def api_codex_account_login(body: CodexAccountLoginIn | None = None):
    """Add a Codex account: `codex login --device-auth` runs in the login tmux session with a CODEX_HOME of its own, so the live login is untouched.
    {label: str (1..60 characters, required), restart?: bool, replace_key?: str}. 202 {ok}; 400 for a missing or bad label; 409 when codex is
    not installed or lacks --device-auth, or a login is already running (restart: true replaces it). The link and the one-time code (typed on
    the page; nothing is pasted back) are state.codex_accounts.login.url / .code; the login ends by itself when auth.json appears.
    `replace_key` is a log-in-again of that saved account: the login that finishes replaces ITS saved login (same key and label, no second
    account; `label` is not needed then) and, when it is the live account, goes live at once unless one of the board's own Codex sessions is
    open (state.codex_accounts.login.result says: {replaced, live, why}). 404 for a key the board never saw."""
    require_real_launch_ok()   # issue #100: refuses (409) on a dev board whose directories could reach the real home
    body = body or CodexAccountLoginIn()
    key = body.replace_key or None
    label = None
    if key is None:
        try:
            label = codex_accounts.clean_label(body.label)
        except ValueError as e:
            return _refuse(400, str(e))
    try:
        codex_accounts.start_login(db, label, restart=body.restart, replace_key=key)
    except codex_accounts.UnknownAccount as e:
        return _refuse(404, str(e))
    except codex_accounts.StoreError as e:
        return _refuse(409, str(e))
    return {"ok": True}


@app.delete("/api/codex-accounts/login")
def api_codex_account_login_cancel():
    """Give up the login that was started from Settings: its session and its pending directory go."""
    codex_accounts.cancel_login()
    return {"ok": True}


@app.delete("/api/codex-accounts/notice")
def api_codex_account_notice_dismiss():
    """Drop the one-time notice of a split-off Codex account (#36: "A different Codex login was detected and saved as a new account"); a rename
    of that account drops it too. 200 {ok, dismissed}."""
    return {"ok": True, "dismissed": codex_accounts.dismiss_notice(db)}


@app.post("/api/codex-accounts/logout")
def api_codex_account_logout():
    """Log the box out of Codex: the live login is saved into its slot first (an unknown login is kept as a new account), then auth.json is removed and
    the current account cleared, so the account can be switched back to. 200 {ok, was, warnings: [text], accounts}; `was` is the account's key, null when
    nobody was logged in (not an error). 409 {detail} while a login from Settings is in flight or one of the board's own Codex sessions is open (close
    them first), or when the login could not be saved first; nothing changes then."""
    try:
        res = codex_accounts.logout(db)
    except codex_accounts.StoreError as e:
        return _refuse(409, str(e))
    _invalidate_scan()
    return {**res, "accounts": _codex_accounts_view(tail=False)}


@app.patch("/api/codex-accounts/{key}")
def api_codex_account_patch(key: str, body: CodexAccountPatchIn):
    """Rename a Codex account: {label} (1..60 characters; an account has no other name, so an empty label is a 400). Answers the account's row."""
    if "label" not in body.model_fields_set:
        return _refuse(400, "label is required")
    try:
        return codex_accounts.set_label(db, key, body.label)
    except ValueError as e:
        return _refuse(400, str(e))
    except codex_accounts.UnknownAccount as e:
        return _refuse(404, str(e))


@app.post("/api/codex-accounts/{key}/switch")
def api_codex_account_switch(key: str):
    """Make a Codex account the live login (its saved copy replaces auth.json; a running Codex keeps the login it started with). 200 {ok, already,
    from, to, warnings: [text], accounts}; 404 unknown account; 409 {detail} when there is no saved login, a login is in progress, one of the
    board's own Codex sessions is open, the current login could not be saved first, or codex is not installed."""
    try:
        res = codex_accounts.switch(db, key)
    except codex_accounts.UnknownAccount as e:
        return _refuse(404, str(e))
    except codex_accounts.StoreError as e:
        return _refuse(409, str(e))
    _invalidate_scan()
    return {**res, "accounts": _codex_accounts_view(tail=False)}


@app.delete("/api/codex-accounts/{key}/saved")
def api_codex_account_forget(key: str):
    """Delete a Codex account's saved login (the account's record stays, unsaved). 404 unknown account; 409 for the live account."""
    try:
        res = codex_accounts.forget(db, key)
    except codex_accounts.UnknownAccount as e:
        return _refuse(404, str(e))
    except codex_accounts.StoreError as e:
        return _refuse(409, str(e))
    return {**res, "accounts": _codex_accounts_view(tail=False)}


# ---------- projects & repos ----------

class ProjectIn(BaseModel):
    name: str
    url: str | None = None


class RepoIn(BaseModel):
    name: str | None = None
    url: str | None = None


def _launch_clone(project: str, repo: str, path: Path, url: str, cleanup: list[Path]) -> str:
    """Start the clone session; on any failure remove the freshly created dirs in `cleanup`."""
    name = tmux.tmux_name(project, repo, "clone")
    try:
        # No redirects: a public host must not bounce the clone to an internal one (a renamed repo already fails the wizard's probe, which shows
        # the new URL). On a git that has it, https is pinned to the addresses check_url accepted (cached), so git cannot resolve the name again.
        pin = projects.clone_pin(url) if preflight.pin_supported() else None
        cmd = (["git", "-c", "http.followRedirects=false"] + (["-c", preflight.pin_option(pin)] if pin else [])
               + ["clone", "--progress", "--", url, "."])
        line = shlex.join(cmd) + " && exit"
        if tmux.has_session(name):
            raise projects.Conflict("a clone is already running for this repo")
        _start_session(name, project, repo, "clone", "clone", str(path), cmd_line=line, claude_session_id=None,
                       add_dirs=[], agent="shell", env_extra={"GIT_ALLOW_PROTOCOL": preflight.ALLOW_PROTOCOL})      # https and ssh only
    except Exception:
        for d in cleanup:
            try:
                d.rmdir()
            except OSError:
                pass
        raise
    return name


@app.post("/api/projects", status_code=201)
def api_create_project(body: ProjectIn):
    projects.check_name("project", body.name)
    rname = None
    if body.url:
        # Validate everything that can fail before touching the disk.
        projects.check_url(body.url)
        rname = projects.derive_repo_name(body.url)
        if not rname:
            raise projects.BadRequest("could not derive a repo name from the URL; create the project, then add the repo with a name")
        if tmux.has_session(tmux.tmux_name(body.name, rname, "clone")):
            raise projects.Conflict("a clone is already running for this repo")
    p = projects.create_project(body.name)
    result = {"name": body.name, "path": str(p)}
    if body.url:
        rname, rpath = projects.prepare_repo_clone(body.name, rname, body.url)
        result["repo"] = rname
        result["clone_session"] = _launch_clone(body.name, rname, rpath, body.url, cleanup=[rpath, p])
    _invalidate_scan()
    return result


@app.get("/api/github/repos")
def api_github_repos(owner: str | None = None, archived: bool = False):
    if owner is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner):
        raise projects.BadRequest("owner must be a GitHub user or org name")
    return {"repos": github.list_repos(owner or None, include_archived=archived), "protocol": github.git_protocol()}


class BulkIn(BaseModel):
    repos: list[dict]


@app.post("/api/projects/{project}/repos/bulk", status_code=202)
def api_bulk_clone(project: str, body: BulkIn):
    projects.check_name("project", project)
    p = projects.project_path(project)
    if not p.is_dir():
        projects.create_project(project)
    items = []
    for r in body.repos[:500]:
        url = projects.check_url(str(r.get("url") or ""))
        name = r.get("name") or projects.derive_repo_name(url)
        if not name:
            raise projects.BadRequest(f"cannot derive a name for {url}")
        items.append({"project": project, "repo": projects.check_new_repo_name(str(name)), "url": url})
    n = clonequeue.enqueue(items)
    clonequeue.step(_launch_clone)
    _invalidate_scan()
    return {"queued": n, "project": project}


@app.post("/api/clone-queue/clear")
def api_clone_queue_clear():
    clonequeue.clear_done()
    _invalidate_scan()
    return {"ok": True}


@app.delete("/api/projects/{project}")
def api_delete_project(project: str):
    p = projects.project_path(project)
    if not p.is_dir():
        raise projects.NotFound(f"project {project} not found")
    killed = tmux.kill_prefix(project + tmux.SEP)
    for k in killed:
        db.end(k, "project_deleted")
    projects.remove_tree(p)
    _invalidate_scan()
    return {"deleted": project, "killed_sessions": killed}


@app.post("/api/projects/{project}/repos", status_code=201)
def api_add_repo(project: str, body: RepoIn):
    if body.url:
        rname, rpath = projects.prepare_repo_clone(project, body.name, body.url)
        sess = _launch_clone(project, rname, rpath, body.url, cleanup=[rpath])
        _invalidate_scan()
        return {"name": rname, "path": str(rpath), "clone_session": sess}
    if not body.name:
        raise projects.BadRequest("give a repo name or a clone URL")
    r = projects.add_repo_blank(project, body.name)
    _invalidate_scan()
    return {"name": body.name, "path": str(r)}


@app.delete("/api/projects/{project}/repos/{repo}")
def api_remove_repo(project: str, repo: str):
    r = projects.repo_path(project, repo)
    if not r.is_dir():
        raise projects.NotFound(f"repo {project}/{repo} not found")
    if r == projects.project_path(project):
        raise projects.BadRequest("this repo is the project folder itself; delete the project instead")
    killed = tmux.kill_prefix(tmux.tmux_name(project, repo, "") )
    for k in killed:
        db.end(k, "project_deleted")
    projects.remove_tree(r)
    _invalidate_scan()
    return {"removed": f"{project}/{repo}", "killed_sessions": killed}


# ---------- tasks (worktree + branch per task) ----------

TASK_PROMPT_HEAD = 600     # chars of the prompt a backlog/queued row carries in the poll payload; GET /api/tasks/{id} has the rest


def _tasks_view(rows: list[dict] | None = None) -> list[dict]:
    """The state.tasks rows (all unarchived tasks, or just `rows`, db.tasks() dicts). Backlog and queued rows also carry
    `prompt` (its first TASK_PROMPT_HEAD characters) and `prompt_len` (the full length: prompt shorter than prompt_len means
    fetch GET /api/tasks/{id} before editing); every other row has prompt None. The runtime's fields (v0.5.14b): `result` (head, 300
    characters), `result_at`, `done_at`, `closed_at` (when its session was closed by auto-close / Close session: sessions.ended_reason
    'auto_close'), `autoclose` (the session's flags.autoclose when it is this task's: {task, due} a countdown, {task, held:
    'question'} a hold that puts the card in needs_you, {task, closing: true}, {task, due, waiting} while a guard defers the close),
    `chain` ({i, n}: step i of n), `limit_hold` ({kind, resets_at, pct}: a queued step waiting for the limit window) and `turns_ahead`
    (a prompt handed to a working Claude session: the Stops that come before its own turn; above 0 the card reads Queued)."""
    rows = db.tasks() if rows is None else rows
    if any(t.get("session_row") is not None or t.get("tmux_name") for t in rows):
        sessions, _ = _merged_sessions()           # an all-backlog list (a fresh card's 201) needs no tmux scan
    else:
        sessions = {}
    by_row = {s["row_id"]: s for s in sessions.values() if s.get("row_id") is not None}
    gone = db.sessions_ended([t["session_row"] for t in rows if t.get("session_row") is not None and t["session_row"] not in by_row])
    chains = taskflow.chain_positions(db, rows) if any(t.get("chain_id") for t in rows) else {}
    queued = any((t.get("phase") or "running") == "queued" for t in rows)
    gates = {a: taskflow.limit_gate(db, agent=a) for a in ("claude", "codex")} if queued else {}      # each agent waits on its own window
    by_id = {t["id"]: t for t in rows}
    out = []
    for t in rows:
        if t.get("session_row") is not None:
            s = by_row.get(t["session_row"])          # authoritative: tmux names are reused, session rows are not
        else:
            s = sessions.get(t["tmux_name"]) if t["tmux_name"] else None   # legacy task: bind by name, and remember it
            if s is not None and s.get("row_id") is not None and s.get("launcher") == "task":   # a reused name on a non-task session is not this task's
                try:
                    db.task_update(t["id"], session_row=s["row_id"])
                except Exception as e:                # a display path: never fail the poll over a bookkeeping write
                    log.debug("could not bind task %s to session row %s: %s", t["id"], s["row_id"], e)
        try:
            ci = json.loads(t["ci"]) if t.get("ci") else None
            prj = json.loads(t["pr_json"]) if t.get("pr_json") else None
            ovl = json.loads(t["overlap"]) if t.get("overlap") else []
        except ValueError:
            ci, prj, ovl = None, None, []
        flags = (s or {}).get("flags") or {}
        ac = flags.get("autoclose") if isinstance(flags.get("autoclose"), dict) and flags["autoclose"].get("task") == t["id"] else None
        end = gone.get(t.get("session_row")) or {}
        held = None                                  # a chain step whose parent is done but the limit window holds auto-dispatch
        gate = gates.get("codex" if t.get("agent") == "codex" else "claude")
        if gate and (t.get("phase") or "running") == "queued" and t.get("parent_id") is not None:
            parent = by_id.get(t["parent_id"]) or db.task_get(t["parent_id"])
            held = gate if (parent or {}).get("phase") == "done" else None
        column = tasks.derive_status(t, s)
        if column == "needs_you" and (t.get("phase") or "running") == "done" and ac and not ac.get("held") and flags.get("wait_kind") == "idle":
            column = "done"                          # Claude's idle-prompt Notification a minute after the Stop is not a call for you while the close is planned
        if column == "done" and ac and ac.get("held") == "question":
            column = "needs_you"                     # a final message that asks something: the session stays, the card wants an answer
        out.append({
            "ci": ci, "pr": prj,
            "id": t["id"], "project": t["project"], "repo": t["repo"], "slug": t["slug"], "title": t["title"],
            "branch": t["branch"], "base": t["base"], "worktree": t["worktree"], "tmux": t["tmux_name"],
            "claude_session_id": t["claude_session_id"], "pr_url": t["pr_url"], "pr_number": t["pr_number"],
            "pr_state": t["pr_state"], "cost_usd": t["cost_usd"], "overlap": ovl,
            "preview_port": t.get("preview_port"), "preview_https": t.get("preview_https"),
            "preview_url": f"https://{previews.public_host()}:{t['preview_https']}/" if t.get("preview_https") and previews.public_host() else None,
            "created_at": t["created_at"], "column": column,
            "agent": t.get("agent") or "claude", "mode": t.get("mode") or "worktree", "phase": t.get("phase") or "running",
            "auto_close": bool(t.get("auto_close")), "parent_id": t.get("parent_id"), "chain_id": t.get("chain_id"),
            "session_row": t.get("session_row"), "result": (t.get("result") or "")[:300] or None,
            "result_at": t.get("result_at"), "done_at": t.get("done_at"),
            "closed_at": end.get("ended_at") if end.get("ended_reason") == "auto_close" else None,
            "autoclose": ac, "chain": chains.get(t["id"]),
            "issue_number": t.get("issue_number"), "issue_url": t.get("issue_url"),
            "issue_commented_at": t.get("issue_commented_at"),
            "limit_hold": held,
            "turns_ahead": taskflow.turns_ahead(t),
            "prompt": (t.get("prompt") or "")[:TASK_PROMPT_HEAD] if (t.get("phase") or "running") in ("backlog", "queued") else None,
            "prompt_len": len(t.get("prompt") or ""),
            "session": {"state": s["state"], "state_at": s["state_at"], "last_message": s["last_message"],
                        "needs_attention": s["needs_attention"], "command": s["command"]} if s else None,
        })
    return out



class LaunchOpts(BaseModel):
    """The launch choices the board offers as proper controls (mirrors the claude CLI flags)."""
    model: str | None = None                 # --model alias or full id
    effort: str | None = None                # --effort low|medium|high|xhigh|max
    permission_mode: str | None = None       # --permission-mode (bypassPermissions only inside a devcontainer)
    allowed_tools: str | None = None         # --allowedTools, comma/newline separated patterns
    disallowed_tools: str | None = None      # --disallowedTools
    append_system_prompt: str | None = None  # --append-system-prompt
    subagent_model: str | None = None        # Claude: CLAUDE_CODE_SUBAGENT_MODEL for the session (inherit | haiku | sonnet | opus | a model id); None = the board default
    subagent_force: bool | None = None       # Claude: also CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1; only ever by this explicit switch
    reasoning_effort: str | None = None      # codex: model_reasoning_effort (a Claude `effort` is read as this when a codex launch has none)
    opts: dict | None = None                 # the agent's own options, as its option_schema names them (codex: sandbox, approval, search,
                                             # profile, ...); wins over the flat fields. Claude ignores it.


def _agent_opts(agent: str, body) -> dict:
    """The raw option dict one agent's adapter validates, from a request body that carries the flat LaunchOpts fields and `opts`.
    Claude: the flat fields as always (its adapter reads only the keys it knows). Any other agent: the flat fields, with a Claude-style
    `effort` read as `reasoning_effort` when none is given, then `opts` on top. Empty values are dropped."""
    flat = {k: v for k, v in body.model_dump(include=set(LaunchOpts.model_fields)).items() if v not in (None, "")}
    own = flat.pop("opts", None)
    if agent == "claude":
        flat.pop("reasoning_effort", None)
        return flat
    if "effort" in flat:
        effort = flat.pop("effort")
        flat.setdefault("reasoning_effort", effort)
    return {**flat, **(own if isinstance(own, dict) else {})}


def _launch_args(body: LaunchOpts) -> list[str]:
    """The launch controls as claude argv (validated with the board's messages). The adapter owns the rules."""
    return agents.get("claude").launch_opt_args(body.model_dump())


class TaskIn(LaunchOpts):
    title: str
    prompt: str
    args: str | None = None
    add_dirs: list[str] | None = None
    agent: str = "claude"


TASK_TITLE_MAX = 120
TASK_PROMPT_MAX = 20000
TASK_SPEC_KEYS = (*LaunchOpts.model_fields, "args", "add_dirs")     # what a backlog task remembers to start with later
_task_lock = threading.RLock()     # one launch or dispatch at a time: a double-tapped Start must not start two sessions


def _task_text(title: str | None, prompt: str | None) -> tuple[str, str]:
    """A task's title (whitespace collapsed, cut at 120) and prompt (stripped, at most 20000), both required."""
    title = " ".join((title or "").split())[:TASK_TITLE_MAX]
    prompt = (prompt or "").strip()
    if not title or not prompt:
        raise projects.BadRequest("title and prompt are required")
    if len(prompt) > TASK_PROMPT_MAX:
        raise projects.BadRequest("prompt too long")
    return title, prompt


def _task_check(project: str, repo: str, body, *, launching: bool) -> dict:
    """Everything a task needs to be true before it is created or started, in the order the legacy route always checked it:
    the repo is a git repo (repo 'root' is the project folder, which must itself be one), title and prompt, claude installed
    (only when launching), the extra args and the bypass/override refusal, the launch controls, the add-dirs. `body` is
    anything with title, prompt, args, add_dirs and the LaunchOpts fields (TaskIn, TaskCreateIn). Raises 400/404 with the
    board's messages; returns {rpath, title, prompt, extra, opts_clean, add_dirs}."""
    rpath = projects.repo_path(project, repo)
    inplace = False
    if not projects.is_repo(rpath):
        if repo != projects.ROOT:
            raise projects.NotFound(f"repo {project}/{repo} is not a git repo")
        if not rpath.is_dir():
            raise projects.NotFound(f"project {project} not found")
        inplace = True      # the whole project folder, not a git repo: the task runs in place (mode 'attached'), no worktree or branch
    title, prompt = _task_text(body.title, body.prompt)
    agent = _task_agent(getattr(body, "agent", None))
    if launching and not (settings.claude_bin() if agent == "claude" else agents.get(agent).bin()):
        raise projects.BadRequest(f"{agent} is not installed on this box")
    extra: list[str] = []
    if body.args:
        if len(body.args) > MAX_ARGS:
            raise projects.BadRequest("extra args too long")
        try:
            extra = shlex.split(body.args)
        except ValueError as e:
            raise projects.BadRequest(f"extra args: {e}")
    refusal = ("bypassPermissions (or a settings override) is not allowed for tasks; start a session and choose bypass there "
               "if you really want it")
    if agent != "claude":
        # the adapter's own rules (Codex: -c/-p/--profile/--enable... and every bypass spelling), the board's bypass spellings, and
        # a bypass or an unsandboxed run asked for through the controls: none of them reaches a task
        raw = _agent_opts(agent, body)
        bad = (agents.get(agent).forbidden_extra(extra, interactive=True, task=True) or _override_requested(extra)
               or _bypass_requested(extra) or _task_danger(raw))
        if bad:
            raise projects.BadRequest(f"{bad}: {refusal}")
        if not raw.get("permission_mode"):
            # a task never inherits its permissions from the person's config.toml (which may say danger-full-access / approval never,
            # the way the box's own Codex threads run): without a mode it runs with the default one (workspace-write + on-request), so
            # the line always carries both -s and -a; an explicit sandbox or approval still replaces its own half of that mode
            raw["permission_mode"] = "default"
        raw["extra"] = extra
        opts_clean = agents.get(agent).validate_opts(raw, interactive=True, tasks_or_headless=True)
        return {"rpath": rpath, "title": title, "prompt": prompt, "extra": [], "opts_clean": opts_clean,
                "add_dirs": _resolve_add_dirs(body.add_dirs, rpath), "inplace": inplace, "agent": agent, "raw_opts": raw}
    bad = _override_requested(extra) or _bypass_requested(extra)
    if bad or body.permission_mode == "bypassPermissions":
        raise projects.BadRequest(f"{bad or 'bypassPermissions'}: {refusal}")
    extra = _launch_args(body) + extra
    opts_clean = agents.get("claude").validate_opts(body.model_dump(include=set(LaunchOpts.model_fields)), interactive=True,
                                                    tasks_or_headless=True)
    add_dirs = _resolve_add_dirs(body.add_dirs, rpath)
    return {"rpath": rpath, "title": title, "prompt": prompt, "extra": extra, "opts_clean": opts_clean, "add_dirs": add_dirs, "inplace": inplace,
            "agent": "claude"}


def _task_danger(raw: dict) -> str | None:
    """A task's agent options that would skip the approval prompts or the sandbox, whatever they are called: the offending
    option, else None (recover._is_bypass is the rule; tasks never run with bypass, the adapter refuses it too and this is the
    board's own check)."""
    for k, v in (raw or {}).items():
        if recover._is_bypass(k, v):
            return f"{k}={v}"
    return None


def _task_launch(project: str, repo: str, body, *, task_id: int | None = None) -> dict:
    """Start a task: a tmux session running the agent in a fresh worktree branch 'worktree-<slug>' of the repo (repo 'root' = the
    project folder). Claude makes the worktree itself (`claude --worktree`); Codex has no such flag, so ccboard runs `git worktree
    add` under .ccboard/worktrees first (see _launch_managed_task). Without task_id a new task row is inserted (phase running, mode
    worktree). With task_id that backlog row is UPDATED instead (slug from its current title, tmux_name, branch, worktree, base,
    claude_session_id, session_row, assigned_at, phase running), so an edited title names the branch. Returns {id, slug, tmux, branch,
    attach_url}."""
    require_real_launch_ok()   # issue #100: before any worktree is made
    with _task_lock:
        c = _task_check(project, repo, body, launching=True)
        agent = c["agent"]
        rpath, title, prompt = c["rpath"], c["title"], c["prompt"]
        taken = db.task_slugs(project, repo)
        if task_id is not None:
            own = db.task_get(task_id)
            if not own:
                raise projects.NotFound("no such task")
            taken = taken - {own["slug"]}             # the backlog row's own slug is not a collision
        slug = tasks.unique_slug(rpath, tasks.slugify(title), taken)
        session = tasks.session_name_for(slug)
        projects.check_name("session", session)
        name = tmux.tmux_name(project, repo, session)
        if tmux.has_session(name):
            raise projects.Conflict(f"session {session} already exists")
        inplace = bool(c.get("inplace"))
        if agent == "claude":
            if not inplace:
                tasks.ensure_excluded(rpath)
            sid: str | None = str(uuid.uuid4())
            cmd_line = (tasks.build_command_inplace(sid, prompt, c["extra"], c["add_dirs"]) if inplace
                        else tasks.build_command(slug, sid, prompt, c["extra"], c["add_dirs"]))
            real, row_id = _start_session_row(name, project, repo, session, "task", str(rpath), cmd_line=cmd_line, claude_session_id=sid,
                                              add_dirs=c["add_dirs"], agent="claude", opts=c["opts_clean"], task_id=task_id)
            wt_path = str(tasks.worktree_path(rpath, slug))
        else:
            real, row_id, sid, wt_path = _launch_managed_task(c, project, repo, session, name, slug, task_id)
        if inplace:
            branch, worktree, base, mode = "", "", "", "attached"
        else:
            branch, worktree, base, mode = f"worktree-{slug}", wt_path, tasks.default_branch(rpath), "worktree"
        if task_id is None:
            spec = {k: v for k in TASK_SPEC_KEYS if (v := getattr(body, k, None)) not in (None, "", [])}      # Reopen starts with the same choices
            if getattr(body, "auto_close", None) is False:
                spec["auto_close"] = False
            tid = db.task_add(project=project, repo=repo, slug=slug, title=title, prompt=prompt, branch=branch, base=base,
                              worktree=worktree, tmux_name=real, claude_session_id=sid, agent=agent, session_row=row_id, mode=mode,
                              assigned_at=db_now(), auto_close=1 if getattr(body, "auto_close", False) else None, spec=spec or None,
                              **_issue_link(body))
        else:
            tid = task_id
            db.task_update(tid, slug=slug, tmux_name=real, branch=branch, base=base, worktree=worktree, claude_session_id=sid,
                           session_row=row_id, agent=agent, mode=mode, phase="running", assigned_at=db_now())
        _invalidate_scan()
        return {"id": tid, "slug": slug, "tmux": real, "branch": branch, "attach_url": f"/term/{real}"}


def _launch_managed_task(c: dict, project: str, repo: str, session: str, name: str, slug: str, task_id: int | None):
    """The agent without a native worktree flag (Codex): `git worktree add -b worktree-<slug> <repo>/.ccboard/worktrees/<slug>` in the
    request thread (git's refusal is a GitError, 422), the repo's .worktreeinclude files copied in, then the adapter's launch line
    typed into a session that starts IN the worktree (so the line carries no -C). A project folder that is not a git repo runs in
    place, as Claude's does. The launch is a task launch: never a bypass. A launch that fails takes its worktree and branch with it.
    Returns (tmux name, sessions.id, agent session id | None, worktree path)."""
    agent, ag, rpath = c["agent"], agents.get(c["agent"]), c["rpath"]
    wt: Path | None = None
    if not c.get("inplace"):
        tasks.ensure_excluded(rpath)
        try:
            wt = tasks.create_managed_worktree(rpath, slug, tasks.default_branch(rpath), agent)
        except tasks.WorktreeError as e:
            raise gitops.GitError(str(e)) from e
        tasks.apply_worktreeinclude(rpath, wt)
    cwd = str(wt or rpath)
    try:
        plan = ag.launch_plan(LaunchReq(kind="new", session_name=session, cwd=cwd, opts=c["raw_opts"], add_dirs=c["add_dirs"],
                                        prompt=c["prompt"], bypass=False, task=True))
        real, row_id = _start_session_row(name, project, repo, session, "task", cwd, cmd_line=plan.cmd_line,
                                          claude_session_id=plan.agent_session_id, add_dirs=c["add_dirs"], agent=agent,
                                          opts=plan.opts_clean or c["opts_clean"], task_id=task_id)
    except Exception:
        if wt is not None:
            tasks.discard_managed_worktree(rpath, slug, wt)
        raise
    return real, row_id, plan.agent_session_id, cwd


@app.post("/api/projects/{project}/repos/{repo}/tasks", status_code=201)
def api_create_task(project: str, repo: str, body: TaskIn):
    """The legacy create: always starts the task now (POST /api/tasks with when 'now'). Response unchanged."""
    return _task_launch(project, repo, body)


def _task_agent(agent: str | None) -> str:
    """The agent a task runs: claude (the default) or any adapter the board has (codex). Anything else is a 400."""
    if agent in (None, ""):
        return "claude"
    if isinstance(agent, str) and agent in agents.names():
        return agent
    raise projects.BadRequest(f"unknown agent {agent!r}; use {' or '.join(agents.names())}")


def _task_row(tid: int) -> dict | None:
    """The state.tasks-shaped row of one task (what a create/dispatch response carries as `task`, so the client paints the card
    at once). Never raises: it is decoration on a write that already happened."""
    try:
        t = db.task_get(tid)
        return _tasks_view([t])[0] if t else None
    except Exception as e:
        log.debug("could not build the view row of task %s: %s", tid, e)
        return None


def _task_spec(t: dict) -> dict:
    try:
        spec = json.loads(t["spec"]) if t.get("spec") else {}
    except ValueError:
        spec = {}
    return spec if isinstance(spec, dict) else {}


class TaskCreateIn(LaunchOpts):
    project: str = ""
    repo: str = ""                       # a repo of the project, or 'root' for the project folder (a git repo)
    title: str = ""
    prompt: str = ""
    when: str | None = None              # now = start a session at once; later = a backlog card; unset: now, or later with after_task_id
    agent: str | None = None             # claude (the default) or codex; a task queued behind another inherits its agent
    args: str | None = None
    add_dirs: list[str] | None = None
    auto_close: bool | None = None       # close the session when the task's turn ends; unset = off here (a lane dispatch later defaults to on)
    after_task_id: int | None = None     # queue behind that task: it starts in a lane of its own when that one is done (a chain step)
    issue_number: int | None = None      # the GitHub issue the task was made from (the card links to it, the result can be posted there)
    issue_url: str | None = None


def _issue_link(body) -> dict:
    """{issue_number, issue_url} to store on a task (empty when none): a positive integer, and an https URL without whitespace."""
    n, url = getattr(body, "issue_number", None), (getattr(body, "issue_url", None) or "").strip()
    if n is None:
        return {}
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise projects.BadRequest("issue_number must be a positive integer")
    if url and not re.fullmatch(r"https://\S{1,300}", url):
        raise projects.BadRequest("issue_url must be an https link")
    return {"issue_number": n, "issue_url": url or None}


@app.post("/api/tasks", status_code=201)
def api_tasks_create(body: TaskCreateIn):
    """Create a task from anywhere. when 'now' starts it (like the legacy route) and answers {id, slug, tmux, branch, attach_url,
    phase 'running', session_row, task} (plus limit_warning when the Claude window is nearly full: a hand start is never held, only
    warned); when 'later' adds a backlog card with the launch choices kept in `spec` and answers {id, slug, phase 'backlog', tmux null,
    task}. With after_task_id the card is a chain step instead: phase 'queued', parent_id = that task, chain_id shared with it (made
    when it had none), agent inherited unless named; it needs when 'later' (or none). `task` is the state.tasks row."""
    project, repo = body.project.strip(), body.repo.strip()
    if not project or not repo:
        raise projects.BadRequest("project and repo are required")
    agent = _task_agent(body.agent)
    when = body.when if body.when is not None else ("later" if body.after_task_id is not None else "now")
    if when not in ("now", "later"):
        raise projects.BadRequest("when must be 'now' or 'later'")
    if body.after_task_id is not None and when != "later":
        raise projects.BadRequest("a task queued behind another cannot start now")
    if when == "now":
        out = _task_launch(project, repo, body)
        t = db.task_get(out["id"]) or {}
        gate = taskflow.limit_gate(db, agent=agent)
        return {**out, "phase": "running", "session_row": t.get("session_row"), "task": _task_row(out["id"]),
                **({"limit_warning": gate} if gate else {})}
    with _task_lock:
        parent = None
        if body.after_task_id is not None:
            parent = db.task_get(body.after_task_id)
            if not parent:
                raise projects.NotFound("no such task to queue behind")
            if not body.agent:
                agent = _task_agent(parent.get("agent"))
        c = _task_check(project, repo, body.model_copy(update={"agent": agent}), launching=False)    # a card for a repo that cannot run it, or with bypass, is refused now
        slug = tasks.unique_slug(c["rpath"], tasks.slugify(c["title"]), db.task_slugs(project, repo))
        spec = {k: v for k in TASK_SPEC_KEYS if (v := getattr(body, k, None)) not in (None, "", [])}
        if body.auto_close is False:
            spec["auto_close"] = False   # an explicit 'off' that a later lane dispatch's default (on) must not override
        chain_id = None
        if parent:
            chain_id = parent.get("chain_id") or uuid.uuid4().hex[:12]
            if not parent.get("chain_id"):
                db.task_update(parent["id"], chain_id=chain_id)
        tid = db.task_add(project=project, repo=repo, slug=slug, title=c["title"], prompt=c["prompt"], tmux_name="", worktree="",
                          branch="", base="" if c.get("inplace") else tasks.default_branch(c["rpath"]), agent=agent,
                          mode="attached" if c.get("inplace") else "worktree", phase="queued" if parent else "backlog",
                          auto_close=1 if body.auto_close else None, spec=spec, parent_id=parent["id"] if parent else None,
                          chain_id=chain_id, **_issue_link(body))
    _invalidate_scan()
    return {"id": tid, "slug": slug, "phase": "queued" if parent else "backlog", "tmux": None, "task": _task_row(tid)}


@app.get("/api/tasks")
def api_tasks_list(project: str | None = None, repo: str | None = None, phase: str | None = None):
    """The state.tasks rows (unarchived), filtered: ?project=&repo=&phase= (phase may be a comma list, e.g. backlog,queued)."""
    want = [p.strip() for p in (phase or "").split(",") if p.strip()]
    bad = [p for p in want if p not in tasks.PHASES]
    if bad:
        raise projects.BadRequest(f"phase must be one of {', '.join(tasks.PHASES)}")
    rows = [t for t in db.tasks() if (not project or t["project"] == project) and (not repo or t["repo"] == repo)
            and (not want or (t.get("phase") or "running") in want)]
    return {"tasks": _tasks_view(rows)}


@app.get("/api/tasks/{tid}")
def api_task_get(tid: int):
    """One task with what the poll leaves out: the full prompt, the full result and the parsed spec (the launch choices a
    backlog card will start with)."""
    t = db.task_get(tid)
    if not t:
        raise projects.NotFound("no such task")
    row = _task_row(tid) or {}
    return {**row, "prompt": t["prompt"], "result": t.get("result"), "spec": _task_spec(t)}


class TaskPatchIn(BaseModel):
    title: str | None = None
    prompt: str | None = None


@app.patch("/api/tasks/{tid}")
def api_task_patch(tid: int, body: TaskPatchIn):
    """Edit a backlog or queued task's title and/or prompt (the branch name is taken from the title when it starts)."""
    with _task_lock:
        t = db.task_get(tid)
        if not t:
            raise projects.NotFound("no such task")
        if (t.get("phase") or "running") not in ("backlog", "queued"):
            raise projects.Conflict("only a backlog task can be edited")
        if body.title is None and body.prompt is None:
            raise projects.BadRequest("title or prompt is required")
        title, prompt = _task_text(t["title"] if body.title is None else body.title, t["prompt"] if body.prompt is None else body.prompt)
        db.task_update(tid, title=title, prompt=prompt)
    _invalidate_scan()
    return {"id": tid, "title": title, "prompt": prompt, "task": _task_row(tid)}


@app.delete("/api/tasks/{tid}", status_code=204)
def api_task_delete(tid: int):
    """Delete a backlog, queued or cancelled-without-a-worktree task. A started task is archived instead."""
    with _task_lock:
        t = db.task_get(tid)
        if not t:
            raise projects.NotFound("no such task")
        phase = t.get("phase") or "running"
        if phase not in ("backlog", "queued") and not (phase == "cancelled" and not tasks.has_worktree(t)):
            raise projects.Conflict("archive a started task instead")
        db.task_delete(tid)
    _invalidate_scan()
    return Response(status_code=204)


class DispatchIn(LaunchOpts):
    mode: str | None = None              # 'lane' (a new session in its own worktree; the default) or 'session'
    session: str | None = None           # hand the prompt to this running session instead (implies mode 'session')
    force: bool = False                  # session whose project/repo differ from the task's: send anyway, naming the repo
    args: str | None = None              # lane only: overrides of what the card remembered
    add_dirs: list[str] | None = None
    agent: str | None = None
    auto_close: bool | None = None       # close the session when this task's turn ends; unset: the card's own switch, else on for a lane, off for a session
    queue: bool = False                  # session only: also accept a working Claude session (what is typed during a turn is queued by the TUI)


def _effective_auto_close(t: dict, asked: bool | None, *, lane: bool) -> bool:
    """Whether a dispatch closes the session at the end of the task's turn: what the request says, else on when the card's own switch is
    on, else off when the card was created with the switch explicitly off (spec.auto_close false), else the default: on for a new lane
    session, off for a session the person already had open."""
    if asked is not None:
        return bool(asked)
    if t.get("auto_close"):
        return True
    if _task_spec(t).get("auto_close") is False:
        return False
    return lane


def _conflict_body(error: str, **extra) -> JSONResponse:
    return JSONResponse({"error": error, **extra}, status_code=409)


def _clean_paste(text: str) -> str:
    """The prompt as one bracketed paste: CRLF normalised and every control character except tab and newline dropped (an ESC
    in a copied log could otherwise end the paste early and type the rest as keys)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c for c in text if c in "\t\n" or ord(c) >= 32)


def _dispatch_lane(t: dict, body: DispatchIn, prompt: str | None = None):
    """Start the backlog (or queued) task in a new session and worktree, with what it remembered (`spec`) plus the body's overrides.
    `prompt` replaces the stored one for this launch (a chain step: the template with its parent's result in it) and is stored on the
    row once the session is up. auto_close follows _effective_auto_close (on by default for a lane)."""
    merged = {k: v for k, v in _task_spec(t).items() if k in TASK_SPEC_KEYS}
    merged.update({k: v for k in TASK_SPEC_KEYS if (v := getattr(body, k, None)) is not None})
    try:
        launch = TaskIn(title=t["title"], prompt=t["prompt"] if prompt is None else prompt, agent=_task_agent(body.agent or t.get("agent")), **merged)
    except ValueError as e:
        raise projects.BadRequest(f"the saved launch options are invalid: {e}")
    out = _task_launch(t["project"], t["repo"], launch, task_id=t["id"])
    db.task_update(t["id"], auto_close=int(_effective_auto_close(t, body.auto_close, lane=True)),
                   **({"prompt": launch.prompt} if prompt is not None else {}))
    row = db.task_get(t["id"]) or {}
    return {"id": t["id"], "phase": "running", "tmux": out["tmux"], "session_row": row.get("session_row"),
            "attach_url": out["attach_url"], "slug": out["slug"], "branch": out["branch"], "task": _task_row(t["id"])}


def _dispatch_session(t: dict, body: DispatchIn, prompt: str | None = None):
    """Hand the backlog task's prompt to a running Claude session that is ready for it: 404 unknown session, 409 {error, state}
    unless it is idle, done, or waiting at its idle prompt (a 'waiting' session whose newest event is not the idle_prompt
    Notification is in a permission prompt or a dialog and typing into it would answer it: 409 {error, state, wait_kind}; a pending
    permission row is 409 {error, state, pending_permission}), 409 {error, mismatch: {task, session}} when it works in another
    repo (unless force, which prefixes the prompt with 'Work in <the task's repo path>.')."""
    name = body.session or ""
    try:
        sproject, srepo, _ = tmux.split_name(name)
    except ValueError:
        raise projects.BadRequest("not a ccboard session name")
    if not tmux.has_session(name):
        raise projects.NotFound(f"session {name} not found")
    row = db.open_rows().get(name)
    state = (row or {}).get("state") or "unknown"
    if row is None or row.get("agent") != (t.get("agent") or "claude"):
        return _conflict_body(f"{name} is not a {t.get('agent') or 'claude'} session the board started", state=state, agent=(row or {}).get("agent"))
    queued = state == "working" and body.queue and row.get("agent") == "claude"       # Claude queues what is typed during a turn
    if state not in ("idle", "done", "waiting") and not queued:
        return _conflict_body(f"the session is {state}; wait for it to finish or pick another", state=state)
    if queued and (row.get("flags") or {}).get("compacting"):
        return _conflict_body("the session is compacting; wait for it to finish or pick another", state=state)
    if any(p["tmux_name"] == name for p in db.perm_pending()):
        return _conflict_body("the session is waiting for a permission decision; answer it first", state=state, pending_permission=True)
    if state == "waiting":
        kind = _wait_kind_of(db.last_event(name))
        if kind != "idle_prompt":
            return _conflict_body("the session is waiting on a prompt (permission or dialog): answer it first", state=state, wait_kind=kind)
    block = _pane_block(name, row)
    if block:
        return _conflict_body(block[1], state=state, pane=block[0])
    task_path = projects.repo_path(t["project"], t["repo"])
    prompt = _clean_paste(t["prompt"] if prompt is None else prompt)
    if projects.repo_path(sproject, srepo) != task_path:
        if not body.force:
            return _conflict_body("this session works in another repo", mismatch={"task": f"{t['project']}/{t['repo']}", "session": f"{sproject}/{srepo}"})
        prompt = f"Work in {task_path}.\n\n{prompt}"
    # a prompt queued behind a turn in flight has a Stop of its own AFTER the Stops of the turns ahead of it (issue #64): the runtime skips
    # that many Stops before it credits this task, and the card reads Queued until then. Claude runs the prompts it holds in its queue as
    # ONE turn, so a second board-queued prompt is not typed now: it is kept in the board (spec.unsent) and the runtime types it once the
    # prompt ahead of it has begun its turn
    ahead = taskflow.next_turns_ahead(db, row["row_id"]) if queued else 0
    held = queued and taskflow.must_hold(db, row["row_id"])
    if not held:
        tmux.paste_text(name, prompt, enter=True)
        # the UserPromptSubmit hook says the same within a second; painting it now keeps the card in In progress, not in Done
        db.set_state(name, "working", "TaskDispatch", prompt=prompt[:500])
    db.add_event(name, "TaskDispatch", t["title"], f"task {t['id']} " + ("held until the queued prompt before it has begun" if held else "sent to the session"),
                 {"task": t["id"], "force": bool(body.force), "queued": queued, "held": held}, agent=row.get("agent"))
    db.task_update(t["id"], tmux_name=name, session_row=row["row_id"], mode="session", phase="running", assigned_at=db_now(),
                   auto_close=int(_effective_auto_close(t, body.auto_close, lane=False)))
    taskflow.set_turns_ahead(db, t["id"], ahead)
    taskflow.set_unsent(db, t["id"], prompt if held else None)
    _invalidate_scan()
    return {"id": t["id"], "phase": "running", "tmux": name, "session_row": row["row_id"], "pasted": not held, "queued": queued, "held": held,
            "turns_ahead": ahead, "task": _task_row(t["id"])}


@app.post("/api/tasks/{tid}/dispatch")
def api_task_dispatch(tid: int, body: DispatchIn | None = None):
    """Start a backlog task: in a new session of its own ({mode: 'lane'}, the default; launch choices from the card's spec,
    overridable in the body) or in a running session ({session: '<tmux>', force?}). Answers {id, phase 'running', tmux,
    session_row, attach_url (lane) | pasted + queued (session), task, limit_warning when the Claude window is at 85 % or a limit
    episode is active: {kind, resets_at, pct}); the client navigates to the tmux session from this response."""
    require_real_launch_ok()   # issue #100: refuses (409) on a dev board whose directories could reach the real home
    body = body or DispatchIn()
    if body.mode not in (None, "lane", "session"):
        raise projects.BadRequest("mode must be 'lane' or 'session'")
    if body.mode == "session" and not body.session:
        raise projects.BadRequest("session is required to hand a task to a session")
    if body.mode == "lane" and body.session:
        raise projects.BadRequest("a lane dispatch starts a new session; drop session or use mode 'session'")
    _task_agent(body.agent)
    with _task_lock:                                   # re-read inside the lock: a second tap sees the first one's phase
        t = db.task_get(tid)
        if not t:
            raise projects.NotFound("no such task")
        phase = t.get("phase") or "running"
        prompt = None
        if phase == "queued":
            # a chain step is started by the runtime when its parent is done; by hand ('Start anyway', while the limit window holds it)
            # only once the parent is done too, with the parent's result in its prompt, exactly as the runtime would have sent it
            parent = db.task_get(t["parent_id"]) if t.get("parent_id") is not None else None
            if parent is None or parent.get("phase") != "done":
                raise projects.Conflict("this task is queued behind another step")
            prompt = taskflow.compose_prompt(t["prompt"], parent)
        elif phase != "backlog":
            raise projects.Conflict("already dispatched")
        res = _dispatch_session(t, body, prompt) if body.session else _dispatch_lane(t, body, prompt)
        gate = taskflow.limit_gate(db, agent=_task_agent(body.agent or t.get("agent"))) if isinstance(res, dict) else None
        if gate:                                             # a hand dispatch is never held by the window, only warned
            res["limit_warning"] = gate
        if isinstance(res, dict) and res.get("phase") == "running" and not body.session and (warn := nodes.lane_warning(db)):
            res["lane_warning"] = warn                       # more lanes than CCBOARD_NODE_LANES: advice for the toast, never a refusal
        return res


# ---------- the task runtime: hook entry, injected callables, reopen / detach / close-session / keep-open / chains (v0.5.14b)

def _taskflow() -> "taskflow.Runtime":
    """The runtime the routes talk to: the lifespan's, or (the board without its workers, as the tests run it) one made on demand for the
    current DB, without a ticker thread."""
    global taskflow_rt
    if taskflow_rt is None or taskflow_rt.db is not db:
        taskflow_rt = taskflow.Runtime(db, start_session=_taskflow_start, end_session=_end_session, perm_pending=_permission_pending)
    return taskflow_rt


def _taskflow_start(t: dict, *, prompt: str | None = None, auto_close: bool | None = None):
    """The runtime's start_session: start a queued chain step in a new lane session (None when someone else got to it first)."""
    with _task_lock:
        cur = db.task_get(t["id"])
        if not cur or (cur.get("phase") or "running") != "queued":
            return None
        return _dispatch_lane(cur, DispatchIn(mode="lane", auto_close=auto_close), prompt=prompt)


def _taskflow_turn_hook(handle, name: str, event: str, norm, row: dict | None) -> None:
    """hooks.TURN_HOOKS entry: Stop and StopFailure end the active task's turn, the events that show the session is not finished cancel a
    pending auto-close. A no-op until the runtime exists for this DB (the lifespan makes it; a route may make one on demand)."""
    rt = taskflow_rt
    if rt is None or rt.db is not handle:
        return
    if event == "Stop":
        rt.on_turn_end(name, row, getattr(norm, "result", None))
    elif event == "StopFailure":
        rt.on_turn_end(name, row, None, failed=True, message=getattr(norm, "message", None), limit=getattr(norm, "limit", None))
    else:
        rt.on_activity(name, event, row, norm)


def _register_turn_hook() -> None:
    """Append _taskflow_turn_hook to hooks.TURN_HOOKS (hooks.py owns the list); a re-import replaces the old entry instead of doubling it."""
    hooks.TURN_HOOKS[:] = [f for f in hooks.TURN_HOOKS if getattr(f, "__qualname__", "") != "_taskflow_turn_hook"] + [_taskflow_turn_hook]


_register_turn_hook()


class ReopenIn(LaunchOpts):
    prompt: str | None = None            # what to tell the new session instead of the task's own prompt
    args: str | None = None
    add_dirs: list[str] | None = None
    auto_close: bool | None = None       # unset: the card's switch, else on (a reopen is a lane dispatch)


def _task_transcript(t: dict, wt: Path, agent: str, sid: str) -> Path | None:
    """The transcript of a lane task's previous conversation, or None. Claude started with --worktree begins in the REPO ROOT, so it keeps
    the conversation under the root's project folder (<config>/projects/<root path mangled>/<id>.jsonl), not under the worktree's: the
    path its own hooks reported (flags.transcript_path of the task's old row) comes first, then the worktree's folder, then the root's.
    Issue #88: looking only at the worktree's folder never found it, so a Reopen started a fresh conversation."""
    ad = agents.get(agent)
    old = db.session_by_id(t.get("session_row"))
    given = ((old or {}).get("flags") or {}).get("transcript_path")
    if given:
        f = ad.transcript_path({"transcript_path": given})
        if f is not None and f.is_file():
            return f
    for cwd in (str(wt), str(projects.repo_path(t["project"], t["repo"]))):
        f = ad.transcript_path({"session_id": sid, "agent_session_id": sid, "cwd": cwd})
        if f is not None:
            return f
    return None


def _stage_transcript(src: Path, wt: Path) -> None:
    """A `claude --resume <id>` looks for the conversation under the project folder of the directory it starts in. The reopened session
    starts in the worktree, but the lane's conversation lives under the repo root's folder: put a copy where the resume will look
    (never over a file that is already there; the original stays, it is the record of the first run)."""
    dest = settings.claude_config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(wt)) / src.name
    if dest.exists() or dest.parent == src.parent:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def _reopen_in_worktree(t: dict, launch: TaskIn, wt: Path) -> tuple[str, int, str | None]:
    """A new session of the task's agent that starts IN its existing worktree (no --worktree, no new branch): the same launch the card
    remembers, with a line that says the work so far is already there. Returns (tmux name, sessions.id, agent session id)."""
    c = _task_check(t["project"], t["repo"], launch, launching=True)
    agent, project, repo = c["agent"], t["project"], t["repo"]
    session = tasks.session_name_for(t["slug"])
    name = tmux.tmux_name(project, repo, session)
    if tmux.has_session(name):
        raise projects.Conflict(f"session {session} already exists")
    prompt = ("This task is being reopened: its previous session stopped, and the work so far is already in this worktree "
              "(git status and git log show it). Continue the task.\n\n" + c["prompt"])
    # #88: resume the previous conversation when its transcript is still on disk, so the reopened session has its history; else a new
    # conversation in the same worktree (the line above tells it where the work is)
    old_sid = t.get("claude_session_id")
    resumable = False
    if isinstance(old_sid, str) and old_sid:
        try:
            found = _task_transcript(t, wt, agent, old_sid)
            if found is not None and agent == "claude":
                _stage_transcript(found, wt)
            resumable = found is not None
        except Exception:      # a lookup problem only means a fresh conversation
            resumable = False
    if resumable:
        # a resume takes no first prompt: the session comes back at its prompt and autoresume types `continue` once it is up
        plan = agents.get(agent).launch_plan(LaunchReq(kind="resume", resume_id=old_sid, session_name=session, cwd=str(wt), opts=c.get("raw_opts") or c.get("opts_clean") or {},
                                                       add_dirs=c["add_dirs"], bypass=False, task=True))
        sid = plan.agent_session_id or old_sid
        real, row_id = _start_session_row(name, project, repo, session, "task", str(wt), cmd_line=plan.cmd_line, claude_session_id=sid,
                                          add_dirs=c["add_dirs"], agent=agent, opts=plan.opts_clean or c["opts_clean"], task_id=t["id"])
        db.task_update(t["id"], tmux_name=real, claude_session_id=sid, session_row=row_id, phase="running", assigned_at=db_now())
        db.update_flags(real, {recover.CONTINUE_FLAG: {"reason": "reopen", "at": time.time(), "task": t["id"]}})
        return real, row_id, sid
    if agent == "claude":
        sid: str | None = str(uuid.uuid4())
        real, row_id = _start_session_row(name, project, repo, session, "task", str(wt),
                                          cmd_line=tasks.build_command_inplace(sid, prompt, c["extra"], c["add_dirs"]),
                                          claude_session_id=sid, add_dirs=c["add_dirs"], agent="claude", opts=c["opts_clean"], task_id=t["id"])
    else:
        plan = agents.get(agent).launch_plan(LaunchReq(kind="new", session_name=session, cwd=str(wt), opts=c["raw_opts"],
                                                       add_dirs=c["add_dirs"], prompt=prompt, bypass=False, task=True))
        sid = plan.agent_session_id
        real, row_id = _start_session_row(name, project, repo, session, "task", str(wt), cmd_line=plan.cmd_line, claude_session_id=sid,
                                          add_dirs=c["add_dirs"], agent=agent, opts=plan.opts_clean or c["opts_clean"], task_id=t["id"])
    db.task_update(t["id"], tmux_name=real, claude_session_id=sid, session_row=row_id, phase="running", assigned_at=db_now())
    return real, row_id, sid


@app.post("/api/tasks/{tid}/reopen")
def api_task_reopen(tid: int, body: ReopenIn | None = None):
    """Run a done, failed or cancelled task again: a new lane session of the same agent with the card's launch choices, in the task's
    existing worktree when it is still on disk (the work so far is there), else in a fresh one. 409 while its old session is still open
    (open the terminal, or close it first). Answers {id, phase 'running', tmux, session_row, attach_url, slug, branch, reopened:
    'worktree' | 'fresh', task, limit_warning?}. The old result is cleared; auto_close follows the request, else the card, else on."""
    require_real_launch_ok()   # issue #100: refuses (409) on a dev board whose directories could reach the real home
    body = body or ReopenIn()
    with _task_lock:
        t = db.task_get(tid)
        if not t:
            raise projects.NotFound("no such task")
        if t.get("archived_at") or t.get("status") == "merged" or t.get("pr_state") == "MERGED":
            raise projects.Conflict("this task is archived or merged; start a new one")
        if (t.get("phase") or "running") not in ("done", "failed", "cancelled"):
            raise projects.Conflict("only a finished task can be reopened")
        old = t.get("tmux_name")
        if old and tmux.has_session(old):
            return _conflict_body("the session is still open; open it, or close it first", tmux=old)
        merged = {k: v for k, v in _task_spec(t).items() if k in TASK_SPEC_KEYS}
        merged.update({k: v for k in TASK_SPEC_KEYS if (v := getattr(body, k, None)) is not None})
        try:
            launch = TaskIn(title=t["title"], prompt=body.prompt or t["prompt"], agent=_task_agent(t.get("agent")), **merged)
        except ValueError as e:
            raise projects.BadRequest(f"the saved launch options are invalid: {e}")
        wt = tasks.task_worktree(t)
        in_worktree = wt is not None and wt.is_dir() and (t.get("mode") or "worktree") == "worktree"
        resumed = False
        if in_worktree:
            real, row_id, _sid = _reopen_in_worktree(t, launch, wt)
            resumed = bool(_sid) and _sid == t.get("claude_session_id")      # #88: the previous conversation was resumed
            slug, branch = t["slug"], t["branch"]
        else:
            out = _task_launch(t["project"], t["repo"], launch, task_id=tid)
            real, slug, branch = out["tmux"], out["slug"], out["branch"]
            row_id = (db.task_get(tid) or {}).get("session_row")
        db.task_update(tid, result=None, result_at=None, done_at=None,
                       auto_close=int(_effective_auto_close(t, body.auto_close, lane=True)))
        taskflow.set_turns_ahead(db, tid, 0)
        taskflow.set_unsent(db, tid, None)
    _invalidate_scan()
    gate = taskflow.limit_gate(db, agent=_task_agent(t.get("agent")))
    return {"id": tid, "phase": "running", "tmux": real, "session_row": row_id, "attach_url": f"/term/{real}", "slug": slug,
            "branch": branch, "reopened": "worktree" if in_worktree else "fresh", "resumed": resumed, "task": _task_row(tid),
            **({"limit_warning": gate} if gate else {})}


@app.post("/api/tasks/{tid}/detach")
def api_task_detach(tid: int):
    """Put a task that was handed to a running session back in the Backlog: unbind it from the session (which keeps running, the task is
    just no longer its work), phase backlog, result and times cleared. A task that owns a worktree (or a PR) is archived instead."""
    with _task_lock:
        t = db.task_get(tid)
        if not t:
            raise projects.NotFound("no such task")
        if (t.get("phase") or "running") in ("backlog", "queued"):
            raise projects.Conflict("this task is not assigned to a session")
        if t.get("archived_at"):
            raise projects.Conflict("this task is archived")
        if tasks.has_worktree(t) or t.get("pr_url"):
            raise projects.Conflict("this task has its own worktree; archive it instead of detaching it")
        name = _taskflow()._open_name(t)
        if name:
            _taskflow().cancel_close(name)
        rpath = projects.repo_path(t["project"], t["repo"])
        mode = "attached" if t["repo"] == projects.ROOT and not projects.is_repo(rpath) else "worktree"
        taskflow.forget_queue(db, t)                  # its counter and held prompt go; the tasks queued behind it wait for one Stop less
        db.task_update(tid, tmux_name="", session_row=None, claude_session_id=None, phase="backlog", mode=mode, assigned_at=None,
                       done_at=None, result=None, result_at=None)
    _invalidate_scan()
    return {"id": tid, "phase": "backlog", "task": _task_row(tid)}


@app.post("/api/tasks/{tid}/close-session")
def api_task_close_session(tid: int):
    """Close the task's session gracefully now: the agent's own exit command is typed, and the session is killed 8 s later (row ended with
    reason auto_close, which is what the card's 'closed' reads). 409 when the task has no open session or the session is busy (working,
    compacting, a permission or dialog waiting): the same refusals as typing into it. Answers {closing, tmux, kill_at, task}."""
    t = db.task_get(tid)
    if not t:
        raise projects.NotFound("no such task")
    rt = _taskflow()
    name = rt._open_name(t)
    row = db.open_row(name) if name else None
    if not name or row is None or not tmux.has_session(name):
        raise projects.Conflict("this task has no open session")
    refusal = _typing_refusal(name, row)
    if refusal:
        return refusal
    info = rt.close_now(tid)
    if info is None:
        raise projects.Conflict("this task has no open session")
    _invalidate_scan()
    return {"closing": True, "tmux": name, "kill_at": info["kill_at"], "task": _task_row(tid)}


@app.post("/api/tasks/{tid}/keep-open")
def api_task_keep_open(tid: int):
    """'Keep open': cancel the auto-close countdown (or the question hold) of the task's session and switch the task's auto_close off, so
    the session stays until someone closes it. Idempotent. Answers {ok, kept (a countdown was running), held, task}."""
    if not db.task_get(tid):
        raise projects.NotFound("no such task")
    out = _taskflow().keep_open(tid)
    _invalidate_scan()
    return {"ok": True, **out, "task": _task_row(tid)}


CHAIN_MAX_STEPS = 10


class ChainStepIn(LaunchOpts):
    title: str = ""
    prompt: str = ""                     # {{result}} is replaced by the previous step's result; without it the result is appended
    agent: str | None = None             # unset: the previous step's agent (the first: claude); a chain may cross agents
    mode: str | None = None              # worktree (the default; a project folder that is not a git repo runs in place)
    args: str | None = None
    add_dirs: list[str] | None = None


class ChainIn(BaseModel):
    steps: list[ChainStepIn] = []
    auto_close: bool | None = None       # close each step's session when its turn ends; unset = on
    dispatch: bool = False               # start step 1 at once (a hand dispatch: the limit window only warns); else it is a Backlog card


@app.post("/api/projects/{project}/repos/{repo}/chains", status_code=201)
def api_create_chain(project: str, repo: str, body: ChainIn):
    """Create a chain of tasks in one repo: step 1 is a Backlog card (or started now with dispatch), every later step is `queued` behind
    the one before it (parent_id) with its launch choices in `spec`; the runtime starts each in a new lane session once its parent is
    done and hands it the parent's result. Answers {chain_id, ids, tasks, started?, limit_warning?}; nothing is created when any step
    is invalid."""
    if body.dispatch:
        require_real_launch_ok()   # issue #100: step 1 starts a session now
    steps = body.steps
    if not steps:
        raise projects.BadRequest("a chain needs at least one step")
    if len(steps) > CHAIN_MAX_STEPS:
        raise projects.BadRequest(f"a chain has at most {CHAIN_MAX_STEPS} steps")
    auto = True if body.auto_close is None else bool(body.auto_close)
    with _task_lock:
        checked, prev_agent = [], None
        taken: set[str] | None = None
        for i, step in enumerate(steps):
            if step.mode not in (None, "worktree", "lane", "attached"):
                raise projects.BadRequest(f"step {i + 1}: mode must be 'worktree'")
            agent = _task_agent(step.agent) if step.agent else (prev_agent or "claude")
            try:
                c = _task_check(project, repo, step.model_copy(update={"agent": agent}), launching=bool(body.dispatch and i == 0))
            except projects.BadRequest as e:
                raise projects.BadRequest(f"step {i + 1}: {e}")
            taken = db.task_slugs(project, repo) if taken is None else taken
            slug = tasks.unique_slug(c["rpath"], tasks.slugify(c["title"]), taken)
            taken.add(slug)
            checked.append((step, c, slug, agent))
            prev_agent = agent
        chain_id = uuid.uuid4().hex[:12]
        ids: list[int] = []
        for i, (step, c, slug, agent) in enumerate(checked):
            spec = {k: v for k in TASK_SPEC_KEYS if (v := getattr(step, k, None)) not in (None, "", [])}
            if not auto:
                spec["auto_close"] = False
            ids.append(db.task_add(project=project, repo=repo, slug=slug, title=c["title"], prompt=c["prompt"], tmux_name="", worktree="",
                                   branch="", base="" if c.get("inplace") else tasks.default_branch(c["rpath"]), agent=agent,
                                   mode="attached" if c.get("inplace") else "worktree", phase="backlog" if i == 0 else "queued",
                                   auto_close=1 if auto else None, spec=spec, parent_id=ids[-1] if ids else None, chain_id=chain_id))
        started = _dispatch_lane(db.task_get(ids[0]), DispatchIn(mode="lane", auto_close=auto)) if body.dispatch else None
    _invalidate_scan()
    gate = taskflow.limit_gate(db, agent=checked[0][3]) if started else None
    return {"chain_id": chain_id, "ids": ids, "tasks": _tasks_view([t for t in (db.task_get(i) for i in ids) if t]), "started": started,
            **({"limit_warning": gate} if gate else {})}


def _task_or_404(tid: int) -> tuple[dict, Path | None]:
    """The task row and its worktree path, which is None for a task that has none (backlog, queued, session mode):
    Path('') is Path('.'), so nothing may build a path from t['worktree'] without tasks.has_worktree."""
    t = db.task_get(tid)
    if not t:
        raise projects.NotFound("no such task")
    return t, tasks.task_worktree(t)


def _task_in_worktree(tid: int) -> tuple[dict, Path]:
    """_task_or_404 for the endpoints that run git in the worktree: a task without one answers 409."""
    t, wt = _task_or_404(tid)
    if wt is None:
        raise projects.Conflict("this task has no worktree yet")
    return t, wt


@app.get("/api/tasks/{tid}/diff")
def api_task_diff(tid: int):
    t, wt = _task_in_worktree(tid)
    return {"task": tid, **gitops.task_diff(wt, t["base"] or "main")}


@app.post("/api/tasks/{tid}/describe")
def api_task_describe(tid: int):
    t, wt = _task_in_worktree(tid)
    return gitops.describe(wt, t["base"] or "main", t["title"], t["prompt"])


class PrIn(BaseModel):
    title: str
    body: str = ""
    draft: bool = False


@app.post("/api/tasks/{tid}/pr")
def api_task_pr(tid: int, body: PrIn):
    t, wt = _task_in_worktree(tid)
    if not body.title.strip():
        raise projects.BadRequest("title is required")
    r = gitops.pr_create(wt, t["branch"], t["base"] or "main", body.title.strip(), body.body, body.draft)
    db.task_update(tid, pr_url=r["url"], pr_number=r["number"], pr_state="OPEN", status="pr")
    _invalidate_scan()
    return r


class MergeIn(BaseModel):
    method: str = "squash"
    force: bool = False


@app.post("/api/tasks/{tid}/merge")
def api_task_merge(tid: int, body: MergeIn | None = None):
    t, wt = _task_in_worktree(tid)
    if not t.get("pr_number"):
        raise projects.BadRequest("no PR for this task yet")
    body = body or MergeIn()
    rpath = projects.repo_path(t["project"], t["repo"])
    if wt.is_dir():
        d = gitops.task_diff(wt, t["base"] or "main")
        if (d["uncommitted"] or d["files_uncommitted"]) and not body.force:
            raise projects.Conflict("the worktree has uncommitted changes; merge with force to discard them")
        # Nothing is destroyed before origin provably holds the local HEAD.
        gitops.push_and_verify(wt, t["branch"])
    # Merge on GitHub first; only then kill the session and remove the worktree (--delete-branch cannot
    # delete a branch that is still checked out, so the merge runs from the repo dir after removal).
    _drop_preview(t)
    if t["tmux_name"]:
        _end_session(t["tmux_name"], "killed")
    err = tasks.remove_worktree(rpath, t["slug"], force=True, agent=t.get("agent"), path=wt) if rpath.is_dir() else None
    try:
        out = gitops.pr_merge(rpath if rpath.is_dir() else wt, int(t["pr_number"]), body.method)
    except gitops.GitError:
        # The branch is intact on origin and locally; the task stays in the PR column for a retry.
        _invalidate_scan()
        raise
    db.task_update(tid, pr_state="MERGED", status="merged", archived_at=db_now())
    _invalidate_scan()
    return {"merged": tid, "output": out, "worktree_removed": err is None}


@app.post("/api/tasks/{tid}/fix-ci")
def api_task_fix_ci(tid: int):
    """Fetch the failing CI logs and hand them to the task's Claude session (relaunched if gone)."""
    require_real_launch_ok()   # issue #100: refuses (409) on a dev board whose directories could reach the real home
    t, wt = _task_in_worktree(tid)
    rpath = projects.repo_path(t["project"], t["repo"])
    cwd = wt if wt.is_dir() else rpath
    run_name, log_text = prpoll.failed_log(cwd, t["branch"])
    prompt = prpoll.fix_ci_prompt(t["branch"], run_name, log_text)
    name = t["tmux_name"]
    relaunched = False
    if not tmux.has_session(name):
        if not wt.is_dir():
            raise projects.Conflict("the worktree is gone; archive this task and start a new one")
        _, _, session = tmux.split_name(name)
        ag = agents.get(t.get("agent") or "claude")
        _start_session(name, t["project"], t["repo"], session, "task", str(wt), cmd_line=shlex.join(ag.continue_argv(str(wt))),
                       claude_session_id=t.get("claude_session_id"), add_dirs=[], agent=ag.name, task_id=t["id"])
        relaunched = True
        time.sleep(4)  # let claude come up before pasting
    block = _pane_block(name, db.open_row(name), shell=not relaunched)
    if block:
        raise projects.Conflict(block[1])
    tmux.paste_text(name, prompt, enter=True)
    db.add_event(name, "FixCI", run_name, f"CI logs sent ({len(log_text)} chars)", {"run": run_name}, agent=t.get("agent"))
    db.set_state(name, "working", "FixCI", prompt=prompt[:500])
    _invalidate_scan()
    return {"ok": True, "relaunched": relaunched, "run": run_name, "chars": len(log_text)}


@app.post("/api/tasks/{tid}/refresh")
def api_task_refresh(tid: int):
    t, wt = _task_or_404(tid)
    if not t.get("pr_number"):
        raise projects.BadRequest("no PR for this task")
    cwd = wt if wt is not None and wt.is_dir() else projects.repo_path(t["project"], t["repo"])
    st = prpoll.pr_status(cwd, int(t["pr_number"]))
    db.task_update(tid, pr_state=st["state"], pr_json=json.dumps(st), ci=json.dumps(st["ci"]),
                   **({"status": "merged"} if st["state"] == "MERGED" else {}))
    _invalidate_scan()
    return st


@app.get("/api/projects/{project}/repos/{repo}/issues")
def api_issues(project: str, repo: str):
    rpath = projects.repo_path(project, repo)
    if not projects.is_repo(rpath):
        raise projects.NotFound("not a git repo")
    return {"issues": prpoll.list_issues(rpath)}


@app.get("/api/projects/{project}/repos/{repo}/issues/{n:int}")
def api_issue(project: str, repo: str, n: int):
    """One issue (gh issue view) with `author`, `trusted` (the author is the owner of the repo's origin) and the parsed `who`
    block of its "Who should do it" section: empty (claude, codex null, no warnings) for an author who is not trusted."""
    rpath = projects.repo_path(project, repo)
    if not projects.is_repo(rpath):
        raise projects.NotFound("not a git repo")
    if n < 1:
        raise projects.BadRequest("issue number must be positive")
    return prpoll.view_issue(rpath, n)


_issue_comment_lock = threading.Lock()


def _issue_comment_ctx(tid: int) -> tuple[dict, Path, str]:
    t = db.task_get(tid)
    if not t:
        raise projects.NotFound("no such task")
    if not t.get("issue_number"):
        raise projects.Conflict("this task has no GitHub issue")
    if (t.get("phase") or "running") != "done":
        raise projects.Conflict("the task is not done yet")
    if t.get("issue_commented_at"):
        raise projects.Conflict("the result was already posted on the issue")
    wt = tasks.task_worktree(t)
    cwd = wt if wt is not None and wt.is_dir() else projects.repo_path(t["project"], t["repo"])
    pr_url = t.get("pr_url")
    return t, cwd, issues_mod.comment_body(t, pr_url)


@app.get("/api/tasks/{tid}/issue-comment")
def api_task_issue_comment_preview(tid: int):
    """The comment body a POST would post (nothing is sent)."""
    t, _, body = _issue_comment_ctx(tid)
    return {"issue_number": t["issue_number"], "issue_url": t.get("issue_url"), "body": body}


@app.post("/api/tasks/{tid}/issue-comment")
def api_task_issue_comment(tid: int):
    """Post the task's result on its issue with gh (body on stdin). 409 when the task has no issue, is not done, or already posted;
    a gh failure is a 422 with its message and the task stays postable. Never replayed on its own."""
    with _issue_comment_lock:
        t, cwd, body = _issue_comment_ctx(tid)
        prpoll.comment_issue(cwd, int(t["issue_number"]), body)
        db.task_update(tid, issue_commented_at=db_now())
    _invalidate_scan()
    return {"ok": True, "issue_number": t["issue_number"], "body": body, "task": _task_row(tid)}


@app.get("/api/projects/{project}/repos/{repo}/tree")
def api_tree(project: str, repo: str, request: Request, path: str = "", hidden: str = "0", ignored: str = "0",
             repos: str = "1", refresh: str = "0"):
    """One level of the repo's tree (repo 'root' = the project folder). Weak ETag; a matching If-None-Match gets a bare 304."""
    payload = tree.list_dir(project, repo, path, hidden=tree.flag(hidden), ignored=tree.flag(ignored),
                            repos=tree.flag(repos), refresh=tree.flag(refresh))
    headers = {"ETag": tree.etag_header(payload), "Cache-Control": "private, no-cache"}
    if "hint" not in payload and tree.not_modified(request.headers.get("if-none-match"), payload["etag"]):    # a hint is given once an hour: never lose it to a 304
        return Response(status_code=304, headers=headers)
    return JSONResponse(payload, headers=headers)


@app.get("/api/projects/{project}/repos/{repo}/file")
def api_tree_file(project: str, repo: str, path: str = "", reveal: str = "0"):
    """Read-only preview text: 200 KB cap, 415 binary, 403 for secret-looking names unless reveal=1, 400 symlinks, 404."""
    try:
        body = tree.read_file(project, repo, path, reveal=tree.flag(reveal))
    except tree.Unsupported as e:
        return JSONResponse({"error": str(e)}, status_code=415)
    return JSONResponse(body, headers={"Cache-Control": "private, no-store"})


@app.get("/api/tasks/{tid}/ports")
def api_task_ports(tid: int):
    t, _ = _task_or_404(tid)
    try:
        s = tmux.list_sessions().get(t["tmux_name"])
    except tmux.TmuxDown:
        s = None
    pid = int((s or {}).get("pid") or 0)
    return {"ports": previews.ports_under(pid) if pid else [], "pane_pid": pid or None}


class PreviewIn(BaseModel):
    port: int | None = None


@app.post("/api/tasks/{tid}/preview")
def api_task_preview(tid: int, body: PreviewIn | None = None):
    t, _ = _task_or_404(tid)
    if not tasks.has_worktree(t) and not t.get("tmux_name"):
        raise projects.Conflict("this task has no session yet; dispatch it first")   # nothing to preview, never allocate a port
    if not previews.public_host():
        raise projects.BadRequest("CCBOARD_PUBLIC_URL is not set (rerun install.sh)")
    port = body.port if body and body.port else None
    if port is None:
        found = api_task_ports(tid)["ports"]
        if not found:
            raise projects.Conflict("no listening port found under this session; start the dev server first or give the port")
        port = found[0]
    if not (1 <= port <= 65535):
        raise projects.BadRequest("bad port")
    try:                                   # never the board's own services (ttyd, code-server, ntfy, claude-mem, the board): #44 F-01
        previews.check_port(port)
    except previews.PreviewError as e:
        raise projects.BadRequest(str(e))
    with _preview_lock:
        # allocate and reserve under one lock: `tailscale serve` takes seconds, and two concurrent requests
        # reading preview_ports_in_use() before either wrote would get the same port
        t = db.task_get(tid) or t
        hp = t.get("preview_https")
        fresh = not hp
        if fresh:
            hp = previews.allocate_https_port(db.preview_ports_in_use())
            db.task_update(tid, preview_https=hp)
    try:
        url = previews.serve_on(hp, port)
    except Exception:
        if fresh:
            db.task_update(tid, preview_https=None)
        raise
    db.task_update(tid, preview_port=port, preview_https=hp)
    _invalidate_scan()
    return {"url": url, "https_port": hp, "port": port}


@app.delete("/api/tasks/{tid}/preview")
def api_task_preview_off(tid: int):
    t, _ = _task_or_404(tid)
    if t.get("preview_https"):
        previews.serve_off(int(t["preview_https"]))
    db.task_update(tid, preview_port=None, preview_https=None)
    _invalidate_scan()
    return {"ok": True}


_preview_lock = threading.Lock()


def _drop_preview(t: dict) -> None:
    if t.get("preview_https"):
        previews.serve_off(int(t["preview_https"]))
        db.task_update(t["id"], preview_port=None, preview_https=None)


class ArchiveIn(BaseModel):
    force: bool = False


@app.post("/api/tasks/{tid}/archive")
def api_archive_task(tid: int, body: ArchiveIn | None = None):
    t = db.task_get(tid)
    if t and t.get("mode") == "session" and (t.get("phase") or "running") not in ("backlog", "queued"):
        # a task handed to a running session borrowed it: no worktree to remove and the session is the user's, so only the card goes
        db.task_update(tid, archived_at=db_now(), status="archived")
        _invalidate_scan()
        return {"archived": tid, "worktree_removed": False}
    t, wt = _task_in_worktree(tid)       # a backlog or queued task has nothing to clean up: it is deleted, not archived
    force = bool(body and body.force)
    _drop_preview(t)
    if t["tmux_name"]:
        _end_session(t["tmux_name"], "killed")
    rpath = projects.repo_path(t["project"], t["repo"])
    err = tasks.remove_worktree(rpath, t["slug"], force=force, agent=t.get("agent"), path=wt) if rpath.is_dir() else None
    if err and not force:
        raise projects.Conflict(f"worktree not removed ({err}); archive with force to discard uncommitted work")
    db.task_update(tid, archived_at=db_now(), status="archived")
    _invalidate_scan()
    return {"archived": tid, "worktree_removed": err is None}


# ---------- scheduler (headless claude -p runs) ----------

class JobIn(BaseModel):
    name: str
    prompt: str
    cron: str | None = None
    permission_mode: str = "acceptEdits"
    max_turns: int | None = None             # Claude: 1..500 (30 when unset); Codex has no such limit and refuses the field
    max_budget_usd: float | None = None      # Claude only, like max_turns
    args: str | None = None
    timeout_s: int | None = None
    run_now: bool = False
    agent: str = "claude"                    # claude | codex: which CLI the headless run uses (v0.5.16)
    model: str | None = None                 # Codex: -m (Claude's model travels in args, as it always did)
    reasoning_effort: str | None = None      # Codex: -c model_reasoning_effort=
    opts: dict | None = None                 # the agent's own options by name (Codex: model, reasoning_effort); wins over the flat fields
    allowed_tools: str | list[str] | None = None      # Claude: pre-approved tool rules (--allowedTools), at most 20 (issue #107)
    acknowledge_fable: bool = False          # Claude: this run bills Fable usage credits without asking; needs max_budget_usd (issue #108)


def _job_agent(agent: str | None) -> str:
    name = (agent or "claude").strip().lower() if isinstance(agent, str) else "claude"
    if name not in agents.names():
        raise projects.BadRequest(f"agent must be one of {', '.join(agents.names())}")
    return name


def _job_installed(agent: str) -> None:
    if not (settings.claude_bin() if agent == "claude" else agents.get(agent).bin()):
        raise projects.BadRequest(f"{agent} is not installed on this box")


FABLE_REFUSAL = "This run bills Fable usage credits without asking (claude -p never asks first)"


def _claude_job_opts(body, parts: list[str]) -> dict:
    """What a Claude job stores in jobs.opts: its pre-approved tools and, when its model resolves to Fable, the acknowledgement given for THIS
    job (the time, the cap and the models acknowledged; editing the model text or the cap makes it stale). 422 for a Fable job without the
    acknowledgement and a Max $ within CCBOARD_HEADLESS_FABLE_CAP: a cap is not consent, and neither is consent without a cap."""
    claude = agents.get("claude")
    out: dict = {}
    tools = claude.allowed_tool_rules(body.allowed_tools)
    if tools:
        out["allowed_tools"] = tools
    models = claude_adapter.fable_models(parts, os.environ)
    if models:
        out["fable_ack"] = _fable_ack(body.acknowledge_fable, body.max_budget_usd, models)
    return out


def _fable_ack(acknowledged: bool, budget: float | None, models: list[str]) -> dict:
    """The acknowledgement stored on a Fable job: {at, cap, models}, or a 422 that says what happened, what it touches and what to do."""
    cap = settings.headless_fable_cap
    if not acknowledged:
        raise projects.Unprocessable(f"{FABLE_REFUSAL}. Tick the box to acknowledge it for this job and set Max $ (at most ${cap:g}); nothing was saved.")
    if budget is None:
        raise projects.Unprocessable(f"{FABLE_REFUSAL}. Set Max $ for this job (at most ${cap:g}); nothing was saved.")
    if budget > cap:
        raise projects.Unprocessable(f"{FABLE_REFUSAL}. Max $ may be at most ${cap:g} for a Fable job (CCBOARD_HEADLESS_FABLE_CAP); nothing was saved.")
    return {"at": db_now(), "cap": budget, "models": models}


def _validate_job(body) -> dict:
    """Everything a schedule or a batch must satisfy before it is stored (400 otherwise). Returns the agent's own options to store with the
    job ({} for Claude; Codex: model and reasoning_effort, validated by the adapter, whose wording refuses max_turns / max_budget_usd)."""
    agent = _job_agent(body.agent)
    if not body.name.strip() or not body.prompt.strip():
        raise projects.BadRequest("name and prompt are required")
    if body.cron and not scheduler.valid_cron(body.cron.strip()):
        raise projects.BadRequest("cron must be a 5-field expression like '30 2 * * *'")
    if body.permission_mode not in scheduler.MODES:
        raise projects.BadRequest(f"permission_mode must be one of {', '.join(scheduler.MODES)} (bypass only inside a devcontainer)")
    if agent == "claude" and body.max_turns is not None and not (1 <= body.max_turns <= 500):
        raise projects.BadRequest("max_turns must be 1..500")
    if body.max_budget_usd is not None and not (0 < body.max_budget_usd <= 1000):
        raise projects.BadRequest("max_budget_usd must be 0..1000")
    try:
        parts = scheduler.check_extra_args(body.args, agent)
    except ValueError as e:
        raise projects.BadRequest(str(e))
    if agent == "claude":
        return _claude_job_opts(body, parts)
    flat = {k: v for k, v in (("model", body.model), ("reasoning_effort", body.reasoning_effort),
                              ("max_turns", body.max_turns), ("max_budget_usd", body.max_budget_usd)) if v not in (None, "")}
    own = body.opts if isinstance(body.opts, dict) else {}
    clean = agents.get(agent).validate_opts({**flat, **own}, interactive=False, tasks_or_headless=True)
    return {k: clean[k] for k in ("model", "reasoning_effort") if k in clean}


@app.post("/api/projects/{project}/repos/{repo}/jobs", status_code=201)
def api_create_job(project: str, repo: str, body: JobIn):
    rpath = projects.repo_path(project, repo)
    if not projects.is_repo(rpath):
        raise projects.NotFound("not a git repo")
    opts = _validate_job(body)
    agent = _job_agent(body.agent)
    _job_installed(agent)
    cron = body.cron.strip() if body.cron else None
    # run_now starts it at once; a cron waits for its next fire; neither = parked (next_run_at NULL): it runs when "Run now" asks, never before
    next_at = db_now() if body.run_now else (scheduler.next_fire(cron) if cron else None)
    jid = db.job_add(project=project, repo=repo, name=" ".join(body.name.split())[:80], prompt=body.prompt.strip(), cron=cron,
                     permission_mode=body.permission_mode, max_turns=body.max_turns, max_budget_usd=body.max_budget_usd,
                     args=body.args, timeout_s=body.timeout_s, enabled=1, batch_id=None, next_run_at=next_at, agent=agent,
                     opts=opts or None)
    _invalidate_scan()
    return {"id": jid, "next_run_at": next_at, "agent": agent}


class BatchIn(BaseModel):
    prompt: str
    repos: list[str]                 # "project/repo" ids
    name: str | None = None
    permission_mode: str = "acceptEdits"
    max_turns: int | None = None
    max_budget_usd: float | None = None
    args: str | None = None
    agent: str = "claude"            # every job of the batch runs with this agent
    model: str | None = None
    reasoning_effort: str | None = None
    opts: dict | None = None
    allowed_tools: str | list[str] | None = None
    acknowledge_fable: bool = False  # one acknowledgement per batch submission: it is recorded on every job the batch makes


@app.post("/api/batch", status_code=201)
def api_batch(body: BatchIn):
    """One prompt across N repos: one-off jobs sharing a batch id, drained by the scheduler (cap + quota aware), for Claude or Codex."""
    if not body.repos:
        raise projects.BadRequest("choose at least one repo")
    if len(body.repos) > 100:
        raise projects.BadRequest("at most 100 repos per batch")
    agent = _job_agent(body.agent)
    opts = _validate_job(JobIn(name=body.name or "batch", prompt=body.prompt, permission_mode=body.permission_mode,
                               max_turns=body.max_turns, max_budget_usd=body.max_budget_usd, args=body.args, agent=agent,
                               model=body.model, reasoning_effort=body.reasoning_effort, opts=body.opts,
                               allowed_tools=body.allowed_tools, acknowledge_fable=body.acknowledge_fable))
    _job_installed(agent)
    targets = []
    for ident in body.repos:
        if not isinstance(ident, str) or ident.count("/") != 1:
            raise projects.BadRequest(f"bad repo id {ident!r}")
        p, r = ident.split("/")
        rpath = projects.repo_path(p, r)
        if not projects.is_repo(rpath):
            raise projects.NotFound(f"{ident} is not a git repo")
        targets.append((p, r))
    batch_id = uuid.uuid4().hex[:8]
    name = " ".join((body.name or "batch").split())[:60]
    ids = []
    for p, r in targets:
        ids.append(db.job_add(project=p, repo=r, name=f"{name} [{batch_id}]", prompt=body.prompt.strip(), cron=None,
                              permission_mode=body.permission_mode, max_turns=body.max_turns, max_budget_usd=body.max_budget_usd,
                              args=body.args, timeout_s=None, enabled=1, batch_id=batch_id, next_run_at=db_now(), agent=agent,
                              opts=opts or None))
    started = sched.tick() if sched else []
    _invalidate_scan()
    return {"batch_id": batch_id, "jobs": ids, "started": started, "agent": agent}


class FableAckIn(BaseModel):
    acknowledge_fable: bool = False
    max_budget_usd: float | None = None


@app.post("/api/jobs/{jid}/acknowledge-fable")
def api_job_acknowledge_fable(jid: int, body: FableAckIn):
    """Edit a held Fable job: record the acknowledgement and the Max $ for it (the only way a stored Fable job without one ever runs).
    422 as on creation; the job is re-armed (a cron job for its next fire, a one-off now)."""
    j = db.job_get(jid)
    if not j:
        raise projects.NotFound("no such job")
    if (j.get("agent") or "claude") != "claude":
        raise projects.BadRequest("only a Claude job bills Fable")
    try:
        parts = shlex.split(j.get("args") or "")
    except ValueError:
        parts = []
    models = claude_adapter.fable_models(parts, os.environ)
    if not models:
        raise projects.BadRequest("this job does not resolve to Fable: nothing to acknowledge")
    if body.max_budget_usd is not None and not (0 < body.max_budget_usd <= 1000):
        raise projects.BadRequest("max_budget_usd must be 0..1000")
    ack = _fable_ack(body.acknowledge_fable, body.max_budget_usd, models)
    opts = {**scheduler.job_opts(j), "fable_ack": ack}
    fields = {"opts": json.dumps(opts), "max_budget_usd": body.max_budget_usd, "last_status": "acknowledged"}
    if j["enabled"] and j.get("last_status") == scheduler.FABLE_HELD:
        fields["next_run_at"] = scheduler.next_fire(j["cron"]) if j.get("cron") else db_now()
    db.job_update(jid, **fields)
    _invalidate_scan()
    return {"id": jid, "fable_ack": ack}                # the scheduler's own tick (every 30 s) starts a re-armed one-off; no run is started from this route


@app.post("/api/jobs/{jid}/run")
def api_job_run(jid: int):
    require_real_launch_ok()   # issue #100: refuses (409) on a dev board whose directories could reach the real home
    if not db.job_get(jid):
        raise projects.NotFound("no such job")
    db.job_update(jid, next_run_at=db_now(), enabled=1)
    started = sched.tick() if sched else []
    _invalidate_scan()
    return {"ok": True, "started": started}


@app.post("/api/jobs/{jid}/toggle")
def api_job_toggle(jid: int):
    j = db.job_get(jid)
    if not j:
        raise projects.NotFound("no such job")
    enabled = 0 if j["enabled"] else 1
    fields = {"enabled": enabled}
    if enabled:
        # a fired one-off job has next_run_at=NULL; enabling it again means "run it again now"
        fields["next_run_at"] = scheduler.next_fire(j["cron"]) if j.get("cron") else db_now()
    db.job_update(jid, **fields)
    _invalidate_scan()
    return {"id": jid, "enabled": enabled}


@app.delete("/api/jobs/{jid}")
def api_job_delete(jid: int):
    if not db.job_get(jid):
        raise projects.NotFound("no such job")
    for rid in db.job_delete(jid):                       # its runs go with it (job ids are reused): so do their output files
        try:
            (Path(settings.data_dir) / "runs" / f"{rid}.txt").unlink(missing_ok=True)
        except OSError as e:
            log.debug("run output %s not removed: %s", rid, e)
    _invalidate_scan()
    return {"deleted": jid}


def _job_agents() -> dict[int, str]:
    return {j["id"]: j.get("agent") or "claude" for j in db.jobs()}


@app.get("/api/runs/{rid}")
def api_run_get(rid: int):
    r = db.run_get(rid)
    if not r:
        raise projects.NotFound("no such run")
    j = db.job_get(r["job_id"])
    return {**r, "agent": (j or {}).get("agent") or "claude"}


@app.post("/api/runs/{rid}/resume")
def api_run_resume(rid: int):
    """Open the run's worktree in a terminal session, resuming its conversation: `claude --resume <id>`, or `codex resume <thread id>`
    for a Codex run (the headless run's rollout stays on disk, it is never --ephemeral)."""
    require_real_launch_ok()   # issue #100: refuses (409) on a dev board whose directories could reach the real home
    r = db.run_get(rid)
    if not r or not r.get("task_id"):
        raise projects.NotFound("no worktree for this run")
    t = db.task_get(int(r["task_id"]))
    if not t or not tasks.has_worktree(t) or not Path(t["worktree"]).is_dir():
        raise projects.NotFound("worktree is gone")
    agent = t.get("agent") or "claude"
    name = t["tmux_name"]
    if not tmux.has_session(name):
        _, _, session = tmux.split_name(name)
        if agent == "claude":
            cmd = ["claude", "--resume", r["session_id"]] if r.get("session_id") else ["claude", "--continue"]
        else:
            ag = agents.get(agent)
            cmd = ag.resume_argv(r["session_id"]) if r.get("session_id") else ag.continue_argv(t["worktree"])
        _start_session(name, t["project"], t["repo"], session, "task", t["worktree"], cmd_line=shlex.join(cmd),
                       claude_session_id=r.get("session_id"), add_dirs=[], agent=agent, task_id=t["id"])
    _invalidate_scan()
    return {"tmux": name, "attach_url": f"/term/{name}"}


# ---------- sessions ----------

# Settings overrides can change the permission mode (and hooks, tools) behind the board's back: always rejected in
# extra args, use the controls instead. The bypass flags themselves are an explicit choice on interactive sessions.
# (OVERRIDE_PARTS and BYPASS_PARTS live in app.agents.claude, where launch_plan enforces the same rules.)

def _arg_matching(extra: list[str], parts: tuple[str, ...]) -> str | None:
    for a in extra:
        low = a.lower()
        if any(b in low for b in parts):
            return a
    return None


def _override_requested(extra: list[str]) -> str | None:
    return _arg_matching(extra, OVERRIDE_PARTS)


def _bypass_requested(extra: list[str]) -> str | None:
    return _arg_matching(extra, BYPASS_PARTS)


def _normalize_launcher(launcher: str, agent: str | None = None) -> tuple[str, str]:
    """A request's launcher -> (agent, launcher), the launcher being what sessions.launcher stores (claude = a new session,
    resume, continue, shell). The codex launchers (codex, codex-resume, codex-continue) start a Codex session; so does the plain
    claude / resume / continue launcher with `agent: "codex"` (the launcher then only says new, resume or continue). Any other
    pairing of a launcher with a different agent is a 400."""
    entry = _LAUNCHER_AGENT.get(launcher) if isinstance(launcher, str) else None
    if entry is None:
        accepted = [k for k, (a, _l) in _LAUNCHER_AGENT.items() if a in STARTABLE_AGENTS]
        raise projects.BadRequest(f"launcher must be one of {', '.join(accepted)}")
    named, norm = entry
    if agent not in (None, "", named):
        if not (isinstance(agent, str) and agent in STARTABLE_AGENTS):
            raise projects.BadRequest(f"unknown agent {agent!r}; use {' or '.join(STARTABLE_AGENTS)}")
        if named != "claude" or agent == "shell":            # only a new/resume/continue launcher can be pointed at another agent
            raise projects.BadRequest(f"launcher {launcher!r} starts {named}; it cannot start {agent}")
        named = agent
    if named not in STARTABLE_AGENTS or (named != "shell" and named not in agents.names()):
        raise projects.BadRequest(f"{named} is not available on this build")
    return named, norm


class SessionIn(LaunchOpts):
    """The body of POST .../sessions. The flat LaunchOpts fields and the first group below are the compat shape (the launcher before v0.5.13
    and the API's callers); the second group is the launcher sheet's semantic fields (v0.5.13): the UI sends words, the adapter turns them into
    flags. `launcher` also accepts new, from_pr and fork (new = claude; from_pr = Claude `--from-pr`; fork = Codex `codex fork`)."""
    launcher: str
    agent: str | None = None           # None: the launcher's own agent; "codex" turns claude / resume / continue into the Codex launch
    name: str | None = None
    args: str | None = None
    resume_id: str | None = None       # Claude: a UUID; Codex: a UUID or a session name
    add_dirs: list[str] | None = None  # "project/repo" ids
    devcontainer: bool = False         # run claude inside the repo's devcontainer (devcontainer CLI)
    bypass: bool = False               # --dangerously-skip-permissions (Claude) / the bypass acknowledgement; needed by mode "bypass"
    mode: str | None = None            # default | auto | read-only | bypass | custom (Codex's picker); Claude also takes its permission modes
    reasoning: str | None = None       # Codex reasoning effort (Claude: read as effort when none is sent)
    prompt: str | None = None          # the first message of a new session
    worktree: object = None            # true, or the worktree's name: Claude `--worktree <name>`; Codex: a managed `git worktree add`
    worktree_name: str | None = None
    from_pr: object = None             # Claude: the PR (123, #123 or its URL) whose session to resume
    fork_session: bool = False         # resume / continue as a copy (Claude --fork-session; Codex: resume by id becomes `codex fork`)
    fallback_model: object = None      # Claude: up to three models, a list or comma separated
    autocompact: object = None         # Claude: auto or a token count
    tools: object = None               # Claude --tools: the built-in tools the session has
    agent_name: str | None = None      # Claude --agent <name>
    mcp_config: str | None = None      # Claude --mcp-config <file>
    config: object = None              # Codex -c lines: a list, or one text with a key=value per line
    fast: bool | None = None           # Claude fast mode (stored; there is no CLI flag)
    cwd_rel: str | None = None         # start in this folder of the repo (relative, no ..)
    sandbox: str | None = None         # Codex -s (mode custom); `opts` carries the same and wins
    approval: str | None = None        # Codex -a (mode custom)
    search: bool | None = None         # Codex --search


def _resolve_add_dirs(ids: list[str] | None, own: Path) -> list[str]:
    out: list[str] = []
    for ident in ids or []:
        if not isinstance(ident, str) or ident.count("/") != 1:
            raise projects.BadRequest(f"bad add_dir id {ident!r}; expected project/repo")
        p, r = ident.split("/")
        path = projects.repo_path(p, r)
        if not (path / ".git").exists():
            raise projects.BadRequest(f"{ident} is not a git repo")
        if path.resolve() == own.resolve():
            continue
        out.append(str(path))
    return out


def _free_session_name(project: str, repo: str) -> str:
    try:
        existing = set(tmux.list_sessions().keys())
    except tmux.TmuxDown:
        raise
    for i in range(1, 1000):
        cand = f"s{i}"
        if tmux.tmux_name(project, repo, cand) not in existing:
            return cand
    raise projects.Conflict("too many sessions")


def _start_session_row(name: str, project: str, repo: str, session: str, launcher: str, cwd: str, *,
                       cmd_line: str | None = None, claude_session_id: str | None = None, add_dirs: list[str] | None = None,
                       agent: str = "claude", opts: dict | None = None, task_id: int | None = None, env_extra: dict | None = None) -> tuple[str, int]:
    """Create the tmux session and its row (agent, launch cwd and the validated opts stored on it), then type the command.
    Returns (real tmux name, sessions.id). With task_id the task's session_row is pointed at the new row, which is how a
    relaunched task session (fix-ci, run resume, reboot recovery) stays the task's live session."""
    require_real_launch_ok()   # issue #100: refuses (409) on a dev board whose directories could reach the real home
    env = {"CCBOARD_SESSION": name, "CCBOARD_URL": settings.loopback_url(),
           "CCBOARD_APPROVE_TIMEOUT": str(int(settings.approve_timeout)), "CCBOARD_AGENT": agent, **(env_extra or {})}
    real = tmux.new_session(name, cwd, env=env)
    try:                                                      # the account the session starts under (None: unknown, the column stays NULL)
        account = accounts.current(db) if agent == "claude" else None
    except Exception as e:
        log.debug("current account lookup failed: %s", e)
        account = None
    row_id = db.add_session(tmux_name=real, project=project, repo=repo, name=session, launcher=launcher, cmd=cmd_line,
                            claude_session_id=claude_session_id, add_dirs=add_dirs, agent=agent, cwd=cwd, opts=opts,
                            account=account)
    if agent == "codex":                                      # what -a / -s the line runs under, for the Tune's Approvals and Sandbox rows (None: config.toml decides)
        try:
            perm = agents.get("codex").permissions_of(opts, cmd_line)
            if perm:
                db.update_flags(real, {"perm": perm})
        except Exception as e:
            log.debug("permissions of %s unknown: %s", real, e)
    if cmd_line:
        try:
            tmux.send_line(real, cmd_line)
        except tmux.TmuxError:
            tmux.kill_session(real)
            db.end(real, "killed")
            raise
    if task_id is not None:                                   # only once the command is really typed: a failed launch leaves the task unbound
        db.task_update(task_id, session_row=row_id)
    return real, row_id


def _start_session(name: str, project: str, repo: str, session: str, launcher: str, cwd: str, *,
                   cmd_line: str | None = None, claude_session_id: str | None = None, add_dirs: list[str] | None = None,
                   agent: str = "claude", opts: dict | None = None, task_id: int | None = None, env_extra: dict | None = None) -> str:
    """_start_session_row for callers that only need the tmux name (reboot recovery calls it positionally)."""
    return _start_session_row(name, project, repo, session, launcher, cwd, cmd_line=cmd_line, claude_session_id=claude_session_id,
                              add_dirs=add_dirs, agent=agent, opts=opts, task_id=task_id, env_extra=env_extra)[0]


def _end_session(name: str, reason: str = "killed") -> bool:
    """Kill a session and close its row: kill tmux, db.end(name, reason), drop the tailnet previews of its tasks, cancel a
    pending auto-close, invalidate the scan. Returns whether tmux had it; when it did not, nothing is written (the row, if
    any, is closed by the next reconcile with reason 'reconciled'). reason: killed | auto_close | exited | project_deleted."""
    if not name or not tmux.kill_session(name):
        return False
    db.update_flags(name, {"autoclose": None})            # a pending auto-close ({task, due}) must not fire for a dead session
    db.end(name, reason)
    for t in db.tasks():
        if t["tmux_name"] == name:
            _drop_preview(t)     # the task's tailnet preview would otherwise keep its port and serve mapping
    _invalidate_scan()
    return True


# The launch kinds the launcher sheet adds to the launcher names: new (Claude unless `agent` says otherwise), from_pr (Claude --from-pr) and
# fork (Codex `codex fork`). Each names the launcher it is stored as and the adapter's launch kind. They stay out of _LAUNCHER_AGENT so the
# "launcher must be one of ..." answer for an unknown name does not change.
_KIND_LAUNCHERS = {"new": ("claude", "new"), "from_pr": ("resume", "from_pr"), "from-pr": ("resume", "from_pr"),
                   "fork": ("resume", "fork"), "codex-fork": ("codex-resume", "fork")}
_ON = ("1", "true", "yes", "on")
MAX_CWD_REL = 300


def _wants(v) -> bool:
    """Did the launcher ask for this switch? True, or an on-word; a name (a worktree's) counts as on for the caller that reads it."""
    if isinstance(v, str):
        return v.strip().lower() in _ON
    return bool(v) if isinstance(v, (bool, int)) else False


def _worktree_request(body) -> tuple[bool, str | None]:
    """(on, name) from `worktree` (true, or the name itself) and `worktree_name`. A worktree_name beside an off switch is nothing."""
    w, named = body.worktree, (body.worktree_name or "").strip() or None
    if isinstance(w, str) and w.strip() and not _wants(w) and w.strip().lower() not in ("0", "false", "no", "off"):
        return True, named or w.strip()
    return _wants(w), named


def _session_cwd(rpath: Path, rel: str | None) -> Path:
    """Where the session starts: the repo, or a folder inside it (`cwd_rel`: relative, no '..', an existing directory whose real path stays
    under the repo's)."""
    if rel in (None, "", ".", "./"):
        return rpath
    if not isinstance(rel, str) or len(rel) > MAX_CWD_REL or "\0" in rel or rel.startswith(("/", "~")) or ".." in Path(rel).parts:
        raise projects.BadRequest("cwd_rel: a folder inside the repo, written relative to it (no '..')")
    target = rpath / rel
    real, root = target.resolve(), rpath.resolve()
    if not target.is_dir() or not (real == root or root in real.parents):
        raise projects.BadRequest(f"cwd_rel: {rel} is not a folder inside this repo")
    return target


def _launch_kind(body) -> tuple[str, str, str]:
    """(agent, stored launcher, adapter launch kind) of a session request. The agent and stored launcher come from _normalize_launcher
    (new / from_pr / fork are spelled through _KIND_LAUNCHERS first); `from_pr` beside a new launch makes it a from_pr launch, and
    `fork_session` beside a Codex resume makes it a fork. Raises the board's 400s for a pairing that does not exist."""
    named = body.launcher
    kind: str | None = None
    if isinstance(named, str) and named in _KIND_LAUNCHERS:
        named, kind = _KIND_LAUNCHERS[named]
    agent, launcher = _normalize_launcher(named, body.agent)
    kind = kind or {"claude": "new", "resume": "resume", "continue": "continue"}.get(launcher, "new")
    if body.from_pr not in (None, "", 0, False):
        if kind == "new":
            kind, launcher = "from_pr", "resume"
        elif kind != "from_pr":
            raise projects.BadRequest("from_pr: only a new launch takes a pull request (resume and continue pick up a session by themselves)")
    if kind == "from_pr" and agent != "claude":
        raise projects.BadRequest("from_pr: only Claude resumes a session from its pull request")
    if kind == "fork" and agent != "codex":
        raise projects.BadRequest("fork: a Codex launch; with Claude resume or continue and tick fork_session")
    if body.fork_session and agent == "codex":
        if kind != "resume":
            raise projects.BadRequest("fork_session: Codex forks a session by its id or name (or from its picker), not the latest one")
        kind = "fork"
    return agent, launcher, kind


def _claude_session_opts(body, extra: list[str]) -> dict:
    """The raw option dict the Claude adapter validates: the flat compat fields as always, then the launcher's words (mode, tools, ...).
    Empty values are the form's untouched fields: dropped, so they never reach validation."""
    opts = {**body.model_dump(include=set(LaunchOpts.model_fields)), "extra": extra, "devcontainer": body.devcontainer}
    for k in ("mode", "reasoning", "tools", "agent_name", "fallback_model", "autocompact", "mcp_config", "from_pr", "fork_session", "fast"):
        v = getattr(body, k)
        if v not in (None, "", [], False):
            opts[k] = str(v) if k in ("from_pr", "autocompact") and isinstance(v, int) else v
    return opts


def _codex_session_opts(body, extra: list[str]) -> dict:
    """The raw option dict the Codex adapter validates. Claude-only fields are forwarded too, so the adapter refuses them by name
    ("tools: not supported by codex") instead of this route dropping them silently; `opts` (the agent's own keys) wins over the rest."""
    own: dict = {}
    for k in ("mode", "reasoning", "sandbox", "approval", "search", "config", "tools", "fallback_model", "autocompact", "agent_name",
              "mcp_config"):
        v = getattr(body, k)
        if v not in (None, "", [], False):
            own[k] = v
    flat = _agent_opts("codex", body)
    if own.get("reasoning"):
        flat["reasoning_effort"] = own.pop("reasoning")
    return {**own, **flat, "extra": extra}


@app.post("/api/projects/{project}/repos/{repo}/sessions", status_code=201)
def api_create_session(project: str, repo: str, body: SessionIn):
    require_real_launch_ok()   # issue #100: refuses (409) on a dev board whose directories could reach the real home
    agent, launcher, kind = _launch_kind(body)
    rpath = projects.repo_path(project, repo)
    if not rpath.is_dir():
        raise projects.NotFound(f"repo {project}/{repo} not found")
    session = body.name or _free_session_name(project, repo)
    projects.check_name("session", session)
    if session == "clone":
        raise projects.BadRequest("'clone' is reserved")
    name = tmux.tmux_name(project, repo, session)
    if tmux.has_session(name):
        raise projects.Conflict(f"session {session} already exists")

    extra: list[str] = []
    if body.args:
        if len(body.args) > MAX_ARGS:
            raise projects.BadRequest("extra args too long")
        try:
            extra = shlex.split(body.args)
        except ValueError as e:
            raise projects.BadRequest(f"extra args: {e}")
    codex_names = agent == "codex"                 # a Codex resume or fork may name the session instead of giving its UUID
    if body.resume_id and not (UUID_RE.match(body.resume_id) or (codex_names and CODEX_NAME_RE.match(body.resume_id))):
        raise projects.BadRequest("resume id must be a UUID" + (" or a session name" if codex_names else ""))

    cmd_line = None
    agent_session_id = None
    add_dirs: list[str] = []
    opts_clean: dict | None = None
    if body.devcontainer and not projects.has_devcontainer(rpath):
        raise projects.BadRequest("this repo has no .devcontainer/devcontainer.json")
    if body.devcontainer and agent != "codex":       # the typed line starts with `devcontainer`: without the CLI the pane only says "command not found" (issue #104)
        cli = doctor.devcontainer_cli_state()
        if cli in ("missing", "broken"):
            raise projects.Conflict(
                ("the devcontainer CLI is not installed on this host" if cli == "missing" else "the devcontainer CLI on this host does not run (it needs node)")
                + f", so the devcontainer launch would only print \"command not found\". Install it on the host ({doctor.DEVCONTAINER_INSTALL}, "
                  "or CCBOARD_DEVCONTAINER=1 ./install.sh), or launch without the devcontainer")
    bad = _override_requested(extra) if agent != "codex" else agents.get("codex").forbidden_extra(extra, interactive=True)
    if bad:
        raise projects.BadRequest(f"{bad}: settings overrides are not allowed in extra args; use the model / effort / permission / tools controls")
    # The danger gate of the launcher sheet: mode "bypass" is the explicit choice and needs the acknowledgement that came with it. The flat
    # compat spellings (permission_mode bypassPermissions, bypass, the args) stay what they were: on the host an explicit choice, never the
    # default, never for tasks / dispatch / headless runs (those routes never read `mode`).
    bypass_mode = isinstance(body.mode, str) and body.mode == "bypass" and agent != "shell"
    if bypass_mode and not body.bypass:
        raise projects.BadRequest("mode bypass skips every approval and the sandbox: send bypass: true with the acknowledgement")
    prompt = (body.prompt or "").strip() or None
    wt_on, wt_name = _worktree_request(body)
    if wt_on and agent != "shell":
        if kind != "new":
            raise projects.BadRequest("worktree: only a new session can start in a new worktree")
        if not projects.is_repo(rpath):
            raise projects.BadRequest("worktree: this folder is not a git repo")
        if wt_name and not WORKTREE_RE.match(wt_name):
            raise projects.BadRequest("worktree name: use letters, digits, '.', '_' or '-'")
    if body.cwd_rel not in (None, "", ".", "./") and (body.devcontainer or (wt_on and agent != "shell")):
        raise projects.BadRequest("cwd_rel: not together with a worktree or the devcontainer (both start at the repo's root)")
    cwd = _session_cwd(rpath, body.cwd_rel)
    # bypassPermissions on the host is an explicit choice (the permission control, the bypass flag, or the arg);
    # Claude Code itself still asks for a one-time confirmation in the terminal. Never the default.
    managed: tuple[str, Path] | None = None
    if agent == "claude":
        exe = settings.claude_bin()
        if not exe and not body.devcontainer:
            raise projects.BadRequest("claude is not installed on this box")
        add_dirs = _resolve_add_dirs(body.add_dirs, rpath) if not body.devcontainer else []
        plan = agents.get("claude").launch_plan(LaunchReq(
            kind=kind, session_name=session, cwd=str(cwd), opts=_claude_session_opts(body, extra), resume_id=body.resume_id,
            add_dirs=add_dirs, prompt=prompt, bypass=body.bypass and not bypass_mode,
            worktree=(wt_name or f"{session}-{uuid.uuid4().hex[:6]}") if wt_on else None))
        cmd_line, agent_session_id, opts_clean = plan.cmd_line, plan.agent_session_id, plan.opts_clean
    elif agent == "codex":
        # Codex has no --session-id: a new session's id is learned from its first hook (or, in v0.5.12, its rollout), and a resume
        # keeps the id it was asked for. The adapter builds the whole line and applies the bypass gate (interactive sessions only).
        ag = agents.get("codex")
        if not ag.bin():
            raise projects.BadRequest("codex is not installed on this box")
        if body.devcontainer:
            raise projects.BadRequest("devcontainer is only supported for claude sessions")
        add_dirs = _resolve_add_dirs(body.add_dirs, rpath)
        raw = _codex_session_opts(body, extra)
        if wt_on:
            ag.validate_opts(raw, interactive=True, tasks_or_headless=False, bypass=body.bypass)    # a 400 before there is a worktree to clean up
            # Codex has no native worktree: the board makes it (git worktree add -b worktree-<slug> under .ccboard/worktrees, the repo's
            # .worktreeinclude files copied in, as for a task) and starts Codex inside it; a launch that fails takes it away again
            slug = tasks.unique_slug(rpath, tasks.slugify(wt_name or session), db.task_slugs(project, repo))
            tasks.ensure_excluded(rpath)
            try:
                wt = tasks.create_managed_worktree(rpath, slug, tasks.default_branch(rpath), "codex")
            except tasks.WorktreeError as e:
                raise gitops.GitError(str(e)) from e
            tasks.apply_worktreeinclude(rpath, wt)
            managed, cwd = (slug, wt), wt
        try:
            plan = ag.launch_plan(LaunchReq(kind=kind, session_name=session, cwd=str(cwd), opts=raw, resume_id=body.resume_id,
                                            add_dirs=add_dirs, prompt=prompt, bypass=body.bypass))
        except Exception:
            if managed:
                tasks.discard_managed_worktree(rpath, managed[0], managed[1])
            raise
        cmd_line, agent_session_id, opts_clean = plan.cmd_line, plan.agent_session_id, plan.opts_clean
    elif body.devcontainer:
        wf = str(rpath)
        cmd_line = (shlex.join(["devcontainer", "up", "--workspace-folder", wf]) + " && "
                    + shlex.join(["devcontainer", "exec", "--workspace-folder", wf, "--", "bash", "-l"]))

    try:
        real = _start_session(name, project, repo, session, launcher, str(cwd), cmd_line=cmd_line,
                              claude_session_id=agent_session_id, add_dirs=add_dirs, agent=agent, opts=opts_clean)
    except Exception:
        if managed:
            tasks.discard_managed_worktree(rpath, managed[0], managed[1])
        raise
    _invalidate_scan()
    return {"tmux": real, "attach_url": f"/tty/?arg={real}", "agent": agent, "agent_session_id": agent_session_id,
            "claude_session_id": agent_session_id, "cmd": cmd_line}


class KeysIn(BaseModel):
    keys: list[str] | None = None
    text: str | None = None
    enter: bool = False


@app.post("/api/sessions/{name}/keys")
def api_send_keys(name: str, body: KeysIn):
    try:
        tmux.split_name(name)
    except ValueError:
        raise projects.BadRequest("not a ccboard session name")
    if not tmux.has_session(name):
        raise projects.NotFound(f"session {name} not found")
    if body.text is not None:
        text = body.text.replace("\r\n", "\n").replace("\r", "\n")       # the composer's newlines; multi-line goes through bracketed paste
        # every Cc character but tab and newline (DEL and the C1 range too), the rule /prompt uses: #44 F-04
        if len(text) > 8000 or any(unicodedata.category(c) == "Cc" and c not in "\t\n" for c in text):
            raise projects.BadRequest("text too long or contains control characters")
        tmux.send_text(name, text, enter=body.enter)
    if body.keys:
        if len(body.keys) > 20:
            raise projects.BadRequest("too many keys")
        try:
            tmux.send_keys(name, body.keys)
        except ValueError as e:
            raise projects.BadRequest(str(e))
    return {"ok": True}


class ScrollIn(BaseModel):
    # object, not str/int: a wrong type must answer 400 like a wrong value (the app has no 422 handler for bodies)
    dir: object = None
    n: object = 1


def _check_terminal_name(name: str) -> None:
    """400 for a name that is not a ccboard session (internal sessions included)."""
    try:
        tmux.split_name(name)
    except ValueError:
        raise projects.BadRequest("not a ccboard session name")


def _terminal_session(name: str) -> None:
    """400 for a name that is not a ccboard session, 404 for one tmux does not have."""
    _check_terminal_name(name)
    if not tmux.has_session(name):
        raise projects.NotFound(f"session {name} not found")


@app.post("/api/sessions/{name}/scroll")
def api_scroll(name: str, body: ScrollIn | None = None):
    """Scroll the terminal: copy-mode for a shell pane, forwarded PageUp/PageDown/C-Home/C-End for Claude's fullscreen TUI."""
    _check_terminal_name(name)
    d, n = (body.dir, body.n) if body else (None, 1)
    if d not in tmux.SCROLL_DIRS:
        raise projects.BadRequest("dir must be one of " + ", ".join(tmux.SCROLL_DIRS))
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= tmux.SCROLL_MAX:
        raise projects.BadRequest(f"n must be an integer from 1 to {tmux.SCROLL_MAX}")
    _terminal_session(name)
    agent = (db.open_rows().get(name) or {}).get("agent") or "claude"
    return {"ok": True, **tmux.scroll(name, d, n, agent)}


@app.get("/api/sessions/{name}/pane")
def api_pane(name: str):
    """The pane's terminal state (alternate screen, copy-mode, scroll position, sizes) and who is attached."""
    _terminal_session(name)
    return {**tmux.pane_info(name), "viewers": tmux.viewers().get(name) or {"full": 0, "grid": 0, "ro": 0}}


# ---------- terminal commands: /resize, /command, /prompt ----------

class ResizeIn(BaseModel):
    cols: object = None            # object, not int: a wrong type answers 400 like a wrong value (see ScrollIn)
    rows: object = None


@app.post("/api/sessions/{name}/resize")
def api_resize(name: str, body: ResizeIn | None = None):
    """Size the session's tmux window (cols x rows) so a phone-shaped terminal reads right. 409 while a full (writable, sized)
    client is attached: its size is that client's, and tmux would snap the window back to it."""
    _check_terminal_name(name)
    cols, rows = (body.cols, body.rows) if body else (None, None)
    for v, what, (lo, hi) in ((cols, "cols", tmux.RESIZE_COLS), (rows, "rows", tmux.RESIZE_ROWS)):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise projects.BadRequest(f"{what} must be an integer from {lo} to {hi}")
    _terminal_session(name)
    try:
        viewers = tmux.viewers().get(name) or {"full": 0, "grid": 0, "ro": 0}
    except tmux.TmuxError:               # list-clients failed: when in doubt count every attached client as a full one
        viewers = {"full": int((tmux.list_sessions().get(name) or {}).get("attached") or 0), "grid": 0, "ro": 0}
    if viewers["full"] > 0:
        return JSONResponse({"error": "a terminal is attached to this session; it sets the window size", "viewers": viewers},
                            status_code=409)
    tmux.resize_window(name, cols, rows)
    _invalidate_scan()
    return {"ok": True, "cols": cols, "rows": rows}


CMD_ARG_MAX = 200
PROMPT_MAX = 20000
CMD_WAIT_DEFAULT, CMD_WAIT_MAX = 1200, 4000      # ms a read command (/usage ...) gets to draw before the pane is captured
CMD_PENDING_TTL = 20                             # seconds a typed command waits for the statusline that confirms it
# States in which an agent's composer is live and takes typed text: idle, done and errored (a failed turn leaves the prompt up).
# `waiting` is allowed only for wait_kind 'idle' (the idle prompt: Claude is waiting for the person), never for a permission or
# elicitation dialog; no state yet (a row Claude has not reported on: it may still be booting behind the trust dialog) and
# `ended` are refused.
TYPEABLE_STATES = ("idle", "done", "errored")
PERMISSION_GRACE = 15                            # s past approve_timeout after which an undecided request has no waiter left


def _cmd_now() -> str:
    """The one clock for pending_cmd.at and its 20 s rule (tests replace it)."""
    return db_now()


def _terminal_refusal(code: str, message: str, row: dict | None, retry: int | None, **extra) -> JSONResponse:
    """409 {error, message, state, wait_kind, retry}: why the board will not type into this pane right now and when to ask again
    (retry = seconds, None when waiting will not help)."""
    flags = (row or {}).get("flags") or {}
    return JSONResponse({"error": code, "message": message, "state": (row or {}).get("state"), "wait_kind": flags.get("wait_kind"),
                         "retry": retry, **extra}, status_code=409)


def _agent_row(name: str) -> tuple[dict | None, object | None, JSONResponse | None]:
    """(row, adapter, None) for a session whose open row belongs to an agent adapter; otherwise (None, None, 409): no open row
    (an external tmux session) or a shell row (nothing there reads slash commands or queues prompts)."""
    row = db.open_row(name)
    if row is None:
        return None, None, _terminal_refusal("no_open_row", "the board has no open row for this session", None, None)
    try:
        return row, agents.get(row.get("agent") or "claude"), None
    except KeyError:
        return None, None, _terminal_refusal("not_an_agent", "this is a shell, not an agent session", row, None)


LAUNCH_GRACE = 20                                # s after a session row is made in which a login shell in its pane is the launch line being typed


def _pane_block(name: str, row: dict | None, command: str | None = None, *, shell: bool = True, invalidate: bool = True) -> tuple[str, str] | None:
    """(code, message) when the board must not type into this Codex pane, else None (any other agent: None). `agent_exited`: the pane's
    foreground process is the login shell (codex quit; a prompt typed now would run as a shell command); the row is marked errored with
    the reason. `dialog`: the screen shows Codex's update dialog (its default, Update now, runs npm install -g) or its trust dialog; the
    board never answers one. Box checks 86 and 92. A tmux hiccup is no evidence: None. `invalidate` is off inside the state builder,
    which holds the scan lock."""
    if (row or {}).get("agent") != "codex":
        return None
    try:
        cmd = command if command is not None else tmux.pane_info(name).get("cmd")
        if shell and codex_pane.shell_foreground(cmd):
            reason = codex_pane.exit_reason(tmux.capture(name, lines=codex_pane.TAIL_LINES))
            _mark_exited(name, row, reason, invalidate)
            return "agent_exited", reason
        kind = codex_pane.dialog(tmux.capture(name, lines=codex_pane.TAIL_LINES))
    except (tmux.TmuxError, tmux.TmuxDown, OSError):
        return None
    return ("dialog", codex_pane.NOTES[kind]) if kind else None


def _mark_exited(name: str, row: dict, reason: str, invalidate: bool = True) -> None:
    """The row of a Codex whose pane went back to the login shell becomes errored, once, with the reason (and a notice)."""
    if row.get("state") in ("ended", "errored") and row.get("last_message") == reason:
        return
    try:
        db.set_state(name, "errored", "AgentExited", message=reason, attention=True)
        db.add_event(name, "AgentExited", None, reason, {}, agent="codex")
        notify.notify_session(name, "errored", reason, "agent_exited")
    except Exception as e:
        log.warning("marking %s errored after codex left its pane failed: %s", name, e)
    if invalidate:
        _invalidate_scan()


def _typing_refusal(name: str, row: dict, queue: bool = False) -> JSONResponse | None:
    """None when typing into the pane is safe, else the 409: the row's state (_state_refusal), then what a Codex pane shows (_pane_block)."""
    refusal = _state_refusal(name, row, queue)
    if refusal is not None:
        return refusal
    block = _pane_block(name, row)
    return _terminal_refusal(block[0], block[1], row, None) if block else None


def _state_refusal(name: str, row: dict, queue: bool = False) -> JSONResponse | None:
    """None when the row's state allows typing, else the 409. compacting and a permission (pending, or the dialog waiting for its
    answer) always refuse; `working` refuses unless `queue` (Claude queues what is typed during a turn)."""
    flags = row.get("flags") or {}
    state, kind = row.get("state"), flags.get("wait_kind")
    if flags.get("compacting"):
        return _terminal_refusal("compacting", "the session is compacting", row, 15)
    if _permission_pending(name):
        return _terminal_refusal("permission_pending", "a permission request is waiting for an answer", row, 10)
    if state == "working":
        return None if queue else _terminal_refusal("working", "the session is working", row, 5)
    if state == "waiting":
        if kind == "permission":
            return _terminal_refusal("permission", "the session is asking for a permission", row, 10)
        if kind == "elicitation":
            return _terminal_refusal("elicitation", "the session is asking a question in a dialog", row, 10)
        if kind != "idle":
            return _terminal_refusal("waiting", "the session is waiting on something the board cannot name", row, 5)
        return None
    if state == "ended":
        return _terminal_refusal("ended", "the session has ended", row, None)
    if state not in TYPEABLE_STATES:
        shown = _dialog_refusal(name, row)           # a fresh Codex has no state yet; the dialog it waits behind is the reason to show
        return shown or _terminal_refusal("not_ready", "the session has not reported a state yet", row, 3)
    return None


def _dialog_refusal(name: str, row: dict) -> JSONResponse | None:
    """The 409 `dialog` when a Codex pane shows its update or trust dialog (it wins over not_ready: Codex sends no hook, so no state, until
    its first turn, and the dialog blocks that turn), else None. Only the dialog is read here: a login shell in a young pane is the launch line."""
    block = _pane_block(name, row, shell=False)
    return _terminal_refusal(block[0], block[1], row, None) if block else None


def _permission_pending(name: str) -> bool:
    """Is a permission request of this session waiting for its answer? Only one the hook can still be long-polling counts: a row left
    undecided by a restart (its waiter died with the process) would otherwise block the session for good."""
    horizon = _utcnow() - timedelta(seconds=settings.approve_timeout + PERMISSION_GRACE)
    for p in db.perm_pending():
        if p.get("tmux_name") == name:
            at = _parse_at(p.get("created_at"))
            if at is None or at >= horizon:
                return True
    return False


def _clean_line(v, what: str, limit: int) -> str:
    """One line of text for the pane: stripped, at most `limit` characters, no control or line-separator characters."""
    if not isinstance(v, str):
        raise projects.BadRequest(f"{what} must be text")
    s = v.strip()
    if len(s) > limit:
        raise projects.BadRequest(f"{what} is longer than {limit} characters")
    if any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in s):
        raise projects.BadRequest(f"{what} must be one line without control characters")
    return s


def _parse_at(at) -> datetime | None:
    try:
        d = datetime.fromisoformat(at) if isinstance(at, str) else None
    except ValueError:
        return None
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def _stat_snapshot(stats) -> dict:
    """The statusline fields a command can change, as they are when it is typed (the baseline `changed` is judged against)."""
    s = stats if isinstance(stats, dict) else {}
    return {k: s.get(k) for k in ("model", "model_id", "effort", "fast")}


def _cmd_outcome(pending: dict, stats: dict, now: datetime) -> bool | None:
    """Did this statusline confirm the pending command? True confirmed, False unconfirmed (its 20 s are over), None keep waiting.
    model: the statusline's model (display name or id) contains the argument, or differs from the one at send time; effort: it
    equals the argument or differs; fast: it differs; every other command: the next statusline (Claude's UI is alive and redrew)."""
    at = _parse_at(pending.get("at"))
    if at is None or (now - at).total_seconds() > CMD_PENDING_TTL:
        return False
    cmd, arg = pending.get("cmd"), str(pending.get("arg") or "").strip().lower()
    before = pending.get("before") if isinstance(pending.get("before"), dict) else {}
    if cmd == "model":
        hay = [str(stats.get(k) or "").lower() for k in ("model", "model_id")]
        want = arg.replace("[1m]", "").strip()
        known = any(before.get(k) is not None for k in ("model", "model_id"))     # no baseline: only the argument can confirm
        changed = known and any(stats.get(k) != before.get(k) for k in ("model", "model_id"))
        return True if changed or (want and any(want in h for h in hay)) else None
    if cmd == "effort":                                  # a statusline from before the change equals the baseline: no false hit
        eff = stats.get("effort")
        return True if eff != before.get("effort") or (arg and str(eff).lower() == arg) else None
    if cmd == "fast":                                    # an absent key counts as off
        return True if bool(stats.get("fast")) != bool(before.get("fast")) else None
    return True


def _cmd_confirm(handle, name: str, stats) -> None:
    """The statusline hook (registered in hooks.STATS_HOOKS, called with the statusline's stats right after db.set_stats): settles
    the row's pending_cmd into flags.last_cmd = {cmd, arg, at, confirmed} and clears pending_cmd. Never raises into the hook path."""
    try:
        row = handle.open_row(name)
        pending = ((row or {}).get("flags") or {}).get("pending_cmd")
        if not isinstance(pending, dict):
            return
        now = _parse_at(_cmd_now()) or datetime.now(timezone.utc)
        done = _cmd_outcome(pending, stats if isinstance(stats, dict) else {}, now)
        if done is not None:
            handle.update_flags(name, {"last_cmd": {"cmd": pending.get("cmd"), "arg": pending.get("arg"), "at": pending.get("at"),
                                                    "confirmed": done}, "pending_cmd": None})
    except Exception as e:
        log.warning("command confirmation for %s failed: %s", name, e)


def _register_stats_hook() -> None:
    """Append _cmd_confirm to hooks.STATS_HOOKS (hooks.py owns the list and calls every entry as fn(db, name, stats) after the
    statusline's set_stats). Created here when hooks.py has none yet; a re-import replaces the old entry instead of doubling it."""
    lst = getattr(hooks, "STATS_HOOKS", None)
    if lst is None:
        lst = hooks.STATS_HOOKS = []
    lst[:] = [f for f in lst if getattr(f, "__qualname__", "") != "_cmd_confirm"] + [_cmd_confirm]


_register_stats_hook()


def _type_command(name: str, row: dict, spec, arg: str, by: str) -> str:
    """Type one allowlisted slash command into the pane (C-u to clear the composer, then the text and Enter), with flags.pending_cmd set
    first (the statusline that follows confirms it) and a BoardCommand event after. The guards are the caller's: POST /command and the
    usage refresh (POST /api/usage/refresh) both come here. Returns the text typed."""
    sent = f"{spec.cmd} {arg}".strip()
    cmd = spec.cmd.lstrip("/")
    prior = (row.get("flags") or {}).get("pending_cmd")
    patch: dict = {"pending_cmd": {"cmd": cmd, "arg": arg or None, "at": _cmd_now(), "before": _stat_snapshot(row.get("stats"))}}
    if isinstance(prior, dict):                         # an earlier command nobody confirmed: its outcome is 'unconfirmed'
        patch["last_cmd"] = {"cmd": prior.get("cmd"), "arg": prior.get("arg"), "at": prior.get("at"), "confirmed": False}
    db.update_flags(name, patch)                        # before typing: the statusline can answer within a few hundred ms
    try:
        tmux.send_keys(name, ["C-u"])
        tmux.send_text(name, sent, enter=True)
    except Exception:
        db.update_flags(name, {"pending_cmd": None})
        raise
    db.add_event(name, "BoardCommand", cmd, sent, {"cmd": cmd, "arg": arg or None, "by": by}, agent=row.get("agent"))
    _invalidate_scan()
    return sent


class CommandIn(BaseModel):
    cmd: object = None
    arg: object = None
    wait_ms: object = None
    confirm: object = None


@app.post("/api/sessions/{name}/command")
def api_command(name: str, request: Request, body: CommandIn | None = None):
    """Type one of the agent's allowlisted slash commands into its pane ('/model opus'). Never types anything else, and refuses
    (409 {error, state, wait_kind, retry}) unless the composer is free: the session idle, done, errored or idle-waiting, with no
    permission pending and no compaction. Clears the composer first (C-u); the statusline that follows confirms it passively."""
    _terminal_session(name)
    b = body or CommandIn()
    row, adapter, refusal = _agent_row(name)
    if refusal:
        return refusal
    allow = adapter.slash_commands()
    raw = b.cmd.strip() if isinstance(b.cmd, str) else ""
    key = (raw[1:] if raw.startswith("/") else raw).lower()
    spec = allow.get(key)
    if spec is None:
        raise projects.BadRequest("unknown command; allowed: " + ", ".join(dict.fromkeys(s.cmd for s in allow.values())))
    if getattr(spec, "drive", "inline") == "restart":    # issue #2: no live command takes it; the session restarts with the new launch flag
        raise projects.BadRequest(f"{spec.label.lower()} is a launch flag in this agent: change it with POST /api/sessions/{name}/restart")
    if getattr(spec, "drive", "inline") != "inline":     # v0.5.21: a picker is driven by POST /tune, never typed with an argument
        raise projects.BadRequest(f"{spec.cmd} opens a picker in this agent: set {spec.label.lower()} with POST /api/sessions/{name}/tune")
    arg = _clean_line(b.arg, "arg", CMD_ARG_MAX) if b.arg is not None else ""
    if spec.arg and not arg:
        raise projects.BadRequest(f"{spec.cmd} needs an argument")
    if not spec.arg and arg:
        raise projects.BadRequest(f"{spec.cmd} takes no argument")
    if getattr(spec, "choices", None) and arg.lower() not in spec.choices:
        raise projects.BadRequest(f"{spec.cmd} takes one of: {', '.join(spec.choices)}")
    arg = arg.lower() if getattr(spec, "choices", None) else arg
    wait = CMD_WAIT_DEFAULT if b.wait_ms is None else b.wait_ms
    if isinstance(wait, bool) or not isinstance(wait, int) or not 0 <= wait <= CMD_WAIT_MAX:
        raise projects.BadRequest(f"wait_ms must be an integer from 0 to {CMD_WAIT_MAX}")
    if b.confirm is not None and not isinstance(b.confirm, bool):
        raise projects.BadRequest("confirm must be true or false")
    refusal = _typing_refusal(name, row)
    if refusal:
        return refusal
    if spec.destructive and b.confirm is not True:
        return JSONResponse({"error": "confirm", "message": f"{spec.cmd} cannot be undone: send it again with confirm", "cmd": spec.cmd,
                             "state": row.get("state"), "wait_kind": (row.get("flags") or {}).get("wait_kind"), "retry": None},
                            status_code=409)

    sent = _type_command(name, row, spec, arg, request.state.user)
    out = {"ok": True, "sent": sent, "verified": spec.verified, "saves_default": bool(getattr(spec, "saves_default", False) and arg
                                                                                       and not arg.startswith("ultracode"))}
    if spec.read:
        time.sleep(wait / 1000)
        out["screen"] = tmux.capture(name, lines=40)
        out["dialog"] = bool(getattr(spec, "dialog", False))     # True: the output sits in a dialog one Escape closes
    return out


# ---------- v0.5.21: session-only tuning through the agent's own pickers (app/agents/pickers.py) ----------

class TuneIn(BaseModel):
    setting: object = None
    value: object = None


# Codex's reasoning and permissions stay accepted here for API compatibility, but the Tune no longer sends them (issue #2): their key paths were never
# recorded on the box (the cursor of /model's second step and of /permissions), so they answer verified false; the Tune restarts the session instead (POST /restart).
TUNE_SETTINGS = {"claude": ("effort", "ultracode"), "codex": ("model", "reasoning", "permissions")}
TUNE_PANE_ONLY = ("ultracode", "permissions")          # settings no statusline / rollout field reports: the pane read is the only answer


def _tune_plan(agent_name: str, adapter, setting: str, value: str, stats: dict):
    from .agents import pickers
    if agent_name == "claude":
        if setting == "effort":
            return pickers.claude_effort(value, stats.get("effort"))
        return pickers.claude_ultracode(value)
    models = adapter.models(fetch=False)
    if setting == "model":
        return pickers.codex_model(value, models, stats.get("model"), stats.get("effort"))
    if setting == "reasoning":
        return pickers.codex_reasoning(value, models, stats.get("model"), stats.get("effort"))
    return pickers.codex_permissions(value)


@app.post("/api/sessions/{name}/tune")
def api_tune(name: str, request: Request, body: TuneIn | None = None):
    """Change one setting of a running session FOR THIS SESSION ONLY through the agent's own picker: Claude's effort (bare /effort,
    Left/Right, `s`) and ultracode (/effort ultracode on|off); Codex's model, reasoning (the /model picker, `s`) and permissions (the
    /permissions picker, Ask for approval or Approve for me; Full Access is never a target). Keys come from the fixed tables of
    app/agents/pickers.py, with the guards of /command (409 unless the composer is free; one drive per session at a time). A picker
    that does not look as the box check showed is backed out of with Escape and nothing is chosen (409 picker). The answer says
    whether the pane showed the change: confirmed true (read back), false (the pane shows another value), null (not shown yet: a
    statusline or the rollout may still confirm it through flags.pending_cmd). `verified` = the box check ran this exact path."""
    from .agents import pickers
    _terminal_session(name)
    b = body or TuneIn()
    row, adapter, refusal = _agent_row(name)
    if refusal:
        return refusal
    agent_name = row.get("agent") or "claude"
    setting = b.setting.strip().lower() if isinstance(b.setting, str) else ""
    allowed = TUNE_SETTINGS.get(agent_name, ())
    if setting not in allowed:
        raise projects.BadRequest(f"setting must be one of {', '.join(allowed) or '(none for this agent)'}")
    value = _clean_line(b.value, "value", 80).lower() if b.value is not None else ""
    if not value:
        raise projects.BadRequest("value is required")
    stats = row.get("stats") if isinstance(row.get("stats"), dict) else {}
    try:
        plan = _tune_plan(agent_name, adapter, setting, value, stats)
    except pickers.Refused as e:
        return _terminal_refusal("cannot_place", str(e), row, None, setting=setting)
    refusal = _typing_refusal(name, row)
    if refusal:
        return refusal
    lock = pickers.session_lock(name)
    if not lock.acquire(blocking=False):
        return _terminal_refusal("busy", "another change is being typed into this session", row, 3, setting=setting)
    try:
        prior = (row.get("flags") or {}).get("pending_cmd")
        pend = {"cmd": plan.cmd, "arg": plan.arg, "at": _cmd_now(), "before": {**_stat_snapshot(stats),
                **{k: stats.get(k) for k in ("approval", "sandbox")}}, "via": "tune"}
        patch: dict = {"pending_cmd": pend}
        if isinstance(prior, dict):
            patch["last_cmd"] = {"cmd": prior.get("cmd"), "arg": prior.get("arg"), "at": prior.get("at"), "confirmed": False}
        db.update_flags(name, patch)
        io = pickers.IO(send_keys=lambda keys: tmux.send_keys(name, keys), send_text=lambda t, enter: tmux.send_text(name, t, enter=enter),
                        capture=lambda: tmux.capture(name, lines=60), sleep=_tune_sleep)
        try:
            screen = pickers.drive(plan, io)
        except pickers.PickerError as e:
            db.update_flags(name, {"pending_cmd": None})
            return _terminal_refusal("picker", str(e), row, None, setting=setting)
        except Exception:
            db.update_flags(name, {"pending_cmd": None})
            raise
    finally:
        lock.release()
    confirmed = plan.confirm(screen) if plan.confirm else None
    observed = plan.observe(screen) if plan.observe else None
    if confirmed is None and plan.cmd in TUNE_PANE_ONLY:
        confirmed = False                              # only the pane can show these: no statusline or rollout field would ever settle it
    if confirmed is not None:                          # read back from the pane: settled now, not by the next statusline
        settle = {"last_cmd": {"cmd": plan.cmd, "arg": plan.arg, "at": pend["at"], "confirmed": confirmed}, "pending_cmd": None}
        if confirmed:
            settle["tuned"] = {**(((row.get("flags") or {}).get("tuned")) or {}), setting: {"value": plan.value, "at": pend["at"]}}
        db.update_flags(name, settle)
    db.add_event(name, "BoardCommand", plan.cmd, f"tune {setting} {plan.value}",
                 {"cmd": plan.cmd, "arg": plan.arg, "by": request.state.user, "via": "picker", "confirmed": confirmed},
                 agent=row.get("agent"))
    _invalidate_scan()
    out = {"ok": True, "setting": setting, "value": plan.value, "confirmed": confirmed, "observed": observed, "verified": plan.verified,
           "session_only": True}
    if confirmed is False:
        out["message"] = (f"{setting} not applied: the session shows {observed}" if observed else
                          f"{setting} {plan.value} not confirmed: the session did not show the change")
    elif confirmed is None:
        out["message"] = f"{setting} {plan.value} sent; waiting for the session to show it"
    return out


# ---------- issue #2: Codex reasoning, approvals and sandbox change by restarting with the conversation resumed ----------

class RestartIn(BaseModel):
    reasoning: object = None
    approval: object = None
    sandbox: object = None
    preview: object = None


@app.post("/api/sessions/{name}/restart")
def api_restart(name: str, request: Request, body: RestartIn | None = None):
    """Restart a running Codex session with another reasoning level (-c model_reasoning_effort), approval policy (-a on-request | never)
    or sandbox (-s read-only | workspace-write), the same conversation resumed (`codex resume <thread> <flags>`), in the same tmux name. No
    live Codex command takes these (box check V8-Codex), and the picker key paths for them were never recorded, so a restart is the
    path the box has run. The line is built by the adapter (CodexAgent.restart_opts + resume_argv): the stored launch options, the model and
    reasoning the statusline shows now, then the change. `preview: true` answers {cmd, from, to} and touches nothing. 400 for a value
    that is not offered (`untrusted`, `on-failure`, `danger-full-access` never are), for a session that is not Codex, a task's or one
    started without approvals and sandbox (a bypass is never carried over); 409 when the session has no conversation id yet, is not at
    its prompt (a restart ends a running turn) or another change is being made. The old process is closed before the new one starts
    (a thread open twice makes Codex warn), so the terminal page shows 'exited' until it is reloaded."""
    require_real_launch_ok()   # issue #100: a restart starts Codex again, so a dev board that could reach the real home refuses (409)
    from .agents import pickers
    _terminal_session(name)
    b = body or RestartIn()
    row, adapter, refusal = _agent_row(name)
    if refusal:
        return refusal
    if (row.get("agent") or "claude") != "codex":
        raise projects.BadRequest("only a Codex session restarts with other launch flags (Claude changes these live)")
    if row.get("launcher") == "task":
        raise projects.BadRequest("a task's session keeps the permissions the task was started with")
    changes = {}
    for key in ("reasoning", "approval", "sandbox"):
        v = getattr(b, key)
        if v not in (None, ""):
            changes[key] = _clean_line(v, key, 40).lower()
    if b.preview is not None and not isinstance(b.preview, bool):
        raise projects.BadRequest("preview must be true or false")
    if not changes:
        raise projects.BadRequest("nothing to change: send reasoning, approval or sandbox")
    sid = str(row.get("agent_session_id") or row.get("claude_session_id") or "").strip()
    if not sid:
        return _dialog_refusal(name, row) or _terminal_refusal(
            "not_ready", "the session has not reported its conversation yet: send it one message, then restart it", row, 3)
    now_perm = adapter.permissions_of(row.get("opts"), row.get("cmd"))
    if now_perm and now_perm.get("bypass"):
        return _terminal_refusal("bypass", "this session runs without approvals and without the sandbox; a restart would not carry that over: "
                                 "start a new session from the launcher instead", row, None)
    add_dirs = [d for d in (row.get("add_dirs") or []) if isinstance(d, str)]
    opts = adapter.restart_opts(row.get("opts") if isinstance(row.get("opts"), dict) else {}, row.get("stats"), **changes)
    argv = adapter.resume_argv(sid, opts=opts, add_dirs=add_dirs)
    cmd_line = shlex.join(argv)
    after = adapter.permissions_of(opts)
    if b.preview is True:
        return {"ok": True, "preview": True, "cmd": cmd_line, "changes": changes, "from": now_perm, "to": after}
    refusal = _typing_refusal(name, row)
    if refusal:
        return refusal
    lock = pickers.session_lock(name)
    if not lock.acquire(blocking=False):
        return _terminal_refusal("busy", "another change is being made to this session", row, 3)
    try:
        project, repo, session = row["project"], row["repo"], row["name"]
        rpath = projects.repo_path(project, repo)
        cwd = row.get("cwd") if row.get("cwd") and Path(str(row.get("cwd"))).is_dir() else str(rpath)
        keep_flags = {k: True for k in (recover.OPT_OUT_FLAG,) if (row.get("flags") or {}).get(k)}
        if not _end_session(name, "killed"):
            raise projects.NotFound(f"session {name} not found")
        _restart_pause(RESTART_PAUSE)                        # tmux returns when the session is gone, not when Codex has released the thread
        try:
            real = _start_session(name, project, repo, session, "recovered", str(cwd), cmd_line=cmd_line, claude_session_id=sid,
                                  add_dirs=add_dirs, agent="codex", opts=opts or None)
        except Exception as e:
            log.warning("restart of %s failed after the old process was closed: %s", name, e)
            return JSONResponse({"error": "restart_failed", "message": f"the old session was closed but the new one did not start ({e}); "
                                 f"resume it from the launcher or run: {cmd_line}", "cmd": cmd_line}, status_code=500)
    finally:
        lock.release()
    if keep_flags:
        db.update_flags(real, keep_flags)                    # the person's auto-continue opt-out outlives the restart, as it does a reboot
    db.add_event(real, "BoardCommand", "restart", "restart " + " ".join(f"{k}={v}" for k, v in changes.items()),
                 {"cmd": "restart", "arg": changes, "by": request.state.user, "via": "restart"}, agent="codex")
    _invalidate_scan()
    return {"ok": True, "tmux": real, "cmd": cmd_line, "agent_session_id": sid, "changes": changes, "from": now_perm, "to": after,
            "attach_url": f"/tty/?arg={real}"}


# ---------- #84: the per-session writable flags (an allowlist, one entry for now) ----------

class FlagsIn(BaseModel):
    model_config = {"extra": "allow"}          # an unknown key must answer 400 with the allowlist, not be dropped
    no_autoresume: object = None


SESSION_FLAGS = ("no_autoresume",)             # the flags a person may write: flags.no_autoresume opts one session out of auto-continue


@app.post("/api/sessions/{name}/flags")
def api_session_flags(name: str, request: Request, body: FlagsIn | None = None):
    """Set or clear a per-session switch on the session's open row. Only `no_autoresume` (bool): true stops the board typing `continue`
    into this session after a limit reset, after an account switch and after a reboot; false clears the key. The flag lives in the row
    (sessions.flags), so it survives a board restart, and reboot recovery carries it to the relaunched row. 400 for a name that is not a
    ccboard session, an internal session, an unknown flag or a non-boolean value; 404 without an open row. Answers {ok, flags: {no_autoresume}}
    read back from the row, which is what the UI counts as applied."""
    _check_terminal_name(name)
    if tmux.is_internal(name):
        raise projects.BadRequest("internal sessions have no switches")
    b = body or FlagsIn()
    given = dict(b.model_extra or {})
    if "no_autoresume" in b.model_fields_set:
        given["no_autoresume"] = b.no_autoresume
    if not given:
        raise projects.BadRequest("body must set one of: " + ", ".join(SESSION_FLAGS))
    bad = sorted(k for k in given if k not in SESSION_FLAGS)
    if bad:
        raise projects.BadRequest(f"cannot set {', '.join(bad[:3])}: the writable flags are {', '.join(SESSION_FLAGS)}")
    if any(not isinstance(v, bool) for v in given.values()):
        raise projects.BadRequest("no_autoresume must be true or false")
    if db.open_row(name) is None:
        raise projects.NotFound(f"no open row for session {name}")
    off = given["no_autoresume"]
    flags = db.update_flags(name, {"no_autoresume": True if off else None})
    db.add_event(name, "AutoContinue", "optout" if off else "optin",
                 "auto-continue off for this session" if off else "auto-continue on for this session", {"by": request.state.user})
    _invalidate_scan()
    return {"ok": True, "flags": {"no_autoresume": bool(flags.get("no_autoresume"))}, "auto_continue": settings.auto_continue}


RESTART_PAUSE = 1.0     # s between closing the old Codex and starting `codex resume` (a thread open twice makes Codex ask, box check #92)


def _restart_pause(seconds: float) -> None:
    """Tests replace it."""
    time.sleep(seconds)


def _tune_sleep(seconds: float) -> None:
    """The picker driver's pause between keys (tests replace it)."""
    time.sleep(seconds)


class PromptIn(BaseModel):
    text: object = None
    enter: object = True
    queue: object = False


@app.post("/api/sessions/{name}/prompt")
def api_prompt(name: str, request: Request, body: PromptIn | None = None):
    """Paste a prompt into the agent's composer (bracketed paste, so newlines stay in the prompt) and press Enter. Same guards as
    /command, except that a working session accepts it with queue=true (Claude queues text typed during a turn). last_prompt is
    not set here: the UserPromptSubmit hook sets it, which also proves the paste landed."""
    _terminal_session(name)
    b = body or PromptIn()
    row, _, refusal = _agent_row(name)
    if refusal:
        return refusal
    if not isinstance(b.text, str):
        raise projects.BadRequest("text must be text")
    text = b.text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise projects.BadRequest("text is empty")
    if len(text) > PROMPT_MAX:
        raise projects.BadRequest(f"text is longer than {PROMPT_MAX} characters")
    if any(unicodedata.category(c) == "Cc" and c not in "\t\n" for c in text):
        raise projects.BadRequest("text contains control characters")
    if not isinstance(b.enter, bool) or not isinstance(b.queue, bool):
        raise projects.BadRequest("enter and queue must be true or false")
    refusal = _typing_refusal(name, row, queue=b.queue)
    if refusal:
        return refusal
    queued = row.get("state") == "working"
    tmux.paste_text(name, text, enter=b.enter)
    db.add_event(name, "BoardPrompt", None, text[:200], {"chars": len(text), "enter": b.enter, "queued": queued,
                                                         "by": request.state.user}, agent=row.get("agent"))
    if row.get("agent") == "codex" and b.enter and not (row.get("flags") or {}).get("turn_seen_at"):
        db.update_flags(name, {"turn_seen_at": db_now()})   # #96: the first turn the board knows of arms hooks_missing
    _invalidate_scan()
    return {"ok": True, "pasted": True, "queued": queued}


@app.delete("/api/sessions/{name}")
def api_kill_session(name: str):
    try:
        tmux.split_name(name)
    except ValueError:
        raise projects.BadRequest("not a ccboard session name")
    if not _end_session(name, "killed"):
        raise projects.NotFound(f"session {name} not found")
    return {"killed": name}


# ---------- live last-lines stream (SSE) ----------

LIVE_LINES = 12                  # default tail length per session (?lines= overrides, 1..LIVE_MAX_LINES)
LIVE_MAX_LINES = 40
LIVE_MAX_NAMES = 20              # ?names= is a comma list of at most this many session names
LIVE_INTERVAL = 2.0


def _stream_query(names: str | None = None, lines: str | None = None) -> tuple[set[str] | None, int]:
    """GET /api/stream?names=a--b--c,d--e--f&lines=12 -> (names filter or None for every session, tail length).

    A FastAPI dependency, not code inside the event generator: an exception raised after the response has started is a broken
    stream, while one raised here is the plain 400 the client can read. Names are ccboard session names (tmux.split_name), at most
    LIVE_MAX_NAMES distinct ones; lines is an integer in 1..LIVE_MAX_LINES (default LIVE_LINES)."""
    wanted: set[str] | None = None
    if names is not None:
        wanted = set()
        for raw in names.split(","):
            name = raw.strip()
            try:
                tmux.split_name(name)
            except ValueError:
                raise projects.BadRequest(f"names: {name!r} is not a ccboard session name")
            wanted.add(name)
        if len(wanted) > LIVE_MAX_NAMES:
            raise projects.BadRequest(f"names: at most {LIVE_MAX_NAMES} sessions per stream")
    n = LIVE_LINES
    if lines is not None:
        try:
            n = int(lines.strip())
        except ValueError:
            raise projects.BadRequest("lines must be an integer")
        if not 1 <= n <= LIVE_MAX_LINES:
            raise projects.BadRequest(f"lines must be between 1 and {LIVE_MAX_LINES}")
    return wanted, n


def _capture_all(names: set[str] | None = None, nlines: int = LIVE_LINES) -> tuple[list[str], dict[str, list[str]]]:
    """(every non-internal session name, name -> last nlines visible lines). Only the sessions in `names` (all when None) are
    captured: one tmux capture-pane each is the expensive part, the listing is one call."""
    try:
        live_names = sorted(n for n in tmux.list_sessions() if not tmux.is_internal(n))
    except tmux.TmuxDown:
        return [], {}
    out: dict[str, list[str]] = {}
    for n in live_names:
        if names is not None and n not in names:
            continue
        try:
            text = tmux.capture(n, lines=nlines, join=False)
        except tmux.TmuxError:
            continue
        tail = [ln.rstrip() for ln in text.splitlines()]
        while tail and not tail[-1]:
            tail.pop()
        out[n] = tail[-nlines:]
    return live_names, out


@app.get("/api/stream", response_class=EventSourceResponse)
async def api_stream(request: Request, query=Depends(_stream_query), once: bool = False):
    """SSE: 'lines' events {name, lines} per session whenever its visible tail changes, a 'tick' {sessions} every interval.
    ?names=a,b (at most 20 session names) limits the 'lines' events and the captures to those sessions; ?lines=N (1..40, default 12)
    sets the tail length. The tick always lists every live session, filtered or not. Bad names or lines are a 400 before the stream
    starts. FastAPI encodes the yielded ServerSentEvent objects (response_class=EventSourceResponse)."""
    names, nlines = query
    last: dict[str, list[str]] = {}
    while True:
        live_names, snap = await asyncio.to_thread(_capture_all, names, nlines)
        for name, tail in snap.items():
            if last.get(name) != tail:
                last[name] = tail
                yield ServerSentEvent(event="lines", data={"name": name, "lines": tail})
        for gone in [n for n in last if n not in snap]:
            del last[gone]
        yield ServerSentEvent(event="tick", data={"sessions": live_names})
        if once or await request.is_disconnected():
            return
        await asyncio.sleep(LIVE_INTERVAL)


# ---------- hooks ----------

@app.post("/api/hook")
async def api_hook(request: Request):
    body = await request.body()
    if len(body) > hooks.MAX_BODY:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    event = request.headers.get("x-ccboard-event") or payload.get("hook_event_name") or "unknown"

    def work():
        rows = db.open_rows()
        name, how = hooks.resolve_session(request.headers, payload, rows)
        if not name or name not in rows:
            return {"ignored": how if not name else "unknown session", "session": name}
        try:
            if hooks.agent_mismatch(rows[name].get("agent"), request.headers.get("x-ccboard-agent")):
                return {"ignored": "foreign", "session": name, "how": how}     # another agent's process: nothing is written
            result = hooks.apply(db, name, event, payload, agent=hooks.hook_agent(rows[name].get("agent"), request.headers.get("x-ccboard-agent")),
                                 child=request.headers.get("x-ccboard-child", "").strip() == "1")
        except Exception as e:  # a malformed payload must never 500 the hook path
            log.warning("hook %s for %s failed: %s", event, name, e)
            return {"ignored": "error", "session": name, "error": str(e)[:200]}
        _invalidate_scan()
        return {**result, "how": how}

    return await asyncio.to_thread(work)


class SubIn(BaseModel):
    subscription: dict


@app.get("/api/push/vapid")
def api_push_vapid():
    try:
        return {"key": push.ensure_keys()}
    except Exception as e:
        raise projects.BadRequest(f"web push unavailable: {e}")


@app.post("/api/push/subscribe")
def api_push_subscribe(body: SubIn):
    if not push.valid_subscription(body.subscription):
        raise projects.BadRequest("invalid push subscription")
    db.push_sub_add(body.subscription)
    return {"ok": True, "count": len(db.push_subs())}


@app.delete("/api/push/subscribe")
def api_push_unsubscribe(body: SubIn):
    if isinstance(body.subscription, dict) and isinstance(body.subscription.get("endpoint"), str):
        db.push_sub_del(body.subscription["endpoint"])
    return {"ok": True, "count": len(db.push_subs())}


@app.post("/api/push/test")
def api_push_test():
    # two buttons, so the phone shows whether this device does notification actions (iOS ignores them); neither names a session
    n = push.send_all(db, "ccboard test", "Web Push works. Two buttons under this text mean this device shows notification actions.", "/", tag="test",
                      extra={"agent": "claude", "state": "done", "tmux": "", "perm_id": None, "ts": int(time.time() * 1000)},
                      actions=[{"action": "terminal", "title": "Open board"}, {"action": "ack", "title": "Dismiss"}], renotify=True)
    return {"sent": n, "subscriptions": len(db.push_subs())}


class NotifyPrefsIn(BaseModel):
    needs: bool | None = None       # needs-you: permission prompts, questions, idle
    done: bool | None = None
    limit: bool | None = None       # rate limit hit
    error: bool | None = None       # error / crash
    login: bool | None = None       # a login that no longer works


@app.get("/api/notify/prefs")
def api_notify_prefs():
    """{prefs: {needs, done, limit, error, login}, samples: {<same keys>: {title, body, priority, buttons}}}: the Settings toggles and the
    example notice behind each, built by the code that builds the real ones."""
    return {"prefs": notify.prefs(), "samples": notify.samples()}


@app.put("/api/notify/prefs")
def api_notify_prefs_set(body: NotifyPrefsIn):
    return {"prefs": notify.set_prefs(body.model_dump(exclude_none=True)), "samples": notify.samples()}


@app.post("/api/notify/test")
def api_notify_test():
    if not notify.enabled():
        raise projects.BadRequest("ntfy is not configured (NTFY_URL is empty)")
    ok = notify.publish("ccboard test", "Notifications work.", click=(settings.public_url or None), tags=["tada"])
    return {"ok": ok}


# ---------- MCP from other devices (issue #13): the /mcp endpoint and its device tokens (app/mcp_remote.py, app/mcp_tokens.py) ----------

@app.post(mcp_remote.PATH)
async def mcp_endpoint(request: Request):
    """Reached only through auth_middleware's /mcp gate (on, identity, a live device token, POST, a known protocol version)."""
    return await mcp_remote.endpoint(request)


class McpTokenIn(BaseModel):
    name: str = ""
    scopes: list[str] | None = None
    expires_days: int | None = None


class McpRemoteIn(BaseModel):
    enabled: bool


def _mcp_manager(request: Request) -> None:
    """Device tokens are managed by a person in Settings: the hook token (local automation) and the hub may not mint or revoke them."""
    if getattr(request.state, "user", None) in ("local-token", "hub"):
        raise projects.Forbidden("device tokens are managed from Settings > Agents")


def _mcp_view(changed: bool = False) -> dict:
    if changed:
        doctor.invalidate()                      # the doctor's mcp-remote check reads the switch and the tokens: no 20 s old answer after a change
    return {"enabled": mcp_tokens.enabled(), "env_default": mcp_tokens.env_default(), "tokens": mcp_tokens.listing(),
            "recent": mcp_tokens.recent(), "max_tokens": mcp_tokens.MAX_TOKENS, "scopes": list(mcp.SCOPES),
            "default_scopes": list(mcp.DEFAULT_SCOPES), "default_days": mcp_tokens.DEFAULT_DAYS, "path": mcp_remote.PATH}


def _mcp_refused(e: "mcp_tokens.Refused") -> JSONResponse:
    return JSONResponse({"error": str(e)}, status_code=e.status)


@app.get("/api/mcp/tokens")
def api_mcp_tokens(request: Request):
    """{enabled, env_default, tokens: [{id, name, scopes, created_at, last_used_at, last_user, last_tool, expires_at, expired}], recent: the
    last tool calls (token, tool, task id, outcome), limits}. Never a token or a digest."""
    _mcp_manager(request)
    return JSONResponse(_mcp_view(), headers={"Cache-Control": "no-store"})


@app.post("/api/mcp/tokens", status_code=201)
def api_mcp_token_mint(body: McpTokenIn, request: Request):
    """Mint a device token: {token (the only time it is ever shown), record, ...the listing}. 400 bad name, scope or expiry; 409 a
    duplicate name or the 10-token limit."""
    _mcp_manager(request)
    try:
        token, record = mcp_tokens.mint(body.name, body.scopes, body.expires_days)
    except mcp_tokens.Refused as e:
        return _mcp_refused(e)
    return JSONResponse({**_mcp_view(True), "token": token, "record": record}, status_code=201, headers={"Cache-Control": "no-store"})


@app.delete("/api/mcp/tokens/{token_id}")
def api_mcp_token_revoke(token_id: str, request: Request):
    """Revoke: the token fails on its very next request. 404 when there is no such token."""
    _mcp_manager(request)
    if not mcp_tokens.revoke(token_id):
        raise projects.NotFound("no such device token")
    return JSONResponse(_mcp_view(True), headers={"Cache-Control": "no-store"})


@app.put("/api/mcp/remote")
def api_mcp_remote(body: McpRemoteIn, request: Request):
    """Turn the /mcp endpoint on or off (stored; wins over CCBOARD_MCP_REMOTE). Tokens stay when it is turned off."""
    _mcp_manager(request)
    try:
        mcp_tokens.set_enabled(body.enabled)
    except mcp_tokens.Refused as e:
        return _mcp_refused(e)
    return JSONResponse(_mcp_view(True), headers={"Cache-Control": "no-store"})


@app.post("/api/deploy/gate")
async def api_deploy_gate(request: Request):
    """Watchtower's pre-update hook (scripts/ccboard-deploy-gate, hook token): {hold, reasons, since, until}. hold = someone is at a
    terminal, a permission is pending or a clone runs, for at most deploy.HOLD_MAX since the first ask and never after Install now."""
    if not hooks.check_token(request.headers.get(hooks.TOKEN_HEADER)):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    def work():
        try:
            viewers = tmux.viewers()
        except tmux.TmuxError:
            viewers = {}
        return deploy.decide(db, viewers=viewers, pending=db.perm_pending(), clones=clonequeue.status())
    return await asyncio.to_thread(work)


@app.post("/api/deploy/now")
def api_deploy_now():
    """Install the waiting update at Watchtower's next ask (within its scan interval), whatever is open."""
    deploy.force(db)
    _invalidate_scan()
    return {"ok": True, "deploy": deploy.view(db)}


@app.post("/api/recovery/dismiss")
def api_recovery_dismiss():
    db.kv_del("last_recovery")
    _invalidate_scan()
    return {"ok": True}


@app.post("/api/backup/run", status_code=202)
def api_backup_run():
    """Start a backup pass now (same code as the nightly timer), detached from the request."""
    if backup.running():
        raise HTTPException(409, "a backup is already running")
    via = backup.start_detached()
    return {"started": True, "via": via,
            "log": plat.hint("logs", "ccboard-backup", sudo=False) if via == "systemd"
            else str(plat.macos_log_dir() / "backup.log") if via == "launchd" else str(settings.data_dir / backup.LOG_FILE)}


def _node_summary() -> dict:
    sessions, down = _merged_sessions()
    attention = sum(1 for s in sessions.values() if s.get("needs_attention"))
    return {"node": settings.node_name or socket.gethostname(), "health": health.snapshot(), "tmux_down": down,
            "sessions": len(sessions), "attention": attention, "tasks": len(db.tasks()),
            "usage": (db.kv_get("rate_limits") or {}).get("value"), "backup": health.backup_status(),
            "url": settings.public_url or None}


@app.get("/api/health")
def api_health():
    return _node_summary()


@app.get("/api/node/summary")
def api_node_summary():
    return _node_summary()


@app.get("/api/node/hello")
def api_node_hello(request: Request):
    """The probe another board sends before it asks anything else (issue #133). No identity: exactly {app, api, node_id}, nothing else (not the
    version, not the name). 30 per minute per source address (nodes.caller_addr: X-Forwarded-For counts only from a loopback connection);
    over that, 429 with Retry-After."""
    ok, wait = nodes.hello_limiter.allow(nodes.caller_addr(request.client.host if request.client else None, request.headers.get("x-forwarded-for")))
    if not ok:
        return JSONResponse({"error": "too many requests"}, status_code=429, headers={"Retry-After": str(wait), "Cache-Control": "no-store"})
    return JSONResponse(nodes.hello(), headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/api/node")
def api_node():
    """This board's node card (issue #133): who it is, what it runs on, its agents and accounts with their windows, how loaded it is and how many
    lanes are free. Unreadable parts are null. A paired node's token with scope `read` will open it too (issue #135)."""
    sessions, down = _merged_sessions()
    return nodes.card(db, health_snap=health.snapshot(consumer="node"), sessions=None if down else sessions.values())


@app.get("/api/nodes/discover")
def api_nodes_discover(request: Request, refresh: int = 0):
    """The devices of the tailnet that may be ccboard nodes, with the probe result of each (issue #134): {at, tailscale: {ok, reason, variant}, rows}.
    Always 200: Tailscale missing or logged out is `ok: false` with a reason and no rows. Without `refresh` nothing leaves the board. `refresh=1` probes
    the online candidates (GET /api/node/hello, tailnet addresses only, at most four at once); because that makes the board send requests, it needs the
    X-CCBoard header like a change does. A device shared in from another user is never listed, and a probe gives the device no access."""
    if refresh and request.headers.get("x-ccboard") != "1":
        return JSONResponse({"error": "missing X-CCBoard header"}, status_code=403)
    return nodes_discovery.discover(db, refresh=bool(refresh))


# ---------- pairing two boards (issue #135): codes, the pair endpoint, the paired lists, rotate and remove; the logic is in app/nodes.py ----------

def _node_manager(request: Request) -> None:
    """Pairing is managed by a person in Settings: the hook token (local automation), the hub and a node token may not make codes, list pairs or remove them."""
    user = getattr(request.state, "user", None)
    if user in ("local-token", "hub") or (isinstance(user, str) and user.startswith("node:")):
        raise projects.Forbidden("pairing is managed from Settings > Nodes by a signed-in person")


class PairCodeIn(BaseModel):
    model_config = {"extra": "forbid"}          # no permission or sandbox field is ever read here
    scopes: list[str] | None = None
    minutes: int = 10


class NodeAddIn(BaseModel):
    model_config = {"extra": "forbid"}
    url: str
    code: str
    handle: str | None = None
    both_ways: bool = False


def _pair_failed(e: "nodes.PairError") -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if e.status == 429:
        headers["Retry-After"] = str(getattr(e, "retry_after", None) or 60)
    return JSONResponse(e.payload(), status_code=e.status, headers=headers)


@app.post("/api/nodes/pair-code")
def api_nodes_pair_code(body: PairCodeIn, request: Request):
    """Make this board's pairing code (the accepting side): {code (the only time it is shown), expires_at, scopes}. Scopes default to read and tasks; minutes are
    1 to 30 (default 10). One code at a time: a new one replaces the old. The scopes are what the board that uses the code may do here."""
    _node_manager(request)
    try:
        made = nodes.create_code(body.scopes, body.minutes, db=db)           # scopes None = read and tasks; a bad scope or minutes is a ValueError
    except ValueError as e:
        raise projects.BadRequest(str(e)) from None
    return JSONResponse({"code": made["code"], "expires_at": made["expires_at"], "scopes": made["scopes"], "minutes": body.minutes},
                        headers={"Cache-Control": "no-store"})


@app.delete("/api/nodes/pair-code")
def api_nodes_pair_code_cancel(request: Request):
    """Cancel the pairing code, if there is one. Always {cancelled: bool}."""
    _node_manager(request)
    return JSONResponse({"cancelled": bool(nodes.cancel_code(db=db))}, headers={"Cache-Control": "no-store"})


async def _read_capped(request: Request, limit: int) -> bytes | None:
    """The request body, or None once it is over `limit` bytes (the rest is never read)."""
    try:
        if int(request.headers.get("content-length") or 0) > limit:
            return None
    except ValueError:
        return None
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            return None
    return bytes(buf)


@app.post("/api/nodes/pair")
async def api_nodes_pair(request: Request):
    """The other board's call with a code (issue #135). No identity is needed (a tagged board has none), so before anything else is read or changed: it must
    send X-CCBoard: 1, must not send an Origin header, and an identity header, when there is one, must be an allowed login. The body is capped at 16 KB and
    only `code`, `node` and `reverse` are accepted (any other key, a permission or sandbox field included, is a 400). Everything after that (the rate limit per source address, the code, the callback) is nodes.handle_pair:
    nothing is stored or called before the code has been checked. Answers once {token, scopes, node, expires_at}."""
    if request.headers.get("x-ccboard") != "1":
        return JSONResponse({"error": "missing X-CCBoard header"}, status_code=403)
    if request.headers.get("origin") is not None:
        return JSONResponse({"error": "requests from a web page (an Origin header) are refused"}, status_code=403)
    if request.headers.get("tailscale-user-login") is not None and identify(request.headers, settings) is None:
        return JSONResponse({"error": "no Tailscale identity or not in CCBOARD_ALLOWED_USERS"}, status_code=403)
    raw = await _read_capped(request, PAIR_BODY_MAX)
    if raw is None:
        return JSONResponse({"error": f"the body is over {PAIR_BODY_MAX // 1024} KB"}, status_code=413)
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = None
    caller = nodes.caller_addr(request.client.host if request.client else None, request.headers.get("x-forwarded-for"))
    try:
        out = await asyncio.to_thread(nodes.handle_pair, body, caller, db=db)
    except nodes.PairError as e:
        return _pair_failed(e)
    doctor.invalidate()
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


PAIR_CONFIRM_MAX = 1024


@app.post("/api/nodes/pair/confirm")
async def api_nodes_pair_confirm(request: Request):
    """The callback of the board this one is pairing with (issue #135): body {proof}, a digest of the code and a nonce that only this board's own
    add_node (in flight right now, 30 seconds at most) and the board it called know. Answers {node_id} while that add_node runs, else 404, the same
    404 for an unknown and a stale proof. The code, the nonce and every token never travel in this call. No identity is needed (a tagged board has none);
    X-CCBoard: 1 is required, an Origin header is refused, the body is capped at 1 KB and a source address gets 30 calls a minute."""
    if request.headers.get("x-ccboard") != "1":
        return JSONResponse({"error": "missing X-CCBoard header"}, status_code=403)
    if request.headers.get("origin") is not None:
        return JSONResponse({"error": "requests from a web page (an Origin header) are refused"}, status_code=403)
    if request.headers.get("tailscale-user-login") is not None and identify(request.headers, settings) is None:
        return JSONResponse({"error": "no Tailscale identity or not in CCBOARD_ALLOWED_USERS"}, status_code=403)
    ok, wait = nodes.confirm_limiter.allow(nodes.caller_addr(request.client.host if request.client else None, request.headers.get("x-forwarded-for")))
    if not ok:
        return JSONResponse({"error": "too many requests"}, status_code=429, headers={"Retry-After": str(wait), "Cache-Control": "no-store"})
    raw = await _read_capped(request, PAIR_CONFIRM_MAX)
    if raw is None:
        return JSONResponse({"error": "the body is over 1 KB"}, status_code=413)
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = None
    got = nodes.confirm_answer(body.get("proof")) if isinstance(body, dict) and set(body) == {"proof"} else None
    if got is None:
        return JSONResponse({"error": "no pairing is in progress here"}, status_code=404, headers={"Cache-Control": "no-store"})
    return JSONResponse({"node_id": got}, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.post("/api/nodes", status_code=201)
def api_nodes_add(body: NodeAddIn, request: Request):
    """Pair with another board (the calling side): its address and the code it shows. This board checks the address (the peer address rule), calls the other
    board's pair endpoint with its own card, keeps the token it gets in <data dir>/node-tokens.json and answers the registry row (never a token). With
    both_ways the other board is also given a token to call this one (read and tasks). 400 a bad address, code or handle (a taken handle gets a
    number after it), 403 a wrong, expired, used or burned code, 409 the other board's address belongs to another node, 429 too many tries, 502 the other
    board refused or did not answer."""
    _node_manager(request)
    try:
        row = nodes.add_node(body.url, body.code, body.handle, body.both_ways, db=db)
    except nodes.PairError as e:
        return _pair_failed(e)
    except nodes.PeerError as e:
        return JSONResponse({"error": f"the other board did not answer ({e})", "reason": e.reason}, status_code=502, headers={"Cache-Control": "no-store"})
    except ValueError as e:                                                 # a bad address (PeerUrlError), code or handle
        raise projects.BadRequest(str(e)) from None
    doctor.invalidate()
    return JSONResponse(row, status_code=201, headers={"Cache-Control": "no-store"})


@app.get("/api/nodes")
def api_nodes(request: Request):
    """The nodes this board calls (direction out; a CCBOARD_NODES entry is a `legacy` row, read only until it is paired) and the pairs that call this board
    (direction in), as {nodes, pairs, at}. Rows are {peer_id, node_id, name, url, scopes, created_at, last_seen, direction, ...}; never a token or a digest.
    A board with no pair answers empty lists and starts nothing."""
    _node_manager(request)
    rows = nodes.peers(db=db)
    return JSONResponse({"nodes": [r for r in rows if r.get("direction") != "in"], "pairs": [r for r in rows if r.get("direction") == "in"], "at": db_now()},
                        headers={"Cache-Control": "no-store"})


@app.get("/api/nodes/pairs")
def api_nodes_pairs(request: Request):
    """Who can control this node: the pairs that call this board (direction in), {pairs, at}. Never a token or a digest."""
    _node_manager(request)
    return JSONResponse({"pairs": [r for r in nodes.peers(db=db) if r.get("direction") == "in"], "at": db_now()}, headers={"Cache-Control": "no-store"})


@app.get("/api/nodes/audit")
def api_nodes_audit(request: Request, limit: int = 100):
    """The pairing and node activity, newest first: {rows: [{id, at, direction, peer, node_name, user, action, target, status, detail}]}. Rows hold no
    token, code or prompt; they are kept 90 days. limit 1 to 500."""
    _node_manager(request)
    return JSONResponse({"rows": nodes.audit_list(max(1, min(limit, 500)), db=db), "at": db_now()}, headers={"Cache-Control": "no-store"})


@app.post("/api/nodes/{peer}/rotate")
def api_nodes_rotate(peer: str, request: Request):
    """Rotate the token of a node this board calls: the other board mints a new one (the old one works there for 60 more seconds) and this board keeps
    it. Nothing about either token is answered. 404 no such node, 409 a pair that calls this board (the board that holds a token rotates it) or a
    legacy row, 502 the other board did not answer (the saved token is then unchanged)."""
    _node_manager(request)
    rec = nodes.peer(peer, db=db)
    if rec is None:
        raise projects.NotFound("no such node")
    if rec.get("direction") == "in" or rec.get("legacy"):
        raise projects.Conflict("only a node this board has paired with can be rotated, and by this board")
    try:
        nodes.rotate_outgoing(rec["peer_id"], db=db)
    except LookupError:
        raise projects.NotFound("no such node") from None
    except nodes.PairError as e:
        return _pair_failed(e)
    except nodes.PeerError as e:
        return JSONResponse({"error": f"the other board did not give a new token ({e}); the saved token is unchanged", "reason": e.reason}, status_code=502,
                            headers={"Cache-Control": "no-store"})
    doctor.invalidate()
    return JSONResponse({"rotated": True, "grace_s": nodes.ROTATE_GRACE}, headers={"Cache-Control": "no-store"})


class NodeRemoveIn(BaseModel):
    model_config = {"extra": "forbid"}
    also_revoke: list[str] = Field(default_factory=list, max_length=50)


@app.post("/api/nodes/{peer}/remove-preview")
def api_nodes_remove_preview(peer: str, request: Request):
    """What removing this pair would also touch, and nothing is changed: {auto, others}. `auto` are the incoming pairs the removal revokes by itself
    (confirmed, same node id, same address); `others` are the active incoming pairs that name the same node id from another address or were never
    confirmed ({peer_id, name, url, verified}): they stay unless DELETE /api/nodes/<peer> lists them in `also_revoke`. 404 no such node."""
    _node_manager(request)
    try:
        out = nodes.removal_preview(peer, db=db)
    except LookupError:
        raise projects.NotFound("no such node") from None
    return JSONResponse({"auto": out["auto"], "others": out["others"], "at": db_now()}, headers={"Cache-Control": "no-store"})


@app.delete("/api/nodes/{peer}")
def api_nodes_remove(peer: str, request: Request, body: NodeRemoveIn | None = None):
    """Remove a node this board calls, or revoke a pair that calls this board (it fails on its next request). Never fails because the other board is
    offline: a node this board calls is asked to unpair (best effort) and {peer_notified} says whether it was told. An optional body
    {also_revoke: [peer_id, ...]} also revokes exactly those incoming pairs; each must be an active pair with the removed node's node id (400
    otherwise, and nothing happens). The answer lists the other pairs that name the same node id and are still active (`other_pairs`). 404 no such node."""
    _node_manager(request)
    rec = nodes.peer(peer, db=db)
    if rec is None:
        raise projects.NotFound("no such node")
    try:
        done = nodes.remove_node(rec["peer_id"], db=db, also_revoke=body.also_revoke if body else None)
    except LookupError:
        raise projects.NotFound("no such node") from None
    except ValueError as e:
        raise projects.BadRequest(str(e)) from None
    doctor.invalidate()
    return JSONResponse({"removed": True, "peer_notified": bool(done["peer_told"]) if rec.get("direction") != "in" and not rec.get("legacy") else None,
                         "also_revoked": done["also_revoked"], "other_pairs": done["other_pairs"]}, headers={"Cache-Control": "no-store"})


@app.post("/api/node/rotate")
def api_node_rotate(request: Request):
    """A paired board rotating the token it uses here (a node token, any scope): answers the new token once; the old one works for 60 more seconds."""
    pid = getattr(request.state, "node_pair", None)
    if not pid:
        raise projects.Forbidden("this route takes a node token")
    if (getattr(request.state, "node_peer", None) or {}).get("via_previous"):
        return _node_refused(401, "this token was already rotated away; rotate with the current token", **{"WWW-Authenticate": 'Bearer realm="ccboard", error="invalid_token"'})
    try:
        token = nodes.rotate_token(pid, db=db)
    except LookupError:
        return _node_refused(401, "this pair is gone")
    return JSONResponse({"token": token, "grace_s": nodes.ROTATE_GRACE}, headers={"Cache-Control": "no-store"})


@app.post("/api/node/unpair")
def api_node_unpair(request: Request):
    """A paired board telling this one it removed the pair (a node token, any scope): the pair is revoked. Idempotent."""
    pid = getattr(request.state, "node_pair", None)
    if not pid:
        raise projects.Forbidden("this route takes a node token")
    nodes.unpair_incoming(pid, db=db)
    doctor.invalidate()
    return JSONResponse({"unpaired": True}, headers={"Cache-Control": "no-store"})


@app.get("/api/search")
def api_search(q: str = "", limit: int = 30):
    if indexer is not None and not indexer.available:
        raise projects.BadRequest("SQLite FTS5 is not available in this Python")
    rows = search.search(db, q, max(1, min(limit, 100)))
    known = {r["claude_session_id"]: r for r in db.session_ids() if r.get("claude_session_id")}
    open_rows = db.open_rows()
    by_claude_id = {r.get("claude_session_id"): name for name, r in open_rows.items() if r.get("claude_session_id")}
    for r in rows:
        k = known.get(r["session_id"])
        r["agent"] = search.agent_of(r.get("file"))
        r["project"] = k["project"] if k else None
        r["repo"] = k["repo"] if k else None
        if k is None and r["agent"] == "codex":                  # a Codex thread the board did not start: its directory names the project
            pr = codex_discovery.project_repo(r.get("cwd"))
            r["project"], r["repo"] = pr if pr else (None, None)
        r["tmux"] = by_claude_id.get(r["session_id"])
    return {"q": q, "results": rows}


@app.post("/api/cost/refresh")
def api_cost_refresh():
    r = cost.refresh(db)
    if r is None:
        raise projects.BadRequest("ccusage is not installed or failed")
    _invalidate_scan()
    return r


@app.post("/api/usage/rate-limit/clear")
def api_clear_rate_limit():
    db.kv_del("rate_limited")
    db.kv_del_prefix("rl_notified:")                     # the once-per-window notice gate: a cleared banner may announce again
    from .agents import codex_rollout                    # late: that module imports the agents package
    db.kv_del_prefix(codex_rollout.NOTIFIED)             # the Codex gate (codex_rl_notified_<resets_at>) too: the next reached window can announce once
    _invalidate_scan()
    return {"ok": True}


class UsageRefreshIn(BaseModel):
    auto: object = None


def _refresh_panes() -> list[dict]:
    """The board's live Claude panes (usage_refresh.panes): tmux's sessions joined to the open rows, with who is attached. [] when tmux is down."""
    try:
        live = tmux.list_sessions()
    except tmux.TmuxDown:
        return []
    try:
        viewers = tmux.viewers()
    except (tmux.TmuxError, tmux.TmuxDown):             # list-clients failed: every attached client counts as a full one
        viewers = None
    return usage_refresh.panes(live, db.open_rows(), viewers)


def _refresh_finish(name: str, at: str) -> None:
    """The follow-up of a refresh, a few seconds after /usage was typed (usage_refresh.later): Escape closes Claude's panel, then the cache Claude
    Code just rewrote is read at once (accounts.poll_usage_cache, the Sampler tick's own hook) instead of up to 15 s later. Escape goes only to a
    pane that is still where the refresh left it: once the session works, waits on a permission or compacts, an Escape would interrupt it or
    answer its dialog, so it is not sent and the kv says why. Never raises; always frees the lock."""
    try:
        row = db.open_row(name)
        if row is None or not tmux.has_session(name):
            usage_refresh.record(db, name, at, ok=False, error="the session ended before the panel could be closed")
        elif _typing_refusal(name, row) is not None:
            usage_refresh.record(db, name, at, ok=False, error="the session was busy again; its /usage panel was left open")
        else:
            tmux.send_keys(name, ["Escape"])
        accounts.poll_usage_cache(db)
    except Exception as e:
        log.warning("usage refresh follow-up for %s failed: %s", name, e.__class__.__name__)
        try:
            usage_refresh.record(db, name, at, ok=False, error=f"the follow-up failed ({e.__class__.__name__})")
        except Exception:
            pass
    finally:
        usage_refresh.release()
        _invalidate_scan()


@app.post("/api/usage/refresh", status_code=202)
def api_usage_refresh(request: Request, body: UsageRefreshIn | None = None):
    """Ask one idle Claude session for /usage so the board's numbers catch up (v0.5.17f). Claude Code fetches the official 5-hour / 7-day
    figures with its own login when /usage runs and rewrites its state file; accounts.poll_usage_cache reads that (credential-free). The pane
    is chosen with the command route's own guards (idle, done, errored or idle-waiting; no permission pending; not compacting; a running
    Claude pane; never an internal, Codex or shell row), an unattached one first, then the most recently active. `auto: true` (the Usage
    page opening on stale numbers) never picks a pane somebody is attached to. /usage is typed through the command route's typing helper;
    four seconds later Escape closes the panel and the cache is read. 202 {ok, session, started_at}; 409 {error, sessions} when no pane
    qualifies (sessions = live Claude panes); 409 {error} while another refresh runs (one at a time, a 20 s lock)."""
    b = body or UsageRefreshIn()
    if b.auto is not None and not isinstance(b.auto, bool):
        raise projects.BadRequest("auto must be true or false")
    if usage_refresh.busy():
        return JSONResponse({"error": usage_refresh.BUSY}, status_code=409)
    panes = _refresh_panes()
    pick = usage_refresh.choose(panes, lambda p: _typing_refusal(p["name"], p["row"]) is None, unattached_only=b.auto is True)
    if pick is None:
        return JSONResponse({"error": usage_refresh.NO_SESSION, "sessions": len(panes)}, status_code=409)
    if not usage_refresh.claim():                        # another request got there between busy() and now
        return JSONResponse({"error": usage_refresh.BUSY}, status_code=409)
    name, at = pick["name"], db_now()
    try:
        _type_command(name, pick["row"], agents.get("claude").slash_commands()["usage"], "", request.state.user)
    except Exception as e:
        usage_refresh.release()
        try:
            usage_refresh.record(db, name, at, ok=False, error=f"typing failed ({e.__class__.__name__})")
        except Exception:
            pass
        raise
    usage_refresh.record(db, name, at)
    usage_refresh.later(usage_refresh.ESCAPE_AFTER_S, lambda: _refresh_finish(name, at))
    return {"ok": True, "session": name, "started_at": at}


@app.post("/api/permission")
async def api_permission(request: Request):
    """PermissionRequest hook long-poll. Returns {behavior: allow|deny|null, message?}."""
    body = await request.body()
    if len(body) > hooks.MAX_BODY:
        return JSONResponse({"behavior": None, "reason": "too large"})
    try:
        payload = json.loads(body or b"{}")
        assert isinstance(payload, dict)
    except (ValueError, AssertionError):
        return JSONResponse({"behavior": None, "reason": "bad json"})
    rows = await asyncio.to_thread(db.open_rows)
    name, how = hooks.resolve_session(request.headers, payload, rows)
    if not name or name not in rows:
        return {"behavior": None, "reason": "unknown session"}
    if hooks.agent_mismatch(rows[name].get("agent"), request.headers.get("x-ccboard-agent")):
        # another agent's process (a Hermes or `codex exec` run in this row's cwd): never a request on the row, never a wait, never a deny
        return {"behavior": None, "reason": "foreign", "ignored": "foreign", "session": name}
    # the guards of /api/hook (hooks.ignore_reason): claude-mem's observer or another row's conversation, a nested claude
    # (X-CCBoard-Child) that is not the row's own conversation, a Codex sub-thread. Answered at once: no pending permission, no
    # notification, no state change, no cancelled auto-close, no wait; behavior null so the asking process uses its own prompt.
    why = await asyncio.to_thread(hooks.ignore_reason, db, name, "PermissionRequest", payload, rows[name],
                                  request.headers.get("x-ccboard-child", "").strip() == "1",
                                  hooks.hook_agent(rows[name].get("agent"), request.headers.get("x-ccboard-agent")))
    if why:
        return {"behavior": None, "reason": why, "ignored": why, "session": name}
    tool = str(payload.get("tool_name") or "tool")
    summary = permissions.summarize(tool, payload.get("tool_input"))

    def record():
        if taskflow_rt is not None and taskflow_rt.db is db:
            taskflow_rt.on_activity(name, "PermissionRequest", rows.get(name))       # a permission prompt cancels a pending auto-close
        db.set_state(name, "waiting", "PermissionRequest", message="permission: " + summary, attention=True)
        db.update_flags(name, {"wait_kind": "permission"})   # same value as Notification(permission_prompt): a PostToolBatch (answered in the TUI) clears it
        db.add_event(name, "PermissionRequest", tool, summary, payload, agent=rows[name].get("agent"))
        return db.perm_add(name, tool, summary, payload.get("tool_input"))
    pid = await asyncio.to_thread(record)
    _invalidate_scan()

    def at_terminal() -> int:
        """Real (full, writable) clients on the session. A grid tile (ignore-size) or a read-only view is not a person at the
        terminal, so it must not short-circuit remote approve. If list-clients fails, fall back to the attached count
        (every client counts): when in doubt the TUI prompt is shown, never swallowed."""
        try:
            return tmux.real_clients(name)
        except (tmux.TmuxError, tmux.TmuxDown):
            pass
        try:
            return int((tmux.list_sessions().get(name) or {}).get("attached") or 0)
        except (tmux.TmuxError, tmux.TmuxDown):
            return 0
    if await asyncio.to_thread(at_terminal) > 0:
        # Someone is looking at the terminal: let the TUI prompt appear at once. The board still
        # lists the request; a board/phone answer within the timeout is ignored by Claude.
        await asyncio.to_thread(db.perm_expire, pid, "tui")
        return {"behavior": None, "reason": "attached", "id": pid}

    ev = permissions.register_waiter(pid)
    await asyncio.to_thread(permissions.push_request, pid, name, summary)
    try:
        await asyncio.wait_for(ev.wait(), timeout=max(1.0, settings.approve_timeout - 5))
    except asyncio.TimeoutError:
        await asyncio.to_thread(db.perm_expire, pid)
        _invalidate_scan()
        return {"behavior": None, "reason": "timeout", "id": pid}
    finally:
        permissions.drop_waiter(pid)
    row = await asyncio.to_thread(db.perm_get, pid)
    _invalidate_scan()
    if row and row.get("decision") in ("allow", "deny"):
        return {"behavior": row["decision"], "message": "Denied from ccboard" if row["decision"] == "deny" else None, "id": pid}
    if row and row.get("decision") == "tui":                       # behavior None: the hook script prints nothing, the TUI asks
        return {"behavior": None, "reason": "tui", "id": pid}
    if row and row.get("decision") == "interrupt":                 # Codex's Interrupt hook dropped the prompt: nothing left to answer
        return {"behavior": None, "reason": "interrupt", "id": pid}
    return {"behavior": None, "reason": "undecided", "id": pid}


@app.post("/api/permission/{pid}/{decision}")
def api_permission_decide(pid: int, decision: str, request: Request):
    if decision not in ("allow", "deny", "tui"):                  # tui: nobody answers from the board, Claude shows its own prompt
        raise projects.BadRequest("decision must be allow, deny or tui")
    row = db.perm_get(pid)
    if not row:
        raise projects.NotFound("no such permission request")
    if not db.perm_decide(pid, decision, request.state.user):
        raise projects.Conflict(f"already decided: {row.get('decision')}")
    permissions.wake(pid)
    _invalidate_scan()
    return {"ok": True, "id": pid, "decision": decision}


@app.post("/api/sessions/{name}/ack")
def api_ack(name: str):
    try:
        tmux.split_name(name)
    except ValueError:
        raise projects.BadRequest("not a ccboard session name")
    db.ack(name)
    _invalidate_scan()
    return {"acked": name}


# ---------- claude login ----------

class CodeIn(BaseModel):
    code: str


@app.post("/api/claude/login")
def api_login():
    require_real_launch_ok()   # issue #100: refuses (409) on a dev board whose directories could reach the real home
    claude_auth.start_login()
    return {"ok": True}


@app.post("/api/claude/login/code")
def api_login_code(body: CodeIn):
    try:
        claude_auth.submit_code(body.code)
    except ValueError as e:
        raise projects.BadRequest(str(e))
    except LookupError as e:
        raise projects.Conflict(str(e))
    return {"ok": True}


@app.post("/api/claude/logout")
def api_logout():
    return claude_auth.logout()


# ---------- static ----------

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/term/{name}")
def term_page(name: str):
    try:
        tmux.split_name(name)
    except ValueError:
        if name != tmux.LOGIN_SESSION:
            raise projects.BadRequest("not a ccboard session name")
    return FileResponse(STATIC / "term.html", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def service_worker():
    # Served at the root so its scope is "/" (a worker under /static/ could only control /static/).
    return Response(SW_JS, media_type="application/javascript", headers={"Cache-Control": "no-cache"})


DEV_TTY_DIR = Path(__file__).resolve().parent.parent / "scripts" / "dev" / "fake_tty"


class _DevTty:
    """Dev harness: serves scripts/dev/fake_tty/ (a stand-in for ttyd's terminal, no network) at /tty, but only while
    settings.dev_bypass_user is set (read per request, so it follows the setting). Otherwise it answers 404 like any
    unknown path: in production the real /tty/ is ttyd's, reached through tailscale serve, and never the board's."""

    async def __call__(self, scope, receive, send):
        if not settings.dev_bypass_user or not DEV_TTY_DIR.is_dir():
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return
        await StaticFiles(directory=str(DEV_TTY_DIR), html=True)(scope, receive, send)


app.mount("/tty", _DevTty(), name="dev-tty")
static_mount = GzipStaticFiles(directory=str(STATIC))             # gzip for static text files only (app/staticgz.py); never a middleware, the SSE stream must not be buffered
app.mount("/static", static_mount, name="static")
