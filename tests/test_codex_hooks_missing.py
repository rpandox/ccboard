"""#96: the 'no hooks (untrusted?)' rule. agents/codex.hooks_missing (one function every surface reads through the state row's
`hooks_missing`), flags.turn_seen_at from a prompt the board sent (POST /prompt) and from the rollout's first user message (the Tailer).
lite_client + fake_tmux and temp CODEX_HOME only; no real codex.
"""
import time

import pytest

from app import hooks, main
from app.agents import codex
from app.agents import codex_rollout as cr
from app.config import settings
from app.db import iso
from tests.test_usage_codex import _fresh_rollout_state, add_codex_row, db, home, jl, make_rollout, work  # noqa: F401

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
NAME = "shop--api--s1"
T = 1_800_000_000.0


def row(agent="codex", **flags):
    return {"agent": agent, "state": "idle", "flags": flags}


def test_hooks_missing_rule(monkeypatch):
    monkeypatch.setattr(settings, "codex_hook_trust", "review")
    assert codex.hooks_missing(row(), T) is None, "a fresh launch with no prompt: no chip"
    assert codex.hooks_missing(row(turn_seen_at=iso(T - 29)), T) is None, "inside the 30 s grace"
    assert codex.hooks_missing(row(turn_seen_at=iso(T - 31)), T) == "untrusted", "31 s after the first prompt with no hook"
    assert codex.hooks_missing(row(turn_seen_at=iso(T - 31), hook_seen=True), T) is None, "a hook arrived"
    assert codex.hooks_missing(row("claude", turn_seen_at=iso(T - 300)), T) is None, "never a Claude row"
    assert codex.hooks_missing(row("shell", turn_seen_at=iso(T - 300)), T) is None
    assert codex.hooks_missing({**row(turn_seen_at=iso(T - 300)), "state": "ended"}, T) is None, "an ended session shows none"
    assert codex.hooks_missing(row(turn_seen_at="not a time"), T) is None and codex.hooks_missing(None, T) is None
    assert codex.hooks_missing(row(turn_seen_at="2027-01-15T08:00:00Z"), T + 10 ** 6) == "untrusted", "a Z suffix reads as UTC"
    monkeypatch.setattr(settings, "codex_hook_trust", "bypass")
    assert codex.hooks_missing(row(turn_seen_at=iso(T - 31)), T) == "bypass", "bypass mode: different fix text"


def test_the_tailer_sets_turn_seen_at_from_the_first_user_message(db, home, work):
    now = time.time()
    p = make_rollout(home, cwd=work, created=now - 5, lines=[jl(now - 2, "event_msg", {"type": "user_message", "message": "hi"})])
    add_codex_row(db, work, flags={"transcript_path": str(p)})
    cr.Tailer().tail_rows(db, now)
    flags = db.open_row(NAME)["flags"]
    assert flags["turn_seen_at"] == iso(now - 2) and "hook_seen" not in flags
    assert codex.hooks_missing(db.open_row(NAME), now + 40) == "untrusted"


def test_a_resumed_threads_old_turns_do_not_arm_it(db, home, work):
    now = time.time()
    p = make_rollout(home, cwd=work, created=now - 86400, lines=[jl(now - 86000, "event_msg", {"type": "user_message", "message": "old"})])
    add_codex_row(db, work, flags={"transcript_path": str(p)})
    cr.Tailer().tail_rows(db, now)
    assert "turn_seen_at" not in db.open_row(NAME)["flags"]


@pytest.fixture
def codex_row(lite_client, fake_tmux):
    main.db.add_session(tmux_name=NAME, project="shop", repo="api", name="s1", launcher="codex", agent="codex")
    fake_tmux["sessions"][NAME] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "codex", "path": "/x", "pid": 1, "env": {}}
    main.db.set_state(NAME, "idle", "Launch")
    return NAME


def test_a_prompt_the_board_sends_arms_it_once_and_the_state_row_carries_it(lite_client, codex_row, monkeypatch):
    monkeypatch.setattr(settings, "codex_hook_trust", "review")
    r = lite_client.post(f"/api/sessions/{NAME}/prompt", headers=H, json={"text": "hello"})
    assert r.status_code == 200, r.text
    first = main.db.open_row(NAME)["flags"]["turn_seen_at"]
    lite_client.post(f"/api/sessions/{NAME}/prompt", headers=H, json={"text": "again"})
    assert main.db.open_row(NAME)["flags"]["turn_seen_at"] == first, "written once"
    sessions, _ = main._merged_sessions()
    assert sessions[NAME]["hooks_missing"] is None, "inside the grace"
    main.db.update_flags(NAME, {"turn_seen_at": iso(time.time() - 60)})
    main._invalidate_scan()
    sessions, _ = main._merged_sessions()
    assert sessions[NAME]["hooks_missing"] == "untrusted" and sessions[NAME]["flags"]["turn_seen_at"]
    assert sessions[NAME]["state"] == "idle", "the chip never changes the state"
    main.db.update_flags(NAME, {"hook_seen": True})
    sessions, _ = main._merged_sessions()
    assert sessions[NAME]["hooks_missing"] is None, "cleared on the next poll once a hook arrived"


def test_a_claude_prompt_writes_no_turn_seen_at(lite_client, fake_tmux):
    main.db.add_session(tmux_name=NAME, project="shop", repo="api", name="s1", launcher="claude")
    fake_tmux["sessions"][NAME] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "claude", "path": "/x", "pid": 1, "env": {}}
    main.db.set_state(NAME, "idle", "SessionStart")
    lite_client.post(f"/api/sessions/{NAME}/prompt", headers=H, json={"text": "hello"})
    assert "turn_seen_at" not in main.db.open_row(NAME)["flags"]
    sessions, _ = main._merged_sessions()
    assert sessions[NAME]["hooks_missing"] is None
