import json
import subprocess
from pathlib import Path

import pytest

from app import hooks, main, notify, samples


def git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)


H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


def _hook(lite_client, payload, session=None, event=None, token=None, extra=None):
    hdr = {"X-CCBoard-Token": token if token is not None else hooks.ensure_token()}
    if session:
        hdr["X-CCBoard-Session"] = session
    if event:
        hdr["X-CCBoard-Event"] = event
    hdr.update(extra or {})
    return lite_client.post("/api/hook", headers=hdr, content=json.dumps(payload))


def test_token_and_resolution(lite_client, projects_dir, fake_tmux, monkeypatch):
    git_init(projects_dir / "shop" / "api")
    r = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"})
    name = r.json()["tmux"]
    assert lite_client.post("/api/hook", content="{}").status_code == 403                       # no token
    assert _hook(lite_client, {}, token="nope").status_code == 403
    assert _hook(lite_client, {"hook_event_name": "Stop"}).json()["ignored"] == "unresolved"     # nothing to map
    assert _hook(lite_client, {"hook_event_name": "Stop"}, session="x--y--z").json()["ignored"] == "unknown session"
    # cwd fallback maps a single live session in that repo
    out = _hook(lite_client, {"hook_event_name": "UserPromptSubmit", "prompt": "fix it", "cwd": str(projects_dir / "shop" / "api")}).json()
    assert out["session"] == name and out["how"] == "cwd" and out["state"] == "working"
    # env header wins
    out = _hook(lite_client, {"hook_event_name": "SessionStart", "session_id": "11111111-1111-4111-8111-111111111111"}, session=name).json()
    assert out["how"] == "env" and out["state"] == "idle"


def test_state_machine(lite_client, projects_dir, fake_tmux):
    git_init(projects_dir / "shop" / "api")
    name = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]

    def sess():
        st = lite_client.get("/api/state", headers=H).json()
        return st["projects"][0]["repos"][0]["sessions"][0], st

    _hook(lite_client, {"hook_event_name": "SessionStart", "source": "startup", "session_id": "22222222-2222-4222-8222-222222222222"}, session=name)
    s, _ = sess()
    assert s["state"] == "idle" and s["claude_session_id"] == "22222222-2222-4222-8222-222222222222" and not s["needs_attention"]
    _hook(lite_client, {"hook_event_name": "UserPromptSubmit", "prompt": "write tests"}, session=name)
    s, _ = sess()
    assert s["state"] == "working" and s["last_prompt"] == "write tests"
    _hook(lite_client, {"hook_event_name": "Notification", "notification_type": "permission_prompt", "message": "Claude needs permission to run npm test"}, session=name)
    s, _ = sess()
    assert s["state"] == "waiting" and s["needs_attention"] and "permission" in s["last_message"]
    _hook(lite_client, {"hook_event_name": "Notification", "notification_type": "auth_success", "message": "ok"}, session=name)
    s, _ = sess()
    assert s["state"] == "waiting"                                    # informational notification keeps the state
    fake_tmux["screen"] = "╭──────╮\n│ All 12 tests pass. │\n╰──────╯\n? for shortcuts\n"
    _hook(lite_client, {"hook_event_name": "Stop"}, session=name)
    s, _ = sess()
    assert s["state"] == "done" and s["needs_attention"] and "12 tests pass" in s["last_message"]
    assert lite_client.post(f"/api/sessions/{name}/ack", headers=H).status_code == 200
    s, _ = sess()
    assert s["state"] == "done" and not s["needs_attention"]
    _hook(lite_client, {"hook_event_name": "StopFailure", "error_type": "rate_limit", "error": "You have hit your limit"}, session=name)
    s, st = sess()
    assert s["state"] == "errored" and s["needs_attention"] and st["rate_limited"]["value"]["session"] == name
    _hook(lite_client, {"hook_event_name": "SessionEnd", "reason": "logout"}, session=name)
    s, _ = sess()
    assert s["state"] == "ended"


def test_statusline_stats_and_usage(lite_client, projects_dir, fake_tmux):
    git_init(projects_dir / "shop" / "api")
    name = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]
    payload = {"session_id": "33333333-3333-4333-8333-333333333333", "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
               "context_window": {"used_percentage": 41.2, "context_window_size": 200000},
               "cost": {"total_cost_usd": 1.25, "total_lines_added": 10, "total_lines_removed": 2},
               "rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 1738425600}, "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600}}}
    assert _hook(lite_client, payload, session=name, event="statusline").json()["stats"] is True
    st = lite_client.get("/api/state", headers=H).json()
    s = st["projects"][0]["repos"][0]["sessions"][0]
    assert s["stats"]["model"] == "Opus" and s["stats"]["context_pct"] == 41.2 and s["claude_session_id"].startswith("3333")
    assert st["usage"]["value"]["five_hour"]["used_percentage"] == 23.5


def test_token_file_is_private(projects_dir):
    import os
    hooks._token = None
    p = hooks.token_path()
    if p.exists():
        p.unlink()
    t = hooks.ensure_token()
    assert len(t) == 64 and oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert hooks.check_token(t) and not hooks.check_token(t[:-1] + "x") and not hooks.check_token(None)


def test_last_screen_line_skips_chrome_and_prompts(monkeypatch):
    from app import tmux as t
    screens = {
        "shell": "➜ api git:(main) echo hi\nAdded login page and 4 passing tests.\n➜ api git:(main) \n",
        "tui": "╭────────────────╮\n│ I updated login.html and added tests. All green. │\n╰────────────────╯\n╭─────╮\n│ >  │\n╰─────╯\n  ? for shortcuts    ⏵⏵ accept edits on (shift+tab to cycle)\n",
        "empty": "\n\n",
    }
    for key, expect in (("shell", "Added login page and 4 passing tests."), ("tui", "I updated login.html and added tests. All green."), ("empty", None)):
        monkeypatch.setattr(t, "capture", lambda name, lines=200, join=True, escapes=False, _k=key: screens[_k])
        assert hooks._last_screen_line("x--y--z") == expect


def test_malformed_statusline_does_not_500(lite_client, projects_dir, fake_tmux):
    git_init(projects_dir / "shop" / "api")
    name = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]
    r = _hook(lite_client, {"model": "opus", "context_window": "x", "cost": None, "rate_limits": "no"}, session=name, event="statusline")
    assert r.status_code == 200 and r.json().get("stats") is True
    r = _hook(lite_client, {"hook_event_name": "Notification", "message": {"nested": 1}}, session=name)
    assert r.status_code == 200


# ---------- v0.5.4 commit B: samples written from the hook path ----------

FIXTURES = Path(__file__).parent / "fixtures" / "agents" / "claude"
LIVE_LIMIT = "You've hit your session limit · resets 10:05pm (Asia/Kathmandu)"
EPOCH0 = "2000-01-01T00:00:00+00:00"


def fixture(name, **extra):
    return {**json.loads((FIXTURES / name).read_text()), **extra}


def rows(series, key=None):
    """[(at, key, value, meta dict)] for one series, oldest first."""
    keys = [key] if key is not None else None
    out = []
    for at, k, v, meta in main.db.samples_query(series, keys, EPOCH0, None):
        out.append((at, k, v, json.loads(meta) if isinstance(meta, str) and meta else (meta or {})))
    return out


def values(series, key=None):
    return [r[2] for r in rows(series, key)]


def limited():
    """The kv 'rate_limited' record /api/state serves as `rate_limited` ({value, at}), read straight from the DB."""
    return main.db.kv_get("rate_limited")


@pytest.fixture
def session(lite_client, projects_dir, fake_tmux, monkeypatch):
    git_init(projects_dir / "shop" / "api")
    name = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]
    calls = []
    monkeypatch.setattr(notify, "notify_rate_limit", lambda n, m: calls.append((n, m)) or True)
    return type("S", (), {"client": lite_client, "name": name, "limit_pushes": calls})


STATUSLINE = {"session_id": "44444444-4444-4444-8444-444444444444", "model": {"id": "claude-opus-5-5", "display_name": "Opus"},
              "context_window": {"used_percentage": 41.2, "context_window_size": 200000},
              "cost": {"total_cost_usd": 1.25, "total_lines_added": 10, "total_lines_removed": 2},
              "rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
                              "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600}}}


def test_statusline_writes_the_series_and_throttles_repeats(session):
    c, name = session.client, session.name
    assert _hook(c, STATUSLINE, session=name, event="statusline").json()["stats"] is True
    assert values("ctx", name) == [41.2]
    assert values("ctx_tok", name) == [pytest.approx(41.2 / 100 * 200000)]
    assert values("scost", name) == [1.25]
    assert values("rl_5h", "claude") == [23.5] and rows("rl_5h", "claude")[0][3]["resets_at"] == 1738425600
    assert values("rl_7d", "claude") == [41.2] and rows("rl_7d", "claude")[0][3]["resets_at"] == 1738857600
    # the same readings again: nothing moved and no throttle window has passed, so nothing is written
    _hook(c, STATUSLINE, session=name, event="statusline")
    assert (len(rows("ctx", name)), len(rows("scost", name)), len(rows("rl_5h", "claude")), len(rows("rl_7d", "claude"))) == (1, 1, 1, 1)
    # context moved by 1 point (>= 0.5) and a few cents were spent; the account limits moved by less than a point
    moved = {**STATUSLINE, "context_window": {"used_percentage": 42.2, "context_window_size": 200000},
             "cost": {"total_cost_usd": 1.26},
             "rate_limits": {"five_hour": {"used_percentage": 23.9, "resets_at": 1738425600},
                             "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600}}}
    _hook(c, moved, session=name, event="statusline")
    assert values("ctx", name) == [41.2, 42.2]
    assert values("ctx_tok", name) == [pytest.approx(41.2 / 100 * 200000), pytest.approx(42.2 / 100 * 200000)]
    assert values("scost", name) == [1.25, 1.26]
    assert values("rl_5h", "claude") == [23.5] and values("rl_7d", "claude") == [41.2]
    assert main.db.samples_query("ev", None, EPOCH0, None) == []          # the statusline is not a hook event: it never bumps ev


def test_statusline_without_limits_or_context_writes_only_what_it_has(session):
    c, name = session.client, session.name
    _hook(c, {"cost": {"total_cost_usd": 0.5}}, session=name, event="statusline")
    assert values("scost", name) == [0.5]
    assert rows("ctx", name) == [] and rows("ctx_tok", name) == [] and rows("rl_5h") == [] and rows("rl_7d") == []


def test_every_stored_hook_event_bumps_the_hourly_counter(session):
    c, name = session.client, session.name
    for payload in ({"hook_event_name": "SessionStart", "source": "startup"}, {"hook_event_name": "UserPromptSubmit", "prompt": "go"},
                    {"hook_event_name": "Notification", "notification_type": "auth_success", "message": "ok"}):
        _hook(c, payload, session=name)
    _hook(c, {"hook_event_name": "PostToolBatch"}, session=name)           # never stored as an event, so never counted
    _hook(c, STATUSLINE, session=name, event="statusline")                  # idem
    evs = rows("ev", "shop")
    assert sum(r[2] for r in evs) == 3 and 1 <= len(evs) <= 2               # one row per (project, hour), bumped in place
    assert rows("ev", "other") == []


def test_samples_failures_never_break_the_hook_path(session, monkeypatch):
    c, name = session.client, session.name

    def boom(*a, **k):
        raise RuntimeError("disk full")

    for fn in ("record_statusline", "bump_event", "record_limit"):
        monkeypatch.setattr(samples, fn, boom)
    assert _hook(c, STATUSLINE, session=name, event="statusline").json()["stats"] is True
    out = _hook(c, {"hook_event_name": "UserPromptSubmit", "prompt": "still stored"}, session=name).json()
    assert out["state"] == "working" and "ignored" not in out
    out = _hook(c, fixture("stopfailure_rate_limit.json"), session=name).json()
    assert out["state"] == "errored" and "ignored" not in out
    assert limited()["value"]["kind"] == "5h" and main.db.open_row(name)["stats"]["cost_usd"] == 1.25


def test_live_rate_limit_failure_sets_kv_notifies_once_and_writes_one_lim_row(session):
    c, name = session.client, session.name
    live = fixture("stopfailure_rate_limit.json")
    assert live["error"] == "rate_limit" and live["last_assistant_message"] == LIVE_LIMIT      # the payload as the box sends it
    # the session itself and two of its subagents fail inside the same episode
    for extra in ({}, {"agent_id": "sub-1"}, {"agent_id": "sub-2", "agent_type": "workflow-subagent"}):
        out = _hook(c, {**live, **extra}, session=name).json()
        assert out["state"] == "errored" and out["kind"] == "rate_limit"
    row = main.db.open_row(name)
    assert row["state"] == "errored" and row["acked_at"] is None and row["last_message"] == LIVE_LIMIT
    kv = limited()["value"]
    assert kv["session"] == name and kv["kind"] == "5h" and kv["message"] == LIVE_LIMIT
    assert isinstance(kv["resets_at"], int) and kv["resets_at"] > 1_700_000_000
    assert session.limit_pushes == [(name, LIVE_LIMIT)]                                          # told once, not three times
    lim = rows("lim")
    assert len(lim) == 1
    at, key, value, meta = lim[0]
    assert key == "5h" and meta["session"] == name and meta["resets_at"] == kv["resets_at"] and LIVE_LIMIT[:40] in meta["message"]
    assert len(meta["message"]) <= 120
    # events table: every failure is still an event of its own
    kinds = [e["event"] for e in main.db.recent_events(20)]
    assert kinds.count("StopFailure") == 3


def test_a_different_limit_is_a_new_episode(session):
    c, name = session.client, session.name
    _hook(c, fixture("stopfailure_rate_limit.json"), session=name)
    _hook(c, fixture("stopfailure_weekly.json"), session=name)                                   # 7d, another reset time
    _hook(c, fixture("stopfailure_weekly.json"), session=name)
    assert sorted(r[1] for r in rows("lim")) == ["5h", "7d"]
    assert len(session.limit_pushes) == 2
    assert limited()["value"]["kind"] == "7d"
    # the person clears the banner: the next failure of the same episode is announced again
    c.post("/api/usage/rate-limit/clear", headers=H)
    _hook(c, fixture("stopfailure_weekly.json"), session=name)
    assert len(session.limit_pushes) == 3 and len(rows("lim")) == 2                              # the lim row is not repeated


def test_server_error_failure_is_not_a_rate_limit(session):
    c, name = session.client, session.name
    out = _hook(c, fixture("stopfailure_server_error.json"), session=name).json()
    assert out["state"] == "errored" and out["kind"] == "server_error"
    row = main.db.open_row(name)
    assert row["state"] == "errored" and row["last_message"] == "API Error: 500 Internal server error"
    assert limited() is None and session.limit_pushes == [] and rows("lim") == []


def test_legacy_error_type_payload_still_detects_the_limit(session):
    c, name = session.client, session.name
    for _ in range(2):
        _hook(c, {"hook_event_name": "StopFailure", "error_type": "rate_limit_error", "error": "You have hit your limit"}, session=name)
    kv = limited()["value"]
    assert kv["session"] == name and kv["message"] == "You have hit your limit" and kv["kind"] == "other"
    assert kv["resets_at"] is None
    assert session.limit_pushes == [(name, "You have hit your limit")]
    assert [r[1] for r in rows("lim")] == ["other"] and rows("lim")[0][3]["session"] == name
    # `matcher` carried the type in the oldest shape
    out = _hook(c, {"hook_event_name": "StopFailure", "matcher": "rate_limit", "message": "slow down"}, session=name).json()
    assert out["state"] == "errored" and out["kind"] == "rate_limit"
    assert limited()["value"]["message"] == "slow down"


def test_limit_message_without_an_error_type_is_still_a_limit(session):
    """A payload whose `error` is not a rate-limit word but whose text says the limit was hit (the adapter's LIMIT_MSG_RE)."""
    c, name = session.client, session.name
    _hook(c, {"hook_event_name": "StopFailure", "error": "unknown", "last_assistant_message": "You've hit your weekly limit · resets Oct 9, 3pm (Asia/Kathmandu)"}, session=name)
    assert [r[1] for r in rows("lim")] == ["7d"] and len(session.limit_pushes) == 1
