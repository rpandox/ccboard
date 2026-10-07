"""v0.5.19 clone URL guard: projects.check_clone_url (behind check_url) refuses every URL that would point git at the box itself, its LAN or the tailnet, on the
preflight probe and on both clone routes. Nothing here touches the network or a real remote: a refused URL never starts a process (the routes are checked with every
launch path patched to fail), and the process-group test runs a fake `git` script from a tmp dir."""
import os
import subprocess
import time
from pathlib import Path

import pytest

from app import config, preflight, projects
from app.config import settings

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
WHY = "that host is not allowed for clones"

ACCEPTED = [
    "https://github.com/o/r.git",
    "https://gitlab.com/o/r",
    "ssh://git@github.com/o/r.git",
    "git@github.com:o/r.git",
    "https://user@github.com/o/r",
    "https://user:token@github.com:8443/o/r.git",
    "ssh://git@github.com:2222/o/r.git",
    "https://GitHub.com./o/r",                      # case and one trailing dot do not make a host private
]

HOST_REFUSED = [                                     # shaped like a clone URL; the host is the problem (the sentence names CCBOARD_CLONE_ALLOWED_HOSTS)
    "https://127.0.0.1/x",
    "https://localhost/x",
    "https://LOCALHOST./x",
    "https://foo.localhost/x",
    "https://[::1]/x",
    "https://[::ffff:127.0.0.1]/x",
    "https://[::ffff:7f00:1]/x",
    "https://2130706433/x",
    "https://0x7f.0.0.1/x",
    "https://0x7f000001/x",
    "https://0177.0.0.1/x",
    "https://127.1/x",
    "https://127.0.1/x",
    "https://0/x",
    "https://10.0.0.5/x",
    "https://172.16.0.1/x",
    "https://192.168.1.1/x",
    "https://169.254.169.254/latest",
    "https://100.64.0.1/x",
    "https://100.100.100.100/x",
    "https://8.8.8.8/x",                             # a public address is still an IP literal
    "https://[fd7a:115c:a1e0::1]/x",
    "https://[fe80::1]/x",
    "https://[fe80::1%25eth0]/x",
    "https://box.example-tailnet.ts.net/x",
    "https://ts.net/x",
    "https://gitea/x",
    "https://nas.local/x",
    "https://nas.lan/x",
    "https://svc.internal/x",
    "https://router.home.arpa/x",
    "https://a%2f@127.0.0.1/x",                      # the host is what follows the last @
    "https://github.com@127.0.0.1/x",
    "ssh://git@10.1.2.3/x",
    "ssh://git@[::1]/x",
    "git@192.168.0.2:o/r",
    "git@localhost:o/r",
    "git@gitea:o/r",
    "git@0x7f.1:o/r",
]

ODD_OR_SHAPE_REFUSED = [                             # refused before any host rule: scheme, shape, characters
    "http://github.com/o/r",
    "git://github.com/o/r",
    "file:///etc",
    "ext::sh -c id",
    "ftp://github.com/o/r",
    "HTTPS://github.com/o/r",
    "https://evil.com\\@127.0.0.1/x",
    "https://github.com/o/r with space",
    "https://github.com/o/r\nsecond",
    "https://github.com/o/r\x00",
    "https://github.com/o/r\x7f",
    "-oProxyCommand=x",
    "--upload-pack=/tmp/x",
    "https://-evil.com/x",
    "https://",
    "https:///o/r",
    "https://github.com",                            # no repository path
    "https://github.com:99999/o/r",
    "https://github.com:abc/o/r",
    "https://exa%6dple.com/x",                       # a percent escape in the host
    "https://exa_mple.com/x",
    "https://exämple.com/x",
    "https://github..com/x",
    "https://u1@u2@github.com/o/r",
    "https://-u@github.com/o/r",
    "ssh://user:pw@github.com/o/r",
    "git@github.com",
    "git@:o/r",
    "git@github.com:",
    "x" * 513,
    "https://github.com/" + "a" * 494,               # 513 characters
]


@pytest.fixture(autouse=True)
def no_allow_list(monkeypatch):
    monkeypatch.setattr(settings, "clone_allowed_hosts", ())


@pytest.mark.parametrize("url", ACCEPTED)
def test_public_https_ssh_and_scp_urls_pass(url):
    assert projects.check_clone_url(url) == url
    assert projects.check_url(f"  {url}  ") == url


def test_a_url_of_exactly_512_characters_passes():
    url = "https://github.com/" + "a" * 493
    assert len(url) == 512 and projects.check_url(url) == url


@pytest.mark.parametrize("url", HOST_REFUSED)
def test_internal_hosts_in_every_spelling_are_refused_with_the_allow_list_sentence(url):
    with pytest.raises(projects.BadRequest) as e:
        projects.check_clone_url(url)
    assert str(e.value).startswith(WHY) and "CCBOARD_CLONE_ALLOWED_HOSTS" in str(e.value), url
    assert "127" not in str(e.value) and "169.254" not in str(e.value), "the answer never echoes an address"


@pytest.mark.parametrize("url", ODD_OR_SHAPE_REFUSED)
def test_other_schemes_odd_characters_and_bad_shapes_are_refused(url):
    with pytest.raises(projects.BadRequest):
        projects.check_clone_url(url)
    with pytest.raises(projects.BadRequest):
        projects.check_url(url)


def test_a_non_string_is_refused():
    for bad in (None, 5, ["https://github.com/o/r"], b"https://github.com/o/r"):
        with pytest.raises(projects.BadRequest):
            projects.check_url(bad)


def test_check_url_is_check_clone_url_answer_for_answer():
    def verdict(fn, url):
        try:
            return fn(url)
        except projects.BadRequest as e:
            return str(e)
    for url in ACCEPTED + HOST_REFUSED + ODD_OR_SHAPE_REFUSED:
        assert verdict(projects.check_url, url) == verdict(projects.check_clone_url, url), url


# ---------------------------------------------------------------- the ranges

@pytest.mark.parametrize("ip", ["127.0.0.1", "127.255.255.254", "::1", "10.1.2.3", "172.16.0.1", "172.31.255.255", "192.168.0.1", "169.254.169.254", "fe80::1",
                                "100.64.0.1", "100.100.100.100", "100.127.255.255", "fd7a:115c:a1e0::1", "fc00::1", "fdff::1", "0.0.0.0", "::", "224.0.0.1", "ff02::1",
                                "255.255.255.255", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "[::1]", "not an address", ""])
def test_is_internal_address_covers_loopback_private_link_local_cgnat_ula_and_the_rest(ip):
    assert projects.is_internal_address(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "140.82.112.3", "100.63.255.255", "100.128.0.1", "172.15.0.1", "172.32.0.1", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_is_internal_address_is_false_for_public_addresses(ip):
    assert projects.is_internal_address(ip) is False


# ---------------------------------------------------------------- the allow-list

def allow(monkeypatch, raw):
    monkeypatch.setattr(settings, "clone_allowed_hosts", config.parse_clone_hosts(raw))


def test_the_allow_list_parses_names_addresses_and_wildcards():
    assert config.parse_clone_hosts(" Gitea.LAN, 192.168.1.5 ,[fd00::1], *.corp.example.com,, git.local. ,gitea.lan") == \
        ("gitea.lan", "192.168.1.5", "fd00::1", "*.corp.example.com", "git.local")
    assert config.parse_clone_hosts("") == () and config.parse_clone_hosts(None) == ()
    assert config.Settings({"CCBOARD_CLONE_ALLOWED_HOSTS": "a.lan"}).clone_allowed_hosts == ("a.lan",)
    assert config.Settings({}).clone_allowed_hosts == ()


def test_a_listed_name_and_a_listed_address_pass_and_nothing_else_does(monkeypatch):
    allow(monkeypatch, "gitea.lan,gitea,192.168.1.5,fd00::1,box.ts.net")
    for url in ("https://gitea.lan/o/r", "https://gitea/o/r", "https://192.168.1.5/o/r", "ssh://git@192.168.1.5:2222/o/r", "git@gitea.lan:o/r", "git@gitea:o/r",
                "https://[fd00::1]/o/r", "https://box.ts.net/o/r", "https://GITEA.LAN./o/r"):
        assert projects.check_url(url) == url
    for url in ("https://other.lan/o/r", "https://192.168.1.6/o/r", "https://127.0.0.1/o/r", "https://localhost/o/r", "https://[fd00::2]/o/r", "https://192.168.1.05/o/r",
                "https://0xc0.168.1.5/o/r", "https://3232235781/o/r", "https://x.gitea.lan/o/r", "https://other.ts.net/o/r"):
        with pytest.raises(projects.BadRequest, match="not allowed for clones"):
            projects.check_url(url)


def test_a_wildcard_matches_names_below_it_and_never_an_address(monkeypatch):
    allow(monkeypatch, "*.corp.example.com,*.0.0.1,*.5")
    assert projects.check_url("https://git.corp.example.com/o/r") == "https://git.corp.example.com/o/r"
    assert projects.check_url("https://a.b.corp.example.com/o/r")
    for url in ("https://127.0.0.1/o/r", "https://10.0.0.5/o/r", "https://corp.example.com.lan/o/r"):
        with pytest.raises(projects.BadRequest, match="not allowed for clones"):
            projects.check_url(url)
    allow(monkeypatch, "*.lan")
    assert projects.check_url("https://nas.lan/o/r")
    with pytest.raises(projects.BadRequest):
        projects.check_url("https://lan/o/r")             # the wildcard is for the names below


def test_a_listed_host_never_brings_back_a_refused_scheme_or_a_bad_shape(monkeypatch):
    allow(monkeypatch, "gitea.lan,192.168.1.5,localhost")
    for url in ("http://gitea.lan/o/r", "git://gitea.lan/o/r", "file://gitea.lan/o/r", "ext::gitea.lan", "http://192.168.1.5/o/r", "https://gitea.lan\\@127.0.0.1/o/r",
                "https://gitea.lan", "https://gi tea.lan/o/r", "https://git%65a.lan/o/r", "https://-gitea.lan/o/r"):
        with pytest.raises(projects.BadRequest):
            projects.check_url(url)


# ---------------------------------------------------------------- the routes: a refused URL is a 400 and nothing starts

@pytest.fixture
def nothing_starts(monkeypatch, fake_tmux):
    """Every way the board could run git for a clone fails the test: the probe, a tmux clone session, git init."""
    def boom(*a, **k):
        raise AssertionError(f"a process was started for a refused URL: {a!r}")
    monkeypatch.setattr(preflight, "_run", boom)
    monkeypatch.setattr(preflight.subprocess, "Popen", boom)
    monkeypatch.setattr(projects.subprocess, "run", boom)
    return fake_tmux


REFUSED_FOR_ROUTES = ["https://127.0.0.1/x", "https://localhost/x", "https://169.254.169.254/latest", "https://100.100.100.100/x", "https://gitea/x", "http://github.com/o/r",
                      "git://github.com/o/r", "ssh://git@10.1.2.3/x", "git@192.168.0.2:o/r", "https://[::1]/x", "https://2130706433/x"]


@pytest.mark.parametrize("url", REFUSED_FOR_ROUTES)
def test_the_preflight_route_refuses_without_starting_git(lite_client, nothing_starts, url):
    r = lite_client.post("/api/preflight/clone", json={"url": url}, headers=H)
    assert r.status_code == 400 and r.json()["error"], url


@pytest.mark.parametrize("url", REFUSED_FOR_ROUTES)
def test_creating_a_project_with_a_refused_url_is_a_400_that_leaves_no_folder_and_no_session(lite_client, projects_dir, nothing_starts, url):
    r = lite_client.post("/api/projects", json={"name": "shop", "url": url}, headers=H)
    assert r.status_code == 400 and r.json()["error"], url
    assert not (projects_dir / "shop").exists() and nothing_starts["created"] == [] and nothing_starts["sent"] == []


@pytest.mark.parametrize("url", REFUSED_FOR_ROUTES)
def test_adding_a_repo_by_url_is_a_400_with_no_folder_and_no_session(lite_client, projects_dir, nothing_starts, url):
    assert lite_client.post("/api/projects", json={"name": "shop"}, headers=H).status_code == 201
    r = lite_client.post("/api/projects/shop/repos", json={"url": url, "name": "x"}, headers=H)
    assert r.status_code == 400 and r.json()["error"], url
    assert not (projects_dir / "shop" / "x").exists() and nothing_starts["created"] == [] and nothing_starts["sent"] == []


def test_the_bulk_route_refuses_the_whole_batch_when_one_url_is_refused(lite_client, nothing_starts):
    body = {"repos": [{"name": "ok", "url": "https://github.com/o/ok.git"}, {"name": "bad", "url": "https://192.168.1.1/o/bad.git"}]}
    r = lite_client.post("/api/projects/shop/repos/bulk", json=body, headers=H)
    assert r.status_code == 400 and WHY in r.json()["error"]
    assert nothing_starts["created"] == [] and nothing_starts["sent"] == []


def test_public_urls_still_clone_and_the_clone_session_carries_the_protocol_allow_list_without_disabling_redirects(lite_client, projects_dir, fake_tmux):
    r = lite_client.post("/api/projects", json={"name": "shop", "url": "https://github.com/o/web.git"}, headers=H)
    assert r.status_code == 201 and r.json()["clone_session"] == "shop--web--clone"
    name, cwd, env = fake_tmux["created"][0]
    assert name == "shop--web--clone" and env["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    line = fake_tmux["sent"][0][1]
    assert line == "git clone --progress -- https://github.com/o/web.git . && exit"
    assert "followRedirects" not in line, "a renamed repository still clones"
    r = lite_client.post("/api/projects/shop/repos", json={"url": "git@github.com:o/api.git"}, headers=H)
    assert r.status_code == 201 and fake_tmux["created"][1][2]["GIT_ALLOW_PROTOCOL"] == "https:ssh"


def test_a_listed_private_host_clones_through_the_route(lite_client, fake_tmux, monkeypatch):
    allow(monkeypatch, "gitea.lan")
    r = lite_client.post("/api/projects", json={"name": "shop", "url": "https://gitea.lan/o/web.git"}, headers=H)
    assert r.status_code == 201
    assert lite_client.post("/api/projects/shop/repos", json={"url": "https://nas.lan/o/api.git"}, headers=H).status_code == 400


# ---------------------------------------------------------------- the probe: protocols, redirects, the hard ceiling

def test_the_probe_command_and_environment_are_locked_down(monkeypatch):
    seen = {}

    def fake(argv, *, env, timeout):
        seen.update(argv=argv, env=env, timeout=timeout)
        return subprocess.CompletedProcess(argv, 0, "ref: refs/heads/main\tHEAD\n" + "a" * 40 + "\trefs/heads/main\n", "")
    monkeypatch.setattr(preflight, "_run", fake)
    assert preflight.ls_remote("https://github.com/o/r.git")["reachable"] is True
    assert "-c" in seen["argv"] and "http.followRedirects=false" in seen["argv"] and seen["argv"][-2:] == ["--", "https://github.com/o/r.git"]
    assert seen["env"]["GIT_ALLOW_PROTOCOL"] == "https:ssh" and seen["timeout"] == preflight.TIMEOUT == 12


def make_fake_git(tmp_path, body):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    g = bindir / "git"
    g.write_text("#!/bin/sh\n" + body)
    g.chmod(0o755)
    return bindir


def alive(pid: int) -> bool:
    """A process that is running or stopped; a zombie waiting for init to reap it is gone."""
    cp = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    stat = cp.stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def test_a_probe_that_times_out_leaves_no_process_behind(tmp_path, monkeypatch):
    """A fake git that starts a long-lived child (as git-remote-https and ssh do) and then waits: after the shortened timeout both are gone."""
    pidfile = tmp_path / "child.pid"
    bindir = make_fake_git(tmp_path, f"sleep 300 &\necho $! > '{pidfile}'\nwait\n")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    t0 = time.monotonic()
    r = preflight.ls_remote("https://github.com/o/r.git", timeout=1)
    assert time.monotonic() - t0 < 8, "the call returns at the ceiling, it does not wait for the child's pipe"
    assert r["reachable"] is False and "1 seconds" in r["error"]
    child = int(pidfile.read_text())
    for _ in range(50):
        if not alive(child):
            break
        time.sleep(0.1)
    assert not alive(child), "the child of git was killed with its process group"


def test_a_probe_that_answers_is_read_through_the_same_runner(tmp_path, monkeypatch):
    bindir = make_fake_git(tmp_path, "echo 'ref: refs/heads/trunk\tHEAD'\necho 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\trefs/heads/trunk'\n")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    r = preflight.ls_remote("https://github.com/o/r.git")
    assert r["reachable"] and r["default_branch"] == "trunk" and r["heads"] == ["trunk"]
    bindir2 = tmp_path / "bin2"
    bindir2.mkdir()
    (bindir2 / "git").write_text("#!/bin/sh\necho 'fatal: could not read Username for x: terminal prompts disabled' >&2\nexit 128\n")
    (bindir2 / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir2}{os.pathsep}{os.environ['PATH']}")
    r = preflight.ls_remote("https://github.com/o/r.git")
    assert r["reachable"] is False and r["needs_auth"] is True


def test_the_probe_never_sees_the_terminal(tmp_path, monkeypatch):
    out = tmp_path / "stdin.txt"
    bindir = make_fake_git(tmp_path, f"if read -r line; then echo got > '{out}'; else echo eof > '{out}'; fi\nexit 0\n")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    preflight.ls_remote("https://github.com/o/r.git")
    assert Path(out).read_text().strip() == "eof"
