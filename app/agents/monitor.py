"""claude-mem health monitor: one daemon thread that probes the memory worker every 20 s and stores the answer in kv `mem_health`.

/api/state's `memory` is that value (memory.state_view), so a state poll never waits on the worker and the worker never sees a
request per poll. The probe itself is app/memory.py's health(): GET only, loopback only, 2 s per request, 3.5 s in all.

Each sample is enriched (enrich()) before it is stored (v0.5.20, the Memory health tile): the worker's total observations and summaries
go into the `mem_obs` and `mem_sum` series (app/samples.py), and the record gains
    rates           {obs: {d1, d7}, sum: {d1, d7}}: per-day increase over the last 24 h and 7 days from those board samples; None
                    ("collecting") while the samples span less than an hour (24 h) or a day (7 days); a counter that drops (a reset
                    database) never makes a rate negative
    rates_since     the oldest sample used (ISO), None without samples
    plugin_version  the claude-mem version installed on disk (the running worker's is `version`; they differ until it restarts)
    compat, tested_worker   memory_proxy.compat(version): ok | untested | unknown against the version the proxy was verified with

Started by the lifespan only when CCBOARD_CLAUDE_MEM is on (settings.claude_mem). Shaped like the other pollers (prpoll.Poller):
a daemon Thread with a `stop` Event; sample_once() is the unit tests call.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from .. import memory, samples

log = logging.getLogger("ccboard.memory")
INTERVAL_SECONDS = 20
START_DELAY = 2.0      # let the board finish starting (and a short-lived process exit) before the first probe
RATE_WINDOWS = (("d1", 86400, 3600), ("d7", 7 * 86400, 86400))     # (name, window seconds, the least span of samples for a rate)
SERIES = (("obs", "mem_obs", "observations"), ("sum", "mem_sum", "summaries"))


def _ts(at: str) -> float:
    return datetime.fromisoformat(at).timestamp()


def rate(db, series: str, window: float, min_span: float, now: float | None = None) -> tuple[float | None, str | None]:
    """(per-day increase of a counter series over the last `window` seconds, the oldest sample's time). The increase sums the positive
    steps between consecutive samples, so a counter that drops and grows again (a reset) is never negative. None while the samples
    span less than `min_span` seconds."""
    now = time.time() if now is None else now
    rows = db.samples_query(series, [""], datetime.fromtimestamp(now - window, timezone.utc))
    pts = [(_ts(at), v) for at, _k, v, _m in rows if isinstance(v, (int, float))]
    if len(pts) < 2:
        return None, (rows[0][0] if rows else None)
    span = pts[-1][0] - pts[0][0]
    if span < min_span:
        return None, rows[0][0]
    inc = sum(max(0.0, b[1] - a[1]) for a, b in zip(pts, pts[1:]))
    return round(inc / span * 86400, 1), rows[0][0]


def enrich(db, h: dict, now: float | None = None) -> dict:
    """Write the sample's counters into mem_obs / mem_sum and add rates, plugin_version and compat to `h` (in place; returned). Never
    raises: a failure leaves the plain health record."""
    try:
        for _name, series, field in SERIES:
            v = h.get(field)
            if isinstance(v, int) and not isinstance(v, bool):
                samples.record(db, series, "", v, at=now)
        rates, since = {}, None
        for name, series, _field in SERIES:
            rates[name] = {}
            for win, secs, min_span in RATE_WINDOWS:
                r, first = rate(db, series, secs, min_span, now)
                rates[name][win] = r
                if win == "d7" and first and (since is None or first < since):
                    since = first
        h["rates"], h["rates_since"] = rates, since
    except Exception as e:
        log.debug("memory rates failed: %s", e.__class__.__name__)
    try:
        h["plugin_version"] = memory.plugin_status().get("version")
    except Exception:
        h["plugin_version"] = None
    try:
        from .. import memory_proxy
        h.update(compat=memory_proxy.compat(h.get("version")), tested_worker=memory_proxy.TESTED_WORKER)
    except Exception as e:
        log.debug("memory compat failed: %s", e.__class__.__name__)
    return h


def sample(db) -> dict:
    """One probe, enriched, stored in kv `mem_health` (replacing the previous one). Returns what was stored."""
    h = enrich(db, memory.health())
    db.kv_set(memory.KV_HEALTH, h)
    return h


class Monitor(threading.Thread):
    def __init__(self, db, interval: float = INTERVAL_SECONDS, start_delay: float | None = None):
        super().__init__(name="mem-monitor", daemon=True)
        self.db = db
        self.interval = interval
        self.start_delay = START_DELAY if start_delay is None else start_delay
        self.stop = threading.Event()

    def sample_once(self) -> dict:
        """Probe the worker and write the answer to kv `mem_health` (replacing the previous one). Returns what was written."""
        return sample(self.db)

    def run(self) -> None:
        if self.stop.wait(self.start_delay):
            return
        while not self.stop.is_set():
            try:
                self.sample_once()
            except Exception as e:
                log.warning("memory health sample failed: %s", e)
            self.stop.wait(self.interval)
