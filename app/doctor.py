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
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.parse import urlsplit

from . import claude_auth, login_problem, memory, preflight, projects, push, tmux
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
    pass


class Proc(NamedTuple):
    rc: int
    out: str
    err: str


def _run(argv: list[str], timeout: float = CMD_TIMEOUT) -> Proc:
    """Run argv with a timeout. FileNotFoundError -> ToolMissing(argv[0]); timeout -> ToolTimeout. Tests patch this."""
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise ToolMissing(Path(argv[0]).name) from e
    except subprocess.TimeoutExpired as e:
        raise ToolTimeout(Path(argv[0]).name) from e
    except PermissionError as e:       # present but not executable: the same remedy as missing
        raise ToolMissing(Path(argv[0]).name) from e
    return Proc(cp.returncode, cp.stdout or "", cp.stderr or "")


def _which(name: str) -> str | None:
    return shutil.which(name)


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


_INSTALL = {
    "tmux": ("Install tmux 3.2 or newer", "sudo apt-get install -y tmux"),
    "git": ("Install git 2.15 or newer", "sudo apt-get install -y git"),
    "gh": ("Install the GitHub CLI, then log in", "sudo apt-get install -y gh && gh auth login"),
    "ccusage": ("Install ccusage (needs node and npm)", "npm install -g --prefix ~/.local ccusage"),
    "claude": ("Install Claude Code on the box", "curl -fsSL https://claude.ai/install.sh | bash"),
    "ttyd": ("Re-run the installer: it installs ttyd", "./install.sh"),
}


def _missing(tool: str) -> Outcome:
    text, cmd = _INSTALL.get(tool, (f"Install {tool}", None))
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
        return _fail(f"tmux {_vs(v)} is older than {_vs(MIN_TMUX)}", fix(f"Upgrade tmux to {_vs(MIN_TMUX)} or newer", "sudo apt-get install -y --only-upgrade tmux"))
    return _pass(f"tmux {_vstr(p.out or p.err) or _vs(v)}")


def _c_tmux_server(db) -> Outcome:
    if tmux.server_up():
        return _pass(f"the tmux server on socket '{settings.tmux_socket}' is running")
    return _fail(f"no tmux server on socket '{settings.tmux_socket}'",
                 fix("Start the tmux service that owns the sessions (never restart it while sessions run)", "sudo systemctl start ccboard-tmux"))


def _c_tmux_conf(db) -> Outcome:
    return _skip("verified in v0.5.7")


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
                     fix("Restart the ttyd service", "sudo systemctl restart ccboard-ttyd"))
    return _fail("ttyd is not installed and nothing listens on its port", fix(*_INSTALL["ttyd"]))


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
    return _fail(f"nothing listens on 127.0.0.1:{port}", fix("Start code-server", "sudo systemctl restart code-server@$USER"))


def _c_projects_dir(db) -> Outcome:
    p = Path(settings.projects_dir)
    if not p.is_dir():
        return _fail(f"{p} is not a directory", fix("Create it and point PROJECTS_DIR at it", f"mkdir -p {p}"))
    if not os.access(p, os.W_OK | os.X_OK):
        return _fail(f"{p} is not writable by the board's user", fix("Give the board's user ownership", f"sudo chown $USER {p}"))
    return _pass(f"{p} exists and is writable")


def _c_git(db) -> Outcome:
    try:
        p = _run(["git", "--version"])
    except ToolMissing:
        return _missing("git")
    v = _ver(p.out)
    if p.rc != 0 or v is None:
        return _warn("could not read the git version", fix("Run git --version on the box", "git --version"))
    if v < MIN_GIT:
        return _fail(f"git {_vs(v)} is older than {_vs(MIN_GIT)}", fix(f"Upgrade git to {_vs(MIN_GIT)} or newer", "sudo apt-get install -y --only-upgrade git"))
    # the clone probe's address pin (http.curloptResolve, git 2.37+) is read from the version, not from `git help config` (no man pages in the container)
    return _pass(f"git {_vstr(p.out) or _vs(v)}; {preflight.pin_note(preflight.parse_git_version(p.out) or (v[0], v[1], 0))}")


def _c_gh(db) -> Outcome:
    try:
        p = _run(["gh", "--version"])
    except ToolMissing:
        return _missing("gh")
    v = _ver(p.out)
    if p.rc != 0:
        return _warn("gh --version failed", fix("Reinstall the GitHub CLI", "sudo apt-get install -y --reinstall gh"))
    try:
        auth = _run(["gh", "auth", "status"])   # exit code only: its output can mention the account and token source
    except ToolMissing:
        return _missing("gh")
    where = f"gh {_vstr(p.out)}" if v else "gh"
    if auth.rc == 0:
        return _pass(f"{where}, logged in")
    return _warn(f"{where}, not logged in", fix("Log gh in on the box (PRs and clone-from-GitHub need it)", "gh auth login"))


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
        return _fail(f"ntfy does not answer at {base}", fix("Start the ntfy server", "sudo systemctl start ntfy"))
    healthy = None
    try:
        healthy = json.loads(body).get("healthy") if body else None
    except (ValueError, AttributeError):
        healthy = None
    if status >= 500 or healthy is False:
        return _warn(f"ntfy answers at {base} but reports a problem (HTTP {status})", fix("Check the ntfy service, then send a test", "sudo journalctl -u ntfy -n 30 --no-pager", "notify_test"))
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


# ------------------------------------------------------------------ memory checks (claude-mem)

MEM_GROUP = "memory"
MEM_QUEUE_WARN = 200              # queued observations above this: the observer is behind
MEM_ERROR_WINDOW = 3600.0         # a provider error newer than this (seconds) is a warning
MEM_BUN_DIRS = ("/usr/local/bin", "/opt/homebrew/bin", "/home/linuxbrew/.linuxbrew/bin", "/usr/bin", "/snap/bin")   # = bin/ccboard-mem-run
MEM_PLUGIN_ADD = "claude plugin marketplace add thedotmack/claude-mem && claude plugin install claude-mem@thedotmack"
MEM_BEHIND = "the observer is behind: it shares your Claude subscription window"
MEM_IDS = (("memory-plugin", "claude-mem plugin"), ("memory-worker", "claude-mem worker"), ("memory-queue", "Observer queue"),
           ("memory-error", "Observer provider errors"), ("memory-projects", "Project keys"), ("memory-env", "Worker environment"),
           ("memory-bun", "bun runtime"), ("memory-api", "Memory page API"))
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
                     "does: systemctl status ccboard-mem"))


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


def _mem_env(db, ctx: dict) -> Outcome:
    h = _mem_health(ctx)
    pid = h.get("pid")
    if not pid:
        return _skip("no worker process to inspect")
    leaks = memory.env_leaks(pid)
    if leaks is None:
        return _skip("could not read the worker's environment (another user's process, or no /proc here)")
    if leaks:
        return _warn(f"the memory worker inherited a session's environment; install ccboard-mem.service (it holds {', '.join(leaks)})",
                     fix("Install the unit, then hand over: the plugin's `worker-service.cjs stop`, then `sudo systemctl start ccboard-mem`. "
                         "An update undoes it unless CLAUDE_MEM_WORKER_AUTOSTART=false (README, claude-mem)",
                         "CCBOARD_MEM_SERVICE=1 ./install.sh"))
    return _pass("the worker's environment holds no ccboard session variables")


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


_MEM_FNS = {"memory-plugin": _mem_plugin, "memory-worker": _mem_worker, "memory-queue": _mem_queue, "memory-error": _mem_error,
            "memory-projects": _mem_projects, "memory-env": _mem_env, "memory-bun": _mem_bun, "memory-api": _mem_api}


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
    ("ttyd", "terminal", "ttyd (web terminal)", _c_ttyd),
    ("attach-wrapper", "terminal", "ccboard-attach wrapper", _c_attach_wrapper),
    ("code-server", "box", "code-server", _c_code_server),
    ("projects-dir", "box", "Projects directory", _c_projects_dir),
    ("git", "box", "git 2.15 or newer", _c_git),
    ("gh", "box", "GitHub CLI", _c_gh),
    ("ccusage", "box", "ccusage", _c_ccusage),
    ("identity", "box", "Identity and access", _c_identity),
    ("samples-heartbeat", "box", "Usage samples heartbeat", _c_samples),
    ("ntfy", "notify", "ntfy server", _c_ntfy),
    ("push", "notify", "Web Push subscriptions", _c_push),
    ("claude-bin", "claude", "Claude Code binary", _c_claude_bin),
    ("claude-auth", "claude", "Claude login", _c_claude_auth),
    ("claude-hooks", "claude", "Claude hooks and statusLine", _c_claude_hooks),
):
    register(_id, _group, _label, _fn)
del _id, _group, _label, _fn
register_provider("memory", MEM_GROUP, memory_checks)       # claude-mem (v0.5.10): one probe, seven checks
register_provider("codex", CODEX_GROUP, codex_checks)       # the Codex adapter's checks (v0.5.11)


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
    except ToolMissing as e:
        status, detail, f = _missing(e.name)
    except ToolTimeout:
        status, detail, f = "warn", "timed out", None
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
