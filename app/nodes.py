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
"""
from __future__ import annotations

import hashlib
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

from . import tailscale as ts
from . import tmux
from .config import settings

log = logging.getLogger("ccboard.nodes")

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
KV_PEERS = "node_peers"                                       # the registry (issue #135 writes it); an empty or missing one means no paired node
SUB_KEY_LEN = 12
LABEL_MAX = 60
TS_TIMEOUT = 3.0                                              # seconds a request waits for `tailscale status --json`
TS_TTL = 120.0                                                # a reading is reused this long (a failed one for TS_TTL_FAIL)
TS_TTL_FAIL = 60.0


class RefError(ValueError):
    """Text that is not a node ref."""


# ---------------------------------------------------------------- Tailscale (one seam, so tests never run the real command)

def _ts_status() -> dict | None:
    """`tailscale status --json` parsed, None when it cannot be read; a short timeout so a request never waits long. Never raises."""
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
    return {"system": _pf.system() or None, "release": _pf.release() or None, "tailscale_os": tos if isinstance(tos, str) and tos else None}


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
