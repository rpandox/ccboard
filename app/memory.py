"""claude-mem on the box: where its worker listens and whether it is healthy (v0.5.10), plus the low-level client the Memory proxy
(app/memory_proxy.py, v0.5.20) builds on.

The memory plugin (claude-mem@thedotmack) runs one loopback HTTP worker that Claude's hooks start on demand. Nothing here starts or
stops it. GET only, loopback only, a 2 s timeout per request, 256 KB per body for health (the proxy passes its own 2 MB cap). The one
exception to GET is save_note(): POST /api/memory/save of a finished task's result, sent only when the owner turned the write-back
setting on (off by default, kv mem_writeback).

Where the worker is (first match wins):
    1. CCBOARD_MEM_PORT                                        (settings.mem_port)
    2. <claude-mem dir>/worker.pid   JSON {pid, port, startedAt, startToken}
    3. <claude-mem dir>/settings.json   CLAUDE_MEM_WORKER_PORT
    4. 37700 + (uid % 100): the plugin's own default for CLAUDE_MEM_WORKER_PORT (37700 with uid 1000, 37701 on a Mac with uid 501;
       37701 when there is no uid)
`<claude-mem dir>` is ~/.claude-mem (settings.claude_mem_dir). CLAUDE_MEM_WORKER_HOST in that settings.json is honoured only when it
is a loopback address; anything else is refused before a socket is opened.

health() answers one dict, never raises, and never takes longer than BUDGET seconds:

    {state: up|degraded|down, version, port, port_source, pid, observations, sessions, summaries, db_size, queue_depth, processing,
     active_sessions, last_error{at, message, provider, failures, last_success_at}|None, reason, at}

    up        /health answered 200 and /api/readiness says ready (then /api/stats and /api/processing-status fill in the numbers;
              if either fails the numbers stay None and the state stays up)
    degraded  the port is open but /health or /api/readiness did not answer in time (or answered something else, or is not ready)
    down      connection refused (a stale or missing worker.pid, a worker that was never started) or a refused non-loopback host

last_error comes from <claude-mem dir>/observer-health.json on disk (never fails the probe). The monitor (app/agents/monitor.py) stores
health() under kv `mem_health` every 20 s and /api/state's `memory` is that value, so the state poll never waits on the worker.

Import rule: this module imports config only (doctor, main and the monitor import it, never the other way round).
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .config import settings

log = logging.getLogger("ccboard.memory")

KV_HEALTH = "mem_health"
PLUGIN_KEY = "claude-mem@thedotmack"


def default_port(uid: int | None = None) -> int:
    """The plugin's default worker port, 37700 + (uid % 100) (`37700+(process.getuid?.()??77)%100` in its 13.29.0 worker-service.cjs).
    Without a uid (a platform that has none) 37701, the documented port."""
    if uid is None:
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            return 37701
        uid = getuid()
    return 37700 + uid % 100


DEFAULT_PORT = default_port()
TIMEOUT = 2.0                  # per request
BUDGET = 3.5                   # the whole probe (several requests); below the doctor's 5 s cap per check
BODY_CAP = 256 * 1024          # bytes read from any response
FILE_CAP = 64 * 1024           # bytes read from any file on disk
MESSAGE_MAX = 300              # the observer's last error message as it travels in the state (polled every 3 s)
ENV_LEAK_VARS = ("CCBOARD_SESSION", "TMUX_PANE")     # a worker that holds either was started from inside a ccboard session
PROC_ROOT = Path("/proc")      # patched by tests
LOOPBACK_NAMES = ("localhost",)

now = time.time                # patched by tests


class NotLoopback(ValueError):
    """The worker host is not a loopback address: refused before any socket is opened."""


class Refused(OSError):
    """Nothing accepted the connection."""


class NoAnswer(OSError):
    """The port is open but the worker did not answer in time (or hung up, or the probe budget ran out)."""


class TooLarge(OSError):
    """The answer was bigger than the caller's cap (fetch(..., strict=True) only)."""


# ------------------------------------------------------------------ discovery

def mem_dir() -> Path:
    return Path(settings.claude_mem_dir)


def _loopback(host) -> bool:
    h = str(host or "").strip().strip("[]").lower()
    if h in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _port(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        v = v.strip()
        v = int(v) if v.isascii() and v.isdigit() and len(v) <= 6 else None
    elif isinstance(v, float) and v.is_integer():
        v = int(v)
    return v if isinstance(v, int) and 0 < v < 65536 else None


def _json_file(path: Path):
    """The parsed JSON file, or None for a missing, oversized, unreadable or malformed one."""
    try:
        with open(path, "rb") as f:
            raw = f.read(FILE_CAP + 1)
    except OSError:
        return None
    if len(raw) > FILE_CAP:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None


def worker_pid_file() -> dict:
    """{pid, port, started_at}: what worker.pid says, each None when absent or invalid. A bare number is taken as the pid."""
    d = _json_file(mem_dir() / "worker.pid")
    if isinstance(d, bool):
        d = None
    if isinstance(d, int):
        d = {"pid": d}
    if not isinstance(d, dict):
        return {"pid": None, "port": None, "started_at": None}
    pid = d.get("pid")
    pid = pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None
    started = d.get("startedAt")
    return {"pid": pid, "port": _port(d.get("port")), "started_at": started if isinstance(started, (str, int, float)) and not isinstance(started, bool) else None}


def discover() -> dict:
    """{host, port, source, pid}: host is whatever settings.json names (None = loopback default), source is env | worker.pid | settings | default."""
    pidfile = worker_pid_file()
    cfg = _json_file(mem_dir() / "settings.json")
    cfg = cfg if isinstance(cfg, dict) else {}
    host = cfg.get("CLAUDE_MEM_WORKER_HOST")
    host = host.strip() if isinstance(host, str) and host.strip() else None
    for source, port in (("env", _port(settings.mem_port)), ("worker.pid", pidfile["port"]),
                         ("settings", _port(cfg.get("CLAUDE_MEM_WORKER_PORT"))), ("default", DEFAULT_PORT)):
        if port:
            return {"host": host, "port": port, "source": source, "pid": pidfile["pid"]}
    raise AssertionError("unreachable: the default port always matches")


def _base(host, port: int) -> str:
    if host is not None and not _loopback(host):
        raise NotLoopback(f"refusing a non-loopback worker host ({str(host)[:60]}); set CLAUDE_MEM_WORKER_HOST to 127.0.0.1")
    h = str(host or "127.0.0.1").strip().strip("[]")
    h = "127.0.0.1" if h.lower() in LOOPBACK_NAMES else h
    return f"http://[{h}]:{port}" if ":" in h else f"http://{h}:{port}"


def worker_base() -> str:
    """http://127.0.0.1:<port>, the port found in the order above. NotLoopback when settings.json names another host."""
    d = discover()
    return _base(d["host"], d["port"])


# ------------------------------------------------------------------ one GET

def _conn(base: str, timeout: float) -> http.client.HTTPConnection:
    """An unopened connection to a loopback http base URL; NotLoopback (before any socket) for anything else. The one loopback guard
    shared by fetch() and save_note()."""
    u = urlsplit(base)
    try:
        port = u.port
    except ValueError:
        port = None
    if u.scheme != "http" or not u.hostname or not _loopback(u.hostname) or not port:
        raise NotLoopback(f"refusing {base[:60]!r}: only http://127.0.0.1:<port> is probed")
    host = "127.0.0.1" if u.hostname.lower() in LOOPBACK_NAMES else u.hostname
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _exchange(base: str, method: str, path: str, timeout: float, cap: int, body: bytes | None = None) -> tuple[int, bytes]:
    """One request -> (status, raw body, at most cap + 1 bytes). Refused / NoAnswer / NotLoopback as fetch() documents."""
    conn = _conn(base, timeout)
    try:
        try:
            conn.connect()
        except TimeoutError as e:
            raise NoAnswer("connect timed out") from e
        except OSError as e:
            raise Refused(e.__class__.__name__) from e
        headers = {"Accept": "application/json", "Connection": "close"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(cap + 1)
            status = resp.status
        except TimeoutError as e:
            raise NoAnswer(f"no answer within {timeout:g} s") from e
        except (OSError, http.client.HTTPException) as e:
            raise NoAnswer(e.__class__.__name__) from e
    finally:
        conn.close()
    return status, raw


def fetch(base: str, path: str, timeout: float = TIMEOUT, cap: int = BODY_CAP, strict: bool = False) -> tuple[int, object | None]:
    """GET base+path -> (status, parsed JSON or None for a body that is not JSON or exceeds `cap`).

    NotLoopback before any socket is opened when base is not a loopback http URL; Refused when nothing accepts the connection;
    NoAnswer when the connection opened but the worker times out, resets or sends garbage. No proxy, no redirect, GET only.
    `cap` is BODY_CAP (256 KB) for health; the Memory proxy passes PROXY_CAP (2 MB) and strict=True, which turns a body over the cap
    into TooLarge instead of None (for the proxy an oversized answer is an error, never an empty one)."""
    status, raw = _exchange(base, "GET", path, timeout, cap)
    if len(raw) > cap:
        if strict:
            raise TooLarge(f"the answer is larger than {cap // 1024} KB")
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return status, None


# ------------------------------------------------------------------ the one write: a note (POST /api/memory/save), behind the write-back setting

KV_WRITEBACK = "mem_writeback"         # {"on": bool}; absent = off (the default)
KV_WRITEBACK_DONE = "mem_wb_task:"     # + task id: the note of that task was handed to the worker once (never twice)
SAVE_PATH = "/api/memory/save"         # plugin source MemoryRoutes.ts, found in the 13.31.0 bundle (box check V11 row 12); never probed live
SAVE_TEXT_MAX = 8 * 1024               # bytes of the result text in a note
SAVE_TITLE_MAX = 200
SAVE_CUT_MARK = "\n[cut by ccboard: the result was longer than 8 KB]"


def writeback_on(db) -> bool:
    """The board preference `memory_writeback` (kv mem_writeback); off unless it was turned on."""
    rec = db.kv_get(KV_WRITEBACK)
    v = rec.get("value") if isinstance(rec, dict) else None
    return isinstance(v, dict) and v.get("on") is True


def set_writeback(db, on: bool) -> bool:
    db.kv_set(KV_WRITEBACK, {"on": bool(on)})
    return writeback_on(db)


def cap_note(text: str, cap: int = SAVE_TEXT_MAX) -> str:
    """`text` cut to at most `cap` UTF-8 bytes, at the last line break inside the cap when there is one, then marked."""
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text
    room = max(0, cap - len(SAVE_CUT_MARK.encode("utf-8")))
    head = raw[:room].decode("utf-8", "ignore")
    nl = head.rfind("\n")
    if nl > room // 2:
        head = head[:nl]
    return head.rstrip() + SAVE_CUT_MARK


def save_note(title: str, text: str, project: str | None, metadata: dict | None = None, timeout: float = TIMEOUT) -> int | None:
    """POST /api/memory/save {text, title, project, metadata} to the loopback worker: the board's single write to claude-mem, made only
    by the write-back of a finished task (memory_proxy.writeback_task) and never reachable from a browser. The text is capped
    (cap_note). Returns the worker's HTTP status; raises like fetch() (NotLoopback, Refused, NoAnswer). The answer is never logged."""
    body = {"text": cap_note(str(text or "").strip()), "title": str(title or "")[:SAVE_TITLE_MAX]}
    if project:
        body["project"] = project
    if metadata:
        body["metadata"] = metadata
    if not body["text"]:
        raise ValueError("an empty note is not saved")
    status, _ = _exchange(worker_base(), "POST", SAVE_PATH, timeout, BODY_CAP, json.dumps(body).encode("utf-8"))
    return status


# ------------------------------------------------------------------ observer-health.json

def _iso(v) -> str | None:
    """A time as UTC ISO text with whole seconds: from epoch seconds or milliseconds (number or numeric text) or an ISO string."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            try:
                v = float(s)
            except ValueError:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        if isinstance(v, (int, float)):
            secs = v / 1000.0 if abs(v) >= 1e11 else float(v)
            return datetime.fromtimestamp(secs, timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _line(v, cap: int) -> str | None:
    s = " ".join(str(v).split()) if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""
    if not s:
        return None
    return s if len(s) <= cap else s[:cap - 1] + "…"


def observer_error() -> dict | None:
    """The observer's last provider error from observer-health.json: {at, message, provider, failures, last_success_at} or None when
    there is none (or the file is missing or unreadable). `failures` is its consecutiveFailures counter: above 0 the observer is failing
    right now. `last_success_at` (lastSuccessAt, None when absent) later than `at` with no failures means it has recovered."""
    d = _json_file(mem_dir() / "observer-health.json")
    if not isinstance(d, dict):
        return None
    at, msg = _iso(d.get("lastErrorAt")), _line(d.get("lastErrorMessage"), MESSAGE_MAX)
    if at is None and msg is None:
        return None
    fails = d.get("consecutiveFailures")
    return {"at": at, "message": msg, "provider": _line(d.get("lastErrorProvider"), 60),
            "failures": fails if isinstance(fails, int) and not isinstance(fails, bool) and fails >= 0 else 0,
            "last_success_at": _iso(d.get("lastSuccessAt"))}


# ------------------------------------------------------------------ health

def _num(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _ready(status: int, body) -> bool:
    b = _dict(body)
    return status == 200 and str(b.get("status", "ready")).lower() in ("ready", "ok") and b.get("ready", True) is not False


def _blank() -> dict:
    return {"state": "down", "version": None, "port": None, "port_source": None, "pid": None, "observations": None, "sessions": None,
            "summaries": None, "db_size": None, "queue_depth": None, "processing": None, "active_sessions": None,
            "last_error": None, "reason": None, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _probe(out: dict, deadline: float) -> None:
    d = discover()
    out.update(port=d["port"], port_source=d["source"], pid=d["pid"])
    try:
        base = _base(d["host"], d["port"])
    except NotLoopback as e:
        out.update(state="down", reason=str(e))
        return

    def get(path: str):
        left = deadline - time.monotonic()
        if left < 0.05:
            raise NoAnswer("probe budget used up")
        return fetch(base, path, min(TIMEOUT, left))

    try:
        status, body = get("/health")
    except Refused:
        why = {"worker.pid": "worker.pid is stale or the worker stopped", "env": "CCBOARD_MEM_PORT points at nothing"}.get(
            d["source"], "no worker.pid: the worker starts with the first Claude session")
        out.update(state="down", reason=f"connection refused ({why})")
        return
    except NoAnswer:
        out.update(state="degraded", reason=f"the port is open but /health did not answer within {TIMEOUT:g} s")
        return
    if status != 200:
        out.update(state="degraded", reason=f"/health answered HTTP {status}")
        return
    h = _dict(body)
    out["pid"] = h["pid"] if isinstance(h.get("pid"), int) and not isinstance(h.get("pid"), bool) and h["pid"] > 0 else out["pid"]
    out["active_sessions"] = _num(h.get("activeSessions"))
    try:
        status, body = get("/api/readiness")
    except Refused:
        out.update(state="down", reason="connection refused (the worker stopped while it was probed)")
        return
    except NoAnswer:
        out.update(state="degraded", reason="/api/readiness did not answer in time")
        return
    if not _ready(status, body):
        out.update(state="degraded", reason="the worker is up but not ready" + (f" (HTTP {status})" if status != 200 else ""))
        return
    out["state"] = "up"

    def enrich(path: str) -> dict:
        """The JSON object of a 200 answer, else {}: a failed enrichment never demotes an up worker."""
        try:
            status, body = get(path)
        except OSError:
            return {}
        return _dict(body) if status == 200 else {}

    s = enrich("/api/stats")
    w, db = _dict(s.get("worker")), _dict(s.get("database"))
    ver = _line(w.get("version"), 40)
    out.update(version=ver, observations=_num(db.get("observations")), sessions=_num(db.get("sessions")),
               summaries=_num(db.get("summaries")), db_size=_num(db.get("size")))
    if out["active_sessions"] is None:
        out["active_sessions"] = _num(w.get("activeSessions"))
    p = enrich("/api/processing-status")
    out["queue_depth"] = _num(p.get("queueDepth"))
    out["processing"] = p["isProcessing"] if isinstance(p.get("isProcessing"), bool) else None


def health(budget: float = BUDGET) -> dict:
    """The worker's health (see the module docstring). Never raises; at most `budget` seconds."""
    out = _blank()
    try:
        _probe(out, time.monotonic() + budget)
    except Exception as e:      # a bug here must not take the monitor or the doctor down
        log.debug("memory probe failed: %s", e)
        out.update(state="down", reason=f"probe error: {e.__class__.__name__}")
    try:
        out["last_error"] = observer_error()
    except Exception:
        out["last_error"] = None
    return out


def state_view(db) -> dict | None:
    """state.memory: the monitor's last health() (kv mem_health), or None when claude-mem is off or nothing was sampled yet.
    Reads the kv only: a state poll never probes the worker."""
    if not settings.claude_mem:
        return None
    rec = db.kv_get(KV_HEALTH)
    v = rec.get("value") if isinstance(rec, dict) else None
    return v if isinstance(v, dict) else None


# ------------------------------------------------------------------ plugin and worker environment (for the doctor)

def plugin_status() -> dict:
    """{installed, version, enabled}: installed = the key claude-mem@thedotmack in <claude config dir>/plugins/installed_plugins.json
    (v2: under "plugins", a list of installs; v1: a top-level key); enabled = enabledPlugins in Claude's settings.json (True, False, or
    None when it says nothing about the plugin)."""
    cdir = Path(settings.claude_config_dir)
    d = _json_file(cdir / "plugins" / "installed_plugins.json")
    plugins = d.get("plugins") if isinstance(d, dict) and isinstance(d.get("plugins"), dict) else d
    entry = plugins.get(PLUGIN_KEY) if isinstance(plugins, dict) else None
    if isinstance(entry, list):
        entry = next((e for e in entry if isinstance(e, dict)), entry[0] if entry else None)
    out = {"installed": isinstance(entry, dict), "version": None, "enabled": None}
    if not out["installed"]:
        return out
    out["version"] = _line(entry.get("version"), 40)
    if isinstance(entry.get("enabled"), bool):
        out["enabled"] = entry["enabled"]
    cfg = _json_file(cdir / "settings.json")
    ep = cfg.get("enabledPlugins") if isinstance(cfg, dict) else None
    if isinstance(ep, dict) and isinstance(ep.get(PLUGIN_KEY), bool):
        out["enabled"] = ep[PLUGIN_KEY]
    return out


def env_leaks(pid) -> list[str] | None:
    """The NAMES (never the values) of the ccboard-session variables process `pid` holds, from /proc/<pid>/environ: [] is clean.
    None when the environment cannot be read (no /proc, another user's process, a process that is gone)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        with open(PROC_ROOT / str(pid) / "environ", "rb") as f:
            raw = f.read(1 << 20)
    except OSError:
        return None
    if not raw:
        return None
    names = {chunk.split(b"=", 1)[0].decode("ascii", "replace") for chunk in raw.split(b"\0") if chunk}
    return [n for n in ENV_LEAK_VARS if n in names]
