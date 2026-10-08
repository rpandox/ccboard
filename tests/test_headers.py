"""Cache headers of the board's static surface (app/main.py auth_middleware, is_immutable_static) and the no-GZip rule.

/static/vendor/** and any /static/**/*.woff2 are immutable for a year; the shell, the app's own scripts and styles, the manifest,
icons, demo fixtures, the worker and the pages stay no-cache. Written through lite_client: no pollers, no tmux.
"""
import pytest

H = {"Tailscale-User-Login": "alice@example.com"}
IMMUTABLE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"
CSP = "default-src 'self'; frame-ancestors 'none'"

IMMUTABLE_URLS = [
    "/static/vendor/blueprint/blueprint.css",
    "/static/vendor/blueprint/blueprint-icons.css",
    "/static/vendor/blueprint/blueprint-icons-16.woff2",         # both rules at once: vendor and woff2
    "/static/vendor/fonts/inter-latin-wght-normal.woff2",
    "/static/vendor/fonts/jetbrains-mono-latin-wght-normal.woff2",
    "/static/vendor/diff2html.min.css",
    "/static/vendor/diff2html-ui-base.min.js",
    "/static/vendor/fonts/OFL-Inter.txt",                         # anything under vendor/, whatever its type
]
NO_CACHE_URLS = [
    "/",
    "/sw.js",
    "/static/sw.js",
    "/static/index.html",
    "/static/term.html",
    "/static/core.js",
    "/static/main.js",
    "/static/style.css",
    "/static/tokens.css",
    "/static/shell.css",
    "/static/manifest.webmanifest",
    "/static/icon-192.png",
    "/static/icon-512.png",
    "/static/demo/state.json",
]


@pytest.mark.parametrize("url", IMMUTABLE_URLS)
def test_vendor_and_fonts_are_immutable(lite_client, url):
    r = lite_client.get(url, headers=H)
    assert r.status_code == 200, url
    assert r.headers["cache-control"] == IMMUTABLE, url
    assert r.headers["etag"], f"{url}: keep the validator so a conditional GET still answers 304"


@pytest.mark.parametrize("url", NO_CACHE_URLS)
def test_everything_else_stays_no_cache(lite_client, url):
    r = lite_client.get(url, headers=H)
    assert r.status_code == 200, url
    assert r.headers["cache-control"] == NO_CACHE, url
    assert "immutable" not in r.headers["cache-control"]


def test_other_security_headers_ride_along_on_immutable_responses(lite_client):
    for url in ("/static/vendor/blueprint/blueprint.css", "/static/vendor/fonts/inter-latin-wght-normal.woff2"):
        r = lite_client.get(url, headers=H)
        assert r.headers["Content-Security-Policy"] == CSP, url
        assert r.headers["X-Content-Type-Options"] == "nosniff", url
        assert r.headers["Referrer-Policy"] == "same-origin", url


def test_conditional_get_on_a_vendor_file_is_a_304_that_keeps_the_policy(lite_client):
    url = "/static/vendor/blueprint/blueprint.css"
    first = lite_client.get(url, headers=H)
    again = lite_client.get(url, headers={**H, "If-None-Match": first.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    assert again.headers["cache-control"] == IMMUTABLE
    core = lite_client.get("/static/core.js", headers=H)
    again = lite_client.get("/static/core.js", headers={**H, "If-None-Match": core.headers["etag"]})
    assert again.status_code == 304 and again.headers["cache-control"] == NO_CACHE


def test_head_and_range_requests_keep_the_policy(lite_client):
    url = "/static/vendor/fonts/inter-latin-wght-normal.woff2"
    assert lite_client.head(url, headers=H).headers["cache-control"] == IMMUTABLE
    part = lite_client.get(url, headers={**H, "Range": "bytes=0-3"})
    assert part.status_code == 206 and part.content == b"wOF2"
    assert part.headers["cache-control"] == IMMUTABLE


@pytest.mark.parametrize("url", [
    "/static/vendor/missing.css",
    "/static/vendor/fonts/missing.woff2",
    "/static/missing.woff2",
    "/static/vendor/",
])
def test_a_missing_file_is_never_cached_for_a_year(lite_client, url):
    r = lite_client.get(url, headers=H)
    assert r.status_code == 404, url
    assert r.headers["cache-control"] == NO_CACHE, url


def test_dot_segments_never_earn_the_immutable_policy(lite_client):
    """StaticFiles resolves '..' after the URL is decoded, so '/static/vendor/%2e%2e/core.js' reaches core.js: the policy follows the file, not the prefix."""
    for url in ("/static/vendor/%2e%2e/core.js", "/static/vendor/..%2fcore.js", "/static/vendor/fonts/%2e%2e/%2e%2e/style.css"):
        r = lite_client.get(url, headers=H)
        assert r.headers["cache-control"] == NO_CACHE, f"{url} -> {r.status_code} {r.headers['cache-control']}"


def test_other_methods_and_api_never_become_immutable(lite_client):
    r = lite_client.get("/api/state", headers=H)
    assert r.status_code == 200 and "immutable" not in r.headers.get("cache-control", "")
    assert "immutable" not in lite_client.get("/healthz").headers.get("cache-control", "")


def test_no_identity_no_static_and_no_cache_header_to_poison(lite_client):
    r = lite_client.get("/static/vendor/blueprint/blueprint.css")
    assert r.status_code == 403 and "immutable" not in r.headers.get("cache-control", "")


@pytest.mark.parametrize("path,expected", [
    ("/static/vendor/blueprint/blueprint.css", True),
    ("/static/vendor/diff2html-LICENSE", True),
    ("/static/vendor/fonts/inter-latin-wght-normal.woff2", True),
    ("/static/fonts/extra.woff2", True),                          # any woff2 under /static
    ("/static/pages/deep/x.woff2", True),
    ("/static/inter.woff2", True),
    ("/static/core.js", False),
    ("/static/pages/home.js", False),
    ("/static/style.css", False),
    ("/static/manifest.webmanifest", False),
    ("/static/icon-512-maskable.png", False),
    ("/static/screenshots/agents-390.png", False),
    ("/static/demo/state.json", False),
    ("/static/vendorish/x.js", False),                            # a prefix match on the directory name only
    ("/static/vendor", False),
    ("/static/woff2", False),
    ("/static/core.woff2.js", False),
    ("/", False),
    ("/sw.js", False),
    ("/term/ccboard--ccboard--s1", False),
    ("/api/state", False),
    ("/vendor/blueprint/blueprint.css", False),                   # not under /static
    ("/other/x.woff2", False),
    ("/static/vendor/../core.js", False),                         # traversal
    ("/static/vendor/fonts/../../core.js", False),
    ("/static/pages/..", False),
    ("/static/vendor//x.css", False),
    ("", False),
])
def test_is_immutable_static(path, expected):
    from app import main
    assert main.is_immutable_static(path) is expected, path


def test_immutable_policy_covers_an_extra_woff2_outside_vendor(lite_client, tmp_path, monkeypatch):
    """A woff2 anywhere under /static is immutable: serve a throwaway one through the real mount without touching app/static."""
    from starlette.routing import Mount
    from app import main
    fonts = tmp_path / "extra"
    (fonts / "deep").mkdir(parents=True)
    (fonts / "deep" / "x.woff2").write_bytes(b"wOF2")
    (fonts / "deep" / "x.js").write_text("//")
    mount = next(r for r in main.app.routes if isinstance(r, Mount) and r.path == "/static")
    monkeypatch.setattr(mount.app, "all_directories", [*mount.app.all_directories, str(fonts)])
    assert lite_client.get("/static/deep/x.woff2", headers=H).headers["cache-control"] == IMMUTABLE
    r = lite_client.get("/static/deep/x.js", headers=H)
    assert r.status_code == 200 and r.headers["cache-control"] == NO_CACHE


def test_no_gzip_middleware_and_no_content_encoding(lite_client):
    """GZipMiddleware buffers streaming bodies and would stall the SSE stream: no middleware compresses. The one exception is
    /api/state, which gzips its own answer inside the route (#46: the 3 s poll, about 3.7x smaller on the box)."""
    from app import main
    assert not [m for m in main.app.user_middleware if "gzip" in repr(m).lower()], main.app.user_middleware
    for url in ("/", "/static/core.js", "/static/vendor/blueprint/blueprint.css"):
        r = lite_client.get(url, headers={**H, "Accept-Encoding": "gzip, br"})
        assert r.status_code == 200 and "content-encoding" not in r.headers, url
    r = lite_client.get("/api/state", headers={**H, "Accept-Encoding": "gzip, br"})
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip" and r.json()["projects"] is not None
