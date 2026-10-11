"""Nodes epic P11, issue #151: notifications across nodes.

Each node notifies for itself; the hub only shows a merged list. Pinned here: a board with no paired node sends byte-identical notices (golden strings captured
before the change, tests/fixtures/notify_golden.json), a board with a paired node names itself in every title and makes its tags unique, the hub never calls a
sender for a remote attention item, the doctor rows (relative links, one subscription shared by two nodes) and that the service worker needs no change.
No test sends a real push or ntfy message: the senders are fakes."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from app import notify, push
from app.agents import codex_rollout
from app.config import settings

from .test_nodes_hub import clock, db, net  # noqa: F401  (fixtures)
from .test_notify import PUB, ROW, TASK, TMUX, capture  # noqa: F401  (capture is a fixture)

GOLDEN = Path(__file__).parent / "fixtures" / "notify_golden.json"
H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
MSG = "Claude needs your permission to use Bash"


def view(n) -> dict:
    """A Notice as plain data, without the clock-dependent field."""
    d = asdict(n)
    d.pop("ts", None)
    return d


def golden_cases(monkeypatch, lite_client, capture) -> dict:
    """Every kind of notice this board sends, built from fixed inputs: session (permission, idle, elicitation, done, error, a limit seen by a session, no row),
    the account notices (rate limit, login, Codex rate limit) and the two test notices, once with a public URL and once without."""
    from app import main
    notify.set_db(main.db)
    out: dict = {}
    sent: list = []
    pushes: list = []
    monkeypatch.setattr(push, "send_all", lambda db, *a, **k: pushes.append({"args": list(a), "kwargs": k}) or 1)
    for label, pub in (("pub", PUB), ("nopub", "")):
        monkeypatch.setattr(settings, "public_url", pub)
        c = {}
        c["permission"] = view(notify.build(ROW, TASK, "waiting", "permission_prompt", MSG, perm={"id": 5, "summary": "Bash: npm test"}))
        c["permission_no_request"] = view(notify.build(ROW, None, "waiting", "permission_prompt", MSG))
        c["idle"] = view(notify.build(ROW, TASK, "waiting", "idle_prompt", "Claude is waiting for your input"))
        c["elicitation"] = view(notify.build(ROW, TASK, "waiting", "elicitation_dialog", "Pick one"))
        c["done"] = view(notify.build(ROW, TASK, "done", None, "Fixed the redirect and the tests pass."))
        c["error"] = view(notify.build(ROW, TASK, "errored", "error", "API Error: 500 Internal server error"))
        c["session_rate_limit"] = view(notify.build(ROW, TASK, "errored", "rate_limit", "5-hour limit reached, resets 14:00"))
        c["no_row"] = view(notify.build({"tmux_name": TMUX}, None, "done", None, "ok"))
        c["rate_limit"] = view(notify._limit_notice(ROW, TASK, TMUX, "claude", "5-hour limit reached, resets 14:00"))
        c["login"] = view(notify._login_notice(ROW, "work", "work", TMUX, "Please run /login", "claude"))
        c["login_no_account"] = view(notify._login_notice({}, None, None, TMUX, None, "claude"))
        sent.clear()
        with monkeypatch.context() as m:
            m.setattr(notify, "send", lambda n: sent.append(n) or True)
            codex_rollout._send_notice(notify, {"used_percent": 100, "window_minutes": 300, "resets_at": 1790000000}, "plus", "work")
        c["codex_rate_limit"] = view(sent[-1])
        c["samples"] = notify.samples()
        capture.clear()
        assert lite_client.post("/api/notify/test", headers=H).json()["ok"] is True
        c["ntfy_test"] = capture[-1][1]
        pushes.clear()
        assert lite_client.post("/api/push/test", headers=H).json()["sent"] == 1
        p = pushes[-1]
        p["kwargs"]["extra"] = {k: v for k, v in p["kwargs"]["extra"].items() if k != "ts"}
        c["push_test"] = p
        out[label] = c
    return out


# ================================================================ one board: today's strings, byte for byte

def pair_out(db, name="node-b", n=2):
    """A node this board calls (a registry row; no token, no network)."""
    from app import nodes
    return nodes.put_peer({"direction": "out", "url": f"https://100.64.0.{n}", "name": name, "scopes": ["read"]}, db=db)


def pair_in(db, name="node-c"):
    """A node that calls this board (an incoming pair row)."""
    from app import nodes
    return nodes.add_incoming({"id": "n_" + "c" * 16, "name": name, "url": "https://100.64.0.3"}, db=db)[0]


def test_a_board_with_no_paired_node_sends_exactly_the_strings_it_sent_before(monkeypatch, lite_client, capture):
    """tests/fixtures/notify_golden.json was captured from the code before issue #151: session, permission, rate-limit, login, Codex limit, the Settings
    examples and the two test notices, with and without a public URL. Nothing about them may change on a board nobody paired with."""
    monkeypatch.setattr(settings, "node_name", "node-a")
    want = json.loads(GOLDEN.read_text(encoding="utf-8"))
    got = golden_cases(monkeypatch, lite_client, capture)
    assert json.loads(json.dumps(got)) == want
    for label in ("pub", "nopub"):
        for case, n in want[label].items():
            if case in ("samples", "ntfy_test", "push_test"):
                continue
            assert "node-a" not in json.dumps(n), (label, case)


def test_a_single_board_never_asks_tailscale_for_its_name_on_the_hook_path(monkeypatch, lite_client, capture):
    from app import main, nodes
    notify.set_db(main.db)
    monkeypatch.setattr(nodes, "display_name", lambda: pytest.fail("the name was read although no node is paired"))
    assert notify.build(ROW, TASK, "done", None, "ok").title == "◆ shop/api · s1: done"
    assert notify.titled("ccboard test") == "ccboard test" and notify.tagged("test") == "test"


def test_a_legacy_row_and_a_revoked_pair_are_not_a_pair(monkeypatch, lite_client, capture):
    from app import main, nodes
    notify.set_db(main.db)
    monkeypatch.setattr(settings, "node_name", "node-a")
    nodes.put_peer({"direction": "out", "url": "https://100.64.0.9", "name": "old", "legacy": True}, db=main.db)
    assert nodes.any_paired(main.db) is False and notify.titled("t") == "t"
    row = pair_in(main.db)
    assert nodes.any_paired(main.db) is True and notify.titled("t") == "node-a · t"
    nodes.revoke(row["peer_id"], db=main.db)
    assert nodes.any_paired(main.db) is False and notify.titled("t") == "t", "a revoked pair is not a pair"
    assert nodes.any_paired(None) is False


# ================================================================ with a paired node: the title names the node, the tag is unique

def transformed(want: dict, node: str) -> dict:
    """The golden notices as they must read once a node is paired: the title starts with `<node> · `, a tag becomes `<node>:<tag>`, nothing else moves."""
    out = {}
    for case, n in want.items():
        n = json.loads(json.dumps(n))
        if case == "samples":
            for v in n.values():
                v["title"] = f"{node} · {v['title']}"
        elif case == "ntfy_test":
            n["title"] = f"{node} · {n['title']}"
        elif case == "push_test":
            n["args"][0] = f"{node} · {n['args'][0]}"
            n["kwargs"]["tag"] = f"{node}:{n['kwargs']['tag']}"
        else:
            n["title"] = f"{node} · {n['title']}"
            n["tag"] = f"{node}:{n['tag']}" if n["tag"] else n["tag"]
        out[case] = n
    return out


@pytest.mark.parametrize("how", ["out", "in"])
def test_with_a_paired_node_every_title_starts_with_the_node_and_every_tag_carries_it(how, monkeypatch, lite_client, capture):
    """The node a hub calls and the leaf a hub calls are both paired: a phone subscribed to two origins hears both, so both name themselves."""
    from app import main
    monkeypatch.setattr(settings, "node_name", "node-a")
    (pair_out if how == "out" else pair_in)(main.db)
    want = json.loads(GOLDEN.read_text(encoding="utf-8"))
    got = json.loads(json.dumps(golden_cases(monkeypatch, lite_client, capture)))
    for label in ("pub", "nopub"):
        assert got[label] == transformed(want[label], "node-a"), label
    perm = got["pub"]["permission"]
    assert perm["title"] == "node-a · ◆ shop/api · s1: needs you" and perm["tag"] == f"node-a:{TMUX}"
    assert got["pub"]["rate_limit"]["title"] == "node-a · Claude rate limited" and got["pub"]["rate_limit"]["tag"] == "node-a:rate-limit"
    assert got["pub"]["login"]["tag"] == "node-a:login-problem" and got["pub"]["codex_rate_limit"]["tag"] == "node-a:rate-limit-codex"
    assert got["nopub"]["done"]["tag"] == f"node-a:{TMUX}" and got["nopub"]["no_row"]["title"] == "node-a · shop / api · s1: done"
    assert got["pub"]["push_test"]["kwargs"]["tag"] == "node-a:test" and got["pub"]["ntfy_test"]["title"] == "node-a · ccboard test"
    assert got["pub"]["permission"]["click"] == f"{PUB}/#/s/{TMUX}", "links are absolute when the public URL is set"
    assert got["nopub"]["permission"]["click"] is None and got["nopub"]["permission"]["url"] == f"/#/s/{TMUX}"


def test_the_name_in_a_title_is_this_boards_own_never_a_peers(monkeypatch, lite_client, capture):
    from app import main
    notify.set_db(main.db)
    monkeypatch.setattr(settings, "node_name", "node-a")
    pair_out(main.db, name="evil-peer")
    n = notify.build(ROW, TASK, "done", None, "ok")
    assert n.title.startswith("node-a · ") and "evil-peer" not in json.dumps(view(n))
    monkeypatch.setattr(settings, "node_name", "node-z")
    assert notify.build(ROW, TASK, "done", None, "ok").title.startswith("node-z · ")


def test_two_nodes_with_a_session_of_one_name_do_not_replace_each_others_notices(monkeypatch, lite_client, capture):
    from app import main
    notify.set_db(main.db)
    pair_out(main.db)
    tags = {}
    for name in ("node-a", "node-b"):
        monkeypatch.setattr(settings, "node_name", name)
        tags[name] = {notify.build(ROW, TASK, "waiting", "idle_prompt", "x").tag, notify._limit_notice(ROW, TASK, TMUX, "claude", "x").tag,
                      notify._login_notice(ROW, None, None, TMUX, None, "claude").tag}
    assert not (tags["node-a"] & tags["node-b"]), "no tag is the same on two nodes"
    assert tags["node-a"] == {f"node-a:{TMUX}", "node-a:rate-limit", "node-a:login-problem"}


def test_the_prefixed_notice_reaches_both_channels_and_the_body_is_untouched(monkeypatch, lite_client, capture):
    from app import main
    notify.set_db(main.db)
    monkeypatch.setattr(settings, "node_name", "node-a")
    pushes = []
    monkeypatch.setattr(push, "send_all", lambda db, *a, **k: pushes.append((a, k)) or 1)
    unpaired = notify.build(ROW, TASK, "done", None, "ok")
    pair_out(main.db)
    n = notify.build(ROW, TASK, "done", None, "ok")
    assert n.body == unpaired.body and n.click == unpaired.click and n.actions == unpaired.actions
    capture.clear()
    assert notify.send(n) is True
    assert capture[-1][1]["title"] == "node-a · ◆ shop/api · s1: done"
    assert pushes[-1][0][0] == "node-a · ◆ shop/api · s1: done" and pushes[-1][0][3] == f"node-a:{TMUX}"


# ================================================================ links: the doctor warns while a paired node's notices carry relative links

def test_doctor_notify_public_url_skips_without_a_pair_warns_with_one_and_passes_with_an_address(monkeypatch, lite_client, capture):
    from app import doctor, main
    monkeypatch.setattr(settings, "public_url", "")
    assert doctor._c_notify_public_url(main.db).status == "skip"
    pair_out(main.db)
    out = doctor._c_notify_public_url(main.db)
    assert out.status == "warn"
    assert out.detail == ("a node is paired and CCBOARD_PUBLIC_URL is empty: links in notifications are relative and will not open from another device")
    assert "CCBOARD_PUBLIC_URL=" in out.fix["text"] and "/etc/ccboard/env" in out.fix["text"] and "install.sh" in out.fix["text"]
    monkeypatch.setattr(settings, "public_url", PUB + "/")
    ok = doctor._c_notify_public_url(main.db)
    assert ok.status == "pass" and ok.fix is None and PUB in ok.detail


def test_doctor_notify_public_url_also_warns_on_a_node_that_is_only_called(monkeypatch, lite_client, capture):
    from app import doctor, main
    monkeypatch.setattr(settings, "public_url", "")
    pair_in(main.db)
    assert doctor._c_notify_public_url(main.db).status == "warn"


def test_the_new_doctor_rows_are_registered_in_the_notify_group():
    from app import doctor
    by = {c[0]: c for c in doctor.CHECKS}
    assert by["notify-public-url"][1] == "notify" and by["notify-shared"][1] == "notify"


# ================================================================ one subscription on two nodes

KEY = "a1b2c3d4e5f6"


def acct(key=KEY, agent="claude", current=True, limited=False, label="Work"):
    return {"agent": agent, "label": label, "current": current, "subscription_key": key, "window": {"pct": 100 if limited else 20, "resets_at": 1790000000,
            "backoff_until": None, "known": True}, "limited": limited}


class FakeHub:
    def __init__(self, recs):
        self.recs = recs

    def records(self):
        return self.recs


def hub_rec(name, items, legacy=False):
    return {"handle": name, "name": name, "legacy": legacy, "card": {"accounts": {"supported": True, "items": items}}, "state": {}}


def test_shared_subscriptions_groups_the_current_account_by_agent_and_key():
    from app import nodes
    both = nodes.shared_subscriptions([("a", [acct()]), ("b", [acct()]), ("c", [acct(key="ffffffffffff")])])
    assert both == [{"agent": "claude", "key": KEY, "nodes": ["a", "b"]}]
    assert nodes.shared_subscriptions([("a", [acct()]), ("b", [acct(key="ffffffffffff")])]) == [], "different keys: two accounts"
    assert nodes.shared_subscriptions([("a", [acct()]), ("b", [acct(agent="codex")])]) == [], "one key under two agents is not one window"
    assert nodes.shared_subscriptions([("a", [acct()]), ("b", [acct(current=False)])]) == [], "a saved login nobody uses sends nothing"
    assert nodes.shared_subscriptions([("a", [acct(), acct()])]) == [], "one node twice is not two nodes"
    assert nodes.shared_subscriptions([("a", [acct(key=None)]), ("b", [acct(key=None)])]) == [], "no key, no claim"
    assert nodes.shared_subscriptions([("a", None), ("b", "junk"), ("c", [None, 3])]) == []


def test_doctor_notify_shared_names_both_nodes_and_says_to_turn_the_notice_off_on_one(monkeypatch, lite_client, capture):
    from app import doctor, main, nodes
    monkeypatch.setattr(settings, "node_name", "node-a")
    monkeypatch.setattr(main, "_hub", lambda: FakeHub([hub_rec("node-b", [acct()]), hub_rec("node-c", [acct(key="ffffffffffff")])]))
    monkeypatch.setattr(nodes, "accounts_view", lambda db: {"items": [acct()]})
    out = doctor._c_notify_shared(main.db)
    assert out.status == "warn"
    assert out.detail == "Claude on node-a, node-b is one subscription: both nodes will send rate-limit notices for one window"
    assert "Settings > Notifications" in out.fix["text"] and "all but one" in out.fix["text"]
    monkeypatch.setattr(nodes, "accounts_view", lambda db: {"items": [acct(key="eeeeeeeeeeee")]})
    out = doctor._c_notify_shared(main.db)
    assert out.status == "pass" and out.fix is None, "different keys: nothing shared"
    monkeypatch.setattr(main, "_hub", lambda: FakeHub([]))
    assert doctor._c_notify_shared(main.db).status == "skip"
    monkeypatch.setattr(main, "_hub", lambda: FakeHub([hub_rec("node-b", [acct()], legacy=True)]))
    assert doctor._c_notify_shared(main.db).status == "skip", "a CCBOARD_NODES entry has no card"


def test_the_card_a_peer_sends_keeps_the_subscription_key_through_the_hub_whitelist():
    """The key is already on the card (issue #133, a 12-character digest, no label of an e-mail kind); the hub's whitelist must not drop it."""
    from app import nodes_hub
    card = {"node_id": "n_" + "b" * 16, "accounts": {"supported": True, "items": [acct()]}}
    clean = nodes_hub.clean_card(card, {"node_id": "n_" + "b" * 16, "name": "node-b", "url": "https://100.64.0.2"})
    assert clean["accounts"]["items"][0]["subscription_key"] == KEY


# ================================================================ no re-emit: the hub only shows

def test_the_hub_never_calls_a_sender_for_a_remote_attention_item(db, net, clock, monkeypatch, capture):
    """A node tells the hub that a session needs the person and a permission waits. The hub polls it three times and serves its read model: not one
    Web Push, ntfy message or badge write, and the local attention count does not move."""
    from .test_nodes_hub import add
    from app import nodes_hub
    notify.set_db(db)
    calls: list = []
    for name in ("web_push", "publish", "send", "notify_session", "notify_rate_limit", "notify_login_problem"):
        monkeypatch.setattr(notify, name, lambda *a, _n=name, **k: calls.append(_n) or False)
    monkeypatch.setattr(push, "send_all", lambda *a, **k: calls.append("send_all") or 0)
    before = notify._attention_count()
    sessions = [{"tmux": "shop--api--s1", "project": "shop", "repo": "api", "session": "s1", "agent": "claude", "state": "waiting", "needs_you": True,
                 "kind": "user", "since": "2026-10-10T10:00:00+00:00", "model": None}]
    _, peer = add(db, net, clock, name="node-b", sessions=sessions, needs_you={"permissions": 1, "input": 1, "errors": 0})
    hub = nodes_hub.NodeHub(db, interval=20.0)
    for _ in range(3):
        rec = hub.poll_once()[0]
        clock.t += 20
    assert rec["state"]["sessions"][0]["needs_you"] is True and rec["state"]["needs_you"]["permissions"] == 1, "the item is in the read model"
    hub.records(), hub.summary_rows(), hub.list_rows()
    assert calls == [] and capture == [], "nothing was sent for a remote item"
    assert notify._attention_count() == before, "the badge counts this board's sessions only"


def test_no_hub_side_module_imports_a_sender():
    """The hub, the relay, the stream relay and the node state know nothing of notify, push or ntfy: a remote item has no path to a sender."""
    import re
    app_dir = Path(__file__).resolve().parent.parent / "app"
    for name in ("nodes.py", "nodes_hub.py", "nodes_relay.py", "nodes_stream.py", "nodes_discovery.py", "node_state.py"):
        src = (app_dir / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from\s+\.?\s*import\s+[^\n]*\b(notify|push)\b|from\s+\.(notify|push)\s+import|import\s+(notify|push)\b)", src, re.M), name
        assert not re.search(r"\b(web_push|notify\.(send|publish|build|notify_\w+)|push\.send_all|setAppBadge)\b", src), name
