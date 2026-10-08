"""Codex CLI adapter (v0.5.11): launch argv, option validation, headless runs, hooks, identity helpers, doctor checks.

First built against codex-cli 0.145.0, now current with 0.160 (the box) and tolerant of other releases: `codex --help` is probed once
per binary (cached per path + mtime) into a capability set, and the adapter emits only flags that set contains. A Codex that has
--no-daemon / --approve-for-me gets them; 0.145 (whose -a offers untrusted | on-request | never, no on-failure) gets `-a on-request`
instead. When no binary can be probed (not installed, timeout, garbage output) the 0.160 baseline is assumed (BASELINE_CAPS: -a
on-request | never only, `untrusted` retired), so argv can always be built (tests, previews, a box without codex).

Rules this module keeps (plan v0.5.11, hard rules):
  * the interactive argv is built here and only here; user `extra` args never carry a flag the adapter owns (-c, -p, -s, -a, -m,
    --enable, --remote*, bypass spellings ...: see forbidden_extra);
  * --dangerously-bypass-approvals-and-sandbox only on an interactive, non-task launch (LaunchReq.bypass or permission_mode
    bypassPermissions), never stored in opts_clean, never re-passed on resume/continue, never in headless argv; an interactive
    `sandbox=danger-full-access` is bypass-class too and is refused unless LaunchReq.bypass acknowledges it (then the one bypass flag
    is what runs); a task always gets both -s and -a (permission_mode defaults to `default`), so its sandbox never comes from the
    person's config.toml;
  * --dangerously-bypass-hook-trust only when CCBOARD_CODEX_HOOK_TRUST=bypass (default `review`);
  * rollouts and state_*.sqlite are display/discovery only (transcript_path); nothing here reads Codex's credentials file: login
    state comes from `codex login status`, whose text is reduced to {loggedIn, authMethod} and never passed on;
  * every subprocess goes through `_run` (one seam for tests) with a short timeout; under pytest it refuses to run against the real
    ~/.codex (a test must sandbox settings.codex_home);
  * `-C` is never emitted: LaunchPlan.cwd is where the tmux pane starts (a managed worktree: plan.cwd == plan.worktree);
  * `codex exec` gets `-a never` only when `codex exec --help` lists it (0.145 and 0.157 do not: exec never prompts anyway), and
    `--search` only when it lists that (neither does).

Import rule: config, projects, base and claude (shared regexes) only; never main, hooks, db, tasks or scheduler.
"""
from __future__ import annotations

import concurrent.futures
import importlib.util
import itertools
import json
import os
import re
import shlex
import subprocess
import threading
import time
import unicodedata
from pathlib import Path

try:
    import tomllib
except ImportError:                                  # Python < 3.11: the trust check falls back to counting records
    tomllib = None

from .. import platform as plat
from .. import projects
from ..config import settings
from .base import Agent, Check, HookNorm, LaunchPlan, LaunchReq, OptField, SlashSpec, TTLCache
from .claude import (CONTINUING_END, FRESH_SOURCES, LIMIT_MSG_RE, PATH_MAX, RATE_RE, RESULT_MAX, UUID_RE, WORKTREE_RE, _kind, _str,
                     _truthy)

BIN_NAME = "codex"
MIN_VERSION = (0, 157, 0)                       # below this the doctor's codex-bin detail says what newer builds add (a pass: the adapter works)
CMD_TIMEOUT = 4.0                               # per subprocess: below doctor.CHECK_TIMEOUT (5 s), whose codex probes run in parallel
MODELS_TIMEOUT = 8.0                            # `codex debug models` prints ~240 KB (and refreshes $CODEX_HOME/models_cache.json)
MODELS_TTL = 3600.0                             # the catalogue is cached for an hour
FAIL_TTL = 60.0                                 # a failed probe is retried after a minute, never on every call
AUTH_TTL = 60.0

MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
LEVEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:@/-]{0,79}$")      # `codex resume <name>`; never starts with '-'
VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

WORKTREE_DIR = ".ccboard/worktrees"             # managed worktrees (tasks.WORKTREES_CODEX is the same): <repo>/.ccboard/worktrees/<slug>

PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")
MODE_ALIASES = {"manual": "default"}            # Claude's name for the default mode
# The launcher's one mode picker (v0.5.13) over the permission modes above: default = -s workspace-write -a on-request, auto = the automatic
# approval review, read-only = -s read-only, bypass = the one danger flag (an explicit acknowledgement), custom = the sandbox and approval
# the person picks (neither: Codex's own config.toml decides).
MODES = ("default", "auto", "read-only", "bypass", "custom")
MODE_TO_PERMISSION = {"default": "default", "auto": "auto", "read-only": "plan", "bypass": "bypassPermissions"}
LAUNCH_KINDS = ("new", "resume", "continue", "fork")      # LaunchReq.kind; continue = `codex resume --last`, fork = `codex fork [id]`
MAX_PROMPT = 8000                               # a first prompt is typed into the pane with the command: longer ones belong in a task
# `-c key=value` lines the person may add (v0.5.13): the shape, how many, and the keys the board never lets through. The adapter owns the
# model, reasoning, sandbox, approval, profile and hook settings (their own controls set them), and the rest would loosen the sandbox, run a
# command of their own (notify, MCP servers) or point Codex at another server or login.
CONFIG_LINE_RE = re.compile(r"^[A-Za-z0-9_.]+=.+$")
MAX_CONFIG_LINES = 20
MAX_CONFIG_LINE = 400
CONFIG_BLOCKED = frozenset({"model", "model_reasoning_effort", "model_provider", "model_providers", "sandbox_mode", "approval_policy",
                            "approvals_reviewer", "profile", "profiles", "hooks", "notify", "mcp_servers", "projects", "sandbox_workspace_write",
                            "chatgpt_base_url", "openai_base_url", "cli_auth_credentials_store",
                            "forced_login_method", "experimental_use_profile", "default_permissions", "permissions"})
CONFIG_BLOCKED_PREFIX = ("features.hooks", "features.codex_hooks", "features.approve_for_me", "features.yolo", "hooks.", "profiles.",
                         "model_providers.", "mcp_servers.", "projects.", "sandbox_workspace_write.", "permissions.")
HEADLESS_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk")      # a headless run never bypasses
SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
# -a values, current first (codex 0.160: on-request and never). `untrusted` is retired (the docs: it "can prevent either client from starting")
# and `on-failure` is deprecated: both stay here only as values an older binary's `--help` may still list, never as a default (_approvals).
APPROVALS = ("on-request", "never", "on-failure", "untrusted")
EFFORTS = ("low", "medium", "high", "xhigh", "max")   # the catalogue adds more per model (`ultra` on gpt-6.1-sol)
UNSUPPORTED = ("allowed_tools", "disallowed_tools", "tools", "append_system_prompt", "fallback_model", "fork_session", "from_pr",
               "max_turns", "max_budget_usd", "devcontainer", "agent_name", "autocompact", "mcp_config")
TASK_REFUSAL = ("bypassPermissions, danger-full-access or a flag override is not allowed for tasks; start a session and choose bypass "
                "there if you really want it")
DANGER_REFUSAL = ("skips the sandbox, so it needs the same explicit acknowledgement as bypassPermissions (bypass: true); otherwise use "
                  "read-only or workspace-write")

# What a user's extra args may never carry. Tokens are matched exactly (a long option also as `--x=value`; a short option also with its
# value attached, `-cfoo=1`). The adapter owns these flags: a repeat makes clap refuse to start, and the config/profile/feature/
# remote ones can change hooks, permissions or the server behind the board's back.
FORBIDDEN_SHORT = frozenset({"-c", "-p", "-s", "-a", "-m", "-C"})
FORBIDDEN_LONG = frozenset({"--config", "--profile", "--enable", "--disable", "--strict-config", "--sandbox", "--ask-for-approval",
                            "--full-auto", "--model", "--cd", "--search", "--no-alt-screen", "--no-daemon", "--worktree"})
FORBIDDEN_PREFIX = ("--remote",)
FORBIDDEN_STRICT_LONG = frozenset({"--add-dir"})        # unattended runs and tasks: extra write access only through the controls
FORBIDDEN_ARG_PARTS = ("dangerously", "yolo", "bypass")  # substrings, any case (scheduler.FORBIDDEN_ARG_PARTS mirrors them)
FORBIDDEN_ARG_TOKENS = tuple(sorted(FORBIDDEN_SHORT | FORBIDDEN_LONG))

# Codex's own flag set at 0.160 (tests/fixtures/codex_help_0160.txt: the 0.157.1 capture, whose option list the box's 0.160.1 `--help`
# matches per the V8-Codex box check): used when `codex --help` cannot be probed. -a offers on-request | never only (`untrusted` is
# retired, `on-failure` deprecated); --no-daemon and --approve-for-me exist. A live probe of an older binary still reports what it lists.
BASELINE_CAPS = {"fork": True, "no_daemon": True, "approve_for_me": True, "worktree": True, "yolo": False, "bypass_approvals": True,
                 "hook_trust_flag": True, "no_alt_screen": True, "search": True, "add_dir": True, "cd": True, "profile": True,
                 "config": True, "model": True, "sandbox": True, "ask_for_approval": True, "approval_on_failure": False,
                 "approval_untrusted": False}
# `codex exec` at 0.145.0 has no -a/--ask-for-approval (it never prompts) and no --search; --skip-git-repo-check, -o, -C, --json do exist.
BASELINE_EXEC_CAPS = {"exec_approval": False, "exec_search": False, "exec_cd": True, "exec_output_file": True, "exec_skip_git": True,
                      "exec_json": True}

# `codex login --help`: --device-auth (codex-cli 0.157 and newer; the box runs 0.160) prints a URL and a one-time code the person types ON THE PAGE, with
# nothing pasted back into the terminal. 0.145 has no such flag: its login wants a browser on the same machine.
BASELINE_LOGIN_CAPS = {"device_auth": False}
DEVICE_LOGIN_CMD = "codex login --device-auth"      # what the board types into its login tmux session (CODEX_HOME=<pending dir> is set on the session)
PROC_NAME = "codex"                                 # process name (/proc/<pid>/comm) of a Codex process: the native binary names itself so, the npm node wrapper is `node`
MAX_ANCESTOR_HOPS = 40

# `codex debug models` fallback: the current visible family at 0.160, in the order the TUI's /model picker lists them (V8-Codex box check).
# Levels: low..max everywhere, `ultra` only where the picker's "More reasoning..." offered it (GPT-6.1-Sol; GPT-6-Luna showed Max only).
# GPT-6-Astra and GPT-6-Sol were not opened on the box: they get low..max, no ultra (a level offered by the live catalogue still wins).
# No hidden model (codex-auto-review is internal); the older gpt-5.6-* models come back through the live catalogue when it lists them.
# app/static/termkit.js TK_CODEX_MODELS and app/static/launcher.js LX_CODEX_MODELS carry the same slugs (tests/test_static_codex.py).
_LEVELS = ["low", "medium", "high", "xhigh", "max"]
FALLBACK_MODELS = [
    {"slug": "gpt-6.1-sol", "name": "gpt-6.1-sol", "reasoning": _LEVELS + ["ultra"], "default_reasoning": "low"},
    {"slug": "gpt-6-astra", "name": "gpt-6-astra", "reasoning": list(_LEVELS), "default_reasoning": None},
    {"slug": "gpt-6-sol", "name": "gpt-6-sol", "reasoning": list(_LEVELS), "default_reasoning": None},
    {"slug": "gpt-6-luna", "name": "gpt-6-luna", "reasoning": list(_LEVELS), "default_reasoning": "medium"},
]

# Hook events the board needs registered (scripts/codex_hooks.py EVENTS is the source; this list is used when that file is missing).
FALLBACK_EVENTS = ["SessionStart", "UserPromptSubmit", "Stop", "SubagentStart", "SubagentStop", "PreCompact", "PostCompact",
                   "Interrupt", "SessionEnd"]
PERMISSION_EVENT = "PermissionRequest"          # scripts/codex_hooks.py PERMISSION_EVENT: registered unless remote approve is off
HOOK_MARKS = ("ccboard-hook", "ccboard-permission")
REBIND_SOURCES = ("clear", "resume", "fork")    # a SessionStart with one of these on the same pane is the SAME session's next thread
REBIND_STATES = ("idle", "done", "ended")       # a SessionStart with a new id while the row is in one of these is too: guardian and
#                                                 subagent threads start during `working` or `waiting`, never when the main thread is at rest
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_clock = time.monotonic                         # patched by tests (cache age)


# ---------------------------------------------------------------- subprocess seam and caches

def _child_env() -> dict:
    env = dict(os.environ)
    env["CODEX_HOME"] = str(settings.codex_home)
    env.setdefault("NO_COLOR", "1")
    return env


def _run(argv: list[str], timeout: float = CMD_TIMEOUT) -> tuple[int, str, str]:
    """(returncode, stdout, stderr). Raises OSError / subprocess.TimeoutExpired: callers catch `_ERRORS`."""
    if os.environ.get("PYTEST_CURRENT_TEST") and os.path.realpath(settings.codex_home) == os.path.realpath(Path.home() / ".codex"):
        # a test that forgot to sandbox codex_home must not drive the real codex against the developer's real ~/.codex
        # (`codex debug models` rewrites models_cache.json there): the callers read this as "codex could not be run"
        raise OSError("refusing to run codex against the real CODEX_HOME under pytest")
    cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=_child_env(), stdin=subprocess.DEVNULL)
    return cp.returncode, cp.stdout or "", cp.stderr or ""


_ERRORS = (OSError, subprocess.SubprocessError, ValueError)

_memo_lock = threading.Lock()
_memo_items: dict = {}                          # key -> (at, value, ok)


def _memo(key, ok_ttl: float, fn):
    """fn() -> (value, ok). A good value lives `ok_ttl` seconds, a failed one FAIL_TTL; computed outside the lock."""
    now = _clock()
    with _memo_lock:
        hit = _memo_items.get(key)
        if hit and now - hit[0] < (ok_ttl if hit[2] else FAIL_TTL):
            return hit[1]
    value, ok = fn()
    with _memo_lock:
        _memo_items[key] = (_clock(), value, ok)
    return value


def _memo_peek(key):
    with _memo_lock:
        hit = _memo_items.get(key)
    return hit[1] if hit else None


def reset_caches() -> None:
    """Forget every probe (tests; also what a `codex update` wants)."""
    with _memo_lock:
        _memo_items.clear()
    _AGENT_AUTH.clear()


def forget_auth() -> None:
    """Drop the cached `codex login status` verdict (60 s): the login just changed on disk, so the next state must ask again."""
    _AGENT_AUTH.clear()


def foreign_process_count(board_pids=()) -> int | None:
    """How many Codex processes run on this box that the board did not start: a process named `codex` (app.platform.comm: /proc/<pid>/comm
    on Linux) that is not this process and has none of `board_pids` (the panes of the board's own tmux sessions) among its ancestors. Such a
    process (a Hermes agent's app-server daemon, a Codex in somebody's terminal) keeps the login it read when it started, and may write that
    login's refreshed token back into auth.json. None when the process list cannot be read: the count is then unknown, not zero. Reads names
    and parent pids only, never an environment or a command line."""
    pids = plat.iter_processes()
    if pids is None:
        return None
    skip = {int(p) for p in board_pids if isinstance(p, int) and not isinstance(p, bool)} | {os.getpid()}
    n = 0
    for pid in pids:
        if plat.comm(pid) != PROC_NAME:
            continue
        if not any(c in skip for c in [pid, *plat.ancestors(pid, MAX_ANCESTOR_HOPS - 1)]):
            n += 1
    return n


def foreign_processes(board_pids=()) -> int:
    """foreign_process_count() with an unreadable process list counted as 0; callers that must tell "none" from "unknown" use the former."""
    return foreign_process_count(board_pids) or 0


# ---------------------------------------------------------------- #97: where Codex is installed and whether its own update can work
SAFE_UPDATE_CMD = "npm install -g --prefix ~/.local @openai/codex@latest"
SAFE_UPDATE_TEXT = f"Update Codex with `{SAFE_UPDATE_CMD}`. Do not accept Codex's own update prompt."
_NPMRC_PREFIX = re.compile(r"^\s*prefix\s*=\s*(.*?)\s*$")


def _npmrc_prefix(path: Path, home: Path) -> str | None:
    """The `prefix` key of one npmrc file, nothing else: every other line (an npmrc can hold auth tokens) is skipped unread past its key and
    never kept, logged or returned. `~/` and ${HOME} expand; quotes are dropped. None when the file or the key is missing."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in itertools.islice(f, 2000):
                m = _NPMRC_PREFIX.match(line)
                if not m:
                    continue
                val = m.group(1).strip().strip('"').strip("'")
                if not val:
                    return None
                val = val.replace("${HOME}", str(home)).replace("$HOME", str(home))
                return str(home / val[2:]) if val.startswith("~/") else (str(home) if val == "~" else val)
    except (OSError, ValueError):
        return None
    return None


def _install_prefix(exe: str) -> tuple[Path, bool]:
    """(the prefix Codex is installed under, whether it is an npm global install): the real path of the binary walked up to
    `<prefix>/lib/node_modules`; a binary that is not under one (a standalone build) gives the parent of its bin directory."""
    real = Path(os.path.realpath(exe))
    for p in real.parents:
        if p.name == "node_modules" and p.parent.name == "lib":
            return p.parent.parent, True
    return Path(exe).parent.parent, False


def _npm_prefix(env: dict, home: Path, node: str | None) -> tuple[str | None, str]:
    """(npm's global prefix, where it was learned) WITHOUT running npm: NPM_CONFIG_PREFIX / npm_config_prefix in the board's environment, the
    `prefix` key of the user npmrc ($NPM_CONFIG_USERCONFIG or ~/.npmrc), of the global npmrc beside node, else npm's built-in default (the
    directory above node's bin). (None, 'unknown') when node is not found either."""
    for k in ("NPM_CONFIG_PREFIX", "npm_config_prefix"):
        if env.get(k):
            return env[k], k
    userrc = Path(env.get("NPM_CONFIG_USERCONFIG") or env.get("npm_config_userconfig") or home / ".npmrc")
    got = _npmrc_prefix(userrc, home)
    if got:
        return got, str(userrc)
    if node:
        nprefix = Path(os.path.realpath(node)).parent.parent
        got = _npmrc_prefix(nprefix / "etc" / "npmrc", home)
        if got:
            return got, str(nprefix / "etc" / "npmrc")
        return str(nprefix), "the default beside node"
    return None, "unknown"


def _writable(prefix: Path) -> bool:
    """Can this user write where `npm install -g --prefix <prefix>` writes (lib/node_modules and bin, or the prefix itself)?"""
    dirs = [d for d in (prefix / "lib" / "node_modules", prefix / "bin") if d.is_dir()] or [prefix]
    return all(os.access(d, os.W_OK) for d in dirs)


def update_path_check(exe: str | None, *, env: dict | None = None, home: Path | None = None, node: str | None | bool = True) -> Check:
    """Doctor `codex-update-path` (#97): where Codex lives and whether its own update prompt (which runs `npm install -g` against npm's global
    prefix) can work. Filesystem only: never runs npm or any process, never touches the network, reads only the `prefix` key of npmrc files.
      pass  the install prefix is writable and npm's global prefix is the same place
      warn  the install prefix is not writable, or npm's global prefix is not writable, or it is somewhere else (an update lands elsewhere)
      skip  no binary, or npm's prefix cannot be learned offline (unknown: not healthy, not failed)"""
    label = "Codex update path"
    if not exe:
        return Check("codex-update-path", "codex", label, "skip", "codex is not installed")
    env = dict(os.environ) if env is None else env
    home = Path.home() if home is None else Path(home)
    if node is True:
        import shutil
        node = shutil.which("node", path=env.get("PATH"))
    fix = {"text": SAFE_UPDATE_TEXT, "cmd": SAFE_UPDATE_CMD}
    try:
        prefix, managed = _install_prefix(exe)
        inst_ok = _writable(prefix)
        npm, where = _npm_prefix(env, home, node or None)
    except (OSError, ValueError, RuntimeError) as e:
        return Check("codex-update-path", "codex", label, "skip", f"unknown: the install could not be read ({e.__class__.__name__})", fix)
    where_bin = f"{exe} (installed under {prefix}{'' if managed else ', not an npm install'}; {'writable' if inst_ok else 'not writable'})"
    if not inst_ok:
        return Check("codex-update-path", "codex", label, "warn",
                     f"{where_bin}: Codex's own update prompt runs npm install -g there and will fail, which can leave Codex without its native "
                     "binary", fix)
    if npm is None:
        return Check("codex-update-path", "codex", label, "skip", f"{where_bin}; npm's global prefix: unknown (no npm setting and no node found)",
                     fix)
    npm_path = Path(os.path.expanduser(npm))
    npm_ok = _writable(npm_path)
    same = os.path.realpath(npm_path) == os.path.realpath(prefix)
    npm_txt = f"npm's global prefix {npm} ({where}; {'writable' if npm_ok else 'not writable'})"
    if not npm_ok:
        return Check("codex-update-path", "codex", label, "warn",
                     f"{where_bin}; {npm_txt}: Codex's own update prompt installs there and will fail", fix)
    if not same:
        return Check("codex-update-path", "codex", label, "warn",
                     f"{where_bin}; {npm_txt}: Codex's own update prompt would install a second copy there, not update this one", fix)
    return Check("codex-update-path", "codex", label, "pass", f"{where_bin}; {npm_txt}; writable as the board's user ({_whoami()})")


def _whoami() -> str:
    """The user the board runs as (writability is judged for it, not for the person's shell): a name, else the uid."""
    return plat.whoami()


# ---------------------------------------------------------------- #96: a Codex session that took a prompt but never sent a hook
HOOKS_GRACE = 30.0          # seconds after the first turn a Codex session may go without any hook before the row says so


def hooks_missing(row: dict | None, now: float | None = None) -> str | None:
    """The one rule every surface reads (the state builder puts it on the row as `hooks_missing`): 'untrusted' (review mode) or 'bypass'
    (CCBOARD_CODEX_HOOK_TRUST=bypass, where hooks should be running) for a Codex row that has sent the board no hook (flags.hook_seen unset)
    HOOKS_GRACE seconds after its first turn (flags.turn_seen_at: a prompt the board sent, or a user message in its rollout), else None.
    Codex sends its first hook only when the first turn starts, so a fresh launch with no prompt yet never qualifies; an ended row and every
    Claude or shell row never do. The caller lists only live tmux sessions, so 'alive' holds there. Pure apart from the setting."""
    if not isinstance(row, dict) or row.get("agent") != "codex" or row.get("state") == "ended":
        return None
    flags = row.get("flags") if isinstance(row.get("flags"), dict) else {}
    if flags.get("hook_seen"):
        return None
    at = flags.get("turn_seen_at")
    from datetime import datetime, timezone
    try:
        d = datetime.fromisoformat(at[:-1] + "+00:00" if at.endswith(("Z", "z")) else at) if isinstance(at, str) and at else None
        t = (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp() if d else None
    except ValueError:
        t = None
    if t is None:
        return None
    if (time.time() if now is None else float(now)) - t < HOOKS_GRACE:
        return None
    return "bypass" if settings.codex_hook_trust == "bypass" else "untrusted"


def auth_verdict(login: dict | None, evidence: dict | None) -> dict:
    """The auth_probe rule, pure. `login` is auth_status()'s verdict ({loggedIn, error?}), `evidence` codex_rollout.auth_signal's newest
    ({kind: ok|rejected, at, message?} or None).
      not logged in (`codex login status` says so)      missing   (source login_status)
      login status could not be read or exited oddly     unknown   (login_status): an unreadable file is not told from a broken binary
      logged in, newest rollout evidence an auth error   rejected  (rollout)
      logged in, newest rollout evidence a model call    ok        (rollout)
      logged in, no evidence                             unknown   (login_status): the file exists; nothing says it still works"""
    login = login if isinstance(login, dict) else {}
    if not login.get("loggedIn"):
        return {"state": "unknown" if login.get("error") else "missing", "source": "login_status"}
    if isinstance(evidence, dict) and evidence.get("kind") in ("ok", "rejected"):
        out = {"state": evidence["kind"], "source": "rollout", "at": evidence.get("at")}
        if evidence["kind"] == "rejected" and isinstance(evidence.get("message"), str):
            out["message"] = evidence["message"][:300]
        return out
    return {"state": "unknown", "source": "login_status"}


def _bin_key(exe: str) -> tuple:
    try:
        st = os.stat(exe)
        return (exe, st.st_mtime_ns, st.st_size)
    except OSError:
        return (exe, 0, 0)


_warm_lock = threading.Lock()
_warming: set = set()
_AGENT_AUTH = TTLCache(AUTH_TTL, clock=lambda: _clock())
_auth_lock = threading.Lock()


# ---------------------------------------------------------------- capability probe

_FLAG_LINE = re.compile(r"^ {2,8}(?:-[A-Za-z], )?(--[A-Za-z0-9][A-Za-z0-9-]*)", re.M)


def _help_flags(text: str) -> set[str]:
    """The long options a clap help text DEFINES (an option line starts at column 2-8; descriptions are indented deeper, so a flag
    merely mentioned in another option's text is not counted)."""
    return set(_FLAG_LINE.findall(text or ""))


def _option_block(text: str, flag: str) -> str:
    lines = (text or "").splitlines()
    out: list[str] = []
    inside = False
    for ln in lines:
        m = _FLAG_LINE.match(ln)
        if m:
            if inside:
                break
            inside = m.group(1) == flag
        if inside:
            out.append(ln)
    return "\n".join(out)


def _help_commands(text: str) -> set[str]:
    """The subcommands a clap help text lists under its `Commands:` heading (a name at column 2; descriptions are indented deeper)."""
    out: set[str] = set()
    inside = False
    for ln in (text or "").splitlines():
        if ln and not ln[0].isspace():
            inside = ln.strip().lower().startswith("commands")
        elif inside:
            m = re.match(r"^ {2}([a-z][a-z0-9-]*)(?:\s{2,}|$)", ln)
            if m:
                out.add(m.group(1))
    return out


def parse_help(text: str) -> dict | None:
    """`codex --help` -> capability dict (see BASELINE_CAPS for the keys), or None when the text defines no options at all."""
    flags = _help_flags(text)
    if not flags:
        return None
    approval = _option_block(text, "--ask-for-approval")
    return {
        "fork": "fork" in _help_commands(text),
        "no_daemon": "--no-daemon" in flags, "approve_for_me": "--approve-for-me" in flags, "worktree": "--worktree" in flags,
        "yolo": "--yolo" in flags, "bypass_approvals": "--dangerously-bypass-approvals-and-sandbox" in flags,
        "hook_trust_flag": "--dangerously-bypass-hook-trust" in flags, "no_alt_screen": "--no-alt-screen" in flags,
        "search": "--search" in flags, "add_dir": "--add-dir" in flags, "cd": "--cd" in flags, "profile": "--profile" in flags,
        "config": "--config" in flags, "model": "--model" in flags, "sandbox": "--sandbox" in flags,
        "ask_for_approval": "--ask-for-approval" in flags,
        "approval_on_failure": "on-failure" in approval, "approval_untrusted": "untrusted" in approval,
    }


def parse_exec_help(text: str) -> dict | None:
    """`codex exec --help` -> {exec_approval, exec_search, exec_cd, exec_output_file, exec_skip_git, exec_json}, or None."""
    flags = _help_flags(text)
    if not flags:
        return None
    return {"exec_approval": "--ask-for-approval" in flags, "exec_search": "--search" in flags, "exec_cd": "--cd" in flags,
            "exec_output_file": "--output-last-message" in flags, "exec_skip_git": "--skip-git-repo-check" in flags,
            "exec_json": "--json" in flags}


def parse_login_help(text: str) -> dict | None:
    """`codex login --help` -> {device_auth}, or None when the text defines no options at all (not a help text)."""
    flags = _help_flags(text)
    if not flags:
        return None
    return {"device_auth": "--device-auth" in flags}


def _version_tuple(v: str | None) -> tuple | None:
    m = VERSION_RE.search(v or "")
    return tuple(int(x) for x in m.groups()) if m else None


# ---------------------------------------------------------------- models catalogue

def parse_models(text: str) -> list[dict]:
    """`codex debug models` -> [{slug, name, reasoning[], default_reasoning}] for the visible models, best priority first.
    Everything else in the ~240 KB catalogue is dropped. [] when the text is not the expected JSON."""
    start = (text or "").find("{")
    if start < 0:
        return []
    try:
        data = json.loads(text[start:])
    except ValueError:
        return []
    rows = data.get("models") if isinstance(data, dict) else None
    out: list[tuple] = []
    for i, m in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(m, dict):
            continue
        slug = m.get("slug")
        if not isinstance(slug, str) or not MODEL_RE.match(slug):
            continue
        if str(m.get("visibility") or "").lower() in ("hide", "hidden"):
            continue
        levels: list[str] = []
        for lv in m.get("supported_reasoning_levels") if isinstance(m.get("supported_reasoning_levels"), list) else []:
            eff = lv.get("effort") if isinstance(lv, dict) else lv
            if isinstance(eff, str) and LEVEL_RE.match(eff) and eff not in levels:
                levels.append(eff)
        dflt = m.get("default_reasoning_level")
        name = m.get("display_name")
        prio = m.get("priority")
        out.append((prio if isinstance(prio, int) and not isinstance(prio, bool) else 10 ** 6, i,
                    {"slug": slug, "name": name if isinstance(name, str) and name else slug, "reasoning": levels,
                     "default_reasoning": dflt if isinstance(dflt, str) and LEVEL_RE.match(dflt) else None}))
    out.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in out]


# ---------------------------------------------------------------- hooks.json helpers

def _hook_commands(groups) -> list[str]:
    """Every hook command under one event's value. Tolerates both shapes: [{matcher?, hooks:[{type, command}]}] and [{command}]."""
    out: list[str] = []
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict):
            continue
        inner = g.get("hooks")
        if isinstance(inner, list):
            out += [str(h.get("command", "")) for h in inner if isinstance(h, dict)]
        elif "command" in g:
            out.append(str(g.get("command", "")))
    return out


def _is_ours(cmd: str) -> bool:
    return any(m in cmd for m in HOOK_MARKS)


_hooks_mod_cache = None


def _hooks_mod():
    """scripts/codex_hooks.py is a script, not a package: load it by path (the one place that knows how to merge and strip ccboard's
    hook entries). Cached once loaded; None when the file is missing or broken (a stripped checkout)."""
    global _hooks_mod_cache
    if _hooks_mod_cache is not None:
        return _hooks_mod_cache
    path = Path(__file__).resolve().parents[2] / "scripts" / "codex_hooks.py"
    try:
        spec = importlib.util.spec_from_file_location("ccboard_codex_hooks", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except (OSError, AttributeError, ImportError, SyntaxError):
        return None
    _hooks_mod_cache = mod
    return mod


def _in_git_repo(cwd: str | None) -> bool:
    if not cwd:
        return False
    p = Path(cwd)
    return any((d / ".git").exists() for d in (p, *p.parents))


def bind_unbound_rows(db) -> int:
    """Identity step 3 (v0.5.12): bind open codex rows with a NULL agent_session_id to their rollout by cwd + originator + start time
    (FIFO, ambiguity left unbound): codex_rollout.bind_unbound_rows, imported late because that module imports this package. The Sampler's
    Tailer calls it every pass; hooks.bind_unbound_rows is the same call for a hook path."""
    from . import codex_rollout
    return codex_rollout.bind_unbound_rows(db)


class CodexAgent(Agent):
    name = "codex"
    label = "Codex"
    glyph = "◇"
    PERMISSION_MODES = PERMISSION_MODES

    # ---- what GET /api/agents lists: derived from the (cached) catalogue, never from a subprocess of its own ----
    @property
    def MODELS(self) -> tuple:                          # type: ignore[override]
        return tuple(m["slug"] for m in self.models(fetch=False))

    @property
    def EFFORTS(self) -> tuple:                         # type: ignore[override]
        return self._allowed_efforts(None)

    @property
    def REASONING_BY_MODEL(self) -> dict:               # type: ignore[override]
        return {m["slug"]: list(m["reasoning"]) for m in self.models(fetch=False) if m["reasoning"]}

    # ---- detection and auth ----
    def bin(self) -> str | None:
        return settings.codex_bin()

    def version(self) -> str | None:
        """The bare semver (`0.145.0`), cached per binary mtime so a `codex update` is seen at once."""
        exe = self.bin()
        if not exe:
            return None

        def probe():
            try:
                rc, out, err = _run([exe, "--version"])
            except _ERRORS:
                return None, False
            m = VERSION_RE.search(out or err)
            return (m.group(0), True) if rc == 0 and m else (None, False)
        return _memo(("version", _bin_key(exe)), float("inf"), probe)

    def auth_status(self, version=None) -> dict:
        """{installed, version, loggedIn, authMethod?, error?} from `codex login status` (60 s cache). Only the verdict leaves this
        function: the command's text (which may carry a masked key) is never returned. `version` (the doctor passes the in-flight
        `version()` call, as a value or a zero-argument callable) saves a second `codex --version` and, as a callable, is awaited only
        after the login probe, so the two subprocesses overlap instead of chaining."""
        exe = self.bin()
        if not exe:
            return {"installed": False, "version": None, "loggedIn": False}
        with _auth_lock:
            return dict(_AGENT_AUTH.get(_bin_key(exe), lambda: self._probe_auth(exe, version)))

    def _probe_auth(self, exe: str, version=None) -> dict:
        verdict = self._login_verdict(exe)
        ver = version() if callable(version) else (version if version is not None else self.version())
        return {"installed": True, "version": ver, **verdict}

    @staticmethod
    def _login_verdict(exe: str) -> dict:
        out: dict = {"loggedIn": False}
        try:
            rc, so, se = _run([exe, "login", "status"])
        except _ERRORS as e:
            out["error"] = f"codex login status failed: {e.__class__.__name__}"
            return out
        text = f"{so}\n{se}".lower()
        out["loggedIn"] = rc == 0 and "logged in" in text and "not logged in" not in text
        if out["loggedIn"]:
            out["authMethod"] = "chatgpt" if "chatgpt" in text else ("apikey" if "api key" in text or "apikey" in text else "unknown")
        elif rc != 0 and "not logged in" not in text:
            out["error"] = f"codex login status exited {rc}"
        return out

    def auth_probe(self, db=None, now: float | None = None) -> dict:
        """{state: ok|missing|rejected|unknown, source: login_status|rollout|none, at?, message?}: what the board can tell about the live
        Codex login from FREE signals only (#68): the cached `codex login status` verdict (it reports that a login file exists, not that it
        works) and the newest rollout tails (codex_rollout.auth_signal; with `db`, only evidence stamped after the current account became
        live). Never reads auth.json, never starts a model request (`codex exec` is a paid probe and belongs behind a button, not here).
        `unknown` is not healthy: a login file alone is never reported as ok. See auth_verdict."""
        if not self.bin():
            return {"state": "unknown", "source": "none"}
        login = self.auth_status()
        evidence = None
        if login.get("loggedIn"):
            from . import codex_rollout
            since = None
            if db is not None:
                rec = db.kv_get(codex_rollout.KV_CURRENT)
                cur = rec.get("value") if isinstance(rec, dict) else None
                since = codex_rollout._epoch(cur.get("since")) if isinstance(cur, dict) else None
            try:
                evidence = codex_rollout.auth_signal(now, since)
            except Exception:
                evidence = None
        return auth_verdict(login, evidence)

    def login_start(self) -> None:
        raise projects.BadRequest("Codex login from the board arrives with onboarding (v0.5.19); run `codex login --device-auth` on the box")

    def login_state(self) -> dict:
        return {"running": False, "url": None, "tail": []}

    def login_submit_code(self, code: str) -> None:
        raise projects.BadRequest("Codex device login takes no pasted code; run `codex login --device-auth` on the box")

    def logout(self) -> dict:
        exe = self.bin()
        if not exe:
            raise projects.BadRequest("codex is not installed")
        rc, so, se = _run([exe, "logout"], timeout=20)
        reset_caches()
        return {"ok": rc == 0, "output": (so + se).strip()[-500:]}

    # ---- capability probe and catalogue ----
    def capabilities(self) -> dict:
        """The flag set of the installed codex (a fresh dict): `codex --help` once per binary mtime; the 0.145 baseline when there
        is no binary or the probe failed. Merged with the exec subcommand's own caps (exec_*)."""
        return {**self._probe_caps()[0], **self._probe_exec_caps(), **(self.login_caps() or BASELINE_LOGIN_CAPS)}

    def _probe_caps(self) -> tuple[dict, bool]:
        exe = self.bin()
        if not exe:
            return dict(BASELINE_CAPS), False

        def probe():
            try:
                rc, out, _ = _run([exe, "--help"])
            except _ERRORS:
                return (dict(BASELINE_CAPS), False), False
            caps = parse_help(out) if rc == 0 else None
            return ((caps, True), True) if caps else ((dict(BASELINE_CAPS), False), False)
        caps, probed = _memo(("caps", _bin_key(exe)), float("inf"), probe)
        return dict(caps), probed

    def _probe_exec_caps(self) -> dict:
        exe = self.bin()
        if not exe:
            return dict(BASELINE_EXEC_CAPS)

        def probe():
            try:
                rc, out, _ = _run([exe, "exec", "--help"])
            except _ERRORS:
                return dict(BASELINE_EXEC_CAPS), False
            caps = parse_exec_help(out) if rc == 0 else None
            return (caps, True) if caps else (dict(BASELINE_EXEC_CAPS), False)
        return dict(_memo(("exec-caps", _bin_key(exe)), float("inf"), probe))

    def login_caps(self, fetch: bool = True) -> dict | None:
        """{device_auth} from `codex login --help`, probed once per binary mtime (a failed probe is retried after a minute). No binary: the
        baseline (False). fetch=False never starts a subprocess: the cached answer, or None while this binary has not been probed
        successfully (the caller treats None as "not known yet", see warm_login_caps)."""
        exe = self.bin()
        if not exe:
            return dict(BASELINE_LOGIN_CAPS)
        key = ("login-caps", _bin_key(exe))
        if not fetch:
            with _memo_lock:
                hit = _memo_items.get(key)
            return dict(hit[1]) if hit and hit[2] else None

        def probe():
            try:
                rc, out, err = _run([exe, "login", "--help"])
            except _ERRORS:
                return dict(BASELINE_LOGIN_CAPS), False
            caps = parse_login_help(out or err) if rc == 0 else None
            return (caps, True) if caps else (dict(BASELINE_LOGIN_CAPS), False)
        return dict(_memo(key, float("inf"), probe))

    def device_auth(self, fetch: bool = True) -> bool | None:
        """Does this codex have `login --device-auth`? None while that is not known (fetch=False and not probed yet)."""
        caps = self.login_caps(fetch)
        return None if caps is None else bool(caps.get("device_auth"))

    def warm_login_caps(self) -> None:
        """Probe `codex login --help` in the background when it has not been probed (one at a time), so a state poll never waits for it."""
        exe = self.bin()
        if not exe or self.login_caps(fetch=False) is not None:
            return
        key = ("login-caps", _bin_key(exe))
        with _warm_lock:
            if key in _warming:
                return
            _warming.add(key)

        def work():
            try:
                self.login_caps(fetch=True)
            except Exception:                                  # a failed probe stays "not known": the next call tries again after a minute
                pass
            finally:
                with _warm_lock:
                    _warming.discard(key)
        threading.Thread(target=work, daemon=True, name="codex-login-caps").start()

    def models(self, fetch: bool = True) -> list[dict]:
        """The visible models [{slug, name, reasoning[], default_reasoning}]: `codex debug models` cached for an hour (only
        slug/name/reasoning kept), the static fallback while that has never worked. fetch=False never starts a subprocess."""
        exe = self.bin()
        if not exe:
            return [dict(m) for m in FALLBACK_MODELS]
        key = ("models", _bin_key(exe))
        if not fetch:
            got = _memo_peek(key)
            return [dict(m) for m in (got or FALLBACK_MODELS)]

        def probe():
            try:
                rc, out, _ = _run([exe, "debug", "models"], timeout=MODELS_TIMEOUT)
            except _ERRORS:
                return [dict(m) for m in FALLBACK_MODELS], False
            rows = parse_models(out) if rc == 0 else []
            return (rows, True) if rows else ([dict(m) for m in FALLBACK_MODELS], False)
        return [dict(m) for m in _memo(key, MODELS_TTL, probe)]

    def live_models(self) -> list[dict] | None:
        """The catalogue as `codex debug models` last answered it (cached, never a subprocess), or None while it has not answered: the
        fallback list is not a reading, so nothing may be judged stale against it (doctor's codex-saved-models)."""
        exe = self.bin()
        if not exe:
            return None
        with _memo_lock:
            hit = _memo_items.get(("models", _bin_key(exe)))
        return [dict(m) for m in hit[1]] if hit and hit[2] else None

    def _warm_models(self, exe: str) -> None:
        """Start a background refresh of the catalogue when it is missing or stale (one at a time), so describe() / option_schema()
        never wait on `codex debug models`: the first GET /api/agents after a cold start shows the fallback, the next one the real list."""
        key = ("models", _bin_key(exe))
        with _memo_lock:
            hit = _memo_items.get(key)
            if hit and _clock() - hit[0] < (MODELS_TTL if hit[2] else FAIL_TTL):
                return
        with _warm_lock:
            if key in _warming:
                return
            _warming.add(key)

        def work():
            try:
                self.models(fetch=True)
            except Exception:                                  # a failed refresh keeps the fallback; nothing to report
                pass
            finally:
                with _warm_lock:
                    _warming.discard(key)
        threading.Thread(target=work, daemon=True, name="codex-models").start()

    def _allowed_efforts(self, model: str | None) -> tuple:
        cat = self.models(fetch=False)
        if model:
            for m in cat:
                if m["slug"] == model and m["reasoning"]:
                    return tuple(m["reasoning"])
        union = list(EFFORTS)
        for m in cat:
            union += [e for e in m["reasoning"] if e not in union]
        return tuple(union)

    def _approvals(self, caps: dict | None = None) -> tuple:
        caps = caps or self._probe_caps()[0]
        return tuple(a for a in APPROVALS if (a not in ("on-failure", "untrusted")
                                              or caps.get("approval_on_failure" if a == "on-failure" else "approval_untrusted")))

    def launch_caps(self) -> dict:
        """What the launcher may offer on this box: the flag set of `codex --help` (cached per binary; the 0.145 baseline until it is
        probed) plus, from the cached login probe only, device_auth. `fork` is the `codex fork` subcommand."""
        caps = dict(self._probe_caps()[0])
        caps["device_auth"] = bool((self.login_caps(fetch=False) or BASELINE_LOGIN_CAPS).get("device_auth"))
        return caps

    # ---- option schema ----
    def option_schema(self) -> list[OptField]:
        """Every launcher control, in the order the sheet shows them (basic first). `launcher` is the launch kind (fork only where this
        codex has `codex fork`); `mode` is the one permission picker, `sandbox` and `approval` appear for its `custom` choice. A `when`
        list means "any of these" ({"launcher": ["resume", "fork"]})."""
        exe = self.bin()
        if exe:
            self._warm_models(exe)                       # background; the schema below uses what is cached (or the fallback)
        models = [m["slug"] for m in self.models(fetch=False)]
        efforts = list(self._allowed_efforts(None))
        caps = self.launch_caps()
        kinds = [k for k in LAUNCH_KINDS if k != "fork" or caps.get("fork")]
        return [
            OptField("launcher", "Start", "select", kinds, "new",
                     "new: a fresh session. resume: an earlier one by id or name (blank = Codex's picker). continue: the most recent in this "
                     "folder (resume --last)." + (" fork: a copy of an earlier one (codex fork)." if "fork" in kinds else ""), "basic"),
            OptField("resume_id", "Session to resume or fork", "text", None, None,
                     "An id (UUID) or the name Codex shows; blank opens Codex's picker.", "basic", False, {"launcher": ["resume", "fork"]}),
            OptField("name", "Session name", "text", None, None,
                     "The board's own label (and the tmux session name). Codex has no --name: it is not passed on.", "basic"),
            OptField("model", "Model", "combo", models, None,
                     "-m. A slug from `codex debug models` (cached for an hour) or any model id; empty = Codex's own default.", "basic"),
            OptField("reasoning_effort", "Reasoning", "select", efforts, None,
                     "-c model_reasoning_effort=...; the levels a model accepts come from the catalogue (reasoning_by_model). Sent as `reasoning` "
                     "or `reasoning_effort`.", "basic"),
            OptField("mode", "Mode", "select", list(MODES), "default",
                     "default: -s workspace-write -a on-request. auto: automatic approval review. read-only: -s read-only. bypass: no "
                     "approvals and no sandbox (needs the acknowledgement, never for tasks). custom: choose the sandbox and approval below.",
                     "basic"),
            OptField("sandbox", "Sandbox", "select", list(SANDBOXES), None,
                     "-s. danger-full-access skips the sandbox like bypass: it needs the bypass acknowledgement and is interactive only.",
                     "basic", False, {"mode": ["custom"]}),
            OptField("approval", "Approval policy", "select", list(self._approvals()), None,
                     "-a. on-request or never; untrusted is retired and on-failure deprecated (offered only where this Codex still lists them).", "basic", False, {"mode": ["custom"]}),
            OptField("prompt", "First prompt", "textarea", None, None,
                     f"Typed as the first message of a new session ({MAX_PROMPT} characters at most).", "basic", False, {"launcher": ["new"]}),
            OptField("search", "Live web search", "bool", None, False, "--search.", "advanced"),
            OptField("bypass", "Skip approvals and the sandbox", "bool", None, False,
                     "--dangerously-bypass-approvals-and-sandbox. Needs an explicit acknowledgement; never offered for tasks, dispatch or "
                     "scheduled runs, and never stored for resume.", "advanced", True),
            OptField("permission_mode", "Permission mode (Claude's words)", "select", list(PERMISSION_MODES), None,
                     "The same choice as Mode in Claude's words: default and acceptEdits: -s workspace-write -a on-request. plan: -s read-only. "
                     "dontAsk: -a never. auto: automatic approval review. bypassPermissions needs the bypass acknowledgement, never for tasks. "
                     "Mode wins when both are sent.", "advanced"),
            OptField("add_dirs", "Extra directories", "dirs", None, None,
                     "--add-dir: sibling repos (project/repo) the session may write.", "advanced"),
            OptField("worktree", "Start in a new git worktree", "bool", None, False,
                     "ccboard runs `git worktree add -b worktree-<slug> .ccboard/worktrees/<slug>` and starts Codex in it; Codex has no "
                     "native worktree flag here.", "advanced", False, {"launcher": ["new"]}),
            OptField("worktree_name", "Worktree name", "text", None, None,
                     "Letters, digits, '.', '_' and '-'. Blank: the session name.", "advanced", False, {"worktree": True}),
            OptField("config", "Config overrides", "textarea", None, None,
                     f"-c: one key=value per line (key: letters, digits, '_' and '.'; {MAX_CONFIG_LINES} lines at most). Keys the launcher "
                     "owns (model, sandbox, approval, profile, hooks, MCP servers, providers) are refused.", "advanced"),
            OptField("profile", "Profile", "text", None, None,
                     "-p: a profile (config.toml [profiles.<name>] or $CODEX_HOME/<name>.config.toml). Unknown names only warn.",
                     "advanced"),
            OptField("no_alt_screen", "Keep scrollback (no alternate screen)", "bool", None, True,
                     "--no-alt-screen: lets tmux copy-mode and the board's scroll see the whole conversation.", "advanced"),
            OptField("extra", "Extra arguments", "args", None, None,
                     "Raw CLI arguments. Flags the board sets itself (-c, -p, -s, -a, -m, --enable, --remote*, bypass spellings) are rejected.",
                     "advanced"),
        ]

    # ---- validation ----
    def forbidden_extra(self, extra: list[str], *, interactive: bool, task: bool = False) -> str | None:
        """The first offending extra-args token, or None. The same set applies to every launch (the adapter owns those flags);
        unattended runs and tasks (not interactive, or task) also lose --add-dir."""
        strict = (not interactive) or task
        for raw in extra or []:
            t = str(raw)
            low = t.lower()
            if t == "--" or any(part in low for part in FORBIDDEN_ARG_PARTS):
                return t                                     # a bare `--` would turn the adapter's own flags into positionals
            if t.startswith("--"):
                base = low.split("=", 1)[0]
                if base in FORBIDDEN_LONG or base.startswith(FORBIDDEN_PREFIX) or (strict and base in FORBIDDEN_STRICT_LONG):
                    return t
            elif t.startswith("-") and len(t) >= 2 and t[:2] in FORBIDDEN_SHORT:
                return t                                     # -c, or -cfoo=1 (clap takes an attached value)
        return None

    @staticmethod
    def _extra_list(raw) -> list[str]:
        if raw is None or raw == "":
            return []
        if isinstance(raw, str):
            try:
                return shlex.split(raw)
            except ValueError as e:
                raise projects.BadRequest(f"extra args: {e}")
        if isinstance(raw, (list, tuple)) and all(isinstance(a, str) for a in raw):
            return list(raw)
        raise projects.BadRequest("extra args must be a list of strings")

    @staticmethod
    def _with_mode(raw: dict) -> dict:
        """The launcher's `mode` as a permission_mode (it wins over a permission_mode beside it): default, auto, read-only (= plan), bypass
        (= bypassPermissions, which every rule below treats as the danger it is), or any permission mode by its own name. `custom` drops
        the permission mode: the sandbox and approval beside it decide."""
        mode = raw.get("mode")
        if mode in (None, ""):
            return raw
        if not isinstance(mode, str) or mode not in (*MODES, *PERMISSION_MODES, *MODE_ALIASES):
            raise projects.BadRequest(f"mode must be one of {', '.join(MODES)}")
        if mode == "custom":
            return {k: v for k, v in raw.items() if k != "permission_mode"}
        return {**raw, "permission_mode": MODE_TO_PERMISSION.get(mode) or MODE_ALIASES.get(mode, mode)}

    @staticmethod
    def config_lines(raw) -> list[str]:
        """The `-c key=value` lines of a launch: a list of strings or one text with a line each. Every line is stripped, must match
        ^[A-Za-z0-9_.]+=.+$ and stay short and free of control characters, and its key may not be one the launcher owns (CONFIG_BLOCKED,
        CONFIG_BLOCKED_PREFIX, anything sandbox- or approval-shaped, any bypass spelling). Raises BadRequest naming the line."""
        if isinstance(raw, str):
            raw = raw.splitlines()
        if not isinstance(raw, (list, tuple)) or not all(isinstance(x, str) for x in raw):
            raise projects.BadRequest("config: give key=value lines")
        lines = [x.strip() for x in raw if x.strip()]
        if len(lines) > MAX_CONFIG_LINES:
            raise projects.BadRequest(f"config: {MAX_CONFIG_LINES} lines at most")
        for line in lines:
            if len(line) > MAX_CONFIG_LINE or any(unicodedata.category(c).startswith("C") for c in line) or not CONFIG_LINE_RE.match(line):
                raise projects.BadRequest(f"config {line[:60]!r}: use key=value (the key: letters, digits, '_' and '.'; one line, "
                                          f"{MAX_CONFIG_LINE} characters at most)")
            key = line.split("=", 1)[0].lower()
            if (key in CONFIG_BLOCKED or key.startswith(CONFIG_BLOCKED_PREFIX) or key.startswith(("sandbox", "approval"))
                    or any(part in key for part in FORBIDDEN_ARG_PARTS)):
                raise projects.BadRequest(f"config {key}: the launcher sets this itself, or it would loosen the sandbox or run something of "
                                          "its own; use the model / reasoning / mode controls")
        return lines

    def _validate(self, raw: dict | None, *, interactive: bool, tasks_or_headless: bool, bypass: bool = False) -> tuple[dict, dict, list[str]]:
        """-> (full, clean, extra). `full` drives argv (it keeps bypassPermissions / danger-full-access for an interactive launch);
        `clean` is what may be stored and re-passed on resume (never a bypass, never the one-off extra args). `bypass` is the launch's
        explicit acknowledgement (LaunchReq.bypass): an interactive sandbox=danger-full-access is bypass-class and needs it. A task
        always ends up with a permission_mode (default), so both -s and -a are on its line and nothing comes from config.toml."""
        raw = raw if isinstance(raw, dict) else {}
        for key in UNSUPPORTED:
            if raw.get(key):
                raise projects.BadRequest(f"{key}: not supported by codex")
        raw = self._with_mode(raw)
        extra = self._extra_list(raw.get("extra"))
        pm = raw.get("permission_mode")
        if pm is not None and not isinstance(pm, str):
            raise projects.BadRequest(f"permission_mode must be one of {', '.join(PERMISSION_MODES)}")
        pm = MODE_ALIASES.get(pm, pm) if pm else None
        sandbox = raw.get("sandbox") or None
        strict = (not interactive) or tasks_or_headless
        if not interactive:                                   # scheduled / headless run
            if pm and pm not in HEADLESS_MODES:
                raise projects.BadRequest(f"permission_mode must be one of {', '.join(HEADLESS_MODES)} (a headless run never bypasses)")
            bad = self.forbidden_extra(extra, interactive=False)
            if bad:
                raise projects.BadRequest(f"argument not allowed for unattended runs: {bad}")
        elif tasks_or_headless:                               # a task: no bypass, no override
            bad = self.forbidden_extra(extra, interactive=True, task=True)
            if bad or pm == "bypassPermissions":
                raise projects.BadRequest(f"{bad or 'bypassPermissions'}: {TASK_REFUSAL}")
            pm = pm or "default"                              # never inherit sandbox / approval from the person's config.toml
        else:                                                 # an interactive session
            bad = self.forbidden_extra(extra, interactive=True)
            if bad:
                raise projects.BadRequest(f"{bad}: the launcher sets this flag itself; use the model / reasoning / permission / sandbox "
                                          "controls instead of extra args")
        if sandbox is not None:
            if sandbox not in SANDBOXES:
                raise projects.BadRequest(f"sandbox must be one of {', '.join(SANDBOXES)}")
            if strict and sandbox == "danger-full-access":
                raise projects.BadRequest(f"sandbox danger-full-access: {TASK_REFUSAL}")
            if sandbox == "danger-full-access" and not (bypass or pm == "bypassPermissions"):   # (that mode is a bypass already)
                raise projects.BadRequest(f"sandbox danger-full-access: {DANGER_REFUSAL}")
        full: dict = {}
        model = raw.get("model")
        if model is not None and not isinstance(model, str):
            raise projects.BadRequest("model: use a model slug such as gpt-6-sol")
        if model and model.strip():
            m = model.strip()
            if not MODEL_RE.match(m):
                raise projects.BadRequest("model: use a model slug such as gpt-6-sol")
            full["model"] = m
        effort = raw.get("reasoning_effort") or raw.get("reasoning") or raw.get("effort")
        if effort:
            allowed = self._allowed_efforts(full.get("model"))
            if not isinstance(effort, str) or effort not in allowed:
                raise projects.BadRequest(f"reasoning_effort must be one of {', '.join(allowed)}")
            full["reasoning_effort"] = effort
        if pm:
            if pm not in PERMISSION_MODES:
                raise projects.BadRequest(f"permission_mode must be one of {', '.join(PERMISSION_MODES)}")
            full["permission_mode"] = pm
        if sandbox:
            full["sandbox"] = sandbox
        approval = raw.get("approval") or None
        if approval is not None:
            ok = self._approvals()
            if approval not in ok:
                raise projects.BadRequest(f"approval must be one of {', '.join(ok)} (this codex supports no other)")
            full["approval"] = approval
        if raw.get("mode") == "custom" and not (sandbox or approval) and not tasks_or_headless and interactive:
            raise projects.BadRequest("mode custom: choose a sandbox, an approval policy or both")
        if raw.get("config") not in (None, "", []):
            full["config"] = self.config_lines(raw["config"])
        if _truthy(raw.get("search")):
            full["search"] = True
        profile = raw.get("profile")
        if profile is not None and profile != "":
            if not isinstance(profile, str) or not PROFILE_RE.match(profile.strip()):
                raise projects.BadRequest("profile: use letters, digits, '.', '_' or '-'")
            full["profile"] = profile.strip()
        if raw.get("no_alt_screen") is not None and raw.get("no_alt_screen") != "" and not _truthy(raw.get("no_alt_screen")):
            full["no_alt_screen"] = False                     # on by default: only the opt-out is stored
        clean = {k: v for k, v in full.items()
                 if not ((k == "permission_mode" and v == "bypassPermissions") or (k == "sandbox" and v == "danger-full-access"))}
        return full, clean, extra

    def validate_opts(self, raw: dict | None, *, interactive: bool = True, tasks_or_headless: bool = False, bypass: bool = False) -> dict:
        return self._validate(raw, interactive=interactive, tasks_or_headless=tasks_or_headless, bypass=bypass)[1]

    def profile_exists(self, name: str) -> bool:
        home = Path(settings.codex_home)
        if (home / f"{name}.config.toml").is_file():
            return True
        try:
            text = (home / "config.toml").read_text(errors="replace")[:512 * 1024]
        except OSError:
            return False
        return re.search(rf'^\s*\[profiles\.(?:"{re.escape(name)}"|{re.escape(name)})\]\s*$', text, re.M) is not None

    def opt_warnings(self, raw: dict | None) -> list[str]:
        """Soft problems the launcher may show next to a launch that still goes ahead (a profile Codex may not know)."""
        prof = (raw or {}).get("profile") if isinstance(raw, dict) else None
        if isinstance(prof, str) and PROFILE_RE.match(prof.strip()) and not self.profile_exists(prof.strip()):
            return [f"profile {prof.strip()!r} was not found in {settings.codex_home}/config.toml ([profiles.{prof.strip()}]) or as "
                    f"{prof.strip()}.config.toml; Codex may refuse to start"]
        return []

    # ---- argv ----
    def _perm_args(self, full: dict, caps: dict, *, bypass: bool) -> list[str]:
        if bypass:                                            # the one flag; every other permission flag conflicts with it
            if caps.get("bypass_approvals"):
                return ["--dangerously-bypass-approvals-and-sandbox"]
            if caps.get("yolo"):
                return ["--yolo"]
            raise projects.BadRequest("this codex has no bypass flag")
        mode = full.get("permission_mode")
        sandbox: str | None = None
        approval: str | None = None
        approve_for_me = False
        approval_first = False
        if mode in ("default", "acceptEdits"):
            sandbox, approval = "workspace-write", "on-request"
        elif mode == "plan":
            sandbox, approval = "read-only", "on-request"
        elif mode == "dontAsk":
            sandbox, approval = "workspace-write", "never"
        elif mode == "auto":
            sandbox = "workspace-write"
            if caps.get("approve_for_me"):
                approve_for_me = True
            elif caps.get("approval_on_failure"):
                approval, approval_first = "on-failure", True
            else:
                approval = "on-request"
        if full.get("sandbox"):
            sandbox = full["sandbox"]
        if full.get("approval"):
            approval, approve_for_me = full["approval"], False
        s = ["-s", sandbox] if sandbox and caps.get("sandbox", True) else []
        a = ["-a", approval] if approval and caps.get("ask_for_approval", True) else []
        return (["--approve-for-me"] if approve_for_me else []) + (a + s if approval_first else s + a)

    def _flags(self, full: dict, caps: dict, *, bypass: bool, add_dirs, extra: list[str]) -> list[str]:
        """Everything after `codex` / `codex resume`, in the plan's order. Never -C: LaunchPlan.cwd is where the tmux pane starts, so
        Codex's working root is already right (managed worktree: plan.cwd == plan.worktree)."""
        out: list[str] = []
        if caps.get("no_daemon"):
            out.append("--no-daemon")
        if settings.codex_hook_trust == "bypass" and caps.get("hook_trust_flag"):
            out.append("--dangerously-bypass-hook-trust")
        if full.get("no_alt_screen", True) and caps.get("no_alt_screen"):
            out.append("--no-alt-screen")
        out += self._perm_args(full, caps, bypass=bypass)
        if "model" in full:
            out += ["-m", full["model"]]
        if "reasoning_effort" in full:
            out += ["-c", f'model_reasoning_effort="{full["reasoning_effort"]}"']
        if caps.get("config", True):
            for line in full.get("config") or ():
                out += ["-c", line]
        if full.get("search") and caps.get("search"):
            out.append("--search")
        if caps.get("add_dir"):
            for d in add_dirs or ():
                out += ["--add-dir", str(d)]
        if "profile" in full and caps.get("profile"):
            out += ["-p", full["profile"]]
        return out + extra

    @staticmethod
    def _no_bypass(full: dict) -> dict:
        """A resume / continue never re-passes a bypass, whatever the stored opts say."""
        full = dict(full)
        if full.get("permission_mode") == "bypassPermissions":
            del full["permission_mode"]
        if full.get("sandbox") == "danger-full-access":
            del full["sandbox"]
        return full

    @classmethod
    def _stored(cls, opts):
        """Stored launch options on their way back into argv (resume, continue, recovery): a bypass in them is dropped silently, before
        validation, because the one acknowledgement that counts is the launch that is being made now."""
        return cls._no_bypass(opts) if isinstance(opts, dict) else opts

    def launch_plan(self, req: LaunchReq) -> LaunchPlan:
        if req.kind not in LAUNCH_KINDS:
            raise projects.BadRequest(f"launch kind must be one of {', '.join(LAUNCH_KINDS)}")
        full, clean, extra = self._validate(req.opts, interactive=True, tasks_or_headless=bool(req.task), bypass=bool(req.bypass))
        if not req.task and req.prompt is not None and len(req.prompt) > MAX_PROMPT:
            raise projects.BadRequest(f"prompt is too long ({MAX_PROMPT} characters at most); a task takes a longer one")
        if req.task:
            first = req.prompt.split(None, 1)[0] if req.prompt and req.prompt.strip() else ""
            bad = self.forbidden_extra([first], interactive=True, task=True) if first.startswith("-") and first != "--" else None
            if req.bypass or bad:                              # a prompt that would parse as a flag must not smuggle one in (belt and braces:
                #                                                the prompt follows `--`, which V16 confirms on the box)
                raise projects.BadRequest(f"{bad or 'bypassPermissions'}: {TASK_REFUSAL}")
        caps = self._probe_caps()[0]
        if req.kind == "fork" and not caps.get("fork"):
            raise projects.BadRequest("fork: this codex has no `codex fork` command; update codex or resume the session instead")
        bypass = bool(req.bypass) or full.get("permission_mode") == "bypassPermissions"
        if bypass and req.kind != "new":
            full = self._no_bypass(full)                       # resume/continue: only an explicit req.bypass re-passes it
            bypass = bool(req.bypass)
        cwd = req.cwd or ""
        worktree = None
        agent_sid: str | None = None
        if req.kind == "new":
            if req.worktree:
                if not WORKTREE_RE.match(req.worktree) or ".." in req.worktree:
                    raise projects.BadRequest("worktree name: use letters, digits, '.', '_' or '-'")
                worktree = str(Path(cwd) / WORKTREE_DIR / req.worktree)
                cwd = worktree                                 # ccboard has run `git worktree add`: the session works in it
            flags = self._flags(full, caps, bypass=bypass, add_dirs=req.add_dirs, extra=extra)
            argv = ["codex", *flags]
            if req.prompt:
                argv += ["--", req.prompt]                     # `--`: a prompt starting with - is a prompt, not an option (V16)
        else:
            if req.prompt:
                raise projects.BadRequest("a first prompt can only start a new session; resume and continue take none")
            if req.worktree:
                raise projects.BadRequest("worktree: only a new session can start in a new worktree")
            flags = self._flags(full, caps, bypass=bypass, add_dirs=req.add_dirs, extra=extra)
            if req.kind in ("resume", "fork"):
                rid = req.resume_id or None
                if rid and not (UUID_RE.match(rid) or NAME_RE.match(rid)):
                    raise projects.BadRequest("resume id must be a UUID or a session name (letters, digits, spaces, '.', '_', ':', '@', '/' "
                                              "or '-', not starting with '-')")
                # a UUID is the thread to resume; a name only says which one: the id is learned from the first hook, as for a new session.
                # `codex fork` takes the same flags and the same id or name; no id opens the picker, as resume does. The forked thread is a
                # new one: its id is never the one asked for.
                agent_sid = rid if rid and UUID_RE.match(rid) and req.kind == "resume" else None
                argv = ["codex", "resume" if req.kind == "resume" else "fork", *flags, *([rid] if rid else [])]
            else:
                argv = ["codex", "resume", *flags, "--last"]
        # agent_sid stays None for a new session: Codex picks the thread id; the hook payload (or the v0.5.12 Tailer) binds it
        return LaunchPlan(argv=argv, cmd_line=shlex.join(argv), agent_session_id=agent_sid, cwd=cwd, opts_clean=clean, worktree=worktree)

    def resume_argv(self, session_id: str | None = None, *, name: str | None = None, opts: dict | None = None, add_dirs=()) -> list[str]:
        if session_id and not UUID_RE.match(session_id):
            raise projects.BadRequest("resume id must be a UUID")
        if not session_id and name and not NAME_RE.match(name):
            raise projects.BadRequest("session name: use letters, digits, spaces, '.', '_', ':', '@', '/' or '-'")
        full = self._no_bypass(self._validate(self._stored(opts), interactive=True, tasks_or_headless=False)[0])
        flags = self._flags(full, self._probe_caps()[0], bypass=False, add_dirs=[str(d) for d in (add_dirs or ())], extra=[])
        target = session_id or name
        return ["codex", "resume", *flags, *([target] if target else [])]

    def continue_argv(self, cwd: str, opts: dict | None = None, add_dirs=()) -> list[str]:
        # `resume --last` filters by the process cwd, and the tmux pane starts in the row's directory
        full = self._no_bypass(self._validate(self._stored(opts), interactive=True, tasks_or_headless=False)[0])
        flags = self._flags(full, self._probe_caps()[0], bypass=False, add_dirs=[str(d) for d in (add_dirs or ())], extra=[])
        return ["codex", "resume", *flags, "--last"]

    def headless_argv(self, prompt: str, *, mode: str, max_turns: int = 0, budget: float | None = None, extra: list[str] | None = None,
                      cwd: str | None = None, slug: str = "", last_message_file: str | None = None, opts: dict | None = None) -> list[str]:
        """`codex exec --json -s <map> [-a never] [-o FILE] [-C cwd] ... [--skip-git-repo-check] -- <prompt>`. Never on-request, never
        bypass, never --ephemeral. max_turns / budget / slug are Claude's (Codex has no such flags; the worktree is the cwd) and are
        ignored. `-a never` is emitted only when `codex exec --help` lists it: 0.145 has no -a on exec (exec never prompts).
        `opts` (optional) carries model / reasoning_effort / search / profile / add_dirs-free options for the run."""
        m = MODE_ALIASES.get(mode, mode) if mode else "default"
        if m not in HEADLESS_MODES:
            raise projects.BadRequest(f"permission_mode must be one of {', '.join(HEADLESS_MODES)} (a headless run never bypasses)")
        extra = [str(a) for a in (extra or [])]
        bad = self.forbidden_extra(extra, interactive=False)
        if bad:
            raise projects.BadRequest(f"argument not allowed for unattended runs: {bad}")
        caps = {**self._probe_caps()[0], **self._probe_exec_caps()}
        full = self._validate({**(opts or {}), "permission_mode": None}, interactive=False, tasks_or_headless=True)[0] if opts else {}
        sandbox = "read-only" if m == "plan" else "workspace-write"
        cmd = ["codex", "exec", "--json", "-s", sandbox]
        if caps.get("exec_approval"):
            cmd += ["-a", "never"]
        if last_message_file and caps.get("exec_output_file"):
            cmd += ["-o", str(last_message_file)]
        if cwd and caps.get("exec_cd"):
            cmd += ["-C", str(cwd)]
        if "model" in full:
            cmd += ["-m", full["model"]]
        if "reasoning_effort" in full:
            cmd += ["-c", f'model_reasoning_effort="{full["reasoning_effort"]}"']
        if full.get("search") and caps.get("exec_search"):          # `codex exec` has no --search on 0.145 or 0.157: dropped, never sent
            cmd.append("--search")
        if "profile" in full and caps.get("profile"):
            cmd += ["-p", full["profile"]]
        if cwd and caps.get("exec_skip_git") and not _in_git_repo(cwd):
            cmd.append("--skip-git-repo-check")
        return cmd + extra + ["--", prompt]

    def parse_headless(self, stdout: str, stderr: str, rc: int) -> dict:
        """`codex exec --json` JSONL -> {text, session_id, cost, turns, is_error, subtype, rate_limited, usage}. text is the last
        agent message; cost is None (Codex reports tokens, not dollars: the cost join prices them, v0.5.12)."""
        text: str | None = None
        sid: str | None = None
        turns = 0
        usage = None
        failed = False
        errors: list[str] = []
        events = 0
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            events += 1
            typ = ev.get("type")
            if typ == "thread.started":
                sid = _str(ev.get("thread_id")) or sid
            elif typ == "turn.completed":
                turns += 1
                usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else usage
            elif typ == "turn.failed":
                failed = True
                err = ev.get("error")
                msg = err.get("message") if isinstance(err, dict) else err
                if isinstance(msg, str) and msg:
                    errors.append(msg)
            elif typ == "error":
                if isinstance(ev.get("message"), str) and ev["message"]:
                    errors.append(ev["message"])
            elif typ == "item.completed":
                item = ev.get("item")
                if isinstance(item, dict) and item.get("type") in ("agent_message", "assistant_message") and isinstance(item.get("text"), str):
                    text = item["text"]
            elif isinstance(ev.get("msg"), dict):              # the older {"id", "msg": {"type": ...}} event shape
                m = ev["msg"]
                if m.get("type") == "agent_message" and isinstance(m.get("message"), str):
                    text = m["message"]
                elif m.get("type") == "task_complete" and isinstance(m.get("last_agent_message"), str):
                    text, turns = m["last_agent_message"], turns + 1
                elif m.get("type") == "error" and isinstance(m.get("message"), str):
                    errors.append(m["message"])
        is_error = rc != 0 or failed or turns == 0
        if events == 0:
            subtype = "no_json"
        elif failed:
            subtype = "turn_failed"
        elif is_error:
            subtype = "error"
        else:
            subtype = "success"
        if not text and is_error:
            text = "\n".join(errors) or (stderr or "").strip()[-4000:] or (stdout or "").strip()[-4000:]
        blob = "\n".join(errors) + (f"\n{stderr}" if rc != 0 else "")
        rate_limited = is_error and bool(RATE_RE.search(blob) or LIMIT_MSG_RE.search(blob[-500:]) or "usage_limit" in blob.lower())
        # #68: a run that already happened says for free whether the login was refused (its turn.failed / error text); never a rate limit
        from ..login_problem import codex_auth_failure
        auth_failure = is_error and not rate_limited and any(codex_auth_failure(e) for e in errors)
        return {"text": (text or "")[:20000], "session_id": sid, "cost": None, "turns": turns or None, "is_error": is_error,
                "subtype": subtype, "rate_limited": rate_limited, "usage": usage, "auth_failure": auth_failure}

    # ---- hooks ----
    @staticmethod
    def _hooks_file() -> Path:
        return Path(settings.codex_home) / "hooks.json"

    def _required_events(self) -> list[str]:
        """The events a healthy install has: the installer's EVENTS, plus PermissionRequest unless remote approve is off (the same
        rule as install.sh and the entrypoint: CCBOARD_REMOTE_APPROVE=0 skips that hook), so a board with no permission hook does not
        read as healthy."""
        mod = _hooks_mod()
        events = getattr(mod, "EVENTS", None)
        out = list(events) if isinstance(events, list) and events else list(FALLBACK_EVENTS)
        if os.environ.get("CCBOARD_REMOTE_APPROVE", "1") != "0":
            perm = getattr(mod, "PERMISSION_EVENT", None)
            out.append(perm if isinstance(perm, str) and perm else PERMISSION_EVENT)
        return out

    def _read_hooks(self, strict: bool) -> dict:
        p = self._hooks_file()
        try:
            text = p.read_text()
        except FileNotFoundError:
            return {}
        except OSError as e:
            if strict:
                raise RuntimeError(f"{p} cannot be read ({e.__class__.__name__}); fix it first")
            return {}
        try:
            data = json.loads(text or "{}")
            if not isinstance(data, dict):
                raise ValueError("the top level is not an object")
        except ValueError as e:
            if strict:
                raise RuntimeError(f"{p} is not valid JSON ({e}); fix it first")
            return {}
        return data

    def _trust_records(self) -> dict:
        """What config.toml says about hook trust (V2): after the /hooks review Codex writes
        [hooks.state."<hooks.json path>:<event in snake_case>:<group>:<index>"] trusted_hash = "...". Returns {total, events}: the number
        of records with a hash and the snake_case events that have one for THIS hooks.json. A record proves a review happened, not that
        the hash still matches the definition (Codex shows "modified" for that). {0, []} when config.toml is missing or unreadable."""
        p = Path(settings.codex_home) / "config.toml"
        try:
            st = p.stat()
        except OSError:
            return {"total": 0, "events": []}

        def probe():
            try:
                text = p.read_text(errors="replace")[:2 * 1024 * 1024]
            except OSError:
                return {"total": 0, "events": []}, False
            total = text.count("trusted_hash")
            events: set[str] = set()
            if tomllib is not None:
                try:
                    state = (tomllib.loads(text).get("hooks") or {}).get("state") or {}
                    mine = os.path.realpath(self._hooks_file())
                    total = 0
                    for key, val in state.items() if isinstance(state, dict) else ():
                        if not (isinstance(val, dict) and val.get("trusted_hash")):
                            continue
                        total += 1
                        parts = str(key).rsplit(":", 3)
                        if len(parts) == 4 and os.path.realpath(parts[0]) == mine:
                            events.add(parts[1])
                except (ValueError, TypeError, AttributeError):
                    pass                                      # not valid TOML: keep the plain count
            return {"total": total, "events": sorted(events)}, True
        return dict(_memo(("trust", str(p), st.st_mtime_ns, st.st_size, str(self._hooks_file())), float("inf"), probe))

    def hooks_status(self) -> dict:
        data = self._read_hooks(strict=False)
        hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
        present = [ev for ev, groups in hooks.items() if any(_is_ours(c) for c in _hook_commands(groups))]
        required = self._required_events()
        events = [e for e in required if e in present] + sorted(e for e in present if e not in required)
        rec = self._trust_records()
        trusted = [e for e in events if re.sub(r"(?<!^)(?=[A-Z])", "_", e).lower() in rec["events"]]
        return {"installed": all(e in present for e in required), "events": events, "trust": settings.codex_hook_trust,
                "trusted_events": trusted, "trusted_hashes": rec["total"]}

    def install_hooks(self, app_dir: Path, *, remote_approve: bool = True, approve_timeout: int = 90) -> None:
        mod = _hooks_mod()
        if mod is None:
            raise RuntimeError("scripts/codex_hooks.py is missing from this checkout")
        data = self._read_hooks(strict=True)
        new = mod.install(data, Path(app_dir).resolve(), remote_approve, int(approve_timeout))
        mod.save(self._hooks_file(), new)

    def uninstall_hooks(self) -> None:
        mod = _hooks_mod()
        if mod is None:
            raise RuntimeError("scripts/codex_hooks.py is missing from this checkout")
        if not self._hooks_file().exists():
            return
        mod.save(self._hooks_file(), mod.strip_ours(self._read_hooks(strict=True)))

    def thread_relation(self, event: str, payload: dict, bound_id: str | None, state: str | None = None) -> str:
        """own | rebind | subthread: how a hook payload's thread relates to the row's bound agent_session_id. Codex runs guardian and
        subagent threads in the same pane with the same env, so their events carry another session_id and must never move the main
        row's state (hooks.apply counts them in flags.subthreads). A SessionStart whose source is clear / resume / fork is the same
        session's next thread (a rebind); so is any SessionStart with a new id while the row is idle, done or ended (`state`), because
        a build that reports `startup` for the first SessionStart of a /new or `codex fork` thread would otherwise freeze the row on
        the old id. No bound id yet, no payload id or an equal id: own."""
        p = payload if isinstance(payload, dict) else {}
        sid = p.get("session_id")
        if not bound_id or not isinstance(sid, str) or not sid or sid == bound_id:
            return "own"
        if event == "SessionStart" and (_kind(p.get("source"), p.get("matcher")) in REBIND_SOURCES or state in REBIND_STATES):
            return "rebind"
        return "subthread"

    def rebinds(self, payload: dict, row: dict | None = None) -> bool:
        """Does this SessionStart move the row to the payload's thread id? Same rule as the guard in hooks.apply: thread_relation says
        `rebind` (clear / resume / fork, or any new id while the row is at rest, so the /new `startup` case) or `own` (no bound id yet, or the
        same id); a `subthread` never reaches here and never rebinds."""
        bound = (row or {}).get("claude_session_id")
        return self.thread_relation("SessionStart", payload, bound, (row or {}).get("state")) != "subthread"

    def normalise_hook(self, event: str, payload: dict) -> HookNorm:
        """One Codex hook payload in the board's terms (same contract as ClaudeAgent.normalise_hook; pure, never raises).
        Codex-only: Interrupt -> state idle, kind 'interrupt' (hooks.apply expires the row's pending permissions with decision
        'interrupt'); no Notification / StopFailure (limits come from the rollout, v0.5.12). The sub-thread rule needs the bound id,
        which a pure function does not have: see thread_relation (hooks.apply calls it before applying)."""
        p = payload if isinstance(payload, dict) else {}
        n = HookNorm(event=event)
        n.flags["hook_seen"] = True
        tp = p.get("transcript_path")
        if isinstance(tp, str) and tp and len(tp) <= PATH_MAX:
            n.flags["transcript_path"] = tp
        if event == "SessionStart":
            n.kind = _kind(p.get("source"), p.get("matcher"))
            n.state = None if n.kind == "compact" else "idle"
            if n.kind is None or n.kind in FRESH_SOURCES:
                n.flags["subagents"] = None
            n.flags["compacting"] = None
            n.flags["resumed"] = None
        elif event == "UserPromptSubmit":
            n.state, n.prompt = "working", p.get("prompt") if isinstance(p.get("prompt"), str) else None
        elif event == "Stop":
            last = _str(p.get("last_assistant_message"))
            last = last[:RESULT_MAX] if last else None
            n.state, n.attention, n.message, n.result = "done", True, last, last
            n.flags["last_result"] = last
            n.flags["compacting"] = None
        elif event == "Interrupt":
            n.state, n.kind, n.attention = "idle", "interrupt", False
            n.flags["compacting"] = None
        elif event == "SessionEnd":
            n.kind = _kind(p.get("reason"), p.get("matcher"))
            n.state = None if n.kind in CONTINUING_END else "ended"
            n.flags["subagents"] = None
            n.flags["compacting"] = None
        elif event == "SubagentStart":
            n.incr["subagents"], n.kind = 1, _kind(p.get("agent_type"), p.get("matcher"))
        elif event == "SubagentStop":
            n.incr["subagents"], n.kind = -1, _kind(p.get("agent_type"), p.get("matcher"))
        elif event in ("PreCompact", "PostCompact"):
            n.flags["compacting"] = event == "PreCompact"
            n.kind = _kind(p.get("trigger"), p.get("matcher"))
        else:
            n.kind = _kind(p.get("matcher"))
        if n.state and n.state != "waiting":
            n.flags["wait_kind"] = None
        return n

    # ---- data sources ----
    def transcript_path(self, row_or_payload: dict | None) -> Path | None:
        """The rollout file of a thread (display only): a path from the payload / row flags when it lies under CODEX_HOME/sessions,
        else a lookup of `rollout-*-<id>.jsonl` by thread id."""
        x = row_or_payload if isinstance(row_or_payload, dict) else {}
        flags = x.get("flags")
        if isinstance(flags, str):
            try:
                flags = json.loads(flags)
            except ValueError:
                flags = None
        given = x.get("transcript_path") or (flags.get("transcript_path") if isinstance(flags, dict) else None)
        if given:
            return self._under_sessions(given)
        sid = x.get("session_id") or x.get("agent_session_id")
        if isinstance(sid, str) and UUID_RE.match(sid):
            root = Path(settings.codex_home) / "sessions"
            try:
                return next(itertools.islice(root.glob(f"*/*/*/rollout-*-{sid.lower()}.jsonl"), 1), None) or \
                    next(itertools.islice(root.glob(f"*/*/*/rollout-*-{sid}.jsonl"), 1), None)
            except OSError:
                return None
        return None

    @staticmethod
    def _under_sessions(p) -> Path | None:
        """A rollout path from a hook payload is only trusted inside CODEX_HOME/sessions."""
        if not isinstance(p, str) or not p.endswith(".jsonl") or len(p) > PATH_MAX:
            return None
        try:
            Path(p).resolve().relative_to((Path(settings.codex_home) / "sessions").resolve())
        except (ValueError, OSError, RuntimeError):
            return None
        return Path(p)

    def usage_sources(self) -> list[str]:
        return ["rollout", "ccusage"]

    def cost_join_key(self, ccusage_row: dict | None) -> str | None:
        """ccusage names a Codex session `YYYY/MM/DD/rollout-<ts>-<uuid>`: the key is the trailing UUID, lower case."""
        row = ccusage_row if isinstance(ccusage_row, dict) else {}
        for k in ("sessionId", "period", "sessionFile"):
            tail = str(row.get(k) or "").replace(".jsonl", "")[-36:]
            if UUID_RE.match(tail):
                return tail.lower()
        return None

    # ---- control surface ----
    def slash_commands(self) -> dict[str, SlashSpec]:
        # The V8-Codex box check (codex 0.160.1, issue #16): no TUI command takes an argument inline (`/model gpt-6-luna` went to the model
        # as a prompt), there is no /reasoning, /approvals or /sandbox, and bare /fast toggles and writes config.toml (never offered).
        # Model and reasoning are the two steps of the /model picker, approvals plus sandbox are the /permissions picker's presets: POST
        # /tune drives them with keys from app/agents/pickers.py and `s` (this session only) where the picker has it. `verified`: the box
        # ran that exact path (picking a model and `s`; /status printing inline). Reasoning (the cursor's start in step 2 was not seen)
        # and /permissions (picked from an unrecorded cursor) read the cursor off the screen instead and stay unverified.
        return {"model": SlashSpec(cmd="/model", label="Model", drive="picker", tune="model", verified=True),
                "reasoning": SlashSpec(cmd="/model", label="Reasoning", drive="picker", tune="reasoning", verified=False),
                "permissions": SlashSpec(cmd="/permissions", label="Permissions", drive="picker", tune="permissions", verified=False),
                "status": SlashSpec(cmd="/status", label="Status", read=True, verified=True, dialog=False)}

    def exit_command(self) -> str:
        return "/quit"                                   # VERIFY V17

    def worktree_strategy(self) -> str:
        return "managed"

    def mcp_register_cmd(self, python: str, script: str, env: dict | None) -> list[str]:
        # install.sh: codex mcp add ccboard --env K=V -- <python> <script>. Never put the hook token or any secret in env.
        pairs = [x for k, v in sorted((env or {}).items()) for x in ("--env", f"{k}={v}")]
        return ["codex", "mcp", "add", "ccboard", *pairs, "--", python, script]

    # ---- doctor ----
    def _features_hooks(self, exe: str):
        """True / False / None (unknown): the `hooks` row of `codex features list`."""
        def probe():
            try:
                rc, out, _ = _run([exe, "features", "list"])
            except _ERRORS:
                return None, False
            if rc != 0:
                return None, False
            for ln in ANSI_RE.sub("", out).splitlines():
                m = re.match(r"^\s*hooks\s+\S+\s+(true|false)\s*$", ln, re.I)
                if m:
                    return m.group(1).lower() == "true", True
            return None, True
        return _memo(("features-hooks", _bin_key(exe)), 60.0, probe)

    def _mcp_registered(self, exe: str):
        def probe():
            try:
                rc, _, _ = _run([exe, "mcp", "get", "ccboard"])
            except _ERRORS:
                return None, False
            return rc == 0, True
        return _memo(("mcp-ccboard", _bin_key(exe)), 20.0, probe)

    @staticmethod
    def _repo_hook_files(limit: int = 20) -> list[str]:
        root = Path(settings.projects_dir)
        if not root.is_dir():
            return []
        found: list[str] = []
        try:
            for pat in (".codex/hooks.json", "*/.codex/hooks.json", "*/*/.codex/hooks.json"):
                found += [str(p) for p in itertools.islice(root.glob(pat), limit)]
        except OSError:
            pass
        return sorted(set(found))[:limit]

    def doctor_checks(self) -> list[Check]:
        out: list[Check] = []
        exe = self.bin()
        if not exe:
            out.append(Check("codex-bin", "codex", "Codex CLI", "skip", "codex is not on PATH or in ~/.local/bin (optional: Claude works without it)",
                             {"text": "Install the Codex CLI (https://github.com/openai/codex) if you want Codex sessions."}))
            for cid, label in (("codex-auth", "Codex login"), ("codex-hooks", "Codex hooks"), ("codex-trust", "Codex hook trust"),
                               ("codex-alt-screen", "Codex --no-alt-screen"), ("codex-features", "Codex hooks feature"),
                               ("codex-sessions", "Codex sessions folder"), ("codex-mcp", "ccboard MCP server in Codex"),
                               ("codex-repo-hooks", "Repo-level Codex hooks"), ("codex-update-path", "Codex update path")):
                out.append(Check(cid, "codex", label, "skip", "codex is not installed"))
            return out
        # the probes are independent and each has its own subprocess timeout: run them side by side so the group fits the 5 s cap.
        # `codex --version` runs once: the login probe is handed its future (not its result: it asks only after `login status`
        # is done), so `--version` and `login status` overlap and the group costs one timeout, not two chained ones
        with concurrent.futures.ThreadPoolExecutor(max_workers=5, thread_name_prefix="codex-doctor") as pool:
            f_ver = pool.submit(self.version)
            f_auth = pool.submit(self.auth_status, f_ver.result)
            f_caps = pool.submit(self._probe_caps)
            f_feat = pool.submit(self._features_hooks, exe)
            f_mcp = pool.submit(self._mcp_registered, exe)
            ver, st, (caps, probed), feat, mcp = (f.result() for f in (f_ver, f_auth, f_caps, f_feat, f_mcp))
        st = st or {}
        # version
        vt = _version_tuple(ver)
        upd = {"text": "Update Codex: newer releases add flags the board uses when present (--approve-for-me, --no-daemon). " + SAFE_UPDATE_TEXT,
               "cmd": SAFE_UPDATE_CMD}
        if not ver:
            out.append(Check("codex-bin", "codex", "Codex CLI", "warn", f"{exe} (version unknown)",
                             {"text": "`codex --version` printed nothing; run codex once in a shell to see why."}))
        elif vt and vt < MIN_VERSION:
            # not a problem: the adapter probes `codex --help` and uses what this build has. Informational only, so the box that
            # runs 0.145 does not carry a permanent warn
            out.append(Check("codex-bin", "codex", "Codex CLI", "pass",
                             f"{exe} ({ver}): works; {'.'.join(map(str, MIN_VERSION[:2]))}+ adds --approve-for-me and --no-daemon"))
        else:
            out.append(Check("codex-bin", "codex", "Codex CLI", "pass", f"{exe} ({ver})"))
        # login
        if st.get("loggedIn"):
            out.append(Check("codex-auth", "codex", "Codex login", "pass", f"logged in ({st.get('authMethod') or 'unknown method'})"))
        elif st.get("error"):
            out.append(Check("codex-auth", "codex", "Codex login", "warn", str(st["error"]),
                             {"text": "The login state could not be read; run `codex login status` on the box.", "cmd": "codex login status"}))
        else:
            out.append(Check("codex-auth", "codex", "Codex login", "fail", "not logged in",
                             {"text": "Log in from Settings > Agents, or on the box (device flow works over ssh).", "cmd": "codex login --device-auth",
                              "action": "codex_login"}))
        # hooks
        hs = self.hooks_status()
        required = self._required_events()
        missing = [e for e in required if e not in hs["events"]]
        fix = {"text": "Re-run ./install.sh from the ccboard checkout: it merges ccboard's hooks into $CODEX_HOME/hooks.json. Hooks apply to new "
                       "Codex sessions only.", "cmd": "python3 scripts/codex_hooks.py install --app-dir ."}
        if hs["installed"]:
            out.append(Check("codex-hooks", "codex", "Codex hooks", "pass", f"{len(hs['events'])} events registered"))
        elif hs["events"]:
            out.append(Check("codex-hooks", "codex", "Codex hooks", "warn", "missing: " + ", ".join(missing), fix))
        else:
            out.append(Check("codex-hooks", "codex", "Codex hooks", "fail", "no ccboard hooks in hooks.json", fix))
        # trust
        if settings.codex_hook_trust == "bypass":
            out.append(Check("codex-trust", "codex", "Codex hook trust", "warn",
                             "bypass: every launch line carries --dangerously-bypass-hook-trust, so hooks run without review (repo-level "
                             ".codex/hooks.json too)",
                             {"text": "Prefer CCBOARD_CODEX_HOOK_TRUST=review and trust ccboard's hooks once with /hooks in a Codex session."}))
        elif not hs["events"]:
            out.append(Check("codex-trust", "codex", "Codex hook trust", "skip", "review mode; no ccboard hooks installed yet"))
        else:
            untrusted = [e for e in hs["events"] if e not in hs["trusted_events"]]
            tfix = {"text": "Start a Codex session, type /hooks (Codex 0.157+ asks at start) and trust the ccboard entries once; a changed hook "
                            "command or a moved checkout needs it again.", "cmd": "python3 scripts/codex_hooks.py trust-help"}
            if not untrusted:
                out.append(Check("codex-trust", "codex", "Codex hook trust", "pass",
                                 f"review mode; all {len(hs['events'])} ccboard hooks have a trust record in config.toml"))
            elif hs["trusted_events"]:
                out.append(Check("codex-trust", "codex", "Codex hook trust", "warn",
                                 "review mode; no trust record for " + ", ".join(untrusted) + " (Codex will not run them)", tfix))
            else:
                out.append(Check("codex-trust", "codex", "Codex hook trust", "warn",
                                 "review mode: no trust record for ccboard's hooks in config.toml yet, so Codex will not run them until you "
                                 "review them" + (f" ({hs['trusted_hashes']} other record(s) exist)" if hs["trusted_hashes"] else ""), tfix))
        # --no-alt-screen (and the flags newer Codex releases add)
        if caps.get("no_alt_screen"):
            newer = [f for k, f in (("no_daemon", "--no-daemon"), ("approve_for_me", "--approve-for-me")) if caps.get(k)]
            detail = "supported" + (f"; this Codex also has {', '.join(newer)}" if newer else "")
            out.append(Check("codex-alt-screen", "codex", "Codex --no-alt-screen", "pass",
                             detail + ("" if probed else " (assumed: `codex --help` could not be read)")))
        else:
            out.append(Check("codex-alt-screen", "codex", "Codex --no-alt-screen", "warn",
                             "this Codex has no --no-alt-screen: its TUI uses the alternate screen, so tmux scrollback shows nothing",
                             upd))
        # features
        if feat is True:
            out.append(Check("codex-features", "codex", "Codex hooks feature", "pass", "hooks: enabled"))
        elif feat is False:
            out.append(Check("codex-features", "codex", "Codex hooks feature", "fail", "the hooks feature is disabled",
                             {"text": "Enable it: codex features enable hooks (or features.hooks = true in config.toml).",
                              "cmd": "codex features enable hooks"}))
        else:
            out.append(Check("codex-features", "codex", "Codex hooks feature", "warn", "could not read `codex features list`",
                             {"text": "Run `codex features list` on the box.", "cmd": "codex features list"}))
        # sessions folder + state db (display / discovery only)
        home = Path(settings.codex_home)
        sessions = home / "sessions"
        dbs = sorted(home.glob("state_*.sqlite")) if home.is_dir() else []
        bad = [p.name for p in (sessions, *dbs) if p.exists() and not os.access(p, os.R_OK | (os.X_OK if p.is_dir() else 0))]
        if bad:
            out.append(Check("codex-sessions", "codex", "Codex sessions folder", "warn", "not readable: " + ", ".join(bad),
                             {"text": f"Fix the permissions under {home} so the board can read rollouts (read-only use)."}))
        elif not sessions.is_dir():
            out.append(Check("codex-sessions", "codex", "Codex sessions folder", "skip", f"{sessions} does not exist yet (no Codex session so far)"))
        else:
            out.append(Check("codex-sessions", "codex", "Codex sessions folder", "pass",
                             f"{sessions} readable" + (f"; {dbs[-1].name} readable" if dbs else "; no state_*.sqlite yet")))
        # MCP
        if mcp:
            out.append(Check("codex-mcp", "codex", "ccboard MCP server in Codex", "pass", "registered (codex mcp get ccboard)"))
        else:
            cmd = shlex.join(self.mcp_register_cmd(".venv/bin/python", "scripts/ccboard_mcp.py", {"CCBOARD_URL": settings.loopback_url()}))
            out.append(Check("codex-mcp", "codex", "ccboard MCP server in Codex", "warn",
                             "not registered" if mcp is False else "could not run `codex mcp get ccboard`",
                             {"text": "Re-run ./install.sh (it registers the MCP server with Codex), or run the command from the ccboard checkout.",
                              "cmd": cmd}))
        # repo-level hooks files
        repo_hooks = self._repo_hook_files()
        if not repo_hooks:
            out.append(Check("codex-repo-hooks", "codex", "Repo-level Codex hooks", "pass", "none under the projects folder"))
        elif settings.codex_hook_trust == "bypass":
            out.append(Check("codex-repo-hooks", "codex", "Repo-level Codex hooks", "warn",
                             f"{len(repo_hooks)} .codex/hooks.json file(s) would run WITHOUT review (bypass): " + ", ".join(repo_hooks[:3]),
                             {"text": "Review them, or switch CCBOARD_CODEX_HOOK_TRUST back to review."}))
        else:
            out.append(Check("codex-repo-hooks", "codex", "Repo-level Codex hooks", "pass",
                             f"{len(repo_hooks)} file(s); Codex asks you to review each before it runs: " + ", ".join(repo_hooks[:3])))
        # where Codex lives and whether its own update prompt can work (#97): files only, never npm
        up = update_path_check(exe)
        if ver:
            up.detail = f"codex {ver}: {up.detail}"
        out.append(up)
        return out
