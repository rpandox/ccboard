"""app/tmux.py's terminal plumbing against a patched tmux.run: clients, viewers, pane_info, copy-mode and scrolling, and what
send_text / send_keys do when the pane is in copy-mode. No tmux is started; the real-tmux behaviour of these argvs was checked by
hand on tmux 3.6 (see the commit notes)."""
import subprocess

import pytest

from app import tmux

T = "=shop--api--s1:"
NAME = "shop--api--s1"
FULL, GRID, RO, CTRL = "attached,focused,UTF-8", "attached,focused,ignore-size,UTF-8", "attached,focused,ignore-size,read-only,UTF-8", "attached,control-mode"


def pane_line(alt=0, mode=0, pos="", hist=120, cols=80, rows=24, wcols=80, wrows=24, cmd="bash"):
    return "\t".join(str(x) for x in (alt, mode, pos, hist, cols, rows, wcols, wrows, cmd)) + "\n"


class FakeRun:
    """Stands in for tmux.run: display-message answers the queued pane lines (the last one repeats), list-clients and
    list-sessions answer what the test sets, everything else succeeds. Every call is recorded as its argv tuple."""

    def __init__(self, panes=(), clients="", sessions="", fail=None):
        self.panes, self.clients, self.sessions, self.fail, self.calls = list(panes), clients, sessions, fail, []

    def __call__(self, *args, timeout=5, check=True, input=None):
        self.calls.append(args if input is None else args + (input,))
        if self.fail:
            exc = self.fail(args)
            if exc:
                raise exc
        out = ""
        if args[0] == "display-message":
            out = self.panes.pop(0) if len(self.panes) > 1 else (self.panes[0] if self.panes else pane_line())
        elif args[0] == "list-clients":
            out = self.clients
        elif args[0] == "list-sessions":
            out = self.sessions
        return subprocess.CompletedProcess(args, 0, out, "")

    def others(self):
        """Calls that are neither display-message nor list-*: what the code under test actually did to the pane."""
        return [c for c in self.calls if c[0] not in ("display-message", "list-clients", "list-sessions", "list-panes")]

    def count(self, cmd):
        return sum(1 for c in self.calls if c[0] == cmd)


@pytest.fixture
def fr(monkeypatch):
    def make(**kw):
        f = FakeRun(**kw)
        monkeypatch.setattr(tmux, "run", f)
        return f
    monkeypatch.setattr(tmux.time, "sleep", lambda *_: None)
    return make


# ---------------------------------------------------------------- clients, real_clients, viewers

def test_clients_parses_session_and_flags(fr):
    f = fr(clients=f"{NAME}\t{FULL}\nother--x--y\t{GRID}\n")
    cl = tmux.clients()
    assert f.calls == [("list-clients", "-F", "#{client_session}\t#{client_flags}")]
    assert cl == [{"session": NAME, "flags": {"attached", "focused", "UTF-8"}},
                  {"session": "other--x--y", "flags": {"attached", "focused", "ignore-size", "UTF-8"}}]
    assert tmux.clients(NAME) == cl[:1] and tmux.clients("nobody") == []
    assert all(isinstance(c["flags"], set) for c in cl)


def test_no_clients_is_empty_not_an_error(fr):
    fr(clients="")
    assert tmux.clients() == [] and tmux.real_clients(NAME) == 0 and tmux.viewers() == {}


def test_ignored_flags_constant():
    assert tmux.IGNORED_CLIENT_FLAGS == {"ignore-size", "read-only", "control-mode"}


@pytest.mark.parametrize("flags, expected", [
    (FULL, 1), ("attached", 1), ("attached,focused,UTF-8,no-detach-on-destroy", 1),
    (GRID, 0), ("ignore-size", 0), (RO, 0), ("read-only", 0), (CTRL, 0), ("attached,control-mode,ignore-size", 0),
])
def test_real_clients_excludes_ignore_size_read_only_and_control_mode(fr, flags, expected):
    fr(clients=f"{NAME}\t{flags}\n")
    assert tmux.real_clients(NAME) == expected


def test_real_clients_counts_only_that_sessions_full_clients(fr):
    fr(clients="\n".join([f"{NAME}\t{FULL}", f"{NAME}\t{FULL}", f"{NAME}\t{GRID}", f"{NAME}\t{RO}", f"{NAME}\t{CTRL}",
                          f"other--x--y\t{FULL}", f"\t{FULL}"]) + "\n")
    assert tmux.real_clients(NAME) == 2
    assert tmux.real_clients("other--x--y") == 1 and tmux.real_clients("none--x--y") == 0


def test_viewers_classification_checks_read_only_before_ignore_size(fr):
    fr(clients="\n".join([f"{NAME}\t{FULL}", f"{NAME}\t{GRID}", f"{NAME}\t{GRID}", f"{NAME}\t{RO}",    # -r sets read-only AND ignore-size
                          f"{NAME}\t{CTRL}", f"b--c--d\t{RO}", f"e--f--g\t{CTRL}", f"\t{FULL}"]) + "\n")
    assert tmux.viewers() == {NAME: {"full": 1, "grid": 2, "ro": 1}, "b--c--d": {"full": 0, "grid": 0, "ro": 1}}


def test_classification_helpers_are_pure():
    cl = [{"session": "a", "flags": {"read-only", "ignore-size"}}, {"session": "a", "flags": {"ignore-size"}}, {"session": "a", "flags": set()}]
    assert tmux.classify_viewers(cl) == {"a": {"full": 1, "grid": 1, "ro": 1}}
    assert tmux.count_real(cl) == 1 and tmux.count_real(cl, "a") == 1 and tmux.count_real(cl, "b") == 0


def test_list_clients_errors_propagate(monkeypatch):
    def down(*a, **k):
        raise tmux.TmuxDown("no server running")

    def err(*a, **k):
        raise tmux.TmuxError("boom")
    monkeypatch.setattr(tmux, "run", down)
    with pytest.raises(tmux.TmuxDown):
        tmux.real_clients(NAME)
    monkeypatch.setattr(tmux, "run", err)
    with pytest.raises(tmux.TmuxError):
        tmux.viewers()


# ---------------------------------------------------------------- list_sessions window size

def test_list_sessions_reads_the_window_size(fr):
    f = fr(sessions=f"{NAME}\t1700000000\t2\t3\t120\t40\nold--x--y\t5\t0\t1\n")        # a 4-field line (older format) still parses
    s = tmux.list_sessions()
    assert f.calls[0][:2] == ("list-sessions", "-F") and f.calls[0][2].endswith("#{window_width}\t#{window_height}")
    assert (s[NAME]["window_width"], s[NAME]["window_height"], s[NAME]["attached"], s[NAME]["windows"]) == (120, 40, 2, 3)
    assert (s["old--x--y"]["window_width"], s["old--x--y"]["window_height"]) == (0, 0)


def test_list_sessions_empty_window_fields_are_zero(fr):
    fr(sessions=f"{NAME}\t1\t0\t1\t\t\n")
    s = tmux.list_sessions()[NAME]
    assert (s["window_width"], s["window_height"]) == (0, 0)


# ---------------------------------------------------------------- pane_info

def test_pane_info_fields_and_argv(fr):
    f = fr(panes=[pane_line(alt=1, mode=0, pos="", hist=0, cols=100, rows=30, wcols=101, wrows=31, cmd="claude")])
    info = tmux.pane_info(NAME)
    assert info == {"alt": True, "in_mode": False, "scroll_pos": 0, "history": 0, "cols": 100, "rows": 30,
                    "win_cols": 101, "win_rows": 31, "cmd": "claude"}
    (call,) = f.calls
    assert call[:4] == ("display-message", "-p", "-t", T)
    assert call[4] == "\t".join("#{%s}" % v for v in ("alternate_on", "pane_in_mode", "scroll_position", "history_size", "pane_width",
                                                      "pane_height", "window_width", "window_height", "pane_current_command"))


def test_pane_info_empty_scroll_position_does_not_shift_the_fields(fr):
    """scroll_position is empty outside copy-mode: two adjacent tabs. A whitespace split would read history as the position."""
    fr(panes=[pane_line(alt=0, mode=0, pos="", hist=4321, cols=88, rows=22, wcols=90, wrows=23, cmd="zsh")])
    info = tmux.pane_info(NAME)
    assert info["scroll_pos"] == 0 and info["history"] == 4321 and (info["cols"], info["rows"]) == (88, 22)
    assert (info["win_cols"], info["win_rows"], info["cmd"]) == (90, 23, "zsh") and info["in_mode"] is False and info["alt"] is False


def test_pane_info_in_copy_mode_and_odd_commands(fr):
    fr(panes=[pane_line(alt=0, mode=1, pos=42, hist=500, cmd="node v22 --flag")])
    info = tmux.pane_info(NAME)
    assert info["in_mode"] is True and info["scroll_pos"] == 42 and info["history"] == 500 and info["cmd"] == "node v22 --flag"


def test_pane_info_tolerates_short_or_garbled_output(fr):
    fr(panes=["1\t1\n"])
    info = tmux.pane_info(NAME)
    assert info["alt"] is True and info["in_mode"] is True and info["history"] == 0 and info["cmd"] == ""
    fr(panes=["\n"])
    assert tmux.pane_info(NAME)["alt"] is False


def test_pane_info_errors_propagate(fr):
    fr(fail=lambda a: tmux.TmuxError("can't find session") if a[0] == "display-message" else None)
    with pytest.raises(tmux.TmuxError):
        tmux.pane_info(NAME)


# ---------------------------------------------------------------- leave_copy_mode

def test_leave_copy_mode_cancels_only_when_in_mode(fr):
    f = fr(panes=[pane_line(mode=0)])
    assert tmux.leave_copy_mode(NAME) is False and f.others() == []
    f = fr(panes=[pane_line(mode=1, pos=5)])
    assert tmux.leave_copy_mode(NAME) is True and f.others() == [("send-keys", "-t", T, "-X", "cancel")]


def test_leave_copy_mode_is_best_effort(fr):
    fr(fail=lambda a: tmux.TmuxError("x") if a[0] == "display-message" else None)
    assert tmux.leave_copy_mode(NAME) is False                       # the caller's own command reports the real error
    fr(panes=[pane_line(mode=1)], fail=lambda a: tmux.TmuxError("not in a mode") if a[:4] == ("send-keys", "-t", T, "-X") else None)
    assert tmux.leave_copy_mode(NAME) is False


# ---------------------------------------------------------------- scroll: the decision table

SEND = "send-keys"
CASES = [
    # (alt, in_mode, dir, n) -> commands issued
    (0, 0, "up", 1, [("copy-mode", "-e", "-u", "-t", T)]),
    (0, 0, "up", 3, [("copy-mode", "-e", "-u", "-t", T), (SEND, "-t", T, "-N", "2", "-X", "page-up")]),
    (0, 0, "down", 2, []),
    (0, 0, "top", 1, [("copy-mode", "-e", "-t", T), (SEND, "-t", T, "-X", "history-top")]),
    (0, 0, "bottom", 1, []),
    (0, 0, "exit", 1, []),
    (0, 1, "up", 1, [(SEND, "-t", T, "-X", "page-up")]),
    (0, 1, "up", 4, [(SEND, "-t", T, "-N", "4", "-X", "page-up")]),
    (0, 1, "down", 1, [(SEND, "-t", T, "-X", "page-down")]),
    (0, 1, "down", 10, [(SEND, "-t", T, "-N", "10", "-X", "page-down")]),
    (0, 1, "top", 1, [(SEND, "-t", T, "-X", "history-top")]),
    (0, 1, "bottom", 1, [(SEND, "-t", T, "-X", "cancel")]),
    (0, 1, "exit", 1, [(SEND, "-t", T, "-X", "cancel")]),
    (1, 0, "up", 1, [(SEND, "-t", T, "PageUp")]),
    (1, 0, "up", 3, [(SEND, "-t", T, "PageUp", "PageUp", "PageUp")]),
    (1, 0, "down", 2, [(SEND, "-t", T, "PageDown", "PageDown")]),
    (1, 0, "top", 5, [(SEND, "-t", T, "C-Home")]),
    (1, 0, "bottom", 5, [(SEND, "-t", T, "C-End")]),
    (1, 0, "exit", 1, []),
    # an alternate-screen pane that IS in tmux copy-mode (prefix [ ) is scrolled by tmux, and 'exit' gets the user out
    (1, 1, "up", 1, [(SEND, "-t", T, "-X", "page-up")]),
    (1, 1, "exit", 1, [(SEND, "-t", T, "-X", "cancel")]),
]


@pytest.mark.parametrize("alt, in_mode, dir, n, expected", CASES, ids=[f"alt{c[0]}-mode{c[1]}-{c[2]}-{c[3]}" for c in CASES])
def test_scroll_argv_per_decision_table(fr, alt, in_mode, dir, n, expected):
    f = fr(panes=[pane_line(alt=alt, mode=in_mode, pos=10 if in_mode else "")])
    tmux.scroll(NAME, dir, n)
    assert f.others() == expected


def test_scroll_never_sends_raw_keys_to_a_copy_mode_pane_or_copy_commands_to_an_app():
    for alt, in_mode, dir, n, expected in CASES:
        is_keys = any(a in ("PageUp", "PageDown", "C-Home", "C-End") for c in expected for a in c)
        assert is_keys == (alt == 1 and in_mode == 0 and dir != "exit")


def test_scroll_result_is_read_after_the_action(fr):
    f = fr(panes=[pane_line(alt=0, mode=0), pane_line(alt=0, mode=1, pos=24)])
    assert tmux.scroll(NAME, "up") == {"mode": "copy", "alt": False, "pos": 24}
    assert f.count("display-message") == 2
    f = fr(panes=[pane_line(alt=0, mode=1, pos=24), pane_line(alt=0, mode=0)])
    assert tmux.scroll(NAME, "exit") == {"mode": "normal", "alt": False, "pos": 0}
    f = fr(panes=[pane_line(alt=1, mode=0), pane_line(alt=1, mode=0)])
    assert tmux.scroll(NAME, "up") == {"mode": "app", "alt": True, "pos": 0}


def test_scroll_that_does_nothing_reads_the_pane_once(fr):
    f = fr(panes=[pane_line(alt=0, mode=0)])
    assert tmux.scroll(NAME, "exit") == {"mode": "normal", "alt": False, "pos": 0} and f.count("display-message") == 1
    f = fr(panes=[pane_line(alt=1, mode=0)])
    assert tmux.scroll(NAME, "exit")["mode"] == "app" and f.others() == [] and f.count("display-message") == 1


def test_scroll_agent_is_accepted(fr):
    f = fr(panes=[pane_line(alt=1)])
    tmux.scroll(NAME, "up", 1, agent="codex")
    tmux.scroll(NAME, "down", n=1, agent="claude")
    assert f.others() == [(SEND, "-t", T, "PageUp"), (SEND, "-t", T, "PageDown")]


@pytest.mark.parametrize("dir, n", [("sideways", 1), ("", 1), (None, 1), ("UP", 1), ("up", 0), ("up", 11), ("up", -1), ("up", "3"), ("up", 1.5), ("up", None), ("up", True)])
def test_scroll_rejects_bad_arguments_before_touching_tmux(fr, dir, n):
    f = fr()
    with pytest.raises(ValueError):
        tmux.scroll(NAME, dir, n)
    assert f.calls == []


def test_scroll_tolerates_the_pane_leaving_copy_mode_underneath(fr):
    """copy-mode -e leaves itself at the bottom: a page-down that arrives after that is the goal reached, not a 500."""
    f = fr(panes=[pane_line(mode=1, pos=3), pane_line(mode=0)],
           fail=lambda a: tmux.TmuxError("not in a mode") if a[:4] == (SEND, "-t", T, "-X") else None)
    assert tmux.scroll(NAME, "down")["mode"] == "normal"
    fr(panes=[pane_line(mode=1, pos=3)], fail=lambda a: tmux.TmuxError("no such pane") if a[:4] == (SEND, "-t", T, "-X") else None)
    with pytest.raises(tmux.TmuxError):
        tmux.scroll(NAME, "down")


# ---------------------------------------------------------------- send_text / send_keys / paste_text and copy-mode

def test_send_text_leaves_copy_mode_first(fr):
    f = fr(panes=[pane_line(mode=1, pos=7)])
    tmux.send_text(NAME, "yes", enter=True)
    assert f.others() == [(SEND, "-t", T, "-X", "cancel"), (SEND, "-t", T, "-l", "--", "yes"), (SEND, "-t", T, "Enter")]
    f = fr(panes=[pane_line(mode=0)])
    tmux.send_text(NAME, "yes", enter=True)
    assert f.others() == [(SEND, "-t", T, "-l", "--", "yes"), (SEND, "-t", T, "Enter")]


def test_send_text_enter_only_and_empty(fr):
    f = fr(panes=[pane_line(mode=1)])
    tmux.send_text(NAME, "", enter=True)
    assert f.others() == [(SEND, "-t", T, "-X", "cancel"), (SEND, "-t", T, "Enter")]


def test_multiline_send_text_pastes_after_leaving_copy_mode_exactly_once(fr):
    f = fr(panes=[pane_line(mode=1, pos=7)])
    tmux.send_text(NAME, "a\nb", enter=True)
    assert f.others() == [(SEND, "-t", T, "-X", "cancel"), ("load-buffer", "-b", "ccboard", "-", "a\nb"),
                          ("paste-buffer", "-p", "-d", "-b", "ccboard", "-t", T), (SEND, "-t", T, "Enter")]
    assert f.count("display-message") == 1


def test_paste_text_alone_leaves_copy_mode(fr):
    f = fr(panes=[pane_line(mode=1)])
    tmux.paste_text(NAME, "task prompt", enter=False)
    assert f.others() == [(SEND, "-t", T, "-X", "cancel"), ("load-buffer", "-b", "ccboard", "-", "task prompt"),
                          ("paste-buffer", "-p", "-d", "-b", "ccboard", "-t", T)]


SAFE_SETS = [["PageUp"], ["PageDown"], ["Up"], ["Down"], ["Escape"], ["PageUp", "PageDown"], ["Up", "Down", "Escape", "PageUp"]]
LEAVING_SETS = [["Enter"], ["C-c"], ["Left"], ["Tab"], ["BTab"], ["C-o"], ["C-Home"], ["C-End"], ["PageUp", "Enter"], ["Escape", "C-c"],
                ["Home"], ["End"], ["BSpace"], ["Space"], ["C-u"], ["C-l"], ["C-z"], ["Right"]]


@pytest.mark.parametrize("keys", SAFE_SETS)
def test_page_arrow_and_escape_keys_do_not_leave_copy_mode(fr, keys):
    f = fr(panes=[pane_line(mode=1, pos=7)])
    tmux.send_keys(NAME, keys)
    assert f.calls == [(SEND, "-t", T, *keys)], "no pane read, no cancel: the keys scroll or step inside the mode"


@pytest.mark.parametrize("keys", LEAVING_SETS)
def test_every_other_key_leaves_copy_mode_first(fr, keys):
    f = fr(panes=[pane_line(mode=1, pos=7)])
    tmux.send_keys(NAME, keys)
    assert f.others() == [(SEND, "-t", T, "-X", "cancel"), (SEND, "-t", T, *keys)]
    f = fr(panes=[pane_line(mode=0)])
    tmux.send_keys(NAME, keys)
    assert f.others() == [(SEND, "-t", T, *keys)]


def test_key_allow_gains_the_scroll_and_shift_tab_keys():
    assert {"BTab", "C-o", "C-Home", "C-End"} <= tmux.KEY_ALLOW
    assert {"Escape", "C-c", "Tab", "Up", "Down", "Left", "Right", "Enter", "BSpace", "Space", "PageUp", "PageDown",
            "Home", "End", "C-u", "C-l", "C-z"} <= tmux.KEY_ALLOW
    assert "C-d" not in tmux.KEY_ALLOW and "C-b" not in tmux.KEY_ALLOW


def test_send_keys_rejects_unknown_keys_before_any_tmux_call(fr):
    f = fr()
    with pytest.raises(ValueError):
        tmux.send_keys(NAME, ["Enter", "C-d"])
    assert f.calls == []
    tmux.send_keys(NAME, [])
    assert f.calls == []
