"""Issue #94 box check (Claude Code 2.1.294, ccusage 20.0.26, 2026-10-09) and its price-table follow-up: the statusline cost (series scost) is exact for the
board's own sessions, so it is preferred over ccusage for them; the table gains a Haiku 5.5 row and prices cache writes by their TTL.

The token splits and costs below are the box's three scratch sessions (one tiny turn each; scrubbed of every name): tokens are
(input / output / cache write 1h / cache read). The expected dollars are worked out by hand from the rates, not by calling the code under test."""
import json
from datetime import datetime, timezone

import pytest

from app import cost, pricing, samples
from app.config import settings
from app.db import DB

NOW = datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc)
FABLE = {"model": "claude-fable-5-1", "tok": {"input": 2, "output": 4, "cache_creation_1h": 28726, "cache_creation": 28726, "cache_read": 26061}, "scost": 0.581255, "ccusage": 0.58125525}
SONNET = {"model": "claude-sonnet-5-5", "tok": {"input": 2, "output": 4, "cache_creation_1h": 26063, "cache_creation": 26063, "cache_read": 26061}, "scost": 0.109508, "ccusage": 0.1069021}
HAIKU = {"model": "claude-haiku-5-5", "tok": {"input": 2, "output": 4, "cache_creation_1h": 30632, "cache_creation": 30632, "cache_read": 26061}, "scost": 0.006389, "ccusage": 0.00638921}
S1, S2, S3 = ("0000000%d-aaaa-4bbb-8ccc-00000000000%d" % (i, i) for i in (1, 2, 3))


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    monkeypatch.setattr(settings, "price_table", "")
    pricing._table_cache.update(key=None, table=None)
    yield
    pricing._table_cache.update(key=None, table=None)


# ---------------------------------------------------------------- the table

@pytest.mark.parametrize("case", [FABLE, SONNET, HAIKU], ids=["fable-5-1", "sonnet-5-5", "haiku-5-5"])
def test_the_table_reproduces_the_statusline_on_the_boxs_three_sessions_with_one_hour_cache_writes(case):
    e = pricing.estimate_model(case["model"], case["tok"])
    assert e["usd"] == pytest.approx(case["scost"], abs=1e-6) and e["basis"] == "list" and e["cache_write_assumed"] is False, "a split that says 1h assumes nothing"


def test_haiku_5_5_is_its_own_row_a_tenth_of_the_4_5_row_and_says_where_it_came_from():
    assert pricing.PRICES["haiku-5-5"] == {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": None}
    assert pricing.PRICES["haiku-4-5"]["input"] == 1.0, "the 4.5 row is untouched"
    assert "box check 2026-10-09" in pricing.ROW_SOURCES["haiku-5-5"] and set(pricing.ROW_SOURCES) <= set(pricing.PRICES)
    tok = {"output": 1_000_000}
    assert pricing.estimate_model("claude-haiku-5-5-20261001", tok)["usd"] == pytest.approx(0.5)
    assert pricing.estimate_model("claude-haiku-4-5-20251001", tok)["usd"] == pytest.approx(5.0)
    assert pricing.PRICE_DATE == "2026-10-07", "the table's date is the pricing page's; the Haiku 5.5 row's own date lives in ROW_SOURCES"


def test_sonnet_5_5_cache_read_is_0_20_as_the_statusline_charges_it_not_ccusages_0_10():
    assert pricing.PRICES["sonnet-5-5"]["cache_read"] == 0.20
    assert pricing.estimate_model("claude-sonnet-5-5", {"cache_read": 1_000_000})["usd"] == pytest.approx(0.20)


def test_a_cache_write_is_priced_by_its_ttl_where_the_split_says_which():
    one_h = pricing.estimate_model("claude-fable-5-1", {"cache_creation": 1_000_000, "cache_creation_1h": 1_000_000})
    five_m = pricing.estimate_model("claude-fable-5-1", {"cache_creation": 1_000_000, "cache_creation_5m": 1_000_000})
    assert one_h["usd"] == pytest.approx(20.0) and one_h["cache_write_assumed"] is False, "1 hour = 2 x the $10 input price"
    assert five_m["usd"] == pytest.approx(12.5) and five_m["cache_write_assumed"] is False, "5 minutes = 1.25 x"
    mixed = pricing.estimate_model("claude-fable-5-1", {"cache_creation": 3_000_000, "cache_creation_1h": 1_000_000, "cache_creation_5m": 1_000_000})
    assert mixed["usd"] == pytest.approx(20 + 12.5 + 12.5) and mixed["cache_write_assumed"] is True, "the 1M the split does not cover is a 5m price, assumed"
    unsplit = pricing.estimate_model("claude-fable-5-1", {"cache_creation": 1_000_000})
    assert unsplit["usd"] == pytest.approx(12.5) and unsplit["cache_write_assumed"] is True, "no split, as ccusage rows come: the old 1.25 x, says so"
    only_split = pricing.estimate_model("claude-fable-5-1", {"cache_creation_1h": 1_000_000})
    assert only_split["usd"] == pytest.approx(20.0), "a split without the total still counts"
    assert pricing.estimate_model("claude-fable-5-1", {"cache_creation": 0})["cache_write_assumed"] is False


def test_an_override_cache_write_price_is_the_5m_rate_and_a_1h_write_never_costs_less_than_2x_input(tmp_path, monkeypatch):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"models": {"sonnet-6": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}}}))
    monkeypatch.setattr(settings, "price_table", str(f))
    tok = {"cache_creation": 2_000_000, "cache_creation_1h": 1_000_000}
    assert pricing.estimate_model("claude-sonnet-6", tok)["usd"] == pytest.approx(6.0 + 3.75) and pricing.estimate_model("claude-sonnet-6", tok)["cache_write_assumed"] is False


# ---------------------------------------------------------------- preferring the statusline

def _board(tmp_path, with_samples=True):
    """A DB with three Claude board sessions (tmux names t1..t3, session ids S1..S3) and the box's three scost values."""
    db = DB(tmp_path / "sl.db")
    for n, (sid, case) in enumerate(((S1, FABLE), (S2, SONNET), (S3, HAIKU)), 1):
        db.add_session(tmux_name=f"t{n}", project="shop", repo="api", name=f"s{n}", launcher="new", claude_session_id=sid)
        if with_samples:
            db.sample("scost", f"t{n}", 0.0, None, "2026-10-10T01:00:00+00:00")           # the first sample of every session, before the first API call
            db.sample("scost", f"t{n}", case["scost"], None, "2026-10-10T01:05:00+00:00")
    return db


def _ccusage(**override):
    """ccusage rows for the three sessions at the box's figures (override: sid -> cost, e.g. 0 for a model ccusage has no rate for)."""
    rows = []
    for sid, case in ((S1, FABLE), (S2, SONNET), (S3, HAIKU)):
        t = case["tok"]
        rows.append({"agent": "claude", "period": sid, "totalCost": override.get(sid, case["ccusage"]), "inputTokens": t["input"], "outputTokens": t["output"],
                     "cacheCreationTokens": t["cache_creation"], "cacheReadTokens": t["cache_read"], "totalTokens": sum(t[k] for k in ("input", "output", "cache_creation", "cache_read")),
                     "modelsUsed": [case["model"]], "metadata": {"lastActivity": "2026-10-10T01:05:00Z"},
                     "modelBreakdowns": [{"modelName": case["model"], "inputTokens": t["input"], "outputTokens": t["output"], "cacheCreationTokens": t["cache_creation"],
                                          "cacheReadTokens": t["cache_read"], "cost": override.get(sid, case["ccusage"]), "missingPricing": override.get(sid) == 0}]})
    return json.dumps({"session": rows})


def test_the_statusline_figure_replaces_ccusages_for_a_board_session_and_is_labelled(tmp_path):
    db = _board(tmp_path)
    costs = cost.parse_sessions(_ccusage())
    assert cost.prefer_statusline(db, costs) == 3
    sonnet = costs[S2]
    assert sonnet["cost"] == pytest.approx(0.109508) and sonnet["ccusage_cost"] == pytest.approx(0.1069021), "ccusage ran 2.4 % low on the cache reads"
    assert sonnet["est"] == sonnet["cost"] and sonnet["est_basis"] == "statusline" and sonnet["est_cwa"] is False and sonnet["unpriced"] is False
    assert costs[S1]["cost"] == pytest.approx(0.581255) and costs[S3]["cost"] == pytest.approx(0.006389)


def test_a_session_ccusage_prices_at_zero_is_no_longer_unpriced_once_its_statusline_cost_exists(tmp_path):
    db = _board(tmp_path)
    costs = cost.parse_sessions(_ccusage(**{S2: 0}))
    assert costs[S2]["unpriced"] is True and costs[S2]["est_basis"] == "list", "before: the table prices it"
    cost.prefer_statusline(db, costs)
    assert costs[S2]["cost"] == pytest.approx(0.109508) and costs[S2]["est_basis"] == "statusline" and costs[S2]["unpriced"] is False
    entries = cost.session_samples(costs, db.session_ids())
    assert cost.unpriced(entries)["sessions"] == 0, "the hatched bar is for sessions with neither source"
    blk = cost.estimate_block(entries)
    assert blk["bases"] == {"statusline": 3} and blk["sessions"] == 0 and blk["usd"] == 0, "an exact figure adds nothing to the estimate's sentence"


def test_without_a_statusline_sample_ccusage_and_the_table_stand_and_a_session_with_neither_stays_unpriced(tmp_path):
    db = _board(tmp_path, with_samples=False)
    costs = cost.parse_sessions(_ccusage(**{S2: 0}))
    assert cost.prefer_statusline(db, costs) == 0
    assert costs[S1]["cost"] == pytest.approx(0.58125525) and costs[S1]["est_basis"] is None
    assert costs[S2]["cost"] == 0 and costs[S2]["est_basis"] == "list" and costs[S2]["unpriced"] is True
    unknown = json.loads(_ccusage(**{S2: 0}))
    unknown["session"][1]["modelsUsed"] = ["claude-mystery-9"]
    unknown["session"][1]["modelBreakdowns"][0]["modelName"] = "claude-mystery-9"
    neither = cost.parse_sessions(json.dumps(unknown))
    cost.prefer_statusline(db, neither)
    assert neither[S2]["unpriced"] is True and neither[S2]["est_basis"] is None, "neither source: hatched, as before"
    assert cost.unpriced(cost.session_samples(neither, []))["sessions"] == 1


def test_a_zero_or_missing_or_implausibly_low_statusline_cost_is_not_used(tmp_path):
    db = _board(tmp_path, with_samples=False)
    db.sample("scost", "t1", 0.0, None, "2026-10-10T01:00:00+00:00")                # only the first, zero sample
    db.sample("scost", "t2", 0.01, None, "2026-10-10T01:00:00+00:00")               # a resumed process: 9 % of what ccusage read from the whole transcript
    costs = cost.parse_sessions(_ccusage())
    assert cost.prefer_statusline(db, costs) == 0
    assert costs[S1]["est_basis"] is None and costs[S2]["cost"] == pytest.approx(0.1069021) and costs[S3]["est_basis"] is None


def test_only_the_newest_row_of_a_tmux_name_takes_the_live_figure(tmp_path):
    db = DB(tmp_path / "sl2.db")
    db.add_session(tmux_name="t1", project="shop", repo="api", name="old", launcher="new", claude_session_id=S1)
    db.add_session(tmux_name="t1", project="shop", repo="api", name="new", launcher="new", claude_session_id=S2)
    db.sample("scost", "t1", 0.109508, None, "2026-10-10T01:05:00+00:00")
    costs = cost.parse_sessions(_ccusage())
    assert cost.prefer_statusline(db, costs) == 1
    assert costs[S2]["est_basis"] == "statusline" and costs[S1]["est_basis"] is None, "the older conversation keeps its own figure"


def test_sessions_the_board_did_not_start_and_codex_rows_are_left_alone(tmp_path):
    db = DB(tmp_path / "sl3.db")
    db.add_session(tmux_name="x1", project="shop", repo="api", name="cx", launcher="new", claude_session_id=S3, agent="codex")
    db.sample("scost", "x1", 5.0, None, "2026-10-10T01:05:00+00:00")
    costs = cost.parse_sessions(_ccusage())
    assert cost.prefer_statusline(db, costs) == 0 and costs[S3]["cost"] == pytest.approx(0.00638921), "a stray sample on a Codex row prices nothing"
    assert cost.prefer_statusline(db, {}) == 0


def test_a_failing_lookup_never_fails_the_refresh(tmp_path):
    class Broken:
        def session_tmux_ids(self, agent):
            raise RuntimeError("db is busy")
    assert cost.prefer_statusline(Broken(), cost.parse_sessions(_ccusage())) == 0


def test_the_cost_series_and_the_summary_carry_the_statusline_figure_on_both_bases(tmp_path):
    from app import usage_summary
    db = _board(tmp_path)
    costs = cost.parse_sessions(_ccusage(**{S2: 0}))
    cost.prefer_statusline(db, costs)
    entries = cost.session_samples(costs, db.session_ids())
    assert samples.record_cost(db, {"sessions": entries}, NOW) == 3
    meta = db.sample_last("cost", f"claude:{S2}")["meta"]
    assert db.sample_last("cost", f"claude:{S2}")["value"] == pytest.approx(0.109508) and meta["eb"] == "statusline" and meta["est"] == pytest.approx(0.109508)
    for basis in (usage_summary.BASIS_REPORTED, usage_summary.BASIS_EST):
        top = {t["key"]: t for t in usage_summary.build(db, days=7, tz_min=0, now=NOW, basis=basis)["top_sessions"]}
        assert top[f"claude:{S2}"]["est_basis"] == "statusline" and top[f"claude:{S2}"]["total"] == pytest.approx(0.1095, abs=1e-4), basis


def test_a_refresh_through_the_route_prices_the_board_session_from_its_statusline_and_the_totals_follow(client, fake_tmux, monkeypatch):
    from app import main
    H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
    monkeypatch.setattr(cost, "fetch_sessions", lambda: cost.parse_sessions(_ccusage(**{S2: 0})))
    main.db.add_session(tmux_name="shop--api--s2", project="shop", repo="api", name="s2", launcher="new", claude_session_id=S2)
    main.db.sample("scost", "shop--api--s2", 0.109508, None, "2026-10-10T01:05:00+00:00")
    r = client.post("/api/cost/refresh", headers=H).json()
    assert r["projects"]["shop"]["total"] == pytest.approx(0.109508, abs=1e-4), "the board's session, priced at last"
    assert r["unpriced"]["sessions"] == 0
    assert r["estimate"]["bases"]["statusline"] == 1 and r["estimate"]["sessions"] == 0
    assert r["total_all"] == pytest.approx(0.581255 + 0.109508 + 0.006389, abs=0.01), "the other two sessions keep ccusage's figures"
