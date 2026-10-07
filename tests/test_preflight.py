"""v0.5.19 clone preflight: POST /api/preflight/clone and app/preflight.py. Nothing here touches the network: the probe runs `git ls-remote` against a bare
repo in a tmp dir and against a path that does not exist; every other remote (https, ssh) is a stubbed subprocess."""
import subprocess

import pytest

from app import preflight, projects

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
GIT = ["-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false"]


def git(*args, cwd=None):
    subprocess.run(["git", *GIT, *args], check=True, capture_output=True, cwd=cwd)


@pytest.fixture
def bare(tmp_path):
    """A bare repo with branches main and dev, HEAD -> main."""
    work = tmp_path / "work"
    work.mkdir()
    git("init", "-q", "-b", "main", cwd=work)
    (work / "a.txt").write_text("a")
    git("add", "a.txt", cwd=work)
    git("commit", "-q", "-m", "one", cwd=work)
    git("branch", "dev", cwd=work)
    out = tmp_path / "remote.git"
    git("clone", "-q", "--bare", str(work), str(out))
    return out


class Run:
    """subprocess.run stand-in: records the call, answers with `result` or raises it."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess(["git"], rc, out, err)


# ---------------------------------------------------------------- the probe against real git

def test_a_local_bare_repo_is_reachable_with_its_default_branch_and_heads(bare):
    r = preflight.ls_remote(str(bare))
    assert r == {"reachable": True, "default_branch": "main", "needs_auth": False, "heads": ["dev", "main"], "error": None}


def test_the_default_branch_is_what_head_points_at_not_a_guess(bare):
    git("symbolic-ref", "HEAD", "refs/heads/dev", cwd=bare)
    r = preflight.ls_remote(str(bare))
    assert r["reachable"] and r["default_branch"] == "dev" and sorted(r["heads"]) == ["dev", "main"]


def test_an_empty_remote_is_reachable_without_a_default_branch(tmp_path):
    empty = tmp_path / "empty.git"
    git("init", "-q", "--bare", str(empty))
    r = preflight.ls_remote(str(empty))
    assert r["reachable"] is True and r["heads"] == [] and r["default_branch"] is None and r["error"] is None


def test_a_refusing_url_is_unreachable_with_one_line_of_why(tmp_path):
    r = preflight.ls_remote("file://" + str(tmp_path / "no-such-repo.git"))
    assert r["reachable"] is False and r["needs_auth"] is False and r["heads"] == [] and r["default_branch"] is None
    assert r["error"] and "\n" not in r["error"] and len(r["error"]) <= 200


# ---------------------------------------------------------------- how git is run

def test_git_never_prompts_never_hangs_and_gets_the_url_after_a_double_dash(monkeypatch):
    run = Run(cp(0, "ref: refs/heads/main\tHEAD\nabc1234def\tHEAD\nabc1234def\trefs/heads/main\n"))
    monkeypatch.setattr(preflight.subprocess, "run", run)
    preflight.ls_remote("https://example.com/o/r.git")
    argv, kw = run.calls[0]
    assert argv == ["git", "ls-remote", "--symref", "--", "https://example.com/o/r.git"]
    assert kw["timeout"] == preflight.TIMEOUT == 12
    env = kw["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_ASKPASS"] == "true"
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]
    assert kw["stdin"] == subprocess.DEVNULL


def test_a_remote_that_does_not_answer_in_time_is_unreachable(monkeypatch):
    monkeypatch.setattr(preflight.subprocess, "run", Run(subprocess.TimeoutExpired(["git"], 12)))
    r = preflight.ls_remote("https://example.com/o/r.git")
    assert r["reachable"] is False and r["needs_auth"] is False and "12 seconds" in r["error"]


def test_a_box_without_git_says_so(monkeypatch):
    monkeypatch.setattr(preflight.subprocess, "run", Run(FileNotFoundError("git")))
    r = preflight.ls_remote("https://example.com/o/r.git")
    assert r["reachable"] is False and "git is not installed" in r["error"]


@pytest.mark.parametrize("stderr", [
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    "remote: Repository not found.\nfatal: repository 'https://github.com/o/r.git/' not found",
    "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.",
    "fatal: Authentication failed for 'https://example.com/o/r.git/'",
])
def test_a_remote_that_asks_for_credentials_is_flagged(monkeypatch, stderr):
    monkeypatch.setattr(preflight.subprocess, "run", Run(cp(128, "", stderr)))
    r = preflight.ls_remote("https://example.com/o/r.git")
    assert r["reachable"] is False and r["needs_auth"] is True and r["error"]


def test_other_failures_are_not_blamed_on_credentials(monkeypatch):
    monkeypatch.setattr(preflight.subprocess, "run", Run(cp(128, "", "fatal: unable to access 'https://x.test/r.git/': Could not resolve host: x.test")))
    r = preflight.ls_remote("https://x.test/r.git")
    assert r["needs_auth"] is False and "Could not resolve host" in r["error"]


def test_the_error_never_carries_credentials_from_the_url(monkeypatch):
    monkeypatch.setattr(preflight.subprocess, "run", Run(cp(128, "", "fatal: unable to access 'https://user:s3cret@x.test/r.git/': 404")))
    r = preflight.ls_remote("https://user:s3cret@x.test/r.git")
    assert "s3cret" not in r["error"] and "user:" not in r["error"]


def test_a_server_without_a_symref_falls_back_to_main_then_master_then_the_first(monkeypatch):
    def heads(*names):
        return cp(0, "".join(f"{'a' * 40}\trefs/heads/{n}\n" for n in names))
    for names, want in ((("zeta", "master", "main"), "main"), (("zeta", "master"), "master"), (("zeta", "alpha"), "zeta")):
        monkeypatch.setattr(preflight.subprocess, "run", Run(heads(*names)))
        assert preflight.ls_remote("https://x.test/r.git")["default_branch"] == want


def test_tags_and_pull_refs_are_not_heads(monkeypatch):
    out = "ref: refs/heads/main\tHEAD\n" + "".join(f"{'a' * 40}\t{r}\n" for r in ("HEAD", "refs/heads/main", "refs/tags/v1", "refs/pull/1/head"))
    monkeypatch.setattr(preflight.subprocess, "run", Run(cp(0, out)))
    assert preflight.ls_remote("https://x.test/r.git")["heads"] == ["main"]


# ---------------------------------------------------------------- preflight_clone: the clone's own URL rule, then the probe

def test_preflight_reuses_check_url_so_the_field_says_what_a_clone_would(monkeypatch):
    seen = []
    real = projects.check_url
    monkeypatch.setattr(projects, "check_url", lambda u: seen.append(u) or real(u))
    monkeypatch.setattr(preflight, "ls_remote", lambda url, timeout=0: {"reachable": True, "default_branch": "main", "needs_auth": False, "heads": ["main"], "error": None})
    r = preflight.preflight_clone("  https://github.com/octo/Hello.World.git ")
    assert seen == ["  https://github.com/octo/Hello.World.git "]
    assert r["name"] == "Hello-World" and r["reachable"] is True


@pytest.mark.parametrize("url", ["junk", "", "-oProxyCommand=x", "file:///tmp/r.git", "/tmp/r.git", "https://x.test/a b"])
def test_preflight_refuses_what_a_clone_refuses_without_running_git(monkeypatch, url):
    monkeypatch.setattr(preflight, "ls_remote", lambda *a, **k: pytest.fail("git must not run for a URL the clone would refuse"))
    with pytest.raises(projects.BadRequest, match="clone URL must start with"):
        preflight.preflight_clone(url)


def test_a_name_that_cannot_be_derived_is_null_not_an_error(monkeypatch):
    monkeypatch.setattr(preflight, "ls_remote", lambda url, timeout=0: {"reachable": True, "default_branch": "main", "needs_auth": False, "heads": [], "error": None})
    assert preflight.preflight_clone("https://x.test/%%%.git")["name"] is None


# ---------------------------------------------------------------- the route

def test_route_answers_the_documented_shape(lite_client, monkeypatch):
    monkeypatch.setattr(preflight, "ls_remote", lambda url, timeout=0: {"reachable": True, "default_branch": "trunk", "needs_auth": False, "heads": ["trunk", "dev"], "error": None})
    r = lite_client.post("/api/preflight/clone", json={"url": "https://github.com/octo/shop.git"}, headers=H)
    assert r.status_code == 200
    assert r.json() == {"reachable": True, "default_branch": "trunk", "needs_auth": False, "heads": ["trunk", "dev"], "name": "shop", "error": None}


def test_route_reports_a_private_repo_as_a_200_that_needs_credentials(lite_client, monkeypatch):
    monkeypatch.setattr(preflight.subprocess, "run", Run(cp(128, "", "fatal: could not read Username for 'https://github.com': terminal prompts disabled")))
    r = lite_client.post("/api/preflight/clone", json={"url": "https://github.com/octo/private.git"}, headers=H)
    assert r.status_code == 200
    j = r.json()
    assert j["reachable"] is False and j["needs_auth"] is True and j["name"] == "private" and j["heads"] == []


def test_route_refuses_a_bad_url_with_400_and_a_missing_one_with_422(lite_client):
    assert lite_client.post("/api/preflight/clone", json={"url": "file:///etc"}, headers=H).status_code == 400
    assert lite_client.post("/api/preflight/clone", json={}, headers=H).status_code == 422


def test_route_needs_a_signed_in_user(lite_client):
    assert lite_client.post("/api/preflight/clone", json={"url": "https://github.com/o/r.git"}).status_code in (401, 403)


# ---------------------------------------------------------------- the wizard's client rules mirror the server's (pages/onboarding.js)

import json  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

from app import tmux  # noqa: E402

PARITY = json.loads((Path(__file__).parent / "fixtures" / "onboarding_names.json").read_text(encoding="utf-8"))
ONBOARDING_JS = (Path(__file__).resolve().parent.parent / "app" / "static" / "pages" / "onboarding.js").read_text(encoding="utf-8")


def js_regex(name):
    m = re.search(rf"const {name} = /(.+)/;", ONBOARDING_JS)
    assert m, f"{name} is not a regex literal in pages/onboarding.js"
    return m.group(1).replace("\\/", "/")


def test_the_fixture_the_node_tests_read_agrees_with_the_server():
    """tests/fixtures/onboarding_names.json is read by tests/js/onboarding.test.mjs: its verdicts are tmux.valid_name's, check_url's and derive_repo_name's own."""
    for value, ok in PARITY["names"]:
        assert tmux.valid_name(value) is ok, repr(value)
    for url, ok in PARITY["urls"]:
        try:
            projects.check_url(url)
            got = True
        except projects.BadRequest:
            got = False
        assert got is ok, repr(url)
    for url, name in PARITY["derive"]:
        assert projects.derive_repo_name(url) == name, repr(url)


def test_the_wizards_name_and_url_regexes_are_the_servers_patterns():
    assert js_regex("WIZ_NAME_RE") == tmux.NAME_RE.pattern
    assert js_regex("WIZ_URL_RE") == projects.URL_RE.pattern
