"""The option registry, redacted Settings view and safe page-owned overrides."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json
import math
import re
import shlex
import threading

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
        Option("NTFY_TOPIC", "notify", "Notification topic", "The ntfy topic the board publishes to.", "text", "ccboard", "page", attr="ntfy_topic", bad="ntfy_topic_bad"),
        Option("NTFY_PUBLIC_URL", "notify", "Notification subscription URL", "The ntfy address a phone subscribes to.", "url", "", "installer", attr="ntfy_public_url"),
        Option("NTFY_HTTPS_PORT", "notify", "Notification HTTPS port", "The tailnet HTTPS port of ntfy.", "int", "8444", "installer", attr="ntfy_https_port", bounds=(1, 65535)),
        Option("NTFY_PORT", "notify", "Notification port", "The loopback port of ntfy.", "int", "2586", "installer", applies="installer", bounds=(1, 65535)),
        Option("CCBOARD_APPROVE_TIMEOUT", "safety", "Approval timeout", "Whole seconds (1–9999) a permission prompt waits. Set by the installer because the agents' hooks carry this timeout.", "int", "90", "installer", attr="approve_timeout", bad="approve_timeout_bad", bounds=(1, 9999)),
        Option("PREVIEW_HTTPS_BASE", "general", "First preview port", "The first tailnet HTTPS port used for task previews.", "int", "9100", "installer", attr="preview_https_base", bounds=(1, 65535)),
        Option("CCBOARD_NODE_NAME", "nodes", "Node name", "The name of this board in fleet views.", "text", "", "page", attr="node_name", bad="node_name_bad"),
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
        Option("CCBOARD_MEM_HTTPS_PORT", "memory", "Memory viewer HTTPS port", "A port exposes the local worker's writable API on the tailnet; empty leaves it unmapped.", "int", "", "installer", attr="mem_https_port", bounds=(1, 65535)),
        Option("CCBOARD_MEM_VIEWER_URL", "memory", "Memory viewer URL", "An existing dashboard URL, HTTPS or HTTP on loopback, without credentials; takes precedence over the viewer port.", "url", "", "page", attr="mem_viewer_url_override", bad="mem_viewer_url_bad"),
        Option("CCBOARD_MEM_SERVICE", "memory", "Memory service", "Run the memory worker as a managed service.", "bool", "0", "installer", applies="installer"),
        Option("CCBOARD_CODEX_HOOK_TRUST", "safety", "Codex hook trust", "Review trusts hooks once in Codex and bypass also trusts repository hooks.", "choice", "review", "confirm", attr="codex_hook_trust", choices=("review", "bypass")),
        Option("CCBOARD_CLONE_ALLOWED_HOSTS", "safety", "Private clone hosts", "Comma-separated private hosts or wildcard domains that clones may reach.", "list", "", "confirm", attr="clone_allowed_hosts"),
        Option("CCBOARD_MCP_REMOTE", "safety", "Remote MCP default", "Start remote MCP enabled only with 1 unless its page switch overrides it.", "bool", "", "confirm", attr="mcp_remote"),
        Option("CODEX_HOME", "advanced", "Codex folder", "Empty uses Codex's own default configuration folder.", "path", "", "file", attr="codex_home"),
        Option("CCBOARD_AUTOCLOSE_GRACE", "agents", "Auto-close grace", "Seconds to wait after Stop before closing a task session.", "float", "45", "page", attr="autoclose_grace", bad="autoclose_grace_bad", bounds=(0, 86400)),
        Option("CCBOARD_CODEX_HOOKS_ASYNC", "agents", "Asynchronous Codex hooks", "Write asynchronous Codex hooks unless this is 0.", "bool", "1", "installer", applies="installer"),
        Option("CCBOARD_CLAUDE_ULTRACODE_FLAG", "agents", "Ultracode support", "Empty asks Claude's help and 1 or 0 records verified flag support.", "choice", "", "file", choices=("", "0", "1")),
        Option("CCBOARD_SUBAGENT_MODEL", "agents", "Subagent model", "Empty or inherit uses the main model and a model name overrides it.", "text", "", "page", attr="subagent_model", bad="subagent_model_bad"),
        Option("CCBOARD_HEADLESS_FABLE_CAP", "agents", "Fable budget ceiling", "The largest dollar cap allowed for a scheduled or batch Fable run.", "float", "25", "confirm", attr="headless_fable_cap", bounds=(0, 1000)),
        Option("CCBOARD_PRICE_TABLE", "advanced", "Price table", "A JSON file that adds to or replaces built-in estimated prices.", "path", "", "file", attr="price_table"),
        Option("CCBOARD_TAILSCALE_PLACEMENT", "advanced", "Tailscale placement", "Choose Tailscale in WSL or on Windows when running under WSL.", "choice", "", "installer", choices=("", "wsl", "host")),
        Option("CCBOARD_NODE_LANES", "nodes", "Task lanes", "Advisory task capacity with 0 meaning no cap.", "int", "3", "page", attr="node_lanes", bad="node_lanes_bad", bounds=(0, 999)),
        Option("CCBOARD_NODE_PORTS", "nodes", "Discovery ports", "Try up to eight HTTPS ports when looking for other boards.", "list", "443,8443", "page", attr="node_ports", bad="node_ports_bad"),
        Option("CCBOARD_NODE_TAGS", "nodes", "Discovery tags", "Tailscale tags identifying candidate boards with none meaning only your own devices.", "list", "tag:ccboard", "page", attr="node_tags", bad="node_tags_bad"),
        Option("CCBOARD_NODES_POLL", "nodes", "Node poll interval", "Seconds between polls of a paired node, from five seconds to one hour.", "float", "20", "page", attr="nodes_poll", bad="nodes_poll_bad", bounds=(5, 3600)),
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
        Option("CCBOARD_LOCAL_SIGNIN", "safety", "Local sign-in", "Allow a browser on this machine to sign in with a one-time link from bin/ccboard-open.", "bool", "0", "confirm", attr="local_signin"),
        Option("CCBOARD_LOCAL_SIGNIN_HOURS", "safety", "Local session hours", "How long a local browser session lasts, from 1 to 168 hours.", "int", "12", "confirm", attr="local_signin_hours", bounds=(1, 168)),
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


def safe_key(key, raw):
    """Only option-shaped names reach diagnostics; never reflect a credential used as a key."""
    return key if isinstance(key, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", key) and not any(
        raw.get(o.key) and raw[o.key] in key for o in registry() if o.secret) else None


def redact(value, raw):
    for option in registry():
        secret = raw.get(option.key) if option.secret else None
        if secret:
            value = value.replace(secret, "***")
    return mask_urls(value)


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
            value = redact(value, settings.raw)
        elif isinstance(value, list):
            value = [redact(v, settings.raw) if isinstance(v, str) else v for v in value]
        problem = None
        if option.bad and getattr(settings, option.bad):
            if option.kind == "list":
                problem = "Invalid entries were rejected; the accepted entries or fallback are shown."
            elif option.key == "CCBOARD_NODE_AUTH":
                problem = "This authentication mode was rejected; node calls are refused."
            else:
                problem = "The configured value was rejected; the default is in use."
        copy_value = "<value>" if option.secret else redact(option.default if problem else raw, settings.raw)
        line = option.key + "=" + (shlex.quote(copy_value) if option.cls == "installer" else copy_value)
        if option.cls == "installer":
            line += " ./install.sh"
        row.update(value=None if option.secret else value, set=bool(settings.raw.get(option.key, "").strip()), line=line,
                   source=settings.sources[option.key],
                   problem=problem)
        if not option.secret and page_values and option.key in page_values:
            row.update(value=page_values[option.key], source="page", set=True)
        rows.append(row)
    return {"runtime": settings.runtime, "groups": [{"id": key, "label": label} for key, label in GROUPS],
            "options": rows, "other_lines": settings.other_lines, "restart_hint": plat.hint("restart", "ccboard")}


write_lock = threading.Lock()


def validate_value(option, value, raw) -> str | None:
    from .config import Settings, NTFY_TOPIC_RE, NODE_NAME_RE
    if not isinstance(value, str) or len(value) > 500:
        return "Use a string of at most 500 characters."
    if mask_urls(value) != value or any(raw.get(o.key) and raw[o.key] in value for o in registry() if o.secret):
        return "Put credentials in the settings file."
    if any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        return "Use text without control characters."
    text = value.strip()
    if option.key == "NTFY_TOPIC" and not NTFY_TOPIC_RE.fullmatch(value):
        return "Use 1 to 64 letters, digits, - or _."
    if option.key == "CCBOARD_NODE_NAME" and not NODE_NAME_RE.fullmatch(value):
        return "Use 1 to 41 letters, digits, - or _, starting with a letter or digit."
    if option.key == "CCBOARD_APPROVE_TIMEOUT" and (not re.fullmatch(r"[0-9]{1,4}", text) or not 1 <= int(text) <= 9999):
        return "Use a whole number from 1 to 9999."
    if option.kind == "bool" and text not in ("0", "1"):
        return "Use 0 or 1."
    if option.choices is not None and text not in option.choices:
        return "Choose one of: " + ", ".join(option.choices) + "."
    if option.kind in ("int", "float"):
        try:
            n = float(text)
        except ValueError:
            return "Use a number."
        if not math.isfinite(n) or (option.kind == "int" and not re.fullmatch(r"-?[0-9]+", text)):
            return "Use a finite whole number." if option.kind == "int" else "Use a finite number."
        if option.bounds:
            lo, hi = option.bounds
            if (lo is not None and n < lo) or (hi is not None and n > hi):
                return f"Use {lo:g} to {hi:g}." if hi is not None else f"Use at least {lo:g}."
    if option.key in ("CCBOARD_NODE_PORTS", "CCBOARD_NODE_TAGS") and len(set(re.split(r"[,\s]+", text))) > 8:
        return "Use at most eight entries."
    # Both files are disabled so validation runs the startup parsers without reading another source.
    dry = Settings({**raw, option.key: value, "CCBOARD_ENV_FILE": "", "CCBOARD_PAGE_FILE": ""}, _quiet=True)
    if option.bad and getattr(dry, option.bad):
        rules = {"CCBOARD_NODE_PORTS": "Use ports from 1 to 65535, at most eight.",
                 "CCBOARD_MEM_VIEWER_URL": "Use HTTPS, or HTTP on loopback, without credentials; leave empty for the viewer port fallback.",
                 "CCBOARD_NODE_TAGS": "Use up to eight tag:name entries, or none.",
                 "CCBOARD_SUBAGENT_MODEL": "Use inherit, a model alias or a model id of at most 80 characters.",
                 "CCBOARD_AUTOCLOSE_GRACE": "Use 0 to less than 86400."}
        return rules.get(option.key, "Use a value within the listed bounds.")
    return None


def next_settings(settings, env=None):
    from .config import Settings
    raw = dict(settings._input_env if env is None else env)
    if settings._process_env:
        for key in settings.file_keys:
            raw.pop(key, None)
        if "CCBOARD_ENV_FILE" not in raw:
            raw["CCBOARD_ENV_FILE"] = "/etc/ccboard/env" if plat.IS_LINUX else str(settings.data_dir / "env")
    raw["CCBOARD_PAGE_FILE"] = str(settings.page_file) if settings.page_file else ""
    return Settings(raw)


def pending(settings, env=None, *, next_start=None):
    if next_start is None:
        next_start = next_settings(settings, env)
    return [o.key for o in registry() if not o.secret and o.applies == "restart"
            and settings.raw.get(o.key, o.default) != next_start.raw.get(o.key, o.default)]


def pending_state(settings):
    from .config import page_stamp
    env_path = settings._input_env.get("CCBOARD_ENV_FILE")
    if env_path is None and settings._process_env:
        env_path = "/etc/ccboard/env" if plat.IS_LINUX else settings.data_dir / "env"
    stamp = (page_stamp(settings.page_file), page_stamp(env_path or None))
    cached = getattr(settings, "_pending_cache", None)
    if cached is None or cached[0] != stamp:
        keys = pending(settings)
        labels = {o.key: o.label for o in registry()}
        cached = (stamp, {"count": len(keys), "keys": keys, "labels": [labels[key] for key in keys],
                          "restart_hint": plat.hint("restart", "ccboard")})
        settings._pending_cache = cached
    return cached[1]


def listing(settings, page_values=None, db=None):
    from . import nodes
    from .config import page_stamp
    stamp = page_stamp(settings.page_file)
    next_start = next_settings(settings)
    result = view(next_start, page_values)
    running = {r["key"]: r for r in view(settings)["options"]}
    keys = pending(settings, next_start=next_start)
    for row in result["options"]:
        row["pending"] = row["key"] in keys
        row["running"] = running[row["key"]]["value"]
        row["edit_value"] = None if row["secret"] else redact(next_start.raw.get(row["key"], row["default"]), settings.raw)
    result.update(pending=keys, stamp=stamp, audit=nodes.audit_list(20, db=db, action="settings_set"),
                  refusals=nodes.audit_list(5, db=db, action="settings_refused"))
    return result


def save(settings, changes, stamp=None):
    from .config import PAGE_FILE_MAX, page_stamp, read_page_file
    with write_lock:
        path = settings.page_file
        if path is None:
            raise ValueError("Page settings are disabled.")
        before = page_stamp(path)
        if stamp is not None and stamp != before:
            raise plat.WriteRaced()
        info = {}
        values = read_page_file(path, env=settings.raw, info=info)
        if info.get("problem"):
            command = "rm -- " + shlex.quote(str(path.absolute()))
            raise ValueError(f"The page file {info['problem']}. To reset page settings, run: {command}")
        old = next_settings(settings)
        for key, value in changes.items():
            if value is None:
                values.pop(key, None)
            else:
                values[key] = value
        data = (json.dumps({"version": 1, "set": values}, ensure_ascii=True) + "\n").encode()
        if len(data) > PAGE_FILE_MAX:
            raise ValueError("The page file must be at most 64 KB.")
        path.parent.mkdir(parents=True, exist_ok=True)
        plat.secure_write(path, data, 0o600, still=lambda: page_stamp(path) == before)
        new = next_settings(settings)
        defaults = {o.key: o.default for o in registry()}
        return "; ".join(f"{k}: {redact(old.raw.get(k, defaults[k]), settings.raw)!r} -> {redact(new.raw.get(k, defaults[k]), settings.raw)!r}" for k in changes)
