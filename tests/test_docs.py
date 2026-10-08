"""Links in the docs resolve (issue #23): every in-page anchor matches a heading under GitHub's slug rules,
and every relative link points at a file in the repository. Text checks only; no git, no network."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ["README.md", "ROADMAP.md", "CONTRIBUTING.md", "CHANGELOG.md"]
LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^(```|~~~)")


def _text_lines(path):
    """Lines outside fenced code blocks."""
    out, fenced = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line.strip()):
            fenced = not fenced
            continue
        if not fenced:
            out.append(line)
    return out


def github_slug(title):
    """GitHub's heading anchor: lower case, drop everything but letters, digits, spaces, hyphens and underscores,
    spaces become hyphens. Inline code and emphasis markers are removed first."""
    t = re.sub(r"`", "", title)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = t.lower()
    t = re.sub(r"[^\w\- ]", "", t)
    return t.replace(" ", "-")


def anchors_of(path):
    seen, slugs = {}, set()
    for line in _text_lines(path):
        m = HEADING.match(line)
        if not m:
            continue
        base = github_slug(m.group(1))
        n = seen.get(base, 0)
        slugs.add(base if n == 0 else f"{base}-{n}")
        seen[base] = n + 1
    return slugs


def _links(path):
    for line in _text_lines(path):
        for target in LINK.findall(line):
            yield target


@pytest.mark.parametrize("doc", DOCS)
def test_in_page_anchors_resolve(doc):
    path = ROOT / doc
    if not path.exists():
        pytest.skip(f"{doc} is not in the repository")
    slugs = anchors_of(path)
    bad = []
    for target in _links(path):
        if target.startswith("#") and target[1:] not in slugs:
            bad.append(target)
    assert not bad, f"{doc}: anchors with no heading: {bad}"


@pytest.mark.parametrize("doc", DOCS)
def test_relative_links_point_at_repository_files(doc):
    path = ROOT / doc
    if not path.exists():
        pytest.skip(f"{doc} is not in the repository")
    bad = []
    for target in _links(path):
        if re.match(r"^[a-z]+:", target) or target.startswith("#"):
            continue
        file_part = target.split("#", 1)[0]
        if file_part and not (path.parent / file_part).exists():
            bad.append(target)
    assert not bad, f"{doc}: relative links to missing files: {bad}"


def test_github_slug_rules():
    assert github_slug("Which hooks the board ignores") == "which-hooks-the-board-ignores"
    assert github_slug("Hooks v2, notifications with context, /command /prompt /resize (v0.5.7)") == \
        "hooks-v2-notifications-with-context-command-prompt-resize-v057"
    assert github_slug("CI in your repos (backup branches)") == "ci-in-your-repos-backup-branches"
