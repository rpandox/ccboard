"""The board's second reading source and the time-aware windows (backend): accounts.poll_usage_cache, accounts.window_now / headroom,
usage.rate_limits_view (state.usage) and the demo fixtures.

Claude Code keeps `cachedUsageUtilization` in its state file (`<config dir>/.claude.json`). On the Sampler tick the board reads only that
key (one stat; a parse only when the file's stamp changed), and feeds it the statusline's way when it is newer than the newest reading it
holds for the account. Nothing here reads the real ~/.claude.json: the config dir is a tmp one with a fixture state file, claude auth
status is faked, and the identity cache runs on a fake clock (tests/test_accounts.py's fixtures).
"""
import builtins
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import accounts, claude_auth, hooks, samples, usage
from app.config import settings
from app.db import DB, iso

UUID_A = "aaaaaaaa-0000-4000-8000-000000000001"
UUID_B = "bbbbbbbb-0000-4000-8000-000000000002"
SECRET = "SECRET-SENTINEL-do-not-leak"
T0 = 1_790_000_000
W5, W7 = 18000, 604800
FIXTURES = Path(__file__).parent / "fixtures"

_mtime = [time.time_ns()]


def utc_iso(epoch: float, ms: bool = False) -> str:
    d = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return d.isoformat(timespec="milliseconds" if ms else "seconds").replace("+00:00", "Z" if ms else "+00:00")


def cache_block(fetched=T0 - 60, account=UUID_A, five=(37.5, T0 + 9000), seven=(62.0, T0 + 300000)) -> dict:
    """cachedUsageUtilization as Claude Code writes it: {fetchedAtMs, accountUuid, utilization: {five_hour, seven_day}}."""
    util = {}
    if five is not None:
        util["five_hour"] = {"utilization": five[0], "resets_at": utc_iso(five[1], ms=True) if five[1] else None}
    if seven is not None:
        util["seven_day"] = {"utilization": seven[0], "resets_at": utc_iso(seven[1], ms=True) if seven[1] else None}
    util["seven_day_opus"] = {"utilization": 99.0, "resets_at": utc_iso(T0 + 1)}          # a window the board does not read
    return {"fetchedAtMs": int(fetched * 1000), "accountUuid": account, "utilization": util}


def write_state(cfg: Path, uc=None, uuid=UUID_A, email="a@example.com", name="Ann", account=True, **extra) -> None:
    """Claude's state file: a big file whose oauthAccount and cachedUsageUtilization are the only parts the board reads. Every write
    gets its own mtime (a coarse filesystem clock can give two quick writes one, and the board tells a rewrite by the stamp)."""
    cfg.mkdir(parents=True, exist_ok=True)
    data = {"primaryApiKey": SECRET, "projects": {"/x": {"history": ["y" * 500] * 50}}, "userID": SECRET, **extra}
    if account:
        data["oauthAccount"] = {"accountUuid": uuid, "emailAddress": email, "displayName": name, "organizationUuid": "cccccccc-0000-4000-8000-000000000003",
                                "organizationName": "Ann's Org", "organizationType": "claude_max", "oauthRefreshToken": SECRET}
    if uc is not None:
        data["cachedUsageUtilization"] = {**uc, "note": SECRET}                          # a neighbour key the board must not carry
    (cfg / ".claude.json").write_text(json.dumps(data))
    _mtime[0] += 1_000_000_000
    os.utime(cfg / ".claude.json", ns=(_mtime[0], _mtime[0]))


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "claude"
    monkeypatch.setattr(settings, "claude_config_dir", cfg)
    monkeypatch.setattr(claude_auth, "status", lambda: {"installed": True, "loggedIn": False})
    clock = [1000.0]
    monkeypatch.setattr(accounts, "_clock", lambda: clock[0])
    accounts.invalidate()
    e = SimpleNamespace(db=DB(tmp_path / "a.db"), cfg=cfg, tmp=tmp_path)
    yield e
    accounts.invalidate()


def kv(db, key):
    r = db.kv_get(key)
    return r["value"] if r else None


def rows(db, name, key):
    return [(at, v, json.loads(m) if m else None) for at, _k, v, m in db.samples_query(name, [key], "2000-01-01T00:00:00+00:00")]


def spy_opens(monkeypatch) -> list:
    seen = []
    real = builtins.open

    def spy(file, *a, **k):
        seen.append(str(file))
        return real(file, *a, **k)
    monkeypatch.setattr(builtins, "open", spy)
    return seen


def one(env, uc=None, **kw):
    """The board's account A observed from a state file that carries the cache `uc`."""
    write_state(env.cfg, cache_block() if uc is None else uc, **kw)
    accounts.observe(env.db, T0)


# ================================================================== reading the cache out of the state file
def test_the_cache_is_read_with_its_whitelisted_fields_only(env):
    write_state(env.cfg, cache_block(fetched=T0 - 60.5))
    got = accounts.read_usage_cache()
    assert got == {"at": T0 - 61, "account": UUID_A,
                   "windows": {"five_hour": {"used_percentage": 37.5, "resets_at": T0 + 9000},
                               "seven_day": {"used_percentage": 62.0, "resets_at": T0 + 300000}}}, \
        "percent used and epoch seconds, the statusline's own units; seven_day_opus and the neighbour keys are dropped"
    assert SECRET not in json.dumps(got)


def test_odd_caches_give_what_they_can_and_never_raise(env):
    cfg = env.cfg
    write_state(cfg, cache_block(five=(140.0, None), seven=None))
    assert accounts.read_usage_cache() == {"at": T0 - 60, "account": UUID_A, "windows": {"five_hour": {"used_percentage": 100.0}}}, \
        "over 100 is clamped, a null reset time is left out, a missing window is absent"
    for bad in ({"fetchedAtMs": "yesterday", "utilization": {"five_hour": {"utilization": 1, "resets_at": None}}},     # no usable time
                {"fetchedAtMs": T0 * 1000, "utilization": {"five_hour": {"utilization": "n/a"}}},                      # no usable number
                {"fetchedAtMs": T0 * 1000, "utilization": []}, {"fetchedAtMs": True, "utilization": {}}, [], "x", 7):
        write_state(cfg, cache_block())
        data = json.loads((cfg / ".claude.json").read_text())
        data["cachedUsageUtilization"] = bad
        (cfg / ".claude.json").write_text(json.dumps(data))
        _mtime[0] += 10
        os.utime(cfg / ".claude.json", ns=(_mtime[0], _mtime[0]))
        assert accounts.read_usage_cache() is None, bad
    (cfg / ".claude.json").write_text("{not json")
    os.utime(cfg / ".claude.json", ns=(_mtime[0] + 7, _mtime[0] + 7))
    assert accounts.read_usage_cache() is None, "a half-written file keeps the last answer (none) instead of raising"
    assert accounts.read_usage_cache(env.tmp / "elsewhere") is None, "no file at all"


def test_a_garbled_file_keeps_the_last_good_answer_and_is_retried(env):
    write_state(env.cfg, cache_block())
    first = accounts.read_usage_cache()
    assert first is not None
    (env.cfg / ".claude.json").write_text('{"cachedUsageUtilization": {"fetched')
    os.utime(env.cfg / ".claude.json", ns=(_mtime[0] + 3, _mtime[0] + 3))
    assert accounts.read_usage_cache() == first, "the file is rewritten constantly: half a write is not 'the cache is gone'"
    write_state(env.cfg, cache_block(fetched=T0 - 30))
    assert accounts.read_usage_cache()["at"] == T0 - 30, "the next clean file is read at once"


def test_the_stamp_gate_one_stat_when_nothing_changed_a_parse_when_the_file_did(env, monkeypatch):
    write_state(env.cfg, cache_block())
    accounts.read_usage_cache()
    opens = spy_opens(monkeypatch)
    for _ in range(5):
        assert accounts.read_usage_cache()["at"] == T0 - 60
    assert not [p for p in opens if p.endswith(".json")], "an unchanged stamp is answered from memory"
    write_state(env.cfg, cache_block(fetched=T0 - 10))
    assert accounts.read_usage_cache()["at"] == T0 - 10
    assert [p for p in opens if p.endswith(".claude.json")], "a rewritten file is parsed again"


def test_the_newer_cache_of_two_candidate_files_wins(env):
    write_state(env.cfg, cache_block(fetched=T0 - 500))
    beside = env.cfg.parent / (env.cfg.name + ".json")                              # `<dir>.json` next to the dir
    beside.write_text(json.dumps({"cachedUsageUtilization": cache_block(fetched=T0 - 20)}))
    assert accounts.read_usage_cache()["at"] == T0 - 20


# ================================================================== feeding it
def test_a_cache_newer_than_the_readings_is_fed_the_statuslines_way(env):
    db = env.db
    one(env)
    assert accounts.poll_usage_cache(db, T0) is True
    assert accounts.current(db) == UUID_A
    five = rows(db, "rl_5h", f"acct:{UUID_A}")
    assert five == [(iso(T0 - 60), 37.5, {"resets_at": T0 + 9000, "source": "cache"})], "stamped with the cache's own fetch time, meta source 'cache'"
    assert rows(db, "rl_5h", "claude") == five and rows(db, "rl_7d", "claude") == rows(db, "rl_7d", f"acct:{UUID_A}")
    assert rows(db, "rl_7d", "claude")[0][1:] == (62.0, {"resets_at": T0 + 300000, "source": "cache"})
    rec = db.kv_get("rate_limits")
    assert rec["at"] == iso(T0 - 60), "the kv record is stamped with when the reading was true, not when it was copied"
    assert rec["value"] == {"five_hour": {"used_percentage": 37.5, "resets_at": T0 + 9000}, "seven_day": {"used_percentage": 62.0, "resets_at": T0 + 300000},
                            "source": "cache"}
    fp = kv(db, "accounts")[UUID_A]
    assert (fp["resets_5h"], fp["resets_7d"]) == (T0 + 9000, T0 + 300000), "the account's fingerprint follows, so the Accounts row shows the new reset"
    row = accounts.view(db)["list"][0]
    assert (row["rl_5h"], row["rl_7d"], row["source"], row["rl_5h_at"]) == (37.5, 62.0, "cache", iso(T0 - 60))


def test_a_reading_already_fed_is_not_fed_again(env, monkeypatch):
    db = env.db
    one(env)
    assert accounts.poll_usage_cache(db, T0) is True
    n = len(rows(db, "rl_5h", "claude"))
    assert accounts.poll_usage_cache(db, T0 + 15) is False
    write_state(env.cfg, cache_block(), extra_key={"a": 1})                       # the file changed, the cache did not
    assert accounts.poll_usage_cache(db, T0 + 30) is False
    assert len(rows(db, "rl_5h", "claude")) == n
    accounts._fed.clear()                                                         # a restart forgets the memo: the readings themselves still say so
    assert accounts.poll_usage_cache(db, T0 + 45) is False


def test_the_cache_is_picked_only_when_newer_than_the_newest_reading(env):
    db = env.db
    one(env, cache_block(fetched=T0 - 60))
    live = {"five_hour": {"used_percentage": 50.0, "resets_at": T0 + 9000}, "seven_day": {"used_percentage": 70.0, "resets_at": T0 + 300000}}
    accounts.for_reading(db, live, T0 - 30)
    samples.record_statusline(db, "shop--api--s1", {"rate_limits": live}, "claude", at=T0 - 30, account=UUID_A, current=True)
    db.kv_set("rate_limits", live, at=T0 - 30)
    assert accounts.poll_usage_cache(db, T0) is False, "a statusline reading from 30 s ago is newer than the cache from 60 s ago"
    assert kv(db, "rate_limits") == live and [v for _a, v, _m in rows(db, "rl_5h", "claude")] == [50.0]
    write_state(env.cfg, cache_block(fetched=T0 - 29))
    assert accounts.poll_usage_cache(db, T0) is True, "one second newer: the cache wins"
    assert kv(db, "rate_limits")["five_hour"]["used_percentage"] == 37.5 and db.kv_get("rate_limits")["at"] == iso(T0 - 29)
    write_state(env.cfg, cache_block(fetched=T0 - 29, five=(99.0, T0 + 9000)))
    accounts._fed.clear()
    assert accounts.poll_usage_cache(db, T0 + 1) is False, "the same fetch time is not newer than the reading it produced"


def test_only_the_kv_reading_counts_for_the_current_account_when_the_series_are_empty(env):
    one(env, cache_block(fetched=T0 - 60))
    env.db.kv_set("rate_limits", {"five_hour": {"used_percentage": 1.0}}, at=T0 - 10)
    assert accounts.poll_usage_cache(env.db, T0) is False, "the pills' own record is a reading the board holds"


def test_attribution_by_account_uuid_leaves_the_current_account_alone(env):
    db = env.db
    write_state(env.cfg, cache_block(account=UUID_B, five=(11.0, T0 + 5000), seven=(22.0, T0 + 250000)))
    accounts.observe(db, T0)                                                       # A is the account in the file ...
    accounts.remember(db, {"key": UUID_B, "email": "b@example.com", "name": "Bob", "plan": "pro", "config_dir": None}, T0)   # ... B is known too
    assert accounts.current(db) == UUID_A
    assert accounts.poll_usage_cache(db, T0) is True
    assert [v for _a, v, _m in rows(db, "rl_5h", f"acct:{UUID_B}")] == [11.0] and [v for _a, v, _m in rows(db, "rl_7d", f"acct:{UUID_B}")] == [22.0]
    assert rows(db, "rl_5h", "claude") == [] and rows(db, "rl_5h", f"acct:{UUID_A}") == [], "the current account's series are untouched"
    assert db.kv_get("rate_limits") is None, "the pills (the current account) keep what they had"
    b = kv(db, "accounts")[UUID_B]
    assert (b["resets_5h"], b["resets_7d"]) == (T0 + 5000, T0 + 250000)
    assert accounts.view(db)["current"] == UUID_A


def test_an_unknown_account_uuid_falls_back_to_the_current_account(env):
    db = env.db
    one(env, cache_block(account="dddddddd-0000-4000-8000-00000000000d"))
    assert accounts.poll_usage_cache(db, T0) is True
    assert [v for _a, v, _m in rows(db, "rl_5h", f"acct:{UUID_A}")] == [37.5]
    assert kv(db, "rate_limits")["five_hour"]["used_percentage"] == 37.5
    assert "dddddddd-0000-4000-8000-00000000000d" not in json.dumps(kv(db, "accounts"))


def test_no_account_known_feeds_the_claude_series_only(env):
    db = env.db
    write_state(env.cfg, cache_block(account=None), account=False)                 # no oauthAccount: nobody to attribute to
    assert accounts.poll_usage_cache(db, T0) is True
    assert [v for _a, v, _m in rows(db, "rl_5h", "claude")] == [37.5] and kv(db, "rate_limits")["seven_day"]["used_percentage"] == 62.0
    assert db.sample_last("rl_5h", f"acct:{UUID_A}") is None


def test_a_fetch_time_in_the_future_is_clamped_to_now(env):
    db = env.db
    one(env, cache_block(fetched=T0 + 99999))
    assert accounts.poll_usage_cache(db, T0) is True
    assert rows(db, "rl_5h", "claude")[0][0] == iso(T0) and db.kv_get("rate_limits")["at"] == iso(T0)


def test_keys_the_cache_does_not_carry_are_kept_in_the_kv_record(env):
    db = env.db
    one(env, cache_block(seven=None))
    db.kv_set("rate_limits", {"five_hour": {"used_percentage": 1.0}, "spend_limit": {"used_percentage": 12.0}}, at=T0 - 900)
    assert accounts.poll_usage_cache(db, T0) is True
    v = kv(db, "rate_limits")
    assert v["spend_limit"] == {"used_percentage": 12.0}, "the SPEND pill must not vanish"
    assert v["five_hour"]["used_percentage"] == 37.5 and "seven_day" not in v and v["source"] == "cache"


def test_a_statusline_reading_after_the_cache_is_the_live_source_again(env):
    db = env.db
    one(env)
    db.add_session(tmux_name="shop--api--s1", project="shop", repo="api", name="s1", launcher="claude")
    assert accounts.poll_usage_cache(db, T0) is True
    hooks.apply(db, "shop--api--s1", "statusline", {"rate_limits": {"five_hour": {"used_percentage": 41.0, "resets_at": T0 + 9000},
                                                                      "seven_day": {"used_percentage": 63.0, "resets_at": T0 + 300000}}})
    assert "source" not in kv(db, "rate_limits"), "a session's statusline replaces the record wholesale: no source mark"
    assert accounts.read_usage_cache()["at"] < time.time() and accounts.poll_usage_cache(db, T0 + 15) is False, "the cache is older than that reading"
    last = db.sample_last("rl_5h", "claude")
    assert last["value"] == 41.0 and "source" not in (last["meta"] or {})


def test_nothing_of_the_state_file_is_logged_or_stored(env, caplog):
    db = env.db
    caplog.set_level(logging.DEBUG)
    one(env)
    accounts.poll_usage_cache(db, T0)
    write_state(env.cfg, cache_block(fetched=T0 - 5))
    accounts.poll_usage_cache(db, T0 + 15)
    (env.cfg / ".claude.json").write_text('{"primaryApiKey": "' + SECRET + '", "cachedUsageUtilization": {"fetch')   # unreadable, with a secret in it
    os.utime(env.cfg / ".claude.json", ns=(_mtime[0] + 9, _mtime[0] + 9))
    assert accounts.poll_usage_cache(db, T0 + 30) is False
    assert SECRET not in caplog.text
    dump = [str(db.kv_get(k)) for k in ("rate_limits", "accounts", "account_current")]
    for name in ("rl_5h", "rl_7d", "acct"):
        dump += [str(r) for r in db.samples_query(name, None, "2000-01-01T00:00:00+00:00")]
    assert SECRET not in " ".join(dump), "only the whitelisted fields reach the kv and the samples"


def test_a_failing_poll_never_raises(env, monkeypatch):
    one(env)
    monkeypatch.setattr(samples, "record_statusline", lambda *a, **k: (_ for _ in ()).throw(RuntimeError(SECRET)))
    assert accounts.poll_usage_cache(env.db, T0) is False


def test_the_sampler_tick_runs_it_after_observe(env):
    write_state(env.cfg, cache_block(fetched=time.time() - 60))
    hooks_ = samples.TICK_HOOKS
    assert accounts.poll_usage_cache in hooks_ and hooks_.index(accounts.poll_usage_cache) == hooks_.index(accounts.observe) + 1
    s = samples.Sampler(env.db, health_fn=lambda: {}, counts_fn=dict, clock=SimpleNamespace(time=time.time))
    s.sample_once()
    assert accounts.current(env.db) == UUID_A, "observe ran first"
    assert [v for _a, v, _m in rows(env.db, "rl_5h", f"acct:{UUID_A}")] == [37.5] and kv(env.db, "rate_limits")["source"] == "cache"


# ================================================================== the arithmetic, one rule on both sides
def _cases():
    return json.loads((FIXTURES / "window_now.json").read_text())


@pytest.mark.parametrize("c", _cases()["cases"], ids=lambda c: c["name"])
def test_window_now_matches_the_shared_table(c):
    period = _cases()["periods"][c["win"]]
    pct, nxt, rolled = accounts.window_now(c["used"], c["resets_at"], period, c["now"])
    if c["expect"] is None:
        assert (pct, nxt, rolled) == (None, None, False)
    else:
        assert (pct, nxt, rolled) == (c["expect"]["pct"], c["expect"]["next"], c["expect"]["rolled"])


def test_headroom_agrees_with_the_window_arithmetic_across_missed_windows(env):
    db = env.db
    one(env)
    accounts.remember(db, {"key": UUID_B, "email": "b@example.com", "name": "Bob", "config_dir": None}, T0)
    for key, v5, r5, v7, r7 in ((UUID_A, 80.0, T0 - 5 * W5 - 100, 40.0, T0 - 3 * W7 - 3600), (UUID_B, 20.0, T0 + 3000, 60.0, T0 + 200000)):
        samples.record_statusline(db, "", {"rate_limits": {"five_hour": {"used_percentage": v5, "resets_at": r5},
                                                           "seven_day": {"used_percentage": v7, "resets_at": r7}}}, "claude",
                                  at=T0 - 7 * 86400, account=key, current=False)
    h = accounts.headroom(db, T0)
    a5 = next(r for r in h["5h"] if r["key"] == UUID_A)
    a7 = next(r for r in h["7d"] if r["key"] == UUID_A)
    assert a5 == {"key": UUID_A, "left_pct": 100.0, "resets_at": T0 - 5 * W5 - 100 + 6 * W5}, "five whole windows missed: the sixth is the one running"
    assert a7 == {"key": UUID_A, "left_pct": 100.0, "resets_at": T0 - 3 * W7 - 3600 + 4 * W7}
    for name, period, v, r in (("5h", W5, 80.0, T0 - 5 * W5 - 100), ("7d", W7, 40.0, T0 - 3 * W7 - 3600)):
        pct, nxt, rolled = accounts.window_now(v, r, period, T0)
        got = next(x for x in h[name] if x["key"] == UUID_A)
        assert (round(100 - pct, 1), nxt) == (got["left_pct"], got["resets_at"]) and rolled
    b5 = next(r for r in h["5h"] if r["key"] == UUID_B)
    assert b5 == {"key": UUID_B, "left_pct": 80.0, "resets_at": T0 + 3000}, "a window not yet reset keeps its percentage and its own reset"


def test_the_summarys_headroom_uses_the_same_rule(env):
    from app import usage_summary
    db = env.db
    db.kv_set("accounts", {UUID_A: {"key": UUID_A}, UUID_B: {"key": UUID_B}})
    for key, v, r in ((UUID_A, 90.0, T0 - 2 * W5 - 5), (UUID_B, 30.0, T0 + 600)):
        db.sample("rl_5h", f"acct:{key}", v, {"resets_at": r}, at=T0 - 86400)
        db.sample("rl_7d", f"acct:{key}", v, {"resets_at": T0 + 100000}, at=T0 - 86400)
    s = usage_summary.build(db, days=7, tz_min=0, now=datetime.fromtimestamp(T0, tz=timezone.utc))
    assert s["total"]["headroom_5h"] == [{"key": UUID_A, "left_pct": 100.0}, {"key": UUID_B, "left_pct": 70.0}]
    h = accounts.headroom(db, T0)
    assert [(r["key"], r["left_pct"]) for r in h["5h"]] == [(r["key"], r["left_pct"]) for r in s["total"]["headroom_5h"]]
    assert [(r["key"], r["left_pct"]) for r in h["7d"]] == [(r["key"], r["left_pct"]) for r in s["total"]["headroom_7d"]]


# ================================================================== state.usage: a window that is missing stays on the board
def test_a_missing_window_is_filled_in_from_the_accounts_last_reading(env):
    db = env.db
    one(env)
    db.sample("rl_5h", f"acct:{UUID_A}", 88.0, {"resets_at": T0 - 3600}, at=T0 - 7200)             # the 5-hour window that has since reset
    db.sample("rl_7d", f"acct:{UUID_A}", 71.0, {"resets_at": T0 + 200000, "source": "cache"}, at=T0 - 7200)
    db.kv_set("rate_limits", {"seven_day": {"used_percentage": 72.0, "resets_at": T0 + 200000}}, at=T0 - 60)     # the statusline named only the weekly window
    v = usage.rate_limits_view(db)
    assert v["at"] == iso(T0 - 60) and v["value"]["seven_day"] == {"used_percentage": 72.0, "resets_at": T0 + 200000}, "what the record has stays as it is"
    assert v["value"]["five_hour"] == {"used_percentage": 88.0, "resets_at": T0 - 3600, "at": iso(T0 - 7200), "source": "statusline"}, \
        "the last known number with its reset: the browser turns it into 0 % and the next reset"
    assert usage.rate_limits_view(db) == v


def test_state_usage_keeps_its_shape_when_both_windows_are_there_and_is_none_without_data(env):
    db = env.db
    assert usage.rate_limits_view(db) is None
    full = {"five_hour": {"used_percentage": 42, "resets_at": T0 + 9}, "seven_day": {"used_percentage": 71, "resets_at": T0 + 99}}
    db.kv_set("rate_limits", full)
    assert usage.rate_limits_view(db) == db.kv_get("rate_limits")


def test_a_board_with_readings_but_no_record_still_shows_them(env):
    db = env.db
    one(env)
    db.sample("rl_5h", f"acct:{UUID_A}", 12.0, {"resets_at": T0 + 100}, at=T0 - 30)
    v = usage.rate_limits_view(db)
    assert v["at"] == iso(T0 - 30) and v["value"] == {"five_hour": {"used_percentage": 12.0, "resets_at": T0 + 100, "at": iso(T0 - 30), "source": "statusline"}}
    other = DB(env.tmp / "b.db")
    other.sample("rl_7d", "claude", 5.0, {"resets_at": T0 + 100}, at=T0 - 30)                      # no identity known: the 'claude' series is the current account's
    assert usage.rate_limits_view(other)["value"]["seven_day"]["used_percentage"] == 5.0


def test_the_state_endpoint_carries_it_and_the_account_rows_their_ages(env, lite_client):
    from app import main
    db = main.db
    write_state(settings.claude_config_dir, cache_block())
    accounts.observe(db, T0)
    db.sample("rl_5h", f"acct:{UUID_A}", 88.0, {"resets_at": T0 - 3600}, at=T0 - 7200)
    db.kv_set("rate_limits", {"seven_day": {"used_percentage": 72.0, "resets_at": T0 + 200000}})
    st = lite_client.get("/api/state", headers={"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}).json()
    assert st["usage"]["value"]["five_hour"]["used_percentage"] == 88.0 and st["usage"]["value"]["seven_day"]["used_percentage"] == 72.0
    row = st["accounts"]["list"][0]
    assert row["rl_5h_at"] == iso(T0 - 7200) and row["rl_7d_at"] is None and row["source"] == "statusline"
    assert set(st) >= {"usage", "accounts"}


# ================================================================== the demo fixtures follow the contract
def _demo(name):
    return json.loads((Path(__file__).parent.parent / "app" / "static" / "demo" / name).read_text())


def test_the_demo_state_carries_the_new_account_fields_and_one_reset_window():
    st = _demo("state.json")
    rows_ = st["accounts"]["list"]
    assert all({"rl_5h_at", "rl_7d_at", "source"} <= set(r) for r in rows_), "the same fields view() adds"
    assert {r["source"] for r in rows_} == {"statusline", "cache"}
    epoch = st["demo"]["epoch"]
    work = next(r for r in rows_ if not r["current"])
    cur = next(r for r in rows_ if r["current"])
    assert work["resets_5h"] < epoch, "the second account's 5-hour window had already reset when the fixture was captured: the rolled case"
    assert cur["resets_5h"] > epoch and cur["resets_7d"] > epoch, "the account in use has its windows ahead of it"
    assert set(st["usage"]) == {"value", "at"}, "state.usage keeps the kv record's shape"


def test_the_demo_summary_readings_carry_a_source():
    fx = _demo("usage_summary.json")
    for a in fx["accounts"]:
        for w in ("rl_5h", "rl_7d"):
            if a[w] is not None:
                assert a[w]["source"] in ("statusline", "cache")
