"""Gzip for the static files only (issue #103, app/staticgz.py): a script or style comes back gzipped for a client that accepts it and plain for one that
does not, both say Vary, the validators differ per variant, and nothing that streams is ever compressed. Written through lite_client: no pollers, no tmux
(the SSE case uses the conftest fake)."""
import gzip
import re

import pytest

from app import main, staticgz

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
GZ = {**H, "Accept-Encoding": "gzip, deflate, br"}
PLAIN = {**H, "Accept-Encoding": "identity"}          # the test client adds "gzip, deflate" on its own when the header is absent
BIG_TEXT = ["/static/core.js", "/static/shell.js", "/static/pages.css", "/static/vendor/blueprint/blueprint.css", "/static/vendor/uplot/uPlot.iife.min.js"]


@pytest.mark.parametrize("url", BIG_TEXT)
def test_a_script_or_style_is_gzipped_for_a_client_that_accepts_it(lite_client, url):
    plain = (main.STATIC / url[len("/static/"):]).read_bytes()
    r = lite_client.get(url, headers=GZ)
    assert r.status_code == 200, url
    assert r.headers["content-encoding"] == "gzip", url
    assert r.headers["vary"] == "Accept-Encoding", url
    assert int(r.headers["content-length"]) < len(plain) * 0.6, f"{url}: gzip should at least halve a text file"
    assert r.content == plain, "httpx decodes the body: it must be the file, byte for byte"
    assert r.headers["content-type"].split(";")[0] in ("text/javascript", "application/javascript", "text/css"), r.headers["content-type"]


@pytest.mark.parametrize("url", BIG_TEXT)
def test_without_accept_encoding_the_plain_file_comes_back_and_still_varies(lite_client, url):
    plain = (main.STATIC / url[len("/static/"):]).read_bytes()
    for h in (PLAIN, {**H, "Accept-Encoding": "br"}, {**H, "Accept-Encoding": "gzip;q=0, br"}):
        r = lite_client.get(url, headers=h)
        assert r.status_code == 200 and "content-encoding" not in r.headers, (url, h)
        assert r.headers["vary"] == "Accept-Encoding", f"{url}: a cache must know the body depends on the header"
        assert r.content == plain


def test_the_two_variants_have_their_own_validators_and_a_conditional_get_is_a_304_per_variant(lite_client):
    url = "/static/core.js"
    plain = lite_client.get(url, headers=PLAIN)
    gz = lite_client.get(url, headers=GZ)
    assert plain.headers["etag"] and gz.headers["etag"] and plain.headers["etag"] != gz.headers["etag"]
    assert gz.headers["etag"].endswith('-gzip"') and gz.headers["last-modified"] == plain.headers["last-modified"]
    again = lite_client.get(url, headers={**GZ, "If-None-Match": gz.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    assert again.headers["vary"] == "Accept-Encoding" and again.headers["etag"] == gz.headers["etag"]
    assert again.headers["cache-control"] == "no-cache", "the board's own cache policy still rides on the 304"
    again = lite_client.get(url, headers={**PLAIN, "If-None-Match": plain.headers["etag"]})
    assert again.status_code == 304 and again.headers["vary"] == "Accept-Encoding"
    mismatch = lite_client.get(url, headers={**GZ, "If-None-Match": plain.headers["etag"]})
    assert mismatch.status_code == 200 and mismatch.headers["content-encoding"] == "gzip", "the plain validator never vouches for the gzipped bytes"


def test_the_security_and_cache_headers_ride_along_on_a_gzipped_answer(lite_client):
    r = lite_client.get("/static/vendor/blueprint/blueprint.css", headers=GZ)
    assert r.headers["content-encoding"] == "gzip"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers["content-security-policy"] == "default-src 'self'; frame-ancestors 'none'"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["etag"]
    assert lite_client.get("/static/core.js", headers=GZ).headers["cache-control"] == "no-cache"


def test_head_and_range_are_never_gzipped_into_a_wrong_length(lite_client):
    url = "/static/core.js"
    full = (main.STATIC / "core.js").read_bytes()
    head = lite_client.head(url, headers=GZ)
    assert head.status_code == 200 and head.content == b"" and head.headers["content-encoding"] == "gzip"
    assert head.headers["content-length"] == lite_client.get(url, headers=GZ).headers["content-length"]
    part = lite_client.get(url, headers={**GZ, "Range": "bytes=0-9"})
    assert part.status_code == 206 and part.content == full[:10] and "content-encoding" not in part.headers, "a byte range is of the plain file"
    assert part.headers["vary"] == "Accept-Encoding"


@pytest.mark.parametrize("url", [
    "/static/vendor/fonts/inter-latin-wght-normal.woff2",         # already compressed
    "/static/vendor/blueprint/blueprint-icons-20.woff2",
    "/static/icon-192.png",
])
def test_compressed_formats_are_left_alone(lite_client, url):
    r = lite_client.get(url, headers=GZ)
    assert r.status_code == 200 and "content-encoding" not in r.headers and "vary" not in r.headers, url


def test_a_tiny_file_and_a_missing_file_are_not_gzipped(lite_client, tmp_path):
    f = tmp_path / "small.js"
    f.write_text("x" * 100)
    mount = staticgz.GzipStaticFiles(directory=str(tmp_path))
    assert mount.gzipped(f, f.stat()) is None
    assert lite_client.get("/static/missing.js", headers=GZ).status_code == 404


def test_the_gzip_is_made_once_per_file_version(tmp_path):
    f = tmp_path / "a.js"
    f.write_text("const a = 1;\n" * 400)
    mount = staticgz.GzipStaticFiles(directory=str(tmp_path))
    first = mount.gzipped(f, f.stat())
    assert first and gzip.decompress(first) == f.read_bytes()
    assert mount.gzipped(f, f.stat()) is first, "cached by path, mtime and size"
    f.write_text("const b = 2;\n" * 500)
    second = mount.gzipped(f, f.stat())
    assert second is not first and gzip.decompress(second) == f.read_bytes(), "a changed file is gzipped again"
    assert mount.warm() == 1


def test_a_file_that_does_not_shrink_is_sent_plain(tmp_path):
    import os
    f = tmp_path / "noise.js"
    f.write_bytes(os.urandom(4096))
    mount = staticgz.GzipStaticFiles(directory=str(tmp_path))
    assert mount.gzipped(f, f.stat()) is None and mount.gzipped(f, f.stat()) is None


@pytest.mark.parametrize("value,ok", [
    ("gzip", True), ("gzip, deflate, br", True), ("br, gzip;q=0.5", True), ("GZIP", True), ("*", True), ("*;q=0.1", True),
    ("gzip;q=0", False), ("gzip;q=0, *", False), ("identity", False), ("br", False), ("", False), (None, False), ("deflate;q=1, *;q=0", False),
    ("x-gzip", False), ("gzip;q=oops", False),
])
def test_accept_encoding_parsing(value, ok):
    assert staticgz.accepts_gzip(value) is ok, value


def test_the_event_stream_and_the_api_are_never_compressed(lite_client, fake_tmux):
    """No middleware gzips: the live tail is a server-sent event stream and a buffering compressor would stall it."""
    assert not [m for m in main.app.user_middleware if "gzip" in repr(m).lower()], main.app.user_middleware
    with lite_client.stream("GET", "/api/stream?once=1", headers=GZ) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert "content-encoding" not in r.headers
        body = "".join(r.iter_text())
    assert re.search(r"^event: tick", body, re.M)
    for url in ("/", "/sw.js", "/healthz", "/api/doctor"):
        r = lite_client.get(url, headers=GZ)
        assert "content-encoding" not in r.headers, f"{url}: only static text files are gzipped here (the state poll does its own)"
    st = lite_client.get("/api/state", headers=GZ)
    assert st.status_code == 200 and st.headers.get("content-encoding") == "gzip", "the state endpoint keeps its own gzip as before"


def test_the_service_worker_shell_still_lists_every_file_the_mount_serves(lite_client):
    """The worker precaches by URL and the browser decodes gzip before it sees the body: compression changes no shell path."""
    shell = main.shell_paths()
    assert "/static/core.js" in shell and "/static/vendor/blueprint/blueprint.css" in shell
    for u in shell[1:6]:
        r = lite_client.get(u, headers={**GZ, "Cache-Control": "no-cache"})
        assert r.status_code == 200, u
    sw = lite_client.get("/sw.js", headers=GZ)
    assert sw.status_code == 200 and b"__SHELL_JSON__" not in sw.content
