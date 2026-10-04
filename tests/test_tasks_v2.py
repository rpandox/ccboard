"""Tasks v2, the create-and-dispatch half (v0.5.14a): POST /api/tasks (now | later), GET /api/tasks, PATCH and DELETE of backlog
cards, POST /api/tasks/{id}/dispatch (a new lane session, or a running session), the project folder as a target, and the
legacy per-repo create that now delegates to the same launch helper.

No real agent binary runs: claude is a fake path, tmux is the conftest fake (store 'created' / 'sent' / 'pasted'), hooks are
simulated by writing the session state the hook would have written.
"""
import json
import shlex
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from app import claude_auth, hooks, projects
from app.agents import registry
from app.config import settings

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
LEGACY_KEYS = {"id", "slug", "tmux", "branch", "attach_url"}


@pytest.fixture(autouse=True)
def _fresh_scan():
    """/api/state caches its project scan for 2 s in a module global; an earlier test's scan must not leak into this one."""
    from app import main
    main._invalidate_scan()
    yield
    main._invalidate_scan()


def git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)
    return path


@pytest.fixture
def board(lite_client, projects_dir, fake_tmux, monkeypatch):
    """The board without workers, the fake tmux, a fake claude binary, and shop/api + shop/web as git repos."""
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(claude_auth, "status", lambda: {"installed": True, "version": "2.1.287 (Claude Code)", "loggedIn": True,
                                                        "authMethod": "claude.ai", "email": "me@example.com"})
    monkeypatch.setattr(claude_auth, "version", lambda: "2.1.287 (Claude Code)")
    monkeypatch.setattr(registry, "_IS_LINUX", False)
    registry.invalidate()
    git_init(projects_dir / "shop" / "api")
    git_init(projects_dir / "shop" / "web")
    yield SimpleNamespace(client=lite_client, tmux=fake_tmux, projects=projects_dir)
    registry.invalidate()


def db():
    from app import main
    return main.db


def post_task(board, **body):
    body = {"project": "shop", "repo": "api", "title": "Add login page", "prompt": "Add a login page with tests.", **body}
    return board.client.post("/api/tasks", headers=H, json=body)


def tasks_in_state(board):
    from app import main
    main._invalidate_scan()
    return {t["id"]: t for t in board.client.get("/api/state", headers=H).json()["tasks"]}


def make_session(board, repo="api", name="s1", launcher="claude", state="idle", project="shop"):
    """A running session started through the board, in the state its hooks would have left it in."""
    r = board.client.post(f"/api/projects/{project}/repos/{repo}/sessions", headers=H, json={"launcher": launcher, "name": name})
    assert r.status_code == 201, r.text
    tmux_name = r.json()["tmux"]
    if state:
        db().set_state(tmux_name, state, "test")
    return tmux_name


def notify(board, tmux_name, kind, message="Claude is waiting"):
    """The Notification hook Claude Code fires (permission_prompt, idle_prompt, elicitation_dialog…), through the real /api/hook intake:
    it puts the session in 'waiting' and stores the event, exactly like a live session does."""
    hdr = {"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard-Session": tmux_name}
    r = board.client.post("/api/hook", headers=hdr, content=json.dumps({"hook_event_name": "Notification", "notification_type": kind, "message": message}))
    assert r.status_code == 200 and r.json()["state"] == "waiting", r.text


def backlog(board, **kw):
    r = post_task(board, when="later", **kw)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------- create now: the legacy route's twin

def test_create_now_starts_the_session_exactly_like_the_legacy_route(board):
    r = post_task(board, add_dirs=["shop/web"], model="sonnet", effort="low")
    assert r.status_code == 201, r.text
    t = r.json()
    assert LEGACY_KEYS <= set(t)
    assert (t["slug"], t["tmux"], t["branch"], t["phase"]) == ("add-login-page", "shop--api--t-add-login-page",
                                                                "worktree-add-login-page", "running")
    assert t["attach_url"] == "/term/shop--api--t-add-login-page"
    name, cwd, env = board.tmux["created"][-1]
    assert name == t["tmux"] and cwd == str(board.projects / "shop" / "api") and env["CCBOARD_AGENT"] == "claude"
    argv = shlex.split(board.tmux["sent"][-1][1])
    row = db().task_get(t["id"])
    assert argv == ["claude", "--model", "sonnet", "--effort", "low", "--add-dir", str(board.projects / "shop" / "web"),
                    "--worktree", "add-login-page", "--session-id", row["claude_session_id"], "--", "Add a login page with tests."]
    assert (row["phase"], row["mode"], row["agent"], row["auto_close"]) == ("running", "worktree", "claude", 0)
    assert row["branch"] == "worktree-add-login-page" and row["tmux_name"] == t["tmux"] and row["assigned_at"]
    assert row["worktree"] == str(board.projects / "shop" / "api" / ".claude" / "worktrees" / "add-login-page")
    assert row["session_row"] == t["session_row"] and row["session_row"] is not None
    assert t["task"]["id"] == t["id"] and t["task"]["phase"] == "running" and t["task"]["column"] == "in_progress"
    assert (board.projects / "shop" / "api" / ".git" / "info" / "exclude").read_text().strip().endswith(".claude/worktrees/")


def test_create_now_and_the_legacy_route_build_the_same_command(board):
    new = post_task(board, title="Same", prompt="same prompt", model="opus", permission_mode="acceptEdits").json()
    old = board.client.post("/api/projects/shop/repos/web/tasks", headers=H,
                            json={"title": "Same", "prompt": "same prompt", "model": "opus", "permission_mode": "acceptEdits"}).json()
    assert set(old) == LEGACY_KEYS, "the legacy response is unchanged"

    def shape(task):
        argv = shlex.split(board.tmux["sent"][[n for n, _ in board.tmux["sent"]].index(task["tmux"])][1])
        sid = argv[argv.index("--session-id") + 1]
        return [a for a in argv if a != sid]
    assert shape(new) == shape(old)
    assert db().task_get(old["id"])["phase"] == "running" and db().task_get(old["id"])["mode"] == "worktree"


def test_create_defaults_to_now_and_when_must_be_known(board):
    assert post_task(board).json()["phase"] == "running"
    assert post_task(board, when="whenever").status_code == 400
    assert post_task(board, when="").status_code == 400


@pytest.mark.parametrize("repo", ["api", "root"])
def test_a_prompt_that_starts_with_a_dash_is_a_prompt_not_an_option(board, repo):
    """A markdown bullet list as the prompt made the claude CLI die at once on an unknown option. `--` ends the options, so the prompt
    is taken literally: also when it IS a flag (it cannot smuggle bypass in), in a worktree task and in a project-folder task."""
    prompts = ["- fix the bug\n- add tests", "--dangerously-skip-permissions", "-x"]
    for i, prompt in enumerate(prompts):
        r = post_task(board, repo=repo, title=f"Dash {i}", prompt=prompt)
        assert r.status_code == 201, r.text
        argv = shlex.split(dict(board.tmux["sent"])[r.json()["tmux"]])
        assert argv[-2:] == ["--", prompt], argv
        assert argv.index("--") > argv.index("--session-id"), "after every option, so none of them is swallowed"
        assert "--dangerously-skip-permissions" not in argv[:argv.index("--")]
    # a backlog card dispatched into a new session builds the same command
    tid = backlog(board, repo=repo, title="Dash later", prompt=prompts[0])
    out = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"mode": "lane"})
    assert out.status_code == 200, out.text
    assert shlex.split(dict(board.tmux["sent"])[out.json()["tmux"]])[-2:] == ["--", prompts[0]]


def test_create_validation_matches_the_legacy_route(board):
    assert post_task(board, title="   ").status_code == 400
    assert post_task(board, prompt="").status_code == 400
    assert post_task(board, project="", repo="").status_code == 400
    assert post_task(board, prompt="x" * 20001).status_code == 400
    assert post_task(board, repo="nope").status_code == 404
    assert post_task(board, project="ghost", repo="api").status_code == 404
    assert post_task(board, args="--model 'unterminated").status_code == 400
    assert post_task(board, effort="extreme").status_code == 400
    assert post_task(board, add_dirs=["shop/missing"]).status_code == 400
    long_title = post_task(board, title="  spaced   out  " + "w" * 200)
    assert long_title.status_code == 201
    assert db().task_get(long_title.json()["id"])["title"] == ("spaced out " + "w" * 200)[:120]
    assert not board.tmux["sent"][:0] and len(board.tmux["created"]) == 1, "refused requests start nothing"


def test_a_missing_claude_refuses_now_but_not_later(board, monkeypatch):
    monkeypatch.setattr(settings, "claude_bin", lambda: None)
    assert post_task(board).status_code == 400 and not board.tmux["created"]
    assert post_task(board, when="later").status_code == 201, "a card can wait for claude to be installed"


# ---------------------------------------------------------------- bypass and codex are refused everywhere

def test_bypass_is_refused_for_now_later_and_dispatch(board):
    for kw in ({"permission_mode": "bypassPermissions"}, {"args": "--dangerously-skip-permissions"},
               {"args": "--permission-mode=bypassPermissions"}, {"args": "--settings '{}'"}):
        for when in ("now", "later"):
            r = post_task(board, when=when, **kw)
            assert r.status_code == 400 and "not allowed for tasks" in r.json()["error"], (kw, when, r.text)
    assert not board.tmux["created"] and db().tasks() == []
    tid = backlog(board)
    r = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"permission_mode": "bypassPermissions"})
    assert r.status_code == 400 and "not allowed for tasks" in r.json()["error"]
    r = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"args": "--dangerously-skip-permissions"})
    assert r.status_code == 400
    assert db().task_get(tid)["phase"] == "backlog" and not board.tmux["created"], "a refused dispatch leaves the card where it was"
    # a card whose stored spec somehow carries bypass (hand-edited row) is refused at dispatch too
    db().task_update(tid, spec={"permission_mode": "bypassPermissions"})
    assert board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={}).status_code == 400
    assert db().task_get(tid)["phase"] == "backlog"


def test_codex_without_a_binary_and_unknown_agents_are_refused(board):
    """Codex is a task agent since v0.5.11 (tests/test_recover.py covers a launch with a fake codex); with no codex binary a start is
    refused, a backlog card is not (it starts nothing), and an agent the board has no adapter for is always a 400."""
    r = post_task(board, agent="codex")
    assert r.status_code == 400 and r.json()["error"] == "codex is not installed on this box"
    assert post_task(board, agent="codex", when="later").status_code == 201
    r = post_task(board, agent="gemini")
    assert r.status_code == 400 and "unknown agent 'gemini'" in r.json()["error"]
    assert post_task(board, agent="claude").status_code == 201
    tid = backlog(board, title="Other")
    r = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"agent": "codex"})
    assert r.status_code == 400 and r.json()["error"] == "codex is not installed on this box"
    assert board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"agent": "gemini"}).status_code == 400
    assert db().task_get(tid)["phase"] == "backlog"


# ---------------------------------------------------------------- create later: a backlog card, nothing started

def test_create_later_makes_a_backlog_row_and_starts_nothing(board):
    r = post_task(board, when="later", model="opus", effort="high", permission_mode="acceptEdits", args="--verbose",
                  add_dirs=["shop/web"], auto_close=True)
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["slug"], body["phase"], body["tmux"]) == ("add-login-page", "backlog", None)
    assert board.tmux["created"] == [] and board.tmux["sent"] == [] and board.tmux["sessions"] == {}
    row = db().task_get(body["id"])
    assert (row["phase"], row["mode"], row["agent"], row["auto_close"]) == ("backlog", "worktree", "claude", 1)
    assert (row["tmux_name"], row["worktree"], row["branch"], row["base"]) == ("", "", "", "main")
    assert row["session_row"] is None and row["assigned_at"] is None and row["claude_session_id"] is None
    assert json.loads(row["spec"]) == {"model": "opus", "effort": "high", "permission_mode": "acceptEdits", "args": "--verbose",
                                       "add_dirs": ["shop/web"]}
    assert (row["title"], row["prompt"]) == ("Add login page", "Add a login page with tests.")
    # the 201 carries the state.tasks row: the client paints the card without waiting for a poll
    v = body["task"]
    assert (v["id"], v["column"], v["phase"], v["tmux"], v["session"]) == (body["id"], "backlog", "backlog", "", None)
    assert v["prompt"] == "Add a login page with tests." and v["prompt_len"] == len("Add a login page with tests.")
    assert (v["project"], v["repo"], v["auto_close"], v["created_at"]) == ("shop", "api", True, row["created_at"])


def test_create_later_without_options_has_an_empty_spec_and_unique_slugs(board):
    a, b = backlog(board), backlog(board)
    assert db().task_get(a)["slug"] == "add-login-page" and db().task_get(b)["slug"] == "add-login-page-2"
    assert json.loads(db().task_get(a)["spec"]) == {}
    started = post_task(board).json()                # a started task with the same title does not collide either
    assert started["slug"] == "add-login-page-3"


def test_create_later_refuses_a_repo_that_could_never_run_it(board):
    assert post_task(board, repo="nope", when="later").status_code == 404
    (board.projects / "shop" / "notes").mkdir()
    assert post_task(board, repo="notes", when="later").status_code == 404, "not a git repo"
    assert db().tasks() == []


# ---------------------------------------------------------------- list and filter

def test_list_and_filter(board):
    a = backlog(board, title="Alpha")
    b = backlog(board, title="Beta", repo="web")
    run = post_task(board, title="Gamma").json()["id"]
    git_init(board.projects / "other" / "svc")
    c = backlog(board, title="Delta", project="other", repo="svc")
    ids = lambda **q: sorted(t["id"] for t in board.client.get("/api/tasks", headers=H, params=q).json()["tasks"])
    assert ids() == sorted([a, b, run, c])
    assert ids(project="shop") == sorted([a, b, run]) and ids(project="other") == [c]
    assert ids(project="shop", repo="web") == [b] and ids(repo="api") == sorted([a, run])
    assert ids(phase="backlog") == sorted([a, b, c]) and ids(phase="running") == [run]
    assert ids(phase="backlog,running", project="shop") == sorted([a, b, run]) and ids(phase="done") == []
    assert board.client.get("/api/tasks", headers=H, params={"phase": "sideways"}).status_code == 400
    # the same shape as state.tasks
    listed = {t["id"]: t for t in board.client.get("/api/tasks", headers=H).json()["tasks"]}
    state = tasks_in_state(board)
    assert listed.keys() == state.keys() and all(listed[i] == state[i] for i in listed)
    # archived tasks drop out of both
    db().task_update(a, archived_at="2026-10-04T00:00:00+00:00")
    assert a not in ids()


def test_get_one_task_has_the_full_prompt_and_spec(board):
    long_prompt = "p" * 1500
    tid = backlog(board, prompt=long_prompt, model="opus")
    head = tasks_in_state(board)[tid]
    assert head["prompt"] == long_prompt[:600] and head["prompt_len"] == 1500, "the poll carries a head and the full length"
    full = board.client.get(f"/api/tasks/{tid}", headers=H).json()
    assert full["prompt"] == long_prompt and full["spec"] == {"model": "opus"} and full["column"] == "backlog"
    assert board.client.get("/api/tasks/9999", headers=H).status_code == 404
    started = post_task(board, title="Started").json()["id"]
    assert tasks_in_state(board)[started]["prompt"] is None, "only unassigned rows carry a prompt head in the poll"


# ---------------------------------------------------------------- patch and delete

def test_patch_edits_a_backlog_card(board):
    tid = backlog(board)
    r = board.client.patch(f"/api/tasks/{tid}", headers=H, json={"title": "  Better   title ", "prompt": "  new prompt \n"})
    assert r.status_code == 200, r.text
    assert (r.json()["title"], r.json()["prompt"]) == ("Better title", "new prompt")
    assert r.json()["task"]["title"] == "Better title"
    assert (db().task_get(tid)["title"], db().task_get(tid)["prompt"]) == ("Better title", "new prompt")
    assert board.client.patch(f"/api/tasks/{tid}", headers=H, json={"title": "Only title"}).status_code == 200
    assert db().task_get(tid)["prompt"] == "new prompt", "a field left out is kept"
    assert board.client.patch(f"/api/tasks/{tid}", headers=H, json={"prompt": "only prompt"}).status_code == 200
    assert db().task_get(tid)["title"] == "Only title"
    for bad in ({}, {"title": "  "}, {"prompt": ""}, {"prompt": "x" * 20001}):
        assert board.client.patch(f"/api/tasks/{tid}", headers=H, json=bad).status_code == 400, bad
    assert db().task_get(tid)["prompt"] == "only prompt"
    assert board.client.patch("/api/tasks/9999", headers=H, json={"title": "x"}).status_code == 404


def test_patch_allows_queued_and_refuses_a_started_task(board):
    queued = db().task_add(project="shop", repo="api", slug="q", title="Queued", prompt="p", phase="queued")
    assert board.client.patch(f"/api/tasks/{queued}", headers=H, json={"title": "Still queued"}).status_code == 200
    started = post_task(board).json()["id"]
    r = board.client.patch(f"/api/tasks/{started}", headers=H, json={"title": "Nope"})
    assert r.status_code == 409 and r.json()["error"] == "only a backlog task can be edited"
    assert db().task_get(started)["title"] == "Add login page"
    for phase in ("done", "failed", "cancelled"):
        db().task_update(started, phase=phase)
        assert board.client.patch(f"/api/tasks/{started}", headers=H, json={"title": "Nope"}).status_code == 409


def test_delete_rules(board):
    tid = backlog(board)
    assert board.client.delete(f"/api/tasks/{tid}", headers=H).status_code == 204
    assert db().task_get(tid) is None and tasks_in_state(board) == {}
    assert board.client.delete(f"/api/tasks/{tid}", headers=H).status_code == 404
    queued = db().task_add(project="shop", repo="api", slug="q", title="Queued", prompt="p", phase="queued")
    assert board.client.delete(f"/api/tasks/{queued}", headers=H).status_code == 204
    cancelled = db().task_add(project="shop", repo="api", slug="c", title="Cancelled", prompt="p", phase="cancelled")
    assert board.client.delete(f"/api/tasks/{cancelled}", headers=H).status_code == 204
    started = post_task(board).json()["id"]
    r = board.client.delete(f"/api/tasks/{started}", headers=H)
    assert r.status_code == 409 and r.json()["error"] == "archive a started task instead"
    assert db().task_get(started) is not None and len(board.tmux["sessions"]) == 1, "nothing was killed"
    for phase in ("running", "done", "failed"):
        db().task_update(started, phase=phase)
        assert board.client.delete(f"/api/tasks/{started}", headers=H).status_code == 409
    db().task_update(started, phase="cancelled")      # a cancelled row that still owns a worktree is archived, never just dropped
    assert board.client.delete(f"/api/tasks/{started}", headers=H).status_code == 409


# ---------------------------------------------------------------- dispatch to a lane

def test_dispatch_lane_starts_the_backlog_row_in_a_new_session(board):
    tid = backlog(board, model="opus", effort="high", permission_mode="acceptEdits", add_dirs=["shop/web"])
    assert board.tmux["created"] == []
    r = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"mode": "lane"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["id"], out["phase"], out["tmux"], out["attach_url"]) == (tid, "running", "shop--api--t-add-login-page",
                                                                       "/term/shop--api--t-add-login-page")
    assert out["session_row"] is not None and out["task"]["phase"] == "running" and out["task"]["column"] == "in_progress"
    assert [c[0] for c in board.tmux["created"]] == [out["tmux"]]
    argv = shlex.split(board.tmux["sent"][-1][1])
    row = db().task_get(tid)
    assert argv == ["claude", "--model", "opus", "--effort", "high", "--permission-mode", "acceptEdits", "--add-dir",
                    str(board.projects / "shop" / "web"), "--worktree", "add-login-page", "--session-id", row["claude_session_id"],
                    "--", "Add a login page with tests."], "the card's spec became the launch options"
    assert (row["phase"], row["mode"], row["agent"]) == ("running", "worktree", "claude")
    assert row["tmux_name"] == out["tmux"] and row["session_row"] == out["session_row"] and row["assigned_at"]
    assert row["branch"] == "worktree-add-login-page" and row["worktree"].endswith(".claude/worktrees/add-login-page")
    assert row["slug"] == "add-login-page" and db().task_slugs("shop", "api") == {"add-login-page"}
    assert len(db().tasks()) == 1, "the row was updated, not duplicated"
    sess = db().open_rows()[out["tmux"]]
    assert sess["launcher"] == "task" and sess["row_id"] == out["session_row"] and sess["opts"]["model"] == "opus"
    # a second dispatch (a double tap, or two tabs) is refused and starts nothing
    again = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={})
    assert again.status_code == 409 and again.json()["error"] == "already dispatched"
    assert len(board.tmux["created"]) == 1 and board.tmux["pasted"] == []
    assert board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": out["tmux"]}).status_code == 409


def test_dispatch_lane_defaults_and_overrides(board):
    tid = backlog(board, model="opus", effort="low")
    r = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H)          # no body at all = lane
    assert r.status_code == 200 and r.json()["phase"] == "running", r.text
    assert shlex.split(board.tmux["sent"][-1][1])[1:5] == ["--model", "opus", "--effort", "low"]
    tid2 = backlog(board, title="Second", model="opus", effort="low")
    r = board.client.post(f"/api/tasks/{tid2}/dispatch", headers=H, json={"model": "haiku", "auto_close": True})
    assert r.status_code == 200, r.text
    argv = shlex.split(board.tmux["sent"][-1][1])
    assert argv[1:5] == ["--model", "haiku", "--effort", "low"], "the body overrides the card, the rest of the spec stays"
    assert db().task_get(tid2)["auto_close"] == 1


def test_dispatch_lane_names_the_branch_after_the_edited_title(board):
    tid = backlog(board, title="Old name")
    board.client.patch(f"/api/tasks/{tid}", headers=H, json={"title": "Brand new name"})
    out = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"mode": "lane"}).json()
    assert out["tmux"] == "shop--api--t-brand-new-name" and out["branch"] == "worktree-brand-new-name"
    assert db().task_get(tid)["slug"] == "brand-new-name"


def test_dispatch_lane_keeps_its_own_slug_but_dodges_a_taken_worktree(board):
    a = backlog(board, title="Same title")
    b = backlog(board, title="Same title")
    assert db().task_get(b)["slug"] == "same-title-2"
    # the first card's slug is free on disk: it keeps it (its own row is not a collision)
    assert board.client.post(f"/api/tasks/{a}/dispatch", headers=H, json={}).json()["slug"] == "same-title"
    # a worktree appeared on disk under the second card's name meanwhile: it moves to the next free one
    (board.projects / "shop" / "api" / ".claude" / "worktrees" / "same-title-2").mkdir(parents=True)
    out = board.client.post(f"/api/tasks/{b}/dispatch", headers=H, json={}).json()
    assert out["slug"] == "same-title-3" and out["tmux"] == "shop--api--t-same-title-3"


def test_dispatch_errors_leave_the_card_in_the_backlog(board, monkeypatch):
    assert board.client.post("/api/tasks/9999/dispatch", headers=H, json={}).status_code == 404
    tid = backlog(board)
    monkeypatch.setattr(settings, "claude_bin", lambda: None)
    assert board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={}).status_code == 400
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    assert board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"mode": "teleport"}).status_code == 400
    assert board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"mode": "session"}).status_code == 400
    assert board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"mode": "lane", "session": "shop--api--s1"}).status_code == 400
    board.tmux["sessions"]["shop--api--t-add-login-page"] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1",
                                                              "command": "zsh", "path": "/", "pid": 1, "env": {}}
    assert board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={}).status_code == 409, "the session name is taken"
    assert db().task_get(tid)["phase"] == "backlog" and db().task_get(tid)["tmux_name"] == ""
    # the repo vanished after the card was written
    gone = backlog(board, title="Gone", repo="web")
    (board.projects / "shop" / "web" / ".git").rename(board.projects / "shop" / "web" / ".git.off")
    assert board.client.post(f"/api/tasks/{gone}/dispatch", headers=H, json={}).status_code == 404
    assert db().task_get(gone)["phase"] == "backlog"


def test_two_taps_on_start_start_one_session(board, monkeypatch):
    """The endpoint is a sync def run in a thread pool: two requests can overlap. The second must see the first one's phase."""
    from app import main
    tid = backlog(board)
    real = main._start_session_row

    def slow(*a, **k):
        time.sleep(0.25)                       # hold the first request inside the launch while the second arrives
        return real(*a, **k)
    monkeypatch.setattr(main, "_start_session_row", slow)
    codes = []

    def tap():
        r = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={})
        codes.append((r.status_code, r.json().get("error")))
    threads = [threading.Thread(target=tap) for _ in range(2)]
    for t in threads:
        t.start()
        time.sleep(0.05)
    for t in threads:
        t.join(10)
    assert sorted(c[0] for c in codes) == [200, 409], codes
    assert [c[1] for c in codes if c[0] == 409] == ["already dispatched"], "refused for its phase, not by a name collision"
    assert len(board.tmux["created"]) == 1 and len(db().tasks()) == 1


# ---------------------------------------------------------------- dispatch to a running session

def test_dispatch_to_a_session_pastes_the_prompt_and_binds_the_row(board):
    s1 = make_session(board, state="idle")
    tid = backlog(board, prompt="Line one.\nLine two.")
    before_created = len(board.tmux["created"])
    r = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1})
    assert r.status_code == 200, r.text
    out = r.json()
    sess_row = db().open_rows()[s1]["row_id"]
    assert (out["id"], out["phase"], out["tmux"], out["session_row"], out["pasted"]) == (tid, "running", s1, sess_row, True)
    assert board.tmux["pasted"][-1] == (s1, "Line one.\nLine two.", True), "one bracketed paste, then Enter"
    assert len(board.tmux["created"]) == before_created, "no new session and no worktree"
    row = db().task_get(tid)
    assert (row["phase"], row["mode"], row["tmux_name"], row["session_row"]) == ("running", "session", s1, sess_row)
    assert row["assigned_at"] and row["worktree"] == "" and row["branch"] == ""
    # the board shows it working at once; the hook confirms later
    assert db().open_rows()[s1]["state"] == "working" and db().open_rows()[s1]["last_prompt"].startswith("Line one.")
    assert out["task"]["mode"] == "session" and out["task"]["column"] == "in_progress"
    # the card cannot be dispatched twice
    again = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1})
    assert again.status_code == 409 and again.json()["error"] == "already dispatched"
    assert len(board.tmux["pasted"]) == 1


@pytest.mark.parametrize("state", ["idle", "done"])
def test_dispatch_to_a_session_accepts_idle_and_done(board, state):
    s1 = make_session(board, state=state)
    tid = backlog(board)
    assert board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1}).status_code == 200
    assert board.tmux["pasted"][-1][0] == s1


def test_dispatch_to_a_waiting_session_needs_its_idle_prompt(board):
    """'waiting' is also what a permission_prompt / elicitation_dialog Notification sets, and typing into those dialogs answers them.
    Measured on a live board: a permission_prompt Notification on a session with no pending permission row, then dispatch {session},
    pasted the prompt and Enter into the TUI dialog. Only an idle_prompt Notification (Claude idle at its prompt) lets the paste in."""
    s1 = make_session(board, state="idle")
    tid = backlog(board)
    for kind in ("permission_prompt", "elicitation_dialog", "elicitation_url_dialog"):
        notify(board, s1, kind)
        r = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1})
        assert r.status_code == 409, (kind, r.text)
        assert r.json() == {"error": "the session is waiting on a prompt (permission or dialog): answer it first", "state": "waiting", "wait_kind": kind}
        assert board.tmux["pasted"] == [] and db().task_get(tid)["phase"] == "backlog", "nothing typed, the card stays in the Backlog"
    notify(board, s1, "idle_prompt")
    r = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1})
    assert r.status_code == 200, r.text
    assert board.tmux["pasted"][-1] == (s1, "Add a login page with tests.", True)


def test_a_waiting_session_with_no_notification_on_record_is_not_pasted_into(board):
    """Fail closed: a session that is 'waiting' for a reason the board has no idle_prompt for (no event at all, or a newer event of
    another kind, like a PermissionRequest after an old idle_prompt) is treated as sitting in a dialog."""
    s1 = make_session(board, state="waiting")                    # state only: no event row (trimmed, or never stored)
    tid = backlog(board)
    r = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1})
    assert r.status_code == 409 and r.json()["state"] == "waiting" and r.json()["wait_kind"] is None, r.text
    notify(board, s1, "idle_prompt")
    db().set_state(s1, "waiting", "PermissionRequest", message="permission: Bash")      # what a later PermissionRequest hook leaves
    db().add_event(s1, "PermissionRequest", "Bash", "Bash: ls", {})
    r = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1})
    assert r.status_code == 409 and r.json()["wait_kind"] == "PermissionRequest", r.text
    assert board.tmux["pasted"] == [] and db().task_get(tid)["phase"] == "backlog"
    # an idle or done session never looks at events: an old permission_prompt on record does not block a finished turn
    notify(board, s1, "permission_prompt")
    db().set_state(s1, "done", "Stop")
    assert board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1}).status_code == 200


def test_state_sessions_say_what_a_waiting_session_waits_on(board):
    """state.projects[].repos[].sessions[].wait_kind: on a 'waiting' session, the Notification kind it waits on (the Send sheet enables it
    only for idle_prompt; None when no event says); a session in any other state has no such key."""
    a, b, c, d = (make_session(board, name=n, state="idle") for n in ("a", "b", "c", "d"))
    notify(board, a, "idle_prompt")
    notify(board, b, "permission_prompt")
    db().set_state(c, "waiting", "test")                         # waiting, no event on record
    db().add_event(d, "Notification", "idle_prompt", "old", {})  # an event, but the session is not waiting
    from app import main
    main._invalidate_scan()
    st = board.client.get("/api/state", headers=H).json()
    sessions = {x["tmux"]: x for p in st["projects"] for r in p["repos"] for x in r["sessions"]}
    assert {k.split("--")[-1]: v["wait_kind"] for k, v in sessions.items() if "wait_kind" in v} == {"a": "idle_prompt", "b": "permission_prompt", "c": None}
    assert "wait_kind" not in sessions[d], "an event on record does not make an idle session wait"
    assert sessions[d]["state"] == "idle" and sessions[c]["state"] == "waiting"
    assert db().last_event(a)["kind"] == "idle_prompt" and db().last_event("shop--api--nope") is None
    assert set(db().last_events([a, b, "shop--api--nope"])) == {a, b}, "one grouped query, a name without events is absent"


@pytest.mark.parametrize("state", ["working", "errored", "ended", "unknown"])
def test_dispatch_to_a_busy_session_is_409_with_its_state(board, state):
    s1 = make_session(board, state=state if state != "unknown" else None)
    tid = backlog(board)
    r = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1})
    assert r.status_code == 409 and r.json()["state"] == state and r.json()["error"], r.text
    assert board.tmux["pasted"] == [] and db().task_get(tid)["phase"] == "backlog"


def test_dispatch_to_a_session_with_a_pending_permission_is_409(board):
    s1 = make_session(board, state="waiting")
    pid = db().perm_add(s1, "Bash", "Bash: rm -rf build", {"command": "rm -rf build"})
    tid = backlog(board)
    r = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1})
    assert r.status_code == 409 and r.json()["state"] == "waiting" and r.json()["pending_permission"] is True
    assert "permission" in r.json()["error"] and board.tmux["pasted"] == [], "typing into a permission dialog would answer it"
    db().perm_decide(pid, "deny", "test")
    assert board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1}).status_code == 409, \
        "decided, but the session still sits in the dialog (no idle_prompt yet): the wait-kind rule still holds it"
    notify(board, s1, "idle_prompt")
    assert board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1}).status_code == 200
    # a permission pending on ANOTHER session does not block this one
    s2 = make_session(board, name="s2", state="idle")
    db().perm_add(s2, "Bash", "x", {})
    t2 = backlog(board, title="Other")
    s3 = make_session(board, name="s3", state="idle")
    assert board.client.post(f"/api/tasks/{t2}/dispatch", headers=H, json={"session": s3}).status_code == 200


def test_dispatch_to_a_session_in_another_repo_needs_force(board):
    web = make_session(board, repo="web", name="w1", state="idle")
    tid = backlog(board, prompt="Fix the api.")
    r = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": web})
    assert r.status_code == 409 and r.json()["mismatch"] == {"task": "shop/api", "session": "shop/web"}
    assert board.tmux["pasted"] == [] and db().task_get(tid)["phase"] == "backlog"
    r = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": web, "force": True})
    assert r.status_code == 200, r.text
    api_path = board.projects / "shop" / "api"
    assert board.tmux["pasted"][-1] == (web, f"Work in {api_path}.\n\nFix the api.", True)
    assert db().task_get(tid)["tmux_name"] == web and db().task_get(tid)["mode"] == "session"


def test_force_does_not_prefix_a_matching_session_and_other_projects_mismatch(board):
    git_init(board.projects / "other" / "svc")
    s1 = make_session(board, state="idle")
    t1 = backlog(board, prompt="Same repo.")
    assert board.client.post(f"/api/tasks/{t1}/dispatch", headers=H, json={"session": s1, "force": True}).status_code == 200
    assert board.tmux["pasted"][-1][1] == "Same repo.", "force only matters when the repos differ"
    other = make_session(board, project="other", repo="svc", state="idle")
    t2 = backlog(board, title="Cross project")
    r = board.client.post(f"/api/tasks/{t2}/dispatch", headers=H, json={"session": other})
    assert r.status_code == 409 and r.json()["mismatch"] == {"task": "shop/api", "session": "other/svc"}


def test_dispatch_to_an_unknown_or_foreign_session(board):
    tid = backlog(board)
    c = board.client
    assert c.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": "shop--api--ghost"}).status_code == 404
    assert c.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": "not a name"}).status_code == 400
    assert c.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": "ccboard-internal"}).status_code == 400
    # a plain shell would execute the prompt as commands: never
    sh = make_session(board, launcher="shell", name="sh1", state="idle")
    r = c.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": sh})
    assert r.status_code == 409 and r.json()["agent"] == "shell"
    # a tmux session the board did not start has no row to bind
    board.tmux["sessions"]["shop--api--outside"] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%9", "command": "claude",
                                                     "path": "/", "pid": 9, "env": {}}
    assert c.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": "shop--api--outside"}).status_code == 409
    assert board.tmux["pasted"] == [] and db().task_get(tid)["phase"] == "backlog"


def test_dispatch_to_a_session_strips_control_characters_from_the_paste(board):
    s1 = make_session(board, state="idle")
    tid = backlog(board, prompt="Run\x1b[201~ rm -rf x\r\nsecond\tline\x07")
    assert board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1}).status_code == 200
    assert board.tmux["pasted"][-1][1] == "Run[201~ rm -rf x\nsecond\tline", "no ESC can end the bracketed paste early"


def test_a_queued_task_is_not_dispatched_by_hand(board):
    s1 = make_session(board, state="idle")
    queued = db().task_add(project="shop", repo="api", slug="q", title="Queued", prompt="p", phase="queued")
    r = board.client.post(f"/api/tasks/{queued}/dispatch", headers=H, json={"session": s1})
    assert r.status_code == 409 and "queued" in r.json()["error"]


# ---------------------------------------------------------------- the project folder as a target

def test_the_project_folder_is_a_task_target_when_it_is_a_git_repo(board):
    git_init(board.projects / "shop")
    r = post_task(board, repo="root")
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["tmux"] == "shop--root--t-add-login-page" and t["branch"] == "worktree-add-login-page"
    assert board.tmux["created"][-1][1] == str(board.projects / "shop"), "the session starts in the project folder"
    row = db().task_get(t["id"])
    assert (row["repo"], row["phase"]) == ("root", "running")
    assert row["worktree"] == str(board.projects / "shop" / ".claude" / "worktrees" / "add-login-page")
    assert "--worktree" in shlex.split(board.tmux["sent"][-1][1])
    assert (board.projects / "shop" / ".git" / "info" / "exclude").read_text().strip().endswith(".claude/worktrees/")
    # later + dispatch, and the legacy route, work the same way
    tid = backlog(board, repo="root", title="Root later")
    out = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={}).json()
    assert out["tmux"] == "shop--root--t-root-later"
    legacy = board.client.post("/api/projects/shop/repos/root/tasks", headers=H, json={"title": "Legacy root", "prompt": "p"})
    assert legacy.status_code == 201 and legacy.json()["tmux"] == "shop--root--t-legacy-root"
    st = tasks_in_state(board)
    assert {t["repo"] for t in st.values()} == {"root"} and all(t["column"] == "in_progress" for t in st.values())


def test_a_project_folder_that_is_not_a_git_repo_runs_the_task_in_place(board):
    """User request: "task not just in repo but in project folder also, like I can give the whole project a task". A project folder
    that is not a git repo cannot host a worktree, so the task runs in place: a session in the folder, no branch, mode 'attached'."""
    r = post_task(board, repo="root", when="now")
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["branch"] == "" and out["tmux"].startswith("shop--root--t-") and out["task"]["mode"] == "attached"
    name, cwd, _env = board.tmux["created"][-1]
    assert cwd == str(board.projects / "shop"), "the session runs in the project folder itself"
    typed = dict(board.tmux["sent"])[name]
    assert typed.startswith("claude ") and "--worktree" not in typed and "--session-id" in typed
    argv = shlex.split(typed)
    assert argv[-2:] == ["--", "Add a login page with tests."], "the in-place command also ends the options before the prompt"
    t = db().task_get(out["id"])
    assert t["mode"] == "attached" and t["worktree"] == "" and t["branch"] == "" and t["phase"] == "running"
    later = post_task(board, repo="root", when="later")
    assert later.status_code == 201 and later.json()["task"]["mode"] == "attached"
    tid = later.json()["id"]
    d = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={})
    assert d.status_code == 200 and db().task_get(tid)["mode"] == "attached" and db().task_get(tid)["worktree"] == ""
    assert post_task(board, project="ghost", repo="root", when="later").status_code == 404, "a missing project is still 404"


def test_a_task_in_the_project_folder_can_go_to_a_session_in_a_repo_with_force(board):
    git_init(board.projects / "shop")
    s1 = make_session(board, state="idle")                      # shop/api
    tid = backlog(board, repo="root", prompt="Look across the project.")
    r = board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1})
    assert r.status_code == 409 and r.json()["mismatch"] == {"task": "shop/root", "session": "shop/api"}
    assert board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1, "force": True}).status_code == 200
    assert board.tmux["pasted"][-1][1] == f"Work in {board.projects / 'shop'}.\n\nLook across the project."


# ---------------------------------------------------------------- what state.tasks shows

def test_state_shows_the_backlog_card_and_the_session_mode_task_in_the_right_columns(board):
    s1 = make_session(board, state="idle")
    card = backlog(board, title="Waits")
    sent = backlog(board, title="Sent")
    st = tasks_in_state(board)
    assert st[card]["column"] == "backlog" and st[card]["phase"] == "backlog" and st[card]["session"] is None
    assert board.client.post(f"/api/tasks/{sent}/dispatch", headers=H, json={"session": s1}).status_code == 200
    st = tasks_in_state(board)
    t = st[sent]
    assert (t["mode"], t["phase"], t["tmux"], t["column"]) == ("session", "running", s1, "in_progress")
    assert t["session"]["state"] == "working" and t["session_row"] == db().open_rows()[s1]["row_id"] and t["worktree"] == ""
    assert st[card]["column"] == "backlog", "the other card did not move"
    # the same task follows its session's state, the way a worktree task does
    db().set_state(s1, "waiting", "Notification")
    assert tasks_in_state(board)[sent]["column"] == "needs_you"
    db().set_state(s1, "working", "UserPromptSubmit")
    assert tasks_in_state(board)[sent]["column"] == "in_progress"
    db().set_state(s1, "done", "Stop")
    assert tasks_in_state(board)[sent]["column"] == "done"
    # the session carries the task chip
    state = board.client.get("/api/state", headers=H).json()
    chips = [s["task"] for p in state["projects"] for r in p["repos"] for s in r["sessions"] if s["tmux"] == s1]
    assert chips and chips[0]["id"] == sent and chips[0]["phase"] == "running"


def test_a_lane_dispatched_task_is_in_progress_before_any_hook_fires(board):
    tid = backlog(board)
    board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={})
    t = tasks_in_state(board)[tid]
    assert t["column"] == "in_progress" and t["session"]["state"] == "unknown" and t["phase"] == "running"


def test_a_session_mode_task_is_archived_without_touching_the_session(board):
    s1 = make_session(board, state="done")
    tid = backlog(board)
    board.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={"session": s1})
    r = board.client.post(f"/api/tasks/{tid}/archive", headers=H, json={})
    assert r.status_code == 200 and r.json() == {"archived": tid, "worktree_removed": False}
    assert s1 in board.tmux["sessions"] and db().open_rows()[s1]["ended_at"] is None, "the session is the user's"
    assert tid not in tasks_in_state(board)
    # a backlog card is still deleted, not archived
    card = backlog(board, title="Card")
    assert board.client.post(f"/api/tasks/{card}/archive", headers=H, json={}).status_code == 409


# ---------------------------------------------------------------- auth

def test_the_new_routes_need_an_identity_and_the_csrf_header(board):
    tid = backlog(board)
    nobody = {"X-CCBoard": "1"}
    no_csrf = {"Tailscale-User-Login": "alice@example.com"}
    for method, path, body in (("get", "/api/tasks", None), ("post", "/api/tasks", {"project": "shop"}),
                               ("get", f"/api/tasks/{tid}", None), ("patch", f"/api/tasks/{tid}", {"title": "x"}),
                               ("delete", f"/api/tasks/{tid}", None), ("post", f"/api/tasks/{tid}/dispatch", {})):
        assert getattr(board.client, method)(path, headers=nobody, **({"json": body} if body is not None else {})).status_code == 403, path
        if method != "get":
            assert getattr(board.client, method)(path, headers=no_csrf, **({"json": body} if body is not None else {})).status_code == 403, path
    assert db().task_get(tid)["title"] == "Add login page" and db().task_get(tid)["phase"] == "backlog"
