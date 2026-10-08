"""v0.5.4 commit A, the HTTP surface: GET /api/agents, /api/sessions/{name}, /api/doctor, /api/external, the /api/state
additions (and the legacy keys that must not move), the launch path through the adapter (_normalize_launcher, sessions storing
agent/cwd/opts, CCBOARD_AGENT), _end_session and the ended_reason it records, the has_worktree guard on every task endpoint that
runs git, hooks skipping PostToolBatch, and reboot recovery through the adapter.

Nothing here runs a real agent binary or touches ~/.claude: claude auth is stubbed, tmux is the conftest fake, and the Claude
config dir is the tmp one the projects_dir fixture sets (the registry tests copy tests/fixtures/claude_registry into it).
"""
import json
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import account_store, claude_auth, doctor, gitops, hooks, overlap, prpoll, projects, recover
from app.agents import registry
from app.config import settings

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
FIX = Path(__file__).parent / "fixtures" / "claude_registry"
S10001 = "c7725076-aa65-4020-9087-97c18a3d922b"            # the fixture session that is busy, has a bridge and job 4f9978a5
NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc).timestamp()
UUID1 = "11111111-1111-4111-8111-111111111111"
AUTH = {"installed": True, "version": "2.1.287 (Claude Code)", "loggedIn": True, "authMethod": "claude.ai",
        "email": "me@example.com", "subscriptionType": "max"}
SESSION_KEYS = {"tmux", "project", "repo", "name", "agent", "state", "state_at", "last_prompt", "last_message", "stats", "flags",
                "agent_session_id", "account", "task", "pending", "viewers", "win", "shell_version",
                "hooks_missing"}      # the plan's GET /api/sessions/{name}; hooks_missing: #96 (the terminal header's chip)


@pytest.fixture(autouse=True)
def _fresh_scan():
    """/api/state caches its project scan for 2 s in a module global; an earlier test's scan must not leak into this one."""
    from app import main
    main._invalidate_scan()
    yield
    main._invalidate_scan()


def git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)
    return path


@pytest.fixture
def board(lite_client, projects_dir, fake_tmux, monkeypatch):
    """The board without workers, the fake tmux, a fixed claude auth status, a fake claude binary and an empty registry cache."""
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(claude_auth, "status", lambda: dict(AUTH))
    monkeypatch.setattr(claude_auth, "version", lambda: AUTH["version"])
    monkeypatch.setattr(registry, "_IS_LINUX", False)              # no real /proc walk, ever
    monkeypatch.setattr(registry, "_wall", lambda: NOW)
    registry.invalidate()
    git_init(projects_dir / "shop" / "api")
    yield SimpleNamespace(client=lite_client, tmux=fake_tmux, projects=projects_dir)
    registry.invalidate()


def db():
    from app import main
    return main.db


def row_of(tmux_name):
    """The newest sessions row of a tmux name, open or ended."""
    with db().lock:
        r = db().conn.execute("SELECT * FROM sessions WHERE tmux_name=? ORDER BY id DESC LIMIT 1", (tmux_name,)).fetchone()
    return dict(r)


def new_session(board, launcher="shell", **body):
    r = board.client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": launcher, **body})
    assert r.status_code == 201, r.text
    return r.json()


def all_sessions(state):
    for p in state["projects"]:
        for r in p["repos"]:
            yield from r["sessions"]
        if p.get("root"):
            yield from p["root"]["sessions"]
        yield from p.get("orphan_sessions", [])


def state_session(board, name):
    st = board.client.get("/api/state", headers=H).json()
    return st, next(s for s in all_sessions(st) if s["tmux"] == name)


def unassigned_task(**kw):
    return db().task_add(project="shop", repo="api", slug="later", title="Later", prompt="do it later", **kw)


# ---------------------------------------------------------------- auth: every new endpoint is GET and behind the identity check

@pytest.mark.parametrize("path", ["/api/agents", "/api/doctor", "/api/external", "/api/accounts", "/api/sessions/shop--api--s1"])
def test_new_endpoints_need_an_identity_and_only_answer_get(board, path):
    assert board.client.get(path).status_code == 403
    assert board.client.get(path, headers={"Tailscale-User-Login": "mallory@example.com", "X-CCBoard": "1"}).status_code == 403
    assert board.client.post(path, headers=H, json={}).status_code in (403, 404, 405)
    if path != "/api/sessions/shop--api--s1":          # DELETE is a real route on the session itself
        assert board.client.delete(path, headers=H).status_code in (404, 405)


# ---------------------------------------------------------------- GET /api/agents

def test_api_agents_shape(board):
    r = board.client.get("/api/agents", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert list(body) == ["agents"] and list(body["agents"]) == ["claude", "codex"], "Claude and Codex (v0.5.11)"
    c = body["agents"]["claude"]
    assert {"name", "label", "glyph", "installed", "version", "auth", "hooks", "options", "permission_modes", "efforts", "models",
            "reasoning_by_model", "slash"} <= set(c)
    assert (c["name"], c["label"], c["glyph"]) == ("claude", "Claude", "◆")
    assert c["installed"] is True and c["version"] == AUTH["version"] and c["auth"]["loggedIn"] is True
    assert c["hooks"] == {"installed": False}                   # nothing installed in the tmp config dir
    assert c["efforts"] == ["low", "medium", "high", "xhigh", "max"] and "bypassPermissions" in c["permission_modes"]
    assert c["models"][:4] == ["opus", "fable", "sonnet", "haiku"]
    opt_keys = {"key", "label", "kind", "choices", "default", "help", "group", "danger", "when"}
    assert c["options"] and all(set(o) == opt_keys for o in c["options"])
    by_key = {o["key"]: o for o in c["options"]}
    assert {"model", "effort", "permission_mode", "bypass", "allowed_tools", "append_system_prompt", "add_dirs"} <= set(by_key)
    assert by_key["bypass"]["danger"] is True and by_key["effort"]["choices"] == c["efforts"]
    assert c["slash"] and all({"cmd", "label", "arg", "read", "verified", "weight", "destructive"} <= set(v) for v in c["slash"].values())
    blob = json.dumps(body).lower()
    assert "token" not in blob and "secret" not in blob and "credential" not in blob


def test_api_agents_reports_a_missing_claude(board, monkeypatch):
    monkeypatch.setattr(claude_auth, "status", lambda: {"installed": False, "loggedIn": False, "version": None})
    c = board.client.get("/api/agents", headers=H).json()["agents"]["claude"]
    assert c["installed"] is False and c["version"] is None and c["auth"]["loggedIn"] is False


# ---------------------------------------------------------------- GET /api/sessions/{name}

def test_api_session_detail_shape_and_values(board):
    s = new_session(board, "claude", name="dev")
    name = s["tmux"]
    hooks.apply(db(), name, "statusline", {"model": {"display_name": "Opus 5", "id": "claude-opus-5"},
                                           "context_window": {"used_percentage": 12, "context_window_size": 200000},
                                           "cost": {"total_cost_usd": 1.5}}, )
    hooks.apply(db(), name, "UserPromptSubmit", {"session_id": s["agent_session_id"], "prompt": "fix the login"})
    db().update_flags(name, {"transcript_path": "/home/x/.claude/projects/p/s.jsonl", "hook_seen": True}, {"subagents": 2})
    pid = db().perm_add(name, "Bash", "npm test", {"command": "npm test"})
    db().perm_add("shop--api--other", "Bash", "not this session", {})
    row = row_of(name)
    tid = db().task_add(project="shop", repo="api", slug="t", title="Fix login", prompt="p", tmux_name=name, session_row=row["id"])
    board.tmux["sessions"][name]["attached"] = 2
    r = board.client.get(f"/api/sessions/{name}", headers=H)
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d) == SESSION_KEYS
    assert (d["tmux"], d["project"], d["repo"], d["name"], d["agent"]) == (name, "shop", "api", "dev", "claude")
    assert d["state"] == "working" and d["last_prompt"] == "fix the login" and d["stats"]["model"] == "Opus 5"
    assert d["agent_session_id"] == s["agent_session_id"]
    assert d["flags"] == {"hook_seen": True, "subagents": 2}, "transcript_path never leaves the box"
    assert d["task"] == {"id": tid, "title": "Fix login", "phase": "running", "auto_close": False}
    assert [p["id"] for p in d["pending"]] == [pid] and d["pending"][0]["tmux_name"] == name
    assert d["viewers"] == {"full": 2, "grid": 0, "ro": 0} and d["win"] is None and isinstance(d["shell_version"], str) and len(d["shell_version"]) == 12
    json.dumps(d)


def test_api_session_detail_for_a_shell_and_an_unmanaged_session(board):
    sh = new_session(board, "shell")
    d = board.client.get(f"/api/sessions/{sh['tmux']}", headers=H).json()
    assert d["agent"] == "shell" and d["agent_session_id"] is None and d["task"] is None and d["pending"] == []
    # a tmux session the board has no row for: listed by tmux, agent unknown
    board.tmux["sessions"]["shop--api--manual"] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%9", "command": "zsh",
                                                   "path": "/tmp", "pid": 9, "env": {}}
    d = board.client.get("/api/sessions/shop--api--manual", headers=H).json()
    assert d["agent"] is None and d["state"] == "unknown" and d["flags"] == {} and d["task"] is None


def test_api_session_detail_404_400_503(board, monkeypatch):
    assert board.client.get("/api/sessions/shop--api--nope", headers=H).status_code == 404
    for bad in ("_ccboard-login", "shop--api", "a--b--c--d", "bad name"):         # internal and malformed names
        assert board.client.get(f"/api/sessions/{bad}", headers=H).status_code in (400, 404), bad
    assert board.client.get("/api/sessions/_ccboard-login", headers=H).status_code == 400
    assert board.client.get("/api/sessions/shop--api", headers=H).status_code == 400
    # an open row whose tmux session is gone is not "a session": 404
    new_session(board, "shell", name="gone")
    board.tmux["sessions"].clear()
    assert board.client.get("/api/sessions/shop--api--gone", headers=H).status_code == 404
    from app import tmux

    def down():
        raise tmux.TmuxDown("no server running")
    monkeypatch.setattr(tmux, "list_sessions", down)
    assert board.client.get("/api/sessions/shop--api--s1", headers=H).status_code == 503


# ---------------------------------------------------------------- GET /api/doctor

@pytest.fixture
def fake_checks(monkeypatch):
    seen = {"calls": 0, "db": []}

    def ok(d):
        seen["calls"] += 1
        seen["db"].append(d)
        return doctor.Outcome("pass", "fine")

    def warn(d):
        return doctor.Outcome("warn", "meh", doctor.fix("do the thing", cmd="echo hi"))

    monkeypatch.setattr(doctor, "CHECKS", [("a-ok", "box", "A", ok), ("b-warn", "claude", "B", warn)])
    monkeypatch.setattr(doctor, "PROVIDERS", [])
    monkeypatch.setattr(doctor, "GROUPS", ["box", "claude"])
    doctor.invalidate()
    yield seen
    doctor.invalidate()


def test_api_doctor_envelope_group_refresh_and_errors(board, fake_checks):
    r = board.client.get("/api/doctor", headers=H)
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"generated_at", "ok", "summary", "checks"}
    assert d["ok"] is True and d["summary"] == {"pass": 1, "warn": 1, "fail": 0, "skip": 0}
    assert [c["id"] for c in d["checks"]] == ["a-ok", "b-warn"]
    assert set(d["checks"][1]) == {"id", "group", "label", "status", "detail", "fix"}
    assert d["checks"][1]["fix"] == {"text": "do the thing", "cmd": "echo hi"} and d["checks"][0]["fix"] is None
    assert fake_checks["db"] == [db()], "the board's db is passed through (the cache key depends on it)"
    # the group filter
    g = board.client.get("/api/doctor?group=claude", headers=H).json()
    assert [c["id"] for c in g["checks"]] == ["b-warn"]
    assert board.client.get("/api/doctor?group=", headers=H).json()["summary"]["pass"] == 1       # empty = all groups
    # cached for 20 s, bypassed by refresh=1 (and only by exactly 1)
    calls = fake_checks["calls"]
    board.client.get("/api/doctor", headers=H)
    assert fake_checks["calls"] == calls
    board.client.get("/api/doctor?refresh=0", headers=H)
    assert fake_checks["calls"] == calls
    board.client.get("/api/doctor?refresh=1", headers=H)
    assert fake_checks["calls"] == calls + 1
    # an unknown group is a 400 that names the valid ones
    bad = board.client.get("/api/doctor?group=codex", headers=H)
    assert bad.status_code == 400 and "box" in bad.json()["error"]


def test_api_doctor_default_checks_run_without_secrets(board, monkeypatch, tmp_path):
    """The real check set against a sandbox: it answers, every status is a known one, and no secret reaches the payload."""
    secret = "sk-ant-oat01-" + "A" * 40
    (tmp_path / "claude").mkdir(exist_ok=True)
    (tmp_path / "claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": secret}}))
    monkeypatch.setattr(doctor, "_run", lambda argv, timeout=4.0: (_ for _ in ()).throw(doctor.ToolMissing(argv[0])))
    monkeypatch.setattr(doctor, "_port_open", lambda *a, **k: False)
    monkeypatch.setattr(doctor, "_http_get", lambda *a, **k: None)
    doctor.invalidate()
    d = board.client.get("/api/doctor?refresh=1", headers=H).json()
    doctor.invalidate()
    assert d["checks"] and {c["status"] for c in d["checks"]} <= {"pass", "warn", "fail", "skip"}
    assert {c["group"] for c in d["checks"]} >= {"terminal", "box", "claude", "notify"}
    blob = json.dumps(d)
    assert secret not in blob and hooks.ensure_token() not in blob


# ---------------------------------------------------------------- GET /api/external

@pytest.fixture
def registry_dir():
    shutil.copytree(FIX, settings.claude_config_dir)
    registry.invalidate()
    return settings.claude_config_dir


def test_api_external_shape(board, registry_dir):
    r = board.client.get("/api/external?agent=claude", headers=H)
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"claude", "codex", "at"} and d["codex"] == [] and d["at"] == "2026-10-03T12:00:00+00:00"
    assert len(d["claude"]) == 6, "4 sessions plus the 2 background jobs no session points at"
    assert d["claude"] == sorted(d["claude"], key=lambda e: e["updated_at"], reverse=True)
    e = next(x for x in d["claude"] if x["session_id"] == S10001)
    assert {"source", "session_id", "pid", "cwd", "kind", "entrypoint", "name", "name_source", "status", "status_at", "updated_at",
            "bridge", "job", "tmux", "waiting_for"} <= set(e)
    assert e["status"] == "busy" and e["bridge"] is True and e["tmux"] is None and e["job"]["state"] == "working"
    assert board.client.get("/api/external", headers=H).json() == d, "no agent filter = the same lists"


def test_api_external_filters_and_errors(board, registry_dir, monkeypatch):
    assert board.client.get("/api/external?agent=codex", headers=H).json()["claude"] == []
    assert board.client.get("/api/external?agent=gemini", headers=H).status_code == 400
    assert board.client.get("/api/external?project=a--b", headers=H).status_code == 400
    # project filter: the fixture's sessions all sit in /srv/projects/demo/repo
    monkeypatch.setattr(settings, "projects_dir", Path("/srv/projects"))
    assert len(board.client.get("/api/external?project=demo", headers=H).json()["claude"]) == 6
    assert board.client.get("/api/external?project=other", headers=H).json()["claude"] == []
    assert board.client.get("/api/external?project=dem", headers=H).json()["claude"] == [], "a prefix of the name is not the project"


def test_api_external_leaves_our_own_sessions_out(board, registry_dir):
    name = "shop--api--s1"
    db().add_session(tmux_name=name, project="shop", repo="api", name="s1", launcher="claude", claude_session_id=S10001)
    board.tmux["sessions"][name] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "claude",
                                    "path": str(board.projects / "shop" / "api"), "pid": 1, "env": {}}
    ext = board.client.get("/api/external", headers=H).json()["claude"]
    assert len(ext) == 5 and S10001 not in {e["session_id"] for e in ext}


def test_api_external_survives_a_broken_registry(board, monkeypatch):
    monkeypatch.setattr(registry, "snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk on fire")))
    assert board.client.get("/api/external", headers=H).json() == {"claude": [], "codex": [], "at": None}
    assert board.client.get("/api/state", headers=H).status_code == 200


# ---------------------------------------------------------------- GET /api/state

LEGACY_TOP = {"tmux_down", "projects", "user", "config", "claude", "login", "pending_permissions", "tasks", "jobs", "runs",
              "clone_queue", "last_recovery", "usage", "block", "cost", "health", "nodes", "backup", "node_name", "rate_limited",
              "scheduler", "version"}


def test_state_legacy_keys_are_unchanged(board):
    db().kv_set("rate_limits", {"five_hour": {"used_percentage": 42, "resets_at": 1791349200}})
    st = board.client.get("/api/state", headers=H).json()
    assert LEGACY_TOP <= set(st) and set(st) - LEGACY_TOP == {"agents", "setup", "deploy", "memory", "accounts", "codex_accounts", "usage_codex", "usage_refresh"}
    assert st["usage_refresh"] == {"running": False, "last": None}, "v0.5.17f: the Usage page's Refresh: a /usage ask in flight, and the newest one"
    assert st["usage_codex"] is None, "v0.5.12: the Codex account's windows (kv rate_limits_codex), null until a rollout reported them"
    assert st["claude"] == AUTH, "state.claude keeps its shape (the claude_auth.status() dict, as is)"
    assert st["login"] == {**claude_auth.login_state(), **account_store.login_view()}, "v0.5.17c: the login view adds adding/email/started_at/result"
    assert set(st["login"]) == {"running", "url", "tail", "adding", "email", "started_at", "result"}
    assert st["usage"] == db().kv_get("rate_limits") and st["usage"]["value"]["five_hour"]["used_percentage"] == 42
    assert st["accounts"] == {"current": None, "list": [], "store": {"supported": False, "reason": account_store.REASON, "count": 0}, "problem": None}, \
        "v0.5.17b: the account list is always present, empty until an identity is read (v0.5.17c: plus the saved-login store summary; v0.5.17g: plus the login problem)"
    assert st["codex_accounts"] == {"current": None, "list": [], "store": {"supported": False, "add": False, "reason": "codex is not installed", "count": 0},
                                    "login": {"running": False, "adding": False, "label": None, "replace_key": None, "started_at": None, "url": None, "code": None,
                                              "result": None}, "notice": None}, \
        "v0.5.17e: the one new top-level key, the saved Codex accounts (the GET body without login.tail); unsupported without a codex binary"


# Keys the real state carries only conditionally; a demo fixture need not list them. One reason each. Paths are "top.key".
DEMO_SHAPE_ALLOW = {
    "config.backup.repo": "only present once a backup repo is configured",
}
DEMO_ONLY_TOP = {"demo", "external"}        # the rebase epoch and the external threads route's data; the real state has neither


def _json_kind(v):
    return "bool" if isinstance(v, bool) else "number" if isinstance(v, (int, float)) else "string" if isinstance(v, str) else None


def demo_state_gaps(real, demo):
    """Every way the demo fixture drifts from the real /api/state: missing keys (top level and one level down) and scalar type clashes."""
    gaps = []
    if set(real) - set(demo):
        gaps.append(f"demo/state.json lacks the top-level keys {sorted(set(real) - set(demo))}")
    if set(demo) - set(real) - DEMO_ONLY_TOP:
        gaps.append(f"demo/state.json has top-level keys the real state does not: {sorted(set(demo) - set(real) - DEMO_ONLY_TOP)}")
    for k in sorted(set(real) & set(demo)):
        r, d = real[k], demo[k]
        if isinstance(r, dict) and r and isinstance(d, dict) and d:
            lack = sorted(x for x in set(r) - set(d) if f"{k}.{x}" not in DEMO_SHAPE_ALLOW)
            if lack:
                gaps.append(f"demo state.{k} lacks {lack}")
            pairs = [(f"{k}.{x}", r[x], d[x]) for x in sorted(set(r) & set(d))]
        else:
            pairs = [(k, r, d)]
        for path, rv, dv in pairs:
            if _json_kind(rv) and dv is not None and _json_kind(rv) != _json_kind(dv):
                gaps.append(f"demo state.{path}: the real value is a {_json_kind(rv)}, the demo's is not")
    return gaps


def test_the_demo_state_has_every_key_of_the_real_state(board):
    """Issue #54. Demo mode (?demo=1) renders from app/static/demo/state.json; a key added to build_state() must be added there too."""
    real = board.client.get("/api/state", headers=H).json()
    demo = json.loads((Path(__file__).parent.parent / "app" / "static" / "demo" / "state.json").read_text())
    assert demo_state_gaps(real, demo) == []
    # negative checks, so the comparison cannot pass vacuously: a deleted top key, a deleted nested key, a changed scalar type
    assert any("memory" in g for g in demo_state_gaps(real, {k: v for k, v in demo.items() if k != "memory"}))
    inner = next(x for x in real["config"] if x in demo["config"])
    assert any(inner in g for g in demo_state_gaps(real, {**demo, "config": {k: v for k, v in demo["config"].items() if k != inner}}))
    assert any("node_name" in g for g in demo_state_gaps({**real, "node_name": "x"}, {**demo, "node_name": 3}))


def test_state_agents_and_setup(board):
    st = board.client.get("/api/state", headers=H).json()
    assert list(st["agents"]) == ["claude", "codex"]
    a = st["agents"]["claude"]
    assert a == {"installed": True, "version": AUTH["version"], "loggedIn": True, "authMethod": "claude.ai",
                 "email": "me@example.com", "glyph": "◆", "hooks": {"installed": False}}
    # codex lives under the same key (v0.5.11); the suite has no codex binary (conftest _isolate_codex), so it reads as not installed
    assert st["agents"]["codex"] == {"installed": False, "version": None, "loggedIn": False, "glyph": "◇",
                                     "hooks": {"installed": False, "trust": "review"}}
    # first_run: no project folder and no session ever. A repo exists in this fixture, so it is not a first run
    assert st["setup"] == {"first_run": False, "ok": True}


def test_state_setup_first_run_and_ok(lite_client, projects_dir, fake_tmux, monkeypatch):
    monkeypatch.setattr(claude_auth, "status", lambda: {"installed": False, "loggedIn": False})
    st = lite_client.get("/api/state", headers=H).json()
    assert st["setup"] == {"first_run": True, "ok": False}          # empty projects dir, no sessions, no claude
    monkeypatch.setattr(claude_auth, "status", lambda: dict(AUTH))
    assert lite_client.get("/api/state", headers=H).json()["setup"] == {"first_run": True, "ok": True}
    # a session row, ended or not, means the board has been used; so does a project folder
    from app import main
    main.db.add_session(tmux_name="a--b--c", project="a", repo="b", name="c", launcher="shell")
    main.db.end("a--b--c")
    main._invalidate_scan()
    assert lite_client.get("/api/state", headers=H).json()["setup"]["first_run"] is False
    # tmux down: not ok
    from app import tmux

    def down():
        raise tmux.TmuxDown("no server running")
    monkeypatch.setattr(tmux, "list_sessions", down)
    main._invalidate_scan()
    st = lite_client.get("/api/state", headers=H).json()
    assert st["tmux_down"] is True and st["setup"]["ok"] is False


def test_state_session_additions_and_payload_hygiene(board):
    cl = new_session(board, "claude", name="dev", model="opus", effort="high", add_dirs=[])
    sh = new_session(board, "shell", name="plain")
    board.tmux["sessions"]["shop--api--manual"] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%9", "command": "zsh",
                                                   "path": "/tmp", "pid": 9, "env": {}}
    db().update_flags(cl["tmux"], {"transcript_path": "/home/x/.claude/projects/p/s.jsonl", "model_hint": "opus"}, {"subagents": 1})
    from app import main
    main._invalidate_scan()
    st = board.client.get("/api/state", headers=H).json()
    by = {s["tmux"]: s for s in all_sessions(st)}
    c, s, m = by[cl["tmux"]], by[sh["tmux"]], by["shop--api--manual"]
    assert c["agent"] == "claude" and c["agent_session_id"] == cl["agent_session_id"] == c["claude_session_id"]
    assert isinstance(c["row_id"], int) and c["row_id"] == row_of(cl["tmux"])["id"]
    assert c["flags"] == {"model_hint": "opus", "subagents": 1} and c["task"] is None
    assert (s["agent"], s["agent_session_id"]) == ("shell", None) and s["flags"] == {}
    assert (m["agent"], m["row_id"], m["agent_session_id"], m["flags"], m["task"]) == (None, None, None, {}, None)
    blob = json.dumps(st)
    assert "transcript_path" not in blob and "/.claude/projects/" not in blob
    assert not {"opts", "cwd"} & set(c), "launch options and cwd stay out of the poll payload"
    assert len(blob) < 150_000


def test_state_task_chip_from_one_query(board, monkeypatch):
    a = new_session(board, "claude", name="one")
    b = new_session(board, "claude", name="two")
    rid_a, rid_b = row_of(a["tmux"])["id"], row_of(b["tmux"])["id"]
    t1 = db().task_add(project="shop", repo="api", slug="t1", title="First", prompt="p", tmux_name=a["tmux"], session_row=rid_a,
                       phase="done")
    t2 = db().task_add(project="shop", repo="api", slug="t2", title="Second", prompt="p", tmux_name=a["tmux"], session_row=rid_a,
                       phase="running", auto_close=1)
    calls = []
    real = db().active_tasks_by_session
    monkeypatch.setattr(type(db()), "active_tasks_by_session", lambda self, *x, **k: calls.append(1) or real(*x, **k))
    from app import main
    main._invalidate_scan()
    st = board.client.get("/api/state", headers=H).json()
    by = {s["tmux"]: s for s in all_sessions(st)}
    assert by[a["tmux"]]["task"] == {"id": t2, "title": "Second", "phase": "running", "auto_close": True}, "running wins over done"
    assert by[b["tmux"]]["task"] is None
    assert len(calls) == 1, "one query per state build, not one per session"
    assert t1 != t2 and rid_b != rid_a


def test_state_tasks_resolve_their_session_by_row_and_bind_legacy_tasks(board):
    name = "shop--api--work"
    # an earlier session of that name that ended: tmux names are reused, session rows are not
    old = db().add_session(tmux_name=name, project="shop", repo="api", name="work", launcher="claude")
    db().end(name)
    s = new_session(board, "claude", name="work")
    assert s["tmux"] == name
    rid = row_of(name)["id"]
    assert rid != old
    db().conn.execute("UPDATE sessions SET launcher='task' WHERE id=?", (rid,))   # a legacy task's session was always launched as a task
    hooks.apply(db(), name, "UserPromptSubmit", {"prompt": "go"})
    # a plain session that happens to carry a task-like name: shown by name, never remembered as the task's row
    plain = new_session(board, "claude", name="plain")["tmux"]
    loose = db().task_add(project="shop", repo="api", slug="loose", title="Loose", prompt="p", tmux_name=plain)
    # a task bound by session_row to this very row
    bound = db().task_add(project="shop", repo="api", slug="bound", title="Bound", prompt="p", tmux_name="shop--api--elsewhere",
                          session_row=rid)
    # a legacy task (session_row NULL) that names the live session: bound lazily, once
    legacy = db().task_add(project="shop", repo="api", slug="legacy", title="Legacy", prompt="p", tmux_name=name)
    # a task whose session_row is the row that ended: the live session reuses its tmux name, but the row is authoritative
    stale = db().task_add(project="shop", repo="api", slug="stale", title="Stale", prompt="p", tmux_name=name, session_row=old)
    st = board.client.get("/api/state", headers=H).json()
    by = {t["id"]: t for t in st["tasks"]}
    assert by[bound]["session"]["state"] == "working" and by[bound]["session_row"] == rid
    assert by[legacy]["session"]["state"] == "working"
    assert by[stale]["session"] is None, "session_row wins over a reused tmux name"
    assert db().task_get(legacy)["session_row"] == row_of(name)["id"]
    assert db().task_get(stale)["session_row"] == old
    assert by[loose]["session"] is not None and db().task_get(loose)["session_row"] is None, "a non-task session is never bound"
    for t in st["tasks"]:
        assert {"agent", "mode", "phase", "auto_close", "parent_id", "chain_id", "session_row", "result"} <= set(t)
    assert by[bound]["agent"] == "claude" and by[bound]["mode"] == "worktree" and by[bound]["phase"] == "running"
    assert by[bound]["auto_close"] is False and by[bound]["result"] is None


def test_state_enriches_our_sessions_from_claude_registry(board, registry_dir):
    name = "shop--api--s1"
    db().add_session(tmux_name=name, project="shop", repo="api", name="s1", launcher="claude", claude_session_id=S10001)
    board.tmux["sessions"][name] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "claude",
                                    "path": str(board.projects / "shop" / "api"), "pid": 1, "env": {}}
    st, s = state_session(board, name)
    reg = s["flags"]["registry"]
    assert set(reg) == {"status", "name", "name_source", "pid", "bridge", "kind", "job", "at", "waiting_for"}
    assert reg["status"] == "busy" and reg["bridge"] is True and reg["pid"] == 10001
    assert set(reg["job"]) == {"state", "tempo", "needs", "suggested_reply"} and reg["job"]["state"] == "working"
    assert s["state"] == "unknown", "display only: the registry never touches the row's state"
    assert db().open_rows()[name]["flags"] == {}, "and never writes flags"


def test_state_survives_a_broken_registry(board, monkeypatch):
    new_session(board, "claude")
    monkeypatch.setattr(registry, "snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(registry, "enrich", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = board.client.get("/api/state", headers=H)
    assert r.status_code == 200 and any(s["agent"] == "claude" for s in all_sessions(r.json()))
    monkeypatch.setattr(registry, "snapshot", lambda *a, **k: {"ours": {}, "external": [], "scanned_at": "x"})
    from app import main
    main._invalidate_scan()
    assert board.client.get("/api/state", headers=H).status_code == 200      # enrich still raising


# ---------------------------------------------------------------- the launch path

def test_normalize_launcher_matrix():
    from app.main import _normalize_launcher as n
    assert n("claude") == ("claude", "claude")
    assert n("resume") == ("claude", "resume")
    assert n("continue") == ("claude", "continue")
    assert n("shell") == ("shell", "shell")
    assert n("codex") == ("codex", "claude") and n("codex-resume") == ("codex", "resume") and n("codex-continue") == ("codex", "continue")
    assert n("claude", "codex") == ("codex", "claude") and n("resume", "codex") == ("codex", "resume") and n("continue", "codex") == ("codex", "continue")
    assert n("claude", "claude") == ("claude", "claude") and n("shell", "shell") == ("shell", "shell") and n("claude", None) == ("claude", "claude")
    for launcher, agent in (("codex", "claude"), ("shell", "codex"), ("claude", "shell"), ("claude", "gemini")):
        with pytest.raises(projects.BadRequest):
            n(launcher, agent)
    for bad in ("", "clone", "task", "recovered", "bash", "Claude", None, 3):
        with pytest.raises(projects.BadRequest, match="launcher must be one of claude, resume, continue, shell, codex, codex-resume, codex-continue"):
            n(bad)


def test_launchers_is_a_superset_of_the_old_tuple():
    from app import main
    assert {"claude", "resume", "continue", "shell"} <= set(main.LAUNCHERS) and "codex" in main.LAUNCHERS


def test_api_create_session_refuses_codex_without_a_binary_and_unknown(board):
    r = board.client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "codex"})
    assert r.status_code == 400 and r.json()["error"] == "codex is not installed on this box"
    r = board.client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "emacs"})
    assert r.status_code == 400 and r.json()["error"] == "launcher must be one of claude, resume, continue, shell, codex, codex-resume, codex-continue"
    assert board.tmux["created"] == [], "nothing was started"


def test_create_claude_session_stores_agent_cwd_opts_and_exports_the_agent(board):
    r = new_session(board, "claude", name="dev", model="fable", effort="high", permission_mode="acceptEdits",
                    allowed_tools="Bash(npm test), Read", bypass=True, args="--verbose")
    assert r["agent"] == "claude" and r["agent_session_id"] == r["claude_session_id"] and r["tmux"] == "shop--api--dev"
    argv = shlex.split(r["cmd"])
    assert argv[:5] == ["claude", "--session-id", r["claude_session_id"], "--name", "dev"]
    assert "--dangerously-skip-permissions" in argv and "--verbose" in argv
    name, cwd, env = board.tmux["created"][-1]
    assert env["CCBOARD_AGENT"] == "claude" and env["CCBOARD_SESSION"] == name
    row = row_of(name)
    assert row["agent"] == "claude" and row["cwd"] == str(board.projects / "shop" / "api") == cwd
    opts = json.loads(row["opts"])
    assert opts == {"model": "fable", "effort": "high", "permission_mode": "acceptEdits", "allowed_tools": ["Bash(npm test)", "Read"]}
    assert "bypass" not in row["opts"] and "--verbose" not in row["opts"] and "extra" not in opts, "never stored: bypass, extra args"
    assert row["claude_session_id"] == r["claude_session_id"]
    # bypassPermissions as the permission mode is run (an explicit choice) but never stored for resume
    r2 = new_session(board, "claude", name="byp", permission_mode="bypassPermissions", model="opus")
    assert "--permission-mode bypassPermissions" in r2["cmd"]
    assert json.loads(row_of(r2["tmux"])["opts"]) == {"model": "opus"}


def test_create_resume_continue_and_shell_sessions(board):
    res = new_session(board, "resume", name="r1", resume_id=UUID1)
    assert res["cmd"] == f"claude --resume {UUID1}" and res["agent_session_id"] == UUID1
    assert row_of(res["tmux"])["launcher"] == "resume" and row_of(res["tmux"])["claude_session_id"] == UUID1
    res2 = new_session(board, "resume", name="r2")
    assert res2["cmd"] == "claude --resume" and res2["agent_session_id"] is None
    con = new_session(board, "continue", name="c1")
    assert con["cmd"] == "claude --continue" and con["agent_session_id"] is None and con["agent"] == "claude"
    sh = new_session(board, "shell", name="sh1")
    assert sh["agent"] == "shell" and sh["cmd"] is None and sh["agent_session_id"] is None and sh["claude_session_id"] is None
    assert row_of(sh["tmux"])["agent"] == "shell" and board.tmux["created"][-1][2]["CCBOARD_AGENT"] == "shell"
    assert board.client.post("/api/projects/shop/repos/api/sessions", headers=H,
                             json={"launcher": "resume", "resume_id": "not-a-uuid"}).status_code == 400


def test_create_session_error_order_is_unchanged(board, monkeypatch):
    post = lambda **b: board.client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "claude", **b})
    r = post(args="--settings x.json")
    assert r.status_code == 400 and "settings overrides are not allowed" in r.json()["error"]
    r = post(devcontainer=True)
    assert r.status_code == 400 and "no .devcontainer" in r.json()["error"]
    monkeypatch.setattr(settings, "claude_bin", lambda: None)
    r = post()
    assert r.status_code == 400 and r.json()["error"] == "claude is not installed on this box"
    assert board.client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).status_code == 201


def test_create_session_with_a_devcontainer_stores_it_in_opts(board):
    (board.projects / "shop" / "api" / ".devcontainer").mkdir()
    (board.projects / "shop" / "api" / ".devcontainer" / "devcontainer.json").write_text("{}")
    r = new_session(board, "claude", name="dc", devcontainer=True, add_dirs=["shop/web"])
    assert r["cmd"].startswith("devcontainer up --workspace-folder ") and " && devcontainer exec " in r["cmd"]
    assert "--add-dir" not in r["cmd"], "sibling repos are not mounted into the devcontainer"
    assert json.loads(row_of(r["tmux"])["opts"]) == {"devcontainer": True}
    sh = new_session(board, "shell", name="dcsh", devcontainer=True)
    assert sh["cmd"].endswith("-- bash -l") and sh["agent"] == "shell"


def test_launch_args_is_a_delegate_with_the_old_messages():
    from app import main
    assert main._launch_args(main.LaunchOpts(model="opus", effort="low")) == ["--model", "opus", "--effort", "low"]
    with pytest.raises(projects.BadRequest, match="effort must be one of low, medium, high, xhigh, max"):
        main._launch_args(main.LaunchOpts(effort="extreme"))
    with pytest.raises(projects.BadRequest, match="model: use an alias"):
        main._launch_args(main.LaunchOpts(model="opus; rm -rf /"))


def test_create_task_binds_its_session_row_and_stores_opts(board):
    r = board.client.post("/api/projects/shop/repos/api/tasks", headers=H,
                          json={"title": "Fix bug", "prompt": "fix it", "model": "sonnet", "effort": "low"})
    assert r.status_code == 201, r.text
    t = r.json()
    row = row_of(t["tmux"])
    task = db().task_get(t["id"])
    assert task["session_row"] == row["id"] and task["agent"] == "claude" and task["phase"] == "running" and task["mode"] == "worktree"
    assert row["launcher"] == "task" and row["agent"] == "claude" and row["cwd"] == str(board.projects / "shop" / "api")
    assert json.loads(row["opts"]) == {"model": "sonnet", "effort": "low"}
    assert board.tmux["created"][-1][2]["CCBOARD_AGENT"] == "claude"
    assert shlex.split(board.tmux["sent"][-1][1])[1:5] == ["--model", "sonnet", "--effort", "low"]
    st, s = state_session(board, t["tmux"])
    assert s["task"] == {"id": t["id"], "title": "Fix bug", "phase": "running", "auto_close": False}
    assert st["tasks"][0]["session"] is not None and st["tasks"][0]["column"] == "in_progress"


# ---------------------------------------------------------------- _end_session and ended_reason

def test_delete_records_ended_reason_and_cancels_the_autoclose_flag(board):
    s = new_session(board, "claude", name="dev")
    name = s["tmux"]
    db().update_flags(name, {"autoclose": {"task": 7, "due": "2099-01-01T00:00:00+00:00"}, "hook_seen": True})
    assert board.client.delete(f"/api/sessions/{name}", headers=H).json() == {"killed": name}
    row = row_of(name)
    assert row["ended_at"] and row["ended_reason"] == "killed" and name not in board.tmux["sessions"]
    assert json.loads(row["flags"]) == {"hook_seen": True}, "a pending auto-close dies with the session"
    assert board.client.get(f"/api/sessions/{name}", headers=H).status_code == 404


def test_delete_of_an_unknown_session_writes_nothing(board):
    s = new_session(board, "shell", name="ghost")
    board.tmux["sessions"].clear()                     # tmux lost it (a reboot) while the row stayed open
    assert board.client.delete(f"/api/sessions/{s['tmux']}", headers=H).status_code == 404
    assert row_of(s["tmux"])["ended_at"] is None, "reconcile closes it with its own reason, not the DELETE"
    with db().lock:                     # reconcile only closes rows older than its tmux snapshot (timestamps are whole seconds)
        db().conn.execute("UPDATE sessions SET created_at='2020-01-01T00:00:00+00:00' WHERE tmux_name=?", (s["tmux"],))
    from app import main
    main._invalidate_scan()
    board.client.get("/api/state", headers=H)
    assert row_of(s["tmux"])["ended_reason"] == "reconciled"
    assert board.client.delete("/api/sessions/not-a-name", headers=H).status_code == 400


def test_end_session_drops_the_task_preview_and_the_scan(board, monkeypatch):
    from app import main, previews
    s = new_session(board, "claude", name="p")
    tid = db().task_add(project="shop", repo="api", slug="p", title="P", prompt="p", tmux_name=s["tmux"], preview_port=3000,
                        preview_https=9100)
    offs = []
    monkeypatch.setattr(previews, "serve_off", lambda port: offs.append(port))
    main._invalidate_scan()
    assert main._end_session(s["tmux"], "auto_close") is True
    assert offs == [9100] and db().task_get(tid)["preview_https"] is None
    assert row_of(s["tmux"])["ended_reason"] == "auto_close"
    assert main._end_session(s["tmux"]) is False and main._end_session("") is False
    assert main._scan_cache is None


def test_archive_and_merge_end_the_session_with_a_reason(board, monkeypatch):
    t = board.client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": "Arch", "prompt": "p"}).json()
    assert board.client.post(f"/api/tasks/{t['id']}/archive", headers=H, json={}).status_code == 200
    assert row_of(t["tmux"])["ended_reason"] == "killed" and t["tmux"] not in board.tmux["sessions"]
    t2 = board.client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": "Merge me", "prompt": "p"}).json()
    db().task_update(t2["id"], pr_number=12, pr_url="https://example.invalid/pr/12", pr_state="OPEN")
    monkeypatch.setattr(gitops, "pr_merge", lambda cwd, number, method: f"merged {number} via {method}")
    out = board.client.post(f"/api/tasks/{t2['id']}/merge", headers=H, json={"method": "squash"})
    assert out.status_code == 200, out.text
    assert out.json()["merged"] == t2["id"] and row_of(t2["tmux"])["ended_reason"] == "killed"
    assert t2["tmux"] not in board.tmux["sessions"] and db().task_get(t2["id"])["pr_state"] == "MERGED"


def test_project_delete_records_project_deleted(board):
    s = new_session(board, "claude", name="gone")
    assert board.client.delete("/api/projects/shop", headers=H).status_code == 200
    assert row_of(s["tmux"])["ended_reason"] == "project_deleted"


# ---------------------------------------------------------------- the has_worktree guard

@pytest.fixture
def no_git(monkeypatch):
    """Any git call for an unassigned task would mean a path was built from t['worktree'] == ''."""
    def boom(*a, **k):
        raise AssertionError(f"git/gh ran for a task without a worktree: {a!r}")
    for fn in ("task_diff", "describe", "pr_create", "pr_merge", "push_and_verify"):
        monkeypatch.setattr(gitops, fn, boom)
    monkeypatch.setattr(prpoll, "failed_log", boom)


def test_unassigned_task_endpoints_answer_409_not_500(board, no_git):
    tid = unassigned_task()
    t = db().task_get(tid)
    assert (t["worktree"], t["tmux_name"], t["branch"], t["phase"]) == ("", "", "", "backlog")
    c = board.client
    assert c.get(f"/api/tasks/{tid}/diff", headers=H).status_code == 409
    assert c.post(f"/api/tasks/{tid}/describe", headers=H).status_code == 409
    assert c.post(f"/api/tasks/{tid}/pr", headers=H, json={"title": "x"}).status_code == 409
    assert c.post(f"/api/tasks/{tid}/merge", headers=H, json={}).status_code == 409
    assert c.post(f"/api/tasks/{tid}/fix-ci", headers=H).status_code == 409
    assert c.post(f"/api/tasks/{tid}/archive", headers=H, json={}).status_code == 409
    assert "no worktree" in c.get(f"/api/tasks/{tid}/diff", headers=H).json()["error"]
    # the ones that never needed git answer plainly
    assert c.post(f"/api/tasks/{tid}/refresh", headers=H).status_code == 400            # no PR yet
    assert c.get(f"/api/tasks/{tid}/ports", headers=H).json() == {"ports": [], "pane_pid": None}
    assert c.post(f"/api/tasks/{tid}/preview", headers=H, json={}).status_code in (400, 409)
    assert c.delete(f"/api/tasks/{tid}/preview", headers=H).status_code == 200
    # nothing was archived, killed or changed
    assert db().task_get(tid)["archived_at"] is None
    # an unknown task is still a 404
    for method, path in (("get", "/api/tasks/999/diff"), ("post", "/api/tasks/999/archive"), ("post", "/api/tasks/999/merge")):
        assert getattr(c, method)(path, headers=H).status_code == 404


def test_unassigned_task_shows_in_state_as_a_backlog_card(board):
    tid = unassigned_task()
    t = next(x for x in board.client.get("/api/state", headers=H).json()["tasks"] if x["id"] == tid)
    assert t["column"] == "backlog" and t["phase"] == "backlog" and t["session"] is None and t["session_row"] is None
    assert t["worktree"] == "" and t["tmux"] == "" and t["branch"] == ""


def test_run_resume_of_an_unassigned_task_is_a_404(board):
    tid = unassigned_task()
    jid = db().job_add(project="shop", repo="api", name="j", prompt="p")
    rid = db().run_start(jid)
    db().run_finish(rid, status="ok", task_id=tid)
    assert board.client.post(f"/api/runs/{rid}/resume", headers=H).status_code == 404


def test_overlap_and_prpoll_skip_tasks_without_a_worktree(board, monkeypatch):
    tid = unassigned_task()
    seen = []
    monkeypatch.setattr(overlap, "changed_files", lambda wt, base: seen.append(wt) or set())
    assert overlap.compute(db()) == 0 and seen == [], "Path('') is the board's own cwd: it must never be diffed"
    db().task_update(tid, pr_number=5, pr_state="OPEN")
    cwds = []
    monkeypatch.setattr(prpoll, "pr_status", lambda cwd, number: cwds.append(cwd) or {"state": "OPEN", "ci": {"bucket": "none"}})
    poller = prpoll.Poller(db(), projects.repo_path)
    assert poller.poll_once() == 1
    assert cwds == [board.projects / "shop" / "api"], "a task without a worktree reads its PR from the repo"


def test_a_task_with_a_worktree_still_works(board, monkeypatch):
    t = board.client.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": "Real", "prompt": "p"}).json()
    wt = board.projects / "shop" / "api" / ".claude" / "worktrees" / "real"
    wt.mkdir(parents=True)
    seen = []
    monkeypatch.setattr(gitops, "task_diff", lambda path, base: seen.append((path, base)) or {"files": [], "uncommitted": ""})
    d = board.client.get(f"/api/tasks/{t['id']}/diff", headers=H)
    assert d.status_code == 200 and seen == [(wt, "main")]


# ---------------------------------------------------------------- hooks

def test_hook_skips_post_tool_batch_and_records_the_agent(board):
    s = new_session(board, "claude", name="h")
    name = s["tmux"]
    hdr = {"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard-Session": name}
    sid = s["agent_session_id"]
    r = board.client.post("/api/hook", headers={**hdr, "X-CCBoard-Event": "UserPromptSubmit"},
                          content=json.dumps({"session_id": sid, "prompt": "go"}))
    assert r.json()["state"] == "working"
    before = db().open_rows()[name]
    out = board.client.post("/api/hook", headers={**hdr, "X-CCBoard-Event": "PostToolBatch"},
                            content=json.dumps({"hook_event_name": "PostToolBatch", "tools": ["Bash"]})).json()
    assert out["skipped"] is True and out["event"] == "PostToolBatch"
    after = db().open_rows()[name]
    assert (after["state"], after["last_event"], after["state_at"]) == (before["state"], before["last_event"], before["state_at"])
    with db().lock:
        rows = db().conn.execute("SELECT event, agent FROM events WHERE tmux_name=? ORDER BY id", (name,)).fetchall()
    assert [(r["event"], r["agent"]) for r in rows] == [("UserPromptSubmit", "claude")], "PostToolBatch is never stored"
    # statusline never was
    board.client.post("/api/hook", headers={**hdr, "X-CCBoard-Event": "statusline"}, content=json.dumps({"model": {"id": "x"}}))
    with db().lock:
        assert db().conn.execute("SELECT COUNT(*) FROM events WHERE event='statusline'").fetchone()[0] == 0


def test_hooks_apply_works_without_an_agent(board):
    s = new_session(board, "claude", name="legacy")
    out = hooks.apply(db(), s["tmux"], "SessionStart", {"source": "startup"})          # the old call shape
    assert out["state"] == "idle"
    with db().lock:
        assert db().conn.execute("SELECT agent FROM events WHERE tmux_name=?", (s["tmux"],)).fetchone()[0] is None


# ---------------------------------------------------------------- recovery through the adapter

def _open_row(name, **kw):
    kw.setdefault("launcher", "claude")
    db().add_session(tmux_name=name, project="shop", repo="api", name=name.split("--")[-1], **kw)
    return db().open_rows()[name]


def test_recover_plan_uses_the_adapter_and_repasses_stored_opts(board):
    a = _open_row("shop--api--a", claude_session_id=UUID1, add_dirs=["/x/web"],
                  opts={"model": "opus", "effort": "high", "permission_mode": "plan"})
    b = _open_row("shop--api--b", launcher="continue")
    todo = {t["name"]: t for t in recover.plan({"shop--api--a": a, "shop--api--b": b}, set())}
    assert todo["shop--api--a"]["cmd"] == ["claude", "--resume", UUID1, "--model", "opus", "--effort", "high", "--permission-mode",
                                           "plan", "--add-dir", "/x/web"]
    assert todo["shop--api--b"]["cmd"] == ["claude", "--continue"]
    # no stored opts: exactly the old argv (pinned elsewhere as a literal too)
    c = _open_row("shop--api--c", claude_session_id=UUID1, add_dirs=["/x/web"])
    assert recover.plan({"shop--api--c": c}, set())[0]["cmd"] == ["claude", "--resume", UUID1, "--add-dir", "/x/web"]


def test_recover_never_repasses_a_bypass_and_survives_bad_rows(board):
    # opts that somehow carry a bypass would be re-validated, not trusted blindly: bypassPermissions in opts is allowed by the
    # adapter (an explicit mode) but the board never stores it, and nothing stored makes recovery add the bypass flag
    ok = _open_row("shop--api--ok", claude_session_id=UUID1, opts={"model": "sonnet"})
    junk_id = _open_row("shop--api--junk", claude_session_id="not-a-uuid")
    bad_opts = _open_row("shop--api--badopts", claude_session_id=UUID1, opts={"effort": "extreme"})
    codex = _open_row("shop--api--cx", agent="gemini", claude_session_id=UUID1)      # an agent with no adapter (codex has one since v0.5.11)
    shell = _open_row("shop--api--sh", launcher="shell", agent="shell")
    rows = {r["tmux_name"]: r for r in (ok, junk_id, bad_opts, codex, shell)}
    todo = {t["name"]: t["cmd"] for t in recover.plan(rows, set())}
    assert todo["shop--api--ok"] == ["claude", "--resume", UUID1, "--model", "sonnet"]
    assert todo["shop--api--junk"] == ["claude", "--continue"], "an id that is not a UUID cannot be resumed: continue instead"
    assert todo["shop--api--badopts"] == ["claude", "--continue"], "unusable stored opts must not stop recovery"
    assert "shop--api--cx" not in todo and "shop--api--sh" not in todo
    assert all("--dangerously-skip-permissions" not in c for c in todo.values())


def test_recover_run_passes_agent_opts_and_rebinds_the_task(board, monkeypatch):
    t = board.client.post("/api/projects/shop/repos/api/tasks", headers=H,
                          json={"title": "Fix bug", "prompt": "fix", "model": "haiku"}).json()
    wt = board.projects / "shop" / "api" / ".claude" / "worktrees" / "fix-bug"
    wt.mkdir(parents=True)
    plain = new_session(board, "claude", name="plain", model="opus", effort="low")
    codex_row = _open_row("shop--api--cx", agent="gemini")            # no adapter: closed, not relaunched
    board.tmux["sessions"].clear()
    board.tmux["created"].clear()
    from app import main
    summary = recover.run(db(), main._start_session)
    assert sorted(summary["recovered"]) == sorted([t["tmux"], plain["tmux"]]) and summary["closed"] == ["shop--api--cx"]
    assert codex_row["tmux_name"] in summary["closed"]
    typed = dict(board.tmux["sent"])
    assert typed[plain["tmux"]] == f"claude --resume {plain['agent_session_id']} --model opus --effort low"
    assert typed[t["tmux"]].startswith(f"claude --resume {db().task_get(t['id'])['claude_session_id']} --model haiku")
    new_row = row_of(t["tmux"])
    assert new_row["launcher"] == "task" and new_row["agent"] == "claude" and new_row["cwd"] == str(wt)
    assert json.loads(new_row["opts"]) == {"model": "haiku"}
    assert db().task_get(t["id"])["session_row"] == new_row["id"], "the relaunched session is the task's live session"
    assert row_of(plain["tmux"])["agent"] == "claude" and row_of(plain["tmux"])["launcher"] == "recovered"
    ended = [r for r in (row_of("shop--api--cx"),) if r["ended_at"]]
    assert ended and ended[0]["ended_reason"] == "reconciled"
    assert all(env["CCBOARD_AGENT"] == "claude" for _n, _c, env in board.tmux["created"])
    st = board.client.get("/api/state", headers=H).json()
    assert next(x for x in st["tasks"] if x["id"] == t["id"])["session"] is not None
    assert recover.run(db(), main._start_session) == {"recovered": [], "closed": [], "skipped": [], "continue": []}
