"""v0.5.17b, usage per subscription account (backend): app/accounts.py and everything that carries an account key.

Identity (the `oauthAccount` object of Claude's state file, the `claude auth status` fallback, the 60 s cache, never a credential file),
observe() on a switch, the fingerprint rule of for_reading() (rollovers, a stale session, the race after a /login), the per-account
series keys and the stale guard, `acct` in the lim / cost / state samples, sessions.account, the kv shapes, GET /api/accounts, PATCH
/api/accounts/{key}, state.accounts and the additive migration.

Nothing reads the real ~/.claude.json or ~/.claude/.credentials.json: the config dir is a tmp one with a fixture state file, claude
auth status is faked, and the identity cache runs on a fake clock.
"""
import builtins
import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import accounts, claude_auth, cost, db as dbmod, hooks, samples
from app.config import settings
from app.db import DB, SCHEMA, iso

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
UUID_A = "aaaaaaaa-0000-4000-8000-000000000001"
UUID_B = "bbbbbbbb-0000-4000-8000-000000000002"
SECRET = "SECRET-SENTINEL-do-not-leak"
T0 = 1_790_000_000                      # epoch seconds, a fixed "now"
W5, W7 = 18000, 604800
NAME = "shop--api--s1"


_mtime = [time.time_ns()]


def state_file(cfg: Path, uuid=UUID_A, email="a@example.com", name="Ann", **oa) -> None:
    """Claude's state file as the box has it: a big file whose oauthAccount is the only part the board may read. Every write gets
    its own mtime (a coarse filesystem clock can give two quick writes the same one, and the board tells a rewrite by the stamp)."""
    cfg.mkdir(parents=True, exist_ok=True)
    account = {"accountUuid": uuid, "emailAddress": email, "displayName": name, "organizationUuid": "cccccccc-0000-4000-8000-000000000003",
               "organizationName": "Ann's Org", "organizationRole": "admin", "organizationType": "claude_max",
               "billingType": "stripe_subscription", "seatTier": None, "organizationRateLimitTier": "default_claude_max_20x",
               "userRateLimitTier": None, "oauthRefreshToken": SECRET, **oa}
    (cfg / ".claude.json").write_text(json.dumps({"oauthAccount": account, "primaryApiKey": SECRET,
                                                  "projects": {"/x": {"history": ["y" * 500] * 50}}}))
    _mtime[0] += 1_000_000_000
    os.utime(cfg / ".claude.json", ns=(_mtime[0], _mtime[0]))


def rl(r5=None, r7=None, u5=10.0, u7=20.0) -> dict:
    out = {}
    if r5 is not None:
        out["five_hour"] = {"used_percentage": u5, "resets_at": r5}
    if r7 is not None:
        out["seven_day"] = {"used_percentage": u7, "resets_at": r7}
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "claude"
    monkeypatch.setattr(settings, "claude_config_dir", cfg)
    monkeypatch.setattr(claude_auth, "status", lambda: {"installed": True, "loggedIn": False})
    clock = [1000.0]
    monkeypatch.setattr(accounts, "_clock", lambda: clock[0])
    accounts.invalidate()
    e = SimpleNamespace(db=DB(tmp_path / "a.db"), cfg=cfg, clock=clock, tmp=tmp_path)

    def later(seconds):                     # the identity cache's clock
        clock[0] += seconds
    e.later = later
    yield e
    accounts.invalidate()


def kv(db, key):
    r = db.kv_get(key)
    return r["value"] if r else None


def events(db):
    return [(k, v, json.loads(m) if m else None) for _at, k, v, m in db.samples_query("acct", None, "2000-01-01T00:00:00+00:00")]


def series(db, name, key):
    return [(v, json.loads(m) if m else None) for _at, k, v, m in db.samples_query(name, [key], "2000-01-01T00:00:00+00:00")]


def spy_opens(monkeypatch) -> list:
    seen = []
    real = builtins.open

    def spy(file, *a, **k):
        seen.append(str(file))
        return real(file, *a, **k)
    monkeypatch.setattr(builtins, "open", spy)
    return seen


# ================================================================== identity
def test_identity_from_the_state_file_inside_the_config_dir(env):
    state_file(env.cfg)
    ident = accounts.read_identity()
    assert ident == {"key": UUID_A, "email": "a@example.com", "name": "Ann", "org": "Ann's Org",
                     "org_id": "cccccccc-0000-4000-8000-000000000003", "plan": "max", "tier": "default_claude_max_20x",
                     "config_dir": str(env.cfg), "source": "file"}
    assert SECRET not in json.dumps(ident), "only the whitelisted identity fields leave the file"


def test_identity_from_the_file_beside_the_config_dir(env):
    (env.tmp / "claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": UUID_B, "emailAddress": "b@example.com"}}))
    ident = accounts.read_identity()
    assert ident["key"] == UUID_B and ident["email"] == "b@example.com" and ident["plan"] is None and ident["name"] is None


def test_the_default_config_dir_reads_the_dot_json_next_to_it(env, monkeypatch):
    home = env.tmp / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(settings, "claude_config_dir", home / ".claude")
    (home / ".claude").mkdir()
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": UUID_A, "emailAddress": "a@example.com"}}))
    assert accounts.read_identity()["key"] == UUID_A


def test_the_newer_of_two_state_files_wins(env):
    import os
    state_file(env.cfg, uuid=UUID_A, email="a@example.com")                 # <dir>/.claude.json
    (env.tmp / "claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": UUID_B, "emailAddress": "b@example.com"}}))
    os.utime(env.cfg / ".claude.json", (1_000_000, 1_000_000))
    assert accounts.read_identity()["key"] == UUID_B, "<dir>.json is newer"
    accounts.invalidate()
    os.utime(env.tmp / "claude.json", (500_000, 500_000))
    assert accounts.read_identity()["key"] == UUID_A


def test_key_falls_back_from_account_uuid_to_org_to_email(env):
    state_file(env.cfg, uuid=None)
    assert accounts.read_identity(force=True)["key"] == "cccccccc-0000-4000-8000-000000000003"
    accounts.invalidate()
    state_file(env.cfg, uuid=None, organizationUuid=None)
    assert accounts.read_identity()["key"] == "a@example.com"
    accounts.invalidate()
    state_file(env.cfg, uuid=None, organizationUuid=None, emailAddress=None)
    assert accounts.read_identity() is None, "an oauthAccount that names nothing is no identity (and auth says logged out)"


def test_plan_comes_from_seat_tier_then_org_type_then_billing(env):
    state_file(env.cfg, seatTier="team_premium")
    assert accounts.read_identity()["plan"] == "team_premium"
    accounts.invalidate()
    state_file(env.cfg, organizationType="claude_pro")
    assert accounts.read_identity()["plan"] == "pro"
    accounts.invalidate()
    state_file(env.cfg, organizationType=None)
    assert accounts.read_identity()["plan"] == "stripe_subscription"
    accounts.invalidate()
    state_file(env.cfg, userRateLimitTier="default_claude_max_5x")
    assert accounts.read_identity()["tier"] == "default_claude_max_5x", "the user's tier wins over the organization's"


def test_a_garbled_or_oversized_state_file_never_raises_and_keeps_the_last_answer(env, monkeypatch):
    calls = []
    monkeypatch.setattr(claude_auth, "status", lambda: calls.append(1) or {"loggedIn": True, "email": "other@example.com"})
    (env.cfg).mkdir(parents=True)
    (env.cfg / ".claude.json").write_text('{"oauthAccount": {"accountUuid": "x"')            # half written
    assert accounts.read_identity() is None and calls == [], "no fallback: it would key the same account differently"
    state_file(env.cfg)
    env.later(10)
    assert accounts.read_identity(force=True)["key"] == UUID_A
    (env.cfg / ".claude.json").write_text("not json at all \x00\xff")
    env.later(10)
    assert accounts.read_identity(force=True)["key"] == UUID_A, "a half-written file does not read as logged out"
    (env.cfg / ".claude.json").write_bytes(b"\xff\xfe\x00bad utf8")
    env.later(10)
    assert accounts.read_identity(force=True)["key"] == UUID_A and calls == []
    monkeypatch.setattr(accounts, "MAX_FILE_BYTES", 10)
    env.later(10)
    assert accounts.read_identity(force=True)["key"] == UUID_A, "an oversized file is ignored the same way"


def test_a_pathologically_nested_state_file_is_a_bad_file_not_a_logout(env, monkeypatch, caplog):
    """json.loads raises RecursionError on a deeply nested document: that must read as 'bad' (keep the last answer, stamp the cache)
    like any other unreadable file, not escape as a failure that forgets who is logged in and logs a warning on every call."""
    calls = []
    monkeypatch.setattr(claude_auth, "status", lambda: calls.append(1) or {"loggedIn": True, "email": "other@example.com"})
    state_file(env.cfg)
    assert accounts.read_identity()["key"] == UUID_A
    (env.cfg / ".claude.json").write_text("[" * 200_000)
    env.later(61)
    with caplog.at_level("WARNING", logger="ccboard.accounts"):
        assert accounts.read_identity()["key"] == UUID_A, "the last answer survives"
        assert accounts.read_identity(force=True)["key"] == UUID_A
    assert not [r for r in caplog.records if "failed" in r.getMessage()], "no 'read_identity failed' warning: it took the bad-file path"
    assert calls == [], "and no fallback to the auth status, which would key the same account differently"
    opens = spy_opens(monkeypatch)
    env.later(30)
    assert accounts.read_identity()["key"] == UUID_A
    assert not [p for p in opens if p.endswith(".claude.json")], "the cache stamp was refreshed: the bomb is not parsed again inside the 60 s"
    assert accounts.observe(env.db, T0) == UUID_A, "and the sampler tick still sees the account"
    state_file(env.cfg, uuid=UUID_B, email="b@example.com")                 # the file is repaired by a real /login
    assert accounts.read_identity(force=True)["key"] == UUID_B


def test_a_directory_instead_of_the_file_or_a_list_is_no_identity(env):
    env.cfg.mkdir(parents=True)
    (env.cfg / ".claude.json").mkdir()
    assert accounts.read_identity() is None
    accounts.invalidate()
    (env.cfg / ".claude.json").rmdir()
    (env.cfg / ".claude.json").write_text("[1, 2, 3]")
    assert accounts.read_identity() is None
    accounts.invalidate()
    (env.cfg / ".claude.json").write_text(json.dumps({"oauthAccount": "a string"}))
    assert accounts.read_identity() is None


def test_auth_status_is_the_fallback_when_the_file_names_no_account(env, monkeypatch):
    monkeypatch.setattr(claude_auth, "status", lambda: {"installed": True, "loggedIn": True, "email": "me@example.com",
                                                        "subscriptionType": "max", "authMethod": "claude.ai", "orgName": "Me Org"})
    ident = accounts.read_identity()                                        # no state file at all
    assert ident == {"key": "me@example.com", "email": "me@example.com", "name": None, "org": "Me Org", "org_id": None,
                     "plan": "max", "tier": None, "config_dir": str(env.cfg), "source": "auth"}
    accounts.invalidate()
    env.cfg.mkdir(parents=True)
    (env.cfg / ".claude.json").write_text(json.dumps({"numStartups": 3}))   # a state file without oauthAccount
    assert accounts.read_identity()["source"] == "auth"
    accounts.invalidate()
    state_file(env.cfg)
    assert accounts.read_identity()["source"] == "file", "the file is asked first"


def test_auth_fallback_needs_a_logged_in_account_it_can_name_and_the_boards_own_dir(env, monkeypatch):
    assert accounts.read_identity() is None, "logged out"
    accounts.invalidate()
    monkeypatch.setattr(claude_auth, "status", lambda: {"loggedIn": True, "authMethod": "api_key"})
    assert accounts.read_identity() is None, "logged in but no email: nothing to key by"
    accounts.invalidate()
    monkeypatch.setattr(claude_auth, "status", lambda: {"loggedIn": True, "email": "me@example.com"})
    assert accounts.read_identity(env.tmp / "elsewhere") is None, "claude auth status speaks for the board's config dir only"
    monkeypatch.setattr(claude_auth, "status", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    accounts.invalidate()
    assert accounts.read_identity() is None, "never raises"


def test_identity_is_cached_for_60_seconds_and_a_forced_read_follows_the_state_file_not_the_clock(env, monkeypatch):
    state_file(env.cfg)
    opens = spy_opens(monkeypatch)

    def reads():
        return len([p for p in opens if p.endswith(".claude.json")])

    def email():
        return accounts.read_identity()["email"]
    assert email() == "a@example.com" and reads() == 1
    state_file(env.cfg, uuid=UUID_B, email="b@example.com")
    env.later(30)
    assert email() == "a@example.com" and reads() == 1, "inside 60 s: the cache"
    assert accounts.read_identity(force=True)["email"] == "b@example.com" and reads() == 2, "forced and the file changed: read again"
    assert accounts.read_identity(force=True)["email"] == "b@example.com" and reads() == 2, "forced and the file did not change: one stat"
    state_file(env.cfg)
    assert accounts.read_identity(force=True)["email"] == "a@example.com" and reads() == 3, "changed again, read at once: no pause"
    env.later(1)
    state_file(env.cfg, uuid=UUID_B, email="b@example.com")
    assert accounts.read_identity(force=True)["email"] == "b@example.com" and reads() == 4, "a second after the last read: still no pause"
    state_file(env.cfg)
    env.later(59)
    assert email() == "b@example.com" and reads() == 4, "59 s after the last read, not forced: the cache"
    env.later(2)
    assert email() == "a@example.com" and reads() == 5, "past 60 s: read again"
    env.later(500)
    assert accounts.read_identity(force=True)["email"] == "a@example.com" and reads() == 5, "a forced read of an unchanged file never re-parses"


def test_a_forced_read_pauses_only_where_the_file_cannot_settle_the_answer(env, monkeypatch):
    """No account in the file: the auth fallback may know more, but `claude auth status` is never started more than every 5 s by
    forced reads; and a forced read on the request path (auth=False) never starts it at all."""
    calls = []
    monkeypatch.setattr(claude_auth, "status", lambda: calls.append(1) or {"loggedIn": True, "email": "me@example.com"})
    assert accounts.read_identity()["source"] == "auth" and calls == [1]
    env.later(2)
    assert accounts.read_identity(force=True)["source"] == "auth" and calls == [1], "forced inside 5 s: the cached answer"
    env.later(10)
    assert accounts.read_identity(force=True, auth=False)["source"] == "auth" and calls == [1], "request path: never a process"
    assert accounts.read_identity(force=True)["source"] == "auth" and calls == [1, 1], "the tick's forced read may ask again after 5 s"


def test_a_request_path_read_does_not_poison_the_cache_for_the_auth_fallback(env, monkeypatch):
    monkeypatch.setattr(claude_auth, "status", lambda: {"loggedIn": True, "email": "me@example.com"})
    assert accounts.read_identity(auth=False) is None, "no file and the request path never starts a process"
    assert accounts.read_identity()["key"] == "me@example.com", "the tick still gets the fallback straight away"
    assert accounts.read_identity(auth=False)["key"] == "me@example.com", "and a file-only read does not forget it"
    env.later(61)
    assert accounts.read_identity(auth=False, force=True)["key"] == "me@example.com"


def test_no_credential_file_is_ever_opened_and_no_secret_reaches_the_kv(env, monkeypatch):
    state_file(env.cfg)
    (env.cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": SECRET}}))
    opens = spy_opens(monkeypatch)
    accounts.observe(env.db, T0)
    accounts.for_reading(env.db, rl(T0 + 100, T0 + 1000), T0)
    accounts.view(env.db, full=True)
    accounts.headroom(env.db, T0)
    assert not [p for p in opens if "credentials" in p], opens
    everything = json.dumps([dict(r) for r in env.db.conn.execute("SELECT * FROM kv")] +
                            [dict(r) for r in env.db.conn.execute("SELECT * FROM samples")])
    assert SECRET not in everything


# ================================================================== observe
def test_observe_creates_the_records_and_one_acct_event(env):
    state_file(env.cfg)
    assert accounts.current(env.db) is None
    assert accounts.observe(env.db, T0) == UUID_A
    rec = kv(env.db, "accounts")[UUID_A]
    assert rec == {"key": UUID_A, "email": "a@example.com", "name": "Ann", "org": "Ann's Org",
                   "org_id": "cccccccc-0000-4000-8000-000000000003", "plan": "max", "tier": "default_claude_max_20x",
                   "config_dir": str(env.cfg), "first_seen": iso(T0), "last_seen": iso(T0),
                   "label": None}
    assert kv(env.db, "account_current") == {"key": UUID_A, "since": iso(T0), "config_dir": str(env.cfg)}
    assert events(env.db) == [(UUID_A, 1.0, {"to": UUID_A})], "the first sighting has no `from`"
    assert accounts.current(env.db) == UUID_A


def test_observe_without_a_change_writes_no_event_and_touches_last_seen_every_5_minutes(env):
    state_file(env.cfg)
    accounts.observe(env.db, T0)
    at0 = env.db.kv_get("accounts")["at"]
    env.later(70)                                                           # the cache expired: the file is read again
    accounts.observe(env.db, T0 + 60)
    assert len(events(env.db)) == 1
    assert kv(env.db, "accounts")[UUID_A]["last_seen"] == iso(T0), "inside 5 minutes nothing is rewritten"
    assert env.db.kv_get("accounts")["at"] == at0
    env.later(70)
    accounts.observe(env.db, T0 + 301)
    assert kv(env.db, "accounts")[UUID_A]["last_seen"] == iso(T0 + 301) and len(events(env.db)) == 1


def test_observe_a_switch_updates_current_and_writes_from_to(env):
    state_file(env.cfg)
    accounts.observe(env.db, T0)
    accounts.set_label(env.db, UUID_A, "Personal")
    state_file(env.cfg, uuid=UUID_B, email="b@example.com", name="Bob", organizationType="claude_pro")
    env.later(61)
    assert accounts.observe(env.db, T0 + 100) == UUID_B
    assert accounts.current(env.db) == UUID_B
    cur = kv(env.db, "account_current")
    assert cur["key"] == UUID_B and cur["since"] == iso(T0 + 100)
    accts = kv(env.db, "accounts")
    assert set(accts) == {UUID_A, UUID_B} and accts[UUID_B]["plan"] == "pro" and accts[UUID_B]["first_seen"] == cur["since"]
    assert accts[UUID_A]["label"] == "Personal" and accts[UUID_A]["first_seen"] == iso(T0)
    assert events(env.db)[-1] == (UUID_B, 1.0, {"from": UUID_A, "to": UUID_B})
    state_file(env.cfg)                                                     # and back to A: a third event, A keeps its history
    env.later(61)
    accounts.observe(env.db, T0 + 200)
    assert [(k, m.get("from")) for k, _v, m in events(env.db)] == [(UUID_A, None), (UUID_B, UUID_A), (UUID_A, UUID_B)]
    assert kv(env.db, "accounts")[UUID_A]["first_seen"] == iso(T0)


def test_observe_refreshes_identity_fields_and_keeps_the_label(env):
    state_file(env.cfg)
    accounts.observe(env.db, T0)
    accounts.set_label(env.db, UUID_A, "Main")
    state_file(env.cfg, name="Ann B.", organizationType="claude_team")
    env.later(61)
    accounts.observe(env.db, T0 + 10)
    rec = kv(env.db, "accounts")[UUID_A]
    assert rec["name"] == "Ann B." and rec["plan"] == "team" and rec["label"] == "Main"
    assert len(events(env.db)) == 1


def test_observe_changes_nothing_when_no_identity_can_be_read_and_never_raises(env, monkeypatch):
    assert accounts.observe(env.db, T0) is None
    assert kv(env.db, "accounts") is None and kv(env.db, "account_current") is None and events(env.db) == []
    env.later(61)                                                           # the "no identity" answer is cached for a minute too
    state_file(env.cfg)
    accounts.observe(env.db, T0)
    (env.cfg / ".claude.json").write_text("{garbled")
    env.later(61)
    assert accounts.observe(env.db, T0 + 100) == UUID_A, "a garbled file keeps the account (no flap to nothing)"
    assert len(events(env.db)) == 1
    monkeypatch.setattr(env.db, "kv_set", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    state_file(env.cfg, uuid=UUID_B, email="b@example.com")
    env.later(61)
    assert accounts.observe(env.db, T0 + 200) is None


def test_the_same_email_reuses_its_record_whichever_form_the_key_came_in(env, monkeypatch):
    monkeypatch.setattr(claude_auth, "status", lambda: {"loggedIn": True, "email": "a@example.com", "subscriptionType": "max"})
    assert accounts.observe(env.db, T0) == "a@example.com", "no state file yet: keyed by email"
    state_file(env.cfg)                                                     # the file turns readable: same person, uuid key
    env.later(61)
    assert accounts.observe(env.db, T0 + 100) == "a@example.com", "the record with that email is reused"
    assert set(kv(env.db, "accounts")) == {"a@example.com"} and len(events(env.db)) == 1


def test_the_sampler_tick_calls_observe(env):
    state_file(env.cfg)
    assert accounts.observe in samples.TICK_HOOKS
    clock = SimpleNamespace(time=lambda: T0 + 5.0)
    s = samples.Sampler(env.db, health_fn=lambda: {}, counts_fn=dict, clock=clock)
    s.sample_once()
    assert accounts.current(env.db) == UUID_A and events(env.db)[0][0] == UUID_A


# ================================================================== for_reading
def test_the_first_reading_goes_to_the_current_account_and_leaves_its_fingerprint(env):
    state_file(env.cfg)
    accounts.observe(env.db, T0)
    assert accounts.for_reading(env.db, rl(T0 + 3000, T0 + 200000), T0 + 1) == UUID_A
    rec = kv(env.db, "accounts")[UUID_A]
    assert (rec["resets_5h"], rec["resets_7d"]) == (T0 + 3000, T0 + 200000)


def test_for_reading_before_any_observe_reads_the_identity_itself(env):
    state_file(env.cfg)
    assert accounts.current(env.db) is None
    assert accounts.for_reading(env.db, rl(T0 + 3000, T0 + 200000), T0) == UUID_A
    assert accounts.current(env.db) == UUID_A, "the unmatched reading forced the identity read and the switch detection"


def test_a_reading_with_nothing_to_go_on_is_unknown_and_makes_no_record(env):
    assert accounts.for_reading(env.db, rl(T0 + 3000, T0 + 200000), T0) == accounts.UNKNOWN
    assert kv(env.db, "accounts") is None, "an unknown account never gets a record"
    for junk in (None, {}, "x", {"five_hour": "no"}, {"five_hour": {"resets_at": "soon"}}, {"seven_day": {"resets_at": True}}):
        assert accounts.for_reading(env.db, junk, T0) == accounts.UNKNOWN
    state_file(env.cfg)
    accounts.observe(env.db, T0)
    before = env.db.kv_get("accounts")
    for junk in (None, {}, "x", {"five_hour": {"resets_at": None}}):
        assert accounts.for_reading(env.db, junk, T0 + 1) == UUID_A, "no reset times: the current account, nothing remembered"
    assert env.db.kv_get("accounts") == before


def two(env, r5a=T0 + 4000, r7a=T0 + 300000, r5b=T0 + 9000, r7b=T0 + 450000):
    """A used first (fingerprint r5a/r7a), then the shared login moved to B (fingerprint r5b/r7b). B is current."""
    state_file(env.cfg)
    accounts.observe(env.db, T0)
    assert accounts.for_reading(env.db, rl(r5a, r7a), T0 + 1) == UUID_A
    state_file(env.cfg, uuid=UUID_B, email="b@example.com", name="Bob")
    env.later(61)
    assert accounts.observe(env.db, T0 + 100) == UUID_B
    assert accounts.for_reading(env.db, rl(r5b, r7b), T0 + 101) == UUID_B
    return SimpleNamespace(r5a=r5a, r7a=r7a, r5b=r5b, r7b=r7b)


def fp(env, key):
    r = kv(env.db, "accounts")[key]
    return r["resets_5h"], r["resets_7d"]


def test_rollovers_of_whole_windows_stay_with_the_account(env):
    w = two(env)
    for n in (1, 2, 5):                                                     # 5 h windows chained while A kept working
        assert accounts.for_reading(env.db, rl(w.r5a + n * W5, w.r7a), T0 + 200) == UUID_A, n
    assert accounts.for_reading(env.db, rl(w.r5a + 5 * W5 + 59, w.r7a), T0 + 201) == UUID_A, "within the 60 s tolerance"
    assert accounts.for_reading(env.db, rl(w.r5a + 5 * W5 + 55, w.r7a + 2 * W7 + 40), T0 + 202) == UUID_A, "weekly rollover by n x 7 d"
    assert accounts.for_reading(env.db, rl(w.r5b + 4 * W5 - 30, w.r7b + 3 * W7 + 30), T0 + 203) == UUID_B


def test_a_stale_session_on_the_old_accounts_token_is_attributed_to_it(env):
    w = two(env)
    assert accounts.current(env.db) == UUID_B
    assert accounts.for_reading(env.db, rl(w.r5a, w.r7a, u5=80, u7=90), T0 + 300) == UUID_A, "A's own windows"
    assert accounts.for_reading(env.db, rl(w.r5a + W5, w.r7a), T0 + 301) == UUID_A, "A's next 5 h window (a rollover)"
    assert accounts.for_reading(env.db, rl(w.r5a + W5, None), T0 + 302) == UUID_A, "a reading with only the 5 h window still matches"
    assert accounts.for_reading(env.db, rl(None, w.r7a), T0 + 303) == UUID_A, "and so does one with only the weekly window"
    assert accounts.for_reading(env.db, rl(w.r5b, w.r7b), T0 + 304) == UUID_B
    assert fp(env, UUID_B) == (w.r5b, w.r7b), "B's fingerprint is untouched by A's readings"
    assert accounts.current(env.db) == UUID_B


def test_a_matched_reading_moves_the_fingerprint_forward_only(env):
    w = two(env)
    accounts.for_reading(env.db, rl(w.r5a + 2 * W5, w.r7a), T0 + 300)
    assert fp(env, UUID_A) == (w.r5a + 2 * W5, w.r7a)
    accounts.for_reading(env.db, rl(w.r5a + W5, w.r7a), T0 + 301)           # an older window from a staler session
    assert fp(env, UUID_A) == (w.r5a + 2 * W5, w.r7a), "a window's reset only moves forward"


def test_a_new_window_of_the_current_account_replaces_its_fingerprint(env):
    w = two(env)
    new5 = w.r5b + 3600 + 777                                               # not on the old grid: a window that started after a pause
    assert accounts.for_reading(env.db, rl(new5, w.r7b), T0 + 300) == UUID_B
    assert fp(env, UUID_B) == (new5, w.r7b)
    assert fp(env, UUID_A) == (w.r5a, w.r7a)


def test_the_weekly_match_beats_the_5h_one(env):
    w = two(env)
    assert accounts.for_reading(env.db, rl(w.r5b + W5, w.r7a), T0 + 300) == UUID_A, "5 h on B's grid, weekly A's: the weekly one decides"


def test_a_5h_grid_coincidence_does_not_claim_a_reading_whose_weekly_window_differs(env):
    w = two(env)
    # B is current. This reading's 5 h reset happens to sit on A's grid, but its weekly reset is neither A's nor B's: not A's.
    assert accounts.for_reading(env.db, rl(w.r5a + W5, T0 + 777777), T0 + 300) == UUID_B
    assert fp(env, UUID_A) == (w.r5a, w.r7a)


def test_an_account_returning_after_weeks_is_recognised_by_its_weekly_instant(env):
    w = two(env)
    assert accounts.for_reading(env.db, rl(T0 + 31 * 86400 + 123, w.r7a + 3 * W7), T0 + 300) == UUID_A


def test_a_switch_the_tick_has_not_seen_yet_does_not_overwrite_the_old_fingerprint(env):
    """After /login to B the statusline fires every few seconds, the tick only every 15 s and the identity is cached 60 s: B's first
    reading matches nothing and must not become A's new fingerprint."""
    state_file(env.cfg)
    accounts.observe(env.db, T0)
    accounts.for_reading(env.db, rl(T0 + 4000, T0 + 300000), T0 + 1)
    state_file(env.cfg, uuid=UUID_B, email="b@example.com", name="Bob")     # the /login happened; nobody has looked yet
    env.later(10)                                                           # the cached identity is still A's
    assert accounts.read_identity()["key"] == UUID_A
    assert accounts.for_reading(env.db, rl(T0 + 9000, T0 + 450000), T0 + 20) == UUID_B
    assert accounts.current(env.db) == UUID_B
    assert fp(env, UUID_A) == (T0 + 4000, T0 + 300000), "A keeps its own windows"
    assert fp(env, UUID_B) == (T0 + 9000, T0 + 450000)
    assert [k for k, _v, _m in events(env.db)] == [UUID_A, UUID_B]
    assert accounts.for_reading(env.db, rl(T0 + 9000, T0 + 450000), T0 + 21) == UUID_B, "and the next one matches B's fingerprint"


@pytest.mark.parametrize("gap", [0.5, 1, 2, 4.9])
def test_b_first_reading_seconds_after_any_identity_read_still_lands_on_b(env, gap):
    """The race the 5 s throttle left open: the identity was read at t (the tick, or an earlier forced read), the /login to B wrote the
    state file at t+1 s, and B's first statusline reading arrived at t+2 s. The forced re-read follows the file, not the clock, so the
    reading is B's: A's fingerprint is untouched and B's windows become B's fingerprint."""
    state_file(env.cfg)
    accounts.observe(env.db, T0)                                            # identity read at t
    accounts.for_reading(env.db, rl(T0 + 4000, T0 + 300000), T0 + 1)
    env.later(1)
    state_file(env.cfg, uuid=UUID_B, email="b@example.com", name="Bob")     # the /login to B at t+1
    env.later(gap)                                                          # B's first reading
    assert accounts.read_identity()["key"] == UUID_A, "the cache still says A: that is the whole problem"
    assert accounts.for_reading(env.db, rl(T0 + 9000, T0 + 450000), T0 + 20) == UUID_B
    assert accounts.current(env.db) == UUID_B
    assert fp(env, UUID_A) == (T0 + 4000, T0 + 300000), "A keeps its own windows"
    assert fp(env, UUID_B) == (T0 + 9000, T0 + 450000)
    assert [k for k, _v, _m in events(env.db)] == [UUID_A, UUID_B]
    env.later(61)
    assert accounts.observe(env.db, T0 + 90) == UUID_B                      # the tick catches up: nothing changes
    assert accounts.for_reading(env.db, rl(T0 + 9000, T0 + 450000), T0 + 91) == UUID_B, "B's next reading matches B, not A"
    assert fp(env, UUID_A) == (T0 + 4000, T0 + 300000)


def test_after_a_quick_login_b_feeds_the_current_views_and_its_own_series(board):
    """The same race through the hook: B's first statusline right after the /login must reach the 'claude' series and kv
    rate_limits (the pills, the scheduler guard) and acct:B, and A's series must not receive B's numbers."""
    db = board.db
    state_file(board.cfg)
    accounts.observe(db, T0)
    statusline(db, NAME, T0 + 4000, T0 + 300000, 40, 50)
    board.later(1)
    state_file(board.cfg, uuid=UUID_B, email="b@example.com", name="Bob")
    board.later(1)
    statusline(db, "shop--api--s2", T0 + 9000, T0 + 450000, 7, 8)           # B's first reading, 2 s after A's identity read
    board.later(61)
    accounts.observe(db, T0 + 100)                                          # the tick
    statusline(db, "shop--api--s2", T0 + 9000, T0 + 450000, 9, 10)          # B's next reading
    assert kv(db, "rate_limits") == rl(T0 + 9000, T0 + 450000, 9, 10)
    assert [v for v, _m in series(db, "rl_5h", "claude")] == [40.0, 7.0, 9.0]
    assert [v for v, _m in series(db, "rl_5h", f"acct:{UUID_B}")] == [7.0, 9.0]
    assert [v for v, _m in series(db, "rl_5h", f"acct:{UUID_A}")] == [40.0], "A got nothing of B's"
    assert db.open_row("shop--api--s2")["account"] == UUID_B and accounts.current(db) == UUID_B


def test_the_same_account_in_a_new_window_after_a_long_idle_gap_updates_its_own_fingerprint(env, monkeypatch):
    """No switch: the state file did not change, so the forced re-read is one stat (no parse) and the identity is known to be current.
    A weekly and 5 h window that match nothing (a new window started after a pause) become A's own fingerprint."""
    state_file(env.cfg)
    accounts.observe(env.db, T0)
    accounts.for_reading(env.db, rl(T0 + 4000, T0 + 300000), T0 + 1)
    opens = spy_opens(monkeypatch)
    env.later(5000)
    new5, new7 = T0 + 9 * 86400 + 777, T0 + 15 * 86400 + 1234              # both off the old grid
    assert accounts.for_reading(env.db, rl(new5, new7), T0 + 9 * 86400 + 800) == UUID_A
    assert fp(env, UUID_A) == (new5, new7), "replaced, not kept"
    assert list(kv(env.db, "accounts")) == [UUID_A] and [k for k, _v, _m in events(env.db)] == [UUID_A]
    assert not [p for p in opens if p.endswith(".claude.json")], "an unchanged state file is never parsed again for a forced read"
    assert accounts.for_reading(env.db, rl(new5 + W5, new7), T0 + 9 * 86400 + 900) == UUID_A, "and the next window chains from it"


def test_an_unmatched_reading_never_overwrites_a_record_on_the_strength_of_an_unconfirmed_identity(env):
    """The state file is unreadable at the moment of the /login (half written): the re-read cannot confirm who is logged in, so the
    reading goes to the current account as before but no fingerprint is written; the next reading, with a readable file, is B's."""
    state_file(env.cfg)
    accounts.observe(env.db, T0)
    accounts.for_reading(env.db, rl(T0 + 4000, T0 + 300000), T0 + 1)
    env.later(1)
    (env.cfg / ".claude.json").write_text('{"oauthAccount": {"accountUuid": "bbb')            # being rewritten for B
    assert accounts.for_reading(env.db, rl(T0 + 9000, T0 + 450000), T0 + 3) == UUID_A, "best guess, as today"
    assert fp(env, UUID_A) == (T0 + 4000, T0 + 300000), "but A's record is left alone"
    state_file(env.cfg, uuid=UUID_B, email="b@example.com", name="Bob")
    assert accounts.for_reading(env.db, rl(T0 + 9000, T0 + 450000), T0 + 4) == UUID_B
    assert fp(env, UUID_B) == (T0 + 9000, T0 + 450000) and fp(env, UUID_A) == (T0 + 4000, T0 + 300000)


def test_the_request_path_never_starts_claude_auth_status(env, monkeypatch):
    calls = []
    monkeypatch.setattr(claude_auth, "status", lambda: calls.append(1) or {"loggedIn": True, "email": "me@example.com"})
    assert accounts.for_reading(env.db, rl(T0 + 3000, T0 + 200000), T0) == accounts.UNKNOWN, "no state file: unknown on the request path"
    assert calls == [], "only the Sampler's tick may use the (cached) auth status fallback"
    assert accounts.observe(env.db, T0 + 1) == "me@example.com" and calls == [1]


def test_remember_false_is_a_pure_lookup(env):
    w = two(env)
    snap = (env.db.kv_get("accounts"), env.db.kv_get("account_current"))
    assert accounts.for_reading(env.db, rl(w.r5a + W5, None), T0 + 300, remember=False) == UUID_A
    assert accounts.for_reading(env.db, rl(T0 + 555, None), T0 + 301, remember=False) == UUID_B, "no match: the current account"
    assert (env.db.kv_get("accounts"), env.db.kv_get("account_current")) == snap


# ================================================================== hooks: series keys, the current-account views, session account
def statusline(db, name, r5, r7, u5=10.0, u7=20.0):
    return hooks.apply(db, name, "statusline", {"rate_limits": rl(r5, r7, u5, u7), "cost": {"total_cost_usd": 1.0}})


@pytest.fixture
def board(env):
    env.db.add_session(tmux_name=NAME, project="shop", repo="api", name="s1", launcher="claude")
    env.db.add_session(tmux_name="shop--api--s2", project="shop", repo="api", name="s2", launcher="claude")
    return env


def test_a_current_reading_goes_to_claude_and_to_its_account_series(board):
    db = board.db
    state_file(board.cfg)
    accounts.observe(db, T0)
    assert statusline(db, NAME, T0 + 3000, T0 + 200000, 10, 20)["stats"] is True
    for name, want in (("rl_5h", 10.0), ("rl_7d", 20.0)):
        assert [v for v, _m in series(db, name, "claude")] == [want]
        assert [v for v, _m in series(db, name, f"acct:{UUID_A}")] == [want]
    assert series(db, "rl_5h", f"acct:{UUID_A}")[0][1] == {"resets_at": T0 + 3000}
    assert kv(db, "rate_limits") == rl(T0 + 3000, T0 + 200000, 10, 20)
    assert db.open_row(NAME)["account"] == UUID_A, "the session's account follows its readings"


def test_a_stale_session_feeds_its_own_account_and_leaves_the_current_views_alone(board):
    db = board.db
    w = two(board)
    statusline(db, "shop--api--s2", w.r5b, w.r7b, 5, 6)                     # a session on B's token (current)
    assert kv(db, "rate_limits") == rl(w.r5b, w.r7b, 5, 6)
    before = (series(db, "rl_5h", "claude"), kv(db, "rate_limits"))
    statusline(db, NAME, w.r5a, w.r7a, 77, 88)                              # a session still on A's token
    assert (series(db, "rl_5h", "claude"), kv(db, "rate_limits")) == before, "the pills, the scheduler guard and the banner read B"
    assert [v for v, _m in series(db, "rl_5h", f"acct:{UUID_A}")] == [77.0]
    assert [v for v, _m in series(db, "rl_7d", f"acct:{UUID_A}")] == [88.0]
    assert [v for v, _m in series(db, "rl_5h", f"acct:{UUID_B}")] == [5.0]
    assert db.open_row(NAME)["account"] == UUID_A and db.open_row("shop--api--s2")["account"] == UUID_B
    # that session's token is replaced by B's (the shared login): its next reading is B's and the row follows
    statusline(db, NAME, w.r5b, w.r7b, 9, 10)
    assert db.open_row(NAME)["account"] == UUID_B
    assert series(db, "rl_5h", "claude")[-1][0] == 9.0 and kv(db, "rate_limits") == rl(w.r5b, w.r7b, 9, 10)


def test_the_stale_guard_and_the_throttle_run_per_key(board):
    db = board.db
    w = two(board)
    statusline(db, "shop--api--s2", w.r5b, w.r7b, 62, 20)
    statusline(db, NAME, w.r5a, w.r7a, 40, 50)
    statusline(db, "shop--api--s2", w.r5b, w.r7b, 60, 20)                   # the same window and a lower number inside 5 minutes: skipped
    statusline(db, NAME, w.r5a, w.r7a, 38, 50)
    assert [v for v, _m in series(db, "rl_5h", f"acct:{UUID_B}")] == [62.0]
    assert [v for v, _m in series(db, "rl_5h", "claude")] == [62.0]
    assert [v for v, _m in series(db, "rl_5h", f"acct:{UUID_A}")] == [40.0]
    statusline(db, NAME, w.r5a, w.r7a, 45, 50)                              # moved by 5: written to A's key only
    assert [v for v, _m in series(db, "rl_5h", f"acct:{UUID_A}")] == [40.0, 45.0] and [v for v, _m in series(db, "rl_5h", "claude")] == [62.0]


def test_switching_back_to_the_first_account_updates_the_current_views_on_its_first_reading(board):
    """B is current. The person /logs in to A again; the first statusline reading carries A's windows, which match A's fingerprint
    (a non-current account). The forced look at the state file makes A current on that very reading, so the 'claude' series and kv
    rate_limits show A's numbers at once, not after the next tick."""
    db = board.db
    w = two(board)                                                          # A used, then B logged in and current
    statusline(db, "shop--api--s2", w.r5b, w.r7b, 30, 31)
    assert kv(db, "rate_limits") == rl(w.r5b, w.r7b, 30, 31)
    board.later(1)
    state_file(board.cfg)                                                   # /login to A again; no tick has run
    assert accounts.current(db) == UUID_B
    statusline(db, NAME, w.r5a, w.r7a, 77, 88)
    assert accounts.current(db) == UUID_A
    assert kv(db, "rate_limits") == rl(w.r5a, w.r7a, 77, 88), "the scheduler guard reads A's numbers now"
    assert series(db, "rl_5h", "claude")[-1][0] == 77.0 and series(db, "rl_7d", "claude")[-1][0] == 88.0
    assert [k for k, _v, _m in events(db)] == [UUID_A, UUID_B, UUID_A]
    assert [v for v, _m in series(db, "rl_5h", f"acct:{UUID_A}")][-1] == 77.0
    statusline(db, "shop--api--s2", w.r5b, w.r7b, 33, 34)                   # a session still on B's token is the stale one now
    assert kv(db, "rate_limits") == rl(w.r5a, w.r7a, 77, 88) and [v for v, _m in series(db, "rl_5h", f"acct:{UUID_B}")][-1] == 33.0


def test_a_stale_session_costs_one_stat_per_reading_not_a_parse(board, monkeypatch):
    """While the file still names the current account, a reading that matches the other account only looks at the file's stamp."""
    db = board.db
    w = two(board)
    statusline(db, "shop--api--s2", w.r5b, w.r7b, 5, 6)
    statusline(db, NAME, w.r5a, w.r7a, 50, 60)                              # warm: the forced look parses once if the file changed
    opens = spy_opens(monkeypatch)
    for i in range(5):
        statusline(db, NAME, w.r5a, w.r7a, 51 + i * 6, 60)
    assert not [p for p in opens if p.endswith(".claude.json")]
    assert [v for v, _m in series(db, "rl_5h", f"acct:{UUID_A}")][-1] == 75.0, "the stale session's readings are still recorded"
    assert accounts.current(db) == UUID_B and kv(db, "rate_limits") == rl(w.r5b, w.r7b, 5, 6)


def test_a_box_with_no_identity_behaves_exactly_as_before(board):
    db = board.db
    statusline(db, NAME, T0 + 3000, T0 + 200000, 10, 20)
    assert [v for v, _m in series(db, "rl_5h", "claude")] == [10.0] and kv(db, "rate_limits") is not None
    assert db.samples_distinct_keys("rl_5h", "2000-01-01T00:00:00+00:00") == ["claude"], "no acct:unknown series"
    assert kv(db, "accounts") is None and db.open_row(NAME)["account"] is None


def test_a_failing_attribution_degrades_to_the_old_behaviour(board, monkeypatch):
    db = board.db
    state_file(board.cfg)
    accounts.observe(db, T0)
    monkeypatch.setattr(accounts, "for_reading", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kv gone")))
    assert statusline(db, NAME, T0 + 3000, T0 + 200000)["stats"] is True
    assert [v for v, _m in series(db, "rl_5h", "claude")] == [10.0] and kv(db, "rate_limits") is not None
    assert db.samples_distinct_keys("rl_5h", "2000-01-01T00:00:00+00:00") == ["claude"]


def test_the_scheduler_guard_and_autoresume_keep_reading_the_current_account(board):
    from app import autoresume, scheduler
    db = board.db
    w = two(board)
    statusline(db, "shop--api--s2", w.r5b, w.r7b, 30, 20)
    statusline(db, NAME, w.r5a, w.r7a, 99, 99)                              # the stale session is at 99 %: not the guard's business
    q = scheduler.quota_state(db)
    assert q["pct"] == 30.0 and q["resets_at"] == w.r5b and q["known"] is True
    assert autoresume.episodes(db, T0 + 500) == []


def test_limit_episodes_carry_the_account_that_hit_them(board):
    db = board.db
    w = two(board)
    hooks._record_limit(db, NAME, {"kind": "5h", "resets_at": w.r5a + W5, "message": "limit"})
    hooks._record_limit(db, "shop--api--s2", {"kind": "5h", "resets_at": w.r5b, "message": "limit"})
    hooks._record_limit(db, "shop--api--s2", {"kind": "7d", "resets_at": w.r7a, "message": "weekly"})
    hooks._record_limit(db, "shop--api--s2", {"kind": "other", "message": "overloaded"})
    metas = [json.loads(m) for _at, _k, _v, m in db.samples_query("lim", None, "2000-01-01T00:00:00+00:00")]
    assert [m["acct"] for m in metas] == [UUID_A, UUID_B, UUID_A, UUID_B]
    assert fp(board, UUID_A) == (w.r5a, w.r7a) and fp(board, UUID_B) == (w.r5b, w.r7b), "a message's reset time is not a fingerprint"


def test_limit_episodes_without_an_identity_have_no_acct(board):
    hooks._record_limit(board.db, NAME, {"kind": "5h", "resets_at": T0 + 100, "message": "limit"})
    assert [json.loads(m) for _a, _k, _v, m in board.db.samples_query("lim", None, "2000-01-01T00:00:00+00:00")] == [
        {"session": NAME, "resets_at": T0 + 100, "message": "limit"}]


def test_state_events_carry_the_account(board):
    from app import main
    db = board.db
    state_file(board.cfg)
    accounts.observe(db, T0)
    hook = main._state_sampler(db)
    hook(NAME, None, "working", "E", {"project": "shop", "repo": "api", "name": "s1", "agent": "claude", "account": UUID_B})
    hook("shop--api--s2", "idle", "working", "E", {"project": "shop", "repo": "api", "name": "s2", "agent": "claude"})
    assert db.sample_last("state", NAME)["meta"]["acct"] == UUID_B, "the session's own account wins"
    assert db.sample_last("state", "shop--api--s2")["meta"]["acct"] == UUID_A, "else the current one"
    assert samples.record_state(db, "t", None, "idle", "E", {"project": "p"}, at=T0, acct="zzz") and db.sample_last("state", "t")["meta"]["acct"] == "zzz"


def test_codex_state_events_get_no_account_but_an_explicit_one_is_kept(board):
    from app import main
    db = board.db
    state_file(board.cfg)
    accounts.observe(db, T0)
    hook = main._state_sampler(db)
    hook("shop--api--cx1", None, "working", "E", {"project": "shop", "repo": "api", "name": "cx1", "agent": "codex"})
    hook("shop--api--s2", None, "working", "E", {"project": "shop", "repo": "api", "name": "s2"})
    assert "acct" not in (db.sample_last("state", "shop--api--cx1")["meta"] or {}), "accounts are Claude subscriptions"
    assert db.sample_last("state", "shop--api--s2")["meta"]["acct"] == UUID_A, "a row with no agent is a Claude one"


def test_only_claude_cost_entries_are_tagged_with_an_account(board):
    db = board.db
    two(board)                                                              # current: B
    sid_c, sid_x, sid_k = ("11111111-1111-4111-8111-11111111111%d" % i for i in (1, 2, 3))
    db.add_session(tmux_name="shop--api--s3", project="shop", repo="api", name="s3", launcher="claude", claude_session_id=sid_c, account=UUID_A)
    entries = [{"agent": "claude", "id": sid_c, "cost": 1.0, "tokens": 10, "last": "2026-10-03T00:00:00Z", "models": []},
               {"agent": "codex", "id": sid_x, "cost": 2.0, "tokens": 20, "last": "2026-10-03T00:00:00Z", "models": []},
               {"id": sid_k, "cost": 3.0, "tokens": 30, "last": "2026-10-03T00:00:00Z", "models": []}]
    cost._tag_accounts(db, entries)
    assert [e.get("acct") for e in entries] == [UUID_A, None, UUID_B], "Claude by its session's account else the current one; Codex none"
    assert "acct" not in entries[1]
    assert samples.record_cost(db, {"sessions": entries}, "2026-10-03T00:05:00+00:00") == 3
    metas = {k: json.loads(m) for _at, k, _v, m in db.samples_query("cost", None, "2000-01-01T00:00:00+00:00")}
    assert "acct" not in metas[f"codex:{sid_x}"] and metas[f"codex:{sid_x}"]["a"] == "codex"
    assert metas[f"claude:{sid_c}"]["acct"] == UUID_A and metas[f"claude:{sid_k}"]["acct"] == UUID_B


def test_the_demo_state_gives_an_account_to_claude_sessions_only():
    demo = json.loads((Path(__file__).resolve().parent.parent / "app" / "static" / "demo" / "state.json").read_text(encoding="utf-8"))
    keys = {a["key"] for a in demo["accounts"]["list"]}
    rows = []

    def walk(o):
        if isinstance(o, dict):
            if "row_id" in o and "agent" in o:
                rows.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(demo)
    assert {r["agent"] for r in rows} >= {"claude", "codex"}
    for r in rows:
        if r["agent"] == "claude":
            assert r["account"] in keys, r["tmux"]
        else:
            assert not r.get("account"), "live code never sets an account on a Codex session"


def test_cost_samples_carry_the_account_of_their_session_else_the_current_one(board, monkeypatch):
    db = board.db
    two(board)                                                              # current: B
    sid_a, sid_b, sid_x = ("11111111-1111-4111-8111-11111111111%d" % i for i in (1, 2, 3))
    db.add_session(tmux_name="shop--api--s3", project="shop", repo="api", name="s3", launcher="claude", claude_session_id=sid_a, account=UUID_A)
    db.add_session(tmux_name="shop--api--s4", project="shop", repo="api", name="s4", launcher="claude", claude_session_id=sid_b)
    costs = {s: {"cost": 1.5, "tokens": 100, "last": "2026-10-03T00:00:00Z", "models": ["claude-opus-5"]} for s in (sid_a, sid_b, sid_x)}
    monkeypatch.setattr(cost, "fetch_sessions", lambda: costs)
    assert cost.refresh(db) is not None
    metas = {k: json.loads(m) for _at, k, _v, m in db.samples_query("cost", None, "2000-01-01T00:00:00+00:00")}
    assert metas[f"claude:{sid_a}"]["acct"] == UUID_A, "started under A and still A's"
    assert metas[f"claude:{sid_b}"]["acct"] == UUID_B and metas[f"claude:{sid_x}"]["acct"] == UUID_B
    assert metas[f"claude:{sid_a}"]["tok"] == 100 and metas[f"claude:{sid_a}"]["a"] == "claude", "the rest of the meta is as before"


def test_cost_samples_have_no_acct_before_an_identity_is_known(board, monkeypatch):
    sid = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(cost, "fetch_sessions", lambda: {sid: {"cost": 1.0, "tokens": 5, "last": "2026-10-03T00:00:00Z", "models": []}})
    assert cost.refresh(board.db) is not None
    assert board.db.sample_last("cost", f"claude:{sid}")["meta"] == {"a": "claude", "tok": 5}, "no identity: the meta is what it was before"


# ================================================================== sessions and the database
def test_session_rows_start_with_the_current_account(env, lite_client, fake_tmux, monkeypatch):
    from app import main
    state_file(settings.claude_config_dir)
    accounts.observe(main.db, T0)
    cwd = str(settings.projects_dir)
    name, rid = main._start_session_row("shop--api--s1", "shop", "api", "s1", "claude", cwd)
    assert main.db.open_row(name)["account"] == UUID_A and rid == main.db.open_row(name)["row_id"]
    name2, _ = main._start_session_row("shop--api--sh", "shop", "api", "sh", "shell", cwd, agent="shell")
    assert main.db.open_row(name2)["account"] is None, "a shell is no Claude session"
    st = lite_client.get("/api/state", headers=H).json()
    assert st["accounts"]["current"] == UUID_A
    one = lite_client.get(f"/api/sessions/{name}", headers=H)
    assert one.status_code == 200 and one.json()["account"] == UUID_A, one.text
    merged, _down = main._merged_sessions(rich=True)
    assert merged[name]["account"] == UUID_A and merged[name2]["account"] is None, "the state's session rows expose their account"


def test_session_view_and_add_session_account(env):
    db = env.db
    db.add_session(tmux_name="a", project="p", repo="r", name="n", launcher="claude")
    db.add_session(tmux_name="b", project="p", repo="r", name="n", launcher="claude", account=UUID_A)
    rows = db.open_rows()
    assert rows["a"]["account"] is None and rows["b"]["account"] == UUID_A
    assert dbmod.session_view({"id": 1})["account"] is None, "a row dict without the column still has the key"


def test_set_session_account_updates_the_newest_open_row_only_on_change(env):
    db = env.db
    db.add_session(tmux_name="a", project="p", repo="r", name="n", launcher="claude", account=UUID_A)
    db.end("a")
    db.add_session(tmux_name="a", project="p", repo="r", name="n", launcher="claude")
    assert db.set_session_account("a", UUID_B) is True
    assert db.set_session_account("a", UUID_B) is False, "unchanged: no write"
    assert db.set_session_account("a", None) is False and db.set_session_account("nope", UUID_B) is False
    assert db.open_row("a")["account"] == UUID_B
    with db.lock:
        old = db.conn.execute("SELECT account FROM sessions WHERE ended_at IS NOT NULL").fetchone()[0]
    assert old == UUID_A, "the ended row is history"


def test_session_accounts_maps_agent_session_ids(env):
    db = env.db
    db.add_session(tmux_name="a", project="p", repo="r", name="n", launcher="claude", claude_session_id="ABC-1", account=UUID_A)
    db.add_session(tmux_name="b", project="p", repo="r", name="n", launcher="claude", claude_session_id="DEF-2")
    db.add_session(tmux_name="c", project="p", repo="r", name="n", launcher="claude", account=UUID_B)
    assert db.session_accounts() == {"abc-1": UUID_A}


def test_the_migration_is_additive_and_idempotent(tmp_path):
    p = tmp_path / "old.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(SCHEMA)                                              # the v0.1 table: no account column
    conn.execute("INSERT INTO sessions(tmux_name, project, repo, name, launcher, created_at) VALUES ('a','p','r','n','claude','2026-01-01')")
    conn.commit()
    conn.close()
    db = DB(p)
    cols = {r[1]: (r[2], r[3], r[4]) for r in db.conn.execute("PRAGMA table_info(sessions)")}
    assert cols["account"] == ("TEXT", 0, None), "nullable, no default: the previous image's INSERTs still work"
    assert db.open_row("a")["account"] is None
    DB(p)                                                                   # a second start is a no-op
    db.conn.execute("INSERT INTO sessions(tmux_name, project, repo, name, launcher, created_at) VALUES ('b','p','r','n','claude','2026-01-01')")
    assert [m for m in dbmod.MIGRATIONS if "account" in m] == ["ALTER TABLE sessions ADD COLUMN account TEXT"]


# ================================================================== views, headroom and the HTTP surface
def seeded(env):
    """Two accounts with readings in their own series; B current."""
    w = two(env)
    samples.record_statusline(env.db, NAME, {"rate_limits": rl(w.r5a, w.r7a, 80, 40)}, "claude", at=T0 + 10, account=UUID_A, current=False)
    samples.record_statusline(env.db, NAME, {"rate_limits": rl(w.r5b, w.r7b, 20, 60)}, "claude", at=T0 + 11, account=UUID_B, current=True)
    return w


def test_view_lists_the_current_account_first_with_its_windows(env):
    w = seeded(env)
    v = accounts.view(env.db)
    assert v["current"] == UUID_B and [a["key"] for a in v["list"]] == [UUID_B, UUID_A]
    b, a = v["list"]
    assert b == {"key": UUID_B, "email": "b@example.com", "name": "Bob", "label": None, "plan": "max", "rl_5h": 20.0, "rl_7d": 60.0,
                 "resets_5h": w.r5b, "resets_7d": w.r7b, "current": True}
    assert a["rl_5h"] == 80.0 and a["rl_7d"] == 40.0 and a["current"] is False
    full = accounts.view(env.db, full=True)["list"][0]
    assert {"org", "org_id", "tier", "config_dir", "first_seen", "last_seen", "rl_5h_at", "rl_7d_at"} <= set(full)
    assert accounts.view(DB(env.tmp / "empty.db")) == {"current": None, "list": []}


def test_view_never_reads_a_file(env, monkeypatch):
    seeded(env)
    opens = spy_opens(monkeypatch)
    accounts.view(env.db)
    assert not [p for p in opens if p.endswith(".json")]


def test_headroom_is_per_account_and_a_passed_reset_is_fully_open(env):
    w = seeded(env)
    h = accounts.headroom(env.db, T0 + 200)
    assert h["5h"] == [{"key": UUID_B, "left_pct": 80.0, "resets_at": w.r5b}, {"key": UUID_A, "left_pct": 20.0, "resets_at": w.r5a}]
    assert [(r["key"], r["left_pct"]) for r in h["7d"]] == [(UUID_A, 60.0), (UUID_B, 40.0)]
    later = accounts.headroom(env.db, w.r5a + 1)
    assert later["5h"][0] == {"key": UUID_A, "left_pct": 100.0, "resets_at": w.r5a}, "A's 5 h window has reset: wide open"
    assert [r["key"] for r in later["7d"]] == [UUID_A, UUID_B]
    empty = DB(env.tmp / "e2.db")
    assert accounts.headroom(empty, T0) == {"5h": [], "7d": []}
    env.db.kv_set("accounts", {**kv(env.db, "accounts"), "zzz": {"key": "zzz"}})
    assert {r["key"] for r in accounts.headroom(env.db, T0)["5h"]} == {UUID_A, UUID_B}, "an account without a reading is left out"


def test_labels_are_cleaned_and_only_the_label_changes(env):
    state_file(env.cfg)
    accounts.observe(env.db, T0)
    assert accounts.label(env.db, UUID_A) == "Ann", "no label: the display name"
    rec = accounts.set_label(env.db, UUID_A, "  Work \n\t account\x00  ")
    assert rec["label"] == "Work account" and accounts.label(env.db, UUID_A) == "Work account"
    assert accounts.set_label(env.db, UUID_A, "x" * 200)["label"] == "x" * 60
    assert accounts.set_label(env.db, UUID_A, "   ")["label"] is None and accounts.set_label(env.db, UUID_A, None)["label"] is None
    assert kv(env.db, "accounts")[UUID_A]["email"] == "a@example.com"
    with pytest.raises(KeyError):
        accounts.set_label(env.db, "nope", "x")
    with pytest.raises(ValueError):
        accounts.set_label(env.db, UUID_A, 5)
    assert accounts.label(env.db, "nope") == "nope"


def test_state_accounts_and_the_api(env, lite_client):
    from app import main
    db = main.db
    state_file(settings.claude_config_dir)
    accounts.observe(db, T0)
    w = SimpleNamespace(r5=T0 + 3000, r7=T0 + 200000)
    statusline(db, "shop--api--s9", w.r5, w.r7, 33, 44)                     # no row for the session: the hook still attributes
    st = lite_client.get("/api/state", headers=H).json()
    assert st["accounts"]["current"] == UUID_A
    row = st["accounts"]["list"][0]
    assert (row["key"], row["rl_5h"], row["rl_7d"], row["resets_5h"], row["resets_7d"], row["current"]) == (UUID_A, 33.0, 44.0, w.r5, w.r7, True)
    assert st["usage"]["value"]["five_hour"]["used_percentage"] == 33, "state.usage keeps its shape"
    # GET /api/accounts
    r = lite_client.get("/api/accounts", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["current"] == UUID_A and body["list"][0]["email"] == "a@example.com" and body["list"][0]["config_dir"] == str(settings.claude_config_dir)
    assert body["list"][0]["first_seen"] and body["list"][0]["rl_5h_at"]
    # PATCH /api/accounts/{key}
    p = lite_client.patch(f"/api/accounts/{UUID_A}", headers=H, json={"label": "  Personal  "})
    assert p.status_code == 200 and p.json()["label"] == "Personal" and p.json()["key"] == UUID_A
    assert lite_client.get("/api/state", headers=H).json()["accounts"]["list"][0]["label"] == "Personal"
    assert lite_client.patch(f"/api/accounts/{UUID_A}", headers=H, json={"label": ""}).json()["label"] is None
    assert lite_client.patch(f"/api/accounts/{UUID_A}", headers=H, json={"label": None, "email": "evil@example.com"}).status_code == 200
    assert kv(db, "accounts")[UUID_A]["email"] == "a@example.com", "label only"
    assert lite_client.patch(f"/api/accounts/{UUID_A}", headers=H, json={}).status_code == 400
    assert lite_client.patch("/api/accounts/nope", headers=H, json={"label": "x"}).status_code == 404
    assert lite_client.patch(f"/api/accounts/{UUID_A}", headers={"Tailscale-User-Login": "mallory@example.com", "X-CCBoard": "1"},
                             json={"label": "x"}).status_code == 403
    assert lite_client.patch(f"/api/accounts/{UUID_A}", headers={"Tailscale-User-Login": "alice@example.com"}, json={"label": "x"}).status_code == 403, "no CSRF header"
    assert lite_client.get("/api/accounts").status_code == 403


def test_the_per_account_series_is_served_by_the_existing_endpoint(env, lite_client):
    from app import main
    state_file(settings.claude_config_dir)
    accounts.observe(main.db, T0)
    statusline(main.db, "shop--api--s9", T0 + 3000, T0 + 200000, 33, 44)
    r = lite_client.get("/api/series", params={"series": "rl_5h", "key": f"acct:{UUID_A}", "since": "1h"}, headers=H)
    assert r.status_code == 200 and f"rl_5h:acct:{UUID_A}" in r.json()["series"]
    ev = lite_client.get("/api/series/events", params={"series": "acct", "since": "30d"}, headers=H).json()["events"]
    assert [(e["key"], e["m"]) for e in ev] == [(UUID_A, {"to": UUID_A})]


# ---------------------------------------------------------------- v0.5.17c: remember, adopt, hold (what the saved-login store needs)

UUID_C = "cccccccc-0000-4000-8000-000000000004"


def ident(uuid=UUID_A, email="a@example.com", name="Ann", config_dir=None, **kw):
    return {"key": uuid, "email": email, "name": name, "org": None, "org_id": None, "plan": "max", "tier": None, "config_dir": config_dir,
            "source": "file", **kw}


def acct_events(db):
    return [(k, json.loads(m)) for _at, k, _v, m in db.samples_query("acct", None, iso(0))]


def test_remember_records_an_account_without_moving_current_or_writing_an_event(env):
    assert accounts.adopt(env.db, ident(UUID_A, config_dir="/live"), T0) == UUID_A
    key = accounts.remember(env.db, ident(UUID_B, "b@example.com", "Bob"), T0 + 5)
    assert key == UUID_B and kv(env.db, "account_current")["key"] == UUID_A and accounts.current(env.db) == UUID_A
    rec = kv(env.db, "accounts")[UUID_B]
    assert rec["email"] == "b@example.com" and rec["name"] == "Bob" and rec["label"] is None and rec["config_dir"] is None
    assert rec["first_seen"] == rec["last_seen"] == iso(T0 + 5), "a new record is seen as of the moment it was added"
    assert acct_events(env.db) == [(UUID_A, {"to": UUID_A})], "no acct event for an account that is not live"
    accounts.set_label(env.db, UUID_B, "Work")
    assert accounts.remember(env.db, ident(UUID_B, "b@example.com", "Bobby", config_dir=None), T0 + 9000) == UUID_B
    rec = kv(env.db, "accounts")[UUID_B]
    assert (rec["name"], rec["label"], rec["last_seen"], rec["first_seen"]) == ("Bobby", "Work", iso(T0 + 5), iso(T0 + 5)), "a refresh keeps last_seen and the label"
    assert accounts.remember(env.db, ident("another-key", "b@example.com"), T0) == UUID_B, "the same email is the same account"
    assert set(kv(env.db, "accounts")) == {UUID_A, UUID_B}
    assert accounts.remember(env.db, ident(UUID_B, config_dir="/elsewhere"), T0)
    assert kv(env.db, "accounts")[UUID_B]["config_dir"] == "/elsewhere"


def test_adopt_is_the_public_current_account_change(env):
    assert accounts.adopt(env.db, ident(UUID_A), T0) == UUID_A and accounts.current(env.db) == UUID_A
    assert accounts.adopt(env.db, ident(UUID_A), T0 + 1) == UUID_A and len(acct_events(env.db)) == 1, "no event without a change"
    assert accounts.adopt(env.db, ident(UUID_B, "b@example.com", "Bob"), T0 + 2) == UUID_B
    assert accounts.current(env.db) == UUID_B and acct_events(env.db)[-1] == (UUID_B, {"from": UUID_A, "to": UUID_B})


def test_hold_reads_a_stale_identity_of_the_old_account_as_the_new_one(env):
    state_file(settings.claude_config_dir, UUID_A)
    assert accounts.observe(env.db, T0) == UUID_A
    accounts.adopt(env.db, ident(UUID_B, "b@example.com", "Bob"), T0 + 1)               # a switch done elsewhere: current is B, the file still says A
    accounts.hold(to=UUID_B, frm=UUID_A, seconds=90)
    assert accounts.held() == {"to": UUID_B, "frm": UUID_A}
    assert accounts.observe(env.db, T0 + 2, force=True) == UUID_B and accounts.current(env.db) == UUID_B, "the stale A is not a switch back"
    assert len(acct_events(env.db)) == 2, "and writes no event"
    state_file(settings.claude_config_dir, UUID_B, "b@example.com", "Bob")              # the file now names the new account: applied as usual, hold stays
    assert accounts.observe(env.db, T0 + 3, force=True) == UUID_B and accounts.held() is not None
    state_file(settings.claude_config_dir, UUID_A)                                      # a stale write-back again
    assert accounts.observe(env.db, T0 + 4, force=True) == UUID_B and accounts.current(env.db) == UUID_B
    state_file(settings.claude_config_dir, UUID_C, "c@example.com", "Cy")               # a real /login to a third account: hold dropped, applied
    assert accounts.observe(env.db, T0 + 5, force=True) == UUID_C and accounts.current(env.db) == UUID_C and accounts.held() is None
    state_file(settings.claude_config_dir, UUID_A)
    assert accounts.observe(env.db, T0 + 6, force=True) == UUID_A, "with the hold gone the old account is an ordinary switch"


def test_the_hold_runs_out_after_its_seconds(env):
    state_file(settings.claude_config_dir, UUID_A)
    accounts.observe(env.db, T0)
    accounts.adopt(env.db, ident(UUID_B, "b@example.com", "Bob"), T0 + 1)
    accounts.hold(to=UUID_B, frm=UUID_A, seconds=90)
    env.later(89)
    assert accounts.observe(env.db, T0 + 2, force=True) == UUID_B
    env.later(2)
    assert accounts.held() is None
    assert accounts.observe(env.db, T0 + 3, force=True) == UUID_A and accounts.current(env.db) == UUID_A


def test_a_hold_without_a_from_account_holds_nothing(env):
    state_file(settings.claude_config_dir, UUID_A)
    accounts.hold(to=UUID_B, frm=None)
    assert accounts.observe(env.db, T0, force=True) == UUID_A and accounts.held() is None


def test_for_reading_honours_the_hold_too(env):
    """A reading that matches no fingerprint forces an identity re-read: a stale A in the file must not take B's reading."""
    state_file(settings.claude_config_dir, UUID_A)
    accounts.observe(env.db, T0)
    accounts.adopt(env.db, ident(UUID_B, "b@example.com", "Bob"), T0 + 1)
    accounts.hold(to=UUID_B, frm=UUID_A)
    assert accounts.for_reading(env.db, rl(T0 + 3000, T0 + 200000), T0 + 2) == UUID_B
    assert accounts.current(env.db) == UUID_B and kv(env.db, "accounts")[UUID_B]["resets_7d"] == T0 + 200000
