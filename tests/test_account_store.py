"""v0.5.17c, saved Claude logins and the one-tap account switch (backend): app/account_store.py, the accounts endpoints, the demo keys.

Everything runs on temp directories: settings.data_dir and settings.claude_config_dir are the sandboxed ones from the fixtures, the login
session is the fake tmux, `claude` is never started (a fake path stands in for the binary, which the fake tmux never runs) and nothing reads
or writes the real ~/.claude, ~/.claude.json or ~/.codex. Credentials are opaque bytes with a sentinel in them (SECRET-A / SECRET-B); the
secrecy tests look for the sentinels everywhere.
"""
import ast
import inspect
import json
import logging
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import account_store, accounts, autoresume, claude_auth, login_problem, tmux
from app.platform import browser_stub
from app.config import settings
from app.db import DB, iso

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
UA = "aaaaaaaa-0000-4000-8000-000000000001"
UB = "bbbbbbbb-0000-4000-8000-000000000002"
UC = "cccccccc-0000-4000-8000-000000000003"
WHO = {"A": (UA, "a@example.com", "Ann"), "B": (UB, "b@example.com", "Bob"), "C": (UC, "c@example.com", "Cy")}
T0 = 1_790_000_000
CRED = {"A": b'{"claudeAiOauth":{"accessToken":"SECRET-A"}}', "B": b'{"claudeAiOauth":{"accessToken":"SECRET-B"}}',
        "A2": b'{"claudeAiOauth":{"accessToken":"SECRET-A-refreshed"}}', "B2": b'{"claudeAiOauth":{"accessToken":"SECRET-B-refreshed"}}',
        "C": b'{"claudeAiOauth":{"accessToken":"SECRET-C"}}'}
_mtime = [time.time_ns()]


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def oauth(who: str) -> dict:
    uuid, email, name = WHO[who]
    return {"accountUuid": uuid, "emailAddress": email, "displayName": name, "organizationUuid": "dddddddd-0000-4000-8000-000000000004",
            "organizationName": f"{name}'s Org", "organizationRole": "admin", "organizationType": "claude_max",
            "billingType": "stripe_subscription"}


def put_state(path: Path, who: str | None, **extra) -> None:
    """Claude's state file as the box has it: the oauthAccount and a lot of other keys that must survive a switch. Every write gets its own
    mtime (a coarse filesystem clock can give two quick writes the same one, and the board tells a rewrite by the stamp)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"numStartups": 7, "theme": "dark", "projects": {"/x": {"history": ["y" * 50] * 5}}, **extra}
    if who:
        body["oauthAccount"] = oauth(who)
    path.write_text(json.dumps(body))
    os.chmod(path, 0o600)                                       # Claude keeps its state file private
    _mtime[0] += 1_000_000_000
    os.utime(path, ns=(_mtime[0], _mtime[0]))


def put_creds(dirpath: Path, blob: bytes, age: float = 100.0) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / ".credentials.json"
    p.write_bytes(blob)
    os.chmod(p, 0o600)
    t = time.time() - age
    os.utime(p, (t, t))
    return p


@pytest.fixture
def box(projects_dir, tmp_path, monkeypatch):
    """Saved logins on, over the sandboxed data dir and Claude config dir, no real sleeping, no real claude."""
    monkeypatch.setattr(account_store, "supported", lambda: True)
    monkeypatch.setattr(account_store, "_sleep", lambda s: None)
    monkeypatch.setattr(claude_auth, "status", lambda: {"installed": True, "loggedIn": False})
    monkeypatch.setattr(claude_auth, "version", lambda: "0.0-test")
    monkeypatch.setattr(settings, "claude_bin", lambda: "/fake/bin/claude")        # the fake tmux never runs it
    monkeypatch.setattr(settings, "backup_extra", [])
    accounts.invalidate()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    live = settings.claude_config_dir
    b = SimpleNamespace(db=DB(settings.db_path), live=live, tmp=tmp_path, state=live / ".claude.json", beside=tmp_path / "claude.json",
                        creds=live / ".credentials.json", store=settings.data_dir / "accounts")

    def log_in(who, creds=None, age=100.0, **extra):
        """What a `/login` in a terminal leaves: the credentials and the state file naming the account."""
        put_creds(live, CRED[creds or who], age)
        put_state(b.state, who, **extra)
        accounts.invalidate()

    def onboard(who, **kw):
        """Log in live, let the board see it and save the login (the lifespan seed or the tick does this on the box)."""
        log_in(who, **kw)
        accounts.observe(b.db, T0, force=True, auth=False)
        key = account_store.seed_current(b.db, T0)
        assert key == WHO[who][0]
        return key

    def slot(who):
        return account_store.slot_dir(WHO[who][0])

    def live_oauth():
        return json.loads(b.state.read_text())["oauthAccount"]

    def pending(who, age=100.0, with_state=True):
        p = account_store.pending_dir()
        p.mkdir(parents=True, exist_ok=True)
        put_creds(p, CRED[who], age)
        if with_state:
            put_state(p / ".claude.json", who)
        return p

    def kvv(key):
        r = b.db.kv_get(key)
        return r["value"] if r else None

    b.log_in, b.onboard, b.slot, b.live_oauth, b.pending, b.kv = log_in, onboard, slot, live_oauth, pending, kvv
    yield b
    account_store.stop_watchers()
    accounts.invalidate()


def tree_has_no_temp_files(root: Path) -> bool:
    return not [p for p in root.rglob("*") if p.name.endswith(".tmp")]


# ---------------------------------------------------------------- slots, modes, save, seed

def test_a_slot_is_named_by_a_hash_of_the_key_never_the_key(box):
    d = account_store.slot_dir("someone@example.com")
    import hashlib
    assert d.parent == box.store and d.name == hashlib.sha256(b"someone@example.com").hexdigest()[:24]
    assert "someone" not in str(d) and "@" not in d.name
    assert account_store.slot_dir("x") != account_store.slot_dir("y")


def test_seed_creates_the_slot_byte_exact_with_private_modes(box):
    assert account_store.has_saved(UA) is False
    key = box.onboard("A")
    assert key == UA and account_store.has_saved(UA)
    s = box.slot("A")
    assert (s / ".credentials.json").read_bytes() == CRED["A"], "a byte copy"
    assert json.loads((s / ".claude.json").read_text())["oauthAccount"] == box.live_oauth()
    assert mode(box.store) == 0o700 and mode(s) == 0o700
    assert mode(s / ".credentials.json") == 0o600 and mode(s / ".claude.json") == 0o600
    assert not (s / ".credentials.json.prev").exists(), "nothing to rotate on a first save"
    assert box.kv("accounts")[UA]["email"] == "a@example.com"
    assert box.kv("account_saved")[UA] == account_store._stamp(box.creds)
    assert tree_has_no_temp_files(box.store) and tree_has_no_temp_files(box.live)


def test_directories_that_exist_wider_are_tightened(box):
    box.store.mkdir(parents=True)
    os.chmod(box.store, 0o755)
    box.onboard("A")
    os.chmod(box.slot("A"), 0o755)
    account_store.save_live(box.db, UA)
    assert mode(box.store) == 0o700 and mode(box.slot("A")) == 0o700


def test_seed_is_a_noop_when_nobody_is_logged_in_or_the_slot_exists(box):
    assert account_store.seed_current(box.db) is None and not box.store.exists()
    box.onboard("A")
    before = (box.slot("A") / ".credentials.json").stat().st_mtime_ns
    box.log_in("A", creds="A2")                                 # the live file moved on, but a slot already exists: seed leaves it alone
    assert account_store.seed_current(box.db) == UA
    assert (box.slot("A") / ".credentials.json").stat().st_mtime_ns == before
    assert (box.slot("A") / ".credentials.json").read_bytes() == CRED["A"]


def test_save_live_rotates_the_old_copy_to_prev_and_only_when_it_changed(box):
    box.onboard("A")
    s = box.slot("A")
    assert account_store.save_live(box.db, UA) is True          # same bytes: nothing rotates
    assert not (s / ".credentials.json.prev").exists()
    box.log_in("A", creds="A2")
    assert account_store.save_live(box.db, UA) is True
    assert (s / ".credentials.json").read_bytes() == CRED["A2"] and (s / ".credentials.json.prev").read_bytes() == CRED["A"]
    assert mode(s / ".credentials.json.prev") == 0o600
    assert box.kv("account_saved")[UA] == account_store._stamp(box.creds)
    box.log_in("A", creds="A")                                  # a third generation: .prev holds exactly one generation
    assert account_store.save_live(box.db, UA) is True
    assert (s / ".credentials.json.prev").read_bytes() == CRED["A2"]


def test_save_live_refuses_when_the_live_identity_is_someone_else(box):
    box.onboard("A")
    slot_creds = box.slot("A") / ".credentials.json"
    before = account_store._stamp(slot_creds)
    box.log_in("B")
    assert account_store.save_live(box.db, UA) is False, "the live state names B"
    assert slot_creds.read_bytes() == CRED["A"], "A's saved login is not overwritten with B's credentials"
    assert account_store._stamp(slot_creds) == before, "not even rewritten: the check comes before the copy, not only after it"
    assert not (box.slot("A") / ".credentials.json.prev").exists()
    assert account_store.save_live(box.db, UC) is False and not box.slot("C").exists(), "no slot is created for an account that is not live"
    assert tree_has_no_temp_files(box.store)


def test_save_live_undoes_the_copy_when_the_identity_changes_during_it(box, monkeypatch):
    box.onboard("A")
    s = box.slot("A")
    box.log_in("A", creds="A2")
    real, calls = accounts.read_identity, []

    def flipping(*a, **k):
        calls.append(1)
        if len(calls) == 2:                                     # the second read is the one after the copy: a /login to B landed meanwhile
            put_state(box.state, "B")
        return real(*a, **k)
    monkeypatch.setattr(accounts, "read_identity", flipping)
    assert account_store.save_live(box.db, UA) is False and len(calls) == 2
    assert (s / ".credentials.json").read_bytes() == CRED["A"] and not (s / ".credentials.json.prev").exists(), "the slot is as it was"
    # a first-ever save that is undone leaves no credentials behind at all
    box.log_in("C")
    calls.clear()
    monkeypatch.setattr(accounts, "read_identity", lambda *a, **k: (calls.append(1), real(*a, **k) if len(calls) == 1 else None)[1])
    assert account_store.save_live(box.db, UC) is False
    assert not (box.slot("C") / ".credentials.json").exists()


def test_save_live_waits_for_a_young_credentials_file_in_small_steps(box, monkeypatch):
    """A /login in a terminal writes the credentials first and the identity a moment later: the copy waits up to SETTLE for the pair."""
    box.log_in("A", age=0.0)                                    # credentials written just now, state naming nobody yet
    put_state(box.state, None)
    accounts.invalidate()
    slept = []

    def sleeper(s):                                             # time passes: each pause makes the credentials file 1.6 s older
        slept.append(s)
        t = time.time() - 1.6 * len(slept)
        os.utime(box.creds, (t, t))
        if len(slept) == 1:
            put_state(box.state, "A")                           # the identity arrives
    monkeypatch.setattr(account_store, "_sleep", sleeper)
    assert account_store.save_live(box.db, UA) is True
    assert slept == [account_store.SETTLE_STEP] * 2, "waited in small steps until the file was SETTLE old, no longer"
    assert (box.slot("A") / ".credentials.json").read_bytes() == CRED["A"]


def test_the_settle_wait_is_bounded(box, monkeypatch):
    box.log_in("A", age=0.0)
    slept = []
    monkeypatch.setattr(account_store, "_sleep", lambda s: slept.append(s))
    assert account_store.save_live(box.db, UA) is True          # the file never gets old (the fake sleep does not pass time): the wait ends
    assert 1 <= len(slept) <= int(account_store.SETTLE / account_store.SETTLE_STEP) + 1
    assert account_store.SETTLE == 3 and account_store.SYNC_SETTLE == 30


def test_save_live_without_credentials_or_with_an_empty_file_saves_nothing(box):
    put_state(box.state, "A")
    accounts.invalidate()
    assert account_store.save_live(box.db, UA) is False and not box.store.exists()
    put_creds(box.live, b"")
    assert account_store.save_live(box.db, UA) is False and not account_store.has_saved(UA)


# ---------------------------------------------------------------- switch

def two_accounts(box):
    """A and B both saved, B live (B logged in last)."""
    box.onboard("A")
    box.onboard("B")


def test_switch_round_trip_puts_the_right_bytes_in_the_right_places(box):
    two_accounts(box)
    assert box.creds.read_bytes() == CRED["B"] and account_store.has_saved(UB)
    before_state = json.loads(box.state.read_text())
    r = account_store.switch(box.db, UA, now=T0 + 10)
    assert r == {"ok": True, "already": False, "from": UB, "to": UA}
    assert box.creds.read_bytes() == CRED["A"], "live now holds A's login, byte for byte"
    assert (box.slot("B") / ".credentials.json").read_bytes() == CRED["B"], "B's login was saved before it was replaced"
    now_state = json.loads(box.state.read_text())
    assert now_state["oauthAccount"] == oauth("A")
    assert {k: v for k, v in now_state.items() if k != "oauthAccount"} == {k: v for k, v in before_state.items() if k != "oauthAccount"}, \
        "every other key of Claude's state file is untouched"
    assert mode(box.creds) == 0o600 and mode(box.state) == 0o600
    r = account_store.switch(box.db, UB, now=T0 + 20)
    assert r == {"ok": True, "already": False, "from": UA, "to": UB}
    assert box.creds.read_bytes() == CRED["B"] and box.live_oauth() == oauth("B")
    assert (box.slot("A") / ".credentials.json").read_bytes() == CRED["A"]
    assert tree_has_no_temp_files(box.store) and tree_has_no_temp_files(box.live) and tree_has_no_temp_files(box.tmp)


def test_a_token_refreshed_by_a_running_session_travels_with_the_account(box):
    two_accounts(box)
    box.log_in("B", creds="B2")                                 # B's session refreshed its token: the live file is the truth for B
    account_store.switch(box.db, UA)
    assert (box.slot("B") / ".credentials.json").read_bytes() == CRED["B2"] and (box.slot("B") / ".credentials.json.prev").read_bytes() == CRED["B"]
    account_store.switch(box.db, UB)
    assert box.creds.read_bytes() == CRED["B2"], "switching back restores the refreshed login, not the one from the first save"


def test_the_state_file_keeps_its_mode_and_both_candidate_files_are_updated(box):
    two_accounts(box)
    put_state(box.beside, "B", marker="beside")                  # the state file beside the dir: both candidates now exist and name B
    os.chmod(box.state, 0o640)
    os.chmod(box.beside, 0o644)
    accounts.invalidate()
    account_store.switch(box.db, UA)
    assert mode(box.state) == 0o640 and mode(box.beside) == 0o644, "the original mode is kept"
    for p in (box.state, box.beside):
        assert json.loads(p.read_text())["oauthAccount"] == oauth("A")
    assert json.loads(box.beside.read_text())["marker"] == "beside"


def test_with_no_live_state_file_the_first_candidate_is_created(box):
    box.onboard("A")
    box.onboard("B")
    box.state.unlink()                                          # B's login lost its state file: nobody can be named
    accounts.invalidate()
    # nobody named + live credentials in place: refuse rather than overwrite a login the board cannot name
    with pytest.raises(account_store.SwitchRefused):
        account_store.switch(box.db, UA)
    assert box.creds.read_bytes() == CRED["B"]
    box.creds.unlink()                                          # truly logged out
    assert account_store.switch(box.db, UA)["from"] is None
    assert box.creds.read_bytes() == CRED["A"] and box.live_oauth() == oauth("A") and mode(box.state) == 0o600


def test_an_unparseable_state_file_is_never_overwritten(box):
    two_accounts(box)
    put_state(box.beside, "B")                                  # the second candidate exists, then goes bad: Claude is mid-write, for good
    accounts.invalidate()
    box.beside.write_text("{not json")
    os.utime(box.beside, ns=(_mtime[0] - 5_000_000_000,) * 2)   # older than the state file: the inside file still names the account
    garbage = box.beside.read_bytes()
    state_before = box.state.read_bytes()
    with pytest.raises(account_store.SwitchRefused) as ei:
        account_store.switch(box.db, UA)
    assert box.beside.read_bytes() == garbage, "never written when it could not be parsed"
    assert box.creds.read_bytes() == CRED["B"], "the previous login is back"
    assert json.loads(box.state.read_text()) == json.loads(state_before), "the file that did parse is back to B too"
    assert "SECRET" not in str(ei.value)


def test_switching_to_the_live_account_is_a_noop(box):
    two_accounts(box)
    stamps = (account_store._stamp(box.creds), account_store._stamp(box.state), box.kv("account_switch"))
    r = account_store.switch(box.db, UB)
    assert r == {"ok": True, "already": True, "from": UB, "to": UB}
    assert (account_store._stamp(box.creds), account_store._stamp(box.state), box.kv("account_switch")) == stamps


def test_switch_refuses_unknown_accounts_and_accounts_without_a_saved_login(box):
    box.onboard("A")
    with pytest.raises(LookupError):
        account_store.switch(box.db, "nope")
    accounts.remember(box.db, {"key": UC, "email": "c@example.com", "name": "Cy", "config_dir": None}, T0)
    with pytest.raises(account_store.NoSavedLogin):
        account_store.switch(box.db, UC)
    assert box.creds.read_bytes() == CRED["A"]


def test_a_slot_whose_identity_is_not_the_account_it_claims_is_refused(box):
    two_accounts(box)
    put_state(box.slot("A") / ".claude.json", "C")              # damaged / mixed up slot: names C, filed under A
    with pytest.raises(account_store.NoSavedLogin):
        account_store.switch(box.db, UA)
    assert box.creds.read_bytes() == CRED["B"] and box.live_oauth() == oauth("B")


def test_switch_is_busy_while_a_login_from_settings_is_waiting_for_its_code(box, fake_tmux):
    two_accounts(box)
    account_store.start_login(box.db)
    with pytest.raises(account_store.Busy):
        account_store.switch(box.db, UA)
    assert box.creds.read_bytes() == CRED["B"]
    account_store.cancel_login()
    assert account_store.switch(box.db, UA)["to"] == UA


def test_switch_refuses_when_the_current_login_cannot_be_saved_first(box, monkeypatch):
    two_accounts(box)
    monkeypatch.setattr(account_store, "_save_live", lambda *a, **k: None)
    with pytest.raises(account_store.SwitchRefused, match="could not save the current login first; nothing was changed"):
        account_store.switch(box.db, UA)
    assert box.creds.read_bytes() == CRED["B"] and box.live_oauth() == oauth("B")


def test_a_failure_applying_the_state_file_puts_the_previous_login_back(box, monkeypatch):
    two_accounts(box)
    real, fail = os.replace, {"on": True}

    def flaky(src, dst, *a, **k):
        if fail["on"] and Path(dst) == box.state:
            fail["on"] = False                                   # fails once: the rollback itself replaces files too
            raise OSError("disk on fire")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(account_store.SwitchRefused, match="previous login was put back"):
        account_store.switch(box.db, UA)
    assert box.creds.read_bytes() == CRED["B"] and box.live_oauth() == oauth("B"), "step 5 was undone from the copy of step 4"
    assert (box.slot("B") / ".credentials.json").read_bytes() == CRED["B"]
    assert box.kv("account_current")["key"] == UB and box.kv("account_switch") is None
    assert tree_has_no_temp_files(box.live)


def test_a_failure_replacing_the_live_credentials_changes_nothing(box, monkeypatch):
    two_accounts(box)
    real, fail = os.replace, {"on": True}

    def flaky(src, dst, *a, **k):
        if fail["on"] and Path(dst) == box.creds:
            fail["on"] = False
            raise OSError("disk on fire")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(account_store.SwitchRefused):
        account_store.switch(box.db, UA)
    assert box.creds.read_bytes() == CRED["B"] and box.live_oauth() == oauth("B")


def test_a_failure_on_the_second_replace_under_the_live_dir_rolls_back(box, monkeypatch):
    """The brief's wording: os.replace fails on the second call (the first replaces the credentials, the second a state file)."""
    two_accounts(box)
    real, seen = os.replace, []

    def flaky(src, dst, *a, **k):
        if str(dst).startswith(str(box.live)):
            seen.append(Path(dst).name)
            if len(seen) == 2:
                raise OSError("disk on fire")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(account_store.SwitchRefused):
        account_store.switch(box.db, UA)
    assert box.creds.read_bytes() == CRED["B"] and box.live_oauth() == oauth("B")


def test_a_switch_moves_the_current_account_and_writes_the_acct_event(box):
    two_accounts(box)
    stamp_calls = box.db.samples_query("acct", None, iso(0))
    assert [(k, json.loads(m)) for _a, k, _v, m in stamp_calls] == [(UA, {"to": UA}), (UB, {"from": UA, "to": UB})]
    account_store.switch(box.db, UA, now=T0 + 99)
    cur = box.kv("account_current")
    assert cur["key"] == UA and cur["config_dir"] == str(settings.claude_config_dir), "the live dir, not the slot's"
    ev = [(k, json.loads(m)) for _a, k, _v, m in box.db.samples_query("acct", None, iso(0))]
    assert ev[-1] == (UA, {"from": UB, "to": UA})
    assert box.kv("accounts")[UA]["config_dir"] == str(settings.claude_config_dir)
    sw = box.kv("account_switch")
    assert sw == {"from": UB, "to": UA, "at": iso(T0 + 99), "creds_stamp": account_store._stamp(box.creds), "repairs": 0}
    assert box.kv("account_saved")[UA] == sw["creds_stamp"], "live and the slot hold the same bytes right after"
    assert accounts.current(box.db) == UA


def test_a_login_the_board_has_not_seen_yet_is_saved_and_listed_when_it_is_switched_away_from(box):
    """/login to a never-seen account in a terminal, then a switch before the tick noticed it: its login must stay one tap away."""
    box.onboard("A")
    box.log_in("C")                                              # no observe(), no seed: the board knows nothing of C
    assert UC not in box.kv("accounts")
    r = account_store.switch(box.db, UA)
    assert r["from"] == UC and box.creds.read_bytes() == CRED["A"]
    assert UC in box.kv("accounts") and account_store.has_saved(UC) and (box.slot("C") / ".credentials.json").read_bytes() == CRED["C"]
    assert {x["key"]: x["saved"] for x in account_store.decorate(accounts.view(box.db))["list"]} == {UA: True, UC: True}
    assert accounts.current(box.db) == UA
    assert account_store.switch(box.db, UC)["to"] == UC and box.creds.read_bytes() == CRED["C"], "and it switches back"


def test_the_hold_keeps_a_stale_copy_of_the_old_account_from_flipping_current_back(box, monkeypatch):
    two_accounts(box)
    clock = [1000.0]
    monkeypatch.setattr(accounts, "_clock", lambda: clock[0])
    account_store.switch(box.db, UA, now=T0 + 10)
    assert accounts.held() == {"to": UA, "frm": UB}
    put_state(box.state, "B")                                    # a session that ran before the switch wrote its own oauthAccount back
    assert accounts.observe(box.db, T0 + 20, force=True, auth=False) == UA
    assert accounts.current(box.db) == UA
    assert [k for _a, k, _v, _m in box.db.samples_query("acct", None, iso(0))][-1] == UA, "no second acct event"
    put_state(box.state, "C")                                    # a real /login to a third account drops the hold and is applied
    assert accounts.observe(box.db, T0 + 30, force=True, auth=False) == UC
    assert accounts.held() is None
    put_state(box.state, "B")
    assert accounts.observe(box.db, T0 + 40, force=True, auth=False) == UB, "with the hold gone, B is a plain switch"


def test_the_hold_runs_out(box, monkeypatch):
    two_accounts(box)
    clock = [1000.0]
    monkeypatch.setattr(accounts, "_clock", lambda: clock[0])
    account_store.switch(box.db, UA)
    clock[0] += account_store.HOLD_SECONDS + 1
    assert accounts.held() is None
    put_state(box.state, "B")
    assert accounts.observe(box.db, T0, force=True, auth=False) == UB


def test_forget_deletes_the_slot_but_not_the_record_and_never_the_live_account(box):
    two_accounts(box)
    with pytest.raises(account_store.Busy):
        account_store.forget(box.db, UB)
    assert account_store.has_saved(UB)
    with pytest.raises(LookupError):
        account_store.forget(box.db, "nope")
    assert account_store.forget(box.db, UA) == {"ok": True, "forgotten": True}
    assert not box.slot("A").exists() and not account_store.has_saved(UA)
    assert UA in box.kv("accounts") and UA not in (box.kv("account_saved") or {}), "the usage history stays"
    assert account_store.forget(box.db, UA)["forgotten"] is False
    with pytest.raises(account_store.NoSavedLogin):
        account_store.switch(box.db, UA)


def test_unsupported_platforms_refuse_every_mutation_and_say_why(box, monkeypatch):
    two_accounts(box)
    monkeypatch.setattr(account_store, "supported", lambda: False)
    for call in (lambda: account_store.save_live(box.db, UB), lambda: account_store.seed_current(box.db), lambda: account_store.switch(box.db, UA),
                 lambda: account_store.forget(box.db, UA), lambda: account_store.start_login(box.db)):
        with pytest.raises(account_store.Unsupported, match=r"saved logins need Claude's file credentials \(Linux\)"):
            call()
    assert account_store.tick(box.db) is None and account_store.finalize(box.db) is None
    d = account_store.decorate(accounts.view(box.db))
    assert d["store"] == {"supported": False, "reason": account_store.REASON, "count": 0} and not any(r["saved"] for r in d["list"])
    assert box.creds.read_bytes() == CRED["B"]


def test_the_real_supported_is_linux_only(monkeypatch):
    from app import platform as plat
    monkeypatch.undo()                                          # drops the suite's default (account_store.supported -> False): the real one is back
    monkeypatch.setattr(plat, "under_drvfs", lambda p: False)
    for linux, mac, win, want in ((True, False, False, True), (False, True, False, False), (False, False, True, False), (False, False, False, False)):
        monkeypatch.setattr(plat, "IS_LINUX", linux)            # (issue #119: the platform module's flags are the one place for OS questions)
        monkeypatch.setattr(plat, "IS_MACOS", mac)
        monkeypatch.setattr(plat, "IS_WINDOWS", win)
        assert account_store.supported() is want


# ---------------------------------------------------------------- the tick: repair, sync, seed

def test_tick_repairs_a_stale_state_file_at_most_five_times_while_the_credentials_are_unchanged(box):
    two_accounts(box)
    account_store.switch(box.db, UA)
    for n in range(1, account_store.MAX_REPAIRS + 1):
        put_state(box.state, "B")                                # the stale write-back
        account_store.tick(box.db)
        assert box.live_oauth() == oauth("A") and box.kv("account_switch")["repairs"] == n
    put_state(box.state, "B")
    account_store.tick(box.db)
    assert box.live_oauth() == oauth("B") and box.kv("account_switch")["repairs"] == 5, "the sixth is left alone"
    assert (box.slot("B") / ".credentials.json").read_bytes() == CRED["B"], "and B's saved login was not overwritten with A's live credentials"


def test_a_changed_credentials_stamp_stops_the_repairs(box):
    two_accounts(box)
    account_store.switch(box.db, UA)
    box.log_in("A", creds="A2")                                  # the live credentials were refreshed since the switch
    put_state(box.state, "B")
    account_store.tick(box.db)
    assert box.live_oauth() == oauth("B") and box.kv("account_switch")["repairs"] == 0
    assert (box.slot("B") / ".credentials.json").read_bytes() == CRED["B"], "never copied into the slot of the account the switch left"


def test_the_sync_never_copies_the_live_credentials_into_the_account_a_switch_left(box):
    two_accounts(box)
    account_store.switch(box.db, UA)
    for _ in range(account_store.MAX_REPAIRS + 2):
        put_state(box.state, "B")
        account_store.tick(box.db)
    assert (box.slot("B") / ".credentials.json").read_bytes() == CRED["B"]
    accounts._hold = None                                        # even once the 90 s hold is over: the switch's `from` stays off limits
    box.log_in("A", creds="A2")
    put_state(box.state, "B")
    account_store.tick(box.db)
    assert (box.slot("B") / ".credentials.json").read_bytes() == CRED["B"]


def test_tick_keeps_the_live_accounts_saved_copy_fresh_once_the_file_is_quiet(box):
    box.onboard("A")
    s = box.slot("A")
    box.log_in("A", creds="A2", age=10)                          # refreshed 10 s ago: younger than SYNC_SETTLE
    account_store.tick(box.db)
    assert (s / ".credentials.json").read_bytes() == CRED["A"], "left alone while the file is still being written"
    put_creds(box.live, CRED["A2"], age=100)
    account_store.tick(box.db)
    assert (s / ".credentials.json").read_bytes() == CRED["A2"] and (s / ".credentials.json.prev").read_bytes() == CRED["A"]
    assert box.kv("account_saved")[UA] == account_store._stamp(box.creds)
    stamp = (s / ".credentials.json").stat().st_mtime_ns
    account_store.tick(box.db)
    assert (s / ".credentials.json").stat().st_mtime_ns == stamp, "an unchanged live file is not copied again"


def test_tick_saves_a_login_made_in_a_terminal_that_has_no_slot_yet(box):
    box.onboard("A")
    box.log_in("C")                                              # /login to a third account in a terminal
    account_store.tick(box.db)
    assert (box.slot("C") / ".credentials.json").read_bytes() == CRED["C"] and UC in box.kv("accounts")
    assert (box.slot("A") / ".credentials.json").read_bytes() == CRED["A"]


def test_tick_never_raises_and_logs_class_names_only(box, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("SECRET-A inside a message")
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(account_store, "finalize", boom)
    monkeypatch.setattr(account_store, "_repair", boom)
    monkeypatch.setattr(account_store, "_sync", boom)
    assert account_store.tick(box.db) is None
    assert "RuntimeError" in caplog.text and "SECRET" not in caplog.text


def test_the_tick_is_registered_next_to_observe():
    from app import samples
    assert account_store.tick in samples.TICK_HOOKS and samples.TICK_HOOKS.index(account_store.tick) > samples.TICK_HOOKS.index(accounts.observe)


# ---------------------------------------------------------------- adding a login

def test_start_login_runs_in_pending_with_the_quoted_email(box, fake_tmux):
    account_store.start_login(box.db, "me+tag@example.org")
    pend = account_store.pending_dir()
    assert pend.is_dir() and mode(pend) == 0o700 and mode(box.store) == 0o700 and not list(pend.iterdir())
    name, cwd, env = fake_tmux["created"][-1]
    assert name == tmux.LOGIN_SESSION and env == {"BROWSER": browser_stub(), "CLAUDE_CONFIG_DIR": str(pend)}
    assert fake_tmux["sent"][-1] == (tmux.LOGIN_SESSION, "claude auth login --email me+tag@example.org")
    v = account_store.login_view()
    assert v["adding"] is True and v["email"] == "me+tag@example.org" and v["started_at"] and v["result"] is None


def test_the_email_is_shell_quoted_because_the_line_goes_to_a_login_shell(box, fake_tmux):
    for email, typed in (("$(id)@x.example", "'$(id)@x.example'"), ("a;b@x.example", "'a;b@x.example'"), ("`id`@x.example", "'`id`@x.example'"),
                         ("a&b|c@x.example", "'a&b|c@x.example'"), ("a b".replace(" ", "%") + "@x.example", "a%b@x.example")):
        account_store.cancel_login()
        account_store.start_login(box.db, email)
        assert fake_tmux["sent"][-1][1] == f"claude auth login --email {typed}"


def test_start_login_without_an_email_types_the_plain_command_and_clears_pending(box, fake_tmux):
    p = account_store.pending_dir()
    p.mkdir(parents=True)
    (p / "left-over").write_text("x")
    account_store.start_login(box.db)
    assert fake_tmux["sent"][-1] == (tmux.LOGIN_SESSION, "claude auth login") and not list(p.iterdir()), ".pending is recreated empty"
    account_store.start_login(box.db, "  ", restart=True)
    assert fake_tmux["sent"][-1][1] == "claude auth login"


@pytest.mark.parametrize("bad", ["a b@x.example", "x@y@z.example", "a'b@x.example", 'a"b@x.example', "a\\b@x.example", "a\n@x.example",
                                 "x\x03@y.example", "x\x1b[A@y.example", "@x.example", "a@", "nodomain", "a" * 121 + "@x.example",
                                 "a@" + "b" * 201, "a\tb@x.example"])
def test_hostile_emails_are_rejected_before_anything_starts(box, fake_tmux, bad):
    with pytest.raises(ValueError):
        account_store.start_login(box.db, bad)
    assert not fake_tmux["created"] and not fake_tmux["sent"] and not account_store.pending_dir().exists()
    with pytest.raises(ValueError):
        claude_auth.start_login(email=bad)
    assert not fake_tmux["created"]


def test_the_plain_login_for_the_live_config_dir_is_byte_identical(box, fake_tmux):
    claude_auth.start_login()
    assert fake_tmux["created"][-1] == (tmux.LOGIN_SESSION, str(settings.claude_config_dir.parent), {"BROWSER": browser_stub()})
    assert fake_tmux["sent"][-1] == (tmux.LOGIN_SESSION, "claude auth login")


def test_one_login_at_a_time_unless_restarted(box, fake_tmux):
    account_store.start_login(box.db)
    with pytest.raises(account_store.Busy):
        account_store.start_login(box.db)
    account_store.pending_dir().joinpath("stale").write_text("x")
    account_store.start_login(box.db, restart=True)
    assert not (account_store.pending_dir() / "stale").exists()


def test_a_login_that_cannot_start_leaves_no_pending_dir(box, fake_tmux, monkeypatch):
    monkeypatch.setattr(settings, "claude_bin", lambda: None)
    with pytest.raises(tmux.TmuxError):
        account_store.start_login(box.db)
    assert not account_store.pending_dir().exists() and account_store.login_view()["adding"] is False


def test_finalize_saves_a_new_account_without_touching_the_live_login(box, fake_tmux):
    box.onboard("A")
    account_store.start_login(box.db)
    box.pending("C")
    live_before = (box.creds.read_bytes(), box.state.read_bytes(), box.kv("account_current"))
    res = account_store.finalize(box.db)
    assert res["ok"] is True and res["key"] == UC and res["name"] == "Cy" and res["live"] is False and res["at"]
    s = box.slot("C")
    assert (s / ".credentials.json").read_bytes() == CRED["C"] and mode(s / ".credentials.json") == 0o600 and mode(s) == 0o700
    assert json.loads((s / ".claude.json").read_text())["oauthAccount"] == oauth("C"), "Claude's own file moved in"
    assert not account_store.pending_dir().exists() and tmux.LOGIN_SESSION not in fake_tmux["sessions"], "the login session is closed"
    assert (box.creds.read_bytes(), box.state.read_bytes(), box.kv("account_current")) == live_before
    assert box.kv("accounts")[UC]["email"] == "c@example.com" and box.kv("accounts")[UC]["label"] is None
    assert [k for _a, k, _v, _m in box.db.samples_query("acct", None, iso(0))] == [UA], "no acct event for an account that is not live"
    assert account_store.has_saved(UC)
    v = account_store.login_view()
    assert v["adding"] is False and v["result"] == res
    assert account_store.finalize(box.db) is None, "decided once"


def test_finalize_puts_the_login_in_place_when_nobody_is_logged_in(box, fake_tmux):
    account_store.start_login(box.db)
    box.pending("C")
    res = account_store.finalize(box.db)
    assert res["ok"] and res["live"] is True
    assert box.creds.read_bytes() == CRED["C"] and box.live_oauth() == oauth("C") and accounts.current(box.db) == UC
    assert [k for _a, k, _v, _m in box.db.samples_query("acct", None, iso(0))] == [UC]


def test_finalize_of_the_live_account_itself_puts_the_new_login_in_place(box, fake_tmux):
    box.onboard("A")
    account_store.start_login(box.db, "a@example.com")
    p = account_store.pending_dir()
    put_creds(p, CRED["A2"])
    put_state(p / ".claude.json", "A")
    res = account_store.finalize(box.db)
    assert res["live"] is True and res["key"] == UA
    assert box.creds.read_bytes() == CRED["A2"], "live runs on the fresh login"
    s = box.slot("A")
    assert (s / ".credentials.json").read_bytes() == CRED["A2"] and (s / ".credentials.json.prev").read_bytes() == CRED["A"]
    assert accounts.held() is None, "no hold: the account did not change"


def test_finalize_replaces_the_saved_login_of_the_same_account_and_rotates_the_old_one(box, fake_tmux):
    two_accounts(box)
    account_store.start_login(box.db)
    p = account_store.pending_dir()
    put_creds(p, CRED["A2"])
    put_state(p / ".claude.json", "A")
    res = account_store.finalize(box.db)
    assert res["key"] == UA and res["live"] is False
    s = box.slot("A")
    assert (s / ".credentials.json").read_bytes() == CRED["A2"] and (s / ".credentials.json.prev").read_bytes() == CRED["A"]
    assert box.creds.read_bytes() == CRED["B"], "B stays live"


def test_finalize_waits_for_credentials_that_are_old_enough_and_an_identity(box, fake_tmux):
    account_store.start_login(box.db)
    p = box.pending("C", age=0.0)                               # credentials written just now
    assert account_store.finalize(box.db) is None and p.exists()
    t = time.time() - 100
    os.utime(p / ".credentials.json", (t, t))
    (p / ".claude.json").unlink()                               # credentials but no identity yet (/login writes the identity second)
    put_state(p / ".claude.json", None)
    assert account_store.finalize(box.db) is None
    put_state(p / ".claude.json", "C")
    assert account_store.finalize(box.db)["key"] == UC


def test_a_login_session_that_ended_without_credentials_reports_the_error(box, fake_tmux):
    account_store.start_login(box.db)
    t = time.time()
    assert account_store.finalize(box.db, now=t) is None, "still running"
    fake_tmux["sessions"][tmux.LOGIN_SESSION]["command"] = "zsh"            # the pane is back at the shell
    assert account_store.finalize(box.db, now=t) is None, "first sighting: the 20 s start now"
    assert account_store.finalize(box.db, now=t + 15) is None
    res = account_store.finalize(box.db, now=t + 21)
    assert res == {"ok": False, "error": "the login did not complete", "at": iso(t + 21)}
    assert not account_store.pending_dir().exists() and tmux.LOGIN_SESSION not in fake_tmux["sessions"]
    v = account_store.login_view()
    assert v["adding"] is False and v["result"] == res and v["email"] is None and v["started_at"] is None


def test_a_session_that_comes_back_to_life_resets_the_dead_clock(box, fake_tmux):
    account_store.start_login(box.db)
    t = time.time()
    fake_tmux["sessions"][tmux.LOGIN_SESSION]["command"] = "zsh"
    account_store.finalize(box.db, now=t)
    fake_tmux["sessions"][tmux.LOGIN_SESSION]["command"] = "claude"
    assert account_store.finalize(box.db, now=t + 10) is None
    fake_tmux["sessions"][tmux.LOGIN_SESSION]["command"] = "zsh"
    assert account_store.finalize(box.db, now=t + 25) is None, "the clock restarted at the new sighting"


def test_a_pending_login_older_than_an_hour_is_abandoned(box, fake_tmux):
    account_store.start_login(box.db)
    t = time.time()
    assert account_store.finalize(box.db, now=t + 3500) is None
    res = account_store.finalize(box.db, now=t + 3601)
    assert res["ok"] is False and res["error"] == "the login did not complete"
    assert not account_store.pending_dir().exists() and tmux.LOGIN_SESSION not in fake_tmux["sessions"]


def test_a_pending_dir_left_by_a_restart_is_tidied_up_silently(box, fake_tmux):
    p = account_store.pending_dir()
    p.mkdir(parents=True)
    old = time.time() - 600
    os.utime(p, (old, old))
    assert account_store.finalize(box.db) is None and not p.exists() and account_store.login_view()["result"] is None
    # a login session that is still running (tmux outlives the board) keeps its pending dir
    p.mkdir(parents=True)
    os.utime(p, (old, old))
    fake_tmux["sessions"][tmux.LOGIN_SESSION] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "claude", "path": "/", "pid": 1, "env": {}}
    assert account_store.finalize(box.db) is None and p.exists()


def test_a_finished_login_survives_a_board_restart(box, fake_tmux):
    """The module state is gone after a restart, the pending dir is not: a login that completed meanwhile is still taken."""
    box.onboard("A")
    box.pending("C")
    assert account_store.login_view()["adding"] is False
    assert account_store.finalize(box.db)["key"] == UC


def test_the_result_reads_none_after_ten_minutes(box, fake_tmux, monkeypatch):
    account_store.start_login(box.db)
    t = time.time()
    fake_tmux["sessions"][tmux.LOGIN_SESSION]["command"] = "zsh"
    account_store.finalize(box.db, now=t)
    account_store.finalize(box.db, now=t + 30)
    assert account_store.login_view()["result"]["ok"] is False
    monkeypatch.setattr(account_store, "_wall", lambda: t + account_store.RESULT_TTL + 1)
    assert account_store.login_view()["result"] is None


def test_cancel_kills_the_session_removes_pending_and_clears_everything(box, fake_tmux):
    account_store.start_login(box.db, "a@example.com")
    box.pending("C")
    account_store.cancel_login()
    assert tmux.LOGIN_SESSION not in fake_tmux["sessions"] and not account_store.pending_dir().exists()
    assert account_store.login_view() == {"adding": False, "email": None, "started_at": None, "result": None}


def test_cancel_leaves_a_plain_login_session_alone(box, fake_tmux):
    claude_auth.start_login()                                    # POST /api/claude/login: the live config dir, not ours
    account_store.cancel_login()
    assert tmux.LOGIN_SESSION in fake_tmux["sessions"]


def test_finalize_soon_polls_until_the_login_is_decided(box, fake_tmux, monkeypatch):
    monkeypatch.setattr(account_store, "WATCH_EVERY", 0.01)
    box.onboard("A")
    account_store.start_login(box.db)
    th = account_store.finalize_soon(box.db)
    time.sleep(0.05)
    assert th.is_alive(), "nothing to take yet"
    box.pending("C")
    th.join(5)
    assert not th.is_alive() and account_store.login_view()["result"]["key"] == UC


def test_finalize_soon_stops_when_there_is_nothing_pending_and_on_stop(box, fake_tmux, monkeypatch):
    monkeypatch.setattr(account_store, "WATCH_EVERY", 0.01)
    account_store.finalize_soon(box.db).join(5)
    th = account_store.finalize_soon(box.db)
    th.join(5)
    assert not th.is_alive()
    account_store.pending_dir().mkdir(parents=True)
    th = account_store.finalize_soon(box.db)
    account_store.stop_watchers()
    assert not th.is_alive()


# ---------------------------------------------------------------- the view

def test_decorate_adds_saved_per_row_and_the_store_summary(box):
    box.onboard("A")
    accounts.remember(box.db, {"key": UC, "email": "c@example.com", "name": "Cy", "config_dir": None}, T0)
    v = accounts.view(box.db)
    d = account_store.decorate(v)
    assert {r["key"]: r["saved"] for r in d["list"]} == {UA: True, UC: False}
    assert d["store"] == {"supported": True, "reason": None, "count": 1} and d["current"] == UA
    assert "saved" not in v["list"][0] and "store" not in v, "the input is not modified"
    assert account_store.decorate({"current": None, "list": []})["store"]["count"] == 0


# ---------------------------------------------------------------- secrecy

def test_the_credentials_never_appear_in_any_answer_log_event_or_kv_value(box, fake_tmux, lite_client, caplog):
    from app import main
    box.db = main.db
    caplog.set_level(logging.DEBUG)
    box.onboard("A")
    account_store.start_login(box.db)
    box.pending("B")
    assert lite_client.post("/api/accounts/login/code", headers=H, json={"code": "abc#def"}).status_code == 200
    deadline = time.time() + 5
    while time.time() < deadline and not account_store.login_view()["result"]:
        time.sleep(0.02)
    assert account_store.login_view()["result"]["ok"] is True
    seen = [lite_client.get("/api/accounts", headers=H).text, lite_client.get("/api/state", headers=H).text]
    r = lite_client.post(f"/api/accounts/{UB}/switch", headers=H, json={"continue_parked": True})
    assert r.status_code == 200 and r.json()["to"] == UB
    seen += [r.text, lite_client.post(f"/api/accounts/{UA}/switch", headers=H, json={}).text,
             lite_client.delete(f"/api/accounts/{UB}/saved", headers=H).text, lite_client.delete(f"/api/accounts/{UB}/saved", headers=H).text,
             lite_client.post(f"/api/accounts/{UB}/switch", headers=H, json={}).text]
    account_store.tick(box.db)
    dump = []
    for (name,) in box.db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        dump += [repr(tuple(row)) for row in box.db.conn.execute(f'SELECT * FROM "{name}"').fetchall()]
    blob = "\n".join(seen + dump + [caplog.text])
    assert "SECRET" not in blob, [ln for ln in blob.splitlines() if "SECRET" in ln][:3]
    assert "switched account" in caplog.text, "the audit line is written, with names only"
    # the one place the secrets are is the credentials files
    assert b"SECRET-A" in (box.slot("A") / ".credentials.json").read_bytes()


def _functions(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def test_no_json_load_is_ever_applied_to_a_credentials_path():
    tree = ast.parse(inspect.getsource(account_store))
    forbidden_names = {"CREDS", "PREV", "_live_creds", "_slot_creds", "_read_stable", "_store_creds"}
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
        assert not (names & forbidden_names), f"{fn.name} parses JSON and touches a credentials path"
        assert not any("credentials" in s.lower() for s in strings), f"{fn.name} parses JSON next to a credentials string"
    assert checked >= 1, "the check found nothing to check: the json-reading helper moved"


def test_the_module_never_runs_a_process_and_never_names_the_claude_binary():
    src = inspect.getsource(account_store)
    tree = ast.parse(src)
    imports = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imports |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not ({"subprocess", "pty", "shlex"} & imports) and "claude_bin" not in src and "os.system" not in src and "Popen" not in src


def test_the_credentials_bytes_helper_mentions_no_json():
    for name in ("_write_atomic", "_read_stable", "_store_creds", "_read_plain", "_snapshot", "_restore"):
        assert "json" not in inspect.getsource(getattr(account_store, name)).lower(), name


def test_every_file_the_store_writes_is_private(box, fake_tmux):
    two_accounts(box)
    account_store.start_login(box.db)
    box.pending("C")
    account_store.finalize(box.db)
    account_store.switch(box.db, UA)
    for p in box.store.rglob("*"):
        assert mode(p) == (0o700 if p.is_dir() else 0o600), p


# ---------------------------------------------------------------- v0.5.17g: logging a saved account in again

FRESH_A = b'{"claudeAiOauth":{"accessToken":"SECRET-A-fresh"}}'


def relogin(box, who="A", asked="a@example.com", creds=None):
    """What `Log in again` does: the add-account login for the account, finished with the given credentials and the identity of `who`."""
    account_store.start_login(box.db, asked)
    p = account_store.pending_dir()
    put_creds(p, creds or CRED.get(who + "2", CRED[who]))
    put_state(p / ".claude.json", who)
    return account_store.finalize(box.db)


def test_logging_a_saved_account_in_again_lands_in_its_own_slot_and_makes_no_second_row(box, fake_tmux):
    two_accounts(box)                                           # A saved, B live
    rows_before = set(box.kv("accounts"))
    res = relogin(box, "A")
    assert res["ok"] is True and res["key"] == UA and res["replaced"] is True and res["live"] is False and "different_account" not in res
    assert set(box.kv("accounts")) == rows_before == {UA, UB}, "a re-login is not a new account"
    s = box.slot("A")
    assert (s / ".credentials.json").read_bytes() == CRED["A2"] and (s / ".credentials.json.prev").read_bytes() == CRED["A"]
    assert box.creds.read_bytes() == CRED["B"] and accounts.current(box.db) == UB, "B stays live"
    assert [d.name for d in box.store.iterdir() if d.is_dir() and d.name != ".pending"].__len__() == 2


def test_logging_the_live_account_in_again_puts_the_fresh_login_live_and_keeps_the_dead_one_as_prev_not_in_the_slot(box, fake_tmux):
    box.onboard("A")
    put_creds(box.state.parent, CRED["A2"], age=100.0)           # the live file moved on (a refresh the board has not saved): this is the dead one
    accounts.invalidate()
    account_store.start_login(box.db, "a@example.com")
    p = account_store.pending_dir()
    put_creds(p, FRESH_A)
    put_state(p / ".claude.json", "A")
    res = account_store.finalize(box.db)
    assert res["ok"] and res["live"] is True and res["replaced"] is True and res["key"] == UA
    assert box.creds.read_bytes() == FRESH_A, "the live login is the fresh one"
    s = box.slot("A")
    assert (s / ".credentials.json").read_bytes() == FRESH_A, "the dead live bytes did not overwrite the fresh slot"
    assert (s / ".credentials.json.prev").read_bytes() == CRED["A2"], "the generation before it is the one that was live"
    assert box.live_oauth() == oauth("A") and accounts.current(box.db) == UA and accounts.held() is None
    account_store.tick(box.db, T0 + 500)                         # the tick keeps the live account's copy current: it must not undo the fresh login
    assert (s / ".credentials.json").read_bytes() == FRESH_A and box.creds.read_bytes() == FRESH_A
    for q in box.store.rglob("*"):
        assert mode(q) == (0o700 if q.is_dir() else 0o600), q


def test_a_login_as_somebody_else_is_saved_as_its_own_row_and_the_result_says_so(box, fake_tmux):
    box.onboard("A")
    account_store.start_login(box.db, "a@example.com")
    p = account_store.pending_dir()
    put_creds(p, CRED["C"])
    put_state(p / ".claude.json", "C")
    live_before = box.creds.read_bytes()
    res = account_store.finalize(box.db)
    assert res["ok"] and res["key"] == UC and res["different_account"] == "c@example.com" and res["live"] is False and "replaced" not in res
    assert box.creds.read_bytes() == live_before and account_store.has_saved(UC) and (box.slot("A") / ".credentials.json").read_bytes() == CRED["A"]
    assert account_store.login_view()["result"] == res


def test_the_asked_email_is_compared_without_case_and_no_email_asks_nothing(box, fake_tmux):
    box.onboard("A")
    res = relogin(box, "A", asked="A@Example.COM")
    assert "different_account" not in res
    account_store.start_login(box.db)                            # no email asked: nothing to compare with
    p = account_store.pending_dir()
    put_creds(p, CRED["C"])
    put_state(p / ".claude.json", "C")
    assert "different_account" not in account_store.finalize(box.db)


def test_saved_at_is_the_age_of_the_saved_copy_and_a_relogin_renews_it(box, fake_tmux):
    from datetime import datetime

    def age(text):
        return time.time() - datetime.fromisoformat(text).timestamp()
    two_accounts(box)                                           # both credentials files were 100 s old when they were copied
    v = account_store.decorate(accounts.view(box.db, full=True), box.db)
    rows = {r["key"]: r for r in v["list"]}
    for k in (UA, UB):
        assert rows[k]["saved"] is True and 90 < age(rows[k]["saved_at"]) < 400, "the copy is as old as the file it was taken from"
    res = relogin(box, "A")
    assert res["replaced"] is True
    v = account_store.decorate(accounts.view(box.db, full=True), box.db)
    assert age({r["key"]: r for r in v["list"]}[UA]["saved_at"]) < 30, "a login written just now reads as just now"
    # without the kv (a caller that has no db) the slot file's own mtime answers
    bare = {r["key"]: r for r in account_store.decorate(accounts.view(box.db, full=True))["list"]}
    assert bare[UA]["saved_at"] and bare[UB]["saved_at"] and account_store.decorate(accounts.view(box.db, full=True))["problem"] is None
    box.db.kv_set("accounts", {**box.kv("accounts"), "x": {"key": "x", "email": None, "label": None}})
    assert {r["key"]: r for r in account_store.decorate(accounts.view(box.db, full=True), box.db)["list"]}["x"]["saved_at"] is None, "no saved login, no age"


def test_a_finished_login_for_the_account_clears_its_login_problem_and_others_leave_it(box, fake_tmux):
    two_accounts(box)
    login_problem.raise_(box.db, agent="claude", account=UC, session="shop--api--s1", message="Invalid API key")
    assert relogin(box, "A")["ok"] and login_problem.get(box.db)["account"] == UC, "another account's login finishing says nothing about C"
    login_problem.raise_(box.db, agent="claude", account=UA, session="shop--api--s1", message="Invalid API key")
    assert relogin(box, "A")["ok"] and login_problem.get(box.db) is None


def test_the_problem_view_names_the_account_and_offers_switch_back_only_right_after_a_switch(box, fake_tmux):
    two_accounts(box)                                           # B live
    account_store.switch(box.db, UA, now=T0 + 10)               # ... then A, which turns out to be dead
    login_problem.raise_(box.db, agent="claude", account=UA, session="shop--api--s1", message="Invalid API key · Please run /login", now=T0 + 100)

    def view():
        return account_store.decorate(accounts.view(box.db, full=True), box.db)["problem"]
    p = view()
    assert p == {"at": iso(T0 + 100), "agent": "claude", "account": UA, "session": "shop--api--s1", "message": "Invalid API key · Please run /login",
                 "label": "Ann", "back": {"key": UB, "label": "Bob"}}
    login_problem.raise_(box.db, agent="claude", account=UA, session="s", message="x", now=T0 + 10 + login_problem.SWITCH_WINDOW + 1)
    assert view()["back"] is None, "a failure long after the switch is not blamed on it"
    login_problem.raise_(box.db, agent="claude", account=UB, session="s", message="x", now=T0 + 100)
    assert view()["back"] is None and view()["label"] == "Bob", "the failing account is not the live one"
    login_problem.raise_(box.db, agent="claude", account=UA, session="s", message="x", now=T0 + 100)
    account_store.forget(box.db, UB)
    assert view()["back"] is None, "nothing saved to switch back to"
    login_problem.raise_(box.db, agent="claude", account=None, session="s", message="x")
    assert view()["label"] is None and view()["account"] is None


# ---------------------------------------------------------------- the API

@pytest.fixture
def api(box, lite_client, fake_tmux):
    from app import main
    box.db = main.db
    box.tmux = fake_tmux
    return lite_client


def post(api, path, **kw):
    return api.post(path, headers=H, **kw)


def test_get_accounts_carries_saved_store_and_the_login_view(box, api):
    box.onboard("A")
    body = api.get("/api/accounts", headers=H).json()
    assert body["current"] == UA and body["list"][0]["saved"] is True and body["list"][0]["email"] == "a@example.com"
    assert body["store"] == {"supported": True, "reason": None, "count": 1}
    assert body["login"] == {"running": False, "url": None, "tail": [], "adding": False, "email": None, "started_at": None, "result": None}
    assert api.get("/api/accounts").status_code == 403


def test_state_carries_the_same_keys_and_no_new_top_level_key(box, api):
    box.onboard("A")
    st = api.get("/api/state", headers=H).json()
    assert st["accounts"]["list"][0]["saved"] is True and st["accounts"]["store"]["count"] == 1
    assert set(st["login"]) == {"running", "url", "tail", "adding", "email", "started_at", "result"}
    assert "store" not in st and "saved" not in st, "no new top-level key"


def test_unsupported_platform_answers_409_with_the_reason_and_says_so_in_state(box, api, monkeypatch):
    box.onboard("A")
    monkeypatch.setattr(account_store, "supported", lambda: False)
    r = post(api, "/api/accounts/login", json={})
    assert r.status_code == 409 and r.json()["detail"] == account_store.REASON == r.json()["error"]
    for path in (f"/api/accounts/{UA}/switch",):
        r = post(api, path, json={})
        assert r.status_code == 409 and r.json()["detail"] == account_store.REASON
    r = api.delete(f"/api/accounts/{UA}/saved", headers=H)
    assert r.status_code == 409 and r.json()["detail"] == account_store.REASON
    st = api.get("/api/state", headers=H).json()
    assert st["accounts"]["store"] == {"supported": False, "reason": account_store.REASON, "count": 0}
    assert api.delete("/api/accounts/login", headers=H).json() == {"ok": True}


def test_login_endpoint_status_codes(box, api):
    assert post(api, "/api/accounts/login", json={"email": "me@example.com"}).status_code == 202
    r = api.get("/api/accounts", headers=H).json()["login"]
    assert r["adding"] is True and r["email"] == "me@example.com" and r["running"] is True
    assert box.tmux["sent"][-1][1] == "claude auth login --email me@example.com"
    r = post(api, "/api/accounts/login", json={})
    assert r.status_code == 409 and "already running" in r.json()["detail"]
    r = post(api, "/api/accounts/login", json={"restart": True, "email": None})
    assert r.status_code == 202 and r.json() == {"ok": True} and box.tmux["sent"][-1][1] == "claude auth login"
    assert api.delete("/api/accounts/login", headers=H).json() == {"ok": True}
    assert tmux.LOGIN_SESSION not in box.tmux["sessions"]
    for bad in ("a b@c.example", "x@y@z.example", "a'b@c.example", "nodomain"):
        r = post(api, "/api/accounts/login", json={"email": bad})
        assert r.status_code == 400 and r.json()["detail"], bad
    assert post(api, "/api/accounts/login").status_code == 202, "the body is optional"


def test_login_code_endpoint_validates_like_the_plain_login_and_finalizes_without_waiting_for_the_tick(box, api, monkeypatch):
    monkeypatch.setattr(account_store, "WATCH_EVERY", 0.02)
    r = post(api, "/api/accounts/login/code", json={"code": "nohash"})
    plain = post(api, "/api/claude/login/code", json={"code": "nohash"})
    assert r.status_code == plain.status_code == 400 and r.json()["error"] == plain.json()["error"]
    r = post(api, "/api/accounts/login/code", json={"code": "abc#def"})
    plain = post(api, "/api/claude/login/code", json={"code": "abc#def"})
    assert r.status_code == plain.status_code == 409 and r.json()["error"] == plain.json()["error"], "no login in progress"
    box.onboard("A")
    post(api, "/api/accounts/login", json={})
    box.pending("B")
    assert post(api, "/api/accounts/login/code", json={"code": "abc#def"}).json() == {"ok": True}
    assert box.tmux["sent"][-1] == (tmux.LOGIN_SESSION, "abc#def")
    deadline = time.time() + 5
    while time.time() < deadline and not account_store.login_view()["result"]:
        time.sleep(0.02)
    login = api.get("/api/accounts", headers=H).json()["login"]
    assert login["result"]["ok"] is True and login["result"]["key"] == UB and login["adding"] is False
    assert api.get("/api/accounts", headers=H).json()["list"][1]["saved"] is True


def test_switch_endpoint(box, api):
    two_accounts(box)
    r = post(api, f"/api/accounts/{UA}/switch", json={})
    body = r.json()
    assert r.status_code == 200 and (body["ok"], body["already"], body["from"], body["to"], body["continued"]) == (True, False, UB, UA, [])
    assert body["accounts"]["current"] == UA and body["accounts"]["store"]["count"] == 2 and all(x["saved"] for x in body["accounts"]["list"])
    assert box.creds.read_bytes() == CRED["A"]
    r = post(api, f"/api/accounts/{UA}/switch")                  # no body at all
    assert r.status_code == 200 and r.json()["already"] is True and r.json()["continued"] == []
    r = post(api, "/api/accounts/nope/switch", json={})
    assert r.status_code == 404 and r.json()["detail"] == "no such account"
    accounts.remember(box.db, {"key": UC, "email": "c@example.com", "name": "Cy", "config_dir": None}, T0)
    r = post(api, f"/api/accounts/{UC}/switch", json={})
    assert r.status_code == 409 and "no saved login" in r.json()["detail"] and box.creds.read_bytes() == CRED["A"]
    post(api, "/api/accounts/login", json={})
    r = post(api, f"/api/accounts/{UB}/switch", json={})
    assert r.status_code == 409 and "login is in progress" in r.json()["detail"]


def test_switch_endpoint_409_when_the_current_login_cannot_be_saved(box, api, monkeypatch):
    two_accounts(box)
    monkeypatch.setattr(account_store, "_save_live", lambda *a, **k: None)
    r = post(api, f"/api/accounts/{UA}/switch", json={})
    assert r.status_code == 409 and r.json()["detail"] == "could not save the current login first; nothing was changed"


def park(db, name, hit_at, resets_at):
    msg = "You've hit your session limit · resets 10:05pm (Asia/Kathmandu)"
    db.add_session(tmux_name=name, project="shop", repo="api", name=name.split("--")[-1], launcher="claude")
    db.set_state(name, "errored", "StopFailure", message=msg)
    db.conn.execute("UPDATE sessions SET state_at=? WHERE tmux_name=?", (iso(hit_at), name))
    db.sample("lim", "5h", 1.0, {"session": name, "resets_at": resets_at, "message": msg}, at=iso(hit_at))


def test_switch_with_continue_parked_types_continue_after_a_real_switch_only(box, api):
    two_accounts(box)
    now = time.time()
    park(box.db, "shop--api--s1", now - 3600, now + 7200)
    box.tmux["sessions"]["shop--api--s1"] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%2", "command": "claude", "path": "/", "pid": 2, "env": {}}
    r = post(api, f"/api/accounts/{UB}/switch", json={"continue_parked": True})
    assert r.json()["already"] is True and r.json()["continued"] == [] and not box.tmux["texts"], "an `already` switch continues nothing"
    r = post(api, f"/api/accounts/{UA}/switch", json={"continue_parked": False})
    assert r.json()["continued"] == [] and not box.tmux["texts"]
    r = post(api, f"/api/accounts/{UB}/switch", json={"continue_parked": True})
    assert r.json()["continued"] == ["shop--api--s1"] and box.tmux["texts"] == [("shop--api--s1", "continue", True)]
    kv = box.db.kv_get(f"autoresume:shop--api--s1:{int(now + 7200)}")["value"]
    assert kv["continued"] is True and kv["switch"] is True and kv["kind"] == "5h"


def test_forget_endpoint(box, api):
    two_accounts(box)
    r = api.delete(f"/api/accounts/{UB}/saved", headers=H)
    assert r.status_code == 409 and "live login" in r.json()["detail"] and account_store.has_saved(UB)
    r = api.delete(f"/api/accounts/{UA}/saved", headers=H)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["forgotten"] is True
    assert {x["key"]: x["saved"] for x in r.json()["accounts"]["list"]} == {UA: False, UB: True}
    assert api.delete("/api/accounts/nope/saved", headers=H).status_code == 404


def test_every_new_non_get_endpoint_needs_the_csrf_header_and_the_allowlist(box, api):
    two_accounts(box)
    no_csrf = {"Tailscale-User-Login": "alice@example.com"}
    other = {"Tailscale-User-Login": "mallory@example.com", "X-CCBoard": "1"}
    for method, path in (("post", "/api/accounts/login"), ("post", "/api/accounts/login/code"), ("delete", "/api/accounts/login"),
                         ("post", f"/api/accounts/{UA}/switch"), ("delete", f"/api/accounts/{UA}/saved")):
        for h in (no_csrf, other, {}):
            assert getattr(api, method)(path, headers=h).status_code == 403, (method, path, h)
    assert box.creds.read_bytes() == CRED["B"] and account_store.has_saved(UA)


def test_the_demo_state_carries_the_new_keys():
    demo = json.loads((Path(__file__).resolve().parent.parent / "app" / "static" / "demo" / "state.json").read_text(encoding="utf-8"))
    assert all(r["saved"] is True for r in demo["accounts"]["list"]) and len(demo["accounts"]["list"]) == 2
    assert demo["accounts"]["store"] == {"supported": True, "reason": None, "count": 2}
    assert {"adding", "email", "started_at", "result"} <= set(demo["login"]) and demo["login"]["adding"] is False


def test_the_lifespan_seeds_the_live_login(box, fake_tmux):
    """The full lifespan (workers and all) starts with a login live: its slot exists by the time the board serves its first request."""
    from fastapi.testclient import TestClient
    from app import main
    box.log_in("A")
    assert not account_store.has_saved(UA)
    with TestClient(main.app) as c:
        assert account_store.has_saved(UA) and (box.slot("A") / ".credentials.json").read_bytes() == CRED["A"]
        assert UA in c.get("/api/accounts", headers=H).json()["list"][0]["key"]
    account_store.stop_watchers()


def test_a_failing_seed_is_only_a_warning_at_startup(box, fake_tmux, monkeypatch, caplog):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(account_store, "seed_current", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with TestClient(main.app) as c:
        assert c.get("/api/state", headers=H).status_code == 200
    assert "saved-login seed failed: RuntimeError" in caplog.text


def test_an_unsupported_platform_starts_quietly(box, fake_tmux, monkeypatch, caplog):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(account_store, "supported", lambda: False)
    box.log_in("A")
    with TestClient(main.app) as c:
        assert c.get("/api/state", headers=H).json()["accounts"]["store"]["supported"] is False
    assert "seed" not in caplog.text and not box.store.exists()
