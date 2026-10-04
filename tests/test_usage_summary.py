"""app/usage_summary.build: the Usage page's numbers, all from the samples table, bucketed in local time from tz_min.

The tests seed the real samples table (DB.sample) at known UTC instants and inject `now`, so no clock, no sleeping and no system
zone is involved."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app import usage_summary
from app.db import DB

NOW = datetime(2026, 10, 3, 6, 0, 0, tzinfo=timezone.utc)      # 11:45 on Oct 3 in Kathmandu (+5:45), 01:00 in UTC-5
KTM = 345


@pytest.fixture
def sdb(tmp_path):
    return DB(tmp_path / "usage.db")


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def put(db, series, key, value, when, meta=None):
    db.sample(series, key, value, meta, iso(when))


def cost(db, key, value, when, p="shop", r="api", tok=None, a=None, m=None, acct=None):
    meta = {"p": p, "r": r, "a": a or key.split(":")[0]}
    if tok is not None:
        meta["tok"] = tok
    if m:
        meta["m"] = m
    if acct:
        meta["acct"] = acct
    put(db, "cost", key, value, when, meta)


def day_of(summary, day):
    return next(d for d in summary["daily"] if d["day"] == day)


# ---------- empty and shape ----------

def test_empty_db_is_an_all_zero_payload_with_the_full_shape(sdb):
    s = usage_summary.build(sdb, days=30, tz_min=KTM, now=NOW)
    assert set(s) == {"generated_at", "tz_min", "windows", "daily", "hourly_profile", "heatmap", "top_sessions", "active_hours",
                      "rate_limits", "episodes", "unpriced", "source", "accounts", "total"}
    assert s["source"] == "samples" and s["tz_min"] == 345 and s["generated_at"] == "2026-10-03T06:00:00+00:00"
    assert set(s["windows"]) == {"today", "7d", "30d"}
    for w in s["windows"].values():
        assert w == {"total": 0, "by_agent": {"claude": {"total": 0, "tokens": 0}}, "by_project": []}
    assert len(s["daily"]) == 30 and all(d["zero"] and d["total"] == 0 and d["tokens"] == 0 and d["hours"] == 0 for d in s["daily"])
    assert s["daily"][0]["day"] == "2026-09-04" and s["daily"][-1]["day"] == "2026-10-03"
    assert s["daily"][-1] == {"day": "2026-10-03", "total": 0, "by_agent": {"claude": 0}, "by_project": {}, "tokens": 0, "hours": 0, "zero": True}
    assert s["hourly_profile"] == [0] * 24 and s["heatmap"] == [[0] * 24 for _ in range(7)]
    assert s["top_sessions"] == [] and s["active_hours"] == {} and s["episodes"] == [] and s["unpriced"] == []
    assert s["rate_limits"] == {"claude": {"rl_5h": None, "rl_7d": None}, "by_account": {}}
    assert s["accounts"] == []
    zero = {"total": 0, "tokens": 0, "hours": 0, "sessions": 0}
    assert s["total"] == {"today": zero, "7d": zero, "30d": zero, "accounts": 0, "headroom_5h": [], "headroom_7d": []}
    json.dumps(s)                                       # the endpoint serialises it as is


def test_days_sets_the_daily_length_and_is_clamped(sdb):
    assert len(usage_summary.build(sdb, days=7, tz_min=KTM, now=NOW)["daily"]) == 7
    assert len(usage_summary.build(sdb, days=1, tz_min=KTM, now=NOW)["daily"]) == 1
    assert len(usage_summary.build(sdb, days=0, tz_min=KTM, now=NOW)["daily"]) == 1
    assert len(usage_summary.build(sdb, days=100000, tz_min=KTM, now=NOW)["daily"]) == 365
    assert len(usage_summary.build(sdb, days="x", tz_min=KTM, now=NOW)["daily"]) == 30
    assert usage_summary.build(sdb, tz_min=99999, now=NOW)["tz_min"] == 840
    assert usage_summary.build(sdb, tz_min=None, now=NOW)["tz_min"] == 345
    naive = usage_summary.build(sdb, tz_min=0, now=datetime(2026, 10, 3, 6, 0))
    assert naive["generated_at"] == "2026-10-03T06:00:00+00:00" and naive["daily"][-1]["day"] == "2026-10-03"


# ---------- local-day bucketing ----------

@pytest.mark.parametrize("tz_min,expect_day,today_total", [
    (345, "2026-10-03", 5.0),     # 18:30 UTC + 5:45 = 00:15 the next day: inside Kathmandu's "today"
    (0, "2026-10-02", 0),         # the same instant is still Oct 2 in UTC
    (-300, "2026-10-02", 0),      # 13:30 on Oct 2 in UTC-5
])
def test_a_sample_at_1830_utc_lands_on_the_local_day(sdb, tz_min, expect_day, today_total):
    cost(sdb, "claude:aaaaaaaa-0000-4000-8000-000000000001", 5.0, utc(2026, 10, 2, 18, 30))
    s = usage_summary.build(sdb, days=30, tz_min=tz_min, now=NOW)
    assert s["daily"][-1]["day"] == "2026-10-03"                       # today's local day at 06:00 UTC is Oct 3 for all three zones
    assert [d["day"] for d in s["daily"] if d["total"] > 0] == [expect_day]
    assert day_of(s, expect_day)["total"] == 5.0 and day_of(s, expect_day)["zero"] is False
    assert s["windows"]["today"]["total"] == today_total
    assert s["windows"]["7d"]["total"] == 5.0 and s["windows"]["30d"]["total"] == 5.0


def test_the_local_day_rolls_over_at_local_midnight_not_at_utc_midnight(sdb):
    # 18:14:59 UTC is 23:59:59 in Kathmandu, 18:15:00 UTC is 00:00:00 the next day
    cost(sdb, "claude:aaaaaaaa-0000-4000-8000-000000000001", 1.0, utc(2026, 10, 1, 18, 14) + timedelta(seconds=59))
    cost(sdb, "claude:aaaaaaaa-0000-4000-8000-000000000002", 2.0, utc(2026, 10, 1, 18, 15))
    s = usage_summary.build(sdb, days=7, tz_min=KTM, now=NOW)
    assert day_of(s, "2026-10-01")["total"] == 1.0 and day_of(s, "2026-10-02")["total"] == 2.0


# ---------- cumulative cost -> daily spend ----------

def test_cost_is_cumulative_so_a_day_is_the_delta_not_the_running_total(sdb):
    k = "claude:bbbbbbbb-0000-4000-8000-000000000001"
    cost(sdb, k, 1.0, utc(2026, 9, 30, 8))
    cost(sdb, k, 3.0, utc(2026, 10, 1, 8))
    s = usage_summary.build(sdb, days=30, tz_min=KTM, now=NOW)
    assert day_of(s, "2026-09-30")["total"] == 1.0 and day_of(s, "2026-10-01")["total"] == 2.0
    assert s["windows"]["30d"]["total"] == 3.0                     # not 1 + 3
    assert s["windows"]["30d"]["total"] == round(sum(d["total"] for d in s["daily"]), 4), "a window is the sum of its daily bars"


def test_only_the_last_value_of_a_day_counts_for_that_day(sdb):
    k = "claude:bbbbbbbb-0000-4000-8000-000000000002"
    for hh, v in ((9, 0.5), (11, 1.25), (14, 2.0)):
        cost(sdb, k, v, utc(2026, 10, 2, hh))
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert day_of(s, "2026-10-02")["total"] == 2.0 and s["windows"]["30d"]["total"] == 2.0


def test_a_session_that_began_before_the_window_contributes_only_what_it_spent_inside_it(sdb):
    k = "claude:bbbbbbbb-0000-4000-8000-000000000003"
    cost(sdb, k, 10.0, utc(2026, 8, 20, 8))                             # 44 days ago: baseline outside the 30 d window
    cost(sdb, k, 12.5, utc(2026, 10, 2, 8))
    s = usage_summary.build(sdb, days=30, tz_min=KTM, now=NOW)
    assert s["windows"]["30d"]["total"] == 2.5 and day_of(s, "2026-10-02")["total"] == 2.5


def test_a_dip_in_the_cumulative_value_never_makes_a_negative_day(sdb):
    k = "claude:bbbbbbbb-0000-4000-8000-000000000004"
    cost(sdb, k, 6.0, utc(2026, 10, 1, 8))
    cost(sdb, k, 4.0, utc(2026, 10, 2, 8))                              # re-priced
    cost(sdb, k, 5.0, utc(2026, 10, 3, 1))
    s = usage_summary.build(sdb, days=30, tz_min=KTM, now=NOW)
    assert [day_of(s, d)["total"] for d in ("2026-10-01", "2026-10-02", "2026-10-03")] == [6.0, 0, 1.0]
    assert day_of(s, "2026-10-02")["zero"] is True


def test_samples_after_now_are_ignored(sdb):
    cost(sdb, "claude:bbbbbbbb-0000-4000-8000-000000000005", 9.0, utc(2026, 10, 5, 8))
    s = usage_summary.build(sdb, days=30, tz_min=KTM, now=NOW)
    assert s["windows"]["30d"]["total"] == 0 and s["top_sessions"] == []


# ---------- windows ----------

def test_windows_are_today_and_the_last_7_and_30_local_days_including_today(sdb):
    # local days at tz 0 with now = Oct 3: today Oct 3, 7d starts Sep 27, 30d starts Sep 4
    for i, when in enumerate([utc(2026, 10, 3, 1), utc(2026, 9, 27, 12), utc(2026, 9, 26, 12), utc(2026, 9, 4, 12), utc(2026, 9, 3, 12)]):
        cost(sdb, f"claude:cccccccc-0000-4000-8000-00000000000{i}", 1.0, when)
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert s["windows"]["today"]["total"] == 1.0
    assert s["windows"]["7d"]["total"] == 2.0           # Oct 3 and Sep 27 (Sep 26 is the 8th day back)
    assert s["windows"]["30d"]["total"] == 4.0          # + Sep 26 and Sep 4 (Sep 3 is the 31st day back)
    assert s["daily"][0]["day"] == "2026-09-04"


def test_a_shorter_days_param_does_not_shrink_the_30d_window(sdb):
    cost(sdb, "claude:cccccccc-0000-4000-8000-000000000010", 4.0, utc(2026, 9, 20, 12))
    s = usage_summary.build(sdb, days=7, tz_min=0, now=NOW)
    assert len(s["daily"]) == 7 and s["windows"]["30d"]["total"] == 4.0 and s["windows"]["7d"]["total"] == 0


def test_by_agent_by_project_and_tokens(sdb):
    cost(sdb, "claude:dddddddd-0000-4000-8000-000000000001", 4.0, utc(2026, 10, 1, 8), p="shop", tok=1000)
    cost(sdb, "claude:dddddddd-0000-4000-8000-000000000001", 6.0, utc(2026, 10, 2, 8), p="shop", tok=1600)
    cost(sdb, "claude:dddddddd-0000-4000-8000-000000000002", 1.5, utc(2026, 10, 2, 9), p="blog", tok=300)
    cost(sdb, "codex:dddddddd-0000-4000-8000-000000000003", 0.5, utc(2026, 10, 2, 10), p="shop", tok=70)
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    w = s["windows"]["30d"]
    assert w["total"] == 8.0
    assert w["by_agent"] == {"claude": {"total": 7.5, "tokens": 1900}, "codex": {"total": 0.5, "tokens": 70}}
    assert [(p["project"], p["total"]) for p in w["by_project"]] == [("shop", 6.5), ("blog", 1.5)]
    d2 = day_of(s, "2026-10-02")
    assert d2["total"] == 4.0 and d2["by_agent"] == {"claude": 3.5, "codex": 0.5} and d2["tokens"] == 970
    assert d2["by_project"] == {"shop": 2.5, "blog": 1.5}
    assert all(set(d["by_agent"]) == {"claude", "codex"} for d in s["daily"]), "every day carries every agent, so a stacked chart lines up"
    assert s["windows"]["today"]["by_agent"] == {"claude": {"total": 0, "tokens": 0}, "codex": {"total": 0, "tokens": 0}}


def test_zero_days_are_present_and_flagged(sdb):
    cost(sdb, "claude:dddddddd-0000-4000-8000-000000000004", 2.0, utc(2026, 9, 30, 8))
    cost(sdb, "claude:dddddddd-0000-4000-8000-000000000005", 3.0, utc(2026, 10, 2, 8))
    s = usage_summary.build(sdb, days=7, tz_min=0, now=NOW)
    assert [d["day"] for d in s["daily"]] == ["2026-09-27", "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03"]
    assert [d["zero"] for d in s["daily"]] == [True, True, True, False, True, False, True]


# ---------- hour of day and heatmap ----------

def test_hourly_profile_and_heatmap_use_local_time(sdb):
    # Sat Oct 3 00:15 local (18:30 UTC Oct 2), and Fri Oct 2 21:00 local (15:15 UTC Oct 2): 7 events and 3 events
    put(sdb, "ev", "shop", 7, utc(2026, 10, 2, 18, 30))
    put(sdb, "ev", "blog", 3, utc(2026, 10, 2, 15, 15))
    put(sdb, "ev", "shop", 2, utc(2026, 10, 2, 18, 45))                  # the same local hour 0: adds up
    s = usage_summary.build(sdb, days=30, tz_min=KTM, now=NOW)
    assert s["hourly_profile"][0] == 9 and s["hourly_profile"][21] == 3 and sum(s["hourly_profile"]) == 12
    sat, fri = date(2026, 10, 3).weekday(), date(2026, 10, 2).weekday()
    assert (sat, fri) == (5, 4)
    assert s["heatmap"][sat][0] == 9 and s["heatmap"][fri][21] == 3 and sum(map(sum, s["heatmap"])) == 12
    u = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)             # in UTC the same rows are Fri 18 and Fri 15
    assert u["hourly_profile"][18] == 9 and u["hourly_profile"][15] == 3 and u["heatmap"][fri][18] == 9
    m = usage_summary.build(sdb, days=30, tz_min=-300, now=NOW)          # UTC-5: Fri 13 and Fri 10
    assert m["hourly_profile"][13] == 9 and m["hourly_profile"][10] == 3


def test_hourly_profile_only_covers_the_requested_days(sdb):
    put(sdb, "ev", "shop", 4, utc(2026, 10, 2, 12))
    put(sdb, "ev", "shop", 8, utc(2026, 9, 10, 12))
    assert sum(usage_summary.build(sdb, days=7, tz_min=0, now=NOW)["hourly_profile"]) == 4
    assert sum(usage_summary.build(sdb, days=30, tz_min=0, now=NOW)["hourly_profile"]) == 12


# ---------- top sessions ----------

def test_top_sessions_are_ordered_by_cumulative_cost_and_capped_at_ten(sdb):
    for i in range(12):
        cost(sdb, f"claude:eeeeeeee-0000-4000-8000-0000000000{i:02d}", float(i + 1), utc(2026, 10, 2, 8), p=f"proj{i % 3}", r=f"repo{i}")
    cost(sdb, "claude:eeeeeeee-0000-4000-8000-0000000000zz", 0.0, utc(2026, 10, 2, 9))        # free: not a top session
    cost(sdb, "claude:eeeeeeee-0000-4000-8000-0000000000old", 500.0, utc(2026, 5, 1, 9))      # last sampled long before the window
    s = usage_summary.build(sdb, days=30, tz_min=KTM, now=NOW)
    top = s["top_sessions"]
    assert len(top) == 10
    assert [t["total"] for t in top] == [12.0, 11.0, 10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0]
    assert top[0] == {"key": "claude:eeeeeeee-0000-4000-8000-000000000011", "project": "proj2", "repo": "repo11", "agent": "claude",
                      "total": 12.0, "hours": 0, "tokens": 0, "models": []}


def test_top_session_total_is_the_last_cumulative_value_and_hours_follow_the_rises(sdb):
    k = "claude:eeeeeeee-0000-4000-8000-000000000100"
    # value rises at 08:00, 08:10, 08:20 (two 10-minute gaps count), is flat at 08:30, rises again at 11:00 (3 h gap does not count)
    for hh, mi, v in ((8, 0, 1.0), (8, 10, 2.0), (8, 20, 3.0), (8, 30, 3.0), (11, 0, 4.0)):
        cost(sdb, k, v, utc(2026, 10, 2, hh, mi), p="shop", r="api")
    s = usage_summary.build(sdb, days=30, tz_min=KTM, now=NOW)
    assert s["top_sessions"] == [{"key": k, "project": "shop", "repo": "api", "agent": "claude", "total": 4.0, "hours": 0.33,
                                  "tokens": 0, "models": []}]


def test_top_sessions_carry_their_tokens_and_models(sdb):
    k = "claude:eeeeeeee-0000-4000-8000-000000000103"
    cost(sdb, k, 3.0, utc(2026, 10, 1, 8), tok=1_000_000, m=["opus-5"])
    cost(sdb, k, 7.5, utc(2026, 10, 2, 8), tok=2_500_000, m=["opus-5", "haiku-4-5"])
    s = usage_summary.build(sdb, days=30, tz_min=KTM, now=NOW)
    assert (s["top_sessions"][0]["tokens"], s["top_sessions"][0]["models"], s["top_sessions"][0]["total"]) == (2_500_000, ["opus-5", "haiku-4-5"], 7.5)


def test_the_agent_and_project_come_from_the_newest_meta(sdb):
    k = "claude:eeeeeeee-0000-4000-8000-000000000101"
    put(sdb, "cost", k, 1.0, utc(2026, 10, 1, 8), {"p": "old", "r": "x", "a": "claude"})
    put(sdb, "cost", k, 2.0, utc(2026, 10, 2, 8), {"p": "new", "r": "y", "a": "claude"})
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert [p["project"] for p in s["windows"]["30d"]["by_project"]] == ["new"]
    assert s["top_sessions"][0]["project"] == "new" and s["top_sessions"][0]["repo"] == "y"


def test_a_key_without_a_project_is_unattributed(sdb):
    put(sdb, "cost", "claude:eeeeeeee-0000-4000-8000-000000000102", 2.0, utc(2026, 10, 2, 8), None)
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert s["windows"]["30d"]["by_project"][0]["project"] == "(unattributed)" and s["top_sessions"][0]["project"] == "(unattributed)"


# ---------- active hours from state-event gaps ----------

def state(db, tmux, when, value=1, p="shop", acct=None, a="claude"):
    meta = {"p": p, "r": "api", "s": tmux, "a": a}
    if acct:
        meta["acct"] = acct
    put(db, "state", tmux, value, when, meta)


def test_active_hours_count_gaps_of_at_most_15_minutes(sdb):
    t = utc(2026, 10, 2, 8, 0)
    for dt in (0, 10, 40):                                              # gaps 10 min (counts) and 30 min (does not)
        state(sdb, "shop--api--s1", t + timedelta(minutes=dt))
    s = usage_summary.build(sdb, days=30, tz_min=KTM, now=NOW)
    assert s["active_hours"] == {"shop": round(10 / 60, 2)}
    assert s["windows"]["30d"]["by_project"] == [{"project": "shop", "total": 0, "hours": 0.17}]
    assert day_of(s, "2026-10-02")["hours"] == 0.17


def test_a_gap_of_exactly_15_minutes_counts_and_one_second_more_does_not(sdb):
    t = utc(2026, 10, 2, 8, 0)
    state(sdb, "shop--api--s1", t)
    state(sdb, "shop--api--s1", t + timedelta(minutes=15))
    state(sdb, "blog--site--s1", t, p="blog")
    state(sdb, "blog--site--s1", t + timedelta(minutes=15, seconds=1), p="blog")
    s = usage_summary.build(sdb, days=30, tz_min=KTM, now=NOW)
    assert s["active_hours"] == {"shop": 0.25}


def test_gaps_are_taken_per_session_so_interleaved_sessions_do_not_hide_a_pause(sdb):
    t = utc(2026, 10, 2, 8, 0)
    # s1 works at 8:00 and 8:50 (a 50 min pause), s2 pings at 8:20 and 8:35 in between: s1's pause must not be filled in
    state(sdb, "shop--api--s1", t)
    state(sdb, "shop--api--s2", t + timedelta(minutes=20))
    state(sdb, "shop--api--s2", t + timedelta(minutes=35))
    state(sdb, "shop--api--s1", t + timedelta(minutes=50))
    s = usage_summary.build(sdb, days=30, tz_min=KTM, now=NOW)
    assert s["active_hours"] == {"shop": 0.25}                          # only s2's 15 minutes


def test_active_time_is_split_at_local_midnight_and_attributed_by_project(sdb):
    # 23:55 -> 00:05 local (18:10 -> 18:20 UTC): 5 minutes on each side of Kathmandu midnight
    state(sdb, "shop--api--s1", utc(2026, 10, 1, 18, 10))
    state(sdb, "shop--api--s1", utc(2026, 10, 1, 18, 20))
    state(sdb, "blog--site--s1", utc(2026, 10, 2, 8, 0), p="blog")
    state(sdb, "blog--site--s1", utc(2026, 10, 2, 8, 12), p="blog")
    s = usage_summary.build(sdb, days=30, tz_min=KTM, now=NOW)
    assert day_of(s, "2026-10-01")["hours"] == round(5 / 60, 2) and day_of(s, "2026-10-02")["hours"] == round((5 + 12) / 60, 2)
    assert s["active_hours"] == {"blog": 0.2, "shop": 0.17}
    assert [(p["project"], p["hours"]) for p in s["windows"]["7d"]["by_project"]] == [("blog", 0.2), ("shop", 0.17)]
    assert s["windows"]["today"]["by_project"] == []                    # nothing on Oct 3 yet


def test_by_project_lists_projects_with_hours_but_no_cost_and_orders_by_cost(sdb):
    cost(sdb, "claude:ffffffff-0000-4000-8000-000000000001", 5.0, utc(2026, 10, 2, 8), p="shop")
    cost(sdb, "claude:ffffffff-0000-4000-8000-000000000002", 9.0, utc(2026, 10, 2, 8), p="blog")
    state(sdb, "quiet--x--s1", utc(2026, 10, 2, 8, 0), p="quiet")
    state(sdb, "quiet--x--s1", utc(2026, 10, 2, 8, 6), p="quiet")
    s = usage_summary.build(sdb, days=30, tz_min=KTM, now=NOW)
    assert [(p["project"], p["total"], p["hours"]) for p in s["windows"]["30d"]["by_project"]] == [("blog", 9.0, 0), ("shop", 5.0, 0), ("quiet", 0, 0.1)]


# ---------- rate limits, episodes, unpriced ----------

def test_rate_limits_are_the_last_samples(sdb):
    put(sdb, "rl_5h", "claude", 40.0, utc(2026, 10, 3, 4, 0), {"resets_at": 1791006000})
    put(sdb, "rl_5h", "claude", 42.0, utc(2026, 10, 3, 5, 0), {"resets_at": 1791010000})
    put(sdb, "rl_7d", "claude", 71.0, utc(2026, 10, 3, 5, 30), {"resets_at": 1791270000})
    put(sdb, "rl_5h", "codex", 9.0, utc(2026, 10, 3, 5, 30), {"resets_at": 1})
    r = usage_summary.build(sdb, days=30, tz_min=KTM, now=NOW)["rate_limits"]
    assert set(r) == {"claude", "by_account"} and r["by_account"] == {}, "agent keys other than claude are not rate limits of a subscription account"
    assert r["claude"]["rl_5h"] == {"at": "2026-10-03T05:00:00+00:00", "value": 42.0, "meta": {"resets_at": 1791010000}}
    assert r["claude"]["rl_7d"] == {"at": "2026-10-03T05:30:00+00:00", "value": 71.0, "meta": {"resets_at": 1791270000}}


def test_episodes_are_the_lim_events_of_the_last_30_days(sdb):
    put(sdb, "lim", "7d", 1, utc(2026, 8, 1, 3), {"session": "old--x--s1", "resets_at": 1})                # too old
    put(sdb, "lim", "5h", 1, utc(2026, 10, 1, 17, 30), {"session": "shop--api--s1", "resets_at": 1791006000, "message": "You've hit your session limit"})
    put(sdb, "lim", "7d", 1, utc(2026, 10, 2, 22, 0), {"session": "blog--site--s1", "resets_at": 1791270000})
    put(sdb, "lim", "other", 1, utc(2026, 10, 3, 1, 0), {"session": "x"})
    s = usage_summary.build(sdb, days=7, tz_min=KTM, now=NOW)            # `days` does not shorten the episode history
    assert s["episodes"] == [
        {"kind": "5h", "at": "2026-10-01T17:30:00+00:00", "resets_at": 1791006000, "session": "shop--api--s1", "acct": None},
        {"kind": "7d", "at": "2026-10-02T22:00:00+00:00", "resets_at": 1791270000, "session": "blog--site--s1", "acct": None},
        {"kind": "other", "at": "2026-10-03T01:00:00+00:00", "resets_at": None, "session": "x", "acct": None},
    ]


def test_unpriced_comes_from_the_kv_cost_record(sdb):
    assert usage_summary.build(sdb, now=NOW)["unpriced"] == []
    sdb.kv_set("cost", {"projects": {}, "tasks": {}})
    assert usage_summary.build(sdb, now=NOW)["unpriced"] == []           # a record that names none
    sdb.kv_set("cost", {"projects": {}, "unpriced": {"sessions": 2, "tokens": 9000, "top": [
        {"id": "aaaaaaaa-0000-4000-8000-000000000001", "agent": "claude", "project": "shop", "repo": "api", "tokens": 1500, "models": ["opus-5"]},
        {"id": "aaaaaaaa-0000-4000-8000-000000000002", "agent": "claude", "project": None, "repo": None, "tokens": 7500, "models": ["fable-5-1", 3]},
    ]}})
    assert usage_summary.build(sdb, now=NOW)["unpriced"] == [
        {"key": "claude:aaaaaaaa-0000-4000-8000-000000000002", "agent": "claude", "project": None, "repo": None, "tokens": 7500, "models": ["fable-5-1"]},
        {"key": "claude:aaaaaaaa-0000-4000-8000-000000000001", "agent": "claude", "project": "shop", "repo": "api", "tokens": 1500, "models": ["opus-5"]},
    ]
    sdb.kv_set("cost", {"unpriced": [{"key": "codex:abc", "tokens": 5, "model": "gpt-5.5"}, "plain-id", 7]})      # a bare list, loosely typed
    assert usage_summary.build(sdb, now=NOW)["unpriced"] == [
        {"key": "codex:abc", "agent": "codex", "project": None, "repo": None, "tokens": 5, "models": ["gpt-5.5"]},
        {"key": "claude:plain-id", "agent": "claude", "project": None, "repo": None, "tokens": 0, "models": []},
    ]
    sdb.kv_set("cost", {"unpriced": "garbage"})
    assert usage_summary.build(sdb, now=NOW)["unpriced"] == []


def test_bad_rows_never_break_the_summary(sdb):
    put(sdb, "cost", "claude:x", None, utc(2026, 10, 2, 8), {"p": "shop"})
    sdb.sample("cost", "claude:y", 1.0, "{not json", iso(utc(2026, 10, 2, 9)))
    put(sdb, "state", "t", 1, utc(2026, 10, 2, 8), None)
    put(sdb, "ev", "shop", None, utc(2026, 10, 2, 8))
    s = usage_summary.build(sdb, days=30, tz_min=KTM, now=NOW)
    assert s["windows"]["30d"]["total"] == 1.0 and sum(s["hourly_profile"]) == 0       # the one row with a number still counts
    assert s["windows"]["30d"]["by_project"][0]["project"] == "(unattributed)"             # its meta text is not JSON


# ---------- accounts: which subscription account used what, and the total ----------

A, B, C = "acc-a-0000", "acc-b-0000", "acc-c-0000"
NOW_TS = int(NOW.timestamp())
ZERO = {"total": 0, "tokens": 0, "hours": 0, "sessions": 0}


def acct_event(db, key, when, frm=None):
    put(db, "acct", key, 1, when, {"from": frm, "to": key})


def by_key(summary):
    return {a["key"]: a for a in summary["accounts"]}


def check_totals(s):
    """The invariants the Usage page relies on: total = the accounts added up = the claude agent's window = the daily bars."""
    claude_days = {w: n for w, n in (("today", 1), ("7d", 7), ("30d", 30))}
    for w, n in claude_days.items():
        rows = [a["windows"][w] for a in s["accounts"]]
        t = s["total"][w]
        assert t["total"] == pytest.approx(sum(r["total"] for r in rows), abs=1e-9)
        assert t["tokens"] == sum(r["tokens"] for r in rows)
        assert t["hours"] == pytest.approx(sum(r["hours"] for r in rows), abs=1e-9)
        assert max([r["sessions"] for r in rows] or [0]) <= t["sessions"] <= sum(r["sessions"] for r in rows)
        assert t["total"] == pytest.approx(s["windows"][w]["by_agent"]["claude"]["total"], abs=1e-3 * max(1, len(rows)))
        assert t["tokens"] == s["windows"][w]["by_agent"]["claude"]["tokens"]
        assert t["total"] == pytest.approx(sum(d["total"] for d in s["daily"][-n:]) - sum(
            v for d in s["daily"][-n:] for k, v in d["by_agent"].items() if k != "claude"), abs=1e-3 * max(1, len(rows)))
    real = [a for a in s["accounts"] if a["key"] != usage_summary.UNKNOWN_ACCOUNT]
    assert s["total"]["accounts"] == len(real)


def test_two_accounts_with_overlapping_days_split_spend_tokens_hours_and_sessions(sdb):
    cost(sdb, "claude:a1111111-0000-4000-8000-000000000001", 2.0, utc(2026, 10, 1, 8), tok=1000, acct=A)
    cost(sdb, "claude:a1111111-0000-4000-8000-000000000001", 5.0, utc(2026, 10, 2, 8), tok=2500, acct=A)
    cost(sdb, "claude:a1111111-0000-4000-8000-000000000002", 1.0, utc(2026, 10, 2, 9), tok=400, acct=B)
    cost(sdb, "claude:a1111111-0000-4000-8000-000000000002", 4.0, utc(2026, 10, 3, 1), tok=1900, acct=B)
    cost(sdb, "claude:a1111111-0000-4000-8000-000000000003", 0.5, utc(2026, 10, 2, 10), tok=200, acct=A)     # a second session of A on Oct 2
    state(sdb, "shop--api--s1", utc(2026, 10, 2, 8, 0), acct=A)
    state(sdb, "shop--api--s1", utc(2026, 10, 2, 8, 10), acct=A)
    state(sdb, "shop--api--s2", utc(2026, 10, 3, 1, 0), acct=B)
    state(sdb, "shop--api--s2", utc(2026, 10, 3, 1, 6), acct=B)
    sdb.kv_set("accounts", {A: {"key": A, "email": "a@example.com", "name": "Ann", "plan": "max"},
                            B: {"key": B, "email": "b@example.com", "name": "Bob", "plan": "pro", "label": "work"}})
    sdb.kv_set("account_current", {"key": B, "since": "2026-10-03T00:30:00+00:00"})
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert [a["key"] for a in s["accounts"]] == [B, A], "the current account first"
    b, a = s["accounts"]
    assert (b["current"], a["current"]) == (True, False)
    assert (b["email"], b["name"], b["label"], b["plan"]) == ("b@example.com", "Bob", "work", "pro")
    assert (a["email"], a["name"], a["label"], a["plan"]) == ("a@example.com", "Ann", None, "max")
    assert b["windows"] == {"today": {"total": 3.0, "tokens": 1500, "hours": 0.1, "sessions": 1},
                            "7d": {"total": 4.0, "tokens": 1900, "hours": 0.1, "sessions": 1},
                            "30d": {"total": 4.0, "tokens": 1900, "hours": 0.1, "sessions": 1}}
    assert a["windows"]["today"] == ZERO
    assert a["windows"]["7d"] == {"total": 5.5, "tokens": 2700, "hours": 0.17, "sessions": 2}
    assert a["windows"]["30d"] == a["windows"]["7d"]
    assert s["total"]["today"] == {"total": 3.0, "tokens": 1500, "hours": 0.1, "sessions": 1}
    assert s["total"]["7d"] == {"total": 9.5, "tokens": 4600, "hours": 0.27, "sessions": 3}
    assert s["total"]["accounts"] == 2
    # the day the two accounts overlap (Oct 2): A spent 3.0 + 0.5, B 1.0, and the daily bar is their sum
    assert day_of(s, "2026-10-02")["total"] == 4.5 and day_of(s, "2026-10-01")["total"] == 2.0 and day_of(s, "2026-10-03")["total"] == 3.0
    check_totals(s)
    json.dumps(s)


def test_a_mid_day_switch_splits_the_day_of_one_session_between_the_accounts(sdb):
    k = "claude:a2222222-0000-4000-8000-000000000001"
    cost(sdb, k, 1.0, utc(2026, 9, 30, 9), tok=100, acct=A)                    # baseline on an earlier day
    cost(sdb, k, 3.0, utc(2026, 10, 2, 8), tok=300, acct=A)
    cost(sdb, k, 6.0, utc(2026, 10, 2, 10), tok=700, acct=A)
    cost(sdb, k, 9.0, utc(2026, 10, 2, 14), tok=1000, acct=B)                  # /login to B in the afternoon
    cost(sdb, k, 10.0, utc(2026, 10, 3, 1), tok=1100, acct=B)
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert day_of(s, "2026-10-02")["total"] == 8.0 and day_of(s, "2026-10-02")["tokens"] == 900
    a, b = by_key(s)[A], by_key(s)[B]
    assert a["windows"]["30d"] == {"total": 6.0, "tokens": 700, "hours": 0, "sessions": 1}      # 1.0 on Sep 30 + 2.0 + 3.0 on Oct 2
    assert b["windows"]["30d"] == {"total": 4.0, "tokens": 400, "hours": 0, "sessions": 1}      # 3.0 on Oct 2 + 1.0 on Oct 3
    assert b["windows"]["today"] == {"total": 1.0, "tokens": 100, "hours": 0, "sessions": 1}
    assert a["windows"]["today"] == ZERO
    assert s["total"]["30d"]["sessions"] == 1, "one session that straddled the switch counts once in the total"
    assert s["total"]["30d"]["total"] == 10.0
    check_totals(s)


def test_a_repriced_dip_inside_a_day_never_changes_the_days_total(sdb):
    k = "claude:a2222222-0000-4000-8000-000000000002"
    cost(sdb, k, 5.0, utc(2026, 10, 2, 8), acct=A)
    cost(sdb, k, 3.0, utc(2026, 10, 2, 9), acct=B)                              # re-priced downwards under B
    cost(sdb, k, 8.0, utc(2026, 10, 2, 10), acct=B)
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert day_of(s, "2026-10-02")["total"] == 8.0
    assert by_key(s)[A]["windows"]["30d"]["total"] == 5.0 and by_key(s)[B]["windows"]["30d"]["total"] == 3.0
    check_totals(s)


def test_a_dip_across_days_stays_clamped_per_account(sdb):
    k = "claude:a2222222-0000-4000-8000-000000000003"
    cost(sdb, k, 6.0, utc(2026, 10, 1, 8), acct=A)
    cost(sdb, k, 4.0, utc(2026, 10, 2, 8), acct=B)                              # re-priced on the next day: that day is 0
    cost(sdb, k, 5.0, utc(2026, 10, 3, 1), acct=B)
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert by_key(s)[A]["windows"]["30d"]["total"] == 6.0 and by_key(s)[B]["windows"]["30d"]["total"] == 1.0
    assert by_key(s)[B]["windows"]["7d"]["sessions"] == 1
    check_totals(s)


def test_the_samples_own_acct_beats_the_timeline(sdb):
    acct_event(sdb, B, utc(2026, 10, 1, 0))
    cost(sdb, "claude:a3333333-0000-4000-8000-000000000001", 2.0, utc(2026, 10, 2, 8), acct=A)
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert by_key(s)[A]["windows"]["30d"]["total"] == 2.0 and by_key(s)[B]["windows"]["30d"]["total"] == 0


# ---------- history without acct: the timeline, then the unknown bucket ----------

def test_history_before_account_tracking_goes_to_the_unknown_bucket_and_later_samples_follow_the_timeline(sdb):
    acct_event(sdb, A, utc(2026, 10, 2, 0))                                       # the first observation: nothing known before it
    cost(sdb, "claude:a4444444-0000-4000-8000-000000000001", 3.0, utc(2026, 9, 28, 8), tok=300)                # before tracking, no acct
    cost(sdb, "claude:a4444444-0000-4000-8000-000000000002", 2.0, utc(2026, 10, 2, 8), tok=200)                # after it, no acct: A is current
    state(sdb, "shop--api--s1", utc(2026, 9, 28, 8, 0))
    state(sdb, "shop--api--s1", utc(2026, 9, 28, 8, 12))
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert [a["key"] for a in s["accounts"]] == [A, "unknown"], "the bucket is last"
    a, unk = s["accounts"]
    assert a["current"] is True, "no kv account_current: the newest acct event says who is current"
    assert (unk["key"], unk["name"], unk["current"], unk["email"], unk["plan"]) == ("unknown", "(before account tracking)", False, None, None)
    assert unk["rl_5h"] is None and unk["rl_7d"] is None
    assert a["windows"]["30d"] == {"total": 2.0, "tokens": 200, "hours": 0, "sessions": 1}
    assert unk["windows"]["7d"] == {"total": 3.0, "tokens": 300, "hours": 0.2, "sessions": 1}
    assert unk["windows"]["today"] == ZERO
    assert s["total"]["30d"] == {"total": 5.0, "tokens": 500, "hours": 0.2, "sessions": 2}
    assert s["total"]["accounts"] == 1, "the bucket is history, not an account"
    assert "unknown" not in s["rate_limits"]["by_account"]
    assert s["total"]["headroom_5h"] == [] and s["total"]["headroom_7d"] == []
    check_totals(s)


def test_a_first_event_that_names_the_previous_account_covers_the_history_before_it(sdb):
    acct_event(sdb, B, utc(2026, 10, 1, 12), frm=A)
    cost(sdb, "claude:a4444444-0000-4000-8000-000000000003", 4.0, utc(2026, 9, 29, 8))                          # before the event: A
    cost(sdb, "claude:a4444444-0000-4000-8000-000000000004", 1.5, utc(2026, 10, 2, 8))                          # after it: B
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert [a["key"] for a in s["accounts"]] == [B, A]                              # B is the newest event's key: current
    assert by_key(s)[A]["windows"]["30d"]["total"] == 4.0 and by_key(s)[B]["windows"]["30d"]["total"] == 1.5
    assert "unknown" not in by_key(s)
    check_totals(s)


def test_a_mid_history_switch_attributes_samples_by_the_account_current_at_their_time(sdb):
    acct_event(sdb, A, utc(2026, 9, 1))
    acct_event(sdb, B, utc(2026, 9, 20, 12), frm=A)
    acct_event(sdb, A, utc(2026, 9, 25, 12), frm=B)
    for i, (when, v) in enumerate(((utc(2026, 9, 10, 8), 1.0), (utc(2026, 9, 21, 8), 2.0), (utc(2026, 9, 26, 8), 4.0))):
        cost(sdb, f"claude:a5555555-0000-4000-8000-00000000000{i}", v, when)
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert by_key(s)[A]["windows"]["30d"]["total"] == 5.0 and by_key(s)[B]["windows"]["30d"]["total"] == 2.0
    assert [a["key"] for a in s["accounts"]] == [A, B] and s["accounts"][0]["current"] is True
    check_totals(s)


def test_no_account_information_at_all_is_one_unknown_bucket_that_still_adds_up(sdb):
    cost(sdb, "claude:a6666666-0000-4000-8000-000000000001", 7.0, utc(2026, 10, 2, 8), tok=70)
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert [a["key"] for a in s["accounts"]] == ["unknown"] and s["accounts"][0]["current"] is False
    assert s["total"]["30d"] == {"total": 7.0, "tokens": 70, "hours": 0, "sessions": 1} and s["total"]["accounts"] == 0
    check_totals(s)


def test_only_claude_usage_is_attributed_to_subscription_accounts(sdb):
    cost(sdb, "claude:a7777777-0000-4000-8000-000000000001", 2.0, utc(2026, 10, 2, 8), tok=20, acct=A)
    cost(sdb, "codex:a7777777-0000-4000-8000-000000000002", 9.0, utc(2026, 10, 2, 8), tok=90, acct=A)          # a stray acct tag on a Codex sample
    state(sdb, "shop--api--s1", utc(2026, 10, 2, 8, 0), acct=A)
    state(sdb, "shop--api--s1", utc(2026, 10, 2, 8, 6), acct=A)
    state(sdb, "shop--api--x1", utc(2026, 10, 2, 8, 0), acct=A, a="codex")
    state(sdb, "shop--api--x1", utc(2026, 10, 2, 8, 12), acct=A, a="codex")
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert s["windows"]["30d"]["total"] == 11.0, "the all-agent windows keep Codex"
    assert by_key(s)[A]["windows"]["30d"] == {"total": 2.0, "tokens": 20, "hours": 0.1, "sessions": 1}
    assert s["total"]["30d"] == {"total": 2.0, "tokens": 20, "hours": 0.1, "sessions": 1}
    check_totals(s)


# ---------- windows, order, days ----------

def test_account_windows_follow_the_same_local_day_ranges_as_the_daily_bars(sdb):
    # tz 0, now = Oct 3: 7d starts Sep 27, 30d starts Sep 4
    for i, (acct, when) in enumerate(((A, utc(2026, 10, 3, 1)), (A, utc(2026, 9, 27, 12)), (A, utc(2026, 9, 26, 12)),
                                      (B, utc(2026, 9, 4, 12)), (B, utc(2026, 9, 3, 12)))):
        cost(sdb, f"claude:a8888888-0000-4000-8000-00000000000{i}", 1.0, when, tok=10, acct=acct)
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    a, b = by_key(s)[A], by_key(s)[B]
    assert [a["windows"][w]["total"] for w in ("today", "7d", "30d")] == [1.0, 2.0, 3.0]
    assert [b["windows"][w]["total"] for w in ("today", "7d", "30d")] == [0, 0, 1.0]
    assert [b["windows"][w]["sessions"] for w in ("today", "7d", "30d")] == [0, 0, 1]
    assert [s["total"][w]["total"] for w in ("today", "7d", "30d")] == [1.0, 2.0, 4.0]
    check_totals(s)


def test_the_local_day_decides_which_window_a_switch_spend_belongs_to(sdb):
    cost(sdb, "claude:a8888888-0000-4000-8000-000000000010", 2.0, utc(2026, 10, 2, 18, 30), acct=B)            # 00:15 Oct 3 in Kathmandu
    s = usage_summary.build(sdb, days=30, tz_min=KTM, now=NOW)
    assert by_key(s)[B]["windows"]["today"]["total"] == 2.0
    u = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert by_key(u)[B]["windows"]["today"]["total"] == 0 and by_key(u)[B]["windows"]["7d"]["total"] == 2.0


def test_a_shorter_days_param_does_not_change_the_account_windows(sdb):
    cost(sdb, "claude:a8888888-0000-4000-8000-000000000011", 4.0, utc(2026, 9, 20, 12), tok=40, acct=A)
    week = usage_summary.build(sdb, days=7, tz_min=0, now=NOW)
    month = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert week["accounts"] == month["accounts"] and week["total"] == month["total"]
    assert by_key(week)[A]["windows"]["30d"]["total"] == 4.0 and by_key(week)[A]["windows"]["7d"]["total"] == 0


def test_accounts_sort_current_first_then_by_7d_total_and_the_bucket_last(sdb):
    sdb.kv_set("accounts", {x: {"key": x} for x in (A, B, C)})
    sdb.kv_set("account_current", {"key": C})
    cost(sdb, "claude:a9999999-0000-4000-8000-000000000001", 1.0, utc(2026, 10, 2, 8), acct=A)
    cost(sdb, "claude:a9999999-0000-4000-8000-000000000002", 6.0, utc(2026, 10, 2, 8), acct=B)
    cost(sdb, "claude:a9999999-0000-4000-8000-000000000003", 50.0, utc(2026, 10, 2, 8))                          # unknown, the biggest
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert [a["key"] for a in s["accounts"]] == [C, B, A, "unknown"]
    check_totals(s)


# ---------- rate limits and headroom ----------

def test_per_account_windows_headroom_and_by_account_rate_limits(sdb):
    sdb.kv_set("accounts", {x: {"key": x, "email": f"{x}@example.com"} for x in (A, B, C, "acc-d-0000")})
    sdb.kv_set("account_current", {"key": A})
    put(sdb, "rl_5h", "claude", 42.0, utc(2026, 10, 3, 5, 0), {"resets_at": NOW_TS + 3600})                    # the current-account series is untouched
    put(sdb, "rl_5h", f"acct:{A}", 42.0, utc(2026, 10, 3, 5, 0), {"resets_at": NOW_TS + 3600})
    put(sdb, "rl_7d", f"acct:{A}", 71.0, utc(2026, 10, 3, 5, 30), {"resets_at": NOW_TS + 86400})
    put(sdb, "rl_5h", f"acct:{B}", 11.0, utc(2026, 10, 3, 3, 0), {"resets_at": NOW_TS + 7200})
    put(sdb, "rl_5h", f"acct:{B}", 12.0, utc(2026, 10, 3, 4, 0), {"resets_at": NOW_TS + 7200})
    put(sdb, "rl_7d", f"acct:{B}", 38.0, utc(2026, 10, 3, 4, 0), {"resets_at": NOW_TS + 200000})
    put(sdb, "rl_5h", f"acct:{C}", 90.0, utc(2026, 10, 2, 22, 0), {"resets_at": NOW_TS})                       # its window reset exactly now: all of it is free again
    put(sdb, "rl_7d", f"acct:{C}", 55.0, utc(2026, 10, 2, 22, 0), {"resets_at": NOW_TS + 100000})
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert [a["key"] for a in s["accounts"]] == [A, B, C, "acc-d-0000"]
    a, b, c, d = s["accounts"]
    assert a["rl_5h"] == {"value": 42.0, "resets_at": NOW_TS + 3600, "at": "2026-10-03T05:00:00+00:00"}
    assert a["rl_7d"] == {"value": 71.0, "resets_at": NOW_TS + 86400, "at": "2026-10-03T05:30:00+00:00"}
    assert b["rl_5h"]["value"] == 12.0 and b["rl_5h"]["resets_at"] == NOW_TS + 7200, "the newest reading"
    assert d["rl_5h"] is None and d["rl_7d"] is None and d["email"] == "acc-d-0000@example.com"
    assert s["total"]["headroom_5h"] == [{"key": C, "left_pct": 100.0}, {"key": B, "left_pct": 88.0}, {"key": A, "left_pct": 58.0}]
    assert s["total"]["headroom_7d"] == [{"key": B, "left_pct": 62.0}, {"key": C, "left_pct": 45.0}, {"key": A, "left_pct": 29.0}]
    assert s["total"]["accounts"] == 4
    r = s["rate_limits"]
    assert r["claude"]["rl_5h"] == {"at": "2026-10-03T05:00:00+00:00", "value": 42.0, "meta": {"resets_at": NOW_TS + 3600}}
    assert r["claude"]["rl_7d"] is None, "the claude series keeps exactly what it had"
    assert set(r["by_account"]) == {A, B, C, "acc-d-0000"}
    assert r["by_account"][B]["rl_7d"] == {"at": "2026-10-03T04:00:00+00:00", "value": 38.0, "meta": {"resets_at": NOW_TS + 200000}}
    assert r["by_account"]["acc-d-0000"] == {"rl_5h": None, "rl_7d": None}
    assert a["last_seen"] == "2026-10-03T05:30:00+00:00", "no kv last_seen: the newest reading says"


def test_equal_headroom_lists_the_current_account_first(sdb):
    sdb.kv_set("accounts", {A: {}, B: {}, C: {}})
    sdb.kv_set("account_current", {"key": C})
    for x in (A, B, C):
        put(sdb, "rl_5h", f"acct:{x}", 30.25, utc(2026, 10, 3, 5, 0), {"resets_at": NOW_TS + 3600})
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert s["total"]["headroom_5h"] == [{"key": C, "left_pct": 69.8}, {"key": A, "left_pct": 69.8}, {"key": B, "left_pct": 69.8}]


def test_a_window_with_a_just_passed_reset_reads_as_full_headroom_and_a_full_window_as_zero(sdb):
    sdb.kv_set("accounts", {A: {}, B: {}})
    put(sdb, "rl_5h", f"acct:{A}", 100.0, utc(2026, 10, 3, 5, 0), {"resets_at": NOW_TS + 60})
    put(sdb, "rl_5h", f"acct:{B}", 100.0, utc(2026, 10, 3, 1, 0), {"resets_at": NOW_TS - 1})
    put(sdb, "rl_7d", f"acct:{B}", 130.0, utc(2026, 10, 3, 1, 0), None)                                         # over 100 and no reset time: clamped, not stale
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert s["total"]["headroom_5h"] == [{"key": B, "left_pct": 100.0}, {"key": A, "left_pct": 0.0}]
    assert s["total"]["headroom_7d"] == [{"key": B, "left_pct": 0.0}]
    assert by_key(s)[B]["rl_7d"] == {"value": 130.0, "resets_at": None, "at": "2026-10-03T01:00:00+00:00"}


# ---------- episodes per account ----------

def test_episodes_carry_their_account_and_are_counted_per_account(sdb):
    acct_event(sdb, B, utc(2026, 10, 1, 0), frm=A)
    put(sdb, "lim", "5h", 1, utc(2026, 9, 30, 12), {"session": "s0", "resets_at": 1})                              # before the first event: A (its `from`)
    put(sdb, "lim", "5h", 1, utc(2026, 10, 1, 17, 30), {"session": "s1", "resets_at": 1791006000, "acct": A})      # the meta says A even though B is current
    put(sdb, "lim", "7d", 1, utc(2026, 10, 2, 22, 0), {"session": "s2", "resets_at": 1791270000})                  # no acct: the timeline says B
    put(sdb, "lim", "other", 1, utc(2026, 10, 3, 1, 0), {"session": "s3", "acct": B})
    s = usage_summary.build(sdb, days=7, tz_min=0, now=NOW)
    assert [(e["session"], e["acct"]) for e in s["episodes"]] == [("s0", A), ("s1", A), ("s2", B), ("s3", B)]
    assert (by_key(s)[A]["episodes"], by_key(s)[B]["episodes"]) == (2, 2)


def test_an_episode_nobody_can_place_counts_for_the_unknown_bucket(sdb):
    put(sdb, "lim", "5h", 1, utc(2026, 10, 1, 17, 30), {"session": "s1", "resets_at": 1791006000})
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert s["episodes"][0]["acct"] is None
    assert [(a["key"], a["episodes"]) for a in s["accounts"]] == [("unknown", 1)]


# ---------- the kv records and bad data ----------

def test_identity_comes_from_the_kv_records_and_current_falls_back_to_the_newest_event(sdb):
    sdb.kv_set("accounts", {A: {"key": A, "email": "a@example.com", "name": "Ann", "plan": "max", "tier": "default_claude_max_20x",
                                "org": "Acme", "label": "personal", "last_seen": "2026-10-03T05:59:00+00:00"}})
    acct_event(sdb, A, utc(2026, 10, 3, 0))
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert s["accounts"] == [{"key": A, "email": "a@example.com", "name": "Ann", "label": "personal", "plan": "max", "current": True,
                              "rl_5h": None, "rl_7d": None, "windows": {"today": ZERO, "7d": ZERO, "30d": ZERO}, "episodes": 0,
                              "last_seen": "2026-10-03T05:59:00+00:00"}]
    sdb.kv_set("account_current", {"key": B, "since": "x"})                       # kv wins over the event when both exist
    assert [(a["key"], a["current"]) for a in usage_summary.build(sdb, days=30, tz_min=0, now=NOW)["accounts"]] == [(B, True), (A, False)]


def test_garbled_kv_records_and_garbled_acct_events_never_break_the_summary(sdb):
    sdb.kv_set("accounts", ["not", "a", "dict"])
    sdb.kv_set("account_current", "a string")
    sdb.sample("acct", "", 1.0, "{not json", iso(utc(2026, 10, 2, 1)))
    sdb.sample("acct", "", None, None, iso(utc(2026, 10, 2, 2)))
    put(sdb, "acct", "", 1, utc(2026, 10, 2, 3), {"to": 5})
    cost(sdb, "claude:abababab-0000-4000-8000-000000000001", 3.0, utc(2026, 10, 2, 8), tok=30)
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert [a["key"] for a in s["accounts"]] == ["unknown"] and s["total"]["30d"]["total"] == 3.0
    sdb.kv_set("accounts", {A: "text", B: {"email": 5, "name": ["x"], "plan": None, "label": "", "last_seen": 3}})
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    b = by_key(s)[B]
    assert (b["email"], b["name"], b["label"], b["plan"], b["last_seen"]) == (None, None, None, None, None)
    assert A not in by_key(s), "a record that is not a dict names no account"
    check_totals(s)


def test_a_cost_sample_with_a_garbled_meta_goes_to_the_timeline_or_the_bucket(sdb):
    acct_event(sdb, A, utc(2026, 10, 1))
    sdb.sample("cost", "claude:acacacac-0000-4000-8000-000000000001", 2.0, "{not json", iso(utc(2026, 10, 2, 8)))
    s = usage_summary.build(sdb, days=30, tz_min=0, now=NOW)
    assert by_key(s)[A]["windows"]["30d"]["total"] == 2.0
    check_totals(s)


# ---------- the demo fixture ----------

def _demo_fixture():
    from pathlib import Path
    return json.loads((Path(usage_summary.__file__).parent / "static" / "demo" / "usage_summary.json").read_text(encoding="utf-8"))


def test_the_demo_fixture_has_the_shape_build_returns_for_accounts(sdb):
    sdb.kv_set("accounts", {A: {"key": A, "email": "a@example.com", "name": "Ann", "plan": "max"}, B: {"key": B}})
    sdb.kv_set("account_current", {"key": A})
    cost(sdb, "claude:acacacac-0000-4000-8000-000000000002", 2.0, utc(2026, 10, 2, 8), tok=20, acct=A)
    put(sdb, "rl_5h", f"acct:{A}", 42.0, utc(2026, 10, 3, 5, 0), {"resets_at": NOW_TS + 3600})
    put(sdb, "rl_7d", f"acct:{A}", 71.0, utc(2026, 10, 3, 5, 0), {"resets_at": NOW_TS + 86400})
    put(sdb, "lim", "5h", 1, utc(2026, 10, 2, 22), {"session": "s", "resets_at": 1, "acct": A})
    real, fx = usage_summary.build(sdb, days=30, tz_min=0, now=NOW), _demo_fixture()
    fx_keys = set(fx) - {"demo"}                                   # `demo` is the fixture's own rebase stamp (core.js demoRebase), not a field build() returns
    assert set(real) == fx_keys and set(real["total"]) == set(fx["total"]) and set(real["total"]["7d"]) == set(fx["total"]["7d"])
    assert set(real["accounts"][0]) == set(fx["accounts"][0]) and set(real["accounts"][0]["windows"]["7d"]) == set(fx["accounts"][0]["windows"]["7d"])
    assert set(real["accounts"][0]["rl_5h"]) == set(fx["accounts"][0]["rl_5h"])
    assert set(real["total"]["headroom_5h"][0]) == set(fx["total"]["headroom_5h"][0])
    assert set(real["rate_limits"]["by_account"][A]["rl_5h"]) == set(fx["rate_limits"]["by_account"][fx["accounts"][0]["key"]]["rl_5h"])
    assert set(real["episodes"][0]) == set(fx["episodes"][0])


def test_the_demo_fixtures_accounts_add_up_like_the_real_thing():
    fx = _demo_fixture()
    accounts, total, now = fx["accounts"], fx["total"], datetime.fromisoformat(fx["generated_at"]).timestamp()
    real = [a for a in accounts if a["key"] != "unknown"]
    assert len(real) >= 2 and total["accounts"] == len(real) and accounts[0]["current"] is True and sum(a["current"] for a in accounts) == 1
    assert accounts[-1]["key"] == "unknown" and accounts[-1]["name"] == "(before account tracking)", "the history bucket is the last row"
    assert [a["windows"]["7d"]["total"] for a in real[1:]] == sorted((a["windows"]["7d"]["total"] for a in real[1:]), reverse=True)
    for w in ("today", "7d", "30d"):
        rows = [a["windows"][w] for a in accounts]
        assert total[w]["total"] == pytest.approx(sum(r["total"] for r in rows), abs=1e-3)
        assert total[w]["tokens"] == sum(r["tokens"] for r in rows) == fx["windows"][w]["by_agent"]["claude"]["tokens"]
        assert total[w]["total"] == pytest.approx(fx["windows"][w]["by_agent"]["claude"]["total"], abs=1e-3)
        assert total[w]["hours"] == pytest.approx(sum(r["hours"] for r in rows), abs=1e-9)
        assert max(r["sessions"] for r in rows) <= total[w]["sessions"] <= sum(r["sessions"] for r in rows)
    for a in accounts:                                                    # a wider window never holds less than a narrower one
        w = a["windows"]
        assert all(w["today"][f] <= w["7d"][f] <= w["30d"][f] for f in ("total", "tokens", "hours", "sessions")), a["key"]
    assert sum(a["episodes"] for a in accounts) == len(fx["episodes"]) and {e["acct"] for e in fx["episodes"]} <= {a["key"] for a in accounts} | {None}
    resets = {a["key"]: (a["rl_5h"]["resets_at"], a["rl_7d"]["resets_at"]) for a in real}
    assert len(set(resets.values())) == len(resets) and len({r[1] for r in resets.values()}) == len(resets), "the accounts' windows reset at different instants"
    cur = fx["rate_limits"]["claude"]
    assert accounts[0]["rl_5h"]["value"] == cur["rl_5h"]["value"] and accounts[0]["rl_7d"]["resets_at"] == cur["rl_7d"]["meta"]["resets_at"]
    assert set(fx["rate_limits"]["by_account"]) == {a["key"] for a in real}
    for series, field in (("rl_5h", "headroom_5h"), ("rl_7d", "headroom_7d")):
        left = {a["key"]: (100.0 if a[series]["resets_at"] <= now else round(100 - a[series]["value"], 1)) for a in real}
        assert total[field] == [{"key": k, "left_pct": v} for k, v in sorted(left.items(), key=lambda kv: (-kv[1], kv[0]))]
