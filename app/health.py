"""Box health (cpu / ram / disk / uptime) from /proc and shutil, and the hub/node fleet: a hub polls the
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

from .config import settings

log = logging.getLogger("ccboard.health")
POLL_SECONDS = 60
KV_NODES = "nodes"
HUB_HEADER = "x-ccboard-hub"
_cpu_prev: tuple[int, int] | None = None


def _cpu_pct() -> float | None:
    global _cpu_prev
    try:
        with open("/proc/stat") as f:
            fields = f.readline().split()[1:]
        vals = [int(x) for x in fields]
        idle, total = vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals)
    except (OSError, ValueError, IndexError):
        return None
    prev, _cpu_prev = _cpu_prev, (idle, total)
    if not prev or total == prev[1]:
        return None
    return round(100.0 * (1 - (idle - prev[0]) / (total - prev[1])), 1)


def _mem() -> dict | None:
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.strip().split()[0]) * 1024
        total, avail = info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
        return {"total": total, "used": total - avail, "pct": round(100.0 * (total - avail) / total, 1)}
    except (OSError, KeyError, ValueError):
        return None


def _uptime() -> float | None:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError):
        return None


def snapshot(extra: dict | None = None) -> dict:
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):
        load1 = None
    try:
        du = shutil.disk_usage(str(settings.projects_dir))
        disk = {"total": du.total, "used": du.used, "pct": round(100.0 * du.used / du.total, 1)}
    except OSError:
        disk = None
    return {"host": socket.gethostname(), "cpu_pct": _cpu_pct(), "load1": load1, "mem": _mem(), "disk": disk,
            "uptime_s": _uptime(), "cores": os.cpu_count(), "at": time.time(), **(extra or {})}


def check_hub_token(given: str | None) -> bool:
    return bool(settings.hub_token) and bool(given) and hmac.compare_digest(given.strip(), settings.hub_token)


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


class Poller(threading.Thread):
    """Runs on the hub: refreshes every node's summary into kv 'nodes'."""

    def __init__(self, db, nodes: list[dict], fetch=fetch_node):
        super().__init__(name="hub-poller", daemon=True)
        self.db, self.nodes, self.fetch = db, nodes, fetch
        self.stop = threading.Event()

    def poll_once(self) -> list[dict]:
        out = []
        for n in self.nodes:
            try:
                s = self.fetch(n["url"])
                out.append({"name": n["name"], "url": n["url"], "online": True, **s})
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
