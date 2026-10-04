"""claude-mem health monitor: one daemon thread that probes the memory worker every 20 s and stores the answer in kv `mem_health`.

/api/state's `memory` is that value (memory.state_view), so a state poll never waits on the worker and the worker never sees a
request per poll. The probe itself is app/memory.py's health(): GET only, loopback only, 2 s per request, 3.5 s in all.

Started by the lifespan only when CCBOARD_CLAUDE_MEM is on (settings.claude_mem). Shaped like the other pollers (prpoll.Poller):
a daemon Thread with a `stop` Event; sample_once() is the unit tests call.
"""
from __future__ import annotations

import logging
import threading

from .. import memory

log = logging.getLogger("ccboard.memory")
INTERVAL_SECONDS = 20
START_DELAY = 2.0      # let the board finish starting (and a short-lived process exit) before the first probe


class Monitor(threading.Thread):
    def __init__(self, db, interval: float = INTERVAL_SECONDS, start_delay: float | None = None):
        super().__init__(name="mem-monitor", daemon=True)
        self.db = db
        self.interval = interval
        self.start_delay = START_DELAY if start_delay is None else start_delay
        self.stop = threading.Event()

    def sample_once(self) -> dict:
        """Probe the worker and write the answer to kv `mem_health` (replacing the previous one). Returns what was written."""
        h = memory.health()
        self.db.kv_set(memory.KV_HEALTH, h)
        return h

    def run(self) -> None:
        if self.stop.wait(self.start_delay):
            return
        while not self.stop.is_set():
            try:
                self.sample_once()
            except Exception as e:
                log.warning("memory health sample failed: %s", e)
            self.stop.wait(self.interval)
