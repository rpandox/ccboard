"""The claude-mem worker contract (issue #20): the scrubbed answers of the box check (tests/fixtures/claude_mem_*.json, worker 13.31.0, and
tests/fixtures/claude_mem_13_35/, worker 13.35.0, issue #12)
validate against the same schema the proxy uses at runtime (memory_proxy.SHAPES / check_shape). Refreshing a fixture after a plugin
update surfaces drift here, in CI.

The live test runs the schema against the real worker on the box, GET only, and is skipped unless CCBOARD_TEST_CLAUDE_MEM=1:

    CCBOARD_TEST_CLAUDE_MEM=1 .venv/bin/pytest -q tests/test_memory_contract.py -k live
"""
import json
import os
from pathlib import Path

import pytest

from app import memory, memory_proxy as mp

FX = Path(__file__).resolve().parent / "fixtures"


def fx(name):
    return json.loads((FX / f"claude_mem_{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name,kind", [("observations", "observations"), ("summaries", "summaries"), ("search", "search"),
                                       ("observation", "observation"), ("projects", "projects"), ("stats", "stats"),
                                       ("timeline", "timeline"), ("timeline_anchor", "timeline")])
def test_every_fixture_has_the_shape_the_proxy_reads(name, kind):
    assert mp.check_shape(kind, fx(name)) is None


def test_the_fixtures_still_say_what_the_proxy_relies_on():
    o = fx("observations")
    assert set(o) >= {"items", "hasMore", "offset", "limit"} and o["items"][0]["created_at_epoch"] > 1e12, "epochs are milliseconds"
    for f in ("facts", "concepts", "files_read", "files_modified"):
        assert isinstance(o["items"][0][f], str) and isinstance(json.loads(o["items"][0][f]), list), f"{f} is a JSON-encoded string"
    assert "agent_type" not in o["items"][0], "the list rows carry no subagent marker"
    assert fx("observation")["agent_type"] == mp.SUBAGENT_TYPE and fx("search")["observations"][0]["agent_type"] == mp.SUBAGENT_TYPE
    assert fx("stats")["worker"]["version"] == "13.31.0"      # these fixtures are the 13.31.0 answers; TESTED_WORKER moved to 13.35.0 after a live GET check (its answers: claude_mem_13_35/)
    assert mp.compat(fx("stats")["worker"]["version"]) == "ok"
    s = fx("search")
    assert set(s) >= {"observations", "sessions", "prompts", "totalResults", "query"}
    assert isinstance(json.loads(s["sessions"][0]["files_edited"]), list), "search session rows carry files_edited as JSON text"
    rows = mp.parse_timeline(fx("timeline")["content"][0]["text"])
    assert [r["id"] for r in rows] == [5930, 5931, 5932]


def test_the_markdown_search_is_why_format_json_is_always_sent():
    assert mp.check_shape("search", fx("search_text")) is not None


def test_errors_arrive_as_documented():
    e = fx("errors")
    assert e["timeline_no_anchor_or_query"]["http_status"] == 200 and e["timeline_no_anchor_or_query"]["body"]["isError"] is True
    assert fx("timeline_error")["isError"] is True
    assert e["search_no_query_no_filter"]["http_status"] == 400 and e["search_no_query_no_filter"]["body"]["code"] == "INVALID_SEARCH_REQUEST"
    assert e["observation_not_found"]["http_status"] == 404
    assert mp.check_shape("observations", e["observations_unknown_project"]["body"]) is None


def test_the_health_fixtures_carry_what_memory_health_reads():
    assert fx("health")["status"] == "ok" and isinstance(fx("health")["activeSessions"], int)
    assert fx("readiness")["status"] == "ready"
    p = fx("processing_status")
    assert isinstance(p["isProcessing"], bool) and isinstance(p["queueDepth"], int)


def test_the_save_route_matches_save_note():
    s = fx("save")
    assert s["request"]["method"] == "POST" and s["request"]["path"] == memory.SAVE_PATH
    assert set(s["request"]["required"]) | set(s["request"]["optional"]) == {"text", "title", "project", "metadata"}
    assert s["response"]["success"] is True


def test_the_project_environments_shape_is_the_one_the_doctor_hint_writes():
    v = fx("project_environments")["value"]
    assert isinstance(v, list) and set(v[0]) == {"name", "patterns"} and isinstance(v[0]["patterns"], list)


# ------------------------------------------------------------------ worker 13.35.0 (box check V11, 2026-10-09)
# tests/fixtures/claude_mem_13_35/<name>.json: {_request, _http, _seconds, body}, two items per list, names scrubbed (<repo>, <uuid>). The
# markdown bodies keep only their length ("<markdown, 640 chars>"), so the timeline parser is exercised by the 13.31.0 fixtures above.

FX35 = FX / "claude_mem_13_35"


def fx35(name):
    return json.loads((FX35 / f"{name}.json").read_text(encoding="utf-8"))


def body35(name):
    return fx35(name)["body"]


def test_the_13_35_fixtures_are_all_there_and_wrapped():
    names = sorted(p.stem for p in FX35.glob("*.json"))
    assert names == ["health", "observations", "processing_status", "projects", "projects_claude", "readiness", "search_filter_only",
                     "search_json", "search_projects_list", "search_q_ignored", "search_text", "sessions", "stats", "summaries",
                     "timeline_anchor", "timeline_no_anchor"]
    for n in names:
        w = fx35(n)
        assert set(w) == {"_request", "_http", "_seconds", "body"} and w["_http"] == 200, n
        assert w["_request"].startswith("/") and w["_seconds"] >= 0, n


@pytest.mark.parametrize("name,kind", [("observations", "observations"), ("summaries", "summaries"), ("search_json", "search"),
                                       ("search_filter_only", "search"), ("search_projects_list", "search"), ("projects", "projects"),
                                       ("projects_claude", "projects"), ("stats", "stats"), ("timeline_anchor", "timeline")])
def test_every_13_35_answer_has_the_shape_the_proxy_reads(name, kind):
    assert mp.check_shape(kind, body35(name)) is None


def test_13_35_is_the_pinned_worker_and_compat_ok():
    ver = body35("stats")["worker"]["version"]
    assert ver == "13.35.0" == mp.TESTED_WORKER
    assert mp.compat(ver) == "ok"
    assert mp.compat("13.35.1") == "untested"


def test_the_names_the_box_check_found_on_13_35():
    # search: the text goes in `query`; `q` is ignored and the worker answers a markdown line about "undefined"
    assert mp.P_QUERY == "query"
    ignored = body35("search_q_ignored")
    assert mp.check_shape("search", ignored) is not None, "a markdown answer is not the JSON shape"
    assert fx35("search_q_ignored")["_request"].startswith("/api/search?q=")
    # format=json is what turns the markdown into rows
    assert mp.check_shape("search", body35("search_text")) is not None and mp.FORMAT_JSON == "json"
    assert set(body35("search_json")) == {"observations", "sessions", "prompts", "totalResults", "query"}
    # project and projects both work: the same JSON shape for one key and for a comma list
    assert "project=" in fx35("search_json")["_request"] and "projects=" in fx35("search_projects_list")["_request"]
    assert (mp.P_PROJECT, mp.P_PROJECTS) == ("project", "projects")
    # filter-only search (the palace's call) answers 200 with rows that carry the subagent marker
    assert fx35("search_filter_only")["_request"].count("query=") == 0 and "agent_type" in body35("search_filter_only")["observations"][0]
    # the list route has no subagent marker; the search rows and the subagent column do
    assert "agent_type" not in body35("observations")["items"][0]
    assert body35("search_json")["observations"][0]["agent_type"] == mp.SUBAGENT_TYPE == "workflow-subagent"
    assert body35("search_json")["observations"][0]["agent_id"]


def test_the_13_35_list_routes():
    o = body35("observations")
    assert set(o) == {"items", "hasMore", "offset", "limit"} and o["limit"] == 2
    assert [x["created_at_epoch"] for x in o["items"]] == sorted((x["created_at_epoch"] for x in o["items"]), reverse=True), "newest first"
    assert o["items"][0]["created_at_epoch"] > 1e12, "epochs are milliseconds"
    for f in ("facts", "concepts", "files_read", "files_modified"):
        assert isinstance(json.loads(o["items"][0][f]), list), f"{f} is JSON text"
    s = body35("summaries")["items"][0]
    assert "notes" in s and set(s) >= {"id", "session_id", "request", "learned", "next_steps", "created_at_epoch"}, "13.35.0 summaries carry notes"


def test_the_13_35_projects_answer_has_a_body_and_one_source():
    for n in ("projects", "projects_claude"):
        b = body35(n)
        assert fx35(n)["_http"] == 200 and set(b) == {"projects", "sources", "projectsBySource"} and b["projects"], n
        assert set(b["projectsBySource"]) == {"claude"}, "no codex platform source on the box"


def test_the_13_35_timeline_needs_an_anchor_or_a_query_and_answers_200():
    e = fx35("timeline_no_anchor")
    assert e["_http"] == 200 and e["body"]["isError"] is True
    assert e["body"]["_first_line_shape"] == 'Error: Must provide either "anchor" or "query" parameter'
    t = body35("timeline_anchor")
    assert t["isError"] is False and t["content"][0]["type"] == "text" and t["_first_line_shape"].startswith("# Timeline around")


def test_the_cold_search_the_docs_state_is_in_the_fixture():
    """The first search after a worker restart took 26 s on the box: the board's 6 s search budget reads it as `degraded` (timeout)
    for a while after every restart. docs/memory-api.md says so; this keeps the number and the budget side by side."""
    assert fx35("search_json")["_seconds"] > mp.SEARCH_BUDGET == 6.0


# ------------------------------------------------------------------ drift

@pytest.mark.parametrize("mutate,kind,needle", [
    (lambda b: b["items"][0].pop("created_at_epoch"), "observations", "created_at_epoch"),
    (lambda b: b["items"][0].__setitem__("id", "9526"), "observations", "'id'"),
    (lambda b: b.__setitem__("items", {"0": b["items"][0]}), "observations", "not a list"),
    (lambda b: b.pop("items"), "summaries", "no 'items'"),
    (lambda b: b.__setitem__("observations", None), "search", "not a list"),
    (lambda b: b.__setitem__("projects", "a,b"), "projects", "list of names"),
    (lambda b: b.pop("worker"), "stats", "'worker'"),
    (lambda b: b.__setitem__("content", []), "timeline", "text content"),
])
def test_a_renamed_field_or_a_changed_container_is_drift(mutate, kind, needle):
    name = {"observations": "observations", "summaries": "summaries", "search": "search", "projects": "projects", "stats": "stats",
            "timeline": "timeline"}[kind]
    body = fx(name)
    mutate(body)
    problem = mp.check_shape(kind, body)
    assert problem and needle in problem


@pytest.mark.parametrize("body", [None, [], "text", 3])
def test_an_empty_or_non_object_body_is_drift(body):
    assert mp.check_shape("observations", body)


def test_missing_optional_fields_are_fine():
    b = {"items": [{"id": 1, "created_at_epoch": 1, "title": None, "type": None}]}
    assert mp.check_shape("observations", b) is None


# ------------------------------------------------------------------ the live contract (opt-in, on the box)

@pytest.mark.real_home("reads the real ~/.claude-mem/worker.pid to find the live worker (GET only)")
@pytest.mark.skipif(os.environ.get("CCBOARD_TEST_CLAUDE_MEM") != "1", reason="live claude-mem contract: set CCBOARD_TEST_CLAUDE_MEM=1 on the box")
def test_live_worker_answers_in_the_tested_shapes():
    base = memory.worker_base()

    def get(path):
        code, body = memory.fetch(base, path, timeout=20.0, cap=mp.PROXY_CAP, strict=True)
        assert code == 200, f"{path.split('?')[0]} answered HTTP {code}"
        return body

    stats = get("/api/stats")
    assert mp.check_shape("stats", stats) is None
    ver = stats["worker"].get("version")
    print(f"worker {ver}, tested {mp.TESTED_WORKER}: compat {mp.compat(ver)}")
    projects = get("/api/projects")
    assert mp.check_shape("projects", projects) is None and projects["projects"], "an empty list means slow or down"
    key = projects["projects"][0]
    obs = get(mp._url("observations", {mp.P_PROJECT: key, mp.P_LIMIT: 1}))
    assert mp.check_shape("observations", obs) is None
    assert mp.check_shape("summaries", get(mp._url("summaries", {mp.P_PROJECT: key, mp.P_LIMIT: 1}))) is None
    srch = get(mp._url("search", {mp.P_QUERY: "the", mp.P_PROJECTS: key, mp.P_FORMAT: mp.FORMAT_JSON, mp.P_LIMIT: 1}))
    assert mp.check_shape("search", srch) is None
    pal = get(mp._url("search", {mp.P_PROJECTS: key, mp.P_TYPE: "observations", mp.P_ORDER: "date_desc", mp.P_FORMAT: mp.FORMAT_JSON,
                                 mp.P_LIMIT: 1}))
    assert mp.check_shape("search", pal) is None, "the palace's filter-only search (checked on the box with worker 13.35.0, 2026-10-09: HTTP 200, rows carry agent_type)"
    if obs["items"]:
        oid = obs["items"][0]["id"]
        assert mp.check_shape("observation", get(mp._url("observation", None, str(oid)))) is None
        tl = get(mp._url("timeline", {mp.P_ANCHOR: oid, mp.P_DEPTH_BEFORE: 1, mp.P_DEPTH_AFTER: 1, mp.P_PROJECT: key}))
        assert mp.check_shape("timeline", tl) is None and not tl.get("isError")
        assert any(r["id"] == oid for r in mp.parse_timeline(mp._first_raw(tl)))
