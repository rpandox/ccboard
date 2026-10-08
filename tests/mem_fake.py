"""A fake claude-mem worker for the memory tests (test_memory, test_memory_proxy, test_memory_palace, test_memory_contract,
test_memory_writeback). The fixtures `mem_home` and `mem_worker` that use it live in tests/conftest.py.

It answers the way the box's worker 13.31.0 does (box check V11, issue #12, and the scrubbed tests/fixtures/claude_mem_*.json):
/health, /api/readiness, /api/stats, /api/processing-status from fixed bodies; /api/projects, /api/observations, /api/summaries,
/api/search, /api/timeline and /api/observation/<id> computed from `obs` and `summaries` (rows stored the way the worker stores them:
facts, concepts and files_* as JSON-encoded strings, created_at_epoch in ms, agent_type only on full rows). POST /api/memory/save is
recorded in `posts` and answered like the plugin source says. Nothing here ever talks to a real worker.
"""
from __future__ import annotations

import http.server
import json
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

FIXTURES = Path(__file__).resolve().parent / "fixtures"

STATS = {"worker": {"version": "13.29.0", "uptime": 5321, "activeSessions": 2, "sseClients": 0, "port": 37700},
         "database": {"path": "/data/claude-mem.db", "size": 123456789, "observations": 9684, "sessions": 270, "summaries": 170,
                      "firstObservationAt": "2026-07-21T04:12:00Z"}}
PROCESSING = {"isProcessing": True, "queueDepth": 435, "parkedSessions": 1}

# the fields /api/observations leaves out of its list rows (they are on /api/observation/<id> and the search JSON rows only)
LIST_OMITS = ("discovery_tokens", "agent_type", "agent_id", "content_hash", "generated_by_model", "relevance_count", "occurrence_count",
              "metadata", "sync_rev", "synced_at", "origin_device_id", "origin_local_id", "title_norm_key", "reinforcement_dates",
              "last_reinforced")


def fixture(name: str):
    return json.loads((FIXTURES / f"claude_mem_{name}.json").read_text(encoding="utf-8"))


def closed_port() -> int:
    """A loopback port that nothing listens on (bound, then released)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def obs(id: int, project: str, epoch_ms: int, *, type: str = "discovery", title: str | None = None, concepts=(), files_read=(),
        files_modified=(), facts=(), agent_type: str | None = None, platform: str = "claude", session: str | None = None,
        narrative: str | None = None, merged_into: str | None = None, subtitle: str | None = None) -> dict:
    """One observation row as the worker stores it (a full row: /api/observation/<id> and the search JSON carry all of it)."""
    sid = session or f"00000000-0000-4000-8000-{id:012d}"
    return {"id": id, "memory_session_id": sid, "content_session_id": sid, "project": project, "merged_into_project": merged_into,
            "platform_source": platform, "type": type, "title": title if title is not None else f"title {id}",
            "subtitle": subtitle if subtitle is not None else f"subtitle {id}", "narrative": narrative if narrative is not None else f"narrative {id}",
            "text": None, "facts": json.dumps(list(facts)), "concepts": json.dumps(list(concepts)), "files_read": json.dumps(list(files_read)),
            "files_modified": json.dumps(list(files_modified)), "prompt_number": 1,
            "created_at": datetime.fromtimestamp(epoch_ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
            "created_at_epoch": epoch_ms, "discovery_tokens": 100 + id % 50, "agent_type": agent_type,
            "agent_id": f"agent-{id}" if agent_type else None, "content_hash": "<hash>", "generated_by_model": "m", "relevance_count": 0,
            "metadata": None, "sync_rev": "1", "occurrence_count": 1}


def summary(id: int, project: str, epoch_ms: int, *, session: str | None = None, platform: str = "claude") -> dict:
    sid = session or f"00000000-0000-4000-9000-{id:012d}"
    return {"id": id, "session_id": sid, "platform_source": platform, "request": f"request {id}", "investigated": f"investigated {id}",
            "learned": f"learned {id}", "completed": f"completed {id}", "next_steps": f"next {id}", "project": project,
            "created_at": datetime.fromtimestamp(epoch_ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z"), "created_at_epoch": epoch_ms}


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _serve(self):
        w = self.server.worker
        w.requests.append((self.command, self.path))
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n) if n else b""
        if self.command == "POST" and urlsplit(self.path).path == "/api/memory/save" and w.accept_posts:
            w.posts.append((self.path, data))
            code, body = w.save_answer(data)
        elif self.command != "GET":
            code, body = 405, {"error": "method not allowed"}
        else:
            code, body = None, None
        delay = w.delay_for(self.path)
        if delay:
            time.sleep(delay)
        if code is None:
            code, body = w.answer(self.path)
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except OSError:                       # the client gave up (a timeout is the point of some tests)
            pass

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = _serve


class MemWorker:
    """Healthy by default. Per-path knobs: `delay` (seconds; the full path with its query, the bare path, or '*' = every path),
    `delay_match` [(substring of the full path, seconds)], `status` and `body` (dict or raw bytes; keyed by the bare path, or the full
    path to pin one query). Data: `obs`, `summaries`, `projects` (None = the distinct keys of obs and summaries). `posts` records
    POST /api/memory/save bodies when `accept_posts` (the default; any other non-GET is a 405)."""

    def __init__(self):
        self.requests: list[tuple[str, str]] = []
        self.posts: list[tuple[str, bytes]] = []
        self.accept_posts = True
        self.delay: dict[str, float] = {}
        self.delay_match: list[tuple[str, float]] = []
        self.status: dict[str, int] = {}
        self.body: dict[str, object] = {
            "/health": {"status": "ok", "activeSessions": 2, "pid": 4242},
            "/api/readiness": {"status": "ready", "mcpReady": True},
            "/api/stats": STATS,
            "/api/processing-status": PROCESSING,
        }
        self.obs: list[dict] = []
        self.summaries: list[dict] = []
        self.projects: list[str] | None = None
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.worker = self
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    # ---- knobs
    def delay_for(self, path: str) -> float:
        bare = urlsplit(path).path
        for k in (path, bare, "*"):
            if k in self.delay:
                return self.delay[k]
        return max((s for sub, s in self.delay_match if sub in path), default=0)

    def answer(self, path: str):
        bare = urlsplit(path).path
        for k in (path, bare):
            if k in self.body:
                return self.status.get(k, self.status.get(bare, 200)), self.body[k]
        code, body = self.compute(bare, parse_qs(urlsplit(path).query, keep_blank_values=True))
        return self.status.get(bare, code), body

    def save_answer(self, raw: bytes):
        try:
            b = json.loads(raw.decode())
        except ValueError:
            return 400, {"error": "bad json"}
        if not isinstance(b, dict) or set(b) - {"text", "title", "project", "metadata"} or not str(b.get("text") or "").strip():
            return 400, {"error": "invalid body"}
        return 200, {"success": True, "id": 1, "title": b.get("title") or "", "project": b.get("project") or "<default>", "message": "saved"}

    # ---- the computed routes
    @staticmethod
    def _one(q, name, default=None):
        v = q.get(name)
        return v[0] if v else default

    @staticmethod
    def _keys(q) -> list[str] | None:
        if "projects" in q:
            return [k.strip().lower() for v in q["projects"] for k in v.split(",") if k.strip()]
        if "project" in q:
            return [q["project"][0].lower()]
        return None

    @staticmethod
    def _match(row, keys) -> bool:
        return keys is None or str(row.get("project") or "").lower() in keys or str(row.get("merged_into_project") or "").lower() in keys

    def _page(self, rows, q, default_limit=20):
        try:
            limit = max(1, min(100, int(self._one(q, "limit", default_limit))))
        except ValueError:
            limit = default_limit
        try:
            offset = max(0, int(self._one(q, "offset", 0)))
        except ValueError:
            offset = 0
        rows = sorted(rows, key=lambda r: r["created_at_epoch"], reverse=True)
        return rows[offset:offset + limit], len(rows) > offset + limit, offset, limit

    def all_projects(self) -> list[str]:
        if self.projects is not None:
            return list(self.projects)
        seen = []
        for r in self.obs + self.summaries:
            if r["project"] not in seen:
                seen.append(r["project"])
        return seen

    def compute(self, bare: str, q: dict):
        keys = self._keys(q)
        plat = self._one(q, "platformSource")
        sess = self._one(q, "contentSessionId")
        if bare == "/api/projects":
            ps = self.all_projects()
            return 200, {"projects": ps, "sources": ["claude"], "projectsBySource": {"claude": ps}}
        if bare in ("/api/observations", "/api/summaries"):
            src = self.obs if bare == "/api/observations" else self.summaries
            rows = [r for r in src if self._match(r, keys) and (not plat or r.get("platform_source") == plat)
                    and (not sess or sess in (r.get("content_session_id"), r.get("session_id")))]
            page, more, offset, limit = self._page(rows, q)
            if bare == "/api/observations":
                page = [{k: v for k, v in r.items() if k not in LIST_OMITS} for r in page]
            return 200, {"items": page, "hasMore": more, "offset": offset, "limit": limit}
        if bare.startswith("/api/observation/"):
            tail = bare[len("/api/observation/"):]
            hit = next((r for r in self.obs if str(r["id"]) == tail), None)
            return (200, hit) if hit else (404, {"error": f"Observation #{tail} not found"})
        if bare == "/api/search":
            query = self._one(q, "query")
            filters = keys is not None or any(k in q for k in ("type", "obs_type", "dateStart", "dateEnd", "concepts", "files"))
            if not query and not filters:
                return 400, {"error": "Either query or filters required for search", "code": "INVALID_SEARCH_REQUEST"}
            typ = self._one(q, "type")
            rows = [r for r in self.obs if self._match(r, keys) and (not plat or r.get("platform_source") == plat)
                    and (not query or query.lower() in (str(r.get("title")) + " " + str(r.get("narrative"))).lower())]
            ot = self._one(q, "obs_type")
            if ot:
                rows = [r for r in rows if r["type"] in ot.split(",")]
            page, _more, _o, _l = self._page(rows, q)
            sums = [] if typ in ("observations", "prompts") else [s for s in self.summaries if self._match(s, keys)][:5]
            if self._one(q, "format") != "json":
                return 200, {"content": [{"type": "text", "text": f"Found {len(page)} result(s)"}]}
            return 200, {"observations": page, "sessions": sums, "prompts": [], "totalResults": len(page) + len(sums), "query": query or ""}
        if bare == "/api/timeline":
            anchor, query = self._one(q, "anchor"), self._one(q, "query")
            if bool(anchor) == bool(query):
                return 200, fixture("timeline_error")
            rows = [r for r in self.obs if self._match(r, keys)]
            rows.sort(key=lambda r: r["created_at_epoch"])
            lines = [f"# Timeline around anchor: {anchor}", "**Window:** 1 records before -> 1 records after", "", "### Sep 27, 2026", "",
                     "| ID | Time | T | Title | Tokens |", "|----|------|---|-------|--------|"]
            lines += [f"| #{r['id']} | 1:23 PM | ○ | {r['title']} | ~{r['discovery_tokens']} |" for r in rows[:3]]
            return 200, {"content": [{"type": "text", "text": "\n".join(lines) + "\n"}]}
        return 404, {"error": "not found"}

    def paths(self):
        return [p for _, p in self.requests]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
