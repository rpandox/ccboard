from app.auth import csrf_ok, decode_login, identify
from app.config import parse_allowlist


class S:
    def __init__(self, allowed, bypass=None):
        self.allowed_users = allowed
        self.dev_bypass_user = bypass


def test_parse_allowlist_drops_empty_and_lowercases_ascii():
    assert parse_allowlist(" Alice@Example.com, ,bob@github,") == {"alice@example.com", "bob@github"}


def test_identify_paths():
    s = S({"alice@example.com"})
    assert identify({}, s) is None
    assert identify({"tailscale-user-login": ""}, s) is None
    assert identify({"tailscale-user-login": "  "}, s) is None
    assert identify({"tailscale-user-login": "mallory@example.com"}, s) is None
    assert identify({"tailscale-user-login": "Alice@Example.com"}, s) == "Alice@Example.com"


def test_empty_allowlist_denies_everyone():
    assert identify({"tailscale-user-login": "alice@example.com"}, S(set())) is None


def test_rfc2047_login():
    # "josé@example.com" Q-encoded as tailscale serve would send it
    enc = "=?utf-8?q?jos=C3=A9=40example=2Ecom?="
    assert decode_login(enc) == "josé@example.com"
    assert identify({"tailscale-user-login": enc}, S({"josé@example.com"})) == "josé@example.com"
    assert decode_login("=?utf-8?q?=ZZ?=") in (None, "=ZZ")  # never raises


def test_dev_bypass():
    assert identify({}, S(set(), bypass="dev")) == "dev"


def test_csrf():
    assert csrf_ok("GET", {})
    assert not csrf_ok("POST", {})
    assert not csrf_ok("DELETE", {"x-ccboard": "0"})
    assert csrf_ok("POST", {"x-ccboard": "1"})


def test_http_layer(lite_client):
    assert lite_client.get("/healthz").text == "ok"
    assert lite_client.get("/").status_code == 403
    assert lite_client.get("/static/core.js").status_code == 403
    assert lite_client.get("/docs").status_code == 403
    h = {"Tailscale-User-Login": "alice@example.com"}
    r = lite_client.get("/", headers=h)
    assert r.status_code == 200 and "Content-Security-Policy" in r.headers
    assert lite_client.get("/static/core.js", headers=h).status_code == 200
    assert lite_client.post("/api/projects", headers=h, json={"name": "x"}).status_code == 403  # no X-CCBoard
    assert lite_client.get("/", headers={"Tailscale-User-Login": "mallory@example.com"}).status_code == 403
