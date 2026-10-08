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
