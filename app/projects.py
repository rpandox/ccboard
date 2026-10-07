"""Projects are folders under PROJECTS_DIR; repos are git repos inside them."""
from __future__ import annotations

import ipaddress
import re
import shutil
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .config import settings
from .tmux import NAME_RE, SEP, valid_name

URL_RE = re.compile(r"^(https://|ssh://|git@[A-Za-z0-9._-]+:)[^\s]{1,512}$")     # the cheap shape check (the wizard's WIZ_URL_RE is this pattern); check_clone_url does the real parse
URL_MAX = 512                                                                  # characters in a whole clone URL
URL_SHAPE_MSG = "clone URL must start with https://, ssh:// or git@host:"
URL_ODD_MSG = "that is not a clone URL the board can use: write it as https://host/owner/repo.git, ssh://git@host/owner/repo.git or git@host:owner/repo.git"
URL_HOST_MSG = "that host is not allowed for clones (a local, private or tailnet address); to allow it, add it to CCBOARD_CLONE_ALLOWED_HOSTS"
GIT_TIMEOUT = 2
ROOT = "root"   # reserved repo name: "the project folder itself" (a session there sees every repo below it)


class BadRequest(Exception):
    pass


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


class Forbidden(Exception):
    """Mapped to 403 by main.py (a file the board will not show without an explicit reveal)."""


def check_name(kind: str, name: str) -> str:
    if not isinstance(name, str) or not valid_name(name):
        raise BadRequest(f"invalid {kind} name {name!r}: use letters, digits, '-' or '_', "
                         f"start and end with a letter or digit, no '--'")
    return name


def check_new_repo_name(name: str) -> str:
    check_name("repo", name)
    if name == ROOT:
        raise BadRequest(f"'{ROOT}' is reserved: it names the project folder itself (sessions in the project folder)")
    return name


def derive_repo_name(url: str) -> str | None:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    if ":" in tail:  # scp-like git@host:repo.git
        tail = tail.rsplit(":", 1)[-1]
    tail = tail.removesuffix(".git")
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", tail)
    s = re.sub(r"-{2,}", "-", s).strip("-_")[:64].rstrip("-_")
    return s if valid_name(s) else None


# Names that only mean something on this box, its LAN or the tailnet (the host rules of check_clone_url).
_INTERNAL_NAMES = ("localhost", "local", "lan", "internal", "home.arpa", "ts.net")
_NUMERIC_LABEL = re.compile(r"^(?:0x[0-9a-f]*|[0-9]+)$")                       # an all-digit or 0x last label: an address in some spelling (2130706433, 0x7f.0.0.1, 0177.0.0.1, 127.1)
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")                                 # what Tailscale hands out (100.100.100.100 is its resolver)


def is_internal_address(ip) -> bool:
    """True for an address that is the box itself, its LAN or its tailnet: loopback, RFC 1918, link-local (169.254.169.254 included), the CGNAT range
    100.64.0.0/10, unique-local IPv6 (fc00::/7, Tailscale's fd7a:115c:a1e0::/48 included), unspecified, multicast, broadcast and anything else that is not
    globally routable; an IPv4-mapped IPv6 address counts as its IPv4 address. `ip` is a string or an ipaddress object; one that does not parse counts as
    internal (fail closed)."""
    try:
        a = ipaddress.ip_address(ip.strip().strip("[]") if isinstance(ip, str) else ip)
    except ValueError:
        return True
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    if isinstance(a, ipaddress.IPv4Address) and (a in _CGNAT or a == ipaddress.IPv4Address("255.255.255.255")):
        return True
    return (not a.is_global) or a.is_loopback or a.is_private or a.is_link_local or a.is_unspecified or a.is_multicast or a.is_reserved


def _canonical_ip(host: str):
    """The address `host` is, when it is one written the plain way (dotted quad, or IPv6 without a zone), else None."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_ip_literal(host: str) -> bool:
    """An address in any spelling: IPv6 (a colon), an all-digit or 0x last label (decimal, octal, hex, short forms), or anything socket.inet_aton reads."""
    if ":" in host or _NUMERIC_LABEL.match(host.rsplit(".", 1)[-1]) or _canonical_ip(host) is not None:
        return True
    try:
        socket.inet_aton(host)
        return True
    except (OSError, ValueError, UnicodeError):
        return False


def clone_host_allowed(host: str) -> bool:
    """Is `host` (lower-case, no trailing dot) on CCBOARD_CLONE_ALLOWED_HOSTS? Exact names and IP literals, or `*.example.com` for the names below example.com."""
    entries = settings.clone_allowed_hosts
    if not entries:
        return False
    ip = _canonical_ip(host)
    if ip is not None:
        return str(ip) in entries
    if _is_ip_literal(host):
        return False                                                           # a spelling of an address that is not the plain one never matches a listed name or address
    return host in entries or any(e.startswith("*.") and host.endswith(e[1:]) for e in entries)


def _check_host(host: str) -> None:
    host = host.lower()
    if host.endswith("."):
        host = host[:-1]                                                       # one trailing dot (a fully qualified name)
    if not host or not host.isascii() or len(host) > 253:
        raise BadRequest(URL_ODD_MSG)
    if _is_ip_literal(host):
        if clone_host_allowed(host):
            return
        raise BadRequest(URL_HOST_MSG)
    if not all(_LABEL.match(label) for label in host.split(".")):              # empty labels, percent escapes, spaces, underscores
        raise BadRequest(URL_ODD_MSG)
    if clone_host_allowed(host):
        return
    if "." not in host or any(host == n or host.endswith("." + n) for n in _INTERNAL_NAMES):
        raise BadRequest(URL_HOST_MSG)


def check_clone_url(url: str) -> str:
    """The clone URL rule (it serves the clone routes and the preflight probe alike): only `https://[user[:token]@]host[:port]/path`, `ssh://[user@]host[:port]/path`
    and `git@host:path`. The host comes from a real parse, never from the regex: it must be a public-looking name (no IP literal in any spelling, no localhost, no
    single-label name, none of .local .lan .internal .home.arpa .ts.net) unless CCBOARD_CLONE_ALLOWED_HOSTS lists it. A listed host never brings back a refused scheme.
    Returns the stripped URL; BadRequest with one sentence otherwise. (A name that resolves to a private address is not caught here: that needs DNS.)"""
    if not isinstance(url, str):
        raise BadRequest(URL_SHAPE_MSG)
    u = url.strip()
    if not u or len(u) > URL_MAX or u.startswith("-") or not URL_RE.match(u):
        raise BadRequest(URL_SHAPE_MSG)
    if any(ord(c) < 33 or ord(c) == 127 for c in u) or "\\" in u:
        raise BadRequest(URL_ODD_MSG)
    if u.startswith("git@"):
        host, _, path = u[4:].partition(":")
        if not path:
            raise BadRequest(URL_ODD_MSG)
        _check_host(host)
        return u
    try:
        parts = urlsplit(u)
    except ValueError:
        raise BadRequest(URL_ODD_MSG) from None
    netloc = parts.netloc
    if parts.scheme not in ("https", "ssh") or not netloc or netloc.count("@") > 1 or len(parts.path) < 2 or not parts.path.startswith("/"):
        raise BadRequest(URL_ODD_MSG)
    user, _, hostport = netloc.rpartition("@")
    if user.startswith("-") or (parts.scheme == "ssh" and ":" in user):
        raise BadRequest(URL_ODD_MSG)
    if hostport.startswith("["):                                               # an IPv6 literal: refused below unless it is listed
        host, _, rest = hostport[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else rest
        if rest and not rest.startswith(":"):
            raise BadRequest(URL_ODD_MSG)
    else:
        host, _, port = hostport.partition(":")
    if port and not (port.isascii() and port.isdigit() and 0 < int(port) < 65536):
        raise BadRequest(URL_ODD_MSG)
    _check_host(host)
    return u


def check_url(url: str) -> str:
    """The clone URL, stripped, or BadRequest (check_clone_url holds the rules)."""
    return check_clone_url(url)


def project_path(project: str) -> Path:
    check_name("project", project)
    return settings.projects_dir / project


def is_repo(path: Path) -> bool:
    """git's own notion: a .git directory or a .git file (worktree/submodule)."""
    return (path / ".git").exists()


def repo_path(project: str, repo: str) -> Path:
    p = project_path(project)
    check_name("repo", repo)
    if repo == ROOT:
        return p  # the project folder itself
    if repo == project and is_repo(p):
        return p  # compat: the project folder itself is the repo
    return p / repo


def contained(path: Path, root: Path | None = None) -> Path:
    """Refuse a symlink at `path` and anything that resolves outside `root` (default: PROJECTS_DIR); returns the resolved path.

    Only `path` itself is checked for being a link: callers that take a relative path under `root` (tree.py) check every
    component of it as well."""
    default = root is None
    root = settings.projects_dir.resolve() if default else root.resolve()
    if path.is_symlink():
        raise BadRequest(f"{path.name} is a symlink; refusing")
    rp = path.resolve()
    if rp != root and root not in rp.parents:
        raise BadRequest("path escapes PROJECTS_DIR" if default else "path escapes its repo")
    return rp


_contained = contained   # the old private name, kept for callers that grew up with it


def _git(path: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def has_devcontainer(path: Path) -> bool:
    return (path / ".devcontainer" / "devcontainer.json").is_file() or (path / ".devcontainer.json").is_file()


def git_info(path: Path) -> dict:
    """branch/dirty for a repo dir; state 'unknown' when git is slow or missing."""
    cp = _git(path, "symbolic-ref", "--short", "-q", "HEAD")
    if cp is None:
        return {"branch": None, "dirty": None, "state": "unknown"}
    branch = cp.stdout.strip() if cp.returncode == 0 else None
    if not branch:
        cp2 = _git(path, "rev-parse", "--short", "HEAD")
        branch = f"detached {cp2.stdout.strip()}" if cp2 and cp2.returncode == 0 and cp2.stdout.strip() else "(no commits)"
    cp3 = _git(path, "status", "--porcelain")
    if cp3 is None:
        return {"branch": branch, "dirty": None, "state": "unknown"}
    return {"branch": branch, "dirty": bool(cp3.stdout.strip()), "state": "ok"}


def _subdirs(path: Path) -> list[Path]:
    out = []
    try:
        for c in sorted(path.iterdir(), key=lambda x: x.name.lower()):
            if c.name.startswith(".") or c.is_symlink() or not c.is_dir():
                continue
            if not valid_name(c.name):
                continue
            out.append(c)
    except OSError:
        pass
    return out


def scan(sessions: dict[str, dict]) -> list[dict]:
    """Full project tree. `sessions` is tmux name -> session dict (already merged with DB rows)."""
    grouped: dict[tuple[str, str], list[dict]] = {}
    for name, s in sessions.items():
        parts = name.split(SEP)
        if len(parts) == 3:
            grouped.setdefault((parts[0], parts[1]), []).append({**s, "tmux": name, "name": parts[2]})
    projects = []
    for pdir in _subdirs(settings.projects_dir):
        pname = pdir.name
        if is_repo(pdir):
            repo_dirs = [(pname, pdir)]
        else:
            repo_dirs = [(c.name, c) for c in _subdirs(pdir) if c.name != ROOT]
        repos = []
        for rname, rdir in repo_dirs:
            rsessions = sorted(grouped.pop((pname, rname), []), key=lambda s: s["created"])
            clone = next((s for s in rsessions if s["name"] == "clone"), None)
            # git creates .git before it fetches, so a live clone session decides first.
            if clone is not None and (clone.get("command") or "") == "git":
                info = {"branch": None, "dirty": None, "state": "cloning"}
            elif is_repo(rdir):
                info = git_info(rdir)
            elif clone is not None:
                info = {"branch": None, "dirty": None, "state": "clone-failed"}  # the shell shows the error
            else:
                info = {"branch": None, "dirty": None, "state": "nogit"}
            repos.append({"name": rname, "path": str(rdir), "sessions": rsessions, "devcontainer": has_devcontainer(rdir), **info})
        root_sessions = sorted(grouped.pop((pname, ROOT), []), key=lambda s: s["created"])
        root = None
        if not is_repo(pdir) or root_sessions:
            # the project folder itself (kept apart from `repos`): a session started here has every repo below in scope
            root = {"name": ROOT, "path": str(pdir), "sessions": root_sessions, "root": True, "state": "project",
                    "branch": None, "dirty": None, "devcontainer": has_devcontainer(pdir)}
        orphans = []
        for (pp, rr), ss in list(grouped.items()):
            if pp == pname:
                orphans += [{**s, "repo": rr} for s in ss]
                grouped.pop((pp, rr))
        projects.append({"name": pname, "path": str(pdir), "repos": repos, "root": root,
                         "orphan_sessions": sorted(orphans, key=lambda s: s["created"])})
    return projects


def create_project(name: str) -> Path:
    p = project_path(name)
    if p.exists():
        raise Conflict(f"project {name} already exists")
    p.mkdir(mode=0o755)
    return p


def add_repo_blank(project: str, repo: str) -> Path:
    p = project_path(project)
    if not p.is_dir():
        raise NotFound(f"project {project} not found")
    if is_repo(p):
        raise Conflict("this project folder is itself a git repo; it cannot hold more repos")
    r = p / check_new_repo_name(repo)
    if r.exists():
        raise Conflict(f"repo {repo} already exists")
    r.mkdir(mode=0o755)
    cp = subprocess.run(["git", "-C", str(r), "init", "-q", "-b", "main"], capture_output=True, text=True, timeout=10)
    if cp.returncode != 0:
        shutil.rmtree(r, ignore_errors=True)
        raise BadRequest(f"git init failed: {cp.stderr.strip()}")
    return r


def prepare_repo_clone(project: str, repo: str | None, url: str) -> tuple[str, Path]:
    """mkdir the target; the caller launches the clone session in it."""
    url = check_url(url)
    p = project_path(project)
    if not p.is_dir():
        raise NotFound(f"project {project} not found")
    if is_repo(p):
        raise Conflict("this project folder is itself a git repo; it cannot hold more repos")
    name = repo or derive_repo_name(url)
    if not name:
        raise BadRequest("could not derive a repo name from the URL; give one")
    r = p / check_new_repo_name(name)
    if r.exists():
        raise Conflict(f"repo {name} already exists")
    r.mkdir(mode=0o755)
    return name, r


def remove_tree(path: Path) -> None:
    rp = contained(path)
    if rp == settings.projects_dir.resolve():
        raise BadRequest("refusing to remove PROJECTS_DIR")
    # A just-killed git clone may still be deleting its own files; tolerate vanished entries once.
    shutil.rmtree(rp, ignore_errors=True)
    if rp.exists():
        shutil.rmtree(rp)


__all__ = [n for n in dir() if not n.startswith("_")]
