"""Settings from the environment. Read once at import, validated at startup."""
from __future__ import annotations

import ipaddress
import logging
import os
import pwd
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
        self.ntfy_url = (env.get("NTFY_URL") or "").strip()            # loopback, e.g. http://127.0.0.1:2586
        self.ntfy_topic = (env.get("NTFY_TOPIC") or "ccboard").strip()
        self.ntfy_public_url = (env.get("NTFY_PUBLIC_URL") or "").strip()  # what the phone subscribes to
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
        self.claude_mem_dir = Path(env.get("CLAUDE_MEM_DATA_DIR") or (Path.home() / ".claude-mem"))   # claude-mem's own override
        try:
            self.login_shell = pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"
        except KeyError:
            self.login_shell = "/bin/sh"

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
