"""Links in the docs resolve (issues #23 and #244): in every Markdown file of the docs (tests/docs_util.py: README.md, CONTRIBUTING.md,
ROADMAP.md, CHANGELOG.md, SECURITY.md, deploy/README.md and docs/**/*.md, those that exist), every in-page anchor matches a heading under
GitHub's slug rules, every relative link points at a file in the repository, and every image link points at an image that exists.
Text checks only; no network."""
import re

import pytest

from tests.docs_util import ROOT, all_docs_text, doc_paths, read_doc, section, settings_rows

DOCS = doc_paths()
LINK = re.compile(r"\]\(([^)\s]+)\)")
IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
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
    slugs = anchors_of(path)
    bad = []
    for target in _links(path):
        if target.startswith("#") and target[1:] not in slugs:
            bad.append(target)
    assert not bad, f"{doc}: anchors with no heading: {bad}"


@pytest.mark.parametrize("doc", DOCS)
def test_relative_links_point_at_repository_files(doc):
    path = ROOT / doc
    bad = []
    for target in _links(path):
        if re.match(r"^[a-z]+:", target) or target.startswith("#"):
            continue
        file_part = target.split("#", 1)[0]
        if file_part and not (path.parent / file_part).exists():
            bad.append(target)
    assert not bad, f"{doc}: relative links to missing files: {bad}"


@pytest.mark.parametrize("doc", DOCS)
def test_image_links_point_at_files_that_exist(doc):
    path = ROOT / doc
    bad = []
    for line in _text_lines(path):
        for target in IMAGE.findall(line):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target):
                continue
            file_part = target.split("#", 1)[0].split("?", 1)[0]
            if not file_part or not (path.parent / file_part).is_file():
                bad.append(target)
    assert not bad, f"{doc}: image links to missing files: {bad}"


def test_the_docs_list_covers_the_files_that_exist():
    assert {"README.md", "CONTRIBUTING.md", "ROADMAP.md", "CHANGELOG.md", "deploy/README.md", "docs/memory-api.md"} <= set(DOCS)
    assert all(rel.endswith(".md") for rel in DOCS)
    text = all_docs_text()
    assert read_doc("README.md") in text and read_doc("deploy/README.md") in text


def test_image_link_pattern():
    assert IMAGE.findall('![a](img/a.png) and ![b](<img/b.png> "title") ![c](https://x.test/c.png)') == ["img/a.png", "img/b.png", "https://x.test/c.png"]
    assert IMAGE.findall("[not an image](img/a.png)") == []


def test_docs_helpers_slice_the_way_the_readers_expect():
    text = "# T\n\n## A\n\nx\n\n### A1\n\ny\n\n## B\n\nz\n"
    assert section(text, "## A") == "## A\n\nx\n\n### A1\n\ny\n\n"
    assert section(text, "### A1") == "### A1\n\ny\n\n"
    rows = settings_rows("### Settings\n\n| `CCBOARD_X` | a |\n| not a name | b |\n\n### What install.sh sets up\n\n| `CCBOARD_Y` | c |\n")
    assert rows == {"CCBOARD_X"}


def test_github_slug_rules():
    assert github_slug("Which hooks the board ignores") == "which-hooks-the-board-ignores"
    assert github_slug("Hooks v2, notifications with context, /command /prompt /resize (v0.5.7)") == \
        "hooks-v2-notifications-with-context-command-prompt-resize-v057"
    assert github_slug("CI in your repos (backup branches)") == "ci-in-your-repos-backup-branches"


@pytest.mark.parametrize("doc", ["README.md", "CHANGELOG.md"])
def test_claim_recovery_documents_wait_bound_and_intentional_read_row(doc):
    text = read_doc(doc)
    assert re.search(r'`wait_s`[^.]*upper bound[^.]*not a measured queue time', text)
    assert re.search(r'one read-only GET row[^.]*`epic_claim_status`[^.]*`tasks`[^.]*on purpose', text)


def test_security_bullets_precede_settings_permission_subsection():
    security = read_doc('README.md').split('## Security model\n')[1].split('\n## ')[0]
    general, settings = security.split('### Settings permission classes\n')
    assert '- **Optional local sign-in' in general
    assert '- Codex:' in general
    assert not any(line.startswith('- ') for line in settings.splitlines())
