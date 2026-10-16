"""The demo of the New epic sheet (issue #158) is held to the board. app/static/demo/epic.json carries a `new` section: an epic as the sheet sends it (`sample`) and what the board's
preview answers for it (`preview`). This test makes the board produce the same preview now, so the fixture cannot drift from app/nodes_epics.py; tests/js/epic-new.test.mjs holds the
demo's own builder (pages/epic-new.js) to the same fixture. The sample uses all four tiers, a depends-on row, and text the template must keep harmless."""
import json
from pathlib import Path

from app import nodes_epics

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
FX = json.loads((STATIC / "demo" / "epic.json").read_text(encoding="utf-8"))["new"]


def test_the_demo_preview_is_what_the_board_answers_for_the_sample():
    live = nodes_epics.preview(FX["sample"], FX["sample"]["repo"])
    live.pop("private")
    live.pop("writes_on")
    assert live == FX["preview"], "regenerate the `new` section of app/static/demo/epic.json from nodes_epics.preview()"


def test_the_sample_uses_every_tier_and_a_dependency_and_a_text_that_must_stay_harmless():
    kids = FX["sample"]["children"]
    assert {c["tier"] for c in kids} == set(nodes_epics.TIERS)
    assert any(c["depends_on"] for c in kids)
    p = FX["preview"]
    assert any("&lt;b>" in c["body"] for c in p["children"]), "a < in typed text is made harmless"
    assert any("\\# kept as text" in c["body"] for c in p["children"]), "a line that starts with # is not a heading"
    assert "\\# Not a heading" in p["parent"]["body"]


def test_the_demo_fixture_names_no_bypass_and_no_host():
    text = json.dumps(FX).lower()
    for word in ("bypasspermissions", "--dangerously", "danger-full-access", "--yolo", ".ts.net"):
        assert word not in text
