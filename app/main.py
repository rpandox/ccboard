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
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import claude_auth, clonequeue, cost, github, gitops, hooks, notify, permissions, previews, projects, prpoll, push, recover, scheduler, search, tasks, tmux, usage
from .auth import csrf_ok, identify
from .config import settings
from .db import DB, now as db_now

log = logging.getLogger("ccboard")
STATIC = Path(__file__).parent / "static"
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
LAUNCHERS = ("claude", "resume", "continue", "shell")
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
    sched_worker = scheduler.Worker(db)
    sched = sched_worker
    sched_worker.start()
    try:
        rec = recover.run(db, _start_session)
        if rec["recovered"] or rec["closed"]:
            log.info("recovery: relaunched %s, closed %s", rec["recovered"], rec["closed"])
    except Exception as e:
        log.warning("recovery failed: %s", e)
    log.info("ccboard on %s, projects in %s, allowlist=%s", settings.loopback_url(), settings.projects_dir,
             sorted(settings.allowed_users) or ("DEV BYPASS" if settings.dev_bypass_user else "EMPTY"))
    yield
    poller.stop.set()
    cloner.stop.set()
    prp.stop.set()
    indexer.stop.set()
    sched_worker.stop.set()


app = FastAPI(title="ccboard", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path == "/healthz":
        return PlainTextResponse("ok")
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
        # Always revalidate the shell and its assets (ETag), so an update never mixes old HTML with new JS.
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
    st["config"] = {"code_https_port": settings.code_https_port, "projects_dir": str(settings.projects_dir),
                    "ntfy": {"enabled": notify.enabled(), "subscribe_url": notify.subscribe_url(), "topic": settings.ntfy_topic},
                    "public_url": settings.public_url}
    st["claude"] = claude_auth.status()
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
        items.append({"project": project, "repo": projects.check_name("repo", str(name)), "url": url})
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


# ---------- tasks (worktree + branch per task) ----------

def _tasks_view() -> list[dict]:
    sessions, _ = _merged_sessions()
    out = []
    for t in db.tasks():
        s = sessions.get(t["tmux_name"])
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
            "session": {"state": s["state"], "state_at": s["state_at"], "last_message": s["last_message"],
                        "needs_attention": s["needs_attention"], "command": s["command"]} if s else None,
        })
    return out


class TaskIn(BaseModel):
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
    real = _start_session(name, project, repo, session, "task", str(rpath), cmd_line=cmd_line, claude_session_id=sid,
                          add_dirs=add_dirs)
    tid = db.task_add(project=project, repo=repo, slug=slug, title=title, prompt=prompt, branch=f"worktree-{slug}",
                      base=tasks.default_branch(rpath), worktree=str(tasks.worktree_path(rpath, slug)), tmux_name=real,
                      claude_session_id=sid)
    _invalidate_scan()
    return {"id": tid, "slug": slug, "tmux": real, "branch": f"worktree-{slug}", "attach_url": f"/term/{real}"}


def _task_or_404(tid: int) -> tuple[dict, Path]:
    t = db.task_get(tid)
    if not t:
        raise projects.NotFound("no such task")
    return t, Path(t["worktree"])


@app.get("/api/tasks/{tid}/diff")
def api_task_diff(tid: int):
    t, wt = _task_or_404(tid)
    return {"task": tid, **gitops.task_diff(wt, t["base"] or "main")}


@app.post("/api/tasks/{tid}/describe")
def api_task_describe(tid: int):
    t, wt = _task_or_404(tid)
    return gitops.describe(wt, t["base"] or "main", t["title"], t["prompt"])


class PrIn(BaseModel):
    title: str
    body: str = ""
    draft: bool = False


@app.post("/api/tasks/{tid}/pr")
def api_task_pr(tid: int, body: PrIn):
    t, wt = _task_or_404(tid)
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
    t, wt = _task_or_404(tid)
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
    if tmux.has_session(t["tmux_name"]):
        tmux.kill_session(t["tmux_name"])
        db.end(t["tmux_name"])
    err = tasks.remove_worktree(rpath, t["slug"], force=True) if rpath.is_dir() else None
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
    t, wt = _task_or_404(tid)
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
        _start_session(name, t["project"], t["repo"], session, "task", str(wt), cmd_line="claude --continue",
                       claude_session_id=t.get("claude_session_id"), add_dirs=[])
        relaunched = True
        time.sleep(4)  # let claude come up before pasting
    tmux.paste_text(name, prompt, enter=True)
    db.add_event(name, "FixCI", run_name, f"CI logs sent ({len(log_text)} chars)", {"run": run_name})
    db.set_state(name, "working", "FixCI", prompt=prompt[:500])
    _invalidate_scan()
    return {"ok": True, "relaunched": relaunched, "run": run_name, "chars": len(log_text)}


@app.post("/api/tasks/{tid}/refresh")
def api_task_refresh(tid: int):
    t, wt = _task_or_404(tid)
    if not t.get("pr_number"):
        raise projects.BadRequest("no PR for this task")
    cwd = wt if wt.is_dir() else projects.repo_path(t["project"], t["repo"])
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
    hp = t.get("preview_https") or previews.allocate_https_port(db.preview_ports_in_use())
    url = previews.serve_on(hp, port)
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


def _drop_preview(t: dict) -> None:
    if t.get("preview_https"):
        previews.serve_off(int(t["preview_https"]))
        db.task_update(t["id"], preview_port=None, preview_https=None)


class ArchiveIn(BaseModel):
    force: bool = False


@app.post("/api/tasks/{tid}/archive")
def api_archive_task(tid: int, body: ArchiveIn | None = None):
    t = db.task_get(tid)
    if not t:
        raise projects.NotFound("no such task")
    force = bool(body and body.force)
    _drop_preview(t)
    if tmux.has_session(t["tmux_name"]):
        tmux.kill_session(t["tmux_name"])
        db.end(t["tmux_name"])
    rpath = projects.repo_path(t["project"], t["repo"])
    err = tasks.remove_worktree(rpath, t["slug"], force=force) if rpath.is_dir() else None
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
    if enabled and j.get("cron"):
        fields["next_run_at"] = scheduler.next_fire(j["cron"])
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
    if not t or not Path(t["worktree"]).is_dir():
        raise projects.NotFound("worktree is gone")
    name = t["tmux_name"]
    if not tmux.has_session(name):
        _, _, session = tmux.split_name(name)
        cmd = ["claude", "--resume", r["session_id"]] if r.get("session_id") else ["claude", "--continue"]
        _start_session(name, t["project"], t["repo"], session, "task", t["worktree"], cmd_line=shlex.join(cmd),
                       claude_session_id=r.get("session_id"), add_dirs=[])
    _invalidate_scan()
    return {"tmux": name, "attach_url": f"/term/{name}"}


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
    env = {"CCBOARD_SESSION": name, "CCBOARD_URL": settings.loopback_url(),
           "CCBOARD_APPROVE_TIMEOUT": str(int(settings.approve_timeout))}
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
        if len(body.text) > 2000 or any(ord(c) < 32 and c not in "\t" for c in body.text):
            raise projects.BadRequest("text too long or contains control characters")
        tmux.send_text(name, body.text, enter=body.enter)
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
    if not tmux.kill_session(name):
        raise projects.NotFound(f"session {name} not found")
    db.end(name)
    _invalidate_scan()
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
            result = hooks.apply(db, name, event, payload)
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
        db.add_event(name, "PermissionRequest", tool, summary, payload)
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
    return FileResponse(STATIC / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
