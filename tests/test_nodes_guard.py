"""Nodes epic P7, issue #140: guard_launch, the one function every relay row that starts or steers an agent runs, on the hub (early) and again on the peer.

1. The matrix, table driven: each refused field and each refused spelling, in every string field, for Claude and Codex bodies, with the allowed values passing.
2. The guard agrees with the rules the board already has (recover._is_bypass, which main._task_danger uses, and the adapters' bypass spellings).
3. No refusal repeats a value of the request (a prompt is never echoed).
4. Over HTTP: a synthetic guarded row (tests/conftest.py probe_rows; this phase ships only read rows) gives a 422 with the message on the hub before any call
   (no transport call) and on the peer when the hub is bypassed by a direct call with a valid token; the allowed bodies pass.

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
