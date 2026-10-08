"""The project page's directory tree and file preview (read only).

list_dir() answers one level of one repo at a time, so the browser loads a directory when it is opened and a repo with thousands of
files never ships a thousand-row payload:

* A git repo (or a repo nested below the one asked for) is listed from ONE cached `git ls-files -z --cached --others
  --exclude-standard` run, kept sorted, and sliced per level with bisect, so a level costs O(children x log n) and never walks the
  tree below it. The cache is keyed on (.git/index mtime, .git/HEAD mtime, flags) and also expires after LS_TTL, because an
  untracked file changes neither the index nor HEAD. `ignored=True` adds a second run (`--others --ignored --directory`): ignored
  directories come back collapsed to one entry and are listed from disk when they are opened.
* The status letters come from one cached `git --no-optional-locks status --porcelain=v2 -z --branch --no-renames` (STATUS_TTL,
  STATUS_TIMEOUT). A slow status degrades to `status_stale=True` instead of blocking the tree, and the failure is cached for
  STATUS_FAIL_TTL so a slow repo does not cost STATUS_TIMEOUT per request. Every ancestor directory of a changed path is `dirty`;
  an untracked directory is one '?' entry and everything below it is '?' too.
* A plain directory (the project folder, a repo-less subfolder, or a git failure) is listed with os.scandir and a skip list.
  A directory with a .git inside is type 'repo' when the caller asks for repos.

Paths are untrusted. Every entry point goes through _resolve(): no NUL, backslash, leading slash, '..' or '.git' segment, no symlink
anywhere along the path (each component is lstat'ed), and the final realpath must lie under the repo's realpath. Nothing here writes.

Tests drive the module through two seams: `_run` (the only place git is started) and `_now` (the clock behind both TTLs).
"""
from __future__ import annotations

import bisect
import codecs
import fnmatch
import hashlib
import json
import os
import shlex
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import platform as plat
from . import projects

MAX_ENTRIES = 1500                 # per level; tests patch it
FILE_CAP = 200 * 1024              # bytes of a file the preview returns
SNIFF = 8192                       # a NUL within this many leading bytes means binary
LS_TTL = 5.0
SLOW_SCAN = 0.4                    # seconds: a cold `git ls-files` slower than this makes the untracked-cache hint (issue #112)
HINT_EVERY = 3600.0                # at most one hint per repo per hour
STATUS_TTL = 2.0
STATUS_FAIL_TTL = 10.0
STATUS_TIMEOUT = 4.0
LS_TIMEOUT = 20.0
CACHE_REPOS = 32                   # repos whose listing / status stay cached (oldest dropped first)
PATH_MAX = 4096
SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", ".cache", "dist", "build", ".next", "target"})
SECRET_PATTERNS = (".env*", "*.pem", "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*", "*credentials*", "*.key", "*secret*", ".npmrc", ".netrc", ".pgpass", "*.p12", "*.pfx", "*.keystore")

_now = time.monotonic
_timer = time.monotonic            # times the cold scan; tests patch it


class Unsupported(Exception):
    """The file is binary or not UTF-8 (the route answers 415)."""


class _GitFailed(Exception):
    pass


def flag(value) -> bool:
    """Query flags are 0/1 (also true/false); anything else is off."""
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------------ git plumbing

def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run(args: list[str], cwd: Path, timeout: float) -> bytes:
    """The one place git is started. Raises subprocess.TimeoutExpired, OSError or _GitFailed."""
    cp = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, timeout=timeout, env=_env())
    if cp.returncode != 0:
        raise _GitFailed(cp.stderr.decode("utf-8", "replace").strip()[:200])
    return cp.stdout


_GIT_ERRORS = (subprocess.TimeoutExpired, _GitFailed, OSError)

_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_LS: dict[tuple[str, bool], "_Listing"] = {}
_STATUS: dict[str, "_Status"] = {}
_HINTED: dict[str, float] = {}      # repo -> when the untracked-cache hint was last given


def _lock(repo: Path) -> threading.RLock:
    with _guard:
        return _locks.setdefault(str(repo), threading.RLock())


def _store(cache: dict, key, rec) -> None:
    """Insert and drop the oldest entries beyond CACHE_REPOS (the per-repo locks do not cover the shared dicts)."""
    with _guard:
        cache[key] = rec
        while len(cache) > CACHE_REPOS:
            cache.pop(min(cache, key=lambda k: cache[k].at))


def _gitdir(root: Path) -> Path | None:
    """The git dir of a work tree: `.git` itself, or the target of the `gitdir:` line of a .git file (worktree, submodule)."""
    g = root / ".git"
    try:
        if g.is_dir():
            return g
        if g.is_file():
            line = g.read_text("utf-8", "replace").splitlines()[0]
            if line.startswith("gitdir:"):
                d = Path(line[7:].strip())
                return d if d.is_absolute() else (root / d)
    except (OSError, IndexError):
        pass
    return None


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _stamp(root: Path) -> tuple[int, int]:
    gd = _gitdir(root)
    return (_mtime_ns(gd / "index"), _mtime_ns(gd / "HEAD")) if gd else (0, 0)


def _head_branch(root: Path) -> str | None:
    """Branch name straight from HEAD (no subprocess): the fallback when status is stale."""
    gd = _gitdir(root)
    try:
        head = (gd / "HEAD").read_text("utf-8", "replace").strip() if gd else ""
    except OSError:
        return None
    if head.startswith("ref:"):
        ref = head[4:].strip()
        return ref.removeprefix("refs/heads/")
    return f"detached {head[:7]}" if head else None


# ------------------------------------------------------------------ the ls-files listing

@dataclass
class _Listing:
    files: list          # sorted repo-relative paths; a collapsed directory is its path without the trailing slash
    ignored: frozenset   # the paths among them that git ignores (only filled when the ignored run was made)
    stamp: tuple
    at: float
    cold_s: float = 0.0  # how long the `git ls-files` that made this listing took (the ignored run adds its own)


def _split(out: bytes) -> list[str]:
    return [os.fsdecode(p).rstrip("/") for p in out.split(b"\0") if p]


def _listing(root: Path, with_ignored: bool, refresh: bool) -> _Listing | None:
    """The cached listing, or None when git could not produce one (the caller falls back to scandir)."""
    with _lock(root):
        stamp = _stamp(root)
        key = (str(root), with_ignored)
        rec = _LS.get(key)
        if rec is not None and not refresh and rec.stamp == stamp and _now() - rec.at < LS_TTL:
            return rec
        try:
            if with_ignored:
                base = _listing(root, False, refresh)
                if base is None:
                    return None
                t0 = _timer()
                ign = _split(_run(["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"], root, LS_TIMEOUT))
                rec = _Listing(sorted(set(base.files) | set(ign)), frozenset(ign), stamp, _now(), base.cold_s + (_timer() - t0))
            else:
                t0 = _timer()
                out = _run(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], root, LS_TIMEOUT)
                rec = _Listing(sorted(set(_split(out))), frozenset(), stamp, _now(), _timer() - t0)
        except _GIT_ERRORS:
            return None
        _store(_LS, key, rec)
        return rec


def _untracked_cache_on(root: Path) -> bool:
    """Is core.untrackedCache already switched on for this repo? Unset, false or unreadable all mean no. Read only."""
    try:
        v = _run(["config", "--get", "core.untrackedCache"], root, STATUS_TIMEOUT).decode("utf-8", "replace").strip().lower()
    except _GIT_ERRORS:
        return False
    return v in ("true", "yes", "on", "1", "keep")


def _untracked_hint(root: Path, listing: "_Listing") -> dict | None:
    """The one-line hint for a repo whose cold scan was slow (issue #112): {kind, cmd}. Given at most once per repo per hour, never for a fast
    repo or one that has the cache on. The board only SAYS the command: it never runs it and never writes the repo's git config."""
    if listing.cold_s <= SLOW_SCAN:
        return None
    key = str(root)
    with _guard:
        last = _HINTED.get(key)
        if last is not None and _now() - last < HINT_EVERY:
            return None
    if _untracked_cache_on(root):
        return None
    with _guard:
        _HINTED[key] = _now()
        while len(_HINTED) > CACHE_REPOS:
            _HINTED.pop(min(_HINTED, key=_HINTED.get))
    return {"kind": "untracked_cache", "cmd": f"git -C {shlex.quote(key)} config core.untrackedCache true"}


def _children(files: list, prefix: str):
    """Yield (name, has_more) for the distinct first segments below `prefix` ('' or 'a/b/') in a sorted path list.

    A name with `has_more` is a directory with listed files below it; the walk then jumps over all of them (they sort between
    'name/' and 'name0', '0' being the character after '/'), so the cost is the number of children, not of descendants."""
    n = len(files)
    pl = len(prefix)
    i = bisect.bisect_left(files, prefix) if prefix else 0
    while i < n:
        f = files[i]
        if not f.startswith(prefix):
            return
        rest = f[pl:]
        cut = rest.find("/")
        if cut < 0:
            yield rest, False
            i += 1
        else:
            name = rest[:cut]
            yield name, True
            i = bisect.bisect_left(files, prefix + name + "0", i + 1)


# ------------------------------------------------------------------ git status

@dataclass
class _Status:
    letters: dict = field(default_factory=dict)     # path -> M|A|D|U|? ; a collapsed untracked directory is its own entry
    untracked: frozenset = frozenset()              # the collapsed untracked directories (their whole subtree is '?')
    dirty: frozenset = frozenset()                  # every ancestor directory of every changed path
    paths: list = field(default_factory=list)       # sorted keys of `letters`, for _children()
    branch: str | None = None
    ahead: int = 0
    behind: int = 0
    stale: bool = False
    stamp: tuple = (0, 0)
    at: float = 0.0
    ttl: float = STATUS_TTL


def _letter(kind: str, xy: str) -> str:
    if kind == "u":
        return "U"
    if "D" in xy:
        return "D"
    if "A" in xy:
        return "A"
    return "M"


def _ancestors(path: str):
    while "/" in path:
        path = path.rsplit("/", 1)[0]
        yield path


def _parse_status(out: bytes) -> _Status:
    letters: dict[str, str] = {}
    untracked: set[str] = set()
    st = _Status()
    oid = head = None
    for raw in out.split(b"\0"):
        if not raw:
            continue
        tok = os.fsdecode(raw)
        kind = tok[0]
        if kind == "#":
            parts = tok.split(" ")
            if tok.startswith("# branch.head "):
                head = tok[len("# branch.head "):]
            elif tok.startswith("# branch.oid "):
                oid = parts[2] if len(parts) > 2 else None
            elif tok.startswith("# branch.ab ") and len(parts) >= 4:
                try:
                    st.ahead, st.behind = int(parts[2].lstrip("+")), abs(int(parts[3]))
                except ValueError:
                    pass
        elif kind == "?":
            path = tok[2:]
            if path.endswith("/"):
                path = path.rstrip("/")
                untracked.add(path)
            letters[path] = "?"
        elif kind == "1":
            f = tok.split(" ", 8)
            if len(f) == 9:
                letters[f[8]] = _letter("1", f[1])
        elif kind == "u":
            f = tok.split(" ", 10)
            if len(f) == 11:
                letters[f[10]] = "U"
        # '2' (rename) cannot occur with --no-renames; '!' (ignored) is not asked for
    dirty: set[str] = set()
    for p in letters:
        dirty.update(_ancestors(p))
    st.letters, st.untracked, st.dirty = letters, frozenset(untracked), frozenset(dirty)
    st.paths = sorted(letters)
    if head and head != "(detached)":
        st.branch = head
    elif head:
        st.branch = f"detached {oid[:7]}" if oid and oid != "(initial)" else "(no commits)"
    return st


def _status(root: Path, refresh: bool) -> _Status:
    with _lock(root):
        stamp = _stamp(root)
        key = str(root)
        old = _STATUS.get(key)
        if old is not None and not refresh and old.stamp == stamp and _now() - old.at < old.ttl:
            return old
        try:
            rec = _parse_status(_run(["--no-optional-locks", "status", "--porcelain=v2", "-z", "--branch", "--no-renames",
                                      "--untracked-files=normal"], root, STATUS_TIMEOUT))
            rec.ttl = STATUS_TTL
        except _GIT_ERRORS:
            # keep what we knew (stale letters beat none), flag it, and do not retry for a while
            rec = _Status(old.letters, old.untracked, old.dirty, old.paths, old.branch, old.ahead, old.behind) if old else _Status()
            rec.stale = True
            rec.ttl = STATUS_FAIL_TTL
        rec.stamp, rec.at = stamp, _now()
        if rec.branch is None:
            rec.branch = _head_branch(root)
        _store(_STATUS, key, rec)
        return rec


# ------------------------------------------------------------------ paths

def check_rel(path) -> list[str]:
    """A repo-relative path as clean segments, or BadRequest. '' is the repo itself; empty and '.' segments are dropped."""
    if path is None:
        path = ""
    if not isinstance(path, str) or len(path) > PATH_MAX:
        raise projects.BadRequest("bad path")
    if "\0" in path:
        raise projects.BadRequest("path contains a NUL byte")
    if "\\" in path:
        raise projects.BadRequest("path contains a backslash")
    if path.startswith("/"):
        raise projects.BadRequest("path must be relative to the repo")
    parts = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise projects.BadRequest("path may not contain '..'")
        if seg.lower() == ".git":      # case-insensitive: macOS and Windows checkouts resolve .GIT to .git
            raise projects.BadRequest("path may not contain .git")
        parts.append(seg)
    return parts


@dataclass
class _Loc:
    root: Path        # realpath of the repo (or of the project folder for 'root')
    target: Path      # the path asked for, inside root
    parts: list       # its segments below root
    eff: Path         # the deepest directory with a .git on the way to the target (root when none: then it may be no repo at all)
    sub: list         # the target's segments below eff


def _lstat(path: Path):
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        raise projects.NotFound(f"{path.name} not found")
    except PermissionError:
        raise projects.Forbidden(f"{path.name}: permission denied")
    except OSError as e:
        raise projects.NotFound(f"{path.name}: {e.strerror or e}")


def _resolve(project: str, repo: str, path) -> _Loc:
    parts = check_rel(path)
    rpath = projects.repo_path(project, repo)          # validates both names
    try:
        pst = os.lstat(projects.project_path(project))
    except OSError:
        raise projects.NotFound(f"project {project} not found")
    if stat.S_ISLNK(pst.st_mode):
        raise projects.BadRequest(f"{project} is a symlink; refusing")
    if not stat.S_ISDIR(pst.st_mode):
        raise projects.NotFound(f"project {project} not found")
    try:
        rst = os.lstat(rpath)
    except OSError:
        raise projects.NotFound(f"repo {project}/{repo} not found")
    if stat.S_ISLNK(rst.st_mode):
        raise projects.BadRequest(f"{rpath.name} is a symlink; refusing")
    if not stat.S_ISDIR(rst.st_mode):
        raise projects.NotFound(f"repo {project}/{repo} not found")
    root = projects.contained(rpath)
    cur, eff, start = root, root, 0
    for k, seg in enumerate(parts):
        cur = cur / seg
        st = _lstat(cur)
        if stat.S_ISLNK(st.st_mode):
            raise projects.BadRequest(f"{seg} is a symlink; refusing")
        if stat.S_ISDIR(st.st_mode):
            if projects.is_repo(cur):
                eff, start = cur, k + 1
        elif k < len(parts) - 1:
            raise projects.NotFound(f"{seg} is not a directory")
    target = projects.contained(cur, root)
    return _Loc(root, target, parts, eff, parts[start:])


# ------------------------------------------------------------------ list_dir

def _sort_key(e: dict):
    return (e["type"] not in ("dir", "repo"), e["name"].lower(), e["name"])


def _visible(name: str, hidden: bool) -> bool:
    return hidden or not name.startswith(".")


def _scan(dirpath: Path, hidden: bool, with_ignored: bool, below_ignored: bool) -> list[tuple]:
    """(name, lstat result or None) for the visible children of a plain directory, honouring the skip list."""
    out = []
    try:
        with os.scandir(dirpath) as it:
            for e in it:
                name = e.name
                if name == ".git" or not _visible(name, hidden):
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    st = None
                if (not below_ignored and name in SKIP_DIRS and st is not None and stat.S_ISDIR(st.st_mode)
                        and not with_ignored):
                    continue
                out.append((name, st))
    except FileNotFoundError:
        raise projects.NotFound(f"{dirpath.name} not found")
    except NotADirectoryError:
        raise projects.BadRequest(f"{dirpath.name} is not a directory")
    except PermissionError:
        raise projects.Forbidden(f"{dirpath.name}: permission denied")
    return out


def _nonempty(path, hidden: bool) -> bool:
    try:
        with os.scandir(path) as it:
            return any(e.name != ".git" and _visible(e.name, hidden) for e in it)
    except OSError:
        return False


def _display(name: str) -> str:
    """A name the JSON layer can encode: bytes that are not UTF-8 (surrogate escapes from fsdecode) become U+FFFD."""
    try:
        name.encode("utf-8")
        return name
    except UnicodeEncodeError:
        return name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _entry(name: str, kind: str, size, status, dirty, has_children, ignored) -> dict:
    return {"name": _display(name), "type": kind, "status": status, "dirty": bool(dirty), "has_children": bool(has_children),
            "ignored": bool(ignored), "size": size}


def _kind(st, abspath: str, repos: bool, has_more: bool) -> str:
    if st is None:
        return "dir" if has_more else "file"
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    if stat.S_ISDIR(st.st_mode):
        return "repo" if repos and os.path.exists(abspath + "/.git") else "dir"      # projects.is_repo, on a str
    return "file"


def list_dir(project: str, repo: str, path: str = "", *, hidden: bool = False, ignored: bool = False,
             repos: bool = True, refresh: bool = False) -> dict:
    loc = _resolve(project, repo, path)
    if not stat.S_ISDIR(os.lstat(loc.target).st_mode):
        raise projects.BadRequest(f"{loc.target.name} is not a directory")
    eff, sub = loc.eff, "/".join(loc.sub)
    is_git = projects.is_repo(eff)
    status = listing = None
    stale = False
    if is_git:
        status = _status(eff, refresh)
        listing = _listing(eff, ignored, refresh)
        stale = status.stale
        if listing is None:               # git could not list: show the disk instead
            is_git, stale = False, True
    lineage = [sub, *_ancestors(sub)] if sub else []
    below_ignored = bool(listing and listing.ignored and any(a in listing.ignored for a in lineage))
    below_skip = any(seg in SKIP_DIRS for seg in loc.sub)
    if is_git and not below_ignored:
        entries, total = _git_level(eff, sub, status, listing, hidden, repos)
    else:
        entries, total = _disk_level(loc.target, hidden, ignored, repos, below_ignored or below_skip)
    body = {
        "project": project, "repo": repo, "path": "/".join(loc.parts), "git": is_git,
        "branch": status.branch if is_git else None,
        "ahead": status.ahead if is_git else None, "behind": status.behind if is_git else None,
        "entries": entries, "truncated": total > len(entries), "total": total,
        "hidden": bool(hidden), "ignored": bool(ignored), "status_stale": stale,
    }
    body["etag"] = hashlib.sha1(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
    if is_git and listing is not None:            # after the etag: a hint appearing or leaving must not change what a 304 means
        hint = _untracked_hint(eff, listing)
        if hint:
            body["hint"] = hint
    return body


def _git_level(eff: Path, sub: str, status: _Status, listing: _Listing, hidden: bool, repos: bool) -> tuple[list, int]:
    prefix = sub + "/" if sub else ""
    kids: dict[str, bool] = {}            # name -> has files listed below it (a directory)
    for name, more in _children(listing.files, prefix):
        if _visible(name, hidden):
            kids[name] = more
    for name, more in _children(status.paths, prefix):     # gone from the index but still reported: show it with its D
        if name not in kids and _visible(name, hidden):
            kids[name] = more
    total = len(kids)
    names = sorted(kids, key=lambda n: (not kids[n], n.lower(), n))[:MAX_ENTRIES]
    level_untracked = bool(sub) and any(a in status.untracked for a in (sub, *_ancestors(sub)))
    entries = []
    base = str(eff)
    for name in names:
        full = prefix + name
        ap = f"{base}/{full}"
        try:
            st = os.lstat(ap)
        except OSError:                   # tracked but deleted in the work tree
            st = None
        kind = _kind(st, ap, repos, kids[name])
        letter = status.letters.get(full) or ("?" if level_untracked else None)
        if kind not in ("dir", "repo"):
            has_children = False
        elif kind == "repo" or full in listing.ignored:
            has_children = True
        elif kids[name]:
            has_children = hidden or any(_visible(n, hidden) for n, _ in _children(listing.files, full + "/"))
        else:
            has_children = _nonempty(ap, hidden)           # a gitlink directory: empty when the submodule is not checked out
        entries.append(_entry(name, kind, st.st_size if st is not None and kind == "file" else None, letter,
                              letter is not None or full in status.dirty, has_children, full in listing.ignored))
    entries.sort(key=_sort_key)
    return entries, total


def _disk_level(target: Path, hidden: bool, with_ignored: bool, repos: bool, below: bool) -> tuple[list, int]:
    """One level of a plain directory. `below`: inside a directory git ignores or the skip list names, where everything is 'ignored'."""
    entries = []
    base = str(target)
    for name, st in _scan(target, hidden, with_ignored, below):
        ap = f"{base}/{name}"
        kind = _kind(st, ap, repos, False)
        skipped = below or (name in SKIP_DIRS and kind in ("dir", "repo"))
        entries.append(_entry(name, kind, st.st_size if st is not None and kind == "file" else None, None, False, False, skipped))
    total = len(entries)
    entries.sort(key=_sort_key)
    entries = entries[:MAX_ENTRIES]
    for e in entries:
        if e["type"] in ("dir", "repo"):
            e["has_children"] = e["type"] == "repo" or _nonempty(f"{base}/{e['name']}", hidden)
    return entries, total


def etag_header(payload: dict) -> str:
    return f'W/"{payload["etag"]}"'


def not_modified(if_none_match: str | None, etag: str) -> bool:
    """True when an If-None-Match header names this payload (weak comparison, any of a comma-separated list, or '*')."""
    if not if_none_match:
        return False
    for tag in if_none_match.split(","):
        tag = tag.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag.strip('"') == etag:
            return True
    return False


# ------------------------------------------------------------------ read_file

def is_secret_name(name: str) -> bool:
    n = name.lower()
    return any(fnmatch.fnmatchcase(n, pat) for pat in SECRET_PATTERNS)


def read_file(project: str, repo: str, path: str, *, reveal: bool = False) -> dict:
    loc = _resolve(project, repo, path)
    if not loc.parts:
        raise projects.BadRequest("path is required")
    name = loc.parts[-1]
    if is_secret_name(name) and not reveal:
        raise projects.Forbidden(f"{name} may hold secrets: ask for it with reveal=1")
    nofollow = plat.o_nofollow()
    try:
        # O_NOFOLLOW: a link swapped in after the checks above is refused by the kernel (where the system has no such flag it is 0 and
        # the lstat below does the check); O_NONBLOCK: a FIFO cannot hang the worker
        fd = os.open(loc.target, os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        raise projects.NotFound(f"{name} not found")
    except PermissionError:
        raise projects.Forbidden(f"{name}: permission denied")
    except OSError as e:                  # ELOOP: it became a symlink
        raise projects.BadRequest(f"{name}: cannot open ({e.strerror or e})")
    try:
        if not nofollow and stat.S_ISLNK(os.lstat(loc.target).st_mode):
            raise projects.BadRequest(f"{name}: cannot open (it is a symbolic link)")
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            raise projects.BadRequest(f"{name} is a directory")
        if not stat.S_ISREG(st.st_mode):
            raise projects.BadRequest(f"{name} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as f:
        data = f.read(FILE_CAP + 1)
    truncated = len(data) > FILE_CAP
    data = data[:FILE_CAP]
    if b"\0" in data[:SNIFF]:
        raise Unsupported(f"{name} is a binary file")
    try:
        # incremental: a cut in the middle of a multi-byte character at the cap is not an error
        text = codecs.getincrementaldecoder("utf-8")("strict").decode(data, final=not truncated)
    except UnicodeDecodeError:
        raise Unsupported(f"{name} is not UTF-8 text")
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    return {"path": "/".join(loc.parts), "size": st.st_size, "mtime": int(st.st_mtime), "truncated": truncated,
            "lines": lines, "text": text}
