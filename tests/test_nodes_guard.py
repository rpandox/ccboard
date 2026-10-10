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

CLAUDE_OK = [
    "claude --permission-mode manual --session-id 11111111-1111-4111-8111-111111111111 --name s1",
    "claude --permission-mode default --name s1",
    "claude --model sonnet --effort high --permission-mode acceptEdits --worktree t-x --session-id 11111111-1111-4111-8111-111111111111 -- fix the bug",
    "env CLAUDE_CODE_SUBAGENT_MODEL=haiku claude --permission-mode plan --name s1",
    "claude --permission-mode=plan --name s1",
    "claude --permission-mode manual --session-id x -- the prompt may say bypassPermissions or --dangerously-skip-permissions and is not read",
]
CLAUDE_WIDE = [
    ("claude --permission-mode bypassPermissions --name s1", "bypassPermissions"),
    ("claude --permission-mode=bypassPermissions --name s1", "bypass"),
    ("claude --dangerously-skip-permissions --name s1", "dangerously"),
    ("claude --allow-dangerously-skip-permissions --permission-mode manual", "dangerously"),
    ("claude --permission-mode auto --name s1", "outside the allowed set"),
    ("claude --permission-mode dontAsk", "outside the allowed set"),
    ("claude --permission-mode manual --permission-mode bypassPermissions", "bypass"),
    ("claude --permission-mode plan --permission-mode auto", "outside the allowed set"),
    ("claude --permission-mode", "outside the allowed set"),
    ("claude --permission-mode manual --allowedTools Bash", "allowedtools"),
    ("claude --permission-mode manual --allowed-tools Bash", "allowed-tools"),
    ("claude --permission-mode manual --settings /tmp/x.json", "--settings"),
    ("claude --permission-mode manual --setting-sources user", "--setting-sources"),
    ("claude --name s1", "sets no permission mode"),
    ("claude", "sets no permission mode"),
    ("zsh", "cannot be read"),
    ("", "cannot be read"),
    (None, "cannot be read"),
    ("claude --permission-mode 'manual", "cannot be read"),
    ("codex -s workspace-write -a on-request", "cannot be read"),
]


@pytest.mark.parametrize("cmd", CLAUDE_OK)
def test_a_claude_line_that_says_a_mode_inside_the_set_is_inside_it(cmd):
    assert nr.launch_line_refusal("claude", cmd) is None


@pytest.mark.parametrize("cmd,why", CLAUDE_WIDE, ids=lambda x: str(x)[:50])
def test_a_claude_line_wider_than_the_set_or_silent_about_its_mode_is_refused(cmd, why):
    got = nr.launch_line_refusal("claude", cmd)
    assert got and why.lower() in got.lower(), got


CODEX_OK = [
    "codex --no-alt-screen -s workspace-write -a on-request",
    "codex --no-alt-screen -s read-only -a on-request -m gpt-5.5 -c 'model_reasoning_effort=\"high\"'",
    "codex resume --no-alt-screen -a on-request -s workspace-write 019a-uuid",
    "codex --no-daemon -c check_for_update_on_startup=false --no-alt-screen -s workspace-write -a on-request -- danger-full-access in a prompt",
    "codex --sandbox=read-only --ask-for-approval=on-request",
]
CODEX_WIDE = [
    ("codex --no-alt-screen -s danger-full-access -a on-request", "danger-full-access"),
    ("codex --no-alt-screen -s workspace-write -a never", "outside the allowed set"),
    ("codex --no-alt-screen -s workspace-write -a on-failure", "outside the allowed set"),
    ("codex --no-alt-screen -s workspace-write -a untrusted", "outside the allowed set"),
    ("codex --no-alt-screen -s workspace-write", "no sandbox or no approval"),
    ("codex --no-alt-screen -a on-request", "no sandbox or no approval"),
    ("codex --no-alt-screen", "no sandbox or no approval"),
    ("codex --dangerously-bypass-approvals-and-sandbox", "dangerously"),
    ("codex --yolo", "--yolo"),
    ("codex -s workspace-write -a on-request --approve-for-me", "--approve-for-me"),
    ("codex -s workspace-write -a on-request --full-auto", "--full-auto"),
    ("codex -s workspace-write -a on-request -c 'sandbox_mode=\"danger-full-access\"'", "danger-full-access"),
    ("codex -s workspace-write -a on-request -c approval_policy=never", "-c line"),
    ("codex -s workspace-write -a on-request --config=sandbox_workspace_write.network_access=true", "-c line"),
    ("codex -s read-only -s danger-full-access -a on-request", "danger-full-access"),
    ("claude --permission-mode manual", "cannot be read"),
]


@pytest.mark.parametrize("cmd", CODEX_OK)
def test_a_codex_line_with_both_flags_inside_the_set_is_inside_it(cmd):
    assert nr.launch_line_refusal("codex", cmd) is None


@pytest.mark.parametrize("cmd,why", CODEX_WIDE, ids=lambda x: str(x)[:50])
def test_a_codex_line_wider_than_the_set_or_with_a_flag_missing_is_refused(cmd, why):
    got = nr.launch_line_refusal("codex", cmd)
    assert got and why.lower() in got.lower(), got


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
    return {"agent": "claude", "launcher": "claude", "cmd": "claude --permission-mode manual --name s1", "opts": {"permission_mode": "manual"}, **kw}


@pytest.mark.parametrize("name,rows,live,fragment", [
    ("not-a-session", {}, True, "not a ccboard session name"),
    ("_ccboard-login", {}, True, "not a ccboard session name"),
    ("shop--api--s1", {}, True, "not a session this board started"),                                           # tmux has it, the board has no row: foreign
    ("shop--api--s1", {"shop--api--s1": row(agent="shell", launcher="shell")}, True, "not an agent session"),
    ("shop--api--s1", {"shop--api--s1": row(launcher="clone")}, True, "not an agent session"),
    ("shop--api--s1", {"shop--api--s1": row(agent="gemini")}, True, "not an agent session"),
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
