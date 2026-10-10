"""One board as a node on a tailnet (the nodes epic, issues #131, #133 and #137): who it is, the small card other boards read, and how a thing on
another node is named in text.

This module talks to no other node. It reads this board's own pieces (the Tailscale client, the agents, the saved accounts, the box health
and the task lanes) and puts the safe part of them in one shape. A board with no paired node runs nothing here on its own: every function is
called by a request or by `build_state`, and no thread, timer or outbound request starts.

Identity
  node_id()      the id of this board, kept in `<data dir>/node-id` (0600, written atomically) and never changed by the board itself. The first
                 read writes `ts:<StableNodeID>` when the Tailscale client answers (`Self.ID`), else `n_` and 16 random hex characters. It is
                 never derived from the host name. If the file says `ts:X` and Tailscale now reports another `Self.ID` (the device was
                 registered again) the file wins and the doctor warns: every pair must then be made again.
  display_name() CCBOARD_NODE_NAME, else the short MagicDNS name, else `hostname -s`, cut to the node-name rule
                 (`^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$`, as install.sh). The name can change; the id cannot.

The card (GET /api/node, and `state.node` without `agents` and `accounts`)
  Every field that cannot be read is null, never an error and never 0. It holds no secret, no prompt, no e-mail address and no path. The
  credentials files are never opened here: account windows come from the board's own kv records. `now` is this node's UTC time, for skew.
  `subscription_key` is the first 12 hex characters of a SHA-256 over the provider's account id (Claude: the account uuid; Codex: the
  account id), null when the board holds none. Two nodes on one login show the same key, so a rate-limit window is counted once. Which stored
  field is the same on two devices is to verify in the two-node check (#155).

Names of things on a node (issue #137), the one grammar the Python and the JavaScript helpers (`Ref` in app/static/nodes.js) both follow
  handle        `^[a-z0-9][a-z0-9-]{0,30}$`. `local` is the board in front of you: it is never written into a URL or a ref.
  session ref   `<handle>/<tmux name>` in text, for example `box/shop--api--t-fix`; the tmux part must be a ccboard session name
                (app/tmux.py split_name). tmux, ttyd and hook names are never changed: the handle is added around them.
  task ref      `<handle>:<id>`, the id digits only, 1 to 12 of them (`box:42`).
  URL forms     `#/n/<handle>` (the node page), `#/n/<handle>/s/<tmux>` (a session peek), `#/n/<handle>/t/<id>` (a task). Local items keep
                `#/s/<tmux>` and `#/t/<id>`, the notification link `{public_url}/#/s/<tmux>` and the old `#s=<tmux>` link unchanged.
  A bare tmux name is a local session. The JavaScript side (`Ref.parse`) takes any [A-Za-z0-9_-] name as the tmux part; this side takes only a
  ccboard session name (three parts joined by `--`, as the relay and the MCP tools need), so what this side accepts, that side accepts too. Addresses use the handle, not the display name, so a peer renaming itself breaks no bookmark; the
  node id is the true key in the registry. Relay routes use path parameters, so refs appear only in UI hashes and the MCP tools.

Pairing (issue #135, phase P3). The contract the routes (app/main.py) and the Settings page (app/static) are written against
  Two stores, never one
    incoming  the pairs ANOTHER board holds with this one, because this board accepted its code. Table `node_pairs`; only the SHA-256 digest of the
              token is kept. `mint_token`, `verify_token`, `rotate_token` and `revoke` work here. `verify_token` is what the auth middleware calls.
    outgoing  the tokens this board RECEIVED when it paired with another board. `<data dir>/node-tokens.json` (0600, written whole and atomically,
              a failed write keeps the old file). Only `PeerClient` reads a token out of it; no public function returns one. `save_outgoing`,
              `drop_outgoing`, `has_outgoing`.
    The pairing code lives in kv `node_pair_code` as a digest, an expiry, the scopes and an attempt counter. No token and no code is ever stored.
  One key, `peer_id`: `p_` and 16 hex characters, minted here, the key of a `node_pairs` row and of a registry row. The routes' `{peer}` is a peer_id; the
    lookup functions also accept a registry handle. Registry rows (kv `node_peers`, direction `out`) also carry `handle`, because registry() and resolve()
    above only see rows with a valid handle. `peers()` merges both stores; a record is
    {peer_id, node_id, name, url, scopes, created_at, last_seen, direction 'in' | 'out'} plus, for `out`: handle, last_error, needs_repair, legacy
    and, for `in`: callback_unverified, expires_at, rotated_at. It never holds a token or a digest.
  Scopes: SCOPES = read, tasks, sessions, permissions; a pair starts with read and tasks. NODE_ROUTES is the closed map (method, path) -> scope (or
    SCOPE_ANY) of what a node token may call; route_scope(method, path) answers it (None = not listed = 403). The walk test in tests/test_nodes_auth.py
    goes over every /api route.
  Seams (all optional keyword arguments are compatible with the fixed interface): every function takes `db=None` (the running board's `main.db`, as
    resolve() does); `_now()` is the clock (code expiry, the 60 s rotation window, the 90 day audit prune); `peer_transport`, when set, replaces the
    HTTPS connection of every outgoing call (`transport(target, method, path, headers, body, timeout) -> (status, headers, body)`).
  Outgoing calls: PeerClient(record).get(path) / .post(path, body) check the address rule again at the call (check_peer_url), connect to the validated
    address, send `Authorization: Bearer <token>`, `X-CCBoard: 1` and `X-CCBoard-Node: <this node id>`, never follow a redirect, read at most RESP_MAX.
    PairError(reason) (reasons none, wrong, expired, burned, rate_limited, bad_request, bad_url, callback_mismatch) carries .status and .payload();
    PeerError(reason) is a failed outgoing call.
  The handshake in one call each side: handle_pair(body, caller) on the accepting board (the route answers its dict), add_node(url, code, handle,
    both_ways) on the calling board, rotate_outgoing(peer) / remove_node(peer) on the calling board, rotate_token(peer_id) and unpair_incoming(peer_id)
    for the two token-authenticated routes. audit(direction, peer_id, action, ok, detail) writes a row with nothing secret in it.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import platform as _pf
import re
import secrets
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from . import platform as plat
from . import tailscale as ts
from . import tmux
from .config import settings
from .db import iso

log =logging.getLogger("ccboard.nodes")

APP = "ccboard"
API_VERSION = 1
CAPABILITIES = ("state", "tasks", "sessions", "stream")     # each later issue adds its own word

ID_FILE = "node-id"
ID_RE = re.compile(r"(?:ts:[A-Za-z0-9._-]{1,64}|n_[0-9a-f]{16})")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,40}")      # install.sh and app/config.py's node-name rule
HANDLE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
TASK_ID_RE = re.compile(r"[0-9]{1,12}")
LOCAL = "local"
LANE_MODES = ("worktree", "attached")                         # a task of these modes owns a lane session; mode 'session' is a prompt handed to one
KV_PEERS = "node_peers"                                       # the registry of nodes this board calls (rows with a handle); empty or missing = no paired node
SUB_KEY_LEN = 12
LABEL_MAX = 60
TS_TIMEOUT = 3.0                                              # seconds a request waits for `tailscale status --json`
TS_TTL = 120.0                                                # a reading is reused this long (a failed one for TS_TTL_FAIL)
TS_TTL_FAIL = 60.0
SUPPORT_PLATFORMS = ("Linux box", "Linux container", "Mac", "WSL2", "Native Windows")   # the rows of the README table "Several devices" (issue #154); tests/test_nodes_platforms.py keeps the two equal
WINDOWS_SIDE = "Tailscale runs on the Windows side: type the other node's address in Pair a node"   # the reason discovery gives on WSL2 with no Tailscale client it may use


class RefError(ValueError):
    """Text that is not a node ref."""


# ---------------------------------------------------------------- Tailscale (one seam, so tests never run the real command)

def windows_side() -> bool:
    """WSL2 only (False anywhere else, with no command run): is Tailscale out of this board's reach, on the Windows side? True when the distro has no
    `tailscale`, or when the only one is `tailscale.exe` through Windows interop and CCBOARD_TAILSCALE_PLACEMENT is not `host` (the setting that says
    Tailscale runs on Windows; it is never implied, so interop is off by default and to verify on a real machine, issue #130). Then the node id is `n_`,
    the card has no Tailscale part and discovery gives WINDOWS_SIDE as its reason; the manual Add node form is always there."""
    if not plat.is_wsl():
        return False
    try:
        cli = ts.find_cli()
    except Exception:
        return False
    exe = cli.exe if cli is not None else ""
    if exe.lower().endswith(".exe"):
        return plat.tailscale_placement(implied=False) != "host"
    return not ts._executable(exe)


def _ts_status() -> dict | None:
    """`tailscale status --json` parsed, None when it cannot be read; a short timeout so a request never waits long. Never raises."""
    if windows_side():
        return None
    try:
        r = ts.call(["status", "--json"], timeout=TS_TIMEOUT)
        if r.rc != 0:
            return None
        d = json.loads(r.out or "null")
    except Exception:
        return None
    return d if isinstance(d, dict) and d else None


_lock = threading.Lock()                                      # guards the three caches below, held only for a dict access
_ts_busy = threading.Lock()                                   # one `tailscale status` at a time
_id_lock = threading.Lock()                                   # the first read of the id file (and its write) happens once
_ts_cache: tuple[float, dict | None] | None = None            # (monotonic time, reading)
_id_cache: dict[str, str] = {}                               # id file path -> the id read from it
_warned: set[str] = set()                                    # log-once keys


def reset() -> None:
    """Forget every in-memory reading (the Tailscale answer, the id read, the hello limiter, what was logged once). Tests call it."""
    global _ts_cache
    with _lock:
        _ts_cache = None
        _id_cache.clear()
        _warned.clear()
    hello_limiter.clear()
    for lim in (pair_limiter, refusal_limiter, node_read_limiter, node_write_limiter, relay_read_limiter, relay_write_limiter, confirm_limiter):
        lim.clear()
    with _pending_lock:
        _pending.clear()
    global _last_prune
    _last_prune = None


def _log_once(key: str, msg: str, *args) -> None:
    with _lock:
        if key in _warned:
            return
        _warned.add(key)
    log.warning(msg, *args)


def _ts() -> dict | None:
    """The Tailscale reading, cached; a reader that is already busy gets the old answer (or None) instead of waiting."""
    global _ts_cache
    now = time.monotonic()
    with _lock:
        hit = _ts_cache
        if hit is not None and now - hit[0] < (TS_TTL if hit[1] is not None else TS_TTL_FAIL):
            return hit[1]
    if not _ts_busy.acquire(blocking=False):
        return hit[1] if hit else None
    try:
        d = _ts_status()
        with _lock:
            _ts_cache = (time.monotonic(), d)
        return d
    finally:
        _ts_busy.release()


def _self(d: dict | None) -> dict:
    s = d.get("Self") if isinstance(d, dict) else None
    return s if isinstance(s, dict) else {}


def _stable_id(d: dict | None) -> str | None:
    v = _self(d).get("ID")
    return v if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", v) else None


# ---------------------------------------------------------------- the node id

def id_path() -> Path:
    return Path(settings.data_dir) / ID_FILE


def _read_id_file(p: Path) -> str | None:
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text if ID_RE.fullmatch(text) else ""         # "" = a file that is there but is not an id


def _write_id_file(p: Path, value: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def node_id() -> str:
    """This board's id: `ts:<StableNodeID>` or `n_` and 16 hex, from `<data dir>/node-id`; written once, never changed by the board. If the file
    cannot be written the id still answers for this run (a new one next start is the price of a read-only data dir)."""
    p = id_path()
    key = str(p)
    with _lock:
        if key in _id_cache:
            return _id_cache[key]
    with _id_lock:
        with _lock:
            if key in _id_cache:
                return _id_cache[key]
        cur = _read_id_file(p)
        if cur:
            with _lock:
                _id_cache[key] = cur
            return cur
        if cur == "":
            _log_once("id-corrupt:" + key, "the node id file is not an id; replacing it")
        sid = _stable_id(_ts()) or _stable_id(_ts_status())     # the first write asks again, directly: a reader that was busy must not downgrade the id for good
        new = f"ts:{sid}" if sid else "n_" + secrets.token_hex(8)
        try:
            _write_id_file(p, new)
        except OSError as e:
            _log_once("id-write:" + key, "could not write the node id file: %s", e.__class__.__name__)
        with _lock:
            _id_cache[key] = new
        return new


def id_report() -> dict:
    """What the doctor needs: {id, kind 'tailscale' | 'random', ts_id (Self.ID now, or None), drifted (the file says ts: and Tailscale reports another id)}."""
    nid = node_id()
    now = _stable_id(_ts())
    kind = "tailscale" if nid.startswith("ts:") else "random"
    return {"id": nid, "kind": kind, "ts_id": now, "drifted": bool(kind == "tailscale" and now and nid != f"ts:{now}")}


# ---------------------------------------------------------------- the name and the address

def _sanitize_name(raw: str) -> str | None:
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", str(raw or "").strip()).strip("-_")[:41].rstrip("-_")
    return s if s and NAME_RE.fullmatch(s) else None


def dns_name(d: dict | None) -> str | None:
    """Self.DNSName of a Tailscale reading without its trailing dot, None when there is none or it is not a host name."""
    n = _self(d).get("DNSName")
    n = n.rstrip(".") if isinstance(n, str) else ""
    return n if n and re.fullmatch(r"[A-Za-z0-9.-]{1,253}", n) else None


def display_name() -> str:
    """CCBOARD_NODE_NAME, else the short MagicDNS name, else `hostname -s`; always a valid node name."""
    dns = dns_name(_ts()) if not settings.node_name else None
    for raw in (settings.node_name, dns.split(".")[0] if dns else "", socket.gethostname().split(".")[0]):
        name = _sanitize_name(raw)
        if name:
            return name
    return "node"


def public_url() -> str | None:
    """CCBOARD_PUBLIC_URL, else https://<MagicDNS name>[:<board HTTPS port>], else None."""
    if settings.public_url:
        return settings.public_url.rstrip("/")
    dns = dns_name(_ts())
    if not dns:
        return None
    port = int(getattr(settings, "ccboard_https_port", 443) or 443)
    return f"https://{dns}" + ("" if port == 443 else f":{port}")


# ---------------------------------------------------------------- the pieces of the card

def _num(v, nd: int = 1):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) == float("inf"):
        return None
    return round(float(v), nd)


def _int(v):
    return int(v) if isinstance(v, int) and not isinstance(v, bool) else None


def load_view(snap: dict | None) -> dict:
    snap = snap if isinstance(snap, dict) else {}
    mem = snap.get("mem") if isinstance(snap.get("mem"), dict) else {}
    disk = snap.get("disk") if isinstance(snap.get("disk"), dict) else {}
    return {"load1": _num(snap.get("load1"), 2), "cores": _int(snap.get("cores")), "cpu_pct": _num(snap.get("cpu_pct")),
            "mem_pct": _num(mem.get("pct")), "disk_pct": _num(disk.get("pct"))}


def session_counts(sessions) -> dict:
    """{live, working, needs_you} over session dicts (state and needs_attention); all null when the sessions could not be read."""
    if sessions is None:
        return {"live": None, "working": None, "needs_you": None}
    rows = [s for s in sessions if isinstance(s, dict)]
    return {"live": len(rows), "working": sum(1 for s in rows if s.get("state") == "working"),
            "needs_you": sum(1 for s in rows if s.get("needs_attention"))}


def lanes_view(db) -> dict:
    """{cap, running, free}. cap is CCBOARD_NODE_LANES (0 = no advisory cap, then free is null); running counts tasks in phase `running` that
    own a lane session. The cap is advice for routing and for a warning on a hand dispatch; nothing is ever refused because of it."""
    cap = int(getattr(settings, "node_lanes", 3))
    try:
        rows = db.tasks_by_phase(("running",))
        running = sum(1 for t in rows if (t.get("mode") or "worktree") in LANE_MODES and (t.get("session_row") is not None or t.get("tmux_name")))
    except Exception as e:
        log.debug("lane count failed: %s", e.__class__.__name__)
        return {"cap": cap, "running": None, "free": None}
    return {"cap": cap, "running": running, "free": max(0, cap - running) if cap > 0 else None}


def lane_warning(db) -> dict | None:
    """After a hand dispatch of a lane: {cap, running} when more lanes run than the cap, else None. Advice only."""
    v = lanes_view(db)
    if v["cap"] > 0 and v["running"] is not None and v["running"] > v["cap"]:
        return {"cap": v["cap"], "running": v["running"]}
    return None


def subscription_key(provider_id) -> str | None:
    """A short one-way hash of a provider account id, None for no id. The same login on two nodes gives the same key."""
    if not isinstance(provider_id, str) or not provider_id.strip():
        return None
    return hashlib.sha256(("ccboard-subscription:" + provider_id.strip()).encode("utf-8")).hexdigest()[:SUB_KEY_LEN]


def _label(v) -> str | None:
    """A person's own account label; one that looks like an e-mail address is dropped (the card never carries one)."""
    if not isinstance(v, str):
        return None
    t = " ".join(v.split())[:LABEL_MAX]
    return None if not t or "@" in t else t


def _window(pct, resets_at, backoff_until) -> dict:
    if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool) and resets_at == resets_at:
        resets_at = int(resets_at)
    elif not isinstance(resets_at, str):
        resets_at = None
    return {"pct": _num(pct), "resets_at": resets_at,
            "backoff_until": backoff_until if isinstance(backoff_until, str) else None, "known": pct is not None}


def _limited(w: dict) -> bool:
    return bool(w["backoff_until"]) or (w["pct"] is not None and w["pct"] >= 100)


def _is_uuid(v) -> bool:
    try:
        return isinstance(v, str) and str(uuid.UUID(v)) == v.lower()
    except ValueError:
        return False


def accounts_view(db) -> dict:
    """{supported, items:[{agent, label, current, subscription_key, window{pct, resets_at, backoff_until, known}, limited}]}. `supported` is
    whether this system can keep and switch saved Claude logins (Linux only); the items are what the board has seen. Built from the kv records and
    the window readings only; a credentials file is never opened and nothing here reads one."""
    from . import account_store, accounts, codex_accounts, scheduler
    out: dict = {"supported": None, "items": []}
    try:
        out["supported"] = bool(account_store.supported())
    except Exception:
        pass
    items = []
    try:
        q = scheduler.quota_state(db, "claude")
        for r in accounts.view(db, full=True).get("list") or []:
            cur = bool(r.get("current"))
            if cur and q.get("known"):
                w = _window(q["pct"], q.get("resets_at"), q.get("backoff_until"))
            else:
                w = _window(r.get("rl_5h"), r.get("resets_5h"), q.get("backoff_until") if cur else None)
            key, org = r.get("key"), r.get("org_id")
            sub = subscription_key(key) if _is_uuid(key) and key != org else None
            items.append({"agent": "claude", "label": _label(r.get("label")), "current": cur, "subscription_key": sub, "window": w, "limited": _limited(w)})
    except Exception as e:
        log.debug("claude accounts for the card failed: %s", e.__class__.__name__)
    try:
        q = scheduler.quota_state(db, "codex")
        accts, cur_key = codex_accounts._load(db), codex_accounts.current(db)
        for key, rec in sorted(accts.items(), key=lambda kv: (kv[0] != cur_key, str(kv[1].get("added_at") or ""), kv[0])):
            cur = key == cur_key
            w = _window(q.get("pct"), q.get("resets_at"), q.get("backoff_until")) if cur else _window(None, None, None)
            items.append({"agent": "codex", "label": _label(rec.get("label")), "current": cur, "subscription_key": subscription_key(rec.get("account_id")),
                          "window": w, "limited": _limited(w)})
    except Exception as e:
        log.debug("codex accounts for the card failed: %s", e.__class__.__name__)
    out["items"] = items
    return out


def agents_view(db) -> list[dict]:
    """[{id, installed, version, logged_in, hooks, login_problem}] built field by field from agents.status_all(), which also holds an e-mail
    address that never leaves this board."""
    from . import agents, login_problem
    try:
        problem = login_problem.get(db)
    except Exception:
        problem = None
    out = []
    try:
        status = agents.status_all()
    except Exception as e:
        log.debug("agent status for the card failed: %s", e.__class__.__name__)
        return out
    for name, st in status.items():
        st = st if isinstance(st, dict) else {}
        hooks = st.get("hooks") if isinstance(st.get("hooks"), dict) else {}
        ver = st.get("version")
        out.append({"id": name, "installed": bool(st.get("installed")), "version": ver if isinstance(ver, str) else None,
                    "logged_in": bool(st.get("loggedIn")), "hooks": bool(hooks.get("installed")),
                    "login_problem": bool(problem and problem.get("agent") == name)})
    return out


def _os_view(d: dict | None) -> dict:
    tos = _self(d).get("OS")
    out = {"system": _pf.system() or None, "release": _pf.release() or None, "tailscale_os": tos if isinstance(tos, str) and tos else None}
    if plat.is_wsl():
        out["wsl"] = True          # only ever added on WSL2: a Linux box's card is exactly what it was. Its health numbers describe the distro and its VM, not Windows
    return out


def tailscale_view(d: dict | None) -> dict:
    s = _self(d)
    if not s:
        return {"dns_name": None, "tags": None, "user_owned": None}
    tags = [t for t in (s.get("Tags") or []) if isinstance(t, str)][:16]
    return {"dns_name": dns_name(d), "tags": tags, "user_owned": not tags}


def card(db, *, health_snap: dict | None = None, sessions=None, full: bool = True) -> dict:
    """The node card. `health_snap` is health.snapshot() (the caller passes the one it already took), `sessions` an iterable of session dicts
    (None = unreadable). `full` False leaves `agents` and `accounts` out and adds `handle: "local"`: that is `state.node`."""
    d = _ts()
    out = {"app": APP, "api": API_VERSION, "node_id": node_id(), "name": display_name(), "url": public_url(),
           "version": settings.image_version or None, "os": _os_view(d), "runtime": settings.runtime or None,
           "now": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
    if full:
        out["agents"] = agents_view(db)
        out["accounts"] = accounts_view(db)
    out.update({"lanes": lanes_view(db), "load": load_view(health_snap), "sessions": session_counts(sessions),
                "capabilities": list(CAPABILITIES), "tailscale": tailscale_view(d)})
    if not full:
        out["handle"] = LOCAL
    return out


def hello() -> dict:
    """The unauthenticated answer: exactly these three keys and nothing else (not the version, not the name)."""
    return {"app": APP, "api": API_VERSION, "node_id": node_id()}


# ---------------------------------------------------------------- the hello rate limit and the caller's address

HELLO_LIMIT = 30                # requests ...
HELLO_WINDOW = 60.0             # ... per source address and minute
HELLO_MAX_SOURCES = 2048


def _loopback(host) -> bool:
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def caller_addr(client_host, forwarded_for=None) -> str:
    """The address a request came from, for rate limiting. `tailscale serve` connects from loopback and puts the caller's tailnet address in
    X-Forwarded-For, so that header is believed only when the connection itself is from loopback (from anywhere else it could be forged); then
    the last entry is used, the one the nearest proxy appended. An unreadable or missing address is "unknown", one shared bucket."""
    peer = str(client_host) if client_host else ""
    if peer and _loopback(peer) and forwarded_for:
        last = str(forwarded_for).split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(last.split("%")[0]))
        except ValueError:
            pass
    try:
        return str(ipaddress.ip_address(peer.split("%")[0])) if peer else "unknown"
    except ValueError:
        return peer[:64] if peer else "unknown"


class _Limiter:
    """A sliding window per source: `allow()` is (True, 0) or (False, seconds until a slot frees up)."""

    def __init__(self, limit: int, window: float, max_sources: int):
        self.limit, self.window, self.max_sources = limit, window, max_sources
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()

    def allow(self, source: str, now: float | None = None) -> tuple[bool, int]:
        t = time.monotonic() if now is None else now
        with self._lock:
            hits = [h for h in self._hits.get(source, ()) if t - h < self.window]
            if len(hits) >= self.limit:
                self._hits[source] = hits
                return False, max(1, int(self.window - (t - hits[0])) + 1)
            hits.append(t)
            self._hits[source] = hits
            if len(self._hits) > self.max_sources:
                for k in [k for k, v in self._hits.items() if not v or t - v[-1] >= self.window]:
                    self._hits.pop(k, None)
                while len(self._hits) > self.max_sources:
                    self._hits.pop(min(self._hits, key=lambda k: self._hits[k][-1]), None)
            return True, 0


hello_limiter = _Limiter(HELLO_LIMIT, HELLO_WINDOW, HELLO_MAX_SOURCES)


# ---------------------------------------------------------------- refs (issue #137)

class Ref(NamedTuple):
    kind: str                 # 'session' | 'task'
    handle: str | None        # None = this board
    rest: str                 # the tmux name, or the task id as digits


def valid_handle(h) -> bool:
    return isinstance(h, str) and bool(HANDLE_RE.fullmatch(h)) and h != LOCAL


def parse(text) -> Ref:
    """Parse a ref. `box/shop--api--t-fix` (a session on node box), `box:42` (a task on box), or a bare tmux name (a session on this board).
    Raises RefError for anything outside the grammar: an uppercase or over-long handle, `local/...` (never written), a slash or `..` inside the
    tmux part, a tmux part that is not a ccboard session name, a task id that is not 1 to 12 digits, an empty part."""
    if not isinstance(text, str) or not text or len(text) > 200:
        raise RefError("not a node ref")
    if "/" in text:
        handle, rest = text.split("/", 1)
        kind = "session"
    elif ":" in text:
        handle, rest = text.split(":", 1)
        kind = "task"
    else:
        handle, rest, kind = None, text, "session"
    if handle is not None and not valid_handle(handle):
        raise RefError("the node handle is not valid")
    if kind == "task":
        if not TASK_ID_RE.fullmatch(rest):
            raise RefError("a task id is digits only")
    else:
        try:
            tmux.split_name(rest)
        except ValueError:
            raise RefError("the session name is not a ccboard session name") from None
    return Ref(kind, handle, rest)


def parse_ref(text) -> tuple[str | None, str]:
    """`parse(text)` as (handle | None, rest). RefError (a ValueError) when the text is outside the grammar."""
    r = parse(text)
    return r.handle, r.rest


def format_ref(handle: str | None, rest: str, kind: str = "session") -> str:
    """The text form of a ref, checked by parsing it back."""
    text = rest if handle is None else (f"{handle}/{rest}" if kind == "session" else f"{handle}:{rest}")
    parse(text)
    return text


def registry(db) -> list[dict]:
    """The paired nodes this board knows (kv `node_peers`, written from issue #135 on): rows with a `handle`. [] when there are none, which is
    every single board; reading it writes nothing."""
    try:
        rec = db.kv_get(KV_PEERS)
    except Exception:
        return []
    v = rec.get("value") if isinstance(rec, dict) and "value" in rec else rec
    if isinstance(v, dict):
        v = list(v.values())
    return [r for r in v if isinstance(r, dict) and valid_handle(r.get("handle"))] if isinstance(v, list) else []


def resolve(handle, db=None):
    """The registry row of a handle. None for `local` (the board in front of you). projects.NotFound (404) for an unknown handle and
    projects.BadRequest (400) for text that is not a handle."""
    from . import projects
    if handle == LOCAL:
        return None
    if not valid_handle(handle):
        raise projects.BadRequest("not a node handle")
    if db is None:
        from . import main                      # the running board's database
        db = main.db
    for row in registry(db):
        if row.get("handle") == handle:
            return row
    raise projects.NotFound("unknown node")


# ---------------------------------------------------------------- the address rule for a peer (issues #134 and #135)

TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")           # Tailscale's CGNAT range
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")     # Tailscale's IPv6 range
_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class PeerUrlError(ValueError):
    """A peer address the rule refuses; str() is the reason in words. `unresolved` is True when the only fault is that the name did not resolve."""

    def __init__(self, reason: str, unresolved: bool = False):
        super().__init__(reason)
        self.unresolved = unresolved


class PeerTarget(NamedTuple):
    url: str                  # https://<host>[:<port>], as given, with no path
    host: str                 # lower case, no trailing dot; the name the certificate is checked for
    port: int
    addrs: tuple[str, ...]    # every address the host resolved to, all inside the tailnet ranges, IPv4 first; a connection goes to addrs[0]


def in_tailnet(addr) -> bool:
    """Is this text an IPv4 address in 100.64.0.0/10 or an IPv6 address in fd7a:115c:a1e0::/48? Anything else is False: loopback, link-local,
    169.254.169.254, private ranges, public addresses, IPv4-mapped IPv6, an address with a zone id, text that is no address."""
    try:
        ip = ipaddress.ip_address(str(addr))
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        return ip.scope_id is None and ip in TAILNET_V6
    return ip in TAILNET_V4


def _suffix(suffix) -> str | None:
    s = str(suffix or "").strip().strip(".").lower()
    return s if s and all(_LABEL_RE.fullmatch(p) for p in s.split(".")) else None


def _resolve_all(host: str, port: int) -> list[str]:
    """Every address the system resolver gives for `host`, as text, IPv4 first, no repeats. OSError when it cannot resolve. This is the one place a
    peer name is looked up; tests replace it with a fake."""
    seen: list[str] = []
    for fam, _t, _p, _c, sa in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        if fam in (socket.AF_INET, socket.AF_INET6) and sa[0] not in seen:
            seen.append(sa[0])
    return sorted(seen, key=lambda a: ":" in a)


def check_peer_url(url, suffix, resolver=None) -> PeerTarget:
    """The one rule for an address of another node. Raises PeerUrlError (never anything else) unless: the scheme is https; there is no user info,
    no query, no fragment and no path beyond `/`; the port is 1 to 65535 (443 when absent); the host is an address inside the tailnet ranges, or a
    name that ends with `.<suffix>` (the tailnet's MagicDNS suffix) AND that resolves, now, only to addresses inside them. One address outside the
    ranges in the answer refuses the whole name. `resolver(host, port) -> [address text]` is called once, for a name only. The caller connects to
    `addrs[0]` and checks the certificate for `host`, so the name is not looked up a second time and a second answer cannot be trusted."""
    resolve = resolver or _resolve_all
    if not isinstance(url, str) or not url or len(url) > 300:
        raise PeerUrlError("not an address")
    if any(ord(c) <= 0x20 or ord(c) >= 0x7F or c == "\\" for c in url):
        raise PeerUrlError("the address holds a space, a control or non-ASCII character or a backslash")
    if "?" in url or "#" in url:
        raise PeerUrlError("no query or fragment is allowed")
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url)
        port = u.port
        host = u.hostname
    except ValueError:
        raise PeerUrlError("not a valid address") from None
    if u.scheme != "https":
        raise PeerUrlError("only https is allowed")
    if "@" in u.netloc or u.username is not None or u.password is not None:
        raise PeerUrlError("no user info is allowed")
    if u.path not in ("", "/"):
        raise PeerUrlError("the address is a host and a port, nothing after")
    if not host:
        raise PeerUrlError("no host")
    if port is not None and not 1 <= port <= 65535:
        raise PeerUrlError("the port is out of range")
    port = port or 443
    host = host.rstrip(".").lower()
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if literal:
        if not in_tailnet(host):
            raise PeerUrlError("the address is not inside the tailnet ranges")
        addrs: list[str] = [str(ipaddress.ip_address(host))]
    else:
        suf = _suffix(suffix)
        if suf is None:
            raise PeerUrlError("this tailnet's MagicDNS name is not known, so a name cannot be checked")
        labels = host.split(".")
        if not host.endswith("." + suf) or not all(_LABEL_RE.fullmatch(p) for p in labels):
            raise PeerUrlError("the name is not under this tailnet's MagicDNS suffix")
        try:
            got = list(resolve(host, port))
        except OSError:
            raise PeerUrlError("the name does not resolve", unresolved=True) from None
        except Exception:
            raise PeerUrlError("the name could not be resolved") from None
        if not got:
            raise PeerUrlError("the name does not resolve", unresolved=True)
        if not all(isinstance(a, str) and in_tailnet(a) for a in got):
            raise PeerUrlError("the name resolves to an address outside the tailnet ranges")
        addrs = [str(ipaddress.ip_address(a)) for a in got]
    shown = f"[{host}]" if ":" in host else host
    return PeerTarget(f"https://{shown}" + ("" if port == 443 else f":{port}"), host, port, tuple(addrs))


def valid_peer_url(url, suffix, resolver=None) -> list[str] | None:
    """The addresses a connection to `url` may use (see check_peer_url), or None when the address is refused."""
    try:
        return list(check_peer_url(url, suffix, resolver).addrs)
    except PeerUrlError:
        return None


# ================================================================ pairing (issue #135, phase P3)

SCOPES = ("read", "tasks", "sessions", "permissions")
DEFAULT_SCOPES = ("read", "tasks")
SCOPE_ANY = "any"                       # a route any valid pair may call, whatever its scopes (rotate and unpair: a pair may always manage itself)
# The closed table of what a node token may call: (method, path) -> the scope it needs. A path may hold `{name}` for one segment. Every later
# issue adds its rows here, each with a refusal test; nothing outside this table opens for a node token (the middleware answers 403).
NODE_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/node"): "read",
    ("GET", "/api/node/summary"): "read",
    ("GET", "/api/node/state"): "read",
    ("POST", "/api/node/rotate"): SCOPE_ANY,
    ("POST", "/api/node/unpair"): SCOPE_ANY,
}

TOKEN_PREFIX = "ccbnode_"
TOKEN_RE = re.compile(r"ccbnode_[A-Za-z0-9_-]{43}")          # secrets.token_urlsafe(32) is 43 characters: 256 bits
PEER_ID_RE = re.compile(r"p_[0-9a-f]{16}")
TOKEN_FILE = "node-tokens.json"
KV_CODE = "node_pair_code"
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"           # Crockford base32: no I, L, O, U
CODE_LEN = 10                                                 # 10 characters of 5 bits: 50 bits
CODE_TRIES = 5                                                # wrong tries that burn the code
CODE_MINUTES = (1, 30)
CODE_DEFAULT_MINUTES = 10
PAIR_RATE = 20                                                # pair attempts per minute and source address
ROTATE_GRACE = 60                                             # seconds the old token stays valid after a rotation
AUDIT_DAYS = 90
AUDIT_DETAIL_MAX = 200
USED_EVERY = 30                                               # last_used_at / last_seen are written at most this often per pair
READ_RATE, WRITE_RATE = 120, 30                               # per pair and minute
BODY_MAX = 256 * 1024                                         # the largest request body a node route accepts (413 above it)
RESP_MAX = 512 * 1024                                         # the largest answer a PeerClient reads
CALL_TIMEOUT = 5.0
RESERVED_HANDLES = ("local", "self")

pair_limiter = _Limiter(PAIR_RATE, 60.0, 2048)               # pair attempts per source address
refusal_limiter = _Limiter(30, 600.0, 4)                      # audit rows for refused pair attempts, whole board
node_read_limiter = _Limiter(READ_RATE, 60.0, 2048)          # per pair
node_write_limiter = _Limiter(WRITE_RATE, 60.0, 2048)
relay_read_limiter = _Limiter(READ_RATE, 60.0, 512)           # the hub relay routes (issue #140), per signed-in person and class
relay_write_limiter = _Limiter(WRITE_RATE, 60.0, 512)
confirm_limiter = _Limiter(30, 60.0, 2048)                    # POST /api/nodes/pair/confirm, per source address
CONFIRM_TTL = 30.0                                            # seconds an in-flight add_node answers the confirm route (its pair call waits 8 s at most)
CONFIRM_MAX = 16                                              # in-flight records kept at most; only add_node (a signed-in person's action) writes one
NONCE_RE = re.compile(r"[A-Za-z0-9_-]{22,64}")
_pending: dict[tuple[str, str], float] = {}                   # (this board's node id, proof) -> expiry: only add_node writes it, in memory only
_pending_lock = threading.Lock()
_code_lock = threading.Lock()                                 # one redeem at a time: two requests cannot both spend the code
_reg_lock = threading.RLock()                                 # the registry is read, changed and written whole
_tok_lock = threading.Lock()                                  # the token file likewise
_last_prune: float | None = None

peer_transport = None                                         # tests set a fake: transport(target, method, path, headers, body, timeout) -> (status, headers, body)


def _now() -> float:
    """The clock for code expiry, the rotation window and the audit prune (epoch seconds). Tests replace it."""
    return time.time()


def _db(db=None):
    if db is not None:
        return db
    from . import main                          # the running board's database, as resolve() does
    return main.db


class PairError(Exception):
    """A pairing that did not happen. `reason` is one word the page can switch on; str() is the sentence for a person; `status` the HTTP code."""
    STATUS = {"none": 403, "wrong": 403, "expired": 403, "burned": 403, "rate_limited": 429, "bad_request": 400, "bad_url": 400,
              "callback_mismatch": 409, "refused": 502, "unreachable": 502, "store": 500}
    MESSAGES = {
        "none": "There is no active pairing code on that node. Make a new one there.",
        "wrong": "That code is not right.",
        "expired": "That code has expired. Make a new one on the other node.",
        "burned": "Too many wrong tries; that code is cancelled. Make a new one on the other node.",
        "rate_limited": "Too many tries; wait a minute.",
        "bad_request": "The pairing request is not valid.",
        "bad_url": "The address is not one this board may call.",
        "callback_mismatch": "The address does not belong to the node that asked to pair.",
        "refused": "The other node refused the pairing.",
        "unreachable": "The other node could not be reached.",
        "store": "The token could not be saved on this node; nothing was paired.",
    }

    def __init__(self, reason: str, retry_after: int = 0, message: str | None = None):
        self.reason = reason if reason in self.STATUS else "refused"
        self.retry_after = int(retry_after or 0)
        self.message = message or self.MESSAGES[self.reason]
        super().__init__(self.message)

    @property
    def status(self) -> int:
        return self.STATUS[self.reason]

    def payload(self) -> dict:
        """The JSON body of the error answer; the route adds Retry-After when `retry_after` is set."""
        return {"error": self.message, "reason": self.reason}


class PeerError(Exception):
    """An outgoing call that failed before an answer was usable. `reason`: url (the address rule refused it), unresolved, no_token, unreachable,
    redirect, too_large, bad_path, store. str() holds no header, no token and no address of the peer's answer."""

    def __init__(self, reason: str, message: str | None = None, unresolved: bool = False, cause: BaseException | None = None):
        self.reason = reason
        self.unresolved = unresolved
        self.cause = cause                  # the transport's own exception for `unreachable` (the hub tells a timeout from a refusal or a TLS failure by its type); never shown
        super().__init__(message or reason)


def route_scope(method: str, path: str) -> str | None:
    """The scope a node token needs for (method, path): a scope name or SCOPE_ANY; None when the route is not in NODE_ROUTES (the token gets a 403)."""
    m = str(method or "").upper()
    hit = NODE_ROUTES.get((m, path))
    if hit is not None:
        return hit
    for (rm, pat), scope in NODE_ROUTES.items():
        if rm == m and "{" in pat and re.fullmatch(re.sub(r"\{[a-z_]+\}", "[^/]+", re.escape(pat).replace("\\{", "{").replace("\\}", "}")), path):
            return scope
    return None


def scope_ok(scopes, needed: str | None) -> bool:
    """Does a pair holding `scopes` meet the scope a route needs (None = the route is not listed: never)?"""
    return needed is not None and (needed == SCOPE_ANY or needed in (scopes or ()))


def clean_scopes(scopes) -> list[str]:
    """`scopes` as a list in SCOPES order, no repeats. ValueError for an empty list, a non-list or a name outside SCOPES."""
    if not isinstance(scopes, (list, tuple)) or not scopes or not all(isinstance(x, str) for x in scopes):
        raise ValueError("choose at least one scope")
    bad = [x for x in scopes if x not in SCOPES]
    if bad:
        raise ValueError("unknown scope")
    return [x for x in SCOPES if x in scopes]


def _scopes_of(raw) -> list[str]:
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return [x for x in SCOPES if x in v] if isinstance(v, list) else []
    except ValueError:
        return []


def tailnet_suffix() -> str | None:
    """This tailnet's MagicDNS suffix, which the address rule needs for a name; None when Tailscale does not say."""
    from . import nodes_discovery               # imported here: that module imports this one
    return nodes_discovery.magic_suffix(_ts())


def own_claim() -> dict:
    """What this board tells another when it pairs: {id, name, url, version}. No path, no address of this machine, no secret."""
    return {"id": node_id(), "name": display_name(), "url": public_url(), "version": settings.image_version or None}


# ---------------------------------------------------------------- the audit

_TOKENISH = re.compile(r"cc(?:bnode|bmcp)_[A-Za-z0-9_-]{6,}")
_CODEISH = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}\b")
_HEXISH = re.compile(r"\b[0-9a-fA-F]{40,}\b")
_last_prune_gap = 3600.0


def _scrub(v, n: int) -> str | None:
    """A short printable text with anything that looks like a token, a pairing code or a digest replaced. Defence in depth: callers pass no secret."""
    if v is None:
        return None
    t = "".join(c for c in str(v) if c.isprintable()).strip()
    t = _HEXISH.sub("[digest]", _CODEISH.sub("[code]", _TOKENISH.sub("[token]", t)))
    return t[:n] or None


def audit(direction: str, peer_id: str, action: str, ok: bool, detail: str | None = None, *, node_name: str | None = None, user: str | None = None,
          target: str | None = None, status: str | None = None, db=None) -> None:
    """Write one audit row (`direction` is `in` or `out`), then, at most once an hour, drop the rows older than 90 days. Never raises: the audit
    must not break the action it describes. Nothing secret goes in: a token, a code or a digest in `detail` is replaced (so a name shaped like a pairing
    code, `ABCDE-12345`, is replaced too), and a prompt is never passed."""
    global _last_prune
    try:
        d = _db(db)
        d.node_audit_add(at=iso(_now()), direction="out" if direction == "out" else "in", peer=str(peer_id or "")[:64],
                         action=_scrub(action, 40) or "unknown", status=status or ("ok" if ok else "failed"), node_name=_scrub(node_name, 41),
                         user=_scrub(user, 64), target=_scrub(target, 120), detail=_scrub(detail, AUDIT_DETAIL_MAX))
        t = time.monotonic()
        if _last_prune is None or t - _last_prune > _last_prune_gap:
            _last_prune = t
            d.node_audit_prune(iso(_now() - AUDIT_DAYS * 86400))
    except Exception as e:
        log.debug("audit row failed: %s", e.__class__.__name__)


def audit_list(limit: int = 100, db=None, *, direction: str | None = None, node: str | None = None, action: str | None = None, failures: bool = False) -> list[dict]:
    """The newest audit rows first: {id, at, direction, peer, node_name, user, action, target, status, detail}. The filters narrow it: `direction` (in or out),
    `node` (the row's node name or its peer id), `action` (one audit action) and `failures` (status other than ok); all together when several are given."""
    try:
        rows = _db(db).node_audit_list(limit, direction=direction, node=node, action=action, failures=failures)
    except Exception:
        return []
    return [{k: r[k] for k in ("id", "at", "direction", "peer", "node_name", "user", "action", "target", "status", "detail")} for r in rows]


# ---------------------------------------------------------------- outgoing tokens: the 0600 file

def tokens_path() -> Path:
    return Path(settings.data_dir) / TOKEN_FILE


def _read_tokens() -> dict[str, str]:
    """{peer_id: token} from the file; {} when it is missing. A file that is there but is not what this module wrote is moved aside as
    `node-tokens.json.bad` (still 0600), once, so a later save does not destroy it silently."""
    p = tokens_path()
    try:
        raw = p.read_bytes()
    except OSError:
        return {}
    try:
        t = json.loads(raw)["tokens"]
        return {k: v for k, v in t.items() if isinstance(k, str) and isinstance(v, str) and TOKEN_RE.fullmatch(v)}
    except (ValueError, KeyError, TypeError, AttributeError):
        _log_once("tokens-bad:" + str(p), "the node token file is not readable; it was set aside and the pairs need to be made again")
        try:
            os.replace(p, p.with_name(p.name + ".bad"))
        except OSError:
            pass
        return {}


def _write_tokens(d: dict[str, str]) -> None:
    _write_id_file(tokens_path(), json.dumps({"v": 1, "tokens": d}, separators=(",", ":")))       # atomic, 0600, the old file stays on any failure


def save_outgoing(peer_id: str, token: str) -> None:
    """Keep the token another board gave this one. OSError when the file cannot be written (then nothing changed)."""
    if not PEER_ID_RE.fullmatch(str(peer_id)) or not TOKEN_RE.fullmatch(str(token)):
        raise ValueError("not a peer id or a token")
    with _tok_lock:
        d = _read_tokens()
        d[peer_id] = token
        _write_tokens(d)


def drop_outgoing(peer_id: str) -> bool:
    """Forget a token. False when there was none or the file could not be rewritten (the caller never fails for that)."""
    with _tok_lock:
        d = _read_tokens()
        if peer_id not in d:
            return False
        del d[peer_id]
        try:
            if d:
                _write_tokens(d)
            else:
                tokens_path().unlink()                      # the last token: no file is left behind
        except OSError as e:
            log.warning("could not rewrite the node token file: %s", e.__class__.__name__)
            return False
        return True


def has_outgoing(peer_id: str) -> bool:
    with _tok_lock:
        return peer_id in _read_tokens()


def _load_outgoing(peer_id: str) -> str | None:
    """The only reader of a stored token: PeerClient and the unpair call use it. Private on purpose."""
    with _tok_lock:
        return _read_tokens().get(peer_id)


# ---------------------------------------------------------------- the registry (outgoing rows in kv, incoming rows in node_pairs)

_OUT_KEYS = ("peer_id", "handle", "node_id", "name", "url", "scopes", "created_at", "last_seen", "direction", "last_error", "needs_repair", "legacy")


def _new_peer_id() -> str:
    return "p_" + secrets.token_hex(8)


def _out_rows(db) -> list[dict]:
    try:
        rec = db.kv_get(KV_PEERS)
    except Exception:
        return []
    v = rec.get("value") if isinstance(rec, dict) else None
    return [r for r in v if isinstance(r, dict) and PEER_ID_RE.fullmatch(str(r.get("peer_id")))] if isinstance(v, list) else []


registry_listener = None                      # main sets it: called (no arguments) after the registry was saved with at least one row, so the hub starts the moment a node is paired


def _save_out(db, rows: list[dict]) -> None:
    if rows:
        db.kv_set(KV_PEERS, rows)
        cb = registry_listener
        if cb is not None:
            try:
                cb()
            except Exception as e:
                log.debug("registry listener failed: %s", e.__class__.__name__)
    else:
        db.kv_del(KV_PEERS)                       # a board with no paired node keeps no row


def out_peers(db) -> list[dict]:
    """The registry rows this board calls (direction out, legacy rows included) as views: no token, no digest. [] on a board with no paired node."""
    return [_out_view(r) for r in _out_rows(db)]


def _out_view(r: dict) -> dict:
    v = {k: r.get(k) for k in _OUT_KEYS}
    v["direction"] = "out"
    v["scopes"] = _scopes_of(r.get("scopes"))
    v["needs_repair"] = bool(r.get("needs_repair"))
    v["legacy"] = bool(r.get("legacy"))
    return v


def _in_view(r: dict) -> dict:
    return {"peer_id": r["peer_id"], "node_id": r["peer_node_id"] or None, "name": r["peer_name"] or None, "url": r["peer_url"],
            "scopes": _scopes_of(r["scopes"]), "created_at": r["created_at"], "last_seen": r["last_used_at"], "direction": "in",
            "callback_unverified": bool(r["callback_unverified"]), "expires_at": r["expires_at"], "rotated_at": r["rotated_at"],
            "superseded_by": r.get("superseded_by") or None, "superseded_by_url": None}


def peers(db=None) -> list[dict]:
    """Every pair this board has, both directions, oldest first; no token and no digest. [] on a board nobody paired with."""
    d = _db(db)
    inc = [_in_view(r) for r in d.node_pairs()]
    if any(v["superseded_by"] for v in inc):                                  # the address of the newer pair, even if that pair was revoked since
        urls = {r["peer_id"]: r["peer_url"] for r in d.node_pairs(include_revoked=True)}
        for v in inc:
            v["superseded_by_url"] = urls.get(v["superseded_by"]) if v["superseded_by"] else None
    rows = [_out_view(r) for r in _out_rows(d)] + inc
    return sorted(rows, key=lambda r: (str(r["created_at"] or ""), r["peer_id"]))


def peer(ident, db=None) -> dict | None:
    """One pair by peer_id, or by registry handle. None when there is none."""
    if not isinstance(ident, str) or not ident:
        return None
    for r in peers(db):
        if r["peer_id"] == ident or (r["direction"] == "out" and r.get("handle") == ident):
            return r
    return None


def _handle_for(wanted, name, taken: set[str]) -> str:
    """A free registry handle. An explicit one that breaks the rule is refused (ValueError); a free one is used as it is; a taken one, or one made
    from the node's name, gets `-2`, `-3` ... `local` and `self` are reserved."""
    if wanted not in (None, ""):
        h = str(wanted)
        if not HANDLE_RE.fullmatch(h) or h in RESERVED_HANDLES:
            raise ValueError("a handle is 1 to 31 lower-case letters, digits or dashes, and starts with a letter or a digit; local and self are reserved")
        base = h
    else:
        base = re.sub(r"[^a-z0-9-]+", "-", str(name or "").lower()).strip("-")[:31].strip("-")
        if not base or base in RESERVED_HANDLES:
            base = "node"
    cand, n = base, 1
    while cand in taken or cand in RESERVED_HANDLES:
        n += 1
        suffix = f"-{n}"
        cand = base[:31 - len(suffix)] + suffix
    return cand


def put_peer(record: dict, db=None) -> dict:
    """Save a registry row (direction `out`) or change a pair row of an incoming pair (direction `in`, which must exist: only a redeemed code or
    add_incoming creates one). Only the known keys are kept, so a token or a digest in `record` is dropped, never stored. The address rule is
    applied here (PeerUrlError, a ValueError, when it fails), except for a legacy row, which the old poller reads on its own terms. Returns the view."""
    if not isinstance(record, dict):
        raise ValueError("not a record")
    d = _db(db)
    direction = record.get("direction", "out")
    url = record.get("url")
    legacy = bool(record.get("legacy"))
    if direction == "in":
        pid = record.get("peer_id")
        row = d.node_pair_get(pid) if isinstance(pid, str) else None
        if row is None or row["revoked_at"]:
            raise ValueError("no such pair")
        fields: dict = {}
        if url is not None:
            check_peer_url(url, tailnet_suffix())
            fields["peer_url"] = str(url)
        if "name" in record:
            fields["peer_name"] = _sanitize_name(record["name"]) or ""
        if "node_id" in record:
            fields["peer_node_id"] = record["node_id"] if isinstance(record["node_id"], str) and ID_RE.fullmatch(record["node_id"]) else ""
        if "scopes" in record:
            fields["scopes"] = json.dumps(clean_scopes(record["scopes"]))
        if "callback_unverified" in record:
            fields["callback_unverified"] = 1 if record["callback_unverified"] else 0
        d.node_pair_update(pid, **fields)
        return _in_view(d.node_pair_get(pid))
    if direction != "out":
        raise ValueError("direction is in or out")
    if legacy:
        if not isinstance(url, str) or not url:
            raise ValueError("a legacy row needs an address")
        shown = url.rstrip("/")
    else:
        shown = check_peer_url(url, tailnet_suffix()).url
    with _reg_lock:
        rows = _out_rows(d)
        pid = record.get("peer_id") or _new_peer_id()
        if not PEER_ID_RE.fullmatch(str(pid)):
            raise ValueError("not a peer id")
        old = next((r for r in rows if r["peer_id"] == pid), None)
        taken = {r.get("handle") for r in rows if r["peer_id"] != pid}
        name = _sanitize_name(record.get("name")) or (old or {}).get("name") or "node"
        handle = record.get("handle") or (old or {}).get("handle")
        new ={"peer_id": pid, "handle": _handle_for(handle, name, taken), "direction": "out",
               "node_id": record.get("node_id") if isinstance(record.get("node_id"), str) and ID_RE.fullmatch(record["node_id"]) else (old or {}).get("node_id"),
               "name": name, "url": shown,
               "scopes": clean_scopes(record["scopes"]) if record.get("scopes") else ((old or {}).get("scopes") or ["read"]),
               "created_at": (old or {}).get("created_at") or record.get("created_at") or iso(_now()),
               "last_seen": record.get("last_seen", (old or {}).get("last_seen")), "last_error": record.get("last_error", (old or {}).get("last_error")),
               "needs_repair": bool(record.get("needs_repair", (old or {}).get("needs_repair"))), "legacy": legacy}
        _save_out(d, [new if r["peer_id"] == pid else r for r in rows] if old else rows + [new])
        return _out_view(new)


def remove_peer(ident, db=None) -> bool:
    """Forget a pair on this board only: an outgoing row and its token are deleted, an incoming pair is revoked. Never calls the other board, so it
    cannot fail because that board is off. False when there is no such pair."""
    d = _db(db)
    p = peer(ident, d)
    if p is None:
        return False
    if p["direction"] == "in":
        return revoke(p["peer_id"], db=d)
    with _reg_lock:
        _save_out(d, [r for r in _out_rows(d) if r["peer_id"] != p["peer_id"]])
    drop_outgoing(p["peer_id"])
    audit("out", p["peer_id"], "removed", True, "legacy row" if p["legacy"] else None, node_name=p["name"], db=d)
    return True


def _note(db, peer_id: str, *, ok: bool | None = None, error: str | None = None, repair: bool | None = None) -> None:
    """Record what the last call to a peer showed: last_seen on an answer, last_error on a failure, needs_repair when the peer no longer takes the
    token. Written only when something changed or the last write is older than USED_EVERY seconds."""
    try:
        with _reg_lock:
            rows = _out_rows(db)
            row = next((r for r in rows if r["peer_id"] == peer_id), None)
            if row is None:
                return
            before = dict(row)
            if ok:
                if not row.get("last_seen") or iso(_now() - USED_EVERY) > row["last_seen"]:
                    row["last_seen"] = iso(_now())
                row["last_error"] = None
                row["needs_repair"] = False
            else:
                row["last_error"] = error
                if repair is not None:
                    row["needs_repair"] = repair
            if row != before:
                _save_out(db, rows)
    except Exception as e:
        log.debug("could not note the call: %s", e.__class__.__name__)


def import_legacy(items, db=None) -> list[dict]:
    """Make the CCBOARD_NODES entries ([{name, url}], as health.parse_nodes gives them) read-only `legacy` rows, and drop legacy rows that are no
    longer listed. The old poller keeps reading them with the hub token on GET /api/node/summary; they hold no token and take no action. Pairing the
    same address replaces the row (add_node). Writes nothing when there is nothing to change. Returns the legacy views."""
    d = _db(db)
    want = [(str(i.get("name") or ""), str(i.get("url") or "").rstrip("/")) for i in (items or []) if isinstance(i, dict) and i.get("url")]
    with _reg_lock:
        rows = _out_rows(d)
        keep = [r for r in rows if not r.get("legacy") or str(r.get("url")).rstrip("/") in {u for _n, u in want}]
        have = {str(r.get("url")).rstrip("/") for r in keep}
        taken = {r.get("handle") for r in keep}
        for n, u in want:
            if u in have:
                continue
            h = _handle_for(None, n, taken)
            taken.add(h)
            have.add(u)
            keep.append({"peer_id": _new_peer_id(), "handle": h, "direction": "out", "node_id": None, "name": _sanitize_name(n) or h, "url": u,
                         "scopes": ["read"], "created_at": iso(_now()), "last_seen": None, "last_error": None, "needs_repair": False, "legacy": True})
        if keep != rows:
            _save_out(d, keep)
        return [_out_view(r) for r in keep if r.get("legacy")]


# ---------------------------------------------------------------- incoming tokens: the digest store

def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def _clean_claim(node) -> dict:
    """The parts of a caller's claimed card this board keeps: a node id, a short name and an address, each only if it is well formed."""
    n = node if isinstance(node, dict) else {}
    nid = n.get("id")
    url = n.get("url")
    return {"node_id": nid if isinstance(nid, str) and ID_RE.fullmatch(nid) else "", "name": _sanitize_name(n.get("name")) or "",
            "url": url if isinstance(url, str) and 0 < len(url) <= 300 else None}


def add_incoming(node=None, scopes=DEFAULT_SCOPES, *, callback_unverified: bool = False, db=None) -> tuple[dict, str]:
    """A new incoming pair: the row (digest only) and the token's plaintext, which this call returns and nothing keeps. It never touches another
    pair: the node id in `node` is only a claim until the caller's address has confirmed it (handle_pair), and only then does
    revoke_older_pairs retire the older pairs of that node."""
    d = _db(db)
    c = _clean_claim(node)
    token = _new_token()
    pid = _new_peer_id()
    d.node_pair_add(peer_id=pid, peer_node_id=c["node_id"], peer_name=c["name"], peer_url=c["url"], token_sha256=_digest(token),
                    scopes=json.dumps(clean_scopes(list(scopes))), created_at=iso(_now()), callback_unverified=1 if callback_unverified else 0)
    return _in_view(d.node_pair_get(pid)), token


def revoke_older_pairs(node_id: str, keep_peer_id: str, db=None) -> int:
    """Pairing the same node again replaces: every other active incoming pair of `node_id` at the SAME address as the kept one is revoked, the pair
    `keep_peer_id` never. Call it only once the node id is confirmed (the callback named the same id); a claim nobody confirmed must not cut another
    pair, so a `keep_peer_id` that is missing, revoked or still `callback_unverified` revokes nothing. A node id is only the word of the board at its
    address, so nothing is revoked on the id alone: a board that claims another node's id from its own address cuts nothing of that node's.
    An older active pair with the same node id at ANOTHER address (the node moved, or it is known by its IP one way and by its name the other) is not
    revoked either, and is not left silent: it is marked `superseded_by` the kept pair and an audit row says so. It keeps working until the person
    revokes it, and Settings > Nodes shows it with its Revoke. Returns how many it revoked."""
    d = _db(db)
    if not isinstance(node_id, str) or not node_id:
        return 0
    keep = d.node_pair_get(keep_peer_id)
    if keep is None or keep["revoked_at"] or keep["callback_unverified"] or keep["peer_node_id"] != node_id:
        return 0
    n = 0
    for r in d.node_pairs():
        if r["peer_node_id"] != node_id or r["peer_id"] == keep_peer_id:
            continue
        if _same_address(r["peer_url"], keep["peer_url"]):
            if revoke(r["peer_id"], db=d, why="replaced by a new pair"):
                n += 1
        elif r["id"] < keep["id"] and r.get("superseded_by") != keep_peer_id:
            d.node_pair_update(r["peer_id"], superseded_by=keep_peer_id)
            audit("in", r["peer_id"], "superseded", True,
                  f"the same node id paired again from {keep['peer_url'] or 'another address'}; this pair stays active until you revoke it",
                  node_name=r["peer_name"], db=d)
    return n


def mint_token(peer_id: str, db=None) -> str:
    """A fresh token for an existing incoming pair (the old one stops at once). Returns the plaintext, once. LookupError for an unknown or revoked pair."""
    d = _db(db)
    row = d.node_pair_get(peer_id)
    if row is None or row["revoked_at"]:
        raise LookupError("no such pair")
    token = _new_token()
    d.node_pair_update(peer_id, token_sha256=_digest(token), prev_sha256=None, prev_until=None)
    return token


def verify_token(plaintext, *, ip: str | None = None, db=None) -> dict | None:
    """The incoming pair a presented token belongs to (the view plus `via_previous`), or None for anything else: not a token, unknown, revoked,
    expired, or an old token past its 60 s. Every active pair is compared with hmac.compare_digest and the loop never stops early. The pair's
    last_used_at (and an address hint) is written at most every 30 s."""
    if not isinstance(plaintext, str) or not TOKEN_RE.fullmatch(plaintext):
        return None
    d = _db(db)
    given = _digest(plaintext)
    now_iso = iso(_now())
    found, via_prev = None, False
    for r in d.node_pairs():
        cur = hmac.compare_digest(given, r["token_sha256"] or "-")
        prev = bool(r["prev_sha256"] and r["prev_until"] and now_iso < r["prev_until"]) and hmac.compare_digest(given, r["prev_sha256"] or "-")
        if (cur or prev) and found is None and not (r["expires_at"] and now_iso >= r["expires_at"]):
            found, via_prev = r, not cur
    if found is None:
        return None
    upd: dict = {}
    if not found["last_used_at"] or iso(_now() - USED_EVERY) > found["last_used_at"]:
        upd["last_used_at"] = now_iso
    hint = _scrub(ip, 45)
    if hint and hint != found["last_ip_hint"]:
        upd["last_ip_hint"] = hint
    if upd:
        d.node_pair_update(found["peer_id"], **upd)
        found = {**found, **upd}
    return {**_in_view(found), "via_previous": via_prev}


def rotate_token(peer_id: str, db=None) -> str:
    """Rotate an incoming pair's token: the new plaintext is returned once, the old one still works for ROTATE_GRACE seconds."""
    d = _db(db)
    row = d.node_pair_get(peer_id)
    if row is None or row["revoked_at"]:
        raise LookupError("no such pair")
    token = _new_token()
    d.node_pair_update(peer_id, token_sha256=_digest(token), prev_sha256=row["token_sha256"] or None,
                       prev_until=iso(_now() + ROTATE_GRACE), rotated_at=iso(_now()))
    audit("in", peer_id, "rotated", True, f"the old token works {ROTATE_GRACE} s more", node_name=row["peer_name"], db=d)
    return token


def revoke(peer_id: str, db=None, why: str | None = None) -> bool:
    """Cut an incoming pair at once: the digests are blanked and the row marked revoked. False when there was nothing to revoke."""
    d = _db(db)
    row = d.node_pair_get(peer_id)
    if row is None or row["revoked_at"]:
        return False
    d.node_pair_update(peer_id, revoked_at=iso(_now()), token_sha256="", prev_sha256=None, prev_until=None)
    audit("in", peer_id, "revoked", True, why, node_name=row["peer_name"], db=d)
    return True


def rate_check(peer_id: str, write: bool = False) -> tuple[bool, int]:
    """The per-pair token bucket (reads 120 a minute, writes 30): (True, 0) or (False, seconds to wait) for the 429's Retry-After."""
    return (node_write_limiter if write else node_read_limiter).allow(str(peer_id))


# ---------------------------------------------------------------- pair codes

def format_code(norm: str) -> str:
    return f"{norm[:5]}-{norm[5:]}"


def normalize_code(text) -> str | None:
    """A typed code as its 10 characters: upper case, dashes and spaces dropped, O read as 0, I and L as 1 (Crockford). None when it is not one."""
    if not isinstance(text, str) or len(text) > 40:
        return None
    t = "".join(text.split()).replace("-", "").upper().translate(str.maketrans("OIL", "011"))
    return t if len(t) == CODE_LEN and all(c in CODE_ALPHABET for c in t) else None


def _code_digest(norm: str) -> str:
    return hashlib.sha256(("ccboard-pair-code:" + norm).encode("utf-8")).hexdigest()


def create_code(scopes=None, minutes: int = CODE_DEFAULT_MINUTES, db=None) -> dict:
    """Make the node's one pairing code (a new one replaces the old). Returns {code 'XXXXX-XXXXX', expires_at, scopes} once; only the digest, the
    expiry, the scopes and an attempt counter are kept. ValueError for a bad scope list or for minutes outside 1 to 30."""
    d = _db(db)
    sc = clean_scopes(list(DEFAULT_SCOPES if scopes is None else scopes))
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not CODE_MINUTES[0] <= minutes <= CODE_MINUTES[1]:
        raise ValueError("minutes is 1 to 30")
    n = secrets.randbits(CODE_LEN * 5)
    norm = "".join(CODE_ALPHABET[(n >> (5 * i)) & 31] for i in range(CODE_LEN - 1, -1, -1))
    expires = _now() + minutes * 60
    with _code_lock:
        d.kv_set(KV_CODE, {"sha256": _code_digest(norm), "expires_at": expires, "scopes": sc, "attempts": 0}, at=_now())
    audit("in", "", "code_created", True, f"scopes {','.join(sc)}, {minutes} min", db=d)
    return {"code": format_code(norm), "expires_at": iso(expires), "scopes": sc}


def code_status(db=None) -> dict:
    """{active, expires_at, scopes} of the pairing code, without the digest. Active only while it has not expired."""
    rec = _db(db).kv_get(KV_CODE)
    v = rec.get("value") if isinstance(rec, dict) else None
    if not isinstance(v, dict) or not isinstance(v.get("expires_at"), (int, float)) or _now() >= v["expires_at"]:
        return {"active": False, "expires_at": None, "scopes": []}
    return {"active": True, "expires_at": iso(v["expires_at"]), "scopes": _scopes_of(v.get("scopes"))}


def cancel_code(db=None) -> bool:
    d = _db(db)
    with _code_lock:
        had = d.kv_get(KV_CODE) is not None
        d.kv_del(KV_CODE)
    if had:
        audit("in", "", "code_cancelled", True, db=d)
    return had


def _audit_refused(db, detail: str) -> None:
    """The audit row of a refused pair attempt. The pair endpoint is open to the whole tailnet, so these rows are capped (30 in 10 minutes for the
    whole board): a flood of guesses cannot fill the table. A burned code and a spent one are always written."""
    if refusal_limiter.allow("all")[0]:
        audit("in", "", "pair_refused", False, detail, status="refused", db=db)


def redeem_code(code, caller: str, *, node=None, db=None) -> dict:
    """Spend the pairing code. Returns {peer_id, token, scopes, expires_at} with the token's plaintext, once. `caller` is the source address (the
    20 attempts a minute are counted before the code is looked at). Raises PairError: rate_limited, none (no active code), expired, wrong, burned
    (the 5th wrong try deletes the code). `node` is the caller's claimed card; its id, name and address fill the pair row."""
    d = _db(db)
    who = str(caller or "unknown")[:64]
    ok, wait = pair_limiter.allow(who)
    if not ok:
        _audit_refused(d, "rate limited")
        raise PairError("rate_limited", retry_after=wait)
    norm = normalize_code(code)
    with _code_lock:
        rec = d.kv_get(KV_CODE)
        v = rec.get("value") if isinstance(rec, dict) else None
        if not isinstance(v, dict) or not isinstance(v.get("sha256"), str) or not isinstance(v.get("expires_at"), (int, float)):
            _audit_refused(d, "no active code")
            raise PairError("none")
        if _now() >= v["expires_at"]:
            d.kv_del(KV_CODE)
            _audit_refused(d, "code expired")
            raise PairError("expired")
        if not hmac.compare_digest(_code_digest(norm or ""), v["sha256"]):
            tries = int(v.get("attempts") or 0) + 1
            if tries >= CODE_TRIES:
                d.kv_del(KV_CODE)
                audit("in", "", "code_burned", False, f"{CODE_TRIES} wrong tries", status="refused", db=d)
                raise PairError("burned")
            d.kv_set(KV_CODE, {**v, "attempts": tries}, at=_now())
            _audit_refused(d, f"wrong code, try {tries}")
            raise PairError("wrong")
        d.kv_del(KV_CODE)                                          # spent: a second use of the same code finds none
    scopes = _scopes_of(v.get("scopes")) or list(DEFAULT_SCOPES)
    row, token = add_incoming(node, scopes, callback_unverified=True, db=d)      # a spent code proves nothing about who is calling: unverified until handle_pair says otherwise
    audit("in", row["peer_id"], "code_used", True, f"scopes {','.join(scopes)}", node_name=row["name"], db=d)
    return {"peer_id": row["peer_id"], "token": token, "scopes": scopes, "expires_at": row["expires_at"]}


# ---------------------------------------------------------------- outgoing calls

class PeerReply:
    """An answer from a peer: `status`, the raw `body` (at most RESP_MAX bytes), `headers` (lower-case names) and `json` (the parsed body or None)."""

    def __init__(self, status: int, body: bytes, headers: dict):
        self.status, self.body, self.headers = status, body, headers
        try:
            self.json = json.loads(body.decode("utf-8")) if body else None
        except (ValueError, UnicodeDecodeError):
            self.json = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


_PATH_RE = re.compile(r"/[A-Za-z0-9._~%/-]*(?:\?[A-Za-z0-9._~%=&-]*)?")


def _https(target: PeerTarget, method: str, path: str, headers: dict, body: bytes | None, timeout: float):
    """One HTTPS exchange over the connection pinned to the validated address (the certificate is checked for the name). No redirect is followed."""
    from .nodes_discovery import _Pinned         # imported here: that module imports this one
    end = time.monotonic() + timeout
    conn = _Pinned(target.host, target.addrs[0], target.port, timeout)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        buf = b""
        while len(buf) <= RESP_MAX:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError("the answer took too long")
            if conn.sock is not None:
                conn.sock.settimeout(left)
            chunk = resp.read(8192)
            if not chunk:
                break
            buf += chunk
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, buf
    finally:
        conn.close()


def peer_call(url: str, method: str, path: str, *, headers: dict | None = None, body=None, timeout: float = CALL_TIMEOUT) -> PeerReply:
    """One call to a peer's address with the address rule applied now: a name is resolved once and the connection goes to that address. Raises
    PeerError (url, unresolved, bad_path, too_large, unreachable, redirect); an HTTP error status is an answer, not an error. `body` is bytes or a
    dict (sent as JSON). The headers are the caller's, so a call made here carries no credential unless the caller adds one."""
    if not isinstance(path, str) or not _PATH_RE.fullmatch(path) or ".." in path or "//" in path:
        raise PeerError("bad_path", "not a path on the peer")
    try:
        target = check_peer_url(url, tailnet_suffix())
    except PeerUrlError as e:
        raise PeerError("url", str(e), unresolved=e.unresolved) from None
    hdrs = {"X-CCBoard": "1", "Accept": "application/json", **(headers or {})}
    raw = None
    if body is not None:
        raw = body if isinstance(body, bytes) else json.dumps(body, separators=(",", ":")).encode("utf-8")
        if len(raw) > BODY_MAX:
            raise PeerError("too_large", "the request is too large")
        hdrs.setdefault("Content-Type", "application/json")
    try:
        status, rh, data = (peer_transport or _https)(target, method.upper(), path, hdrs, raw, timeout)
    except Exception as e:
        raise PeerError("unreachable", f"the peer could not be reached ({e.__class__.__name__})", cause=e) from None
    if 300 <= status < 400 and status != 304:           # 304 answers a conditional GET (If-None-Match): a status, never a redirect
        raise PeerError("redirect", "the peer answered with a redirect, which is never followed")
    if len(data) > RESP_MAX:
        raise PeerError("too_large", "the peer's answer is too large")
    return PeerReply(status, data, {str(k).lower(): v for k, v in (rh or {}).items()})


class PeerClient:
    """Calls another board as a paired node: `PeerClient(record).get("/api/node")`. `record` is a registry view (peer_id and url). The token is read
    from the 0600 file at each call and goes only into the Authorization header. The address rule runs at each call. A 401 marks the pair
    `needs_repair`; an answer clears it. Never follows a redirect, never sends an identity header."""

    def __init__(self, record: dict, *, db=None):
        self.peer_id = str(record.get("peer_id") or "")
        self.url = str(record.get("url") or "")
        self.legacy = bool(record.get("legacy"))
        self._db_arg = db

    def request(self, method: str, path: str, body=None, *, acting_user: str | None = None, timeout: float = CALL_TIMEOUT,
                headers: dict | None = None) -> PeerReply:
        token = None if self.legacy else _load_outgoing(self.peer_id)
        if not token:
            raise PeerError("no_token", "this node holds no token for that peer; pair again")
        headers = {**(headers or {}), "Authorization": "Bearer " + token, "X-CCBoard-Node": node_id()}      # extra headers (If-None-Match) can never replace the credential
        who = _scrub(acting_user, 64)
        if who:
            headers["X-CCBoard-Acting-User"] = who
        d = self._db_arg or _db()
        try:
            r = peer_call(self.url, method, path, headers=headers, body=body, timeout=timeout)
        except PeerError as e:
            _note(d, self.peer_id, ok=False, error=e.reason)
            raise
        if r.ok:
            _note(d, self.peer_id, ok=True)
        else:
            _note(d, self.peer_id, ok=False, error=f"the peer answered {r.status}", repair=True if r.status == 401 else None)
        return r

    def get(self, path: str, **kw) -> PeerReply:
        return self.request("GET", path, **kw)

    def post(self, path: str, body=None, **kw) -> PeerReply:
        return self.request("POST", path, body, **kw)


def _remote_unpair(url: str, token: str) -> bool:
    """Tell a peer we are leaving. Best effort: True only when it answered 2xx; any failure is False and never raises."""
    try:
        return peer_call(url, "POST", "/api/node/unpair", headers={"Authorization": "Bearer " + token, "X-CCBoard-Node": node_id()}, timeout=3.0).ok
    except Exception:
        return False


# ---------------------------------------------------------------- who is redeeming this code right now (the callback proof)

def canon_url(u) -> str | None:
    """A board address in one spelling: scheme://host:port with the scheme and host in lower case, no trailing dot and the port always there (443
    when absent). None when it is not an address with a host. Both sides of the confirm proof and every same-address check use this."""
    from urllib.parse import urlsplit
    try:
        p = urlsplit(str(u or ""))
        host = (p.hostname or "").rstrip(".").lower()
        port = p.port or 443
    except ValueError:
        return None
    if not host or not p.scheme:
        return None
    return f"{p.scheme.lower()}://{'[' + host + ']' if ':' in host else host}:{port}"


def confirm_proof(norm: str, nonce: str, caller_id: str, caller_url: str, target_url: str) -> str | None:
    """The proof that the board redeeming a code is the one that called add_node with it, and for THIS target: an HMAC-SHA256 keyed with the code over
    the nonce, the caller's node id, the caller's address and the address the caller typed (the target). The receiving board fills in its own address
    as the target, so a request that a third board relayed (the person typed the relay's address, the relay forwarded the body) gives a different
    digest and the caller answers 404. Neither the code nor the nonce goes into the callback, only this digest. None when an address is not one."""
    c, t = canon_url(caller_url), canon_url(target_url)
    if not c or not t or not isinstance(norm, str) or not isinstance(nonce, str) or not isinstance(caller_id, str):
        return None
    msg = json.dumps(["ccboard-pair-confirm/2", nonce, caller_id, c, t], separators=(",", ":")).encode("utf-8")
    return hmac.new(norm.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def begin_confirm(norm: str, nonce: str, target_url: str):
    """Register an in-flight add_node on this board towards `target_url` (in memory, CONFIRM_TTL seconds). Returns the key to hand to end_confirm,
    or None when this board has no address to put in the proof."""
    proof = confirm_proof(norm, nonce, node_id(), public_url() or "", target_url)
    if proof is None:
        return None
    key = (node_id(), proof)
    now = _now()
    with _pending_lock:
        for k in [k for k, exp in _pending.items() if exp <= now]:
            _pending.pop(k, None)
        while len(_pending) >= CONFIRM_MAX:
            _pending.pop(min(_pending, key=_pending.get), None)
        _pending[key] = now + CONFIRM_TTL
    return key


def end_confirm(key) -> None:
    if key is not None:
        with _pending_lock:
            _pending.pop(key, None)


def confirm_answer(proof) -> str | None:
    """This board's node id when `proof` belongs to an add_node that is in flight on this board right now, else None (the route answers 404 for an
    unknown and for a stale proof alike, so it says nothing about what is pending). Only a caller that knows the code and the nonce can make a
    proof that matches; every record is compared in constant time and the loop never stops early."""
    if not isinstance(proof, str) or not re.fullmatch(r"[0-9a-f]{64}", proof):
        return None
    me = node_id()
    now = _now()
    found = False
    with _pending_lock:
        for (nid, pr), exp in _pending.items():
            same = hmac.compare_digest(pr.encode("ascii"), proof.encode("ascii"))
            found = found or (same and nid == me and exp > now)
    return me if found else None


def _same_address(a, b) -> bool:
    """Do two peer addresses name the same scheme, host and port (host lower case, port 443 when absent)? False when either is missing or no address.
    A node id is only a claim, and the board at an address can claim any id: every gate that acts on a claimed id also asks for the same address."""
    ca, cb = canon_url(a), canon_url(b)
    return ca is not None and ca == cb


# ---------------------------------------------------------------- the handshake, accepting side

_NODE_KEYS = {"id", "name", "url", "version"}
_PAIR_KEYS = {"code", "node", "reverse", "confirm"}


def _check_pair_body(body) -> tuple[str, dict, dict | None, str | None]:
    """(code, node claim, reverse or None, confirm nonce or None) from the pair request, or PairError('bad_request'). Strict: a key nobody asked
    for (a permission mode, a sandbox, a scope list outside `reverse`) refuses the whole request, so no pair route can carry such a field. The
    `confirm` nonce is what the calling board's add_node made up for this one call (see handle_pair); an older board sends none."""
    bad = PairError("bad_request")
    if not isinstance(body, dict) or not set(body) <= _PAIR_KEYS or not isinstance(body.get("code"), str):
        raise bad
    confirm = body.get("confirm")
    if confirm is not None and (not isinstance(confirm, str) or not NONCE_RE.fullmatch(confirm)):
        raise bad
    node = body.get("node")
    if not isinstance(node, dict) or not set(node) <= _NODE_KEYS or not isinstance(node.get("id"), str) or not ID_RE.fullmatch(node["id"]):
        raise bad
    if not isinstance(node.get("name"), str) or not NAME_RE.fullmatch(node["name"]) or not isinstance(node.get("url"), str):
        raise bad
    if node.get("version") is not None and not isinstance(node.get("version"), str):
        raise bad
    try:
        check_peer_url(node["url"], tailnet_suffix(), lambda h, p: ["100.64.0.1"])          # the syntax only: no lookup before the code is checked
    except PeerUrlError:
        raise PairError("bad_url") from None
    rev = body.get("reverse")
    if rev is not None:
        if (not isinstance(rev, dict) or not set(rev) <= {"token", "scopes"} or not isinstance(rev.get("token"), str)
                or not TOKEN_RE.fullmatch(rev["token"])):
            raise bad
        try:
            rev = {"token": rev["token"], "scopes": clean_scopes(rev.get("scopes") or list(DEFAULT_SCOPES))}
        except ValueError:
            raise bad from None
    return body["code"], node, rev, confirm


def handle_pair(body, caller: str, db=None) -> dict:
    """The accepting side of the handshake: everything POST /api/nodes/pair does after the route has checked its headers. Nothing happens before the
    code is right (only the shape of the request is read). Then: the code is spent and the pair row (digest only) is written as
    `callback_unverified`. B then proves that the board at the caller's address is the one redeeming this code right now: it POSTs
    /api/nodes/pair/confirm there with a digest of the code and the request's `confirm` nonce (never the code, the nonce or a token), and the pair
    is verified only when that route answers with the claimed node id. A different id revokes the pair and refuses (callback_mismatch). An
    address that is down, an older board without the route, a board with no add_node in flight (a stranger who holds the code and claims another
    node's id and address) and a request with no `confirm` all leave the pair `callback_unverified`. A `reverse` token is kept as an outgoing pair
    for the caller only when verified. Returns {token, scopes, node, expires_at, callback_unverified, reverse}. Raises PairError."""
    d = _db(db)
    code, node, rev, confirm = _check_pair_body(body)
    norm = normalize_code(code)
    r = redeem_code(code, caller, node=node, db=d)
    pid = r["peer_id"]
    unverified = True
    try:
        # The target in the proof is THIS board's own address, never one taken from the request: a body that a third board relayed here was typed by
        # the person at that board's address, so the caller's proof for it does not match and the caller answers 404.
        proof = confirm_proof(norm, confirm, node["id"], node["url"], public_url() or "") if confirm and norm else None
        if proof:
            reply = peer_call(node["url"], "POST", "/api/nodes/pair/confirm", body={"proof": proof}, timeout=3.0)
            got = reply.json.get("node_id") if reply.ok and isinstance(reply.json, dict) else None
        else:
            check_peer_url(node["url"], tailnet_suffix())             # no proof to send, but the address rule still applies
            got = None
        if isinstance(got, str) and got != node["id"]:
            revoke(pid, db=d, why="callback named another node")
            audit("in", pid, "pair_refused", False, "callback named another node", node_name=node["name"], status="refused", db=d)
            raise PairError("callback_mismatch")
        unverified = not isinstance(got, str)
    except (PeerError, PeerUrlError) as e:
        if isinstance(e, PeerUrlError):
            e = PeerError("url", str(e), unresolved=e.unresolved)
        if e.reason == "url" and not e.unresolved:
            revoke(pid, db=d, why="the address is outside the tailnet")
            audit("in", pid, "pair_refused", False, "the address breaks the rule", node_name=node["name"], status="refused", db=d)
            raise PairError("bad_url") from None
        unverified = True
    reverse_reason = None
    if unverified:
        audit("in", pid, "callback_unverified", False, "the caller's address did not confirm this pairing", node_name=node["name"], db=d)
    else:
        d.node_pair_update(pid, callback_unverified=0)                # only now does the claimed node id count
        revoke_older_pairs(node["id"], pid, db=d)                     # the node id is confirmed: pairing again replaces
    reverse_saved = None
    if rev is not None and unverified:
        reverse_saved = False                                         # the claim was never confirmed: no outgoing row, no token written
        reverse_reason = "callback_unverified"
        audit("in", pid, "reverse_dropped", False, "the caller's address did not confirm its node id", node_name=node["name"], db=d)
    elif rev is not None:
        reverse_saved = False
        try:
            rid = _new_peer_id()
            save_outgoing(rid, rev["token"])
            try:
                put_peer({"peer_id": rid, "direction": "out", "node_id": node["id"], "name": node["name"], "url": node["url"], "scopes": rev["scopes"]}, db=d)
                reverse_saved = True
                audit("out", rid, "paired_back", True, f"scopes {','.join(rev['scopes'])}", node_name=node["name"], db=d)
            finally:
                if not reverse_saved:
                    drop_outgoing(rid)
        except (OSError, ValueError) as e:
            log.info("could not keep the reverse pair: %s", e.__class__.__name__)
    out = {"token": r["token"], "scopes": r["scopes"], "node": own_claim(), "expires_at": r["expires_at"], "callback_unverified": unverified,
           "reverse": reverse_saved}
    if reverse_reason:
        out["reverse_reason"] = reverse_reason
    return out


def unpair_incoming(peer_id: str, db=None) -> bool:
    """The other board says it is leaving (POST /api/node/unpair): cut its own pair here. The outgoing pair to the same node goes too, but only when
    this pair's node id was confirmed (not `callback_unverified`) AND the outgoing pair is at the same address: a node id is a claim, and a claim
    nobody confirmed, or one made from another address, must not remove the pair to another node. When an outgoing pair to the same node id is
    kept for that reason, an audit row (`unpair_kept_outgoing`) says which one stayed, so it is not silent. Never calls the other board. False when
    the pair was already gone."""
    d = _db(db)
    row = d.node_pair_get(peer_id)
    if row is None:
        return False
    cut = revoke(peer_id, db=d, why="the other node unpaired")
    if row["peer_node_id"]:
        kept = []
        for p in peers(d):
            if p["direction"] == "out" and not p["legacy"] and p["node_id"] == row["peer_node_id"]:
                if not row["callback_unverified"] and _same_address(p["url"], row["peer_url"]):
                    remove_peer(p["peer_id"], d)
                else:
                    kept.append(p)
        if kept:                                                      # nothing is removed on a claimed id; the person is told what stayed
            audit("in", peer_id, "unpair_kept_outgoing", True,
                  "kept the pair this board holds with " + ", ".join(f"{k['name'] or 'a node'} at {k['url'] or 'no address'}" for k in kept[:3])
                  + ": same node id, another address or not confirmed. Remove it yourself if it should go.", node_name=row["peer_name"], db=d)
    return cut


# ---------------------------------------------------------------- the handshake, calling side

def add_node(url: str, code: str, handle: str | None = None, both_ways: bool = False, db=None) -> dict:
    """The calling side of the handshake (POST /api/nodes): check the address, send this board's card and the code to the other board's pair endpoint,
    keep the token it answers (0600 file) and the registry row, read the card once to confirm, and return the registry view. With `both_ways` this
    board also mints a token (scopes read and tasks) for the other board and sends it as `reverse`. Pairing an address that is already in the registry
    (a legacy row, or the same node again) replaces that row. Raises ValueError (a bad address, code or handle; PeerUrlError is one), PairError
    (the other board refused, or the token could not be kept), PeerError (it could not be reached)."""
    d = _db(db)
    norm = normalize_code(code)
    if norm is None:
        raise ValueError("a pairing code is 10 letters and digits, like ABCDE-12345")
    target = check_peer_url(url, tailnet_suffix())
    if handle not in (None, ""):
        _handle_for(handle, None, set())                              # refuses a handle that breaks the rule, before anything is sent
    me = own_claim()
    if not me["url"]:
        raise ValueError("this node does not know its own address, so the other node could not call back; set CCBOARD_PUBLIC_URL")
    nonce = secrets.token_urlsafe(16)
    body: dict = {"code": format_code(norm), "node": me, "confirm": nonce}
    back = None
    if both_ways:
        back, back_token = add_incoming(None, DEFAULT_SCOPES, callback_unverified=True, db=d)       # unverified until the other board has answered
        body["reverse"] = {"token": back_token, "scopes": list(DEFAULT_SCOPES)}
    pending = begin_confirm(norm, nonce, target.url)       # while this call runs, POST /api/nodes/pair/confirm tells the board at `target` that it is us
    try:
        try:
            reply = peer_call(target.url, "POST", "/api/nodes/pair", body=body, timeout=8.0)
        except PeerError as e:
            raise PairError("unreachable", message=str(e) if e.reason in ("url", "unreachable", "redirect") else None) from None
        body = None                                                   # the reverse token's plaintext is not kept past the call
        if not reply.ok:
            j = reply.json if isinstance(reply.json, dict) else {}
            reason = j.get("reason") if j.get("reason") in PairError.STATUS else ("rate_limited" if reply.status == 429 else "refused")
            raise PairError(reason, retry_after=int(reply.headers.get("retry-after", 0) or 0) if reply.status == 429 else 0)
        j = reply.json if isinstance(reply.json, dict) else {}
        token, theirs = j.get("token"), j.get("node") if isinstance(j.get("node"), dict) else {}
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token) or not isinstance(theirs.get("id"), str) or not ID_RE.fullmatch(theirs["id"]):
            raise PairError("refused", message="The other node's answer was not a pairing answer.")
        scopes = [x for x in SCOPES if x in (j.get("scopes") or [])] or list(DEFAULT_SCOPES)
        name = _sanitize_name(theirs.get("name")) or "node"
        # An existing row is replaced only when it is at the address the person typed. The node id in the answer is the other board's own claim, so
        # it must not pick a row (and so its token and handle) of a node at another address; that makes a new row with a free handle.
        same = next((r for r in _out_rows(d) if str(r.get("url")).rstrip("/") == target.url.rstrip("/")), None)
        pid = same["peer_id"] if same else _new_peer_id()
        try:
            save_outgoing(pid, token)
        except OSError:
            _remote_unpair(target.url, token)                         # B holds a pair we cannot use; best effort
            raise PairError("store") from None
        token = None
        try:
            view = put_peer({"peer_id": pid, "direction": "out", "node_id": theirs["id"], "name": name, "url": target.url, "scopes": scopes,
                             "handle": (same or {}).get("handle") if same else handle, "legacy": False, "needs_repair": False, "last_error": None}, db=d)
        except Exception:
            tk = _load_outgoing(pid)
            if tk:
                _remote_unpair(target.url, tk)
            drop_outgoing(pid)
            raise
        if back is not None and j.get("reverse") is not True:         # the other board did not keep our token, so nobody can use this pair
            revoke(back["peer_id"], db=d, why="the other node did not keep the reverse token")
            back = None
        if back is not None:
            # The id is the other board's own word, from an answer at the address the person typed; it counts as confirmed only for that address (the
            # gates in unpair_incoming and remove_node also ask for the same address).
            d.node_pair_update(back["peer_id"], peer_node_id=theirs["id"], peer_name=name, peer_url=target.url, callback_unverified=0)
    except BaseException:
        if back is not None:
            revoke(back["peer_id"], db=d, why="pairing did not finish")
        audit("out", "", "pair_failed", False, None, db=d)
        raise
    finally:
        end_confirm(pending)
    audit("out", pid, "paired", True, f"scopes {','.join(scopes)}" + (", both ways" if both_ways else ""), node_name=name, db=d)
    try:                                                              # the first card read: a pair is "paired" once the card answers
        card_reply = PeerClient(view, db=d).get("/api/node")
        if card_reply.ok and isinstance(card_reply.json, dict) and card_reply.json.get("node_id") != theirs["id"]:
            _note(d, pid, ok=False, error="the card names another node id", repair=True)
    except PeerError:
        pass
    return peer(pid, d) or view


def rotate_outgoing(ident, db=None) -> dict:
    """Ask the other board for a new token (POST /api/node/rotate with the current one) and keep it. The old token works there for 60 s more.
    Returns the registry view. LookupError for an unknown pair, PeerError when the other board cannot be reached or does not answer a token,
    PairError('store') when the new token could not be saved (the pair is then marked needs_repair)."""
    d = _db(db)
    p = peer(ident, d)
    if p is None or p["direction"] != "out":
        raise LookupError("no such node")
    r = PeerClient(p, db=d).post("/api/node/rotate")
    tok = r.json.get("token") if r.ok and isinstance(r.json, dict) else None
    if not isinstance(tok, str) or not TOKEN_RE.fullmatch(tok):
        audit("out", p["peer_id"], "rotate_failed", False, f"the peer answered {r.status}", node_name=p["name"], db=d)
        raise PeerError("refused", f"the other node did not give a new token (it answered {r.status})")
    try:
        save_outgoing(p["peer_id"], tok)
    except OSError:
        _note(d, p["peer_id"], ok=False, error="the new token could not be saved", repair=True)
        audit("out", p["peer_id"], "rotate_failed", False, "the new token could not be saved", node_name=p["name"], db=d)
        raise PairError("store") from None
    audit("out", p["peer_id"], "rotated", True, f"the old token works {ROTATE_GRACE} s more there", node_name=p["name"], db=d)
    return peer(p["peer_id"], d) or p


def _claimants(d, p: dict) -> tuple[list[dict], list[dict]]:
    """The active incoming pairs that name the node id of `p` (a pair row of peers()), split in two: the ones remove_node revokes by itself (confirmed,
    at the same address as `p`) and the others (another address, or never confirmed), which only the person may decide on. Each is
    {peer_id, name, url, verified, node_id}. Both empty when `p` has no node id (a legacy row)."""
    if not p.get("node_id") or p.get("legacy"):
        return [], []
    auto, others = [], []
    for r in d.node_pairs():
        if r["peer_node_id"] != p["node_id"] or r["peer_id"] == p["peer_id"]:
            continue
        v = {"peer_id": r["peer_id"], "name": r["peer_name"] or None, "url": r["peer_url"], "verified": not r["callback_unverified"], "node_id": r["peer_node_id"]}
        if p["direction"] == "out" and v["verified"] and _same_address(r["peer_url"], p["url"]):
            auto.append(v)
        else:
            others.append(v)
    return auto, others


def removal_preview(ident, db=None) -> dict:
    """What removing a pair would also touch, without touching anything: {peer_id, name, url, auto, others}. `auto` are the incoming pairs remove_node
    revokes by itself (confirmed, same node id, same address); `others` are the active incoming pairs that name the same node id from another address
    or were never confirmed, which stay unless the person lists them in `also_revoke`. LookupError for an unknown pair."""
    d = _db(db)
    p = peer(ident, d)
    if p is None:
        raise LookupError("no such node")
    auto, others = _claimants(d, p)
    return {"peer_id": p["peer_id"], "name": p["name"], "url": p["url"], "auto": auto, "others": others}


def remove_node(ident, db=None, also_revoke=None) -> dict:
    """Remove a pair from this board. For a node this board calls, the other board is told first (best effort, 3 s), then the registry row and the
    token are deleted, and so is the pair that board holds for this one (confirmed, the same node id and the same address). This never fails because
    the other board is off. A node id is only a claim, so another active incoming pair that names the same node id (from another address, or never
    confirmed) is NOT revoked here: it is returned in `other_pairs` ({peer_id, name, url, verified}), and the person can revoke exactly the ones they
    list in `also_revoke` (peer ids). Every id in `also_revoke` must be an active incoming pair with the removed node's node id, else ValueError and
    nothing at all happens (the other board is not told either). Returns {removed, peer_told, also_revoked, other_pairs}. LookupError for an unknown pair."""
    d = _db(db)
    p = peer(ident, d)
    if p is None:
        raise LookupError("no such node")
    wanted = list(dict.fromkeys(also_revoke or []))
    if wanted:
        if not p.get("node_id") or p.get("legacy"):
            raise ValueError("this node has no node id, so no other pair can be revoked with it")
        by_id = {r["peer_id"]: r for r in d.node_pairs()}
        for w in wanted:
            r = by_id.get(w) if isinstance(w, str) else None
            if r is None or r["revoked_at"] or r["peer_node_id"] != p["node_id"]:
                raise ValueError("only an active pair with the same node id can be revoked together with this node")
    told = False
    if p["direction"] == "out":
        tk = None if p["legacy"] else _load_outgoing(p["peer_id"])
        told = bool(tk and p["url"] and _remote_unpair(p["url"], tk))
        tk = None
        auto, _ = _claimants(d, p)                                    # the pair that board holds for us: confirmed, same node id and the same address
        for q in auto:
            revoke(q["peer_id"], db=d, why="the pair was removed on this node")
    revoked = []
    for w in wanted:
        if revoke(w, db=d, why="revoked together with the node it names"):
            revoked.append(w)
    remove_peer(p["peer_id"], d)
    if p["direction"] == "out":
        audit("out", p["peer_id"], "unpair", told, "the other node was told" if told else "the other node was not told", node_name=p["name"], db=d)
    _, others = _claimants(d, p)
    return {"removed": True, "peer_told": told, "also_revoked": revoked,
            "other_pairs": [{k: o[k] for k in ("peer_id", "name", "url", "verified")} for o in others]}


# The relay table (issue #140, app/nodes_relay.py) lists the rows it adds to NODE_ROUTES when it is imported; import it last, whoever imported this module first.
from . import nodes_relay  # noqa: E402,F401
