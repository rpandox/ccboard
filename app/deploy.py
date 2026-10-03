"""Deploy gate: a container swap never interrupts someone at a terminal.

On ubu2 a deploy is Watchtower stopping the board's container and starting the new image, which under the box's disk load takes
about a minute (the app itself exits within a few seconds; docker's remove/create/start is the rest). Watchtower runs the
pre-update lifecycle hook `scripts/ccboard-deploy-gate` inside the container before every swap; that script asks
POST /api/deploy/gate (hook token, loopback) and exits 75 when the answer is `hold`, which makes Watchtower skip the update
until its next scan (every 5 min). The gate holds while:

  - a full ttyd client is attached to any session (someone is typing in a terminal: the term page, the dock, a quad tile in
    full mode), or
  - a permission is pending (the prompt is waiting for a remote answer), or
  - a clone is in progress;

and never longer than HOLD_MAX after the first ask, nor once someone pressed "Install now" (kv deploy_force). The record of
the pending update lives in kv deploy_pending so the Home banner can say "an update is waiting" with the reasons and a button;
the new container clears both keys at startup (clear()). A record older than STALE is ignored by the UI: a swap that went
ahead but failed on the docker side must not show "installing" forever.
"""
from __future__ import annotations

import time

KV_PENDING = "deploy_pending"
KV_FORCE = "deploy_force"
HOLD_MAX = 30 * 60        # seconds after the first ask: after this the update goes ahead whatever is open
STALE = 10 * 60           # a pending record not refreshed for this long is over (the swap happened or Watchtower gave up)

now = time.time           # patched by tests


def _value(rec) -> dict:
    v = rec.get("value") if isinstance(rec, dict) else None
    return v if isinstance(v, dict) else {}


def reasons(*, viewers: dict | None, pending: list | None, clones: dict | None) -> list[str]:
    """Why the board would rather not restart right now (empty = go ahead)."""
    out = []
    full = sum(int((v or {}).get("full") or 0) for v in (viewers or {}).values())
    if full:
        out.append(f"{full} terminal{'s' if full != 1 else ''} open")
    n = len(pending or [])
    if n:
        out.append(f"{n} permission{'s' if n != 1 else ''} pending")
    q = len((clones or {}).get("queued") or [])
    if q:
        out.append(f"{q} clone{'s' if q != 1 else ''} running")
    return out


def decide(db, *, viewers: dict | None, pending: list | None, clones: dict | None = None, at: float | None = None) -> dict:
    """Answer one pre-update ask: {hold, reasons, since, until, forced}. Records the ask in kv deploy_pending."""
    at = now() if at is None else at
    prev = _value(db.kv_get(KV_PENDING))
    since = prev.get("since") if isinstance(prev.get("since"), (int, float)) and at - prev["since"] < STALE + HOLD_MAX else None
    since = since or at
    forced = db.kv_get(KV_FORCE) is not None
    why = reasons(viewers=viewers, pending=pending, clones=clones)
    hold = bool(why) and not forced and (at - since) < HOLD_MAX
    db.kv_set(KV_PENDING, {"since": since, "last": at, "hold": hold, "reasons": why, "forced": forced,
                           "asks": int(prev.get("asks") or 0) + 1})
    if not hold:
        db.kv_del(KV_FORCE)                   # consumed: the swap goes ahead now
    return {"hold": hold, "reasons": why, "since": since, "until": since + HOLD_MAX, "forced": forced}


def force(db) -> None:
    """'Install now': the next ask answers go whatever is open (Watchtower asks again within its scan interval)."""
    db.kv_set(KV_FORCE, {"at": now()})


def clear(db) -> None:
    """A new container is up: whatever update was pending has happened."""
    db.kv_del(KV_PENDING)
    db.kv_del(KV_FORCE)


def view(db, at: float | None = None) -> dict | None:
    """The state's `deploy` field: None when no update is waiting, else {pending, hold, reasons, since, until, forced, installing,
    minutes_left}."""
    at = now() if at is None else at
    rec = _value(db.kv_get(KV_PENDING))
    last = rec.get("last")
    if not isinstance(last, (int, float)) or at - last > STALE:
        return None
    since = rec.get("since") if isinstance(rec.get("since"), (int, float)) else last
    hold = bool(rec.get("hold"))
    return {"pending": True, "hold": hold, "installing": not hold, "reasons": list(rec.get("reasons") or []), "since": since,
            "until": since + HOLD_MAX, "forced": bool(rec.get("forced")) or db.kv_get(KV_FORCE) is not None,
            "minutes_left": max(0, int((since + HOLD_MAX - at) // 60))}
