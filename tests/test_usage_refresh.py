"""POST /api/usage/refresh (v0.5.17f, part 2): the user-initiated usage refresh, app/usage_refresh.py and its route.

lite_client + fake_tmux (tests/conftest.py): no tmux, no claude, no workers. The follow-up the route schedules (Escape, then the cache read) is
captured by `later` and run by the test, so nothing waits four seconds and nothing starts a thread. Claude's state file is a fixture in a tmp
config dir; nothing here reads the real ~/.claude, ~/.claude.json or ~/.codex.
"""
import json
import subprocess

import pytest

from app import accounts, main, samples, tmux, usage_refresh
from app.config import settings
from tests.test_usage_cache import T0, UUID_A, cache_block, write_state

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
A, B, C = "shop--api--s1", "shop--api--s2", "shop--web--s3"


@pytest.fixture(autouse=True)
def _free_lock():
    usage_refresh.release()
    yield
    usage_refresh.release()


@pytest.fixture(autouse=True)
def later(monkeypatch):
    """usage_refresh.later collects (delay, fn) instead of starting a timer thread; a test runs fn itself. Autouse: no test of this file may start a real
    timer that would fire four seconds later against a torn-down board."""
    q = []
    monkeypatch.setattr(usage_refresh, "later", lambda delay, fn: q.append((delay, fn)))
    return q


@pytest.fixture
def board(lite_client, fake_tmux):
    """(client, store, add): add(name, ...) puts a row in the DB and a session in the fake tmux."""
    def add(name, state="idle", agent="claude", launcher="claude", command="claude", attached=0, at=None, **flags):
        parts = name.split("--")
        project, repo, sess = parts if len(parts) == 3 else ("internal", "login", name)         # an internal session has no ccboard name; its row is a trap, not a candidate
        main.db.add_session(tmux_name=name, project=project, repo=repo, name=sess, launcher=launcher, agent=agent)
        fake_tmux["sessions"][name] = {"created": 1, "attached": attached, "windows": 1, "pane_id": "%1", "command": command,
                                       "path": "/x", "pid": 1, "env": {}}
        if state:
            main.db.set_state(name, state, "Test")
        if flags:
            main.db.update_flags(name, flags)
        if at:
            with main.db.lock:
                main.db.conn.execute("UPDATE sessions SET state_at=? WHERE tmux_name=? AND ended_at IS NULL", (at, name))
                main.db.conn.commit()
        return name
    return lite_client, fake_tmux, add


def refresh(client, body=None, headers=H):
    return client.post("/api/usage/refresh", headers=headers, json=body if body is not None else {})


def untouched(store):
    return not (store["keys"] or store["texts"] or store["pasted"])


# ================================================================ the pure choice (usage_refresh.panes / choose)

def pane(name, full=0, at="2026-10-05T10:00:00+00:00"):
    return {"name": name, "row": {}, "full": full, "at": at}


def test_choose_prefers_an_unattached_pane_then_the_most_recently_active():
    ps = [pane("a", full=1, at="2026-10-05T12:00:00+00:00"), pane("b", at="2026-10-05T09:00:00+00:00"),
          pane("c", at="2026-10-05T11:00:00+00:00"), pane("d", full=2, at="2026-10-05T13:00:00+00:00")]
    assert usage_refresh.choose(ps, lambda p: True)["name"] == "c"                   # unattached first, newest of those
    assert usage_refresh.choose(ps[:1] + ps[3:], lambda p: True)["name"] == "d"      # only attached ones: the newest of them
    assert usage_refresh.choose(ps, lambda p: p["name"] in ("a", "d"))["name"] == "d"


def test_choose_unattached_only_never_picks_an_attached_pane():
    ps = [pane("a", full=1), pane("b", full=3)]
    assert usage_refresh.choose(ps, lambda p: True, unattached_only=True) is None
    assert usage_refresh.choose(ps + [pane("c")], lambda p: True, unattached_only=True)["name"] == "c"


def test_choose_applies_the_guard_and_answers_none_without_a_pane():
    assert usage_refresh.choose([], lambda p: True) is None
    assert usage_refresh.choose([pane("a"), pane("b")], lambda p: False) is None


def test_panes_keeps_only_running_claude_panes_of_the_board():
    live = {"p--r--ok": {"command": "2.1.5", "attached": 0}, "_ccboard-login": {"command": "claude", "attached": 0},
            "p--r--cx": {"command": "codex", "attached": 0}, "p--r--sh": {"command": "zsh", "attached": 0},
            "p--r--dead": {"command": "-zsh", "attached": 0}, "p--r--norow": {"command": "claude", "attached": 0},
            "p--r--noagent": {"command": "claude", "attached": 2}}
    rows = {"p--r--ok": {"agent": "claude"}, "_ccboard-login": {"agent": "claude"}, "p--r--cx": {"agent": "codex"},
            "p--r--sh": {"agent": "shell"}, "p--r--dead": {"agent": "claude"}, "p--r--noagent": {"agent": None, "state_at": "t"}}
    got = {p["name"]: p for p in usage_refresh.panes(live, rows, None)}
    assert sorted(got) == ["p--r--noagent", "p--r--ok"]                    # internal, Codex, shell row, a login shell in the pane and a row-less session are out
    assert got["p--r--noagent"]["full"] == 2 and got["p--r--noagent"]["at"] == "t"       # no list-clients: every attached client counts as a full one
    viewers = {"p--r--ok": {"full": 0, "grid": 3, "ro": 1}, "p--r--noagent": {"full": 1, "grid": 0, "ro": 0}}
    got = {p["name"]: p["full"] for p in usage_refresh.panes(live, rows, viewers)}
    assert got == {"p--r--ok": 0, "p--r--noagent": 1}                       # grid tiles and read-only views are not somebody at the terminal


# ================================================================ the route: which session, what reaches the pane

def test_it_types_usage_into_an_idle_claude_session_and_answers_202(board, later):
    client, store, add = board
    add(A)
    r = refresh(client)
    assert r.status_code == 202, r.text
    d = r.json()
    assert d["ok"] is True and d["session"] == A and isinstance(d["started_at"], str) and d["started_at"].endswith("+00:00")
    assert store["keys"] == [(A, ["C-u"])] and store["texts"] == [(A, "/usage", True)]       # the command route's own typing: clear the composer, the text, Enter
    assert store["pasted"] == []
    assert [d_ for d_, _ in later] == [usage_refresh.ESCAPE_AFTER_S] and usage_refresh.ESCAPE_AFTER_S == 4.0


def test_the_typing_is_the_command_routes_own_pending_cmd_and_event(board, later):
    client, store, add = board
    add(A)
    refresh(client)
    flags = main.db.open_row(A)["flags"]
    assert flags["pending_cmd"]["cmd"] == "usage" and flags["pending_cmd"]["arg"] is None
    ev = [e for e in main.db.recent_events() if e["tmux_name"] == A][0]
    assert ev["event"] == "BoardCommand" and ev["kind"] == "usage" and ev["message"] == "/usage"


def test_the_follow_up_sends_escape_once_and_reads_the_cache(board, later, monkeypatch):
    client, store, add = board
    add(A)
    polled = []
    monkeypatch.setattr(accounts, "poll_usage_cache", lambda db, now=None: polled.append(db) or True)
    refresh(client)
    assert usage_refresh.busy()
    store["keys"].clear()
    later[0][1]()                                                            # the four seconds are over
    assert store["keys"] == [(A, ["Escape"])]                                # one Escape: a second would open Claude's rewind menu
    assert polled == [main.db]                                               # the cache Claude Code just rewrote is read at once, not on the next 15 s tick
    assert not usage_refresh.busy()
    assert main.db.kv_get("usage_refresh")["value"]["ok"] is True


def test_the_follow_up_does_not_press_escape_into_a_session_that_is_busy_again(board, later, monkeypatch):
    client, store, add = board
    add(A)
    monkeypatch.setattr(accounts, "poll_usage_cache", lambda db, now=None: False)
    refresh(client)
    store["keys"].clear()
    main.db.set_state(A, "working", "UserPromptSubmit")                      # somebody sent a prompt meanwhile: Escape would interrupt it
    later[0][1]()
    assert store["keys"] == []
    rec = main.db.kv_get("usage_refresh")["value"]
    assert rec["ok"] is False and "busy" in rec["error"] and rec["session"] == A
    assert not usage_refresh.busy()


def test_the_follow_up_survives_a_session_that_ended(board, later, monkeypatch):
    client, store, add = board
    add(A)
    monkeypatch.setattr(accounts, "poll_usage_cache", lambda db, now=None: False)
    refresh(client)
    store["keys"].clear()
    store["sessions"].pop(A)
    later[0][1]()
    assert store["keys"] == [] and main.db.kv_get("usage_refresh")["value"]["ok"] is False
    assert not usage_refresh.busy()


def test_an_unattached_session_is_preferred_over_a_newer_attached_one(board, later):
    client, store, add = board
    add(A, attached=1, at="2026-10-05T12:00:00+00:00")
    add(B, at="2026-10-05T08:00:00+00:00")
    assert refresh(client).json()["session"] == B
    assert [k[0] for k in store["keys"]] == [B]


def test_the_most_recently_active_unattached_session_wins(board, later):
    client, store, add = board
    add(A, at="2026-10-05T08:00:00+00:00")
    add(B, at="2026-10-05T11:00:00+00:00")
    add(C, at="2026-10-05T09:00:00+00:00")
    assert refresh(client).json()["session"] == B


def test_a_tap_may_use_an_attached_session_when_nothing_else_qualifies(board, later):
    client, store, add = board
    add(A, attached=1)
    assert refresh(client).json()["session"] == A


def test_auto_never_types_into_a_session_somebody_is_attached_to(board, later):
    client, store, add = board
    add(A, attached=1)
    r = refresh(client, {"auto": True})
    assert r.status_code == 409 and r.json() == {"error": usage_refresh.NO_SESSION, "sessions": 1}
    assert untouched(store) and later == [] and not usage_refresh.busy()
    add(B)
    assert refresh(client, {"auto": True}).json()["session"] == B           # an unattached one is fine


def test_a_grid_tile_is_not_somebody_attached(board, later):
    client, store, add = board
    add(A, attached=1)
    store["clients"] = [{"session": A, "flags": {"attached", "ignore-size"}}]                  # a quad-view tile: not a window of their own
    assert refresh(client, {"auto": True}).json()["session"] == A


@pytest.mark.parametrize("state,flags", [("idle", {}), ("done", {}), ("errored", {}), ("waiting", {"wait_kind": "idle"})])
def test_every_state_the_command_route_types_into_qualifies(board, later, state, flags):
    client, store, add = board
    add(A, state=state, **flags)
    assert refresh(client).status_code == 202


@pytest.mark.parametrize("state,flags", [("working", {}), ("waiting", {"wait_kind": "permission"}), ("waiting", {"wait_kind": "elicitation"}),
                                         ("waiting", {}), ("idle", {"compacting": True}), ("ended", {}), (None, {})])
def test_a_session_the_command_route_refuses_never_qualifies(board, later, state, flags):
    client, store, add = board
    add(A, state=state, **flags)
    r = refresh(client)
    assert r.status_code == 409 and r.json() == {"error": usage_refresh.NO_SESSION, "sessions": 1}
    assert untouched(store)


def test_a_pending_permission_disqualifies_a_session(board, later):
    client, store, add = board
    add(A)
    main.db.perm_add(A, "Bash", "Bash: ls", {})                              # a request still waiting for its answer: typing would answer it
    r = refresh(client)
    assert r.status_code == 409 and r.json() == {"error": usage_refresh.NO_SESSION, "sessions": 1}
    assert untouched(store)
    add(B)
    assert refresh(client).json()["session"] == B                            # another session's request does not block it


def test_internal_codex_shell_and_row_less_sessions_are_never_asked(board, later):
    client, store, add = board
    add("_ccboard-login", state="idle")
    add("shop--api--cx1", agent="codex", launcher="codex")
    add("shop--api--sh1", agent="shell", launcher="shell", command="zsh")
    store["sessions"]["shop--api--ext"] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%9", "command": "claude", "path": "/x",
                                           "pid": 9, "env": {}}                  # a tmux session the board has no row for
    r = refresh(client)
    assert r.status_code == 409 and r.json() == {"error": usage_refresh.NO_SESSION, "sessions": 0}
    assert untouched(store)
    add(A)
    assert refresh(client).json()["session"] == A and [k[0] for k in store["keys"]] == [A]


def test_a_claude_row_whose_pane_is_back_at_a_login_shell_is_not_asked(board, later):
    client, store, add = board
    add(A, command="-zsh")                                                    # claude exited, the row never said so: typing /usage would run it in zsh
    r = refresh(client)
    assert r.status_code == 409 and r.json() == {"error": usage_refresh.NO_SESSION, "sessions": 0}
    assert untouched(store)


# ================================================================ the three 409s

def test_409_when_the_box_has_no_claude_session_at_all(board, later):
    client, store, add = board
    r = refresh(client)
    assert r.status_code == 409
    assert r.json() == {"error": "no Claude session is at its prompt; start one to refresh", "sessions": 0}
    assert untouched(store) and later == [] and main.db.kv_get("usage_refresh") is None


def test_409_when_claude_sessions_exist_but_none_is_at_its_prompt(board, later):
    client, store, add = board
    add(A, state="working")
    add(B, state="waiting", wait_kind="permission")
    r = refresh(client)
    assert r.status_code == 409
    assert r.json() == {"error": "no Claude session is at its prompt; start one to refresh", "sessions": 2}
    assert untouched(store) and not usage_refresh.busy()


def test_409_while_a_refresh_is_in_flight_and_the_lock_frees_when_it_ends(board, later):
    client, store, add = board
    add(A)
    add(B)
    assert refresh(client).status_code == 202
    typed = list(store["texts"])
    r = refresh(client)
    assert r.status_code == 409 and r.json() == {"error": "a refresh is already running"}
    assert store["texts"] == typed and len(later) == 1                         # the second tap typed nothing and scheduled nothing
    later[0][1]()                                                              # the first one finishes
    assert refresh(client).status_code == 202 and len(later) == 2


def test_the_in_flight_answer_comes_before_the_session_check(board, later):
    client, store, add = board
    assert usage_refresh.claim()
    r = refresh(client)
    assert r.status_code == 409 and r.json() == {"error": "a refresh is already running"}                # not 'no Claude session'


def test_the_lock_frees_itself_after_20_seconds(board, later, monkeypatch):
    client, store, add = board
    add(A)
    now = [1000.0]
    monkeypatch.setattr(usage_refresh, "_clock", lambda: now[0])
    assert refresh(client).status_code == 202
    now[0] += usage_refresh.LOCK_S - 0.1
    assert refresh(client).status_code == 409
    now[0] += 0.2                                                             # the follow-up never reported back: the lock still lapses
    assert refresh(client).status_code == 202 and usage_refresh.LOCK_S == 20.0


def test_a_failed_typing_frees_the_lock_and_says_so_in_the_kv(board, later, monkeypatch):
    client, store, add = board
    add(A)

    def boom(*a, **k):
        raise tmux.TmuxError("send-keys failed")
    monkeypatch.setattr(tmux, "send_text", boom)
    r = refresh(client)
    assert r.status_code >= 400 and r.status_code != 409
    assert not usage_refresh.busy() and later == []
    rec = main.db.kv_get("usage_refresh")["value"]
    assert rec["ok"] is False and rec["session"] == A and "typing failed" in rec["error"]
    assert main.db.open_row(A)["flags"].get("pending_cmd") is None


# ================================================================ the request, the kv, the state

def test_auto_must_be_true_or_false(board, later):
    client, store, add = board
    add(A)
    for bad in ("yes", 1, [True], {"a": 1}):
        r = refresh(client, {"auto": bad})
        assert r.status_code == 400 and "auto" in r.json()["error"], bad
    assert untouched(store)
    assert refresh(client, {"auto": False}).status_code == 202


def test_no_body_is_fine(board, later):
    client, store, add = board
    add(A)
    assert client.post("/api/usage/refresh", headers=H).status_code == 202


def test_it_needs_identity_and_the_csrf_header(board, later):
    client, store, add = board
    add(A)
    assert client.post("/api/usage/refresh", headers={"X-CCBoard": "1"}).status_code == 403
    assert client.post("/api/usage/refresh", headers={"Tailscale-User-Login": "alice@example.com"}).status_code == 403
    assert untouched(store) and later == []


def test_the_kv_records_when_which_session_and_whether_it_worked(board, later):
    client, store, add = board
    add(A)
    d = refresh(client).json()
    rec = main.db.kv_get("usage_refresh")["value"]
    assert rec == {"at": d["started_at"], "session": A, "ok": True}


def test_the_state_carries_the_refresh_view(board, later):
    client, store, add = board
    assert client.get("/api/state", headers=H).json()["usage_refresh"] == {"running": False, "last": None}
    add(A)
    d = refresh(client).json()
    st = client.get("/api/state", headers=H).json()["usage_refresh"]
    assert st == {"running": True, "last": {"at": d["started_at"], "session": A, "ok": True}}
    later[0][1]()
    assert client.get("/api/state", headers=H).json()["usage_refresh"]["running"] is False


# ================================================================ end to end over a fixture state file

def test_the_cache_claude_code_rewrites_reaches_the_board_through_the_follow_up(board, later, tmp_path, monkeypatch):
    """/usage typed, Claude Code rewrites cachedUsageUtilization (here: a fixture file in a tmp config dir), the follow-up reads it at once:
    state.usage carries the new reading with source 'cache' without waiting for the Sampler tick."""
    client, store, add = board
    add(A)
    cfg = settings.claude_config_dir
    write_state(cfg, cache_block(fetched=T0 - 5, five=(41.0, T0 + 9000), seven=(55.0, T0 + 300000)))
    assert refresh(client).status_code == 202
    assert not (client.get("/api/state", headers=H).json()["usage"] or {}).get("value")                # nothing fed before the follow-up
    later[0][1]()
    u = client.get("/api/state", headers=H).json()["usage"]
    assert u["value"]["five_hour"]["used_percentage"] == 41.0 and u["value"]["source"] == "cache"
    assert u["value"]["seven_day"]["used_percentage"] == 55.0
