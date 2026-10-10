"""The terminal dev harness: scripts/dev/fake_tty is the board's /tty/ ONLY under the dev bypass (CCBOARD_DEV_BYPASS_USER).

With it, the board serves scripts/dev/fake_tty/ at /tty/ (a page with a stub ttyd `window.term`, so scripts/qa_terminal.sh can drive
the terminal page without tmux or ttyd) and the CSP middleware leaves paths under /tty/ alone, as ttyd's own pages are not ours.
Without it /tty is not served by the board at all (on the box tailscale serve maps /tty to ttyd, never to this app) and every response
keeps the CSP.

Whether the mount is decided at import or per request is the backend's choice, so the positive case runs the way the dev board runs:
a fresh interpreter whose environment sets (or lacks) CCBOARD_DEV_BYPASS_USER, one request batch through TestClient (no lifespan: the
static and routing layers need no database).
"""
import http.server
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAKE = ROOT / "scripts" / "dev" / "fake_tty"
CSP = "default-src 'self'; frame-ancestors 'none'"
IDENTITY = {"Tailscale-User-Login": "alice@example.com"}

PROBE = r"""
import json, sys
from fastapi.testclient import TestClient
from app import main
paths, headers = json.loads(sys.argv[1]), json.loads(sys.argv[2])
c = TestClient(main.app)
out = {}
for p in paths:
    r = c.get(p, headers=headers, follow_redirects=False)
    out[p] = {"status": r.status_code, "csp": r.headers.get("content-security-policy"), "type": r.headers.get("content-type", ""),
              "nosniff": r.headers.get("x-content-type-options"), "location": r.headers.get("location"), "body": r.text[:60000]}
print("PROBE" + json.dumps(out))
"""

TTY_PATHS = ["/tty/", "/tty/index.html", "/tty/fake_tty.js", "/tty/nope.js"]
OTHER_PATHS = ["/", "/static/core.js", "/static/termkit.js", "/static/term.css", "/sw.js", "/term/ccboard--ccboard--s1", "/nope", "/ttyx/", "/ttyx", "/tty-x/fake_tty.js"]


def probe(tmp_path, paths, bypass, headers):
    env = {k: v for k, v in os.environ.items() if not k.startswith("CCBOARD_") and k not in ("INVOCATION_ID", "PROJECTS_DIR", "CODE_SERVER_PORT")}
    env.update({"PROJECTS_DIR": str(tmp_path / "projects"), "CCBOARD_DATA_DIR": str(tmp_path / "data"), "CCBOARD_RUNTIME": "host",
                "CCBOARD_ALLOWED_USERS": "alice@example.com", "PYTHONDONTWRITEBYTECODE": "1"})
    if bypass:
        env["CCBOARD_DEV_BYPASS_USER"] = "dev"
    r = subprocess.run([sys.executable, "-c", PROBE, json.dumps(paths), json.dumps(headers)], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=90)
    assert r.returncode == 0, f"probe failed (bypass={bypass}):\n{r.stdout[-2000:]}\n{r.stderr[-3000:]}"
    line = next(l for l in r.stdout.splitlines() if l.startswith("PROBE"))
    return json.loads(line[len("PROBE"):])


@pytest.fixture(scope="module")
def dev_board(tmp_path_factory):
    """Responses of a board started WITH the dev bypass; the request carries no identity header at all (the bypass supplies one)."""
    return probe(tmp_path_factory.mktemp("dev"), TTY_PATHS + OTHER_PATHS, bypass=True, headers={})


@pytest.fixture(scope="module")
def prod_board(tmp_path_factory):
    """Responses of a board started WITHOUT the bypass, for an allowed Tailscale user."""
    return probe(tmp_path_factory.mktemp("prod"), TTY_PATHS + OTHER_PATHS, bypass=False, headers=IDENTITY)


# ---------- the fake page itself ----------

def test_fake_tty_files_exist():
    assert FAKE.is_dir(), "scripts/dev/fake_tty/ is missing"
    assert (FAKE / "index.html").is_file() and (FAKE / "fake_tty.js").is_file()


def test_fake_tty_page_loads_its_script_from_the_same_directory_and_has_no_inline_code():
    html = (FAKE / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script\b([^>]*)>", html, re.I)
    assert scripts, "index.html has no <script>"
    srcs = [m.group(1) for s in scripts if (m := re.search(r"""\bsrc\s*=\s*["']([^"']+)["']""", s))]
    assert len(srcs) == len(scripts), "every <script> has a src (the page is served without a CSP, but stays CSP-clean anyway)"
    assert any(s.rsplit("/", 1)[-1] == "fake_tty.js" and not re.match(r"[a-z]+:|//", s) for s in srcs), srcs
    assert re.search(r"<meta[^>]+viewport", html, re.I), "a viewport meta, like ttyd's page, so the iframe lays out at the phone width"


def test_fake_tty_script_defines_the_ttyd_surface_the_terminal_page_uses():
    """What TermKit.bind reads from the iframe: window.term with fit() and options.fontSize, an .xterm > .xterm-screen to put the
    touch-to-wheel shim on and a .xterm-helper-textarea for focus; plus the counters scripts/qa_terminal.sh reads."""
    js = (FAKE / "fake_tty.js").read_text(encoding="utf-8")
    for needle in ("window.term", "fit", "__fits", "options", "fontSize", "cols", "rows", "xterm-screen", "xterm-helper-textarea",
                   "__wheel", "__lastDelta"):
        assert needle in js, f"fake_tty.js never mentions {needle}"
    assert re.search(r"['\"]wheel['\"]", js), "fake_tty.js counts 'wheel' events on the screen"
    assert re.search(r"\bfontSize\s*:\s*13\b", js) and re.search(r"\bcols\s*:\s*80\b", js) and re.search(r"\brows\s*:\s*24\b", js), \
        "window.term = { fit, options: { fontSize: 13 }, cols: 80, rows: 24 }"


def test_fake_tty_script_has_the_font_spike_surface():
    """The v0.5.8 font spike (TermKit.bind with ccboard:term:font=1) needs what ttyd's page has: window.FontFace, document.fonts and
    term.options.fontFamily, measured into cols and rows by fit(); the recorder and the modes are what scripts/qa_terminal.sh drives."""
    js = (FAKE / "fake_tty.js").read_text(encoding="utf-8")
    for needle in ("FontFace", "document.fonts", "fontFamily", "__font", "__fitsAtFamily", "ccboard:fake:font", "measureText"):
        assert needle in js, f"fake_tty.js never mentions {needle}"
    for mode in ("stub", "fail", "hang", "missing"):
        assert re.search(rf"""['"]{mode}['"]""", js), f"fake_tty.js has no '{mode}' font mode"
    assert "Consolas,Liberation Mono,Menlo,Courier,monospace" in js, "the default family is ttyd's own, so 'unchanged' means something"
    assert not re.search(r"\burl\s*\(", js), "the fake never names a font URL itself: the spike passes the vendored woff2, the stand-in only records it"


def test_fake_tty_script_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    r = subprocess.run([node, "--check", str(FAKE / "fake_tty.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---------- with the dev bypass: /tty/ is the fake and carries no CSP ----------

def test_dev_bypass_serves_the_fake_tty_page(dev_board):
    r = dev_board["/tty/"]
    assert r["status"] == 200, r
    assert r["type"].startswith("text/html"), r["type"]
    assert "fake_tty.js" in r["body"]
    assert r["body"] == (FAKE / "index.html").read_text(encoding="utf-8"), "/tty/ is scripts/dev/fake_tty/index.html (StaticFiles html=True)"
    assert dev_board["/tty/index.html"]["status"] in (200, 307, 308), "index.html itself is either served or redirected to the directory URL"


def test_dev_bypass_serves_the_fake_script(dev_board):
    r = dev_board["/tty/fake_tty.js"]
    assert r["status"] == 200, r
    assert "javascript" in r["type"], r["type"]
    assert r["body"] == (FAKE / "fake_tty.js").read_text(encoding="utf-8")


def test_dev_bypass_tty_responses_carry_no_csp(dev_board):
    """The CSP is `default-src 'self'` with no inline code: the stub page is a test double for ttyd's page, which is not ours."""
    for path in ("/tty/", "/tty/fake_tty.js"):
        assert dev_board[path]["csp"] is None, f"{path} must not carry a Content-Security-Policy in dev-bypass mode"
    assert dev_board["/tty/nope.js"]["status"] == 404


def test_dev_bypass_keeps_the_csp_on_everything_outside_tty(dev_board):
    """The skip is for paths UNDER /tty/ only: neighbours that merely start with the same letters keep the policy."""
    for path in OTHER_PATHS:
        r = dev_board[path]
        assert r["csp"] == CSP, f"{path} lost its CSP in dev-bypass mode (status {r['status']})"
        assert r["nosniff"] == "nosniff", path
    assert dev_board["/"]["status"] == 200 and dev_board["/term/ccboard--ccboard--s1"]["status"] == 200, "the bypass identified the (header-less) request"


# ---------- without it: /tty is not the board's business ----------

def test_without_the_bypass_tty_is_404_and_still_has_the_csp(prod_board):
    for path in TTY_PATHS:
        r = prod_board[path]
        assert r["status"] == 404, f"{path} answered {r['status']}: without the dev bypass the board does not serve /tty at all"
        assert r["csp"] == CSP, f"{path}: a 404 under /tty/ keeps the CSP when the bypass is off"
        assert "fake_tty" not in r["body"]


def test_without_the_bypass_every_response_keeps_the_csp(prod_board):
    for path in OTHER_PATHS:
        r = prod_board[path]
        assert r["csp"] == CSP, f"{path} (status {r['status']}) has no CSP"
        assert r["nosniff"] == "nosniff", path
    assert prod_board["/"]["status"] == 200 and prod_board["/static/termkit.js"]["status"] == 200


def test_without_the_bypass_the_terminal_assets_are_not_dev_only(prod_board):
    assert prod_board["/term/ccboard--ccboard--s1"]["status"] == 200
    assert "fake_tty" not in prod_board["/term/ccboard--ccboard--s1"]["body"]


def test_without_the_bypass_and_without_identity_nothing_is_served(tmp_path):
    out = probe(tmp_path, ["/tty/", "/tty/fake_tty.js", "/"], bypass=False, headers={})
    assert all(r["status"] == 403 for r in out.values()), {p: r["status"] for p, r in out.items()}


def test_in_process_board_has_no_tty_route(lite_client):
    """pytest's own board (conftest clears the bypass): the quick form of the same guarantee."""
    if os.environ.get("CCBOARD_DEV_BYPASS_USER"):
        pytest.skip("the shell that started pytest exports CCBOARD_DEV_BYPASS_USER, so the imported app may mount /tty")
    for path in ("/tty/", "/tty/fake_tty.js"):
        r = lite_client.get(path, headers=IDENTITY)
        assert r.status_code == 404, path
        assert r.headers["Content-Security-Policy"] == CSP, path
    assert lite_client.get("/tty/").status_code == 403


# ---------- scripts/qa_terminal.sh: the v0.5.8 assertions, and the script itself run against a stub board with a browser that sees nothing ----------

QA = ROOT / "scripts" / "qa_terminal.sh"

#: a browse CLI with no browser: it logs every `js` snippet (NUL separated) and answers just enough for qa_terminal.sh to walk its branches
FAKE_BROWSE = r"""#!/usr/bin/env bash
LOG="${FAKE_BROWSE_LOG:?}"
VP="${LOG}.vp"
case "${1:-}" in
  js)
    printf '%s' "$2" >> "$LOG"; printf '\0' >> "$LOG"
    case "$2" in
      "1 + 1") echo 2 ;;
      "window.innerWidth") cat "$VP" 2>/dev/null || echo 390 ;;
      *"return 'ok'"*|*": 'ok'"*) echo ok ;;
      *) echo 1 ;;
    esac ;;
  viewport) printf '%s' "${2%x*}" > "$VP" ;;
  screenshot) echo png > "$2" ;;
esac
exit 0
"""

NODE_PARSE = r"""
const vm = require('vm');
const snippets = require('fs').readFileSync(process.argv[1], 'utf8').split('\0').filter((s) => s.trim());
let bad = 0;
snippets.forEach((s, i) => { try { new vm.Script(s); } catch (e) { bad += 1; console.log('SYNTAX #' + i + ': ' + e.message + '\n' + s.slice(0, 400)); } });
console.log('SNIPPETS ' + snippets.length + ' BAD ' + bad);
"""


class _StubBoard(http.server.BaseHTTPRequestHandler):
    """/ answers 200, /tty/ is the fake tty's page or a ttyd-looking one (class attribute), /api/ is a 404 (no session on this board)."""
    tty_is_fake = True
    sandboxed = True            # /api/state's dev.sandboxed (issue #100): the QA scripts refuse a board that does not say true

    def do_GET(self):
        if self.path.startswith("/tty/"):
            body = b'<script src="fake_tty.js"></script>' if self.tty_is_fake else b"<html>ttyd</html>"
            code = 200
        elif self.path.startswith("/api/state"):
            body, code = (b'{"dev": {"sandboxed": true}}' if self.sandboxed else b"{}"), 200
        elif self.path.startswith("/api/"):
            body, code = b"{}", 404
        else:
            body, code = b"ok", 200
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def run_qa(tmp_path, fake_tty, env_extra=None, width="390", sandboxed=True):
    """Run the script once against a stub board and the blind browser: (CompletedProcess, [snippets]). Not a verdict about the page (that
    is the reviewer's browser run); it proves every JS snippet the script sends parses, that no branch dies on `set -u` or a typo, and
    which checks each mode runs."""
    for tool in ("bash", "curl", "python3", "node"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} is not installed")
    handler = type("Board", (_StubBoard,), {"tty_is_fake": fake_tty, "sandboxed": sandboxed})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        browse = tmp_path / "fakebrowse"
        browse.write_text(FAKE_BROWSE, encoding="utf-8")
        browse.chmod(0o755)
        log = tmp_path / "js.log"
        env = dict(os.environ, B=str(browse), FAKE_BROWSE_LOG=str(log), QA_WIDTHS=width, QA_SETTLE="0", QA_TICK="0.01", QA_START_WAIT="0.02",
                   QA_TMUX="qa--terminal--s1")
        for k in ("QA_REAL_TTYD", "QA_FONT", "QA_FONT_W", "QA_HEADER", "QA_TMUX_CREATE", "QA_SHARED_BROWSER"):
            env.pop(k, None)
        env.update(env_extra or {})
        r = subprocess.run(["bash", str(QA), f"http://127.0.0.1:{srv.server_address[1]}", str(tmp_path / "out")], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=240)
    finally:
        srv.shutdown()
    snippets = [x for x in (log.read_bytes().decode("utf-8", "replace").split("\0") if log.exists() else []) if x.strip()]
    return r, snippets


def assert_snippets_parse(tmp_path, snippets):
    path = tmp_path / "snippets.bin"
    path.write_bytes("\0".join(snippets).encode("utf-8"))
    out = subprocess.run(["node", "-e", NODE_PARSE, str(path)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert re.search(r"SNIPPETS \d+ BAD 0\b", out.stdout), out.stdout[-2000:]


def test_qa_terminal_script_is_valid_bash():
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is not installed")
    r = subprocess.run([bash, "-n", str(QA)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_qa_terminal_script_carries_the_v058_assertions_and_the_real_ttyd_mode():
    text = QA.read_text(encoding="utf-8")
    for needle in ("QA_REAL_TTYD", "QA_FONT", "#tune", "dialog.readout", "dialog.qr-editor", "Edit quick replies", "window.prompt",
                   "ccboard:term:font", "ccboard:fake:font", "TermKit.fontState", "confirm:true", "Escape", "queue", "44",
                   "TUNE QUICK FONT KBD", "ccboard:fake:vv", "ccboard:term:tune", ".tune-toggle", "more-r", "document.body.scrollWidth", "poke out of #tune"):
        assert needle in text, f"qa_terminal.sh never mentions {needle}"
    # the ultracode switch is `/effort ultracode on` (term.js EFFORT_ARG, claude.py V19): the script asserts the argument the page really sends, and says the two retire together
    assert "fake_cmd_count effort 'ultracode on'" in text, "the TUNE check waits for arg 'ultracode on', not 'ultracode'"
    assert "fake_cmd_count effort ultracode" not in text
    assert re.search(r"retire\s+TOGETHER", text), "the script says the EFFORT_ARG mapping and this assertion retire together"
    assert re.search(r"window\.prompt\s*=", text) or "window.prompt = boom" in text, "the script replaces window.prompt before the click"


def test_qa_terminal_fake_mode_walks_every_branch_and_every_snippet_parses(tmp_path):
    r, snippets = run_qa(tmp_path, fake_tty=True)
    assert r.returncode == 1, f"the blind browser fails assertions, so the script exits 1, not {r.returncode}:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}"
    for bad in ("unbound variable", "syntax error", "command not found", "bad substitution", "unexpected EOF"):
        assert bad not in r.stderr, r.stderr[-2000:]
    assert "mode=fake-tty" in r.stdout
    header = next(l for l in r.stdout.splitlines() if l.startswith("VIEWPORT"))
    assert header.split()[11:17] == ["COMPACT", "TUNE", "QUICK", "FONT", "KBD", "CONSOLE"], header
    assert "font spike at 390px wide" in r.stdout
    assert len(snippets) > 150, len(snippets)
    assert_snippets_parse(tmp_path, snippets)
    joined = "\n".join(snippets)
    for needle in ("window.__qaFake", "#tune button", "dialog.readout[open]", "dialog.qr-editor[open]", "window.prompt = boom", "visibilitychange",
                   "x.body.arg === 'ultracode on'", "#headtools .tune-toggle", "localStorage.setItem('ccboard:fake:vv', '520')", "document.body.classList.contains('kbd')", ".qr-save",
                   "Edit quick replies", "localStorage.setItem('ccboard:term:font', '1')", "localStorage.setItem('ccboard:fake:font', 'stub')",
                   "localStorage.setItem('ccboard:fake:font', 'fail')", "localStorage.setItem('ccboard:fake:font', 'missing')", "TermKit.fontState", "__wheel"):
        assert needle in joined, f"no snippet contains {needle}"


def test_qa_terminal_real_ttyd_mode_skips_the_fake_only_checks_and_runs_the_on_box_font_numbers(tmp_path):
    r, snippets = run_qa(tmp_path, fake_tty=False, env_extra={"QA_REAL_TTYD": "1", "QA_FONT": "1"})
    assert r.returncode == 1, f"{r.returncode}:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}"
    for bad in ("unbound variable", "syntax error", "command not found", "bad substitution", "unexpected EOF"):
        assert bad not in r.stderr, r.stderr[-2000:]
    assert "mode=real-ttyd" in r.stdout
    rows = [l.split() for l in r.stdout.splitlines() if l.startswith("390 ")]
    assert rows and rows[0][8] == "skip", f"the touch check needs the fake's wheel counter and is skipped on a real ttyd: {rows}"
    assert_snippets_parse(tmp_path, snippets)
    joined = "\n".join(snippets)
    assert "window.__qaFake" not in joined, "nothing is faked on a real board: no stubbed fetch, nothing typed into a real session"
    assert "__wheel" not in joined.replace("window.__wheel", ""), "the wheel counter belongs to the fake"
    assert not re.search(r"setItem\('ccboard:fake:font', '[a-z]+'\)", joined), "no fake font mode is ever set against a real ttyd"
    assert "ccboard:fake:vv" not in joined, "no soft-keyboard shim against a real ttyd either (it is the fake tty's)"
    assert rows[0][15] == "skip", f"the KBD column (the fake tty's soft-keyboard shim) is skipped on a real ttyd: {rows[0]}"
    assert "localStorage.setItem('ccboard:term:font', '1')" in joined, "QA_FONT=1 runs the on-box font spike"
    assert "real ttyd, flag off" in r.stdout
    for keep in ("#keyhost .kb-key", "#rail .rail-btn", "#ttywrap", "dialog.qr-editor[open]"):
        assert keep in joined, f"the real-ttyd run still checks {keep}"


def test_qa_terminal_real_mode_refuses_the_fake_tty_and_fake_mode_refuses_a_real_one(tmp_path):
    r, _ = run_qa(tmp_path, fake_tty=True, env_extra={"QA_REAL_TTYD": "1"})
    assert r.returncode == 2 and "is the fake tty" in r.stderr, (r.returncode, r.stderr)
    second = tmp_path / "second"
    second.mkdir()
    r2, _ = run_qa(second, fake_tty=False)
    assert r2.returncode == 2 and "QA_REAL_TTYD=1" in r2.stderr, (r2.returncode, r2.stderr)


# ---------- issue #100: launch routes are inert on a dev board whose directories could reach the real home ----------

import ast

from app import claude_auth, devguard
from app.config import settings

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
FAKE_AGENTS = ROOT / "scripts" / "dev" / "fake_agents"
UUID1 = "11111111-1111-4111-8111-111111111111"

#: every route that can start a process, by function name: (method, path, json body). The guard answers before anything else is looked up.
LAUNCH_ROUTES = {
    "api_create_session": ("POST", "/api/projects/shop/repos/api/sessions", {"launcher": "shell"}),
    "api_external_open": ("POST", f"/api/external/codex/{UUID1}/open", None),
    "api_task_dispatch": ("POST", "/api/tasks/1/dispatch", {"mode": "lane"}),
    "api_task_reopen": ("POST", "/api/tasks/1/reopen", {}),
    "api_task_fix_ci": ("POST", "/api/tasks/1/fix-ci", None),
    "api_run_resume": ("POST", "/api/runs/1/resume", None),
    "api_restart": ("POST", "/api/sessions/shop--api--s1/restart", {"approval": "never"}),
    "api_job_run": ("POST", "/api/jobs/1/run", None),
    "api_account_login": ("POST", "/api/accounts/login", {}),
    "api_codex_account_login": ("POST", "/api/codex-accounts/login", {"label": "qa"}),
    "api_login": ("POST", "/api/claude/login", None),
}
#: routes that reach a launch primitive but are exempt, each with its reason (none today)
LAUNCH_EXEMPT: dict[str, str] = {
    "api_create_project": "clones with `git clone` in a tmux session, never an agent; the launch point still refuses and _launch_clone cleans up",
    "api_add_repo": "same: `git clone` only, refused at the launch point on an unsandboxed dev board",
}
#: routes that reach the guard through a helper which calls it first thing
GUARDED_HELPERS = {"_task_launch", "_session_create", "_task_dispatch", "_tasks_create"}      # the routes' bodies (issue #141 split them out so a paired node's wrapper calls the same code); _tasks_create reaches the guard through _task_launch
#: routes that start a session only on some bodies, with the body that makes them do so
LAUNCH_ROUTES.update({"api_create_task": ("POST", "/api/projects/shop/repos/api/tasks", {"title": "t", "prompt": "p", "dispatch": True}),
                      "api_tasks_create": ("POST", "/api/tasks", {"project": "shop", "repo": "api", "title": "t", "prompt": "p", "dispatch": True}),
                      "api_create_chain": ("POST", "/api/projects/shop/repos/api/chains", {"dispatch": True, "steps": [{"title": "t", "prompt": "p"}]})})


@pytest.fixture
def devboard(lite_client, projects_dir, fake_tmux, monkeypatch):
    """A board under the dev bypass whose four directories are the conftest temp ones (sandboxed), with tmux, claude auth and the claude
    binary faked. `unsandbox()` moves the Claude config dir under the (temp) home, the way the documented dev run without CLAUDE_CONFIG_DIR does."""
    repo = projects_dir / "shop" / "api"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    monkeypatch.setattr(settings, "dev_bypass_user", "dev")
    monkeypatch.setattr(settings, "claude_bin", lambda: str(FAKE_AGENTS / "claude"))
    monkeypatch.setattr(claude_auth, "status", lambda: {"installed": True, "version": "2.1.0", "loggedIn": True, "authMethod": "claude.ai"})
    monkeypatch.setattr(claude_auth, "version", lambda: "2.1.0")
    started = []
    monkeypatch.setattr(claude_auth, "start_login", lambda *a, **k: started.append("claude"))
    from app import main
    main._invalidate_scan()

    def unsandbox():
        monkeypatch.setattr(settings, "claude_config_dir", pathlib.Path.home() / ".claude")

    return type("Dev", (), {"client": lite_client, "tmux": fake_tmux, "started": started, "unsandbox": staticmethod(unsandbox)})


def test_dev_sandboxed_needs_the_bypass_and_all_four_directories_outside_the_home(devboard, monkeypatch, tmp_path):
    assert settings.dev_sandboxed() is True
    assert devguard.launch_ok() is True
    home = pathlib.Path.home()
    for attr in ("data_dir", "projects_dir", "claude_config_dir", "codex_home"):
        keep = getattr(settings, attr)
        monkeypatch.setattr(settings, attr, home / "inside")
        assert settings.dev_sandboxed() is False, f"{attr} under the home"
        monkeypatch.setattr(settings, attr, pathlib.Path("relative/dir"))
        assert settings.dev_sandboxed() is False, f"{attr} relative"
        link = tmp_path / f"link-{attr}"
        link.symlink_to(home)
        monkeypatch.setattr(settings, attr, link)
        assert settings.dev_sandboxed() is False, f"{attr} a symlink back into the home"
        monkeypatch.setattr(settings, attr, keep)
    assert settings.dev_sandboxed() is True
    monkeypatch.setattr(settings, "dev_bypass_user", None)
    assert settings.dev_sandboxed() is False, "no bypass: not a sandboxed dev board"
    assert devguard.launch_ok() is True, "no bypass: production is never blocked"


def test_every_launch_route_answers_409_on_an_unsandboxed_dev_board_and_starts_nothing(devboard):
    devboard.unsandbox()
    for name, (method, path, body) in LAUNCH_ROUTES.items():
        r = devboard.client.request(method, path, headers=H, json=body)
        assert r.status_code == 409, (name, r.status_code, r.text)
        assert "dev mode: set the four directories to temp paths" in r.json()["error"], name
        assert str(pathlib.Path.home()) not in r.text, "the refusal names the settings, never a path"
    assert devboard.tmux["created"] == [] and devboard.started == [], "nothing was launched"


def test_a_launch_works_on_a_sandboxed_dev_board_with_the_fake_agent(devboard):
    r = devboard.client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "claude"})
    assert r.status_code == 201, r.text
    assert len(devboard.tmux["created"]) == 1
    assert devboard.client.post("/api/claude/login", headers=H).status_code == 200 and devboard.started == ["claude"]


def test_state_reports_dev_sandboxed_only_under_the_bypass(devboard, monkeypatch):
    assert devboard.client.get("/api/state", headers=H).json()["dev"] == {"sandboxed": True}
    devboard.unsandbox()
    assert devboard.client.get("/api/state", headers=H).json()["dev"] == {"sandboxed": False}
    monkeypatch.setattr(settings, "dev_bypass_user", None)
    assert "dev" not in devboard.client.get("/api/state", headers=H).json()


def test_production_without_the_bypass_is_unchanged(devboard, monkeypatch):
    monkeypatch.setattr(settings, "dev_bypass_user", None)
    devboard.unsandbox()                         # even with the config dir under the home, production launches as before
    r = devboard.client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "claude"})
    assert r.status_code == 201, r.text
    devguard.require_real_launch_ok()            # a no-op


def test_a_headless_job_run_is_refused_on_an_unsandboxed_dev_board(devboard, monkeypatch):
    """The scheduler's own worker starts `claude -p` / `codex exec` without a route: run_job refuses there too."""
    from app import scheduler
    devboard.unsandbox()
    monkeypatch.setattr(scheduler, "_finish", lambda db, job, run_id, summary, *a: summary)
    monkeypatch.setattr(scheduler.subprocess, "run", lambda *a, **k: pytest.fail("a headless agent was started"))
    monkeypatch.setattr(scheduler.projects, "repo_path", lambda p, r: pathlib.Path("/nonexistent"))
    monkeypatch.setattr(scheduler.tasks, "unique_slug", lambda *a, **k: "s")
    fake_db = type("D", (), {"task_slugs": lambda self, p, r: []})()
    out = scheduler.run_job(fake_db, {"id": 1, "name": "n", "project": "p", "repo": "r", "agent": "claude", "prompt": "x"}, 1)
    assert out["status"] == "error" and "dev mode" in out["error"]


def _route_functions(tree):
    out = {}
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in
                                                  ("post", "put", "patch", "delete") for d in n.decorator_list):
            out[n.name] = n
    return out


def _called(fn):
    return {c.func.id if isinstance(c.func, ast.Name) else c.func.attr for c in ast.walk(fn)
            if isinstance(c, ast.Call) and isinstance(c.func, (ast.Name, ast.Attribute))}


def test_every_route_that_can_start_a_process_calls_the_guard():
    """By name: the listed routes call require_real_launch_ok() themselves, and no other route reaches a launch primitive (through any
    helper in main.py) without being listed or exempted with a reason. A new launch route cannot skip the guard silently."""
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    calls = {name: _called(fn) for name, fn in fns.items()}
    prims = {"start_login", "_start_session_row"}            # tmux.new_session is reached only through _start_session_row in main.py
    reach = {n for n, c in calls.items() if c & prims}
    while True:
        more = {n for n, c in calls.items() if c & reach} - reach
        if not more:
            break
        reach |= more
    routes = _route_functions(tree)
    unlisted = sorted(n for n in set(routes) & reach if n not in LAUNCH_ROUTES and n not in LAUNCH_EXEMPT)
    assert not unlisted, f"routes that can start a process but are neither guarded nor exempted: {unlisted}"
    for name in LAUNCH_ROUTES:
        assert name in routes, f"{name} is not a route any more"
        assert "require_real_launch_ok" in calls[name] or calls[name] & GUARDED_HELPERS, f"{name} does not call the guard"
    for h in GUARDED_HELPERS:
        assert "require_real_launch_ok" in calls[h] or calls[h] & (GUARDED_HELPERS - {h}), f"{h} is listed as a guarded helper but does not call the guard"
    assert "require_real_launch_ok" in calls["_start_session_row"], "the one tmux launch point is guarded too"


def test_the_fake_agents_are_harmless_and_answer_the_probes(tmp_path):
    home = tmp_path / "fakehome"
    home.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    for name in ("claude", "codex"):
        exe = FAKE_AGENTS / name
        assert os.access(exe, os.X_OK), f"{name} must be executable"
        v = subprocess.run([str(exe), "--version"], capture_output=True, text=True, env=env, timeout=10)
        assert v.returncode == 0 and "dev stand-in" in v.stdout
        h = subprocess.run([str(exe), "--help"], capture_output=True, text=True, env=env, timeout=10)
        assert h.returncode == 0 and "dev stand-in" in h.stdout
        for quit_word in ("/exit", "/quit"):
            r = subprocess.run([str(exe)], input=f"hello\n{quit_word}\n", capture_output=True, text=True, env=env, timeout=10)
            assert r.returncode == 0 and "not a real agent" in r.stdout and "bye" in r.stdout
    st = subprocess.run([str(FAKE_AGENTS / "claude"), "auth", "status", "--json"], capture_output=True, text=True, env=env, timeout=10)
    assert json.loads(st.stdout)["loggedIn"] is False
    ls = subprocess.run([str(FAKE_AGENTS / "codex"), "login", "status"], capture_output=True, text=True, env=env, timeout=10)
    assert ls.returncode == 0 and ls.stdout.strip()
    assert list(home.iterdir()) == [], "the stand-ins never write to the home"


def test_qa_terminal_refuses_a_board_that_is_not_sandboxed(tmp_path):
    r, snippets = run_qa(tmp_path, fake_tty=True, sandboxed=False)
    assert r.returncode == 2 and "dev.sandboxed" in r.stderr, r.stderr[-1500:]
    assert snippets == [], "the browser was never driven"


def test_qa_ui_refuses_a_board_that_is_not_sandboxed(tmp_path):
    for tool in ("bash", "curl", "python3"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} is not installed")
    handler = type("Board", (_StubBoard,), {"tty_is_fake": True, "sandboxed": False})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        browse = tmp_path / "fakebrowse"
        browse.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        browse.chmod(0o755)
        env = dict(os.environ, B=str(browse))
        env.pop("QA_HEADER", None)
        r = subprocess.run(["bash", str(ROOT / "scripts" / "qa-ui.sh"), f"http://127.0.0.1:{srv.server_address[1]}", str(tmp_path / "out")],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    finally:
        srv.shutdown()
    assert r.returncode == 2 and "dev.sandboxed" in r.stderr, r.stderr[-1500:]
