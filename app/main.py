"""ccboard: a status-and-attention layer over Claude Code CLI sessions in tmux."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import claude_auth, hooks, projects, tmux
from .auth import csrf_ok, identify
from .config import settings
from .db import DB, now as db_now

log = logging.getLogger("ccboard")
STATIC = Path(__file__).parent / "static"
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
LAUNCHERS = ("claude", "resume", "continue", "shell")
MAX_ARGS = 1024

db: DB | None = None
_scan_lock = threading.Lock()
_scan_cache: tuple[float, dict] | None = None
SCAN_TTL = 2.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    settings.validate()
    db = DB(settings.db_path)
    hooks.ensure_token()
    log.info("ccboard on %s, projects in %s, allowlist=%s", settings.loopback_url(), settings.projects_dir,
             sorted(settings.allowed_users) or ("DEV BYPASS" if settings.dev_bypass_user else "EMPTY"))
    yield


app = FastAPI(title="ccboard", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path == "/healthz":
        return PlainTextResponse("ok")
    if request.url.path == "/api/hook":
        # Hooks run on the box itself (no Tailscale identity); they carry the local token instead.
        if request.method != "POST" or not hooks.check_token(request.headers.get(hooks.TOKEN_HEADER)):
            return JSONResponse({"error": "bad hook token"}, status_code=403)
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


@app.exception_handler(tmux.TmuxDown)
async def _down(_, e):
    return JSONResponse({"error": "ccboard-tmux is not running (sudo systemctl start ccboard-tmux)"}, status_code=503)


@app.exception_handler(tmux.TmuxError)
async def _tmuxerr(_, e):
    return JSONResponse({"error": f"tmux: {e}"}, status_code=500)


# ---------- state ----------

def _merged_sessions() -> tuple[dict[str, dict], bool]:
    """tmux sessions (non-internal) merged with DB rows. Returns (sessions, tmux_down)."""
    snapshot_at = db_now()
    try:
        live = tmux.list_sessions()
    except tmux.TmuxDown:
        return {}, True
    assert db is not None
    db.reconcile(set(live.keys()), before=snapshot_at)
    rows = db.open_rows()
    out: dict[str, dict] = {}
    for name, s in live.items():
        if tmux.is_internal(name):
            continue
        row = rows.get(name, {})
        out[name] = {
            "created": s["created"], "attached": s["attached"], "command": s["command"], "path": s["path"],
            "pane_id": s["pane_id"], "launcher": row.get("launcher", "external"), "cmd": row.get("cmd"),
            "claude_session_id": row.get("claude_session_id"), "add_dirs": row.get("add_dirs", []),
            "state": row.get("state") or "unknown", "state_at": row.get("state_at"), "last_event": row.get("last_event"),
            "last_message": row.get("last_message"), "last_prompt": row.get("last_prompt"), "stats": row.get("stats"),
            "needs_attention": bool(row.get("state") in hooks.ATTENTION_STATES and not row.get("acked_at")),
        }
    return out, False


def build_state(user: str) -> dict:
    global _scan_cache
    with _scan_lock:
        if _scan_cache and time.monotonic() - _scan_cache[0] < SCAN_TTL:
            st = dict(_scan_cache[1])
        else:
            sessions, down = _merged_sessions()
            st = {"tmux_down": down, "projects": projects.scan(sessions)}
            _scan_cache = (time.monotonic(), st)
    st["user"] = user
    st["config"] = {"code_https_port": settings.code_https_port, "projects_dir": str(settings.projects_dir)}
    st["claude"] = claude_auth.status()
    st["login"] = claude_auth.login_state()
    st["usage"] = db.kv_get("rate_limits")
    st["rate_limited"] = db.kv_get("rate_limited")
    return st


def _invalidate_scan() -> None:
    global _scan_cache
    with _scan_lock:
        _scan_cache = None


@app.get("/api/state")
def api_state(request: Request):
    return build_state(request.state.user)


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
                       add_dirs=[])
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


@app.delete("/api/projects/{project}")
def api_delete_project(project: str):
    p = projects.project_path(project)
    if not p.is_dir():
        raise projects.NotFound(f"project {project} not found")
    killed = tmux.kill_prefix(project + tmux.SEP)
    for k in killed:
        db.end(k)
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
        db.end(k)
    projects.remove_tree(r)
    _invalidate_scan()
    return {"removed": f"{project}/{repo}", "killed_sessions": killed}


# ---------- sessions ----------

class SessionIn(BaseModel):
    launcher: str
    name: str | None = None
    args: str | None = None
    resume_id: str | None = None
    add_dirs: list[str] | None = None  # "project/repo" ids


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


def _start_session(name: str, project: str, repo: str, session: str, launcher: str, cwd: str, *,
                   cmd_line: str | None, claude_session_id: str | None, add_dirs: list[str]) -> str:
    env = {"CCBOARD_SESSION": name, "CCBOARD_URL": settings.loopback_url()}
    real = tmux.new_session(name, cwd, env=env)
    db.add_session(tmux_name=real, project=project, repo=repo, name=session, launcher=launcher, cmd=cmd_line,
                   claude_session_id=claude_session_id, add_dirs=add_dirs)
    if cmd_line:
        try:
            tmux.send_line(real, cmd_line)
        except tmux.TmuxError:
            tmux.kill_session(real)
            db.end(real)
            raise
    return real


@app.post("/api/projects/{project}/repos/{repo}/sessions", status_code=201)
def api_create_session(project: str, repo: str, body: SessionIn):
    if body.launcher not in LAUNCHERS:
        raise projects.BadRequest(f"launcher must be one of {', '.join(LAUNCHERS)}")
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
    claude_session_id = None
    add_dirs: list[str] = []
    if body.launcher != "shell":
        exe = settings.claude_bin()
        if not exe:
            raise projects.BadRequest("claude is not installed on this box")
        add_dirs = _resolve_add_dirs(body.add_dirs, rpath)
        cmd = ["claude"]
        if body.launcher == "claude":
            claude_session_id = str(uuid.uuid4())
            cmd += ["--session-id", claude_session_id]
        elif body.launcher == "resume":
            cmd += ["--resume"] + ([body.resume_id] if body.resume_id else [])
        elif body.launcher == "continue":
            cmd += ["--continue"]
        cmd += extra
        if add_dirs:
            cmd += ["--add-dir", *add_dirs]
        cmd_line = shlex.join(cmd)

    real = _start_session(name, project, repo, session, body.launcher, str(rpath), cmd_line=cmd_line,
                          claude_session_id=claude_session_id, add_dirs=add_dirs)
    _invalidate_scan()
    return {"tmux": real, "attach_url": f"/tty/?arg={real}", "claude_session_id": claude_session_id,
            "cmd": cmd_line}


@app.delete("/api/sessions/{name}")
def api_kill_session(name: str):
    try:
        tmux.split_name(name)
    except ValueError:
        raise projects.BadRequest("not a ccboard session name")
    if not tmux.kill_session(name):
        raise projects.NotFound(f"session {name} not found")
    db.end(name)
    _invalidate_scan()
    return {"killed": name}


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
        result = hooks.apply(db, name, event, payload)
        _invalidate_scan()
        return {**result, "how": how}

    return await asyncio.to_thread(work)


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


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
