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


def _window_reading(db, series: str, key: str) -> dict | None:
    """{used_percentage, resets_at?, at, source} of the newest sample of one window's series, None without a numeric one."""
    last = db.sample_last(series, key)
    v = last.get("value") if last else None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    meta = last.get("meta") if isinstance(last.get("meta"), dict) else {}
    out = {"used_percentage": float(v), "at": last.get("at"),
           "source": "cache" if meta.get("source") == "cache" else "statusline"}
    ra = meta.get("resets_at")
    if isinstance(ra, (int, float)) and not isinstance(ra, bool):
        out["resets_at"] = int(ra)
    return out


def rate_limits_view(db) -> dict | None:
    """state.usage: the kv `rate_limits` record ({value: {five_hour, seven_day, ...}, at}) as the topbar pills and the gauges read it.
    A statusline names only the windows that have something in them (a window that reset and has not been used since is left out), and
    a cache reading may carry one window only, so a window missing from the record is filled in from the newest reading of the
    current account's series ({used_percentage, resets_at, at, source}: the last known number, which the browser turns into 0 % and the
    next reset once its reset has passed). Nothing is added when both windows are there, so the record keeps its shape; None when the
    board has neither a record nor a reading."""
    from . import accounts                                       # late: accounts imports samples lazily, samples registers its hooks
    rec = db.kv_get("rate_limits")
    value = rec.get("value") if isinstance(rec, dict) else None
    value = value if isinstance(value, dict) else {}
    cur = accounts.current(db)
    fill: dict[str, dict] = {}
    for name, series in (("five_hour", "rl_5h"), ("seven_day", "rl_7d")):
        w = value.get(name)
        if isinstance(w, dict) and isinstance(w.get("used_percentage"), (int, float)) and not isinstance(w.get("used_percentage"), bool):
            continue
        got = _window_reading(db, series, accounts.SERIES_PREFIX + cur if cur else "claude")      # 'claude' = the current account before any identity is known
        if got:
            fill[name] = got
    if not fill:
        return rec if isinstance(rec, dict) else None
    at = rec.get("at") if isinstance(rec, dict) and rec.get("at") else max(w["at"] for w in fill.values())
    return {"value": {**value, **fill}, "at": at}


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
