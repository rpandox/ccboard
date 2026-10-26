"""Opt-in runtime profiles. Credentials are handed over and fingerprinted as opaque bytes."""
from __future__ import annotations

import hashlib
import ctypes
import struct
from functools import wraps
import json
import os
import re
import shlex
import sqlite3
import stat
import sys
import threading
import time
from pathlib import Path

from .config import settings
from . import projects
from scripts.profile_env import UNSET, PREFIXES, scrub, setup_token_value
from .handover import MARKER as READ_ONLY, owner, HandoverError

ACCOUNT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
PROVIDERS = ("claude", "codex")
SETUP_TOKEN = ".setup-token"
MAC_REASON = "the Mac Keychain adapter comes in P5"
SWITCH_REASON = "a pool session is running on a profile"
_migration_lock = threading.RLock()


def require_enabled():
    if not settings.pools:
        raise projects.BadRequest("pools are off")


def validate_account_id(account_id: str) -> str:
    if not isinstance(account_id, str) or not ACCOUNT_ID.fullmatch(account_id):
        raise projects.BadRequest("invalid account id")
    return account_id


def _root() -> Path:
    root = Path(settings.data_dir).resolve() / "profiles"
    if root.is_symlink():
        raise projects.BadRequest("profile path escapes profiles")
    return root


def _contained(path: Path) -> Path:
    root = _root()
    if not path.resolve().is_relative_to(root):
        raise projects.BadRequest("profile path escapes profiles")
    return path.resolve()


def profile_dir(provider: str, account_id: str) -> Path:
    if provider not in PROVIDERS:
        raise projects.BadRequest("invalid profile provider")
    return _contained(_root() / provider / validate_account_id(account_id))


def _mkdir(path: Path):
    _contained(path)
    if path != _root():
        _mkdir(path.parent)
    if path == _root():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)


def _plain(path: Path) -> bytes | None:
    # O_NOFOLLOW also protects credential reads from a replaced file symlink.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise projects.BadRequest("profile file must be a regular file")
        return f.read()


def _write(path: Path, data: bytes):
    _contained(path)
    if path.is_symlink():
        raise projects.BadRequest("profile file must not be a symlink")
    from . import platform as plat
    plat.secure_write(path, data, 0o600)


def _link(path: Path, target: Path):
    # Only these explicit transcript links may leave the profiles tree.
    if path.is_symlink():
        if path.resolve() != target.resolve():
            raise projects.BadRequest("unexpected transcript symlink")
    elif path.exists():
        raise projects.BadRequest("transcript path must be a symlink")
    else:
        path.symlink_to(target, target_is_directory=True)


def ensure_profile(provider: str, account_id: str, *, setup_token: bool = False) -> Path:
    require_enabled()
    path = profile_dir(provider, account_id)
    if provider == "claude" and sys.platform == "darwin" and not setup_token and not (path / SETUP_TOKEN).is_file():
        raise projects.BadRequest(MAC_REASON)
    leaf = "projects" if provider == "claude" else "sessions"
    home = settings.claude_config_dir if provider == "claude" else settings.codex_home
    transcript_root = Path(home).resolve() / leaf
    if not transcript_root.is_dir():
        raise projects.BadRequest(f"transcript root does not exist: {transcript_root}")
    if (path / SETUP_TOKEN).exists() or (path / SETUP_TOKEN).is_symlink():
        try:
            setup_token_value(path / SETUP_TOKEN)
        except (OSError, ValueError) as e:
            raise projects.BadRequest(str(e)) from None
    _mkdir(path)
    for leaf in ("settings.json", "hooks.json", "config.toml", SETUP_TOKEN, ".credentials.json", "auth.json"):
        if (path / leaf).is_symlink():
            raise projects.BadRequest("profile file must not be a symlink")
    shared = _root() / "_shared" / provider
    _mkdir(shared)
    leaf = "projects" if provider == "claude" else "sessions"
    home = settings.claude_config_dir if provider == "claude" else settings.codex_home
    transcript_root = Path(home).resolve() / leaf
    _link(shared / leaf, transcript_root)
    _link(path / leaf, shared / leaf)
    if provider == "claude":
        from .agents.claude import _claude_settings_mod
        installer = _claude_settings_mod()
        if installer is None:
            raise projects.BadRequest("Claude hook template is unavailable")
        from .doctor import _app_dir
        template = installer.install({}, _app_dir().resolve(),
                                     remote_approve=os.environ.get("CCBOARD_REMOTE_APPROVE", "1") != "0",
                                     approve_timeout=settings.approve_timeout)
        if not (path / "settings.json").exists():
            _write(path / "settings.json", json.dumps(template).encode())
        else:
            _contained(path / "settings.json")
    else:
        from scripts import codex_hooks
        from .doctor import _app_dir
        hooks_file = path / "hooks.json"
        data = json.loads(_plain(hooks_file) or b"{}")
        _write(hooks_file, json.dumps(codex_hooks.install(data, _app_dir().resolve(),
               remote_approve=os.environ.get("CCBOARD_REMOTE_APPROVE", "1") != "0",
               approve_timeout=settings.approve_timeout, async_hooks=codex_hooks.async_default())).encode())
        if not (path / "config.toml").exists():
            _write(path / "config.toml", b'cli_auth_credentials_store = "file"\n')
        else:
            _contained(path / "config.toml")
    return path.resolve()


def launch_env(provider: str, account_id: str, *, prepare=True) -> tuple[list[str], dict[str, str]]:
    path = ensure_profile(provider, account_id) if prepare else profile_dir(provider, account_id)
    unset = list(UNSET) + sorted(k for k in os.environ if k.startswith(PREFIXES))
    selected = {"CLAUDE_CONFIG_DIR" if provider == "claude" else "CODEX_HOME": str(path)}
    if provider == "codex":
        selected["CODEX_SQLITE_HOME"] = str(Path(settings.codex_home).resolve())
    return unset, selected


def child_env(provider: str, account_id: str, base=None) -> dict:
    _, selected = launch_env(provider, account_id)
    env = scrub(os.environ if base is None else base)
    env.update(selected)
    if provider == "claude":
        try:
            token = setup_token_value(Path(selected["CLAUDE_CONFIG_DIR"]) / SETUP_TOKEN)
        except (OSError, ValueError) as e:
            raise projects.BadRequest(str(e)) from None
        if token is not None:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def wrap_command(command: str, provider: str, account_id: str, *, prepare=True) -> str:
    unset, selected = launch_env(provider, account_id, prepare=prepare)
    # Run after the login shell's rc. The small exec shim also removes federation variables
    # known only to the tmux server/shell, and reads setup-token bytes without putting them in argv.
    from .doctor import _app_dir
    python = os.environ.get("CCBOARD_HOST_PYTHON", "/usr/bin/python3") if settings.runtime == "docker" else sys.executable
    prefix = ["env", *[v for key in unset for v in ("-u", key)],
              *[f"{key}={value}" for key, value in selected.items()], python,
              str(_app_dir().resolve() / "scripts" / "profile_exec.py")]
    return shlex.join(prefix) + " " + command


def require_account(db, provider, account_id):
    if account_id is None:
        return
    require_enabled()
    profile_dir(provider, account_id)
    with db.lock:
        row = db.conn.execute("SELECT provider FROM pool_account WHERE id=?", (account_id,)).fetchone()
    if row is None or row[0] != provider:
        raise projects.BadRequest("no such account profile")



def accounts_view(db) -> list[dict]:
    with db.lock:
        return [dict(r) for r in db.conn.execute("SELECT id, provider, label, state, cred_version FROM pool_account ORDER BY id")]


def credential_path(provider, account_id):
    path = profile_dir(provider, account_id)
    return path / (SETUP_TOKEN if provider == "claude" and (path / SETUP_TOKEN).is_file()
                   else ".credentials.json" if provider == "claude" else "auth.json")


def credential_version(provider, account_id):
    raw = _plain(credential_path(provider, account_id))
    return hashlib.sha256(raw).hexdigest()[:16] if raw is not None else None


def _files(provider):
    from . import account_store, codex_accounts
    return ((account_store.CREDS, account_store.STATE, account_store.PREV) if provider == "claude"
            else (codex_accounts.AUTH, codex_accounts.PREV))


def _move(source, dest):
    """Durably install opaque bytes before unlinking the old owner. Reruns accept identical bytes."""
    from . import platform as plat
    raw = _plain(source)
    if raw is None:
        return
    existing = _plain(dest)
    if existing is not None and existing != raw:
        raise projects.BadRequest("credential destinations disagree; release or repair before retrying")
    plat.secure_write(dest, raw, 0o600)
    # Persist the new directory entry before removing the old owner.
    for directory in (dest.parent, source.parent):
        if directory == source.parent:
            source.unlink()
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _set_slot_owner(db, provider, key, account_id):
    from . import accounts, codex_accounts
    kv = accounts.KV_ACCOUNTS if provider == "claude" else codex_accounts.KV_ACCOUNTS
    records = (db.kv_get(kv) or {}).get("value") or {}
    if key in records and records[key].get("pool_account") != account_id:
        if account_id:
            records[key]["pool_account"] = account_id
        else:
            records[key].pop("pool_account", None)
        db.kv_set(kv, records)


def refresh_handover_views(db):
    """Once at startup, recover UI metadata from markers; no marker I/O on state polls."""
    from . import accounts, account_store, codex_accounts
    for provider, store, records in (("claude", account_store, accounts._load(db)),
                                     ("codex", codex_accounts, codex_accounts._load(db))):
        for key in records:
            try:
                _set_slot_owner(db, provider, key, owner(store.slot_dir(key)))
            except (OSError, ValueError):
                continue


def migrate(db):
    if not settings.pools:
        return []
    from . import account_store, accounts, codex_accounts, platform as plat
    from .db import now
    results = []
    with _migration_lock, account_store._lock, codex_accounts._lock:
        for provider, records, store in (("claude", accounts._load(db), account_store),
                                         ("codex", codex_accounts._load(db), codex_accounts)):
            for key, rec in records.items():
                result = {"slot": key}
                try:
                    slot = store.slot_dir(key)
                    aid = provider + "-" + hashlib.sha256(key.encode()).hexdigest()[:24]
                    path = profile_dir(provider, aid)
                    marked = owner(slot)
                    exists = db.conn.execute("SELECT state FROM pool_account WHERE id=?", (aid,)).fetchone()
                    if exists and exists[0] == "releasing":
                        raise projects.BadRequest("release is incomplete; retry Release")
                    if not marked and not exists and not store.has_saved(key) and not path.exists():
                        continue
                    if provider == "claude":
                        live = account_store._live_account(db)[0]
                    else:
                        got = codex_accounts._live_bytes()
                        live = (codex_accounts._matching_slot(got[0]) if got else None) or codex_accounts.current(db)
                    if key == live:
                        result["skipped"] = "live login: switch the legacy login to another account first"
                    else:
                        if marked and marked != aid:
                            raise projects.BadRequest("handover marker names another account")
                        path = ensure_profile(provider, aid)
                        # Claim the slot before the first move. A crash cannot make it usable again.
                        if not marked:
                            plat.secure_write(slot / READ_ONLY, (aid + "\n").encode(), 0o600)
                        _set_slot_owner(db, provider, key, aid)
                        for leaf in _files(provider):
                            _move(slot / leaf, path / leaf)
                        raw = _plain(path / _files(provider)[0])
                        if not raw:
                            raise projects.BadRequest("credentials are missing from both slot and profile")
                        with db.lock:
                            db.conn.execute("INSERT OR IGNORE INTO pool_account VALUES (?,?,?,?,?,?)",
                                            (aid, provider, rec.get("label") or aid, "ready", hashlib.sha256(raw).hexdigest()[:16], now()))
                        # Metadata is not needed by the handed-over slot.
                        if provider == "codex":
                            (slot / codex_accounts.META).unlink(missing_ok=True)
                        result.update(moved=aid)
                except Exception as e:
                    if isinstance(e, (projects.BadRequest, HandoverError, account_store.StoreError, codex_accounts.StoreError)):
                        result["error"] = str(e)
                    elif isinstance(e, OSError):
                        result["error"] = f"could not access slot files ({e.strerror or type(e).__name__})"
                    elif isinstance(e, sqlite3.Error):
                        result["error"] = "database could not record the handover; retry migration"
                    else:
                        result["error"] = f"invalid slot or profile configuration ({type(e).__name__})"
                results.append(result)
    return results


def release(db, account_id):
    from . import account_store, accounts, codex_accounts
    require_enabled()
    validate_account_id(account_id)
    with _migration_lock, account_store._lock, codex_accounts._lock:
        row = db.conn.execute("SELECT * FROM pool_account WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise projects.BadRequest("no such pool account")
        reconcile_attempts(db)
        if db.conn.execute("SELECT 1 FROM runtime_attempt WHERE account_id=? AND ended_at IS NULL", (account_id,)).fetchone():
            from fastapi import HTTPException
            raise HTTPException(409, "this pool account has an open attempt")
        provider = row["provider"]
        store = account_store if provider == "claude" else codex_accounts
        records = accounts._load(db) if provider == "claude" else codex_accounts._load(db)
        slots = [store.slot_dir(k) for k in records if owner(store.slot_dir(k)) == account_id or
                 (row["state"] == "releasing" and provider + "-" + hashlib.sha256(k.encode()).hexdigest()[:24] == account_id)]
        if len(slots) != 1:
            raise projects.BadRequest("pool account has no unique handover slot")
        slot, path = slots[0], profile_dir(provider, account_id)
        if not _plain(path / _files(provider)[0]) and not _plain(slot / _files(provider)[0]):
            raise projects.BadRequest("credentials are missing from both profile and slot")
        with db.lock:
            db.conn.execute("UPDATE pool_account SET state='releasing' WHERE id=?", (account_id,))
        for leaf in _files(provider):
            _move(path / leaf, slot / leaf)
        (slot / READ_ONLY).unlink(missing_ok=True)
        for key in records:
            if store.slot_dir(key) == slot:
                _set_slot_owner(db, provider, key, None)
        with db.lock:
            db.conn.execute("DELETE FROM pool_account WHERE id=?", (account_id,))
        return {"released": account_id}


def reconcile_attempts(db):
    if not settings.pools:
        return
    from . import tmux
    with db.lock:
        attempts = list(db.conn.execute("SELECT * FROM runtime_attempt WHERE ended_at IS NULL"))
        sessions = list(db.conn.execute("SELECT id,tmux_name,ended_at FROM sessions"))
    for attempt in attempts:
        stale = False
        for session in sessions:
            binding = db.kv_get(f"runtime_session:{session['id']}")
            if binding and binding["value"]["attempt"] == attempt["id"]:
                try:
                    stale = bool(session["ended_at"]) or not tmux.has_session(session["tmux_name"])
                except tmux.TmuxError:
                    stale = False
                break
        if str(attempt["task_id"] or "").startswith("run:"):
            rid = str(attempt["task_id"])[4:]
            run = db.conn.execute("SELECT finished_at FROM runs WHERE id=?", (rid,)).fetchone()
            stale = run is None or bool(run[0])
        if stale:
            finish_attempt(db, attempt["id"], "reconciled")


def guard_switch(db):
    if not settings.pools:
        return
    reconcile_attempts(db)
    with db.lock:
        active = db.conn.execute("SELECT 1 FROM runtime_attempt WHERE ended_at IS NULL LIMIT 1").fetchone()
    if active:
        from fastapi import HTTPException
        raise HTTPException(409, SWITCH_REASON)


def _serialized(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        with _migration_lock:
            return fn(*args, **kwargs)
    return call


@_serialized
def start_attempt(db, provider, account_id, *, task_id=None, session_row=None, provider_session_id=None, resumed_from=None):
    require_account(db, provider, account_id)
    with db.lock:
        row = db.conn.execute("SELECT state FROM pool_account WHERE id=?", (account_id,)).fetchone()
        if row and row[0] == "releasing":
            raise projects.BadRequest("release is incomplete; retry Release")
    from datetime import datetime, timezone
    started_at = datetime.now(timezone.utc).isoformat()
    with db.lock:
        n = 1
        if task_id is not None:
            n = db.conn.execute("SELECT COALESCE(MAX(n),0)+1 FROM runtime_attempt WHERE task_id=?", (str(task_id),)).fetchone()[0]
        elif resumed_from is not None:
            previous = db.conn.execute("SELECT n FROM runtime_attempt WHERE id=?", (resumed_from,)).fetchone()
            n = previous[0] + 1 if previous else 1
        cur = db.conn.execute("INSERT INTO runtime_attempt (task_id,n,account_id,provider,profile_dir,started_at,provider_session_id,resumed_from_attempt_id) VALUES (?,?,?,?,?,?,?,?)",
                              (str(task_id) if task_id is not None else None, n, account_id, provider, str(profile_dir(provider, account_id)), started_at, provider_session_id, resumed_from))
        aid = cur.lastrowid
    if session_row is not None:
        db.kv_set(f"runtime_session:{session_row}", {"attempt": aid})
    return aid


def finish_attempt(db, attempt_id, reason, provider_session_id=None):
    if not settings.pools or attempt_id is None:
        return
    from .db import now
    with db.lock:
        row = db.conn.execute("SELECT * FROM runtime_attempt WHERE id=? AND ended_at IS NULL", (attempt_id,)).fetchone()
    if row is None:
        return
    try:
        version = credential_version(row["provider"], row["account_id"])
    except (OSError, projects.BadRequest):
        version = None
    with db.lock:
        db.conn.execute("UPDATE runtime_attempt SET ended_at=?, end_reason=?, provider_session_id=COALESCE(?,provider_session_id) WHERE id=? AND ended_at IS NULL",
                        (now(), reason, provider_session_id, attempt_id))
        if version is not None:
            db.conn.execute("UPDATE pool_account SET cred_version=? WHERE id=? AND cred_version IS NOT ?", (version, row["account_id"], version))


def sync_session(db, name, reason=None):
    if not settings.pools:
        return
    with db.lock:
        row = db.conn.execute("SELECT id, claude_session_id FROM sessions WHERE tmux_name=? ORDER BY id DESC LIMIT 1", (name,)).fetchone()
    if row is None:
        return
    binding = db.kv_get(f"runtime_session:{row['id']}")
    if not binding:
        return
    aid = binding["value"]["attempt"]
    with db.lock:
        db.conn.execute("UPDATE runtime_attempt SET provider_session_id=COALESCE(?,provider_session_id) WHERE id=?", (row["claude_session_id"], aid))
    if reason:
        finish_attempt(db, aid, reason, row["claude_session_id"])


def attach_task(db, session_row, task_id):
    if not settings.pools:
        return
    binding = db.kv_get(f"runtime_session:{session_row}")
    if binding:
        with db.lock:
            db.conn.execute("UPDATE runtime_attempt SET task_id=? WHERE id=?", (str(task_id), binding["value"]["attempt"]))


def transcript_directory(provider, cwd):
    from datetime import datetime, timezone
    if provider == "claude":
        slug = re.sub(r"[^a-zA-Z0-9-]", "-", str(Path(cwd).resolve()))
        return Path(settings.claude_config_dir) / "projects" / slug
    return Path(settings.codex_home) / "sessions" / datetime.now(timezone.utc).strftime("%Y/%m/%d")


def _created_at(path, st):
    """Use birth time, never ctime (editing an old transcript changes ctime)."""
    if hasattr(st, "st_birthtime"):
        return st.st_birthtime
    if sys.platform == "linux":
        # Linux's statx ABI exposes birth time even where Python's stat does not.
        # struct statx is 256 bytes; stx_mask is at 0, stx_btime at 80.
        libc = ctypes.CDLL(None, use_errno=True)
        statx = getattr(libc, "statx", None)
        if statx is not None:
            buf = ctypes.create_string_buffer(256)
            if statx(-100, os.fsencode(path), 0x100, 0x800, ctypes.byref(buf)) == 0:
                mask = struct.unpack_from("=I", buf.raw)[0]
                if mask & 0x800:
                    seconds, nanos = struct.unpack_from("=qI", buf.raw, 80)
                    return seconds + nanos / 1e9
    # A filesystem without birth times cannot prove that this is a new file.
    return None


def newest_transcript(provider, cwd, started):
    matches = []
    for path in transcript_directory(provider, cwd).glob("*.jsonl"):
        try:
            st = path.stat()
            created = _created_at(path, st)
            if created is None or created <= started:
                continue
            with path.open() as f:
                for line in f:
                    meta = json.loads(line)
                    if provider == "codex":
                        meta = meta.get("payload", {}) if meta.get("type") == "session_meta" else {}
                    sid = meta.get("sessionId" if provider == "claude" else "id")
                    if sid:
                        if meta.get("cwd") and Path(meta["cwd"]).resolve() == Path(cwd).resolve():
                            matches.append(sid)
                        break
        except (OSError, ValueError, AttributeError):
            continue
    return matches[0] if len(matches) == 1 else None


def discover_attempt(db, attempt_id, cwd, provider, started, name=None):
    """A ten-second fallback only for a bound process that has not learned an id yet."""
    def discover():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with db.lock:
                    row = db.conn.execute("SELECT ended_at,provider_session_id FROM runtime_attempt WHERE id=?", (attempt_id,)).fetchone()
                if not row or row["ended_at"] or row["provider_session_id"]:
                    return
                sid = newest_transcript(provider, cwd, started)
                if sid:
                    with db.lock:
                        session = db.conn.execute("SELECT claude_session_id FROM sessions WHERE tmux_name=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1", (name,)).fetchone() if name else None
                        if session and session[0]:
                            return
                        if name and not db.rebind_session_id(name, sid):
                            return
                    with db.lock:
                        db.conn.execute("UPDATE runtime_attempt SET provider_session_id=? WHERE id=? AND provider_session_id IS NULL", (sid, attempt_id))
                    return
            except (OSError, ValueError):
                pass
            except sqlite3.ProgrammingError:
                return
            time.sleep(1)
    if settings.pools:
        if name:
            sync_session(db, name)
        with db.lock:
            row = db.conn.execute("SELECT provider_session_id,started_at FROM runtime_attempt WHERE id=?", (attempt_id,)).fetchone()
        if row is None or row[0]:
            return None
        from datetime import datetime
        started = datetime.fromisoformat(row[1].replace("Z", "+00:00")).timestamp()
        worker = threading.Thread(target=discover, name="profile-discovery", daemon=True)
        worker.start()
        return worker



def previous_attempt(db, name, task_id=None):
    with db.lock:
        if task_id is not None:
            row = db.conn.execute("SELECT id FROM runtime_attempt WHERE task_id=? ORDER BY id DESC LIMIT 1", (str(task_id),)).fetchone()
            if row:
                return row[0]
        row = db.conn.execute("SELECT id FROM sessions WHERE tmux_name=? ORDER BY id DESC LIMIT 1", (name,)).fetchone()
    binding = db.kv_get(f"runtime_session:{row[0]}") if row else None
    return binding["value"]["attempt"] if binding else None
