"""ccboard: a status-and-attention layer over Claude Code CLI sessions in tmux."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agents, backup, claude_auth, clonequeue, cost, doctor, github, gitops, health, hooks, notify, permissions, previews, projects, prpoll, push, recover, scheduler, search, tasks, tmux, usage
from .agents import registry
from .agents.base import LaunchReq
from .agents.claude import BYPASS_PARTS, OVERRIDE_PARTS
from .auth import csrf_ok, identify
from .config import settings
from .db import DB, now as db_now

log = logging.getLogger("ccboard")
STATIC = Path(__file__).parent / "static"
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Every launcher the board knows by name. The codex ones are recognised so the error can say when they arrive, but nothing
# starts a codex session before v0.5.11 (_normalize_launcher refuses them).
LAUNCHERS = ("claude", "resume", "continue", "shell", "codex", "codex-resume", "codex-continue")
_LAUNCHER_AGENT = {"claude": ("claude", "claude"), "resume": ("claude", "resume"), "continue": ("claude", "continue"),
                   "shell": ("shell", "shell"),
                   "codex": ("codex", "claude"), "codex-resume": ("codex", "resume"), "codex-continue": ("codex", "continue")}
STARTABLE_AGENTS = ("claude", "shell")
MAX_ARGS = 1024

db: DB | None = None
indexer = None
sched = None
_scan_lock = threading.Lock()
_scan_cache: tuple[float, dict] | None = None
SCAN_TTL = 2.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    settings.validate()
    db = DB(settings.db_path)
    hooks.ensure_token()
    notify.set_db(db)
    try:
        push.ensure_keys()
    except Exception as e:  # pywebpush missing or unwritable data dir: the board still works
        log.warning("web push disabled: %s", e)
    poller = usage.Poller(db)
    poller.start()
    cloner = clonequeue.Worker(_launch_clone)
    cloner.start()
    prp = prpoll.Poller(db, projects.repo_path)
    prp.start()
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
    nodes = health.parse_nodes(settings.nodes_raw)
    if nodes and settings.hub_token:
        hub = health.Poller(db, nodes)
        hub.start()
    elif nodes:
        log.warning("CCBOARD_NODES is set but CCBOARD_HUB_TOKEN is empty: hub polling disabled")
    try:
        rec = recover.run(db, _start_session)
        if rec["recovered"] or rec["closed"]:
            log.info("recovery: relaunched %s, closed %s", rec["recovered"], rec["closed"])
    except Exception as e:
        log.warning("recovery failed: %s", e)
    log.info("ccboard on %s, projects in %s, allowlist=%s", settings.loopback_url(), settings.projects_dir,
             sorted(settings.allowed_users) or ("DEV BYPASS" if settings.dev_bypass_user else "EMPTY"))
    log.info("runtime %s, image %s", settings.runtime, settings.image_version or "-")
    yield
    poller.stop.set()
    cloner.stop.set()
    prp.stop.set()
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


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path == "/healthz":
        return PlainTextResponse("ok")
    if request.url.path == "/api/node/summary" and request.headers.get(health.HUB_HEADER):
        if not health.check_hub_token(request.headers.get(health.HUB_HEADER)):
            return JSONResponse({"error": "bad hub token"}, status_code=403)
        request.state.user = "hub"
        return await call_next(request)
    if request.url.path in ("/api/hook", "/api/permission"):
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


@app.exception_handler(projects.Conflict)
async def _conflict(_, e):
    return JSONResponse({"error": str(e)}, status_code=409)


@app.exception_handler(projects.NotFound)
async def _notfound(_, e):
    return JSONResponse({"error": str(e)}, status_code=404)


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
    return JSONResponse({"error": "ccboard-tmux is not running (sudo systemctl start ccboard-tmux)"}, status_code=503)


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
    out: dict[str, dict] = {}
    for name, s in live.items():
        if tmux.is_internal(name):
            continue
        row = rows.get(name, {})
        chip = (chips.get(row.get("row_id")) or [None])[0]
        out[name] = {
            "created": s["created"], "attached": s["attached"], "command": s["command"], "path": s["path"],
            "pane_id": s["pane_id"], "launcher": row.get("launcher", "external"), "cmd": row.get("cmd"),
            "claude_session_id": row.get("claude_session_id"), "add_dirs": row.get("add_dirs", []),
            "state": row.get("state") or "unknown", "state_at": row.get("state_at"), "last_event": row.get("last_event"),
            "last_message": row.get("last_message"), "last_prompt": row.get("last_prompt"), "stats": row.get("stats"),
            "needs_attention": bool(row.get("state") in hooks.ATTENTION_STATES and not row.get("acked_at")),
            # None for a tmux session the board has no row for (the UI guesses from the launcher and the pane command)
            "agent": row.get("agent"), "agent_session_id": row.get("agent_session_id"), "row_id": row.get("row_id"),
            "flags": {k: v for k, v in (row.get("flags") or {}).items() if k != "transcript_path"},
            "task": chip,
        }
    if rich and out:
        snap = _registry_snapshot({n: s["pid"] for n, s in live.items() if s.get("pid")})
        if snap:
            try:
                registry.enrich(out, snap)           # display only: sets flags.registry on our own sessions
            except Exception as e:
                log.debug("registry enrich failed: %s", e)
    return out, False


def build_state(user: str) -> dict:
    global _scan_cache
    with _scan_lock:
        if _scan_cache and time.monotonic() - _scan_cache[0] < SCAN_TTL:
            st = dict(_scan_cache[1])
        else:
            sessions, down = _merged_sessions(rich=True)
            st = {"tmux_down": down, "projects": projects.scan(sessions)}
            _scan_cache = (time.monotonic(), st)
    st["user"] = user
    st["config"] = {"code_https_port": settings.code_https_port, "projects_dir": str(settings.projects_dir),
                    "runtime": settings.runtime,
                    "ntfy": {"enabled": notify.enabled(), "subscribe_url": notify.subscribe_url(), "topic": settings.ntfy_topic},
                    "public_url": settings.public_url,
                    "backup": {"restic": backup.restic_enabled(), "repo": settings.restic_repo if backup.restic_enabled() else None,
                               "restic_installed": shutil.which("restic") is not None, "push": settings.backup_push}}
    st["claude"] = claude_auth.status()
    st["agents"] = agents.status_all()
    st["login"] = claude_auth.login_state()
    st["pending_permissions"] = db.perm_pending()
    st["tasks"] = _tasks_view()
    st["jobs"] = db.jobs()
    st["runs"] = [{k: (v[:400] if k == "result" and isinstance(v, str) else v) for k, v in r.items()} for r in db.runs(30)]
    st["clone_queue"] = clonequeue.status()
    st["last_recovery"] = db.kv_get("last_recovery")
    st["usage"] = db.kv_get("rate_limits")
    st["block"] = db.kv_get(usage.KV_BLOCK)
    st["cost"] = db.kv_get(cost.KV_COST)
    st["health"] = health.snapshot()
    st["nodes"] = db.kv_get(health.KV_NODES)
    st["backup"] = health.backup_status()
    st["node_name"] = settings.node_name or st["health"]["host"]
    st["rate_limited"] = db.kv_get("rate_limited")
    st["scheduler"] = scheduler.quota_state(db)
    st["version"] = ASSET_VERSION
    st["setup"] = _setup_state(st)
    return st


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


@app.get("/api/state")
def api_state(request: Request):
    return build_state(request.state.user)


# ---------- agents, doctor, session detail, external sessions (read only) ----------

@app.get("/api/agents")
def api_agents():
    """Every agent the board can launch: identity, install/auth/hooks state, the launcher's option schema, the slash registry."""
    return {"agents": {a.name: a.describe() for a in agents.all()}}


@app.get("/api/doctor")
def api_doctor(group: str | None = None, refresh: str | None = None):
    """Box and agent checks, each pass|warn|fail|skip with a fix hint. A plain `def`: a check may block for up to its 5 s cap."""
    try:
        return doctor.run(group or None, refresh == "1", db=db)
    except ValueError as e:                       # unknown group
        raise projects.BadRequest(str(e))


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
            "flags": s["flags"], "agent_session_id": s["agent_session_id"], "task": s["task"],
            "pending": [p for p in db.perm_pending() if p["tmux_name"] == name],
            # v0.5.7 (attach modes) classifies clients and learns the window size and the attach wrapper's version; until then
            # every attached client counts as a full one
            "viewers": {"full": s["attached"], "grid": 0, "ro": 0}, "win": None, "shell_version": None}


@app.get("/api/external")
def api_external(agent: str | None = None, project: str | None = None):
    """Agent sessions running on this box that the board did not start (Claude's own registry, file reads, cached 30 s)."""
    if agent not in (None, "", "claude", "codex"):
        raise projects.BadRequest("agent must be claude or codex")
    if project:
        projects.check_name("project", project)
    try:
        pane_pids = {n: s["pid"] for n, s in tmux.list_sessions().items() if s.get("pid")}
    except tmux.TmuxDown:
        pane_pids = {}
    snap = _registry_snapshot(pane_pids) or {"external": [], "scanned_at": None}
    ext = snap["external"] if agent in (None, "", "claude") else []      # Codex joins in v0.5.12
    if project:
        base = str(settings.projects_dir / project)
        ext = [e for e in ext if (e.get("cwd") or "") == base or (e.get("cwd") or "").startswith(base + "/")]
    return {"claude": ext, "codex": [], "at": snap["scanned_at"]}


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
    cmd = ["git", "clone", "--progress", "--", url, "."]
    line = shlex.join(cmd) + " && exit"
    try:
        if tmux.has_session(name):
            raise projects.Conflict("a clone is already running for this repo")
        _start_session(name, project, repo, "clone", "clone", str(path), cmd_line=line, claude_session_id=None,
                       add_dirs=[], agent="shell")
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

def _tasks_view() -> list[dict]:
    sessions, _ = _merged_sessions()
    by_row = {s["row_id"]: s for s in sessions.values() if s.get("row_id") is not None}
    out = []
    for t in db.tasks():
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
        out.append({
            "ci": ci, "pr": prj,
            "id": t["id"], "project": t["project"], "repo": t["repo"], "slug": t["slug"], "title": t["title"],
            "branch": t["branch"], "base": t["base"], "worktree": t["worktree"], "tmux": t["tmux_name"],
            "claude_session_id": t["claude_session_id"], "pr_url": t["pr_url"], "pr_number": t["pr_number"],
            "pr_state": t["pr_state"], "cost_usd": t["cost_usd"], "overlap": ovl,
            "preview_port": t.get("preview_port"), "preview_https": t.get("preview_https"),
            "preview_url": f"https://{previews.public_host()}:{t['preview_https']}/" if t.get("preview_https") and previews.public_host() else None,
            "created_at": t["created_at"], "column": tasks.derive_status(t, s),
            "agent": t.get("agent") or "claude", "mode": t.get("mode") or "worktree", "phase": t.get("phase") or "running",
            "auto_close": bool(t.get("auto_close")), "parent_id": t.get("parent_id"), "chain_id": t.get("chain_id"),
            "session_row": t.get("session_row"), "result": (t.get("result") or "")[:300] or None,
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


def _launch_args(body: LaunchOpts) -> list[str]:
    """The launch controls as claude argv (validated with the board's messages). The adapter owns the rules."""
    return agents.get("claude").launch_opt_args(body.model_dump())


class TaskIn(LaunchOpts):
    title: str
    prompt: str
    args: str | None = None
    add_dirs: list[str] | None = None


@app.post("/api/projects/{project}/repos/{repo}/tasks", status_code=201)
def api_create_task(project: str, repo: str, body: TaskIn):
    rpath = projects.repo_path(project, repo)
    if not projects.is_repo(rpath):
        raise projects.NotFound(f"repo {project}/{repo} is not a git repo")
    title = " ".join(body.title.split())[:120]
    prompt = body.prompt.strip()
    if not title or not prompt:
        raise projects.BadRequest("title and prompt are required")
    if len(prompt) > 20000:
        raise projects.BadRequest("prompt too long")
    if not settings.claude_bin():
        raise projects.BadRequest("claude is not installed on this box")
    extra: list[str] = []
    if body.args:
        try:
            extra = shlex.split(body.args)
        except ValueError as e:
            raise projects.BadRequest(f"extra args: {e}")
    bad = _override_requested(extra) or _bypass_requested(extra)
    if bad or body.permission_mode == "bypassPermissions":
        raise projects.BadRequest(f"{bad or 'bypassPermissions'}: bypassPermissions (or a settings override) is not allowed for tasks; start a session and choose bypass there if you really want it")
    extra = _launch_args(body) + extra
    opts_clean = agents.get("claude").validate_opts(body.model_dump(include=set(LaunchOpts.model_fields)), interactive=True,
                                                    tasks_or_headless=True)
    add_dirs = _resolve_add_dirs(body.add_dirs, rpath)
    slug = tasks.unique_slug(rpath, tasks.slugify(title), db.task_slugs(project, repo))
    session = tasks.session_name_for(slug)
    projects.check_name("session", session)
    name = tmux.tmux_name(project, repo, session)
    if tmux.has_session(name):
        raise projects.Conflict(f"session {session} already exists")
    tasks.ensure_excluded(rpath)
    sid = str(uuid.uuid4())
    cmd_line = tasks.build_command(slug, sid, prompt, extra, add_dirs)
    real, row_id = _start_session_row(name, project, repo, session, "task", str(rpath), cmd_line=cmd_line, claude_session_id=sid,
                                      add_dirs=add_dirs, agent="claude", opts=opts_clean)
    tid = db.task_add(project=project, repo=repo, slug=slug, title=title, prompt=prompt, branch=f"worktree-{slug}",
                      base=tasks.default_branch(rpath), worktree=str(tasks.worktree_path(rpath, slug)), tmux_name=real,
                      claude_session_id=sid, agent="claude", session_row=row_id)
    _invalidate_scan()
    return {"id": tid, "slug": slug, "tmux": real, "branch": f"worktree-{slug}", "attach_url": f"/term/{real}"}


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
    max_turns: int = 30
    max_budget_usd: float | None = None
    args: str | None = None
    timeout_s: int | None = None
    run_now: bool = False


def _validate_job(body: JobIn) -> None:
    if not body.name.strip() or not body.prompt.strip():
        raise projects.BadRequest("name and prompt are required")
    if body.cron and not scheduler.valid_cron(body.cron.strip()):
        raise projects.BadRequest("cron must be a 5-field expression like '30 2 * * *'")
    if body.permission_mode not in scheduler.MODES:
        raise projects.BadRequest(f"permission_mode must be one of {', '.join(scheduler.MODES)} (bypass only inside a devcontainer)")
    if not (1 <= body.max_turns <= 500):
        raise projects.BadRequest("max_turns must be 1..500")
    if body.max_budget_usd is not None and not (0 < body.max_budget_usd <= 1000):
        raise projects.BadRequest("max_budget_usd must be 0..1000")
    try:
        scheduler.check_extra_args(body.args)
    except ValueError as e:
        raise projects.BadRequest(str(e))


@app.post("/api/projects/{project}/repos/{repo}/jobs", status_code=201)
def api_create_job(project: str, repo: str, body: JobIn):
    rpath = projects.repo_path(project, repo)
    if not projects.is_repo(rpath):
        raise projects.NotFound("not a git repo")
    _validate_job(body)
    if not settings.claude_bin():
        raise projects.BadRequest("claude is not installed on this box")
    cron = body.cron.strip() if body.cron else None
    next_at = db_now() if (body.run_now or not cron) else scheduler.next_fire(cron)
    jid = db.job_add(project=project, repo=repo, name=" ".join(body.name.split())[:80], prompt=body.prompt.strip(), cron=cron,
                     permission_mode=body.permission_mode, max_turns=body.max_turns, max_budget_usd=body.max_budget_usd,
                     args=body.args, timeout_s=body.timeout_s, enabled=1, batch_id=None, next_run_at=next_at)
    _invalidate_scan()
    return {"id": jid, "next_run_at": next_at}


class BatchIn(BaseModel):
    prompt: str
    repos: list[str]                 # "project/repo" ids
    name: str | None = None
    permission_mode: str = "acceptEdits"
    max_turns: int = 30
    max_budget_usd: float | None = None
    args: str | None = None


@app.post("/api/batch", status_code=201)
def api_batch(body: BatchIn):
    """One prompt across N repos: one-off jobs sharing a batch id, drained by the scheduler (cap + quota aware)."""
    if not body.repos:
        raise projects.BadRequest("choose at least one repo")
    if len(body.repos) > 100:
        raise projects.BadRequest("at most 100 repos per batch")
    _validate_job(JobIn(name=body.name or "batch", prompt=body.prompt, permission_mode=body.permission_mode,
                        max_turns=body.max_turns, max_budget_usd=body.max_budget_usd, args=body.args))
    if not settings.claude_bin():
        raise projects.BadRequest("claude is not installed on this box")
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
                              args=body.args, timeout_s=None, enabled=1, batch_id=batch_id, next_run_at=db_now()))
    started = sched.tick() if sched else []
    _invalidate_scan()
    return {"batch_id": batch_id, "jobs": ids, "started": started}


@app.post("/api/jobs/{jid}/run")
def api_job_run(jid: int):
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
    db.job_delete(jid)
    _invalidate_scan()
    return {"deleted": jid}


@app.get("/api/runs/{rid}")
def api_run_get(rid: int):
    r = db.run_get(rid)
    if not r:
        raise projects.NotFound("no such run")
    return r


@app.post("/api/runs/{rid}/resume")
def api_run_resume(rid: int):
    """Open the run's worktree in a terminal session, resuming its Claude conversation."""
    r = db.run_get(rid)
    if not r or not r.get("task_id"):
        raise projects.NotFound("no worktree for this run")
    t = db.task_get(int(r["task_id"]))
    if not t or not tasks.has_worktree(t) or not Path(t["worktree"]).is_dir():
        raise projects.NotFound("worktree is gone")
    name = t["tmux_name"]
    if not tmux.has_session(name):
        _, _, session = tmux.split_name(name)
        cmd = ["claude", "--resume", r["session_id"]] if r.get("session_id") else ["claude", "--continue"]
        _start_session(name, t["project"], t["repo"], session, "task", t["worktree"], cmd_line=shlex.join(cmd),
                       claude_session_id=r.get("session_id"), add_dirs=[], agent=t.get("agent") or "claude", task_id=t["id"])
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


def _normalize_launcher(launcher: str) -> tuple[str, str]:
    """A request's launcher -> (agent, launcher), the launcher being what sessions.launcher stores (claude = a new session,
    resume, continue, shell). Only claude and shell can be started until v0.5.11; a codex launcher is recognised and refused
    with the version that brings it."""
    entry = _LAUNCHER_AGENT.get(launcher) if isinstance(launcher, str) else None
    if entry is None:
        accepted = [k for k, (a, _l) in _LAUNCHER_AGENT.items() if a in STARTABLE_AGENTS]
        raise projects.BadRequest(f"launcher must be one of {', '.join(accepted)}")
    agent, norm = entry
    if agent not in STARTABLE_AGENTS:
        raise projects.BadRequest(f"{agent} arrives in v0.5.11")
    return agent, norm


class SessionIn(LaunchOpts):
    launcher: str
    name: str | None = None
    args: str | None = None
    resume_id: str | None = None
    add_dirs: list[str] | None = None  # "project/repo" ids
    devcontainer: bool = False         # run claude inside the repo's devcontainer (devcontainer CLI)
    bypass: bool = False               # --dangerously-skip-permissions; only allowed with devcontainer


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
                       agent: str = "claude", opts: dict | None = None, task_id: int | None = None) -> tuple[str, int]:
    """Create the tmux session and its row (agent, launch cwd and the validated opts stored on it), then type the command.
    Returns (real tmux name, sessions.id). With task_id the task's session_row is pointed at the new row, which is how a
    relaunched task session (fix-ci, run resume, reboot recovery) stays the task's live session."""
    env = {"CCBOARD_SESSION": name, "CCBOARD_URL": settings.loopback_url(),
           "CCBOARD_APPROVE_TIMEOUT": str(int(settings.approve_timeout)), "CCBOARD_AGENT": agent}
    real = tmux.new_session(name, cwd, env=env)
    row_id = db.add_session(tmux_name=real, project=project, repo=repo, name=session, launcher=launcher, cmd=cmd_line,
                            claude_session_id=claude_session_id, add_dirs=add_dirs, agent=agent, cwd=cwd, opts=opts)
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
                   agent: str = "claude", opts: dict | None = None, task_id: int | None = None) -> str:
    """_start_session_row for callers that only need the tmux name (reboot recovery calls it positionally)."""
    return _start_session_row(name, project, repo, session, launcher, cwd, cmd_line=cmd_line, claude_session_id=claude_session_id,
                              add_dirs=add_dirs, agent=agent, opts=opts, task_id=task_id)[0]


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


@app.post("/api/projects/{project}/repos/{repo}/sessions", status_code=201)
def api_create_session(project: str, repo: str, body: SessionIn):
    agent, launcher = _normalize_launcher(body.launcher)
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
    if body.resume_id and not UUID_RE.match(body.resume_id):
        raise projects.BadRequest("resume id must be a UUID")

    cmd_line = None
    agent_session_id = None
    add_dirs: list[str] = []
    opts_clean: dict | None = None
    if body.devcontainer and not projects.has_devcontainer(rpath):
        raise projects.BadRequest("this repo has no .devcontainer/devcontainer.json")
    bad = _override_requested(extra)
    if bad:
        raise projects.BadRequest(f"{bad}: settings overrides are not allowed in extra args; use the model / effort / permission / tools controls")
    # bypassPermissions on the host is an explicit choice (the permission control, the bypass flag, or the arg);
    # Claude Code itself still asks for a one-time confirmation in the terminal. Never the default.
    if agent == "claude":
        exe = settings.claude_bin()
        if not exe and not body.devcontainer:
            raise projects.BadRequest("claude is not installed on this box")
        add_dirs = _resolve_add_dirs(body.add_dirs, rpath) if not body.devcontainer else []
        opts = {**body.model_dump(include=set(LaunchOpts.model_fields)), "extra": extra, "devcontainer": body.devcontainer}
        plan = agents.get("claude").launch_plan(LaunchReq(
            kind={"claude": "new", "resume": "resume", "continue": "continue"}[launcher], session_name=session, cwd=str(rpath),
            opts=opts, resume_id=body.resume_id, add_dirs=add_dirs, bypass=body.bypass))
        cmd_line, agent_session_id, opts_clean = plan.cmd_line, plan.agent_session_id, plan.opts_clean
    elif body.devcontainer:
        wf = str(rpath)
        cmd_line = (shlex.join(["devcontainer", "up", "--workspace-folder", wf]) + " && "
                    + shlex.join(["devcontainer", "exec", "--workspace-folder", wf, "--", "bash", "-l"]))

    real = _start_session(name, project, repo, session, launcher, str(rpath), cmd_line=cmd_line,
                          claude_session_id=agent_session_id, add_dirs=add_dirs, agent=agent, opts=opts_clean)
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
        if len(text) > 8000 or any(ord(c) < 32 and c not in "\t\n" for c in text):
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

LIVE_LINES = 20
LIVE_INTERVAL = 2.0


def _capture_all() -> dict[str, list[str]]:
    """Visible tail of every non-internal session: name -> last LIVE_LINES lines."""
    try:
        names = [n for n in tmux.list_sessions() if not tmux.is_internal(n)]
    except tmux.TmuxDown:
        return {}
    out: dict[str, list[str]] = {}
    for n in names:
        try:
            text = tmux.capture(n, lines=LIVE_LINES, join=False)
        except tmux.TmuxError:
            continue
        lines = [ln.rstrip() for ln in text.splitlines()]
        while lines and not lines[-1]:
            lines.pop()
        out[n] = lines[-LIVE_LINES:]
    return out


@app.get("/api/stream", response_class=EventSourceResponse)
async def api_stream(request: Request, once: bool = False):
    """SSE: 'lines' events per session whenever its visible tail changes, a 'tick' every interval.
    FastAPI encodes the yielded ServerSentEvent objects (response_class=EventSourceResponse)."""
    last: dict[str, list[str]] = {}
    while True:
        snap = await asyncio.to_thread(_capture_all)
        for name, lines in snap.items():
            if last.get(name) != lines:
                last[name] = lines
                yield ServerSentEvent(event="lines", data={"name": name, "lines": lines})
        for gone in [n for n in last if n not in snap]:
            del last[gone]
        yield ServerSentEvent(event="tick", data={"sessions": sorted(snap.keys())})
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
            result = hooks.apply(db, name, event, payload, agent=rows[name].get("agent"))
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
    n = push.send_all(db, "ccboard test", "Web Push works.", "/", tag="test")
    return {"sent": n, "subscriptions": len(db.push_subs())}


@app.post("/api/notify/test")
def api_notify_test():
    if not notify.enabled():
        raise projects.BadRequest("ntfy is not configured (NTFY_URL is empty)")
    ok = notify.publish("ccboard test", "Notifications work.", click=(settings.public_url or None), tags=["tada"])
    return {"ok": ok}


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
            "log": "journalctl -u ccboard-backup" if via == "systemd" else str(settings.data_dir / backup.LOG_FILE)}


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
        r["project"] = k["project"] if k else None
        r["repo"] = k["repo"] if k else None
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
    _invalidate_scan()
    return {"ok": True}


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
    tool = str(payload.get("tool_name") or "tool")
    summary = permissions.summarize(tool, payload.get("tool_input"))

    def record():
        db.set_state(name, "waiting", "PermissionRequest", message="permission: " + summary, attention=True)
        db.add_event(name, "PermissionRequest", tool, summary, payload, agent=rows[name].get("agent"))
        return db.perm_add(name, tool, summary, payload.get("tool_input"))
    pid = await asyncio.to_thread(record)
    _invalidate_scan()

    def attached() -> int:
        try:
            return int((tmux.list_sessions().get(name) or {}).get("attached") or 0)
        except tmux.TmuxError:
            return 0
    if await asyncio.to_thread(attached) > 0:
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
    return {"behavior": None, "reason": "undecided", "id": pid}


@app.post("/api/permission/{pid}/{decision}")
def api_permission_decide(pid: int, decision: str, request: Request):
    if decision not in ("allow", "deny"):
        raise projects.BadRequest("decision must be allow or deny")
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


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
