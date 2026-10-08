"""v0.5.13, the launcher's backend: GET /api/agents (the schema the sheet draws itself from, for both agents, installed and not, and the
demo fixture held to the same contract) and POST /api/projects/{p}/repos/{r}/sessions with the sheet's semantic body (every new field
reaches the adapter's argv, the compat fields are unchanged, every rejection answers the board's 400).

No real claude or codex runs: claude auth and the binary are stubbed, codex is the conftest fake (a 0.145.0 that only answers the probes),
tmux is the conftest fake, and the config dirs are the temp ones.
"""
import json
import re
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import claude_auth, tasks
from app.agents import claude, codex, registry
from app.agents.base import OptField
from app.config import settings
from tests.conftest import write_fake_codex

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
AUTH = {"installed": True, "version": "2.1.288", "loggedIn": True, "authMethod": "claude.ai", "email": "me@example.com",
        "subscriptionType": "max"}
DEMO = Path(__file__).resolve().parent.parent / "app" / "static" / "demo" / "agents.json"
UUID1 = "11111111-1111-4111-8111-111111111111"
UUID2 = "22222222-2222-4222-8222-222222222222"
RESP_KEYS = {"tmux", "attach_url", "agent", "agent_session_id", "claude_session_id", "cmd"}


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), "-c", "user.email=t@e.x", "-c", "user.name=t", *args], check=True, capture_output=True,
                          text=True).stdout


def git_repo(path: Path, commit: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)
    if commit:
        (path / "README.md").write_text("hi\n")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def board(lite_client, projects_dir, fake_tmux, monkeypatch):
    """The board with the fake tmux, a fake claude binary and fixed claude auth, and one repo shop/api (no commit)."""
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(claude_auth, "status", lambda: dict(AUTH))
    monkeypatch.setattr(claude_auth, "version", lambda: AUTH["version"])
    monkeypatch.setattr(registry, "_IS_LINUX", False)
    monkeypatch.delenv(claude.ULTRACODE_ENV, raising=False)
    claude.reset_caches()
    registry.invalidate()
    git_repo(projects_dir / "shop" / "api")
    from app import main
    main._invalidate_scan()
    yield SimpleNamespace(client=lite_client, tmux=fake_tmux, projects=projects_dir, repo=projects_dir / "shop" / "api")
    claude.reset_caches()
    registry.invalidate()
    main._invalidate_scan()


def post(board, body, repo="api", expect=201):
    r = board.client.post(f"/api/projects/shop/repos/{repo}/sessions", headers=H, json=body)
    assert r.status_code == expect, r.text
    return r.json()


def refused(board, body, message=None, repo="api"):
    """The launch answers a 400 (the message exactly, or matching /message/ when it is a re.Pattern) and starts nothing."""
    before, typed = len(board.tmux["created"]), len(board.tmux["sent"])
    r = board.client.post(f"/api/projects/shop/repos/{repo}/sessions", headers=H, json=body)
    assert r.status_code == 400, r.text
    if isinstance(message, re.Pattern):
        assert message.search(r.json()["error"]), r.json()["error"]
    elif message:
        assert r.json()["error"] == message
    assert len(board.tmux["created"]) == before and len(board.tmux["sent"]) == typed, "nothing was started"
    return r.json()["error"]


def argv_of(res):
    return shlex.split(res["cmd"])


def row_of(name):
    """The newest sessions row of a tmux name, as stored (opts is the JSON text)."""
    from app import main
    with main.db.lock:
        r = main.db.conn.execute("SELECT * FROM sessions WHERE tmux_name=? ORDER BY id DESC LIMIT 1", (name,)).fetchone()
    return dict(r)


# ======================================================================== GET /api/agents

KINDS = {"select", "combo", "bool", "text", "textarea", "dirs", "args"}
ENTRY_KEYS = {"name", "label", "glyph", "installed", "version", "auth", "hooks", "options", "permission_modes", "efforts", "models",
              "reasoning_by_model", "capabilities", "slash"}
OPTION_KEYS = {"key", "label", "kind", "choices", "default", "help", "group", "danger", "when"}
WHEN_REFS = {"launcher", "worktree", "mode", "repo.devcontainer"}      # what a `when` may look at: another field of the sheet, or the repo


def check_entry(name, e):
    """The contract of one GET /api/agents entry (also held by the demo fixture): what the launcher reads to draw its fields."""
    assert set(e) == ENTRY_KEYS and e["name"] == name
    assert isinstance(e["label"], str) and isinstance(e["glyph"], str) and isinstance(e["installed"], bool)
    assert e["version"] is None or isinstance(e["version"], str)
    assert isinstance(e["auth"], dict) and isinstance(e["auth"].get("loggedIn"), bool)
    assert isinstance(e["hooks"], dict) and isinstance(e["hooks"]["installed"], bool)
    for k in ("permission_modes", "efforts", "models"):
        assert isinstance(e[k], list) and e[k] and all(isinstance(x, str) for x in e[k]), k
    assert isinstance(e["capabilities"], dict) and all(isinstance(v, bool) for v in e["capabilities"].values())
    assert set(e["reasoning_by_model"]) <= set(e["models"]) | {""} and all(isinstance(v, list) and v for v in e["reasoning_by_model"].values())
    keys = [o["key"] for o in e["options"]]
    assert len(keys) == len(set(keys)), "a key once"
    groups = [o["group"] for o in e["options"]]
    assert set(groups) == {"basic", "advanced"} and groups == sorted(groups, key=lambda g: g != "basic"), "basic fields first, advanced after"
    for o in e["options"]:
        assert set(o) == OPTION_KEYS, o["key"]
        assert o["kind"] in KINDS and isinstance(o["label"], str) and o["label"] and isinstance(o["help"], str) and o["help"]
        assert isinstance(o["danger"], bool)
        assert o["choices"] is None or (isinstance(o["choices"], list) and o["choices"] and all(isinstance(c, str) for c in o["choices"]))
        if o["kind"] in ("select", "combo") and o["choices"] and o["default"] is not None:
            assert o["default"] in o["choices"], o["key"]
        assert o["default"] is None or isinstance(o["default"], (str, bool, int)), o["key"]
        if o["kind"] == "select":
            assert o["choices"], f"{o['key']}: a select without choices"
        if o["when"] is not None:
            assert o["when"] and set(o["when"]) <= WHEN_REFS, o["key"]
            for ref, want in o["when"].items():
                assert isinstance(want, bool) or (isinstance(want, list) and want and all(isinstance(w, str) for w in want)), (o["key"], ref)
                if ref in keys and isinstance(want, list):
                    pick = next(x for x in e["options"] if x["key"] == ref)
                    assert set(want) <= set(pick["choices"]), f"{o['key']} waits for a {ref} the schema does not offer"
    by = {o["key"]: o for o in e["options"]}
    assert [k for k, o in by.items() if o["danger"]] == ["bypass"], "the one danger switch"
    assert by["launcher"]["group"] == "basic" and by["launcher"]["default"] == "new" and by["launcher"]["choices"][0] == "new"
    assert {"model", "name", "prompt", "extra", "add_dirs"} <= set(by)
    blob = json.dumps(e).lower()
    assert not any(w in blob for w in ("secret", "credential", "access_token", "refresh_token", "auth.json"))
    return by


def test_api_agents_for_claude_and_codex_installed(board, fake_codex, monkeypatch):
    monkeypatch.setattr(codex.CodexAgent, "_warm_models", lambda self, exe: None)
    body = board.client.get("/api/agents", headers=H).json()
    assert list(body) == ["agents"] and list(body["agents"]) == ["claude", "codex"]
    c, x = body["agents"]["claude"], body["agents"]["codex"]
    cby, xby = check_entry("claude", c), check_entry("codex", x)
    assert c["installed"] and c["auth"]["loggedIn"] and c["version"] == AUTH["version"] and c["auth"]["email"] == "me@example.com"
    assert x["installed"] and x["version"] == "0.145.0" and x["auth"]["loggedIn"] and x["hooks"] == {"installed": False, "trust": "review"}
    # Claude: the sheet's field set in the order it draws it
    assert list(cby) == ["launcher", "resume_id", "from_pr", "name", "model", "effort", "fast", "permission_mode", "prompt", "bypass",
                         "allowed_tools", "disallowed_tools", "tools", "append_system_prompt", "agent_name", "fallback_model", "subagent_model",
                         "subagent_force", "autocompact", "worktree", "worktree_name", "fork_session", "add_dirs", "devcontainer", "mcp_config", "extra"]
    assert cby["launcher"]["choices"] == ["new", "resume", "continue", "from_pr"]
    assert c["models"][:4] == ["opus", "fable", "sonnet", "haiku"] and (cby["model"]["default"], cby["effort"]["default"]) == ("opus", "high")
    assert c["efforts"] == ["low", "medium", "high", "xhigh", "max"] == cby["effort"]["choices"] and c["capabilities"] == {"ultracode_flag": False, "permission_prompts_none": False}
    assert c["reasoning_by_model"] == {} and "bypassPermissions" in c["permission_modes"]
    assert cby["fork_session"]["when"] == {"launcher": ["resume", "continue"]} and cby["devcontainer"]["when"] == {"repo.devcontainer": True}
    # Codex: from the catalogue and the box's flags
    assert list(xby) == ["launcher", "resume_id", "name", "model", "reasoning_effort", "mode", "sandbox", "approval", "prompt", "search",
                         "bypass", "permission_mode", "add_dirs", "worktree", "worktree_name", "config", "profile", "no_alt_screen", "extra"]
    assert xby["launcher"]["choices"] == ["new", "resume", "continue", "fork"] and xby["mode"]["choices"] == ["default", "auto", "read-only", "bypass", "custom"]
    assert xby["sandbox"]["when"] == {"mode": ["custom"]} and xby["approval"]["choices"] == ["on-request", "never", "untrusted"]   # the fake is 0.145
    assert x["models"] == xby["model"]["choices"] and x["models"], "the catalogue, or the box's visible slugs until it has been read"
    assert set(x["reasoning_by_model"]) <= set(x["models"]) and x["capabilities"]["fork"] is True
    assert x["capabilities"]["approve_for_me"] is False and x["capabilities"]["no_alt_screen"] is True


def test_api_agents_for_claude_and_codex_not_installed(board, monkeypatch):
    monkeypatch.setattr(claude_auth, "status", lambda: {"installed": False, "loggedIn": False, "version": None})
    monkeypatch.setattr(settings, "claude_bin", lambda: None)
    body = board.client.get("/api/agents", headers=H).json()["agents"]
    c, x = body["claude"], body["codex"]
    check_entry("claude", c)
    check_entry("codex", x)
    assert c["installed"] is False and c["version"] is None and c["auth"]["loggedIn"] is False and c["hooks"] == {"installed": False}
    assert x["installed"] is False and x["version"] is None and x["auth"]["loggedIn"] is False
    # the schema is still whole: the launcher can draw (and disable) the agent, and fall back on the box's visible slugs for Codex
    assert x["models"] == [m["slug"] for m in codex.FALLBACK_MODELS] and x["capabilities"]["fork"] is True and c["capabilities"] == {"ultracode_flag": False, "permission_prompts_none": False}
    assert len(c["options"]) > 20 and len(x["options"]) > 15 and c["slash"] and x["slash"]


def test_api_agents_serves_ultracode_when_the_box_takes_it(board, monkeypatch):
    monkeypatch.setenv(claude.ULTRACODE_ENV, "1")
    c = board.client.get("/api/agents", headers=H).json()["agents"]["claude"]
    effort = next(o for o in c["options"] if o["key"] == "effort")
    assert c["efforts"][-1] == "ultracode" == effort["choices"][-1] and c["capabilities"] == {"ultracode_flag": True, "permission_prompts_none": False}
    check_entry("claude", c)


def test_api_agents_does_not_probe_on_every_request(board, fake_codex, monkeypatch):
    """The heavy parts (the models catalogue, the flag set) are memoised per binary: a second request starts no subprocess."""
    monkeypatch.setattr(codex.CodexAgent, "_warm_models", lambda self, exe: None)
    board.client.get("/api/agents", headers=H)
    runs = []
    real = codex._run
    monkeypatch.setattr(codex, "_run", lambda argv, timeout=0: runs.append(argv[1:]) or real(argv, timeout))
    for _ in range(3):
        assert board.client.get("/api/agents", headers=H).status_code == 200
    assert [r for r in runs if r not in (["login", "status"],)] == [], "only the 60 s auth probe may repeat"


def test_the_demo_fixture_is_held_to_the_same_contract(board, fake_codex, monkeypatch):
    monkeypatch.setattr(codex.CodexAgent, "_warm_models", lambda self, exe: None)
    demo = json.loads(DEMO.read_text())
    assert list(demo) == ["agents"] and list(demo["agents"]) == ["claude", "codex"]
    live = board.client.get("/api/agents", headers=H).json()["agents"]
    for name, e in demo["agents"].items():
        by = check_entry(name, e)
        assert e["installed"] is True and e["auth"]["loggedIn"] is True and e["hooks"]["installed"] is True, "the demo's agents are both ready"
        assert list(by) == [o["key"] for o in live[name]["options"]], f"{name}: the fixture's fields drifted from the adapter's schema"
        for k, o in by.items():
            lo = next(x for x in live[name]["options"] if x["key"] == k)
            assert {f: o[f] for f in ("kind", "group", "danger", "when")} == {f: lo[f] for f in ("kind", "group", "danger", "when")}, (name, k)
        assert set(e["slash"]) == set(live[name]["slash"]) and set(e["capabilities"]) == set(live[name]["capabilities"])
    assert demo["agents"]["claude"]["models"][:4] == ["opus", "fable", "sonnet", "haiku"]
    assert demo["agents"]["codex"]["reasoning_by_model"]["gpt-6.1-sol"][-1] == "ultra" and "ultracode" not in demo["agents"]["claude"]["efforts"]
    assert "ultra" not in demo["agents"]["codex"]["reasoning_by_model"]["gpt-6-luna"] and "codex-auto-review" not in demo["agents"]["codex"]["models"]
    for name, e in demo["agents"].items():                                                # the demo's slash rows are the adapters' own
        assert e["slash"] == live[name]["slash"], name
    text = DEMO.read_text()
    assert not any(n in text for n in ("/Users/", "/tmp/", "/private/", "gmail.com", "rpan" + "dox", "ccb-fix"))


def test_option_fields_stay_nine_keys(board):
    """The shape existing callers read: no key was added to OptField."""
    assert list(OptField.__dataclass_fields__) == ["key", "label", "kind", "choices", "default", "help", "group", "danger", "when"]


# ======================================================================== POST sessions: Claude

def test_the_response_keeps_its_shape(board):
    r = post(board, {"launcher": "claude"})
    assert set(r) == RESP_KEYS and r["agent"] == "claude" and r["agent_session_id"] == r["claude_session_id"] and r["cmd"].startswith("claude ")
    assert r["attach_url"] == f"/tty/?arg={r['tmux']}" and re.match(r"^[0-9a-f-]{36}$", r["agent_session_id"])


def test_every_claude_field_of_the_sheet_reaches_the_argv(board):
    cfg = settings.claude_config_dir
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "mcp.json").write_text("{}")
    r = post(board, {"launcher": "new", "name": "dev", "model": "opus[1m]", "effort": "high", "mode": "acceptEdits", "prompt": "fix the login",
                     "tools": "Bash, Edit", "agent_name": "reviewer", "fallback_model": ["sonnet", "haiku"], "autocompact": 150000,
                     "mcp_config": str(cfg / "mcp.json"), "allowed_tools": "Bash(git *)", "disallowed_tools": "WebFetch",
                     "append_system_prompt": "be brief", "fast": True, "args": "--verbose", "add_dirs": []})
    argv = argv_of(r)
    assert argv == ["claude", "--model", "opus[1m]", "--effort", "high", "--permission-mode", "acceptEdits", "--allowedTools", "Bash(git *)",
                    "--disallowedTools", "WebFetch", "--append-system-prompt", "be brief", "--tools", "Bash,Edit", "--agent", "reviewer",
                    "--fallback-model", "sonnet,haiku", "--autocompact", "150000", "--mcp-config", str(cfg / "mcp.json"), "--verbose",
                    "--session-id", r["agent_session_id"], "--name", "dev", "--", "fix the login"]
    row = row_of(r["tmux"])
    assert json.loads(row["opts"]) == {"model": "opus[1m]", "effort": "high", "permission_mode": "acceptEdits", "allowed_tools": ["Bash(git *)"],
                                       "disallowed_tools": ["WebFetch"], "append_system_prompt": "be brief", "tools": ["Bash", "Edit"],
                                       "agent_name": "reviewer", "fallback_model": ["sonnet", "haiku"], "autocompact": "150000",
                                       "mcp_config": str(cfg / "mcp.json"), "fast": True}, "stored for resume: all but the extra args"
    assert row["launcher"] == "claude" and row["agent"] == "claude"


def test_claude_worktree_and_cwd_rel(board):
    r = post(board, {"launcher": "claude", "worktree": True, "name": "wt1", "prompt": "go"})
    argv = argv_of(r)
    i = argv.index("--worktree")
    assert re.match(r"^wt1-[0-9a-f]{6}$", argv[i + 1]), "no name given: the session name plus a short suffix"
    assert argv[i + 2:] == ["--session-id", r["agent_session_id"], "--name", "wt1", "--", "go"]
    named = argv_of(post(board, {"launcher": "claude", "worktree": True, "worktree_name": "fix-it", "name": "wt2"}))
    assert named[named.index("--worktree") + 1] == "fix-it"
    by_name = argv_of(post(board, {"launcher": "claude", "worktree": "from-the-switch", "name": "wt3"}))
    assert by_name[by_name.index("--worktree") + 1] == "from-the-switch"
    off = argv_of(post(board, {"launcher": "claude", "worktree": False, "worktree_name": "ignored", "name": "wt4"}))
    assert "--worktree" not in off, "a name beside an off switch is nothing"
    # cwd_rel: the session starts in that folder of the repo
    sub = board.repo / "pkg" / "web"
    sub.mkdir(parents=True)
    r = post(board, {"launcher": "claude", "cwd_rel": "pkg/web", "name": "in-sub"})
    name, cwd, env = board.tmux["created"][-1]
    assert cwd == str(sub) and row_of(name)["cwd"] == str(sub)
    assert post(board, {"launcher": "claude", "cwd_rel": "", "name": "at-root"}) and board.tmux["created"][-1][1] == str(board.repo)


def test_from_pr_resume_continue_and_fork(board):
    r = post(board, {"launcher": "from_pr", "from_pr": "#42", "model": "opus"})
    assert argv_of(r) == ["claude", "--from-pr", "42", "--model", "opus"] and r["agent_session_id"] is None
    row = row_of(r["tmux"])
    assert row["launcher"] == "resume" and json.loads(row["opts"]) == {"model": "opus"}
    # from_pr beside a new launch is the same launch; a PR URL is fine; a number sent as a number too
    assert argv_of(post(board, {"launcher": "claude", "from_pr": "https://github.com/o/r/pull/7"}))[:3] == \
        ["claude", "--from-pr", "https://github.com/o/r/pull/7"]
    assert argv_of(post(board, {"launcher": "claude", "from_pr": 9}))[:3] == ["claude", "--from-pr", "9"]
    assert argv_of(post(board, {"launcher": "from-pr", "from_pr": "5"}))[:3] == ["claude", "--from-pr", "5"]
    f = post(board, {"launcher": "resume", "resume_id": UUID1, "fork_session": True})
    assert argv_of(f) == ["claude", "--resume", UUID1, "--fork-session"] and f["agent_session_id"] == UUID1
    assert json.loads(row_of(f["tmux"])["opts"] if row_of(f["tmux"])["opts"] else "{}") == {}, "a fork is for this launch only"
    assert argv_of(post(board, {"launcher": "continue", "fork_session": True, "model": "fable"})) == \
        ["claude", "--continue", "--fork-session", "--model", "fable"]
    assert argv_of(post(board, {"launcher": "resume", "fork_session": True}))[:3] == ["claude", "--resume", "--fork-session"]


def test_reasoning_is_the_effort_and_mode_the_permission_mode_for_claude(board):
    assert argv_of(post(board, {"launcher": "claude", "reasoning": "max", "mode": "read-only"}))[-4:] == ["--effort", "max", "--permission-mode", "plan"]
    assert argv_of(post(board, {"launcher": "claude", "effort": "low", "reasoning": "max"}))[-2:] == ["--effort", "low"]
    assert argv_of(post(board, {"launcher": "claude", "mode": "default"}))[-2:] == ["--permission-mode", "manual"]


def test_ultracode_is_an_effort_only_where_the_box_takes_it_over_http(board, monkeypatch):
    refused(board, {"launcher": "claude", "effort": "ultracode"}, "effort must be one of low, medium, high, xhigh, max")
    monkeypatch.setenv(claude.ULTRACODE_ENV, "1")
    assert argv_of(post(board, {"launcher": "claude", "effort": "ultracode"}))[-2:] == ["--effort", "ultracode"]


def test_the_flat_compat_body_is_unchanged(board):
    r = post(board, {"launcher": "claude", "name": "old", "model": "fable", "effort": "high", "permission_mode": "acceptEdits",
                     "allowed_tools": "Bash(npm test), Read", "disallowed_tools": "WebFetch", "append_system_prompt": "x", "args": "--verbose",
                     "bypass": True})
    assert argv_of(r) == ["claude", "--session-id", r["agent_session_id"], "--name", "old", "--dangerously-skip-permissions", "--model", "fable",
                          "--effort", "high", "--permission-mode", "acceptEdits", "--allowedTools", "Bash(npm test)", "Read",
                          "--disallowedTools", "WebFetch", "--append-system-prompt", "x", "--verbose"]
    # the pre-v0.5.13 spellings of bypass stay what they were: an explicit choice on the host, never stored
    r = post(board, {"launcher": "claude", "name": "byp", "permission_mode": "bypassPermissions"})
    assert argv_of(r)[-2:] == ["--permission-mode", "bypassPermissions"] and row_of(r["tmux"])["opts"] in (None, "", "{}")
    assert post(board, {"launcher": "shell", "name": "sh"})["cmd"] is None
    assert post(board, {"launcher": "resume", "resume_id": UUID2, "name": "rr"})["cmd"] == f"claude --resume {UUID2}"
    assert post(board, {"launcher": "continue", "name": "cc"})["cmd"] == "claude --continue"


def test_mode_bypass_needs_the_acknowledgement(board):
    msg = "mode bypass skips every approval and the sandbox: send bypass: true with the acknowledgement"
    refused(board, {"launcher": "claude", "mode": "bypass"}, msg)
    refused(board, {"launcher": "claude", "mode": "bypass", "bypass": False}, msg)
    r = post(board, {"launcher": "claude", "mode": "bypass", "bypass": True, "name": "ack"})
    argv = argv_of(r)
    assert argv[-2:] == ["--permission-mode", "bypassPermissions"] and "--dangerously-skip-permissions" not in argv, "one spelling, not both"
    assert row_of(r["tmux"])["opts"] in (None, "", "{}"), "never stored for resume"


def test_the_sheets_bypass_body_builds_the_same_argv_as_the_flat_spelling(board):
    """The sheet says the danger gate's answer twice (mode bypass + bypass true, beside permission_mode): the route must build exactly the
    argv the older flat `permission_mode: bypassPermissions` always did, with the one --permission-mode and no --dangerously-skip-permissions."""
    base = {"launcher": "claude", "model": "opus", "effort": "high", "prompt": "go"}
    flat = post(board, {**base, "name": "flat", "permission_mode": "bypassPermissions"})
    sheet = post(board, {**base, "name": "sheet", "permission_mode": "bypassPermissions", "mode": "bypass", "bypass": True})
    only_mode = post(board, {**base, "name": "modeonly", "mode": "bypass", "bypass": True})

    def masked(res, name):
        return ["<name>" if t == name else ("<uuid>" if t == res["agent_session_id"] else t) for t in argv_of(res)]

    want = masked(flat, "flat")
    assert "--dangerously-skip-permissions" not in want and want.count("--permission-mode") == 1 and "bypassPermissions" in want
    assert masked(sheet, "sheet") == want, "the sheet's body and the flat spelling: one argv"
    assert masked(only_mode, "modeonly") == want, "mode alone says the same"
    stored = [json.loads(row_of(res["tmux"])["opts"] or "{}") for res in (flat, sheet, only_mode)]
    assert stored[0] == stored[1] == stored[2] == {"model": "opus", "effort": "high"}, "the same options are kept, and no bypass among them (never stored for a resume)"


@pytest.mark.parametrize("body,message", [
    ({"launcher": "claude", "from_pr": "abc"}, "from_pr: use a pull request number (123 or #123) or its URL"),
    ({"launcher": "resume", "from_pr": "3"}, "from_pr: only a new launch takes a pull request (resume and continue pick up a session by themselves)"),
    ({"launcher": "from_pr"}, "from_pr: give the pull request number or URL"),
    ({"launcher": "from_pr", "from_pr": "3", "agent": "codex"}, "from_pr: only Claude resumes a session from its pull request"),
    ({"launcher": "fork"}, "fork: a Codex launch; with Claude resume or continue and tick fork_session"),
    ({"launcher": "claude", "fork_session": True}, "fork_session: only a resume or continue launch can fork"),
    ({"launcher": "claude", "autocompact": "off"}, "autocompact: use auto or a number of tokens (4 to 9 digits)"),
    ({"launcher": "claude", "fallback_model": "a,b,c,d"}, "fallback_model: use up to 3 aliases or model ids, comma separated"),
    ({"launcher": "claude", "agent_name": "-x"}, "agent_name: use letters, digits, '.', '_', ':', '@', '/' or '-'"),
    ({"launcher": "claude", "tools": "Bash;rm"}, "tool pattern not allowed: 'Bash;rm'"),
    ({"launcher": "claude", "mcp_config": "/etc/passwd"},
     "mcp_config: use the absolute path of a JSON file inside the projects folder or the Claude config folder"),
    ({"launcher": "claude", "mode": "yolo"}, re.compile(r"^mode must be one of default, read-only, bypass, ")),
    ({"launcher": "claude", "mode": "custom"}, re.compile(r"^mode must be one of")),
    ({"launcher": "resume", "prompt": "hi"}, re.compile(r"^a first prompt can only start a new session")),
    ({"launcher": "continue", "worktree": True}, "worktree: only a new session can start in a new worktree"),
    ({"launcher": "claude", "worktree": True, "worktree_name": "bad name"}, "worktree name: use letters, digits, '.', '_' or '-'"),
    ({"launcher": "claude", "worktree": True, "cwd_rel": "pkg"}, re.compile(r"^cwd_rel: not together with a worktree")),
    ({"launcher": "claude", "cwd_rel": "../x"}, "cwd_rel: a folder inside the repo, written relative to it (no '..')"),
    ({"launcher": "claude", "cwd_rel": "/etc"}, "cwd_rel: a folder inside the repo, written relative to it (no '..')"),
    ({"launcher": "claude", "cwd_rel": "nope"}, "cwd_rel: nope is not a folder inside this repo"),
    ({"launcher": "claude", "prompt": "x" * 8001}, re.compile(r"^prompt is too long \(8000 characters at most\)")),
    ({"launcher": "claude", "args": "--settings /tmp/x.json"}, re.compile(r"^--settings: settings overrides are not allowed")),
    ({"launcher": "resume", "resume_id": "-x"}, "resume id must be a UUID"),
    ({"launcher": "new", "name": "bad name"}, re.compile(r"")),
    ({"launcher": "emacs"}, "launcher must be one of claude, resume, continue, shell, codex, codex-resume, codex-continue"),
])
def test_claude_rejections(board, body, message):
    refused(board, body, message)


def test_a_cwd_rel_that_escapes_through_a_symlink_is_refused(board, tmp_path):
    (tmp_path / "outside").mkdir()
    (board.repo / "out").symlink_to(tmp_path / "outside")
    refused(board, {"launcher": "claude", "cwd_rel": "out"}, "cwd_rel: out is not a folder inside this repo")
    (board.repo / "file.txt").write_text("x")
    refused(board, {"launcher": "claude", "cwd_rel": "file.txt"}, "cwd_rel: file.txt is not a folder inside this repo")


def test_a_worktree_needs_a_git_repo(board):
    (board.projects / "shop" / "plain").mkdir()
    refused(board, {"launcher": "claude", "worktree": True}, "worktree: this folder is not a git repo", repo="plain")
    assert argv_of(post(board, {"launcher": "claude", "name": "fine"}, repo="plain"))[0] == "claude", "without the switch a plain folder is fine"


# ======================================================================== POST sessions: Codex

@pytest.fixture
def cx(board, fake_codex, monkeypatch):
    monkeypatch.setattr(codex.CodexAgent, "_warm_models", lambda self, exe: None)
    return board


def test_every_codex_field_of_the_sheet_reaches_the_argv(cx):
    r = post(cx, {"launcher": "new", "agent": "codex", "name": "cdx", "model": "gpt-5.5", "reasoning": "high", "mode": "read-only", "search": True,
                  "prompt": "review it", "config": ["tui.theme=dark", "a.b=1"], "add_dirs": [], "args": "--no-hooks-please"})
    assert r["agent"] == "codex" and r["agent_session_id"] is None
    assert argv_of(r) == ["codex", "--no-alt-screen", "-s", "read-only", "-a", "on-request", "-m", "gpt-5.5", "-c", 'model_reasoning_effort="high"',
                          "-c", "tui.theme=dark", "-c", "a.b=1", "--search", "--no-hooks-please", "--", "review it"]
    row = row_of(r["tmux"])
    assert row["agent"] == "codex" and row["launcher"] == "claude" and json.loads(row["opts"]) == {
        "model": "gpt-5.5", "reasoning_effort": "high", "permission_mode": "plan", "config": ["tui.theme=dark", "a.b=1"], "search": True}
    # the old spellings still work: the codex launcher, the flat effort read as reasoning, `opts` over the flat fields
    r = post(cx, {"launcher": "codex", "name": "old", "effort": "low", "permission_mode": "dontAsk", "opts": {"search": True}})
    assert argv_of(r) == ["codex", "--no-alt-screen", "-s", "workspace-write", "-a", "never", "-c", 'model_reasoning_effort="low"', "--search"]


def test_the_codex_mode_picker_over_http(cx):
    assert argv_of(post(cx, {"launcher": "new", "agent": "codex", "mode": "default"}))[1:] == ["--no-alt-screen", "-s", "workspace-write", "-a", "on-request"]
    assert argv_of(post(cx, {"launcher": "new", "agent": "codex", "mode": "auto"}))[-4:] == ["-s", "workspace-write", "-a", "on-request"]
    assert argv_of(post(cx, {"launcher": "new", "agent": "codex", "mode": "custom", "sandbox": "read-only", "approval": "never"}))[-4:] == \
        ["-s", "read-only", "-a", "never"]
    assert argv_of(post(cx, {"launcher": "new", "agent": "codex", "mode": "custom", "opts": {"approval": "untrusted"}}))[-2:] == ["-a", "untrusted"]
    refused(cx, {"launcher": "new", "agent": "codex", "mode": "custom"}, "mode custom: choose a sandbox, an approval policy or both")
    refused(cx, {"launcher": "new", "agent": "codex", "mode": "yolo"}, "mode must be one of default, auto, read-only, bypass, custom")


def test_codex_bypass_and_danger_full_access_need_the_acknowledgement(cx):
    msg = "mode bypass skips every approval and the sandbox: send bypass: true with the acknowledgement"
    refused(cx, {"launcher": "new", "agent": "codex", "mode": "bypass"}, msg)
    r = post(cx, {"launcher": "new", "agent": "codex", "mode": "bypass", "bypass": True})
    assert argv_of(r) == ["codex", "--no-alt-screen", "--dangerously-bypass-approvals-and-sandbox"] and row_of(r["tmux"])["opts"] in (None, "", "{}")
    refused(cx, {"launcher": "new", "agent": "codex", "mode": "custom", "sandbox": "danger-full-access"}, re.compile(r"^sandbox danger-full-access: "))
    r = post(cx, {"launcher": "new", "agent": "codex", "mode": "custom", "sandbox": "danger-full-access", "bypass": True})
    assert argv_of(r)[-1] == "--dangerously-bypass-approvals-and-sandbox"
    # the pre-v0.5.13 spelling is what it was
    assert argv_of(post(cx, {"launcher": "codex", "permission_mode": "bypassPermissions"}))[-1] == "--dangerously-bypass-approvals-and-sandbox"


def test_codex_resume_by_id_or_name_continue_and_fork(cx):
    assert argv_of(post(cx, {"launcher": "resume", "agent": "codex", "resume_id": UUID1}))[:3] == ["codex", "resume", "--no-alt-screen"]
    named = post(cx, {"launcher": "codex-resume", "resume_id": "my thread"})
    assert argv_of(named)[-1] == "my thread" and named["agent_session_id"] is None
    assert argv_of(post(cx, {"launcher": "continue", "agent": "codex"}))[-1] == "--last"
    f = post(cx, {"launcher": "fork", "agent": "codex", "resume_id": UUID1, "mode": "read-only"})
    assert argv_of(f) == ["codex", "fork", "--no-alt-screen", "-s", "read-only", "-a", "on-request", UUID1] and f["agent_session_id"] is None
    assert row_of(f["tmux"])["launcher"] == "resume", "a fork is recovered like a resumed session"
    assert argv_of(post(cx, {"launcher": "codex-fork", "resume_id": "my thread"}))[:2] == ["codex", "fork"]
    assert argv_of(post(cx, {"launcher": "fork", "agent": "codex"}))[-1] == "--no-alt-screen", "no id: Codex's picker"
    assert argv_of(post(cx, {"launcher": "resume", "agent": "codex", "resume_id": UUID1, "fork_session": True}))[:2] == ["codex", "fork"]
    refused(cx, {"launcher": "continue", "agent": "codex", "fork_session": True},
            "fork_session: Codex forks a session by its id or name (or from its picker), not the latest one")
    refused(cx, {"launcher": "resume", "agent": "codex", "resume_id": "-x"}, re.compile(r"^resume id must be a UUID or a session name"))
    refused(cx, {"launcher": "resume", "agent": "codex", "resume_id": "a;b"}, re.compile(r"^resume id must be a UUID or a session name"))


def test_codex_without_fork_refuses_it_with_a_reason(cx, fake_codex):
    text = Path(__file__).parent.joinpath("fixtures", "codex_help_0145_real.txt").read_text()
    import tests.conftest as c
    stripped = fake_codex.parent / "help-no-fork.txt"
    stripped.write_text(re.sub(r"^  fork .*\n.*most recent\)\n", "", text, flags=re.M))
    fake_codex.write_text(fake_codex.read_text().replace(str(Path(c.__file__).parent / "fixtures" / "codex_help_0145_real.txt"), str(stripped)))
    codex.reset_caches()
    refused(cx, {"launcher": "fork", "agent": "codex"}, re.compile(r"^fork: this codex has no `codex fork` command"))
    assert "fork" not in next(o for o in cx.client.get("/api/agents", headers=H).json()["agents"]["codex"]["options"] if o["key"] == "launcher")["choices"]


@pytest.mark.parametrize("body,message", [
    ({"config": ["model=o3"]}, re.compile(r"^config model: the launcher sets this itself")),
    ({"config": "sandbox_mode=danger-full-access"}, re.compile(r"^config sandbox_mode: ")),
    ({"config": ["approval_policy=never"]}, re.compile(r"^config approval_policy: ")),
    ({"config": ["hooks.x=1"]}, re.compile(r"^config hooks.x: ")),
    ({"config": ["mcp_servers.x.command=sh"]}, re.compile(r"^config mcp_servers.x.command: ")),
    ({"config": ["notify=sh"]}, re.compile(r"^config notify: ")),
    ({"config": ["not a line"]}, re.compile(r"^config 'not a line': use key=value")),
    ({"config": [f"a.k{i}=1" for i in range(21)]}, "config: 20 lines at most"),
    ({"tools": "Bash"}, "tools: not supported by codex"),
    ({"fallback_model": "sonnet"}, "fallback_model: not supported by codex"),
    ({"agent_name": "reviewer"}, "agent_name: not supported by codex"),
    ({"autocompact": "auto"}, "autocompact: not supported by codex"),
    ({"mcp_config": "/x.json"}, "mcp_config: not supported by codex"),
    ({"allowed_tools": "Bash"}, "allowed_tools: not supported by codex"),
    ({"append_system_prompt": "x"}, "append_system_prompt: not supported by codex"),
    ({"model": "gpt-6-luna", "reasoning": "ultra"}, re.compile(r"^reasoning_effort must be one of")),     # Luna has no ultra (#17)
    ({"devcontainer": True}, "this repo has no .devcontainer/devcontainer.json"),
    ({"args": "-c model=x"}, re.compile(r"^-c: settings overrides are not allowed in extra args")),
    ({"args": "--yolo"}, re.compile(r"^--yolo: ")),
    ({"prompt": "x" * 8001}, re.compile(r"^prompt is too long")),
    ({"worktree": True, "cwd_rel": "x"}, re.compile(r"^cwd_rel: not together with a worktree")),
])
def test_codex_rejections(cx, body, message):
    refused(cx, {"launcher": "new", "agent": "codex", **body}, message)


def test_a_saved_untrusted_is_a_plain_400_on_a_0160_codex_and_builds_nothing(cx, fake_codex):
    """#17: a launch (the same validate_opts serves tasks and schedules) naming an approval the probed codex does not list is refused
    with the value and the allowed ones; on-request and never still build their -a flag."""
    import tests.conftest as c
    fixtures = Path(c.__file__).parent / "fixtures"
    fake_codex.write_text(fake_codex.read_text().replace(str(fixtures / "codex_help_0145_real.txt"), str(fixtures / "codex_help_0160.txt")))
    codex.reset_caches()
    before = len(cx.tmux["created"])
    refused(cx, {"launcher": "new", "agent": "codex", "mode": "custom", "approval": "untrusted"},
            "approval must be one of on-request, never (this codex supports no other)")
    refused(cx, {"launcher": "new", "agent": "codex", "mode": "custom", "approval": "on-failure"}, re.compile(r"^approval must be one of on-request, never"))
    assert len(cx.tmux["created"]) == before, "no session, no argv"
    for value in ("on-request", "never"):
        argv = argv_of(post(cx, {"launcher": "new", "agent": "codex", "mode": "custom", "approval": value}))
        assert argv[argv.index("-a") + 1] == value and "untrusted" not in argv


def test_codex_config_lines_arrive_as_text_too(cx):
    r = post(cx, {"launcher": "new", "agent": "codex", "config": "tui.theme=dark\n\n  a.b=1  "})
    assert argv_of(r)[-4:] == ["-c", "tui.theme=dark", "-c", "a.b=1"]


def test_a_codex_worktree_is_made_by_the_board(cx):
    repo = git_repo(cx.projects / "shop" / "wt", commit=True)
    r = post(cx, {"launcher": "new", "agent": "codex", "worktree": True, "worktree_name": "try-it", "prompt": "go", "name": "w1"}, repo="wt")
    wt = repo / ".ccboard" / "worktrees" / "try-it"
    name, cwd, env = cx.tmux["created"][-1]
    assert cwd == str(wt) and (wt / "README.md").is_file() and row_of(name)["cwd"] == str(wt), "Codex starts inside the worktree"
    assert "worktree-try-it" in git(repo, "branch", "--list", "worktree-try-it")
    assert argv_of(r)[-2:] == ["--", "go"] and "-C" not in argv_of(r)
    # a second one under the same name takes the next free slug; a worktree is never a task row
    post(cx, {"launcher": "new", "agent": "codex", "worktree": True, "worktree_name": "try-it", "name": "w2"}, repo="wt")
    assert (repo / ".ccboard" / "worktrees" / "try-it-2").is_dir()
    from app import main
    assert main.db.tasks() == []


def test_a_refused_codex_worktree_launch_leaves_nothing_behind(cx):
    repo = git_repo(cx.projects / "shop" / "wt", commit=True)
    refused(cx, {"launcher": "new", "agent": "codex", "worktree": True, "config": ["model=x"]}, re.compile(r"^config model: "), repo="wt")
    assert not (repo / ".ccboard" / "worktrees").exists() or not any((repo / ".ccboard" / "worktrees").iterdir())
    assert "worktree-" not in git(repo, "branch", "--list")


def test_a_codex_worktree_needs_a_git_repo_and_a_new_launch(cx):
    (cx.projects / "shop" / "plain").mkdir()
    refused(cx, {"launcher": "new", "agent": "codex", "worktree": True}, "worktree: this folder is not a git repo", repo="plain")
    refused(cx, {"launcher": "continue", "agent": "codex", "worktree": True}, "worktree: only a new session can start in a new worktree")


def test_a_codex_launch_failure_after_the_worktree_takes_it_away(cx, monkeypatch):
    from app import main
    repo = git_repo(cx.projects / "shop" / "wt", commit=True)
    monkeypatch.setattr(main, "_start_session", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tmux said no")))
    with pytest.raises(RuntimeError):
        cx.client.post("/api/projects/shop/repos/wt/sessions", headers=H, json={"launcher": "new", "agent": "codex", "worktree": True})
    assert not any((repo / ".ccboard" / "worktrees").iterdir()) and "worktree-" not in git(repo, "branch", "--list")


def test_the_session_route_shell_and_unknown_pairs(cx):
    assert post(cx, {"launcher": "shell", "name": "plain", "model": "x", "mode": "bypass", "prompt": "ignored"})["cmd"] is None
    refused(cx, {"launcher": "shell", "agent": "codex"}, re.compile(r"cannot start codex"))
    refused(cx, {"launcher": "codex", "agent": "claude"}, re.compile(r"starts codex; it cannot start claude"))


# ======================================================================== tasks and dispatch never get the danger

def test_a_task_cannot_ask_for_bypass_through_the_new_words(cx):
    body = {"title": "t", "prompt": "p", "agent": "codex"}
    for extra in ({"opts": {"mode": "bypass"}}, {"opts": {"permission_mode": "bypassPermissions"}}, {"opts": {"sandbox": "danger-full-access"}},
                  {"opts": {"mode": "custom", "sandbox": "danger-full-access"}}):
        r = cx.client.post("/api/projects/shop/repos/api/tasks", headers=H, json={**body, **extra})
        assert r.status_code == 400, (extra, r.text)
    # the claude task route has no `mode` at all: it is ignored, and the claude rule for bypass holds
    r = cx.client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": "t", "prompt": "p", "permission_mode": "bypassPermissions"})
    assert r.status_code == 400
    assert not cx.tmux["created"], "no session was started by any of them"


# ======================================================================== the preview and the route build the same command

CASES = json.loads((Path(__file__).resolve().parent / "fixtures" / "launcher_cases.json").read_text())["cases"]
UUID_SHAPE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HEX6 = re.compile(r"-[0-9a-f]{6}$")


def masked_tokens(res, case):
    """The route's cmd as tokens with its random parts named the way the preview names them: the id after --session-id is <uuid>; the six hex
    a Claude worktree name gets when the sheet gave none are xxxxxx (a name the sheet gave stays as it is)."""
    argv = argv_of(res)
    for i, t in enumerate(argv[:-1]):
        if t == "--session-id":
            assert UUID_SHAPE.match(argv[i + 1]) and argv[i + 1] == res["agent_session_id"]
            argv[i + 1] = "<uuid>"
        if t == "--worktree" and not case["body"].get("worktree_name"):
            assert HEX6.search(argv[i + 1]), f"a blank worktree name gets a random suffix: {argv[i + 1]}"
            argv[i + 1] = HEX6.sub("-xxxxxx", argv[i + 1])
    return argv


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_the_sheets_body_gets_the_command_the_preview_shows(case, request):
    """tests/fixtures/launcher_cases.json is read by tests/js/launcher.test.mjs too: there launcherPayload(v) must equal `body` and the preview
    of v must split into `tokens`; here `body` goes to the real route (the fake tmux, the fake codex 0.145, the temp dirs) and its cmd must split
    into the same `tokens`. So the preview cannot say a command the server does not build."""
    board_ = request.getfixturevalue("cx" if case["v"]["agent"] == "codex" else "board")
    res = post(board_, case["body"])
    assert masked_tokens(res, case) == case["tokens"], res["cmd"]
