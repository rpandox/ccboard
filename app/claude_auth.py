"""Claude login state and the web-driven `claude auth login` flow (runs in tmux)."""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time

from . import tmux
from .config import settings

CODE_RE = re.compile(r"^[A-Za-z0-9._~-]{1,256}#[A-Za-z0-9._~-]{1,256}$")
URL_RE = re.compile(r"https://\S*oauth\S+")
OSC8_RE = re.compile(r"\x1b\]8;;(https://[^\x1b\x07]+)")
STATUS_TTL = 60.0
SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh"}

_lock = threading.Lock()
_status_cache: tuple[tuple, float, dict] | None = None  # (key, time, value)
_version_cache: str | None = None


def _creds_key() -> tuple:
    p = settings.claude_config_dir / ".credentials.json"
    try:
        st = p.stat()
        return (True, st.st_mtime_ns, st.st_size)
    except OSError:
        return (False, 0, 0)


def invalidate() -> None:
    global _status_cache
    with _lock:
        _status_cache = None


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


def start_login() -> None:
    """(Re)start `claude auth login` in the internal tmux session."""
    exe = settings.claude_bin()
    if not exe:
        raise tmux.TmuxError("claude is not installed")
    tmux.kill_session(tmux.LOGIN_SESSION)
    tmux.new_session(tmux.LOGIN_SESSION, str(settings.claude_config_dir.parent),
                     env={"BROWSER": "/bin/true"}, width=400, height=50)
    tmux.send_line(tmux.LOGIN_SESSION, "claude auth login")
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
