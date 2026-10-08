"""The Memory proxy (v0.5.20): one ccboard project's claude-mem memory, read from the loopback worker for the Memory page.

The browser never talks to the worker: the board is its only client, GET only, loopback only (app/memory.py fetch()), with a fixed
table of worker endpoints and whitelisted, url-encoded query values. No part of a worker path comes from the caller (the one id in a
path, /api/observation/<id>, is an int the proxy formats itself). The answers, their flags and the degraded states are written down
in docs/memory-api.md; the route handlers in app/main.py are thin.

    keys_for(project)    the claude-mem project keys of a ccboard project: each repo folder's name, the project folder's own name
                         (`root`), `<repo>/<slug>` for each task worktree (Claude .claude/worktrees, ccboard .ccboard/worktrees), the
                         names CLAUDE_MEM_PROJECT_ENVIRONMENTS gives the project's folders, intersected with the worker's /api/projects
                         (60 s cache; worker keys `<repo>/<anything>` of the project's repos are added, so an ended worktree's memory
                         stays visible). Noise keys (the unix user, tmp, a notes folder, hidden agent folders) never match.
    observations()       /api/observations fanned out per key (at most 4 in flight, one ~3 s budget), merged newest first, paged
    summaries()          with limit/offset and the `before` cursor, filtered after the merge (type, repo, agent, session, range).
    search()             /api/search with query=, projects=a,b (one call, the worker's own union) and format=json; per key on refusal.
    timeline()           /api/timeline around one observation id (markdown only, parsed into rows; errors come as 200 + isError).
    palace()             wings (repos), rooms (concepts and types), drawers (latest observations), gotchas; cached 30 s.
    writeback_task()     the write-back of a finished task's result (off by default, memory.save_note), the one write.

Every worker answer is checked against SHAPES (shared with tests/test_memory_contract.py); a mismatch is state 'incompatible', never a
half-parsed list. A last-good cache per worker URL serves `stale: true` when the worker is down or slow; an error is never cached and
never turned into an empty list. No worker body is ever logged (observations carry narratives, paths and tool output).

Import rule: this module imports memory, projects, tasks and config; app/memory.py stays the low-level client (it imports config only).
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from . import memory, projects, tasks
from .config import settings

log = logging.getLogger("ccboard.memory")

# ------------------------------------------------------------------ the worker's names (pinned)
# Verified live against claude-mem worker 13.31.0 on the box (box check V11, issue #12, 2026-10-08) and again against the running 13.34.2
# worker (2026-10-08, GET only: /api/stats, /api/projects, /api/observations, /api/summaries, /api/search with format=json, each answer
# passed check_shape with no drift), so TESTED_WORKER is 13.34.2. The scrubbed fixtures in tests/fixtures are still the 13.31.0 answers.
# tests/test_memory_contract.py pins every name below; a rename in a plugin update shows there and as state 'incompatible' at runtime.
TESTED_WORKER = "13.34.2"
ENDPOINTS = {
    "projects": "/api/projects",           # {projects: [str], sources, projectsBySource}; never empty while the worker is up
    "observations": "/api/observations",   # {items, hasMore, offset, limit}, newest first, limit <= 100
    "summaries": "/api/summaries",         # the same wrapper
    "search": "/api/search",               # format=json: {observations, sessions, prompts, totalResults, query}
    "timeline": "/api/timeline",           # markdown only: {content: [{type: text, text}]}, errors as HTTP 200 + isError: true
    "observation": "/api/observation/",    # + <int id>: one full row (agent_type, discovery_tokens), 404 {error} when missing
    "stats": "/api/stats",                 # {worker: {version, ...}, database: {observations, sessions, summaries, ...}}
}
P_QUERY = "query"                 # NOT q: /api/search ignores q= and searches for the text "undefined"
P_PROJECT = "project"             # one key (exact, case-insensitive, also matches merged_into_project)
P_PROJECTS = "projects"           # several keys, comma separated: the worker returns their union
P_FORMAT, FORMAT_JSON = "format", "json"     # /api/search answers markdown unless format=json
P_LIMIT, P_OFFSET = "limit", "offset"
P_PLATFORM = "platformSource"     # claude | codex (nothing writes codex rows on the box yet)
P_SESSION = "contentSessionId"
P_TYPE = "type"                   # search: observations | sessions | prompts
P_OBS_TYPE = "obs_type"           # search: bugfix, feature, ... (comma list)
P_ORDER = "orderBy"               # search: relevance | date_desc | date_asc
P_ANCHOR = "anchor"               # timeline: an observation id
P_DEPTH_BEFORE, P_DEPTH_AFTER = "depth_before", "depth_after"
WORKER_PAGE = 100                 # the list routes clamp limit to 100
JSON_LIST_FIELDS = ("facts", "concepts", "files_read", "files_modified", "files_edited")     # JSON-encoded strings in the worker
SUBAGENT_TYPE = "workflow-subagent"        # observations.agent_type of a workflow subagent (null for a main session)
INVALID_PROJECTS = "INVALID_PROJECTS"      # the 400 code of a projects= value the worker refuses

# ------------------------------------------------------------------ budgets and caps
PROXY_CAP = 2 * 1024 * 1024       # bytes of one worker answer; more is an error (memory.TooLarge), never a silently empty list
FETCH_TIMEOUT = 5.0               # one worker request: a cold read on the box's spinning disk took over 2 s (warm ones take ms)
BUDGET = 6.0                      # one board request (a fan-out included)
SEARCH_BUDGET = 6.0               # a text search: the box saw a broad one take 15.7 s; past this it reads 'slow', never 'down'
FANOUT = 4                        # worker requests in flight for one board request
SCAN_CAP = 300                    # rows read per key for one page (the `before` cursor over-fetches up to here)
PAGE_DEFAULT, PAGE_MAX = 50, 100
OFFSET_MAX = 300
CACHE_FRESH = 5.0                 # seconds an answer is served from the cache without asking the worker
CACHE_MAX_ENTRIES = 200
CACHE_MAX_BYTES = 16 * 1024 * 1024
PROJECTS_TTL = 60.0               # the worker's project list
PALACE_TTL = 30.0
PALACE_SCAN = 300                 # the newest observations a palace is built from
DRAWERS_DEFAULT, DRAWERS_MAX = 8, 20
GOTCHAS = 3
NARRATIVE_MAX = 2000              # characters of a drawer's narrative (the drawer expands to it without another call)
FACTS_MAX, FACT_MAX = 10, 500
CONCEPT_ROOMS = ("gotcha", "problem-solution", "pattern", "how-it-works")       # first, in this order (the inventory's order)
TYPE_ROOMS = ("bugfix", "feature", "change", "decision", "discovery")
AGENTS = ("claude", "codex")
ENV_SETTING = "CLAUDE_MEM_PROJECT_ENVIRONMENTS"

clock = time.monotonic            # patched by tests (cache freshness, palace TTL)


# ------------------------------------------------------------------ errors and compat

class WorkerError(Exception):
    """The worker could not give an answer: state 'down' (refused, a non-loopback host) or 'degraded' (slow, an HTTP error, too big, a
    worker-side error). `code` is one of refused | not_loopback | timeout | http | too_large | worker_error; `reason` is one line."""

    def __init__(self, state: str, code: str, reason: str, status: int | None = None):
        super().__init__(reason)
        self.state, self.code, self.reason, self.status = state, code, reason, status

    def body(self) -> dict:
        return {"error": self.reason, "state": self.state, "up": False, "reason": self.reason, "reason_code": self.code}


class ShapeError(Exception):
    """A worker answer that does not have the shape SHAPES describes (a renamed field, a list that became an object, an empty body)."""


def _vtuple(v) -> tuple[int, int, int] | None:
    m = re.match(r"^\s*v?(\d+)\.(\d+)\.(\d+)", str(v or ""))
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def compat(version) -> str:
    """ok: the same major as TESTED_WORKER and not newer; untested: the same major, a newer minor or patch; unknown: another major or a
    version that cannot be read (never ok)."""
    v, t = _vtuple(version), _vtuple(TESTED_WORKER)
    if v is None or t is None or v[0] != t[0]:
        return "unknown"
    return "ok" if v <= t else "untested"


def compat_info(health: dict | None) -> dict:
    """{compat, worker_version, tested_worker} from a health() record (the monitor's last probe)."""
    ver = (health or {}).get("version")
    return {"compat": compat(ver), "worker_version": ver, "tested_worker": TESTED_WORKER}


# ------------------------------------------------------------------ shapes (shared with tests/test_memory_contract.py)

_INT, _STR_OR_NONE = "int", "str|None"
OBS_REQUIRED = {"id": _INT, "created_at_epoch": _INT, "title": _STR_OR_NONE, "type": _STR_OR_NONE}
SUMMARY_REQUIRED = {"id": _INT, "created_at_epoch": _INT}
SEARCH_SESSION_REQUIRED = {"id": _INT, "created_at_epoch": _INT}
PROMPT_REQUIRED = {"id": _INT, "created_at_epoch": _INT}
SHAPES = {
    "observations": {"lists": {"items": OBS_REQUIRED}},
    "summaries": {"lists": {"items": SUMMARY_REQUIRED}},
    "search": {"lists": {"observations": OBS_REQUIRED, "sessions": SEARCH_SESSION_REQUIRED, "prompts": PROMPT_REQUIRED}},
    "observation": {"row": OBS_REQUIRED},
    "projects": {"strings": "projects"},
    "stats": {"objects": ("worker", "database")},
    "timeline": {"text": True},
}


def _ok_value(v, rule: str) -> bool:
    if rule == _INT:
        return isinstance(v, int) and not isinstance(v, bool)
    return v is None or isinstance(v, str)


def _row_problem(row, required: dict, where: str) -> str | None:
    if not isinstance(row, dict):
        return f"{where} is not an object"
    for f, rule in required.items():
        if f not in row:
            return f"{where} has no '{f}' field"
        if not _ok_value(row[f], rule):
            return f"{where} has a '{f}' that is not {'a number' if rule == _INT else 'text'}"
    return None


def check_shape(kind: str, body) -> str | None:
    """None when `body` has the shape of a `kind` answer (SHAPES), else a one-line reason. Missing optional fields are fine."""
    spec = SHAPES[kind]
    if not isinstance(body, dict):
        return "the answer is not a JSON object" if body is not None else "the answer is empty or not JSON"
    for name, required in spec.get("lists", {}).items():
        if name not in body:
            return f"the answer has no '{name}' list"
        if not isinstance(body[name], list):
            return f"'{name}' is not a list"
        for i, row in enumerate(body[name]):
            p = _row_problem(row, required, f"{name}[{i}]")
            if p:
                return p
    if "row" in spec:
        return _row_problem(body, spec["row"], "the observation")
    if "strings" in spec:
        v = body.get(spec["strings"])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            return f"'{spec['strings']}' is not a list of names"
    for name in spec.get("objects", ()):
        if not isinstance(body.get(name), dict):
            return f"the answer has no '{name}' object"
    if spec.get("text"):
        c = body.get("content")
        if not isinstance(c, list) or not c or not all(isinstance(x, dict) and isinstance(x.get("text"), str) for x in c):
            return "the answer has no text content"
    return None


# ------------------------------------------------------------------ the last-good cache

class _Cache:
    """Last good answer per worker URL (path + sorted encoded params), bounded by entries and bytes, least recently used out first."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data: OrderedDict[str, tuple[float, str, object, int]] = OrderedDict()
        self.bytes = 0

    def get(self, url: str):
        with self.lock:
            hit = self.data.get(url)
            if hit is not None:
                self.data.move_to_end(url)
            return hit

    def put(self, url: str, body) -> None:
        size = len(json.dumps(body, separators=(",", ":")))
        if size > CACHE_MAX_BYTES // 4:
            return
        with self.lock:
            old = self.data.pop(url, None)
            if old is not None:
                self.bytes -= old[3]
            self.data[url] = (clock(), _now_iso(), body, size)
            self.bytes += size
            while self.data and (len(self.data) > CACHE_MAX_ENTRIES or self.bytes > CACHE_MAX_BYTES):
                _, ev = self.data.popitem(last=False)
                self.bytes -= ev[3]

    def clear(self) -> None:
        with self.lock:
            self.data.clear()
            self.bytes = 0


_cache = _Cache()
_palace_cache: dict[tuple, tuple[float, dict]] = {}
_palace_lock = threading.Lock()
_projects_cache: dict[str, object] = {}


def reset() -> None:
    """Forget every cached answer (tests)."""
    _cache.clear()
    with _palace_lock:
        _palace_cache.clear()
    _projects_cache.clear()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Answer:
    __slots__ = ("body", "stale_at", "stale_reason")

    def __init__(self, body, stale_at=None, stale_reason=None):
        self.body, self.stale_at, self.stale_reason = body, stale_at, stale_reason


def _url(name: str, params: dict | None, tail: str = "") -> str:
    path = ENDPOINTS[name] + tail
    clean = sorted((k, str(v)) for k, v in (params or {}).items() if v is not None and v != "")
    return path + ("?" + urlencode(clean) if clean else "")


def _call(name: str, params: dict | None, deadline: float, *, shape: str | None = None, tail: str = "",
          timeout: float = FETCH_TIMEOUT, allow_404: bool = False) -> _Answer:
    """One worker GET through the cache. A fresh entry (CACHE_FRESH) answers without a request; a failed request answers the last good
    entry as stale, else raises WorkerError. ShapeError for an answer of the wrong shape (never cached, never served stale)."""
    url = _url(name, params, tail)
    hit = _cache.get(url)
    if hit is not None and clock() - hit[0] < CACHE_FRESH:
        return _Answer(hit[2])
    try:
        body = _get(url, deadline, timeout, allow_404)
    except WorkerError as e:
        if hit is not None:
            return _Answer(hit[2], stale_at=hit[1], stale_reason=e)
        raise
    if body is None:                                # an allowed 404
        return _Answer(None)
    if shape:
        problem = check_shape(shape, body)
        if problem:
            raise ShapeError(f"{ENDPOINTS[name]}: {problem}")
    _cache.put(url, body)
    return _Answer(body)


def _get(url: str, deadline: float, timeout: float, allow_404: bool):
    left = deadline - clock()
    if left < 0.05:
        raise WorkerError("degraded", "timeout", "the worker is slow: the board's time for this answer ran out")
    try:
        base = memory.worker_base()
    except memory.NotLoopback as e:
        raise WorkerError("down", "not_loopback", str(e)) from None
    t = min(timeout, left)
    try:
        status, body = memory.fetch(base, url, timeout=t, cap=PROXY_CAP, strict=True)
    except memory.Refused:
        raise WorkerError("down", "refused", "connection refused: the claude-mem worker is not running") from None
    except memory.NoAnswer:
        raise WorkerError("degraded", "timeout", f"the worker is slow: no answer within {t:.1f} s") from None
    except memory.TooLarge:
        raise WorkerError("degraded", "too_large", f"the worker's answer is larger than {PROXY_CAP // (1024 * 1024)} MB") from None
    except memory.NotLoopback as e:
        raise WorkerError("down", "not_loopback", str(e)) from None
    if status == 404 and allow_404:
        return None
    if status != 200:
        code = body.get("code") if isinstance(body, dict) else None
        raise WorkerError("degraded", "http", f"the worker answered HTTP {status}" + (f" ({str(code)[:40]})" if code else ""), status)
    if isinstance(body, dict) and body.get("isError") is True:
        raise WorkerError("degraded", "worker_error", "the worker reported an error: " + _first_text(body)[:160])
    return body


def _first_text(body: dict) -> str:
    c = body.get("content")
    if isinstance(c, list):
        for x in c:
            if isinstance(x, dict) and isinstance(x.get("text"), str):
                return " ".join(x["text"].split())
    return "no message"


# ------------------------------------------------------------------ keys

def _project_dir(project: str) -> Path:
    p = projects.project_path(project)             # BadRequest for a bad name
    if not p.is_dir() or p.is_symlink():
        raise projects.NotFound(f"no project {project}")
    return p


def _repo_dirs(pdir: Path) -> list[tuple[str, Path]]:
    """(ccboard repo name, folder) of a project; the project folder itself when it is the repo."""
    if projects.is_repo(pdir):
        return [(pdir.name, pdir)]
    return [(c.name, c) for c in projects._subdirs(pdir) if c.name != projects.ROOT]


def _worktree_dirs(rdir: Path) -> list[Path]:
    out = []
    for sub in (tasks.WORKTREES, tasks.MANAGED_WORKTREES):
        d = rdir / sub
        try:
            out += [c for c in sorted(d.iterdir()) if c.is_dir() and not c.is_symlink() and not c.name.startswith(".")]
        except OSError:
            pass
    return out


def worktree_key(repo_folder: str, slug: str) -> str:
    """The key claude-mem gives a session in a git worktree: <parent repo folder>/<worktree folder>; a worktree folder named like its
    repo collapses to plain <repo> (plugin utils/project-name.ts, box check V11 row 10)."""
    return repo_folder if slug == repo_folder else f"{repo_folder}/{slug}"


def _env_entries() -> list[tuple[str, list[str]]]:
    """CLAUDE_MEM_PROJECT_ENVIRONMENTS: the env var wins over ~/.claude-mem/settings.json; a JSON array of {name, patterns[]} given as a
    JSON string or a native array (plugin project-environments.ts). Anything invalid is ignored, as the plugin does."""
    raw = os.environ.get(ENV_SETTING)
    if raw is None:
        cfg = memory._json_file(memory.mem_dir() / "settings.json")
        raw = cfg.get(ENV_SETTING) if isinstance(cfg, dict) else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    out = []
    for e in raw if isinstance(raw, list) else []:
        if isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"].strip() and isinstance(e.get("patterns"), list):
            pats = [p for p in e["patterns"] if isinstance(p, str) and p.strip()]
            if pats:
                out.append((e["name"].strip(), pats))
    return out


def _env_name(folder: Path, entries) -> str | None:
    s = str(folder)
    for name, pats in entries:
        for p in pats:
            p = os.path.expanduser(p)
            if fnmatch.fnmatchcase(s, p) or fnmatch.fnmatchcase(s + "/x", p):
                return name
    return None


def candidate_keys(project: str) -> list[dict]:
    """The keys this project's sessions write under, from the disk alone: [{key, wing, kind}] with kind repo | root | worktree | env.
    `wing` is the ccboard repo name ('root' for the project folder), the palace's wing."""
    pdir = _project_dir(project)
    env = _env_entries()
    out: list[dict] = []
    seen: set[str] = set()

    def add(key, wing, kind):
        if key and key.lower() not in seen:
            seen.add(key.lower())
            out.append({"key": key, "wing": wing, "kind": kind})

    for wing, rdir in _repo_dirs(pdir):
        en = _env_name(rdir, env)
        add(en or rdir.name, wing, "env" if en else "repo")
        for wt in _worktree_dirs(rdir):
            ew = _env_name(wt, env)
            add(ew or worktree_key(rdir.name, wt.name), wing, "env" if ew else "worktree")
    if not projects.is_repo(pdir):
        en = _env_name(pdir, env)
        add(en or pdir.name, projects.ROOT, "env" if en else "root")
    return out


def worker_projects(deadline: float) -> list[str] | None:
    """The worker's /api/projects list (cached PROJECTS_TTL; the last good one when the worker fails), or None when there is none."""
    hit = _projects_cache.get("v")
    if hit is not None and clock() - hit[0] < PROJECTS_TTL:
        return list(hit[1])
    try:
        ans = _call("projects", None, deadline, shape="projects")
    except (WorkerError, ShapeError):
        return list(hit[1]) if hit is not None else None
    names = [p for p in ans.body["projects"] if p]
    if names:                                      # an empty list means slow or down, never "no projects" (probe row 8)
        _projects_cache["v"] = (clock(), names)
    return names or (list(hit[1]) if hit is not None else None)


def _owners() -> dict[str, list[str]]:
    """key (lower case) -> ['project/repo', ...] over every ccboard project: the repo folders and the non-repo project folders (the
    doctor's _repo_dirs_by_name idea, plus the project folders, whose name is the key of a session started there)."""
    out: dict[str, list[str]] = {}
    for pdir in projects._subdirs(Path(settings.projects_dir)):
        if projects.is_repo(pdir):
            out.setdefault(pdir.name.lower(), []).append(pdir.name)
            continue
        out.setdefault(pdir.name.lower(), []).append(f"{pdir.name}/{projects.ROOT}")
        for c in projects._subdirs(pdir):
            if c.name != projects.ROOT:
                out.setdefault(c.name.lower(), []).append(f"{pdir.name}/{c.name}")
    return out


def keys_for(project: str, deadline: float | None = None) -> dict:
    """{keys: [{key, wing, kind}], ambiguous: [{key, shared_with}], checked: bool}. `checked` is False when the worker's project list
    could not be read (the disk keys are used as they are: still only this project's own keys)."""
    deadline = clock() + BUDGET if deadline is None else deadline
    pdir = _project_dir(project)
    cands = candidate_keys(project)
    listed = worker_projects(deadline)
    if listed is None:
        keys = cands
    else:
        by_lower = {k.lower(): k for k in listed}
        keys = [{**c, "key": by_lower[c["key"].lower()]} for c in cands if c["key"].lower() in by_lower]
        have = {k["key"].lower() for k in keys}
        for wing, rdir in _repo_dirs(pdir):                 # worktrees that are gone from the disk keep their memory under <repo>/<slug>
            pre = rdir.name.lower() + "/"
            for k in listed:
                if k.lower().startswith(pre) and k.lower() not in have:
                    have.add(k.lower())
                    keys.append({"key": k, "wing": wing, "kind": "worktree"})
    owners = _owners()
    mine = {project} if projects.is_repo(pdir) else {f"{project}/{w}" for w, _ in _repo_dirs(pdir)} | {f"{project}/{projects.ROOT}"}
    amb = []
    for k in keys:
        if k["kind"] == "env":
            continue
        others = [o for o in owners.get(k["key"].split("/")[0].lower(), []) if o not in mine]
        if others:
            amb.append({"key": k["key"], "shared_with": others})
    return {"keys": keys, "ambiguous": amb, "checked": listed is not None}


# ------------------------------------------------------------------ rows

def _json_list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        try:
            x = json.loads(v)
        except ValueError:
            return []
        return x if isinstance(x, list) else []
    return []


OBS_FIELDS = ("id", "type", "title", "subtitle", "narrative", "facts", "concepts", "files_read", "files_modified", "created_at",
              "created_at_epoch", "project", "merged_into_project", "platform_source", "memory_session_id", "content_session_id",
              "prompt_number", "discovery_tokens", "agent_type", "agent_id")
SUMMARY_FIELDS = ("id", "session_id", "memory_session_id", "request", "investigated", "learned", "completed", "next_steps", "notes",
                  "files_read", "files_edited", "project", "merged_into_project", "platform_source", "prompt_number", "discovery_tokens",
                  "created_at", "created_at_epoch")
PROMPT_FIELDS = ("id", "prompt_text", "prompt_number", "project", "content_session_id", "memory_session_id", "platform_source",
                 "created_at", "created_at_epoch")


def _norm(row: dict, fields, key: str | None, wing: str | None) -> dict:
    out = {f: row.get(f) for f in fields}
    for f in JSON_LIST_FIELDS:
        if f in out:
            out[f] = [x for x in _json_list(out[f]) if isinstance(x, (str, int, float))]
    out["key"], out["repo"] = key, wing
    return out


def _paths(row: dict) -> list[str]:
    return [p for f in ("files_read", "files_modified", "files_edited") for p in _json_list(row.get(f)) if isinstance(p, str)]


def _belongs(row: dict, project: str) -> bool:
    """The best-effort path filter for an ambiguous key: a row that names absolute paths keeps only when one lies under
    <PROJECTS_DIR>/<project>/; a row without absolute paths cannot be told apart and stays."""
    abs_paths = [p for p in _paths(row) if p.startswith("/")]
    if not abs_paths:
        return True
    pre = str(Path(settings.projects_dir) / project).rstrip("/") + "/"
    return any(p.startswith(pre) for p in abs_paths)


def _key_of(row: dict, by_lower: dict) -> dict | None:
    for f in ("project", "merged_into_project"):
        v = row.get(f)
        if isinstance(v, str) and v.lower() in by_lower:
            return by_lower[v.lower()]
    return None


# ------------------------------------------------------------------ answers

def _envelope(project: str, kf: dict, health: dict | None) -> dict:
    return {"project": project, "keys": [k["key"] for k in kf["keys"]], "keys_checked": kf["checked"], "state": "ok", "up": True,
            "stale": False, "stale_at": None, "reason": None, "reason_code": None, "partial": [], "ambiguous": kf["ambiguous"],
            "ambiguous_filter": "files" if kf["ambiguous"] else None, "shape_error": False, **compat_info(health), "at": _now_iso()}


def _incompatible(out: dict, reason: str, lists=("items",)) -> dict:
    out.update(state="incompatible", shape_error=True, reason_code="shape",
               reason=f"the worker answered in a shape this board does not know: {reason}")
    for name in lists:
        out[name] = []
    return out


def _mark_stale(out: dict, answers) -> None:
    stale = [a for a in answers if a is not None and a.stale_at]
    if stale:
        first = min(stale, key=lambda a: a.stale_at)
        out.update(stale=True, stale_at=first.stale_at, reason=f"showing the last good answer: {first.stale_reason.reason}",
                   reason_code=first.stale_reason.code)


def _fan(jobs: dict, deadline: float) -> tuple[dict, dict]:
    """Run jobs {name: fn} at most FANOUT at a time until the deadline. (results, errors): an error is the exception a job raised; a
    job that did not finish in time is a WorkerError timeout. The pool is never joined in the request path."""
    if not jobs:
        return {}, {}
    ex = ThreadPoolExecutor(max_workers=min(FANOUT, len(jobs)), thread_name_prefix="mem-proxy")
    futs = {ex.submit(fn): name for name, fn in jobs.items()}
    try:
        done, _ = wait(futs, timeout=max(0.0, deadline - clock()) + 0.05)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    results, errors = {}, {}
    for f, name in futs.items():
        if f in done and not f.cancelled():
            e = f.exception()
            if e is None:
                results[name] = f.result()
            else:
                errors[name] = e
        else:
            errors[name] = WorkerError("degraded", "timeout", "the worker is slow: this key did not answer in time")
    return results, errors


def _worst(errors) -> WorkerError:
    errs = [e for e in errors if isinstance(e, WorkerError)]
    for e in errs:
        if e.state == "down":
            return e
    return errs[0] if errs else WorkerError("degraded", "worker_error", "the worker could not answer")


def _int(v, name: str, lo: int, hi: int, default: int | None) -> int | None:
    if v is None or v == "":
        return default
    try:
        n = int(str(v).strip())
    except ValueError:
        raise projects.BadRequest(f"{name} must be a whole number") from None
    if not lo <= n <= hi:
        raise projects.BadRequest(f"{name} must be between {lo} and {hi}")
    return n


def _epoch_ms(v, name: str) -> int | None:
    """A time as epoch milliseconds: from ms, seconds (numbers below 1e11) or ISO text."""
    if v is None or v == "":
        return None
    s = str(v).strip()
    try:
        n = float(s)
        return int(n if n >= 1e11 else n * 1000)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise projects.BadRequest(f"{name} must be epoch milliseconds or an ISO time") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _filters(kind: str, type_=None, agent=None, session=None, since=None, until=None) -> dict:
    types = {t.strip() for t in str(type_ or "").split(",") if t.strip()} if kind == "observations" else set()
    if agent is not None and agent != "" and agent not in AGENTS:
        raise projects.BadRequest(f"agent must be one of {', '.join(AGENTS)}")
    if session is not None and session != "" and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(session)):
        raise projects.BadRequest("session must be a session id")
    return {"types": types, "agent": agent or None, "session": session or None, "since": _epoch_ms(since, "since"),
            "until": _epoch_ms(until, "until")}


def _passes(row: dict, f: dict, session_fields) -> bool:
    if f["types"] and row.get("type") not in f["types"]:
        return False
    if f["agent"] and row.get("platform_source") not in (None, f["agent"]):
        return False
    if f["session"] and f["session"] not in tuple(row.get(s) for s in session_fields):
        return False
    e = row.get("created_at_epoch")
    if f["until"] is not None and e > f["until"]:
        return False
    return True


def _listing(kind: str, project: str, *, limit=None, offset=None, before=None, repo=None, type_=None, agent=None, session=None,
             since=None, until=None, health: dict | None = None) -> dict:
    """observations() and summaries(): see the module docstring and docs/memory-api.md."""
    limit = _int(limit, "limit", 1, PAGE_MAX, PAGE_DEFAULT)
    offset = _int(offset, "offset", 0, OFFSET_MAX, 0)
    before = _epoch_ms(before, "before")
    f = _filters(kind, type_, agent, session, since, until)
    deadline = clock() + BUDGET
    kf = keys_for(project, deadline)
    if repo:
        projects.check_name("repo", repo)
        kf = {**kf, "keys": [k for k in kf["keys"] if k["wing"] == repo]}
    out = _envelope(project, kf, health)
    out.update(items=[], has_more=False, next_before=None, scan_capped=False)
    amb = {a["key"].lower() for a in kf["ambiguous"]}
    fields = OBS_FIELDS if kind == "observations" else SUMMARY_FIELDS
    sess_fields = ("content_session_id", "memory_session_id") if kind == "observations" else ("session_id", "memory_session_id")
    need = offset + limit
    extra = {P_PLATFORM: f["agent"], P_SESSION: f["session"]}

    def scan(k: dict):
        rows, scanned, woff, more, oldest, answers = [], 0, 0, True, None, []
        while more and scanned < SCAN_CAP:
            ans = _call(kind, {P_PROJECT: k["key"], P_LIMIT: WORKER_PAGE, P_OFFSET: woff, **extra}, deadline, shape=kind)
            answers.append(ans)
            items = ans.body["items"]
            scanned += len(items)
            woff += len(items)
            hm = ans.body.get("hasMore")
            more = hm if isinstance(hm, bool) else len(items) >= WORKER_PAGE
            if not items:
                more = False
            for r in items:
                e = r["created_at_epoch"]
                oldest = e
                if before is not None and e >= before:
                    continue
                if f["since"] is not None and e < f["since"]:
                    more = False
                    break
                if k["key"].lower() in amb and not _belongs(r, project):
                    continue
                if _passes(r, f, sess_fields):
                    rows.append(_norm(r, fields, k["key"], k["wing"]))
            if len(rows) >= need:
                break
        return {"rows": rows, "more": more, "oldest": oldest, "capped": more and scanned >= SCAN_CAP, "answers": answers}

    results, errors = _fan({k["key"]: (lambda k=k: scan(k)) for k in kf["keys"]}, deadline)
    shape = next((e for e in errors.values() if isinstance(e, ShapeError)), None)
    if shape is not None:
        return _incompatible(out, str(shape))
    if errors and not results:
        raise _worst(errors.values())
    for name, e in errors.items():
        if not isinstance(e, WorkerError):
            log.warning("memory proxy: key scan failed: %s", e.__class__.__name__)
    out["partial"] = sorted(errors)
    _mark_stale(out, [a for r in results.values() for a in r["answers"]])
    frontier = max((r["oldest"] for r in results.values() if r["more"] and r["oldest"] is not None), default=None)
    pool = [row for r in results.values() for row in r["rows"]]
    if frontier is not None:
        pool = [row for row in pool if row["created_at_epoch"] >= frontier]
    pool.sort(key=lambda row: (row["created_at_epoch"], row["id"]), reverse=True)
    page = pool[offset:offset + limit]
    out["items"] = page
    if len(pool) > offset + limit:
        out.update(has_more=True, next_before=page[-1]["created_at_epoch"] if page else None)
    elif frontier is not None:
        out.update(has_more=True, next_before=frontier)
        out["scan_capped"] = len(page) < limit and any(r["capped"] for r in results.values())
    return out


def observations(project: str, **kw) -> dict:
    return _listing("observations", project, **kw)


def summaries(project: str, **kw) -> dict:
    kw.pop("type_", None)
    return _listing("summaries", project, **kw)


def search(project: str, q: str | None, *, type_=None, obs_type=None, limit=None, offset=None, agent=None, order=None,
           health: dict | None = None) -> dict:
    """GET /api/search?query=..&projects=a,b&format=json in one call (the worker's own union); per key when the worker refuses the
    plural (400 INVALID_PROJECTS). Rows of a key outside this project are dropped."""
    q = " ".join(str(q or "").split())
    if not q:
        raise projects.BadRequest("a search needs some words (q)")
    if len(q) > 500:
        raise projects.BadRequest("the search text is longer than 500 characters")
    limit = _int(limit, "limit", 1, PAGE_MAX, 20)
    offset = _int(offset, "offset", 0, OFFSET_MAX, 0)
    if type_ not in (None, "", "observations", "sessions", "prompts"):
        raise projects.BadRequest("type must be observations, sessions or prompts")
    if obs_type and not re.fullmatch(r"[a-z_,-]{1,200}", obs_type):
        raise projects.BadRequest("obs_type must be observation types, comma separated")
    if order not in (None, "", "relevance", "date_desc", "date_asc"):
        raise projects.BadRequest("order must be relevance, date_desc or date_asc")
    f = _filters("search", agent=agent)
    deadline = clock() + SEARCH_BUDGET
    kf = keys_for(project, min(deadline, clock() + BUDGET))
    out = _envelope(project, kf, health)
    out.update(query=q, observations=[], sessions=[], prompts=[], total=0, fallback=None)
    keys = [k["key"] for k in kf["keys"]]
    if not keys:
        return out
    base = {P_QUERY: q, P_FORMAT: FORMAT_JSON, P_LIMIT: limit, P_OFFSET: offset, P_TYPE: type_ or None, P_OBS_TYPE: obs_type or None,
            P_ORDER: order or None, P_PLATFORM: f["agent"]}
    answers = []
    try:
        answers.append(_call("search", {**base, P_PROJECTS: ",".join(keys)}, deadline, shape="search", timeout=SEARCH_BUDGET))
    except ShapeError as e:
        return _incompatible(out, str(e), ("observations", "sessions", "prompts"))
    except WorkerError as e:
        if not (e.code == "http" and e.status == 400 and INVALID_PROJECTS in e.reason):
            raise
        results, errors = _fan({k: (lambda k=k: _call("search", {**base, P_PROJECT: k}, deadline, shape="search", timeout=SEARCH_BUDGET))
                                for k in keys}, deadline)
        if any(isinstance(x, ShapeError) for x in errors.values()):
            return _incompatible(out, str(next(x for x in errors.values() if isinstance(x, ShapeError))), ("observations", "sessions", "prompts"))
        if not results:
            raise _worst(errors.values()) from None
        out.update(partial=sorted(errors), fallback="per_key")
        answers = list(results.values())
    _mark_stale(out, answers)
    by_lower = {k["key"].lower(): k for k in kf["keys"]}
    amb = {a["key"].lower() for a in kf["ambiguous"]}
    seen = {"observations": set(), "sessions": set(), "prompts": set()}
    total = 0
    for a in answers:
        total += a.body.get("totalResults") if isinstance(a.body.get("totalResults"), int) else 0
        for name, fields in (("observations", OBS_FIELDS), ("sessions", SUMMARY_FIELDS), ("prompts", PROMPT_FIELDS)):
            for r in a.body[name]:
                k = _key_of(r, by_lower)
                if k is None or r["id"] in seen[name]:
                    continue
                if name == "observations" and k["key"].lower() in amb and not _belongs(r, project):
                    continue
                seen[name].add(r["id"])
                out[name].append(_norm(r, fields, k["key"], k["wing"]))
    if out["fallback"]:
        for name in seen:
            out[name].sort(key=lambda r: r["created_at_epoch"], reverse=True)
    out["total"] = total
    return out


_ROW = re.compile(r"^\|\s*#(\d+)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*(.*?)\s*\|\s*~?(\d+)?\s*\|\s*$")
_DAY = re.compile(r"^###\s+(.+?)\s*$")


def parse_timeline(text: str) -> list[dict]:
    """The rows of the worker's timeline markdown: [{id, day, time, glyph, title, tokens}] in the order given. Titles are text."""
    out, day = [], None
    for line in str(text or "").splitlines():
        m = _DAY.match(line)
        if m:
            day = m[1]
            continue
        m = _ROW.match(line)
        if m:
            out.append({"id": int(m[1]), "day": day, "time": m[2] or None, "glyph": m[3] or None, "title": m[4],
                        "tokens": int(m[5]) if m[5] else None})
    return out


def timeline(project: str, anchor, *, depth_before=None, depth_after=None, health: dict | None = None) -> dict:
    """The window around one observation of this project: GET /api/observation/<id> (it must belong to one of the project's keys,
    else 404, so another project's memory is never shown), then /api/timeline?anchor=<id>&project=<its key>."""
    anchor = _int(anchor, "anchor", 1, 2**53, None)
    if anchor is None:
        raise projects.BadRequest("anchor (an observation id) is required; a project's timeline is GET .../observations")
    db_ = _int(depth_before, "depth_before", 0, 50, 5)
    da_ = _int(depth_after, "depth_after", 0, 50, 5)
    deadline = clock() + BUDGET
    kf = keys_for(project, deadline)
    out = _envelope(project, kf, health)
    out.update(anchor=None, items=[])
    try:
        row = _call("observation", None, deadline, shape="observation", tail=str(anchor), allow_404=True)
    except ShapeError as e:
        return _incompatible(out, str(e))
    by_lower = {k["key"].lower(): k for k in kf["keys"]}
    k = _key_of(row.body, by_lower) if row.body is not None else None
    if k is None:
        raise projects.NotFound(f"no observation #{anchor} in project {project}")
    try:
        tl = _call("timeline", {P_ANCHOR: anchor, P_DEPTH_BEFORE: db_, P_DEPTH_AFTER: da_, P_PROJECT: k["key"]}, deadline, shape="timeline")
    except ShapeError as e:
        return _incompatible(out, str(e))
    _mark_stale(out, [row, tl])
    out.update(anchor=_norm(row.body, OBS_FIELDS, k["key"], k["wing"]), key=k["key"], items=parse_timeline(_first_raw(tl.body)))
    return out


def _first_raw(body: dict) -> str:
    return "\n".join(x["text"] for x in body.get("content", []) if isinstance(x, dict) and isinstance(x.get("text"), str))


# ------------------------------------------------------------------ the palace

def _drawer(r: dict) -> dict:
    files = []
    for p in (r.get("files_read") or []) + (r.get("files_modified") or []):
        if isinstance(p, str) and p not in files:
            files.append(p)
    narrative = r.get("narrative") if isinstance(r.get("narrative"), str) else None
    if narrative and len(narrative) > NARRATIVE_MAX:
        narrative = narrative[:NARRATIVE_MAX - 1] + "…"
    facts = [str(x)[:FACT_MAX] for x in (r.get("facts") or [])][:FACTS_MAX]
    return {"id": r["id"], "title": r.get("title"), "subtitle": r.get("subtitle"), "type": r.get("type"), "concepts": r.get("concepts") or [],
            "files": files, "created_at": r.get("created_at"), "created_at_epoch": r["created_at_epoch"], "key": r.get("key"),
            "narrative": narrative, "facts": facts, "agent_type": r.get("agent_type")}


def _rooms(rows: list[dict]) -> list[dict]:
    concepts: dict[str, int] = {}
    types: dict[str, int] = {}
    for r in rows:
        for c in dict.fromkeys(x for x in (r.get("concepts") or []) if isinstance(x, str) and x.strip()):
            concepts[c] = concepts.get(c, 0) + 1
        t = r.get("type")
        if isinstance(t, str) and t.strip():
            types[t] = types.get(t, 0) + 1
    out = [{"name": c, "kind": "concept", "count": concepts[c]} for c in CONCEPT_ROOMS if concepts.get(c)]
    out += [{"name": c, "kind": "concept", "count": n} for c, n in sorted(concepts.items(), key=lambda x: (-x[1], x[0]))
            if c not in CONCEPT_ROOMS]
    out += [{"name": t, "kind": "type", "count": types[t]} for t in TYPE_ROOMS if types.get(t)]
    out += [{"name": t, "kind": "type", "count": n} for t, n in sorted(types.items(), key=lambda x: (-x[1], x[0])) if t not in TYPE_ROOMS]
    return out


def _palace_rows(project: str, kf: dict, deadline: float) -> tuple[list[dict], list, list, str | None, bool]:
    """(rows, answers, partial keys, subagent marker source, truncated). The newest PALACE_SCAN observations of the project's keys from
    the search JSON (it carries agent_type; one filter-only call with projects=a,b and orderBy=date_desc, UNVERIFIED on the box: the
    probe pinned a filter-only search with type/obs_type/dateStart, not with projects alone). On a refusal or a shape the board does not
    know, the list route is used instead and the subagent marker is not available (subagent_filter: 'unavailable')."""
    keys = [k["key"] for k in kf["keys"]]
    by_lower = {k["key"].lower(): k for k in kf["keys"]}
    rows, answers, seen, off = [], [], set(), 0
    try:
        while off < PALACE_SCAN:
            ans = _call("search", {P_PROJECTS: ",".join(keys), P_TYPE: "observations", P_ORDER: "date_desc", P_FORMAT: FORMAT_JSON,
                                   P_LIMIT: WORKER_PAGE, P_OFFSET: off}, deadline, shape="search", timeout=SEARCH_BUDGET)
            answers.append(ans)
            items = ans.body["observations"]
            for r in items:
                k = _key_of(r, by_lower)
                if k is not None and r["id"] not in seen:
                    seen.add(r["id"])
                    rows.append(_norm(r, OBS_FIELDS, k["key"], k["wing"]))
            off += WORKER_PAGE
            if len(items) < WORKER_PAGE:
                return rows, answers, [], "search", False
        return rows, answers, [], "search", True
    except WorkerError as e:
        if not (e.code == "http" and e.status == 400):
            raise
    except ShapeError:
        pass
    rows, answers, partial, truncated = [], [], [], False

    def scan(k):
        got, woff, ans_l = [], 0, []
        while woff < PALACE_SCAN:
            a = _call("observations", {P_PROJECT: k["key"], P_LIMIT: WORKER_PAGE, P_OFFSET: woff}, deadline, shape="observations")
            ans_l.append(a)
            got += [_norm(r, OBS_FIELDS, k["key"], k["wing"]) for r in a.body["items"]]
            woff += WORKER_PAGE
            if not a.body.get("hasMore"):
                return got, ans_l, False
        return got, ans_l, True

    results, errors = _fan({k["key"]: (lambda k=k: scan(k)) for k in kf["keys"]}, deadline)
    if any(isinstance(x, ShapeError) for x in errors.values()):
        raise next(x for x in errors.values() if isinstance(x, ShapeError))
    if errors and not results:
        raise _worst(errors.values())
    for got, ans_l, more in results.values():
        rows += got
        answers += ans_l
        truncated = truncated or more
    rows.sort(key=lambda r: (r["created_at_epoch"], r["id"]), reverse=True)
    return rows[:PALACE_SCAN], answers, sorted(errors), "unavailable", truncated or len(rows) > PALACE_SCAN


def palace(project: str, *, subagents=False, drawers=None, health: dict | None = None) -> dict:
    """GET /api/memory/<project>/palace. The answer (docs/memory-api.md has it in full):

        {project, keys, wings: [{name, keys, total, types: {type: n}, rooms: [{name, kind: concept|type, count}], drawers: [drawer]}],
         rooms: [...the same over the whole project], gotchas: [drawer] (<= 3, newest first), total, scanned, truncated,
         summaries, summaries_scope: 'worker', tokens_saved, subagents, subagent_filter: 'applied'|'unavailable', excluded_subagents,
         state, up, stale, stale_at, reason, partial, ambiguous, ambiguous_filter, compat, worker_version, tested_worker, at, cached}

    A wing is a ccboard repo ('root' is the project folder); worktree keys <repo>/<slug> fold into their repo's wing. A room counts the
    observations in that wing (or project) carrying the concept or type; one with several concepts counts in each room, so rooms do not
    sum to the total. Counts are over the newest `scanned` observations (at most PALACE_SCAN; `truncated` says there are more).
    A drawer is {id, title, subtitle, type, concepts, files, created_at, created_at_epoch, key, narrative (<= 2000 characters), facts
    (<= 10), agent_type}: it expands in place to its narrative and facts without another call. `summaries` is the worker's whole
    summary count from /api/stats (every project: summaries_scope 'worker'), `tokens_saved` is null unless /api/stats reports it.
    Workflow-subagent observations (agent_type 'workflow-subagent') are left out unless subagents=True. Cached PALACE_TTL per
    (project, subagents, drawers); only a clean answer (not stale, not partial) is cached."""
    drawers = _int(drawers, "drawers", 1, DRAWERS_MAX, DRAWERS_DEFAULT)
    subagents = bool(subagents)
    ck = (project, subagents, drawers)
    with _palace_lock:
        hit = _palace_cache.get(ck)
    if hit is not None and clock() - hit[0] < PALACE_TTL:
        return {**hit[1], **compat_info(health), "cached": True}
    deadline = clock() + SEARCH_BUDGET
    kf = keys_for(project, min(deadline, clock() + BUDGET))
    out = _envelope(project, kf, health)
    out.update(wings=[], rooms=[], gotchas=[], total=0, scanned=0, truncated=False, summaries=None, summaries_scope="worker",
               tokens_saved=None, subagents=subagents, subagent_filter="applied", excluded_subagents=0, cached=False)
    if kf["keys"]:
        try:
            rows, answers, partial, marker, truncated = _palace_rows(project, {**kf, "project": project}, deadline)
        except ShapeError as e:
            return _incompatible(out, str(e), ("wings", "rooms", "gotchas"))
        out["partial"] = partial
        _mark_stale(out, answers)
        amb = {a["key"].lower() for a in kf["ambiguous"]}
        rows = [r for r in rows if r["key"].lower() not in amb or _belongs(r, project)]
        out.update(scanned=len(rows), truncated=truncated, subagent_filter="applied" if marker == "search" else "unavailable")
        if not subagents and marker == "search":
            kept = [r for r in rows if r.get("agent_type") != SUBAGENT_TYPE]
            out["excluded_subagents"] = len(rows) - len(kept)
            rows = kept
        rows.sort(key=lambda r: (r["created_at_epoch"], r["id"]), reverse=True)
        wings: dict[str, list[dict]] = {}
        for r in rows:
            wings.setdefault(r["repo"], []).append(r)
        out["wings"] = sorted(({"name": w, "keys": sorted({r["key"] for r in rs}), "total": len(rs),
                                "types": {t["name"]: t["count"] for t in _rooms(rs) if t["kind"] == "type"}, "rooms": _rooms(rs),
                                "drawers": [_drawer(r) for r in rs[:drawers]]} for w, rs in wings.items()), key=lambda w: (-w["total"], w["name"]))
        out["rooms"] = _rooms(rows)
        out["gotchas"] = [_drawer(r) for r in rows if "gotcha" in (r.get("concepts") or [])][:GOTCHAS]
        out["total"] = len(rows)
    try:
        st = _call("stats", None, clock() + 1.0, shape="stats").body
        n = st["database"].get("summaries")
        out["summaries"] = n if isinstance(n, int) and not isinstance(n, bool) else None
        ts = st["database"].get("tokensSaved", st["worker"].get("tokensSaved"))
        out["tokens_saved"] = ts if isinstance(ts, int) and not isinstance(ts, bool) else None
    except (WorkerError, ShapeError):
        pass
    if not out["stale"] and not out["partial"] and out["state"] == "ok":
        with _palace_lock:
            _palace_cache[ck] = (clock(), out)
    return out


# ------------------------------------------------------------------ the write-back of a finished task (issue #10)

def task_key(t: dict) -> str | None:
    """The key the observer gives the task's session: the repo folder's name, or <repo folder>/<worktree folder> for a task in a
    worktree (the project folder's own name for a task on `root`)."""
    try:
        rp = projects.repo_path(t["project"], t["repo"])
    except (KeyError, projects.BadRequest):
        return None
    env = _env_entries()
    wt = tasks.task_worktree(t)
    if wt is not None:
        return _env_name(wt, env) or worktree_key(rp.name, wt.name)
    return _env_name(rp, env) or rp.name


def writeback_task(db, t: dict, text: str, *, run=None) -> bool:
    """Save a finished task's result to claude-mem as a note, in a daemon thread, when the owner turned the write-back on. Never
    raises, never waits on the worker, and writes at most once per task id (kv mem_wb_task:<id>, set before the send). Returns
    whether a save was started. `run` replaces the thread start (tests)."""
    try:
        if not settings.claude_mem or not memory.writeback_on(db) or not str(text or "").strip() or not t.get("id"):
            return False
        pl = memory.plugin_status()
        if not pl["installed"] or pl["enabled"] is False:
            return False
        marker = memory.KV_WRITEBACK_DONE + str(t["id"])
        if db.kv_get(marker) is not None:
            return False
        key = task_key(t)
        db.kv_set(marker, {"at": _now_iso()})
        args = (f"Task: {str(t.get('title') or '').strip()}"[:memory.SAVE_TITLE_MAX], str(text), key,
                {"source": "ccboard", "task_id": t["id"], "agent": t.get("agent") or "claude"})
    except Exception as e:
        log.debug("memory write-back skipped: %s", e.__class__.__name__)
        return False

    def send():
        try:
            memory.save_note(*args)
        except Exception as e:                       # best effort: a stopped or slow worker never reaches the task
            log.debug("memory write-back failed: %s", e.__class__.__name__)

    if run is not None:
        run(send)
    else:
        threading.Thread(target=send, name="mem-writeback", daemon=True).start()
    return True
