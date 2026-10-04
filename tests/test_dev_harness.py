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

    def do_GET(self):
        if self.path.startswith("/tty/"):
            body = b'<script src="fake_tty.js"></script>' if self.tty_is_fake else b"<html>ttyd</html>"
            code = 200
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


def run_qa(tmp_path, fake_tty, env_extra=None, width="390"):
    """Run the script once against a stub board and the blind browser: (CompletedProcess, [snippets]). Not a verdict about the page (that
    is the reviewer's browser run); it proves every JS snippet the script sends parses, that no branch dies on `set -u` or a typo, and
    which checks each mode runs."""
    for tool in ("bash", "curl", "python3", "node"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} is not installed")
    handler = type("Board", (_StubBoard,), {"tty_is_fake": fake_tty})
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
