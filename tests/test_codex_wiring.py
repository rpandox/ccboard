"""v0.5.11 slice C, the pieces around the Codex adapter that need no session: managed worktrees and .worktreeinclude (real git),
the scheduler's unattended-argument mirror, the doctor's codex group, the agent registry on the wire (GET /api/agents, state.agents,
GET /api/doctor), and the option mapping of a launch request. Launch, resume, recovery and tasks are in tests/test_recover.py; the
hook payloads in tests/test_hooks.py; the MCP passthrough in tests/test_mcp.py.

No real codex runs (conftest: _isolate_codex answers 'not installed' until fake_codex is asked for).
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from app import agents, doctor, scheduler, tasks
from app.agents import codex
from app.config import settings
from tests.conftest import write_fake_codex

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), "-c", "user.email=t@e.x", "-c", "user.name=t", *args], check=True, capture_output=True,
                          text=True).stdout


def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "-C", str(r), "init", "-q", "-b", "main"], check=True)
    (r / "README.md").write_text("hi\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


# ---------------------------------------------------------------- managed worktrees

def test_worktree_path_per_agent_and_both_folders_excluded(tmp_path):
    r = repo(tmp_path)
    assert tasks.worktree_path(r, "job") == r / ".claude" / "worktrees" / "job"
    assert tasks.worktree_path(r, "job", "claude") == r / ".claude" / "worktrees" / "job"
    assert tasks.worktree_path(r, "job", "codex") == r / ".ccboard" / "worktrees" / "job"
    assert tasks.worktree_path(r, "job", None) == r / ".claude" / "worktrees" / "job"
    tasks.ensure_excluded(r)
    tasks.ensure_excluded(r)
    ex = (r / ".git" / "info" / "exclude").read_text()
    assert ex.count(".ccboard/worktrees/") == 1 and ex.count(".claude/worktrees/") == 1
    assert git(r, "status", "--porcelain").strip() == ""
    # a slug is taken when either folder holds it
    (r / ".ccboard" / "worktrees" / "taken").mkdir(parents=True)
    assert tasks.unique_slug(r, "taken", set()) == "taken-2"


def test_create_managed_worktree_makes_the_branch_and_folder_claude_uses(tmp_path):
    r = repo(tmp_path)
    tasks.ensure_excluded(r)
    wt = tasks.create_managed_worktree(r, "fix-bug", "main")
    assert wt == r / ".ccboard" / "worktrees" / "fix-bug" and (wt / "README.md").read_text() == "hi\n"
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == "worktree-fix-bug"
    assert git(r, "status", "--porcelain").strip() == "", "the main checkout stays clean"
    assert tasks.default_branch(r) == "main"
    with pytest.raises(tasks.WorktreeError, match="already exists"):
        tasks.create_managed_worktree(r, "fix-bug", "main")
    with pytest.raises(tasks.WorktreeError):
        tasks.create_managed_worktree(r, "other", "no-such-branch")
    assert not (r / ".ccboard" / "worktrees" / "other").exists() and "worktree-other" not in git(r, "branch", "--format=%(refname:short)")
    # the existing remove path (archive, merge) takes it away again
    assert tasks.remove_worktree(r, "fix-bug", force=True, agent="codex") is None and not wt.exists()


def test_the_managed_worktree_branches_from_origin_when_there_is_one(tmp_path):
    r = repo(tmp_path)
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(r), str(bare)], check=True)
    git(r, "remote", "add", "origin", str(bare))
    git(r, "fetch", "-q", "origin")
    (r / "later.txt").write_text("local only\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "local commit not pushed")
    wt = tasks.create_managed_worktree(r, "job", "main")
    assert not (wt / "later.txt").exists(), "origin/main is the start point (what a PR diffs against)"
    assert git(wt, "log", "--oneline").count("\n") == 1


def test_discard_managed_worktree_removes_folder_and_branch_and_never_raises(tmp_path):
    r = repo(tmp_path)
    wt = tasks.create_managed_worktree(r, "gone", "main")
    tasks.discard_managed_worktree(r, "gone", wt)
    assert not wt.exists() and "worktree-gone" not in git(r, "branch", "--format=%(refname:short)")
    tasks.discard_managed_worktree(r, "gone", wt)                              # twice: nothing left to do, nothing raised
    tasks.discard_managed_worktree(tmp_path / "not-a-repo", "x", tmp_path / "not-a-repo" / "wt")


# ---------------------------------------------------------------- .worktreeinclude

def test_worktreeinclude_copies_ignored_files_that_match_and_nothing_else(tmp_path):
    r = repo(tmp_path)
    (r / ".gitignore").write_text(".env\n.env.*\nsecrets/\nnode_modules/\nbuild.log\n")
    (r / ".worktreeinclude").write_text("# local config the worktree needs\n.env\n.env.*\nsecrets/\nREADME.md\nnot-ignored.txt\n")
    (r / ".env").write_text("A=1\n")
    (r / ".env.local").write_text("B=2\n")
    (r / "secrets").mkdir()
    (r / "secrets" / "key.pem").write_text("k\n")
    (r / "secrets" / "deep").mkdir()
    (r / "secrets" / "deep" / "more.txt").write_text("m\n")
    (r / "node_modules").mkdir()
    (r / "node_modules" / "x.js").write_text("x\n")                    # ignored but not listed: stays out
    (r / "build.log").write_text("log\n")                              # ignored but not listed: stays out
    (r / "not-ignored.txt").write_text("n\n")                          # listed but not ignored: a worktree has the tracked files, not this
    git(r, "add", ".gitignore", ".worktreeinclude")
    git(r, "commit", "-q", "-m", "config")
    wt = tasks.create_managed_worktree(r, "inc", "main")
    copied = tasks.apply_worktreeinclude(r, wt)
    assert sorted(copied) == [".env", ".env.local", "secrets/deep/more.txt", "secrets/key.pem"]
    assert (wt / ".env").read_text() == "A=1\n" and (wt / "secrets" / "deep" / "more.txt").read_text() == "m\n"
    for stay in ("node_modules", "build.log", "not-ignored.txt"):
        assert not (wt / stay).exists(), stay
    assert (wt / "README.md").read_text() == "hi\n", "a tracked file named in the list is left to git"
    # a second pass copies nothing and overwrites nothing
    (wt / ".env").write_text("CHANGED=1\n")
    assert tasks.apply_worktreeinclude(r, wt) == [] and (wt / ".env").read_text() == "CHANGED=1\n"


def test_worktreeinclude_edge_cases(tmp_path):
    r = repo(tmp_path)
    wt = tasks.create_managed_worktree(r, "inc", "main")
    assert tasks.apply_worktreeinclude(r, wt) == [], "no .worktreeinclude: nothing to do"
    (r / ".gitignore").write_text(".env\nlink\nbig.bin\n")
    (r / ".worktreeinclude").write_text("")
    assert tasks.apply_worktreeinclude(r, wt) == [], "an empty list copies nothing"
    (r / ".worktreeinclude").write_text(".env\nlink\nbig.bin\n")
    (r / ".env").write_text("A=1\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    os.symlink(outside, r / "link")                                    # a symlink is never followed
    (r / "big.bin").write_bytes(b"x" * 1024)
    assert tasks.apply_worktreeinclude(r, wt) == [".env", "big.bin"] and not (wt / "link").exists()
    # the caps stop a runaway pattern
    (wt / ".env").unlink()
    (wt / "big.bin").unlink()
    old = (tasks.INCLUDE_MAX_BYTES, tasks.INCLUDE_MAX_FILES)
    try:
        tasks.INCLUDE_MAX_BYTES = 1000
        assert tasks.apply_worktreeinclude(r, wt) == [".env"], "big.bin (1 KiB) would pass the byte cap"
        (wt / ".env").unlink()
        tasks.INCLUDE_MAX_BYTES, tasks.INCLUDE_MAX_FILES = old[0], 1
        assert tasks.apply_worktreeinclude(r, wt) == [".env"]
    finally:
        tasks.INCLUDE_MAX_BYTES, tasks.INCLUDE_MAX_FILES = old
    # a repo that is not a repo, or a worktree path that does not exist: less is copied, nothing is raised
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / ".worktreeinclude").write_text(".env\n")
    assert tasks.apply_worktreeinclude(plain, plain / "nowhere") == []


# ---------------------------------------------------------------- scheduler: the unattended argument rules per agent

CODEX_BAD = ["-c", "--config", "-p", "--profile", "--enable", "--disable", "--strict-config", "--remote", "--remote-auth-token-env",
             "--yolo", "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust", "--bypass", "--profile=x", "-cfoo=1"]


def test_the_scheduler_mirror_agrees_with_the_codex_adapter():
    ag = agents.get("codex")
    assert scheduler.FORBIDDEN_ARG_PARTS_BY_AGENT["codex"] == codex.FORBIDDEN_ARG_PARTS == ("dangerously", "yolo", "bypass")
    assert scheduler.FORBIDDEN_ARG_PARTS_BY_AGENT["claude"] == scheduler.FORBIDDEN_ARG_PARTS
    for tok in CODEX_BAD:                                       # the plan's list: both the mirror and the adapter refuse each one
        assert scheduler.forbidden_arg([tok], "codex") == tok, tok
        assert ag.forbidden_extra([tok], interactive=False) == tok, tok
    for tok in ("--model", "-s", "--sandbox", "--add-dir", "-C"):  # flags the adapter owns on top of that list: it refuses them, the mirror need not
        assert ag.forbidden_extra([tok], interactive=False) == tok, tok
    for ok in ("--verbose", "hello", "gpt-5.5", "--timeout=5"):
        assert scheduler.forbidden_arg([ok], "codex") is None and ag.forbidden_extra([ok], interactive=False) is None, ok
    # claude's rules are untouched by the codex ones: -c and --profile are fine for Claude's args, --permission-mode is not
    assert scheduler.forbidden_arg(["-c"], "claude") is None and scheduler.forbidden_arg(["--profile"], "claude") is None
    assert scheduler.forbidden_arg(["--permission-mode", "plan"], "claude") == "--permission-mode"
    assert scheduler.forbidden_arg(["--model", "opus", "--DangerouslySkip"], "claude") == "--DangerouslySkip"
    assert scheduler.forbidden_arg(["--yolo"], "gemini") is None, "an agent with no list of its own is read with claude's"


def test_check_extra_args_is_agent_aware():
    assert scheduler.check_extra_args("--model opus") == ["--model", "opus"]
    assert scheduler.check_extra_args("--verbose", "codex") == ["--verbose"]
    with pytest.raises(ValueError, match="not allowed for unattended runs: -c"):
        scheduler.check_extra_args("-c model=x", "codex")
    with pytest.raises(ValueError, match="--yolo"):
        scheduler.check_extra_args("--yolo", "codex")
    assert scheduler.check_extra_args("-c model=x") == ["-c", "model=x"], "-c means nothing to Claude's headless runs"
    with pytest.raises(ValueError, match="--permission-mode"):
        scheduler.check_extra_args("--permission-mode bypassPermissions")
    with pytest.raises(KeyError):
        scheduler.check_extra_args("--x", "gemini")


# ---------------------------------------------------------------- doctor

def test_the_codex_group_is_registered_at_import_and_delegates_to_the_adapter(monkeypatch):
    # (the doctor test module pins the registry per test; here the import-time state is read straight off the module)
    assert any(p[0] == "codex" and p[1] == "codex" for p in doctor.PROVIDERS) and "codex" in doctor.GROUPS
    out = doctor.codex_checks(None)
    assert [c.id for c in out][:1] == ["codex-bin"] and all(c.status == "skip" for c in out), "no binary: every check is a skip"
    assert all(c.group == "codex" for c in out)
    seen = []
    fake = type("A", (), {"doctor_checks": lambda self: seen.append(1) or [doctor.Check("codex-bin", "codex", "Codex", "pass", "ok")]})()
    monkeypatch.setitem(agents._AGENTS, "codex", fake)
    assert [c.status for c in doctor.codex_checks(None)] == ["pass"] and seen == [1]
    monkeypatch.delitem(agents._AGENTS, "codex")
    assert doctor.codex_checks(None) == [], "a build without the adapter has nothing to say"


def test_the_codex_group_over_http_with_a_fake_codex(lite_client, projects_dir, fake_tmux, fake_codex, monkeypatch):
    doctor.invalidate()
    r = lite_client.get("/api/doctor?group=codex&refresh=1", headers=H)
    assert r.status_code == 200, r.text
    by = {c["id"]: c for c in r.json()["checks"]}
    assert by["codex-bin"]["status"] == "pass" and "0.145.0" in by["codex-bin"]["detail"], "0.145 works: informational, not a warning"
    assert "works; 0.157+ adds --approve-for-me and --no-daemon" in by["codex-bin"]["detail"] and not by["codex-bin"].get("fix")
    assert by["codex-auth"]["status"] == "pass" and by["codex-alt-screen"]["status"] == "pass"
    assert by["codex-hooks"]["status"] == "fail" and by["codex-hooks"]["fix"]["cmd"].startswith("python3 scripts/codex_hooks.py install")
    doctor.invalidate()


# ---------------------------------------------------------------- the registry on the wire

def test_agents_endpoint_lists_codex_with_its_schema(lite_client, projects_dir, fake_tmux, fake_codex):
    body = lite_client.get("/api/agents", headers=H).json()["agents"]
    assert list(body) == ["claude", "codex"]
    c = body["codex"]
    assert (c["name"], c["label"], c["glyph"]) == ("codex", "Codex", "◇") and c["installed"] is True and c["version"] == "0.145.0"
    assert c["auth"]["loggedIn"] is True and c["auth"]["authMethod"] == "chatgpt" and c["hooks"]["installed"] is False
    keys = {o["key"] for o in c["options"]}
    assert {"model", "reasoning_effort", "permission_mode", "bypass", "sandbox", "approval", "search", "add_dirs", "profile", "extra"} <= keys
    assert next(o for o in c["options"] if o["key"] == "bypass")["danger"] is True
    assert "bypassPermissions" in c["permission_modes"] and c["models"], "the model list comes from the catalogue or the static fallback"
    blob = json.dumps(body).lower()
    assert "auth.json" not in blob and "access_token" not in blob and "refresh_token" not in blob


def test_state_agents_codex_is_the_adapters_summary(lite_client, projects_dir, fake_tmux, fake_codex, codex_home):
    st = lite_client.get("/api/state", headers=H).json()
    assert list(st["agents"]) == ["claude", "codex"]
    assert st["agents"]["codex"] == {"installed": True, "version": "0.145.0", "loggedIn": True, "authMethod": "chatgpt", "glyph": "◇",
                                     "hooks": {"installed": False, "trust": "review"}}
    # a codex that is installed but logged out; hooks installed afterwards show on the next poll (the file is read each time)
    write_fake_codex(fake_codex, login="Not logged in")
    codex.reset_caches()
    assert lite_client.get("/api/state", headers=H).json()["agents"]["codex"]["loggedIn"] is False
    agents.get("codex").install_hooks(Path(__file__).resolve().parents[1], remote_approve=True, approve_timeout=90)
    assert lite_client.get("/api/state", headers=H).json()["agents"]["codex"]["hooks"] == {"installed": True, "trust": "review"}
    assert (codex_home / "hooks.json").is_file()
    assert "access_token" not in json.dumps(lite_client.get("/api/state", headers=H).json())


def test_state_agents_without_codex_is_not_installed(lite_client, projects_dir, fake_tmux):
    a = lite_client.get("/api/state", headers=H).json()["agents"]["codex"]
    assert a["installed"] is False and a["loggedIn"] is False and a["version"] is None


# ---------------------------------------------------------------- launch option mapping

def test_agent_opts_maps_a_launch_request_for_each_agent():
    from app import main
    body = main.SessionIn(launcher="codex", model="gpt-5.5", effort="high", permission_mode="dontAsk", allowed_tools="Read",
                          opts={"sandbox": "read-only", "search": True})
    got = main._agent_opts("codex", body)
    assert got == {"model": "gpt-5.5", "reasoning_effort": "high", "permission_mode": "dontAsk", "allowed_tools": "Read",
                   "sandbox": "read-only", "search": True}, "a Claude-style effort is the reasoning level; opts ride along"
    both = main.SessionIn(launcher="codex", effort="low", reasoning_effort="xhigh", opts={"permission_mode": "plan"}, permission_mode="auto")
    assert main._agent_opts("codex", both) == {"reasoning_effort": "xhigh", "permission_mode": "plan"}, "the explicit reasoning and opts win"
    assert main._agent_opts("claude", both) == {"effort": "low", "permission_mode": "auto"}, "claude reads neither reasoning_effort nor opts"
    assert main._agent_opts("codex", main.SessionIn(launcher="codex")) == {}


def test_launchopts_keep_their_old_fields_and_the_card_spec_is_unchanged_without_them():
    from app import main
    assert {"model", "effort", "permission_mode", "allowed_tools", "disallowed_tools", "append_system_prompt"} <= set(main.LaunchOpts.model_fields)
    assert {"reasoning_effort", "opts"} <= set(main.TASK_SPEC_KEYS) and "args" in main.TASK_SPEC_KEYS and "add_dirs" in main.TASK_SPEC_KEYS
