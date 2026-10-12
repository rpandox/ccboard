"""GitHub via the gh CLI on the box (already logged in by the user)."""
from __future__ import annotations

import collections
import email.utils
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any, NamedTuple
from urllib.parse import quote

FIELDS = "name,url,sshUrl,isArchived,isFork,isPrivate,updatedAt,description"


class GhError(Exception):
    pass


def _gh(*args: str, timeout: int = 60) -> str:
    exe = shutil.which("gh")
    if not exe:
        raise GhError("gh is not installed on the box")
    try:
        cp = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GhError("gh timed out")
    if cp.returncode != 0:
        err = (cp.stderr or cp.stdout).strip()
        if "auth login" in err or "not logged" in err.lower():
            raise GhError("gh is not logged in on the box: run `gh auth login` there")
        raise GhError(err[-300:] or "gh failed")
    return cp.stdout


def git_protocol() -> str:
    try:
        return _gh("config", "get", "git_protocol", timeout=10).strip() or "https"
    except GhError:
        return "https"


def list_repos(owner: str | None = None, limit: int = 200, include_archived: bool = False) -> list[dict]:
    args = ["repo", "list"]
    if owner:
        args.append(owner)
    args += ["--json", FIELDS, "--limit", str(min(max(limit, 1), 1000))]
    if not include_archived:
        args.append("--no-archived")
    try:
        data = json.loads(_gh(*args) or "[]")
    except ValueError:
        raise GhError("unexpected gh output")
    proto = git_protocol()
    out = []
    for r in data:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        url = r.get("sshUrl") if proto == "ssh" and r.get("sshUrl") else (r.get("url") or "") + ".git"
        out.append({"name": r["name"], "url": url, "private": bool(r.get("isPrivate")), "fork": bool(r.get("isFork")),
                    "archived": bool(r.get("isArchived")), "updated": r.get("updatedAt"), "description": r.get("description") or ""})
    return out


# ---------------------------------------------------------------- the read-only reader (nodes epic, issue #147)
#
# Everything below only ever GETs, through `gh api --include`, with an argv list (never a shell), a bounded timeout and a capped answer. owner, repo and
# issue number are checked against GitHub's own rules BEFORE they reach an argv, and `api()` refuses any path that is not one of the shapes below, so nothing
# a caller or GitHub sends can add an option or a second path. The token stays inside gh: it is never read, printed, logged or returned, and a gh failure is
# reduced to one of the fixed reasons in MESSAGES (no stderr text is ever passed on). Whatever GitHub returns is text written by other people: callers keep
# whitelisted fields only and run strings through `clean_text`.

OWNER_RE = re.compile(r"(?=.{1,39}\Z)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")             # GitHub's user and organisation rule: no leading, trailing or double dash
REPO_RE = re.compile(r"(?![-.])[A-Za-z0-9_.-]{1,100}")                          # no leading dash or dot, so never `.`, `..` or an option
NUM_RE = re.compile(r"[1-9][0-9]{0,8}")
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.-]{0,49}")
ETAG_RE = re.compile(r'(?:W/)?"[A-Za-z0-9+/=_.:-]{1,200}"')                      # an ETag echoed back to GitHub: no space, quote, CR or LF can ride along
PER_PAGE = 100
MAX_PAGES = 5
OUT_CAP = 2 * 1024 * 1024                    # bytes of one `gh api` answer; more is `too_large`
TIMEOUT = 20.0
CACHE_MAX = 400                             # entries: a 100-child epic needs about 205 (the parent, the list, a comments path and an issue path per child)
BACKOFF_MAX = 3600
BACKOFF_DEFAULT = 60                        # the secondary limit's own advice, when GitHub names no wait
SAFE_HEADERS = ("etag", "date", "retry-after", "link", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "x-oauth-scopes")

MESSAGES = {
    "no_gh": "gh is not installed on this board, so epics cannot be read from GitHub",
    "no_login": "gh is not logged in on this board: run gh auth login there",
    "no_access": "this gh login cannot read that repository",
    "not_found": "GitHub has no such issue, or this login cannot see it",
    "rate_limited": "GitHub asked us to wait",
    "timeout": "gh did not answer in time",
    "unavailable": "GitHub did not answer (an error on its side)",
    "bad_answer": "GitHub sent an answer this board could not read",
    "too_large": "GitHub's answer was larger than this board reads",
    "bad_request": "that is not a GitHub repository or issue this board will ask for",
}
_REPO_PATH = re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})")
_PATH = re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/issues(?:/[1-9][0-9]{0,8}(?:/(?:sub_issues|comments)\?per_page=[0-9]{1,3}&page=[0-9]{1,3})?"
                   r"|\?labels=[A-Za-z0-9%._-]{1,200}&state=(?:open|closed|all)&per_page=[0-9]{1,3}&page=[0-9]{1,3})")


class GhReadError(Exception):
    """A read that did not work, as a fixed reason (a key of MESSAGES) and a plain sentence. `wait_s` is set for `rate_limited`."""

    def __init__(self, reason: str, wait_s: int | None = None):
        self.reason = reason if reason in MESSAGES else "bad_answer"
        self.wait_s = wait_s
        super().__init__(self.message)

    @property
    def message(self) -> str:
        return f"{MESSAGES['rate_limited']} {self.wait_s} s" if self.reason == "rate_limited" and self.wait_s else MESSAGES[self.reason]

    def as_dict(self) -> dict:
        return {"reason": self.reason, "message": self.message, "wait_s": self.wait_s}


def valid_slug(owner, repo) -> bool:
    return isinstance(owner, str) and isinstance(repo, str) and bool(OWNER_RE.fullmatch(owner)) and bool(REPO_RE.fullmatch(repo))


def valid_number(n) -> bool:
    if isinstance(n, bool):
        return False
    if isinstance(n, int):
        return 0 < n < 10 ** 9
    return isinstance(n, str) and bool(NUM_RE.fullmatch(n))


_NO_NUMBER = object()


def _check(owner, repo, n=_NO_NUMBER) -> str:
    """/repos/{owner}/{repo}/issues, plus /{n} when a number is given; owner, repository and number must pass GitHub's own rules (GhReadError otherwise)."""
    if not valid_slug(owner, repo) or (n is not _NO_NUMBER and not valid_number(n)):
        raise GhReadError("bad_request")
    base = f"/repos/{owner}/{repo}/issues"
    return base if n is _NO_NUMBER else f"{base}/{int(n)}"


def _page(page, per_page) -> str:
    if not (isinstance(page, int) and not isinstance(page, bool) and 1 <= page <= 999 and isinstance(per_page, int) and not isinstance(per_page, bool) and 1 <= per_page <= 100):
        raise GhReadError("bad_request")
    return f"per_page={per_page}&page={page}"


def issue_path(owner, repo, n) -> str:
    return _check(owner, repo, n)


def sub_issues_path(owner, repo, n, page: int = 1, per_page: int | None = None) -> str:
    return f"{_check(owner, repo, n)}/sub_issues?{_page(page, PER_PAGE if per_page is None else per_page)}"


def comments_path(owner, repo, n, page: int = 1, per_page: int | None = None) -> str:
    return f"{_check(owner, repo, n)}/comments?{_page(page, PER_PAGE if per_page is None else per_page)}"


def labeled_path(owner, repo, label, state: str = "open", page: int = 1, per_page: int | None = None) -> str:
    if not isinstance(label, str) or not LABEL_RE.fullmatch(label) or state not in ("open", "closed", "all"):
        raise GhReadError("bad_request")
    return f"{_check(owner, repo)}?labels={quote(label, safe='')}&state={state}&{_page(page, PER_PAGE if per_page is None else per_page)}"


RATE_PATH = "/rate_limit"


def repo_path(owner, repo) -> str:
    """/repos/{owner}/{repo}: the repository itself, read by the doctor for `permissions.push` and `private` (a GET like every other path of this module)."""
    if not valid_slug(owner, repo):
        raise GhReadError("bad_request")
    return f"/repos/{owner}/{repo}"


def allowed_path(path) -> bool:
    """True for the paths this module may GET and nothing else: /repos/{owner}/{repo}/issues/{n}, .../sub_issues, .../comments, /repos/{owner}/{repo}/issues?labels=...,
    /repos/{owner}/{repo} itself (the doctor reads `permissions.push` and `private`, issue #148; owner and repo checked again against GitHub's rules) and /rate_limit."""
    if not isinstance(path, str) or len(path) > 400:
        return False
    if path == RATE_PATH:
        return True
    r = _REPO_PATH.fullmatch(path)
    if r:
        return valid_slug(r.group(1), r.group(2))
    m = _PATH.fullmatch(path)
    return bool(m) and valid_slug(m.group(1), m.group(2))


def argv(path: str, etag: str | None = None, exe: str = "gh") -> list[str]:
    """The one command line this module builds: `gh api --include --method GET -H ... <path>`. The path must be an allowed one; an ETag that is not a plain
    ETag is not sent."""
    if not allowed_path(path):
        raise GhReadError("bad_request")
    out = [exe, "api", "--include", "--method", "GET", "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28"]
    if isinstance(etag, str) and ETAG_RE.fullmatch(etag):
        out += ["-H", f"If-None-Match: {etag}"]
    return out + [path]


def clean_text(v, n: int = 200) -> str:
    """Untrusted text made safe to keep and show: control, format (bidi marks, zero-width characters), separator, private-use and unassigned characters are
    dropped, other white space becomes one space, at most `n` characters. Not a string: ''. The page still renders it with textContent."""
    if not isinstance(v, str):
        return ""
    out = []
    for ch in v[:65536]:
        cat = unicodedata.category(ch)
        if ch in " \t\n\r\f\v" or cat == "Zs":
            out.append(" ")
        elif cat in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            continue
        else:
            out.append(ch)
    return " ".join("".join(out).split())[:n]


_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def clean_ts(v) -> str | None:
    """A GitHub timestamp (`2026-10-10T09:00:00Z`) or None."""
    if isinstance(v, str) and _TS.fullmatch(v):
        try:
            datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
            return v
        except ValueError:
            return None
    return None


def ts_epoch(v) -> float | None:
    v = clean_ts(v)
    return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() if v else None


def epoch_iso(t) -> str | None:
    if isinstance(t, bool) or not isinstance(t, (int, float)) or t != t or not 0 < t < 4e9:
        return None
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_output(raw: bytes) -> tuple[int | None, dict, bytes]:
    """(status, headers, body) of `gh api --include` output; status None when there is no HTTP head. Only SAFE_HEADERS are kept, names in lower case."""
    text = raw.decode("utf-8", "replace")
    m = re.match(r"HTTP/\S+\s+(\d{3})", text)
    if not m:
        return None, {}, b""
    cut = re.search(r"\r?\n\r?\n", text)
    head, body = (text[:cut.start()], text[cut.end():]) if cut else (text, "")
    headers = {}
    for line in head.splitlines()[1:]:
        k, colon, v = line.partition(":")
        k = k.strip().lower()
        if colon and k in SAFE_HEADERS and len(v) <= 500:
            headers[k] = v.strip()
    return int(m.group(1)), headers, body.encode("utf-8")


def _exec(args: list[str]) -> tuple[int, bytes]:
    """Run the argv: (exit code, stdout). stderr is only classified, never kept. Raises GhReadError for a missing gh, a missing login, a timeout and an
    answer over OUT_CAP."""
    try:
        cp = subprocess.run(args, capture_output=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL,
                            env={**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "GH_NO_UPDATE_NOTIFIER": "1", "GH_SPINNER_DISABLED": "1"})
    except subprocess.TimeoutExpired:
        raise GhReadError("timeout") from None
    except OSError:
        raise GhReadError("no_gh") from None
    if len(cp.stdout) > OUT_CAP:
        raise GhReadError("too_large")
    err = (cp.stderr or b"")[:4096].decode("utf-8", "replace").lower()
    if (cp.returncode == 4 or "gh auth login" in err) and not cp.stdout.startswith(b"HTTP/"):      # exit 4 is gh's "authentication required"
        raise GhReadError("no_login")
    return cp.returncode, cp.stdout


def api(path: str, etag: str | None = None) -> tuple[int, dict, Any]:
    """GET `path` through `gh api --include`: (status, headers, json). Any HTTP status is returned (a 304 has data None); a failure of gh itself is a
    GhReadError. `etag` goes out as If-None-Match when it is a plain ETag. The Date header is in headers['date']."""
    exe = shutil.which("gh")
    if not exe:
        raise GhReadError("no_gh")
    rc, out = _exec(argv(path, etag, exe))
    status, headers, body = _parse_output(out)
    if status is None:
        raise GhReadError("unavailable" if rc != 0 else "bad_answer")
    data = None
    if body.strip() and (200 <= status < 300 or status >= 400):
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            if 200 <= status < 300:
                raise GhReadError("bad_answer") from None
    return status, headers, data


def server_time(headers: dict) -> float | None:
    """The Date header as epoch seconds (GitHub's clock), None when absent or not a date."""
    try:
        d = email.utils.parsedate_to_datetime(headers.get("date") or "")
        return d.timestamp() if d is not None and d.tzinfo is not None else None
    except (TypeError, ValueError, IndexError):
        return None


def _int(v) -> int | None:
    return int(v) if isinstance(v, str) and v.isascii() and v.isdigit() and len(v) < 12 else None


def backoff_wait(status: int, headers: dict, server_now: float | None, message) -> int | None:
    """Seconds GitHub wants us to stay away, from a rate-limit answer or an exhausted budget; None when this answer asks for nothing. Retry-After wins;
    else the reset time minus GitHub's own Date; else 60 s (the secondary limit's advice). Never longer than an hour."""
    ra, remaining = _int(headers.get("retry-after")), headers.get("x-ratelimit-remaining")
    said = isinstance(message, str) and "rate limit" in message.lower()
    limited = status == 429 or (status == 403 and (ra is not None or remaining == "0" or said)) or (status in (200, 304) and remaining == "0")
    if not limited:
        return None
    wait = ra
    if wait is None:
        reset = _int(headers.get("x-ratelimit-reset"))
        if remaining == "0" and reset is not None and server_now is not None:
            wait = reset - int(server_now)
            if wait <= 0 and status in (200, 304):
                return None
        if wait is None or wait <= 0:
            wait = BACKOFF_DEFAULT
    return max(1, min(BACKOFF_MAX, wait))


class Reply(NamedTuple):
    data: Any
    headers: dict
    server_now: float | None          # GitHub's clock when this was confirmed (the Date header)
    cached: bool                      # served from the memory cache (no gh call, or a 304)
    stale: bool                       # older than asked for, kept because GitHub asked us to wait
    scopes: str | None = None         # X-OAuth-Scopes of the answer that confirmed this (None: a fine-grained token sends none)
    source: str = "network"           # where it came from: network (a 200 just now), revalidated (a 304 just now), cache (a fresh entry, no call) or stale (kept during a back-off)

    @property
    def fresh(self) -> bool:
        """True when GitHub itself confirmed this answer in this call (a 200 or a 304 on If-None-Match), not a cache entry or a stale one."""
        return self.source in ("network", "revalidated")


class _Entry:
    __slots__ = ("etag", "data", "headers", "server_now", "at", "scopes")

    def __init__(self, etag, data, headers, server_now, at, scopes=None):
        self.etag, self.data, self.headers, self.server_now, self.at, self.scopes = etag, data, headers, server_now, at, scopes


class Reader:
    """GET with a memory cache (ETag + If-None-Match, a TTL per call), one request at a time, and a back-off that is kept in memory and never looped on.
    No thread, no timer: it works only when a caller asks."""

    def __init__(self, fetch=None, mono=time.monotonic):
        self.fetch = fetch
        self.mono = mono
        self.lock = threading.RLock()
        self.cache: "collections.OrderedDict[str, _Entry]" = collections.OrderedDict()
        self.until = 0.0
        self.limit: int | None = None
        self.remaining: int | None = None
        self.reset: int | None = None
        self.calls = 0

    # ---- the back-off
    def wait_left(self) -> int:
        return max(0, math.ceil(self.until - self.mono()))

    def rate(self) -> dict:
        """{remaining, limit, reset (ISO, GitHub's clock), wait_s}: what the last answers said; Nones when GitHub has not been asked."""
        with self.lock:
            return {"remaining": self.remaining, "limit": self.limit, "reset": epoch_iso(self.reset), "wait_s": self.wait_left() or None}

    def reset_state(self) -> None:
        with self.lock:
            self.cache.clear()
            self.until, self.limit, self.remaining, self.reset, self.calls = 0.0, None, None, None, 0

    def _note(self, status, headers, server_now, data) -> int | None:
        if "x-ratelimit-limit" in headers:
            self.limit = _int(headers["x-ratelimit-limit"])
        if "x-ratelimit-remaining" in headers:
            self.remaining, self.reset = _int(headers["x-ratelimit-remaining"]), _int(headers.get("x-ratelimit-reset"))
        wait = backoff_wait(status, headers, server_now, data.get("message") if isinstance(data, dict) else None)
        if wait:
            self.until = max(self.until, self.mono() + wait)
        return wait

    # ---- the read
    def get(self, path: str, ttl: float = 60.0) -> Reply:
        """The JSON at `path`. A fresh cache entry (younger than `ttl`) costs no call; an older one is revalidated with its ETag (a 304 is free of the
        primary limit). Raises GhReadError (rate_limited carries wait_s); while GitHub has asked us to wait, an old entry is returned as `stale` and
        nothing is asked."""
        with self.lock:
            ent = self.cache.get(path)
            if ent is not None and self.mono() - ent.at < ttl:
                self.cache.move_to_end(path)
                return Reply(ent.data, ent.headers, ent.server_now, True, False, ent.scopes, "cache")
            left = self.wait_left()
            if left > 0:
                if ent is not None:
                    return Reply(ent.data, ent.headers, ent.server_now, True, True, ent.scopes, "stale")
                raise GhReadError("rate_limited", left)
            self.calls += 1
            status, headers, data = (self.fetch or api)(path, ent.etag if ent is not None else None)
            server_now = server_time(headers)
            wait = self._note(status, headers, server_now, data)
            now = self.mono()
            scopes = clean_text(headers["x-oauth-scopes"], 200) if "x-oauth-scopes" in headers else None          # of THIS answer, not of the merged cache headers
            if status == 304 and ent is not None:
                ent.at, ent.server_now, ent.scopes = now, server_now if server_now is not None else ent.server_now, scopes
                ent.headers = {**ent.headers, **headers}                                                           # a 304 carries the current headers
                self.cache.move_to_end(path)
                return Reply(ent.data, ent.headers, ent.server_now, True, False, scopes, "revalidated")
            if 200 <= status < 300 and isinstance(data, (list, dict)):
                tag = headers.get("etag")
                self.cache[path] = _Entry(tag if isinstance(tag, str) and ETAG_RE.fullmatch(tag) else None, data, headers, server_now, now, scopes)
                self.cache.move_to_end(path)
                while len(self.cache) > CACHE_MAX:
                    self.cache.popitem(last=False)
                return Reply(data, headers, server_now, False, False, scopes, "network")
            if status in (404, 410):
                self.cache.pop(path, None)
                raise GhReadError("not_found")
            if wait:
                if ent is not None:
                    return Reply(ent.data, ent.headers, ent.server_now, True, True, ent.scopes, "stale")
                raise GhReadError("rate_limited", wait)
            if status == 401:
                raise GhReadError("no_login")
            if status == 403:
                raise GhReadError("no_access")
            if status >= 500:
                raise GhReadError("unavailable")
            raise GhReadError("bad_answer")


reader = Reader()
