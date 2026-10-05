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
    _limited(db, 1791367200)                                                             # the next window is told again
    assert notify.notify_rate_limit(TMUX, "You have hit your limit") and len(capture) == 2
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


def test_without_a_reset_time_there_is_no_window_gate(capture, db):
    db.kv_set("rate_limited", {"session": TMUX, "message": "slow down", "kind": "other", "resets_at": None})
    assert notify.notify_rate_limit(TMUX, "slow down") and notify.notify_rate_limit(TMUX, "slow down")
    assert not [k for k in ("rl_notified:claude:None", "rl_notified:claude:") if db.kv_get(k)]
    # a record of another session (or an old one) is not this session's window
    _limited(db, 1791349200, session="other--x--y")
    assert notify.notify_rate_limit(TMUX, "limit") and notify.notify_rate_limit(TMUX, "limit")


def test_rate_limit_window_is_remembered_in_memory_without_a_db(capture):
    assert notify.notify_rate_limit(TMUX, "limit", resets_at=111) and not notify.notify_rate_limit(TMUX, "limit", resets_at=111)
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
    assert row["project"] == "shop" and task["title"] == "Running now"
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
    client.post("/api/hook", headers={"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard-Session": name},
                content=json.dumps({"hook_event_name": "StopFailure", "error_type": "rate_limit", "error": "limit"}))
    titles = [b["title"] for _, b in capture]
    assert "Claude rate limited" in titles and any(t.endswith(": error") for t in titles)
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
