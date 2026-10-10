"""Static accessibility guards (issue #45): they read the shipped source, so a fix cannot rot unseen.

* an icon-only button built with el('button', ...) names itself (aria-label, or visible text);
* an `outline: none` in the stylesheets is either a pointer-focus rule (`:focus:not(:focus-visible)`), paired with another visible focus style in the
  same rule, or listed in OUTLINE_REPLACED with the rule that shows the focus instead. A new bare `outline: none` fails here.
"""
import pathlib
import re

STATIC = pathlib.Path(__file__).resolve().parent.parent / "app" / "static"

# bare `outline:none` rules that are allowed, and the selector (substring) of the rule that draws the replacement
OUTLINE_REPLACED = {
    "dialog#helpdlg:focus": None,                      # a dialog holding the focus itself after open(): the dialog is not a control
    "dialog#sheet:focus": None,                        # same: sheet and drawer hold the focus themselves
    "dialog.qr-editor:focus": None,
    "dialog.rename-dlg:focus": None,
    ".tk-pop:focus": None,                             # a pointer-opened popover holds the focus; its items show focus
    "#page .ftree .treenode": ".treenode:focus-visible > .treecontent",
    "#page .quad .qtile:focus": ".qtile:focus-visible",
    ".dock-resize:focus-visible": ".dock-resize:focus-visible::after",
    ".sb-resize:focus-visible": ".sb-resize:focus-visible::after",
    ".tk-mode:focus": ".tk-mode:focus-visible",
    "body[data-shell] #main": None,                    # the scroll container takes programmatic focus only
}


def _rules(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", text)]


def test_every_icon_button_names_itself():
    bad = []
    for f in sorted(STATIC.rglob("*.js")):
        if "vendor" in f.parts:
            continue
        s = f.read_text()
        for m in re.finditer(r"el\('button',\s*\{", s):
            i, d = m.end(), 1
            while d and i < len(s):
                d += (s[i] == "{") - (s[i] == "}")
                i += 1
            props = s[m.end():i - 1]
            if re.search(r"class:\s*[^,]*\bicon\b", props) and "aria-label" not in props and "text:" not in props:
                bad.append(f"{f.name}:{s.count(chr(10), 0, m.start()) + 1}")
    assert not bad, "icon-only buttons without an aria-label: " + ", ".join(bad)


def test_the_scan_sees_the_icon_buttons():
    """Guards the guard: if the pattern stopped matching, the test above would pass for the wrong reason."""
    n = sum(len(re.findall(r"el\('button',\s*\{[^}]*class:\s*'[^']*\bicon\b", p.read_text())) for p in STATIC.rglob("*.js") if "vendor" not in p.parts)
    assert n >= 10


def test_no_outline_none_without_a_replacement():
    bad, seen = [], set()
    for css in sorted(STATIC.glob("*.css")):
        rules = _rules(css.read_text())
        for sel, body in rules:
            if not re.search(r"outline:\s*(none|0)\b", body):
                continue
            if ":not(:focus-visible)" in sel or re.search(r"box-shadow|border-color|background|outline-offset", body):
                continue
            key = next((k for k in OUTLINE_REPLACED if k in sel), None)
            if key is None:
                bad.append(f"{css.name}: {sel[:80]}")
                continue
            seen.add(key)
            repl = OUTLINE_REPLACED[key]
            if repl and not any(repl in s2 and re.search(r"outline:\s*2px|background:\s*var\(--sig\)", b2) for s2, b2 in rules):
                bad.append(f"{css.name}: {sel[:60]} has no replacement rule matching {repl}")
    assert not bad, "outline removed without a visible replacement: " + "; ".join(bad)
    assert seen, "the allowlist matched nothing: the scan is broken"


def test_reduced_motion_is_honoured_for_everything():
    tokens = (STATIC / "tokens.css").read_text()
    m = re.search(r"@media \(prefers-reduced-motion:\s*reduce\)\s*\{\s*\*,\s*\*::before,\s*\*::after\s*\{([^}]*)\}", tokens)
    assert m and "animation-duration" in m.group(1) and "transition-duration" in m.group(1), "tokens.css must cut every animation and transition for reduced motion"


# ---- the axe-core findings of the #45 pass (scripts/dev/axe-run.sh): what was fixed, pinned in the source so it does not come back ----

SECTION_HEADS = ("set-h", "mem-wing-name", "mem-k", "mem-sec", "mem-day", "empty-title", "pj-sec")


def _css_all():
    return "\n".join(p.read_text() for p in sorted(STATIC.glob("*.css")) + sorted(STATIC.glob("pages/*.css")))      # pages/*.css: the page sheets split out of pages.css (issue #103)


def _js_all():
    return "\n".join(p.read_text() for p in list(STATIC.glob("*.js")) + list(STATIC.glob("pages/*.js")))


def test_ended_chip_keeps_its_contrast():
    """axe color-contrast: the dim text of an ended state chip, faded by opacity .6, measured 3.4:1; .85 keeps it faded and 4.5:1 or better."""
    ops = [float(m.group(1)) for m in re.finditer(r"\.state\.ended\s*\{\s*opacity:\s*(\.?\d*\.?\d+)", _css_all())]
    assert ops, "no .state.ended opacity rule: the scan is broken"
    assert all(o >= 0.8 for o in ops), f".state.ended fades below 0.8: {ops}"


def test_issue_link_in_text_is_underlined():
    """axe link-in-text-block: the #N link on a task card sits in running text and is told apart by more than colour."""
    assert re.search(r"\.task a\.tk-issue-link\s*\{[^}]*text-decoration:\s*underline", _css_all())


def test_section_headings_sit_one_level_under_the_page_h1():
    """axe heading-order: these headings follow a page h1 (or a sheet's h2) directly, so they are h2; they are styled by class and every rule sets size and margin."""
    js = _js_all()
    for cls in SECTION_HEADS:
        assert not re.search(r"el\('h[3-6]',\s*\{\s*class:\s*'" + cls + r"\b", js), f"{cls} must not be an h3 to h6 under a page h1"
        assert re.search(r"el\('h2',\s*\{\s*class:\s*'" + cls + r"\b", js), f"{cls} should be built as an h2"
    # the kanban columns: home.js renderTasks and the shared board in components.js
    assert len(re.findall(r"class: 'col', 'data-col': key[^\n]*el\('h2'", js)) == 2
    assert not re.search(r"class: 'col', 'data-col': key[^\n]*el\('h3'", js)
    rules = _rules(_css_all())
    assert any(s.strip() == ".col > h2" and "font-size" in b and "margin" in b for s, b in rules)
    for cls in SECTION_HEADS:
        hits = [b for s, b in rules if re.search(r"\." + cls + r"\s*$", s.strip())]
        assert hits, f"no css rule for .{cls}"
        assert any("font-size" in b and "margin" in b for b in hits), f".{cls} must set its own size and margin (the h2 default would show otherwise)"


def test_the_sidebar_resize_strip_is_inside_a_landmark():
    """axe region: the strip used to be a bare child of #app, outside every landmark."""
    assert re.search(r"sb-resize-wrap'[^)]*role:\s*'region'[^)]*'aria-label'", (STATIC / "shell.js").read_text())
    assert ".sb-resize-wrap" in _css_all()


def test_agents_summary_strip_scrolls_with_a_focusable_stop():
    """axe scrollable-region-focusable: the Agents legend scrolls sideways at 390 px and holds no link, so the strip itself takes the focus."""
    assert re.search(r"'summary sumbar sumbar-static'[^}]*tabindex:\s*'0'", (STATIC / "pages" / "agents-page.js").read_text())


def test_usage_details_column_header_has_text():
    """axe empty-table-header: a header cell with only an aria-label is empty; the text is visually hidden instead."""
    usage = (STATIC / "pages" / "usage.js").read_text()
    assert "el('th', { 'aria-label': 'Details' })" not in usage
    assert re.search(r"el\('th',\s*\{\s*scope:\s*'col'\s*\},\s*el\('span',\s*\{\s*class:\s*'sr-only',\s*text:\s*'Details'", usage)
    assert re.search(r"\.sr-only\s*\{[^}]*clip", _css_all())


def test_charts_are_keyboard_reachable():
    """The cost bars, the Gantt and the heat grid are one tab stop each (Charts._kbd), no longer a pointer-only role=img, and their focus ring is drawn."""
    charts = (STATIC / "charts.js").read_text()
    assert len(re.findall(r"Charts\._kbd\(node, read,", charts)) == 3, "bars, gantt and heatmap each call Charts._kbd"
    for name in ("'bars'", "'gantt'", "'heat-grid'"):
        assert not re.search(r"class:\s*" + name + r"[^}]*role:\s*'img'", charts), f"{name} must not stay a role=img"
    kbd = charts[charts.index("Charts._kbd = function"):]
    kbd = kbd[:kbd.index("\n};")]
    for needle in ("'tabindex', '0'", "'aria-describedby'", "'data-kbd', 'chart'", "'Escape'"):
        assert needle in kbd, f"Charts._kbd lost {needle}"
    assert len(re.findall(r"class: 'chart-read dim', 'aria-live': 'polite'", charts)) == 3, "every readout stays the aria-live region"
    css = (STATIC / "charts.css").read_text()
    for root in (".bars", ".gantt", ".heat-grid"):
        assert re.search(re.escape(root) + r":focus-visible[^{]*\{[^}]*outline:\s*2px solid var\(--sig\)", css), f"{root} has no focus ring"
    # the page's own keys leave a focused chart alone
    assert "Keymap.inChart(e.target)" in (STATIC / "keymap.js").read_text()
    assert "[data-kbd=chart]" in (STATIC / "shell.js").read_text()
