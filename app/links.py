"""Registered local folders: only an unchanged, board-created symlink is trusted."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .config import settings
from . import projects
from .db import DB

OFF = "linking local folders is off on this board (CCBOARD_LINK_ROOTS)"
_lock = threading.RLock()
_cache = None
_database = None
_database_path = None


def _db():
    global _database, _database_path, _cache
    if _database_path != settings.db_path:
        if _database is not None:
            _database.conn.close()
        _database = None
        _cache = None
        _database_path = settings.db_path
    if _database is None:
        _database = DB(settings.db_path)
    return _database


def _rows() -> dict:
    global _cache
    with _lock:
        # Scanning an uninitialised board must not create its database.
        if not settings.db_path.exists() and _database_path != settings.db_path:
            return {}
        db = _db()
        if _cache is None:
            row = db.kv_get("linked_folders")
            _cache = row["value"] if row else {}
        return _cache


def all() -> dict:
    return {k: dict(v) for k, v in _rows().items()}


def get(key):
    row = _rows().get(key)
    return dict(row) if row else None


def enabled():
    return bool(settings.link_roots)


def require_enabled():
    if not enabled():
        raise projects.Forbidden(OFF)


def check_target(path_str) -> Path:
    require_enabled()
    if not isinstance(path_str, str) or not path_str or "\0" in path_str:
        raise projects.BadRequest("give an absolute folder path")
    try:
        path = settings.expand_user_path(path_str)
        if not path.is_absolute():
            raise projects.BadRequest("the folder path must be absolute")
        target = path.resolve()
    except (OSError, RuntimeError, ValueError):
        raise projects.BadRequest("the folder path cannot be resolved") from None
    if not target.is_dir():
        raise projects.BadRequest("the folder must be an existing directory")
    roots = [Path(r).resolve() for r in settings.link_roots]
    home = settings.home_dir.resolve()
    if target in [Path("/"), home, *roots]:
        raise projects.BadRequest("link a folder below the allowed root, not the root itself")
    if not any(r in target.parents for r in roots):
        raise projects.BadRequest("the folder is outside CCBOARD_LINK_ROOTS")
    denied = [settings.projects_dir, settings.data_dir, settings.codex_home,
              *(home / n for n in (".ssh", ".gnupg", ".claude", ".codex", ".config", ".local", "Library", ".aws", ".kube", ".docker"))]
    for d in denied:
        d = Path(d).resolve()
        if target == d or d in target.parents:
            raise projects.BadRequest("this folder is in a protected location")
    for row in all().values():
        other = Path(row["target"])
        if target == other or target in other.parents or other in target.parents:
            raise projects.Conflict("the folder overlaps an already linked folder")
    return target


def trusted(path) -> str | None:
    path = Path(path)
    try:
        key = path.absolute().relative_to(settings.projects_dir.absolute()).as_posix()
        if len(Path(key).parts) not in (1, 2):
            return None
        # A repo link must live in a real project directory, never another link.
        if "/" in key and path.parent.is_symlink():
            return None
        row = get(key)
        if row and path.is_symlink() and os.readlink(path) == row["target"]:
            # Reject a target directory that has itself been replaced by a symlink.
            if str(Path(row["target"]).resolve()) == row["target"]:
                return row["target"]
    except (OSError, ValueError, RuntimeError):
        pass
    return None


def add(project, repo, target):
    global _cache
    with _lock:
        target = check_target(str(target))
        p = projects.project_path(project)
        if repo is not None:
            projects.contained(p)
            if p.is_symlink():
                raise projects.BadRequest("a linked project cannot hold new repos")
            if not p.is_dir():
                raise projects.NotFound(f"project {project} not found")
            if projects.is_repo(p):
                raise projects.Conflict("this project folder is itself a git repo; it cannot hold more repos")
            projects.check_new_repo_name(repo)
            projects.check_case_free("repo", p, repo)
        linkpath = p / repo if repo else p
        if linkpath.exists() or linkpath.is_symlink():
            raise projects.Conflict("that name is already taken")
        key = project + ("/" + repo if repo else "")
        rows = all()
        if key in rows:
            raise projects.Conflict("that name is already registered")
        os.symlink(str(target), linkpath)
        rows[key] = {"target": str(target), "added_at": time.time()}
        try:
            _db().kv_set("linked_folders", rows)
        except Exception:
            linkpath.unlink()
            raise
        finally:
            _cache = None
        return linkpath


def remove(project, repo):
    global _cache
    with _lock:
        key = project + ("/" + repo if repo else "")
        path = projects.project_path(project)
        if repo:
            path /= projects.check_new_repo_name(repo)
        if path.exists() or path.is_symlink():
            if not trusted(path):
                raise projects.BadRequest("the linked folder has changed; refusing to unlink it")
            path.unlink()
        rows = all()
        rows.pop(key, None)
        try:
            _db().kv_set("linked_folders", rows)
        finally:
            _cache = None


def owner_of_real(realpath):
    real = Path(realpath).resolve()
    for key, row in all().items():
        target = Path(row["target"])
        if real != target and target not in real.parents:
            continue
        if not trusted(settings.projects_dir / key):
            continue
        project, _, repo = key.partition("/")
        if repo:
            return project, repo
        if projects.is_repo(target):
            return project, project
        parts = real.relative_to(target).parts
        if not parts or parts[0] in (".claude", ".ccboard"):
            return project, projects.ROOT
        try:
            return project, projects.check_name("repo", parts[0])
        except projects.BadRequest:
            return None
    return None
