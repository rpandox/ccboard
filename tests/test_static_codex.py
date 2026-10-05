"""The v0.5.12 demo fixtures of the Codex UI (`?demo=1#/usage`, `#/agents`): what state.json, series.json and series_events.json must carry so the CX pill, the Usage page's Codex tab and
Codex block and the Agents page's "Started outside the board" section show every state they draw, with the shapes the backend produces (app/agents/codex_rollout.py,
app/agents/codex_discovery.py). It reads files; the behaviour is in tests/js/shell-codex.test.mjs, usage-codex.test.mjs and agents-external.test.mjs.
"""
import datetime
import json
import pathlib
import re

from tests.test_static import blank_js

STATIC = pathlib.Path(__file__).resolve().parent.parent / "app" / "static"
DEMO = STATIC / "demo"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def demo(name):
    return json.loads((DEMO / name).read_text(encoding="utf-8"))


def epoch_of(iso):
    return datetime.datetime.fromisoformat(iso).timestamp()


def test_demo_usage_codex_is_the_weekly_only_reading_of_the_account_in_use():
    st = demo("state.json")
    rec = st["usage_codex"]
    v = rec["value"]
    assert {"limit_id", "plan_type", "primary", "secondary", "credits", "reached", "observed_at", "account"} <= set(v), "the keys of kv rate_limits_codex"
    assert v["limit_id"] == "codex" and v["plan_type"] == "plus" and v["reached"] is False
    assert v["primary"]["window_minutes"] == 10080 and v["secondary"] is None, "a weekly-only plan: one pill, one gauge"
    assert 0 < v["primary"]["used_percent"] < 60
    assert v["account"] == st["codex_accounts"]["current"], "the reading belongs to the Codex account in use"
    epoch = st["demo"]["epoch"]
    assert 0 <= epoch - epoch_of(v["observed_at"]) < 3600, "observed a minute or so before the fixture clock"
    assert v["primary"]["resets_at"] > epoch


def test_demo_series_carries_the_codex_windows_of_both_demo_accounts():
    s = demo("series.json")
    st = demo("state.json")
    n = len(s["t"])
    keys = [a["key"] for a in st["codex_accounts"]["list"]]
    assert len(keys) == 2
    assert {"rl_5h:claude", "rl_7d:claude"} <= set(s["series"]), "the Claude chart keeps its two lines"
    names = ["rl_7d:codex"] + [f"rl_7d:cacct:{k}" for k in keys]
    for name in names:
        values = s["series"][name]
        assert len(values) == n, name
        known = [v for v in values if v is not None]
        assert known and all(isinstance(v, (int, float)) and 0 <= v <= 100 for v in known), name
        assert known == sorted(known), f"{name}: a weekly window only grows until it resets"
        assert s["meta"][name]["resets_at"] > st["demo"]["epoch"], f"{name}: its window ends after the fixture clock (demoApi shifts it to now)"
    current = st["codex_accounts"]["current"]
    live = st["usage_codex"]["value"]["primary"]["used_percent"]
    assert s["series"]["rl_7d:codex"][-1] == live, "key=codex is the account in use: its last point is the live reading"
    assert s["series"][f"rl_7d:cacct:{current}"][-1] == live
    other = next(k for k in keys if k != current)
    assert s["series"][f"rl_7d:cacct:{other}"][0] is None, "the second account has a gap before its first sessions"
    assert s["series"][f"rl_7d:cacct:{other}"][-1] != live, "and its own number"
    assert s["demo"]["epoch"] == st["demo"]["epoch"], "the fixture clock rides along so demoApi can move the reset times"


def test_core_shifts_the_demo_series_reset_times_to_now():
    """A Codex window that ends after the fixture's moment must not read as 'rolled over' hours later: demoApi moves the series' meta.resets_at by the elapsed time (series.json carries demo.epoch)."""
    core = blank_js((STATIC / "core.js").read_text(encoding="utf-8"))
    assert re.search(r"name === 'series' && data && data\.demo && data\.demo\.epoch", core), "core.js demoApi has no clock shift for the series fixture"
    assert re.search(r"resets_at: m\.resets_at \+ dt", core)


def test_demo_external_threads_cover_every_row_the_agents_page_draws():
    st = demo("state.json")
    ext = st["external"]
    assert ext["claude"] == [] and isinstance(ext["codex"], list) and ext["at"]
    rows = ext["codex"]
    base = st["config"]["projects_dir"]
    assert len({r["id"] for r in rows}) == len(rows) >= 3
    epoch = st["demo"]["epoch"]
    for r in rows:
        assert r["agent"] == "codex" and UUID.match(r["id"]) and r["session_id"] == r["id"]
        assert r["title"] and r["cwd"] and r["model"] and r["tokens"] > 0, r
        assert 0 <= epoch - epoch_of(r["updated_at"]) <= 14 * 86400, "inside the 14 days discovery lists"
        assert r.get("tmux") is None, "a thread the board runs is not listed"
        assert r["openable"] == (r["cwd"] == base or r["cwd"].startswith(base + "/")), r
        assert (r["project"] is not None) == r["openable"]
        assert (r["badge"] or None) == (r["originator"] if r["originator"] != "codex-tui" else None), "the badge is the originator unless it is codex-tui"
    assert any(r["badge"] == "hermes" and not r["openable"] for r in rows), "a Hermes thread, badged, whose folder is outside the projects (Open disabled)"
    assert any(r["openable"] and r["badge"] is None for r in rows), "a native thread with an Open that works"
    assert max(len(r["title"]) for r in rows) >= 100, "a long title that wraps at 390 px"
    names = {p["name"] for p in st["projects"]}
    assert all(r["project"] in names for r in rows if r["project"]), "the projects the rows name exist in the fixture"
    live = [s["claude_session_id"] for p in st["projects"] for g in [*[x["sessions"] for x in p["repos"]], p["orphan_sessions"]] for s in g]
    assert not {r["id"] for r in rows} & set(live), "an external thread is not one of the live sessions"
