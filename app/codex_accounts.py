"""Saved Codex logins: keep a copy of each Codex account's login on the box and switch the live login between them with one call.

The twin of app/account_store.py (read its docstring first), for Codex, which differs in four ways:

  * Codex keeps its login in ONE file, `$CODEX_HOME/auth.json` (mode 0600, honoured by every codex command; `codex logout` removes it). There is
    no identity file beside it: at login time nothing says whose login it is. The person NAMES an account when adding it (a required label),
    and the board learns its account id and plan later, from the rollouts of Codex sessions run while that login was live (`session_meta`
    carries `creator_account_id`; rate-limit events carry `plan_type`). auth.json itself is never opened for that.
  * An account is a random slot, not an identity: its key is the slot's name (24 hex characters of sha256 of a random token minted when the
    account is added).
  * Adding an account runs `codex login --device-auth` (codex-cli 0.157 and newer) with CODEX_HOME set to a pending dir of its own. It prints
    a link and a one-time code the person types ON THE PAGE: nothing is pasted back, so there is no "submit the code" call, only a watcher that
    sees auth.json appear.
  * Other Codex processes on the box (a Hermes agent's app-server daemon, a Codex in somebody's terminal) keep the login they read when they
    started and may write that login's refreshed token back into auth.json. A switch says so (warnings), refuses while one of the BOARD's own
    Codex sessions is open, and for ten minutes puts the new login back if the file reverts to the previous one's exact bytes.

Layout (under `<data dir>/codex-accounts`, every directory 0700, every file 0600; never under /tmp: Codex refuses to create its helper binaries
under a temporary dir, and the login runs with a CODEX_HOME in here):

    <slot>/auth.json         a byte copy of Codex's login file
    <slot>/auth.json.prev    the generation before it (one), for recovery
    <slot>/meta.json         {label, added_at, saved_at?, account_id?, user_id?, plan?, last_seen?}: what the board knows about the account
    .pending/                the CODEX_HOME of the one login in flight, seeded with a copy of the live config.toml and nothing else

Rules this module keeps (tests pin them):
  * The login file is an OPAQUE blob. It is never parsed, never logged and no part of it is in an API answer, an event, a kv value, a
    notification or an exception message: bytes are read, compared and written, nothing else. The bytes helpers are account_store's.
  * `codex` is never run here (the one process helper is agents/codex.py's); the login session is typed into the board's own tmux session.
  * The live login is "current" by kv and confirmed by bytes: a live file identical to a slot's copy names that slot. A live file that
    matches no slot is either the current account's refreshed token (saved into its slot once the file has been quiet for SYNC_SETTLE
    seconds) or, when there is no current account, an unknown login (adopted as a new account labelled "codex login <date>"): it is
    never overwritten without a copy being kept.
  * When in doubt do nothing rather than overwrite. One RLock serialises switch, save, seed, forget, finalize and the tick; the small
    login/result state the views read has its own lock so a 3 s poll of /api/state never waits for a save.

Known limit: a Codex process that was running before a switch keeps the old login in memory, and its next token refresh rewrites auth.json
with the OLD account's refreshed token. Within REPAIR_WINDOW of a switch the tick puts the new login back when the file reverts to the old
slot's exact bytes (at most MAX_REPAIRS times); bytes that match no slot are left alone then (they are the old account's refresh or the new
one's, and nothing in the file says which), and are saved into the current account's slot only after the window.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import accounts, claude_auth, login_problem, tmux
from .account_store import _read_plain, _read_stable, _secure_dir, _stamp, _Unstable, _write_atomic
from .agents import codex as codex_agent
from .agents import get as get_agent
from .config import settings
from .db import iso

log = logging.getLogger("ccboard.codex_accounts")

SETTLE = 3                  # a live login file younger than this is not adopted as a new account yet (it may still be being written)
SYNC_SETTLE = 30            # the tick re-saves the live account's login only once the file has been quiet this long
REPAIR_WINDOW = 600         # seconds after a switch during which a revert to the previous login's bytes is undone
MAX_REPAIRS = 5
PENDING_MIN_AGE = 1         # a pending login file must be this old (seconds) before it is taken
DEAD_AFTER = 20             # a login pane back at the shell this long without a login file has failed
ABANDON_AFTER = 3600        # a pending login this old is abandoned
RESULT_TTL = 600            # the outcome of the last login is shown this long
WATCH_FOR = 1200.0          # the page waits for no code: a watcher looks for the login file every WATCH_EVERY for this long (a device code lives 15 min)
WATCH_EVERY = 1.0
WATCH_DEAD_CHECK = 5        # ... and checks the pane (a process call) only every this many looks
SEEN_EVERY = 300            # last_seen is rewritten at most this often
LEARN_EVERY = 60            # the rollouts are looked at this often while an account has no id or plan yet
MAX_ROLLOUTS = 40
META_LINE_MAX = 64 * 1024   # the first line of a rollout (its session_meta) is read up to this
TAIL_BYTES = 128 * 1024     # the end of a rollout, where the rate-limit events are
CONFIG_MAX = 1024 * 1024    # the live config.toml is copied into .pending up to this size
LABEL_MAX = accounts.LABEL_MAX

STORE = "codex-accounts"
PENDING = ".pending"
AUTH = "auth.json"
PREV = "auth.json.prev"
META = "meta.json"
CONFIG = "config.toml"
KV_ACCOUNTS = "codex_accounts"        # {key: {key, label, added_at, saved_at, account_id, user_id, plan, last_seen, saved}}
KV_CURRENT = "codex_account_current"  # {key, since}
KV_SWITCH = "codex_account_switch"    # {from, to, at, repairs, last_repair?}
KV_RELOGIN = "codex_relogin_waiting"  # {key, at, applied}: a fresh login saved for the live account that could not go live yet (a board session was open)
RELOGIN_TTL = 86400                   # a waiting fresh login is given up on after this long
SERIES = "cacct"                      # event sample: key = the account that became current, meta {from, to}; or the account logged in again, meta {to, relogin: true}
SLOT_RE = re.compile(r"^[0-9a-f]{24}$")
REASON_NOT_INSTALLED = "codex is not installed"
REASON_UPDATE = "update Codex to 0.157 or newer: npm install -g @openai/codex"
WARN_OTHER = "other Codex processes on this box keep the previous login until they restart"
BUSY_SESSIONS = "close the board's Codex sessions first; a running Codex keeps its login and would write it back"
ERR_DID_NOT_COMPLETE = "the login did not complete"
ERR_NEED_LABEL = "give the account a name"

URL_RE = re.compile(r"https://[^\s\x1b\x07\"'<>]+")
CODE_RE = re.compile(r"^[A-Z0-9]{4,}-?[A-Z0-9]{4,}$")
PLAN_RE = re.compile(r'"plan_type"\s*:\s*"([A-Za-z0-9_-]{1,32})"')
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")

_lock = threading.RLock()
_vlock = threading.Lock()          # guards _login / _result only (the views)
_login: dict = {"adding": False, "label": None, "replace_key": None, "started_at": None, "ended_seen": None}
_result: dict | None = None
_result_at = 0.0
_watch_gen = 0
_watchers: list[threading.Thread] = []
_learn_at = 0.0
_wall = time.time                  # patched by tests (file ages are wall-clock mtime comparisons)
_WATCH_SLEEP = time.sleep


class StoreError(Exception):
    """Something the store refuses to do; the message is safe to show (no login bytes in it, ever). The API answers 409."""


class Unsupported(StoreError):
    pass


class NoSavedLogin(StoreError):
    pass


class Busy(StoreError):
    pass


class SwitchRefused(StoreError):
    pass


class UnknownAccount(LookupError):
    """No such account in the kv record (the API answers 404)."""


# ------------------------------------------------------------------ platform, paths
def _agent():
    return get_agent("codex")


def supported() -> bool:
    """Saved Codex logins work wherever a codex binary exists: Codex keeps file credentials on every OS."""
    try:
        return bool(_agent().bin())
    except Exception:
        return False


def _require() -> None:
    if not supported():
        raise Unsupported(REASON_NOT_INSTALLED)


def store_dir() -> Path:
    return Path(settings.data_dir) / STORE


def slot_dir(key: str) -> Path:
    """The slot of an account key. ValueError for anything that is not a slot name: the key arrives from the URL, and it is a directory name."""
    k = str(key)
    if not SLOT_RE.match(k):
        raise ValueError("not an account key")
    return store_dir() / k


def pending_dir() -> Path:
    return store_dir() / PENDING


def _live_dir() -> Path:
    return Path(settings.codex_home)


def _live_auth() -> Path:
    return _live_dir() / AUTH


def _slot_auth(key: str) -> Path:
    return slot_dir(key) / AUTH


def has_saved(key: str) -> bool:
    """True when the key's slot holds a non-empty login file."""
    try:
        return _slot_auth(key).stat().st_size > 0
    except (OSError, ValueError):
        return False


def _slot_ready(key: str) -> Path:
    _secure_dir(store_dir())
    return _secure_dir(slot_dir(key))


def _store_auth(slot: Path, data: bytes) -> None:
    """Put `data` in the slot as its login; the generation it replaces (when different) becomes `.prev`."""
    dest, prev = slot / AUTH, slot / PREV
    old = _read_plain(dest)
    if old is not None and old != data:
        if old:
            _write_atomic(prev, old, 0o600)
        _write_atomic(dest, data, 0o600)
    elif old is None:
        _write_atomic(dest, data, 0o600)
    else:
        dest.chmod(0o600)


def _live_bytes() -> tuple[bytes, list] | None:
    """(bytes, stamp) of the live login file, None when there is none (absent or empty). _Unstable when it kept changing."""
    return _read_stable(_live_auth())


def _slot_bytes(key: str) -> bytes | None:
    try:
        return _read_plain(_slot_auth(key)) or None
    except ValueError:
        return None


def _slot_keys() -> list[str]:
    try:
        return sorted(p.name for p in store_dir().iterdir() if SLOT_RE.match(p.name) and p.is_dir())
    except OSError:
        return []


def _matching_slot(data: bytes) -> str | None:
    """The key of the slot whose copy is byte-identical to `data`, None when there is none."""
    for k in _slot_keys():
        if _read_plain(_slot_auth(k)) == data:
            return k
    return None


def _is_prev(key: str, data: bytes) -> bool:
    """Is `data` byte-identical to the generation before the key's saved login (`.prev`)? A live file that is, is NOT that account's refreshed
    token (a refresh makes a new login, never an old one): it is the older login an older Codex process wrote back, or the dead login a
    log-in-again replaced while a board session kept the live file. Bytes compared, nothing read out of them."""
    try:
        return _read_plain(slot_dir(key) / PREV) == data
    except ValueError:
        return False


# ------------------------------------------------------------------ the kv records and the slot's meta.json
def _kv(db, key: str):
    rec = db.kv_get(key)
    return rec.get("value") if isinstance(rec, dict) else None


def _load(db) -> dict[str, dict]:
    v = _kv(db, KV_ACCOUNTS)
    if not isinstance(v, dict):
        return {}
    return {str(k): dict(r) for k, r in v.items() if isinstance(r, dict) and SLOT_RE.match(str(k))}


def _save(db, accts: dict) -> None:
    db.kv_set(KV_ACCOUNTS, accts)


def current(db) -> str | None:
    """The key of the account the board last saw live (kv codex_account_current), None when there is none."""
    v = _kv(db, KV_CURRENT)
    k = v.get("key") if isinstance(v, dict) else None
    return k if isinstance(k, str) and SLOT_RE.match(k) else None


def _text(v, n: int = 120) -> str | None:
    if not isinstance(v, str):
        return None
    t = v.strip()
    return t[:n] if t and t.isprintable() else None


def _meta_of(rec: dict) -> dict:
    out = {"label": rec.get("label"), "added_at": rec.get("added_at")}
    for k in ("saved_at", "account_id", "user_id", "plan", "last_seen"):
        if rec.get(k):
            out[k] = rec[k]
    return out


def _write_meta(key: str, rec: dict) -> None:
    slot = slot_dir(key)
    if slot.is_dir():
        _write_atomic(slot / META, (json.dumps(_meta_of(rec), indent=2, ensure_ascii=False) + "\n").encode("utf-8"), 0o600)


def _read_meta(slot: Path) -> dict | None:
    try:
        data = json.loads((slot / META).read_bytes())
    except (OSError, ValueError, RecursionError, MemoryError):
        return None
    return data if isinstance(data, dict) else None


def _put_record(db, key: str, **fields) -> dict:
    """Merge `fields` into the account's kv record (a new one when there is none) and its slot's meta.json. Returns the record."""
    with _lock:
        accts = _load(db)
        rec = dict(accts.get(key) or {"key": key, "label": None, "added_at": iso(None), "saved_at": None, "account_id": None,
                                      "user_id": None, "plan": None, "last_seen": None, "saved": True})
        rec.update(fields)
        accts[key] = rec
        _save(db, accts)
        with contextlib.suppress(OSError, ValueError):
            _write_meta(key, rec)
        return rec


def _known(db, key: str) -> dict:
    """The kv record of `key`, UnknownAccount when the board has none (checked before any path is built from the key)."""
    rec = _load(db).get(key) if isinstance(key, str) and SLOT_RE.match(key) else None
    if rec is None:
        raise UnknownAccount("no such account")
    return rec


def label_of(db, key: str) -> str:
    rec = _load(db).get(key) or {}
    return _text(rec.get("label"), LABEL_MAX) or key[:6]


def clean_label(raw) -> str:
    """A label as stored (accounts.clean_label's rules: control characters and runs of whitespace become one space, trimmed, at most 60
    characters), required here: ValueError for none or an empty one."""
    label = accounts.clean_label(raw)
    if not label:
        raise ValueError(ERR_NEED_LABEL)
    return label


def set_label(db, key: str, raw) -> dict:
    """Rename an account (its label only). UnknownAccount for an unknown key, ValueError for a label that is empty or not text."""
    label = clean_label(raw)
    with _lock:
        _known(db, key)
        return _row(db, _put_record(db, key, label=label), current(db))


def _set_current(db, key: str | None, now, *, event: bool = True) -> None:
    """kv codex_account_current moves to `key` (`since` = now) and, unless event is False, the `cacct` event is written. No-op when it is already so."""
    prev = current(db)
    if key == prev or key is None:
        return
    ts = iso(now)
    db.kv_set(KV_CURRENT, {"key": key, "since": ts})
    if event:
        from . import samples                                   # late: samples registers this module's tick at import
        samples.record(db, SERIES, key, 1, {"from": prev, "to": key} if prev else {"to": key}, at=ts, force=True)


def _touch_seen(db, key: str, now) -> None:
    rec = _load(db).get(key)
    if rec is None:
        return
    t = accounts._to_epoch(now)
    try:
        stale = not rec.get("last_seen") or t - accounts._to_epoch(rec["last_seen"]) >= SEEN_EVERY
    except (ValueError, TypeError):
        stale = True
    if stale:
        _put_record(db, key, last_seen=iso(now))


# ------------------------------------------------------------------ create, adopt, reindex
def _new_account(db, data: bytes, label: str, now) -> str:
    """A new slot for `data` (the login bytes) under a fresh random key; registers the account (not as the current one)."""
    for _ in range(5):
        key = hashlib.sha256(secrets.token_bytes(16)).hexdigest()[:24]
        if not slot_dir(key).exists():
            break
    slot = _slot_ready(key)
    try:
        _store_auth(slot, data)
        _put_record(db, key, label=label, added_at=iso(now), saved_at=iso(now), saved=True)
    except BaseException:
        shutil.rmtree(slot, ignore_errors=True)
        raise
    return key


def _adopt_unknown(db, data: bytes, now) -> str:
    """A live login that matches no slot and has no current account to belong to: keep a copy as a new, unlabelled account (labelled
    "codex login <date>", the person renames it) and make it the current one."""
    key = _new_account(db, data, f"codex login {iso(now)[:10]}", now)
    _set_current(db, key, now)
    log.info("adopted the live Codex login as a new account (%s)", key[:6])
    return key


def _reindex(db) -> None:
    """Slots on disk the kv does not know (a restored database): their meta.json brings them back (a label, else the date)."""
    accts = _load(db)
    for k in _slot_keys():
        if k in accts or not has_saved(k):
            continue
        meta = _read_meta(slot_dir(k)) or {}
        _put_record(db, k, label=_text(meta.get("label"), LABEL_MAX) or f"codex login {k[:6]}",
                    added_at=_text(meta.get("added_at"), 40) or iso(None), saved_at=_text(meta.get("saved_at"), 40), account_id=_text(meta.get("account_id")),
                    user_id=_text(meta.get("user_id")), plan=_text(meta.get("plan"), 32), last_seen=_text(meta.get("last_seen"), 40), saved=True)


# ------------------------------------------------------------------ keeping the live account's saved copy
def _in_window(db, now) -> dict | None:
    """The switch record while its repair window is open."""
    sw = _kv(db, KV_SWITCH)
    if not isinstance(sw, dict) or not sw.get("to"):
        return None
    try:
        age = accounts._to_epoch(now) - accounts._to_epoch(sw.get("at"))
    except (ValueError, TypeError):
        return None
    return sw if 0 <= age <= REPAIR_WINDOW else None


def _file_age(now, stamp: list) -> float:
    return accounts._to_epoch(now) - stamp[0] / 1e9


def _sync_live(db, now, *, quiet: float = SYNC_SETTLE) -> str | None:
    """Reconcile the live login file with the slots. Returns the key of the account that is live afterwards (None: nobody, or not decided yet).
      live == a slot's copy         that slot is current (a hand switch is adopted);
      live matches no slot          inside a switch's repair window: nothing; else the current account's slot takes it (a refreshed
                                    token) once the file has been quiet `quiet` seconds, and with no current account it is adopted as a
                                    new unlabelled account."""
    got = _live_bytes()
    if got is None:
        return None
    data, stamp = got
    cur = current(db)
    if cur and _slot_bytes(cur) == data:
        _touch_seen(db, cur, now)
        return cur
    match = _matching_slot(data)
    sw = _in_window(db, now)
    if match is not None:
        if sw and match == sw.get("from") and match != cur:
            return cur                                           # an older process wrote the previous login back: _repair's, not a hand switch
        _set_current(db, match, now)
        _touch_seen(db, match, now)
        return match
    if sw:
        return cur                                               # nothing says whose refresh this is: left alone until the window is over
    if cur and has_saved(cur) and _is_prev(cur, data):
        return cur                                               # the older generation, not a refresh: the saved login is the newer one (never saved over it)
    if cur and has_saved(cur):
        if _file_age(now, stamp) < quiet:
            return cur
        _store_auth(slot_dir(cur), data)
        _put_record(db, cur, last_seen=iso(now), saved_at=iso(now))
        log.info("saved the refreshed login of %s", label_of(db, cur))
        return cur
    if _file_age(now, stamp) < min(quiet, SETTLE):
        return None
    return _adopt_unknown(db, data, now)


def seed_current(db, now=None) -> str | None:
    """At startup: bring back slots the kv lost, and give the live login its saved copy (a slot that already holds it, the current
    account's slot when the file is its refreshed token, else a new unlabelled account). Returns the live account's key, None when nobody
    is logged in or nothing was decided yet. This is what makes a bad switch one tap from recovery."""
    _require()
    with _lock:
        _reindex(db)
        try:
            return _sync_live(db, now)
        except _Unstable as e:
            log.info("live Codex login not read: %s", e.__class__.__name__)
            return None


# ------------------------------------------------------------------ switch
def _login_running() -> bool:
    try:
        return _pane()[0]
    except Exception:
        return False


def _login_in_flight() -> bool:
    """A login started from Settings is waiting for the person (the pending dir is in use and its session is running)."""
    with _vlock:
        adding = _login["adding"]
    return bool(adding) and _login_running()


def _board_sessions(db) -> list[str]:
    """The names of the board's own Codex sessions that are open (a row of agent codex whose tmux session is alive)."""
    names = [n for n, r in db.open_rows().items() if (r or {}).get("agent") == "codex"]
    if not names:
        return []
    try:
        alive = set(tmux.list_sessions())
    except tmux.TmuxError:
        alive = set()
    return [n for n in names if n in alive]


def _warnings() -> list[str]:
    """What the switch cannot fix: Codex processes the board did not start keep the previous login until they restart."""
    try:
        panes = [int(s["pid"]) for s in tmux.list_sessions().values() if isinstance(s.get("pid"), int)]
    except Exception:
        panes = []
    try:
        return [WARN_OTHER] if codex_agent.foreign_processes(panes) else []
    except Exception:
        return []


def _confirm_current(db, got, now) -> tuple[str | None, bool]:
    """(the account that is live right now, whether the live file is its refreshed token rather than a copy of its slot), decided by bytes
    (see _sync_live). A live file that matches no slot and has no current account to belong to is kept as a new unlabelled account first.
    (None, False) when nobody is logged in."""
    if got is None:
        return None, False
    data = got[0]
    cur = current(db)
    if cur and has_saved(cur) and _slot_bytes(cur) == data:
        return cur, False
    match = _matching_slot(data)
    if match is not None:
        _set_current(db, match, now)
        return match, False
    if cur and has_saved(cur):
        return cur, not _is_prev(cur, data)                      # its refreshed token (saved below, before anything is overwritten), unless the older generation
    return _adopt_unknown(db, data, now), False


def _put_live(db, key: str, data: bytes | None) -> None:
    """The saved login of the CURRENT account `key` becomes the live file (one atomic replace; the account does not change, so no switch
    record and no repair window). SwitchRefused when it cannot be written: the previous file is then still in place."""
    if not data:
        raise NoSavedLogin("the saved login is incomplete; log in to the account again from Settings")
    try:
        _live_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_atomic(_live_auth(), data, 0o600)
    except OSError as e:
        log.warning("saved Codex login not put in place (%s): the previous login is still in place", e.__class__.__name__)
        raise SwitchRefused("could not apply the saved login; the previous login is still in place") from None
    _touch_seen(db, key, iso(None))


def _apply(db, key: str, frm: str | None, now) -> None:
    """Put the slot's login in place of the live one (one atomic replace: a failure leaves the previous login as it was), record the switch
    (the repair window starts) and move the current account (the `cacct` event)."""
    got = _read_stable(_slot_auth(key))
    if got is None:
        raise NoSavedLogin("the saved login is incomplete; log in to the account again from Settings")
    try:
        _live_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_atomic(_live_auth(), got[0], 0o600)
    except OSError as e:
        log.warning("switch to a saved Codex login failed (%s): the previous login is still in place", e.__class__.__name__)
        raise SwitchRefused("could not apply the saved login; the previous login is still in place") from None
    db.kv_set(KV_SWITCH, {"from": frm, "to": key, "at": iso(now), "repairs": 0})
    _set_current(db, key, now)
    _touch_seen(db, key, now)
    log.info("switched Codex account %s -> %s", label_of(db, frm) if frm else "-", label_of(db, key))


def switch(db, key: str, *, now=None) -> dict:
    """Make `key` the live Codex login. UnknownAccount (no such account), NoSavedLogin (nothing saved for it), Busy (a login from Settings is
    in flight, or one of the board's own Codex sessions is open), SwitchRefused (the current login could not be saved first, or the
    switch could not be applied: the previous login is then still in place). Switching to the account that is live is a no-op
    ({"already": True}). Returns {ok, already, from, to, warnings}."""
    _require()
    with _lock:
        _known(db, key)
        if not has_saved(key):
            raise NoSavedLogin("there is no saved login for this account; log in to it from Settings first")
        if _login_in_flight():
            raise Busy("a login is in progress; finish or cancel it first")
        if _board_sessions(db):
            raise Busy(BUSY_SESSIONS)
        try:
            got = _live_bytes()
        except _Unstable:
            raise SwitchRefused("the login file is being rewritten right now; nothing was changed") from None
        cur, refreshed = _confirm_current(db, got, now)
        if cur == key and not refreshed and got is not None and _slot_bytes(key) != got[0]:
            # the saved login is a fresh one the live file never got (a log-in-again made while a board session was open): this is it
            _put_live(db, key, _slot_bytes(key))
            db.kv_del(KV_RELOGIN)
            return {"ok": True, "already": False, "from": cur, "to": key, "warnings": _warnings()}
        if cur == key and not refreshed:
            return {"ok": True, "already": True, "from": cur, "to": key, "warnings": []}
        if refreshed and _in_window(db, now):
            raise SwitchRefused("the login file changed right after the last switch and cannot be told from an older Codex's write-back; "
                                "wait a few minutes and try again")
        if cur == key:
            _store_auth(slot_dir(cur), got[0])                   # the live account's refreshed token is its saved login: nothing to switch
            return {"ok": True, "already": True, "from": cur, "to": key, "warnings": []}
        if cur is not None and got is not None and not _is_prev(cur, got[0]):
            for _ in range(3):                                   # the live file may be rewritten between the save and the overwrite
                try:
                    _store_auth(slot_dir(cur), got[0])
                    again = _live_bytes()
                except (_Unstable, OSError) as e:
                    log.info("current Codex login not saved: %s", e.__class__.__name__)
                    raise SwitchRefused("could not save the current login first; nothing was changed") from None
                if again is not None and again[1] == got[1] and again[0] == got[0]:
                    break
                got = again
                if got is None:
                    break
            else:
                raise SwitchRefused("could not save the current login first; nothing was changed")
        _apply(db, key, cur, now)
        return {"ok": True, "already": False, "from": cur, "to": key, "warnings": _warnings()}


def forget(db, key: str) -> dict:
    """Delete the key's saved login. The kv record stays (saved: false). UnknownAccount for an account the board never saw, Busy for the live
    account (switch to another one first). Returns {"ok": True, "forgotten": whether there was a slot}."""
    _require()
    with _lock:
        _known(db, key)
        live = None
        with contextlib.suppress(_Unstable):
            live = _live_bytes()
        if current(db) == key or (live is not None and _slot_bytes(key) == live[0]):
            raise Busy("this account is the live login; switch to another account first")
        slot = slot_dir(key)
        existed = slot.exists()
        shutil.rmtree(slot, ignore_errors=True)
        _put_record(db, key, saved=False)
        return {"ok": True, "forgotten": existed}


def _drop_live() -> None:
    """Remove the live login file (the store's byte-level equivalent of `codex logout`, which this module never runs). Absent is fine."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(_live_auth())


def logout(db, *, now=None) -> dict:
    """Log the box out of Codex without losing the login: its bytes are kept in a slot first, then the live auth.json is removed and the current
    account is cleared. Busy (a login from Settings is in flight, or one of the board's own Codex sessions is open: a running Codex keeps its
    login in memory and would write it back), SwitchRefused (the login file kept changing, or its bytes could not be copied: nothing is removed
    then). Returns {ok, was, warnings}: `was` is the key of the account that was logged out, None when nobody was logged in (a harmless no-op).

    Decision for the unknown login (a live file that is no slot's copy and has no current account to belong to): it is adopted as a new account
    labelled "codex login <date>" (_confirm_current -> _adopt_unknown, the same path a switch uses) BEFORE the file is removed, so the person can switch
    back to it. Refusing would leave a logged-in box that cannot log out; deleting would lose a login. A live file that is the current account's
    refreshed token is saved over its slot (the old generation becomes .prev). The file is removed only after its exact bytes are found in a slot (or
    in a slot's .prev, the older generation) and are still what the file holds: bytes with no copy are never deleted."""
    _require()
    with _lock:
        if _login_in_flight():
            raise Busy("a login is in progress; finish or cancel it first")
        if _board_sessions(db):
            raise Busy(BUSY_SESSIONS)
        try:
            got = _live_bytes()
        except _Unstable:
            raise SwitchRefused("the login file is being rewritten right now; nothing was changed") from None
        if got is None:
            if current(db):
                db.kv_del(KV_CURRENT)                            # the file is gone already (a hand logout): the board stops naming an account
            db.kv_del(KV_SWITCH)
            db.kv_del(KV_RELOGIN)
            codex_agent.forget_auth()
            return {"ok": True, "was": None, "warnings": []}
        cur, refreshed = _confirm_current(db, got, now)
        if refreshed and _in_window(db, now):
            raise SwitchRefused("the login file changed right after the last switch and cannot be told from an older Codex's write-back; "
                                "wait a few minutes and try again")
        if refreshed:
            try:
                _store_auth(slot_dir(cur), got[0])
            except (OSError, ValueError) as e:
                log.info("current Codex login not saved: %s", e.__class__.__name__)
                raise SwitchRefused("could not save the current login first; nothing was changed") from None
        kept = _matching_slot(got[0]) or (cur if cur and _is_prev(cur, got[0]) else None)
        if kept is None:
            raise SwitchRefused("could not save the current login first; nothing was changed")
        try:
            again = _live_bytes()
        except _Unstable:
            again = None
        if again is None or again[1] != got[1] or again[0] != got[0]:
            raise SwitchRefused("the login file changed while it was being saved; nothing was changed")
        try:
            _drop_live()
        except OSError as e:
            log.warning("Codex login not removed (%s)", e.__class__.__name__)
            raise SwitchRefused("could not remove the login file; the login is still in place") from None
        was = cur or kept
        db.kv_del(KV_CURRENT)
        db.kv_del(KV_SWITCH)                                     # no repair window: nothing may put the dropped login back
        db.kv_del(KV_RELOGIN)
        codex_agent.forget_auth()                                # the 60 s `codex login status` verdict must not say "logged in" any more
        log.info("logged out of Codex; the login of %s stays saved", label_of(db, was))
        return {"ok": True, "was": was, "warnings": _warnings()}


# ------------------------------------------------------------------ adding a login (codex login --device-auth in .pending)
def _reset_login() -> None:
    with _vlock:
        _login.update(adding=False, label=None, replace_key=None, started_at=None, ended_seen=None)


def _set_result(result: dict | None, at: float) -> None:
    global _result, _result_at
    with _vlock:
        _result, _result_at = result, at


def _seed_config(pend: Path) -> None:
    """The pending CODEX_HOME starts with a copy of the live config.toml (model and provider settings apply to the login) and nothing
    else: no login, no hooks (the login needs none)."""
    src = _live_dir() / CONFIG
    try:
        if src.is_file() and not src.is_symlink() and src.stat().st_size <= CONFIG_MAX:
            _write_atomic(pend / CONFIG, src.read_bytes(), 0o600)
    except OSError as e:
        log.info("config.toml not copied into the pending login: %s", e.__class__.__name__)


def start_login(db, label, *, restart: bool = False, replace_key: str | None = None) -> None:
    """Start `codex login --device-auth` for a NEW account in the internal tmux session, with CODEX_HOME set to an empty `.pending` (seeded with
    the live config.toml): the live login is not touched. `label` names the account (required, accounts.clean_label's rules): ValueError
    for none. One at a time: Busy while any login session is running, unless `restart`. Unsupported without a codex that has
    --device-auth.

    `replace_key` is a log-in-again: the login that finishes REPLACES that account's saved login (same key, same label, same place in the list;
    see _replace) instead of making a second account. The account keeps its own name, `label` is not asked for then (UnknownAccount for a key
    the board never saw)."""
    global _watch_gen
    _require()
    if replace_key is not None:
        _known(db, replace_key)
        label = label_of(db, replace_key)
    else:
        label = clean_label(label)
    if not _agent().device_auth(fetch=True):
        raise Unsupported(REASON_UPDATE)
    with _lock:
        if not restart and _login_running():
            raise Busy("a login is already running")
        pend = pending_dir()
        _secure_dir(store_dir())
        shutil.rmtree(pend, ignore_errors=True)
        _secure_dir(pend)
        try:
            _seed_config(pend)
            with contextlib.suppress(tmux.TmuxDown):
                tmux.kill_session(tmux.LOGIN_SESSION)
            tmux.new_session(tmux.LOGIN_SESSION, str(Path.home()), env={"CODEX_HOME": str(pend), "BROWSER": "/bin/true"}, width=400, height=50)
            tmux.send_line(tmux.LOGIN_SESSION, codex_agent.DEVICE_LOGIN_CMD)
        except BaseException:
            shutil.rmtree(pend, ignore_errors=True)
            raise
        _set_result(None, 0.0)
        _watch_gen += 1                                          # a watcher of an earlier login stops: this one has its own
        with _vlock:
            _login.update(adding=True, label=label, replace_key=replace_key, started_at=_wall(), ended_seen=None)
    watch_login(db)


def _kill_login_session() -> None:
    with contextlib.suppress(Exception):
        tmux.kill_session(tmux.LOGIN_SESSION)


def cancel_login() -> None:
    """Stop a login started from Settings: its tmux session is killed, `.pending` removed, the state and the last result cleared. A login
    session this module did not start (the Claude one) is left alone."""
    global _watch_gen
    with _lock:
        with _vlock:
            ours = bool(_login["adding"])
        pend = pending_dir()
        if ours or pend.exists():
            _kill_login_session()
            shutil.rmtree(pend, ignore_errors=True)
        _watch_gen += 1
        _reset_login()
        _set_result(None, 0.0)


def _decide(result: dict) -> dict:
    """Store the outcome of the login (it is shown RESULT_TTL of wall-clock time, whatever `now` the caller passed)."""
    _reset_login()
    _set_result(result, _wall())
    return result


def _fail(pend: Path, t: float) -> dict:
    _kill_login_session()
    shutil.rmtree(pend, ignore_errors=True)
    return _decide({"ok": False, "error": ERR_DID_NOT_COMPLETE, "at": iso(t)})


def _replace(db, pend: Path, key: str, got, live, t: float, now) -> dict | None:
    """A log-in-again finished: the fresh login replaces account `key`'s saved one (the generation it replaces becomes `.prev`; the key, the
    label and which account is current stay; meta.json and the record get a new saved_at). When `key` is the live account (its login is the
    dead one) the fresh login goes live at once, the live bytes it replaces kept in `.prev`, unless one of the board's own Codex sessions is
    open: a running Codex keeps its login and would write the dead one back, so the fresh one stays saved and the result says why
    ({"live": False, "why": ...}). Nobody logged in live: it goes live like a new account's would. None when it could not be saved yet."""
    _kill_login_session()                                        # nothing writes into .pending any more
    try:
        was_current = current(db) == key or (live is not None and _slot_bytes(key) == live[0])
        slot = _slot_ready(key)
        _store_auth(slot, got[0])
        if was_current and live is not None and live[0] != got[0]:
            _write_atomic(slot / PREV, live[0], 0o600)           # the live login (refreshed since the slot was written, or dead) is the generation before
        _put_record(db, key, saved=True, saved_at=iso(now))
    except OSError as e:
        log.warning("a finished login could not be saved yet: %s", e.__class__.__name__)
        return None                                              # .pending stays: the next call tries again (or abandons it after an hour)
    shutil.rmtree(pend, ignore_errors=True)
    try:
        from . import samples                                    # late: samples registers this module's tick at import
        samples.record(db, SERIES, key, 1, {"to": key, "relogin": True}, at=iso(now), force=True)   # the login was renewed; the account in use did not change
    except Exception as e:
        log.warning("relogin event not written: %s", e.__class__.__name__)
    live_ok, why, warnings = False, None, []
    try:
        if live is None:
            _apply(db, key, current(db), now)
            live_ok = True
        elif was_current:
            if _board_sessions(db):
                why = BUSY_SESSIONS
                db.kv_set(KV_RELOGIN, {"key": key, "at": iso(now), "applied": 0})      # the tick puts it in once the sessions are closed
            else:
                _put_live(db, key, got[0])                       # the account does not change: no switch record, no repair window
                _set_current(db, key, now)
                live_ok = True
        if live_ok:
            warnings = _warnings()
            if (_kv(db, KV_RELOGIN) or {}).get("key") == key:
                db.kv_del(KV_RELOGIN)
    except (StoreError, OSError) as e:
        why = "the fresh login is saved but could not be put in place"
        log.warning("the new login is saved but was not put in place: %s", e.__class__.__name__)
    login_problem.clear(db, "codex", key)
    out = {"ok": True, "key": key, "label": label_of(db, key), "live": live_ok, "replaced": True, "why": why, "at": iso(t)}
    if warnings:
        out["warnings"] = warnings
    return _decide(out)


def _complete(db, pend: Path, label: str | None, t: float, now, replace_key: str | None = None) -> dict | None:
    """The pending login has its file: move it into a new slot, register the account (not as the current one), close the login and, when no
    Codex login is live (no file, or a login nobody saved and no account yet), put it in place at once; a login nobody saved is kept as an
    unlabelled account first, never replaced without a copy. With `replace_key` (a log-in-again of a known account) it goes into THAT
    account's slot instead (_replace); an account that has vanished since (merged away) gets the login as a new one under `label`."""
    got = _read_stable(pend / AUTH)
    try:
        live = _live_bytes()
    except _Unstable:
        return None                                              # the live file is being rewritten: the next call tries again
    if got is None:
        return None                                              # still being written
    if replace_key and replace_key in _load(db):
        return _replace(db, pend, replace_key, got, live, t, now)
    _kill_login_session()                                        # nothing writes into .pending any more
    label = label or f"codex login {iso(now if now is not None else t)[:10]}"
    try:
        had_accounts = any(has_saved(k) for k in _load(db))
        unknown_live = live is not None and _matching_slot(live[0]) is None
        cur = current(db)
        if unknown_live and not (cur and has_saved(cur)):
            _adopt_unknown(db, live[0], now)
        key = _new_account(db, got[0], label, now)
    except OSError as e:
        log.warning("a finished login could not be saved yet: %s", e.__class__.__name__)
        return None                                              # .pending stays: the next call tries again (or abandons it after an hour)
    shutil.rmtree(pend, ignore_errors=True)
    live_ok = False
    if live is None or (unknown_live and not had_accounts):
        try:
            _apply(db, key, current(db), now)
            live_ok = True
        except (StoreError, OSError) as e:
            log.warning("the new login is saved but was not put in place: %s", e.__class__.__name__)
    return _decide({"ok": True, "key": key, "label": label_of(db, key), "live": live_ok, "at": iso(t)})


def finalize(db, now=None) -> dict | None:
    """Look at the pending login. A login file there that is at least PENDING_MIN_AGE old: save it as a new account (see _complete). A login
    session that has been back at the shell for more than DEAD_AFTER seconds without a file, or a pending dir older than ABANDON_AFTER: the
    login failed and `.pending` is removed. Returns the outcome ({"ok": True, "key", "label", "live", "at"} or {"ok": False, "error", "at"})
    when something was decided, else None."""
    if not supported():
        return None
    with _lock:
        pend = pending_dir()
        with _vlock:
            adding, started, label, replace_key = bool(_login["adding"]), _login["started_at"], _login["label"], _login["replace_key"]
        if not pend.is_dir():
            if adding:
                _reset_login()
            return None
        t = _wall() if now is None else accounts._to_epoch(now)
        try:
            ast = (pend / AUTH).stat()
        except OSError:
            ast = None
        have = bool(ast and ast.st_size > 0)
        if have and t - ast.st_mtime >= PENDING_MIN_AGE:
            res = _complete(db, pend, label, t, now, replace_key)
            if res is not None:
                return res
        if started is None:
            with contextlib.suppress(OSError):
                started = pend.stat().st_mtime
        if started is not None and t - started > ABANDON_AFTER:
            return _fail(pend, t)
        if have:
            return None                                          # the file's age is a moment away
        running = _login_running()
        with _vlock:
            if running:
                _login["ended_seen"] = None
                return None
            if adding:
                if _login["ended_seen"] is None:
                    _login["ended_seen"] = t
                    return None
                dead = t - _login["ended_seen"] > DEAD_AFTER
            else:                                                # left behind by a restart: nothing to report, just tidy up
                dead = started is not None and t - started > DEAD_AFTER
        if not dead:
            return None
        if adding:
            return _fail(pend, t)
        shutil.rmtree(pend, ignore_errors=True)
        return None


def watch_login(db) -> threading.Thread:
    """After a login was started: a daemon thread that looks for the login file every WATCH_EVERY seconds for up to WATCH_FOR (the page waits for
    no code to be pasted, so the 15 s tick is too slow), stopping at the first outcome or when there is no pending login. The pane (a process
    call) is looked at only every WATCH_DEAD_CHECK looks."""
    gen = _watch_gen

    def run() -> None:
        end = time.monotonic() + WATCH_FOR
        n = 0
        while time.monotonic() < end and gen == _watch_gen:
            try:
                if not pending_dir().exists():
                    return
                if (pending_dir() / AUTH).exists() or n % WATCH_DEAD_CHECK == 0:
                    if finalize(db) is not None or not pending_dir().exists():
                        return
            except Exception as e:
                log.warning("finalize failed: %s", e.__class__.__name__)
            n += 1
            _WATCH_SLEEP(WATCH_EVERY)

    th = threading.Thread(target=run, name="codex-account-finalize", daemon=True)
    _watchers[:] = [w for w in _watchers if w.is_alive()]
    _watchers.append(th)
    th.start()
    return th


def stop_watchers(timeout: float = 5.0) -> None:
    """Stop every watch_login thread (tests; and nothing else needs it: they end by themselves)."""
    global _watch_gen
    _watch_gen += 1
    for th in list(_watchers):
        th.join(timeout)
    _watchers.clear()


# ------------------------------------------------------------------ what the login pane shows
def _pane() -> tuple[bool, str]:
    """(is the login session running something other than its shell, its text). (False, "") when there is no session."""
    try:
        sessions = tmux.list_sessions()
    except tmux.TmuxDown:
        return False, ""
    s = sessions.get(tmux.LOGIN_SESSION)
    if not s:
        return False, ""
    cmd = (s.get("command") or "").lstrip("-")
    running = bool(cmd) and cmd not in claude_auth.SHELLS
    return running, tmux.capture(tmux.LOGIN_SESSION)


def parse_login_text(text: str) -> dict:
    """{url, code, line} from what `codex login --device-auth` printed: the newest https link that names `device` or `auth`, and the first
    token after that link's line shaped like a one-time code (`ABCD-12345`), with the raw line it was on. None for what is not there (yet)."""
    lines = [ln.rstrip() for ln in str(text or "").splitlines()]
    url, at = None, -1
    for i, ln in enumerate(lines):
        for u in URL_RE.findall(ln):
            u = u.rstrip(".,;:)]}")
            if "device" in u.lower() or "auth" in u.lower():
                url, at = u, i
    code, raw = None, None
    if url is not None:
        for ln in lines[at + 1:]:
            for tok in ln.split():
                tok = tok.strip(".,;:()[]{}\"'")
                if CODE_RE.match(tok):
                    code, raw = tok, ln.strip()
                    break
            if code:
                break
    return {"url": url, "code": code, "line": raw}


def login_view() -> dict:
    """{running, adding, label, replace_key, started_at, url, code, tail, result} for the Settings page: what is being added (`replace_key`: the
    account being logged in again, None for a new one), the link and the one-time code
    the login printed, and how the last login ended (None once older than RESULT_TTL). Looks at the login pane only while a login is being
    added."""
    with _vlock:
        res = dict(_result) if _result is not None and _wall() - _result_at <= RESULT_TTL else None
        adding, label, started, rk = bool(_login["adding"]), _login["label"], _login["started_at"], _login["replace_key"]
    out = {"running": False, "adding": adding, "label": label, "replace_key": rk if adding else None, "started_at": iso(started) if started else None,
           "url": None, "code": None, "tail": [], "result": res}
    if adding:
        try:
            running, text = _pane()
        except Exception:
            running, text = False, ""
        parsed = parse_login_text(text)
        out.update(running=running, url=parsed["url"], code=parsed["code"],
                   tail=[ln.rstrip() for ln in text.splitlines() if ln.strip()][-15:])
    return out


# ------------------------------------------------------------------ learning who an account is, from the rollouts
def _utc(ts) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        d = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def _rollout_meta(path: Path) -> dict | None:
    """{account_id, user_id, created} from the first line of a rollout (its session_meta payload: the two creator keys and the time only),
    None for a file that is not one."""
    try:
        with open(path, "rb") as f:
            line = f.readline(META_LINE_MAX)
        data = json.loads(line)
    except (OSError, ValueError, RecursionError, MemoryError):
        return None
    payload = data.get("payload") if isinstance(data, dict) and data.get("type") == "session_meta" else None
    if not isinstance(payload, dict):
        return None
    aid = payload.get("creator_account_id")
    created = _utc(payload.get("timestamp")) or _utc(data.get("timestamp"))
    uid = payload.get("creator_user_id")
    return {"account_id": aid if isinstance(aid, str) and ID_RE.match(aid) else None,
            "user_id": uid if isinstance(uid, str) and ID_RE.match(uid) else None, "created": created}


def _rollout_plan(path: Path) -> str | None:
    """The last `plan_type` among the end of a rollout's rate-limit events."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read(TAIL_BYTES).decode("utf-8", "ignore")
    except OSError:
        return None
    found = PLAN_RE.findall(tail)
    return found[-1] if found else None


def _recent_rollouts() -> list[Path]:
    root = Path(settings.codex_home) / "sessions"
    try:
        return sorted(root.glob("*/*/*/rollout-*.jsonl"), reverse=True)[:MAX_ROLLOUTS]
    except OSError:
        return []


def _learn(db, now, *, force: bool = False) -> None:
    """Fill the current account's id and plan from the newest rollouts of sessions created after it became live (session_meta: creator_account_id,
    creator_user_id; the newest rate-limit event: plan_type). Several different account ids among those sessions is ambiguous (another Codex
    process may still hold the previous login): nothing is learned then. An id that another account already has makes them one account (_merge)."""
    global _learn_at
    cur = current(db)
    rec = _load(db).get(cur) if cur else None
    if not rec or (rec.get("account_id") and rec.get("plan")):
        return
    t = _wall()
    if not force and t - _learn_at < (LEARN_EVERY if not rec.get("account_id") else LEARN_EVERY * 10):
        return
    _learn_at = t
    cv = _kv(db, KV_CURRENT)
    since = accounts._to_epoch(cv.get("since")) if isinstance(cv, dict) and cv.get("since") else 0.0
    ids, users, plan = set(), set(), None
    for path in _recent_rollouts():
        meta = _rollout_meta(path)
        if not meta or meta["created"] is None or meta["created"] < since or not meta["account_id"]:
            continue
        ids.add(meta["account_id"])
        if meta["user_id"]:
            users.add(meta["user_id"])
        plan = plan or _rollout_plan(path)                       # newest first: the first plan seen is the newest
    if len(ids) != 1:
        if len(ids) > 1:
            log.info("Codex rollouts since the switch name more than one account: nothing learned")
        return
    fields = {}
    aid = next(iter(ids))
    if not rec.get("account_id"):
        fields["account_id"] = aid
        if len(users) == 1:
            fields["user_id"] = next(iter(users))
    elif rec.get("account_id") != aid:
        return
    if plan and plan != rec.get("plan"):
        fields["plan"] = plan
    if fields:
        _put_record(db, cur, **fields)
    other = [k for k, r in _load(db).items() if k != cur and r.get("account_id") == aid]
    if other:
        _merge(db, cur, other[0], now)


def _merge(db, a: str, b: str, now) -> None:
    """Two keys with the same account id are one account: the older key stays (with the newer one's label when it has one), the newer slot
    goes. The survivor's slot takes the newer slot's login unless the survivor is the one that is live; a live newer account hands its
    place to the survivor (the file is not touched: same bytes)."""
    accts = _load(db)
    keep, drop = sorted((a, b), key=lambda k: (str(accts[k].get("added_at") or ""), k))
    kr, dr = accts[keep], accts[drop]
    live = None
    with contextlib.suppress(_Unstable):
        live = _live_bytes()
    keep_live = live is not None and _slot_bytes(keep) == live[0]
    newer = _slot_bytes(drop)
    if newer is not None and not keep_live:
        _store_auth(_slot_ready(keep), newer)
    label = _text(dr.get("label"), LABEL_MAX) or _text(kr.get("label"), LABEL_MAX)
    fields = {"label": label, "saved": has_saved(keep) or newer is not None}
    for k in ("account_id", "user_id", "plan"):
        fields[k] = kr.get(k) or dr.get(k)
    _put_record(db, keep, **fields)
    was_current = current(db) == drop
    shutil.rmtree(slot_dir(drop), ignore_errors=True)
    accts = _load(db)
    accts.pop(drop, None)
    _save(db, accts)
    if was_current:
        cv = _kv(db, KV_CURRENT) or {}
        db.kv_set(KV_CURRENT, {"key": keep, "since": cv.get("since") or iso(now)})
    sw = _kv(db, KV_SWITCH)
    if isinstance(sw, dict) and drop in (sw.get("from"), sw.get("to")):
        db.kv_set(KV_SWITCH, {**sw, **{k: keep for k in ("from", "to") if sw.get(k) == drop}})
    log.info("two saved Codex logins were the same account: kept %s", label_of(db, keep))


# ------------------------------------------------------------------ the tick
def _repair(db, now) -> None:
    """A Codex process that was running before the switch wrote the previous login back: for REPAIR_WINDOW after the switch, while the live file
    is byte-identical to the previous account's slot, put the new login in again (at most MAX_REPAIRS times). Bytes that match no slot are
    never touched."""
    with _lock:
        sw = _in_window(db, now)
        if not sw or not sw.get("from") or not has_saved(sw["to"]):
            return
        repairs = sw.get("repairs") if isinstance(sw.get("repairs"), int) else 0
        if repairs >= MAX_REPAIRS:
            return
        got = _live_bytes()
        frm, to = _slot_bytes(sw["from"]), _slot_bytes(sw["to"])
        if got is None or frm is None or to is None or got[0] != frm or got[0] == to:
            return
        _write_atomic(_live_auth(), to, 0o600)
        db.kv_set(KV_SWITCH, {**sw, "repairs": repairs + 1, "last_repair": iso(now)})
        log.info("an older Codex process wrote the previous login back; the new one was put in place again (%d of %d)", repairs + 1, MAX_REPAIRS)


def _finish_relogin(db, now) -> None:
    """A log-in-again of the live account that stayed saved because one of the board's Codex sessions was open (kv KV_RELOGIN): once none is
    open, and the live file is still the older generation (or gone), the fresh login goes live (at most MAX_REPAIRS times: an older Codex
    that keeps writing its login back is not fought for ever). Dropped when it is live, when the account is no longer the current one, when
    something else is live, or after RELOGIN_TTL."""
    with _lock:
        w = _kv(db, KV_RELOGIN)
        if not isinstance(w, dict) or not isinstance(w.get("key"), str):
            return
        key = w["key"]
        try:
            expired = accounts._to_epoch(now) - accounts._to_epoch(w.get("at")) > RELOGIN_TTL
        except (ValueError, TypeError):
            expired = True
        to = _slot_bytes(key) if SLOT_RE.match(key) else None
        if expired or to is None or current(db) != key:
            db.kv_del(KV_RELOGIN)
            return
        got = _live_bytes()
        if got is not None and got[0] == to:
            db.kv_del(KV_RELOGIN)                                # it is live
            return
        if got is not None and not _is_prev(key, got[0]):
            db.kv_del(KV_RELOGIN)                                # something else is live now: not ours to replace
            return
        applied = w.get("applied") if isinstance(w.get("applied"), int) else 0
        if applied >= MAX_REPAIRS or _board_sessions(db):
            return
        _put_live(db, key, to)
        db.kv_set(KV_RELOGIN, {**w, "applied": applied + 1})
        log.info("the fresh login of %s went live now that no board Codex session is open", label_of(db, key))


def _sync(db, now) -> None:
    with _lock:
        _sync_live(db, now)


def _learn_step(db, now) -> None:
    with _lock:
        _learn(db, now)


def tick(db, now=None) -> None:
    """The Sampler's 15 s tick: (a) finish a login that has its file, (b) undo a stale write-back after a switch, (b') put a waiting fresh
    login in place, (c) keep the live account's saved copy fresh, (d) learn who the account is. Never raises (class names only in the log); nothing at all without a codex binary."""
    if not supported():
        return
    for step in (finalize, _repair, _finish_relogin, _sync, _learn_step):
        try:
            step(db, now)
        except Exception as e:
            log.warning("saved Codex logins: %s failed: %s", step.__name__.strip("_"), e.__class__.__name__)


# ------------------------------------------------------------------ views
def _saved_at(key: str, rec: dict) -> str | None:
    """When the key's saved login was last written: the record's saved_at, else its slot's meta.json, else the login file's own mtime (a slot
    written before saved_at existed). Only times: the login file's content is never read."""
    t = _text(rec.get("saved_at"), 40)
    if t:
        return t
    try:
        t = _text((_read_meta(slot_dir(key)) or {}).get("saved_at"), 40)
    except ValueError:
        t = None
    if t:
        return t
    try:
        return iso(_slot_auth(key).stat().st_mtime)
    except (OSError, ValueError):
        return None


def _row(db, rec: dict, cur, sup: bool | None = None) -> dict:
    key = rec["key"]
    sup = supported() if sup is None else sup
    saved = bool(sup and has_saved(key))
    return {"key": key, "label": rec.get("label"), "account_id": rec.get("account_id"), "plan": rec.get("plan"),
            "saved": saved, "saved_at": _saved_at(key, rec) if saved else None, "current": key == cur, "added_at": rec.get("added_at"),
            "last_seen": rec.get("last_seen")}


def store_view(count: int) -> dict:
    """{supported, add, reason, count}: whether saved logins work here (a codex binary), whether an account can be added from the board
    (`codex login --device-auth` exists; unknown counts as yes until the probe, started in the background, has answered) and why not."""
    try:
        agent = _agent()
        exe = agent.bin()
        if not exe:
            return {"supported": False, "add": False, "reason": REASON_NOT_INSTALLED, "count": 0}
        da = agent.device_auth(fetch=False)
        if da is None:
            agent.warm_login_caps()
    except Exception:
        return {"supported": False, "add": False, "reason": REASON_NOT_INSTALLED, "count": 0}
    add = da is not False
    return {"supported": True, "add": add, "reason": None if add else REASON_UPDATE, "count": count}


def view(db, *, tail: bool = True) -> dict:
    """The GET /api/codex-accounts body: {current, list: [{key, label, account_id, plan, saved, saved_at, current, added_at, last_seen}], store:
    {supported, add, reason, count}, login: {running, adding, label, replace_key, started_at, url, code, tail, result}}. The current account first, then by when it
    was added. `tail=False` leaves the login's terminal output out (what /api/state carries). Stats a file per account; never raises."""
    try:
        accts, cur = _load(db), current(db)
    except Exception:
        accts, cur = {}, None
    rows, sup = [], supported()
    for rec in accts.values():
        try:
            rows.append(_row(db, rec, cur, sup))
        except Exception:
            continue
    rows.sort(key=lambda r: (not r["current"], str(r["added_at"] or ""), r["key"]))
    try:
        login = login_view()
    except Exception:
        login = {"running": False, "adding": False, "label": None, "replace_key": None, "started_at": None, "url": None, "code": None, "tail": [],
                 "result": None}
    if not tail:
        login = {k: v for k, v in login.items() if k != "tail"}
    return {"current": cur, "list": rows, "store": store_view(sum(1 for r in rows if r["saved"])), "login": login}
