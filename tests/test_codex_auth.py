"""#68 and #35, a dead Codex login: the free signals (login_problem.codex_auth_failure, codex_rollout.parse_tail's error item,
tail_auth_evidence / auth_signal, codex.auth_verdict / CodexAgent.auth_probe, parse_headless's auth_failure) and the Tailer raising and
clearing kv login_problem for the rollout's owner account.

Provenance (codex-cli 0.161.0 on the box, 2026-10-09). OBSERVED: a working turn's token_count event (codex_auth/rollout_ok_token_count_0161.jsonl),
the TUI bootstrap error "account/read failed ... timed out" and the /tmp PATH-aliases warning (both texts are in the table below as NOT auth
failures). ASSUMED, never captured: every `codex login status` string and the 401 / revoked refresh token error item
(codex_auth/*_synthetic.*, codex_exec/auth_failed_synthetic.jsonl): they follow Codex's source and docs. codex_auth/login_states_0161.json
marks each record observed or not. Temp CODEX_HOME, a bare DB, no real codex; no test opens or parses an auth.json.
"""
import json
from pathlib import Path

import pytest

from app import login_problem, notify
from app.agents import codex
from app.agents import codex_rollout as cr
from app.config import settings
from app.db import iso
from tests.test_usage_codex import (ID_A, ID_B, KA, KB, NOW, _fresh_rollout_state, accounts_kv, add_codex_row, db, home, jl,  # noqa: F401
                                    make_rollout, row_of, token_line, usage, work)

FIX = Path(__file__).parent / "fixtures"
AUTH_ROLLOUT = FIX / "codex_auth" / "rollout_auth_error_synthetic.jsonl"
LOGIN = json.loads((FIX / "codex_auth" / "login_status_synthetic.json").read_text())
REVOKED = ("unexpected status 401 Unauthorized: Your access token could not be refreshed because your refresh token was revoked. "
           "Please log out and sign in again.")


# ------------------------------------------------------------------ the regex table (#35)
@pytest.mark.parametrize("text,auth", [
    (REVOKED, True),
    ("Your access token could not be refreshed because your refresh token has expired. Please log out and sign in again.", True),
    ("Your access token could not be refreshed because your refresh token was already used. Please log out and sign in again.", True),
    ("unexpected status 401 Unauthorized", True),
    ("Not logged in. Run codex login", True),
    ("unauthorized", True),                                      # a codex_error_info kind
    ("You've hit your usage limit. Try again at 3:14 PM.", False),
    ("429 Too Many Requests: rate limit exceeded", False),
    ("context window exceeded", False),
    ("stream disconnected before completion: error sending request for url (https://example.invalid/responses)", False),
    ("Reconnecting... 2/5 (timeout)", False),
    # OBSERVED on codex-cli 0.161.0 (box checks 86, 87): real failures and noise that are not a dead login
    ("Error: account/read failed during TUI bootstrap: account/read failed: workspace routing discovery timed out (code -32603)", False),
    ('WARNING: proceeding, even though we could not create PATH aliases: Refusing to create helper binaries under temporary dir "/tmp"', False),
    ("Follow these steps to sign in with ChatGPT using device code authorization:", False),
    ("Continue only if you started this login in Codex. If a website or another person gave you this code, cancel.", False),
    # shapes a pane or a Stop payload could carry (ASSUMED): the TUI prefixes an error line with a bullet
    ("■ unexpected status 401 Unauthorized: Your access token could not be refreshed. Please log out and sign in again.", True),
    ("Your session has expired. Please log in again.", True),
    ("", False), (None, False),
])
def test_codex_auth_failure_table(text, auth):
    assert login_problem.codex_auth_failure(text) is auth


def test_fixture_texts_classify_as_the_issue_says():
    exec_dir = FIX / "codex_exec"
    def errors(name):
        out = []
        for line in (exec_dir / name).read_text().splitlines():
            ev = json.loads(line)
            if ev.get("type") == "error":
                out.append(ev["message"])
            elif ev.get("type") == "turn.failed":
                out.append(ev["error"]["message"])
        return out
    assert all(login_problem.codex_auth_failure(t) for t in errors("auth_failed_synthetic.jsonl"))
    assert not any(login_problem.codex_auth_failure(t) for t in errors("rate_limit.jsonl"))
    assert not any(login_problem.codex_auth_failure(t) for t in errors("turn_failed.jsonl"))


# ------------------------------------------------------------------ parse_tail's error item and turn_at
def test_parse_tail_reads_the_newest_error_item_with_no_token_usage_after_it():
    out = cr.parse_tail(AUTH_ROLLOUT.read_text())
    assert out["error"] == {"message": REVOKED, "kind": "unauthorized", "at": "2026-10-04T12:00:03.000Z"}
    assert out["turn_at"] == "2026-10-04T12:00:01.000Z"                     # the newest user_message
    assert out["last_token_usage"]["total"] == 1050                          # the older successful turn is still read


def test_a_token_usage_after_the_error_means_the_error_is_over():
    text = AUTH_ROLLOUT.read_text() + token_line(1_791_200_000, info=usage(2000)) + "\n"
    out = cr.parse_tail(text)
    assert out["error"] is None and out["last_token_usage"]["total"] == 2000


def test_an_error_word_in_a_message_is_not_an_error_item():
    line = jl(NOW, "response_item", {"type": "message", "role": "user", "content": [{"text": "\"error\" 401 unauthorized"}]})
    out = cr.parse_tail(line + "\n")
    assert out["error"] is None and out["turn_at"] is None
    long = jl(NOW, "event_msg", {"type": "error", "message": "x " * 400})
    assert len(cr.parse_tail(long + "\n")["error"]["message"]) == cr.ERROR_MAX


def test_tail_auth_evidence_three_ways():
    assert cr.tail_auth_evidence(cr.parse_tail(AUTH_ROLLOUT.read_text()))["kind"] == "rejected"
    ok = cr.tail_auth_evidence(cr.parse_tail(token_line(NOW, info=usage(10)) + "\n"))
    assert ok["kind"] == "ok" and ok["at"] == pytest.approx(int(NOW), abs=1)
    limit = jl(NOW, "event_msg", {"type": "error", "message": "You've hit your usage limit. Try again at 3:14 PM."})
    assert cr.tail_auth_evidence(cr.parse_tail(limit + "\n")) is None          # not about the login: no evidence either way
    assert cr.tail_auth_evidence(cr.parse_tail("")) is None


# ------------------------------------------------------------------ auth_verdict / auth_probe (#68)
def verdict_of(rc, so, se):
    """The adapter's own `login status` reduction (CodexAgent._login_verdict) of a canned answer, through a fake _run."""
    real = codex._run
    codex._run = lambda argv, timeout=0: (rc, so, se)
    try:
        return codex.CodexAgent._login_verdict("/fake/codex")
    finally:
        codex._run = real


def _states():
    return json.loads((FIX / "codex_auth" / "login_states_0161.json").read_text())


def test_the_states_fixture_says_which_text_is_observed_and_which_is_assumed():
    st = _states()
    assert "ASSUMED" in st["_note"] and "observed" in st["_note"]
    # captured on the box (codex-cli 0.161.0, 2026-10-09): logged in (on stderr), logged out and unreadable via a scratch CODEX_HOME;
    # a revoked login needs a real account the server has revoked, so it stays assumed
    for name, seen in (("logged_in", True), ("logged_out", True), ("unreadable", True), ("expired_revoked", False)):
        ls = st[name]["login_status"]
        assert ls["observed"] is seen and ls["basis"], f"{name}: the fixture must say whether its text was captured on the box"
        roll = st[name]["rollout"]
        assert roll is None or (isinstance(roll["observed"], bool) and roll["basis"])
    assert st["logged_in"]["rollout"]["observed"] is True and st["expired_revoked"]["rollout"]["observed"] is False
    assert all(t["observed"] is True and t["basis"] for t in st["not_an_auth_failure"])
    assert not any(login_problem.codex_auth_failure(t["text"]) for t in st["not_an_auth_failure"])


def test_the_observed_login_status_texts_reduce_as_expected():
    """The exact 0.161.0 answers captured on the box: the logged-in line comes on stderr; an empty or garbage auth.json is an error, never ok."""
    st = _states()
    li, lo, un = st["logged_in"]["login_status"], st["logged_out"]["login_status"], st["unreadable"]["login_status"]
    v = verdict_of(li["rc"], li["stdout"], li["stderr"])
    assert v["loggedIn"] is True and v["authMethod"] == "chatgpt"
    v = verdict_of(lo["rc"], lo["stdout"], lo["stderr"])
    assert v["loggedIn"] is False and "error" not in v
    for key in ("stderr_empty_file", "stderr_garbage_file"):
        v = verdict_of(un["rc"], un["stdout"], un[key])
        assert v["loggedIn"] is False and v["error"] == "codex login status exited 1", key


@pytest.mark.parametrize("state", ["logged_in", "logged_out", "expired_revoked"])
def test_auth_probe_rule_over_the_three_states(state):
    """login status answer + rollout tail, both from fixtures, through the adapter's own reductions (no codex, no auth.json)."""
    rec = _states()[state]
    ls = rec["login_status"]
    login = verdict_of(ls["rc"], ls["stdout"], ls["stderr"])
    evidence = None
    if rec["rollout"]:
        evidence = cr.tail_auth_evidence(cr.parse_tail((FIX / "codex_auth" / rec["rollout"]["file"]).read_text()))
    got = codex.auth_verdict(login, evidence)
    assert got["state"] == rec["expect"]
    assert got["source"] == ("login_status" if rec["expect"] == "missing" else "rollout")
    if rec["expect"] == "rejected":
        assert "401" in got["message"]
    # the login file alone (no rollout evidence) is never ok, in any of the three
    assert codex.auth_verdict(login, None)["state"] in ("missing", "unknown")


def test_the_observed_working_turn_is_read_as_ok_evidence_and_its_stats_survive():
    parsed = cr.parse_tail((FIX / "codex_auth" / "rollout_ok_token_count_0161.jsonl").read_text())
    ev = cr.tail_auth_evidence(parsed)
    assert ev["kind"] == "ok" and ev["at"] == pytest.approx(1791570300, abs=1)
    assert parsed["error"] is None and parsed["last_token_usage"]["total"] == 17038


def test_login_status_with_the_tmp_warning_on_stderr_still_reads_as_logged_in():
    """A scratch-home probe (CODEX_HOME under /tmp) prints Codex's PATH-aliases WARNING on stderr beside the answer (OBSERVED for `login
    --device-auth` on 0.161.0, assumed to be the same for `login status`). The warning must neither make a logged-in answer an error nor a
    logged-out answer look logged in."""
    warn = 'WARNING: proceeding, even though we could not create PATH aliases: Refusing to create helper binaries under temporary dir "/tmp"\n'
    assert verdict_of(0, "Logged in using ChatGPT\n", warn)["loggedIn"] is True
    out = verdict_of(1, "", warn + "Not logged in\n")
    assert out["loggedIn"] is False and "error" not in out
    odd = verdict_of(1, "", warn)                                    # only the warning, exit 1: unreadable, not "missing"
    assert odd["loggedIn"] is False and "error" in odd
    assert codex.auth_verdict(odd, None)["state"] == "unknown"


def test_auth_verdict_states_from_the_login_status_fixtures():
    missing, unreadable, present = (verdict_of(LOGIN[k]["rc"], LOGIN[k]["stdout"], LOGIN[k]["stderr"])
                                    for k in ("missing", "unreadable", "present"))
    assert codex.auth_verdict(missing, None) == {"state": "missing", "source": "login_status"}
    assert codex.auth_verdict(unreadable, None) == {"state": "unknown", "source": "login_status"}
    # a login file that merely exists is never ok: unknown until a free signal supports it
    assert codex.auth_verdict(present, None) == {"state": "unknown", "source": "login_status"}
    rejected = codex.auth_verdict(present, {"kind": "rejected", "at": 5.0, "message": REVOKED})
    assert rejected == {"state": "rejected", "source": "rollout", "at": 5.0, "message": REVOKED}
    assert codex.auth_verdict(present, {"kind": "ok", "at": 6.0}) == {"state": "ok", "source": "rollout", "at": 6.0}
    assert codex.auth_verdict(missing, {"kind": "ok", "at": 6.0})["state"] == "missing"     # no file beats an old success
    assert codex.auth_verdict(None, None)["state"] == "missing"


class _Probe:
    """A fake codex binary that answers `login status` only: any other argv (exec above all) fails the test."""

    def __init__(self, tmp_path, monkeypatch, answer):
        exe = tmp_path / "bin" / "codex"
        exe.parent.mkdir(exist_ok=True)
        exe.write_text("#!/bin/sh\n")
        self.calls = []
        monkeypatch.setattr(settings, "codex_bin", lambda: str(exe))
        def run(argv, timeout=0):
            self.calls.append(tuple(argv[1:]))
            if tuple(argv[1:]) == ("login", "status"):
                return answer
            if tuple(argv[1:]) == ("--version",):
                return 0, "codex-cli 0.160.1\n", ""
            raise AssertionError(f"auth_probe ran {argv}")
        monkeypatch.setattr(codex, "_run", run)
        codex.reset_caches()


def test_auth_probe_uses_only_login_status_and_rollout_tails(tmp_path, monkeypatch, db, home):
    from app import agents
    ag = agents.get("codex")
    p = _Probe(tmp_path, monkeypatch, (0, "Logged in using ChatGPT\n", ""))
    assert ag.auth_probe(db, now=NOW) == {"state": "unknown", "source": "login_status"}          # no rollout yet
    make_rollout(home, created=NOW - 60, lines=[jl(NOW - 30, "event_msg", {"type": "user_message", "message": "hi"}),
                                                jl(NOW - 20, "event_msg", {"type": "error", "message": REVOKED})])
    got = ag.auth_probe(db, now=NOW)
    assert got["state"] == "rejected" and got["source"] == "rollout"
    make_rollout(home, created=NOW - 10, lines=[token_line(NOW - 5, info=usage(100))])
    assert ag.auth_probe(db, now=NOW)["state"] == "ok"                                           # the newest evidence wins
    db.kv_set("codex_account_current", {"key": KA, "since": iso(NOW - 1)})                        # a switch after both: they say nothing now
    assert ag.auth_probe(db, now=NOW)["state"] == "unknown"
    assert set(p.calls) <= {("login", "status"), ("--version",)}
    codex.reset_caches()
    p2 = _Probe(tmp_path, monkeypatch, (1, "", "Not logged in\n"))
    assert ag.auth_probe(db, now=NOW) == {"state": "missing", "source": "login_status"}
    assert not any(c and c[0] == "exec" for c in p.calls + p2.calls)


def test_auth_probe_never_opens_auth_json(tmp_path, monkeypatch, db, home):
    from app import agents
    (home / "auth.json").write_bytes(b"\x00opaque-not-json\xff")
    opened = []
    real_open = open
    def spy(file, *a, **k):
        if str(file).endswith("auth.json"):
            opened.append(str(file))
        return real_open(file, *a, **k)
    monkeypatch.setattr("builtins.open", spy)
    _Probe(tmp_path, monkeypatch, (0, "Logged in using ChatGPT\n", ""))
    make_rollout(home, created=NOW - 60, lines=[jl(NOW - 20, "event_msg", {"type": "error", "message": REVOKED})])
    assert agents.get("codex").auth_probe(db, now=NOW)["state"] == "rejected"
    assert opened == []


def test_parse_headless_flags_an_auth_failure_and_never_a_rate_limit():
    from app import agents
    ag = agents.get("codex")
    text = (FIX / "codex_exec" / "auth_failed_synthetic.jsonl").read_text()
    r = ag.parse_headless(text, "", 1)
    assert r["is_error"] and r["auth_failure"] is True and r["rate_limited"] is False
    for name in ("rate_limit.jsonl", "turn_failed.jsonl", "success.jsonl"):
        assert ag.parse_headless((FIX / "codex_exec" / name).read_text(), "", 1 if name != "success.jsonl" else 0)["auth_failure"] is False


# ------------------------------------------------------------------ the Tailer raises and clears (#35)
@pytest.fixture
def notices(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "notify_login_problem", lambda *a, **k: sent.append((a, k)) or True)
    return sent


def auth_rollout(home, work, account=ID_A, err_at=None):
    err_at = NOW - 10 if err_at is None else err_at
    return make_rollout(home, cwd=work, account=account, created=NOW - 120,
                        lines=[jl(NOW - 100, "event_msg", {"type": "user_message", "message": "go"}),
                               token_line(NOW - 90, info=usage(100)),
                               jl(err_at - 2, "event_msg", {"type": "user_message", "message": "again"}),
                               jl(err_at, "event_msg", {"type": "error", "message": REVOKED})])


def test_an_auth_error_item_raises_login_problem_for_the_owner_within_one_tick(db, home, work, notices):
    accounts_kv(db, current=KB)                                  # the current account is B, the rollout is A's: A is named
    p = auth_rollout(home, work)
    add_codex_row(db, work, flags={"transcript_path": str(p), "hook_seen": True})
    t = cr.Tailer()
    t.tail_rows(db, NOW)
    prob = login_problem.get(db)
    assert prob["agent"] == "codex" and prob["account"] == KA and prob["session"] == "shop--api--s1"
    assert "401" in prob["message"] and len(prob["message"]) <= login_problem.MESSAGE_MAX
    assert len(notices) == 1 and notices[0][1] == {"agent": "codex"} and notices[0][0][:3] == (KA, "Work", "shop--api--s1")
    events = [e for e in db.recent_events(20) if e.get("event") == "LoginProblem"]
    assert len(events) == 1
    login_problem.clear(db)                                      # dismissed by the person: the same error item does not come back
    t.tail_rows(db, NOW + 6)
    assert login_problem.get(db) is None and len(notices) == 1


def test_a_later_token_usage_clears_it_and_a_rate_limit_raises_nothing(db, home, work, notices):
    accounts_kv(db, current=KA)
    p = auth_rollout(home, work)
    add_codex_row(db, work, flags={"transcript_path": str(p), "hook_seen": True})
    t = cr.Tailer()
    t.tail_rows(db, NOW)
    assert login_problem.get(db)["account"] == KA
    with open(p, "a") as f:
        f.write(token_line(NOW + 1, info=usage(500)) + "\n")
    t.tail_rows(db, NOW + 6)
    assert login_problem.get(db) is None
    with open(p, "a") as f:
        f.write(jl(NOW + 7, "event_msg", {"type": "error", "message": "You've hit your usage limit. Try again at 3:14 PM."}) + "\n")
    t.tail_rows(db, NOW + 12)
    assert login_problem.get(db) is None and len(notices) == 1


def test_an_old_error_item_is_not_replayed_after_a_restart(db, home, work, notices):
    accounts_kv(db, current=KA)
    p = auth_rollout(home, work, err_at=NOW - cr.AUTH_FRESH - 60)
    add_codex_row(db, work, flags={"transcript_path": str(p), "hook_seen": True})
    cr.Tailer().tail_rows(db, NOW)
    assert login_problem.get(db) is None and notices == []


def test_a_claude_problem_is_not_cleared_by_a_codex_turn(db, home, work):
    accounts_kv(db, current=KA)
    login_problem.raise_(db, agent="claude", account="c" * 24, session="x", message="OAuth token has expired", now=NOW - 60)
    p = make_rollout(home, cwd=work, account=ID_A, lines=[token_line(NOW - 1, info=usage(10))])
    add_codex_row(db, work, flags={"transcript_path": str(p), "hook_seen": True})
    cr.Tailer().tail_rows(db, NOW)
    assert login_problem.get(db)["agent"] == "claude"
