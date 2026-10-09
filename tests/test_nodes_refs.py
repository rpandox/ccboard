"""Nodes epic P1, issue #137: the grammar for a thing on another node (app/nodes.py parse_ref, format_ref, resolve) and the shape of the migration
that goes with it (tests/test_db_migrations.py has the column tests).

tests/fixtures/node_refs.json is the shared table: the JavaScript helpers (app/static/nodes.js Ref.parse) must read it the same way."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import nodes, projects
from app.db import DB

TABLE = json.loads((Path(__file__).parent / "fixtures" / "node_refs.json").read_text(encoding="utf-8"))
SESSION = "shop--api--t-fix"


@pytest.mark.parametrize("row", TABLE["accept"], ids=lambda r: r["text"])
def test_accepted_refs_parse_to_a_handle_and_the_rest(row):
    assert nodes.parse_ref(row["text"]) == (row["handle"], row["rest"])
    r = nodes.parse(row["text"])
    assert (r.kind, r.handle, r.rest) == (row["kind"], row["handle"], row["rest"])


@pytest.mark.parametrize("row", TABLE["accept"], ids=lambda r: r["text"])
def test_a_ref_round_trips_to_the_text_it_came_from(row):
    assert nodes.format_ref(row["handle"], row["rest"], row["kind"]) == row["text"]


@pytest.mark.parametrize("text", TABLE["refuse"], ids=repr)
def test_refused_refs_raise_a_value_error(text):
    with pytest.raises(ValueError):
        nodes.parse_ref(text)
    with pytest.raises(nodes.RefError):
        nodes.parse(text)


@pytest.mark.parametrize("text", [None, 42, b"box:1", ["box:1"], {"box": 1}, "x" * 201])
def test_things_that_are_not_text_are_refused(text):
    with pytest.raises(nodes.RefError):
        nodes.parse(text)


def test_a_bare_ccboard_session_name_is_a_local_session():
    assert nodes.parse_ref(SESSION) == (None, SESSION)
    assert nodes.parse(SESSION).kind == "session"
    assert nodes.format_ref(None, SESSION) == SESSION


@pytest.mark.parametrize("text", ["shop", "shop--api", "shop--api--", "a--b--c--d", "shop--api--t fix", "shop--api--t.fix", "-a--b--c", "box/shop--api"])
def test_a_tmux_part_that_the_name_validator_refuses_is_refused(text):
    with pytest.raises(nodes.RefError):
        nodes.parse(text)


def test_the_handle_rule_is_the_one_written_in_the_issue():
    assert nodes.valid_handle("box") and nodes.valid_handle("a") and nodes.valid_handle("0") and nodes.valid_handle("a-b-9")
    assert nodes.valid_handle("x" * 31) and not nodes.valid_handle("x" * 32)
    for bad in ("", "Box", "-a", "a_b", "a.b", "a b", "a\n", "local", None, 7):
        assert not nodes.valid_handle(bad), bad


def test_local_is_never_written_into_a_ref():
    for text in (f"local/{SESSION}", "local:1"):
        with pytest.raises(nodes.RefError):
            nodes.parse_ref(text)
    with pytest.raises(nodes.RefError):
        nodes.format_ref("local", SESSION)


def test_a_session_and_a_task_with_the_same_text_on_two_nodes_are_two_refs():
    a, b = nodes.parse(f"box/{SESSION}"), nodes.parse(f"lab/{SESSION}")
    assert a != b and a.rest == b.rest and a.handle != b.handle
    assert nodes.parse(SESSION) not in (a, b), "the local session is a third thing"
    assert nodes.parse("box:7") != nodes.parse("lab:7")


# ---------------------------------------------------------------- resolve

@pytest.fixture
def handle_db(tmp_path):
    d = DB(tmp_path / "ccboard.db")
    yield d
    d.conn.close()


def test_resolve_of_local_is_none_and_never_touches_the_registry():
    assert nodes.resolve("local", db=object()) is None


def test_resolve_of_an_unknown_handle_is_a_404_and_a_bad_handle_a_400(handle_db):
    with pytest.raises(projects.NotFound):
        nodes.resolve("box", db=handle_db)
    for bad in ("Box", "a/b", "", "x" * 32, None):
        with pytest.raises(projects.BadRequest):
            nodes.resolve(bad, db=handle_db)


def test_resolve_returns_the_registry_row_of_a_paired_node(handle_db):
    handle_db.kv_set(nodes.KV_PEERS, [{"handle": "box", "node_id": "ts:nBOX", "name": "box"}, {"handle": "Bad Handle", "node_id": "x"},
                                      "not a row", {"handle": "lab", "node_id": "n_0123456789abcdef"}])
    assert nodes.resolve("box", db=handle_db)["node_id"] == "ts:nBOX"
    assert nodes.resolve("lab", db=handle_db)["node_id"] == "n_0123456789abcdef"
    with pytest.raises(projects.NotFound):
        nodes.resolve("bad-handle", db=handle_db)
    assert [r["handle"] for r in nodes.registry(handle_db)] == ["box", "lab"], "a row without a valid handle is not a node"


def test_the_registry_reads_a_dict_of_rows_and_survives_junk(handle_db):
    assert nodes.registry(handle_db) == []
    handle_db.kv_set(nodes.KV_PEERS, {"p1": {"handle": "box"}})
    assert [r["handle"] for r in nodes.registry(handle_db)] == ["box"]
    handle_db.kv_set(nodes.KV_PEERS, "junk")
    assert nodes.registry(handle_db) == []

    class Broken:
        def kv_get(self, key):
            raise RuntimeError("db is gone")
    assert nodes.registry(Broken()) == []


def test_resolve_uses_the_running_boards_database_when_none_is_given(lite_client):
    from app import main
    main.db.kv_set(nodes.KV_PEERS, [{"handle": "box", "node_id": "ts:nBOX"}])
    assert nodes.resolve("box")["node_id"] == "ts:nBOX"
