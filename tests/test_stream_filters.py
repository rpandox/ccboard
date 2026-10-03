"""v0.5.5 backend bits: GET /api/stream?names=a,b&lines=12 (validation 400s before the stream starts, the filter, the tail length,
the tick that stays unfiltered) and state.rate_limited (always present: null, or the kv record with the message cut to 200).

tmux is the conftest fake; its capture() answers one screen for every session, so the tests that need a screen per session swap in
their own capture() after fake_tmux (it also records the calls: a filtered stream must not capture the sessions it was not asked for).
"""
import json
import re

import pytest

from app import main, tmux

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
A, B, C = "shop--api--s1", "shop--api--s2", "blog--web--s3"


def session(name, command="claude"):
    return {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": command, "path": "/x", "pid": 1, "env": {}}


@pytest.fixture
def board(lite_client, fake_tmux, monkeypatch):
    """Three sessions with their own screens (line i of session X reads 'X:i'), an internal one, and a recorder of capture() calls."""
    for n in (A, B, C, "_ccboard-login"):
        fake_tmux["sessions"][n] = session(n)
    calls = []
    screens = {n: "\n".join(f"{n}:{i}" for i in range(1, 31)) + "\n\n" for n in (A, B, C, "_ccboard-login")}

    def capture(name, lines=200, join=True, escapes=False):
        calls.append((name, lines))
        return screens.get(name, "")

    monkeypatch.setattr(tmux, "capture", capture)
    main._invalidate_scan()
    return type("Board", (), {"client": lite_client, "calls": calls, "screens": screens, "tmux": fake_tmux})


def events(client, query=""):
    """GET /api/stream?once=1&<query> -> [(event, data)] in order."""
    with client.stream("GET", "/api/stream?once=1" + ("&" + query if query else ""), headers=H) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    out = []
    for block in re.split(r"\r?\n\r?\n", body):
        ev = re.search(r"^event: (\S+)", block, re.M)
        data = re.search(r"^data: (.*)$", block, re.M)
        if ev and data:
            out.append((ev.group(1), json.loads(data.group(1))))
    return out


def lines_events(evs):
    return {d["name"]: d["lines"] for e, d in evs if e == "lines"}


def ticks(evs):
    return [d["sessions"] for e, d in evs if e == "tick"]


# ---------------------------------------------------------------- the query, validated

def test_stream_query_defaults():
    assert main._stream_query() == (None, 12)
    assert main.LIVE_LINES == 12 and main.LIVE_MAX_LINES == 40 and main.LIVE_MAX_NAMES == 20


def test_stream_query_parses_names_and_lines():
    assert main._stream_query(names=A) == ({A}, 12)
    assert main._stream_query(names=f"{A},{C}", lines="3") == ({A, C}, 3)
    assert main._stream_query(names=f" {A} , {A} ,{B}") == ({A, B}, 12), "whitespace is trimmed and a repeated name counts once"
    assert main._stream_query(lines=" 40 ")[1] == 40 and main._stream_query(lines="1")[1] == 1


@pytest.mark.parametrize("names", ["", ",", f"{A},", f"{A},,{B}", "nope", f"{A},nope", "a--b", "a--b--c--d", "_ccboard-login", "a b--c--d", f"{A};{B}"])
def test_stream_query_rejects_bad_names(names):
    with pytest.raises(main.projects.BadRequest, match="names"):
        main._stream_query(names=names)


def test_stream_query_caps_the_name_list_at_twenty():
    ok = ",".join(f"p--r--s{i}" for i in range(20))
    assert len(main._stream_query(names=ok)[0]) == 20
    with pytest.raises(main.projects.BadRequest, match="at most 20"):
        main._stream_query(names=ok + ",p--r--s20")
    assert len(main._stream_query(names=ok + ",p--r--s0")[0]) == 20, "duplicates do not count towards the cap"


@pytest.mark.parametrize("lines", ["0", "41", "-1", "abc", "", "1.5", "12x", "1e1"])
def test_stream_query_rejects_bad_lines(lines):
    with pytest.raises(main.projects.BadRequest, match="lines"):
        main._stream_query(lines=lines)


@pytest.mark.parametrize("query", ["names=nope", f"names={A},nope", "names=", f"names={A},,{B}", "names=_ccboard-login", "lines=0", "lines=41",
                                   "lines=abc", "lines=", f"names={A}&lines=99", "names=nope&lines=5"])
def test_bad_query_is_a_400_with_the_error_text_and_no_stream(board, query):
    r = board.client.get("/api/stream?once=1&" + query, headers=H)
    assert r.status_code == 400, query
    assert r.headers["content-type"].startswith("application/json") and r.json()["error"], r.text
    assert board.calls == [], "nothing was captured for a refused request"


def test_more_than_twenty_names_is_a_400(board):
    names = ",".join(f"p--r--s{i}" for i in range(21))
    assert board.client.get(f"/api/stream?once=1&names={names}", headers=H).status_code == 400


def test_stream_still_needs_the_identity_header(board):
    assert board.client.get("/api/stream?once=1").status_code == 403
    assert board.client.get(f"/api/stream?once=1&names={A}").status_code == 403


# ---------------------------------------------------------------- filtering

def test_without_names_every_session_streams_like_before(board):
    evs = events(board.client)
    got = lines_events(evs)
    assert set(got) == {A, B, C}, "the internal login session never streams"
    assert got[A][-1] == f"{A}:30" and len(got[A]) == 12, "the default tail is 12 lines"
    assert {n for n, _ in board.calls} == {A, B, C}


def test_names_limit_the_lines_events_and_the_captures(board):
    evs = events(board.client, f"names={B}")
    assert list(lines_events(evs)) == [B]
    assert [n for n, _ in board.calls] == [B], "only the asked-for session is captured"
    evs = events(board.client, f"names={A},{C}")
    assert set(lines_events(evs)) == {A, C}


def test_the_tick_is_unfiltered_and_lists_every_live_session(board):
    filtered = events(board.client, f"names={A}")
    plain = events(board.client)
    assert ticks(filtered) == ticks(plain) == [sorted([A, B, C])], "tick events do not change with the filter; internal sessions stay out"
    assert [e for e, _ in filtered][-1] == "tick", "one tick closes each pass"


def test_a_name_that_is_not_running_streams_nothing_and_is_not_an_error(board):
    evs = events(board.client, "names=blog--web--gone")
    assert lines_events(evs) == {} and ticks(evs) == [sorted([A, B, C])]
    assert board.calls == []
    evs = events(board.client, f"names=blog--web--gone,{A}")
    assert list(lines_events(evs)) == [A]


def test_a_name_filter_cannot_reach_an_internal_session(board):
    """Session names start with a letter or digit, so an internal name (_ccboard-login, _ccboard--x--y) is a 400, never a capture."""
    board.tmux["sessions"]["_ccboard--x--y"] = session("_ccboard--x--y")
    for n in ("_ccboard--x--y", "_ccboard-login"):
        r = board.client.get(f"/api/stream?once=1&names={n}", headers=H)
        assert r.status_code == 400
    assert board.calls == []
    assert "_ccboard--x--y" not in ticks(events(board.client))[0], "and an unfiltered stream skips them too"


def test_tmux_down_is_an_empty_pass(board, monkeypatch):
    def down():
        raise tmux.TmuxDown("no server")
    monkeypatch.setattr(tmux, "list_sessions", down)
    evs = events(board.client, f"names={A}")
    assert lines_events(evs) == {} and ticks(evs) == [[]]


# ---------------------------------------------------------------- the tail length

def test_lines_sets_the_tail_and_the_capture_depth(board):
    got = lines_events(events(board.client, f"names={A}&lines=3"))
    assert got[A] == [f"{A}:28", f"{A}:29", f"{A}:30"]
    assert board.calls == [(A, 3)]
    board.calls.clear()
    got = lines_events(events(board.client, f"names={A}&lines=40"))
    assert len(got[A]) == 30 and got[A][0] == f"{A}:1", "a pane with fewer lines than asked shows what it has"
    assert board.calls == [(A, 40)]


def test_the_default_tail_is_twelve_lines_and_trailing_blank_lines_are_dropped(board):
    got = lines_events(events(board.client, f"names={C}"))
    assert got[C] == [f"{C}:{i}" for i in range(19, 31)]
    board.screens[C] = "$ build\n\n  ok   \n\n\n"
    assert lines_events(events(board.client, f"names={C}"))[C] == ["$ build", "", "  ok"]


def test_capture_all_returns_the_live_names_and_the_filtered_tails(board):
    names, snap = main._capture_all({A}, 2)
    assert names == sorted([A, B, C]) and snap == {A: [f"{A}:29", f"{A}:30"]}
    names, snap = main._capture_all(None, 1)
    assert snap == {A: [f"{A}:30"], B: [f"{B}:30"], C: [f"{C}:30"]}


def test_a_failing_capture_skips_that_session_only(board, monkeypatch):
    real = tmux.capture

    def flaky(name, lines=200, join=True, escapes=False):
        if name == B:
            raise tmux.TmuxError("pane gone")
        return real(name, lines=lines, join=join, escapes=escapes)

    monkeypatch.setattr(tmux, "capture", flaky)
    evs = events(board.client)
    assert set(lines_events(evs)) == {A, C} and ticks(evs) == [sorted([A, B, C])]


# ---------------------------------------------------------------- state.rate_limited

def state(client):
    main._invalidate_scan()
    return client.get("/api/state", headers=H).json()


def test_state_always_carries_rate_limited_null_when_nothing_is_recorded(lite_client, fake_tmux):
    st = state(lite_client)
    assert "rate_limited" in st and st["rate_limited"] is None


def test_state_rate_limited_is_the_kv_record_with_the_message_cut_to_200(lite_client, fake_tmux):
    record = {"session": A, "message": "You've hit your session limit. " + "x" * 400, "kind": "5h", "resets_at": 1791006000}
    main.db.kv_set("rate_limited", record)
    rl = state(lite_client)["rate_limited"]
    assert rl["value"]["session"] == A and rl["value"]["kind"] == "5h" and rl["value"]["resets_at"] == 1791006000
    assert len(rl["value"]["message"]) == 200 and rl["value"]["message"] == record["message"][:200]
    assert rl["at"], "the kv's own timestamp rides along, as for the other kv-backed keys"
    assert main.db.kv_get("rate_limited")["value"]["message"] == record["message"], "only the payload is cut; the kv keeps the whole text"


def test_state_rate_limited_keeps_short_messages_and_tolerates_odd_records(lite_client, fake_tmux):
    main.db.kv_set("rate_limited", {"session": A, "message": "limit hit"})
    assert state(lite_client)["rate_limited"]["value"] == {"session": A, "message": "limit hit"}
    main.db.kv_set("rate_limited", {"session": A, "message": None, "kind": "7d", "resets_at": None})
    assert state(lite_client)["rate_limited"]["value"]["kind"] == "7d"
    main.db.kv_set("rate_limited", "a plain string")
    assert state(lite_client)["rate_limited"]["value"] == "a plain string", "never a 500 over a record someone wrote by hand"


def test_the_dismiss_endpoint_clears_it_again(lite_client, fake_tmux):
    main.db.kv_set("rate_limited", {"session": A, "message": "limit hit", "kind": "5h", "resets_at": 1})
    assert state(lite_client)["rate_limited"]
    assert lite_client.post("/api/usage/rate-limit/clear", headers=H).status_code == 200
    assert state(lite_client)["rate_limited"] is None
