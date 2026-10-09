"""Gzip for the board's STATIC files only (issue #103: about 1.6 MB of scripts and styles went out uncompressed).

`GzipStaticFiles` is StaticFiles that answers a text file (script, style, page, manifest, JSON, SVG) with a gzipped body when the client sends
`Accept-Encoding: gzip`, and with the plain file otherwise. It is not a middleware on purpose: a response middleware would also wrap the live tail
(server-sent events), the MCP endpoint and every other streaming answer, and buffer them. Only files read from disk by this mount are touched.

  * Both variants say `Vary: Accept-Encoding`; the gzipped one has its own validator (`"<etag>-gzip"`), so a cache or a conditional GET can
    never be answered with the wrong variant, and a 304 keeps the headers of the variant it validated.
  * The gzipped bytes are made once per file version (path, mtime, size) and kept in memory (about 0.5 MB for the whole UI); `warm()` fills the cache
    in the background at startup so the first page load does not pay for it.
  * A Range request, a file under MIN_BYTES, a type that is already compressed (woff2, png) and a file that does not shrink are served as they are.
  * The service worker needs nothing: the browser decodes the body before the worker or the page sees it.

Not here, and unchanged: /api/state gzips its own answer inside the route (app/main.py state_response), and /tty and the terminal proxy never pass through the board."""
from __future__ import annotations

import gzip
import logging
import os
import re
from pathlib import Path

from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response
from starlette.staticfiles import NotModifiedResponse, StaticFiles

log = logging.getLogger("ccboard.staticgz")

COMPRESSIBLE = {".js", ".css", ".html", ".json", ".webmanifest", ".svg", ".txt", ".map"}
MIN_BYTES = 1024
GZIP_LEVEL = 9                         # made once per file version, so the slowest and smallest level costs nothing per request
MAX_CACHED = 512                       # entries; the UI has about 50 files, the cap only stops an unbounded directory from growing the cache forever
VARY = "Accept-Encoding"

_Q = re.compile(r"^\s*([A-Za-z0-9*._-]+)\s*(?:;\s*q\s*=\s*([0-9.]+))?\s*$")


def accepts_gzip(header: str | None) -> bool:
    """True when an Accept-Encoding value allows gzip: `gzip` (or `*` when gzip is not named) with a quality above 0."""
    if not header:
        return False
    named: dict[str, float] = {}
    for part in header.split(","):
        m = _Q.match(part)
        if not m:
            continue
        try:
            q = float(m.group(2)) if m.group(2) is not None else 1.0
        except ValueError:
            q = 0.0
        named[m.group(1).lower()] = q
    if "gzip" in named:
        return named["gzip"] > 0
    return named.get("*", 0.0) > 0


class _GzipBody(Response):
    """A fully-buffered gzip body; a HEAD request gets the headers (with the real Content-Length) and no body."""

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("method", "GET").upper() != "HEAD":
            await super().__call__(scope, receive, send)
            return
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        await send({"type": "http.response.body", "body": b""})


class GzipStaticFiles(StaticFiles):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._gz: dict[str, tuple[int, int, bytes]] = {}          # full path -> (mtime_ns, size, gzipped bytes)

    @staticmethod
    def compressible(path) -> bool:
        return Path(path).suffix.lower() in COMPRESSIBLE

    def gzipped(self, full_path, stat_result: os.stat_result) -> bytes | None:
        """The gzipped bytes of a file, or None when it is better sent as is (too small, or gzip does not shrink it)."""
        if stat_result.st_size < MIN_BYTES:
            return None
        key = str(full_path)
        hit = self._gz.get(key)
        if hit and hit[0] == stat_result.st_mtime_ns and hit[1] == stat_result.st_size:
            return hit[2] or None
        try:
            raw = Path(full_path).read_bytes()
        except OSError:
            return None
        gz = gzip.compress(raw, GZIP_LEVEL, mtime=0)             # mtime 0: the same bytes every time for the same file
        body = gz if len(gz) < len(raw) else b""                  # b"" remembers 'does not shrink'
        if len(self._gz) >= MAX_CACHED:
            self._gz.pop(next(iter(self._gz)))
        self._gz[key] = (stat_result.st_mtime_ns, stat_result.st_size, body)
        return body or None

    def file_response(self, full_path, stat_result, scope, status_code: int = 200) -> Response:
        if not self.compressible(full_path):
            return super().file_response(full_path, stat_result, scope, status_code)
        request_headers = Headers(scope=scope)
        plain = FileResponse(full_path, status_code=status_code, stat_result=stat_result)
        plain.headers["Vary"] = VARY                              # the answer depends on Accept-Encoding whichever variant this request gets
        gz = None
        if status_code == 200 and "range" not in request_headers and accepts_gzip(request_headers.get("accept-encoding")):
            gz = self.gzipped(full_path, stat_result)
        if gz is None:
            if self.is_not_modified(plain.headers, request_headers):
                return NotModifiedResponse(plain.headers)
            return plain
        headers = {"Content-Encoding": "gzip", "Vary": VARY, "Content-Type": plain.headers["content-type"],
                   "Last-Modified": plain.headers["last-modified"], "ETag": plain.headers["etag"][:-1] + '-gzip"'}
        if self.is_not_modified(Headers(headers), request_headers):
            return NotModifiedResponse(Headers(headers))
        return _GzipBody(gz, status_code=200, headers={**headers, "Content-Length": str(len(gz))})

    def warm(self) -> int:
        """Gzip every compressible file under the served directories now (a background thread at startup). Returns how many are cached."""
        n = 0
        for d in self.all_directories:
            for root, _dirs, files in os.walk(d):
                for name in files:
                    p = os.path.join(root, name)
                    if not self.compressible(p):
                        continue
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    if self.gzipped(p, st):
                        n += 1
        return n
