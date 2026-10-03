"""The generated service-worker shell and the glob-based asset version (app/main.py static section, app/static/sw.js)."""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

STATIC_ROOT = pathlib.Path(__file__).resolve().parent.parent / "app" / "static"
H = {"Tailscale-User-Login": "alice@example.com"}
SHELL_EXTS = {".js", ".css", ".woff2", ".webmanifest", ".png"}


def shell_in(sw_text):
    m = re.search(r"JSON\.parse\('([^']*)'\)", sw_text)
    assert m, "SHELL = JSON.parse('...') not found in the served worker"
    return json.loads(m.group(1))


def static_copy(tmp_path, monkeypatch):
    """A private copy of app/static with main.STATIC_DIR pointed at it, so a test may add and edit files freely."""
    from app import main
    dst = tmp_path / "static"
    shutil.copytree(main.STATIC_DIR, dst, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(main, "STATIC_DIR", dst)
    return dst


def test_sw_is_generated_from_the_shell(lite_client):
    client = lite_client
    from app import main
    r = client.get("/sw.js", headers=H)
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-cache"
    assert "__" not in r.text                       # no placeholder survives the substitution
    assert "'ccboard-shell-" + main.ASSET_VERSION + "'" in r.text and len(main.ASSET_VERSION) == 12
    assert shell_in(r.text) == main.shell_paths()
    assert r.content == main.SW_JS == main.render_sw()
    assert "addEventListener('push'" in r.text and "addEventListener('notificationclick'" in r.text


def test_shell_covers_every_static_file(lite_client):
    client = lite_client
    from app import main
    root = main.STATIC_DIR
    shell = shell_in(client.get("/sw.js", headers=H).text)
    assert shell[0] == "/" and shell[1:] == sorted(shell[1:]) and len(set(shell)) == len(shell)
    for path in shell[1:]:
        assert path.startswith("/static/") and (root / path[len("/static/"):]).is_file(), path
    expected = {"/static/" + p.relative_to(root).as_posix() for p in root.rglob("*")
                if p.is_file() and p.suffix in SHELL_EXTS and p.name != "sw.js" and "__pycache__" not in p.parts and "demo" not in p.relative_to(root).parts}
    assert set(shell[1:]) == expected and expected
    assert "/static/sw.js" not in shell and not any(p.endswith(".html") for p in shell)
    assert not any("/demo/" in p for p in shell)
    for must in ("/static/style.css", "/static/manifest.webmanifest", "/static/icon-192.png", "/static/vendor/blueprint/blueprint.css"):
        assert must in shell


def test_shell_skips_demo_pycache_and_unsafe_names(tmp_path, monkeypatch):
    from app import main
    dst = static_copy(tmp_path, monkeypatch)
    for rel in ("demo/data.js", "pages/demo/x.js", "__pycache__/c.js", "pages/it's.js", "pages/a b.png", "pages/ok_1-2.js"):
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        (dst / rel).write_text("x")
    (dst / "notes.txt").write_text("not a shippable type")
    files = {p.relative_to(dst).as_posix() for p in main.static_files()}
    assert {"index.html", "term.html", "sw.js", "pages/ok_1-2.js"} <= files
    assert files.isdisjoint({"demo/data.js", "pages/demo/x.js", "__pycache__/c.js", "notes.txt"})
    shell = main.shell_paths()
    assert "/static/pages/ok_1-2.js" in shell
    assert not any(c in p for p in shell for c in "'\\ \"") and not any("demo" in p for p in shell)
    assert "/static/pages/it's.js" not in shell and "/static/pages/a b.png" not in shell     # carried in no quotes at all
    body = main.render_sw().decode()
    assert "__" not in body and shell_in(body) == shell


def test_asset_version_follows_every_static_file(tmp_path, monkeypatch):
    from app import main
    dst = static_copy(tmp_path, monkeypatch)
    v0 = main.asset_version()
    assert re.fullmatch(r"[0-9a-f]{12}", v0) and main.asset_version() == v0
    (dst / "x.js").write_text("1")
    v1 = main.asset_version()
    assert v1 != v0
    (dst / "x.js").write_text("2")                                           # an edit anywhere, not just a new name
    v2 = main.asset_version()
    assert v2 not in (v0, v1)
    vendor = dst / "vendor" / "blueprint" / "blueprint.css"
    vendor.write_bytes(vendor.read_bytes() + b"\n/* edit */")
    v3 = main.asset_version()
    assert v3 not in (v0, v1, v2)
    sw = dst / "sw.js"
    sw.write_text(sw.read_text() + "\n// edit")                              # the worker template is part of the build id
    v4 = main.asset_version()
    assert v4 not in (v0, v1, v2, v3)
    (dst / "pages").mkdir(exist_ok=True)
    (dst / "pages" / "new.js").write_text("")                                # a new empty file still counts
    assert main.asset_version() != v4
    before = main.asset_version()
    (dst / "demo").mkdir(exist_ok=True)
    (dst / "demo" / "fixture.js").write_text("demo data")                    # excluded: never ships in the build id
    (dst / "README.txt").write_text("not a shippable type")
    assert main.asset_version() == before


def test_rendered_sw_is_valid_javascript(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    from app import main
    out = tmp_path / "sw.js"
    out.write_bytes(main.render_sw())
    r = subprocess.run([node, "--check", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_static_responses_carry_csp_and_nosniff(lite_client):
    client = lite_client
    for url in ("/static/style.css", "/static/sw.js", "/static/vendor/blueprint/blueprint.css", "/sw.js"):
        r = client.get(url, headers=H)
        assert r.status_code == 200, url
        assert r.headers["Content-Security-Policy"] == "default-src 'self'; frame-ancestors 'none'", url
        assert r.headers["X-Content-Type-Options"] == "nosniff", url
    assert client.get("/static/style.css").status_code == 403       # no Tailscale identity: nothing is served
    assert client.get("/sw.js").status_code == 403


def _scripts(html_name):
    from html.parser import HTMLParser
    found = []

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == "script":
                found.append(dict(attrs).get("src"))
    P().feed((STATIC_ROOT / html_name).read_text())
    return found


def test_script_order_core_first_main_last():
    """Classic scripts share one global scope: core.js must define el()/api() before anything uses them and
    main.js (the only caller of startStatePolling) must come last. term.html loads only the terminal set."""
    idx = _scripts("index.html")
    assert idx[0] == "/static/core.js" and idx[-1] == "/static/main.js", idx
    assert idx[:2] == ["/static/core.js", "/static/components.js"], idx
    assert idx.index("/static/router.js") < idx.index("/static/pages/home.js") < idx.index("/static/main.js"), idx
    assert len(idx) == len(set(idx)), f"duplicate script tag: {idx}"
    term = _scripts("term.html")
    allowed = ["/static/core.js", "/static/components.js", "/static/termkit.js", "/static/term.js"]
    assert term[0] == "/static/core.js" and term[-1] == "/static/term.js" and all(t in allowed for t in term) and len(term) == len(set(term)), term


def test_only_main_js_starts_the_poll():
    """A top-level (column 0) startStatePolling() call anywhere but main.js would poll twice."""
    for js in sorted(STATIC_ROOT.rglob("*.js")):
        rel = js.relative_to(STATIC_ROOT).as_posix()
        if rel.startswith("vendor/") or rel.startswith("demo/") or rel == "main.js":
            continue
        for n, line in enumerate(js.read_text().splitlines(), 1):
            assert not re.match(r"^\s*startStatePolling\s*\(", line), f"{rel}:{n} starts the poll outside main.js"
    assert any(re.match(r"^\s*startStatePolling\s*\(", l) for l in (STATIC_ROOT / "main.js").read_text().splitlines())


def test_every_app_owned_static_file_has_a_known_extension():
    """A new static file type that STATIC_EXTS does not list would be served but left out of the build id and the shell."""
    from app import main
    bad = [p.relative_to(STATIC_ROOT).as_posix() for p in STATIC_ROOT.rglob("*")
           if p.is_file() and not p.relative_to(STATIC_ROOT).as_posix().startswith(("vendor/", "demo/"))
           and p.suffix not in main.STATIC_EXTS and "__pycache__" not in p.parts]
    assert not bad, f"unlisted static file types: {bad}"


FONT_PATHS = ("/static/vendor/fonts/jetbrains-mono-latin-wght-normal.woff2", "/static/vendor/fonts/inter-latin-wght-normal.woff2")


def test_fonts_are_in_the_generated_shell(lite_client):
    """Both vendored fonts are precached (and, being in the shell, hashed into the build id): offline the board keeps its type."""
    from app import main
    for path in FONT_PATHS:
        assert path in main.shell_paths(), path
    served = shell_in(lite_client.get("/sw.js", headers=H).text)
    for path in FONT_PATHS:
        assert path in served, path


def test_manifest_is_the_dark_installable_app():
    manifest = json.loads((STATIC_ROOT / "manifest.webmanifest").read_text())
    assert manifest["start_url"] == "/" and manifest["id"] == "/" and manifest["scope"] == "/"
    assert manifest["theme_color"] == "#14181c" and manifest["background_color"] == "#14181c"
    assert manifest["display"] == "standalone"
    shortcuts = manifest["shortcuts"]
    assert len(shortcuts) == 3
    assert [s["url"] for s in shortcuts] == ["/#/inbox", "/#/quad", "/#/usage"]
    assert all(s.get("name") for s in shortcuts)
    sizes = {i["sizes"] for i in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes


# ---------- v0.5.3: the route table, the page registry, the boot sequence ----------
# Written against the v0.5.3 contract; a failure here usually means a slice has not landed yet and the assertion names what is missing.

ROUTE_IDS = {"home", "inbox", "tasks", "agents", "project", "quad", "usage", "memory", "onboarding", "settings", "search", "session"}


def _blank_js(text):
    from tests.test_static import blank_js
    return blank_js(text)


def _routes_block():
    src = _blank_js((STATIC_ROOT / "router.js").read_text())
    m = re.search(r"const ROUTES\s*=\s*\[(.*?)^\];", src, re.S | re.M)
    assert m, "const ROUTES = [ ... ]; not found in app/static/router.js"
    return m.group(1)


def test_routes_array_is_exactly_the_twelve_route_ids():
    ids = re.findall(r"\bid:\s*'([a-z]+)'", _routes_block())
    assert len(ids) == len(set(ids)), f"duplicate route ids: {ids}"
    assert set(ids) == ROUTE_IDS and len(ids) == 12, f"missing {sorted(ROUTE_IDS - set(ids))}, unexpected {sorted(set(ids) - ROUTE_IDS)}"


def test_agents_route_is_the_plain_path_slash_agents():
    block = _routes_block()
    assert re.search(r"\{\s*id:\s*'agents',\s*re:\s*/\^\\/agents\$/\s*\}", block), \
        "ROUTES needs { id: 'agents', re: /^\\/agents$/ } (the Agents roster, v0.5.3 plan change)"


def _register_calls():
    """{page id: [files that call registerPage('<id>', ...)]} over app/static/pages/*.js."""
    calls = {}
    for js in sorted((STATIC_ROOT / "pages").glob("*.js")):
        code = _blank_js(js.read_text())
        for m in re.finditer(r"\bregisterPage\(\s*'([A-Za-z0-9_-]+)'", code):
            calls.setdefault(m.group(1), []).append(js.name)
    return calls


def test_exactly_one_register_page_per_route_id_across_the_pages():
    calls = _register_calls()
    assert set(calls) <= ROUTE_IDS, f"registerPage for an id that is not a route: {sorted(set(calls) - ROUTE_IDS)}"
    missing = sorted(ROUTE_IDS - set(calls))
    assert not missing, f"no registerPage call in app/static/pages/*.js for: {missing}"
    dup = {i: files for i, files in calls.items() if len(files) != 1}
    assert not dup, f"each route id is registered exactly once: {dup}"


def test_each_page_registers_in_its_own_file_or_the_placeholders():
    """pages/<id>.js owns its route; the routes with no page yet live in pages/placeholders.js until their phase lands."""
    for page_id, files in _register_calls().items():
        assert files[0] in (f"{page_id}.js", "placeholders.js"), f"registerPage('{page_id}') sits in pages/{files[0]}"
    placeholders = [i for i, files in _register_calls().items() if files == ["placeholders.js"]]
    assert {"project", "quad", "usage", "memory", "onboarding"} >= set(placeholders), placeholders


def test_main_js_rewrites_the_legacy_hash_before_the_poll_starts():
    code = _blank_js((STATIC_ROOT / "main.js").read_text())
    m = re.search(r"\brewriteLegacyHash\s*\(\s*\)", code)
    assert m, "main.js must call rewriteLegacyHash() at boot so '#s=<tmux>' becomes '#/s/<tmux>'"
    assert m.start() < code.index("startStatePolling("), "rewriteLegacyHash() has to run before the first poll"


def test_the_service_worker_nav_message_sets_location_hash():
    """navigator.serviceWorker 'message' {type:'nav', url} lets a second push tap change the hash of an already open board."""
    owners = []
    for js in sorted(STATIC_ROOT.glob("*.js")) + sorted((STATIC_ROOT / "pages").glob("*.js")):
        if js.name == "sw.js":
            continue
        code = _blank_js(js.read_text())
        if re.search(r"serviceWorker\s*\.\s*addEventListener\(\s*'message'", code) and re.search(r"""['"]nav['"]""", code):
            owners.append(js.name)
    assert owners, "no app script handles the service worker 'message' event with type 'nav' (router.js owns it)"
