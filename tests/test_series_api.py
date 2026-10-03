"""The writers and the HTTP surface of the samples backbone (v0.5.4 commit B, the slice that wires samples.py into the board).

GET /api/series, /api/series/events and /api/usage/summary go through lite_client against samples put in with DB.sample at known
times; the endpoints read one clock, main._utcnow, which the `board` fixture freezes, so no test sleeps and no bucket depends on when
it runs. The series and downsampling maths themselves belong to tests/test_series.py: here only validation, the response shape, the
cache header and the delegation to usage_summary. Below them: the lifespan wiring (state samples from DB.on_state_change, the counts
and health sources of the Sampler, the thread's start and stop) and cost.refresh feeding the 'cost' series. The hook-path writers
(statusline, ev, lim) are in tests/test_hooks.py and the doctor's heartbeat check in tests/test_doctor.py.
"""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from app import cost, health, hooks, main, samples, usage_summary

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
NOW = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)
SESSION_A = "shop--api--s1"
SESSION_B = "shop--api--s2"


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def ago(**kw) -> str:
    return iso(NOW - timedelta(**kw))


@pytest.fixture
def board(lite_client, monkeypatch):
    monkeypatch.setattr(main, "_utcnow", lambda: NOW)
    return lite_client


def put(series, key, value, at, meta=None):
    main.db.sample(series, key, value, meta, at=at)


def get(board, path, **params):
    return board.get(path, headers=H, params=params)


def nonnull(values):
    return [v for v in values if v is not None]


# ------------------------------------------------------------------ /api/series

def test_series_shape_values_and_cache_header(board):
    for h, (a, b) in zip((3, 2, 1), ((10.0, 40.0), (20.0, 50.0), (30.0, 60.0))):
        put("rl_5h", "claude", a, ago(hours=h), {"resets_at": 1790000000})
        put("rl_7d", "claude", b, ago(hours=h))
    r = get(board, "/api/series", series="rl_5h,rl_7d", key="claude", since="6h")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private, max-age=10"
    body = r.json()
    assert {"since", "until", "step", "t", "series", "meta"} <= set(body)
    assert set(body["series"]) == {"rl_5h:claude", "rl_7d:claude"}
    n = len(body["t"])
    assert n > 1 and n <= 500 and body["step"] > 0
    assert all(len(v) == n for v in body["series"].values())
    assert body["t"] == sorted(body["t"]) and all(isinstance(t, (int, float)) for t in body["t"])
    assert nonnull(body["series"]["rl_5h:claude"]) == [10.0, 20.0, 30.0]            # hourly samples never share a bucket
    assert nonnull(body["series"]["rl_7d:claude"]) == [40.0, 50.0, 60.0]
    assert body["meta"]["rl_5h:claude"] == {"resets_at": 1790000000}                 # the newest sample's meta
    assert datetime.fromisoformat(body["until"]) > datetime.fromisoformat(body["since"])


def test_series_window_excludes_older_samples_and_until_cuts_the_end(board):
    put("rl_5h", "claude", 5.0, ago(hours=30))
    put("rl_5h", "claude", 15.0, ago(hours=3))
    put("rl_5h", "claude", 25.0, ago(hours=1))
    body = get(board, "/api/series", series="rl_5h", key="claude", since="24h").json()
    assert nonnull(body["series"]["rl_5h:claude"]) == [15.0, 25.0]
    body = get(board, "/api/series", series="rl_5h", key="claude", since=ago(hours=6), until=ago(hours=2)).json()
    assert nonnull(body["series"]["rl_5h:claude"]) == [15.0]
    assert body["until"].startswith("2026-10-03T10:00:00")


def test_series_key_star_and_missing_key_return_every_key(board):
    put("ctx", SESSION_A, 40.0, ago(hours=2))
    put("ctx", SESSION_B, 70.0, ago(hours=2))
    for params in ({"key": "*"}, {}):
        body = get(board, "/api/series", series="ctx", since="6h", **params).json()
        assert set(body["series"]) == {f"ctx:{SESSION_A}", f"ctx:{SESSION_B}"}
    body = get(board, "/api/series", series="ctx", key=SESSION_B, since="6h").json()
    assert set(body["series"]) == {f"ctx:{SESSION_B}"}


def test_series_empty_window_is_a_well_formed_empty_answer(board):
    r = get(board, "/api/series", series="rl_5h", key="claude", since="1h")
    assert r.status_code == 200
    body = r.json()
    assert all(v is None for vs in body["series"].values() for v in vs)
    assert len(body["t"]) <= 500


@pytest.mark.parametrize("params", [
    {},                                                                  # no series
    {"series": ""},
    {"series": "nope"},                                                  # unknown
    {"series": "rl_5h,nope", "key": "claude"},                           # one unknown among known
    {"series": "ctx,ctx_tok,scost", "key": "a,b,c"},                     # 9 combos
    {"series": "ctx,ctx_tok", "key": "a,b,c,d,e"},                       # 10 combos
    {"series": "rl_5h,rl_7d,ctx,ctx_tok,scost,state,ev,cost,h_cpu"},     # nine series, every key
    {"series": "rl_5h", "since": "yesterday"},
    {"series": "rl_5h", "since": "24h", "until": "soon"},
    {"series": "rl_5h", "since": "1h", "until": "6h"},                   # since after until
    {"series": "rl_5h", "points": "many"},
])
def test_series_rejects_bad_requests(board, params):
    r = get(board, "/api/series", **params)
    assert r.status_code == 400, params
    assert r.json()["error"]


def test_series_eight_combos_is_the_limit(board):
    put("ctx", "a", 1.0, ago(hours=1))
    assert get(board, "/api/series", series="ctx,ctx_tok", key="a,b,c,d", since="6h").status_code == 200       # 2 x 4
    assert get(board, "/api/series", series="ctx", key="a,b,c,d,e,f,g,h", since="6h").status_code == 200       # 1 x 8


def test_series_points_are_clamped_not_refused(board):
    put("ctx", SESSION_A, 40.0, ago(hours=2))
    body = get(board, "/api/series", series="ctx", key=SESSION_A, since="90d", points="100000").json()
    assert len(body["t"]) <= 500
    body = get(board, "/api/series", series="ctx", key=SESSION_A, since="6h", points="2").json()
    assert 1 <= len(body["t"]) <= 10


def test_series_needs_an_identity(board):
    assert board.get("/api/series?series=rl_5h").status_code == 403


# ------------------------------------------------------------------ /api/series/events

def test_series_events_shape_filters_and_since(board):
    put("state", SESSION_A, 1, ago(hours=5), {"p": "shop", "r": "api", "s": "s1", "a": "claude"})
    put("state", SESSION_A, 3, ago(hours=4, minutes=30), {"p": "shop", "r": "api", "s": "s1", "a": "claude"})
    put("state", SESSION_B, 2, ago(hours=3), {"p": "shop", "r": "api", "s": "s2", "a": "claude"})
    put("state", SESSION_A, 0, ago(hours=30))                                    # outside the 24 h default
    r = get(board, "/api/series/events", series="state")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"events", "truncated"} and body["truncated"] is False
    assert [(e["key"], e["v"]) for e in body["events"]] == [(SESSION_A, 1), (SESSION_A, 3), (SESSION_B, 2)]
    first = body["events"][0]
    assert set(first) == {"t", "key", "v", "m"} and first["m"]["p"] == "shop" and first["m"]["a"] == "claude"
    assert [e["t"] for e in body["events"]] == sorted(e["t"] for e in body["events"])
    only_b = get(board, "/api/series/events", series="state", key=SESSION_B).json()
    assert [e["key"] for e in only_b["events"]] == [SESSION_B]
    recent = get(board, "/api/series/events", series="state", since="4h").json()
    assert [e["key"] for e in recent["events"]] == [SESSION_B]
    wide = get(board, "/api/series/events", series="state", since="7d").json()
    assert len(wide["events"]) == 4


def test_series_events_lim(board):
    put("lim", "5h", 1, ago(hours=2), {"session": SESSION_A, "resets_at": 1790000000, "message": "You've hit your session limit"})
    put("lim", "7d", 1, ago(hours=1), {"session": SESSION_B, "resets_at": 1790500000, "message": "weekly"})
    body = get(board, "/api/series/events", series="lim", since="24h").json()
    assert [e["key"] for e in body["events"]] == ["5h", "7d"]
    assert body["events"][0]["m"]["resets_at"] == 1790000000
    assert [e["key"] for e in get(board, "/api/series/events", series="lim", key="7d").json()["events"]] == ["7d"]


def test_series_events_default_series_is_state(board):
    put("state", SESSION_A, 1, ago(hours=1))
    assert len(get(board, "/api/series/events").json()["events"]) == 1


@pytest.mark.parametrize("params", [
    {"series": "rl_5h"},                      # a value series, not events
    {"series": "ctx"},
    {"series": "nope"},
    {"series": "state,lim"},                  # one at a time
    {"series": "state", "since": "whenever"},
])
def test_series_events_rejects_bad_requests(board, params):
    assert get(board, "/api/series/events", **params).status_code == 400, params


# ------------------------------------------------------------------ /api/usage/summary

@pytest.fixture
def built(board, monkeypatch):
    calls = []

    def fake_build(db, days=30, tz_min=345, now=None):
        calls.append({"db": db, "days": days, "tz_min": tz_min, "now": now})
        return {"source": "samples", "days": days, "tz_min": tz_min, "marker": "from-usage_summary"}

    monkeypatch.setattr(usage_summary, "build", fake_build)
    return board, calls


def test_usage_summary_delegates_with_the_plan_defaults(built):
    board, calls = built
    r = get(board, "/api/usage/summary")
    assert r.status_code == 200 and r.json() == {"source": "samples", "days": 30, "tz_min": 345, "marker": "from-usage_summary"}
    assert len(calls) == 1
    assert calls[0]["db"] is main.db and (calls[0]["days"], calls[0]["tz_min"]) == (30, 345)
    assert calls[0]["now"] == NOW


def test_usage_summary_passes_days_and_zone_through(built):
    board, calls = built
    assert get(board, "/api/usage/summary", days="7", tz_min="-300").json()["tz_min"] == -300
    assert (calls[-1]["days"], calls[-1]["tz_min"]) == (7, -300)
    assert get(board, "/api/usage/summary", days="90", tz_min="0").status_code == 200
    assert (calls[-1]["days"], calls[-1]["tz_min"]) == (90, 0)


@pytest.mark.parametrize("params", [
    {"days": "0"}, {"days": "-3"}, {"days": "abc"}, {"days": "3.5"}, {"days": "9999"},
    {"tz_min": "x"}, {"tz_min": "99999"}, {"tz_min": "-99999"}, {"tz_min": "5.75"},
])
def test_usage_summary_rejects_bad_days_and_tz(built, params):
    board, calls = built
    r = get(board, "/api/usage/summary", **params)
    assert r.status_code == 400 and r.json()["error"], params
    assert calls == []                                                   # never reached the builder


def test_usage_summary_answers_from_an_empty_samples_table(board):
    """No patch: the real builder over a fresh database returns its documented shape without error."""
    r = get(board, "/api/usage/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "samples" and body["tz_min"] == 345
    assert {"windows", "daily", "top_sessions"} <= set(body)


def test_catalogue_covers_every_series_the_endpoints_accept():
    assert {"rl_5h", "rl_7d", "ctx", "ctx_tok", "scost", "state", "ev", "cost", "h_cpu", "h_mem", "h_load", "h_disk",
            "n_live", "n_work", "n_attn", "lim"} <= set(samples.CATALOGUE)
    assert {n for n, spec in samples.CATALOGUE.items() if spec["agg"] == "events"} >= {"state", "lim"}


# ------------------------------------------------------------------ lifespan wiring: state samples, counts, the Sampler

EPOCH0 = "2000-01-01T00:00:00+00:00"


def _git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)


def _rows(series, key=None):
    return [(at, k, v, json.loads(m) if m else None)
            for at, k, v, m in main.db.samples_query(series, None if key is None else [key], EPOCH0, None)]


def _new_session(client, launcher="shell"):
    return client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": launcher}).json()["tmux"]


def _hook(client, name, payload, event=None):
    hdr = {"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard-Session": name}
    if event:
        hdr["X-CCBoard-Event"] = event
    r = client.post("/api/hook", headers=hdr, content=json.dumps(payload))
    assert r.status_code == 200 and "ignored" not in r.json(), r.text
    return r.json()


class Clock:
    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t


@pytest.fixture
def wired(lite_client, projects_dir, fake_tmux):
    """The lite board with what the lifespan wires on top: the state sampler on the DB (lite_lifespan skips it)."""
    _git_init(projects_dir / "shop" / "api")
    main.db.on_state_change = main._state_sampler(main.db)
    return lite_client


def test_state_changes_are_sampled_once_each_with_the_row_meta(wired, fake_tmux):
    name = _new_session(wired)
    assert _rows("state") == []                                              # creating a row is not a state change
    _hook(wired, name, {"hook_event_name": "UserPromptSubmit", "prompt": "go"})
    _hook(wired, name, {"hook_event_name": "UserPromptSubmit", "prompt": "again"})      # same state: nothing
    _hook(wired, name, {"hook_event_name": "Stop"})
    assert wired.delete(f"/api/sessions/{name}", headers=H).status_code == 200            # db.end -> 'ended'
    got = _rows("state", name)
    assert [r[2] for r in got] == [1, 3, 5]                                   # working, done, ended
    p, r, s = name.split("--")
    assert got[0][3] == {"p": p, "r": r, "s": s, "a": "shell"} and got[-1][3]["a"] == "shell"
    assert all(k == name for _, k, _, _ in got)


def test_reconcile_closes_dead_sessions_and_samples_ended(wired, fake_tmux):
    name = _new_session(wired)
    _hook(wired, name, {"hook_event_name": "UserPromptSubmit", "prompt": "go"})
    main.db.conn.execute("UPDATE sessions SET created_at='2020-01-01T00:00:00+00:00'")      # older than the tmux snapshot
    fake_tmux["sessions"].pop(name)                                                          # tmux lost it (reboot, manual kill)
    sessions, down = main._merged_sessions()
    assert sessions == {} and down is False
    assert [r[2] for r in _rows("state", name)] == [1, 5]


def test_state_sampler_fills_the_meta_from_the_name_and_skips_internal_sessions(lite_client):
    hook = main._state_sampler(main.db)
    hook("shop--api--s9", None, "idle", "SessionStart", None)                                # a closed row may be all the DB has
    hook("_ccboard-probe", None, "working", "x", {"project": "shop"})
    hook("not-a-ccboard-name", None, "working", "x", None)                                   # unparsable name: no project, still no crash
    assert _rows("state", "shop--api--s9")[0][3] == {"p": "shop", "r": "api", "s": "s9", "a": "claude"}
    assert _rows("state", "_ccboard-probe") == []
    assert [r[2] for r in _rows("state", "not-a-ccboard-name")] == [1]
    hook("shop--api--s9", "idle", "idle", "x", {})                                          # not a change: record_state drops it
    assert len(_rows("state", "shop--api--s9")) == 1


def test_sampler_counts_are_live_working_and_needing_attention(wired, fake_tmux):
    a, b = _new_session(wired), _new_session(wired)
    assert main._sampler_counts() == {"live": 2, "work": 0, "attn": 0}
    _hook(wired, a, {"hook_event_name": "UserPromptSubmit", "prompt": "go"})
    _hook(wired, b, {"hook_event_name": "Stop"})
    assert main._sampler_counts() == {"live": 2, "work": 1, "attn": 1}
    wired.post(f"/api/sessions/{b}/ack", headers=H)
    assert main._sampler_counts() == {"live": 2, "work": 1, "attn": 0}
    fake_tmux["sessions"]["_ccboard-probe"] = dict(fake_tmux["sessions"][a])                 # internal sessions are not counted
    assert main._sampler_counts()["live"] == 2


def test_sampler_counts_skip_the_tick_when_tmux_errors(lite_client, monkeypatch):
    def broken():
        raise main.tmux.TmuxError("tmux is not installed")

    monkeypatch.setattr(main.tmux, "list_sessions", broken)
    assert main._sampler_counts() == {}                       # record_counts({}) writes nothing: no false zero


def test_sampler_health_asks_for_its_own_cpu_delta(monkeypatch):
    seen = []
    monkeypatch.setattr(health, "snapshot", lambda extra=None, consumer="default": seen.append(consumer) or {"host": "box"})
    assert main._sampler_health() == {"host": "box"} and seen == ["sampler"]


def test_sampler_tick_with_the_boards_sources(wired, fake_tmux, monkeypatch):
    a = _new_session(wired)
    _hook(wired, a, {"hook_event_name": "UserPromptSubmit", "prompt": "go"})
    monkeypatch.setattr(health, "snapshot", lambda extra=None, consumer="default": {
        "host": "ubu2", "cpu_pct": 12.5, "load1": 0.4, "mem": {"pct": 55.0}, "disk": {"pct": 61.0}})
    clock = Clock(1_790_000_000.0)
    sm = samples.Sampler(main.db, health_fn=main._sampler_health, counts_fn=main._sampler_counts, clock=clock)
    out = sm.sample_once()
    assert set(out["wrote"]) == {"h_cpu", "h_mem", "h_load", "h_disk", "n_live", "n_work", "n_attn"}
    assert [r[2] for r in _rows("n_live", "")] == [1] and [r[2] for r in _rows("n_work", "")] == [1]
    assert [r[2] for r in _rows("h_cpu", "ubu2")] == [12.5]
    assert main.db.kv_get("samples_heartbeat")["value"]["ts"] == clock.t
    clock.t += 15                                                            # next tick: every throttle still holds
    assert sm.sample_once()["wrote"] == []


def test_the_lifespan_starts_the_sampler_wires_the_state_hook_and_stops_the_thread(client, projects_dir, fake_tmux):
    assert main.db.on_state_change is not None
    sm = main.sampler
    assert isinstance(sm, samples.Sampler) and sm.is_alive()
    sm.sample_once()                                                         # the thread's own first tick races the test: tick here
    assert main.db.kv_get("samples_heartbeat") is not None and main.db.kv_get("samples_pruned_at") is not None
    _git_init(projects_dir / "shop" / "api")
    name = _new_session(client)
    _hook(client, name, {"hook_event_name": "UserPromptSubmit", "prompt": "go"})
    assert [r[2] for r in _rows("state", name)] == [1]
    state = client.get("/api/doctor?group=box&refresh=1", headers=H).json()
    hb = next(c for c in state["checks"] if c["id"] == "samples-heartbeat")
    assert hb["status"] == "pass"                                            # the doctor reads what the Sampler wrote


def test_the_sampler_thread_ends_with_the_lifespan(projects_dir, fake_tmux):
    from fastapi.testclient import TestClient
    with TestClient(main.app):
        sm = main.sampler
        assert sm.is_alive()
    assert not sm.is_alive()                                                 # sampler.stop() joined it on shutdown


# ------------------------------------------------------------------ cost.refresh -> the 'cost' series

U1, U2, U3 = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
              "33333333-3333-4333-8333-333333333333")


def ccusage(*rows):
    return {"session": [{"period": sid, "totalCost": c, "totalTokens": t, "modelsUsed": models,
                         "metadata": {"lastActivity": iso(last)}} for sid, c, t, models, last in rows]}


@pytest.fixture
def priced(lite_client, monkeypatch):
    """cost.refresh over a fake ccusage at a frozen clock: U1 belongs to a board session (shop/api), U2 was started elsewhere,
    U3 has tokens and no price. `state['now']` moves the clock, `state['raw']` is what ccusage prints."""
    main.db.add_session(tmux_name="shop--api--s1", project="shop", repo="api", name="s1", launcher="claude", claude_session_id=U1)
    state = {"now": NOW, "raw": ccusage((U1, 2.5, 1000, ["claude-opus-5-5"], NOW - timedelta(hours=3)),
                                        (U2, 0.75, 400, ["claude-sonnet-5-5", "claude-haiku-4-5"], NOW - timedelta(hours=30)),
                                        (U3, 0.0, 9000, ["claude-fable-5-1"], NOW - timedelta(hours=1)))}
    monkeypatch.setattr(cost, "fetch_sessions", lambda: cost.parse_sessions(json.dumps(state["raw"])))
    monkeypatch.setattr(cost, "_now", lambda: state["now"])
    return state


def test_refresh_writes_one_cumulative_cost_sample_per_session(priced):
    res = cost.refresh(main.db)
    assert res["sessions_known"] == 3
    rows = {k: (at, v, m) for at, k, v, m in _rows("cost")}
    assert set(rows) == {f"claude:{U1}", f"claude:{U2}", f"claude:{U3}"}
    at, v, m = rows[f"claude:{U1}"]
    assert v == 2.5 and m == {"p": "shop", "r": "api", "a": "claude", "tok": 1000, "m": ["claude-opus-5-5"]}
    assert at == ago(hours=3)                                                # first sight is stamped at the session's lastActivity
    assert rows[f"claude:{U2}"][2] == {"a": "claude", "tok": 400, "m": ["claude-sonnet-5-5", "claude-haiku-4-5"]}     # not the board's
    assert rows[f"claude:{U2}"][0] == ago(hours=30)
    assert rows[f"claude:{U3}"][1] == 0.0


def test_refresh_writes_only_changes_and_at_most_every_ten_minutes(priced):
    cost.refresh(main.db)
    priced["now"] = NOW + timedelta(minutes=20)
    cost.refresh(main.db)                                                    # nothing changed: no rows
    assert len(_rows("cost")) == 3
    priced["raw"] = ccusage((U1, 3.0, 1500, ["claude-opus-5-5"], NOW), (U2, 0.75, 400, [], NOW - timedelta(hours=30)),
                            (U3, 0.0, 9000, [], NOW - timedelta(hours=1)))
    cost.refresh(main.db)                                                    # U1 spent more, and its last row is hours old
    assert [(at, v) for at, _, v, _ in _rows("cost", f"claude:{U1}")] == [(ago(hours=3), 2.5), (iso(priced["now"]), 3.0)]
    priced["raw"] = ccusage((U1, 3.4, 1700, ["claude-opus-5-5"], NOW), (U2, 0.75, 400, [], NOW - timedelta(hours=30)),
                            (U3, 0.0, 9000, [], NOW - timedelta(hours=1)))
    priced["now"] += timedelta(minutes=5)
    cost.refresh(main.db)                                                    # moved again but only 5 minutes later: held back
    assert len(_rows("cost", f"claude:{U1}")) == 2
    priced["now"] += timedelta(minutes=6)
    cost.refresh(main.db)
    assert [v for _, _, v, _ in _rows("cost", f"claude:{U1}")] == [2.5, 3.0, 3.4]
    assert len(_rows("cost", f"claude:{U2}")) == 1 and len(_rows("cost", f"claude:{U3}")) == 1


def test_refresh_names_unpriced_sessions_in_the_kv_record_not_the_samples(priced):
    cost.refresh(main.db)
    un = main.db.kv_get("cost")["value"]["unpriced"]
    assert un["sessions"] == 1 and un["tokens"] == 9000
    assert un["top"] == [{"id": U3, "agent": "claude", "project": None, "repo": None, "tokens": 9000, "models": ["claude-fable-5-1"]}]
    assert "sessions" not in main.db.kv_get("cost")["value"]                 # the per-session list never reaches the polled kv record


def test_session_samples_join_board_sessions_and_keep_the_rest():
    costs = cost.parse_sessions(json.dumps(ccusage((U1, 1.0, 10, ["m"], NOW), (U2, 2.0, 20, [], NOW))))
    out = cost.session_samples(costs, [{"project": "shop", "repo": "api", "claude_session_id": U1.upper()},
                                       {"project": "dup", "repo": "dup", "claude_session_id": U1}])
    by = {e["id"]: e for e in out}
    assert by[U1]["project"] == "shop" and by[U1]["repo"] == "api" and by[U1]["models"] == ["m"] and by[U1]["agent"] == "claude"
    assert by[U2]["project"] is None and by[U2]["repo"] is None and by[U2]["cost"] == 2.0 and by[U2]["tokens"] == 20


def test_unpriced_ranks_by_tokens_and_caps_the_list():
    entries = [{"id": f"s{i}", "agent": "claude", "cost": 0.0, "tokens": i + 1, "project": None, "repo": None, "models": []}
               for i in range(30)]
    entries.append({"id": "paid", "agent": "claude", "cost": 1.0, "tokens": 10**6, "project": None, "repo": None, "models": []})
    entries.append({"id": "empty", "agent": "claude", "cost": 0.0, "tokens": 0, "project": None, "repo": None, "models": []})
    u = cost.unpriced(entries)
    assert u["sessions"] == 30 and u["tokens"] == sum(range(1, 31))
    assert len(u["top"]) == cost.UNPRICED_TOP == 20 and u["top"][0]["id"] == "s29"
    assert cost.unpriced([]) == {"sessions": 0, "tokens": 0, "top": []}


def test_a_failing_cost_sampler_never_fails_the_refresh(priced, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(samples, "record_cost", boom)
    res = cost.refresh(main.db)
    assert res["sessions_known"] == 3 and main.db.kv_get("cost")["value"]["sessions_known"] == 3
