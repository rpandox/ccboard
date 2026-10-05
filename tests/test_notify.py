import json

import pytest

from app import notify, push
from app.config import settings
from app.db import DB

PUB = "https://box.ts.net:8443"
TMUX = "shop--api--s1"
ROW = {"id": 7, "row_id": 7, "tmux_name": TMUX, "project": "shop", "repo": "api", "name": "s1", "agent": "claude",
       "launcher": "claude", "last_prompt": "fix the login redirect", "flags": {}}
TASK = {"title": "Fix login redirect", "phase": "running"}


class Sent(list):
    """What reached ntfy, as (url, body) pairs; `.clock[0]` is the fake clock."""
    clock: list


@pytest.fixture
def capture(monkeypatch):
    """ntfy on (every publish lands in the returned list), no DB, a clock the test moves (`capture.clock[0]`)."""
    sent = Sent()

    class R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=5):
        sent.append((req.full_url, json.loads(req.data)))
        return R()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(settings, "ntfy_url", "http://127.0.0.1:2586")
    monkeypatch.setattr(settings, "ntfy_topic", "ccboard")
    monkeypatch.setattr(settings, "ntfy_public_url", "https://box.ts.net:8444")
    monkeypatch.setattr(settings, "public_url", PUB)
    monkeypatch.setattr(notify, "_db", None)             # a stale DB from an earlier test must not leak in
    notify._last.clear()
    notify._rl_mem.clear()
    notify._rl_last.clear()
    notify._login_told.clear()
    clock = [1000.0]
    monkeypatch.setattr(notify, "_clock", lambda: clock[0])
    sent.clock = clock
    return sent


@pytest.fixture
def db(tmp_path, capture, monkeypatch):
    """A real DB behind the notify module, with the session row `shop--api--s1` (a claude row that last asked for a login fix)."""
    d = DB(tmp_path / "n.db")
    d.add_session(tmux_name=TMUX, project="shop", repo="api", name="s1", launcher="claude")
    d.set_state(TMUX, "working", "UserPromptSubmit", prompt="fix the login redirect")
    monkeypatch.setattr(notify, "_db", d)
    return d


def acts(n):
    return {a["label"]: a for a in n.actions}


# ---------------------------------------------------------------- build(): one Notice per state

def test_waiting_permission_notice(capture):
    n = notify.build(ROW, TASK, "waiting", "permission_prompt", "Claude needs your permission to use Bash",
                     perm={"id": 5, "summary": "Bash: npm test"})
    assert n.title == "◆ shop/api · s1: needs you"
    assert n.body == "Fix login redirect\n› fix the login redirect\n? Bash: npm test"
    assert n.click == f"{PUB}/#/s/{TMUX}" and n.url == n.click and n.tag == TMUX and n.kind == "permission"
    assert (n.priority, n.tags) == (4, ["bell", "key"])
    assert [a["label"] for a in n.actions] == ["Allow", "Deny", "Terminal"]
    a = acts(n)
    assert a["Allow"]["url"] == f"{PUB}/api/permission/5/allow" and a["Allow"]["method"] == "POST"
    assert a["Allow"]["headers"] == {"X-CCBoard": "1"} and a["Allow"]["clear"] is True and a["Allow"]["action"] == "http"
    assert a["Deny"]["url"] == f"{PUB}/api/permission/5/deny"
    assert a["Terminal"] == {"action": "view", "label": "Terminal", "url": f"{PUB}/term/{TMUX}"}
    assert [w["action"] for w in n.web_actions] == ["allow", "deny", "terminal"] and n.perm_id == 5


def test_permission_notification_without_a_request_uses_the_message_and_has_no_allow_deny(capture):
    n = notify.build(ROW, None, "waiting", "permission_prompt", "Claude needs your permission to use Bash")
    assert n.kind == "permission" and (n.priority, n.tags) == (4, ["bell", "key"])
    assert n.body == "› fix the login redirect\n? Claude needs your permission to use Bash"
    assert [a["label"] for a in n.actions] == ["Terminal", "Ack"] and n.perm_id is None


@pytest.mark.parametrize("kind,want", [("idle_prompt", "idle"), ("idle", "idle"), ("elicitation_dialog", "elicitation"),
                                       ("elicitation_url_dialog", "elicitation"), ("elicitation", "elicitation"), (None, "waiting")])
def test_waiting_without_permission_is_priority_4_bell(capture, kind, want):
    n = notify.build(ROW, TASK, "waiting", kind, "Claude is waiting for your input")
    assert n.kind == want and (n.priority, n.tags) == (4, ["bell"])
    assert n.body.splitlines()[-1] == "? Claude is waiting for your input"
    ack = acts(n)["Ack"]
    assert ack["url"] == f"{PUB}/api/sessions/{TMUX}/ack" and ack["method"] == "POST" and ack["headers"] == {"X-CCBoard": "1"}
    assert [a["label"] for a in n.actions] == ["Terminal", "Ack"]


def test_done_notice_shows_the_first_200_chars_of_the_last_message(capture):
    last = "All tests pass.\n\n" + "x" * 400
    n = notify.build(ROW, TASK, "done", None, last)
    assert n.title == "◆ shop/api · s1: done" and (n.priority, n.tags, n.kind) == (3, ["white_check_mark"], "done")
    q = n.body.splitlines()[-1]
    assert q.startswith("? All tests pass. xxx") and len(q) == 2 + 200            # whitespace collapsed, then cut
    assert [a["label"] for a in n.actions] == ["Terminal", "Ack"]


def test_errored_notice_carries_the_error_text(capture):
    n = notify.build(ROW, TASK, "errored", "server_error", "API Error: 500 overloaded")
    assert n.title == "◆ shop/api · s1: error" and (n.priority, n.tags, n.kind) == (4, ["rotating_light"], "error")
    assert n.body.splitlines()[-1] == "? API Error: 500 overloaded"


def test_rate_limit_kind_is_priority_5(capture):
    n = notify.build(ROW, TASK, "errored", "rate_limit", "You have hit your limit")
    assert (n.priority, n.tags, n.kind) == (5, ["no_entry"], "rate_limit") and n.title.endswith(": error")


def test_empty_lines_are_omitted_and_the_body_is_never_empty(capture):
    n = notify.build({**ROW, "last_prompt": None}, None, "done", None, "  ")
    assert n.body == "done"                                                            # nothing to show: the state, not ""
    n = notify.build({**ROW, "last_prompt": None}, {"title": "", "phase": "running"}, "done", None, "ok")
    assert n.body == "? ok"
    n = notify.build({**ROW, "last_prompt": "   "}, None, "waiting", "idle_prompt", "waiting")
    assert n.body == "? waiting"


# ---------------------------------------------------------------- title: glyph, place, session

def test_glyphs_and_the_root_repo(capture):
    assert notify.build({**ROW, "agent": "codex"}, None, "done", None, "x").title == "◇ shop/api · s1: done"
    assert notify.build({**ROW, "agent": "shell"}, None, "done", None, "x").title == "▸ shop/api · s1: done"
    assert notify.build({**ROW, "launcher": "shell"}, None, "done", None, "x").title == "▸ shop/api · s1: done"   # a shell row stored as claude
    assert notify.build({**ROW, "repo": "root"}, None, "done", None, "x").title == "◆ shop · s1: done"
    assert notify.build({**ROW, "agent": "shell"}, None, "done", None, "x").agent == "shell"


def test_a_row_without_project_keeps_the_old_label(capture):
    n = notify.build({"tmux_name": TMUX}, None, "waiting", "idle_prompt", "hello")
    assert n.title == "shop / api · s1: needs you" and n.body == "? hello"


def test_no_public_url_means_no_click_and_no_actions(capture, monkeypatch):
    monkeypatch.setattr(settings, "public_url", "")
    n = notify.build(ROW, TASK, "waiting", "permission_prompt", "m", perm={"id": 5, "summary": "Bash: ls"})
    assert n.click is None and n.actions == [] and n.url == f"/#/s/{TMUX}" and n.path == f"/#/s/{TMUX}"


def test_the_old_hash_form_is_never_emitted(capture):
    for st, k in (("waiting", "permission_prompt"), ("done", None), ("errored", "x")):
        n = notify.build(ROW, TASK, st, k, "m", perm={"id": 1, "summary": "s"} if st == "waiting" else None)
        assert "#s=" not in json.dumps(n.__dict__)
        assert "/#/s/" in n.click


def test_ntfy_actions_never_exceed_three(capture):
    for st, k, perm in (("waiting", "permission_prompt", {"id": 1, "summary": "s"}), ("waiting", "idle_prompt", None),
                        ("done", None, None), ("errored", "rate_limit", None)):
        assert len(notify.build(ROW, TASK, st, k, "m", perm).actions) <= 3


# ---------------------------------------------------------------- the prompt line

def test_prompt_line_is_cut_at_120_chars(capture):
    n = notify.build({**ROW, "last_prompt": "word " * 60}, None, "done", None, "ok")
    line = [x for x in n.body.splitlines() if x.startswith("›")][0]
    assert len(line) == 2 + 120 and line == "› " + ("word " * 60).strip()[:120]
    multi = notify.build({**ROW, "last_prompt": "line one\n\nline two"}, None, "done", None, "ok")
    assert "› line one line two" in multi.body


@pytest.mark.parametrize("prompt", [
    "<task-notification> <task-id>b1</task-id>", "  \n<system-reminder>be brief</system-reminder>", "[SYSTEM NOTIFICATION from cron]",
    "<pasted_content name=a.txt>", "<command-name>/model</command-name>", "<local-command-stdout>ok</local-command-stdout>",
    "<bash-input>ls</bash-input>"])
def test_system_turn_prompts_are_never_the_prompt_line(capture, prompt):
    n = notify.build({**ROW, "last_prompt": prompt}, TASK, "done", None, "finished")
    assert "›" not in n.body and n.body == "Fix login redirect\n? finished"


def test_hooks_is_system_turn_wins_when_it_exists(capture, monkeypatch):
    from app import hooks
    monkeypatch.setattr(hooks, "is_system_turn", lambda p: "system" if p.startswith("MARKER") else None, raising=False)
    assert "›" not in notify.build({**ROW, "last_prompt": "MARKER turn"}, None, "done", None, "ok").body
    assert "› fix the login redirect" in notify.build(ROW, None, "done", None, "ok").body
    monkeypatch.delattr(hooks, "is_system_turn", raising=False)                         # without it the local list answers
    assert "›" not in notify.build({**ROW, "last_prompt": "<task-notification> x"}, None, "done", None, "ok").body


# ---------------------------------------------------------------- notify_session: context, wire shape, throttle

def test_session_push_shape_and_context_from_the_db(capture, db):
    t = db.task_add(project="shop", repo="api", slug="login", title="Fix login redirect", prompt="p", tmux_name=TMUX,
                    worktree="/w", branch="b", session_row=db.open_row(TMUX)["id"], phase="running")
    assert t
    assert notify.subscribe_url() == "https://box.ts.net:8444/ccboard"
    assert notify.notify_session(TMUX, "waiting", "Claude is waiting for your input", "idle_prompt")
    url, body = capture[-1]
    assert url == "http://127.0.0.1:2586/" and body["topic"] == "ccboard" and body["priority"] == 4
    assert body["title"] == "◆ shop/api · s1: needs you"
    assert body["message"] == "Fix login redirect\n› fix the login redirect\n? Claude is waiting for your input"
    assert body["click"] == f"{PUB}/#/s/{TMUX}" and body["tags"] == ["bell"]
    assert [a["label"] for a in body["actions"]] == ["Terminal", "Ack"]


def test_a_pending_permission_gives_the_hook_notice_its_buttons(capture, db):
    pid = db.perm_add(TMUX, "Bash", "Bash: npm test", {"command": "npm test"})
    assert notify.notify_session(TMUX, "waiting", "Claude needs your permission to use Bash", "permission_prompt")
    body = capture[-1][1]
    assert body["message"].endswith("? Bash: npm test") and body["tags"] == ["bell", "key"]
    assert [a["label"] for a in body["actions"]] == ["Allow", "Deny", "Terminal"]
    assert body["actions"][0]["url"].endswith(f"/api/permission/{pid}/allow")


def test_compat_wrapper_without_a_db_or_a_row(capture):
    assert notify.notify_session("shop--api--s1", "waiting", "Claude needs permission to run npm test", "permission_prompt")
    url, body = capture[-1]
    assert body["title"] == "shop / api · s1: needs you" and "npm test" in body["message"]
    assert body["click"] == f"{PUB}/#/s/shop--api--s1" and body["priority"] == 4
    assert acts(notify.build({"tmux_name": TMUX}, None, "done", None, "x"))["Terminal"]["url"].endswith(f"/term/{TMUX}")
    assert not notify.notify_session("shop--api--s1", "waiting", "again", None)         # throttled
    assert notify.notify_session("shop--api--s1", "done", "All tests pass", None)        # another state
    assert not notify.notify_session("shop--api--s1", "working", None, None)             # not an attention state
    capture.clear()
    assert notify.notify_session("x--y--z", "done", None, None) and capture[-1][1]["message"] == "done"


def test_a_session_without_a_row_in_a_db_still_notifies(capture, db):
    assert notify.notify_session("other--repo--s9", "done", "ok", None)
    assert capture[-1][1]["title"] == "other / repo · s9: done"


def test_disabled_without_url(capture, monkeypatch):
    monkeypatch.setattr(settings, "ntfy_url", "")
    assert not notify.enabled() and notify.subscribe_url() is None
    assert not notify.notify_session("a--b--c", "done", "x", None)


def test_cooldown_matrix(capture):
    """The effective window per (session, state) is max(THROTTLE_SECONDS, STATE_COOLDOWN[state]): waiting 60, done 120, errored 60."""
    clock = capture.clock

    def fire(state, name=TMUX):
        return notify.notify_session(name, state, "m", None)

    t0 = clock[0]
    assert fire("waiting") and fire("done") and fire("errored")
    for state, window in (("waiting", 60), ("done", 120), ("errored", 60)):
        clock[0] = t0 + window - 1
        assert not fire(state), f"{state} inside its {window}s window"
    clock[0] = t0 + 60
    assert fire("waiting") and fire("errored") and not fire("done")                      # done still cooling at 60 s
    clock[0] = t0 + 120
    assert fire("done")
    clock[0] = t0 + 121
    assert fire("waiting", "shop--api--s2")                                              # another session never shares a window
    assert not fire("waiting", "shop--api--s2")


def test_the_longer_of_throttle_and_cooldown_wins(capture, monkeypatch):
    monkeypatch.setattr(notify, "STATE_COOLDOWN", {"waiting": 300, "done": 5, "errored": 60})
    clock = capture.clock
    assert notify.notify_session(TMUX, "waiting", "m", None) and notify.notify_session(TMUX, "done", "m", None)
    clock[0] += 59
    assert not notify.notify_session(TMUX, "waiting", "m", None)                         # 300 beats the 60 s throttle
    assert not notify.notify_session(TMUX, "done", "m", None)                            # 60 beats the 5 s cooldown
    clock[0] += 2
    assert notify.notify_session(TMUX, "done", "m", None)                                # 61 s: the throttle is over
    assert not notify.notify_session(TMUX, "waiting", "m", None)                         # the 300 s window is not
    clock[0] += 240
    assert notify.notify_session(TMUX, "waiting", "m", None)


def test_a_permission_push_makes_the_following_hook_notice_the_throttled_one(capture, db):
    from app import permissions
    pid = db.perm_add(TMUX, "Bash", "Bash: npm test", {"command": "npm test"})
    permissions.push_request(pid, TMUX, "Bash: npm test")
    assert len(capture) == 1
    capture.clock[0] += 5
    assert not notify.notify_session(TMUX, "waiting", "Claude needs your permission to use Bash", "permission_prompt")
    capture.clock[0] += 60
    assert notify.notify_session(TMUX, "waiting", "Claude needs your permission to use Bash", "permission_prompt")


# ---------------------------------------------------------------- rate limits: once per (agent, resets_at)

def _limited(db, resets_at, session=TMUX):
    db.kv_set("rate_limited", {"session": session, "message": "You have hit your limit", "kind": "5h", "resets_at": resets_at})


def test_rate_limit_notice_once_per_window(capture, db):
    _limited(db, 1791349200)
    assert notify.notify_rate_limit(TMUX, "You have hit your limit")
    body = capture[-1][1]
    assert body["title"] == "Claude rate limited" and body["priority"] == 5 and body["tags"] == ["no_entry"]
    assert body["message"].splitlines()[0] == "◆ shop/api · s1" and body["message"].endswith("? You have hit your limit")
    assert db.kv_get("rl_notified:claude:1791349200") is not None
    for _ in range(50):                                                                  # one session retried 482 times
        assert not notify.notify_rate_limit(TMUX, "You have hit your limit")
    assert len(capture) == 1
    _limited(db, 1791367200)                                                             # the next window: not inside the 15 minute floor ...
    assert not notify.notify_rate_limit(TMUX, "You have hit your limit") and len(capture) == 1
    capture.clock[0] += notify.RATE_LIMIT_EVERY
    _limited(db, 1791367200)
    assert notify.notify_rate_limit(TMUX, "You have hit your limit") and len(capture) == 2    # ... and told again after it
    assert db.kv_get("rl_notified:claude:1791367200") is not None


def test_a_shell_rows_limit_is_claudes(capture, db):
    db.add_session(tmux_name="shop--api--sh", project="shop", repo="api", name="sh", launcher="shell", agent="shell")
    assert notify.notify_rate_limit("shop--api--sh", "limit", resets_at=5)
    assert capture[-1][1]["title"] == "Claude rate limited" and db.kv_get("rl_notified:claude:5") is not None


def test_rate_limit_windows_are_per_agent(capture, db):
    _limited(db, 1791349200)
    assert notify.notify_rate_limit(TMUX, "limit") and not notify.notify_rate_limit(TMUX, "limit")
    assert notify.notify_rate_limit(TMUX, "limit", agent="codex", resets_at=1791349200)
    assert capture[-1][1]["title"] == "Codex rate limited"
    assert not notify.notify_rate_limit(TMUX, "limit", agent="codex", resets_at=1791349200)


def test_the_session_error_of_a_told_window_adds_nothing(capture, db):
    _limited(db, 1791349200)
    assert notify.notify_rate_limit(TMUX, "You have hit your limit")
    assert not notify.notify_session(TMUX, "errored", "You have hit your limit", "rate_limit")
    assert len(capture) == 1
    assert notify.notify_session(TMUX, "errored", "API Error: 500", "server_error")      # a real error is still told


def test_the_session_error_tells_a_window_nobody_announced_yet(capture, db):
    _limited(db, 1791349200)
    assert notify.notify_session(TMUX, "errored", "You have hit your limit", "rate_limit")
    assert capture[-1][1]["priority"] == 5 and capture[-1][1]["title"].endswith(": error")
    assert not notify.notify_rate_limit(TMUX, "You have hit your limit")                 # the window is told now


def test_without_a_reset_time_the_gate_is_fifteen_minutes_per_agent(capture, db):
    db.kv_set("rate_limited", {"session": TMUX, "message": "slow down", "kind": "other", "resets_at": None})
    assert notify.notify_rate_limit(TMUX, "slow down")
    capture.clock[0] += notify.RATE_LIMIT_EVERY - 1
    assert not notify.notify_rate_limit(TMUX, "slow down")                               # not the 60 s errored cooldown: a quarter of an hour
    capture.clock[0] += 1
    assert notify.notify_rate_limit(TMUX, "slow down") and len(capture) == 2
    assert not [k for k in ("rl_notified:claude:None", "rl_notified:claude:") if db.kv_get(k)]
    # a record of another session (or an old one) is not this session's window
    _limited(db, 1791349200, session="other--x--y")
    capture.clock[0] += notify.RATE_LIMIT_EVERY
    assert notify.notify_rate_limit(TMUX, "limit") and not notify.notify_rate_limit(TMUX, "limit")


def test_a_session_that_reports_the_limit_again_and_again_is_told_once_per_agent_per_quarter_hour(capture, db):
    for _ in range(40):                                                                  # one session retried 482 times; a minute apart is the old cooldown
        notify.notify_session(TMUX, "errored", "You have hit your limit", "rate_limit")
        capture.clock[0] += 61
    assert len(capture) == 3                                                             # 40 minutes: minute 0, 15:xx and 30:xx
    assert all(b["title"].endswith(": error") and b["priority"] == 5 for _, b in capture)


def test_the_account_notice_and_the_session_error_of_one_limit_are_one_notice(capture, db):
    db.kv_set("rate_limited", {"session": TMUX, "message": "slow down", "kind": "other", "resets_at": None})
    assert notify.notify_rate_limit(TMUX, "slow down")
    assert not notify.notify_session(TMUX, "errored", "slow down", "rate_limit")
    assert len(capture) == 1 and capture[0][1]["title"] == "Claude rate limited"


def test_each_agents_limit_has_its_own_fifteen_minutes(capture, db):
    assert notify.notify_rate_limit(TMUX, "limit") and not notify.notify_rate_limit(TMUX, "limit")
    assert notify.notify_rate_limit(TMUX, "limit", agent="codex") and not notify.notify_rate_limit(TMUX, "limit", agent="codex")


def test_rate_limit_window_is_remembered_in_memory_without_a_db(capture):
    assert notify.notify_rate_limit(TMUX, "limit", resets_at=111) and not notify.notify_rate_limit(TMUX, "limit", resets_at=111)
    capture.clock[0] += notify.RATE_LIMIT_EVERY
    assert notify.notify_rate_limit(TMUX, "limit", resets_at=222)
    assert capture[0][1]["message"].startswith("shop / api · s1")                         # no row: the old label names the session


def test_rate_limit_without_a_channel_claims_nothing(capture, monkeypatch, db):
    monkeypatch.setattr(settings, "ntfy_url", "")
    _limited(db, 1791349200)
    assert not notify.notify_rate_limit(TMUX, "limit")
    monkeypatch.setattr(settings, "ntfy_url", "http://127.0.0.1:2586")
    assert notify.notify_rate_limit(TMUX, "limit")                                       # ntfy came back: this window was never told


# ---------------------------------------------------------------- web push

def test_web_push_tag_is_the_session_and_the_payload_gains_the_context(capture, db, monkeypatch):
    calls = []

    def fake_send_all(db_, title, body, url="/", tag=None, extra=None):
        calls.append({"title": title, "body": body, "url": url, "tag": tag, "extra": extra})
        return 1

    monkeypatch.setattr(push, "send_all", fake_send_all)
    pid = db.perm_add(TMUX, "Bash", "Bash: npm test", {})
    assert notify.notify_session(TMUX, "waiting", "needs permission", "permission_prompt")
    c = calls[-1]
    assert c["tag"] == TMUX and c["url"] == f"/#/s/{TMUX}" and c["title"] == "◆ shop/api · s1: needs you"
    assert c["extra"]["agent"] == "claude" and c["extra"]["state"] == "waiting" and c["extra"]["tmux"] == TMUX
    assert c["extra"]["perm_id"] == pid and [a["action"] for a in c["extra"]["actions"]] == ["allow", "deny", "terminal"]
    capture.clock[0] += 500
    notify.notify_session(TMUX, "done", "ok", None)
    assert calls[-1]["tag"] == TMUX and calls[-1]["extra"]["state"] == "done" and calls[-1]["extra"]["perm_id"] is None


def test_web_push_still_works_with_a_send_all_that_has_no_extra(capture, db, monkeypatch):
    calls = []
    monkeypatch.setattr(push, "send_all", lambda db_, title, body, url="/", tag=None: calls.append((title, url, tag)) or 1)
    assert notify.notify_session(TMUX, "done", "ok", None)
    assert calls == [("◆ shop/api · s1: done", f"/#/s/{TMUX}", TMUX)]


def test_a_failing_web_push_never_breaks_the_notice(capture, db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("push service down")

    monkeypatch.setattr(push, "send_all", boom)
    assert notify.web_push("t", "b", "/", tag="x", extra={"a": 1}) == 0
    assert notify.notify_session(TMUX, "done", "ok", None)                               # ntfy still goes out


def test_any_channel_counts_web_push_subscriptions(capture, db, monkeypatch):
    monkeypatch.setattr(settings, "ntfy_url", "")
    assert not notify.any_channel()
    monkeypatch.setattr(db, "push_subs", lambda: [{"endpoint": "e"}])
    assert notify.any_channel()


# ---------------------------------------------------------------- context()

def test_context_picks_the_most_relevant_task_and_survives_a_dead_db(capture, db, monkeypatch):
    rid = db.open_row(TMUX)["id"]
    db.task_add(project="shop", repo="api", slug="old", title="Old and done", prompt="p", tmux_name=TMUX, worktree="/w", branch="b",
                session_row=rid, phase="done")
    db.task_add(project="shop", repo="api", slug="new", title="Running now", prompt="p", tmux_name=TMUX, worktree="/w2", branch="b2",
                session_row=rid, phase="running")
    row, task = notify.context(TMUX)
    assert row["project"] == "shop" and task["title"] == "Running now" and task["chain_next"] is False
    assert notify.context("nobody--x--y") == (None, None)

    def boom(*a, **k):
        raise RuntimeError("db closed")

    monkeypatch.setattr(db, "open_row", boom)
    assert notify.context(TMUX) == (None, None)
    monkeypatch.setattr(notify, "_db", None)
    assert notify.context(TMUX) == (None, None)


# ---------------------------------------------------------------- through the board

def test_hook_triggers_push(client, projects_dir, fake_tmux, capture):
    import subprocess
    subprocess.run(["git", "-C", str(projects_dir), "init", "-q", "-b", "main", "shop/api"], check=True)
    H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
    name = client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]
    from app import hooks, main
    notify.set_db(main.db)                                       # the capture fixture cleared it; the board's DB is the real one
    hook = {"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard-Session": name}
    client.post("/api/hook", headers=hook, content=json.dumps({"hook_event_name": "StopFailure", "error_type": "rate_limit", "error": "limit"}))
    assert [b["title"] for _, b in capture] == ["Claude rate limited"]                   # the account's notice; the session's own error adds nothing
    client.post("/api/hook", headers=hook, content=json.dumps({"hook_event_name": "StopFailure", "error_type": "server_error", "error": "boom"}))
    titles = [b["title"] for _, b in capture]
    assert any(t.endswith(": error") for t in titles)
    err = [b for _, b in capture if b["title"].endswith(": error")][0]
    assert err["title"] == "▸ shop/api · " + name.split("--")[2] + ": error"                # a shell row: the ▸ glyph
    assert client.post("/api/notify/test", headers=H).json()["ok"] is True
    assert capture[-1][1]["title"] == "ccboard test"


# --- v0.5.17g: the account-level "login not valid" notice ---

def test_login_problem_notice_names_the_account_links_to_its_settings_row_and_goes_once_per_account_per_15_minutes(db, capture):
    assert notify.notify_login_problem("acct-1", "Ann", TMUX, "Invalid API key · Please run /login") is True
    url, body = capture[-1]
    assert body["title"] == "Claude login not valid: Ann" and body["priority"] == 4 and "key" in body["tags"]
    assert body["click"] == f"{PUB}/#/settings?sec=accounts&acct=acct-1"
    assert "shop/api · s1" in body["message"] and "Invalid API key" in body["message"] and "Log in again in Settings" in body["message"]
    assert [a["label"] for a in body["actions"]] == ["Log in again"]
    n = len(capture)
    assert notify.notify_login_problem("acct-1", "Ann", "shop--api--s2", "Invalid API key") is False and len(capture) == n, "another session, same account: told once"
    assert notify.notify_login_problem("acct-2", "Bob", TMUX, "Invalid API key") is True and len(capture) == n + 1, "another account has its own window"
    capture.clock[0] += notify.LOGIN_EVERY - 1
    assert notify.notify_login_problem("acct-1", "Ann", TMUX, "x") is False
    capture.clock[0] += 2
    assert notify.notify_login_problem("acct-1", "Ann", TMUX, "x") is True and len(capture) == n + 2


def test_login_problem_notice_without_a_channel_does_not_use_up_the_window(db, capture, monkeypatch):
    monkeypatch.setattr(settings, "ntfy_url", "")
    assert notify.notify_login_problem("acct-1", "Ann", TMUX, "x") is False and notify._login_told == {}
    monkeypatch.setattr(settings, "ntfy_url", "http://127.0.0.1:2586")
    assert notify.notify_login_problem("acct-1", "Ann", TMUX, "x") is True


def test_login_problem_notice_without_an_account_or_a_message_still_reads_well(db, capture):
    assert notify.notify_login_problem(None, None, TMUX, None) is True
    _url, body = capture[-1]
    assert body["title"] == "Claude login not valid" and body["click"] == f"{PUB}/#/settings?sec=accounts" and "Log in again in Settings" in body["message"]


# ---------------------------------------------------------------- v0.5.18: per-kind toggles, chain hand-over, auto-close, payload

def _web(monkeypatch):
    """Replace push.send_all with a recorder (the Web Push senders are always fakes in tests); returns the list of calls."""
    calls = []

    def fake(db_, title, body, url="/", tag=None, extra=None):
        calls.append({"title": title, "body": body, "url": url, "tag": tag, "extra": extra})
        return 1

    monkeypatch.setattr(push, "send_all", fake)
    return calls


def _task(db, title="Fix login redirect", **kw):
    rid = db.open_row(TMUX)["id"]
    return db.task_add(project="shop", repo="api", slug=kw.pop("slug", "t"), title=title, prompt="p", tmux_name=TMUX, worktree="/w",
                       branch=kw.pop("branch", "b"), session_row=rid, phase=kw.pop("phase", "running"), **kw)


def test_every_toggle_is_on_until_switched_off_and_is_kept_in_kv(capture, db):
    assert notify.prefs() == {"needs": True, "done": True, "limit": True, "error": True, "login": True}
    assert notify.set_prefs({"done": False, "bogus": False, "limit": "no"}) == {"needs": True, "done": False, "limit": True, "error": True, "login": True}
    assert db.kv_get("notify_prefs")["value"]["done"] is False
    assert notify.prefs()["done"] is False and notify.prefs()["limit"] is True            # not a boolean: ignored
    assert notify.kind_enabled("done") is False and notify.kind_enabled("permission") is True
    assert notify.kind_enabled("test") is True                                           # a kind with no toggle is never switched off
    notify.set_prefs({"done": True})
    assert notify.prefs()["done"] is True


def test_prefs_survive_a_dead_db_and_a_damaged_row(capture, db, monkeypatch):
    db.kv_set("notify_prefs", ["not", "a", "dict"])
    assert notify.prefs()["needs"] is True
    monkeypatch.setattr(db, "kv_get", lambda k: (_ for _ in ()).throw(RuntimeError("db closed")))
    assert notify.prefs() == {k: True for k in notify.PREF_KEYS}
    monkeypatch.setattr(notify, "_db", None)
    assert notify.prefs()["done"] is True


@pytest.mark.parametrize("toggle,fire", [
    ("needs", lambda: notify.notify_session(TMUX, "waiting", "Claude is waiting for your input", "idle_prompt")),
    ("needs", lambda: notify.notify_session(TMUX, "waiting", "question?", "elicitation_dialog")),
    ("done", lambda: notify.notify_session(TMUX, "done", "ok", None)),
    ("error", lambda: notify.notify_session(TMUX, "errored", "API Error: 500", "server_error")),
    ("limit", lambda: notify.notify_session(TMUX, "errored", "You have hit your limit", "rate_limit")),
    ("limit", lambda: notify.notify_rate_limit(TMUX, "You have hit your limit", resets_at=99)),
    ("login", lambda: notify.notify_login_problem("work", "work", TMUX, "Please run /login")),
])
def test_a_switched_off_kind_sends_nothing_on_either_channel(capture, db, monkeypatch, toggle, fire):
    web = _web(monkeypatch)
    notify.set_prefs({toggle: False})
    assert not fire()
    assert capture == [] and web == []
    notify.set_prefs({toggle: True})
    assert fire() and len(capture) == 1 and len(web) == 1                                 # back on: it goes out (a switched-off kind used no cooldown)


def test_the_permission_push_obeys_the_needs_you_toggle_too(capture, db, monkeypatch):
    from app import permissions
    web = _web(monkeypatch)
    notify.set_prefs({"needs": False})
    permissions.push_request(5, TMUX, "Bash: npm test")                                  # skips the throttles, not the toggle
    assert capture == [] and web == []
    notify.set_prefs({"needs": True})
    permissions.push_request(5, TMUX, "Bash: npm test")
    assert len(capture) == 1 and len(web) == 1


def test_a_switched_off_limit_does_not_claim_its_window(capture, db):
    notify.set_prefs({"limit": False})
    assert not notify.notify_rate_limit(TMUX, "limit", resets_at=77)
    assert db.kv_get("rl_notified:claude:77") is None
    notify.set_prefs({"limit": True})
    assert notify.notify_rate_limit(TMUX, "limit", resets_at=77)


def test_send_alone_honours_the_toggles_but_the_ntfy_test_does_not(capture, db):
    notify.set_prefs({"done": False})
    assert notify.send(notify.build(ROW, TASK, "done", None, "ok")) is False and capture == []
    assert notify.publish("ccboard test", "Notifications work.", tags=["tada"])          # /api/notify/test goes through publish()
    assert capture[-1][1]["title"] == "ccboard test"


def test_a_chain_step_that_hands_over_is_priority_2_ntfy_only(capture, db, monkeypatch):
    web = _web(monkeypatch)
    first = _task(db)
    db.task_add(project="shop", repo="api", slug="step2", title="Second step", prompt="p", phase="queued", parent_id=first, chain_id="c1")
    db.task_update(first, chain_id="c1")
    row, task = notify.context(TMUX)
    assert task["chain_next"] is True
    assert notify.notify_session(TMUX, "done", "step one finished", None)
    assert capture[-1][1]["priority"] == 2 and capture[-1][1]["tags"] == ["white_check_mark"]
    assert web == []                                                                     # no Web Push for a hand-over
    n = notify.build(row, task, "done", None, "x")
    assert n.priority == 2 and n.web is False


def test_the_last_step_of_a_chain_and_a_plain_task_notify_normally(capture, db, monkeypatch):
    web = _web(monkeypatch)
    first = _task(db)
    last = db.task_add(project="shop", repo="api", slug="step2", title="Last step", prompt="p", phase="done", parent_id=first, chain_id="c1")
    db.task_update(first, chain_id="c1", phase="done")
    assert notify.context(TMUX)[1]["chain_next"] is False                                # nothing is queued behind it
    assert notify.notify_session(TMUX, "done", "all done", None)
    assert capture[-1][1]["priority"] == 3 and len(web) == 1 and last


def test_a_chain_hand_over_is_only_quiet_for_done(capture, db):
    first = _task(db)
    db.task_add(project="shop", repo="api", slug="step2", title="Second", prompt="p", phase="queued", parent_id=first, chain_id="c1")
    row, task = notify.context(TMUX)
    assert notify.build(row, task, "waiting", "idle_prompt", "?").priority == 4 and notify.build(row, task, "waiting", "idle_prompt", "?").web
    assert notify.build(row, task, "errored", "server_error", "boom").priority == 4


def test_an_auto_close_done_notice_says_when_the_session_closes(capture, db, monkeypatch):
    monkeypatch.setattr(settings, "autoclose_grace", 45.0)
    _task(db, auto_close=1)
    assert notify.notify_session(TMUX, "done", "Fixed the redirect.", None)
    assert capture[-1][1]["message"].splitlines()[-2:] == ["? Fixed the redirect.", "(session closes in 45s)"]
    monkeypatch.setattr(settings, "autoclose_grace", 12.5)
    n = notify.build(ROW, {"title": "T", "auto_close": True}, "done", None, "ok")
    assert n.body.endswith("\n(session closes in 12.5s)")


def test_no_close_promise_without_auto_close_or_when_the_message_asks_something(capture, db):
    assert not notify.build(ROW, {"title": "T", "auto_close": False}, "done", None, "ok").body.endswith("s)")
    assert not notify.build(ROW, None, "done", None, "ok").body.endswith("s)")
    held = notify.build(ROW, {"title": "T", "auto_close": True}, "done", None, "Shall I also update the docs?")
    assert "session closes" not in held.body                                             # a question holds the close (taskflow)
    assert "session closes" not in notify.build(ROW, {"title": "T", "auto_close": True}, "waiting", "idle_prompt", "waiting").body
    assert "session closes" not in notify.build(ROW, {"title": "T", "auto_close": True}, "errored", "server_error", "boom").body


def test_web_extras_carry_renotify_a_timestamp_and_the_attention_count(capture, db, monkeypatch):
    web = _web(monkeypatch)
    db.add_session(tmux_name="shop--api--s2", project="shop", repo="api", name="s2", launcher="claude")
    db.set_state("shop--api--s2", "waiting", "Notification", message="?", attention=True)
    db.set_state(TMUX, "done", "Stop", message="ok", attention=True)
    assert notify.notify_session(TMUX, "done", "ok", None)
    extra = web[-1]["extra"]
    assert extra["renotify"] is True and extra["badge"] == 2 and isinstance(extra["ts"], int) and extra["ts"] > 1.7e12
    db.ack(TMUX)
    capture.clock[0] += 500
    assert notify.notify_session(TMUX, "done", "ok again", None)
    assert web[-1]["extra"]["badge"] == 1                                                # acknowledged sessions are not counted


def test_a_notice_without_a_tag_never_asks_to_renotify(capture):
    n = notify.Notice(title="t", body="b", click=None, url="/", tag="", priority=3, tags=[], actions=[], web_actions=[], kind="done")
    assert n.web_extra()["renotify"] is False
    assert notify.build({"tmux_name": TMUX}, None, "done", None, "ok").web_extra()["renotify"] is True


def test_the_sample_notices_come_from_the_real_builder(capture):
    smp = notify.samples()
    assert list(smp) == ["needs", "done", "limit", "error", "login"]
    assert smp["needs"]["title"] == "◆ shop/api · s1: needs you" and smp["needs"]["body"].endswith("? Bash: npm test")
    assert smp["needs"]["buttons"] == ["Allow", "Deny"] and smp["done"]["buttons"] == ["Terminal", "Ack"]
    assert smp["limit"]["title"] == "Claude rate limited" and smp["limit"]["priority"] == 5
    assert smp["error"]["title"].endswith(": error") and smp["login"]["title"].startswith("Claude login not valid")
    assert set(smp["needs"]) == {"title", "body", "priority", "kind", "buttons"}


def test_notify_prefs_api_reads_and_writes_the_toggles(lite_client, capture):
    from app import main
    notify.set_db(main.db)
    H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
    r = lite_client.get("/api/notify/prefs", headers=H).json()
    assert r["prefs"] == {k: True for k in notify.PREF_KEYS} and list(r["samples"]) == list(notify.PREF_KEYS)
    r = lite_client.put("/api/notify/prefs", headers=H, json={"done": False, "error": False}).json()
    assert r["prefs"]["done"] is False and r["prefs"]["error"] is False and r["prefs"]["needs"] is True
    assert lite_client.get("/api/notify/prefs", headers=H).json()["prefs"]["done"] is False
    assert lite_client.put("/api/notify/prefs", headers=H, json={"done": "maybe"}).status_code == 422
    assert lite_client.put("/api/notify/prefs", headers={"Tailscale-User-Login": "alice@example.com"}, json={"done": True}).status_code == 403   # no X-CCBoard
    assert lite_client.post("/api/notify/test", headers=H).json()["ok"] is True          # the test notice ignores the toggles
    assert capture[-1][1]["title"] == "ccboard test"
