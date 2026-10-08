"""Settings from the environment. Read once at import, validated at startup."""
from __future__ import annotations

import ipaddress
import logging
import os
import pwd
import re
import shutil
from pathlib import Path

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


class Settings:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.projects_dir = Path(env.get("PROJECTS_DIR", "/srv/projects"))
        self.port = int(env.get("CCBOARD_PORT", "8000"))
        self.ttyd_port = int(env.get("TTYD_PORT", "7681"))
        self.code_server_port = int(env.get("CODE_SERVER_PORT", "8080"))
        self.ccboard_https_port = int(env.get("CCBOARD_HTTPS_PORT", "443"))
        self.code_https_port = int(env.get("CODE_HTTPS_PORT", "8443"))
        self.allowed_users = parse_allowlist(env.get("CCBOARD_ALLOWED_USERS", ""))
        self.clone_allowed_hosts = parse_clone_hosts(env.get("CCBOARD_CLONE_ALLOWED_HOSTS", ""))     # hosts a clone may name although they look local or private (app/projects.py check_clone_url)
        data_dir = env.get("CCBOARD_DATA_DIR") or str(Path.home() / ".local" / "share" / "ccboard")
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "ccboard.db"
        # Where this process runs: 'docker' (compose sets CCBOARD_RUNTIME=docker), 'systemd' (INVOCATION_ID is always
        # set by a unit) or 'host' (a dev shell). An unknown CCBOARD_RUNTIME is ignored, never trusted.
        runtime = (env.get("CCBOARD_RUNTIME") or "").strip().lower()
        if runtime not in ("docker", "systemd", "host"):
            if runtime:
                log.warning("ignoring unknown CCBOARD_RUNTIME=%r", runtime)
            runtime = "systemd" if env.get("INVOCATION_ID") else "host"
        self.runtime = runtime
        self.image_version = (env.get("CCBOARD_IMAGE_VERSION") or "").strip()   # baked into the image by CI
        # The dev bypass is for a dev shell only: ignored under systemd (INVOCATION_ID) and inside the container.
        self.dev_bypass_user = env.get("CCBOARD_DEV_BYPASS_USER") if runtime == "host" and not env.get("INVOCATION_ID") else None
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
        self.nodes_raw = env.get("CCBOARD_NODES") or ""
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
        try:
            self.login_shell = pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"
        except KeyError:
            self.login_shell = "/bin/sh"

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
        found = shutil.which("claude")
        if found:
            return found
        local = Path.home() / ".local" / "bin" / "claude"
        return str(local) if local.exists() else None

    def codex_bin(self) -> str | None:
        """codex on PATH, else ~/.local/bin/codex (the box installs it there, and that dir is only on a login shell's PATH)."""
        found = shutil.which("codex")
        if found:
            return found
        local = Path.home() / ".local" / "bin" / "codex"
        return str(local) if local.exists() else None

    def dev_sandboxed(self) -> bool:
        """Dev bypass on AND the data, projects, Claude config and Codex home directories are all absolute and resolve (symlinks followed)
        outside the real home. The defaults live under the home, so a default or a missing value counts as not sandboxed (issue #100).
        Both $HOME and the account's passwd home count as the real home, so pointing HOME at a temp dir does not fake a sandbox."""
        if not self.dev_bypass_user:
            return False
        homes = {Path.home().resolve()}
        try:
            homes.add(Path(pwd.getpwuid(os.getuid()).pw_dir).resolve())
        except (KeyError, OSError):
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
        if p.stat().st_uid != os.getuid():
            raise SystemExit(f"PROJECTS_DIR must be owned by the ccboard user: {p}")
        if not self.allowed_users and not self.dev_bypass_user:
            log.error("CCBOARD_ALLOWED_USERS is empty: every request will be denied (fail closed)")
        self.data_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
