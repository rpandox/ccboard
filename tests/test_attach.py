"""bin/ccboard-attach v2, run as a real subprocess with a stub `tmux` and a stub `sleep` first on PATH.

The stub tmux writes its argv (one argument per line) to $RECORD and exits 0; the stub sleep only records, so the 2 s pause of a
refused attach costs nothing. exec'ing the stub is how the script 'attaches': a refusal never reaches tmux. Run under /bin/sh and, where
it exists, dash (Ubuntu's /bin/sh): the script is POSIX sh, and `${2-full}` / `${#name}` must behave the same in both.
"""
import os
import pathlib
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.posix_sh      # runs the shell scripts under sh

ATTACH = pathlib.Path(__file__).resolve().parent.parent / "bin" / "ccboard-attach"
SHELLS = ["/bin/sh"] + ([shutil.which("dash")] if shutil.which("dash") and os.path.realpath(shutil.which("dash")) != os.path.realpath("/bin/sh") else [])


@pytest.fixture
def stubs(tmp_path):
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    (bindir / "tmux").write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$RECORD"\n')
    (bindir / "sleep").write_text('#!/bin/sh\necho "$@" >> "$SLEEPS"\n')
    for f in bindir.iterdir():
        f.chmod(0o755)
    return bindir


@pytest.fixture(params=SHELLS)
def attach(request, stubs, tmp_path):
    """attach(*args, sock=None) -> (returncode, stdout, tmux argv or None, slept)."""
    record, sleeps = tmp_path / "record", tmp_path / "sleeps"

    def run(*args, sock=None):
        record.unlink(missing_ok=True)
        sleeps.unlink(missing_ok=True)
        env = {"PATH": f"{stubs}:/usr/bin:/bin", "RECORD": str(record), "SLEEPS": str(sleeps)}
        if sock:
            env["CCBOARD_TMUX_SOCKET"] = sock
        # run it the way ttyd does (the shebang), except under the other shell
        cmd = [str(ATTACH), *args] if request.param == "/bin/sh" else [request.param, str(ATTACH), *args]
        cp = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=20)
        argv = record.read_text().splitlines() if record.exists() else None
        return cp.returncode, cp.stdout, argv, sleeps.exists()
    return run


def test_script_is_executable_posix_sh_and_carries_the_v2_marker():
    assert os.access(ATTACH, os.X_OK)
    text = ATTACH.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert "# ccboard-attach v2" in text[:200], "app/doctor.py's wrapper check looks for this marker in the first 4 KB"


@pytest.mark.parametrize("args, tmux_argv", [
    (("shop--api--s1",), ["-N", "-L", "ccboard", "attach-session", "-t", "=shop--api--s1"]),
    (("shop--api--s1", "full"), ["-N", "-L", "ccboard", "attach-session", "-t", "=shop--api--s1"]),
    (("shop--api--s1", "grid"), ["-N", "-L", "ccboard", "attach-session", "-f", "ignore-size", "-t", "=shop--api--s1"]),
    (("shop--api--s1", "ro"), ["-N", "-L", "ccboard", "attach-session", "-r", "-t", "=shop--api--s1"]),
])
def test_modes_exec_the_right_tmux_command(attach, args, tmux_argv):
    rc, out, argv, slept = attach(*args)
    assert (rc, out, argv, slept) == (0, "", tmux_argv, False)


def test_socket_comes_from_the_environment(attach):
    assert attach("a--b--c", "grid", sock="other")[2] == ["-N", "-L", "other", "attach-session", "-f", "ignore-size", "-t", "=a--b--c"]


@pytest.mark.parametrize("mode", ["", "GRID", "Full", "-f", "-r", "grid ", " grid", "full;ls", "x", "ignore-size", "rw"])
def test_bad_modes_fail_without_attaching(attach, mode):
    rc, out, argv, slept = attach("shop--api--s1", mode)
    assert rc == 1 and argv is None and "invalid mode" in out and slept, (mode, out)


@pytest.mark.parametrize("args", [(), ("a--b--c", "grid", "extra"), ("a--b--c", "grid", ""), ("a", "b", "c", "d")])
def test_wrong_argument_counts_print_usage(attach, args):
    rc, out, argv, slept = attach(*args)
    assert rc == 1 and argv is None and "usage: ccboard-attach <session> [full|grid|ro]" in out and slept


HOSTILE = [
    ("", "empty session name"),
    ("a;b", "invalid session name"),
    ("a b", "invalid session name"),
    ("$(x)", "invalid session name"),
    ("`x`", "invalid session name"),
    ("a\nb", "invalid session name"),
    ("a\tb", "invalid session name"),
    ("a:b", "invalid session name"),
    ("a.b", "invalid session name"),
    ("=a", "invalid session name"),
    ("-t", None),                       # valid characters: reaches tmux as '=-t', an exact session name, never as an option
    ("a" * 201, "session name too long"),
]


@pytest.mark.parametrize("name, why", HOSTILE)
@pytest.mark.parametrize("mode", [None, "grid"])
def test_hostile_names_never_reach_tmux_as_anything_but_one_exact_target(attach, name, why, mode):
    rc, out, argv, slept = attach(*([name] if mode is None else [name, mode]))
    if why is None:
        assert rc == 0 and argv[-2:] == ["-t", "=" + name], argv
    else:
        assert rc == 1 and argv is None and why in out and slept, (name, out)


def test_200_characters_is_the_longest_name_that_attaches(attach):
    assert attach("a" * 200)[0] == 0
    assert attach("a" * 201)[0] == 1


@pytest.mark.parametrize("name", ["_ccboard-x", "_ccboard", "_ccboard-login-x", "_ccboardlogin", "_ccboard-tmux", "_ccboard-loginx"])
@pytest.mark.parametrize("mode", [None, "full", "grid", "ro"])
def test_other_internal_sessions_are_refused_in_every_mode(attach, name, mode):
    rc, out, argv, slept = attach(*([name] if mode is None else [name, mode]))
    assert rc == 1 and argv is None and "internal session" in out and slept


@pytest.mark.parametrize("name", ["_ccboard-login", "_ccboard-login-codex"])
def test_login_sessions_attach_in_full_mode_only(attach, name):
    assert attach(name)[2] == ["-N", "-L", "ccboard", "attach-session", "-t", "=" + name]
    assert attach(name, "full")[2] == ["-N", "-L", "ccboard", "attach-session", "-t", "=" + name]
    for mode in ("grid", "ro", ""):
        rc, out, argv, slept = attach(name, mode)
        assert rc == 1 and argv is None and "login session is full only" in out, (name, mode)
