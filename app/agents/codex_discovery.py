"""Codex sessions the board did not start, read from Codex's own files (display only: nothing here changes a session's state).

Two sources under CODEX_HOME, both read-only:
  state_<n>.sqlite          Codex's thread index (table `threads`). Opened read-only (`mode=ro`, or `immutable=1` when no -wal file
                            exists: see _open_ro), schema drift tolerated
                            through PRAGMA table_info: only the columns that exist are selected.
  sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
                            one per thread. The FIRST line (session_meta) names the thread's originator, source, cwd and id; it is read
                            for every discovered thread (the DB has no originator) and is the only source for native rollouts the DB
                            never indexed.
Never opened: auth.json (or any other file in CODEX_HOME beyond the two above and external_agent_session_imports.json).

What is listed: user-initiated, non-archived, non-imported, non-subagent threads updated in the last 14 days (newest first, at most
100). Dropped: threads Codex Desktop imported from Claude (tokens 0 with no model and source vscode, or ids in
external_agent_session_imports.json), guardian / thread_spawn children, and threads with no user event. Threads whose originator is not
`codex-tui` (a Hermes agent drives Codex on the box) stay in the list with `badge` set to the originator, so the Agents roster can mark
them. Never a sum of per-thread `tokens_used` (forks inherit the parent's total): `tokens` is shown per thread, totals come from ccusage.

This module imports only config, projects and the standard library (db and the board's own rows are parameters), so it never creates an
import cycle with app.main, app.db or the rest of app.agents.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import projects
from ..config import settings

log = logging.getLogger("ccboard.codex")

DAYS = 14                          # an entry nobody touched for two weeks is not "running somewhere"
LIMIT = 100
SNAPSHOT_TTL = 30.0                # how long a scan is reused (the same ttl as the Claude registry's)
KV_KEY = "external_sessions"
NATIVE_ORIGINATOR = "codex-tui"    # a thread started in the terminal (the board's sessions are these); anything else is badged
META_MAX_BYTES = 512 * 1024        # session_meta carries base_instructions (tens of KB): read the first line up to this
MAX_ROLLOUTS = 600                 # newest rollout files looked at for threads the DB missed
IMPORTS_FILE = "external_agent_session_imports.json"
IMPORTS_MAX_BYTES = 5_000_000
META_KEYS = ("id", "cwd", "originator", "source", "cli_version", "creator_account_id", "model_provider", "timestamp", "thread_source")
THREAD_COLS = ("id", "title", "name", "cwd", "model", "reasoning_effort", "tokens_used", "created_at", "updated_at", "rollout_path",
               "git_branch", "source", "model_provider", "thread_source")

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
UUID_RE = re.compile(f"^{_UUID}$")
ROLLOUT_RE = re.compile(rf"^rollout-.*?({_UUID})\.jsonl$")
STATE_RE = re.compile(r"^state_(\d+)\.sqlite$")

_wall = time.time                  # patched by tests (epoch seconds)
_clock = time.monotonic            # patched by tests (cache age)


# ------------------------------------------------------------------ small converters

def _epoch(v) -> float | None:
    """Epoch seconds or milliseconds, or an ISO 8601 string -> epoch seconds."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        if x <= 0:
            return None
        return x / 1000.0 if x > 1e11 else x
    if isinstance(v, str) and v.strip():
        try:
            dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def _iso(t: float | None) -> str | None:
    if t is None:
        return None
    try:
        return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _s(v) -> str | None:
    return v if isinstance(v, str) and v else None


# ------------------------------------------------------------------ rollout files

def sessions_root(codex_home: Path | None = None) -> Path:
    return Path(codex_home if codex_home is not None else settings.codex_home) / "sessions"


def under_sessions(path, codex_home: Path | None = None) -> Path | None:
    """A rollout path is only trusted inside CODEX_HOME/sessions (a DB row or a payload can name any path)."""
    if not isinstance(path, str) or not path.endswith(".jsonl") or len(path) > 4096:
        return None
    try:
        root = sessions_root(codex_home).resolve()
        p = Path(path).resolve()
        p.relative_to(root)
    except (ValueError, OSError, RuntimeError):
        return None
    return p


_meta_cache: dict[str, dict | None] = {}
_META_CACHE_MAX = 2000


def first_line_meta(path) -> dict | None:
    """The session_meta of a rollout: the whitelisted keys of the first line's payload (META_KEYS, with the thread id under `id`).
    None when the file is unreadable or its first line is not a session_meta. The first line never changes once written, so a parsed
    result is cached by path; nothing but the whitelist (never base_instructions, never account or workspace details) leaves here."""
    key = str(path)
    if key in _meta_cache:
        return _meta_cache[key]
    try:
        with open(path, "rb") as fh:
            raw = fh.readline(META_MAX_BYTES)
    except OSError:
        return None
    if not raw.endswith(b"\n") and len(raw) >= META_MAX_BYTES:
        return None                                       # an unterminated first line this long is not a session_meta
    try:
        obj = json.loads(raw)
    except ValueError:
        return None                                       # a half-written first line: try again next time
    pl = obj.get("payload") if isinstance(obj, dict) and obj.get("type") == "session_meta" else None
    meta: dict | None = None
    if isinstance(pl, dict):
        meta = {k: pl[k] for k in META_KEYS if k in pl and isinstance(pl[k], (str, dict))}
        if "id" not in meta and isinstance(pl.get("session_id"), str):
            meta["id"] = pl["session_id"]
    if len(_meta_cache) >= _META_CACHE_MAX:
        _meta_cache.clear()
    _meta_cache[key] = meta
    return meta


def reset_caches() -> None:
    global _cache
    _meta_cache.clear()
    with _lock:
        _cache = None


def is_subagent(source, thread_source=None) -> bool:
    """A guardian / thread_spawn / review / compact child of another thread: its source is an object with a `subagent` key (or the JSON
    text of one), or names one. A thread the user started has source `cli`, `vscode`, `exec`, `mcp` or a client's own name."""
    for v in (source, thread_source):
        if isinstance(v, str) and v.lstrip().startswith("{"):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        if isinstance(v, dict):
            if any("subagent" in str(k).lower().replace("_", "") for k in v):
                return True
        elif isinstance(v, str):
            low = v.lower().replace("_", "")
            if "subagent" in low or "threadspawn" in low or "guardian" in low:
                return True
    return False


def source_name(source) -> str | None:
    """The client kind of a session source as a short word: 'cli', 'vscode', 'exec', ... (an object source has none)."""
    if isinstance(source, str) and source and not source.lstrip().startswith("{"):
        return source[:40]
    return None


def project_repo(cwd, projects_dir: Path | None = None) -> tuple[str, str] | None:
    """(project, repo) for a directory under PROJECTS_DIR, else None: a thin alias of projects.repo_for_cwd (issue #57), kept for its callers."""
    return projects.repo_for_cwd(cwd, projects_dir)


def _imported_ids(codex_home: Path) -> set[str]:
    """Thread ids Codex Desktop imported from other agents: every uuid string in external_agent_session_imports.json (its exact layout is
    Codex's business, so any nesting is read)."""
    p = codex_home / IMPORTS_FILE
    try:
        if p.stat().st_size > IMPORTS_MAX_BYTES:
            return set()
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return set()
    out: set[str] = set()
    todo = [data]
    while todo and len(out) < 50_000:
        x = todo.pop()
        if isinstance(x, str):
            if UUID_RE.match(x):
                out.add(x.lower())
        elif isinstance(x, dict):
            todo.extend(x.keys())
            todo.extend(x.values())
        elif isinstance(x, list):
            todo.extend(x)
    return out


# ------------------------------------------------------------------ the thread index

def state_db(codex_home: Path) -> Path | None:
    """The newest state_<n>.sqlite (highest n, then newest file)."""
    best = None
    try:
        for f in codex_home.iterdir():
            m = STATE_RE.match(f.name)
            if m and f.is_file():
                k = (int(m.group(1)), f.stat().st_mtime)
                if best is None or k > best[0]:
                    best = (k, f)
    except OSError:
        return None
    return best[1] if best else None


def _open_ro(path: Path) -> sqlite3.Connection | None:
    """Read-only. With a -wal file beside the database (Codex is running, or died with frames not yet checkpointed) `mode=ro` is used:
    it reads the WAL, so the newest threads are seen. Without one every row is in the main file, and `immutable=1` reads it without a
    lock and without creating the -wal/-shm files that even a `mode=ro` open of a WAL database leaves behind. Whichever fails first
    falls back to the other."""
    uri = path.resolve().as_uri()
    wal = path.with_name(path.name + "-wal")
    for q in (("?mode=ro", "?immutable=1") if wal.exists() else ("?immutable=1", "?mode=ro")):
        try:
            conn = sqlite3.connect(uri + q, uri=True, timeout=1.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=1")
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            return conn
        except sqlite3.Error:
            continue
    return None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}      # `table` is one of two literals below
    except sqlite3.Error:
        return set()


def _epoch_sql(col: str) -> str:
    """A time column as epoch seconds whatever Codex stored: integer seconds or milliseconds, a float, or an ISO string."""
    return (f"CASE typeof({col}) WHEN 'integer' THEN CASE WHEN {col} > 100000000000 THEN {col} / 1000 ELSE {col} END "
            f"WHEN 'real' THEN CASE WHEN {col} > 1e11 THEN {col} / 1000.0 ELSE {col} END "
            f"WHEN 'text' THEN CAST(strftime('%s', {col}) AS INTEGER) ELSE NULL END")


def _thread_rows(path: Path, cutoff: float, limit: int) -> tuple[list[dict], set[str]]:
    """(candidate thread rows newest first, every thread id the DB knows). The filters that can be asked of SQL are: the drift-tolerant
    ones (a column that does not exist is not asked about)."""
    conn = _open_ro(path)
    if conn is None:
        return [], set()
    try:
        cols = _columns(conn, "threads")
        if "id" not in cols:
            return [], set()
        known = {str(r[0]).lower() for r in conn.execute("SELECT id FROM threads LIMIT 50000") if r[0]}
        tcol = "updated_at" if "updated_at" in cols else "created_at" if "created_at" in cols else None
        if tcol is None:
            return [], known
        sel = [c for c in THREAD_COLS if c in cols]
        where = [f"{_epoch_sql(tcol)} >= ?"]
        if "archived" in cols:
            where.append("COALESCE(archived, 0) = 0")
        if "has_user_event" in cols:
            where.append("COALESCE(has_user_event, 1) <> 0")
        if {"tokens_used", "model", "source"} <= cols:       # Codex Desktop's imports of Claude sessions: no tokens, no model, source vscode
            where.append("NOT (COALESCE(tokens_used, 0) = 0 AND COALESCE(model, '') = '' AND LOWER(COALESCE(source, '')) = 'vscode')")
        if "source" in cols:
            where.append("LOWER(COALESCE(source, '')) NOT LIKE '%subagent%' AND LOWER(COALESCE(source, '')) NOT LIKE '%thread_spawn%'")
        edges = _columns(conn, "thread_spawn_edges")
        if "child_thread_id" in edges:
            where.append("id NOT IN (SELECT child_thread_id FROM thread_spawn_edges WHERE child_thread_id IS NOT NULL)")
        sql = (f"SELECT {', '.join(sel)} FROM threads WHERE {' AND '.join(where)} ORDER BY {_epoch_sql(tcol)} DESC LIMIT ?")
        rows = [dict(r) for r in conn.execute(sql, (cutoff, limit))]
        for r in rows:
            r["_time"] = _epoch(r.get("updated_at") if tcol == "updated_at" else r.get("created_at"))
        return rows, known
    except sqlite3.Error as e:
        log.debug("codex state db read failed: %s", e)
        return [], set()
    finally:
        conn.close()


# ------------------------------------------------------------------ discovery

def _entry(*, source: str, tid: str, meta: dict | None, row: dict | None, rollout: Path | None, updated: float | None,
           created: float | None, root: Path, projects_dir) -> dict:
    row = row or {}
    meta = meta or {}
    cwd = _s(row.get("cwd")) or _s(meta.get("cwd"))
    originator = _s(meta.get("originator"))
    pr = project_repo(cwd, projects_dir)
    title = _s(row.get("title"))
    name = _s(row.get("name"))
    tokens = row.get("tokens_used")
    try:
        rel = str(rollout.relative_to(root)) if rollout else None
    except ValueError:
        rel = None
    return {
        "agent": "codex", "source": source, "id": tid, "session_id": tid, "name": name or title, "title": title, "cwd": cwd,
        "pid": None, "kind": None, "entrypoint": originator, "status": None, "tmux": None,
        "model": _s(row.get("model")), "effort": _s(row.get("reasoning_effort")),
        "tokens": int(tokens) if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) else None,
        "created_at": _iso(created), "updated_at": _iso(updated), "branch": _s(row.get("git_branch")),
        "originator": originator, "badge": originator if originator and originator != NATIVE_ORIGINATOR else None,
        "client": source_name(row.get("source")) or source_name(meta.get("source")),
        "rollout": rel, "project": pr[0] if pr else None, "repo": pr[1] if pr else None,
        "openable": bool(pr and cwd and os.path.isdir(cwd)),
    }


def _date_dirs(root: Path, now: float, days: int):
    seen = set()
    for back in range(days + 2):
        for t in (now - back * 86400, now - back * 86400 + 86400):
            for tz in (timezone.utc, None):
                d = datetime.fromtimestamp(t, tz)
                p = root / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}"
                if p not in seen:
                    seen.add(p)
                    if p.is_dir():
                        yield p


def discover(codex_home: Path | None = None, *, now: float | None = None, days: int = DAYS, limit: int = LIMIT,
             projects_dir: Path | None = None) -> list[dict]:
    """Codex threads on this box that are listed under CODEX_HOME, newest first: see the module docstring for what is kept. Never raises
    (an unreadable DB or rollout only leaves its threads out). Each entry: {agent: 'codex', source: 'state'|'rollout', id, session_id,
    name, title, cwd, pid: None, kind: None, entrypoint (= originator), status: None, tmux: None, model, effort, tokens (this thread's
    tokens_used, display only), created_at, updated_at (ISO UTC), branch, originator, badge (the originator when it is not codex-tui),
    client (cli, vscode, ...), rollout (path under sessions/), project, repo (when cwd lies under PROJECTS_DIR), openable}."""
    home = Path(codex_home if codex_home is not None else settings.codex_home)
    now = _wall() if now is None else now
    cutoff = now - days * 86400
    root = sessions_root(home)
    imported = _imported_ids(home)
    out: dict[str, dict] = {}
    known: set[str] = set()
    db_path = state_db(home)
    if db_path is not None:
        rows, known = _thread_rows(db_path, cutoff, limit)
        for r in rows:
            tid = str(r.get("id") or "").lower()
            if not UUID_RE.match(tid) or tid in imported or tid in out:
                continue
            rollout = under_sessions(r.get("rollout_path"), home)
            meta = first_line_meta(rollout) if rollout and rollout.is_file() else None
            if is_subagent(r.get("source"), r.get("thread_source")) or (meta and is_subagent(meta.get("source"), meta.get("thread_source"))):
                continue
            out[tid] = _entry(source="state", tid=tid, meta=meta, row=r, rollout=rollout, updated=r.get("_time"),
                              created=_epoch(r.get("created_at")), root=root, projects_dir=projects_dir)
    found: list[tuple[float, Path, str]] = []             # native rollouts the DB never indexed
    try:
        for d in _date_dirs(root, now, days):
            for f in d.iterdir():
                m = ROLLOUT_RE.match(f.name)
                if not m:
                    continue
                tid = m.group(1).lower()
                if tid in known or tid in imported or tid in out:
                    continue
                try:
                    mt = f.stat().st_mtime
                except OSError:
                    continue
                if mt >= cutoff:
                    found.append((mt, f, tid))
    except OSError as e:
        log.debug("codex rollout scan failed: %s", e)
    found.sort(key=lambda t: t[0], reverse=True)
    for mt, f, tid in found[:MAX_ROLLOUTS]:
        meta = first_line_meta(f)
        if meta is None or is_subagent(meta.get("source"), meta.get("thread_source")):
            continue
        mid = _s(meta.get("id"))
        if mid and mid.lower() != tid:
            tid = mid.lower() if UUID_RE.match(mid) else tid
            if tid in known or tid in imported or tid in out:
                continue
        out[tid] = _entry(source="rollout", tid=tid, meta=meta, row=None, rollout=f, updated=mt, created=_epoch(meta.get("timestamp")),
                          root=root, projects_dir=projects_dir)
    items = sorted(out.values(), key=lambda e: e["updated_at"] or "", reverse=True)
    return items[:limit]


# ------------------------------------------------------------------ the poll path

_lock = threading.Lock()
_cache: dict | None = None     # {'home': str, 'at': monotonic, 'items': [...], 'scanned_at': iso}


def snapshot(db=None, ttl: float = SNAPSHOT_TTL, *, codex_home: Path | None = None, projects_dir: Path | None = None,
             now: float | None = None) -> dict:
    """discover() for the poll path: {items, at}. A scan is reused for `ttl` seconds (one scan at a time; the lock keeps concurrent polls
    from scanning twice). A fresh scan is also written to the kv record `external_sessions` {codex: [...], at} when `db` is given, for
    the pages that read the record instead of calling the endpoint."""
    global _cache
    home = Path(codex_home if codex_home is not None else settings.codex_home)
    with _lock:
        c = _cache
        if c and c["home"] == str(home) and _clock() - c["at"] < ttl:
            return {"items": c["items"], "at": c["scanned_at"]}
        items = discover(home, now=now, projects_dir=projects_dir)
        scanned = _iso(_wall() if now is None else now)
        _cache = {"home": str(home), "at": _clock(), "items": items, "scanned_at": scanned}
    if db is not None:
        try:
            db.kv_set(KV_KEY, {"codex": items, "at": scanned})
        except Exception as e:                               # a display record: its failure never fails the poll
            log.debug("external_sessions kv write failed: %s", e)
    return {"items": items, "at": scanned}


def owned_ids(db) -> set[str]:
    """Codex thread ids that are one of the board's own sessions (a row of any age): not external."""
    try:
        return {str(r["claude_session_id"]).lower() for r in db.session_ids("codex") if r.get("claude_session_id")}
    except Exception as e:
        log.debug("owned codex ids lookup failed: %s", e)
        return set()


def external(db, project: str | None = None, *, ttl: float = SNAPSHOT_TTL, codex_home: Path | None = None,
             projects_dir: Path | None = None) -> tuple[list[dict], str | None]:
    """(the external Codex threads, scanned_at): the snapshot minus the board's own sessions, narrowed to a project (cwd under
    PROJECTS_DIR/<project>) when asked. Never raises."""
    try:
        snap = snapshot(db, ttl, codex_home=codex_home, projects_dir=projects_dir)
    except Exception as e:
        log.debug("codex discovery failed: %s", e)
        return [], None
    mine = owned_ids(db) if db is not None else set()
    items = [e for e in snap["items"] if e["id"] not in mine]
    if project:
        items = [e for e in items if e.get("project") == project]
    return items, snap["at"]


def find(db, tid: str, *, ttl: float = SNAPSHOT_TTL, codex_home: Path | None = None, projects_dir: Path | None = None) -> dict | None:
    """The external entry of one thread id (what POST /api/external/codex/<id>/open acts on), or None."""
    tid = str(tid or "").lower()
    items, _ = external(db, ttl=ttl, codex_home=codex_home, projects_dir=projects_dir)
    return next((e for e in items if e["id"] == tid), None)
