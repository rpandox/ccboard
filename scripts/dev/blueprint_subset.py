#!/usr/bin/env python3
"""Regenerate the checked-in Blueprint CSS subset; stdlib only, no build step.

Keep literal bp5/bp6 classes from all app JS/CSS/HTML, including lazy pages,
el() arguments, className assignments, classList calls and template strings.
The documented dynamic keep list below handles computed classes and states.
Keep attribute-selector rules conservatively: state can change without adding
a class (aria-pressed, aria-selected, disabled, class*=bp5-intent-, etc.).
:not() classes exclude matches, so never require their use.
Keep unclassified selectors, font faces and keyframes conservatively. Preserve
rule order, declarations and enclosing media/supports rules; never edit vendor
blueprint.css. --check verifies the checked-in result without writing it.
"""
import argparse
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / 'app/static'
SOURCE = STATIC / 'vendor/blueprint/blueprint.css'
OUTPUT = SOURCE.with_name('blueprint-used.css')
CLASS = re.compile(r'\bbp[56]-[a-zA-Z0-9_-]+')

# core.js maps semantic names through INTENT/SEMANTIC and builds ic(name) by
# concatenation. Keep whole families, including names supplied by API data;
# looking only for a complete literal cannot prove those names are unused.
DYNAMIC_PREFIXES = tuple(f'bp{v}-{family}-' for v in (5, 6)
                         for family in ('icon', 'intent', 'elevation'))
# Widget modifiers may be toggled after construction, or supplied as semantic
# state. Keep them even when the initial HTML never contains them. Both versions
# are intentional: a vendor namespace upgrade must not silently lose states.
DYNAMIC_CLASSES = {f'bp{v}-{state}' for v in (5, 6) for state in (
    'active', 'selected', 'disabled', 'loading', 'minimal', 'outlined',
    'small', 'large', 'compact', 'fill', 'round', 'dark',
    'expanded', 'collapsed', 'checked', 'indeterminate',
)}


def dynamically_kept(name):
    return name in DYNAMIC_CLASSES or name.startswith(DYNAMIC_PREFIXES)


def uncovered_prefixes():
    """Find concatenated/template Blueprint prefixes, independently of DOM APIs.

    A new family must be documented above before --check can pass. A bare
    'bp5-' + value is deliberately rejected: enumerate its possible families
    instead of silently retaining the entire vendor sheet.
    """
    missing = set()
    for path in sorted(STATIC.rglob('*.js')):
        if 'vendor' in path.parts:
            continue
        for match in re.finditer(r'''["'`]([^"'`\n]*\bbp[56]-[\w-]*)(?:["']\s*\+|\$\{)''', path.read_text()):
            prefix = re.search(r'bp[56]-[\w-]*$', match[1])[0]
            if not dynamically_kept(prefix):
                missing.add((str(path.relative_to(STATIC)), prefix))
    return sorted(missing)


def used_classes():
    used = set(DYNAMIC_CLASSES)
    for path in sorted(STATIC.rglob('*')):
        if 'vendor' not in path.parts and path.suffix in {'.js', '.css', '.html'}:
            text = path.read_text()
            used.update(CLASS.findall(text))
    return used


def split_selectors(text):
    """Commas inside functional pseudo classes / attribute strings are not lists."""
    depth, start, quote, escape = 0, 0, '', False
    for i, char in enumerate(text):
        if escape:
            escape = False
        elif char == '\\':
            escape = True
        elif quote:
            if char == quote:
                quote = ''
        elif char in '\"\'':
            quote = char
        elif char in '([':
            depth += 1
        elif char in ')]':
            depth -= 1
        elif char == ',' and not depth:
            yield text[start:i].strip()
            start = i + 1
    yield text[start:].strip()


def blocks(css):
    """Yield top-level rule headers and bodies, respecting strings and nesting."""
    start, opening, depth, quote, escape = 0, None, 0, '', False
    for i, char in enumerate(css):
        if escape:
            escape = False
        elif char == '\\':
            escape = True
        elif quote:
            if char == quote:
                quote = ''
        elif char in '\"\'':
            quote = char
        elif char == ';' and not depth:
            yield css[start:i + 1].strip(), None
            start = i + 1
        elif char == '{':
            if not depth:
                opening = i
            depth += 1
        elif char == '}':
            depth -= 1
            if not depth:
                yield css[start:opening].strip(), css[opening + 1:i]
                start = i + 1
    if depth or quote or css[start:].strip():
        raise ValueError('Unrecognised CSS; inspect vendor changes before regenerating')


def subset(css, used):
    output = []
    for header, body in blocks(css):
        if body is None:
            output.append(header + '\n')
        elif header.startswith(('@media', '@supports', '@layer')):
            inner = subset(body, used)
            if inner.strip():
                output.append(header + '{\n' + inner + '}\n')
        elif header.startswith('@'):
            output.append(header + '{' + body + '}\n')
        else:
            keep = []
            for selector in split_selectors(header):
                positive = re.sub(r':not\([^()]*\)', '', selector)
                names = re.findall(r'\.(bp[56]-[\w-]+)', positive)
                if '[' in selector or all(n in used or dynamically_kept(n) for n in names):
                    keep.append(selector)
            if keep:
                output.append(',\n'.join(keep) + '{' + body + '}\n')
    return ''.join(output)


def generate():
    css = re.sub(r'/\*.*?\*/', '', SOURCE.read_text(), flags=re.S)
    return ('/* Generated by scripts/dev/blueprint_subset.py from blueprint.css.\n'
            '   Literal app classes + dynamic families + attribute rules; see script for matching rules. */\n'
            + subset(css, used_classes()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    missing = uncovered_prefixes()
    if missing:
        parser.exit(1, f'Undocumented dynamic Blueprint prefixes: {missing}\n'
                    'Add their possible families to DYNAMIC_PREFIXES before regenerating.\n')
    result = generate()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != result:
            parser.exit(1, 'Blueprint subset is stale: run python3 scripts/dev/blueprint_subset.py\n')
    else:
        OUTPUT.write_text(result)
    print(f'Blueprint subset: {len(result.encode())} bytes (full: {SOURCE.stat().st_size})')


if __name__ == '__main__':
    main()
