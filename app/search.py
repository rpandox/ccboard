"""Transcript full-text search (display only): an FTS5 index over ~/.claude/projects/**/*.jsonl and the Codex rollouts under
~/.codex/sessions/**/rollout-*.jsonl, built incrementally by file offset. Transcripts are never used as a source of state.
claude-mem's observer sessions are not indexed (their transcripts are the observer's own chatter, not the user's work)."""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from pathlib import Path

from .config import settings

log = logging.getLogger("ccboard.search")
POLL_SECONDS = 60
MAX_TEXT = 20_000
MAX_BYTES_PER_CYCLE = 50_000_000
ROLLOUT_ID_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$")

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


def rollout_text(obj: dict) -> tuple[str, str] | None:
    """(kind, text) for a Codex rollout line: what the user typed and what the agent answered (`event_msg` payloads `user_message` and
    `agent_message`). Tool calls, reasoning, token counts and the context Codex injects are skipped. Codex also writes every message as
    a `response_item`; only the event is read, so a message is indexed once and the injected AGENTS.md / environment blocks never are."""
    if obj.get("type") != "event_msg":
        return None
    pl = obj.get("payload")
    if not isinstance(pl, dict):
        return None
    kind = {"user_message": "user", "agent_message": "assistant"}.get(pl.get("type"))
    msg = pl.get("message")
    if kind is None or not isinstance(msg, str) or not msg.strip():
        return None
    return kind, msg[:MAX_TEXT]


def is_observer_path(path: Path | str) -> bool:
    """A claude-mem observer transcript: a path segment `observer-sessions`, or the project-dir slug of such a cwd
    (`-home-x--claude-mem-observer-sessions-NNN`; the segment check keeps an unrelated `-observer-sessions` dir out)."""
    return any(seg == "observer-sessions" or "claude-mem-observer-sessions" in seg for seg in str(path).replace("\\", "/").split("/"))


def rollout_context(path: Path) -> tuple[str, str]:
    """(session id, cwd) of a Codex rollout: only its first line carries them, and an incremental read starts mid-file, so the id comes
    from the file name (rollout-<ts>-<uuid>.jsonl) and the cwd from the cached first line ('' when it cannot be read)."""
    from .agents import codex_discovery            # lazy: the agents package is heavier than this module's own imports
    m = ROLLOUT_ID_RE.search(path.name)
    meta = codex_discovery.first_line_meta(path) or {}
    sid = (m.group(1) if m else None) or (meta.get("id") if isinstance(meta.get("id"), str) else None) or path.stem
    return sid.lower(), meta.get("cwd") if isinstance(meta.get("cwd"), str) else ""


class Indexer(threading.Thread):
    def __init__(self, db, projects_dir: Path | None = None, codex_dir: Path | None = None):
        super().__init__(name="transcript-indexer", daemon=True)
        self.db = db
        self.projects_dir = projects_dir or (settings.claude_config_dir / "projects")
        self._codex_dir = codex_dir                  # None: CODEX_HOME/sessions as settings has it at the time of each pass
        self.stop = threading.Event()
        self.available = False
        self._pruned = False
        with self.db.lock:
            self.available = fts_available(self.db.conn)
            if self.available:
                self.db.conn.executescript(SCHEMA)

    @property
    def codex_dir(self) -> Path:
        return Path(self._codex_dir) if self._codex_dir is not None else Path(settings.codex_home) / "sessions"

    def is_rollout(self, path: Path) -> bool:
        return path.name.startswith("rollout-") and path.is_relative_to(self.codex_dir)

    def files(self) -> list[Path]:
        """Every transcript worth indexing: Claude's (observer sessions left out) and Codex's rollouts."""
        out = []
        if self.projects_dir.is_dir():
            for root, dirs, files in os.walk(self.projects_dir):
                if is_observer_path(Path(root).relative_to(self.projects_dir)):
                    dirs[:] = []                       # nothing below an observer's directory is indexed
                    continue
                for f in files:
                    if f.endswith(".jsonl"):
                        out.append(Path(root) / f)
        if self.codex_dir.is_dir():
            for root, dirs, files in os.walk(self.codex_dir):
                for f in files:
                    if f.startswith("rollout-") and f.endswith(".jsonl"):
                        out.append(Path(root) / f)
        return out

    def _prune_observers(self) -> None:
        """Drop what an earlier version indexed from claude-mem's observer sessions (one statement over the whole index, once per
        process, and only when fts_files names such a file)."""
        self._pruned = True
        like = "path LIKE '%claude-mem-observer-sessions%' OR path LIKE '%/observer-sessions/%'"
        with self.db.lock:
            if self.db.conn.execute(f"SELECT 1 FROM fts_files WHERE {like} LIMIT 1").fetchone():
                self.db.conn.execute(f"DELETE FROM transcript_fts WHERE file IN (SELECT path FROM fts_files WHERE {like})")
                self.db.conn.execute(f"DELETE FROM fts_files WHERE {like}")

    def index_once(self) -> int:
        """Index new bytes of every transcript. Returns the number of entries added."""
        if not self.available:
            return 0
        if not self._pruned:
            self._prune_observers()
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
            rollout = self.is_rollout(path)
            r_sid, r_cwd = rollout_context(path) if rollout else ("", "")
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
                got = rollout_text(obj) if rollout else extract_text(obj)
                if not got:
                    continue
                kind, text = got
                if rollout:
                    entries.append((text, r_sid, r_cwd, str(obj.get("timestamp") or ""), kind, key, offset + lineno))
                else:
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


def agent_of(file) -> str:
    """Which agent wrote the transcript file a hit came from: 'codex' for a rollout under CODEX_HOME/sessions, else 'claude'."""
    try:
        return "codex" if Path(str(file)).is_relative_to(Path(settings.codex_home) / "sessions") else "claude"
    except (ValueError, TypeError):
        return "claude"


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
