"""The Claude adapter seam (app/agents). Written before the adapter: every expected argv below is a literal copied from what
main.py / tasks.py / recover.py / scheduler.py produced at v0.5.3b, so the adapter is pinned to byte-for-byte parity."""
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import agents, claude_auth, projects
from app.agents import base, claude
from app.agents.base import Agent, Check, HookNorm, LaunchReq, OptField, SlashSpec
from app.config import settings

SID = "11111111-2222-4333-8444-555555555555"
RID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures" / "agents" / "claude"


@pytest.fixture(autouse=True)
def _sandbox_config_dir(tmp_path, monkeypatch):
    """Every test here runs against a throwaway Claude config dir: install_hooks writes its settings file and transcript_path
    creates project folders, which must never land in the real home. The claude binary is hidden so none of it can run."""
    cfg = tmp_path / "claude-config"
    monkeypatch.setattr(settings, "claude_config_dir", cfg)
    monkeypatch.setattr(settings, "claude_bin", lambda: None)
    assert str(cfg).startswith(str(tmp_path)) and Path.home() not in cfg.parents
    yield cfg


@pytest.fixture
def ag():
    return agents.get("claude")


def plan(ag, **kw):
    kw.setdefault("kind", "new")
    kw.setdefault("session_name", "s1")
    kw.setdefault("cwd", "/p/shop/api")
    kw.setdefault("session_id", SID)
    return ag.launch_plan(LaunchReq(**kw))


# ---------- registry ----------

def test_registry(ag):
    assert agents.names() == ["claude", "codex"]                  # Codex joined in v0.5.11 (tests/test_agents_codex.py)
    assert [a.name for a in agents.all()] == ["claude", "codex"]
    assert agents.get("claude") is ag and isinstance(ag, Agent) and isinstance(ag, claude.ClaudeAgent)
    assert (ag.name, ag.label, ag.glyph) == ("claude", "Claude", "◆")
    with pytest.raises(KeyError):
        agents.get("gemini")                     # no adapter for it
    with pytest.raises(KeyError):
        agents.get("shell")                      # shell is not an adapter
    with pytest.raises(TypeError):
        Agent()                                  # the ABC is abstract


def test_every_interface_method_is_implemented(ag):
    names = ("bin version auth_status login_start login_state login_submit_code logout option_schema validate_opts launch_plan "
             "resume_argv continue_argv headless_argv parse_headless install_hooks uninstall_hooks hooks_status normalise_hook "
             "transcript_path usage_sources cost_join_key slash_commands exit_command worktree_strategy mcp_register_cmd "
             "forbidden_extra doctor_checks").split()
    for n in names:
        assert callable(getattr(ag, n)), n
        assert n in Agent.__abstractmethods__, n
    assert not Agent.__abstractmethods__ - set(names), "an abstract method the adapter test does not know about"


# ---------- launch_plan parity: sessions (main.api_create_session at v0.5.3b) ----------

def test_new_default():
    ag = agents.get("claude")
    p = plan(ag)
    assert p.argv == ["claude", "--session-id", SID, "--name", "s1"]
    assert p.cmd_line == f"claude --session-id {SID} --name s1"
    assert (p.agent_session_id, p.cwd, p.worktree, p.opts_clean) == (SID, "/p/shop/api", None, {})


def test_new_generates_a_uuid_when_none_given(ag):
    p = ag.launch_plan(LaunchReq(kind="new", session_name="s1", cwd="/p"))
    assert p.agent_session_id and p.argv[:3] == ["claude", "--session-id", p.agent_session_id]
    assert claude.UUID_RE.match(p.agent_session_id)
    with pytest.raises(projects.BadRequest):
        ag.launch_plan(LaunchReq(kind="new", session_name="s1", cwd="/p", session_id="not-a-uuid"))


def test_model_effort_permission_tools_append_order(ag):
    assert plan(ag, opts={"model": "opus"}).argv == ["claude", "--session-id", SID, "--name", "s1", "--model", "opus"]
    opts = {"model": " fable ", "effort": "xhigh", "permission_mode": "acceptEdits",
            "allowed_tools": "Bash(git *), Edit\nRead", "disallowed_tools": "WebFetch",
            "append_system_prompt": "  be brief  "}
    p = plan(ag, opts=opts)
    assert p.argv == ["claude", "--session-id", SID, "--name", "s1", "--model", "fable", "--effort", "xhigh",
                      "--permission-mode", "acceptEdits", "--allowedTools", "Bash(git *)", "Edit", "Read",
                      "--disallowedTools", "WebFetch", "--append-system-prompt", "be brief"]
    assert p.cmd_line == (f"claude --session-id {SID} --name s1 --model fable --effort xhigh --permission-mode acceptEdits "
                          "--allowedTools 'Bash(git *)' Edit Read --disallowedTools WebFetch --append-system-prompt 'be brief'")
    assert p.opts_clean == {"model": "fable", "effort": "xhigh", "permission_mode": "acceptEdits",
                            "allowed_tools": ["Bash(git *)", "Edit", "Read"], "disallowed_tools": ["WebFetch"],
                            "append_system_prompt": "be brief"}
    # opts_clean is itself valid input and yields the same argv (it is what a resume re-passes)
    assert plan(ag, opts=p.opts_clean).argv == p.argv


@pytest.mark.parametrize("mode", ["manual", "acceptEdits", "plan", "auto", "dontAsk"])
def test_each_permission_mode(ag, mode):
    p = plan(ag, opts={"permission_mode": mode})
    assert p.argv[-2:] == ["--permission-mode", mode] and p.opts_clean == {"permission_mode": mode}


def test_bypass_permission_mode_reaches_argv_but_is_never_stored(ag):
    p = plan(ag, opts={"permission_mode": "bypassPermissions"})
    assert p.argv[-2:] == ["--permission-mode", "bypassPermissions"]
    assert "permission_mode" not in p.opts_clean and "bypass" not in p.opts_clean


def test_bypass_flag_guard(ag):
    base_ = ["claude", "--session-id", SID, "--name", "s1"]
    assert plan(ag, bypass=True).argv == base_ + ["--dangerously-skip-permissions"]
    # the flag goes before the launch controls and the extra args (test_tmux_cmd: "--dangerously-skip-permissions --model opus")
    assert plan(ag, bypass=True, opts={"extra": ["--model", "opus"]}).argv == base_ + ["--dangerously-skip-permissions", "--model", "opus"]
    # a bypass spelling already in the extra args is not duplicated (main: `body.bypass and not _bypass_requested(extra)`)
    for spelling in (["--dangerously-skip-permissions"], ["--permission-mode=bypassPermissions"], ["--DANGEROUSLY-skip-permissions"]):
        argv = plan(ag, bypass=True, opts={"extra": spelling}).argv
        assert argv == base_ + spelling, spelling
    assert plan(ag, bypass=False).argv == base_
    assert plan(ag, bypass=True).opts_clean == {}, "bypass is never stored"


def test_extra_args_then_add_dirs_last(ag):
    p = plan(ag, opts={"model": "opus", "extra": ["--permission-mode", "acceptEdits"]}, add_dirs=["/a", "/b"])
    assert p.argv == ["claude", "--session-id", SID, "--name", "s1", "--model", "opus", "--permission-mode", "acceptEdits",
                      "--add-dir", "/a", "/b"]
    assert "extra" not in p.opts_clean, "extra args are one-off: not stored, not re-passed on resume"
    # test_tmux_cmd: args '--permission-mode acceptEdits' + add_dirs -> argv[-2:] == --add-dir <first>
    assert plan(ag, add_dirs=["/a"]).argv[-2:] == ["--add-dir", "/a"]
    # extra may also arrive as a raw string (shlex-split, with main's error text)
    assert plan(ag, opts={"extra": "--model opus"}).argv[-2:] == ["--model", "opus"]
    with pytest.raises(projects.BadRequest, match="extra args: "):
        plan(ag, opts={"extra": "'unbalanced"})


def test_resume_and_continue(ag):
    assert plan(ag, kind="resume", resume_id=RID).argv == ["claude", "--resume", RID]
    assert plan(ag, kind="resume", resume_id=RID).agent_session_id == RID
    assert plan(ag, kind="resume").argv == ["claude", "--resume"]                      # the picker
    assert plan(ag, kind="resume").agent_session_id is None
    assert plan(ag, kind="continue").argv == ["claude", "--continue"]
    assert plan(ag, kind="continue").agent_session_id is None
    assert plan(ag, kind="resume", resume_id=RID, add_dirs=["/a", "/b"]).argv == ["claude", "--resume", RID, "--add-dir", "/a", "/b"]
    assert plan(ag, kind="continue", bypass=True, opts={"model": "opus", "extra": ["--verbose"]}, add_dirs=["/a"]).argv == \
        ["claude", "--continue", "--dangerously-skip-permissions", "--model", "opus", "--verbose", "--add-dir", "/a"]
    assert plan(ag, kind="resume", resume_id=RID).cmd_line == f"claude --resume {RID}"
    with pytest.raises(projects.BadRequest, match="resume id must be a UUID"):
        plan(ag, kind="resume", resume_id="-x")
    with pytest.raises(projects.BadRequest):
        plan(ag, kind="sideways")
    with pytest.raises(projects.BadRequest, match="first prompt"):
        plan(ag, kind="resume", resume_id=RID, prompt="hi")                           # a positional after --resume [value] is ambiguous
    with pytest.raises(projects.BadRequest, match="worktree"):
        plan(ag, kind="continue", worktree="x")


def test_devcontainer_wraps_and_drops_add_dirs(ag, tmp_path):
    repo = tmp_path / "web"
    (repo / ".devcontainer").mkdir(parents=True)
    (repo / ".devcontainer" / "devcontainer.json").write_text("{}")
    wf = str(repo)
    p = plan(ag, cwd=wf, bypass=True, opts={"devcontainer": True, "extra": ["--model", "opus"]}, add_dirs=["/ignored"])
    assert p.argv == ["claude", "--session-id", SID, "--name", "s1", "--dangerously-skip-permissions", "--model", "opus"]
    assert p.cmd_line == (f"devcontainer up --workspace-folder {wf} && devcontainer exec --workspace-folder {wf} -- "
                          f"claude --session-id {SID} --name s1 --dangerously-skip-permissions --model opus")
    assert p.opts_clean == {"devcontainer": True}
    # resume inside the devcontainer is wrapped too
    p = plan(ag, kind="resume", resume_id=RID, cwd=wf, opts={"devcontainer": True})
    assert p.cmd_line == f"devcontainer up --workspace-folder {wf} && devcontainer exec --workspace-folder {wf} -- claude --resume {RID}"
    with pytest.raises(projects.BadRequest, match="no .devcontainer/devcontainer.json"):
        plan(ag, cwd=str(tmp_path / "nothing"), opts={"devcontainer": True})
    assert plan(ag, cwd=wf, opts={"devcontainer": False}).cmd_line.startswith("claude ")


def test_fast_is_schema_only_not_argv(ag):
    p = plan(ag, opts={"fast": True})
    assert p.argv == ["claude", "--session-id", SID, "--name", "s1"]       # no --fast flag exists; /fast is sent after start
    assert p.opts_clean == {"fast": True}
    assert plan(ag, opts={"fast": False}).opts_clean == {}


def test_launch_args_match_main_launch_args(ag):
    """main._launch_args (still the source of truth until the integrator delegates it) and the adapter agree."""
    from app import main
    cases = [{}, {"model": "opus"}, {"effort": "max"}, {"permission_mode": "plan"}, {"allowed_tools": "A, B\nC"}, {"disallowed_tools": "X"},
             {"append_system_prompt": "hello  "}, {"model": "sonnet", "effort": "low", "permission_mode": "dontAsk", "allowed_tools": "Bash(ls *)",
                                                    "disallowed_tools": "Edit,Write", "append_system_prompt": "x"}]
    head = ["claude", "--session-id", SID, "--name", "s1"]
    for kw in cases:
        want = main._launch_args(main.LaunchOpts(**kw))
        assert plan(ag, opts=kw).argv == head + want, kw


# ---------- launch_plan parity: tasks (tasks.build_command) ----------

def test_task_launch_parity(ag, tmp_path):
    from app import tasks
    repo = tmp_path / "api"
    p = plan(ag, cwd=str(repo), worktree="fix-bug", prompt="fix it", task=True, add_dirs=["/x"],
             opts={"model": "opus", "extra": ["--max-turns", "3"]})
    assert p.argv == ["claude", "--model", "opus", "--max-turns", "3", "--add-dir", "/x", "--worktree", "fix-bug", "--session-id", SID, "--", "fix it"]
    assert shlex.split(tasks.build_command("fix-bug", SID, "fix it", ["--model", "opus", "--max-turns", "3"], ["/x"])) == p.argv
    assert p.cmd_line == tasks.build_command("fix-bug", SID, "fix it", ["--model", "opus", "--max-turns", "3"], ["/x"])
    assert p.worktree == str(tasks.worktree_path(repo, "fix-bug")) == str(repo / ".claude" / "worktrees" / "fix-bug")
    assert (p.cwd, p.agent_session_id) == (str(repo), SID)
    assert plan(ag, cwd=str(repo), worktree="w", prompt="p", task=True).argv == ["claude", "--worktree", "w", "--session-id", SID, "--", "p"]
    # a prompt that starts with '-' (a markdown bullet) is a prompt: `--` ends the options, in both builders
    bullet = "- fix the bug\n- add tests"
    assert plan(ag, cwd=str(repo), worktree="w", prompt=bullet, task=True).argv == ["claude", "--worktree", "w", "--session-id", SID, "--", bullet]
    assert shlex.split(tasks.build_command("w", SID, bullet, [], [])) == ["claude", "--worktree", "w", "--session-id", SID, "--", bullet]
    assert tasks.WORKTREES == claude.WORKTREE_DIR


def test_task_rejects_bypass_in_every_spelling(ag):
    msg = "is not allowed for tasks; start a session and choose bypass there if you really want it"
    for kw in ({"bypass": True}, {"opts": {"permission_mode": "bypassPermissions"}},
               {"opts": {"extra": ["--dangerously-skip-permissions"]}}, {"opts": {"extra": ["--permission-mode=bypassPermissions"]}},
               {"opts": {"extra": ["--settings", "{}"]}}):
        with pytest.raises(projects.BadRequest, match=msg):
            plan(ag, worktree="w", prompt="p", task=True, **kw)
    # a worktree session that is not a task keeps the interactive rules (bypass is an explicit choice there)
    assert plan(ag, worktree="w", prompt="p", bypass=True).argv[:2] == ["claude", "--dangerously-skip-permissions"]


def test_task_prompt_cannot_smuggle_a_flag(ag):
    """The prompt is the last positional: a prompt that IS a bypass flag would be parsed as one. Tasks refuse that; ordinary prompts
    (including a markdown bullet list, or one that merely mentions the flag) are untouched."""
    msg = "is not allowed for tasks"
    for prompt in ("--dangerously-skip-permissions", "--permission-mode=bypassPermissions", "--settings={}", "  --dangerously-skip-permissions please"):
        with pytest.raises(projects.BadRequest, match=msg):
            plan(ag, worktree="w", prompt=prompt, task=True)
    for prompt in ("- fix the bug\n- add tests", "explain why --dangerously-skip-permissions is risky", "-x"):
        assert plan(ag, worktree="w", prompt=prompt, task=True).argv[-1] == prompt


def test_prompt_form_puts_variadics_before_the_positional(ag):
    """A first prompt on a normal session: --add-dir and --allowedTools are variadic and would swallow the prompt, so they go first
    and `--session-id <id> --name <n>` ends the list (the same trick tasks.build_command uses)."""
    p = plan(ag, prompt="hello", add_dirs=["/x"], opts={"model": "opus", "allowed_tools": "A,B"})
    assert p.argv == ["claude", "--model", "opus", "--allowedTools", "A", "B", "--add-dir", "/x", "--session-id", SID, "--name", "s1", "--", "hello"]
    dash = plan(ag, prompt="- fix the bug\n- add tests")
    assert dash.argv[-2:] == ["--", "- fix the bug\n- add tests"], "a prompt that starts with '-' is a prompt, not an option"


# ---------- resume / continue argv (recover.py, api_run_resume) ----------

def test_resume_and_continue_argv_match_recover(ag):
    assert ag.resume_argv(RID) == ["claude", "--resume", RID]
    assert ag.resume_argv(RID, add_dirs=["/a", "/b"]) == ["claude", "--resume", RID, "--add-dir", "/a", "/b"]
    assert ag.resume_argv() == ["claude", "--resume"]
    assert ag.resume_argv(None, name="my chat") == ["claude", "--resume", "my chat"]
    assert ag.continue_argv("/p/shop/api") == ["claude", "--continue"]
    assert ag.continue_argv("/p/shop/api", add_dirs=("/a",)) == ["claude", "--continue", "--add-dir", "/a"]
    # opts (the stored opts_clean) are re-passed on resume
    assert ag.resume_argv(RID, opts={"model": "opus", "effort": "high"}) == ["claude", "--resume", RID, "--model", "opus", "--effort", "high"]
    assert ag.continue_argv("/p", opts={"model": "opus"}, add_dirs=["/a"]) == ["claude", "--continue", "--model", "opus", "--add-dir", "/a"]
    with pytest.raises(projects.BadRequest):
        ag.resume_argv("-x")


def test_recover_plan_is_unchanged_literal(monkeypatch, tmp_path):
    """recover.plan still builds ['claude','--resume',id] (+ --add-dir) itself: the adapter must agree with it."""
    from app import recover
    monkeypatch.setattr(projects, "repo_path", lambda p, r: tmp_path)
    rows = {"shop--api--s1": {"project": "shop", "repo": "api", "launcher": "claude", "claude_session_id": RID, "add_dirs": ["/a"]},
            "shop--api--s2": {"project": "shop", "repo": "api", "launcher": "continue", "claude_session_id": None, "add_dirs": []}}
    by = {t["name"]: t["cmd"] for t in recover.plan(rows, set())}
    ag = agents.get("claude")
    assert by["shop--api--s1"] == ag.resume_argv(RID, add_dirs=["/a"]) == ["claude", "--resume", RID, "--add-dir", "/a"]
    assert by["shop--api--s2"] == ag.continue_argv(str(tmp_path)) == ["claude", "--continue"]


# ---------- headless ----------

def test_headless_argv_parity(ag):
    argv = ag.headless_argv("do it", mode="acceptEdits", max_turns=20, budget=2.5, extra=["--model", "opus"], cwd=None,
                            slug="job-1", last_message_file=None)
    assert argv == ["claude", "-p", "do it", "--worktree", "job-1", "--output-format", "json", "--permission-mode", "acceptEdits",
                    "--max-turns", "20", "--max-budget-usd", "2.50", "--model", "opus"]
    for budget in (None, 0, 0.0):
        assert "--max-budget-usd" not in ag.headless_argv("p", mode="plan", max_turns=3, budget=budget, extra=[], cwd="/x",
                                                          slug="s", last_message_file="/tmp/f")
    assert ag.headless_argv("p", mode="plan", max_turns=3, budget=None, extra=[], cwd=None, slug="s", last_message_file=None) == \
        ["claude", "-p", "p", "--worktree", "s", "--output-format", "json", "--permission-mode", "plan", "--max-turns", "3"]


def test_scheduler_delegates_to_the_adapter(ag):
    from app import scheduler
    assert scheduler.build_command("do it", "job-1", "acceptEdits", 20, 2.5, ["--model", "opus"]) == \
        ag.headless_argv("do it", mode="acceptEdits", max_turns=20, budget=2.5, extra=["--model", "opus"], cwd=None, slug="job-1",
                         last_message_file=None)
    assert scheduler.parse_result is claude.parse_result
    assert scheduler.LIMIT_MSG_RE is claude.LIMIT_MSG_RE and scheduler.RATE_RE is claude.RATE_RE
    assert scheduler.MODES == claude.HEADLESS_MODES == ("default", "acceptEdits", "plan", "auto", "dontAsk")
    assert scheduler.FORBIDDEN_ARG_PARTS == claude.FORBIDDEN_ARG_PARTS
    assert scheduler.check_extra_args("--model opus") == ["--model", "opus"]
    assert scheduler.check_extra_args("--model opus", agent="claude") == ["--model", "opus"]
    assert scheduler.check_extra_args(None) == [] and scheduler.check_extra_args("") == []
    with pytest.raises(ValueError, match="^argument not allowed for unattended runs: --permission-mode=auto$"):
        scheduler.check_extra_args("--model opus --permission-mode=auto")
    with pytest.raises(ValueError, match="^args: "):
        scheduler.check_extra_args("'unbalanced")
    with pytest.raises(KeyError):
        scheduler.check_extra_args("--x", agent="nope")


# ---------- validate_opts: today's BadRequest messages ----------

def v(ag, raw, *, interactive=True, tasks_or_headless=False):
    return ag.validate_opts(raw, interactive=interactive, tasks_or_headless=tasks_or_headless)


@pytest.mark.parametrize("raw,msg", [
    ({"model": "bad model!"}, "model: use an alias (fable, opus, sonnet, haiku) or a full model id"),
    ({"model": "-rf"}, "model: use an alias (fable, opus, sonnet, haiku) or a full model id"),
    ({"effort": "ultracode"}, "effort must be one of low, medium, high, xhigh, max"),
    ({"effort": "HIGH"}, "effort must be one of low, medium, high, xhigh, max"),
    ({"permission_mode": "yolo"}, "permission_mode must be one of manual, acceptEdits, plan, auto, dontAsk, bypassPermissions"),
    ({"allowed_tools": "Bash;rm"}, "tool pattern not allowed: 'Bash;rm'"),
    ({"disallowed_tools": "ok, $(x)"}, "tool pattern not allowed: '$(x)'"),
    ({"append_system_prompt": "x" * 4001}, "append_system_prompt is too long (4000 chars max)"),
    ({"extra": ["--settings", "/tmp/x.json"]},
     "--settings: settings overrides are not allowed in extra args; use the model / effort / permission / tools controls"),
    ({"extra": ["--model", "opus", "--Setting-Sources=user"]},
     "--Setting-Sources=user: settings overrides are not allowed in extra args; use the model / effort / permission / tools controls"),
    ({"extra": ["--permission-prompt-tool", "x"]},
     "--permission-prompt-tool: settings overrides are not allowed in extra args; use the model / effort / permission / tools controls"),
    ({"extra": 7}, "extra args must be a list of strings"),
])
def test_validate_opts_session_messages(ag, raw, msg):
    with pytest.raises(projects.BadRequest) as e:
        v(ag, raw)
    assert str(e.value) == msg


def test_validate_opts_session_accepts_and_cleans(ag):
    assert v(ag, {}) == {} and v(ag, None) == {}
    assert v(ag, {"model": None, "effort": "", "permission_mode": None, "allowed_tools": "", "append_system_prompt": "  "}) == {}
    assert v(ag, {"model": " opus ", "unknown": 1, "add_dirs": ["x"], "bypass": True}) == {"model": "opus"}      # bypass is never stored
    assert v(ag, {"effort": "max", "fast": True, "devcontainer": True}) == {"effort": "max", "fast": True, "devcontainer": True}
    assert v(ag, {"allowed_tools": ["Read", " Edit "]}) == {"allowed_tools": ["Read", "Edit"]}
    # bypass spellings in extra are allowed for interactive sessions (an explicit choice) ...
    assert v(ag, {"extra": ["--dangerously-skip-permissions"]}) == {}
    assert v(ag, {"permission_mode": "bypassPermissions"}) == {}      # ... and permission_mode too, but neither is stored
    assert len("x" * 4000) == 4000 and v(ag, {"append_system_prompt": "x" * 4000}) == {"append_system_prompt": "x" * 4000}


def test_validate_opts_task_messages(ag):
    tail = "bypassPermissions (or a settings override) is not allowed for tasks; start a session and choose bypass there if you really want it"
    for raw, first in (({"extra": ["--dangerously-skip-permissions"]}, "--dangerously-skip-permissions"),
                       ({"extra": ["--permission-mode=bypassPermissions"]}, "--permission-mode=bypassPermissions"),
                       ({"extra": ["--settings", "x"]}, "--settings"),
                       ({"permission_mode": "bypassPermissions"}, "bypassPermissions")):
        with pytest.raises(projects.BadRequest) as e:
            v(ag, raw, tasks_or_headless=True)
        assert str(e.value) == f"{first}: {tail}"
    # tasks may still use the permission-mode control and --permission-mode in extra (only headless forbids that)
    assert v(ag, {"permission_mode": "acceptEdits", "extra": ["--permission-mode", "plan"]}, tasks_or_headless=True) == {"permission_mode": "acceptEdits"}
    with pytest.raises(projects.BadRequest, match="^permission_mode must be one of manual"):
        v(ag, {"permission_mode": "bogus"}, tasks_or_headless=True)


def test_validate_opts_headless_messages(ag):
    h = dict(interactive=False, tasks_or_headless=True)
    for pm in ("bypassPermissions", "manual", "bogus"):
        with pytest.raises(projects.BadRequest) as e:
            v(ag, {"permission_mode": pm}, **h)
        assert str(e.value) == "permission_mode must be one of default, acceptEdits, plan, auto, dontAsk (bypass only inside a devcontainer)"
    for bad in ("--permission-mode=auto", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--settings"):
        with pytest.raises(projects.BadRequest) as e:
            v(ag, {"extra": ["--model", "opus", bad]}, **h)
        assert str(e.value) == f"argument not allowed for unattended runs: {bad}"
    assert v(ag, {"permission_mode": "default", "model": "opus", "extra": ["--add-dir", "/tmp"]}, **h) == {"permission_mode": "default", "model": "opus"}


# ---------- forbidden_extra: the three tiers that exist today ----------

def test_forbidden_extra_tiers(ag):
    # interactive session: settings overrides only (main._override_requested)
    assert ag.forbidden_extra(["--model", "opus"], interactive=True) is None
    assert ag.forbidden_extra(["--SETTINGS=x"], interactive=True) == "--SETTINGS=x"
    assert ag.forbidden_extra(["--setting-sources", "user"], interactive=True) == "--setting-sources"
    assert ag.forbidden_extra(["--permission-prompt-tool", "x"], interactive=True) == "--permission-prompt-tool"
    for ok in ("--dangerously-skip-permissions", "--permission-mode=bypassPermissions", "--permission-mode", "--allow-dangerously-skip-permissions"):
        assert ag.forbidden_extra([ok], interactive=True) is None, ok
    # task (interactive, task=True): override OR bypass, overrides reported first; --permission-mode stays allowed
    assert ag.forbidden_extra(["--dangerously-skip-permissions"], interactive=True, task=True) == "--dangerously-skip-permissions"
    assert ag.forbidden_extra(["--permission-mode=bypassPermissions"], interactive=True, task=True) == "--permission-mode=bypassPermissions"
    assert ag.forbidden_extra(["bypassPermissions", "--settings"], interactive=True, task=True) == "--settings"
    assert ag.forbidden_extra(["--permission-mode", "plan"], interactive=True, task=True) is None
    # headless: scheduler.FORBIDDEN_ARG_PARTS, any spelling
    for bad in ("--permission-mode", "--PERMISSION-MODE=auto", "--allow-dangerously-skip-permissions", "dangerously", "BypassPermissions",
                "--settings", "--setting-sources", "--permission-prompt-tool"):
        assert ag.forbidden_extra(["--model", "opus", bad], interactive=False) == bad, bad
    assert ag.forbidden_extra(["--model", "opus", "--add-dir", "/tmp"], interactive=False) is None
    assert ag.forbidden_extra([], interactive=False) is None


# ---------- option_schema ----------

def test_option_schema(ag):
    s = ag.option_schema()
    assert all(isinstance(f, OptField) for f in s)
    assert [f.key for f in s] == ["launcher", "resume_id", "from_pr", "name", "model", "effort", "fast", "permission_mode", "prompt", "bypass",
                                  "allowed_tools", "disallowed_tools", "tools", "append_system_prompt", "agent_name", "fallback_model",
                                  "autocompact", "worktree", "worktree_name", "fork_session", "add_dirs", "devcontainer", "mcp_config", "extra"]
    by = {f.key: f for f in s}
    assert by["model"].choices[:4] == ["opus", "fable", "sonnet", "haiku"] and by["model"].kind == "combo"
    assert by["model"].choices[4:] == ["opusplan", "best", "opus[1m]", "sonnet[1m]"], "the [1m] variants follow the aliases"
    assert by["effort"].choices == list(claude.EFFORTS) == ["low", "medium", "high", "xhigh", "max"]
    assert "ultracode" not in by["effort"].choices and "V19" in by["effort"].help
    assert by["permission_mode"].choices == list(claude.PERMISSION_MODES)
    assert by["fast"].kind == "bool" and "/fast" in by["fast"].help
    assert by["bypass"].danger is True and by["bypass"].kind == "bool" and by["bypass"].group == "advanced"
    assert not any(f.danger for f in s if f.key != "bypass")
    assert {f.group for f in s} == {"basic", "advanced"}
    assert [f.key for f in s if f.group == "basic"] == ["launcher", "resume_id", "from_pr", "name", "model", "effort", "fast", "permission_mode",
                                                        "prompt"]
    assert by["launcher"].choices == ["new", "resume", "continue", "from_pr"] and by["launcher"].default == "new"
    assert by["resume_id"].when == {"launcher": ["resume"]} and by["from_pr"].when == {"launcher": ["from_pr"]}
    assert by["fork_session"].when == {"launcher": ["resume", "continue"]} and by["prompt"].when == {"launcher": ["new"]}
    assert by["worktree_name"].when == {"worktree": True} and by["worktree"].when == {"launcher": ["new"]}
    assert by["devcontainer"].when == {"repo.devcontainer": True}
    assert (by["model"].default, by["effort"].default) == ("opus", "high")                  # v0.5.13 launcher defaults
    assert by["autocompact"].choices == ["auto"] and by["autocompact"].kind == "combo"
    assert json.dumps([f.__dict__ for f in s])                                              # JSON-serialisable for GET /api/agents
    # every key the schema offers is accepted by validate_opts (or handled by the launch request), none is silently dropped
    accepted = {"model", "effort", "permission_mode", "fast", "allowed_tools", "disallowed_tools", "append_system_prompt", "devcontainer",
                "extra", "tools", "agent_name", "fallback_model", "autocompact", "mcp_config", "from_pr", "fork_session"}
    carried = {"launcher", "resume_id", "name", "prompt", "bypass", "add_dirs", "worktree", "worktree_name"}   # LaunchReq.kind/resume_id/session_name/prompt/bypass/add_dirs/worktree
    assert {f.key for f in s} - accepted == carried


# ---------- slash commands ----------

def test_slash_commands(ag):
    sc = ag.slash_commands()
    assert all(isinstance(x, SlashSpec) for x in sc.values())
    got = {k: (x.cmd, x.weight, x.arg, x.read, x.destructive, x.verified) for k, x in sc.items()}
    assert got == {
        "clear": ("/clear", 155, False, False, True, False),
        "compact": ("/compact", 120, False, False, False, False),
        "usage": ("/usage", 100, False, True, False, False),
        "effort": ("/effort", 58, True, False, False, False),
        "model": ("/model", 39, True, False, False, False),
        "rename": ("/rename", 10, True, False, False, False),
        "context": ("/context", 9, False, True, False, False),
        "status": ("/status", 3, False, True, False, False),
        "cost": ("/cost", 0, False, True, False, False),
        "fast": ("/fast", 0, False, False, False, False),
    }
    assert [k for k, x in sc.items() if x.hidden] == ["cost", "fast"]
    assert list(sc)[:3] == ["clear", "compact", "usage"], "ordered by weight"
    assert ag.exit_command() == "/exit"
    assert ag.worktree_strategy() == "native"


# ---------- parse_result / limit text (moved from scheduler) ----------

LIVE_LIMIT = "You've hit your session limit · resets 10:05pm (Asia/Kathmandu)"


def test_limit_regex_matches_live_text():
    assert claude.LIMIT_MSG_RE.search(LIVE_LIMIT)
    assert claude.LIMIT_MSG_RE.search("You've hit your usage limit. Resets 4pm.")
    assert claude.LIMIT_MSG_RE.search("You've hit your weekly limit · resets Oct 9, 3pm (Asia/Kathmandu)")
    assert not claude.LIMIT_MSG_RE.search("Added a rate limit middleware to the API.")


def test_parse_result_fixtures_via_the_reexport():
    from app import scheduler
    r = scheduler.parse_result('noise\n{"result":"ok","session_id":"s","total_cost_usd":1,"num_turns":2,"is_error":false,"subtype":"success"}')
    assert r == {"text": "ok", "session_id": "s", "cost": 1, "turns": 2, "is_error": False, "subtype": "success", "rate_limited": False}
    assert scheduler.parse_result('{"result":"rate limit hit","is_error":true,"subtype":"error_during_execution"}')["rate_limited"]
    assert scheduler.parse_result("garbage")["is_error"] and scheduler.parse_result("garbage")["subtype"] == "no_json"
    base_ = {"type": "result", "subtype": "success", "is_error": False, "session_id": "s", "total_cost_usd": 1, "num_turns": 9}
    filler = "x" * 900 + "\n"
    assert scheduler.parse_result(json.dumps({**base_, "result": filler + LIVE_LIMIT}))["rate_limited"]
    assert not scheduler.parse_result(json.dumps({**base_, "result": "Added a rate limit middleware. " + filler + "All tests pass."}))["rate_limited"]
    assert scheduler.parse_result(json.dumps({**base_, "subtype": "error_rate_limit", "result": "x"}))["rate_limited"]


def test_parse_headless_adds_the_run_job_fallbacks(ag):
    ok = ag.parse_headless('{"result":"ok","session_id":"s","total_cost_usd":0.5,"num_turns":3,"is_error":false,"subtype":"success"}', "", 0)
    assert ok["text"] == "ok" and not ok["rate_limited"] and ok["cost"] == 0.5
    empty = ag.parse_headless("", "boom: could not start\n", 1)                     # no stdout: the stderr tail becomes the text
    assert empty["text"] == "boom: could not start" and empty["is_error"] and not empty["rate_limited"]
    limited = ag.parse_headless("", "Error: rate limit exceeded", 1)                 # the CLI reported the limit on stderr and gave up
    assert limited["rate_limited"]
    assert not ag.parse_headless('{"result":"ok","is_error":false,"subtype":"success"}', "rate limit", 0)["rate_limited"], "rc 0 ignores stderr"
    assert len(ag.parse_headless("", "e" * 9000, 1)["text"]) == 4000


def test_parse_limit_message_kinds_and_reset_time():
    tz = pytest.importorskip("zoneinfo")
    try:
        tz.ZoneInfo("Asia/Kathmandu")
    except Exception:
        pytest.skip("no tz database")
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)                         # 17:45 in Kathmandu (+05:45)
    r = claude.parse_limit_message(LIVE_LIMIT, now=now)
    assert r["kind"] == "5h" and datetime.fromtimestamp(r["resets_at"], timezone.utc) == datetime(2026, 10, 3, 16, 20, tzinfo=timezone.utc)
    later = claude.parse_limit_message(LIVE_LIMIT, now=datetime(2026, 10, 3, 17, 0, tzinfo=timezone.utc))   # 22:45 local: already past 10:05pm
    assert datetime.fromtimestamp(later["resets_at"], timezone.utc) == datetime(2026, 10, 4, 16, 20, tzinfo=timezone.utc)
    w = claude.parse_limit_message("You've hit your weekly limit · resets Oct 9, 3pm (Asia/Kathmandu)", now=now)
    assert w["kind"] == "7d" and datetime.fromtimestamp(w["resets_at"], timezone.utc) == datetime(2026, 10, 9, 9, 15, tzinfo=timezone.utc)
    assert claude.parse_limit_message("5-hour limit reached ∙ resets 3pm", now=now)["kind"] == "5h"
    o = claude.parse_limit_message("You've hit your Opus limit", now=now)
    assert o == {"kind": "other", "resets_at": None}
    assert claude.parse_limit_message("", now=now) == {"kind": "other", "resets_at": None}
    assert claude.parse_limit_message("resets 25pm (Not/AZone)", now=now)["resets_at"] is None      # never raises


# ---------- hooks: normalise_hook ----------

def load(name):
    return json.loads((FIX / name).read_text())


def test_normalise_stopfailure_live_payload(ag):
    n = ag.normalise_hook("StopFailure", load("stopfailure_rate_limit.json"))
    assert isinstance(n, HookNorm)
    assert (n.state, n.event, n.kind, n.attention, n.ignored) == ("errored", "StopFailure", "rate_limit", True, None)
    assert n.message == LIVE_LIMIT and n.result == LIVE_LIMIT
    assert n.limit and n.limit["kind"] == "5h" and n.limit["message"] == LIVE_LIMIT
    assert n.flags["hook_seen"] is True and n.flags["transcript_path"].endswith(".jsonl")
    w = ag.normalise_hook("StopFailure", load("stopfailure_weekly.json"))
    assert w.limit["kind"] == "7d" and w.state == "errored"
    s = ag.normalise_hook("StopFailure", load("stopfailure_server_error.json"))
    assert (s.state, s.kind, s.limit) == ("errored", "server_error", None) and s.message == "API Error: 500 Internal server error"
    # older shapes still resolve: error_type / error_category / matcher, and 'error' as a bare string
    assert ag.normalise_hook("StopFailure", {"error_type": "rate_limit_error", "error": "slow down"}).limit is not None
    assert ag.normalise_hook("StopFailure", {}).kind == "error" and ag.normalise_hook("StopFailure", {}).state == "errored"


def test_normalise_other_events(ag):
    n = ag.normalise_hook("Notification", load("notification_permission.json"))
    assert (n.state, n.attention, n.kind, n.message) == ("waiting", True, "permission_prompt", "Claude needs your permission to use Bash")
    assert n.flags["wait_kind"] == "permission"
    idle = ag.normalise_hook("Notification", {"notification_type": "idle_prompt", "message": "Claude is waiting"})
    assert (idle.state, idle.flags["wait_kind"]) == ("waiting", "idle")
    assert ag.normalise_hook("Notification", {"notification_type": "elicitation_dialog"}).flags["wait_kind"] == "elicitation"
    other = ag.normalise_hook("Notification", {"notification_type": "auth_success", "message": "m"})
    assert (other.state, other.attention, other.message) == (None, False, "m")
    stop = ag.normalise_hook("Stop", load("stop.json"))
    assert (stop.state, stop.attention, stop.result) == ("done", True, "Done. Tests pass.\n\nShall I open a PR?")
    assert stop.message == stop.result, "the screen-line fallback in hooks.apply only runs when this is None"
    assert ag.normalise_hook("Stop", {}).message is None and ag.normalise_hook("Stop", {}).state == "done"
    up = ag.normalise_hook("UserPromptSubmit", {"prompt": "do the thing", "session_id": SID})
    assert (up.state, up.prompt, up.attention) == ("working", "do the thing", False)
    assert ag.normalise_hook("UserPromptSubmit", {"prompt": 5}).prompt is None
    ss = ag.normalise_hook("SessionStart", {"source": "resume"})
    assert (ss.state, ss.kind) == ("idle", "resume")
    se = ag.normalise_hook("SessionEnd", {"reason": "logout"})
    assert (se.state, se.kind) == ("ended", "logout")
    odd = ag.normalise_hook("PreCompact", {"matcher": "manual"})
    assert (odd.state, odd.kind, odd.flags["compacting"]) == (None, "manual", True)
    assert ag.normalise_hook("PostCompact", {}).flags["compacting"] is False
    assert ag.normalise_hook("SubagentStart", {}).incr == {"subagents": 1} and ag.normalise_hook("SubagentStop", {}).incr == {"subagents": -1}
    assert ag.normalise_hook("statusline", {"model": {}}).ignored == "statusline"
    assert ag.normalise_hook("Whatever", {"matcher": "x"}).kind == "x"
    for bad in (None, [], "str", 5):
        assert ag.normalise_hook("Stop", bad).state == "done"                  # malformed payloads never raise


def test_transcript_path(ag, tmp_path):
    cfg = settings.claude_config_dir
    inside = cfg / "projects" / "-srv-projects-shop-api" / f"{SID}.jsonl"
    assert ag.transcript_path({"transcript_path": str(inside)}) == inside
    assert ag.transcript_path({"transcript_path": "/etc/passwd"}) is None                  # outside the config dir
    assert ag.transcript_path({"transcript_path": str(cfg / "projects" / "x" / "a.txt")}) is None
    assert ag.transcript_path({"transcript_path": str(cfg / "projects" / ".." / ".." / "etc" / "a.jsonl")}) is None
    assert ag.transcript_path({"flags": {"transcript_path": str(inside)}}) == inside       # a session row
    assert ag.transcript_path({"flags": json.dumps({"transcript_path": str(inside)})}) == inside
    assert ag.transcript_path({}) is None and ag.transcript_path(None) is None
    # derived from cwd + session id only when the file exists
    d = cfg / "projects" / "-srv-projects-shop--claude-worktrees-fix"
    d.mkdir(parents=True)
    f = d / f"{SID}.jsonl"
    f.write_text("{}\n")
    assert ag.transcript_path({"cwd": "/srv/projects/shop/.claude/worktrees/fix", "session_id": SID}) == f
    assert ag.transcript_path({"cwd": "/srv/projects/shop/.claude/worktrees/fix", "agent_session_id": SID}) == f
    assert ag.transcript_path({"cwd": "/srv/projects/shop/other", "session_id": SID}) is None


# ---------- hooks install / status ----------

def test_hooks_status_install_uninstall(ag, tmp_path):
    cfg = settings.claude_config_dir
    assert ag.hooks_status() == {"installed": False, "events": [], "trust": None}
    app_dir = tmp_path / "app"
    foreign = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/usr/local/bin/mine"}]}]}, "model": "opus"}
    cfg.mkdir(parents=True)
    (cfg / "settings.json").write_text(json.dumps(foreign))
    ag.install_hooks(app_dir, remote_approve=False, approve_timeout=90)
    ag.install_hooks(app_dir, remote_approve=False, approve_timeout=90)             # idempotent
    st = ag.hooks_status()
    assert st["installed"] is True and st["trust"] is None
    assert set(st["events"]) >= {"SessionStart", "UserPromptSubmit", "Notification", "Stop", "StopFailure", "SessionEnd"}
    data = json.loads((cfg / "settings.json").read_text())
    assert data["model"] == "opus"
    stop_cmds = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert stop_cmds.count("/usr/local/bin/mine") == 1 and sum("ccboard-hook" in c for c in stop_cmds) == 1
    assert "PermissionRequest" not in data["hooks"]
    ag.install_hooks(app_dir, remote_approve=True, approve_timeout=45)
    data = json.loads((cfg / "settings.json").read_text())
    assert data["hooks"]["PermissionRequest"][0]["hooks"][0]["timeout"] == 75
    ag.uninstall_hooks()
    st = ag.hooks_status()
    assert st["installed"] is False and st["events"] == []
    data = json.loads((cfg / "settings.json").read_text())
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "/usr/local/bin/mine"


def test_settings_script_is_loaded_once():
    mod = claude._claude_settings_mod()
    assert mod is claude._claude_settings_mod() and mod.EVENTS[:2] == ["SessionStart", "UserPromptSubmit"]   # hooks_status runs on every state poll


def test_hooks_status_partial_and_unreadable(ag):
    cfg = settings.claude_config_dir
    cfg.mkdir(parents=True)
    (cfg / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/x/bin/ccboard-hook"}]}]}}))
    st = ag.hooks_status()
    assert st == {"installed": False, "events": ["Stop"], "trust": None}
    (cfg / "settings.json").write_text("{not json")
    assert ag.hooks_status() == {"installed": False, "events": [], "trust": None}
    (cfg / "settings.json").write_text("[]")
    assert ag.hooks_status()["installed"] is False
    with pytest.raises(RuntimeError, match="not valid JSON"):
        ag.install_hooks(Path("/x"), remote_approve=False, approve_timeout=90)           # never overwrites a settings file it cannot read


# ---------- auth wrappers, status_all, doctor ----------

def test_auth_wrappers_delegate_to_claude_auth(ag, monkeypatch):
    calls = []
    monkeypatch.setattr(claude_auth, "status", lambda: {"installed": True, "version": "9", "loggedIn": True})
    monkeypatch.setattr(claude_auth, "version", lambda: "9.9.9 (Claude Code)")
    monkeypatch.setattr(claude_auth, "start_login", lambda: calls.append("start"))
    monkeypatch.setattr(claude_auth, "login_state", lambda: {"running": True, "url": "u", "tail": []})
    monkeypatch.setattr(claude_auth, "submit_code", lambda c: calls.append(("code", c)))
    monkeypatch.setattr(claude_auth, "logout", lambda: {"ok": True, "output": ""})
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    assert ag.bin() == "/fake/claude" and ag.version() == "9.9.9 (Claude Code)"
    assert ag.auth_status() == {"installed": True, "version": "9", "loggedIn": True}
    ag.login_start()
    assert ag.login_state()["url"] == "u"
    ag.login_submit_code("abc#def")
    assert ag.logout() == {"ok": True, "output": ""}
    assert calls == ["start", ("code", "abc#def")]
    monkeypatch.setattr(settings, "claude_bin", lambda: None)
    assert ag.bin() is None


def test_status_all_shape_and_no_extra_binary_calls(ag, monkeypatch):
    n = {"status": 0}

    def status():
        n["status"] += 1
        return {"installed": True, "version": "2.1.287 (Claude Code)", "loggedIn": True, "authMethod": "claude.ai", "email": "a@b.c",
                "subscriptionType": "max"}
    monkeypatch.setattr(claude_auth, "status", status)
    out = agents.status_all()
    assert list(out) == ["claude", "codex"]
    out = {"claude": out["claude"]}                               # codex's own summary is tests/test_agents_codex.py's
    assert out == {"claude": {"installed": True, "version": "2.1.287 (Claude Code)", "loggedIn": True, "authMethod": "claude.ai",
                              "email": "a@b.c", "glyph": "◆", "hooks": {"installed": False}}}
    assert n["status"] == 1, "one claude_auth.status() per call (it is the cached layer)"
    agents.status_all()["claude"]["hooks"]["installed"] = True    # callers cannot poison later results
    assert agents.status_all()["claude"]["hooks"]["installed"] is False
    monkeypatch.setattr(claude_auth, "status", lambda: {"installed": False, "version": None, "loggedIn": False})
    assert agents.status_all()["claude"] == {"installed": False, "version": None, "loggedIn": False, "glyph": "◆", "hooks": {"installed": False}}
    # hooks installed after the fact show up at once: the settings file is read on every call, there is no cache to wait out
    cfg = settings.claude_config_dir
    ag.install_hooks(Path("/opt/ccboard"), remote_approve=False, approve_timeout=90)
    assert agents.status_all()["claude"]["hooks"]["installed"] is True


def test_doctor_checks(ag, monkeypatch):
    monkeypatch.setattr(settings, "claude_bin", lambda: None)
    checks = ag.doctor_checks()
    assert all(isinstance(c, Check) for c in checks)
    assert [(c.id, c.status) for c in checks] == [("claude-bin", "fail"), ("claude-auth", "skip"), ("claude-hooks", "skip")]
    assert all(c.group == "claude" for c in checks)
    assert checks[0].fix and "claude.ai/install" in checks[0].fix["text"] and checks[0].fix.get("cmd")

    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(claude_auth, "version", lambda: "2.1.287 (Claude Code)")
    monkeypatch.setattr(claude_auth, "status", lambda: {"installed": True, "version": "2.1.287", "loggedIn": False})
    by = {c.id: c for c in ag.doctor_checks()}
    assert by["claude-bin"].status == "pass" and "/fake/claude" in by["claude-bin"].detail and "2.1.287" in by["claude-bin"].detail
    assert by["claude-auth"].status == "fail" and by["claude-auth"].fix["cmd"] == "claude auth login" and by["claude-auth"].fix.get("action")
    assert by["claude-hooks"].status == "fail" and "claude_settings.py install" in by["claude-hooks"].fix["cmd"]

    monkeypatch.setattr(claude_auth, "status", lambda: {"installed": True, "version": "2.1.287", "loggedIn": True, "authMethod": "claude.ai",
                                                        "email": "a@b.c", "subscriptionType": "max", "orgName": "Org"})
    ag.install_hooks(Path("/opt/ccboard"), remote_approve=False, approve_timeout=90)
    by = {c.id: c for c in ag.doctor_checks()}
    assert by["claude-auth"].status == "pass" and by["claude-auth"].fix is None
    assert "claude.ai" in by["claude-auth"].detail and "a@b.c" in by["claude-auth"].detail
    assert by["claude-hooks"].status == "pass" and by["claude-hooks"].fix is None

    # partial hooks -> warn; unknown version -> warn
    cfg = settings.claude_config_dir
    data = json.loads((cfg / "settings.json").read_text())
    del data["hooks"]["StopFailure"]
    (cfg / "settings.json").write_text(json.dumps(data))
    monkeypatch.setattr(claude_auth, "version", lambda: None)
    by = {c.id: c for c in ag.doctor_checks()}
    assert by["claude-hooks"].status == "warn" and "StopFailure" in by["claude-hooks"].detail
    assert by["claude-bin"].status == "warn"
    # the detail never carries credentials or token material
    blob = json.dumps([c.__dict__ for c in ag.doctor_checks()])
    assert "token" not in blob.lower() and "credentials" not in blob.lower()


def test_doctor_checks_survive_a_broken_auth_call(ag, monkeypatch):
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(claude_auth, "version", lambda: "1")
    monkeypatch.setattr(claude_auth, "status", lambda: {"installed": True, "version": "1", "loggedIn": False, "error": "claude auth status failed: TimeoutExpired"})
    by = {c.id: c for c in ag.doctor_checks()}
    assert by["claude-auth"].status == "warn" and "TimeoutExpired" in by["claude-auth"].detail


# ---------- misc interface ----------

def test_mcp_register_cmd_mirrors_install_sh(ag):
    assert ag.mcp_register_cmd("/opt/ccboard/.venv/bin/python", "/opt/ccboard/scripts/ccboard_mcp.py", {}) == \
        ["claude", "mcp", "add", "--scope", "user", "ccboard", "--", "/opt/ccboard/.venv/bin/python", "/opt/ccboard/scripts/ccboard_mcp.py"]
    # env pairs go after the name and before `--` (-e is variadic; before the name it would swallow it), sorted for a stable line
    assert ag.mcp_register_cmd("/usr/bin/python3", "/s.py", {"CCBOARD_URL": "http://127.0.0.1:8000", "A": "1"}) == \
        ["claude", "mcp", "add", "--scope", "user", "ccboard", "-e", "A=1", "-e", "CCBOARD_URL=http://127.0.0.1:8000", "--", "/usr/bin/python3", "/s.py"]


def test_usage_and_cost_join(ag):
    assert ag.usage_sources() == ["statusline", "ccusage"]
    assert ag.cost_join_key({"period": SID.upper()}) == SID                       # cost.parse_sessions: id lower-cased
    assert ag.cost_join_key({"sessionId": SID}) == SID
    assert ag.cost_join_key({"period": "2026-10-03"}) is None and ag.cost_join_key({}) is None and ag.cost_join_key(None) is None


def test_describe_is_the_api_agents_entry(ag, monkeypatch):
    monkeypatch.setattr(claude_auth, "status", lambda: {"installed": True, "version": "2.1.287", "loggedIn": True, "authMethod": "claude.ai",
                                                        "email": "a@b.c", "subscriptionType": "max"})
    d = ag.describe()
    assert json.loads(json.dumps(d)) == d
    assert (d["name"], d["label"], d["glyph"], d["installed"], d["version"]) == ("claude", "Claude", "◆", True, "2.1.287")
    assert d["auth"]["loggedIn"] is True and d["hooks"] == {"installed": False}
    assert [o["key"] for o in d["options"]][:8] == ["launcher", "resume_id", "from_pr", "name", "model", "effort", "fast", "permission_mode"]
    assert d["permission_modes"] == list(claude.PERMISSION_MODES) and d["efforts"] == list(claude.EFFORTS) and "ultracode" not in d["efforts"]
    assert d["models"][:4] == ["opus", "fable", "sonnet", "haiku"] and d["reasoning_by_model"] == {}
    assert d["capabilities"] == {"ultracode_flag": False}
    assert d["slash"]["clear"] == {"cmd": "/clear", "label": "Clear", "arg": False, "read": False, "verified": False, "weight": 155,
                                   "destructive": True}
    assert "token" not in json.dumps(d).lower() and "password" not in json.dumps(d).lower()


def test_launch_opt_args_is_main_launch_args(ag):
    assert ag.launch_opt_args({}) == [] and ag.launch_opt_args(None) == []
    assert ag.launch_opt_args({"model": "opus", "effort": "high", "permission_mode": "bypassPermissions"}) == \
        ["--model", "opus", "--effort", "high", "--permission-mode", "bypassPermissions"]
    with pytest.raises(projects.BadRequest, match="^effort must be one of"):
        ag.launch_opt_args({"effort": "turbo"})


def test_ttl_cache_helper():
    t = {"now": 100.0}
    c = base.TTLCache(60.0, clock=lambda: t["now"])
    n = []
    assert c.get("k", lambda: n.append(1) or "v1") == "v1" and c.get("k", lambda: n.append(1) or "v2") == "v1"
    t["now"] = 159.0
    assert c.get("k", lambda: "v3") == "v1"
    t["now"] = 161.0
    assert c.get("k", lambda: "v3") == "v3" and len(n) == 1
    c.clear()
    assert c.get("k", lambda: "v4") == "v4"


def test_dataclass_shapes():
    r = LaunchReq(kind="new")
    assert (r.opts, r.add_dirs, r.bypass, r.task, r.resume_id, r.prompt, r.worktree, r.session_id) == ({}, [], False, False, None, None, None, None)
    assert LaunchReq(kind="new").opts is not LaunchReq(kind="new").opts
    assert [f for f in OptField.__dataclass_fields__] == ["key", "label", "kind", "choices", "default", "help", "group", "danger", "when"]
    assert [f for f in SlashSpec.__dataclass_fields__][:7] == ["cmd", "label", "arg", "read", "verified", "weight", "destructive"]
    assert [f for f in Check.__dataclass_fields__] == ["id", "group", "label", "status", "detail", "fix"]
    assert [f for f in HookNorm.__dataclass_fields__][:7] == ["state", "event", "message", "prompt", "result", "flags", "ignored"]


# ---------- import graph ----------

def _imports(code: str) -> str:
    cp = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT), timeout=60)
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


def test_tasks_never_imports_agents():
    out = _imports("import sys, app.tasks; print(sorted(m for m in sys.modules if m.startswith('app.agents')))")
    assert out == "[]"


def test_agents_import_nothing_above_them():
    out = _imports("import sys, app.agents; print(sorted(m for m in ('app.main','app.scheduler','app.tasks','app.hooks','app.db') if m in sys.modules))")
    assert out == "[]"


def test_scheduler_imports_agents_but_not_the_other_way():
    out = _imports("import sys, app.scheduler; print('app.agents.claude' in sys.modules, 'app.main' in sys.modules)")
    assert out == "True False"


# ---------- v0.5.13: the launcher's fields reach the argv ----------

def test_the_launcher_fields_land_in_the_new_session_argv(ag):
    opts = {"model": "opus", "effort": "high", "tools": "Bash, Edit\nRead", "agent_name": "reviewer", "fallback_model": "sonnet, haiku",
            "autocompact": "auto"}
    p = plan(ag, opts=opts)
    assert p.argv == ["claude", "--session-id", SID, "--name", "s1", "--model", "opus", "--effort", "high", "--tools", "Bash,Edit,Read",
                      "--agent", "reviewer", "--fallback-model", "sonnet,haiku", "--autocompact", "auto"]
    # with a first prompt the variadic flags go first and --session-id / --name end the list, the prompt follows `--`
    p = plan(ag, opts=opts, prompt="fix it", add_dirs=["/x"])
    assert p.argv == ["claude", "--model", "opus", "--effort", "high", "--tools", "Bash,Edit,Read", "--agent", "reviewer", "--fallback-model",
                      "sonnet,haiku", "--autocompact", "auto", "--add-dir", "/x", "--session-id", SID, "--name", "s1", "--", "fix it"]
    # a token count is an autocompact value too; all of it is stored for resume
    assert plan(ag, opts={"autocompact": 150000}).argv[-2:] == ["--autocompact", "150000"]
    assert p.opts_clean == {"model": "opus", "effort": "high", "tools": ["Bash", "Edit", "Read"], "agent_name": "reviewer",
                            "fallback_model": ["sonnet", "haiku"], "autocompact": "auto"}
    # and re-passed by a resume or a continue (recovery), where Claude restores none of them
    assert ag.resume_argv(RID, opts=p.opts_clean)[:3] == ["claude", "--resume", RID] and "--fallback-model" in ag.resume_argv(RID, opts=p.opts_clean)
    assert ag.continue_argv("/p", opts=p.opts_clean)[-2:] == ["--autocompact", "auto"]


def test_a_worktree_session_keeps_its_name_but_a_task_does_not(ag, tmp_path):
    repo = tmp_path / "shop"
    repo.mkdir()
    p = plan(ag, cwd=str(repo), worktree="w1", prompt="p")
    assert p.argv == ["claude", "--worktree", "w1", "--session-id", SID, "--name", "s1", "--", "p"]
    assert plan(ag, cwd=str(repo), worktree="w1", prompt="p", task=True).argv == ["claude", "--worktree", "w1", "--session-id", SID, "--", "p"]


def test_models_may_name_the_1m_variant(ag):
    assert v(ag, {"model": "opus[1m]"}) == {"model": "opus[1m]"} and v(ag, {"model": "claude-opus-5[1m]"}) == {"model": "claude-opus-5[1m]"}
    assert v(ag, {"fallback_model": ["sonnet[1m]", "haiku"]}) == {"fallback_model": ["sonnet[1m]", "haiku"]}
    for bad in ("opus[2m]", "opus[1m", "[1m]", "op[1m]us"):
        with pytest.raises(projects.BadRequest, match="^model: use an alias"):
            v(ag, {"model": bad})


@pytest.mark.parametrize("raw,msg", [
    ({"tools": "Bash;rm"}, "tool pattern not allowed: 'Bash;rm'"),
    ({"agent_name": "-x"}, "agent_name: use letters, digits, '.', '_', ':', '@', '/' or '-'"),
    ({"agent_name": 5}, "agent_name: use letters, digits, '.', '_', ':', '@', '/' or '-'"),
    ({"fallback_model": "a,b,c,d"}, "fallback_model: use up to 3 aliases or model ids, comma separated"),
    ({"fallback_model": "bad model!"}, "fallback_model: use up to 3 aliases or model ids, comma separated"),
    ({"fallback_model": 7}, "fallback_model: use up to 3 aliases or model ids, comma separated"),
    ({"autocompact": "off"}, "autocompact: use auto or a number of tokens (4 to 9 digits)"),
    ({"autocompact": "12"}, "autocompact: use auto or a number of tokens (4 to 9 digits)"),
    ({"autocompact": True}, "autocompact: use auto or a number of tokens (4 to 9 digits)"),
    ({"from_pr": "abc"}, "from_pr: use a pull request number (123 or #123) or its URL"),
    ({"from_pr": "http://github.com/o/r/pull/1"}, "from_pr: use a pull request number (123 or #123) or its URL"),
    ({"from_pr": "-1"}, "from_pr: use a pull request number (123 or #123) or its URL"),
    ({"mode": "yolo"}, "mode must be one of default, read-only, bypass, acceptEdits, plan, auto, dontAsk, bypassPermissions"),
    ({"mode": "custom"}, "mode must be one of default, read-only, bypass, acceptEdits, plan, auto, dontAsk, bypassPermissions"),
])
def test_the_new_options_reject_what_is_not_theirs(ag, raw, msg):
    with pytest.raises(projects.BadRequest) as e:
        v(ag, raw)
    assert str(e.value) == msg


def test_the_mode_word_is_a_permission_mode(ag):
    assert v(ag, {"mode": "default"}) == {"permission_mode": "manual"}
    assert v(ag, {"mode": "read-only"}) == {"permission_mode": "plan"}
    assert v(ag, {"mode": "acceptEdits"}) == {"permission_mode": "acceptEdits"}
    assert v(ag, {"mode": "auto", "permission_mode": "plan"}) == {"permission_mode": "auto"}, "mode wins over a permission_mode beside it"
    assert v(ag, {"mode": "bypass"}) == {}, "a bypass is never stored for resume"
    assert plan(ag, opts={"mode": "bypass"}).argv[-2:] == ["--permission-mode", "bypassPermissions"]
    # a task and a headless run refuse it under either spelling
    for raw in ({"mode": "bypass"}, {"permission_mode": "bypassPermissions"}):
        with pytest.raises(projects.BadRequest, match="bypassPermissions"):
            v(ag, raw, tasks_or_headless=True)
        with pytest.raises(projects.BadRequest, match="^permission_mode must be one of"):
            v(ag, raw, interactive=False)
    # `reasoning` is the other agents' word for the effort
    assert v(ag, {"reasoning": "max"}) == {"effort": "max"} and v(ag, {"effort": "low", "reasoning": "max"}) == {"effort": "low"}


def test_from_pr_is_a_launch_of_its_own(ag):
    p = plan(ag, kind="from_pr", opts={"from_pr": "#123", "model": "opus"})
    assert p.argv == ["claude", "--from-pr", "123", "--model", "opus"] and p.agent_session_id is None
    assert p.opts_clean == {"model": "opus"}, "the PR is for this launch only"
    url = "https://github.com/o/r/pull/12"
    assert plan(ag, kind="from_pr", opts={"from_pr": url}, add_dirs=["/x"]).argv == ["claude", "--from-pr", url, "--add-dir", "/x"]
    assert plan(ag, kind="from_pr", opts={"from_pr": "7"}, bypass=True).argv == ["claude", "--from-pr", "7", "--dangerously-skip-permissions"]
    with pytest.raises(projects.BadRequest, match="^from_pr: give the pull request number or URL$"):
        plan(ag, kind="from_pr")
    with pytest.raises(projects.BadRequest, match="^from_pr: only the from_pr launch takes a pull request$"):
        plan(ag, opts={"from_pr": "7"})
    with pytest.raises(projects.BadRequest, match="first prompt"):
        plan(ag, kind="from_pr", opts={"from_pr": "7"}, prompt="hi")
    with pytest.raises(projects.BadRequest, match="worktree"):
        plan(ag, kind="from_pr", opts={"from_pr": "7"}, worktree="w")
    with pytest.raises(projects.BadRequest, match="^launch kind must be one of new, resume, continue, from_pr$"):
        plan(ag, kind="fork")


def test_fork_session_copies_a_resumed_conversation(ag):
    assert plan(ag, kind="resume", resume_id=RID, opts={"fork_session": True}).argv == ["claude", "--resume", RID, "--fork-session"]
    assert plan(ag, kind="resume", opts={"fork_session": "true"}).argv == ["claude", "--resume", "--fork-session"]
    assert plan(ag, kind="continue", opts={"fork_session": True, "model": "opus"}).argv == ["claude", "--continue", "--fork-session", "--model", "opus"]
    assert plan(ag, kind="resume", resume_id=RID, opts={"fork_session": True}).opts_clean == {}, "a fork is for this launch only"
    assert plan(ag, kind="resume", resume_id=RID, opts={"fork_session": False}).argv == ["claude", "--resume", RID]
    for kind, extra in (("new", {}), ("from_pr", {"from_pr": "1"})):
        with pytest.raises(projects.BadRequest, match="^fork_session: only a resume or continue launch can fork$"):
            plan(ag, kind=kind, opts={"fork_session": True, **extra})


def test_the_mcp_config_file_is_checked_and_stored(ag, tmp_path):
    cfg = settings.claude_config_dir
    cfg.mkdir(parents=True, exist_ok=True)
    f = cfg / "mcp.json"
    f.write_text("{}")
    p = plan(ag, opts={"mcp_config": str(f)})
    assert p.argv[-2:] == ["--mcp-config", str(f)] and p.opts_clean == {"mcp_config": str(f)}
    # resume does not restore it: the stored path is re-passed, and a file that has gone since is Claude's to report, not a launch failure
    assert ag.resume_argv(RID, opts=p.opts_clean)[-2:] == ["--mcp-config", str(f)]
    f.unlink()
    assert ag.resume_argv(RID, opts=p.opts_clean)[-2:] == ["--mcp-config", str(f)]
    what = "mcp_config: use the absolute path of a JSON file inside the projects folder or the Claude config folder"
    for bad in ("mcp.json", "-x", "/etc/passwd", str(tmp_path / "elsewhere.json"), "../x", "", "a\0b"):
        if bad == "":
            assert v(ag, {"mcp_config": bad}) == {}, "an untouched field is not an option"
            continue
        with pytest.raises(projects.BadRequest, match="^" + what + "$"):
            v(ag, {"mcp_config": bad})
    with pytest.raises(projects.BadRequest, match="^mcp_config: no such file$"):
        v(ag, {"mcp_config": str(f)})
    link = cfg / "link.json"                                        # a symlink out of the allowed folders does not pass for inside them
    link.symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(projects.BadRequest):
        v(ag, {"mcp_config": str(link)})


def test_a_first_prompt_is_capped_for_sessions_not_for_tasks(ag, tmp_path):
    repo = tmp_path / "shop"
    repo.mkdir()
    assert plan(ag, prompt="x" * claude.MAX_PROMPT).argv[-1] == "x" * claude.MAX_PROMPT
    with pytest.raises(projects.BadRequest, match="^prompt is too long"):
        plan(ag, prompt="x" * (claude.MAX_PROMPT + 1))
    assert plan(ag, cwd=str(repo), worktree="w", prompt="x" * 20000, task=True).argv[-1] == "x" * 20000


# ---------- v0.5.13: ultracode as a box capability ----------

FAKE_HELP = """Usage: claude [options] [command] [prompt]

Options:
  --effort <level>            Effort level for the current session (low, medium, high, xhigh, max{more})
  --fallback-model <model>    Enable automatic fallback (ultracode is not mentioned here)
  -h, --help                  Display help for command
"""


def fake_claude(path, more="", rc=0):
    """A stand-in `claude` that only answers --help (the capability probe); returns the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\ncat <<'EOF'\n" + FAKE_HELP.format(more=more) + "EOF\nexit " + str(rc) + "\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def probe(monkeypatch, tmp_path):
    monkeypatch.delenv(claude.ULTRACODE_ENV, raising=False)
    claude.reset_caches()
    yield tmp_path / "bin" / "claude"
    claude.reset_caches()


def test_ultracode_is_an_effort_only_where_the_box_takes_it(ag, monkeypatch, probe):
    assert ag.capabilities() == {"ultracode_flag": False} and ag.efforts() == claude.EFFORTS, "no binary: not offered"
    exe = fake_claude(probe)
    monkeypatch.setattr(settings, "claude_bin", lambda: str(exe))
    assert ag.capabilities() == {"ultracode_flag": False}
    assert "ultracode" not in ag.EFFORTS and "ultracode" not in {f.key: f for f in ag.option_schema()}["effort"].choices
    with pytest.raises(projects.BadRequest, match="^effort must be one of low, medium, high, xhigh, max$"):
        v(ag, {"effort": "ultracode"})
    # the box's claude learns it: a new binary (mtime) is probed again, and everything follows: the list, the schema, the validation, argv
    fake_claude(exe, more=", ultracode")
    os.utime(exe, (5, 5))
    assert ag.capabilities() == {"ultracode_flag": True} and ag.efforts()[-1] == "ultracode"
    by = {f.key: f for f in ag.option_schema()}
    assert by["effort"].choices == list(ag.efforts()) == ag.describe()["efforts"] and "accepted" in by["effort"].help
    assert ag.describe()["capabilities"] == {"ultracode_flag": True}
    assert v(ag, {"effort": "ultracode"}) == {"effort": "ultracode"}
    assert plan(ag, opts={"effort": "ultracode"}).argv[-2:] == ["--effort", "ultracode"]


def test_the_ultracode_probe_runs_once_per_binary_and_failures_retry_later(ag, monkeypatch, probe):
    exe = fake_claude(probe, rc=1)
    monkeypatch.setattr(settings, "claude_bin", lambda: str(exe))
    now = {"t": 1000.0}
    monkeypatch.setattr(claude, "_clock", lambda: now["t"])
    calls = []
    real = claude.subprocess.run
    monkeypatch.setattr(claude.subprocess, "run", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    assert ag.capabilities() == {"ultracode_flag": False} and ag.capabilities() == {"ultracode_flag": False}
    assert calls == [[str(exe), "--help"]], "one probe, a failed one cached for a minute"
    now["t"] += 61
    fake_claude(exe, more=", ultracode")
    assert ag.capabilities() == {"ultracode_flag": True} and len(calls) == 2
    assert ag.capabilities() == {"ultracode_flag": True} and len(calls) == 2, "a good answer is kept for this binary"
    # a binary that cannot be run is a plain False, never an error
    claude.reset_caches()
    monkeypatch.setattr(settings, "claude_bin", lambda: str(probe.parent / "missing"))
    assert ag.capabilities() == {"ultracode_flag": False}


def test_the_ultracode_env_records_the_v19_box_check(ag, monkeypatch, probe):
    exe = fake_claude(probe, more=", ultracode")
    monkeypatch.setattr(settings, "claude_bin", lambda: str(exe))
    monkeypatch.setenv(claude.ULTRACODE_ENV, "0")
    assert ag.capabilities() == {"ultracode_flag": False}, "0 says V19 failed, whatever the help says"
    monkeypatch.setenv(claude.ULTRACODE_ENV, "1")
    monkeypatch.setattr(settings, "claude_bin", lambda: None)
    assert ag.capabilities() == {"ultracode_flag": True} and "ultracode" in ag.efforts()


def test_help_lists_ultracode_reads_only_the_effort_option():
    assert claude.help_lists_ultracode(FAKE_HELP.format(more=", ultracode")) is True
    assert claude.help_lists_ultracode(FAKE_HELP.format(more="")) is False
    wrapped = "  --effort <level>   Effort level\n                     (low, medium, ultracode)\n  --model <m>   x\n"
    assert claude.help_lists_ultracode(wrapped) is True
    assert claude.help_lists_ultracode("  --effort <level>   Effort\n  --other <x>   mentions ultracode\n") is False
    assert claude.help_lists_ultracode("") is False and claude.help_lists_ultracode(None) is False
