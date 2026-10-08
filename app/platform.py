"""The one place in the board that knows which operating system it runs on (issue #116).

Every use of the uid, a POSIX-only module (`pwd`, `fcntl`), `/proc`, `ss`, a service manager or a Linux-only hint goes through here.
On Linux each function behaves exactly as the code it replaced did (same parsing, same argv, same bytes, same strings); on macOS the
same questions are answered through psutil (or `lsof`) so features stop degrading silently; on native Windows the board at least
imports (native Windows stays unsupported, issue #124).

Naming: this module is `app.platform`. Nothing may put `app/` itself on sys.path, so `import platform` elsewhere still gets the
standard library (pinned by tests/test_platform.py).

Sections: system flags, uid and passwd, locks, files, defaults, runtime, processes (one PROC_ROOT seam), host numbers, hints.
"""
from __future__ import annotations

import contextlib
import functools
import logging
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

log = logging.getLogger("ccboard")

# ---------------------------------------------------------------- system flags (tests patch these)

IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"


@functools.lru_cache(maxsize=1)
def is_wsl() -> bool:
    """Linux inside WSL (1 or 2): /proc/version names Microsoft."""
    if not IS_LINUX:
        return False
    try:
        return "microsoft" in Path("/proc/version").read_text(errors="replace").lower()
    except OSError:
        return False


# ---------------------------------------------------------------- uid and passwd


def current_uid() -> int | None:
    """The process uid, or None where the system has none (native Windows)."""
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


def _passwd():
    """This uid's passwd entry, or None (no pwd module, no uid, or no entry)."""
    uid = current_uid()
    if uid is None:
        return None
    try:
        import pwd
    except ImportError:
        return None
    try:
        return pwd.getpwuid(uid)
    except KeyError:
        return None


def login_shell() -> str:
    """The account's login shell from passwd, then $SHELL, then /bin/sh. Linux: passwd answers (as Settings did before)."""
    pw = _passwd()
    if pw is not None:
        return pw.pw_shell or "/bin/sh"
    return os.environ.get("SHELL") or "/bin/sh"


def passwd_home() -> Path | None:
    """The account's home from passwd (not $HOME), or None when it cannot be read."""
    pw = _passwd()
    return Path(pw.pw_dir) if pw is not None and pw.pw_dir else None


def owned_by_me(path) -> bool | None:
    """Is `path` owned by this process's uid? None without a uid (the owner check is then skipped, never answered "not yours")."""
    uid = current_uid()
    if uid is None:
        return None
    return os.stat(path).st_uid == uid


# ---------------------------------------------------------------- locks


def lock_nb(f) -> None:
    """Take an exclusive, non-blocking lock on the open file `f`; raises OSError (BlockingIOError on POSIX) when another holder has it.
    POSIX: fcntl.flock(LOCK_EX | LOCK_NB), exactly as before. Windows: msvcrt.locking on the first byte."""
    if IS_WINDOWS:
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)


# ---------------------------------------------------------------- files


class WriteRaced(Exception):
    """secure_write's `still` check said the destination changed under us; nothing was replaced."""


def atomic_replace(src, dst) -> None:
    """os.replace; on Windows only, three short retries when the target is held open (PermissionError). POSIX: one plain os.replace."""
    if not IS_WINDOWS:
        os.replace(src, dst)
        return
    for attempt in range(3):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.05 * (attempt + 1))


def secure_write(dest, data: bytes, mode: int = 0o600, *, still=None) -> None:
    """Replace `dest` with `data`: a temp file in the same directory created 0600 then set to `mode`, fsync, atomic_replace. `still`, when
    given, is asked just before the replace and must say the destination is as it was (else WriteRaced and nothing is replaced).
    POSIX: the same bytes, modes and temp-file name pattern as app/account_store.py's former _write_atomic."""
    dest = Path(dest)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if fchmod is None:
            with contextlib.suppress(OSError):
                os.chmod(tmp, mode)
        if still is not None and not still():
            raise WriteRaced()
        atomic_replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def spawn_detached(argv: list[str], log_file, **kw) -> subprocess.Popen:
    """Start `argv` detached from the board: stdin from /dev/null, stdout and stderr to the open `log_file`. POSIX: its own session
    (start_new_session=True, as before). Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP. Extra keyword args go to Popen."""
    if IS_WINDOWS:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT, creationflags=flags, **kw)
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True, **kw)


def o_nofollow() -> int:
    """os.O_NOFOLLOW, or 0 where the system lacks it (Windows); a caller given 0 must lstat-check for a link after the open."""
    return getattr(os, "O_NOFOLLOW", 0)


def browser_stub() -> str:
    """A BROWSER value that opens nothing: the `true` on PATH, else the bare word `true` (Linux: /bin/true is what `which` finds
    on the box; the literal stays "/bin/true" when it exists so the login env is byte for byte as before)."""
    if os.path.exists("/bin/true"):
        return "/bin/true"
    return shutil.which("true") or "true"


# ---------------------------------------------------------------- defaults


def default_projects_dir() -> Path:
    """/srv/projects on Linux (as before); ~/projects elsewhere (/srv cannot be made on a sealed macOS root volume)."""
    if IS_LINUX:
        return Path("/srv/projects")
    return Path.home() / "projects"


def default_data_dir() -> Path:
    """~/.local/share/ccboard on every system on purpose: the hook scripts in bin/ default to it, and a path with a space would
    break the hook commands (issue #121)."""
    return Path.home() / ".local" / "share" / "ccboard"


def under_drvfs(path) -> bool:
    """WSL with `path` below /mnt/ (a Windows drive: no real uid, weak inode stamps, slow). Callers warn, never refuse."""
    if not is_wsl():
        return False
    try:
        p = Path(path).resolve()
    except OSError:
        p = Path(path)
    return p.parts[:2] == ("/", "mnt")


@functools.lru_cache(maxsize=64)
def fs_case_insensitive(directory: str) -> bool:
    """Does the file system holding `directory` fold case (APFS's default, NTFS)? A temp-file probe, cached per directory.
    False when the probe cannot run (the conservative answer for name checks is "they differ")."""
    try:
        fd, name = tempfile.mkstemp(prefix=".ccboard-case-", dir=directory)
    except OSError:
        return False
    try:
        os.close(fd)
        base = os.path.basename(name)
        other = os.path.join(directory, base.swapcase())
        return other != name and os.path.exists(other)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(name)


# ---------------------------------------------------------------- runtime

RUNTIMES = ("docker", "systemd", "launchd", "host")


def resolve_runtime(env) -> tuple[str, bool]:
    """(runtime, detected) from the environment. An explicit CCBOARD_RUNTIME in RUNTIMES wins (detected False); an unknown value is
    logged and ignored; otherwise a service marker decides: INVOCATION_ID (systemd always sets it) gives 'systemd', XPC_SERVICE_NAME
    naming a ccboard launchd label gives 'launchd' (detected True), else 'host'."""
    runtime = (env.get("CCBOARD_RUNTIME") or "").strip().lower()
    if runtime in RUNTIMES:
        return runtime, False
    if runtime:
        log.warning("ignoring unknown CCBOARD_RUNTIME=%r", runtime)
    if env.get("INVOCATION_ID"):
        return "systemd", True
    if (env.get("XPC_SERVICE_NAME") or "").startswith("dev.ccboard"):
        return "launchd", True
    return "host", False


def dev_bypass_allowed(env, runtime: str) -> bool:
    """The dev bypass (CCBOARD_DEV_BYPASS_USER) is honoured only for the 'host' runtime with no service marker in the environment:
    never under docker, systemd or launchd, never when INVOCATION_ID or a launchd XPC_SERVICE_NAME is present. Linux: identical to the
    former rule (runtime == 'host' and not INVOCATION_ID)."""
    if runtime != "host" or env.get("INVOCATION_ID"):
        return False
    return not (env.get("XPC_SERVICE_NAME") or "").startswith("dev.ccboard")


# ---------------------------------------------------------------- processes (implemented in the Phase H workflow, see #116 slice 5)

PROC_ROOT = Path("/proc")     # the one test seam for every /proc reader


def ppid(pid: int) -> int | None:
    """Parent pid. Linux: /proc/<pid>/stat ('pid (comm) S ppid ...'; comm may hold spaces and parentheses). Else psutil. None if gone."""
    raise NotImplementedError


def ancestors(pid: int, hops: int) -> list[int]:
    """The parent chain of `pid`, nearest first, at most `hops` long, stopping at pid 1 or an unreadable parent."""
    raise NotImplementedError


def children(pid: int) -> list[int]:
    """Direct children. Linux: /proc/<pid>/task/*/children. Else psutil. [] when unknown."""
    raise NotImplementedError


def comm(pid: int) -> str | None:
    """The process name (Linux: /proc/<pid>/comm stripped). Else psutil name(). None if gone or unreadable."""
    raise NotImplementedError


def iter_processes() -> list[int] | None:
    """Every visible pid, or None when the process list cannot be read (callers must then say "unknown", never "none")."""
    raise NotImplementedError


def environ_names(pid: int) -> list[str] | None:
    """The NAMES (never the values) of process `pid`'s environment, or None when it cannot be read."""
    raise NotImplementedError


def listening_ports(pids) -> dict[int, set[int]]:
    """{pid: {tcp ports in LISTEN}} for the given pids. Linux: `ss -ltnpH` parsed as before. Else per-process psutil net_connections,
    then `lsof -nP -iTCP -sTCP:LISTEN` when psutil is missing; {} when neither works. Never raises."""
    raise NotImplementedError


# ---------------------------------------------------------------- host numbers (#116 slice 6)


def cpu_pct(consumer: str) -> float | None:
    """CPU busy % since this consumer's previous reading (None on its first reading or when it cannot be read)."""
    raise NotImplementedError


def mem() -> dict | None:
    """The memory figures in exactly the shape app/health.py returns today, or None."""
    raise NotImplementedError


def uptime() -> float | None:
    """Seconds since boot, or None."""
    raise NotImplementedError


# ---------------------------------------------------------------- hints (#116 slice 7)


def service_manager() -> str:
    """'docker' | 'systemd' | 'launchd' | 'none' for the running board."""
    raise NotImplementedError


def hint(kind: str, name: str) -> str:
    """The sentence for `kind` in ('start', 'restart', 'logs', 'install') of `name` on this system. Linux strings are the literals the
    call sites used before, byte for byte (apt-get on Debian/Ubuntu, systemctl, journalctl); brew and launchctl on macOS; a plain
    "install <name>" elsewhere."""
    raise NotImplementedError
