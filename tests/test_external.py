"""v0.5.12 discovery of external Codex sessions (app/agents/codex_discovery.py), GET /api/external with the Codex half, POST
/api/external/{agent}/{id}/open, and the Codex rollouts in the nightly backup.

Everything runs on temp dirs: CODEX_HOME is the conftest tmp one, state_5.sqlite is built here (with Codex Desktop's imports, guardian
children, an archived thread, a Hermes thread and rollouts the DB never indexed), nothing reads the real ~/.codex, and a spy proves that
auth.json is never opened."""
import hashlib
import io
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import backup, claude_auth
from app.agents import codex_discovery as cd
from app.agents import registry
from app.config import settings
from app.db import DB

from .test_backup import backup_env  # noqa: F401  (a fixture: the nightly backup's temp restic repo, DB and repos)

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc).timestamp()
DAY = 86400
AUTH = {"installed": True, "version": "2.1.287 (Claude Code)", "loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}

A = "019e0001-aaaa-7aaa-8aaa-000000000001"        # a native terminal thread in shop/api
H1 = "019e0002-bbbb-7bbb-8bbb-000000000002"       # Hermes drives Codex from /home/.../brain (originator hermes, source vscode)
IMP1 = "019e0003-cccc-7ccc-8ccc-000000000003"     # Codex Desktop's import of a Claude session: no tokens, no model, source vscode
IMP2 = "019e0004-dddd-7ddd-8ddd-000000000004"     # an import named in external_agent_session_imports.json
G = "019e0005-eeee-7eee-8eee-000000000005"        # a guardian (auto-review) child
K = "019e0006-ffff-7fff-8fff-000000000006"        # a spawned child listed in thread_spawn_edges
AR = "019e0007-1111-7111-8111-000000000007"       # archived
NU = "019e0008-2222-7222-8222-000000000008"       # no user event
OLD = "019e0009-3333-7333-8333-000000000009"      # last touched 20 days ago
MS = "019e000a-4444-7444-8444-00000000000a"       # updated_at stored in milliseconds, cwd = the project folder itself
B = "019e000b-5555-7555-8555-00000000000b"        # a native rollout the DB never indexed
BS = "019e000c-6666-7666-8666-00000000000c"       # a rollout the DB never indexed that is a subagent's
BH = "019e000d-7777-7777-8777-00000000000d"       # a rollout the DB never indexed, driven by Hermes

THREADS_DDL = """
CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, created_at INTEGER, updated_at INTEGER, source TEXT, model_provider TEXT,
  cwd TEXT, title TEXT, name TEXT, sandbox_policy TEXT, approval_mode TEXT, tokens_used INTEGER DEFAULT 0, has_user_event INTEGER DEFAULT 0,
  archived INTEGER DEFAULT 0, git_branch TEXT, model TEXT, reasoning_effort TEXT);
CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT PRIMARY KEY, status TEXT);
CREATE TABLE external_agent_config_imports (id INTEGER PRIMARY KEY, kind TEXT);
CREATE TABLE remote_control_enrollments (id INTEGER PRIMARY KEY, name TEXT);
"""


def git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)
    return path


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def rollout(home, tid, *, at, originator="codex-tui", source="cli", cwd="/srv/projects/shop/api", mtime=None, extra=None):
    """A rollout file with a realistic first line (a big base_instructions block, account ids) and a token_count line."""
    d = datetime.fromtimestamp(at, timezone.utc)
    p = home / "sessions" / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}" / f"rollout-{d:%Y-%m-%dT%H-%M-%S}-{tid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"id": tid, "session_id": tid, "cwd": cwd, "originator": originator, "source": source, "cli_version": "0.160.0",
               "model_provider": "openai", "timestamp": iso(at), "creator_account_id": "acct-SECRET-1", "creator_user_id": "user-SECRET-1",
               "base_instructions": {"text": "You are Codex. " * 3000}, "runtime_workspace_roots": [cwd], **(extra or {})}
    with p.open("w") as f:
        f.write(json.dumps({"timestamp": iso(at), "type": "session_meta", "payload": payload}) + "\n")
        f.write(json.dumps({"timestamp": iso(at + 5), "type": "event_msg", "payload": {"type": "token_count"}}) + "\n")
    os.utime(p, (mtime or at, mtime or at))
    return p


def add_thread(conn, tid, home=None, **kw):
    row = {"id": tid, "rollout_path": None, "created_at": int(NOW - 2 * DAY), "updated_at": int(NOW - 600), "source": "cli",
           "model_provider": "openai", "cwd": "/srv/projects/shop/api", "title": f"thread {tid[-2:]}", "name": None, "tokens_used": 1000,
           "has_user_event": 1, "archived": 0, "git_branch": None, "model": "gpt-5.6-sol", "reasoning_effort": "medium"}
    row.update(kw)
    conn.execute(f"INSERT INTO threads({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))


@pytest.fixture
def box(codex_home, projects_dir, monkeypatch):
    """A Codex home with state_5.sqlite and rollouts, and a projects dir the threads' cwds live under."""
    monkeypatch.setattr(cd, "_wall", lambda: NOW)
    cd.reset_caches()
    shop = git_init(projects_dir / "shop" / "api")
    conn = sqlite3.connect(codex_home / "state_5.sqlite")
    conn.executescript(THREADS_DDL)
    cwd = str(shop)
    ra = rollout(codex_home, A, at=NOW - 3 * 3600, cwd=cwd)
    rh = rollout(codex_home, H1, at=NOW - 3600, originator="hermes", source="vscode", cwd="/home/user/brain")
    add_thread(conn, A, cwd=cwd, rollout_path=str(ra), title="Fix the checkout", tokens_used=12345, reasoning_effort="high",
               git_branch="feat/x", updated_at=int(NOW - 3600), created_at=int(NOW - 3 * 3600))
    add_thread(conn, H1, cwd="/home/user/brain", rollout_path=str(rh), source="vscode", tokens_used=500, model="gpt-5.5",
               updated_at=int(NOW - 60), title="hermes errand")
    add_thread(conn, IMP1, source="vscode", tokens_used=0, model=None, updated_at=int(NOW - 10), title="imported from Claude")
    add_thread(conn, IMP2, source="vscode", tokens_used=100, model="gpt-5.5", updated_at=int(NOW - 20), title="named in the imports file")
    add_thread(conn, G, source='{"subagent":{"other":"guardian"}}', tokens_used=10, updated_at=int(NOW - 30), title="guardian")
    add_thread(conn, K, source="cli", updated_at=int(NOW - 40), title="spawned child")
    conn.execute("INSERT INTO thread_spawn_edges VALUES (?,?,?)", (A, K, "closed"))
    add_thread(conn, AR, archived=1, updated_at=int(NOW - 50), title="archived")
    add_thread(conn, NU, has_user_event=0, updated_at=int(NOW - 55), title="no user event")
    add_thread(conn, OLD, updated_at=int(NOW - 20 * DAY), title="too old")
    add_thread(conn, MS, cwd=str(projects_dir / "shop"), updated_at=int((NOW - 7200) * 1000), title="stored in ms")
    conn.commit()
    conn.close()
    (codex_home / cd.IMPORTS_FILE).write_text(json.dumps({"imports": [{"claudeSessionId": "x", "threadId": IMP2}], "version": 1}))
    rollout(codex_home, B, at=NOW - 1800, cwd=cwd)                                                   # indexed by nobody
    rollout(codex_home, BS, at=NOW - 900, cwd=cwd, source={"subagent": {"thread_spawn": {"parent_thread_id": A, "depth": 1}}})
    rollout(codex_home, BH, at=NOW - 120, originator="hermes", source="vscode", cwd=str(projects_dir / "shop" / "api"))
    rollout(codex_home, "019e00ff-0000-7000-8000-0000000000ff", at=NOW - 30 * DAY, cwd=cwd)          # a month old: outside the window
    return SimpleNamespace(home=codex_home, projects=projects_dir, shop=shop, cwd=cwd)


def by_id(items):
    return {e["id"]: e for e in items}


# ---------------------------------------------------------------- discover()

def test_discover_keeps_user_started_native_threads_and_drops_the_rest(box):
    items = cd.discover(box.home, now=NOW, projects_dir=box.projects)
    assert [e["id"] for e in items] == [H1, BH, B, A, MS], "newest first; imports, guardian, spawned, archived, no-user-event, old, subagent rollout are gone"
    got = by_id(items)
    a = got[A]
    assert (a["agent"], a["source"], a["title"], a["name"], a["model"], a["effort"], a["tokens"], a["branch"]) == \
        ("codex", "state", "Fix the checkout", "Fix the checkout", "gpt-5.6-sol", "high", 12345, "feat/x")
    assert a["cwd"] == box.cwd and (a["project"], a["repo"], a["openable"]) == ("shop", "api", True)
    assert a["updated_at"] == iso(NOW - 3600) and a["created_at"] == iso(NOW - 3 * 3600)
    assert a["originator"] == "codex-tui" and a["badge"] is None and a["client"] == "cli" and a["entrypoint"] == "codex-tui"
    assert a["rollout"].startswith("2026/10/04/rollout-") and a["rollout"].endswith(f"{A}.jsonl")
    assert a["session_id"] == A and a["pid"] is None and a["tmux"] is None and a["status"] is None
    assert got[MS]["updated_at"] == iso(NOW - 7200), "a millisecond timestamp reads as seconds"
    assert (got[MS]["project"], got[MS]["repo"]) == ("shop", "root"), "a thread in the project folder itself is repo root"


def test_hermes_threads_are_badged_not_hidden(box):
    got = by_id(cd.discover(box.home, now=NOW, projects_dir=box.projects))
    h = got[H1]
    assert h["badge"] == "hermes" and h["originator"] == "hermes" and h["client"] == "vscode"
    assert h["project"] is None and h["repo"] is None and h["openable"] is False, "its cwd is not under the projects folder"
    assert got[BH]["badge"] == "hermes" and got[BH]["openable"] is True and got[BH]["source"] == "rollout"


def test_native_rollouts_the_db_missed_are_read_from_session_meta(box):
    got = by_id(cd.discover(box.home, now=NOW, projects_dir=box.projects))
    b = got[B]
    assert (b["source"], b["title"], b["model"], b["tokens"]) == ("rollout", None, None, None)
    assert b["cwd"] == box.cwd and b["originator"] == "codex-tui" and b["badge"] is None
    assert b["updated_at"] == iso(NOW - 1800) and b["created_at"] == iso(NOW - 1800)
    assert BS not in got, "a subagent rollout is dropped"
    assert "019e00ff-0000-7000-8000-0000000000ff" not in got


def test_imports_are_dropped_by_shape_and_by_the_imports_file(box):
    got = by_id(cd.discover(box.home, now=NOW, projects_dir=box.projects))
    assert IMP1 not in got and IMP2 not in got and G not in got and K not in got and AR not in got and NU not in got and OLD not in got
    # without the imports file only the shape-detected import is gone: IMP2 has tokens and a model, so it looks native
    (box.home / cd.IMPORTS_FILE).unlink()
    cd.reset_caches()
    again = by_id(cd.discover(box.home, now=NOW, projects_dir=box.projects))
    assert IMP2 in again and IMP1 not in again


def test_per_thread_tokens_are_shown_but_never_summed(box):
    items = cd.discover(box.home, now=NOW, projects_dir=box.projects)
    assert all("total" not in e for e in items)
    assert by_id(items)[A]["tokens"] == 12345            # display only: ccusage is the total (forks inherit their parent's tokens_used)


def test_the_limit_and_the_window_apply(box):
    assert len(cd.discover(box.home, now=NOW, projects_dir=box.projects, limit=2)) == 2
    assert [e["id"] for e in cd.discover(box.home, now=NOW, projects_dir=box.projects, limit=2)] == [H1, BH], "the newest two"
    # the 14-day window moves with `now`: 50 minutes before the 14-day mark only what was touched in the last 50 minutes is left
    ids = [e["id"] for e in cd.discover(box.home, now=NOW + 14 * DAY - 3000, projects_dir=box.projects)]
    assert ids == [H1, BH, B]
    assert cd.discover(box.home, now=NOW + 14 * DAY + 1, projects_dir=box.projects) == []


def test_the_state_db_is_opened_read_only(box):
    db_file = box.home / "state_5.sqlite"
    before = hashlib.sha256(db_file.read_bytes()).hexdigest()
    files = sorted(os.listdir(box.home))
    cd.discover(box.home, now=NOW, projects_dir=box.projects)
    assert hashlib.sha256(db_file.read_bytes()).hexdigest() == before and sorted(os.listdir(box.home)) == files
    conn = cd._open_ro(db_file)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM threads")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE t (x)")
    conn.close()
    db_file.chmod(0o400)                                  # a read-only file (and, on CI, a read-only directory is the same path)
    try:
        assert [e["id"] for e in cd.discover(box.home, now=NOW, projects_dir=box.projects)][:2] == [H1, BH]
    finally:
        db_file.chmod(0o600)


def test_a_wal_database_is_read_with_its_wal_and_leaves_no_files_behind(codex_home, projects_dir):
    """Codex's state db is a WAL database. While Codex runs, the newest threads sit in the -wal file (mode=ro reads them); once it is
    gone there is no -wal and nothing may be created beside the database by looking at it (immutable)."""
    cwd = str(git_init(projects_dir / "shop" / "api"))
    w = sqlite3.connect(codex_home / "state_5.sqlite")
    w.execute("PRAGMA journal_mode=WAL")
    w.executescript(THREADS_DDL)
    add_thread(w, A, cwd=cwd, updated_at=int(NOW - 100), title="in the wal")
    w.commit()                                                     # not checkpointed: the row is in state_5.sqlite-wal
    assert (codex_home / "state_5.sqlite-wal").exists()
    try:
        assert [e["id"] for e in cd.discover(codex_home, now=NOW, projects_dir=projects_dir)] == [A]
    finally:
        w.close()                                                  # the last connection closing checkpoints and removes -wal and -shm
    assert sorted(os.listdir(codex_home)) == ["state_5.sqlite"]
    assert [e["id"] for e in cd.discover(codex_home, now=NOW, projects_dir=projects_dir)] == [A]
    assert sorted(os.listdir(codex_home)) == ["state_5.sqlite"], "reading a quiet WAL database creates no -wal/-shm beside it"


def test_codex_rollout_discover_is_the_plans_entry_point(box):
    from app.agents import codex_rollout
    assert [e["id"] for e in codex_rollout.discover(box.home, now=NOW, projects_dir=box.projects)] == [e["id"] for e in cd.discover(box.home, now=NOW, projects_dir=box.projects)]


def test_the_newest_state_db_wins(box):
    other = sqlite3.connect(box.home / "state_4.sqlite")
    other.executescript(THREADS_DDL)
    add_thread(other, "019e0100-0000-7000-8000-000000000100", cwd=box.cwd, title="from the old db", updated_at=int(NOW - 5))
    other.commit()
    other.close()
    assert cd.state_db(box.home).name == "state_5.sqlite"
    (box.home / "state_10.sqlite").write_bytes(b"")        # a newer, empty/broken one is the newest: the scan then relies on the rollouts
    assert cd.state_db(box.home).name == "state_10.sqlite"
    ids = [e["id"] for e in cd.discover(box.home, now=NOW, projects_dir=box.projects)]
    assert "019e0100-0000-7000-8000-000000000100" not in ids and {BH, B} <= set(ids) and IMP1 not in ids


def test_schema_drift_missing_columns_and_tables(codex_home, projects_dir, monkeypatch):
    git_init(projects_dir / "shop" / "api")
    cwd = str(projects_dir / "shop" / "api")
    conn = sqlite3.connect(codex_home / "state_5.sqlite")      # a future/older schema: no has_user_event, archived, source, model, edges...
    conn.executescript("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, updated_at TEXT, title TEXT, rollout_path TEXT);")
    conn.execute("INSERT INTO threads VALUES (?,?,?,?,?)", (A, cwd, iso(NOW - 100), "iso timestamps", None))
    conn.execute("INSERT INTO threads VALUES (?,?,?,?,?)", (B, cwd, iso(NOW - 40 * DAY), "too old", None))
    conn.commit()
    conn.close()
    items = cd.discover(codex_home, now=NOW, projects_dir=projects_dir)
    assert [(e["id"], e["title"], e["model"], e["tokens"], e["badge"]) for e in items] == [(A, "iso timestamps", None, None, None)]
    assert items[0]["updated_at"] == iso(NOW - 100)


def test_no_db_a_foreign_table_or_garbage_leave_the_rollouts(codex_home, projects_dir):
    cwd = str(git_init(projects_dir / "shop" / "api"))
    rollout(codex_home, B, at=NOW - 100, cwd=cwd)
    assert [e["id"] for e in cd.discover(codex_home, now=NOW, projects_dir=projects_dir)] == [B]            # no state db at all
    conn = sqlite3.connect(codex_home / "state_5.sqlite")
    conn.execute("CREATE TABLE unrelated (x)")
    conn.commit()
    conn.close()
    assert [e["id"] for e in cd.discover(codex_home, now=NOW, projects_dir=projects_dir)] == [B]            # no threads table
    (codex_home / "state_5.sqlite").write_bytes(b"this is not a database" * 100)
    cd.reset_caches()
    assert [e["id"] for e in cd.discover(codex_home, now=NOW, projects_dir=projects_dir)] == [B]            # not a database
    assert cd.discover(codex_home / "missing", now=NOW, projects_dir=projects_dir) == []                   # no codex home at all


def test_rollout_paths_outside_sessions_are_not_trusted(box, tmp_path):
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text(json.dumps({"type": "session_meta", "payload": {"id": A, "originator": "evil", "cwd": box.cwd}}) + "\n")
    conn = sqlite3.connect(box.home / "state_5.sqlite")
    conn.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(outside), A))
    conn.commit()
    conn.close()
    assert cd.under_sessions(str(outside), box.home) is None and cd.under_sessions("../../etc/passwd", box.home) is None
    assert cd.under_sessions(str(box.home / "sessions" / ".." / "auth.json"), box.home) is None
    a = by_id(cd.discover(box.home, now=NOW, projects_dir=box.projects))[A]
    assert a["originator"] is None and a["badge"] is None and a["rollout"] is None, "the row is listed, but nothing is read from the stray path"


def test_nothing_ever_opens_auth_json(box, monkeypatch):
    (box.home / "auth.json").write_text('{"tokens": {"access_token": "SECRET-FAKE-LOGIN"}}')
    opened = []
    real_open, real_connect = open, sqlite3.connect

    def spy_open(f, *a, **k):
        opened.append(str(f))
        return real_open(f, *a, **k)

    def spy_connect(f, *a, **k):
        opened.append(str(f))
        return real_connect(f, *a, **k)
    monkeypatch.setattr("builtins.open", spy_open)
    monkeypatch.setattr(io, "open", spy_open)
    monkeypatch.setattr(sqlite3, "connect", spy_connect)
    cd.discover(box.home, now=NOW, projects_dir=box.projects)
    cd.snapshot(None, codex_home=box.home, projects_dir=box.projects, now=NOW)
    assert any("state_5.sqlite" in p for p in opened) and any(p.endswith(cd.IMPORTS_FILE) for p in opened), "the spy sees the reads that do happen"
    assert any(p.endswith(".jsonl") for p in opened)
    assert not any("auth.json" in p for p in opened)


# ---------------------------------------------------------------- the pieces: session_meta, source kinds, project_repo

def test_first_line_meta_returns_the_whitelist_only(codex_home):
    p = rollout(codex_home, A, at=NOW, extra={"history_mode": "full", "context_window": 272000})
    m = cd.first_line_meta(p)
    assert set(m) <= set(cd.META_KEYS) and m["id"] == A and m["originator"] == "codex-tui" and m["cwd"] == "/srv/projects/shop/api"
    assert m["creator_account_id"] == "acct-SECRET-1" and m["source"] == "cli"
    blob = json.dumps(m)
    assert "You are Codex" not in blob and "runtime_workspace_roots" not in blob and "creator_user_id" not in blob and "history_mode" not in blob
    d = rollout(codex_home, B, at=NOW, source={"subagent": {"thread_spawn": {"depth": 1}}})
    assert cd.first_line_meta(d)["source"] == {"subagent": {"thread_spawn": {"depth": 1}}}


def test_first_line_meta_edge_cases(tmp_path):
    cd.reset_caches()
    f = tmp_path / "r.jsonl"
    assert cd.first_line_meta(f) is None                                                      # missing
    f.write_text('{"type": "event_msg", "payload": {}}\n')
    assert cd.first_line_meta(f) is None                                                      # first line is not a session_meta
    g = tmp_path / "partial.jsonl"
    g.write_text('{"type": "session_meta", "payload": {"id": "x"')
    assert cd.first_line_meta(g) is None
    g.write_text(json.dumps({"type": "session_meta", "payload": {"session_id": A, "cwd": "/x"}}) + "\n")
    assert cd.first_line_meta(g) == {"cwd": "/x", "id": A}, "a half-written first line is retried, not cached; session_id stands in for id"
    huge = tmp_path / "huge.jsonl"
    huge.write_text('{"type": "session_meta", "payload": {"id": "' + "x" * (cd.META_MAX_BYTES + 10) + '"}}\n')
    assert cd.first_line_meta(huge) is None


def test_is_subagent_and_source_name():
    assert cd.is_subagent({"subagent": {"thread_spawn": {}}}) and cd.is_subagent({"sub_agent": "review"})
    assert cd.is_subagent('{"subagent": {"other": "guardian"}}') and cd.is_subagent("guardian") and cd.is_subagent("thread_spawn")
    assert cd.is_subagent("cli", '{"subagent": "compact"}')
    assert not cd.is_subagent("cli") and not cd.is_subagent("vscode") and not cd.is_subagent(None) and not cd.is_subagent({"custom": "x"})
    assert cd.source_name("vscode") == "vscode" and cd.source_name({"subagent": {}}) is None and cd.source_name('{"a": 1}') is None


def test_project_repo(tmp_path):
    root = tmp_path / "projects"
    (root / "shop" / "api").mkdir(parents=True)
    pr = lambda p: cd.project_repo(str(p), root)
    assert pr(root / "shop" / "api") == ("shop", "api")
    assert pr(root / "shop" / "api" / "src" / "deep") == ("shop", "api")
    assert pr(root / "shop") == ("shop", "root")
    assert pr(root) is None and pr(tmp_path) is None and pr(tmp_path / "projects-other" / "x") is None
    assert pr(root / ".hidden" / "x") is None and pr(root / "bad--name" / "x") is None
    assert cd.project_repo(None, root) is None and cd.project_repo("", root) is None
    link = tmp_path / "link"
    link.symlink_to(root)
    assert cd.project_repo(str(link / "shop" / "api"), root) == ("shop", "api"), "a symlinked projects dir resolves"


def test_snapshot_caches_for_the_ttl_and_writes_the_kv_record(box, monkeypatch, tmp_path):
    db = DB(tmp_path / "ext.db")
    clock = [100.0]
    monkeypatch.setattr(cd, "_clock", lambda: clock[0])
    s1 = cd.snapshot(db, codex_home=box.home, projects_dir=box.projects)
    assert s1["at"] == iso(NOW) and [e["id"] for e in s1["items"]][:2] == [H1, BH]
    kv = db.kv_get(cd.KV_KEY)["value"]
    assert kv["at"] == iso(NOW) and [e["id"] for e in kv["codex"]] == [e["id"] for e in s1["items"]]
    (box.home / "state_5.sqlite").unlink()
    clock[0] += cd.SNAPSHOT_TTL - 1
    assert cd.snapshot(db, codex_home=box.home, projects_dir=box.projects)["items"] == s1["items"], "within the ttl the scan is reused"
    clock[0] += 2
    s3 = cd.snapshot(db, codex_home=box.home, projects_dir=box.projects)
    assert {e["id"] for e in s3["items"]} == {H1, BH, B, A}, "past the ttl it is read again (the db is gone: the rollouts that exist are left)"
    assert {e["source"] for e in s3["items"]} == {"rollout"}
    assert {e["id"] for e in db.kv_get(cd.KV_KEY)["value"]["codex"]} == {H1, BH, B, A}


def test_discovery_has_no_tick_hook_nobody_reads_the_kv_record_between_polls(box):
    """Issue #32 (f): codex_discovery.tick was never registered and no page reads the kv record `external_sessions` without calling the
    endpoint (which refreshes it), so the hook was removed instead of adding a scan to the 15 s tick of a loaded box."""
    from app import samples
    assert not hasattr(cd, "tick") and all(getattr(f, "__module__", "") != cd.__name__ for f in samples.TICK_HOOKS)


def test_external_leaves_out_the_boards_own_threads_and_narrows_by_project(box, tmp_path):
    db = DB(tmp_path / "ext2.db")
    items, at = cd.external(db, codex_home=box.home, projects_dir=box.projects)
    assert at == iso(NOW) and A in {e["id"] for e in items}
    db.add_session(tmux_name="shop--api--s1", project="shop", repo="api", name="s1", launcher="resume", claude_session_id=A.upper(), agent="codex")
    db.add_session(tmux_name="shop--api--s2", project="shop", repo="api", name="s2", launcher="claude", claude_session_id=B, agent="claude")
    items, _ = cd.external(db, codex_home=box.home, projects_dir=box.projects)
    assert A not in {e["id"] for e in items}, "a thread a board row (any age) owns is not external"
    assert B in {e["id"] for e in items}, "a Claude row's id is not a Codex thread's"
    shop, _ = cd.external(db, "shop", codex_home=box.home, projects_dir=box.projects)
    assert {e["id"] for e in shop} == {BH, B, MS} and cd.external(db, "blog", codex_home=box.home, projects_dir=box.projects)[0] == []
    assert cd.find(db, B, codex_home=box.home, projects_dir=box.projects)["id"] == B
    assert cd.find(db, A, codex_home=box.home, projects_dir=box.projects) is None and cd.find(db, "nope", codex_home=box.home) is None


# ---------------------------------------------------------------- GET /api/external and POST /api/external/{agent}/{id}/open

@pytest.fixture
def board(lite_client, box, fake_tmux, fake_codex, monkeypatch):
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/claude")
    monkeypatch.setattr(claude_auth, "status", lambda: dict(AUTH))
    monkeypatch.setattr(claude_auth, "version", lambda: AUTH["version"])
    monkeypatch.setattr(registry, "_IS_LINUX", False)
    monkeypatch.setattr(registry, "_wall", lambda: NOW)
    registry.invalidate()
    git_init(box.projects / "shop" / "web")
    yield SimpleNamespace(client=lite_client, tmux=fake_tmux, box=box)
    registry.invalidate()
    cd.reset_caches()


def main_db():
    from app import main
    return main.db


def test_api_external_lists_codex_threads(board):
    r = board.client.get("/api/external?agent=codex", headers=H)
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"claude", "codex", "at"} and d["claude"] == [] and d["at"] == iso(NOW)
    assert [e["id"] for e in d["codex"]] == [H1, BH, B, A, MS]
    a = by_id(d["codex"])[A]
    assert {"agent", "source", "id", "session_id", "name", "title", "cwd", "model", "effort", "tokens", "created_at", "updated_at", "branch",
            "originator", "badge", "client", "rollout", "project", "repo", "openable"} <= set(a)
    assert by_id(d["codex"])[H1]["badge"] == "hermes"
    both = board.client.get("/api/external", headers=H).json()
    assert both["codex"] == d["codex"] and both["claude"] == [], "no agent filter = both lists (the registry is empty here)"
    assert board.client.get("/api/external?agent=claude", headers=H).json()["codex"] == []
    blob = json.dumps(d)
    assert "SECRET" not in blob and "You are Codex" not in blob and "creator_account_id" not in blob


def test_api_external_project_filter_and_bad_input(board):
    assert [e["id"] for e in board.client.get("/api/external?agent=codex&project=shop", headers=H).json()["codex"]] == [BH, B, A, MS]
    assert board.client.get("/api/external?agent=codex&project=other", headers=H).json()["codex"] == []
    assert board.client.get("/api/external?agent=codex&project=sho", headers=H).json()["codex"] == [], "a prefix of the name is not the project"
    assert board.client.get("/api/external?project=a--b", headers=H).status_code == 400
    assert board.client.get("/api/external?agent=gemini", headers=H).status_code == 400


def test_api_external_drops_threads_the_board_owns_and_writes_the_kv(board):
    db = main_db()
    db.add_session(tmux_name="shop--api--s1", project="shop", repo="api", name="s1", launcher="resume", claude_session_id=A, agent="codex")
    ids = [e["id"] for e in board.client.get("/api/external?agent=codex", headers=H).json()["codex"]]
    assert A not in ids and ids == [H1, BH, B, MS]
    assert A in [e["id"] for e in db.kv_get("external_sessions")["value"]["codex"]], "the record is the scan; ownership is applied per read"


def test_api_external_survives_a_broken_discovery(board, monkeypatch):
    monkeypatch.setattr(cd, "discover", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk on fire")))
    cd.reset_caches()
    assert board.client.get("/api/external?agent=codex", headers=H).json() == {"claude": [], "codex": [], "at": None}
    assert board.client.get("/api/state", headers=H).status_code == 200


@pytest.mark.parametrize("path", ["/api/external", f"/api/external/codex/{A}/open"])
def test_external_endpoints_need_an_identity_and_the_csrf_header_for_post(board, path):
    bad = {"Tailscale-User-Login": "mallory@example.com", "X-CCBoard": "1"}
    if path == "/api/external":
        assert board.client.get(path).status_code == 403 and board.client.get(path, headers=bad).status_code == 403
    else:
        assert board.client.post(path).status_code == 403
        assert board.client.post(path, headers=bad).status_code == 403
        assert board.client.post(path, headers={"Tailscale-User-Login": "alice@example.com"}).status_code == 403, "no X-CCBoard"
        assert board.client.get(path, headers=H).status_code in (404, 405)
    assert not board.tmux["sent"]


def test_open_resumes_the_codex_thread_in_its_directory(board):
    r = board.client.post(f"/api/external/codex/{A}/open", headers=H)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["tmux"] == "shop--api--s1" and d["agent"] == "codex" and d["agent_session_id"] == A and d["claude_session_id"] == A
    assert d["attach_url"] == "/tty/?arg=shop--api--s1" and (d["project"], d["repo"]) == ("shop", "api")
    assert d["cmd"].startswith("codex resume") and d["cmd"].endswith(f" {A}")
    assert board.tmux["sent"] == [("shop--api--s1", d["cmd"])]
    assert board.tmux["created"][0][0] == "shop--api--s1" and board.tmux["created"][0][1] == board.box.cwd
    row = main_db().open_rows()["shop--api--s1"]
    assert row["agent"] == "codex" and row["agent_session_id"] == A and row["cwd"] == board.box.cwd and row["launcher"] == "resume"
    # the thread is the board's now: it left the external list, and a second open is refused without touching tmux
    assert A not in [e["id"] for e in board.client.get("/api/external?agent=codex", headers=H).json()["codex"]]
    again = board.client.post(f"/api/external/codex/{A}/open", headers=H)
    assert again.status_code == 409 and "shop--api--s1" in again.json()["error"]
    assert len(board.tmux["sent"]) == 1


def test_open_a_thread_in_the_project_folder_uses_repo_root(board):
    d = board.client.post(f"/api/external/codex/{MS}/open", headers=H).json()
    assert d["tmux"] == "shop--root--s1" and (d["project"], d["repo"]) == ("shop", "root")
    assert board.tmux["created"][0][1] == str(board.box.projects / "shop")


def test_open_refuses_what_it_cannot_resume_here(board, monkeypatch):
    c = board.client
    assert c.post(f"/api/external/codex/{H1}/open", headers=H).status_code == 400, "a Hermes thread whose cwd is outside the projects folder"
    assert "projects folder" in c.post(f"/api/external/codex/{H1}/open", headers=H).json()["error"]
    assert c.post(f"/api/external/codex/{IMP1}/open", headers=H).status_code == 404, "an import is not listed, so it cannot be opened"
    assert c.post(f"/api/external/codex/{G}/open", headers=H).status_code == 404
    assert c.post("/api/external/codex/019effff-0000-7000-8000-000000000000/open", headers=H).status_code == 404
    assert c.post("/api/external/codex/not-a-uuid/open", headers=H).status_code == 400
    assert c.post(f"/api/external/gemini/{A}/open", headers=H).status_code == 400
    assert c.post(f"/api/external/shell/{A}/open", headers=H).status_code == 400
    import shutil
    shutil.rmtree(board.box.shop)                                                     # the directory is gone: nothing to resume in
    cd.reset_caches()
    assert c.post(f"/api/external/codex/{A}/open", headers=H).status_code == 400
    monkeypatch.setattr(settings, "codex_bin", lambda: None)
    assert c.post(f"/api/external/codex/{B}/open", headers=H).status_code == 400, "codex is not installed on this box"
    assert board.tmux["sent"] == [] and board.tmux["created"] == []


def test_open_refuses_a_directory_that_resolves_outside_the_projects_folder(board, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = board.box.projects / "shop" / "escape"
    link.symlink_to(outside, target_is_directory=True)
    sneaky = "019e00aa-0000-7000-8000-0000000000aa"
    rollout(board.box.home, sneaky, at=NOW - 60, cwd=str(link))
    cd.reset_caches()
    listed = by_id(board.client.get("/api/external?agent=codex", headers=H).json()["codex"])
    assert sneaky in listed and listed[sneaky]["project"] == "shop"
    r = board.client.post(f"/api/external/codex/{sneaky}/open", headers=H)
    assert r.status_code == 400 and ("symlink" in r.json()["error"] or "escapes" in r.json()["error"])
    assert board.tmux["sent"] == [] and board.tmux["created"] == []


def test_open_a_claude_session_from_the_registry(board):
    sid = "c7725076-aa65-4020-9087-97c18a3d922b"
    d = settings.claude_config_dir / "sessions"
    d.mkdir(parents=True)
    (d / "4242.json").write_text(json.dumps({"pid": 4242, "sessionId": sid, "cwd": str(board.box.projects / "shop" / "web"), "name": "other terminal",
                                             "status": "idle", "updatedAt": int(NOW * 1000) - 5000, "startedAt": int(NOW * 1000) - 60000}))
    registry.invalidate()
    ext = board.client.get("/api/external?agent=claude", headers=H).json()["claude"]
    assert [e["session_id"] for e in ext] == [sid]
    r = board.client.post(f"/api/external/claude/{sid}/open", headers=H)
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["agent"] == "claude" and j["tmux"] == "shop--web--s1" and "--resume" in j["cmd"] and j["cmd"].endswith(sid)
    assert main_db().open_rows()["shop--web--s1"]["agent_session_id"] == sid
    assert board.client.post(f"/api/external/claude/{sid}/open", headers=H).status_code == 409
    assert board.client.post("/api/external/claude/c7725076-aa65-4020-9087-000000000000/open", headers=H).status_code == 404


# ---------------------------------------------------------------- backup

def test_the_nightly_backup_includes_the_codex_rollouts_and_never_the_codex_home(backup_env, codex_home):  # noqa: F811
    sessions = codex_home / "sessions" / "2026" / "10" / "04"
    sessions.mkdir(parents=True)
    (sessions / f"rollout-2026-10-04T01-00-00-{A}.jsonl").write_text('{"type": "session_meta"}\n')
    (codex_home / "auth.json").write_text('{"tokens": {"access_token": "SECRET-FAKE-LOGIN"}}')
    st = backup.run(push=False)
    assert st["status"] == "ok" and st["errors"] == []
    assert str(codex_home / "sessions") in st["paths"], st["paths"]
    assert str(codex_home) not in st["paths"] and not any(p.endswith("auth.json") for p in st["paths"])
    calls = (backup_env["repo"] / "calls.log").read_text()
    assert "SECRET" not in calls and "auth.json" not in calls and str(codex_home / "sessions") in calls


def test_the_backup_skips_a_missing_sessions_dir(backup_env, codex_home):  # noqa: F811
    st = backup.run(push=False)
    assert st["status"] == "ok" and not any("sessions" in p for p in st["paths"])
