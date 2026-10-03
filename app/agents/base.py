"""The agent adapter interface: everything the board needs to know about one coding agent CLI, behind one class.

`Agent` is the contract (plan: "Key specs / Agent interface"); `claude.py` is the only implementation until the Codex phase.
`shell` is not an adapter. Import rule: this package may import config, projects and claude_auth, never main, hooks, db, tasks
or scheduler (tasks.py must not import agents, and scheduler.py imports this package).
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

STATUSES = ("pass", "warn", "fail", "skip")          # Check.status


class TTLCache:
    """Tiny thread-safe TTL cache for adapters whose CLI probes are slow (version, auth status): `get(key, fn)` runs `fn` at most
    once per `ttl` seconds per key. ClaudeAgent does not need it (claude_auth already caches for 60 s); the Codex adapter will."""

    def __init__(self, ttl: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._lock = threading.Lock()
        self._items: dict = {}

    def get(self, key, fn: Callable[[], object]):
        now = self.clock()
        with self._lock:
            hit = self._items.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        value = fn()                                   # outside the lock: a slow probe must not block other keys
        with self._lock:
            self._items[key] = (self.clock(), value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


@dataclass
class LaunchReq:
    """One launch, agent-neutral. `opts` holds the agent's own options (Claude: model, effort, permission_mode, allowed_tools,
    disallowed_tools, append_system_prompt, fast, devcontainer, and `extra`: the already shlex-split extra CLI args). `bypass` is
    separate from `opts` on purpose: it is never stored. `task=True` applies the stricter task rules (no bypass, no settings
    overrides). `worktree` is the native worktree name (a slug), not a path."""
    kind: str                                          # new | resume | continue
    session_name: str = ""
    cwd: str = ""
    opts: dict = field(default_factory=dict)
    resume_id: str | None = None
    add_dirs: list = field(default_factory=list)
    prompt: str | None = None
    worktree: str | None = None
    bypass: bool = False
    session_id: str | None = None
    task: bool = False


@dataclass
class LaunchPlan:
    """What to type into the tmux session. `argv` is the agent's own argv; `cmd_line` is the full shell line (argv joined, wrapped
    in `devcontainer up && devcontainer exec` when asked). `opts_clean` is the validated, storable subset of the options (never
    bypass, never extra args): `sessions.opts` re-passes it on resume. `worktree` is the planned worktree path or None."""
    argv: list
    cmd_line: str
    agent_session_id: str | None
    cwd: str
    opts_clean: dict = field(default_factory=dict)
    worktree: str | None = None


@dataclass
class OptField:
    """One launcher control. kind: select | combo (select plus free text) | bool | text | textarea | dirs | args.
    `default` pre-fills the launcher UI (None = leave the CLI's own default). `when` shows the field only while every listed
    condition holds, e.g. {"repo.devcontainer": True} or {"worktree": True}."""
    key: str
    label: str
    kind: str
    choices: list | None = None
    default: object = None
    help: str = ""
    group: str = "basic"                               # basic | advanced
    danger: bool = False
    when: dict | None = None


@dataclass
class SlashSpec:
    """A slash command the board may type into a session. weight = measured use count (orders the tuning strip and the palette);
    weight 0 means hidden from the default strip. read = it only prints something (the board captures the pane afterwards)."""
    cmd: str
    label: str
    arg: bool = False
    read: bool = False
    verified: bool = False                             # flips to True per V8 on the box
    weight: int = 0
    destructive: bool = False

    @property
    def hidden(self) -> bool:
        return self.weight <= 0


@dataclass
class HookNorm:
    """A hook event in the board's terms. `flags` is a patch for `db.update_flags` (None deletes a key); `incr` holds counter deltas
    (subagents +1/-1) for the same call. `message` is what the row shows, `result` the full final text of a turn. `limit` is set on a
    rate-limit StopFailure: {kind: 5h|7d|other, resets_at: epoch|None, message}. `ignored` names why an event must not be applied."""
    state: str | None = None
    event: str = ""
    message: str | None = None
    prompt: str | None = None
    result: str | None = None
    flags: dict = field(default_factory=dict)
    ignored: str | None = None
    kind: str | None = None
    attention: bool = False
    incr: dict = field(default_factory=dict)
    limit: dict | None = None


@dataclass
class Check:
    """One doctor check. status: pass | warn | fail | skip. fix: {text, cmd?, action?} or None."""
    id: str
    group: str
    label: str
    status: str
    detail: str = ""
    fix: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class Agent(ABC):
    name: str = ""
    label: str = ""
    glyph: str = ""                                    # claude ◆, codex ◇ (also in notification titles)
    # What GET /api/agents lists next to the option schema (agent-specific, so subclasses override them).
    PERMISSION_MODES: tuple = ()
    EFFORTS: tuple = ()
    MODELS: tuple = ()
    REASONING_BY_MODEL: dict = {}

    # ---- detection and auth (callers may poll these: implementations keep a 60 s cache, see TTLCache) ----
    @abstractmethod
    def bin(self) -> str | None: ...
    @abstractmethod
    def version(self) -> str | None: ...
    @abstractmethod
    def auth_status(self) -> dict: ...                 # {installed, version, loggedIn, authMethod, email?, plan?, error?}
    @abstractmethod
    def login_start(self) -> None: ...
    @abstractmethod
    def login_state(self) -> dict: ...
    @abstractmethod
    def login_submit_code(self, code: str) -> None: ...
    @abstractmethod
    def logout(self) -> dict: ...

    # ---- launching ----
    @abstractmethod
    def option_schema(self) -> list[OptField]: ...
    @abstractmethod
    def validate_opts(self, raw: dict | None, *, interactive: bool, tasks_or_headless: bool) -> dict: ...   # raises projects.BadRequest
    @abstractmethod
    def launch_plan(self, req: LaunchReq) -> LaunchPlan: ...
    @abstractmethod
    def resume_argv(self, session_id: str | None = None, *, name: str | None = None, opts: dict | None = None, add_dirs=()) -> list[str]: ...
    @abstractmethod
    def continue_argv(self, cwd: str, opts: dict | None = None, add_dirs=()) -> list[str]: ...
    @abstractmethod
    def headless_argv(self, prompt: str, *, mode: str, max_turns: int, budget: float | None, extra: list[str], cwd: str | None,
                      slug: str, last_message_file: str | None) -> list[str]: ...
    @abstractmethod
    def parse_headless(self, stdout: str, stderr: str, rc: int) -> dict: ...

    # ---- hooks ----
    @abstractmethod
    def install_hooks(self, app_dir: Path, *, remote_approve: bool, approve_timeout: int) -> None: ...
    @abstractmethod
    def uninstall_hooks(self) -> None: ...
    @abstractmethod
    def hooks_status(self) -> dict: ...                # {installed, events, trust}
    @abstractmethod
    def normalise_hook(self, event: str, payload: dict) -> HookNorm: ...

    # ---- data sources ----
    @abstractmethod
    def transcript_path(self, row_or_payload: dict | None) -> Path | None: ...
    @abstractmethod
    def usage_sources(self) -> list[str]: ...
    @abstractmethod
    def cost_join_key(self, ccusage_row: dict | None) -> str | None: ...

    # ---- control surface ----
    @abstractmethod
    def slash_commands(self) -> dict[str, SlashSpec]: ...
    @abstractmethod
    def exit_command(self) -> str: ...                 # "/exit" | "/quit" (VERIFY V17)
    @abstractmethod
    def worktree_strategy(self) -> str: ...            # "native" | "managed"
    @abstractmethod
    def mcp_register_cmd(self, python: str, script: str, env: dict | None) -> list[str]: ...
    @abstractmethod
    def forbidden_extra(self, extra: list[str], *, interactive: bool, task: bool = False) -> str | None: ...   # the offending token
    @abstractmethod
    def doctor_checks(self) -> list[Check]: ...

    # ---- shared ----
    def status_summary(self) -> dict:
        """The slice of auth_status the board puts in /api/state `agents.<name>`. Built from cached pieces only."""
        st = self.auth_status() or {}
        out = {"installed": bool(st.get("installed")), "version": st.get("version"), "loggedIn": bool(st.get("loggedIn"))}
        for k in ("authMethod", "email"):
            if st.get(k):
                out[k] = st[k]
        out["glyph"] = self.glyph
        hs = self.hooks_status() or {}
        out["hooks"] = {"installed": bool(hs.get("installed"))}
        if hs.get("trust") is not None:
            out["hooks"]["trust"] = hs["trust"]
        return out

    def describe(self) -> dict:
        """The GET /api/agents entry for this agent: identity, install/auth/hooks state, the launcher's option schema and the slash
        registry. Built from cached pieces only; JSON-serialisable; carries no credentials (auth_status holds none)."""
        st = self.auth_status() or {}
        hs = self.hooks_status() or {}
        hooks = {"installed": bool(hs.get("installed"))}
        if hs.get("trust") is not None:
            hooks["trust"] = hs["trust"]
        return {"name": self.name, "label": self.label, "glyph": self.glyph, "installed": bool(st.get("installed")),
                "version": st.get("version"), "auth": dict(st), "hooks": hooks,
                "options": [asdict(f) for f in self.option_schema()], "permission_modes": list(self.PERMISSION_MODES),
                "efforts": list(self.EFFORTS), "models": list(self.MODELS), "reasoning_by_model": dict(self.REASONING_BY_MODEL),
                "slash": {k: asdict(v) for k, v in self.slash_commands().items()}}
