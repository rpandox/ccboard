"""Schema migrations and the data-layer API of v0.5.4: a DB written by the previous image is upgraded in place,
additively and idempotently, and the previous image's own SQL still works on the upgraded DB."""
import json
import sqlite3

import pytest

from app import db as dbmod
from app.db import DB

# ---- the schema as shipped by v0.5.3b (SCHEMA + MIGRATIONS of app/db.py at commit c635d6c, copied verbatim) -------
OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY,
  tmux_name TEXT NOT NULL,
  project TEXT NOT NULL,
  repo TEXT NOT NULL,
  name TEXT NOT NULL,
  launcher TEXT NOT NULL,
  cmd TEXT,
  claude_session_id TEXT,
  add_dirs TEXT,
  created_at TEXT NOT NULL,
  ended_at TEXT
);
CREATE INDEX IF NOT EXISTS sessions_open ON sessions(tmux_name) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  tmux_name TEXT NOT NULL,
  event TEXT NOT NULL,
  kind TEXT,
  message TEXT,
  payload TEXT,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_session ON events(tmux_name, id);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS push_subs (endpoint TEXT PRIMARY KEY, sub TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL,
  repo TEXT NOT NULL,
  slug TEXT NOT NULL,
  title TEXT NOT NULL,
  prompt TEXT NOT NULL,
  branch TEXT NOT NULL,
  base TEXT,
  worktree TEXT NOT NULL,
  tmux_name TEXT NOT NULL,
  claude_session_id TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  pr_number INTEGER,
  pr_url TEXT,
  pr_state TEXT,
  pr_json TEXT,
  ci TEXT,
  cost_usd REAL,
  overlap TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  archived_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL,
  repo TEXT NOT NULL,
  name TEXT NOT NULL,
  prompt TEXT NOT NULL,
  cron TEXT,
  permission_mode TEXT NOT NULL DEFAULT 'acceptEdits',
  max_turns INTEGER NOT NULL DEFAULT 30,
  max_budget_usd REAL,
  args TEXT,
  timeout_s INTEGER,
  enabled INTEGER NOT NULL DEFAULT 1,
  batch_id TEXT,
  next_run_at TEXT,
  last_run_at TEXT,
  last_status TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  result TEXT,
  error TEXT,
  session_id TEXT,
  cost_usd REAL,
  num_turns INTEGER,
  worktree TEXT,
  branch TEXT,
  task_id INTEGER
);
CREATE TABLE IF NOT EXISTS permissions (
  id INTEGER PRIMARY KEY,
  tmux_name TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  summary TEXT NOT NULL,
  input TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  decision TEXT,
  source TEXT
);
"""

OLD_MIGRATIONS = [
    "ALTER TABLE sessions ADD COLUMN state TEXT",
    "ALTER TABLE sessions ADD COLUMN state_at TEXT",
    "ALTER TABLE sessions ADD COLUMN last_event TEXT",
    "ALTER TABLE sessions ADD COLUMN last_message TEXT",
    "ALTER TABLE sessions ADD COLUMN last_prompt TEXT",
    "ALTER TABLE sessions ADD COLUMN stats TEXT",
    "ALTER TABLE sessions ADD COLUMN acked_at TEXT",
    "ALTER TABLE tasks ADD COLUMN preview_port INTEGER",
    "ALTER TABLE tasks ADD COLUMN preview_https INTEGER",
]


def make_old_db(path, *, with_migrations=True):
    """A DB exactly as the previous image left it, with one row of each kind that matters."""
    c = sqlite3.connect(str(path), isolation_level=None)
    c.executescript(OLD_SCHEMA)
    if with_migrations:
        for stmt in OLD_MIGRATIONS:
            c.execute(stmt)
    ins = ("INSERT INTO sessions(tmux_name, project, repo, name, launcher, cmd, claude_session_id, add_dirs, created_at)"
           " VALUES (?,?,?,?,?,?,?,?,?)")
    c.execute(ins, ("shop--api--s1", "shop", "api", "s1", "claude", "claude", "aaaa", "[]", "2026-10-01T00:00:00+00:00"))
    c.execute(ins, ("shop--api--sh", "shop", "api", "sh", "shell", None, None, "[]", "2026-10-01T00:00:01+00:00"))
    c.execute(ins, ("shop--api--clone", "shop", "api", "clone", "clone", "git clone x", None, "[]", "2026-10-01T00:00:02+00:00"))
    c.execute(ins, ("shop--api--t-fix", "shop", "api", "t-fix", "task", "claude --worktree fix", "bbbb", "[]", "2026-10-01T00:00:03+00:00"))
    c.execute("INSERT INTO tasks(project, repo, slug, title, prompt, branch, base, worktree, tmux_name, claude_session_id,"
              " created_at, updated_at) VALUES ('shop','api','fix','Fix it','do it','worktree-fix','main',"
              "'/p/shop/api/.claude/worktrees/fix','shop--api--t-fix','bbbb','2026-10-01T00:00:03+00:00','2026-10-01T00:00:03+00:00')")
    c.execute("INSERT INTO jobs(project, repo, name, prompt, created_at) VALUES ('shop','api','nightly','p','2026-10-01T00:00:04+00:00')")
    c.execute("INSERT INTO events(tmux_name, event, kind, message, payload, at) VALUES ('shop--api--s1','Stop',NULL,'m','{}','2026-10-01T00:00:05+00:00')")
    c.execute("INSERT INTO permissions(tmux_name, tool_name, summary, created_at) VALUES ('shop--api--s1','Bash','ls','2026-10-01T00:00:06+00:00')")
    c.close()


def columns(conn, table):
    return {r[1]: (r[2], r[3], r[4]) for r in conn.execute(f"PRAGMA table_info({table})")}     # name -> (type, notnull, default)


def dump(db):
    return list(db.conn.iterdump())


NEW_SESSION_COLS = {"agent", "cwd", "opts", "flags", "ended_reason"}
NEW_TASK_COLS = {"agent", "mode", "phase", "session_row", "auto_close", "parent_id", "chain_id", "spec", "result",
                 "result_at", "assigned_at", "done_at", "issue_number", "issue_url", "issue_commented_at", "origin"}


@pytest.fixture
def db(tmp_path):
    d = DB(tmp_path / "ccboard.db")
    yield d
    d.conn.close()


def add(db, name="shop--api--s1", **kw):
    base = dict(tmux_name=name, project="shop", repo="api", name=name.split("--")[-1], launcher="claude", cmd="claude",
                claude_session_id=None, add_dirs=[])
    base.update(kw)
    return db.add_session(**base)


# ---- upgrade in place ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("with_migrations", [True, False], ids=["v0.5.3b-db", "pre-v0.2-db"])
def test_old_db_is_upgraded_in_place(tmp_path, with_migrations):
    path = tmp_path / "old.db"
    make_old_db(path, with_migrations=with_migrations)
    db = DB(path)
    try:
        s = columns(db.conn, "sessions")
        assert NEW_SESSION_COLS <= set(s) and s["agent"] == ("TEXT", 1, "'claude'")
        t = columns(db.conn, "tasks")
        assert NEW_TASK_COLS <= set(t)
        assert t["phase"][2] == "'running'" and t["mode"][2] == "'worktree'" and t["auto_close"] == ("INTEGER", 1, "0")
        assert "agent" in columns(db.conn, "events") and columns(db.conn, "jobs")["agent"] == ("TEXT", 1, "'claude'")
        idx = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"tasks_by_session", "tasks_by_parent", "sessions_open", "events_by_session"} <= idx

        agents = {r["tmux_name"]: r["agent"] for r in db.conn.execute("SELECT tmux_name, agent FROM sessions")}
        assert agents == {"shop--api--s1": "claude", "shop--api--sh": "shell", "shop--api--clone": "shell", "shop--api--t-fix": "claude"}
        row = db.conn.execute("SELECT * FROM sessions WHERE tmux_name='shop--api--s1'").fetchone()
        assert row["cwd"] is None and row["opts"] is None and row["flags"] is None and row["ended_reason"] is None
        assert row["claude_session_id"] == "aaaa"                      # nothing renamed, nothing lost

        task = db.task_get(1)
        assert task["worktree"] == "/p/shop/api/.claude/worktrees/fix" and task["tmux_name"] == "shop--api--t-fix"
        assert (task["agent"], task["mode"], task["phase"], task["auto_close"]) == ("claude", "worktree", "running", 0)
        for k in ("session_row", "parent_id", "chain_id", "spec", "result", "result_at", "assigned_at", "done_at",
                  "issue_number", "issue_url", "issue_commented_at", "origin"):
            assert task[k] is None, k
        assert db.jobs()[0]["agent"] == "claude"
        assert db.conn.execute("SELECT agent FROM events").fetchone()["agent"] is None
        assert db.perm_get(1)["decision"] is None                      # permissions untouched (no CHECK to migrate)
    finally:
        db.conn.close()


def test_second_open_changes_nothing(tmp_path):
    path = tmp_path / "old.db"
    make_old_db(path)
    first = DB(path)
    before = dump(first)
    first.conn.close()
    second = DB(path)
    try:
        assert dump(second) == before
    finally:
        second.conn.close()
    third = DB(path)
    try:
        assert dump(third) == before
    finally:
        third.conn.close()


def test_fresh_db_matches_an_upgraded_one(tmp_path):
    make_old_db(tmp_path / "old.db")
    old, fresh = DB(tmp_path / "old.db"), DB(tmp_path / "fresh.db")
    try:
        for table in ("sessions", "events", "tasks", "jobs", "runs", "permissions"):
            assert columns(fresh.conn, table) == columns(old.conn, table), table
        assert fresh.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    finally:
        old.conn.close()
        fresh.conn.close()


def test_backfill_is_rerunnable_and_narrow(tmp_path):
    path = tmp_path / "old.db"
    make_old_db(path)
    db = DB(path)
    codex_shell = add(db, "shop--api--cx", launcher="shell", agent="codex")
    claude_row = add(db, "shop--api--c2", launcher="claude")
    db.conn.close()
    # a rolled-back previous image writes a shell session without naming the agent column: it lands as 'claude'
    c = sqlite3.connect(str(path), isolation_level=None)
    c.execute("INSERT INTO sessions(tmux_name, project, repo, name, launcher, created_at)"
              " VALUES ('shop--api--sh2','shop','api','sh2','shell','2026-10-02T00:00:00+00:00')")
    assert c.execute("SELECT agent FROM sessions WHERE name='sh2'").fetchone()[0] == "claude"
    c.close()
    db = DB(path)                                                      # the new image comes back
    try:
        got = {r["name"]: r["agent"] for r in db.conn.execute("SELECT name, agent FROM sessions")}
        assert got["sh2"] == "shell"
        assert got["cx"] == "codex"                                    # only agent='claude' rows are touched
        assert got["c2"] == "claude" and got["s1"] == "claude" and got["t-fix"] == "claude"
        assert (codex_shell, claude_row) == (5, 6)
    finally:
        db.conn.close()


def test_previous_image_sql_still_works_on_a_migrated_db(tmp_path):
    """Rollback safety: the old image's literal INSERTs (it names none of the new columns) succeed and the new
    NOT NULL columns take their defaults."""
    path = tmp_path / "old.db"
    make_old_db(path)
    DB(path).conn.close()
    c = sqlite3.connect(str(path), isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("INSERT INTO sessions(tmux_name, project, repo, name, launcher, cmd, claude_session_id,"
              " add_dirs, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
              ("shop--api--s9", "shop", "api", "s9", "claude", "claude", "cccc", "[]", "2026-10-03T00:00:00+00:00"))
    c.execute("INSERT INTO tasks(project, repo, slug, title, prompt, branch, base, worktree, tmux_name, claude_session_id,"
              " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              ("shop", "api", "s9", "t", "p", "worktree-s9", "main", "/w", "shop--api--s9", None, "x", "x"))
    c.execute("INSERT INTO jobs(project, repo, name, prompt, cron, permission_mode, max_turns, max_budget_usd, args,"
              " timeout_s, enabled, batch_id, next_run_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              ("shop", "api", "j", "p", None, "acceptEdits", 5, None, None, None, 1, None, None, "x"))
    c.execute("INSERT INTO events(tmux_name, event, kind, message, payload, at) VALUES ('n','Stop',NULL,NULL,'{}','x')")
    assert c.execute("SELECT agent FROM sessions WHERE name='s9'").fetchone()[0] == "claude"
    t = c.execute("SELECT agent, mode, phase, auto_close FROM tasks WHERE slug='s9'").fetchone()
    assert tuple(t) == ("claude", "worktree", "running", 0)
    assert c.execute("SELECT agent FROM jobs WHERE name='j'").fetchone()[0] == "claude"
    # and the old image's reads (SELECT *) still see every old column
    assert {"tmux_name", "claude_session_id", "stats", "acked_at"} <= set(c.execute("SELECT * FROM sessions").fetchone().keys())
    c.close()


# ---- add_session / task_add / job_add -----------------------------------------------------------------------------
def test_add_session_returns_row_id_and_builds_columns(db):
    a = add(db, "shop--api--s1", claude_session_id="sid1", add_dirs=["x"])
    b = add(db, "shop--api--sh", launcher="shell", cmd=None, agent="shell", cwd="/p/shop/api")
    c = add(db, "shop--api--cx", launcher="codex", agent="codex", cwd="/w", opts={"model": "gpt", "effort": "high"},
            flags={"hook_seen": True})
    assert (a, b, c) == (1, 2, 3)
    rows = {r["name"]: r for r in db.conn.execute("SELECT * FROM sessions")}
    assert rows["s1"]["agent"] == "claude" and rows["s1"]["cwd"] is None and rows["s1"]["opts"] is None
    assert json.loads(rows["s1"]["add_dirs"]) == ["x"] and rows["s1"]["claude_session_id"] == "sid1"
    assert rows["sh"]["agent"] == "shell" and rows["sh"]["cmd"] is None and rows["sh"]["cwd"] == "/p/shop/api"
    assert json.loads(rows["cx"]["opts"]) == {"model": "gpt", "effort": "high"} and json.loads(rows["cx"]["flags"]) == {"hook_seen": True}
    # an explicit None never reaches the NOT NULL DEFAULT column
    d = add(db, "shop--api--n", agent=None)
    assert db.conn.execute("SELECT agent FROM sessions WHERE id=?", (d,)).fetchone()[0] == "claude"
    # legacy call shape (every old kwarg, nothing new) still works
    db.add_session(tmux_name="x--y--z", project="x", repo="y", name="z", launcher="claude", cmd="claude",
                   claude_session_id=None, add_dirs=None)


def test_task_add_never_writes_null_into_defaulted_columns(db):
    t = db.task_add(project="shop", repo="api", slug="a", title="A", prompt="p", branch="worktree-a", base="main",
                    worktree="/w/a", tmux_name="shop--api--t-a", claude_session_id=None,
                    agent=None, mode=None, phase=None, status=None, auto_close=None)
    row = db.task_get(t)
    assert (row["agent"], row["mode"], row["phase"], row["status"], row["auto_close"]) == ("claude", "worktree", "running", "open", 0)
    assert row["worktree"] == "/w/a" and row["branch"] == "worktree-a" and row["created_at"] == row["updated_at"]


def test_unassigned_task_stores_sentinels_and_backlog(db):
    t = db.task_add(project="shop", repo="api", slug="idea", title="Idea", prompt="later", agent="codex", mode="worktree")
    row = db.task_get(t)
    assert (row["tmux_name"], row["worktree"], row["branch"]) == ("", "", "")
    assert row["phase"] == "backlog" and row["agent"] == "codex" and row["session_row"] is None
    # None for the three sentinels behaves like leaving them out
    t2 = db.task_add(project="shop", repo="api", slug="idea2", title="I2", prompt="p", branch=None, worktree=None, tmux_name=None, base=None)
    assert (db.task_get(t2)["tmux_name"], db.task_get(t2)["phase"]) == ("", "backlog")
    # an explicit phase wins; a dict spec is stored as JSON
    t3 = db.task_add(project="shop", repo="api", slug="step2", title="S2", prompt="p", phase="queued", parent_id=t,
                     chain_id="c1", spec={"agent": "claude", "mode": "worktree"})
    row3 = db.task_get(t3)
    assert row3["phase"] == "queued" and row3["parent_id"] == t and row3["chain_id"] == "c1"
    assert json.loads(row3["spec"]) == {"agent": "claude", "mode": "worktree"}
    # a session-mode task has a tmux name but no worktree: it is running, not backlog
    t4 = db.task_add(project="shop", repo="api", slug="inplace", title="In", prompt="p", tmux_name="shop--api--s1", mode="session")
    assert (db.task_get(t4)["phase"], db.task_get(t4)["worktree"]) == ("running", "")


def test_task_add_and_update_guard_inputs(db):
    with pytest.raises(TypeError, match="unknown column"):
        db.task_add(project="shop", repo="api", slug="a", title="A", prompt="p", nope=1)
    with pytest.raises(TypeError, match="missing"):
        db.task_add(project="shop", repo="api", slug="a", title="A")
    t = db.task_add(project="shop", repo="api", slug="a", title="A", prompt="p", result="x" * 30000)
    assert len(db.task_get(t)["result"]) == dbmod.RESULT_MAX
    db.task_update(t, result="y" * 25000, spec={"k": 1}, session_row=7, phase="running")
    row = db.task_get(t)
    assert len(row["result"]) == dbmod.RESULT_MAX and json.loads(row["spec"]) == {"k": 1} and row["session_row"] == 7
    db.task_update(t, session_row=None)                                # a detach really does write NULL
    assert db.task_get(t)["session_row"] is None


def test_job_add_defaults_and_agent(db):
    j = db.job_add(project="shop", repo="api", name="n", prompt="p", cron=None, permission_mode=None, max_turns=None,
                   max_budget_usd=None, args=None, timeout_s=None, enabled=None, batch_id=None, next_run_at=None)
    row = db.job_get(j)
    assert (row["permission_mode"], row["max_turns"], row["enabled"], row["agent"]) == ("acceptEdits", 30, 1, "claude")
    j2 = db.job_add(project="shop", repo="api", name="n2", prompt="p", agent="codex", max_turns=3, cron="* * * * *")
    row2 = db.job_get(j2)
    assert (row2["agent"], row2["max_turns"], row2["cron"]) == ("codex", 3, "* * * * *")
    with pytest.raises(TypeError):
        db.job_add(project="shop", repo="api", name="n3", prompt="p", bogus=1)
    with pytest.raises(TypeError):
        db.job_add(project="shop", repo="api", name="n3")


# ---- update_flags -------------------------------------------------------------------------------------------------
def test_update_flags_set_delete_incr(db):
    add(db, "shop--api--s1")
    assert db.update_flags("shop--api--s1", {"compacting": True, "model_hint": "opus"}) == {"compacting": True, "model_hint": "opus"}
    assert db.update_flags("shop--api--s1", {"compacting": None, "last_tool_at": 5}) == {"model_hint": "opus", "last_tool_at": 5}
    assert db.update_flags("shop--api--s1", {"missing": None}) == {"model_hint": "opus", "last_tool_at": 5}      # deleting a missing key is a no-op
    assert db.update_flags("shop--api--s1", incr={"subagents": 1}) == {"model_hint": "opus", "last_tool_at": 5, "subagents": 1}
    assert db.update_flags("shop--api--s1", incr={"subagents": 2})["subagents"] == 3
    assert db.update_flags("shop--api--s1", incr={"subagents": -1})["subagents"] == 2
    assert db.update_flags("shop--api--s1", incr={"subagents": -10})["subagents"] == 0       # floor 0
    assert db.update_flags("shop--api--s1", incr={"subthreads": -1})["subthreads"] == 0      # a missing counter starts at 0
    assert db.update_flags("shop--api--s1", patch={"wait_kind": "permission"}, incr={"subagents": 1}) \
        == {"model_hint": "opus", "last_tool_at": 5, "subagents": 1, "subthreads": 0, "wait_kind": "permission"}
    stored = json.loads(db.conn.execute("SELECT flags FROM sessions").fetchone()[0])
    assert stored == db.update_flags("shop--api--s1")                                          # no-arg call just reads
    assert db.open_rows()["shop--api--s1"]["flags"] == stored
    # nested values (registry, pending_cmd) round-trip
    reg = {"status": "busy", "pid": 12, "at": "t"}
    assert db.update_flags("shop--api--s1", {"registry": reg})["registry"] == reg


def test_update_flags_targets_the_newest_open_row_only(db):
    add(db, "shop--api--s1")
    db.update_flags("shop--api--s1", {"a": 1})
    db.end("shop--api--s1")
    assert db.update_flags("shop--api--s1", {"a": 2}) == {}                                    # no open row: nothing written
    assert json.loads(db.conn.execute("SELECT flags FROM sessions WHERE id=1").fetchone()[0]) == {"a": 1}
    add(db, "shop--api--s1")                                                                    # the name is reused: a fresh row
    assert db.update_flags("shop--api--s1", {"b": 1}) == {"b": 1}
    assert json.loads(db.conn.execute("SELECT flags FROM sessions WHERE id=1").fetchone()[0]) == {"a": 1}
    assert db.update_flags("nope", {"x": 1}) == {}


def test_update_flags_tolerates_bad_json_and_types(db):
    add(db, "shop--api--s1")
    db.conn.execute("UPDATE sessions SET flags='{not json'")
    assert db.update_flags("shop--api--s1", {"k": 1}) == {"k": 1}
    db.conn.execute("UPDATE sessions SET flags='[1,2]'")                                       # valid JSON, wrong shape
    assert db.update_flags("shop--api--s1", incr={"n": 1}) == {"n": 1}
    db.update_flags("shop--api--s1", {"flag": True, "word": "x"})
    assert db.update_flags("shop--api--s1", incr={"flag": 1, "word": 2}) == {"n": 1, "flag": 1, "word": 2}   # bool/str count as 0


def test_update_flags_is_atomic_across_threads(db):
    import threading
    add(db, "shop--api--s1")
    ts = [threading.Thread(target=lambda: [db.update_flags("shop--api--s1", incr={"subagents": 1}) for _ in range(50)])
          for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert db.update_flags("shop--api--s1")["subagents"] == 200


# ---- open_rows / session_view -------------------------------------------------------------------------------------
def test_open_rows_aliases_and_parsing(db):
    rid = add(db, "shop--api--s1", claude_session_id="sid1", add_dirs=["d"], cwd="/w", opts={"model": "opus"},
              flags={"hook_seen": True})
    shell = add(db, "shop--api--sh", launcher="shell", cmd=None, agent="shell")
    db.set_stats("shop--api--s1", {"model": "Opus"})
    rows = db.open_rows()
    r = rows["shop--api--s1"]
    assert r["row_id"] == r["id"] == rid and r["agent_session_id"] == r["claude_session_id"] == "sid1"
    assert (r["agent"], r["cwd"], r["add_dirs"], r["opts"], r["flags"], r["stats"]) == \
        ("claude", "/w", ["d"], {"model": "opus"}, {"hook_seen": True}, {"model": "Opus"})
    s = rows["shop--api--sh"]
    assert s["row_id"] == shell and s["agent"] == "shell" and s["agent_session_id"] is None
    assert s["flags"] == {} and s["opts"] == {} and s["stats"] is None and s["add_dirs"] == []
    assert db.open_row("shop--api--s1") == r and db.open_row("nope") is None


def test_open_rows_never_raises_on_bad_json(db):
    add(db, "shop--api--s1")
    db.conn.execute("UPDATE sessions SET flags='{oops', opts='[1]', stats='{x', add_dirs='nope'")
    r = db.open_rows()["shop--api--s1"]
    assert r["flags"] == {} and r["opts"] == {} and r["stats"] is None and r["add_dirs"] == []
    db.conn.execute("UPDATE sessions SET flags=NULL, opts='', stats='', add_dirs=NULL")
    r = db.open_rows()["shop--api--s1"]
    assert r["flags"] == {} and r["opts"] == {} and r["stats"] is None and r["add_dirs"] == []


def test_open_rows_newest_row_wins_and_ended_are_hidden(db):
    add(db, "shop--api--s1", claude_session_id="old")
    db.end("shop--api--s1")
    new = add(db, "shop--api--s1", claude_session_id="new")
    rows = db.open_rows()
    assert list(rows) == ["shop--api--s1"] and rows["shop--api--s1"]["row_id"] == new
    assert rows["shop--api--s1"]["agent_session_id"] == "new"


# ---- end / reconcile ----------------------------------------------------------------------------------------------
def test_end_records_a_reason(db):
    a, b, c = add(db, "a--r--1"), add(db, "b--r--2"), add(db, "c--r--3")
    db.end("a--r--1")
    db.end("b--r--2", "auto_close")
    db.end("never--r--open", "exited")                                   # unknown name: nothing happens
    db.reconcile({"b--r--2"}, before="9999-01-01T00:00:00+00:00")        # c is not alive any more, b is already closed
    got = {r["id"]: (r["ended_reason"], r["ended_at"] is not None) for r in db.conn.execute("SELECT * FROM sessions")}
    assert got == {a: ("killed", True), b: ("auto_close", True), c: ("reconciled", True)}
    d = add(db, "d--r--4")
    assert db.conn.execute("SELECT ended_reason, ended_at FROM sessions WHERE id=?", (d,)).fetchone()[:] == (None, None)


# ---- active_tasks_by_session --------------------------------------------------------------------------------------
def test_active_tasks_by_session_one_query(db):
    s1, s2 = add(db, "shop--api--s1"), add(db, "shop--api--s2")
    def mk(slug, **kw):
        return db.task_add(project="shop", repo="api", slug=slug, title=f"T {slug}", prompt="p", **kw)

    run = mk("run", tmux_name="shop--api--s1", phase="running", session_row=s1, auto_close=1)
    done = mk("done", tmux_name="shop--api--s1", phase="done", session_row=s1)
    other = mk("other", tmux_name="shop--api--s2", phase="failed", session_row=s2)
    mk("backlog", phase="backlog")                                                         # no session
    mk("cancelled", tmux_name="x", phase="cancelled", session_row=s1)                      # not active
    mk("legacy", tmux_name="shop--api--s2", worktree="/w")                                 # legacy row: no session_row
    arch = mk("arch", tmux_name="shop--api--s1", phase="running", session_row=s1)
    db.task_update(arch, archived_at=dbmod.now())
    queries = []
    db.conn.set_trace_callback(queries.append)
    got = db.active_tasks_by_session()
    db.conn.set_trace_callback(None)
    assert len([q for q in queries if q.lstrip().upper().startswith("SELECT")]) == 1
    assert got == {
        s1: [{"id": run, "title": "T run", "phase": "running", "auto_close": True},
             {"id": done, "title": "T done", "phase": "done", "auto_close": False}],
        s2: [{"id": other, "title": "T other", "phase": "failed", "auto_close": False}],
    }
    assert db.active_tasks_by_session(phases=("done",)) == {s1: [{"id": done, "title": "T done", "phase": "done", "auto_close": False}]}


# ---- events -------------------------------------------------------------------------------------------------------
def test_event_constants():
    assert dbmod.EVENTS_CAP == 20000
    assert dbmod.SKIP_EVENTS == {"PostToolBatch", "statusline"}


def test_add_event_skips_noise_and_caps(db, monkeypatch):
    assert db.add_event("n", "PostToolBatch", None, "x", {}) is False
    assert db.add_event("n", "statusline", None, None, {"model": {}}) is False
    assert db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert db.add_event("n", "Stop", "k", "msg", {"a": 1}, agent="claude") is True
    row = db.conn.execute("SELECT event, kind, message, agent FROM events").fetchone()
    assert tuple(row) == ("Stop", "k", "msg", "claude")
    assert db.add_event("n", "Notification", None, None, {}) is True                       # agent is optional
    assert db.conn.execute("SELECT agent FROM events ORDER BY id DESC").fetchone()[0] is None
    monkeypatch.setattr(dbmod, "EVENTS_CAP", 5)
    for i in range(12):
        db.add_event("n", "Stop", None, f"m{i}", {"i": i})
    msgs = [r[0] for r in db.conn.execute("SELECT message FROM events ORDER BY id")]
    assert msgs == [f"m{i}" for i in range(7, 12)]                                         # exactly the newest EVENTS_CAP rows
    assert [e["message"] for e in db.recent_events(2)] == ["m11", "m10"]


# ---- nodes epic P1 (issue #137): tasks.origin ------------------------------------------------------------------------
def test_tasks_origin_is_a_nullable_text_column_for_work_asked_by_another_board(tmp_path):
    path = tmp_path / "old.db"
    make_old_db(path)
    db = DB(path)
    try:
        assert columns(db.conn, "tasks")["origin"] == ("TEXT", 0, None)                  # nullable, no default: the previous image's INSERTs still work
        assert db.task_get(1)["origin"] is None                                           # an old row
        local = db.task_add(project="shop", repo="api", slug="a", title="a", prompt="p")
        assert db.task_get(local)["origin"] is None                                       # a local request stays null
        remote = db.task_add(project="shop", repo="api", slug="b", title="b", prompt="p", origin={"node": "n_0123456789abcdef", "user": "alice"})
        assert json.loads(db.task_get(remote)["origin"]) == {"node": "n_0123456789abcdef", "user": "alice"}
        db.task_update(remote, origin=None)
        assert db.task_get(remote)["origin"] is None
    finally:
        db.conn.close()
