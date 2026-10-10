"""Finding the other ccboard nodes on the tailnet (the nodes epic, issue #134, phase P2).

What this module does, in order: read `tailscale status --json` (through app/nodes.py, which reads it through app/tailscale.py), keep the devices that
may be nodes (`candidates`), and, only when a person asked for a refresh, ask each online one `GET /api/node/hello` (`probe`). Finding a node gives it
no access; pairing does (issue #135). Nothing here runs at import, at start or on a timer: no thread, no request, no timer. A refresh uses a pool of
four threads that lives for that one request and is gone when the answer is.

Which devices are candidates (the decision of issue #132, option C), `candidates(status, tags, self_user)`
  * a peer that belongs to the same Tailscale user as this device (`UserID` equal, and this device is not tagged), or
  * a peer whose `Tags` hold a name from CCBOARD_NODE_TAGS (default `tag:ccboard`); `none` leaves only the first rule;
  * never this device, never a peer without a `DNSName`, never a peer shared in from another user: a device whose name is not under this tailnet's
    MagicDNS suffix, or that Tailscale marks as a sharee, is not listed even when it carries a configured tag (a tag is only a name, and any
    tailnet may use `tag:ccboard`).
  A tagged device has no user, and every tagged device of a tailnet shares one pseudo user id, so when this device is tagged nothing counts as "the
  same user" and only the tag rule applies.

What a probe may reach (the rule is `nodes.check_peer_url`): https only, no user info, query or fragment, a MagicDNS name under the tailnet's suffix
or an address in 100.64.0.0/10 or fd7a:115c:a1e0::/48, and a name must resolve only to such addresses. The name is resolved once; the connection goes
to that validated address while the certificate is checked for the name, so a second DNS answer cannot redirect it. Redirects are never followed (a
3xx answer is `unreachable`), an offline device is never probed, at most four probes are in flight, the request carries no header of its own (no
credential, no identity) and nothing is believed about the peer beyond the hello's three keys (`app`, `api`, `node_id`).

Probe states
  found       200 and a ccboard hello (`app` ccboard, `api` a whole number, `node_id` an id); carries the node id and the address that answered
  refuses     401 or 403: something answers and refuses us (a grant or an identity is missing)
  no_ccboard  connection refused, or an answer that is not a ccboard hello (the tailnet works; ccboard or `tailscale serve` is down or on another port)
  no_tls      the TLS handshake failed (HTTPS is not enabled for the tailnet, the certificate is wrong, or the port is not HTTPS). An IP-literal
              address fails here too, because the certificate is for the MagicDNS name
  unreachable a timeout, a redirect, a rate-limit answer, a name that does not resolve, anything else
  offline     Tailscale says the device is not connected; no probe is made
  invalid     the device's name fails the address rule; no probe is made
  unchecked   (answer only) the device is a candidate but nobody has asked for a probe yet
Over several ports the best answer wins: found, refuses, no_tls, no_ccboard, unreachable; the loop stops at found or refuses.

The cache is kv `node_discovery`: {at, rows: {ts_id: {state, url, node_id, t}}}, one entry per candidate that was probed. An entry older than 30 s is
shown as stale and refreshed on the next refresh; it is never removed for age (only a device that stopped being a candidate drops out).
"""
from __future__ import annotations

import concurrent.futures
import http.client
import json
import logging
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from . import nodes
from . import tailscale as ts
from .config import settings

log = logging.getLogger("ccboard.nodes.discovery")

KV = "node_discovery"
HELLO_PATH = "/api/node/hello"
PROBE_TIMEOUT = 2.0         # seconds one attempt may take, connect, handshake and answer together
RESOLVE_TIMEOUT = 2.0       # seconds a name lookup may take before the row is `unreachable` (getaddrinfo itself has no timeout)
MAX_LOOKUPS = 4             # lookups still running, across refreshes (a lookup that outlived its timeout keeps its slot until it ends)
MAX_IN_FLIGHT = 4
STALE_AFTER = 30.0          # seconds after which a probe result is marked stale
MIN_REPROBE = 5.0           # a refresh leaves a result younger than this alone (two tabs, a double tap)
REFRESH_DEADLINE = 8.0      # seconds a refresh waits for its probes before it answers with what it has
BODY_MAX = 4096
MAX_ROWS = 64
STATES = ("found", "refuses", "no_ccboard", "no_tls", "unreachable", "offline", "invalid", "unchecked")
_RANK = {"found": 5, "refuses": 4, "no_tls": 3, "no_ccboard": 2, "unreachable": 1}

_slots = threading.BoundedSemaphore(MAX_IN_FLIGHT)    # probes in flight, across refreshes (a refresh that gave up leaves its stragglers here)
_refresh_lock = threading.Lock()                      # one refresh at a time; a second caller is answered from the cache
_lookups = threading.BoundedSemaphore(MAX_LOOKUPS)


def bounded_resolver(resolver: Callable | None = None) -> Callable:
    """`resolver(host, port)` with a deadline: the lookup runs in a daemon thread and a probe waits RESOLVE_TIMEOUT for it, then counts the name as
    unresolved (OSError) and moves on. The thread cannot be stopped, so it keeps one of MAX_LOOKUPS slots until the system resolver gives up;
    with every slot taken a lookup is refused at once. So a probe always ends within RESOLVE_TIMEOUT plus PROBE_TIMEOUT per port, and the refresh
    lock it holds (discover) always comes back: a hanging resolver can slow discovery, never switch it off."""
    real = resolver or nodes._resolve_all

    def look(host: str, port: int) -> list[str]:
        if not _lookups.acquire(blocking=False):
            raise OSError("too many name lookups still running")
        box: dict = {}
        done = threading.Event()

        def run() -> None:
            try:
                box["v"] = list(real(host, port))
            except BaseException as e:      # handed back to the probe below
                box["e"] = e
            finally:
                done.set()
                _lookups.release()
        threading.Thread(target=run, name="ccboard-lookup", daemon=True).start()
        if not done.wait(RESOLVE_TIMEOUT):
            raise OSError("the name lookup timed out")
        if "e" in box:
            raise box["e"]
        return box["v"]
    return look


# ---------------------------------------------------------------- the candidate list (pure)

def _text(v, n: int) -> str | None:
    """A short printable text from the status file, or None."""
    if not isinstance(v, str):
        return None
    t = "".join(c for c in v if c.isprintable()).strip()[:n]
    return t or None


def magic_suffix(status) -> str | None:
    """The tailnet's MagicDNS suffix from the status file (`CurrentTailnet.MagicDNSSuffix`, else the older top-level key), lower case, no dots at the ends."""
    if not isinstance(status, dict):
        return None
    cur = status.get("CurrentTailnet")
    raw = cur.get("MagicDNSSuffix") if isinstance(cur, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        raw = status.get("MagicDNSSuffix")
    s = raw.strip().strip(".").lower() if isinstance(raw, str) else ""
    return s or None


def _tags(v) -> list[str]:
    return [t[:64] for t in (v or []) if isinstance(t, str) and t][:16] if isinstance(v, list) else []


def self_user(status) -> int | None:
    """The Tailscale user id of this device, or None when this device is tagged (a tagged device has no user) or the file does not say."""
    me = status.get("Self") if isinstance(status, dict) else None
    if not isinstance(me, dict) or _tags(me.get("Tags")):
        return None
    uid = me.get("UserID")
    return uid if isinstance(uid, int) and not isinstance(uid, bool) and uid > 0 else None


def _peers(status) -> list[dict]:
    p = status.get("Peer") if isinstance(status, dict) else None
    items = list(p.values()) if isinstance(p, dict) else (list(p) if isinstance(p, list) else [])
    return [x for x in items if isinstance(x, dict)]


def candidates(status, tags, self_user) -> list[dict]:
    """The devices of `status` (the parsed `tailscale status --json`) that may be ccboard nodes: rows
    {ts_id, name, dns_name, os, online, last_seen, tags, same_user, ips}, online first, then by name, keyed by the Tailscale id (`ID`), because
    `HostName` is not unique. `tags` is the configured tag names; `self_user` the user id of this device (None = no user, see self_user()).
    Excludes this device, a peer without a DNSName, a peer shared in from another user, and a peer that is neither the same user's nor tagged
    with a configured name. Pure: no clock, no network."""
    tagset = {t for t in (tags or ()) if isinstance(t, str) and t}
    suffix = magic_suffix(status)
    me = status.get("Self") if isinstance(status, dict) else None
    my_id = me.get("ID") if isinstance(me, dict) else None
    out, seen = [], set()
    for p in _peers(status):
        tid = p.get("ID")
        if not isinstance(tid, str) or not nodes_id_ok(tid) or tid == my_id or tid in seen:
            continue
        raw_dns = p.get("DNSName")
        dns = raw_dns.strip().rstrip(".") if isinstance(raw_dns, str) else ""
        if not dns or p.get("ShareeNode"):
            continue
        ptags = _tags(p.get("Tags"))
        uid = p.get("UserID")
        same = bool(self_user is not None and not ptags and isinstance(uid, int) and not isinstance(uid, bool) and uid == self_user)
        tagged = bool(tagset and any(t in tagset for t in ptags))
        if not (same or tagged):
            continue
        if not same and suffix and not dns.lower().endswith("." + suffix):
            continue                              # a tag from another tailnet's device (shared in) proves nothing
        seen.add(tid)
        ok_dns = len(dns) <= 253 and all(c.isalnum() and c.isascii() or c in ".-" for c in dns)
        name = _text(p.get("HostName"), 63) or (dns.split(".")[0][:63] if ok_dns else None) or tid
        ls = _text(p.get("LastSeen"), 40)
        out.append({
            "ts_id": tid, "name": name, "dns_name": dns if ok_dns else None, "os": _text(p.get("OS"), 32),
            "online": bool(p.get("Online")) and not p.get("Expired"),
            "last_seen": None if not ls or ls.startswith("0001-") else ls,
            "tags": ptags, "same_user": same,
            "ips": [a for a in (p.get("TailscaleIPs") or []) if isinstance(a, str) and nodes.in_tailnet(a)] if isinstance(p.get("TailscaleIPs"), list) else [],
        })
    out.sort(key=lambda r: (not r["online"], r["name"].lower(), r["ts_id"]))
    return out


def nodes_id_ok(v: str) -> bool:
    return 0 < len(v) <= 64 and all(c.isalnum() and c.isascii() or c in "._-" for c in v)


# ---------------------------------------------------------------- the connection (pinned to a validated address)

class _Pinned(http.client.HTTPSConnection):
    """An HTTPS connection whose TCP connection goes to `addr`, an address that was validated, while the TLS handshake and the Host header use the
    name: the certificate is verified for the name (default context: certificates and host name are checked) and the name is not looked up again."""

    def __init__(self, host: str, addr: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._addr = addr

    def connect(self):
        sock = socket.create_connection((self._addr, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def pinned_transport(host: str, addr: str, port: int, timeout: float = PROBE_TIMEOUT) -> tuple[int, bytes]:
    """GET /api/node/hello from `host`:`port` through the connection to `addr`. Returns (status, first BODY_MAX bytes). No redirect is followed
    (http.client never does), no header is added to the request, and the whole exchange has to end within `timeout` seconds. Raises what the socket
    layer raises (ConnectionRefusedError, ssl.SSLError, TimeoutError, OSError)."""
    end = time.monotonic() + timeout
    conn = _Pinned(host, addr, port, timeout)
    try:
        conn.request("GET", HELLO_PATH)
        resp = conn.getresponse()
        body = b""
        while len(body) < BODY_MAX:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError("the answer took too long")
            if conn.sock is not None:
                conn.sock.settimeout(left)
            chunk = resp.read(min(1024, BODY_MAX - len(body)))
            if not chunk:
                break
            body += chunk
        return resp.status, body
    finally:
        conn.close()


# ---------------------------------------------------------------- one probe

def _classify(status: int, body: bytes) -> tuple[str, str | None]:
    """(state, node_id) of one answer."""
    if status in (401, 403):
        return "refuses", None
    if 300 <= status < 400 or status == 429:
        return "unreachable", None                       # a redirect is never followed; a rate-limit answer says nothing about the node
    if status == 200:
        try:
            d = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return "no_ccboard", None
        if (isinstance(d, dict) and d.get("app") == nodes.APP and isinstance(d.get("api"), int) and not isinstance(d.get("api"), bool)
                and isinstance(d.get("node_id"), str) and nodes.ID_RE.fullmatch(d["node_id"])):
            return "found", d["node_id"]                 # the hello's other keys, if any, are not read
    return "no_ccboard", None


def _error_state(e: BaseException) -> str:
    if isinstance(e, ssl.SSLError):
        return "no_tls"
    if isinstance(e, ConnectionRefusedError):
        return "no_ccboard"
    return "unreachable"                                 # a timeout, a reset, no route, anything else


def _result(state: str, url: str | None = None, node_id: str | None = None) -> dict:
    return {"state": state, "url": url, "node_id": node_id}


def _url_for(host: str, port: int) -> str:
    return f"https://{host}" + ("" if port == 443 else f":{port}")


def probe(row: dict, ports, suffix, transport: Callable | None = None, resolver: Callable | None = None) -> dict:
    """Probe one candidate row. Returns {state, url, node_id}; never raises. `transport(host, addr, port, timeout) -> (status, body)` is
    pinned_transport unless a test passes a fake. The name is resolved here, once, through `resolver` (see nodes.check_peer_url)."""
    try:
        return _probe(row, ports, suffix, transport or pinned_transport, resolver)
    except Exception as e:
        log.debug("probe failed: %s", e.__class__.__name__)
        return _result("unreachable")


def _probe(row, ports, suffix, transport, resolver) -> dict:
    if not row.get("online"):
        return _result("offline")
    dns = row.get("dns_name")
    if not dns:
        return _result("invalid")
    try:
        target = nodes.check_peer_url(f"https://{dns}", suffix, bounded_resolver(resolver))
    except nodes.PeerUrlError as e:
        return _result("unreachable" if e.unresolved else "invalid")
    addr = target.addrs[0]
    best = _result("unreachable")
    seen_any = False
    for port in ports:
        url = _url_for(target.host, port)
        try:
            nodes.check_peer_url(url, suffix, lambda h, p, a=target.addrs: list(a))     # the syntax of this port's url; no second lookup
        except nodes.PeerUrlError:
            continue
        with _slots:
            try:
                status, body = transport(target.host, addr, port, PROBE_TIMEOUT)
                state, node_id = _classify(status, body)
            except Exception as e:
                state, node_id = _error_state(e), None
        res = _result(state, url if state in ("found", "refuses") else None, node_id)
        if not seen_any or _RANK[state] > _RANK[best["state"]]:
            best, seen_any = res, True
        if state in ("found", "refuses"):
            break
    return best


# ---------------------------------------------------------------- reading Tailscale for discovery and for the doctor

def read_tailscale(fresh: bool = False) -> tuple[dict | None, dict]:
    """(status, {ok, reason, variant}). `status` is the parsed `tailscale status --json` (None when it cannot be used) and `reason` the sentence for
    a person when `ok` is false. `fresh` asks Tailscale again; otherwise a reading the board took in the last two minutes is reused. Never raises."""
    try:
        cli = ts.find_cli()
        variant = ts.variant(cli)
    except Exception:
        cli, variant = None, "unknown"
    info = {"ok": False, "reason": "", "variant": variant}
    gui = variant in ("macos-appstore", "macos-standalone")
    if cli is None:
        info["reason"] = ts.missing_reason()
        return None, info
    d = nodes._ts_status() if fresh else nodes._ts()
    if not isinstance(d, dict) or not d:
        info["reason"] = ("Tailscale is not running or cannot be reached from the board: open the Tailscale app and make sure it is signed in" if gui
                          else "Tailscale is not running or cannot be reached from the board: start it, then try again")
        return None, info
    if str(d.get("BackendState") or "") != "Running":
        info["reason"] = ("Tailscale is not signed in on this device: open the Tailscale app and sign in" if gui
                          else "Tailscale is not logged in on this device: run `tailscale up`")
        return None, info
    if not magic_suffix(d):
        info["reason"] = "MagicDNS is not turned on for this tailnet, so devices cannot be found by name: turn it on in the Tailscale admin console (DNS page)"
        return None, info
    info["ok"] = True
    return d, info


# ---------------------------------------------------------------- the answer

def _iso(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def _load(db) -> dict:
    try:
        rec = db.kv_get(KV)
    except Exception:
        return {}
    v = rec.get("value") if isinstance(rec, dict) else None
    rows = v.get("rows") if isinstance(v, dict) else None
    return {k: x for k, x in rows.items() if isinstance(k, str) and isinstance(x, dict)} if isinstance(rows, dict) else {}


def _syntax_ok(dns, suffix) -> bool:
    """Does the device's name pass the address rule's syntax, with no lookup?"""
    if not dns:
        return False
    try:
        nodes.check_peer_url(f"https://{dns}", suffix, lambda h, p: ["100.64.0.1"])
        return True
    except nodes.PeerUrlError:
        return False


def _out_row(row: dict, cached: dict | None, now: float, suffix) -> dict:
    t = cached.get("t") if isinstance(cached, dict) and isinstance(cached.get("t"), (int, float)) else None
    if not row["online"]:
        state, url, nid, t = "offline", None, None, None
    elif not _syntax_ok(row["dns_name"], suffix):
        state, url, nid, t = "invalid", None, None, None
    elif cached is not None and t is not None and cached.get("state") in STATES:
        state, url, nid = cached["state"], cached.get("url"), cached.get("node_id")
    else:
        state, url, nid, t = "unchecked", None, None, None
    age = None if t is None else max(0, int(now - t))
    return {"ts_id": row["ts_id"], "name": row["name"], "dns_name": row["dns_name"], "os": row["os"], "online": row["online"],
            "last_seen": row["last_seen"], "owner": "user" if row["same_user"] else "tag", "tags": row["tags"], "state": state,
            "url": url, "node_id": nid, "at": _iso(t), "age": age, "stale": bool(age is not None and age > STALE_AFTER)}


def _probe_all(rows: list[dict], ports, suffix, transport, resolver, on_idle: Callable | None = None) -> dict[str, dict]:
    """Probe `rows` with at most MAX_IN_FLIGHT at a time; {ts_id: result}. Waits REFRESH_DEADLINE at most, a row that has not answered by then is
    `unreachable`. The pool exists for this call only. `on_idle` runs once every probe of this call has finished or been cancelled, which can be
    after this returns: discover() releases the refresh lock there, so a refresh that gave up never overlaps the next one and the probe threads
    stay at MAX_IN_FLIGHT however often Refresh is pressed (a straggler past the deadline still holds the lock)."""
    if not rows:
        if on_idle:
            on_idle()
        return {}
    try:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_IN_FLIGHT, thread_name_prefix="ccboard-discover")
        futs = {pool.submit(probe, r, ports, suffix, transport, resolver): r["ts_id"] for r in rows}
    except BaseException:
        if on_idle:                                      # nothing is running: the caller's lock must not stay held
            on_idle()
        raise
    left, mu = [len(futs)], threading.Lock()

    def _one_done(_f) -> None:
        with mu:
            left[0] -= 1
            last = left[0] == 0
        if last and on_idle:
            on_idle()
    for f in futs:
        f.add_done_callback(_one_done)
    try:
        concurrent.futures.wait(list(futs), timeout=REFRESH_DEADLINE)
        return {tid: (f.result() if f.done() and not f.cancelled() else _result("unreachable")) for f, tid in futs.items()}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def discover(db, *, refresh: bool = False, transport: Callable | None = None, resolver: Callable | None = None, clock: Callable | None = None) -> dict:
    """The answer of GET /api/nodes/discover: {at, tailscale: {ok, reason, variant}, rows}. Never raises (a failure is `ok: false` and a reason).
    Without `refresh` nothing leaves the board: the candidates come from Tailscale's own reading and each row carries its last probe result (or
    `unchecked`). With `refresh` the online candidates are probed (at most four at once, none that were probed in the last MIN_REPROBE seconds) and the
    results are kept in kv `node_discovery`. The rows hold no address and nothing about a peer beyond the hello's node id."""
    now = (clock or time.time)()
    try:
        status, info = read_tailscale(fresh=refresh)
        if status is None:
            return {"at": _iso(now), "tailscale": info, "rows": []}
        suffix = magic_suffix(status)
        cand = candidates(status, settings.node_tags, self_user(status))[:MAX_ROWS]
        cache = _load(db)
        if refresh and _refresh_lock.acquire(blocking=False):
            handed = False                                       # True once _probe_all owns the release (it frees the lock when its last probe ends)
            try:
                todo = [r for r in cand if r["online"] and _syntax_ok(r["dns_name"], suffix)
                        and not (isinstance(cache.get(r["ts_id"]), dict) and now - float(cache[r["ts_id"]].get("t") or 0) < MIN_REPROBE)]
                handed = True
                for tid, res in _probe_all(todo, settings.node_ports, suffix, transport, resolver, on_idle=_refresh_lock.release).items():
                    cache[tid] = {**res, "t": now}
                keep = {r["ts_id"] for r in cand}
                cache = {k: v for k, v in cache.items() if k in keep}
                db.kv_set(KV, {"at": now, "rows": cache})
            finally:
                if not handed:
                    _refresh_lock.release()
        return {"at": _iso(now), "tailscale": info, "rows": [_out_row(r, cache.get(r["ts_id"]), now, suffix) for r in cand]}
    except Exception as e:
        log.warning("discovery failed: %s", e.__class__.__name__)
        return {"at": _iso(now), "tailscale": {"ok": False, "reason": f"Finding nodes failed ({e.__class__.__name__}); try again", "variant": "unknown"}, "rows": []}
