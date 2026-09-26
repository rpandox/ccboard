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
"""


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
            out[d["tmux_name"]] = d
        return out

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
