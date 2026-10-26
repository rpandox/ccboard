"""Runtime profiles use temp homes, fake agents and the in-memory tmux seam only."""
import builtins
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

import pytest

from app import profiles, projects, scheduler
from app.config import Settings, settings
from app.db import DB

SID = "11111111-1111-4111-8111-111111111111"
HEADERS = {"Tailscale-User-Login": "owner", "X-CCBoard": "1"}


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "pools", True)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data with space")
    monkeypatch.setattr(settings, "claude_config_dir", tmp_path / "global-claude")
    monkeypatch.setattr(settings, "codex_home", tmp_path / "global-codex")
    monkeypatch.setattr(profiles.sys, "platform", "linux")
    (settings.claude_config_dir / "projects").mkdir(parents=True)
    (settings.codex_home / "sessions").mkdir(parents=True)
    db = DB(settings.data_dir / "test.db")
    yield db
    db.close()


def account(db, provider="claude", aid="account-a"):
    home = settings.claude_config_dir if provider == "claude" else settings.codex_home
    (home / ("projects" if provider == "claude" else "sessions")).mkdir(parents=True, exist_ok=True)
    path = profiles.ensure_profile(provider, aid)
    db.conn.execute("INSERT INTO pool_account(id,provider,label,state,cred_version,created_at) VALUES (?,?,?,?,?,?)", (aid, provider, aid, "ready", None, "2026-01-01"))
    profiles._write(path / (".credentials.json" if provider == "claude" else "auth.json"), b"opaque fixture bytes")
    return path


def rows(db):
    return [dict(r) for r in db.conn.execute("SELECT * FROM runtime_attempt ORDER BY id")]


@pytest.mark.parametrize("value,on", [(None, False), ("0", False), ("false", False), ("1", True), ("true", True), ("on", True)])
def test_switch(value, on):
    assert Settings({} if value is None else {"CCBOARD_POOLS": value}).pools is on


@pytest.mark.parametrize("bad", ["..", "a/b", "A", "a..b", "-a", "a" * 49, "a\n", ""])
def test_id_allowlist(box, bad):
    with pytest.raises(projects.BadRequest, match="account id"):
        profiles.ensure_profile("claude", bad)


@pytest.mark.parametrize("part", ["", "claude", "claude/account-a", "_shared", "_shared/claude"])
def test_containment(box, tmp_path, part):
    outside = tmp_path / "outside"
    outside.mkdir()
    path = settings.data_dir / "profiles" / part
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside, target_is_directory=True)
    with pytest.raises(projects.BadRequest, match="escapes"):
        profiles.ensure_profile("claude", "account-a")
    assert list(outside.iterdir()) == []


def test_profile_templates_shared_transcripts_and_modes(box):
    from app.agents.claude import _claude_settings_mod
    a, b = account(box), account(box, aid="account-b")
    c = account(box, "codex", "codex-a")
    assert str(a) == str(a.resolve()) and not str(a).endswith("/")
    for path in [a, a.parent, a.parent.parent, a.parent.parent / "_shared", a.parent.parent / "_shared/claude"]:
        assert path.stat().st_mode & 0o777 == 0o700
    expected = _claude_settings_mod().install({}, Path(__file__).resolve().parents[1], approve_timeout=settings.approve_timeout)
    assert json.loads((a / "settings.json").read_text())["hooks"] == expected["hooks"]
    (a / "projects" / "conversation.jsonl").write_text("conversation\n")
    assert (b / "projects" / "conversation.jsonl").read_text() == "conversation\n"
    assert (a / "projects").readlink() == settings.data_dir.resolve() / "profiles/_shared/claude/projects"
    assert (a.parent.parent / "_shared/claude/projects").readlink() == settings.claude_config_dir.resolve() / "projects"
    assert (c / "config.toml").read_text() == 'cli_auth_credentials_store = "file"\n'
    assert profiles.launch_env("codex", "codex-a")[1] == {"CODEX_HOME": str(c), "CODEX_SQLITE_HOME": str(settings.codex_home.resolve())}


def test_environment_and_two_fake_processes(box, tmp_path, monkeypatch):
    a, b = account(box), account(box, aid="account-b")
    home = tmp_path / "home"
    global_creds = home / ".claude/.credentials.json"
    global_creds.parent.mkdir(parents=True)
    global_creds.write_bytes(b"global fixture bytes")
    mtime = global_creds.stat().st_mtime_ns
    monkeypatch.setenv("HOME", str(home))
    for key in [*profiles.UNSET, *(p + "X" for p in profiles.PREFIXES)]:
        monkeypatch.setenv(key, "parent-value")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(global_creds.parent))
    # This fake fails on any attempt to open the global credentials. It reads its profile only.
    fake = tmp_path / "fake claude"
    fake.write_text('''import builtins, json, os
from pathlib import Path
original = builtins.open
def guarded(path, *args, **kwargs):
    assert Path(path) != Path.home() / ".claude/.credentials.json"
    return original(path, *args, **kwargs)
builtins.open = guarded
profile = Path(os.environ["CLAUDE_CONFIG_DIR"])
with open(profile / ".credentials.json", "rb") as f:
    assert f.read()
assert not any(k.startswith("ANTHROPIC_FEDERATION_") for k in os.environ)
assert "ANTHROPIC_API_KEY" not in os.environ and "OPENAI_API_KEY" not in os.environ
print(json.dumps({"profile": str(profile)}))
''')
    processes = []
    for aid in ("account-a", "account-b"):
        cmd = profiles.wrap_command(shlex.join([sys.executable, str(fake)]), "claude", aid)
        assert "env -u ANTHROPIC_API_KEY" in cmd
        # Added after command construction, as a tmux server or shell rc could do.
        env = {**os.environ, "ANTHROPIC_FEDERATION_UNKNOWN": "parent-value"}
        processes.append(subprocess.Popen(["/bin/sh", "-c", cmd], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    results = [p.communicate(timeout=10) for p in processes]
    assert [p.returncode for p in processes] == [0, 0], results
    assert [json.loads(out)["profile"] for out, _ in results] == [str(a), str(b)]
    assert global_creds.stat().st_mtime_ns == mtime
    child = profiles.child_env("claude", "account-a", {"ANTHROPIC_API_KEY": "parent-value", "OPENAI_API_KEY": "parent-value", "KEEP": "yes", "ANTHROPIC_FEDERATION_X": "parent-value"})
    assert child == {"KEEP": "yes", "CLAUDE_CONFIG_DIR": str(a)}


def test_mac_refusal_and_setup_token(box, monkeypatch):
    monkeypatch.setattr(profiles.sys, "platform", "darwin")
    with pytest.raises(projects.BadRequest, match="Mac Keychain"):
        profiles.ensure_profile("claude", "account-a")
    assert not (settings.data_dir / "profiles").exists()
    path = profiles.ensure_profile("claude", "account-a", setup_token=True)
    profiles._write(path / profiles.SETUP_TOKEN, b"opaque fixture value")
    assert profiles.child_env("claude", "account-a", {})["CLAUDE_CODE_OAUTH_TOKEN"] == "opaque fixture value"
    assert "opaque fixture value" not in profiles.wrap_command("claude", "claude", "account-a")



def test_attempt_binding_exit_and_fingerprint(box):
    path = account(box)
    name = "project--root--one"
    row = box.add_session(tmux_name=name, project="project", repo="root", name="one", launcher="claude", cwd="/project")
    aid = profiles.start_attempt(box, "claude", "account-a", session_row=row, task_id=1)
    box.rebind_session_id(name, SID)
    assert rows(box)[0]["provider_session_id"] == SID
    profiles._write(path / ".credentials.json", b"refreshed opaque bytes")
    box.set_state(name, "ended", "SessionEnd")
    assert rows(box)[0]["ended_at"] is None
    box.end(name, "exited")
    attempt = rows(box)[0]
    assert attempt["id"] == aid and attempt["ended_at"] and attempt["end_reason"] == "exited"
    assert profiles.accounts_view(box)[0]["cred_version"] == hashlib.sha256(b"refreshed opaque bytes").hexdigest()[:16]
    profiles.finish_attempt(box, aid, "again")
    assert rows(box)[0]["end_reason"] == "exited"


@pytest.mark.parametrize("ending", ["end", "reconcile"])
def test_attempt_ends_with_session(box, ending):
    account(box)
    name = "project--root--one"
    row = box.add_session(tmux_name=name, project="project", repo="root", name="one", launcher="claude")
    profiles.start_attempt(box, "claude", "account-a", session_row=row)
    if ending == "end":
        box.end(name, "killed")
    else:
        box.reconcile(set(), "9999")
    assert rows(box)[0]["ended_at"]


def test_routes_scrubbed_account_response_and_legacy_guard(lite_client, fake_tmux, monkeypatch):
    from app import main, account_store, codex_accounts
    monkeypatch.setattr(settings, "allowed_users", {"owner"})
    monkeypatch.setattr(settings, "pools", True)
    monkeypatch.setattr(profiles.sys, "platform", "linux")
    account(main.db)
    response = lite_client.get("/api/pools/accounts", headers=HEADERS)
    assert response.status_code == 200
    assert set(response.json()[0]) == {"id", "provider", "label", "state", "cred_version"}
    assert not [key for key in response.json()[0] if key != "cred_version" and re.search("cred|token|auth|secret|email", key, re.I)]
    with profiles.inflight_launch(main.db) as owner:
        aid = profiles.start_attempt(main.db, "claude", "account-a", reservation_owner=owner)
        from app import pool_usage
        pool_usage.collector(main.db).tick(active=False)
        usage = lite_client.get("/api/pools/usage", headers=HEADERS)
        assert usage.status_code == 200 and set(usage.json()) == {"groups"}
        group = usage.json()["groups"][0]
        assert set(group) == {"id", "provider", "plan", "accounts", "window_5h_pct", "window_5h_reset_at", "weekly_pct", "weekly_reset_at", "sampled_at", "age_s", "confidence", "source", "state", "nodes", "account_labels", "reason"}
        assert not [key for key in group if re.search("cred|token|auth|secret|email|uuid", key, re.I)]
        for provider, store in [("accounts", account_store), ("codex-accounts", codex_accounts)]:
            monkeypatch.setattr(store, "_require", lambda: pytest.fail("guard must precede legacy work"))
            r = lite_client.post(f"/api/{provider}/any/switch", headers=HEADERS)
            assert r.status_code == 409 and r.json() == {"detail": profiles.SWITCH_REASON + f": attempt {aid}"}
    profiles.finish_attempt(main.db, aid, "exited")
    # Existing switch handlers still use their original implementation when no attempt is open.
    for provider, store in [("accounts", account_store), ("codex-accounts", codex_accounts)]:
        monkeypatch.setattr(store, "switch", lambda *a, **kw: {"ok": True, "already": True})
        assert lite_client.post(f"/api/{provider}/any/switch", headers=HEADERS).status_code == 200


def test_off_routes_bindings_and_no_side_effects(lite_client, fake_tmux, projects_dir, monkeypatch):
    from app import main
    monkeypatch.setattr(settings, "allowed_users", {"owner"})
    monkeypatch.setattr(settings, "pools", False)
    (projects_dir / "project").mkdir()
    for method, url in [("GET", "/api/pools/accounts"), ("GET", "/api/pools/usage"), ("POST", "/api/pools/migrate")]:
        r = lite_client.request(method, url, headers=HEADERS)
        assert r.status_code == 404 and r.json() == {"detail": "pools are off"}
    for url, body in [("/api/projects/project/repos/root/tasks", {"title": "Task", "prompt": "Do it"}),
                      ("/api/projects/project/repos/root/sessions", {"launcher": "claude"})]:
        r = lite_client.post(url, json={**body, "account_id": "account-a"}, headers=HEADERS)
        assert r.status_code == 400 and r.json().get("detail", r.json().get("error")) == "pools are off"
    assert profiles.migrate(main.db) == []
    with pytest.raises(projects.BadRequest, match="pools are off"):
        profiles.ensure_profile("claude", "account-a")
    assert rows(main.db) == []
    assert not (settings.data_dir / "profiles").exists()


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_unbound_task_command_and_tmux_env_match_before_profiles(lite_client, fake_tmux, projects_dir, monkeypatch, provider):
    from app import main, agents
    monkeypatch.setattr(settings, "pools", False)
    monkeypatch.setattr(settings, "allowed_users", {"owner"})
    monkeypatch.setattr(settings, "claude_bin", lambda: "fake-claude")
    monkeypatch.setattr(main.uuid, "uuid4", lambda: SID)
    monkeypatch.setattr(agents.get("codex"), "bin", lambda: "fake-codex")
    monkeypatch.setattr(agents.get("codex"), "_probe_caps", lambda: ({}, True))
    (projects_dir / "project").mkdir()
    r = lite_client.post("/api/projects/project/repos/root/tasks", headers=HEADERS,
                         json={"title": "Task", "prompt": "Do it", "agent": provider})
    assert r.status_code == 201, r.text
    name, line = fake_tmux["sent"][-1]
    expected = ["claude", "--session-id", SID, "--", "Do it"] if provider == "claude" else ["codex", "-s", "workspace-write", "-a", "on-request", "--", "Do it"]
    assert line == shlex.join(expected)
    assert shlex.split(line) == expected
    assert fake_tmux["created"][-1][2] == {"CCBOARD_SESSION": name, "CCBOARD_URL": settings.loopback_url(), "CCBOARD_APPROVE_TIMEOUT": str(int(settings.approve_timeout)), "CCBOARD_AGENT": provider}
    assert not (settings.data_dir / "profiles").exists() and rows(main.db) == []
    demo = json.loads((Path(__file__).resolve().parents[1] / 'app/static/demo/state.json').read_text())
    for section in ['accounts', 'codex_accounts']:
        assert demo[section]['list'] and all(r['pool_account'] is None for r in demo[section]['list'])


def test_unbound_scheduler_argv_env_match_before_profiles(box, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "pools", False)
    monkeypatch.setattr(settings, "subagent_model", "inherit")
    monkeypatch.setattr(settings, "claude_bin", lambda: "fake-claude")
    monkeypatch.setattr(scheduler.projects, "repo_path", lambda *a: tmp_path)
    monkeypatch.setattr(scheduler.tasks, "unique_slug", lambda *a: "job")
    monkeypatch.setattr(scheduler.tasks, "ensure_excluded", lambda *a: None)
    monkeypatch.setattr(scheduler, "fable_hold", lambda *a: None)
    monkeypatch.setattr(scheduler.devguard, "launch_ok", lambda: True)
    monkeypatch.setattr(scheduler, "_finish", lambda db, job, rid, summary, *a: summary)
    calls = []
    monkeypatch.setattr(scheduler, "_run_agent", lambda cmd, **kw: calls.append((cmd, kw)) or subprocess.CompletedProcess(cmd, 0, '{"result":"ok"}', ''))
    scheduler.run_job(box, {"id": 1, "name": "Job", "project": "project", "repo": "root", "prompt": "Do it", "permission_mode": "acceptEdits"}, 1)
    assert calls == [(["fake-claude", "-p", "Do it", "--worktree", "job", "--output-format", "json", "--permission-mode", "acceptEdits", "--max-turns", "30"],
                     {"cwd": str(tmp_path), "capture_output": True, "text": True, "timeout": scheduler.RUN_TIMEOUT, "stdin": subprocess.DEVNULL})]
    assert rows(box) == [] and not (settings.data_dir / "profiles").exists()


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_bound_task_process_and_session_lifecycle(lite_client, fake_tmux, projects_dir, monkeypatch, provider, tmp_path):
    from app import main, agents
    monkeypatch.setattr(settings, "allowed_users", {"owner"})
    monkeypatch.setattr(settings, "pools", True)
    monkeypatch.setattr(profiles.sys, "platform", "linux")
    monkeypatch.setattr(settings, "claude_bin", lambda: "fake-claude")
    monkeypatch.setattr(agents.get("codex"), "bin", lambda: "fake-codex")
    monkeypatch.setattr(agents.get("codex"), "_probe_caps", lambda: ({}, True))
    monkeypatch.setattr(profiles, "discover_attempt", lambda *a: None)
    account(main.db, provider)
    (projects_dir / "project").mkdir()
    r = lite_client.post("/api/projects/project/repos/root/tasks", headers=HEADERS,
                         json={"title": "Bound task", "prompt": "resume", "agent": provider, "account_id": "account-a"})
    assert r.status_code == 201, r.text
    task = r.json()
    assert "profile_exec.py" in fake_tmux["sent"][-1][1]
    assert main.db.open_row(task["tmux"])["account"] == "account-a"
    assert "env -u ANTHROPIC_API_KEY" in fake_tmux["sent"][-1][1]
    assert str(profiles.profile_dir(provider, "account-a")) in fake_tmux["sent"][-1][1]
    home = tmp_path / "fake-home"
    home.mkdir()
    dump = home / "env.json"
    fake = home / provider
    fake.write_text('#!' + sys.executable + '\nimport json, os\nfrom pathlib import Path\nPath(os.environ["DUMP_ENV"]).write_text(json.dumps(dict(os.environ)))\n')
    fake.chmod(0o700)
    rc = home / '.bash_profile'
    rc.write_text('export PATH=' + shlex.quote(str(home)) + ':"$PATH"\n' + '\n'.join(
        f'export {key}=fixture-value' for key in [*profiles.UNSET, *(prefix + 'X' for prefix in profiles.PREFIXES)]))
    built = fake_tmux['sent'][-1][1]
    result = subprocess.run(['bash', '-lc', '. ' + shlex.quote(str(rc)) + '\n' + built],
                            env={**os.environ, 'HOME': str(home), 'DUMP_ENV': str(dump)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    env = json.loads(dump.read_text())
    selected = {'CLAUDE_CONFIG_DIR'} if provider == 'claude' else {'CODEX_HOME', 'CODEX_SQLITE_HOME'}
    assert all(key not in env for key in profiles.UNSET if key not in selected)
    assert not any(key.startswith(profiles.PREFIXES) for key in env)
    assert env['CLAUDE_CONFIG_DIR' if provider == 'claude' else 'CODEX_HOME'] == str(profiles.profile_dir(provider, 'account-a'))
    attempt = rows(main.db)[0]
    assert attempt["account_id"] == "account-a" and attempt["task_id"] == str(task["id"])
    assert attempt["ended_at"] is None
    main.db.rebind_session_id(task["tmux"], SID)
    assert rows(main.db)[0]["provider_session_id"] == SID
    main.db.end(task["tmux"], "killed")
    assert rows(main.db)[0]["end_reason"] == "killed"


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_bound_scheduler_scrubs_and_closes_attempt(box, monkeypatch, tmp_path, provider):
    from app import agents
    account(box, provider)
    for key in [*profiles.UNSET, *(p + "X" for p in profiles.PREFIXES)]:
        monkeypatch.setenv(key, "parent-value")
    monkeypatch.setattr(settings, "claude_bin", lambda: "fake-claude")
    monkeypatch.setattr(agents.get("codex"), "bin", lambda: "fake-codex")
    monkeypatch.setattr(scheduler.projects, "repo_path", lambda *a: tmp_path)
    monkeypatch.setattr(scheduler.tasks, "unique_slug", lambda *a: "job")
    monkeypatch.setattr(scheduler.tasks, "ensure_excluded", lambda *a: None)
    monkeypatch.setattr(scheduler.tasks, "default_branch", lambda *a: "main")
    monkeypatch.setattr(scheduler.tasks, "create_managed_worktree", lambda *a: tmp_path)
    monkeypatch.setattr(scheduler.tasks, "apply_worktreeinclude", lambda *a: None)
    monkeypatch.setattr(scheduler, "fable_hold", lambda *a: None)
    monkeypatch.setattr(scheduler.devguard, "launch_ok", lambda: True)
    monkeypatch.setattr(scheduler, "_finish", lambda db, job, rid, summary, *a: summary)
    monkeypatch.setattr(scheduler, "codex_command", lambda *a: ["codex", "exec", "--json", "Do it"])
    monkeypatch.setattr(profiles, "discover_attempt", lambda *a: None)
    calls = []
    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        assert len(rows(box)) == 1 and rows(box)[0]["ended_at"] is None
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(scheduler, "_run_agent", run)
    result = scheduler.run_job(box, {"id": 1, "name": "Job", "project": "project", "repo": "root", "prompt": "Do it", "agent": provider,
                                   "permission_mode": "acceptEdits", "opts": {"account_id": "account-a"}}, 1)
    env = calls[0][1]["env"]
    selected = {"CLAUDE_CONFIG_DIR"} if provider == "claude" else {"CODEX_HOME", "CODEX_SQLITE_HOME"}
    assert all(k not in env for k in profiles.UNSET if k not in selected)
    assert not any(k.startswith(profiles.PREFIXES) for k in env)
    assert env["CLAUDE_CONFIG_DIR" if provider == "claude" else "CODEX_HOME"] == str(profiles.profile_dir(provider, "account-a"))
    assert result["error"] == "timed out" and rows(box)[0]["ended_at"]


@pytest.fixture
def no_birthtime(monkeypatch):
    """Exercise the portable stat fields even on macOS."""
    from types import SimpleNamespace
    original = Path.stat
    def stat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        return SimpleNamespace(**{key: getattr(result, key) for key in dir(result)
                                  if key.startswith("st_") and not key.startswith("st_birthtime")})
    monkeypatch.setattr(Path, "stat", stat)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_fallback_finds_only_fresh_matching_project(box, tmp_path, provider, no_birthtime):
    account(box, provider)
    root = profiles.transcript_directory(provider, tmp_path)
    root.mkdir(parents=True)
    def transcript(path, cwd, sid):
        data = {"cwd": str(cwd), "sessionId": sid} if provider == "claude" else {"type": "session_meta", "payload": {"cwd": str(cwd), "id": sid}}
        path.write_text(json.dumps(data) + "\n")
    old = root / "old.jsonl"
    transcript(old, tmp_path, "33333333-3333-4333-8333-333333333333")
    stale = root / "stale.jsonl"
    transcript(stale, tmp_path, "44444444-4444-4444-8444-444444444444")
    os.utime(stale, (time.time() - 10, time.time() - 10))
    snapshot = profiles.transcript_snapshot(provider, tmp_path)
    assert snapshot == {(p.name, p.stat().st_ino, p.stat().st_size) for p in (old, stale)}
    assert not hasattr(old.stat(), "st_birthtime")
    aid = profiles.start_attempt(box, provider, "account-a")
    started = time.time()
    # Recent edits to a preexisting transcript must not make discovery ambiguous.
    with old.open("a") as f:
        f.write('{}\n')
    transcript(root / "good.jsonl", tmp_path, SID)
    transcript(root / "wrong-project.jsonl", tmp_path / "elsewhere", "22222222-2222-4222-8222-222222222222")

    worker = profiles.discover_attempt(box, aid, str(tmp_path), provider, started, None, snapshot)
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert rows(box)[0]["provider_session_id"] == SID


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("launch", ["tmux", "headless"])
def test_launch_passes_snapshot_taken_before_agent_starts(box, tmp_path, monkeypatch, fake_tmux, provider, launch, no_birthtime):
    from app import main
    account(box, provider)
    root = profiles.transcript_directory(provider, tmp_path)
    root.mkdir(parents=True)
    old = root / "old.jsonl"
    old.write_text('{}\n')
    expected = {(old.name, old.stat().st_ino, old.stat().st_size)}
    discoveries = []
    monkeypatch.setattr(profiles, "discover_attempt", lambda *args: discoveries.append(args))
    def run(*args, **kwargs):
        meta = {"cwd": str(tmp_path), "sessionId": SID} if provider == "claude" else {"type": "session_meta", "payload": {"cwd": str(tmp_path), "id": SID}}
        (root / "new.jsonl").write_text(json.dumps(meta) + '\n')
        return subprocess.CompletedProcess([], 0, '', '')
    if launch == "tmux":
        monkeypatch.setattr(main, "db", box)
        monkeypatch.setattr(main, "require_real_launch_ok", lambda: None)
        monkeypatch.setattr(main.tmux, "send_line", run)
        main._start_session_row("p--r--s", "p", "r", "s", "claude", str(tmp_path),
                                cmd_line=provider, agent=provider, opts={"account_id": "account-a"})
    else:
        monkeypatch.setattr(scheduler, "_run_agent", run)
        scheduler._bound_run([provider], binding={"db": box, "account_id": "account-a", "run_id": 1}, provider=provider, cwd=str(tmp_path))
    assert len(discoveries) == 1
    _, aid, cwd, found_provider, started, _, snapshot = discoveries[0]
    assert aid == rows(box)[0]["id"] and cwd == str(tmp_path) and found_provider == provider
    assert snapshot == expected
    os.utime(root / "new.jsonl", (started - 0.01, started - 0.01))
    assert profiles.newest_transcript(provider, cwd, started, snapshot) == SID


def test_off_state_keys_and_unbound_on_launch(lite_client, fake_tmux, projects_dir, monkeypatch):
    from app import main, accounts, codex_accounts
    for kv, keys in [(accounts.KV_ACCOUNTS, ['first', 'second']), (codex_accounts.KV_ACCOUNTS, ['a' * 24, 'b' * 24])]:
        main.db.kv_set(kv, {keys[0]: {'key': keys[0], 'pool_account': 'pool-owner'}, keys[1]: {'key': keys[1]}})
    monkeypatch.setattr(settings, "allowed_users", {"owner"})
    monkeypatch.setattr(settings, "pools", False)
    before = lite_client.get("/api/state", headers=HEADERS).json()
    assert "pool_accounts" not in before
    assert before["pools"] is None
    monkeypatch.setattr(settings, "pools", True)
    enabled = lite_client.get("/api/state", headers=HEADERS).json()
    assert set(enabled) == set(before)
    assert "pool_accounts" not in enabled
    assert enabled["pools"] == {"enabled": True, "groups": [], "pools": []}
    monkeypatch.setattr(settings, "pools", False)
    after = lite_client.get("/api/state", headers=HEADERS).json()
    assert set(after) == set(before)
    for state in [before, enabled, after]:
        for section in ['accounts', 'codex_accounts']:
            listed = state[section]['list']
            assert len(listed) == 2
            assert all('pool_account' in r for r in listed)
            assert {r['pool_account'] for r in listed} == {None, 'pool-owner'}
    assert not (settings.data_dir / "profiles").exists() and rows(main.db) == []
    demo = json.loads((Path(__file__).resolve().parents[1] / 'app/static/demo/state.json').read_text())
    for section in ['accounts', 'codex_accounts']:
        assert demo[section]['list'] and all(r['pool_account'] is None for r in demo[section]['list'])


def test_bound_adapter_refuses_codex_names_and_last(box, monkeypatch):
    from app import agents
    from app.agents.base import LaunchReq
    account(box, "codex")
    adapter = agents.get("codex")
    monkeypatch.setattr(adapter, "_probe_caps", lambda: ({}, True))
    for kind, rid in [("resume", "named-thread"), ("resume", None), ("continue", None)]:
        with pytest.raises(projects.BadRequest, match="thread UUID"):
            adapter.launch_plan(LaunchReq(kind=kind, resume_id=rid, account_id="account-a"))
    plan = adapter.launch_plan(LaunchReq(kind="resume", resume_id=SID, account_id="account-a"))
    assert "env -u ANTHROPIC_API_KEY" in plan.cmd_line and SID in plan.argv


@pytest.mark.parametrize("provider,leaf", [("claude", "projects"), ("codex", "sessions")])
def test_first_profile_has_a_writable_transcript_link(box, provider, leaf):
    home = settings.claude_config_dir if provider == "claude" else settings.codex_home
    (home / leaf).rmdir()
    with pytest.raises(projects.BadRequest, match="transcript root does not exist"):
        profiles.ensure_profile(provider, "account-a")
    assert not (home / leaf).exists()
    assert not (settings.data_dir / "profiles").exists()


def test_docker_tmux_uses_host_visible_hook_and_exec_paths(box, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "runtime", "docker")
    monkeypatch.setenv("CCBOARD_HOST_PYTHON", str(tmp_path / "host-python"))
    path = account(box)
    seeded = settings.data_dir.resolve() / "app"
    template = json.loads((path / "settings.json").read_text())
    commands = [hook["command"] for groups in template["hooks"].values() for group in groups for hook in group["hooks"]]
    assert all(str(seeded / "bin") in cmd for cmd in commands)
    argv = shlex.split(profiles.wrap_command("claude", "claude", "account-a"))
    assert argv[-3:] == [str(tmp_path / "host-python"), str(seeded / "scripts/profile_exec.py"), "claude"]


def saved_slots(db, monkeypatch, provider):
    from app import account_store, accounts, codex_accounts
    store = account_store if provider == 'claude' else codex_accounts
    home = settings.claude_config_dir if provider == 'claude' else settings.codex_home
    (home / ('projects' if provider == 'claude' else 'sessions')).mkdir(parents=True, exist_ok=True)
    keys = ['first', 'second'] if provider == 'claude' else ['a' * 24, 'b' * 24]
    db.kv_set(accounts.KV_ACCOUNTS if provider == 'claude' else store.KV_ACCOUNTS,
              {k: {'key': k, 'label': k, 'uuid': k} for k in keys})
    monkeypatch.setattr(store, '_require', lambda: None)
    monkeypatch.setattr(store, '_login_in_flight', lambda: False)
    if provider == 'claude':
        monkeypatch.setattr(store, '_live_account', lambda db: (keys[1], {'uuid': keys[1]}))
    else:
        db.kv_set(store.KV_CURRENT, {'key': keys[1]})
        monkeypatch.setattr(store, '_board_sessions', lambda db: [])
    for key in keys:
        slot = store._slot_ready(key)
        store._write_atomic(slot / profiles._files(provider)[0], ('opaque fixture ' + key).encode())
        if provider == 'claude':
            store._write_atomic(slot / store.STATE, json.dumps({'oauthAccount': {'accountUuid': key}}).encode())
    return store, keys


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_handover_switch_saveback_release_and_flag_off(box, lite_client, monkeypatch, provider):
    from app import main
    monkeypatch.setattr(main, 'db', box)
    monkeypatch.setattr(settings, 'allowed_users', {'owner'})
    store, keys = saved_slots(box, monkeypatch, provider)
    endpoint = 'accounts' if provider == 'claude' else 'codex-accounts'
    result = lite_client.post('/api/pools/migrate', headers=HEADERS)
    assert result.status_code == 200, result.text
    moved, skipped = result.json()['results']
    assert skipped == {'slot': keys[1], 'skipped': 'live login: switch the legacy login to another account first'}
    aid = moved['moved']
    slot = store.slot_dir(keys[0])
    assert sorted(p.name for p in slot.iterdir()) == [profiles.READ_ONLY]
    assert profiles.owner(slot) == aid
    assert len(profiles.accounts_view(box)) == 1
    assert profiles.migrate(box)[0]['moved'] == aid
    expected = f'this login now belongs to pool account {aid}; release it in Settings > Accounts first'
    for enabled in (True, False):
        monkeypatch.setattr(settings, 'pools', enabled)
        for method, suffix in [('POST', 'switch'), ('DELETE', 'saved')]:
            response = lite_client.request(method, f'/api/{endpoint}/{keys[0]}/{suffix}', headers=HEADERS)
            assert response.status_code == 409 and response.json() == {'detail': expected}
        with pytest.raises(profiles.HandoverError, match='belongs to pool account'):
            store._write_atomic(slot / profiles._files(provider)[0], b'opaque replacement')
        # Exercise the real switch save-back, with a legacy live identity now naming the handed-over account.
        if provider == 'claude':
            monkeypatch.setattr(store, '_live_account', lambda db: (keys[0], {'uuid': keys[0]}))
            store._live_creds().parent.mkdir(parents=True, exist_ok=True)
            store._live_creds().write_bytes(b'opaque live fixture')
            with pytest.raises(profiles.HandoverError):
                store.save_live(box, keys[0])
        else:
            monkeypatch.setattr(store, '_live_bytes', lambda: (b'opaque live fixture', [1, 2, 3]))
            monkeypatch.setattr(store, '_confirm_current', lambda *a: (keys[0], False))
            monkeypatch.setattr(store, '_is_prev', lambda *a: False)
        response = lite_client.post(f'/api/{endpoint}/{keys[1]}/switch', headers=HEADERS)
        assert response.status_code == 409 and response.json() == {'detail': expected}
    monkeypatch.setattr(settings, 'pools', True)
    with profiles.inflight_launch(box) as owner:
        attempt = profiles.start_attempt(box, provider, aid, reservation_owner=owner)
        assert lite_client.post(f'/api/pools/accounts/{aid}/release', headers=HEADERS).status_code == 409
    profiles.finish_attempt(box, attempt, 'exited')
    response = lite_client.post(f'/api/pools/accounts/{aid}/release', headers=HEADERS)
    assert response.status_code == 200, response.text
    assert not (slot / profiles.READ_ONLY).exists()
    assert (slot / profiles._files(provider)[0]).read_bytes() == ('opaque fixture ' + keys[0]).encode()
    assert profiles.accounts_view(box) == []
    assert not (profiles.profile_dir(provider, aid) / profiles._files(provider)[0]).exists()


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_crash_after_file_move_before_row_insert_and_missing_files(box, monkeypatch, provider):
    store, keys = saved_slots(box, monkeypatch, provider)
    original = profiles._move
    def crash(source, dest):
        original(source, dest)
        raise SystemExit('simulated process death')
    monkeypatch.setattr(profiles, '_move', crash)
    with pytest.raises(SystemExit, match='simulated'):
        profiles.migrate(box)
    assert profiles.accounts_view(box) == []
    monkeypatch.setattr(profiles, '_move', original)
    result = profiles.migrate(box)
    aid = result[0]['moved']
    assert len(profiles.accounts_view(box)) == 1
    cred = profiles.profile_dir(provider, aid) / profiles._files(provider)[0]
    raw = cred.read_bytes()
    cred.unlink()
    assert 'missing' in profiles.migrate(box)[0]['error']
    # A row that outlived the move is repaired from the remaining slot file.
    (store.slot_dir(keys[0]) / profiles._files(provider)[0]).write_bytes(raw)
    assert profiles.migrate(box)[0]['moved'] == aid
    assert cred.read_bytes() == raw
    assert len(profiles.accounts_view(box)) == 1


def test_migration_errors_are_json_4xx(box, lite_client, monkeypatch):
    from app import main
    monkeypatch.setattr(main, 'db', box)
    monkeypatch.setattr(settings, 'allowed_users', {'owner'})
    store, keys = saved_slots(box, monkeypatch, 'claude')
    monkeypatch.setattr(profiles, 'ensure_profile', lambda *a, **k: (_ for _ in ()).throw(OSError('fixture failure')))
    response = lite_client.post('/api/pools/migrate', headers=HEADERS)
    assert response.status_code in (400, 409)
    assert response.json()['results'][0] == {'slot': keys[0], 'error': 'could not access slot files (OSError)'}


def test_startup_never_migrates():
    import inspect
    from app.main import lifespan
    source = inspect.getsource(lifespan)
    assert 'profiles.migrate' not in source
    assert 'profiles.reconcile_attempts(db)' in source


def test_clear_keeps_attempt_and_guard_then_flag_toggle_reconciles(box, monkeypatch):
    from fastapi import HTTPException
    from app import tmux
    account(box)
    monkeypatch.setattr(tmux, 'has_session', lambda name: True)
    row = box.add_session(tmux_name='test', project='p', repo='root', name='n', launcher='claude', account='account-a')
    profiles.start_attempt(box, 'claude', 'account-a', session_row=row)
    box.rebind_session_id('test', SID)
    box.set_state('test', 'ended', 'SessionEnd')
    box.rebind_session_id('test', '22222222-2222-4222-8222-222222222222')
    assert rows(box)[0]['ended_at'] is None
    with pytest.raises(HTTPException) as error:
        profiles.guard_switch(box)
    assert error.value.status_code == 409
    monkeypatch.setattr(settings, 'pools', False)
    box.end('test', 'exited')
    assert rows(box)[0]['ended_at'] is None
    monkeypatch.setattr(settings, 'pools', True)
    profiles.guard_switch(box)
    assert rows(box)[0]['end_reason'] == 'reconciled'


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_validation_has_no_filesystem_side_effects(box, provider):
    box.conn.execute('INSERT INTO pool_account(id,provider,label,state,cred_version,created_at) VALUES (?,?,?,?,?,?)', ('account-a', provider, 'A', 'ready', None, '2026-01-01'))
    profiles.require_account(box, provider, 'account-a')
    assert not (settings.data_dir / 'profiles').exists()
    with pytest.raises(projects.BadRequest):
        profiles.require_account(box, provider, 'missing')
    assert not (settings.data_dir / 'profiles').exists()


@pytest.mark.parametrize('mode,data', [(0o644, b'opaque fixture'), (0o600, b''), (0o600, bytes([255]))])
def test_setup_token_refused_in_child_and_exec(box, tmp_path, mode, data):
    path = account(box)
    token = path / profiles.SETUP_TOKEN
    token.write_bytes(data)
    token.chmod(mode)
    with pytest.raises(projects.BadRequest, match='.setup-token'):
        profiles.child_env('claude', 'account-a', {})
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/profile_exec.py'), sys.executable, '-c', 'pass'],
                            env={**os.environ, 'CLAUDE_CONFIG_DIR': str(path)}, capture_output=True, text=True)
    assert result.returncode != 0 and str(token) in result.stderr
    assert 'opaque fixture' not in result.stderr


def test_codex_profile_hooks_use_global_installer(box):
    from scripts import codex_hooks
    from app.doctor import _app_dir
    path = account(box, 'codex')
    expected = codex_hooks.install({}, _app_dir().resolve(), remote_approve=os.environ.get('CCBOARD_REMOTE_APPROVE', '1') != '0',
                                   approve_timeout=settings.approve_timeout, async_hooks=codex_hooks.async_default())
    assert json.loads((path / 'hooks.json').read_text()) == expected


def test_bound_shell_scrubs_entire_policy_after_rc(box, tmp_path):
    path = account(box)
    output = tmp_path / 'environment.json'
    home = tmp_path / 'home'
    home.mkdir()
    rc = home / 'rc'
    rc.write_text('\n'.join(f'export {key}=fixture-value' for key in [*profiles.UNSET, *(p + 'X' for p in profiles.PREFIXES)]))
    fake = tmp_path / 'claude'
    fake.write_text('#!' + sys.executable + '\nimport json, os\nfrom pathlib import Path\nPath(os.environ["DUMP_ENV"]).write_text(json.dumps(dict(os.environ)))\n')
    fake.chmod(0o700)
    line = profiles.wrap_command(shlex.quote(str(fake)), 'claude', 'account-a')
    # Explicitly source the temp rc in a login shell: portable even when /etc/profile resets HOME.
    result = subprocess.run(['bash', '-lc', '. ' + shlex.quote(str(rc)) + '\n' + line],
                            env={**os.environ, 'HOME': str(home), 'DUMP_ENV': str(output)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    env = json.loads(output.read_text())
    assert env['CLAUDE_CONFIG_DIR'] == str(path)
    assert all(key not in env for key in profiles.UNSET if key != 'CLAUDE_CONFIG_DIR')
    assert not any(key.startswith(profiles.PREFIXES) for key in env)


def test_fallback_ambiguous_first_id_line_and_existing_id(box, tmp_path, monkeypatch):
    account(box)
    root = profiles.transcript_directory('claude', tmp_path)
    root.mkdir()
    aid = profiles.start_attempt(box, 'claude', 'account-a')
    first = root / 'first.jsonl'
    first.write_text('{}\n' + json.dumps({'cwd': str(tmp_path), 'sessionId': SID}) + '\n')
    assert profiles.newest_transcript('claude', tmp_path, time.time() - 1) == SID
    (root / 'second.jsonl').write_text(first.read_text())
    assert profiles.newest_transcript('claude', tmp_path, time.time() - 1) is None
    assert profiles.newest_transcript('claude', tmp_path, time.time() + 3) is None
    box.conn.execute('UPDATE runtime_attempt SET provider_session_id=? WHERE id=?', (SID, aid))
    monkeypatch.setattr(profiles, 'newest_transcript', lambda *a: pytest.fail('already bound'))
    assert profiles.discover_attempt(box, aid, str(tmp_path), 'claude', 0) is None


def test_bound_readings_and_switch_continue_skip(box, monkeypatch):
    from app import hooks, autoresume
    account(box)
    rid = box.add_session(tmux_name='test', project='p', repo='root', name='n', launcher='claude', account='account-a')
    profiles.start_attempt(box, 'claude', 'account-a', session_row=rid)
    assert hooks._attribute_reading(box, 'test', box.open_row('test'), {}) == ('account-a', False)
    assert not box.set_session_account('test', 'legacy')
    monkeypatch.setattr(autoresume, 'episodes', lambda *a: [{'session': 'test', 'resets_at': time.time() + 60, 'kind': '5h'}])
    monkeypatch.setattr(autoresume, '_still_parked', lambda *a: True)
    assert autoresume.continue_parked(box, send=lambda *a: pytest.fail('bound session continued'), clients=lambda n: 0, alive=lambda n: True) == []


def test_task_validation_and_launch_plan_do_not_make_directories(box, lite_client, projects_dir, monkeypatch):
    from app import main, agents
    from app.agents.base import LaunchReq
    monkeypatch.setattr(main, 'db', box)
    (projects_dir / 'project').mkdir()
    box.conn.execute('INSERT INTO pool_account(id,provider,label,state,cred_version,created_at) VALUES (?,?,?,?,?,?)', ('account-a', 'claude', 'A', 'ready', None, '2026-01-01'))
    body = main.TaskIn(title='Task', prompt='Do it', account_id='account-a')
    main._task_check('project', 'root', body, launching=False)
    agents.get('claude').launch_plan(LaunchReq(kind='new', account_id='account-a'))
    assert not (settings.data_dir / 'profiles').exists()


def test_fallback_does_not_overwrite_an_id_learned_while_reading(box, tmp_path, monkeypatch):
    account(box)
    rid = box.add_session(tmux_name='test', project='p', repo='root', name='n', launcher='claude')
    aid = profiles.start_attempt(box, 'claude', 'account-a', session_row=rid)
    def discovered(*args):
        box.rebind_session_id('test', SID)
        return '22222222-2222-4222-8222-222222222222'
    monkeypatch.setattr(profiles, 'newest_transcript', discovered)
    thread = profiles.discover_attempt(box, aid, str(tmp_path), 'claude', 0, 'test')
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert box.open_row('test')['claude_session_id'] == SID
    assert rows(box)[0]['provider_session_id'] == SID


def test_fallback_polls_one_second_for_at_most_ten_seconds(box, tmp_path, monkeypatch):
    account(box)
    aid = profiles.start_attempt(box, 'claude', 'account-a')
    clock, sleeps, reads = [0], [], []
    monkeypatch.setattr(profiles.time, 'monotonic', lambda: clock[0])
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(profiles.time, 'sleep', sleep)
    monkeypatch.setattr(profiles, 'newest_transcript', lambda *a: reads.append(a) or None)
    thread = profiles.discover_attempt(box, aid, str(tmp_path), 'claude', 0)
    thread.join(timeout=2)
    assert sleeps == [1] * 10 and len(reads) == 10
    assert rows(box)[0]['provider_session_id'] is None


@pytest.mark.parametrize('kind', ['pane', 'run'])
def test_reconcile_dead_pane_or_missing_run(box, monkeypatch, kind):
    from app import tmux
    account(box)
    if kind == 'pane':
        rid = box.add_session(tmux_name='test', project='p', repo='root', name='n', launcher='claude')
        profiles.start_attempt(box, 'claude', 'account-a', session_row=rid)
        monkeypatch.setattr(tmux, 'has_session', lambda name: False)
    else:
        profiles.start_attempt(box, 'claude', 'account-a', task_id='run:123')
    profiles.guard_switch(box)
    assert rows(box)[0]['end_reason'] == ('reconciled' if kind == 'pane' else 'reservation_abandoned')


def test_legacy_restore_merge_and_relogin_raise_typed_conflicts(box, monkeypatch):
    from app import account_store, codex_accounts
    store, keys = saved_slots(box, monkeypatch, 'claude')
    aid = profiles.migrate(box)[0]['moved']
    with pytest.raises(profiles.HandoverError, match=aid):
        store._restore(store.slot_dir(keys[0]), {store.CREDS: b'opaque fixture'})
    with pytest.raises(profiles.HandoverError, match=aid):
        store._put_oauth(store.slot_dir(keys[0]) / store.STATE, {}, create=True)
    records = account_store.accounts._load(box)
    records[keys[0]]['email'] = 'fixture@example.test'
    box.kv_set(account_store.accounts.KV_ACCOUNTS, records)
    with pytest.raises(profiles.HandoverError, match=aid):
        store.start_login(box, 'fixture@example.test')
    store, keys = saved_slots(box, monkeypatch, 'codex')
    aid = next(r['moved'] for r in profiles.migrate(box) if r['slot'] == keys[0])
    with pytest.raises(profiles.HandoverError, match=aid):
        store._merge(box, *keys, None)
    with pytest.raises(profiles.HandoverError, match=aid):
        store.start_login(box, None, replace_key=keys[0])


def test_settings_marker_metadata_needs_no_hot_path_file_reads(box, monkeypatch):
    from app import account_store, accounts
    store, keys = saved_slots(box, monkeypatch, 'claude')
    aid = profiles.migrate(box)[0]['moved']
    monkeypatch.setattr(settings, 'pools', False)
    monkeypatch.setattr(profiles, 'owner', lambda *a: pytest.fail('marker read on state path'))
    view = account_store.decorate(accounts.view(box), box)
    assert next(r for r in view['list'] if r['key'] == keys[0])['pool_account'] == aid


def test_release_recovers_a_crash_after_returning_credentials(box, monkeypatch):
    store, keys = saved_slots(box, monkeypatch, 'claude')
    aid = profiles.migrate(box)[0]['moved']
    original = profiles._move
    def crash(source, dest):
        original(source, dest)
        raise SystemExit('simulated release death')
    monkeypatch.setattr(profiles, '_move', crash)
    with pytest.raises(SystemExit):
        profiles.release(box, aid)
    assert 'release is incomplete' in profiles.migrate(box)[0]['error']
    monkeypatch.setattr(profiles, '_move', original)
    assert profiles.release(box, aid) == {'released': aid}
    assert profiles.accounts_view(box) == []
    assert not (store.slot_dir(keys[0]) / profiles.READ_ONLY).exists()


def test_flag_toggle_reconcile_returns_200_from_real_switch_route(box, lite_client, monkeypatch):
    from app import main, tmux
    monkeypatch.setattr(main, 'db', box)
    monkeypatch.setattr(settings, 'allowed_users', {'owner'})
    store, keys = saved_slots(box, monkeypatch, 'claude')
    aid = profiles.migrate(box)[0]['moved']
    rid = box.add_session(tmux_name='test', project='p', repo='root', name='n', launcher='claude', account=aid)
    profiles.start_attempt(box, 'claude', aid, session_row=rid)
    monkeypatch.setattr(tmux, 'has_session', lambda name: True)
    url = f'/api/accounts/{keys[1]}/switch'
    box.set_state('test', 'ended', 'SessionEnd')
    assert lite_client.post(url, headers=HEADERS).status_code == 409
    monkeypatch.setattr(settings, 'pools', False)
    box.end('test', 'exited')
    monkeypatch.setattr(settings, 'pools', True)
    response = lite_client.post(url, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert rows(box)[0]['end_reason'] == 'reconciled'


def test_codex_migration_skips_live_bytes_even_when_cached_current_is_stale(box, monkeypatch):
    store, keys = saved_slots(box, monkeypatch, 'codex')
    raw = (store.slot_dir(keys[0]) / store.AUTH).read_bytes()
    store._live_auth().write_bytes(raw)
    results = profiles.migrate(box)
    assert results[0]['slot'] == keys[0] and 'live login' in results[0]['skipped']
    assert (store.slot_dir(keys[0]) / store.AUTH).read_bytes() == raw
    assert not (store.slot_dir(keys[0]) / profiles.READ_ONLY).exists()


@pytest.mark.parametrize('kind', ['directory', 'symlink', 'owner', 'read_error'])
def test_setup_token_file_type_owner_and_read_errors(box, tmp_path, monkeypatch, kind):
    from scripts import profile_env
    path = account(box)
    token = path / profiles.SETUP_TOKEN
    if kind == 'directory':
        token.mkdir()
    elif kind == 'symlink':
        target = tmp_path / 'fixture'
        target.write_text('opaque fixture')
        token.symlink_to(target)
    else:
        token.write_text('opaque fixture')
        token.chmod(0o600)
        if kind == 'owner':
            uid = os.getuid()
            monkeypatch.setattr(profile_env.os, 'getuid', lambda: uid + 1)
        else:
            original = profile_env.os.open
            def fail(file, *args, **kwargs):
                if Path(file) == token:
                    raise OSError('fixture failure')
                return original(file, *args, **kwargs)
            monkeypatch.setattr(profile_env.os, 'open', fail)
    with pytest.raises((projects.BadRequest, ValueError), match='.setup-token'):
        profiles.child_env('claude', 'account-a', {})


def test_live_slot_migration_does_not_create_a_profile(box, monkeypatch):
    store, keys = saved_slots(box, monkeypatch, 'claude')
    # Only the current saved slot exists.
    import shutil
    shutil.rmtree(store.slot_dir(keys[0]))
    results = profiles.migrate(box)
    assert results == [{'slot': keys[1], 'skipped': 'live login: switch the legacy login to another account first'}]
    assert not (settings.data_dir / 'profiles').exists()


def test_move_crash_after_fsync_before_unlink_converges(box, monkeypatch):
    store, keys = saved_slots(box, monkeypatch, 'claude')
    source = store.slot_dir(keys[0]) / store.CREDS
    unlink = Path.unlink
    def crash(path, *args, **kwargs):
        if path == source:
            raise SystemExit('simulated unlink death')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', crash)
    with pytest.raises(SystemExit):
        profiles.migrate(box)
    assert source.exists()
    aid = profiles.owner(source.parent)
    assert (profiles.profile_dir('claude', aid) / store.CREDS).read_bytes() == source.read_bytes()
    assert profiles.accounts_view(box) == []
    monkeypatch.setattr(Path, 'unlink', unlink)
    assert profiles.migrate(box)[0]['moved'] == aid
    assert not source.exists() and len(profiles.accounts_view(box)) == 1


@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('populated', [False, True])
@pytest.mark.parametrize('lag', [0.01, 10])
def test_transcript_snapshot_freshness_ignores_mtime(box, tmp_path, provider, populated, lag, no_birthtime):
    root = profiles.transcript_directory(provider, tmp_path)
    root.mkdir(parents=True)
    if populated:
        (root / 'old.jsonl').write_text('{}\n')
    snapshot = profiles.transcript_snapshot(provider, tmp_path)
    started = 1793022432.0
    path = root / 'new.jsonl'
    meta = {'sessionId': SID, 'cwd': str(tmp_path)} if provider == 'claude' else {'type': 'session_meta', 'payload': {'id': SID, 'cwd': str(tmp_path)}}
    path.write_text(json.dumps(meta) + '\n')
    os.utime(path, (started - lag, started - lag))
    assert profiles.newest_transcript(provider, tmp_path, started, snapshot) == SID
    second = root / 'second.jsonl'
    second.write_text(path.read_text())
    os.utime(second, (started - lag, started - lag))
    assert profiles.newest_transcript(provider, tmp_path, started, snapshot) is None


@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('lag,expected', [(0.01, SID), (2, SID), (2.01, None)])
def test_transcript_without_snapshot_allows_mtime_slack(box, tmp_path, provider, lag, expected, no_birthtime):
    root = profiles.transcript_directory(provider, tmp_path)
    root.mkdir(parents=True)
    started = 1793022432.0
    path = root / 'new.jsonl'
    meta = {'sessionId': SID, 'cwd': str(tmp_path)} if provider == 'claude' else {'type': 'session_meta', 'payload': {'id': SID, 'cwd': str(tmp_path)}}
    path.write_text(json.dumps(meta) + '\n')
    os.utime(path, (started - lag, started - lag))
    assert profiles.newest_transcript(provider, tmp_path, started) == expected


@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('change', ['touch', 'append', 'rewrite', 'replace'])
def test_transcript_snapshot_excludes_existing_files(box, tmp_path, provider, change, no_birthtime):
    root = profiles.transcript_directory(provider, tmp_path)
    root.mkdir(parents=True)
    path = root / 'old.jsonl'
    meta = {'sessionId': SID, 'cwd': str(tmp_path)} if provider == 'claude' else {'type': 'session_meta', 'payload': {'id': SID, 'cwd': str(tmp_path)}}
    data = json.dumps(meta) + '\n'
    path.write_text(data)
    snapshot = profiles.transcript_snapshot(provider, tmp_path)
    started = time.time()
    if change == 'append':
        with path.open('a') as f:
            f.write('{}\n')
    elif change == 'rewrite':
        path.write_text(data + '{}\n')
    elif change == 'replace':
        replacement = root / 'replacement.tmp'
        replacement.write_text(data)
        replacement.replace(path)
    # Include the equality boundary, and give even the old file a fresh mtime.
    os.utime(path, (started, started))
    expected = SID if change == 'replace' else None
    assert profiles.newest_transcript(provider, tmp_path, started, snapshot) == expected


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_flag_off_handover_errors_precede_missing_cli(box, lite_client, monkeypatch, provider):
    from app import main
    monkeypatch.setattr(main, 'db', box)
    monkeypatch.setattr(settings, 'allowed_users', {'owner'})
    store, keys = saved_slots(box, monkeypatch, provider)
    aid = next(r['moved'] for r in profiles.migrate(box) if r['slot'] == keys[0])
    monkeypatch.setattr(settings, 'pools', False)
    monkeypatch.setattr(store, '_require', lambda: pytest.fail('marker must be checked before probing a CLI'))
    endpoint = 'accounts' if provider == 'claude' else 'codex-accounts'
    response = lite_client.delete(f'/api/{endpoint}/{keys[0]}/saved', headers=HEADERS)
    assert response.status_code == 409 and aid in response.json()['detail']
    if provider == 'claude':
        records = store.accounts._load(box)
        records[keys[0]]['email'] = 'fixture@example.test'
        box.kv_set(store.accounts.KV_ACCOUNTS, records)
        body = {'email': 'fixture@example.test'}
        with pytest.raises(profiles.HandoverError):
            store.save_live(box, keys[0])
    else:
        body = {'replace_key': keys[0]}
    response = lite_client.post(f'/api/{endpoint}/login', headers=HEADERS, json=body)
    assert response.status_code == 409 and aid in response.json()['detail']


@pytest.mark.parametrize('error', ['TmuxDown', 'TmuxError'])
def test_tmux_unknown_preserves_attempt_and_routes_return_503(box, lite_client, fake_tmux, monkeypatch, error):
    from app import main, tmux
    monkeypatch.setattr(main, 'db', box)
    monkeypatch.setattr(settings, 'allowed_users', {'owner'})
    account(box)
    rid = box.add_session(tmux_name='pool-shell', project='p', repo='r', name='n', launcher='claude')
    profiles.start_attempt(box, 'claude', 'account-a', session_row=rid)
    def unavailable(*a):
        raise getattr(tmux, error)('fixture down')
    monkeypatch.setattr(tmux, 'has_session', unavailable)
    assert profiles.reconcile_attempts(box) is False
    for url in ['/api/accounts/any/switch', '/api/codex-accounts/any/switch', '/api/pools/accounts/account-a/release']:
        response = lite_client.post(url, headers=HEADERS)
        assert response.status_code == 503
        assert response.json() == {'detail': 'tmux is not running; cannot check pool sessions'}
    assert rows(box)[0]['ended_at'] is None


@pytest.mark.parametrize('unexpected', [False, True])
def test_startup_survives_pool_reconciliation_failure(box, monkeypatch, caplog, unexpected):
    import asyncio
    from app import main, tmux
    account(box)
    rid = box.add_session(tmux_name='open', project='p', repo='r', name='n', launcher='claude')
    profiles.start_attempt(box, 'claude', 'account-a', session_row=rid)
    def down(*a):
        raise tmux.TmuxDown('fixture down')
    monkeypatch.setattr(tmux, 'has_session', down)
    monkeypatch.setattr(main, 'DB', lambda *a: box)
    monkeypatch.setattr(settings, 'validate', lambda: None)
    if unexpected:
        monkeypatch.setattr(profiles, 'reconcile_attempts', lambda *a: (_ for _ in ()).throw(RuntimeError('fixture')))
    class ReachedNextStartupStep(Exception):
        pass
    monkeypatch.setattr(main.hooks, 'ensure_token', lambda: (_ for _ in ()).throw(ReachedNextStartupStep()))
    async def run():
        async with main.lifespan(main.app):
            pytest.fail('workers must not start in this test')
    with pytest.raises(ReachedNextStartupStep):
        asyncio.run(run())
    assert rows(box)[0]['ended_at'] is None
    if unexpected:
        assert 'pool attempt reconciliation failed at startup' in caplog.text


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_login_handover_finishes_as_failure_and_cleans_pending(box, monkeypatch, provider):
    from app import accounts
    store, keys = saved_slots(box, monkeypatch, provider)
    aid = profiles.migrate(box)[0]['moved']
    monkeypatch.setattr(store, 'supported', lambda: True)
    monkeypatch.setattr(store, '_kill_login_session', lambda: None)
    pend = store.pending_dir()
    pend.mkdir()
    leaf = profiles._files(provider)[0]
    (pend / leaf).write_bytes(b'opaque new login')
    os.utime(pend / leaf, (1, 1))
    monkeypatch.setitem(store._login, 'adding', True)
    if provider == 'claude':
        monkeypatch.setattr(accounts, 'read_identity', lambda **kw: {'key': keys[0], 'email': 'fixture@example.test'})
        monkeypatch.setattr(accounts, 'remember', lambda *a, **kw: pytest.fail('must check before remembering identity'))
    else:
        monkeypatch.setitem(store._login, 'replace_key', keys[0])
        monkeypatch.setattr(store, '_read_stable', lambda *a: (b'opaque new login', (1, 2, 3)))
    result = store.finalize(box)
    assert result['ok'] is False
    assert result['error'] == f'this login belongs to pool account {aid}; release it first'
    assert not pend.exists() and store._login['adding'] is False
    assert store.finalize(box) is None
    assert profiles.credential_path(provider, aid).read_bytes() == ('opaque fixture ' + keys[0]).encode()


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_known_handover_refused_before_login_work(box, monkeypatch, provider):
    from app import accounts
    store, keys = saved_slots(box, monkeypatch, provider)
    if provider == 'claude':
        records = accounts._load(box)
        records[keys[0]]['email'] = 'fixture@example.test'
        box.kv_set(accounts.KV_ACCOUNTS, records)
    aid = profiles.migrate(box)[0]['moved']
    monkeypatch.setattr(store, '_require', lambda: pytest.fail('login work before handover check'))
    with pytest.raises(profiles.HandoverError, match=aid):
        if provider == 'claude':
            store.start_login(box, 'fixture@example.test')
        else:
            store.start_login(box, None, replace_key=keys[0])
    assert not store.pending_dir().exists()


def test_codex_handover_rename_saves_only_kv(box, lite_client, monkeypatch):
    from app import main
    monkeypatch.setattr(main, 'db', box)
    monkeypatch.setattr(settings, 'allowed_users', {'owner'})
    store, keys = saved_slots(box, monkeypatch, 'codex')
    aid = profiles.migrate(box)[0]['moved']
    monkeypatch.setattr(store, '_write_meta', lambda *a: pytest.fail('handed-over metadata file write'))
    response = lite_client.patch(f'/api/codex-accounts/{keys[0]}', json={'label': 'Renamed pool login'}, headers=HEADERS)
    assert response.status_code == 200, response.text
    assert store._load(box)[keys[0]]['label'] == 'Renamed pool login'
    assert profiles.owner(store.slot_dir(keys[0])) == aid
    assert list(store.slot_dir(keys[0]).iterdir()) == [store.slot_dir(keys[0]) / profiles.READ_ONLY]


@pytest.mark.parametrize('current_is_handed_over', [False, True])
def test_codex_learn_skips_handed_over_keys(box, monkeypatch, current_is_handed_over):
    store, keys = saved_slots(box, monkeypatch, 'codex')
    profiles.migrate(box)
    records = store._load(box)
    records[keys[0]]['account_id'] = 'shared-id'
    box.kv_set(store.KV_ACCOUNTS, records)
    if current_is_handed_over:
        box.kv_set(store.KV_CURRENT, {'key': keys[0]})
        monkeypatch.setattr(store, '_recent_rollouts', lambda: pytest.fail('learning handed-over current account'))
    else:
        monkeypatch.setattr(store, '_recent_rollouts', lambda: ['fixture'])
        monkeypatch.setattr(store, '_rollout_meta', lambda p: {'created': time.time(), 'account_id': 'shared-id', 'user_id': None})
        monkeypatch.setattr(store, '_rollout_plan', lambda p: None)
    monkeypatch.setattr(store, '_merge', lambda *a: pytest.fail('merging handed-over key'))
    store._learn(box, None, force=True)
    assert set(store._load(box)) == set(keys)


@pytest.mark.parametrize('winner', ['release', 'launch'])
def test_release_launch_race_has_one_winner(box, fake_tmux, monkeypatch, winner):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from fastapi import HTTPException
    from app import main, tmux
    monkeypatch.setattr(main, 'db', box)
    monkeypatch.setattr(main, 'require_real_launch_ok', lambda: None)
    store, keys = saved_slots(box, monkeypatch, 'claude')
    aid = profiles.migrate(box)[0]['moved']
    reached, proceed = threading.Event(), threading.Event()
    def launch():
        return main._start_session_row('pool-launch', 'p', 'r', 'n', 'claude', '/tmp', cmd_line='claude',
                                       claude_session_id=SID, opts={'account_id': aid})
    if winner == 'release':
        original = profiles.start_attempt
        def pause(*a, **kw):
            reached.set()
            assert proceed.wait(5)
            return original(*a, **kw)
        monkeypatch.setattr(profiles, 'start_attempt', pause)
    else:
        original = tmux.new_session
        def pause(*a, **kw):
            reached.set()
            assert proceed.wait(5)
            return original(*a, **kw)
        monkeypatch.setattr(tmux, 'new_session', pause)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(launch)
        try:
            assert reached.wait(5)
            if winner == 'release':
                assert profiles.release(box, aid) == {'released': aid}
            else:
                with pytest.raises(HTTPException) as err:
                    profiles.release(box, aid)
                assert err.value.status_code == 409
        finally:
            proceed.set()
        if winner == 'release':
            with pytest.raises(projects.BadRequest, match='no such account'):
                pending.result(5)
            assert not fake_tmux['created'] and rows(box) == [] and box.open_rows() == {}
        else:
            assert pending.result(5)[0] == 'pool-launch'
            assert rows(box)[0]['ended_at'] is None
            with pytest.raises(HTTPException) as err:
                profiles.release(box, aid)
            assert err.value.status_code == 409 and 'pool-launch' in err.value.detail


@pytest.mark.parametrize('stage', ['new_session', 'add_session', 'binding', 'send_line'])
def test_bound_launch_failure_leaves_no_live_session_or_attempt(box, fake_tmux, monkeypatch, stage):
    from app import main, tmux
    monkeypatch.setattr(main, 'db', box)
    monkeypatch.setattr(main, 'require_real_launch_ok', lambda: None)
    account(box)
    def fail(*a, **kw):
        raise tmux.TmuxDown('fixture launch failure')
    if stage in ('new_session', 'send_line'):
        monkeypatch.setattr(tmux, stage, fail)
    elif stage == 'add_session':
        monkeypatch.setattr(box, stage, fail)
    else:
        monkeypatch.setattr(box, 'kv_set', fail)
    with pytest.raises(tmux.TmuxDown):
        main._start_session_row('pool-launch', 'p', 'r', 'n', 'claude', '/tmp', cmd_line='claude',
                                claude_session_id=SID, opts={'account_id': 'account-a'})
    assert not fake_tmux['sessions'] and box.open_rows() == {}
    assert len(rows(box)) == 1 and rows(box)[0]['end_reason'] == 'launch_failed'


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_pane_scan_ends_bound_agent_even_after_hook_state(box, fake_tmux, monkeypatch, provider):
    from app import main, tmux
    from fastapi import HTTPException
    monkeypatch.setattr(main, 'db', box)
    monkeypatch.setattr(main.notify, 'notify_session', lambda *a: None)
    monkeypatch.setattr(main, '_taskflow', lambda: type('Flow', (), {'on_turn_end': lambda *a, **kw: None})())
    account(box, provider)
    for name in ['pool-one', 'pool-two']:
        tmux.new_session(name, '/tmp')
        rid = box.add_session(tmux_name=name, project='p', repo='r', name=name, launcher=provider, agent=provider,
                              opts={'account_id': 'account-a'})
        profiles.start_attempt(box, provider, 'account-a', session_row=rid)
        box.conn.execute("UPDATE sessions SET created_at='2020-01-01T00:00:00Z' WHERE id=?", (rid,))
        box.set_state(name, 'idle', 'Stop')
    with pytest.raises(HTTPException) as err:
        profiles.guard_switch(box)
    assert all(n in err.value.detail for n in ['pool-one', 'pool-two'])
    main._merged_sessions(rich=True)
    assert all(r['end_reason'] == 'agent_exited' for r in rows(box))
    assert set(fake_tmux['sessions']) == {'pool-one', 'pool-two'}
    profiles.guard_switch(box)


def test_pool_db_access_is_locked_and_reconciliation_batches_bindings(box, fake_tmux, monkeypatch):
    store, keys = saved_slots(box, monkeypatch, 'claude')
    conn = box.conn
    statements = []
    class CheckedConnection:
        def execute(self, sql, *args):
            assert box.lock.locked(), sql
            statements.append(sql)
            return conn.execute(sql, *args)
        def __getattr__(self, key):
            return getattr(conn, key)
    monkeypatch.setattr(box, 'conn', CheckedConnection())
    aid = profiles.migrate(box)[0]['moved']
    for i in range(10):
        rid = box.add_session(tmux_name=f's{i}', project='p', repo='r', name=str(i), launcher='claude')
        profiles.start_attempt(box, 'claude', aid, session_row=rid)
    original_get = box.kv_get
    monkeypatch.setattr(box, 'kv_get', lambda *a: pytest.fail('per-session kv lookup'))
    statements.clear()
    profiles.reconcile_attempts(box)
    assert sum('JOIN kv' in s for s in statements) == 1
    # One batched orphan snapshot plus one batched session/run liveness snapshot.
    assert sum('FROM runtime_attempt a' in s for s in statements) == 2
    assert not any(s.lstrip().startswith('SELECT value FROM kv') for s in statements)
    monkeypatch.setattr(box, 'kv_get', original_get)
    # Check release separately too, including its first read and active-attempt query.
    monkeypatch.setattr(box, 'conn', CheckedConnection())
    profiles.release(box, aid)


@pytest.mark.parametrize('method,url,function', [
    ('GET', '/api/pools/accounts', 'accounts_view'),
    ('POST', '/api/pools/migrate', 'migrate'),
    ('POST', '/api/pools/accounts/account-a/release', 'release'),
])
def test_pool_routes_map_sqlite_errors_to_json(lite_client, monkeypatch, method, url, function):
    import sqlite3
    monkeypatch.setattr(settings, 'pools', True)
    monkeypatch.setattr(settings, 'allowed_users', {'owner'})
    def fail(*a):
        raise sqlite3.OperationalError('private diagnostic')
    monkeypatch.setattr(profiles, function, fail)
    response = lite_client.request(method, url, headers=HEADERS)
    assert response.status_code == 500 and response.json() == {
        'detail': 'pool database operation failed', 'error': 'pool database operation failed'}


def test_migrate_propagates_database_failure_for_route_500(box, monkeypatch):
    import sqlite3
    saved_slots(box, monkeypatch, 'claude')
    monkeypatch.setattr(profiles, '_set_slot_owner', lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError('fixture')))
    with pytest.raises(sqlite3.OperationalError):
        profiles.migrate(box)


def test_flag_off_event_paths_do_zero_pool_kv_reads(box, monkeypatch):
    from app import hooks, accounts, autoresume
    monkeypatch.setattr(settings, 'pools', False)
    box.add_session(tmux_name='legacy', project='p', repo='r', name='n', launcher='claude', account='old')
    reads = []
    original = box.kv_get
    def counting(key):
        reads.append(key)
        return original(key)
    monkeypatch.setattr(box, 'kv_get', counting)
    assert box.set_session_account('legacy', 'new')
    assert reads == []
    monkeypatch.setattr(accounts, 'for_reading', lambda *a: 'new')
    monkeypatch.setattr(accounts, 'current', lambda *a: 'new')
    assert hooks._attribute_reading(box, 'legacy', box.open_row('legacy'), {}) == ('new', True)
    assert reads == []
    monkeypatch.setattr(autoresume, 'episodes', lambda *a: [{'session': 'legacy', 'resets_at': 123, 'kind': '5h'}])
    monkeypatch.setattr(autoresume, '_still_parked', lambda *a: True)
    assert autoresume.continue_parked(box, send=lambda *a: None, clients=lambda n: 0, alive=lambda n: True) == ['legacy']
    assert reads == ['autoresume:legacy:123']  # the existing deduplication read only


def test_extended_scrub_list_and_intentional_subagent_model(box):
    from scripts.profile_env import scrub
    keys = ['ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL',
            'AWS_PROFILE', 'AWS_REGION', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
            'GOOGLE_APPLICATION_CREDENTIALS', 'CLOUD_ML_REGION']
    assert set(keys) <= set(profiles.UNSET)
    assert scrub({**dict.fromkeys(keys, 'inherited'), 'CLAUDE_CODE_SUBAGENT_MODEL': 'intentional'}) == {
        'CLAUDE_CODE_SUBAGENT_MODEL': 'intentional'}


def test_codex_profile_trust_review_instructions_and_existing_records(box):
    path = account(box, 'codex')
    reviewed = (path / 'config.toml').read_text() + f'\n[hooks.state."{path}/hooks.json:session_start:0:0"]\ntrusted_hash = "sha256:fixture"\n'
    (path / 'config.toml').write_text(reviewed)
    profiles.ensure_profile('codex', 'account-a')
    assert (path / 'config.toml').read_text() == reviewed
    doc = (Path(__file__).resolve().parents[1] / 'docs/pools.md').read_text()
    for required in ['CCBOARD_CODEX_HOOK_TRUST=review', "CODEX_HOME='<data dir>/profiles/codex/<account_id>' codex",
                     '/hooks', 'trusted_hash', 'per profile', 'does not grant trust automatically']:
        assert required in doc


def test_release_serializes_with_pool_probe_and_prunes_metadata(box, monkeypatch):
    import threading
    from app import pool_usage as pu
    store, keys = saved_slots(box, monkeypatch, 'codex')
    aid = profiles.migrate(box)[0]['moved']
    path = profiles.profile_dir('codex', aid)
    (path / 'auth.json').write_text(json.dumps({'tokens': {'account_id': 'workspace'}}))
    monkeypatch.setattr(profiles, '_check_liveness', lambda db: None)
    n = profiles.start_attempt(box, 'codex', aid)
    profiles.finish_attempt(box, n, 'done')
    a = pu.ensure_groups(box)[0]
    entered, unblock, released = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(pu.shutil, 'which', lambda _: 'fake')
    def probe(account):
        entered.set()
        assert unblock.wait(3)
        assert (path / 'auth.json').exists()
        assert box.conn.execute('SELECT state FROM pool_account WHERE id=?', (aid,)).fetchone()[0] == 'ready'
        return {'window_5h_pct': 20}
    monkeypatch.setattr(pu, 'app_server', probe)
    c = pu.collector(box)
    sample = threading.Thread(target=lambda: c.active_sample(a, time.time()))
    sample.start()
    assert entered.wait(2)
    errors = []
    def release_account():
        try:
            profiles.release(box, aid)
            released.set()
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=release_account)
    worker.start()
    try:
        assert not released.wait(.1), 'release overlapped app-server'
        assert (path / 'auth.json').exists()
    finally:
        unblock.set()
        sample.join(3)
        worker.join(3)
    assert not errors and released.is_set()
    assert box.kv_get('pool_refresh:' + aid) is None
    assert box.conn.execute('SELECT count(*) FROM quota_group').fetchone()[0] == 0
    assert c.active_sample(a, time.time() + 600) is None
