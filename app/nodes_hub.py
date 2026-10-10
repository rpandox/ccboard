"""The hub read model: every paired board's card and trimmed state, polled in parallel, with the age of each reading (nodes epic, issue #138).

A board with no paired node runs nothing here: `NodeHub` is an object that holds no thread and makes no request until `ensure()` finds a row in the
registry (kv `node_peers`, written by pairing, app/nodes.py) and starts its one scheduler thread. The browser never calls a peer (CSP); the pages read
what this module keeps, from the board's own origin.

Polling
  One scheduler thread wakes about once a second and hands every node that is due to a pool of at most WORKERS (4) threads. A node is polled every
  CCBOARD_NODES_POLL seconds (default 20, never below 5), the next time being chosen when the poll starts, with +-JITTER of random spread, so the cadence
  does not depend on how long a poll takes. A node is never handed to the pool while its previous poll is still running, a poll ends within the 5 s
  timeout of one request (a state request and, at most every CARD_EVERY seconds, a card request), and each node has its own record: a slow node holds one
  worker, never the other nodes' schedule or data.
  Each poll calls only the registry's own address with the token saved for that pair (nodes.PeerClient). `GET /api/node/state` carries `If-None-Match`
  when a state is held; 304 means "unchanged" and costs no body. The card (`GET /api/node`) is read on the first good poll and then every CARD_EVERY seconds.

The record of one node: {peer_id, handle, node_id, name, url, status, polled_at, last_ok_at, age_s, skew_ms, skew_warn, error_kind, card, state, etag, legacy}
  Times are the hub's own clock (never the peer's); `age_s` is the time since `last_ok_at` (null when never). name, url and node_id are the registry's.
  status    online        the last good reading is at most two cycles old
            stale         at most 10 minutes old (the last good state and card are still shown)
            offline       older than that, or never read
            unauthorized  the node answered 401 or 403: the pair was revoked or the token rotated away (the row says "re-pair")
            unpaired      the node answered 404 on a node route, or it reports another node id than the registry holds (error_kind identity_changed)
  error_kind  timeout, refused, tls, http_5xx (any other error status), bad_body (not JSON, wrong shape, a redirect is `refused`), too_large (over 100 KB),
              identity_changed; null when the last poll was good. A failed poll changes status, age and error_kind only: card and state are kept.
  skew_ms   the peer's `node.now` minus the midpoint of the request (hub send time + half the round trip); skew_warn is true above 5000 ms. A 304 carries
            the peer's time in `X-CCBoard-Now`, so the skew is refreshed there too.

A peer answer is untrusted. The body is read as bytes (at most 100 KB), parsed, and rebuilt field by field from a whitelist: unknown keys are dropped,
strings are cut to 200 characters without control characters, lists to 200 rows (the card's to 50), numbers and booleans checked. `node.name` and
`node.url` of the body are dropped; the record's name and url come from the registry. A body whose node id differs from the registry row's is not merged.
Nothing a peer sends is written into the board's database except the record kept in kv `node_last` (at most once a minute, and at stop), which is read
back at start with the true age: a restart shows the last reading as old as it is.

Legacy rows (CCBOARD_NODES) are polled the old way, `GET /api/node/summary` with the hub token, and fill only the summary fields (`summary` in the stored
record, whitelisted); they have no card and no state, and need CCBOARD_HUB_TOKEN to be polled at all.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import random
import re
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from . import health, node_state, nodes
from .config import settings
from .db import iso

log = logging.getLogger("ccboard.nodes.hub")

KV_LAST = "node_last"
WORKERS = 4
TIMEOUT = 5.0                 # seconds one request to a node may take
STOP_WAIT = TIMEOUT + 1.0     # seconds stop() waits for the polls in flight, so none reads the database after it returned
JITTER = 0.1                  # +-10 % of the interval
STALE_AFTER = 600.0           # seconds after the last good reading at which `stale` becomes `offline`
ONLINE_CYCLES = 2             # `online` while the last good reading is at most this many intervals old
CARD_EVERY = 300.0            # seconds between two reads of a node's card
PERSIST_EVERY = 60.0          # kv node_last is written at most this often
SKEW_WARN_MS = 5000
STATE_MAX = node_state.BODY_MAX
CARD_MAX = 64 * 1024
STATES = ("online", "stale", "offline", "unauthorized", "unpaired")
ERROR_KINDS = ("timeout", "refused", "tls", "http_5xx", "bad_body", "too_large", "identity_changed")
ETAG_RE = re.compile(r'W/"[0-9a-f]{8,64}"')
NOW_HEADER = "x-ccboard-now"

_CARD_KEYS = ("app", "api", "node_id", "name", "url", "version", "os", "runtime", "now", "agents", "accounts", "lanes", "load", "sessions", "capabilities", "tailscale")
_SUMMARY_KEYS = ("node", "health", "tmux_down", "sessions", "attention", "tasks", "usage", "backup", "url")
_STATE_KEYS = ("api", "node", "etag_base", "projects", "sessions", "tasks", "needs_you", "usage", "lanes", "login_problems", "truncated")


class Bad(ValueError):
    """A peer's answer that does not have the shape it must have."""


# ---------------------------------------------------------------- validation: a peer's answer is rebuilt from a whitelist

def _s(v, n: int = node_state.STR_MAX):
    return node_state.clean(v, n)


def _b(v):
    return v if isinstance(v, bool) else None


def _i(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and abs(v) < 10 ** 15 else None


def _n(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) == float("inf") or abs(v) > 1e15:
        return None
    return v


def _rows(v, cap: int) -> list[dict]:
    return [r for r in v[:cap] if isinstance(r, dict)] if isinstance(v, list) else []


def scrub(v, depth: int = 4):
    """A JSON-like value cut down to safe size: strings to 200 characters without control characters, lists to 50 items, objects to 50 short keys, depth
    to `depth`, numbers finite. Anything else (and anything deeper) becomes None."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, str):
        return node_state.clean(v) or ""
    if isinstance(v, (int, float)):
        return _n(v)
    if depth <= 0:
        return None
    if isinstance(v, list):
        return [scrub(x, depth - 1) for x in v[:50]]
    if isinstance(v, dict):
        out = {}
        for k, x in list(v.items())[:50]:
            kk = node_state.clean(k, 40) if isinstance(k, str) else None
            if kk:
                out[kk] = scrub(x, depth - 1)
        return out
    return None


def clean_card(card, row: dict) -> dict:
    """The card of a peer, whitelisted and scrubbed; `name` and `url` are the registry's. Bad when it is not an object, is too large, or names another node."""
    if not isinstance(card, dict):
        raise Bad("card")
    if row.get("node_id") and card.get("node_id") != row["node_id"]:
        raise Bad("card of another node")
    out = {k: scrub(card[k]) for k in _CARD_KEYS if k in card}
    out["name"], out["url"] = row.get("name"), row.get("url")
    if len(json.dumps(out)) > CARD_MAX:
        raise Bad("card too large")
    return out


def clean_state(body, row: dict) -> dict:
    """The state of a peer rebuilt from the whitelist of app/node_state.py. Raises Bad for a body that is not an object, has no node block, or whose
    `node.id` is not an id; IdentityChanged (a Bad) when the id is not the registry row's."""
    if not isinstance(body, dict):
        raise Bad("not an object")
    node = body.get("node")
    if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not nodes.ID_RE.fullmatch(node["id"]):
        raise Bad("no node id")
    if row.get("node_id") and node["id"] != row["node_id"]:
        raise IdentityChanged("another node id")
    projects = []
    for p in _rows(body.get("projects"), node_state.PROJECTS_MAX):
        repos = [{"name": _s(r.get("name")), "slug": _s(r.get("slug")) if node_state._SLUG.fullmatch(str(r.get("slug"))) else None,
                  "branch": _s(r.get("branch")), "dirty": _b(r.get("dirty"))} for r in _rows(p.get("repos"), node_state.REPOS_MAX)]
        projects.append({"name": _s(p.get("name")), "repos": repos})
    sessions = [{"tmux": _s(r.get("tmux")), "project": _s(r.get("project")), "repo": _s(r.get("repo")), "session": _s(r.get("session")),
                 "agent": _s(r.get("agent"), 20), "state": _s(r.get("state"), 20), "needs_you": bool(r.get("needs_you")), "kind": _s(r.get("kind"), 20),
                 "since": _s(r.get("since"), 40), "model": _s(r.get("model"), 80)} for r in _rows(body.get("sessions"), node_state.SESSIONS_MAX)]
    tasks = [{"id": _i(r.get("id")), "title": _s(r.get("title")), "phase": _s(r.get("phase"), 20), "agent": _s(r.get("agent"), 20), "project": _s(r.get("project")),
              "repo": _s(r.get("repo")), "branch": _s(r.get("branch")), "tmux": _s(r.get("tmux")), "issue_ref": _s(r.get("issue_ref"), 20),
              "updated_at": _s(r.get("updated_at"), 40)} for r in _rows(body.get("tasks"), node_state.TASKS_MAX)]
    ny = body.get("needs_you") if isinstance(body.get("needs_you"), dict) else {}
    lanes = body.get("lanes") if isinstance(body.get("lanes"), dict) else {}
    usage = {}
    for agent in ("claude", "codex"):
        w = (body.get("usage") or {}).get(agent) if isinstance(body.get("usage"), dict) else None
        if isinstance(w, dict):
            usage[agent] = {"pct": _n(w.get("pct")), "resets_at": w.get("resets_at") if isinstance(w.get("resets_at"), int) and not isinstance(w.get("resets_at"), bool) else _s(w.get("resets_at"), 40),
                            "backoff_until": _s(w.get("backoff_until"), 40), "known": bool(w.get("known")), "limited": bool(w.get("limited"))}
    login = body.get("login_problems")
    return {"api": _i(body.get("api")), "node": {"id": node["id"], "version": _s(node.get("version"), 40), "now": _s(node.get("now"), 40)},
            "etag_base": _s(body.get("etag_base"), 64), "projects": projects, "sessions": sessions, "tasks": tasks,
            "needs_you": {k: _i(ny.get(k)) for k in ("permissions", "input", "errors")}, "usage": usage,
            "lanes": {k: _i(lanes.get(k)) for k in ("cap", "running", "free")},
            "login_problems": [x for x in (_s(a, 20) for a in login[:8]) if x] if isinstance(login, list) else [],
            "truncated": bool(body.get("truncated"))}


class IdentityChanged(Bad):
    """The peer reports another node id than the registry row holds."""


def clean_summary(s) -> dict:
    """The legacy summary: only the keys of the old node summary, scrubbed."""
    if not isinstance(s, dict):
        raise Bad("summary is not an object")
    return {k: scrub(s[k]) for k in _SUMMARY_KEYS if k in s}


def parse_time(v) -> float | None:
    """A peer's ISO time as epoch seconds, None when it is not one."""
    if not isinstance(v, str) or not v:
        return None
    try:
        d = datetime.fromisoformat(v)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def classify(e: BaseException) -> tuple[str | None, str | None]:
    """(hard status, error kind) for a failed call: nothing to present is `unauthorized`; a timeout, a TLS failure, a size and the rest by type."""
    if isinstance(e, nodes.PeerError):
        if e.reason == "no_token":
            return "unauthorized", None
        if e.reason == "too_large":
            return None, "too_large"
        cause = e.cause
        if isinstance(cause, (TimeoutError, socket.timeout)):
            return None, "timeout"
        if isinstance(cause, ssl.SSLError):
            return None, "tls"
        return None, "refused"
    if isinstance(e, (TimeoutError, socket.timeout)):
        return None, "timeout"
    if isinstance(e, ssl.SSLError):
        return None, "tls"
    return None, "refused"


def legacy_failure(e: BaseException) -> dict:
    """The record update for a failed legacy poll (urllib): 401 or 403 is `unauthorized` (the hub token is wrong), 404 `unpaired`, other statuses `http_5xx`;
    a timeout, a TLS failure or a refusal by the type of the cause."""
    code = getattr(e, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        if code in (401, 403):
            return {"hard": "unauthorized", "error_kind": None}
        return {"hard": "unpaired", "error_kind": None} if code == 404 else {"hard": None, "error_kind": "http_5xx"}
    cause = getattr(e, "reason", None)
    if isinstance(cause, BaseException):
        e = cause
    if isinstance(e, (json.JSONDecodeError, UnicodeDecodeError)):
        return {"hard": None, "error_kind": "bad_body"}
    return {"hard": None, "error_kind": classify(e)[1]}


# ---------------------------------------------------------------- the hub

class NodeHub:
    """See the module text. `fetch(row, path, etag)` and `fetch_legacy(url)` are seams for tests; `clock` is the epoch clock (nodes._now), `mono` a monotonic one."""

    def __init__(self, db, *, interval: float | None = None, workers: int = WORKERS, timeout: float = TIMEOUT, jitter: float = JITTER,
                 clock: Callable[[], float] | None = None, mono: Callable[[], float] = time.monotonic, rng: Callable[[], float] = random.random,
                 fetch: Callable | None = None, fetch_legacy: Callable | None = None):
        self.db = db
        self._interval = interval
        self.workers, self.timeout, self.jitter = max(1, min(WORKERS, int(workers))), timeout, jitter
        self._clock = clock
        self.mono, self.rng = mono, rng
        self._fetch = fetch or self._peer_fetch
        self._fetch_legacy = fetch_legacy or (lambda url: health.fetch_node(url, timeout=self.timeout))
        self._lock = threading.RLock()
        self._rec: dict[str, dict] = {}
        self._inflight: set[str] = set()
        self._due: dict[str, float] = {}
        self._thread: threading.Thread | None = None
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None
        self._stop = threading.Event()
        self._last_persist: float | None = None
        self._dirty = False
        self._load()

    # ---- settings and the clock
    @property
    def interval(self) -> float:
        return float(self._interval if self._interval is not None else getattr(settings, "nodes_poll", 20.0))

    def clock(self) -> float:
        return float(self._clock() if self._clock else nodes._now())

    # ---- the registry
    def rows(self) -> list[dict]:
        """The registry rows this hub polls (outgoing rows with a handle). [] on a board with no paired node; reading writes nothing."""
        try:
            return [r for r in nodes.out_peers(self.db) if nodes.valid_handle(r.get("handle"))]
        except Exception as e:
            log.debug("registry could not be read: %s", e.__class__.__name__)
            return []

    def enabled(self) -> bool:
        return bool(self.rows())

    # ---- threads
    def ensure(self) -> bool:
        """Start the scheduler thread when the registry has a row and none runs. True when it started. A board with no row starts nothing."""
        with self._lock:
            if self._stop.is_set() or (self._thread is not None and self._thread.is_alive()) or not self.rows():
                return False
            self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="node-hub")
            self._thread = threading.Thread(target=self._run, name="node-hub", daemon=True)
            self._thread.start()
            return True

    def running(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive())

    def stop(self) -> None:
        from . import nodes_stream                      # the streams to peers (issue #143) end with the hub: their reader threads never touch the database, but nothing is left open behind it
        nodes_stream.close_all("shutdown", wait=1.0)
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        pool = self._pool
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        own = threading.current_thread().name.startswith("node-hub")      # called from a worker or the scheduler: waiting for itself would only burn STOP_WAIT
        deadline = time.monotonic() + (0.0 if own else STOP_WAIT)  # a poll in flight ends by its own timeout; wait for it, so nothing reads the database after stop()
        while time.monotonic() < deadline:
            with self._lock:
                if not self._inflight:
                    break
            time.sleep(0.02)
        self.persist(force=True)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    if not self.tick():
                        return                                  # the last node was removed: the thread ends, ensure() starts a new one
                except Exception as e:
                    log.warning("node hub tick failed: %s", e.__class__.__name__)
                self._stop.wait(self._sleep())
        finally:
            with self._lock:
                pool, self._pool = self._pool, None
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)

    def _sleep(self) -> float:
        with self._lock:
            soon = min(self._due.values(), default=None)
        return 1.0 if soon is None else min(1.0, max(0.05, soon - self.mono()))

    def tick(self) -> bool:
        """One scheduler step: sync the records with the registry, hand every due node (not already in flight) to the pool. False when the registry is empty."""
        rows = self.rows()
        self._sync(rows)
        if not rows:
            return False
        now = self.mono()
        for row in rows:
            pid = row["peer_id"]
            with self._lock:
                if pid in self._inflight or self._due.get(pid, 0.0) > now or self._pool is None:
                    continue
                self._inflight.add(pid)
                self._due[pid] = now + self.interval * (1.0 + self.jitter * (2.0 * self.rng() - 1.0))
                pool = self._pool
            try:
                pool.submit(self._guarded, row)
            except RuntimeError:                                # the pool was shut down under us
                with self._lock:
                    self._inflight.discard(pid)
        return True

    def _guarded(self, row: dict) -> None:
        try:
            self.poll_one(row)
        except Exception as e:                                  # a poll never kills a worker
            log.warning("node poll failed: %s", e.__class__.__name__)
        finally:
            with self._lock:
                self._inflight.discard(row["peer_id"])

    def poll_once(self, handles=None) -> list[dict]:
        """Poll every node (or the handles named) now, through a pool of at most WORKERS threads, and wait for all of them. Returns the records. For tests and
        for a person who asks for a refresh; the scheduler thread does not use it."""
        rows = self.rows()
        self._sync(rows)
        if handles is not None:
            rows = [r for r in rows if r.get("handle") in handles]
        if rows:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.workers, len(rows)), thread_name_prefix="node-hub-once") as pool:
                list(pool.map(self._guarded_once, rows))
        return self.records()

    def _guarded_once(self, row: dict) -> None:
        try:
            self.poll_one(row)
        except Exception as e:
            log.warning("node poll failed: %s", e.__class__.__name__)

    # ---- records
    def _sync(self, rows: list[dict]) -> None:
        """Records follow the registry: a new row gets a blank record, a removed row loses its record (and node_last is rewritten at once)."""
        ids = {r["peer_id"] for r in rows}
        with self._lock:
            gone = [p for p in self._rec if p not in ids]
            for p in gone:
                self._rec.pop(p, None)
                self._due.pop(p, None)
            for r in rows:
                self._rec.setdefault(r["peer_id"], self._blank(r))
        if gone:
            self.persist(force=True)
        if not rows:
            self._drop_last()

    @staticmethod
    def _blank(row: dict) -> dict:
        return {"hard": None, "error_kind": None, "polled_ts": None, "ok_ts": None, "card_ts": None, "skew_ms": None, "card": None, "state": None,
                "etag": None, "summary": None, "node_id": row.get("node_id")}

    def _drop_last(self) -> None:
        try:
            if self.db.kv_get(KV_LAST) is not None:
                self.db.kv_del(KV_LAST)
        except Exception as e:
            log.debug("node_last could not be dropped: %s", e.__class__.__name__)

    def status_of(self, rec: dict, now: float) -> str:
        if rec.get("hard") in ("unauthorized", "unpaired"):
            return rec["hard"]
        ok = rec.get("ok_ts")
        if ok is None:
            return "offline"
        age = max(0.0, now - ok)
        if age <= ONLINE_CYCLES * self.interval:
            return "online"
        return "stale" if age <= STALE_AFTER else "offline"

    def _public(self, row: dict, rec: dict, now: float) -> dict:
        ok = rec.get("ok_ts")
        skew = rec.get("skew_ms")
        return {"peer_id": row["peer_id"], "handle": row.get("handle"), "node_id": row.get("node_id"), "name": row.get("name"), "url": row.get("url"),
                "status": self.status_of(rec, now), "polled_at": iso(rec["polled_ts"]) if rec.get("polled_ts") is not None else None,
                "last_ok_at": iso(ok) if ok is not None else None, "age_s": int(max(0.0, now - ok)) if ok is not None else None,
                "skew_ms": skew, "skew_warn": bool(skew is not None and abs(skew) > SKEW_WARN_MS), "error_kind": rec.get("error_kind"),
                "card": rec.get("card"), "state": rec.get("state"), "etag": rec.get("etag"), "legacy": bool(row.get("legacy")),
                "scopes": row.get("scopes") or []}

    def records(self, handle: str | None = None) -> list[dict]:
        """Every registry row with its record, in registry order (a row never polled shows offline with nothing in it)."""
        now = self.clock()
        out = []
        with self._lock:
            for row in self.rows():
                if handle is not None and row.get("handle") != handle:
                    continue
                rec = self._rec.get(row["peer_id"]) or self._blank(row)
                out.append(self._public(row, rec, now))
        return out

    def etag(self, records: list[dict]) -> str:
        """A weak ETag over the records without the fields that tick by themselves (`age_s`)."""
        canon = json.dumps([{k: v for k, v in r.items() if k != "age_s"} for r in records], separators=(",", ":"), sort_keys=True)
        return 'W/"' + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32] + '"'

    @staticmethod
    def counts(r: dict) -> dict:
        st = r.get("state") or {}
        ss, ts = st.get("sessions") or [], st.get("tasks") or []
        ny = st.get("needs_you") or {}
        return {"sessions": len(ss) if r.get("state") else None, "working": sum(1 for s in ss if s.get("state") == "working") if r.get("state") else None,
                "needs_you": (sum(1 for s in ss if s.get("needs_you")) + (ny.get("permissions") or 0)) if r.get("state") else None,
                "tasks": sum(1 for t in ts if t.get("phase") not in node_state.FINISHED) if r.get("state") else None}

    def list_rows(self) -> dict[str, dict]:
        """{peer_id: the cheap fields of its record} for GET /api/nodes: status, age, error kind, skew, counts."""
        out = {}
        for r in self.records():
            out[r["peer_id"]] = {"status": r["status"], "polled_at": r["polled_at"], "last_ok_at": r["last_ok_at"], "age_s": r["age_s"], "skew_ms": r["skew_ms"],
                                 "skew_warn": r["skew_warn"], "error_kind": r["error_kind"], "counts": self.counts(r)}
        return out

    def summary_rows(self) -> dict | None:
        """state.nodes in today's shape ({value: [{name, url, online, ...}], at}) built from the records; None when there is no row to show. A legacy row
        that was never polled (no hub token, or not yet) is left out, as the old poller left it out of kv `nodes`."""
        recs = self.records()
        out = []
        for r in recs:
            if r["legacy"] and r["last_ok_at"] is None and r["polled_at"] is None:
                continue
            base = {"name": r["name"], "url": r["url"], "online": r["status"] == "online", "status": r["status"], "age_s": r["age_s"], "handle": r["handle"]}
            with self._lock:
                rec = self._rec.get(r["peer_id"]) or {}
            if r["legacy"]:
                if rec.get("summary"):
                    base = {**rec["summary"], **base}
                if r["error_kind"]:
                    base["error"] = r["error_kind"]
            else:
                base.update(self._summary_of(r))
            out.append(base)
        return {"value": out, "at": iso(self.clock())} if out else None

    def _summary_of(self, r: dict) -> dict:
        st, card = r.get("state") or {}, r.get("card") or {}
        if not st and not card:
            return {"error": r["error_kind"]} if r["error_kind"] else {}
        load = card.get("load") if isinstance(card.get("load"), dict) else {}
        ss = st.get("sessions") or []
        c = self.counts(r)
        usage = st.get("usage") or {}
        cl = usage.get("claude") or {}
        out = {"node": r["name"], "sessions": c["sessions"], "attention": c["needs_you"], "tasks": c["tasks"],
               "health": {"host": r["name"], "cpu_pct": load.get("cpu_pct"), "load1": load.get("load1"), "cores": load.get("cores"),
                          "mem": {"pct": load.get("mem_pct")}, "disk": {"pct": load.get("disk_pct")}},
               "usage": {"five_hour": {"used_percentage": cl.get("pct"), "resets_at": cl.get("resets_at")}} if cl.get("known") else None}
        if r["error_kind"]:
            out["error"] = r["error_kind"]
        return out

    # ---- one poll
    def _peer_fetch(self, row: dict, path: str, etag: str | None = None) -> nodes.PeerReply:
        extra = {"If-None-Match": etag} if etag else None
        return nodes.PeerClient(row, db=self.db).get(path, timeout=self.timeout, headers=extra)

    def poll_one(self, row: dict) -> dict:
        """Poll one registry row now and update its record. Safe to call from any thread; two calls for different nodes never wait for each other. Returns the
        public record."""
        pid = row["peer_id"]
        with self._lock:
            rec = self._rec.setdefault(pid, self._blank(row))
            before = dict(rec)
        if row.get("legacy") and not settings.hub_token:
            return self.records(row.get("handle"))[0]                # no credential for the old fetch: nothing is asked, nothing is recorded
        t_sent = self.clock()
        m0 = self.mono()
        upd: dict = {"polled_ts": t_sent}
        try:
            if row.get("legacy"):
                upd.update(self._poll_legacy(row, before))
            else:
                upd.update(self._poll_peer(row, before, t_sent, m0))
        except Exception as e:
            log.warning("node %s: poll failed: %s", row.get("handle"), e.__class__.__name__)
            upd.update({"hard": None, "error_kind": "bad_body"})
        with self._lock:
            cur = self._rec.get(pid)
            if cur is not None:                                     # the row may have been removed while the poll ran
                cur.update(upd)
                self._dirty = True
        self.persist()
        now = self.clock()
        with self._lock:
            return self._public(row, self._rec.get(pid) or self._blank(row), now)

    def _poll_legacy(self, row: dict, rec: dict) -> dict:
        if not settings.hub_token:
            return {}                                               # no credential: the old poller never ran either
        try:
            s = clean_summary(self._fetch_legacy(row["url"]))
        except Bad:
            return {"hard": None, "error_kind": "bad_body"}
        except Exception as e:
            return legacy_failure(e)
        return {"hard": None, "error_kind": None, "ok_ts": self.clock(), "summary": s}

    def _poll_peer(self, row: dict, rec: dict, t_sent: float, m0: float) -> dict:
        etag = rec.get("etag") if rec.get("state") is not None else None
        try:
            r = self._fetch(row, "/api/node/state", etag)
        except Exception as e:
            hard, kind = classify(e)
            return {"hard": hard, "error_kind": kind}
        rt = max(0.0, self.mono() - m0)
        st = r.status
        if st in (401, 403):
            return {"hard": "unauthorized", "error_kind": None}
        if st == 404:
            return {"hard": "unpaired", "error_kind": None}
        if st == 304:
            if etag is None:
                return {"hard": None, "error_kind": "bad_body"}
            out = {"hard": None, "error_kind": None, "ok_ts": self.clock()}
            sk = self._skew(parse_time(r.headers.get(NOW_HEADER)), t_sent, rt)
            if sk is not None:
                out["skew_ms"] = sk
            return {**out, **self._card_update(row, rec)}
        if st != 200:
            return {"hard": None, "error_kind": "http_5xx"}
        if len(r.body) > STATE_MAX:
            return {"hard": None, "error_kind": "too_large"}
        try:
            if not isinstance(r.json, dict):
                raise Bad("not JSON")
            state = clean_state(r.json, row)
        except IdentityChanged:
            return {"hard": "unpaired", "error_kind": "identity_changed"}
        except Bad:
            return {"hard": None, "error_kind": "bad_body"}
        tag = r.headers.get("etag")
        out = {"hard": None, "error_kind": None, "ok_ts": self.clock(), "state": state, "etag": tag if isinstance(tag, str) and ETAG_RE.fullmatch(tag) else None}
        sk = self._skew(parse_time(state["node"].get("now")), t_sent, rt)
        if sk is not None:
            out["skew_ms"] = sk
        return {**out, **self._card_update(row, rec)}

    @staticmethod
    def _skew(peer_now: float | None, t_sent: float, rt: float) -> int | None:
        return None if peer_now is None else int(round((peer_now - (t_sent + rt / 2.0)) * 1000.0))

    def _card_update(self, row: dict, rec: dict) -> dict:
        """The card is read on the first good poll and then every CARD_EVERY seconds; a card that fails or does not validate changes nothing."""
        now = self.clock()
        if rec.get("card") is not None and rec.get("card_ts") is not None and now - rec["card_ts"] < CARD_EVERY:
            return {}
        try:
            r = self._fetch(row, "/api/node", None)
            if r.status != 200 or len(r.body) > CARD_MAX or not isinstance(r.json, dict):
                return {}
            return {"card": clean_card(r.json, row), "card_ts": now}
        except Exception as e:
            log.debug("card of %s could not be read: %s", row.get("handle"), e.__class__.__name__)
            return {}

    # ---- node_last
    def _payload(self) -> dict:
        with self._lock:
            ids = {r["peer_id"]: r for r in self.rows()}
            recs = {}
            for pid, rec in self._rec.items():
                row = ids.get(pid)
                if row is None:
                    continue
                recs[pid] = {"handle": row.get("handle"), "polled_ts": rec.get("polled_ts"), "ok_ts": rec.get("ok_ts"), "card_ts": rec.get("card_ts"),
                             "hard": rec.get("hard"), "error_kind": rec.get("error_kind"), "skew_ms": rec.get("skew_ms"), "card": rec.get("card"),
                             "state": rec.get("state"), "etag": rec.get("etag"), "summary": rec.get("summary")}
        return {"v": 1, "records": recs}

    def persist(self, force: bool = False) -> bool:
        """Write kv node_last: at most once a minute (and at stop), and only when a poll changed something since the last write."""
        now = self.mono()
        with self._lock:
            if not self._dirty and not force:
                return False
            if not force and self._last_persist is not None and now - self._last_persist < PERSIST_EVERY:
                return False
            self._last_persist = now
            self._dirty = False
        try:
            payload = self._payload()
            if not payload["records"]:
                self._drop_last()
            else:
                self.db.kv_set(KV_LAST, payload, at=self.clock())
            return True
        except Exception as e:
            log.warning("node_last could not be written: %s", e.__class__.__name__)
            return False

    def _load(self) -> None:
        """Read kv node_last back. Each record is checked again like a peer answer would be; the status is worked out from the stored `ok_ts` and the
        clock now, so a reading from before a restart is as old as it is."""
        try:
            rec = self.db.kv_get(KV_LAST)
            rows = {r["peer_id"]: r for r in self.rows()}
        except Exception:
            return
        saved = (rec or {}).get("value", {}).get("records") if isinstance((rec or {}).get("value"), dict) else None
        if not isinstance(saved, dict):
            return
        for pid, s in saved.items():
            row = rows.get(pid)
            if row is None or not isinstance(s, dict):
                continue
            blank = self._blank(row)
            try:
                state = clean_state(s["state"], row) if isinstance(s.get("state"), dict) else None
                card = clean_card(s["card"], row) if isinstance(s.get("card"), dict) else None
                summary = clean_summary(s["summary"]) if isinstance(s.get("summary"), dict) else None
            except Bad:
                continue
            ok, pt = s.get("ok_ts"), s.get("polled_ts")
            blank.update({"state": state, "card": card, "summary": summary, "etag": s.get("etag") if isinstance(s.get("etag"), str) and ETAG_RE.fullmatch(s["etag"]) else None,
                          "ok_ts": float(ok) if isinstance(ok, (int, float)) and not isinstance(ok, bool) else None,
                          "polled_ts": float(pt) if isinstance(pt, (int, float)) and not isinstance(pt, bool) else None,
                          "card_ts": float(s["card_ts"]) if isinstance(s.get("card_ts"), (int, float)) and not isinstance(s.get("card_ts"), bool) else None,
                          "hard": s.get("hard") if s.get("hard") in ("unauthorized", "unpaired") else None,
                          "error_kind": s.get("error_kind") if s.get("error_kind") in ERROR_KINDS else None,
                          "skew_ms": _i(s.get("skew_ms"))})
            self._rec[pid] = blank
