"""Issue #125: the board asks its own tmux server where the socket is (tmux.socket_path), hooks compare a hook's $TMUX with it, and
the doctor explains the socket. Fake tmux scripts on a temp PATH and temp directories only; never the real socket directory."""
import os
import types

import pytest

from app import doctor, hooks, platform as plat, tmux
from app.config import settings

UID = 4242      # a made-up uid; the old rule is pinned against it, so no real id is needed


@pytest.fixture(autouse=True)
def _fresh_cache():
    """socket_path() keeps its answer in module state: every test starts with none."""
    tmux._sock.update(key=None, path=None, at=0.0, up=None)
    yield
    tmux._sock.update(key=None, path=None, at=0.0, up=None)


@pytest.fixture
def uid(monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: UID)          # plat.current_uid reads it, and so did the old hooks code


@pytest.fixture
def name(monkeypatch):
    monkeypatch.setattr(settings, "tmux_socket", "ccboard")
    return "ccboard"


# ---- the old rule, pinned before the change (green on the unchanged code): while socket_path() is None the uid substring decides

@pytest.mark.parametrize("env, want", [
    (f"/tmp/tmux-{UID}/ccboard,12345,0", True),
    (f"/private/tmp/tmux-{UID}/ccboard,12345,0", True),
    (f"/tmp/tmux-{UID}/other,12345,0", False),                 # another socket name
    (f"/tmp/tmux-{UID + 1}/ccboard,12345,0", False),           # another user's directory
    (f"/tmp/tmux-{UID}/ccboard-x,12345,0", False),             # a longer name is not ours
    (f"/tmp/tmux-{UID}/ccboard", False),                       # no comma after the name
    ("", False),
    (None, False),
])
def test_old_uid_rule_while_socket_path_is_unknown(monkeypatch, uid, name, env, want):
    monkeypatch.setattr(tmux, "socket_path", lambda fresh=False: None)
    assert hooks._our_socket(env) is want


# ---- the new rule: the first field of $TMUX against what the server says, after realpath on both

@pytest.fixture
def ours(monkeypatch, tmp_path, uid, name):
    """A socket directory standing in for /private/tmp/tmux-<uid>, a link `tmp` standing in for /tmp -> /private/tmp, and socket_path()
    answering with the real path."""
    real = tmp_path / "real" / f"tmux-{UID}"
    real.mkdir(parents=True)
    (tmp_path / "tmp").symlink_to(tmp_path / "real")
    path = str(real / "ccboard")
    monkeypatch.setattr(tmux, "socket_path", lambda fresh=False: path)
    return types.SimpleNamespace(path=path, linked=str(tmp_path / "tmp" / f"tmux-{UID}" / "ccboard"), real=real, root=tmp_path)


def test_same_path_is_ours(ours):
    assert hooks._our_socket(f"{ours.path},12345,0") is True


def test_a_path_through_a_symlinked_directory_is_ours(ours):
    assert ours.linked != ours.path
    assert hooks._our_socket(f"{ours.linked},12345,0") is True


def test_another_socket_name_is_not_ours(ours):
    assert hooks._our_socket(f"{ours.real}/other,12345,0") is False


def test_another_uid_directory_is_not_ours(ours):
    other = ours.root / "real" / f"tmux-{UID + 1}"
    other.mkdir()
    assert hooks._our_socket(f"{other}/ccboard,12345,0") is False


def test_an_s_style_path_equal_to_socket_path_is_ours(monkeypatch, tmp_path, uid, name):
    """`tmux -S <path>` keeps its socket anywhere: no tmux-<uid> directory, a name that is not ours; equal to socket_path() is enough."""
    custom = tmp_path / "somewhere" / "board.sock"
    custom.parent.mkdir()
    monkeypatch.setattr(tmux, "socket_path", lambda fresh=False: str(custom))
    assert hooks._our_socket(f"{custom},77,1") is True
    assert hooks._our_socket(f"{tmp_path}/somewhere/other.sock,77,1") is False


def test_the_old_uid_shape_no_longer_matches_when_the_server_names_another_path(monkeypatch, uid, name, tmp_path):
    """socket_path() known: the substring is not consulted, so a foreign server that looks like ours (same uid directory) is refused."""
    monkeypatch.setattr(tmux, "socket_path", lambda fresh=False: str(tmp_path / "elsewhere" / "ccboard"))
    assert hooks._our_socket(f"/tmp/tmux-{UID}/ccboard,12345,0") is False


@pytest.mark.parametrize("env", ["", None, ",12345,0"])
def test_empty_tmux_env_is_not_ours(ours, env):
    assert hooks._our_socket(env) is False


def test_no_uid_and_no_socket_path_is_not_ours(monkeypatch, name):
    monkeypatch.setattr(tmux, "socket_path", lambda fresh=False: None)
    monkeypatch.setattr(plat, "current_uid", lambda: None)
    assert hooks._our_socket(f"/tmp/tmux-{UID}/ccboard,12345,0") is False


def test_no_uid_but_a_known_socket_path_still_matches(ours, monkeypatch):
    monkeypatch.setattr(plat, "current_uid", lambda: None)
    assert hooks._our_socket(f"{ours.path},1,0") is True


# ---- tmux.socket_path() against a fake tmux on a temp PATH

FAKE_TMUX = """#!/bin/sh
# argv: -N -L <name> <command...>; state lives in $FAKE_TMUX_DIR (down, nofmt, fail, socket, session)
d=$FAKE_TMUX_DIR
shift 3
echo "$*" >> "$d/log"
case "$1" in
  list-sessions) [ -e "$d/down" ] && { echo "no server running on /x" >&2; exit 1; }; exit 0 ;;
  display-message)
    if [ -e "$d/down" ]; then echo "no server running on /x" >&2; exit 1; fi
    case "$*" in
      *socket_path*) [ -e "$d/fail" ] && { echo "unknown format" >&2; exit 1; }
                    [ -e "$d/nofmt" ] && { echo; exit 0; }
                    cat "$d/socket" ;;
      *session_name*) cat "$d/session" ;;
      *) echo ok ;;
    esac ;;
esac
"""


class FakeTmux:
    def __init__(self, tmp):
        self.dir = tmp / "faketmux"
        self.dir.mkdir()
        self.bin = tmp / "fakebin"
        self.bin.mkdir()
        exe = self.bin / "tmux"
        exe.write_text(FAKE_TMUX)
        exe.chmod(0o755)
        self.set_socket("/scratch/tmux-1/scratch")

    def set_socket(self, path):
        (self.dir / "socket").write_text(path + "\n")

    def flag(self, name, on=True):
        f = self.dir / name
        if on:
            f.touch()
        else:
            f.unlink(missing_ok=True)

    def calls(self, needle="socket_path"):
        log = self.dir / "log"
        return [ln for ln in (log.read_text().splitlines() if log.exists() else []) if needle in ln]


@pytest.fixture
def ftmux(monkeypatch, tmp_path):
    ft = FakeTmux(tmp_path)
    monkeypatch.setenv("PATH", f"{ft.bin}:/usr/bin:/bin")      # the fake first; cat and friends after it
    monkeypatch.setenv("FAKE_TMUX_DIR", str(ft.dir))
    monkeypatch.setattr(settings, "tmux_socket", "scratch")
    return ft


def test_socket_path_asks_tmux_with_the_board_base(ftmux):
    assert tmux.socket_path() == "/scratch/tmux-1/scratch"
    assert ftmux.calls() == ["display-message -p #{socket_path}"]


def test_socket_path_is_cached_no_tmux_process_per_call(ftmux):
    for _ in range(25):
        assert tmux.socket_path() == "/scratch/tmux-1/scratch"
    assert len(ftmux.calls()) == 1


def test_a_hook_in_the_steady_state_starts_no_tmux_process(ftmux, uid):
    env = "/scratch/tmux-1/scratch,99,0"
    assert hooks._our_socket(env) is True
    before = len(ftmux.calls(""))
    for _ in range(40):
        assert hooks._our_socket(env) is True
        assert hooks._our_socket("/scratch/tmux-1/foreign,99,0") is False
    assert len(ftmux.calls("")) == before


def test_socket_path_refreshes_when_server_up_flips(ftmux):
    assert tmux.socket_path() == "/scratch/tmux-1/scratch"
    ftmux.flag("down")
    assert tmux.server_up() is False                      # the flip clears the kept answer
    assert tmux.socket_path() is None
    ftmux.flag("down", False)
    ftmux.set_socket("/scratch/tmux-1/moved")
    assert tmux.server_up() is True                       # and the flip back clears the kept "no answer"
    assert tmux.socket_path() == "/scratch/tmux-1/moved"


def test_socket_path_none_when_tmux_is_down_and_not_asked_again_at_once(ftmux):
    ftmux.flag("down")
    assert tmux.socket_path() is None
    assert tmux.socket_path() is None
    assert len(ftmux.calls()) == 1                         # the miss is kept for a few seconds, not one process per hook


def test_a_kept_miss_ends_after_its_ttl(ftmux, monkeypatch):
    ftmux.flag("down")
    assert tmux.socket_path() is None
    ftmux.flag("down", False)
    monkeypatch.setattr(tmux, "_SOCK_MISS_TTL", 0.0)
    assert tmux.socket_path() == "/scratch/tmux-1/scratch"


def test_socket_path_fresh_asks_again(ftmux):
    assert tmux.socket_path() == "/scratch/tmux-1/scratch"
    ftmux.set_socket("/scratch/tmux-1/other")
    assert tmux.socket_path() == "/scratch/tmux-1/scratch"
    assert tmux.socket_path(fresh=True) == "/scratch/tmux-1/other"


@pytest.mark.parametrize("flag", ["nofmt", "fail"])
def test_socket_path_none_for_a_tmux_that_rejects_the_format(ftmux, flag):
    ftmux.flag(flag)
    assert tmux.socket_path() is None


def test_socket_path_none_without_tmux_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))              # nothing on it
    assert tmux.socket_path() is None


def test_socket_path_is_per_socket_name(ftmux, monkeypatch):
    assert tmux.socket_path() == "/scratch/tmux-1/scratch"
    ftmux.set_socket("/scratch/tmux-1/second")
    monkeypatch.setattr(settings, "tmux_socket", "second")
    assert tmux.socket_path() == "/scratch/tmux-1/second"


# ---- resolve_session: a pane on our socket resolves to its session, a foreign one does not

NAME = "proj--repo--s1"


def test_resolve_session_resolves_a_pane_through_a_matching_socket(ftmux):
    (ftmux.dir / "session").write_text(NAME + "\n")
    headers = {"x-ccboard-pane": "%3", "x-ccboard-tmux": "/scratch/tmux-1/scratch,12345,0"}
    assert hooks.resolve_session(headers, {}, {}) == (NAME, "pane")


def test_resolve_session_refuses_a_pane_id_from_a_foreign_socket(ftmux):
    (ftmux.dir / "session").write_text(NAME + "\n")
    headers = {"x-ccboard-pane": "%3", "x-ccboard-tmux": "/scratch/tmux-1/foreign,12345,0"}
    assert hooks.resolve_session(headers, {}, {}) == (None, "foreign_tmux")      # and the folder is not tried either (box check 63)
    assert not ftmux.calls("-t %3")                        # the pane id was never put to our server


def test_resolve_session_through_a_symlinked_socket_directory(ftmux, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "tmp").symlink_to(real)
    ftmux.set_socket(str(real / "scratch"))
    (ftmux.dir / "session").write_text(NAME + "\n")
    headers = {"x-ccboard-pane": "%3", "x-ccboard-tmux": f"{tmp_path}/tmp/scratch,1,0"}
    assert hooks.resolve_session(headers, {}, {}) == (NAME, "pane")


# ---- doctor: tmux-socket

@pytest.fixture
def sockdir(ftmux, tmp_path, monkeypatch):
    """A private socket directory holding a (plain file) socket, which the fake tmux reports; the length limit is lifted for it, since
    a pytest temp path alone can pass 100 bytes."""
    d = tmp_path / "tmux-1"
    d.mkdir()
    d.chmod(0o700)
    sock = d / "scratch"
    sock.touch()
    ftmux.set_socket(str(sock))
    monkeypatch.setattr(doctor, "SOCKET_PATH_MAX", 4096)
    return types.SimpleNamespace(dir=d, sock=sock)


def test_doctor_tmux_socket_is_registered_in_the_terminal_group():
    assert [c[1] for c in doctor.CHECKS if c[0] == "tmux-socket"] == ["terminal"]
    assert doctor.SOCKET_PATH_MAX == 100


def test_doctor_tmux_socket_passes_and_shows_path_mode_and_length(sockdir):
    out = doctor._c_tmux_socket(None)
    assert out.status == "pass" and out.fix is None
    assert str(sockdir.sock) in out.detail and "0700" in out.detail and "bytes" in out.detail


@pytest.mark.parametrize("mode", [0o777, 0o770, 0o707, 0o720])
def test_doctor_tmux_socket_warns_when_the_directory_is_group_or_world_writable(sockdir, mode):
    sockdir.dir.chmod(mode)
    out = doctor._c_tmux_socket(None)
    assert out.status == "warn" and "writable by group or others" in out.detail and f"{mode:04o}" in out.detail
    assert out.fix["cmd"].startswith("chmod 700 ")


def test_doctor_tmux_socket_allows_a_group_readable_directory(sockdir):
    sockdir.dir.chmod(0o750)
    assert doctor._c_tmux_socket(None).status == "pass"


def test_doctor_tmux_socket_warns_when_the_path_is_over_100_bytes(ftmux, tmp_path):
    deep = tmp_path / ("d" * 60) / ("e" * 60)
    deep.mkdir(parents=True)
    deep.chmod(0o700)
    sock = deep / "scratch"
    sock.touch()
    ftmux.set_socket(str(sock))
    out = doctor._c_tmux_socket(None)
    assert len(str(sock).encode()) > 100
    assert out.status == "warn" and "over the 100 byte limit" in out.detail and "TMUX_TMPDIR" in out.fix["text"]


def test_doctor_tmux_socket_warns_with_the_manual_fix_when_the_socket_file_is_gone(ftmux, tmp_path, monkeypatch):
    """Server alive, socket file removed: tmux cannot be reached (so socket_path() is None); the process list names the server."""
    gone = tmp_path / "tmux-1" / "scratch"
    ftmux.flag("down")                                      # a client cannot connect without the socket file
    monkeypatch.setattr(doctor, "_tmux_server_sockets", lambda: [(4321, str(gone))])
    sent = []
    monkeypatch.setattr(os, "kill", lambda *a: sent.append(a))
    out = doctor._c_tmux_socket(None)
    assert out.status == "warn" and "4321" in out.detail and str(gone) in out.detail and "gone" in out.detail
    assert "SIGUSR1" in out.fix["text"] and "never sends it" in out.fix["text"] and out.fix["cmd"] == "kill -USR1 4321"
    assert sent == []                                       # nothing was signalled


def test_doctor_tmux_socket_alive_but_unreachable_with_the_file_present_warns_differently(ftmux, tmp_path, monkeypatch):
    present = tmp_path / "scratch"
    present.touch()
    ftmux.flag("down")
    monkeypatch.setattr(doctor, "_tmux_server_sockets", lambda: [(77, str(present))])
    out = doctor._c_tmux_socket(None)
    assert out.status == "warn" and "does not answer" in out.detail and "SIGUSR1" not in out.fix["text"]


def test_doctor_tmux_socket_skips_when_tmux_is_down(ftmux, monkeypatch):
    ftmux.flag("down")
    monkeypatch.setattr(doctor, "_tmux_server_sockets", lambda: [])
    assert doctor._c_tmux_socket(None).status == "skip"
    monkeypatch.setattr(doctor, "_tmux_server_sockets", lambda: None)       # process list unreadable: still no claim
    assert doctor._c_tmux_socket(None).status == "skip"


def test_doctor_tmux_socket_never_asks_tmux_for_session_content(sockdir, ftmux):
    doctor._c_tmux_socket(None)
    assert [c for c in ftmux.calls("") if "capture" in c or "session_name" in c or "send-keys" in c] == []


def test_parse_server_titles_keeps_only_our_socket_name():
    text = ("  101 tmux: server (/tmp/tmux-1000/ccboard)\n"
            "  202 tmux: server (/tmp/tmux-1000/default)\n"
            "  303 tmux: client\n"
            "  404 grep tmux: server (/tmp/tmux-1000/ccboard)\n"
            "  505 tmux: server (/private/tmp/tmux-501/ccboard) (tmux)\n")
    assert doctor._parse_server_titles(text, "ccboard") == [(101, "/tmp/tmux-1000/ccboard"), (505, "/private/tmp/tmux-501/ccboard")]
    assert doctor._parse_server_titles("", "ccboard") == []


def test_tmux_server_sockets_reads_ps_and_gives_none_when_ps_is_missing(monkeypatch):
    monkeypatch.setattr(settings, "tmux_socket", "ccboard")
    ok = doctor.Proc(0, "  9 tmux: server (/tmp/tmux-1000/ccboard)\n", "")
    monkeypatch.setattr(doctor, "_run", lambda argv, timeout=1: ok)
    assert doctor._tmux_server_sockets() == [(9, "/tmp/tmux-1000/ccboard")]

    def missing(argv, timeout=1):
        raise doctor.ToolMissing("ps")
    monkeypatch.setattr(doctor, "_run", missing)
    assert doctor._tmux_server_sockets() is None
