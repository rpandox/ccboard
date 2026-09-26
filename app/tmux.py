"""tmux client wrappers. The board never starts a server (-N); ccboard-tmux.service owns it."""
from __future__ import annotations

import re
import subprocess
import time

from .config import settings

NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?$")
SEP = "--"
INTERNAL_PREFIX = "_ccboard"
LOGIN_SESSION = "_ccboard-login"


class TmuxDown(Exception):
    """The ccboard tmux server is not running."""


class TmuxError(Exception):
    pass


def valid_name(s: str) -> bool:
    return bool(s) and bool(NAME_RE.match(s)) and SEP not in s


def tmux_name(project: str, repo: str, session: str) -> str:
    return f"{project}{SEP}{repo}{SEP}{session}"


def split_name(name: str) -> tuple[str, str, str]:
    parts = name.split(SEP)
    if len(parts) != 3 or not all(valid_name(p) for p in parts):
        raise ValueError(f"not a ccboard session name: {name!r}")
    return parts[0], parts[1], parts[2]


def is_internal(name: str) -> bool:
    return name.startswith(INTERNAL_PREFIX)


def _base() -> list[str]:
    return ["tmux", "-N", "-L", settings.tmux_socket]


def run(*args: str, timeout: float = 5, check: bool = True) -> subprocess.CompletedProcess:
    try:
        cp = subprocess.run(_base() + list(args), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise TmuxError("tmux is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise TmuxError(f"tmux {args[0]} timed out") from e
    if cp.returncode != 0 and check:
        err = (cp.stderr or cp.stdout).strip()
        if "no server running" in err or "error connecting" in err:
            raise TmuxDown(err)
        raise TmuxError(err or f"tmux {args[0]} failed")
    return cp


def server_up() -> bool:
    try:
        run("list-sessions", check=False)
        cp = run("display-message", "-p", "ok", check=False)
        return "no server running" not in (cp.stderr or "") and "error connecting" not in (cp.stderr or "")
    except TmuxError:
        return False


def list_sessions() -> dict[str, dict]:
    """name -> {created, attached, windows, pane_id, command, path, pid}. Raises TmuxDown."""
    cp = run("list-sessions", "-F", "#{session_name}\t#{session_created}\t#{session_attached}\t#{session_windows}",
             check=False)
    if cp.returncode != 0:
        err = (cp.stderr or "").strip()
        if "no server running" in err or "error connecting" in err:
            raise TmuxDown(err)
        return {}  # e.g. "no sessions"
    out: dict[str, dict] = {}
    for line in cp.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        name, created, attached, windows = parts
        out[name] = {"created": int(created or 0), "attached": int(attached or 0),
                     "windows": int(windows or 0), "pane_id": None, "command": None, "path": None, "pid": None}
    if out:
        cp = run("list-panes", "-a",
                 "-F", "#{session_name}\t#{pane_id}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_pid}",
                 check=False)
        for line in cp.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 5:
                continue
            name, pane_id, cmd, path, pid = parts
            s = out.get(name)
            if s is not None and s["pane_id"] is None:  # first (active-most) pane wins
                s.update(pane_id=pane_id, command=cmd, path=path, pid=int(pid or 0))
    return out


def has_session(name: str) -> bool:
    cp = run("has-session", "-t", f"={name}", check=False)
    if cp.returncode != 0:
        err = (cp.stderr or "").strip()
        if "no server running" in err or "error connecting" in err:
            raise TmuxDown(err)
        return False
    return True


def new_session(name: str, cwd: str, env: dict[str, str] | None = None, width: int = 220, height: int = 50) -> str:
    """Create a detached session running the user's login shell. Returns the real name."""
    args = ["new-session", "-d", "-P", "-F", "#{session_name}", "-s", name, "-c", cwd,
            "-x", str(width), "-y", str(height)]
    for k, v in (env or {}).items():
        args += ["-e", f"{k}={v}"]
    cp = run(*args)
    real = cp.stdout.strip() or name
    return real


def pane_target(name: str) -> str:
    """Exact-session target for pane commands (send-keys, capture-pane): '=name:' = that
    session's current window. Plain '=name' is only valid for session commands."""
    return f"={name}:"


def send_line(name: str, text: str, pause: float = 0.0) -> None:
    """Type text literally, then Enter."""
    run("send-keys", "-t", pane_target(name), "-l", "--", text)
    if pause:
        time.sleep(pause)
    run("send-keys", "-t", pane_target(name), "Enter")


def kill_session(name: str) -> bool:
    cp = run("kill-session", "-t", f"={name}", check=False)
    if cp.returncode != 0:
        err = (cp.stderr or "").strip()
        if "no server running" in err or "error connecting" in err:
            raise TmuxDown(err)
        return False
    return True


def capture(name: str, lines: int = 200, join: bool = True, escapes: bool = False) -> str:
    args = ["capture-pane", "-p", "-t", pane_target(name), "-S", f"-{lines}"]
    if join:
        args.append("-J")
    if escapes:
        args.append("-e")
    cp = run(*args, check=False)
    return cp.stdout if cp.returncode == 0 else ""


def kill_prefix(prefix: str, wait: float = 2.0) -> list[str]:
    """Kill every session whose name starts with prefix and wait (up to `wait` s) for their
    processes to go away, so a following rmtree does not race git's own cleanup."""
    killed = []
    try:
        names = list(list_sessions().keys())
    except TmuxDown:
        return killed
    for n in names:
        if n.startswith(prefix) and kill_session(n):
            killed.append(n)
    deadline = time.monotonic() + wait
    while killed and time.monotonic() < deadline:
        try:
            if not any(n in list_sessions() for n in killed):
                break
        except TmuxDown:
            break
        time.sleep(0.1)
    if killed:
        time.sleep(0.2)  # let HUP'd children finish unlinking
    return killed
