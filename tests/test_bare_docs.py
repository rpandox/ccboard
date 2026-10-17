"""The README section "Devices without a board" quotes the board's own messages (issue #278).

Every quotation in its failure table must still occur in the code, so the words cannot drift. Text checks only."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
# Where a person can read a message: the module that builds them, the app, and the page scripts.
SOURCES = [ROOT / "app" / "bare.py", ROOT / "app" / "main.py", *sorted((ROOT / "app" / "static").glob("*.js"))]
PLACEHOLDERS = {"<device>": "{name}", "<user>": "{user}", "<agent>": "{agent}"}


def _section():
    start = README.index("\n#### Devices without a board\n")
    end = README.index("\n#### ", start + 10)
    return README[start:end]


def _table_quotes():
    """Backtick spans in the 'When something goes wrong' table's first three columns, placeholders turned into the code's own."""
    section = _section()
    table = section[section.index("| Scenario | What you see | What to do |"):]
    table = table[:table.index("\n\n")]
    quotes = []
    for line in table.splitlines()[2:]:
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        assert len(cells) == 3, line
        for cell in cells[1:]:
            quotes += re.findall(r"`([^`]+)`", cell)
    return quotes


def _code(span):
    for shown, source in PLACEHOLDERS.items():
        span = span.replace(shown, source)
    return span


def test_every_quoted_message_in_the_failure_table_occurs_in_the_code():
    quotes = _table_quotes()
    assert len(quotes) >= 20, quotes
    code = "\n".join(p.read_text(encoding="utf-8") for p in SOURCES)
    missing = [q for q in quotes if _code(q) not in code]
    assert not missing, f"README quotes words the code no longer has: {missing}"


def test_the_quoted_messages_come_mostly_from_the_device_module():
    bare = (ROOT / "app" / "bare.py").read_text(encoding="utf-8")
    quotes = _table_quotes()
    assert sum(_code(q) in bare for q in quotes) >= len(quotes) - 1


def test_the_failure_table_covers_the_plain_messages_of_the_check():
    quotes = " ".join(_table_quotes())
    for word in ("is offline", "asks for a Tailscale check", "tailnet rule denied", "timed out", "host key has changed",
                 "sudo, wheel or admin", "tmux is not installed", "PATH", "could not be read", "unconfirmed"):
        assert word in quotes, word


def test_the_readme_has_one_section_linked_from_the_several_devices_index():
    assert README.count("\n#### Devices without a board\n") == 1
    assert "(#devices-without-a-board)" in README
    assert "(#several-devices-issue-154-phase-p6)" in _section()
    section = _section()
    for topic in ("The tailnet rule", "The dedicated user", "10 seconds", "The state log", "Removal", "Not yet run on real hardware"):
        assert topic in section, topic
    assert "Tailscale SSH" in section and "Never put root" in section


def test_the_readme_and_the_demo_name_the_same_states():
    devices = json.loads((ROOT / "app" / "static" / "demo" / "bare.json").read_text(encoding="utf-8"))["devices"]
    states = {s["state"] for d in devices for s in d["sessions"]}
    assert states == {"working", "waiting", "dialog", "unconfirmed", "stopping"}
    demo = _section()[_section().index("**Demo.**"):]
    for word in ("working", "waiting", "needs you", "unconfirmed", "stopping"):
        assert word in demo, word
