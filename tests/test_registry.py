"""app/agents/registry.py against the sanitised real files in tests/fixtures/claude_registry (Claude Code 2.1.287).

Fixture facts the assertions lean on: four sessions/<pid>.json (10001 busy with a bridge and job 4f9978a5, 10002 idle with
job 55bfe763, 10003 waiting on a permission prompt, 10004 shell; the last two name job dirs that do not exist), four jobs
(4f9978a5 and 55bfe763 join sessions, 8356146c and 9e79c54b are orphans), every cwd is /srv/projects/demo/repo.
"""
import importlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.proc_fake import use_fake_proc

registry = importlib.import_module("app.agents.registry")
REAL_PPID = registry.plat.ppid          # captured before the autouse fixture stubs it

FIX = Path(__file__).parent / "fixtures" / "claude_registry"
CWD = "/srv/projects/demo/repo"
S10001 = "c7725076-aa65-4020-9087-97c18a3d922b"
S10002 = "55bfe763-48a3-4f46-8d95-1a5b8980ff90"
S10003 = "2e0124bf-15b5-4b64-93d9-0659b12ab0f3"
S10004 = "d87838a6-9f4c-4053-9a70-3cae207bb0be"
J4F99_OWN = "4f9978a5-24b4-4ae2-9fc3-e1e6ee0be1c9"      # the job's own sessionId (10001 is its resumeSessionId)


def ts(*a) -> float:
    return datetime(*a, tzinfo=timezone.utc).timestamp()


NOW = ts(2026, 10, 3, 12)                                # a few hours after the newest fixture file


def row(name, **kw):
    return {"tmux_name": name, "project": "demo", "repo": "repo", "launcher": "claude", **kw}


def by_session(entries):
    return {e["session_id"]: e for e in entries}


@pytest.fixture
def cfg(tmp_path):
    """A writable copy of the fixture tree (a stand-in for ~/.claude)."""
    d = tmp_path / "claude"
    shutil.copytree(FIX, d)
    return d


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    registry.invalidate()
    monkeypatch.setattr(registry.plat, "ppid", lambda pid: None)    # no real /proc in any test unless it asks for it
    yield
    registry.invalidate()


# ------------------------------------------------------------------ parsing

def test_every_fixture_file_parses():
    snap = registry.scan(FIX, [], now=NOW)
    assert snap["ours"] == {}
    ext = by_session(snap["external"])
    assert snap["scanned_at"] == "2026-10-03T12:00:00+00:00"
    # 4 sessions + the 2 jobs no session points at; the jobs joined to 10001 and 10002 do not appear twice
    assert len(snap["external"]) == 6
    assert set(ext) == {S10001, S10002, S10003, S10004,
                        "8356146c-1b32-440e-a3e9-67ce77029888", "9e79c54b-60fe-42ed-acb4-2e942a76aa35"}
    assert [e["updated_at"] for e in snap["external"]] == sorted((e["updated_at"] for e in snap["external"]), reverse=True)
    json.dumps(snap)                                     # JSON-ready as is


def test_session_fields_map_from_the_file():
    e = by_session(registry.scan(FIX, [], now=NOW)["external"])[S10001]
    assert e["source"] == "sessions" and e["pid"] == 10001 and e["cwd"] == CWD
    assert (e["kind"], e["entrypoint"]) == ("bg", "cli")
    assert (e["name"], e["name_source"]) == ("ccboard-v0.1-planning", "auto")
    assert e["status"] == "busy" and e["bridge"] is True and e["tmux"] is None and e["waiting_for"] is None
    assert e["status_at"] == "2026-10-03T04:43:06+00:00"      # statusUpdatedAt 1791002586762 ms
    assert e["updated_at"] == "2026-10-03T04:59:26+00:00"     # the joined job (04:59:26.749Z) is newer than the session file

    e3 = by_session(registry.scan(FIX, [], now=NOW)["external"])[S10003]
    assert e3["status"] == "waiting" and e3["waiting_for"] == "permission prompt" and e3["bridge"] is False
    assert by_session(registry.scan(FIX, [], now=NOW)["external"])[S10004]["status"] == "shell"
    assert by_session(registry.scan(FIX, [], now=NOW)["external"])[S10002]["name_source"] == "user"


def test_job_joins_its_session_by_session_id_and_resume_id():
    ext = by_session(registry.scan(FIX, [], now=NOW)["external"])
    j1 = ext[S10001]["job"]                    # job 4f9978a5: its resumeSessionId is session 10001's sessionId
    assert j1 == {"id": "4f9978a5", "state": "working", "tempo": "active", "needs": None, "suggested_reply": None,
                  "children_count": 0, "intent_head": "Fix the flaky CI test and open a PR",
                  "worktree_path": None, "resume_session_id": S10001}
    j2 = ext[S10002]["job"]                    # job 55bfe763: sessionId == resumeSessionId == session 10002's
    assert j2["id"] == "55bfe763" and j2["state"] == "blocked" and j2["tempo"] == "blocked"
    assert j2["needs"] == "login required — run /login · Login expired"
    assert j2["children_count"] == 1
    # 10003 and 10004 name job dirs (a07fdedf, d87838a6) that do not exist: no job
    assert ext[S10003]["job"] is None and ext[S10004]["job"] is None


def test_orphan_jobs_are_listed_as_jobs_sources():
    ext = by_session(registry.scan(FIX, [], now=NOW)["external"])
    done = ext["8356146c-1b32-440e-a3e9-67ce77029888"]
    assert done["source"] == "jobs" and done["pid"] is None and done["kind"] == "bg"
    assert done["status"] == "done" and done["name"] == "deploy logs box" and done["bridge"] is True
    assert done["job"]["id"] == "8356146c" and done["job"]["children_count"] == 1
    assert done["job"]["tempo"] == "idle" and done["updated_at"] == "2026-10-02T20:27:10+00:00"
    stopped = ext["9e79c54b-60fe-42ed-acb4-2e942a76aa35"]
    assert stopped["status"] == "stopped" and stopped["bridge"] is False and stopped["job"]["children_count"] == 0


def test_job_extras_suggested_reply_worktree_and_intent_head(cfg):
    write(cfg / "jobs" / "aaaa1111" / "state.json", {
        "state": "waiting", "tempo": "blocked", "needs": "pick one", "suggestedReply": "continue",
        "worktreePath": "/srv/projects/demo/repo/.claude/worktrees/x", "children": [{"id": 1}, {"id": 2}, {"id": 3}],
        "intent": "  line one\n" + "word " * 60, "sessionId": "aaaa1111-0000-4000-8000-000000000000",
        "resumeSessionId": "aaaa1111-0000-4000-8000-000000000000", "name": "x", "nameSource": "auto",
        "cwd": CWD, "updatedAt": "2026-10-03T10:00:00.000Z"})
    j = by_session(registry.scan(cfg, [], now=NOW)["external"])["aaaa1111-0000-4000-8000-000000000000"]["job"]
    assert j["suggested_reply"] == "continue" and j["worktree_path"].endswith("/worktrees/x")
    assert j["children_count"] == 3 and j["needs"] == "pick one"
    assert len(j["intent_head"]) == 120 and "\n" not in j["intent_head"] and j["intent_head"].startswith("line one word")


def test_malformed_and_key_files_are_skipped(cfg):
    base = registry.scan(cfg, [], now=NOW)
    write(cfg / "sessions" / "99999.key", {"pid": 99999, "sessionId": "key-file-must-not-be-read", "cwd": CWD, "updatedAt": 1791002586762})
    write(cfg / "sessions" / "bad.json", "{not json")
    write(cfg / "sessions" / "list.json", "[1, 2]")
    write(cfg / "sessions" / "empty.json", "")
    write(cfg / "sessions" / "no-session-id.json", {"pid": 5, "cwd": CWD, "updatedAt": 1791002586762})
    write(cfg / "sessions" / "bin.json", "\xff\xfe\x00 not utf8")
    (cfg / "sessions" / "subdir.json").mkdir()
    write(cfg / "jobs" / "badjob" / "state.json", "{{{")
    write(cfg / "jobs" / "nostate" / "other.txt", "x")
    write(cfg / "jobs" / "stray-file", "x")
    after = registry.scan(cfg, [], now=NOW)
    assert after["external"] == base["external"]
    assert "key-file-must-not-be-read" not in json.dumps(after)


def test_missing_config_dir_is_empty(tmp_path):
    snap = registry.scan(tmp_path / "nope", [row("a--b--c")], now=NOW)
    assert snap["ours"] == {} and snap["external"] == []


# ------------------------------------------------------------------ ours vs external

def test_ours_by_session_id_agent_session_id_and_claude_session_id():
    snap = registry.scan(FIX, [row("demo--repo--a", claude_session_id=S10002)], now=NOW)
    assert list(snap["ours"]) == ["demo--repo--a"]
    mine = snap["ours"]["demo--repo--a"]
    assert mine["session_id"] == S10002 and mine["tmux"] == "demo--repo--a" and mine["status"] == "idle"
    assert S10002 not in by_session(snap["external"]) and len(snap["external"]) == 5
    # the db's new alias wins when both are present
    snap = registry.scan(FIX, [row("demo--repo--a", agent_session_id=S10003, claude_session_id="other")], now=NOW)
    assert snap["ours"]["demo--repo--a"]["session_id"] == S10003
    # a list of rows or db.open_rows()'s dict, keyed by tmux_name or tmux: all the same
    as_dict = {"demo--repo--a": row("demo--repo--a", claude_session_id=S10002)}
    assert registry.scan(FIX, as_dict, now=NOW)["ours"].keys() == {"demo--repo--a"}
    tmux_keyed = [{"tmux": "demo--repo--a", "claude_session_id": S10002}]
    assert registry.scan(FIX, tmux_keyed, now=NOW)["ours"].keys() == {"demo--repo--a"}


def test_ours_by_the_jobs_own_session_id():
    snap = registry.scan(FIX, [row("demo--repo--a", claude_session_id=J4F99_OWN)], now=NOW)
    assert snap["ours"]["demo--repo--a"]["session_id"] == S10001
    assert snap["ours"]["demo--repo--a"]["job"]["id"] == "4f9978a5"


def test_ours_by_pane_pid():
    snap = registry.scan(FIX, [row("demo--repo--a")], pane_pids={"demo--repo--a": 10003}, now=NOW)
    assert snap["ours"]["demo--repo--a"]["session_id"] == S10003
    assert S10003 not in by_session(snap["external"])
    # a pid nobody registered, a missing or bogus pane pid: nothing claimed
    for pp in ({"demo--repo--a": 424242}, {"other": 10003}, {"demo--repo--a": None}, {"demo--repo--a": 0}, None):
        assert registry.scan(FIX, [row("demo--repo--a", cwd="/elsewhere", project=None, repo=None)], pane_pids=pp, now=NOW)["ours"] == {}


def test_ours_by_pid_descendant_on_linux(monkeypatch, tmp_path):
    parents = {10004: 777, 777: 500, 500: 1}             # claude 10004 <- zsh 777 <- tmux pane shell 500
    monkeypatch.setattr(registry.plat, "ppid", lambda pid: parents.get(pid))
    snap = registry.scan(FIX, [row("demo--repo--a", cwd="/elsewhere")], pane_pids={"demo--repo--a": 500}, now=NOW)
    assert snap["ours"]["demo--repo--a"]["session_id"] == S10004
    # where no parent can be read (no /proc, no psutil) the ancestry finds nothing
    monkeypatch.setattr(registry.plat, "ppid", lambda pid: None)
    assert registry.scan(FIX, [row("demo--repo--a", cwd="/elsewhere")], pane_pids={"demo--repo--a": 500}, now=NOW)["ours"] == {}


def test_ppid_reads_proc_stat_and_never_raises(monkeypatch, tmp_path):
    (tmp_path / "123").mkdir()
    (tmp_path / "123" / "stat").write_text("123 (claude (v2) x) S 456 123 123 0 -1 4194560\n")   # comm with spaces and parens
    (tmp_path / "124").mkdir()
    (tmp_path / "124" / "stat").write_text("garbage")
    use_fake_proc(monkeypatch, tmp_path)
    assert REAL_PPID(123) == 456
    assert REAL_PPID(124) is None and REAL_PPID(999) is None


def test_pid_ancestry_is_bounded_and_survives_loops(monkeypatch):
    monkeypatch.setattr(registry.plat, "ppid", lambda pid: pid + 1)            # never reaches 1
    assert len(registry._ancestors(10, {})) == registry.MAX_ANCESTOR_HOPS
    monkeypatch.setattr(registry.plat, "ppid", lambda pid: {10: 11, 11: 10}.get(pid))
    assert registry._ancestors(10, {}) == [11]


def test_ours_by_cwd_only_when_exactly_one_row_has_it():
    only = registry.scan(FIX, [row("demo--repo--a", cwd=CWD)], now=NOW)
    # every entry shares the cwd; the newest wins the row (10001, joined job updated 04:59Z), the rest stay external
    assert only["ours"]["demo--repo--a"]["session_id"] == S10001
    assert len(only["external"]) == 5 and S10001 not in by_session(only["external"])
    two = registry.scan(FIX, [row("demo--repo--a", cwd=CWD), row("demo--repo--b", cwd=CWD)], now=NOW)
    assert two["ours"] == {} and len(two["external"]) == 6
    # a shell row never claims by cwd (a Mac-side claude in the same repo is not "its" session)
    sh = registry.scan(FIX, [row("demo--repo--sh", cwd=CWD, launcher="shell")], now=NOW)
    assert sh["ours"] == {}
    sh = registry.scan(FIX, [row("demo--repo--sh", cwd=CWD, agent="shell")], now=NOW)
    assert sh["ours"] == {}
    # a codex row is not a Claude session at all
    assert registry.scan(FIX, [row("demo--repo--cx", cwd=CWD, agent="codex")], now=NOW)["ours"] == {}


def test_cwd_falls_back_to_pane_path_then_projects_dir():
    r = {"tmux_name": "demo--repo--a", "project": "demo", "repo": "repo", "launcher": "claude"}
    assert registry.scan(FIX, [r], now=NOW)["ours"] == {}                                    # nothing to compare with
    assert registry.scan(FIX, [r], projects_dir=Path("/srv/projects"), now=NOW)["ours"].keys() == {"demo--repo--a"}
    assert registry.scan(FIX, [r], projects_dir=Path("/elsewhere"), now=NOW)["ours"] == {}
    assert registry.scan(FIX, [{**r, "path": CWD}], now=NOW)["ours"].keys() == {"demo--repo--a"}   # merged tmux rows carry `path`
    # repo 'root' is the project folder itself (app.projects.ROOT); here the project folder is the entries' cwd
    root = {"tmux_name": "demo--repo--a", "project": "repo", "repo": "root", "launcher": "claude"}
    assert registry.scan(FIX, [root], projects_dir=Path("/srv/projects/demo"), now=NOW)["ours"].keys() == {"demo--repo--a"}
    assert registry.scan(FIX, [{**root, "repo": "other"}], projects_dir=Path("/srv/projects/demo"), now=NOW)["ours"] == {}
    # the stored cwd beats the project/repo fallback
    assert registry.scan(FIX, [{**r, "cwd": "/elsewhere"}], projects_dir=Path("/srv/projects"), now=NOW)["ours"] == {}


def test_precedence_session_id_then_pid_then_cwd():
    rows = [row("demo--repo--byid", claude_session_id=S10004, cwd="/elsewhere/a"),
            row("demo--repo--bypid", cwd="/elsewhere/b"),
            row("demo--repo--bycwd", cwd=CWD)]
    snap = registry.scan(FIX, rows, pane_pids={"demo--repo--bypid": 10002}, now=NOW)
    assert snap["ours"]["demo--repo--byid"]["session_id"] == S10004
    assert snap["ours"]["demo--repo--bypid"]["session_id"] == S10002
    assert snap["ours"]["demo--repo--bycwd"]["session_id"] == S10001      # newest of the entries nothing else claimed
    assert len(snap["external"]) == 3


def test_one_row_per_entry_and_one_entry_per_row():
    # two rows know the same session id: the first row gets it, the second gets nothing
    snap = registry.scan(FIX, [row("demo--repo--a", claude_session_id=S10002, cwd="/x"),
                               row("demo--repo--b", claude_session_id=S10002, cwd="/y")], now=NOW)
    assert list(snap["ours"]) == ["demo--repo--a"]
    # a row whose id matches one entry and whose cwd matches the others keeps the id match only
    snap = registry.scan(FIX, [row("demo--repo--a", claude_session_id=S10003, cwd=CWD)], now=NOW)
    assert snap["ours"]["demo--repo--a"]["session_id"] == S10003 and len(snap["external"]) == 5


# ------------------------------------------------------------------ external filtering

def test_external_cutoff_is_14_days_from_the_entry_update(monkeypatch):
    # updated: 10004 2026-09-24 09:19:52.577Z, job 9e79c54b 2026-09-26 13:12:13Z, 10003 2026-10-02 17:05Z, job 8356146c 2026-10-02 20:27Z
    assert len(registry.scan(FIX, [], now=ts(2026, 10, 8))["external"]) == 6
    assert len(registry.scan(FIX, [], now=ts(2026, 10, 10))["external"]) == 5          # 10004 is 15.6 days old
    assert S10004 not in by_session(registry.scan(FIX, [], now=ts(2026, 10, 10))["external"])
    assert len(registry.scan(FIX, [], now=ts(2026, 10, 11))["external"]) == 4          # 9e79c54b is 14.5 days old
    assert set(by_session(registry.scan(FIX, [], now=ts(2026, 10, 12))["external"])) == {
        S10001, S10002, S10003, "8356146c-1b32-440e-a3e9-67ce77029888"}
    assert set(by_session(registry.scan(FIX, [], now=ts(2026, 10, 17))["external"])) == {S10001, S10002}
    assert registry.scan(FIX, [], now=ts(2026, 11, 20))["external"] == []
    # the boundary itself: exactly 14 days after the entry's last update is still external, a moment later is not
    u = registry._epoch(1790241592577)                                                 # 10004's updatedAt
    assert S10004 in by_session(registry.scan(FIX, [], now=u + registry.EXTERNAL_MAX_AGE_S)["external"])
    assert S10004 not in by_session(registry.scan(FIX, [], now=u + registry.EXTERNAL_MAX_AGE_S + 0.01)["external"])
    # the cutoff only applies to external entries: an old session that is one of ours is still ours
    old_ours = registry.scan(FIX, [row("demo--repo--a", claude_session_id=S10004)], now=ts(2026, 11, 20))
    assert old_ours["ours"]["demo--repo--a"]["session_id"] == S10004
    # default clock when `now` is omitted
    monkeypatch.setattr(registry, "_wall", lambda: ts(2026, 11, 20))
    assert registry.scan(FIX, [])["external"] == []


def test_entry_without_any_usable_time_is_not_external(cfg):
    write(cfg / "sessions" / "7.json", {"pid": 7, "sessionId": "no-time", "cwd": CWD, "status": "idle"})
    assert "no-time" not in by_session(registry.scan(cfg, [], now=NOW)["external"])


def test_observer_sessions_are_excluded_from_external(cfg):
    obs = "/home/user/.claude-mem/observer-sessions/abc123"
    write(cfg / "sessions" / "20001.json", {"pid": 20001, "sessionId": "obs-cwd", "cwd": obs, "entrypoint": "sdk-ts",
                                            "status": "idle", "updatedAt": 1791002586762})
    write(cfg / "sessions" / "20002.json", {"pid": 20002, "sessionId": "obs-transcript", "cwd": "/tmp/x", "status": "idle",
                                            "updatedAt": 1791002586762,
                                            "transcriptPath": "/home/user/.claude/projects/-x/observer-sessions/t.jsonl"})
    write(cfg / "sessions" / "20003.json", {"pid": 20003, "sessionId": "obs-root", "cwd": "/home/user/.claude-mem", "status": "idle",
                                            "updatedAt": 1791002586762})
    write(cfg / "sessions" / "20004.json", {"pid": 20004, "sessionId": "sdk-but-real", "cwd": "/srv/projects/demo/repo",
                                            "entrypoint": "sdk-ts", "status": "idle", "updatedAt": 1791002586762})
    write(cfg / "jobs" / "obsjob" / "state.json", {"state": "working", "sessionId": "obs-job", "cwd": obs,
                                                   "updatedAt": "2026-10-03T10:00:00.000Z"})
    ext = by_session(registry.scan(cfg, [], now=NOW)["external"])
    assert not {"obs-cwd", "obs-transcript", "obs-root", "obs-job"} & set(ext)
    assert "sdk-but-real" in ext and len(ext) == 7                       # entrypoint alone does not make an observer
    # but a session the board launched itself is ours whatever its cwd
    snap = registry.scan(cfg, [row("demo--repo--a", claude_session_id="obs-cwd")], now=NOW)
    assert snap["ours"]["demo--repo--a"]["session_id"] == "obs-cwd"


# ------------------------------------------------------------------ enrich

def test_enrich_sets_flags_registry_and_nothing_else():
    snap = registry.scan(FIX, [row("demo--repo--a", claude_session_id=S10001),
                               row("demo--repo--b", claude_session_id=S10003)], now=NOW)
    a = {"tmux": "demo--repo--a", "state": "idle", "state_at": "2026-10-03T00:00:00+00:00", "flags": None}
    b = {"tmux_name": "demo--repo--b", "state": "working", "flags": {"subagents": 2, "wait_kind": None}}
    c = {"tmux": "demo--repo--none", "state": "idle"}
    d = {"tmux": "demo--repo--a", "state": "done", "flags": "not-a-dict"}
    frozen = [json.dumps(x, sort_keys=True) for x in (a, b, c, d)]
    out = registry.enrich([a, b, c, d], snap)
    assert out == [a, b, c, d]
    assert a["flags"]["registry"] == {
        "status": "busy", "name": "ccboard-v0.1-planning", "name_source": "auto", "pid": 10001, "bridge": True, "kind": "bg",
        "job": {"state": "working", "tempo": "active", "needs": None, "suggested_reply": None},
        "at": "2026-10-03T04:43:06+00:00", "waiting_for": None}
    assert b["flags"]["subagents"] == 2 and b["flags"]["registry"]["status"] == "waiting"
    assert b["flags"]["registry"]["waiting_for"] == "permission prompt" and b["flags"]["registry"]["job"] is None
    assert "flags" not in c                                              # not in the snapshot: untouched
    assert d["flags"]["registry"]["name"] == "ccboard-v0.1-planning"     # a non-dict flags value is replaced, not crashed on
    for x, before in zip((a, b, c, d), frozen):                          # state fields never move
        after = {k: v for k, v in x.items() if k != "flags"}
        assert after == {k: v for k, v in json.loads(before).items() if k != "flags"}
    assert registry.enrich([], snap) == [] and registry.enrich(None, snap) is None
    assert registry.enrich([c], {}) == [c] and registry.enrich([c], None) == [c]


def test_enrich_accepts_a_dict_keyed_by_tmux_name():
    snap = registry.scan(FIX, [row("demo--repo--a", claude_session_id=S10002)], now=NOW)
    sessions = {"demo--repo--a": {"state": "idle", "flags": {"subagents": 1}}, "demo--repo--z": {"state": "idle"}}
    assert registry.enrich(sessions, snap) is sessions
    assert sessions["demo--repo--a"]["flags"]["subagents"] == 1
    assert sessions["demo--repo--a"]["flags"]["registry"]["name"] == "m-hvac-mail"
    assert sessions["demo--repo--a"]["flags"]["registry"]["job"]["state"] == "blocked"
    assert "flags" not in sessions["demo--repo--z"]


def test_scan_results_are_independent_copies():
    r1 = registry.scan(FIX, [row("demo--repo--a", claude_session_id=S10002)], now=NOW)
    r1["ours"]["demo--repo--a"]["name"] = "mutated"
    r1["external"].clear()
    snap_rows = [row("demo--repo--a", claude_session_id=S10002)]
    registry.snapshot(None, FIX, now=NOW)
    r2 = registry.snapshot(type("D", (), {"open_rows": lambda self: snap_rows})(), FIX, now=NOW)
    assert r2["ours"]["demo--repo--a"]["name"] == "m-hvac-mail" and len(r2["external"]) == 5
    r2["ours"]["demo--repo--a"]["job"]["state"] = "mutated"
    r3 = registry.snapshot(type("D", (), {"open_rows": lambda self: snap_rows})(), FIX, now=NOW)
    assert r3["ours"]["demo--repo--a"]["job"]["state"] == "blocked"


# ------------------------------------------------------------------ snapshot cache

class FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def open_rows(self):
        self.calls += 1
        return self.rows


def test_snapshot_reuses_parsed_files_for_ttl_but_not_the_ours_split(cfg, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(registry, "_clock", lambda: clock[0])
    db = FakeDB({})
    first = registry.snapshot(db, cfg, 30, now=NOW)
    assert by_session(first["external"])[S10002]["name"] == "m-hvac-mail"
    # rewrite the file: within the ttl the old parse is served
    p = cfg / "sessions" / "10002.json"
    data = json.loads(p.read_text())
    data["name"] = "renamed"
    p.write_text(json.dumps(data))
    clock[0] += 29.9
    assert by_session(registry.snapshot(db, cfg, 30, now=NOW)["external"])[S10002]["name"] == "m-hvac-mail"
    # ...but a row that appeared a second ago is classified at once
    db.rows = {"demo--repo--a": row("demo--repo--a", claude_session_id=S10002)}
    assert registry.snapshot(db, cfg, 30, now=NOW)["ours"]["demo--repo--a"]["name"] == "m-hvac-mail"
    clock[0] += 0.2                                                      # 30.1 s: expired
    snap = registry.snapshot(db, cfg, 30, now=NOW)
    assert snap["ours"]["demo--repo--a"]["name"] == "renamed"
    assert db.calls == 4


def test_snapshot_ttl_zero_always_rereads_and_other_dir_invalidates(cfg, tmp_path, monkeypatch):
    clock = [5.0]
    monkeypatch.setattr(registry, "_clock", lambda: clock[0])
    assert len(registry.snapshot(None, cfg, 0, now=NOW)["external"]) == 6
    (cfg / "sessions" / "10004.json").unlink()
    assert len(registry.snapshot(None, cfg, 0, now=NOW)["external"]) == 5
    registry.invalidate()
    other = tmp_path / "other"
    assert registry.snapshot(None, other, 30, now=NOW)["external"] == []
    assert len(registry.snapshot(None, cfg, 30, now=NOW)["external"]) == 5      # a different dir is never served from the cache


def test_snapshot_tolerates_a_failing_or_missing_db(cfg):
    class Boom:
        def open_rows(self):
            raise RuntimeError("db closed")
    assert len(registry.snapshot(Boom(), cfg, now=NOW)["external"]) == 6
    assert len(registry.snapshot(None, cfg, now=NOW)["external"]) == 6


def test_snapshot_passes_pane_pids_and_projects_dir_through(cfg):
    db = FakeDB([{"tmux_name": "demo--repo--a", "project": "demo", "repo": "repo"}])
    snap = registry.snapshot(db, cfg, pane_pids={"demo--repo--a": 10002}, now=NOW)
    assert snap["ours"]["demo--repo--a"]["session_id"] == S10002
    snap = registry.snapshot(db, cfg, projects_dir=Path("/srv/projects"), now=NOW)
    assert snap["ours"]["demo--repo--a"]["session_id"] == S10001



def test_registry_imports_only_the_standard_library():
    """Display only and acyclic: no db, tmux, hooks, subprocess or sibling agent module is imported."""
    import ast
    tree = ast.parse(Path(registry.__file__).read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            if n.level == 2 and not n.module and [a.name for a in n.names] == ["platform"]:
                mods.add("app.platform")                  # stdlib only itself (tests/test_platform_proc.py), so still acyclic
                continue
            assert n.level == 0, "relative import"
            mods.add((n.module or "").split(".")[0])
    assert mods == {"__future__", "json", "os", "app.platform", "threading", "time", "dataclasses", "datetime", "pathlib"}
