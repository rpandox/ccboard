"""POST /api/sessions/{name}/restart (issue #2): a Codex session's reasoning, approvals and sandbox change by restarting it with the same
conversation resumed (`codex resume <thread> <flags>`), because no live Codex command takes them (box check V8-Codex) and the picker key
paths for them were never recorded. The command is built by the adapter, previewed before anything changes, and never carries a value the
Tune does not offer: untrusted and on-failure (retired / deprecated: codex --help on 0.160 and 0.161), danger-full-access and a bypass.

lite_client + fake_tmux + a fake codex whose --help is tests/fixtures/codex_help_0160.txt (--no-daemon, --approve-for-me). No real codex runs.
"""
import shlex

import pytest

from app import main, tmux
from app.agents import codex
from app.config import settings
from tests.conftest import write_fake_codex

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
NAME = "shop--api--s1"
SID = "11111111-1111-4111-8111-111111111111"
LINE = "codex --no-daemon --no-alt-screen -s workspace-write -a on-request"


@pytest.fixture
def cx(lite_client, fake_tmux, fake_codex, projects_dir, monkeypatch):
    monkeypatch.setattr(codex.CodexAgent, "_warm_models", lambda self, exe: None)
    write_fake_codex(fake_codex, help_name="codex_help_0160.txt")
    codex.reset_caches()
    (projects_dir / "shop" / "api").mkdir(parents=True)
    lite_client.tmux = fake_tmux
    lite_client.pauses = pauses = []
    monkeypatch.setattr(main, "_restart_pause", lambda s: pauses.append(s))
    return lite_client


def start(c, *, opts=None, cmd=LINE, sid=SID, state="idle", stats=None, launcher="claude", agent="codex", flags=None):
    opts = {"permission_mode": "default"} if opts is None else opts
    main.db.add_session(tmux_name=NAME, project="shop", repo="api", name="s1", launcher=launcher, cmd=cmd, claude_session_id=sid, agent=agent,
                        cwd=str(settings.projects_dir / "shop" / "api"), opts=opts, flags=flags)
    c.tmux["sessions"][NAME] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": agent, "path": "/x", "pid": 1, "env": {}}
    main.db.set_state(NAME, state, "SessionStart")
    main.db.set_stats(NAME, stats if stats is not None else {"model": "gpt-6-sol", "effort": "high", "approval": "on-request", "sandbox": "workspace-write"})


def restart(c, **body):
    return c.post(f"/api/sessions/{NAME}/restart", headers=H, json=body)


def untouched(c):
    return c.tmux["created"] == [] and c.tmux["sent"] == [] and NAME in c.tmux["sessions"] and main.db.open_row(NAME)["claude_session_id"] == SID


# ================================================================ the preview

def test_preview_shows_the_adapters_command_and_touches_nothing(cx):
    start(cx)
    r = restart(cx, approval="never", preview=True)
    assert r.status_code == 200, r.text
    b = r.json()
    assert shlex.split(b["cmd"]) == ["codex", "resume", "--no-daemon", "-c", "check_for_update_on_startup=false", "--no-alt-screen", "-s", "workspace-write", "-a", "never",
                                      "-m", "gpt-6-sol", "-c", 'model_reasoning_effort="high"', SID]
    assert b["preview"] is True and b["changes"] == {"approval": "never"}
    assert b["from"] == {"approval": "on-request", "sandbox": "workspace-write"} and b["to"] == {"approval": "never", "sandbox": "workspace-write"}
    assert untouched(cx)


def test_the_model_and_reasoning_the_statusline_shows_survive_a_restart(cx):
    """A model picked live with /model (the stored launch options still name the old one) is not reverted by an approvals restart."""
    start(cx, opts={"permission_mode": "default", "model": "gpt-6-astra", "reasoning_effort": "low"}, stats={"model": "gpt-6-luna", "effort": "xhigh"})
    argv = shlex.split(restart(cx, sandbox="read-only", preview=True).json()["cmd"])
    assert argv[argv.index("-m") + 1] == "gpt-6-luna" and 'model_reasoning_effort="xhigh"' in argv and argv[argv.index("-s") + 1] == "read-only"


def test_each_change_alone_and_together(cx):
    start(cx)
    for body, tokens in (({"reasoning": "low"}, ["-s", "workspace-write", "-a", "on-request", "-m", "gpt-6-sol", "-c", 'model_reasoning_effort="low"']),
                         ({"sandbox": "read-only"}, ["-s", "read-only", "-a", "on-request"]),
                         ({"approval": "never", "sandbox": "read-only"}, ["-s", "read-only", "-a", "never"])):
        argv = shlex.split(restart(cx, preview=True, **body).json()["cmd"])
        assert argv[:6] == ["codex", "resume", "--no-daemon", "-c", "check_for_update_on_startup=false", "--no-alt-screen"] and argv[-1] == SID
        assert all(t in argv for t in tokens), (body, argv)


# ================================================================ doing it

def test_the_restart_closes_the_old_session_and_resumes_the_conversation_in_the_same_name(cx):
    start(cx, flags={"no_autoresume": True})
    old_id = main.db.open_row(NAME)["row_id"]
    r = restart(cx, approval="never")
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["tmux"], b["agent_session_id"], b["changes"]) == (NAME, SID, {"approval": "never"})
    line = shlex.split(b["cmd"])
    assert line[:3] == ["codex", "resume", "--no-daemon"] and line[-1] == SID and line[line.index("-a") + 1] == "never"
    assert cx.pauses == [main.RESTART_PAUSE] and main.RESTART_PAUSE >= 1.0, "a pause between closing the old Codex and resuming its thread"
    assert [c[0] for c in cx.tmux["created"]] == [NAME], "a new tmux session of the same name"
    assert cx.tmux["sent"] == [(NAME, b["cmd"])], "the adapter's line, typed once"
    new = main.db.open_row(NAME)
    assert new["row_id"] != old_id and new["claude_session_id"] == SID and new["launcher"] == "recovered"
    assert new["opts"] == {"permission_mode": "default", "model": "gpt-6-sol", "reasoning_effort": "high", "approval": "never"}
    assert new["flags"]["perm"] == {"approval": "never", "sandbox": "workspace-write"}, "the Tune shows how it runs now"
    assert new["flags"]["no_autoresume"] is True, "the person's auto-continue opt-out outlives the restart"
    with main.db.lock:
        ended = main.db.conn.execute("SELECT ended_reason FROM sessions WHERE id=?", (old_id,)).fetchone()
    assert ended["ended_reason"] == "killed"
    with main.db.lock:
        ev = main.db.conn.execute("SELECT event, kind, message FROM events WHERE tmux_name=? ORDER BY id DESC LIMIT 1", (NAME,)).fetchone()
    assert (ev["event"], ev["kind"], ev["message"]) == ("BoardCommand", "restart", "restart approval=never"), "the restart is on the session's record"


def test_a_failed_start_after_the_old_process_closed_says_how_to_resume_by_hand(cx, monkeypatch):
    start(cx)

    def boom(*a, **k):
        raise tmux.TmuxError("no server")
    monkeypatch.setattr(main, "_start_session", boom)
    r = restart(cx, sandbox="read-only")
    assert r.status_code == 500 and r.json()["error"] == "restart_failed" and "codex resume" in r.json()["message"] and SID in r.json()["cmd"]


# ================================================================ refusals: nothing changes

@pytest.mark.parametrize("body,status,text", [
    ({"approval": "untrusted"}, 400, "approval must be one of on-request, never"),
    ({"approval": "on-failure"}, 400, "approval must be one of on-request, never"),
    ({"sandbox": "danger-full-access"}, 400, "sandbox must be one of read-only, workspace-write"),
    ({"reasoning": "turbo"}, 400, "reasoning must be one of"),
    ({"reasoning": "ultra"}, 400, "reasoning must be one of"),             # gpt-6-sol does not list ultra
    ({}, 400, "nothing to change"),
    ({"approval": "never", "preview": "yes"}, 400, "preview must be true or false"),
    ({"approval": "never\nrm -rf /"}, 400, "one line"),
])
def test_a_value_that_is_not_offered_is_refused_before_anything_changes(cx, body, status, text):
    start(cx)
    r = restart(cx, **body)
    assert r.status_code == status and text in r.text, r.text
    assert untouched(cx)
    assert restart(cx, **{"preview": True, **body}).status_code == status


def test_the_gates(cx):
    start(cx, state="working")
    r = restart(cx, approval="never")
    assert r.status_code == 409 and r.json()["error"] == "working" and untouched(cx), "a restart ends a running turn: only at the prompt"
    assert restart(cx, approval="never", preview=True).status_code == 200, "looking at the command is allowed any time"


def test_a_session_that_has_not_spoken_has_no_conversation_to_resume(cx):
    start(cx, sid=None)
    r = restart(cx, approval="never", preview=True)
    assert r.status_code == 409 and r.json()["error"] == "not_ready" and "send it one message" in r.json()["message"]
    assert NAME in cx.tmux["sessions"] and cx.tmux["created"] == []


def test_a_session_started_without_approvals_and_sandbox_is_not_restarted_into_a_different_posture(cx):
    start(cx, cmd="codex --no-daemon --no-alt-screen --dangerously-bypass-approvals-and-sandbox")
    r = restart(cx, sandbox="read-only")
    assert r.status_code == 409 and r.json()["error"] == "bypass" and untouched(cx)


def test_only_codex_and_not_a_task(cx):
    start(cx, agent="claude", opts={})
    r = restart(cx, approval="never")
    assert r.status_code == 400 and "only a Codex session" in r.text
    main.db.end(NAME, "killed")
    start(cx, launcher="task")
    r = restart(cx, approval="never")
    assert r.status_code == 400 and "task" in r.text


def test_approve_for_me_cannot_move_to_another_sandbox_but_can_change_approvals(cx):
    start(cx, opts={"permission_mode": "auto"}, cmd="codex --no-daemon --no-alt-screen --approve-for-me -s workspace-write")
    r = restart(cx, sandbox="read-only")
    assert r.status_code == 400 and "Approve for me" in r.text and untouched(cx)
    ok = restart(cx, approval="on-request", preview=True)
    assert ok.status_code == 200 and "--approve-for-me" not in ok.json()["cmd"], "an explicit approval replaces the automatic reviewer"


def test_no_session_no_agent_row_and_a_bad_name(cx):
    assert restart(cx, approval="never").status_code == 404
    cx.tmux["sessions"][NAME] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "zsh", "path": "/x", "pid": 1, "env": {}}
    assert restart(cx, approval="never").status_code == 409, "a tmux session the board has no row for"
    assert cx.post("/api/sessions/not-a-session/restart", headers=H, json={"approval": "never"}).status_code == 400


# ================================================================ the adapter

def test_permissions_of_reads_a_launch_in_the_words_of_a_and_s():
    of = codex.CodexAgent.permissions_of
    assert of({"permission_mode": "default"}) == {"approval": "on-request", "sandbox": "workspace-write"}
    assert of({"permission_mode": "plan"}) == {"approval": "on-request", "sandbox": "read-only"}
    assert of({"permission_mode": "dontAsk"}) == {"approval": "never", "sandbox": "workspace-write"}
    assert of({"permission_mode": "auto"}) == {"approval": None, "sandbox": "workspace-write", "reviewer": "auto"}
    assert of({"sandbox": "read-only", "approval": "never"}) == {"approval": "never", "sandbox": "read-only"}
    assert of({"permission_mode": "default", "approval": "never"}) == {"approval": "never", "sandbox": "workspace-write"}
    assert of({"model": "gpt-6-sol"}) is None and of({}) is None and of(None) is None, "config.toml decides: unknown, so nothing is shown selected"
    assert of({"permission_mode": "default"}, "codex --dangerously-bypass-approvals-and-sandbox") == {"bypass": True}
    assert of(None, "codex --yolo") == {"bypass": True}


def test_the_launch_line_is_unchanged_by_the_permission_refactor(fake_codex):
    """_perm_args now sits on _perm_choice: the argv of every mode is what it was."""
    ag = codex.CodexAgent()
    caps = {"approve_for_me": True, "sandbox": True, "ask_for_approval": True, "approval_on_failure": False}
    old = {"approve_for_me": False, "sandbox": True, "ask_for_approval": True, "approval_on_failure": True}
    args = lambda full, c: ag._perm_args(full, c, bypass=False)       # noqa: E731
    assert args({"permission_mode": "default"}, caps) == ["-s", "workspace-write", "-a", "on-request"]
    assert args({"permission_mode": "auto"}, caps) == ["--approve-for-me", "-s", "workspace-write"]
    assert args({"permission_mode": "auto"}, old) == ["-a", "on-failure", "-s", "workspace-write"]
    assert args({"permission_mode": "auto", "approval": "never"}, caps) == ["-s", "workspace-write", "-a", "never"]
    assert args({"permission_mode": "plan", "approval": "never"}, caps) == ["-s", "read-only", "-a", "never"]


def test_a_session_the_board_starts_carries_how_it_runs(cx):
    """flags.perm is written by _start_session_row for every Codex row: the launcher, reboot recovery and a restart all come through it."""
    real = main._start_session(NAME, "shop", "api", "s1", "claude", str(settings.projects_dir / "shop" / "api"), cmd_line=LINE, agent="codex",
                               opts={"sandbox": "read-only", "approval": "never"})
    assert main.db.open_row(real)["flags"]["perm"] == {"approval": "never", "sandbox": "read-only"}
    main.db.end(real, "killed")
    real = main._start_session(NAME, "shop", "api", "s1", "claude", str(settings.projects_dir / "shop" / "api"), cmd_line=LINE, agent="codex", opts=None)
    assert "perm" not in main.db.open_row(real)["flags"], "unknown stays unknown"
