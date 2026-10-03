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
                if p.is_file() and p.suffix in SHELL_EXTS and p.name != "sw.js" and "__pycache__" not in p.parts
                and "demo" not in p.relative_to(root).parts and "screenshots" not in p.relative_to(root).parts}
    assert set(shell[1:]) == expected and expected
    assert not any("/screenshots/" in p for p in shell), "manifest screenshots are not part of the offline shell"
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


TERM_SCRIPTS = ["/static/core.js", "/static/components.js", "/static/termkit.js", "/static/term.js"]


def test_script_order_core_first_main_last():
    """Classic scripts share one global scope: core.js must define el()/api() before anything uses them and
    main.js (the only caller of startStatePolling) must come last. term.html loads exactly the terminal set, in this order:
    core.js (el/api), components.js (composer), termkit.js (TermKit, definition-only), term.js (starts the page)."""
    from tests.test_static import SCRIPT_ORDER
    idx = _scripts("index.html")
    assert idx[0] == "/static/core.js" and idx[-1] == "/static/main.js", idx
    assert idx[:3] == ["/static/core.js", "/static/components.js", "/static/keymap.js"], idx
    assert idx.index("/static/router.js") < idx.index("/static/pages/home.js") < idx.index("/static/main.js"), idx
    assert idx.index("/static/palette.js") < idx.index("/static/shell.js"), idx
    assert len(idx) == len(set(idx)), f"duplicate script tag: {idx}"
    assert idx == SCRIPT_ORDER, "index.html script order differs from the contract:\n  got      " + "\n  ".join(map(str, idx)) + "\n  expected " + "\n  ".join(SCRIPT_ORDER)
    assert "/static/termkit.js" not in idx, "index.html is not changed in the terminal phase: termkit.js is loaded by term.html only (the dock and quad load it in v0.5.9)"
    assert "termkit" not in (STATIC_ROOT / "index.html").read_text()
    assert _scripts("term.html") == TERM_SCRIPTS, _scripts("term.html")


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


def _manifest():
    return json.loads((STATIC_ROOT / "manifest.webmanifest").read_text())


def _static_file(url):
    assert url.startswith("/static/"), f"{url}: manifest URLs point at /static/"
    path = STATIC_ROOT / url[len("/static/"):]
    assert path.is_file(), f"{url} is in the manifest but app/static/{url[len('/static/'):]} does not exist"
    return path


def test_manifest_is_installable_on_ipad_and_laptop():
    """v0.5.3b: title-bar overlay on desktop Chrome/Edge, one window per app, categories, a share target, no store listing."""
    m = _manifest()
    assert m["display"] == "standalone" and m["id"] == "/" and m["start_url"] == "/" and m["scope"] == "/"
    assert m["display_override"] == ["window-controls-overlay", "standalone"]
    assert m["launch_handler"]["client_mode"] == "focus-existing"
    assert m["prefer_related_applications"] is False
    assert isinstance(m["description"], str) and len(m["description"]) >= 20
    assert isinstance(m["categories"], list) and m["categories"] and all(isinstance(c, str) and c == c.lower() for c in m["categories"])
    assert "short_name" in m and m["name"] == "ccboard"


def test_manifest_share_target_is_a_plain_get_on_the_root():
    """A GET share target cannot add fixed params, so the page detects a share by the title/text/url params themselves."""
    share = _manifest()["share_target"]
    assert share["action"] == "/" and share["method"] == "GET"
    assert share["params"] == {"title": "title", "text": "text", "url": "url"}
    assert "enctype" not in share or share["enctype"] == "application/x-www-form-urlencoded"
    assert "?" not in share["action"], "share_target cannot add fixed params: detect a share by the title/text/url params"
    assert share["action"].startswith(_manifest()["scope"])


def test_manifest_icons_exist_with_their_declared_sizes_and_a_512_maskable():
    from tests.test_static import PNG_COLOR_TYPES_WITH_ALPHA, png_header
    icons = _manifest()["icons"]
    for icon in icons:
        assert icon["type"] == "image/png"
        w, h = (int(n) for n in icon["sizes"].split("x"))
        assert png_header(_static_file(icon["src"]))[:2] == (w, h), f"{icon['src']} is not {icon['sizes']}"
    assert {"192x192", "512x512"} <= {i["sizes"] for i in icons}
    assert any(i["sizes"] == "512x512" and i.get("purpose", "any") == "any" for i in icons), "a plain 'any' 512 icon is still needed"
    maskable = [i for i in icons if "maskable" in i.get("purpose", "")]
    assert maskable, "manifest needs an icon with purpose maskable"
    for icon in maskable:
        assert icon["purpose"] == "maskable", "keep maskable and any as separate entries (a combined 'any maskable' icon is cropped on Android)"
        assert icon["sizes"] == "512x512"
        width, height, _, color_type = png_header(_static_file(icon["src"]))
        assert (width, height) == (512, 512)
        assert color_type not in PNG_COLOR_TYPES_WITH_ALPHA, f"{icon['src']} has an alpha channel: a maskable icon is full-bleed and opaque"
    assert _static_file("/static/icon-512-maskable.png") in [_static_file(i["src"]) for i in maskable]


def test_manifest_screenshots_exist_with_their_declared_sizes():
    """Chrome's rich install dialog: >= 1 narrow and >= 1 wide shot, each 320..3840 px with a long side of at most 2.3 x the short one."""
    from tests.test_static import png_header
    shots = _manifest()["screenshots"]
    assert len(shots) >= 2
    forms = {s["form_factor"] for s in shots}
    assert forms == {"narrow", "wide"}, forms
    for shot in shots:
        assert shot["type"] == "image/png" and shot.get("label")
        assert shot["src"].startswith("/static/screenshots/"), shot["src"]
        w, h = (int(n) for n in shot["sizes"].split("x"))
        actual = png_header(_static_file(shot["src"]))[:2]
        assert actual == (w, h), f"{shot['src']} is {actual[0]}x{actual[1]} but the manifest says {shot['sizes']}"
        assert 320 <= min(w, h) and max(w, h) <= 3840 and max(w, h) <= 2.3 * min(w, h), shot["sizes"]
        assert (h > w) if shot["form_factor"] == "narrow" else (w > h), f"{shot['src']} is {shot['sizes']} for a {shot['form_factor']} shot"
    names = {s["src"].rsplit("/", 1)[1] for s in shots}
    assert {"agents-390.png", "agents-1280.png"} <= names


def test_maskable_icon_in_the_shell_and_screenshots_left_out_on_purpose(lite_client):
    from app import main
    served = shell_in(lite_client.get("/sw.js", headers=H).text)
    assert "/static/icon-512-maskable.png" in main.shell_paths() and "/static/icon-512-maskable.png" in served
    for s in _manifest()["screenshots"]:              # store metadata: read from the manifest, never through the worker
        assert s["src"] not in main.shell_paths() and s["src"] not in served, f"{s['src']} would cost every install ~100 KB for nothing"
        assert lite_client.get(s["src"], headers=H).status_code == 200
    assert any(p.relative_to(main.STATIC_DIR).as_posix().startswith("screenshots/") for p in main.static_files()), \
        "screenshots still count towards the asset version"


def test_sw_precaches_past_the_http_cache_and_serves_vendor_cache_first(lite_client):
    """/static/vendor/** and *.woff2 are served immutable for a year under unversioned URLs: only this versioned shell cache
    may decide when they change, so the worker must never read them through the browser's HTTP cache."""
    sw = lite_client.get("/sw.js", headers=H).text
    assert "cache: 'reload'" in sw
    assert "path.startsWith('/static/vendor/')" in sw and "cacheFirst(req)" in sw
    assert "fetch(new Request(req.url, { cache: 'reload' }))" in sw


def test_cache_policy_vendor_and_fonts_immutable_the_app_no_cache(lite_client):
    """The full matrix is tests/test_headers.py; this keeps the contract next to the shell it protects."""
    immutable = "public, max-age=31536000, immutable"
    for url in ("/static/vendor/blueprint/blueprint.css", FONT_PATHS[0], FONT_PATHS[1]):
        r = lite_client.get(url, headers=H)
        assert r.status_code == 200 and r.headers["cache-control"] == immutable, url
    for url in ("/static/core.js", "/static/shell.css", "/static/manifest.webmanifest", "/"):
        r = lite_client.get(url, headers=H)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", url


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


# ---------- terminal page on mobile: termkit.js, term.html, the dev tty fake stays out of the shell ----------

TERM_ASSETS = ("/static/termkit.js", "/static/term.js", "/static/term.css", "/static/core.js", "/static/components.js")


def test_terminal_assets_are_in_the_generated_shell_and_the_build_id(lite_client, tmp_path, monkeypatch):
    """termkit.js is a new static file: it must be precached (offline terminal shell) and hashed into the build id, both from the glob."""
    from app import main
    for path in TERM_ASSETS:
        assert path in main.shell_paths(), f"{path} is not in the generated service-worker shell"
    served = shell_in(lite_client.get("/sw.js", headers=H).text)
    for path in TERM_ASSETS:
        assert path in served, path
    assert (STATIC_ROOT / "termkit.js").is_file()
    dst = static_copy(tmp_path, monkeypatch)
    before = main.asset_version()
    kit = dst / "termkit.js"
    kit.write_text(kit.read_text() + "\n// edit")
    assert main.asset_version() != before, "an edit to termkit.js must change the build id (open pages reload after a deploy)"


def test_the_dev_tty_fake_never_ships_in_the_shell(lite_client):
    """scripts/dev/fake_tty lives outside app/static: not precached, not hashed, not served under /static."""
    from app import main
    assert not any("fake_tty" in p for p in main.shell_paths())
    assert not any("fake_tty" in p.as_posix() for p in main.static_files())
    assert lite_client.get("/static/fake_tty.js", headers=H).status_code == 404
    assert lite_client.get("/static/fake_tty/index.html", headers=H).status_code == 404


def test_term_page_is_the_no_cache_skeleton_with_the_terminal_scripts(lite_client):
    r = lite_client.get("/term/ccboard--ccboard--s1", headers=H)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["Content-Security-Policy"] == "default-src 'self'; frame-ancestors 'none'"
    for path in TERM_SCRIPTS:
        assert f'src="{path}"' in r.text, path
    assert r.text.index("/static/core.js") < r.text.index("/static/components.js") < r.text.index("/static/termkit.js") < r.text.index("/static/term.js")
    assert lite_client.get("/term/not-a-ccboard-name", headers=H).status_code == 400


def _meta(name):
    from html.parser import HTMLParser
    found = []

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "meta" and a.get("name") == name:
                found.append(a.get("content", ""))
    P().feed((STATIC_ROOT / "term.html").read_text())
    return found


def test_term_html_keeps_the_viewport_that_resizes_with_the_soft_keyboard():
    """interactive-widget=resizes-content makes the layout viewport shrink for the soft keyboard (Chrome Android); body.term sizes
    itself from --vvh for iOS Safari, which ignores it. viewport-fit=cover is what makes the safe-area insets non-zero."""
    metas = _meta("viewport")
    assert len(metas) == 1, metas
    content = {k.strip(): v.strip() for k, _, v in (part.partition("=") for part in metas[0].split(","))}
    assert content.get("width") == "device-width" and content.get("initial-scale") == "1"
    assert content.get("viewport-fit") == "cover"
    assert content.get("interactive-widget") == "resizes-content"
    assert "user-scalable" not in content and "maximum-scale" not in content, "never lock zoom (accessibility)"


def test_term_html_body_is_the_fixed_terminal_layout_and_dark():
    from tests.test_static import html_tree
    body = next(n for n in html_tree(STATIC_ROOT / "term.html").walk() if n.tag == "body")
    assert "term" in body.classes and "bp5-dark" in body.classes, body.classes



def test_term_html_skeleton_has_the_ids_the_page_and_scripts_qa_terminal_use():
    """term.js fills these (el()) and scripts/qa_terminal.sh selects them: renaming one silently blinds the QA run."""
    from tests.test_static import html_tree
    root = html_tree(STATIC_ROOT / "term.html")
    by_id = {}
    for n in root.walk():
        if "id" in n.attrs:
            assert n.attrs["id"] not in by_id, f"duplicate id {n.attrs['id']}"
            by_id[n.attrs["id"]] = n
    for must in ("termhead", "termmain", "ctxstrip", "ttywrap", "tty", "rail", "histchip", "keyhost", "quickrow", "quick", "sendform", "sendtext", "nl", "sendbtn", "toasts", "back"):
        assert must in by_id, f"term.html has no #{must}"
    wrap = by_id["ttywrap"]
    inside = {n.attrs.get("id") for n in wrap.walk() if n is not wrap}
    assert {"tty", "rail", "histchip"} <= inside, "the iframe, the scroll rail and the History chip live inside #ttywrap (position:relative; the iframe is absolute)"
    assert by_id["tty"].tag == "iframe"
    assert by_id["sendtext"].tag == "textarea" and "composer" in by_id["sendtext"].classes
    assert any(n is by_id["sendtext"] for n in by_id["sendform"].walk()), "the composer is inside the send form"
    assert by_id["sendbtn"].attrs.get("type") == "submit"
    order = [c.attrs.get("id") for c in by_id["termmain"].children]
    assert order == ["ctxstrip", "ttywrap", "keyhost", "quickrow", "sendform"], order
    assert "hidden" in by_id["histchip"].classes, "the History chip starts hidden and shows only while the pane is in copy-mode"
    assert not any("style" in n.attrs for n in root.walk())
