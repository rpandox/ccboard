"""#68 and #35, a dead Codex login: the free signals (login_problem.codex_auth_failure, codex_rollout.parse_tail's error item,
tail_auth_evidence / auth_signal, codex.auth_verdict / CodexAgent.auth_probe, parse_headless's auth_failure) and the Tailer raising and
clearing kv login_problem for the rollout's owner account.

Provenance (codex-cli 0.161.0 on the box, 2026-10-09 and 2026-10-10). OBSERVED: a working turn's token_count event
(codex_auth/rollout_ok_token_count_0161.jsonl), the TUI bootstrap error "account/read failed ... timed out" and the /tmp PATH-aliases warning (both
texts are in the table below as NOT auth failures), the `codex login status` answers (login_states_0161.json) and the `codex exec --json` run with no
usable login (codex_exec/auth_failed_0161.jsonl: ten 401 retries, then turn.failed, exit 1; the three logged-out states look the same).
FROM SOURCE, never captured (v0.5.105): the texts of a login the SERVER rejects (expired, reused or revoked refresh token, a 401 with an auth error code)
are the strings of openai/codex at commit 979011409de0a60b52f179721948e65531d26144 (tag rust-v0.161.0), listed with their files in
codex_auth/login_states_0161.json (expired_revoked.from_source) and built into codex_exec/auth_rejected_*.jsonl. Only the texts are the source's: the event
sequence around them is assumed. The error item in codex_auth/rollout_auth_error_synthetic.jsonl has the source's text in a SYNTHETIC shape (Codex 0.161.0
does not persist error events to a rollout at all) and the login_status_synthetic.json strings are assumed. codex_auth/login_states_0161.json marks each
record observed or not. Temp CODEX_HOME, a bare DB, no real codex; no test opens or parses an auth.json.
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
REVOKED = ("Your access token could not be refreshed because your refresh token was revoked. "         # manager.rs:208 @ 9790114, shown bare
           "Please log out and sign in again.")
ACCOUNT_MISMATCH = ("Your access token could not be refreshed because you have since logged out or signed in to another account. "
                    "Please sign in again.")                                                              # manager.rs:211: not a dead login
REJECTED_FIXTURES = ["auth_rejected_refresh_expired.jsonl", "auth_rejected_refresh_reused.jsonl", "auth_rejected_refresh_revoked.jsonl",
                     "auth_rejected_refresh_unknown.jsonl", "auth_rejected_responses_401_token_expired.jsonl"]


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
    # FROM SOURCE (openai/codex 9790114): the texts of a login the server rejects, see login_states_0161.json expired_revoked.from_source
    ("Your access token could not be refreshed. Please log out and sign in again.", True),                  # manager.rs:209
    ("Your authentication session could not be refreshed automatically. Please log out and sign in again.", True),   # cloud-config service.rs:44
    ('unexpected status 401 Unauthorized: {"detail":"Unauthorized"}, url: <scrubbed>, auth error code: token_expired', True),   # error.rs:633
    ("unexpected status 403 Forbidden: nope, url: <scrubbed>, auth error code: token_revoked", True),
    ("Your refresh token has already been used to generate a new access token. Please try signing in again.", True),   # util.rs, server wording
    ("refresh_token_invalidated", True),                                                                    # the server's code
    ("Not signed in. Please run 'codex login' to sign in with ChatGPT, then re-run 'codex cloud'.", True),  # cloud-tasks lib.rs
    # not a dead login: the account-switch mismatch, other agents' logins, ordinary failures
    (ACCOUNT_MISMATCH, False),
    ("MCP OAuth refresh token was rejected; reauthorization required", False),                              # rmcp-client: an MCP server's login
    ("enterprise IdP session has no refresh token; sign in again", False),
    ("exceeded retry limit, last status: 429 Too Many Requests, request id: <scrubbed>", False),
    ("Quota exceeded. Check your plan and billing details.", False),
    ("We’re currently experiencing high demand, which may cause temporary errors.", False),
    ("unexpected status 500 Internal Server Error: upstream failure, url: <scrubbed>, request id: <scrubbed>", False),
    ("stream disconnected before completion: connection reset by peer", False),
    ("I can't log in to the shop page, can you look at the form?", False),                                  # a prompt, not an error
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
    assert all(login_problem.codex_auth_failure(t) for t in errors("auth_failed_0161.jsonl"))
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
    assert "FROM SOURCE" in st["_note"] and "observed" in st["_note"] and "979011409de0a60b52f179721948e65531d26144" in st["_note"]
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
        assert "refresh token was revoked" in got["message"]
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
    text = (FIX / "codex_exec" / "auth_failed_0161.jsonl").read_text()
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


def auth_rollout(home, work, account=ID_A, err_at=None, message=REVOKED):
    err_at = NOW - 10 if err_at is None else err_at
    return make_rollout(home, cwd=work, account=account, created=NOW - 120,
                        lines=[jl(NOW - 100, "event_msg", {"type": "user_message", "message": "go"}),
                               token_line(NOW - 90, info=usage(100)),
                               jl(err_at - 2, "event_msg", {"type": "user_message", "message": "again"}),
                               jl(err_at, "event_msg", {"type": "error", "message": message})])


def test_an_auth_error_item_raises_login_problem_for_the_owner_within_one_tick(db, home, work, notices):
    accounts_kv(db, current=KB)                                  # the current account is B, the rollout is A's: A is named
    p = auth_rollout(home, work)
    add_codex_row(db, work, flags={"transcript_path": str(p), "hook_seen": True})
    t = cr.Tailer()
    t.tail_rows(db, NOW)
    prob = login_problem.get(db)
    assert prob["agent"] == "codex" and prob["account"] == KA and prob["session"] == "shop--api--s1"
    assert "refresh token was revoked" in prob["message"] and len(prob["message"]) <= login_problem.MESSAGE_MAX
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


# ------------------------------------------------------------------ the observed `codex exec` capture (#68, codex-cli 0.161.0, 2026-10-10)
def _exec_events(name="auth_failed_0161.jsonl"):
    return [json.loads(line) for line in (FIX / "codex_exec" / name).read_text().splitlines()]


def test_the_observed_exec_capture_has_the_recorded_shape_and_no_ids():
    import re
    ev = _exec_events()
    kinds = [e["type"] for e in ev]
    assert kinds == (["thread.started", "turn.started"] + ["error"] * 4 + ["item.completed"] + ["error"] * 5 + ["error", "turn.failed"])
    assert ev[6]["item"]["type"] == "error" and ev[6]["item"]["message"].startswith("Falling back from WebSockets to HTTPS transport")
    assert [e["message"].split(" (")[0] for e in ev[2:6]] == [f"Reconnecting... {n}/5" for n in (1, 2, 3, 4)]
    assert ev[-1]["error"]["message"] == "unexpected status 401 Unauthorized: Missing bearer or basic authentication in header"
    raw = (FIX / "codex_exec" / "auth_failed_0161.jsonl").read_text()
    assert "<scrubbed>" in raw and not re.search(r"[0-9a-f]{16}-[A-Z]{3}", raw)                 # a cf-ray value looks like <hex>-<AIRPORT>
    assert not (FIX / "codex_exec" / "auth_failed_synthetic.jsonl").exists()


def test_the_observed_exec_run_is_an_auth_failure_with_the_401_as_its_text():
    from app import agents
    r = agents.get("codex").parse_headless((FIX / "codex_exec" / "auth_failed_0161.jsonl").read_text(),
                                           "failed to connect to websocket: HTTP error: 401 Unauthorized\n", 1)
    assert r["is_error"] and r["subtype"] == "turn_failed" and r["auth_failure"] is True and r["rate_limited"] is False
    assert r["session_id"] == "00000000-0000-0000-0000-000000000000" and r["turns"] is None
    # the run's verdict, not twelve lines of retry noise
    assert r["text"] == "unexpected status 401 Unauthorized: Missing bearer or basic authentication in header"


def test_a_401_retry_that_ended_in_a_different_failure_is_not_a_dead_login():
    """turn.failed decides when there is one: a transient 401 in a retry line followed by a context overflow is not a login problem."""
    from app import agents
    lines = [json.dumps(e) for e in _exec_events()[:4]]
    lines.append(json.dumps({"type": "turn.failed", "error": {"message": "context window exceeded"}}))
    r = agents.get("codex").parse_headless("\n".join(lines) + "\n", "", 1)
    assert r["is_error"] and r["auth_failure"] is False and r["text"] == "context window exceeded"


def test_an_exec_run_that_exits_0_after_a_reconnect_401_is_not_an_error():
    from app import agents
    ev = _exec_events()[:3] + [{"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
                               {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}]
    r = agents.get("codex").parse_headless("\n".join(json.dumps(e) for e in ev) + "\n", "", 0)
    assert r["is_error"] is False and r["auth_failure"] is False and r["text"] == "ok"


def test_the_states_fixture_records_the_exec_capture_as_observed_and_the_rejected_login_as_assumed():
    st = _states()
    ex = st["exec_no_login"]
    assert ex["observed"] is True and ex["version"] == "codex-cli 0.161.0" and ex["date"] == "2026-10-10" and ex["rc"] == 1
    assert ex["tells_the_states_apart"] is False and len(ex["states_that_look_the_same"]) == 3
    assert (FIX / "codex_auth" / ex["file"]).resolve().exists()
    assert st["expired_revoked"]["exec"]["observed"] is False and "ASSUMED" in st["expired_revoked"]["exec"]["basis"]
    assert st["expired_revoked"]["login_status"]["observed"] is False
    # the second garbage text seen on 2026-10-10 reads as an error as well, never as logged in
    un = st["unreadable"]["login_status"]
    v = verdict_of(un["rc"], un["stdout"], un["stderr_garbage_text_file"])
    assert v["loggedIn"] is False and v["error"] == "codex login status exited 1"


# ------------------------------------------------------------------ a login the server rejects, FROM SOURCE (v0.5.105, #35, #68)
# The fixtures are built from openai/codex at SOURCE_SHA (tag rust-v0.161.0), not observed on a dead login.
SOURCE_SHA = "979011409de0a60b52f179721948e65531d26144"


def _fixture_text(name):
    return (FIX / "codex_exec" / name).read_text()


def _fixture_message(name):
    return [e for e in _exec_events(name) if e["type"] == "turn.failed"][0]["error"]["message"]


@pytest.mark.parametrize("name", REJECTED_FIXTURES)
def test_a_rejected_login_fixture_is_an_auth_failure_through_parse_headless(name):
    from app import agents
    r = agents.get("codex").parse_headless(_fixture_text(name), "", 1)
    assert r["is_error"] and r["subtype"] == "turn_failed" and r["auth_failure"] is True and r["rate_limited"] is False
    assert r["text"] == _fixture_message(name) and r["turns"] is None


@pytest.mark.parametrize("name", REJECTED_FIXTURES)
def test_a_rejected_login_fixture_text_in_a_rollout_error_item_is_rejected_evidence(name):
    msg = _fixture_message(name)
    item = jl(NOW - 5, "event_msg", {"type": "error", "message": msg, "codex_error_info": "unauthorized"})
    ev = cr.tail_auth_evidence(cr.parse_tail(item + "\n"))
    assert ev is not None and ev["kind"] == "rejected" and ev["message"] == msg


@pytest.mark.parametrize("name", REJECTED_FIXTURES)
def test_a_rejected_login_fixture_text_raises_login_problem_through_the_tailer(name, db, home, work, notices):
    accounts_kv(db, current=KA)
    p = auth_rollout(home, work, message=_fixture_message(name))
    add_codex_row(db, work, flags={"transcript_path": str(p), "hook_seen": True})
    cr.Tailer().tail_rows(db, NOW)
    prob = login_problem.get(db)
    assert prob["agent"] == "codex" and prob["account"] == KA and prob["message"] == _fixture_message(name)[:login_problem.MESSAGE_MAX]
    assert len(notices) == 1


def test_the_rejected_login_fixtures_have_the_assumed_sequence_and_no_ids():
    import re
    for name in REJECTED_FIXTURES:
        ev = _exec_events(name)
        assert [e["type"] for e in ev] == ["thread.started", "turn.started", "error", "turn.failed"]
        assert ev[2]["message"] == ev[3]["error"]["message"] and ev[0]["thread_id"] == "00000000-0000-0000-0000-000000000000"
        assert not re.search(r"[0-9a-f]{16}-[A-Z]{3}|eyJ|Bearer|@", _fixture_text(name))


def test_the_states_file_pins_the_source_commit_and_marks_every_string_as_not_observed():
    st = _states()["expired_revoked"]
    assert st["from_source"]["commit"] == SOURCE_SHA and st["exec"]["source_sha"] == SOURCE_SHA and st["exec"]["observed"] is False
    assert st["rollout"]["observed"] is False and st["login_status"]["observed"] is False
    assert "STDERR" in st["login_status"]["basis"] and st["login_status"]["stdout"] == "" and st["login_status"]["stderr"] == "Logged in using ChatGPT\n"
    assert {Path(r).name for r in st["exec"]["files"]} == set(REJECTED_FIXTURES)
    for rel in st["exec"]["files"]:
        assert (FIX / "codex_auth" / rel).resolve().exists()
    # every source string with a verdict agrees with the matcher
    texts = {s["name"]: s for s in st["from_source"]["strings"]}
    for key in ("REFRESH_TOKEN_EXPIRED_MESSAGE", "REFRESH_TOKEN_REUSED_MESSAGE", "REFRESH_TOKEN_INVALIDATED_MESSAGE", "REFRESH_TOKEN_UNKNOWN_MESSAGE"):
        assert login_problem.codex_auth_failure(texts[key]["text"]) is True, key
    assert login_problem.codex_auth_failure(texts["REFRESH_TOKEN_ACCOUNT_MISMATCH_MESSAGE"]["text"]) is False
    assert texts["REFRESH_TOKEN_INVALIDATED_MESSAGE"]["text"] == REVOKED
    assert not any(login_problem.codex_auth_failure(t["text"]) for t in _states()["not_an_auth_failure_from_source"])


# the negative list, through the real parsers: none of these raises a login problem
NOT_A_LOGIN = [
    ("rate_limit", "You've hit your usage limit. Upgrade to Pro or try again at 3:14 PM."),
    ("network", "stream disconnected before completion: error sending request for url (<scrubbed>)"),
    ("retry_limit", "exceeded retry limit, last status: 429 Too Many Requests, request id: <scrubbed>"),
    ("server_error", "unexpected status 500 Internal Server Error: upstream failure, url: <scrubbed>"),
    ("context", "Codex ran out of room in the model's context window. Start a new thread or clear earlier history before retrying."),
    ("generic", "turn failed"),
    ("account_switch", ACCOUNT_MISMATCH),
    ("mcp_login", "MCP OAuth refresh token was rejected; reauthorization required"),
]


@pytest.mark.parametrize("label,msg", NOT_A_LOGIN, ids=[n for n, _ in NOT_A_LOGIN])
def test_an_ordinary_failure_is_not_a_dead_login_through_parse_headless(label, msg):
    from app import agents
    ev = [{"type": "thread.started", "thread_id": "00000000-0000-0000-0000-000000000000"}, {"type": "turn.started"},
          {"type": "error", "message": msg}, {"type": "turn.failed", "error": {"message": msg}}]
    r = agents.get("codex").parse_headless("\n".join(json.dumps(e) for e in ev) + "\n", "", 1)
    assert r["is_error"] and r["auth_failure"] is False


@pytest.mark.parametrize("label,msg", NOT_A_LOGIN, ids=[n for n, _ in NOT_A_LOGIN])
def test_an_ordinary_failure_in_a_rollout_is_no_login_evidence(label, msg):
    item = jl(NOW - 5, "event_msg", {"type": "error", "message": msg})
    assert cr.tail_auth_evidence(cr.parse_tail(item + "\n")) is None


def test_a_prompt_or_a_reply_that_says_login_raises_nothing(db, home, work, notices):
    from app import agents
    said = "my login is broken: sign in again, log out and sign in again, unauthorized 401, codex login, refresh token revoked"
    # headless: the words are in the agent's reply of a run that succeeded
    ev = [{"type": "thread.started", "thread_id": "00000000-0000-0000-0000-000000000000"}, {"type": "turn.started"},
          {"type": "item.completed", "item": {"type": "agent_message", "text": said}},
          {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}]
    r = agents.get("codex").parse_headless("\n".join(json.dumps(e) for e in ev) + "\n", "", 0)
    assert r["is_error"] is False and r["auth_failure"] is False and r["text"] == said
    # rollout: the words are in a user message and an assistant message, no error item
    accounts_kv(db, current=KA)
    p = make_rollout(home, cwd=work, account=ID_A, created=NOW - 120,
                     lines=[jl(NOW - 100, "event_msg", {"type": "user_message", "message": said}),
                            jl(NOW - 90, "event_msg", {"type": "agent_message", "message": said}),
                            jl(NOW - 80, "response_item", {"type": "message", "role": "assistant", "content": [{"text": said}]})])
    assert cr.parse_tail(p.read_text())["error"] is None
    add_codex_row(db, work, flags={"transcript_path": str(p), "hook_seen": True})
    cr.Tailer().tail_rows(db, NOW)
    assert login_problem.get(db) is None and notices == []


# ------------------------------------------------------------------ a rejected login ON SCREEN (interactive sessions, #35; from the source, not observed)
# Codex 0.145 to 0.161 keeps error events out of the rollout (rollout/src/policy.rs), so an interactive session shows a dead login only on the pane:
# the TUI draws an error as one red line "■ {message}" (tui/src/history_cell/notices.rs:331 new_error_event @ 9790114). The screens below are built
# from that rule and the source's strings, not captured.
from app.agents import codex_pane  # noqa: E402

REAL_PANE_CAPTURE = cr._pane_capture                 # before the conftest stub

EXPIRED = ("Your access token could not be refreshed because your refresh token has expired. "
           "Please log out and sign in again.")                                                      # manager.rs:206
TOKEN_401 = ('unexpected status 401 Unauthorized: {"detail":"Unauthorized"}, url: <scrubbed>, request id: <scrubbed>, '
             'auth error code: token_expired')                                                      # error.rs:633


def screen(*blocks):
    return "\n".join(["› go", "", *blocks, "", "  gpt-6.1-sol default · ~/work · Reply wit"]) + "\n"


@pytest.fixture
def pane(monkeypatch):
    box = {"text": ""}
    monkeypatch.setattr(cr, "_pane_capture", lambda name: box["text"])
    return box


def pane_row(db, home, work):
    accounts_kv(db, current=KA)
    p = make_rollout(home, cwd=work, account=ID_A, created=NOW - 120,
                     lines=[jl(NOW - 100, "event_msg", {"type": "user_message", "message": "go"}), token_line(NOW - 90, info=usage(100))])
    add_codex_row(db, work, flags={"transcript_path": str(p), "hook_seen": True})
    return p


def test_the_tui_error_line_is_read_with_its_wrapped_continuation():
    assert codex_pane.error_messages(screen("■ one", "■ two words", "  more words")) == ["one", "two words more words"]
    assert codex_pane.error_messages(screen("• ■ not at the start", "  ■no space", "plain ■ in the middle")) == []
    text = screen("■ Your access token could not be refreshed because your refresh token has", "  expired. Please log out and sign in again.")
    assert codex_pane.rejected_login(text) == EXPIRED
    assert codex_pane.rejected_login(screen("■ " + TOKEN_401)) == TOKEN_401
    assert codex_pane.rejected_login(screen("■ " + ACCOUNT_MISMATCH)) is None


def test_a_prefixed_refresh_failure_on_screen_raises_once(db, home, work, notices, pane):
    pane_row(db, home, work)
    pane["text"] = screen("■ " + EXPIRED)
    t = cr.Tailer()
    t.tail_rows(db, NOW)
    prob = login_problem.get(db)
    assert prob["agent"] == "codex" and prob["account"] == KA and prob["session"] == "shop--api--s1" and prob["message"] == EXPIRED
    assert len(notices) == 1 and notices[0][1] == {"agent": "codex"} and notices[0][0][:3] == (KA, "Work", "shop--api--s1")
    assert len([e for e in db.recent_events(20) if e.get("event") == "LoginProblem"]) == 1
    t.tail_rows(db, NOW + 6)                                      # the same screen on the next tick: nothing new
    cr.Tailer().tail_rows(db, NOW + 12)                           # ... and after a restart of the board
    assert len(notices) == 1 and len([e for e in db.recent_events(20) if e.get("event") == "LoginProblem"]) == 1
    login_problem.clear(db)                                       # dismissed while the line is still on screen: it does not come back
    t.tail_rows(db, NOW + 18)
    assert login_problem.get(db) is None and len(notices) == 1


def test_the_401_with_an_auth_error_code_on_screen_raises(db, home, work, notices, pane):
    pane_row(db, home, work)
    pane["text"] = screen("■ " + TOKEN_401)
    cr.Tailer().tail_rows(db, NOW)
    assert login_problem.get(db)["agent"] == "codex" and "token_expired" in login_problem.get(db)["message"] and len(notices) == 1


def test_a_wrapped_line_raises_and_a_line_that_comes_back_after_it_left_raises_again(db, home, work, notices, pane):
    pane_row(db, home, work)
    t = cr.Tailer()
    pane["text"] = screen("■ Your access token could not be refreshed because your refresh token has", "  expired. Please log out and sign in again.")
    t.tail_rows(db, NOW)
    assert login_problem.get(db)["message"] == EXPIRED and len(notices) == 1
    pane["text"] = screen("• fine now")                          # scrolled away
    t.tail_rows(db, NOW + 6)
    login_problem.clear(db)
    pane["text"] = screen("■ " + EXPIRED)                        # a new failure
    t.tail_rows(db, NOW + 12)
    assert login_problem.get(db) is not None and len(notices) == 2


def test_the_same_words_without_the_error_prefix_never_raise(db, home, work, notices, pane):
    pane_row(db, home, work)
    pane["text"] = screen("• You could see: " + EXPIRED, "", "• " + EXPIRED,
                          "  " + EXPIRED, "", "› " + EXPIRED, "", "+    msg = \"" + EXPIRED + "\"", "-    msg = \"" + TOKEN_401 + "\"",
                          "> " + EXPIRED, "# ■ " + EXPIRED, "x ■ " + EXPIRED, "Error: " + EXPIRED)
    cr.Tailer().tail_rows(db, NOW)
    assert login_problem.get(db) is None and notices == []


PANE_NEGATIVES = NOT_A_LOGIN + [("mcp_enterprise", "enterprise IdP session has no refresh token; sign in again"),
                                ("plain_401", "unexpected status 401 Unauthorized: Missing bearer or basic authentication in header")]


@pytest.mark.parametrize("label,msg", PANE_NEGATIVES, ids=[n for n, _ in PANE_NEGATIVES])
def test_a_prefixed_line_that_is_not_a_rejected_refresh_never_raises(label, msg, db, home, work, notices, pane):
    pane_row(db, home, work)
    pane["text"] = screen("■ " + msg)
    cr.Tailer().tail_rows(db, NOW)
    assert login_problem.get(db) is None and notices == []


def test_a_later_token_usage_clears_it_and_the_old_line_does_not_raise_again(db, home, work, notices, pane):
    p = pane_row(db, home, work)
    pane["text"] = screen("■ " + EXPIRED)
    t = cr.Tailer()
    t.tail_rows(db, NOW)
    assert login_problem.get(db) is not None
    with open(p, "a") as f:
        f.write(token_line(NOW + 1, info=usage(500)) + "\n")      # a turn that worked after it: the same rule as the rollout path
    t.tail_rows(db, NOW + 6)
    t.tail_rows(db, NOW + 12)
    assert login_problem.get(db) is None and len(notices) == 1


def test_the_pane_is_only_read_through_the_seam_and_a_failed_capture_is_nothing(monkeypatch):
    from app import tmux
    monkeypatch.setattr(tmux, "capture", lambda name, lines=0, **k: (_ for _ in ()).throw(tmux.TmuxDown("no server")))
    assert REAL_PANE_CAPTURE("shop--api--s1") == ""
    asked = []
    monkeypatch.setattr(tmux, "capture", lambda name, lines=0, **k: asked.append((name, lines)) or screen("■ " + EXPIRED))
    assert REAL_PANE_CAPTURE("shop--api--s1") == screen("■ " + EXPIRED) and asked == [("shop--api--s1", codex_pane.PANE_LINES)]
