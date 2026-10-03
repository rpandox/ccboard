"""app/tree.py and its two GET routes: one level of a repo's tree, the file preview, and the path safety around both.

Every test builds real git repos in a temp PROJECTS_DIR with subprocess (tracked, untracked, ignored, dotfile, nested, gitlink,
symlink, conflicted), so the listing, the status letters and the caches are checked against what git itself says. The two seams the
module offers are used instead of sleeping: `tree._run` (the only place git starts: wrapped to count and to delay) and `tree._now`
(the clock behind the 5 s listing TTL and the 2 s status TTL). The HTTP layer goes through lite_client.
"""
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import projects, tree

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "-c", "protocol.file.allow=always"]


def git(cwd, *args):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def write(root, rel, text=""):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def init_repo(path, files, commit=True):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    for rel, text in files.items():
        write(path, rel, text)
    if commit:
        git(path, "add", "-A")
        git(path, "commit", "-qm", "init")
    return path


FILES = {
    "README.md": "# api\n", "apple.txt": "a\n", "Banana.txt": "b\n", "Zed.txt": "z\n",
    "Assets/logo.txt": "logo", "docs/.gitkeep": "", "src/app.py": "print(1)\n", "src/util/helpers.py": "x = 1\n",
    ".gitignore": "*.log\nbuild/\n", ".github/ci.yml": "on: push\n",
}


@pytest.fixture(autouse=True)
def _clean_caches():
    for cache in (tree._LS, tree._STATUS):
        cache.clear()
    yield
    for cache in (tree._LS, tree._STATUS):
        cache.clear()


@pytest.fixture
def world(projects_dir):
    """projects/shop/api: a committed repo with a clean work tree."""
    return init_repo(projects_dir / "shop" / "api", FILES)


@pytest.fixture
def clock(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(tree, "_now", lambda: t["now"])
    return t


class GitLog:
    def __init__(self):
        self.calls = []
        self.delay = {}

    def n(self, verb):
        return sum(verb in c for c in self.calls)


@pytest.fixture
def gitlog(monkeypatch):
    log = GitLog()
    real = tree._run

    def spy(args, cwd, timeout):
        log.calls.append(list(args))
        for verb, secs in log.delay.items():
            if verb in args:
                time.sleep(secs)
        return real(args, cwd, timeout)

    monkeypatch.setattr(tree, "_run", spy)
    return log


def ls(project="shop", repo="api", path="", **kw):
    return tree.list_dir(project, repo, path, **kw)


def names(payload):
    return [e["name"] for e in payload["entries"]]


def by_name(payload):
    return {e["name"]: e for e in payload["entries"]}


def bump_index(repo, seconds=2):
    idx = repo / ".git" / "index"
    st = idx.stat()
    os.utime(idx, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


# ------------------------------------------------------------------ listing

def test_level_listing_dirs_first_case_insensitive(world):
    p = ls()
    assert names(p) == ["Assets", "docs", "src", "apple.txt", "Banana.txt", "README.md", "Zed.txt"]
    assert [e["type"] for e in p["entries"]] == ["dir"] * 3 + ["file"] * 4
    assert (p["project"], p["repo"], p["path"], p["git"], p["branch"]) == ("shop", "api", "", True, "main")
    assert (p["ahead"], p["behind"], p["truncated"], p["total"], p["status_stale"]) == (0, 0, False, 7, False)
    assert (p["hidden"], p["ignored"]) == (False, False)
    assert list(p) == ["project", "repo", "path", "git", "branch", "ahead", "behind", "entries", "truncated", "total",
                       "hidden", "ignored", "status_stale", "etag"]
    e = by_name(p)
    assert e["apple.txt"] == {"name": "apple.txt", "type": "file", "status": None, "dirty": False, "has_children": False,
                              "ignored": False, "size": 2}
    assert e["src"]["size"] is None and e["src"]["has_children"] is True and e["src"]["dirty"] is False
    assert names(ls(path="src")) == ["util", "app.py"]
    assert names(ls(path="src/util")) == ["helpers.py"]
    assert names(ls(path="src/")) == ["util", "app.py"]          # trailing slash, ./ and // are normalised away
    assert ls(path="./src//util")["path"] == "src/util"


def test_git_dir_never_listed_and_hidden_toggle(world):
    shown = names(ls())
    assert ".git" not in shown and ".gitignore" not in shown and ".github" not in shown
    hid = ls(hidden=True)
    assert names(hid) == [".github", "Assets", "docs", "src", ".gitignore", "apple.txt", "Banana.txt", "README.md", "Zed.txt"]
    assert hid["hidden"] is True and hid["total"] == 9
    assert ".git" not in names(ls(hidden=True, ignored=True))
    # docs holds only a dotfile: no caret while dotfiles are hidden, a caret once they are not
    assert by_name(ls())["docs"]["has_children"] is False
    assert by_name(ls(hidden=True))["docs"]["has_children"] is True
    assert names(ls(path="docs")) == [] and names(ls(path="docs", hidden=True)) == [".gitkeep"]
    assert names(ls(path=".github", hidden=True)) == ["ci.yml"]


def test_ignored_toggle(world):
    write(world, "debug.log", "x")
    write(world, "build/out.js", "o")
    write(world, "src/x.log", "y")
    assert "debug.log" not in names(ls()) and "build" not in names(ls())
    p = ls(ignored=True)
    e = by_name(p)
    assert e["debug.log"]["ignored"] is True and e["debug.log"]["type"] == "file"
    assert e["build"]["ignored"] is True and e["build"]["type"] == "dir" and e["build"]["has_children"] is True
    assert e["src"]["ignored"] is False and e["apple.txt"]["ignored"] is False and p["ignored"] is True
    assert by_name(ls(path="src", ignored=True))["x.log"]["ignored"] is True
    assert "x.log" not in names(ls(path="src"))
    assert [(x["name"], x["ignored"]) for x in ls(path="build", ignored=True)["entries"]] == [("out.js", True)]   # listed from disk


def test_status_letters_and_ancestor_dirty(world):
    write(world, "src/util/helpers.py", "x = 2\n")            # M, two levels down
    write(world, "added.txt", "a")
    git(world, "add", "added.txt")                           # A
    write(world, "fresh.txt", "f")                           # ?
    (world / "apple.txt").unlink()                           # D (still in the index)
    top = by_name(ls())
    assert top["added.txt"]["status"] == "A" and top["fresh.txt"]["status"] == "?" and top["apple.txt"]["status"] == "D"
    assert top["apple.txt"]["size"] is None and top["apple.txt"]["type"] == "file"
    assert all(top[n]["dirty"] for n in ("added.txt", "fresh.txt", "apple.txt"))
    assert top["README.md"]["status"] is None and top["README.md"]["dirty"] is False
    assert top["src"]["dirty"] is True and top["src"]["status"] is None          # an ancestor: dirty, no letter
    assert top["Assets"]["dirty"] is False and top["docs"]["dirty"] is False
    src = by_name(ls(path="src"))
    assert src["util"]["dirty"] is True and src["app.py"]["dirty"] is False
    assert by_name(ls(path="src/util"))["helpers.py"]["status"] == "M"


def test_unmerged_staged_delete_and_branch_state(projects_dir):
    repo = init_repo(projects_dir / "shop" / "conf", {"f.txt": "1\n", "keep.txt": "k\n", "gone.txt": "g\n"})
    git(repo, "checkout", "-qb", "other")
    write(repo, "f.txt", "2\n")
    git(repo, "commit", "-qam", "2")
    git(repo, "checkout", "-q", "main")
    write(repo, "f.txt", "3\n")
    git(repo, "commit", "-qam", "3")
    subprocess.run([*GIT, "merge", "other"], cwd=repo, capture_output=True)     # conflicts, exit 1
    git(repo, "rm", "-q", "gone.txt")                                           # staged delete: gone from the index and the disk
    e = by_name(ls(repo="conf"))
    assert e["f.txt"]["status"] == "U" and e["f.txt"]["dirty"] is True
    assert e["gone.txt"]["status"] == "D" and e["gone.txt"]["size"] is None     # reported by status only, still shown
    assert e["keep.txt"]["status"] is None
    git(repo, "merge", "--abort")
    git(repo, "checkout", "-q", "--detach")
    p = ls(repo="conf", refresh=True)
    assert p["branch"].startswith("detached ") and len(p["branch"]) == len("detached ") + 7


def test_ahead_and_behind_from_status(projects_dir):
    bare = projects_dir.parent / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    repo = init_repo(projects_dir / "shop" / "up", {"a.txt": "a"})
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "-u", "origin", "main")
    other = projects_dir.parent / "other"
    git(projects_dir.parent, "clone", "-q", str(bare), str(other))
    write(other, "o.txt", "o")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "o")
    git(other, "push", "-q")
    write(repo, "l1.txt", "1")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "l1")
    write(repo, "l2.txt", "2")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "l2")
    git(repo, "fetch", "-q")
    p = ls(repo="up")
    assert (p["branch"], p["ahead"], p["behind"]) == ("main", 2, 1)


def test_collapsed_untracked_directory(world):
    write(world, "newdir/a.txt", "a")
    write(world, "newdir/sub/b.txt", "b")
    top = by_name(ls())
    assert top["newdir"]["type"] == "dir" and top["newdir"]["status"] == "?" and top["newdir"]["dirty"] is True
    assert top["newdir"]["has_children"] is True
    assert top["src"]["status"] is None and top["src"]["dirty"] is False            # siblings are untouched
    inner = by_name(ls(path="newdir"))
    assert {n: (e["status"], e["dirty"]) for n, e in inner.items()} == {"sub": ("?", True), "a.txt": ("?", True)}
    assert by_name(ls(path="newdir/sub"))["b.txt"]["status"] == "?"
    # the status itself carries ONE entry for the whole directory
    st = tree._status(world.resolve(), refresh=False)
    assert st.letters == {"newdir": "?"} and st.untracked == {"newdir"}


def test_parse_status_records():
    out = (b"# branch.oid 0123456789abcdef\0# branch.head feat\0# branch.upstream origin/feat\0# branch.ab +2 -3\0"
           b"1 .M N... 100644 100644 100644 aa bb src/my file.py\0"
           b"1 AM N... 000000 100644 100644 00 bb new.py\0"
           b"1 D. N... 100644 000000 000000 aa 00 old.py\0"
           b"u UU N... 100644 100644 100644 100644 aa bb cc conflict.txt\0"
           b"? x.txt\0? new dir/\0")
    st = tree._parse_status(out)
    assert st.letters == {"src/my file.py": "M", "new.py": "A", "old.py": "D", "conflict.txt": "U", "x.txt": "?", "new dir": "?"}
    assert st.untracked == {"new dir"} and st.dirty == {"src"}
    assert (st.branch, st.ahead, st.behind) == ("feat", 2, 3)
    assert st.paths == sorted(st.letters)
    assert tree._parse_status(b"# branch.oid (initial)\0# branch.head main\0").branch == "main"
    assert tree._parse_status(b"# branch.oid 0123456789abcdef\0# branch.head (detached)\0").branch == "detached 0123456"


def test_truncation_at_max_entries(world, monkeypatch):
    monkeypatch.setattr(tree, "MAX_ENTRIES", 50)
    for i in range(10):
        write(world, f"big/d{i:02d}/x.txt", "x")
    for i in range(110):
        write(world, f"big/f{i:03d}.txt", "x")
    git(world, "add", "-A")
    p = ls(path="big")
    assert len(p["entries"]) == 50 and p["total"] == 120 and p["truncated"] is True
    assert [e["type"] for e in p["entries"]] == ["dir"] * 10 + ["file"] * 40       # the cap cuts files, never directories first
    assert names(p)[10] == "f000.txt" and names(p)[-1] == "f039.txt"
    small = ls(path="src")
    assert small["truncated"] is False and small["total"] == 2


def test_children_walk_does_not_touch_every_descendant():
    class Counting(list):
        reads = 0

        def __getitem__(self, i):
            Counting.reads += 1
            return super().__getitem__(i)

    files = Counting(sorted([f"a/{i:05d}" for i in range(6000)] + [f"b/{i:05d}" for i in range(6000)] + ["c.txt", "d/e/f"]))
    got = list(tree._children(files, ""))
    assert got == [("a", True), ("b", True), ("c.txt", False), ("d", True)]
    assert Counting.reads < 120                                                  # not 12000
    Counting.reads = 0
    assert [n for n, _ in tree._children(files, "a/")][:2] == ["00000", "00001"]
    assert list(tree._children(files, "d/")) == [("e", True)]
    assert list(tree._children(files, "zz/")) == []
    assert list(tree._children(files, "b0/")) == []                              # a prefix that merely sorts nearby


# ------------------------------------------------------------------ plain directories, the project folder, nested repos

@pytest.fixture
def mono(projects_dir):
    """projects/mono is not a repo: api (git), plain (no git), the usual junk, and loose files."""
    p = projects_dir / "mono"
    init_repo(p / "api", {"a.txt": "a"})
    write(p, "plain/readme.txt", "r")
    for junk in ("node_modules", ".venv", "venv", "__pycache__", ".cache", "dist", "build", ".next", "target"):
        write(p, f"{junk}/inner/x.js", "j")
    write(p, "notes.txt", "n")
    write(p, ".env", "K=V")
    write(p, "build.sh", "#!/bin/sh")                          # a FILE named like junk is not skipped
    return p


def test_project_folder_scandir_skip_list(mono):
    p = ls("mono", "root")
    assert p["git"] is False and p["branch"] is None and p["ahead"] is None and p["status_stale"] is False
    assert names(p) == ["api", "plain", "build.sh", "notes.txt"]
    assert [e["type"] for e in p["entries"]] == ["repo", "dir", "file", "file"]
    assert all(e["status"] is None and e["dirty"] is False and e["ignored"] is False for e in p["entries"])
    assert by_name(p)["notes.txt"]["size"] == 1
    shown = ls("mono", "root", hidden=True)
    assert names(shown) == ["api", "plain", ".env", "build.sh", "notes.txt"]     # still no junk, still no .git
    both = ls("mono", "root", hidden=True, ignored=True)
    e = by_name(both)
    for junk in ("node_modules", ".venv", "venv", "__pycache__", ".cache", "dist", "build", ".next", "target"):
        assert e[junk]["type"] == "dir" and e[junk]["ignored"] is True and e[junk]["has_children"] is True
    assert e["api"]["ignored"] is False and e["notes.txt"]["ignored"] is False and ".git" not in e
    assert [x["ignored"] for x in ls("mono", "root", path="node_modules")["entries"]] == [True]   # everything below is ignored too
    assert names(ls("mono", "root", path="plain")) == ["readme.txt"]


def test_nested_repo_is_type_repo_only_when_asked(mono):
    assert by_name(ls("mono", "root", repos=True))["api"]["type"] == "repo"
    assert by_name(ls("mono", "root", repos=False))["api"]["type"] == "dir"
    assert by_name(ls("mono", "root", repos=True))["plain"]["type"] == "dir"
    # opening the repo entry in place lists it through its own git
    inner = ls("mono", "root", path="api")
    assert inner["git"] is True and inner["branch"] == "main" and names(inner) == ["a.txt"] and inner["path"] == "api"
    write(mono / "api", "a.txt", "changed")
    assert by_name(ls("mono", "root", path="api", refresh=True))["a.txt"]["status"] == "M"
    # a repo's own tree and the same repo reached through root agree
    assert names(ls("mono", "api")) == names(inner)


def test_gitlink_untracked_nested_and_gitfile_repos(world, projects_dir):
    sub = init_repo(projects_dir / "scratch" / "lib", {"lib.py": "l", "pkg/m.py": "m"})
    (world / "vendor").mkdir()
    subprocess.run([*GIT, "clone", "-q", str(sub), str(world / "vendor" / "lib")], check=True, capture_output=True)
    git(world, "add", "vendor/lib")                                  # a gitlink, the shape of a submodule
    git(world, "commit", "-qm", "gitlink")
    init_repo(world / "scratch-repo", {"s.txt": "s"})                # an untracked nested repo (`scratch-repo/` in ls-files)
    sep = projects_dir.parent / "sep.gitdir"
    (world / "sep").mkdir()
    git(world / "sep", "init", "-q", "-b", "main", f"--separate-git-dir={sep}")   # .git is a FILE here
    write(world, "sep/s.txt", "s")
    assert (world / "sep" / ".git").is_file()
    top = by_name(ls())
    assert top["scratch-repo"]["type"] == "repo" and top["sep"]["type"] == "repo"
    assert by_name(ls(path="vendor"))["lib"]["type"] == "repo"
    assert by_name(ls(path="vendor"))["lib"]["has_children"] is True
    off = ls(repos=False)
    assert by_name(off)["scratch-repo"]["type"] == "dir" and by_name(off)["sep"]["type"] == "dir"
    assert by_name(ls(path="vendor", repos=False))["lib"]["type"] == "dir"
    # listing inside each goes through the nested repo's own git
    for rel, expect in (("vendor/lib", ["pkg", "lib.py"]), ("scratch-repo", ["s.txt"]), ("sep", ["s.txt"])):
        p = ls(path=rel)
        assert p["git"] is True and p["branch"] == "main" and names(p) == expect, rel
    assert by_name(ls(path="sep"))["s.txt"]["status"] == "?"
    assert names(ls(path="vendor/lib/pkg")) == ["m.py"]
    # a gitlink directory that is not checked out (the submodule was never initialised) is an empty dir with no caret
    shutil.rmtree(world / "vendor" / "lib")
    (world / "vendor" / "lib").mkdir()
    lib = by_name(ls(path="vendor", refresh=True))["lib"]
    assert lib["type"] == "dir" and lib["has_children"] is False


def test_project_folder_that_is_itself_the_repo(projects_dir):
    solo = init_repo(projects_dir / "solo", {"a.txt": "a", "d/b.txt": "b"})
    write(solo, "u.txt", "u")
    for repo in ("solo", "root"):                                    # compat repo name, and the reserved one
        p = ls("solo", repo)
        assert p["git"] is True and names(p) == ["d", "a.txt", "u.txt"] and by_name(p)["u.txt"]["status"] == "?"


# ------------------------------------------------------------------ caches

def test_ls_files_runs_once_per_stamp_and_ttl(world, gitlog, clock):
    ls()
    assert gitlog.n("ls-files") == 1 and gitlog.n("status") == 1
    ls()
    ls(path="src")                                                   # another level of the same repo: the same listing
    assert gitlog.n("ls-files") == 1 and gitlog.n("status") == 1
    bump_index(world)                                                # .git/index changed (git add, commit, checkout...)
    ls()
    assert gitlog.n("ls-files") == 2
    clock["now"] += tree.LS_TTL - 0.5
    ls()
    assert gitlog.n("ls-files") == 2                                 # still inside the TTL
    clock["now"] += 1.0
    ls()
    assert gitlog.n("ls-files") == 3                                 # TTL passed: an untracked file could have appeared
    ls(refresh=True)
    assert gitlog.n("ls-files") == 4 and gitlog.n("status") == 4
    # the command is the one the contract names
    first = next(c for c in gitlog.calls if "ls-files" in c)
    assert first == ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    st = next(c for c in gitlog.calls if "status" in c)
    assert st[:3] == ["--no-optional-locks", "status", "--porcelain=v2"] and "-z" in st


def test_untracked_file_shows_after_the_ttl(world, clock):
    assert "fresh.txt" not in names(ls())
    write(world, "fresh.txt", "f")
    assert "fresh.txt" not in names(ls())                            # cached for LS_TTL
    clock["now"] += tree.LS_TTL + 1
    assert by_name(ls())["fresh.txt"]["status"] == "?"


def test_status_cache_ttl_and_index_stamp(world, gitlog, clock):
    ls()
    clock["now"] += tree.STATUS_TTL - 0.5
    write(world, "src/app.py", "changed")
    assert by_name(ls(path="src"))["app.py"]["status"] is None       # served from the 2 s cache
    assert gitlog.n("status") == 1
    clock["now"] += 1.0
    assert by_name(ls(path="src"))["app.py"]["status"] == "M"
    assert gitlog.n("status") == 2


def test_eight_threads_coalesce_to_one_status(world, gitlog):
    gitlog.delay = {"status": 0.25, "ls-files": 0.1}
    start = threading.Barrier(8)

    def go(_):
        start.wait()
        return ls(path="src")["etag"]

    with ThreadPoolExecutor(8) as ex:
        etags = list(ex.map(go, range(8)))
    assert len(set(etags)) == 1
    assert gitlog.n("status") == 1 and gitlog.n("ls-files") == 1


def test_slow_status_degrades_to_stale_and_is_not_retried_every_request(world, monkeypatch, gitlog, clock):
    real = tree._run
    state = {"fail": True}

    def flaky(args, cwd, timeout):
        if "status" in args and state["fail"]:
            gitlog.calls.append(list(args))
            raise subprocess.TimeoutExpired(args, timeout)
        return real(args, cwd, timeout)

    monkeypatch.setattr(tree, "_run", flaky)
    p = ls()
    assert p["status_stale"] is True and p["git"] is True and p["branch"] == "main"       # branch read from .git/HEAD
    assert names(p)[:3] == ["Assets", "docs", "src"] and all(e["status"] is None for e in p["entries"])
    ls()
    ls()
    assert gitlog.n("status") == 1                                   # the failure is cached: no 4 s stall per request
    clock["now"] += tree.STATUS_FAIL_TTL + 1
    state["fail"] = False
    write(world, "fresh.txt", "f")
    clock["now"] += tree.LS_TTL
    p = ls()
    assert p["status_stale"] is False and by_name(p)["fresh.txt"]["status"] == "?"
    # a failure after a success keeps the last letters, flagged stale
    state["fail"] = True
    clock["now"] += tree.STATUS_TTL + 1
    p = ls()
    assert p["status_stale"] is True and by_name(p)["fresh.txt"]["status"] == "?"


def test_git_failure_falls_back_to_the_disk(world, monkeypatch):
    def broken(args, cwd, timeout):
        raise tree._GitFailed("fatal: not happy")

    monkeypatch.setattr(tree, "_run", broken)
    p = ls(hidden=True)
    assert p["git"] is False and p["status_stale"] is True and p["branch"] is None
    assert names(p) == [".github", "Assets", "docs", "src", ".gitignore", "apple.txt", "Banana.txt", "README.md", "Zed.txt"]
    assert ".git" not in names(p)


def test_cache_holds_a_bounded_number_of_repos(projects_dir, monkeypatch):
    monkeypatch.setattr(tree, "CACHE_REPOS", 2)
    for n in ("r1", "r2", "r3"):
        init_repo(projects_dir / "shop" / n, {"f.txt": "f"})
        ls(repo=n)
    assert len(tree._LS) == 2 and len(tree._STATUS) == 2


# ------------------------------------------------------------------ path safety

BAD_PATHS = ["..", "../x", "src/../..", "src/../../..", "/etc", "/", "a\0b", "src\\util", "..\\x", ".git", ".git/config", ".GIT/config",
             "src/.git/hooks", "src/util/.Git"]


@pytest.mark.parametrize("bad", BAD_PATHS)
def test_unsafe_paths_are_refused(world, bad):
    with pytest.raises(projects.BadRequest):
        ls(path=bad)
    with pytest.raises(projects.BadRequest):
        tree.read_file("shop", "api", bad)


def test_safe_oddities_are_normalised_not_refused(world):
    assert tree.check_rel("./a//b/") == ["a", "b"]
    assert tree.check_rel("") == [] and tree.check_rel(None) == []
    assert tree.check_rel("a/.gitignore/.github") == ["a", ".gitignore", ".github"]       # only the exact segment .git is special
    assert tree.check_rel("a b/ü/%2e%2e") == ["a b", "ü", "%2e%2e"]                       # a literal, not an escape: no such file
    with pytest.raises(projects.NotFound):
        ls(path="%2e%2e")


@pytest.fixture
def links(world, projects_dir):
    outside = projects_dir.parent / "outside"
    write(outside, "passwd", "root:x")
    write(outside, "sub/deep.txt", "d")
    os.symlink(outside, world / "outdir")                            # a link pointing out
    os.symlink(world / "src", world / "srclink")                     # a link pointing inside the repo: also refused
    os.symlink(outside / "passwd", world / "passwd")                 # a file link pointing out
    os.symlink(world / "README.md", world / "readme-link")
    os.symlink(world, projects_dir / "shop" / "repolink")            # a repo that is a symlink
    os.symlink(outside, projects_dir / "shop" / "outrepo")
    os.symlink(projects_dir / "shop", projects_dir / "alias")        # a project that is a symlink
    return outside


def test_symlinks_are_refused_everywhere(links, world):
    for bad in ("outdir", "outdir/sub", "outdir/passwd", "srclink", "srclink/app.py", "passwd", "readme-link"):
        with pytest.raises(projects.BadRequest) as ei:
            tree.read_file("shop", "api", bad)
        assert "symlink" in str(ei.value), bad
    for bad in ("outdir", "outdir/sub", "srclink", "srclink/util"):
        with pytest.raises(projects.BadRequest):
            ls(path=bad)
    for project, repo in (("shop", "repolink"), ("shop", "outrepo"), ("alias", "api")):
        with pytest.raises(projects.BadRequest):
            ls(project, repo)
        with pytest.raises(projects.BadRequest):
            tree.read_file(project, repo, "README.md")
    # listed, but typed as a symlink and never followed: no caret, no size
    e = by_name(ls())
    for n in ("outdir", "srclink", "passwd", "readme-link"):
        assert e[n]["type"] == "symlink" and e[n]["has_children"] is False and e[n]["size"] is None
    # the same link in a plain directory
    os.symlink(links, world.parent / "dirlink")
    p = ls("shop", "root")
    assert by_name(p)["dirlink"]["type"] == "symlink"
    with pytest.raises(projects.BadRequest):
        ls("shop", "root", path="dirlink")


def test_missing_and_wrong_kind_paths(world):
    for call in (lambda: ls(path="nope"), lambda: ls(path="src/nope/deeper"), lambda: ls("nope", "api"), lambda: ls("shop", "nope"),
                 lambda: tree.read_file("shop", "api", "nope.txt"), lambda: tree.read_file("shop", "api", "README.md/x")):
        with pytest.raises(projects.NotFound):
            call()
    with pytest.raises(projects.BadRequest):
        ls(path="README.md")                                         # a file is not a directory
    with pytest.raises(projects.BadRequest):
        tree.read_file("shop", "api", "src")                         # nor a directory a file
    with pytest.raises(projects.BadRequest):
        tree.read_file("shop", "api", "")
    with pytest.raises(projects.BadRequest):
        ls("bad..name", "api")


def test_contained_helper(projects_dir, tmp_path):
    inside = projects_dir / "p"
    inside.mkdir()
    assert projects.contained(inside) == inside.resolve()
    assert projects.contained(inside, projects_dir) == inside.resolve()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(projects.BadRequest):
        projects.contained(outside)
    with pytest.raises(projects.BadRequest):
        projects.contained(inside, outside)
    os.symlink(inside, projects_dir / "l")
    with pytest.raises(projects.BadRequest):
        projects.contained(projects_dir / "l")
    assert projects.contained(projects_dir, projects_dir) == projects_dir.resolve()


# ------------------------------------------------------------------ read_file

def test_read_file_basics(world):
    r = tree.read_file("shop", "api", "src/app.py")
    assert r["path"] == "src/app.py" and r["text"] == "print(1)\n" and r["size"] == 9 and r["lines"] == 1
    assert r["truncated"] is False and isinstance(r["mtime"], int) and r["mtime"] > 1_000_000_000
    assert list(r) == ["path", "size", "mtime", "truncated", "lines", "text"]
    write(world, "n.txt", "a\nb\nc")
    assert tree.read_file("shop", "api", "n.txt")["lines"] == 3
    write(world, "e.txt", "")
    assert tree.read_file("shop", "api", "e.txt")["lines"] == 0
    write(world, "u.txt", "héllo wörld € 日本\n")
    assert tree.read_file("shop", "api", "u.txt")["text"] == "héllo wörld € 日本\n"
    assert tree.read_file("shop", "api", ".github/ci.yml")["text"] == "on: push\n"          # dotfiles are fine
    assert tree.read_file("shop", "root", "api/src/app.py")["text"] == "print(1)\n"


def test_read_file_cap_and_multibyte_cut(world):
    write(world, "big.txt", "line\n" * 100_000)                                # 500 KB
    r = tree.read_file("shop", "api", "big.txt")
    assert r["truncated"] is True and len(r["text"].encode()) == tree.FILE_CAP and r["size"] == 500_000
    assert r["lines"] == tree.FILE_CAP // 5 + 1 or r["lines"] == tree.FILE_CAP // 5      # the last line may be partial
    (world / "exact.txt").write_bytes(b"a" * tree.FILE_CAP)
    r = tree.read_file("shop", "api", "exact.txt")
    assert r["truncated"] is False and len(r["text"]) == tree.FILE_CAP
    (world / "plus1.txt").write_bytes(b"a" * (tree.FILE_CAP + 1))
    assert tree.read_file("shop", "api", "plus1.txt")["truncated"] is True
    (world / "euro.txt").write_bytes("€".encode() * 100_000)                   # 300 KB: the cap cuts a 3-byte character in two
    r = tree.read_file("shop", "api", "euro.txt")
    assert r["truncated"] is True and set(r["text"]) == {"€"} and len(r["text"]) == tree.FILE_CAP // 3


def test_read_file_binary_is_415(world):
    (world / "img.bin").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 100)
    with pytest.raises(tree.Unsupported):
        tree.read_file("shop", "api", "img.bin")
    (world / "latin1.txt").write_bytes("café".encode("latin-1"))
    with pytest.raises(tree.Unsupported):
        tree.read_file("shop", "api", "latin1.txt")
    (world / "late-nul.txt").write_bytes(b"a" * 9000 + b"\x00" + b"b")        # a NUL after the first 8 KB is not sniffed
    assert tree.read_file("shop", "api", "late-nul.txt")["size"] == 9002


@pytest.mark.parametrize("name", [".env", ".env.local", ".env.example", "server.pem", "id_rsa", "id_rsa.pub", "aws-credentials.json",
                                  "credentials", "tls.key", "my_secret.txt", "SECRETS.yml", "Server.PEM"])
def test_secret_names_need_reveal(world, name):
    write(world, f"conf/{name}", "TOKEN=1\n")
    with pytest.raises(projects.Forbidden):
        tree.read_file("shop", "api", f"conf/{name}")
    assert tree.read_file("shop", "api", f"conf/{name}", reveal=True)["text"] == "TOKEN=1\n"


def test_ordinary_names_are_not_secrets(world):
    for name in ("README.md", "env.py", "keys.txt", "monkey.txt", "pem.txt", ".envrc.txt", "src/app.py"):
        assert tree.is_secret_name(os.path.basename(name)) is (name == ".envrc.txt"), name   # '.env*' also catches .envrc*
    write(world, "keys.txt", "k")
    assert tree.read_file("shop", "api", "keys.txt", reveal=True)["text"] == "k"              # reveal on a plain file is a no-op


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no fifos")
def test_fifo_does_not_hang_the_worker(world):
    os.mkfifo(world / "pipe")
    with pytest.raises(projects.BadRequest):
        tree.read_file("shop", "api", "pipe")


# ------------------------------------------------------------------ HTTP

URL = "/api/projects/shop/repos/{repo}/tree"
FURL = "/api/projects/shop/repos/{repo}/file"


def get(c, url, **params):
    return c.get(url, headers=H, params=params)


def test_http_requires_the_board_login(world, lite_client):
    assert lite_client.get(URL.format(repo="api")).status_code == 403
    assert lite_client.get(FURL.format(repo="api"), params={"path": "README.md"}).status_code == 403


def test_http_tree_shape_flags_and_errors(world, mono, lite_client):
    r = get(lite_client, URL.format(repo="api"))
    assert r.status_code == 200
    body = r.json()
    assert body == ls() and names(body)[:3] == ["Assets", "docs", "src"]
    assert r.headers["etag"] == f'W/"{body["etag"]}"' and r.headers["cache-control"] == "private, no-cache"
    assert names(get(lite_client, URL.format(repo="api"), hidden=1).json())[0] == ".github"
    assert "build" in names(get(lite_client, "/api/projects/mono/repos/root/tree", hidden=0, ignored=1).json())
    flags = get(lite_client, URL.format(repo="api"), path="src", hidden="true", ignored="0", repos="0", refresh="1").json()
    assert flags["path"] == "src" and flags["hidden"] is True and flags["ignored"] is False
    assert by_name(get(lite_client, "/api/projects/mono/repos/root/tree", repos=0).json())["api"]["type"] == "dir"
    assert by_name(get(lite_client, "/api/projects/mono/repos/root/tree").json())["api"]["type"] == "repo"
    assert get(lite_client, URL.format(repo="nope")).status_code == 404
    assert get(lite_client, URL.format(repo="api"), path="nope").status_code == 404
    assert get(lite_client, "/api/projects/nope/repos/api/tree").status_code == 404
    assert get(lite_client, URL.format(repo="api"), path="README.md").status_code == 400
    assert get(lite_client, "/api/projects/shop/repos/.hid/tree").status_code == 400
    assert "error" in get(lite_client, URL.format(repo="api"), path="nope").json()


def test_http_etag_and_304(world, lite_client):
    first = get(lite_client, URL.format(repo="api"))
    tag = first.headers["etag"]
    again = lite_client.get(URL.format(repo="api"), headers=H | {"If-None-Match": tag})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == tag
    assert lite_client.get(URL.format(repo="api"), headers=H | {"If-None-Match": f'"x", {tag}'}).status_code == 304
    assert lite_client.get(URL.format(repo="api"), headers=H | {"If-None-Match": tag[2:]}).status_code == 304      # strong form of the same tag
    assert lite_client.get(URL.format(repo="api"), headers=H | {"If-None-Match": "*"}).status_code == 304
    assert lite_client.get(URL.format(repo="api"), headers=H | {"If-None-Match": 'W/"nope"'}).status_code == 200
    # the tag is per payload: another level, other flags, or a changed file all have another one
    assert get(lite_client, URL.format(repo="api"), path="src").headers["etag"] != tag
    assert get(lite_client, URL.format(repo="api"), hidden=1).headers["etag"] != tag
    src_tag = get(lite_client, URL.format(repo="api"), path="src").headers["etag"]
    write(world, "src/app.py", "changed")
    stale = lite_client.get(URL.format(repo="api") + "?path=src&refresh=1", headers=H | {"If-None-Match": src_tag})
    assert stale.status_code == 200 and stale.headers["etag"] != src_tag                   # the M appeared: a new tag, a full body
    assert by_name(stale.json())["app.py"]["status"] == "M"
    assert lite_client.get(URL.format(repo="api") + "?path=src", headers=H | {"If-None-Match": stale.headers["etag"]}).status_code == 304
    write(world, "README.md", "# changed, longer\n")
    after = lite_client.get(URL.format(repo="api") + "?refresh=1", headers=H | {"If-None-Match": tag})
    assert after.status_code == 200 and after.headers["etag"] != tag


def test_http_unsafe_paths_through_the_query_string(world, links, lite_client):
    raw = ["%2e%2e", "%2e%2e%2f%2e%2e", "src%2f%2e%2e%2f%2e%2e", "..%2fx", "%2fetc", "a%00b", "src%5Cutil", ".git%2fconfig", "%2egit",
           "src%2f.git", "outdir", "outdir%2fsub", "srclink", "passwd"]
    for q in raw:
        r = lite_client.get(f"/api/projects/shop/repos/api/tree?path={q}", headers=H)
        assert r.status_code == 400, (q, r.status_code, r.text)
        r = lite_client.get(f"/api/projects/shop/repos/api/file?path={q}", headers=H)
        assert r.status_code == 400, (q, r.status_code, r.text)
    for repo in ("repolink", "outrepo"):
        assert lite_client.get(f"/api/projects/shop/repos/{repo}/tree", headers=H).status_code == 400
    assert lite_client.get("/api/projects/alias/repos/api/tree", headers=H).status_code == 400
    assert lite_client.get("/api/projects/shop/repos/api/tree?path=%252e%252e", headers=H).status_code == 404   # double-encoded: a name, not '..'


def test_http_file_statuses(world, lite_client):
    ok = get(lite_client, FURL.format(repo="api"), path="src/app.py")
    assert ok.status_code == 200 and ok.json()["text"] == "print(1)\n" and "no-store" in ok.headers["cache-control"]
    assert get(lite_client, FURL.format(repo="api"), path="nope.txt").status_code == 404
    assert get(lite_client, FURL.format(repo="api"), path="src").status_code == 400
    assert get(lite_client, FURL.format(repo="api")).status_code == 400
    (world / "img.bin").write_bytes(b"\x00\x01\x02")
    r = get(lite_client, FURL.format(repo="api"), path="img.bin")
    assert r.status_code == 415 and "binary" in r.json()["error"]
    write(world, ".env", "SECRET=1")
    denied = get(lite_client, FURL.format(repo="api"), path=".env")
    assert denied.status_code == 403 and "reveal" in denied.json()["error"]
    assert get(lite_client, FURL.format(repo="api"), path=".env", reveal=0).status_code == 403
    shown = get(lite_client, FURL.format(repo="api"), path=".env", reveal=1)
    assert shown.status_code == 200 and shown.json()["text"] == "SECRET=1"
    write(world, "big.txt", "x" * 300_000)
    big = get(lite_client, FURL.format(repo="api"), path="big.txt").json()
    assert big["truncated"] is True and len(big["text"]) == tree.FILE_CAP and big["size"] == 300_000


def test_names_that_are_not_utf8_cannot_break_the_json(world):
    bad = os.fsdecode(b"caf\xe9.txt")                         # what fsdecode makes of a Latin-1 file name
    assert tree._display(bad) == "caf\ufffd.txt" and tree._display("ok é") == "ok é"
    tree._display(bad).encode("utf-8")


def test_flag_parsing():
    assert [tree.flag(v) for v in ("1", "true", "True", "yes", "on", " 1 ")] == [True] * 6
    assert [tree.flag(v) for v in ("0", "false", "", "no", "2", None, "off")] == [False] * 7
    assert tree.not_modified(None, "abc") is False and tree.not_modified("", "abc") is False
    assert tree.not_modified('W/"abc"', "abc") and tree.not_modified('"abc"', "abc") and tree.not_modified('"x", W/"abc"', "abc")
    assert not tree.not_modified('W/"abd"', "abc")


def test_secret_names_cover_keys_and_rc_files():
    from app import tree
    for n in (".env", ".env.local", "server.pem", "id_rsa", "id_rsa.pub", "id_ed25519", "id_ecdsa.pub", "credentials.json", "tls.key",
              "my-secret.txt", ".npmrc", ".netrc", ".pgpass", "cert.p12", "store.keystore"):
        assert tree.is_secret_name(n), n
    for n in ("identity.txt", "README.md", "keyboard.js", "secrets_doc.md.bak"[:0] or "index.html", "env.example"):
        assert not tree.is_secret_name(n), n
