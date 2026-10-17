"""CHANGELOG.md keeps one honest entry per shipped phase (issue #53).

Pure text checks: no git (CI checks out at depth 1)."""
import datetime
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"

HEADING = re.compile(r"^## (v\d+\.\d+\.\d+[a-z]?(?:-[a-z]+)?) - (\d{4}-\d{2}-\d{2})$")
BULLET_HEADINGS = ("### Added", "### Changed", "### Fixed")


def _entries():
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    entries, cur = [], None
    for line in lines:
        if line.startswith("## "):
            if cur:
                entries.append(cur)
            cur = {"heading": line, "lines": []}
        elif cur is not None:
            cur["lines"].append(line)
    if cur:
        entries.append(cur)
    return entries


def test_first_heading_is_an_entry_with_version_and_date():
    entries = _entries()
    assert entries, "CHANGELOG.md has no entries"
    assert HEADING.match(entries[0]["heading"]), entries[0]["heading"]


def test_every_heading_is_well_formed():
    for e in _entries():
        assert HEADING.match(e["heading"]), f"malformed heading: {e['heading']!r}"


def test_versions_are_not_repeated():
    versions = [HEADING.match(e["heading"]).group(1) for e in _entries()]
    dup = sorted({v for v in versions if versions.count(v) > 1})
    assert not dup, f"repeated versions: {dup}"


def test_dates_are_not_future_and_not_increasing_downwards():
    today = datetime.date.today()
    dates = []
    for e in _entries():
        d = datetime.date.fromisoformat(HEADING.match(e["heading"]).group(2))
        assert d <= today, f"future date in {e['heading']!r}"
        dates.append(d)
    for newer, older in zip(dates, dates[1:]):
        assert older <= newer, "entries must be newest first"


def test_every_entry_has_one_upgrade_line_and_few_bullets():
    for e in _entries():
        upgrades = [l for l in e["lines"] if l.startswith("Upgrade: ")]
        assert len(upgrades) == 1, f"{e['heading']!r} needs exactly one Upgrade: line"
        # Setting changes can explain their migration instead of prescribing a rerun.
        assert re.fullmatch(r"Upgrade: \S.*\.", upgrades[0]), upgrades[0]
        bullets = 0
        for l in e["lines"]:
            if l.startswith(BULLET_HEADINGS):
                bullets = 0
            elif l.startswith("- "):
                bullets += 1
                assert bullets <= 5, f"{e['heading']!r} has more than five bullets in one section"


def test_pending_pairing_upgrade_explains_mixed_version_support():
    entry = next(e for e in _entries() if e["heading"].startswith("## v0.5.77 "))
    upgrade = next(line for line in entry["lines"] if line.startswith("Upgrade: "))
    assert "a new pairing between an updated and an older board still works" in upgrade.lower()


def test_settings_widening_release_describes_added_switch_and_file_only_options():
    entry = next(e for e in _entries() if e['heading'].startswith('## v0.5.83 '))
    assert '### Added' in entry['lines']
    bullets = [line for line in entry['lines'] if line.startswith('- ')]
    assert len(bullets) <= 5
    assert any('CCBOARD_PAGE_WIDEN' in line for line in bullets)
    assert any('file-only' in line.lower() and 'CCBOARD_CODEX_HOOK_TRUST' in line and 'CCBOARD_NODE_TAGS' in line for line in bullets)
