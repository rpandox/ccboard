"""Usage & limits. Account rate limits arrive through the statusline hook (kv 'rate_limits');
the 5-hour billing block (cost, tokens, burn rate, projection) comes from `ccusage blocks --json --active`,
polled in a background thread when ccusage is installed."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading

from . import cost

log = logging.getLogger("ccboard.usage")
POLL_SECONDS = 120
COST_EVERY = 5  # polls -> every 10 minutes
KV_BLOCK = "ccusage_block"


def parse_blocks(raw: str) -> dict:
    """ccusage JSON -> compact block dict. Never raises on odd input."""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {"available": True, "active": False, "error": "unparseable ccusage output"}
    blocks = data.get("blocks") if isinstance(data, dict) else None
    active = next((b for b in (blocks or []) if isinstance(b, dict) and b.get("isActive")), None)
    if not active:
        return {"available": True, "active": False}
    burn = active.get("burnRate") or {}
    proj = active.get("projection") or {}
    return {
        "available": True, "active": True,
        "start": active.get("startTime"), "end": active.get("endTime"),
        "cost_usd": active.get("costUSD"), "total_tokens": active.get("totalTokens"),
        "burn_cost_per_hour": burn.get("costPerHour"), "burn_tokens_per_minute": burn.get("tokensPerMinute"),
        "projected_cost": proj.get("totalCost"), "projected_tokens": proj.get("totalTokens"),
        "remaining_minutes": proj.get("remainingMinutes"),
        "models": active.get("models") or [],
    }


def fetch_block() -> dict:
    exe = shutil.which("ccusage")
    if not exe:
        return {"available": False}
    try:
        cp = subprocess.run([exe, "blocks", "--json", "--active"], capture_output=True, text=True, timeout=90)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"available": True, "active": False, "error": f"ccusage failed: {e.__class__.__name__}"}
    if cp.returncode != 0:
        return {"available": True, "active": False, "error": (cp.stderr or cp.stdout).strip()[-200:]}
    return parse_blocks(cp.stdout)


class Poller(threading.Thread):
    def __init__(self, db):
        super().__init__(name="ccusage-poller", daemon=True)
        self.db = db
        self.stop = threading.Event()

    def run(self) -> None:
        n = 0
        while not self.stop.is_set():
            try:
                self.db.kv_set(KV_BLOCK, fetch_block())
            except Exception as e:  # never die
                log.warning("ccusage poll failed: %s", e)
            if n % COST_EVERY == 0:
                try:
                    cost.refresh(self.db)
                except Exception as e:
                    log.warning("cost refresh failed: %s", e)
            n += 1
            self.stop.wait(POLL_SECONDS)
