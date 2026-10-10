"""SQLite state. tmux is the truth for 'alive'; rows hold what tmux cannot."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("ccboard.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY,
  tmux_name TEXT NOT NULL,
  project TEXT NOT NULL,
  repo TEXT NOT NULL,
  name TEXT NOT NULL,
  launcher TEXT NOT NULL,
  cmd TEXT,
  claude_session_id TEXT,
  add_dirs TEXT,
  created_at TEXT NOT NULL,
  ended_at TEXT
);
CREATE INDEX IF NOT EXISTS sessions_open ON sessions(tmux_name) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  tmux_name TEXT NOT NULL,
  event TEXT NOT NULL,
  kind TEXT,
  message TEXT,
  payload TEXT,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_session ON events(tmux_name, id);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS push_subs (endpoint TEXT PRIMARY KEY, sub TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL,
  repo TEXT NOT NULL,
  slug TEXT NOT NULL,
  title TEXT NOT NULL,
  prompt TEXT NOT NULL,
  branch TEXT NOT NULL,
  base TEXT,
  worktree TEXT NOT NULL,
  tmux_name TEXT NOT NULL,
  claude_session_id TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  pr_number INTEGER,
  pr_url TEXT,
  pr_state TEXT,
  pr_json TEXT,
  ci TEXT,
  cost_usd REAL,
  overlap TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  archived_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL,
  repo TEXT NOT NULL,
  name TEXT NOT NULL,
  prompt TEXT NOT NULL,
  cron TEXT,
  permission_mode TEXT NOT NULL DEFAULT 'acceptEdits',
  max_turns INTEGER NOT NULL DEFAULT 30,
  max_budget_usd REAL,
  args TEXT,
  timeout_s INTEGER,
  enabled INTEGER NOT NULL DEFAULT 1,
  batch_id TEXT,
  next_run_at TEXT,
  last_run_at TEXT,
  last_status TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  result TEXT,
  error TEXT,
  session_id TEXT,
  cost_usd REAL,
  num_turns INTEGER,
  worktree TEXT,
  branch TEXT,
  task_id INTEGER
);
CREATE TABLE IF NOT EXISTS permissions (
  id INTEGER PRIMARY KEY,
  tmux_name TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  summary TEXT NOT NULL,
  input TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  decision TEXT,
  source TEXT
);
CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY,
  series TEXT NOT NULL,
  key TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL,
  value REAL,
  meta TEXT
);
CREATE INDEX IF NOT EXISTS samples_series_key_at ON samples(series, key, at);
CREATE INDEX IF NOT EXISTS samples_at ON samples(at);
"""

# Added after v0.1; applied with ALTER TABLE, "duplicate column" errors are ignored.
#
# Rules (the board ships as a container image, a git push deploys within minutes and these run on every start):
#   * idempotent: ALTER ... ADD COLUMN (a "duplicate column" error is swallowed), CREATE ... IF NOT EXISTS, and data
#     statements that can be re-run;
#   * additive only: never drop, rename or tighten a column, so the PREVIOUS image still runs on the migrated DB (its
#     INSERTs name fewer columns and every new NOT NULL column has a DEFAULT; rows it writes after a rollback, e.g. a
#     shell session with agent='claude', are fixed by the re-runnable backfill the next time the new image opens the DB);
#   * order matters inside this list: a statement may only use columns added above it.
# New columns are deliberately not added to SCHEMA, so a fresh DB and an upgraded one take the identical path.
MIGRATIONS = [
    "ALTER TABLE sessions ADD COLUMN state TEXT",
    "ALTER TABLE sessions ADD COLUMN state_at TEXT",
    "ALTER TABLE sessions ADD COLUMN last_event TEXT",
    "ALTER TABLE sessions ADD COLUMN last_message TEXT",
    "ALTER TABLE sessions ADD COLUMN last_prompt TEXT",
    "ALTER TABLE sessions ADD COLUMN stats TEXT",
    "ALTER TABLE sessions ADD COLUMN acked_at TEXT",
    "ALTER TABLE tasks ADD COLUMN preview_port INTEGER",
    "ALTER TABLE tasks ADD COLUMN preview_https INTEGER",
    # ---- v0.5.4 (agent seam, tasks v2)
    # sessions: the physical column claude_session_id is NOT renamed; open_rows() exposes it as agent_session_id.
    "ALTER TABLE sessions ADD COLUMN agent TEXT NOT NULL DEFAULT 'claude'",     # claude|codex|shell
    "UPDATE sessions SET agent='shell' WHERE launcher IN ('shell','clone') AND agent='claude'",   # backfill, re-runnable
    "ALTER TABLE sessions ADD COLUMN cwd TEXT",             # launch cwd (repo or worktree)
    "ALTER TABLE sessions ADD COLUMN opts TEXT",            # JSON of validated launch options re-passed on resume
    "ALTER TABLE sessions ADD COLUMN flags TEXT",           # JSON, only through DB.update_flags
    "ALTER TABLE sessions ADD COLUMN ended_reason TEXT",    # killed|auto_close|exited|reconciled|project_deleted
    "ALTER TABLE events ADD COLUMN agent TEXT",
    # tasks: tmux_name, worktree and branch stay NOT NULL; an unassigned task stores '' in all three (tasks.has_worktree)
    "ALTER TABLE tasks ADD COLUMN agent TEXT NOT NULL DEFAULT 'claude'",
    "ALTER TABLE tasks ADD COLUMN mode TEXT NOT NULL DEFAULT 'worktree'",      # worktree|session|attached
    "ALTER TABLE tasks ADD COLUMN phase TEXT NOT NULL DEFAULT 'running'",      # backlog|queued|running|done|failed|cancelled
    "ALTER TABLE tasks ADD COLUMN session_row INTEGER",     # sessions.id running it (tmux names are reused)
    "ALTER TABLE tasks ADD COLUMN auto_close INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN parent_id INTEGER",
    "ALTER TABLE tasks ADD COLUMN chain_id TEXT",
    "ALTER TABLE tasks ADD COLUMN spec TEXT",               # JSON dispatch intent of a queued step
    "ALTER TABLE tasks ADD COLUMN result TEXT",             # <= RESULT_MAX chars
    "ALTER TABLE tasks ADD COLUMN result_at TEXT",
    "ALTER TABLE tasks ADD COLUMN assigned_at TEXT",
    "ALTER TABLE tasks ADD COLUMN done_at TEXT",
    "ALTER TABLE tasks ADD COLUMN issue_number INTEGER",        # the GitHub issue the task was made from
    "ALTER TABLE tasks ADD COLUMN issue_url TEXT",
    "ALTER TABLE tasks ADD COLUMN issue_commented_at TEXT",     # set once the result was posted on the issue
    "CREATE INDEX IF NOT EXISTS tasks_by_session ON tasks(session_row)",
    "CREATE INDEX IF NOT EXISTS tasks_by_parent ON tasks(parent_id)",
    "ALTER TABLE jobs ADD COLUMN agent TEXT NOT NULL DEFAULT 'claude'",
    # ---- v0.5.16 (schedules per agent): the agent's own options for a headless run as JSON (Codex: model, reasoning_effort); NULL = none
    "ALTER TABLE jobs ADD COLUMN opts TEXT",
    # ---- v0.5.17b (usage per subscription account): the account key (app/accounts.py) the session last ran under; NULL = unknown
    "ALTER TABLE sessions ADD COLUMN account TEXT",
    # ---- v0.5.21 (issue #57, usage by folder): session id -> the folder a session ran in, learned from Claude's registry and the first lines of its transcript
    #      ('' = looked and found none). Additive: the previous image ignores the table.
    "CREATE TABLE IF NOT EXISTS session_cwd (session_id TEXT PRIMARY KEY, cwd TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL)",
    # ---- box fixes (Codex 0.161): a headless run's token usage as JSON ({input_tokens, cached_input_tokens, output_tokens}); NULL = none reported
    "ALTER TABLE runs ADD COLUMN usage TEXT",
    # ---- nodes epic P1 (issue #137): the board that asked for this task, as JSON {node, user}; NULL = asked for here (every task today, and every old row).
    #      Never a token or a prompt. Additive: the previous image ignores the column.
    "ALTER TABLE tasks ADD COLUMN origin TEXT",
    # permissions.decision takes allow|deny|tui|interrupt (plus timeout from perm_expire). It has no CHECK constraint,
    # so nothing to migrate: the new values are plain TEXT.
]

# Events: the table keeps the newest EVENTS_CAP rows. These names are never stored (statusline fires every few
# seconds per session, PostToolBatch once per tool batch; they drown the interesting events and the cap with them).
# hooks.py must not rely on add_event() for them: it should skip the call (or import SKIP_EVENTS) for these names.
EVENTS_CAP = 20000
SKIP_EVENTS = frozenset({"PostToolBatch", "statusline"})
RESULT_MAX = 20000

# Columns a caller may name in task_add / job_add (the column names are interpolated into SQL, so this is an allowlist;
# an unknown key is a programming error and raises TypeError instead of being silently dropped).
TASK_COLS = ("project", "repo", "slug", "title", "prompt", "branch", "base", "worktree", "tmux_name", "claude_session_id",
             "status", "pr_number", "pr_url", "pr_state", "pr_json", "ci", "cost_usd", "overlap", "archived_at",
             "preview_port", "preview_https",
             "agent", "mode", "phase", "session_row", "auto_close", "parent_id", "chain_id", "spec", "result",
             "result_at", "assigned_at", "done_at", "issue_number", "issue_url", "issue_commented_at", "origin")
TASK_REQUIRED = ("project", "repo", "slug", "title", "prompt")
TASK_UNASSIGNED = ("tmux_name", "worktree", "branch")     # NOT NULL without a default: '' when there is none yet
JOB_COLS = ("project", "repo", "name", "prompt", "cron", "permission_mode", "max_turns", "max_budget_usd", "args",
            "timeout_s", "enabled", "batch_id", "next_run_at", "agent", "opts")
JOB_REQUIRED = ("project", "repo", "name", "prompt")
ACTIVE_TASK_PHASES = ("queued", "running", "done", "failed")


def _json(v):
    """Dict/list -> JSON text; text passes through; None stays None."""
    if v is None or isinstance(v, str):
        return v
    return json.dumps(v)


def _loads(raw, kind):
    """Parse a JSON text column; anything unparsable or of the wrong type is `kind()` (never raises)."""
    if isinstance(raw, kind):
        return raw
    if not raw or not isinstance(raw, (str, bytes)):
        return kind()
    try:
        v = json.loads(raw)
    except ValueError:
        return kind()
    return v if isinstance(v, kind) else kind()


def session_view(row: dict) -> dict:
    """A sessions row shaped for the API: JSON columns parsed (bad JSON never raises), plus the aliases
    agent_session_id (= claude_session_id) and row_id (= id)."""
    d = dict(row)
    d["add_dirs"] = _loads(d.get("add_dirs"), list)
    try:
        d["stats"] = json.loads(d["stats"]) if d.get("stats") else None
    except (ValueError, TypeError):
        d["stats"] = None
    d["flags"] = _loads(d.get("flags"), dict)
    d["opts"] = _loads(d.get("opts"), dict)
    d["agent"] = d.get("agent") or "claude"
    d["agent_session_id"] = d.get("claude_session_id")
    d["row_id"] = d.get("id")
    d["account"] = d.get("account") or None
    return d


def _insert(conn, table: str, vals: dict) -> int:
    cols = list(vals)
    cur = conn.execute(f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(vals.values()))
    return int(cur.lastrowid)


def _task_fields(fields: dict) -> dict:
    """Normalise the JSON/size-bounded task columns (shared by task_add and task_update)."""
    out = dict(fields)
    if "spec" in out:
        out["spec"] = _json(out["spec"])
    if "origin" in out:
        out["origin"] = _json(out["origin"])
    if isinstance(out.get("result"), str):
        out["result"] = out["result"][:RESULT_MAX]
    return out


def _run_view(row) -> dict:
    """A runs row as a dict, its usage column (JSON text) as a dict or None."""
    d = dict(row)
    d["usage"] = _loads(d.get("usage"), dict) or None
    return d


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iso(at=None) -> str:
    """A time as the one text form every timestamp column uses ('2026-10-03T10:00:00+00:00': UTC, whole seconds), so string
    comparison is time comparison. `at` is None (now), a datetime (naive counts as UTC), epoch seconds or an ISO text (a 'Z' suffix,
    an offset or fractional seconds are fine). Anything else raises ValueError."""
    if at is None:
        return now()
    if isinstance(at, datetime):
        d = at
    elif isinstance(at, (int, float)) and not isinstance(at, bool):
        d = datetime.fromtimestamp(at, tz=timezone.utc)
    elif isinstance(at, str) and at.strip():
        t = at.strip()
        d = datetime.fromisoformat(t[:-1] + "+00:00" if t[-1] in "Zz" else t)
    else:
        raise ValueError(f"not a time: {at!r}")
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).replace(microsecond=0).isoformat(timespec="seconds")


def _meta_text(meta) -> str | None:
    """Sample meta as compact JSON text (a str passes through, None stays None)."""
    if meta is None or isinstance(meta, str):
        return meta
    return json.dumps(meta, separators=(",", ":"), default=str)


def _meta_obj(raw):
    """Sample meta text -> parsed JSON (None when absent or unparsable; never raises)."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        # callable(tmux_name, old_state, new_state, event, row), called OUTSIDE the lock (so it may write through this DB) when a
        # session's state really changed: set_state (event = the hook event), end (-> 'ended', event = the reason) and reconcile
        # (-> 'ended', event = 'reconciled'). `row` is the session_view of the row as it is after the change. Exceptions are
        # logged and swallowed. samples.record_state is the one consumer.
        self.on_state_change: Optional[Callable] = None
        self._sample_last: dict[tuple[str, str], dict] = {}      # (series, key) -> {at, value, meta (JSON text)}, newest row only
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            for stmt in MIGRATIONS:
                try:
                    self.conn.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e):
                        raise

    def add_session(self, *, tmux_name, project, repo, name, launcher, cmd=None, claude_session_id=None, add_dirs=None,
                    agent="claude", cwd=None, opts=None, flags=None, account=None) -> int:
        """Insert a session row and return its id (sessions.id, exposed as row_id). The column list is built from the
        values given: a None is never written, so NOT NULL DEFAULT columns (agent) keep their default. `account` is the
        subscription account key the session starts under (accounts.current); None leaves the column NULL (unknown)."""
        vals = {"tmux_name": tmux_name, "project": project, "repo": repo, "name": name, "launcher": launcher, "cmd": cmd,
                "claude_session_id": claude_session_id, "add_dirs": json.dumps(add_dirs or []),
                "agent": agent, "cwd": str(cwd) if cwd is not None else None, "opts": _json(opts), "flags": _json(flags),
                "account": account or None, "created_at": now()}
        with self.lock:
            return _insert(self.conn, "sessions", {k: v for k, v in vals.items() if v is not None})

    def open_rows(self) -> dict[str, dict]:
        """Newest open row per tmux name, shaped by session_view()."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY id ASC"
            ).fetchall()
        return {r["tmux_name"]: session_view(dict(r)) for r in rows}

    def update_flags(self, tmux: str, patch: dict | None = None, incr: dict | None = None) -> dict:
        """Read-modify-write sessions.flags of the newest open row for `tmux`, atomically (one lock hold, so two hook
        threads never lose each other's update). `patch`: key -> value, a None value deletes the key. `incr`: key -> int
        delta added to the current value (a missing or non-integer value counts as 0; the result never goes below 0).
        The patch is applied first, then the increments. Returns the new flags dict ({} and no write when no row is
        open; bad JSON in the column starts over from {})."""
        with self.lock:
            r = self.conn.execute(
                "SELECT id, flags FROM sessions WHERE tmux_name=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
                (tmux,)).fetchone()
            if not r:
                return {}
            old = _loads(r["flags"], dict)
            new = dict(old)
            for k, v in (patch or {}).items():
                if v is None:
                    new.pop(k, None)
                else:
                    new[k] = v
            for k, delta in (incr or {}).items():
                cur = new.get(k)
                cur = cur if isinstance(cur, int) and not isinstance(cur, bool) else 0
                new[k] = max(0, cur + int(delta))
            if new != old:
                self.conn.execute("UPDATE sessions SET flags=? WHERE id=?", (json.dumps(new), r["id"]))
            return new

    def any_session_ever(self) -> bool:
        """Has the board ever started (or seen) a session? Open or ended; feeds state.setup.first_run."""
        with self.lock:
            return self.conn.execute("SELECT 1 FROM sessions LIMIT 1").fetchone() is not None

    def session_ids(self, agent: str | None = None) -> list[dict]:
        """Every session row that learned an agent session id (open or ended), for cost attribution: {project, repo,
        claude_session_id (the id of either agent), agent}. `agent` selects one agent's rows ('claude' or 'codex')."""
        sql, args = "SELECT project, repo, claude_session_id, agent FROM sessions WHERE claude_session_id IS NOT NULL", ()
        if agent:
            sql, args = sql + " AND agent=?", (agent,)
        with self.lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def session_tmux_ids(self, agent: str = "claude") -> list[dict]:
        """Newest session row per tmux name that learned an agent session id: [{tmux_name, claude_session_id}], for pricing a session from its statusline
        samples (series keyed by tmux name). Only the newest row of a name: an older row's id belongs to a conversation the name no longer holds."""
        with self.lock:
            rows = self.conn.execute("SELECT tmux_name, claude_session_id FROM sessions WHERE id IN (SELECT MAX(id) FROM sessions GROUP BY tmux_name)"
                                     " AND claude_session_id IS NOT NULL AND COALESCE(agent, 'claude')=?", (agent,)).fetchall()
        return [dict(r) for r in rows]

    def rebind_session_id(self, name: str, sid: str) -> bool:
        """SessionStart: the conversation in this tmux session is `sid` now (set_state only fills a NULL id; a /resume or /clear starts a
        new conversation under a new id on the same row). Refused (False) when another open row already holds `sid`: one conversation
        belongs to one row."""
        with self.lock:
            if self.conn.execute("SELECT 1 FROM sessions WHERE ended_at IS NULL AND claude_session_id=? AND tmux_name<>? LIMIT 1",
                                 (sid, name)).fetchone():
                return False
            self.conn.execute("UPDATE sessions SET claude_session_id=? WHERE id=(SELECT id FROM sessions WHERE tmux_name=? AND"
                              " ended_at IS NULL ORDER BY id DESC LIMIT 1)", (sid, name))
        return True

    def open_row(self, tmux_name: str) -> dict | None:
        with self.lock:
            r = self.conn.execute(
                "SELECT * FROM sessions WHERE tmux_name=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1", (tmux_name,)
            ).fetchone()
        return session_view(dict(r)) if r else None

    def set_state(self, tmux_name: str, state: str | None, event: str, *, message: str | None = None,
                  prompt: str | None = None, claude_session_id: str | None = None, attention: bool = False) -> None:
        """Update the newest open row. state=None keeps the current state. When the state really changes (old != new, a first
        state counts) on_state_change fires after the write, outside the lock."""
        sets = ["last_event=?"]
        args: list = [event]
        ts = now()
        if state is not None:
            sets += ["state=?", "state_at=?"]
            args += [state, ts]
        if message is not None:
            sets.append("last_message=?")
            args.append(message[:500])
        if prompt is not None:
            sets.append("last_prompt=?")
            args.append(prompt[:500])
        if claude_session_id:
            sets.append("claude_session_id=COALESCE(claude_session_id, ?)")
            args.append(claude_session_id)
        if attention:
            sets.append("acked_at=NULL")
        changed = None
        with self.lock:
            before = None
            if state is not None and self.on_state_change is not None:
                before = self._open_row_locked(tmux_name)
            self.conn.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE id=(SELECT id FROM sessions WHERE tmux_name=? AND"
                f" ended_at IS NULL ORDER BY id DESC LIMIT 1)", (*args, tmux_name))
            if before is not None and before["state"] != state:
                changed = (tmux_name, before["state"], state, event,
                           session_view({**before, "state": state, "state_at": ts, "last_event": event}))
        if changed:
            self._fire_state([changed])

    def _open_row_locked(self, tmux_name: str) -> dict | None:
        """The newest open row of a tmux name as a plain dict (the caller holds self.lock)."""
        r = self.conn.execute(
            "SELECT * FROM sessions WHERE tmux_name=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1", (tmux_name,)).fetchone()
        return dict(r) if r else None

    def _fire_state(self, changes: list[tuple]) -> None:
        """Hand state changes to on_state_change, one call each. Called with the lock released; a failing consumer is logged
        and never breaks the write that triggered it."""
        cb = self.on_state_change
        if cb is None:
            return
        for c in changes:
            try:
                cb(*c)
            except Exception as e:
                log.warning("on_state_change(%s, %s -> %s) failed: %s", c[0], c[1], c[2], e)

    def set_stats(self, tmux_name: str, stats: dict, claude_session_id: str | None = None) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE sessions SET stats=?, claude_session_id=COALESCE(claude_session_id, ?) WHERE id=(SELECT id FROM"
                " sessions WHERE tmux_name=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1)",
                (json.dumps(stats), claude_session_id, tmux_name))

    def set_session_account(self, tmux_name: str, account: str | None) -> bool:
        """Record the subscription account key the newest open row of `tmux_name` runs under (accounts.for_reading attributed one of
        its statusline readings to it). Writes only when the value changes. Returns whether a row was updated."""
        if not account:
            return False
        with self.lock:
            cur = self.conn.execute(
                "UPDATE sessions SET account=? WHERE id=(SELECT id FROM sessions WHERE tmux_name=? AND ended_at IS NULL"
                " ORDER BY id DESC LIMIT 1) AND COALESCE(account, '')<>?", (account, tmux_name, account))
            return cur.rowcount > 0

    def session_cwds(self) -> dict[str, tuple[str, str]]:
        """{agent session id (lower case): (cwd, first_seen)} for every folder learned so far; cwd '' means a transcript was read and named none."""
        with self.lock:
            rows = self.conn.execute("SELECT session_id, cwd, first_seen FROM session_cwd").fetchall()
        return {str(r[0]).lower(): (r[1], r[2]) for r in rows}

    def session_cwd_put(self, rows: list[tuple[str, str]], at=None) -> int:
        """Remember (session id, cwd) pairs. A known folder is never overwritten by another one (the first sighting stands); a '' (looked, found none)
        is replaced by a real folder. Returns the rows written."""
        stamp = iso(at)
        n = 0
        with self.lock:
            for sid, cwd in rows:
                sid = str(sid).lower()
                cur = self.conn.execute("INSERT OR IGNORE INTO session_cwd(session_id, cwd, first_seen) VALUES (?,?,?)", (sid, cwd, stamp))
                if cur.rowcount == 0 and cwd:
                    cur = self.conn.execute("UPDATE session_cwd SET cwd=?, first_seen=? WHERE session_id=? AND cwd=''", (cwd, stamp, sid))
                n += cur.rowcount
        return n

    def session_accounts(self) -> dict[str, str]:
        """{agent session id (lower case): account key} for every session row that has both (open or ended): which subscription
        account a conversation last ran under, for cost.refresh's cost samples. The newest row wins when two rows share an id."""
        with self.lock:
            rows = self.conn.execute("SELECT claude_session_id, account FROM sessions WHERE claude_session_id IS NOT NULL"
                                     " AND account IS NOT NULL ORDER BY id ASC").fetchall()
        return {str(r[0]).lower(): r[1] for r in rows}

    def ack(self, tmux_name: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE sessions SET acked_at=? WHERE tmux_name=? AND ended_at IS NULL", (now(), tmux_name))

    def add_event(self, tmux_name: str, event: str, kind: str | None, message: str | None, payload: dict,
                  agent: str | None = None) -> bool:
        """Store one event (and trim the table to the newest EVENTS_CAP rows). Names in SKIP_EVENTS are never stored.
        Returns whether a row was written."""
        if event in SKIP_EVENTS:
            return False
        with self.lock:
            self.conn.execute(
                "INSERT INTO events(tmux_name, event, kind, message, payload, at, agent) VALUES (?,?,?,?,?,?,?)",
                (tmux_name, event, kind, (message or "")[:500] or None, json.dumps(payload)[:20000], now(), agent))
            self.conn.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?", (EVENTS_CAP,))
        return True

    def recent_events(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, tmux_name, event, kind, message, at FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def last_events(self, tmux_names) -> dict[str, dict]:
        """The newest stored event of each tmux name, {name: {event, kind, at}} (a name without events is absent): one query for
        any number of sessions, on the events_by_session index."""
        names = sorted({n for n in tmux_names if n})
        if not names:
            return {}
        marks = ",".join("?" * len(names))
        with self.lock:
            rows = self.conn.execute(
                f"SELECT tmux_name, event, kind, at FROM events WHERE id IN "
                f"(SELECT MAX(id) FROM events WHERE tmux_name IN ({marks}) GROUP BY tmux_name)", names).fetchall()
        return {r["tmux_name"]: {"event": r["event"], "kind": r["kind"], "at": r["at"]} for r in rows}

    def last_event(self, tmux_name: str) -> dict | None:
        """The newest stored event of one session, {event, kind, at}, or None when it has none."""
        return self.last_events([tmux_name]).get(tmux_name)

    def kv_set(self, key: str, value, at=None) -> None:
        """Store a kv value stamped `at` (per iso(); default now). A caller whose value was read earlier than it is written (the usage
        cache, accounts.poll_usage_cache) passes the reading's own time, so `at` stays "when this was true"."""
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO kv(key, value, at) VALUES (?,?,?)", (key, json.dumps(value), iso(at)))

    def push_sub_add(self, sub: dict) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO push_subs(endpoint, sub, at) VALUES (?,?,?)",
                              (sub["endpoint"], json.dumps(sub), now()))

    def push_sub_del(self, endpoint: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM push_subs WHERE endpoint=?", (endpoint,))

    def push_subs(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT endpoint, sub FROM push_subs").fetchall()
        out = []
        for r in rows:
            try:
                out.append({"endpoint": r[0], "sub": json.loads(r[1])})
            except ValueError:
                continue
        return out

    # ---- jobs & runs
    def job_add(self, **row) -> int:
        """Insert a job. Only the keys given (and not None) are written, so NOT NULL DEFAULT columns
        (permission_mode, max_turns, enabled, agent) take their defaults instead of failing on an explicit None."""
        unknown = set(row) - set(JOB_COLS)
        if unknown:
            raise TypeError(f"job_add: unknown column(s) {sorted(unknown)}")
        vals = {k: v for k, v in row.items() if v is not None}
        if "opts" in vals:
            vals["opts"] = _json(vals["opts"])
        missing = [k for k in JOB_REQUIRED if k not in vals]
        if missing:
            raise TypeError(f"job_add: missing {missing}")
        with self.lock:
            return _insert(self.conn, "jobs", {**vals, "created_at": now()})

    def job_get(self, jid: int) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        return dict(r) if r else None

    def jobs(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def jobs_due(self, at: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM jobs WHERE enabled=1 AND next_run_at IS NOT NULL AND next_run_at <= ?"
                " AND id NOT IN (SELECT job_id FROM runs WHERE status='running') ORDER BY next_run_at ASC", (at,)).fetchall()
        return [dict(r) for r in rows]

    def job_update(self, jid: int, **fields) -> None:
        if not fields:
            return
        with self.lock:
            self.conn.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), jid))

    def job_delete(self, jid: int) -> list[int]:
        """Delete a job and its runs; returns the deleted run ids (the caller removes their output files). Job ids are reused (the table
        has no AUTOINCREMENT and cannot get one additively), so a run left behind would read as a run of the next job with that id."""
        with self.lock:
            rids = [int(r[0]) for r in self.conn.execute("SELECT id FROM runs WHERE job_id=?", (jid,))]
            self.conn.execute("DELETE FROM runs WHERE job_id=?", (jid,))
            self.conn.execute("DELETE FROM jobs WHERE id=?", (jid,))
        return rids

    def run_start(self, jid: int) -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO runs(job_id, started_at) VALUES (?, ?)", (jid, now()))
            return int(cur.lastrowid)

    def run_finish(self, rid: int, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in ("status", "result", "error", "session_id", "cost_usd", "num_turns",
                                                           "worktree", "branch", "task_id", "usage")}
        if "usage" in fields:
            fields["usage"] = _json(fields["usage"]) if isinstance(fields["usage"], dict) and fields["usage"] else None
        with self.lock:
            self.conn.execute(f"UPDATE runs SET finished_at=?, {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                              (now(), *fields.values(), rid))

    def runs_interrupt_stale(self, reason: str = "interrupted: the board restarted while this run was in progress") -> list[int]:
        """Runs still 'running' when the process starts can never finish (their worker thread died with the old
        process) and would exclude their job from jobs_due() forever; close them as errors."""
        with self.lock:
            rows = self.conn.execute("SELECT id, job_id FROM runs WHERE status='running'").fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                self.conn.execute(f"UPDATE runs SET status='error', error=?, finished_at=? WHERE id IN ({','.join('?' * len(ids))})",
                                  (reason, now(), *ids))
                for r in rows:
                    self.conn.execute("UPDATE jobs SET last_status='interrupted' WHERE id=?", (r["job_id"],))
        return ids

    def run_get(self, rid: int) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        return _run_view(r) if r else None

    def runs(self, limit: int = 50, job_id: int | None = None) -> list[dict]:
        q = "SELECT * FROM runs" + (" WHERE job_id=?" if job_id else "") + " ORDER BY id DESC LIMIT ?"
        with self.lock:
            rows = self.conn.execute(q, ((job_id, limit) if job_id else (limit,))).fetchall()
        return [_run_view(r) for r in rows]

    # ---- tasks
    def task_add(self, **row) -> int:
        """Insert a task. Only the keys given (and not None) are written, so NOT NULL DEFAULT columns (agent, mode,
        phase, auto_close, status) take their defaults. A task without tmux_name/worktree/branch is unassigned: those
        store '' (tasks.has_worktree is the guard) and phase defaults to 'backlog'; one with a tmux_name defaults to the
        column default 'running'. Pass phase explicitly (for example 'queued' with a spec) to override either."""
        unknown = set(row) - set(TASK_COLS)
        if unknown:
            raise TypeError(f"task_add: unknown column(s) {sorted(unknown)}")
        vals = _task_fields({k: v for k, v in row.items() if v is not None})
        missing = [k for k in TASK_REQUIRED if k not in vals]
        if missing:
            raise TypeError(f"task_add: missing {missing}")
        for k in TASK_UNASSIGNED:
            vals.setdefault(k, "")
        if "phase" not in vals and not vals["tmux_name"]:
            vals["phase"] = "backlog"
        ts = now()
        with self.lock:
            return _insert(self.conn, "tasks", {**vals, "created_at": ts, "updated_at": ts})

    def active_tasks_by_session(self, phases: tuple[str, ...] = ACTIVE_TASK_PHASES) -> dict[int, list[dict]]:
        """{sessions.id: [{id, title, phase, auto_close}]} for unarchived tasks bound to a session, in ONE query.
        Each list is ordered most relevant first (running, queued, done, failed; newest first inside a phase), so a
        caller that shows one task per session takes the first element."""
        marks = ",".join("?" * len(phases))
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, title, phase, auto_close, session_row FROM tasks"
                f" WHERE session_row IS NOT NULL AND archived_at IS NULL AND phase IN ({marks})"
                " ORDER BY CASE phase WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'done' THEN 2 ELSE 3 END, id DESC",
                tuple(phases)).fetchall()
        out: dict[int, list[dict]] = {}
        for r in rows:
            out.setdefault(int(r["session_row"]), []).append(
                {"id": r["id"], "title": r["title"], "phase": r["phase"], "auto_close": bool(r["auto_close"])})
        return out

    def task_get(self, tid: int) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return dict(r) if r else None

    def tasks(self, include_archived: bool = False) -> list[dict]:
        q = "SELECT * FROM tasks" + ("" if include_archived else " WHERE archived_at IS NULL") + " ORDER BY id DESC"
        with self.lock:
            rows = self.conn.execute(q).fetchall()
        return [dict(r) for r in rows]

    def task_update(self, tid: int, **fields) -> None:
        if not fields:
            return
        fields = _task_fields(fields)
        sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
        with self.lock:
            self.conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), now(), tid))

    def tasks_by_phase(self, phases, include_archived: bool = False) -> list[dict]:
        """Task rows (oldest first) whose phase is one of `phases`: the runtime's sweep and chain advance read these every tick."""
        phases = tuple(phases)
        if not phases:
            return []
        marks = ",".join("?" * len(phases))
        q = f"SELECT * FROM tasks WHERE phase IN ({marks})" + ("" if include_archived else " AND archived_at IS NULL") + " ORDER BY id ASC"
        with self.lock:
            rows = self.conn.execute(q, phases).fetchall()
        return [dict(r) for r in rows]

    def children_of(self, parent_id: int) -> list[dict]:
        """The unarchived tasks queued behind `parent_id` (tasks.parent_id), oldest first: a chain step has one, fan-out has several."""
        with self.lock:
            rows = self.conn.execute("SELECT * FROM tasks WHERE parent_id=? AND archived_at IS NULL ORDER BY id ASC", (parent_id,)).fetchall()
        return [dict(r) for r in rows]

    def tasks_for_session(self, row_id: int, phases=None) -> list[dict]:
        """The unarchived tasks bound to one session row (tasks.session_row), newest first, optionally only those in `phases`."""
        q, args = "SELECT * FROM tasks WHERE session_row=? AND archived_at IS NULL", [row_id]
        if phases:
            q += f" AND phase IN ({','.join('?' * len(tuple(phases)))})"
            args += list(phases)
        with self.lock:
            rows = self.conn.execute(q + " ORDER BY id DESC", args).fetchall()
        return [dict(r) for r in rows]

    def tasks_in_chains(self, chain_ids) -> list[dict]:
        """Every task (archived too: a step's place in its chain must not move when a sibling is archived) of the given chains, with
        just the columns a position needs: id, chain_id, parent_id, phase, title."""
        ids = sorted({c for c in chain_ids if c})
        if not ids:
            return []
        with self.lock:
            rows = self.conn.execute(f"SELECT id, chain_id, parent_id, phase, title FROM tasks WHERE chain_id IN ({','.join('?' * len(ids))})"
                                     " ORDER BY id ASC", ids).fetchall()
        return [dict(r) for r in rows]

    def sessions_ended(self, ids) -> dict[int, dict]:
        """{sessions.id: {ended_at, ended_reason}} for the rows of `ids` (open rows too: ended_at None), one query. The merged tmux
        scan only knows live sessions, so a task card's 'closed after stop' (ended_reason auto_close) comes from here."""
        ids = sorted({int(i) for i in ids if i is not None})
        out: dict[int, dict] = {}
        for i in range(0, len(ids), 500):                      # under SQLite's variable limit however many tasks there are
            chunk = ids[i:i + 500]
            with self.lock:
                rows = self.conn.execute(f"SELECT id, ended_at, ended_reason FROM sessions WHERE id IN ({','.join('?' * len(chunk))})",
                                         chunk).fetchall()
            out.update({int(r["id"]): {"ended_at": r["ended_at"], "ended_reason": r["ended_reason"]} for r in rows})
        return out

    def task_delete(self, tid: int) -> bool:
        """Remove a task row (a backlog, queued or cancelled one: the caller decides). Returns whether a row went."""
        with self.lock:
            return self.conn.execute("DELETE FROM tasks WHERE id=?", (tid,)).rowcount == 1

    def preview_ports_in_use(self) -> set[int]:
        with self.lock:
            rows = self.conn.execute("SELECT preview_https FROM tasks WHERE preview_https IS NOT NULL").fetchall()
        return {int(r[0]) for r in rows}

    def task_slugs(self, project: str, repo: str) -> set[str]:
        with self.lock:
            rows = self.conn.execute("SELECT slug FROM tasks WHERE project=? AND repo=?", (project, repo)).fetchall()
        return {r[0] for r in rows}

    def perm_add(self, tmux_name: str, tool_name: str, summary: str, tool_input) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO permissions(tmux_name, tool_name, summary, input, created_at) VALUES (?,?,?,?,?)",
                (tmux_name, tool_name, summary, json.dumps(tool_input)[:20000], now()))
            return int(cur.lastrowid)

    def perm_get(self, pid: int) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM permissions WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None

    def perm_decide(self, pid: int, decision: str, source: str) -> bool:
        """Record a decision once. Returns False if already decided or unknown."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE permissions SET decision=?, decided_at=?, source=? WHERE id=? AND decision IS NULL",
                (decision, now(), source, pid))
            return cur.rowcount == 1

    def perm_pending(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, tmux_name, tool_name, summary, created_at FROM permissions WHERE decision IS NULL"
                " ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]

    def perm_expire(self, pid: int, decision: str = "timeout") -> None:
        self.perm_decide(pid, decision, "system")

    def kv_del(self, key: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM kv WHERE key=?", (key,))

    def kv_del_prefix(self, prefix: str) -> int:
        """Delete every kv row whose key starts with `prefix` (e.g. the rl_notified:<agent>:<resets_at> gates); returns the count."""
        with self.lock:
            cur = self.conn.execute("DELETE FROM kv WHERE substr(key, 1, ?) = ?", (len(prefix), prefix))
            return cur.rowcount

    def kv_get(self, key: str):
        with self.lock:
            r = self.conn.execute("SELECT value, at FROM kv WHERE key=?", (key,)).fetchone()
        if not r:
            return None
        try:
            return {"value": json.loads(r[0]), "at": r[1]}
        except ValueError:
            return None

    def end(self, tmux_name: str, reason: str = "killed") -> None:
        """Close every open row of this tmux name. reason: killed|auto_close|exited|reconciled|project_deleted. A row that was
        not already 'ended' fires on_state_change(name, old, 'ended', reason, row) once (the newest open row speaks for the name)."""
        ts = now()
        changed = None
        with self.lock:
            before = self._open_row_locked(tmux_name) if self.on_state_change is not None else None
            self.conn.execute(
                "UPDATE sessions SET ended_at=?, ended_reason=? WHERE tmux_name=? AND ended_at IS NULL",
                (ts, reason, tmux_name)
            )
            if before is not None and before["state"] != "ended":
                changed = (tmux_name, before["state"], "ended", reason, self._ended_view(before, ts, reason))
        if changed:
            self._fire_state([changed])

    @staticmethod
    def _ended_view(before: dict, ts: str, reason: str) -> dict:
        return session_view({**before, "state": "ended", "state_at": ts, "ended_at": ts, "ended_reason": reason})

    def reconcile(self, alive: set[str], before: str) -> None:
        """Close rows created before the tmux snapshot `before` whose session no longer exists
        (reboot, manual kill). Rows newer than the snapshot may belong to a session created
        after it was taken, so they are left alone. Each closed name fires on_state_change like end() does."""
        changes = []
        with self.lock:
            rows = self.conn.execute(
                "SELECT DISTINCT tmux_name FROM sessions WHERE ended_at IS NULL AND created_at < ?", (before,)
            ).fetchall()
            stale = [r[0] for r in rows if r[0] not in alive]
            for name in stale:
                prev = self._open_row_locked(name) if self.on_state_change is not None else None
                ts = now()
                self.conn.execute(
                    "UPDATE sessions SET ended_at=?, ended_reason='reconciled' WHERE tmux_name=? AND ended_at IS NULL",
                    (ts, name)
                )
                if prev is not None and prev["state"] != "ended":
                    changes.append((name, prev["state"], "ended", "reconciled", self._ended_view(prev, ts, "reconciled")))
        if changes:
            self._fire_state(changes)

    # ---- samples (time series; the catalogue, throttles and retention live in samples.py)
    def _cache_put(self, series: str, key: str, at: str, value, meta) -> None:
        """Remember the newest row of (series, key). An older (back-dated) row never replaces it, and a key that is not cached yet
        stays uncached (sample_last loads the true newest row on first use). The caller holds self.lock."""
        c = self._sample_last.get((series, key))
        if c is not None and at >= c["at"]:
            self._sample_last[(series, key)] = {"at": at, "value": value, "meta": meta}

    def sample(self, series: str, key: str, value, meta=None, at=None) -> int:
        """Append one sample and return its row id. `at` per iso() (default now); `meta` a dict (stored as JSON) or text."""
        ts, m = iso(at), _meta_text(meta)
        v = None if value is None else float(value)
        with self.lock:
            cur = self.conn.execute("INSERT INTO samples(series, key, at, value, meta) VALUES (?,?,?,?,?)",
                                    (series, key or "", ts, v, m))
            self._cache_put(series, key or "", ts, v, m)
            return int(cur.lastrowid)

    def sample_many(self, rows) -> int:
        """Append many samples in one transaction: rows of (series, key, value, meta, at). Returns how many were written."""
        data = [(s_, k or "", iso(a), None if v is None else float(v), _meta_text(m)) for s_, k, v, m, a in rows]
        if not data:
            return 0
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany("INSERT INTO samples(series, key, at, value, meta) VALUES (?,?,?,?,?)",
                                      [(s_, k, a, v, m) for s_, k, a, v, m in data])
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            for s_, k, a, v, m in data:
                self._cache_put(s_, k, a, v, m)
        return len(data)

    def sample_bump(self, series: str, key: str, at=None, delta: float = 1.0) -> int:
        """Count into the current UTC-hour row of (series, key): when the newest row of the pair falls in the same hour as `at`
        its value grows by `delta` in place, otherwise a row with value `delta` is inserted (stamped `at`, the first event of that
        hour). Read and write share one lock hold, so concurrent hook threads never lose a count. Returns the row id."""
        ts, key = iso(at), key or ""
        with self.lock:
            r = self.conn.execute("SELECT id, at, value, meta FROM samples WHERE series=? AND key=? ORDER BY at DESC, id DESC LIMIT 1",
                                  (series, key)).fetchone()
            if r is not None and r["at"][:13] == ts[:13]:
                v = float(r["value"] or 0) + delta
                self.conn.execute("UPDATE samples SET value=? WHERE id=?", (v, r["id"]))
                self._cache_put(series, key, r["at"], v, r["meta"])
                return int(r["id"])
            cur = self.conn.execute("INSERT INTO samples(series, key, at, value, meta) VALUES (?,?,?,?,NULL)", (series, key, ts, float(delta)))
            self._cache_put(series, key, ts, float(delta), None)
            return int(cur.lastrowid)

    def sample_last(self, series: str, key: str) -> dict | None:
        """The newest sample of (series, key) as {at, value, meta} (meta parsed JSON or None), None when there is none. Served from
        memory once seen and kept current by every write, so throttles cost no query."""
        key = key or ""
        with self.lock:
            c = self._sample_last.get((series, key))
            if c is None:
                r = self.conn.execute("SELECT at, value, meta FROM samples WHERE series=? AND key=? ORDER BY at DESC, id DESC LIMIT 1",
                                      (series, key)).fetchone()
                if r is None:
                    return None
                c = self._sample_last[(series, key)] = {"at": r["at"], "value": r["value"], "meta": r["meta"]}
            c = dict(c)
        return {"at": c["at"], "value": c["value"], "meta": _meta_obj(c["meta"])}

    def samples_query(self, series: str, keys: list[str] | None, since, until=None, *, limit: int | None = None,
                      newest: bool = False) -> list[tuple]:
        """Samples of one series from `since` to `until` (both inclusive, per iso(); until None = no upper bound), oldest first, as
        (at_iso, key, value, meta_json). `keys` None = every key. `limit` caps the rows: the oldest ones, or the newest with
        newest=True (the result is oldest first either way)."""
        q = "SELECT at, key, value, meta FROM samples WHERE series=? AND at>=?"
        args: list = [series, iso(since)]
        if until is not None:
            q += " AND at<=?"
            args.append(iso(until))
        if keys is not None:
            if not keys:
                return []
            q += f" AND key IN ({','.join('?' * len(keys))})"
            args += [k or "" for k in keys]
        q += " ORDER BY at DESC, id DESC" if newest and limit else " ORDER BY at ASC, id ASC"
        if limit:
            q += " LIMIT ?"
            args.append(int(limit))
        with self.lock:
            rows = self.conn.execute(q, args).fetchall()
        out = [(r["at"], r["key"], r["value"], r["meta"]) for r in rows]
        if newest and limit:
            out.reverse()
        return out

    def samples_distinct_keys(self, series: str, since) -> list[str]:
        """Keys that have a sample of `series` since `since`, most recently active first (ties by name)."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT key, MAX(at) AS m FROM samples WHERE series=? AND at>=? GROUP BY key ORDER BY m DESC, key ASC",
                (series, iso(since))).fetchall()
        return [r["key"] for r in rows]

    def samples_prune(self, retention: dict[str, float], now=None) -> int:
        """Delete samples older than their series' retention (days). Series missing from the map are kept. Returns the number
        of rows deleted."""
        base = datetime.fromisoformat(iso(now))
        n = 0
        with self.lock:
            for series, days in retention.items():
                cutoff = (base - timedelta(days=days)).isoformat(timespec="seconds")
                n += self.conn.execute("DELETE FROM samples WHERE series=? AND at<?", (series, cutoff)).rowcount
            self._sample_last.clear()
        return n

    def wal_checkpoint(self) -> tuple | None:
        """PRAGMA wal_checkpoint(TRUNCATE) under the lock (the Sampler runs it after its daily prune so the WAL file shrinks).
        Returns sqlite's (busy, log, checkpointed) row, None when it could not run."""
        with self.lock:
            try:
                r = self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            except sqlite3.OperationalError as e:
                log.warning("wal_checkpoint failed: %s", e)
                return None
        return tuple(r) if r else None
