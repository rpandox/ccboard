"""Box and agent checks behind GET /api/doctor: what is installed, what is running, what is configured.

Each check is a function db -> Outcome(status, detail, fix) registered with an id, a group and a label. run() executes
them on a thread pool with a hard cap per check (a slow one becomes 'warn: timed out'), caches the answer for a few
seconds per group, and returns plain JSON-ready dicts:

    {generated_at, ok, summary{pass,warn,fail,skip}, checks:[{id, group, label, status, detail, fix{text,cmd?,action?}|None}]}

Rules every check follows: one line of detail, never a secret (versions are parsed out of tool output, never echoed;
`gh auth status` is judged by its exit code only; settings.json and credential files are never quoted), every subprocess
goes through _run() (timeout; a missing binary is a 'fail' with an install fix). Later phases add checks with
register() (one check) or register_provider() (a function returning several finished checks, which is the shape of
Agent.doctor_checks() in app/agents/base.py); Check.to_dict() is the record they produce. The `memory` group (claude-mem, v0.5.10)
is such a provider, memory_checks(): one probe of the worker (app/memory.py) feeds eight checks, and registering it adds the group.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.parse import urlsplit

from . import backup, claude_auth, login_problem, memory, preflight, projects, push, tmux
from . import platform as plat
from . import tailscale as ts
from .config import settings

GROUPS = ["box", "claude", "notify", "terminal"]   # register()/register_provider() append the others (memory, codex)
STATUSES = ("pass", "warn", "fail", "skip")
CHECK_TIMEOUT = 5.0       # hard cap per check; the answer for a slower one is 'warn: timed out'
CACHE_TTL = 20.0
CMD_TIMEOUT = 4.0         # per subprocess, below CHECK_TIMEOUT so a hung tool ends before its check is given up on
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
TTYD_BIN = "/usr/local/bin/ttyd"
ATTACH_MARKER = "# ccboard-attach v2"
HOOK_MARK = "ccboard-hook"
STATUS_MARK = "ccboard-statusline"
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Notification", "Stop", "StopFailure", "SessionEnd", "SubagentStart", "SubagentStop",
               "PreCompact", "PostCompact", "PostModelSwitch", "TaskCreated", "TaskCompleted", "PostToolBatch", "ConfigChange")  # = scripts/claude_settings.py EVENTS (v0.5.7)
HEARTBEAT_KEY = "samples_heartbeat"   # kv key the Sampler touches on every 15 s tick (app/samples.py)
HEARTBEAT_PASS = 60.0                 # seconds: fresher than this passes
HEARTBEAT_WARN = 600.0                # seconds: fresher than this warns, older fails
HEARTBEAT_GRACE = 30.0                # a board up less than this long may not have ticked yet
MIN_TMUX = (3, 2)
MIN_GIT = (2, 15)
DETAIL_MAX = 200

_clock = time.monotonic   # patched by tests (cache age)
_STARTED = time.monotonic()


# ------------------------------------------------------------------ records

class Outcome(NamedTuple):
    status: str
    detail: str
    fix: dict | None = None


@dataclass
class Check:
    id: str
    group: str
    label: str
    status: str
    detail: str
    fix: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def fix(text: str, cmd: str | None = None, action: str | None = None) -> dict:
    out = {"text": text}
    if cmd:
        out["cmd"] = cmd
    if action:
        out["action"] = action
    return out


def _pass(detail: str, f: dict | None = None) -> Outcome:
    return Outcome("pass", detail, f)


def _warn(detail: str, f: dict | None = None) -> Outcome:
    return Outcome("warn", detail, f)


def _fail(detail: str, f: dict | None = None) -> Outcome:
    return Outcome("fail", detail, f)


def _skip(detail: str, f: dict | None = None) -> Outcome:
    return Outcome("skip", detail, f)


_SECRET_RES = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{4,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,})"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),     # the hook token is 64 hex characters
    re.compile(r"(?i)\b(?:token|secret|password|passwd|api[_-]?key)\s*[=:]\s*\S+"),
]


def _clean(detail) -> str:
    """One line, secrets redacted (belt and braces: checks never put raw tool output or file contents here), capped."""
    s = " ".join(str(detail or "").split())
    for rx in _SECRET_RES:
        s = rx.sub("[redacted]", s)
    return s if len(s) <= DETAIL_MAX else s[:DETAIL_MAX - 1] + "…"


def _clean_fix(f: dict | None) -> dict | None:
    if not f:
        return None
    return {k: _clean(v) if k != "cmd" else str(v) for k, v in f.items() if v}


# ------------------------------------------------------------------ the one subprocess helper

class ToolMissing(Exception):
    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


class ToolTimeout(Exception):
    def __init__(self, name: str = "", limit: float | None = None):
        super().__init__(name)
        self.name = name
        self.limit = limit              # the seconds the command was given (None: not known)


class Proc(NamedTuple):
    rc: int
    out: str
    err: str


def _run(argv: list[str], timeout: float = CMD_TIMEOUT) -> Proc:
    """Run argv with a timeout. FileNotFoundError -> ToolMissing(argv[0]); timeout -> ToolTimeout. Tests patch this."""
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        again = _resolve_missing(argv)
        if again is not None:
            return _run(again, timeout)
        raise ToolMissing(Path(argv[0]).name) from e
    except subprocess.TimeoutExpired as e:
        raise ToolTimeout(Path(argv[0]).name, timeout) from e
    except PermissionError as e:       # present but not executable: the same remedy as missing
        raise ToolMissing(Path(argv[0]).name) from e
    return Proc(cp.returncode, cp.stdout or "", cp.stderr or "")


def _resolve_missing(argv: list[str]) -> list[str] | None:
    """`argv` with its bare program name replaced by where platform.resolve_bin finds it (the login shell's PATH, the Homebrew folders), or
    None. Off Linux only: a LaunchAgent's PATH is bare, so a tool the owner has would read as "not installed" (issue #117). Linux never retries,
    so its answers are exactly what the PATH lookup gave."""
    if plat.IS_LINUX or not argv or os.sep in argv[0]:
        return None
    found = plat.resolve_bin(argv[0])
    return [found, *argv[1:]] if found and found != argv[0] else None


def _which(name: str) -> str | None:
    return shutil.which(name) or (None if plat.IS_LINUX else plat.resolve_bin(name))


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return False
    try:
        s.close()
    except OSError:
        pass
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _http_get(url: str, timeout: float = 2.0) -> tuple[int, str]:
    """(status, first 1 KB of the body). An HTTP error status is an answer; a connection failure raises OSError."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as r:
            return r.status, r.read(1024).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except urllib.error.URLError as e:
        raise OSError(str(e.reason)) from e


_VER_RE = re.compile(r"(\d+)\.(\d+)")


def _ver(text: str) -> tuple[int, int] | None:
    """'tmux 3.3a', 'tmux next-3.5', 'git version 2.39.3 (Apple Git-146)', 'gh version 2.40.1 (2024-01-01)' -> (major, minor)."""
    m = _VER_RE.search(text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _vs(v: tuple[int, int]) -> str:
    return f"{v[0]}.{v[1]}"


_FULL_VER_RE = re.compile(r"\d+(?:\.\d+)+")


def _vstr(text: str) -> str | None:
    """The first dotted version in tool output ('ttyd version 1.7.7' -> '1.7.7'); the only part of the output that is shown."""
    m = _FULL_VER_RE.search(text or "")
    return m.group(0) if m else None


def _install(tool: str) -> tuple[str, str | None]:
    """(text, command) for installing `tool`; the package commands name this system's package manager (platform.hint)."""
    return {
        "tmux": ("Install tmux 3.2 or newer", plat.hint("install", "tmux")),
        "git": ("Install git 2.15 or newer", plat.hint("install", "git")),
        "gh": ("Install the GitHub CLI, then log in", plat.hint("install", "gh") + " && gh auth login"),
        "ccusage": ("Install ccusage (needs node and npm)", "npm install -g --prefix ~/.local ccusage"),
        "claude": ("Install Claude Code on the box", "curl -fsSL https://claude.ai/install.sh | bash"),
        "ttyd": ("Re-run the installer: it installs ttyd", "./install.sh"),
    }.get(tool, (f"Install {tool}", None))


def _missing(tool: str) -> Outcome:
    text, cmd = _install(tool)
    return _fail(f"{tool} is not installed", fix(text, cmd))


# ------------------------------------------------------------------ terminal checks

def _c_tmux(db) -> Outcome:
    try:
        p = _run(["tmux", "-V"])
    except ToolMissing:
        return _missing("tmux")
    v = _ver(p.out or p.err)
    if p.rc != 0 or v is None:
        return _warn("could not read the tmux version", fix("Run tmux -V on the box", "tmux -V"))
    if v < MIN_TMUX:
        return _fail(f"tmux {_vs(v)} is older than {_vs(MIN_TMUX)}", fix(f"Upgrade tmux to {_vs(MIN_TMUX)} or newer", plat.hint("upgrade", "tmux")))
    return _pass(f"tmux {_vstr(p.out or p.err) or _vs(v)}")


def _c_tmux_server(db) -> Outcome:
    if tmux.server_up():
        return _pass(f"the tmux server on socket '{settings.tmux_socket}' is running")
    return _fail(f"no tmux server on socket '{settings.tmux_socket}'",
                 fix("Start the tmux service that owns the sessions (never restart it while sessions run)", plat.hint("start", "ccboard-tmux")))


def _c_tmux_conf(db) -> Outcome:
    return _skip("verified in v0.5.7")


SOCKET_PATH_MAX = 100     # bytes; a unix socket path holds 104 on macOS and 108 on Linux (sun_path), 100 leaves a margin


def _tmux_server_sockets() -> list[tuple[int, str]] | None:
    """(pid, socket path) of every live tmux server on the board's socket name, read from the process titles ('tmux: server (<path>)')
    that `ps` shows; None when the process list cannot be read. Tests patch this."""
    try:
        p = _run(["ps", "-axo", "pid=,command="])
    except (ToolMissing, ToolTimeout):
        return None
    if p.rc != 0:
        return None
    return _parse_server_titles(p.out, settings.tmux_socket)


def _parse_server_titles(text: str, name: str) -> list[tuple[int, str]]:
    found = []
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)\s+tmux: server \((/[^)]*)\)", line)
        if m and m.group(2).rsplit("/", 1)[-1] == name:
            found.append((int(m.group(1)), m.group(2)))
    return found


def _c_tmux_socket(db) -> Outcome:
    """Where the board's tmux socket is and whether it is safe: path from tmux itself, directory mode, length, and a server that is
    alive while its socket file is gone. Sends no signal and reads no session content; a down server is tmux-server's case."""
    path = tmux.socket_path(fresh=True)
    if path is None:
        alive = _tmux_server_sockets()
        if not alive:
            return _skip("no tmux server to ask (the tmux server check says why)")
        pid, where = alive[0]
        if os.path.exists(where):
            return _warn(f"a tmux server (pid {pid}) is running but does not answer on {where}",
                         fix("Look at the socket directory permissions and at the tmux service log", f"ls -ld {shlex.quote(os.path.dirname(where))}"))
        return _warn(f"the tmux server (pid {pid}) is running but its socket file {where} is gone",
                     fix("tmux recreates its socket when the server process gets SIGUSR1 (tmux manual); the board never sends it, run it yourself "
                         "and never restart the tmux service while sessions run", f"kill -USR1 {pid}"))
    folder = os.path.dirname(path)
    try:
        mode = os.stat(folder).st_mode & 0o777
    except OSError:
        return _warn(f"socket {path}: its directory cannot be read")
    n = len(path.encode("utf-8", "replace"))
    if mode & 0o022:
        return _warn(f"socket {path}: the directory is writable by group or others (mode {mode:04o})",
                     fix("The socket directory must be private to its owner", f"chmod 700 {shlex.quote(folder)}"))
    if n > SOCKET_PATH_MAX:
        return _warn(f"socket {path}: the path is {n} bytes, over the {SOCKET_PATH_MAX} byte limit",
                     fix("Use a shorter TMUX_TMPDIR for the tmux service (a unix socket path holds 104 bytes on macOS and 108 on Linux)"))
    if not os.path.exists(path):
        return _warn(f"socket {path}: tmux reports it but the file is missing",
                     fix("tmux recreates its socket when the server process gets SIGUSR1 (tmux manual); the board never sends it"))
    return _pass(f"socket {path}, directory mode {mode:04o}, {n} of {SOCKET_PATH_MAX} bytes")


def _c_ttyd(db) -> Outcome:
    port = settings.ttyd_port
    up = _port_open("127.0.0.1", port, 1.0)
    exe = _which("ttyd") or (TTYD_BIN if os.access(TTYD_BIN, os.X_OK) else None)
    ver = None
    if exe:
        try:
            ver = _vstr(_run([exe, "--version"]).out)
        except (ToolMissing, ToolTimeout):
            ver = None
    if up:
        return _pass(f"listening on 127.0.0.1:{port}" + (f", ttyd {ver}" if ver else ""))
    if exe:
        return _fail(f"ttyd is installed but nothing listens on 127.0.0.1:{port}",
                     fix("Restart the ttyd service", plat.hint("restart", "ccboard-ttyd")))
    return _fail("ttyd is not installed and nothing listens on its port", fix(*_install("ttyd")))


CHECKOUT = Path(__file__).resolve().parent.parent


def _app_dir() -> Path:
    """Where bin/ and scripts/ live for the host side: the data dir's copy (container runtime, or whenever it exists), else the checkout."""
    seeded = Path(settings.data_dir) / "app"
    return seeded if settings.runtime == "docker" or (seeded / "bin").is_dir() else CHECKOUT


def _attach_paths() -> list[Path]:
    paths = [Path(settings.data_dir) / "app" / "bin" / "ccboard-attach"]
    if settings.runtime != "docker":      # the systemd units run the checkout's copy
        paths.append(CHECKOUT / "bin" / "ccboard-attach")
    return paths


def _c_attach_wrapper(db) -> Outcome:
    paths = _attach_paths()
    path = next((p for p in paths if p.is_file()), None)
    if path is None:
        return _fail(f"{paths[0]} does not exist", fix("Restart the board container, or re-run the installer: both sync bin/ to the data dir", "./install.sh"))
    if not os.access(path, os.X_OK):
        return _fail(f"{path} is not executable", fix("Make it executable", f"chmod 0755 {path}"))
    try:
        with open(path, "r", errors="replace") as f:
            head = f.read(4096)
    except OSError:
        return _warn(f"{path} is not readable", fix("Make it readable by the board's user", f"chmod 0755 {path}"))
    if ATTACH_MARKER not in head:
        return _warn("v1 wrapper", fix("The v2 wrapper (viewer modes, read-only attach) arrives with v0.5.7; nothing to do yet"))
    return _pass(f"v2 wrapper at {path}")


# ------------------------------------------------------------------ box checks

def code_server_settings_path() -> Path:
    """code-server's user settings (CODE_SERVER_SETTINGS overrides, as scripts/code_server_settings.py does)."""
    return Path(os.environ.get("CODE_SERVER_SETTINGS") or (Path.home() / ".local" / "share" / "code-server" / "User" / "settings.json"))


CODE_SERVER_SETTINGS = code_server_settings_path()


def _c_code_server(db) -> Outcome:
    port = settings.code_server_port
    if _port_open("127.0.0.1", port, 1.0):
        try:
            cfg = json.loads(Path(CODE_SERVER_SETTINGS).read_text() or "{}")
        except (OSError, ValueError):
            cfg = {}
        if not isinstance(cfg, dict) or "files.watcherExclude" not in cfg:
            return _warn(f"listening on 127.0.0.1:{port}; no watcher excludes in its user settings (slow to open on a busy box)",
                         fix("Merge ccboard's code-server settings", "python3 scripts/code_server_settings.py install"))
        return _pass(f"listening on 127.0.0.1:{port}")
    return _fail(f"nothing listens on 127.0.0.1:{port}", fix("Start code-server", plat.hint("restart", "code-server@$USER")))


def _c_projects_dir(db) -> Outcome:
    p = Path(settings.projects_dir)
    if not p.is_dir():
        return _fail(f"{p} is not a directory", fix("Create it and point PROJECTS_DIR at it", f"mkdir -p {p}"))
    if not os.access(p, os.W_OK | os.X_OK):
        return _fail(f"{p} is not writable by the board's user", fix("Give the board's user ownership", f"sudo chown $USER {p}"))
    return _pass(f"{p} exists and is writable")


PROC_VERSION = Path("/proc/version")      # tests point this at a temp file
CONTAINER_LINUX_ONLY = "Container mode is Linux only: it needs a Linux host with Docker Engine"    # the README says the same


def _c_runtime_host(db) -> Outcome:
    """Runtime docker on a Docker Desktop virtual machine (its kernel names linuxkit) cannot reach the host's tmux, processes or
    programs. Reads /proc/version only; never calls the Docker socket. The linuxkit signal is to verify on a Mac and on Windows."""
    if settings.runtime != "docker":
        return _skip("the board is not running as a container")
    try:
        kernel = PROC_VERSION.read_text(errors="replace").lower()
    except OSError:
        return _skip("the kernel version could not be read")
    if "linuxkit" in kernel:
        return _fail("this container runs inside Docker Desktop's virtual machine (the kernel names linuxkit), not on a Linux host: "
                     "the host's tmux sockets, processes and programs are out of reach",
                     fix("Run the board on a Linux host with Docker Engine (docker-ce inside WSL2 on Windows), or use the systemd runtime"))
    return _pass(f"{CONTAINER_LINUX_ONLY}, and this kernel is Linux")


def _c_git(db) -> Outcome:
    try:
        p = _run(["git", "--version"])
    except ToolMissing:
        return _missing("git")
    v = _ver(p.out)
    if p.rc != 0 or v is None:
        return _warn("could not read the git version", fix("Run git --version on the box", "git --version"))
    if v < MIN_GIT:
        return _fail(f"git {_vs(v)} is older than {_vs(MIN_GIT)}", fix(f"Upgrade git to {_vs(MIN_GIT)} or newer", plat.hint("upgrade", "git")))
    # the clone probe's address pin (http.curloptResolve, git 2.37+) is read from the version, not from `git help config` (no man pages in the container)
    return _pass(f"git {_vstr(p.out) or _vs(v)}; {preflight.pin_note(preflight.parse_git_version(p.out) or (v[0], v[1], 0))}")


# ------------------------------------------------------------------ devcontainer sessions need the HOST's docker (issue #104)

DEVCONTAINER_INSTALL = "npm install -g --prefix ~/.local @devcontainers/cli"       # what `CCBOARD_DEVCONTAINER=1 ./install.sh` runs


def _devcontainer_repos() -> list[str] | None:
    """`project/repo` of every repo in the project scan that has .devcontainer/devcontainer.json; None when the board has no scan to give."""
    src = _projects_source
    projs = src() if callable(src) else None
    if projs is None:
        return None
    return [f"{p.get('name')}/{r.get('name')}" for p in (projs if isinstance(projs, list) else []) if isinstance(p, dict)
            for r in (p.get("repos") or []) if isinstance(r, dict) and r.get("devcontainer") is True]


def _probe_docker() -> str:
    """'ok' | 'missing' | 'denied' (docker ran and the daemon did not answer this user: it is stopped, or the user is not in the docker group) |
    'timeout'. `docker info` talks to the daemon, which is the question; `docker --version` would pass for a user without access."""
    try:
        p = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=HELPER_TIMEOUT)
    except ToolMissing:
        return "missing"
    except ToolTimeout:
        return "timeout"
    return "ok" if p.rc == 0 else "denied"


def _devcontainer_path() -> str:
    """Where the host's devcontainer CLI is looked up: the hook PATH, then ~/.local/bin (where install.sh puts it)."""
    return _hook_path() + os.pathsep + str(Path.home() / ".local" / "bin")


def devcontainer_cli_state() -> str:
    """'ok' | 'missing' | 'broken' (ran, exited non-zero) | 'timeout': the probe behind the Devcontainer doctor row and the launcher's refusal.
    On the host it runs `devcontainer --version` on the host's PATH. In a container the host's PATH is out of sight, so the one place it can
    look is ~/.local/bin/devcontainer (the host's home is mounted)."""
    if settings.runtime == "docker":
        local = Path.home() / ".local" / "bin" / "devcontainer"
        return "ok" if os.path.isfile(local) and os.access(local, os.X_OK) else "missing"
    return _probe_helper("devcontainer", ["--version"], _devcontainer_path())


def _c_devcontainer(db) -> Outcome:
    """What "Run in the devcontainer" needs. The board types `devcontainer up ... && devcontainer exec ... -- claude` into the session's tmux
    window, and that tmux is the HOST's, so the line runs on the host: the prerequisites are the host's docker, the user in the docker group
    and the `devcontainer` CLI. The board itself never calls docker (it only tests that .devcontainer/devcontainer.json exists), so a board
    container needs no docker CLI and no socket. Skips when no repo has a devcontainer. Run by the board on the host (systemd, launchd) it asks
    docker and the CLI directly. Run by the board in a container it cannot see the host's docker, and says so; the one thing it can see is
    ~/.local/bin/devcontainer (the host's home is mounted), where install.sh puts the CLI."""
    repos = _devcontainer_repos()
    if repos is None:
        return _skip("the project scan is not ready yet, so it is not known whether a repo has a devcontainer")
    if not repos:
        return _skip("no repo has a .devcontainer/devcontainer.json, so nothing asks for the host's docker and devcontainer CLI")
    n = f"{len(repos)} repo{'s' if len(repos) != 1 else ''} with a devcontainer"
    install = fix(f"Install the CLI on the host: CCBOARD_DEVCONTAINER=1 ./install.sh, or {DEVCONTAINER_INSTALL}", DEVCONTAINER_INSTALL)
    local = Path.home() / ".local" / "bin" / "devcontainer"
    if settings.runtime == "docker":
        if os.path.isfile(local) and os.access(local, os.X_OK):
            return _pass(f"{n}: the devcontainer CLI is in ~/.local/bin. Docker and the docker group are the host's and out of sight of this "
                         f"container; the line the board types runs in the host's tmux")
        return _warn(f"{n}, but no devcontainer CLI in ~/.local/bin (where install.sh puts it); a copy elsewhere on the host cannot be seen "
                     f"from the container",
                     fix(f"On the host: CCBOARD_DEVCONTAINER=1 ./install.sh, or {DEVCONTAINER_INSTALL} (ignore this if it is installed elsewhere there)",
                         DEVCONTAINER_INSTALL))
    from concurrent.futures import ThreadPoolExecutor
    path = _devcontainer_path()
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_docker = ex.submit(_probe_docker)
        f_cli = ex.submit(_probe_helper, "devcontainer", ["--version"], path)
        docker, cli = f_docker.result(), f_cli.result()
    if docker == cli == "ok":
        return _pass(f"{n}: docker answers for this user and the devcontainer CLI runs")
    bad: list[tuple[str, dict]] = []
    if docker == "missing":
        if plat.IS_MACOS:   # Homebrew has no docker.io: on a Mac docker comes with Docker Desktop
            bad.append(("docker is not installed", fix("Install Docker Desktop for Mac and start it (https://docs.docker.com/desktop/setup/install/mac-install/)")))
        else:
            bad.append(("docker is not installed", fix("Install Docker, then add your user to the docker group", plat.hint("install", "docker.io"))))
    elif docker == "denied":
        bad.append(("docker did not answer for this user (stopped, or not in the docker group)",
                    fix("Start Docker, and add your user to the docker group (log in again afterwards)", "sudo usermod -aG docker $USER")))
    if cli in ("missing", "broken"):
        bad.append(("the devcontainer CLI is not installed" if cli == "missing" else "the devcontainer CLI does not run (it needs node)", install))
    if bad:
        return _warn(f"{n}, but {' and '.join(b[0] for b in bad)}: 'Run in the devcontainer' would fail", bad[0][1])
    slow = [n_ for n_, r in (("docker info", docker), ("devcontainer --version", cli)) if r == "timeout"]
    return _warn(f"unknown: {' and '.join(slow)} did not answer within {int(HELPER_TIMEOUT)} s", fix("Run docker info and devcontainer --version in a terminal"))


# ------------------------------------------------------------------ a slow tool is not a broken tool (issue #99)

BUSY_NOTE = "the box is busy; this is not a failure"
STALE_AFTER = 86400.0                  # a last good answer older than this is a warning again
GH_AUTH_LIMIT = 15.0                   # `gh auth status` contacts GitHub: it gets its own, longer limit on a background thread
GH_WAIT = 3.5                          # how long the gh check itself waits for its probes (the cap is CHECK_TIMEOUT)
RECHECK_FIX = "Press Re-check in a minute; if it stays, the box is loaded (see Home > Box)"
_wall = time.time                      # patched by tests (the age of a last good answer)
_last_good: dict[str, tuple[float, Outcome]] = {}      # check id -> (when, the last `pass` it gave): what a timeout falls back to


def _age_words(seconds: float) -> str:
    s = int(max(0.0, seconds))
    return f"{s // 86400} d" if s >= 2 * 86400 else _ago(seconds)


def _busy_words(tool: str, limit: float | None) -> str:
    return f"{tool} did not answer in {limit:g} s ({BUSY_NOTE})" if limit else f"{tool} did not answer in time ({BUSY_NOTE})"


def _stale(tool: str, last: tuple[float, Outcome] | None, limit: float | None = None, running: bool = False) -> Outcome:
    """The one wording for a tool that is slow, not broken. With a last good answer: that answer, its age, and why it is not fresh
    ('logged in, checked 3 min ago; the latest re-check is still running'), still a pass (a warning that was real stays a warning),
    unless the answer is over a day old. Without one: a skip, never a warn, with the cause. `running`: the probe has not finished yet."""
    why = "the latest re-check is still running" if running else _busy_words(tool, limit)
    if last is None:
        return _skip(f"{tool} is still answering ({BUSY_NOTE})" if running else _busy_words(tool, limit), fix(RECHECK_FIX))
    at, out = last
    age = max(0.0, _wall() - at)
    if age > STALE_AFTER:
        return _warn(f"the last good answer from {tool} is {_age_words(age)} old; {why}", fix(RECHECK_FIX))
    return Outcome(out.status, f"{out.detail}, checked {_age_words(age)} ago; {why}", out.fix if out.status != "pass" else None)


class _Probe:
    """One slow command per key, run on a daemon thread, at most one in flight. `last` is the newest real answer (when, value)."""
    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.done = threading.Event()
        self.result: tuple[str, object] | None = None      # ("ok", value) | ("err", exception)
        self.last: tuple[float, object] | None = None


_probes: dict[str, _Probe] = {}


def _probe_start(key: str, fn: Callable) -> _Probe:
    """Start fn() on a daemon thread unless this key's probe is still running (a re-check never piles up probes)."""
    with _lock:
        pr = _probes.setdefault(key, _Probe())
    with pr.lock:
        if pr.thread is None or not pr.thread.is_alive():
            done = pr.done = threading.Event()
            pr.result = None

            def work():
                try:
                    r: tuple[str, object] = ("ok", fn())
                except Exception as e:                      # handed to the check, which decides what it means
                    r = ("err", e)
                with pr.lock:
                    pr.result = r
                    if r[0] == "ok":
                        pr.last = (_wall(), r[1])
                done.set()
            pr.thread = threading.Thread(target=work, name=f"doctor-probe-{key}", daemon=True)
            pr.thread.start()
    return pr


def _probe_wait(pr: _Probe, deadline: float) -> tuple[str, object] | None:
    """The probe's answer if it arrives before the monotonic `deadline`, else None (it keeps running and fills `last`)."""
    done = pr.done
    done.wait(max(0.0, deadline - time.monotonic()))
    with pr.lock:
        return pr.result if done.is_set() else None


def _gh_answer(where: str, proc: Proc) -> Outcome:
    if proc.rc == 0:
        return _pass(f"{where}, logged in")
    return _warn(f"{where}, not logged in", fix("Log gh in on the box (PRs and clone-from-GitHub need it)", "gh auth login"))


def _c_gh(db) -> Outcome:
    """gh --version and gh auth status run side by side on background probes, so a slow auth check (it contacts GitHub; the auth probe gets
    GH_AUTH_LIMIT) never hides a missing binary and never turns into a bare 'timed out': a probe that has not answered gives the last good
    answer with its age, or a skip that says the box is busy. A missing binary and a logged-out gh still warn with their fix."""
    pv = _probe_start("gh-version", lambda: _run(["gh", "--version"]))
    pa = _probe_start("gh-auth", lambda: _run(["gh", "auth", "status"], GH_AUTH_LIMIT))   # exit code only: its output can mention the account and token source
    deadline = time.monotonic() + GH_WAIT
    rv = _probe_wait(pv, deadline)
    if rv and rv[0] == "err":
        if isinstance(rv[1], ToolMissing):
            return _missing("gh")
        if not isinstance(rv[1], ToolTimeout):
            raise rv[1]                                     # type: ignore[misc]
    proc = rv[1] if rv and rv[0] == "ok" else None
    if proc is not None and proc.rc != 0:
        return _warn("gh --version failed", fix("Reinstall the GitHub CLI", plat.hint("reinstall", "gh")))
    seen = proc.out if proc is not None else (pv.last[1].out if pv.last else "")      # type: ignore[union-attr]
    where = f"gh {_vstr(seen)}" if _ver(seen) else "gh"
    ra = _probe_wait(pa, deadline)
    limit, running = GH_AUTH_LIMIT, ra is None
    if ra is not None:
        if ra[0] == "ok":
            return _gh_answer(where, ra[1])                 # type: ignore[arg-type]
        if isinstance(ra[1], ToolMissing):
            return _missing("gh")
        if not isinstance(ra[1], ToolTimeout):
            raise ra[1]                                     # type: ignore[misc]
        limit = getattr(ra[1], "limit", None) or GH_AUTH_LIMIT
    last = (pa.last[0], _gh_answer(where, pa.last[1])) if pa.last else None       # type: ignore[arg-type]
    return _stale("gh", last, limit, running)


def _c_ccusage(db) -> Outcome:
    try:
        p = _run(["ccusage", "--version"])
    except ToolMissing:
        return _missing("ccusage")
    v = _vstr(p.out or p.err)
    if p.rc != 0:
        return _warn("ccusage --version failed", fix("Reinstall ccusage", "npm install -g --prefix ~/.local ccusage"))
    return _pass(f"ccusage {v}" if v else "ccusage installed")


def _c_identity(db) -> Outcome:
    rt = settings.runtime
    n = len(settings.allowed_users or ())
    if settings.dev_bypass_user:
        return _warn(f"runtime {rt}; dev bypass is ON: every request is treated as one fixed user",
                     fix("Unset CCBOARD_DEV_BYPASS_USER outside a dev shell"))
    if n == 0:
        return _fail(f"runtime {rt}; CCBOARD_ALLOWED_USERS is empty, so every request is denied",
                     fix("List the allowed Tailscale logins in /etc/ccboard/env (CCBOARD_ALLOWED_USERS), then restart the board"))
    return _pass(f"runtime {rt}; the Tailscale-User-Login header is expected; {n} allowed user{'s' if n != 1 else ''}")


TS_TIMEOUT = 1.5     # seconds per tailscale command: three of them still end inside CHECK_TIMEOUT


def _ts_start_fix(v: str) -> dict:
    """Where Tailscale is not running or not reachable: the fix for this variant."""
    if v == "linux":
        return fix("Start the Tailscale daemon on the box (a container reads its mounted socket)", plat.hint("start", "tailscaled"))
    if v == "macos-opensource":
        return fix("Start the Tailscale daemon", "sudo brew services start tailscale")
    if v in ("macos-appstore", "macos-standalone"):
        return fix("Open the Tailscale app and sign in")
    return fix("Start Tailscale and sign in (the steps for this system are to verify)")


def _ts_serve(cli, v: str) -> tuple[str, str, dict | None]:
    """Read `tailscale serve status --json` as this user: (state, detail, fix) with state ok | harmless | denied | unreadable. On Linux outside the
    container a denial is judged by trying the same `sudo -n` retry the board uses (a read of the serve status, which the sudoers rule covers)."""
    cmd = cli.cmd("serve", "status", "--json")
    try:
        p = _run(cmd, timeout=TS_TIMEOUT)
    except (ToolMissing, ToolTimeout):
        return "unreadable", "serve status could not be read", fix("Check that Tailscale is running")
    kind = ts.classify(p.rc, p.out, p.err)
    if kind == "ok":
        return "ok", "serve status readable", None
    if kind != "denied":
        return "unreadable", "serve status could not be read", fix("Check that Tailscale is running and signed in")
    user = Path.home().name or "<user>"
    if ts.sudo_retry_allowed():
        try:
            q = _run(["sudo", "-n", *cmd], timeout=TS_TIMEOUT)
        except (ToolMissing, ToolTimeout):
            q = Proc(1, "", "")
        if q.rc == 0:
            return "harmless", "serve status is denied to this user, which is harmless: previews retry through sudo -n and that works", None
        return "denied", "serve is denied to this user and the sudo -n retry failed, so previews cannot be opened", fix(
            "Re-run the installer: it writes the sudoers rule for tailscale serve", "./install.sh")
    if settings.runtime == "docker":
        return "denied", "serve is denied to this user and the container has no sudo, so previews cannot be opened", fix(
            "Allow this user to run serve (once, on the host)", f"sudo tailscale set --operator={user}")
    return "denied", f"serve is denied to this user: {ts.LABELS.get(v, ts.LABELS['unknown'])} refused it", fix(
        ts.denial_fix(v), f"sudo tailscale set --operator={user}" if v == "macos-opensource" else None)


def _c_tailscale(db) -> Outcome:
    """Tailscale on this machine, read only: the command found and which, the variant (from file layout), signed in (BackendState Running), a
    MagicDNS name, HTTPS certificates, and whether `serve status` can be read by this user (the operator state: ok, denied, not running, not
    signed in, not installed). Every identity the board accepts comes through `tailscale serve`, and previews need serve. On Linux a denial
    that the sudoers rule makes harmless passes and says so; a refusal that is not harmless is a warning (the board works, previews do not)."""
    cli = ts.find_cli()
    v = ts.variant(cli)
    if cli is None:
        return _fail(ts.missing_reason(), fix("Install Tailscale, sign in, then check again", None))
    where = f"{cli.exe} ({v})"
    try:
        p = _run(cli.cmd("status", "--json"), timeout=TS_TIMEOUT)
    except ToolMissing:
        if plat.tailscale_placement(implied=False) == "host":    # WSL, set on purpose (issue #118): Tailscale is on Windows, the distro has no command for it
            return _skip("Tailscale runs on the Windows host (CCBOARD_TAILSCALE_PLACEMENT=host), so this distro has no tailscale command; check it from Windows. "
                         "The wsl-placement row says what that means for previews")
        return _fail(f"{where}: {ts.missing_reason()}", fix("Install Tailscale, sign in, then check again", None))
    except ToolTimeout:
        return _warn(f"{where}: tailscale status did not answer in time", fix("Check that Tailscale is running"))
    d = ts._json(p.out) if p.rc == 0 else None
    if not isinstance(d, dict) or not d:
        return _fail(f"{where}: Tailscale is not running or cannot be reached", _ts_start_fix(v))
    s = ts.summarize_status(d)
    if s["state"] != "Running":
        return _fail(f"{where}: Tailscale is not signed in or is stopped (BackendState {s['state'] or 'unknown'})",
                     fix("Sign in to Tailscale", "sudo tailscale up" if v == "linux" else None))
    problems = []
    if not s["fqdn"]:
        problems.append(("no MagicDNS name", fix("Enable MagicDNS in the Tailscale admin console (DNS page)")))
    if not s["certs"]:
        problems.append(("HTTPS certificates are off for the tailnet", fix("Turn on HTTPS Certificates in the Tailscale admin console (DNS page)")))
    state, note, sfix = _ts_serve(cli, v)
    if state in ("unreadable", "denied"):
        problems.append((note, sfix))
    if problems:
        return _warn(f"{where}: signed in; " + "; ".join(t for t, _ in problems), problems[0][1])
    ok = "MagicDNS and HTTPS certificates on"
    return _pass(f"{where}: signed in, {ok}; {note}")


def _c_node(db) -> Outcome:
    """This board as a node (issues #131 and #133), read only: the node id is saved and still the one Tailscale reports, the name is a valid node
    name, CCBOARD_PUBLIC_URL is set when other boards are configured (a peer needs an address to reach this one), and CCBOARD_NODE_LANES is a
    whole number. Nothing is written here; the id file is only created by the board's own first request for the id."""
    from . import health, nodes
    try:
        rep = nodes.id_report()
    except Exception as e:
        return _warn(f"the node id could not be read ({e.__class__.__name__})", fix("Check that the data directory is writable by the board"))
    if rep["drifted"]:
        return _warn(f"the saved node id ({rep['id']}) is not the id Tailscale reports now ({rep['ts_id']}): this device was registered again",
                     fix("Reset the node id (remove the node-id file in the data directory and restart the board); every pair must be made again afterwards"))
    problems = []
    if settings.node_name and nodes.display_name() != settings.node_name:
        problems.append(("CCBOARD_NODE_NAME is not a valid node name (letters, digits, - or _, at most 41 characters), so the shown name is " + nodes.display_name(),
                         fix("Set CCBOARD_NODE_NAME in /etc/ccboard/env to letters, digits, - or _ and restart the board")))
    if getattr(settings, "node_lanes_bad", False):
        problems.append(("CCBOARD_NODE_LANES is not a whole number from 0 to 999, so the default 3 is used",
                         fix("Set CCBOARD_NODE_LANES in /etc/ccboard/env to a whole number (0 means no limit) and restart the board")))
    if health.parse_nodes(settings.nodes_raw) and not settings.public_url:
        problems.append(("other nodes are configured but CCBOARD_PUBLIC_URL is empty, so they cannot be told this board's address",
                         fix("Set CCBOARD_PUBLIC_URL in /etc/ccboard/env (the board's https address on the tailnet) and rerun ./install.sh")))
    if problems:
        return _warn("; ".join(t for t, _ in problems), problems[0][1])
    cap = int(getattr(settings, "node_lanes", 3))
    return _pass(f"node {nodes.display_name()}, id {rep['id']} ({'from Tailscale' if rep['kind'] == 'tailscale' else 'random, kept in the data directory'}); "
                 + (f"{cap} lanes advised" if cap else "no lane limit advised"))


def _c_tailscale_status(db) -> Outcome:
    """What finding other nodes needs from Tailscale (issue #134), read only, the same reading Settings > Nodes shows: signed in, a MagicDNS suffix
    (names under it are the only ones a probe may reach), HTTPS certificates for this device, and how many devices could be nodes. The `tailscale` row
    above judges the command and `serve`; this one is about the tailnet. A board that is not on a tailnet is not a failure: finding nodes is optional."""
    from . import nodes, nodes_discovery as nd
    status, info = nd.read_tailscale(fresh=True)
    if status is None:
        if nodes.windows_side():
            return _skip(info["reason"])
        if ts.find_cli() is None:
            return _skip(f"{info['reason']}. Finding other nodes needs Tailscale; a single board does not")
        return _warn(info["reason"], _ts_start_fix(info["variant"]))
    problems = []
    if not nodes.dns_name(status):
        problems.append(("this device has no MagicDNS name", fix("Enable MagicDNS in the Tailscale admin console (DNS page)")))
    if not status.get("CertDomains"):
        problems.append(("HTTPS certificates are off for the tailnet, so a board cannot answer a probe over https",
                         fix("Turn on HTTPS Certificates in the Tailscale admin console (DNS page)")))
    if problems:
        return _warn("; ".join(t for t, _ in problems), problems[0][1])
    cand = nd.candidates(status, settings.node_tags, nd.self_user(status))
    online = sum(1 for r in cand if r["online"])
    tags = ", ".join(settings.node_tags) or "none"
    return _pass(f"signed in, MagicDNS suffix {nd.magic_suffix(status)}, HTTPS on; {len(cand)} device{'s' if len(cand) != 1 else ''} could be a node "
                 f"({online} online; the same user's devices and tags: {tags})")


def _c_nodes_port(db) -> Outcome:
    """This board is served on a port the probe list covers (issue #134): another board looks for ccboard on CCBOARD_NODE_PORTS (443 and 8443 by
    default), so a board on any other port is not found unless every other board lists its port too. A malformed list is named."""
    port = int(getattr(settings, "ccboard_https_port", 443) or 443)
    ports = tuple(getattr(settings, "node_ports", (443, 8443)))
    listed = ",".join(str(p) for p in ports)
    if getattr(settings, "node_ports_bad", False):
        return _warn(f"CCBOARD_NODE_PORTS has an item that is not a port (1 to 65535), so only {listed} is used",
                     fix("Set CCBOARD_NODE_PORTS in /etc/ccboard/env to ports separated by commas, for example 443,8443, and restart the board"))
    if port not in ports:
        return _warn(f"this board is served on port {port}, which is not in CCBOARD_NODE_PORTS ({listed}), so other boards will not find it",
                     fix(f"Add {port} to CCBOARD_NODE_PORTS in /etc/ccboard/env on this board and on every other board that should find it, then restart them",
                         f"CCBOARD_NODE_PORTS={listed},{port}"))
    return _pass(f"this board is served on port {port}; probes try {listed}")


def _c_nodes_tagged_self(db) -> Outcome:
    """Is this device tagged (issue #134)? A tagged device has no user, so a browser on it carries no identity and cannot open any board; it is fine
    for a server nobody browses from. Also names a malformed CCBOARD_NODE_TAGS. Read only; nothing is probed."""
    from . import nodes_discovery as nd
    status, info = nd.read_tailscale(fresh=False)
    me = status.get("Self") if isinstance(status, dict) else None
    bad = getattr(settings, "node_tags_bad", False)
    if bad:
        return _warn("CCBOARD_NODE_TAGS has an item that is not a Tailscale tag (tag:name), so it is ignored",
                     fix("Set CCBOARD_NODE_TAGS in /etc/ccboard/env to tags such as tag:ccboard separated by commas (none = the same user's devices only), then restart the board"))
    if not isinstance(me, dict):
        return _skip(f"{info['reason'] or 'Tailscale could not be read'}, so it is not known whether this device is tagged")
    if nd._tags(me.get("Tags")):
        return _warn("this device is tagged: browsers on it carry no identity (fine for a server nobody browses from)",
                     fix("To open boards from this device, remove its tag in the Tailscale admin console and sign in as a user; otherwise open the boards from another device"))
    return _pass("this device belongs to a user, so a browser on it carries an identity")


PAIR_UNUSED_DAYS = 90        # a pair not used for this long is worth a look (issue #135)
PAIR_TOKEN_DAYS = 180        # a pair token this old should be rotated


def _c_nodes_pairs(db) -> Outcome:
    """The pairs with other boards (issue #135), read only, from the same lists Settings > Nodes shows: a pair nobody has used for 90 days, a token older
    than 180 days (since it was made or last rotated) and a pair marked needs_repair (the other board stopped taking the token). Each fix names the button.
    A board with no pair skips; nothing is called, so an offline peer cannot make this slow."""
    from . import nodes
    try:
        rows = nodes.peers(db=db)
    except Exception as e:
        return _warn(f"the paired nodes could not be read ({e.__class__.__name__})", fix("Check that the data directory is writable by the board"))
    rows = [r for r in rows if not r.get("legacy")]
    if not rows:
        return _skip("no node is paired; pair one from Settings > Nodes when you want boards to work together")
    unused, old, repair = [], [], []
    for r in rows:
        name = r.get("name") or r.get("peer_id") or "a node"
        calls = r.get("direction") == "in"
        if r.get("needs_repair"):
            repair.append(name)
        seen = _iso_age(r.get("last_seen") or r.get("created_at"))
        if seen is not None and seen > PAIR_UNUSED_DAYS * 86400:
            unused.append((name, calls))
        # A pair that calls this board records when its token was rotated. A pair this board calls does not yet (the row keeps its first created_at), so
        # its token's age is not judged until the row has a rotated_at key: otherwise Rotate token could never clear the warning.
        known = calls or "rotated_at" in r
        made = _iso_age(r.get("rotated_at") or r.get("created_at")) if known else None
        if made is not None and made > PAIR_TOKEN_DAYS * 86400:
            old.append((name, calls))
    if repair:
        return _warn(f"the other board no longer takes the saved token for {', '.join(repair)}",
                     fix("Open Settings > Nodes, press Remove on that node, then Add node with a new pairing code from the other board"))
    if old:
        who = ", ".join(n for n, _ in old)
        mine = [n for n, calls in old if not calls]
        if mine:
            return _warn(f"the token for {who} is over {PAIR_TOKEN_DAYS} days old",
                         fix("Open Settings > Nodes and press Rotate token on that node (the old token keeps working for 60 seconds)"))
        return _warn(f"the token {who} uses here is over {PAIR_TOKEN_DAYS} days old",
                     fix("Ask the owner of that board to press Rotate token on its Settings > Nodes, or press Revoke under Who can control this node and pair again"))
    if unused:
        who = ", ".join(n for n, _ in unused)
        return _warn(f"{who} {'has' if len(unused) == 1 else 'have'} not been used for {PAIR_UNUSED_DAYS} days",
                     fix("If it is not needed, open Settings > Nodes and press Remove (a node this board calls) or Revoke (one that calls this board)"))
    return _pass(f"{len(rows)} paired node{'s' if len(rows) != 1 else ''}, all used within {PAIR_UNUSED_DAYS} days and with a token under {PAIR_TOKEN_DAYS} days old")


MCP_STALE_DAYS = 90         # a device token not used for this long is worth a look
MCP_EXPIRY_WARN_DAYS = 7     # a device token that expires within this many days is about to stop working


def _mcp_probe(url: str, user: str | None) -> int:
    """POST one initialize to the board's own /mcp the way a device with an allowed identity but no device token would: the HTTP status.
    OSError when the board could not be reached. No token of any kind is sent. Patched by tests (a fake middleware)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "ccboard-doctor"}}}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if user:
        headers["Tailscale-User-Login"] = user
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, data=body, method="POST", headers=headers), timeout=2.0) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except urllib.error.URLError as e:
        raise OSError(str(e.reason)) from e


def _c_mcp_remote(db) -> Outcome:
    """The remote MCP endpoint (issue #13/#14): off is a skip; on, the board probes its own /mcp with an allowed identity and no device token
    and fails when that gets in (identity alone must never open it); a probe that could not run is a warning, never a pass. Then: no public
    URL (Settings cannot offer the client commands), a token unused for 90 days, one expiring within 7 days or already expired."""
    from . import mcp_tokens
    to_settings = fix("Open Settings > Agents > Connect from another device to revoke or replace device tokens", action="mcp_settings")
    if not callable(getattr(db, "kv_get", None)) or not mcp_tokens.enabled(db):     # the board's own DB, as run() was given it
        return _skip("remote MCP is off: /mcp answers 404", None)
    user = settings.dev_bypass_user or next(iter(sorted(settings.allowed_users or ())), None)
    try:
        code = _mcp_probe(settings.loopback_url() + "/mcp", user)
    except OSError:
        return _warn("remote MCP is on, but the board could not probe its own /mcp, so whether identity alone opens it is unknown",
                     fix("Look again in a minute; if it stays, check that the board answers on its loopback port"))
    if 200 <= code < 300:
        return _fail("remote MCP is on and /mcp let in a request with an identity but no device token",
                     fix("Turn remote MCP off in Settings > Agents now, then update the board", action="mcp_settings"))
    if code == 404:
        return _warn("remote MCP was switched off while the doctor probed it; look again")
    if code != 401:
        return _warn(f"remote MCP is on; the probe answered {code}, so the token rule could not be proven"
                     + ("" if user else " (CCBOARD_ALLOWED_USERS is empty: there is no identity to probe with)"), to_settings)
    if not settings.public_url:
        return _warn("remote MCP is on, but CCBOARD_PUBLIC_URL is empty: Settings cannot show the client commands",
                     fix("Set CCBOARD_PUBLIC_URL in /etc/ccboard/env (the board's https address on the tailnet) and rerun ./install.sh"))
    now = datetime.now(timezone.utc)
    stale, ending = [], []
    tokens = mcp_tokens.listing(db)
    for t in tokens:
        def when(key):
            try:
                return datetime.fromisoformat(str(t.get(key) or "").replace("Z", "+00:00"))
            except ValueError:
                return None
        exp, used, made = when("expires_at"), when("last_used_at"), when("created_at")
        if t.get("expired") or (exp and exp - now <= timedelta(days=MCP_EXPIRY_WARN_DAYS)):
            ending.append(t.get("name") or "?")
        elif (used or made) and now - (used or made) >= timedelta(days=MCP_STALE_DAYS):
            stale.append(t.get("name") or "?")
    if ending or stale:
        parts = ([f"expiring or expired: {', '.join(ending[:5])}"] if ending else []) + ([f"unused for {MCP_STALE_DAYS} days: {', '.join(stale[:5])}"] if stale else [])
        return _warn(f"remote MCP is on and refuses identity alone; device tokens {'; '.join(parts)}", to_settings)
    n = len(tokens)
    return _pass(f"remote MCP is on; identity alone is refused (401); {n} device token{'s' if n != 1 else ''}")


def _uptime() -> float:
    """Seconds since this process imported the doctor (patched by tests): the Sampler's first heartbeat lands within seconds of that."""
    return time.monotonic() - _STARTED


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)                 # patched by tests


def _heartbeat_age(hb: dict) -> float | None:
    """Seconds since the kv row was written (its own `at`, the DB's clock); None when it carries no readable time."""
    try:
        at = datetime.fromisoformat(str(hb.get("at")).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return max(0.0, (_utcnow() - at).total_seconds())


def _c_samples(db) -> Outcome:
    """The Sampler thread writes kv samples_heartbeat every 15 s tick: pass under a minute, warn under ten, fail beyond (or never,
    once the board has been up a few seconds). Without a database handle there is nothing to read."""
    kv_get = getattr(db, "kv_get", None)
    if kv_get is None:
        return _skip("no database handle to read the heartbeat from")
    hb = kv_get(HEARTBEAT_KEY)
    age = _heartbeat_age(hb) if isinstance(hb, dict) else None
    restart = fix("Restart the board and read its log for 'sampler' errors (the time series stop accruing while this is red)")
    if age is None:
        if _uptime() < HEARTBEAT_GRACE:
            return _skip("the board has only just started; the sampler writes its first heartbeat within seconds")
        return _fail("the sampler has not written a heartbeat since the board started", restart)
    if age < HEARTBEAT_PASS:
        return _pass(f"the sampler last ticked {int(age)} s ago")
    mins = f"{int(age // 60)} min" if age >= 120 else f"{int(age)} s"
    if age < HEARTBEAT_WARN:
        return _warn(f"the sampler last ticked {mins} ago (it ticks every 15 s)", restart)
    return _fail(f"the sampler last ticked {mins} ago; usage history is not accruing", restart)


# ------------------------------------------------------------------ notify checks

def _loopback_base(url: str) -> str | None:
    try:
        u = urlsplit(url)
        host = (u.hostname or "").lower()
        port = u.port
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or host not in LOOPBACK_HOSTS:
        return None
    h = f"[{host}]" if ":" in host else host
    return f"{u.scheme}://{h}" + (f":{port}" if port else "")


def _c_ntfy(db) -> Outcome:
    if not settings.ntfy_url:
        return _skip("NTFY_URL is not set", fix("Set NTFY_URL (loopback) to enable ntfy phone pushes; install.sh sets it up"))
    base = _loopback_base(settings.ntfy_url)
    if base is None:
        return _warn("NTFY_URL is not a loopback http(s) URL, so it is not probed",
                     fix("Point NTFY_URL at the local ntfy server, e.g. http://127.0.0.1:2586, or send a test to see whether yours delivers", None, "notify_test"))
    try:
        status, body = _http_get(base + "/v1/health", timeout=2.0)
    except OSError:
        return _fail(f"ntfy does not answer at {base}", fix("Start the ntfy server", plat.hint("start", "ntfy")))
    healthy = None
    try:
        healthy = json.loads(body).get("healthy") if body else None
    except (ValueError, AttributeError):
        healthy = None
    if status >= 500 or healthy is False:
        return _warn(f"ntfy answers at {base} but reports a problem (HTTP {status})", fix("Check the ntfy service, then send a test", plat.hint("logs", "ntfy", flags="-n 30 --no-pager"), "notify_test"))
    return _pass(f"ntfy answers at {base}, topic '{settings.ntfy_topic}'")


def _c_push(db) -> Outcome:
    if db is None:
        return _skip("no database handle")
    if importlib.util.find_spec("pywebpush") is None:
        return _fail("pywebpush is not installed, so Web Push is off", fix("Install the board's requirements", "pip install -r requirements.txt"))
    try:
        n = len(db.push_subs())
    except Exception as e:
        return _warn(f"could not read the subscriptions ({e.__class__.__name__})", fix("Restart the board and read its log for database errors"))
    keys = push.key_path().exists()
    if n == 0:
        return _warn("no device is subscribed to Web Push", fix("Open the board on your phone or iPad and enable notifications in Settings"))
    return _pass(f"{n} subscription{'s' if n != 1 else ''}" + ("" if keys else "; the VAPID key is created on the next board start"))


# ------------------------------------------------------------------ claude checks

def _c_claude_bin(db) -> Outcome:
    exe = settings.claude_bin()
    if not exe:
        return _missing("claude")
    try:
        p = _run([exe, "--version"])
    except ToolMissing:
        return _missing("claude")
    v = _vstr(p.out)
    if p.rc != 0 or not v:
        return _warn("claude is installed but --version failed", fix("Run claude --version on the box", f"{exe} --version"))
    return _pass(f"claude {v} at {exe}")


def _c_claude_auth(db) -> Outcome:
    st = claude_auth.status()
    if not st.get("installed"):
        return _skip("claude is not installed")
    if st.get("error"):
        return _warn(str(st["error"]), fix("Run claude auth status on the box", "claude auth status"))
    if st.get("loggedIn"):
        try:
            prob = login_problem.get(db) if db is not None else None
        except Exception:                                      # a check never fails because the kv could not be read
            prob = None
        if prob and prob.get("agent") == "claude":             # `auth status` only sees that a file exists: a session's own failure is the word that counts
            when = str(prob.get("at") or "")[:16].replace("T", " ")
            return _warn(f"Claude reported its login invalid at {when + ' UTC' if when else 'some time ago'}: log in again in Settings",
                         fix("Log in again in Settings > Accounts", None, "claude_login"))
        bits = [b for b in (st.get("authMethod"), st.get("subscriptionType")) if isinstance(b, str) and b]
        return _pass("logged in" + (f" ({', '.join(bits)})" if bits else ""))
    return _fail("claude is not logged in", fix("Log in from the board, or run claude auth login on the box", "claude auth login", "claude_login"))


def _hook_commands(hooks) -> list[str]:
    out: list[str] = []
    if isinstance(hooks, dict):
        for groups in hooks.values():
            for g in groups if isinstance(groups, list) else []:
                for h in (g.get("hooks") if isinstance(g, dict) else None) or []:
                    if isinstance(h, dict) and isinstance(h.get("command"), str):
                        out.append(h["command"])
    return out


def _c_claude_hooks(db) -> Outcome:
    path = Path(settings.claude_config_dir) / "settings.json"
    app_dir = _app_dir()
    install = fix("Merge the board's hooks and statusLine into Claude's settings (idempotent)",
                  f"python3 {app_dir / 'scripts' / 'claude_settings.py'} install --app-dir {app_dir}")
    try:
        data = json.loads(path.read_text() or "{}")
    except FileNotFoundError:
        return _fail("Claude's settings.json does not exist, so no hooks are installed", install)
    except (OSError, ValueError):
        return _fail("Claude's settings.json is unreadable or not valid JSON", fix("Fix settings.json, then reinstall the hooks"))
    if not isinstance(data, dict):
        return _fail("Claude's settings.json is not a JSON object", fix("Fix settings.json, then reinstall the hooks"))
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    ours = {ev for ev, groups in hooks.items()
            if any(HOOK_MARK in c for c in _hook_commands({ev: groups}))}
    if not ours:
        return _fail("the board's hooks are not installed in Claude's settings.json", install)
    sl = data.get("statusLine")
    sl_cmd = sl.get("command") if isinstance(sl, dict) else None
    sl_ours = isinstance(sl_cmd, str) and STATUS_MARK in sl_cmd
    missing = [e for e in HOOK_EVENTS if e not in ours]
    if missing:
        return _warn(f"hooks installed but missing for: {', '.join(missing)}", install)
    if not sl_ours:
        return _warn("hooks installed, but the statusLine is not the board's, so per-session context and usage numbers will not arrive",
                     fix("Remove your statusLine from Claude's settings.json and reinstall, or accept the loss"))
    return _pass(f"hooks for {len(ours)} events and the statusLine are installed")


HELPER_TIMEOUT = 3.0          # per probe of the hook helpers (curl --version, python3 -c pass)
LOGIN_PATH_TIMEOUT = 1.5      # reading the login shell's PATH; below CHECK_TIMEOUT together with the probes (they run side by side)
HELPER_EFFECT = "the permission hook falls back to the terminal prompt and the statusline shows nothing"


def _login_path() -> str:
    """PATH of the account's login shell, or '' when it cannot be read in LOGIN_PATH_TIMEOUT. A hook started from a login shell sees it; one
    started by the service sees only the board's own. Only the last line counts: a profile may print a banner. Tests patch this."""
    try:
        p = _run([plat.login_shell(), "-lc", 'printf "\\n%s" "$PATH"'], timeout=LOGIN_PATH_TIMEOUT)
    except (ToolMissing, ToolTimeout):
        return ""
    last = p.out.rstrip("\n").rsplit("\n", 1)[-1] if p.rc == 0 else ""
    return last if "/" in last else ""


def _hook_path() -> str:
    """The directories a hook may find curl and python3 in: the board's own PATH, then the login shell's, each directory once."""
    seen: list[str] = []
    for d in (os.environ.get("PATH", "") + os.pathsep + _login_path()).split(os.pathsep):
        if d and d not in seen:
            seen.append(d)
    return os.pathsep.join(seen)


def _probe_helper(name: str, tail: list[str], path: str) -> str:
    """'ok' | 'missing' | 'broken' (ran, exited non-zero) | 'timeout' for `name` looked up on `path` and run with `tail`."""
    exe = shutil.which(name, path=path)
    if not exe:
        return "missing"
    try:
        p = _run([exe, *tail], timeout=HELPER_TIMEOUT)
    except ToolMissing:
        return "missing"
    except ToolTimeout:
        return "timeout"
    return "ok" if p.rc == 0 else "broken"


def _helper_install(tool: str) -> dict:
    """The fix for a missing helper, through the platform hint (apt-get on Debian and Ubuntu, brew on a Mac); the Homebrew formula for
    python3 is `python`."""
    pkg = "python" if tool == "python3" and plat.IS_MACOS else tool
    return fix(f"Install {tool}", plat.hint("install", pkg))


def _c_hook_helpers(db) -> Outcome:
    """Issue #121: bin/ccboard-hook, -permission and -statusline call `curl`, the last two also `python3 -c`, and all of them fail soft, so a
    missing helper is silent. Runs both under the PATH a hook would see, each with a 3 s limit (side by side, inside the check's 5 s cap)."""
    from concurrent.futures import ThreadPoolExecutor
    path = _hook_path()
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_curl = ex.submit(_probe_helper, "curl", ["--version"], path)
        f_py = ex.submit(_probe_helper, "python3", ["-c", "pass"], path)
        curl, py = f_curl.result(), f_py.result()
    if curl == py == "ok":
        return _pass("curl and python3 both run under the PATH the hooks see")
    if plat.IS_MACOS and py in ("broken", "timeout") and _xcode_stub():      # the stub fails, or stalls on its install dialog
        py = "stub"
    bad_curl, bad_py = curl in ("missing", "broken"), py in ("missing", "broken", "stub")
    if bad_curl or bad_py:
        reasons, fixes = [], []
        if bad_curl:
            reasons.append("curl is not found" if curl == "missing" else "curl does not run")
            fixes.append(_helper_install("curl"))
        if bad_py:
            if py == "stub":
                reasons.append("python3 is the Command Line Tools stub, not a real Python")
                fixes.append(fix(f"Install the Command Line Tools, or Homebrew Python ({plat.hint('install', 'python')})",
                                 "xcode-select --install"))
            else:
                reasons.append("python3 is not found" if py == "missing" else "python3 does not run")
                fixes.append(_helper_install("python3"))
        effect = ("no hook can reach the board, and " if bad_curl else "") + HELPER_EFFECT
        return Outcome("fail" if bad_curl else "warn", f"{' and '.join(reasons)}: {effect}", fixes[0])
    slow = [n for n, r in (("curl --version", curl), ("python3 -c pass", py)) if r == "timeout"]
    return _warn(f"unknown: {' and '.join(slow)} did not answer within {int(HELPER_TIMEOUT)} s, so the hook helpers could not be judged",
                 fix("Run curl --version and python3 -c pass in a terminal"))


def _xcode_stub() -> bool:
    """macOS: `xcode-select -p` fails, so /usr/bin/python3 is the Command Line Tools stub and no real developer directory exists."""
    try:
        return _run(["xcode-select", "-p"], timeout=HELPER_TIMEOUT).rc != 0
    except (ToolMissing, ToolTimeout):
        return False


# ------------------------------------------------------------------ memory checks (claude-mem)

MEM_GROUP = "memory"
MEM_QUEUE_WARN = 200              # queued observations above this: the observer is behind
MEM_ERROR_WINDOW = 3600.0         # a provider error newer than this (seconds) is a warning
MEM_BUN_DIRS = ("/usr/local/bin", "/opt/homebrew/bin", "/home/linuxbrew/.linuxbrew/bin", "/usr/bin", "/snap/bin")   # = bin/ccboard-mem-run
MEM_PLUGIN_ADD = "claude plugin marketplace add thedotmack/claude-mem && claude plugin install claude-mem@thedotmack"
MEM_BEHIND = "the observer is behind: it shares your Claude subscription window"
MEM_IDS = (("memory-plugin", "claude-mem plugin"), ("memory-worker", "claude-mem worker"), ("memory-queue", "Observer queue"),
           ("memory-error", "Observer provider errors"), ("memory-projects", "Project keys"), ("memory-env", "Worker environment"),
           ("memory-bun", "bun runtime"), ("memory-api", "Memory page API"), ("memory-viewer", "claude-mem viewer"))
_MEM_PORT_SOURCE = {"env": "CCBOARD_MEM_PORT", "worker.pid": "worker.pid", "settings": "settings.json", "default": "default"}


def _mem_where(h: dict) -> str:
    return f"127.0.0.1:{h.get('port')} ({_MEM_PORT_SOURCE.get(h.get('port_source'), '?')})"


def _ago(seconds: float) -> str:
    s = int(max(0.0, seconds))
    return f"{s // 3600} h {s % 3600 // 60} min" if s >= 3600 else f"{s // 60} min" if s >= 120 else f"{s} s"


def _mem_plugin(db, ctx: dict) -> Outcome:
    pl = ctx["plugin"] = memory.plugin_status()
    if not pl["installed"]:
        return _warn("the claude-mem plugin is not installed, so Claude sessions keep no memory",
                     fix("Install the plugin for Claude (the installer does this too)", MEM_PLUGIN_ADD))
    ver = f" {pl['version']}" if pl["version"] else ""
    if pl["enabled"] is False:
        return _warn(f"claude-mem{ver} is installed but disabled in Claude's settings",
                     fix("Enable the plugin", f"claude plugin enable {memory.PLUGIN_KEY}"))
    return _pass(f"claude-mem{ver} installed" + (" and enabled" if pl["enabled"] else ""))


def _mem_health(ctx: dict) -> dict:
    if "health" not in ctx:
        ctx["health"] = memory.health()
    return ctx["health"]


def _mem_worker(db, ctx: dict) -> Outcome:
    h = _mem_health(ctx)
    where = _mem_where(h)
    if h["state"] == "up":
        bits = [f"claude-mem {h['version']}" if h.get("version") else "claude-mem", f"is up on {where}"]
        tail = f"{h['observations']:,} observations" if isinstance(h.get("observations"), int) else ""
        return _pass(" ".join(bits) + (f", {tail}" if tail else ""))
    if h["state"] == "degraded":
        return _warn(f"{where}: {h.get('reason') or 'the worker is not answering'}",
                     fix("Give it a minute; if it stays stuck, stop the worker (its pid is in ~/.claude-mem/worker.pid) and start any Claude session"))
    return _warn(f"no worker on {where}: {h.get('reason') or 'connection refused'}",
                 fix("Start any Claude session: the plugin's hooks start the worker. With CLAUDE_MEM_WORKER_AUTOSTART=false only ccboard-mem.service "
                     f"does: {plat.hint('status', 'ccboard-mem', sudo=False)}"))


def _mem_queue(db, ctx: dict) -> Outcome:
    h = _mem_health(ctx)
    if h["state"] != "up":
        return _skip("the worker is not up")
    depth = h.get("queue_depth")
    if depth is None:
        return _pass("the worker does not report a queue depth")
    busy = ", processing" if h.get("processing") else ", idle"
    if depth > MEM_QUEUE_WARN:
        return _warn(f"{depth:,} observations are queued{busy}; {MEM_BEHIND}",
                     fix("Let it drain, or stop the worker while you need the whole window; it starts again with the next session"))
    return _pass(f"{depth:,} observation{'s' if depth != 1 else ''} queued{busy}")


def _mem_time(v) -> datetime | None:
    """An ISO time as an aware UTC datetime, or None for anything else."""
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None
    except ValueError:
        return None
    return t.replace(tzinfo=timezone.utc) if t is not None and t.tzinfo is None else t


def _mem_error(db, ctx: dict) -> Outcome:
    e = _mem_health(ctx).get("last_error")
    if not e:
        return _pass("no observer provider error recorded")
    at = _mem_time(e.get("at"))
    if at is None:
        return _pass("an observer provider error is on record, without a time")
    age = max(0.0, (_utcnow() - at).total_seconds())
    who = f" ({e['provider']})" if e.get("provider") else ""
    if age > MEM_ERROR_WINDOW:
        return _pass(f"the last observer provider error was {_ago(age)} ago{who}")
    # a transient error the observer has since got past is not a problem: no failures counted now and a success after the error
    ok = _mem_time(e.get("last_success_at"))
    if not e.get("failures") and ok is not None and ok >= at:
        return _pass(f"the observer's provider failed {_ago(age)} ago{who} and has recovered (a success followed)")
    still = " and is still failing" if e.get("failures") else ""
    return _warn(f"the observer's provider failed {_ago(age)} ago{who}{still}: {e.get('message') or 'no message'}",
                 fix("The observer uses your Claude subscription and retries once the allowance is back; nothing to do unless it keeps failing"))


def _repo_dirs_by_name() -> dict[str, list[str]]:
    """claude-mem keys memories by the folder name a session starts in: basename -> ['project/repo', ...] for every folder
    under PROJECTS_DIR that scan() lists as a repo. Cheap on purpose (no git): a doctor check has 5 s."""
    out: dict[str, list[str]] = {}
    for pdir in projects._subdirs(Path(settings.projects_dir)):
        repos = [pdir] if projects.is_repo(pdir) else [c for c in projects._subdirs(pdir) if c.name != projects.ROOT]
        for r in repos:
            out.setdefault(r.name, []).append(pdir.name if r == pdir else f"{pdir.name}/{r.name}")
    return out


def mem_env_snippet(dup: dict[str, list[str]]) -> str:
    """The opt-in CLAUDE_MEM_PROJECT_ENVIRONMENTS entry for ~/.claude-mem/settings.json that gives each colliding repo its own key:
    {name: '<project>-<repo>', patterns: ['<PROJECTS_DIR>/<project>/<repo>/**']}. The shape is the plugin's own (project-environments.ts,
    docs/public/configuration.mdx; box check V11 row 13: a JSON array of {name, patterns[]}, as a string or a native array). ccboard
    never writes this setting; it only shows it."""
    entries = [{"name": o.replace("/", "-"), "patterns": [f"{Path(settings.projects_dir) / o}/**"]}
               for _k, owners in sorted(dup.items()) for o in owners]
    return '"CLAUDE_MEM_PROJECT_ENVIRONMENTS": ' + json.dumps(entries)


def _mem_projects(db, ctx: dict) -> Outcome:
    if not Path(settings.projects_dir).is_dir():
        return _skip("the projects directory does not exist")
    dup = {k: v for k, v in _repo_dirs_by_name().items() if len(v) > 1}
    if not dup:
        return _pass("every repo has its own claude-mem project key")
    shown = "; ".join(f"'{k}' = {' and '.join(v[:3])}" for k, v in sorted(dup.items())[:2])
    more = f" (+{len(dup) - 2} more)" if len(dup) > 2 else ""
    return _warn(f"repos that share a folder name share one claude-mem project, so their memories merge: {shown}{more}",
                 fix("Rename one folder of each pair (claude-mem keys memories by the folder name), or give each its own key: add this "
                     "entry to ~/.claude-mem/settings.json (new sessions use it; older memories keep the shared key, and the Memory page "
                     "filters them by file path, best effort)", mem_env_snippet(dup)))


MEM_API_FIX = "the Memory page may be wrong until the board is updated; report it"


def _mem_api_sample() -> tuple[int, object]:
    """One cheap live read for the memory-api check: GET /api/observations?limit=1 (the shape only; the body is never logged)."""
    from . import memory_proxy
    return memory.fetch(memory.worker_base(), memory_proxy._url("observations", {memory_proxy.P_LIMIT: 1}), timeout=memory.TIMEOUT,
                        cap=memory_proxy.PROXY_CAP, strict=True)


def _mem_api(db, ctx: dict) -> Outcome:
    """The worker's API against what the Memory proxy was verified with (memory_proxy.TESTED_WORKER, issue #20): the running version
    and one `limit=1` observations read checked with the proxy's own shape check."""
    from . import memory_proxy
    h = _mem_health(ctx)
    if h["state"] != "up":
        return _skip("the worker is not up")
    ver, tested = h.get("version"), memory_proxy.TESTED_WORKER
    try:
        status, body = _mem_api_sample()
    except Exception as e:
        return _warn(f"could not read a sample from the worker ({e.__class__.__name__}); the Memory page may show it as slow", fix(MEM_API_FIX))
    problem = f"HTTP {status}" if status != 200 else memory_proxy.check_shape("observations", body)
    if problem:
        return _warn(f"claude-mem {ver or '(version unknown)'} answers in a shape this board does not know: {problem}", fix(MEM_API_FIX))
    c = memory_proxy.compat(ver)
    if c == "untested":
        return _warn(f"claude-mem {ver} is newer than {tested}, the version this board was tested with; a sample read fine", fix(MEM_API_FIX))
    if c == "unknown":
        return _warn(f"claude-mem {ver or 'of an unknown version'} is not the {tested.split('.')[0]}.x line this board was tested with",
                     fix(MEM_API_FIX))
    return _pass(f"claude-mem {ver} answers in the shapes this board was tested with ({tested})")


MEM_ENV_FILE = "mem-env.json"              # <data dir>/mem-env.json, written by scripts/ccboard-mem-env on the host (names only)
MEM_ENV_FRESH_S = 600                      # the host watchdog runs every 2 minutes; ten minutes without a report is stale


def _mem_env_report(pid) -> tuple[list[str] | None, str]:
    """(names, note) from the host's <data dir>/mem-env.json for worker `pid`: the variable NAMES the host watchdog saw in the worker's
    environment. names is None when the file is missing, malformed, older than 10 minutes or about another pid; `note` then says which."""
    try:
        with open(Path(settings.data_dir) / MEM_ENV_FILE, "rb") as f:
            raw = f.read(65537)
        d = json.loads(raw[:65536].decode("utf-8", "replace")) if len(raw) <= 65536 else None
    except (OSError, ValueError):
        d = None
    if not isinstance(d, dict) or not isinstance(d.get("names"), list):
        return None, "none yet"
    if d.get("pid") != pid:
        return None, "other worker pid"
    try:
        at = datetime.fromisoformat(str(d.get("at")).replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        age = (_utcnow() - at).total_seconds()
    except ValueError:
        return None, "no readable time"
    if age > MEM_ENV_FRESH_S or age < -60:
        return None, "stale"
    return [n for n in memory.ENV_LEAK_VARS if n in {x for x in d["names"] if isinstance(x, str)}], f"{max(0, int(age // 60))} min ago"


def _mem_env(db, ctx: dict) -> Outcome:
    h = _mem_health(ctx)
    pid = h.get("pid")
    if not pid:
        return _skip("no worker process to inspect")
    leaks = memory.env_leaks(pid)
    note = ""
    if leaks is None:
        # The board in a container cannot read the host worker's /proc/<pid>/environ; the host watchdog does and leaves the NAMES in
        # <data dir>/mem-env.json (scripts/ccboard-mem-env). Under systemd the live read above is the one that counts.
        leaks, why = _mem_env_report(pid)
        if leaks is None:
            if settings.runtime == "docker":
                return _skip(f"could not read the worker's environment: a container cannot see the host's /proc; the host watchdog has not "
                             f"reported (is the crontab line installed?) - {why}",
                             fix("The host-side script scripts/ccboard-mem-env runs from the watchdog's crontab line (every 2 minutes) and "
                                 "reports the worker's variable names, never values. Check the line is there, then run it once",
                                 "crontab -l | grep ccboard-watchdog"))
            if not plat.IS_LINUX:
                return _skip("could not read the worker's environment (another user's process, or psutil is not installed)")
            return _skip("could not read the worker's environment (another user's process, or no /proc here)")
        note = f" (reported by the host watchdog {why})"
    if leaks:
        return _warn(f"the memory worker inherited a session's environment; install ccboard-mem.service (it holds {', '.join(leaks)})",
                     fix("Install the unit, then hand over: the plugin's `worker-service.cjs stop`, then "
                         f"`{plat.hint('start', 'ccboard-mem')}`. An update undoes it unless CLAUDE_MEM_WORKER_AUTOSTART=false (README, claude-mem)",
                         "CCBOARD_MEM_SERVICE=1 ./install.sh"))
    return _pass("the worker's environment holds no ccboard session variables" + note)


def _executable(p) -> bool:      # patched by tests (a dev box has a real bun in /usr/local/bin)
    return bool(p) and os.path.isfile(p) and os.access(p, os.X_OK)


def _mem_find_bun() -> tuple[str | None, list[str]]:
    """(the first bun found, the places looked at): the order of bin/ccboard-mem-run, which follows the plugin's own search
    (CCBOARD_MEM_BUN is the launcher's override; the plugin reads BUN, BUN_PATH, BUN_INSTALL, ~/.bun/bin and the system directories)."""
    env = os.environ
    first = [env.get("CCBOARD_MEM_BUN"), env.get("BUN"), env.get("BUN_PATH")]
    if env.get("BUN_INSTALL"):
        first += [os.path.join(env["BUN_INSTALL"], "bin", "bun"), os.path.join(env["BUN_INSTALL"], "bun")]
    first.append(str(Path.home() / ".bun" / "bin" / "bun"))
    raw_dirs = env.get("CCBOARD_MEM_BUN_DIRS")               # set (even empty) replaces the default list, as in the launcher
    dirs = list(MEM_BUN_DIRS) if raw_dirs is None else raw_dirs.split()
    where = ["CCBOARD_MEM_BUN", "BUN", "BUN_PATH", "BUN_INSTALL", "~/.bun/bin", *dirs, "PATH"]
    for p in first:
        if _executable(p):
            return p, where
    for d in dirs:
        if _executable(os.path.join(d, "bun")):
            return os.path.join(d, "bun"), where
    w = _which("bun")
    return (w if w and os.path.isabs(w) else None), where


def _mem_bun(db, ctx: dict) -> Outcome:
    """The plugin's worker runs on bun and nothing here installs it: bun-runner.js exits 1 ("Bun not found") and the hooks' lazy
    spawn of a worker refuses without it (plugin 13.29.0). A missing bun only matters while no worker is running."""
    exe, where = _mem_find_bun()
    if exe:
        return _pass(f"bun found at {exe}; the plugin's worker runs on it")
    if _mem_health(ctx)["state"] != "down":
        return _pass("bun is not visible from here, but a worker is running, so the plugin found its own (the board may see another filesystem)")
    return _warn("bun not found and no worker is running, so hooks cannot start one; looked in " + ", ".join(where[4:]),
                 fix("The plugin runs on bun and installs none: install it (https://bun.sh) or point BUN at it. Also read: BUN_PATH, BUN_INSTALL, "
                     "and CCBOARD_MEM_BUN for ccboard-mem.service"))


MEM_VIEWER_OPEN = "every device on your tailnet can open and change the claude-mem worker"


def _mem_serve_web() -> dict | None:
    """`tailscale serve status --json` -> its Web map, {} when nothing is served, None when it cannot be read (no tailscale, no socket,
    not JSON). Read only; the doctor never changes a mapping. Tests patch this."""
    from . import previews
    try:
        p = _run(previews.serve_cmd("status", "--json"))
    except (ToolMissing, ToolTimeout):
        return None
    if p.rc != 0:
        return None
    try:
        d = json.loads(p.out.strip() or "null")
    except ValueError:
        return None
    if d is None:
        return {}
    if not isinstance(d, dict):
        return None
    web = d.get("Web")
    return web if isinstance(web, dict) else {}


def _mem_viewer(db, ctx: dict) -> Outcome:
    """The optional claude-mem viewer on the tailnet (CCBOARD_MEM_HTTPS_PORT, issue #9). Not set: skip. Set: the worker has no sign-in of its
    own and its API can write, so a mapping that points at the current worker port is a warning that says who can reach it; a missing or
    stale mapping is a warning with the installer as the fix; a mapping that cannot be read is reported as unknown, never as healthy."""
    port = settings.mem_https_port
    if not port:
        return _skip("not set: the claude-mem worker is not exposed on the tailnet (CCBOARD_MEM_HTTPS_PORT is empty)")
    web = _mem_serve_web()
    if web is None:
        return _warn(f"unknown: could not read the tailscale serve mapping for port {port}; if it exists, {MEM_VIEWER_OPEN}",
                     fix("Check that tailscale is running; to remove the viewer, clear the setting", "CCBOARD_MEM_HTTPS_PORT=off ./install.sh"))
    want = f"http://127.0.0.1:{memory.discover()['port']}"
    handlers = None
    for key, val in web.items():
        if str(key).rsplit(":", 1)[-1] == str(port) and isinstance(val, dict):
            handlers = val.get("Handlers") or {}
    root = handlers.get("/") if isinstance(handlers, dict) else None
    proxy = root.get("Proxy") if isinstance(root, dict) else None
    if not proxy:
        return _warn(f"CCBOARD_MEM_HTTPS_PORT={port} is set but nothing is served on that port",
                     fix("Run the installer to add the mapping, or clear the setting", "./install.sh"))
    if proxy != want:
        return _warn(f"the viewer mapping on port {port} points at {proxy}, not the worker at {want}",
                     fix("Run the installer to point it at the current worker", "./install.sh"))
    return _warn(f"claude-mem's viewer is open on tailnet port {port}: {MEM_VIEWER_OPEN}",
                 fix("Clear the setting to close it, or restrict the port with a Tailscale ACL", "CCBOARD_MEM_HTTPS_PORT=off ./install.sh"))


_MEM_FNS = {"memory-plugin": _mem_plugin, "memory-worker": _mem_worker, "memory-queue": _mem_queue, "memory-error": _mem_error,
            "memory-projects": _mem_projects, "memory-env": _mem_env, "memory-bun": _mem_bun, "memory-api": _mem_api,
            "memory-viewer": _mem_viewer}


def memory_checks(db) -> list[Check]:
    """The `memory` group, as one provider: a single probe of the worker feeds every check (a check each probing would be
    four requests per run). CCBOARD_CLAUDE_MEM=0 skips them all; without the plugin only the plugin check speaks. One check
    that raises becomes its own warning and never takes the group down."""
    if not settings.claude_mem:
        return [Check(cid, MEM_GROUP, label, "skip", "claude-mem is turned off (CCBOARD_CLAUDE_MEM=0)") for cid, label in MEM_IDS]
    ctx: dict = {}
    out: list[Check] = []
    for cid, label in MEM_IDS:
        if cid != "memory-plugin" and not ctx.get("plugin", {}).get("installed"):
            out.append(Check(cid, MEM_GROUP, label, "skip", "the claude-mem plugin is not installed"))
            continue
        try:
            st, detail, f = _MEM_FNS[cid](db, ctx)
        except Exception as e:
            st, detail, f = "warn", f"check error: {e.__class__.__name__}", None
        out.append(Check(cid, MEM_GROUP, label, st, detail, f))
    return out


# ------------------------------------------------------------------ codex checks (the adapter's, v0.5.11)

CODEX_GROUP = "codex"


def codex_checks(db) -> list[Check]:
    """The `codex` group, as one provider: whatever the Codex adapter reports (version, login, hooks.json, hook trust mode,
    --no-alt-screen support, features, sessions and state database, the ccboard MCP server, repo-level hooks). Codex is optional:
    the adapter answers `skip` for every check on a box without the binary (so the report stays ok), and a build without the adapter
    has no content in this group at all."""
    from . import agents                       # lazy: the adapter package imports config/projects/claude_auth, and tests swap it out
    try:
        ag = agents.get("codex")
    except KeyError:
        return []
    return list(ag.doctor_checks())


# ------------------------------------------------------------------ registry and runner

CHECKS: list[tuple[str, str, str, Callable]] = []
PROVIDERS: list[tuple[str, str, Callable]] = []


def register_provider(name: str, group: str, fn: Callable) -> None:
    """Add (or replace) a provider: fn(db) -> list of Check-likes (doctor.Check, app.agents.base.Check or dicts with
    id, group, label, status, detail, fix). It runs as one unit under the per-check cap; a check it returns replaces a
    registered check with the same id. This is how an adapter contributes its checks, e.g.
    register_provider('codex', 'codex', lambda db: agents.get('codex').doctor_checks())."""
    PROVIDERS[:] = [p for p in PROVIDERS if p[0] != name]
    PROVIDERS.append((name, group, fn))
    if group not in GROUPS:
        GROUPS.append(group)


def register(check_id: str, group: str, label: str, fn: Callable) -> None:
    """Add (or replace) a check. fn(db) -> Outcome. A new group name becomes a valid `group` argument of run()."""
    CHECKS[:] = [c for c in CHECKS if c[0] != check_id]
    CHECKS.append((check_id, group, label, fn))
    if group not in GROUPS:
        GROUPS.append(group)


for _id, _group, _label, _fn in (
    ("tmux", "terminal", "tmux 3.2 or newer", _c_tmux),
    ("tmux-server", "terminal", "tmux server", _c_tmux_server),
    ("tmux-conf", "terminal", "tmux.conf applied", _c_tmux_conf),
    ("tmux-socket", "terminal", "tmux socket", _c_tmux_socket),
    ("ttyd", "terminal", "ttyd (web terminal)", _c_ttyd),
    ("attach-wrapper", "terminal", "ccboard-attach wrapper", _c_attach_wrapper),
    ("code-server", "box", "code-server", _c_code_server),
    ("projects-dir", "box", "Projects directory", _c_projects_dir),
    ("git", "box", "git 2.15 or newer", _c_git),
    ("runtime-host", "box", "Container host", _c_runtime_host),
    ("gh", "box", "GitHub CLI", _c_gh),
    ("ccusage", "box", "ccusage", _c_ccusage),
    ("identity", "box", "Identity and access", _c_identity),
    ("samples-heartbeat", "box", "Usage samples heartbeat", _c_samples),
    ("mcp-remote", "box", "MCP from other devices", _c_mcp_remote),
    ("ntfy", "notify", "ntfy server", _c_ntfy),
    ("push", "notify", "Web Push subscriptions", _c_push),
    ("claude-bin", "claude", "Claude Code binary", _c_claude_bin),
    ("claude-auth", "claude", "Claude login", _c_claude_auth),
    ("claude-hooks", "claude", "Claude hooks and statusLine", _c_claude_hooks),
):
    register(_id, _group, _label, _fn)
del _id, _group, _label, _fn
register("hook-helpers", "claude", "Hook helpers (curl, python3)", _c_hook_helpers)   # issue #121
register("tailscale", "box", "Tailscale", _c_tailscale)   # issue #126
register("devcontainer", "box", "Devcontainer prerequisites (host)", _c_devcontainer)   # issue #104
register("node", "box", "This board as a node", _c_node)   # issue #133
register("tailscale-status", "box", "Tailnet for finding nodes", _c_tailscale_status)   # issue #134
register("nodes-port", "box", "Node probe ports", _c_nodes_port)   # issue #134
register("nodes-tagged-self", "box", "This device is tagged", _c_nodes_tagged_self)   # issue #134
register("nodes-pairs", "box", "Paired nodes", _c_nodes_pairs)   # issue #135
register_provider("memory", MEM_GROUP, memory_checks)       # claude-mem (v0.5.10): one probe, seven checks
register_provider("codex", CODEX_GROUP, codex_checks)       # the Codex adapter's checks (v0.5.11)
if plat.IS_MACOS:                                            # issue #117: the macOS checks (app/doctor_macos.py) exist on a Mac only; a Linux board lists none
    from . import doctor_macos as _doctor_macos              # noqa: F401  (registers its checks when it is imported on a Mac)
if plat.IS_MACOS or plat.is_wsl():                           # issue #154: the node-* checks (app/doctor_nodes.py) exist on a Mac and in WSL only; a Linux board lists none
    from . import doctor_nodes as _doctor_nodes              # noqa: F401
if plat.is_wsl():                                            # issue #118: the wsl- checks (app/doctor_wsl.py) exist inside WSL only; any other board lists none
    from . import doctor_wsl as _doctor_wsl                  # noqa: F401  (registers its checks, and the Sampler hook that records the distro's starts, when imported in WSL)


def _c_codex_saved_models(db) -> Outcome:
    """#17: a Codex schedule whose saved model the live catalogue (`codex debug models`) no longer lists as visible (gpt-5.5 leaves Codex on
    2026-10-14). Judged only against a catalogue Codex actually answered: the built-in fallback is not a reading, so an unknown catalogue
    is a skip, never a failure. Launcher defaults live in the browser and tasks carry no model, so schedules are what the board can see."""
    from . import agents
    try:
        live = agents.get("codex").live_models()
    except (KeyError, AttributeError):
        return _skip("no Codex adapter")
    if live is None:
        return _skip("the Codex model list has not been read yet (it is read in the background once Codex is installed)")
    visible = {str(m.get("slug")) for m in live}
    stale = []
    for j in (db.jobs() if db is not None else []):
        model = (j.get("opts") or {}).get("model") if isinstance(j.get("opts"), dict) else None
        if j.get("agent") == "codex" and isinstance(model, str) and model and model not in visible:
            stale.append(f"{j.get('name') or 'schedule ' + str(j.get('id'))} ({model})")
    if stale:
        return _warn(f"{len(stale)} Codex schedule{'s' if len(stale) != 1 else ''} name{'' if len(stale) != 1 else 's'} a model Codex no longer "
                     f"lists: {', '.join(stale[:5])}", fix("pick another model"))
    return _pass(f"every Codex schedule names a listed model ({len(visible)} listed)")


register("codex-saved-models", CODEX_GROUP, "Saved Codex models", _c_codex_saved_models)


def _c_codex_cred_store(db) -> Outcome:
    """Where Codex keeps its login and whether the board can save and switch it (issue #119): the `cli_auth_credentials_store` key of
    config.toml (read by codex_accounts.credential_store(), which never opens auth.json) and whether auth.json exists. Every system."""
    from . import codex_accounts
    try:
        if codex_accounts.support_reason() == codex_accounts.REASON_NOT_INSTALLED:
            return _skip("codex is not installed")
        cred = codex_accounts.credential_store()
    except Exception as e:                                      # a check never fails because of what it looked at
        return _warn(f"check error: {e.__class__.__name__}")
    if not cred.file:
        said = {"keyring": "Codex keeps its login in the system keyring", "ephemeral": "Codex does not keep its login on disk",
                "auto": "there is no auth.json, so the login may be in the keyring"}[cred.setting]
        return _warn(f"cli_auth_credentials_store is {cred.setting}: {said}, so saved Codex logins are off",
                     fix('Set cli_auth_credentials_store = "file" in config.toml in the Codex folder, then log in to Codex again'))
    where = fix("Codex keeps its login in auth.json in the Codex folder; the board saves and switches copies of it")
    if cred.setting == "file":
        return _pass("cli_auth_credentials_store is file: the Codex login is auth.json" + ("" if cred.auth_file else " (not made yet)"), where)
    if cred.setting == "auto":
        return _pass("cli_auth_credentials_store is auto and auth.json exists: saved Codex logins can be switched", where)
    if cred.auth_file:
        return _pass("cli_auth_credentials_store is not set and auth.json exists: saved Codex logins can be switched", where)
    return _pass("cli_auth_credentials_store is not set and there is no auth.json yet: a first codex login shows where Codex keeps it (to verify)")


register("codex-cred-store", CODEX_GROUP, "Codex login store", _c_codex_cred_store)


def _home_short(p) -> str:
    """A path with the home folder written as ~ (a detail line, not a credential: it names a folder, never opens it)."""
    s = str(p)
    home = str(Path.home())
    return "~" + s[len(home):] if home and s.startswith(home) else s


def _keychain_run(argv: list[str]) -> tuple[int, str]:
    """claude_auth.keychain_item()'s seam on the doctor's own _run: the exit status and stdout of an attribute-only `security` call."""
    p = _run(argv, CMD_TIMEOUT)
    return p.rc, p.out


def _c_claude_creds_store(db) -> Outcome:
    """macOS only (issue #119): where Claude Code's login lives, keychain, file, both or none. The Keychain half is an attribute-only
    `security find-generic-password -s "Claude Code-credentials"` (never -w or -g: the secret is not asked for, not seen, not kept; the answer is
    the words present or absent). A call that failed or timed out is "unknown", never "none". The board never deletes the file."""
    if not plat.IS_MACOS:
        return _skip("macOS only: Claude keeps its login in a file on this system")
    item = claude_auth.keychain_item(run=_keychain_run)
    path = settings.claude_config_dir / ".credentials.json"
    has_file = path.is_file()
    where = _home_short(path)
    if item["state"] == "unknown":
        return _warn(f"unknown: the Keychain could not be read; {where} " + ("exists" if has_file else "does not exist"),
                     fix('Run security find-generic-password -s "Claude Code-credentials" in Terminal: it prints attributes, never the value'))
    kc = item["state"] == "present"
    if kc and has_file:
        return _warn(f"both: the login is in the Keychain and in {where}; the file is probably a stale second copy",
                     fix(f"The live login is the Keychain item; {where} is only written when the Keychain refuses a write. Delete it yourself if unwanted: the board never does"))
    if kc:
        return _pass("keychain: the login is in the macOS Keychain, which the board does not swap, so saved Claude logins are off",
                     fix("It lives in the login Keychain as the item Claude Code-credentials (Keychain Access shows it)"))
    if has_file:
        return _pass(f"file: the login is in {where}; the Keychain has no item", fix(f"Claude Code wrote the login to {where}"))
    return _warn(f"none: no Claude login in the Keychain and no {where}",
                 fix("Log in from the board, or run claude auth login on this Mac", "claude auth login", "claude_login"))


if plat.IS_MACOS:                                                # issue #119: macOS only; a Linux board lists no such row
    register("claude-creds-store", "claude", "Claude login store", _c_claude_creds_store)


# ------------------------------------------------------------------ backup, CI and version-gate checks (issues #49, #50, #51, #113)
# All cheap: they read files and the project scan the board already holds, call a tool only through _run() under a timeout, never run restic,
# and the GitHub question is cached for five minutes and asked on a background probe (the doctor page never waits on GitHub).

_projects_source: Callable[[], list | None] | None = None


def set_projects_source(fn: Callable[[], list | None] | None) -> None:
    """The board registers where the doctor reads its project scan from (main._cached_projects): the scan the state already carries, so
    the doctor never runs `git status` over every repo itself. fn() -> the list of projects, or None when no scan exists yet."""
    global _projects_source
    _projects_source = fn


BACKUP_WARN_AGE = 36 * 3600.0            # an `ok` run older than this warns (the timer runs nightly)
BACKUP_FAIL_AGE = 72 * 3600.0            # older than this fails
FIRST_DAY = 86400.0                      # a board younger than this has had no night to run its first backup
UNCOMMITTED_AGE = 86400.0                # changed files older than this are worth a warning
UNCOMMITTED_FILES = 200                  # files stat'ed per repo
UNCOMMITTED_BUDGET = 3.0                 # seconds for the whole uncommitted-work check


def _backup_timer_fix() -> dict:
    return fix("Check that the nightly timer is running", plat.hint("timers", "ccboard-backup.timer"))


def _backup_now() -> str:
    return (f"Run one now (Back up now in the bell panel, or {plat.hint('start', 'ccboard-backup')}), "
            f"then read {plat.hint('logs', 'ccboard-backup', sudo=False)}")


def _st_dev(path) -> int:
    return os.stat(path).st_dev                       # patched by tests (a second device)


def _nearest_existing(p: Path) -> Path | None:
    p = p.expanduser()
    return next((c for c in (p, *p.parents) if c.exists()), None)


def _c_backup_repo(db) -> Outcome:
    """Is the restic repository somewhere the data is not? Off is a skip (not a pass); a remote repository passes; a local path on the same
    filesystem as the data directory warns: it survives a deleted file or a corrupt database, not a lost disk."""
    if not backup.restic_enabled():
        return _skip("restic backup is off (CCBOARD_RESTIC_REPO=off)")
    repo = settings.restic_repo
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]+:", repo):                      # sftp: rclone: s3: rest: b2: azure: gs: swift:
        return _pass(f"the restic repository is remote ({repo.split(':', 1)[0]}:)")
    try:
        a, b = _nearest_existing(Path(repo)), _nearest_existing(Path(settings.data_dir))
        if a is None or b is None:
            return _skip("could not tell which disk the restic repository or the data directory is on")
        same = _st_dev(a) == _st_dev(b)
    except OSError:
        return _skip("could not read the disks of the restic repository and the data directory")
    if same:
        return _warn("the restic repository is on the same disk as the data: it protects against deletion and corruption, not against losing the disk",
                     fix("Set CCBOARD_RESTIC_REPO to another disk or a remote (sftp:, rclone:, s3:, rest:) and restart; restic-password is in the "
                         "data dir too, copy it elsewhere (README, Restoring from a backup)"))
    return _pass("the restic repository is on a different disk than the data")


def _c_backup_last(db) -> Outcome:
    """The last nightly run, from <data dir>/backup-status.json: ok and under 36 h passes; partial (first warning) or older than 36 h warns;
    failed or older than 72 h fails; no file on a board under a day old is a skip (it has not had a night yet)."""
    try:
        st = json.loads(backup.status_path().read_text())
    except FileNotFoundError:
        if _uptime() < FIRST_DAY:
            return _skip("no backup has run yet; the first nightly run comes with the timer (02:30 by default)")
        return _warn("no backup has ever run on this box", fix(_backup_now(), plat.hint("status", "ccboard-backup.timer", sudo=False, flags="--no-pager")))
    except (OSError, ValueError):
        return _skip("the backup status file could not be read")
    try:
        at = datetime.fromisoformat(str(st.get("at")).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return _skip("the backup status file has no readable time")
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    age = max(0.0, (_utcnow() - at).total_seconds())
    when = f"{at:%Y-%m-%d %H:%M} UTC, {_age_words(age)} ago"
    status = st.get("status")
    first = lambda key: next((str(x) for x in (st.get(key) or []) if x), "")        # noqa: E731
    if status == "failed":
        return _fail(f"the last backup failed ({when}): {first('errors') or 'no message'}", fix("Fix what it names, then " + _backup_now()[0].lower() + _backup_now()[1:]))
    if age > BACKUP_FAIL_AGE:
        return _fail(f"the last backup is {_age_words(age)} old ({when}); the nightly timer is not running it", _backup_timer_fix())
    if status == "partial":
        return _warn(f"the last backup was partial ({when}): {first('warnings') or 'a backup branch was not written'}",
                     fix("The snapshot is fine; read the warning in Settings > Box > Backup"))
    if status != "ok":
        return _warn(f"the last backup reports status {str(status)[:20]!r} ({when})", fix("Run a backup now", None))
    if age > BACKUP_WARN_AGE:
        return _warn(f"the last backup is {_age_words(age)} old ({when}); it should run every night", _backup_timer_fix())
    return _pass(f"the last backup was ok ({when})")


def _backup_record() -> dict | None:
    """The board's own backup record (<data dir>/backup-status.json, the one the Backup status line shows); None when absent or unreadable."""
    try:
        st = json.loads(backup.status_path().read_text())
    except (OSError, ValueError):
        return None
    return st if isinstance(st, dict) else None


def _backup_job_path() -> list[str] | None:
    """The PATH directories the installed backup job runs with: `EnvironmentVariables.PATH` of ~/Library/LaunchAgents/dev.ccboard.backup.plist
    under launchd, `Environment=PATH=` of the installed service unit under systemd. None when the file cannot be read (shown as unknown)."""
    try:
        if settings.runtime == "launchd":
            import plistlib
            plist = Path.home() / "Library" / "LaunchAgents" / f"{plat.launchd_label('backup')}.plist"
            raw = plistlib.loads(plist.read_bytes())["EnvironmentVariables"]["PATH"]
        else:
            raw = re.search(r"(?m)^Environment=PATH=(\S+)", backup.SYSTEMD_UNIT.read_text()).group(1)
        return [x for x in str(raw).split(":") if x]
    except Exception:                    # no file, no key, not a plist: the PATH is unknown, never a failure of the check
        return None


def _on_path(name: str, dirs: list[str]) -> bool:
    return any(os.path.isfile(os.path.join(p, name)) and os.access(os.path.join(p, name), os.X_OK) for p in dirs)


def _c_backup_job(db) -> Outcome:
    """The job that runs the nightly backup: installed and loaded (a launchd label on a Mac, the systemd timer on Linux), the last run's age
    from the board's own record, restic on the job's PATH, a local repository on a Windows drive under WSL, and on a Mac the ssh-agent note
    after a push failure. Where this board is not run by that job manager (a container, a hand-started board) the check skips with the reason:
    under docker the timer belongs to the host. A job that is not loaded warns (CCBOARD_BACKUP=0 leaves it out on purpose); a repository
    setting restic would be handed wrongly fails."""
    rt = settings.runtime
    if rt == "docker":
        return _skip(f"the board runs in a container; the nightly timer belongs to the host ({plat.hint('timers', 'ccboard-backup.timer')} there)")
    if not ((rt == "launchd" and plat.IS_MACOS) or (rt == "systemd" and plat.IS_LINUX)):
        return _skip(f"the board is not run by systemd or launchd here (runtime {rt}), so there is no backup job to look at")
    launchd = rt == "launchd"
    if launchd and plat.current_uid() is None:
        return _skip("this system has no user id to name the launchd domain with")
    what = plat.launchd_label("backup") if launchd else "ccboard-backup.timer"
    try:
        if launchd:
            loaded = _run(["launchctl", "print", plat.launchd_target("backup")]).rc == 0
        else:
            p = _run(["systemctl", "is-active", "ccboard-backup.timer"])
            loaded = p.rc == 0 and p.out.strip() == "active"
    except ToolMissing as e:
        return _skip(f"{e.name} is not available, so the backup job cannot be looked at")
    except ToolTimeout as e:
        return _warn(f"{e.name or 'the service manager'} did not answer in time")

    bad: list[tuple[str, str, dict | None]] = []          # (level, text, fix), worst first at the end
    notes: list[str] = []
    if not loaded:
        text = f"{what} is not loaded" if launchd else f"{what} is not active"
        fx = fix("Load the nightly job again (rerun the installer; CCBOARD_BACKUP=0 leaves it out on purpose)",
                 f"launchctl bootstrap {plat.launchd_domain()}/$(id -u) ~/Library/LaunchAgents/{what}.plist" if launchd else plat.hint("start", "ccboard-backup.timer"))
        bad.append(("warn", text, fx))
    else:
        notes.append(f"{what} is loaded" if launchd else f"{what} is active")

    st = _backup_record()
    at = None
    if st is not None:
        try:
            at = datetime.fromisoformat(str(st.get("at")).replace("Z", "+00:00"))
            at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            at = None
    if at is None:
        notes.append("last run unknown (no readable record yet)")
    else:
        age = max(0.0, (_utcnow() - at).total_seconds())
        notes.append(f"last run {st.get('status')} {_age_words(age)} ago")
        if loaded and age > BACKUP_FAIL_AGE:
            sleepy = " (a Mac that sleeps or is off at the set time runs it on the next wake, to verify)" if launchd else ""
            bad.append(("warn", f"the job is loaded but the last run is {_age_words(age)} old{sleepy}",
                        fix("Run one now, then read the log", plat.hint("start", "ccboard-backup"))))

    if backup.restic_enabled():
        repo = settings.restic_repo
        try:
            local = backup.restic_is_local(repo)
        except ValueError as e:
            bad.append(("fail", str(e), fix("Set CCBOARD_RESTIC_REPO to an absolute path or a remote in the settings file, then restart the board")))
            local = False
        dirs = _backup_job_path()
        if dirs is None:
            notes.append("restic on the job's PATH unknown (the job file could not be read)")
        elif _on_path("restic", dirs):
            notes.append("restic is on the job's PATH")
        else:
            bad.append(("warn", "restic is not on the job's PATH, so the nightly snapshot fails",
                        fix("Install restic" + (" and rerun the installer, which writes the job's PATH" if launchd else ""), plat.hint("install", "restic"))))
        if local and plat.under_drvfs(repo):
            bad.append(("warn", "the restic repository is under /mnt/ (a Windows drive): it is slow there and file modes are ignored",
                        fix("Keep the repository on the WSL file system (ext4) or a remote (sftp:, rclone:, s3:, rest:); wsl --export of the distro is a second copy")))
    pushes = (st or {}).get("push")
    push_failed = any(isinstance(x, dict) and x.get("error") for x in (pushes if isinstance(pushes, list) else []))
    if push_failed and launchd and plat.IS_MACOS:
        bad.append(("warn", "the last run could not push the backup branches; a launchd job may not see your ssh-agent or the Keychain key (UNVERIFIED)",
                    fix("Load the key without a prompt (ssh-add --apple-use-keychain on the key file), or use an https remote with `gh auth setup-git`; "
                        "the job runs ssh with BatchMode, so it never asks")))
    if not bad:
        return _pass("; ".join(notes))
    worst = "fail" if any(b[0] == "fail" for b in bad) else "warn"
    first = next(b for b in bad if b[0] == worst)
    return Outcome(worst, "; ".join([b[1] for b in bad] + notes), first[2])


def _dirty_repos() -> list[tuple[str, str]] | None:
    """(label, path) of every repo the project scan marks dirty; None when the board has no scan to give."""
    src = _projects_source
    projs = src() if callable(src) else None
    if projs is None:
        return None
    out = []
    for p in projs if isinstance(projs, list) else []:
        for r in (p.get("repos") or []) if isinstance(p, dict) else []:
            if isinstance(r, dict) and r.get("dirty") is True and r.get("path"):
                out.append((f"{p.get('name')}/{r.get('name')}", str(r["path"])))
    return out


def _oldest_change_age(repo: str, porcelain: str) -> float | None:
    """Age in seconds of the OLDEST changed file in `git status --porcelain` output (first UNCOMMITTED_FILES lines; a deleted or vanished
    path has no age and is left out); None when no listed path could be read."""
    now, oldest = _wall(), None
    for line in porcelain.splitlines()[:UNCOMMITTED_FILES]:
        xy, rest = line[:2], line[3:]
        if "D" in xy or not rest or rest.startswith('"'):
            continue
        rel = rest.split(" -> ")[-1].rstrip("/")
        try:
            age = now - os.stat(os.path.join(repo, rel)).st_mtime
        except OSError:
            continue
        oldest = age if oldest is None else max(oldest, age)
    return oldest


def _c_backup_uncommitted(db) -> Outcome:
    """Repos with uncommitted work are in no backup (the nightly run copies only committed work). Dirty repos come from the project scan the
    board already holds; only those are asked for their file list (`git status --porcelain`), within UNCOMMITTED_BUDGET seconds in all.
    A repo with changed files older than a day warns; a missing path, an unreadable repo or a slow git skips that repo only."""
    dirty = _dirty_repos()
    if dirty is None:
        return _skip("the board has not scanned the projects yet")
    if not dirty:
        return _pass("no repo has uncommitted changes")
    deadline = time.monotonic() + UNCOMMITTED_BUDGET
    old: list[str] = []
    checked = skipped = 0
    for label, path in dirty:
        left = deadline - time.monotonic()
        if left <= 0:
            skipped += len(dirty) - checked - skipped
            break
        if not Path(path).is_dir():
            skipped += 1
            continue
        try:
            p = _run(["git", "-C", path, "status", "--porcelain"], timeout=max(0.5, min(CMD_TIMEOUT, left)))
        except ToolMissing:
            return _skip("git is not installed, so uncommitted work cannot be listed")
        except ToolTimeout:
            skipped += 1
            continue
        if p.rc != 0:
            skipped += 1
            continue
        checked += 1
        age = _oldest_change_age(path, p.out)
        if age is not None and age > UNCOMMITTED_AGE:
            old.append(label)
    if old:
        names = ", ".join(old[:5]) + (f" and {len(old) - 5} more" if len(old) > 5 else "")
        return _warn(f"{len(old)} repo{'s' if len(old) != 1 else ''} with changes older than a day that no backup covers: {names}",
                     fix("Commit or push them: the nightly run copies only committed work to the backup branches, never a dirty working tree"))
    if checked == 0:
        return _skip(f"could not list the changes of {skipped} dirty repo{'s' if skipped != 1 else ''} (path missing, git slow or failing)")
    more = f"; {skipped} could not be read" if skipped else ""
    return _pass(f"{checked} repo{'s' if checked != 1 else ''} with uncommitted changes, none older than a day{more} (the nightly backup copies only committed work)")


BRANCH_IGNORE = "branches-ignore: ['ccboard-backup/**']"
CI_MAX_REPOS, CI_MAX_FILES, CI_MAX_BYTES = 20, 10, 65536


def _has_origin(repo: Path) -> bool:
    try:
        return '[remote "origin"]' in (repo / ".git" / "config").read_text(errors="replace")
    except OSError:
        return False


def _c_backup_branch_ci(db) -> Outcome:
    """Repos whose workflows would run on every ccboard-backup/<node>/<branch> push (a team repo with `on: push` and no branch filter starts CI
    for each backup branch). Read-only text scan of .github/workflows (20 repos, 10 files each, 64 KB each); unknown is never a hit."""
    from . import workflows
    if not settings.backup_push:
        return _skip("CCBOARD_BACKUP_PUSH=0: nothing is pushed to ccboard-backup branches")
    deadline = time.monotonic() + 3.0
    hits: list[tuple[str, list[str]]] = []
    for repo in backup.repos()[:CI_MAX_REPOS]:
        if time.monotonic() > deadline:
            break
        wf = repo / ".github" / "workflows"
        if not wf.is_dir() or not _has_origin(repo):
            continue
        try:
            files = sorted(f for f in wf.iterdir() if f.suffix in (".yml", ".yaml") and f.is_file())[:CI_MAX_FILES]
        except OSError:
            continue
        runs = []
        for f in files:
            try:
                with open(f, "r", errors="replace") as fh:
                    text = fh.read(CI_MAX_BYTES)
            except OSError:
                continue
            if workflows.push_runs_on_backup_branches(text) is True:
                runs.append(f.name)
        if runs:
            hits.append((str(repo.relative_to(settings.projects_dir)), runs))
    if not hits:
        return _pass("no repo's CI runs on the backup branches")
    shown = "; ".join(f"{r} ({', '.join(fs[:3])})" for r, fs in hits[:5]) + (f"; and {len(hits) - 5} more" if len(hits) > 5 else "")
    return _warn(f"{len(hits)} repo{'s' if len(hits) != 1 else ''} would run CI on every backup branch: {shown}",
                 fix(f"In that repo's workflow add {BRANCH_IGNORE} under push: (the change belongs in that repo, not in ccboard)", BRANCH_IGNORE))


# ---- GitHub Actions (issue #50)

CI_TTL = 300.0                    # GitHub is asked at most once per five minutes, across Doctor opens, Re-checks and board restarts
CI_KV = "doctor_ci_runs"
CI_STUCK = 1800.0                 # queued, waiting or in progress for longer than this is stuck
CI_BAD = ("failure", "timed_out", "cancelled", "startup_failure")
CI_PENDING = ("queued", "waiting", "requested", "pending")
CI_LIMIT = 15.0
CI_FIELDS = "status,conclusion,createdAt,updatedAt,headSha,url,name"
_mem_cache: dict[str, dict] = {}


def _cache_get(db, key: str) -> dict | None:
    try:
        row = db.kv_get(key) if callable(getattr(db, "kv_get", None)) else None
        val = row.get("value") if isinstance(row, dict) else None
    except Exception:
        val = None
    val = val if isinstance(val, dict) else _mem_cache.get(key)
    return val if isinstance(val, dict) else None


def _cache_set(db, key: str, val: dict) -> None:
    _mem_cache[key] = val
    try:
        if callable(getattr(db, "kv_set", None)):
            db.kv_set(key, val)
    except Exception:
        pass                                           # a cache that cannot be written only costs a second call


def _ci_fetch(db, repo: str) -> dict:
    """One `gh run list` for the repository's main branch, stored (success or failure) so nothing asks again for CI_TTL. Raises ToolMissing."""
    p = _run(["gh", "run", "list", "--repo", repo, "--branch", "main", "--limit", "5", "--json", CI_FIELDS], CI_LIMIT)
    rec: dict = {"at": _wall(), "repo": repo}
    try:
        runs = json.loads(p.out) if p.rc == 0 else None
    except ValueError:
        runs = None
    if isinstance(runs, list):
        rec["runs"] = [{k: r.get(k) for k in ("status", "conclusion", "createdAt", "updatedAt", "headSha", "url", "name")} for r in runs if isinstance(r, dict)][:5]
    else:
        rec["error"] = f"gh exited {p.rc}" if p.rc != 0 else "gh printed no run list"
    _cache_set(db, CI_KV, rec)
    return rec


def _iso_age(text) -> float | None:
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (_utcnow() - (t if t.tzinfo else t.replace(tzinfo=timezone.utc))).total_seconds())


def _ci_verdict(rec: dict, revision: str, note: str) -> Outcome:
    runs = rec.get("runs") or []
    if "error" in rec and not runs:
        return _skip(f"GitHub did not give the run list ({rec['error']}); that says nothing about the build{note}", fix("Check that gh is logged in on the box", "gh auth status"))
    if not runs:
        return _skip(f"GitHub lists no runs on main{note}")
    problems: list[str] = []
    done = next((r for r in runs if r.get("status") == "completed"), None)
    bad_url = None
    if done and done.get("conclusion") in CI_BAD:
        age = _iso_age(done.get("updatedAt") or done.get("createdAt"))
        problems.append(f"{done.get('name') or 'a workflow'} ended {done.get('conclusion')}" + (f" {_age_words(age)} ago" if age is not None else ""))
        bad_url = done.get("url")
    stuck = [r for r in runs if r.get("status") in CI_PENDING + ("in_progress",) and (_iso_age(r.get("createdAt")) or 0) > CI_STUCK]
    for r in stuck[:2]:
        problems.append(f"{r.get('name') or 'a workflow'} has been {str(r.get('status')).replace('_', ' ')} for {_age_words(_iso_age(r.get('createdAt')) or 0)} (stuck)")
        bad_url = bad_url or r.get("url")
    green = next((r for r in runs if r.get("status") == "completed" and r.get("conclusion") == "success"), None)
    behind = ""
    if revision and green and green.get("headSha") and green["headSha"] != revision:
        behind = f"; the box is behind the last green commit (it runs {revision[:7]}, the last green is {str(green['headSha'])[:7]})"
    if problems:
        return _warn("GitHub Actions: " + "; ".join(problems) + behind + note, fix(f"Open the run: {bad_url}" if bad_url else "Open the Actions tab of the repository"))
    newest = runs[0]
    age = _iso_age(newest.get("updatedAt") or newest.get("createdAt"))
    state = str(newest.get("conclusion") or newest.get("status") or "unknown").replace("_", " ")
    return _pass(f"the latest run on main ({newest.get('name') or 'workflow'}) is {state}" + (f", {_age_words(age)} ago" if age is not None else "") + behind + note)


def _c_ci_status(db) -> Outcome:
    """Is the build that deploys the board red or stuck? Needs the repository the image was built from (CCBOARD_SOURCE_REPO) and a logged-in gh.
    The answer is cached for five minutes in the board's kv; the call runs on a background probe, so a slow GitHub or a loaded box gives the
    cached answer with its age (or a skip), and a network failure is never red."""
    repo = os.environ.get("CCBOARD_SOURCE_REPO", "").strip()
    if not repo:
        return _skip("this image does not know which repository built it (CCBOARD_SOURCE_REPO is empty: a local build)")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return _skip("CCBOARD_SOURCE_REPO is not an owner/name pair")
    revision = os.environ.get("CCBOARD_IMAGE_REVISION", "").strip()
    rec = _cache_get(db, CI_KV)
    rec = rec if rec and rec.get("repo") == repo else None
    if rec and 0 <= _wall() - float(rec.get("at") or 0) < CI_TTL:
        return _ci_verdict(rec, revision, f" (checked {_age_words(_wall() - float(rec['at']))} ago)")
    pr = _probe_start("ci-status", lambda: _ci_fetch(db, repo))
    got = _probe_wait(pr, time.monotonic() + GH_WAIT)
    if got is not None and got[0] == "ok":
        return _ci_verdict(got[1], revision, "")                  # type: ignore[arg-type]
    if got is not None and isinstance(got[1], ToolMissing):
        return _skip("gh is not installed, so the build cannot be read", fix(*_install("gh")))
    if got is not None and not isinstance(got[1], ToolTimeout):
        return _skip(f"could not read the build ({type(got[1]).__name__})")
    if rec:                                                       # slow: the older answer, with its age, never a warning about the slowness
        return _ci_verdict(rec, revision, f" (checked {_age_words(_wall() - float(rec.get('at') or 0))} ago; GitHub is slow to answer, {BUSY_NOTE})")
    return _skip(f"GitHub did not answer yet ({BUSY_NOTE})", fix(RECHECK_FIX))


# ---- Claude Code version gates (issue #113)

def _claude_version_text() -> str | None:
    """`claude --version` output (one process per doctor run; ToolMissing and ToolTimeout reach _execute, which words them)."""
    exe = settings.claude_bin()
    if not exe:
        return None
    p = _run([exe, "--version"])
    return p.out if p.rc == 0 else None


def _c_claude_features(db) -> Outcome:
    """Which board features need a newer Claude Code than the one installed (agents/claude.py FEATURE_GATES, from the docs read on 2026-10-07).
    No network call. Where a `--help` probe has already answered (ultracode), the probe wins over the number."""
    from .agents import claude as cl
    if not settings.claude_bin():
        return _skip("claude is not installed")
    v = cl.parse_version(_claude_version_text())
    if v is None:
        return _skip("the Claude Code version could not be read")
    unmet = cl.unmet_gates(v)
    try:
        from . import agents
        caps = agents.get("claude").peek_capabilities()
    except Exception:
        caps = None
    if caps and caps.get("ultracode_flag"):
        unmet = [g for g in unmet if g.key != "ultracode_effort"]
    if caps and caps.get("permission_prompts_none"):
        unmet = [g for g in unmet if g.key != "permission_prompts_none"]          # `claude --help` lists it: the probe wins over the number
    ver =".".join(map(str, v))
    if not unmet:
        return _pass(f"Claude Code {ver} meets all {len(cl.FEATURE_GATES)} version gates the board uses")
    shown = ", ".join(f"{g.feature} ({g.min_version})" for g in unmet[:4]) + (f" and {len(unmet) - 4} more" if len(unmet) > 4 else "")
    return _warn(f"Claude Code {ver} is too old for: {shown}", fix("Update Claude Code (README, Updating); the launcher hides or refuses the options meanwhile", "claude update"))


def _c_claude_unattended(db) -> Outcome:
    """Does this box's claude take `--permission-prompts none` (issue #107)? Read from the cached `claude --help` probe, never a new process:
    where it does, every scheduled and batch Claude run passes it, so a tool that is not pre-approved is denied at once. Where it does not,
    an unattended run may stall on a prompt nobody can answer."""
    if not settings.claude_bin():
        return _skip("claude is not installed")
    try:
        from . import agents
        caps = agents.get("claude").peek_capabilities()
    except Exception:
        caps = None
    if caps is None:
        return _skip("claude --help has not been read yet (the launcher reads it once per binary)")
    if caps.get("permission_prompts_none"):
        return _pass("scheduled and batch Claude runs pass --permission-prompts none: a tool that is not pre-approved is denied at once")
    return _warn("this Claude Code has no --permission-prompts none: an unattended run may stall on a permission prompt nobody can answer",
                 fix("Update Claude Code (the flag needs 2.1.259); until then pick dontAsk and list the Allowed tools of each schedule", "claude update"))


def _c_fable_jobs(db) -> Outcome:
    """Stored Claude jobs that resolve to Fable and have no acknowledgement and Max $ of their own (issue #108). They are held, not run: a
    default cap is not consent, and `claude -p` never asks before billing Fable usage credits."""
    from . import scheduler
    held = [j for j in (db.jobs() if db is not None else []) if (j.get("agent") or "claude") == "claude" and scheduler.fable_hold(j)]
    if not held:
        return _pass("no scheduled or batch Claude job is waiting for a Fable acknowledgement")
    names = ", ".join(f"{j['project']}/{j['repo']}: {j['name']}" for j in held[:4]) + (f" and {len(held) - 4} more" if len(held) > 4 else "")
    one = len(held) == 1
    return _warn(f"{len(held)} Claude job{'' if one else 's'} bill{'s' if one else ''} Fable usage credits without asking and {'is' if one else 'are'} held: {names}",
                 fix("Open Schedules, press Acknowledge on each job and set its Max $, or change its model; a held job never runs under a default cap"))


for _id, _group, _label, _fn in (
    ("claude-unattended", "claude", "Unattended Claude runs", _c_claude_unattended),
    ("fable-jobs", "claude", "Fable jobs waiting", _c_fable_jobs),
    ("backup-repo", "box", "Backup repository", _c_backup_repo),
    ("backup-last", "box", "Last backup", _c_backup_last),
    ("backup-job", "box", "Backup job", _c_backup_job),
    ("backup-uncommitted", "box", "Uncommitted work", _c_backup_uncommitted),
    ("backup-branch-ci", "box", "CI on backup branches", _c_backup_branch_ci),
    ("ci-status", "box", "GitHub Actions", _c_ci_status),
    ("claude-features", "claude", "Claude Code version gates", _c_claude_features),
):
    register(_id, _group, _label, _fn)
del _id, _group, _label, _fn


def _finish(cid: str, group: str, label: str, status: str, detail, f) -> Check:
    if status not in STATUSES:
        status, detail, f = "warn", f"unknown status {status!r}", None
    return Check(cid, group, label, status, _clean(detail), _clean_fix(f))


def _execute(spec: tuple, db) -> Check:
    cid, group, label, fn = spec
    try:
        out = fn(db)
        if not isinstance(out, Outcome):
            out = Outcome(*out)
        status, detail, f = out
        if status == "pass":
            _last_good[cid] = (_wall(), Outcome(status, detail, f))
    except ToolMissing as e:
        status, detail, f = _missing(e.name)
    except ToolTimeout as e:             # a slow tool is not a broken one (#99): the last good answer with its age, or a skip that says the box is busy
        status, detail, f = _stale(e.name or cid, _last_good.get(cid), e.limit)
    except Exception as e:     # a bug in a check must not take the page down
        status, detail, f = "warn", f"check error: {e.__class__.__name__}", None
    return _finish(cid, group, label, status, detail, f)


def _field(x, name, default=None):
    return x.get(name, default) if isinstance(x, dict) else getattr(x, name, default)


def _execute_provider(prov: tuple, db) -> list[Check]:
    name, group, fn = prov
    try:
        items = list(fn(db))
        return [_finish(str(_field(x, "id", name)), str(_field(x, "group", group)), str(_field(x, "label", name)),
                        str(_field(x, "status", "")), _field(x, "detail", ""), _field(x, "fix")) for x in items]
    except ToolTimeout:
        return [_finish(name, group, name, "warn", "timed out", None)]
    except Exception as e:
        return [_finish(name, group, name, "warn", f"check error: {e.__class__.__name__}", None)]


_lock = threading.Lock()
_cache: dict[tuple, tuple[float, dict]] = {}
_inflight: dict[tuple, threading.Lock] = {}          # one lock per cache key: concurrent runs coalesce


def run(group: str | None = None, refresh: bool = False, db=None) -> dict:
    """Run the checks of one group (all when None). Raises ValueError for an unknown group.
    Cached for CACHE_TTL seconds per group; refresh=True bypasses the cache (and refills it)."""
    if group is not None and group not in GROUPS:
        raise ValueError(f"unknown doctor group {group!r}; use one of {', '.join(GROUPS)}")
    key = (group, db is not None)
    started = _clock()
    with _lock:
        flight = _inflight.setdefault(key, threading.Lock())
    with flight:                                              # single flight: concurrent callers (a refresh storm) share one run
        with _lock:
            hit = _cache.get(key)
            if hit and (hit[0] > started or (not refresh and _clock() - hit[0] < CACHE_TTL)):
                return copy.deepcopy(hit[1])                  # fresh enough, or produced while this caller was waiting
        units: list[tuple] = [("check", c, c[1]) for c in CHECKS if group is None or c[1] == group]
        units += [("provider", p, p[1]) for p in PROVIDERS if group is None or p[1] == group]
        got_all = _run_units(units, db)
        merged: list[Check] = []
        for i, (kind, spec, grp) in enumerate(units):
            got = got_all.get(i)
            if got is None:
                got = Check(spec[0], grp, spec[2], "warn", "timed out") if kind == "check" else Check(spec[0], grp, spec[0], "warn", "timed out")
            for c in got if isinstance(got, list) else [got]:
                if group is not None and c.group != group:
                    continue                                            # a provider's other groups are not asked for
                i2 = next((j for j, m in enumerate(merged) if m.id == c.id), None)
                if i2 is None:
                    merged.append(c)
                else:
                    merged[i2] = c
        return _assemble(key, merged)


def _run_units(units: list[tuple], db) -> dict[int, object]:
    """Every unit on its own daemon thread (a hung subprocess can never delay interpreter exit), all joined by one deadline
    of CHECK_TIMEOUT: a unit that has not answered by then is reported as 'timed out' and left to finish in the background."""
    results: dict[int, object] = {}
    threads = []
    for i, (kind, spec, grp) in enumerate(units):
        fn = _execute if kind == "check" else _execute_provider
        def work(i=i, fn=fn, spec=spec, kind=kind, grp=grp):
            try:
                results[i] = fn(spec, db)
            except Exception as e:                                   # _execute already wraps; this is the last net
                results[i] = Check(spec[0], grp, spec[2] if kind == "check" else spec[0], "warn", f"check error: {type(e).__name__}")
        t = threading.Thread(target=work, name=f"doctor-{spec[0]}", daemon=True)
        t.start()
        threads.append(t)
    deadline = time.monotonic() + CHECK_TIMEOUT
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return dict(results)


def _assemble(key: tuple, merged: list[Check]) -> dict:
    results = merged
    summary = {k: 0 for k in STATUSES}
    for c in results:
        summary[c.status] += 1
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
           "ok": summary["fail"] == 0, "summary": summary, "checks": [c.to_dict() for c in results]}
    with _lock:
        _cache[key] = (_clock(), out)
    return copy.deepcopy(out)


def invalidate() -> None:
    with _lock:
        _cache.clear()
        _probes.clear()
        _last_good.clear()
        _mem_cache.clear()
