"""app/pricing.py and the estimates built on it (issue #95): a dated list-price table for the Claude models ccusage prices at zero, the sibling fallback, and
what cost.parse_sessions / attribute / session_samples / samples.record_cost / usage_summary.build do with it.

The fixture tests/fixtures/ccusage_session_breakdown.json is made of real rows of `ccusage session --json` from the box (keys and numbers only, the session
names replaced), captured 2026-10-08: the output DOES carry a per-model split (modelBreakdowns: input, output, cacheCreation, cacheRead tokens, cost,
missingPricing). The expected dollars below are worked out by hand from the list prices, not by calling the code under test."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import cost, pricing, samples, usage_summary
from app.config import settings
from app.db import DB

FX = (Path(__file__).parent / "fixtures" / "ccusage_session_breakdown.json").read_text()
NOW = datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc)
S1 = "00000001-aaaa-4bbb-8ccc-000000000001"      # opus-5-5 priced + sonnet-5-5 missingPricing
S2 = "00000002-aaaa-4bbb-8ccc-000000000002"      # fable-5-1, opus-5-5 priced + sonnet-5-5 missingPricing
S3 = "00000003-aaaa-4bbb-8ccc-000000000003"      # sonnet-5-5 only, cost 0
S4 = "00000004-aaaa-4bbb-8ccc-000000000004"      # opus-4-7, fully priced
S5 = "00000005-aaaa-4bbb-8ccc-000000000005"      # haiku, fully priced
S6 = "00000006-aaaa-4bbb-8ccc-000000000006"      # opus-5 + fable-5-1, fully priced

# sonnet-5-5 at 2 / 10 per million, cache read 0.20, cache write assumed 1.25 x 2 = 2.5
SONNET_S1 = (702 * 2 + 1160978 * 10 + 2016361 * 2.5 + 157731273 * 0.20) / 1e6
SONNET_S2 = (296 * 2 + 73084 * 10 + 1688321 * 2.5 + 33622270 * 0.20) / 1e6
SONNET_S3 = (64 * 2 + 27383 * 10 + 112413 * 2.5 + 2673185 * 0.20) / 1e6


@pytest.fixture(autouse=True)
def _no_override(monkeypatch):
    monkeypatch.setattr(settings, "price_table", "")
    pricing._table_cache.update(key=None, table=None)
    yield
    pricing._table_cache.update(key=None, table=None)


# ---------------------------------------------------------------- the table

def test_the_table_is_dated_and_carries_its_source():
    assert pricing.PRICE_DATE == "2026-10-07" and "Anthropic pricing page" in pricing.PRICE_SOURCE
    assert pricing.info() == {"date": "2026-10-07", "source": pricing.PRICE_SOURCE}
    assert pricing.PRICES["fable-5-1"] == {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": None}
    assert pricing.PRICES["opus-5-5"] == {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": None}
    assert pricing.PRICES["sonnet-5-5"] == {"input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_write": None}
    assert pricing.PRICES["haiku-4-5"] == {"input": 1.0, "output": 5.0, "cache_read": 0.10, "cache_write": None}
    assert all(v["cache_write"] is None for v in pricing.PRICES.values()), "cache-write prices were not in the material: unknown, and the estimate says so"


@pytest.mark.parametrize("model,key", [
    ("claude-sonnet-5-5", "sonnet-5-5"), ("claude-sonnet-5-5-20260901", "sonnet-5-5"), ("claude-haiku-4-5-20251001", "haiku-4-5"),
    ("anthropic/claude-opus-5-5", "opus-5-5"), ("CLAUDE-OPUS-5-5", "opus-5-5"), ("claude-opus-5-latest", "opus-5"),
])
def test_family_key(model, key):
    assert pricing.family_key(model) == key


@pytest.mark.parametrize("model", ["gpt-5.6-sol", "", None, "claude-", "claude-opus", "sonnet-5-5", "../claude-opus-5-5", "claude-opus-5-5/x"])
def test_family_key_refuses_what_is_not_a_claude_id(model):
    assert pricing.family_key(model) is None


def test_each_current_model_resolves_to_itself_as_list():
    for m, fam in (("claude-fable-5-1", "fable-5-1"), ("claude-opus-5-5", "opus-5-5"), ("claude-sonnet-5-5", "sonnet-5-5"), ("claude-haiku-4-5-20251001", "haiku-4-5")):
        key, entry, basis = pricing.table().resolve(m)
        assert (key, basis) == (fam, "list") and entry == pricing.PRICES[fam]


def test_a_legacy_id_takes_the_newest_entry_of_its_family_as_a_sibling():
    for m, fam in (("claude-opus-5", "opus-5-5"), ("claude-opus-4-7", "opus-5-5"), ("claude-fable-5", "fable-5-1"), ("claude-sonnet-5", "sonnet-5-5"),
                   ("claude-sonnet-4-6", "sonnet-5-5"), ("claude-haiku-5", "haiku-4-5")):
        key, _entry, basis = pricing.table().resolve(m)
        assert (key, basis) == (fam, "sibling"), m


def test_a_model_with_no_family_in_the_table_is_unpriced_never_guessed():
    assert pricing.table().resolve("claude-mystery-9") is None
    assert pricing.estimate_model("claude-mystery-9", {"output": 10**6}) is None
    assert pricing.estimate_model("gpt-5.6-sol", {"output": 10**6}) is None


# ---------------------------------------------------------------- arithmetic

def test_the_estimate_is_the_four_token_kinds_at_their_rates():
    tok = {"input": 1_000_000, "output": 1_000_000, "cache_creation": 1_000_000, "cache_read": 1_000_000}
    e = pricing.estimate_model("claude-opus-5-5", tok)
    assert e["usd"] == pytest.approx(4 + 20 + 5 + 0.2) and e["basis"] == "list" and e["cache_write_assumed"] is True, "cache write = 1.25 x input, assumed"
    e = pricing.estimate_model("claude-fable-5-1", {"output": 2_000_000, "cache_read": 4_000_000})
    assert e["usd"] == pytest.approx(100 + 1.0) and e["cache_write_assumed"] is False, "no cache-write tokens: nothing was assumed"
    assert pricing.estimate_model("claude-sonnet-5-5", {})["usd"] == 0
    assert pricing.estimate_model("claude-opus-5", {"output": 1_000_000})["basis"] == "sibling"


def test_rough_is_the_mean_rate_of_the_models_and_refuses_when_one_has_no_price():
    tok = {"input": 0, "output": 1_000_000, "cache_creation": 0, "cache_read": 0}
    r = pricing.estimate_rough(["claude-opus-5-5", "claude-sonnet-5-5"], tok)
    assert r["usd"] == pytest.approx((20 + 10) / 2) and r["basis"] == "rough"
    assert pricing.estimate_rough(["claude-opus-5-5", "claude-mystery-9"], tok) is None
    assert pricing.estimate_rough([], tok) is None


def test_the_override_file_adds_entries_and_moves_the_date_and_a_bad_one_changes_nothing(tmp_path, monkeypatch):
    f = tmp_path / "prices.json"
    f.write_text(json.dumps({"date": "2026-11-01", "source": "my notes", "models": {
        "sonnet-6": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}, "opus-5-5": {"input": 5, "output": 25},
        "claude-haiku-5": {"input": 1, "output": 5, "cache_read": 0.1}, "bad": {"input": 1}, "sonnet-7": {"input": -1, "output": 2}}}))
    monkeypatch.setattr(settings, "price_table", str(f))
    assert pricing.info()["date"] == "2026-11-01" and pricing.info()["source"].startswith("my notes") and "override" in pricing.info()["source"]
    e = pricing.estimate_model("claude-sonnet-6", {"output": 1_000_000, "cache_creation": 1_000_000})
    assert e["usd"] == pytest.approx(15 + 3.75) and e["basis"] == "list" and e["cache_write_assumed"] is False, "an override can say the cache-write price"
    assert pricing.estimate_model("claude-opus-5-5", {"output": 1_000_000})["usd"] == pytest.approx(25), "and replace a built-in entry"
    assert pricing.estimate_model("claude-haiku-5", {"output": 1_000_000})["usd"] == pytest.approx(5)
    assert "sonnet-7" not in pricing.table().prices and "bad" not in pricing.table().prices, "negative and incomplete entries are dropped"
    f.write_text("{not json")
    pricing._table_cache.update(key=None, table=None)
    assert pricing.info()["date"] == "2026-10-07" and pricing.estimate_model("claude-sonnet-6", {"output": 1}) is not None, "the built-in table stands (sibling)"
    monkeypatch.setattr(settings, "price_table", str(tmp_path / "missing.json"))
    assert pricing.info()["date"] == "2026-10-07"


def test_the_settings_key_comes_from_the_environment():
    from app.config import Settings
    assert Settings(env={"CCBOARD_PRICE_TABLE": " /etc/ccboard/prices.json "}).price_table == "/etc/ccboard/prices.json"
    assert Settings(env={}).price_table == ""


# ---------------------------------------------------------------- the ccusage rows

def test_the_fixture_is_real_ccusage_shape():
    rows = json.loads(FX)["session"]
    assert all("modelBreakdowns" in r for r in rows), "ccusage's session output does carry a per-model split"
    assert any(b.get("missingPricing") for r in rows for b in r["modelBreakdowns"])
    assert not any(k in json.dumps(rows) for k in ("projectPath", "/home/", "/Us" + "ers/"))


def test_the_estimate_prices_only_the_part_ccusage_priced_at_zero():
    c = cost.parse_sessions(FX)
    assert c[S1]["cost"] == pytest.approx(18.2370202), "the reported figure never changes"
    assert c[S1]["est"] == pytest.approx(18.2370202 + SONNET_S1) and c[S1]["est_basis"] == "list" and c[S1]["est_cwa"] is True
    assert c[S2]["est"] == pytest.approx(6.6475585499999985 + SONNET_S2)
    assert c[S3]["cost"] == 0 and c[S3]["est"] == pytest.approx(SONNET_S3) and c[S3]["unpriced"] is True
    assert round(SONNET_S3, 4) == 1.0896


def test_a_fully_priced_session_has_no_estimate_and_est_equals_the_reported_cost():
    c = cost.parse_sessions(FX)
    for sid in (S4, S5, S6):
        assert c[sid]["est"] == c[sid]["cost"] and c[sid]["est_basis"] is None and c[sid]["est_cwa"] is False, sid


def test_the_rolled_up_workflow_rows_add_their_estimates_to_the_parent():
    rows = json.loads(FX)["session"]
    child = json.loads(json.dumps(rows[2]))
    child["period"] = f"{S3}/subagents/workflows/wf_1/agent"
    out = cost.parse_sessions(json.dumps({"session": [rows[2], child]}))
    assert out[S3]["subs"] == 1 and out[S3]["est"] == pytest.approx(2 * SONNET_S3) and out[S3]["est_basis"] == "list"


def test_without_a_per_model_split_a_single_model_row_is_priced_from_the_session_totals_and_several_models_are_rough():
    row = {"agent": "claude", "period": S1, "totalCost": 0, "totalTokens": 3_000_000, "inputTokens": 0, "outputTokens": 1_000_000, "cacheCreationTokens": 0,
           "cacheReadTokens": 2_000_000, "modelsUsed": ["claude-sonnet-5-5"], "metadata": {"lastActivity": "2026-10-01T00:00:00Z"}}
    one = cost.parse_sessions(json.dumps({"session": [row]}))[S1]
    assert one["est"] == pytest.approx(10 + 0.4) and one["est_basis"] == "list"
    row2 = dict(row, modelsUsed=["claude-sonnet-5-5", "claude-opus-5-5"])
    two = cost.parse_sessions(json.dumps({"session": [row2]}))[S1]
    assert two["est"] == pytest.approx((10 + 20) / 2 + 2_000_000 * (0.2 + 0.2) / 2 / 1e6) and two["est_basis"] == "rough"
    row3 = dict(row, modelsUsed=["claude-sonnet-5-5", "claude-mystery-9"])
    three = cost.parse_sessions(json.dumps({"session": [row3]}))[S1]
    assert three["est"] == 0 and three["est_basis"] is None and three["unpriced"] is True, "a model with no price among them: nothing guessed"
    only_total = {"agent": "claude", "period": S1, "totalCost": 0, "totalTokens": 5000, "modelsUsed": ["claude-sonnet-5-5"]}
    assert cost.parse_sessions(json.dumps({"session": [only_total]}))[S1]["est_basis"] is None, "no split of any kind: no estimate"


def test_a_legacy_model_in_the_breakdown_is_a_sibling_estimate():
    row = {"agent": "claude", "period": S1, "totalCost": 0, "totalTokens": 1_000_000, "modelsUsed": ["claude-opus-5"],
           "modelBreakdowns": [{"modelName": "claude-opus-5", "inputTokens": 0, "outputTokens": 1_000_000, "cacheCreationTokens": 0, "cacheReadTokens": 0, "cost": 0,
                                "missingPricing": True}]}
    e = cost.parse_sessions(json.dumps({"session": [row]}))[S1]
    assert e["est"] == pytest.approx(20) and e["est_basis"] == "sibling"
    assert cost.weaker("list", "sibling") == "sibling" and cost.weaker("rough", "list") == "rough" and cost.weaker(None, "list") == "list"


def test_codex_rows_are_never_estimated():
    row = {"agent": "codex", "period": "2026/09/01/rollout-2026-09-01T00-00-00-" + S1, "totalCost": 0, "totalTokens": 100, "modelsUsed": ["gpt-5.6-sol"]}
    e = cost.parse_sessions(json.dumps({"session": [row]}))[S1]
    assert e["est"] == 0 and e["est_basis"] is None


# ---------------------------------------------------------------- attribution and the kv record

def test_attribute_carries_est_beside_total_only_where_they_differ():
    costs = cost.parse_sessions(FX)
    rows = [{"project": "shop", "repo": "api", "claude_session_id": S1, "agent": "claude"}, {"project": "blog", "repo": "site", "claude_session_id": S4, "agent": "claude"}]
    r = cost.attribute(costs, rows, [], NOW)
    assert r["projects"]["shop"]["total"] == pytest.approx(18.2370, abs=1e-4) and r["projects"]["shop"]["est"] == pytest.approx(18.2370202 + SONNET_S1, abs=1e-4)
    assert "est" not in r["projects"]["blog"], "nothing estimated there"
    assert r["by_agent"]["claude"]["est"] > r["by_agent"]["claude"]["total"] and "est" not in r["by_agent"]["codex"]
    assert r["total_all_est"] == pytest.approx(r["total_all"] + SONNET_S1 + SONNET_S2 + SONNET_S3, abs=0.02)
    assert r["total_all"] == round(sum(c["cost"] for c in costs.values()), 2), "the reported total is untouched"
    assert "total_all_est" not in cost.attribute(cost.parse_sessions(json.dumps({"session": [json.loads(FX)["session"][3]]})), [], [], NOW)


def test_estimate_block_and_unpriced_say_which_sessions_have_an_estimate():
    costs = cost.parse_sessions(FX)
    entries = cost.session_samples(costs, [])
    blk = cost.estimate_block(entries)
    assert blk["date"] == "2026-10-07" and blk["sessions"] == 3 and blk["bases"] == {"list": 3} and blk["cache_write_assumed"] is True and blk["cache_write_x"] == 1.25
    assert blk["usd"] == pytest.approx(SONNET_S1 + SONNET_S2 + SONNET_S3, abs=0.01)
    un = cost.unpriced(entries)
    assert un["sessions"] == 1 and un["top"][0]["est_basis"] == "list", "S3 has tokens and no price, and now an estimate"
    assert cost.estimate_block([])["sessions"] == 0 and cost.estimate_block([])["date"] == "2026-10-07"


# ---------------------------------------------------------------- the series and the summary

@pytest.fixture
def sdb(tmp_path):
    return DB(tmp_path / "pricing.db")


def _record(db, when, fx=FX):
    costs = cost.parse_sessions(fx)
    entries = cost.session_samples(costs, [{"project": "shop", "repo": "api", "claude_session_id": S1, "agent": "claude"}])
    return samples.record_cost(db, {"sessions": entries}, when)


def test_the_cost_series_carries_est_and_its_basis_and_only_for_sessions_that_have_one(sdb):
    _record(sdb, NOW)
    m1 = sdb.sample_last("cost", f"claude:{S1}")["meta"]
    assert m1["eb"] == "list" and m1["est"] == pytest.approx(18.2370202 + SONNET_S1, abs=1e-5)
    assert sdb.sample_last("cost", f"claude:{S1}")["value"] == pytest.approx(18.2370202), "the value stays the reported one"
    assert "est" not in sdb.sample_last("cost", f"claude:{S4}")["meta"] and "eb" not in sdb.sample_last("cost", f"claude:{S4}")["meta"]


def test_a_changed_price_table_writes_one_row_and_an_unchanged_one_writes_none(sdb, tmp_path, monkeypatch):
    first = _record(sdb, NOW)
    assert _record(sdb, NOW + timedelta(hours=1)) == 0, "nothing changed"
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"models": {"sonnet-5-5": {"input": 4, "output": 20, "cache_read": 0.4}}}))
    monkeypatch.setattr(settings, "price_table", str(f))
    assert _record(sdb, NOW + timedelta(hours=2)) == 3, "the three sessions with an estimate get one row each, the priced ones none"
    assert first == 6
    assert _record(sdb, NOW + timedelta(hours=3)) == 0


def _seed(db, key, rows):
    """rows: [(utc datetime, reported cumulative, tokens, est cumulative or None)]"""
    for when, v, tok, est in rows:
        meta = {"p": "shop", "r": "api", "a": "claude", "tok": tok}
        if est is not None:
            meta.update(est=est, eb="list")
        db.sample("cost", key, v, meta, when.isoformat(timespec="seconds"))


TZ0 = 0
D = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def test_the_estimated_basis_values_every_figure_with_the_estimate_and_the_reported_basis_is_unchanged(sdb):
    _seed(sdb, "claude:aaaa", [(D - timedelta(days=1), 0.0, 100, None), (D, 0.0, 300, 3.0)])
    _seed(sdb, "claude:bbbb", [(D - timedelta(days=1), 5.0, 100, None), (D, 7.0, 200, None)])
    rep = usage_summary.build(sdb, days=7, tz_min=TZ0, now=D + timedelta(hours=1))
    est = usage_summary.build(sdb, days=7, tz_min=TZ0, now=D + timedelta(hours=1), basis="est")
    assert rep["basis"] == "reported" and est["basis"] == "est"
    assert rep["windows"]["7d"]["total"] == pytest.approx(7.0), "reported: 5 + 2, the unpriced session adds nothing"
    assert est["windows"]["7d"]["total"] == pytest.approx(10.0), "estimated: the same plus 3"
    today = lambda s: next(d for d in s["daily"] if d["day"] == "2026-10-07")
    assert today(rep)["total"] == pytest.approx(2.0)
    assert today(est)["total"] == pytest.approx(4.0), "2 reported + 2 of the estimate: the other 1 sits on the day before (spread by tokens)"
    assert est["windows"]["7d"]["by_project"][0]["project"] == "shop" and est["windows"]["7d"]["by_project"][0]["total"] == pytest.approx(10.0)
    assert today(est)["by_agent"]["claude"] == pytest.approx(4.0) and today(est)["by_project"]["shop"] == pytest.approx(4.0)


def test_history_from_before_the_estimate_is_spread_by_tokens_so_one_day_does_not_take_it_all(sdb):
    # reported stays 0 all along; the first sample WITH an estimate (day 3) says 6.0 USD for 300 tokens, i.e. 0.02 per token
    _seed(sdb, "claude:cccc", [(D - timedelta(days=2), 0.0, 100, None), (D - timedelta(days=1), 0.0, 200, None), (D, 0.0, 300, 6.0)])
    est = usage_summary.build(sdb, days=7, tz_min=TZ0, now=D + timedelta(hours=1), basis="est")
    by_day = {d["day"]: d["total"] for d in est["daily"]}
    assert by_day["2026-10-05"] == pytest.approx(2.0) and by_day["2026-10-06"] == pytest.approx(2.0) and by_day["2026-10-07"] == pytest.approx(2.0)
    assert est["windows"]["7d"]["total"] == pytest.approx(6.0), "the whole estimate, not more"
    assert usage_summary.build(sdb, days=7, tz_min=TZ0, now=D + timedelta(hours=1))["windows"]["7d"]["total"] == 0


def test_a_sample_after_the_estimate_that_lacks_one_is_the_reported_cost_so_ccusage_catching_up_does_not_double_count(sdb):
    _seed(sdb, "claude:dddd", [(D - timedelta(days=1), 0.0, 100, 2.0), (D, 4.0, 200, None)])
    est = usage_summary.build(sdb, days=7, tz_min=TZ0, now=D + timedelta(hours=1), basis="est")
    assert est["windows"]["7d"]["total"] == pytest.approx(4.0), "ccusage prices it at 4: that is the figure now, not 4 + something"


def test_top_sessions_name_their_estimate_basis_and_the_unpriced_hatch_goes_where_there_is_an_estimate(sdb):
    _seed(sdb, "claude:eeee", [(D, 0.0, 300, 3.0)])
    _seed(sdb, "claude:ffff", [(D, 0.0, 300, None)])
    sdb.kv_set("cost", {"unpriced": {"sessions": 2, "tokens": 600, "top": [
        {"id": "eeee", "agent": "claude", "project": "shop", "repo": "api", "tokens": 300, "models": ["claude-sonnet-5-5"], "est_basis": "list"},
        {"id": "ffff", "agent": "claude", "project": "shop", "repo": "api", "tokens": 300, "models": ["claude-mystery-9"]}]},
        "estimate": {"date": "2026-10-07", "source": "S", "sessions": 1, "usd": 3.0, "bases": {"list": 1}, "cache_write_assumed": True, "cache_write_x": 1.25}})
    rep = usage_summary.build(sdb, days=7, tz_min=TZ0, now=D + timedelta(hours=1))
    est = usage_summary.build(sdb, days=7, tz_min=TZ0, now=D + timedelta(hours=1), basis="est")
    assert [u["key"] for u in rep["unpriced"]] == ["claude:eeee", "claude:ffff"] or [u["key"] for u in rep["unpriced"]] == ["claude:ffff", "claude:eeee"]
    assert [u["key"] for u in est["unpriced"]] == ["claude:ffff"], "the session with an estimate is no longer hatched; the one with none still is"
    assert [(t["key"], t.get("est_basis"), t["total"]) for t in est["top_sessions"]] == [("claude:eeee", "list", 3.0)]
    assert all("est_basis" not in t for t in rep["top_sessions"]), "the reported basis has no estimate to name"
    assert est["estimate"] == {"date": "2026-10-07", "source": "S", "sessions": 1, "usd": 3.0, "bases": {"list": 1}, "cache_write_assumed": True, "cache_write_x": 1.25}
    assert rep["estimate"] == est["estimate"]


def test_the_estimate_block_defaults_to_the_price_tables_own_date_before_any_refresh(sdb):
    s = usage_summary.build(sdb, days=7, tz_min=TZ0, now=D)
    assert s["estimate"]["date"] == "2026-10-07" and s["estimate"]["sessions"] == 0 and s["estimate"]["bases"] == {} and s["basis"] == "reported"
    assert usage_summary.build(sdb, days=7, tz_min=TZ0, now=D, basis="anything-else")["basis"] == "reported"


def test_accounts_follow_the_basis_too(sdb):
    _seed(sdb, "claude:gggg", [(D, 0.0, 300, 3.0)])
    rep = usage_summary.build(sdb, days=7, tz_min=TZ0, now=D + timedelta(hours=1))
    est = usage_summary.build(sdb, days=7, tz_min=TZ0, now=D + timedelta(hours=1), basis="est")
    tot = lambda s: sum(a["windows"]["7d"]["total"] for a in s["accounts"])
    assert tot(rep) == 0 and tot(est) == pytest.approx(3.0)
