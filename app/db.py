"""SQLite state. tmux is the truth for 'alive'; rows hold what tmux cannot."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

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
"""

# Added after v0.1; applied with ALTER TABLE, "duplicate column" errors are ignored.
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
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
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

    def add_session(self, *, tmux_name, project, repo, name, launcher, cmd, claude_session_id, add_dirs):
        with self.lock:
            self.conn.execute(
                "INSERT INTO sessions(tmux_name, project, repo, name, launcher, cmd, claude_session_id,"
                " add_dirs, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (tmux_name, project, repo, name, launcher, cmd, claude_session_id,
                 json.dumps(add_dirs or []), now()),
            )

    def open_rows(self) -> dict[str, dict]:
        """Newest open row per tmux name."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY id ASC"
            ).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            d = dict(r)
            d["add_dirs"] = json.loads(d.get("add_dirs") or "[]")
            try:
                d["stats"] = json.loads(d["stats"]) if d.get("stats") else None
            except ValueError:
                d["stats"] = None
            out[d["tmux_name"]] = d
        return out

    def session_ids(self) -> list[dict]:
        """Every session row that learned a Claude session id (open or ended), for cost attribution."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT project, repo, claude_session_id FROM sessions WHERE claude_session_id IS NOT NULL").fetchall()
        return [dict(r) for r in rows]

    def open_row(self, tmux_name: str) -> dict | None:
        with self.lock:
            r = self.conn.execute(
                "SELECT * FROM sessions WHERE tmux_name=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1", (tmux_name,)
            ).fetchone()
        return dict(r) if r else None

    def set_state(self, tmux_name: str, state: str | None, event: str, *, message: str | None = None,
                  prompt: str | None = None, claude_session_id: str | None = None, attention: bool = False) -> None:
        """Update the newest open row. state=None keeps the current state."""
        sets = ["last_event=?"]
        args: list = [event]
        if state is not None:
            sets += ["state=?", "state_at=?"]
            args += [state, now()]
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
        with self.lock:
            self.conn.execute(
                f"UPDATE sessions SET {', '.join(sets)} WHERE id=(SELECT id FROM sessions WHERE tmux_name=? AND"
                f" ended_at IS NULL ORDER BY id DESC LIMIT 1)", (*args, tmux_name))

    def set_stats(self, tmux_name: str, stats: dict, claude_session_id: str | None = None) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE sessions SET stats=?, claude_session_id=COALESCE(claude_session_id, ?) WHERE id=(SELECT id FROM"
                " sessions WHERE tmux_name=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1)",
                (json.dumps(stats), claude_session_id, tmux_name))

    def ack(self, tmux_name: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE sessions SET acked_at=? WHERE tmux_name=? AND ended_at IS NULL", (now(), tmux_name))

    def add_event(self, tmux_name: str, event: str, kind: str | None, message: str | None, payload: dict) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO events(tmux_name, event, kind, message, payload, at) VALUES (?,?,?,?,?,?)",
                (tmux_name, event, kind, (message or "")[:500] or None, json.dumps(payload)[:20000], now()))
            self.conn.execute("DELETE FROM events WHERE id < (SELECT MAX(id) FROM events) - 5000")

    def recent_events(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, tmux_name, event, kind, message, at FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def kv_set(self, key: str, value) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO kv(key, value, at) VALUES (?,?,?)", (key, json.dumps(value), now()))

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
        cols = ["project", "repo", "name", "prompt", "cron", "permission_mode", "max_turns", "max_budget_usd", "args",
                "timeout_s", "enabled", "batch_id", "next_run_at"]
        with self.lock:
            cur = self.conn.execute(
                f"INSERT INTO jobs({', '.join(cols)}, created_at) VALUES ({', '.join('?' * len(cols))}, ?)",
                (*[row.get(c) for c in cols], now()))
            return int(cur.lastrowid)

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

    def job_delete(self, jid: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM jobs WHERE id=?", (jid,))

    def run_start(self, jid: int) -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO runs(job_id, started_at) VALUES (?, ?)", (jid, now()))
            return int(cur.lastrowid)

    def run_finish(self, rid: int, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in ("status", "result", "error", "session_id", "cost_usd", "num_turns",
                                                           "worktree", "branch", "task_id")}
        with self.lock:
            self.conn.execute(f"UPDATE runs SET finished_at=?, {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                              (now(), *fields.values(), rid))

    def run_get(self, rid: int) -> dict | None:
        with self.lock:
            r = self.conn.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None

    def runs(self, limit: int = 50, job_id: int | None = None) -> list[dict]:
        q = "SELECT * FROM runs" + (" WHERE job_id=?" if job_id else "") + " ORDER BY id DESC LIMIT ?"
        with self.lock:
            rows = self.conn.execute(q, ((job_id, limit) if job_id else (limit,))).fetchall()
        return [dict(r) for r in rows]

    # ---- tasks
    def task_add(self, **row) -> int:
        cols = ["project", "repo", "slug", "title", "prompt", "branch", "base", "worktree", "tmux_name", "claude_session_id"]
        with self.lock:
            cur = self.conn.execute(
                f"INSERT INTO tasks({', '.join(cols)}, created_at, updated_at) VALUES ({', '.join('?' * len(cols))}, ?, ?)",
                (*[row.get(c) for c in cols], now(), now()))
            return int(cur.lastrowid)

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
        sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
        with self.lock:
            self.conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), now(), tid))

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

    def kv_get(self, key: str):
        with self.lock:
            r = self.conn.execute("SELECT value, at FROM kv WHERE key=?", (key,)).fetchone()
        if not r:
            return None
        try:
            return {"value": json.loads(r[0]), "at": r[1]}
        except ValueError:
            return None

    def end(self, tmux_name: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE sessions SET ended_at=? WHERE tmux_name=? AND ended_at IS NULL", (now(), tmux_name)
            )

    def reconcile(self, alive: set[str], before: str) -> None:
        """Close rows created before the tmux snapshot `before` whose session no longer exists
        (reboot, manual kill). Rows newer than the snapshot may belong to a session created
        after it was taken, so they are left alone."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT DISTINCT tmux_name FROM sessions WHERE ended_at IS NULL AND created_at < ?", (before,)
            ).fetchall()
            stale = [r[0] for r in rows if r[0] not in alive]
            for name in stale:
                self.conn.execute(
                    "UPDATE sessions SET ended_at=? WHERE tmux_name=? AND ended_at IS NULL", (now(), name)
                )
