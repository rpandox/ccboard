"""Pins between the board (app/nodes_work.py, app/nodes_epics.py) and the screens of issue #149 (app/static/pages/epic.js, app/static/components.js, app/static/demo/*.json): the
words the page paints are the words the board gives, every refusal word of the work route has a sentence on the page, every phase has a glyph and words, and the demo fixtures carry
the shapes the board really sends. Text and JSON checks only; no server, no network."""
import json
import re
from pathlib import Path

from app import nodes_epics, nodes_work

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
EPIC_JS = (STATIC / "pages" / "epic.js").read_text(encoding="utf-8")
COMPONENTS_JS = (STATIC / "components-tasks.js").read_text(encoding="utf-8")


def _js_has(src: str, sentence: str) -> bool:
    return sentence in src or sentence.replace("'", "\\'") in src


def test_the_page_paints_the_sentences_the_board_gives_as_plan():
    """The page paints the paragraph before the board answers, so its sentences must be the board's own."""
    for report in nodes_epics.REPORTS:
        st = {"report": report, "auto_pr": False, "close": False}
        assert _js_has(EPIC_JS, nodes_epics.work_plan(st)["report"]), report
    assert _js_has(EPIC_JS, nodes_epics.work_plan({"report": "phase", "auto_pr": True, "close": True})["auto_pr"])
    assert _js_has(EPIC_JS, nodes_epics.work_plan({"report": "phase", "auto_pr": False, "close": False})["auto_pr"])
    for st in ({"auto_pr": True, "close": True}, {"auto_pr": False, "close": True}, {"auto_pr": False, "close": False}):
        assert _js_has(EPIC_JS, nodes_epics.work_plan({"report": "phase", **st})["close"])


def test_the_page_offers_the_three_reports_the_board_knows_and_no_more():
    words = re.findall(r"\['(phase|summary|none)', '", EPIC_JS.split("const EP_REPORTS = ")[1].split("\n")[0])
    assert tuple(words) == nodes_epics.REPORTS


def test_every_refusal_word_of_the_work_route_has_a_sentence_on_the_page():
    """A refusal the page has no words for would read as a bare error. The claim's own words are said by epClaimSentence, the work's own by epWorkSentence."""
    for reason in nodes_work.REASONS:
        assert re.search(rf"\b{re.escape(reason)}:", EPIC_JS), f"no sentence for {reason}"
    for word in ("repo_not_here", "offline", "scope", "unconfirmed"):
        assert re.search(rf"\b{word}:", EPIC_JS), word


def test_every_phase_of_the_work_record_has_a_glyph_and_words_on_the_page_and_on_the_card():
    block_page = EPIC_JS.split("const EP_WORK_PHASE = ")[1].split("};")[0]
    block_card = COMPONENTS_JS.split("const TASK_EPIC = ")[1].split("};")[0]
    for phase in nodes_work.PHASES:
        assert re.search(rf"\b{phase}: \['", block_page), phase
        assert re.search(rf"\b{phase}: \['", block_card), phase


def _view_keys():
    rec = {"state": "active", "phase": "running", "ended": None, "owner": "o", "repo": "r", "number": 1, "claim_ref": None, "task_id": 3, "branch": "b", "alias": "a", "attempt": 1,
           "report": "phase", "auto_pr": False, "close": False, "pr_number": None, "created_at": 1.0}
    return set(nodes_work.view_of(rec))


def test_the_demo_work_records_have_the_keys_of_the_board_view():
    fx = json.loads((STATIC / "demo" / "epic.json").read_text(encoding="utf-8"))
    keys = _view_keys()
    works = [c["work"] for c in fx["details"]["example/website#147"]["children"] if c.get("work")]
    works += [v["work"] for var in fx["variants"].values() for v in (var.get("child") or {}).values() if v.get("work")]
    assert works
    for w in works:
        assert set(w) == keys
        assert w["phase"] in nodes_work.PHASES
    task = json.loads((STATIC / "demo" / "task-epic.json").read_text(encoding="utf-8"))
    for name, v in task.items():
        if name != "_note":
            assert set(v["epic"]) == keys, name
    state = json.loads((STATIC / "demo" / "state.json").read_text(encoding="utf-8"))
    assert set(next(t for t in state["tasks"] if t["id"] == 23)["epic"]) == keys


def test_the_demo_settings_and_plan_are_the_boards_defaults():
    fx = json.loads((STATIC / "demo" / "epic.json").read_text(encoding="utf-8"))
    d = fx["details"]["example/website#147"]
    assert (d["report"], d["auto_pr"], d["close"]) == ("phase", False, False)
    assert d["plan"] == nodes_epics.work_plan({"report": "phase", "auto_pr": False, "close": False})
    assert set(d["authors"]) == {"allowed", "epic", "epic_ok", "blocked", "text"}
    for c in d["children"]:
        assert {"author", "author_ok", "work", "phase"} <= set(c)


def test_edit_refusal_has_a_glyph_words_and_a_disabled_work_reason():
    assert "'data-edit': c.edit_state" in EPIC_JS
    assert "'blocked', 'unknown'" in EPIC_JS
    assert "text: '⚠ '" in EPIC_JS
    assert "off = 'edit'; text = epEditText(c)" in EPIC_JS
    fx = json.loads((STATIC / 'demo' / 'epic.json').read_text())
    assert any(c.get('edit_state') == 'blocked' for c in fx['details']['example/website#147']['children'])


def test_worker_setup_copy_reuses_owner_safety_and_options_link():
    from app.options import WORKER_SAFETY
    source = (STATIC / 'nodes-worker.js').read_text()
    assert _js_has(source, WORKER_SAFETY)
    assert 'Set it up in Settings > Options > Epics & worker' in source
    assert '#/settings?sec=options&group=epics' in source
    assert 'Nothing on this page turns it on' not in source
    assert 'CCBOARD_PAGE_WIDEN=1' in source
