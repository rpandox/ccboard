"""The board's option registry and its read-only, redacted Settings view."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import math
import re
import shlex

from . import platform as plat

GROUPS = (
    ("general", "General"), ("agents", "Agents"), ("safety", "Permissions & safety"),
    ("nodes", "Nodes"), ("epics", "Epics & worker"), ("notify", "Notifications"),
    ("backup", "Backup"), ("memory", "Memory"), ("advanced", "Advanced"),
)
EXTRAS = {"CCBOARD_RECOVER", "CCBOARD_TMUX_SOCKET", "CCBOARD_ENV_FILE"}


@dataclass(frozen=True)
class Option:
    key: str
    group: str
    label: str
    help: str
    kind: str
    default: str
    cls: str
    applies: str = "restart"
    secret: bool = False
    attr: str | None = None
    bad: str | None = None
    bounds: tuple[float | None, float | None] | None = None
    choices: tuple[str, ...] | None = None


def registry() -> tuple[Option, ...]:
    """Installer order, followed by the three non-remembered options; resolve platform defaults without reading any settings file."""
    return (
        Option("PROJECTS_DIR", "general", "Projects folder", "The folder containing this board's projects.", "path", str(plat.default_projects_dir()), "file", attr="projects_dir"),
        Option("CCBOARD_PORT", "general", "Board port", "The loopback port of the board.", "int", "8000", "installer", attr="port", bounds=(1, 65535)),
        Option("TTYD_PORT", "general", "Terminal port", "The loopback port of the terminal service.", "int", "7681", "installer", attr="ttyd_port", bounds=(1, 65535)),
        Option("CODE_SERVER_PORT", "general", "Editor port", "The loopback port of code-server.", "int", "8080", "installer", attr="code_server_port", bounds=(1, 65535)),
        Option("CCBOARD_HTTPS_PORT", "general", "Board HTTPS port", "The tailnet HTTPS port of the board and terminal.", "int", "443", "installer", attr="ccboard_https_port", bounds=(1, 65535)),
        Option("CODE_HTTPS_PORT", "general", "Editor HTTPS port", "The tailnet HTTPS port of code-server.", "int", "8443", "installer", attr="code_https_port", bounds=(1, 65535)),
        Option("CCBOARD_ALLOWED_USERS", "safety", "Allowed users", "Comma-separated tailnet logins allowed to sign in.", "list", "", "file", attr="allowed_users"),
        Option("CCBOARD_DATA_DIR", "advanced", "Data folder", "An empty value uses the platform's default board data folder.", "path", "", "file", attr="data_dir"),
        Option("CODE_SERVER_VERSION", "advanced", "Editor version", "The code-server version installed when it is absent.", "text", "4.139.1", "installer", applies="installer"),
        Option("CCBOARD_PUBLIC_URL", "general", "Board URL", "The public board address used in outgoing links.", "url", "", "installer", attr="public_url"),
        Option("NTFY_URL", "notify", "Notification server", "The server address used to publish ntfy notices.", "url", "", "file", attr="ntfy_url"),
        Option("NTFY_TOPIC", "notify", "Notification topic", "The ntfy topic the board publishes to.", "text", "ccboard", "page", attr="ntfy_topic"),
        Option("NTFY_PUBLIC_URL", "notify", "Notification subscription URL", "The ntfy address a phone subscribes to.", "url", "", "installer", attr="ntfy_public_url"),
        Option("NTFY_HTTPS_PORT", "notify", "Notification HTTPS port", "The tailnet HTTPS port of ntfy.", "int", "8444", "installer", attr="ntfy_https_port", bounds=(1, 65535)),
        Option("NTFY_PORT", "notify", "Notification port", "The loopback port of ntfy.", "int", "2586", "installer", applies="installer", bounds=(1, 65535)),
        Option("CCBOARD_APPROVE_TIMEOUT", "safety", "Approval timeout", "Seconds a permission prompt waits for a remote answer.", "float", "90", "page", attr="approve_timeout"),
        Option("PREVIEW_HTTPS_BASE", "general", "First preview port", "The first tailnet HTTPS port used for task previews.", "int", "9100", "installer", attr="preview_https_base", bounds=(1, 65535)),
        Option("CCBOARD_NODE_NAME", "nodes", "Node name", "The name of this board in fleet views.", "text", "", "page", attr="node_name"),
        Option("CCBOARD_HUB_TOKEN", "nodes", "Legacy hub token", "The shared secret used by the legacy fleet summary endpoint.", "secret", "", "file", secret=True, attr="hub_token"),
        Option("CCBOARD_NODES", "nodes", "Legacy nodes", "Comma-separated name=URL entries for the legacy fleet.", "list", "", "file", attr="nodes_raw"),
        Option("CCBOARD_RESTIC_REPO", "backup", "Backup repository", "Empty uses the data folder's restic repository and off disables it.", "url", "", "file", attr="restic_repo"),
        Option("CCBOARD_RESTIC_PASSWORD_FILE", "backup", "Backup password file", "Empty uses restic-password in the board data folder.", "path", "", "file", attr="restic_password_file"),
        Option("CCBOARD_BACKUP_PUSH", "backup", "Push backup branches", "Copy unpushed work to backup branches on each repository's origin.", "bool", "1", "confirm", attr="backup_push"),
        Option("CCBOARD_BACKUP_ONCALENDAR", "backup", "Backup schedule", "The calendar expression for the nightly backup job.", "text", "*-*-* 02:30:00", "installer", applies="installer"),
        Option("CCBOARD_BACKUP_EXTRA", "backup", "Extra backup folders", "Colon-separated paths to include in the backup snapshot.", "list", "", "file", attr="backup_extra"),
        Option("CCBOARD_RUNTIME", "advanced", "Runtime", "Empty detects how this board process was started.", "choice", "", "installer", attr="runtime", choices=("", "host", "systemd", "docker", "launchd")),
        Option("CCBOARD_AUTO_CONTINUE", "agents", "Auto-continue", "Type continue once when a limit resets or a working session recovers.", "bool", "1", "confirm", attr="auto_continue"),
        Option("CCBOARD_CLAUDE_MEM", "memory", "Memory worker monitoring", "Install the memory plugin and monitor its worker when enabled.", "bool", "1", "installer", attr="claude_mem"),
        Option("CCBOARD_MEM_PORT", "memory", "Memory worker port", "Empty discovers the memory worker port automatically.", "int", "", "file", attr="mem_port", bounds=(1, 65535)),
        Option("CCBOARD_MEM_HTTPS_PORT", "memory", "Memory viewer HTTPS port", "Empty hides the viewer and a port exposes its writable API on the tailnet.", "int", "", "installer", attr="mem_https_port", bounds=(1, 65535)),
        Option("CCBOARD_MEM_SERVICE", "memory", "Memory service", "Run the memory worker as a managed service.", "bool", "0", "installer", applies="installer"),
        Option("CCBOARD_CODEX_HOOK_TRUST", "safety", "Codex hook trust", "Review trusts hooks once in Codex and bypass also trusts repository hooks.", "choice", "review", "confirm", attr="codex_hook_trust", choices=("review", "bypass")),
        Option("CCBOARD_CLONE_ALLOWED_HOSTS", "safety", "Private clone hosts", "Comma-separated private hosts or wildcard domains that clones may reach.", "list", "", "confirm", attr="clone_allowed_hosts"),
        Option("CCBOARD_MCP_REMOTE", "safety", "Remote MCP default", "Start remote MCP enabled only with 1 unless its page switch overrides it.", "bool", "", "confirm", attr="mcp_remote"),
        Option("CODEX_HOME", "advanced", "Codex folder", "Empty uses Codex's own default configuration folder.", "path", "", "file", attr="codex_home"),
        Option("CCBOARD_AUTOCLOSE_GRACE", "agents", "Auto-close grace", "Seconds to wait after Stop before closing a task session.", "float", "45", "page", attr="autoclose_grace", bounds=(0, 86400)),
        Option("CCBOARD_CODEX_HOOKS_ASYNC", "agents", "Asynchronous Codex hooks", "Write asynchronous Codex hooks unless this is 0.", "bool", "1", "installer", applies="installer"),
        Option("CCBOARD_CLAUDE_ULTRACODE_FLAG", "agents", "Ultracode support", "Empty asks Claude's help and 1 or 0 records verified flag support.", "choice", "", "file", choices=("", "0", "1")),
        Option("CCBOARD_SUBAGENT_MODEL", "agents", "Subagent model", "Empty or inherit uses the main model and a model name overrides it.", "text", "", "page", attr="subagent_model"),
        Option("CCBOARD_HEADLESS_FABLE_CAP", "agents", "Fable budget ceiling", "The largest dollar cap allowed for a scheduled or batch Fable run.", "float", "25", "confirm", attr="headless_fable_cap", bounds=(0, 1000)),
        Option("CCBOARD_PRICE_TABLE", "advanced", "Price table", "A JSON file that adds to or replaces built-in estimated prices.", "path", "", "file", attr="price_table"),
        Option("CCBOARD_TAILSCALE_PLACEMENT", "advanced", "Tailscale placement", "Choose Tailscale in WSL or on Windows when running under WSL.", "choice", "", "installer", choices=("", "wsl", "host")),
        Option("CCBOARD_NODE_LANES", "nodes", "Task lanes", "Advisory task capacity with 0 meaning no cap.", "int", "3", "page", attr="node_lanes", bad="node_lanes_bad", bounds=(0, 999)),
        Option("CCBOARD_NODE_PORTS", "nodes", "Discovery ports", "Try up to eight HTTPS ports when looking for other boards.", "list", "443,8443", "page", attr="node_ports", bad="node_ports_bad"),
        Option("CCBOARD_NODE_TAGS", "nodes", "Discovery tags", "Tailscale tags identifying candidate boards with none meaning only your own devices.", "list", "tag:ccboard", "page", attr="node_tags", bad="node_tags_bad"),
        Option("CCBOARD_NODES_POLL", "nodes", "Node poll interval", "Seconds between polls of a paired node with a minimum of five.", "float", "20", "page", attr="nodes_poll", bounds=(5, None)),
        Option("CCBOARD_MCP_NODES", "safety", "MCP node access", "Allow agents to read paired nodes or also start tasks there.", "choice", "0", "confirm", attr="mcp_nodes", choices=("0", "read", "tasks", "1")),
        Option("CCBOARD_EPIC_AUTHORS", "epics", "Allowed authors", "Comma-separated login or login:id entries trusted for epic work.", "list", "", "confirm", attr="epic_authors", bad="epic_authors_bad"),
        Option("CCBOARD_EPIC_WRITES", "epics", "Epic writes", "Allow the board to write claims and work updates to GitHub.", "bool", "0", "confirm", attr="epic_writes"),
        Option("CCBOARD_EPIC_ALIAS", "epics", "Public alias", "Empty uses a neutral name for this board's public claim comments.", "text", "", "page", attr="epic_alias"),
        Option("CCBOARD_EPIC_LEASE_MIN", "epics", "Claim lease", "Minutes a claim remains live without renewal.", "int", "30", "page", attr="epic_lease_min", bad="epic_lease_bad", bounds=(10, 120)),
        Option("CCBOARD_EPIC_MAX_WINDOW_PCT", "epics", "Usage window ceiling", "Skip nodes at or above this usage window percentage during routing.", "int", "85", "page", attr="epic_max_window_pct", bad="epic_max_window_bad", bounds=(10, 100)),
        Option("CCBOARD_EPIC_WORKER", "epics", "Unattended worker", "Take ready epic issues automatically when the required allowlists and writes are enabled.", "bool", "0", "confirm", attr="epic_worker"),
        Option("CCBOARD_EPIC_REPOS", "epics", "Worker repositories", "Comma-separated owner/name repositories the worker may take issues from.", "list", "", "confirm", attr="epic_repos", bad="epic_repos_bad"),
        Option("CCBOARD_EPIC_WORKER_POLL", "epics", "Worker poll interval", "Seconds between worker checks with a little jitter.", "int", "90", "page", attr="epic_worker_poll", bad="epic_worker_poll_bad", bounds=(30, 3600)),
        Option("CCBOARD_EPIC_WORKER_MAX_PER_HOUR", "epics", "Hourly start cap", "The most issues the worker starts in an hour.", "int", "6", "page", attr="epic_worker_max_hour", bad="epic_worker_max_hour_bad", bounds=(1, 60)),
        Option("CCBOARD_EPIC_WORKER_MAX_PER_DAY", "epics", "Daily start cap", "The most issues the worker starts in twenty-four hours.", "int", "30", "page", attr="epic_worker_max_day", bad="epic_worker_max_day_bad", bounds=(1, 500)),
        Option("CCBOARD_NODE_AUTH", "safety", "Node authentication", "Require a pair token alone or also its device and an app capability.", "choice", "token", "confirm", attr="node_auth", bad="node_auth_bad", choices=("token", "whois", "whois+cap")),
        Option("CCBOARD_NODE_CAP", "safety", "Node capability", "The domain/cap/name capability required by whois+cap authentication.", "text", "", "confirm", attr="node_cap", bad="node_cap_bad"),
        Option("CCBOARD_NODE_KEYS", "safety", "Node signing keys", "prefer keeps old unkeyed pairs working and upgrades them; require refuses unkeyed pairs; off turns signing off.", "choice", "prefer", "confirm", attr="node_keys", choices=("prefer", "require", "off")),
        Option("CCBOARD_RECOVER", "agents", "Session recovery", "Relaunch Claude sessions after a reboot unless this is 0.", "bool", "1", "confirm", attr="recover"),
        Option("CCBOARD_TMUX_SOCKET", "advanced", "Terminal socket", "The named tmux socket used for board sessions.", "text", "ccboard", "file", attr="tmux_socket"),
        Option("CCBOARD_ENV_FILE", "advanced", "Settings file", "An explicit path selects the settings file and an explicit empty value disables it.", "path", "", "file"),
    )


def mask_urls(value: str) -> str:
    """Redact URL credentials, including malformed userinfo and comma-separated URLs."""
    # Be conservative about malformed userinfo: /, ? and # may be part of a password.
    # The last @ wins, but never consume the next list entry or whitespace-delimited URL.
    value = re.sub(r"(//)[^\s,]*@", r"\1***@", value)
    return re.sub(r"([?&](?:token|auth|key|pass[^=&#,\s]*|pw|secret|sig|signature)=)[^&#,\s]*",
                  r"\1***", value, flags=re.IGNORECASE)


def view(settings, page_values=None) -> dict:
    rows = []
    for option in registry():
        row = asdict(option)
        row.pop("attr")
        row.pop("bad")
        raw = settings.raw.get(option.key, option.default)
        value = getattr(settings, option.attr) if option.attr else raw
        if isinstance(value, float) and not math.isfinite(value):
            value = str(value)
        if isinstance(value, Path):
            value = str(value)
        elif isinstance(value, (set, tuple)):
            value = sorted(value) if isinstance(value, set) else list(value)
        if isinstance(value, str):
            value = mask_urls(value)
        elif isinstance(value, list):
            value = [mask_urls(v) if isinstance(v, str) else v for v in value]
        problem = None
        if option.bad and getattr(settings, option.bad):
            if option.kind == "list":
                problem = "Invalid entries were rejected; the accepted entries or fallback are shown."
            elif option.key == "CCBOARD_NODE_AUTH":
                problem = "This authentication mode was rejected; node calls are refused."
            else:
                problem = "The configured value was rejected; the default is in use."
        copy_value = "<value>" if option.secret else mask_urls(option.default if problem else raw)
        line = option.key + "=" + (shlex.quote(copy_value) if option.cls == "installer" else copy_value)
        if option.cls == "installer":
            line += " ./install.sh"
        row.update(value=None if option.secret else value, set=bool(settings.raw.get(option.key, "").strip()), line=line,
                   source="file" if option.key in settings.file_keys else "environment" if settings.raw.get(option.key, "").strip() else "default",
                   problem=problem)
        if not option.secret and page_values and option.key in page_values:
            row.update(value=page_values[option.key], source="page", set=True)
        rows.append(row)
    return {"runtime": settings.runtime, "groups": [{"id": key, "label": label} for key, label in GROUPS],
            "options": rows, "other_lines": settings.other_lines, "restart_hint": plat.hint("restart", "ccboard")}
