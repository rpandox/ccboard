"""The writer next to the reader (nodes epic, issue #148): the few GitHub writes the claim of an epic issue needs, and nothing else.

WRITES ARE OFF BY DEFAULT. `settings.epic_writes` (CCBOARD_EPIC_WRITES, exactly "1") turns them on. With it off, `argv()` refuses before an argv exists, `send()` and the queue refuse
before anything else, and no gh command with a method other than GET is ever built or run (the reader in app/github.py stays GET only).

The allow list is the ONLY thing this module can send. Each shape is built from a validated owner, repository, issue number, comment id, the one label and a login:

    POST   /repos/{o}/{r}/issues/{n}/comments        create the claim comment (or the fixed "a person is needed" comment)
    PATCH  /repos/{o}/{r}/issues/comments/{id}       edit the board's OWN claim comment
    DELETE /repos/{o}/{r}/issues/comments/{id}       delete the board's OWN claim comment
    POST   /repos/{o}/{r}/issues/{n}/assignees       add the board's own login
    DELETE /repos/{o}/{r}/issues/{n}/assignees       remove the board's own login
    POST   /repos/{o}/{r}/issues/{n}/labels          add `ccboard:in-progress`
    DELETE /repos/{o}/{r}/issues/{n}/labels/{name}   remove `ccboard:in-progress`
    POST   /repos/{o}/{r}/labels                     create `ccboard:in-progress`

No issue edit, no close, no pull request, no repository setting, no other label, no comment that is not the board's own. Whose comment it is, is the caller's business (app/nodes_epics.py
records the ids it created and re-reads before every edit); this module only guarantees the SHAPE. A comment body is never free text: it is made by `claim_body()` from validated fields and a
fixed sentence, or it is HELP_BODY, and `argv()` refuses any other body. The body goes to gh on stdin as JSON (`--input -`), the command is an argv list (never a shell) with a bounded timeout
and a capped answer, and a gh failure is reduced to a fixed reason (no stderr text is ever passed on).

The QUEUE: one write at a time per board, at least 1.1 s apart, at most 30 a minute, a back-off on 403 and 429 (Retry-After, else the secondary limit's 60 s), and never a retry here: a write
whose outcome is unknown (a timeout, a 5xx, no answer) is raised as `unknown=True` and the caller reads back what exists before it does anything again.
"""
from __future__ import annotations

import collections
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, NamedTuple

from . import github
from .config import settings

LABEL = "ccboard:in-progress"
LABEL_COLOR = "fbca04"
LABEL_DESCRIPTION = "A ccboard node is working this issue"
MARKER = "<!-- ccboard:claim v1 -->"
CLAIM_TEXT = ("Claimed by a ccboard node. The line above is how the nodes tell who holds this issue. Please do not edit or delete this comment while the issue is being worked.")
HELP_BODY = ("ccboard: three nodes have taken this issue and each attempt ended without a result, so the nodes have stopped trying. A person needs to look at it. "
             "To let the nodes try again, delete the claim comments above.")
STATUSES = ("claimed", "running", "done", "failed", "released", "cancelled")
MIN_GAP = 1.1                       # seconds between two writes of one board
CEILING = 30                        # writes a minute
CEILING_WINDOW = 60.0
LOCK_TIMEOUT = 45.0                 # the longest a write waits for its turn in the queue; then it is refused as busy and nothing is sent
MAX_WAIT = 120.0                    # the longest the queue sleeps for one write; a longer back-off is raised as rate_limited instead
TIMEOUT = 20.0
OUT_CAP = 256 * 1024
MESSAGES = {
    "writes_off": "this board does not write to GitHub (CCBOARD_EPIC_WRITES is off)",
    "no_gh": "gh is not installed on this board",
    "no_login": "gh is not logged in on this board: run gh auth login there",
    "no_access": "this gh login may not do that on that repository",
    "not_found": "GitHub has no such issue or comment, or this login cannot see it",
    "invalid": "GitHub refused the request as invalid",
    "rate_limited": "GitHub asked us to wait",
    "busy": "too many GitHub writes are waiting on this board, so this one was not sent",
    "timeout": "gh did not answer in time, so the outcome of the write is not known",
    "unavailable": "GitHub did not answer (an error on its side), so the outcome of the write is not known",
    "bad_answer": "GitHub sent an answer this board could not read",
    "bad_request": "that is not a write this board will make",
}

# a claim comment's fields, as the writer accepts them (ASCII only: a name with a dot looks like a host name and is refused)
ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")
NID_RE = re.compile(r"[0-9a-f]{8}")
TASKREF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:#-]{0,39}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,39}){0,4}")
CID_RE = re.compile(r"[1-9][0-9]{0,15}")
_NUM = re.compile(r"[0-9]{1,9}")
_BODY = re.compile(r"<!-- ccboard:claim v1 -->\nccboard claim: alias=(?P<alias>\S+) nid=(?P<nid>\S+) lease=(?P<lease>[0-9]{1,3})m seq=(?P<seq>[0-9]{1,9}) status=(?P<status>[a-z]+) "
                   r"attempt=(?P<attempt>[0-9]{1,2})(?: reclaims=(?P<reclaims>[0-9]{1,16}))?(?: task=(?P<task>\S+))?(?: branch=(?P<branch>\S+))?\n\n" + re.escape(CLAIM_TEXT))
_PATHS = (
    ("POST", re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/issues/[1-9][0-9]{0,8}/comments")),
    ("PATCH", re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/issues/comments/[1-9][0-9]{0,15}")),
    ("DELETE", re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/issues/comments/[1-9][0-9]{0,15}")),
    ("POST", re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/issues/[1-9][0-9]{0,8}/assignees")),
    ("DELETE", re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/issues/[1-9][0-9]{0,8}/assignees")),
    ("POST", re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/issues/[1-9][0-9]{0,8}/labels")),
    ("DELETE", re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/issues/[1-9][0-9]{0,8}/labels/ccboard%3Ain-progress")),
    ("POST", re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/labels")),
)
SECRETISH = re.compile(r"gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|(?i:bearer\s+\S{8,})|[A-Za-z0-9_-]{41,}")


class GhWriteError(Exception):
    """A write that did not happen, or may not have. `reason` is a key of MESSAGES; `unknown` is True when GitHub may have applied it (a timeout, a 5xx, no answer): the caller
    must read back what exists before it writes again. `status` is the HTTP code when there was one; `wait_s` is set for rate_limited."""

    def __init__(self, reason: str, wait_s: int | None = None, unknown: bool = False, status: int | None = None):
        self.reason = reason if reason in MESSAGES else "bad_answer"
        self.wait_s, self.unknown, self.status = wait_s, bool(unknown), status
        super().__init__(self.message)

    @property
    def message(self) -> str:
        return f"{MESSAGES['rate_limited']} {self.wait_s} s" if self.reason == "rate_limited" and self.wait_s else MESSAGES[self.reason]

    def as_dict(self) -> dict:
        return {"reason": self.reason, "message": self.message, "wait_s": self.wait_s, "unknown": self.unknown}


class Request(NamedTuple):
    method: str
    path: str
    body: dict | None


def writes_enabled() -> bool:
    return settings.epic_writes is True


def _off() -> None:
    if not writes_enabled():
        raise GhWriteError("writes_off")


# ---------------------------------------------------------------- the comment body: a fixed template of validated fields, never free text

def claim_body(alias: str, nid: str, lease_min: int, seq: int, status: str, attempt: int, *, reclaims: int | None = None, task: str | None = None, branch: str | None = None) -> str:
    """The claim comment. Every field is checked against its own pattern (ValueError otherwise): no free text from a peer, an agent, an issue or a caller can enter it. Nothing here
    names a path, a host, an address or a token."""
    def num(v, lo, hi):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValueError("bad number")
        return v
    if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias) or not isinstance(nid, str) or not NID_RE.fullmatch(nid) or status not in STATUSES:
        raise ValueError("bad claim field")
    if task is not None and not (isinstance(task, str) and TASKREF_RE.fullmatch(task)) or branch is not None and not (isinstance(branch, str) and BRANCH_RE.fullmatch(branch) and ".." not in branch):
        raise ValueError("bad claim field")
    line = f"ccboard claim: alias={alias} nid={nid} lease={num(lease_min, 10, 120)}m seq={num(seq, 0, 999_999_999)} status={status} attempt={num(attempt, 1, 99)}"
    if reclaims is not None:
        if not CID_RE.fullmatch(str(num(reclaims, 1, 10 ** 15))):
            raise ValueError("bad claim field")
        line += f" reclaims={reclaims}"
    if task is not None:
        line += f" task={task}"
    if branch is not None:
        line += f" branch={branch}"
    if SECRETISH.search(line):
        raise ValueError("bad claim field")                         # fits the patterns but looks like a token
    return f"{MARKER}\n{line}\n\n{CLAIM_TEXT}"


def valid_body(body) -> bool:
    """True only for HELP_BODY or a body `claim_body` could have made (its pattern, its fields checked again, nothing secret-shaped)."""
    if body == HELP_BODY:
        return True
    if not isinstance(body, str) or len(body) > 1200:
        return False
    m = _BODY.fullmatch(body)
    if not m or SECRETISH.search(body.replace(CLAIM_TEXT, "")):
        return False
    try:
        claim_body(m["alias"], m["nid"], int(m["lease"]), int(m["seq"]), m["status"], int(m["attempt"]), reclaims=int(m["reclaims"]) if m["reclaims"] else None, task=m["task"], branch=m["branch"])
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------- the request builders: every one validates what goes into a path

def _slug(owner, repo) -> str:
    if not github.valid_slug(owner, repo):
        raise GhWriteError("bad_request")
    return f"/repos/{owner}/{repo}"


def _num(n) -> int:
    if not github.valid_number(n):
        raise GhWriteError("bad_request")
    return int(n)


def _cid(cid) -> int:
    if isinstance(cid, bool) or not isinstance(cid, int) or not CID_RE.fullmatch(str(cid)):
        raise GhWriteError("bad_request")
    return cid


def _body(body) -> dict:
    if not valid_body(body):
        raise GhWriteError("bad_request")
    return {"body": body}


def _login(login) -> dict:
    if not isinstance(login, str) or not github.OWNER_RE.fullmatch(login):
        raise GhWriteError("bad_request")
    return {"assignees": [login]}


def comment_create(owner, repo, n, body) -> Request:
    return Request("POST", f"{_slug(owner, repo)}/issues/{_num(n)}/comments", _body(body))


def comment_edit(owner, repo, cid, body) -> Request:
    return Request("PATCH", f"{_slug(owner, repo)}/issues/comments/{_cid(cid)}", _body(body))


def comment_delete(owner, repo, cid) -> Request:
    return Request("DELETE", f"{_slug(owner, repo)}/issues/comments/{_cid(cid)}", None)


def assignee_add(owner, repo, n, login) -> Request:
    return Request("POST", f"{_slug(owner, repo)}/issues/{_num(n)}/assignees", _login(login))


def assignee_remove(owner, repo, n, login) -> Request:
    return Request("DELETE", f"{_slug(owner, repo)}/issues/{_num(n)}/assignees", _login(login))


def label_add(owner, repo, n) -> Request:
    return Request("POST", f"{_slug(owner, repo)}/issues/{_num(n)}/labels", {"labels": [LABEL]})


def label_remove(owner, repo, n) -> Request:
    return Request("DELETE", f"{_slug(owner, repo)}/issues/{_num(n)}/labels/ccboard%3Ain-progress", None)


def label_create(owner, repo) -> Request:
    return Request("POST", f"{_slug(owner, repo)}/labels", {"name": LABEL, "color": LABEL_COLOR, "description": LABEL_DESCRIPTION})


def allowed_write(method, path) -> bool:
    """True for the method and path shapes of the list at the top of this module and nothing else (owner and repository checked again against GitHub's rules)."""
    if not isinstance(method, str) or not isinstance(path, str) or len(path) > 300:
        return False
    for m, rx in _PATHS:
        hit = rx.fullmatch(path) if m == method else None
        if hit and github.valid_slug(hit.group(1), hit.group(2)):
            return True
    return False


def _checked_body(req: Request) -> bytes | None:
    """The JSON the request carries, after checking that its KIND is the one its path takes (a comment path takes {body}, an assignees path {assignees: [login]}, the issue labels
    path {labels: [the one label]}, the repository labels path the one label's definition, and the delete paths of comments and labels take none) and every value in it."""
    b, p = req.body, req.path
    if p.endswith("/comments") or "/issues/comments/" in p and req.method == "PATCH":
        ok = isinstance(b, dict) and set(b) == {"body"} and valid_body(b["body"])
    elif p.endswith("/assignees"):
        ok = isinstance(b, dict) and set(b) == {"assignees"} and isinstance(b["assignees"], list) and len(b["assignees"]) == 1 and isinstance(b["assignees"][0], str) \
            and bool(github.OWNER_RE.fullmatch(b["assignees"][0]))
    elif p.endswith("/labels") and "/issues/" in p:
        ok = b == {"labels": [LABEL]}
    elif p.endswith("/labels"):
        ok = b == {"name": LABEL, "color": LABEL_COLOR, "description": LABEL_DESCRIPTION}
    else:
        ok = b is None                                              # DELETE of a comment or of the label
    if not ok:
        raise GhWriteError("bad_request")
    return None if b is None else json.dumps(b, separators=(",", ":")).encode("utf-8")


def argv(req: Request, exe: str = "gh") -> list[str]:
    """The one command line the writer builds: `gh api --include --method <M> -H ... --input - <path>` (the body goes on stdin). Refuses with writes_off BEFORE anything is
    built when CCBOARD_EPIC_WRITES is off, and with bad_request for any method and path that is not on the allow list or any body that is not made from validated fields."""
    _off()
    if not isinstance(req, Request) or not allowed_write(req.method, req.path):
        raise GhWriteError("bad_request")
    _checked_body(req)
    out = [exe, "api", "--include", "--method", req.method, "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28"]
    if req.body is not None:
        out += ["--input", "-"]
    return out + [req.path]


# ---------------------------------------------------------------- the transport: the one seam that runs gh (tests replace it; tests/conftest.py makes it refuse)

def transport(req: Request) -> tuple[int, dict, Any]:
    """Run one write: (status, headers, json). Any HTTP status is returned; a failure of gh itself is a GhWriteError (a timeout is unknown=True). Reached only through `Queue.run`."""
    _off()
    exe = shutil.which("gh")
    if not exe:
        raise GhWriteError("no_gh")
    args = argv(req, exe)
    data_in = _checked_body(req)
    try:
        cp = subprocess.run(args, input=data_in if data_in is not None else b"", capture_output=True, timeout=TIMEOUT,
                            env={**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "GH_NO_UPDATE_NOTIFIER": "1", "GH_SPINNER_DISABLED": "1"})
    except subprocess.TimeoutExpired:
        raise GhWriteError("timeout", unknown=True) from None
    except OSError:
        raise GhWriteError("no_gh") from None
    if len(cp.stdout) > OUT_CAP:
        raise GhWriteError("bad_answer", unknown=True)
    status, headers, body = github._parse_output(cp.stdout)
    if status is None:
        err = (cp.stderr or b"")[:4096].decode("utf-8", "replace").lower()
        if cp.returncode == 4 or "gh auth login" in err:
            raise GhWriteError("no_login")
        raise GhWriteError("unavailable", unknown=True)
    data = None
    if body.strip():
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            data = None
    return status, headers, data


# ---------------------------------------------------------------- the queue

class Queue:
    """One write at a time, MIN_GAP seconds apart, CEILING a minute, a back-off after 403/429. `mono` and `sleep` are injectable (tests pass a fake clock). Each board has one; the
    module's `queue` is this board's. Nothing here retries: a refusal or an unknown outcome is raised once and the caller decides."""

    def __init__(self, mono=None, sleep=None, gap: float = MIN_GAP, ceiling: int = CEILING, lock_timeout: float = LOCK_TIMEOUT):
        self.mono = mono or time.monotonic
        self.sleep = sleep or time.sleep
        self.gap, self.ceiling, self.lock_timeout = gap, ceiling, lock_timeout
        self.lock = threading.Lock()
        self.last: float | None = None
        self.until = 0.0
        self.stamps: "collections.deque[float]" = collections.deque(maxlen=ceiling)
        self.count = 0
        self.slept = 0.0

    def wait_left(self) -> int:
        return max(0, math.ceil(self.until - self.mono()))

    def _pace(self) -> None:
        now = self.mono()
        need = self.until - now
        if self.last is not None:
            need = max(need, self.last + self.gap - now)
        if len(self.stamps) >= self.ceiling:
            need = max(need, self.stamps[0] + CEILING_WINDOW - now)
        if need > MAX_WAIT:
            raise GhWriteError("rate_limited", wait_s=math.ceil(need))
        if need > 0:
            self.slept += need
            self.sleep(need)

    def run(self, req: Request, tport=None) -> tuple[int, dict, Any]:
        """Send one request through the allow list, the pacing and the transport: (status, headers, json) for a 2xx. A 4xx/5xx is a GhWriteError (5xx and unknown outcomes have
        unknown=True; 403/429 set the back-off and are rate_limited when GitHub asked us to wait)."""
        _off()
        argv(req)                                                       # the allow list and the body rule, before the lock and before any wait
        if not self.lock.acquire(timeout=self.lock_timeout):
            raise GhWriteError("busy")                              # another write holds the turn (or is waiting out a back-off): refused, nothing sent, never retried here
        try:
            _off()
            self._pace()
            now = self.mono()
            self.last = now
            self.stamps.append(now)
            self.count += 1
            status, headers, data = (tport or transport)(req)
            wait = github.backoff_wait(status, headers, github.server_time(headers), data.get("message") if isinstance(data, dict) else None)
            if wait:
                self.until = max(self.until, self.mono() + wait)
            if 200 <= status < 300:
                return status, headers, data
            if wait:
                raise GhWriteError("rate_limited", wait_s=wait, status=status)
            if status == 401:
                raise GhWriteError("no_login", status=status)
            if status == 403:
                raise GhWriteError("no_access", status=status)
            if status in (404, 410):
                raise GhWriteError("not_found", status=status)
            if status in (409, 422):
                raise GhWriteError("invalid", status=status)
            if status >= 500:
                raise GhWriteError("unavailable", unknown=True, status=status)
            raise GhWriteError("bad_answer", unknown=True, status=status)
        finally:
            self.lock.release()


queue = Queue()


def send(req: Request, q: "Queue | None" = None) -> tuple[int, dict, Any]:
    """Send `req` through the board's queue (or `q`). Raises GhWriteError("writes_off") before anything else when writes are off."""
    _off()
    return (q or queue).run(req)
