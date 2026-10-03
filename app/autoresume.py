"""Auto-continue after a rate-limit reset.

The usage analysis found 'continue' typed 128 times and limit hits clustered at night. The board records every limit episode as a
'lim' sample (key 5h|7d|other, meta {session, resets_at, message}). On the Sampler's tick (every 15 s) this module looks for an
episode whose reset time has passed and whose session is still parked on it, and types `continue` once:

  - the session row is open, its state is errored or idle, its last message still mentions the limit, and nothing happened to it
    after the hit (its state_at is not later than the episode): the person has not moved on;
  - the tmux session exists and nobody is attached to it with a real client (someone at the terminal types for themselves);
  - at least GRACE seconds after resets_at (the window does not open to the second) and at most WINDOW_AFTER later;
  - once per (session, resets_at), remembered in kv autoresume:<session>:<resets_at>.

What it did is an AutoContinue event on the session and one low-priority notification. CCBOARD_AUTO_CONTINUE=0 turns it off;
flags.no_autoresume on a session row opts that session out (a later Settings toggle writes it).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from . import notify, tmux
from .agents.claude import LIMIT_MSG_RE
from .config import settings

log = logging.getLogger("ccboard.autoresume")

GRACE = 45                 # seconds after resets_at before typing
WINDOW_AFTER = 2 * 3600    # seconds after resets_at during which an episode is still acted on
LOOKBACK_DAYS = 8          # a weekly window resets within 7 days
TEXT = "continue"


def _epoch(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v:
        try:
            return float(v)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def episodes(db, now: float) -> list[dict]:
    """Limit episodes of the last LOOKBACK_DAYS: [{session, kind, resets_at, at, message}] oldest first."""
    since = datetime.fromtimestamp(now - LOOKBACK_DAYS * 86400, tz=timezone.utc).isoformat(timespec="seconds")
    out = []
    for at_iso, key, _value, meta_json in db.samples_query("lim", None, since, None):
        try:
            meta = json.loads(meta_json) if isinstance(meta_json, str) else (meta_json or {})
        except ValueError:
            meta = {}
        if not isinstance(meta, dict) or not meta.get("session"):
            continue
        out.append({"session": str(meta["session"]), "kind": key, "resets_at": _epoch(meta.get("resets_at")), "at": _epoch(at_iso),
                    "message": str(meta.get("message") or "")})
    return out


def _still_parked(row: dict, ep: dict) -> bool:
    if (row.get("state") or "") not in ("errored", "idle"):
        return False
    if (row.get("flags") or {}).get("no_autoresume"):
        return False
    if not LIMIT_MSG_RE.search((row.get("last_message") or "")[-500:]):
        return False
    state_at = _epoch(row.get("state_at"))
    return not state_at or state_at <= ep["at"] + 60          # no state change after the hit


def tick(db, now: float | None = None, *, send=None, clients=None, alive=None) -> list[str]:
    """One pass. Returns the sessions continued this tick."""
    if not settings.auto_continue:
        return []
    now = time.time() if now is None else now
    send = send or (lambda name, text: tmux.send_text(name, text, enter=True))
    clients = clients or tmux.real_clients
    alive = alive or tmux.has_session
    opened = db.open_rows()                                   # a dict keyed by tmux name (a list in older shapes)
    rows = opened if isinstance(opened, dict) else {r["tmux_name"]: r for r in opened}
    done: list[str] = []
    for ep in episodes(db, now):
        if not ep["resets_at"] or now < ep["resets_at"] + GRACE or now > ep["resets_at"] + WINDOW_AFTER:
            continue
        key = f"autoresume:{ep['session']}:{int(ep['resets_at'])}"
        if db.kv_get(key) is not None:
            continue
        row = rows.get(ep["session"])
        if row is None or not _still_parked(row, ep):
            db.kv_set(key, {"skipped": "moved on" if row else "no open session", "at": now})
            continue
        try:
            if not alive(ep["session"]):
                db.kv_set(key, {"skipped": "tmux session gone", "at": now})
                continue
            if clients(ep["session"]) > 0:
                continue                                        # someone is at the terminal: try again next tick, within the window
            send(ep["session"], TEXT)
        except Exception as e:                                  # tmux hiccup: try again next tick
            log.warning("autoresume %s: %s", ep["session"], e)
            continue
        db.kv_set(key, {"continued": True, "at": now, "kind": ep["kind"]})
        db.add_event(ep["session"], "AutoContinue", ep["kind"], f"typed '{TEXT}' after the {ep['kind']} limit reset", {"resets_at": ep["resets_at"]})
        done.append(ep["session"])
        try:
            parts = tmux.split_name(ep["session"])
            where = f"{parts[0]}/{parts[1]} · {parts[2]}"
        except ValueError:
            where = ep["session"]
        notify.publish(f"◆ {where}: continued after the {ep['kind']} limit reset", f"the board typed '{TEXT}' for you", priority=3, tags=["arrow_forward"],
                       click=(settings.public_url + f"/#/s/{ep['session']}") if settings.public_url else None)
        log.info("autoresume: continued %s after the %s limit reset", ep["session"], ep["kind"])
    return done
