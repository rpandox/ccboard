"""Settings from the environment. Read once at import, validated at startup."""
from __future__ import annotations

import hashlib
import json
import ipaddress
import logging
import math
import os
import re
import stat
import sys
from pathlib import Path

from . import platform as plat
from .options import EXTRAS, registry

log = logging.getLogger("ccboard")


def parse_mem_viewer_url(raw: str) -> tuple[str, bool]:
    """Accept a credential-free HTTPS dashboard, or HTTP on literal loopback/localhost."""
    from urllib.parse import urlsplit
    from .options import mask_urls
    text = raw.strip()
    if not text:
        return "", False
    try:
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in text) or "\\" in text or mask_urls(text) != text:
            raise ValueError
        url = urlsplit(text)
        host = url.hostname
        if not host or url.username is not None or url.password is not None or url.scheme not in ("https", "http"):
            raise ValueError
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
            raise ValueError
        if url.scheme == "http" and host != "localhost":
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError
    except ValueError:
        return "", True
    return text, False


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


NODE_AUTH_MODES = ("token", "whois", "whois+cap")
_CAP_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+/cap/[a-z0-9][a-z0-9_.-]{0,62}")


def uvicorn_host(argv) -> str | None:
    """The --host of a uvicorn command line, "127.0.0.1" (uvicorn's default) when it names none, None when argv is not a uvicorn command or it listens
    on a unix socket or a file descriptor (--uds, --fd: no TCP address to judge)."""
    try:
        args = [str(a) for a in (argv or [])]
    except TypeError:
        return None
    first = Path(args[0]) if args else None
    if first is None or not (first.name in ("uvicorn", "uvicorn.exe") or (first.name == "__main__.py" and first.parent.name == "uvicorn")):
        return None                                   # `python -m uvicorn` shows as .../uvicorn/__main__.py
    host = "127.0.0.1"
    for i, a in enumerate(args):
        if a in ("--uds", "--fd") or a.startswith(("--uds=", "--fd=")):
            return None
        if a == "--host" and i + 1 < len(args):
            host = args[i + 1]
        elif a.startswith("--host="):
            host = a.split("=", 1)[1]
    return host.strip() or None


def uvicorn_forwarded_allow(argv) -> str | None:
    """The --forwarded-allow-ips of a uvicorn command line (the addresses whose X-Forwarded-For uvicorn believes), None when argv is not a uvicorn
    command or names none (uvicorn then reads FORWARDED_ALLOW_IPS from the environment, else trusts 127.0.0.1 and ::1)."""
    try:
        args = [str(a) for a in (argv or [])]
    except TypeError:
        return None
    first = Path(args[0]) if args else None
    if first is None or not (first.name in ("uvicorn", "uvicorn.exe") or (first.name == "__main__.py" and first.parent.name == "uvicorn")):
        return None
    got = None
    for i, a in enumerate(args):
        if a == "--forwarded-allow-ips" and i + 1 < len(args):
            got = args[i + 1]
        elif a.startswith("--forwarded-allow-ips="):
            got = a.split("=", 1)[1]
    return got


def parse_node_auth(raw) -> tuple[str, bool]:
    """CCBOARD_NODE_AUTH (issue #136): `token` (the default: no extra device check), `whois` (token plus the device it was paired from) or `whois+cap`
    (that plus a Tailscale app capability). Unset or blank is `token`. Any other text is returned as it was typed (lower case) with True: it is never
    read as `token`, so a typo cannot quietly turn the check off; app/nodes_whois.py refuses every node call until it is fixed."""
    text = (raw or "").strip().lower()
    if not text:
        return "token", False
    if text in NODE_AUTH_MODES:
        return text, False
    return text[:40], True


def parse_node_cap(raw) -> tuple[str, bool]:
    """CCBOARD_NODE_CAP: the app capability name a grant gives this board, `<a domain you control>/cap/<name>`, lower case. Unset or blank is ""
    (`whois+cap` then refuses to switch on and says to set it). `tailscale.com` is reserved by Tailscale and refused. Returns (name, bad)."""
    text = (raw or "").strip().lower()
    if not text:
        return "", False
    if not _CAP_RE.fullmatch(text) or text.split("/", 1)[0] in ("tailscale.com",) or text.split("/", 1)[0].endswith(".tailscale.com"):
        return "", True
    return text, False


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


EPIC_AUTHORS_MAX = 20
_LOGIN_RE = re.compile(r"(?=.{1,39}\Z)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")      # GitHub's login rule (the same as app/github.py OWNER_RE)


def parse_epic_author_entries(raw) -> tuple[tuple[str, ...], dict[str, int], bool]:
    """CCBOARD_EPIC_AUTHORS (issue #147): GitHub logins, separated by commas or spaces, lower-cased, at most 20, whose claim comments count on an epic (and only
    when GitHub says the author can write to the repository). An entry is `login` or `login:id` (issue #157): with the numeric GitHub user id the entry pins that account,
    so a stranger who takes the login later is not the same person. Returns (logins, {login: id}, bad). Items that are not a login, an id that is not a number, and a second
    id for a login are dropped and bad is True. Empty: () and the board trusts the gh login only; the repository owner is never trusted by being the owner."""
    out, ids, bad = [], {}, False
    for item in re.split(r"[,\s]+", (raw or "").strip()):
        if not item:
            continue
        login, sep, num = item.partition(":")
        uid = int(num) if sep and num.isascii() and num.isdigit() and 0 < int(num) < 10 ** 12 else None
        if not _LOGIN_RE.fullmatch(login) or (sep and uid is None):
            bad = True
            continue
        key = login.lower()
        if uid is not None and ids.get(key, uid) != uid:
            bad = True                                                      # two ids for one login: the first stays
            continue
        if key not in out and len(out) >= EPIC_AUTHORS_MAX:
            bad = True
            continue
        if key not in out:
            out.append(key)
        if uid is not None:
            ids[key] = uid
    return tuple(out), ids, bad


def parse_epic_authors(raw) -> tuple[tuple[str, ...], bool]:
    """The logins of CCBOARD_EPIC_AUTHORS and whether an item was dropped (see parse_epic_author_entries; the ids are `Settings.epic_author_ids`)."""
    logins, _ids, bad = parse_epic_author_entries(raw)
    return logins, bad


EPIC_LEASE_MIN, EPIC_LEASE_MAX, EPIC_LEASE_DEFAULT = 10, 120, 30


def parse_epic_lease(raw) -> tuple[int, bool]:
    """CCBOARD_EPIC_LEASE_MIN (issue #148): the minutes a claim on an epic issue stays live without a renewal, 10 to 120 (default 30). Anything else is the default and
    the second value is True (the doctor says so)."""
    text = (raw or "").strip()
    if not text:
        return EPIC_LEASE_DEFAULT, False
    if text.isascii() and text.isdigit() and len(text) <= 4 and EPIC_LEASE_MIN <= int(text) <= EPIC_LEASE_MAX:
        return int(text), False
    return EPIC_LEASE_DEFAULT, True


EPIC_MAX_WINDOW_DEFAULT = 85


def parse_epic_max_window(raw) -> tuple[int, bool]:
    """CCBOARD_EPIC_MAX_WINDOW_PCT (issue #150): the rate-limit window percentage at or above which auto routing does not send an epic issue to a node, 10 to 100
    (default 85). Anything else is the default and the second value is True."""
    text = (raw or "").strip()
    if not text:
        return EPIC_MAX_WINDOW_DEFAULT, False
    if text.isascii() and text.isdigit() and len(text) <= 3 and 10 <= int(text) <= 100:
        return int(text), False
    return EPIC_MAX_WINDOW_DEFAULT, True


EPIC_REPOS_MAX = 25
_REPO_NAME_RE = re.compile(r"(?![-.])[A-Za-z0-9_.-]{1,100}")                   # GitHub's repository rule (the same as app/github.py REPO_RE)


def parse_epic_repos(raw) -> tuple[tuple[str, ...], bool]:
    """CCBOARD_EPIC_REPOS (issue #157): the `owner/name` repositories whose epics the worker may take issues from, separated by commas or spaces, lower-cased, at most 25.
    An item that is not `owner/name` by GitHub's rules is dropped and the second value is True. Empty: () and the worker refuses to start (it never means "every repository")."""
    out, bad = [], False
    for item in re.split(r"[,\s]+", (raw or "").strip()):
        if not item:
            continue
        owner, sep, name = item.partition("/")
        if sep and _LOGIN_RE.fullmatch(owner) and _REPO_NAME_RE.fullmatch(name) and name not in (".", "..") and len(out) < EPIC_REPOS_MAX:
            if item.lower() not in out:
                out.append(item.lower())
        else:
            bad = True
    return tuple(out), bad


EPIC_WORKER_POLL_DEFAULT, EPIC_WORKER_POLL_MIN, EPIC_WORKER_POLL_MAX = 90, 30, 3600


def parse_bounded(raw, default: int, lo: int, hi: int) -> tuple[int, bool]:
    """A whole number from `lo` to `hi`; empty is `default`; anything else is `default` and the second value is True (the doctor says so). Used by the worker's settings (issue #157)."""
    text = (raw or "").strip()
    if not text:
        return default, False
    if text.isascii() and text.isdigit() and len(text) <= 6 and lo <= int(text) <= hi:
        return int(text), False
    return default, True


# The installer and file reader use the registry's order; the three extras are never loaded from the file.
ENV_FILE_KEYS = tuple(o.key for o in registry() if o.key not in EXTRAS)
ENV_FILE_MAX = 1 << 20     # bytes read from the settings file


_CHANGELOG_VERSION = re.compile(r"^## (v\d+\.\d+\.\d+[a-z]?)\b", re.M)


def checkout_version(path=None) -> str:
    """The newest `## vX.Y.Z` heading of the checkout's CHANGELOG.md: the version of a native board, which has no image tag (issue #233).
    Reads the first 8 KB once; a missing or unreadable file, or no heading, gives ''."""
    p = Path(path) if path else Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            head = f.read(8192)
    except OSError:
        return ""
    m = _CHANGELOG_VERSION.search(head)
    return m.group(1) if m else ""


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


def read_env_file(path, *, missing_ok: bool = True, info=None) -> dict[str, str]:
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
        if info is not None and (not sep or key not in ENV_FILE_KEYS):
            info["other_lines"] = info.get("other_lines", 0) + 1
        if not sep or key not in ENV_FILE_KEYS or not value:
            continue
        out[key] = value
    return out


def apply_env_file(env, *, file_keys=None, info=None) -> dict | os._Environ:
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
    elif process and plat.IS_LINUX:
        # Inspect only: Linux already receives these values from the service or container environment.
        path = Path("/etc/ccboard/env")
        if env_file_problem(path) is None:
            values = read_env_file(path, info=info)
            if file_keys is not None:
                file_keys.update(k for k, v in values.items() if env.get(k) == v)
        return env
    else:
        return env
    if path is None:
        return env
    values = read_env_file(path, missing_ok=not explicit, info=info)
    fresh = {k: v for k, v in values.items() if not env.get(k)}
    if file_keys is not None:
        file_keys.update(fresh)
        if process and plat.IS_LINUX:
            file_keys.update(k for k, v in values.items() if env.get(k) == v)
    if not fresh:
        return env
    if process:
        os.environ.update(fresh)
        return os.environ
    merged = dict(env)
    merged.update(fresh)
    return merged


PAGE_FILE_MAX = 64 * 1024
# Keep these patterns identical to install.sh.
NTFY_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
NODE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$")
_page_warnings = set()


def page_path(env):
    if "CCBOARD_PAGE_FILE" in env:
        return Path(env["CCBOARD_PAGE_FILE"]) if env["CCBOARD_PAGE_FILE"] else None
    if env is os.environ:
        return Path(env.get("CCBOARD_DATA_DIR") or plat.default_data_dir()) / "settings.page.json"
    return None


def page_stamp(path) -> str:
    if path is None:
        return "disabled"
    try:
        st = os.lstat(path)
        return hashlib.sha256(str((st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)).encode()).hexdigest()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"


def read_page_file(path, *, env=None, info=None, document=None) -> dict[str, str]:
    """Read a private, bounded file; no rejected value is logged, even by a startup parser."""
    from .options import safe_key, validate_value
    info = info if info is not None else {}
    if path is None:
        return {}
    problem = env_file_problem(path)
    values = {}
    confirmed = []
    if problem is None:
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | plat.o_nofollow() | getattr(os, "O_NONBLOCK", 0)), "rb") as f:
                st = os.fstat(f.fileno())
                uid = plat.current_uid()
                if not stat.S_ISREG(st.st_mode) or (uid is not None and (st.st_uid != uid or st.st_mode & 0o077)):
                    raise ValueError("is not a private regular file owned by the board user")
                raw = f.read(PAGE_FILE_MAX + 1)
            if len(raw) > PAGE_FILE_MAX:
                raise ValueError("exceeds 64 KB")
            try:
                doc = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise ValueError("is not valid JSON") from None
            if (not isinstance(doc, dict) or type(doc.get("version")) is not int or doc["version"] != 1
                    or not isinstance(doc.get("set"), dict) or any(not isinstance(v, str) for v in doc["set"].values())):
                raise ValueError("must contain version 1 and string settings")
            confirmed = doc.get("confirmed", [])
            if not isinstance(confirmed, list) or any(not isinstance(k, str) for k in confirmed):
                raise ValueError("must contain a list of confirmed option names")
            values = doc["set"]
            if document is not None:
                document.update(doc)
        except FileNotFoundError:
            return {}
        except OSError:
            problem = "cannot be read"
        except ValueError as e:
            problem = str(e)
    if problem:
        info["problem"] = problem
        warning = (str(path), problem)
        if warning not in _page_warnings:
            _page_warnings.add(warning)
            log.warning("ignoring settings.page.json: %s", problem)
        return {}
    allowed = {o.key: o for o in registry() if o.cls in ("page", "confirm") and not o.secret}
    out = {}
    for key, value in values.items():
        reason = "not a page option" if key not in allowed else validate_value(allowed[key], value, env or {})
        if key in allowed and allowed[key].cls == "confirm":
            if (env or {}).get("CCBOARD_PAGE_WIDEN") != "1":
                reason = "CCBOARD_PAGE_WIDEN is off"
                info["ignored_widening"] = info.get("ignored_widening", 0) + 1
            elif key not in confirmed:
                reason = "not a page option; save with confirmation in Settings"
        if reason:
            # JSON keys are untrusted too; only option-shaped names can appear in diagnostics.
            name = safe_key(key, env or {}) or "unknown key"
            info.setdefault("ignored", []).append(name)
            warning = (str(path), name, reason)
            if warning not in _page_warnings:
                _page_warnings.add(warning)
                log.warning("ignoring page option %s: %s", name, reason)
        else:
            out[key] = value
    return out


def apply_page_settings(env, *, info=None):
    values = read_page_file(page_path(env), env=env, info=info)
    if info is not None:
        info["values"] = values
    return {**env, **values} if values else env


class Settings:
    def __init__(self, env=None, *, _quiet=False):
        original = os.environ if env is None else env
        self._input_env = dict(original)
        self._process_env = original is os.environ
        self.file_keys: set[str] = set()
        info = {}
        env = apply_env_file(os.environ if env is None else env, file_keys=self.file_keys, info=info)
        self.page_file = page_path(env)
        self.page_widen = env.get("CCBOARD_PAGE_WIDEN") == "1"
        self.page_info = {}
        env = apply_page_settings(env, info=self.page_info)
        self.raw = {o.key: str(env[o.key]) for o in registry() if o.key in env}
        self.other_lines = info.get("other_lines", 0)
        self.projects_dir = Path(env["PROJECTS_DIR"]) if "PROJECTS_DIR" in env else plat.default_projects_dir()
        self.port = int(env.get("CCBOARD_PORT", "8000"))
        self.ttyd_port = int(env.get("TTYD_PORT", "7681"))
        self.code_server_port = int(env.get("CODE_SERVER_PORT", "8080"))
        self.ccboard_https_port = int(env.get("CCBOARD_HTTPS_PORT", "443"))
        self.code_https_port = int(env.get("CODE_HTTPS_PORT", "8443"))
        self.allowed_users = parse_allowlist(env.get("CCBOARD_ALLOWED_USERS", ""))
        self.local_signin = env.get("CCBOARD_LOCAL_SIGNIN", "0") == "1"
        self.local_signin_hours = int(env.get("CCBOARD_LOCAL_SIGNIN_HOURS", "12"))
        if not 1 <= self.local_signin_hours <= 168:
            raise ValueError("CCBOARD_LOCAL_SIGNIN_HOURS must be between 1 and 168")
        first = next((u.strip() for u in env.get("CCBOARD_ALLOWED_USERS", "").split(",") if u.strip()), "")
        self.local_signin_user = first.lower() if first.isascii() else first

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
        self.checkout_version = "" if self.image_version else checkout_version()   # a native board: the newest CHANGELOG heading (issue #233)
        # The dev bypass is for a dev shell only: ignored under systemd (INVOCATION_ID), launchd and inside the container.
        self.dev_bypass_user = env.get("CCBOARD_DEV_BYPASS_USER") if plat.dev_bypass_allowed(env, runtime) else None
        if env.get("CCBOARD_DEV_BYPASS_USER") and self.dev_bypass_user is None:
            if not _quiet:
                log.warning("ignoring CCBOARD_DEV_BYPASS_USER (runtime %s): the dev bypass is for a dev shell only, never a service or a container", runtime)
        self.tmux_socket = env.get("CCBOARD_TMUX_SOCKET", "ccboard")
        self.public_url = (env.get("CCBOARD_PUBLIC_URL") or "").strip()
        # The remote MCP endpoint /mcp (issue #13): '1' turns it on until someone flips the switch in Settings > Agents (the stored
        # choice then wins); unset or anything else = off. Off, /mcp answers 404.
        self.mcp_remote = (env.get("CCBOARD_MCP_REMOTE") or "").strip()
        # MCP across nodes (issue #152): how far an agent's MCP tools (the stdio shim and /mcp) may act on a paired node through the relay. `read` (or the old `1`) lets them
        # list the nodes and read a node's projects and tasks; `tasks` also lets them create a task there and start it in a new session; unset, `0` or anything else = off
        # ("" here) and a tool call that names a node other than `local` is a tool error. Read at start; there is no switch in Settings, only this word.
        self.mcp_nodes = {"1": "read", "read": "read", "tasks": "tasks"}.get((env.get("CCBOARD_MCP_NODES") or "").strip().lower(), "")
        # Whose claim comments count on an epic (issue #147): GitHub logins who can also write to the repository. Empty = the login gh uses on this board only. Never the repository owner by default.
        self.epic_authors, self.epic_author_ids, self.epic_authors_bad = parse_epic_author_entries(env.get("CCBOARD_EPIC_AUTHORS"))      # `login` or `login:id`; an id pins the account (worker mode, issue #157)
        # Writes to GitHub for the claim of an epic issue (issue #148): OFF unless this is exactly "1". With it off no code path builds or runs a gh command that is not a GET.
        self.epic_writes = (env.get("CCBOARD_EPIC_WRITES") or "").strip() == "1"
        # The name this board gives itself in a claim comment on GitHub (public on a public repository): empty = a neutral word derived from the node id, never the host name.
        self.epic_alias = (env.get("CCBOARD_EPIC_ALIAS") or "").strip()
        self.epic_lease_min, self.epic_lease_bad = parse_epic_lease(env.get("CCBOARD_EPIC_LEASE_MIN"))
        # Auto routing of epic issues (issue #150): a node whose rate-limit window for the issue's agent is at or above this percentage is not chosen (an unknown reading counts as 50).
        self.epic_max_window_pct, self.epic_max_window_bad = parse_epic_max_window(env.get("CCBOARD_EPIC_MAX_WINDOW_PCT"))
        # Worker mode (issue #157): a node that takes ready issues of the listed epics by itself, unattended. OFF unless this is exactly "1"; it also needs CCBOARD_EPIC_WRITES=1, a
        # non-empty CCBOARD_EPIC_REPOS and a known allowed author, or it refuses to start. A poll below 30 s is not allowed (the default is used and the doctor says so).
        self.epic_worker = (env.get("CCBOARD_EPIC_WORKER") or "").strip() == "1"
        self.epic_repos, self.epic_repos_bad = parse_epic_repos(env.get("CCBOARD_EPIC_REPOS"))
        self.epic_worker_poll, self.epic_worker_poll_bad = parse_bounded(env.get("CCBOARD_EPIC_WORKER_POLL"), EPIC_WORKER_POLL_DEFAULT, EPIC_WORKER_POLL_MIN, EPIC_WORKER_POLL_MAX)
        self.epic_worker_max_hour, self.epic_worker_max_hour_bad = parse_bounded(env.get("CCBOARD_EPIC_WORKER_MAX_PER_HOUR"), 6, 1, 60)
        self.epic_worker_max_day, self.epic_worker_max_day_bad = parse_bounded(env.get("CCBOARD_EPIC_WORKER_MAX_PER_DAY"), 30, 1, 500)
        self.ntfy_url = (env.get("NTFY_URL") or "").strip()            # loopback, e.g. http://127.0.0.1:2586
        self.ntfy_topic = env.get("NTFY_TOPIC") if (env.get("NTFY_TOPIC") or "").strip() else "ccboard"     # empty means unset, as in the installer
        self.ntfy_topic_bad = NTFY_TOPIC_RE.fullmatch(self.ntfy_topic) is None
        if self.ntfy_topic_bad:
            self.ntfy_topic = "ccboard"
        self.ntfy_public_url = (env.get("NTFY_PUBLIC_URL") or "").strip()  # what the phone subscribes to
        try:                                                               # ntfy's tailnet HTTPS port: a preview never takes it (#44 F-01)
            self.ntfy_https_port = int(env.get("NTFY_HTTPS_PORT") or 8444)
        except ValueError:
            self.ntfy_https_port = 8444
        self.recover = (env.get("CCBOARD_RECOVER") or "1") != "0"
        self.auto_continue = (env.get("CCBOARD_AUTO_CONTINUE") or "1") != "0"   # type `continue` once a limit window resets (app/autoresume.py)
        self.node_name = env.get("CCBOARD_NODE_NAME") if (env.get("CCBOARD_NODE_NAME") or "").strip() else ""     # empty means unset, as in the installer
        self.node_name_bad = bool(self.node_name) and NODE_NAME_RE.fullmatch(self.node_name) is None
        if self.node_name_bad:
            self.node_name = ""
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
        # Issue #136: the optional device binding and app capability for calls from paired nodes (app/nodes_whois.py). Off (`token`) by default.
        self.node_keys_input = str(env.get("CCBOARD_NODE_KEYS") or "prefer")
        self.node_keys = self.node_keys_input.strip().lower()
        if self.node_keys not in ("prefer", "require", "off"):
            self.node_keys = "require"
        self.node_auth, self.node_auth_bad = parse_node_auth(env.get("CCBOARD_NODE_AUTH"))
        self.node_cap, self.node_cap_bad = parse_node_cap(env.get("CCBOARD_NODE_CAP"))
        # The address uvicorn was told to listen on, from this process's own command line (`--host`; uvicorn's default is 127.0.0.1); None when the
        # board was not started by the uvicorn command (tests, a custom runner). The board-bind check (app/nodes_whois.py) also reads the socket.
        self.bind_host = uvicorn_host(sys.argv)
        self.forwarded_allow_argv = uvicorn_forwarded_allow(sys.argv)      # issue #136: a widened uvicorn proxy trust keeps the whois modes off
        self.nodes_raw = env.get("CCBOARD_NODES") or ""
        # The hub read model: never below 5 s (a smaller number is raised to 5, as always); over an hour or not a number: the default.
        self.nodes_poll_bad = False
        try:
            poll = float((env.get("CCBOARD_NODES_POLL") or "").strip() or 20)
            if not math.isfinite(poll) or poll > 3600:
                raise ValueError("outside poll bounds")
            self.nodes_poll = max(5.0, poll)
        except ValueError:
            self.nodes_poll_bad = True
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
        self.approve_timeout_bad = False
        try:
            raw_timeout = (env.get("CCBOARD_APPROVE_TIMEOUT") or "90").strip()
            if not re.fullmatch(r"[0-9]{1,4}", raw_timeout) or not 1 <= int(raw_timeout) <= 9999:
                raise ValueError("outside approval timeout bounds")
            self.approve_timeout = int(raw_timeout)
        except ValueError:
            self.approve_timeout_bad = True
            self.approve_timeout = 90
        # Auto-close (app/taskflow.py): seconds between a task's Stop and the graceful close of its session, so a person can still say
        # "keep open" (the countdown is flags.autoclose.due). Not a positive number: the default.
        self.autoclose_grace_bad = False
        try:
            self.autoclose_grace = float(env.get("CCBOARD_AUTOCLOSE_GRACE") or 45)
        except ValueError:
            self.autoclose_grace_bad = True
            self.autoclose_grace = 45.0
        if not 0 <= self.autoclose_grace < 24 * 3600:
            self.autoclose_grace_bad = True
            self.autoclose_grace = 45.0
        self.claude_config_dir = Path(env.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
        # The model Claude's subagents use on sessions, tasks and headless runs the board starts (CLAUDE_CODE_SUBAGENT_MODEL in their
        # environment, issue #106). Empty or `inherit` = the main model, the behaviour before this setting. An alias (haiku, sonnet, opus) or a
        # full model id; anything else is ignored, never passed on.
        raw_sub = (env.get("CCBOARD_SUBAGENT_MODEL") or "").strip()
        if raw_sub.lower() == "inherit":
            raw_sub = ""
        self.subagent_model_bad = False
        if raw_sub and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}(\[1m\])?$", raw_sub):
            self.subagent_model_bad = True
            if not _quiet:
                log.warning("ignoring CCBOARD_SUBAGENT_MODEL (use haiku, sonnet, opus or a full model id)")
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
            if not _quiet:
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
                if not _quiet:
                    log.warning("ignoring CCBOARD_MEM_PORT=%r (not a port number)", raw_mem_port)
        # An optional JSON file that adds to or replaces the built-in list prices behind the Usage page's estimates (app/pricing.py, issue #95). Server side only.
        self.price_table = (env.get("CCBOARD_PRICE_TABLE") or "").strip()
        self.claude_mem_dir = Path(env.get("CLAUDE_MEM_DATA_DIR") or (Path.home() / ".claude-mem"))   # claude-mem's own override
        self.mem_viewer_url_override, self.mem_viewer_url_bad = parse_mem_viewer_url(env.get("CCBOARD_MEM_VIEWER_URL") or "")
        if self.mem_viewer_url_bad and not _quiet:
            log.warning("ignoring CCBOARD_MEM_VIEWER_URL (use HTTPS, or HTTP on loopback, without credentials)")
        # The optional claude-mem viewer link (v0.5.20, issue #9): CCBOARD_MEM_HTTPS_PORT is the tailnet HTTPS port the installer would map to the worker.
        # Empty (the default) exposes nothing. The explicit dashboard URL wins for the link. A value that is not 1-65535, or that is a port the board already uses
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
                if not _quiet:
                    log.warning("ignoring CCBOARD_MEM_HTTPS_PORT=%r (not a port number)", raw_mem_https)
            elif mp in taken:
                if not _quiet:
                    log.warning("ignoring CCBOARD_MEM_HTTPS_PORT=%d (443 and the board's, code-server's and ntfy's ports are never used for the viewer)", mp)
            else:
                self.mem_https_port = mp
        self.login_shell = plat.login_shell()

    @property
    def sources(self):
        return {o.key: "page" if o.key in self.page_info.get("values", {}) else "file" if o.key in self.file_keys
                else "environment" if self.raw.get(o.key, "").strip() else "default" for o in registry()}

    @property
    def version(self) -> str:
        """The image tag in the container, else the checkout's CHANGELOG version; '' when neither is known."""
        return self.image_version or self.checkout_version

    def mem_viewer_url(self) -> str | None:
        """An existing dashboard wins; otherwise use the optional local viewer mapping."""
        if self.mem_viewer_url_override:
            return self.mem_viewer_url_override
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
    if argv == ["open"]:
        import subprocess
        env = dict(os.environ, CCBOARD_PORT=str(settings.port), CCBOARD_DATA_DIR=str(settings.data_dir))
        return subprocess.call([str(Path(__file__).resolve().parent.parent / "bin" / "ccboard-open")], env=env)
    if argv[:1] == ["keys"]:
        print("\n".join(ENV_FILE_KEYS))
        return 0
    if argv[:1] == ["check"] and len(argv) == 2:
        problem = env_file_problem(argv[1])
        if problem is not None:
            print(f"{argv[1]} {problem}")
            return 1
        return 0
    print("usage: python -m app.config keys | check <settings file> | open")
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
