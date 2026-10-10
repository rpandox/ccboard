"""Box health (cpu / ram / disk / uptime) from app.platform (/proc on Linux, psutil elsewhere) and shutil, and the hub/node fleet: a hub polls the
other boxes' /api/node/summary over MagicDNS with a shared token (tagged nodes send no identity headers)."""
from __future__ import annotations

import hmac
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from . import platform
from .config import settings

log = logging.getLogger("ccboard.health")
POLL_SECONDS = 60
KV_NODES = "nodes"
HUB_HEADER = "x-ccboard-hub"


def _cpu_pct(consumer: str = "default") -> float | None:
    """CPU busy % since this consumer's previous reading (None on its first reading or when the host cannot say; app.platform keeps one
    previous reading per consumer, so the Sampler (60 s) and the /api/state poll (3 s) never steal each other's delta)."""
    return platform.cpu_pct(consumer)


def _mem() -> dict | None:
    return platform.mem()


def _uptime() -> float | None:
    return platform.uptime()


LOAD_TTL = 5.0                                      # seconds an under_load() answer is reused
_load_cache: tuple[float, bool] | None = None        # (monotonic time read, answer)
_load_lock = threading.Lock()


def under_load() -> bool:
    """True while the box is busy: the 1-minute load average is above the CPU count. Read at most once per LOAD_TTL seconds; an
    unreadable load (no getloadavg, no cpu_count) counts as not busy, and it never raises. The board backs off its own background
    work with it (#31): the project scan reuses git answers for longer, the cost refresh and the transcript indexer skip a pass.
    User actions and hook ingestion never look at it."""
    global _load_cache
    now = time.monotonic()
    with _load_lock:
        if _load_cache is not None and now - _load_cache[0] < LOAD_TTL:
            return _load_cache[1]
    try:
        cores = os.cpu_count() or 0
        busy = bool(cores) and float(os.getloadavg()[0]) > cores
    except Exception:                                # OSError, AttributeError (no getloadavg), odd values: not busy
        busy = False
    with _load_lock:
        _load_cache = (now, busy)
    return busy


def load_cache_clear() -> None:
    global _load_cache
    with _load_lock:
        _load_cache = None


def snapshot(extra: dict | None = None, consumer: str = "default") -> dict:
    """Box health. `consumer` names who is asking, for the cpu delta (see _cpu_pct)."""
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):
        load1 = None
    try:
        du = shutil.disk_usage(str(settings.projects_dir))
        disk = {"total": du.total, "used": du.used, "pct": round(100.0 * du.used / du.total, 1)}
    except OSError:
        disk = None
    snap = {"host": socket.gethostname(), "cpu_pct": _cpu_pct(consumer), "load1": load1, "mem": _mem(), "disk": disk,
            "uptime_s": _uptime(), "cores": os.cpu_count(), "at": time.time(), **(extra or {})}
    if platform.is_wsl():                            # the numbers above describe the WSL2 VM, not Windows (the key "host" is the host name)
        snap["host_note"] = "wsl2"
    return snap


def check_hub_token(given: str | None) -> bool:
    """Constant-time, as bytes (a non-ASCII header is a refusal, not a TypeError; issue #44, F-02)."""
    if not settings.hub_token or not given or not isinstance(given, str):
        return False
    return hmac.compare_digest(given.strip().encode("utf-8", "replace"), settings.hub_token.encode("utf-8", "replace"))


def parse_nodes(raw: str) -> list[dict]:
    """CCBOARD_NODES='ubu=https://ubu.tailnet.ts.net:8443,box2=https://box2.tailnet.ts.net' -> [{name, url}]."""
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, url = part.split("=", 1)
        name, url = name.strip(), url.strip().rstrip("/")
        if name and url.startswith("https://"):
            out.append({"name": name, "url": url})
    return out


def fetch_node(url: str) -> dict:
    req = urllib.request.Request(url + "/api/node/summary", headers={HUB_HEADER: settings.hub_token, "X-CCBoard": "1"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def same_url(a: str, b: str) -> bool:
    """Two node addresses are the same when they differ only in case or a trailing slash."""
    return str(a or "").strip().rstrip("/").lower() == str(b or "").strip().rstrip("/").lower()


class Poller(threading.Thread):
    """Runs on the hub: refreshes every node's summary into kv 'nodes'. The CCBOARD_NODES entries are the legacy rows of the registry (issue #135):
    polled with the hub token on GET /api/node/summary and nothing else. `paired()` (optional) returns the addresses that have since been paired
    with a token; pairing the same address replaces the legacy row, so those entries are no longer polled this way."""

    def __init__(self, db, nodes: list[dict], fetch=fetch_node, paired=None):
        super().__init__(name="hub-poller", daemon=True)
        self.db, self.nodes, self.fetch, self.paired = db, nodes, fetch, paired
        self.stop = threading.Event()

    def _targets(self) -> list[dict]:
        try:
            urls = list(self.paired()) if self.paired else []
        except Exception as e:
            log.warning("hub poll could not read the paired nodes: %s", e.__class__.__name__)
            urls = []
        return [n for n in self.nodes if not any(same_url(n["url"], u) for u in urls)]

    def poll_once(self) -> list[dict]:
        out = []
        for n in self._targets():
            try:
                s = self.fetch(n["url"])
                if not isinstance(s, dict):
                    raise ValueError("summary is not a JSON object")
                # trusted fields last: a node's own reply must not override the name/url the hub was configured with
                out.append({**s, "name": n["name"], "url": n["url"], "online": True})
            except Exception as e:
                out.append({"name": n["name"], "url": n["url"], "online": False, "error": str(e)[:200]})
        self.db.kv_set(KV_NODES, out)
        return out

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.poll_once()
            except Exception as e:
                log.warning("hub poll failed: %s", e)
            self.stop.wait(POLL_SECONDS)


def backup_status() -> dict | None:
    p = settings.data_dir / "backup-status.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None
