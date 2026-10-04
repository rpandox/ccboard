"""v0.5.11 Codex hooks: scripts/codex_hooks.py (the hooks.json installer), install.sh's Codex steps and docker-entrypoint.sh's.

Nothing here touches the real ~/.codex: every test runs with HOME (and CODEX_HOME where it matters) pointing into tmp_path, and the
one test that drives the real `codex` binary is opt-in (CCBOARD_TEST_CODEX_BINARY=1) and does the same."""
import copy
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "codex_hooks.py"
INSTALL = ROOT / "install.sh"
ENTRYPOINT = ROOT / "scripts" / "docker-entrypoint.sh"
APP = Path("/opt/ccboard")
FOREIGN = {"type": "command", "command": "/usr/local/bin/mine"}


def _load():
    spec = importlib.util.spec_from_file_location("codex_hooks_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cx = _load()

STANDARD = ["SessionStart", "UserPromptSubmit", "Stop", "SubagentStart", "SubagentStop", "PreCompact", "PostCompact"]
HOOK_CMD = "env CCBOARD_AGENT=codex /opt/ccboard/bin/ccboard-hook"
SYNC_HOOK = {"type": "command", "command": HOOK_CMD, "timeout": 5}
ASYNC_HOOK = {"type": "command", "command": HOOK_CMD, "async": True, "timeout": 5}
FAST_CMD = "env CCBOARD_AGENT=codex /opt/ccboard/bin/ccboard-hook-fast"
PERM_CMD = "env CCBOARD_AGENT=codex /opt/ccboard/bin/ccboard-permission"      # the permission hook names its agent like the others


@pytest.fixture(autouse=True)
def _ambient_home(tmp_path, monkeypatch):
    """No test may reach the developer's real ~/.codex, even through a default path."""
    home = tmp_path / "ambient_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return home


def cmds(data, ev):
    return [h["command"] for g in data["hooks"][ev] for h in g["hooks"]]


def ours(data, ev):
    return [h for g in data["hooks"][ev] if isinstance(g, dict) for h in g.get("hooks", []) if cx.is_ours(h)]


def positions(data):
    """(event, group index, hook index) of every ccboard hook: with the file path, Codex's trust key for it."""
    return sorted((ev, gi, hi) for ev, groups in data["hooks"].items() for gi, g in enumerate(groups)
                  for hi, h in enumerate(g["hooks"]) if cx.is_ours(h))


# ------------------------------------------------------------------------------------------------ what is registered
def test_events_are_the_agreed_set_in_order():
    assert cx.STANDARD_EVENTS == STANDARD
    assert cx.FAST_EVENTS == ["Interrupt", "SessionEnd"]
    assert cx.EVENTS == STANDARD + ["Interrupt", "SessionEnd"] and cx.PERMISSION_EVENT == "PermissionRequest"
    assert not [e for e in cx.EVENTS if e in ("PreToolUse", "PostToolUse")], "per-tool hooks are noise, and PreToolUse can block a tool"


def test_install_registers_every_event_with_the_right_wrapper_async_only_where_codex_honours_it():
    data = cx.install({}, APP, remote_approve=True, approve_timeout=90)
    assert set(data) == {"hooks"}, "Codex rejects a hooks.json with any top-level key but hooks and description"
    assert set(data["hooks"]) == set(cx.EVENTS) | {"PermissionRequest"}
    for ev in STANDARD:
        assert data["hooks"][ev] == [{"hooks": [ASYNC_HOOK]}], ev
    # Interrupt and SessionEnd: Codex clamps their timeout to 3 s and runs a SessionEnd hook synchronously even when it says async
    for ev in ("Interrupt", "SessionEnd"):
        assert data["hooks"][ev] == [{"hooks": [{"type": "command", "command": FAST_CMD, "timeout": 3}]}], ev
    assert data["hooks"]["PermissionRequest"] == [{"hooks": [{"type": "command", "command": PERM_CMD, "timeout": 120}]}]
    every = [h for groups in data["hooks"].values() for g in groups for h in g["hooks"]]
    assert {h["command"].split()[-1].rsplit("/", 1)[1] for h in every if "async" in h} == {"ccboard-hook"}
    assert [ev for ev, groups in data["hooks"].items() if "async" in groups[0]["hooks"][0]] == STANDARD
    assert all(set(h) <= {"type", "command", "async", "timeout"} and isinstance(h["timeout"], int) for h in every), "timeout must be an integer"
    assert all(h["timeout"] <= 3 for ev in ("Interrupt", "SessionEnd") for h in (data["hooks"][ev][0]["hooks"][0],))


def test_the_sync_fallback_writes_no_async_key_anywhere():
    """For a codex build that skips async hooks: --no-async / CCBOARD_CODEX_HOOKS_ASYNC=0."""
    data = cx.install({}, APP, async_hooks=False)
    for ev in STANDARD:
        assert data["hooks"][ev] == [{"hooks": [SYNC_HOOK]}], ev
    assert not [h for groups in data["hooks"].values() for g in groups for h in g["hooks"] if "async" in h]
    # switching in either direction rewrites the entries where they sit
    up = cx.install(copy.deepcopy(data), APP)
    assert up == cx.install({}, APP) and positions(up) == positions(data)
    assert cx.install(copy.deepcopy(up), APP, async_hooks=False) == data


def test_command_strings_are_byte_stable():
    """Codex trusts a hook by a hash of its definition: these definitions may only change on purpose (and then every box re-reviews)."""
    base = "/home/rpandox/.local/share/ccboard/app/bin"
    assert cx.wanted_hooks(Path("/home/rpandox/.local/share/ccboard/app")) == {
        **{ev: {"type": "command", "command": f"env CCBOARD_AGENT=codex {base}/ccboard-hook", "async": True, "timeout": 5} for ev in STANDARD},
        "Interrupt": {"type": "command", "command": f"env CCBOARD_AGENT=codex {base}/ccboard-hook-fast", "timeout": 3},
        "SessionEnd": {"type": "command", "command": f"env CCBOARD_AGENT=codex {base}/ccboard-hook-fast", "timeout": 3},
        "PermissionRequest": {"type": "command", "command": f"env CCBOARD_AGENT=codex {base}/ccboard-permission", "timeout": 120},
    }
    assert list(cx.wanted_hooks(APP)) == cx.EVENTS + ["PermissionRequest"]


def test_every_hook_names_its_agent_the_permission_hook_included():
    """A Hermes or `codex exec` run fires the global hooks too: each command names `codex` itself, so the board can tell whose it is."""
    data = cx.install({}, APP)
    for ev in [*cx.EVENTS, "PermissionRequest"]:
        assert shlex.split(cmds(data, ev)[0])[:2] == ["env", "CCBOARD_AGENT=codex"], ev
    assert cmds(data, "PermissionRequest") == [PERM_CMD]


def test_an_install_from_before_the_permission_prefix_is_rewritten_in_place():
    """The bare ccboard-permission command of the first v0.5.11 build becomes the prefixed one where it sits (its trust key keeps its
    group and index: nothing was trusted against the old string before this fix shipped)."""
    old = {"hooks": {"PermissionRequest": [{"hooks": [FOREIGN]}, {"hooks": [{"type": "command", "command": "/opt/ccboard/bin/ccboard-permission",
                                                                              "timeout": 120}]}]}}
    new = cx.install(old, APP)
    assert new["hooks"]["PermissionRequest"] == [{"hooks": [FOREIGN]}, {"hooks": [{"type": "command", "command": PERM_CMD, "timeout": 120}]}]
    assert cx.install(copy.deepcopy(new), APP) == new, "idempotent"


def test_a_path_with_a_space_is_quoted_so_the_shell_runs_it():
    cmd = cx.wanted_hooks(Path("/srv/my apps/ccboard"))["Stop"]["command"]
    assert shlex.split(cmd) == ["env", "CCBOARD_AGENT=codex", "/srv/my apps/ccboard/bin/ccboard-hook"]
    assert shlex.split(cx.wanted_hooks(Path("/srv/my apps/ccboard"))["PermissionRequest"]["command"]) == \
        ["env", "CCBOARD_AGENT=codex", "/srv/my apps/ccboard/bin/ccboard-permission"]


def test_no_remote_approve_skips_the_permission_hook_and_the_timeout_follows_the_setting():
    assert "PermissionRequest" not in cx.install({}, APP, remote_approve=False)["hooks"]
    perm = cx.install({}, APP, approve_timeout=45)["hooks"]["PermissionRequest"][0]["hooks"][0]
    assert perm["timeout"] == 75


def test_turning_remote_approve_off_takes_an_installed_permission_hook_out_and_keeps_foreign_ones():
    on = cx.install({"hooks": {"PermissionRequest": [{"hooks": [FOREIGN]}]}}, APP)
    assert len(on["hooks"]["PermissionRequest"]) == 2
    off = cx.install(copy.deepcopy(on), APP, remote_approve=False)
    assert off["hooks"]["PermissionRequest"] == [{"hooks": [FOREIGN]}]
    assert "PermissionRequest" not in cx.install(cx.install({}, APP), APP, remote_approve=False)["hooks"]


# ------------------------------------------------------------------------------------------------ merging
def test_install_is_idempotent_and_keeps_foreign_hooks():
    start = {"description": "mine", "hooks": {"Stop": [{"hooks": [FOREIGN]}], "PreToolUse": [{"matcher": "Bash", "hooks": [FOREIGN]}]}}
    once = cx.install(copy.deepcopy(start), APP)
    twice = cx.install(copy.deepcopy(once), APP)
    thrice = cx.install(copy.deepcopy(twice), APP)
    assert once == twice == thrice
    assert once["description"] == "mine"
    assert cmds(once, "Stop").count("/usr/local/bin/mine") == 1 and len(ours(once, "Stop")) == 1
    assert once["hooks"]["PreToolUse"] == start["hooks"]["PreToolUse"]


def test_a_foreign_hook_sharing_a_group_with_ours_keeps_its_group_and_ours_stays_beside_it():
    shared = {"hooks": {"Stop": [{"matcher": "x", "hooks": [FOREIGN, {"type": "command", "command": "/old/bin/ccboard-hook"}]}]}}
    out = cx.install(copy.deepcopy(shared), APP)
    assert out["hooks"]["Stop"] == [{"matcher": "x", "hooks": [FOREIGN, ASYNC_HOOK]}]
    assert cx.install(copy.deepcopy(out), APP) == out


def test_ccboards_entries_never_move_so_codexs_trust_keys_survive_a_reinstall():
    """The key Codex stores a review under is <file>:<event>:<group index>:<hook index>: a re-install, a new app dir or a new
    timeout must rewrite the entry where it sits, and a new event goes after whatever is there."""
    first = cx.install({"hooks": {"SessionStart": [{"hooks": [FOREIGN]}], "Stop": [{"hooks": [FOREIGN]}]}}, APP)
    assert positions(first)[positions(first).index(("SessionStart", 1, 0))]       # appended after the foreign group
    assert ("Stop", 1, 0) in positions(first)
    # somebody adds hooks after ours, in a new group and in ours
    mid = copy.deepcopy(first)
    mid["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "/later/group"}]})
    mid["hooks"]["SessionStart"][1]["hooks"].append({"type": "command", "command": "/later/sibling"})
    before = positions(mid)
    for app, approve in ((APP, 90), (Path("/elsewhere/app"), 90), (APP, 20)):
        again = cx.install(copy.deepcopy(mid), app, approve_timeout=approve)
        assert positions(again) == before, (app, approve)
        assert [g["hooks"] for g in again["hooks"]["Stop"]][2] == [{"type": "command", "command": "/later/group"}]
        assert again["hooks"]["SessionStart"][1]["hooks"][1] == {"type": "command", "command": "/later/sibling"}
    moved = cx.install(copy.deepcopy(mid), Path("/elsewhere/app"))
    assert ours(moved, "Stop")[0]["command"] == "env CCBOARD_AGENT=codex /elsewhere/app/bin/ccboard-hook", "the entry is rewritten in place"


def test_an_old_install_is_upgraded_in_place_and_duplicates_collapse():
    old = {"hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "/opt/ccboard/bin/ccboard-hook", "timeout": 9}]}],
        "SessionEnd": [{"hooks": [FOREIGN]}, {"hooks": [{"type": "command", "command": "/x/ccboard-hook", "async": True}]},
                       {"hooks": [{"type": "command", "command": "/y/ccboard-hook-fast"}]}]}}
    new = cx.install(old, APP)
    assert new["hooks"]["Stop"] == [{"hooks": [ASYNC_HOOK]}], "a hook of an older definition is rewritten to the current one"
    assert new["hooks"]["SessionEnd"] == [{"hooks": [FOREIGN]}, {"hooks": [{"type": "command", "command": FAST_CMD, "timeout": 3}]}]


def test_strip_ours_removes_only_ours():
    data = cx.install({"hooks": {"Stop": [{"hooks": [FOREIGN]}], "SessionEnd": [{"hooks": [FOREIGN]}]}}, APP)
    out = cx.strip_ours(copy.deepcopy(data))
    assert out["hooks"] == {"Stop": [{"hooks": [FOREIGN]}], "SessionEnd": [{"hooks": [FOREIGN]}]}
    lone = {"hooks": {"Interrupt": [{"hooks": [{"type": "command", "command": "/x/ccboard-hook-fast"}]}],
                      "PermissionRequest": [{"hooks": [{"type": "command", "command": "/x/ccboard-permission"}]}]}}
    assert cx.strip_ours(lone) == {}
    assert cx.strip_ours(cx.install({}, APP)) == {}
    foreign_only = {"hooks": {"Stop": [{"hooks": [FOREIGN]}]}, "description": "d"}
    assert cx.strip_ours(copy.deepcopy(foreign_only)) == foreign_only


def test_a_non_command_entry_that_names_our_script_is_not_ours():
    odd = {"type": "prompt", "command": "/x/ccboard-hook"}
    assert not cx.is_ours(odd) and not cx.is_ours("ccboard-hook") and not cx.is_ours(None)
    assert cx.strip_ours({"hooks": {"Stop": [{"hooks": [odd]}]}}) == {"hooks": {"Stop": [{"hooks": [odd]}]}}


def test_shapes_we_do_not_know_survive_a_strip_and_stop_an_install():
    odd = {"hooks": {"Stop": [{"hooks": [FOREIGN]}, "not-a-group", {"hooks": "nope"}], "Weird": {"a": 1}}}
    assert cx.strip_ours(copy.deepcopy(odd)) == odd
    with pytest.raises(RuntimeError, match="hooks.*not an object"):
        cx.install({"hooks": ["x"]}, APP)
    with pytest.raises(RuntimeError, match="hooks.Stop is not a list"):
        cx.install({"hooks": {"Stop": {"a": 1}}}, APP)
    assert cx.install({"hooks": None}, APP)["hooks"]["Stop"]
    kept = cx.install({"hooks": {"Stop": [{"hooks": [FOREIGN]}, "not-a-group"]}}, APP)
    assert kept["hooks"]["Stop"][:2] == [{"hooks": [FOREIGN]}, "not-a-group"] and len(ours(kept, "Stop")) == 1


# ------------------------------------------------------------------------------------------------ the CLI
def run_script(*args, env_extra=None, drop=()):
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=30)


def test_cli_install_show_remove_on_a_hooks_file(tmp_path):
    f = tmp_path / "x.json"
    f.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [FOREIGN]}]}}))
    r = run_script("install", "--hooks", str(f), "--app-dir", str(APP))
    assert r.returncode == 0, r.stderr
    assert "install:" in r.stdout and "trust-help" in r.stdout, "the next step is named"
    first = f.read_text()
    assert run_script("install", "--hooks", str(f), "--app-dir", str(APP)).returncode == 0
    assert f.read_text() == first, "re-running changes nothing, byte for byte"
    shown = json.loads(run_script("show", "--hooks", str(f)).stdout)
    assert set(shown["hooks"]) == set(cx.EVENTS) | {"PermissionRequest"}
    r = run_script("remove", "--hooks", str(f))
    assert r.returncode == 0
    assert json.loads(f.read_text()) == {"hooks": {"Stop": [{"hooks": [FOREIGN]}]}}


def test_cli_no_remote_approve_and_approve_timeout(tmp_path):
    f = tmp_path / "x.json"
    assert run_script("install", "--hooks", str(f), "--app-dir", str(APP), "--no-remote-approve").returncode == 0
    assert "PermissionRequest" not in json.loads(f.read_text())["hooks"]
    assert run_script("install", "--hooks", str(f), "--app-dir", str(APP), "--approve-timeout", "30").returncode == 0
    assert json.loads(f.read_text())["hooks"]["PermissionRequest"][0]["hooks"][0]["timeout"] == 60


def test_cli_no_async_flag_and_env_switch_write_every_hook_synchronous(tmp_path):
    f = tmp_path / "x.json"
    assert run_script("install", "--hooks", str(f), "--app-dir", str(APP), "--no-async").returncode == 0
    assert "async" not in f.read_text()
    g = tmp_path / "y.json"
    assert run_script("install", "--hooks", str(g), "--app-dir", str(APP), env_extra={"CCBOARD_CODEX_HOOKS_ASYNC": "0"}).returncode == 0
    assert g.read_text() == f.read_text()
    h = tmp_path / "z.json"
    assert run_script("install", "--hooks", str(h), "--app-dir", str(APP), env_extra={"CCBOARD_CODEX_HOOKS_ASYNC": "1"}).returncode == 0
    assert json.loads(h.read_text())["hooks"]["Stop"][0]["hooks"][0]["async"] is True


def test_cli_removing_everything_leaves_an_empty_object_codex_accepts(tmp_path):
    f = tmp_path / "x.json"
    run_script("install", "--hooks", str(f), "--app-dir", str(APP))
    assert run_script("remove", "--hooks", str(f)).returncode == 0
    assert json.loads(f.read_text()) == {}


def test_cli_default_path_follows_codex_home(tmp_path):
    ch = tmp_path / "codex_home"
    r = run_script("install", "--app-dir", str(APP), env_extra={"CODEX_HOME": str(ch)})
    assert r.returncode == 0, r.stderr
    assert (ch / "hooks.json").is_file(), "CODEX_HOME is created when it does not exist yet"
    assert not (Path(os.environ["HOME"]) / ".codex").exists()
    assert str(ch / "hooks.json") in r.stdout
    assert run_script("remove", env_extra={"CODEX_HOME": str(ch)}).returncode == 0
    assert json.loads((ch / "hooks.json").read_text()) == {}


def test_cli_default_path_is_home_dot_codex_and_an_empty_codex_home_counts_as_unset():
    r = run_script("install", "--app-dir", str(APP), env_extra={"CODEX_HOME": ""})
    assert r.returncode == 0, r.stderr
    assert (Path(os.environ["HOME"]) / ".codex" / "hooks.json").is_file()
    assert cx.hooks_path() == Path(os.environ["HOME"]) / ".codex" / "hooks.json"


def test_cli_refuses_a_file_it_cannot_read(tmp_path):
    f = tmp_path / "x.json"
    f.write_text("{not json")
    r = run_script("install", "--hooks", str(f), "--app-dir", str(APP))
    assert r.returncode != 0 and "not valid JSON" in r.stderr and f.read_text() == "{not json"
    f.write_text("[]")
    assert run_script("install", "--hooks", str(f), "--app-dir", str(APP)).returncode != 0
    f.write_text(json.dumps({"hooks": {"Stop": "x"}}))
    r = run_script("install", "--hooks", str(f), "--app-dir", str(APP))
    assert r.returncode != 0 and "hooks.Stop is not a list" in r.stderr
    assert json.loads(f.read_text()) == {"hooks": {"Stop": "x"}}


def test_cli_leaves_no_temp_file_behind(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    f = d / "x.json"
    run_script("install", "--hooks", str(f), "--app-dir", str(APP))
    run_script("remove", "--hooks", str(f))
    assert sorted(p.name for p in d.iterdir()) == ["x.json"]


# ------------------------------------------------------------------------------------------------ trust-help
def test_trust_help_says_how_to_trust_and_what_bypass_costs(tmp_path):
    f = tmp_path / "hooks.json"
    r = run_script("trust-help", "--hooks", str(f))
    assert r.returncode == 0 and "no ccboard hooks yet" in r.stdout and "install" in r.stdout
    run_script("install", "--hooks", str(f), "--app-dir", str(APP))
    before = f.read_text()
    out = run_script("trust-help", "--hooks", str(f)).stdout
    assert "10 ccboard hooks are in" in out and str(f) in out
    for needle in ("/hooks", "trust", "need review", "no hooks (untrusted?)", "trusted_hash", "session_start",
                   "CCBOARD_CODEX_HOOK_TRUST=bypass", "--dangerously-bypass-hook-trust", ".codex/hooks.json", "review"):
        assert needle in out, needle
    assert out.endswith("\n")
    assert f.read_text() == before, "trust-help only reads"


# ------------------------------------------------------------------------------------------------ the hook command, run as Codex runs it
def _serve_once():
    import http.server
    import threading
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got["path"] = self.path
            got["headers"] = {k.lower(): v for k, v in self.headers.items()}
            got["body"] = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    return srv, got


@pytest.mark.parametrize("event,wrapper", [("Stop", "ccboard-hook"), ("Interrupt", "ccboard-hook-fast")])
def test_the_registered_command_runs_through_a_shell_and_reports_as_codex(tmp_path, event, wrapper):
    """Codex runs a hook's command through a shell with the payload on stdin (checked on 0.157.1); the board must see agent codex even
    when the session's own environment says nothing (or says claude)."""
    data = cx.install({}, ROOT)
    cmd = data["hooks"][event][0]["hooks"][0]["command"]
    assert wrapper in cmd
    srv, got = _serve_once()
    tok = tmp_path / "tok"
    tok.write_text("sekret\n")
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("CCBOARD_")}, "CCBOARD_URL": f"http://127.0.0.1:{srv.server_port}",
           "CCBOARD_HOOK_TOKEN_FILE": str(tok), "CCBOARD_SESSION": "shop--api--x", "CCBOARD_AGENT": "claude"}
    r = subprocess.run(["sh", "-c", cmd], input=b'{"hook_event_name":"Stop","session_id":"s1"}', capture_output=True, env=env, timeout=10)
    srv.server_close()
    assert r.returncode == 0 and r.stdout == b""
    assert got["path"] == "/api/hook" and json.loads(got["body"])["session_id"] == "s1"
    h = got["headers"]
    assert h["x-ccboard-agent"] == "codex" and h["x-ccboard-session"] == "shop--api--x" and h["x-ccboard-token"] == "sekret"


def _permission_run(tmp_path, cmd, **extra_env):
    """Run a PermissionRequest command through a shell against a one-shot board that answers {}: returns the request the board saw."""
    srv, got = _serve_once()
    tok = tmp_path / "tok"
    tok.write_text("sekret\n")
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("CCBOARD_")}, "CCBOARD_URL": f"http://127.0.0.1:{srv.server_port}",
           "CCBOARD_HOOK_TOKEN_FILE": str(tok), "CCBOARD_SESSION": "shop--api--x", **extra_env}
    r = subprocess.run(["sh", "-c", cmd], input=b'{"tool_name":"Bash","tool_input":{"command":"ls"}}', capture_output=True, env=env, timeout=10)
    srv.server_close()
    assert r.returncode == 0 and r.stdout == b"", "an answer without allow/deny prints nothing: the TUI prompt shows"
    return got


def test_the_permission_command_reports_as_codex_and_the_claude_script_as_claude(tmp_path):
    cmd = cx.install({}, ROOT)["hooks"]["PermissionRequest"][0]["hooks"][0]["command"]
    got = _permission_run(tmp_path, cmd, CCBOARD_AGENT="claude")           # the command's own prefix wins over the session's environment
    assert got["path"] == "/api/permission" and got["headers"]["x-ccboard-agent"] == "codex"
    assert got["headers"]["x-ccboard-session"] == "shop--api--x" and got["headers"]["x-ccboard-token"] == "sekret"
    got = _permission_run(tmp_path, str(ROOT / "bin" / "ccboard-permission"))      # Claude's settings.json entry: no prefix, no env
    assert got["headers"]["x-ccboard-agent"] == "claude"
    got = _permission_run(tmp_path, str(ROOT / "bin" / "ccboard-permission"), CCBOARD_AGENT="shell")
    assert got["headers"]["x-ccboard-agent"] == "shell"


# ------------------------------------------------------------------------------------------------ install.sh
def _install_text():
    return INSTALL.read_text()


def test_install_sh_syntax():
    assert subprocess.run(["bash", "-n", str(INSTALL)], capture_output=True, text=True).returncode == 0


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
def test_install_sh_and_entrypoint_shellcheck():
    r = subprocess.run(["shellcheck", "-S", "error", str(INSTALL)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
    r = subprocess.run(["shellcheck", "-s", "sh", "--severity=warning", str(ENTRYPOINT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_install_sh_remembers_the_two_new_env_keys_binds_and_validates_them():
    t = _install_text()
    keys = re.search(r"^ENV_KEYS=\((.*)\)$", t, re.M).group(1).split()
    assert "CCBOARD_CODEX_HOOK_TRUST" in keys and "CODEX_HOME" in keys
    assert ': "${CCBOARD_CODEX_HOOK_TRUST:=review}"' in t and ': "${CODEX_HOME:=}"' in t, "an unbound ENV_KEYS name dies under set -u"
    assert 'case "$CCBOARD_CODEX_HOOK_TRUST" in review|bypass) ;;' in t
    assert '[[ "$CODEX_HOME" = /* ]]' in t
    assert re.search(r"for k in PROJECTS_DIR [^\n]*CODEX_HOME; do", t), "CODEX_HOME goes through the no-whitespace/quotes check: the env file is data"
    assert re.search(r'warn "CCBOARD_CODEX_HOOK_TRUST=bypass', t), "bypass is announced at install time"


def _codex_block():
    t = _install_text()
    m = re.search(r"^# -+ Codex: hooks\.json \+ the MCP server.*?\n(.*?)^# -+ sudoers", t, re.S | re.M)
    assert m, "install.sh lost its Codex block"
    return m.group(1)


def test_install_sh_codex_block_is_guarded_never_aborts_and_sits_after_the_claude_steps():
    t, b = _install_text(), _codex_block()
    assert t.index('log "MCP server registration"') < t.index('log "Codex hooks and MCP server"') < t.index('log "sudoers rule for restarts"')
    assert 'if have codex; then CODEX_BIN=$(command -v codex); elif [ -x "$HOME_DIR/.local/bin/codex" ]' in b
    assert 'if [ -z "$CODEX_BIN" ]; then' in b and "codex is not installed" in b
    assert not re.search(r"\bdie\b|\bexit\b", b), "a Codex problem is a warning, never an abort"
    assert b.count("scripts/codex_hooks.py") == 2 and b.count("|| warn") >= 2
    assert '|| true' in b.split("codex_mcp_register \"$CODEX_BIN\"")[1].splitlines()[0]
    # docker: hooks and the MCP shim come from the data dir the container keeps current, with the host's python
    assert 'CODEX_APP="$DOCKER_APP"; CODEX_PY=/usr/bin/python3' in b
    assert 'CODEX_APP="$APP_DIR"; CODEX_PY="$APP_DIR/.venv/bin/python"' in b
    assert "--no-remote-approve" in b and '--approve-timeout "${CCBOARD_APPROVE_TIMEOUT:-90}"' in b
    assert '"http://127.0.0.1:$CCBOARD_PORT"' in b


def _register_fn():
    m = re.search(r"^codex_mcp_register\(\) \{.*?^\}\n", _install_text(), re.S | re.M)
    assert m, "install.sh lost codex_mcp_register"
    return m.group(0)


class FakeCodex:
    """A codex that logs its argv and keeps one MCP entry in a state file; `json` False makes `mcp get --json` fail (an older codex)."""

    def __init__(self, tmp: Path, json_get: bool = True, add_rc: int = 0, remove_rc: int = 0):
        self.dir = tmp / "fakebin"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = tmp / "codex.log"
        self.state = tmp / "codex.state"
        q, st = shlex.quote(str(self.log)), shlex.quote(str(self.state))
        self.bin = self.dir / "codex"
        self.bin.write_text(f"""#!/bin/sh
echo "codex $*" >> {q}
echo "CODEX_HOME=${{CODEX_HOME-unset}}" >> {q}.env
case "$1 $2" in
  "mcp get")
    [ -f {st} ] || {{ echo "Error: No MCP server named 'ccboard' found." >&2; exit 1; }}
    if [ "$4" = "--json" ]; then
      {"" if json_get else 'exit 2'}
      cat {st}
    else
      echo "ccboard"; echo "  command: $(sed -n 1p {st})"; echo "  args: $(sed -n 2p {st})"; echo "  env: CCBOARD_URL=*****"
    fi
    exit 0;;
  "mcp add")
    [ {add_rc} -eq 0 ] || exit {add_rc}
    printf '%s\\n%s\\n' "$7" "$8" > {st}.plain
    printf '{{"name":"ccboard","transport":{{"type":"stdio","command":"%s","args":["%s"],"env":{{"CCBOARD_URL":"%s"}}}}}}\\n' "$7" "$8" "${{5#CCBOARD_URL=}}" > {st}
    exit 0;;
  "mcp remove") rm -f {st}; exit {remove_rc};;
esac
exit 0
""")
        self.bin.chmod(0o755)

    def register(self, command="/usr/bin/python3", script="/data/app/scripts/ccboard_mcp.py", url="http://127.0.0.1:8000"):
        self.state.write_text(json.dumps({"name": "ccboard", "transport": {"type": "stdio", "command": command, "args": [script],
                                                                           "env": {"CCBOARD_URL": url}}}) + "\n")

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def envs(self):
        """What each call saw as CODEX_HOME: the path, `unset`, or an empty string (set but empty)."""
        p = Path(str(self.log) + ".env")
        return [ln.split("=", 1)[1] for ln in p.read_text().splitlines()] if p.exists() else []


def _run_register(fake: FakeCodex, py="/usr/bin/python3", script="/data/app/scripts/ccboard_mcp.py", url="http://127.0.0.1:8000"):
    body = f"""set -euo pipefail
note() {{ echo "note: $*"; }}
warn() {{ echo "warn: $*"; }}
{_register_fn()}
codex_mcp_register {shlex.quote(str(fake.bin))} {shlex.quote(py)} {shlex.quote(script)} {shlex.quote(url)}
"""
    return subprocess.run(["bash", "-c", body], capture_output=True, text=True, timeout=30)


def test_register_adds_the_server_with_the_board_url_and_a_host_valid_command(tmp_path):
    fake = FakeCodex(tmp_path)
    r = _run_register(fake, url="http://127.0.0.1:8123")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "codex mcp add ccboard --env CCBOARD_URL=http://127.0.0.1:8123 -- /usr/bin/python3 /data/app/scripts/ccboard_mcp.py" in fake.calls()
    assert "registered 'ccboard' with Codex" in r.stdout
    again = _run_register(fake, url="http://127.0.0.1:8123")           # the entry it just made is current: nothing more happens
    assert again.returncode == 0 and "present" in again.stdout
    assert len([c for c in fake.calls() if c.startswith("codex mcp add")]) == 1


def test_register_repoints_a_stale_script_or_board_url_and_leaves_a_current_entry_alone(tmp_path):
    fake = FakeCodex(tmp_path)
    fake.register()
    r = _run_register(fake)
    assert r.returncode == 0 and "present" in r.stdout and not [c for c in fake.calls() if "mcp add" in c or "mcp remove" in c]
    stale = FakeCodex(tmp_path / "s1")
    stale.register(script="/old/checkout/scripts/ccboard_mcp.py")
    r = _run_register(stale)
    assert r.returncode == 0 and "re-pointing" in r.stdout
    assert [c for c in stale.calls() if "mcp remove" in c] and [c for c in stale.calls() if "mcp add" in c]
    port = FakeCodex(tmp_path / "s2")
    port.register(url="http://127.0.0.1:9999")
    r = _run_register(port, url="http://127.0.0.1:8000")
    assert "re-pointing" in r.stdout and "CCBOARD_URL=http://127.0.0.1:8000" in port.calls()[-1]


def test_register_on_a_codex_without_get_json_checks_the_script_only(tmp_path):
    fake = FakeCodex(tmp_path, json_get=False)
    fake.register()
    r = _run_register(fake)                      # env values are masked in the plain form: the path matches, so it is left alone
    assert r.returncode == 0 and "present" in r.stdout and not [c for c in fake.calls() if "mcp add" in c]
    gone = FakeCodex(tmp_path / "g", json_get=False)
    gone.register(script="/old/ccboard_mcp.py")
    assert "re-pointing" in _run_register(gone).stdout


def test_register_failures_warn_and_return_nonzero_without_exiting_the_shell(tmp_path):
    fake = FakeCodex(tmp_path, add_rc=3)
    r = _run_register(fake)
    assert r.returncode != 0 and "warn: codex mcp add failed; register manually: codex mcp add ccboard" in r.stdout
    stuck = FakeCodex(tmp_path / "x", remove_rc=1)
    stuck.register(script="/old/ccboard_mcp.py")
    r = _run_register(stuck)
    assert r.returncode != 0 and "warn: codex mcp remove ccboard failed" in r.stdout and not [c for c in stuck.calls() if "mcp add" in c]


# ------------------------------------------------------------------------------------------------ docker-entrypoint.sh
def _exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


class Box:
    """A fake image dir, a fake host home and fake id/tmux/timeout/uvicorn (+ an optional fake codex), as test_docker_files' Box does."""

    def __init__(self, tmp: Path, codex: FakeCodex | None = None):
        self.home = tmp / "box_home"
        self.home.mkdir()
        self.app = tmp / "image"
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(ROOT / "bin", self.app / "bin", ignore=ignore)
        shutil.copytree(ROOT / "scripts", self.app / "scripts", ignore=ignore)
        shutil.copy(ROOT / "tmux.conf", self.app / "tmux.conf")
        self.log = tmp / "calls.log"
        q = shlex.quote(str(self.log))
        self.fakes = tmp / "fakes"
        _exe(self.fakes / "id", "echo 1000\n")
        _exe(self.fakes / "tmux", f'echo "tmux $*" >> {q}\n')
        _exe(self.fakes / "timeout", 'shift\nexec "$@"\n')
        _exe(self.app / ".venv" / "bin" / "uvicorn", f'echo "uvicorn $*" >> {q}\n')
        self.codex = codex
        if codex:
            shutil.copy(codex.bin, self.fakes / "codex")
        self.data = self.home / ".local" / "share" / "ccboard"

    def run(self, **extra: str) -> subprocess.CompletedProcess:
        env = {"PATH": f"{self.fakes}:/usr/bin:/bin", "HOME": str(self.home), "CCBOARD_APP_ROOT": str(self.app),
               "CCBOARD_HOST_PYTHON": sys.executable, "CCBOARD_PORT": "8123", "LANG": "C"}
        env.update(extra)
        return subprocess.run([str(ENTRYPOINT)], env=env, capture_output=True, text=True, timeout=60)

    def hooks(self, codex_home: Path | None = None) -> dict:
        return json.loads(((codex_home or self.home / ".codex") / "hooks.json").read_text())


def test_entrypoint_merges_the_codex_hooks_and_registers_the_mcp_server_when_codex_exists(tmp_path):
    box = Box(tmp_path, FakeCodex(tmp_path))
    r = box.run()
    assert r.returncode == 0, r.stderr
    app = box.data / "app"
    assert (app / "scripts" / "codex_hooks.py").is_file()
    hooks = box.hooks()
    assert set(hooks["hooks"]) == set(cx.EVENTS) | {"PermissionRequest"}
    assert cmds(hooks, "Stop") == [f"env CCBOARD_AGENT=codex {app / 'bin' / 'ccboard-hook'}"]
    assert cmds(hooks, "PermissionRequest") == [f"env CCBOARD_AGENT=codex {app / 'bin' / 'ccboard-permission'}"]
    calls = box.codex.calls()
    assert f"codex mcp add ccboard --env CCBOARD_URL=http://127.0.0.1:8123 -- {sys.executable} {app / 'scripts' / 'ccboard_mcp.py'}" in calls
    assert [c for c in (box.log.read_text().splitlines()) if c.startswith("uvicorn ")], "the board still starts"


def test_entrypoint_codex_steps_are_idempotent(tmp_path):
    box = Box(tmp_path, FakeCodex(tmp_path))
    assert box.run().returncode == 0
    first = (box.home / ".codex" / "hooks.json").read_text()
    assert box.run().returncode == 0
    assert (box.home / ".codex" / "hooks.json").read_text() == first
    assert len([c for c in box.codex.calls() if c.startswith("codex mcp add")]) == 1, "the entry it made is current on the second start"
    assert not [c for c in box.codex.calls() if c.startswith("codex mcp remove")]


def test_entrypoint_repoints_a_codex_mcp_entry_that_names_another_checkout(tmp_path):
    fake = FakeCodex(tmp_path)
    fake.register(script="/home/u/ccboard/scripts/ccboard_mcp.py")
    box = Box(tmp_path, fake)
    assert box.run().returncode == 0
    calls = fake.calls()
    assert any(c.startswith("codex mcp remove ccboard") for c in calls) and any(c.startswith("codex mcp add ccboard") for c in calls)


def test_entrypoint_without_codex_skips_both_steps_and_says_so(tmp_path):
    box = Box(tmp_path)
    r = box.run()
    assert r.returncode == 0, r.stderr
    assert "codex is not on PATH" in r.stdout and not (box.home / ".codex").exists()


def test_entrypoint_shadow_run_leaves_codex_alone(tmp_path):
    box = Box(tmp_path, FakeCodex(tmp_path))
    r = box.run(CCBOARD_SHADOW="1", CCBOARD_DATA_DIR=str(box.home / ".local" / "share" / "ccboard-shadow"))
    assert r.returncode == 0, r.stderr
    assert not (box.home / ".codex").exists() and box.codex.calls() == []


def test_entrypoint_hands_codex_an_unset_codex_home_when_the_env_file_says_empty(tmp_path):
    """/etc/ccboard/env carries `CODEX_HOME=` (empty): the container's codex calls must see it unset, not as an empty path (0.145 is
    only known to treat the empty value as unset through `codex mcp list`; the guard makes that irrelevant)."""
    box = Box(tmp_path, FakeCodex(tmp_path))
    assert box.run(CODEX_HOME="").returncode == 0
    envs = box.codex.envs()
    assert envs and set(envs) == {"unset"}, envs
    assert (box.home / ".codex" / "hooks.json").is_file(), "hooks.json went to the default home, as for Codex itself"
    ch = tmp_path / "set"
    ch.mkdir()
    again = Box(ch, FakeCodex(ch))
    assert again.run(CODEX_HOME=str(ch / "elsewhere")).returncode == 0
    assert set(again.codex.envs()) == {str(ch / "elsewhere")}, "a real value is left alone"


def test_entrypoint_honours_codex_home_and_the_remote_approve_switch(tmp_path):
    box = Box(tmp_path, FakeCodex(tmp_path))
    ch = tmp_path / "elsewhere"
    ch.mkdir()
    assert box.run(CODEX_HOME=str(ch), CCBOARD_REMOTE_APPROVE="0").returncode == 0
    assert not (box.home / ".codex").exists()
    hooks = box.hooks(ch)
    assert "PermissionRequest" not in hooks["hooks"] and "Stop" in hooks["hooks"]
    on = tmp_path / "on"
    on.mkdir()
    box2 = Box(on, FakeCodex(on))
    assert box2.run(CCBOARD_APPROVE_TIMEOUT="45").returncode == 0
    assert box2.hooks()["hooks"]["PermissionRequest"][0]["hooks"][0]["timeout"] == 75


def test_entrypoint_codex_failures_never_block_the_board(tmp_path):
    box = Box(tmp_path, FakeCodex(tmp_path, add_rc=1))
    r = box.run()
    assert r.returncode == 0, r.stderr
    assert "codex mcp add failed" in r.stderr
    broken = tmp_path / "b"
    broken.mkdir()
    box2 = Box(broken, FakeCodex(broken))
    r = box2.run(CCBOARD_HOST_PYTHON=str(broken / "no-such-python"))
    assert r.returncode == 0 and "could not merge the Codex hooks" in r.stderr
    assert [c for c in box2.log.read_text().splitlines() if c.startswith("uvicorn ")]


def test_entrypoint_text_keeps_its_rules():
    t = ENTRYPOINT.read_text()
    assert "443" not in t and "[[" not in t
    assert "command -v codex" in t and "codex_hooks.py" in t and "codex mcp add" in t


# ------------------------------------------------------------------------------------------------ the real codex binary (opt-in)
@pytest.mark.skipif(not (shutil.which("codex") and os.environ.get("CCBOARD_TEST_CODEX_BINARY") == "1"),
                    reason="set CCBOARD_TEST_CODEX_BINARY=1 with codex on PATH: it drives `codex app-server` in a temp HOME")
def test_codex_parses_the_installed_hooks_without_warnings(tmp_path):
    """The same check the box runs: Codex's own parser (app-server hooks/list) takes every ccboard hook, as sync, with our timeouts."""
    import select
    import time
    home = tmp_path / "codex_probe_home"
    (home / ".codex").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    env = {**os.environ, "HOME": str(home), "CODEX_HOME": str(home / ".codex")}
    r = subprocess.run([sys.executable, str(SCRIPT), "install", "--app-dir", str(APP)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    p = subprocess.Popen(["codex", "app-server", "--listen", "stdio://"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=env, text=True, cwd=str(proj))

    def send(o):
        p.stdin.write(json.dumps(o) + "\n")
        p.stdin.flush()

    def recv(i):
        end = time.time() + 30
        while time.time() < end:
            if select.select([p.stdout], [], [], 0.5)[0]:
                m = json.loads(p.stdout.readline())
                if m.get("id") == i:
                    return m
        raise AssertionError(f"no answer to request {i}")

    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "ccboard-test", "version": "0"}}})
        recv(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "hooks/list", "params": {"cwds": [str(proj)]}})
        entry = recv(2)["result"]["data"][0]
    finally:
        p.terminate()
    assert entry["warnings"] == [] and entry["errors"] == []
    got = {h["eventName"]: h for h in entry["hooks"]}
    want = {"sessionStart", "userPromptSubmit", "stop", "subagentStart", "subagentStop", "preCompact", "postCompact", "interrupt",
            "sessionEnd", "permissionRequest"}
    assert set(got) == want, set(got) ^ want
    assert all(h["handlerType"] == "command" for h in got.values())
    assert {ev for ev, h in got.items() if h["async"]} == {"sessionStart", "userPromptSubmit", "stop", "subagentStart", "subagentStop",
                                                           "preCompact", "postCompact"}
    assert got["interrupt"]["timeoutSec"] == 3 and got["stop"]["timeoutSec"] == 5 and got["permissionRequest"]["timeoutSec"] == 120
