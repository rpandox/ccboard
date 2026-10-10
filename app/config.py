"""Settings from the environment. Read once at import, validated at startup."""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import stat
from pathlib import Path

from . import platform as plat

log = logging.getLogger("ccboard")


def parse_allowlist(raw: str) -> set[str]:
    """Comma list -> normalised set. Empty entries are dropped (never match '')."""
    out: set[str] = set()
    for part in raw.split(","):
        u = part.strip()
        if u:
            out.add(u.lower() if u.isascii() else u)
    return out


def parse_clone_hosts(raw: str) -> tuple[str, ...]:
    """CCBOARD_CLONE_ALLOWED_HOSTS: comma list of exact host names or IP literals, plus `*.example.com` for the names below example.com. Lower-cased, one trailing dot and
    the brackets round an IPv6 literal dropped, an address written in its plain form (so a URL's host compares equal). Empty entries are dropped."""
    out: list[str] = []
    for part in (raw or "").split(","):
        h = part.strip().lower().strip("[]")
        if h.endswith("."):
            h = h[:-1]
        if not h or not h.isascii():
            continue
        try:
            h = str(ipaddress.ip_address(h))
        except ValueError:
            pass
        if h not in out:
            out.append(h)
    return tuple(out)


DEFAULT_NODE_PORTS = (443, 8443)
DEFAULT_NODE_TAGS = ("tag:ccboard",)
NODE_LIST_MAX = 8
_TAG_RE = re.compile(r"tag:[a-z][a-z0-9-]{0,62}")


def parse_node_ports(raw) -> tuple[tuple[int, ...], bool]:
    """CCBOARD_NODE_PORTS: comma or space separated ports 1 to 65535, in order, no repeats, at most 8. Unset or blank: (443, 8443). Items that are not
    a port are dropped and the second value is True (the doctor says so); when nothing is left the default is used."""
    text = (raw or "").strip()
    if not text:
        return DEFAULT_NODE_PORTS, False
    out, bad = [], False
    for item in re.split(r"[,\s]+", text):
        if not item:
            continue
        if item.isascii() and item.isdigit() and 1 <= int(item) <= 65535:
            if int(item) not in out and len(out) < NODE_LIST_MAX:
                out.append(int(item))
        else:
            bad = True
    return (tuple(out), bad) if out else (DEFAULT_NODE_PORTS, True)


def parse_node_tags(raw) -> tuple[tuple[str, ...], bool]:
    """CCBOARD_NODE_TAGS: comma or space separated Tailscale tags (`tag:name`, a bare `name` gets the prefix), at most 8. Unset or blank: ("tag:ccboard",).
    `none` means no tag counts, only the same user's devices. Items that are not a tag are dropped and the second value is True."""
    text = (raw or "").strip()
    if not text:
        return DEFAULT_NODE_TAGS, False
    if text.lower() == "none":
        return (), False
    out, bad = [], False
    for item in re.split(r"[,\s]+", text):
        if not item:
            continue
        tag = item if item.startswith("tag:") else "tag:" + item
        if _TAG_RE.fullmatch(tag):
            if tag not in out and len(out) < NODE_LIST_MAX:
                out.append(tag)
        else:
            bad = True
    return tuple(out), bad


# The settings a launchd job cannot get from an EnvironmentFile (issue #117): the keys install.sh accepts for /etc/ccboard/env, in its order
# (tests/test_macos_board.py pins this tuple to install.sh's ENV_KEYS). Any other line in the file is ignored.
ENV_FILE_KEYS = (
    "PROJECTS_DIR", "CCBOARD_PORT", "TTYD_PORT", "CODE_SERVER_PORT", "CCBOARD_HTTPS_PORT", "CODE_HTTPS_PORT", "CCBOARD_ALLOWED_USERS",
    "CCBOARD_DATA_DIR", "CODE_SERVER_VERSION", "CCBOARD_PUBLIC_URL", "NTFY_URL", "NTFY_TOPIC", "NTFY_PUBLIC_URL", "NTFY_HTTPS_PORT", "NTFY_PORT",
    "CCBOARD_APPROVE_TIMEOUT", "PREVIEW_HTTPS_BASE", "CCBOARD_NODE_NAME", "CCBOARD_HUB_TOKEN", "CCBOARD_NODES", "CCBOARD_RESTIC_REPO",
    "CCBOARD_RESTIC_PASSWORD_FILE", "CCBOARD_BACKUP_PUSH", "CCBOARD_BACKUP_ONCALENDAR", "CCBOARD_BACKUP_EXTRA", "CCBOARD_RUNTIME",
    "CCBOARD_AUTO_CONTINUE", "CCBOARD_CLAUDE_MEM", "CCBOARD_MEM_PORT", "CCBOARD_MEM_HTTPS_PORT", "CCBOARD_MEM_SERVICE",
    "CCBOARD_CODEX_HOOK_TRUST", "CCBOARD_CLONE_ALLOWED_HOSTS", "CCBOARD_MCP_REMOTE", "CODEX_HOME", "CCBOARD_AUTOCLOSE_GRACE",
    "CCBOARD_CODEX_HOOKS_ASYNC", "CCBOARD_CLAUDE_ULTRACODE_FLAG", "CCBOARD_SUBAGENT_MODEL", "CCBOARD_HEADLESS_FABLE_CAP", "CCBOARD_PRICE_TABLE",
    "CCBOARD_TAILSCALE_PLACEMENT", "CCBOARD_NODE_LANES", "CCBOARD_NODE_PORTS", "CCBOARD_NODE_TAGS", "CCBOARD_NODES_POLL",
)
ENV_FILE_MAX = 1 << 20     # bytes read from the settings file


def env_file_problem(path) -> str | None:
    """Why the settings file at `path` may not be read, or None when it may (or does not exist). It holds the hub token, so it must be a
    regular file (not a link), owned by this user, with no group or world permission. Where the system has no uid (native Windows) the
    owner and mode checks are skipped. Never raises."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        return f"cannot be read ({e.strerror or type(e).__name__})"
    if stat.S_ISLNK(st.st_mode):
        return "is a symbolic link"
    if not stat.S_ISREG(st.st_mode):
        return "is not a regular file"
    uid = plat.current_uid()
    if uid is not None:
        if st.st_uid != uid:
            return "is not owned by the user the board runs as"
        if st.st_mode & 0o077:
            return f"is readable by group or others (mode {st.st_mode & 0o777:04o}); run chmod 600 on it"
    return None


def read_env_file(path, *, missing_ok: bool = True) -> dict[str, str]:
    """The ENV_FILE_KEYS lines of the settings file as {key: value}. KEY=value lines are DATA, split at the first '=': nothing is expanded,
    unquoted or run (the same reading as install.sh, which never sources /etc/ccboard/env either). A key outside ENV_FILE_KEYS, a comment, a line
    without '=' and an empty value are skipped (an empty value reads as unset, as in the installer). {} when the file is absent, or refused:
    a file that fails env_file_problem is logged with the reason and not read at all. `missing_ok=False` logs an absent file too."""
    problem = env_file_problem(path)
    if problem is not None:
        log.warning("ignoring the settings file %s: it %s", path, problem)
        return {}
    try:
        fd = os.open(path, os.O_RDONLY | plat.o_nofollow())
    except FileNotFoundError:
        if not missing_ok:
            log.warning("the settings file %s named by CCBOARD_ENV_FILE does not exist", path)
        return {}
    except OSError as e:
        log.warning("ignoring the settings file %s: it cannot be opened (%s)", path, e.strerror or type(e).__name__)
        return {}
    try:
        with os.fdopen(fd, "rb") as f:
            raw = f.read(ENV_FILE_MAX)
    except OSError as e:
        log.warning("ignoring the settings file %s: it cannot be read (%s)", path, e.strerror or type(e).__name__)
        return {}
    out: dict[str, str] = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        key, sep, value = line.partition("=")
        if not sep or key not in ENV_FILE_KEYS or not value:
            continue
        out[key] = value
    return out


def apply_env_file(env) -> dict | os._Environ:
    """`env` with the settings file's values added for every key the environment does not already set (a non-empty explicit value wins,
    as in install.sh). The file is CCBOARD_ENV_FILE when that key is present (an empty value switches the file off), else, for the
    process environment and off Linux only, <data dir>/env; a mapping a test passes in is never merged with a file unless it names one. For the process
    environment the values are put into os.environ itself (what a systemd EnvironmentFile does), so a module that reads os.environ sees
    them too; for any other mapping a merged copy is returned. A system with no such file is left exactly as it was."""
    process = env is os.environ
    if "CCBOARD_ENV_FILE" in env:
        raw = (env.get("CCBOARD_ENV_FILE") or "").strip()
        path = Path(raw) if raw else None
        explicit = True
    elif process and not plat.IS_LINUX:
        # Off Linux only: on Linux the settings live in root-owned /etc/ccboard/env (systemd) or the container's environment, and a file
        # in the data dir, which the board's own user (and every session it runs) can write, must never become a second source.
        path = Path(env.get("CCBOARD_DATA_DIR") or str(plat.default_data_dir())) / "env"
        explicit = False
    else:
        return env
    if path is None:
        return env
    fresh = {k: v for k, v in read_env_file(path, missing_ok=not explicit).items() if not env.get(k)}
    if not fresh:
        return env
    if process:
        os.environ.update(fresh)
        return os.environ
    merged = dict(env)
    merged.update(fresh)
    return merged


class Settings:
    def __init__(self, env=None):
        env = apply_env_file(os.environ if env is None else env)
        self.projects_dir = Path(env["PROJECTS_DIR"]) if "PROJECTS_DIR" in env else plat.default_projects_dir()
        self.port = int(env.get("CCBOARD_PORT", "8000"))
        self.ttyd_port = int(env.get("TTYD_PORT", "7681"))
        self.code_server_port = int(env.get("CODE_SERVER_PORT", "8080"))
        self.ccboard_https_port = int(env.get("CCBOARD_HTTPS_PORT", "443"))
        self.code_https_port = int(env.get("CODE_HTTPS_PORT", "8443"))
        self.allowed_users = parse_allowlist(env.get("CCBOARD_ALLOWED_USERS", ""))
        self.clone_allowed_hosts = parse_clone_hosts(env.get("CCBOARD_CLONE_ALLOWED_HOSTS", ""))     # hosts a clone may name although they look local or private (app/projects.py check_clone_url)
        data_dir = env.get("CCBOARD_DATA_DIR") or str(plat.default_data_dir())
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "ccboard.db"
        # Where this process runs: 'docker' (compose sets CCBOARD_RUNTIME=docker), 'systemd' (INVOCATION_ID is always
        # set by a unit), 'launchd' (a macOS LaunchAgent: CCBOARD_RUNTIME=launchd, or an XPC_SERVICE_NAME naming a ccboard label)
        # or 'host' (a dev shell). An unknown CCBOARD_RUNTIME is ignored, never trusted (app/platform.py resolve_runtime).
        runtime, _detected = plat.resolve_runtime(env)
        self.runtime = runtime
        self.image_version = (env.get("CCBOARD_IMAGE_VERSION") or "").strip()   # baked into the image by CI
        # The dev bypass is for a dev shell only: ignored under systemd (INVOCATION_ID), launchd and inside the container.
        self.dev_bypass_user = env.get("CCBOARD_DEV_BYPASS_USER") if plat.dev_bypass_allowed(env, runtime) else None
        if env.get("CCBOARD_DEV_BYPASS_USER") and self.dev_bypass_user is None:
            log.warning("ignoring CCBOARD_DEV_BYPASS_USER (runtime %s): the dev bypass is for a dev shell only, never a service or a container", runtime)
        self.tmux_socket = env.get("CCBOARD_TMUX_SOCKET", "ccboard")
        self.public_url = (env.get("CCBOARD_PUBLIC_URL") or "").strip()
        # The remote MCP endpoint /mcp (issue #13): '1' turns it on until someone flips the switch in Settings > Agents (the stored
        # choice then wins); unset or anything else = off. Off, /mcp answers 404.
        self.mcp_remote = (env.get("CCBOARD_MCP_REMOTE") or "").strip()
        self.ntfy_url = (env.get("NTFY_URL") or "").strip()            # loopback, e.g. http://127.0.0.1:2586
        self.ntfy_topic = (env.get("NTFY_TOPIC") or "ccboard").strip()
        self.ntfy_public_url = (env.get("NTFY_PUBLIC_URL") or "").strip()  # what the phone subscribes to
        try:                                                               # ntfy's tailnet HTTPS port: a preview never takes it (#44 F-01)
            self.ntfy_https_port = int(env.get("NTFY_HTTPS_PORT") or 8444)
        except ValueError:
            self.ntfy_https_port = 8444
        self.recover = (env.get("CCBOARD_RECOVER") or "1") != "0"
        self.auto_continue = (env.get("CCBOARD_AUTO_CONTINUE") or "1") != "0"   # type `continue` once a limit window resets (app/autoresume.py)
        self.node_name = (env.get("CCBOARD_NODE_NAME") or "").strip()
        self.hub_token = (env.get("CCBOARD_HUB_TOKEN") or "").strip()
        # Task lanes this node advertises (issue #133): advice for routing and a warning on a hand start, never a refusal. 0 = no advisory cap.
        # Not a whole number from 0 to 999: the default (3), and the doctor's node check says so.
        raw_lanes = (env.get("CCBOARD_NODE_LANES") or "").strip()
        self.node_lanes_bad = False
        self.node_lanes = 3
        if raw_lanes:
            if raw_lanes.isascii() and raw_lanes.isdigit() and int(raw_lanes) <= 999:
                self.node_lanes = int(raw_lanes)
            else:
                self.node_lanes_bad = True
        # Finding other nodes (issue #134): the HTTPS ports a probe tries on a tailnet device, and the tags that make a device a candidate besides
        # "same Tailscale user". Bad items are dropped and the doctor says so; nothing here starts a request.
        self.node_ports, self.node_ports_bad = parse_node_ports(env.get("CCBOARD_NODE_PORTS"))
        self.node_tags, self.node_tags_bad = parse_node_tags(env.get("CCBOARD_NODE_TAGS"))
        self.nodes_raw = env.get("CCBOARD_NODES") or ""
        # The hub read model (issue #138): seconds between two polls of one paired node. Default 20, never below 5; not a number: the default.
        try:
            poll = float((env.get("CCBOARD_NODES_POLL") or "").strip() or 20)
            if poll != poll or poll in (float("inf"), float("-inf")):
                raise ValueError("not finite")
            self.nodes_poll = max(5.0, poll)
        except ValueError:
            self.nodes_poll = 20.0
        # Nightly backup: restic repo ('off' disables restic; empty = local repo under the data dir), its password
        # file (written by install.sh), whether to copy unpushed work to backup branches on every repo's origin, extra paths to include.
        self.restic_repo = (env.get("CCBOARD_RESTIC_REPO") or "").strip() or str(self.data_dir / "restic")
        self.restic_password_file = Path((env.get("CCBOARD_RESTIC_PASSWORD_FILE") or "").strip() or (self.data_dir / "restic-password"))
        self.backup_push = (env.get("CCBOARD_BACKUP_PUSH") or "1") != "0"
        self.backup_extra = [x.strip() for x in (env.get("CCBOARD_BACKUP_EXTRA") or "").split(":") if x.strip()]
        try:
            self.preview_https_base = int(env.get("PREVIEW_HTTPS_BASE") or 9100)
        except ValueError:
            self.preview_https_base = 9100
        try:
            self.approve_timeout = float(env.get("CCBOARD_APPROVE_TIMEOUT") or 90)
        except ValueError:
            self.approve_timeout = 90.0
        # Auto-close (app/taskflow.py): seconds between a task's Stop and the graceful close of its session, so a person can still say
        # "keep open" (the countdown is flags.autoclose.due). Not a positive number: the default.
        try:
            self.autoclose_grace = float(env.get("CCBOARD_AUTOCLOSE_GRACE") or 45)
        except ValueError:
            self.autoclose_grace = 45.0
        if not 0 <= self.autoclose_grace < 24 * 3600:
            self.autoclose_grace = 45.0
        self.claude_config_dir = Path(env.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
        # The model Claude's subagents use on sessions, tasks and headless runs the board starts (CLAUDE_CODE_SUBAGENT_MODEL in their
        # environment, issue #106). Empty or `inherit` = the main model, the behaviour before this setting. An alias (haiku, sonnet, opus) or a
        # full model id; anything else is ignored, never passed on.
        raw_sub = (env.get("CCBOARD_SUBAGENT_MODEL") or "").strip()
        if raw_sub.lower() == "inherit":
            raw_sub = ""
        if raw_sub and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}(\[1m\])?$", raw_sub):
            log.warning("ignoring CCBOARD_SUBAGENT_MODEL=%r (use haiku, sonnet, opus or a full model id)", raw_sub)
            raw_sub = ""
        self.subagent_model = raw_sub
        # The most a scheduled or batch Claude run that resolves to Fable may be capped at (its Max $); a Fable job also needs its own
        # acknowledgement (issue #108). A ceiling, not a default budget: a cap is not consent.
        try:
            self.headless_fable_cap = float(env.get("CCBOARD_HEADLESS_FABLE_CAP") or 25)
        except ValueError:
            self.headless_fable_cap = 25.0
        if not 0 < self.headless_fable_cap <= 1000:
            self.headless_fable_cap = 25.0
        # Codex (app/agents/codex.py): its home (hooks.json, config.toml, sessions/ and state_*.sqlite live there; auth.json is never
        # read by the board) and how its hooks are trusted. `review` (default) relies on the one-time /hooks review in the TUI;
        # `bypass` adds --dangerously-bypass-hook-trust to every interactive launch line (and also skips the review of repo-level
        # .codex/hooks.json, which the doctor warns about). Anything else is ignored.
        self.codex_home = Path(env.get("CODEX_HOME") or (Path.home() / ".codex"))
        trust = (env.get("CCBOARD_CODEX_HOOK_TRUST") or "review").strip().lower()
        if trust not in ("review", "bypass"):
            log.warning("ignoring unknown CCBOARD_CODEX_HOOK_TRUST=%r (use review or bypass)", trust)
            trust = "review"
        self.codex_hook_trust = trust
        # claude-mem (the memory plugin): 0 skips the installer's plugin step and the health monitor (app/memory.py). The worker's
        # port is read from its own worker.pid; CCBOARD_MEM_PORT pins it (loopback only, 1-65535, anything else is ignored).
        self.claude_mem = (env.get("CCBOARD_CLAUDE_MEM") or "1") != "0"
        self.mem_port = None
        raw_mem_port = (env.get("CCBOARD_MEM_PORT") or "").strip()
        if raw_mem_port:
            try:
                p = int(raw_mem_port)
            except ValueError:
                p = 0
            if 0 < p < 65536:
                self.mem_port = p
            else:
                log.warning("ignoring CCBOARD_MEM_PORT=%r (not a port number)", raw_mem_port)
        # An optional JSON file that adds to or replaces the built-in list prices behind the Usage page's estimates (app/pricing.py, issue #95). Server side only.
        self.price_table = (env.get("CCBOARD_PRICE_TABLE") or "").strip()
        self.claude_mem_dir = Path(env.get("CLAUDE_MEM_DATA_DIR") or (Path.home() / ".claude-mem"))   # claude-mem's own override
        # The optional claude-mem viewer link (v0.5.20, issue #9): CCBOARD_MEM_HTTPS_PORT is the tailnet HTTPS port the installer would map to the worker.
        # Empty (the default) exposes nothing and hides the Memory page's link. A value that is not 1-65535, or that is a port the board already uses
        # (443 carries another service on the owner's box; the board, code-server and ntfy ports), is ignored with a warning, never used.
        self.mem_https_port = None
        raw_mem_https = (env.get("CCBOARD_MEM_HTTPS_PORT") or "").strip()
        if raw_mem_https:
            try:
                mp = int(raw_mem_https)
            except ValueError:
                mp = 0
            try:
                ntfy_port = int(env.get("NTFY_HTTPS_PORT") or 8444)
            except ValueError:
                ntfy_port = 8444
            taken = {443, self.ccboard_https_port, self.code_https_port, ntfy_port}
            if not 0 < mp < 65536:
                log.warning("ignoring CCBOARD_MEM_HTTPS_PORT=%r (not a port number)", raw_mem_https)
            elif mp in taken:
                log.warning("ignoring CCBOARD_MEM_HTTPS_PORT=%d (443 and the board's, code-server's and ntfy's ports are never used for the viewer)", mp)
            else:
                self.mem_https_port = mp
        self.login_shell = plat.login_shell()

    def mem_viewer_url(self) -> str | None:
        """https://<the board's public host>:<CCBOARD_MEM_HTTPS_PORT>/ for the Memory page's viewer link, or None: not set, or no public URL to take the host from.
        No token or path is ever part of it."""
        if not self.mem_https_port or not self.public_url:
            return None
        from urllib.parse import urlsplit
        try:
            host = urlsplit(self.public_url).hostname
        except ValueError:
            return None
        return f"https://{host}:{self.mem_https_port}/" if host else None

    def claude_bin(self) -> str | None:
        """claude on PATH, else ~/.local/bin/claude, else (a LaunchAgent's PATH is bare) the login shell's lookup and the Homebrew folders: platform.resolve_bin."""
        return plat.resolve_bin("claude")

    def codex_bin(self) -> str | None:
        """codex on PATH, else ~/.local/bin/codex (the box installs it there, and that dir is only on a login shell's PATH), else the login shell's lookup and the Homebrew folders."""
        return plat.resolve_bin("codex")

    def dev_sandboxed(self) -> bool:
        """Dev bypass on AND the data, projects, Claude config and Codex home directories are all absolute and resolve (symlinks followed)
        outside the real home. The defaults live under the home, so a default or a missing value counts as not sandboxed (issue #100).
        Both $HOME and the account's passwd home count as the real home, so pointing HOME at a temp dir does not fake a sandbox."""
        if not self.dev_bypass_user:
            return False
        homes = {Path.home().resolve()}
        try:
            pw_home = plat.passwd_home()
            if pw_home is not None:
                homes.add(pw_home.resolve())
        except OSError:
            pass
        for d in (self.data_dir, self.projects_dir, self.claude_config_dir, self.codex_home):
            if not Path(d).is_absolute():
                return False
            real = Path(d).resolve()
            if any(real == h or h in real.parents for h in homes):
                return False
        return True

    def loopback_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def validate(self) -> None:
        p = self.projects_dir
        if not p.is_absolute():
            raise SystemExit(f"PROJECTS_DIR must be absolute: {p}")
        if not p.is_dir():
            raise SystemExit(f"PROJECTS_DIR is not a directory: {p}")
        if p.resolve() in (Path("/"), Path.home().resolve()):
            raise SystemExit(f"PROJECTS_DIR must not be / or your home directory: {p}")
        if plat.owned_by_me(p) is False:                 # None (no uid on this system) skips the check
            raise SystemExit(f"PROJECTS_DIR must be owned by the ccboard user: {p}")
        if plat.under_drvfs(p):
            log.warning("PROJECTS_DIR %s is on a Windows drive (below /mnt/): file owners and change stamps are weak there and it is slow; keep projects in the WSL home instead", p)
        if not self.allowed_users and not self.dev_bypass_user:
            log.error("CCBOARD_ALLOWED_USERS is empty: every request will be denied (fail closed)")
        self.data_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()


def _main(argv: list[str]) -> int:
    """For installers: `python -m app.config keys` prints the settings-file keys, one per line; `python -m app.config check <file>` prints why
    the board would refuse that file and exits 1, or exits 0 when it is absent or acceptable."""
    if argv[:1] == ["keys"]:
        print("\n".join(ENV_FILE_KEYS))
        return 0
    if argv[:1] == ["check"] and len(argv) == 2:
        problem = env_file_problem(argv[1])
        if problem is not None:
            print(f"{argv[1]} {problem}")
            return 1
        return 0
    print("usage: python -m app.config keys | check <settings file>")
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
