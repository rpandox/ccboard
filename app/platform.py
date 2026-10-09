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
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
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


def whoami() -> str:
    """The name of the user the process runs as (effective uid): the passwd name, else "uid N", else "unknown" where there is no uid."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return "unknown"
    try:
        import pwd
        return pwd.getpwuid(geteuid()).pw_name
    except (ImportError, KeyError, OSError):
        return f"uid {geteuid()}"


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


# ---------------------------------------------------------------- WSL2 (issue #118)
# Everything the board asks about being inside WSL lives here; every function answers "not WSL" (None, False) on a system that is not WSL, and
# none of them changes anything. The Windows side is reached only by the doctor, through interop (app/doctor_wsl.py).

PROC_MOUNTS = Path("/proc/mounts")                       # test seam: the mount table
WSL_INTEROP_FILES = (Path("/proc/sys/fs/binfmt_misc/WSLInterop"), Path("/proc/sys/fs/binfmt_misc/WSLInterop-late"))   # exists while Windows interop is on
WSLINFO_TIMEOUT = 3                                      # seconds for `wslinfo`
WSL_NETWORKING_MODES = ("nat", "mirrored", "bridged", "virtioproxy", "none")
WINDOWS_DRIVE_FSTYPES = ("9p", "drvfs", "virtiofs")      # how a Windows drive shows in the mount table (virtiofs: to verify)
TAILSCALE_PLACEMENTS = ("wsl", "host")                   # CCBOARD_TAILSCALE_PLACEMENT: Tailscale inside the distro, or on the Windows host


def wsl_networking_mode() -> str | None:
    """None off WSL. On WSL the word `wslinfo --networking-mode` prints ('nat', 'mirrored', ...), lower case; 'unknown' when wslinfo is not
    there (older WSL: whether it exists on the installed version is to verify), fails, times out or prints something else. Not cached."""
    if not is_wsl():
        return None
    exe = shutil.which("wslinfo")
    if not exe:
        return "unknown"
    try:
        cp = subprocess.run([exe, "--networking-mode"], capture_output=True, text=True, timeout=WSLINFO_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    word = (cp.stdout or "").strip().lower().split()[:1]
    return word[0] if cp.returncode == 0 and word and word[0] in WSL_NETWORKING_MODES else "unknown"


def wsl_interop() -> bool:
    """WSL with Windows interop on (a Windows program such as powershell.exe can be started from here): the WSLInterop entry exists in binfmt_misc."""
    return is_wsl() and any(p.exists() for p in WSL_INTEROP_FILES)


def windows_mount(path) -> str | None:
    """The mount point of the Windows drive that holds `path`, or None: WSL only; the longest mount point in the mount table that is a drvfs-style
    file system (9p, drvfs, virtiofs) whose source names a drive (`C:\\`, shown as C:\\134 in /proc/mounts) or whose options say drvfs. A drive mounted
    outside /mnt/ is found this way; under_drvfs() is the quick look at the path alone."""
    if not is_wsl():
        return None
    try:
        p = str(Path(path).resolve())
        rows = PROC_MOUNTS.read_text(errors="replace").splitlines()
    except OSError:
        return None
    best = None
    for row in rows:
        f = row.split()
        if len(f) < 4 or f[2] not in WINDOWS_DRIVE_FSTYPES:
            continue
        src, mp, opts = f[0], f[1].replace("\\040", " "), f[3]
        if not (re.match(r"^[A-Za-z]:(\\|$)", src.replace("\\134", "\\")) or "aname=drvfs" in opts or "drvfs" in src):
            continue
        if (p == mp or p.startswith(mp.rstrip("/") + "/")) and (best is None or len(mp) > len(best)):
            best = mp
    return best


def on_windows_drive(path) -> bool:
    """WSL and `path` is below /mnt/ or on a Windows drive found in the mount table (callers warn, never refuse)."""
    return under_drvfs(path) or windows_mount(path) is not None


def init_identity() -> tuple[str, float] | None:
    """(id, started) of PID 1 on Linux, else None. `id` is the first 8 hex digits of the kernel boot id and PID 1's start tick ('1a2b3c4d:1234'):
    it changes when the virtual machine restarts (new boot id) and when the distro restarts (new init). Neither needs the wall clock, which a WSL2
    VM can get wrong after Windows sleeps. `started` is the estimated epoch second PID 1 started (now - uptime + its start tick)."""
    if not IS_LINUX:
        return None
    try:
        boot = (PROC_ROOT / "sys" / "kernel" / "random" / "boot_id").read_text().strip().replace("-", "")[:8]
        ticks = int((PROC_ROOT / "1" / "stat").read_text(errors="replace").rsplit(")", 1)[1].split()[19])
        hz = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None
    up = uptime()
    if not boot or not hz or up is None:
        return None
    return f"{boot}:{ticks}", time.time() - up + ticks / hz


def tailscale_placement(env=None, *, implied: bool = True) -> str | None:
    """WSL only (else None): where Tailscale runs, 'wsl' (inside the distro: the installer's normal path) or 'host' (on Windows; serve is run there).
    CCBOARD_TAILSCALE_PLACEMENT decides (install.sh remembers it); unset, it is 'wsl' when the distro has a `tailscale` command, else 'host' (the same
    default the installer picks); `implied=False` returns None for an unset value instead. A value that is neither is ignored."""
    if not is_wsl():
        return None
    value = ((env if env is not None else os.environ).get("CCBOARD_TAILSCALE_PLACEMENT") or "").strip().lower()
    if value in TAILSCALE_PLACEMENTS:
        return value
    if not implied:
        return None
    return "wsl" if shutil.which("tailscale") else "host"


@functools.lru_cache(maxsize=64)
def fs_case_insensitive(directory: str) -> bool:
    """Does the file system holding `directory` fold case (APFS's default, NTFS)? A temp-file probe, cached per directory.
    False when the probe cannot run (the conservative answer for name checks is "they differ"), and on Linux outside a Windows drive
    without probing (ext4 and the box's volumes are case-sensitive, so nothing is written there)."""
    if IS_LINUX and not under_drvfs(directory):
        return False
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


# ---------------------------------------------------------------- processes (#116 slice 5)

PROC_ROOT = Path("/proc")     # the one test seam for every /proc reader
SS_RE = re.compile(r"pid=(\d+)")
LISTEN_TIMEOUT = 5            # seconds for `ss` and for `lsof`


def _psutil():
    """The psutil module, imported on first use (it is only installed off Linux), or None when it is missing."""
    try:
        import psutil
    except ImportError:
        return None
    return psutil


def ppid(pid: int) -> int | None:
    """Parent pid. Linux: /proc/<pid>/stat ('pid (comm) S ppid ...'; comm may hold spaces and parentheses). Else psutil. None if gone."""
    if IS_LINUX:
        try:
            raw = (PROC_ROOT / str(pid) / "stat").read_text(errors="replace")
            return int(raw.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None
    ps = _psutil()
    if ps is None:
        return None
    try:
        return int(ps.Process(pid).ppid())
    except Exception:                         # NoSuchProcess, AccessDenied, ZombieProcess, a pid out of range: gone or unreadable
        return None


def ancestors(pid: int, hops: int) -> list[int]:
    """The parent chain of `pid`, nearest first, at most `hops` long, stopping at pid 1 or an unreadable parent."""
    chain: list[int] = []
    cur = pid
    seen = {pid}
    for _ in range(hops):
        nxt = ppid(cur)
        if not nxt or nxt <= 1 or nxt in seen:
            break
        chain.append(nxt)
        seen.add(nxt)
        cur = nxt
    return chain


def children(pid: int) -> list[int]:
    """Direct children. Linux: /proc/<pid>/task/*/children. Else psutil. [] when unknown."""
    if IS_LINUX:
        out: list[int] = []
        task_dir = PROC_ROOT / str(pid) / "task"
        try:
            for t in task_dir.iterdir():
                try:
                    out += [int(x) for x in (t / "children").read_text().split()]
                except (OSError, ValueError):
                    continue
        except OSError:
            pass
        return out
    ps = _psutil()
    if ps is None:
        return []
    try:
        return [int(c.pid) for c in ps.Process(pid).children(recursive=False)]
    except Exception:
        return []


def comm(pid: int) -> str | None:
    """The process name (Linux: /proc/<pid>/comm stripped). Else psutil name(). None if gone or unreadable."""
    if IS_LINUX:
        try:
            return (PROC_ROOT / str(pid) / "comm").read_text(errors="replace").strip()
        except OSError:
            return None
    ps = _psutil()
    if ps is None:
        return None
    try:
        return str(ps.Process(pid).name()).strip()
    except Exception:
        return None


def iter_processes() -> list[int] | None:
    """Every visible pid, or None when the process list cannot be read (callers must then say "unknown", never "none")."""
    if IS_LINUX:
        try:
            return [int(n) for n in os.listdir(PROC_ROOT) if n.isdigit()]
        except OSError:
            return None
    ps = _psutil()
    if ps is None:
        return None
    try:
        return [int(p) for p in ps.pids()]
    except Exception:
        return None


def environ_names(pid: int) -> list[str] | None:
    """The NAMES (never the values) of process `pid`'s environment, or None when it cannot be read. Linux: /proc/<pid>/environ, the first
    MiB; an empty file (a zombie, a kernel thread) reads as unreadable. Else psutil Process.environ() keys; macOS lets a process read
    its own user's processes only."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if IS_LINUX:
        try:
            with open(PROC_ROOT / str(pid) / "environ", "rb") as f:
                raw = f.read(1 << 20)
        except OSError:
            return None
        if not raw:
            return None
        return sorted({chunk.split(b"=", 1)[0].decode("ascii", "replace") for chunk in raw.split(b"\0") if chunk} - {""})
    ps = _psutil()
    if ps is None:
        return None
    try:
        env = ps.Process(pid).environ()
        names = sorted(str(k) for k in env)
    except Exception:                         # AccessDenied (another user's process), NoSuchProcess, an unsupported system
        return None
    return names or None


def _ss_ports() -> dict[int, set[int]]:
    exe = shutil.which("ss")
    if not exe:
        return {}
    try:
        cp = subprocess.run([exe, "-ltnpH"], capture_output=True, text=True, timeout=LISTEN_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return {}
    out: dict[int, set[int]] = {}
    for line in cp.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        addr = parts[3]
        try:
            port = int(addr.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        for m in SS_RE.finditer(line):
            out.setdefault(int(m.group(1)), set()).add(port)
    return out


def _psutil_ports(ps, pids) -> dict[int, set[int]]:
    """Per process, never the system-wide psutil.net_connections() (it needs root on macOS): each pid's own TCP sockets in LISTEN."""
    if pids is None:
        pids = iter_processes() or []
    listen = getattr(ps, "CONN_LISTEN", "LISTEN")
    out: dict[int, set[int]] = {}
    for pid in pids:
        try:
            conns = ps.Process(pid).net_connections(kind="tcp")
        except Exception:                     # gone, or another user's process
            continue
        for c in conns:
            try:
                if c.status == listen and c.laddr:
                    out.setdefault(int(pid), set()).add(int(c.laddr.port))
            except (AttributeError, TypeError, ValueError):
                continue
    return out


def _lsof_ports() -> dict[int, set[int]]:
    exe = shutil.which("lsof")
    if not exe:
        return {}
    try:
        cp = subprocess.run([exe, "-nP", "-iTCP", "-sTCP:LISTEN"], capture_output=True, text=True, timeout=LISTEN_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return {}
    out: dict[int, set[int]] = {}
    for line in cp.stdout.splitlines():
        parts = line.split()                  # COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME (LISTEN)
        if len(parts) < 10 or not parts[1].isdigit() or parts[-1] != "(LISTEN)":
            continue
        try:
            port = int(parts[-2].rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        out.setdefault(int(parts[1]), set()).add(port)
    return out


def listening_ports(pids) -> dict[int, set[int]]:
    """{pid: {tcp ports in LISTEN}} for the given pids (None: every process that can be seen). Linux: `ss -ltnpH` parsed as before. Else
    per-process psutil net_connections, then `lsof -nP -iTCP -sTCP:LISTEN` when psutil is missing; {} when neither works. Never raises."""
    want = None if pids is None else {int(p) for p in pids if isinstance(p, int) and not isinstance(p, bool)}
    try:
        if IS_LINUX:
            found = _ss_ports()
        else:
            ps = _psutil()
            found = _psutil_ports(ps, None if want is None else sorted(want)) if ps is not None else _lsof_ports()
    except Exception:
        return {}
    if want is None:
        return found
    return {pid: ports for pid, ports in found.items() if pid in want}


# ---------------------------------------------------------------- host numbers (#116 slice 6)

_cpu_prev: dict[str, tuple[int, int]] = {}      # consumer -> the previous (idle, total) reading
_cpu_lock = threading.Lock()


def _cpu_reading() -> tuple[int, int] | None:
    """(idle, total) cpu time now: /proc/stat's first line on Linux, psutil.cpu_times() elsewhere; None when unreadable."""
    if IS_LINUX:
        try:
            with open(PROC_ROOT / "stat") as f:
                fields = f.readline().split()[1:]
            vals = [int(x) for x in fields]
            return vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals)
        except (OSError, ValueError, IndexError):
            return None
    ps = _psutil()
    if ps is None:
        return None
    try:
        t = ps.cpu_times()
        idle = float(getattr(t, "idle", 0)) + float(getattr(t, "iowait", 0))
        total = sum(float(getattr(t, f, 0)) for f in getattr(t, "_fields", ()) if f not in ("guest", "guest_nice"))
        return int(idle * 100), int(total * 100)
    except Exception:
        return None


def cpu_pct(consumer: str = "default") -> float | None:
    """CPU busy % since this consumer's previous reading (None on its first reading or when it cannot be read). Every consumer
    keeps its own previous reading, so the Sampler (60 s) and the /api/state poll (3 s) never steal each other's delta. Elsewhere
    psutil.cpu_times() feeds the same per-consumer arithmetic (psutil.cpu_percent(interval=None) keeps ONE shared baseline)."""
    reading = _cpu_reading()
    if reading is None:
        return None
    idle, total = reading
    with _cpu_lock:
        prev = _cpu_prev.get(consumer)
        _cpu_prev[consumer] = (idle, total)
    if not prev or total == prev[1]:
        return None
    return round(100.0 * (1 - (idle - prev[0]) / (total - prev[1])), 1)


def mem() -> dict | None:
    """The memory figures in exactly the shape app/health.py returns today ({total, used, pct}, bytes), or None."""
    if IS_LINUX:
        try:
            info = {}
            with open(PROC_ROOT / "meminfo") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    info[k] = int(v.strip().split()[0]) * 1024
            total, avail = info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
            return {"total": total, "used": total - avail, "pct": round(100.0 * (total - avail) / total, 1)}
        except (OSError, KeyError, ValueError):
            return None
    ps = _psutil()
    if ps is None:
        return None
    try:
        vm = ps.virtual_memory()
        total, avail = int(vm.total), int(vm.available)
        return {"total": total, "used": total - avail, "pct": round(100.0 * (total - avail) / total, 1)}
    except Exception:
        return None


def uptime() -> float | None:
    """Seconds since boot, or None."""
    if IS_LINUX:
        try:
            with open(PROC_ROOT / "uptime") as f:
                return float(f.read().split()[0])
        except (OSError, ValueError):
            return None
    ps = _psutil()
    if ps is None:
        return None
    try:
        return max(0.0, time.time() - float(ps.boot_time()))
    except Exception:
        return None


# ---------------------------------------------------------------- binaries (#117)

BREW_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")     # Apple silicon and Intel Homebrew prefixes
LOGIN_SHELL_TIMEOUT = 3.0                                    # seconds the login-shell lookup may take (an rc file can be slow)
LOGIN_LOOKUP_HIT_TTL = 600.0                                 # a found path is trusted this long (and re-checked for being executable on every use)
LOGIN_LOOKUP_MISS_TTL = 300.0                                # a miss is remembered this long, so a missing tool costs one shell start per interval
_BIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_login_lookups: dict[str, tuple[float, str | None]] = {}
_login_lookups_lock = threading.Lock()


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _login_shell_lookup(name: str) -> str | None:
    """`$SHELL -lic 'command -v <name>'` through the account's login shell (login_shell()), with LOGIN_SHELL_TIMEOUT: what a terminal would find
    after the user's profile ran (nvm, asdf, Homebrew's shellenv). Only an absolute path to an executable file counts (an alias or function
    prints a word, not a path); the last output line is read because a profile may print a banner. None on any failure. Tests patch this."""
    shell = login_shell()
    if not os.path.isabs(shell) or not _executable(shell):
        return None
    try:
        cp = subprocess.run([shell, "-lic", f"command -v {name}"], capture_output=True, text=True, timeout=LOGIN_SHELL_TIMEOUT,
                            stdin=subprocess.DEVNULL, start_new_session=not IS_WINDOWS)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    lines = [ln.strip() for ln in (cp.stdout or "").splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    return last if last.startswith("/") and _executable(last) else None


def login_shell_bin(name: str) -> str | None:
    """The login shell's answer for `name`, cached per name (hits LOGIN_LOOKUP_HIT_TTL, misses LOGIN_LOOKUP_MISS_TTL). A cached path that
    stopped being executable is looked up again."""
    if not _BIN_NAME_RE.match(name or ""):
        return None
    now = time.monotonic()
    with _login_lookups_lock:
        hit = _login_lookups.get(name)
    if hit is not None:
        at, path = hit
        if path is None and now - at < LOGIN_LOOKUP_MISS_TTL:
            return None
        if path is not None and now - at < LOGIN_LOOKUP_HIT_TTL and _executable(path):
            return path
    path = _login_shell_lookup(name)
    with _login_lookups_lock:
        _login_lookups[name] = (now, path)
    return path


def resolve_bin(name: str) -> str | None:
    """Where the board finds the program `name`, in this order: PATH; ~/.local/bin/<name> (these two are the answer Settings.claude_bin() and
    codex_bin() always gave, so a system that finds the program there sees no change and no shell is started); the login shell's lookup
    (login_shell_bin: a LaunchAgent has PATH=/usr/bin:/bin:/usr/sbin:/sbin, so nvm, asdf and Homebrew tools are invisible to it); the
    Homebrew folders (BREW_BIN_DIRS). None when none has it."""
    found = shutil.which(name)
    if found:
        return found
    local = Path.home() / ".local" / "bin" / name
    if local.exists():
        return str(local)
    via_shell = None if IS_LINUX else login_shell_bin(name)     # Linux keeps its old instant answer: systemd and the image set PATH
    if via_shell:
        return via_shell
    if _BIN_NAME_RE.match(name or ""):
        for d in BREW_BIN_DIRS:
            cand = os.path.join(d, name)
            if _executable(cand):
                return cand
    return None


# ---------------------------------------------------------------- hints (#116 slice 7)


OS_RELEASE = Path("/etc/os-release")        # the one test seam for the distribution check


def service_manager() -> str:
    """'docker' | 'systemd' | 'launchd' | 'none' for the running board: its runtime (resolve_runtime), where a hand-started board ('host') has none."""
    runtime = resolve_runtime(os.environ)[0]
    return runtime if runtime in ("docker", "systemd", "launchd") else "none"


def _apt_system() -> bool:
    """Debian family, where `sudo apt-get install` is the right hint. /etc/os-release naming another family says no; an unreadable file
    says yes (the box and its image are Ubuntu, so the hints stay as they were)."""
    try:
        text = OS_RELEASE.read_text(errors="replace")
    except OSError:
        return True
    ids: set[str] = set()
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key in ("ID", "ID_LIKE"):
            ids.update(value.strip().strip("\"'").lower().split())
    return not ids or bool(ids & {"debian", "ubuntu"})


LAUNCHD_PREFIX = "dev.ccboard"                                  # every launchd job's label is <prefix>.<job> (issue #117)
LAUNCHD_JOBS = ("board", "tmux", "ttyd", "awake", "code-server", "mem", "backup")
MACOS_LOG_DIR = "~/Library/Logs/ccboard"                         # <job>.log, stdout and stderr of each job (expanded by the installer)


def launchd_label(job: str) -> str:
    """dev.ccboard.<job> for one of LAUNCHD_JOBS."""
    return f"{LAUNCHD_PREFIX}.{job}"


def launchd_job(unit: str) -> str | None:
    """The launchd job that stands in for a systemd unit name on a Mac: ccboard -> board, ccboard-<x> -> <x>, code-server -> code-server;
    None for anything else (a Homebrew service)."""
    if unit == "ccboard":
        return "board"
    if unit.startswith("ccboard-") and unit.removeprefix("ccboard-") in LAUNCHD_JOBS:
        return unit.removeprefix("ccboard-")
    if unit == "code-server":
        return "code-server"
    return None


def launchd_domain(env=None) -> str:
    """'gui' (the default: LaunchAgents of the logged-in user) or 'user' (CCBOARD_LAUNCHD_DOMAIN=user: the experimental Background-session layout)."""
    raw = ((os.environ if env is None else env).get("CCBOARD_LAUNCHD_DOMAIN") or "").strip().lower()
    return "user" if raw == "user" else "gui"


def launchd_target(job: str, env=None) -> str | None:
    """<domain>/<uid>/dev.ccboard.<job>, the argument of launchctl print and kickstart; None where there is no uid."""
    uid = current_uid()
    return None if uid is None else f"{launchd_domain(env)}/{uid}/{launchd_label(job)}"


def launchd_loaded(job: str, env=None) -> bool | None:
    """Is dev.ccboard.<job> loaded in its domain (`launchctl print <target>` exits 0)? None where that cannot be asked: no launchctl, no uid, or no
    answer in 10 seconds. Read only; a loaded job that is not running (a calendar job between runs) is loaded."""
    target = launchd_target(job, env)
    if target is None or shutil.which("launchctl") is None:
        return None
    try:
        r = subprocess.run(["launchctl", "print", target], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.returncode == 0


def macos_log_dir() -> Path:
    """The folder of the launchd jobs' logs (MACOS_LOG_DIR with ~ expanded, from $HOME)."""
    return Path(os.path.expanduser(MACOS_LOG_DIR))


def macos_protected_folders() -> list[tuple[str, Path]]:
    """(name, path) of the home folders macOS guards with a Files and Folders grant: Desktop, Documents, Downloads and iCloud Drive."""
    home = Path.home()
    return [("Desktop", home / "Desktop"), ("Documents", home / "Documents"), ("Downloads", home / "Downloads"),
            ("iCloud Drive", home / "Library" / "Mobile Documents")]


def _hint_family() -> str:
    """'linux' | 'macos' | 'other': whose tools the hints name (a seam: tests that assert the Linux words on any host patch this)."""
    return "linux" if IS_LINUX else "macos" if IS_MACOS else "other"


_HINT_KINDS = ("start", "restart", "status", "timers", "logs", "install", "upgrade", "reinstall")


def hint(kind: str, name: str, *, sudo: bool = True, flags: str = "") -> str:
    """The sentence for `kind` of `name` on this system. Kinds: start, restart, status, timers, logs (a service) and install, upgrade,
    reinstall (a package). `sudo=False` drops the sudo of the systemd forms; `flags` is appended to the systemctl and journalctl forms.
    Linux strings are the literals the call sites used before, byte for byte (apt-get on Debian/Ubuntu, systemctl, journalctl); macOS
    names brew (packages, and brew services for ntfy and code-server) and launchctl (the ccboard-* agents, label dev.ccboard.<name>);
    any other system gets a plain "install <name>". A ValueError for an unknown kind."""
    if kind not in _HINT_KINDS:
        raise ValueError(f"unknown hint kind {kind!r}")
    extra = f" {flags}" if flags else ""
    family = _hint_family()
    if family == "linux":
        pre = "sudo " if sudo else ""
        if kind in ("install", "upgrade", "reinstall"):
            if not _apt_system():
                return f"{kind} {name}"
            opt = {"install": "", "upgrade": "--only-upgrade ", "reinstall": "--reinstall "}[kind]
            return f"sudo apt-get install -y {opt}{name}"
        if kind == "timers":
            return f"systemctl list-timers {name} --no-pager"
        if kind == "logs":
            return f"{pre}journalctl -u {name}{extra}"
        return f"{pre}systemctl {kind} {name}{extra}"
    if family == "macos":
        pkg = name.split("@")[0]
        if kind in ("install", "upgrade", "reinstall"):
            return f"brew {kind} {pkg}"
        unit = pkg.removesuffix(".timer").removesuffix(".service")
        agent = launchd_job(unit)
        if agent is not None:
            label = f"gui/$(id -u)/{launchd_label(agent)}"
            if kind == "start":
                return f"launchctl kickstart {label}"
            if kind == "restart":
                return f"launchctl kickstart -k {label}"
            if kind == "logs":
                return f"tail -n 30 ~/Library/Logs/ccboard/{agent}.log"
            return f"launchctl print {label}"
        if kind == "logs":
            return f"tail -n 30 $(brew --prefix)/var/log/{unit}.log"          # to verify per formula
        return f"brew services {'info' if kind in ('status', 'timers') else kind} {unit}"
    return f"{kind} {name}"
