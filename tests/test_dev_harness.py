"""The terminal dev harness: scripts/dev/fake_tty is the board's /tty/ ONLY under the dev bypass (CCBOARD_DEV_BYPASS_USER).

With it, the board serves scripts/dev/fake_tty/ at /tty/ (a page with a stub ttyd `window.term`, so scripts/qa_terminal.sh can drive
the terminal page without tmux or ttyd) and the CSP middleware leaves paths under /tty/ alone, as ttyd's own pages are not ours.
Without it /tty is not served by the board at all (on the box tailscale serve maps /tty to ttyd, never to this app) and every response
keeps the CSP.

Whether the mount is decided at import or per request is the backend's choice, so the positive case runs the way the dev board runs:
a fresh interpreter whose environment sets (or lacks) CCBOARD_DEV_BYPASS_USER, one request batch through TestClient (no lifespan: the
static and routing layers need no database).
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

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
