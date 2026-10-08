"""Claude Code's own session registry, read from files. Display only: nothing here changes a session's state.

Two sources under the Claude config dir (file reads, no subprocess, no network):
  sessions/<pid>.json       one per running Claude process: pid, sessionId, cwd, name, status busy|idle|waiting|shell, ...
  jobs/<id>/state.json      one per background job: state, tempo, needs, suggestedReply, children, worktreePath, ...
A job joins its session by sessionId/resumeSessionId (or the session's jobId). Each entry is then either ours (it is
one of the board's own tmux sessions) or external (anything else Claude runs on this box, minus claude-mem's observer
sessions and anything not updated for 14 days).

This module imports only the standard library and app.platform (config dir, projects dir and pane pids are parameters), so it never
creates an import cycle with app.main, app.db or the rest of app.agents.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .. import platform as plat

EXTERNAL_MAX_AGE_S = 14 * 86400   # an entry nobody touched for two weeks is not "running somewhere"
SNAPSHOT_TTL = 30.0               # how long the parsed files are reused; classification is redone on every call
MAX_FILE_BYTES = 2_000_000        # a state.json bigger than this is not read
MAX_JOBS = 1000                   # newest job dirs by mtime
MAX_ANCESTOR_HOPS = 20
INTENT_HEAD = 120
ROOT_REPO = "root"                # app.projects.ROOT: the reserved repo name for "the project folder itself"

_wall = time.time                              # patched by tests (epoch seconds)
_clock = time.monotonic                        # patched by tests (cache age)


@dataclass
class RegistryInfo:
    source: str                    # 'sessions' | 'jobs'
    session_id: str | None
    pid: int | None
    cwd: str | None
    kind: str | None
    entrypoint: str | None
    name: str | None
    name_source: str | None
    status: str | None
    status_at: str | None          # ISO 8601 UTC
    updated_at: str | None         # ISO 8601 UTC
    bridge: bool
    job: dict | None
    tmux: str | None = None        # set for the board's own sessions
    waiting_for: str | None = None  # e.g. 'permission prompt'

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Entry:
    info: RegistryInfo
    ids: frozenset
    updated: float | None          # epoch seconds, None when the files carry no usable time
    observer: bool
    cwd_keys: frozenset


@dataclass
class _Parsed:
    entries: list
    ancestors: dict                # pid -> [ppid, grandparent, ...], filled lazily


# ------------------------------------------------------------------ small converters

def _s(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _epoch(v) -> float | None:
    """ms epoch (sessions), seconds epoch or ISO 8601 string (jobs) -> epoch seconds."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        if x <= 0:
            return None
        return x / 1000.0 if x > 1e11 else x
    if isinstance(v, str) and v:
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


def _pid(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int) and v > 0:
        return v
    if isinstance(v, str) and v.isdigit() and int(v) > 0:
        return int(v)
    return None


def _norm(path) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    return os.path.normpath(path)


def _cwd_keys(path) -> frozenset:
    """The normalised path and, when it differs, its realpath (/tmp vs /private/tmp, symlinked repos)."""
    n = _norm(path)
    if n is None:
        return frozenset()
    keys = {n}
    try:
        keys.add(os.path.realpath(n))
    except (OSError, ValueError):
        pass
    return frozenset(keys)


def _read_json(path: Path) -> dict | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError):   # JSONDecodeError and UnicodeDecodeError are ValueErrors
        return None
    return data if isinstance(data, dict) else None


def _is_observer(cwd, transcript) -> bool:
    """claude-mem's observer sessions (one Claude process per observed session, cwd under ~/.claude-mem/)."""
    if isinstance(cwd, str) and cwd:
        c = cwd.replace("\\", "/")
        if "/.claude-mem/" in c + "/" or "/observer-sessions" in c:
            return True
    return isinstance(transcript, str) and "/observer-sessions/" in transcript.replace("\\", "/")


# ------------------------------------------------------------------ parsing

def _job_dict(jid: str, st: dict) -> dict:
    children = st.get("children")
    intent = _s(st.get("intent")) or ""
    return {
        "id": jid,
        "state": _s(st.get("state")),
        "tempo": _s(st.get("tempo")),
        "needs": _s(st.get("needs")),
        "suggested_reply": _s(st.get("suggestedReply")),
        "children_count": len(children) if isinstance(children, list) else 0,
        "intent_head": " ".join(intent.split())[:INTENT_HEAD],
        "worktree_path": _s(st.get("worktreePath")),
        "resume_session_id": _s(st.get("resumeSessionId")),
    }


def _load_jobs(config_dir: Path) -> list[dict]:
    """[{id, state(dict), updated(epoch|None)}] for the newest MAX_JOBS job dirs."""
    root = config_dir / "jobs"
    found: list[tuple[float, str, Path]] = []
    try:
        with os.scandir(root) as it:
            for d in it:
                try:
                    if not d.is_dir():
                        continue
                    p = Path(d.path) / "state.json"
                    found.append((p.stat().st_mtime, d.name, p))
                except OSError:
                    continue
    except OSError:
        return []
    found.sort(key=lambda t: t[0], reverse=True)
    jobs = []
    for _mt, jid, p in found[:MAX_JOBS]:
        st = _read_json(p)
        if st is None:
            continue
        jobs.append({"id": jid, "state": st, "updated": _epoch(st.get("updatedAt"))})
    return jobs


def _job_for(sess: dict, jobs: list[dict]) -> dict | None:
    sid = _s(sess.get("sessionId"))
    jid = _s(sess.get("jobId"))
    best = None
    for j in jobs:
        st = j["state"]
        hit = (sid is not None and sid in (st.get("sessionId"), st.get("resumeSessionId"))) \
            or (jid is not None and jid in (j["id"], st.get("daemonShort")))
        if hit and (best is None or (j["updated"] or 0) > (best["updated"] or 0)):
            best = j
    return best


def _session_entry(sess: dict, job: dict | None, fallback_pid: int | None) -> _Entry | None:
    sid = _s(sess.get("sessionId"))
    if sid is None:
        return None
    jst = job["state"] if job else {}
    ids = {sid}
    if job:
        ids.update(x for x in (_s(jst.get("sessionId")), _s(jst.get("resumeSessionId"))) if x)
    s_updated = _epoch(sess.get("updatedAt")) or _epoch(sess.get("statusUpdatedAt")) or _epoch(sess.get("startedAt"))
    j_updated = job["updated"] if job else None
    updated = max((t for t in (s_updated, j_updated) if t is not None), default=None)
    status_t = _epoch(sess.get("statusUpdatedAt")) or s_updated
    info = RegistryInfo(
        source="sessions", session_id=sid, pid=_pid(sess.get("pid")) or fallback_pid, cwd=_s(sess.get("cwd")) or _s(jst.get("cwd")),
        kind=_s(sess.get("kind")), entrypoint=_s(sess.get("entrypoint")),
        name=_s(sess.get("name")) or _s(jst.get("name")),
        name_source=_s(sess.get("nameSource")) or _s(jst.get("nameSource")),
        status=_s(sess.get("status")) or _s(jst.get("state")),
        status_at=_iso(status_t), updated_at=_iso(updated),
        bridge=bool(sess.get("bridgeSessionId") or jst.get("bridgeSessionId")),
        job=_job_dict(job["id"], jst) if job else None,
        waiting_for=_s(sess.get("waitingFor")),
    )
    transcript = sess.get("transcriptPath") or sess.get("transcript_path") or sess.get("transcript")
    return _Entry(info=info, ids=frozenset(ids), updated=updated,
                  observer=_is_observer(info.cwd, transcript), cwd_keys=_cwd_keys(info.cwd))


def _job_entry(job: dict) -> _Entry:
    st = job["state"]
    sid = _s(st.get("sessionId")) or _s(st.get("resumeSessionId"))
    ids = {x for x in (_s(st.get("sessionId")), _s(st.get("resumeSessionId"))) if x}
    cwd = _s(st.get("cwd")) or _s(st.get("originCwd"))
    info = RegistryInfo(
        source="jobs", session_id=sid, pid=None, cwd=cwd, kind="bg", entrypoint=None,
        name=_s(st.get("name")), name_source=_s(st.get("nameSource")),
        status=_s(st.get("state")), status_at=_iso(job["updated"]), updated_at=_iso(job["updated"]),
        bridge=bool(st.get("bridgeSessionId")), job=_job_dict(job["id"], st),
    )
    return _Entry(info=info, ids=frozenset(ids), updated=job["updated"],
                  observer=_is_observer(cwd, st.get("linkScanPath")), cwd_keys=_cwd_keys(cwd))


def _parse(config_dir: Path) -> _Parsed:
    """Every session and job file under config_dir, malformed ones skipped (never raises)."""
    jobs = _load_jobs(config_dir)
    sessions: list[tuple[dict, int | None]] = []
    try:
        names = sorted(os.listdir(config_dir / "sessions"))
    except OSError:
        names = []
    for n in names:
        if not n.endswith(".json") or ".key" in n:     # *.key files hold keys, never read them
            continue
        data = _read_json(config_dir / "sessions" / n)
        if data is not None:
            stem = n[:-5]
            sessions.append((data, int(stem) if stem.isdigit() else None))
    entries: list[_Entry] = []
    joined: set[str] = set()
    for sess, fallback_pid in sessions:
        job = _job_for(sess, jobs)
        e = _session_entry(sess, job, fallback_pid)
        if e is None:
            continue
        if job:
            joined.add(job["id"])
        entries.append(e)
    for j in jobs:
        if j["id"] not in joined:
            entries.append(_job_entry(j))
    return _Parsed(entries=entries, ancestors={})


# ------------------------------------------------------------------ pid ancestry (app.platform: /proc on Linux, psutil elsewhere)

def _ancestors(pid: int, cache: dict) -> list[int]:
    if pid in cache:
        return cache[pid]
    chain = plat.ancestors(pid, MAX_ANCESTOR_HOPS)
    cache[pid] = chain
    return chain


# ------------------------------------------------------------------ classification

def _row_name(r: dict) -> str | None:
    return _s(r.get("tmux_name")) or _s(r.get("tmux"))


def _row_ids(r: dict) -> set[str]:
    return {x for x in (_s(r.get("agent_session_id")), _s(r.get("claude_session_id"))) if x}


def _row_agent(r: dict) -> str:
    a = _s(r.get("agent"))
    if a:
        return a
    return "shell" if r.get("launcher") in ("shell", "clone") else "claude"


def _row_cwds(r: dict, projects_dir: Path | None) -> set[str]:
    """Where this session was launched: the stored cwd, else the pane path, else projects_dir/project/repo."""
    raw = [r.get("cwd"), r.get("path")]
    keys: set[str] = set()
    for p in raw:
        if _s(p):
            keys |= _cwd_keys(p)
    if keys:
        return keys
    proj, repo = _s(r.get("project")), _s(r.get("repo"))
    if projects_dir is not None and proj:
        base = Path(projects_dir) / proj
        for p in ([base] if repo in (None, ROOT_REPO) else [base / repo] + ([base] if repo == proj else [])):
            keys |= _cwd_keys(str(p))
    return keys


def _classify(parsed: _Parsed, open_rows, *, projects_dir, pane_pids, now: float) -> dict:
    rows: list[dict] = []
    for r in (open_rows.values() if isinstance(open_rows, dict) else (open_rows or [])):
        if isinstance(r, dict) and _row_name(r) and _row_agent(r) != "codex":
            rows.append(r)
    names = [_row_name(r) for r in rows]
    entries = sorted(parsed.entries, key=lambda e: e.updated or 0.0, reverse=True)   # newest wins every tie
    claimed: dict[str, _Entry] = {}
    used: set[int] = set()

    def claim(name: str, e: _Entry) -> None:
        claimed[name] = e
        used.add(id(e))

    # 1. the session id is the row's own (agent_session_id, else claude_session_id)
    by_id: dict[str, list[str]] = {}
    for r, n in zip(rows, names):
        for sid in _row_ids(r):
            by_id.setdefault(sid, []).append(n)
    for e in entries:
        hit = next((n for sid in e.ids for n in by_id.get(sid, ()) if n not in claimed), None)
        if hit:
            claim(hit, e)

    # 2. the process is the pane's pid, or a descendant of it
    by_pane: dict[int, list[str]] = {}
    for n in names:
        p = _pid((pane_pids or {}).get(n))
        if p:
            by_pane.setdefault(p, []).append(n)
    if by_pane:
        for e in entries:
            if id(e) in used or not e.info.pid:
                continue
            chain = [e.info.pid] + _ancestors(e.info.pid, parsed.ancestors)
            hit = next((n for p in chain for n in by_pane.get(p, ()) if n not in claimed), None)
            if hit:
                claim(hit, e)

    # 3. the same cwd, only when exactly one agent row has it (like hooks.resolve_session) and nothing claimed it yet
    by_cwd: dict[str, set[str]] = {}
    for r, n in zip(rows, names):
        if _row_agent(r) == "shell":
            continue
        for k in _row_cwds(r, projects_dir):
            by_cwd.setdefault(k, set()).add(n)
    for e in entries:
        if id(e) in used:
            continue
        cands = {n for k in e.cwd_keys for n in by_cwd.get(k, ())}
        if len(cands) == 1:
            n = next(iter(cands))
            if n not in claimed:
                claim(n, e)

    ours = {n: replace(e.info, tmux=n).to_dict() for n, e in claimed.items()}
    external = [e.info.to_dict() for e in entries
                if id(e) not in used and not e.observer and e.updated is not None
                and now - e.updated <= EXTERNAL_MAX_AGE_S]
    return {"ours": ours, "external": external, "scanned_at": _iso(now)}


# ------------------------------------------------------------------ public API

def scan(config_dir: Path, open_rows: list[dict], *, projects_dir: Path | None = None,
         pane_pids: dict[str, int] | None = None, now: float | None = None) -> dict:
    """Read the registry files and split them into the board's own sessions and everything else.

    open_rows: the board's open session rows ({tmux_name|tmux, claude_session_id, agent_session_id, cwd, project, repo,
    agent, launcher}); a dict name -> row (db.open_rows()) is accepted too. pane_pids: tmux name -> pane pid.
    Returns {'ours': {tmux_name: RegistryInfo dict}, 'external': [RegistryInfo dict, newest first], 'scanned_at'}.
    Timestamps are ISO 8601 UTC strings. Never raises on missing or malformed files."""
    return _classify(_parse(Path(config_dir)), open_rows, projects_dir=projects_dir, pane_pids=pane_pids,
                     now=_wall() if now is None else now)


def session_cwds(config_dir: Path) -> dict[str, str]:
    """{session id: cwd} for every non-observer entry of the registry (a running Claude process or a background job), for the usage join by folder
    (issue #57). Display and usage only; the ids a job also answers to (resumeSessionId) point at the same folder."""
    out: dict[str, str] = {}
    for e in _parse(Path(config_dir)).entries:
        if e.observer or not e.info.cwd:
            continue
        for sid in e.ids:
            out.setdefault(sid.lower(), e.info.cwd)
    return out


_lock = threading.Lock()
_cache: dict | None = None   # {'dir': str, 'at': monotonic, 'parsed': _Parsed}


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = None


def _parsed_cached(config_dir: Path, ttl: float) -> _Parsed:
    global _cache
    key = str(config_dir)
    with _lock:
        c = _cache
        if c and c["dir"] == key and _clock() - c["at"] < ttl:
            return c["parsed"]
        parsed = _parse(config_dir)       # a handful of small files; the lock keeps concurrent polls from scanning twice
        _cache = {"dir": key, "at": _clock(), "parsed": parsed}
        return parsed


def snapshot(db, config_dir: Path, ttl: float = SNAPSHOT_TTL, *, pane_pids: dict[str, int] | None = None,
             projects_dir: Path | None = None, now: float | None = None) -> dict:
    """scan() for the poll path: the parsed files are cached for `ttl` seconds, the ours/external split is recomputed on
    every call from the current open rows (cheap, in memory), so a session launched a second ago is already ours.
    `db` needs only .open_rows(); None or a failing db means no rows (everything external)."""
    try:
        rows = db.open_rows() if db is not None else []
    except Exception:
        rows = []
    parsed = _parsed_cached(Path(config_dir), ttl)
    return _classify(parsed, rows, projects_dir=projects_dir, pane_pids=pane_pids,
                     now=_wall() if now is None else now)


def enrich(rows, snap: dict):
    """Set row['flags']['registry'] = {status, name, name_source, pid, bridge, kind, job, at, waiting_for} for every row
    that is one of the board's sessions in the snapshot. Display only: the row's state fields are never touched.
    `rows` is a list of session dicts keyed by 'tmux' (projects.scan) or 'tmux_name' (db rows), or a dict tmux name ->
    session dict (what main._merged_sessions builds). Returns the same object."""
    ours = (snap or {}).get("ours") or {}
    items = [(k, r) for k, r in rows.items()] if isinstance(rows, dict) else [(None, r) for r in rows or []]
    for key, row in items:
        if not isinstance(row, dict):
            continue
        info = ours.get(key if isinstance(key, str) else (_row_name(row) or ""))
        if not info:
            continue
        job = info.get("job")
        flags = row.get("flags")
        flags = dict(flags) if isinstance(flags, dict) else {}
        flags["registry"] = {
            "status": info.get("status"), "name": info.get("name"), "name_source": info.get("name_source"),
            "pid": info.get("pid"), "bridge": bool(info.get("bridge")), "kind": info.get("kind"),
            "job": {"state": job.get("state"), "tempo": job.get("tempo"), "needs": job.get("needs"),
                    "suggested_reply": job.get("suggested_reply")} if job else None,
            "at": info.get("status_at"), "waiting_for": info.get("waiting_for"),
        }
        row["flags"] = flags
    return rows
