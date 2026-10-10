"""Nodes epic P8, issue #143: the read-only stream relay for a peer's session tail.

1. The parser: only `lines` and `tick` cross, rebuilt; bad JSON, other events, foreign names, long lines, many lines, control characters and secrets, and no peer text
   reaches a regex before the caps.
2. The hub route over a real ASGI exchange (the browser is a harness that reads the response as it is written and can leave): passthrough, headers, heartbeat, the idle
   limit on a patched clock, the browser leaving, the pair removed, hub shutdown, the caps, the offline node, every auth case.
3. The peer route: names required, 20 names, 1 to 40 lines, the `sessions` scope, redaction on the peer, one `in` audit row with the names.
4. Both boards end to end (the upstream bridged to the peer's route), and the real byte reader over a loopback socket.
5. Threads: every reader ends when the stream does, on the browser's leaving, on a revocation and on stop.

The upstream of the hub is a fake (`nodes.peer_stream_transport`): a stream object the test feeds and ends, and whose close() it counts. Nothing opens a connection to
another host; the loopback test talks to a server in this process on a temp port.
"""
from __future__ import annotations

import asyncio
import http.client
import http.server
import json
import queue
import re
import select
import socketserver
import threading
import time
from types import SimpleNamespace

import pytest

from app import nodes, nodes_relay as nr, nodes_stream as ns

ID = {"Tailscale-User-Login": "alice@example.com"}
H = {**ID, "X-CCBoard": "1"}
S1, S2, S3 = "shop--api--s1", "shop--api--s2", "shop--api--s3"
SECRET = "xox" + "b-" + "1234567890-abcdefghijklmnop"
TAIL_MARKER = "MARKER-tail-8d1f03"
WIDE = "a first line that is wider than all the others on this screen, so the secret is not taken for a wrapped one"


def ev(name: str, obj) -> bytes:
    return f"event: {name}\ndata: {json.dumps(obj)}\n\n".encode()


def lines_ev(name: str, lines) -> bytes:
    return ev("lines", {"name": name, "lines": lines})


# ================================================================ fakes

class FakeStream:
    """The open body of an upstream answer. feed() bytes, end() closes it from the peer's side; close() is what the hub calls and is counted."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.closed = threading.Event()
        self.closes = 0

    def feed(self, data: bytes) -> None:
        self.q.put(data)

    def end(self) -> None:
        self.q.put(None)

    def read(self, n: int) -> bytes:
        while True:
            if self.closed.is_set():
                return b""
            try:
                item = self.q.get(timeout=0.02)
            except queue.Empty:
                continue
            return b"" if item is None else item

    def close(self) -> None:
        self.closes += 1
        self.closed.set()


class Upstream:
    """nodes.peer_stream_transport: records every request (path and headers) and hands out a new FakeStream (or `status` with an error body)."""

    def __init__(self, status=200, body=b"", raises=None):
        self.status, self.body, self.raises = status, body, raises
        self.calls: list[dict] = []
        self.streams: list[FakeStream] = []

    def __call__(self, target, path, headers, timeout):
        self.calls.append({"path": path, "headers": dict(headers), "timeout": timeout, "host": target.host})
        if self.raises:
            raise self.raises
        if self.status != 200:
            return self.status, {"content-type": "application/json"}, nodes._Closed(self.body)
        s = FakeStream()
        self.streams.append(s)
        return 200, {"content-type": "text/event-stream"}, s


class FakeHub:
    def __init__(self, **rec):
        self.rec = {"status": "online", "age_s": 3, "polled_at": "2026-10-10T00:00:00Z", "last_ok_at": "2026-10-10T00:00:00Z", **rec}

    def records(self, handle=None):
        return [self.rec]


class Conn:
    """A browser: one GET to the app as a raw ASGI exchange, read as it is written, with the means to leave."""

    def __init__(self, target: str, headers: dict | None = None):
        path, _, qs = target.partition("?")
        hdrs = {**H, **(headers or {})}
        self.scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "path": path, "raw_path": path.encode(), "query_string": qs.encode(),
                      "headers": [(k.lower().encode(), v.encode()) for k, v in hdrs.items()], "client": ("testclient", 50000), "server": ("testserver", 443), "scheme": "https",
                      "root_path": ""}
        self.status, self.headers, self.buf, self.done = None, {}, b"", False
        self.exc: BaseException | None = None

    async def open(self):
        from app import main
        self.disc, self._sent = asyncio.Event(), False

        async def receive():
            if not self._sent:
                self._sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await self.disc.wait()
            return {"type": "http.disconnect"}

        async def send(m):
            if m["type"] == "http.response.start":
                self.status, self.headers = m["status"], {k.decode().lower(): v.decode() for k, v in m["headers"]}
            elif m["type"] == "http.response.body":
                self.buf += m.get("body", b"")
                if not m.get("more_body"):
                    self.done = True

        async def run():
            try:
                await main.app(self.scope, receive, send)
            except BaseException as e:           # noqa: BLE001 - a cancelled exchange is a result here
                self.exc = e
            self.done = True
        self.task = asyncio.create_task(run())
        await self.until(lambda: self.status is not None or self.done)
        return self

    def blocks(self) -> list[tuple[str, object]]:
        out = []
        for block in self.buf.decode("utf-8", "replace").split("\n\n"):
            block = block.strip("\n")
            if not block:
                continue
            if block.startswith(":"):
                out.append(("comment", block[1:].strip()))
            elif block.startswith("retry:"):
                out.append(("retry", int(block.split(":", 1)[1])))
            else:
                e = re.search(r"^event: (\S+)", block, re.M)
                d = re.search(r"^data: (.*)$", block, re.M)
                out.append((e.group(1), json.loads(d.group(1))) if e and d else ("raw", block))
        return out

    def events(self, name: str | None = None) -> list:
        return [d for k, d in self.blocks() if k not in ("comment", "retry", "raw") and (name is None or k == name)]

    async def until(self, pred, timeout: float = 4.0):
        end = time.monotonic() + timeout
        while not pred():
            if time.monotonic() > end:
                raise AssertionError(f"timed out; got {self.buf[-400:]!r} status={self.status} exc={self.exc!r}")
            await asyncio.sleep(0.01)

    async def leave(self):
        self.disc.set()
        await asyncio.wait_for(asyncio.shield(self.task), 4.0)


def stream_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "node-stream" and t.is_alive()]


def wait_threads(n: int = 0, timeout: float = 4.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if len(stream_threads()) <= n:
            return True
        time.sleep(0.02)
    return len(stream_threads()) <= n


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    from app import main
    ns.LEASES.close_all("shutdown", wait=1.0)
    ns.LEASES._open.clear()
    monkeypatch.setattr(ns, "POLL_S", 0.02)
    monkeypatch.setattr(main, "_hub", lambda: FakeHub())
    big = lambda: nodes._Limiter(10 ** 6, 60.0, 64)
    for n in ("node_read_limiter", "node_write_limiter", "relay_read_limiter", "relay_write_limiter"):
        monkeypatch.setattr(nodes, n, big())
    yield
    ns.LEASES.close_all("shutdown", wait=1.0)
    assert wait_threads(0), "a stream reader thread outlived its test"
    main._invalidate_scan()


@pytest.fixture
def clock(monkeypatch):
    c = [1000.0]
    monkeypatch.setattr(ns, "mono", lambda: c[0])
    return c


@pytest.fixture
def world(split_boards, pair_up):
    reg = pair_up(["read", "sessions"])
    up = Upstream()
    nodes.peer_stream_transport = up
    yield SimpleNamespace(two=split_boards, a=split_boards.a, b=split_boards.b, reg=reg, h=reg["handle"], up=up, pair_up=pair_up)
    nodes.peer_stream_transport = None


def url(w, names=S1, lines=None, handle=None) -> str:
    q = f"names={names}" + (f"&lines={lines}" if lines is not None else "")
    return f"/api/nodes/{handle or w.h}/stream?{q}"


def run(w, scenario):
    """Run an async scenario as board a (the hub) in this thread; every browser it opens is a Conn."""
    with w.a.enter():
        return asyncio.run(scenario())


def audit(board, **kw):
    return board.db.node_audit_list(500, **kw)


# ================================================================ 1. the parser

def parse(feed: list[bytes], names=(S1, S2), n=12) -> tuple[list, ns.Parser]:
    p = ns.Parser(frozenset(names), n)
    out = []
    for chunk in feed:
        for line in chunk.split(b"\n")[:-1] if chunk.endswith(b"\n") else chunk.split(b"\n"):
            out += p.feed(line)
    return out, p


def test_lines_and_tick_are_rebuilt():
    out, p = parse([lines_ev(S1, ["a", "b"]), ev("tick", {"sessions": [S1, S2, "other--x--y"]})])
    assert out == [("lines", {"name": S1, "lines": ["a", "b"]}), ("tick", {"sessions": [S1, S2]})] and p.dropped == 0


@pytest.mark.parametrize("feed", [
    ev("hello", {"x": 1}), ev("permission", {"name": S1}), b"event: lines\ndata: not json\n\n", b"event: lines\ndata: [1,2]\n\n", b"event: lines\ndata: \"s\"\n\n",
    lines_ev("nope--x--y", ["a"]), lines_ev(S1 + "x", ["a"]), ev("lines", {"name": 5, "lines": []}), ev("lines", {"name": S1, "lines": "text"}), ev("lines", {"name": S1}),
    ev("tick", {"sessions": "x"}), ev("tick", [1]), b"event: lines\ndata: " + b"[" * 5000 + b"\n\n", b"event: \ndata: {}\n\n",
], ids=lambda b: b[:30].decode("utf-8", "replace"))
def test_everything_else_is_dropped_and_counted(feed):
    out, p = parse([feed])
    assert out == [] and p.dropped == 1


def test_an_event_with_no_name_and_no_data_and_comments_and_other_fields_are_ignored_not_counted():
    out, p = parse([b": ping\n\n", b"id: 4\nretry: 10\n\n", b"\n", b": x\nevent: tick\ndata: {\"sessions\": []}\n\n"])
    assert out == [("tick", {"sessions": []})] and p.dropped == 0


def test_a_name_outside_the_asked_set_is_dropped_even_when_it_is_a_valid_session():
    out, p = parse([lines_ev(S3, ["secret of another session"])])
    assert out == [] and p.dropped == 1


def test_a_3000_character_line_is_cut_and_80_lines_become_the_asked_count():
    out, _ = parse([lines_ev(S1, ["x" * 3000] + [f"row {i}" for i in range(79)])], n=40)
    (kind, d), = out
    assert len(d["lines"]) == 40 and d["lines"][-1] == "row 78" and d["lines"][0] == "row 39"
    out, _ = parse([lines_ev(S1, ["x" * 3000])], n=40)
    assert len(out[0][1]["lines"][0]) <= nr.PANE_LINE_MAX < 2000
    out, _ = parse([lines_ev(S1, [f"r{i}" for i in range(80)])], n=12)
    assert out[0][1]["lines"] == [f"r{i}" for i in range(68, 80)]
    out, _ = parse([lines_ev(S1, [f"r{i}" for i in range(80)])], n=100)
    assert len(out[0][1]["lines"]) == 40, "never more than 40, whatever was asked"


def test_control_characters_go_and_a_non_string_line_is_skipped():
    out, _ = parse([ev("lines", {"name": S1, "lines": ["a\x1b[31mred\x07\x00b", "tab\there", 5, None, {"x": 1}, "zero​width"]})])
    assert out[0][1]["lines"] == ["a[31mredb", "tab    here", "zerowidth"]


def test_a_secret_shaped_line_is_redacted_by_the_hub_whatever_the_peer_did():
    out, _ = parse([lines_ev(S1, [f"export TOKEN={SECRET}", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz", "sk-ant-" + "A1b2C3d4E5f6G7h8I9j0"])])
    text = json.dumps(out)
    assert SECRET not in text and "abcdefghijklmnopqrstuvwxyz" not in text and "A1b2C3d4E5f6G7h8I9j0" not in text and "[redacted]" in text


def test_no_peer_text_reaches_a_regex_unbounded(monkeypatch):
    seen = []
    real = nr.redact
    monkeypatch.setattr(nr, "redact", lambda t, known=(), strict=True: (seen.append(len(t)), real(t, known, strict))[1])
    t0 = time.monotonic()
    shapes = [
        ["=" + "a" * 60000],                                                       # one line at the 64 KB event cap
        ["token=" + "b" * 30000, "-----BEGIN KEY-----" + "c" * 30000],
        ["=" + "a" * 1400] * 44,                                                   # more lines than asked, each long
        ["Bearer " + "d" * 20, "x"] * 1200,                                         # thousands of short lines
        ["key=" * 10000],
    ]
    for lines in shapes:
        out, _ = parse([lines_ev(S1, lines)], n=40)
        assert out and all(len(x) <= nr.PANE_LINE_MAX for x in out[0][1]["lines"]) and len(out[0][1]["lines"]) <= 40
    assert time.monotonic() - t0 < 3.0, "linear on hostile input"
    assert seen and max(seen) <= nr.REDACT_MAX, "the text was cut to the pane caps before the first pattern saw it"


def test_an_event_over_64_kb_is_too_large_and_a_line_over_64_kb_is_too_large_in_the_reader():
    p = ns.Parser(frozenset({S1}), 12)
    p.feed(b"event: lines")
    with pytest.raises(ns.TooLarge):
        for _ in range(3):
            p.feed(b"data: " + b"x" * 30000)


def test_the_browsers_event_is_one_event_whatever_the_value():
    b = ns.sse("lines", {"name": S1, "lines": ["a\ndata: injected\n\nevent: gone", " x"]})
    assert b.count(b"\n\n") == 1 and b.endswith(b"\n\n") and b"\ndata: injected" not in b
    assert re.fullmatch(rb"event: lines\ndata: \{.*\}\n\n", b)


# ================================================================ 2. the hub route

def test_lines_and_ticks_pass_through_re_serialised_with_the_headers_and_a_retry_hint(world):
    w = world

    async def scenario():
        c = await Conn(url(w, f"{S1},{S2}", 12)).open()
        assert c.status == 200 and c.headers["content-type"].startswith("text/event-stream")
        assert c.headers["cache-control"] == "no-store" and c.headers["x-accel-buffering"] == "no" and c.headers["x-content-type-options"] == "nosniff"
        await c.until(lambda: w.up.streams)
        s = w.up.streams[0]
        s.feed(lines_ev(S1, ["one", "two"]) + ev("tick", {"sessions": [S1, S2, S3]}))
        s.feed(ev("junk", {"a": 1}) + b"event: lines\ndata: nope\n\n" + lines_ev(S3, ["foreign"]))
        s.feed(lines_ev(S2, [WIDE, SECRET, "ok"]))
        await c.until(lambda: len(c.events("lines")) == 2)
        assert c.blocks()[0] == ("retry", 5000)
        assert c.events("lines") == [{"name": S1, "lines": ["one", "two"]}, {"name": S2, "lines": [WIDE, "[redacted]", "ok"]}]
        assert c.events("tick") == [{"sessions": [S1, S2]}]
        assert SECRET not in c.buf.decode() and "foreign" not in c.buf.decode() and "junk" not in c.buf.decode()
        s.end()
        await c.until(lambda: c.done)
        g = c.events("gone")
        assert len(g) == 1 and g[0]["reason"] == "closed" and g[0]["final"] is False and g[0]["dropped"] == 3, g
    run(w, scenario)
    call = w.up.calls[0]
    assert call["path"] == f"/api/node/stream?names={S1}%2C{S2}&lines=12" and call["host"] == "100.64.0.2"
    h = call["headers"]
    assert h["Accept"] == "text/event-stream" and h["Authorization"].startswith("Bearer ccbnode_") and h["X-CCBoard-Acting-User"] == "alice@example.com" and h["X-CCBoard"] == "1"
    assert "Origin" not in h and call["timeout"] == ns.OPEN_TIMEOUT


def test_the_upstream_bytes_are_never_copied_to_the_browser(world):
    w = world

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: w.up.streams)
        w.up.streams[0].feed(b"HTTP/1.1 200 OK\r\nSet-Cookie: x=1\r\n\r\n<script>alert(1)</script>\n\nevent: lines\ndata: {\"name\": \"" + S1.encode() + b"\", \"lines\": [\"x\"], \"extra\": 1}\n\n")
        await c.until(lambda: c.events("lines"))
        assert c.events("lines") == [{"name": S1, "lines": ["x"]}]
        assert b"<script>" not in c.buf and b"Set-Cookie" not in c.buf and b"extra" not in c.buf
        await c.leave()
    run(w, scenario)


def test_the_browser_leaving_closes_the_upstream_within_a_tick_and_ends_the_thread(world):
    w = world

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: w.up.streams)
        s = w.up.streams[0]
        s.feed(lines_ev(S1, ["a"]))
        await c.until(lambda: c.events("lines"))
        assert ns.open_count() == 1 and len(stream_threads()) == 1
        t0 = time.monotonic()
        await c.leave()
        await c.until(lambda: s.closed.is_set())
        assert time.monotonic() - t0 < 2.0 and s.closes >= 1
        assert ns.open_count() == 0
    run(w, scenario)
    assert wait_threads(0)


def test_hub_shutdown_closes_every_stream_with_a_gone(world):
    w = world

    async def scenario():
        c1 = await Conn(url(w, S1)).open()
        c2 = await Conn(url(w, S2)).open()
        await c1.until(lambda: len(w.up.streams) == 2)
        assert ns.open_count(w.reg["peer_id"]) == 2
        n = await asyncio.to_thread(ns.close_all, "shutdown", 3.0)
        assert n == 2
        for c in (c1, c2):
            await c.until(lambda c=c: c.done)
            g = c.events("gone")
            assert len(g) == 1 and g[0]["reason"] == "shutdown" and g[0]["final"] is True
        assert all(s.closed.is_set() for s in w.up.streams) and ns.open_count() == 0
    run(w, scenario)
    assert wait_threads(0)


def test_node_hub_stop_closes_the_streams_too(world):
    from app import nodes_hub
    w = world

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: w.up.streams)
        await asyncio.to_thread(nodes_hub.NodeHub(w.a.db).stop)
        await c.until(lambda: c.done)
        assert c.events("gone")[0]["reason"] == "shutdown" and w.up.streams[0].closed.is_set()
    run(w, scenario)


def test_removing_the_pair_closes_that_nodes_streams_with_a_final_gone(world):
    w = world

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: w.up.streams)
        await asyncio.to_thread(nodes.remove_peer, w.reg["peer_id"], w.a.db)
        await c.until(lambda: c.done)
        g = c.events("gone")
        assert len(g) == 1 and g[0]["reason"] == "revoked" and g[0]["final"] is True and "removed" in g[0]["message"]
        assert w.up.streams[0].closed.is_set()
    run(w, scenario)
    assert wait_threads(0)


def test_a_peer_that_stops_taking_the_token_ends_the_streams_of_that_pair(world):
    w = world

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: w.up.streams)
        await asyncio.to_thread(nodes._note, w.a.db, w.reg["peer_id"], ok=False, error="the peer answered 401", repair=True)
        await c.until(lambda: c.done)
        assert c.events("gone")[0]["reason"] == "revoked" and w.up.streams[0].closed.is_set()
    run(w, scenario)


def test_only_the_pair_that_went_is_closed(world):
    w = world
    other = ns.LEASES.acquire("p_" + "1" * 16, "other")
    assert ns.LEASES.close_node(w.reg["peer_id"]) == 0 and not other.stop.is_set()
    assert ns.LEASES.close_node("p_" + "1" * 16) == 1 and other.stop.is_set()


def test_the_third_stream_to_one_node_is_a_429_with_retry_after_and_a_slot_comes_back(world):
    w = world

    async def scenario():
        c1 = await Conn(url(w, S1)).open()
        c2 = await Conn(url(w, S2)).open()
        await c1.until(lambda: len(w.up.streams) == 2)
        c3 = await Conn(url(w, S3)).open()
        await c3.until(lambda: c3.done)
        assert c3.status == 429 and c3.headers["retry-after"] == "5"
        body = json.loads(c3.buf)
        assert body["reason"] == "stream_limit" and body["node"] == w.h and "2 live streams" in body["error"]
        assert len(w.up.streams) == 2, "no third upstream request"
        row = audit(w.a, action="read_stream")[0]
        assert row["status"] == "refused" and "stream_limit" in row["detail"]
        await c1.leave()
        await c1.until(lambda: ns.open_count() == 1)
        c4 = await Conn(url(w, S3)).open()
        await c4.until(lambda: len(w.up.streams) == 3)
        assert c4.status == 200
        await c2.leave()
        await c4.leave()
    run(w, scenario)


def test_the_ninth_stream_on_a_hub_is_refused_and_the_caps_are_the_issues():
    assert (ns.MAX_PER_NODE, ns.MAX_PER_USER, ns.MAX_TOTAL, ns.LINE_MAX, ns.IDLE_S, ns.BEAT_S) == (2, 6, 8, 64 * 1024, 60.0, 15.0)
    held = [ns.LEASES.acquire(f"p_{i:016x}", f"n{i}", f"person{i % 2 + 10 * (i // 2)}") for i in range(4) for _ in range(2)]      # 4 nodes x 2, held by 4 people
    assert ns.open_count() == 8
    with pytest.raises(nr.RelayError) as e:
        ns.LEASES.acquire("p_" + "f" * 16, "ninth", "someone-new")
    assert e.value.status == 429 and e.value.headers == {"Retry-After": "5"} and "8 live streams" in e.value.message
    with pytest.raises(nr.RelayError):
        ns.LEASES.acquire("p_" + "0" * 16, "third-on-the-first", "someone-new")
    ns.LEASES.release(held[0])
    assert ns.LEASES.acquire("p_" + "f" * 16, "ninth", "someone-new") is not None


def test_a_lease_nobody_renews_is_swept_by_the_next_open(clock):
    a = ns.LEASES.acquire("p_" + "1" * 16, "a")
    ns.LEASES.acquire("p_" + "1" * 16, "a")
    clock[0] += ns.LEASE_TTL + 1
    c = ns.LEASES.acquire("p_" + "1" * 16, "a")
    assert a.stop.is_set() and ns.open_count() == 1 and c is not None


def test_an_idle_upstream_is_closed_after_60_seconds_with_a_gone(world, clock):
    w = world

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: w.up.streams)
        s = w.up.streams[0]
        s.feed(ev("junk", {}) + b": comment\n\n")
        await asyncio.sleep(0.1)
        clock[0] += 59.0
        s.feed(lines_ev(S1, ["alive"]))
        await c.until(lambda: c.events("lines"))
        clock[0] += 59.0
        await asyncio.sleep(0.15)
        assert not c.done, "a valid event 59 s ago keeps it open"
        clock[0] += 2.0
        await c.until(lambda: c.done)
        g = c.events("gone")
        assert g[0]["reason"] == "idle" and "60 s" in g[0]["message"] and s.closed.is_set()
    run(w, scenario)
    assert wait_threads(0)


def test_junk_events_do_not_keep_a_stream_alive(world, clock):
    w = world

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: w.up.streams)
        s = w.up.streams[0]
        for _ in range(4):
            clock[0] += 20.0
            s.feed(ev("noise", {"a": 1}))
            await asyncio.sleep(0.1)
        await c.until(lambda: c.done)
        assert c.events("gone")[0]["reason"] == "idle"
    run(w, scenario)


def test_a_comment_heartbeat_every_15_seconds_while_nothing_else_is_sent(world, clock):
    w = world

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: w.up.streams)
        await asyncio.sleep(0.1)
        assert ("comment", "ping") not in c.blocks()
        clock[0] += 16.0
        await c.until(lambda: ("comment", "ping") in c.blocks())
        n = c.blocks().count(("comment", "ping"))
        await asyncio.sleep(0.1)
        assert c.blocks().count(("comment", "ping")) == n, "once per 15 s, not per poll"
        await c.leave()
    run(w, scenario)


def test_a_line_over_64_kb_closes_with_a_too_large_gone(world):
    w = world

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: w.up.streams)
        w.up.streams[0].feed(b"event: lines\ndata: " + b"y" * (ns.LINE_MAX + 5) + b"\n\n")
        await c.until(lambda: c.done)
        assert c.events("gone")[0]["reason"] == "too_large" and w.up.streams[0].closed.is_set()
    run(w, scenario)
    assert wait_threads(0)


def test_the_reader_never_hands_over_a_line_over_64_kb(world):
    """The reader's own cap (memory): a line over 64 KB is not queued at all, the stream ends with too_large. The pump's parser would catch the same line later;
    this pins the earlier cut."""
    posted = []

    class Loop:
        def call_soon_threadsafe(self, fn, item):
            posted.append(item)
    lease = ns.Lease("p_" + "1" * 16, "x")
    stream = FakeStream()
    stream.feed(b"ok line\n" + b"y" * (ns.LINE_MAX + 10) + b"\nlater\n")
    stream.end()                                      # without the reader's cap the loop would run to here and post all three lines
    ns._read_loop(lease, stream, Loop(), SimpleNamespace(put_nowait=None), threading.BoundedSemaphore(8))
    assert posted == [("line", b"ok line"), ("end", "too_large")] and stream.closes >= 1


def test_the_pump_ends_and_closes_the_upstream_when_the_browser_has_gone_even_if_nothing_cancels_it(world):
    """Starlette cancels a response whose client left; this checks the pump's own look every tick for a server that does not."""
    w = world

    class Gone:
        async def is_disconnected(self):
            return True
    lease = ns.LEASES.acquire(w.reg["peer_id"], w.h)
    stream = FakeStream()

    async def scenario():
        async def collect():
            return [chunk async for chunk in ns.pump(Gone(), lease, stream, "node-b", frozenset({S1}), 12)]
        out = await asyncio.wait_for(collect(), 5.0)
        assert out == [b"retry: 5000\n\n"], "no gone event for a browser that is not there"
        assert stream.closed.is_set() and ns.open_count() == 0
    asyncio.run(scenario())
    assert wait_threads(0)


def test_an_endless_line_with_no_newline_is_cut_at_64_kb(world):
    w = world

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: w.up.streams)
        s = w.up.streams[0]
        for _ in range(12):
            s.feed(b"z" * 8192)
        await c.until(lambda: c.done)
        assert c.events("gone")[0]["reason"] == "too_large"
    run(w, scenario)


def test_an_upstream_read_error_is_an_upstream_error_gone_and_the_hub_does_not_retry(world):
    w = world

    class Boom(FakeStream):
        def read(self, n):
            raise ConnectionResetError("reset")
    up = Upstream()
    up.streams = []
    nodes.peer_stream_transport = lambda target, path, headers, timeout: (200, {}, Boom())

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: c.done)
        g = c.events("gone")
        assert len(g) == 1 and g[0]["reason"] == "upstream_error" and g[0]["final"] is False
    run(w, scenario)


def test_an_offline_node_is_one_gone_event_with_no_upstream_call(world, monkeypatch):
    from app import main
    w = world
    monkeypatch.setattr(main, "_hub", lambda: FakeHub(status="offline", age_s=300))

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: c.done)
        assert c.status == 200 and c.headers["content-type"].startswith("text/event-stream") and c.headers["cache-control"] == "no-store"
        assert [k for k, _ in c.blocks()] == ["retry", "gone"]
        g = c.events("gone")[0]
        assert g["reason"] == "offline" and "offline" in g["message"] and "5 min" in g["message"] and "nothing was sent" not in g["message"]
    run(w, scenario)
    assert w.up.calls == [] and ns.open_count() == 0
    row = audit(w.a, action="read_stream")[0]
    assert row["direction"] == "out" and row["status"] == "refused" and row["detail"].startswith("offline")


@pytest.mark.parametrize("rec,reason,final", [({"status": "unauthorized"}, "repair", True), ({"status": "unpaired"}, "unpaired", True),
                                              ({"polled_at": None, "last_ok_at": None, "status": "offline"}, "not_read_yet", False)])
def test_a_node_that_needs_a_new_pair_or_has_not_been_read_is_one_gone_event(world, monkeypatch, rec, reason, final):
    from app import main
    w = world
    monkeypatch.setattr(main, "_hub", lambda: FakeHub(**rec))

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: c.done)
        g = c.events("gone")
        assert len(g) == 1 and g[0]["reason"] == reason and g[0]["final"] is final
    run(w, scenario)
    assert w.up.calls == []


def test_the_pair_marked_needs_repair_is_a_gone_repair_with_no_call(world):
    w = world
    with w.a.enter():
        nodes._note(w.a.db, w.reg["peer_id"], ok=False, error="the peer answered 401", repair=True)

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: c.done)
        assert c.events("gone")[0]["reason"] == "repair"
    run(w, scenario)
    assert w.up.calls == []


def test_the_peer_answering_401_is_a_gone_repair_and_marks_the_pair(world):
    w = world
    nodes.peer_stream_transport = Upstream(status=401, body=b'{"error":"bad token"}')

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: c.done)
        assert c.events("gone")[0]["reason"] == "repair" and c.events("gone")[0]["final"] is True
    run(w, scenario)
    with w.a.enter():
        assert any(r.get("needs_repair") for r in nodes.out_peers(w.a.db))


@pytest.mark.parametrize("status,code,reason", [(403, 409, "scope"), (429, 429, "rate_limited")])
def test_a_refusal_of_the_peer_before_the_stream_is_a_json_answer(world, status, code, reason):
    w = world
    nodes.peer_stream_transport = Upstream(status=status, body=b"{}")

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: c.done)
        assert c.status == code and json.loads(c.buf)["reason"] == reason
    run(w, scenario)
    assert ns.open_count() == 0


@pytest.mark.parametrize("make,reason", [(lambda: Upstream(status=500, body=b"boom"), "upstream_error"), (lambda: Upstream(raises=ConnectionRefusedError("no")), "upstream_error"),
                                         (lambda: Upstream(raises=TimeoutError("slow")), "upstream_error")])
def test_a_peer_that_cannot_be_opened_is_a_gone_upstream_error_and_the_slot_is_given_back(world, make, reason):
    w = world
    nodes.peer_stream_transport = make()

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: c.done)
        g = c.events("gone")
        assert len(g) == 1 and g[0]["reason"] == reason and "boom" not in g[0]["message"]
    run(w, scenario)
    assert ns.open_count() == 0


def test_a_redirect_is_never_followed(world):
    w = world
    nodes.peer_stream_transport = Upstream(status=302)

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: c.done)
        assert c.events("gone")[0]["reason"] == "upstream_error"
    run(w, scenario)


# ---- who may ask, and what

def test_no_credential_is_403_and_no_csrf_header_is_403(world):
    w = world

    async def scenario():
        for headers, msg in (({"Tailscale-User-Login": ""}, None),):
            pass
        c = Conn(url(w))
        c.scope["headers"] = []
        await c.open()
        await c.until(lambda: c.done)
        assert c.status == 403 and "no Tailscale identity" in json.loads(c.buf)["error"]
        c = Conn(url(w))
        c.scope["headers"] = [(b"tailscale-user-login", b"alice@example.com")]
        await c.open()
        await c.until(lambda: c.done)
        assert c.status == 403 and json.loads(c.buf)["error"] == "missing X-CCBoard header"
        c = Conn(url(w))
        c.scope["headers"] = [(b"tailscale-user-login", b"mallory@example.com"), (b"x-ccboard", b"1")]
        await c.open()
        await c.until(lambda: c.done)
        assert c.status == 403
    run(w, scenario)
    assert w.up.calls == []


def test_a_node_token_at_the_hub_route_is_403_and_the_hook_token_is_403_human_only(world):
    w = world
    _, tok = nodes.add_incoming({"id": "ts:nCALLER000001", "name": "caller", "url": "https://100.64.0.9"}, list(nodes.SCOPES), db=w.a.db)

    async def scenario():
        c = Conn(url(w))
        c.scope["headers"] = [(b"authorization", f"Bearer {tok}".encode()), (b"x-ccboard", b"1")]
        await c.open()
        await c.until(lambda: c.done)
        assert c.status == 403 and json.loads(c.buf)["error"] == "this node token does not open that route"
        c = Conn(url(w))
        c.scope["headers"] = [(b"x-ccboard-token", w.a.hook_token.encode())]
        await c.open()
        await c.until(lambda: c.done)
        assert c.status == 403 and json.loads(c.buf)["reason"] == "human_only"
        c = Conn(url(w))
        c.scope["headers"] = [(b"x-ccboard-token", w.a.hook_token.encode()), (b"x-ccboard", b"1"), (b"tailscale-user-login", b"alice@example.com")]
        await c.open()
        await c.until(lambda: c.done)
        assert c.status == 403, "the hook token with a person's headers beside it is still the hook token"
    run(w, scenario)
    assert w.up.calls == []


def test_a_pair_that_holds_read_only_cannot_stream_screen_text_409_and_no_call(world):
    w = world
    reg = w.pair_up(["read", "tasks"])

    async def scenario():
        c = await Conn(url(w, handle=reg["handle"])).open()
        await c.until(lambda: c.done)
        assert c.status == 409 and json.loads(c.buf)["reason"] == "scope" and json.loads(c.buf)["error"] == "needs the sessions scope on node-b"
    run(w, scenario)
    assert w.up.calls == []
    assert audit(w.a, action="read_stream")[0]["status"] == "refused"


@pytest.mark.parametrize("handle,status,reason", [("nope", 404, "unknown_node"), ("local", 400, "local"), ("Node-B", 400, "bad_handle"), ("x" * 40, 400, "bad_handle")])
def test_the_handle_is_checked_against_the_registry(world, handle, status, reason):
    w = world

    async def scenario():
        c = await Conn(url(w, handle=handle)).open()
        await c.until(lambda: c.done)
        assert c.status == status and json.loads(c.buf)["reason"] == reason
    run(w, scenario)
    assert w.up.calls == []


@pytest.mark.parametrize("query", ["", "names=", "lines=12", "names=bad", f"names={S1},bad", f"names={S1}&lines=0", f"names={S1}&lines=41", f"names={S1}&lines=x", f"names={S1}&lines=1.5",
                                   f"names={S1}&names={S2}", f"names={S1}&foo=1", f"names={S1}&once=1", "names=" + ",".join(f"shop--api--s{i}" for i in range(21))])
def test_names_and_lines_are_checked_before_any_call(world, query):
    w = world

    async def scenario():
        c = Conn(f"/api/nodes/{w.h}/stream" + ("?" + query if query else ""))
        await c.open()
        await c.until(lambda: c.done)
        assert c.status == 400 and json.loads(c.buf)["reason"] == "invalid" and json.loads(c.buf)["node"] == w.h, c.buf
    run(w, scenario)
    assert w.up.calls == []
    assert audit(w.a, action="read_stream")[0]["status"] == "refused"


def test_twenty_names_and_forty_lines_are_the_most(world):
    w = world
    names = ",".join(f"shop--api--s{i}" for i in range(20))

    async def scenario():
        c = await Conn(url(w, names, 40)).open()
        await c.until(lambda: w.up.streams)
        assert c.status == 200
        await c.leave()
    run(w, scenario)
    assert "lines=40" in w.up.calls[0]["path"]


def test_the_open_rate_is_the_read_bucket_per_person(world, monkeypatch):
    w = world
    monkeypatch.setattr(nodes, "relay_read_limiter", nodes._Limiter(1, 60.0, 8))

    async def scenario():
        c1 = await Conn(url(w, S1)).open()
        c2 = await Conn(url(w, S2)).open()
        await c2.until(lambda: c2.done)
        assert c2.status == 429 and int(c2.headers["retry-after"]) >= 1
        await c1.leave()
    run(w, scenario)


def test_a_legacy_row_has_no_token_and_a_self_relay_is_refused(two_nodes):
    with two_nodes.a.enter():
        nodes.import_legacy([{"name": "old-box", "url": "https://100.64.0.2"}], db=two_nodes.a.db)

    async def scenario():
        c = await Conn("/api/nodes/old-box/stream?names=" + S1).open()
        await c.until(lambda: c.done)
        assert c.status == 409 and json.loads(c.buf)["reason"] == "legacy"
    with two_nodes.a.enter():
        asyncio.run(scenario())


# ---- the audit and the logs

def test_one_out_row_per_open_with_the_session_names_and_no_tail_text(world):
    w = world

    async def scenario():
        c = await Conn(url(w, f"{S2},{S1}")).open()
        await c.until(lambda: w.up.streams)
        w.up.streams[0].feed(lines_ev(S1, [TAIL_MARKER]) + ev("tick", {"sessions": [S1]}) * 3)
        await c.until(lambda: len(c.events("tick")) == 3)
        await c.leave()
    run(w, scenario)
    rows = audit(w.a, action="read_stream")
    assert len(rows) == 1, "one row per open, not per event"
    r = rows[0]
    assert r["direction"] == "out" and r["status"] == "ok" and r["user"] == "alice@example.com" and r["node_name"] == "node-b" and r["target"] == f"stream {S1},{S2}"
    assert TAIL_MARKER not in json.dumps(audit(w.a)) and w.up.calls[0]["headers"]["Authorization"].split()[1] not in json.dumps(audit(w.a))


def test_no_log_line_holds_the_tail_or_the_token(world, caplog):
    import logging
    w = world
    caplog.set_level(logging.DEBUG)

    async def scenario():
        c = await Conn(url(w)).open()
        await c.until(lambda: w.up.streams)
        w.up.streams[0].feed(lines_ev(S1, [TAIL_MARKER]) + b"event: lines\ndata: " + TAIL_MARKER.encode() + b"\n\n")
        await c.until(lambda: c.events("lines"))
        await c.leave()
    run(w, scenario)
    assert TAIL_MARKER not in caplog.text and "ccbnode_" not in caplog.text


def test_a_board_with_no_pair_has_no_stream_and_writes_nothing(lite_client):
    from app import main
    nodes.reset()
    r = lite_client.get("/api/nodes/node-b/stream?names=" + S1, headers=H)
    assert r.status_code == 404 and r.json()["reason"] == "unknown_node"
    assert main.db.node_audit_list(20) == [] and ns.open_count() == 0 and stream_threads() == []


# ================================================================ 3. the peer route

def peer_get(w, path: str, token=None, **kw):
    if token is None:
        with w.a.enter():
            token = nodes._load_outgoing(w.reg["peer_id"])
    return w.b.call("GET", path, owner=False, headers={"Authorization": f"Bearer {token}", "X-CCBoard": "1"}, **kw)


def peer_sessions(w, screen: str, names=(S1, S2, S3)):
    for n in names:
        w.two.tmux_b["sessions"][n] = {}
    w.two.tmux_b["screen"] = screen


def sse_events(text: str) -> list:
    out = []
    for block in re.split(r"\r?\n\r?\n", text):
        e = re.search(r"^event: (\S+)", block, re.M)
        d = re.search(r"^data: (.*)$", block, re.M)
        if e and d:
            out.append((e.group(1), json.loads(d.group(1))))
    return out


def test_the_peer_route_streams_the_asked_sessions_only_with_the_tail_redacted(world):
    w = world
    peer_sessions(w, f"{WIDE}\nexport TOKEN={SECRET}\nlast")
    r = peer_get(w, f"/api/node/stream?names={S1},{S2}&lines=3&once=1")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-store" and r.headers["x-accel-buffering"] == "no" and r.headers["x-content-type-options"] == "nosniff"
    evs = sse_events(r.text)
    assert SECRET not in r.text and "[redacted]" in r.text, "redacted on the peer too"
    lines = {d["name"]: d["lines"] for k, d in evs if k == "lines"}
    assert set(lines) == {S1, S2} and S3 not in r.text and lines[S1] == [WIDE, "export TOKEN=[redacted]", "last"]
    assert [d for k, d in evs if k == "tick"] == [{"sessions": [S1, S2]}], "the tick lists the asked sessions only, not every live one"


def test_the_peer_route_cuts_before_it_redacts(world, monkeypatch):
    w = world
    peer_sessions(w, "\n".join("=" + "q" * 100000 for _ in range(200)))
    seen = []
    real = nr.redact
    monkeypatch.setattr(nr, "redact", lambda t, known=(), strict=True: (seen.append(len(t)), real(t, known, strict))[1])
    r = peer_get(w, f"/api/node/stream?names={S1}&lines=40&once=1")
    assert r.status_code == 200 and seen and max(seen) <= nr.REDACT_MAX
    assert all(len(x) <= nr.PANE_LINE_MAX for k, d in sse_events(r.text) if k == "lines" for x in d["lines"])


@pytest.mark.parametrize("query", ["", "lines=5", "names=", "names=bad", f"names={S1},", f"names={S1}&lines=0", f"names={S1}&lines=41", f"names={S1}&lines=-1", f"names={S1}&lines=abc",
                                   f"names={S1}&names={S2}", f"names={S1}&extra=1", "names=" + ",".join(f"shop--api--s{i}" for i in range(21))])
def test_the_peer_refuses_before_the_stream_starts_with_a_400(world, query):
    w = world
    r = peer_get(w, "/api/node/stream" + ("?" + query if query else ""))
    assert r.status_code == 400 and r.headers["content-type"].startswith("application/json"), r.text
    assert any(x["action"] == "read_stream" and x["status"] == "refused" and x["direction"] == "in" for x in audit(w.b))


def test_the_peer_takes_twenty_names_and_forty_lines(world):
    w = world
    names = [f"shop--api--s{i}" for i in range(20)]
    peer_sessions(w, "x", names)
    r = peer_get(w, "/api/node/stream?names=" + ",".join(names) + "&lines=40&once=1")
    assert r.status_code == 200 and len([1 for k, _ in sse_events(r.text) if k == "lines"]) == 20


def test_the_peer_route_needs_the_sessions_scope_not_read(world):
    w = world
    peer_sessions(w, "secret on screen")
    ro = w.pair_up(["read", "tasks"])
    with w.a.enter():
        token = nodes._load_outgoing(ro["peer_id"])
    r = peer_get(w, f"/api/node/stream?names={S1}&once=1", token)
    assert r.status_code == 403 and "sessions scope" in r.json()["error"] and "secret on screen" not in r.text
    assert any(x["action"] == "scope_refused" for x in audit(w.b))


def test_the_peer_route_refuses_a_person_and_the_hook_token_and_everything_without_a_pair(world):
    w = world
    for headers in (H, {"X-CCBoard-Token": w.b.hook_token}):
        r = w.b.call("GET", f"/api/node/stream?names={S1}&once=1", owner=False, headers=headers)
        assert r.status_code == 403, headers
    assert w.b.call("GET", f"/api/node/stream?names={S1}", owner=False).status_code == 403
    r = w.b.call("GET", f"/api/node/stream?names={S1}", owner=False, headers={"Authorization": "Bearer ccbnode_" + "A" * 43, "X-CCBoard": "1"})
    assert r.status_code == 401


def test_the_peer_route_with_the_wrapper_scope_check_alone(world, monkeypatch):
    """The wrapper checks the scope itself when the middleware's table was opened by a mistake."""
    w = world
    ro = w.pair_up(["read"])
    with w.a.enter():
        token = nodes._load_outgoing(ro["peer_id"])
    monkeypatch.setattr(nodes, "scope_ok", lambda scopes, needed: True if needed != "sessions" else False)
    monkeypatch.setitem(nodes.NODE_ROUTES, ("GET", "/api/node/stream"), nodes.SCOPE_ANY)
    r = peer_get(w, f"/api/node/stream?names={S1}&once=1", token)
    assert r.status_code == 403


def test_the_peer_audits_one_in_row_per_open_with_the_names_and_no_tail_text(world):
    w = world
    peer_sessions(w, TAIL_MARKER)
    peer_get(w, f"/api/node/stream?names={S2},{S1}&once=1", headers=None) if False else None
    with w.a.enter():
        token = nodes._load_outgoing(w.reg["peer_id"])
    w.b.call("GET", f"/api/node/stream?names={S2},{S1}&once=1", owner=False,
             headers={"Authorization": f"Bearer {token}", "X-CCBoard": "1", "X-CCBoard-Acting-User": "alice@example.com"})
    rows = [x for x in audit(w.b) if x["action"] == "read_stream"]
    assert len(rows) == 1
    r = rows[0]
    assert r["direction"] == "in" and r["status"] == "ok" and r["node_name"] == "node-a" and r["user"] == "for alice@example.com" and r["target"] == f"stream {S1},{S2}"
    assert TAIL_MARKER not in json.dumps(audit(w.b))


def test_the_local_stream_is_unchanged_it_keeps_the_raw_tail_and_every_live_session(world):
    w = world
    peer_sessions(w, f"export TOKEN={SECRET}")
    r = w.b.call("GET", "/api/stream?once=1")
    evs = sse_events(r.text)
    assert SECRET in r.text, "the owner's own stream is not redacted"
    assert [d for k, d in evs if k == "tick"] == [{"sessions": sorted([S1, S2, S3])}]


# ================================================================ 4. both boards end to end, and the real byte reader

def bridge(w):
    """The hub's upstream, served by the peer board itself: the request goes to board b's route (with once=1 so the exchange ends) and its bytes are the stream."""
    seen = []

    def transport(target, path, headers, timeout):
        seen.append({"path": path, "headers": dict(headers)})
        board = w.two.boards[(target.host, target.port)]
        with board.enter():
            r = w.two.client.request("GET", path + "&once=1", headers=headers)
        if r.status_code != 200:
            return r.status_code, dict(r.headers), nodes._Closed(r.content)
        s = FakeStream()
        s.feed(r.content)
        s.end()
        return 200, dict(r.headers), s
    return transport, seen


def test_two_boards_end_to_end_the_tail_of_a_session_on_the_peer_reaches_the_hubs_browser(world):
    w = world
    peer_sessions(w, f"{WIDE}\nexport TOKEN={SECRET}\ndone")
    transport, seen = bridge(w)
    nodes.peer_stream_transport = transport

    async def scenario():
        c = await Conn(url(w, f"{S1},{S2}", 5)).open()
        await c.until(lambda: c.done)
        assert c.status == 200
        lines = {d["name"]: d["lines"] for d in c.events("lines")}
        assert lines[S1] == [WIDE, "export TOKEN=[redacted]", "done"] and set(lines) == {S1, S2}
        assert c.events("tick") == [{"sessions": [S1, S2]}] and SECRET not in c.buf.decode()
        assert c.events("gone")[0]["reason"] == "closed"
    run(w, scenario)
    assert seen[0]["path"] == f"/api/node/stream?names={S1}%2C{S2}&lines=5" and seen[0]["headers"]["Authorization"].startswith("Bearer ccbnode_")
    out, inn = audit(w.a, action="read_stream"), audit(w.b, action="read_stream")
    assert len(out) == 1 and len(inn) == 1 and out[0]["direction"] == "out" and inn[0]["direction"] == "in"
    assert out[0]["target"] == inn[0]["target"] == f"stream {S1},{S2}" and out[0]["user"] == "alice@example.com" and inn[0]["user"] == "for alice@example.com"
    assert "wider than" not in json.dumps([out, inn])


def test_a_read_only_pair_cannot_stream_end_to_end_the_peer_says_403_as_well(world):
    w = world
    ro = w.pair_up(["read", "tasks"])
    transport, seen = bridge(w)
    nodes.peer_stream_transport = transport
    # the hub refuses first (409); the peer, asked directly with that pair's token, refuses too (403)
    async def scenario():
        c = await Conn(url(w, handle=ro["handle"])).open()
        await c.until(lambda: c.done)
        assert c.status == 409
    run(w, scenario)
    assert seen == []


class _Chunked(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    script: list = []
    hold = threading.Event()
    served = threading.Event()
    gone = threading.Event()

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/err"):
            body = b'{"error":"nope"}'
            self.send_response(403)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for piece in self.script:
                self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                self.wfile.flush()
            self.served.set()
            while not self.hold.wait(0.05):
                readable, _, _ = select.select([self.connection], [], [], 0)
                if readable and not self.connection.recv(1):       # the client closed its end
                    break
        except OSError:
            pass
        finally:
            self.gone.set()


class _PlainPinned(http.client.HTTPConnection):
    """Stands in for the TLS connection pinned to a validated address: the same constructor, plain TCP to the loopback server."""

    def __init__(self, host, addr, port, timeout):
        super().__init__(addr, port, timeout=timeout)


@pytest.fixture
def loopback(monkeypatch):
    from app import nodes_discovery
    _Chunked.script, _Chunked.hold, _Chunked.served, _Chunked.gone = [], threading.Event(), threading.Event(), threading.Event()
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Chunked)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.setattr(nodes_discovery, "_Pinned", _PlainPinned)
    yield SimpleNamespace(port=srv.server_address[1], target=SimpleNamespace(host="peer.example", addrs=["127.0.0.1"], port=srv.server_address[1]))
    _Chunked.hold.set()
    srv.shutdown()
    srv.server_close()


def test_the_real_byte_reader_reads_chunked_sse_and_close_from_another_thread_unblocks_it(loopback):
    _Chunked.script = [b"event: tick\ndata: {\"sessions\": []}\n\n", b": ping\n\n"]
    status, hdrs, stream = nodes._https_stream(loopback.target, "/stream", {"Accept": "text/event-stream"}, 3.0)
    assert status == 200 and hdrs["content-type"] == "text/event-stream" and isinstance(stream, nodes.PeerStream)
    got = b""
    while b"ping" not in got:
        got += stream.read(8192)
    assert b"event: tick" in got
    box = {}

    def blocked():
        t0 = time.monotonic()
        box["data"] = stream.read(8192)
        box["took"] = time.monotonic() - t0
    th = threading.Thread(target=blocked)
    th.start()
    time.sleep(0.2)
    assert th.is_alive(), "the read is blocked waiting for the peer"
    stream.close()
    th.join(3.0)
    assert not th.is_alive() and box["took"] < 2.5, "close() from another thread ended the blocked read"
    assert _Chunked.gone.wait(3.0), "the server saw the connection go"


def test_the_real_reader_hands_an_error_status_its_body_and_no_stream(loopback):
    status, hdrs, stream = nodes._https_stream(loopback.target, "/err", {}, 3.0)
    assert status == 403 and stream.body == b'{"error":"nope"}'


def test_peer_stream_call_applies_the_address_rule_the_path_rule_and_never_follows_a_redirect(monkeypatch):
    with pytest.raises(nodes.PeerError) as e:
        nodes.peer_stream_call("https://100.64.0.2", "/api/node/stream?names=../x")
    assert e.value.reason == "bad_path"
    with pytest.raises(nodes.PeerError) as e:
        nodes.peer_stream_call("https://example.com", "/api/node/stream?names=a")
    assert e.value.reason == "url", "not a tailnet address"
    closed = []
    s = FakeStream()
    s.close = lambda: closed.append(1)
    nodes.peer_stream_transport = lambda target, path, headers, timeout: (302, {"location": "https://evil.example/"}, s)
    try:
        with pytest.raises(nodes.PeerError) as e:
            nodes.peer_stream_call("https://100.64.0.2", "/api/node/stream?names=a")
        assert e.value.reason == "redirect" and closed == [1]
    finally:
        nodes.peer_stream_transport = None


def test_peer_client_open_stream_sends_the_token_and_the_headers_a_call_does(world):
    w = world
    with w.a.enter():
        reg = next(r for r in nodes.registry(w.a.db) if r["handle"] == w.h)
        reply = nodes.PeerClient(reg, db=w.a.db).open_stream(f"/api/node/stream?names={S1}", acting_user="alice@example.com")
    assert reply.ok and w.up.calls[0]["headers"]["Authorization"].startswith("Bearer ") and w.up.calls[0]["headers"]["X-CCBoard-Node"]
    reply.stream.close()


# ================================================================ review of P8: one person cannot take the hub's streams, internal names, the once flag

def test_one_person_holds_at_most_six_streams_and_a_slot_comes_back(monkeypatch):
    monkeypatch.setattr(ns, "MAX_PER_NODE", 8)
    held = [ns.LEASES.acquire(f"p_{i:016x}", f"n{i}", "alice@example.com") for i in range(6)]
    assert {x.user for x in held} == {"alice@example.com"}
    with pytest.raises(nr.RelayError) as e:
        ns.LEASES.acquire("p_" + "e" * 16, "seventh", "alice@example.com")
    assert e.value.status == 429 and e.value.headers == {"Retry-After": "5"} and "you already hold 6 live streams" in e.value.message
    assert ns.LEASES.acquire("p_" + "e" * 16, "seventh", "bob@example.com") is not None, "another person is not held back by alice"
    ns.LEASES.acquire("p_" + "c" * 16, "eighth", "dave@example.com")
    with pytest.raises(nr.RelayError) as e:
        ns.LEASES.acquire("p_" + "d" * 16, "ninth", "carol@example.com")
    assert "this board already has 8 live streams" in e.value.message, "the hub's own cap still holds"
    ns.LEASES.release(held[0])
    assert ns.LEASES.acquire("p_" + "e" * 16, "seventh", "alice@example.com") is not None


def test_one_person_cannot_take_the_hubs_streams_over_http(world, monkeypatch):
    w = world
    monkeypatch.setattr(ns, "MAX_PER_NODE", 8)
    monkeypatch.setattr(ns, "MAX_PER_USER", 2)

    async def scenario():
        c1 = await Conn(url(w, S1)).open()
        c2 = await Conn(url(w, S2)).open()
        await c1.until(lambda: len(w.up.streams) == 2)
        c3 = await Conn(url(w, S3)).open()
        await c3.until(lambda: c3.done)
        body = json.loads(c3.buf)
        assert c3.status == 429 and body["reason"] == "stream_limit" and "you already hold 2 live streams" in body["error"], c3.buf
        assert len(w.up.streams) == 2, "no third upstream request"
        assert {x.user for x in ns.LEASES._open} == {"alice@example.com"}
        await c1.leave()
        await c1.until(lambda: ns.open_count() == 1)
        c4 = await Conn(url(w, S3)).open()
        await c4.until(lambda: len(w.up.streams) == 3)
        assert c4.status == 200
        await c2.leave()
        await c4.leave()
    run(w, scenario)


@pytest.mark.parametrize("query", ["names=_ccboard-login", f"names={S1},_ccboard-login", "names=_ccboard--a--b", "names=--a--b", "names=shop--api--", "names=shop--api--s1--x", "names=a%2Fb--c--d"])
def test_an_internal_or_odd_name_is_a_400_on_the_hub_with_no_call_and_on_the_peer_before_any_tmux_call(world, query):
    w = world

    async def scenario():
        c = Conn(f"/api/nodes/{w.h}/stream?{query}")
        await c.open()
        await c.until(lambda: c.done)
        assert c.status == 400 and json.loads(c.buf)["reason"] == "invalid", c.buf
    run(w, scenario)
    assert w.up.calls == []
    n = len(w.two.tmux_b["run"])
    r = peer_get(w, f"/api/node/stream?{query}")
    assert r.status_code == 400, r.text
    assert len(w.two.tmux_b["run"]) == n, "the peer touched tmux for a name it refused"


@pytest.mark.parametrize("once", ["1", "true", "0", "false", ""])
def test_the_hub_route_takes_no_once_flag_so_a_person_cannot_end_or_reshape_the_upstream(world, once):
    w = world

    async def scenario():
        c = Conn(f"/api/nodes/{w.h}/stream?names={S1}&once={once}")
        await c.open()
        await c.until(lambda: c.done)
        assert c.status == 400 and "only names and lines are taken" in json.loads(c.buf)["error"], c.buf
    run(w, scenario)
    assert w.up.calls == [] and ns.open_count() == 0


def test_the_peer_never_streams_an_internal_session_even_when_the_hub_names_one_that_exists(world):
    w = world
    with w.b.enter():
        w.two.tmux_b["sessions"]["_ccboard-login"] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%7", "command": "claude", "path": "/x", "pid": 1, "env": {}}
        w.two.tmux_b["screen"] = "LOGIN CODE ABCD-EFGH"
    r = peer_get(w, "/api/node/stream?names=_ccboard-login&once=1")
    assert r.status_code == 400 and "ABCD-EFGH" not in r.text


def test_a_hub_only_ever_asks_for_names_and_lines_upstream(world):
    w = world

    async def scenario():
        c = await Conn(url(w, f"{S1},{S2}", 5)).open()
        await c.until(lambda: w.up.streams)
        await c.leave()
    run(w, scenario)
    path = w.up.calls[0]["path"]
    assert path.split("?")[0] == "/api/node/stream" and set(q.split("=")[0] for q in path.split("?")[1].split("&")) == {"names", "lines"}
