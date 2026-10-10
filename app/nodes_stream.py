"""The read-only stream relay (nodes epic, issue #143): a hub page watches a session that runs on another node as a live tail, and the browser never contacts that node.

The two routes
  peer  GET /api/node/stream?names=a--b--c&lines=12   (app/main.py api_node_stream) a paired node's token with the `sessions` scope. Screen text is session content,
        so `read` is never enough. The same generator as GET /api/stream with the same limits (at most 20 names, lines 1 to 40) and one more rule: `names` is
        required. Every tail goes through the cap-then-redact pass of the pane row (nodes_relay.screen_lines), and the tick lists the asked names only. Refusals
        are plain answers before the stream starts (403 without the scope, 400 for a bad name or line count). One `in` audit row per open, with the names.
  hub   GET /api/nodes/{handle}/stream?names=...&lines=...   a signed-in person with X-CCBoard, nothing else: a node token is a 403 from the middleware, the local
        hook token is a 403 `human_only` (it is held by every agent session on the box). It checks, in this order, and makes no request when one refuses:
          caller 403, rate (the read bucket per person) 429, handle 400/404, own address 400, legacy row 409, the pair's scope `sessions` 409, names and lines 400,
          the hub's reading of the node (offline, re-pair, unpaired, not read yet: ONE `gone` event and the stream closes, status 200, because an EventSource cannot
          read an error body), the caps (2 streams per node, 6 per signed-in person, 8 in total: 429 with Retry-After). `once` and any other query key are a 400 here and
          are never put on the upstream path (only names and lines are).
        Then it opens ONE upstream request through nodes.PeerClient (the address rule at the call, TLS verified, no redirect, `Accept: text/event-stream`) and
        reads it in one daemon thread per stream (a blocking read, a bounded hand-over to the event loop, never more than 8 threads on a hub).

What the browser gets: the events of this module only, written by this module (the upstream's bytes are never copied)
  retry: 5000                       the page reconnects no faster than once per 5 s
  event: lines  data: {"name", "lines": [..]}      name in the asked set; at most the asked number of lines (40 at most), each cut and redacted by
                                                   nodes_relay.screen_lines_from (cap first, then the regexes, so no peer text reaches a pattern unbounded)
  event: tick   data: {"sessions": [..]}           the asked names that are alive on the peer
  event: gone   data: {"reason", "message", "final", "dropped"}   the end of the stream, always, with a reason the tile can show; `final` is true when
                                                   reconnecting cannot help (re-pair, unpaired, the pair was removed); `dropped` counts upstream events refused
  : ping                            a comment every 15 s while nothing else is sent, so a proxy keeps the connection
  Headers: Content-Type text/event-stream, Cache-Control no-store, X-Accel-Buffering no.
  Reasons: offline, repair, unpaired, not_read_yet, upstream_error, closed (the peer ended it), idle (60 s without a valid event), too_large (a line over 64 KB),
  revoked (the pair was removed or the peer refused its token), shutdown (this board is stopping).
  Only `lines` and `tick` cross; any other event, bad JSON, a name outside the asked set, a shape that is not an object is dropped and counted. The hub never
  retries by itself.

Lifecycle: the upstream ends when the browser leaves (checked every second), on a `gone`, on hub shutdown (close_all), and when the pair is removed or the peer
answered 401 (nodes.stream_closer). Every stream holds a lease; a lease nobody renews for LEASE_TTL seconds is closed by the next open, so a response that never started
cannot hold a slot. A reader thread never touches the database, so a stopped hub has nothing to race.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import StreamingResponse

from . import nodes, nodes_relay as nr, projects

log = logging.getLogger("ccboard.nodes.stream")

MAX_PER_NODE = 2                    # upstream streams per paired node
MAX_TOTAL = 8                       # upstream streams on one hub
MAX_PER_USER = 6                    # upstream streams one signed-in person may hold: one person cannot take the hub's whole share
LINE_MAX = 64 * 1024                # bytes of one upstream line (and of one event's data)
IDLE_S = 60.0                       # seconds without a valid event before the stream is closed
BEAT_S = 15.0                       # a comment heartbeat while nothing else is sent
POLL_S = 1.0                        # how often the pump looks at the browser, the lease and the clock
OPEN_TIMEOUT = 10.0                 # seconds the upstream request may take to answer with its headers
QUEUE_MAX = 256                     # lines the reader may hand over before the pump has taken them (back-pressure)
RETRY_AFTER = 5                     # seconds in the 429 of a full hub, and the EventSource retry hint
LEASE_TTL = IDLE_S + BEAT_S + 30.0  # a lease the pump has not renewed for this long is closed by the next open
LINES_MAX = 40
mono = time.monotonic               # tests replace it (the idle limit runs on it)

HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no", "X-Content-Type-Options": "nosniff"}
STREAM_ROW = nr.Row("stream", "GET", "/api/nodes/{handle}/stream", "GET", "/api/node/stream", "sessions", nr.NoFields, nr.READ, "read_stream", human_only=True,
                    target=lambda p: "stream")
nr.register_scopes([STREAM_ROW])    # the peer route is in nodes.NODE_ROUTES with scope `sessions`, as soon as this module is imported

MESSAGES = {
    "offline": "{n} is offline",
    "repair": "{n} no longer takes this board's token: re-pair",
    "unpaired": "{n} answers as another node or no longer knows this board: remove it and pair it again",
    "not_read_yet": "{n} has not been read yet: wait for the first reading",
    "upstream_error": "{n} could not be read",
    "closed": "{n} ended the stream",
    "idle": "{n} sent nothing for 60 s",
    "too_large": "{n} sent a line over 64 KB",
    "revoked": "the pair with {n} was removed",
    "shutdown": "this board is stopping",
}
FINAL = frozenset({"repair", "unpaired", "revoked", "shutdown"})      # reconnecting cannot help


# ================================================================ leases: the caps and the cleanup

class Lease:
    """One open upstream stream. `close()` may be called from any thread: it flags the stop and closes the upstream, which unblocks the reader."""

    def __init__(self, peer_id: str, handle: str, user: str = ""):
        self.peer_id, self.handle, self.user = peer_id, handle, user
        self.stop = threading.Event()
        self.reason: str | None = None
        self.stream = None
        self.thread: threading.Thread | None = None
        self.dropped = 0
        self.expires = mono() + LEASE_TTL

    def touch(self) -> None:
        self.expires = mono() + LEASE_TTL

    def close(self, reason: str = "closed") -> None:
        if self.reason is None:
            self.reason = reason
        self.stop.set()
        nodes._close_quietly(self.stream)


class Leases:
    def __init__(self):
        self._lock = threading.Lock()
        self._open: set[Lease] = set()

    def acquire(self, peer_id: str, handle: str, user: str = "") -> Lease:
        """A lease, or nr.RelayError 429 (with Retry-After) when the node already has MAX_PER_NODE streams, the person already holds MAX_PER_USER or the hub has
        MAX_TOTAL. A person is whoever the hub's caller check named (the signed-in login)."""
        now = mono()
        with self._lock:
            for old in [x for x in self._open if x.expires < now]:     # a lease nobody renews: its response never started or its pump died
                self._open.discard(old)
                old.close("closed")
            mine = sum(1 for x in self._open if x.peer_id == peer_id)
            yours = sum(1 for x in self._open if x.user == user)
            if mine >= MAX_PER_NODE or yours >= MAX_PER_USER or len(self._open) >= MAX_TOTAL:
                what, cap = (("this node already has", MAX_PER_NODE) if mine >= MAX_PER_NODE else ("you already hold", MAX_PER_USER) if yours >= MAX_PER_USER
                             else ("this board already has", MAX_TOTAL))
                raise nr.RelayError(429, "stream_limit", f"{what} {cap} live streams: close one first, then try again",
                                    headers={"Retry-After": str(RETRY_AFTER)}, kind="refused")
            lease = Lease(peer_id, handle, user)
            self._open.add(lease)
            return lease

    def release(self, lease: Lease) -> None:
        with self._lock:
            self._open.discard(lease)

    def close_node(self, peer_id: str, reason: str = "revoked") -> int:
        with self._lock:
            hit = [x for x in self._open if x.peer_id == peer_id]
        for x in hit:
            x.close(reason)
        return len(hit)

    def close_all(self, reason: str = "shutdown", wait: float = 2.0) -> int:
        with self._lock:
            hit = list(self._open)
        for x in hit:
            x.close(reason)
        end = time.monotonic() + wait
        for x in hit:
            t = x.thread
            if t is not None and t is not threading.current_thread():
                t.join(max(0.0, end - time.monotonic()))
        return len(hit)

    def count(self, peer_id: str | None = None) -> int:
        with self._lock:
            return sum(1 for x in self._open if peer_id is None or x.peer_id == peer_id)


LEASES = Leases()
nodes.stream_closer = lambda peer_id: LEASES.close_node(peer_id, "revoked")


def close_all(reason: str = "shutdown", wait: float = 2.0) -> int:
    """End every open stream (hub shutdown) and wait a moment for the reader threads. Called before the hub and the database stop."""
    return LEASES.close_all(reason, wait)


def open_count(peer_id: str | None = None) -> int:
    return LEASES.count(peer_id)


# ================================================================ the reader thread and the parser

def _read_loop(lease: Lease, stream, loop, q: asyncio.Queue, credits: threading.BoundedSemaphore) -> None:
    """Read the upstream in pieces and hand whole lines to the event loop, at most QUEUE_MAX at a time. Ends on the end of the stream, on a line over LINE_MAX, on an
    error and when the lease is stopped (close() on the stream makes a blocked read return). Touches nothing but the stream and the queue: no database."""
    why = "closed"

    def post(item) -> bool:
        try:
            loop.call_soon_threadsafe(q.put_nowait, item)
            return True
        except RuntimeError:                      # the loop is gone
            return False

    buf = b""
    try:
        while not lease.stop.is_set():
            chunk = stream.read(8192)
            if not chunk:
                break
            buf += chunk
            while True:
                i = buf.find(b"\n")
                if i < 0:
                    break
                line, buf = buf[:i], buf[i + 1:]
                if len(line) > LINE_MAX:
                    why = "too_large"
                    return
                while not credits.acquire(timeout=0.5):             # back-pressure: the pump has not taken the earlier lines
                    if lease.stop.is_set():
                        return
                if not post(("line", line)):
                    return
            if len(buf) > LINE_MAX:
                why = "too_large"
                return
    except Exception as e:
        if not lease.stop.is_set():
            why = "upstream_error"
            log.debug("stream read failed: %s", e.__class__.__name__)
    finally:
        nodes._close_quietly(stream)
        post(("end", why))


class TooLarge(Exception):
    """An upstream event over LINE_MAX bytes."""


class Parser:
    """The upstream's event lines, parsed. `feed(line)` answers the events that may cross, already rebuilt: [("lines", {name, lines}), ("tick", {sessions})].
    Anything else (another event type, bad JSON, a shape that is not an object, a name outside the asked set) is dropped and counted in `dropped`. No peer text
    reaches a regex before nodes_relay.screen_lines_from has cut it to its line and width caps."""

    def __init__(self, requested: frozenset, nlines: int):
        self.requested, self.nlines = requested, max(1, min(int(nlines), LINES_MAX))
        self.event: str | None = None
        self.data: list[str] = []
        self.size = 0
        self.dropped = 0

    def feed(self, raw: bytes) -> list[tuple[str, dict]]:
        line = raw.rstrip(b"\r").decode("utf-8", "replace")
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return []
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            self.event = value[:40]
        elif field == "data":
            self.size += len(value) + 1
            if self.size > LINE_MAX:
                raise TooLarge()
            self.data.append(value)
        return []

    def _dispatch(self) -> list[tuple[str, dict]]:
        event, data = self.event, self.data
        self.event, self.data, self.size = None, [], 0
        if event is None and not data:
            return []
        if event not in ("lines", "tick"):
            self.dropped += 1
            return []
        try:
            obj = json.loads("\n".join(data))
        except (ValueError, RecursionError):
            self.dropped += 1
            return []
        if not isinstance(obj, dict):
            self.dropped += 1
            return []
        if event == "lines":
            name, lines = obj.get("name"), obj.get("lines")
            if not isinstance(name, str) or name not in self.requested or not isinstance(lines, list):
                self.dropped += 1
                return []
            return [("lines", {"name": name, "lines": nr.screen_lines_from(lines, self.nlines)})]
        sessions = obj.get("sessions")
        if not isinstance(sessions, list):
            self.dropped += 1
            return []
        seen: list[str] = []
        for x in sessions[:200]:
            if isinstance(x, str) and x in self.requested and x not in seen:
                seen.append(x)
        return [("tick", {"sessions": seen})]


def sse(event: str, data: dict) -> bytes:
    """One event of the browser's stream. json.dumps escapes every newline, so a value can never end the event early."""
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode("utf-8")


def gone(reason: str, name: str, dropped: int = 0, message: str | None = None) -> bytes:
    text = message or MESSAGES.get(reason, "{n} ended the stream").format(n=name)
    return sse("gone", {"reason": reason, "message": nodes._scrub(text, 200) or "", "final": reason in FINAL, "dropped": dropped})


# ================================================================ the pump (the browser's response)

async def pump(request: Request, lease: Lease, stream, name: str, requested: frozenset, nlines: int):
    """The body of the hub's response: the retry hint, then the re-serialised events, a heartbeat every BEAT_S, and at the end a `gone`. Always ends the upstream
    and gives the lease back, whoever ends first (the upstream, the browser, a revocation, the clock)."""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    credits = threading.BoundedSemaphore(QUEUE_MAX)
    lease.stream = stream
    t = threading.Thread(target=_read_loop, args=(lease, stream, loop, q, credits), name="node-stream", daemon=True)
    lease.thread = t
    parser = Parser(requested, nlines)
    try:
        t.start()
        yield f"retry: {RETRY_AFTER * 1000}\n\n".encode()
        last_ok = last_beat = mono()
        while True:
            if lease.stop.is_set():
                break
            try:
                kind, val = await asyncio.wait_for(q.get(), POLL_S)
            except asyncio.TimeoutError:
                kind, val = None, None
            now = mono()
            if kind == "line":
                credits.release()
                try:
                    out = parser.feed(val)
                except TooLarge:
                    lease.close("too_large")
                    break
                for ev, data in out:
                    yield sse(ev, data)
                    last_ok = last_beat = now
            elif kind == "end":
                lease.close(val)
                break
            lease.dropped = parser.dropped
            if now - last_ok > IDLE_S:
                lease.close("idle")
                break
            if now - last_beat >= BEAT_S:
                yield b": ping\n\n"
                last_beat = now
            lease.touch()
            if await request.is_disconnected():
                lease.close("disconnected")
                return
        yield gone(lease.reason or "closed", name, parser.dropped)
    finally:
        lease.close(lease.reason or "closed")
        LEASES.release(lease)


# ================================================================ the hub route

class Gone(Exception):
    """A node condition that ends the request as one `gone` event (status 200): the stream never opened."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason, self.message = reason, message


def _query(request: Request) -> tuple[frozenset, int]:
    from . import main                       # the same parser as /api/stream: names are ccboard session names, at most 20, lines 1 to 40
    items = list(request.query_params.multi_items())
    keys = [k for k, _ in items]
    if len(set(keys)) != len(keys):
        raise nr.RelayError(400, "invalid", "a query field is given twice", kind="refused")
    if not set(keys) <= {"names", "lines"}:
        raise nr.RelayError(400, "invalid", "only names and lines are taken", kind="refused")
    q = dict(items)
    if "names" not in q:
        raise nr.RelayError(400, "invalid", "names is required: at most 20 session names", kind="refused")
    bad = nr.stream_names_error(q["names"])
    if bad:
        raise nr.RelayError(400, "invalid", bad, kind="refused")
    try:
        wanted, n = main._stream_query(q["names"], q.get("lines"))
    except projects.BadRequest as e:
        raise nr.RelayError(400, "invalid", nr._text(str(e), 120), kind="refused") from None
    return frozenset(wanted or ()), n


_GONE_FROM_STATUS = {"needs_repair": "repair", "unpaired": "unpaired", "not_read_yet": "not_read_yet", "offline": "offline"}


def _open(request: Request, handle: str, db, hub):
    """Everything up to and including the upstream request (blocking: runs in a thread). Returns (lease, reply, reg, names, nlines, user) or raises
    nr.RelayError (a JSON answer) or Gone (a single `gone` event). An `out` audit row is written for every outcome once the node is known."""
    reg, user, target = None, None, "stream"
    try:
        user = nr._caller(request, STREAM_ROW)
        ok, wait = nodes.relay_read_limiter.allow(user)
        if not ok:
            raise nr.RelayError(429, "rate_limited", f"too many relayed requests from you; wait {wait} s", headers={"Retry-After": str(wait)}, kind="refused")
        reg = nr._registry_row(handle, db)
        nr._self_check(reg)
        name = reg.get("name") or handle
        if reg.get("legacy"):
            raise nr.RelayError(409, "legacy", f"{name} was added with CCBOARD_NODES and holds no token: pair it to act on it", kind="refused")
        if "sessions" not in (nodes._scopes_of(reg.get("scopes")) or []):
            raise nr.RelayError(409, "scope", f"needs the sessions scope on {name}", kind="refused")
        names, nlines = _query(request)
        target = "stream " + ",".join(sorted(names))
        try:
            nr._status_check(reg, nr._hub_record(hub, handle), name)
        except nr.RelayError as e:
            if e.reason in _GONE_FROM_STATUS:
                raise Gone(_GONE_FROM_STATUS[e.reason], e.message.replace("; nothing was sent", "")) from None
            raise
        lease = LEASES.acquire(reg["peer_id"], handle, user or "")
        try:
            path = "/api/node/stream?" + urlencode({"names": ",".join(sorted(names)), "lines": nlines})
            try:
                reply = nodes.PeerClient(reg, db=db).open_stream(path, acting_user=user, timeout=OPEN_TIMEOUT)
            except nodes.PeerError as e:
                if e.reason == "no_token":
                    raise Gone("repair", f"{name} holds no token on this board: re-pair") from None
                if e.reason in ("url", "bad_path"):
                    raise nr.RelayError(502, "bad_address", f"the address of {name} is not one this board may call; nothing was sent", kind="refused") from None
                if isinstance(e.cause, (TimeoutError,)):
                    raise Gone("upstream_error", f"{name} did not answer in {int(OPEN_TIMEOUT)} s") from None
                raise Gone("upstream_error", f"{name} could not be reached") from None
            if not reply.ok:
                if reply.status == 401:
                    raise Gone("repair", f"{name} no longer takes this board's token: re-pair")
                if reply.status == 403:
                    raise nr.RelayError(409, "scope", f"needs the sessions scope on {name}", kind="failed")
                if reply.status == 429:
                    raise nr.RelayError(429, "rate_limited", f"{name} is receiving too many requests from this board; wait {RETRY_AFTER} s",
                                        headers={"Retry-After": str(RETRY_AFTER)}, kind="failed")
                raise Gone("upstream_error", f"{name} answered with an error ({reply.status if reply.status >= 500 else 'unexpected'})")
            lease.stream = reply.stream                 # from here close_node, close_all and the lease sweep can end the upstream, even before the pump starts
        except BaseException:
            lease.close("closed")
            LEASES.release(lease)
            raise
    except nr.RelayError as e:
        if reg is not None:
            nodes.audit("out", reg.get("peer_id") or "", STREAM_ROW.audit_action, False, f"{e.reason}: {e.message}"[:200], node_name=reg.get("name"), user=user,
                        target=target, status=e.kind, db=db)
        raise
    except Gone as g:
        if reg is not None:
            nodes.audit("out", reg.get("peer_id") or "", STREAM_ROW.audit_action, False, f"{g.reason}: {g.message}"[:200], node_name=reg.get("name"), user=user,
                        target=target, status="refused" if g.reason in ("offline", "repair", "unpaired", "not_read_yet") else "failed", db=db)
        raise
    nodes.audit("out", reg["peer_id"], STREAM_ROW.audit_action, True, None, node_name=reg.get("name"), user=user, target=target, status="ok", db=db)
    return lease, reply.stream, reg, names, nlines


def _gone_response(g: Gone, name: str) -> StreamingResponse:
    body = f"retry: {RETRY_AFTER * 1000}\n\n".encode() + gone(g.reason, name, message=g.message)

    async def once():
        yield body
    return StreamingResponse(once(), media_type="text/event-stream", headers=HEADERS)


def hub_endpoint(get_db, get_hub):
    async def endpoint(request: Request):
        handle = request.path_params.get("handle", "")
        label = handle if nodes.valid_handle(handle) else None
        try:
            lease, stream, reg, names, nlines = await asyncio.to_thread(_open, request, handle, get_db(), get_hub())
        except nr.RelayError as e:
            return e.response(label)
        except Gone as g:
            return _gone_response(g, handle)
        return StreamingResponse(pump(request, lease, stream, reg.get("name") or handle, names, nlines), media_type="text/event-stream", headers=HEADERS)
    endpoint.__name__ = "relay_stream"
    return endpoint
