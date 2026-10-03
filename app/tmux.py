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


def run(*args: str, timeout: float = 5, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess:
    try:
        cp = subprocess.run(_base() + list(args), capture_output=True, text=True, timeout=timeout, input=input)
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
    """name -> {created, attached, windows, window_width, window_height, pane_id, command, path, pid}. Raises TmuxDown.

    window_width/height are the size of the session's current window (0 when tmux does not say): one more format field on
    the same call, so the board learns the window size without a subprocess per session."""
    cp = run("list-sessions", "-F",
             "#{session_name}\t#{session_created}\t#{session_attached}\t#{session_windows}\t#{window_width}\t#{window_height}",
             check=False)
    if cp.returncode != 0:
        err = (cp.stderr or "").strip()
        if "no server running" in err or "error connecting" in err:
            raise TmuxDown(err)
        return {}  # e.g. "no sessions"
    out: dict[str, dict] = {}
    for line in cp.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) not in (4, 6):                      # 4: a tmux that does not know the window formats
            continue
        name, created, attached, windows = parts[:4]
        ww, wh = (parts[4], parts[5]) if len(parts) == 6 else ("", "")
        out[name] = {"created": _int(created), "attached": _int(attached), "windows": _int(windows),
                     "window_width": _int(ww), "window_height": _int(wh),
                     "pane_id": None, "command": None, "path": None, "pid": None}
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


def _int(s: str) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


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


KEY_ALLOW = {"Escape", "C-c", "Tab", "Up", "Down", "Left", "Right", "Enter", "BSpace", "Space", "PageUp", "PageDown",
             "Home", "End", "C-u", "C-l", "C-z", "BTab", "C-o", "C-Home", "C-End"}

# Keys that may be sent while the pane is in copy-mode without leaving it first: they scroll or step inside the mode (or, for
# Escape, leave it themselves). Any other key typed into a pane in copy-mode would be read as a copy-mode command, not as input.
COPY_MODE_SAFE_KEYS = {"PageUp", "PageDown", "Up", "Down", "Escape"}


def send_keys(name: str, keys: list[str]) -> None:
    """Send named keys (allowlisted) to the session's active pane. Leaves copy-mode first unless every key is in
    COPY_MODE_SAFE_KEYS (the board's own scroll keys)."""
    bad = [k for k in keys if k not in KEY_ALLOW]
    if bad:
        raise ValueError(f"key not allowed: {bad[0]}")
    if keys:
        if not all(k in COPY_MODE_SAFE_KEYS for k in keys):
            leave_copy_mode(name)
        run("send-keys", "-t", pane_target(name), *keys)


def send_text(name: str, text: str, enter: bool = False) -> None:
    """Type literal text, optionally followed by Enter. Multi-line text goes through bracketed paste (paste_text), so the
    program receives the newlines as text (Claude Code keeps them inside the prompt) instead of an Enter after each line.
    Leaves copy-mode first: text typed into a pane in copy-mode would be read as copy-mode commands."""
    if text and "\n" in text:
        paste_text(name, text, enter=enter)             # leaves copy-mode itself
        return
    leave_copy_mode(name)
    if text:
        run("send-keys", "-t", pane_target(name), "-l", "--", text)
    if enter:
        if text:
            time.sleep(0.15)
        run("send-keys", "-t", pane_target(name), "Enter")


def paste_text(name: str, text: str, enter: bool = True) -> None:
    """Paste multi-line text with bracketed paste (newlines do not submit), then Enter. Leaves copy-mode first."""
    leave_copy_mode(name)
    run("load-buffer", "-b", "ccboard", "-", input=text)
    run("paste-buffer", "-p", "-d", "-b", "ccboard", "-t", pane_target(name))
    if enter:
        time.sleep(0.3)
        run("send-keys", "-t", pane_target(name), "Enter")


# ---------------------------------------------------------------- clients (who is looking at a session)

# Client flags that do not make someone a person at the terminal: a grid tile (attach -f ignore-size), a read-only view
# (attach -r sets read-only AND ignore-size) and a control-mode client. Remote approve is skipped only for a real one.
IGNORED_CLIENT_FLAGS = {"ignore-size", "read-only", "control-mode"}
_CLIENT_FMT = "#{client_session}\t#{client_flags}"


def _parse_clients(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        sess, _, flags = line.partition("\t")
        out.append({"session": sess.strip(), "flags": {f.strip() for f in flags.split(",") if f.strip()}})
    return out


def count_real(clients: list[dict], name: str | None = None) -> int:
    """Clients (optionally of one session) whose flags are disjoint from IGNORED_CLIENT_FLAGS."""
    return sum(1 for c in clients if (name is None or c["session"] == name) and c["session"]
               and not (c["flags"] & IGNORED_CLIENT_FLAGS))


def classify_viewers(clients: list[dict]) -> dict[str, dict]:
    """{session: {full, grid, ro}}. read-only is checked first (attach -r sets read-only AND ignore-size, so it must not count as
    a grid tile); control-mode clients are not viewers at all. Sessions without a client are absent."""
    out: dict[str, dict] = {}
    for c in clients:
        if not c["session"] or "control-mode" in c["flags"]:
            continue
        v = out.setdefault(c["session"], {"full": 0, "grid": 0, "ro": 0})
        v["ro" if "read-only" in c["flags"] else "grid" if "ignore-size" in c["flags"] else "full"] += 1
    return out


def clients(name: str | None = None) -> list[dict]:
    """Attached clients as [{session, flags:set}], optionally only those of one session. Raises TmuxDown / TmuxError."""
    cp = run("list-clients", "-F", _CLIENT_FMT)
    cl = _parse_clients(cp.stdout)
    return [c for c in cl if c["session"] == name] if name is not None else cl


def real_clients(name: str) -> int:
    """How many real (full, writable) clients are attached to the session."""
    return count_real(clients(name), name)


def viewers() -> dict[str, dict]:
    """{session: {full, grid, ro}} for every session that has a client."""
    return classify_viewers(clients())


# ---------------------------------------------------------------- pane state and scrolling

_PANE_FMT = "\t".join("#{%s}" % f for f in ("alternate_on", "pane_in_mode", "scroll_position", "history_size", "pane_width",
                                              "pane_height", "window_width", "window_height", "pane_current_command"))


def pane_info(name: str) -> dict:
    """The session's active pane: {alt, in_mode, scroll_pos, history, cols, rows, win_cols, win_rows, cmd}.

    Tab separated: scroll_position is empty outside copy-mode (reads as 0), so a space split would shift every later field."""
    cp = run("display-message", "-p", "-t", pane_target(name), _PANE_FMT)
    parts = cp.stdout.rstrip("\r\n").split("\t", 8)
    parts += [""] * (9 - len(parts))
    alt, mode, pos, hist, cols, rows, wc, wr, cmd = parts
    return {"alt": _int(alt) == 1, "in_mode": _int(mode) == 1, "scroll_pos": _int(pos), "history": _int(hist),
            "cols": _int(cols), "rows": _int(rows), "win_cols": _int(wc), "win_rows": _int(wr), "cmd": cmd}


def leave_copy_mode(name: str) -> bool:
    """Cancel tmux copy-mode on the session's pane when it is in it. Best effort: if the pane cannot be read, the caller's own
    command reports the real error. Returns True when it cancelled."""
    try:
        if not pane_info(name)["in_mode"]:
            return False
        run("send-keys", "-t", pane_target(name), "-X", "cancel")
    except TmuxError:
        return False
    return True


SCROLL_DIRS = ("up", "down", "top", "bottom", "exit")
SCROLL_MAX = 10


def _copy_cmd(target: str, cmd: str, n: int = 1) -> None:
    """`send-keys -X cmd` (n times as one `-N n` call: copy-mode started with -e leaves itself when a page-down reaches the
    bottom, and tmux stops the repeat there; n separate calls would hit 'not in a mode'). A pane that left copy-mode between
    our read and this call is the goal reached, not an error."""
    args = ["send-keys", "-t", target] + (["-N", str(n)] if n > 1 else []) + ["-X", cmd]
    try:
        run(*args)
    except TmuxError as e:
        if "not in a mode" not in str(e):
            raise


def scroll(name: str, dir: str, n: int = 1, agent: str = "claude") -> dict:
    """Scroll the session's pane. Returns {mode: 'copy'|'app'|'normal', alt, pos} read after the action.

    A pane on the alternate screen that is not in copy-mode (Claude Code's fullscreen TUI) owns its scrollback: the board sends
    it PageUp/PageDown (n keys) and C-Home/C-End for top/bottom; 'exit' does nothing. Every other pane (a shell, Codex with
    --no-alt-screen, or any pane already in tmux copy-mode) scrolls tmux's own history: up enters copy-mode with
    `copy-mode -e -u` (that is the first page; n-1 more page-ups follow) or sends `-X page-up` n times when already in it;
    down sends `-X page-down` n times (nothing when not in copy-mode; copy-mode -e leaves itself at the bottom); top enters copy-mode if needed then `-X history-top`;
    bottom and exit send `-X cancel` (nothing when not in copy-mode). `agent` is reserved: Codex uses the Claude keys until
    its transcript scrolling is verified on the box."""
    if dir not in SCROLL_DIRS:
        raise ValueError(f"bad scroll direction: {dir!r}")
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= SCROLL_MAX:
        raise ValueError(f"n must be an integer from 1 to {SCROLL_MAX}")
    t = pane_target(name)
    info = pane_info(name)
    acted = True
    if info["alt"] and not info["in_mode"]:
        if dir in ("up", "down"):
            run("send-keys", "-t", t, *(["PageUp" if dir == "up" else "PageDown"] * n))
        elif dir in ("top", "bottom"):
            run("send-keys", "-t", t, "C-Home" if dir == "top" else "C-End")
        else:
            acted = False
    else:
        in_mode = info["in_mode"]
        if dir == "up":
            more = n
            if not in_mode:
                run("copy-mode", "-e", "-u", "-t", t)
                more = n - 1
            if more:
                _copy_cmd(t, "page-up", more)
        elif dir == "top":
            if not in_mode:
                run("copy-mode", "-e", "-t", t)
            _copy_cmd(t, "history-top")
        elif not in_mode:
            acted = False
        elif dir == "down":
            _copy_cmd(t, "page-down", n)
        else:                                                   # bottom | exit
            _copy_cmd(t, "cancel")
    after = pane_info(name) if acted else info
    return {"mode": "copy" if after["in_mode"] else "app" if after["alt"] else "normal",
            "alt": after["alt"], "pos": after["scroll_pos"]}


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
