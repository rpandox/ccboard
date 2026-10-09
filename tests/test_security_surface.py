"""Issue #44: the board's own attack surface, pinned (docs/security-audit-2026-10.md has the findings table).

1. The route table. EXPECTED is every route of the running app (app.routes) with the auth it needs. A new route fails
   test_route_table_is_complete until it is listed here with its kind, and every listed route is probed through the real
   middleware: without an identity it is refused (only /healthz answers, and /mcp is a 404 while off), and every non-GET route
   that a person calls is refused without the X-CCBoard header. Both probes stop in auth_middleware, so no handler runs.
2. One regression test per finding the audit fixed (F-ids from the report), each written to fail before its fix.
3. The log-hygiene test: a representative slice (the full startup, device tokens minted, used, refused and revoked, the hook
   token right and wrong, a clone URL carrying a password) under DEBUG capture; no token, digest, password or login may appear.

Kinds: identity (the Tailscale login on CCBOARD_ALLOWED_USERS), identity+csrf (that and X-CCBoard: 1), hook-token (the 0600
<data dir>/hook-token only), identity+device-token (/mcp, issue #13), hub-token|identity (/api/node/summary), none (/api/node/hello,
issue #133: exactly {app, api, node_id}, rate limited; nothing else under /api/node answers without auth). Besides these, the
hook token also opens every other /api/* route without identity or X-CCBoard (local automation; accepted, see the report, F-A2).
No route answers without auth except /healthz, which is answered by the middleware itself and is not in app.routes, and /api/node/hello.
Temp dirs and fakes only (tests/conftest.py); nothing here starts tmux, git over the network, claude or codex.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import subprocess

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Mount

ID = {"Tailscale-User-Login": "alice@example.com"}
H = {**ID, "X-CCBoard": "1"}
UUID = "0b0e7c4c-1a2b-4c3d-8e9f-0123456789ab"
CSP = "default-src 'self'; frame-ancestors 'none'"
NO_ID = "no Tailscale identity or not in CCBOARD_ALLOWED_USERS"
NO_CSRF = "missing X-CCBoard header"

EXPECTED = {
    ("GET", "/api/state"): "identity",
    ("GET", "/api/agents"): "identity",
    ("GET", "/api/doctor"): "identity",
    ("GET", "/api/skills"): "identity",     # read-only listing of installed skills (front matter only); behind the same identity middleware as every /api read
    ("GET", "/api/memory/health"): "identity",
    ("GET", "/api/memory/{project}/observations"): "identity",
    ("GET", "/api/memory/{project}/summaries"): "identity",
    ("GET", "/api/memory/{project}/search"): "identity",
    ("GET", "/api/memory/{project}/timeline"): "identity",
    ("GET", "/api/memory/{project}/palace"): "identity",
    ("GET", "/api/memory/prefs"): "identity",
    ("PUT", "/api/memory/prefs"): "identity+csrf",
    ("POST", "/api/preflight/clone"): "identity+csrf",
    ("GET", "/api/sessions/{name}"): "identity",
    ("GET", "/api/external"): "identity",
    ("POST", "/api/external/{agent}/{sid}/open"): "identity+csrf",
    ("GET", "/api/series"): "identity",
    ("GET", "/api/series/events"): "identity",
    ("GET", "/api/usage/summary"): "identity",
    ("GET", "/api/accounts"): "identity",
    ("PATCH", "/api/accounts/{key}"): "identity+csrf",
    ("POST", "/api/accounts/login"): "identity+csrf",
    ("POST", "/api/accounts/login/code"): "identity+csrf",
    ("DELETE", "/api/accounts/login"): "identity+csrf",
    ("DELETE", "/api/accounts/problem"): "identity+csrf",
    ("POST", "/api/accounts/{key}/switch"): "identity+csrf",
    ("DELETE", "/api/accounts/{key}/saved"): "identity+csrf",
    ("GET", "/api/codex-accounts"): "identity",
    ("POST", "/api/codex-accounts/login"): "identity+csrf",
    ("DELETE", "/api/codex-accounts/login"): "identity+csrf",
    ("DELETE", "/api/codex-accounts/notice"): "identity+csrf",
    ("POST", "/api/codex-accounts/logout"): "identity+csrf",
    ("PATCH", "/api/codex-accounts/{key}"): "identity+csrf",
    ("POST", "/api/codex-accounts/{key}/switch"): "identity+csrf",
    ("DELETE", "/api/codex-accounts/{key}/saved"): "identity+csrf",
    ("POST", "/api/projects"): "identity+csrf",
    ("GET", "/api/github/repos"): "identity",
    ("POST", "/api/projects/{project}/repos/bulk"): "identity+csrf",
    ("POST", "/api/clone-queue/clear"): "identity+csrf",
    ("DELETE", "/api/projects/{project}"): "identity+csrf",
    ("POST", "/api/projects/{project}/repos"): "identity+csrf",
    ("DELETE", "/api/projects/{project}/repos/{repo}"): "identity+csrf",
    ("POST", "/api/projects/{project}/repos/{repo}/tasks"): "identity+csrf",
    ("POST", "/api/tasks"): "identity+csrf",
    ("GET", "/api/tasks"): "identity",
    ("GET", "/api/tasks/{tid}"): "identity",
    ("PATCH", "/api/tasks/{tid}"): "identity+csrf",
    ("DELETE", "/api/tasks/{tid}"): "identity+csrf",
    ("POST", "/api/tasks/{tid}/dispatch"): "identity+csrf",
    ("POST", "/api/tasks/{tid}/reopen"): "identity+csrf",
    ("POST", "/api/tasks/{tid}/detach"): "identity+csrf",
    ("POST", "/api/tasks/{tid}/close-session"): "identity+csrf",
    ("POST", "/api/tasks/{tid}/keep-open"): "identity+csrf",
    ("POST", "/api/projects/{project}/repos/{repo}/chains"): "identity+csrf",
    ("GET", "/api/tasks/{tid}/diff"): "identity",
    ("POST", "/api/tasks/{tid}/describe"): "identity+csrf",
    ("POST", "/api/tasks/{tid}/pr"): "identity+csrf",
    ("POST", "/api/tasks/{tid}/merge"): "identity+csrf",
    ("POST", "/api/tasks/{tid}/fix-ci"): "identity+csrf",
    ("POST", "/api/tasks/{tid}/refresh"): "identity+csrf",
    ("GET", "/api/projects/{project}/repos/{repo}/issues"): "identity",
    ("GET", "/api/projects/{project}/repos/{repo}/issues/{n:int}"): "identity",
    ("GET", "/api/tasks/{tid}/issue-comment"): "identity",
    ("POST", "/api/tasks/{tid}/issue-comment"): "identity+csrf",
    ("GET", "/api/projects/{project}/repos/{repo}/tree"): "identity",
    ("GET", "/api/projects/{project}/repos/{repo}/file"): "identity",
    ("GET", "/api/tasks/{tid}/ports"): "identity",
    ("POST", "/api/tasks/{tid}/preview"): "identity+csrf",
    ("DELETE", "/api/tasks/{tid}/preview"): "identity+csrf",
    ("POST", "/api/tasks/{tid}/archive"): "identity+csrf",
    ("POST", "/api/projects/{project}/repos/{repo}/jobs"): "identity+csrf",
    ("POST", "/api/batch"): "identity+csrf",
    ("POST", "/api/jobs/{jid}/run"): "identity+csrf",
    ("POST", "/api/jobs/{jid}/toggle"): "identity+csrf",
    ("POST", "/api/jobs/{jid}/acknowledge-fable"): "identity+csrf",
    ("DELETE", "/api/jobs/{jid}"): "identity+csrf",
    ("GET", "/api/runs/{rid}"): "identity",
    ("POST", "/api/runs/{rid}/resume"): "identity+csrf",
    ("POST", "/api/projects/{project}/repos/{repo}/sessions"): "identity+csrf",
    ("POST", "/api/sessions/{name}/keys"): "identity+csrf",
    ("POST", "/api/sessions/{name}/scroll"): "identity+csrf",
    ("GET", "/api/sessions/{name}/pane"): "identity",
    ("POST", "/api/sessions/{name}/resize"): "identity+csrf",
    ("POST", "/api/sessions/{name}/command"): "identity+csrf",
    ("POST", "/api/sessions/{name}/tune"): "identity+csrf",
    ("POST", "/api/sessions/{name}/flags"): "identity+csrf",
    ("POST", "/api/sessions/{name}/restart"): "identity+csrf",
    ("POST", "/api/sessions/{name}/prompt"): "identity+csrf",
    ("DELETE", "/api/sessions/{name}"): "identity+csrf",
    ("GET", "/api/stream"): "identity",
    ("POST", "/api/hook"): "hook-token",
    ("GET", "/api/push/vapid"): "identity",
    ("POST", "/api/push/subscribe"): "identity+csrf",
    ("DELETE", "/api/push/subscribe"): "identity+csrf",
    ("POST", "/api/push/test"): "identity+csrf",
    ("GET", "/api/notify/prefs"): "identity",
    ("PUT", "/api/notify/prefs"): "identity+csrf",
    ("POST", "/api/notify/test"): "identity+csrf",
    ("POST", "/mcp"): "identity+device-token",
    ("GET", "/api/mcp/tokens"): "identity",
    ("POST", "/api/mcp/tokens"): "identity+csrf",
    ("DELETE", "/api/mcp/tokens/{token_id}"): "identity+csrf",
    ("PUT", "/api/mcp/remote"): "identity+csrf",
    ("POST", "/api/deploy/gate"): "hook-token",
    ("POST", "/api/deploy/now"): "identity+csrf",
    ("POST", "/api/recovery/dismiss"): "identity+csrf",
    ("POST", "/api/backup/run"): "identity+csrf",
    ("GET", "/api/health"): "identity",
    ("GET", "/api/node/summary"): "hub-token|identity",
    ("GET", "/api/node/hello"): "none",         # the one route that answers with no identity (issue #133): three fixed keys, 30 per minute per source
    ("GET", "/api/node"): "identity",            # the node card; a paired node's token will open it too (issue #135)
    ("GET", "/api/search"): "identity",
    ("POST", "/api/cost/refresh"): "identity+csrf",
    ("POST", "/api/usage/rate-limit/clear"): "identity+csrf",
    ("POST", "/api/usage/refresh"): "identity+csrf",
    ("POST", "/api/permission"): "hook-token",
    ("POST", "/api/permission/{pid}/{decision}"): "identity+csrf",
    ("POST", "/api/sessions/{name}/ack"): "identity+csrf",
    ("POST", "/api/claude/login"): "identity+csrf",
    ("POST", "/api/claude/login/code"): "identity+csrf",
    ("POST", "/api/claude/logout"): "identity+csrf",
    ("GET", "/"): "identity",
    ("GET", "/term/{name}"): "identity",
    ("GET", "/sw.js"): "identity",
    ("GET", "/tty/"): "identity",          # mount: the dev harness's fake terminal (dev bypass only); the real /tty/ is ttyd's, never the board's
    ("GET", "/static/"): "identity",       # mount: StaticFiles
}
# Answered with no auth at all, by auth_middleware before any route (not in app.routes): the compose and Dockerfile healthcheck over loopback.
NO_AUTH = {("GET", "/healthz"): "a constant 'ok' for the container healthcheck; reads nothing, changes nothing"}
FILL = {"agent": "claude", "decision": "allow", "jid": "1", "key": "k", "n:int": "1", "name": "p--r--s", "pid": "1", "project": "p",
        "repo": "r", "rid": "1", "sid": UUID, "tid": "1", "token_id": "t"}
PROBE = {"/static/": "/static/core.js", "/tty/": "/tty/"}


def live_routes() -> dict:
    from app import main
    out = {}
    for r in main.app.routes:
        if isinstance(r, Mount):
            out[("GET", r.path + "/")] = "?"
        elif isinstance(r, APIRoute):
            for m in r.methods - {"HEAD"}:
                out[(m, r.path)] = "?"
        else:                                        # a plain Starlette route or websocket would bypass this table's reasoning
            out[("?", getattr(r, "path", repr(r)))] = "?"
    return out


def concrete(path: str) -> str:
    return PROBE.get(path) or re.sub(r"\{([^}]+)\}", lambda m: FILL[m.group(1)], path)


def send(c, method, path, headers=None):
    return c.request(method, concrete(path), headers=headers or {}, content=b"{}" if method != "GET" else None)


# ---------------------------------------------------------------- 1. the route table

def test_route_table_is_complete():
    live = set(live_routes())
    new, gone = sorted(live - set(EXPECTED)), sorted(set(EXPECTED) - live)
    assert not new, f"new route(s) {new}: add each to EXPECTED in tests/test_security_surface.py with the auth it needs, and say why if it needs none"
    assert not gone, f"route(s) {gone} are gone: drop them from EXPECTED"


def test_kinds_follow_the_method():
    """A non-GET route a person calls must be identity+csrf; only the token routes are exempt, and each is named."""
    for (method, path), kind in EXPECTED.items():
        if kind in ("hook-token", "identity+device-token", "hub-token|identity", "none"):
            continue
        assert kind == ("identity" if method == "GET" else "identity+csrf"), (method, path, kind)


def test_no_docs_or_openapi_listing():
    from app import main
    assert main.app.docs_url is None and main.app.redoc_url is None and main.app.openapi_url is None


@pytest.mark.parametrize("method,path", sorted(EXPECTED))
def test_every_route_refuses_a_request_without_identity(lite_client, method, path):
    kind = EXPECTED[(method, path)]
    r = send(lite_client, method, path)
    if kind == "identity+device-token":
        assert r.status_code == 404                  # off by default: the endpoint does not advertise itself
        return
    if kind == "none":
        assert r.status_code == 200 and set(r.json()) == {"app", "api", "node_id"}, (method, path)
        return
    assert r.status_code == 403, (method, path, r.status_code)
    want = "bad hook token" if kind == "hook-token" else NO_ID
    assert r.json()["error"] == want, (method, path)


@pytest.mark.parametrize("method,path", sorted(k for k, v in EXPECTED.items() if v == "identity+csrf"))
def test_every_non_get_route_needs_the_csrf_header(lite_client, method, path):
    for headers in (ID, {**ID, "X-CCBoard": "true"}, {**ID, "X-CCBoard": "0"}, {**ID, "Origin": "https://evil.example"}):
        r = send(lite_client, method, path, headers)
        assert r.status_code == 403, (method, path, headers)
        assert r.json()["error"] == NO_CSRF, (method, path)


@pytest.mark.parametrize("method,path", sorted(k for k, v in EXPECTED.items() if v == "hook-token"))
def test_hook_routes_take_only_the_hook_token(lite_client, method, path):
    for headers in (H, {**H, "X-CCBoard-Token": "nope"}, {"Authorization": "Bearer ccbmcp_x"}):
        r = send(lite_client, method, path, headers)
        assert r.status_code == 403, (path, headers)


def test_only_healthz_answers_without_auth(lite_client):
    r = lite_client.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"
    for path in ("/nope", "/api/nope", "/static/nope.js", "/docs", "/openapi.json", "/term/a--b--c", "/sw.js", "/"):
        assert lite_client.get(path).status_code == 403, path        # an unknown path does not even say it is unknown


def test_a_cors_preflight_is_refused_and_never_allowed(lite_client):
    r = lite_client.options("/api/tasks", headers={**ID, "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
                                                  "Access-Control-Request-Headers": "x-ccboard"})
    assert r.status_code == 403 and "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_pages_carry_the_csp(lite_client, fake_tmux):
    for path in ("/", "/term/a--b--c", "/static/term.html", "/static/index.html", "/sw.js"):
        r = lite_client.get(path, headers=ID)
        assert r.status_code == 200, path
        assert r.headers["content-security-policy"] == CSP and r.headers["x-content-type-options"] == "nosniff", path


def test_the_hook_token_opens_api_but_never_the_token_routes_or_mcp(lite_client):
    """F-A2 (accepted): the hook token is local automation's key to /api/* (no identity, no X-CCBoard); it never mints, lists or
    revokes device tokens, never flips /mcp, and is refused at /mcp."""
    from app import hooks
    tok = {"X-CCBoard-Token": hooks.ensure_token()}
    assert lite_client.get("/api/state", headers=tok).status_code == 200
    assert lite_client.get("/api/mcp/tokens", headers=tok).status_code == 403
    assert lite_client.post("/api/mcp/tokens", headers=tok, json={"name": "x"}).status_code == 403
    assert lite_client.put("/api/mcp/remote", headers=tok, json={"enabled": True}).status_code == 403
    assert lite_client.post("/mcp", headers={**ID, **tok}, json={}).status_code == 404       # off; on, it is a 403 (tests/test_mcp_remote.py)


# ---------------------------------------------------------------- 2. regression tests for the fixed findings

@pytest.mark.parametrize("header", ["X-CCBoard-Token", "X-CCBoard-Hub-Token"])
def test_f02_a_non_ascii_token_header_is_refused_not_a_500(lite_client, monkeypatch, header):
    """F-02: hmac.compare_digest raises TypeError on a non-ASCII str, so a latin-1 byte in the hook or hub token header was a 500 with a
    traceback for an unauthenticated caller. It is a plain 403 now."""
    from app import health
    from app.config import settings
    monkeypatch.setattr(settings, "hub_token", "hub-secret")
    name = health.HUB_HEADER if header == "X-CCBoard-Hub-Token" else header
    path = "/api/node/summary" if header == "X-CCBoard-Hub-Token" else "/api/state"
    for path_ in (path, "/api/hook") if header == "X-CCBoard-Token" else (path,):
        r = lite_client.request("POST" if path_ == "/api/hook" else "GET", path_, headers={name: b"\xe9t\xe9"}, content=b"{}")
        assert r.status_code == 403, (path_, r.status_code)


def test_f02_token_checks_never_raise():
    from app import health, hooks
    for given in ("é", "\udce9", "x" * 5000, " ", "", None):
        assert hooks.check_token(given) is False
        assert health.check_hub_token(given) is False


@pytest.mark.parametrize("bad", ["shop\n", "shop\r", "a\n", "\nshop"])
def test_f03_names_with_a_trailing_newline_are_refused(lite_client, projects_dir, bad):
    """F-03: NAME_RE ended in '$', which also matches before a trailing newline, so 'shop\\n' was a valid project, repo or session name and
    POST /api/projects made a directory whose name ends in a newline."""
    from app import projects, tmux
    assert tmux.valid_name(bad) is False
    with pytest.raises(ValueError):
        tmux.split_name(f"{bad}--r--s")
    r = lite_client.post("/api/projects", headers=H, json={"name": bad})
    assert r.status_code == 400
    assert sorted(p.name for p in projects_dir.iterdir()) == []
    with pytest.raises(projects.BadRequest):
        projects.check_name("repo", bad)


def test_f03_directory_naming_regexes_refuse_a_trailing_newline():
    from app import codex_accounts
    from app.agents import claude
    assert not claude.WORKTREE_RE.match("wt\n") and claude.WORKTREE_RE.match("wt")
    assert not codex_accounts.SLOT_RE.match("0" * 24 + "\n") and codex_accounts.SLOT_RE.match("0" * 24)


def _preview_world(c, projects_dir, monkeypatch):
    from app import previews
    from app.config import settings
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(settings, "public_url", "https://box.example.ts.net:8443")
    calls = []
    monkeypatch.setattr(previews, "_run_serve", lambda args: calls.append(args))
    t = c.post("/api/projects/shop/repos/api/tasks", headers=H, json={"title": "ui", "prompt": "p"}).json()
    return t, calls


def test_f01_a_preview_never_publishes_the_boards_own_ports(lite_client, projects_dir, fake_tmux, monkeypatch):
    """F-01: POST /api/tasks/{id}/preview {port} put any loopback port on a new tailnet HTTPS port, the board's own services included:
    the board, ttyd (a shell), code-server, ntfy and the claude-mem worker (no sign-in, writable API). Those are refused now; a dev
    server's port still works."""
    from app import memory, previews
    from app.config import settings
    monkeypatch.setattr(settings, "ntfy_url", "http://127.0.0.1:2586")
    monkeypatch.setattr(memory, "discover", lambda: {"host": "127.0.0.1", "port": 37777, "source": "pid"})
    t, calls = _preview_world(lite_client, projects_dir, monkeypatch)
    for port in (settings.port, settings.ttyd_port, settings.code_server_port, 2586, 37777):
        r = lite_client.post(f"/api/tasks/{t['id']}/preview", headers=H, json={"port": port})
        assert r.status_code == 400, (port, r.text)
        assert "board's own" in r.json()["error"]
    assert calls == []
    assert previews.infra_ports() >= {settings.port, settings.ttyd_port, settings.code_server_port, 2586, 37777}
    r = lite_client.post(f"/api/tasks/{t['id']}/preview", headers=H, json={"port": 5173})
    assert r.status_code == 200 and calls[-1] == ["--bg", "--https=9100", "http://127.0.0.1:5173"]


def test_f01_preview_https_ports_skip_443_and_the_boxs_other_tailnet_ports(monkeypatch):
    """F-01: allocate_https_port reserved only the board's and code-server's HTTPS ports. 443 (another service's Funnel on the owner's box)
    and the ntfy and claude-mem viewer ports are never handed out either, whatever PREVIEW_HTTPS_BASE says."""
    from app import previews
    from app.config import settings
    monkeypatch.setattr(settings, "preview_https_base", 443)
    monkeypatch.setattr(settings, "ccboard_https_port", 8443)
    monkeypatch.setattr(settings, "code_https_port", 444)
    monkeypatch.setattr(settings, "mem_https_port", 446)
    monkeypatch.setattr(settings, "ntfy_https_port", 445)
    assert previews.allocate_https_port(set()) == 447
    monkeypatch.setattr(settings, "preview_https_base", 8443)
    monkeypatch.setattr(settings, "ntfy_https_port", 8444)
    assert previews.allocate_https_port(set()) == 8445                # 8443 is the board's, 8444 ntfy's default


def test_f04_keys_text_refuses_every_control_character(lite_client, projects_dir, fake_tmux):
    """F-04: /keys refused ord < 32 only, while /prompt and dispatch refuse or drop every Cc character; DEL and the C1 range (U+0080 to
    U+009F, U+009B is CSI in 8-bit terminals) passed. Not a breakout (tmux sends them UTF-8 encoded), but one rule for every typing route."""
    from app import tmux
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)
    name = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]
    for bad in ("ls\x7f", "a\x9b201~b", "\x1b[201~", "x\x00"):
        r = lite_client.post(f"/api/sessions/{name}/keys", headers=H, json={"text": bad})
        assert r.status_code == 400, repr(bad)
    assert fake_tmux["texts"] == [] and fake_tmux["pasted"] == []
    ok = lite_client.post(f"/api/sessions/{name}/keys", headers=H, json={"text": "echo hi\tthere\nnext", "enter": True})
    assert ok.status_code == 200 and fake_tmux["pasted"][-1][1] == "echo hi\tthere\nnext"
    assert tmux.KEY_ALLOW >= {"Enter", "C-c"} and not any(k.startswith(("M-", "F")) for k in tmux.KEY_ALLOW)


def test_f05_backup_extra_never_takes_the_live_logins(projects_dir, tmp_path, monkeypatch):
    """F-05: _holds_saved_logins guarded only <data dir>/accounts and codex-accounts, so CCBOARD_BACKUP_EXTRA=<Claude config dir> or
    <CODEX_HOME> put the live .credentials.json or auth.json into the restic snapshot, against its own docstring."""
    from app import backup
    from app.config import settings
    claude_dir, codex_dir = tmp_path / "claude", tmp_path / "codex"
    for d in (claude_dir, codex_dir):
        d.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "claude_config_dir", claude_dir)
    monkeypatch.setattr(settings, "codex_home", codex_dir)
    for p in (claude_dir, codex_dir, claude_dir / ".credentials.json", codex_dir / "auth.json", tmp_path):
        assert backup._holds_saved_logins(p) is True, p
    for p in (claude_dir / "projects", codex_dir / "sessions", projects_dir / "shop"):
        assert backup._holds_saved_logins(p) is False, p


def test_f06_startup_log_names_no_login(projects_dir):
    """F-06: the startup line printed CCBOARD_ALLOWED_USERS (logins, usually emails) into the journal / docker logs."""
    from app import main
    from app.config import settings
    line = main.startup_line()
    assert "alice@example.com" not in line and "1 allowed login" in line
    assert settings.allowed_users == {"alice@example.com"}


def test_f07_claude_extra_args_refuse_a_bare_double_dash():
    """F-07: Codex refused a bare `--` in extra args, Claude did not: `claude -- --session-id <id> --name s -- hi` made the board's own
    session id a prompt word, so the hooks of that session never matched its row."""
    from app import agents, projects
    from app.agents.base import LaunchReq
    ag = agents.get("claude")
    for interactive, task in ((True, False), (True, True), (False, False)):
        assert ag.forbidden_extra(["--model", "opus", "--"], interactive=interactive, task=task) == "--"
    with pytest.raises(projects.BadRequest, match="--"):
        ag.launch_plan(LaunchReq(kind="new", session_name="s", cwd="/x", opts={"extra": "--"}, prompt="hi"))
    assert ag.forbidden_extra(["--model", "opus", "--verbose"], interactive=True) is None
    # a task prompt whose first word is `--` is still text, never a refusal; a flag-shaped first word still is
    p = ag.launch_plan(LaunchReq(kind="new", session_name="s", cwd="/x", prompt="-- plain words", task=True, worktree="wt"))
    assert p.argv[-2:] == ["--", "-- plain words"]
    with pytest.raises(projects.BadRequest, match="dangerously"):
        ag.launch_plan(LaunchReq(kind="new", session_name="s", cwd="/x", prompt="--dangerously-skip-permissions please", task=True, worktree="wt"))


# ---------------------------------------------------------------- validators: spellings the existing suites do not list

@pytest.mark.parametrize("token", ["--permission-mode=BYPASSPERMISSIONS", "--Dangerously-Skip-Permissions", "--allow-dangerously-skip-permissions",
                                   "bypassPermissions", "--SETTINGS", "--settings={\"hooks\":{}}", "--setting-sources=local", "--permission-prompt-tool=x"])
def test_claude_task_extra_args_cannot_loosen_permissions(token):
    from app import agents
    ag = agents.get("claude")
    assert ag.forbidden_extra(["--model", "opus", token], interactive=True, task=True) == token
    assert ag.forbidden_extra(["--model", "opus", token], interactive=False) == token


@pytest.mark.parametrize("token", ["-csandbox_mode=danger-full-access", "--config=approval_policy=never", "--sandbox=danger-full-access",
                                   "--ask-for-approval=never", "-anever", "--dangerously-bypass-approvals-and-sandbox", "--YOLO",
                                   "--profile=x", "--enable=hooks", "--remote-auth=x", "--", "-pwork", "--full-auto", "--Config=a=b"])
def test_codex_extra_args_cannot_loosen_sandbox_or_approval(token):
    from app import agents
    ag = agents.get("codex")
    for interactive, task in ((True, False), (True, True), (False, False)):
        assert ag.forbidden_extra(["--oss", token], interactive=interactive, task=task) == token, (token, interactive, task)


@pytest.mark.parametrize("line", ["Sandbox_Mode=\"danger-full-access\"", "APPROVAL_POLICY=never", "approvals_reviewer=x", "sandbox_workspace_write.network_access=true",
                                  "mcp_servers.x.command=sh", "MCP_SERVERS.x.command=sh", "hooks.stop=x", "features.hooks=false", "features.codex_hooks=false",
                                  "notify=[\"sh\"]", "model_provider=x", "model_providers.x.base_url=http://h", "openai_base_url=http://h",
                                  "chatgpt_base_url=http://h", "cli_auth_credentials_store=file", "profiles.x.sandbox_mode=danger-full-access",
                                  "projects.x.trust_level=trusted", "permissions.x=y", "default_permissions=x", "features.yolo=true", "x.bypass=1",
                                  "sandbox_mode =danger-full-access", "approval_policy=never\nmodel=x"])
def test_codex_config_lines_cannot_loosen_anything(line):
    from app import agents, projects
    ag = agents.get("codex")
    with pytest.raises(projects.BadRequest):
        ag.config_lines([line])
    assert ag.config_lines(["tui.theme=dark"]) == ["tui.theme=dark"]


# ---------------------------------------------------------------- 3. log hygiene

SECRET = "SECRETPASS123"


def _scan(blob: str, needles: dict) -> list:
    return [what for what, n in needles.items() if n and n in blob]


@pytest.fixture
def debug_logs(caplog):
    """DEBUG capture from before the board starts: requested ahead of `client`, so the lifespan's own lines are in the setup records."""
    caplog.set_level(logging.DEBUG)
    return caplog


def test_log_hygiene_no_token_digest_password_or_login(debug_logs, client, projects_dir, fake_tmux, monkeypatch):
    """A representative slice under DEBUG capture: the full startup (the client fixture's lifespan), the remote MCP switch, a device token
    minted, used, refused and revoked, the hook token right and wrong, a hook event, clone URLs that carry a password. Nothing in the log,
    in /api/state or in the token listing may hold a token, a digest, the password or the login; the database holds no raw token."""
    from app import hooks
    caplog = debug_logs
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)

    assert client.put("/api/mcp/remote", headers=H, json={"enabled": True}).status_code == 200
    r = client.post("/api/mcp/tokens", headers=H, json={"name": "laptop"})
    assert r.status_code == 201
    dev_token, rec = r.json()["token"], r.json()["record"]
    digest = hashlib.sha256(dev_token.encode()).hexdigest()
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}}}
    assert client.post("/mcp", headers={**ID, "Authorization": f"Bearer {dev_token}"}, json=init).status_code == 200
    lst = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_tasks", "arguments": {}}}
    assert client.post("/mcp", headers={**ID, "Authorization": f"Bearer {dev_token}"}, json=lst).status_code == 200
    assert client.post("/mcp", headers={**ID, "Authorization": f"Bearer {dev_token}x"}, json=init).status_code == 401
    assert client.get("/api/state", headers={**ID, "Authorization": f"Bearer {dev_token}"}).status_code == 403

    hook_token = hooks.ensure_token()
    assert client.get("/api/state", headers={"X-CCBoard-Token": hook_token + "x"}).status_code == 403
    assert client.get("/api/state", headers={"X-CCBoard-Token": hook_token}).status_code == 200
    client.post("/api/hook", headers={"X-CCBoard-Token": hook_token, "X-CCBoard-Event": "Notification"},
                content=json.dumps({"hook_event_name": "Notification", "session_id": UUID, "message": "hi"}))

    for url in (f"https://user:{SECRET}@127.0.0.1/x.git", f"https://user:{SECRET}@localhost/x.git", f"ssh://git:{SECRET}@example.com/x.git"):
        r = client.post("/api/projects", headers=H, json={"name": "clonetest", "url": url})
        assert r.status_code == 400 and SECRET not in r.text, url

    state = client.get("/api/state", headers=H).text
    listing = client.get("/api/mcp/tokens", headers=H).text
    assert client.delete(f"/api/mcp/tokens/{rec['id']}", headers=H).status_code in (200, 204)

    needles = {"device token": dev_token, "device token digest": digest, "hook token": hook_token, "clone password": SECRET,
               "the login": "alice@example.com"}
    records = caplog.get_records("setup") + caplog.records
    logs = "\n".join(f"{r.name} {r.levelname} {r.getMessage()}" for r in records)
    assert any("ccboard on" in r.getMessage() for r in records), "the startup line was not captured: the lifespan did not run under capture"
    assert _scan(logs, needles) == [], "a secret or the login reached the log"
    assert _scan(state, {k: v for k, v in needles.items() if k != "the login"}) == [], "a secret reached /api/state"
    assert _scan(listing, {k: v for k, v in needles.items() if k not in ("the login",)}) == [], "a secret reached the token listing"
    from app.config import settings
    con = sqlite3.connect(str(settings.db_path))
    try:
        dump = "\n".join(con.iterdump())
    finally:
        con.close()
    assert dev_token not in dump and hook_token not in dump and SECRET not in dump
