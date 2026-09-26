"""Transcript full-text search (display only): an FTS5 index over ~/.claude/projects/**/*.jsonl,
built incrementally by file offset. Transcripts are never used as a source of state."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from pathlib import Path

from .config import settings

log = logging.getLogger("ccboard.search")
POLL_SECONDS = 60
MAX_TEXT = 20_000
MAX_BYTES_PER_CYCLE = 50_000_000

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
  text, session_id UNINDEXED, cwd UNINDEXED, ts UNINDEXED, kind UNINDEXED, file UNINDEXED, line UNINDEXED,
  tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS fts_files (path TEXT PRIMARY KEY, offset INTEGER NOT NULL, mtime REAL NOT NULL);
"""


def fts_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def extract_text(obj: dict) -> tuple[str, str] | None:
    """(kind, text) for user/assistant lines; tool results and tool calls are skipped."""
    kind = obj.get("type")
    if kind not in ("user", "assistant"):
        return None
    msg = obj.get("message") or {}
    content = msg.get("content")
    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
                parts.append(c["text"])
    text = "\n".join(p for p in parts if p and p.strip())
    if not text.strip() or text.lstrip().startswith("<command-name>") or text.lstrip().startswith("<local-command"):
        return None
    if obj.get("isSidechain") or obj.get("agentId"):
        kind = "subagent"
    return kind, text[:MAX_TEXT]


class Indexer(threading.Thread):
    def __init__(self, db, projects_dir: Path | None = None):
        super().__init__(name="transcript-indexer", daemon=True)
        self.db = db
        self.projects_dir = projects_dir or (settings.claude_config_dir / "projects")
        self.stop = threading.Event()
        self.available = False
        with self.db.lock:
            self.available = fts_available(self.db.conn)
            if self.available:
                self.db.conn.executescript(SCHEMA)

    def files(self) -> list[Path]:
        if not self.projects_dir.is_dir():
            return []
        out = []
        for root, dirs, files in os.walk(self.projects_dir):
            for f in files:
                if f.endswith(".jsonl"):
                    out.append(Path(root) / f)
        return out

    def index_once(self) -> int:
        """Index new bytes of every transcript. Returns the number of entries added."""
        if not self.available:
            return 0
        added = 0
        budget = MAX_BYTES_PER_CYCLE
        for path in self.files():
            try:
                st = path.stat()
            except OSError:
                continue
            key = str(path)
            with self.db.lock:
                row = self.db.conn.execute("SELECT offset, mtime FROM fts_files WHERE path=?", (key,)).fetchone()
            offset = int(row[0]) if row else 0
            if row and st.st_size == offset and st.st_mtime == row[1]:
                continue
            if st.st_size < offset:  # rewritten/truncated: reindex from scratch
                with self.db.lock:
                    self.db.conn.execute("DELETE FROM transcript_fts WHERE file=?", (key,))
                offset = 0
            if budget <= 0:
                break
            try:
                with path.open("rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read(min(budget, st.st_size - offset))
            except OSError:
                continue
            budget -= len(chunk)
            last_nl = chunk.rfind(b"\n")
            if last_nl < 0:
                continue
            body = chunk[: last_nl + 1]
            new_offset = offset + len(body)
            entries = []
            lineno = 0
            for raw in body.split(b"\n"):
                lineno += 1
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                got = extract_text(obj)
                if not got:
                    continue
                kind, text = got
                entries.append((text, str(obj.get("sessionId") or path.stem), str(obj.get("cwd") or ""),
                                str(obj.get("timestamp") or ""), kind, key, offset + lineno))
            with self.db.lock:
                if entries:
                    self.db.conn.executemany(
                        "INSERT INTO transcript_fts(text, session_id, cwd, ts, kind, file, line) VALUES (?,?,?,?,?,?,?)", entries)
                self.db.conn.execute("INSERT OR REPLACE INTO fts_files(path, offset, mtime) VALUES (?,?,?)",
                                     (key, new_offset, st.st_mtime))
            added += len(entries)
        return added

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                n = self.index_once()
                if n:
                    log.info("indexed %d transcript entries", n)
            except Exception as e:
                log.warning("transcript indexing failed: %s", e)
            self.stop.wait(POLL_SECONDS)


def _sanitize(q: str) -> str:
    q = q.strip()[:200]
    return '"' + q.replace('"', '""') + '"'


def search(db, q: str, limit: int = 30) -> list[dict]:
    q = q.strip()
    if not q:
        return []
    sql = ("SELECT session_id, cwd, ts, kind, file, line, snippet(transcript_fts, 0, '[', ']', '…', 24) AS snippet"
           " FROM transcript_fts WHERE transcript_fts MATCH ? ORDER BY bm25(transcript_fts) LIMIT ?")
    with db.lock:
        try:
            rows = db.conn.execute(sql, (q, limit)).fetchall()
        except sqlite3.OperationalError:  # unbalanced quotes / operators: fall back to a phrase
            try:
                rows = db.conn.execute(sql, (_sanitize(q), limit)).fetchall()
            except sqlite3.OperationalError:
                return []
    return [dict(r) for r in rows]
