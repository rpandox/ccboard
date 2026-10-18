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
        return _gh("config", "get", "git_protocol", timeout=CONFIG_TIMEOUT).strip() or "https"
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
CONFIG_TIMEOUT = 10                         # seconds for `gh config get`
CACHE_MAX = 400                             # entries: a 100-child epic needs about 205 (the parent, the list, a comments path and an issue path per child)
BACKOFF_MAX = 3600
BACKOFF_DEFAULT = 60                        # the secondary limit's own advice, when GitHub names no wait
SAFE_HEADERS = ("etag", "date", "retry-after", "link", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "x-ratelimit-resource", "x-oauth-scopes", "x-github-sso")

MESSAGES = {
    "no_gh": "gh is not installed on this board, so epics cannot be read from GitHub",
    "no_login": "gh is not logged in on this board: run gh auth login there",
    "no_access": "this gh login cannot read that repository",
    "sso": "this token must be authorised for the organization that owns the repository (SAML single sign-on)",
    "not_found": "GitHub has no such issue, or this login cannot see it",
    "rate_limited": "GitHub asked us to wait",
    "timeout": "gh did not answer in time",
    "unavailable": "GitHub did not answer (an error on its side)",
    "bad_answer": "GitHub sent an answer this board could not read",
    "too_large": "GitHub's answer was larger than this board reads",
    "bad_request": "that is not a GitHub repository or issue this board will ask for",
}
_REPO_PATH = re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})")
_PATH = re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/issues(?:/[1-9][0-9]{0,8}(?:/(?:sub_issues|comments|events)\?per_page=[0-9]{1,3}&page=[0-9]{1,3})?"
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


def events_path(owner, repo, n, page: int = 1, per_page: int | None = None) -> str:
    """/repos/{owner}/{repo}/issues/{n}/events: one page of the issue's events (issue #157: who set the label `ccboard:ready`, and who renamed the issue). A GET like every other path."""
    return f"{_check(owner, repo, n)}/events?{_page(page, PER_PAGE if per_page is None else per_page)}"


def labeled_path(owner, repo, label, state: str = "open", page: int = 1, per_page: int | None = None) -> str:
    if not isinstance(label, str) or not LABEL_RE.fullmatch(label) or state not in ("open", "closed", "all"):
        raise GhReadError("bad_request")
    return f"{_check(owner, repo)}?labels={quote(label, safe='')}&state={state}&{_page(page, PER_PAGE if per_page is None else per_page)}"


RATE_PATH = "/rate_limit"

# The lists the team reader asks for (issue #197): the open issues somebody is assigned to, and the open pull requests. Issue #200 adds the open issues nobody is assigned to (the free
# issues of the Team view). All are plain GETs and all can be revalidated with an ETag.
_TEAM_LIST = re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/(?:issues\?state=open&assignee=(?:\*|none)|pulls\?state=open)&per_page=[0-9]{1,3}&page=[0-9]{1,3}")


def assigned_issues_path(owner, repo, page: int = 1, per_page: int | None = None) -> str:
    """/repos/{owner}/{repo}/issues?state=open&assignee=*: the open issues (and pull requests, which GitHub lists as issues) that have an assignee."""
    return f"{_check(owner, repo)}?state=open&assignee=*&{_page(page, PER_PAGE if per_page is None else per_page)}"


def unassigned_issues_path(owner, repo, page: int = 1, per_page: int | None = None) -> str:
    """/repos/{owner}/{repo}/issues?state=open&assignee=none (issue #200): the open issues nobody is assigned to (GitHub lists pull requests among them; the reader drops those)."""
    return f"{_check(owner, repo)}?state=open&assignee=none&{_page(page, PER_PAGE if per_page is None else per_page)}"


def open_pulls_path(owner, repo, page: int = 1, per_page: int | None = None) -> str:
    if not valid_slug(owner, repo):
        raise GhReadError("bad_request")
    return f"/repos/{owner}/{repo}/pulls?state=open&{_page(page, PER_PAGE if per_page is None else per_page)}"


# The files one pull request changes (issue #199, Team P3): a plain GET, 100 a page, with an ETag. It is the only cross-person file signal: the list is what every reader of the
# repository can already see on the pull request. The number and the page are checked, the owner and repository against GitHub's rules.
_PULL_FILES = re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/pulls/[1-9][0-9]{0,8}/files\?per_page=[0-9]{1,3}&page=[0-9]{1,3}")


def pull_files_path(owner, repo, n, page: int = 1, per_page: int | None = None) -> str:
    """/repos/{owner}/{repo}/pulls/{n}/files: the files of one pull request, one page (a GET like every other path of this module)."""
    if not valid_slug(owner, repo) or not valid_number(n):
        raise GhReadError("bad_request")
    return f"/repos/{owner}/{repo}/pulls/{int(n)}/files?{_page(page, PER_PAGE if per_page is None else per_page)}"


def slim_files(path: str, data):
    """The one answer of this module that is cut before it is kept: a page of /pulls/{n}/files holds `patch` text, up to 2 MiB a page, and only the names are ever read from it, so
    only `filename` and `previous_filename` stay (and at most PER_PAGE entries). Any other path, and any other shape, is returned as it is."""
    if not isinstance(data, list) or not _PULL_FILES.fullmatch(path):
        return data
    return [{k: f[k] for k in ("filename", "previous_filename") if isinstance(f.get(k), str)} for f in data[:PER_PAGE] if isinstance(f, dict)]


_UNASSIGNED = re.compile(r"/repos/[^/?]{1,100}/[^/?]{1,100}/issues\?state=open&assignee=none&per_page=[0-9]{1,3}&page=[0-9]{1,3}")


def slim_unassigned(path: str, data):
    """The second answer of this module that is cut before it is kept (issue #200): a page of the open issues nobody is assigned to holds every issue's `body` (up to 65 KB each, 100 a
    page), and only the number, title, label names and `updated_at` are ever read, so only those stay. A pull request GitHub lists among the issues stays as `{"pull_request": true}`
    (so the page still counts as full) and is dropped by the reader. Any other path, and any other shape, is returned as it is."""
    if not isinstance(data, list) or not _UNASSIGNED.fullmatch(path):
        return data
    out = []
    for it in data[:PER_PAGE]:
        if not isinstance(it, dict):
            continue
        if "pull_request" in it:
            out.append({"pull_request": True})
            continue
        labels = []
        for lb in (it.get("labels") or [])[:30] if isinstance(it.get("labels"), list) else []:
            name = lb.get("name") if isinstance(lb, dict) else lb
            if isinstance(name, str):
                labels.append({"name": name[:200]})
        out.append({"number": it.get("number") if isinstance(it.get("number"), int) else None, "title": it["title"][:1000] if isinstance(it.get("title"), str) else "",
                    "labels": labels, "updated_at": it["updated_at"][:40] if isinstance(it.get("updated_at"), str) else None})
    return out


def repo_path(owner, repo) -> str:
    """/repos/{owner}/{repo}: the repository itself, read by the doctor for `permissions.push` and `private` (a GET like every other path of this module)."""
    if not valid_slug(owner, repo):
        raise GhReadError("bad_request")
    return f"/repos/{owner}/{repo}"


# A branch of a repository (issue #198, Team P2): does it exist on GitHub? A presence line names a branch only when GitHub has it (a 404 is "not pushed", and a local-only name
# never leaves the box). The name is checked against a narrow rule first: letters, digits, `.`, `_`, `-` and single slashes, no `..`, no leading dash or dot.
BRANCH_NAME_RE = re.compile(r"(?![-.])[A-Za-z0-9._-]{1,100}(?:/(?![-.])[A-Za-z0-9._-]{1,100}){0,5}")
_BRANCH_PATH = re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/branches/([A-Za-z0-9._/-]{1,250})")


def valid_branch(name) -> bool:
    return isinstance(name, str) and len(name) <= 200 and bool(BRANCH_NAME_RE.fullmatch(name)) and ".." not in name and not name.endswith((".lock", "/", "."))


def branch_path(owner, repo, name) -> str:
    """/repos/{owner}/{repo}/branches/{name}: one branch, read to learn whether it was pushed (a GET like every other path of this module)."""
    if not valid_slug(owner, repo) or not valid_branch(name):
        raise GhReadError("bad_request")
    return f"/repos/{owner}/{repo}/branches/{name}"


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
    t = _TEAM_LIST.fullmatch(path)
    if t:
        return valid_slug(t.group(1), t.group(2))
    f = _PULL_FILES.fullmatch(path)
    if f:
        return valid_slug(f.group(1), f.group(2))
    b = _BRANCH_PATH.fullmatch(path)
    if b:
        return valid_slug(b.group(1), b.group(2)) and valid_branch(b.group(3))
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


_NOT_MODIFIED = re.compile(r"gh: (?:Not Modified \()?HTTP 304\)?(?:\s|$)")


def is_not_modified(rc: int, stdout: bytes, stderr_head: str) -> bool:
    """gh 2.87 answers a conditional read (`If-None-Match`) that GitHub accepted with exit 1, the text `gh: HTTP 304` on stderr and no JSON: that is "not modified", not a failure.
    True only for exactly that: exit code 1, no HTTP head on stdout, and a stderr line that is gh's own `HTTP 304` (not 3040, not 404, not another 3xx). Any other non-zero exit stays an error."""
    if rc != 1 or stdout.startswith(b"HTTP/"):
        return False
    return any(_NOT_MODIFIED.match(line) is not None for line in stderr_head.splitlines()[:5])


def _exec(args: list[str]) -> tuple[int, bytes, bool]:
    """Run the argv: (exit code, stdout, whether gh said `HTTP 304`). stderr is only classified, never kept. Raises GhReadError for a missing gh, a missing login, a timeout and an
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
    raw_err = (cp.stderr or b"")[:4096].decode("utf-8", "replace")
    err = raw_err.lower()
    if (cp.returncode == 4 or "gh auth login" in err) and not cp.stdout.startswith(b"HTTP/"):      # exit 4 is gh's "authentication required"
        raise GhReadError("no_login")
    return cp.returncode, cp.stdout, is_not_modified(cp.returncode, cp.stdout, raw_err)


def _date_now() -> str:
    """This box's clock as an HTTP Date. gh prints no head for a 304, so the reader would otherwise keep the clock of the last 200 for ever; the box's clock is within seconds
    of GitHub's while NTP runs, and the claim lease it is compared with is minutes."""
    return email.utils.formatdate(usegmt=True)


def api(path: str, etag: str | None = None) -> tuple[int, dict, Any]:
    """GET `path` through `gh api --include`: (status, headers, json). Any HTTP status is returned (a 304 has data None); a failure of gh itself is a
    GhReadError. `etag` goes out as If-None-Match when it is a plain ETag. The Date header is in headers['date']. A 304 that gh reports only as exit 1 and
    `gh: HTTP 304` (no HTTP head) is returned as (304, {'date': now}, None), and only when an ETag was sent: the Date is this box's clock, so a revalidated reply moves the clock on."""
    exe = shutil.which("gh")
    if not exe:
        raise GhReadError("no_gh")
    sent = argv(path, etag, exe)
    rc, out, not_modified = _exec(sent)
    status, headers, body = _parse_output(out)
    if status is None:
        if not_modified and any(a.startswith("If-None-Match: ") for a in sent):
            return 304, {"date": _date_now()}, None
        raise GhReadError("unavailable" if rc != 0 else "bad_answer")
    data = None
    if body.strip() and (200 <= status < 300 or status >= 400):
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            if 200 <= status < 300:
                raise GhReadError("bad_answer") from None
    return status, headers, data


# ---- GraphQL, read only (issue #197). GitHub's GraphQL endpoint takes every query as a POST on the wire, and `gh api graphql -f query=...` is how gh sends one; that is
# unavoidable and it writes nothing: the ONE query below is a constant (it has no mutation and no variable that is not a plain owner or repository name, checked against
# GitHub's rules first), no `--method` flag is built, and `graphql_argv` builds nothing else. The linked branches of assigned issues and of the (first 100) unassigned ones, so
# an unassigned issue with a linked branch is not shown as free (issue #200), the closing issues of open pull
# requests and the branch tips come from it, because REST has no 304 path for them and no linked-branch list at all.
GQL_TEAM = (
    "query($owner:String!,$name:String!){rateLimit{cost remaining resetAt}"
    "repository(owner:$owner,name:$name){nameWithOwner isPrivate isFork parent{nameWithOwner}"
    "issues(states:OPEN,first:30,filterBy:{assignee:\"*\"},orderBy:{field:UPDATED_AT,direction:DESC}){nodes{number "
    "linkedBranches(first:3){nodes{ref{name target{... on Commit{committedDate author{user{login databaseId}}}}}}}}}"
    "unassigned:issues(states:OPEN,first:100,filterBy:{assignee:null},orderBy:{field:UPDATED_AT,direction:DESC}){nodes{number "
    "linkedBranches(first:1){nodes{ref{name target{... on Commit{committedDate author{user{login databaseId}}}}}}}}}"
    "pullRequests(states:OPEN,first:30,orderBy:{field:UPDATED_AT,direction:DESC}){nodes{number isDraft isCrossRepository headRefName "
    "author{login ... on User{databaseId}} closingIssuesReferences(first:3){nodes{number}}}}"
    "refs(refPrefix:\"refs/heads/\",first:100){pageInfo{hasNextPage} nodes{name target{... on Commit{committedDate author{user{login databaseId}}}}}}}}"
)
GQL_WORDS = ("mutation", "subscription")


def graphql_argv(owner, repo, exe: str = "gh") -> list[str]:
    """`gh api graphql --include -f query=<the constant query> -f owner=<owner> -f name=<repo>`: -f (never -F, which would turn a numeric-looking name into a number), owner and
    repository checked against GitHub's rules first, nothing else in the command."""
    if not valid_slug(owner, repo) or any(w in GQL_TEAM.lower() for w in GQL_WORDS):
        raise GhReadError("bad_request")
    return [exe, "api", "graphql", "--include", "-f", "query=" + GQL_TEAM, "-f", f"owner={owner}", "-f", f"name={repo}"]


# The editor check of the presence comments (issue #198, Team P2): REST does not say who last edited a comment, GraphQL does (`editor`, `lastEditedAt`). A second constant query,
# read only like GQL_TEAM: the last EDITORS_MAX comments of ONE issue (the status issue), each with its id, its author's and its last editor's numeric id. Only owner, name and
# the issue number vary; the number goes with -F (GraphQL wants an Int) and is checked against GitHub's rule first.
EDITORS_MAX = 50
GQL_EDITORS = (
    "query($owner:String!,$name:String!,$number:Int!){rateLimit{cost remaining resetAt}"
    "repository(owner:$owner,name:$name){issue(number:$number){comments(last:" + str(EDITORS_MAX) + "){nodes{databaseId lastEditedAt "
    "author{login ... on User{databaseId}} editor{login ... on User{databaseId}}}}}}}"
)


def graphql_editors_argv(owner, repo, n, exe: str = "gh") -> list[str]:
    """`gh api graphql --include -f query=<GQL_EDITORS> -f owner=<owner> -f name=<repo> -F number=<n>`: owner, repository and number checked first, nothing else in the command."""
    if not valid_slug(owner, repo) or not valid_number(n) or any(w in GQL_EDITORS.lower() for w in GQL_WORDS):
        raise GhReadError("bad_request")
    return [exe, "api", "graphql", "--include", "-f", "query=" + GQL_EDITORS, "-f", f"owner={owner}", "-f", f"name={repo}", "-F", f"number={int(n)}"]


def graphql_editors(owner, repo, n) -> tuple[int, dict, Any]:
    """The editor query for issue n: (status, headers, json) like `graphql`."""
    return _gql_run(lambda exe: graphql_editors_argv(owner, repo, n, exe))


def _gql_run(build) -> tuple[int, dict, Any]:
    exe = shutil.which("gh")
    if not exe:
        raise GhReadError("no_gh")
    rc, out, _ = _exec(build(exe))
    status, headers, body = _parse_output(out)
    if status is None:
        raise GhReadError("unavailable" if rc != 0 else "bad_answer")
    data = None
    if body.strip():
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            if 200 <= status < 300:
                raise GhReadError("bad_answer") from None
    return status, headers, data


# The same check for ONE comment, by its node id (REST gives every comment a `node_id`): a comment older than the last EDITORS_MAX of its issue is out of the window above and would get
# no verdict. A third constant query; only the id varies, and it is checked against a narrow rule first.
GQL_EDITOR = ("query($id:ID!){rateLimit{cost remaining resetAt}node(id:$id){... on IssueComment{databaseId lastEditedAt author{login ... on User{databaseId}} "
              "editor{login ... on User{databaseId}}} ... on Issue{databaseId lastEditedAt author{login ... on User{databaseId}} editor{login ... on User{databaseId}}}}}")
# Issue #157: the same query answers for an ISSUE's node id too (the worker's check of who last edited an issue's body, which REST does not say). Same fields, same parser.
NODE_ID_RE = re.compile(r"[A-Za-z0-9_=-]{4,100}")


def clean_node_id(v) -> str | None:
    return v if isinstance(v, str) and NODE_ID_RE.fullmatch(v) else None


def graphql_editor_argv(node_id, exe: str = "gh") -> list[str]:
    """`gh api graphql --include -f query=<GQL_EDITOR> -f id=<node id>`: the id checked first, nothing else in the command."""
    if clean_node_id(node_id) is None or any(w in GQL_EDITOR.lower() for w in GQL_WORDS):
        raise GhReadError("bad_request")
    return [exe, "api", "graphql", "--include", "-f", "query=" + GQL_EDITOR, "-f", f"id={node_id}"]


def graphql_editor(node_id) -> tuple[int, dict, Any]:
    """The editor query for one comment: (status, headers, json) like `graphql`."""
    return _gql_run(lambda exe: graphql_editor_argv(node_id, exe))


# Only the node id and an opaque page cursor vary. Title history is bounded too; an
# incomplete timeline cannot authorize the title that becomes the task prompt.
GQL_EDIT_HISTORY = (
    "query($id:ID!,$cursor:String){rateLimit{cost remaining resetAt}node(id:$id){__typename ... on Issue{"
    "id number databaseId updatedAt lastEditedAt repository{nameWithOwner} author{login ... on User{databaseId}} "
    "userContentEdits(first:100,after:$cursor){nodes{editedAt deletedAt editor{__typename login ... on User{databaseId}}} pageInfo{hasNextPage endCursor}} "
    "timelineItems(itemTypes:[RENAMED_TITLE_EVENT],first:50){nodes{... on RenamedTitleEvent{createdAt actor{__typename login ... on User{databaseId}}}} pageInfo{hasNextPage}}}}}"
)
EDIT_CURSOR_RE = re.compile(r"[A-Za-z0-9_+/=-]{1,256}")
EDIT_PAGES = 3


def graphql_edit_history_argv(node_id, cursor=None, exe: str = "gh") -> list[str]:
    if clean_node_id(node_id) is None or any(w in GQL_EDIT_HISTORY.lower() for w in GQL_WORDS) or (cursor is not None and
            (not isinstance(cursor, str) or not EDIT_CURSOR_RE.fullmatch(cursor))):
        raise GhReadError("bad_request")
    args = [exe, "api", "graphql", "--include", "-f", "query=" + GQL_EDIT_HISTORY, "-f", f"id={node_id}"]
    return args + (["-f", f"cursor={cursor}"] if cursor is not None else [])


def graphql_edit_history(node_id, cursor=None) -> tuple[int, dict, Any]:
    return _gql_run(lambda exe: graphql_edit_history_argv(node_id, cursor, exe))


def parse_edit_history(data, issue_id) -> dict | None:
    d = data.get("data") if isinstance(data, dict) else None
    node = d.get("node") if isinstance(d, dict) else None
    if not isinstance(node, dict) or node.get("__typename") != "Issue" or not isinstance(issue_id, int) or isinstance(issue_id, bool) or issue_id <= 0 or _gql_uid(node) != issue_id:
        return None
    edits, renames = node.get("userContentEdits"), node.get("timelineItems")
    rate = d.get("rateLimit") if isinstance(d.get("rateLimit"), dict) else {}
    out = {"complete": True, "edits": [], "renames": [], "last_edited_at": node.get("lastEditedAt"),
           "cost": rate.get("cost"), "remaining": rate.get("remaining"), "reset_at": clean_ts(rate.get("resetAt")),
           "updated_at": clean_ts(node.get("updatedAt")), "node_id": node.get("id"), "number": node.get("number"),
           "repo": (node.get("repository") or {}).get("nameWithOwner") if isinstance(node.get("repository"), dict) else None,
           "next_cursor": None, "has_next": False}
    if "lastEditedAt" not in node or (node["lastEditedAt"] is not None and not clean_ts(node["lastEditedAt"])):
        out["complete"] = False
    for connection, dest, actor, date in ((edits, "edits", "editor", "editedAt"), (renames, "renames", "actor", "createdAt")):
        if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list) or not isinstance(connection.get("pageInfo"), dict):
            out["complete"] = False
            continue
        page = connection["pageInfo"]
        if len(connection["nodes"]) > (100 if dest == "edits" else 50) or (page.get("hasNextPage") and not connection["nodes"]):
            out["complete"] = False
        if not isinstance(page.get("hasNextPage"), bool):
            out["complete"] = False
        if dest == "edits":
            out["has_next"], out["next_cursor"] = page.get("hasNextPage"), page.get("endCursor")
            if out["has_next"] and (not isinstance(out["next_cursor"], str) or not EDIT_CURSOR_RE.fullmatch(out["next_cursor"])):
                out["complete"] = False
        elif page.get("hasNextPage"):
            out["complete"] = False
        for item in connection["nodes"]:
            item = item if isinstance(item, dict) else {}
            u = item.get(actor)
            uid = _gql_uid(u)
            login = u.get("login") if isinstance(u, dict) and u.get("__typename") == "User" and uid else None
            login = login if isinstance(login, str) and OWNER_RE.fullmatch(login) else None
            at = clean_ts(item.get(date))
            deleted = item.get("deletedAt")
            if at is None or (dest == "edits" and ("deletedAt" not in item or (deleted is not None and not clean_ts(deleted)))):
                out["complete"] = False
            out[dest].append({"login": login, "id": uid if login else None, "at": at, **({"deleted": deleted} if dest == "edits" else {})})
    return out


def _gql_uid(u) -> int | None:
    v = u.get("databaseId") if isinstance(u, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 < v < 10 ** 12 else None


def parse_editors(data) -> dict[int, dict] | None:
    """{comment id: {author: id|None, editor: id|None, edited: bool}} from the editor query's answer, None when the issue is missing. A comment never edited has edited False;
    one edited by a deleted account or an app has editor None (and so counts as edited by someone else: fail closed)."""
    d = data.get("data") if isinstance(data, dict) else None
    repo = d.get("repository") if isinstance(d, dict) else None
    issue = repo.get("issue") if isinstance(repo, dict) else None
    if not isinstance(issue, dict):
        return None
    comments = issue.get("comments") if isinstance(issue.get("comments"), dict) else {}
    nodes = comments.get("nodes") if isinstance(comments.get("nodes"), list) else []
    out: dict[int, dict] = {}
    for c in nodes[:EDITORS_MAX]:
        if not isinstance(c, dict):
            continue
        cid = c.get("databaseId")
        if isinstance(cid, bool) or not isinstance(cid, int) or cid <= 0:
            continue
        out[cid] = {"author": _gql_uid(c.get("author")), "editor": _gql_uid(c.get("editor")), "edited": c.get("lastEditedAt") is not None or c.get("editor") is not None}
    return out


def parse_editor(data, cid) -> dict | None:
    """{author, editor, edited} of comment `cid` from the single-comment query's answer, None when the node is missing or is another comment (same shape as parse_editors)."""
    d = data.get("data") if isinstance(data, dict) else None
    c = d.get("node") if isinstance(d, dict) else None
    if not isinstance(c, dict) or isinstance(cid, bool) or c.get("databaseId") != cid:
        return None
    ed = c.get("editor")
    login = ed.get("login") if isinstance(ed, dict) and isinstance(ed.get("login"), str) and OWNER_RE.fullmatch(ed["login"]) else None
    return {"author": _gql_uid(c.get("author")), "editor": _gql_uid(ed), "edited": c.get("lastEditedAt") is not None or ed is not None, "editor_login": login}


def graphql_reason(data) -> str | None:
    """The fixed reason (a key of MESSAGES) for the errors of a GraphQL answer, None when it has none. GitHub says 200 and puts the error in the body."""
    errs = data.get("errors") if isinstance(data, dict) else None
    if not isinstance(errs, list) or not errs:
        return None
    for e in errs[:5]:
        kind = e.get("type") if isinstance(e, dict) else None
        msg = (e.get("message") if isinstance(e, dict) else "") or ""
        low = msg.lower() if isinstance(msg, str) else ""
        if "saml" in low:
            return "sso"
        if kind == "RATE_LIMITED" or "rate limit" in low:
            return "rate_limited"
        if kind == "NOT_FOUND":
            return "not_found"
        if kind == "FORBIDDEN":
            return "no_access"
    return "bad_answer"


def graphql(owner, repo) -> tuple[int, dict, Any]:
    """The one team query for a repository: (status, headers, json) like `api`. A failure of gh itself is a GhReadError; GitHub's own errors are in the body (see graphql_reason)."""
    exe = shutil.which("gh")
    if not exe:
        raise GhReadError("no_gh")
    rc, out, _ = _exec(graphql_argv(owner, repo, exe))
    status, headers, body = _parse_output(out)
    if status is None:
        raise GhReadError("unavailable" if rc != 0 else "bad_answer")
    data = None
    if body.strip():
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            if 200 <= status < 300:
                raise GhReadError("bad_answer") from None
    return status, headers, data


def sso_signal(headers: dict, data) -> bool:
    """Does a 403 mean "authorise this token for the organization"? GitHub sends `X-GitHub-SSO: required; url=...` (the url is never kept) or says SAML in the message."""
    if "x-github-sso" in headers:
        return True
    msg = data.get("message") if isinstance(data, dict) else None
    return isinstance(msg, str) and "saml" in msg.lower()


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

    def defer_to(self, until: float) -> None:
        """Stay away until `until` (this reader's monotonic clock) too: another reader on the same login was told to wait."""
        with self.lock:
            self.until = max(self.until, until)

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

    def cached_fresh(self, path: str, ttl: float) -> bool:
        """True when `get(path, ttl)` would be answered from a fresh cache entry or (during a back-off) a stale one: it would make no call."""
        with self.lock:
            ent = self.cache.get(path)
            if ent is not None and self.mono() - ent.at < ttl:
                return True
            return ent is not None and self.wait_left() > 0

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
                data = slim_files(path, data)                                                                      # a files page is kept (and handed back) as names only, never as patch text
                data = slim_unassigned(path, data)                                                                 # an unassigned-issues page is kept as number, title, labels and date, never as bodies
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
                raise GhReadError("sso" if sso_signal(headers, data) else "no_access")
            if status >= 500:
                raise GhReadError("unavailable")
            raise GhReadError("bad_answer")


reader = Reader()
