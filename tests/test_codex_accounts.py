"""v0.5.17e, saved Codex logins and the one-tap account switch: app/codex_accounts.py, the /api/codex-accounts endpoints, the adapter's
`login --help` probe and foreign-process scan, the demo keys.

Everything runs on temp directories: settings.data_dir and settings.codex_home are the sandboxed ones from the fixtures, the login session is
the fake tmux (it never runs what is typed), `codex` is a fake script from tests/conftest.py (write_fake_codex, run by hand where a test wants
its output) and nothing reads or writes the real ~/.codex, ~/.claude or ~/.claude.json. Logins are opaque bytes with a sentinel in them
(SECRET-CODEX-A ...); the secrecy tests look for the sentinels everywhere. The rollout fixtures (session_meta with creator_account_id,
rate-limit events with plan_type) are shaped from the brief and the box notes, not captured from a real Codex.
"""
import ast
import inspect
import json
import logging
import os
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import account_store, accounts, codex_accounts as cx, login_problem, samples, tmux
from app.agents import codex as codex_agent
from app.platform import browser_stub
from app.config import settings
from app.db import DB, iso
from tests.conftest import FAKE_AUTH_BLOB, write_fake_codex
from tests.proc_fake import use_fake_proc

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
T0 = int(time.time()) + 1000     # after every real-clock event the fixtures write (seeds), so event order is the order of the calls
AUTH = {"A": b'{"tokens":{"access_token":"SECRET-CODEX-A"}}', "B": b'{"tokens":{"access_token":"SECRET-CODEX-B"}}',
        "A2": b'{"tokens":{"access_token":"SECRET-CODEX-A-refreshed"}}', "B2": b'{"tokens":{"access_token":"SECRET-CODEX-B-refreshed"}}',
        "C": b'{"tokens":{"access_token":"SECRET-CODEX-C"}}'}
ACCT_A, ACCT_B = "acct-aaaaaaaa-0000-4000-8000-000000000001", "acct-bbbbbbbb-0000-4000-8000-000000000002"
DEVICE_PANE = """Welcome to Codex [v0.160.0]

Follow these steps to sign in with ChatGPT using device code authorization:

1. Open this link in your browser and sign in to your account
   https://auth.openai.com/codex/device

2. Enter this one-time code (expires in 15 minutes)
   ABCD-12345

Device codes are a common phishing target. Never share this code.
"""
_mtime = [time.time_ns()]
REAL_WATCH = cx.watch_login          # the box fixture silences the watcher thread (it would race a test that drives finalize by hand); the watcher tests put it back


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def put_auth(dirpath: Path, blob: bytes, age: float = 100.0) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / "auth.json"
    p.write_bytes(blob)
    os.chmod(p, 0o600)
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def write_rollout(codex_home: Path, name: str, *, account=None, user=None, created=None, plan=None, day="2026/10/04") -> Path:
    """A rollout the way Codex writes it: line 1 a session_meta (payload carries the ids), later lines events (a rate-limit one with the plan)."""
    d = codex_home / "sessions" / day
    d.mkdir(parents=True, exist_ok=True)
    payload = {"id": name, "timestamp": iso(created if created is not None else time.time()), "cwd": "/srv/projects/shop/api", "originator": "codex_cli_rs"}
    if account:
        payload["creator_account_id"] = account
    if user:
        payload["creator_user_id"] = user
    lines = [json.dumps({"timestamp": payload["timestamp"], "type": "session_meta", "payload": payload})]
    lines.append(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "hello"}}))
    if plan:
        lines.append(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"plan_type": plan,
                                                                                                      "primary": {"used_percent": 12.0}}}}))
    p = d / f"rollout-{name}.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return p


@pytest.fixture
def box(projects_dir, tmp_path, monkeypatch, fake_tmux):
    """Saved Codex logins on, over the sandboxed data dir and CODEX_HOME, a fake codex 0.160, the fake tmux, an empty fake /proc."""
    path = write_fake_codex(tmp_path / "fakebin" / "codex", device_auth=True)
    monkeypatch.setattr(settings, "codex_bin", lambda: str(path))
    codex_agent.reset_caches()
    proc = tmp_path / "proc"
    proc.mkdir()
    use_fake_proc(monkeypatch, proc)
    monkeypatch.setattr(account_store, "_sleep", lambda s: None)
    monkeypatch.setattr(cx, "watch_login", lambda db: None)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    live = settings.codex_home
    b = SimpleNamespace(db=DB(settings.db_path), live=live, auth=live / "auth.json", store=settings.data_dir / "codex-accounts", tmp=tmp_path,
                        tmux=fake_tmux, proc=proc, fake=path)

    def log_in(who, age=100.0):
        """What `codex login` leaves: the login file."""
        return put_auth(live, AUTH[who], age)

    def onboard(who="A"):
        """Log in live and let the board save it (the lifespan seed or the tick does this on the box)."""
        log_in(who)
        key = cx.seed_current(b.db)
        assert key and cx.has_saved(key)
        return key

    def add(who, label):
        """A second account saved without being live (what a finished add-account login leaves)."""
        return cx._new_account(b.db, AUTH[who], label, T0)

    def kvv(name):
        r = b.db.kv_get(name)
        return r["value"] if r else None

    def two(label_a="Work", label_b="Home"):
        """A is live and current, B is saved: returns (ka, kb)."""
        ka = onboard("A")
        cx.set_label(b.db, ka, label_a)
        kb = add("B", label_b)
        return ka, kb

    def pending(who="C", age=100.0):
        p = cx.pending_dir()
        p.mkdir(parents=True, exist_ok=True)
        return put_auth(p, AUTH[who], age)

    def events():
        return [(k, json.loads(m)) for _a, k, _v, m in b.db.samples_query("cacct", None, iso(0))]

    b.log_in, b.onboard, b.add, b.kv, b.two, b.pending, b.events = log_in, onboard, add, kvv, two, pending, events
    yield b
    cx.stop_watchers()


def tree_has_no_temp_files(root: Path) -> bool:
    return not [p for p in root.rglob("*") if p.name.endswith(".tmp")]


# ---------------------------------------------------------------- the adapter: login --help, the device_auth capability, foreign processes

LOGIN_HELP_0160 = (Path(__file__).parent / "fixtures" / "codex_login_help_0160.txt").read_text()
LOGIN_HELP_0145 = (Path(__file__).parent / "fixtures" / "codex_login_help_0145.txt").read_text()


def test_parse_login_help_finds_device_auth_only_where_it_is_an_option():
    assert codex_agent.parse_login_help(LOGIN_HELP_0160) == {"device_auth": True}
    assert codex_agent.parse_login_help(LOGIN_HELP_0145) == {"device_auth": False}
    mentioned = LOGIN_HELP_0145.replace("Read the API key from stdin", "Instead of --device-auth, read the API key from stdin")
    assert codex_agent.parse_login_help(mentioned) == {"device_auth": False}, "a flag only mentioned in another option's text is not an option"
    assert codex_agent.parse_login_help("junk") is None and codex_agent.parse_login_help("") is None


def test_device_auth_is_a_capability_probed_once_per_binary(box):
    ag = codex_agent.CodexAgent()
    assert ag.capabilities()["device_auth"] is True and ag.device_auth() is True
    write_fake_codex(box.fake, device_auth=False)                 # a binary that was replaced (its stamp moved): probed again
    os.utime(box.fake, (1, 1))
    assert ag.device_auth() is False and ag.capabilities()["device_auth"] is False


def test_no_binary_or_a_failed_probe_means_no_device_auth_and_is_retried_after_a_minute(box, monkeypatch):
    ag = codex_agent.CodexAgent()
    real = codex_agent._run
    broken = [True]
    calls = []

    def run(argv, timeout=codex_agent.CMD_TIMEOUT):
        if argv[1:] == ["login", "--help"]:
            calls.append(argv)
            if broken[0]:
                return 1, "", "boom"
        return real(argv, timeout)
    monkeypatch.setattr(codex_agent, "_run", run)
    clock = [1000.0]
    monkeypatch.setattr(codex_agent, "_clock", lambda: clock[0])
    assert ag.device_auth() is False and ag.device_auth(fetch=False) is None, "a failed probe is not an answer: 'not known yet'"
    broken[0] = False
    assert ag.device_auth() is False and len(calls) == 1, "not retried on every call"
    clock[0] += codex_agent.FAIL_TTL + 1
    assert ag.device_auth() is True and ag.device_auth(fetch=False) is True and len(calls) == 2


def test_no_binary_means_the_baseline(projects_dir):
    ag = codex_agent.CodexAgent()
    assert ag.bin() is None and ag.device_auth() is False and ag.login_caps() == {"device_auth": False} and ag.capabilities()["device_auth"] is False


def test_fetch_false_never_starts_a_process_and_warming_probes_in_the_background(box, monkeypatch):
    ag = codex_agent.CodexAgent()
    calls = []
    real = codex_agent._run
    monkeypatch.setattr(codex_agent, "_run", lambda argv, timeout=codex_agent.CMD_TIMEOUT: calls.append(argv) or real(argv, timeout))
    assert ag.device_auth(fetch=False) is None and calls == []
    ag.warm_login_caps()
    deadline = time.time() + 5
    while time.time() < deadline and ag.device_auth(fetch=False) is None:
        time.sleep(0.02)
    assert ag.device_auth(fetch=False) is True and len(calls) == 1 and calls[0][1:] == ["login", "--help"]


def fake_proc(root: Path, pid: int, comm: str, ppid: int = 1) -> None:
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "comm").write_text(comm + "\n")
    (d / "stat").write_text(f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1\n")


def test_foreign_processes_counts_codex_processes_the_board_did_not_start(box):
    fake_proc(box.proc, 100, "codex", ppid=1)                      # a Hermes agent's app-server daemon
    fake_proc(box.proc, 101, "codex", ppid=500)                    # one in the board's tmux pane 500 ...
    fake_proc(box.proc, 500, "zsh", ppid=400)
    fake_proc(box.proc, 102, "codex", ppid=101)                    # ... and its child
    fake_proc(box.proc, 103, "node", ppid=1)
    fake_proc(box.proc, 104, "bash", ppid=1)
    assert codex_agent.foreign_processes([]) == 3
    assert codex_agent.foreign_processes([500]) == 1
    assert codex_agent.foreign_processes([500, 100]) == 0
    assert codex_agent.foreign_processes(["x", True, None]) == 3, "only real pids count"


def test_foreign_processes_without_proc_is_zero_and_never_matches_by_command_line(box, monkeypatch):
    use_fake_proc(monkeypatch, box.tmp / "no-such-proc")
    assert codex_agent.foreign_processes([]) == 0
    d = box.proc / "7"                                             # a process whose command line mentions codex is not a codex process
    d.mkdir()
    (d / "comm").write_text("uvicorn\n")
    (d / "cmdline").write_text("uvicorn\0wt-codexacct\0")
    assert codex_agent.foreign_processes([]) == 0


# ---------------------------------------------------------------- slots, modes, save, seed

def test_a_slot_is_a_24_hex_name_and_anything_else_is_refused_before_a_path_is_built(box):
    key = "0123456789abcdef01234567"
    assert cx.slot_dir(key) == box.store / key
    for bad in ("", "..", "../x", "a/b", key + "0", key[:-1], key.upper(), "x" * 24, None, "0123456789abcdef0123456\n"):
        with pytest.raises(ValueError):
            cx.slot_dir(bad)
    assert cx.has_saved("../../etc/passwd") is False


def test_seed_keeps_an_unknown_live_login_as_an_unlabelled_account_byte_exact_with_private_modes(box):
    assert cx.seed_current(box.db) is None and not box.store.exists(), "nobody is logged in: nothing is created"
    box.log_in("A")
    key = cx.seed_current(box.db, T0)
    assert key and cx.has_saved(key) and len(key) == 24
    s = cx.slot_dir(key)
    assert (s / "auth.json").read_bytes() == AUTH["A"], "a byte copy"
    assert mode(box.store) == 0o700 and mode(s) == 0o700 and mode(s / "auth.json") == 0o600 and mode(s / "meta.json") == 0o600
    assert not (s / "auth.json.prev").exists(), "nothing to rotate on a first save"
    meta = json.loads((s / "meta.json").read_text())
    assert meta["label"] == f"codex login {iso(T0)[:10]}" and meta["added_at"] == iso(T0) and "account_id" not in meta
    rec = box.kv("codex_accounts")[key]
    assert rec["key"] == key and rec["label"] == meta["label"] and rec["saved"] is True and rec["account_id"] is None
    assert box.kv("codex_account_current")["key"] == key
    assert box.events() == [(key, {"to": key})]
    assert box.auth.read_bytes() == AUTH["A"], "the live file is not touched"
    assert tree_has_no_temp_files(box.store) and tree_has_no_temp_files(box.live)


def test_the_key_comes_from_a_random_token_not_from_the_login(box):
    a = box.onboard("A")
    box.auth.unlink()
    box.kv("codex_account_current")
    b2 = box.add("A", "same bytes")                              # the same bytes under another name: another account
    assert a != b2 and cx.slot_dir(a) != cx.slot_dir(b2)
    import hashlib
    assert a != hashlib.sha256(AUTH["A"]).hexdigest()[:24]


def test_directories_that_exist_wider_are_tightened(box):
    box.store.mkdir(parents=True)
    os.chmod(box.store, 0o755)
    key = box.onboard("A")
    os.chmod(cx.slot_dir(key), 0o755)
    cx._slot_ready(key)
    assert mode(box.store) == 0o700 and mode(cx.slot_dir(key)) == 0o700


def test_seed_is_a_noop_when_the_live_login_is_already_a_slots_copy(box):
    key = box.onboard("A")
    before = (cx.slot_dir(key) / "auth.json").stat().st_mtime_ns
    assert cx.seed_current(box.db) == key
    assert (cx.slot_dir(key) / "auth.json").stat().st_mtime_ns == before and len(box.kv("codex_accounts")) == 1
    assert len(box.events()) == 1


def test_seed_saves_a_refreshed_token_into_the_current_slot_only_once_the_file_is_quiet(box):
    key = box.onboard("A")
    box.log_in("A2", age=5)                                      # Codex refreshed its token while the board was down, 5 s ago
    assert cx.seed_current(box.db) == key and cx.slot_dir(key).joinpath("auth.json").read_bytes() == AUTH["A"], "not yet: still being written"
    box.log_in("A2", age=cx.SYNC_SETTLE + 5)
    assert cx.seed_current(box.db) == key
    s = cx.slot_dir(key)
    assert (s / "auth.json").read_bytes() == AUTH["A2"] and (s / "auth.json.prev").read_bytes() == AUTH["A"] and mode(s / "auth.json.prev") == 0o600
    assert len(box.kv("codex_accounts")) == 1, "the refresh is not a second account"
    box.log_in("B2")                                             # a third generation (a new login: a refresh never goes back to an older one): .prev holds exactly one generation
    cx.seed_current(box.db)
    assert (s / "auth.json").read_bytes() == AUTH["B2"] and (s / "auth.json.prev").read_bytes() == AUTH["A2"]
    box.log_in("A2")                                             # the older generation written back by an older process: not a refresh, the saved login stays
    cx.seed_current(box.db)
    assert (s / "auth.json").read_bytes() == AUTH["B2"] and (s / "auth.json.prev").read_bytes() == AUTH["A2"]


def test_a_young_unknown_login_is_not_adopted_until_it_has_settled(box):
    box.log_in("A", age=0.5)
    assert cx.seed_current(box.db) is None and not box.store.exists()
    box.log_in("A", age=cx.SETTLE + 1)
    assert cx.seed_current(box.db)


def test_seed_brings_back_slots_the_database_lost_from_their_meta(box):
    ka, kb = box.two("Work", "Home")
    cx._put_record(box.db, kb, account_id=ACCT_B, plan="plus")
    box.db.kv_set("codex_accounts", {})
    box.db.kv_set("codex_account_current", {})
    cx.seed_current(box.db)
    accts = box.kv("codex_accounts")
    assert accts[ka]["label"] == "Work" and accts[kb]["label"] == "Home" and accts[kb]["account_id"] == ACCT_B and accts[kb]["plan"] == "plus"
    assert accts[kb]["saved"] is True and box.kv("codex_account_current")["key"] == ka, "the live file names its slot again"


# ---------------------------------------------------------------- switch

def test_switch_round_trip_puts_the_right_bytes_in_the_right_places(box):
    ka, kb = box.two()
    r = cx.switch(box.db, kb, now=T0 + 5)
    assert r == {"ok": True, "already": False, "from": ka, "to": kb, "warnings": []}
    assert box.auth.read_bytes() == AUTH["B"] and mode(box.auth) == 0o600
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["A"], "A's login is kept"
    assert tree_has_no_temp_files(box.live)
    r = cx.switch(box.db, ka, now=T0 + 6)
    assert r["from"] == kb and r["to"] == ka and box.auth.read_bytes() == AUTH["A"]
    assert (cx.slot_dir(kb) / "auth.json").read_bytes() == AUTH["B"]


def test_a_token_refreshed_by_a_running_codex_travels_with_the_account(box):
    ka, kb = box.two()
    box.log_in("A2")                                             # Codex refreshed A's token on its own
    cx.switch(box.db, kb)
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["A2"] and (cx.slot_dir(ka) / "auth.json.prev").read_bytes() == AUTH["A"]
    cx.switch(box.db, ka)
    assert box.auth.read_bytes() == AUTH["A2"], "and comes back with it"


def test_switching_to_the_live_account_is_a_noop(box):
    ka, _kb = box.two()
    before = box.auth.stat().st_mtime_ns
    assert cx.switch(box.db, ka) == {"ok": True, "already": True, "from": ka, "to": ka, "warnings": []}
    assert box.auth.stat().st_mtime_ns == before and len(box.events()) == 1 and box.kv("codex_account_switch") is None


def test_switch_refuses_unknown_accounts_and_accounts_without_a_saved_login(box):
    ka, kb = box.two()
    for bad in ("nope", "../../etc", "0123456789abcdef01234567", ""):
        with pytest.raises(LookupError, match="no such account"):
            cx.switch(box.db, bad)
    cx.forget(box.db, kb)
    with pytest.raises(cx.NoSavedLogin, match="no saved login"):
        cx.switch(box.db, kb)
    assert box.auth.read_bytes() == AUTH["A"]


def test_a_saved_login_that_was_emptied_is_refused_and_changes_nothing(box):
    ka, kb = box.two()
    (cx.slot_dir(kb) / "auth.json").write_bytes(b"")
    with pytest.raises(cx.NoSavedLogin):
        cx.switch(box.db, kb)
    assert box.auth.read_bytes() == AUTH["A"] and box.kv("codex_account_current")["key"] == ka


def add_codex_row(db, name="shop--api--s1", agent="codex"):
    db.add_session(tmux_name=name, project="shop", repo="api", name=name.split("--")[-1], launcher="codex" if agent == "codex" else "claude", agent=agent)


def alive(box, name):
    box.tmux["sessions"][name] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%2", "command": "codex", "path": "/", "pid": 2, "env": {}}


def test_switch_is_busy_while_a_board_codex_session_is_open(box):
    ka, kb = box.two()
    add_codex_row(box.db)
    alive(box, "shop--api--s1")
    with pytest.raises(cx.Busy, match=r"close the board's Codex sessions first; a running Codex keeps its login and would write it back"):
        cx.switch(box.db, kb)
    assert box.auth.read_bytes() == AUTH["A"] and not (cx.slot_dir(ka) / "auth.json.prev").exists()
    box.tmux["sessions"].clear()                                 # the row is still open but its tmux session is gone: nothing runs
    assert cx.switch(box.db, kb)["to"] == kb


def test_a_claude_session_does_not_block_a_codex_switch(box):
    ka, kb = box.two()
    add_codex_row(box.db, "shop--api--s2", agent="claude")
    alive(box, "shop--api--s2")
    assert cx.switch(box.db, kb)["from"] == ka


def test_switch_is_busy_while_a_login_from_settings_is_in_flight(box):
    ka, kb = box.two()
    cx.start_login(box.db, "Third")
    with pytest.raises(cx.Busy, match="a login is in progress"):
        cx.switch(box.db, kb)
    cx.cancel_login()
    assert cx.switch(box.db, kb)["to"] == kb


def test_switch_refuses_when_the_current_login_cannot_be_saved_first(box, monkeypatch):
    ka, kb = box.two()
    box.log_in("A2")
    monkeypatch.setattr(cx, "_store_auth", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(cx.SwitchRefused, match="could not save the current login first; nothing was changed"):
        cx.switch(box.db, kb)
    assert box.auth.read_bytes() == AUTH["A2"] and box.kv("codex_account_current")["key"] == ka


def test_a_failure_putting_the_new_login_in_place_leaves_the_previous_login_as_it_was(box, monkeypatch):
    ka, kb = box.two()
    real = os.replace

    def flaky(src, dst, *a, **k):
        if Path(dst) == box.auth:
            raise OSError("boom")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(cx.SwitchRefused, match="the previous login is still in place"):
        cx.switch(box.db, kb)
    assert box.auth.read_bytes() == AUTH["A"] and box.kv("codex_account_current")["key"] == ka and box.kv("codex_account_switch") is None
    assert tree_has_no_temp_files(box.live)


def test_a_switch_moves_the_current_account_and_writes_the_cacct_event(box):
    ka, kb = box.two()
    assert box.events() == [(ka, {"to": ka})]
    cx.switch(box.db, kb, now=T0 + 99)
    assert box.kv("codex_account_current") == {"key": kb, "since": iso(T0 + 99)}
    assert box.events()[-1] == (kb, {"from": ka, "to": kb})
    assert box.kv("codex_account_switch") == {"from": ka, "to": kb, "at": iso(T0 + 99), "repairs": 0}
    assert cx.current(box.db) == kb
    assert "cacct" in samples.CATALOGUE and samples.CATALOGUE["cacct"]["agg"] == "events"


def test_a_login_that_matches_no_slot_is_the_current_accounts_refresh_and_is_saved_before_the_switch(box):
    """With a current account the board cannot tell `codex login` to somebody else from a token refresh (nothing in the file says): the
    far commoner refresh wins, and the generation it replaces is kept as .prev."""
    ka = box.onboard("A")
    kb = box.add("B", "Home")
    box.log_in("C")
    r = cx.switch(box.db, kb)
    assert r["from"] == ka and box.auth.read_bytes() == AUTH["B"]
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["C"] and (cx.slot_dir(ka) / "auth.json.prev").read_bytes() == AUTH["A"]


def test_a_live_login_nobody_saved_is_kept_as_an_unlabelled_account_when_there_is_no_current_account(box):
    ka = box.onboard("A")
    kb = box.add("B", "Home")
    box.db.kv_set("codex_account_current", {})                   # the board has lost track of who is live (a restored database)
    box.log_in("C")
    assert cx._matching_slot(AUTH["C"]) is None
    r = cx.switch(box.db, kb)
    assert r["from"] and r["from"] not in (ka, kb) and box.auth.read_bytes() == AUTH["B"]
    assert (cx.slot_dir(r["from"]) / "auth.json").read_bytes() == AUTH["C"], "its login is one tap away"
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["A"], "and A's saved login was not touched"
    assert box.kv("codex_accounts")[r["from"]]["label"].startswith("codex login ")
    assert cx.switch(box.db, r["from"])["to"] == r["from"] and box.auth.read_bytes() == AUTH["C"]


def test_a_login_file_that_is_a_slots_copy_is_a_hand_switch_and_is_adopted(box):
    ka, kb = box.two()
    box.log_in("B")                                              # somebody copied B's file over the live one
    assert cx.switch(box.db, kb) == {"ok": True, "already": True, "from": kb, "to": kb, "warnings": []}
    assert box.kv("codex_account_current")["key"] == kb and box.events()[-1] == (kb, {"from": ka, "to": kb})


def test_a_switch_right_after_a_switch_refuses_a_live_file_it_cannot_explain(box):
    ka, kb = box.two()
    cx.switch(box.db, kb)
    box.log_in("A2")                                             # an older Codex wrote A's refreshed token back, or B's own refresh: unknown
    with pytest.raises(cx.SwitchRefused, match="cannot be told from an older Codex's write-back"):
        cx.switch(box.db, ka)
    assert (cx.slot_dir(kb) / "auth.json").read_bytes() == AUTH["B"], "B's saved login was not overwritten with A's refreshed token"


def test_the_switch_warns_when_other_codex_processes_keep_the_previous_login(box):
    ka, kb = box.two()
    fake_proc(box.proc, 100, "codex", ppid=1)                    # the daemon of an agent that is not the board
    r = cx.switch(box.db, kb)
    assert r["warnings"] == ["other Codex processes on this box keep the previous login until they restart"]
    box.log_in("B")
    box.tmux["sessions"]["shop--api--s1"] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%3", "command": "zsh", "path": "/", "pid": 300, "env": {}}
    fake_proc(box.proc, 100, "zsh", ppid=1)                      # now the only codex is a child of the board's own pane
    fake_proc(box.proc, 301, "codex", ppid=300)
    assert cx.switch(box.db, ka)["warnings"] == []


def test_unsupported_without_a_codex_binary_refuses_every_mutation_and_says_why(box, monkeypatch):
    ka, kb = box.two()
    monkeypatch.setattr(settings, "codex_bin", lambda: None)
    for call in (lambda: cx.seed_current(box.db), lambda: cx.switch(box.db, kb), lambda: cx.forget(box.db, kb), lambda: cx.start_login(box.db, "x")):
        with pytest.raises(cx.Unsupported, match="codex is not installed"):
            call()
    assert cx.tick(box.db) is None and cx.finalize(box.db) is None
    v = cx.view(box.db)
    assert v["store"] == {"supported": False, "add": False, "reason": "codex is not installed", "count": 0} and not any(r["saved"] for r in v["list"])
    assert box.auth.read_bytes() == AUTH["A"]


# ---------------------------------------------------------------- forget, rename

def test_forget_deletes_the_slot_but_not_the_record_and_never_the_live_account(box):
    ka, kb = box.two()
    with pytest.raises(cx.Busy, match="live login"):
        cx.forget(box.db, ka)
    assert cx.has_saved(ka)
    for bad in ("nope", "../x"):
        with pytest.raises(LookupError):
            cx.forget(box.db, bad)
    assert cx.forget(box.db, kb) == {"ok": True, "forgotten": True}
    assert not cx.slot_dir(kb).exists() and not cx.has_saved(kb)
    assert box.kv("codex_accounts")[kb]["saved"] is False and box.kv("codex_accounts")[kb]["label"] == "Home", "the record stays"
    assert cx.forget(box.db, kb)["forgotten"] is False
    with pytest.raises(cx.NoSavedLogin):
        cx.switch(box.db, kb)


def test_forget_also_refuses_the_account_whose_copy_is_the_live_file_even_if_the_kv_says_otherwise(box):
    ka, kb = box.two()
    box.db.kv_set("codex_account_current", {"key": kb, "since": iso(T0)})        # stale: the live file is A's
    with pytest.raises(cx.Busy):
        cx.forget(box.db, ka)


def test_set_label_cleans_requires_text_and_updates_meta(box):
    ka, _kb = box.two()
    row = cx.set_label(box.db, ka, "  Per\x00sonal \n\t account  ")
    assert row["label"] == "Per sonal account" and json.loads((cx.slot_dir(ka) / "meta.json").read_text())["label"] == "Per sonal account"
    assert len(cx.set_label(box.db, ka, "x" * 100)["label"]) == 60
    for bad in (None, "", "   \n", "\x00\x01"):
        with pytest.raises(ValueError, match="give the account a name"):
            cx.set_label(box.db, ka, bad)
    with pytest.raises(ValueError):
        cx.set_label(box.db, ka, 5)
    with pytest.raises(LookupError):
        cx.set_label(box.db, "nope", "x")
    assert box.kv("codex_accounts")[ka]["label"] == "x" * 60


# ---------------------------------------------------------------- the tick

def test_tick_undoes_a_write_back_of_the_previous_login_at_most_five_times_inside_ten_minutes(box):
    ka, kb = box.two()
    cx.switch(box.db, kb, now=T0)
    for n in range(1, cx.MAX_REPAIRS + 1):
        box.log_in("A")                                          # an older Codex process wrote A's login back
        cx.tick(box.db, T0 + 30)
        assert box.auth.read_bytes() == AUTH["B"] and box.kv("codex_account_switch")["repairs"] == n
        assert mode(box.auth) == 0o600
    box.log_in("A")
    cx.tick(box.db, T0 + 30)
    assert box.auth.read_bytes() == AUTH["A"] and box.kv("codex_account_switch")["repairs"] == 5, "the sixth is left alone"
    assert box.kv("codex_account_current")["key"] == kb, "and the slots are untouched: B's saved login was not overwritten with A's"
    assert (cx.slot_dir(kb) / "auth.json").read_bytes() == AUTH["B"] and (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["A"]
    assert box.kv("codex_account_switch")["last_repair"]


def test_no_repair_after_the_window_the_revert_is_then_a_hand_switch(box):
    ka, kb = box.two()
    cx.switch(box.db, kb, now=T0)
    box.log_in("A")
    cx.tick(box.db, T0 + cx.REPAIR_WINDOW + 5)
    assert box.auth.read_bytes() == AUTH["A"] and box.kv("codex_account_switch")["repairs"] == 0
    assert box.kv("codex_account_current")["key"] == ka and box.events()[-1] == (ka, {"from": kb, "to": ka})


def test_bytes_that_match_no_slot_are_left_alone_inside_the_window_and_saved_into_the_current_slot_after_it(box):
    ka, kb = box.two()
    cx.switch(box.db, kb, now=T0)
    box.log_in("A2", age=cx.SYNC_SETTLE + 60)                    # nothing says whose refresh this is
    cx.tick(box.db, T0 + 30)
    assert (cx.slot_dir(kb) / "auth.json").read_bytes() == AUTH["B"], "not saved into B's slot: it may be A's token"
    assert len(box.kv("codex_accounts")) == 2, "and not adopted as a third account either"
    assert box.auth.read_bytes() == AUTH["A2"], "never touched"
    cx.tick(box.db, T0 + cx.REPAIR_WINDOW + 5)
    assert (cx.slot_dir(kb) / "auth.json").read_bytes() == AUTH["A2"] and (cx.slot_dir(kb) / "auth.json.prev").read_bytes() == AUTH["B"]


def test_tick_keeps_the_live_accounts_saved_copy_fresh_once_the_file_is_quiet(box):
    ka, _kb = box.two()
    box.log_in("A2", age=3)
    cx.tick(box.db)
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["A"], "not while Codex may still be writing it"
    box.log_in("A2", age=cx.SYNC_SETTLE + 1)
    cx.tick(box.db)
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["A2"]
    assert box.kv("codex_accounts")[ka]["last_seen"], "and the account is seen"


def test_tick_adopts_a_login_made_by_hand_when_nobody_was_current(box):
    box.log_in("A")
    cx.tick(box.db)
    assert len(box.kv("codex_accounts")) == 1 and box.kv("codex_account_current")["key"]


def test_a_logout_leaves_the_slots_and_the_current_record_alone(box):
    ka, kb = box.two()
    box.auth.unlink()
    cx.tick(box.db)
    assert cx.has_saved(ka) and box.kv("codex_account_current")["key"] == ka
    r = cx.switch(box.db, kb)
    assert r["from"] is None and r["to"] == kb and box.auth.read_bytes() == AUTH["B"], "no live login: nothing to save first"
    assert box.events()[-1] == (kb, {"from": ka, "to": kb})


def test_tick_never_raises_and_logs_class_names_only(box, monkeypatch, caplog):
    box.two()
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(cx, "_sync_live", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("SECRET-CODEX-A in the message")))
    cx.tick(box.db)
    assert "RuntimeError" in caplog.text and "SECRET" not in caplog.text


def test_the_tick_is_registered_next_to_the_other_account_ticks():
    assert cx.tick in samples.TICK_HOOKS and account_store.tick in samples.TICK_HOOKS


# ---------------------------------------------------------------- learning who an account is

def test_the_current_account_learns_its_id_user_and_plan_from_the_rollouts_of_sessions_run_while_it_was_live(box):
    ka, kb = box.two()
    cx.switch(box.db, kb, now=time.time() - 1000)                # B has been live for 1000 s
    write_rollout(box.live, "old", account=ACCT_A, created=time.time() - 5000, plan="pro")                   # before the switch: A's
    write_rollout(box.live, "new", account=ACCT_B, user="user-bbb", created=time.time() - 100, plan="plus")
    cx.tick(box.db)
    rec = box.kv("codex_accounts")[kb]
    assert (rec["account_id"], rec["user_id"], rec["plan"]) == (ACCT_B, "user-bbb", "plus")
    assert box.kv("codex_accounts")[ka]["account_id"] is None
    assert json.loads((cx.slot_dir(kb) / "meta.json").read_text()) | {} and json.loads((cx.slot_dir(kb) / "meta.json").read_text())["account_id"] == ACCT_B
    row = {r["key"]: r for r in cx.view(box.db)["list"]}[kb]
    assert (row["account_id"], row["plan"]) == (ACCT_B, "plus")


def test_a_session_that_does_not_say_who_created_it_teaches_nothing(box):
    ka = box.onboard("A")
    write_rollout(box.live, "x", account=None, created=time.time() + 5)
    cx.tick(box.db)
    assert box.kv("codex_accounts")[ka]["account_id"] is None


def test_several_accounts_among_the_new_sessions_is_ambiguous_and_nothing_is_learned(box, caplog):
    """Another Codex process may still hold the previous login and keep creating sessions with it."""
    caplog.set_level(logging.INFO)
    ka, kb = box.two()
    cx.switch(box.db, kb, now=time.time() - 1000)
    write_rollout(box.live, "mine", account=ACCT_B, created=time.time() - 100)
    write_rollout(box.live, "daemon", account=ACCT_A, created=time.time() - 50)
    cx.tick(box.db)
    assert box.kv("codex_accounts")[kb]["account_id"] is None and "more than one account" in caplog.text


def test_a_plan_that_is_not_a_plain_word_is_ignored_and_only_the_end_of_the_file_is_read(box):
    ka = box.onboard("A")
    p = write_rollout(box.live, "x", account=ACCT_A, created=time.time() + 5, plan="pro")
    p.write_text(p.read_text() + json.dumps({"rate_limits": {"plan_type": "<script>alert(1)</script>"}}) + "\n")
    cx.tick(box.db)
    assert box.kv("codex_accounts")[ka]["account_id"] == ACCT_A and box.kv("codex_accounts")[ka]["plan"] == "pro", "the odd value never matches"


def test_a_garbage_rollout_is_skipped(box):
    ka = box.onboard("A")
    d = box.live / "sessions" / "2026" / "10" / "04"
    d.mkdir(parents=True)
    (d / "rollout-bad.jsonl").write_text("not json\n")
    (d / "rollout-bad2.jsonl").write_text('{"type":"session_meta","payload":"x"}\n')
    (d / "rollout-bad3.jsonl").write_bytes(b"\xff\xfe\x00" * 10)
    write_rollout(box.live, "ok", account=ACCT_A, created=time.time() + 5)
    cx.tick(box.db)
    assert box.kv("codex_accounts")[ka]["account_id"] == ACCT_A


def test_the_rollouts_are_looked_at_once_a_minute_while_an_account_has_no_id(box):
    ka = box.onboard("A")
    cx.tick(box.db)
    write_rollout(box.live, "late", account=ACCT_A, created=time.time() + 5)
    cx.tick(box.db)
    assert box.kv("codex_accounts")[ka]["account_id"] is None, "the clock has not run out"
    cx._learn_at = 0.0
    cx.tick(box.db)
    assert box.kv("codex_accounts")[ka]["account_id"] == ACCT_A


def test_two_accounts_with_the_same_id_are_one_the_older_key_stays_with_the_newer_label(box):
    ka, kb = box.two("Work", "Work again")
    cx._put_record(box.db, ka, account_id=ACCT_A, plan="pro", added_at=iso(T0))
    cx._put_record(box.db, kb, added_at=iso(T0 + 100))
    cx.switch(box.db, kb)                                        # the newer one is live
    write_rollout(box.live, "x", account=ACCT_A, created=time.time() + 5)
    cx.tick(box.db)
    accts = box.kv("codex_accounts")
    assert list(accts) == [ka] and accts[ka]["label"] == "Work again" and accts[ka]["account_id"] == ACCT_A and accts[ka]["plan"] == "pro"
    assert not cx.slot_dir(kb).exists()
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["B"] == box.auth.read_bytes(), "the survivor holds the login that is live"
    assert box.kv("codex_account_current")["key"] == ka and cx.switch(box.db, ka)["already"] is True
    assert json.loads((cx.slot_dir(ka) / "meta.json").read_text())["label"] == "Work again"


def test_merging_keeps_the_live_survivors_file_and_takes_a_forgotten_ones_new_login(box):
    ka, kb = box.two("Work", "Work")
    cx._put_record(box.db, ka, account_id=ACCT_A, added_at=iso(T0))
    cx._put_record(box.db, kb, account_id=ACCT_A, added_at=iso(T0 + 100))
    cx._merge(box.db, ka, kb, T0)                                # A is live: its file is not replaced by B's
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["A"] and box.auth.read_bytes() == AUTH["A"] and list(box.kv("codex_accounts")) == [ka]
    kc = box.add("C", "Back again")                              # the account was forgotten, then added again under a new key
    cx.forget(box.db, ka) if False else None
    cx._put_record(box.db, kc, account_id=ACCT_A, added_at=iso(T0 + 200))
    cx._merge(box.db, kc, ka, T0)
    assert list(box.kv("codex_accounts")) == [ka]


def test_a_forgotten_record_that_is_logged_into_again_gets_its_slot_back(box):
    ka, kb = box.two("Work", "Home")
    cx._put_record(box.db, kb, account_id=ACCT_B, added_at=iso(T0))
    cx.forget(box.db, kb)
    kc = box.add("C", "Home 2")
    cx._put_record(box.db, kc, added_at=iso(T0 + 50))
    cx.switch(box.db, kc)
    write_rollout(box.live, "y", account=ACCT_B, created=time.time() + 5)
    cx.tick(box.db)
    accts = box.kv("codex_accounts")
    assert set(accts) == {ka, kb} and accts[kb]["saved"] is True and accts[kb]["label"] == "Home 2"
    assert (cx.slot_dir(kb) / "auth.json").read_bytes() == AUTH["C"] == box.auth.read_bytes() and box.kv("codex_account_current")["key"] == kb


# ---------------------------------------------------------------- #36: a hand login to another account is split off
ACCT_C = "acct-cccccccc-0000-4000-8000-000000000003"


def split_box(box, *, synced=True):
    """A (id ACCT_A, plan pro) live and current, B (id ACCT_B) saved; then somebody runs `codex login` by hand to another account: the live
    file becomes C's bytes and, with `synced`, the tick has taken them for A's refresh (A's slot holds C, A's own login is `.prev`)."""
    ka, kb = box.two()
    cx._put_record(box.db, ka, account_id=ACCT_A, plan="pro")
    cx._put_record(box.db, kb, account_id=ACCT_B, plan="plus")
    box.log_in("C", age=cx.SYNC_SETTLE + 5 if synced else 1)
    if synced:
        cx.tick(box.db)
        assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["C"] and (cx.slot_dir(ka) / "auth.json.prev").read_bytes() == AUTH["A"]
    cx._split_at = 0.0
    return ka, kb


def split_now(box):
    cx._split_at = 0.0
    cx.tick(box.db)


def test_split_same_id_does_nothing(box):
    ka, kb = split_box(box)
    write_rollout(box.live, "same", account=ACCT_A, created=time.time() + 5)
    split_now(box)
    assert set(box.kv("codex_accounts")) == {ka, kb} and cx.current(box.db) == ka and box.kv("codex_account_split") is None


def test_split_a_single_new_id_since_the_login_makes_a_new_current_account_and_gives_the_old_one_its_login_back(box):
    ka, kb = split_box(box)
    write_rollout(box.live, "hand", account=ACCT_C, user="user-ccc", created=time.time() + 5)
    split_now(box)
    accts = box.kv("codex_accounts")
    (kc,) = set(accts) - {ka, kb}
    assert accts[kc]["label"] == cx.SPLIT_LABEL and accts[kc]["account_id"] == ACCT_C and accts[kc]["user_id"] == "user-ccc"
    assert cx.current(box.db) == kc and box.auth.read_bytes() == AUTH["C"], "the live file is untouched"
    assert (cx.slot_dir(kc) / "auth.json").read_bytes() == AUTH["C"]
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["A"], "the old account has its own login again"
    assert (cx.slot_dir(ka) / "auth.json.prev").read_bytes() == AUTH["A"], ".prev is kept, nothing deleted"
    assert accts[ka]["account_id"] == ACCT_A and accts[ka]["label"] == "Work"
    sw = box.kv("codex_account_switch")
    assert (sw["from"], sw["to"], sw["repairs"]) == (ka, kc, 0), "the repair window protects the new current account"
    v = cx.view(box.db)
    assert v["notice"] == {"key": kc, "label": cx.SPLIT_LABEL, "text": "A different Codex login was detected and saved as a new account. Rename it.",
                           "at": box.kv("codex_account_split")["at"]}
    assert all(cx.has_saved(k) for k in (ka, kb, kc)), "no slot is left without a login"
    box.log_in("A")                                              # an older Codex process writes A's login back: repaired
    cx.tick(box.db)
    assert box.auth.read_bytes() == AUTH["C"]
    cx.set_label(box.db, kc, "Partner")
    assert cx.view(box.db)["notice"] is None, "the rename ends the notice"


def test_split_before_the_tick_took_the_new_bytes_leaves_the_old_slot_alone(box):
    ka, kb = split_box(box, synced=False)
    write_rollout(box.live, "hand", account=ACCT_C, created=time.time() + 5)
    split_now(box)
    kc = cx.current(box.db)
    assert kc not in (ka, kb) and (cx.slot_dir(kc) / "auth.json").read_bytes() == AUTH["C"]
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["A"] and not (cx.slot_dir(ka) / "auth.json.prev").exists()


def test_split_two_ids_is_ambiguous_and_does_nothing(box):
    ka, kb = split_box(box)
    write_rollout(box.live, "hand", account=ACCT_C, created=time.time() + 5)
    write_rollout(box.live, "daemon", account=ACCT_A, created=time.time() + 6)      # an older process still runs on A's login
    split_now(box)
    assert set(box.kv("codex_accounts")) == {ka, kb} and cx.current(box.db) == ka
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["C"], "nothing moved"


def test_split_no_rollout_yet_does_nothing(box):
    ka, kb = split_box(box)
    write_rollout(box.live, "before", account=ACCT_C, created=time.time() - 3600)  # older than the login change: says nothing
    split_now(box)
    assert set(box.kv("codex_accounts")) == {ka, kb} and cx.current(box.db) == ka and cx.view(box.db)["notice"] is None


def test_split_a_repeated_tick_after_a_split_does_nothing(box):
    ka, kb = split_box(box)
    write_rollout(box.live, "hand", account=ACCT_C, created=time.time() + 5)
    split_now(box)
    def snap():
        accts = box.kv("codex_accounts")
        slots = {k: (cx.slot_dir(k) / "auth.json").read_bytes() for k in accts}
        return ({k: (r["label"], r["account_id"]) for k, r in accts.items()}, slots, cx.current(box.db), box.kv("codex_account_split"),
                box.kv("codex_account_switch")["to"])
    after = snap()
    for _ in range(2):
        split_now(box)
    assert snap() == after


def test_split_to_a_known_accounts_id_is_a_hand_switch_to_it_not_a_new_account(box):
    ka, kb = split_box(box)
    write_rollout(box.live, "hand", account=ACCT_B, created=time.time() + 5)
    split_now(box)
    assert set(box.kv("codex_accounts")) == {ka, kb} and cx.current(box.db) == kb
    assert (cx.slot_dir(kb) / "auth.json").read_bytes() == AUTH["C"] and (cx.slot_dir(kb) / "auth.json.prev").read_bytes() == AUTH["B"]
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["A"] and box.kv("codex_account_split") is None
    assert box.kv("codex_accounts")[kb]["label"] == "Home", "the known account keeps its name"


def test_split_never_happens_without_the_old_login_to_give_back(box):
    ka, kb = split_box(box)
    (cx.slot_dir(ka) / "auth.json.prev").unlink()                 # nothing to restore the old account from
    write_rollout(box.live, "hand", account=ACCT_C, created=time.time() + 5)
    split_now(box)
    assert set(box.kv("codex_accounts")) == {ka, kb} and cx.has_saved(ka)


def test_split_notice_can_be_dismissed(box):
    ka, kb = split_box(box)
    write_rollout(box.live, "hand", account=ACCT_C, created=time.time() + 5)
    split_now(box)
    assert cx.view(box.db)["notice"] and cx.dismiss_notice(box.db) is True
    assert cx.view(box.db)["notice"] is None and cx.dismiss_notice(box.db) is False


# ---------------------------------------------------------------- adding a login: codex login --device-auth in .pending

def test_start_login_runs_the_device_login_in_pending_under_the_data_dir_with_a_copy_of_config_and_nothing_else(box):
    (box.live / "config.toml").write_text('model = "gpt-5.6-sol"\n')
    (box.live / "hooks.json").write_text('{"hooks": {}}')
    put_auth(box.live, AUTH["A"])
    cx.start_login(box.db, "  My  work\x00 account ")
    pend = cx.pending_dir()
    # under the data dir (Codex refuses helper binaries under a temp dir, so the pending dir is never /tmp in production; the test's own data dir
    # may well live under /tmp on CI, so that is not asserted by path prefix)
    assert pend == box.store / ".pending" and mode(pend) == 0o700 and mode(box.store) == 0o700
    assert sorted(p.name for p in pend.iterdir()) == ["config.toml"], "the live login and the hooks are not copied"
    assert (pend / "config.toml").read_text() == 'model = "gpt-5.6-sol"\n' and mode(pend / "config.toml") == 0o600
    name, cwd, env = box.tmux["created"][-1]
    assert name == tmux.LOGIN_SESSION and env["CODEX_HOME"] == str(pend) and env["BROWSER"] == browser_stub()
    assert box.tmux["sent"][-1] == (tmux.LOGIN_SESSION, "codex login --device-auth") == (tmux.LOGIN_SESSION, codex_agent.DEVICE_LOGIN_CMD)
    v = cx.login_view()
    assert v["adding"] is True and v["label"] == "My work account" and v["running"] is True and v["started_at"]
    assert box.auth.read_bytes() == AUTH["A"], "the live login is untouched"
    cx.cancel_login()


def test_start_login_without_a_live_config_still_starts(box):
    cx.start_login(box.db, "x")
    assert list(cx.pending_dir().iterdir()) == []
    cx.cancel_login()


@pytest.mark.parametrize("bad", [None, "", "   ", "\x00\x01\n", 5])
def test_a_label_is_required_and_checked_before_anything_starts(box, bad):
    with pytest.raises(ValueError):
        cx.start_login(box.db, bad)
    assert not box.tmux["created"] and not cx.pending_dir().exists()


def test_a_label_is_cut_at_sixty_characters(box):
    cx.start_login(box.db, "é" * 90)
    assert cx.login_view()["label"] == "é" * 60
    cx.cancel_login()


def test_adding_needs_a_codex_with_device_auth(box, monkeypatch):
    write_fake_codex(box.fake, device_auth=False)
    os.utime(box.fake, (2, 2))
    codex_agent.reset_caches()
    with pytest.raises(cx.Unsupported, match=r"update Codex to 0\.157 or newer: npm install -g --prefix ~/\.local @openai/codex@latest"):
        cx.start_login(box.db, "x")
    assert not box.tmux["created"] and not cx.pending_dir().exists()
    assert cx.view(box.db)["store"] == {"supported": True, "add": False, "reason": cx.REASON_UPDATE, "count": 0}


def test_one_login_at_a_time_unless_restarted_and_a_claude_login_counts(box):
    cx.start_login(box.db, "one")
    with pytest.raises(cx.Busy, match="already running"):
        cx.start_login(box.db, "two")
    cx.start_login(box.db, "two", restart=True)
    assert cx.login_view()["label"] == "two" and [c for c in box.tmux["created"] if c[0] == tmux.LOGIN_SESSION].__len__() == 2
    cx.cancel_login()
    box.tmux["sessions"][tmux.LOGIN_SESSION] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%9", "command": "claude", "path": "/", "pid": 9, "env": {}}
    with pytest.raises(cx.Busy):
        cx.start_login(box.db, "three")                          # the Claude add-account login is running in the same session


def test_a_login_that_cannot_start_leaves_no_pending_dir(box, monkeypatch):
    monkeypatch.setattr(tmux, "new_session", lambda *a, **k: (_ for _ in ()).throw(tmux.TmuxError("no tmux")))
    with pytest.raises(tmux.TmuxError):
        cx.start_login(box.db, "x")
    assert not cx.pending_dir().exists() and cx.login_view()["adding"] is False


def test_parse_login_text_reads_the_link_and_the_code_after_it():
    p = cx.parse_login_text(DEVICE_PANE)
    assert p == {"url": "https://auth.openai.com/codex/device", "code": "ABCD-12345", "line": "ABCD-12345"}
    assert cx.parse_login_text("") == {"url": None, "code": None, "line": None} and cx.parse_login_text(None)["url"] is None
    only_url = cx.parse_login_text("1. Open https://auth.openai.com/codex/device\n")
    assert only_url["url"] and only_url["code"] is None, "the code has not been printed yet"
    again = DEVICE_PANE + "\nTry again:\n https://auth.openai.com/codex/device?x=1\n   WXYZ-98765\n"
    p = cx.parse_login_text(again)
    assert p["url"].endswith("?x=1") and p["code"] == "WXYZ-98765", "the newest attempt wins"
    assert cx.parse_login_text("see https://example.com/docs then\n   ABCD-12345\n")["url"] is None, "a link that names neither device nor auth"
    assert cx.parse_login_text("https://auth.openai.com/codex/device.\n  Code: ABCD1234\n")["code"] == "ABCD1234"
    assert cx.parse_login_text("ABCD-12345 before\nhttps://auth.openai.com/codex/device\n")["code"] is None, "only tokens after the link's line"
    assert cx.parse_login_text("https://auth.openai.com/codex/device\nWelcome Codex 0.160.0 enter ab-12 and 1234\n")["code"] is None


def test_parse_login_text_reads_the_real_codex_0161_device_auth_pane():
    """OBSERVED on the box (codex-cli 0.161.0, `codex login --device-auth` in a scratch home, cancelled; the one-time code replaced by
    ABCD-12345): the capture starts at Codex's /tmp warning line, whose wrapped path must not be taken for a link or a code."""
    text = (Path(__file__).parent / "fixtures" / "codex_login_device_auth_pane_0161.txt").read_text()
    assert "v0.161.0" in text and "tmp/ccb-check-codexhome" in text
    p = cx.parse_login_text(text)
    assert p["url"] == "https://auth.openai.com/codex/device" and p["code"] == "ABCD-12345" and p["line"] == "ABCD-12345"
    assert cx.CODE_RE.match(p["code"]) and len(p["code"].split("-")[0]) == 4 and len(p["code"].split("-")[1]) == 5
    p2 = cx.parse_login_text(text + "\n› Use /skills to list available skills\n")           # a noisy prompt line after it changes nothing
    assert p2 == p


def test_login_view_shows_the_link_the_code_and_the_tail_only_while_adding(box):
    box.tmux["screen"] = DEVICE_PANE
    assert cx.login_view() == {"running": False, "adding": False, "label": None, "replace_key": None, "started_at": None, "url": None, "code": None,
                               "tail": [], "result": None}
    cx.start_login(box.db, "Work")
    v = cx.login_view()
    assert (v["url"], v["code"], v["running"], v["adding"]) == ("https://auth.openai.com/codex/device", "ABCD-12345", True, True)
    assert v["tail"][-1].startswith("Device codes are a common phishing target") and len(v["tail"]) <= 15
    cx.cancel_login()


def test_finalize_saves_the_new_account_without_touching_the_live_login(box):
    ka = box.onboard("A")
    cx.start_login(box.db, "Home")
    box.pending("B")
    res = cx.finalize(box.db)
    assert res["ok"] is True and res["label"] == "Home" and res["live"] is False and res["key"] not in (ka, None)
    key = res["key"]
    assert (cx.slot_dir(key) / "auth.json").read_bytes() == AUTH["B"] and mode(cx.slot_dir(key) / "auth.json") == 0o600
    assert json.loads((cx.slot_dir(key) / "meta.json").read_text())["label"] == "Home"
    assert box.auth.read_bytes() == AUTH["A"] and box.kv("codex_account_current")["key"] == ka
    assert box.kv("codex_accounts")[key]["saved"] is True
    assert not cx.pending_dir().exists() and tmux.LOGIN_SESSION not in box.tmux["sessions"]
    v = cx.login_view()
    assert v["adding"] is False and v["result"]["ok"] is True and v["result"]["key"] == key and v["result"]["label"] == "Home" and v["result"]["live"] is False
    assert [r["key"] for r in cx.view(box.db)["list"]] == [ka, key]


def test_finalize_puts_the_login_in_place_when_nobody_is_logged_in(box):
    cx.start_login(box.db, "First")
    box.pending("B")
    res = cx.finalize(box.db)
    assert res["ok"] and res["live"] is True and box.auth.read_bytes() == AUTH["B"] and mode(box.auth) == 0o600
    assert box.kv("codex_account_current")["key"] == res["key"] and box.events() == [(res["key"], {"to": res["key"]})]


def test_finalize_keeps_a_live_login_nobody_saved_before_putting_the_first_account_in_place(box):
    box.log_in("A")                                              # a stray login, never seen by the board
    cx.start_login(box.db, "First")
    box.pending("B")
    res = cx.finalize(box.db)
    assert res["ok"] and res["live"] is True and box.auth.read_bytes() == AUTH["B"]
    saved = {r["label"]: r for r in cx.view(box.db)["list"]}
    stray = [r for lbl, r in saved.items() if lbl.startswith("codex login ")]
    assert len(stray) == 1 and (cx.slot_dir(stray[0]["key"]) / "auth.json").read_bytes() == AUTH["A"], "the old login was kept, one tap away"


def test_finalize_leaves_an_unmatched_live_login_alone_when_there_are_accounts(box):
    ka = box.onboard("A")
    box.log_in("A2")                                             # A's refreshed token: the tick will save it
    cx.start_login(box.db, "Home")
    box.pending("B")
    res = cx.finalize(box.db)
    assert res["ok"] and res["live"] is False and box.auth.read_bytes() == AUTH["A2"] and box.kv("codex_account_current")["key"] == ka
    assert len(box.kv("codex_accounts")) == 2


def test_finalize_waits_for_a_file_that_is_old_enough_and_not_empty(box):
    cx.start_login(box.db, "x")
    p = box.pending("B", age=0.1)
    assert cx.finalize(box.db) is None and cx.pending_dir().exists() and cx.login_view()["adding"] is True
    p.write_bytes(b"")
    old = time.time() - 50
    os.utime(p, (old, old))
    assert cx.finalize(box.db) is None, "an empty file is not a login"
    put_auth(cx.pending_dir(), AUTH["B"], 5)
    assert cx.finalize(box.db)["ok"] is True


def test_a_login_session_that_ended_without_a_file_reports_the_error(box):
    cx.start_login(box.db, "x")
    box.tmux["sessions"][tmux.LOGIN_SESSION]["command"] = "zsh"            # back at the shell
    assert cx.finalize(box.db, T0) is None and cx.pending_dir().exists()
    assert cx.finalize(box.db, T0 + cx.DEAD_AFTER - 1) is None
    res = cx.finalize(box.db, T0 + cx.DEAD_AFTER + 1)
    assert res["ok"] is False and res["error"] == "the login did not complete"
    assert not cx.pending_dir().exists() and tmux.LOGIN_SESSION not in box.tmux["sessions"]
    assert cx.login_view()["adding"] is False and cx.login_view()["result"]["ok"] is False


def test_a_session_that_comes_back_to_life_resets_the_dead_clock(box):
    cx.start_login(box.db, "x")
    box.tmux["sessions"][tmux.LOGIN_SESSION]["command"] = "zsh"
    cx.finalize(box.db, T0)
    box.tmux["sessions"][tmux.LOGIN_SESSION]["command"] = "codex"
    assert cx.finalize(box.db, T0 + 15) is None
    box.tmux["sessions"][tmux.LOGIN_SESSION]["command"] = "zsh"
    assert cx.finalize(box.db, T0 + 30) is None and cx.finalize(box.db, T0 + 45) is None, "the clock restarted at T0+30"
    assert cx.finalize(box.db, T0 + 30 + cx.DEAD_AFTER + 1)["ok"] is False


def test_a_pending_login_older_than_an_hour_is_abandoned(box):
    cx.start_login(box.db, "x")
    res = cx.finalize(box.db, time.time() + cx.ABANDON_AFTER + 5)
    assert res["ok"] is False and not cx.pending_dir().exists()


def test_a_pending_dir_left_by_a_restart_is_tidied_up_silently(box):
    cx.pending_dir().mkdir(parents=True)
    old = time.time() - 600
    os.utime(cx.pending_dir(), (old, old))
    assert cx.login_view()["adding"] is False
    assert cx.finalize(box.db) is None and not cx.pending_dir().exists() and cx.login_view()["result"] is None


def test_a_login_that_finished_while_the_board_restarted_is_saved_with_a_dated_label(box):
    box.pending("B")                                             # the login completed after the restart: nobody remembers its label
    res = cx.finalize(box.db)
    assert res["ok"] and res["label"].startswith("codex login ") and cx.has_saved(res["key"])


def test_the_result_reads_none_after_ten_minutes(box, monkeypatch):
    cx.start_login(box.db, "x")
    box.pending("B")
    cx.finalize(box.db)
    assert cx.login_view()["result"]["ok"] is True
    monkeypatch.setattr(cx, "_wall", lambda: time.time() + cx.RESULT_TTL + 1)
    assert cx.login_view()["result"] is None


def test_cancel_kills_the_session_removes_pending_and_clears_everything(box):
    cx.start_login(box.db, "x")
    cx.cancel_login()
    assert not cx.pending_dir().exists() and tmux.LOGIN_SESSION not in box.tmux["sessions"]
    assert cx.login_view()["adding"] is False and cx.login_view()["result"] is None


def test_cancel_leaves_a_login_session_it_did_not_start_alone(box):
    box.tmux["sessions"][tmux.LOGIN_SESSION] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%9", "command": "claude", "path": "/", "pid": 9, "env": {}}
    cx.cancel_login()
    assert tmux.LOGIN_SESSION in box.tmux["sessions"], "the Claude add-account login is not ours"


def test_the_watcher_finishes_the_login_without_waiting_for_the_tick(box, monkeypatch):
    monkeypatch.setattr(cx, "watch_login", REAL_WATCH)
    monkeypatch.setattr(cx, "_WATCH_SLEEP", lambda s: time.sleep(0.02))
    cx.start_login(box.db, "Home")
    deadline = time.time() + 5
    time.sleep(0.1)
    box.pending("B")                                             # the person typed the code on the page: codex wrote the file
    while time.time() < deadline and not cx.login_view()["result"]:
        time.sleep(0.02)
    res = cx.login_view()["result"]
    assert res and res["ok"] is True and cx.has_saved(res["key"]) and res["label"] == "Home"
    cx.stop_watchers()


def test_the_watcher_looks_at_the_pane_only_now_and_then_and_stops_on_cancel(box, monkeypatch):
    monkeypatch.setattr(cx, "watch_login", REAL_WATCH)
    panes = []
    real = cx._pane
    monkeypatch.setattr(cx, "_pane", lambda: panes.append(1) or real())
    monkeypatch.setattr(cx, "_WATCH_SLEEP", lambda s: time.sleep(0.01))
    cx.start_login(box.db, "x")
    time.sleep(0.3)
    n = len(panes)
    assert 0 < n <= 1 + 0.3 / 0.01 / cx.WATCH_DEAD_CHECK + 5, "a process call every fifth look, not every look"
    cx.cancel_login()
    cx.stop_watchers()
    assert all(not t.is_alive() for t in cx._watchers) and cx._watchers == []


def test_a_second_login_replaces_the_first_ones_watcher(box, monkeypatch):
    monkeypatch.setattr(cx, "watch_login", REAL_WATCH)
    monkeypatch.setattr(cx, "_WATCH_SLEEP", lambda s: time.sleep(0.01))
    cx.start_login(box.db, "one")
    first = cx._watchers[-1]
    cx.start_login(box.db, "two", restart=True)
    first.join(2)
    assert not first.is_alive() and cx._watchers[-1].is_alive()
    cx.cancel_login()


# ---------------------------------------------------------------- the view

def test_view_shape_order_and_the_store_summary(box):
    ka, kb = box.two("Work", "Home")
    kc = box.add("C", "Spare")
    cx._put_record(box.db, ka, plan="pro", account_id=ACCT_A)
    cx.forget(box.db, kc)
    v = cx.view(box.db)
    assert set(v) == {"current", "list", "store", "login", "notice"} and v["current"] == ka and v["notice"] is None
    assert [r["key"] for r in v["list"]][0] == ka and {r["key"] for r in v["list"]} == {ka, kb, kc}
    row = v["list"][0]
    assert set(row) == {"key", "label", "account_id", "plan", "saved", "saved_at", "current", "added_at", "last_seen"}
    assert (row["label"], row["plan"], row["account_id"], row["saved"], row["current"]) == ("Work", "pro", ACCT_A, True, True)
    assert {r["key"]: r["saved"] for r in v["list"]} == {ka: True, kb: True, kc: False}
    assert v["store"] == {"supported": True, "add": True, "reason": None, "count": 2}
    assert set(v["login"]) == {"running", "adding", "label", "replace_key", "started_at", "url", "code", "tail", "result"}
    assert "tail" not in cx.view(box.db, tail=False)["login"]


def test_store_view_says_when_codex_is_missing_or_too_old_and_knows_nothing_until_probed(box, monkeypatch):
    monkeypatch.setattr(settings, "codex_bin", lambda: None)
    assert cx.store_view(0) == {"supported": False, "add": False, "reason": "codex is not installed", "count": 0}
    monkeypatch.setattr(settings, "codex_bin", lambda: str(box.fake))
    codex_agent.reset_caches()
    first = cx.store_view(0)                                     # not probed yet: counts as available, the probe starts in the background
    assert first["supported"] is True and first["add"] is True
    deadline = time.time() + 5
    while time.time() < deadline and codex_agent.CodexAgent().device_auth(fetch=False) is None:
        time.sleep(0.02)
    assert cx.store_view(3) == {"supported": True, "add": True, "reason": None, "count": 3}


def test_view_never_raises(box, monkeypatch):
    box.two()
    monkeypatch.setattr(cx, "_row", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(cx, "login_view", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    v = cx.view(box.db)
    assert v["list"] == [] and v["login"]["adding"] is False


# ---------------------------------------------------------------- secrecy

def test_the_login_bytes_never_appear_in_any_answer_log_event_or_kv_value(box, lite_client, caplog):
    from app import main
    box.db = main.db
    caplog.set_level(logging.DEBUG)
    ka = box.onboard("A")
    cx.set_label(box.db, ka, "Work")
    r = lite_client.post("/api/codex-accounts/login", headers=H, json={"label": "Home"})
    assert r.status_code == 202
    box.tmux["screen"] = DEVICE_PANE
    box.pending("B")
    cx.finalize(box.db)
    key = cx.view(box.db)["list"][1]["key"]
    seen = [lite_client.get("/api/codex-accounts", headers=H).text, lite_client.get("/api/state", headers=H).text]
    r = lite_client.post(f"/api/codex-accounts/{key}/switch", headers=H)
    assert r.status_code == 200 and r.json()["to"] == key
    seen += [r.text, lite_client.post(f"/api/codex-accounts/{ka}/switch", headers=H).text,
             lite_client.delete(f"/api/codex-accounts/{key}/saved", headers=H).text, lite_client.delete(f"/api/codex-accounts/{key}/saved", headers=H).text,
             lite_client.post(f"/api/codex-accounts/{key}/switch", headers=H).text, lite_client.patch(f"/api/codex-accounts/{ka}", headers=H, json={"label": "Z"}).text]
    box.log_in("A2")
    cx.tick(box.db)
    write_rollout(box.live, "r", account=ACCT_A, created=time.time() + 5)
    cx._learn_at = 0.0
    cx.tick(box.db, time.time() + 2000)
    dump = []
    for (name,) in box.db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        dump += [repr(tuple(row)) for row in box.db.conn.execute(f'SELECT * FROM "{name}"').fetchall()]
    blob = "\n".join(seen + dump + [caplog.text, json.dumps(cx.view(box.db))] + [(cx.slot_dir(k) / "meta.json").read_text() for k in cx._slot_keys()])
    assert "SECRET" not in blob, [ln for ln in blob.splitlines() if "SECRET" in ln][:3]
    assert "switched Codex account" in caplog.text, "the audit line is written, with names only"
    assert b"SECRET-CODEX-A" in (cx.slot_dir(ka) / "auth.json").read_bytes(), "the one place the secret is, is the slot"


def _functions(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def test_no_json_load_is_ever_applied_to_a_login_path():
    tree = ast.parse(inspect.getsource(cx))
    forbidden_names = {"AUTH", "PREV", "_live_auth", "_slot_auth", "_read_stable", "_store_auth", "_live_bytes", "_slot_bytes", "_matching_slot"}
    checked = 0
    for fn in _functions(tree):
        body = fn.body[1:] if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(getattr(fn.body[0], "value", None), ast.Constant) else fn.body
        nodes = [n for stmt in body for n in ast.walk(stmt)]
        uses_json_load = any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "json" and n.attr in ("load", "loads")
                             for n in nodes)
        if not uses_json_load:
            continue
        checked += 1
        names = {n.id for n in nodes if isinstance(n, ast.Name)} | {n.attr for n in nodes if isinstance(n, ast.Attribute)}
        strings = [n.value for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        assert not (names & forbidden_names), f"{fn.name} parses JSON and touches a login path"
        assert not any("auth" in s.lower() for s in strings), f"{fn.name} parses JSON next to a login-file string"
    assert checked >= 1, "the check found nothing to check: the rollout readers moved"      # _rollout_meta now delegates to codex_rollout.session_meta (issue #32 f)


def test_the_module_never_runs_a_process_and_never_names_the_codex_binary():
    src = inspect.getsource(cx)
    tree = ast.parse(src)
    imports = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imports |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not ({"subprocess", "pty", "shlex"} & imports) and "codex_bin" not in src and "os.system" not in src and "Popen" not in src


def test_the_bytes_helpers_mention_no_json():
    for name in ("_store_auth", "_live_bytes", "_slot_bytes", "_matching_slot", "_confirm_current", "_sync_live", "_repair"):
        assert "json" not in inspect.getsource(getattr(cx, name)).lower(), name


def test_every_file_the_store_writes_is_private(box):
    ka, kb = box.two()
    cx.start_login(box.db, "Third")
    box.pending("C")
    res = cx.finalize(box.db)
    cx.switch(box.db, kb)
    cx.switch(box.db, res["key"])
    cx.set_label(box.db, ka, "Renamed")
    for p in box.store.rglob("*"):
        assert mode(p) == (0o700 if p.is_dir() else 0o600), p
    assert mode(box.auth) == 0o600


# ---------------------------------------------------------------- v0.5.17g: logging a saved account in again

def relogin_cx(box, key, who="C", **kw):
    """What `Log in again` does: the device login for the account, finished with the given login file."""
    cx.start_login(box.db, None, replace_key=key, **kw)
    box.pending(who)
    return cx.finalize(box.db)


def test_logging_a_saved_account_in_again_replaces_its_login_and_makes_no_second_account(box):
    ka, kb = box.two()                                          # A live and current, B saved ("Home")
    accounts_before = set(cx._load(box.db))
    cx.start_login(box.db, None, replace_key=kb)
    v = cx.login_view()
    assert v["adding"] is True and v["label"] == "Home" and v["replace_key"] == kb, "the page can say who is being logged in again"
    box.pending("C")
    res = cx.finalize(box.db)
    assert res == {"ok": True, "key": kb, "label": "Home", "live": False, "replaced": True, "why": None, "at": res["at"]}
    assert set(cx._load(box.db)) == accounts_before and cx.label_of(box.db, kb) == "Home", "same key, same label, no second account"
    s = cx.slot_dir(kb)
    assert (s / "auth.json").read_bytes() == AUTH["C"] and (s / "auth.json.prev").read_bytes() == AUTH["B"], "the old login becomes .prev"
    assert box.auth.read_bytes() == AUTH["A"] and cx.current(box.db) == ka, "the live login and the current account do not move"
    assert mode(s / "auth.json") == 0o600 and mode(s / "auth.json.prev") == 0o600 and tree_has_no_temp_files(box.store)
    meta = json.loads((s / "meta.json").read_text())
    assert meta["label"] == "Home" and meta["saved_at"]
    row = {r["key"]: r for r in cx.view(box.db)["list"]}[kb]
    assert row["saved_at"] == meta["saved_at"] and row["saved"] is True
    assert not cx.pending_dir().exists() and cx.login_view()["replace_key"] is None and cx.login_view()["result"] == res
    assert cx.finalize(box.db) is None, "decided once"


def test_logging_the_live_account_in_again_puts_the_fresh_login_live_at_once(box):
    ka, kb = box.two()
    put_auth(box.live, AUTH["A2"])                              # the live file moved on (a refresh the board has not saved): the dead one
    events_before = box.events()
    res = relogin_cx(box, ka)
    assert res["ok"] and res["live"] is True and res["replaced"] is True and res["why"] is None and res["key"] == ka
    assert box.auth.read_bytes() == AUTH["C"], "the live file is the fresh login"
    s = cx.slot_dir(ka)
    assert (s / "auth.json").read_bytes() == AUTH["C"], "the dead live bytes did not overwrite the fresh slot"
    assert (s / "auth.json.prev").read_bytes() == AUTH["A2"], "the generation before it is the one that was live"
    assert cx.current(box.db) == ka and box.events() == events_before + [(ka, {"to": ka, "relogin": True})], "an event says the login was renewed; the account in use did not change"
    sw = box.kv("codex_account_switch")                          # #35: a switch-like record (from == to) opens the repair window
    assert (sw["from"], sw["to"], sw["repairs"], sw["relogin"]) == (ka, ka, 0, True)
    cx.tick(box.db, T0 + 500)                                   # the tick keeps the live account's copy current: it must not undo the fresh login
    assert box.auth.read_bytes() == AUTH["C"] and (s / "auth.json").read_bytes() == AUTH["C"]
    assert mode(box.auth) == 0o600


def test_a_stale_writer_of_the_dead_login_is_repaired_inside_the_window_after_logging_the_live_account_in_again(box):
    """#35: an older Codex process that still holds the dead login writes it back after "Log in again": inside REPAIR_WINDOW the fresh login
    is put back (at most MAX_REPAIRS times); bytes that are neither the dead login nor a slot's are left alone; after the window nothing is
    repaired, and the dead bytes are still never saved over the fresh slot."""
    ka, kb = box.two()
    put_auth(box.live, AUTH["A2"])                              # the dead login is live
    relogin_cx(box, ka)
    t = time.time()
    for n in range(1, cx.MAX_REPAIRS + 1):
        box.log_in("A2")                                         # the older process writes the dead login back
        cx.tick(box.db, t + 30)
        assert box.auth.read_bytes() == AUTH["C"] and box.kv("codex_account_switch")["repairs"] == n
    box.log_in("A2")
    cx.tick(box.db, t + 30)
    assert box.auth.read_bytes() == AUTH["A2"], "the sixth is left alone"
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["C"], "and the fresh slot was never overwritten"
    assert cx.current(box.db) == ka and len(cx._load(box.db)) == 2


def test_after_a_relogin_unknown_bytes_and_late_writes_are_not_repaired(box):
    ka, kb = box.two()
    put_auth(box.live, AUTH["A2"])
    relogin_cx(box, ka)
    t = time.time()
    box.log_in("B2", age=cx.SYNC_SETTLE + 60)                    # matches neither the dead login nor a slot: nothing says whose it is
    cx.tick(box.db, t + 30)
    assert box.auth.read_bytes() == AUTH["B2"] and box.kv("codex_account_switch")["repairs"] == 0
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["C"], "not saved inside the window"
    box.log_in("A2")
    cx.tick(box.db, t + cx.REPAIR_WINDOW + 30)
    assert box.auth.read_bytes() == AUTH["A2"] and box.kv("codex_account_switch")["repairs"] == 0, "no repair after the window"
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["C"], "the dead login (.prev) is still never saved over the fresh one"


def test_the_fresh_login_of_the_live_account_stays_saved_while_a_board_codex_session_is_open(box):
    ka, kb = box.two()
    put_auth(box.live, AUTH["A2"])
    add_codex_row(box.db)
    alive(box, "shop--api--s1")
    res = relogin_cx(box, ka)
    assert res["ok"] and res["live"] is False and res["replaced"] is True and res["why"] == cx.BUSY_SESSIONS
    assert box.auth.read_bytes() == AUTH["A2"], "a running Codex keeps its login and would write the dead one back: the live file is left alone"
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["C"] and (cx.slot_dir(ka) / "auth.json.prev").read_bytes() == AUTH["A2"]
    assert cx.login_view()["result"] == res and box.kv("codex_relogin_waiting")["key"] == ka
    for t in (T0 + 100, T0 + 200):                               # the tick keeps the live account's copy current, but the live file is not that account's refresh
        cx.tick(box.db, t)
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["C"] and box.auth.read_bytes() == AUTH["A2"], "the dead login never overwrites the fresh one, and waits for the sessions"
    box.tmux["sessions"].clear()                                # closed: the next tick puts the fresh login in
    cx.tick(box.db, T0 + 300)
    assert box.auth.read_bytes() == AUTH["C"] and (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["C"] and cx.current(box.db) == ka
    cx.tick(box.db, T0 + 400)
    assert box.kv("codex_relogin_waiting") is None, "live now: nothing waits"


def test_a_waiting_fresh_login_is_not_saved_over_by_a_switch_away_and_comes_back_with_a_switch_to_it(box):
    ka, kb = box.two()
    put_auth(box.live, AUTH["A2"])
    add_codex_row(box.db)
    alive(box, "shop--api--s1")
    relogin_cx(box, ka)
    box.tmux["sessions"].clear()
    assert cx.switch(box.db, kb)["to"] == kb
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["C"], "A's slot kept the fresh login: the dead live bytes were not saved over it"
    assert box.auth.read_bytes() == AUTH["B"]
    assert cx.switch(box.db, ka)["to"] == ka and box.auth.read_bytes() == AUTH["C"]


def test_a_waiting_fresh_login_goes_live_with_a_switch_to_the_current_account_too(box):
    ka, kb = box.two()
    put_auth(box.live, AUTH["A2"])
    add_codex_row(box.db)
    alive(box, "shop--api--s1")
    relogin_cx(box, ka)
    box.tmux["sessions"].clear()
    r = cx.switch(box.db, ka)
    assert r["ok"] and r["already"] is False and r["to"] == ka and box.auth.read_bytes() == AUTH["C"] and box.kv("codex_relogin_waiting") is None


def test_a_waiting_fresh_login_is_given_up_when_the_account_is_no_longer_current_or_another_login_is_live(box):
    ka, kb = box.two()
    put_auth(box.live, AUTH["A2"])
    add_codex_row(box.db)
    alive(box, "shop--api--s1")
    relogin_cx(box, ka)
    box.tmux["sessions"].clear()
    put_auth(box.live, AUTH["C"][:-3] + b"xx}")                  # somebody logged in by hand: not the dead login, not ours to replace
    cx.tick(box.db, T0 + 100)
    assert box.kv("codex_relogin_waiting") is None and box.auth.read_bytes() != AUTH["C"]


def test_a_relogin_with_nobody_logged_in_live_goes_live_like_a_new_account(box):
    ka, kb = box.two()
    box.auth.unlink()
    res = relogin_cx(box, kb)
    assert res["live"] is True and box.auth.read_bytes() == AUTH["C"] and cx.current(box.db) == kb


def test_a_forgotten_account_can_be_logged_in_again_into_the_same_key(box):
    ka, kb = box.two()
    kc = box.add("C", "Spare")
    cx.forget(box.db, kc)
    assert not cx.has_saved(kc)
    accounts_before = set(cx._load(box.db))
    res = relogin_cx(box, kc, who="B2")
    assert res["ok"] and res["key"] == kc and res["live"] is False and cx.has_saved(kc)
    assert set(cx._load(box.db)) == accounts_before and cx._load(box.db)[kc]["saved"] is True and cx.label_of(box.db, kc) == "Spare"
    assert {r["key"]: r for r in cx.view(box.db)["list"]}[kc]["saved"] is True and box.auth.read_bytes() == AUTH["A"]


def test_an_unknown_key_starts_nothing_and_a_given_label_does_not_rename(box):
    box.two()
    for bad in ("0" * 24, "../escape", "", "A" * 24):
        with pytest.raises(cx.UnknownAccount):
            cx.start_login(box.db, None, replace_key=bad)
    assert not cx.pending_dir().exists() and [c for c in box.tmux["created"] if c[0] == tmux.LOGIN_SESSION] == []
    ka, kb = [r["key"] for r in cx.view(box.db)["list"]]
    cx.start_login(box.db, "Other name", replace_key=kb)
    assert cx.login_view()["label"] == "Home" and cx.label_of(box.db, kb) == "Home"


def test_an_account_that_vanished_while_the_login_ran_gets_the_login_as_a_new_account(box):
    ka, kb = box.two()
    cx.start_login(box.db, None, replace_key=kb)
    accts = cx._load(box.db)                                    # merged away meanwhile
    accts.pop(kb)
    cx._save(box.db, accts)
    box.pending("C")
    res = cx.finalize(box.db)
    assert res["ok"] and res["key"] != kb and res["label"] == "Home" and "replaced" not in res and cx.has_saved(res["key"])


def test_a_finished_login_clears_the_codex_login_problem_of_that_account_only(box):
    ka, kb = box.two()
    login_problem.raise_(box.db, agent="codex", account=ka, session=None, message="x")
    relogin_cx(box, kb)
    assert login_problem.get(box.db)["account"] == ka
    relogin_cx(box, ka, who="B2")
    assert login_problem.get(box.db) is None


# ---------------------------------------------------------------- the API

@pytest.fixture
def api(box, lite_client):
    from app import main
    box.db = main.db
    return lite_client


def post(api, path, **kw):
    return api.post(path, headers=H, **kw)


def test_get_codex_accounts_carries_rows_store_and_the_login_view(box, api):
    ka = box.onboard("A")
    body = api.get("/api/codex-accounts", headers=H).json()
    assert body["current"] == ka and body["list"][0]["saved"] is True and body["list"][0]["label"].startswith("codex login ")
    assert body["store"] == {"supported": True, "add": True, "reason": None, "count": 1}
    assert body["login"] == {"running": False, "adding": False, "label": None, "replace_key": None, "started_at": None, "url": None, "code": None,
                             "tail": [], "result": None}
    assert api.get("/api/codex-accounts").status_code == 403


def test_state_carries_one_new_top_level_key_the_get_body_without_the_tail(box, api):
    ka = box.onboard("A")
    st = api.get("/api/state", headers=H).json()
    assert st["codex_accounts"]["current"] == ka and st["codex_accounts"]["store"]["count"] == 1
    assert "tail" not in st["codex_accounts"]["login"] and set(st["codex_accounts"]["login"]) == {"running", "adding", "label", "replace_key", "started_at", "url", "code", "result"}
    get = api.get("/api/codex-accounts", headers=H).json()
    assert {k: v for k, v in get["login"].items() if k != "tail"} == st["codex_accounts"]["login"] and get["list"] == st["codex_accounts"]["list"]
    assert set(st["login"]) == {"running", "url", "tail", "adding", "email", "started_at", "result"}, "the Claude login payload is unchanged"
    assert st["accounts"]["store"]["supported"] is False, "and the Claude accounts are off here (no Linux, no credentials)"


def test_state_without_a_codex_binary_says_so_and_still_answers(box, api, monkeypatch):
    monkeypatch.setattr(settings, "codex_bin", lambda: None)
    st = api.get("/api/state", headers=H).json()
    assert st["codex_accounts"]["store"] == {"supported": False, "add": False, "reason": "codex is not installed", "count": 0} and st["codex_accounts"]["list"] == []


def test_a_failing_view_answers_the_empty_shape_not_an_error(box, api, monkeypatch):
    monkeypatch.setattr(cx, "view", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    r = api.get("/api/state", headers=H)
    assert r.status_code == 200 and r.json()["codex_accounts"]["list"] == [] and r.json()["codex_accounts"]["store"]["supported"] is False
    assert api.get("/api/codex-accounts", headers=H).json()["login"]["tail"] == []


def test_login_endpoint_status_codes(box, api):
    assert post(api, "/api/codex-accounts/login", json={"label": "Work"}).status_code == 202
    login = api.get("/api/codex-accounts", headers=H).json()["login"]
    assert login["adding"] is True and login["label"] == "Work" and login["running"] is True
    assert box.tmux["sent"][-1] == (tmux.LOGIN_SESSION, "codex login --device-auth")
    r = post(api, "/api/codex-accounts/login", json={"label": "Other"})
    assert r.status_code == 409 and "already running" in r.json()["detail"] == r.json()["error"] or "already running" in r.json()["error"]
    r = post(api, "/api/codex-accounts/login", json={"label": "Other", "restart": True})
    assert r.status_code == 202 and r.json() == {"ok": True} and api.get("/api/codex-accounts", headers=H).json()["login"]["label"] == "Other"
    assert api.delete("/api/codex-accounts/login", headers=H).json() == {"ok": True}
    assert tmux.LOGIN_SESSION not in box.tmux["sessions"] and api.get("/api/codex-accounts", headers=H).json()["login"]["adding"] is False
    for bad in ({}, {"label": ""}, {"label": "   "}, {"label": None}):
        r = post(api, "/api/codex-accounts/login", json=bad)
        assert r.status_code == 400 and r.json()["detail"] == "give the account a name" == r.json()["error"], bad
    assert post(api, "/api/codex-accounts/login").status_code == 400, "the body is optional but the label is not"
    assert post(api, "/api/codex-accounts/login", json={"label": 5}).status_code == 422


def test_login_endpoint_409_without_codex_or_device_auth(box, api, monkeypatch):
    write_fake_codex(box.fake, device_auth=False)
    os.utime(box.fake, (3, 3))
    codex_agent.reset_caches()
    r = post(api, "/api/codex-accounts/login", json={"label": "x"})
    assert r.status_code == 409 and r.json()["detail"] == cx.REASON_UPDATE == r.json()["error"]
    monkeypatch.setattr(settings, "codex_bin", lambda: None)
    r = post(api, "/api/codex-accounts/login", json={"label": "x"})
    assert r.status_code == 409 and r.json()["detail"] == "codex is not installed"


def test_the_whole_add_flow_over_the_api(box, api, monkeypatch):
    monkeypatch.setattr(cx, "watch_login", REAL_WATCH)
    ka = box.onboard("A")
    assert post(api, "/api/codex-accounts/login", json={"label": "Home"}).status_code == 202
    box.tmux["screen"] = DEVICE_PANE
    st = api.get("/api/state", headers=H).json()["codex_accounts"]["login"]
    assert st["url"] == "https://auth.openai.com/codex/device" and st["code"] == "ABCD-12345" and st["adding"] is True
    deadline = time.time() + 5
    box.pending("B")
    while time.time() < deadline and not cx.login_view()["result"]:
        time.sleep(0.02)
    body = api.get("/api/codex-accounts", headers=H).json()
    assert body["login"]["result"]["ok"] is True and body["login"]["adding"] is False and body["login"]["url"] is None
    assert [r["label"] for r in body["list"]][1:] == ["Home"] and body["list"][1]["saved"] is True and body["current"] == ka


def test_switch_endpoint(box, api):
    ka, kb = box.two()
    r = post(api, f"/api/codex-accounts/{kb}/switch")
    body = r.json()
    assert r.status_code == 200 and (body["ok"], body["already"], body["from"], body["to"], body["warnings"]) == (True, False, ka, kb, [])
    assert body["accounts"]["current"] == kb and body["accounts"]["store"]["count"] == 2 and all(x["saved"] for x in body["accounts"]["list"])
    assert "tail" not in body["accounts"]["login"] and box.auth.read_bytes() == AUTH["B"]
    r = post(api, f"/api/codex-accounts/{kb}/switch")
    assert r.status_code == 200 and r.json()["already"] is True
    r = post(api, "/api/codex-accounts/nope/switch")
    assert r.status_code == 404 and r.json()["detail"] == "no such account" == r.json()["error"]
    assert post(api, "/api/codex-accounts/..%2F..%2Fetc/switch").status_code == 404
    kc = box.add("C", "Spare")
    cx.forget(box.db, kc)
    r = post(api, f"/api/codex-accounts/{kc}/switch")
    assert r.status_code == 409 and "no saved login" in r.json()["detail"] and box.auth.read_bytes() == AUTH["B"]
    post(api, "/api/codex-accounts/login", json={"label": "x"})
    r = post(api, f"/api/codex-accounts/{ka}/switch")
    assert r.status_code == 409 and "login is in progress" in r.json()["detail"]
    api.delete("/api/codex-accounts/login", headers=H)
    add_codex_row(box.db)
    alive(box, "shop--api--s1")
    r = post(api, f"/api/codex-accounts/{ka}/switch")
    assert r.status_code == 409 and "close the board's Codex sessions first" in r.json()["detail"] and box.auth.read_bytes() == AUTH["B"]


def test_switch_endpoint_carries_the_warning_when_other_codex_processes_run(box, api):
    ka, kb = box.two()
    fake_proc(box.proc, 100, "codex", ppid=1)
    r = post(api, f"/api/codex-accounts/{kb}/switch")
    assert r.json()["warnings"] == [cx.WARN_OTHER]


def test_switch_endpoint_409_when_the_current_login_cannot_be_saved(box, api, monkeypatch):
    ka, kb = box.two()
    box.log_in("A2")
    monkeypatch.setattr(cx, "_store_auth", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    r = post(api, f"/api/codex-accounts/{kb}/switch")
    assert r.status_code == 409 and r.json()["detail"] == "could not save the current login first; nothing was changed"


def test_forget_endpoint(box, api):
    ka, kb = box.two()
    r = api.delete(f"/api/codex-accounts/{ka}/saved", headers=H)
    assert r.status_code == 409 and "live login" in r.json()["detail"] and cx.has_saved(ka)
    r = api.delete(f"/api/codex-accounts/{kb}/saved", headers=H)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["forgotten"] is True
    assert {x["key"]: x["saved"] for x in r.json()["accounts"]["list"]} == {ka: True, kb: False}
    assert api.delete("/api/codex-accounts/nope/saved", headers=H).status_code == 404


def test_patch_endpoint_renames_and_requires_a_label(box, api):
    ka, _kb = box.two()
    r = api.patch(f"/api/codex-accounts/{ka}", headers=H, json={"label": "  Main  "})
    assert r.status_code == 200 and r.json()["label"] == "Main" and r.json()["key"] == ka and r.json()["current"] is True
    for bad in ({}, {"label": None}, {"label": ""}, {"label": "  "}):
        r = api.patch(f"/api/codex-accounts/{ka}", headers=H, json=bad)
        assert r.status_code == 400 and r.json()["detail"] and r.json()["error"], bad
    assert api.patch("/api/codex-accounts/nope", headers=H, json={"label": "x"}).status_code == 404
    assert api.get("/api/codex-accounts", headers=H).json()["list"][0]["label"] == "Main"


def test_every_new_non_get_endpoint_needs_the_csrf_header_and_the_allowlist(box, api):
    ka, kb = box.two()
    no_csrf = {"Tailscale-User-Login": "alice@example.com"}
    other = {"Tailscale-User-Login": "mallory@example.com", "X-CCBoard": "1"}
    for method, path in (("post", "/api/codex-accounts/login"), ("delete", "/api/codex-accounts/login"), ("post", f"/api/codex-accounts/{kb}/switch"),
                         ("delete", f"/api/codex-accounts/{kb}/saved"), ("patch", f"/api/codex-accounts/{kb}")):
        for h in (no_csrf, other, {}):
            assert getattr(api, method)(path, headers=h).status_code == 403, (method, path, h)
    assert box.auth.read_bytes() == AUTH["A"] and cx.has_saved(kb)


def test_the_demo_state_carries_two_codex_accounts_one_current():
    demo = json.loads((Path(__file__).resolve().parent.parent / "app" / "static" / "demo" / "state.json").read_text(encoding="utf-8"))
    ca = demo["codex_accounts"]
    assert len(ca["list"]) == 2 and sum(1 for r in ca["list"] if r["current"]) == 1 and ca["current"] in {r["key"] for r in ca["list"]}
    assert all(set(r) == {"key", "label", "account_id", "plan", "saved", "current", "added_at", "last_seen", "saved_at"} and r["saved"] is True for r in ca["list"])
    assert ca["store"] == {"supported": True, "add": True, "reason": None, "count": 2}
    assert set(ca["login"]) == {"running", "adding", "label", "started_at", "url", "code", "result", "replace_key"} and ca["login"]["adding"] is False


# ---------------------------------------------------------------- startup

def test_the_lifespan_seeds_the_live_login(box):
    """The full lifespan (workers and all) starts with a login live: its slot exists by the time the board serves its first request."""
    from fastapi.testclient import TestClient
    from app import main
    box.log_in("A")
    assert not box.store.exists()
    with TestClient(main.app) as c:
        keys = cx._slot_keys()
        assert len(keys) == 1 and (cx.slot_dir(keys[0]) / "auth.json").read_bytes() == AUTH["A"]
        assert c.get("/api/codex-accounts", headers=H).json()["list"][0]["key"] == keys[0]
    cx.stop_watchers()


def test_a_failing_seed_is_only_a_warning_at_startup(box, monkeypatch, caplog):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(cx, "seed_current", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with TestClient(main.app) as c:
        assert c.get("/api/state", headers=H).status_code == 200
    assert "saved Codex login seed failed: RuntimeError" in caplog.text


def test_a_box_without_codex_starts_quietly(box, monkeypatch, caplog):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(settings, "codex_bin", lambda: None)
    box.log_in("A")
    with TestClient(main.app) as c:
        assert c.get("/api/state", headers=H).json()["codex_accounts"]["store"]["supported"] is False
    assert "Codex login seed" not in caplog.text and not box.store.exists()


# ---------------------------------------------------------------- the fake codex itself (what the real flow prints and writes)

def test_the_fake_codex_prints_the_link_and_the_code_and_writes_a_login_into_codex_home(box):
    home = box.tmp / "device-home"
    out = subprocess.run([str(box.fake), "login", "--device-auth"], capture_output=True, text=True, env={**os.environ, "CODEX_HOME": str(home)}).stdout
    parsed = cx.parse_login_text(out)
    assert parsed["url"] == "https://auth.openai.com/codex/device" and parsed["code"] == "ABCD-12345"
    assert (home / "auth.json").read_text() == FAKE_AUTH_BLOB and mode(home / "auth.json") == 0o600
    lh = subprocess.run([str(box.fake), "login", "--help"], capture_output=True, text=True).stdout
    assert "--device-auth" in lh
    assert subprocess.run([str(box.fake), "--version"], capture_output=True, text=True).stdout.strip() == "codex-cli 0.160.0"


def test_the_login_endpoint_takes_replace_key_without_a_label_and_answers_404_for_an_unknown_key(box, api):
    ka, kb = box.two()
    assert post(api, "/api/codex-accounts/login", json={}).status_code == 400, "a new account still needs a name"
    r = post(api, "/api/codex-accounts/login", json={"replace_key": "0" * 24})
    assert r.status_code == 404 and r.json()["detail"] == "no such account"
    assert not cx.pending_dir().exists()
    r = post(api, "/api/codex-accounts/login", json={"replace_key": kb})
    assert r.status_code == 202 and r.json() == {"ok": True}
    login = api.get("/api/codex-accounts", headers=H).json()["login"]
    assert login["adding"] is True and login["label"] == "Home" and login["replace_key"] == kb
    box.pending("C")
    res = cx.finalize(box.db)
    assert res["replaced"] is True and res["key"] == kb
    body = api.get("/api/codex-accounts", headers=H).json()
    assert [r["key"] for r in body["list"]].count(kb) == 1 and len(body["list"]) == 2 and body["login"]["result"]["replaced"] is True
    assert {r["key"]: r for r in body["list"]}[kb]["saved_at"]
    st = api.get("/api/state", headers=H).json()["codex_accounts"]
    assert "tail" not in st["login"] and st["login"]["replace_key"] is None


# ---------------------------------------------------------------- logout (v0.5.19, issue 3): POST /api/codex-accounts/logout

def test_logout_keeps_the_saved_copy_removes_the_live_file_and_clears_the_current_account(box):
    ka, kb = box.two()
    r = cx.logout(box.db)
    assert r == {"ok": True, "was": ka, "warnings": []}
    assert not box.auth.exists(), "the live login is gone"
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["A"], "the slot still holds the login"
    assert cx.current(box.db) is None and box.kv("codex_account_current") is None
    assert box.kv("codex_account_switch") is None
    assert cx.view(box.db)["current"] is None and [x["current"] for x in cx.view(box.db)["list"]] == [False, False]
    cx.tick(box.db)                                              # nothing brings the dropped login back or adopts anything
    assert not box.auth.exists() and cx.current(box.db) is None and len(box.kv("codex_accounts")) == 2
    assert cx.switch(box.db, ka)["to"] == ka and box.auth.read_bytes() == AUTH["A"], "and the old account can be switched back to"


def test_logout_saves_a_refreshed_token_into_the_current_slot_before_removing_it(box):
    ka, kb = box.two()
    box.log_in("A2")                                             # a running Codex refreshed the token; the tick has not saved it yet
    cx.logout(box.db)
    assert not box.auth.exists()
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["A2"] and (cx.slot_dir(ka) / "auth.json.prev").read_bytes() == AUTH["A"]


def test_logout_of_a_live_login_that_matches_no_slot_keeps_a_copy_as_an_adopted_account_first(box):
    # decision: an unknown login (no slot holds it, no current account owns it) is adopted like _adopt_unknown does, then removed; never deleted without a copy
    ka = box.onboard("A")
    box.db.kv_set("codex_account_current", {})                   # the board lost track of who is live
    box.log_in("C")
    assert cx._matching_slot(AUTH["C"]) is None
    r = cx.logout(box.db)
    assert r["ok"] and r["was"] and r["was"] != ka
    assert not box.auth.exists()
    assert (cx.slot_dir(r["was"]) / "auth.json").read_bytes() == AUTH["C"]
    assert box.kv("codex_accounts")[r["was"]]["label"].startswith("codex login ")
    assert (cx.slot_dir(ka) / "auth.json").read_bytes() == AUTH["A"], "A's saved login was not touched"
    assert cx.switch(box.db, r["was"])["to"] == r["was"] and box.auth.read_bytes() == AUTH["C"]


def test_logout_never_removes_bytes_it_could_not_copy(box, monkeypatch):
    ka, kb = box.two()
    box.log_in("A2")
    monkeypatch.setattr(cx, "_store_auth", lambda slot, data: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(cx.SwitchRefused, match="could not save the current login first; nothing was changed"):
        cx.logout(box.db)
    assert box.auth.read_bytes() == AUTH["A2"] and cx.current(box.db) == ka


def test_logout_refuses_when_the_file_changes_while_it_is_saved(box, monkeypatch):
    ka, kb = box.two()
    box.log_in("A2")
    real = cx._store_auth

    def store_then_rewrite(slot, data):
        real(slot, data)
        put_auth(box.live, AUTH["B2"], age=0.0)                  # another Codex rewrote the file in between
    monkeypatch.setattr(cx, "_store_auth", store_then_rewrite)
    with pytest.raises(cx.SwitchRefused, match="changed while it was being saved"):
        cx.logout(box.db)
    assert box.auth.read_bytes() == AUTH["B2"] and cx.current(box.db) == ka


def test_logout_is_busy_while_a_board_codex_session_is_open_or_a_login_is_in_flight_and_changes_nothing(box):
    ka, kb = box.two()
    add_codex_row(box.db)
    alive(box, "shop--api--s1")
    with pytest.raises(cx.Busy, match=r"close the board's Codex sessions first; a running Codex keeps its login and would write it back"):
        cx.logout(box.db)
    assert box.auth.read_bytes() == AUTH["A"] and cx.current(box.db) == ka
    box.tmux["sessions"].clear()
    cx.start_login(box.db, "Third")
    with pytest.raises(cx.Busy, match="a login is in progress"):
        cx.logout(box.db)
    assert box.auth.read_bytes() == AUTH["A"] and cx.current(box.db) == ka and not (cx.slot_dir(ka) / "auth.json.prev").exists()
    cx.cancel_login()
    assert cx.logout(box.db)["was"] == ka


def test_logout_with_nobody_logged_in_is_a_harmless_noop(box):
    assert cx.logout(box.db) == {"ok": True, "was": None, "warnings": []}
    ka, kb = box.two()
    box.auth.unlink()                                            # a hand logout: the kv still names A
    assert cx.logout(box.db) == {"ok": True, "was": None, "warnings": []}
    assert cx.current(box.db) is None and cx.has_saved(ka), "the stale current record is cleared, the slot is kept"


def test_logout_refuses_a_live_file_it_cannot_explain_right_after_a_switch(box):
    ka, kb = box.two()
    cx.switch(box.db, kb)
    box.log_in("A2")
    with pytest.raises(cx.SwitchRefused, match="cannot be told from an older Codex's write-back"):
        cx.logout(box.db)
    assert box.auth.read_bytes() == AUTH["A2"]


def test_logout_warns_when_other_codex_processes_exist(box):
    box.two()
    fake_proc(box.proc, 100, "codex", ppid=1)
    assert cx.logout(box.db)["warnings"] == ["other Codex processes on this box keep the previous login until they restart"]


def test_logout_drops_the_cached_login_verdict(box, monkeypatch):
    box.two()
    cleared = []
    monkeypatch.setattr(codex_agent, "forget_auth", lambda: cleared.append(1))
    cx.logout(box.db)
    assert cleared == [1]


def test_logout_needs_a_codex_binary(box, monkeypatch):
    monkeypatch.setattr(settings, "codex_bin", lambda: None)
    with pytest.raises(cx.Unsupported):
        cx.logout(box.db)


def test_the_logout_route_answers_ok_was_warnings_accounts_and_refuses_with_409(box, api):
    ka, kb = box.two()
    assert api.post("/api/codex-accounts/logout").status_code == 403, "X-CCBoard is needed"
    add_codex_row(box.db)
    alive(box, "shop--api--s1")
    r = post(api, "/api/codex-accounts/logout")
    assert r.status_code == 409 and r.json()["detail"] == r.json()["error"] and "close the board's Codex sessions first" in r.json()["detail"]
    assert box.auth.read_bytes() == AUTH["A"]
    box.tmux["sessions"].clear()
    r = post(api, "/api/codex-accounts/logout")
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True and body["was"] == ka and body["warnings"] == []
    assert body["accounts"]["current"] is None and {x["key"]: x["saved"] for x in body["accounts"]["list"]} == {ka: True, kb: True}
    assert api.get("/api/state", headers=H).json()["codex_accounts"]["current"] is None
    again = post(api, "/api/codex-accounts/logout")
    assert again.status_code == 200 and again.json()["was"] is None and again.json()["ok"] is True


def test_the_logout_bytes_never_appear_in_the_answer_the_log_the_kv_or_the_events(box, api, caplog):
    caplog.set_level(logging.DEBUG)
    ka, kb = box.two()
    box.log_in("A2")
    seen = [post(api, "/api/codex-accounts/logout").text, post(api, "/api/codex-accounts/logout").text, api.get("/api/state", headers=H).text]
    dump = []
    for (name,) in box.db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        dump += [repr(tuple(row)) for row in box.db.conn.execute(f'SELECT * FROM "{name}"').fetchall()]
    blob = "\n".join(seen + dump + [caplog.text] + [(cx.slot_dir(k) / "meta.json").read_text() for k in cx._slot_keys()])
    assert "SECRET" not in blob, [ln for ln in blob.splitlines() if "SECRET" in ln][:3]
    assert "logged out of Codex" in caplog.text


def test_split_notice_rides_on_state_and_the_dismiss_endpoint_drops_it(box, api):
    ka, kb = split_box(box)
    write_rollout(box.live, "hand", account=ACCT_C, created=time.time() + 5)
    split_now(box)
    note = api.get("/api/state", headers=H).json()["codex_accounts"]["notice"]
    assert note["text"] == cx.SPLIT_NOTICE and note["key"] == cx.current(box.db) and note["label"] == cx.SPLIT_LABEL
    assert api.delete("/api/codex-accounts/notice", headers=H).json() == {"ok": True, "dismissed": True}
    assert api.get("/api/state", headers=H).json()["codex_accounts"]["notice"] is None
    assert api.delete("/api/codex-accounts/notice", headers=H).json() == {"ok": True, "dismissed": False}
