"""Nodes epic P7, issue #140: guard_launch, the one function every relay row that starts or steers an agent runs, on the hub (early) and again on the peer.

1. The matrix, table driven: each refused field and each refused spelling, in every string field, for Claude and Codex bodies, with the allowed values passing.
2. The guard agrees with the rules the board already has (recover._is_bypass, which main._task_danger uses, and the adapters' bypass spellings).
3. No refusal repeats a value of the request (a prompt is never echoed).
4. Over HTTP: a synthetic guarded row (tests/conftest.py probe_rows, kept for the scope and human-only cases the real rows do not cover) gives a 422 with the
   message on the hub before any call (no transport call) and on the peer when the hub is bypassed by a direct call with a valid token; the allowed bodies pass.
   The same REFUSED_BODIES run against the three real rows (task_create, task_dispatch, session_open) in tests/test_nodes_remote_tasks.py.

Temp dirs and fakes only: nothing here reaches a real board, tmux, claude or codex.
"""
from __future__ import annotations

import pytest

from app import nodes, nodes_relay as nr
from app.agents import claude as claude_agent, codex as codex_agent

MARKER = "MARKER-prompt-7f3a91"
ALLOWED = [
    {},
    {"agent": "claude", "permission_mode": "default", "prompt": "fix the failing test", "title": "Fix it"},
    {"agent": "claude", "permission_mode": "acceptEdits"},
    {"agent": "claude", "permission_mode": "plan", "model": "opus", "effort": "high"},
    {"agent": "codex", "sandbox": "read-only", "approval": "on-request"},
    {"agent": "codex", "sandbox": "workspace-write", "approval": "on-request", "reasoning_effort": "high"},
    {"agent": "codex", "opts": {"sandbox": "workspace-write", "approval": "on-request", "search": True}},
    {"agent": "codex", "mode": "read-only"},
    {"agent": "claude", "mode": "default"},
    {"permission_mode": None, "sandbox": None, "approval": None, "bypass": False, "args": None, "add_dirs": [], "allowed_tools": "", "agent": None},
    {"prompt": "the word bypass in a sentence, and a danger in prose, and yolo as a word are not the refused spellings"},
    {"prompt": "no flags here: -s workspace-write is how the adapter names it"},
]


def refused(fields) -> list[str]:
    return nr.guard_launch(fields)


@pytest.mark.parametrize("fields", ALLOWED, ids=lambda f: ",".join(f) or "empty")
def test_allowed_values_pass(fields):
    assert refused(fields) == []


# ---------------------------------------------------------------- 1. the matrix: fields

@pytest.mark.parametrize("value", ["bypassPermissions", "dontAsk", "auto", "acceptedits", "Default", "plan ", "x", 1, True, ["plan"], {"a": 1}])
@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_a_permission_mode_outside_the_three_is_refused(agent, value):
    out = refused({"agent": agent, "permission_mode": value})
    assert any("permission_mode must be default, acceptEdits or plan" in m for m in out), out


@pytest.mark.parametrize("value", ["bypass", "auto", "custom", "full", 3])
def test_a_launcher_mode_outside_the_four_is_refused(value):
    assert any(m.startswith("mode must be") for m in refused({"agent": "codex", "mode": value}))


@pytest.mark.parametrize("key", ["bypass", "Bypass", "dangerously_skip_permissions", "dangerouslySkipPermissions", "dangerously-skip-permissions", "yolo", "YOLO",
                                 "allow_dangerously_skip_permissions", "skip_permissions", "skipPermissions", "bypass_ack", "bypassAcknowledged", "acknowledged",
                                 "acknowledge_danger", "acknowledgement", "bypass_permissions", "dangerous", "dangerous_ok"])
@pytest.mark.parametrize("value", [True, "yes", "true", 1, "false"])
def test_any_bypass_flag_or_acknowledgement_that_is_set_is_refused(key, value):
    out = refused({"agent": "claude", key: value})
    assert any("bypass flag or acknowledgement" in m for m in out), (key, value, out)


@pytest.mark.parametrize("key", ["bypass", "yolo", "dangerously_skip_permissions", "acknowledged"])
@pytest.mark.parametrize("value", [False, None, 0, ""])
def test_a_bypass_flag_that_is_off_passes(key, value):
    assert refused({"agent": "claude", key: value}) == []


@pytest.mark.parametrize("key,shown", [("args", "args"), ("extra_args", "args"), ("extraArgs", "args"), ("argv", "args"), ("add_dirs", "add_dirs"),
                                       ("addDirs", "add_dirs"), ("add-dirs", "add_dirs"), ("allowed_tools", "allowed_tools"), ("allowedTools", "allowed_tools"),
                                       ("disallowed_tools", "disallowed_tools"), ("disallowedTools", "disallowed_tools"),
                                       ("append_system_prompt", "append_system_prompt"), ("appendSystemPrompt", "append_system_prompt"),
                                       ("tools", "tools"), ("mcp_config", "mcp_config"), ("config", "config"), ("settings", "settings")])
@pytest.mark.parametrize("value", ["--model x", ["a/b"], "Bash(git *)", {"k": "v"}, "x"])
def test_the_fields_no_remote_model_carries_are_refused(key, shown, value):
    out = refused({"agent": "claude", key: value})
    assert f"{shown} is not part of a request from another node" in out, (key, out)


@pytest.mark.parametrize("key", ["args", "add_dirs", "allowed_tools", "disallowed_tools", "append_system_prompt"])
def test_those_fields_inside_opts_are_refused_too(key):
    assert refused({"agent": "codex", "opts": {key: "x"}}) != []


@pytest.mark.parametrize("value", ["danger-full-access", "full", "read-write", "READ-ONLY", "", 1, True])
def test_a_codex_sandbox_beyond_the_two_is_refused(value):
    out = refused({"agent": "codex", "sandbox": value})
    assert (out != []) == (value != ""), out
    if value != "":
        assert any("sandbox must be read-only or workspace-write" in m for m in out) or any("danger-full-access" in m for m in out)


@pytest.mark.parametrize("value", ["never", "on-failure", "untrusted", "on-request ", "ON-REQUEST", 0.5, True])
def test_a_codex_approval_beyond_on_request_is_refused(value):
    out = refused({"agent": "codex", "approval": value})
    assert any("approval must be on-request" in m for m in out), out


def test_sandbox_and_approval_are_read_inside_opts():
    assert any("sandbox" in m for m in refused({"agent": "codex", "opts": {"sandbox": "danger-full-access"}}))
    assert any("approval" in m for m in refused({"agent": "codex", "opts": {"approval": "never"}}))
    assert any("sandbox" in m for m in refused({"agent": "codex", "opts": {"nested": {"sandbox": "full"}}}))


@pytest.mark.parametrize("agent", ["shell", "bash", "gemini", "Claude", "claude ", "", 5, ["claude"]])
def test_an_unknown_agent_is_refused(agent):
    out = refused({"agent": agent})
    assert (out != []) == (agent != ""), out
    if agent not in ("", "shell"):
        assert any(m.startswith("unknown agent") for m in out)


@pytest.mark.parametrize("launcher", ["shell", "Shell", "shell-resume"])
def test_the_shell_launcher_is_refused(launcher):
    assert "a shell session cannot be started from another node" in refused({"agent": "claude", "launcher": launcher})


@pytest.mark.parametrize("body", [None, [], "x", 3, ["permission_mode"]])
def test_a_body_that_is_no_object_is_refused(body):
    assert refused(body) == ["the request must be a JSON object"]


def test_a_very_deep_body_is_refused_not_walked_forever():
    deep: dict = {}
    cur = deep
    for _ in range(40):
        cur["x"] = {}
        cur = cur["x"]
    assert refused(deep) == ["the request is nested too deeply"]


# ---------------------------------------------------------------- 1. the matrix: spellings anywhere

SPELLED = ["--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox", "--yolo",
           "danger-full-access", "bypassPermissions"]
STRING_FIELDS = ["title", "prompt", "name", "model", "effort", "reasoning_effort", "resume_id", "cwd_rel", "worktree_name", "note", "launcher_text"]


def test_the_spelling_list_is_the_issues():
    assert nr.SPELLINGS == tuple(SPELLED)


@pytest.mark.parametrize("spelling", SPELLED)
@pytest.mark.parametrize("field", STRING_FIELDS)
@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_each_dangerous_spelling_is_refused_in_every_string_field(agent, field, spelling):
    out = refused({"agent": agent, field: f"please run it {spelling} now"})
    assert f"{spelling} is not allowed anywhere in a request from another node" in out, (field, spelling, out)


@pytest.mark.parametrize("spelling", SPELLED)
@pytest.mark.parametrize("make", [
    lambda s: {"opts": {"anything": s}},
    lambda s: {"list": ["fine", s]},
    lambda s: {"list": [{"deep": [s]}]},
    lambda s: {s: "value"},
    lambda s: {"opts": {s: True}},
    lambda s: {"prompt": s.upper()},
    lambda s: {"prompt": s.title()},
    lambda s: {"prompt": "a\u200b" + s[:3] + "\u200d" + s[3:]},                 # zero-width characters inside the spelling
    lambda s: {"prompt": s.replace("-", "\u2011")},                             # a non-breaking hyphen for every dash
    lambda s: {"prompt": s.replace("-", "\u2013")},                             # an en dash
    lambda s: {"prompt": "".join(chr(ord(c) + 0xFEE0) if c.isascii() and c.isalpha() else c for c in s)},       # full-width letters
])
def test_a_spelling_is_found_at_any_depth_in_any_case_and_through_invisible_characters(spelling, make):
    out = refused(make(spelling))
    assert any(spelling in m or "bypass flag" in m or "is not allowed anywhere" in m for m in out), (make(spelling), out)


def test_every_spelling_the_adapters_refuse_for_tasks_is_refused_here_too():
    """The board's own lists: claude.BYPASS_PARTS (dangerously, bypasspermissions) and codex.FORBIDDEN_ARG_PARTS (dangerously, yolo, bypass) read as flags."""
    for part in claude_agent.BYPASS_PARTS:
        flag = "--dangerously-skip-permissions" if part == "dangerously" else "bypassPermissions"
        assert refused({"prompt": flag}), part
    for part in codex_agent.FORBIDDEN_ARG_PARTS:
        flag = {"dangerously": "--dangerously-bypass-approvals-and-sandbox", "yolo": "--yolo", "bypass": "bypassPermissions"}[part]
        assert refused({"prompt": flag}), part


@pytest.mark.parametrize("raw", [{"bypass": True}, {"yolo": 1}, {"dangerously_skip_permissions": "x"}, {"permission_mode": "bypassPermissions"},
                                 {"sandbox": "danger-full-access"}, {"opts": {"bypass": True}}])
def test_whatever_the_boards_own_task_rule_flags_the_guard_flags(raw):
    """main._task_danger (recover._is_bypass) is the board's rule for tasks; a node never gets a laxer one."""
    from app import main
    flat = {**raw, **(raw.get("opts") or {})}
    assert main._task_danger(flat) is not None
    assert refused(raw) != []


def test_a_refusal_never_repeats_a_value_of_the_request():
    """A prompt is never echoed: the messages name fields and the fixed spellings, nothing the person typed."""
    body = {"agent": "claude", "permission_mode": "bypassPermissions " + MARKER, "prompt": MARKER + " --yolo", "args": MARKER, "sandbox": MARKER,
            "approval": MARKER, "mode": MARKER, "opts": {"k": MARKER, "bypass": MARKER}, MARKER: 1}
    out = refused(body)
    assert out and MARKER not in "\n".join(out)
    assert all(len(m) < 160 for m in out)


def test_the_refusals_are_listed_once_each_in_a_stable_order():
    out = refused({"permission_mode": "x", "args": "y", "prompt": "--yolo --yolo", "bypass": True, "bypass2": True})
    assert out == ["permission_mode must be default, acceptEdits or plan", "args is not part of a request from another node",
                   "--yolo is not allowed anywhere in a request from another node", "a bypass flag or acknowledgement is not allowed in a request from another node"]


# ---------------------------------------------------------------- 4. over HTTP, on the hub (early) and on the peer (direct)

REFUSED_BODIES = [
    ({"agent": "claude", "permission_mode": "bypassPermissions"}, "permission_mode must be default, acceptEdits or plan"),
    ({"agent": "claude", "permission_mode": "dontAsk"}, "permission_mode must be default, acceptEdits or plan"),
    ({"agent": "codex", "sandbox": "danger-full-access"}, "sandbox must be read-only or workspace-write"),
    ({"agent": "codex", "approval": "never"}, "approval must be on-request"),
    ({"agent": "codex", "opts": {"sandbox": "danger-full-access"}}, "sandbox must be read-only or workspace-write"),
    ({"agent": "claude", "prompt": "run claude --dangerously-skip-permissions"}, "--dangerously-skip-permissions is not allowed anywhere"),
    ({"agent": "codex", "title": "use --yolo"}, "--yolo is not allowed anywhere"),
    ({"agent": "claude", "args": "--model x"}, "args is not part of a request from another node"),
    ({"agent": "claude", "add_dirs": ["a/b"]}, "add_dirs is not part of a request from another node"),
    ({"agent": "claude", "allowed_tools": "Bash(*)"}, "allowed_tools is not part of a request from another node"),
    ({"agent": "claude", "append_system_prompt": "x"}, "append_system_prompt is not part of a request from another node"),
    ({"agent": "claude", "bypass": True}, "bypass flag or acknowledgement"),
    ({"agent": "gemini"}, "unknown agent"),
    ({"agent": "claude", "launcher": "shell"}, "a shell session cannot be started from another node"),
]
OK_BODIES = [{"agent": "claude", "permission_mode": "plan", "prompt": "hello"}, {"agent": "codex", "sandbox": "read-only", "approval": "on-request"},
             {"agent": "codex", "opts": {"sandbox": "workspace-write"}}]


class Count:
    """The transport with a counter: every call a hub makes to a peer goes through `seen`."""

    def __init__(self, inner):
        self.inner, self.seen = inner, 0

    def __call__(self, *a):
        self.seen += 1
        return self.inner(*a)


@pytest.fixture(autouse=True)
def _hub_online(monkeypatch):
    """The relay waits for the hub to have read the node; these tests are about the guard, so the hub has."""
    from app import main

    class Online:
        def records(self, handle=None):
            return [{"status": "online", "age_s": 1, "polled_at": "2026-10-10T00:00:00Z", "last_ok_at": "2026-10-10T00:00:00Z"}]
    monkeypatch.setattr(main, "_hub", lambda: Online())


@pytest.fixture
def wide(monkeypatch):
    big = lambda: nodes._Limiter(10 ** 6, 60.0, 64)
    monkeypatch.setattr(nodes, "node_read_limiter", big())
    monkeypatch.setattr(nodes, "node_write_limiter", big())


@pytest.mark.parametrize("body,message", REFUSED_BODIES, ids=lambda x: x if isinstance(x, str) else ",".join(x))
def test_the_hub_refuses_before_any_call_and_the_peer_refuses_a_direct_call(two_nodes, pair_up, probe_rows, wide, body, message):
    row = pair_up()
    count = Count(two_nodes.transport)
    nodes.peer_transport = count
    a, b = two_nodes.a, two_nodes.b
    r = a.post(f"/api/nodes/{row['handle']}/probe", json=body)
    assert r.status_code == 422 and message in r.json()["error"] and r.json()["reason"] == "invalid", r.text
    assert count.seen == 0, "the hub refused early: no request reached the peer"
    assert probe_rows.calls == []
    with a.enter():
        token = nodes._load_outgoing(row["peer_id"])
    direct = b.post("/api/node/probe", owner=False, headers={"Authorization": f"Bearer {token}", "X-CCBoard": "1"}, json=body)
    assert direct.status_code == 422 and message in direct.json()["error"], direct.text
    assert probe_rows.calls == [], "the peer applied the guard itself and ran nothing"
    refusals = [x for x in b.db.node_audit_list(50) if x["action"] == "probe_launch"]
    assert refusals and refusals[0]["status"] == "refused" and refusals[0]["direction"] == "in"


@pytest.mark.parametrize("body", OK_BODIES)
def test_the_allowed_bodies_pass_on_both_sides(two_nodes, pair_up, probe_rows, wide, body):
    row = pair_up()
    a, b = two_nodes.a, two_nodes.b
    r = a.post(f"/api/nodes/{row['handle']}/probe", json=body)
    assert r.status_code == 200 and r.json()["data"] == {"ok": True} and r.json()["node"] == row["handle"], r.text
    assert probe_rows.calls == [{k: v for k, v in body.items()}]


def test_a_field_the_model_does_not_have_is_a_422_on_both_sides_even_when_the_guard_is_silent(two_nodes, pair_up, probe_rows, wide):
    row = pair_up()
    r = two_nodes.a.post(f"/api/nodes/{row['handle']}/probe", json={"agent": "claude", "surprise": 1})
    assert r.status_code == 422 and "surprise" in r.json()["error"] and probe_rows.calls == []


# ---------------------------------------------------------------- 5. the launch line: what a remote launch runs with and what a target session runs with (review of P8)
# The strings are the lines the adapters type (tests/test_launcher_api.py shows them), plus the ways a person widens a session on the peer's own board.

UUID1 = "11111111-1111-4111-8111-111111111111"
UUID2 = "22222222-2222-4222-8222-222222222222"


def adapter_lines(codex_ready: bool = True) -> list[tuple[str, str]]:
    """(agent, line) for every line the board's own adapters and tasks.build_command write for a launch with the options a node may send (and the resume, continue and
    fork lines of a session started with them): generated from launch_plan, so this table follows the code. A new flag an adapter adds shows up here and must pass
    launch_line_refusal, or be added to its table on purpose."""
    from app import agents, tasks
    from app.agents.base import LaunchReq
    from app.config import settings
    out: list[tuple[str, str]] = []
    cl = agents.get("claude")
    for pm in ("manual", "acceptEdits", "plan"):
        for extra in ({}, {"model": "sonnet", "effort": "high"}, {"model": "opus[1m]", "effort": "max"}, {"fallback_model": ["sonnet", "haiku"]},
                      {"subagent_model": "haiku"}, {"subagent_model": "sonnet", "subagent_force": True}):
            opts = {"permission_mode": pm, **extra}
            for kind, kw in (("new", {"session_name": "s1"}), ("new", {"session_name": "s1", "prompt": "fix the bug, and say -- 'quoted'"}),
                             ("new", {"session_name": "t-x", "prompt": "- a markdown bullet", "worktree": "t-x", "task": True}),
                             ("new", {"session_name": "s1", "session_id": UUID1}), ("resume", {"resume_id": UUID1}), ("resume", {}), ("continue", {})):
                out.append(("claude", cl.launch_plan(LaunchReq(kind=kind, cwd="/tmp/x", opts=opts, **kw)).cmd_line))
            out.append(("claude", cl.launch_plan(LaunchReq(kind="resume", cwd="/tmp/x", opts={**opts, "fork_session": True}, resume_id=UUID1)).cmd_line))
        argv = cl.launch_opt_args({"permission_mode": pm, "model": "sonnet"})
        out.append(("claude", tasks.build_command("t-x", UUID1, "do $(this) `now`; ok", argv, [])))
        out.append(("claude", tasks.build_command_inplace(UUID1, "- bullet", argv, [])))
    if codex_ready:
        cx = agents.get("codex")
        for hook in ("review", "bypass"):
            saved = settings.codex_hook_trust
            settings.codex_hook_trust = hook
            try:
                for mode in ({"permission_mode": "default"}, {"permission_mode": "plan"}, {"sandbox": "read-only", "approval": "on-request"},
                             {"sandbox": "workspace-write", "approval": "on-request", "model": "gpt-5.5", "reasoning_effort": "high"}):
                    for kind, kw in (("new", {}), ("new", {"prompt": "- fix it"}), ("resume", {"resume_id": UUID2}), ("resume", {"resume_id": "my-session"}),
                                     ("continue", {}), ("fork", {"resume_id": UUID2})):
                        out.append(("codex", cx.launch_plan(LaunchReq(kind=kind, cwd="/tmp/x", opts=mode, **kw)).cmd_line))
            finally:
                settings.codex_hook_trust = saved
    return out


@pytest.fixture
def codex_on(fake_codex, monkeypatch):
    monkeypatch.setattr(codex_agent.CodexAgent, "_warm_models", lambda self, exe: None)


def test_every_line_the_adapters_write_for_the_allowed_modes_passes(codex_on, monkeypatch):
    from app.config import settings
    lines = adapter_lines()
    assert len(lines) > 100 and {a for a, _ in lines} == {"claude", "codex"}
    # the board's own CCBOARD_CODEX_HOOK_TRUST=bypass puts --dangerously-bypass-hook-trust on every Codex line it writes: that flag passes only while that setting is on
    monkeypatch.setattr(settings, "codex_hook_trust", "bypass")
    bad = [(a, c, nr.launch_line_refusal(a, c)) for a, c in lines if nr.launch_line_refusal(a, c)]
    assert bad == [], bad[:3]
    monkeypatch.setattr(settings, "codex_hook_trust", "review")
    bad = [(a, c) for a, c in lines if nr.launch_line_refusal(a, c) and "--dangerously-bypass-hook-trust" not in c]
    assert bad == [], bad[:3]
    assert any("--dangerously-bypass-hook-trust" in c and nr.launch_line_refusal(a, c) for a, c in lines), "refused while the setting is off"


def test_a_flag_an_adapter_adds_later_fails_closed_and_the_adapter_lines_test_would_catch_it(codex_on, monkeypatch):
    from app.agents import claude
    real = claude.ClaudeAgent._opt_args
    monkeypatch.setattr(claude.ClaudeAgent, "_opt_args", lambda self, full: real(self, full) + ["--future-flag", "x"])
    bad = [c for a, c in adapter_lines(codex_ready=False) if a == "claude" and nr.launch_line_refusal(a, c)]
    assert bad and all("--future-flag is on its launch line" == nr.launch_line_refusal("claude", c) for c in bad[:5]), "the table has no such flag: refused until it is added on purpose"
    assert nr.launch_line_refusal("claude", "claude --permission-mode manual --future-flag") == "--future-flag is on its launch line"


SAFE_CLAUDE_LINE = "claude --permission-mode manual --session-id " + UUID1


def widen(*extra: str, base: str = SAFE_CLAUDE_LINE) -> str:
    return base + " " + " ".join(extra)


CLAUDE_REFUSED = [
    # flags that widen what a session may do or change how it is read, in both forms, each on top of an otherwise fine line
    widen("--add-dir /tmp/x"), widen("--add-dir=/tmp/x"), widen("--mcp-config f.json"), widen("--mcp-config=f.json"), widen("--strict-mcp-config"),
    widen("--append-system-prompt hi"), widen("--append-system-prompt=hi"), widen("--system-prompt hi"), widen("--agents '{}'"), widen("--plugin-dir /tmp/p"), widen("--ide"),
    widen("--debug"), widen("--debug=api"), widen("--allowedTools Bash"), widen("--allowed-tools=Bash"), widen("--disallowedTools Bash"), widen("--tools Bash"),
    widen("--settings s.json"), widen("--setting-sources user"), widen("--permission-prompt-tool x"), widen("--dangerously-skip-permissions"),
    widen("--allow-dangerously-skip-permissions"), widen("--from-pr 12"), widen("--agent x"), widen("--autocompact auto"), widen("--verbose"), widen("-p"), widen("-c"),
    # values of known flags that are not the shapes the board writes
    widen("--model -x"), widen("--model ''"), widen("--model 'a;b'"), widen("--model '$(x)'"), widen("--model a/b"), widen("--model " + "m" * 81), widen("--model=-x"),
    widen("--effort turbo"), widen("--effort=''"), widen("--name 'a b'"), widen("--name a--b"), widen("--name -x"), widen("--worktree ../x"), widen("--worktree a/b"),
    widen("--fallback-model a,b,c,d"), widen("--fallback-model 'a;b'"), widen("--session-id notauuid"), widen("--resume notauuid"),
    # the mode: once, from the set, either form
    "claude --permission-mode bypassPermissions", "claude --permission-mode=bypassPermissions", "claude --permission-mode auto", "claude --permission-mode dontAsk",
    "claude --permission-mode=auto", "claude --permission-mode manual --permission-mode manual", "claude --permission-mode manual --permission-mode=plan",
    "claude --permission-mode plan --permission-mode bypassPermissions", "claude --permission", "claude --permission-mod manual", "claude --permission-mode",
    "claude --permission-mode=", "claude", "claude --name s1", "claude --session-id " + UUID1,
    # repeated or abbreviated flags
    widen("--model a --model b"), widen("--session-id " + UUID2), widen("--effort high --effort low"), widen("--wor x"), widen("--sess " + UUID1),
    # a flag, or anything else, after the prompt separator
    SAFE_CLAUDE_LINE + " -- prompt --dangerously-skip-permissions", SAFE_CLAUDE_LINE + " -- prompt more", SAFE_CLAUDE_LINE + " -- a -- b", SAFE_CLAUDE_LINE + " --",
    SAFE_CLAUDE_LINE + " stray", "claude stray --permission-mode manual",
    # environment prefixes: only the two names the board sets itself, with values of their shape
    "env ANTHROPIC_BASE_URL=http://evil claude --permission-mode manual", "env CLAUDE_CODE_USE_BEDROCK=1 claude --permission-mode manual",
    "ANTHROPIC_API_KEY=x claude --permission-mode manual", "env CLAUDE_CODE_SUBAGENT_MODEL='a;b' claude --permission-mode manual",
    "env CLAUDE_CODE_SUBAGENT_MODEL=haiku CLAUDE_CODE_SUBAGENT_MODEL=opus claude --permission-mode manual", "env CLAUDE_CODE_SUBAGENT_MODEL_FORCE=2 claude --permission-mode manual",
    "env claude --permission-mode manual", "env -i claude --permission-mode manual", "env PATH=/tmp/evil claude --permission-mode manual",
    "env CLAUDE_CODE_SUBAGENT_MODEL=haiku codex -s read-only -a on-request",
    # wrappers
    "sh -c 'claude --permission-mode manual'", "bash -lc 'claude --permission-mode manual'", "nice claude --permission-mode manual", "timeout 5 claude --permission-mode manual",
    "devcontainer up --workspace-folder /tmp/x && devcontainer exec --workspace-folder /tmp/x -- claude --permission-mode manual", "sudo claude --permission-mode manual",
    "/usr/bin/claude --permission-mode manual", "./claude --permission-mode manual", "claude.sh --permission-mode manual", "exec claude --permission-mode manual",
    # chaining and substitution, unquoted
    SAFE_CLAUDE_LINE + " ; rm -rf x", SAFE_CLAUDE_LINE + "; rm -rf x", SAFE_CLAUDE_LINE + " && curl x", SAFE_CLAUDE_LINE + " | sh", SAFE_CLAUDE_LINE + " || true",
    SAFE_CLAUDE_LINE + " `id`", SAFE_CLAUDE_LINE + " $(id)", SAFE_CLAUDE_LINE + " > /tmp/x", SAFE_CLAUDE_LINE + " &", SAFE_CLAUDE_LINE + " -- $(id)",
    SAFE_CLAUDE_LINE + " -- `id`", SAFE_CLAUDE_LINE + " -- a;b", "claude --name $(id) --permission-mode manual", "claude --permission-mode manual\nrm -rf x",
    "echo hi; claude --permission-mode manual", "cd /tmp && claude --permission-mode manual",
    # not tokenisable, not in canonical form, not a line at all
    "claude --permission-mode 'manual", "claude  --permission-mode manual", " claude --permission-mode  manual", "claude --permission-mode \"manual\"", "zsh", "", "   ", None, 5,
    "codex -s workspace-write -a on-request",
]


@pytest.mark.parametrize("cmd", CLAUDE_REFUSED, ids=lambda c: repr(c)[:70])
def test_a_claude_line_that_is_not_exactly_a_line_the_board_writes_is_refused(cmd):
    got = nr.launch_line_refusal("claude", cmd)
    assert got and isinstance(got, str), cmd


SAFE_CODEX_LINE = "codex --no-alt-screen -s workspace-write -a on-request"


def cwiden(*extra: str) -> str:
    return SAFE_CODEX_LINE + " " + " ".join(extra)


CODEX_REFUSED = [
    cwiden("-c foo=bar"), cwiden("-c sandbox_mode=danger-full-access"), cwiden("-c 'sandbox_mode=\"danger-full-access\"'"), cwiden("-c approval_policy=never"),
    cwiden("-c model_reasoning_effort=high"), cwiden("-c 'model_reasoning_effort=\"hi;gh\"'"), cwiden("--config foo=bar"), cwiden("--config=foo=bar"),
    cwiden("-c check_for_update_on_startup=true"), cwiden("-c check_for_update_on_startup=false -c check_for_update_on_startup=false"), cwiden("-c"),
    cwiden("--profile x"), cwiden("--profile=x"), cwiden("-p x"), cwiden("--full-auto"), cwiden("--dangerously-bypass-approvals-and-sandbox"), cwiden("--yolo"),
    cwiden("--dangerously-bypass-hook-trust"), cwiden("--sandbox read-only"), cwiden("--sandbox=read-only"), cwiden("--ask-for-approval on-request"),
    cwiden("--ask-for-approval=on-request"), cwiden("--add-dir /tmp/x"), cwiden("--add-dir=/tmp/x"), cwiden("--cd /tmp/x"), cwiden("-C /tmp/x"), cwiden("--search"),
    cwiden("--approve-for-me"), cwiden("--oss"), cwiden("--enable x"), cwiden("--image f.png"), cwiden("-i f.png"), cwiden("--model x"), cwiden("-m -x"), cwiden("-m 'a;b'"),
    cwiden("-s read-only"), cwiden("-a on-request"), cwiden("-s=read-only"), cwiden("-sread-only"),
    "codex --no-alt-screen -s danger-full-access -a on-request", "codex --no-alt-screen -s workspace-write -a never", "codex --no-alt-screen -s workspace-write -a on-failure",
    "codex --no-alt-screen -s workspace-write -a untrusted", "codex --no-alt-screen -s workspace-write", "codex --no-alt-screen -a on-request", "codex --no-alt-screen",
    "codex", "codex -s workspace-write -a on-request --last", "codex fork --last", "codex exec -s workspace-write -a on-request", "codex resume -s workspace-write -a on-request a b",
    "codex resume a -s workspace-write -a on-request", "codex resume -s workspace-write -a on-request '-x'", "codex login -s workspace-write -a on-request",
    SAFE_CODEX_LINE + " -- prompt more", SAFE_CODEX_LINE + " -- prompt --yolo", SAFE_CODEX_LINE + " stray", SAFE_CODEX_LINE + " ; rm -rf x", SAFE_CODEX_LINE + " && id",
    SAFE_CODEX_LINE + " | sh", SAFE_CODEX_LINE + " $(id)", SAFE_CODEX_LINE + " -- $(id)",
    "env CLAUDE_CODE_SUBAGENT_MODEL=haiku " + SAFE_CODEX_LINE, "env OPENAI_BASE_URL=http://evil " + SAFE_CODEX_LINE, "OPENAI_API_KEY=x " + SAFE_CODEX_LINE,
    "sh -c '" + SAFE_CODEX_LINE + "'", "nice " + SAFE_CODEX_LINE, "timeout 5 " + SAFE_CODEX_LINE, "/usr/local/bin/codex -s workspace-write -a on-request",
    "claude --permission-mode manual",
]


@pytest.mark.parametrize("cmd", CODEX_REFUSED, ids=lambda c: repr(c)[:70])
def test_a_codex_line_that_is_not_exactly_a_line_the_board_writes_is_refused(cmd, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "codex_hook_trust", "review")
    got = nr.launch_line_refusal("codex", cmd)
    assert got and isinstance(got, str), cmd


def test_the_one_function_serves_both_uses():
    """The target-session check and the final-plan check of a remote launch are the same function: a line one refuses, the other refuses with the same reason."""
    from app import projects
    line = widen("--add-dir /tmp/x")
    why = nr.launch_line_refusal("claude", line)
    with pytest.raises(projects.BadRequest) as e:
        nr.require_remote_launch("claude", line)
    assert why in str(e.value)
    assert nr.launch_line_refusal("claude", SAFE_CLAUDE_LINE) is None


@pytest.mark.parametrize("agent,opts", [("claude", {"permission_mode": "auto"}), ("claude", {"permission_mode": "dontAsk"}), ("claude", {"permission_mode": "bypassPermissions"}),
                                        ("codex", {"permission_mode": "auto"}), ("codex", {"permission_mode": "dontAsk"})])
def test_stored_options_outside_the_set_refuse_even_when_the_line_looks_fine(agent, opts):
    cmd = "claude --permission-mode manual" if agent == "claude" else "codex -s workspace-write -a on-request"
    assert "stored permission mode" in nr.launch_line_refusal(agent, cmd, opts)
    assert nr.launch_line_refusal(agent, cmd, {"permission_mode": "plan"}) is None


@pytest.mark.parametrize("agent,args,want", [
    ("claude", {}, {"permission_mode": "manual"}),
    ("claude", {"permission_mode": "default"}, {"permission_mode": "manual"}),
    ("claude", {"mode": "default"}, {"permission_mode": "manual"}),
    ("claude", {"permission_mode": "acceptEdits"}, {"permission_mode": "acceptEdits"}),
    ("claude", {"mode": "read-only"}, {"permission_mode": "plan"}),
    ("claude", {"permission_mode": "plan", "mode": "acceptEdits"}, {"permission_mode": "acceptEdits"}),
    ("codex", {}, {"sandbox": "workspace-write", "approval": "on-request"}),
    ("codex", {"permission_mode": "plan"}, {"sandbox": "read-only", "approval": "on-request"}),
    ("codex", {"mode": "read-only"}, {"sandbox": "read-only", "approval": "on-request"}),
    ("codex", {"sandbox": "read-only"}, {"sandbox": "read-only", "approval": "on-request"}),
    ("codex", {"approval": "on-request"}, {"sandbox": "workspace-write", "approval": "on-request"}),
    (None, {}, {"permission_mode": "manual"}),
])
def test_an_omitted_permission_is_resolved_to_an_explicit_safe_value(agent, args, want):
    assert nr.safe_launch(agent, **args) == want


@pytest.mark.parametrize("agent,args", [("claude", {"permission_mode": "auto"}), ("claude", {"mode": "bypass"}), ("codex", {"sandbox": "danger-full-access"}),
                                        ("codex", {"approval": "never"}), ("codex", {"mode": "custom"}), ("claude", {"permission_mode": "bypassPermissions"})])
def test_a_word_outside_the_set_is_not_resolved_to_anything(agent, args):
    from app import projects
    with pytest.raises(projects.BadRequest):
        nr.safe_launch(agent, **args)


def test_the_final_options_are_guarded_with_manual_read_as_default():
    assert nr.guard_final({"permission_mode": "manual", "subagent_model": "inherit", "subagent_force": False, "opts": {"sandbox": "workspace-write", "approval": "on-request"}}) == []
    assert nr.guard_final({"permission_mode": "auto"}) and nr.guard_final({"permission_mode": "bypassPermissions"}) and nr.guard_final({"opts": {"approval": "never"}})
    assert nr.guard_final({"permission_mode": "manual", "allowed_tools": "Bash"}) and nr.guard_final({"permission_mode": "manual", "model": "x --dangerously-skip-permissions"})


def test_require_remote_launch_raises_before_anything_starts():
    from app import projects
    nr.require_remote_launch("claude", "claude --permission-mode manual --name s")
    with pytest.raises(projects.BadRequest) as e:
        nr.require_remote_launch("claude", "claude --name s")
    assert "nothing was started" in str(e.value)


class _Db:
    def __init__(self, rows):
        self.rows = rows

    def open_rows(self):
        return self.rows


@pytest.fixture
def tmux_has(monkeypatch):
    from app import tmux
    live = set()
    monkeypatch.setattr(tmux, "has_session", lambda name: name in live)
    return live


TASK = {"project": "shop", "repo": "api"}


def row(**kw):
    return {"agent": "claude", "launcher": "claude", "project": "shop", "repo": "api", "cmd": "claude --permission-mode manual --name s1", "opts": {"permission_mode": "manual"}, **kw}


@pytest.mark.parametrize("name,rows,live,fragment", [
    ("not-a-session", {}, True, "not a ccboard session name"),
    ("_ccboard-login", {}, True, "not a ccboard session name"),
    ("shop--api--s1", {}, True, "not a session this board started"),                                           # tmux has it, the board has no row: foreign
    ("shop--api--s1", {"shop--api--s1": row(agent="shell", launcher="shell")}, True, "not an agent session"),
    ("shop--api--s1", {"shop--api--s1": row(launcher="clone")}, True, "not an agent session"),
    ("shop--api--s1", {"shop--api--s1": row(agent="gemini")}, True, "not an agent session"),
    ("shop--api--s1", {"shop--api--s1": row(launcher="weird")}, True, "not an agent session"),                   # an allow list of launchers, not a deny list
    ("shop--api--s1", {"shop--api--s1": row(project="shop", repo="other")}, True, "another repo"),                 # the row's own project and repo too, not only the name
    ("shop--other--s1", {"shop--other--s1": row()}, True, "another repo"),
    ("other--api--s1", {"other--api--s1": row()}, True, "another repo"),
    ("shop--api--s1", {"shop--api--s1": row(cmd=None)}, True, "wider permissions"),
    ("shop--api--s1", {"shop--api--s1": row(cmd="claude --dangerously-skip-permissions")}, True, "wider permissions"),
    ("shop--api--s1", {"shop--api--s1": row(opts={"permission_mode": "auto"})}, True, "wider permissions"),
    ("shop--api--s1", {"shop--api--s1": row(agent="codex", cmd="codex -s workspace-write -a never", opts={})}, True, "wider permissions"),
])
def test_the_target_of_a_session_dispatch_must_be_one_of_the_boards_own_sessions_in_the_tasks_repo_inside_the_set(tmux_has, name, rows, live, fragment):
    if live:
        tmux_has.add(name)
    assert fragment in nr.session_target_refusal(_Db(rows), TASK, name)


def test_a_target_inside_the_set_and_a_session_tmux_does_not_have_are_left_to_the_board(tmux_has):
    tmux_has.add("shop--api--s1")
    assert nr.session_target_refusal(_Db({"shop--api--s1": row()}), TASK, "shop--api--s1") is None
    assert nr.session_target_refusal(_Db({"shop--api--s1": row(agent="codex", cmd="codex -s read-only -a on-request", opts={})}), TASK, "shop--api--s1") is None
    assert nr.session_target_refusal(_Db({}), TASK, "shop--api--gone") is None, "the board answers 404 itself"


# ---------------------------------------------------------------- 6. every string a remote caller sends has a rule of its own (one helper: nr.check_field / nr.free_text)

def _leaf_types(tp):
    import typing
    if typing.get_origin(tp) is typing.Literal:
        return set()
    args = typing.get_args(tp)
    return {tp} if not args else set().union(*(_leaf_types(a) for a in args))


@pytest.mark.parametrize("model", [nr.TaskCreateBody, nr.DispatchBody, nr.SessionOpenBody, nr.PromptBody, nr.KeysBody], ids=lambda m: m.__name__)
def test_every_string_field_of_a_remote_model_has_a_validator_so_a_field_added_later_is_caught(model):
    validated = {f for d in model.__pydantic_decorators__.field_validators.values() for f in d.info.fields}
    strs = [name for name, f in model.model_fields.items() if str in _leaf_types(f.annotation)]
    assert strs, model
    assert [n for n in strs if n not in validated] == [], "a str field with no validator: give it a rule in nr.check_field"
    assert all(k in nr.FIELD_KINDS for k in ("title", "prompt", "agent", "model", "effort", "name", "session", "issue_ref", "permission_mode", "mode", "sandbox", "approval",
                                              "text", "key", "decision"))


def test_every_model_a_row_uses_is_strict_and_every_str_field_of_every_row_has_a_validator():
    """Walks the table, not a list of models: a row added later with a str field that has no rule fails here by itself."""
    for row in nr.RELAY:
        model = row.body_model
        assert model.model_config.get("extra") == "forbid", row.name
        validated = {f for d in model.__pydantic_decorators__.field_validators.values() for f in d.info.fields}
        strs = [name for name, f in model.model_fields.items() if str in _leaf_types(f.annotation)]
        assert [n for n in strs if n not in validated] == [], (row.name, "a str field with no validator")
    assert all(nr._valid_param(p, "!") is False for r in nr.RELAY for p in r.params), "every path parameter has a rule that an arbitrary string does not pass"


ROW_BASE = {"task_create": {"project": "shop", "repo": "api", "title": "t", "prompt": "p"}, "session_open": {"project": "shop", "repo": "api"}, "task_dispatch": {}}
# field -> (rows that take it, a good value)
FIELDS = {
    "agent": (("task_create", "task_dispatch", "session_open"), "claude"), "model": (("task_create", "task_dispatch", "session_open"), "sonnet"),
    "effort": (("task_create", "task_dispatch", "session_open"), "high"), "reasoning_effort": (("task_create", "task_dispatch", "session_open"), "high"),
    "project": (("task_create", "session_open"), "shop"), "repo": (("task_create", "session_open"), "api"), "name": (("session_open",), "scratch"),
    "title": (("task_create",), "Add login"), "prompt": (("task_create",), "do it"), "issue_ref": (("task_create",), "acme/api#1"),
    "permission_mode": (("session_open",), "plan"), "mode": (("session_open",), "default"), "sandbox": (("session_open",), "read-only"), "approval": (("session_open",), "on-request"),
}
FREE = {"title", "prompt"}
# values no patterned field takes: a control character, a leading dash, shell metacharacters, a space, a newline, a look-alike (full-width), a bidi control, too long
PLAIN_BAD = ["x\x03", "x\x1b", "x\x00", "x\x7f", "-x", "--x", "a;b", "a|b", "a&b", "$(id)", "`id`", "a>b", "a<b", "a'b", 'a"b', "a\\b", "a b", " x", "x ", "x\n", "x\r", "\nx",
             "ｍodel", "x‮", "x​", "x ", "x ", "x﻿", "x" * 300, "", "é", "a/b", "..", "a=b", "a*b", "a$b", "a#b", "a!b"]
FREE_BAD = ["x\x03", "x\x04", "x\x1b[2J", "x\x00", "x\x7f", "a\rb", "x\x85", "x ", "x ", "x‮", "x⁦", "x​", "x‍", "x﻿", "x­", "x" if False else "x⁠"]


def _row(name):
    return next(r for r in nr.RELAY if r.name == name)


def _validate(rowname, field, value):
    row = _row(rowname)
    params = {"tid": "1"} if rowname == "task_dispatch" else {}
    body = {**ROW_BASE[rowname], field: value}
    return nr.validate(row, params, body)


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_a_good_value_of_each_field_passes_on_every_row_that_takes_it(field):
    rows, good = FIELDS[field]
    for r in rows:
        assert _validate(r, field, good)[field] == good


@pytest.mark.parametrize("field", sorted(set(FIELDS) - FREE))
def test_a_patterned_field_refuses_control_characters_a_leading_dash_shell_metacharacters_look_alikes_and_length(field):
    rows, good = FIELDS[field]
    for r in rows:
        for bad in PLAIN_BAD:
            with pytest.raises(nr.Invalid) as e:
                _validate(r, field, bad)
            assert "\x03" not in str(e.value) and "\x1b" not in str(e.value) and "id)" not in str(e.value), "a refusal never repeats the value"


@pytest.mark.parametrize("field", sorted(FREE))
def test_free_text_refuses_control_format_and_line_separator_characters_and_length(field):
    for bad in FREE_BAD:
        with pytest.raises(nr.Invalid):
            _validate("task_create", field, "start " + bad + " end")
    cap = nr.TASK_TITLE_IN if field == "title" else nr.TASK_PROMPT_IN
    with pytest.raises(nr.Invalid):
        _validate("task_create", field, "x" * (cap + 1))
    assert _validate("task_create", field, "x" * cap)[field] == "x" * cap
    for ok in ("tab\there", "two\nlines", "crlf\r\nlines", "accents é ü", "an emoji \U0001F600", "quotes 'a' \"b\" $(not run) `nor this` ; | & > <"):
        assert _validate("task_create", field, ok)[field] == ok.replace("\r\n", "\n")


def test_the_session_field_of_a_dispatch_is_a_ccboard_session_name_exactly():
    row = _row("task_dispatch")
    for bad in ["shop--api--s1\n", " shop--api--s1", "shop--api--s1 ", "shop--api--", "_ccboard-login", "shop--api--s1\x03", "ｓhop--api--s1", "shop--api--" + "x" * 200, "shop--api--a--b",
                "a/b--c--d", "-shop--api--s1", "shop--api--s1;id", "shop--api--$(id)"]:
        with pytest.raises(nr.Invalid):
            nr.validate(row, {"tid": "1"}, {"mode": "session", "session": bad})
    assert nr.validate(row, {"tid": "1"}, {"mode": "session", "session": "shop--api--s1"})["session"] == "shop--api--s1"


def test_path_parameters_and_stream_names_use_the_same_session_rule():
    for bad in ["shop--api--s1\n", "shop--api--s1 ", "ｓhop--api--s1", "_ccboard-login", "shop--api--" + "x" * 200, "shop--api--s1\x00", "a--b"]:
        assert nr._valid_param("name", bad) is False
        assert nr.stream_names_error(bad), bad
    assert nr._valid_param("name", "shop--api--s1") is True and nr.stream_names_error("shop--api--s1,shop--api--s2") is None
    assert nr.stream_names_error("shop--api--s1,") and nr.stream_names_error("") and nr.stream_names_error(",".join(f"shop--api--s{i}" for i in range(21)))
    assert nr.stream_names_error(" shop--api--s1") and nr.stream_names_error("shop--api--s1, shop--api--s2")
    for bad in ["1x", "", "1 ", "٣", "-1", "1" * 13]:
        assert nr._valid_param("tid", bad) is False


CARD = {"title": "Add login", "prompt": "do it\nthen stop"}


@pytest.mark.parametrize("task,spec,want", [
    (CARD, {}, None),
    (CARD, {"model": "sonnet", "effort": "high", "permission_mode": "plan", "subagent_model": "haiku", "auto_close": False, "opts": {"sandbox": "read-only", "approval": "on-request"}}, None),
    (CARD, {"model": "", "opts": {}, "allowed_tools": ""}, None),
    ({**CARD, "prompt": "a b"}, {}, "text"), ({**CARD, "prompt": "a\x03b"}, {}, "text"), ({**CARD, "title": "a‮b"}, {}, "text"), ({**CARD, "title": "a​b"}, {}, "text"),
    ({**CARD, "prompt": "x" * 20001}, {}, "text"),
    (CARD, {"model": "-x"}, "options"), (CARD, {"model": "a;b"}, "options"), (CARD, {"effort": "hi\x03"}, "options"), (CARD, {"reasoning_effort": "$(id)"}, "options"),
    (CARD, {"permission_mode": "auto"}, "options"), (CARD, {"permission_mode": "bypassPermissions"}, "options"), (CARD, {"subagent_model": "a;b"}, "options"),
    (CARD, {"args": "--x"}, "options"), (CARD, {"add_dirs": ["a/b"]}, "options"), (CARD, {"allowed_tools": "Bash"}, "options"), (CARD, {"a_new_key": "x"}, "options"),
    (CARD, {"opts": {"profile": "p"}}, "options"), (CARD, {"opts": {"search": True}}, "options"), (CARD, {"opts": {"sandbox": "danger-full-access"}}, "options"),
    (CARD, {"opts": {"approval": "never"}}, "options"), (CARD, {"opts": ["x"]}, "options"),
])
def test_a_stored_card_is_checked_by_the_same_rules_as_a_request_body(task, spec, want):
    assert nr.card_refusal(task, spec) == want


def test_the_final_options_are_an_allow_list_not_a_deny_list():
    assert nr.guard_final({"permission_mode": "manual", "model": "sonnet", "effort": "high", "agent": "claude", "subagent_model": "inherit",
                           "opts": {"sandbox": "workspace-write", "approval": "on-request"}, "name": "scratch"}) == []
    for f in ({"mcp_servers": "x"}, {"cwd_rel": "x"}, {"devcontainer": True}, {"worktree": "x"}, {"prompt": "x"}, {"fast": True}, {"search": True}, {"profile": "p"},
              {"model": "-x"}, {"effort": "hi;gh"}, {"agent": "claude;x"}, {"opts": {"profile": "p"}}, {"opts": {"sandbox": "danger-full-access"}}, {"opts": "x"},
              {"name": "a b"}, {"subagent_model": "a;b"}, {"sandbox": "full"}, {"approval": "never"}):
        assert nr.guard_final(f), f


@pytest.mark.parametrize("prompt,want", [("do it", None), ("@file.py look", None), ("run tests / fix", None), ("wow!", None), ("a\n/b\n!c", None),
                                         ("/model", "command"), ("!ls", "command"), ("#note", "command"), ("／model", "command"), ("！ls", "command"), ("＃n", "command"),
                                         ("  \n /x", "command"), ("a\x03b", "text")])
def test_a_stored_card_may_not_start_like_a_terminal_command_in_any_dispatch_mode(prompt, want):
    card = {"title": "t", "prompt": prompt}
    assert nr.card_refusal(card, {}, typed=True) == want
    assert nr.card_refusal(card, {}) == (None if want == "command" else want), "only the remote dispatch asks for the command rule"


def test_the_prompt_models_apply_the_command_rule_to_a_steer_text_and_to_a_new_tasks_prompt():
    from pydantic import ValidationError
    for bad in ("/model", "!ls", "#x", "！ls", " \n/x"):
        with pytest.raises(ValidationError, match="cannot start with / ! or #"):
            nr.PromptBody(text=bad)
        with pytest.raises(ValidationError, match="cannot start with / ! or #"):
            nr.TaskCreateBody(project="p", repo="r", title="t", prompt=bad)
    assert nr.PromptBody(text="@f.py ok. wow!").text == "@f.py ok. wow!"
    assert nr.TaskCreateBody(project="p", repo="r", title="t", prompt="@f.py review /x").prompt == "@f.py review /x"
    with pytest.raises(ValidationError, match="at most"):
        nr.TaskCreateBody(project="p", repo="r", title="t", prompt="x" * (nr.TASK_PROMPT_IN + 1))
    assert nr.check_field("typed_prompt", "x" * nr.TASK_PROMPT_IN)
