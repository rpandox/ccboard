"""The samples backbone (v0.5.4 commit B, core): the samples table and DB methods, the state-change hook, the series catalogue with its
throttles, every writer, the Sampler thread, and the read-side helpers behind /api/series and /api/series/events.

Everything time-driven goes through an explicit `at=` or an injected clock and nothing sleeps. The only wall-clock reads are the
`now` defaults (parse_since, series_payload with until=None) and the one real-thread Sampler test, each with a generous tolerance."""
import io
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import accounts, health, samples
from app.config import settings
from app.db import DB, iso

UTC = timezone.utc
T0 = datetime(2026, 10, 3, 6, 0, 0, tzinfo=UTC)          # 11:45 in Asia/Kathmandu


def T(sec: float = 0) -> datetime:
    return T0 + timedelta(seconds=sec)


def I(sec: float = 0) -> str:
    return T(sec).isoformat(timespec="seconds")


def rec(db, series, key, value, sec, **kw):
    return samples.record(db, series, key, value, at=T(sec), **kw)


@pytest.fixture(autouse=True)
def _no_real_identity(tmp_path, monkeypatch):
    """The Sampler's tick runs accounts.observe: keep it off the developer's real ~/.claude.json (a bare DB test never sets the config dir)."""
    monkeypatch.setattr(settings, "claude_config_dir", tmp_path / "no-claude-config")
    accounts.invalidate()
    yield
    accounts.invalidate()


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "samples.db")


def rows_of(db, series, key=None):
    return [(r[0], r[1], r[2]) for r in db.samples_query(series, None if key is None else [key], "2000-01-01T00:00:00+00:00")]


def new_row(db, name="shop--api--s1", agent="claude", launcher="claude", repo="api"):
    db.add_session(tmux_name=name, project=name.split("--")[0], repo=repo, name="s1", launcher=launcher, agent=agent)
    return name


# ================================================================== table and DB methods
def test_table_and_indexes_are_created_idempotently(tmp_path):
    p = tmp_path / "x.db"
    DB(p)
    db = DB(p)                                             # a second start over the same file is a no-op
    cols = {r[1]: (r[2], r[3], r[4]) for r in db.conn.execute("PRAGMA table_info(samples)")}
    assert cols == {"id": ("INTEGER", 0, None), "series": ("TEXT", 1, None), "key": ("TEXT", 1, "''"),
                    "at": ("TEXT", 1, None), "value": ("REAL", 0, None), "meta": ("TEXT", 0, None)}
    idx = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"samples_series_key_at", "samples_at"} <= idx


def test_iso_normalises_every_form_to_utc_seconds():
    want = "2026-10-03T06:00:00+00:00"
    for v in (T0, T0.replace(tzinfo=None), T0.timestamp(), int(T0.timestamp()), "2026-10-03T06:00:00Z", "2026-10-03T06:00:00.987Z",
              "2026-10-03T11:45:00+05:45", "2026-10-03T06:00:00", " 2026-10-03T06:00:00+00:00 "):
        assert iso(v) == want, v
    for bad in ("", "yesterday", True, [1], {}):
        with pytest.raises(ValueError):
            iso(bad)


def test_sample_roundtrip_meta_and_last(db):
    assert db.sample_last("ctx", "a") is None
    i1 = db.sample("ctx", "a", 41.5, {"model": "opus", "window": 200000}, at=T(0))
    i2 = db.sample("ctx", "a", 50, at=T(10))
    assert i2 > i1
    assert db.sample_last("ctx", "a") == {"at": I(10), "value": 50.0, "meta": None}
    db.sample("ctx", "a", 60, {"model": "x"}, at=T(20))
    assert db.sample_last("ctx", "a") == {"at": I(20), "value": 60.0, "meta": {"model": "x"}}
    assert db.sample_last("ctx", "b") is None
    q = db.samples_query("ctx", ["a"], T(0))
    assert q[0] == (I(0), "a", 41.5, '{"model":"opus","window":200000}') and q[1][3] is None
    db.sample("ctx", "", 1, at=T(30))                       # key '' is a key like any other (n_live, ...)
    assert db.sample_last("ctx", "")["value"] == 1.0 and db.sample_last("ctx", None)["value"] == 1.0
    db.sample("ctx", "m", 1, "plain text meta", at=T(30))   # text meta passes through
    assert db.samples_query("ctx", ["m"], T(0))[0][3] == "plain text meta" and db.sample_last("ctx", "m")["meta"] is None


class Spy:
    """Counts statements sent through a sqlite connection (sqlite3.Connection attributes are read-only, so wrap it)."""
    def __init__(self, conn):
        self.conn, self.sql = conn, []

    def execute(self, sql, *a):
        self.sql.append(sql)
        return self.conn.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self.conn, name)


def test_sample_last_is_cached_and_kept_current_by_writes(tmp_path):
    p = tmp_path / "c.db"
    d1 = DB(p)
    d1.sample("scost", "t", 1.0, at=T(0))
    db = DB(p)                                              # a fresh process: the cache starts empty
    spy = Spy(db.conn)
    db.conn = spy
    assert db.sample_last("scost", "t")["value"] == 1.0
    assert db.sample_last("scost", "t")["value"] == 1.0
    assert len([s for s in spy.sql if "FROM samples" in s]) == 1, "the second read comes from memory"
    db.sample("scost", "t", 2.0, at=T(10))
    assert db.sample_last("scost", "t") == {"at": I(10), "value": 2.0, "meta": None}
    assert len([s for s in spy.sql if "SELECT" in s]) == 1, "a write refreshes the cache without a query"
    db.sample("scost", "t", 0.5, at=T(5))                   # back-dated: the newest row stays the last one
    assert db.sample_last("scost", "t")["value"] == 2.0
    db.sample("scost", "fresh", 9.0, at=T(0))               # a key that was never read stays uncached ...
    db.sample("scost", "fresh", 8.0, at=T(-100))            # ... so a back-dated row cannot become its 'last'
    assert db.sample_last("scost", "fresh")["value"] == 9.0


def test_samples_query_ordering_filters_and_limits(db):
    for sec, key, v in ((30, "b", 3), (10, "a", 1), (20, "b", 2), (40, "a", 4), (50, "c", 5)):
        db.sample("ctx", key, v, at=T(sec))
    db.sample("other", "a", 99, at=T(15))
    got = db.samples_query("ctx", None, T(10), T(40))
    assert [(r[1], r[2]) for r in got] == [("a", 1), ("b", 2), ("b", 3), ("a", 4)], "oldest first, both ends inclusive"
    assert [r[1] for r in db.samples_query("ctx", ["b", "c"], T(0))] == ["b", "b", "c"]
    assert db.samples_query("ctx", [], T(0)) == []
    assert db.samples_query("ctx", None, T(51)) == []
    assert [r[2] for r in db.samples_query("ctx", None, T(0), limit=2)] == [1, 2], "limit keeps the oldest"
    assert [r[2] for r in db.samples_query("ctx", None, T(0), limit=2, newest=True)] == [4, 5], "newest=True keeps the newest, still oldest first"
    assert db.samples_query("ctx", None, "2026-10-03T06:00:10Z", until=T(10).timestamp())[0][2] == 1   # since/until take any time form


def test_samples_distinct_keys_most_recent_first(db):
    db.sample("ctx", "old", 1, at=T(0))
    db.sample("ctx", "mid", 1, at=T(50))
    db.sample("ctx", "new", 1, at=T(90))
    db.sample("ctx", "mid", 1, at=T(60))
    db.sample("ctx", "gone", 1, at=T(-500))
    db.sample("rl_5h", "claude", 1, at=T(70))
    assert db.samples_distinct_keys("ctx", T(-10)) == ["new", "mid", "old"]
    assert db.samples_distinct_keys("ctx", T(80)) == ["new"]
    assert db.samples_distinct_keys("nope", T(0)) == []


def test_samples_prune_by_retention_per_series(db):
    now = T(0)
    old = lambda days: now - timedelta(days=days)
    db.sample("ctx", "a", 1, at=old(20))                    # ctx keeps 14 d: gone
    db.sample("ctx", "a", 2, at=old(10))                    # kept
    db.sample("rl_5h", "claude", 1, at=old(20))             # rl keeps 90 d: kept
    db.sample("rl_5h", "claude", 2, at=old(100))            # gone
    db.sample("lim", "5h", 1, at=old(100))                  # lim keeps 180 d: kept
    db.sample("custom", "x", 1, at=old(1000))               # not in the map: never pruned
    assert db.sample_last("ctx", "a")["value"] == 2.0       # warm the cache before the prune
    assert db.samples_prune(samples.RETENTION, now=now) == 2
    assert [r[2] for r in rows_of(db, "ctx")] == [2.0] and [r[2] for r in rows_of(db, "rl_5h")] == [1.0]
    assert len(rows_of(db, "lim")) == 1 and len(rows_of(db, "custom")) == 1
    assert db.samples_prune(samples.RETENTION, now=now) == 0
    db.sample("ctx", "b", 5, at=old(30))
    db.sample_last("ctx", "b")
    assert db.samples_prune({"ctx": 14}, now=now) == 1
    assert db.sample_last("ctx", "b") is None, "the cache forgot the pruned row"
    assert db.samples_prune({"ctx": 14}, now=now.replace(tzinfo=None)) == 0     # a naive time counts as UTC


def test_sample_bump_counts_per_hour_in_place(db):
    a = db.sample_bump("ev", "shop", at=T(10))              # 06:00:10
    assert db.sample_bump("ev", "shop", at=T(1800)) == a    # same UTC hour: the same row
    assert db.sample_bump("ev", "shop", at=T(3590)) == a    # 06:59:50
    b = db.sample_bump("ev", "shop", at=T(3600))            # 07:00:00: a new row
    c = db.sample_bump("ev", "blog", at=T(3601))            # another project: its own row
    assert len({a, b, c}) == 3
    assert rows_of(db, "ev", "shop") == [(I(10), "shop", 3.0), (I(3600), "shop", 1.0)], "stamped with the hour's first event"
    assert rows_of(db, "ev", "blog") == [(I(3601), "blog", 1.0)]
    assert db.sample_last("ev", "shop") == {"at": I(3600), "value": 1.0, "meta": None}
    db.sample_bump("ev", "shop", at=T(3700), delta=4)
    assert db.sample_last("ev", "shop")["value"] == 5.0
    db.sample_bump("ev", "shop", at=T(5))                   # back in the first hour while a later row exists: a row of its own
    assert len(rows_of(db, "ev", "shop")) == 3


def test_sample_bump_loses_no_count_under_threads(db):
    errs = []

    def work():
        try:
            for _ in range(60):
                db.sample_bump("ev", "shop", at=T(100))
        except Exception as e:                              # pragma: no cover
            errs.append(e)
    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and rows_of(db, "ev", "shop") == [(I(100), "shop", 480.0)]


def test_sample_many_is_one_transaction(db):
    n = db.sample_many([("cost", f"claude:{i}", i * 1.5, {"p": "shop"}, T(i)) for i in range(5)])
    assert n == 5 and len(rows_of(db, "cost")) == 5 and db.sample_many([]) == 0
    with pytest.raises(ValueError):                         # an unreadable time: nothing of the batch lands
        db.sample_many([("cost", "ok", 1, None, T(0)), ("cost", "bad", 1, None, "not a time")])
    with pytest.raises(sqlite3.IntegrityError):             # a failure in the middle of the inserts rolls the batch back
        db.sample_many([("cost", "ok", 1, None, T(0)), (None, "bad", 1, None, T(0))])
    assert len(rows_of(db, "cost")) == 5
    assert not db.conn.in_transaction


def test_wal_checkpoint_runs_under_the_lock(db):
    db.sample("ctx", "a", 1)
    r = db.wal_checkpoint()
    assert r is None or len(r) == 3


# ================================================================== on_state_change
def calls_of(db):
    calls = []
    db.on_state_change = lambda *a: calls.append(a)
    return calls


def test_set_state_fires_only_on_a_real_change(db):
    name = new_row(db)
    calls = calls_of(db)
    db.set_state(name, "idle", "SessionStart")
    db.set_state(name, "idle", "SessionStart")              # same state again: silent
    db.set_state(name, None, "PreCompact")                  # state=None keeps the state: silent
    db.set_state(name, "working", "UserPromptSubmit", prompt="hi")
    db.set_state(name, "working", "UserPromptSubmit")
    db.set_state("nobody--x--y", "working", "UserPromptSubmit")    # no open row: nothing to report
    assert [(c[0], c[1], c[2], c[3]) for c in calls] == [(name, None, "idle", "SessionStart"),
                                                          (name, "idle", "working", "UserPromptSubmit")]
    row = calls[1][4]
    assert row["state"] == "working" and row["project"] == "shop" and row["repo"] == "api" and row["agent"] == "claude"
    assert row["name"] == "s1" and row["tmux_name"] == name and row["row_id"] and row["state_at"]
    assert db.open_row(name)["state"] == "working"


def test_state_callback_runs_outside_the_lock_and_never_breaks_the_write(db, caplog):
    name = new_row(db)
    seen = []

    def cb(tmux, old, new, event, row):
        assert db.lock.acquire(blocking=False), "the callback runs with the lock released"
        db.lock.release()
        samples.record_state(db, tmux, old, new, event, row, at=T(0))     # and may write through the same DB
        seen.append(new)
        raise RuntimeError("consumer bug")
    db.on_state_change = cb
    with caplog.at_level(logging.WARNING, logger="ccboard.db"):
        db.set_state(name, "working", "UserPromptSubmit")
        db.end(name, "killed")
    assert seen == ["working", "ended"] and db.open_row(name) is None
    assert sum("on_state_change" in r.message and "consumer bug" in r.message for r in caplog.records) == 2
    assert [r[2] for r in rows_of(db, "state")] == [1.0, 5.0]


def test_end_fires_once_per_name_with_the_reason(db):
    name = new_row(db)
    db.add_session(tmux_name=name, project="shop", repo="api", name="s1b", launcher="claude")   # two open rows, one name
    db.set_state(name, "working", "UserPromptSubmit")
    calls = calls_of(db)
    db.end(name, "auto_close")
    db.end(name, "killed")                                  # already closed: nothing
    db.end("ghost--x--y", "killed")
    assert len(calls) == 1
    n, old, new, event, row = calls[0]
    assert (n, old, new, event) == (name, "working", "ended", "auto_close")
    assert row["state"] == "ended" and row["ended_reason"] == "auto_close" and row["ended_at"]


def test_end_after_session_end_hook_is_silent(db):
    name = new_row(db)
    db.set_state(name, "working", "UserPromptSubmit")
    calls = calls_of(db)
    db.set_state(name, "ended", "SessionEnd")
    db.end(name, "exited")                                  # the row already said 'ended': no second event
    assert [(c[1], c[2]) for c in calls] == [("working", "ended")]
    shell = new_row(db, "shop--api--sh", launcher="shell", agent="shell")      # a shell row never had a state
    db.end(shell, "killed")
    assert (calls[1][1], calls[1][2], calls[1][4]["agent"]) == (None, "ended", "shell")


def test_reconcile_fires_for_each_row_it_closes(db):
    a, b, c = new_row(db, "shop--api--a"), new_row(db, "shop--api--b"), new_row(db, "shop--api--c")
    db.set_state(a, "working", "UserPromptSubmit")
    db.set_state(b, "idle", "SessionStart")
    db.set_state(c, "ended", "SessionEnd")
    calls = calls_of(db)
    db.reconcile({b}, before="9999-01-01T00:00:00+00:00")   # a and c are gone from tmux, b is alive
    assert [(x[0], x[1], x[2], x[3]) for x in calls] == [(a, "working", "ended", "reconciled")]
    assert calls[0][4]["ended_reason"] == "reconciled"
    db.reconcile(set(), before="2000-01-01T00:00:00+00:00")  # nothing was created before this snapshot
    assert len(calls) == 1 and db.open_row(b) is not None


def test_no_callback_no_cost(db):
    name = new_row(db)
    db.set_state(name, "working", "UserPromptSubmit")
    db.end(name)
    db.reconcile(set(), before="9999-01-01T00:00:00+00:00")
    assert db.on_state_change is None and db.open_row(name) is None


def test_session_ids_carry_the_agent(db):
    db.add_session(tmux_name="shop--api--s1", project="shop", repo="api", name="s1", launcher="claude", claude_session_id="abc")
    assert db.session_ids() == [{"project": "shop", "repo": "api", "claude_session_id": "abc", "agent": "claude"}]


# ================================================================== catalogue and record()
def test_catalogue_is_the_plan_table_plus_lim_acct_and_cacct():
    c = samples.CATALOGUE
    assert set(c) == {"rl_5h", "rl_7d", "ctx", "ctx_tok", "scost", "stok", "state", "ev", "cost", "h_cpu", "h_mem", "h_load",
                      "h_disk", "n_live", "n_work", "n_attn", "lim", "acct", "cacct", "mem_obs", "mem_sum"}   # mem_*: the Memory health tile (v0.5.20)
    assert all(set(v) == {"agg", "throttle", "retention_days"} and set(v["throttle"]) == {"delta", "seconds"} for v in c.values())
    assert {k: v["agg"] for k, v in c.items()} == {
        "rl_5h": "avg", "rl_7d": "avg", "ctx": "avg", "ctx_tok": "avg", "scost": "last", "stok": "last", "state": "events", "ev": "sum",
        "cost": "last", "h_cpu": "avg", "h_mem": "avg", "h_load": "avg", "h_disk": "avg", "n_live": "avg", "n_work": "avg",
        "n_attn": "avg", "lim": "events", "acct": "events", "cacct": "events", "mem_obs": "last", "mem_sum": "last"}
    assert {k: v["retention_days"] for k, v in c.items()} == {
        "rl_5h": 90, "rl_7d": 90, "ctx": 14, "ctx_tok": 14, "scost": 30, "stok": 30, "state": 90, "ev": 120, "cost": 120, "h_cpu": 14,
        "h_mem": 14, "h_load": 14, "h_disk": 30, "n_live": 30, "n_work": 30, "n_attn": 30, "lim": 180, "acct": 365, "cacct": 365,
        "mem_obs": 30, "mem_sum": 30}
    assert c["rl_5h"]["throttle"] == {"delta": 1, "seconds": 300} and c["ctx"]["throttle"] == {"delta": 0.5, "seconds": 900}
    assert c["scost"]["throttle"] == {"delta": 0.005, "seconds": None} and c["cost"]["throttle"] == {"delta": None, "seconds": 600}
    assert c["h_disk"]["throttle"]["seconds"] == 900 and c["h_cpu"]["throttle"]["seconds"] == 60 == c["n_work"]["throttle"]["seconds"]
    assert samples.RETENTION["lim"] == 180
    assert c["acct"]["throttle"] == {"delta": None, "seconds": None}, "an events series is never throttled"
    assert samples.RETENTION["acct"] >= samples.RETENTION["cost"], "the acct events must cover the cost history they attribute"


def test_throttle_delta_or_age_rl(db):
    assert rec(db, "rl_5h", "claude", 10, 0)
    assert not rec(db, "rl_5h", "claude", 10.6, 60), "moved 0.6 < 1 pt and only a minute old"
    assert rec(db, "rl_5h", "claude", 11.2, 120), "moved 1.2 from the last WRITTEN value (10): skipped samples do not move the baseline"
    assert not rec(db, "rl_5h", "claude", 11.2, 120 + 299)
    assert rec(db, "rl_5h", "claude", 11.2, 120 + 300), "unchanged but 5 minutes old: a heartbeat sample"
    assert rec(db, "rl_5h", "claude", 9.9, 120 + 301), "a drop counts too"
    assert rec(db, "rl_5h", "codex", 1, 0), "the throttle is per key"
    assert [r[2] for r in rows_of(db, "rl_5h", "claude")] == [10.0, 11.2, 11.2, 9.9]


def test_throttle_ctx_and_ctx_tok(db):
    assert rec(db, "ctx", "t", 20, 0) and not rec(db, "ctx", "t", 20.4, 10) and rec(db, "ctx", "t", 20.5, 10)
    assert not rec(db, "ctx", "t", 20.5, 909) and rec(db, "ctx", "t", 20.5, 910), "unchanged: one heartbeat every 15 min, not every 2"


def test_throttle_scost_is_delta_only(db):
    assert rec(db, "scost", "t", 0.10, 0)
    assert not rec(db, "scost", "t", 0.104, 5)
    assert not rec(db, "scost", "t", 0.10, 86400), "no time trigger: a flat cost never repeats"
    assert rec(db, "scost", "t", 0.105, 6), "0.005 apart despite float noise"
    assert rec(db, "scost", "t", 0.0, 7), "a drop (cost reset) is a move"


def test_throttle_stok_writes_on_change_only(db):
    assert rec(db, "stok", "t", 1000, 0) and not rec(db, "stok", "t", 1000, 99999) and rec(db, "stok", "t", 1001, 1)


def test_throttle_seconds_only_series(db):
    assert rec(db, "h_cpu", "box", 10, 0)
    assert not rec(db, "h_cpu", "box", 90, 59), "a big move inside the minute is still skipped"
    assert rec(db, "h_cpu", "box", 11, 60)
    assert rec(db, "h_disk", "box", 50, 0) and not rec(db, "h_disk", "box", 51, 899) and rec(db, "h_disk", "box", 50, 900)
    assert rec(db, "cost", "claude:x", 1, 0) and not rec(db, "cost", "claude:x", 2, 599) and rec(db, "cost", "claude:x", 2, 600)


def test_events_series_force_and_bad_values_bypass_or_drop(db):
    assert rec(db, "state", "t", 1, 0) and rec(db, "state", "t", 1, 0), "an events series is never throttled"
    assert rec(db, "lim", "5h", 1, 0) and rec(db, "lim", "5h", 1, 0)
    assert rec(db, "ctx", "t", 5, 0) and not rec(db, "ctx", "t", 5, 1) and rec(db, "ctx", "t", 5, 2, force=True)
    for bad in (None, float("nan"), float("inf"), True, "41", [1], {}):
        assert rec(db, "ctx", "bad", bad, 0) is False
    assert db.sample_last("ctx", "bad") is None
    with pytest.raises(ValueError):
        rec(db, "nope", "k", 1, 0)
    assert rec(db, "ev", "p", 3, 0) and rec(db, "ev", "p", 3, 0), "ev has no throttle: every call writes"


def test_record_uses_at_as_the_throttle_clock_and_stores_it_normalised(db):
    assert samples.record(db, "rl_5h", "claude", 5, at="2026-10-03T06:00:00.9Z")
    assert not samples.record(db, "rl_5h", "claude", 5, at="2026-10-03T11:49:00+05:45")      # 4 min later in another zone
    assert samples.record(db, "rl_5h", "claude", 5, at="2026-10-03T11:50:00+05:45")          # 5 min
    assert [r[0] for r in rows_of(db, "rl_5h")] == [I(0), I(300)]


# ================================================================== writers
STATUS = {"model": "Opus", "model_id": "claude-opus-5-5", "context_pct": 41.2, "context_size": 200000, "cost_usd": 1.25,
          "rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
                          "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600}}}


def test_record_statusline_writes_every_series(db):
    got = samples.record_statusline(db, "shop--api--s1", STATUS, "claude", at=T(0))
    assert got == ["ctx", "ctx_tok", "scost", "rl_5h", "rl_7d"]
    assert db.sample_last("ctx", "shop--api--s1") == {"at": I(0), "value": 41.2, "meta": {"model": "claude-opus-5-5", "window": 200000}}
    assert db.sample_last("ctx_tok", "shop--api--s1")["value"] == 82400.0
    assert db.sample_last("scost", "shop--api--s1")["value"] == 1.25
    assert db.sample_last("rl_5h", "claude") == {"at": I(0), "value": 23.5, "meta": {"resets_at": 1738425600}}
    assert db.sample_last("rl_7d", "claude")["value"] == 41.2 and db.sample_last("rl_7d", "claude")["meta"] == {"resets_at": 1738857600}
    assert samples.record_statusline(db, "shop--api--s1", STATUS, "claude", at=T(5)) == [], "the next identical event writes nothing"
    moved = {**STATUS, "context_pct": 41.8, "cost_usd": 1.26}
    assert samples.record_statusline(db, "shop--api--s1", moved, "claude", at=T(10)) == ["ctx", "ctx_tok", "scost"], "ctx_tok follows ctx"
    assert db.sample_last("ctx_tok", "shop--api--s1")["value"] == 83600.0


def test_record_statusline_codex_agent_key_and_field_fallbacks(db):
    st = {"context_pct": None, "context_size": 200000, "cost_usd": None,
          "rate_limits": {"five_hour": {"used_percent": 7, "resets_at": 5}, "seven_day": "junk"}}
    assert samples.record_statusline(db, "t", st, "codex", at=T(0)) == ["rl_5h"], "used_percent is accepted when used_percentage is absent"
    assert db.sample_last("rl_5h", "codex")["value"] == 7.0 and db.sample_last("rl_7d", "codex") is None
    assert samples.record_statusline(db, "t", {"context_pct": 10.0}, at=T(0)) == ["ctx"], "no window size: no ctx_tok, and no meta window"
    assert db.sample_last("ctx", "t")["meta"] is None
    for junk in (None, "x", {}, {"rate_limits": "no", "context_pct": "x", "cost_usd": True}, {"rate_limits": {"five_hour": {"used_percentage": "n/a"}}}):
        assert samples.record_statusline(db, "u", junk, at=T(0)) == []


def test_record_state_codes_and_meta(db):
    row = {"project": "shop", "repo": "api", "name": "s1", "agent": "claude", "prompt": "never stored", "cmd": "claude --x"}
    for i, st in enumerate(("idle", "working", "waiting", "done", "errored", "ended")):
        assert samples.record_state(db, "shop--api--s1", None, st, "E", row, at=T(i))
    assert [r[2] for r in rows_of(db, "state")] == [0, 1, 2, 3, 4, 5]
    q = db.samples_query("state", None, T(0))
    assert json.loads(q[1][3]) == {"p": "shop", "r": "api", "s": "s1", "a": "claude"}, "only p, r, s, a"
    assert samples.record_state(db, "t", "idle", "idle", "E", row, at=T(9)) is False
    assert samples.record_state(db, "t", "idle", "bogus", "E", row, at=T(9)) is False
    assert samples.record_state(db, "t2", "idle", "working", "E", None, at=T(9))
    assert db.sample_last("state", "t2")["meta"] == {"a": "claude"}
    samples.record_state(db, "t3", None, "idle", "E", {"project": "p", "repo": "r", "name": "n", "agent": "shell"}, at=T(9))
    assert db.sample_last("state", "t3")["meta"]["a"] == "shell"


def test_state_changes_reach_the_series_through_the_db_hook(db):
    db.on_state_change = lambda *a: samples.record_state(db, *a)
    name = new_row(db)
    db.set_state(name, "idle", "SessionStart")
    db.set_state(name, "idle", "SessionStart")
    db.set_state(name, "working", "UserPromptSubmit")
    db.set_state(name, "done", "Stop", attention=True)
    db.end(name, "killed")
    assert [r[2] for r in rows_of(db, "state", name)] == [0.0, 1.0, 3.0, 5.0]
    assert db.sample_last("state", name)["meta"] == {"p": "shop", "r": "api", "s": "s1", "a": "claude"}


def test_bump_event_counts_per_project_hour(db):
    for sec in (0, 1, 2):
        samples.bump_event(db, "shop", at=T(sec))
    samples.bump_event(db, "blog", at=T(3))
    samples.bump_event(db, "shop", at=T(3600 + 5))
    assert samples.bump_event(db, "", at=T(0)) == 0 and samples.bump_event(db, None, at=T(0)) == 0
    assert rows_of(db, "ev", "shop") == [(I(0), "shop", 3.0), (I(3605), "shop", 1.0)] and rows_of(db, "ev", "blog") == [(I(3), "blog", 1.0)]


LIVE_LIMIT = "You've hit your session limit · resets 10:05pm (Asia/Kathmandu)"


def limit_of(text=LIVE_LIMIT, now=T0):
    from app.agents.claude import parse_limit_message
    return {**parse_limit_message(text, now), "message": text}


def test_record_limit_collapses_the_repeats_of_one_episode(db):
    lim = limit_of()
    assert lim["kind"] == "5h" and lim["resets_at"]
    assert samples.record_limit(db, "shop--api--s1", lim, {"project": "shop"}, at=T(0))
    for sec in (1, 2, 600, 3600, 4 * 3600):                 # the session retries, its subagents fail: still one episode
        assert not samples.record_limit(db, "shop--api--s1", lim, None, at=T(sec))
    assert len(rows_of(db, "lim")) == 1
    assert samples.record_limit(db, "shop--api--s2", lim, None, at=T(5)), "another session reports its own row"
    nxt = {**lim, "resets_at": lim["resets_at"] + 5 * 3600}
    assert samples.record_limit(db, "shop--api--s1", nxt, None, at=T(6 * 3600)), "the next window is a new episode"
    wk = limit_of("You've hit your weekly limit · resets Oct 9, 3pm")
    assert wk["kind"] == "7d" and samples.record_limit(db, "shop--api--s1", wk, None, at=T(7))
    got = db.samples_query("lim", None, T(0))
    assert [(r[1], r[2]) for r in got] == [("5h", 1.0), ("5h", 1.0), ("7d", 1.0), ("5h", 1.0)]
    meta = json.loads(got[0][3])
    assert meta == {"session": "shop--api--s1", "resets_at": lim["resets_at"], "message": LIVE_LIMIT[:120]}


def test_record_limit_without_a_reset_time_dedupes_per_hour(db):
    lim = {"kind": "other", "resets_at": None, "message": "x" * 300}
    assert samples.record_limit(db, "t", lim, None, at=T(10))
    assert not samples.record_limit(db, "t", lim, None, at=T(3000))
    assert samples.record_limit(db, "t", lim, None, at=T(3700)), "the next UTC hour"
    assert samples.record_limit(db, "u", lim, None, at=T(20))
    m = json.loads(db.samples_query("lim", ["other"], T(0))[0][3])
    assert m == {"session": "t", "message": "x" * 120}, "message cut to 120, no resets_at key"
    assert samples.record_limit(db, "w", {"kind": "weird"}, None, at=T(10)), "an unknown kind files under 'other'"
    assert samples.record_limit(db, "t", None, None, at=T(5 * 3600))
    assert samples.record_limit(db, "t", {"kind": "5h", "resets_at": 123.0}, None, at=T(0)) and not samples.record_limit(
        db, "t", {"kind": "5h", "resets_at": 123}, None, at=T(1)), "123.0 and 123 are the same reset time"


def test_live_stopfailure_payload_becomes_one_lim_row(db):
    from app import agents
    payload = {"hook_event_name": "StopFailure", "error": "rate_limit", "last_assistant_message": LIVE_LIMIT, "session_id": "s",
               "cwd": "/p", "agent_type": "workflow-subagent", "agent_id": "a1"}
    n = agents.get("claude").normalise_hook("StopFailure", payload)
    assert n.kind == "rate_limit" and n.limit["kind"] == "5h"
    assert samples.record_limit(db, "shop--api--s1", n.limit, None, at=T(0))
    n2 = agents.get("claude").normalise_hook("StopFailure", {**payload, "agent_id": "a2"})
    assert not samples.record_limit(db, "shop--api--s1", n2.limit, None, at=T(30))


def cost_entry(i="11111111-1111-1111-1111-111111111111", cost=2.5, tokens=1000, last=None, **kw):
    return {"agent": "claude", "id": i, "cost": cost, "tokens": tokens, "last": last or I(-3600), "project": "shop", "repo": "api",
            "models": ["claude-opus-5-5"], **kw}


KEY = "claude:11111111-1111-1111-1111-111111111111"


def test_record_cost_first_sight_is_stamped_at_last_activity(db):
    n = samples.record_cost(db, {"sessions": [cost_entry(last="2026-10-02T13:00:00.250Z"), cost_entry("22222222-2222-2222-2222-222222222222", 0, 500)]}, T(0))
    assert n == 2
    last = db.sample_last("cost", KEY)
    assert last == {"at": "2026-10-02T13:00:00+00:00", "value": 2.5,
                    "meta": {"p": "shop", "r": "api", "a": "claude", "tok": 1000, "m": ["claude-opus-5-5"]}}
    assert db.sample_last("cost", "claude:22222222-2222-2222-2222-222222222222")["value"] == 0.0, "an unpriced session still gets its row"


def test_record_cost_real_precision_does_not_rewrite_an_idle_session(db):
    """ccusage reports 7-8 decimals (1.04047475); the row stores 6. The compare must use the stored precision, or every idle
    session would get a new row at every refresh (807 extra rows per pass were seen with a real ccusage dump)."""
    def at(cost, **kw):
        return {"sessions": [cost_entry(cost=cost, tokens=123456, last=I(0), **kw)]}
    assert samples.record_cost(db, at(1.04047475), T(0)) == 1
    assert db.sample_last("cost", KEY)["value"] == 1.040475
    assert samples.record_cost(db, at(1.04047475), T(11 * 60)) == 0, "same cost to the stored precision: no row"
    assert samples.record_cost(db, at(1.04047475), T(2 * 86400)) == 0, "still none, however old the last row is"
    assert samples.record_cost(db, at(1.04047600), T(22 * 60)) == 1, "a change visible at 6 dp after 10 minutes: one row"


def test_record_cost_cadence_changed_and_ten_minutes(db):
    def at_zero(**kw):
        return {"sessions": [cost_entry(last=I(0), **kw)]}
    assert samples.record_cost(db, at_zero(), T(0)) == 1
    assert samples.record_cost(db, at_zero(), T(86400)) == 0, "unchanged: nothing, however old the last row is"
    assert samples.record_cost(db, at_zero(cost=3.0), T(599)) == 0, "changed, but the last row is under 10 minutes old"
    assert samples.record_cost(db, at_zero(cost=3.0), T(600)) == 1
    assert samples.record_cost(db, at_zero(cost=3.0), T(1300)) == 0
    assert samples.record_cost(db, at_zero(cost=3.0, tokens=2000), T(1300)) == 1, "a token-only change counts (an unpriced session)"
    assert [(r[0], r[2]) for r in rows_of(db, "cost", KEY)] == [(I(0), 2.5), (I(600), 3.0), (I(1300), 3.0)]
    assert json.loads(db.samples_query("cost", [KEY], T(0))[-1][3])["tok"] == 2000
    assert samples.record_cost(db, {"sessions": [cost_entry(last=I(0), cost=3.0, tokens=2000), cost_entry("33333333-3333-3333-3333-333333333333")]}, T(2000)) == 1, \
        "one entry unchanged, one new: only the new one is written"


def test_record_cost_skips_stale_future_and_malformed_entries(db):
    old = I(-121 * 86400)
    entries = [cost_entry("aaaaaaaa-0000-0000-0000-000000000001", last=old),                       # older than the 120 d retention
               cost_entry("aaaaaaaa-0000-0000-0000-000000000002", last=I(+7200)),                  # in the future: stamped now
               cost_entry("aaaaaaaa-0000-0000-0000-000000000003", last="garbage"),                 # unreadable: stamped now
               cost_entry("aaaaaaaa-0000-0000-0000-000000000004", last=None) | {"last": None},     # none: stamped now
               {"agent": "codex", "id": "x1", "cost": 1.0, "tokens": 5, "last": I(-60), "project": None, "repo": None, "models": []},
               {"id": "", "cost": 1}, {"id": "n", "cost": None}, "junk", None]
    assert samples.record_cost(db, {"sessions": entries}, T(0)) == 4
    assert db.sample_last("cost", "claude:aaaaaaaa-0000-0000-0000-000000000001") is None
    for i in (2, 3, 4):
        assert db.sample_last("cost", f"claude:aaaaaaaa-0000-0000-0000-00000000000{i}")["at"] == I(0)
    assert db.sample_last("cost", "codex:x1")["meta"] == {"a": "codex", "tok": 5}
    assert samples.record_cost(db, {}, T(0)) == 0 and samples.record_cost(db, None, T(0)) == 0 and samples.record_cost(db, {"sessions": []}, T(0)) == 0


def snap(cpu=12.5, mem=40.0, load=0.7, disk=63.2, host="ctr-123"):
    return {"host": host, "cpu_pct": cpu, "load1": load, "mem": {"total": 1, "used": 1, "pct": mem}, "disk": {"pct": disk}, "uptime_s": 5, "at": 0}


def test_record_health_cadence_and_key(db, monkeypatch):
    monkeypatch.setattr(settings, "node_name", "")
    assert samples.record_health(db, snap(), at=T(0)) == ["h_cpu", "h_mem", "h_load", "h_disk"]
    assert db.sample_last("h_cpu", "ctr-123")["value"] == 12.5 and db.sample_last("h_disk", "ctr-123")["value"] == 63.2
    assert samples.record_health(db, snap(), at=T(30)) == []
    assert samples.record_health(db, snap(), at=T(60)) == ["h_cpu", "h_mem", "h_load"], "disk waits 15 minutes"
    assert samples.record_health(db, snap(), at=T(899)) == ["h_cpu", "h_mem", "h_load"]
    assert samples.record_health(db, snap(), at=T(900)) == ["h_disk"], "the 15 minutes are counted from the first disk sample"
    monkeypatch.setattr(settings, "node_name", "ubu2")      # a container's host name changes with every image: the node name is the key
    samples.record_health(db, snap(), at=T(1000))
    assert db.sample_last("h_cpu", "ubu2") is not None
    assert samples.record_health(db, snap(cpu=None, mem=None, disk=None, load=None), at=T(2000)) == [], "what the box cannot report is skipped"
    assert samples.record_health(db, None, at=T(2000)) == []


def test_record_counts_cadence(db):
    assert samples.record_counts(db, {"live": 7, "work": 1, "attn": 2}, at=T(0)) == ["n_live", "n_work", "n_attn"]
    assert samples.record_counts(db, {"live": 8, "work": 1, "attn": 2}, at=T(59)) == []
    assert samples.record_counts(db, {"live": 8, "work": 0, "attn": 2}, at=T(60)) == ["n_live", "n_work", "n_attn"]
    assert [r[2] for r in rows_of(db, "n_live", "")] == [7.0, 8.0] and [r[2] for r in rows_of(db, "n_work", "")] == [1.0, 0.0]
    assert samples.record_counts(db, {"n_live": 3}, at=T(200)) == ["n_live"]
    assert samples.record_counts(db, None, at=T(300)) == []


# ================================================================== Sampler
class Clock:
    def __init__(self, at=T0):
        self.now = at.timestamp()

    def time(self):
        return self.now

    def advance(self, sec):
        self.now += sec


def make_sampler(db, clock, health_fn=None, counts_fn=None):
    return samples.Sampler(db, health_fn=health_fn or (lambda: snap()), counts_fn=counts_fn or (lambda: {"live": 3, "work": 1, "attn": 0}),
                           clock=clock)


def test_sample_once_writes_by_throttle_and_stamps_the_heartbeat(db, monkeypatch):
    monkeypatch.setattr(settings, "node_name", "box")
    clock = Clock()
    s = make_sampler(db, clock)
    r = s.sample_once()
    assert r["at"] == I(0) and r["wrote"] == ["h_cpu", "h_mem", "h_load", "h_disk", "n_live", "n_work", "n_attn"]
    hb = db.kv_get("samples_heartbeat")
    assert hb["value"] == {"ts": T0.timestamp(), "at": I(0)} and hb["at"]
    for _ in range(3):                                      # 15, 30, 45 s: heartbeat only
        clock.advance(15)
        assert s.sample_once()["wrote"] == []
    assert db.kv_get("samples_heartbeat")["value"]["ts"] == T0.timestamp() + 45
    clock.advance(15)
    assert s.sample_once()["wrote"] == ["h_cpu", "h_mem", "h_load", "n_live", "n_work", "n_attn"]
    assert [r[0] for r in rows_of(db, "h_cpu", "box")] == [I(0), I(60)] and [r[0] for r in rows_of(db, "n_live", "")] == [I(0), I(60)]


def test_a_failing_source_does_not_stop_the_tick(db, caplog):
    def boom():
        raise OSError("no /proc")
    s = make_sampler(db, Clock(), health_fn=boom)
    with caplog.at_level(logging.WARNING, logger="ccboard.samples"):
        r = s.sample_once()
    assert r["wrote"] == ["n_live", "n_work", "n_attn"] and db.kv_get("samples_heartbeat") and "no /proc" in caplog.text
    s = make_sampler(db, Clock(T(500)), counts_fn=boom)
    assert s.sample_once()["wrote"][:1] == ["h_cpu"]


def test_daily_prune_and_checkpoint_run_once_per_utc_day(db, monkeypatch):
    db.sample("ctx", "old", 1, at=T0 - timedelta(days=20))
    db.sample("ctx", "fresh", 1, at=T0 - timedelta(days=1))
    calls = {"prune": 0, "ckpt": 0}
    real_prune, real_ckpt = db.samples_prune, db.wal_checkpoint
    monkeypatch.setattr(db, "samples_prune", lambda *a, **k: (calls.__setitem__("prune", calls["prune"] + 1), real_prune(*a, **k))[1])
    monkeypatch.setattr(db, "wal_checkpoint", lambda: (calls.__setitem__("ckpt", calls["ckpt"] + 1), real_ckpt())[1])
    clock = Clock(T0)
    s = make_sampler(db, clock)
    assert s.sample_once()["pruned"] == 1
    assert [r[1] for r in rows_of(db, "ctx")] == ["fresh"]
    for _ in range(5):
        clock.advance(15)
        assert s.sample_once()["pruned"] is None
    assert calls == {"prune": 1, "ckpt": 1}
    assert make_sampler(db, clock).sample_once()["pruned"] is None, "a restart on the same day does not prune again"
    clock.advance(86400)
    assert s.sample_once()["pruned"] == 0
    assert calls == {"prune": 2, "ckpt": 2}
    assert db.kv_get("samples_pruned_at")["value"]["day"] == "2026-10-04"
    clock.now = datetime(2026, 10, 4, 23, 59, 59, tzinfo=UTC).timestamp()
    assert s.sample_once()["pruned"] is None
    clock.advance(1)                                        # midnight UTC
    assert s.sample_once()["pruned"] == 0 and calls["prune"] == 3


def test_a_failed_prune_is_retried_on_the_next_tick(db, monkeypatch, caplog):
    monkeypatch.setattr(db, "samples_prune", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    clock = Clock()
    s = make_sampler(db, clock)
    with caplog.at_level(logging.WARNING, logger="ccboard.samples"):
        assert s.sample_once()["pruned"] is None
    assert db.kv_get("samples_pruned_at") is None and "disk" in caplog.text
    monkeypatch.undo()
    clock.advance(15)
    assert s.sample_once()["pruned"] == 0


def test_sampler_thread_ticks_at_once_and_stop_joins(db):
    first = threading.Event()

    def health_fn():
        first.set()
        return snap()
    s = samples.Sampler(db, health_fn=health_fn, counts_fn=lambda: {"live": 1, "work": 0, "attn": 0})
    s.start()
    assert first.wait(5), "the first tick runs at start, not 15 s later"
    t0 = time.monotonic()
    s.stop()
    assert time.monotonic() - t0 < 5 and not s.is_alive(), "stop() joins (it waits at most 2 s) instead of sitting out the 15 s tick"
    assert db.kv_get("samples_heartbeat") is not None
    s.stop()                                                # idempotent
    samples.Sampler(db, health_fn=snap, counts_fn=dict).stop()      # never started: no error


# ================================================================== read side
def test_parse_since_forms():
    now = datetime(2026, 10, 3, 6, 0, 0, tzinfo=UTC)
    for s, d in (("1h", timedelta(hours=1)), ("6h", timedelta(hours=6)), ("24h", timedelta(hours=24)), ("7d", timedelta(days=7)),
                 ("30d", timedelta(days=30)), ("90d", timedelta(days=90)), ("15m", timedelta(minutes=15)), ("2w", timedelta(weeks=2)),
                 (" 24H ", timedelta(hours=24))):
        assert samples.parse_since(s, now) == now - d, s
    assert samples.parse_since("now", now) == now
    for s in ("2026-10-01T09:00:00Z", "2026-10-01T09:00:00+00:00", "2026-10-01T14:45:00+05:45", "2026-10-01T09:00:00", "2026-10-01T09:00:00.5Z"):
        got = samples.parse_since(s, now)
        assert got == datetime(2026, 10, 1, 9, 0, tzinfo=UTC) and got.utcoffset() == timedelta(0), s
    assert samples.parse_since("2026-10-01", now) == datetime(2026, 10, 1, tzinfo=UTC)
    assert samples.parse_since(datetime(2026, 10, 1, 9, 0), now) == datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    for bad in ("", None, "yesterday", "5y", "h", "1 hour", "99999d", "2026-13-45", "24"):
        with pytest.raises(ValueError):
            samples.parse_since(bad, now)
    assert abs((samples.parse_since("1h") - datetime.now(UTC)).total_seconds() + 3600) < 5      # now defaults to the clock


def test_choose_step_picks_the_nice_grid():
    end = datetime(2026, 10, 3, 6, 0, 0, tzinfo=UTC)
    got = {span: samples.choose_step(end - span, end, 500) for span in (
        timedelta(minutes=5), timedelta(hours=1), timedelta(hours=6), timedelta(hours=24), timedelta(days=7), timedelta(days=30),
        timedelta(days=90), timedelta(days=365), timedelta(days=3650))}
    assert list(got.values()) == [10, 10, 60, 300, 1800, 21600, 21600, 86400, 86400 * 8]
    assert samples.choose_step(end - timedelta(hours=24), end, 100) == 900 and samples.choose_step(end - timedelta(hours=24), end, 5000) == 300, \
        "points above 500 are clamped to 500"
    assert samples.choose_step(end, end + timedelta(hours=1), 1) == 21600, "one bucket: the first step whose bucket holds both ends"
    assert samples.choose_step(end - timedelta(hours=1), end, 1) == 86400, "an end on the boundary opens the next 6 h bucket (until is inclusive)"
    assert samples.choose_step(end, end, 500) == 10 and samples.choose_step(end - timedelta(hours=1), end, 0) == 10


def test_choose_step_never_exceeds_the_point_budget_on_the_aligned_grid():
    for offset in (0, 1, 7, 29, 59, 299, 1799, 3599, 21599, 43200):
        for span in (1, 10, 299, 300, 4990, 5000, 5001, 86399, 86400, 7 * 86400 + 13, 90 * 86400 + 5):
            a = T(offset)
            b = a + timedelta(seconds=span)
            for points in (1, 2, 50, 500):
                step = samples.choose_step(a, b, points)
                t0, n = samples._buckets(a.timestamp(), b.timestamp(), step)
                assert n <= points and t0 % step == 0 and t0 <= a.timestamp() and t0 + (n - 1) * step <= b.timestamp() < t0 + n * step, (offset, span, points)
                assert step in samples.STEP_GRID or step % 86400 == 0


def rowset(*items):
    return [(I(sec), key, v, None) for sec, key, v in items]


def test_downsample_avg_last_sum_alignment_with_gaps():
    since, until = T(0), T(3600)                            # 06:00 .. 07:00, step 600 -> 7 buckets (the last holds the instant 07:00)
    rows = rowset((10, "a", 10), (590, "a", 20), (1210, "a", 40), (3000, "a", 5), (3600, "a", 7), (700, "b", 1.5))
    ds = samples.downsample(rows, 600, "avg", since, until)
    assert ds["t"] == [int(T(600 * i).timestamp()) for i in range(7)]
    assert ds["values"]["a"] == [15.0, None, 40.0, None, None, 5.0, 7.0]
    assert ds["values"]["b"] == [None, 1.5, None, None, None, None, None]
    assert samples.downsample(rows, 600, "last", since, until)["values"]["a"] == [20.0, None, 40.0, None, None, 5.0, 7.0]
    assert samples.downsample(rows, 600, "sum", since, until)["values"]["a"] == [30.0, None, 40.0, None, None, 5.0, 7.0]
    assert samples.downsample(rows, 600, "events", since, until)["values"]["a"] == [20.0, None, 40.0, None, None, 5.0, 7.0], "events read as last"
    assert samples.downsample(reversed(list(rows)), 600, "last", since, until)["values"]["a"][0] == 20.0, "row order does not matter"


def test_downsample_grid_is_epoch_aligned_not_since_aligned():
    since = T(137)                                           # not on a boundary
    ds = samples.downsample(rowset((140, "a", 1), (590, "a", 3), (601, "a", 9)), 600, "avg", since, T(1300))
    assert ds["t"] == [int(T(0).timestamp()), int(T(600).timestamp()), int(T(1200).timestamp())]
    assert ds["values"]["a"] == [2.0, 9.0, None]
    assert all(t % 600 == 0 for t in ds["t"])


def test_downsample_ignores_rows_out_of_range_and_valueless_rows():
    rows = rowset((-1, "a", 99), (0, "a", 1), (60, "a", 2), (61, "a", 99)) + [(I(30), "a", None, None)]
    ds = samples.downsample(rows, 30, "last", T(0), T(60))
    assert ds["values"]["a"] == [1.0, None, 2.0] and ds["t"][0] == int(T(0).timestamp())
    assert samples.downsample([], 30, "avg", T(0), T(60)) == {"t": ds["t"], "values": {}}


def seed_series(db):
    for sec in range(0, 7200, 300):                          # rl_5h every 5 minutes for 2 h, claude and codex
        db.sample("rl_5h", "claude", 10 + sec / 300, {"resets_at": 1000 + sec}, at=T(sec))
    db.sample("rl_5h", "codex", 3, at=T(0))
    db.sample("rl_7d", "claude", 50, {"resets_at": 7}, at=T(60))


def test_series_payload_shape_alignment_and_meta(db):
    seed_series(db)
    p = samples.series_payload(db, ["rl_5h", "rl_7d"], ["claude", "codex"], T(0), T(7200), 500)
    assert (p["since"], p["until"]) == (I(0), I(7200)) and p["step"] == 30 and "capped" not in p
    assert p["t"][0] == int(T(0).timestamp()) and len(p["t"]) == 241 and p["t"][1] - p["t"][0] == 30
    assert set(p["series"]) == {"rl_5h:claude", "rl_5h:codex", "rl_7d:claude", "rl_7d:codex"}
    assert all(len(v) == len(p["t"]) for v in p["series"].values())
    claude = p["series"]["rl_5h:claude"]
    assert claude[0] == 10.0 and claude[10] == 11.0 and claude[1] is None and claude[230] == 33.0 and claude[-1] is None
    assert sum(v is not None for v in claude) == 24
    assert p["series"]["rl_5h:codex"][0] == 3.0 and all(v is None for v in p["series"]["rl_7d:codex"]), "a key without data is all gaps"
    assert p["series"]["rl_7d:claude"][2] == 50.0
    assert p["meta"] == {"rl_5h:claude": {"resets_at": 1000 + 6900}, "rl_7d:claude": {"resets_at": 7}}, "last meta per series:key; none for codex"
    json.dumps(p)


def test_series_payload_step_follows_the_window_and_points(db):
    seed_series(db)
    assert samples.series_payload(db, "rl_5h", "claude", T(0), T(7200), 25)["step"] == 300, "a bare series and key are accepted"
    assert samples.series_payload(db, "rl_5h", "claude", T(0), T(7200), 24)["step"] == 900, "25 buckets do not fit in 24 points"
    p = samples.series_payload(db, ["rl_5h"], ["claude"], T(0), T(7200), 25)
    assert len(p["t"]) == 25 and p["series"]["rl_5h:claude"][:3] == [10.0, 11.0, 12.0]
    p = samples.series_payload(db, ["rl_5h"], ["claude"], T(0), T(7200), 10 ** 6)
    assert len(p["t"]) <= samples.MAX_POINTS
    p = samples.series_payload(db, ["rl_5h"], ["claude"], T(0), T(7200), 1)
    assert len(p["t"]) == 1 and p["series"]["rl_5h:claude"][0] == pytest.approx(sum(range(10, 34)) / 24, abs=0.01)
    wide = samples.series_payload(db, ["rl_5h"], ["claude"], T(-90 * 86400), T(0), 500)
    assert wide["step"] == 21600 and len(wide["t"]) <= 500
    for kw in (dict(since="2026-10-03T06:00:00Z", until="2026-10-03T08:00:00Z"), dict(since=T(0).timestamp(), until=T(7200).timestamp())):
        assert samples.series_payload(db, ["rl_5h"], ["claude"], **kw)["since"] == I(0)


def test_series_payload_every_key_most_recent_first_and_the_combo_cap(db):
    for i in range(12):
        db.sample("ctx", f"s{i:02d}", i, {"model": "m%d" % i}, at=T(i * 10))
    db.sample("rl_5h", "claude", 1, at=T(0))
    p = samples.series_payload(db, ["ctx"], None, T(0), T(600), 100)
    assert list(p["series"]) == [f"ctx:s{i:02d}" for i in range(11, 3, -1)] and p["capped"] is True, "newest 8 keys, most recent first"
    assert p["meta"]["ctx:s11"] == {"model": "m11"}
    star = samples.series_payload(db, ["ctx"], ["*"], T(0), T(600), 100)
    assert list(star["series"]) == list(p["series"])
    two = samples.series_payload(db, ["rl_5h", "ctx"], None, T(0), T(600), 100)
    assert len(two["series"]) == 8 and "rl_5h:claude" in two["series"] and two["capped"] is True, "the cap is over series x keys together"
    few = samples.series_payload(db, ["rl_5h"], None, T(0), T(600), 100)
    assert list(few["series"]) == ["rl_5h:claude"] and "capped" not in few
    ok = samples.series_payload(db, ["ctx", "rl_5h"], ["s00", "s01", "s02", "s03"], T(0), T(600), 100)
    assert len(ok["series"]) == 8
    with pytest.raises(ValueError):
        samples.series_payload(db, ["ctx", "rl_5h"], ["s00", "s01", "s02", "s03", "s04"], T(0), T(600), 100)
    with pytest.raises(ValueError):
        samples.series_payload(db, ["ctx"], [f"s{i}" for i in range(9)], T(0), T(600), 100)
    assert len(samples.series_payload(db, ["ctx"], [f"s{i}" for i in range(8)], T(0), T(600), 100)["series"]) == 8


def test_series_payload_validation(db):
    for args in ((["nope"], ["a"], T(0), T(10)), ([], ["a"], T(0), T(10)), (["ctx"], ["a"], T(10), T(10)), (["ctx"], ["a"], T(20), T(10)),
                 (["ctx", "bogus"], ["a"], T(0), T(10))):
        with pytest.raises(ValueError):
            samples.series_payload(db, *args)
    assert samples.series_payload(db, ["ctx"], ["a"], T(0), T(10))["series"] == {"ctx:a": [None, None]}, "a quiet series is an empty chart, not an error"
    assert samples.series_payload(db, ["n_live"], [""], T(0), T(10))["series"] == {"n_live:": [None, None]}
    until_now = samples.series_payload(db, ["ctx"], ["a"], datetime.now(UTC) - timedelta(hours=1))
    assert datetime.fromisoformat(until_now["until"]) > datetime.now(UTC) - timedelta(seconds=30)


def test_series_payload_for_a_sum_series_counts_gaps_as_none(db):
    for sec in (5, 6, 7, 3700):
        samples.bump_event(db, "shop", at=T(sec))
    p = samples.series_payload(db, ["ev"], ["shop"], T(0), T(7200), 3)
    assert p["step"] == 3600 and p["series"]["ev:shop"] == [3.0, 1.0, None]


def test_events_payload_order_filter_and_meta(db):
    db.sample("state", "b", 1, {"p": "shop", "a": "claude"}, at=T(30))
    db.sample("state", "a", 0, None, at=T(10))
    db.sample("state", "a", 4, {"p": "blog"}, at=T(20))
    db.sample("state", "a", 9, None, at=T(-5))
    db.sample("lim", "5h", 1, {"session": "s", "resets_at": 5}, at=T(1))
    got = samples.events_payload(db, "state", T(0))
    assert got["truncated"] is False
    assert got["events"] == [{"t": int(T(10).timestamp()), "key": "a", "v": 0.0, "m": None},
                             {"t": int(T(20).timestamp()), "key": "a", "v": 4.0, "m": {"p": "blog"}},
                             {"t": int(T(30).timestamp()), "key": "b", "v": 1.0, "m": {"p": "shop", "a": "claude"}}]
    assert [e["key"] for e in samples.events_payload(db, "state", T(0), "a")["events"]] == ["a", "a"]
    assert len(samples.events_payload(db, "state", T(0), "*")["events"]) == 3 == len(samples.events_payload(db, "state", I(0), "")["events"])
    assert samples.events_payload(db, "lim", T(0))["events"][0]["m"] == {"session": "s", "resets_at": 5}
    assert samples.events_payload(db, "state", T(40)) == {"events": [], "truncated": False}
    with pytest.raises(ValueError):
        samples.events_payload(db, "bogus", T(0))


def test_events_payload_cap_keeps_the_newest_in_order(db):
    n = samples.EVENTS_CAP
    assert n == 5000
    db.sample_many([("state", "t", i % 6, None, T(i)) for i in range(n + 3)])
    got = samples.events_payload(db, "state", T(0))
    assert got["truncated"] is True and len(got["events"]) == n
    ts = [e["t"] for e in got["events"]]
    assert ts == sorted(ts) and ts[0] == int(T(3).timestamp()) and ts[-1] == int(T(n + 2).timestamp()), "the 3 oldest were cut"
    db.sample_many([("lim", "5h", 1, None, T(i)) for i in range(n)])
    exact = samples.events_payload(db, "lim", T(0))
    assert len(exact["events"]) == n and exact["truncated"] is False, "exactly the cap is not truncated"


# ================================================================== health._cpu_pct per consumer
def test_cpu_pct_keeps_one_previous_reading_per_consumer(monkeypatch):
    stat = {"line": ""}
    real_open = open

    def fake_open(path, *a, **k):
        return io.StringIO(stat["line"]) if path == "/proc/stat" else real_open(path, *a, **k)
    monkeypatch.setattr(health, "open", fake_open, raising=False)
    monkeypatch.setattr(health, "_cpu_prev", {})

    def feed(idle, busy):
        stat["line"] = f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\n"

    feed(idle=100, busy=100)
    assert health._cpu_pct("a") is None and health._cpu_pct("b") is None, "each consumer's first reading has nothing to diff"
    feed(idle=100, busy=200)                                # all busy since the first reading
    assert health._cpu_pct("a") == 100.0
    feed(idle=200, busy=200)                                # a: all idle since ITS last reading; b: half busy since ITS first one
    assert health._cpu_pct("a") == 0.0, "a's baseline is its own previous reading"
    assert health._cpu_pct("b") == 50.0, "b was not disturbed by a's reads in between"
    assert health._cpu_pct() is None, "the default consumer is a third baseline"
    feed(idle=300, busy=200)
    assert health._cpu_pct() == 0.0
    assert health._cpu_pct("a") == 0.0
    monkeypatch.setattr(health, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("no /proc")), raising=False)
    assert health._cpu_pct("a") is None


def test_snapshot_takes_a_consumer(monkeypatch):
    seen = []
    monkeypatch.setattr(health, "_cpu_pct", lambda consumer="default": seen.append(consumer) or 1.5)
    assert health.snapshot()["cpu_pct"] == 1.5 and health.snapshot(consumer="sampler")["cpu_pct"] == 1.5
    assert health.snapshot({"x": 1}, "sampler")["x"] == 1
    assert seen == ["default", "sampler", "sampler"]


def test_rate_limit_readings_from_two_sessions_do_not_ping_pong(db):
    """Two sessions report the same account-wide window a little apart (62 then 60): the lower, staler reading is dropped; a rise, a
    new window or the 5 min heartbeat still write."""
    def sl(used, resets_at=1791048600):
        return {"rate_limits": {"five_hour": {"used_percent": used, "resets_at": resets_at}}}
    assert "rl_5h" in samples.record_statusline(db, "shop--api--s1", sl(62), at=T(0))
    assert "rl_5h" not in samples.record_statusline(db, "shop--api--s2", sl(60), at=T(10)), "lower in the same window: stale"
    assert "rl_5h" not in samples.record_statusline(db, "shop--api--s1", sl(62), at=T(20)), "unchanged: throttled"
    assert "rl_5h" in samples.record_statusline(db, "shop--api--s2", sl(63), at=T(30)), "a rise is news"
    assert "rl_5h" not in samples.record_statusline(db, "shop--api--s1", sl(62), at=T(40))
    assert "rl_5h" in samples.record_statusline(db, "shop--api--s1", sl(3, resets_at=1791066600), at=T(50)), "a new window starts low"
    assert "rl_5h" in samples.record_statusline(db, "shop--api--s2", sl(1, resets_at=1791066600), at=T(50 + 301)), "the heartbeat still writes a lower value after 5 min"
    assert [r[2] for r in db.samples_query("rl_5h", None, "2000-01-01T00:00:00+00:00", None)] == [62.0, 63.0, 3.0, 1.0]


# ================================================================== claude-mem counters and their per-day rates (agents/monitor.py, v0.5.20)
def test_mem_rates_from_board_samples_never_negative_and_collecting_until_enough(db):
    from app.agents import monitor
    t0 = 1_800_000_000.0
    assert monitor.rate(db, "mem_obs", 86400, 3600, now=t0) == (None, None), "no samples: collecting"
    samples.record(db, "mem_obs", "", 1000, at=t0 - 7200, force=True)
    samples.record(db, "mem_obs", "", 1100, at=t0 - 5400, force=True)
    assert monitor.rate(db, "mem_obs", 86400, 3600, now=t0 - 5400)[0] is None, "30 min of samples: still collecting"
    samples.record(db, "mem_obs", "", 5, at=t0 - 3600, force=True)            # the counter reset (a new database)
    samples.record(db, "mem_obs", "", 105, at=t0, force=True)
    r, since = monitor.rate(db, "mem_obs", 86400, 3600, now=t0)
    assert r == round((100 + 100) / 7200 * 86400, 1) and r > 0, "the drop adds nothing, the growth after it counts"
    assert since is not None
    assert monitor.rate(db, "mem_obs", 7 * 86400, 86400, now=t0)[0] is None, "two hours of samples do not make a week's rate"


def test_monitor_enrich_writes_the_series_and_adds_rates_and_compat(db, monkeypatch):
    from app import memory
    from app.agents import monitor
    monkeypatch.setattr(memory, "plugin_status", lambda: {"installed": True, "version": "13.34.2", "enabled": True})
    h = monitor.enrich(db, {"state": "up", "version": "13.31.0", "observations": 10, "summaries": 2})
    assert db.sample_last("mem_obs", "")["value"] == 10 and db.sample_last("mem_sum", "")["value"] == 2
    assert h["rates"] == {"obs": {"d1": None, "d7": None}, "sum": {"d1": None, "d7": None}}
    assert h["plugin_version"] == "13.34.2" and h["compat"] == "ok" and h["tested_worker"] == "13.31.0"
    down = monitor.enrich(db, {"state": "down", "version": None, "observations": None, "summaries": None})
    assert down["compat"] == "unknown" and db.sample_last("mem_obs", "")["value"] == 10, "a down worker writes no zero"
