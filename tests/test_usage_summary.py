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


def cost(db, key, value, when, p="shop", r="api", tok=None, a=None, m=None):
    meta = {"p": p, "r": r, "a": a or key.split(":")[0]}
    if tok is not None:
        meta["tok"] = tok
    if m:
        meta["m"] = m
    put(db, "cost", key, value, when, meta)


def day_of(summary, day):
    return next(d for d in summary["daily"] if d["day"] == day)


# ---------- empty and shape ----------

def test_empty_db_is_an_all_zero_payload_with_the_full_shape(sdb):
    s = usage_summary.build(sdb, days=30, tz_min=KTM, now=NOW)
    assert set(s) == {"generated_at", "tz_min", "windows", "daily", "hourly_profile", "heatmap", "top_sessions", "active_hours",
                      "rate_limits", "episodes", "unpriced", "source"}
    assert s["source"] == "samples" and s["tz_min"] == 345 and s["generated_at"] == "2026-10-03T06:00:00+00:00"
    assert set(s["windows"]) == {"today", "7d", "30d"}
    for w in s["windows"].values():
        assert w == {"total": 0, "by_agent": {"claude": {"total": 0, "tokens": 0}}, "by_project": []}
    assert len(s["daily"]) == 30 and all(d["zero"] and d["total"] == 0 and d["tokens"] == 0 and d["hours"] == 0 for d in s["daily"])
    assert s["daily"][0]["day"] == "2026-09-04" and s["daily"][-1]["day"] == "2026-10-03"
    assert s["daily"][-1] == {"day": "2026-10-03", "total": 0, "by_agent": {"claude": 0}, "by_project": {}, "tokens": 0, "hours": 0, "zero": True}
    assert s["hourly_profile"] == [0] * 24 and s["heatmap"] == [[0] * 24 for _ in range(7)]
    assert s["top_sessions"] == [] and s["active_hours"] == {} and s["episodes"] == [] and s["unpriced"] == []
    assert s["rate_limits"] == {"claude": {"rl_5h": None, "rl_7d": None}}
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

def state(db, tmux, when, value=1, p="shop"):
    put(db, "state", tmux, value, when, {"p": p, "r": "api", "s": tmux, "a": "claude"})


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
    assert set(r) == {"claude"}
    assert r["claude"]["rl_5h"] == {"at": "2026-10-03T05:00:00+00:00", "value": 42.0, "meta": {"resets_at": 1791010000}}
    assert r["claude"]["rl_7d"] == {"at": "2026-10-03T05:30:00+00:00", "value": 71.0, "meta": {"resets_at": 1791270000}}


def test_episodes_are_the_lim_events_of_the_last_30_days(sdb):
    put(sdb, "lim", "7d", 1, utc(2026, 8, 1, 3), {"session": "old--x--s1", "resets_at": 1})                # too old
    put(sdb, "lim", "5h", 1, utc(2026, 10, 1, 17, 30), {"session": "shop--api--s1", "resets_at": 1791006000, "message": "You've hit your session limit"})
    put(sdb, "lim", "7d", 1, utc(2026, 10, 2, 22, 0), {"session": "blog--site--s1", "resets_at": 1791270000})
    put(sdb, "lim", "other", 1, utc(2026, 10, 3, 1, 0), {"session": "x"})
    s = usage_summary.build(sdb, days=7, tz_min=KTM, now=NOW)            # `days` does not shorten the episode history
    assert s["episodes"] == [
        {"kind": "5h", "at": "2026-10-01T17:30:00+00:00", "resets_at": 1791006000, "session": "shop--api--s1"},
        {"kind": "7d", "at": "2026-10-02T22:00:00+00:00", "resets_at": 1791270000, "session": "blog--site--s1"},
        {"kind": "other", "at": "2026-10-03T01:00:00+00:00", "resets_at": None, "session": "x"},
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
