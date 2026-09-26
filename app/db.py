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
