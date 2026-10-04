"""The Codex adapter (app/agents/codex.py, v0.5.11), built against codex-cli 0.145.0 (the box) and newer.

Nothing here runs a real codex: an autouse fixture hides the binary, points settings.codex_home at a temp dir and turns `codex._run`
(the one subprocess seam) into an assertion. Tests that need a probe use the `fake` fixture, which serves canned answers per argv.

The help fixtures are all real captures: codex_help_0157.txt / codex_exec_help_0157.txt are `--help` of codex-cli 0.157.1 (`--no-daemon`,
`--approve-for-me`, `--worktree`); codex_help_0145_real.txt / codex_exec_help_0145_real.txt / codex_resume_help_0145_real.txt are
`codex --help`, `codex exec --help` and `codex resume --help` of the ubu2 box (0.145.0, captured 2026-10-04): its `-a` offers
untrusted | on-request | never (there is NO on-failure), and `codex exec` has neither -a nor --search.
"""
import itertools
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app import agents, projects
from app.agents import codex
from app.agents.base import Agent, Check, HookNorm, LaunchReq
from app.config import settings

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures"
SID = "019a1b2c-3d4e-7f50-8a6b-7c8d9e0f1a2b"
OTHER = "019a1b2c-3d4e-7f50-8a6b-000000000002"
ORIGINAL_RUN = codex._run


def fixture_text(name: str) -> str:
    return (FIX / name).read_text()


HELP_0145, HELP_0157 = fixture_text("codex_help_0145_real.txt"), fixture_text("codex_help_0157.txt")
LOGIN_0145, LOGIN_0160 = fixture_text("codex_login_help_0145.txt"), fixture_text("codex_login_help_0160.txt")   # shaped like clap's output (not captured from a real codex)
EXEC_0145, EXEC_0157 = fixture_text("codex_exec_help_0145_real.txt"), fixture_text("codex_exec_help_0157.txt")
RESUME_0145 = fixture_text("codex_resume_help_0145_real.txt")
MODELS_JSON = fixture_text("codex_models_small.json")


@pytest.fixture(autouse=True)
def _codex_sandbox(tmp_path, monkeypatch):
    """No test here may run the real codex or touch the real ~/.codex."""
    home = tmp_path / "codex-home"
    monkeypatch.setattr(settings, "codex_home", home)
    monkeypatch.setattr(settings, "codex_bin", lambda: None)
    monkeypatch.setattr(settings, "codex_hook_trust", "review")
    monkeypatch.setattr(settings, "projects_dir", tmp_path / "projects")
    monkeypatch.delenv("CCBOARD_REMOTE_APPROVE", raising=False)          # unset = remote approve on (the hooks include PermissionRequest)

    def refuse(argv, timeout=0):
        raise AssertionError(f"unexpected subprocess: {argv}")
    monkeypatch.setattr(codex, "_run", refuse)
    codex.reset_caches()
    assert str(home).startswith(str(tmp_path)) and Path.home() not in home.parents
    yield home
    codex.reset_caches()


@pytest.fixture
def ag():
    return agents.get("codex")


class FakeCodex:
    """A fake codex binary: a real (empty) file for the mtime key, and canned `_run` answers keyed by argv[1:]."""

    def __init__(self, tmp_path, monkeypatch):
        self.path = tmp_path / "bin" / "codex"
        self.path.parent.mkdir()
        self.path.write_text("#!/bin/sh\n")
        self.calls: list[tuple] = []
        self.answers: dict[tuple, object] = {}
        monkeypatch.setattr(settings, "codex_bin", lambda: str(self.path))
        monkeypatch.setattr(codex, "_run", self._run)

    def _run(self, argv, timeout=0):
        assert argv[0] == str(self.path), argv
        key = tuple(argv[1:])
        self.calls.append(key)
        if key not in self.answers:
            raise AssertionError(f"unexpected codex call: {argv}")
        ans = self.answers[key]
        if isinstance(ans, BaseException):
            raise ans
        return ans

    def count(self, *key) -> int:
        return self.calls.count(key)

    def use(self, version="0.145.0", help_text=HELP_0145, exec_text=EXEC_0145, login=(0, "Logged in using ChatGPT\n", ""),
            features="hooks  stable  true\n", mcp=0, models=None, login_help=None):
        self.answers[("--version",)] = (0, f"codex-cli {version}\n", "")
        self.answers[("--help",)] = (0, help_text, "")
        self.answers[("exec", "--help")] = (0, exec_text, "")
        self.answers[("login", "status")] = login
        self.answers[("login", "--help")] = (0, LOGIN_0160 if login_help is None else login_help, "")
        self.answers[("features", "list")] = (0, features, "")
        self.answers[("mcp", "get", "ccboard")] = (mcp, "", "")
        if models is not None:
            self.answers[("debug", "models")] = (0, models, "")
        return self


@pytest.fixture
def fake(tmp_path, monkeypatch):
    return FakeCodex(tmp_path, monkeypatch)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(codex, "_clock", lambda: now[0])
    return now


def plan(ag, **kw):
    kw.setdefault("kind", "new")
    kw.setdefault("session_name", "s1")
    kw.setdefault("cwd", "/p/shop/api")
    return ag.launch_plan(LaunchReq(**kw))


# ---------- registry and interface ----------

def test_registry(ag):
    assert agents.names() == ["claude", "codex"]
    assert [a.name for a in agents.all()] == ["claude", "codex"]
    assert agents.get("codex") is ag and isinstance(ag, Agent) and isinstance(ag, codex.CodexAgent)
    assert (ag.name, ag.label, ag.glyph) == ("codex", "Codex", "◇")
    with pytest.raises(KeyError):
        agents.get("shell")


def test_every_interface_method_is_implemented(ag):
    for n in sorted(Agent.__abstractmethods__):
        assert callable(getattr(ag, n)), n
    assert not any(n in type(ag).__abstractmethods__ for n in Agent.__abstractmethods__)


def test_status_all_lists_codex_without_a_binary():
    st = agents.status_all()
    assert st["codex"] == {"installed": False, "version": None, "loggedIn": False, "glyph": "◇", "hooks": {"installed": False, "trust": "review"}}
    assert st["claude"]["glyph"] == "◆"


def test_fallback_events_match_the_installer():
    import importlib.util
    spec = importlib.util.spec_from_file_location("codex_hooks_t", ROOT / "scripts" / "codex_hooks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert codex.FALLBACK_EVENTS == mod.EVENTS


def test_run_refuses_the_real_codex_home_under_pytest(monkeypatch):
    monkeypatch.setattr(settings, "codex_home", Path.home() / ".codex")
    with pytest.raises(OSError, match="real CODEX_HOME"):
        ORIGINAL_RUN([sys.executable, "-c", "print(1)"])


# ---------- capability probe ----------

CAP_KEYS = ("no_daemon approve_for_me worktree yolo bypass_approvals hook_trust_flag no_alt_screen search add_dir").split()


def test_probe_parses_the_0145_help():
    caps = codex.parse_help(HELP_0145)
    assert {k: caps[k] for k in CAP_KEYS} == {"no_daemon": False, "approve_for_me": False, "worktree": False, "yolo": False,
                                              "bypass_approvals": True, "hook_trust_flag": True, "no_alt_screen": True,
                                              "search": True, "add_dir": True}
    # the box's real -a offers untrusted | on-request | never: there is NO on-failure (R3c of the box check)
    assert not caps["approval_on_failure"] and caps["approval_untrusted"] and caps["ask_for_approval"] and caps["sandbox"]
    assert "on-failure" not in HELP_0145 and "on-failure" not in EXEC_0145 and "on-failure" not in RESUME_0145
    assert caps == {**codex.BASELINE_CAPS}                     # the baseline IS the 0.145 box


def test_probe_parses_the_0157_help():
    caps = codex.parse_help(HELP_0157)
    assert caps["no_daemon"] and caps["approve_for_me"] and caps["worktree"] and caps["no_alt_screen"] and caps["hook_trust_flag"]
    assert not caps["yolo"] and caps["bypass_approvals"]
    assert not caps["approval_on_failure"] and not caps["approval_untrusted"]       # 0.157's -a offers on-request|never only


def test_a_flag_only_mentioned_in_another_options_text_is_not_a_capability():
    text = ("Options:\n  -m, --model <MODEL>\n          Model. Use --no-daemon or --approve-for-me where your build has them\n"
            "  -a, --ask-for-approval <P>\n          [possible values: on-request, never]\n")
    caps = codex.parse_help(text)
    assert not caps["no_daemon"] and not caps["approve_for_me"] and caps["model"] and caps["ask_for_approval"]
    assert codex.parse_help("not a help text at all") is None and codex.parse_help("") is None


def test_exec_caps_never_offer_ask_for_approval_or_search_on_the_real_helps():
    for text in (EXEC_0145, EXEC_0157):
        c = codex.parse_exec_help(text)
        assert c == {"exec_approval": False, "exec_search": False, "exec_cd": True, "exec_output_file": True, "exec_skip_git": True,
                     "exec_json": True}
    assert {k: v for k, v in codex.parse_exec_help(EXEC_0145).items()} == codex.BASELINE_EXEC_CAPS      # the baseline IS the 0.145 box
    doctored = EXEC_0157.replace("  -s, --sandbox", "  -a, --ask-for-approval <P>\n          x\n\n  -s, --sandbox", 1)
    assert codex.parse_exec_help(doctored)["exec_approval"] is True
    doctored = EXEC_0157.replace("  -s, --sandbox", "      --search\n          x\n\n  -s, --sandbox", 1)
    assert codex.parse_exec_help(doctored)["exec_search"] is True
    assert codex.parse_exec_help("junk") is None


def test_no_binary_assumes_the_0145_baseline(ag):
    assert ag.bin() is None and ag.version() is None
    caps = ag.capabilities()
    assert {k: caps[k] for k in CAP_KEYS} == {k: codex.BASELINE_CAPS[k] for k in CAP_KEYS}
    assert caps["exec_approval"] is False


def test_probe_runs_help_once_per_binary_mtime(ag, fake):
    fake.use(help_text=HELP_0157)
    assert ag.capabilities()["no_daemon"] is True
    ag.capabilities()
    plan(ag)
    assert fake.count("--help") == 1 and fake.count("exec", "--help") == 1
    os.utime(fake.path, (1, 1))                                 # a `codex update` replaced the binary
    fake.answers[("--help",)] = (0, HELP_0145, "")
    assert ag.capabilities()["no_daemon"] is False
    assert fake.count("--help") == 2


def test_failed_probe_falls_back_and_is_retried_only_after_a_minute(ag, fake, clock):
    fake.use()
    fake.answers[("--help",)] = (1, "", "boom")
    assert ag.capabilities()["no_daemon"] is False and ag.capabilities()["bypass_approvals"] is True
    assert fake.count("--help") == 1                           # not retried on every call
    clock[0] += codex.FAIL_TTL + 1
    fake.answers[("--help",)] = (0, HELP_0157, "")
    assert ag.capabilities()["no_daemon"] is True
    assert fake.count("--help") == 2
    for bad in (subprocess.TimeoutExpired("codex", 4), FileNotFoundError("gone")):
        codex.reset_caches()
        fake.answers[("--help",)] = bad
        assert ag.capabilities()["no_daemon"] is False          # never raises


def test_argv_on_0145_never_emits_the_flags_it_lacks(ag, fake):
    fake.use(help_text=HELP_0145)
    for opts in ({}, {"permission_mode": "auto"}, {"search": True, "permission_mode": "plan"}):
        argv = plan(ag, opts=opts, prompt="go").argv
        assert "--no-daemon" not in argv and "--approve-for-me" not in argv and "--worktree" not in argv and "--yolo" not in argv
    # the real 0.145 has no on-failure and no --approve-for-me: `auto` is the plain on-request pair
    assert plan(ag, opts={"permission_mode": "auto"}).argv == ["codex", "--no-alt-screen", "-s", "workspace-write", "-a", "on-request"]


def test_argv_on_0157_emits_no_daemon_and_approve_for_me(ag, fake):
    fake.use(version="0.157.1", help_text=HELP_0157, exec_text=EXEC_0157)
    assert plan(ag).argv == ["codex", "--no-daemon", "--no-alt-screen"]
    assert plan(ag, opts={"permission_mode": "auto"}).argv == ["codex", "--no-daemon", "--no-alt-screen", "--approve-for-me",
                                                              "-s", "workspace-write"]
    assert plan(ag, kind="resume", resume_id=SID).argv == ["codex", "resume", "--no-daemon", "--no-alt-screen", SID]
    assert plan(ag, kind="continue").argv == ["codex", "resume", "--no-daemon", "--no-alt-screen", "--last"]
    settings.codex_hook_trust = "bypass"
    assert plan(ag).argv == ["codex", "--no-daemon", "--dangerously-bypass-hook-trust", "--no-alt-screen"]
    with pytest.raises(projects.BadRequest, match="approval must be one of"):
        plan(ag, opts={"approval": "on-failure"})               # 0.157's -a has no on-failure


def test_a_missing_flag_is_skipped_not_emitted(ag, monkeypatch):
    caps = {**codex.BASELINE_CAPS, "no_alt_screen": False, "search": False, "add_dir": False, "profile": False, "hook_trust_flag": False}
    monkeypatch.setattr(ag, "_probe_caps", lambda: (caps, True))
    settings.codex_hook_trust = "bypass"
    p = plan(ag, opts={"search": True, "profile": "work"}, add_dirs=["/p/x"], prompt="hi")
    assert p.argv == ["codex", "--", "hi"]
    caps2 = {**codex.BASELINE_CAPS, "approval_on_failure": True}                         # an older build that does list on-failure
    monkeypatch.setattr(ag, "_probe_caps", lambda: (caps2, True))
    assert plan(ag, opts={"permission_mode": "auto"}).argv[-4:] == ["-a", "on-failure", "-s", "workspace-write"]
    assert plan(ag, opts={"approval": "on-failure"}).argv[-2:] == ["-a", "on-failure"]
    caps3 = {**codex.BASELINE_CAPS}                                                      # neither flag (the 0.145 box): plain on-request
    monkeypatch.setattr(ag, "_probe_caps", lambda: (caps3, True))
    assert plan(ag, opts={"permission_mode": "auto"}).argv[-4:] == ["-s", "workspace-write", "-a", "on-request"]
    with pytest.raises(projects.BadRequest, match="approval must be one of untrusted, on-request, never"):
        plan(ag, opts={"approval": "on-failure"})


def test_bypass_flag_spellings_follow_the_capability(ag, monkeypatch):
    only_yolo = {**codex.BASELINE_CAPS, "bypass_approvals": False, "yolo": True}
    monkeypatch.setattr(ag, "_probe_caps", lambda: (only_yolo, True))
    assert plan(ag, bypass=True).argv == ["codex", "--no-alt-screen", "--yolo"]
    neither = {**codex.BASELINE_CAPS, "bypass_approvals": False, "yolo": False}
    monkeypatch.setattr(ag, "_probe_caps", lambda: (neither, True))
    with pytest.raises(projects.BadRequest, match="no bypass flag"):
        plan(ag, bypass=True)


# ---------- interactive launch_plan ----------

def test_new_default_session_has_no_thread_id_yet(ag):
    p = plan(ag)
    assert p.argv == ["codex", "--no-alt-screen"]
    assert p.cmd_line == "codex --no-alt-screen"
    assert (p.agent_session_id, p.cwd, p.worktree, p.opts_clean) == (None, "/p/shop/api", None, {})
    assert plan(ag, session_id=SID).agent_session_id is None        # Codex picks the thread id; the hook payload binds it


def test_new_with_a_prompt_puts_it_after_double_dash(ag):
    assert plan(ag, prompt="fix the bug").argv == ["codex", "--no-alt-screen", "--", "fix the bug"]
    assert plan(ag, prompt="fix the bug").cmd_line == "codex --no-alt-screen -- 'fix the bug'"
    dash = plan(ag, prompt="-x test").argv
    assert dash[-2:] == ["--", "-x test"] and dash.index("--") < dash.index("-x test")
    bullet = plan(ag, prompt="- first\n- second", opts={"model": "gpt-5.5"}).argv
    assert bullet[-2:] == ["--", "- first\n- second"] and "-m" in bullet[:bullet.index("--")]


def test_option_order_and_quoting(ag):
    opts = {"model": " gpt-5.6-sol ", "reasoning_effort": "high", "permission_mode": "acceptEdits", "search": True, "profile": "work",
            "extra": "--oss"}
    p = plan(ag, opts=opts, add_dirs=["/p/shop/web", "/p/shop/docs"], prompt="go")
    assert p.argv == ["codex", "--no-alt-screen", "-s", "workspace-write", "-a", "on-request", "-m", "gpt-5.6-sol",
                      "-c", 'model_reasoning_effort="high"', "--search", "--add-dir", "/p/shop/web", "--add-dir", "/p/shop/docs",
                      "-p", "work", "--oss", "--", "go"]
    assert p.cmd_line == ("codex --no-alt-screen -s workspace-write -a on-request -m gpt-5.6-sol -c 'model_reasoning_effort=\"high\"' "
                          "--search --add-dir /p/shop/web --add-dir /p/shop/docs -p work --oss -- go")
    assert p.opts_clean == {"model": "gpt-5.6-sol", "reasoning_effort": "high", "permission_mode": "acceptEdits", "search": True,
                            "profile": "work"}                   # extra is a one-off: never stored
    again = plan(ag, opts=p.opts_clean, add_dirs=["/p/shop/web", "/p/shop/docs"], prompt="go").argv      # what a resume re-passes
    assert again == [a for a in p.argv if a != "--oss"]
    # the -c token is exactly the TOML string the CLI parses
    assert 'model_reasoning_effort="high"' in p.argv


@pytest.mark.parametrize("mode,flags", [
    ("default", ["-s", "workspace-write", "-a", "on-request"]),
    ("manual", ["-s", "workspace-write", "-a", "on-request"]),             # Claude's name for the default mode
    ("acceptEdits", ["-s", "workspace-write", "-a", "on-request"]),
    ("plan", ["-s", "read-only", "-a", "on-request"]),
    ("auto", ["-s", "workspace-write", "-a", "on-request"]),               # 0.145 has neither --approve-for-me nor on-failure
    ("dontAsk", ["-s", "workspace-write", "-a", "never"]),
])
def test_interactive_permission_map(ag, mode, flags):
    p = plan(ag, opts={"permission_mode": mode})
    assert p.argv == ["codex", "--no-alt-screen", *flags]
    assert p.opts_clean == {"permission_mode": "default" if mode == "manual" else mode}


def test_sandbox_and_approval_overrides_replace_the_modes_parts(ag):
    assert plan(ag, opts={"permission_mode": "plan", "approval": "never"}).argv[-4:] == ["-s", "read-only", "-a", "never"]
    assert plan(ag, opts={"sandbox": "read-only"}).argv[-2:] == ["-s", "read-only"]
    assert plan(ag, opts={"approval": "untrusted"}).argv[-2:] == ["-a", "untrusted"]
    with pytest.raises(projects.BadRequest):
        plan(ag, opts={"sandbox": "everything"})
    with pytest.raises(projects.BadRequest):
        plan(ag, opts={"approval": "always"})


def test_bypass_is_one_flag_interactive_only_and_never_stored(ag):
    for p in (plan(ag, opts={"permission_mode": "bypassPermissions"}), plan(ag, bypass=True),
              plan(ag, bypass=True, opts={"permission_mode": "plan", "sandbox": "read-only", "approval": "never"})):
        assert p.argv == ["codex", "--no-alt-screen", "--dangerously-bypass-approvals-and-sandbox"]   # every other permission flag dropped
        assert p.opts_clean.get("permission_mode") != "bypassPermissions" and "bypass" not in p.opts_clean
    assert plan(ag, opts={"permission_mode": "bypassPermissions"}).opts_clean == {}
    # sandbox=danger-full-access is bypass-class (decision of the v0.5.11 review): refused without the acknowledgement, and with it the
    # one bypass flag is what runs (the explicit sandbox choice collapses into it); never stored either way
    with pytest.raises(projects.BadRequest, match="danger-full-access: skips the sandbox"):
        plan(ag, opts={"sandbox": "danger-full-access"})
    with pytest.raises(projects.BadRequest, match="bypass: true"):
        plan(ag, opts={"sandbox": "danger-full-access", "approval": "never"})
    danger = plan(ag, opts={"sandbox": "danger-full-access", "model": "gpt-5.5"}, bypass=True)
    assert danger.argv == ["codex", "--no-alt-screen", "--dangerously-bypass-approvals-and-sandbox", "-m", "gpt-5.5"]
    assert danger.opts_clean == {"model": "gpt-5.5"}
    both = plan(ag, opts={"permission_mode": "bypassPermissions", "sandbox": "danger-full-access"})      # that mode is the acknowledgement
    assert both.argv == ["codex", "--no-alt-screen", "--dangerously-bypass-approvals-and-sandbox"] and both.opts_clean == {}
    # approval=never with an explicit sandbox is Claude's dontAsk, not a bypass: it stays an ordinary interactive choice
    assert plan(ag, opts={"sandbox": "workspace-write", "approval": "never"}).argv[-4:] == ["-s", "workspace-write", "-a", "never"]
    assert ag.validate_opts({"sandbox": "danger-full-access"}, interactive=True, tasks_or_headless=False, bypass=True) == {}
    with pytest.raises(projects.BadRequest):
        ag.validate_opts({"sandbox": "danger-full-access"}, interactive=True, tasks_or_headless=False)


def test_resume_and_continue_never_re_pass_a_bypass(ag):
    stored = {"permission_mode": "bypassPermissions", "sandbox": "danger-full-access", "model": "gpt-5.5"}
    for argv in (ag.resume_argv(SID, opts=stored), ag.continue_argv("/p", opts=stored),
                 plan(ag, kind="resume", resume_id=SID, opts=stored).argv, plan(ag, kind="continue", opts=stored).argv):
        assert "--dangerously-bypass-approvals-and-sandbox" not in argv and "danger-full-access" not in argv
        assert argv[argv.index("-m") + 1] == "gpt-5.5"
    # an explicit acknowledgement on the resume itself is the only way back
    assert "--dangerously-bypass-approvals-and-sandbox" in plan(ag, kind="resume", resume_id=SID, bypass=True).argv


def test_hook_trust_flag_only_in_bypass_mode(ag):
    assert "--dangerously-bypass-hook-trust" not in plan(ag).argv
    settings.codex_hook_trust = "bypass"
    p = plan(ag, opts={"permission_mode": "plan"}, prompt="x")
    assert p.argv[:3] == ["codex", "--dangerously-bypass-hook-trust", "--no-alt-screen"]
    assert ag.resume_argv(SID)[:3] == ["codex", "resume", "--dangerously-bypass-hook-trust"]
    assert ag.continue_argv("/p")[-1] == "--last" and "--dangerously-bypass-hook-trust" in ag.continue_argv("/p")
    assert "--dangerously-bypass-hook-trust" not in ag.headless_argv("x", mode="default", max_turns=3, budget=None, extra=[], cwd=None,
                                                                    slug="s", last_message_file=None)


def test_no_alt_screen_is_on_by_default_and_opt_out_is_stored(ag):
    assert "--no-alt-screen" in plan(ag).argv
    p = plan(ag, opts={"no_alt_screen": False})
    assert "--no-alt-screen" not in p.argv and p.opts_clean == {"no_alt_screen": False}
    assert "--no-alt-screen" in plan(ag, opts={"no_alt_screen": "yes"}).argv
    assert plan(ag, opts=p.opts_clean).argv == p.argv


def test_resume_continue_and_resume_by_name(ag):
    assert plan(ag, kind="resume", resume_id=SID).argv == ["codex", "resume", "--no-alt-screen", SID]
    p = plan(ag, kind="resume", resume_id=SID, opts={"model": "gpt-5.5", "permission_mode": "dontAsk"}, add_dirs=["/p/x"])
    assert p.argv == ["codex", "resume", "--no-alt-screen", "-s", "workspace-write", "-a", "never", "-m", "gpt-5.5", "--add-dir", "/p/x", SID]
    assert p.agent_session_id == SID
    assert plan(ag, kind="resume").argv == ["codex", "resume", "--no-alt-screen"]            # the picker
    c = plan(ag, kind="continue")
    assert c.argv == ["codex", "resume", "--no-alt-screen", "--last"] and c.agent_session_id is None
    assert ag.resume_argv(SID, opts={"permission_mode": "plan"}, add_dirs=["/p/x"]) == \
        ["codex", "resume", "--no-alt-screen", "-s", "read-only", "-a", "on-request", "--add-dir", "/p/x", SID]
    assert ag.resume_argv(None, name="my session") == ["codex", "resume", "--no-alt-screen", "my session"]
    assert ag.resume_argv() == ["codex", "resume", "--no-alt-screen"]
    assert ag.continue_argv("/p/shop/api", opts={"model": "gpt-5.5"}) == ["codex", "resume", "--no-alt-screen", "-m", "gpt-5.5", "--last"]
    for bad in ("not-a-uuid", "../etc/passwd", "-x"):
        with pytest.raises(projects.BadRequest):
            ag.resume_argv(bad)
        with pytest.raises(projects.BadRequest):
            plan(ag, kind="resume", resume_id=bad)
    with pytest.raises(projects.BadRequest):
        ag.resume_argv(None, name="--last")                     # a name never starts with '-'
    with pytest.raises(projects.BadRequest, match="first prompt"):
        plan(ag, kind="resume", resume_id=SID, prompt="more")
    with pytest.raises(projects.BadRequest, match="worktree"):
        plan(ag, kind="continue", worktree="w")
    with pytest.raises(projects.BadRequest, match="launch kind"):
        plan(ag, kind="fork")


def test_managed_worktree_task_argv(ag):
    """The task path: ccboard has run `git worktree add -b worktree-<slug> <repo>/.ccboard/worktrees/<slug>`; the session starts there."""
    wt = "/p/shop/api/.ccboard/worktrees/fix-1"
    p = plan(ag, task=True, worktree="fix-1", prompt="fix the login", opts={"permission_mode": "acceptEdits", "model": "gpt-5.5"})
    assert p.argv == ["codex", "--no-alt-screen", "-s", "workspace-write", "-a", "on-request", "-m", "gpt-5.5", "--", "fix the login"]
    assert p.cwd == p.worktree == wt                                # no -C and no --worktree: plan.cwd IS where tmux starts
    assert "-C" not in p.argv and "--worktree" not in p.argv
    assert ag.worktree_strategy() == "managed" and codex.WORKTREE_DIR == ".ccboard/worktrees"
    for bad in ("../x", "a/b", "-x", ""):
        if bad:
            with pytest.raises(projects.BadRequest):
                plan(ag, worktree=bad)


def test_tasks_refuse_every_bypass_route(ag):
    for kw in ({"bypass": True}, {"opts": {"permission_mode": "bypassPermissions"}}, {"opts": {"sandbox": "danger-full-access"}},
               {"opts": {"extra": "--dangerously-bypass-approvals-and-sandbox"}}, {"opts": {"extra": "--yolo"}},
               {"opts": {"extra": "-c approval_policy=never"}},
               {"prompt": "--dangerously-bypass-approvals-and-sandbox do it"}, {"prompt": "--yolo x"}):
        with pytest.raises(projects.BadRequest):
            plan(ag, task=True, **{"prompt": "p", **kw})
    plan(ag, task=True, prompt="-- looks like a flag but is harmless", opts={"permission_mode": "dontAsk"})      # `--` protects it
    with pytest.raises(projects.BadRequest, match="add-dir"):
        plan(ag, task=True, prompt="p", opts={"extra": "--add-dir /etc"})


def test_unsupported_options_are_rejected(ag):
    for key, val in (("allowed_tools", "Bash"), ("disallowed_tools", ["WebFetch"]), ("tools", "x"), ("append_system_prompt", "be brief"),
                     ("fallback_model", "sonnet"), ("fork_session", True), ("from_pr", 12), ("max_turns", 5), ("max_budget_usd", 1.5),
                     ("devcontainer", True)):
        for kw in ({"interactive": True, "tasks_or_headless": False}, {"interactive": True, "tasks_or_headless": True},
                   {"interactive": False, "tasks_or_headless": True}):
            with pytest.raises(projects.BadRequest, match=f"{key}: not supported by codex"):
                ag.validate_opts({key: val}, **kw)
        with pytest.raises(projects.BadRequest, match="not supported by codex"):
            plan(ag, opts={key: val})
    # empty / false values are what a launcher form sends for an untouched field: ignored
    assert ag.validate_opts({"allowed_tools": "", "disallowed_tools": [], "max_turns": 0, "devcontainer": False, "fast": True,
                             "unknown_key": 1}, interactive=True, tasks_or_headless=False) == {}


def test_option_validation_messages(ag):
    bad = [{"model": 5}, {"model": "bad model!"}, {"model": "-x"}, {"reasoning_effort": "ultracode"}, {"reasoning_effort": "ultra"},
           {"reasoning_effort": ["high"]}, {"permission_mode": "yolo"}, {"permission_mode": 3}, {"profile": "../x"}, {"profile": "a b"},
           {"extra": "unterminated 'quote"}, {"extra": 5}]
    for opts in bad:
        with pytest.raises(projects.BadRequest):
            ag.validate_opts(opts, interactive=True, tasks_or_headless=False)
    # Claude's `effort` is an alias for reasoning_effort
    assert ag.validate_opts({"effort": "medium"}, interactive=True, tasks_or_headless=False) == {"reasoning_effort": "medium"}
    assert ag.validate_opts(None, interactive=True, tasks_or_headless=False) == {}
    assert ag.validate_opts({"permission_mode": "bypassPermissions"}, interactive=True, tasks_or_headless=False) == {}
    with pytest.raises(projects.BadRequest, match="never bypasses"):
        ag.validate_opts({"permission_mode": "bypassPermissions"}, interactive=False, tasks_or_headless=True)
    with pytest.raises(projects.BadRequest, match="not allowed for tasks"):
        ag.validate_opts({"permission_mode": "bypassPermissions"}, interactive=True, tasks_or_headless=True)
    with pytest.raises(projects.BadRequest, match="never bypasses"):
        ag.validate_opts({"permission_mode": "sneaky"}, interactive=False, tasks_or_headless=True)


def test_reasoning_levels_are_checked_against_the_models_catalogue(ag, fake, clock):
    fake.use(models=MODELS_JSON)
    ag.models()                                                  # fills the cache from the fixture
    assert ag.validate_opts({"model": "gpt-5.6-sol", "reasoning_effort": "max"}, interactive=True, tasks_or_headless=False)
    with pytest.raises(projects.BadRequest, match="reasoning_effort must be one of low, medium, high, xhigh$"):
        ag.validate_opts({"model": "gpt-5.5", "reasoning_effort": "max"}, interactive=True, tasks_or_headless=False)
    # an unknown model id: the union of what any model offers
    assert ag.validate_opts({"model": "custom-model", "reasoning_effort": "max"}, interactive=True, tasks_or_headless=False)
    assert ag.validate_opts({"reasoning_effort": "xhigh"}, interactive=True, tasks_or_headless=False)
    with pytest.raises(projects.BadRequest):
        ag.validate_opts({"model": "custom-model", "reasoning_effort": "ultra"}, interactive=True, tasks_or_headless=False)


def test_profiles_are_checked_leniently(ag, _codex_sandbox):
    home = _codex_sandbox
    assert plan(ag, opts={"profile": "work"}).argv[-2:] == ["-p", "work"]         # unknown profile: still launches
    assert "work" in ag.opt_warnings({"profile": "work"})[0]
    assert ag.opt_warnings({}) == [] and ag.opt_warnings({"profile": ""}) == [] and ag.opt_warnings(None) == []
    home.mkdir(parents=True)
    (home / "config.toml").write_text('model = "gpt-5.5"\n\n[profiles.work]\nmodel = "gpt-5.6-sol"\n')
    assert ag.opt_warnings({"profile": "work"}) == []
    assert ag.opt_warnings({"profile": "other"})
    (home / "other.config.toml").write_text("model = 'x'\n")
    assert ag.opt_warnings({"profile": "other"}) == []


# ---------- forbidden extra args ----------

BRIEF_TOKENS = ["-c", "--config", "-p", "--profile", "--enable", "--disable", "--strict-config", "--remote", "--remote-auth-token-env"]


@pytest.mark.parametrize("token", BRIEF_TOKENS + ["--config=a=b", "--profile=work", "--remote=ws://h:1", "--ENABLE", "-cmodel=x", "-pwork",
                                                  "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust",
                                                  "--yolo", "--Bypass-Anything", "--full-auto", "-s", "--sandbox=read-only", "-a",
                                                  "--ask-for-approval", "-m", "--model", "-C", "--cd", "--search", "--no-alt-screen",
                                                  "--no-daemon", "--worktree", "--"])
def test_forbidden_extra_tokens(ag, token):
    for interactive, task in ((True, False), (True, True), (False, False)):
        assert ag.forbidden_extra(["--oss", token, "-i"], interactive=interactive, task=task) == token


def test_forbidden_extra_allows_the_harmless_and_scopes_add_dir(ag):
    assert ag.forbidden_extra([], interactive=True) is None and ag.forbidden_extra(None, interactive=False) is None
    assert ag.forbidden_extra(["--oss", "-i", "shot.png", "--color", "never", "--local-provider", "ollama", "-", "x"], interactive=True) is None
    assert ag.forbidden_extra(["--add-dir", "/x"], interactive=True) is None                 # an interactive user may add a directory
    assert ag.forbidden_extra(["--add-dir=/x"], interactive=True, task=True) == "--add-dir=/x"
    assert ag.forbidden_extra(["--add-dir", "/x"], interactive=False) == "--add-dir"
    assert ag.forbidden_extra(["-i", "-cx"], interactive=True) == "-cx"


def test_forbidden_constants_cover_the_brief_for_the_scheduler_to_mirror():
    for tok in ("-c", "--config", "-p", "--profile", "--enable", "--disable", "--strict-config"):
        assert tok in codex.FORBIDDEN_ARG_TOKENS
    assert codex.FORBIDDEN_PREFIX == ("--remote",)
    assert set(codex.FORBIDDEN_ARG_PARTS) == {"dangerously", "yolo", "bypass"}


def test_extra_args_in_validation_messages(ag):
    with pytest.raises(projects.BadRequest, match="-c"):
        ag.validate_opts({"extra": "-c approval_policy=never"}, interactive=True, tasks_or_headless=False)
    with pytest.raises(projects.BadRequest, match="unattended"):
        ag.validate_opts({"extra": ["--profile", "x"]}, interactive=False, tasks_or_headless=True)
    assert plan(ag, opts={"extra": ["--oss", "-i", "a b.png"]}).argv == ["codex", "--no-alt-screen", "--oss", "-i", "a b.png"]
    assert plan(ag, opts={"extra": ["--oss"]}).argv == ["codex", "--no-alt-screen", "--oss"]


# ---------- headless ----------

def headless(ag, **kw):
    kw.setdefault("mode", "default")
    kw.setdefault("max_turns", 30)
    kw.setdefault("budget", None)
    kw.setdefault("extra", [])
    kw.setdefault("cwd", None)
    kw.setdefault("slug", "fix-1")
    kw.setdefault("last_message_file", None)
    return ag.headless_argv("do the thing", **kw)


def test_headless_argv_on_0145(ag, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    out = tmp_path / "last.txt"
    assert headless(ag) == ["codex", "exec", "--json", "-s", "workspace-write", "--", "do the thing"]
    argv = headless(ag, cwd=str(repo), last_message_file=str(out), max_turns=99, budget=2.5)
    assert argv == ["codex", "exec", "--json", "-s", "workspace-write", "-o", str(out), "-C", str(repo), "--", "do the thing"]
    plain = tmp_path / "plain"
    plain.mkdir()
    assert headless(ag, cwd=str(plain)) == ["codex", "exec", "--json", "-s", "workspace-write", "-C", str(plain), "--skip-git-repo-check", "--",
                                            "do the thing"]                                                  # only outside a git repo
    sub = repo / "pkg"
    sub.mkdir()
    assert "--skip-git-repo-check" not in headless(ag, cwd=str(sub))                                          # a repo's subfolder counts


@pytest.mark.parametrize("mode,sandbox", [("default", "workspace-write"), ("manual", "workspace-write"), ("acceptEdits", "workspace-write"),
                                          ("dontAsk", "workspace-write"), ("auto", "workspace-write"), ("plan", "read-only")])
def test_headless_modes_never_prompt_never_bypass(ag, fake, mode, sandbox):
    # `-a never` appears only where `codex exec --help` lists --ask-for-approval; it is never `on-request`, never --ephemeral
    for exec_text, has_a in ((EXEC_0145, False), (EXEC_0157, False),
                             (EXEC_0157.replace("  -s, --sandbox", "  -a, --ask-for-approval <P>\n          x\n\n  -s, --sandbox", 1), True)):
        codex.reset_caches()
        fake.use(exec_text=exec_text)
        argv = headless(ag, mode=mode)
        assert argv[:5] == ["codex", "exec", "--json", "-s", sandbox]
        assert ("-a" in argv) is has_a
        if has_a:
            assert argv[5:7] == ["-a", "never"]
        for forbidden in ("on-request", "--ephemeral", "--dangerously-bypass-approvals-and-sandbox", "--yolo", "--full-auto",
                          "--dangerously-bypass-hook-trust"):
            assert forbidden not in argv


def test_headless_rejects_bypass_and_forbidden_extra(ag):
    for bad_mode in ("bypassPermissions", "yolo", "sneaky"):
        with pytest.raises(projects.BadRequest):
            headless(ag, mode=bad_mode)
    for bad in (["--dangerously-bypass-approvals-and-sandbox"], ["-c", "x=1"], ["--add-dir", "/etc"], ["--profile=p"], ["--yolo"]):
        with pytest.raises(projects.BadRequest, match="unattended"):
            headless(ag, extra=bad)
    assert headless(ag, extra=["--oss"])[-3:] == ["--oss", "--", "do the thing"]
    assert ag.headless_argv("-x", mode="plan", max_turns=1, budget=None, extra=[], cwd=None, slug="s", last_message_file=None)[-2:] == ["--", "-x"]


def test_headless_options_model_reasoning_search_profile(ag):
    argv = headless(ag, opts={"model": "gpt-5.5", "reasoning_effort": "high", "search": True, "profile": "ci", "permission_mode": "plan"})
    assert argv == ["codex", "exec", "--json", "-s", "workspace-write", "-m", "gpt-5.5", "-c", 'model_reasoning_effort="high"',
                    "-p", "ci", "--", "do the thing"]                  # the run's mode wins over opts.permission_mode; no --search: exec has none
    with pytest.raises(projects.BadRequest):
        headless(ag, opts={"sandbox": "danger-full-access"})
    with pytest.raises(projects.BadRequest, match="not supported by codex"):
        headless(ag, opts={"max_turns": 3})


JSONL = "\n".join(json.dumps(e) for e in [
    {"type": "thread.started", "thread_id": SID},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "thinking"}},
    {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "first"}},
    {"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": "All done: 3 files changed"}},
    {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 7}},
])


def test_parse_headless_success(ag):
    r = ag.parse_headless("Reading prompt from stdin...\n" + JSONL + "\n", "", 0)
    assert r == {"text": "All done: 3 files changed", "session_id": SID, "cost": None, "turns": 1, "is_error": False, "subtype": "success",
                 "rate_limited": False, "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 7}}
    assert set(r) >= {"text", "session_id", "cost", "turns", "is_error", "subtype", "rate_limited"}        # the scheduler's keys


def test_parse_headless_failures(ag):
    limit = "\n".join(json.dumps(e) for e in [
        {"type": "thread.started", "thread_id": SID}, {"type": "turn.started"},
        {"type": "error", "message": "You've hit your usage limit. Try again at 3:14 PM."},
        {"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again at 3:14 PM."}}])
    r = ag.parse_headless(limit, "", 1)
    assert (r["is_error"], r["subtype"], r["rate_limited"], r["session_id"]) == (True, "turn_failed", True, SID)
    assert "usage limit" in r["text"]
    other = ag.parse_headless(json.dumps({"type": "turn.failed", "error": {"message": "context window exceeded"}}), "", 1)
    assert other["is_error"] and not other["rate_limited"] and other["text"] == "context window exceeded"
    none = ag.parse_headless("", "codex: command not found\n", 127)
    assert (none["is_error"], none["subtype"], none["text"], none["turns"]) == (True, "no_json", "codex: command not found", None)
    assert ag.parse_headless("", "429 Too Many Requests: rate limit exceeded", 1)["rate_limited"] is True
    assert ag.parse_headless(JSONL, "", 2)["is_error"] is True                                 # a non-zero exit is an error even with a result
    # a successful run that merely talks about rate limits is not rate limited
    talk = JSONL.replace("All done: 3 files changed", "I added a rate limit and a usage limit banner")
    assert ag.parse_headless(talk, "", 0)["rate_limited"] is False
    old = json.dumps({"id": "1", "msg": {"type": "task_complete", "last_agent_message": "older shape"}})
    assert ag.parse_headless(old, "", 0)["text"] == "older shape"


# ---------- models catalogue ----------

def test_parse_models_keeps_visible_models_in_priority_order():
    rows = codex.parse_models(MODELS_JSON)
    assert [r["slug"] for r in rows] == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.5"]               # hidden, malformed and non-dict rows dropped
    assert rows[0] == {"slug": "gpt-5.6-sol", "name": "GPT-5.6 Sol", "reasoning": ["low", "medium", "high", "xhigh", "max"],
                       "default_reasoning": "low"}
    assert rows[2]["reasoning"] == ["low", "medium", "high", "xhigh"]
    assert codex.parse_models("Loading models...\n" + MODELS_JSON)[0]["slug"] == "gpt-5.6-sol"        # a preamble line is tolerated
    for junk in ("", "no json", "{", '{"models": 5}', '{"models": []}', "[]"):
        assert codex.parse_models(junk) == []
    assert set(rows[0]) == {"slug", "name", "reasoning", "default_reasoning"}                   # nothing else of the 240 KB is kept


def test_fallback_models_are_the_boxs_visible_slugs(ag):
    assert [m["slug"] for m in codex.FALLBACK_MODELS] == ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.5", "codex-auto-review"]
    assert not any(m["slug"].startswith("gpt-6") for m in codex.FALLBACK_MODELS)
    assert [m["slug"] for m in ag.models()] == [m["slug"] for m in codex.FALLBACK_MODELS]          # no binary: the fallback, no subprocess
    assert ag.MODELS == tuple(m["slug"] for m in codex.FALLBACK_MODELS)


def test_models_catalogue_is_cached_for_an_hour(ag, fake, clock):
    fake.use(models=MODELS_JSON)
    assert [m["slug"] for m in ag.models()] == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.5"]
    clock[0] += codex.MODELS_TTL - 5
    ag.models()
    assert fake.count("debug", "models") == 1
    clock[0] += 10
    ag.models()
    assert fake.count("debug", "models") == 2                                                 # the hour is up
    assert ag.MODELS == ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.5")
    assert ag.REASONING_BY_MODEL["gpt-5.5"] == ["low", "medium", "high", "xhigh"]
    assert ag.EFFORTS == ("low", "medium", "high", "xhigh", "max")                           # the union of the catalogue (max from sol)


def test_models_failure_keeps_the_fallback_and_retries_after_a_minute(ag, fake, clock):
    fake.use()
    fake.answers[("debug", "models")] = (1, "", "no network")
    assert [m["slug"] for m in ag.models()] == [m["slug"] for m in codex.FALLBACK_MODELS]
    ag.models()
    assert fake.count("debug", "models") == 1
    clock[0] += codex.FAIL_TTL + 1
    fake.answers[("debug", "models")] = (0, MODELS_JSON, "")
    assert ag.models()[0]["slug"] == "gpt-5.6-sol" and fake.count("debug", "models") == 2
    codex.reset_caches()
    fake.answers[("debug", "models")] = subprocess.TimeoutExpired("codex", 8)
    assert ag.models()[0]["slug"] == "gpt-5.6-terra"                                           # never raises


def test_models_fetch_false_never_runs_a_subprocess(ag, fake):
    fake.use(models=MODELS_JSON)
    assert [m["slug"] for m in ag.models(fetch=False)] == [m["slug"] for m in codex.FALLBACK_MODELS]
    assert fake.calls == []


def test_option_schema_and_describe(ag, fake, monkeypatch):
    monkeypatch.setattr(codex.CodexAgent, "_warm_models", lambda self, exe: None)         # no background thread in tests
    fake.use(models=MODELS_JSON, version="0.145.0")
    ag.models()
    fields = {f.key: f for f in ag.option_schema()}
    assert list(fields) == ["model", "reasoning_effort", "permission_mode", "search", "bypass", "sandbox", "approval", "add_dirs",
                            "profile", "no_alt_screen", "worktree", "extra"]
    assert fields["model"].kind == "combo" and fields["model"].choices == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.5"]
    assert fields["reasoning_effort"].choices == ["low", "medium", "high", "xhigh", "max"]
    assert fields["permission_mode"].choices == list(codex.PERMISSION_MODES)
    assert fields["approval"].choices == ["untrusted", "on-request", "never"]                         # 0.145's -a: no on-failure
    assert fields["bypass"].danger is True and fields["bypass"].group == "advanced"
    assert fields["no_alt_screen"].default is True
    for k in ("allowed_tools", "disallowed_tools", "append_system_prompt", "tools", "fallback_model", "fork_session", "from_pr", "max_turns"):
        assert k not in fields
    d = ag.describe()
    json.dumps(d)                                                                            # JSON-serialisable
    assert (d["name"], d["glyph"], d["installed"], d["version"]) == ("codex", "◇", True, "0.145.0")
    assert d["models"] == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.5"] and d["reasoning_by_model"]["gpt-5.6-sol"][-1] == "max"
    assert set(d["slash"]) == {"model", "reasoning"} and d["auth"]["loggedIn"] is True
    codex.reset_caches()
    fake.use(help_text=HELP_0157)
    assert {f.key: f for f in ag.option_schema()}["approval"].choices == ["on-request", "never"]


def test_option_schema_without_a_binary_never_starts_a_thread(ag, monkeypatch):
    started = []
    monkeypatch.setattr(codex.threading, "Thread", lambda *a, **k: started.append(1))
    assert {f.key: f for f in ag.option_schema()}["model"].choices == [m["slug"] for m in codex.FALLBACK_MODELS]
    assert started == []


# ---------- version, auth ----------

def test_version_is_the_bare_semver_cached_per_binary(ag, fake):
    fake.use(version="0.145.0")
    assert ag.version() == "0.145.0" and ag.version() == "0.145.0"
    assert fake.count("--version") == 1
    os.utime(fake.path, (5, 5))
    fake.answers[("--version",)] = (0, "codex-cli 0.157.1\n", "")
    assert ag.version() == "0.157.1"
    codex.reset_caches()
    fake.answers[("--version",)] = (1, "", "oops")
    assert ag.version() is None
    fake.answers[("--version",)] = FileNotFoundError("x")
    codex.reset_caches()
    assert ag.version() is None


def test_auth_status_reduces_login_status_to_a_verdict(ag, fake, clock):
    fake.use(login=(0, "Logged in using ChatGPT\n", ""))
    st = ag.auth_status()
    assert st == {"installed": True, "version": "0.145.0", "loggedIn": True, "authMethod": "chatgpt"}
    ag.auth_status()
    assert fake.count("login", "status") == 1                                                # 60 s cache
    clock[0] += codex.AUTH_TTL + 1
    ag.auth_status()
    assert fake.count("login", "status") == 2
    summary = ag.status_summary()
    assert summary["loggedIn"] and summary["glyph"] == "◇" and summary["authMethod"] == "chatgpt" and summary["hooks"]["trust"] == "review"


def test_auth_status_variants(ag, fake, clock):
    secret = "sk-proj-abcdef0123456789"
    fake.use(login=(0, f"Logged in using an API key - {secret}\n", ""))
    st = ag.auth_status()
    assert st["loggedIn"] and st["authMethod"] == "apikey" and secret not in json.dumps(st)        # the text itself never leaves
    for login, expect_logged, expect_error in (((1, "", "Not logged in\n"), False, False), ((1, "Not logged in\n", ""), False, False),
                                               ((3, "", "weird failure"), False, True), ((0, "Not logged in", ""), False, False)):
        codex.reset_caches()
        fake.answers[("login", "status")] = login
        st = ag.auth_status()
        assert st["loggedIn"] is expect_logged and ("error" in st) is expect_error, login
    codex.reset_caches()
    fake.answers[("login", "status")] = subprocess.TimeoutExpired("codex", 4)
    st = ag.auth_status()
    assert st["loggedIn"] is False and "TimeoutExpired" in st["error"]
    assert ag.auth_status() is not ag.auth_status()                                          # copies: a caller cannot poison the cache


def test_login_and_logout(ag, fake):
    with pytest.raises(projects.BadRequest, match="device-auth"):
        ag.login_start()
    with pytest.raises(projects.BadRequest):
        ag.login_submit_code("abc#def")
    assert ag.login_state() == {"running": False, "url": None, "tail": []}
    fake.use()
    fake.answers[("logout",)] = (0, "Logged out\n", "")
    ag.auth_status()
    assert ag.logout() == {"ok": True, "output": "Logged out"}
    ag.auth_status()
    assert fake.count("login", "status") == 2                                                # logout dropped the cached verdict


# ---------- hooks ----------

def test_hooks_status_roundtrip_through_the_installer(ag, _codex_sandbox):
    assert ag.hooks_status() == {"installed": False, "events": [], "trust": "review", "trusted_events": [], "trusted_hashes": 0}
    ag.install_hooks(ROOT, remote_approve=True, approve_timeout=90)
    hs = ag.hooks_status()
    assert hs["installed"] is True and hs["events"] == [*codex.FALLBACK_EVENTS, "PermissionRequest"]
    assert (_codex_sandbox / "hooks.json").is_file()
    ag.install_hooks(ROOT, remote_approve=True, approve_timeout=90)                          # idempotent
    assert ag.hooks_status() == hs
    ag.uninstall_hooks()
    assert ag.hooks_status()["events"] == [] and not ag.hooks_status()["installed"]
    ag.uninstall_hooks()                                                                      # nothing to do twice


def test_hooks_status_ignores_foreign_hooks_and_bad_files(ag, _codex_sandbox):
    home = _codex_sandbox
    home.mkdir(parents=True)
    f = home / "hooks.json"
    f.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/notify-me"}]}],
                                       "SessionStart": [{"hooks": [{"type": "command", "command": "env CCBOARD_AGENT=codex /x/bin/ccboard-hook"}]}],
                                       "Weird": "not a list"}}))
    hs = ag.hooks_status()
    assert hs["events"] == ["SessionStart"] and hs["installed"] is False
    f.write_text("{ not json")
    assert ag.hooks_status()["events"] == []
    with pytest.raises(RuntimeError, match="not valid JSON"):
        ag.install_hooks(ROOT)
    f.write_text("[]")
    assert ag.hooks_status()["installed"] is False
    with pytest.raises(RuntimeError, match="not a JSON object|valid JSON"):
        ag.uninstall_hooks()
    settings.codex_hook_trust = "bypass"
    assert ag.hooks_status()["trust"] == "bypass"


def test_hook_trust_records_are_read_per_event(ag, _codex_sandbox):
    home = _codex_sandbox
    ag.install_hooks(ROOT)
    hooks_file = home / "hooks.json"
    (home / "config.toml").write_text(
        'model = "gpt-5.5"\n\n'
        f'[hooks.state."{hooks_file}:session_start:0:0"]\ntrusted_hash = "sha256:aaa"\n\n'
        f'[hooks.state."{hooks_file}:user_prompt_submit:0:0"]\ntrusted_hash = "sha256:bbb"\n\n'
        f'[hooks.state."{home / "elsewhere.json"}:stop:0:0"]\ntrusted_hash = "sha256:ccc"\n\n'      # another file: not ours
        f'[hooks.state."{hooks_file}:stop:0:0"]\nenabled = true\n')                                  # no hash: not trusted
    hs = ag.hooks_status()
    assert hs["trusted_events"] == ["SessionStart", "UserPromptSubmit"] and hs["trusted_hashes"] == 3
    (home / "config.toml").write_text("this is [not toml\n# trusted_hash\n")
    hs = ag.hooks_status()
    assert hs["trusted_events"] == [] and hs["trusted_hashes"] == 1                              # invalid TOML: only the plain count
    (home / "config.toml").unlink()
    assert ag.hooks_status()["trusted_hashes"] == 0


# ---------- normalise_hook ----------

def norm(ag, event, **payload):
    return ag.normalise_hook(event, {"session_id": SID, "hook_event_name": event, "cwd": "/p", **payload})


def test_session_start(ag):
    n = norm(ag, "SessionStart", source="startup", transcript_path="/h/.codex/sessions/2026/10/04/rollout-x.jsonl")
    assert (n.state, n.kind, n.event, n.attention, n.ignored) == ("idle", "startup", "SessionStart", False, None)
    assert n.flags["hook_seen"] is True and n.flags["transcript_path"].endswith("rollout-x.jsonl")
    assert n.flags["subagents"] is None and n.flags["compacting"] is None and n.flags["wait_kind"] is None
    assert norm(ag, "SessionStart", source="compact").state is None                              # a compaction is not a new session
    assert "subagents" not in norm(ag, "SessionStart", source="compact").flags
    assert norm(ag, "SessionStart", source="resume").flags["subagents"] is None
    assert norm(ag, "SessionStart", source="clear").kind == "clear"
    assert norm(ag, "SessionStart").state == "idle"
    assert "transcript_path" not in norm(ag, "SessionStart", transcript_path="x" * 5000).flags


def test_prompt_stop_interrupt_end(ag):
    n = norm(ag, "UserPromptSubmit", prompt="fix it", turn_id="t1")
    assert (n.state, n.prompt, n.attention) == ("working", "fix it", False) and n.flags["wait_kind"] is None
    assert norm(ag, "UserPromptSubmit", prompt=5).prompt is None
    s = norm(ag, "Stop", last_assistant_message="All done.", turn_id="t1")
    assert (s.state, s.attention, s.message, s.result) == ("done", True, "All done.", "All done.")
    assert s.flags["last_result"] == "All done." and s.flags["compacting"] is None
    empty = norm(ag, "Stop")
    assert (empty.state, empty.message, empty.result) == ("done", None, None) and empty.flags["last_result"] is None   # apply scrapes the screen
    assert len(norm(ag, "Stop", last_assistant_message="x" * 30000).result) == codex.RESULT_MAX
    i = norm(ag, "Interrupt", turn_id="t1")
    assert (i.state, i.kind, i.event, i.attention, i.limit) == ("idle", "interrupt", "Interrupt", False, None)
    assert i.flags["wait_kind"] is None and i.flags["hook_seen"] is True
    e = norm(ag, "SessionEnd", reason="exit")
    assert (e.state, e.kind) == ("ended", "exit") and e.flags["subagents"] is None
    assert norm(ag, "SessionEnd", reason="clear").state is None and norm(ag, "SessionEnd", reason="resume").state is None
    assert norm(ag, "SessionEnd").state == "ended"


def test_subagents_and_compaction(ag):
    a, b = norm(ag, "SubagentStart", agent_id="a1", agent_type="reviewer"), norm(ag, "SubagentStop", agent_id="a1")
    assert (a.incr, a.kind, a.state) == ({"subagents": 1}, "reviewer", None) and b.incr == {"subagents": -1}
    assert norm(ag, "PreCompact", trigger="auto").flags["compacting"] is True
    assert norm(ag, "PostCompact", trigger="auto").flags["compacting"] is False and norm(ag, "PreCompact", trigger="auto").kind == "auto"
    assert norm(ag, "PermissionRequest", tool_name="Bash").state is None                         # /api/permission owns it
    assert norm(ag, "SomethingNew", matcher="m").kind == "m" and norm(ag, "SomethingNew").state is None


def test_malformed_payloads_never_raise(ag):
    for payload in (None, [], "x", 5, {"session_id": 5, "prompt": ["a"], "last_assistant_message": {"a": 1}, "transcript_path": 7, "source": ["x"],
                                        "reason": 3, "trigger": {}, "agent_type": []}):
        for ev in ("SessionStart", "UserPromptSubmit", "Stop", "Interrupt", "SessionEnd", "SubagentStart", "SubagentStop", "PreCompact",
                   "PostCompact", "PermissionRequest", "Whatever"):
            n = ag.normalise_hook(ev, payload)
            assert isinstance(n, HookNorm) and n.event == ev


def test_thread_relation_for_the_subthread_rule(ag):
    rel = ag.thread_relation
    assert rel("Stop", {"session_id": SID}, SID) == "own"
    assert rel("Stop", {"session_id": SID}, None) == "own" and rel("Stop", {}, SID) == "own" and rel("Stop", None, SID) == "own"
    assert rel("Stop", {"session_id": OTHER}, SID) == "subthread"
    assert rel("UserPromptSubmit", {"session_id": OTHER}, SID) == "subthread"
    assert rel("SessionStart", {"session_id": OTHER, "source": "startup"}, SID) == "subthread"      # a guardian / subagent thread starting
    for src in ("resume", "clear", "fork"):
        assert rel("SessionStart", {"session_id": OTHER, "source": src}, SID) == "rebind"
    assert rel("SessionStart", {"session_id": OTHER, "source": "compact"}, SID) == "subthread"
    assert rel("Stop", {"session_id": OTHER, "source": "resume"}, SID) == "subthread"               # only a SessionStart can rebind
    assert rel("Stop", {"session_id": 5}, SID) == "own"


# ---------- data sources and control surface ----------

def test_transcript_path(ag, _codex_sandbox):
    home = _codex_sandbox
    day = home / "sessions" / "2026" / "10" / "04"
    day.mkdir(parents=True)
    rollout = day / f"rollout-2026-10-04T10-00-00-{SID}.jsonl"
    rollout.write_text("{}\n")
    assert ag.transcript_path({"transcript_path": str(rollout)}) == rollout
    assert ag.transcript_path({"flags": json.dumps({"transcript_path": str(rollout)})}) == rollout
    assert ag.transcript_path({"flags": {"transcript_path": str(rollout)}}) == rollout
    assert ag.transcript_path({"session_id": SID}) == rollout and ag.transcript_path({"agent_session_id": SID}) == rollout
    assert ag.transcript_path({"session_id": OTHER}) is None
    outside = home / "elsewhere" / "rollout-x.jsonl"
    outside.parent.mkdir()
    outside.write_text("{}")
    for bad in (str(outside), str(day / "../../../../etc/passwd.jsonl"), str(day / "x.txt"), 5, "", "x" * 3000):
        assert ag.transcript_path({"transcript_path": bad}) is None
    for junk in (None, {}, "x", {"session_id": "../*"}, {"flags": "not json"}):
        assert ag.transcript_path(junk) is None


def test_cost_join_key_and_usage_sources(ag):
    assert ag.cost_join_key({"period": f"2026/10/04/rollout-2026-10-04T10-00-00-{SID.upper()}"}) == SID
    assert ag.cost_join_key({"sessionId": f"2026/10/04/rollout-x-{SID}.jsonl"}) == SID
    assert ag.cost_join_key({"period": "2026-10-04"}) is None and ag.cost_join_key(None) is None and ag.cost_join_key({}) is None
    assert ag.usage_sources() == ["rollout", "ccusage"]


def test_control_surface(ag):
    slash = ag.slash_commands()
    assert set(slash) == {"model", "reasoning"}
    for k, spec in slash.items():
        assert (spec.cmd, spec.arg, spec.read, spec.verified, spec.weight, spec.hidden) == ("/" + k, True, False, False, 0, True)
    assert ag.exit_command() == "/quit" and ag.worktree_strategy() == "managed"
    assert ag.mcp_register_cmd("/v/python", "/a/ccboard_mcp.py", {"CCBOARD_URL": "http://127.0.0.1:8000", "A": "1"}) == \
        ["codex", "mcp", "add", "ccboard", "--env", "A=1", "--env", "CCBOARD_URL=http://127.0.0.1:8000", "--", "/v/python", "/a/ccboard_mcp.py"]
    assert ag.mcp_register_cmd("py", "s", None) == ["codex", "mcp", "add", "ccboard", "--", "py", "s"]
    assert codex.bind_unbound_rows(object()) == 0                                              # the v0.5.12 seam


# ---------- doctor ----------

def by_id(checks):
    assert all(isinstance(c, Check) and c.group == "codex" for c in checks)
    ids = [c.id for c in checks]
    assert len(ids) == len(set(ids))
    return {c.id: c for c in checks}


IDS = ["codex-bin", "codex-auth", "codex-hooks", "codex-trust", "codex-alt-screen", "codex-features", "codex-sessions", "codex-mcp",
       "codex-repo-hooks"]


def test_doctor_without_codex_skips_everything(ag):
    checks = ag.doctor_checks()
    assert [c.id for c in checks] == IDS and {c.status for c in checks} == {"skip"}
    assert "optional" in checks[0].detail and checks[0].fix["text"]


def test_doctor_all_green_on_a_current_codex(ag, fake, _codex_sandbox):
    home = _codex_sandbox
    fake.use(version="0.157.1", help_text=HELP_0157, exec_text=EXEC_0157)
    ag.install_hooks(ROOT)
    (home / "sessions" / "2026" / "10" / "04").mkdir(parents=True)
    (home / "state_5.sqlite").write_text("x")
    cfg = "".join(f'[hooks.state."{home / "hooks.json"}:{e}:0:0"]\ntrusted_hash = "sha256:x"\n\n'
                  for e in ("session_start", "user_prompt_submit", "stop", "subagent_start", "subagent_stop", "pre_compact", "post_compact",
                            "interrupt", "session_end", "permission_request"))
    (home / "config.toml").write_text(cfg)
    c = by_id(ag.doctor_checks())
    assert list(c) == IDS
    assert {k: v.status for k, v in c.items()} == {k: "pass" for k in IDS}, {k: (v.status, v.detail) for k, v in c.items()}
    assert "0.157.1" in c["codex-bin"].detail and "chatgpt" in c["codex-auth"].detail and "--no-daemon" in c["codex-alt-screen"].detail
    assert "state_5.sqlite" in c["codex-sessions"].detail


def test_doctor_on_the_0145_box_before_install(ag, fake, _codex_sandbox):
    fake.use(version="0.145.0", login=(1, "Not logged in\n", ""), features="hooks  stable  false\n", mcp=1)
    c = by_id(ag.doctor_checks())
    # 0.145 works: a pass, with what newer builds add as information (the adapter probes --help and uses what this build has)
    assert c["codex-bin"].status == "pass" and c["codex-bin"].fix is None
    assert "0.145.0" in c["codex-bin"].detail and "works; 0.157+ adds --approve-for-me and --no-daemon" in c["codex-bin"].detail
    assert (c["codex-auth"].status, c["codex-auth"].fix["cmd"]) == ("fail", "codex login --device-auth")
    assert c["codex-hooks"].status == "fail" and "codex_hooks.py install" in c["codex-hooks"].fix["cmd"]
    assert c["codex-trust"].status == "skip"
    assert c["codex-alt-screen"].status == "pass" and "--no-daemon" not in c["codex-alt-screen"].detail
    assert (c["codex-features"].status, c["codex-features"].fix["cmd"]) == ("fail", "codex features enable hooks")
    assert c["codex-sessions"].status == "skip"
    assert c["codex-mcp"].status == "warn" and c["codex-mcp"].fix["cmd"].startswith("codex mcp add ccboard --env CCBOARD_URL=http://127.0.0.1:")
    assert c["codex-repo-hooks"].status == "pass"


def test_doctor_partial_hooks_and_review_trust(ag, fake, _codex_sandbox):
    home = _codex_sandbox
    fake.use()
    ag.install_hooks(ROOT)
    data = json.loads((home / "hooks.json").read_text())
    del data["hooks"]["Interrupt"]
    (home / "hooks.json").write_text(json.dumps(data))
    c = by_id(ag.doctor_checks())
    assert c["codex-hooks"].status == "warn" and "Interrupt" in c["codex-hooks"].detail
    assert c["codex-trust"].status == "warn" and "no trust record" in c["codex-trust"].detail and c["codex-trust"].fix["cmd"]
    (home / "config.toml").write_text(f'[hooks.state."{home / "hooks.json"}:session_start:0:0"]\ntrusted_hash = "sha256:x"\n')
    c = by_id(ag.doctor_checks())
    assert c["codex-trust"].status == "warn" and "Stop" in c["codex-trust"].detail and "SessionStart" not in c["codex-trust"].detail


def test_doctor_alt_screen_hooks_feature_and_unreadable_answers(ag, fake):
    no_alt = HELP_0145.replace("--no-alt-screen", "--keep-alt-screen")
    fake.use(help_text=no_alt)
    assert by_id(ag.doctor_checks())["codex-alt-screen"].status == "warn"
    codex.reset_caches()
    fake.use()
    fake.answers[("features", "list")] = (0, "multi_agent  stable  true\n", "")
    fake.answers[("mcp", "get", "ccboard")] = subprocess.TimeoutExpired("codex", 4)
    fake.answers[("--help",)] = (1, "", "x")
    c = by_id(ag.doctor_checks())
    assert c["codex-features"].status == "warn"
    assert c["codex-mcp"].status == "warn" and "could not run" in c["codex-mcp"].detail
    assert c["codex-alt-screen"].status == "pass" and "assumed" in c["codex-alt-screen"].detail           # the baseline, flagged as assumed
    codex.reset_caches()
    fake.answers[("--version",)] = (0, "garbage", "")
    fake.answers[("login", "status")] = (9, "", "boom")
    c = by_id(ag.doctor_checks())
    assert c["codex-bin"].status == "warn" and "version unknown" in c["codex-bin"].detail
    assert c["codex-auth"].status == "warn" and "exited 9" in c["codex-auth"].detail


def test_doctor_trust_bypass_and_repo_level_hooks(ag, fake, tmp_path):
    fake.use()
    repo_hooks = tmp_path / "projects" / "shop" / "api" / ".codex" / "hooks.json"
    repo_hooks.parent.mkdir(parents=True)
    repo_hooks.write_text("{}")
    deep = tmp_path / "projects" / "a" / "b" / "c" / "d" / ".codex"
    deep.mkdir(parents=True)
    (deep / "hooks.json").write_text("{}")                                  # four levels down: not scanned
    c = by_id(ag.doctor_checks())
    assert c["codex-repo-hooks"].status == "pass" and "review" in c["codex-repo-hooks"].detail and str(repo_hooks) in c["codex-repo-hooks"].detail
    assert str(deep) not in c["codex-repo-hooks"].detail
    settings.codex_hook_trust = "bypass"
    c = by_id(ag.doctor_checks())
    assert c["codex-trust"].status == "warn" and "--dangerously-bypass-hook-trust" in c["codex-trust"].detail
    assert c["codex-repo-hooks"].status == "warn" and "WITHOUT review" in c["codex-repo-hooks"].detail
    settings.projects_dir = tmp_path / "missing"
    assert by_id(ag.doctor_checks())["codex-repo-hooks"].status == "pass"


def test_doctor_sessions_folder_readability(ag, fake, _codex_sandbox):
    home = _codex_sandbox
    fake.use()
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    assert by_id(ag.doctor_checks())["codex-sessions"].status == "pass"
    sessions.chmod(0o000)
    try:
        if os.access(sessions, os.R_OK):
            pytest.skip("running as a user that ignores permissions")
        c = by_id(ag.doctor_checks())["codex-sessions"]
        assert c.status == "warn" and "sessions" in c.detail
    finally:
        sessions.chmod(0o755)


def test_doctor_runs_its_probes_within_the_checks_budget(ag, fake):
    """The group is one unit under doctor.CHECK_TIMEOUT: every subprocess timeout stays below it and the probes run side by side."""
    from app import doctor
    assert codex.CMD_TIMEOUT < doctor.CHECK_TIMEOUT
    fake.use()
    ag.doctor_checks()
    assert {("--version",), ("login", "status"), ("--help",), ("features", "list"), ("mcp", "get", "ccboard")} <= set(fake.calls)
    assert ("debug", "models") not in fake.calls and ("logout",) not in fake.calls                  # nothing the doctor does changes state


# ---------- the real 0.145 help texts (review fixes: K) ----------

def help_options(text: str) -> set[str]:
    """Every option, short and long, that a clap help text DEFINES (an option line starts at column 2-8)."""
    out: set[str] = set()
    for m in re.finditer(r"^ {2,8}(?:(-[A-Za-z]), )?(--[A-Za-z0-9][A-Za-z0-9-]*)", text, re.M):
        out.add(m.group(2))
        if m.group(1):
            out.add(m.group(1))
    return out


MISSING_ON_0145 = ("on-failure", "--no-daemon", "--approve-for-me", "--yolo", "--worktree", "--full-auto")


def test_the_real_0145_texts_are_the_box_and_agree_with_the_baseline():
    top, exe, res = help_options(HELP_0145), help_options(EXEC_0145), help_options(RESUME_0145)
    assert {"-c", "-s", "-a", "-m", "-p", "-C", "--no-alt-screen", "--search", "--add-dir", "--dangerously-bypass-hook-trust",
            "--dangerously-bypass-approvals-and-sandbox"} <= top
    assert not top & {"--no-daemon", "--approve-for-me", "--worktree", "--yolo", "--full-auto"}
    assert "--ask-for-approval" not in exe and "--search" not in exe and {"--json", "-o", "-C", "--skip-git-repo-check"} <= exe
    assert {"--last", "--no-alt-screen", "--search", "--add-dir", "--ask-for-approval", "--sandbox", "--profile", "--model", "--config",
            "--dangerously-bypass-hook-trust", "--dangerously-bypass-approvals-and-sandbox"} <= res, "resume takes every flag the top level does"
    approval = codex._option_block(HELP_0145, "--ask-for-approval")
    assert all(f"- {v}:" in approval for v in ("untrusted", "on-request", "never")) and "on-failure" not in approval


def test_auto_on_the_real_0145_is_on_request_with_a_workspace_sandbox(ag, fake):
    fake.use(help_text=HELP_0145, exec_text=EXEC_0145)
    assert ag.capabilities()["approval_on_failure"] is False and ag.capabilities()["approve_for_me"] is False
    argv = plan(ag, opts={"permission_mode": "auto"}).argv
    assert argv == ["codex", "--no-alt-screen", "-s", "workspace-write", "-a", "on-request"]
    assert plan(ag, kind="continue", opts={"permission_mode": "auto"}).argv[-5:] == ["-s", "workspace-write", "-a", "on-request", "--last"]
    assert ag._approvals() == ("untrusted", "on-request", "never")


def test_no_argv_for_the_real_0145_carries_a_flag_it_lacks(ag, fake):
    """Every launch / resume / continue / recovery argv the adapter can build against the real 0.145 help: modes x kinds x option sets x
    trust modes x bypass. No flag outside that build, and `codex resume` argvs only use flags the real `codex resume --help` lists."""
    fake.use(help_text=HELP_0145, exec_text=EXEC_0145)
    top, res = help_options(HELP_0145), help_options(RESUME_0145)
    option_sets = ({}, {"model": "gpt-5.5", "reasoning_effort": "high", "search": True, "profile": "work"},
                   {"sandbox": "read-only", "approval": "untrusted"}, {"no_alt_screen": False, "approval": "never"})
    seen = 0
    for mode, opts, trust, bypass, dirs in itertools.product(codex.PERMISSION_MODES, option_sets, ("review", "bypass"), (False, True),
                                                             ([], ["/p/shop/web"])):
        settings.codex_hook_trust = trust
        o = {**opts, "permission_mode": mode}
        builds = [(plan(ag, opts=o, bypass=bypass, add_dirs=dirs, prompt="go").argv, top),
                  (plan(ag, kind="resume", resume_id=SID, opts=o, bypass=bypass, add_dirs=dirs).argv, res),
                  (plan(ag, kind="resume", opts=o, bypass=bypass, add_dirs=dirs).argv, res),
                  (plan(ag, kind="continue", opts=o, bypass=bypass, add_dirs=dirs).argv, res),
                  (ag.resume_argv(SID, opts=o, add_dirs=dirs), res), (ag.resume_argv(None, name="my chat", opts=o), res),
                  (ag.continue_argv("/p", o, dirs), res),
                  (plan(ag, task=True, worktree="w", opts={**o, "permission_mode": mode if mode != "bypassPermissions" else "default"},
                        add_dirs=[], prompt="go").argv, top)]
        for argv, allowed in builds:
            flags = argv[:argv.index("--")] if "--" in argv else argv
            seen += 1
            for tok in flags:
                assert tok not in MISSING_ON_0145, (tok, argv)
                if tok.startswith("-"):
                    assert tok in allowed, (tok, argv)
            assert "on-failure" not in argv
    assert seen == 6 * 4 * 2 * 2 * 2 * 8                      # every combination was built (6 modes, 4 option sets, 2 trust, 2 bypass, 2 dirs)
    settings.codex_hook_trust = "review"


def test_headless_argv_for_the_real_0145_uses_only_flags_codex_exec_lists(ag, fake, tmp_path):
    fake.use(help_text=HELP_0145, exec_text=EXEC_0145)
    exe = help_options(EXEC_0145)
    repo = tmp_path / "plain"
    repo.mkdir()
    for mode in codex.HEADLESS_MODES:
        for opts in ({}, {"model": "gpt-5.5", "reasoning_effort": "high", "search": True, "profile": "ci"}):
            argv = headless(ag, mode=mode, opts=opts or None, cwd=str(repo), last_message_file=str(tmp_path / "o.txt"))
            for tok in argv[2:argv.index("--")]:
                if tok.startswith("-"):
                    assert tok in exe, (tok, argv)
            assert "--search" not in argv and "-a" not in argv and "--ask-for-approval" not in argv


def test_exec_search_is_sent_only_when_codex_exec_lists_it(ag, fake):
    doctored = EXEC_0157.replace("  -s, --sandbox", "      --search\n          x\n\n  -s, --sandbox", 1)
    fake.use(help_text=HELP_0157, exec_text=doctored)
    assert "--search" in headless(ag, opts={"search": True})
    codex.reset_caches()
    fake.use(help_text=HELP_0157, exec_text=EXEC_0157)
    assert "--search" not in headless(ag, opts={"search": True})


# ---------- tasks always carry -s and -a (review fixes: A) ----------

@pytest.mark.parametrize("opts,tail,stored", [
    ({}, ["-s", "workspace-write", "-a", "on-request"], {"permission_mode": "default"}),
    ({"approval": "never"}, ["-s", "workspace-write", "-a", "never"], {"permission_mode": "default", "approval": "never"}),
    ({"approval": "on-request"}, ["-s", "workspace-write", "-a", "on-request"], {"permission_mode": "default", "approval": "on-request"}),
    ({"approval": "untrusted"}, ["-s", "workspace-write", "-a", "untrusted"], {"permission_mode": "default", "approval": "untrusted"}),
    ({"sandbox": "read-only"}, ["-s", "read-only", "-a", "on-request"], {"permission_mode": "default", "sandbox": "read-only"}),
    ({"sandbox": "workspace-write"}, ["-s", "workspace-write", "-a", "on-request"], {"permission_mode": "default", "sandbox": "workspace-write"}),
    ({"sandbox": "read-only", "approval": "never"}, ["-s", "read-only", "-a", "never"],
     {"permission_mode": "default", "sandbox": "read-only", "approval": "never"}),
    ({"permission_mode": "dontAsk"}, ["-s", "workspace-write", "-a", "never"], {"permission_mode": "dontAsk"}),
    ({"permission_mode": "plan", "approval": "never"}, ["-s", "read-only", "-a", "never"], {"permission_mode": "plan", "approval": "never"}),
])
def test_a_task_always_has_both_a_sandbox_and_an_approval_policy(ag, opts, tail, stored):
    p = plan(ag, task=True, worktree="fix-1", prompt="go", opts=opts)
    assert p.argv == ["codex", "--no-alt-screen", *tail, "--", "go"], "config.toml never decides a task's sandbox or approval"
    assert p.opts_clean == stored and ag.validate_opts(opts, interactive=True, tasks_or_headless=True) == stored


def test_a_task_refuses_danger_even_with_an_acknowledgement(ag):
    for kw in ({"opts": {"sandbox": "danger-full-access"}}, {"opts": {"permission_mode": "bypassPermissions"}}, {"bypass": True}):
        with pytest.raises(projects.BadRequest):
            plan(ag, task=True, worktree="w", prompt="go", **kw)
        with pytest.raises(projects.BadRequest):
            plan(ag, task=True, worktree="w", prompt="go", **{**kw, "bypass": True})
    with pytest.raises(projects.BadRequest, match="danger-full-access"):
        ag.validate_opts({"sandbox": "danger-full-access"}, interactive=True, tasks_or_headless=True, bypass=True)
    with pytest.raises(projects.BadRequest, match="danger-full-access"):
        ag.validate_opts({"sandbox": "danger-full-access"}, interactive=False, tasks_or_headless=True, bypass=True)
    with pytest.raises(projects.BadRequest, match="danger-full-access"):
        headless(ag, opts={"sandbox": "danger-full-access"})


def test_stored_options_come_back_without_a_bypass_and_without_an_error(ag):
    """Recovery and resume re-pass stored options: a hand-edited danger-full-access is dropped silently, never an error, never the flag."""
    stored = {"sandbox": "danger-full-access", "model": "gpt-5.5"}
    for argv in (ag.resume_argv(SID, opts=stored), ag.resume_argv(None, name="n", opts=stored), ag.continue_argv("/p", opts=stored)):
        assert "danger-full-access" not in argv and "--dangerously-bypass-approvals-and-sandbox" not in argv
        assert argv[argv.index("-m") + 1] == "gpt-5.5"
    # a fresh request that asks for it on a resume still needs the acknowledgement
    with pytest.raises(projects.BadRequest, match="danger-full-access"):
        plan(ag, kind="resume", resume_id=SID, opts=stored)
    assert "--dangerously-bypass-approvals-and-sandbox" in plan(ag, kind="resume", resume_id=SID, opts=stored, bypass=True).argv


# ---------- the sub-thread rule at rest (review fixes: J) ----------

def test_a_session_start_with_a_new_id_while_the_row_is_at_rest_is_a_rebind(ag):
    rel = ag.thread_relation
    start = {"session_id": OTHER, "source": "startup"}
    for state in ("idle", "done", "ended"):
        assert rel("SessionStart", start, SID, state) == "rebind", state
        assert rel("SessionStart", {"session_id": OTHER}, SID, state) == "rebind", "no source at all"
    for state in ("working", "waiting", "errored", None):
        assert rel("SessionStart", start, SID, state) == "subthread", state          # guardian / subagent threads start mid-turn
    for src in ("resume", "clear", "fork"):
        assert rel("SessionStart", {"session_id": OTHER, "source": src}, SID, "working") == "rebind", "the sources still rebind"
    assert rel("Stop", {"session_id": OTHER}, SID, "idle") == "subthread" and rel("UserPromptSubmit", {"session_id": OTHER}, SID, "done") == "subthread"
    assert rel("SessionStart", start, SID, "idle") == rel("SessionStart", start, SID, state="idle")
    assert rel("SessionStart", {"session_id": SID, "source": "startup"}, SID, "idle") == "own"       # same id: always own


# ---------- the doctor's required events (review fixes: E) ----------

def test_the_permission_hook_is_required_unless_remote_approve_is_off(ag, _codex_sandbox, monkeypatch):
    assert ag._required_events() == [*codex.FALLBACK_EVENTS, "PermissionRequest"] and codex.PERMISSION_EVENT == "PermissionRequest"
    ag.install_hooks(ROOT, remote_approve=False)                       # a board whose hooks lack the permission hook is not healthy...
    hs = ag.hooks_status()
    assert hs["installed"] is False and "PermissionRequest" not in hs["events"]
    monkeypatch.setenv("CCBOARD_REMOTE_APPROVE", "0")                  # ...unless remote approve is off (install.sh's rule)
    assert ag._required_events() == codex.FALLBACK_EVENTS and ag.hooks_status()["installed"] is True
    monkeypatch.setenv("CCBOARD_REMOTE_APPROVE", "1")
    assert ag.hooks_status()["installed"] is False
    ag.install_hooks(ROOT, remote_approve=True)
    assert ag.hooks_status()["installed"] is True
    monkeypatch.setenv("CCBOARD_REMOTE_APPROVE", "0")
    assert ag.hooks_status()["installed"] is True, "extra ccboard hooks never make an install unhealthy"


def test_the_doctor_flags_a_missing_permission_hook_and_not_when_remote_approve_is_off(ag, fake, _codex_sandbox, monkeypatch):
    fake.use()
    ag.install_hooks(ROOT, remote_approve=False)
    c = by_id(ag.doctor_checks())
    assert c["codex-hooks"].status == "warn" and "PermissionRequest" in c["codex-hooks"].detail
    monkeypatch.setenv("CCBOARD_REMOTE_APPROVE", "0")
    assert by_id(ag.doctor_checks())["codex-hooks"].status == "pass"


# ---------- the doctor's version probe runs once and beside the login probe (review fixes: G) ----------

def test_the_doctor_runs_version_once_beside_login_status_not_chained_with_it(ag, fake, monkeypatch):
    """`codex --version` and `codex login status` are both in flight at the same time (a barrier that only trips when they overlap),
    and `--version` runs exactly once: the login probe is handed the version call instead of making its own."""
    fake.use()
    gate = threading.Barrier(2, timeout=2)
    real = fake._run

    def run(argv, timeout=0):
        if tuple(argv[1:]) in (("--version",), ("login", "status")):
            gate.wait()                                   # serial probes would time this out (BrokenBarrierError fails the doctor)
        if tuple(argv[1:]) == ("--version",):
            time.sleep(0.3)                               # still in flight when `login status` is done: a second `--version` would start
        return real(argv, timeout)
    monkeypatch.setattr(codex, "_run", run)
    checks = by_id(ag.doctor_checks())
    assert fake.count("--version") == 1 and fake.count("login", "status") == 1
    assert checks["codex-bin"].status == "pass" and "0.145.0" in checks["codex-bin"].detail
    assert checks["codex-auth"].status == "pass" and "chatgpt" in checks["codex-auth"].detail
    assert ag.auth_status()["version"] == "0.145.0"       # the cached verdict carries the version the doctor resolved


def test_auth_status_takes_a_version_value_or_a_callable_and_keeps_its_old_shape(ag, fake):
    fake.use(version="0.150.2")
    assert ag.auth_status("9.9.9")["version"] == "9.9.9"
    codex.reset_caches()
    calls = []
    assert ag.auth_status(lambda: calls.append(1) or "8.8.8") == {"installed": True, "version": "8.8.8", "loggedIn": True, "authMethod": "chatgpt"}
    assert calls == [1] and fake.count("--version") == 0
    codex.reset_caches()
    assert ag.auth_status()["version"] == "0.150.2" and fake.count("--version") == 1


def test_a_hung_version_probe_does_not_chain_with_the_login_probe(ag, fake):
    fake.use()
    fake.answers[("--version",)] = subprocess.TimeoutExpired("codex", 4)
    c = by_id(ag.doctor_checks())
    assert c["codex-bin"].status == "warn" and "version unknown" in c["codex-bin"].detail
    assert c["codex-auth"].status == "pass" and fake.count("--version") == 1, "the failed probe is remembered, not retried by the login probe"
