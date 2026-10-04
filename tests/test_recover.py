import subprocess

from app import recover

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


def test_plan_and_run(client, projects_dir, fake_tmux, monkeypatch):
    from app import main
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/web"], check=True)
    c1 = client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "claude", "add_dirs": ["shop/web"]}).json()
    c2 = client.post("/api/projects/shop/repos/web/sessions", headers=H, json={"launcher": "continue"}).json()
    sh = client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()
    # a "reboot": tmux forgets everything, the DB still has the rows
    fake_tmux["sessions"].clear()
    fake_tmux["created"].clear()
    rows = main.db.open_rows()
    todo = recover.plan(rows, set())
    by = {t["name"]: t for t in todo}
    assert set(by) == {c1["tmux"], c2["tmux"]}
    assert by[c1["tmux"]]["cmd"][:3] == ["claude", "--resume", c1["claude_session_id"]] and "--add-dir" in by[c1["tmux"]]["cmd"]
    assert by[c2["tmux"]]["cmd"] == ["claude", "--continue"]
    summary = recover.run(main.db, main._start_session)
    assert sorted(summary["recovered"]) == sorted([c1["tmux"], c2["tmux"]]) and summary["closed"] == [sh["tmux"]]
    names = [c[0] for c in fake_tmux["created"]]
    assert sorted(names) == sorted([c1["tmux"], c2["tmux"]])
    typed = dict(fake_tmux["sent"])
    assert typed[c1["tmux"]].startswith("claude --resume " + c1["claude_session_id"])
    rows = main.db.open_rows()
    assert rows[c1["tmux"]]["launcher"] == "recovered" and rows[c1["tmux"]]["claude_session_id"] == c1["claude_session_id"]
    st = client.get("/api/state", headers=H).json()
    assert sorted(st["last_recovery"]["value"]["recovered"]) == sorted([c1["tmux"], c2["tmux"]])
    assert client.post("/api/recovery/dismiss", headers=H).status_code == 200
    assert client.get("/api/state", headers=H).json()["last_recovery"] is None
    # second run: everything is live now -> nothing to do
    assert recover.run(main.db, main._start_session) == {"recovered": [], "closed": [], "skipped": [], "continue": []}


def test_disabled(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "recover", False)
    assert recover.run(None, None) == {"recovered": [], "closed": [], "skipped": [], "continue": []}


def test_task_session_recovers_in_worktree(client, projects_dir, fake_tmux, monkeypatch, tmp_path):
    from app import main
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)
    t = client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": "Fix bug", "prompt": "fix"}).json()
    wt = projects_dir / "shop" / "api" / ".claude" / "worktrees" / "fix-bug"
    wt.mkdir(parents=True)
    fake_tmux["sessions"].clear(); fake_tmux["created"].clear()
    summary = recover.run(main.db, main._start_session)
    assert summary["recovered"] == [t["tmux"]]
    name, cwd, env = fake_tmux["created"][-1]
    assert cwd == str(wt) and dict(fake_tmux["sent"])[name].startswith("claude --resume ")
    assert main.db.open_rows()[name]["launcher"] == "task"
    # a task whose worktree never appeared is closed, not relaunched
    t2 = client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": "Other", "prompt": "x"}).json()
    fake_tmux["sessions"].clear()
    summary = recover.run(main.db, main._start_session)
    assert t2["tmux"] in summary["closed"]


def test_working_session_is_marked_to_continue_after_the_relaunch(client, projects_dir, fake_tmux, monkeypatch):
    """A power cut mid-turn: the relaunched row carries continue_after_resume; once its SessionStart arrives and it sits
    idle, the autoresume tick types `continue` once. An idle row before the reboot is relaunched without the flag."""
    import time
    from app import autoresume, hooks, main
    from app.config import settings
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(settings, "auto_continue", True)
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)
    busy = client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "claude"}).json()
    idle = client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "claude"}).json()
    main.db.set_state(busy["tmux"], "working", "UserPromptSubmit", prompt="finish the migration and run the tests")
    main.db.set_state(idle["tmux"], "idle", "SessionStart")
    assert recover.wants_continue(main.db.open_rows()[busy["tmux"]]) and not recover.wants_continue(main.db.open_rows()[idle["tmux"]])
    fake_tmux["sessions"].clear(); fake_tmux["created"].clear()
    summary = recover.run(main.db, main._start_session)
    assert sorted(summary["recovered"]) == sorted([busy["tmux"], idle["tmux"]]) and summary["continue"] == [busy["tmux"]]
    rows = main.db.open_rows()
    flag = rows[busy["tmux"]]["flags"][recover.CONTINUE_FLAG]
    assert flag["reason"] == "reboot" and flag["prompt"].startswith("finish the migration")
    assert recover.CONTINUE_FLAG not in rows[idle["tmux"]]["flags"]
    assert client.get("/api/state", headers=H).json()["last_recovery"]["value"]["continue"] == [busy["tmux"]]
    # the resumed session comes back and reports SessionStart; nothing is typed until it has settled, then exactly once
    sent = []
    tick = lambda now: autoresume.tick(main.db, now, send=lambda n, t: sent.append((n, t)), clients=lambda n: 0, alive=lambda n: True)
    assert tick(time.time() + 1) == [] and sent == []
    hooks.apply(main.db, busy["tmux"], "SessionStart", {"source": "resume", "session_id": busy["claude_session_id"]})
    assert tick(time.time() + 1) == [], "settle first"
    assert tick(time.time() + autoresume.RESUME_SETTLE + 1) == [], "no statusline yet: wait for the prompt to be drawn"
    hooks.apply(main.db, busy["tmux"], "statusline", {"session_id": busy["claude_session_id"], "model": {"display_name": "Opus"},
                                                      "context_window": {"used_percentage": 2, "context_window_size": 200000}})
    assert tick(time.time() + autoresume.RESUME_SETTLE + 1) == [busy["tmux"]]
    assert sent == [(busy["tmux"], "continue")]
    assert recover.CONTINUE_FLAG not in main.db.open_rows()[busy["tmux"]]["flags"]
    assert tick(time.time() + 600) == [] and len(sent) == 1


def test_a_row_working_for_days_is_not_continued(monkeypatch):
    """A Stop hook that never arrived leaves a row 'working' for days; relaunching that is fine, nudging it is not."""
    import time
    from datetime import datetime, timezone
    old = datetime.fromtimestamp(time.time() - 3 * 86400, tz=timezone.utc).isoformat(timespec="seconds")
    assert not recover.wants_continue({"state": "working", "agent": "claude", "state_at": old, "flags": {}})
    fresh = datetime.fromtimestamp(time.time() - 600, tz=timezone.utc).isoformat(timespec="seconds")
    assert recover.wants_continue({"state": "working", "agent": "claude", "state_at": fresh, "flags": {}})
    assert not recover.wants_continue({"state": "working", "agent": "shell", "state_at": fresh, "flags": {}})
    assert not recover.wants_continue({"state": "working", "agent": "claude", "state_at": fresh, "flags": {"no_autoresume": True}})
    assert not recover.wants_continue({"state": "waiting", "agent": "claude", "state_at": fresh, "flags": {}})


# ---------------------------------------------------------------- Codex rows (v0.5.11): launch, resume, recover, tasks
#
# No real codex runs: conftest's fake_codex answers the adapter's probes like 0.145.0, tmux is the fake, the repos are real git repos
# with one commit (a managed worktree needs a commit to branch from).

import json
import shlex
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import hooks
from app.config import settings

CX1 = "0198aaaa-bbbb-7ccc-8ddd-eeeeeeeeeee1"
CX2 = "0198aaaa-bbbb-7ccc-8ddd-eeeeeeeeeee2"
BYPASS = "--dangerously-bypass-approvals-and-sandbox"


def commit_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@e.x", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"],
                   check=True)
    return path


@pytest.fixture
def cx(lite_client, projects_dir, fake_tmux, fake_codex, monkeypatch):
    """The board without workers, the fake tmux, a fake codex (and claude) binary and shop/api + shop/web as committed git repos."""
    from app import main
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    commit_repo(projects_dir / "shop" / "api")
    commit_repo(projects_dir / "shop" / "web")
    main._invalidate_scan()
    yield SimpleNamespace(client=lite_client, tmux=fake_tmux, projects=projects_dir, api=projects_dir / "shop" / "api", db=lambda: main.db)
    main._invalidate_scan()


def new_cx(cx, **body):
    r = cx.client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "codex", **body})
    assert r.status_code == 201, r.text
    return r.json()


def argv_of(resp):
    return shlex.split(resp["cmd"])


def hook(cx, name, event, payload, agent="codex", extra=None):
    hdr = {"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard-Session": name, "X-CCBoard-Agent": agent, **(extra or {})}
    r = cx.client.post("/api/hook", headers=hdr, content=json.dumps({"hook_event_name": event, **payload}))
    assert r.status_code == 200, r.text
    return r.json()


def row(cx, name):
    return cx.db().open_rows()[name]


def test_a_codex_session_launches_through_the_adapter_and_is_stored_as_codex(cx):
    r = new_cx(cx, name="cx", model="gpt-5.5", effort="high", permission_mode="dontAsk")
    assert r["agent"] == "codex" and r["tmux"] == "shop--api--cx" and r["agent_session_id"] is None and r["claude_session_id"] is None
    argv = argv_of(r)
    assert argv[0] == "codex" and "--no-alt-screen" in argv and argv[argv.index("-s") + 1] == "workspace-write"
    assert argv[argv.index("-a") + 1] == "never" and argv[argv.index("-m") + 1] == "gpt-5.5"
    assert 'model_reasoning_effort="high"' in argv and argv[argv.index("-c") + 1] == 'model_reasoning_effort="high"'
    assert "--no-daemon" not in argv and "--dangerously-bypass-hook-trust" not in argv and BYPASS not in argv and "--" not in argv
    name, cwd, env = cx.tmux["created"][-1]
    assert name == r["tmux"] and cwd == str(cx.api) and env["CCBOARD_AGENT"] == "codex" and env["CCBOARD_SESSION"] == name
    assert dict(cx.tmux["sent"])[name] == r["cmd"] and cx.tmux["sessions"][name]["command"] == "codex"
    row_ = row(cx, name)
    assert row_["agent"] == "codex" and row_["launcher"] == "claude" and row_["claude_session_id"] is None and row_["cwd"] == str(cx.api)
    assert row_["opts"] == {"model": "gpt-5.5", "reasoning_effort": "high", "permission_mode": "dontAsk"}
    st = cx.client.get("/api/state", headers=H).json()
    mine = next(s for p in st["projects"] for rp in p["repos"] for s in rp["sessions"] if s["tmux"] == name)
    assert mine["agent"] == "codex"


def test_codex_options_ride_in_opts_and_extra_dirs_in_add_dirs(cx):
    r = new_cx(cx, name="adv", model="gpt-5.5", opts={"sandbox": "read-only", "approval": "untrusted", "search": True, "profile": "work",
                                                      "reasoning_effort": "xhigh"}, add_dirs=["shop/web"], args="--verbose")
    argv = argv_of(r)
    assert argv[argv.index("-s") + 1] == "read-only" and argv[argv.index("-a") + 1] == "untrusted" and "--search" in argv
    assert argv[argv.index("-p") + 1] == "work" and 'model_reasoning_effort="xhigh"' in argv and "--verbose" in argv
    assert argv[argv.index("--add-dir") + 1] == str(cx.projects / "shop" / "web")
    got = row(cx, r["tmux"])
    assert got["opts"] == {"model": "gpt-5.5", "reasoning_effort": "xhigh", "sandbox": "read-only", "approval": "untrusted", "search": True,
                           "profile": "work"} and got["add_dirs"] == [str(cx.projects / "shop" / "web")]
    # the stored options and the extra directories come back on the relaunch (never the one-off extra args)
    cx.tmux["sessions"].clear()
    cmd = recover.plan(cx.db().open_rows(), set())[0]["cmd"]
    assert cmd[:2] == ["codex", "resume"] and cmd[cmd.index("-s") + 1] == "read-only" and "--add-dir" in cmd and "--verbose" not in cmd
    # approval never with an explicit sandbox is Claude's dontAsk: an ordinary interactive choice, stored as chosen
    r2 = new_cx(cx, name="open", opts={"sandbox": "workspace-write", "approval": "never"})
    assert argv_of(r2)[argv_of(r2).index("-s") + 1] == "workspace-write" and argv_of(r2)[argv_of(r2).index("-a") + 1] == "never"
    assert row(cx, r2["tmux"])["opts"] == {"sandbox": "workspace-write", "approval": "never"}


def test_the_agent_field_and_the_codex_resume_and_continue_launchers(cx):
    r = cx.client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "claude", "agent": "codex", "name": "a1"}).json()
    assert r["agent"] == "codex" and argv_of(r)[0] == "codex"
    r = cx.client.post("/api/projects/shop/repos/api/sessions", headers=H,
                       json={"launcher": "codex-resume", "resume_id": CX1, "name": "r1"}).json()
    argv = argv_of(r)
    assert argv[:2] == ["codex", "resume"] and argv[-1] == CX1 and r["agent_session_id"] == CX1
    assert row(cx, r["tmux"])["claude_session_id"] == CX1 and row(cx, r["tmux"])["launcher"] == "resume"
    r = cx.client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "codex-continue", "name": "c1"}).json()
    argv = argv_of(r)
    assert argv[:2] == ["codex", "resume"] and argv[-1] == "--last" and r["agent_session_id"] is None
    assert row(cx, r["tmux"])["launcher"] == "continue"
    # a launcher of one agent cannot be pointed at another
    for body in ({"launcher": "codex", "agent": "claude"}, {"launcher": "shell", "agent": "codex"}, {"launcher": "claude", "agent": "gemini"},
                 {"launcher": "codex-resume", "resume_id": "not-a-uuid"}):
        before = len(cx.tmux["created"])
        assert cx.client.post("/api/projects/shop/repos/api/sessions", headers=H, json=body).status_code == 400, body
        assert len(cx.tmux["created"]) == before


def test_codex_danger_full_access_is_bypass_class_for_sessions(cx):
    """Decision of the v0.5.11 review: an interactive sandbox=danger-full-access needs the same acknowledgement as bypassPermissions
    (bypass: true), and with it the one bypass flag is what runs. approval=never with an explicit sandbox stays an ordinary choice."""
    for opts in ({"sandbox": "danger-full-access"}, {"sandbox": "danger-full-access", "approval": "never"},
                 {"sandbox": "danger-full-access", "permission_mode": "plan"}):
        r = cx.client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "codex", "name": "d1", "opts": opts})
        assert r.status_code == 400 and "danger-full-access" in r.json()["error"] and "bypass" in r.json()["error"], r.text
        assert cx.tmux["created"] == [] and cx.db().open_rows() == {}
    r = cx.client.post("/api/projects/shop/repos/api/sessions", headers=H,
                       json={"launcher": "codex-resume", "resume_id": CX1, "name": "d2", "opts": {"sandbox": "danger-full-access"}})
    assert r.status_code == 400 and cx.tmux["created"] == []
    ok = new_cx(cx, name="d3", bypass=True, model="gpt-5.5", opts={"sandbox": "danger-full-access", "approval": "never"})
    argv = argv_of(ok)
    assert BYPASS in argv and "-s" not in argv and "-a" not in argv and "danger-full-access" not in argv, "the acknowledged bypass stands alone"
    assert row(cx, ok["tmux"])["opts"] == {"model": "gpt-5.5", "approval": "never"}, "the sandbox choice is never stored"
    fine = new_cx(cx, name="d4", opts={"sandbox": "read-only", "approval": "never"})
    assert argv_of(fine)[argv_of(fine).index("-s") + 1] == "read-only" and BYPASS not in argv_of(fine)


def test_codex_bypass_is_an_interactive_choice_and_is_never_stored(cx):
    r = new_cx(cx, name="by", bypass=True, model="gpt-5.5")
    argv = argv_of(r)
    assert BYPASS in argv and "-s" not in argv and "-a" not in argv, "the bypass flag stands alone"
    assert row(cx, r["tmux"])["opts"] == {"model": "gpt-5.5"}
    r2 = new_cx(cx, name="by2", permission_mode="bypassPermissions")
    assert BYPASS in argv_of(r2) and "permission_mode" not in row(cx, r2["tmux"])["opts"]
    # reboot: the relaunch has no bypass either (two unbound rows share the directory, so each is a fresh codex, never `resume --last`)
    cx.tmux["sessions"].clear()
    todo = {t["name"]: t["cmd"] for t in recover.plan(cx.db().open_rows(), set())}
    assert todo[r["tmux"]][0] == "codex" and "resume" not in todo[r["tmux"]] and all(BYPASS not in c for c in todo.values())


@pytest.mark.parametrize("body,needle", [
    ({"allowed_tools": "Bash(git *)"}, "allowed_tools: not supported by codex"),
    ({"append_system_prompt": "be brief"}, "append_system_prompt: not supported by codex"),
    ({"args": "-c model=x"}, "-c"),
    ({"args": "--profile work"}, "--profile"),
    ({"args": "--yolo"}, "--yolo"),
    ({"args": BYPASS}, BYPASS),
    ({"devcontainer": True}, "devcontainer"),
    ({"effort": "extreme"}, "reasoning_effort must be one of"),
    ({"permission_mode": "everything"}, "permission_mode must be one of"),
])
def test_codex_launch_refusals_start_nothing(cx, body, needle):
    r = cx.client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "codex", **body})
    assert r.status_code == 400 and needle in r.json()["error"], r.text
    assert cx.tmux["created"] == [] and cx.db().open_rows() == {}


def test_a_codex_row_is_recovered_with_codex_resume_by_id_or_last(cx):
    bound = new_cx(cx, name="bound", model="gpt-5.5", permission_mode="plan")
    task_less = cx.client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "claude", "name": "cl"}).json()
    web = cx.client.post("/api/projects/shop/repos/web/sessions", headers=H, json={"launcher": "codex", "name": "loose"}).json()
    hook(cx, bound["tmux"], "SessionStart", {"session_id": CX1, "source": "startup", "cwd": str(cx.api)})
    assert row(cx, bound["tmux"])["claude_session_id"] == CX1
    cx.tmux["sessions"].clear()
    cx.tmux["created"].clear()
    todo = {t["name"]: t for t in recover.plan(cx.db().open_rows(), set())}
    cmd = todo[bound["tmux"]]["cmd"]
    assert cmd[:2] == ["codex", "resume"] and cmd[-1] == CX1 and "-m" in cmd and cmd[cmd.index("-s") + 1] == "read-only", cmd
    # an unbound Codex row that is the only one of its directory (shop/web): `resume --last` there can only be its own conversation
    assert todo[web["tmux"]]["cmd"][:2] == ["codex", "resume"] and todo[web["tmux"]]["cmd"][-1] == "--last" and not todo[web["tmux"]]["fresh"]
    assert todo[task_less["tmux"]]["cmd"][0] == "claude", "a Claude row next to them still resumes with claude"
    summary = recover.run(cx.db(), main_start_session())
    assert sorted(summary["recovered"]) == sorted([bound["tmux"], web["tmux"], task_less["tmux"]]) and summary["skipped"] == []
    assert "notes" not in summary, "nothing was guessed"
    typed = dict(cx.tmux["sent"])
    assert typed[bound["tmux"]].startswith(f"codex resume ") and typed[bound["tmux"]].endswith(CX1)
    assert typed[web["tmux"]].endswith("--last")
    envs = {n: env for n, _c, env in cx.tmux["created"]}
    assert envs[bound["tmux"]]["CCBOARD_AGENT"] == "codex" and envs[task_less["tmux"]]["CCBOARD_AGENT"] == "claude"
    new_row = row(cx, bound["tmux"])
    assert new_row["agent"] == "codex" and new_row["launcher"] == "recovered" and new_row["claude_session_id"] == CX1
    assert new_row["opts"] == {"model": "gpt-5.5", "permission_mode": "plan"}
    assert cx.tmux["sessions"][bound["tmux"]]["command"] == "codex"


def main_start_session():
    from app import main
    return main._start_session


def test_an_unbound_codex_session_row_resumes_last_only_when_it_is_alone_in_its_directory(cx):
    """Defect 8: before the first /hooks trust no Codex row has a conversation id, and `codex resume --last` filters by directory only.
    Two rows in one repo (or a bound row next to an unbound one) could resume each other's thread: those get a fresh codex."""
    a = new_cx(cx, name="one", model="gpt-5.5", permission_mode="plan")
    b = new_cx(cx, name="two")
    web = cx.client.post("/api/projects/shop/repos/web/sessions", headers=H, json={"launcher": "codex", "name": "solo"}).json()
    for r in (a, b, web):
        with cx.db().lock:                                  # state working a minute ago: a reboot interrupted a turn
            cx.db().conn.execute("UPDATE sessions SET state='working', state_at=? WHERE tmux_name=?",
                                 (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60)), r["tmux"]))
    cx.tmux["sessions"].clear()
    cx.tmux["created"].clear()
    todo = {t["name"]: t for t in recover.plan(cx.db().open_rows(), set())}
    for r in (a, b):
        t = todo[r["tmux"]]
        assert t["fresh"] is True and t["cmd"][0] == "codex" and "resume" not in t["cmd"] and "--last" not in t["cmd"], t["cmd"]
        assert "fresh codex" in t["note"] and "resume --last" in t["note"]
    assert "-m" in todo[a["tmux"]]["cmd"] and todo[a["tmux"]]["cmd"][todo[a["tmux"]]["cmd"].index("-s") + 1] == "read-only", "stored options still apply"
    assert todo[web["tmux"]]["cmd"][-1] == "--last" and not todo[web["tmux"]]["fresh"] and todo[web["tmux"]]["note"] is None
    summary = recover.run(cx.db(), main_start_session())
    assert sorted(summary["recovered"]) == sorted([a["tmux"], b["tmux"], web["tmux"]]) and summary["skipped"] == []
    assert sorted(n.split(":")[0] for n in summary["notes"]) == sorted([a["tmux"], b["tmux"]]) and all("fresh codex" in n for n in summary["notes"])
    assert cx.db().kv_get("last_recovery")["value"]["notes"] == summary["notes"], "the note is kept with the last recovery"
    typed = dict(cx.tmux["sent"])
    assert not typed[a["tmux"]].endswith("--last") and "resume" not in typed[a["tmux"]] and typed[web["tmux"]].endswith("--last")
    # a fresh codex has no turn to continue: `continue` is not typed into it; the lone row's resumed conversation still gets it
    assert summary["continue"] == [web["tmux"]], summary["continue"]
    assert not row(cx, a["tmux"])["flags"].get(recover.CONTINUE_FLAG) and not row(cx, b["tmux"])["flags"].get(recover.CONTINUE_FLAG)


def test_a_bound_row_and_an_unbound_row_in_one_directory_do_not_resume_each_other(cx):
    bound = new_cx(cx, name="bound")
    loose = new_cx(cx, name="loose")
    hook(cx, bound["tmux"], "SessionStart", {"session_id": CX1, "source": "startup"})
    cx.tmux["sessions"].clear()
    todo = {t["name"]: t for t in recover.plan(cx.db().open_rows(), set())}
    assert todo[bound["tmux"]]["cmd"][-1] == CX1, "a bound row resumes its own id whatever shares the directory"
    assert todo[loose["tmux"]]["fresh"] and "resume" not in todo[loose["tmux"]]["cmd"]
    # the bound row's live session in the same directory counts as well (it is still an open Codex row there)
    todo = {t["name"]: t for t in recover.plan(cx.db().open_rows(), {bound["tmux"]})}
    assert list(todo) == [loose["tmux"]] and todo[loose["tmux"]]["fresh"]


def test_a_lone_unbound_row_stays_resume_last_and_an_agent_that_is_not_codex_is_unaffected(cx):
    one = new_cx(cx, name="alone")
    cl = cx.client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "claude", "name": "cl"}).json()
    cx.tmux["sessions"].clear()
    todo = {t["name"]: t for t in recover.plan(cx.db().open_rows(), set())}
    assert todo[one["tmux"]]["cmd"][-1] == "--last" and todo[one["tmux"]]["cmd"][:2] == ["codex", "resume"]
    assert todo[cl["tmux"]]["cmd"][0] == "claude" and not todo[cl["tmux"]]["fresh"], "a Claude row next to a Codex one is a different matter"


def test_codex_task_rows_keep_resume_last_in_their_own_worktrees_even_with_neighbours(cx):
    a = post_task(cx, title="First job").json()
    b = post_task(cx, title="Second job").json()
    plain = new_cx(cx, name="plain")                                  # an unbound session in the repo's own directory
    cx.tmux["sessions"].clear()
    todo = {t["name"]: t for t in recover.plan(cx.db().open_rows(), set(), {x["tmux"]: str(cx.api / ".ccboard" / "worktrees" / x["slug"])
                                                                          for x in (a, b)})}
    assert todo[a["tmux"]]["cmd"][-1] == "--last" and todo[b["tmux"]]["cmd"][-1] == "--last" and not todo[a["tmux"]]["fresh"]
    assert todo[plain["tmux"]]["cmd"][-1] == "--last", "the session row is alone in the repo directory: the tasks run in their worktrees"


def test_a_stored_task_row_without_a_permission_mode_is_relaunched_with_both_sandbox_and_approval(cx):
    """Defect 1 on the recovery side: a task row written before the default existed (or edited by hand) must not come back with only -a."""
    t = post_task(cx, opts={"approval": "never"}).json()
    with cx.db().lock:
        cx.db().conn.execute("UPDATE sessions SET opts=? WHERE tmux_name=?", (json.dumps({"approval": "never"}), t["tmux"]))
    cx.tmux["sessions"].clear()
    cx.tmux["created"].clear()
    summary = recover.run(cx.db(), main_start_session())
    assert summary["recovered"] == [t["tmux"]]
    argv = shlex.split(dict(cx.tmux["sent"])[t["tmux"]])
    assert argv[:2] == ["codex", "resume"] and argv[-1] == "--last"
    assert argv[argv.index("-s") + 1] == "workspace-write" and argv[argv.index("-a") + 1] == "never", argv
    assert row(cx, t["tmux"])["opts"] == {"approval": "never", "permission_mode": "default"}


def test_recovery_drops_a_bypass_from_hand_edited_codex_opts(cx):
    r = new_cx(cx, name="sneaky")
    with cx.db().lock:                      # a stored row nobody should be able to make: the adapter never stores these
        cx.db().conn.execute("UPDATE sessions SET opts=? WHERE tmux_name=?",
                             (json.dumps({"model": "gpt-5.5", "permission_mode": "bypassPermissions", "sandbox": "danger-full-access",
                                          "bypass": True}), r["tmux"]))
    todo = recover.plan(cx.db().open_rows(), set())
    cmd = todo[0]["cmd"]
    assert BYPASS not in cmd and "danger-full-access" not in cmd and "-m" in cmd and todo[0]["opts"] == {"model": "gpt-5.5"}
    cx.tmux["sessions"].clear()                               # run() stores the sanitised opts on the relaunched row
    recover.run(cx.db(), main_start_session())
    assert row(cx, r["tmux"])["opts"] == {"model": "gpt-5.5"}


def test_system_prompt_text_is_not_mistaken_for_a_bypass_when_recovering(cx):
    """recover reads option NAMES and the permission values, never free text: a Claude row whose appended prompt says 'dangerously'
    keeps its option on the relaunch."""
    c = cx.client.post("/api/projects/shop/repos/api/sessions", headers=H,
                       json={"launcher": "claude", "name": "txt", "append_system_prompt": "never run things dangerously"}).json()
    cx.tmux["sessions"].clear()
    todo = recover.plan(cx.db().open_rows(), set())
    assert "--append-system-prompt" in todo[0]["cmd"] and todo[0]["opts"]["append_system_prompt"] == "never run things dangerously"
    assert c["tmux"] == todo[0]["name"]


def post_task(cx, **body):
    body = {"project": "shop", "repo": "api", "title": "Add login page", "prompt": "Add a login page with tests.", "agent": "codex", **body}
    return cx.client.post("/api/tasks", headers=H, json=body)


def branches(path: Path) -> set[str]:
    out = subprocess.run(["git", "-C", str(path), "branch", "--format=%(refname:short)"], capture_output=True, text=True, check=True).stdout
    return set(out.split())


def test_a_codex_task_gets_a_managed_worktree_and_starts_in_it(cx):
    (cx.api / ".gitignore").write_text(".env\n")
    (cx.api / ".worktreeinclude").write_text(".env\n")
    (cx.api / ".env").write_text("TOKEN=abc\n")
    r = post_task(cx, model="gpt-5.5", effort="low")
    assert r.status_code == 201, r.text
    body = r.json()
    wt = cx.api / ".ccboard" / "worktrees" / "add-login-page"
    assert body["branch"] == "worktree-add-login-page" and body["tmux"] == "shop--api--t-add-login-page" and body["phase"] == "running"
    assert wt.is_dir() and (wt / ".git").exists() and "worktree-add-login-page" in branches(cx.api)
    assert (wt / ".env").read_text() == "TOKEN=abc\n", ".worktreeinclude files are copied in"
    assert not (cx.api / ".claude" / "worktrees" / "add-login-page").exists()
    name, cwd, env = cx.tmux["created"][-1]
    assert cwd == str(wt) and env["CCBOARD_AGENT"] == "codex"
    argv = shlex.split(dict(cx.tmux["sent"])[name])
    assert argv[0] == "codex" and argv[-2:] == ["--", "Add a login page with tests."] and "-C" not in argv, argv
    assert BYPASS not in argv and "-m" in argv and 'model_reasoning_effort="low"' in argv
    assert argv[argv.index("-s") + 1] == "workspace-write" and argv[argv.index("-a") + 1] == "on-request", \
        "a task with no permission choice does not inherit config.toml's (possibly full-access) defaults"
    t = cx.db().task_get(body["id"])
    assert t["agent"] == "codex" and t["worktree"] == str(wt) and t["branch"] == "worktree-add-login-page" and t["mode"] == "worktree"
    assert t["claude_session_id"] is None and t["base"] == "main"
    assert row(cx, name)["opts"] == {"model": "gpt-5.5", "reasoning_effort": "low", "permission_mode": "default"}
    row_ = row(cx, name)
    assert row_["agent"] == "codex" and row_["launcher"] == "task" and row_["cwd"] == str(wt)
    assert (cx.api / ".git" / "info" / "exclude").read_text().count(".ccboard/worktrees/") == 1
    state_task = next(x for x in cx.client.get("/api/state", headers=H).json()["tasks"] if x["id"] == body["id"])
    assert state_task["agent"] == "codex" and state_task["column"] == "in_progress"
    # the hooks of the task's session come from inside the worktree, with no env header: the cwd finds the row
    out = hook(cx, "", "SessionStart", {"session_id": CX1, "cwd": str(wt), "source": "startup"}, extra={"X-CCBoard-Session": ""})
    assert out["session"] == name and out["how"] == "cwd"


@pytest.mark.parametrize("body,sandbox,approval", [
    ({}, "workspace-write", "on-request"),
    ({"opts": {"approval": "never"}}, "workspace-write", "never"),                 # defect 1: this used to type `-a never` and no `-s` at all
    ({"opts": {"approval": "on-request"}}, "workspace-write", "on-request"),
    ({"opts": {"approval": "untrusted", "sandbox": "workspace-write"}}, "workspace-write", "untrusted"),
    ({"opts": {"sandbox": "read-only"}}, "read-only", "on-request"),               # and this `-s read-only` and no `-a`
    ({"opts": {"sandbox": "workspace-write"}}, "workspace-write", "on-request"),
    ({"opts": {"sandbox": "read-only", "approval": "never"}}, "read-only", "never"),
    ({"permission_mode": "dontAsk"}, "workspace-write", "never"),
    ({"permission_mode": "plan"}, "read-only", "on-request"),
])
def test_a_codex_task_always_types_both_a_sandbox_and_an_approval_policy(cx, body, sandbox, approval):
    r = post_task(cx, **body)
    assert r.status_code == 201, r.text
    argv = shlex.split(dict(cx.tmux["sent"])[r.json()["tmux"]])
    assert argv[argv.index("-s") + 1] == sandbox and argv[argv.index("-a") + 1] == approval, argv
    assert BYPASS not in argv and "danger-full-access" not in argv
    assert row(cx, r.json()["tmux"])["opts"]["permission_mode"] in ("default", "dontAsk", "plan"), "the stored options carry the mode too"


def test_a_backlog_codex_card_with_a_hand_edited_spec_still_gets_both_flags_at_dispatch(cx):
    tid = post_task(cx, when="later").json()["id"]
    for spec, sandbox, approval in (({"opts": {"approval": "never"}}, "workspace-write", "never"),
                                    ({"opts": {"sandbox": "read-only"}}, "read-only", "on-request")):
        cx.db().task_update(tid, spec=spec)
        d = cx.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={})
        assert d.status_code == 200, d.text
        argv = shlex.split(dict(cx.tmux["sent"])[d.json()["tmux"]])
        assert argv[argv.index("-s") + 1] == sandbox and argv[argv.index("-a") + 1] == approval, argv
        cx.client.delete(f"/api/tasks/{tid}", headers=H)
        tid = post_task(cx, when="later", title="Second card").json()["id"]
    for spec in ({"opts": {"sandbox": "danger-full-access"}}, {"opts": {"sandbox": "danger-full-access", "approval": "never"}}):
        cx.db().task_update(tid, spec=spec)
        assert cx.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={}).status_code == 400
        assert cx.db().task_get(tid)["phase"] == "backlog"


def test_a_codex_task_is_recovered_in_its_worktree_with_last_or_its_id(cx):
    a = post_task(cx, title="First job").json()
    b = post_task(cx, title="Second job").json()
    hook(cx, b["tmux"], "SessionStart", {"session_id": CX2, "source": "startup"})
    cx.tmux["sessions"].clear()
    cx.tmux["created"].clear()
    summary = recover.run(cx.db(), main_start_session())
    assert sorted(summary["recovered"]) == sorted([a["tmux"], b["tmux"]]), summary
    typed = dict(cx.tmux["sent"])
    assert typed[a["tmux"]].startswith("codex resume ") and typed[a["tmux"]].endswith("--last")
    assert typed[b["tmux"]].endswith(CX2)
    cwds = {n: c for n, c, _e in cx.tmux["created"]}
    assert cwds[a["tmux"]].endswith(".ccboard/worktrees/first-job") and cwds[b["tmux"]].endswith(".ccboard/worktrees/second-job")
    assert row(cx, a["tmux"])["launcher"] == "task" and row(cx, a["tmux"])["agent"] == "codex"
    assert all(BYPASS not in c for c in typed.values())
    assert cx.db().task_get(a["id"])["session_row"] == row(cx, a["tmux"])["id"], "the relaunch is the task's live session"
    # a task whose worktree is gone is closed, not relaunched
    cx.tmux["sessions"].clear()
    subprocess.run(["git", "-C", str(cx.api), "worktree", "remove", "--force", str(cx.api / ".ccboard" / "worktrees" / "first-job")], check=True)
    summary = recover.run(cx.db(), main_start_session())
    assert a["tmux"] in summary["closed"] and b["tmux"] in summary["recovered"]


@pytest.mark.parametrize("body", [
    {"permission_mode": "bypassPermissions"},
    {"args": BYPASS},
    {"args": "--yolo"},
    {"args": "-c sandbox_mode=danger-full-access"},
    {"opts": {"sandbox": "danger-full-access"}},
    {"opts": {"bypass": True}},
    {"opts": {"permission_mode": "bypassPermissions"}},
    {"opts": {"sandbox": "danger-full-access", "approval": "never"}},
    {"opts": {"approval": "never", "bypass": True}},
    {"permission_mode": "bypassPermissions", "opts": {"approval": "never"}},
    {"prompt": BYPASS + " and do it"},
])
def test_codex_bypass_is_refused_for_tasks_and_leaves_nothing_behind(cx, body):
    r = post_task(cx, **body)
    assert r.status_code == 400, r.text
    assert not (cx.api / ".ccboard" / "worktrees").exists() or not any((cx.api / ".ccboard" / "worktrees").iterdir())
    assert branches(cx.api) == {"main"} and cx.tmux["created"] == [] and cx.db().tasks() == []


def test_a_backlog_codex_card_keeps_its_agent_and_a_spec_with_bypass_is_refused_at_dispatch(cx):
    r = post_task(cx, when="later", model="gpt-5.5", opts={"search": True})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    t = cx.db().task_get(tid)
    assert t["agent"] == "codex" and t["tmux_name"] == "" and cx.tmux["created"] == []
    assert json.loads(t["spec"]) == {"model": "gpt-5.5", "opts": {"search": True}}
    cx.db().task_update(tid, spec={"permission_mode": "bypassPermissions"})              # a hand-edited card
    assert cx.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={}).status_code == 400
    assert cx.db().task_get(tid)["phase"] == "backlog" and branches(cx.api) == {"main"}
    cx.db().task_update(tid, spec={"model": "gpt-5.5", "opts": {"search": True}})
    d = cx.client.post(f"/api/tasks/{tid}/dispatch", headers=H, json={})
    assert d.status_code == 200, d.text
    argv = shlex.split(dict(cx.tmux["sent"])[d.json()["tmux"]])
    assert argv[0] == "codex" and "--search" in argv and argv[-2:] == ["--", "Add a login page with tests."]
    t = cx.db().task_get(tid)
    assert t["agent"] == "codex" and t["phase"] == "running" and (cx.api / ".ccboard" / "worktrees" / "add-login-page").is_dir()


def test_a_failed_codex_launch_takes_its_worktree_and_branch_with_it(cx, monkeypatch):
    from app import tmux

    def boom(name, text, pause=0.0):
        raise tmux.TmuxError("send-keys failed")
    monkeypatch.setattr(tmux, "send_line", boom)
    r = post_task(cx)
    assert r.status_code == 500 and "send-keys failed" in r.json()["error"]
    assert not (cx.api / ".ccboard" / "worktrees" / "add-login-page").exists() and branches(cx.api) == {"main"}
    assert cx.db().tasks() == [] and cx.db().open_rows() == {}


def test_a_codex_task_without_a_commit_is_a_git_error_not_a_crash(cx):
    bare = cx.projects / "shop" / "fresh"
    subprocess.run(["git", "-C", str(cx.projects / "shop"), "init", "-q", "-b", "main", "fresh"], check=True)       # no commit yet
    r = post_task(cx, repo="fresh")
    assert r.status_code == 422 and r.json()["error"], r.text
    assert cx.tmux["created"] == [] and cx.db().tasks() == [] and not (bare / ".ccboard" / "worktrees" / "add-login-page").exists()


def test_a_codex_task_on_a_project_folder_that_is_not_a_repo_runs_in_place(cx):
    folder = cx.projects / "plain"
    folder.mkdir()
    r = post_task(cx, project="plain", repo="root")
    assert r.status_code == 201, r.text
    body = r.json()
    name, cwd, _env = cx.tmux["created"][-1]
    assert cwd == str(folder) and body["branch"] == "" and not (folder / ".ccboard").exists()
    t = cx.db().task_get(body["id"])
    assert t["mode"] == "attached" and t["worktree"] == "" and t["agent"] == "codex"
    assert shlex.split(dict(cx.tmux["sent"])[name])[0] == "codex"
