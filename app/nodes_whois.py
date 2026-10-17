"""The optional device binding and app-capability check for calls from paired nodes (issue #136). Off by default.

CCBOARD_NODE_AUTH picks how a call that carries a node token (`Authorization: Bearer ccbnode_...`) is checked:

  token      the default: the pair token alone, exactly as before. Nothing in this module runs: no whois, no forwarded header read, no audit row.
  whois      the token, AND the device it is used from must be the device that paired: the receiving board asks Tailscale who the caller's address
             is (`tailscale whois`) and compares its StableNodeID with `peer_ts_id`, stored in the pair row when the pair was made.
  whois+cap  that, AND the tailnet policy must grant the app capability CCBOARD_NODE_CAP with the operation of the route's scope
             (`node.read`, `node.tasks`, `node.sessions`, `node.permissions`), read from the header Serve adds (`Tailscale-App-Capabilities`).

Where the caller is. `tailscale serve` ends TLS and proxies to the board on loopback, putting the caller's tailnet address in X-Forwarded-For (it
deletes a client-sent copy). uvicorn's proxy headers are on by default and believe only 127.0.0.1 and ::1: for a connection from Serve it has already
replaced the loopback peer (`request.client`) with the rightmost forwarded hop that is not loopback, which is the one Serve added (Serve sends one);
for any other peer it keeps the real peer and ignores the header. So the peer this module sees is NOT the proxy. caller() asks, in this order: the
connection arrived on a loopback address of this board (the ASGI `server`, which uvicorn never rewrites; nothing else is read before this), the
request carried X-Forwarded-For at all (one without it reached the board directly), and the caller is then `request.client`, which must be a tailnet
address (with --no-proxy-headers the peer stays loopback and the last X-Forwarded-For hop is taken instead). The whois modes refuse to switch on while
the board listens beyond loopback (board-bind, bind_state()) or uvicorn was told to believe forwarded headers from wider (FORWARDED_ALLOW_IPS or
--forwarded-allow-ips, proxy_trust()). What this does not cover: any process on this machine can connect to 127.0.0.1 and send these headers, so the
check does not protect against a hostile local user (the same holds for the Tailscale-User-Login sign-in).

Fail closed. In the whois modes a call is answered only when every step says yes. Tailscale that cannot answer is a 503 `tailscale_unavailable`,
too many uncached lookups a 429, a wrong device a 403 `device_mismatch`, a pair made before the binding was on a 403 `not_bound`; each refusal
writes an audit row with its reason. A mode that cannot switch on (the board listens wider, no capability name, a typo in the setting) refuses every
node call with a 503 and says why in Doctor and Settings; it never falls back to the token alone.

Never applied by ccboard: the grant and the Serve flag are text for the owner (grant_snippet, serve_command). Nothing here runs `tailscale serve`
or changes the tailnet policy; the only commands are the reads `whois`, `version`, `status` and `serve status`.
"""
from __future__ import annotations

import email.errors
import email.header
import ipaddress
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import NamedTuple

from . import tailscale as ts
from .config import NODE_AUTH_MODES, settings

log = logging.getLogger("ccboard.nodes_whois")

TOKEN, WHOIS, WHOIS_CAP = NODE_AUTH_MODES
CAP_HEADER = "tailscale-app-capabilities"
CAP_HEADER_MAX = 16 * 1024                   # bytes of the raw header read at most (a larger one is refused before it is decoded or parsed)
CAP_OBJECTS_MAX = 64                          # grant objects merged at most
OPS = {"read": "node.read", "tasks": "node.tasks", "sessions": "node.sessions", "permissions": "node.permissions"}
PLACEHOLDER_CAP = "example.com/cap/ccboard"   # the snippet's name while CCBOARD_NODE_CAP is unset: a reserved domain, never a real one
PLACEHOLDER_LOGIN = "<owner login>"
BIND_TTL = 60.0

REASONS = {
    "bad_mode": "CCBOARD_NODE_AUTH is not token, whois or whois+cap, so every call from a paired node is refused until it is fixed",
    "board_bind": "the board listens beyond loopback, so forwarded addresses could be forged; the whois modes stay off and every call from a paired node is refused",
    "no_cap_name": "whois+cap needs CCBOARD_NODE_CAP (<a domain you control>/cap/ccboard); every call from a paired node is refused until it is set",
    "not_via_serve": "a node call must come through tailscale serve (a loopback connection that carries X-Forwarded-For) when the device check is on",
    "proxy_trust": "uvicorn believes forwarded addresses from beyond loopback (FORWARDED_ALLOW_IPS or --forwarded-allow-ips), so a caller address could be forged; the whois modes stay off and every call from a paired node is refused",
    "no_caller_address": "the forwarded caller address is missing or is not a tailnet address",
    "not_bound": "this pair has no bound device (it was made before the device check was on, or its device could not be confirmed); pair the node again",
    "tailscale_unavailable": "Tailscale could not say who is calling, so the call is refused (the device check never falls back to the token alone)",
    "whois_rate_limited": "too many device lookups this minute; try again shortly",
    "device_mismatch": "this device is not the one that was paired",
    "tag_refused": "the calling device does not carry a tag from CCBOARD_NODE_TAGS",
    "cap_missing": "the tailnet policy gives this device no ccboard capability (no capability header)",
    "cap_invalid": "the capability header could not be read",
    "cap_op_missing": "the tailnet policy does not grant this operation to this device",
}
STATUS = {"bad_mode": 503, "board_bind": 503, "proxy_trust": 503, "no_cap_name": 503, "tailscale_unavailable": 503, "whois_rate_limited": 429}


class Denial(NamedTuple):
    status: int
    reason: str
    message: str
    retry_after: int = 0
    detail: str = ""


def _deny(reason: str, detail: str = "", retry_after: int = 0) -> Denial:
    return Denial(STATUS.get(reason, 403), reason, REASONS[reason], int(retry_after or 0), detail)


_lock = threading.Lock()
_last_denial: dict | None = None             # {at, reason, node} of the newest refusal, for Doctor and Settings (memory only)
_bind_cache: list = []                        # [(monotonic at, state)]


def reset() -> None:
    """Forget the last denial and the bind reading (tests)."""
    global _last_denial
    with _lock:
        _last_denial = None
        _bind_cache.clear()


# ---------------------------------------------------------------- the mode and its precondition

def mode() -> str:
    """The configured mode: token, whois, whois+cap, or the text of an unknown value (which counts as on, and refuses: never as token)."""
    return settings.node_auth


def enabled() -> bool:
    """Is any check beyond the token on? False only for `token` (the default): then nothing in this module runs."""
    return mode() != TOKEN


def _loopback(host) -> bool:
    h = str(host or "").strip().strip("[]").split("%")[0]
    if h.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _hex_addr(text: str) -> str | None:
    """An address from /proc/net/tcp{,6} (little-endian hex words) as text."""
    try:
        raw = bytes.fromhex(text)
        if len(raw) == 4:
            return str(ipaddress.IPv4Address(raw[::-1]))
        if len(raw) == 16:
            return str(ipaddress.IPv6Address(b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))))
    except ValueError:
        return None
    return None


def proc_listeners(port: int, root: Path = Path("/proc/net")) -> list[str] | None:
    """The local addresses listening on TCP `port` from /proc/net/tcp and tcp6 (Linux, the container included: it shares the host's network), or None
    when neither file can be read."""
    out, read = [], False
    for name in ("tcp", "tcp6"):
        try:
            lines = (root / name).read_text().splitlines()[1:]
        except OSError:
            continue
        read = True
        for line in lines:
            parts = line.split()
            if len(parts) < 4 or parts[3] != "0A" or ":" not in parts[1]:        # 0A = LISTEN
                continue
            addr, _, hport = parts[1].rpartition(":")
            try:
                if int(hport, 16) != int(port):
                    continue
            except ValueError:
                continue
            a = _hex_addr(addr)
            if a and a not in out:
                out.append(a)
    return out if read else None


def _psutil_listeners(port: int) -> list[str] | None:
    try:
        import psutil                        # not a dependency on Linux; on a Mac it is in requirements.txt
        conns = psutil.Process().net_connections(kind="inet")
    except Exception:
        return None
    return sorted({c.laddr.ip for c in conns if c.status == "LISTEN" and c.laddr and c.laddr.port == int(port)})


def listeners(port: int) -> list[str] | None:
    """Where this machine listens on the board's port: /proc on Linux, psutil (this process) elsewhere; None when it cannot be read. Tests replace it."""
    got = proc_listeners(port)
    return got if got is not None else _psutil_listeners(port)


def bind_state(fresh: bool = False) -> dict:
    """board-bind: does the board listen on loopback only? {ok, hosts, source, reason}. `ok` is True (every address read is loopback), False (one is
    not: forwarded headers could be forged, so the whois modes refuse to switch on) or None (nothing could be read: unknown, never a pass). Read from
    the settings (the uvicorn --host of this process) and from the socket table; both must be loopback. Cached BIND_TTL seconds."""
    with _lock:
        if _bind_cache and not fresh and time.monotonic() - _bind_cache[0][0] < BIND_TTL:
            return dict(_bind_cache[0][1])
    hosts, sources = [], []
    if settings.bind_host:
        hosts.append(str(settings.bind_host))
        sources.append("settings")
    try:
        socks = listeners(settings.port)
    except Exception:
        socks = None
    if socks:
        hosts += [h for h in socks if h not in hosts]
        sources.append("socket")
    wide = [h for h in hosts if not _loopback(h)]
    if not hosts:
        st = {"ok": None, "hosts": [], "source": None,
              "reason": "could not tell which address the board listens on (no uvicorn --host and no socket table to read); to verify on this runtime"}
    elif wide:
        st = {"ok": False, "hosts": hosts, "source": "+".join(sources),
              "reason": f"the board listens on {', '.join(wide)}, beyond loopback: anyone who reaches that address could forge the forwarded headers"}
    else:
        st = {"ok": True, "hosts": hosts, "source": "+".join(sources), "reason": f"the board listens on {', '.join(hosts)} only"}
    with _lock:
        _bind_cache[:] = [(time.monotonic(), st)]
    return dict(st)


def _loopback_net(text: str) -> bool:
    try:
        return ipaddress.ip_network(text.strip().strip("[]"), strict=False).is_loopback
    except ValueError:
        return False


def proxy_trust() -> dict:
    """Whose X-Forwarded-For uvicorn believes: {ok, source, value}. uvicorn's default (127.0.0.1 and ::1) is ok. A --forwarded-allow-ips on this
    process's command line or FORWARDED_ALLOW_IPS in its environment that names anything but a loopback address or network (`*` included) is not:
    then a peer beyond loopback could put any address in request.client, so the whois modes refuse to switch on. Both are read; either one wide refuses."""
    for source, value in (("--forwarded-allow-ips", getattr(settings, "forwarded_allow_argv", None)), ("FORWARDED_ALLOW_IPS", os.environ.get("FORWARDED_ALLOW_IPS"))):
        if value is None:
            continue
        wide = [x for x in (i.strip() for i in str(value).split(",")) if x and not _loopback_net(x)]
        if wide:
            return {"ok": False, "source": source, "value": ", ".join(wide)[:120]}
    return {"ok": True, "source": None, "value": None}


def readiness() -> dict:
    """{mode, ready, reason, message}: can the configured mode run now? `token` always can. A whois mode cannot when the setting is unknown, the
    board listens beyond loopback (an unknown bind is allowed and shown as unknown) or, for whois+cap, CCBOARD_NODE_CAP is unset or malformed."""
    m = mode()
    if m == TOKEN:
        return {"mode": m, "ready": True, "reason": None, "message": "the pair token alone (the default)"}
    if settings.node_auth_bad or m not in NODE_AUTH_MODES:
        return {"mode": m, "ready": False, "reason": "bad_mode", "message": REASONS["bad_mode"]}
    if bind_state()["ok"] is False:
        return {"mode": m, "ready": False, "reason": "board_bind", "message": REASONS["board_bind"]}
    pt = proxy_trust()
    if not pt["ok"]:
        return {"mode": m, "ready": False, "reason": "proxy_trust", "message": f"{REASONS['proxy_trust']} ({pt['source']} names {pt['value']})"}
    if m == WHOIS_CAP and not settings.node_cap:
        return {"mode": m, "ready": False, "reason": "no_cap_name",
                "message": REASONS["no_cap_name"] + (" (the value set is not a capability name)" if settings.node_cap_bad else "")}
    return {"mode": m, "ready": True, "reason": None, "message": "on"}


def startup_check() -> dict | None:
    """At the board's start: say once in the log what the mode is and whether it can run. Nothing at all in `token` mode."""
    if not enabled():
        return None
    r = readiness()
    if r["ready"]:
        log.info("node auth %s: on (%s)", r["mode"], bind_state()["reason"])
    else:
        log.error("node auth %s cannot switch on: %s", r["mode"], r["message"])
    return r


# ---------------------------------------------------------------- the capability header

def decode_header_value(raw: str) -> str:
    """An RFC 2047 encoded value (`=?utf-8?q?...?=` or `?b?`, possibly several words) as text, with the standard library; plain text as it is.
    ValueError when it cannot be decoded."""
    if "=?" not in raw:
        return raw
    try:
        parts = email.header.decode_header(raw)
        return "".join(p.decode(cs or "ascii") if isinstance(p, bytes) else p for p, cs in parts)
    except (LookupError, UnicodeDecodeError, email.errors.HeaderParseError) as e:
        raise ValueError(e.__class__.__name__) from None


def parse_caps(raw, cap: str) -> dict:
    """The operations a capability header grants under `cap`: {ok: True, ops: [..]} or {ok: False, reason: cap_missing | cap_invalid, detail}.
    The header is JSON {"<cap>": [{...}, ...]} (one object per matching grant), possibly RFC 2047 encoded. It is size-capped before it is decoded,
    decoded, then parsed; the objects under `cap` are merged: their `ops` lists are joined. Anything that is not that shape is cap_invalid; a header
    without `cap` (or an empty one) is cap_missing."""
    if raw is None or not str(raw).strip():
        return {"ok": False, "reason": "cap_missing", "detail": "no capability header"}
    raw = str(raw)
    if len(raw.encode("utf-8", "replace")) > CAP_HEADER_MAX:
        return {"ok": False, "reason": "cap_invalid", "detail": "the header is too large"}
    try:
        text = decode_header_value(raw.strip())
    except ValueError:
        return {"ok": False, "reason": "cap_invalid", "detail": "the header is not valid RFC 2047"}
    if len(text) > CAP_HEADER_MAX:
        return {"ok": False, "reason": "cap_invalid", "detail": "the header is too large"}
    try:
        d = json.loads(text)
    except ValueError:
        return {"ok": False, "reason": "cap_invalid", "detail": "the header is not JSON"}
    if not isinstance(d, dict):
        return {"ok": False, "reason": "cap_invalid", "detail": "the header is not a JSON object"}
    objs = d.get(cap)
    if objs is None:
        return {"ok": False, "reason": "cap_missing", "detail": f"no grant of {cap}"}
    if not isinstance(objs, list) or len(objs) > CAP_OBJECTS_MAX or not all(isinstance(o, dict) for o in objs):
        return {"ok": False, "reason": "cap_invalid", "detail": f"{cap} is not a list of objects"}
    ops: list[str] = []
    for o in objs:
        got = o.get("ops", [])
        if not isinstance(got, list) or not all(isinstance(x, str) for x in got):
            return {"ok": False, "reason": "cap_invalid", "detail": "ops is not a list of names"}
        ops += [x for x in got if x not in ops]
    return {"ok": True, "ops": ops}


def cap_check(values: list, scope: str | None) -> Denial | None:
    """whois+cap: the header values (exactly one) must grant the op of `scope`. A route open to any pair (rotate, unpair: a pair may always manage
    itself) needs a readable grant of the capability but no particular op. The capability only narrows the token's scopes, never widens them."""
    from . import nodes
    if len(values) > 1:
        return _deny("cap_invalid", "more than one capability header")
    got = parse_caps(values[0] if values else None, settings.node_cap)
    if not got["ok"]:
        return _deny(got["reason"], got.get("detail", ""))
    if scope == nodes.SCOPE_ANY:
        return None
    op = OPS.get(str(scope))
    if op is None or op not in got["ops"]:
        return _deny("cap_op_missing", f"{op or scope} not granted")
    return None


# ---------------------------------------------------------------- the checks

def caller(client_host, server_host, headers) -> tuple[str | None, Denial | None]:
    """The calling device's tailnet address, or why there is none: (address, None) or (None, Denial); the rule is in the module text.
    `client_host` is request.client (already the Serve hop under uvicorn's default proxy headers), `server_host` the local address the connection
    arrived on (the ASGI `server`). No header is read unless `server_host` is loopback; then only X-Forwarded-For, and its value only while the peer
    is still loopback (uvicorn run with --no-proxy-headers)."""
    if not _loopback(server_host):
        return None, _deny("not_via_serve", "the connection did not arrive on a loopback address")
    vals = headers.getlist("x-forwarded-for") if hasattr(headers, "getlist") else [headers.get("x-forwarded-for")]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None, _deny("not_via_serve", "no X-Forwarded-For: the request reached the board directly")
    if _loopback(client_host):                       # uvicorn did not rewrite the peer: --no-proxy-headers, or every hop was loopback
        addr = ts.tailnet_ip(str(vals[-1]).split(",")[-1].strip())
    else:                                            # uvicorn's ProxyHeadersMiddleware already took the hop Serve added
        addr = ts.tailnet_ip(client_host)
    return (addr, None) if addr else (None, _deny("no_caller_address"))


def _tags_of(raw) -> list[str]:
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _self_tagged() -> bool | None:
    """Is this board's own device tagged (a tagged setup)? From the cached `tailscale status` the node card already reads; None when it cannot be
    read (bind_pair then refuses an untagged caller: unknown is never read as "not tagged")."""
    from . import nodes
    try:
        d = nodes._ts()
    except Exception:
        return None
    me = d.get("Self") if isinstance(d, dict) else None
    if not isinstance(me, dict):
        return None
    return bool(_tags_of(me.get("Tags")))


def tag_ok(tags) -> bool:
    return bool(set(_tags_of(tags)) & set(settings.node_tags))


def _record(den: Denial, peer_id: str = "", node_name: str | None = None, target: str | None = None, db=None) -> None:
    """The audit row of a refusal and the memory of the last one. Rows before the token resolved are capped like refused pair attempts."""
    global _last_denial
    from . import nodes
    with _lock:
        _last_denial = {"at": nodes.iso(nodes._now()), "reason": den.reason, "node": node_name or None}
    if peer_id or nodes.refusal_limiter.allow("whois")[0]:
        nodes.audit("in", peer_id, den.reason, False, (den.detail or den.message)[:nodes.AUDIT_DETAIL_MAX], node_name=node_name, target=target,
                    status="refused", db=db)


def last_denial() -> dict | None:
    with _lock:
        return dict(_last_denial) if _last_denial else None


def pre_check(client_host, server_host, headers, *, target: str | None = None, db=None) -> Denial | None:
    """Before the token is looked up: the mode must be able to run, and the call must have come through Serve from a tailnet caller (caller()).
    None in token mode, always."""
    if not enabled():
        return None
    r = readiness()
    if not r["ready"]:
        den = _deny(r["reason"], r["message"])
    else:
        den = caller(client_host, server_host, headers)[1]
        if den is None:
            return None
    _record(den, target=target, db=db)
    return den


def check_device(addr: str | None, row: dict | None) -> tuple[Denial | None, dict | None]:
    """The device behind `addr` must be the one `row` was paired from: (None, whois answer) or (Denial, None). No audit (the caller writes it)."""
    if addr is None:
        return _deny("no_caller_address"), None
    if not row or not row.get("peer_ts_id"):
        return _deny("not_bound"), None
    w = ts.whois(addr)
    if not w.get("ok"):
        why = w.get("reason")
        if why == "rate_limited":
            return _deny("whois_rate_limited", retry_after=w.get("retry_after") or 60), None
        if why in ("not_found", "not_tailnet"):
            return _deny("device_mismatch", "Tailscale does not know the calling address"), None
        return _deny("tailscale_unavailable", f"whois: {w.get('detail') or why}"), None
    if w["stable_id"] != row["peer_ts_id"]:
        return _deny("device_mismatch", f"called from {w.get('name') or 'another device'}"), None
    if _tags_of(row.get("peer_tags")) and not tag_ok(w.get("tags")):
        return _deny("tag_refused", "the paired device lost its allowed tag"), None
    return None, w


def check_call(client_host, server_host, headers, peer: dict, scope: str | None, *, target: str | None = None, db=None) -> Denial | None:
    """Every call with a node token, after the token resolved and its scope passed, in a whois mode: caller() again (no header is read for a
    connection that did not arrive on loopback), whois, the StableID against the pair row, and in whois+cap the capability header. Writes the audit
    row of a refusal. Run it in a thread: whois can start a process. None in token mode."""
    if not enabled():
        return None
    from . import nodes
    pid, name = str(peer.get("peer_id") or ""), peer.get("name")
    addr, den = caller(client_host, server_host, headers)
    if den is None:
        try:
            row = nodes._db(db).node_pair_get(pid)
        except Exception:
            row = None
        den, _w = check_device(addr, row)
        if den is None and mode() == WHOIS_CAP:
            vals = headers.getlist(CAP_HEADER) if hasattr(headers, "getlist") else [v for v in [headers.get(CAP_HEADER)] if v is not None]
            den = cap_check(vals, scope)
    if den is not None:
        _record(den, pid, name, target, db=db)
    return den


def bind_pair(client_host, server_host, headers) -> dict | None:
    """At pairing, in a whois mode (after the code was found right; and at the pair confirm, for the reverse pair of a both-ways pairing): who the
    calling device is, found by caller(). Returns {peer_ts_id, peer_tags} for the pair row, or raises nodes.PairError with a plain reason: Tailscale
    could not answer, or this board cannot read its own status to tell whether an untagged caller may pair (503); the device is unknown or (in a
    tagged setup: this board tagged, or the caller tagged) carries no tag from CCBOARD_NODE_TAGS (403). None in token mode."""
    from . import nodes
    if not enabled():
        return None
    addr, den = caller(client_host, server_host, headers)
    if den is not None and den.reason == "not_via_serve":
        raise nodes.PairError("device_refused", message="A pairing must come through tailscale serve while the device check is on; nothing was paired.")
    if addr is None:
        raise nodes.PairError("device_refused", message="The calling address is not a tailnet address, so the device cannot be checked; nothing was paired.")
    w = ts.whois(addr)
    if not w.get("ok"):
        why = w.get("reason")
        if why == "rate_limited":
            raise nodes.PairError("rate_limited", retry_after=w.get("retry_after") or 60)
        if why in ("not_found", "not_tailnet"):
            raise nodes.PairError("device_refused", message="Tailscale does not know the calling device, so it cannot be bound; nothing was paired.")
        raise nodes.PairError("tailscale_unavailable")
    tags = w.get("tags") or []
    tagged_setup = True if tags else _self_tagged()
    if tagged_setup is None:
        raise nodes.PairError("tailscale_unavailable", message="This node could not read its own Tailscale status, so it cannot tell whether an untagged "
                                                               "device may pair; nothing was paired. Try again once Tailscale answers there.")
    if tagged_setup and not tag_ok(tags):
        allowed = ", ".join(settings.node_tags) or "none"
        raise nodes.PairError("device_refused", message=f"The calling device carries no tag from CCBOARD_NODE_TAGS ({allowed}); nothing was paired.")
    return {"peer_ts_id": w["stable_id"], "peer_tags": json.dumps(tags[:16])}


def bind_reverse(client_host, server_host, headers) -> dict | None:
    """At the pair confirm of a both-ways pairing this board started: the device of the other board (the one that will use the reverse token), by the
    same rule as bind_pair. None when the mode is off or cannot run now (the reverse pair then stays unbound and its calls are refused as not_bound);
    raises nodes.PairError like bind_pair."""
    if not enabled() or not readiness()["ready"]:
        return None
    return bind_pair(client_host, server_host, headers)


# ---------------------------------------------------------------- what the owner applies (text only)

def cap_name() -> str:
    return settings.node_cap or PLACEHOLDER_CAP


def serve_command() -> str:
    """The Serve command that passes the capability header on to the board (v1.92 and newer). Shown, never run."""
    return f"tailscale serve --bg --https={settings.ccboard_https_port} --accept-app-caps={cap_name()} http://127.0.0.1:{settings.port}"


def grant_snippet(kind: str) -> str:
    """The tailnet policy grant for `kind` 'user' (user-owned devices: src the owner's login, dst autogroup:self) or 'tag' (src and dst the node tag),
    with the network line next to the capability (a grant with only `app` gives no connectivity). Placeholder login and domain only. Shown, never applied."""
    tag = settings.node_tags[0] if settings.node_tags else "tag:ccboard"
    src, dst = ([PLACEHOLDER_LOGIN], ["autogroup:self"]) if kind == "user" else ([tag], [tag])
    grant = {"src": src, "dst": dst, "ip": [f"tcp:{settings.ccboard_https_port}"], "app": {cap_name(): [{"ops": list(OPS.values())}]}}
    return json.dumps({"grants": [grant]}, indent=2)


# ---------------------------------------------------------------- Settings and Doctor

def _host_reading() -> dict:
    """What this host can do now (runs reads only): the client version, whether it is new enough for the capability header, whether Serve passes the
    capability, and whether whois answers for this device's own address. None for each value that could not be read (shown as unknown, never as fine)."""
    from . import nodes
    v = ts.version()
    serve_cap = ts.serve_accepts_caps(ts.serve_status(), settings.ccboard_https_port, settings.node_cap) if settings.node_cap else None
    whois_ok = None
    try:
        ips = nodes._self(nodes._ts()).get("TailscaleIPs") or []
        mine = next((ip for ip in ips if ts.tailnet_ip(ip)), None)
        if mine:
            w = ts.whois(mine)
            whois_ok = True if w.get("ok") else (False if w.get("reason") in ("unavailable", "not_found") else None)
    except Exception:
        whois_ok = None
    return {"checked": True, "version": v, "version_ok": ts.caps_supported(v), "serve_cap": serve_cap, "whois": whois_ok}


def banner(r: dict, host: dict) -> str | None:
    """One plain sentence when the configured mode is stronger than what the host can do now, else None."""
    if r["mode"] == TOKEN:
        return None
    if not r["ready"]:
        return r["message"]
    if not host.get("checked"):
        return None
    if host.get("whois") is False:
        return "Tailscale on this board does not answer who is calling, so every call from a paired node is refused"
    if r["mode"] == WHOIS_CAP and host.get("version_ok") is False:
        return f"Tailscale {host.get('version')} is older than 1.92 and cannot pass the capability, so every call from a paired node is refused"
    if r["mode"] == WHOIS_CAP and host.get("serve_cap") is False:
        return "Serve does not pass the capability to the board (no --accept-app-caps), so every call from a paired node is refused"
    labels = {"whois": "whois", "version_ok": "the Tailscale version", "serve_cap": "the Serve flag"}
    unknown = [labels[k] for k in labels if host.get(k) is None and (k == "whois" or r["mode"] == WHOIS_CAP)]
    if unknown:
        return f"Could not confirm {', '.join(unknown)} on this host (to verify), so calls from paired nodes may be refused"
    return None


def settings_view(check: bool = False) -> dict:
    """What Settings > Nodes shows (GET /api/nodes/auth). In token mode Tailscale is asked nothing unless `check` (the page's Check button)."""
    r = readiness()
    host = _host_reading() if (check or r["mode"] != TOKEN) else {"checked": False, "version": None, "version_ok": None, "serve_cap": None, "whois": None}
    return {
        "mode": r["mode"], "modes": list(NODE_AUTH_MODES), "ready": r["ready"], "reason": r["reason"], "message": r["message"],
        "bind": bind_state(), "cap": {"name": settings.node_cap or None, "set": bool(settings.node_cap), "bad": bool(settings.node_cap_bad)},
        "tags": list(settings.node_tags), "host": host, "banner": banner(r, host), "last_denial": last_denial(),
        "snippet": {"user": grant_snippet("user"), "tag": grant_snippet("tag"), "serve": serve_command(), "cap": cap_name(),
                    "placeholder": not settings.node_cap, "version_min": "1.92", "version_cmd": "tailscale version"},
    }


def doctor_bind() -> tuple[str, str, str | None]:
    """(status, detail, fix text) of the board-bind row. A wider bind is a fail in a whois mode and a warning in token mode (rate limits by address could
    be fooled); an unknown bind is a skip in token mode and a warning in a whois mode."""
    b = bind_state(fresh=True)
    on = enabled()
    fix = "Start the board with --host 127.0.0.1 (the installers' units, plist and container all do) and reach it through tailscale serve"
    if b["ok"] is True:
        return "pass", b["reason"], None
    if b["ok"] is False:
        return ("fail" if on else "warn"), b["reason"] + ("; the whois modes refuse to switch on" if on else "; the whois modes would refuse to switch on"), fix
    return ("warn" if on else "skip"), "unknown: " + b["reason"], (fix if on else None)


def doctor_auth() -> tuple[str, str, str | None]:
    """(status, detail, fix text) of the node-auth row: the mode, the Tailscale version, whether Serve passes the capability, the last denial. Read only.
    Token mode is a skip and asks Tailscale nothing."""
    r = readiness()
    if r["mode"] == TOKEN:
        return "skip", "CCBOARD_NODE_AUTH=token (the default): no extra device check; node signatures still apply; whois and whois+cap add the device check", None
    if not r["ready"]:
        fix = {"bad_mode": "Set CCBOARD_NODE_AUTH to token, whois or whois+cap in the settings file, then restart the board",
               "board_bind": "Start the board with --host 127.0.0.1 and reach it through tailscale serve, then restart it",
               "proxy_trust": "Remove FORWARDED_ALLOW_IPS and --forwarded-allow-ips from the board's start (uvicorn's default believes loopback only, "
                              "which is what tailscale serve needs), then restart it",
               "no_cap_name": "Set CCBOARD_NODE_CAP=<a domain you control>/cap/ccboard in the settings file, then restart the board"}[r["reason"]]
        return "fail", f"CCBOARD_NODE_AUTH={r['mode']}: {r['message']}", fix
    host = _host_reading()
    parts = [f"CCBOARD_NODE_AUTH={r['mode']}", f"Tailscale {host['version'] or 'version unknown'}"]
    status, fix = "pass", None
    if host["whois"] is False:
        status, fix = "fail", "Check that Tailscale runs and that this user may read it (tailscale whois on this device's own address must answer)"
        parts.append("whois does not answer")
    if r["mode"] == WHOIS_CAP:
        if host["version_ok"] is False:
            status, fix = "fail", "Update Tailscale to 1.92 or newer"
            parts.append("older than 1.92 (no capability header)")
        sc = host["serve_cap"]
        parts.append("Serve passes the capability" if sc else ("Serve does not pass the capability" if sc is False else "Serve flag unknown (to verify)"))
        if sc is False and status == "pass":
            status, fix = "fail", f"Run once: {serve_command()}"
        elif sc is None and status == "pass":
            status = "warn"
    if host["version"] is None and status == "pass":
        status = "warn"
    b = bind_state()
    if b["ok"] is None:
        parts.append("bind unknown")
        status = "warn" if status == "pass" else status
    ld = last_denial()
    parts.append(f"last refusal: {ld['reason']} at {ld['at']}" if ld else "no refusal since the start")
    return status, "; ".join(parts), fix
