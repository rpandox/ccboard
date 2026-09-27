"""Projects are folders under PROJECTS_DIR; repos are git repos inside them."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .config import settings
from .tmux import NAME_RE, SEP, valid_name

URL_RE = re.compile(r"^(https?://|ssh://|git://|git@[A-Za-z0-9._-]+:)[^\s]{1,512}$")
GIT_TIMEOUT = 2
ROOT = "root"   # reserved repo name: "the project folder itself" (a session there sees every repo below it)


class BadRequest(Exception):
    pass


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


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


def check_url(url: str) -> str:
    if not isinstance(url, str) or not URL_RE.match(url.strip()) or url.strip().startswith("-"):
        raise BadRequest("clone URL must start with https://, http://, ssh://, git:// or git@host:")
    return url.strip()


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


def _contained(path: Path) -> Path:
    """Refuse symlinks and anything that resolves outside PROJECTS_DIR."""
    root = settings.projects_dir.resolve()
    if path.is_symlink():
        raise BadRequest(f"{path.name} is a symlink; refusing")
    rp = path.resolve()
    if rp != root and root not in rp.parents:
        raise BadRequest("path escapes PROJECTS_DIR")
    return rp


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
    rp = _contained(path)
    if rp == settings.projects_dir.resolve():
        raise BadRequest("refusing to remove PROJECTS_DIR")
    # A just-killed git clone may still be deleting its own files; tolerate vanished entries once.
    shutil.rmtree(rp, ignore_errors=True)
    if rp.exists():
        shutil.rmtree(rp)


__all__ = [n for n in dir() if not n.startswith("_")]
