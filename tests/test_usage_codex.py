"""v0.5.12 slice A, Codex rollout data: app/agents/codex_rollout.py (parse_tail, session_meta, the Tailer, bind_unbound_rows, the rate-limit
poll with per-account attribution), its Sampler hook, kv rate_limits_codex / state.usage_codex and the rl_5h / rl_7d series keys `codex` and
`cacct:<key>`.

Everything runs on temp directories (the autouse isolation fixtures give every test a temp CODEX_HOME), a bare DB and fake clocks; nothing
reads the real ~/.codex. The rollout shapes are those of the brief and the box notes (a session_meta first line with a large
base_instructions; event_msg token_count lines with `info` nested or null and `rate_limits` beside it; turn_context items), built by the
helpers below and by tests/fixtures/codex_rollout.jsonl; they are not captured from a live Codex, so the box checks (V7) stay on the plan.
"""
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import accounts, hooks, notify, samples
from app.agents import codex as codex_agent
from app.agents import codex_rollout as cr
from app.config import settings
from app.db import DB, iso

FIXTURE = Path(__file__).parent / "fixtures" / "codex_rollout.jsonl"
NOW = time.time()
RESET = int(NOW + 3 * 86400)                 # a weekly window that has not rolled over
SENTINEL = "SECRET-BASE-INSTRUCTIONS"
KA, KB = "a" * 24, "b" * 24                  # two saved Codex accounts' slot keys
ID_A, ID_B = "acct-aaaaaaaa-0000-4000-8000-000000000001", "acct-bbbbbbbb-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _fresh_rollout_state(tmp_path, monkeypatch):
    """The module's caches and Tailer clocks start and end empty; and the Sampler tick some tests run also runs accounts.observe, which
    reads the Claude config dir's state file and may ask `claude auth status`: keep both off the developer's real ~/.claude (as test_series)."""
    monkeypatch.setattr(settings, "claude_config_dir", tmp_path / "no-claude-config")
    monkeypatch.setattr(settings, "claude_bin", lambda: None)
    accounts.invalidate()
    cr.reset()
    yield
    cr.reset()
    accounts.invalidate()


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "db" / "ccboard.db")


@pytest.fixture
def home(codex_home):
    return codex_home


@pytest.fixture
def work(tmp_path):
    """A real directory a Codex session can have as its cwd."""
    d = tmp_path / "srv" / "shop" / "api"
    d.mkdir(parents=True)
    return d


# ------------------------------------------------------------------ builders
def jl(at, typ, payload) -> str:
    return json.dumps({"timestamp": iso(at), "type": typ, "payload": payload}, separators=(",", ":"))


def usage(total, last=None, window=258400):
    def u(n):
        return {"input_tokens": n, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": n}
    return {"total_token_usage": u(total), "last_token_usage": u(total if last is None else last), "model_context_window": window}


def win(used, minutes=10080, resets=None):
    return {"used_percent": used, "window_minutes": minutes, "resets_at": RESET if resets is None else resets}


def limits(primary=None, secondary=None, limit_id="codex", plan="plus"):
    return {"limit_id": limit_id, "primary": primary, "secondary": secondary, "credits": None, "plan_type": plan}


def token_line(at, *, info=None, rate_limits=None) -> str:
    return jl(at, "event_msg", {"type": "token_count", "info": info, "rate_limits": rate_limits})


def turn_line(at, model="gpt-5.5", effort="medium", approval="on-request", sandbox="workspace-write") -> str:
    return jl(at, "turn_context", {"cwd": "/x", "approval_policy": approval, "sandbox_policy": {"type": sandbox}, "model": model,
                                   "effort": effort, "summary": "auto"})


def make_rollout(home, *, cwd="/srv/projects/shop/api", created=None, originator="codex-tui", source="cli", account=None, lines=(),
                 mtime=None, thread_id=None, extra=None, name=None) -> Path:
    """A rollout file the way Codex lays it out: sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl, a session_meta first line (with a base
    instructions blob that must never leak) and then `lines`. The thread id is returned as `.thread_id` on the path object's name."""
    created = NOW - 3600 if created is None else created          # an hour ago: before every row the test makes, so no row binds to it by accident
    tid = thread_id or str(uuid.uuid4())
    day = datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y/%m/%d")
    d = Path(home) / "sessions" / day
    d.mkdir(parents=True, exist_ok=True)
    payload = {"id": tid, "timestamp": iso(created), "cwd": str(cwd), "originator": originator, "cli_version": "0.160.0", "source": source,
               "model_provider": "openai", "base_instructions": {"text": SENTINEL + " " + "x" * 5000}}
    if account:
        payload["creator_account_id"] = account
    payload.update(extra or {})
    p = d / (name or f"rollout-{datetime.fromtimestamp(created, tz=timezone.utc).strftime('%Y-%m-%dT%H-%M-%S')}-{tid}.jsonl")
    p.write_text("\n".join([jl(created, "session_meta", payload), *lines]) + "\n")
    t = max(created, time.time()) if mtime is None else mtime
    os.utime(p, (t, t))
    return p


def meta_id(path: Path) -> str:
    return json.loads(path.read_text().split("\n", 1)[0])["payload"]["id"]


def add_codex_row(db, work, name="shop--api--s1", *, flags=None, cwd="default", agent="codex", sid=None):
    return db.add_session(tmux_name=name, project="shop", repo="api", name=name.split("--")[-1], launcher="session", agent=agent,
                          cwd=str(work) if cwd == "default" else cwd, flags=flags, claude_session_id=sid)


def row_of(db, name="shop--api--s1"):
    return db.open_row(name)


def backdate(db, row_id, seconds):
    with db.lock:
        db.conn.execute("UPDATE sessions SET created_at=? WHERE id=?", (iso(NOW - seconds), row_id))


def accounts_kv(db, current=KA, *, plan="plus"):
    db.kv_set("codex_accounts", {KA: {"key": KA, "label": "Work", "account_id": ID_A, "plan": plan},
                                 KB: {"key": KB, "label": "Home", "account_id": ID_B, "plan": plan}})
    if current:
        db.kv_set("codex_account_current", {"key": current, "since": iso(NOW - 86400)})


def series_last(db, series, key):
    return db.sample_last(series, key)


def kv(db, key="rate_limits_codex"):
    rec = db.kv_get(key)
    return rec["value"] if rec else None


# ================================================================== parse_tail
def test_the_fixture_rollout_is_read_from_the_newest_line_back():
    out = cr.parse_tail(FIXTURE.read_text())
    assert out["last_token_usage"]["total"] == 25840 and out["total_token_usage"]["total"] == 54600
    assert out["model_context_window"] == 258400 and out["token_at"] == "2026-10-04T10:05:30.000Z"
    rl = out["rate_limits"]
    assert rl["limit_id"] == "codex" and rl["plan_type"] == "plus" and rl["secondary"] is None and rl["reached"] is False
    assert rl["primary"] == {"used_percent": 18.0, "window_minutes": 10080, "resets_at": 1791349200}
    assert out["rate_limits_at"] == "2026-10-04T10:05:30.000Z"
    assert out["config"] == {"model": "gpt-5.5", "effort": "high", "approval": "on-request", "sandbox": "workspace-write",
                             "at": "2026-10-04T10:05:00.000Z"}


def test_a_message_that_merely_says_token_count_is_not_a_token_count():
    line = jl(NOW, "response_item", {"type": "message", "content": [{"text": "the token_count event and turn_context"}]})
    out = cr.parse_tail(line + "\n")
    assert out["last_token_usage"] is None and out["rate_limits"] is None and out["config"] is None


def test_context_percent_is_the_last_call_over_the_window_not_the_cumulative_total():
    parsed = cr.parse_tail(FIXTURE.read_text())
    stats = cr.build_stats(cr.session_meta(FIXTURE), parsed)
    assert stats["context_pct"] == 10.0                        # 25840 / 258400, not 54600 / 258400 (21.1)
    assert stats["context_size"] == 258400 and stats["cost_usd"] is None and stats["version"] == "0.160.0"
    assert stats["model"] == stats["model_id"] == "gpt-5.5" and stats["effort"] == "high"
    assert stats["approval"] == "on-request" and stats["sandbox"] == "workspace-write"
    assert stats["tokens"] == {"input": 52000, "cached": 40000, "output": 2600, "reasoning": 900, "total": 54600, "last": 25840}


def test_the_context_window_falls_back_to_the_session_meta_and_the_percent_is_capped():
    p = cr.parse_tail(token_line(NOW, info={"last_token_usage": {"total_tokens": 500}}) + "\n")
    assert p["model_context_window"] is None
    assert cr.build_stats({"context_window": 1000}, p)["context_pct"] == 50.0
    assert cr.build_stats({"context_window": 100}, p)["context_pct"] == 100.0
    assert cr.build_stats({}, p)["context_pct"] is None and cr.build_stats({}, p)["context_size"] is None


def test_a_null_info_event_still_carries_the_rate_limits():
    text = token_line(NOW, info=None, rate_limits=limits(win(7.5))) + "\n"
    out = cr.parse_tail(text)
    assert out["last_token_usage"] is None and out["total_token_usage"] is None and out["model_context_window"] is None
    assert out["rate_limits"]["primary"]["used_percent"] == 7.5


def test_usage_and_rate_limits_come_from_the_newest_event_that_has_each():
    text = "\n".join([token_line(NOW - 30, info=usage(1000), rate_limits=limits(win(10))),
                      token_line(NOW - 20, info=None, rate_limits=limits(win(12)))]) + "\n"
    out = cr.parse_tail(text)
    assert out["last_token_usage"]["total"] == 1000 and out["rate_limits"]["primary"]["used_percent"] == 12


def test_the_flat_token_count_shape_is_read_too():
    payload = {"type": "token_count", "last_token_usage": {"total_tokens": 300}, "total_token_usage": {"total_tokens": 900},
               "model_context_window": 3000, "rate_limits": limits(win(5))}
    out = cr.parse_tail(jl(NOW, "event_msg", payload) + "\n")
    assert out["last_token_usage"]["total"] == 300 and out["total_token_usage"]["total"] == 900 and out["model_context_window"] == 3000
    assert out["rate_limits"]["primary"]["used_percent"] == 5


def test_a_partial_first_line_is_dropped_and_the_rest_still_read():
    full = FIXTURE.read_text()
    lines = full.strip().split("\n")
    cut = lines[-1][40:] + "\n"                                  # the last line cut in half: it is no JSON
    out = cr.parse_tail(cut)
    assert out["last_token_usage"] is None and out["rate_limits"] is None
    out = cr.parse_tail(lines[-2][25:] + "\n" + lines[-1] + "\n")
    assert out["last_token_usage"]["total"] == 25840 and out["rate_limits"]["primary"]["used_percent"] == 18.0
    assert out["config"] is None                                 # the cut turn_context line is skipped, not half-read


def test_nothing_to_find_is_all_none_never_an_error():
    empty = {"last_token_usage": None, "total_token_usage": None, "model_context_window": None, "token_at": None, "rate_limits": None,
             "rate_limits_at": None, "config": None, "error": None, "turn_at": None}
    for text in ("", "\n\n", "not json\n{{{\n", '{"type":"event_msg","payload":"token_count"}\n', "[1,2]\n", None, 5, b"token_count"):
        assert cr.parse_tail(text) == empty
    assert cr.parse_tail('{"payload": {"type": "token_count", "info": {"last_token_usage": "x"}, "rate_limits": 3}}\n') == empty


def test_windows_are_validated_and_clamped():
    rl = lambda **w: cr.parse_tail(token_line(NOW, rate_limits=limits(**w)) + "\n")["rate_limits"]       # noqa: E731
    assert rl(primary={"used_percent": "12"}) is None and rl(primary={"used_percent": True}) is None
    assert rl(primary={"used_percent": float("nan")}) is None and rl(primary={"used_percent": None}, secondary=None) is None
    assert rl(primary=win(250))["primary"]["used_percent"] == 100.0 and rl(primary=win(-4))["primary"]["used_percent"] == 0.0
    both = rl(primary=win(40, 300), secondary=win(61, 10080))
    assert both["primary"]["window_minutes"] == 300 and both["secondary"]["used_percent"] == 61
    assert rl(primary={"used_percent": 3})["primary"] == {"used_percent": 3.0, "window_minutes": None, "resets_at": None}


def test_reset_in_seconds_is_resolved_against_the_event_time():
    w = {"used_percent": 10, "window_minutes": 300, "resets_in_seconds": 600}
    out = cr.parse_tail(jl(1_800_000_000, "event_msg", {"type": "token_count", "rate_limits": limits(w)}) + "\n")
    assert out["rate_limits"]["primary"]["resets_at"] == 1_800_000_600


def test_reached_is_a_full_window_or_the_reached_type():
    rl = lambda r: cr.parse_tail(token_line(NOW, rate_limits=r) + "\n")["rate_limits"]                  # noqa: E731
    assert rl(limits(win(100)))["reached"] is True and rl(limits(win(99.9)))["reached"] is False
    assert rl({**limits(win(10)), "rate_limit_reached_type": "primary"})["reached"] is True
    assert rl({**limits(win(10)), "credits": {"has_credits": True, "unlimited": False, "balance": "5.0", "junk": 1}})["credits"] == {
        "has_credits": True, "unlimited": False, "balance": "5.0"}


def test_window_kinds():
    assert [cr.window_kind(m) for m in (300, 360, 361, 1440, 7200, 10080, None, "x")] == ["5h", "5h", "361m", "1440m", "7d", "7d", "other",
                                                                                      "other"]


def test_settings_are_filled_newest_first_across_both_event_kinds():
    text = "\n".join([turn_line(NOW - 50, "gpt-5.5", "low", "never", "read-only"),
                      jl(NOW - 10, "event_msg", {"type": "thread_settings_applied", "settings": {"model": "gpt-5.6-sol",
                                                                                                    "reasoning_effort": "xhigh"}})]) + "\n"
    cfg = cr.parse_tail(text)["config"]
    assert cfg["model"] == "gpt-5.6-sol" and cfg["effort"] == "xhigh"            # the newest event
    assert cfg["approval"] == "never" and cfg["sandbox"] == "read-only"           # from the older turn_context it did not repeat
    assert cfg["at"] == iso(NOW - 10)
    nested = jl(NOW, "turn_context", {"collaboration_mode": {"settings": {"model": "m1", "reasoning_effort": "high"}},
                                      "approval_policy": {"granular": {"sandbox_approval": True}}, "sandbox_policy": "danger-full-access"})
    cfg = cr.parse_tail(nested + "\n")["config"]
    assert (cfg["model"], cfg["effort"], cfg["approval"], cfg["sandbox"]) == ("m1", "high", "granular", "danger-full-access")


# ------------------------------------------------------------------ reading files
def test_the_tail_of_a_large_file_is_a_seek_not_a_whole_read(tmp_path):
    p = tmp_path / "big.jsonl"
    filler = jl(NOW, "response_item", {"type": "message", "text": "y" * 900}) + "\n"
    with open(p, "w") as f:
        for _ in range(7000):                                    # ~6.5 MB
            f.write(filler)
        f.write(token_line(NOW, info=usage(777), rate_limits=limits(win(33))) + "\n")
    assert p.stat().st_size > 6_000_000
    text = cr.read_tail(p)
    assert 0 < len(text.encode()) <= cr.TAIL_BYTES
    out = cr.parse_tail(text)
    assert out["last_token_usage"]["total"] == 777 and out["rate_limits"]["primary"]["used_percent"] == 33


def test_a_small_file_is_read_whole_and_a_missing_one_is_empty(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("abc\n")
    assert cr.read_tail(p) == "abc\n" and cr.read_tail(tmp_path / "nope.jsonl") == "" and cr.read_tail(tmp_path) == ""


# ================================================================== session_meta
def test_session_meta_returns_the_whitelisted_keys_and_never_the_instructions(home):
    p = make_rollout(home, account=ID_A, extra={"creator_user_id": "user-1", "history_mode": "persisted", "runtime_workspace_roots": ["/x"]})
    m = cr.session_meta(p)
    assert set(m) == {"id", "cwd", "originator", "source", "thread_source", "cli_version", "model_provider", "context_window", "account_id",
                      "user_id", "forked_from_id", "timestamp", "created"}
    assert m["originator"] == "codex-tui" and m["cwd"] == "/srv/projects/shop/api" and m["source"] == "cli"
    assert m["account_id"] == ID_A and m["user_id"] == "user-1" and m["cli_version"] == "0.160.0"
    assert abs(m["created"] - (NOW - 3600)) < 2 and m["id"] == meta_id(p)
    assert SENTINEL not in json.dumps(m) and "runtime_workspace_roots" not in m
    fx = cr.session_meta(FIXTURE)
    assert fx["context_window"] == 258400 and fx["id"] == "0199aaaa-1111-7222-8333-444455556666" and SENTINEL not in json.dumps(fx)


def test_a_file_that_is_not_a_rollout_has_no_meta(tmp_path):
    cases = {"empty": b"", "text": b"hello\n", "badjson": b'{"type":"session_meta",\n', "wrong": b'{"type":"event_msg","payload":{}}\n',
             "payload": b'{"type":"session_meta","payload":"x"}\n', "binary": b"\xff\xfe\x00" * 20, "list": b"[]\n"}
    for name, data in cases.items():
        (tmp_path / name).write_bytes(data)
        assert cr.session_meta(tmp_path / name) is None, name
    assert cr.session_meta(tmp_path / "missing") is None and cr.session_meta(tmp_path) is None


def test_a_first_line_longer_than_the_cap_is_not_a_meta(tmp_path):
    p = tmp_path / "huge.jsonl"
    p.write_text(json.dumps({"type": "session_meta", "payload": {"id": "x1", "base_instructions": "z" * (cr.META_MAX + 10)}}) + "\n")
    assert cr.session_meta(p) is None


def test_the_source_can_be_a_string_or_an_object_and_a_child_thread_is_recognised(home):
    cli = cr.session_meta(make_rollout(home, source="vscode"))
    assert cli["source"] == "vscode" and not cr.is_subthread(cli)
    child = cr.session_meta(make_rollout(home, source={"subagent": {"thread_spawn": {"parent_thread_id": "p", "depth": 1}}}))
    assert child["source"].startswith('{"subagent"') and cr.is_subthread(child)
    guardian = cr.session_meta(make_rollout(home, extra={"thread_source": "guardian"}))
    assert cr.is_subthread(guardian) and not cr.is_subthread(None) and not cr.is_subthread({})


def test_the_thread_id_may_come_as_session_id_and_a_bad_id_is_dropped(home):
    p = make_rollout(home, extra={"id": None, "session_id": "sess-1"})
    assert cr.session_meta(p)["id"] == "sess-1"
    assert cr.session_meta(make_rollout(home, extra={"id": "has space", "session_id": "bad id"}))["id"] is None


def test_recent_rollouts_walks_only_the_recent_day_directories(home):
    new = make_rollout(home, created=NOW)
    old = make_rollout(home, created=NOW - 40 * 86400, mtime=NOW - 40 * 86400)
    stale = make_rollout(home, created=NOW - 86400, mtime=NOW - 20 * 86400)
    (home / "sessions" / "notes.txt").write_text("x")
    (home / "sessions" / "2026" / "junk").mkdir(parents=True, exist_ok=True)
    (Path(new).parent / "rollout-x.txt").write_text("x")
    got = [p for _m, _s, p in cr.recent_rollouts(NOW - 8 * 86400)]
    assert new in got and old not in got and stale not in got and len(got) == 1
    assert [p for _m, _s, p in cr.recent_rollouts(NOW - 60 * 86400)].count(old) == 1


def test_no_sessions_directory_is_no_rollouts(home):
    assert cr.recent_rollouts(0) == [] and cr.tick(None, NOW) is None


# ================================================================== the Tailer: stats, samples, state untouched
def stats_of(db, name="shop--api--s1"):
    return row_of(db, name)["stats"]


def test_the_tailer_puts_the_rollout_stats_on_the_row_and_samples_the_context(db, home, work):
    p = make_rollout(home, cwd=work, lines=[turn_line(NOW - 5, "gpt-5.5", "high"),
                                            token_line(NOW - 1, info=usage(54600, 25840), rate_limits=limits(win(17)))])
    add_codex_row(db, work, flags={"transcript_path": str(p), "hook_seen": True})
    before = row_of(db)
    cr.Tailer().tail_rows(db, NOW)
    row = row_of(db)
    assert row["stats"]["model"] == "gpt-5.5" and row["stats"]["effort"] == "high" and row["stats"]["context_pct"] == 10.0
    assert row["stats"]["tokens"]["total"] == 54600 and row["stats"]["version"] == "0.160.0" and row["stats"]["cost_usd"] is None
    assert row["state"] == before["state"] and row["state_at"] == before["state_at"] and row["last_event"] == before["last_event"]
    # display only: nothing else of the row moves, except turn_seen_at (#96: the first turn the rollout shows, for hooks_missing)
    assert {k: v for k, v in row["flags"].items() if k != "turn_seen_at"} == before["flags"]
    assert row["agent_session_id"] == meta_id(p)                                           # but a row whose hook named its rollout takes its id
    ctx, tok, stok = (series_last(db, s, "shop--api--s1") for s in ("ctx", "ctx_tok", "stok"))
    assert ctx["value"] == 10.0 and ctx["meta"] == {"model": "gpt-5.5", "window": 258400}
    assert tok["value"] == 25840 and stok["value"] == 54600
    assert db.samples_query("state", None, iso(NOW - 86400)) == []


def test_an_unchanged_rollout_is_not_written_again_and_a_changed_one_is(db, home, work, monkeypatch):
    p = make_rollout(home, cwd=work, lines=[turn_line(NOW - 5), token_line(NOW - 1, info=usage(1000, 1000, 10000))])
    add_codex_row(db, work, flags={"transcript_path": str(p)})
    writes = []
    real = db.set_stats
    monkeypatch.setattr(db, "set_stats", lambda *a, **k: (writes.append(a), real(*a, **k))[1])
    t = cr.Tailer()
    assert t.tail_rows(db, NOW) == 1 and t.tail_rows(db, NOW + 1) == 0 and len(writes) == 1
    with open(p, "a") as f:
        f.write(token_line(NOW + 2, info=usage(2500, 2500, 10000)) + "\n")
    assert t.tail_rows(db, NOW + 3) == 1 and len(writes) == 2
    assert stats_of(db)["context_pct"] == 25.0
    assert series_last(db, "ctx", "shop--api--s1")["value"] == 25.0 and series_last(db, "stok", "shop--api--s1")["value"] == 2500


def test_only_codex_rows_with_a_trusted_rollout_path_are_tailed(db, home, work, tmp_path):
    p = make_rollout(home, cwd=work, lines=[turn_line(NOW), token_line(NOW, info=usage(10, 10, 100))])
    add_codex_row(db, work, "shop--api--cl", flags={"transcript_path": str(p)}, agent="claude")
    add_codex_row(db, work, "shop--api--sh", flags={"transcript_path": str(p)}, agent="shell")
    outside = tmp_path / "elsewhere" / "rollout-x.jsonl"
    outside.parent.mkdir()
    outside.write_text(p.read_text())
    add_codex_row(db, work, "shop--api--out", flags={"transcript_path": str(outside)})
    add_codex_row(db, work, "shop--api--none")
    assert cr.Tailer().tail_rows(db, NOW) == 0
    assert all(row_of(db, n)["stats"] is None for n in ("shop--api--cl", "shop--api--sh", "shop--api--out", "shop--api--none"))


def test_a_row_with_only_its_conversation_id_finds_the_rollout_by_it(db, home, work):
    t = str(uuid.uuid4())
    p = make_rollout(home, cwd=work, thread_id=t, lines=[turn_line(NOW), token_line(NOW, info=usage(50, 50, 100))])
    assert t in p.name
    add_codex_row(db, work, sid=t)
    assert cr.Tailer().tail_rows(db, NOW) == 1 and stats_of(db)["context_pct"] == 50.0


def test_a_row_without_a_rollout_is_looked_up_again_only_every_half_minute(db, home, work, monkeypatch):
    add_codex_row(db, work, sid=str(uuid.uuid4()))
    calls = []
    real = codex_agent.CodexAgent.transcript_path
    monkeypatch.setattr(codex_agent.CodexAgent, "transcript_path", lambda self, r: (calls.append(1), real(self, r))[1])
    t = cr.Tailer()
    t.tail_rows(db, NOW)
    t.tail_rows(db, NOW + 10)
    assert len(calls) == 1
    t.tail_rows(db, NOW + 31)
    assert len(calls) == 2


def test_a_vanished_rollout_and_a_garbage_one_do_not_break_the_pass(db, home, work):
    p = make_rollout(home, cwd=work, lines=[token_line(NOW, info=usage(10, 10, 100))])
    add_codex_row(db, work, flags={"transcript_path": str(p)})
    t = cr.Tailer()
    t.tail_rows(db, NOW)
    p.unlink()
    assert t.tail_rows(db, NOW + 6) == 0
    q = make_rollout(home, cwd=work)
    q.write_bytes(b"\xff\xfe\x00" * 5000)
    add_codex_row(db, work, "shop--api--s2", flags={"transcript_path": str(q)})
    assert t.tail_rows(db, NOW + 7) in (0, 1)


def test_the_settings_seen_earlier_survive_a_tail_that_no_longer_reaches_them(db, home, work):
    p = make_rollout(home, cwd=work, lines=[turn_line(NOW - 5, "gpt-5.6-sol", "xhigh", "never", "read-only"),
                                            token_line(NOW - 4, info=usage(100, 100, 1000))])
    add_codex_row(db, work, flags={"transcript_path": str(p)})
    t = cr.Tailer()
    t.tail_rows(db, NOW)
    assert stats_of(db)["model"] == "gpt-5.6-sol"
    filler = jl(NOW, "response_item", {"type": "message", "text": "y" * 900}) + "\n"
    with open(p, "a") as f:
        f.write(filler * 400)                                    # 360 KB: the turn_context is out of the tail now
        f.write(token_line(NOW + 1, info=usage(300, 300, 1000)) + "\n")
    assert cr.parse_tail(cr.read_tail(p))["config"] is None
    t.tail_rows(db, NOW + 6)
    s = stats_of(db)
    assert s["context_pct"] == 30.0 and (s["model"], s["effort"], s["approval"], s["sandbox"]) == ("gpt-5.6-sol", "xhigh", "never", "read-only")


def test_the_pass_gap_is_five_seconds_and_the_rate_limit_gap_a_minute(db, home, monkeypatch):
    t = cr.Tailer()
    calls = {"tail": 0, "rl": 0}
    monkeypatch.setattr(t, "tail_rows", lambda d, n: calls.__setitem__("tail", calls["tail"] + 1))
    monkeypatch.setattr(t, "rate_limits", lambda d, n: calls.__setitem__("rl", calls["rl"] + 1))
    for dt in (0, 1, 4, 5, 9, 10, 59, 60, 61):
        t.step(db, NOW + dt)
    assert calls == {"tail": 4, "rl": 2}                         # tail at 0, 5, 10, 60; rate limits at 0, 60
    t.step(db, NOW - 1000)                                       # a clock that went back runs both again
    assert calls == {"tail": 5, "rl": 3}


def test_a_failing_part_never_stops_the_other_or_raises(db, monkeypatch, caplog):
    t = cr.Tailer()
    ran = []
    monkeypatch.setattr(t, "tail_rows", lambda d, n: 1 / 0)
    monkeypatch.setattr(t, "rate_limits", lambda d, n: ran.append(1))
    with caplog.at_level(logging.WARNING, logger="ccboard.codex_rollout"):
        t.step(db, NOW)
    assert ran == [1] and "ZeroDivisionError" in caplog.text
    monkeypatch.setattr(cr._tailer, "step", lambda d, n: 1 / 0)
    cr.tick(db, NOW)                                              # the Sampler hook swallows it too


# ------------------------------------------------------------------ confirming a typed /model or /reasoning
def pending(cmd, arg, at, **before):
    return {"cmd": cmd, "arg": arg, "at": iso(at), "before": {"model": "gpt-5.5", "model_id": "gpt-5.5", "effort": "medium", "fast": None,
                                                                 **before}}


def test_a_model_command_is_confirmed_by_a_turn_context_after_it_and_not_by_an_older_one(db, home, work):
    p = make_rollout(home, cwd=work, lines=[turn_line(NOW - 60, "gpt-5.5", "medium"), token_line(NOW - 59, info=usage(10, 10, 100))])
    add_codex_row(db, work, flags={"transcript_path": str(p), "pending_cmd": pending("model", "gpt-5.5-mini", NOW - 5)})
    t = cr.Tailer()
    t.tail_rows(db, NOW)
    f = row_of(db)["flags"]
    assert f["pending_cmd"]["cmd"] == "model" and "last_cmd" not in f              # the only turn_context is from before the command
    with open(p, "a") as fh:
        fh.write(turn_line(NOW + 1, "gpt-5.5-mini", "medium") + "\n")
    t.tail_rows(db, NOW + 2)
    f = row_of(db)["flags"]
    assert f["last_cmd"] == {"cmd": "model", "arg": "gpt-5.5-mini", "at": iso(NOW - 5), "confirmed": True} and "pending_cmd" not in f


def test_a_model_command_is_confirmed_by_a_change_even_when_the_argument_is_an_alias(db, home, work):
    p = make_rollout(home, cwd=work, lines=[turn_line(NOW + 1, "gpt-5.6-sol", "medium")])
    add_codex_row(db, work, flags={"transcript_path": str(p), "pending_cmd": pending("model", "sol", NOW)})
    cr.Tailer().tail_rows(db, NOW + 2)
    assert row_of(db)["flags"]["last_cmd"]["confirmed"] is True


def test_a_reasoning_command_is_confirmed_by_the_effort_and_a_thread_settings_event_counts(db, home, work):
    p = make_rollout(home, cwd=work, lines=[turn_line(NOW - 60, "gpt-5.5", "medium")])
    add_codex_row(db, work, flags={"transcript_path": str(p), "pending_cmd": pending("reasoning", "xhigh", NOW)})
    t = cr.Tailer()
    t.tail_rows(db, NOW + 1)
    assert "last_cmd" not in row_of(db)["flags"]
    with open(p, "a") as fh:
        fh.write(jl(NOW + 2, "event_msg", {"type": "thread_settings_applied", "reasoning_effort": "xhigh"}) + "\n")
    t.tail_rows(db, NOW + 3)
    assert row_of(db)["flags"]["last_cmd"] == {"cmd": "reasoning", "arg": "xhigh", "at": iso(NOW), "confirmed": True}
    assert stats_of(db)["effort"] == "xhigh"


def test_a_command_nothing_confirmed_is_unconfirmed_after_twenty_seconds(db, home, work):
    p = make_rollout(home, cwd=work, lines=[turn_line(NOW - 60, "gpt-5.5", "medium")])
    add_codex_row(db, work, flags={"transcript_path": str(p), "pending_cmd": pending("reasoning", "high", NOW)})
    t = cr.Tailer()
    t.tail_rows(db, NOW + 19)
    assert row_of(db)["flags"]["pending_cmd"]["cmd"] == "reasoning"
    t.tail_rows(db, NOW + 21)
    f = row_of(db)["flags"]
    assert f["last_cmd"]["confirmed"] is False and f["last_cmd"]["cmd"] == "reasoning" and "pending_cmd" not in f


def test_a_pending_command_with_no_stamp_is_settled_as_unconfirmed(db, home, work):
    p = make_rollout(home, cwd=work, lines=[turn_line(NOW, "gpt-5.5", "medium")])
    add_codex_row(db, work, flags={"transcript_path": str(p), "pending_cmd": {"cmd": "model", "arg": "x"}})
    cr.Tailer().tail_rows(db, NOW)
    assert row_of(db)["flags"]["last_cmd"]["confirmed"] is False


# ================================================================== binding an unbound row to its rollout
def test_an_unbound_row_binds_to_the_rollout_of_its_cwd_and_the_tailer_follows(db, home, work):
    add_codex_row(db, work)
    p = make_rollout(home, cwd=work, created=time.time() + 1, lines=[turn_line(NOW + 1), token_line(NOW + 1, info=usage(10, 10, 100))])
    assert cr.bind_unbound_rows(db) == 1
    row = row_of(db)
    assert row["agent_session_id"] == meta_id(p) and row["flags"]["transcript_path"] == str(p)
    assert cr.bind_unbound_rows(db) == 0                                         # bound rows are not looked at again
    cr.Tailer().tail_rows(db, NOW)
    assert stats_of(db)["context_pct"] == 10.0                                   # the flag the bind set is what the tailer reads


def test_one_tailer_pass_binds_and_then_tails(db, home, work):
    add_codex_row(db, work)
    make_rollout(home, cwd=work, created=time.time() + 1, lines=[turn_line(NOW), token_line(NOW, info=usage(20, 20, 100))])
    assert cr.Tailer().tail_rows(db, NOW) == 1
    assert row_of(db)["agent_session_id"] and stats_of(db)["context_pct"] == 20.0


def test_only_codex_tui_rollouts_in_the_rows_cwd_inside_its_life_bind(db, home, work, tmp_path):
    rid = add_codex_row(db, work)
    other = tmp_path / "srv" / "other"
    other.mkdir()
    now = time.time()
    make_rollout(home, cwd=work, created=now + 1, originator="hermes", source="vscode")
    make_rollout(home, cwd=work, created=now + 1, originator="codex_exec")
    make_rollout(home, cwd=work, created=now + 1, source={"subagent": {"thread_spawn": {"depth": 1}}})
    make_rollout(home, cwd=work, created=now + 1, extra={"thread_source": "guardian"})
    make_rollout(home, cwd=other, created=now + 1)
    make_rollout(home, cwd=work, created=now - 600)                              # before the row existed
    (Path(home) / "sessions" / "rollout-flat.jsonl").write_text("not a rollout\n")
    assert cr.bind_unbound_rows(db) == 0 and row_of(db)["agent_session_id"] is None
    good = make_rollout(home, cwd=work, created=now + 2)
    assert cr.bind_unbound_rows(db) == 1 and row_of(db)["agent_session_id"] == meta_id(good) and rid


def test_a_row_made_a_few_seconds_after_its_rollout_stamp_still_matches(db, home, work):
    add_codex_row(db, work)
    p = make_rollout(home, cwd=work, created=time.time() - 3)
    assert cr.bind_unbound_rows(db) == 1 and row_of(db)["agent_session_id"] == meta_id(p)


def test_several_rows_bind_first_in_first_out(db, home, work):
    r1 = add_codex_row(db, work, "shop--api--s1")
    r2 = add_codex_row(db, work, "shop--api--s2")
    backdate(db, r1, 120)
    backdate(db, r2, 60)
    p1 = make_rollout(home, cwd=work, created=NOW - 100)
    p2 = make_rollout(home, cwd=work, created=NOW - 50)
    assert cr.bind_unbound_rows(db) == 2
    assert row_of(db, "shop--api--s1")["agent_session_id"] == meta_id(p1) and row_of(db, "shop--api--s2")["agent_session_id"] == meta_id(p2)


def test_an_ambiguous_pairing_binds_nothing(db, home, work):
    r1 = add_codex_row(db, work, "shop--api--s1")
    r2 = add_codex_row(db, work, "shop--api--s2")
    backdate(db, r1, 120)
    backdate(db, r2, 60)
    make_rollout(home, cwd=work, created=NOW - 30)                               # two rows, one rollout: whose?
    assert cr.bind_unbound_rows(db) == 0
    assert row_of(db, "shop--api--s1")["agent_session_id"] is None and row_of(db, "shop--api--s2")["agent_session_id"] is None
    make_rollout(home, cwd=work, created=NOW - 20)
    make_rollout(home, cwd=work, created=NOW - 10)                               # three rollouts for two rows: a /new, a child?
    assert cr.bind_unbound_rows(db) == 0


def test_a_pairing_with_a_rollout_older_than_its_row_is_refused(db, home, work):
    r1 = add_codex_row(db, work, "shop--api--s1")
    r2 = add_codex_row(db, work, "shop--api--s2")
    backdate(db, r1, 100)
    backdate(db, r2, 10)
    make_rollout(home, cwd=work, created=NOW - 60)                               # after s1, before s2
    make_rollout(home, cwd=work, created=NOW - 50)                               # also before s2: s2 has no rollout of its own yet
    assert cr.bind_unbound_rows(db) == 0


def test_a_rollout_some_row_already_owns_is_not_a_candidate(db, home, work):
    taken = make_rollout(home, cwd=work, created=time.time() + 1)
    ended = add_codex_row(db, work, "shop--api--old", sid=meta_id(taken))
    db.end("shop--api--old")
    add_codex_row(db, work, "shop--api--new")
    assert ended and cr.bind_unbound_rows(db) == 0
    hooked = make_rollout(home, cwd=work, created=time.time() + 2)
    add_codex_row(db, work, "shop--api--hooked", cwd="/nowhere", sid=meta_id(hooked))
    assert cr.bind_unbound_rows(db) == 0


def test_rows_that_already_have_an_id_and_non_codex_rows_are_left_alone(db, home, work):
    add_codex_row(db, work, "shop--api--s1", sid="hook-bound-id")
    add_codex_row(db, work, "shop--api--cl", agent="claude")
    make_rollout(home, cwd=work, created=time.time() + 1)
    assert cr.bind_unbound_rows(db) == 0
    assert row_of(db, "shop--api--s1")["agent_session_id"] == "hook-bound-id" and row_of(db, "shop--api--cl")["agent_session_id"] is None


def test_the_cwd_is_compared_resolved_and_a_row_without_one_uses_its_repo_folder(db, home, work, tmp_path, projects_dir):
    link = tmp_path / "link"
    link.symlink_to(work, target_is_directory=True)
    add_codex_row(db, work, cwd=str(link) + "/")
    p = make_rollout(home, cwd=work, created=time.time() + 1)
    assert cr.bind_unbound_rows(db) == 1 and row_of(db)["agent_session_id"] == meta_id(p)
    repo = projects_dir / "shop" / "api"
    repo.mkdir(parents=True)
    add_codex_row(db, work, "shop--api--nocwd", cwd=None)
    q = make_rollout(home, cwd=repo, created=time.time() + 2)
    assert cr.bind_unbound_rows(db) == 1 and row_of(db, "shop--api--nocwd")["agent_session_id"] == meta_id(q)


def test_a_hook_that_binds_the_row_first_wins(db, home, work, monkeypatch):
    add_codex_row(db, work)
    make_rollout(home, cwd=work, created=time.time() + 1)
    real = cr.recent_rollouts

    def racing(since, limit=cr.MAX_STAT):
        out = real(since, limit)
        with db.lock:
            db.conn.execute("UPDATE sessions SET claude_session_id='from-the-hook' WHERE tmux_name='shop--api--s1'")
        return out
    monkeypatch.setattr(cr, "recent_rollouts", racing)
    assert cr.bind_unbound_rows(db) == 0 and row_of(db)["agent_session_id"] == "from-the-hook"


def test_the_bind_never_raises_and_the_adapter_and_hook_seams_reach_it(db, home, work, monkeypatch):
    assert cr.bind_unbound_rows(None) == 0
    add_codex_row(db, work)
    p = make_rollout(home, cwd=work, created=time.time() + 1)
    assert codex_agent.bind_unbound_rows(db) == 1 and row_of(db)["agent_session_id"] == meta_id(p)
    with monkeypatch.context() as m:
        m.setattr(db, "open_rows", lambda: 1 / 0)
        assert cr.bind_unbound_rows(db) == 0 and hooks.bind_unbound_rows(db) == 0
    add_codex_row(db, work, "shop--api--s9")
    make_rollout(home, cwd=work, created=time.time() + 5)
    assert hooks.bind_unbound_rows(db) == 1


# ================================================================== rate limits
def write_rl(home, *, at=None, rate_limits, account=None, name=None, cwd="/srv/projects/shop/api", info=None, created=None, **kw):
    at = NOW - 10 if at is None else at
    return make_rollout(home, cwd=cwd, account=account, created=created if created is not None else at - 60,
                        lines=[token_line(at, info=usage(100, 100) if info is None else info, rate_limits=rate_limits)], **kw)


def poll(db, now=None):
    return cr.Tailer().rate_limits(db, NOW if now is None else now)


def test_a_weekly_only_plan_feeds_the_7d_series_and_the_kv(db, home):
    write_rl(home, at=NOW - 30, rate_limits=limits(win(17)))
    out = poll(db)
    v = kv(db)
    assert v == {"limit_id": "codex", "plan_type": "plus", "primary": {"used_percent": 17.0, "window_minutes": 10080, "resets_at": RESET},
                 "secondary": None, "credits": None, "reached": False, "observed_at": iso(NOW - 30), "account": None}
    assert out == {None: v}
    last = series_last(db, "rl_7d", "codex")
    assert last["value"] == 17.0 and last["meta"] == {"resets_at": RESET} and last["at"] == iso(NOW - 30)
    assert series_last(db, "rl_5h", "codex") is None and db.samples_query("rl_5h", None, iso(0)) == []


def test_both_windows_chart_by_their_minutes_and_the_kv_keeps_both(db, home):
    write_rl(home, rate_limits=limits(win(40, 300, int(NOW + 3600)), win(61, 10080)))
    poll(db)
    v = kv(db)
    assert v["primary"]["window_minutes"] == 300 and v["secondary"]["window_minutes"] == 10080
    assert series_last(db, "rl_5h", "codex")["value"] == 40.0 and series_last(db, "rl_7d", "codex")["value"] == 61.0


def test_a_window_of_another_length_is_shown_but_never_charted(db, home):
    write_rl(home, rate_limits=limits(win(33, 1440, int(NOW + 600))))
    poll(db)
    assert kv(db)["primary"]["window_minutes"] == 1440
    assert series_last(db, "rl_5h", "codex") is None and series_last(db, "rl_7d", "codex") is None
    assert series_last(db, "rl_5h", "cacct:" + KA) is None


def test_the_other_limit_that_sits_at_zero_does_not_flip_the_pill(db, home):
    write_rl(home, at=NOW - 100, rate_limits=limits(win(17)))
    write_rl(home, at=NOW - 5, rate_limits=limits(win(0, 300, int(NOW + 900)), win(0, 10080), limit_id="codex_bengalfox"))
    poll(db)
    v = kv(db)
    assert v["limit_id"] == "codex" and v["primary"]["used_percent"] == 17.0 and v["secondary"] is None
    assert v["observed_at"] == iso(NOW - 100) and series_last(db, "rl_5h", "codex") is None


def test_an_other_limit_with_use_counts_and_the_highest_percent_per_window_wins(db, home):
    write_rl(home, at=NOW - 100, rate_limits=limits(win(17)))
    write_rl(home, at=NOW - 50, rate_limits=limits(win(30, 300, int(NOW + 900)), win(5, 10080), limit_id="codex_spark"))
    poll(db)
    v = kv(db)
    assert v["limit_id"] == "codex" and v["primary"]["window_minutes"] == 300 and v["primary"]["used_percent"] == 30.0
    assert v["secondary"]["window_minutes"] == 10080 and v["secondary"]["used_percent"] == 17.0      # codex's 17 beats spark's 5
    assert series_last(db, "rl_5h", "codex")["value"] == 30.0 and series_last(db, "rl_7d", "codex")["value"] == 17.0


def test_when_only_an_all_zero_other_limit_exists_the_zero_stands(db, home):
    write_rl(home, rate_limits=limits(win(0), limit_id="codex_bengalfox"))
    poll(db)
    assert kv(db)["limit_id"] == "codex_bengalfox" and kv(db)["primary"]["used_percent"] == 0.0


def test_the_newest_reading_of_a_limit_wins_over_an_older_rollout(db, home):
    write_rl(home, at=NOW - 500, rate_limits=limits(win(12)))
    write_rl(home, at=NOW - 20, rate_limits=limits(win(19)))
    poll(db)
    assert kv(db)["primary"]["used_percent"] == 19.0 and kv(db)["observed_at"] == iso(NOW - 20)


def test_within_one_rollout_the_newest_event_is_the_reading(db, home):
    make_rollout(home, lines=[token_line(NOW - 90, rate_limits=limits(win(10))), token_line(NOW - 30, rate_limits=limits(win(14)))])
    poll(db)
    assert kv(db)["primary"]["used_percent"] == 14.0


def test_a_window_that_has_reset_is_no_reading_and_a_fully_stale_one_stands_unreached(db, home, tmp_path):
    write_rl(home, at=NOW - 3600, rate_limits=limits(win(100, 300, int(NOW - 600)), win(40, 10080)))
    poll(db)
    v = kv(db)
    assert v["primary"]["window_minutes"] == 10080 and v["primary"]["used_percent"] == 40.0 and v["secondary"] is None
    assert series_last(db, "rl_5h", "codex") is None
    old = int(NOW - 7200)
    db2 = DB(tmp_path / "second" / "ccboard.db")
    for p in (Path(home) / "sessions").rglob("rollout-*.jsonl"):
        p.unlink()
    write_rl(home, at=NOW - 3 * 86400, rate_limits=limits(win(100, 10080, old)))
    cr.reset()
    poll(db2)
    v = kv(db2)
    assert v["primary"]["resets_at"] == old and v["primary"]["used_percent"] == 100.0 and v["reached"] is False
    assert v["observed_at"] == iso(NOW - 3 * 86400)


def test_nothing_is_written_when_the_reading_has_not_changed(db, home):
    write_rl(home, rate_limits=limits(win(17)))
    t = cr.Tailer()
    t.rate_limits(db, NOW)
    first = db.kv_get("rate_limits_codex")
    n = len(db.samples_query("rl_7d", None, iso(0)))
    t.rate_limits(db, NOW + 60)
    assert db.kv_get("rate_limits_codex") == first and len(db.samples_query("rl_7d", None, iso(0))) == n == 1


def test_an_older_reading_never_replaces_a_newer_one_of_the_same_account(db, home):
    newer = write_rl(home, at=NOW - 10, rate_limits=limits(win(25)))
    poll(db)
    newer.unlink()
    cr.reset()
    write_rl(home, at=NOW - 900, rate_limits=limits(win(20)))
    poll(db, NOW + 5)
    assert kv(db)["primary"]["used_percent"] == 25.0
    assert series_last(db, "rl_7d", "codex")["value"] == 25.0


def test_samples_carry_the_events_own_time_and_the_chart_keeps_climbing(db, home):
    p = write_rl(home, at=NOW - 200, rate_limits=limits(win(10)))
    poll(db, NOW - 100)
    with open(p, "a") as f:
        f.write(token_line(NOW - 50, rate_limits=limits(win(15))) + "\n")
    poll(db, NOW)
    rows = db.samples_query("rl_7d", ["codex"], iso(0))
    assert [(r[0], r[2]) for r in rows] == [(iso(NOW - 200), 10.0), (iso(NOW - 50), 15.0)]


def test_an_event_stamped_in_the_future_is_clamped_to_now(db, home):
    write_rl(home, at=NOW + 5000, rate_limits=limits(win(10)))
    poll(db)
    assert series_last(db, "rl_7d", "codex")["at"] == iso(NOW) and kv(db)["observed_at"] == iso(NOW)


# ------------------------------------------------------------------ per Codex account
def test_a_reading_goes_to_the_account_its_creator_id_names_and_only_the_current_one_to_codex_and_the_kv(db, home):
    accounts_kv(db, current=KA)
    write_rl(home, at=NOW - 20, rate_limits=limits(win(17)), account=ID_A)
    write_rl(home, at=NOW - 10, rate_limits=limits(win(60)), account=ID_B)
    out = poll(db)
    assert series_last(db, "rl_7d", "cacct:" + KA)["value"] == 17.0 and series_last(db, "rl_7d", "cacct:" + KB)["value"] == 60.0
    assert series_last(db, "rl_7d", "codex")["value"] == 17.0                       # the current account's: B's 60 never reaches it
    v = kv(db)
    assert v["primary"]["used_percent"] == 17.0 and v["account"] == KA
    assert set(out) == {KA, KB} and out[KB]["primary"]["used_percent"] == 60.0 and out[KB]["account"] is None


def test_a_reading_of_an_account_the_board_does_not_know_belongs_to_no_account(db, home):
    """Issue #32 (c): saved accounts carry ids and the rollout's creator id matches none of them: the reading feeds only the `codex` series
    and the kv, never the current account."""
    accounts_kv(db, current=KB)
    write_rl(home, at=NOW - 20, rate_limits=limits(win(33)), account="acct-someone-else")
    poll(db)
    assert series_last(db, "rl_7d", "codex")["value"] == 33.0 and kv(db)["account"] is None
    assert series_last(db, "rl_7d", "cacct:" + KB) is None and series_last(db, "rl_7d", "cacct:" + KA) is None


def test_an_unknown_creator_id_stands_in_for_the_current_account_while_no_saved_account_has_an_id(db, home):
    db.kv_set("codex_accounts", {KA: {"key": KA, "label": "Work", "account_id": None, "plan": "plus"},
                                 KB: {"key": KB, "label": "Home", "plan": "plus"}})
    db.kv_set("codex_account_current", {"key": KB, "since": iso(NOW - 86400)})
    write_rl(home, at=NOW - 20, rate_limits=limits(win(33)), account="acct-someone-else")
    poll(db)
    assert series_last(db, "rl_7d", "cacct:" + KB)["value"] == 33.0 and kv(db)["account"] == KB
    assert cr._owner({}, "k", "x") == "k" and cr._owner({"a": {"account_id": "i"}}, "k", "x") is None
    assert cr._owner({"a": {"account_id": "i"}}, "k", "i") == "a" and cr._owner({"a": {"account_id": "i"}}, "k", None) == "k"


def test_a_rollout_without_a_creator_id_belongs_to_the_current_account(db, home):
    accounts_kv(db, current=KA)
    write_rl(home, rate_limits=limits(win(21)))
    poll(db)
    assert series_last(db, "rl_7d", "cacct:" + KA)["value"] == 21.0 and kv(db)["account"] == KA


def test_without_any_saved_account_only_the_codex_key_and_the_kv_are_written(db, home):
    write_rl(home, rate_limits=limits(win(17)), account=ID_A)
    poll(db)
    assert series_last(db, "rl_7d", "codex")["value"] == 17.0 and kv(db)["account"] is None
    assert db.samples_distinct_keys("rl_7d", iso(0)) == ["codex"]


def test_the_pill_follows_the_current_account_when_it_changes(db, home):
    accounts_kv(db, current=KA)
    write_rl(home, at=NOW - 20, rate_limits=limits(win(17)), account=ID_A)
    write_rl(home, at=NOW - 500, rate_limits=limits(win(60)), account=ID_B)
    t = cr.Tailer()
    t.rate_limits(db, NOW)
    assert kv(db)["account"] == KA
    db.kv_set("codex_account_current", {"key": KB, "since": iso(NOW)})
    t.rate_limits(db, NOW + 60)
    v = kv(db)
    assert v["account"] == KB and v["primary"]["used_percent"] == 60.0               # B's reading is older than A's, and still replaces it
    codex = series_last(db, "rl_7d", "codex")
    assert codex["value"] == 60.0 and codex["meta"]["acct"] == KB and codex["at"] == iso(NOW - 20)   # stamped no earlier than the series' last point
    assert series_last(db, "rl_7d", "cacct:" + KA)["value"] == 17.0 and series_last(db, "rl_7d", "cacct:" + KB)["at"] == iso(NOW - 500)


def test_a_known_account_that_is_not_current_does_not_touch_the_kv_or_the_codex_key(db, home):
    accounts_kv(db, current=None)
    write_rl(home, rate_limits=limits(win(60)), account=ID_B)
    poll(db)
    assert kv(db) is None and series_last(db, "rl_7d", "codex") is None and series_last(db, "rl_7d", "cacct:" + KB)["value"] == 60.0


def test_the_account_kv_is_read_defensively(db, home):
    db.kv_set("codex_accounts", ["junk"])
    db.kv_set("codex_account_current", {"key": 5})
    write_rl(home, rate_limits=limits(win(9)))
    assert poll(db)[None]["primary"]["used_percent"] == 9.0
    db.kv_set("codex_accounts", {KA: "x", KB: {"account_id": ID_B}})
    assert cr._accounts(db) == ({KB: {"account_id": ID_B}}, None)


# ------------------------------------------------------------------ the notice
@pytest.fixture
def sent(monkeypatch):
    got = []
    monkeypatch.setattr(notify, "any_channel", lambda: True)
    monkeypatch.setattr(notify, "send", lambda n: (got.append(n), True)[1])
    return got


def test_a_reached_window_is_announced_once_and_a_new_window_again(db, home, sent):
    accounts_kv(db, current=KA)
    p = write_rl(home, at=NOW - 20, rate_limits=limits(win(100)), account=ID_A)
    t = cr.Tailer()
    t.rate_limits(db, NOW)
    t.rate_limits(db, NOW + 60)
    t.rate_limits(db, NOW + 120)
    assert len(sent) == 1 and db.kv_get(f"codex_rl_notified_{RESET}") is not None
    n = sent[0]
    assert n.title == "Codex rate limited" and n.kind == "rate_limit" and n.agent == "codex" and n.path == "/#/usage"
    assert "weekly window used 100%" in n.body and "Work" in n.body and "plus" in n.body and "resets" in n.body
    assert kv(db)["reached"] is True
    with open(p, "a") as f:
        f.write(token_line(NOW + 200, rate_limits=limits(win(100, 10080, RESET + 604800))) + "\n")
    t.rate_limits(db, NOW + 300)
    assert len(sent) == 2 and db.kv_get(f"codex_rl_notified_{RESET + 604800}") is not None


def test_both_windows_reached_announce_each_and_a_rearmed_gate_announces_again(db, home, sent):
    write_rl(home, rate_limits=limits(win(100, 300, int(NOW + 3600)), win(100, 10080)))
    t = cr.Tailer()
    t.rate_limits(db, NOW)
    assert len(sent) == 2 and {"5-hour" in s.body for s in sent} == {True, False}
    assert db.kv_del_prefix("codex_rl_notified_") == 2
    t.rate_limits(db, NOW + 60)
    assert len(sent) == 4


def test_a_window_below_full_or_already_reset_is_not_announced(db, home, sent):
    write_rl(home, at=NOW - 20, rate_limits=limits(win(99.9)))
    poll(db)
    assert sent == []
    for p in (Path(home) / "sessions").rglob("rollout-*.jsonl"):
        p.unlink()
    cr.reset()
    write_rl(home, at=NOW - 3600, rate_limits=limits(win(100, 10080, int(NOW - 60))))
    poll(db, NOW + 1)
    assert sent == [] and kv(db)["reached"] is False


def test_without_a_channel_nothing_is_claimed_so_a_later_channel_is_still_told(db, home, monkeypatch):
    write_rl(home, rate_limits=limits(win(100)))
    got = []
    monkeypatch.setattr(notify, "any_channel", lambda: False)
    monkeypatch.setattr(notify, "send", lambda n: (got.append(n), True)[1])
    t = cr.Tailer()
    t.rate_limits(db, NOW)
    assert got == [] and db.kv_get(f"codex_rl_notified_{RESET}") is None
    monkeypatch.setattr(notify, "any_channel", lambda: True)
    t.rate_limits(db, NOW + 60)
    assert len(got) == 1


def test_a_failing_notice_is_logged_and_the_poll_goes_on(db, home, monkeypatch, caplog):
    write_rl(home, rate_limits=limits(win(100)))
    monkeypatch.setattr(notify, "any_channel", lambda: True)
    monkeypatch.setattr(notify, "send", lambda n: 1 / 0)
    with caplog.at_level(logging.WARNING, logger="ccboard.codex_rollout"):
        poll(db)
    assert kv(db)["reached"] is True and "ZeroDivisionError" in caplog.text


def test_a_notice_for_a_non_current_account_names_it(db, home, sent):
    accounts_kv(db, current=KA)
    write_rl(home, rate_limits=limits(win(100)), account=ID_B)
    poll(db)
    assert len(sent) == 1 and "Home" in sent[0].body and kv(db) is None


def test_a_rollout_written_by_another_originator_still_reports_the_accounts_limits(db, home):
    write_rl(home, rate_limits=limits(win(26)), originator="hermes", source="vscode")
    poll(db)
    assert kv(db)["primary"]["used_percent"] == 26.0                              # the windows are the account's, whoever drove Codex


def test_the_fixture_rollout_feeds_the_poll_end_to_end(db, home):
    day = home / "sessions" / "2026" / "10" / "04"
    day.mkdir(parents=True)
    p = day / "rollout-2026-10-04T10-00-00-0199aaaa-1111-7222-8333-444455556666.jsonl"
    p.write_text(FIXTURE.read_text())
    now = 1791349200 - 86400
    os.utime(p, (now - 3600, now - 3600))
    accounts_kv(db, current=KA)
    cr.Tailer().rate_limits(db, now)
    v = kv(db)
    assert v == {"limit_id": "codex", "plan_type": "plus", "primary": {"used_percent": 18.0, "window_minutes": 10080, "resets_at": 1791349200},
                 "secondary": None, "credits": {"has_credits": False, "unlimited": False, "balance": None}, "reached": False,
                 "observed_at": "2026-10-04T10:05:30+00:00", "account": KA}, "the bengalfox zeros at the end of the file are ignored"
    assert series_last(db, "rl_7d", "cacct:" + KA)["value"] == 18.0


def test_select_prefers_the_newest_reading_per_limit_and_breaks_ties_by_time():
    a, b = cr._rate_limits(limits(win(30, 300)), NOW), cr._rate_limits(limits(win(30, 300), limit_id="codex_x"), NOW)
    sel = cr._select([(a, NOW - 100), (b, NOW - 10)], NOW)
    assert sel["value"]["limit_id"] == "codex" and sel["windows"]["5h"][1] == NOW - 10         # equal percent: the newer reading is the source


# ================================================================== wiring
def test_state_carries_the_codex_windows_as_the_kv_record(lite_client):
    from app import main
    H = {"Tailscale-User-Login": "alice@example.com"}
    assert lite_client.get("/api/state", headers=H).json()["usage_codex"] is None
    main.db.kv_set("rate_limits_codex", {"limit_id": "codex", "plan_type": "plus", "primary": {"used_percent": 17.0, "window_minutes": 10080,
                                                                                                 "resets_at": RESET}, "secondary": None,
                                         "credits": None, "reached": False, "observed_at": iso(NOW), "account": None})
    u = lite_client.get("/api/state", headers=H).json()["usage_codex"]
    assert u["value"]["primary"]["window_minutes"] == 10080 and u["value"]["plan_type"] == "plus" and u["at"]


def test_the_sampler_runs_the_tailer_after_the_codex_accounts_tick(db, home, work):
    from app import codex_accounts
    assert samples.TICK_HOOKS.index(cr.tick) > samples.TICK_HOOKS.index(codex_accounts.tick)
    p = make_rollout(home, cwd=work, lines=[turn_line(NOW), token_line(NOW - 5, info=usage(10, 10, 100), rate_limits=limits(win(17)))])
    add_codex_row(db, work, flags={"transcript_path": str(p)})

    class Clock:
        def time(self):
            return NOW
    s = samples.Sampler(db, health_fn=lambda: {}, counts_fn=dict, clock=Clock())
    s.sample_once()
    assert stats_of(db)["context_pct"] == 10.0 and kv(db)["primary"]["used_percent"] == 17.0
    assert series_last(db, "rl_7d", "codex")["value"] == 17.0


def test_the_tick_with_no_rollouts_and_a_bare_db_does_nothing(db, home):
    cr.tick(db, NOW)
    assert db.kv_get("rate_limits_codex") is None and db.open_rows() == {}


def test_no_rollout_text_reaches_the_kv_the_samples_the_rows_or_the_log(db, home, work, caplog, sent):
    accounts_kv(db, current=KA)
    p = make_rollout(home, cwd=work, account=ID_A, created=time.time() + 1,
                     lines=[turn_line(NOW), token_line(NOW - 5, info=usage(10, 10, 100), rate_limits=limits(win(100)))])
    add_codex_row(db, work)
    with caplog.at_level(logging.DEBUG):
        t = cr.Tailer()
        t.step(db, NOW)
        broken = p.parent / "rollout-broken.jsonl"
        broken.write_text(p.read_text()[:300])
        t.step(db, NOW + 61)
    with db.lock:
        dump = "".join(str(tuple(r)) for tbl in ("kv", "samples", "sessions", "events") for r in db.conn.execute(f"SELECT * FROM {tbl}"))
    assert SENTINEL not in dump and SENTINEL not in caplog.text and all(SENTINEL not in n.body + n.title for n in sent)
    assert "x" * 100 not in dump
    assert row_of(db)["agent_session_id"] == meta_id(p)


def test_nothing_in_the_module_names_the_credentials_file_or_imports_the_forbidden_modules():
    import ast
    tree = ast.parse(Path(cr.__file__).read_text())
    docstrings = {id(n.body[0].value) for n in ast.walk(tree) if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef))
                  and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]
    assert not [s for s in strings if "auth" in s.lower()], "the module opens rollouts and nothing else of the Codex home"
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {a.name for n in top for a in n.names} | {n.module for n in top if isinstance(n, ast.ImportFrom) and n.module}
    assert not names & {"db", "hooks", "main", "codex_accounts", "tasks", "scheduler", "samples", "notify", "accounts"}, names
