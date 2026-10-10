"""Nodes epic P7, issue #140, second security review: the redaction pass and the caps in front of every pattern.

1. Nothing a peer controls reaches a regex before it is cut: the pane is cut to 40 lines of 528 characters first (on the peer and again on the hub), redact()
   cuts its own input, nodes._scrub and node_state.clean cut theirs. Adversarial inputs (512 KB of repeated text built to make a pattern backtrack) go through
   the peer path and the hub path in well under a second, and a spy on redact() sees only capped text.
2. The shapes: URL credentials and connection strings, flags, NAME=VALUE for names that hold key, token, secret, password, credential, Cookie and X-...-Key
   headers, Basic, the provider keys, key blocks of any label, and a known token wrapped over several lines. Ordinary text stays.
3. Every free-text field of a peer's answer is redacted by the hub; a known secret never shows in a repr.

Secret-shaped test data is built by concatenation so that no literal in this file looks like a real token. Temp dirs and fakes only.
"""
from __future__ import annotations

import json
import time

import pytest

from app import node_state, nodes, nodes_relay as nr

ID = {"Tailscale-User-Login": "alice@example.com"}
BY = nr.BY_NAME
BIG = 512 * 1024


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-CCBoard": "1"}


class Answer:
    def __init__(self, status=200, body=b"{}"):
        self.status, self.body, self.seen = status, body, 0

    def __call__(self, target, method, path, headers, body, timeout):
        self.seen += 1
        return self.status, {}, self.body


class Online:
    def records(self, handle=None):
        return [{"status": "online", "age_s": 1, "polled_at": "2026-10-10T00:00:00Z", "last_ok_at": "2026-10-10T00:00:00Z"}]


@pytest.fixture(autouse=True)
def _online(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "_hub", lambda: Online())
    yield
    main._invalidate_scan()


@pytest.fixture
def lone_board(lite_client):
    from app import main
    nodes.reset()
    return lite_client, main.db


@pytest.fixture
def spy(monkeypatch):
    """Every length redact() was asked to read, from any caller."""
    seen: list[int] = []
    real = nr.redact

    def wrapper(text, *a, **kw):
        seen.append(len(str(text or "")))
        return real(text, *a, **kw)
    monkeypatch.setattr(nr, "redact", wrapper)
    return seen


def session(fake_tmux, name="shop--api--s1"):
    fake_tmux["sessions"][name] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1", "command": "zsh", "path": "/x", "pid": 1, "env": {}}
    return name


# ================================================================ 1. caps before patterns

def attacks(n: int = BIG) -> dict[str, str]:
    return {
        "repeated a": "a" * n,
        "a= pairs": "a=" * (n // 2),
        "BEGIN prefixes": "-----BEGIN" * (n // 10),
        "BEGIN with label, no end": "-----BEGIN X----- " * (n // 18),
        "base64 with a late mismatch": "A" * (n - 1) + "!",
        "hex with a late mismatch": "0" * (n - 1) + "g",
        "nested quotes": "\"'" * (n // 2),
        "password colons": "password:" * (n // 9),
        "spaces after authorization": "authorization" + " " * n,
        "spaces after =": "key=" + " " * n,
        "dashes": "a-" * (n // 2),
        "dots": "a." * (n // 2),
        "three-part": ("A" * 15 + ".") * (n // 16),
        "sk- runs": "sk-" * (n // 3),
        "scheme runs": "a://" * (n // 4),
        "x- headers": "x-" * (n // 2),
        "bearer spaces": "bearer" + " " * n,
        "basic words": "basic " + "A" * n,
        "newlines": "a\n" * (n // 2),
        "key words": "key-" * (n // 4),
        "secret dots": "secret." * (n // 7),
        "token colons": "token:" * (n // 6),
        "flag runs": "--token " * (n // 8),
        "name then spaces": "a_key" + " " * n,
    }


@pytest.mark.parametrize("name", sorted(attacks(1024)))
def test_the_peers_pane_path_reads_adversarial_input_in_well_under_a_second_and_the_patterns_see_only_capped_text(lone_board, fake_tmux, spy, name):
    c, db = lone_board
    sess = session(fake_tmux)
    fake_tmux["screen"] = attacks()[name]
    _, token = nodes.add_incoming({"id": "ts:nCALLER000001", "name": "caller", "url": "https://100.64.0.9"}, ["sessions"], db=db)
    t0 = time.monotonic()
    r = c.get(f"/api/node/sessions/{sess}/pane", headers=bearer(token))
    took = time.monotonic() - t0
    assert r.status_code == 200 and took < 1.0, (name, took)
    assert len(r.json()["lines"]) <= 40 and all(len(x) <= nr.PANE_LINE_MAX for x in r.json()["lines"])
    assert spy and max(spy) <= nr.REDACT_MAX, (name, max(spy))


@pytest.mark.parametrize("name", sorted(attacks(1024)))
def test_the_hub_path_cuts_a_peers_pane_before_any_pattern_and_refuses_an_over_size_answer(two_nodes, pair_up, spy, name):
    reg = pair_up()
    path = f"/api/nodes/{reg['handle']}/sessions/shop--api--s1/pane"
    lines = [attacks(6 * 1024)[name]] * 3
    body = json.dumps({"name": "shop--api--s1", "lines": lines}).encode()
    assert len(body) < BY["pane"].answer_max
    nodes.peer_transport = Answer(200, body)
    t0 = time.monotonic()
    r = two_nodes.a.get(path)
    assert r.status_code == 200 and time.monotonic() - t0 < 1.0, name
    assert len(r.json()["data"]["lines"]) <= 40 and all(len(x) <= nr.PANE_LINE_MAX for x in r.json()["data"]["lines"])
    assert spy and max(spy) <= nr.REDACT_MAX
    spy.clear()
    huge = json.dumps({"name": "x", "lines": [attacks()[name]]}).encode()
    assert len(huge) > BY["pane"].answer_max
    nodes.peer_transport = Answer(200, huge[:nodes.RESP_MAX])
    t0 = time.monotonic()
    r = two_nodes.a.get(path)
    assert r.status_code == 502 and r.json()["reason"] == "too_large" and time.monotonic() - t0 < 1.0 and spy == [], "refused by size before it is read any further"


def test_the_hub_keeps_the_last_40_of_thousands_of_short_lines_before_any_pattern(two_nodes, pair_up, spy):
    reg = pair_up()
    body = json.dumps({"name": "shop--api--s1", "lines": ["a"] * 12000}).encode()
    assert len(body) < BY["pane"].answer_max
    nodes.peer_transport = Answer(200, body)
    r = two_nodes.a.get(f"/api/nodes/{reg['handle']}/sessions/shop--api--s1/pane")
    assert r.status_code == 200 and len(r.json()["data"]["lines"]) == 40 and max(spy) <= 40 * 2, spy


@pytest.mark.parametrize("name", sorted(attacks(1024)))
def test_every_helper_that_takes_a_peers_text_cuts_it_first(name):
    text = attacks()[name]
    t0 = time.monotonic()
    assert len(nr.redact(text)) <= nr.REDACT_MAX + 64
    assert len(nodes._scrub(text, 200) or "") <= 200
    assert len(node_state.clean(text, 200) or "") <= 200
    assert len(nr.screen_lines(text, 40)) <= 40
    assert len(nr.redact_tree({"title": text, "list": [text], "help": text})["title"]) <= nr.REDACT_MAX + 64
    assert time.monotonic() - t0 < 1.5, name


def test_redact_reads_at_most_redact_max_characters_whoever_calls_it():
    assert nr.REDACT_MAX == 40 * (nr.PANE_LINE_MAX + nr.MARGIN + 1)
    assert nr.redact("a" * (nr.REDACT_MAX * 3)) == "a" * nr.REDACT_MAX


def test_a_secret_that_straddles_the_400th_character_is_redacted_whole_before_the_final_cut(fake_tmux):
    key = "AKIA" + "IOSFODNN7" + "EXAMPLE"
    line = "x" * 392 + " " + key + " tail"
    out = nr.screen_lines(line, 5)[0]
    assert key[:8] not in out and len(out) <= nr.PANE_LINE_MAX


def test_the_pattern_set_has_no_unbounded_overlapping_quantifier_left():
    """Review by reading: every `{n,}` with no upper bound was turned into `{n,m}`, except the single-class runs that cannot overlap anything."""
    import re
    src = open(nr.__file__, encoding="utf-8").read()
    for rx in re.findall(r"\{\d+,\}", src[src.index("_PEM ="):src.index("def known_secrets")]):
        raise AssertionError(f"an unbounded quantifier {rx} in the redaction patterns")


# ================================================================ 2. the shapes

def build(*parts: str) -> str:
    return "".join(parts)


NEW_SHAPES = {
    "google": build("AIza", "Sy", "A" * 33),
    "stripe sk": build("sk_", "live_", "A1b2C3d4E5F6g7"),
    "stripe rk": build("rk_", "live_", "A1b2C3d4E5F6g7"),
    "stripe pk": build("pk_", "live_", "A1b2C3d4E5F6g7"),
    "npm": build("npm_", "A1b2C3d4" * 4),
    "gitlab": build("glpat-", "A1b2C3d4E5F6G7H8I9J0"),
    "hugging face": build("hf_", "A1b2C3d4E5" * 3, "XY"),
    "openai project": build("sk-", "proj-", "A1b2C3d4E5F6G7H8I9J0K1"),
}


@pytest.mark.parametrize("name", sorted(NEW_SHAPES))
def test_the_provider_key_shapes(name):
    secret = NEW_SHAPES[name]
    out = nr.redact(f"before {secret} after")
    assert secret not in out and out == "before [redacted] after", out


CREDS = [
    (build("postgres", "://user:p4ss", "word@db.example:5432/app"), "p4ssword"),
    (build("mongodb+srv", "://admin:hunt", "er2@cluster0.example.net/x"), "hunter2"),
    (build("redis", "://:sec", "retpw@cache.example:6379"), "secretpw"),
    (build("https", "://user:pass", "word@host.example/path"), "password"),
    (build("https", "://ghp_", "A1b2C3d4E5F6g7H8@github.com/o/r.git"), "A1b2C3d4E5F6g7H8"),
    (build("amqp", "://guest:gue", "st@mq.example"), "guest:"),
]


@pytest.mark.parametrize("text,secret", CREDS)
def test_url_credentials_and_connection_strings(text, secret):
    out = nr.redact(f"conn {text} ok")
    assert secret not in out and "[redacted]@" in out and out.startswith("conn ") and out.endswith(" ok"), out


def test_a_url_without_credentials_is_left_alone():
    for t in ("see https://example.com/a/b?x=1", "ssh://host.example/repo", "http://100.64.0.2:8443/api"):
        assert nr.redact(t) == t


FLAGS = ["--token VALUE12345", "--password VALUE12345", "--password=VALUE12345", "--api-key VALUE12345", "--auth-token VALUE12345", "--client-secret 'VALUE 12345'",
         "-token VALUE12345", "--access-token=\"VALUE 12345\"", "--secret VALUE12345"]


@pytest.mark.parametrize("flag", FLAGS)
def test_flag_forms(flag):
    out = nr.redact(f"tool run {flag} --verbose")
    assert "VALUE" not in out and out.startswith("tool run -") and "[redacted]" in out and out.endswith("--verbose"), out


def test_a_flag_followed_by_another_flag_is_not_a_value():
    assert nr.redact("run --token --verbose") == "run --token --verbose"


NAMES = ["API_KEY", "api_key", "MY_SECRET", "my-secret", "SECRET", "Password", "DB_PASSWD", "GITHUB_TOKEN", "tokenValue", "AWS_SECRET_ACCESS_KEY", "SSH_KEY", "KEY",
         "stripe.key", "CREDENTIALS", "client_credential", "auth_token", "passphrase", "SOMETHING_TOKEN_2"]


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("form", ["{n}={v}", "export {n}={v}", "{n} = {v}", "{n}: {v}", "{n}=\"{v}\"", "{n}='{v}'", "{n}  =  \"{v} with spaces\"", "ENV {n}={v}"])
def test_name_equals_value_for_names_that_hold_a_secret_word(name, form):
    value = "Zq9" + "xK2mP7vL4"
    out = nr.redact("$ " + form.format(n=name, v=value) + " # done")
    assert "Zq9xK2" not in out and "[redacted]" in out and out.startswith("$ "), out


@pytest.mark.parametrize("text", ["monkey=3", "keyboard=us", "turkey: roast", "tokens used: 5", "passage: 12", "keys: a b", "the key point", "Press any key"])
def test_names_that_only_contain_a_secret_word_inside_another_word_stay(text):
    assert nr.redact(text) == text


HEADERS = ["Cookie: sid=abc123def456; theme=dark", "Set-Cookie: session=abc123def456; Path=/; HttpOnly", "x-api-key: abc123def456", "X-Auth-Token: abc123def456",
           "X-Amz-Security-Token: abc123def456", "Proxy-Authorization: Basic abc123def456", "X-Custom-Secret: abc123def456", "Authorization: Token abc123def456"]


@pytest.mark.parametrize("header", HEADERS)
def test_header_lines_lose_their_value_and_the_next_line_stays(header):
    out = nr.redact(f"> {header}\nnext line")
    assert "abc123def456" not in out and out.endswith("\nnext line"), out


def test_a_harmless_header_stays():
    assert nr.redact("Content-Type: text/html\nX-Request-Id: 42") == "Content-Type: text/html\nX-Request-Id: 42"


def test_basic_auth_base64_is_redacted_but_the_english_word_is_not():
    assert nr.redact("curl -H 'Basic dXNlcjpwYXNz'") == "curl -H 'Basic [redacted]'"
    assert nr.redact("Basic dXNlcjpwYXNzd29yZDEyMw==").endswith("[redacted]")
    for t in ("Basic configuration", "basic setup of the board", "Basic usage notes"):
        assert nr.redact(t) == t


LABELS = ["RSA PRIVATE KEY", "OPENSSH PRIVATE KEY", "EC PRIVATE KEY", "ENCRYPTED PRIVATE KEY", "PRIVATE KEY", "PGP PRIVATE KEY BLOCK", "DSA PRIVATE KEY", "CERTIFICATE"]


@pytest.mark.parametrize("label", LABELS)
def test_key_blocks_of_any_label(label):
    text = build("ls\n-----BEGIN ", label, "-----\nMIIEvQIBADANBgkqhkiG9w0B\nabcDEF123==\n-----END ", label, "-----\nafter")
    out = nr.redact(text)
    assert "MIIEvQ" not in out and "abcDEF" not in out and out.startswith("ls\n") and out.endswith("\nafter"), out


def test_ordinary_text_stays():
    for t in ("a1b2c3d Fix the parser", "9f8e7d6c5b4a fix: typo in README", "commit 1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b", "def secret_santa(): pass",
              "123e4567-e89b-12d3-a456-426614174000", "https://example.com/very/long/path/with/many/segments/and/words", "total 42", "-rw-r--r-- 1 me staff 12 Oct 10 notes.txt",
              "tokens used so far", "ERROR: file not found", "line 007 hello"):
        assert nr.redact(t) == t, t
    assert nr.redact("id: 123e4567-e89b-12d3-a456-426614174000") .endswith("[redacted]"), "a 32+ run after : stays redacted"


# ---------------------------------------------------------------- a token wrapped by tmux

def wrap(text: str, width: int) -> list[str]:
    return [text[i:i + width] for i in range(0, len(text), width)]


def test_a_known_token_wrapped_over_two_or_more_lines_is_redacted_in_every_fragment(monkeypatch):
    from app import hooks
    token = "9f8e" * 16
    monkeypatch.setattr(hooks, "_token", token)
    for width in (40, 32, 20, 13):
        lines = ["$ echo", *wrap(token, width), "$ "]
        out = nr.screen_lines_from(lines, 40)
        joined = "".join(out)
        assert token[:12] not in joined and token[-12:] not in joined, (width, out)
        assert all(token[i:i + 8] not in joined for i in range(0, len(token) - 8, 4)), (width, out)
        assert out[0] == "$ echo" and out[-1] == "$", out
        assert all(x in ("[redacted]", "$ echo", "$") or "[redacted]" in x for x in out[1:-1]), out


def test_a_hub_token_wrapped_the_same_way(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "hub_token", "HUB-" + "Q7" * 20)
    out = nr.screen_lines_from(wrap("export X=" + settings.hub_token, 24), 40)
    assert "Q7Q7Q7Q7" not in "".join(out)


@pytest.mark.parametrize("secret", [nodes.TOKEN_PREFIX + "Q" * 43, "ccbmcp_" + "R" * 43, build("sk-", "ant-", "api03-AbCdEf123456789_xyzQ"), build("ghp_", "A1b2C3d4E5" * 3)])
def test_a_token_shape_wrapped_over_two_lines_is_redacted(secret):
    for width in (20, 25):
        out = nr.screen_lines_from(["x"] + wrap(secret, width) + ["y"], 40)
        assert secret[:10] not in "".join(out) and secret[-10:] not in "".join(out), (width, out)


def test_text_that_only_looks_like_a_wrapped_token_is_not_touched():
    lines = ["short", "words", "on", "many", "lines", "with", "no", "secret"]
    assert nr.screen_lines_from(lines, 40) == lines


def test_a_wrapped_hook_token_through_the_peers_pane_row(lone_board, fake_tmux, monkeypatch):
    from app import hooks
    c, db = lone_board
    sess = session(fake_tmux)
    fake_tmux["screen"] = "\n".join(["$ cat token", *wrap(hooks.ensure_token(), 30), "$ done"])
    _, token = nodes.add_incoming({"id": "ts:nCALLER000001", "name": "caller", "url": "https://100.64.0.9"}, ["sessions"], db=db)
    body = c.get(f"/api/node/sessions/{sess}/pane", headers=bearer(token)).json()
    assert hooks.ensure_token()[:10] not in "".join(body["lines"]) and hooks.ensure_token()[-10:] not in "".join(body["lines"])


# ================================================================ 3. every free-text field, known secrets, limits

def test_a_known_secret_never_shows_in_its_repr_or_in_an_error_text():
    secrets = nr.known_secrets()
    assert secrets, "the hook token is known once the board started"
    for s in secrets:
        assert "<secret>" == repr(s) and s not in repr([s]) and s not in repr({"k": s})
        try:
            raise ValueError(secrets)
        except ValueError as e:
            assert s not in repr(e)


def test_the_hub_redacts_every_free_text_field_of_a_peers_answer(two_nodes, pair_up):
    reg = pair_up()
    leak = build("ghp_", "A1b2C3d4E5" * 3)
    task = {"id": 5, "title": f"use token={leak} now", "phase": "running", "agent": "claude", "project": "shop", "repo": "api", "branch": f"fix-{leak}", "tmux": "shop--api--t",
            "issue_ref": "#1", "updated_at": "2026-10-10", "slug": f"s-{leak}", "mode": "worktree", "base": "main", "created_at": "x", "assigned_at": "x", "done_at": None,
            "pr_number": None, "pr_state": f"password={leak}", "has_result": False}
    nodes.peer_transport = Answer(200, json.dumps(task).encode())
    r = two_nodes.a.get(f"/api/nodes/{reg['handle']}/tasks/5")
    assert r.status_code == 200 and leak not in r.text and "[redacted]" in r.text
    agents = {"agents": [{"name": "claude", "label": f"C {leak}", "glyph": "c", "installed": True, "version": "1", "logged_in": True, "hooks": True,
                         "options": [{"key": "model", "label": "Model", "kind": "text", "choices": None, "default": leak, "help": f"see {leak} and key=value pairs", "group": "basic",
                                      "danger": False, "when": None}],
                         "permission_modes": [], "efforts": [], "models": [leak], "reasoning_by_model": {}}]}
    nodes.peer_transport = Answer(200, json.dumps(agents).encode())
    r = two_nodes.a.get(f"/api/nodes/{reg['handle']}/agents")
    assert r.status_code == 200 and leak not in r.text
    assert "key=value" in r.text, "launcher help is prose: only keys and URL credentials are taken out of it"


def test_a_peers_error_text_with_a_secret_is_redacted_in_the_answer_and_the_audit(two_nodes, pair_up):
    reg = pair_up()
    leak = build("ghp_", "A1b2C3d4E5" * 3)
    nodes.peer_transport = Answer(400, json.dumps({"error": f"bad token={leak}"}).encode())
    r = two_nodes.a.get(f"/api/nodes/{reg['handle']}/tasks/5")
    assert r.status_code == 400 and leak not in r.text
    assert leak not in json.dumps(two_nodes.a.db.node_audit_list(50))


@pytest.mark.parametrize("handle", ["Node-B", "NODE-B", "node-b%0A", "node-b%20", "nodE-b", "%EF%BD%8Eode-b", "node%E2%80%91b", "node-b.", "node-b%2F", "node_b", "-node-b", "node-b%00",
                                    "n%C3%B6de-b", "node-b-", "x" * 32])
def test_a_handle_must_match_the_registrys_exact_rule_so_no_status_check_is_reached_by_a_look_alike(two_nodes, pair_up, handle):
    pair_up()
    seen = Answer()
    nodes.peer_transport = seen
    paths = [(row.hub_method, row.hub_path, row.name) for row in nr.RELAY] + [("GET", "/api/nodes/{handle}/stream?names=p--r--s", "stream")]
    for method, hub_path, name in paths:
        path = hub_path.replace("{handle}", handle).replace("{tid}", "1").replace("{name}", "p--r--s")
        r = two_nodes.a.call(method, path, **({} if method == "GET" else {"json": {}}))
        assert r.status_code in (400, 404) and r.json().get("reason") in (None, "bad_handle", "unknown_node"), (handle, name, r.status_code, r.text[:80])
    assert seen.seen == 0


def test_the_handle_cannot_come_from_a_query_either(two_nodes, pair_up):
    reg = pair_up()
    r = two_nodes.a.get(f"/api/nodes/{reg['handle']}/card?handle=other")
    assert r.status_code == 422 and "handle" in r.json()["error"]


def test_the_relay_limiters_hold_at_most_512_people_and_evict_the_oldest():
    for lim in (nodes.relay_read_limiter, nodes.relay_write_limiter):
        assert lim.max_sources == 512
        for i in range(2000):
            lim.allow(f"user{i}@example.com")
        assert len(lim._hits) <= 512
        lim.clear()


# ---------------------------------------------------------------- the hub's stop and the database's close

def test_stop_does_not_wait_when_a_hub_thread_calls_it_and_waits_for_the_polls_in_flight_otherwise(lite_client, monkeypatch):
    import threading
    from app import main, nodes_hub
    hub = nodes_hub.NodeHub(main.db)
    monkeypatch.setattr(nodes_hub, "STOP_WAIT", 1.0)
    hub._inflight.add("p_0123456789abcdef")
    took = {}

    def from_worker():
        t0 = time.monotonic()
        hub.stop()
        took["worker"] = time.monotonic() - t0
    th = threading.Thread(target=from_worker, name="node-hub_0")
    th.start()
    th.join(5)
    assert took["worker"] < 0.5, "a worker never waits for itself"
    hub2 = nodes_hub.NodeHub(main.db)
    hub2._inflight.add("p_0123456789abcdef")
    t0 = time.monotonic()
    hub2.stop()
    assert 0.9 <= time.monotonic() - t0 < 3.0, "anyone else waits for the polls in flight, up to STOP_WAIT"


def test_nothing_but_the_lifespan_stops_the_hub_and_no_route_can():
    import ast
    from pathlib import Path
    root = Path(nr.__file__).parent
    callers = []
    for p in root.glob("*.py"):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr == "stop" and isinstance(node.value, ast.Name) and node.value.id == "hub":
                callers.append(p.name)
    assert callers == ["main.py"], callers
    src = (root / "main.py").read_text(encoding="utf-8")
    assert "await asyncio.to_thread(hub.stop)" in src, "the lifespan stops it off the event loop"


def test_close_waits_for_a_query_then_gives_up_instead_of_hanging_and_a_closed_database_raises(tmp_path, monkeypatch):
    import sqlite3
    import threading
    from app.db import DB
    d = DB(tmp_path / "close.db")
    monkeypatch.setattr(DB, "CLOSE_WAIT", 0.3)
    hold, release = threading.Event(), threading.Event()

    def busy():
        with d.lock:
            hold.set()
            release.wait(5)
    th = threading.Thread(target=busy)
    th.start()
    hold.wait(2)
    t0 = time.monotonic()
    d.close()
    assert 0.25 <= time.monotonic() - t0 < 2.0, "it waited, then left the connection open"
    release.set()
    th.join(2)
    assert d.kv_get("nothing") is None, "still usable: it was not closed under the query"
    d.close()
    with pytest.raises(sqlite3.ProgrammingError):
        d.kv_get("nothing")


def test_close_after_a_state_change_callback_that_writes_through_the_database_does_not_deadlock(tmp_path):
    from app.db import DB
    d = DB(tmp_path / "cb.db")
    wrote = []
    d.on_state_change = lambda *a: (d.kv_set("seen", {"n": len(wrote)}), wrote.append(a))
    d.add_session(tmux_name="shop--api--s1", project="shop", repo="api", name="s1", launcher="claude")
    d.set_state("shop--api--s1", "working", "UserPromptSubmit")
    d.set_state("shop--api--s1", "idle", "Stop")
    assert wrote, "the callback ran and wrote through the database"
    t0 = time.monotonic()
    d.close()
    assert time.monotonic() - t0 < 1.0
