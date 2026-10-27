"""Runtime state must survive Blueprint subsetting, including cold lazy routes."""
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'app/static'
SCRIPT = ROOT / 'scripts/dev/blueprint_subset.py'
spec = importlib.util.spec_from_file_location('blueprint_subset', SCRIPT)
bp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bp)


def test_generator_check_and_runtime_class_families():
    subprocess.run([sys.executable, str(SCRIPT), '--check'], check=True, capture_output=True, text=True)
    used = bp.used_classes()
    prefixes = set()
    # Grep every JS source, not only core.js or the eager bundle. This catches
    # el(), className, classList, string templates and concatenation alike.
    for path in STATIC.rglob('*.js'):
        if 'vendor' in path.parts:
            continue
        text = path.read_text()
        for literal in re.findall(r'''["'`]([^"'`\n]*)["'`]''', text):
            for name in re.findall(r'\bbp[56]-[\w-]*', literal):
                if name.endswith('-'):
                    prefixes.add(name)
                    assert any(name.startswith(p) for p in bp.DYNAMIC_PREFIXES), (path, name)
                else:
                    assert name in used or bp.dynamically_kept(name), (path, name)
        for prefix in re.findall(r'\bbp[56]-[\w-]*(?=\$\{)', text):
            assert any(prefix.startswith(p) for p in bp.DYNAMIC_PREFIXES), (path, prefix)
    assert 'bp5-icon-' in prefixes, 'the computed ic(name) family must be audited'
    # All dynamically named icon glyph rules survive; merely finding the
    # prefix in a comment or the keep list is insufficient.
    subset_classes = set(bp.CLASS.findall(bp.OUTPUT.read_text()))
    glyphs = set(re.findall(r'\.(bp[56]-icon-[\w-]+)::before', bp.SOURCE.read_text()))
    assert glyphs and glyphs <= subset_classes


def test_dynamic_states_attributes_and_nested_rules_survive():
    for version in (5, 6):
        button = f'bp{version}-button'
        for state in ('active', 'selected', 'disabled', 'minimal', 'outlined', 'intent-danger', 'icon-future-icon'):
            selector = f'.{button}.bp{version}-{state}'
            css = f'@media (min-width:1px){{@supports (display:grid){{{selector}{{color:red}}}}}}'
            result = bp.subset(css, {button})
            assert selector + '{color:red}' in result
            assert '@media (min-width:1px)' in result and '@supports (display:grid)' in result
        for attr in ('aria-pressed=true', 'aria-selected=true', 'disabled', f'class*=bp{version}-intent-'):
            selector = f'.bp{version}-future-widget[{attr}]'
            assert selector in bp.subset(selector + '{color:red}', set())
        assert bp.subset(f'.{button}.bp{version}-unused{{color:red}}', {button}) == ''


def test_new_computed_family_fails_check(tmp_path, monkeypatch):
    (tmp_path / 'new.js').write_text("el('div', {class: 'bp6-new-' + state}); const c = `bp5-other-${name}`;")
    monkeypatch.setattr(bp, 'STATIC', tmp_path)
    assert bp.uncovered_prefixes() == [('new.js', 'bp5-other-'), ('new.js', 'bp6-new-')]


def test_shared_states_are_available_without_visiting_another_page():
    # Usage reuses the Project switcher, but does not load Project's CSS.
    shared = (STATIC / 'pages.css').read_text()
    assert '#page .pj-switch {' in shared
    assert '#page .pj-repo-btn[aria-pressed=true]' in shared
    assert '#page .pj-toolbar button[aria-pressed=true]' in shared
    assert '#page .seg-btn[aria-pressed=true]' in shared
    assert '#page .seg-ctl {' in shared
    assert '#page .unote {' in shared
    assert '.tm-status {' in shared and '.tm-actions > button { width:100%' in shared
    assert 'html.force-coarse #page .seg-btn { min-height:var(--tap); }' in shared
    task_css = (STATIC / 'pages/tasks.css').read_text()
    for selector in ('.task .tk-issue {', '.task a.tk-issue-link {', '.task .tk-epic {'):
        assert selector in task_css
