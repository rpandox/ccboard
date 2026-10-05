"""Subscription accounts: who the board's Claude sessions are logged in as, and which account a rate-limit reading belongs to.

A Claude subscription is measured in windows (the 5-hour and the 7-day percentage), and the person may use more than one account
(`/login` to another one today, several config dirs later). The statusline payload names no account, so the board works it out:

  identity     read_identity(): the `oauthAccount` object of Claude's state file (`<config dir>/.claude.json`, or `<dir>.json` next to
               the dir: `~/.claude.json` for the default `~/.claude`) first, `claude auth status` (claude_auth.status(), cached) as the
               fallback. Only the whitelisted identity fields of `oauthAccount` are ever read out of the file (the file is Claude's whole
               state and large); the credentials file is never opened. Cached 60 s per config dir; never raises.
  current      observe(): the Sampler's tick compares the identity with the kv `account_current` and, on a change, updates it and the kv
               `accounts`, and writes an `acct` event sample (key = the new account, meta {from, to}).
  attribution  for_reading(): the pair (five_hour.resets_at, seven_day.resets_at) is a fingerprint of the account behind a reading
               (two accounts have different reset instants). An account whose remembered 5 h or 7 d reset matches the reading's
               (equal, or a whole number of windows apart: n x 18000 s / n x 604800 s, within 60 s) owns it, so a session still
               running on account A's token keeps reporting A after the shared login moved to B. No match: the current account owns it
               and its fingerprint is replaced by the reading's.

usage cache  poll_usage_cache(): the second reading source, credential-free. Claude Code keeps `cachedUsageUtilization` ({fetchedAtMs,
               accountUuid, utilization: {five_hour|seven_day: {utilization 0..100, resets_at ISO}}}) in the same state file. On the
               Sampler tick (one stat; a parse only when the file's stamp changed) a cache newer than the newest reading the board holds
               for that account is fed through the statusline's own path (record_statusline with meta source 'cache', the kv
               `rate_limits` of the current account, the account's fingerprint). Only those keys are read out of the file; nothing of
               it is logged. A session's statusline stays the live source: its readings are newer, so the cache never overrides them.

kv shapes (the contract slice B and the Usage page read):
  accounts         {key: {key, email, name, org, org_id, plan, tier, config_dir, first_seen, last_seen, label,
                          resets_5h, resets_7d}}      first_seen / last_seen ISO UTC; resets_* epoch seconds (the fingerprint);
                                                     label is the person's own name for the account (PATCH /api/accounts/{key}), else None
  account_current  {key, since, config_dir}

Account key: oauthAccount.accountUuid, else its organizationUuid, else the email, else 'unknown'. The auth-status fallback knows no
account uuid, so it keys by email; an existing record with the same email is reused whichever form the key came in, so an account
whose state file is briefly unreadable does not split into two.

saved logins  app/account_store.py keeps one directory of credentials per account and switches the live login between them. It needs
               three things of this module: remember() (an account the board knows but is not logged in as: a record without a
               current-account change), adopt() (the switch itself: current moves and the `acct` event is written at once) and hold()
               (for 90 s after a switch a running Claude session may write its stale `oauthAccount` back into the state file; the hold
               makes observe() read that as the new account instead of as a switch back).

Nothing here runs on the request path except for_reading (kv reads, no file or process access except one forced identity re-read
when a reading matches no fingerprint) and the views (kv and the in-memory sample_last cache). A forced re-read is decided by the
state file's stamp (path, mtime, size, inode of both candidate files), not by the clock: unchanged stamp = one stat and the cached
identity, which is then known to be current; changed stamp = parse again, however recently the last read was. A switch is therefore
seen the moment the first reading of the new account arrives.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import claude_auth
from .config import settings
from .db import iso

log = logging.getLogger("ccboard.accounts")

KV_ACCOUNTS = "accounts"
KV_CURRENT = "account_current"
UNKNOWN = "unknown"
SERIES_PREFIX = "acct:"                     # the per-account rl_5h / rl_7d series key is 'acct:<account key>'
IDENTITY_TTL = 60.0                         # seconds a config dir's identity is cached (the state file is rewritten constantly)
FORCE_MIN = 5.0                             # a forced read never re-runs the auth fallback (or retries a bad file) more often than this
MAX_FILE_BYTES = 64 * 1024 * 1024
SEEN_EVERY = 300                            # last_seen is rewritten at most this often (seconds)
TOLERANCE = 60                              # seconds a reset time may differ from "a whole number of windows later"
PERIOD_5H = 5 * 3600
PERIOD_7D = 7 * 86400
MAX_5H_ROLLOVERS = 6                        # 5 h windows chain only while the person keeps working: beyond 30 h a match is luck
LABEL_MAX = 60
FIELD_MAX = 200
SOURCE_STATUSLINE, SOURCE_CACHE = "statusline", "cache"    # where a reading came from (sample meta `source`; absent = the statusline)
USAGE_KEY = "cachedUsageUtilization"        # the one other key of Claude's state file the board reads (poll_usage_cache)

_lock = threading.RLock()                   # kv accounts / account_current read-modify-write (sampler tick, hook threads, PATCH)
# config dir -> (monotonic time, identity, auth fallback was allowed, state file stamp when it was read, the file read cleanly)
_cache: dict[str, tuple[float, dict | None, bool, tuple | None, bool]] = {}
_clock = time.monotonic                     # patched by tests
_hold: dict | None = None                   # {to, frm, until}: see hold(); in memory only (a restart drops it, the kv is already right)
_ucache: dict[str, tuple[tuple, dict | None]] = {}    # config dir -> (state file stamp, the usage cache read at that stamp): the tick's gate
_fed: dict[str, int] = {}                   # account key ('' = none known) -> the fetchedAt (epoch s) of the cache reading last fed


# ------------------------------------------------------------------ small helpers
def _s(v, n: int = FIELD_MAX) -> str | None:
    """A trimmed, length-capped string, None for anything else or an empty one."""
    if not isinstance(v, str):
        return None
    t = v.strip()
    return t[:n] if t else None


def _int(v) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return int(v) if v == v and abs(v) != float("inf") else None
    except (OverflowError, ValueError):
        return None


def _to_epoch(now=None) -> float:
    """Epoch seconds from None (now), a number, a datetime or an ISO text."""
    if now is None:
        return time.time()
    if isinstance(now, bool):
        return time.time()
    if isinstance(now, (int, float)):
        return float(now)
    return datetime.fromisoformat(iso(now)).timestamp()


def _loads_kv(db, key: str):
    rec = db.kv_get(key)
    v = rec.get("value") if isinstance(rec, dict) else None
    return v


def _load(db) -> dict[str, dict]:
    """The kv `accounts` record as {key: record}; anything that is not a dict of dicts counts as empty."""
    v = _loads_kv(db, KV_ACCOUNTS)
    if not isinstance(v, dict):
        return {}
    return {str(k): dict(r) for k, r in v.items() if isinstance(r, dict)}


def current(db) -> str | None:
    """The key of the account the board last saw logged in (kv account_current), None until observe() has read an identity."""
    v = _loads_kv(db, KV_CURRENT)
    k = v.get("key") if isinstance(v, dict) else None
    return k if isinstance(k, str) and k else None


# ------------------------------------------------------------------ identity
def _plan_from_file(oa: dict) -> str | None:
    seat = _s(oa.get("seatTier"), 40)
    if seat:
        return seat
    org_type = _s(oa.get("organizationType"), 60)
    if org_type:
        t = org_type.lower()
        return t[len("claude_"):] if t.startswith("claude_") else t      # 'claude_max' -> 'max'
    return _s(oa.get("billingType"), 40)


def _candidates(d: Path) -> list[Path]:
    """Where Claude keeps its state file for config dir `d`: `~/.claude.json` for the default `~/.claude`, `<dir>/.claude.json` for a
    CLAUDE_CONFIG_DIR; each is also tried the other way round (a `<dir>.json` beside the dir)."""
    inside, beside = d / ".claude.json", d.parent / (d.name + ".json")
    return [beside, inside] if d == Path.home() / ".claude" else [inside, beside]


def _from_file(d: Path) -> tuple[str, dict | None]:
    """('ok', identity) | ('none', None) no file or no oauthAccount in it | ('bad', None) a file that could not be read or parsed (it is
    rewritten constantly, so a half-written one is normal and must not read as 'logged out'). When both candidate files name an
    account the newer file wins (CLAUDE_CONFIG_DIR set to the default dir keeps its state inside the dir; unset, beside it)."""
    verdict = "none"
    best: tuple[int, dict] | None = None
    for path in _candidates(d):
        try:
            st = path.stat()
            if st.st_size > MAX_FILE_BYTES:
                verdict = "bad"
                continue
            with open(path, "rb") as f:
                data = json.loads(f.read())
        except FileNotFoundError:
            continue
        except (OSError, ValueError, RecursionError, MemoryError):      # RecursionError: a pathologically nested file is a bad file
            verdict = "bad"
            continue
        oa = data.get("oauthAccount") if isinstance(data, dict) else None
        del data                                                # nothing but the identity object leaves this function
        if not isinstance(oa, dict):
            continue
        email, org_id = _s(oa.get("emailAddress")), _s(oa.get("organizationUuid"), 80)
        key = _s(oa.get("accountUuid"), 80) or org_id or email
        if not key:
            continue
        ident = {"key": key, "email": email, "name": _s(oa.get("displayName")), "org": _s(oa.get("organizationName")),
                 "org_id": org_id, "plan": _plan_from_file(oa),
                 "tier": _s(oa.get("userRateLimitTier"), 80) or _s(oa.get("organizationRateLimitTier"), 80),
                 "config_dir": str(d), "source": "file"}
        if best is None or st.st_mtime_ns > best[0]:
            best = (st.st_mtime_ns, ident)
    return ("ok", best[1]) if best else (verdict, None)


def _from_auth(d: Path) -> dict | None:
    """The identity `claude auth status` reports (claude_auth.status(), cached 60 s there): only for the board's own config dir, and
    only for a logged-in account it can name (an email: that status carries no uuid)."""
    if d != settings.claude_config_dir:
        return None
    try:
        st = claude_auth.status()
    except Exception as e:
        log.debug("auth status unavailable: %s", e)
        return None
    if not isinstance(st, dict) or not st.get("loggedIn"):
        return None
    email = _s(st.get("email"))
    if not email:
        return None
    return {"key": email, "email": email, "name": None, "org": _s(st.get("orgName")), "org_id": None,
            "plan": _s(st.get("subscriptionType"), 40), "tier": None, "config_dir": str(d), "source": "auth"}


def _stamp(d: Path) -> tuple:
    """What the state file(s) of config dir `d` look like right now: (path, mtime_ns, size, inode) per candidate, (path, None) for one
    that is absent. Two stats, no read. Size and inode ride along because a coarse filesystem clock can give two different writes one
    mtime, and Claude replaces the file by rename."""
    out = []
    for path in _candidates(d):
        try:
            st = path.stat()
            out.append((str(path), st.st_mtime_ns, st.st_size, st.st_ino))
        except (OSError, ValueError):
            out.append((str(path), None))
    return tuple(out)


def _identity(config_dir, force: bool, auth: bool) -> tuple[dict | None, bool]:
    """(identity, fresh) for read_identity. `fresh` is True when this call confirmed the answer against the state file as it is now (it
    parsed the file, or found its stamp unchanged since a clean parse) and False for an answer taken on trust: a cache hit inside the
    60 s, or the last answer kept because the file could not be read."""
    d = Path(config_dir) if config_dir else Path(settings.claude_config_dir)
    k = str(d)
    now = _clock()
    with _lock:
        hit = _cache.get(k)
    usable = bool(hit) and (hit[1] is not None or hit[2] or not auth)
    stamp = _stamp(d)                                           # before the parse: a write during it shows up as a change next time
    if usable and not force:
        if now - hit[0] < IDENTITY_TTL:
            return (dict(hit[1]) if hit[1] else None), False
    elif usable and stamp == hit[3]:
        age = now - hit[0]
        file_answer = hit[1] is not None and hit[1].get("source") == "file"
        if (hit[4] and file_answer) or age < FORCE_MIN or not auth:
            # nothing was written since the last read: the cached answer is the file's answer (one stat). Only what the file cannot
            # settle (no account in it: the auth fallback may know more; unreadable: retry) waits FORCE_MIN before another look
            return (dict(hit[1]) if hit[1] else None), hit[4]
    status, ident = _from_file(d)
    if status == "bad":
        ident = dict(hit[1]) if hit and hit[1] else None
    elif status == "none" and auth:
        ident = _from_auth(d)
    elif status == "none" and hit and hit[1] and hit[1].get("source") == "auth":
        ident = dict(hit[1])                                    # a file-only read must not forget what the fallback found
    with _lock:
        _cache[k] = (now, ident, bool(auth) or status == "ok" or bool(hit and hit[2]), stamp, status != "bad")
    return (dict(ident) if ident else None), status != "bad"


def read_identity(config_dir=None, *, force: bool = False, auth: bool = True) -> dict | None:
    """The account behind a config dir (default: the board's, settings.claude_config_dir) as {key, email, name, org, org_id, plan, tier,
    config_dir, source: 'file' | 'auth'}, or None when it cannot be told (not logged in, no readable state). The state file is
    consulted first and `claude auth status` (when `auth`) only when the file names no account. A state file that exists but cannot be
    read keeps the previous answer instead of falling back (the fallback would key the same account differently). Cached 60 s per
    config dir. `force` is for the moment a switch is suspected: it re-parses whenever the state file changed since the last read (its
    stamp differs, however recent that read was) and otherwise returns the cached answer after one stat, which is then known to be
    current; only the auth fallback and a retry of an unreadable file keep a FORCE_MIN pause. `auth=False` is for the request path
    (never starts a process) and does not poison the cache for a caller that allows the fallback. Never raises, never logs the file's
    content."""
    try:
        return _identity(config_dir, force, auth)[0]
    except Exception as e:
        log.warning("read_identity failed: %s", e.__class__.__name__)
        return None


def invalidate() -> None:
    """Forget every cached identity (the next read goes to the file) and what the usage-cache poll remembers."""
    with _lock:
        _cache.clear()
        _ucache.clear()
        _fed.clear()


# ------------------------------------------------------------------ Claude Code's usage cache (the second reading source)
def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if f == f and abs(f) != float("inf") else None


def _iso_epoch(v) -> int | None:
    """Epoch seconds of an ISO time ('2026-10-05T12:30:00.123456+00:00', 'Z' accepted; naive = UTC) or of a number; None otherwise."""
    n = _num(v)
    if n is not None:
        return int(n) if n > 0 else None
    if not isinstance(v, str) or not v.strip():
        return None
    t = v.strip()
    try:
        d = datetime.fromisoformat(t[:-1] + "+00:00" if t[-1] in "Zz" else t)
        return int((d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp())
    except (ValueError, OverflowError, OSError):
        return None


def _cache_windows(util) -> dict[str, dict]:
    """{five_hour: {used_percentage, resets_at}, seven_day: ...} (the statusline's own shape and units: percent used, epoch seconds)
    out of the cache's `utilization` object, for the windows that carry a number; resets_at is left out when the cache has none."""
    out: dict[str, dict] = {}
    if not isinstance(util, dict):
        return out
    for name in ("five_hour", "seven_day"):
        w = util.get(name)
        used = _num(w.get("utilization")) if isinstance(w, dict) else None
        if used is None:
            continue
        rec: dict = {"used_percentage": max(0.0, min(100.0, used))}
        resets = _iso_epoch(w.get("resets_at"))
        if resets is not None:
            rec["resets_at"] = resets
        out[name] = rec
    return out


def _usage_from_file(d: Path) -> tuple[str, dict | None]:
    """('ok', {at, account, windows}) | ('none', None) no file, or none with a usable cache | ('bad', None) a file that could not be read
    or parsed (half-written is normal). `at` is the cache's fetchedAtMs as epoch seconds, `account` its accountUuid (or None). Only
    `cachedUsageUtilization` is taken out of the file and only its whitelisted fields are copied; the rest of the (large, private)
    state is dropped unread, and nothing of it reaches a log. With two candidate files the one with the newer cache wins."""
    verdict = "none"
    best: dict | None = None
    for path in _candidates(d):
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                verdict = "bad"
                continue
            with open(path, "rb") as f:
                data = json.loads(f.read())
        except FileNotFoundError:
            continue
        except (OSError, ValueError, RecursionError, MemoryError):
            verdict = "bad"
            continue
        uc = data.get(USAGE_KEY) if isinstance(data, dict) else None
        del data
        if not isinstance(uc, dict):
            continue
        ms = _num(uc.get("fetchedAtMs"))
        windows = _cache_windows(uc.get("utilization"))
        if ms is None or ms <= 0 or not windows:
            continue
        found = {"at": int(ms // 1000), "account": _s(uc.get("accountUuid"), 80), "windows": windows}
        if best is None or found["at"] > best["at"]:
            best = found
    return ("ok", best) if best else (verdict, None)


def read_usage_cache(config_dir=None) -> dict | None:
    """Claude Code's own usage cache of a config dir (default: the board's) as {at, account, windows}, or None (see _usage_from_file).
    Gated by the state file's stamp (path, mtime, size, inode of both candidates: two stats): an unchanged stamp answers from memory,
    a changed one parses the file again. A file that could not be read keeps the last answer and is retried next time. Never raises."""
    try:
        d = Path(config_dir) if config_dir else Path(settings.claude_config_dir)
        k = str(d)
        stamp = _stamp(d)                                       # before the parse: a write during it shows up as a change next time
        with _lock:
            hit = _ucache.get(k)
        if hit and hit[0] == stamp:
            return dict(hit[1]) if hit[1] else None
        status, found = _usage_from_file(d)
        if status == "bad":
            return dict(hit[1]) if hit and hit[1] else None
        with _lock:
            _ucache[k] = (stamp, found)
        return dict(found) if found else None
    except Exception as e:
        log.warning("read_usage_cache failed: %s", e.__class__.__name__)
        return None


def _newest_reading(db, key: str | None, is_current: bool) -> float:
    """Epoch seconds of the newest reading of either window the board holds for the account: its per-account series, and for the
    current account also the 'claude' series and the kv `rate_limits`. 0 when there is none."""
    stamps: list[str] = []
    for series in ("rl_5h", "rl_7d"):
        for k in ([SERIES_PREFIX + key] if key else []) + (["claude"] if is_current else []):
            last = db.sample_last(series, k)
            if last and last.get("at"):
                stamps.append(last["at"])
    if is_current:
        rec = db.kv_get("rate_limits")
        if isinstance(rec, dict) and rec.get("at"):
            stamps.append(rec["at"])
    best = 0.0
    for t in stamps:
        try:
            best = max(best, datetime.fromisoformat(t).timestamp())
        except (ValueError, TypeError):
            continue
    return best


def poll_usage_cache(db, now=None) -> bool:
    """The Sampler's tick hook: feed Claude Code's usage cache to the board when it is newer than what the board holds. The account is
    the one whose key equals the cache's accountUuid, else the current account. The reading goes the statusline's way
    (samples.record_statusline: the account's series, and the 'claude' series when it is the current account; meta source 'cache'),
    stamped with the cache's own fetch time (never in the future), the account's reset fingerprint moves to it, and for the current
    account the kv `rate_limits` follows (the pills read it; keys the cache does not carry, like spend_limit, are kept). A reading
    already fed is never fed again. Returns whether anything was fed. Never raises."""
    try:
        return _poll(db, _to_epoch(now))
    except Exception as e:
        log.warning("accounts.poll_usage_cache failed: %s", e.__class__.__name__)
        return False


def _poll(db, t: float) -> bool:
    cache = read_usage_cache()
    if not cache:
        return False
    at = int(min(cache["at"], t))
    with _lock:
        accts, cur = _load(db), current(db)
    by_uuid = cache["account"] in accts if cache["account"] else False
    acct = cache["account"] if by_uuid else cur
    is_current = acct is None or acct == cur
    if at <= max(_newest_reading(db, acct, is_current), _fed.get(acct or "", 0)):
        return False
    rl = cache["windows"]
    from . import samples                                       # late: samples registers this module's hooks at import
    samples.record_statusline(db, "", {"rate_limits": rl}, "claude", at=at, account=acct, current=is_current, source="cache")
    r5, r7 = _resets(rl)
    with _lock:
        accts = _load(db)
        rec = accts.get(acct) if acct else None
        if rec is not None and _remember(rec, r5, r7, replace=by_uuid):
            accts[acct] = rec
            db.kv_set(KV_ACCOUNTS, accts)
    if is_current:
        old = _loads_kv(db, "rate_limits")
        extras = {k: v for k, v in old.items() if k not in ("five_hour", "seven_day", "source")} if isinstance(old, dict) else {}
        db.kv_set("rate_limits", {**extras, **rl, "source": "cache"}, at=at)
    _fed[acct or ""] = at
    return True


# ------------------------------------------------------------------ current account
def _key_for(accts: dict[str, dict], ident: dict) -> str:
    """The record key for an identity: its own key when known, else the key of a record with the same email, else the new key."""
    key = ident["key"]
    if key in accts:
        return key
    email = ident.get("email")
    if email:
        same = [r for r in accts.values() if r.get("email") == email and isinstance(r.get("key"), str)]
        if same:
            return max(same, key=lambda r: str(r.get("last_seen") or ""))["key"]
    return key


def _apply_identity(db, ident: dict, now) -> str:
    ts = iso(now)
    with _lock:
        accts = _load(db)
        key = _key_for(accts, ident)
        prev = current(db)
        rec = dict(accts.get(key) or {"key": key, "first_seen": ts, "label": None, "last_seen": None})
        dirty = key not in accts
        for f in ("email", "name", "org", "org_id", "plan", "tier", "config_dir"):
            v = ident.get(f)
            if v is not None and rec.get(f) != v:
                rec[f] = v
                dirty = True
            rec.setdefault(f, None)
        seen = rec.get("last_seen")
        try:
            stale = not seen or (_to_epoch(ts) - datetime.fromisoformat(seen).timestamp()) >= SEEN_EVERY
        except (ValueError, TypeError):
            stale = True
        if stale or key != prev:
            rec["last_seen"] = ts
            dirty = True
        if dirty:
            accts[key] = rec
            db.kv_set(KV_ACCOUNTS, accts)
        if key != prev:
            db.kv_set(KV_CURRENT, {"key": key, "since": ts, "config_dir": ident.get("config_dir")})
            from . import samples                                   # late: samples registers observe() as a tick hook at import
            samples.record(db, "acct", key, 1, {"from": prev, "to": key} if prev else {"to": key}, at=ts, force=True)
    return key


def _observe(db, now, force: bool, auth: bool) -> tuple[str | None, bool]:
    """observe() plus whether the identity it acted on was fresh (see _identity); (None, False) when nothing could be read."""
    try:
        ident, fresh = _identity(None, force, auth)
        if not ident:
            return None, False
        held = _held_for(db, ident)
        if held:
            return held, fresh                                  # a stale copy of the account a switch just left: not a switch back
        return _apply_identity(db, ident, now), fresh
    except Exception as e:
        log.warning("accounts.observe failed: %s", e.__class__.__name__)
        return None, False


def observe(db, now=None, *, force: bool = False, auth: bool = True) -> str | None:
    """Look at who is logged in now and keep the kv in step (the Sampler's tick calls it with the epoch time): the account record is
    created or refreshed (identity fields, last_seen at most every 5 minutes), and when the account differs from kv account_current
    that is updated and an `acct` event written (value 1, key = the new account, meta {from, to}; the first one has no `from`).
    `force` re-reads the identity if the state file changed (see read_identity). Returns the current key, None when no identity can be
    read (nothing is changed then). Never raises."""
    return _observe(db, now, force, auth)[0]


def hold(to: str, frm: str | None, seconds: float = 90) -> None:
    """After a switch from `frm` to `to` (record keys): for `seconds` a state file whose identity still names `frm` reads as `to` in
    observe() (nothing is applied, `to` is returned), because a Claude session that was running before the switch can write its own
    stale `oauthAccount` back. An identity naming `to` is applied as usual and keeps the hold; any other identity (a real `/login` to
    a third account) drops the hold and is applied normally. In memory only."""
    global _hold
    with _lock:
        _hold = {"to": to, "frm": frm, "until": _clock() + float(seconds)}


def held() -> dict | None:
    """The hold in force right now as {to, frm}, None when there is none (or it ran out). For the saved-login sync, which must not
    copy the live credentials into the account a switch has just left."""
    global _hold
    with _lock:
        h = _hold
        if h and _clock() >= h["until"]:
            _hold = h = None
        return {"to": h["to"], "frm": h["frm"]} if h else None


def _held_for(db, ident: dict) -> str | None:
    """`to` when a hold is in force and `ident` names the account it moved away from, else None (dropping the hold when the identity is
    a third account)."""
    global _hold
    with _lock:
        h = _hold
        if h and _clock() >= h["until"]:
            _hold = h = None
        if not h:
            return None
        key = _key_for(_load(db), ident)
        if h["frm"] is not None and key == h["frm"]:
            return h["to"]
        if key != h["to"]:
            _hold = None
        return None


def adopt(db, ident: dict, now=None) -> str:
    """Make the account behind `ident` the current one at once (what observe() does when it sees a switch): the record is created or
    refreshed, kv account_current moves and the `acct` event is written when the account changed. Returns the canonical key."""
    return _apply_identity(db, ident, now)


def remember(db, ident: dict, now=None) -> str:
    """Create or refresh the record of an account the board knows but is not logged in as (a login added from Settings): identity fields
    and first_seen only, kv account_current untouched and no `acct` event. A new record's last_seen is the moment it was added; an
    existing record keeps its own. Returns the canonical key."""
    ts = iso(now)
    with _lock:
        accts = _load(db)
        key = _key_for(accts, ident)
        rec = dict(accts.get(key) or {"key": key, "first_seen": ts, "label": None, "last_seen": ts})
        dirty = key not in accts
        for f in ("email", "name", "org", "org_id", "plan", "tier", "config_dir"):
            v = ident.get(f)
            if v is not None and rec.get(f) != v:
                rec[f] = v
                dirty = True
            rec.setdefault(f, None)
        if dirty:
            accts[key] = rec
            db.kv_set(KV_ACCOUNTS, accts)
    return key


# ------------------------------------------------------------------ attribution
def _resets(rate_limits) -> tuple[int | None, int | None]:
    """(five_hour.resets_at, seven_day.resets_at) of a statusline `rate_limits` dict as epoch ints (None for what is missing)."""
    rl = rate_limits if isinstance(rate_limits, dict) else {}
    out = []
    for name in ("five_hour", "seven_day"):
        w = rl.get(name)
        out.append(_int(w.get("resets_at")) if isinstance(w, dict) else None)
    return out[0], out[1]


def _congruent(known, got, period: int, max_n: int | None = None) -> bool:
    """Is `got` the same window as `known` or a whole number of windows later or earlier (within the tolerance)?"""
    if known is None or got is None:
        return False
    d = got - known
    n = round(d / period)
    if max_n is not None and abs(n) > max_n:
        return False
    return abs(d - n * period) <= TOLERANCE


def _match(accts: dict[str, dict], r5, r7, cur) -> str | None:
    """The account whose remembered fingerprint the reading (r5, r7) matches. A 7 d match beats a 5 h one (the weekly instant is fixed
    per account; a 5 h window starts with the first message after the last one ran out, so it chains only while work continues). A 5 h
    match does not count when both sides know a 7 d reset and they disagree: that is another account whose 5 h grid happens to line
    up. Several candidates: the current account, else the one seen most recently."""
    by7, by5 = [], []
    for k, rec in accts.items():
        f5, f7 = _int(rec.get("resets_5h")), _int(rec.get("resets_7d"))
        if _congruent(f7, r7, PERIOD_7D):
            by7.append(k)
        elif _congruent(f5, r5, PERIOD_5H, MAX_5H_ROLLOVERS) and not (f7 is not None and r7 is not None):
            by5.append(k)
    pick = by7 or by5
    if not pick:
        return None
    if cur in pick:
        return cur
    return max(pick, key=lambda k: str(accts[k].get("last_seen") or ""))


def _remember(rec: dict, r5, r7, *, replace: bool) -> bool:
    """Store the reading's reset times on the account's record: the later one of the old and new (a window's reset only moves
    forward; an older reading is a staler session's), or the reading's own when `replace` (nothing matched: a new window)."""
    changed = False
    for f, got in (("resets_5h", r5), ("resets_7d", r7)):
        if got is None:
            continue
        old = _int(rec.get(f))
        new = got if replace or old is None else max(old, got)
        if new != old:
            rec[f] = new
            changed = True
    return changed


# The fingerprint is a heuristic with known limits, recorded here rather than papered over: it assumes an account's reset instants stay
# on a fixed grid (n x 18000 s for the 5 h window, n x 604800 s for the 7 d one). (1) Reset times are hour-aligned, so two unrelated
# accounts share a weekly slot with a chance of about 1 in 168; then both match and the current account wins, which is the behaviour
# without account tracking. (2) If a NON-current account's window rolls off its grid (a new window after a pause) while one of its
# sessions still reports it, those readings match nothing, go to the current account and replace its fingerprint, so the pills can
# flicker until the next matching reading. Exposure is low while all sessions share one config dir. Once the launcher gives each
# account its own config dir, the account comes from the config dir the session runs in and the fingerprint is not needed.
def for_reading(db, rate_limits, now=None, *, remember: bool = True) -> str:
    """The account key a statusline `rate_limits` reading belongs to (see the module docstring): the account whose remembered reset
    times it matches, else the current account (and the reading's reset times become that account's fingerprint), else 'unknown'.
    When nothing matches the identity is re-read from the state file first (`force`: a changed file is parsed at once, an unchanged
    one costs a stat), because the usual cause is a switch the 15 s tick has not seen yet: without that the new account's first
    reading would overwrite the old account's fingerprint. If that re-read could not be confirmed against the file (it is unreadable
    just now), the reading still goes to the current account but no fingerprint is written: another account's record is never
    replaced on the strength of an identity taken on trust. A reading with no reset times belongs to the current account and changes
    nothing. remember=False is a pure lookup (no identity re-read, no kv write): for a reset time that is only a good guess, like the
    one parsed out of a rate-limit message. Never raises past a kv problem the caller wraps."""
    r5, r7 = _resets(rate_limits)
    with _lock:
        owner = _match(_load(db), r5, r7, current(db))
        if not remember:
            return owner or current(db) or UNKNOWN
    fresh = True
    if owner is None and (r5 is not None or r7 is not None):
        _key, fresh = _observe(db, now, True, False)                # outside the lock: it reads a file
    ts = iso(now)
    with _lock:
        accts = _load(db)
        matched = owner is not None
        owner = owner or current(db) or UNKNOWN
        rec = accts.get(owner)
        if rec is not None:
            dirty = _remember(rec, r5, r7, replace=not matched) if matched or fresh else False
            seen = rec.get("last_seen")
            try:
                stale = not seen or (_to_epoch(ts) - datetime.fromisoformat(seen).timestamp()) >= SEEN_EVERY
            except (ValueError, TypeError):
                stale = True
            if stale:
                rec["last_seen"], dirty = ts, True
            if dirty:
                accts[owner] = rec
                db.kv_set(KV_ACCOUNTS, accts)
    return owner


# ------------------------------------------------------------------ labels, views, headroom
def label(db, key: str) -> str:
    """What to call an account: the person's own label, else the display name, else the email, else the key."""
    rec = _load(db).get(key) or {}
    return _s(rec.get("label"), LABEL_MAX) or _s(rec.get("name")) or _s(rec.get("email")) or key


_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def clean_label(raw) -> str | None:
    """A label as stored: control characters and runs of whitespace become one space, trimmed, at most 60 characters; '' -> None."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("label must be text")
    t = " ".join(_CTRL_RE.sub(" ", raw).split())[:LABEL_MAX].strip()
    return t or None


def set_label(db, key: str, raw) -> dict:
    """Rename an account (its label only; identity fields are never editable). Returns the updated record. KeyError for an unknown
    key, ValueError for a label that is not text."""
    new = clean_label(raw)
    with _lock:
        accts = _load(db)
        if key not in accts:
            raise KeyError(key)
        accts[key]["label"] = new
        db.kv_set(KV_ACCOUNTS, accts)
        return dict(accts[key])


def _reading(db, series: str, key: str) -> tuple[float | None, int | None, str | None, str]:
    """(used %, resets_at, at, source) of the newest per-account reading of a window, (None, None, None, 'statusline') when there is
    none. source is 'cache' for a reading that came from Claude Code's usage cache, else 'statusline'."""
    last = db.sample_last(series, SERIES_PREFIX + key)
    if not last or not isinstance(last.get("value"), (int, float)):
        return None, None, None, SOURCE_STATUSLINE
    meta = last.get("meta") if isinstance(last.get("meta"), dict) else {}
    src = SOURCE_CACHE if meta.get("source") == SOURCE_CACHE else SOURCE_STATUSLINE
    return round(float(last["value"]), 2), _int(meta.get("resets_at")), last.get("at"), src


def _row(db, key: str, rec: dict, cur, full: bool) -> dict:
    v5, m5, at5, s5 = _reading(db, "rl_5h", key)
    v7, m7, at7, s7 = _reading(db, "rl_7d", key)
    row = {"key": key, "email": rec.get("email"), "name": rec.get("name"), "label": rec.get("label"), "plan": rec.get("plan"),
           "rl_5h": v5, "rl_7d": v7, "resets_5h": _int(rec.get("resets_5h")) or m5, "resets_7d": _int(rec.get("resets_7d")) or m7,
           "rl_5h_at": at5, "rl_7d_at": at7, "source": s5 if (at5 or "") >= (at7 or "") else s7,
           "current": key == cur}
    if full:
        row.update({"org": rec.get("org"), "org_id": rec.get("org_id"), "tier": rec.get("tier"), "config_dir": rec.get("config_dir"),
                    "first_seen": rec.get("first_seen"), "last_seen": rec.get("last_seen")})
    return row


def view(db, full: bool = False) -> dict:
    """{current: key | None, list: [{key, email, name, label, plan, rl_5h, rl_7d, resets_5h, resets_7d, rl_5h_at, rl_7d_at, source,
    current}]} for /api/state (`accounts`) and, with full=True, GET /api/accounts (adds org, org_id, tier, config_dir, first_seen,
    last_seen). rl_* are the used percentages of the newest per-account reading (series rl_5h / rl_7d, key 'acct:<key>'), rl_*_at when
    each was read (ISO), source where the newer of the two came from ('statusline' | 'cache'), resets_* the account's remembered window
    reset (epoch; the raw readings: the browser's agentsWindowNow turns a passed one into 0 % and the next reset). Current account
    first, then the most used 7 d window. Reads the kv and the in-memory sample cache only: never a file or a process (the state is
    polled every 3 s)."""
    accts, cur = _load(db), current(db)
    rows = [_row(db, k, rec, cur, full) for k, rec in accts.items()]
    rows.sort(key=lambda r: (not r["current"], -(r["rl_7d"] if r["rl_7d"] is not None else -1.0), r["key"]))
    return {"current": cur, "list": rows}


def next_reset(resets_at, period: int, now: float) -> int | None:
    """The reset instant of the window that is running at `now`, given a window that ended (or ends) at `resets_at`: itself while it is
    still ahead, else resets_at + n x period for the first n that puts it past `now` (the windows nobody read in between are assumed to
    follow each other). None when the window's end is not known. The browser's agentsWindowNow computes the same."""
    if resets_at is None or not period or period <= 0:
        return None
    if resets_at > now:
        return int(resets_at)
    return int(resets_at + (int((now - resets_at) // period) + 1) * period)


def window_now(used, resets_at, period: int, now: float) -> tuple[float | None, int | None, bool]:
    """(used % now, the reset to count down to, rolled) of one window reading at `now` (epoch s). A window whose reset has passed with
    no newer reading has nothing counted in it yet: 0 % used and the next reset (see next_reset), rolled=True. Otherwise the reading
    stands. used None (no reading) stays None. The one rule behind headroom() and the summary's headroom_*; the browser's
    agentsWindowNow is its twin (tests/fixtures/window_now.json drives both)."""
    if used is None:
        return None, None, False
    rolled = resets_at is not None and resets_at <= now
    pct = 0.0 if rolled else max(0.0, min(100.0, float(used)))
    return pct, next_reset(resets_at, period, now), rolled


def headroom(db, now=None) -> dict:
    """Per-account room left in each window, for the launcher's 'most headroom' and the dispatch gate:
    {'5h': [{key, left_pct, resets_at}], '7d': [...]}, most room first (the current account first among equals). Accounts without a
    reading are left out. A window whose reset time has passed counts as fully open (100): its stored percentage is from the window
    that ended, and `resets_at` is then the next reset (window_now), as the pills and gauges count down to it. The reset is the
    account's remembered one first, as in view()'s rows."""
    t = _to_epoch(now)
    accts, cur = _load(db), current(db)
    out: dict[str, list] = {"5h": [], "7d": []}
    for name, series, fp, period in (("5h", "rl_5h", "resets_5h", PERIOD_5H), ("7d", "rl_7d", "resets_7d", PERIOD_7D)):
        for k, rec in accts.items():
            used, resets, _at, _src = _reading(db, series, k)
            if used is None:
                continue
            pct, nxt, _rolled = window_now(used, _int(rec.get(fp)) or resets, period, t)
            out[name].append({"key": k, "left_pct": round(100.0 - pct, 1), "resets_at": nxt})
        out[name].sort(key=lambda r: (-r["left_pct"], r["key"] != cur, r["key"]))
    return out
