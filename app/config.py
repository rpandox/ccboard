"""Settings from the environment. Read once at import, validated at startup."""
from __future__ import annotations

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
        data_dir = env.get("CCBOARD_DATA_DIR") or str(Path.home() / ".local" / "share" / "ccboard")
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "ccboard.db"
        # The dev bypass is ignored under systemd, where INVOCATION_ID is always set.
        self.dev_bypass_user = env.get("CCBOARD_DEV_BYPASS_USER") if not env.get("INVOCATION_ID") else None
        self.tmux_socket = env.get("CCBOARD_TMUX_SOCKET", "ccboard")
        self.public_url = (env.get("CCBOARD_PUBLIC_URL") or "").strip()
        self.ntfy_url = (env.get("NTFY_URL") or "").strip()            # loopback, e.g. http://127.0.0.1:2586
        self.ntfy_topic = (env.get("NTFY_TOPIC") or "ccboard").strip()
        self.ntfy_public_url = (env.get("NTFY_PUBLIC_URL") or "").strip()  # what the phone subscribes to
        self.recover = (env.get("CCBOARD_RECOVER") or "1") != "0"
        try:
            self.approve_timeout = float(env.get("CCBOARD_APPROVE_TIMEOUT") or 90)
        except ValueError:
            self.approve_timeout = 90.0
        self.claude_config_dir = Path(env.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
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
