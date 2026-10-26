"""#329: the #92 harness's 15 cases, with no Node, Codex, tmux or git process.

launcher.test.mjs checks the actual JS payload and preview against the same
fixture; here SessionIn -> _codex_session_opts -> launch_plan checks every token.
"""
import json
import shlex
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import main, projects
from app.agents import codex
from app.agents.base import LaunchReq
from app.config import settings

FIX = Path(__file__).parent / "fixtures"
PARITY = json.loads((FIX / "codex_launcher_parity.json").read_text())


@pytest.fixture
def adapter(monkeypatch):
    caps = codex.parse_help((FIX / "codex_help_0162.txt").read_text())
    monkeypatch.setattr(codex.CodexAgent, "_probe_caps", lambda self: (caps, None))
    monkeypatch.setattr(codex.CodexAgent, "bin", lambda self: "/fake/codex")
    monkeypatch.setattr(codex.CodexAgent, "models", lambda self, **kw: codex.FALLBACK_MODELS)
    monkeypatch.setattr(settings, "codex_hook_trust", "review")

    def no_process(*args, **kwargs):
        pytest.fail("the parity test must not run Codex")

    monkeypatch.setattr(codex, "_run", no_process)
    return codex.CodexAgent()


@pytest.mark.parametrize("case", PARITY["cases"], ids=lambda c: c["id"])
def test_preview_equals_server_argv(case, adapter, tmp_path):
    assert len(PARITY["cases"]) == 15
    assert adapter.launch_caps() == PARITY["ctx"]["caps"]
    body = main.SessionIn(**case["body"])
    agent, _, kind = main._launch_kind(body)
    assert agent == "codex"
    extra = shlex.split(body.args or "")
    assert not adapter.forbidden_extra(extra, interactive=True)
    cwd = tmp_path / "repo"
    if body.worktree:
        cwd = cwd / codex.WORKTREE_DIR / body.worktree_name
    plan = adapter.launch_plan(LaunchReq(
        kind=kind, session_name=body.name or "s1", cwd=str(cwd),
        opts=main._codex_session_opts(body, extra), resume_id=body.resume_id,
        prompt=(body.prompt or "").strip() or None, bypass=body.bypass))
    assert plan.argv == case["tokens"]
    assert shlex.split(plan.cmd_line) == case["tokens"]
    assert plan.cwd == str(cwd)


@pytest.mark.parametrize("profile", ["../x", "a b", "review\n", " review", "x" * 65, "a;b", "$(id)", "é", False, 1, [], {}])
def test_invalid_profile_is_400_without_launch(profile, adapter, projects_dir, fake_tmux, lite_client):
    (projects_dir / "shop" / "api").mkdir(parents=True)
    r = lite_client.post("/api/projects/shop/repos/api/sessions",
                        headers={"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"},
                        json={"launcher": "codex", "name": "s1", "profile": profile})
    assert r.status_code == 400, r.text
    assert "profile" in r.json()["error"]
    assert not fake_tmux["created"] and not fake_tmux["sent"]


@pytest.mark.parametrize("profile", ["review", ".review", "_review", "-review", "x" * 64])
@pytest.mark.parametrize("opts,permissions", [
    ({}, ["-s", "workspace-write", "-a", "on-request"]),
    ({"mode": "custom", "approval": "never"}, ["-s", "workspace-write", "-a", "never"]),
    ({"mode": "custom", "sandbox": "read-only"}, ["-s", "read-only", "-a", "on-request"]),
    ({"mode": "auto"}, ["--approve-for-me", "-s", "workspace-write"]),
])
def test_profile_always_has_explicit_permissions(profile, opts, permissions, adapter):
    body = main.SessionIn(launcher="codex", profile=profile, **opts)
    plan = adapter.launch_plan(LaunchReq(kind="new", opts=main._codex_session_opts(body, [])))
    assert plan.argv[plan.argv.index("-p"):] == ["-p", profile, *permissions]


@pytest.mark.parametrize("opts", [{"sandbox": "danger-full-access"}, {"mode": "bypass"}, {"extra": ["--yolo"]}])
def test_profile_cannot_bypass_danger_checks(opts, adapter):
    with pytest.raises(projects.BadRequest):
        adapter.launch_plan(LaunchReq(kind="new", opts={"profile": "review", **opts}))


def test_nested_opts_still_win_and_false_survives(adapter):
    body = main.SessionIn(launcher="codex", profile="review", no_alt_screen=False,
                          opts={"profile": "ci", "no_alt_screen": True})
    opts = main._codex_session_opts(body, [])
    assert opts["profile"] == "ci" and opts["no_alt_screen"] is True
    body.opts = None
    assert main._codex_session_opts(body, [])["no_alt_screen"] is False


@pytest.mark.parametrize("value", ["false", "true", 0, 1, [], {}])
def test_scrollback_requires_a_json_boolean(value):
    with pytest.raises(ValidationError):
        main.SessionIn(launcher="codex", no_alt_screen=value)


@pytest.mark.parametrize("missing", ["sandbox", "ask_for_approval"])
def test_profile_refused_if_cli_cannot_enforce_permissions(missing, adapter, monkeypatch):
    caps = {**adapter._probe_caps()[0], missing: False}
    monkeypatch.setattr(adapter, "_probe_caps", lambda: (caps, None))
    with pytest.raises(projects.BadRequest, match="cannot enforce"):
        adapter.launch_plan(LaunchReq(kind="new", opts={"profile": "review"}))
