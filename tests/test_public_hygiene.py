"""The repository is public (issue #60): it must read as a product, not as one person's machine.

Every tracked file (`git ls-files`, not only the ones an issue names) is scanned for the owner's host name, the owner's home path and
login, the owner's first name, macOS-style home path prefixes and e-mail addresses on real mail providers. The names are built from
fragments here so this file does not hold them as plain text, and it is itself scanned.

Allowed on purpose (the allowlist is explicit):
  * the repository's own coordinates, `github.com/<login>/ccboard` and `ghcr.io/<login>/ccboard` (README, ci.yml, Dockerfile labels, the image
    name in scripts/deploy.sh, the compose image line and the tests that pin them);
  * the licence holder's name in LICENSE;
  * the negative needles of the two older guards (tests/test_launcher_api.py, tests/test_static.py), which name a macOS home path.
Git history still holds the old names: only current files change.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

HOST = "ub" + "u2"
LOGIN = "rpan" + "dox"
FIRST = "rosh" + "an"
MAC_HOME = "/Us" + "ers/"
OWNER_HOME = "/home/" + LOGIN

REPO_COORDINATES = re.compile(rf"(?:github\.com|ghcr\.io)/{LOGIN}/ccboard", re.I)
ALLOWED = {
    "LICENSE": {FIRST},                                   # the copyright line
    "tests/test_launcher_api.py": {MAC_HOME},             # a negative needle: the launcher's JSON holds none of these
    "tests/test_static.py": {MAC_HOME},
}
PROVIDERS = r"gmail\.com|googlemail\.com|outlook\.com|hotmail\.com|live\.com|yahoo\.[a-z.]+|icloud\.com|me\.com|proton\.me|protonmail\.com|aol\.com"
EMAIL = re.compile(rf"[A-Za-z0-9._%+-]+@(?:{PROVIDERS})\b", re.I)


def tracked():
    """git ls-files, plus new files not yet added (so work in progress is scanned before it is committed); ignored files (.venv) are not."""
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"], capture_output=True, text=True, check=True).stdout
    return [f for f in out.split("\0") if f]


def read(rel):
    try:
        data = (ROOT / rel).read_bytes()
    except OSError:
        return None
    if b"\0" in data[:4096]:
        return None                                       # a binary file (an icon, a font)
    return data.decode("utf-8", "replace")


def findings(files=None, reader=read):
    bad = []
    for rel in files if files is not None else tracked():
        text = reader(rel)
        if text is None:
            continue
        allowed = ALLOWED.get(rel, set())
        scrubbed = REPO_COORDINATES.sub("", text)
        for label, needle, rx in (("the owner's host name", HOST, None), ("the owner's login", LOGIN, None), ("the owner's home path", OWNER_HOME, None),
                                  ("the owner's first name", FIRST, None), ("a macOS home path", MAC_HOME, None)):
            if needle in allowed:
                continue
            hay = scrubbed if needle in (LOGIN, OWNER_HOME) else text
            for n, line in enumerate(hay.splitlines(), 1):
                if needle.lower() in line.lower():
                    bad.append(f"{rel}:{n}: {label}")
                    break
        m = EMAIL.search(text)
        if m:
            bad.append(f"{rel}: an address on a real mail provider ({m.group(0).split('@')[0][:2]}...@{m.group(0).split('@')[1]})")
    return bad


def test_no_tracked_file_names_the_owner_or_the_box():
    assert findings() == []


def test_the_scan_sees_the_whole_tree_and_this_file_is_part_of_it():
    files = tracked()
    assert len(files) > 200 and "tests/test_public_hygiene.py" in files and "README.md" in files and "install.sh" in files
    assert any(f.startswith("app/static/demo/") for f in files) and any(f.startswith("deploy/") for f in files)
    me = read("tests/test_public_hygiene.py")
    assert HOST not in me and LOGIN not in me and FIRST not in me and MAC_HOME not in me, "this file builds the names from fragments"


@pytest.mark.parametrize("text,expect", [
    (f"ssh {HOST}", "host name"), (f"cd {OWNER_HOME}/x", "home path"), (f"docker login -u {LOGIN}", "login"),
    (f"{MAC_HOME}someone/work", "macOS"), ("write to boss@gm" + "ail.com", "mail provider"), (f"hello {FIRST.title()}", "first name"),
])
def test_each_kind_of_leak_is_found(text, expect):
    got = findings(["x.txt"], lambda rel: text)
    assert len(got) >= 1 and expect.split()[0] in " ".join(got).replace("the owner's ", "").replace("an address on a real ", ""), got


def test_the_repository_coordinates_are_the_only_allowed_use_of_the_login():
    ok = f"image: ghcr.io/{LOGIN}/ccboard:latest and https://github.com/{LOGIN}/ccboard/actions"
    assert findings(["x.txt"], lambda rel: ok) == []
    assert findings(["x.txt"], lambda rel: ok + f"\ndocker login ghcr.io -u {LOGIN}") != []
    assert findings(["x.txt"], lambda rel: f"https://github.com/{LOGIN}/other-repo") != []
    assert findings(["x.txt"], lambda rel: "someone@example.com and a@b.c and t@e.x") == [], "example and synthetic domains are fine"


def test_the_two_older_guards_still_scan_what_they_scanned():
    for f in ("tests/test_launcher_api.py", "tests/test_static.py"):
        assert "gmail.com" in read(f) and "ccb-fix" in read(f)
