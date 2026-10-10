"""Issue #107 (box check, Claude Code 2.1.294, 2026-10-09): a headless run denied every tool exits 0 and says success; --bare cannot log in on a
claude.ai box; a job with no schedule and no run_now ran within seconds."""
import json

import pytest

from app import scheduler
from tests.test_scheduler import H, _join_all, _post_job, _runs_report, fake_claude, make_repo

# the shape of the denied runs on the box (exit 0, is_error false, subtype success, permission_denials populated); the inputs are scrubbed
DENIED_RUN = {"type": "result", "subtype": "success", "is_error": False, "result": "I could not create note.txt: the tools were denied.",
              "session_id": "44444444-4444-4444-8444-444444444444", "total_cost_usd": 0.02, "num_turns": 4,
              "permission_denials": [{"tool_name": "Bash", "tool_use_id": "a", "tool_input": {"command": "printf hi > note.txt"}},
                                     {"tool_name": "Write", "tool_use_id": "b", "tool_input": {"file_path": "note.txt"}},
                                     {"tool_name": "Bash", "tool_use_id": "c", "tool_input": {"command": "ls"}}]}


def test_parse_result_names_the_denied_tools_and_never_keeps_their_input():
    r = scheduler.parse_result(json.dumps(DENIED_RUN))
    assert r["denials"] == ["Bash", "Write", "Bash"] and not r["is_error"] and r["subtype"] == "success"
    assert "printf" not in json.dumps(r) and "note.txt" in r["text"]
    assert scheduler.parse_result(json.dumps({**DENIED_RUN, "permission_denials": []}))["denials"] == []
    assert scheduler.parse_result(json.dumps({k: v for k, v in DENIED_RUN.items() if k != "permission_denials"}))["denials"] == []
    assert scheduler.parse_result(json.dumps({**DENIED_RUN, "permission_denials": "oops"}))["denials"] == []
    assert scheduler.parse_result(json.dumps({**DENIED_RUN, "permission_denials": ["Edit", {"x": 1}, None]}))["denials"] == ["Edit", "?", "?"]
    assert scheduler.parse_result("garbage")["denials"] == []


def test_denied_note_counts_calls_and_lists_each_tool_once():
    assert scheduler.denied_note([]) is None and scheduler.denied_note(None) is None
    assert scheduler.denied_note(["Bash"]) == "denied 1 tool call: Bash"
    assert scheduler.denied_note(["Bash", "Write", "Bash"]) == "denied 3 tool calls: Bash, Write"
    assert scheduler.denied_note([f"T{i}" for i in range(8)]).endswith("T5 ...")


# the live case (box, 2026-10-10): Haiku probed a claude-mem MCP tool first, it was denied, then it answered "ok"
PROBE_THEN_ANSWER = {"type": "result", "subtype": "success", "is_error": False, "result": "ok",
                     "session_id": "55555555-5555-4555-8555-555555555555", "total_cost_usd": 0.006, "num_turns": 2,
                     "permission_denials": [{"tool_name": "mcp__plugin_claude-mem_mcp-search__work_state_read", "tool_use_id": "p", "tool_input": {}}]}


def test_headless_outcome_one_denied_probe_then_a_good_answer_is_ok_with_a_warning():
    res = scheduler.parse_result(json.dumps(PROBE_THEN_ANSWER))
    status, note = scheduler.headless_outcome("ok", res)
    assert status == "ok" and note == "denied 1 tool call: mcp__plugin_claude-mem_mcp-search__work_state_read"


def test_headless_outcome_denied_only_when_there_is_no_usable_result():
    assert scheduler.headless_outcome("ok", scheduler.parse_result(json.dumps(DENIED_RUN))) == ("denied", "denied 3 tool calls: Bash, Write")
    assert scheduler.headless_outcome("ok", scheduler.parse_result(json.dumps({**PROBE_THEN_ANSWER, "result": ""})))[0] == "denied"
    assert scheduler.headless_outcome("ok", scheduler.parse_result(json.dumps({**PROBE_THEN_ANSWER, "result": "   "})))[0] == "denied"
    for said in ("I couldn't commit: the Bash tool was refused.", "Unable to write note.txt.", "Permission was denied for the commit.",
                 "I cannot proceed without that tool."):
        assert scheduler.headless_outcome("ok", scheduler.parse_result(json.dumps({**PROBE_THEN_ANSWER, "result": said})))[0] == "denied", said
    # no denials: nothing to judge, and other statuses pass through untouched
    assert scheduler.headless_outcome("ok", scheduler.parse_result(json.dumps({**PROBE_THEN_ANSWER, "permission_denials": []}))) == ("ok", None)
    assert scheduler.headless_outcome("error", scheduler.parse_result(json.dumps(DENIED_RUN))) == ("error", None)
    assert scheduler.headless_outcome("rate_limited", {"denials": ["Bash"], "text": ""}) == ("rate_limited", None)


def test_a_probe_then_answer_run_is_ok_with_the_warning_on_the_run_line(lite_client, projects_dir, fake_tmux, tmp_path, monkeypatch):
    from app import main
    make_repo(projects_dir)
    claude = fake_claude(tmp_path)
    claude.write_text(claude.read_text().replace("  *denyme*)", "  *probeok*) echo '" + json.dumps(PROBE_THEN_ANSWER) + "';;\n  *denyme*)", 1))
    monkeypatch.setattr(main.settings, "claude_bin", lambda: str(claude))
    monkeypatch.setattr(main, "sched", None)
    r = _post_job(lite_client, cron=None, run_now=True, prompt="reply with probeok")
    assert r.status_code == 201
    w = scheduler.Worker(main.db)
    rids = w.tick()
    _join_all(w)
    run = main.db.run_get(rids[0])
    assert run["status"] == "ok", _runs_report(main.db, rids)
    assert run["error"] == "denied 1 tool call: mcp__plugin_claude-mem_mcp-search__work_state_read" and run["result"] == "ok"
    assert main.db.job_get(r.json()["id"])["last_status"] == "ok"
    card = main.db.task_get(run["task_id"])
    assert card["phase"] == "done" and "denied" not in card["title"]


def test_a_run_whose_tools_were_denied_is_denied_not_ok(lite_client, projects_dir, fake_tmux, tmp_path, monkeypatch):
    from app import main
    make_repo(projects_dir)
    monkeypatch.setattr(main.settings, "claude_bin", lambda: str(fake_claude(tmp_path)))
    monkeypatch.setattr(main, "sched", None)
    r = _post_job(lite_client, cron=None, run_now=True, prompt="please denyme")
    assert r.status_code == 201
    w = scheduler.Worker(main.db)
    rids = w.tick()
    _join_all(w)
    run = main.db.run_get(rids[0])
    assert run["status"] == "denied", _runs_report(main.db, rids)
    assert run["error"] == "denied 3 tool calls: Bash, Write" and run["num_turns"] == 4 and "denied" in run["result"]
    assert main.db.job_get(r.json()["id"])["last_status"] == "denied"
    assert "(denied 3 tool calls: Bash, Write)" in main.db.task_get(run["task_id"])["title"], "the card says so too"
    st = main.build_state("alice@example.com")
    assert [x["status"] for x in st["runs"] if x["id"] == rids[0]] == ["denied"]
    # a run with no denials stays ok, with no note on the card
    ok = _post_job(lite_client, cron=None, run_now=True, name="fine")
    rids2 = w.tick()
    _join_all(w)
    run2 = main.db.run_get(rids2[0])
    assert ok.status_code == 201 and run2["status"] == "ok" and run2["error"] is None
    assert "denied" not in main.db.task_get(run2["task_id"])["title"]


def test_bare_is_refused_for_unattended_runs_unless_an_api_key_is_set(lite_client, projects_dir, fake_tmux, tmp_path, monkeypatch):
    from app import main
    make_repo(projects_dir)
    monkeypatch.setattr(main.settings, "claude_bin", lambda: str(fake_claude(tmp_path)))
    monkeypatch.setattr(main, "sched", None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for bad in ("--bare", "--model haiku --BARE"):
        r = _post_job(lite_client, args=bad)
        assert r.status_code == 400 and "Not logged in" in r.text and "ANTHROPIC_API_KEY" in r.text, r.text
    assert main.db.jobs() == [], "nothing was saved"
    batch = lite_client.post("/api/batch", headers=H, json={"prompt": "p", "repos": ["shop/api"], "args": "--bare"})
    assert batch.status_code == 400 and "--bare" in batch.text
    with pytest.raises(ValueError, match="Not logged in"):
        scheduler.check_extra_args("--bare")
    assert scheduler.check_extra_args("--barefoot") == ["--barefoot"], "only the flag itself, not a longer word"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  ")
    assert _post_job(lite_client, args="--bare").status_code == 400, "a blank key is no key"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    assert _post_job(lite_client, args="--bare").status_code == 201


def test_a_job_with_no_schedule_and_no_run_now_is_parked_until_asked(lite_client, projects_dir, fake_tmux, tmp_path, monkeypatch):
    from app import main
    make_repo(projects_dir)
    monkeypatch.setattr(main.settings, "claude_bin", lambda: str(fake_claude(tmp_path)))
    monkeypatch.setattr(main, "sched", None)
    r = _post_job(lite_client, cron=None)
    assert r.status_code == 201 and r.json()["next_run_at"] is None
    jid = r.json()["id"]
    assert main.db.job_get(jid)["next_run_at"] is None and main.db.job_get(jid)["enabled"] == 1
    w = scheduler.Worker(main.db)
    assert w.tick() == [] and w.tick() == [], "it did not run on its own"
    shown = next(j for j in main.build_state("alice@example.com")["jobs"] if j["id"] == jid)
    assert shown["enabled"] == 1 and shown["next_run_at"] is None
    # "Run now" is the ask (main.sched is off here, so the worker above ticks)
    lite_client.post(f"/api/jobs/{jid}/run", headers=H)
    rids = w.tick()
    _join_all(w)
    assert main.db.run_get(rids[0])["status"] == "ok", _runs_report(main.db, rids)
    assert main.db.job_get(jid)["enabled"] == 0 and main.db.job_get(jid)["last_run_at"]
    # run_now true runs at once, a cron waits for its fire
    assert _post_job(lite_client, cron=None, run_now=True).json()["next_run_at"] <= main.db_now()
    assert _post_job(lite_client, cron="0 3 * * *").json()["next_run_at"] > main.db_now()
    # Enable is an explicit ask too: a one-off enabled again runs again now, as before
    assert lite_client.post(f"/api/jobs/{jid}/toggle", headers=H).json()["enabled"] == 1
    assert main.db.job_get(jid)["next_run_at"] is not None
