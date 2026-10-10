"""The Codex defects the 2026-10-09 box checks found (codex-cli 0.161.0): a foreign Codex in a board row's folder, the empty "outside
threads" list, the launch dialogs and a dead Codex under a typing board, and the headless run's usage / task phase / reused job ids.

Hook payloads are the scrubbed ones from the box (tests/fixtures/codex_0161/hook_*.json, placeholders filled in here); the 0.161 threads
table is tests/fixtures/codex_0161/threads.sql. Nothing here touches a real ~/.codex, tmux or binary."""
import json
import os
import sqlite3
from pathlib import Path

import pytest

from app import hooks, main
from app.agents import codex_discovery as cd
from app.config import settings

from .test_external import DAY, NOW, git_init as ext_git_init, rollout  # noqa: F401
from .test_hooks import CXA, CXB, H, _hook, _snapshot, _untouched, cx_hook, cxs, event_names, row_of  # noqa: F401  (cxs is a fixture)

FIX = Path(__file__).parent / "fixtures" / "codex_0161"
FOREIGN_THREAD = "00000000-0000-4000-8000-000000000086"          # the id in the box fixtures


def box_payload(event: str, cwd: str, **over) -> dict:
    p = json.loads((FIX / f"hook_{event}.json").read_text())
    p.update(cwd=cwd, **over)
    return p


def foreign_tmux_headers() -> dict:
    """What bin/ccboard-hook sends for a Codex started by a person under another tmux server: no CCBOARD_SESSION, a pane id and a
    $TMUX of its own socket."""
    return {"X-CCBoard-Agent": "codex", "X-CCBoard-Pane": "%7", "X-CCBoard-Tmux": "/tmp/tmux-1000/ccbcheck,4242,0"}


# ---------------------------------------------------------------- 1. a foreign Codex in the same folder

def test_a_foreign_codex_in_the_rows_folder_never_takes_the_row_over(cxs, projects_dir):
    """Box check 63, bug 1: SessionStart / UserPromptSubmit / Stop / SessionEnd of a Codex started in another tmux server, same cwd as an
    idle board row, were stored under the row, its thread id and last prompt replaced and the row marked ended."""
    cwd = str(projects_dir / "shop" / "api")
    cx_hook(cxs, "SessionStart", source="startup", cwd=cwd)
    cx_hook(cxs, "UserPromptSubmit", prompt="the board's own prompt", cwd=cwd)
    assert cx_hook(cxs, "Stop", last_assistant_message="board done", cwd=cwd)["state"] == "done"
    before = _snapshot(cxs.name)
    assert before["row"]["claude_session_id"] == CXA and before["row"]["state"] == "done"
    for event, extra in (("SessionStart", {}), ("UserPromptSubmit", {"prompt": "-x test"}),
                         ("Stop", {"last_assistant_message": "the foreign reply"}), ("SessionEnd", {"reason": "other"})):
        payload = box_payload("SessionStart" if event == "SessionEnd" else event, cwd, session_id=FOREIGN_THREAD,
                              hook_event_name=event, **extra)
        out = _hook(cxs.client, payload, extra=foreign_tmux_headers()).json()          # no session header, a foreign pane and socket
        assert out["ignored"] == "foreign_tmux" and out["session"] is None, (event, out)
        _untouched(cxs.name, before)
    assert row_of(cxs.name)["state"] == "done" and row_of(cxs.name)["last_prompt"] == "the board's own prompt"
    # the same four events from a Codex with no tmux at all (a plain shell, ssh): the folder is all that ties it to the row, and the row
    # already follows another thread
    for event in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"):
        payload = box_payload("SessionStart", cwd, session_id=FOREIGN_THREAD, hook_event_name=event)
        out = _hook(cxs.client, payload, extra={"X-CCBoard-Agent": "codex"}).json()
        assert out["ignored"] == "other_thread" and out["session"] is None, (event, out)
        _untouched(cxs.name, before)


def test_the_rows_own_codex_is_still_found_without_its_env(cxs, projects_dir):
    """What the foreign-hook guards must not break: the row's own thread resolves by its id, and a row with no thread yet is bound by
    the cwd (a Codex that did not inherit the tmux environment)."""
    cwd = str(projects_dir / "shop" / "api")
    out = _hook(cxs.client, box_payload("SessionStart", cwd, session_id=CXA), extra={"X-CCBoard-Agent": "codex"}).json()
    assert out["session"] == cxs.name and out["how"] == "cwd" and row_of(cxs.name)["claude_session_id"] == CXA
    out = _hook(cxs.client, box_payload("UserPromptSubmit", cwd, session_id=CXA, prompt="mine"), extra={"X-CCBoard-Agent": "codex"}).json()
    assert out["session"] == cxs.name and out["how"] == "session_id" and out["state"] == "working"
    # a pane of the board's own socket that tmux cannot name any more still falls back to the cwd for an unbound row
    ours = {"X-CCBoard-Agent": "codex", "X-CCBoard-Pane": "%9", "X-CCBoard-Tmux": f"/tmp/tmux-{os.getuid()}/{settings.tmux_socket},1,0"}
    assert hooks._our_socket(ours["X-CCBoard-Tmux"])
    out = _hook(cxs.client, box_payload("Stop", cwd, session_id=CXA, last_assistant_message="ok"), extra=ours).json()
    assert out["session"] == cxs.name and out["state"] == "done"


def test_resolve_session_refuses_the_cwd_for_another_tmux_server_and_another_thread(cxs, projects_dir):
    cwd = str(projects_dir / "shop" / "api")
    rows = main.db.open_rows()
    payload = box_payload("SessionStart", cwd, session_id=FOREIGN_THREAD)
    foreign = {k.lower(): v for k, v in foreign_tmux_headers().items()}
    assert hooks.resolve_session(foreign, payload, rows) == (None, "foreign_tmux")
    # no tmux header at all and no bound thread: the cwd binds the row (the old behaviour for an env-less Codex)
    assert hooks.resolve_session({}, payload, rows) == (cxs.name, "cwd")
    main.db.set_state(cxs.name, "done", "Stop", claude_session_id=CXA)
    rows = main.db.open_rows()
    assert hooks.resolve_session({}, payload, rows) == (None, "other_thread")
    assert hooks.resolve_session({}, {**payload, "session_id": CXA}, rows) == (cxs.name, "session_id")
    assert hooks.resolve_session({}, {**payload, "session_id": None}, rows) == (cxs.name, "cwd")      # no id says nothing against the row


def test_a_foreign_permission_request_does_not_wait_for_the_row_either(cxs, projects_dir):
    cwd = str(projects_dir / "shop" / "api")
    cx_hook(cxs, "SessionStart", source="startup", cwd=cwd)
    payload = box_payload("SessionStart", cwd, session_id=FOREIGN_THREAD, hook_event_name="PermissionRequest",
                          tool_name="Bash", tool_input={"command": "echo x"})
    hdr = {"X-CCBoard-Token": hooks.ensure_token(), **foreign_tmux_headers()}
    r = cxs.client.post("/api/permission", headers=hdr, content=json.dumps(payload))
    assert r.status_code == 200 and r.json() == {"behavior": None, "reason": "unknown session"}
    assert main.db.perm_pending() == []


# ---------------------------------------------------------------- 2. outside threads in Codex 0.161

def _db_0161(home: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(home / "state_5.sqlite")
    conn.executescript((FIX / "threads.sql").read_text())
    return conn


def _thread(conn, tid, **kw):
    row = {"id": tid, "rollout_path": None, "created_at": int(NOW - 2 * DAY), "updated_at": int(NOW - 600), "source": "cli",
           "model_provider": "openai", "cwd": "/srv/projects/shop/api", "title": f"thread {tid[-2:]}", "tokens_used": 1000,
           "has_user_event": 0, "archived": 0, "model": "gpt-6.1-sol", "reasoning_effort": "medium", "first_user_message": "hello",
           "originator": "codex-tui"}
    row.update(kw)
    conn.execute(f"INSERT INTO threads({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))


T1, T2, T3, T4, T5, T6 = (f"019f000{i}-aaaa-7aaa-8aaa-00000000000{i}" for i in range(1, 7))


def test_codex_0161_threads_with_has_user_event_zero_are_listed(codex_home, projects_dir, monkeypatch):
    """Box check 63, bug 2: every thread of a 0.161 state_5.sqlite has has_user_event = 0, so the Agents page listed none."""
    monkeypatch.setattr(cd, "_wall", lambda: NOW)
    cd.reset_caches()
    shop = ext_git_init(projects_dir / "shop" / "api")
    conn = _db_0161(codex_home)
    _thread(conn, T1, cwd=str(shop), title="foreign tui", updated_at=int(NOW - 60))
    _thread(conn, T2, originator="hermes", source="vscode", cwd="/home/user/brain", updated_at=int(NOW - 120))
    _thread(conn, T3, originator="codex_exec", source="exec", first_user_message="say ok", tokens_used=0, updated_at=int(NOW - 180))
    _thread(conn, T4, first_user_message="", tokens_used=0, title="opened, never used", updated_at=int(NOW - 240))     # no user turn at all
    _thread(conn, T5, has_user_event=1, first_user_message="", title="older flag still works", updated_at=int(NOW - 300))
    _thread(conn, T6, archived=1, updated_at=int(NOW - 360))
    conn.commit()
    conn.close()
    items = cd.discover(codex_home, now=NOW, projects_dir=projects_dir)
    got = {e["id"]: e for e in items}
    assert [e["id"] for e in items] == [T1, T2, T3, T5], "newest first; the thread with no user turn and the archived one are not listed"
    assert got[T1]["badge"] is None and got[T1]["originator"] == "codex-tui" and (got[T1]["project"], got[T1]["repo"]) == ("shop", "api")
    assert got[T2]["badge"] == "hermes" and got[T3]["badge"] == "codex_exec", "the originator column badges threads whose rollout is not read"
    assert got[T1]["tokens"] == 1000 and got[T1]["model"] == "gpt-6.1-sol"


def test_the_originator_column_wins_over_the_rollout_and_older_schemas_still_work(codex_home, projects_dir, monkeypatch):
    monkeypatch.setattr(cd, "_wall", lambda: NOW)
    cd.reset_caches()
    conn = _db_0161(codex_home)
    r = rollout(codex_home, T1, at=NOW - 3600, originator="codex-tui", cwd="/srv/projects/shop/api")
    _thread(conn, T1, rollout_path=str(r), originator="hermes")
    conn.commit()
    conn.close()
    e = cd.discover(codex_home, now=NOW, projects_dir=projects_dir)[0]
    assert e["originator"] == "hermes" and e["badge"] == "hermes"
    # a pre-0.161 table (has_user_event set by Codex, no first_user_message, no originator): the flag alone decides, as before
    (codex_home / "state_5.sqlite").unlink()
    cd.reset_caches()
    old = sqlite3.connect(codex_home / "state_5.sqlite")
    old.executescript("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, updated_at INTEGER, title TEXT, rollout_path TEXT, "
                      "tokens_used INTEGER, has_user_event INTEGER);")
    old.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?)", (T1, "/srv/projects/shop/api", int(NOW - 60), "used", None, 50, 1))
    old.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?)", (T2, "/srv/projects/shop/api", int(NOW - 70), "empty", None, 50, 0))
    old.commit()
    old.close()
    assert [e["id"] for e in cd.discover(codex_home, now=NOW, projects_dir=projects_dir)] == [T1]


# ---------------------------------------------------------------- 4. headless runs: usage, task phase, reused job ids

from app import scheduler  # noqa: E402
from .test_scheduler import SID, codex_box, post_job, run_all  # noqa: E402,F401  (codex_box is a fixture)

USAGE = {"input_tokens": 1200, "cached_input_tokens": 800, "output_tokens": 45}          # tests/fixtures/codex_exec/success.jsonl


def _run_codex_job(client, main, **body):
    j = post_job(client, agent="codex", permission_mode="plan", **body)
    assert j.status_code == 201, j.text
    w = scheduler.Worker(main.db)
    rid = w.tick()[0]
    run_all(w)
    return j.json()["id"], rid


def test_a_codex_runs_usage_is_stored_and_its_task_card_ends_done(client, codex_box):
    """Box check 70: the usage parsed from turn.completed was dropped by run_finish, and the run's card stayed `running` for ever."""
    main, repo, _ = codex_box
    jid, rid = _run_codex_job(client, main)
    run = main.db.run_get(rid)
    assert run["status"] == "ok" and run["usage"] == USAGE and run["cost_usd"] is None
    task = main.db.task_get(run["task_id"])
    assert task["phase"] == "done" and task["result"] == "Final answer from the -o file." and task["done_at"] and task["result_at"]
    assert main.db.tasks_by_phase(("running",)) == [], "nothing counts a finished run as a running task"
    st = client.get("/api/state", headers=H).json()
    assert st["runs"][0]["usage"] == USAGE and client.get(f"/api/runs/{rid}", headers=H).json()["usage"] == USAGE
    with main.db.lock:
        raw = main.db.conn.execute("SELECT usage FROM runs WHERE id=?", (rid,)).fetchone()[0]
    assert json.loads(raw) == USAGE


def test_a_failed_codex_run_ends_its_card_failed_and_a_bad_usage_is_stored_as_none(client, codex_box):
    main, repo, _ = codex_box
    jid, rid = _run_codex_job(client, main, prompt="failrun please")
    run = main.db.run_get(rid)
    assert run["status"] == "error" and run["usage"] is None
    task = main.db.task_get(run["task_id"])
    assert task["phase"] == "failed" and task["done_at"]
    main.db.run_finish(rid, status="ok", usage="not a dict")              # whatever is not a usage dict is stored as none
    assert main.db.run_get(rid)["usage"] is None


def test_deleting_a_job_deletes_its_runs_so_a_reused_id_starts_clean(client, codex_box):
    """Box check 70: DELETE /api/jobs left the runs row, and the next job took the id and showed the old run as its own."""
    main, repo, _ = codex_box
    jid, rid = _run_codex_job(client, main)
    out_file = Path(main.settings.data_dir) / "runs" / f"{rid}.txt"
    assert out_file.is_file() and main.db.run_get(rid)
    other = main.db.run_start(jid)                                        # a second run, still marked running, goes too
    assert client.delete(f"/api/jobs/{jid}", headers=H).json() == {"deleted": jid}
    assert main.db.run_get(rid) is None and main.db.run_get(other) is None and not out_file.exists()
    again = post_job(client, agent="codex", name="second")
    assert again.json()["id"] == jid, "ids are reused (the table has no AUTOINCREMENT), which is why the runs must not outlive the job"
    assert main.db.runs(limit=10, job_id=jid) == []
    assert [r for r in client.get("/api/state", headers=H).json()["runs"] if r["job_id"] == jid] == []
    assert client.delete("/api/jobs/9999", headers=H).status_code == 404


# ---------------------------------------------------------------- 3. launch dialogs and a Codex that left its pane

# The three pane fixtures are reconstructed from what boxchecks 86 and 92 quote (the dialogs' wording and the error line), not captured
# byte for byte: the live_check in the hand-over has the orchestrator confirm the real screens match.
from app.agents import codex_pane  # noqa: E402

CXNAME = "shop--api--cx1"
OLD = "2020-01-01T00:00:00+00:00"


def pane(name: str) -> str:
    return (FIX / f"pane_{name}.txt").read_text()


def test_the_pane_rules_on_the_reconstructed_screens():
    assert codex_pane.dialog(pane("update_dialog")) == "update"
    assert codex_pane.dialog(pane("trust_dialog")) == "trust"
    assert codex_pane.dialog(pane("bootstrap_error")) is None and codex_pane.dialog("") is None and codex_pane.dialog(None) is None
    both = pane("trust_dialog") + pane("update_dialog")
    assert codex_pane.dialog(both) == "update", "the update dialog is the dangerous one"
    # a dialog that was answered: Codex drew its composer footer after it
    answered = pane("update_dialog") + "\n› Ask Codex to do anything\n  97% context left · ? for shortcuts\n"
    assert codex_pane.dialog(answered) is None
    # old scrollback above the last 40 lines does not count
    assert codex_pane.dialog(pane("update_dialog") + "\n".join(f"line {i}" for i in range(60))) is None
    assert codex_pane.exit_reason(pane("bootstrap_error")).startswith("codex exited: Error: account/read failed")
    assert "login shell" in codex_pane.exit_reason("$ codex\n$ ")
    assert len(codex_pane.exit_reason("Error: " + "x" * 900)) <= 300
    assert [codex_pane.shell_foreground(c) for c in ("zsh", "-zsh", "bash", "sh", "node", "codex", "2.1.294", "", None)] == \
        [True, True, True, True, False, False, False, False, False]


def test_an_answered_trust_dialog_is_not_the_current_screen_even_when_it_is_still_in_the_last_lines():
    """Live check (issues 2 and 86, codex-cli 0.161): the dialog stayed in the last 40 lines and the narrow-pane footer
    "GPT-6.1-Sol default \u00b7 <path> \u00b7 Reply wit\u2026" has none of the old footer words, so prompt and restart answered 409 dialog for several turns."""
    assert codex_pane.dialog(pane("trust_dialog")) == "trust"
    assert codex_pane.dialog(pane("trust_answered_footer")) is None, "only the 0.161 footer line after the dialog"
    assert codex_pane.dialog(pane("trust_answered_working")) is None, "header box, prompt, reply and footer after the dialog"
    assert len(pane("trust_answered_working").splitlines()) < codex_pane.TAIL_LINES, "the dialog is inside the tail: no scrolling out"
    # the dialog's own numbered options and the selector arrow are not Codex's running screen
    selected = pane("trust_dialog").replace("  1. Yes, continue", "\u203a 1. Yes, continue")
    assert codex_pane.dialog(selected) == "trust"
    # a NEW dialog drawn after a working session counts again
    assert codex_pane.dialog(pane("trust_answered_working") + "\n" + pane("update_dialog")) == "update"


@pytest.fixture
def cxrow(lite_client, fake_tmux):
    """A Codex row in tmux session CXNAME, idle (it has taken a turn), its pane running codex with an empty screen."""
    main.db.add_session(tmux_name=CXNAME, project="shop", repo="api", name="cx1", launcher="codex", agent="codex")
    fake_tmux["sessions"][CXNAME] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "codex",
                                     "path": "/x", "pid": 1, "env": {}}
    fake_tmux["screen"] = ""
    main.db.set_state(CXNAME, "idle", "SessionStart", claude_session_id=CXA)
    return CXNAME


def post_prompt(client, name, text="do it"):
    return client.post(f"/api/sessions/{name}/prompt", headers=H, json={"text": text})


def nothing_typed(fake_tmux):
    return not (fake_tmux["keys"] or fake_tmux["texts"] or fake_tmux["pasted"])


@pytest.mark.parametrize("which, kind", [("update_dialog", "update"), ("trust_dialog", "trust")])
def test_the_board_never_types_into_a_codex_dialog(lite_client, cxrow, fake_tmux, which, kind):
    fake_tmux["screen"] = pane(which)
    before = main.db.open_row(cxrow)
    r = post_prompt(lite_client, cxrow)
    assert r.status_code == 409 and r.json()["error"] == "dialog" and r.json()["retry"] is None, r.text
    assert ("Skip" in r.json()["message"]) == (kind == "update") and nothing_typed(fake_tmux)
    r = lite_client.post(f"/api/sessions/{cxrow}/command", headers=H, json={"cmd": "/status"})
    assert r.status_code == 409 and r.json()["error"] == "dialog" and nothing_typed(fake_tmux)
    after = main.db.open_row(cxrow)
    assert (after["state"], after["last_message"], after["claude_session_id"]) == (before["state"], before["last_message"], CXA)
    fake_tmux["screen"] = ""                                       # answered by a person in the terminal: typing works again
    assert post_prompt(lite_client, cxrow).status_code == 200 and fake_tmux["pasted"] == [(cxrow, "do it", True)]


def test_the_board_never_types_a_prompt_into_the_login_shell_and_marks_the_row_errored(lite_client, cxrow, fake_tmux):
    """Box check 86: codex exited at launch ("account/read failed ... timed out") and the board typed the next prompt into zsh."""
    fake_tmux["sessions"][cxrow]["command"] = "zsh"
    fake_tmux["screen"] = pane("bootstrap_error")
    r = post_prompt(lite_client, cxrow, "Reply with the word ok")
    assert r.status_code == 409 and r.json()["error"] == "agent_exited" and "account/read failed" in r.json()["message"], r.text
    assert nothing_typed(fake_tmux)
    row = main.db.open_row(cxrow)
    assert row["state"] == "errored" and row["last_event"] == "AgentExited" and "workspace routing discovery timed out" in row["last_message"]
    assert "AgentExited" in event_names(cxrow)
    n = len(event_names(cxrow))
    assert post_prompt(lite_client, cxrow).status_code == 409 and len(event_names(cxrow)) == n, "the reason is stored once"
    r = lite_client.post(f"/api/sessions/{cxrow}/command", headers=H, json={"cmd": "/status"})
    assert r.status_code == 409 and r.json()["error"] == "agent_exited" and nothing_typed(fake_tmux)


def test_a_claude_row_and_a_codex_row_that_runs_are_untouched_by_the_pane_rules(lite_client, cxrow, fake_tmux):
    fake_tmux["screen"] = pane("update_dialog")                    # Claude has no such dialog; the rules are Codex's
    main.db.add_session(tmux_name="shop--api--cl1", project="shop", repo="api", name="cl1", launcher="claude")
    fake_tmux["sessions"]["shop--api--cl1"] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%2", "command": "zsh",
                                               "path": "/x", "pid": 2, "env": {}}
    main.db.set_state("shop--api--cl1", "idle", "SessionStart")
    assert post_prompt(lite_client, "shop--api--cl1").status_code == 200
    fake_tmux["screen"] = ""
    assert post_prompt(lite_client, cxrow).status_code == 200


def test_a_task_is_not_dispatched_into_a_codex_session_showing_a_dialog(lite_client, cxrow, fake_tmux, projects_dir):
    git_init_repo = ext_git_init(projects_dir / "shop" / "api")                    # noqa: F841
    t = lite_client.post("/api/tasks", headers=H, json={"project": "shop", "repo": "api", "title": "t", "prompt": "do the thing",
                                                        "agent": "codex", "when": "later"})
    assert t.status_code in (200, 201), t.text
    tid = t.json().get("id") or t.json()["task"]["id"]
    fake_tmux["screen"] = pane("update_dialog")
    r = lite_client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"mode": "session", "session": cxrow})
    assert r.status_code == 409 and r.json()["pane"] == "dialog" and nothing_typed(fake_tmux), r.text
    fake_tmux["sessions"][cxrow]["command"] = "bash"
    fake_tmux["screen"] = pane("bootstrap_error")
    r = lite_client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"mode": "session", "session": cxrow})
    assert r.status_code == 409 and r.json()["pane"] == "agent_exited" and nothing_typed(fake_tmux), r.text
    assert main.db.task_get(tid)["phase"] == "backlog"


def _session_view(client, name):
    main._invalidate_scan()
    st = client.get("/api/state", headers=H).json()
    return next(s for p in st["projects"] for r in p["repos"] for s in r["sessions"] if s["tmux"] == name)


def test_a_fresh_codex_row_behind_a_dialog_shows_as_needing_a_person_and_a_dead_launch_as_errored(lite_client, projects_dir, fake_tmux, fake_codex):
    """Codex sends no hook before its first turn, so the row has no state; the state builder reads the pane for such a row only."""
    ext_git_init(projects_dir / "shop" / "api")
    name = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "codex"}).json()["tmux"]
    fake_tmux["screen"] = pane("update_dialog")
    s = _session_view(lite_client, name)
    assert s["state"] == "unknown" and "pane_dialog" not in s and not s["needs_attention"], "still starting: the launch line is being typed"
    with main.db.lock:
        main.db.conn.execute("UPDATE sessions SET created_at=? WHERE tmux_name=?", (OLD, name))
    s = _session_view(lite_client, name)
    assert s["pane_dialog"].startswith("Codex is asking whether to update") and s["needs_attention"] and s["last_message"] == s["pane_dialog"]
    assert main.db.open_row(name)["state"] is None, "a view only: nothing is stored, nothing was typed"
    assert nothing_typed(fake_tmux)
    fake_tmux["screen"] = ""
    assert "pane_dialog" not in _session_view(lite_client, name)
    fake_tmux["sessions"][name]["command"] = "zsh"
    fake_tmux["screen"] = pane("bootstrap_error")
    s = _session_view(lite_client, name)
    assert s["state"] == "errored" and s["needs_attention"] and "account/read failed" in s["last_message"]
    assert main.db.open_row(name)["state"] == "errored" and event_names(name).count("AgentExited") == 1
    _session_view(lite_client, name)
    assert event_names(name).count("AgentExited") == 1


def test_a_fresh_codex_behind_a_dialog_answers_dialog_not_not_ready(lite_client, projects_dir, fake_tmux, fake_codex):
    """Live check (issue 86): a fresh launch has no state yet, so _state_refusal answered not_ready and the dialog never showed."""
    ext_git_init(projects_dir / "shop" / "api")
    name = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "codex"}).json()["tmux"]
    assert main.db.open_row(name)["state"] is None
    fake_tmux["screen"] = ""
    r = post_prompt(lite_client, name)
    assert r.status_code == 409 and r.json()["error"] == "not_ready", "no dialog on screen: the plain reason"
    for which, word in (("update_dialog", "Skip"), ("trust_dialog", "trust this folder")):
        fake_tmux["screen"] = pane(which)
        r = post_prompt(lite_client, name)
        assert r.status_code == 409 and r.json()["error"] == "dialog" and word in r.json()["message"], r.text
        assert nothing_typed(fake_tmux)
    fake_tmux["screen"] = pane("trust_answered_footer")        # answered, no hook yet: back to the plain reason
    assert post_prompt(lite_client, name).json()["error"] == "not_ready"
    fake_tmux["sessions"][name]["command"] = "zsh"             # a login shell in a young pane is the launch line: not agent_exited here
    fake_tmux["screen"] = ""
    assert post_prompt(lite_client, name).json()["error"] == "not_ready"


def test_an_answered_dialog_no_longer_blocks_prompt_and_restart_of_a_working_session(lite_client, cxrow, fake_tmux):
    """The same live finding through the routes: the screen still holds the answered trust dialog above the working composer."""
    fake_tmux["screen"] = pane("trust_answered_working")
    r = post_prompt(lite_client, cxrow)
    assert r.status_code == 200, r.text
    fake_tmux["screen"] = pane("trust_answered_footer")
    assert post_prompt(lite_client, cxrow).status_code == 200
