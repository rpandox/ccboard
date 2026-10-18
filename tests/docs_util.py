"""One place the tests read the documentation from (issue #244, slice 0).

A test that pins a sentence, a table or a heading of the docs goes through these helpers, so moving a section to another page changes a path
in one place instead of the text-slicing in a dozen tests. Text checks only: no git, no network.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

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


def settings_span(text):
    """The settings section as written today: from `### Settings` up to `### What install.sh sets up`."""
    return text[text.index("### Settings"):text.index("### What install.sh sets up")]


def settings_rows(text):
    """Names that open a row of the settings tables: a line that starts with a back-ticked NAME in the first cell."""
    return set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \|", settings_span(text), re.M))
