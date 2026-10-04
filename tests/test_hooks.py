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


# ---------- v0.5.7 hooks v2: every event through the adapter, honest prompts, wait kinds, foreign guard, statusline extras ----------

SID = "55555555-5555-4555-8555-555555555555"
SID2 = "66666666-6666-4666-8666-666666666666"
SID3 = "77777777-7777-4777-8777-777777777777"

# one fixture per payload shape (field names as Claude Code 2.1.28x sends them; research2.md sections 2 and 3)
SHAPES = {
    "session_start": {"hook_event_name": "SessionStart", "source": "startup", "session_id": SID, "model": "claude-opus-5-5"},
    "session_start_resume": {"hook_event_name": "SessionStart", "source": "resume", "session_id": SID2, "seconds_since_last_response": 5400,
                             "context_tokens": 91234, "prompt_cache_likely_expired": True, "estimated_cache_write_usd": 0.4},
    "prompt": {"hook_event_name": "UserPromptSubmit", "session_id": SID, "prompt": "write the tests", "permission_mode": "default"},
    "notify_permission": {"hook_event_name": "Notification", "session_id": SID, "notification_type": "permission_prompt",
                          "message": "Claude needs your permission to use Bash"},
    "notify_idle": {"hook_event_name": "Notification", "session_id": SID, "notification_type": "idle_prompt", "message": "Claude is waiting"},
    "notify_elicitation": {"hook_event_name": "Notification", "session_id": SID, "notification_type": "elicitation_dialog",
                           "message": "An MCP server asks for input"},
    "notify_elicitation_url": {"hook_event_name": "Notification", "session_id": SID, "notification_type": "elicitation_url_dialog",
                               "message": "Open the browser"},
    "notify_elicitation_complete": {"hook_event_name": "Notification", "session_id": SID, "notification_type": "elicitation_complete"},
    "stop": {"hook_event_name": "Stop", "session_id": SID, "stop_hook_active": False, "last_assistant_message": "Done. Tests pass."},
    "subagent_start": {"hook_event_name": "SubagentStart", "session_id": SID, "agent_id": "a1", "agent_type": "Explore"},
    "subagent_stop": {"hook_event_name": "SubagentStop", "session_id": SID, "agent_id": "a1", "agent_transcript_path": "/x/agent.jsonl"},
    "pre_compact": {"hook_event_name": "PreCompact", "session_id": SID, "trigger": "auto"},
    "post_compact": {"hook_event_name": "PostCompact", "session_id": SID, "trigger": "auto"},
    "post_tool_batch": {"hook_event_name": "PostToolBatch", "session_id": SID},
    "session_end": {"hook_event_name": "SessionEnd", "session_id": SID, "reason": "prompt_input_exit"},
}


def hk(session, shape, **extra):
    """Send one SHAPES entry (with overrides) and return the JSON answer."""
    payload = {**(SHAPES[shape] if isinstance(shape, str) else shape), **extra}
    r = _hook(session.client, payload, session=session.name)
    assert r.status_code == 200, r.text
    return r.json()


def row_of(name):
    return main.db.open_row(name)


def event_names(name):
    with main.db.lock:
        return [r["event"] for r in main.db.conn.execute("SELECT event FROM events WHERE tmux_name=? ORDER BY id", (name,)).fetchall()]


def second_row(client):
    r = client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"})
    assert r.status_code == 201, r.text
    return r.json()["tmux"]


# ----- hooks.is_system_turn (pure) -----

@pytest.mark.parametrize("prompt,kind", [
    ("<task-notification>\n<task-id>abc</task-id>", "task-notification"),
    ("   \n\t<task-notification> indented", "task-notification"),
    ("<system-reminder>be brief</system-reminder>", "system"),
    ("[SYSTEM NOTIFICATION - the build finished]", "system"),
    ("<pasted_content id=1>", "paste"),
    ("<command-name>/model</command-name>", "command"),
    ("<command-message>model</command-message><command-name>/model</command-name>", "command"),
    ("<local-command-stdout>Set model</local-command-stdout>", "local"),
    ("<local-command-caveat>Caveat</local-command-caveat>", "local"),
    ("<bash-input>ls</bash-input>", "local"),
    ("", "empty"), ("   ", "empty"), ("\n\t  ", "empty"),
    ("write tests", None), ("fix <system-reminder> handling", None), ("a <task-notification> in the middle", None),
    ("/model opus", None), ("[SYSTEM] not the marker", None), ("<task-notifications>", None),
    (None, None), (5, None), ([], None), ({"a": 1}, None), (b"<task-notification>", None),
])
def test_is_system_turn(prompt, kind):
    assert hooks.is_system_turn(prompt) == kind


# ----- last_prompt hygiene -----

SYSTEM_PROMPTS = [("<task-notification>\n<task-id>b7x</task-id><status>completed</status>", "task-notification"),
                  ("<system-reminder>The task tools haven't been used recently.</system-reminder>", "system"),
                  ("[SYSTEM NOTIFICATION] 3 background tasks finished", "system"),
                  ("<pasted_content id=2 lines=400/>", "paste"),
                  ("<command-name>/compact</command-name>", "command"),
                  ("<local-command-stdout>Compacted</local-command-stdout>", "local"),
                  ("<bash-input>git status</bash-input>", "local")]


@pytest.mark.parametrize("prompt,kind", SYSTEM_PROMPTS)
def test_a_system_turn_flips_to_working_but_never_becomes_the_last_prompt(session, prompt, kind):
    hk(session, "prompt")
    hk(session, "stop")
    assert row_of(session.name)["state"] == "done"
    out = hk(session, "prompt", prompt=prompt)
    assert out["state"] == "working"
    r = row_of(session.name)
    assert r["state"] == "working" and r["last_prompt"] == "write the tests"
    turn = r["flags"]["last_system_turn"]
    assert turn["kind"] == kind and turn["head"] == prompt[:160] and turn["at"]
    # the next real prompt replaces last_prompt as before and leaves the system record where it was
    hk(session, "prompt", prompt="now the docs")
    r = row_of(session.name)
    assert r["last_prompt"] == "now the docs" and r["flags"]["last_system_turn"]["kind"] == kind


def test_system_turn_head_is_the_first_160_characters_of_the_trimmed_text(session):
    hk(session, "prompt", prompt="   <task-notification>" + "x" * 500)
    turn = row_of(session.name)["flags"]["last_system_turn"]
    assert len(turn["head"]) == 160 and turn["head"].startswith("<task-notification>x")


def test_a_whitespace_only_prompt_is_ignored_the_same_way(session):
    hk(session, "prompt")
    hk(session, "stop")
    hk(session, "prompt", prompt="  \n \t ")
    r = row_of(session.name)
    assert r["state"] == "working" and r["last_prompt"] == "write the tests"
    assert r["flags"]["last_system_turn"]["kind"] == "empty" and r["flags"]["last_system_turn"]["head"] == ""


@pytest.mark.parametrize("bad", [None, 5, ["a"], {"x": 1}, True])
def test_a_prompt_that_is_not_text_keeps_the_last_prompt_and_stays_quiet(session, bad):
    hk(session, "prompt")
    hk(session, "stop")
    hk(session, "prompt", prompt=bad)
    r = row_of(session.name)
    assert r["state"] == "working" and r["last_prompt"] == "write the tests" and "last_system_turn" not in r["flags"]


def test_a_normal_prompt_writes_no_flags_beyond_hook_seen(session):
    hk(session, "prompt")
    assert row_of(session.name)["flags"] == {"hook_seen": True}


# ----- subagents, compaction -----

def test_subagent_counter_counts_up_and_down_with_a_floor_of_zero(session):
    hk(session, "subagent_stop")                          # a stop before any start: never negative
    assert row_of(session.name)["flags"].get("subagents", 0) == 0
    hk(session, "subagent_start")
    hk(session, "subagent_start")
    assert row_of(session.name)["flags"]["subagents"] == 2
    hk(session, "subagent_stop")
    assert row_of(session.name)["flags"]["subagents"] == 1
    hk(session, "subagent_stop")
    hk(session, "subagent_stop")
    hk(session, "subagent_stop")
    assert row_of(session.name)["flags"]["subagents"] == 0
    assert event_names(session.name).count("SubagentStart") == 2 and event_names(session.name).count("SubagentStop") == 5


def test_a_new_conversation_or_the_end_resets_a_stale_subagent_count(session):
    for _ in range(3):
        hk(session, "subagent_start")
    hk(session, "session_start", source="compact")        # the same conversation goes on: its agents may still run
    assert row_of(session.name)["flags"]["subagents"] == 3
    hk(session, "session_start", source="clear")
    assert "subagents" not in row_of(session.name)["flags"]
    hk(session, "subagent_start")
    hk(session, "session_end")
    assert "subagents" not in row_of(session.name)["flags"]


def test_compaction_flag_follows_pre_and_post_compact_and_dies_with_the_turn(session):
    hk(session, "pre_compact")
    assert row_of(session.name)["flags"]["compacting"] is True
    hk(session, "post_compact")
    assert row_of(session.name)["flags"]["compacting"] is False
    hk(session, "pre_compact", trigger="manual")
    hk(session, "stop")                                    # PostCompact never came: the turn's end clears it
    assert "compacting" not in row_of(session.name)["flags"]
    kinds = [e["kind"] for e in main.db.recent_events(10) if e["event"] in ("PreCompact", "PostCompact")]
    assert set(kinds) == {"auto", "manual"}


# ----- wait kinds -----

@pytest.mark.parametrize("shape,kind", [("notify_permission", "permission"), ("notify_idle", "idle"),
                                         ("notify_elicitation", "elicitation"), ("notify_elicitation_url", "elicitation")])
def test_wait_kind_per_notification_type(session, shape, kind):
    out = hk(session, shape)
    r = row_of(session.name)
    assert out["state"] == "waiting" and r["state"] == "waiting" and r["flags"]["wait_kind"] == kind and not r["acked_at"]
    hk(session, "prompt")                                  # the person answered: no wait any more
    r = row_of(session.name)
    assert r["state"] == "working" and "wait_kind" not in r["flags"]


def test_wait_kind_is_dropped_when_the_turn_ends_or_the_session_stops(session):
    hk(session, "notify_permission")
    hk(session, "stop")
    assert "wait_kind" not in row_of(session.name)["flags"]
    hk(session, "notify_idle")
    hk(session, "session_end")
    assert "wait_kind" not in row_of(session.name)["flags"]


@pytest.mark.parametrize("ntype", ["agent_needs_input", "input_needed", "auth_success", "agent_completed", "quota_auto_resume_fired",
                                   "elicitation_response_x", "something_new"])
def test_other_notification_types_record_the_event_and_change_nothing(session, ntype):
    hk(session, "prompt")
    before = row_of(session.name)
    out = hk(session, "notify_permission", notification_type=ntype, message="fyi")
    assert out["state"] is None and out["kind"] == ntype
    after = row_of(session.name)
    assert after["state"] == "working" and "wait_kind" not in after["flags"] and not after["acked_at"]
    assert after["last_message"] == "fyi" and after["state_at"] == before["state_at"]
    assert event_names(session.name)[-1] == "Notification"


def test_the_dead_notification_types_are_gone_and_the_url_dialog_is_in():
    assert hooks.WAITING_NOTIFICATIONS == {"permission_prompt", "idle_prompt", "elicitation_dialog", "elicitation_url_dialog"}
    assert hooks.WAIT_KIND == {"permission_prompt": "permission", "idle_prompt": "idle", "elicitation_dialog": "elicitation",
                               "elicitation_url_dialog": "elicitation"}


def test_an_elicitation_completion_clears_only_the_elicitation_wait(session):
    hk(session, "notify_elicitation")
    out = hk(session, "notify_elicitation_complete")
    r = row_of(session.name)
    assert out["state"] == "working" and r["state"] == "working" and "wait_kind" not in r["flags"]
    assert event_names(session.name)[-1] == "Notification"
    # the response form of the completion closes it the same way
    hk(session, "notify_elicitation_url")
    hk(session, "notify_elicitation_complete", notification_type="elicitation_response")
    assert row_of(session.name)["state"] == "working"
    # a completion while the row waits for something else leaves that wait alone
    hk(session, "notify_permission")
    hk(session, "notify_elicitation_complete")
    r = row_of(session.name)
    assert r["state"] == "waiting" and r["flags"]["wait_kind"] == "permission"
    hk(session, "notify_idle")
    hk(session, "notify_elicitation_complete")
    assert row_of(session.name)["flags"]["wait_kind"] == "idle"
    # and a completion with no wait at all is just an event
    hk(session, "prompt")
    hk(session, "notify_elicitation_complete")
    assert row_of(session.name)["state"] == "working"


# ----- PostToolBatch -----

def test_post_tool_batch_after_a_permission_answered_in_the_tui_flips_to_working(session):
    hk(session, "prompt")
    hk(session, "notify_permission")
    assert row_of(session.name)["state"] == "waiting"
    before = event_names(session.name)
    out = hk(session, "post_tool_batch")
    r = row_of(session.name)
    assert out["skipped"] is True and out["state"] == "working"
    assert r["state"] == "working" and "wait_kind" not in r["flags"] and r["flags"]["last_tool_at"]
    assert r["last_event"] == "PostToolBatch", "the flip goes through set_state with the batch as its event"
    assert event_names(session.name) == before, "and PostToolBatch is never stored as an event row"
    assert rows("ev", "shop") == [] or sum(x[2] for x in rows("ev", "shop")) == 2        # not counted either (prompt + notification)


def test_post_tool_batch_leaves_an_idle_wait_and_a_working_row_alone(session):
    hk(session, "prompt")
    hk(session, "stop")
    hk(session, "notify_idle")
    before = row_of(session.name)
    out = hk(session, "post_tool_batch")
    after = row_of(session.name)
    assert out["skipped"] is True and "state" not in out
    assert (after["state"], after["state_at"], after["last_event"]) == ("waiting", before["state_at"], "Notification")
    assert after["flags"]["wait_kind"] == "idle" and after["flags"]["last_tool_at"]
    assert not after["acked_at"] and after["acked_at"] == before["acked_at"]
    # an elicitation wait too, and a plain working row keeps its last_event
    hk(session, "notify_elicitation")
    hk(session, "post_tool_batch")
    assert row_of(session.name)["flags"]["wait_kind"] == "elicitation"
    hk(session, "prompt")
    snap = row_of(session.name)
    hk(session, "post_tool_batch")
    now = row_of(session.name)
    assert (now["state"], now["state_at"], now["last_event"]) == (snap["state"], snap["state_at"], snap["last_event"])


def test_post_tool_batch_only_stamps_liveness_without_a_wait(session):
    hk(session, "post_tool_batch")
    first = row_of(session.name)["flags"]["last_tool_at"]
    assert first and row_of(session.name)["last_event"] is None and event_names(session.name) == []


def test_post_tool_batch_from_another_session_is_dropped(session):
    hk(session, "notify_permission")
    out = hk(session, "post_tool_batch", cwd=str(mem_dirs()[0] / "observer-sessions" / "x"))
    assert out["ignored"] == "foreign" and row_of(session.name)["state"] == "waiting"


# ----- foreign sessions -----

def mem_dirs():
    from app.config import settings
    return [Path.home() / ".claude-mem", settings.claude_config_dir.parent / ".claude-mem"]


@pytest.mark.parametrize("which", [0, 1])
def test_a_claude_mem_observer_cwd_is_foreign(session, which):
    obs = str(mem_dirs()[which] / "observer-sessions" / "abc")
    hk(session, "prompt")
    hk(session, "stop")
    before = row_of(session.name)
    for shape in ("prompt", "notify_permission", "stop", "session_start", "session_end", "subagent_start"):
        out = hk(session, shape, cwd=obs)
        assert out == {"session": session.name, "event": SHAPES[shape]["hook_event_name"], "ignored": "foreign", "how": "env"}
    out = hk(session, "prompt", cwd=str(mem_dirs()[which]))                      # the directory itself counts
    assert out["ignored"] == "foreign"
    after = row_of(session.name)
    assert (after["state"], after["last_prompt"], after["last_message"], after["flags"]) == \
        (before["state"], before["last_prompt"], before["last_message"], before["flags"])
    assert event_names(session.name) == ["UserPromptSubmit", "Stop"], "a foreign hook writes nothing, not even an event"


def test_a_path_that_only_looks_like_the_mem_dir_is_not_foreign(session):
    for cwd in (str(mem_dirs()[0]) + "-notes", str(mem_dirs()[0].parent / "claude-mem"), str(Path.home() / "projects" / ".claude-mem-x")):
        assert "ignored" not in hk(session, "prompt", cwd=cwd), cwd


def test_a_transcript_under_observer_sessions_is_foreign(session):
    out = hk(session, "prompt", transcript_path="/home/u/.claude/projects/-observer-sessions/abc.jsonl")
    assert "ignored" not in out                                                  # the marker is the path segment, not a substring
    out = hk(session, "prompt", transcript_path="/home/u/.claude-mem/observer-sessions/abc.jsonl")
    assert out["ignored"] == "foreign"
    assert row_of(session.name)["last_prompt"] == "write the tests"
    assert row_of(session.name)["flags"].get("transcript_path", "").endswith("-observer-sessions/abc.jsonl")


def test_a_conversation_another_open_row_owns_is_foreign(session):
    other = second_row(session.client)
    hk(session, "session_start")                                                 # SID is bound to the first row
    out = _hook(session.client, SHAPES["prompt"], session=other).json()
    assert out["ignored"] == "foreign" and out["session"] == other
    assert row_of(other)["state"] != "working" and row_of(other)["claude_session_id"] is None
    assert _hook(session.client, {**SHAPES["notify_permission"]}, session=other).json()["ignored"] == "foreign"
    assert _hook(session.client, {**STATUSLINE, "session_id": SID}, session=other, event="statusline").json()["ignored"] == "foreign"
    assert row_of(other)["stats"] is None
    # an id nobody owns binds to the row that reports it, and the first row still hears its own conversation
    assert _hook(session.client, {**SHAPES["prompt"], "session_id": SID3}, session=other).json()["state"] == "working"
    assert row_of(other)["claude_session_id"] == SID3
    assert hk(session, "prompt")["state"] == "working"


def test_an_ended_row_does_not_own_its_conversation(session):
    other = second_row(session.client)
    hk(session, "session_start")
    main.db.end(session.name, "killed")
    out = _hook(session.client, SHAPES["prompt"], session=other).json()
    assert "ignored" not in out and row_of(other)["claude_session_id"] == SID


def test_a_resumed_conversation_under_a_new_id_on_the_same_row_still_updates_it(session):
    hk(session, "session_start")
    assert row_of(session.name)["claude_session_id"] == SID
    out = hk(session, "session_start_resume")                                    # /resume: SID2, the same tmux session
    assert out["state"] == "idle" and "ignored" not in out
    r = row_of(session.name)
    assert r["claude_session_id"] == SID2 and r["agent_session_id"] == SID2
    assert hk(session, "prompt", session_id=SID2)["state"] == "working"
    # SID is nobody's now: the row hears it as the unknown id it is, without rebinding
    out = hk(session, "prompt", prompt="old id", session_id=SID)
    assert out["state"] == "working" and row_of(session.name)["claude_session_id"] == SID2
    # every SessionStart source rebinds
    for source, sid in (("clear", SID3), ("compact", SID), ("fork", SID2), ("startup", SID3)):
        hk(session, "session_start", source=source, session_id=sid)
        assert row_of(session.name)["claude_session_id"] == sid, source


def test_other_events_only_fill_a_missing_id(session):
    assert row_of(session.name)["claude_session_id"] is None
    hk(session, "prompt")
    assert row_of(session.name)["claude_session_id"] == SID
    hk(session, "notify_permission", session_id=SID3)
    _hook(session.client, {**STATUSLINE, "session_id": SID3}, session=session.name, event="statusline")
    assert row_of(session.name)["claude_session_id"] == SID


def test_malformed_session_ids_are_not_stored(session):
    for bad in (5, ["x"], "x" * 200, "has space", "../../etc", ""):
        hk(session, "session_start", session_id=bad)
    assert row_of(session.name)["claude_session_id"] is None
    hk(session, "session_start", session_id="plain-id_1.2")
    assert row_of(session.name)["claude_session_id"] == "plain-id_1.2"


def test_the_foreign_guard_runs_before_any_write_even_to_the_stats(session):
    out = _hook(session.client, {**STATUSLINE, "cwd": str(mem_dirs()[0] / "observer-sessions" / "z")}, session=session.name, event="statusline").json()
    assert out["ignored"] == "foreign"
    assert row_of(session.name)["stats"] is None and main.db.kv_get("statusline_sample") is None and values("ctx", session.name) == []


# ----- statusline extras -----

STATUSLINE_V2 = {
    **STATUSLINE, "session_name": "login-fix", "version": "2.1.288", "cwd": "/srv/projects/shop/api",
    "effort": {"level": "high"}, "fast_mode": False, "thinking": {"enabled": True}, "exceeds_200k_tokens": False,
    "prompt_cache": {"warm": True, "caching_observed": True, "ttl": "5m", "expires_at": 1738426200, "requests": 12, "misses": 1,
                     "hit_ratio": 0.92, "cache_write_tokens": 4000},
    "workspace": {"current_dir": "/srv/projects/shop/api", "project_dir": "/srv/projects/shop/api", "git_worktree": "login-fix",
                  "repo": {"host": "github.com", "owner": "rpandox", "name": "api"}},
    "pr": {"number": 42, "url": "https://github.com/rpandox/api/pull/42", "review_state": "approved", "kind": "pr"},
}


def stats_of(session, payload):
    assert _hook(session.client, payload, session=session.name, event="statusline").json()["stats"] is True
    return row_of(session.name)["stats"]


def test_statusline_extras_land_in_stats(session):
    st = stats_of(session, STATUSLINE_V2)
    assert (st["effort"], st["fast"], st["thinking"], st["session_name"], st["exceeds_200k"]) == ("high", False, True, "login-fix", False)
    assert st["prompt_cache"] == {"warm": True, "hit_ratio": 0.92, "expires_at": 1738426200, "ttl": "5m"}
    assert st["pr"] == {"number": 42, "url": "https://github.com/rpandox/api/pull/42", "review_state": "approved"}
    assert (st["worktree"], st["repo"]) == ("login-fix", "api")
    # everything that was there before is still there, under the same names
    assert (st["model"], st["model_id"], st["context_pct"], st["context_size"], st["cost_usd"]) == ("Opus", "claude-opus-5-5", 41.2, 200000, 1.25)
    assert (st["lines_added"], st["lines_removed"], st["version"]) == (10, 2, "2.1.288")
    assert st["rate_limits"]["five_hour"]["used_percentage"] == 23.5
    assert values("ctx", session.name) == [41.2]                                 # samples.record_statusline is unchanged


def test_statusline_with_none_of_the_extras_has_them_as_none(session):
    st = stats_of(session, {"model": {"id": "x", "display_name": "X"}})
    for k in ("effort", "fast", "thinking", "session_name", "prompt_cache", "pr", "worktree", "repo", "exceeds_200k", "rate_limits",
              "context_pct", "cost_usd"):
        assert st[k] is None, k
    st = stats_of(session, {"effort": {}, "workspace": {"repo": {}}, "prompt_cache": {}, "pr": {}, "thinking": {}})
    assert st["effort"] is None and st["repo"] is None and st["prompt_cache"] is None and st["pr"] is None and st["thinking"] is None
    st = stats_of(session, {"prompt_cache": {"warm": False}})
    assert st["prompt_cache"] == {"warm": False, "hit_ratio": None, "expires_at": None, "ttl": None}
    st = stats_of(session, {"workspace": {"git_worktree": "wt"}, "pr": {"number": 7}})
    assert st["worktree"] == "wt" and st["repo"] is None and st["pr"] == {"number": 7, "url": None, "review_state": None}


def test_statusline_extras_of_the_wrong_shape_never_raise_and_never_store_junk(session):
    junk = {"effort": ["high"], "fast_mode": "yes", "thinking": "on", "session_name": {"a": 1}, "exceeds_200k_tokens": 1,
            "prompt_cache": "warm", "workspace": "x", "pr": [1], "model": {"display_name": ["Opus"], "id": 5},
            "context_window": {"used_percentage": {"x": 1}, "context_window_size": float("nan")}, "cost": {"total_cost_usd": [1]},
            "rate_limits": {"five_hour": "x" * 10000}, "version": {"v": 1}}
    st = stats_of(session, junk)
    assert st["fast"] is None and st["thinking"] is None and st["exceeds_200k"] is None and st["effort"] is None
    assert st["session_name"] is None and st["prompt_cache"] is None and st["pr"] is None and st["repo"] is None
    assert st["model"] is None and st["context_pct"] is None and st["context_size"] is None and st["cost_usd"] is None
    assert st["rate_limits"] is None and st["version"] is None, "an absurdly large rate_limits blob is dropped"
    json.dumps(st, allow_nan=False)                                              # valid JSON all the way
    st = stats_of(session, {"session_name": "n" * 500, "pr": {"url": "u" * 900}, "effort": "x" * 90})
    assert len(st["session_name"]) == 120 and len(st["pr"]["url"]) == 300 and len(st["effort"]) == 20


def test_statusline_sample_is_the_raw_payload_plus_at_and_is_overwritten(session):
    assert main.db.kv_get("statusline_sample") is None
    _hook(session.client, STATUSLINE_V2, session=session.name, event="statusline")
    got = main.db.kv_get("statusline_sample")["value"]
    assert got == {**STATUSLINE_V2, "at": got["at"]} and got["at"]
    _hook(session.client, {"model": {"id": "other"}, "cwd": "/x"}, session=session.name, event="statusline")
    again = main.db.kv_get("statusline_sample")["value"]
    assert set(again) == {"model", "cwd", "at"}, "overwritten, not merged"


def test_statusline_sample_is_capped_at_8kb_and_stays_valid_json(session):
    huge = {**STATUSLINE_V2, "transcript_blob": "z" * 300_000, "tools": [{"n": i, "d": "d" * 50} for i in range(200)]}
    assert _hook(session.client, huge, session=session.name, event="statusline").json()["stats"] is True
    stored = main.db.kv_get("statusline_sample")["value"]
    assert len(json.dumps(stored)) <= 8192
    assert "transcript_blob" in stored["_truncated"] and "model" in stored and stored["at"]
    # everything oversized and nothing small: the marker still fits
    many = {f"k{i}": "v" * 900 for i in range(40)}
    _hook(session.client, many, session=session.name, event="statusline")
    stored = main.db.kv_get("statusline_sample")["value"]
    assert len(json.dumps(stored)) <= 8192 and stored["at"] and stored["_truncated"]
    _hook(session.client, {"x" * 9000: 1}, session=session.name, event="statusline")
    assert len(json.dumps(main.db.kv_get("statusline_sample")["value"])) <= 8192


def test_a_failing_statusline_sample_write_never_breaks_the_statusline(session, monkeypatch):
    real = main.db.kv_set

    def kv_set(key, value):
        if key == "statusline_sample":
            raise RuntimeError("disk full")
        return real(key, value)

    monkeypatch.setattr(main.db, "kv_set", kv_set)
    out = _hook(session.client, STATUSLINE_V2, session=session.name, event="statusline").json()
    assert out["stats"] is True and row_of(session.name)["stats"]["effort"] == "high"


# ----- STATS_HOOKS -----

def test_stats_hooks_run_right_after_the_stats_are_stored_and_a_failure_is_contained(session, monkeypatch):
    seen = []

    def spy(db, name, stats):
        seen.append((name, stats["model"], row_of(name)["stats"]["model"]))      # the row already holds the stats

    def boom(db, name, stats):
        raise RuntimeError("hook bug")

    monkeypatch.setattr(hooks, "STATS_HOOKS", [boom, spy])
    out = _hook(session.client, STATUSLINE, session=session.name, event="statusline").json()
    assert out["stats"] is True and "ignored" not in out
    assert seen == [(session.name, "Opus", "Opus")]
    assert values("ctx", session.name) == [41.2], "the series write after the hooks still happens"
    monkeypatch.setattr(hooks, "STATS_HOOKS", [])
    assert _hook(session.client, STATUSLINE, session=session.name, event="statusline").json()["stats"] is True
    assert isinstance(hooks.STATS_HOOKS, list)


def test_stats_hooks_are_a_module_level_list():
    import importlib
    assert isinstance(importlib.import_module("app.hooks").STATS_HOOKS, list)


# ----- Stop -----

def test_stop_prefers_the_last_assistant_message_and_keeps_the_full_text(session, fake_tmux):
    fake_tmux["screen"] = "the screen line that must not win\n? for shortcuts\n"
    text = "All done.\n\n  I fixed   the login   and added tests.\n" + "More detail. " * 60
    out = hk(session, "stop", last_assistant_message=text)
    r = row_of(session.name)
    assert out["state"] == "done" and r["state"] == "done"
    assert r["last_message"] == " ".join(text.split())[:300] and "\n" not in r["last_message"] and len(r["last_message"]) == 300
    assert r["flags"]["last_result"] == text
    assert not r["acked_at"]


def test_stop_keeps_at_most_20000_characters_of_the_result(session):
    hk(session, "stop", last_assistant_message="é" * 25_000)
    r = row_of(session.name)
    assert len(r["flags"]["last_result"]) == 20_000 and len(r["last_message"]) == 300


def test_stop_without_the_message_falls_back_to_the_screen_and_drops_a_stale_result(session, fake_tmux):
    hk(session, "stop", last_assistant_message="turn one result")
    assert row_of(session.name)["flags"]["last_result"] == "turn one result"
    fake_tmux["screen"] = "╭──────╮\n│ Fixed it on the screen. │\n╰──────╯\n? for shortcuts\n"
    hk(session, {"hook_event_name": "Stop", "session_id": SID})
    r = row_of(session.name)
    assert r["last_message"] == "Fixed it on the screen." and "last_result" not in r["flags"]
    for blank in ("", "   \n  "):                                                # nothing usable in the payload: the screen again
        hk(session, "stop", last_assistant_message=blank)
        assert row_of(session.name)["last_message"] == "Fixed it on the screen."
    hk(session, "stop", last_assistant_message=None)
    assert row_of(session.name)["last_message"] == "Fixed it on the screen."


def test_a_stop_failure_clears_the_previous_result(session):
    hk(session, "stop", last_assistant_message="result")
    hk(session, {"hook_event_name": "StopFailure", "session_id": SID, "error": "server_error", "last_assistant_message": "API Error: 500"})
    r = row_of(session.name)
    assert r["state"] == "errored" and r["last_message"] == "API Error: 500" and "last_result" not in r["flags"]


def test_flags_and_state_are_on_disk_before_the_notification_is_built(session, monkeypatch):
    seen = []
    monkeypatch.setattr(notify, "notify_session", lambda name, state, message, kind: seen.append((state, message, kind, row_of(name))) or True)
    hk(session, "stop", last_assistant_message="shipped it")
    hk(session, "notify_permission")
    (state, message, kind, row), (state2, _, kind2, row2) = seen
    assert (state, message, kind) == ("done", "shipped it", None)
    assert row["state"] == "done" and row["flags"]["last_result"] == "shipped it"
    assert (state2, kind2, row2["flags"]["wait_kind"]) == ("waiting", "permission_prompt", "permission")


# ----- SessionStart / SessionEnd -----

def test_a_resume_records_how_long_it_sat_and_how_big_it_is(session):
    hk(session, "session_start")
    assert "resumed" not in row_of(session.name)["flags"]
    hk(session, "session_start_resume")
    res = row_of(session.name)["flags"]["resumed"]
    assert res["context_tokens"] == 91234 and res["since_s"] == 5400 and res["cache_cold"] is True and res["at"]
    hk(session, "session_start", source="startup", session_id=SID2)               # a fresh start forgets it
    assert "resumed" not in row_of(session.name)["flags"]
    hk(session, "session_start_resume", seconds_since_last_response=None, prompt_cache_likely_expired="no")
    assert row_of(session.name)["flags"]["resumed"]["context_tokens"] == 91234
    assert "cache_cold" not in row_of(session.name)["flags"]["resumed"]
    hk(session, "session_start_resume", seconds_since_last_response="soon", context_tokens=True)
    assert "resumed" not in row_of(session.name)["flags"]


def test_a_compaction_restart_does_not_flip_a_working_turn_to_idle(session):
    hk(session, "prompt")
    out = hk(session, "session_start", source="compact")
    assert out["state"] is None and row_of(session.name)["state"] == "working"
    assert event_names(session.name)[-1] == "SessionStart"
    for source in ("startup", "resume", "clear", "fork"):
        hk(session, "prompt")
        hk(session, "session_start", source=source)
        assert row_of(session.name)["state"] == "idle", source


def test_session_end_ends_the_row_except_when_a_clear_or_resume_carries_on(session):
    hk(session, "prompt")
    hk(session, "session_end", reason="clear")
    assert row_of(session.name)["state"] == "working"
    hk(session, "session_end", reason="resume")
    assert row_of(session.name)["state"] == "working"
    out = hk(session, "session_end", reason="logout")
    assert out["state"] == "ended" and row_of(session.name)["state"] == "ended"
    hk(session, "session_start")
    assert row_of(session.name)["state"] == "idle"
    hk(session, "session_end", reason="other")
    assert row_of(session.name)["state"] == "ended"
    assert event_names(session.name).count("SessionEnd") == 4, "every SessionEnd is still recorded"


def test_events_without_a_dedicated_branch_record_their_matcher_and_change_nothing(session):
    hk(session, "prompt")
    before = row_of(session.name)
    for ev in ("PostModelSwitch", "TaskCreated", "TaskCompleted", "ConfigChange", "SomethingNew"):
        out = _hook(session.client, {"hook_event_name": ev, "session_id": SID, "matcher": "user_settings"}, session=session.name).json()
        assert out == {"session": session.name, "event": ev, "state": None, "kind": "user_settings", "how": "env"}
    after = row_of(session.name)
    assert (after["state"], after["state_at"], after["last_prompt"]) == (before["state"], before["state_at"], before["last_prompt"])
    assert after["last_event"] == "SomethingNew"
    assert {"PostModelSwitch", "TaskCreated", "TaskCompleted", "ConfigChange"} <= set(event_names(session.name))


# ----- malformed input -----

ALL_EVENTS = ["SessionStart", "UserPromptSubmit", "Notification", "Stop", "StopFailure", "SessionEnd", "SubagentStart", "SubagentStop",
              "PreCompact", "PostCompact", "PostToolBatch", "statusline", "PostModelSwitch", "TaskCreated", "TaskCompleted",
              "ConfigChange", "Whatever"]
WRONG_TYPES = [None, [], ["a"], 5, 1.5, True, "str", {"a": [1]}, "x" * 5000]
FIELDS = ["session_id", "prompt", "message", "notification_type", "type", "matcher", "source", "reason", "error", "error_type",
          "last_assistant_message", "transcript_path", "cwd", "trigger", "seconds_since_last_response", "context_tokens", "model",
          "context_window", "cost", "rate_limits", "effort", "workspace", "prompt_cache", "pr", "thinking", "fast_mode"]


@pytest.mark.parametrize("payload", [None, [], 5, "x", [{"a": 1}], 1.5, True])
def test_apply_takes_any_payload_without_raising(session, payload):
    for ev in ALL_EVENTS:
        out = hooks.apply(main.db, session.name, ev, payload)
        assert out["session"] == session.name and out["event"] == ev


@pytest.mark.parametrize("bad", WRONG_TYPES, ids=lambda v: type(v).__name__ + str(len(str(v))))
def test_apply_takes_every_field_with_the_wrong_type_without_raising(session, bad):
    for ev in ALL_EVENTS:
        payload = {f: bad for f in FIELDS}
        out = hooks.apply(main.db, session.name, ev, payload)
        assert out["session"] == session.name
        for f in FIELDS:                                                         # one bad field at a time too
            hooks.apply(main.db, session.name, ev, {f: bad, "hook_event_name": ev})
    r = row_of(session.name)
    assert r is not None and json.dumps(r["flags"]) and len(json.dumps(r["flags"])) < 20_000 + 4096
    json.dumps(r["stats"], allow_nan=False)


@pytest.mark.parametrize("event", [None, 5, ["Stop"], {"a": 1}, "", "E" * 500])
def test_apply_coerces_an_odd_event_name(session, event):
    out = hooks.apply(main.db, session.name, event, {"hook_event_name": "Stop"})
    assert isinstance(out["event"], str) and 0 < len(out["event"]) <= 64


def test_hook_route_never_500s_on_malformed_bodies(session):
    c = session.client
    hdr = {"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard-Session": session.name}
    for body, want in ((b"[1,2]", 400), (b"5", 400), (b"null", 400), (b'"s"', 400), (b"{not json", 400), (b"", 200),
                       (b'{"hook_event_name": ["Stop"], "message": {"a": 1}}', 200),
                       (b'{"hook_event_name": "Notification", "notification_type": {"a": 1}, "message": [1]}', 200),
                       (b'{"hook_event_name": "UserPromptSubmit", "prompt": {"a": 1}, "session_id": {"b": 2}}', 200)):
        r = c.post("/api/hook", headers=hdr, content=body)
        assert r.status_code == want, (body, r.status_code, r.text)
    big = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "p" * 600_000}).encode()
    assert c.post("/api/hook", headers=hdr, content=big).status_code == 413           # over MAX_BODY: refused, not stored
    ok = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "<task-notification>" + "p" * 500_000}).encode()
    assert len(ok) < hooks.MAX_BODY and c.post("/api/hook", headers=hdr, content=ok).status_code == 200
    r = row_of(session.name)
    assert r["state"] == "working" and len(r["flags"]["last_system_turn"]["head"]) == 160 and len(json.dumps(r["flags"])) < 1000


def test_a_cwd_of_the_wrong_type_is_unresolved_not_a_500(session):
    c = session.client
    for cwd in (5, ["x"], {"a": 1}, True, "", "x" * 10_000, "\x00bad"):
        r = _hook(c, {"hook_event_name": "Stop", "cwd": cwd})                     # no session header: resolution falls to the cwd
        assert r.status_code == 200 and r.json()["ignored"] == "unresolved", cwd


def test_oversized_fields_are_bounded_where_they_are_stored(session):
    hk(session, "stop", last_assistant_message="m" * 500_000, transcript_path="t" * 5000, matcher="k" * 5000)
    r = row_of(session.name)
    assert len(r["flags"]["last_result"]) == 20_000 and "transcript_path" not in r["flags"] and len(r["last_message"]) == 300
    hk(session, "notify_permission", message="n" * 500_000, notification_type="permission_prompt")
    assert len(row_of(session.name)["last_message"]) <= 500
    with main.db.lock:
        sizes = [len(x["payload"]) for x in main.db.conn.execute("SELECT payload FROM events").fetchall()]
    assert max(sizes) <= 20_000
    ev = main.db.recent_events(5)
    assert all(len(e["kind"] or "") <= 200 for e in ev)


def test_the_events_cap_and_skip_list_are_unchanged():
    from app import db as dbmod
    assert dbmod.EVENTS_CAP == 20000 and dbmod.SKIP_EVENTS == {"PostToolBatch", "statusline"}


def test_hooks_status_reports_the_new_event_set(session, tmp_path):
    from app.agents import claude
    from app.config import settings
    ag = claude.ClaudeAgent()
    cfg = settings.claude_config_dir
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/x/bin/ccboard-hook"}]}]}}))
    st = ag.hooks_status()
    assert st["installed"] is False and st["events"] == ["Stop"]
    ag.install_hooks(tmp_path / "app", remote_approve=False)
    st = ag.hooks_status()
    assert st["installed"] is True and st["events"] == ag._required_events() and "SessionEnd" in st["events"] and "PostToolBatch" in st["events"]
    assert len(st["events"]) == 15


def test_child_sessions_are_not_the_row(lite_client, projects_dir, fake_tmux, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")        # CI has no claude: a session create needs one
    """A nested claude (CLAUDE_CODE_CHILD_SESSION=1: claude-mem's observer, a workflow or SDK subagent) inherits CCBOARD_SESSION and
    TMUX_PANE from the parent; on ubu2 the observer's SessionStart/Stop landed on the user's row. X-CCBoard-Child: 1 makes apply
    ignore the event unless the session_id is the row's own."""
    git_init(projects_dir / "shop" / "api")
    r = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "claude"}).json()
    name, own = r["tmux"], r["claude_session_id"]
    res = _hook(lite_client, {"session_id": "11111111-1111-4111-8111-111111111111", "hook_event_name": "UserPromptSubmit", "prompt": "observer turn"},
                session=name, extra={"X-CCBoard-Child": "1"}).json()
    assert res.get("ignored") == "child", res
    row = main.db.open_rows()[name]
    assert row["state"] != "working" and (row.get("last_prompt") or "") != "observer turn"
    res = _hook(lite_client, {"session_id": own, "hook_event_name": "UserPromptSubmit", "prompt": "the real one"}, session=name, extra={"X-CCBoard-Child": "1"}).json()
    assert res.get("ignored") is None and main.db.open_rows()[name]["last_prompt"] == "the real one", "the row's own id is never a child"
    res = _hook(lite_client, {"session_id": "22222222-2222-4222-8222-222222222222", "hook_event_name": "Stop",
                              "transcript_path": "/home/x/.claude/projects/-home-x--claude-mem-observer-sessions-270/abc.jsonl"}, session=name).json()
    assert res.get("ignored") == "foreign", "the observer transcript slug (dashes, no slashes) is foreign too"


def test_statusline_sample_cap_is_linear():
    """A statusline body under MAX_BODY with tens of thousands of keys must not hold the hook thread (the first version re-dumped
    the whole payload after every dropped key: 183 s for 30000 keys)."""
    import time
    big = {f"k{i}": "x" * 12 for i in range(30000)}
    t0 = time.monotonic()
    out = hooks._statusline_sample(big)
    assert time.monotonic() - t0 < 2.0
    assert out["_truncated"] and "at" in out
    mid = {f"k{i}": "y" * 300 for i in range(60)}                 # a few large keys: the biggest go first, the rest survive
    out = hooks._statusline_sample(mid)
    assert len(json.dumps(out)) <= hooks.STATUSLINE_SAMPLE_MAX and out["_truncated"] and any(k.startswith("k") for k in out)
    small = {"model": {"display_name": "Opus"}, "cost": {"total_cost_usd": 1.5}}
    assert hooks._statusline_sample(small)["model"] == {"display_name": "Opus"}


def _hook_perm(client, session, payload, extra=None):
    """POST /api/permission the way bin/ccboard-permission does (hook token + session header, X-CCBoard-Agent in `extra`); the call
    long-polls for a decision."""
    hdr = {"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard-Session": session, "Content-Type": "application/json", **(extra or {})}
    return client.post("/api/permission", headers=hdr, content=json.dumps(payload))


def test_a_board_permission_sets_the_wait_kind_the_tool_batch_clears(lite_client, projects_dir, fake_tmux, monkeypatch):
    """The board's own PermissionRequest path (api_permission) must set flags.wait_kind='permission' like the Notification hook,
    otherwise a prompt answered in the TUI never leaves 'needs you' (the PostToolBatch flip keys on that flag)."""
    import threading, time
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")        # CI has no claude: a session create needs one
    git_init(projects_dir / "shop" / "api")
    r = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "claude"}).json()
    name = r["tmux"]
    monkeypatch.setattr(settings, "approve_timeout", 2)
    out = {}
    def ask():
        out["r"] = _hook_perm(lite_client, name, {"tool_name": "Bash", "tool_input": {"command": "npm test"}})
    t = threading.Thread(target=ask); t.start()
    for _ in range(100):
        row = main.db.open_rows()[name]
        if row["state"] == "waiting":
            break
        time.sleep(0.05)
    assert row["state"] == "waiting" and row["flags"].get("wait_kind") == "permission"
    res = _hook(lite_client, {"session_id": r["claude_session_id"], "hook_event_name": "PostToolBatch"}, session=name, event="PostToolBatch").json()
    row = main.db.open_rows()[name]
    assert row["state"] == "working" and not row["flags"].get("wait_kind"), (res, row["flags"])
    t.join(timeout=5)


# ---------------------------------------------------------------- Codex hooks (v0.5.11)
#
# Codex payloads go through /api/hook with X-CCBoard-Agent: codex (what bin/ccboard-hook sends from a codex session, CCBOARD_AGENT in the
# tmux env). The row's agent picks the adapter; the sub-thread rule and the Interrupt permission expiry live in hooks.apply.

CXA = "0198aaaa-bbbb-7ccc-8ddd-eeeeeeeeeee1"          # the row's own thread
CXB = "0198aaaa-bbbb-7ccc-8ddd-eeeeeeeeeee2"          # a guardian / subagent thread (or the next conversation after a /resume)
CXC = "0198aaaa-bbbb-7ccc-8ddd-eeeeeeeeeee3"


@pytest.fixture
def cxs(lite_client, projects_dir, fake_tmux, fake_codex):
    """A board with one codex session row (no conversation id yet) and a claude one next to it."""
    git_init(projects_dir / "shop" / "api")
    name = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "codex"}).json()["tmux"]
    return type("CX", (), {"client": lite_client, "name": name})


def cx_hook(s, event, name=None, agent="codex", extra=None, **payload):
    r = _hook(s.client, {"hook_event_name": event, "session_id": CXA, **payload}, session=name or s.name,
              extra={"X-CCBoard-Agent": agent, **(extra or {})})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_codex_row_follows_its_hooks_through_a_whole_turn(cxs):
    assert row_of(cxs.name)["agent"] == "codex" and row_of(cxs.name)["claude_session_id"] is None
    out = cx_hook(cxs, "SessionStart", source="startup", cwd="/srv/projects/shop/api")
    assert out["state"] == "idle" and out["how"] == "env" and out["kind"] == "startup"
    r = row_of(cxs.name)
    assert r["claude_session_id"] == CXA and r["flags"]["hook_seen"] is True, "the first hook binds the conversation id"
    out = cx_hook(cxs, "UserPromptSubmit", prompt="add a login page", turn_id="t1")
    assert out["state"] == "working" and row_of(cxs.name)["last_prompt"] == "add a login page"
    out = cx_hook(cxs, "Stop", last_assistant_message="Done: added /login with tests.", turn_id="t1")
    r = row_of(cxs.name)
    assert out["state"] == "done" and r["state"] == "done" and r["last_message"] == "Done: added /login with tests."
    assert r["flags"]["last_result"] == "Done: added /login with tests."
    cx_hook(cxs, "SubagentStart", agent_type="explorer")
    cx_hook(cxs, "SubagentStart", agent_type="explorer")
    cx_hook(cxs, "SubagentStop", agent_type="explorer")
    assert row_of(cxs.name)["flags"]["subagents"] == 1
    cx_hook(cxs, "PreCompact", trigger="auto")
    assert row_of(cxs.name)["flags"]["compacting"] is True
    cx_hook(cxs, "PostCompact", trigger="auto")
    assert row_of(cxs.name)["flags"]["compacting"] is False
    out = cx_hook(cxs, "SessionEnd", reason="prompt_input_exit")
    assert out["state"] == "ended" and row_of(cxs.name)["state"] == "ended"
    assert {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"} <= set(event_names(cxs.name))
    with main.db.lock:
        agents_on_events = {r[0] for r in main.db.conn.execute("SELECT agent FROM events WHERE tmux_name=?", (cxs.name,)).fetchall()}
    assert agents_on_events == {"codex"}


def test_a_codex_interrupt_goes_idle_and_closes_the_pending_permissions_as_interrupted(cxs):
    cx_hook(cxs, "SessionStart", source="startup")
    cx_hook(cxs, "UserPromptSubmit", prompt="run the migration")
    pid = main.db.perm_add(cxs.name, "Bash", "Bash: ./migrate.sh", {"command": "./migrate.sh"})
    other = main.db.perm_add("shop--api--elsewhere", "Bash", "Bash: ls", {})
    main.db.set_state(cxs.name, "waiting", "PermissionRequest", message="permission: Bash: ./migrate.sh", attention=True)
    out = cx_hook(cxs, "Interrupt", extra={"X-CCBoard-Agent": "codex"})
    assert out["state"] == "idle" and out["kind"] == "interrupt" and row_of(cxs.name)["state"] == "idle"
    got = main.db.perm_get(pid)
    assert got["decision"] == "interrupt" and got["source"] == "system" and got["decided_at"]
    assert main.db.perm_get(other)["decision"] is None, "another session's request is not this session's to close"
    assert [p["id"] for p in main.db.perm_pending()] == [other]
    assert row_of(cxs.name)["flags"].get("wait_kind") is None
    assert "Interrupt" in event_names(cxs.name)


def test_an_interrupt_wakes_the_permission_long_poll_at_once(cxs, monkeypatch):
    import threading, time
    from app.config import settings
    monkeypatch.setattr(settings, "approve_timeout", 30)
    cx_hook(cxs, "SessionStart", source="startup")
    out = {}

    def ask():
        out["r"] = _hook_perm(cxs.client, cxs.name, {"tool_name": "apply_patch", "tool_input": {"input": "*** Begin Patch"}})
    t = threading.Thread(target=ask)
    t.start()
    for _ in range(100):
        if main.db.perm_pending():
            break
        time.sleep(0.05)
    assert main.db.perm_pending() and row_of(cxs.name)["state"] == "waiting"
    started = time.monotonic()
    cx_hook(cxs, "Interrupt")
    t.join(timeout=10)
    assert not t.is_alive() and time.monotonic() - started < 5, "the waiter was woken, not left to its 25 s timeout"
    assert out["r"].json()["behavior"] is None and out["r"].json()["reason"] == "interrupt"
    assert row_of(cxs.name)["state"] == "idle" and main.db.perm_pending() == []


def test_events_of_another_codex_thread_are_counted_and_never_change_the_state(cxs):
    cx_hook(cxs, "SessionStart", source="startup")
    cx_hook(cxs, "UserPromptSubmit", prompt="review the diff")
    before = row_of(cxs.name)
    # a guardian / subagent thread reports under its own id, in the same pane with the same env
    for event, extra in (("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {"prompt": "guardian: assess this command"}),
                         ("Stop", {"last_assistant_message": "allow"}), ("Interrupt", {}), ("SessionEnd", {"reason": "other"})):
        out = _hook(cxs.client, {"hook_event_name": event, "session_id": CXB, **extra}, session=cxs.name,
                    extra={"X-CCBoard-Agent": "codex"}).json()
        assert out == {"session": cxs.name, "event": event, "ignored": "subthread", "how": "env"}, (event, out)
    after = row_of(cxs.name)
    assert after["state"] == before["state"] == "working" and after["state_at"] == before["state_at"]
    assert after["last_prompt"] == "review the diff" and after["last_message"] == before["last_message"]
    assert after["claude_session_id"] == CXA and after["flags"]["subthreads"] == 5
    assert after["flags"].get("last_result") is None
    with main.db.lock:
        own = [r[0] for r in main.db.conn.execute("SELECT event FROM events WHERE tmux_name=? AND COALESCE(kind, '')<>'subthread' ORDER BY id",
                                                  (cxs.name,)).fetchall()]
        subs = [r[0] for r in main.db.conn.execute("SELECT event FROM events WHERE tmux_name=? AND kind='subthread' ORDER BY id",
                                                   (cxs.name,)).fetchall()]
    assert own == ["SessionStart", "UserPromptSubmit"], "the sub-thread events are stored as such, not as the row's own"
    assert subs == ["SessionStart", "UserPromptSubmit", "Stop", "Interrupt", "SessionEnd"]
    # the row's own thread still drives it
    assert cx_hook(cxs, "Stop", last_assistant_message="all good")["state"] == "done"


def test_a_subthread_interrupt_does_not_close_the_rows_permission(cxs):
    cx_hook(cxs, "SessionStart", source="startup")
    pid = main.db.perm_add(cxs.name, "Bash", "Bash: ls", {})
    main.db.set_state(cxs.name, "waiting", "PermissionRequest", attention=True)
    _hook(cxs.client, {"hook_event_name": "Interrupt", "session_id": CXB}, session=cxs.name, extra={"X-CCBoard-Agent": "codex"})
    assert main.db.perm_get(pid)["decision"] is None and row_of(cxs.name)["state"] == "waiting"


def test_a_resume_clear_or_fork_session_start_rebinds_the_codex_row(cxs):
    cx_hook(cxs, "SessionStart", source="startup")
    for source, sid in (("resume", CXB), ("clear", CXC), ("fork", CXA)):
        out = cx_hook(cxs, "SessionStart", source=source, session_id=sid)
        assert out["state"] == "idle" and "ignored" not in out, source
        assert row_of(cxs.name)["claude_session_id"] == sid, source
        assert cx_hook(cxs, "UserPromptSubmit", prompt="next", session_id=sid)["state"] == "working"
    assert row_of(cxs.name)["flags"].get("subthreads") is None, "none of those was a sub-thread"


def test_before_the_first_hook_binds_an_id_nothing_is_a_subthread(cxs):
    assert row_of(cxs.name)["claude_session_id"] is None
    out = cx_hook(cxs, "UserPromptSubmit", prompt="hello", session_id=CXB)           # no SessionStart seen (hooks reviewed late)
    assert out["state"] == "working" and row_of(cxs.name)["claude_session_id"] == CXB
    assert row_of(cxs.name)["flags"].get("subthreads") is None


def test_the_header_never_relabels_a_claude_row_and_picks_the_adapter_of_a_shell_row(lite_client, projects_dir, fake_tmux, fake_codex, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    git_init(projects_dir / "shop" / "api")
    claude = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "claude"}).json()
    shell = second_row(lite_client)
    assert hooks.hook_agent("claude", "codex") == "claude" and hooks.hook_agent("codex", "claude") == "codex"
    assert hooks.hook_agent("shell", "codex") == "codex" and hooks.hook_agent("shell", "CODEX ") == "codex"
    assert hooks.hook_agent("shell", "claude") == "claude" and hooks.hook_agent("shell", "gemini") == "shell"
    assert hooks.hook_agent(None, None) is None and hooks.hook_agent("shell", None) == "shell"
    # an Interrupt that claims to be codex's, sent to a Claude row, is somebody else's hook (a Codex process that is not this row's): it
    # is ignored before anything is written: no state change, no permission closed, no event row
    pid = main.db.perm_add(claude["tmux"], "Bash", "Bash: ls", {})
    main.db.set_state(claude["tmux"], "waiting", "PermissionRequest", attention=True)
    before = event_names(claude["tmux"])
    res = _hook(lite_client, {"hook_event_name": "Interrupt", "session_id": claude["claude_session_id"]}, session=claude["tmux"],
                extra={"X-CCBoard-Agent": "codex"}).json()
    assert res == {"ignored": "foreign", "session": claude["tmux"], "how": "env"}
    assert main.db.perm_get(pid)["decision"] is None and row_of(claude["tmux"])["state"] == "waiting" and event_names(claude["tmux"]) == before
    # a shell row where somebody ran codex by hand: the header makes it a codex event (stored as one), and the Stop text lands
    res = _hook(lite_client, {"hook_event_name": "Stop", "session_id": CXA, "last_assistant_message": "hand-run answer"}, session=shell,
                extra={"X-CCBoard-Agent": "codex"}).json()
    assert res["state"] == "done"
    with main.db.lock:
        got = main.db.conn.execute("SELECT agent FROM events WHERE tmux_name=? AND event='Stop'", (shell,)).fetchone()
    assert got[0] == "codex" and row_of(shell)["agent"] == "shell", "the row keeps its own agent"


@pytest.mark.parametrize("row,header,mismatch", [
    ("claude", "codex", True), ("codex", "claude", True), ("claude", " CODEX ", True), ("codex", "Claude", True),
    ("claude", "claude", False), ("codex", "codex", False),
    ("shell", "claude", False), ("shell", "codex", False), ("shell", "shell", False),       # a shell row takes either: the person may run both by hand
    ("claude", None, False), ("codex", "", False), ("claude", "gemini", False), ("codex", "shell", False),   # no or unknown agent: not a mismatch
    (None, "codex", False), ("gemini", "codex", False),
])
def test_agent_mismatch_is_two_known_adapter_agents_that_differ(row, header, mismatch):
    assert hooks.agent_mismatch(row, header) is mismatch


def _new_claude_row(client, monkeypatch, repo="api"):
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    return client.post(f"/api/projects/shop/repos/{repo}/sessions", headers=H, json={"launcher": "claude"}).json()


def _untouched(name, before):
    r = row_of(name)
    assert {k: r[k] for k in ("state", "state_at", "last_prompt", "last_message", "claude_session_id", "flags")} == before["row"]
    assert event_names(name) == before["events"]


def _snapshot(name):
    r = row_of(name)
    return {"row": {k: r[k] for k in ("state", "state_at", "last_prompt", "last_message", "claude_session_id", "flags")},
            "events": event_names(name)}


def test_a_codex_hook_on_a_claude_row_is_ignored_by_env_and_by_cwd_and_the_row_is_untouched(lite_client, projects_dir, fake_tmux, monkeypatch):
    """Defect 2: ccboard's hooks sit in the global ~/.codex/hooks.json, so Hermes, `codex exec` and Codex Desktop fire at the board too.
    Whatever their cwd or inherited env, they must not move a Claude row."""
    git_init(projects_dir / "shop" / "api")
    claude = _new_claude_row(lite_client, monkeypatch)
    name = claude["tmux"]
    _hook(lite_client, {"hook_event_name": "SessionStart", "source": "startup", "session_id": claude["claude_session_id"]}, session=name)
    _hook(lite_client, {"hook_event_name": "UserPromptSubmit", "session_id": claude["claude_session_id"], "prompt": "real prompt"}, session=name)
    before = _snapshot(name)
    foreign = {"X-CCBoard-Agent": "codex"}
    for event, extra in (("UserPromptSubmit", {"prompt": "hermes prompt"}), ("Stop", {"last_assistant_message": "hermes done"}),
                         ("SessionStart", {"source": "startup"}), ("Interrupt", {}), ("SessionEnd", {"reason": "other"})):
        payload = {"hook_event_name": event, "session_id": CXA, **extra}
        out = _hook(lite_client, payload, session=name, extra=foreign).json()                       # the env path (an inherited CCBOARD_SESSION)
        assert out == {"ignored": "foreign", "session": name, "how": "env"}, (event, out)
        out = _hook(lite_client, {**payload, "cwd": str(projects_dir / "shop" / "api")}, extra=foreign).json()      # the cwd path
        assert out == {"ignored": "foreign", "session": name, "how": "cwd"}, (event, out)
        _untouched(name, before)
    assert row_of(name)["state"] == "working" and row_of(name)["last_prompt"] == "real prompt"
    # the row's own agent (and a hook that names none) still drives it
    assert _hook(lite_client, {"hook_event_name": "Stop", "session_id": claude["claude_session_id"]}, session=name,
                 extra={"X-CCBoard-Agent": "claude"}).json()["state"] == "done"
    assert _hook(lite_client, {"hook_event_name": "UserPromptSubmit", "session_id": claude["claude_session_id"], "prompt": "again"},
                 session=name).json()["state"] == "working"


def test_a_claude_hook_on_a_codex_row_is_ignored_the_same_way(cxs, projects_dir):
    cx_hook(cxs, "SessionStart", source="startup")
    cx_hook(cxs, "UserPromptSubmit", prompt="the codex prompt")
    before = _snapshot(cxs.name)
    for event, extra in (("UserPromptSubmit", {"prompt": "claude prompt"}), ("Stop", {}), ("SessionStart", {"source": "startup"})):
        payload = {"hook_event_name": event, "session_id": "11111111-1111-4111-8111-111111111111", **extra}
        out = _hook(cxs.client, payload, session=cxs.name, extra={"X-CCBoard-Agent": "claude"}).json()
        assert out == {"ignored": "foreign", "session": cxs.name, "how": "env"}, (event, out)
        out = _hook(cxs.client, {**payload, "cwd": str(projects_dir / "shop" / "api")}, extra={"X-CCBoard-Agent": "claude"}).json()
        assert out == {"ignored": "foreign", "session": cxs.name, "how": "cwd"}, (event, out)
        _untouched(cxs.name, before)
    assert cx_hook(cxs, "Stop", last_assistant_message="fine")["state"] == "done", "a codex hook for the codex row still lands"


def test_a_shell_row_takes_hooks_from_either_agent_and_a_headerless_hook_takes_the_old_path(lite_client, projects_dir, fake_tmux, fake_codex):
    git_init(projects_dir / "shop" / "api")
    for agent, sid in (("claude", "22222222-2222-4222-8222-222222222222"), ("codex", CXA)):
        shell = second_row(lite_client)                                                  # one shell row per agent the person runs by hand
        out = _hook(lite_client, {"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": f"by hand with {agent}"}, session=shell,
                    extra={"X-CCBoard-Agent": agent}).json()
        assert out["state"] == "working" and "ignored" not in out, (agent, out)
        out = _hook(lite_client, {"hook_event_name": "Stop", "session_id": sid}, session=shell).json()      # no header: an older hook script
        assert out["state"] == "done", (agent, out)


def test_a_hook_of_an_unknown_agent_is_not_a_mismatch(lite_client, projects_dir, fake_tmux, monkeypatch):
    git_init(projects_dir / "shop" / "api")
    claude = _new_claude_row(lite_client, monkeypatch)
    out = _hook(lite_client, {"hook_event_name": "UserPromptSubmit", "session_id": claude["claude_session_id"], "prompt": "x"},
                session=claude["tmux"], extra={"X-CCBoard-Agent": "gemini"}).json()
    assert out["state"] == "working"


def test_a_foreign_permission_request_never_touches_the_row_or_blocks_the_caller(lite_client, projects_dir, fake_tmux, fake_codex, monkeypatch):
    """Defect 3: the PermissionRequest hook is synchronous. A Hermes or `codex exec` run whose request resolves to a board row must get
    an immediate empty answer: no pending request, no `waiting`, no phone notice, no event row, no deny."""
    import time
    from app.config import settings
    monkeypatch.setattr(settings, "approve_timeout", 30)
    git_init(projects_dir / "shop" / "api")
    claude = _new_claude_row(lite_client, monkeypatch)
    cxname = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "codex"}).json()["tmux"]
    pushes = []
    monkeypatch.setattr("app.permissions.push_request", lambda *a, **k: pushes.append(a))
    payload = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}
    for name, agent in ((claude["tmux"], "codex"), (cxname, "claude")):
        before = _snapshot(name)
        started = time.monotonic()
        r = _hook_perm(lite_client, name, payload, {"X-CCBoard-Agent": agent})
        assert r.status_code == 200 and time.monotonic() - started < 5, "it answered at once, not after the approve timeout"
        assert r.json() == {"behavior": None, "reason": "foreign", "ignored": "foreign", "session": name}, r.json()
        assert main.db.perm_pending() == [] and pushes == []
        _untouched(name, before)
    # the owner's own request is recorded: the codex row with the codex header (no waiter answers, so it times out after the minimum 1 s)
    monkeypatch.setattr(settings, "approve_timeout", 2)
    r = _hook_perm(lite_client, cxname, payload, {"X-CCBoard-Agent": "codex"})
    assert r.json()["behavior"] is None and r.json().get("reason") != "foreign" and "id" in r.json()
    assert row_of(cxname)["state"] == "waiting" and "PermissionRequest" in event_names(cxname)
    # and a request without any agent header (an older bin/ccboard-permission) keeps working on a Claude row
    r = _hook_perm(lite_client, claude["tmux"], payload)
    assert r.json().get("reason") != "foreign" and "id" in r.json()


def test_a_startup_session_start_with_a_new_id_rebinds_an_idle_row_and_is_a_subthread_while_working(cxs):
    """Defect 10 (the prepared fallback): a build that reports `startup` for the first SessionStart of a /new or `codex fork` thread must
    not freeze the row on the old id; a guardian or subagent thread starts mid-turn and still counts as a sub-thread."""
    cx_hook(cxs, "SessionStart", source="startup")
    assert row_of(cxs.name)["state"] == "idle" and row_of(cxs.name)["claude_session_id"] == CXA
    out = cx_hook(cxs, "SessionStart", source="startup", session_id=CXB)                     # /new on an idle row
    assert out["state"] == "idle" and "ignored" not in out and row_of(cxs.name)["claude_session_id"] == CXB
    assert cx_hook(cxs, "UserPromptSubmit", prompt="next turn", session_id=CXB)["state"] == "working"
    out = cx_hook(cxs, "SessionStart", source="startup", session_id=CXC)                      # a guardian thread, mid-turn
    assert out["ignored"] == "subthread" and row_of(cxs.name)["claude_session_id"] == CXB and row_of(cxs.name)["flags"]["subthreads"] == 1
    assert row_of(cxs.name)["state"] == "working"
    assert cx_hook(cxs, "Stop", last_assistant_message="done", session_id=CXB)["state"] == "done"
    out = cx_hook(cxs, "SessionStart", source="startup", session_id=CXA)                      # a fork on a finished row
    assert out["state"] == "idle" and row_of(cxs.name)["claude_session_id"] == CXA and row_of(cxs.name)["flags"]["subthreads"] == 1
    cx_hook(cxs, "SessionEnd", reason="other", session_id=CXA)
    assert row_of(cxs.name)["state"] == "ended"


def test_resolve_session_by_the_payload_session_id_when_the_env_is_gone(cxs, projects_dir):
    cx_hook(cxs, "SessionStart", source="startup")
    second = second_row(cxs.client)
    out = _hook(cxs.client, {"hook_event_name": "UserPromptSubmit", "session_id": CXA, "prompt": "no env here"},
                extra={"X-CCBoard-Agent": "codex"}).json()
    assert out["session"] == cxs.name and out["how"] == "session_id" and out["state"] == "working"
    # the env header still wins over the id; an unknown or malformed id is not a match; two open owners of one id are not either
    assert _hook(cxs.client, {"hook_event_name": "UserPromptSubmit", "session_id": CXA, "prompt": "x"}, session=second).json()["ignored"] == "foreign"
    assert _hook(cxs.client, {"hook_event_name": "Stop", "session_id": CXB}).json()["ignored"] == "unresolved"
    assert _hook(cxs.client, {"hook_event_name": "Stop", "session_id": ["x"]}).json()["ignored"] == "unresolved"
    with main.db.lock:
        main.db.conn.execute("UPDATE sessions SET claude_session_id=? WHERE tmux_name=?", (CXA, second))
    assert _hook(cxs.client, {"hook_event_name": "Stop", "session_id": CXA}).json()["ignored"] == "unresolved"


def test_hook_cwd_matches_a_rows_own_launch_directory(lite_client, projects_dir, fake_tmux):
    git_init(projects_dir / "shop" / "api")
    wt = projects_dir / "shop" / "api" / ".ccboard" / "worktrees" / "job"
    wt.mkdir(parents=True)
    name = second_row(lite_client)
    with main.db.lock:
        main.db.conn.execute("UPDATE sessions SET cwd=? WHERE tmux_name=?", (str(wt), name))
    out = _hook(lite_client, {"hook_event_name": "UserPromptSubmit", "prompt": "x", "cwd": str(wt)}).json()
    assert out["session"] == name and out["how"] == "cwd"
    # the repo path itself still matches (the row's cwd is not the only candidate)
    out = _hook(lite_client, {"hook_event_name": "UserPromptSubmit", "prompt": "y", "cwd": str(projects_dir / "shop" / "api")}).json()
    assert out["session"] == name and out["how"] == "cwd"


def test_the_rollout_bind_is_a_seam_that_binds_nothing_until_v0512(monkeypatch):
    from app import agents
    assert hooks.bind_unbound_rows(main.db) == 0
    monkeypatch.setattr(agents.codex, "bind_unbound_rows", lambda db: 3)
    assert hooks.bind_unbound_rows(object()) == 3
    monkeypatch.setattr(agents.codex, "bind_unbound_rows", lambda db: 1 / 0)
    assert hooks.bind_unbound_rows(object()) == 0, "a failing Tailer step never reaches the hook path"
