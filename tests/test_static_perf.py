"""Pins for the page-load fixes of issue #103 (Lighthouse): scripts that do not block the first paint, the icon font fetched early, a frame that already has
its sidebar column before shell.js runs, and the per-route lazy loading (index.html carries only the shell set; app/static/lazy.js lists the bundles the router and
the on-use entry points load once). The text-compression half is tests/test_static_gzip.py."""
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k: (v if v is not None else "") for k, v in attrs}))


def tags(page):
    p = _Tags()
    p.feed((STATIC / page).read_text(encoding="utf-8"))
    return p.tags


@pytest.mark.parametrize("page", ["index.html", "term.html"])
def test_every_script_is_deferred_classic_and_external(page):
    """defer keeps the order (the scripts share one global scope) and runs them before DOMContentLoaded, but the parser and the first paint no longer wait for them."""
    scripts = [a for t, a in tags(page) if t == "script"]
    assert scripts
    for a in scripts:
        assert a.get("src", "").startswith("/static/") and "defer" in a, (page, a)
        assert "async" not in a and a.get("type", "text/javascript") in ("", "text/javascript"), (page, a)


@pytest.mark.parametrize("page", ["index.html", "term.html"])
def test_the_icon_font_is_preloaded_beside_the_text_fonts(page):
    """Blueprint's icons are glyphs of blueprint-icons-20.woff2: asked for only when JS builds the first icon, the font arrived after the first paint and every row swapped
    its icon width (a layout shift on each page). The preload needs as=font, type and crossorigin, like the text fonts."""
    pre = {a.get("href"): a for t, a in tags(page) if t == "link" and a.get("rel") == "preload"}
    a = pre.get("/static/vendor/blueprint/blueprint-icons-20.woff2")
    assert a is not None, f"{page}: no preload for the icon font"
    assert a.get("as") == "font" and a.get("type") == "font/woff2" and "crossorigin" in a
    assert (STATIC / "vendor" / "blueprint" / "blueprint-icons-20.woff2").is_file()
    assert "blueprint-icons-20" in (STATIC / "vendor" / "blueprint" / "blueprint-icons.css").read_text(encoding="utf-8")


def test_the_frame_has_its_sidebar_column_before_the_shell_is_built():
    """index.html ships body[data-shell=""] (no data-sb) until Shell.applyMode runs; without these rules #main sat at the left edge and then moved 260 px (CLS 0.18 on every
    desktop page). The widths are applyMode's own defaults: the rail from 600 px, the full sidebar from 840 px."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert re.search(r'<body[^>]*\bdata-shell=""', html), "the pre-shell state these rules key on"
    css = (STATIC / "shell.css").read_text(encoding="utf-8")
    assert re.search(r'@media \(min-width:600px\)\s*\{\s*body\[data-shell=""\] #app\s*\{\s*--sb-w:var\(--sb-rail\);', css)
    assert re.search(r'@media \(min-width:840px\)\s*\{\s*body\[data-shell=""\] #app\s*\{\s*--sb-w:var\(--sb-full\);', css)
    js = (STATIC / "shell.js").read_text(encoding="utf-8")
    assert "mode === 'compact' ? 'none'" in js and "'rail' : 'full'" in js, "applyMode still picks none / rail / full the way the CSS defaults assume"
    assert re.search(r"--sb-rail:\s*48px", css) and re.search(r"--sb-full:\s*260px", css)



# ---------------------------------------------------------------- lazy loading (lazy.js): the first-paint set and the bundles

LAZY_SRC = STATIC / "lazy.js"
FIRST_PAINT_BUDGET = 560_000      # bytes of script index.html may load up front; the lazy split took it from 1.1 MB to about 520 KB (the Agents page, the create flow and the task card sheets later brought it back from 560 KB to about 518 KB)


def _manifest():
    """LAZY_BUNDLES of lazy.js, read from the source (it is a literal): name -> {'js': [...], 'css': [...], 'needs': [...]}."""
    src = LAZY_SRC.read_text(encoding="utf-8")
    start = src.index("const LAZY_BUNDLES = {")
    body = src[start:src.index("\n};", start)]
    out = {}
    for m in re.finditer(r"^  (\w+): \{(.*)\},?$", body, re.M):
        spec = m.group(2)
        grab = lambda key: re.search(key + r": \[([^\]]*)\]", spec)      # noqa: E731
        js, css, needs = grab("js"), grab("css"), grab("needs")
        out[m.group(1)] = {
            "js": re.findall(r"'([^']+)'", js.group(1)) if js else [],
            "css": re.findall(r"href: '([^']+)'", css.group(1)) if css else [],
            "ranks": [int(x) for x in re.findall(r"rank: (\d+)", css.group(1))] if css else [],
            "needs": re.findall(r"'([^']+)'", needs.group(1)) if needs else [],
        }
    return out


def test_the_first_paint_set_is_the_shell_and_stays_under_budget():
    """Issue #103: index.html carries what the shell and the first paint need (Home, Inbox, Widgets, Tasks, Agents, Search, the session peek, the placeholders);
    every other page script and the launcher, palette, dnd, tree and terminal kit load on first use."""
    scripts = [a["src"] for t, a in tags("index.html") if t == "script"]
    total = sum((STATIC / s[len("/static/"):]).stat().st_size for s in scripts)
    assert total <= FIRST_PAINT_BUDGET, f"index.html loads {total} bytes of script up front (budget {FIRST_PAINT_BUDGET}): {scripts}"
    assert "/static/lazy.js" in scripts


def test_every_script_is_in_index_or_in_exactly_one_lazy_bundle():
    m = _manifest()
    assert {"launcher", "palette", "dnd", "tree", "termkit", "memory", "project", "settings", "usage", "quad", "onboarding", "agents", "shellcreate", "tasksheets"} <= set(m)
    lazy = [j for b in m.values() for j in b["js"]]
    assert len(lazy) == len(set(lazy)), "a script is in two bundles"
    eager = [a["src"] for t, a in tags("index.html") if t == "script"]
    assert not set(lazy) & set(eager), "a script is both in index.html and in a bundle"
    on_disk = {"/static/" + p.relative_to(STATIC).as_posix() for p in list(STATIC.glob("*.js")) + list(STATIC.glob("pages/*.js"))}
    on_disk -= {"/static/sw.js", "/static/term.js"}       # the worker has its own scope; term.js belongs to term.html
    assert on_disk == set(eager) | set(lazy), f"orphaned or missing: {sorted(on_disk ^ (set(eager) | set(lazy)))}"


def test_lazy_files_exist_stay_same_origin_and_are_precached_by_the_service_worker():
    from app.main import shell_paths
    shell = set(shell_paths())
    m = _manifest()
    for name, b in m.items():
        for path in b["js"] + b["css"]:
            assert re.fullmatch(r"/static/[A-Za-z0-9_./-]+", path) and ".." not in path, (name, path)
            assert (STATIC / path[len("/static/"):]).is_file(), (name, path)
            assert path in shell, f"{path} is missing from the service-worker shell: the {name} bundle would not open offline"
        assert set(b["needs"]) <= set(m), (name, b["needs"])
    code = re.sub(r"/\*.*?\*/", "", LAZY_SRC.read_text(encoding="utf-8"), flags=re.S)
    assert not re.search(r"\beval\s*\(|new Function|innerHTML|insertAdjacentHTML|\.text\s*=|\.textContent\s*=|document\.write", code), "the loader only sets a same-origin src: no eval, no inline script text"


def test_every_lazy_route_points_at_a_bundle_that_registers_it():
    src = LAZY_SRC.read_text(encoding="utf-8")
    routes = dict(re.findall(r"(\w+): '(\w+)'", re.search(r"const LAZY_ROUTES = \{([^}]*)\};", src).group(1)))
    m = _manifest()
    ids = set(re.findall(r"\{ id: '(\w+)'", (STATIC / "router.js").read_text(encoding="utf-8")))
    for route, bundle in routes.items():
        assert route in ids and bundle in m, (route, bundle)
        js = "".join((STATIC / j[len("/static/"):]).read_text(encoding="utf-8") for j in m[bundle]["js"])
        assert f"registerPage('{route}'" in js, f"{bundle} does not register the {route} page"
    # every route of the router is registered either by an eager script or by one lazy bundle
    eager_js = "".join((STATIC / a["src"][len("/static/"):]).read_text(encoding="utf-8") for t, a in tags("index.html") if t == "script")
    for rid in ids:
        assert (f"registerPage('{rid}'" in eager_js) != (rid in routes), f"route {rid} must be registered by exactly one of index.html and a lazy route"


def test_the_page_sheets_split_out_of_pages_css_load_with_their_scripts_in_the_old_cascade_order():
    m = _manifest()
    pages_css = (STATIC / "pages.css").read_text(encoding="utf-8")
    assert len(pages_css.encode("utf-8")) < 80_000, "pages.css was 128 KB before the split"
    probes = {"quad": ("pages/quad.css", "#page .quad .qgrid"), "memory": ("pages/memory.css", ".mem-narr"), "settings": ("pages/settings.css", "#page .settings-page .set-h"),
              "onboarding": ("pages/onboarding.css", "#page .wiz .wiz-steps")}
    for bundle, (css, needle) in probes.items():
        assert "/static/" + css in m[bundle]["css"], (bundle, css)
        assert needle in (STATIC / css).read_text(encoding="utf-8"), (css, needle)
        assert needle not in pages_css, f"{needle} is in pages.css and in {css}"
    ranks = {m[b]["css"][0]: m[b]["ranks"][0] for b in probes}
    order = [c for c, _ in sorted(ranks.items(), key=lambda kv: kv[1])]
    assert order == ["/static/pages/settings.css", "/static/pages/onboarding.css", "/static/pages/quad.css", "/static/pages/memory.css"], \
        "the sheets come in the order their rules had in pages.css"
    styles = [a["href"] for t, a in tags("index.html") if t == "link" and a.get("rel") == "stylesheet"]
    assert styles.index("/static/pages.css") + 1 == styles.index("/static/charts.css"), "lazy sheets go in just before charts.css (lazy.js), i.e. straight after pages.css"
    assert "link[href=\"/static/charts.css\"]" in LAZY_SRC.read_text(encoding="utf-8")


def test_the_lazy_loader_is_definition_only_and_the_router_waits_for_it():
    src = (STATIC / "router.js").read_text(encoding="utf-8")
    assert "Lazy.pending(r.id)" in src and "routeLazy(r, wait)" in src and "showLoadError" in src
    main = (STATIC / "main.js").read_text(encoding="utf-8")
    assert "Lazy.watchLinks()" in main
    assert (STATIC / "term.html").read_text(encoding="utf-8").count("lazy.js") == 0, "the terminal page loads its own scripts eagerly"


# ---------------------------------------------------------------- code moved out of the first-paint set (the pieces a first Home paint never runs)

def _code(rel):
    """A static file without its comments (a name that only a comment mentions is not a use)."""
    return re.sub(r"/\*.*?\*/", "", (STATIC / rel).read_text(encoding="utf-8"), flags=re.S)


def _declares(code, name):
    return re.search(rf"^(?:async\s+)?(?:function\s+{name}\b|(?:const|let|var)\s+{name}\b|{name}\s*=)", code, re.M) is not None


# (lazy file, bundle, the file the code was cut from, the declarations that moved)
MOVES = [
    ("pages/agents-page.js", "agents", "pages/agents.js",
     ["agentsPage", "agentsGroupNode", "agentsSummaryNode", "agentsPatchSummary", "agentsExt", "agentsExtNode", "agentsExtRow", "agentsExtRows", "agentsExtLoad", "agentsExtOpen", "AGENTS_SEGS"]),
    ("shell-create.js", "shellcreate", "shell.js", []),
    ("task-sheets.js", "tasksheets", "components.js", ["taskMoveSheet", "taskEditSheet", "taskPortSheet"]),
    ("task-sheets.js", "tasksheets", "pages/home.js", ["openTaskModal", "diffSideControl"]),
    ("pages/project.js", "project", "components.js", ["makeTaskBoard", "taskSig"]),
]


@pytest.mark.parametrize("lazy_file,bundle,source,names", MOVES, ids=lambda v: v if isinstance(v, str) else "")
def test_moved_declarations_are_gone_from_the_eager_file_and_live_in_a_bundle(lazy_file, bundle, source, names):
    m = _manifest()
    assert "/static/" + lazy_file in m[bundle]["js"], f"{lazy_file} is not in the {bundle} bundle"
    assert "/static/" + source in [a["src"] for t, a in tags("index.html") if t == "script"], f"{source} is an eager file"
    eager, lazy = _code(source), _code(lazy_file)
    for name in names:
        assert not _declares(eager, name), f"{name} is still declared in {source}"
        assert _declares(lazy, name), f"{name} is not declared in {lazy_file}"


def test_the_shell_create_flow_moved_out_of_shell_js_but_openCreate_stayed():
    eager, lazy = _code("shell.js"), _code("shell-create.js")
    for name in ("routeCtx", "createFor", "launchAt", "pickRepo", "showForm", "createPlace"):
        assert f"Shell.{name} = " not in eager, f"Shell.{name} is still in shell.js"
        assert f"Shell.{name} = " in lazy, f"Shell.{name} is not in shell-create.js"
    for name in ("openCreate", "createItems", "defaultRepo", "targetLabel", "withCreate"):
        assert f"Shell.{name} = " in eager, f"Shell.{name} must stay in shell.js: callers read its answer, or build the menu, at once"
    assert "Lazy.run('shellcreate'" in eager


def test_the_task_card_names_are_stubs_in_lazy_js_for_the_tasksheets_bundle():
    src = LAZY_SRC.read_text(encoding="utf-8")
    m = re.search(r"for \(const name of \[([^\]]*)\]\) if \(typeof window\[name\] !== 'function'\) window\[name\] = lazyStub\(name, '[^']*', 'tasksheets'\)", src)
    assert m, "the loop that stubs the task sheets"
    stubbed = re.findall(r"'(\w+)'", m.group(1))
    assert sorted(stubbed) == ["openTaskModal", "taskEditSheet", "taskMoveSheet", "taskPortSheet"]
    # the probe must be a name that is NOT stubbed, or the stub would make the bundle look loaded before its file ran
    probe = re.search(r"tasksheets: \{[^}]*probe: \(\) => typeof (\w+)", src).group(1)
    assert probe not in stubbed and _declares(_code("task-sheets.js"), probe)
    shellcreate = re.search(r"shellcreate: \{[^}]*probe: \(\) => typeof Shell !== 'undefined' && typeof Shell\.(\w+)", src).group(1)
    assert shellcreate == "pickRepo", "the shellcreate probe is a method that shell.js does not define"


def test_the_agents_route_is_lazy_and_settings_loads_the_bundle_it_reads():
    m = _manifest()
    assert m["agents"]["js"] == ["/static/pages/agents-page.js"]
    assert "agents" in m["settings"]["needs"], "Settings > Agents reads agentsExtRows / agentsExtLoad from the agents bundle"
    assert re.search(r"LAZY_ROUTES = \{[^}]*\bagents: 'agents'", LAZY_SRC.read_text(encoding="utf-8"))
    eager_js = "".join(_code(a["src"][len("/static/"):]) for t, a in tags("index.html") if t == "script")
    assert "registerPage('agents'" not in eager_js and "agentsExt" not in eager_js


def test_no_term_page_script_uses_what_moved_out_of_components_js():
    """term.html loads core.js and components.js without lazy.js: it has no stubs, so nothing it runs may need the task sheets."""
    term = "".join(_code(f) for f in ("term.js", "termkit.js"))
    for name in ("taskMoveSheet", "taskEditSheet", "taskPortSheet", "openTaskModal", "makeTaskBoard", "diffSideControl"):
        assert not re.search(rf"\b{name}\b", term), f"{name} is used by the terminal page, which has no lazy loader"
