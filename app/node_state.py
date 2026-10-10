"""The trimmed, read-only state a paired node reads from this board: `GET /api/node/state` (nodes epic, issue #138).

A hub does not read `GET /api/state` (accounts, usage, doctor-sized lists, a `version` that reloads every open page, node-local ids). It reads this
small body instead, built from the project scan the page poll already made (the 2 s scan cache in app/main.py), the task table and a few counters.
The caller is a paired node with scope `read`, or a signed-in person. What the body holds, field by field (nothing else is ever added by spreading a
scan row, so a field a later change gives a session cannot leak by itself):

  api, node{id, name, url, version, now}, etag_base
  projects[{name, repos[{name, slug, branch, dirty}]}]     slug = `owner/name` of the repo's `origin` when that is a GitHub remote, else null
  sessions[{tmux, project, repo, session, agent, state, needs_you, kind, since, model}]      kind = how the session was started (task, user, external ...)
  tasks[{id, title, phase, agent, project, repo, branch, tmux, issue_ref, updated_at}]       issue_ref = "#<number>" or null
  needs_you{permissions, input, errors}, usage{claude, codex}, lanes{cap, running, free}, login_problems[agent], truncated

No prompt, reply, result, transcript, account label, e-mail address or path (project and repo names only). Task titles and session names are shown to
every paired node; the README says so. Strings are cut to 200 characters, control characters dropped; at most 200 sessions and 200 tasks (newest and
not-finished first); the encoded body is at most 100 KB: if it is larger, finished tasks older than 24 hours go first, then ended sessions, then the
tail of the lists; `truncated` is true whenever any row was left out for any of these reasons.

`etag_base` is the SHA-256 (first 32 hex characters) of the canonical JSON of the body without `node.now` and without `etag_base`; the weak ETag is
`W/"<etag_base>"`. The same state therefore gives the same ETag, whatever the clock says, and `If-None-Match` answers 304 with no body.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import login_problem, nodes, projects, scheduler
from .config import settings
from .db import iso

log = logging.getLogger("ccboard.node_state")

SESSIONS_MAX = 200
TASKS_MAX = 200
PROJECTS_MAX = 100
REPOS_MAX = 100                       # per project
STR_MAX = 200
BODY_MAX = 100 * 1024                 # the encoded body, in bytes
FINISHED_AGE = 24 * 3600              # seconds: a finished task older than this is the first thing dropped from a body that is too large
FINISHED = ("done", "failed", "cancelled")
SLUG_TTL = 600.0
SLUG_CACHE_MAX = 512
ETAG_LEN = 32
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
_SLUG = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_GITHUB = (re.compile(r"^(?:https?|ssh|git)://(?:[^/@]+@)?github\.com(?::\d+)?/([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE),
           re.compile(r"^(?:[^@/\s]+@)?github\.com:([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.IGNORECASE))


def clean(v, n: int = STR_MAX) -> str | None:
    """A string for the wire: control characters become nothing, whitespace is collapsed, at most `n` characters; None for anything but a non-empty string."""
    if not isinstance(v, str):
        return None
    t = " ".join(_CTRL.sub(" ", v).split())[:n]
    return t or None


def github_slug(url) -> str | None:
    """`owner/name` of a GitHub remote URL (https, ssh:// or scp style), None for any other host or text. A credential in the URL is dropped with the rest."""
    u = (url or "").strip() if isinstance(url, str) else ""
    for rx in _GITHUB:
        m = rx.match(u)
        if m:
            slug = f"{m.group(1)}/{m.group(2)}"
            return slug if _SLUG.fullmatch(slug) else None
    return None


_slug_cache: dict[str, tuple[float, str | None]] = {}
_slug_lock = threading.Lock()


def _origin_url(path: Path) -> str | None:
    cp = projects._git(path, "remote", "get-url", "origin")
    return cp.stdout.strip() if cp is not None and cp.returncode == 0 else None


def repo_slug(path) -> str | None:
    """The GitHub slug of the repo at `path`, remembered for SLUG_TTL seconds (one `git remote get-url origin` per repo and ten minutes; the page poll never runs it)."""
    key = str(path)
    now = time.monotonic()
    with _slug_lock:
        hit = _slug_cache.get(key)
        if hit and now - hit[0] < SLUG_TTL:
            return hit[1]
    try:
        slug = github_slug(_origin_url(Path(key)))
    except Exception as e:
        log.debug("origin of a repo could not be read: %s", e.__class__.__name__)
        slug = None
    with _slug_lock:
        if len(_slug_cache) >= SLUG_CACHE_MAX:
            _slug_cache.clear()
        _slug_cache[key] = (now, slug)
    return slug


def slug_cache_clear() -> None:
    with _slug_lock:
        _slug_cache.clear()


def _groups(projs):
    """(project name, repo name, session dict) over every session of the scan: repo sessions, the project folder's (repo `root`) and the orphans."""
    for p in projs or []:
        pname = p.get("name")
        for r in p.get("repos") or []:
            for s in r.get("sessions") or []:
                yield pname, r.get("name"), s
        for s in (p.get("root") or {}).get("sessions") or []:
            yield pname, projects.ROOT, s
        for s in p.get("orphan_sessions") or []:
            yield pname, s.get("repo"), s


def _session_row(project, repo, s: dict) -> dict:
    stats = s.get("stats") if isinstance(s.get("stats"), dict) else {}
    return {"tmux": clean(s.get("tmux")), "project": clean(project), "repo": clean(repo), "session": clean(s.get("name")),
            "agent": clean(s.get("agent"), 20), "state": clean(s.get("state"), 20), "needs_you": bool(s.get("needs_attention")),
            "kind": clean(s.get("launcher"), 20), "since": clean(s.get("state_at"), 40), "model": clean(stats.get("model"), 80)}


def _task_row(t: dict) -> dict:
    n = t.get("issue_number")
    return {"id": t.get("id") if isinstance(t.get("id"), int) else None, "title": clean(t.get("title")), "phase": clean(t.get("phase") or "running", 20),
            "agent": clean(t.get("agent") or "claude", 20), "project": clean(t.get("project")), "repo": clean(t.get("repo")),
            "branch": clean(t.get("branch")), "tmux": clean(t.get("tmux_name")),
            "issue_ref": f"#{n}" if isinstance(n, int) and not isinstance(n, bool) and n > 0 else None, "updated_at": clean(t.get("updated_at"), 40)}


def _usage(db) -> dict:
    out = {}
    for agent in ("claude", "codex"):
        try:
            q = scheduler.quota_state(db, agent)
            w = nodes._window(q.get("pct"), q.get("resets_at"), q.get("backoff_until"))
            out[agent] = {**w, "limited": nodes._limited(w)}
        except Exception as e:
            log.debug("usage window of %s failed: %s", agent, e.__class__.__name__)
            out[agent] = {"pct": None, "resets_at": None, "backoff_until": None, "known": False, "limited": False}
    return out


def _problems(db) -> list[str]:
    try:
        p = login_problem.get(db)
    except Exception:
        return []
    a = clean((p or {}).get("agent"), 20)
    return [a] if a else []


def _size(body: dict) -> int:
    return len(json.dumps(body, separators=(",", ":")).encode("utf-8"))


def digest(body: dict) -> str:
    """etag_base: the canonical JSON of the body without `node.now` and without `etag_base`."""
    b = {k: v for k, v in body.items() if k != "etag_base"}
    b["node"] = {k: v for k, v in (b.get("node") or {}).items() if k != "now"}
    return hashlib.sha256(json.dumps(b, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()[:ETAG_LEN]


def build(db, scan: dict) -> dict:
    """The body for `scan` (the scan cache's {tmux_down, projects, ...}); nothing in it is a reference to the scan. Already within the caps."""
    projs = scan.get("projects") or []
    truncated = False
    sess_all = [_session_row(p, r, s) for p, r, s in _groups(projs)]
    sess_all = [s for s in sess_all if s["tmux"]]
    needs = {"permissions": len(db.perm_pending()),
             "input": sum(1 for s in sess_all if s["needs_you"] and s["state"] in ("waiting", "done")),
             "errors": sum(1 for s in sess_all if s["needs_you"] and s["state"] == "errored")}
    sess = sorted(sess_all, key=lambda s: s["state"] == "ended")             # stable: live sessions first, the scan's order inside each group
    if len(sess) > SESSIONS_MAX:
        sess, truncated = sess[:SESSIONS_MAX], True
    rows = [_task_row(t) for t in db.tasks()]
    active = [t for t in rows if t["phase"] not in FINISHED]
    finished = sorted((t for t in rows if t["phase"] in FINISHED), key=lambda t: t["updated_at"] or "", reverse=True)
    tasks = (active + finished)
    if len(tasks) > TASKS_MAX:
        tasks, truncated = tasks[:TASKS_MAX], True
    plist = []
    for p in projs[:PROJECTS_MAX]:
        repos = []
        for r in (p.get("repos") or [])[:REPOS_MAX]:
            git = r.get("state") == "ok"
            repos.append({"name": clean(r.get("name")), "slug": repo_slug(r["path"]) if git and r.get("path") else None, "branch": clean(r.get("branch")),
                          "dirty": r["dirty"] if isinstance(r.get("dirty"), bool) else None})
        if len(p.get("repos") or []) > REPOS_MAX:
            truncated = True
        plist.append({"name": clean(p.get("name")), "repos": repos})
    if len(projs) > PROJECTS_MAX:
        truncated = True
    body = {"api": nodes.API_VERSION,
            "node": {"id": nodes.node_id(), "name": nodes.display_name(), "url": nodes.public_url(), "version": settings.image_version or None,
                     "now": datetime.now(timezone.utc).isoformat(timespec="milliseconds")},
            "etag_base": "0" * ETAG_LEN, "projects": plist, "sessions": sess, "tasks": tasks, "needs_you": needs, "usage": _usage(db),
            "lanes": nodes.lanes_view(db), "login_problems": _problems(db), "truncated": truncated}
    return fit(body)


def fit(body: dict) -> dict:
    """Make the encoded body at most BODY_MAX bytes and set `etag_base`. Order: finished tasks older than 24 hours, then ended sessions, then (only if it is
    still too large) 10 rows at a time from the tail of the longer of the task and session lists (the project list last). `truncated` is set when a row went."""
    if _size(body) > BODY_MAX:
        cutoff = iso(nodes._now() - FINISHED_AGE)
        keep = [t for t in body["tasks"] if not (t["phase"] in FINISHED and (t["updated_at"] or "") < cutoff)]
        if len(keep) != len(body["tasks"]):
            body["tasks"], body["truncated"] = keep, True
    if _size(body) > BODY_MAX:
        keep = [s for s in body["sessions"] if s["state"] != "ended"]
        if len(keep) != len(body["sessions"]):
            body["sessions"], body["truncated"] = keep, True
    while _size(body) > BODY_MAX and (body["tasks"] or body["sessions"] or body["projects"]):
        key = "tasks" if len(body["tasks"]) >= len(body["sessions"]) and body["tasks"] else "sessions" if body["sessions"] else "projects"
        del body[key][-10:]                                  # the tail of the longer list: oldest finished tasks, then the rest
        body["truncated"] = True
    body["etag_base"] = digest(body)
    return body


def etag(body: dict) -> str:
    return f'W/"{body["etag_base"]}"'


def matches(header: str | None, tag: str) -> bool:
    """Does an If-None-Match header name `tag` (weak comparison; `*` matches anything)?"""
    if not header:
        return False
    bare = tag[2:] if tag.startswith("W/") else tag
    for part in header.split(","):
        p = part.strip()
        if p == "*" or (p[2:] if p.startswith("W/") else p) == bare:
            return True
    return False


def encode(body: dict) -> bytes:
    return json.dumps(body, separators=(",", ":")).encode("utf-8")
