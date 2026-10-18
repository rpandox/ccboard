"""One place the tests read the documentation from (issue #244, slice 0).

A test that pins a sentence, a table or a heading of the docs goes through these helpers, so moving a section to another page changes a path
in one place instead of the text-slicing in a dozen tests. Text checks only: no git, no network.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Where the sections of the former README live (issue #244, slice 1). A test that pins a sentence reads the page its section moved to by
# these names; moving a section again changes one line here. The JS twin is tests/js/docs-util.mjs.
INSTALL_LINUX = "docs/install/linux.md"
INSTALL_MACOS = "docs/install/macos.md"
INSTALL_WSL2 = "docs/install/windows-wsl2.md"
INSTALL_DOCKER = "docs/install/docker.md"
CONFIGURATION = "docs/configuration.md"
BACKUP = "docs/backup-and-restore.md"
SECURITY = "docs/security.md"
UNINSTALL = "docs/uninstall.md"
DEVELOPMENT = "docs/development.md"
CLAUDE_MEM = "docs/guide/claude-mem.md"
MCP_PAGE = "docs/guide/mcp.md"
HOOKS = "docs/reference/hooks.md"
NODES_OVERVIEW = "docs/nodes/overview.md"
NODES_PLATFORMS = "docs/nodes/platforms.md"
NODES_EPICS = "docs/nodes/epics.md"
DEVICES_WITHOUT_BOARD = "docs/nodes/device-check.md"

# The two files of docs/ that never were part of the README.
NOT_FROM_README = ("docs/memory-api.md", "docs/security-audit-2026-10.md")

# The Markdown files tests/test_docs.py covers. A file that is not there yet is skipped; docs/ is searched at any depth.
TOP_DOCS = ("README.md", "CONTRIBUTING.md", "ROADMAP.md", "CHANGELOG.md", "SECURITY.md", "deploy/README.md")


def doc_paths():
    """Every Markdown file of the docs, as paths relative to ROOT in a stable order, for the ones that exist."""
    found = [rel for rel in TOP_DOCS if (ROOT / rel).is_file()]
    docs = ROOT / "docs"
    if docs.is_dir():
        found += sorted(p.relative_to(ROOT).as_posix() for p in docs.rglob("*.md"))
    return found


def read_doc(rel):
    """The text of one file of the repository, `rel` relative to ROOT."""
    return (ROOT / rel).read_text(encoding="utf-8")


def all_docs_text():
    """Every Markdown file of the docs, concatenated (a blank line between files)."""
    return "\n\n".join(read_doc(rel) for rel in doc_paths())


def section(text, heading):
    """The text from `heading` to the next heading of the same or a higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    start = text.index(heading + "\n")
    body = start + len(heading)
    m = re.search(rf"^#{{1,{level}}} ", text[body:], re.M)
    return text[start:body + (m.start() if m else len(text))]


def readme_text():
    """What the README held before slice 1: README.md and every page of docs/ that came out of it, concatenated. For a check that a
    sentence is somewhere, or is nowhere, in the documentation of the product (not ROADMAP, CHANGELOG or the audit)."""
    rels = ["README.md"] + [rel for rel in doc_paths() if rel.startswith("docs/") and rel not in NOT_FROM_README and rel != "docs/moved.md"]
    return "\n\n".join(read_doc(rel) for rel in rels)


def nodes_text():
    """The nodes section of the former README: every page under docs/nodes/, concatenated in file-name order."""
    return "\n\n".join(read_doc(p.relative_to(ROOT).as_posix()) for p in sorted((ROOT / "docs" / "nodes").glob("*.md")))


def settings_span(text):
    """Every settings table of docs/configuration.md: `### Settings` (its page-options subsection and one `####` table per group, from
    "Most people set at most these" to "Platform specific") and `### Advanced and testing variables`. `section` stops at the next heading
    of the same or a higher level, so a new `####` group table is read without a change here; a new `###` heading is not."""
    return section(text, "### Settings") + section(text, "### Advanced and testing variables")


def settings_rows(text):
    """Names that open a row of any settings table (all the groups and the advanced table): a line that starts with a back-ticked NAME in the first cell."""
    return set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \|", settings_span(text), re.M))


def headings(text):
    """The heading lines of a Markdown text (`#` to `######`), leaving out lines inside fenced code blocks."""
    out, fenced = [], False
    for line in text.splitlines():
        if re.match(r"^(```|~~~)", line.strip()):
            fenced = not fenced
        elif not fenced and re.match(r"^#{1,6}\s+\S", line):
            out.append(line)
    return out
