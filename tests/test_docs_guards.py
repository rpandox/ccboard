"""Guards for the documentation (issue #244, slice 4): the rules that keep the docs readable by a stranger once the restructure is done.

What is guarded where, so that nothing is checked twice:
  - tests/test_docs.py: README.md is 200 lines or fewer and 25 KB or less, has its outline and no settings table; every page of docs/ is
    linked from docs/README.md, starts with an H1 and a line for its reader, and links back; every link, anchor and image resolves;
    every row of docs/moved.md points at a file and a heading that exist.
  - tests/test_docs_uninstall.py: the uninstall page against the installers.   tests/test_env_keys.py: the settings table against install.sh.
  - this file: labels in headings, size of a Markdown file, an H1 on every page, the word "the box", the path a message in the code names.
Text checks only: no network, no tools, no git.
"""
import re

import pytest

from tests.docs_util import ROOT, doc_paths, headings, read_doc

# What an issue tracker leaves in a heading: "(issue #134; phase P2)", "(v0.5.13)", "(#84)", "Team P3". The reader should not need the tracker.
LABEL = re.compile(r"\bissues? #\d|\bphase P\d|\bTeam P\d|\(v\d+\.\d|\bv\d+\.\d+(\.\d+)?\b|\(#\d+\)|\bWhat v\d+\.\d+ (adds|does)")
SCOPE = [rel for rel in doc_paths() if rel == "README.md" or rel.startswith("docs/")]
PAGES = [rel for rel in SCOPE if rel.startswith("docs/")]

BOX_DEFINITION = 'In this page, "the box" means the machine that runs the board.'


def labelled(heading):
    return LABEL.search(heading) is not None


def test_the_label_pattern_catches_what_the_headings_used_to_carry():
    for old in ("#### Pairing two boards (issue #135; phase P3)", "### Notifications (v0.5.7, v0.5.18)", "### Auto-continue (#84)",
                "### Team: publish my status (issue #198; Team P2)", "## What v0.4 adds: autonomy & fleet", "## What v0.1 does",
                "#### Routing an epic to a node (issues #150 and #152; phase P15)", "# Memory API (v0.5.20): the board's claude-mem proxy"):
        assert labelled(old), old
    for plain in ("### Hooks v2", "### Install on Ubuntu 22.04 or 24.04", "#### Several devices", "### 3. Install the board inside the distro",
                  "## Windows 11 with WSL2", "### Notifications across nodes"):
        assert not labelled(plain), plain


@pytest.mark.parametrize("rel", SCOPE)
def test_no_heading_carries_an_issue_phase_or_version_label(rel):
    """The numbers live in a Background line at the end of the page, not in the heading (and not in its anchor)."""
    bad = [h for h in headings(read_doc(rel)) if labelled(h)]
    assert not bad, f"{rel}: move the label to the page's Background line: {bad}"


@pytest.mark.parametrize("rel", PAGES)
def test_a_background_line_is_the_last_paragraph_of_its_page(rel):
    paragraphs = [p for p in read_doc(rel).strip().split("\n\n") if p.strip()]
    inside = [i for i, p in enumerate(paragraphs) if p.startswith("**Background.**")]
    assert len(inside) <= 1 and (not inside or inside[0] == len(paragraphs) - 1), f"{rel}: one Background line, and it comes last"


@pytest.mark.parametrize("rel", doc_paths())
def test_no_markdown_file_nears_githubs_limit(rel):
    """GitHub cuts a Markdown file off at 500 KiB; the guard is 400 KB so a page is split before it is cut."""
    assert (ROOT / rel).stat().st_size <= 400 * 1000, f"{rel} is over 400 KB: split it"


@pytest.mark.parametrize("rel", PAGES)
def test_every_page_of_docs_starts_with_an_h1(rel):
    """test_docs.py checks the H1 and the reader line of the pages that came out of the README; this covers the three that did not."""
    first = read_doc(rel).splitlines()[0]
    assert re.match(r"^# \S", first), f"{rel}: the first line is the page title"


def test_dropped_prose_is_not_back_as_a_what_vx_adds_block():
    """Slice 4 deleted the release-note blocks that repeated CHANGELOG.md and ROADMAP.md; a bullet that stayed lives under a plain topical heading."""
    for rel in SCOPE:
        assert not [h for h in headings(read_doc(rel)) if re.search(r"What v\d+\.\d+ (adds|does)", h)], rel


# ---------------------------------------------------------------- "the box"
# "The box" is the owner's word for the machine that runs the board. README.md never uses it (tests/test_docs.py). A page of docs/ may use it
# only if it says what it means in its first lines, or if the use is in docs/maintainers/ (written for the maintainer) or in an
# "Evidence and open checks" block (a record of what was run on which machine).

def _outside_evidence(text):
    return re.sub(r"^## Evidence and open checks\n.*?(?=^## |\Z)", "", text, flags=re.M | re.S)


def _says_the_box(text):
    """A use of 'the box' outside an Evidence block and outside the definition line itself (the definition is not a use)."""
    return re.search(r"\bthe box\b", _outside_evidence(text).replace(BOX_DEFINITION, ""), re.I) is not None


def test_the_box_rule_sees_a_use_and_lets_evidence_blocks_alone():
    assert _says_the_box("# T\n\nThe box runs it.\n") and _says_the_box("# T\n\nOn the box, run it.\n")
    assert not _says_the_box("# T\n\n## Evidence and open checks\n\nMeasured on the box.\n")
    assert _says_the_box("# T\n\n## Evidence and open checks\n\nx\n\n## Next\n\nthe box\n")
    assert not _says_the_box(f"# T\n\n{BOX_DEFINITION}\n\nNothing else.\n") and _says_the_box(f"# T\n\n{BOX_DEFINITION}\n\nRun it on the box.\n")


@pytest.mark.parametrize("rel", [p for p in PAGES if not p.startswith("docs/maintainers/")])
def test_a_page_says_the_box_only_where_it_defines_it(rel):
    text = read_doc(rel)
    if _says_the_box(text):
        assert BOX_DEFINITION in "\n".join(text.splitlines()[:10]), f"{rel}: say what 'the box' means in the first lines, or use plain words"


# ---------------------------------------------------------------- what the code points at
CODE_FILES = sorted(p for pat in ("app/*.py", "app/agents/*.py", "scripts/*", "bin/*") for p in ROOT.glob(pat) if p.is_file()) + [ROOT / "install.sh"]
DOC_PATH = re.compile(r"\bdocs/[A-Za-z0-9_./-]+?\.md(?![A-Za-z0-9])")
README_SECTION = re.compile(r"README(?:\.md)?(?:'s)?,? (?:section |Updating|Restoring|Security|Troubleshooting|Uninstall|Settings|Install|Development)|[Tt]he README's [A-Za-z]+ section")


def _code_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def test_every_docs_path_a_message_or_comment_names_exists():
    """The message strings of the Doctor and the installers name `docs/...md` paths (a path, not a heading, so a heading edit cannot break it)."""
    assert len(CODE_FILES) > 20
    missing = sorted({f"{p.relative_to(ROOT)}: {m}" for p in CODE_FILES for m in DOC_PATH.findall(_code_text(p)) if not (ROOT / m).is_file()})
    assert not missing, missing


def test_no_message_names_a_readme_section_by_its_title():
    """The README is a short front page now; a message that says 'README, Updating' sends the reader to a heading that is not there."""
    found = sorted(f"{p.relative_to(ROOT)}: {m.group(0)}" for p in CODE_FILES for m in README_SECTION.finditer(_code_text(p)))
    assert not found, found


def test_the_readme_section_pattern_sees_the_old_wording():
    assert README_SECTION.search("Update Claude Code (README, Updating)") and README_SECTION.search("README, Restoring from a backup")
    assert README_SECTION.search("The README's macOS section") and not README_SECTION.search("see docs/upgrade.md#updating-claude-code")


# ---------------------------------------------------------------- what is still to do by a person
def test_the_two_fresh_machine_walkthroughs_are_listed_as_not_done():
    """The walkthroughs need a real, empty machine and a person who did not write the pages; the index says so and does not claim them."""
    index = read_doc("docs/README.md")
    todo = index[index.index("## Still to do by a person"):]
    assert "fresh Linux install" in todo and "fresh Mac install" in todo and "have not been done" in todo
    assert "Ubuntu 24.04" in todo and "Apple silicon" in todo
