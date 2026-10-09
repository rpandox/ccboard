"""Claude login state and the web-driven `claude auth login` flow (runs in tmux).

On macOS Claude Code keeps the live login in the Keychain (service `Claude Code-credentials`), not in `<config dir>/.credentials.json`. The board
never reads that item's secret: keychain_item() runs `security find-generic-password -s "Claude Code-credentials"` WITHOUT -w or -g, which prints
the item's attributes only, and keeps the exit status and the modification date. The date is part of the status cache key, so the login status
refreshes when the login changes (issue #119)."""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import threading
import time

from . import platform, tmux
from .config import settings

CODE_RE = re.compile(r"^[A-Za-z0-9._~-]{1,256}#[A-Za-z0-9._~-]{1,256}$")
URL_RE = re.compile(r"https://\S*oauth\S+")
OSC8_RE = re.compile(r"\x1b\]8;;(https://[^\x1b\x07]+)")
EMAIL_RE = re.compile(r"^[^\s@'\"\\]{1,120}@[^\s@'\"\\]{1,200}$")
STATUS_TTL = 60.0
SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh"}

KEYCHAIN_SERVICE = "Claude Code-credentials"
KEYCHAIN_EVERY = 30.0          # the Keychain item's attributes are asked for at most this often
KEYCHAIN_TIMEOUT = 3.0         # seconds `security` may take (it asks nobody: no -w, no -g)
SECURITY_NOT_FOUND = 44        # `security` exit status for "the specified item could not be found"
MDAT_RE = re.compile(r'"mdat"<timedate>=\S+\s+"(\d{14})Z')

_lock = threading.Lock()
_status_cache: tuple[tuple, float, dict] | None = None  # (key, time, value)
_version_cache: str | None = None
_kc_lock = threading.Lock()
_kc_cache: tuple[float, dict] | None = None             # (time, keychain_item() answer)
_kc_clock = time.monotonic                              # patched by tests


def _security(argv: list[str]) -> tuple[int, str]:
    """(exit status, stdout) of a `security` call; OSError or TimeoutExpired propagate. Tests put a fake `security` first on PATH."""
    exe = shutil.which(argv[0]) or "/usr/bin/security"
    cp = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, timeout=KEYCHAIN_TIMEOUT, stdin=subprocess.DEVNULL)
    return cp.returncode, cp.stdout or ""


def parse_mdat(text: str) -> str | None:
    """The `mdat` (modification date) attribute of a `security find-generic-password` listing as 'YYYYMMDDHHMMSS', None when absent. Pure:
    it looks at that one line only."""
    m = MDAT_RE.search(text or "")
    return m.group(1) if m else None


def keychain_item(run=None) -> dict:
    """{state: 'present' | 'absent' | 'unknown', modified: 'YYYYMMDDHHMMSS' | None} for the Claude Code Keychain item, from an attribute-only
    `security find-generic-password -s "Claude Code-credentials"` (no -w, no -g: the secret is never asked for, never seen, never kept).
    Exit 0 is present, exit 44 absent, anything else (a failed or timed-out call, a missing `security`, a locked keychain) unknown, which is
    NOT absent. `run(argv) -> (rc, stdout)` is the seam (the doctor passes its own; whatever it raises reads as unknown). Off macOS:
    unknown, nothing is run."""
    if not platform.IS_MACOS:
        return {"state": "unknown", "modified": None}
    argv = ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE]
    try:
        rc, out = (run or _security)(argv)
    except Exception:                                   # a probe must never fail its caller: unknown is an honest answer
        return {"state": "unknown", "modified": None}
    if rc == 0:
        return {"state": "present", "modified": parse_mdat(out)}
    if rc == SECURITY_NOT_FOUND:
        return {"state": "absent", "modified": None}
    return {"state": "unknown", "modified": None}


def _keychain_cached() -> dict:
    """keychain_item() at most once per KEYCHAIN_EVERY seconds (invalidate() forgets the answer)."""
    global _kc_cache
    now = _kc_clock()
    with _kc_lock:
        c = _kc_cache
        if c and now - c[0] < KEYCHAIN_EVERY:
            return c[1]
    got = keychain_item()
    with _kc_lock:
        _kc_cache = (now, got)
    return got


def _creds_key() -> tuple:
    """What the cached login status is keyed on: whether `<config dir>/.credentials.json` exists, its mtime and size. On macOS the live login is
    in the Keychain, so the item's state and modification date join the key (asked for at most every 30 s). Linux: the same 3-tuple as ever."""
    p = settings.claude_config_dir / ".credentials.json"
    try:
        st = p.stat()
        key: tuple = (True, st.st_mtime_ns, st.st_size)
    except OSError:
        key = (False, 0, 0)
    if platform.IS_MACOS:
        kc = _keychain_cached()
        key = (*key, kc["state"], kc["modified"])
    return key


def invalidate() -> None:
    global _status_cache, _kc_cache
    with _lock:
        _status_cache = None
    with _kc_lock:
        _kc_cache = None


def version() -> str | None:
    global _version_cache
    if _version_cache is not None:
        return _version_cache or None
    exe = settings.claude_bin()
    if not exe:
        return None
    try:
        cp = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        _version_cache = cp.stdout.strip().split("\n")[0] if cp.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        _version_cache = ""
    return _version_cache or None


def status() -> dict:
    """{installed, version, loggedIn, email, subscriptionType, authMethod, error?}. Cached."""
    global _status_cache
    exe = settings.claude_bin()
    if not exe:
        return {"installed": False, "version": None, "loggedIn": False}
    key = _creds_key()
    with _lock:
        c = _status_cache
        if c and c[0] == key and time.monotonic() - c[1] < STATUS_TTL:
            return c[2]
    out: dict = {"installed": True, "version": version(), "loggedIn": False}
    try:
        cp = subprocess.run([exe, "auth", "status", "--json"], capture_output=True, text=True, timeout=10)
        data = json.loads(cp.stdout or "{}")
        for k in ("loggedIn", "email", "subscriptionType", "authMethod", "orgName"):
            if k in data:
                out[k] = data[k]
        out["loggedIn"] = bool(data.get("loggedIn"))
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        out["error"] = f"claude auth status failed: {e.__class__.__name__}"
    with _lock:
        _status_cache = (key, time.monotonic(), out)
    return out


def clean_email(email) -> str | None:
    """The address `claude auth login --email` may be given, None for none. Anything else raises ValueError. The line is typed into a
    login shell, so beyond EMAIL_RE (no whitespace, quote, backslash or second @) nothing unprintable gets through either: a control
    character typed into the pane would be a key press, not text."""
    if email is None:
        return None
    if not isinstance(email, str):
        raise ValueError("email must be text")
    e = email.strip()
    if not e:
        return None
    if not EMAIL_RE.match(e) or not e.isprintable():
        raise ValueError("that does not look like an email address")
    return e


def start_login(config_dir=None, email: str | None = None) -> None:
    """(Re)start `claude auth login` in the internal tmux session. With no arguments it logs in the board's own config dir. `config_dir`
    runs it with CLAUDE_CONFIG_DIR=<dir> (an empty dir of its own: the live login is not touched, which is how a second account is
    added); `email` is typed as `--email <address>` (shell-quoted: the line goes to a login shell). ValueError for a bad address."""
    email = clean_email(email)
    exe = settings.claude_bin()
    if not exe:
        raise tmux.TmuxError("claude is not installed")
    tmux.kill_session(tmux.LOGIN_SESSION)
    env = {"BROWSER": platform.browser_stub()}
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    tmux.new_session(tmux.LOGIN_SESSION, str(settings.claude_config_dir.parent),
                     env=env, width=400, height=50)
    tmux.send_line(tmux.LOGIN_SESSION, "claude auth login" + (f" --email {shlex.quote(email)}" if email else ""))
    invalidate()


def login_state() -> dict:
    """{running, url, tail}. running = the pane is currently executing claude."""
    try:
        sessions = tmux.list_sessions()
    except tmux.TmuxDown:
        return {"running": False, "url": None, "tail": []}
    s = sessions.get(tmux.LOGIN_SESSION)
    if not s:
        return {"running": False, "url": None, "tail": []}
    # The native claude binary's process name is its version string, so "running" means
    # "the pane is executing something other than the login shell".
    cmd = (s.get("command") or "").lstrip("-")
    running = bool(cmd) and cmd not in SHELLS
    text = tmux.capture(tmux.LOGIN_SESSION)
    urls = URL_RE.findall(text)
    url = urls[-1].rstrip(".,)") if urls else None  # newest login attempt wins
    if not url:
        raw = tmux.capture(tmux.LOGIN_SESSION, escapes=True)
        urls = OSC8_RE.findall(raw)
        url = urls[-1] if urls else None
    tail = [ln.rstrip() for ln in text.splitlines() if ln.strip()][-15:]
    return {"running": running, "url": url, "tail": tail}


def submit_code(code: str) -> None:
    code = (code or "").strip()
    if not CODE_RE.match(code):
        raise ValueError("paste the whole code shown by the browser, including the part after '#'")
    st = login_state()
    if not st["running"]:
        raise LookupError("no login in progress; click Log in first")
    tmux.send_line(tmux.LOGIN_SESSION, code, pause=0.3)
    invalidate()


def logout() -> dict:
    exe = settings.claude_bin()
    if not exe:
        raise tmux.TmuxError("claude is not installed")
    try:
        tmux.kill_session(tmux.LOGIN_SESSION)
    except tmux.TmuxDown:
        pass
    cp = subprocess.run([exe, "auth", "logout"], capture_output=True, text=True, timeout=20)
    invalidate()
    return {"ok": cp.returncode == 0, "output": (cp.stdout + cp.stderr).strip()[-500:]}
