"""Saved Claude logins: keep a copy of each account's login on the box and switch the live login between them with one call.

Why this exists. Claude Code keeps one login per config dir (`<config dir>/.credentials.json` on Linux, mode 0600) and names the account in
its state file (`~/.claude.json` for the default `~/.claude`, `<dir>/.claude.json` otherwise; the `oauthAccount` object). A running
session re-reads the credentials file when its mtime changes, so replacing the file moves every running session to the new login on its
next request. To use several subscriptions the person needs each login kept somewhere and put in place on demand, and a login added
without logging the live one out.

Layout (under `<data dir>/accounts`, every directory 0700, every file 0600; a slot is named by the first 24 hex characters of
sha256(account key): the key can be an email and must not appear on disk as a name):

    <slot>/.credentials.json        a byte copy of Claude's credentials file
    <slot>/.credentials.json.prev   the generation before it (one), for recovery
    <slot>/.claude.json             at least {"oauthAccount": {...}}: who the credentials belong to
    .pending/                       the config dir of the one login in flight (CLAUDE_CONFIG_DIR of `claude auth login`)

Rules this module keeps (tests pin them):
  * The credentials file is an OPAQUE blob. It is never parsed, never logged and no part of it is in an API answer, an event, a kv value,
    a notification or an exception message: bytes are read and written, nothing else. A refresh rotates the token, so only one copy of an
    account's login may be in use: the live file is the truth while the account is live, the saved copy while it is not.
  * `claude` is never run against a slot. The one process that ever has a slot-like directory as its CLAUDE_CONFIG_DIR is the login
    itself, in `.pending`. Who a slot belongs to is read from its `.claude.json` (accounts.read_identity(config_dir=slot, auth=False)).
  * Saved logins need Claude's FILE credentials, so they exist on Linux only (macOS keeps them in the Keychain): on any other platform
    every mutating call raises Unsupported and the view says so.
  * When in doubt do nothing rather than overwrite: an identity that cannot be read, a state file that does not parse, a credentials file
    that is being rewritten while it is read, a login that cannot be saved first, all end the call with nothing changed.
  * One RLock serialises switch, save, seed, forget, finalize and the tick. The small login/result state the views read has its own lock
    so a 3 s poll of /api/state never waits for a save.

Known limit: a Claude session that was running before a switch can write its own stale `oauthAccount` back into the state file. For
90 s observe() reads that as the new account (accounts.hold) and the tick writes the saved account's object again (at most 5 times, while
the credentials file has not changed since the switch). The credentials, which are what authenticate, are the new account's throughout.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import shutil
import stat
import sys
import threading
import time
from pathlib import Path

from . import accounts, claude_auth, login_problem, tmux
from .config import settings
from .db import iso

log = logging.getLogger("ccboard.account_store")

SETTLE = 3                  # a live credentials file younger than this is waited for before it is copied (/login writes it before the identity)
SETTLE_STEP = 0.25
SYNC_SETTLE = 30            # the tick re-saves the live account's credentials only once the file has been quiet this long
HOLD_SECONDS = 90           # accounts.hold after a switch
MAX_REPAIRS = 5
STATE_RETRIES = 5           # a state file that does not parse is Claude mid-write: try again
STATE_PAUSE = 0.05
PENDING_MIN_AGE = 1         # a pending credentials file must be this old (seconds) before it is taken
DEAD_AFTER = 20             # a login pane back at the shell this long without credentials has failed
ABANDON_AFTER = 3600        # a pending login this old is abandoned
RESULT_TTL = 600            # the outcome of the last login is shown this long
WATCH_FOR = 60.0            # after a code is submitted the page must not wait for the 15 s tick: finalize every WATCH_EVERY for this long
WATCH_EVERY = 1.0

STORE = "accounts"
PENDING = ".pending"
CREDS = ".credentials.json"
PREV = ".credentials.json.prev"
STATE = ".claude.json"
KV_SWITCH = "account_switch"       # {from, to, at, creds_stamp: [mtime_ns, size, ino], repairs}
KV_SAVED = "account_saved"         # {key: [mtime_ns, size, ino]}: the live credentials file as it was when the key's slot was last written
REASON = "saved logins need Claude's file credentials (Linux)"
ERR_DID_NOT_COMPLETE = "the login did not complete"

_lock = threading.RLock()
_vlock = threading.Lock()          # guards _login / _result only (the views)
_login: dict = {"adding": False, "email": None, "started_at": None, "ended_seen": None}
_result: dict | None = None
_result_at = 0.0
_watch_gen = 0
_watchers: list[threading.Thread] = []
_sleep = time.sleep                # patched by tests
_wall = time.time                  # patched by tests (file ages are wall-clock mtime comparisons)
_WATCH_SLEEP = time.sleep


class StoreError(Exception):
    """Something the store refuses to do; the message is safe to show (no credentials in it, ever). The API answers 409."""


class Unsupported(StoreError):
    pass


class NoSavedLogin(StoreError):
    pass


class Busy(StoreError):
    pass


class SwitchRefused(StoreError):
    pass


class UnknownAccount(LookupError):
    """No such account in the kv accounts record (the API answers 404)."""


class _Unstable(Exception):
    """A file changed while it was being read."""


class _StateUnreadable(Exception):
    """A Claude state file that does not parse; the message names the file, never its content."""


class _Changed(Exception):
    """A state file was rewritten between our read and our replace."""


_MISSING = object()                # "the file had no oauthAccount key"
_CREATED = object()                # "the file did not exist"


# ------------------------------------------------------------------ platform, paths
def supported() -> bool:
    """Saved logins need Claude's file credentials: Linux. (macOS keeps them in the Keychain; Windows is not a board host.)"""
    return sys.platform.startswith("linux")


def _require() -> None:
    if not supported():
        raise Unsupported(REASON)


def store_dir() -> Path:
    return Path(settings.data_dir) / STORE


def slot_dir(key: str) -> Path:
    return store_dir() / hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:24]


def pending_dir() -> Path:
    return store_dir() / PENDING


def _live_dir() -> Path:
    return Path(settings.claude_config_dir)


def _live_creds() -> Path:
    return _live_dir() / CREDS


def has_saved(key: str) -> bool:
    """True when the key's slot holds a non-empty credentials file."""
    try:
        return (slot_dir(key) / CREDS).stat().st_size > 0
    except OSError:
        return False


def _secure_dir(path: Path) -> Path:
    """`path` exists as a directory with mode 0700 (created so, and re-chmodded when it exists wider)."""
    if not path.is_dir():
        path.parent.mkdir(parents=True, exist_ok=True)          # the data dir itself keeps the mode it gets
        with contextlib.suppress(FileExistsError):
            os.mkdir(path, 0o700)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        os.chmod(path, 0o700)
    return path


def _slot_ready(key: str) -> Path:
    _secure_dir(store_dir())
    return _secure_dir(slot_dir(key))


# ------------------------------------------------------------------ bytes: the only code that touches a credentials file's content
def _stamp(path: Path) -> list | None:
    """[mtime_ns, size, inode] of a file, None when it is absent. A list (that is what a kv round trip gives back)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size, st.st_ino]


def _read_plain(path: Path) -> bytes | None:
    """The bytes of one of our own files, None when it is absent."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _read_stable(path: Path) -> tuple[bytes, list] | None:
    """(bytes, stamp) of a file Claude may be replacing, read only while its stamp holds still; None when absent or empty. _Unstable
    when three tries all saw it change."""
    for _ in range(3):
        try:
            before = os.stat(path)
            data = path.read_bytes()
            after = os.stat(path)
        except FileNotFoundError:
            return None
        stamp = [before.st_mtime_ns, before.st_size, before.st_ino]
        if stamp == [after.st_mtime_ns, after.st_size, after.st_ino] and len(data) == after.st_size:
            return (data, stamp) if data else None
        _sleep(0.02)
    raise _Unstable("credentials file kept changing")


def _write_atomic(dest: Path, data: bytes, mode: int = 0o600, *, still=None) -> None:
    """Replace `dest` with `data`: a temp file in the same directory (created 0600, then set to `mode`), fsync, os.replace. `still`, when
    given, is asked just before the replace and must say the destination is as it was (else _Changed and nothing is replaced)."""
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if still is not None and not still():
            raise _Changed()
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _store_creds(slot: Path, data: bytes) -> None:
    """Put `data` in the slot as its credentials; the generation it replaces (when different) becomes `.prev`."""
    dest, prev = slot / CREDS, slot / PREV
    old = _read_plain(dest)
    if old is not None and old != data:
        if old:
            _write_atomic(prev, old, 0o600)
        _write_atomic(dest, data, 0o600)
    elif old is None:
        _write_atomic(dest, data, 0o600)
    else:
        os.chmod(dest, 0o600)


_SLOT_FILES = (CREDS, PREV, STATE)


def _snapshot(slot: Path) -> dict[str, bytes | None]:
    return {n: _read_plain(slot / n) for n in _SLOT_FILES}


def _restore(slot: Path, snap: dict[str, bytes | None]) -> None:
    """Put a slot back as _snapshot saw it (best effort: one file failing does not stop the others)."""
    for n, data in snap.items():
        try:
            if data is None:
                (slot / n).unlink(missing_ok=True)
            else:
                _write_atomic(slot / n, data, 0o600)
        except OSError as e:
            log.warning("could not restore a saved login's file: %s", e.__class__.__name__)


def _wait_settled(path: Path) -> None:
    """A credentials file younger than SETTLE is waited for (in small steps, at most SETTLE in all): a `/login` in a terminal writes the
    credentials first and the identity a moment later, and the pair must not be read in between."""
    for _ in range(int(SETTLE / SETTLE_STEP) + 1):
        try:
            age = _wall() - os.stat(path).st_mtime
        except OSError:
            return
        if age >= SETTLE:
            return
        _sleep(SETTLE_STEP)


# ------------------------------------------------------------------ state files (Claude's .claude.json): the one oauthAccount key
def _dump(data: dict) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _parse_state(path: Path) -> dict | None:
    """A Claude state file as a dict, None when it does not parse (or is not an object). Raises FileNotFoundError when it is absent."""
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read())
    except FileNotFoundError:
        raise
    except (OSError, ValueError, RecursionError, MemoryError):
        return None
    return data if isinstance(data, dict) else None


def _oauth_of(path: Path) -> dict | None:
    """The `oauthAccount` object of a state file; None when the file is absent or has none. _StateUnreadable when it never parses."""
    for i in range(STATE_RETRIES):
        try:
            data = _parse_state(path)
        except FileNotFoundError:
            return None
        if data is not None:
            oa = data.get("oauthAccount")
            return dict(oa) if isinstance(oa, dict) else None
        if i < STATE_RETRIES - 1:
            _sleep(STATE_PAUSE)
    raise _StateUnreadable(path.name)


def _put_oauth(path: Path, oa, *, create: bool = False):
    """Set the one key `oauthAccount` of the state file `path` to `oa` (`_MISSING` removes it), every other key and the file's mode as
    they are (0600 for a file created here). Read-modify-write: retried while the file does not parse and while Claude rewrites it
    between our read and our replace. Never writes a file it could not parse. Returns what _put_oauth must be given to undo it."""
    for i in range(STATE_RETRIES):
        try:
            before = os.stat(path)
        except FileNotFoundError:
            if not create:
                raise
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(path, _dump({"oauthAccount": oa}), 0o600)
            return _CREATED
        try:
            data = _parse_state(path)
        except FileNotFoundError:
            continue
        if data is None:
            if i < STATE_RETRIES - 1:
                _sleep(STATE_PAUSE)
            continue
        old = data.get("oauthAccount", _MISSING)
        if oa is _MISSING:
            data.pop("oauthAccount", None)
        else:
            data["oauthAccount"] = oa
        try:
            _write_atomic(path, _dump(data), stat.S_IMODE(before.st_mode),
                          still=lambda: _stamp(path) == [before.st_mtime_ns, before.st_size, before.st_ino])
        except _Changed:
            continue
        return old
    raise _StateUnreadable(path.name)


def _undo_oauth(path: Path, old) -> None:
    try:
        if old is _CREATED:
            path.unlink(missing_ok=True)
        else:
            _put_oauth(path, old)
    except (OSError, _StateUnreadable) as e:
        log.warning("could not undo a state file change: %s", e.__class__.__name__)


def _live_state_files() -> list[Path]:
    return accounts._candidates(_live_dir())


def _live_oauth() -> dict | None:
    """The oauthAccount object of the live state file (the newest of the two candidates that has one), None when there is none."""
    best: tuple[int, dict] | None = None
    for path in _live_state_files():
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            continue
        with contextlib.suppress(_StateUnreadable):
            oa = _oauth_of(path)
            if oa is not None and (best is None or mtime > best[0]):
                best = (mtime, oa)
    return best[1] if best else None


def _write_live_oauth(oa: dict) -> list[tuple[Path, object]]:
    """Put `oa` into every existing live state file (the first candidate is created when there is none). Returns what to undo. Rolls
    back its own writes and re-raises when one fails."""
    cands = _live_state_files()
    existing = [p for p in cands if p.exists()]
    touched: list[tuple[Path, object]] = []
    try:
        if existing:
            for p in existing:
                touched.append((p, _put_oauth(p, oa)))
        else:
            touched.append((cands[0], _put_oauth(cands[0], oa, create=True)))
    except BaseException:
        for p, old in reversed(touched):
            _undo_oauth(p, old)
        raise
    return touched


# ------------------------------------------------------------------ identity
def _canon(db, ident: dict) -> str:
    """The record key of an identity (accounts._key_for: its own key, or the key of the record with the same email)."""
    return accounts._key_for(accounts._load(db), ident)


def _live_account(db) -> tuple[str | None, dict | None]:
    """(key, identity) of the account that is logged in live, (None, None) when nobody is; the state file is re-read now. SwitchRefused
    when it cannot be read just now (an answer taken on trust is not good enough to decide whose credentials a file holds)."""
    try:
        ident, fresh = accounts._identity(None, True, False)
    except Exception as e:
        log.warning("live identity unreadable: %s", e.__class__.__name__)
        raise SwitchRefused("could not tell who is logged in right now; nothing was changed") from None
    if not fresh:
        raise SwitchRefused("could not tell who is logged in right now; nothing was changed")
    if not ident:
        return None, None
    return _canon(db, ident), ident


def _names(db, key: str) -> bool:
    """Does the live state file name `key` right now?"""
    ident = accounts.read_identity(force=True, auth=False)
    return bool(ident) and _canon(db, ident) == key


def _slot_identity(slot: Path) -> dict | None:
    return accounts.read_identity(config_dir=slot, auth=False, force=True)


def _slot_oauth(db, key: str) -> dict:
    """The oauthAccount object of the key's slot, checked to name `key`. NoSavedLogin when the slot has none or names someone else."""
    slot = slot_dir(key)
    try:
        oa = _oauth_of(slot / STATE)
    except _StateUnreadable:
        oa = None
    ident = _slot_identity(slot) if oa is not None else None
    if oa is None or not ident or _canon(db, ident) != key:
        raise NoSavedLogin("the saved login is incomplete; log in to the account again from Settings")
    return oa


def _kv(db, key: str):
    rec = db.kv_get(key)
    return rec.get("value") if isinstance(rec, dict) else None


def _note_saved(db, key: str, stamp) -> None:
    saved = _kv(db, KV_SAVED)
    saved = dict(saved) if isinstance(saved, dict) else {}
    saved[key] = stamp
    db.kv_set(KV_SAVED, saved)


def _drop_saved(db, key: str) -> None:
    saved = _kv(db, KV_SAVED)
    if isinstance(saved, dict) and key in saved:
        saved = {k: v for k, v in saved.items() if k != key}
        db.kv_set(KV_SAVED, saved)


# ------------------------------------------------------------------ save, seed
def _save_live(db, key: str, now) -> list | None:
    """The work of save_live. Returns the stamp of the live credentials file that was copied, None when nothing was saved (the identity
    does not name `key`, no credentials, or the identity changed while copying: then the slot is put back as it was)."""
    live = _live_creds()
    _wait_settled(live)
    if not _names(db, key):
        return None
    got = _read_stable(live)
    if got is None:
        return None
    data, stamp = got
    oa = _live_oauth()
    if oa is None:
        return None
    slot = _slot_ready(key)
    snap = _snapshot(slot)
    try:
        _store_creds(slot, data)
        try:
            _put_oauth(slot / STATE, oa, create=True)
        except _StateUnreadable:                                # our own file, damaged: write it afresh
            _write_atomic(slot / STATE, _dump({"oauthAccount": oa}), 0o600)
        if not _names(db, key):
            _restore(slot, snap)
            return None
    except BaseException:
        _restore(slot, snap)
        raise
    _note_saved(db, key, stamp)
    return stamp


def save_live(db, key: str, *, now=None) -> bool:
    """Copy the LIVE credentials into the key's slot (the copy it replaces becomes `.prev` when different) and refresh the slot's
    oauthAccount from the live state file. The live identity must name `key` before AND after the copy, else the copy is undone and
    False is returned; a credentials file younger than SETTLE is waited for first. False too for anything else that stops the save
    (no credentials, a file that kept changing, a state file that does not parse): the slot is then as it was."""
    _require()
    with _lock:
        try:
            return _save_live(db, key, now) is not None
        except (_Unstable, _StateUnreadable) as e:
            log.info("saved login not updated: %s", e.__class__.__name__)
            return False
        except OSError as e:
            log.warning("saved login not updated: %s", e.__class__.__name__)
            return False


def _remember_live(db, ident: dict, now) -> None:
    """The live account has a kv record (a saved login is switched to through it)."""
    accounts.remember(db, {**ident, "config_dir": None}, now)


def seed_current(db, now=None) -> str | None:
    """If somebody is logged in live and that account has no slot yet, create it (save_live). Returns the live account's key, None when
    nobody is logged in or the copy was refused. This is what makes a bad switch one tap from recovery."""
    _require()
    with _lock:
        try:
            key, ident = _live_account(db)
        except SwitchRefused:
            return None
        if key is None:
            return None
        if not has_saved(key):
            if key not in accounts._load(db):
                _remember_live(db, ident, now)
            if not save_live(db, key, now=now):
                return None
        return key


# ------------------------------------------------------------------ switch
def _login_running() -> bool:
    try:
        return bool(claude_auth.login_state().get("running"))
    except Exception:
        return False


def _login_in_flight() -> bool:
    """A login started from Settings is waiting for its code (the pending dir is in use and its session is running)."""
    with _vlock:
        adding = _login["adding"]
    return bool(adding) and _login_running()


def _apply(db, key: str, slot_oa: dict, frm: str | None, now) -> None:
    """Steps 5 to 8 of a switch: the slot's credentials become the live ones, its oauthAccount goes into the live state file(s), the
    switch is recorded, the hold set and the current account moved. Anything failing in the first two puts the previous live login
    back (credentials from the copy taken just before, state files by undoing the one key) and ends in SwitchRefused."""
    live = _live_creds()
    got = _read_stable(_slot_creds(key))
    if got is None:
        raise NoSavedLogin("the saved login is incomplete; log in to the account again from Settings")
    new_bytes = got[0]
    old_live = _read_plain(live)
    _live_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    touched: list[tuple[Path, object]] = []
    try:
        _write_atomic(live, new_bytes, 0o600)
        touched = _write_live_oauth(slot_oa)
    except BaseException as e:
        log.warning("switch to a saved login failed (%s): putting the previous login back", e.__class__.__name__)
        for p, old in reversed(touched):
            _undo_oauth(p, old)
        try:
            if old_live is None:
                live.unlink(missing_ok=True)
            else:
                _write_atomic(live, old_live, 0o600)
        except OSError as e2:
            log.error("could not put the previous login back (%s); its saved copy is in the store", e2.__class__.__name__)
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        raise SwitchRefused("could not apply the saved login; the previous login was put back") from None
    stamp = _stamp(live)
    db.kv_set(KV_SWITCH, {"from": frm, "to": key, "at": iso(now), "creds_stamp": stamp, "repairs": 0})
    _note_saved(db, key, stamp)                                  # live and the slot hold the same bytes now
    if frm is not None:
        accounts.hold(to=key, frm=frm, seconds=HOLD_SECONDS)
    claude_auth.invalidate()
    accounts.invalidate()
    try:
        ident = _slot_identity(slot_dir(key))
        if ident:
            accounts.adopt(db, {**ident, "config_dir": str(settings.claude_config_dir)}, now)
    except Exception as e:                                       # the switch happened; the next observe() tick catches the kv up
        log.warning("switched, but the current account was not recorded at once: %s", e.__class__.__name__)
    log.info("switched account %s -> %s", accounts.label(db, frm) if frm else "-", accounts.label(db, key))


def _slot_creds(key: str) -> Path:
    return slot_dir(key) / CREDS


def switch(db, key: str, *, now=None) -> dict:
    """Make `key` the live login. UnknownAccount (no such account), NoSavedLogin (nothing saved for it), Busy (a login from Settings
    is in flight), SwitchRefused (the current login could not be saved first, or the switch could not be applied: nothing is changed
    then, or the previous login is put back). Switching to the account that is live is a no-op ({"already": True})."""
    _require()
    with _lock:
        if key not in accounts._load(db):
            raise UnknownAccount("no such account")
        if not has_saved(key):
            raise NoSavedLogin("there is no saved login for this account; log in to it from Settings first")
        if _login_in_flight():
            raise Busy("a login is in progress; finish or cancel it first")
        cur, cur_ident = _live_account(db)
        if cur == key:
            return {"ok": True, "already": True, "from": cur, "to": key}
        slot_oa = _slot_oauth(db, key)
        if cur is not None and cur not in accounts._load(db):
            _remember_live(db, cur_ident, now)                   # a terminal /login the tick has not seen yet: the slot saved below must be reachable
        live = _live_creds()
        st = _stamp(live)
        have_live = bool(st and st[1] > 0)
        if cur is not None and have_live:
            for _ in range(3):                                   # the live file may be rewritten between the save and the overwrite
                try:
                    stamp = _save_live(db, cur, now)
                except (_Unstable, _StateUnreadable, OSError) as e:
                    log.info("current login not saved: %s", e.__class__.__name__)
                    stamp = None
                if stamp is None:
                    raise SwitchRefused("could not save the current login first; nothing was changed")
                if _stamp(live) == stamp:
                    break
            else:
                raise SwitchRefused("could not save the current login first; nothing was changed")
        elif cur is None and have_live:
            raise SwitchRefused("the login in place belongs to an account the board cannot name; nothing was changed")
        _apply(db, key, slot_oa, cur, now)
        return {"ok": True, "already": False, "from": cur, "to": key}


def forget(db, key: str) -> dict:
    """Delete the key's saved login. The kv accounts record stays (usage history). UnknownAccount for an account the board never saw,
    Busy for the live account (switch to another one first). Returns {"ok": True, "forgotten": whether there was a slot}."""
    _require()
    with _lock:
        if key not in accounts._load(db):
            raise UnknownAccount("no such account")
        cur, _ident = _live_account(db)
        if cur == key:
            raise Busy("this account is the live login; switch to another account first")
        slot = slot_dir(key)
        existed = slot.exists()
        shutil.rmtree(slot, ignore_errors=True)
        _drop_saved(db, key)
        return {"ok": True, "forgotten": existed}


# ------------------------------------------------------------------ adding a login (claude auth login in .pending)
def _reset_login() -> None:
    with _vlock:
        _login.update(adding=False, email=None, started_at=None, ended_seen=None)


def _set_result(result: dict | None, at: float) -> None:
    global _result, _result_at
    with _vlock:
        _result, _result_at = result, at


def start_login(db, email: str | None = None, *, restart: bool = False) -> None:
    """Start `claude auth login` for a NEW account in the internal tmux session, with an empty `.pending` as its config dir (the live
    login is not touched). One at a time: Busy while any login session is running, unless `restart`. ValueError for a bad email."""
    _require()
    email = claude_auth.clean_email(email)
    with _lock:
        if not restart and _login_running():
            raise Busy("a login is already running")
        pend = pending_dir()
        _secure_dir(store_dir())
        shutil.rmtree(pend, ignore_errors=True)
        _secure_dir(pend)
        try:
            claude_auth.start_login(config_dir=pend, email=email)
        except BaseException:
            shutil.rmtree(pend, ignore_errors=True)
            raise
        _set_result(None, 0.0)
        with _vlock:
            _login.update(adding=True, email=email, started_at=_wall(), ended_seen=None)


def _kill_login_session() -> None:
    with contextlib.suppress(Exception):
        tmux.kill_session(tmux.LOGIN_SESSION)


def cancel_login() -> None:
    """Stop a login started from Settings: its tmux session is killed, `.pending` removed, the state and the last result cleared. A
    login session the board did not start for `.pending` (the plain /api/claude/login one) is left alone."""
    with _lock:
        with _vlock:
            ours = bool(_login["adding"])
        pend = pending_dir()
        if ours or pend.exists():
            _kill_login_session()
            shutil.rmtree(pend, ignore_errors=True)
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


def _complete(db, pend: Path, ident: dict, t: float, now, asked: str | None = None) -> dict | None:
    """The pending login named an account: move its two files into the account's slot, register the account (not as the current one),
    close the login and, when nothing else is logged in live or the live account is this very account, put it in place.

    A re-login (the account already had a slot): the fresh login replaces the saved one (the generation it replaces becomes `.prev`) and
    the result says `replaced`. When the live account IS this account (its login is the dead one) the fresh login goes live at once, and
    the live bytes it replaces are kept in the slot's `.prev` rather than first saved over the fresh slot. `asked` is the email the login
    was started for: when the person signed in as somebody else the result says `different_account` (that account got a row of its own)."""
    key = accounts.remember(db, {**ident, "config_dir": None}, now)
    _kill_login_session()                                        # nothing writes into .pending any more
    slot = _slot_ready(key)
    snap = _snapshot(slot)
    replaced = has_saved(key)
    try:
        got = _read_stable(pend / CREDS)
        claude_json = _read_plain(pend / STATE)
        if got is None or claude_json is None:
            raise _Unstable("pending login incomplete")
        _store_creds(slot, got[0])
        _write_atomic(slot / STATE, claude_json, 0o600)
    except (_Unstable, OSError) as e:
        _restore(slot, snap)
        log.warning("a finished login could not be saved yet: %s", e.__class__.__name__)
        return None                                              # .pending stays: the next call tries again (or abandons it after an hour)
    shutil.rmtree(pend, ignore_errors=True)
    with contextlib.suppress(OSError):
        _note_saved(db, key, _stamp(slot / CREDS))               # "login saved <age> ago" counts from now
    live_ok = False
    try:
        cur, _i = _live_account(db)
        live_bytes = _read_plain(_live_creds())
        nobody = cur is None and not live_bytes
        if nobody or cur == key:
            if cur == key and live_bytes and live_bytes != got[0]:
                with contextlib.suppress(OSError):               # the live (dead) login is the generation just before the fresh one
                    _write_atomic(slot / PREV, live_bytes, 0o600)
            _apply(db, key, _slot_oauth(db, key), None, now)
            live_ok = True
    except (StoreError, OSError) as e:
        log.warning("the new login is saved but was not put in place: %s", e.__class__.__name__)
    login_problem.clear(db, "claude", key)                       # a login for this account finished: whatever was reported about it is over
    out = {"ok": True, "key": key, "name": accounts.label(db, key), "live": live_ok, "at": iso(t)}
    if replaced:
        out["replaced"] = True
    got_email = ident.get("email")
    if asked and got_email and got_email.strip().lower() != asked.strip().lower():
        out["different_account"] = got_email
    return _decide(out)


def finalize(db, now=None) -> dict | None:
    """Look at the pending login. Credentials there that are at least PENDING_MIN_AGE old, with an identity in its `.claude.json`: save
    them as the account's login (see _complete). A login session that has been back at the shell for more than DEAD_AFTER seconds
    without credentials, or a pending dir older than ABANDON_AFTER: the login failed and `.pending` is removed. Returns the outcome
    ({"ok": True, "key", "name", "live", "at"} or {"ok": False, "error", "at"}) when something was decided, else None."""
    if not supported():
        return None
    with _lock:
        pend = pending_dir()
        with _vlock:
            adding, started, asked = bool(_login["adding"]), _login["started_at"], _login["email"]
        if not pend.is_dir():
            if adding:
                _reset_login()
            return None
        t = _wall() if now is None else accounts._to_epoch(now)
        try:
            cst = os.stat(pend / CREDS)
        except OSError:
            cst = None
        have_creds = bool(cst and cst.st_size > 0)
        if have_creds and t - cst.st_mtime >= PENDING_MIN_AGE:
            ident = accounts.read_identity(config_dir=pend, auth=False, force=True)
            if ident:
                return _complete(db, pend, ident, t, now, asked)
        if started is None:
            with contextlib.suppress(OSError):
                started = pend.stat().st_mtime
        if started is not None and t - started > ABANDON_AFTER:
            return _fail(pend, t)
        if have_creds:
            return None                                          # the identity (or the file's age) is a moment away
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


def finalize_soon(db) -> threading.Thread:
    """After a code was submitted: a daemon thread that runs finalize() every WATCH_EVERY seconds for up to WATCH_FOR, stopping at the
    first outcome (or when there is no pending login). The page must not wait for the 15 s tick."""
    gen = _watch_gen

    def run() -> None:
        end = time.monotonic() + WATCH_FOR
        while time.monotonic() < end and gen == _watch_gen:
            try:
                if finalize(db) is not None or not pending_dir().exists():
                    return
            except Exception as e:
                log.warning("finalize failed: %s", e.__class__.__name__)
            _WATCH_SLEEP(WATCH_EVERY)

    th = threading.Thread(target=run, name="account-finalize", daemon=True)
    _watchers[:] = [w for w in _watchers if w.is_alive()]
    _watchers.append(th)
    th.start()
    return th


def stop_watchers(timeout: float = 5.0) -> None:
    """Stop every finalize_soon thread (tests; and nothing else needs it: they end by themselves)."""
    global _watch_gen
    _watch_gen += 1
    for th in list(_watchers):
        th.join(timeout)
    _watchers.clear()


def login_view() -> dict:
    """{adding, email, started_at, result} for the Settings page: what is being added, and how the last login ended (None once older
    than RESULT_TTL). Never touches a file or a process."""
    with _vlock:
        res = dict(_result) if _result is not None and _wall() - _result_at <= RESULT_TTL else None
        started = _login["started_at"]
        return {"adding": bool(_login["adding"]), "email": _login["email"],
                "started_at": iso(started) if started else None, "result": res}


# ------------------------------------------------------------------ the tick
def _repair(db, now) -> None:
    """A Claude session that was running before the switch wrote its stale oauthAccount back: while the credentials file is still the one
    the switch wrote, put the new account's object in again (at most MAX_REPAIRS times)."""
    with _lock:
        sw = _kv(db, KV_SWITCH)
        if not isinstance(sw, dict) or not sw.get("from") or not sw.get("to"):
            return
        repairs = sw.get("repairs") if isinstance(sw.get("repairs"), int) else 0
        if repairs >= MAX_REPAIRS or _stamp(_live_creds()) != sw.get("creds_stamp"):
            return
        ident, fresh = accounts._identity(None, True, False)
        if not ident or not fresh or _canon(db, ident) != sw["from"] or not has_saved(sw["to"]):
            return
        _write_live_oauth(_slot_oauth(db, sw["to"]))
        db.kv_set(KV_SWITCH, {**sw, "repairs": repairs + 1})
        accounts.invalidate()
        log.info("repaired the state file after a stale write (%d of %d)", repairs + 1, MAX_REPAIRS)


def _sync(db, now) -> None:
    """Keep the live account's saved copy current (its token is rotated by use) and create the slot of a login that has none (an account
    logged in from a terminal). Never for the account a switch has just left: a stale identity may still name it while the credentials
    are already the new account's, and copying them into its slot would destroy its saved login."""
    with _lock:
        try:
            key, ident = _live_account(db)
        except SwitchRefused:
            return
        if key is None:
            return
        held = accounts.held()
        if held and held["frm"] == key:
            return
        sw = _kv(db, KV_SWITCH)
        if isinstance(sw, dict) and sw.get("from") == key:
            return
        if not has_saved(key):
            seed_current(db, now)
            return
        live = _live_creds()
        stamp = _stamp(live)
        if stamp is None or stamp[1] <= 0:
            return
        saved = _kv(db, KV_SAVED)
        if isinstance(saved, dict) and saved.get(key) == stamp:
            return
        t = _wall() if now is None else accounts._to_epoch(now)
        if t - stamp[0] / 1e9 < SYNC_SETTLE:
            return
        save_live(db, key, now=now)


def tick(db, now=None) -> None:
    """The Sampler's 15 s tick: (a) finish a login that has its credentials, (b) repair a stale state file after a switch, (c) keep the
    live account's saved copy fresh. Never raises (class names only in the log); nothing at all where saved logins are not supported."""
    if not supported():
        return
    for step in (finalize, _repair, _sync):
        try:
            step(db, now)
        except Exception as e:
            log.warning("saved logins: %s failed: %s", step.__name__.strip("_"), e.__class__.__name__)


# ------------------------------------------------------------------ views
def _saved_at(key: str, stamps) -> str | None:
    """When the key's saved login was last written, as an ISO time: the mtime of the credentials file at the moment its copy was taken
    (the `_note_saved` stamp, kv account_saved), else the slot's own file mtime. Only a stamp is looked at, never a file's content."""
    st = stamps.get(key) if isinstance(stamps, dict) else None
    ns = st[0] if isinstance(st, list) and st and isinstance(st[0], (int, float)) and not isinstance(st[0], bool) else None
    if not ns:
        try:
            ns = os.stat(_slot_creds(key)).st_mtime_ns
        except OSError:
            return None
    try:
        return iso(ns / 1e9)
    except (ValueError, OverflowError, OSError):
        return None


def _problem_view(db, current: str | None) -> dict | None:
    """state.accounts.problem: the kv login_problem record ({at, agent, account, session, message}) plus `label` (what the account is
    called) and `back` ({key, label} of the account to switch back to) when the failing account is the live one and the failure came
    within login_problem.SWITCH_WINDOW of a switch to it from an account that still has its saved login. None when there is no problem."""
    rec = login_problem.get(db)
    if rec is None:
        return None
    key = rec.get("account")
    out = {**rec, "label": None, "back": None}
    if rec.get("agent") == "claude":
        out["label"] = accounts.label(db, key) if key else None
        sw = _kv(db, KV_SWITCH)
        if key and key == current and isinstance(sw, dict) and sw.get("to") == key and sw.get("from") not in (None, key):
            try:
                gap = accounts._to_epoch(rec.get("at")) - accounts._to_epoch(sw.get("at"))
            except (ValueError, TypeError):
                gap = None
            if gap is not None and 0 <= gap <= login_problem.SWITCH_WINDOW and sw["from"] in accounts._load(db) and has_saved(sw["from"]):
                out["back"] = {"key": sw["from"], "label": accounts.label(db, sw["from"])}
    else:
        accts = db.kv_get("codex_accounts")
        row = ((accts or {}).get("value") or {}).get(key) if key else None
        out["label"] = row.get("label") if isinstance(row, dict) else None
    return out


def decorate(view: dict, db=None) -> dict:
    """accounts.view() with `saved` and `saved_at` (when the saved login was last written; None without one) on every list row, a top-level
    `store: {supported, reason, count}` (count = accounts with a saved login) and `problem` (see _problem_view; given a `db`, else None).
    Stats a file per account, nothing more; never raises."""
    sup = supported()
    stamps = None
    if db is not None:
        try:
            stamps = _kv(db, KV_SAVED)
        except Exception:
            stamps = None
    rows, n = [], 0
    for r in (view.get("list") if isinstance(view, dict) else None) or []:
        try:
            saved = sup and has_saved(r["key"])
        except Exception:
            saved = False
        n += 1 if saved else 0
        rows.append({**r, "saved": bool(saved), "saved_at": _saved_at(r.get("key"), stamps) if saved else None})
    out = dict(view) if isinstance(view, dict) else {"current": None}
    out["list"] = rows
    out["store"] = {"supported": sup, "reason": None if sup else REASON, "count": n}
    try:
        out["problem"] = _problem_view(db, out.get("current")) if db is not None else None
    except Exception as e:
        log.warning("login problem view failed: %s", e.__class__.__name__)
        out["problem"] = None
    return out
