"""The epic view, server side (nodes epic, issue #147): list the epics of the repos this board knows, show one epic's issues with the node that holds each,
the live phase of its task and its pull request, and keep the owner's pins and pause. Read only towards GitHub; nothing here claims, runs or comments.

What an epic is (the decision of issue #146): a parent issue labelled `ccboard:epic` whose sub-issues are the children (REST .../issues/{n}/sub_issues, in the
queue order GitHub keeps). When that endpoint is not there (404) the children are the issues labelled `epic:<n>` in the same repository, in issue-number order,
and the answer says so (`source: labels`). Other labels the board reads: `ccboard:in-progress` (a child some node holds), `ccboard:node:<alias>` (a pin) and
`ccboard:ready`. A child's body may hold `depends on #N` or `depends on owner/name#N`; the child waits until N is closed.

GitHub is read through app/github.py (`gh api` GET only, ETag, a 60 s cache, a back-off kept in memory) and only when a person (or the hook token) asks:
nothing here starts a thread or a timer, so a board with no epic label anywhere and no paired node does nothing in the background.

EVERYTHING GitHub returns is untrusted text written by other people. Lists are capped (100 children, 100 comments from each end of an issue, 25 repositories),
only whitelisted fields are kept, strings go through github.clean_text, links are built from a validated owner, repository and number (never copied from the
payload) and the page renders all of it with textContent. A cap never turns into a permissive answer: an epic with more children than were read says so
(`truncated`), and a child whose comments were not all read is `claim_state: unknown`, never free.

Which repositories this board may ask GitHub about: exactly the GitHub remotes of its OWN projects (`known_repos`, from local `git remote` only), and an epic
addressed in a route only when its repository is in that set. Nothing another node sends (a remote task's issue_ref, a slug in a node's state) and nothing GitHub
sends (a repository_url, a `depends on owner/name#N` line, a sub-issue living elsewhere) ever selects a request: a child in a repository outside the set is shown
from the data the sub-issues answer already held, with its claim "in another repository". A node's task is only JOINED to a child already read, by the exact issue
number and a repository slug that is in the set.

A claim comment (`<!-- ccboard:claim v1 -->`, written by the claim phase, issue #148) counts only when ALL of these hold: its author is a user (a user object with a
login and type User, not a bot), the login is in CCBOARD_EPIC_AUTHORS (empty: the login gh is authenticated as on this board; when neither is known nobody counts),
and GitHub's own `author_association` on the comment is OWNER, MEMBER or COLLABORATOR (someone who can write to the repository). Being the repository owner earns no
trust by itself. A claim-shaped comment from anyone else is counted as `claim_rejected` and never shown as a claim. A claim is live when its status is claimed or
running and GitHub's own clock (the Date header of the comments answer) is less than the claim's lease after the comment's updated_at; the local clock decides nothing.

What a child's claim knowledge is (`claim_state`): `claimed` (an allowed claim was found), `none` (ALL the comments of the issue were read in this refresh and none is
an allowed claim: the only state that means free) or `unknown` (anything else: not read, cut short, an error, a back-off, another repository, no way to tell who may
claim). A thread of up to 200 comments is read whole (its first and its last 100); a longer one is read from both ends and is `unknown` unless a LIVE allowed claim
was found. The last page is computed from the comments count GitHub sends with the issue and the page size; no Link URL is ever followed.
"""
from __future__ import annotations

import hashlib
import logging
import re
import socket
import threading
import time
import unicodedata
from functools import wraps
from pathlib import Path
from typing import Any, NamedTuple

from . import github, github_write, issues, node_state, nodes, projects
from .config import settings

log = logging.getLogger("ccboard.nodes.epics")


def _claim_step(step):
    def decorate(fn):
        @wraps(fn)
        def timed(*args, **kwargs):
            started = time.monotonic()
            try:
                return fn(*args, **kwargs)
            finally:
                log.info("claim step %s took %.3f s", step, time.monotonic() - started)
        return timed
    return decorate


EPIC_LABEL = "ccboard:epic"
IN_PROGRESS_LABEL = "ccboard:in-progress"
READY_LABEL = "ccboard:ready"
NODE_LABEL = "ccboard:node:"
FALLBACK_LABEL = "epic:"                 # + the parent's number, on each child, when the sub-issues endpoint is not there
CHILD_CAP = 100
COMMENT_CAP = 100
REPO_CAP = 25                            # repositories the Epics list reads at most (more is said: `repos_truncated`)
REPO_SET_MAX = 500                       # repositories of this board's own projects the route check knows
EPIC_CAP = 50                            # epics listed at most
LABEL_CAP = 30
DEPENDS_CAP = 20
PINS_CAP = 200
GOOD_MAX = 32                            # last good answers kept in memory
READ_CHILD_MAX_AGE = 900.0               # seconds a reading of an epic keeps its children claimable; an older one is read again (the page names the epic in its claim)
LIST_TTL = 60.0
ISSUE_TTL = 60.0
COMMENTS_TTL = 0.0                       # an open child's comments are revalidated on every refresh (a 304 costs no request budget): `none` is a statement about a moment
COMMENTS_TTL_CLOSED = 600.0              # a closed child's: its claim history is settled; a page kept this way is never the basis of `none`, and a child that was reopened is open again
COMMENT_CALLS = 40                       # gh calls for comments one refresh may make; the rest wait for the next one (answer.partial)
DEADLINE = 30.0                          # seconds one refresh may take
STALE_NOTE = "not from an allowed author"
AUTHORS_NOTE = "cannot tell who may claim: gh login unknown"
ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")           # GitHub's own word for the people who can write to the repository
UNKNOWN_TEXT = {                                               # claim_state unknown: the reason code and the sentence the page shows
    "too_long": "too many comments to read",
    "rate_limited": "GitHub asked us to wait",
    "budget": "not read yet in this refresh",
    "error": "the comments could not be read",
    "other_repo": "in another repository",
    "authors_unknown": AUTHORS_NOTE,
    "not_recent": "not read recently",
    "changed": "changed while it was read",
}
KV_PREFIX = "epic:"
STATUSES = ("claimed", "running", "done", "failed", "released", "cancelled")
LEASE_MIN, LEASE_MAX, LEASE_DEFAULT = 10, 120, 30
PHASES = ("backlog", "queued", "running", "done", "failed", "cancelled")

_MARKER = re.compile(r"\A\s*<!--\s*ccboard:claim\s+v([0-9]{1,3})\s*-->")
_FIELDS = re.compile(r"^[ \t]*ccboard claim:(.*)$", re.M)
_ALIAS = re.compile(r"\w[\w.-]{0,39}")
_NID = re.compile(r"[0-9a-f]{8}")
_LEASE = re.compile(r"([0-9]{1,3})m")
_TASKREF = re.compile(r"[A-Za-z0-9._:#/-]{1,40}")
_BRANCH = re.compile(r"(?![-.])[A-Za-z0-9._/-]{1,100}")
_DIGITS = re.compile(r"[0-9]{1,9}")
_REPO_URL = re.compile(r"https://api\.github\.com/repos/([^/]{1,100})/([^/]{1,100})")
_ISSUE_URL = re.compile(r"https://github\.com/([^/]{1,100})/([^/]{1,100})/issues/([0-9]{1,9})")
_DEPENDS = re.compile(r"^[ \t>*+-]*depends[ \t]+on[ \t]*:?[ \t]*(.{1,400})$", re.I | re.M)
_REF = re.compile(r"(?<![\w/#])(?:([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100}))?#([1-9][0-9]{0,8})\b")
_NODE_ALIAS = re.compile(r"\w[\w.-]{0,39}")

_lock = threading.Lock()
_last_good: dict[tuple, tuple[float, dict]] = {}       # (owner, repo, n) -> (monotonic time, the GitHub part of the answer): what a failed read falls back to
_list_good: dict[tuple, tuple[float, dict]] = {}
mono = time.monotonic                                   # patched by tests


def reset() -> None:
    """Forget every cache and the back-off (tests)."""
    stop_renewals(5.0)                                      # a renew thread of an earlier test must not outlive it
    with _lock:
        _last_good.clear()
        _list_good.clear()
    with _klock:
        _locks.clear()
    github.reader.reset_state()


# ---------------------------------------------------------------- links: built from validated parts, never copied from GitHub

def issue_url(owner: str, repo: str, n: int) -> str:
    return f"https://github.com/{owner}/{repo}/issues/{int(n)}"


def pull_url(owner: str, repo: str, n: int) -> str:
    return f"https://github.com/{owner}/{repo}/pull/{int(n)}"


# ---------------------------------------------------------------- claims

def _i(v, lo: int, hi: int, default: int) -> int:
    return int(v) if isinstance(v, str) and v.isascii() and v.isdigit() and len(v) < 12 and lo <= int(v) <= hi else default


def parse_claim(body) -> dict | None:
    """The claim a comment body holds, or None. The first line must be the marker `<!-- ccboard:claim v1 -->` (another version is ignored), followed by the line
    `ccboard claim: alias=<alias> nid=<8 hex> lease=30m seq=<n> status=<...> attempt=<n>` and optionally `task=<ref> branch=<name>`. Unknown keys are ignored,
    a value that does not fit its rule drops the whole claim, nothing is evaluated."""
    if not isinstance(body, str):
        return None
    head = body[:2000]
    m = _MARKER.match(head)
    if not m or m.group(1) != "1":
        return None
    line = _FIELDS.search(head, m.end())
    if not line:
        return None
    f: dict[str, str] = {}
    for tok in line.group(1).split()[:24]:
        k, eq, v = tok.partition("=")
        if eq and k in ("alias", "nid", "lease", "seq", "status", "attempt", "task", "branch", "reclaims") and k not in f and len(v) <= 120:
            f[k] = v
    alias = f.get("alias", "")
    if not _ALIAS.fullmatch(alias) or github.clean_text(alias, 40) != alias or f.get("status") not in STATUSES:
        return None
    lease = _LEASE.fullmatch(f.get("lease", ""))
    return {"alias": alias, "status": f["status"], "nid": f["nid"] if _NID.fullmatch(f.get("nid", "")) else None,
            "lease_min": max(LEASE_MIN, min(LEASE_MAX, int(lease.group(1)))) if lease else LEASE_DEFAULT,
            "seq": _i(f.get("seq"), 0, 10 ** 9, 0), "attempt": _i(f.get("attempt"), 1, 99, 1),
            "task_ref": f["task"] if _TASKREF.fullmatch(f.get("task", "")) else None,
            "branch": f["branch"] if _BRANCH.fullmatch(f.get("branch", "")) else None,
            "reclaims": _i(f.get("reclaims"), 1, 10 ** 11, 0) or None}


def parse_claims(comments) -> list[dict]:
    """The claims in a list of GitHub comment objects, oldest first by (created_at, id): {id, author, created_at, updated_at, alias, status, ...}. Comments
    without the marker, with another version or without a usable id and timestamps are ignored; `author` is the login (validated) of a user object of type
    User, else None (no user, a bot, an organisation, a login that is not one); `association` is GitHub's author_association word or None. No author check
    happens here: see `pick_claim`."""
    out = []
    for c in comments[:2 * COMMENT_CAP] if isinstance(comments, list) else []:
        if not isinstance(c, dict):
            continue
        cid, created, updated = c.get("id"), github.clean_ts(c.get("created_at")), github.clean_ts(c.get("updated_at"))
        if isinstance(cid, bool) or not isinstance(cid, int) or cid <= 0 or not created or not updated:
            continue
        claim = parse_claim(c.get("body"))
        if claim is None:
            continue
        user = c.get("user")
        login = user.get("login") if isinstance(user, dict) and user.get("type") == "User" else None
        assoc = c.get("author_association")
        claim.update(id=cid, created_at=created, updated_at=updated, author=login if isinstance(login, str) and github.OWNER_RE.fullmatch(login) else None,
                     association=assoc if isinstance(assoc, str) and assoc.isupper() and len(assoc) <= 30 else None)
        out.append(claim)
    out.sort(key=lambda c: (c["created_at"], c["id"]))
    return out


def is_live(claim: dict, server_now: float | None) -> bool | None:
    """True for a claimed or running claim whose last update is less than its lease before GitHub's clock; None when GitHub's clock is not known."""
    if claim["status"] not in ("claimed", "running"):
        return False
    upd = github.ts_epoch(claim["updated_at"])
    if server_now is None or upd is None:
        return None
    return server_now - upd < claim["lease_min"] * 60


def allowed_authors(gh_login: str | None, configured=None) -> set[str]:
    """The logins whose claim comments may count (lower case): CCBOARD_EPIC_AUTHORS, or, when that is empty, the login gh is authenticated as on this board. The
    repository owner is not added: owning a repository one has a clone of says nothing about who this board trusts. EMPTY when neither is known: nobody counts and the
    page says it cannot tell (fail closed). Every name is checked against GitHub's login rule."""
    configured = settings.epic_authors if configured is None else configured
    names = list(configured) if configured else [gh_login]
    return {n.lower() for n in names if isinstance(n, str) and github.OWNER_RE.fullmatch(n)}


def allowed_claims(claims: list[dict], allowed: set[str]) -> list[dict]:
    """The claims that count, in the order they came: author a user whose login is in `allowed` AND GitHub's author_association OWNER, MEMBER or COLLABORATOR. The one rule
    the reader (`pick_claim`) and the claim protocol (ordering, attempts, the fence) share, so a claim one ignores the other ignores."""
    return [c for c in claims if (c.get("author") or "").lower() in allowed and c.get("author") and c.get("association") in ASSOCIATIONS]


def pick_claim(claims: list[dict], allowed: set[str], server_now: float | None) -> tuple[dict | None, int]:
    """(the claim to show, how many claim comments were refused). The first live claim by (created_at, id) among the allowed authors', else the newest one of
    theirs (not live). A comment counts only when its author is a user whose login is in `allowed` AND GitHub's author_association on it is OWNER, MEMBER or
    COLLABORATOR; a claim-shaped comment of anyone else is only counted."""
    ok = allowed_claims(claims, allowed)
    rejected = len(claims) - len(ok)
    if not ok:
        return None, rejected
    for c in ok:
        c["live"] = is_live(c, server_now)
    live = [c for c in ok if c["live"]]
    return (live[0] if live else ok[-1]), rejected


# ---------------------------------------------------------------- children

def _repo_of(item: dict) -> tuple[str, str] | None:
    """The repository a sub-issue lives in, from the payload's repository_url (for display and the "another repository" mark only: it never selects a request unless
    the repository is one of this board's own). None when the payload names none that passes GitHub's rules."""
    u = item.get("repository_url")
    m = _REPO_URL.fullmatch(u) if isinstance(u, str) and len(u) < 300 else None
    return (m.group(1), m.group(2)) if m and github.valid_slug(m.group(1), m.group(2)) else None


def author_of(item) -> str | None:
    """The login that wrote an issue (a GitHub issue payload), when it is a user (type User, not a bot or an organisation) with a login that passes GitHub's rule; else None. None is
    never an allowed author."""
    u = item.get("user") if isinstance(item, dict) else None
    login = u.get("login") if isinstance(u, dict) and u.get("type") == "User" else None
    return login if isinstance(login, str) and github.OWNER_RE.fullmatch(login) else None


def author_ok(login, allowed: set[str]) -> bool:
    """True when `login` is among the allowed authors (the one rule of `allowed_authors`: the logins of CCBOARD_EPIC_AUTHORS, else this board's gh login)."""
    return isinstance(login, str) and login.lower() in allowed


def parse_depends(body, owner: str, repo: str, own: int | None = None) -> list[str]:
    """The issues a child waits for: `depends on #N` or `depends on owner/name#N` lines of its body, as `owner/name#N` (lower case), at most 20; never the
    issue itself."""
    out: list[str] = []
    for line in _DEPENDS.finditer(body[:8000] if isinstance(body, str) else ""):
        for m in _REF.finditer(line.group(1)):
            o, r, n = m.group(1), m.group(2), m.group(3)
            if o is None:
                o, r = owner, repo
            elif not github.valid_slug(o, r):
                continue
            ref = f"{o}/{r}#{int(n)}".lower()
            if ref not in out and len(out) < DEPENDS_CAP and ref != f"{owner}/{repo}#{own}".lower():
                out.append(ref)
    return out


def issue_agent(body) -> str:
    """The agent an issue asks for in its "Who should do it" block, as app/nodes_work.launch_of picks it when the node has both: claude when it has a claude line, else codex when it has a
    codex line, else claude. The body is untrusted text and is cut before the parser sees it."""
    from . import issues
    try:
        who = issues.parse_who(body[:8000] if isinstance(body, str) else "")
    except Exception:
        return "claude"
    return who.get("default_agent") if who.get("default_agent") in ("claude", "codex") else "claude"


def shape_child(item, owner: str, repo: str, index: int, same_repo: bool = False) -> dict | None | bool:
    """One issue of a list as a child, whitelisted: None for a pull request or anything without a valid number. Nothing of the payload's URLs is kept except the
    repository it names (checked against GitHub's rules). `same_repo` is for a list the board asked of ONE repository (the label list): the child is of that
    repository whatever the payload says. A sub-issue whose payload names no usable repository is False: it cannot be placed, and the caller counts it as not shown."""
    if not isinstance(item, dict) or "pull_request" in item:
        return None
    num = item.get("number")
    if isinstance(num, bool) or not isinstance(num, int) or not github.valid_number(num):
        return None
    where = (owner, repo) if same_repo else _repo_of(item)
    if where is None:
        return False
    o, r = where
    labels = [github.clean_text(lb.get("name"), 50) for lb in (item.get("labels") or [])[:LABEL_CAP] if isinstance(lb, dict)]
    labels = [lb for lb in labels if lb]
    cnt = item.get("comments")
    return {"repo": f"{o}/{r}", "owner": o, "name": r, "number": num, "title": github.clean_text(item.get("title"), 200) or f"Issue {num}",
            "state": "closed" if item.get("state") == "closed" else "open", "url": issue_url(o, r, num), "labels": labels,
            "depends_on_refs": parse_depends(item.get("body"), o, r, num), "order": index, "agent": issue_agent(item.get("body")),
            "comments": cnt if isinstance(cnt, int) and not isinstance(cnt, bool) and cnt >= 0 else None,
            "updated_at": github.clean_ts(item.get("updated_at")), "author": author_of(item), "claims": [], "server_now": None,
            "thread": None, "reason": None, "fresh": False, "read_at": None}               # thread: None (not read), "complete" (every comment read) or "incomplete"; reason: why the claim is unknown (UNKNOWN_TEXT)


def _display_ref(ref: str, repo_lower: str) -> str:
    slug, _, n = ref.partition("#")
    return f"#{n}" if slug == repo_lower else ref


def _link_children(children: list[dict]) -> None:
    """depends_on (as the page shows it), blocked_by (dependencies that are open children) and dependency_cycle, from `depends_on_refs`."""
    by_ref = {f"{c['repo']}#{c['number']}".lower(): c for c in children}
    for c in children:
        rl = c["repo"].lower()
        c["depends_on"] = [_display_ref(d, rl) for d in c["depends_on_refs"]]
        c["blocked_by"] = [_display_ref(d, rl) for d in c["depends_on_refs"] if d in by_ref and by_ref[d]["state"] == "open"]

    def reaches(start: str, goal: str) -> bool:
        seen, todo = set(), [start]
        while todo:
            cur = todo.pop()
            if cur in seen or cur not in by_ref:
                continue
            seen.add(cur)
            for d in by_ref[cur]["depends_on_refs"]:
                if d == goal:
                    return True
                todo.append(d)
        return False

    for c in children:
        me = f"{c['repo']}#{c['number']}".lower()
        c["dependency_cycle"] = bool(c["depends_on_refs"]) and reaches(me, me)


def _paged(builder, cap: int, ttl: float) -> tuple[list, bool, float | None, bool, bool]:
    """(items, truncated, GitHub's clock, any page stale, every page fresh): pages 1.. of builder(page) until a short page, `cap` items or MAX_PAGES. Never follows a
    link. "Fresh" means GitHub itself confirmed every page in this call (a 200, or a 304 on If-None-Match), not a cache entry or a stale one."""
    items: list = []
    srv: float | None = None
    stale = truncated = False
    fresh = True
    per = github.PER_PAGE
    for page in range(1, github.MAX_PAGES + 1):
        rep = github.reader.get(builder(page), ttl)
        data = rep.data
        if not isinstance(data, list):
            raise github.GhReadError("bad_answer")
        if rep.server_now is not None:
            srv = rep.server_now if srv is None else max(srv, rep.server_now)
        stale = stale or rep.stale
        fresh = fresh and rep.fresh
        items.extend(data)
        more = len(data) >= per or 'rel="next"' in (rep.headers.get("link") or "")
        if len(items) >= cap:
            truncated = len(items) > cap or more
            items = items[:cap]
            break
        if not more or not data:
            break
    else:
        truncated = True
    return items, truncated, srv, stale, fresh


class _Cut(Exception):
    """The refresh's own budget (gh calls for comments, seconds) ran out in the middle of a thread."""


def _snapshot(c: dict, spend) -> dict:
    """The issue itself, REVALIDATED now (never from the cache): {count (its comments count), updated (updated_at), state, fresh, srv}. One gh call (a 304 when nothing
    changed, which costs no request budget). The count and updated_at before and after the pages of a thread are what make the pages one consistent reading."""
    spend()
    rep = github.reader.get(github.issue_path(c["owner"], c["name"], c["number"]), 0.0)
    d = rep.data
    if not isinstance(d, dict):
        raise github.GhReadError("bad_answer")
    cnt = d.get("comments")
    return {"count": cnt if isinstance(cnt, int) and not isinstance(cnt, bool) and cnt >= 0 else None, "updated": github.clean_ts(d.get("updated_at")),
            "state": "closed" if d.get("state") == "closed" else "open", "fresh": rep.fresh, "srv": rep.server_now,
            "assignees": sorted({u["login"].lower() for u in (d.get("assignees") or [])[:30] if isinstance(u, dict) and isinstance(u.get("login"), str) and github.OWNER_RE.fullmatch(u["login"])}),
            "labels": [github.clean_text(lb.get("name"), 50) for lb in (d.get("labels") or [])[:LABEL_CAP] if isinstance(lb, dict) and github.clean_text(lb.get("name"), 50)]}


def _read_pages(c: dict, ttl: float, spend, count) -> dict:
    """The pages of one thread: page 1, then the page where the thread ends (worked out from `count`, the comments count GitHub sent, and the page size, and walked
    up or down while a page disagrees; a Link URL is never followed) and the page before it when the newest COMMENT_CAP comments need it. Returns {flat (the comments
    read, in order), complete (every comment of the issue is in `flat`: a thread of up to 2 * COMMENT_CAP comments is read whole, a longer one gives its first and its
    last COMMENT_CAP comments, and a thread whose end was not found is not complete), end (how many comments the thread has, or None), fresh (every page confirmed by
    GitHub in this call), srv (the oldest server time of a page), now (the newest), odd, last_next, ids_ok}. `spend()` raises _Cut when the refresh may not make
    another call."""
    per = github.PER_PAGE
    pages: dict[int, list] = {}
    nxt: dict[int, bool] = {}
    seen = {"srv": None, "now": None, "fresh": True, "odd": False}

    def page(p: int) -> list:
        if p not in pages:
            spend()
            rep = github.reader.get(github.comments_path(c["owner"], c["name"], c["number"], p, per), ttl)
            if not isinstance(rep.data, list):
                raise github.GhReadError("bad_answer")
            if rep.server_now is not None:
                seen["srv"] = rep.server_now if seen["srv"] is None else min(seen["srv"], rep.server_now)
                seen["now"] = rep.server_now if seen["now"] is None else max(seen["now"], rep.server_now)
            seen["fresh"] = seen["fresh"] and rep.fresh
            nxt[p] = 'rel="next"' in (rep.headers.get("link") or "")
            if len(rep.data) < per and nxt[p]:
                seen["odd"] = True                                                  # a short page that says more follows: the page arithmetic does not hold, so no whole read
            pages[p] = rep.data
        return pages[p]

    def out(complete: bool, end, wanted=None) -> dict:
        flat: list = []
        for p in sorted(pages):
            for i, x in enumerate(pages[p]):
                if wanted is None or ((p - 1) * per + i + 1) in wanted:
                    flat.append(x)
        ids = [x.get("id") if isinstance(x, dict) else None for x in flat]
        ok = all(isinstance(i, int) and not isinstance(i, bool) for i in ids) and all(a < b for a, b in zip(ids, ids[1:]))      # strictly increasing: no repeat, no reorder
        return {"flat": flat, "complete": complete and not seen["odd"], "end": end, "fresh": seen["fresh"], "srv": seen["srv"], "now": seen["now"], "odd": seen["odd"],
                "last_next": nxt.get(max(pages), False), "ids_ok": ok}

    first = page(1)
    if len(first) < per:
        return out(True, len(first))
    p = max(2, -(-count // per)) if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 2          # the last page by the count GitHub sent
    end = None                                                                                                      # how many comments the thread has
    for _ in range(4):
        d = page(p)
        if 0 < len(d) < per:
            end = (p - 1) * per + len(d)
            break
        if not d:
            if len(page(p - 1)) == per:
                end = (p - 1) * per
                break
            p -= 1
        else:
            p += 1
    if end is None:
        return out(False, None)
    head, tail = set(range(1, min(end, COMMENT_CAP) + 1)), set(range(max(1, end - COMMENT_CAP + 1), end + 1))
    for idx in sorted(head | tail):
        page((idx - 1) // per + 1)
    return out(min(tail) <= max(head) + 1, end, head | tail)


def _consistent(pr: dict, before: dict, after: dict) -> bool:
    """True when the pages of a thread are one reading of it: the issue's comments count and updated_at are the same before and after, the count is the number of
    comments the pages show the thread to have, the ids of what was read strictly increase (so none repeats), a whole thread has exactly that many comments in
    hand, and no page read carried a next link beyond the last one read."""
    if before["count"] is None or after["count"] is None or before["count"] != after["count"] or before["updated"] != after["updated"]:
        return False
    if pr["end"] is not None and pr["end"] != after["count"]:
        return False
    if not pr["ids_ok"] or pr["last_next"]:
        return False
    return not pr["complete"] or len(pr["flat"]) == after["count"]


def _read_consistent(c: dict, ttl: float, spend, before: dict) -> dict:
    """One thread, read so that `none` can be believed. `before` is the issue's count and updated_at read in this refresh (the revalidated list, or `_snapshot`).
    Pages are read with `ttl` (0: every page revalidated now), then the issue is read again, revalidated; if anything differs the whole thread is read ONCE more
    against that newer reading, and if that differs too the result is `consistent: False` (reason "changed while it was read"). Pages that were not confirmed by GitHub
    in this call (a cache entry, a stale one) are returned as `fresh: False` with no second read. Returns {flat, complete, fresh, consistent, srv, now}."""
    pr: dict = {}
    for _ in range(2):
        pr = _read_pages(c, ttl, spend, before["count"])
        if not (pr["fresh"] and before["fresh"]):
            return {**pr, "fresh": False, "consistent": True}
        after = _snapshot(c, spend)
        if not after["fresh"]:
            return {**pr, "fresh": False, "consistent": True}
        if _consistent(pr, before, after):
            return {**pr, "consistent": True, "now": max(t for t in (pr["now"], after["srv"]) if t is not None) if (pr["now"] or after["srv"]) else None}
        before = after
    return {**pr, "consistent": False}


def _read_epic(owner: str, repo: str, n: int, readable: frozenset = frozenset(), read_comments: bool = True) -> dict:
    """The GitHub part of an epic's answer: the parent, its children (with the claim comments of each) and what was cut. Raises GhReadError when the parent
    cannot be read; a failure while reading comments ends that part early (`partial_error`) and keeps what was read. Comments are asked only for children in a
    repository of `readable` (lower-case slugs: this board's own projects); the others, and every child when `read_comments` is False, get a reason instead.
    The list of children is revalidated on every refresh (a 304 costs no request budget), so its comments counts are current; an open child's pages are revalidated
    too and the issue is read once more after them (see `_read_consistent`): at most pages + 1 gh calls for a child, none for a child with no comments."""
    started = mono()
    rd = github.reader
    head = rd.get(github.issue_path(owner, repo, n), ISSUE_TTL)
    issue = head.data
    if not isinstance(issue, dict):
        raise github.GhReadError("bad_answer")
    labels = [github.clean_text(lb.get("name"), 50) for lb in (issue.get("labels") or [])[:LABEL_CAP] if isinstance(lb, dict)]
    labels = [lb for lb in labels if lb]
    part = {"epic": {"repo": f"{owner}/{repo}", "owner": owner, "name": repo, "number": n, "title": github.clean_text(issue.get("title"), 200) or f"Issue {n}",
                     "url": issue_url(owner, repo, n), "state": "closed" if issue.get("state") == "closed" else "open", "labelled": EPIC_LABEL in labels,
                     "updated_at": github.clean_ts(issue.get("updated_at")), "author": author_of(issue)},
            "source": "sub_issues", "note": None, "truncated": False, "partial": False, "comments_pending": 0, "comments_failed": 0, "partial_error": None,
            "server_now": head.server_now, "stale": head.stale}
    try:
        items, trunc, srv, stale, lfresh = _paged(lambda p: github.sub_issues_path(owner, repo, n, p), CHILD_CAP, 0.0)
    except github.GhReadError as e:
        if e.reason != "not_found":
            raise
        part["source"] = "labels"
        part["note"] = f"This repository has no sub-issues endpoint, so the children are the issues labelled {FALLBACK_LABEL}{n}."
        items, trunc, srv, stale, lfresh = _paged(lambda p: github.labeled_path(owner, repo, f"{FALLBACK_LABEL}{n}", "all", p), CHILD_CAP, 0.0)
        items = sorted((i for i in items if isinstance(i, dict) and isinstance(i.get("number"), int)), key=lambda i: i["number"])
    same = part["source"] == "labels"
    own = f"{owner}/{repo}".lower()
    children, unplaced = [], 0
    for item in items:
        c = shape_child(item, owner, repo, len(children) + 1, same)
        if c is False:
            unplaced += 1                                                           # a sub-issue whose payload names no repository: it cannot be placed, so the epic is not complete
        elif c is not None and not (c["number"] == n and c["repo"].lower() == own):
            children.append(c)
    _link_children(children)
    part.update(children=children, truncated=trunc or unplaced > 0, stale=part["stale"] or stale)
    srv_all = [t for t in (head.server_now, srv) if t is not None]
    calls0 = rd.calls

    def spend() -> None:
        if rd.calls - calls0 >= COMMENT_CALLS or mono() - started > DEADLINE:
            raise _Cut()

    stop = None                                                                     # why the loop ended early: a reason code of UNKNOWN_TEXT
    for c in sorted(children, key=lambda c: (c["state"] != "open", c["order"])):          # open children first: they are the ones that change
        if not read_comments:
            c["reason"] = "authors_unknown"
            continue
        if c["repo"].lower() not in readable:                                       # not one of this board's own repositories: no request, whatever the payload or a node says
            c["reason"] = "other_repo"
            continue
        if c["comments"] == 0 and lfresh:                                           # the list was confirmed by GitHub in this refresh and says there is no comment
            c["thread"], c["fresh"], c["read_at"] = "complete", True, srv
            continue
        if stop:
            c["reason"] = stop
            continue
        try:
            if lfresh and isinstance(c["comments"], int):
                before = {"count": c["comments"], "updated": c["updated_at"], "fresh": True, "srv": srv}
            else:
                before = _snapshot(c, spend)                                        # the list is not this refresh's own reading: ask the issue itself
            res = _read_consistent(c, COMMENTS_TTL if c["state"] == "open" else COMMENTS_TTL_CLOSED, spend, before)
        except _Cut:
            stop = c["reason"] = "budget"
            continue
        except github.GhReadError as e:
            part["comments_failed"] += 1
            c["reason"] = "rate_limited" if e.reason == "rate_limited" else "error"
            if e.reason in ("rate_limited", "no_login", "no_gh", "timeout"):
                part["partial_error"] = e.as_dict()
                stop = c["reason"]
            continue
        c["claims"], c["server_now"], c["read_at"], c["fresh"] = parse_claims(res["flat"]), res["now"], res["srv"], res["fresh"]
        if not res["consistent"]:
            c["thread"], c["reason"] = "incomplete", "changed"
        else:
            c["thread"], c["reason"] = ("complete", None) if res["complete"] else ("incomplete", "too_long")
        part["stale"] = part["stale"] or not res["fresh"] and c["state"] == "open"
        if res["now"] is not None:
            srv_all.append(res["now"])
    # children of this board's repositories whose comments were not all read for a reason that passes (budget, a failure): the next refresh reads them
    part["comments_pending"] = sum(1 for c in children if c["thread"] is None and c["reason"] in ("budget", "rate_limited", "error"))
    part["partial"] = bool(part["comments_pending"])
    part["server_now"] = max(srv_all) if srv_all else None
    return part


@_claim_step("read")
def fresh_claim_state(owner: str, repo: str, number, repos, login=None, detail: bool = False) -> dict:
    """The claim state of ONE issue, read now with every cache and TTL bypassed: the issue and each page of its comments are revalidated with GitHub in this call
    (a 304 on If-None-Match counts), the issue is read again afterwards and the thread is read once more if anything moved (see `_read_consistent`). Returns
    {state: claimed | none | unknown, reason (a key of UNKNOWN_TEXT, or None), text, claim (the allowed claim, or None), read_at (GitHub's clock, ISO), issue_state
    (open | closed | None)}. It costs at most the pages plus 2 gh calls (more only for a retry) against the same budget and deadline as a refresh.
    **This is the only answer a later phase may act on.** The `claim_state` that GET /api/epics/... shows can be minutes old (a closed issue's comments are cached for
    ten minutes), may come from the last good answer while GitHub is unreachable, and says `none` only for the moment it was read: nothing may treat a cached `none` as
    free. `repos` is the list from `known_repos` (an issue of any other repository is `unknown: other_repo`, with no request).
    `detail=True` (the claim protocol, issue #148) adds what the same reading held: `claims` (every claim that counts, oldest first by (created_at, id), each with `live`
    judged by GitHub's clock), `comments` (the raw comments read), `now` (GitHub's clock, epoch), `issue` ({assignees, labels} of the first reading) and `complete`."""
    out = {"state": "unknown", "reason": None, "text": None, "claim": None, "read_at": None, "issue_state": None}

    def done(reason: str) -> dict:
        out.update(state="unknown", reason=reason, text=UNKNOWN_TEXT[reason])
        return out
    if not github.valid_slug(owner, repo) or not github.valid_number(number) or f"{owner}/{repo}".lower() not in {s.lower() for s in (repos or ())}:
        return done("other_repo")
    allowed = allowed_authors(None if settings.epic_authors else (login if login is not None else _gh_login()))
    if not allowed:
        return done("authors_unknown")
    rd = github.reader
    started, calls0 = mono(), rd.calls

    def spend() -> None:
        if rd.calls - calls0 >= COMMENT_CALLS or mono() - started > DEADLINE:
            raise _Cut()
    c = {"owner": owner, "name": repo, "number": int(number), "comments": None, "state": "open"}
    try:
        before = _snapshot(c, spend)
        out["issue_state"] = before["state"]
        if not before["fresh"]:
            return done("not_recent")
        if before["count"] == 0:
            res = {"flat": [], "complete": True, "fresh": True, "consistent": True, "srv": before["srv"], "now": before["srv"]}
        else:
            res = _read_consistent(c, 0.0, spend, before)
    except _Cut:
        return done("budget")
    except github.GhReadError as e:
        return done("rate_limited" if e.reason == "rate_limited" else "error")
    if not res["fresh"]:
        return done("not_recent")                                                   # a page came from the cache or a stale entry: the thread is not known now, whatever it holds
    parsed = parse_claims(res["flat"])
    claim, _ = pick_claim(parsed, allowed, res["now"])
    c.update(thread="incomplete" if not res["consistent"] or not res["complete"] else "complete", fresh=res["fresh"],
             reason="changed" if not res["consistent"] else (None if res["complete"] else "too_long"))
    state, reason = _claim_state(c, claim, True)
    out.update(state=state, reason=reason, text=UNKNOWN_TEXT[reason] if reason else None, claim=claim if state == "claimed" else None, read_at=github.epoch_iso(res["srv"]))
    if detail:
        ok = allowed_claims(parsed, allowed)
        for x in ok:
            x["live"] = is_live(x, res["now"])
        out.update(claims=ok, comments=res["flat"], now=res["now"], complete=bool(res["complete"] and res["consistent"]),
                   issue={"assignees": before["assignees"], "labels": before["labels"]})
    return out


# ---------------------------------------------------------------- the hub's side: nodes, tasks, pull requests

def _display() -> str:
    try:
        return nodes.display_name()
    except Exception:
        return "node"


def node_list(records) -> list[dict]:
    """This board and every paired node, for the pin menu: {handle, name, status, age_s} and, for a paired node, `tasks` (the pair holds the tasks scope)."""
    out = [{"handle": nodes.LOCAL, "name": _display(), "status": "online", "age_s": 0}]
    for r in records or []:
        if isinstance(r, dict) and nodes.valid_handle(r.get("handle")) and not r.get("legacy"):
            out.append({"handle": r["handle"], "name": github.clean_text(r.get("name"), 60) or r["handle"], "status": r.get("status") or "offline", "age_s": r.get("age_s"),
                        "tasks": "tasks" in (nodes._scopes_of(r.get("scopes")) or [])})          # the pair holds the tasks scope: a claim can be asked of this node (issue #148)
    return out


def _phase(v) -> str:
    return v if isinstance(v, str) and v in PHASES else "unknown"


def _task_views(db, records, display: str) -> dict[int, list[dict]]:
    """{issue number: [task views]} from this board's tasks and the hub's last readings of the nodes. A view: {slug (lower | None), repo (lower), id, phase,
    node, node_name, status, age_s, updated_at, pr}. A remote task carries only `#<n>` (or `owner/name#<n>`) as its issue_ref, so the repository is told by the
    node's own project list (slug) or, failing that, by the repository name."""
    by_num: dict[int, list[dict]] = {}
    try:
        local = db.tasks()
    except Exception:
        local = []
    for t in local:
        n = t.get("issue_number")
        if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
            continue
        m = _ISSUE_URL.fullmatch(t.get("issue_url") or "")
        slug = f"{m.group(1)}/{m.group(2)}".lower() if m and int(m.group(3)) == n else None
        by_num.setdefault(n, []).append({"slug": slug, "repo": str(t.get("repo") or "").lower(), "id": t.get("id"), "phase": _phase(t.get("phase") or "running"),
                                         "node": nodes.LOCAL, "node_name": display, "status": "online", "age_s": 0, "updated_at": str(t.get("updated_at") or "") or None,
                                         "pr": _local_pr(t)})
    for r in records or []:
        st = r.get("state") if isinstance(r, dict) else None
        if not isinstance(st, dict) or r.get("legacy") or not nodes.valid_handle(r.get("handle")):
            continue
        slugs = {}
        for p in st.get("projects") or []:
            for rp in (p.get("repos") or []) if isinstance(p, dict) else []:
                if isinstance(rp, dict) and isinstance(rp.get("slug"), str):
                    slugs[(p.get("name"), rp.get("name"))] = rp["slug"].lower()
        for t in st.get("tasks") or []:
            ref = t.get("issue_ref") if isinstance(t, dict) else None
            m = re.fullmatch(r"(?:([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100}))?#([1-9][0-9]{0,8})", ref) if isinstance(ref, str) else None
            if not m:
                continue
            slug = f"{m.group(1)}/{m.group(2)}".lower() if m.group(1) else slugs.get((t.get("project"), t.get("repo")))
            by_num.setdefault(int(m.group(3)), []).append({
                "slug": slug, "repo": str(t.get("repo") or "").lower(), "id": t.get("id") if isinstance(t.get("id"), int) else None, "phase": _phase(t.get("phase")),
                "node": r["handle"], "node_name": github.clean_text(r.get("name"), 60) or r["handle"], "status": r.get("status") or "offline", "age_s": r.get("age_s"),
                "updated_at": github.clean_text(t.get("updated_at"), 40) or None, "pr": None})
    return by_num


def _local_pr(t: dict) -> dict | None:
    n, state = t.get("pr_number"), str(t.get("pr_state") or "").upper()
    if isinstance(n, bool) or not isinstance(n, int) or n <= 0 or state not in ("OPEN", "MERGED", "CLOSED"):
        return None
    draft = False
    try:
        import json
        pj = json.loads(t.get("pr_json") or "{}")
        draft = bool(pj.get("is_draft")) if isinstance(pj, dict) else False
    except ValueError:
        pass
    return {"number": n, "state": state.lower(), "draft": draft}


def _join_task(views: dict[int, list[dict]], child: dict, readable: frozenset) -> dict | None:
    """The task for a child ALREADY READ from GitHub: the exact issue number, and a repository slug that is one of this board's own (a node cannot name another one
    into the join, make a child appear or change one). A task of this board with no issue address may be told by its repository name; a node's task never is."""
    slug = child["repo"].lower()
    if slug not in readable:
        return None
    cands = [v for v in views.get(child["number"], []) if v["slug"] == slug or (v["slug"] is None and v["node"] == nodes.LOCAL and v["repo"] == child["name"].lower())]
    if not cands:
        return None
    active = [v for v in cands if v["phase"] in ("backlog", "queued", "running")]
    pool = active or cands
    return max(pool, key=lambda v: (v["updated_at"] or "", v["id"] or 0))


def _task_out(v: dict | None) -> dict | None:
    if v is None:
        return None
    ref = None
    if isinstance(v["id"], int) and not isinstance(v["id"], bool):
        try:
            ref = str(v["id"]) if v["node"] == nodes.LOCAL else nodes.format_ref(v["node"], str(v["id"]), "task")          # `42` here, `box:42` on a node
        except nodes.RefError:
            ref = None
    return {"ref": ref, "phase": v["phase"], "node": v["node"], "node_name": v["node_name"], "status": v["status"], "age_s": v["age_s"], "updated_at": v["updated_at"]}


# ---------------------------------------------------------------- settings: pins, pause (kept on this board only, kv `epic:<owner/repo#n>`)

def settings_key(owner: str, repo: str, n) -> str:
    return f"{KV_PREFIX}{owner.lower()}/{repo.lower()}#{int(n)}"


REPORTS = ("phase", "summary", "none")       # what a node posts on the issue of this epic (issue #149): phase comments only (the default), also the redacted head of the result, or nothing
DEFAULTS = {"pins": {}, "epic_pin": None, "paused": False, "report": "phase", "auto_pr": False, "close": False}
# Routing settings (issue #150) and the switch that lets an agent run the epic (issue #152). They are kept in the same row only when they are not at their default, so a row
# with none of them looks exactly as it did before: `max_window_pct` (None = CCBOARD_EPIC_MAX_WINDOW_PCT), `group` (lists of node handles that share one subscription, set by hand),
# `mcp_run` (false: an agent's epic_run is refused). Only a signed-in person on the epic page sets them.
ROUTING_DEFAULTS = {"max_window_pct": None, "group": [], "mcp_run": False}
GROUP_MAX, GROUP_MEMBERS_MAX = 8, 8


def report_word(v, default: str = "phase") -> str:
    """The `report` setting as one of REPORTS. A stored boolean is migrated without losing the choice (the first phases kept it as true or false): true is `summary` (the person
    asked for the result to be reported), false and missing are `phase`. Anything else is the default."""
    if v is True:
        return "summary"
    if v is False or v is None:
        return "phase"
    return v if isinstance(v, str) and v in REPORTS else default


def work_plan(st: dict) -> dict:
    """The three per-epic switches that start off, in plain words for the page, so a person reads what Run will post and push before pressing it."""
    return {"report": {"phase": "Posts short phase comments on each issue: waiting for a person, pull request opened, done or failed. No result text leaves the node.",
                       "summary": "Posts the phase comments and the first 1500 characters of each result, after home folders and anything shaped like a token are removed.",
                       "none": "Posts no phase comment and no result. The claim comment and a refusal still appear."}[st["report"]],
            "auto_pr": "Pushes the task's own branch and opens a pull request when a task is done." if st["auto_pr"] else "Pushes nothing and opens no pull request.",
            "close": "The pull request says Closes #N." if st["close"] and st["auto_pr"] else ("Close is on, but it only applies with pull requests on." if st["close"] else "Nothing says Closes #N.")}


def load_settings(db, owner: str, repo: str, n) -> dict:
    """The stored pins and flags with defaults filled in; whatever is stored is re-validated (a hand-edited row cannot put a wrong type in the answer)."""
    try:
        rec = db.kv_get(settings_key(owner, repo, n))
    except Exception:
        rec = None
    v = rec.get("value") if isinstance(rec, dict) else None
    v = v if isinstance(v, dict) else {}
    pins = {}
    for k, h in (v.get("pins") if isinstance(v.get("pins"), dict) else {}).items():
        if isinstance(k, str) and _DIGITS.fullmatch(k) and github.valid_number(k) and (h == nodes.LOCAL or nodes.valid_handle(h)) and len(pins) < PINS_CAP:
            pins[str(int(k))] = h
    ep = v.get("epic_pin")
    return {"pins": pins, "epic_pin": ep if ep == nodes.LOCAL or nodes.valid_handle(ep) else None, "paused": v.get("paused") is True,
            "report": report_word(v.get("report")), "auto_pr": v.get("auto_pr") is True, "close": v.get("close") is True,
            "max_window_pct": window_word(v.get("max_window_pct")), "group": group_word(v.get("group")), "mcp_run": v.get("mcp_run") is True}


def window_word(v):
    """The stored `max_window_pct` as a whole number from 10 to 100, else None (the board's CCBOARD_EPIC_MAX_WINDOW_PCT applies)."""
    return v if isinstance(v, int) and not isinstance(v, bool) and 10 <= v <= 100 else None


def group_word(v) -> list[list[str]]:
    """The stored manual subscription groups as lists of two to eight distinct node handles (`local` is never a member), at most eight lists, a handle in one list only. Anything else is dropped."""
    out: list[list[str]] = []
    seen: set[str] = set()
    for g in v[:GROUP_MAX] if isinstance(v, list) else []:
        members = [h for h in dict.fromkeys(g[:GROUP_MEMBERS_MAX] if isinstance(g, list) else []) if nodes.valid_handle(h) and h not in seen]
        if len(members) >= 2:
            out.append(members)
            seen.update(members)
    return out


def max_window_pct(st: dict) -> int:
    """The window percentage at which routing stops sending this epic's issues to a node: the epic's own setting, else the board's."""
    return st.get("max_window_pct") or int(getattr(settings, "epic_max_window_pct", 85) or 85)


def _handle_ok(h, known: set[str]) -> bool:
    return isinstance(h, str) and (h == nodes.LOCAL or (nodes.valid_handle(h) and h in known))


def _read_children(owner: str, repo: str, n) -> set[int]:
    """The numbers of the epic's own-repository issues this board has read (its last good answer, kept in memory): the only issues a pin may name."""
    with _lock:
        lg = _last_good.get((owner.lower(), repo.lower(), int(n)))
    own = f"{owner}/{repo}".lower()
    return {c["number"] for c in lg[1]["children"] if c["repo"].lower() == own} if lg is not None else set()


def save_settings(db, owner: str, repo: str, n, patch: dict) -> dict:
    """Merge `patch` ({pins, epic_pin, paused, report (phase | summary | none; a boolean is migrated), auto_pr, close}; only the keys present) into the stored settings and return the new ones. `pins` merges per
    issue: a handle sets, null or '' removes. Every handle must be `local` or a node in the registry, and a pin may name only an issue of the epic's own
    repository that this board has read as one of its children (BadRequest otherwise); issue keys are digits, flags are booleans. Nothing is sent to GitHub.
    When everything is back to its default the row is removed."""
    known = {r["handle"] for r in nodes.registry(db)}
    cur = load_settings(db, owner, repo, n)
    kids = _read_children(owner, repo, n) if isinstance(patch.get("pins"), dict) else set()
    if "pins" in patch:
        pins = patch["pins"]
        if not isinstance(pins, dict) or len(pins) > PINS_CAP:
            raise projects.BadRequest("pins is an object of issue number to node handle, at most 200")
        for k, h in pins.items():
            ks = str(k)
            if not _DIGITS.fullmatch(ks) or not github.valid_number(ks):
                raise projects.BadRequest("a pin key is an issue number")
            if h is None or h == "":
                cur["pins"].pop(str(int(ks)), None)
            elif _handle_ok(h, known):
                if int(ks) not in kids:
                    raise projects.BadRequest("a pin names an issue that is not one of the epic's issues this board has read (open the epic first)")
                cur["pins"][str(int(ks))] = h
            else:
                raise projects.BadRequest("a pin names a node that is not paired with this board (or not a node handle)")
        if len(cur["pins"]) > PINS_CAP:
            raise projects.BadRequest("too many pins")
    if "epic_pin" in patch:
        h = patch["epic_pin"]
        if h is None or h == "":
            cur["epic_pin"] = None
        elif _handle_ok(h, known):
            cur["epic_pin"] = h
        else:
            raise projects.BadRequest("epic_pin names a node that is not paired with this board (or not a node handle)")
    for k in ("paused", "auto_pr", "close"):
        if k in patch:
            if not isinstance(patch[k], bool):
                raise projects.BadRequest(f"{k} is true or false")
            cur[k] = patch[k]
    if "report" in patch:
        v = patch["report"]
        if not (isinstance(v, bool) or (isinstance(v, str) and v in REPORTS)):
            raise projects.BadRequest("report is phase, summary or none")
        cur["report"] = report_word(v)                         # a boolean from an older page is migrated: true is summary, false is phase
    if "mcp_run" in patch:
        if not isinstance(patch["mcp_run"], bool):
            raise projects.BadRequest("mcp_run is true or false")
        cur["mcp_run"] = patch["mcp_run"]
    if "max_window_pct" in patch:
        v = patch["max_window_pct"]
        if v is None:
            cur["max_window_pct"] = None
        elif isinstance(v, int) and not isinstance(v, bool) and 10 <= v <= 100:
            cur["max_window_pct"] = v
        else:
            raise projects.BadRequest("max_window_pct is a whole number from 10 to 100, or null for the board's setting")
    if "group" in patch:
        g = [] if patch["group"] is None else patch["group"]
        if not isinstance(g, list) or len(g) > GROUP_MAX or any(not isinstance(x, list) or len(x) > GROUP_MEMBERS_MAX for x in g):
            raise projects.BadRequest("group is a list of at most 8 lists of at most 8 node handles")
        seen: set[str] = set()
        for x in g:
            if len(x) < 2 or any(not _handle_ok(h, known) or h == nodes.LOCAL for h in x) or len(set(x)) != len(x) or seen & set(x):
                raise projects.BadRequest("a group names two or more different paired nodes, and a node is in one group only")
            seen.update(x)
        cur["group"] = [list(x) for x in g]
    key = settings_key(owner, repo, n)
    row = {k: cur[k] for k in DEFAULTS}
    row.update({k: cur[k] for k in ROUTING_DEFAULTS if cur[k] != ROUTING_DEFAULTS[k]})          # a routing setting at its default is not stored: an old row stays what it was
    if row == DEFAULTS:
        db.kv_del(key)
    else:
        db.kv_set(key, row)
    return cur


def settings_view(db, owner: str, repo: str, n, records) -> dict:
    """{pins: {issue: {handle, node_name, source, stale}}, epic_pin, paused, report (phase | summary | none), auto_pr, close, plan, nodes}: the stored settings as the page shows them.
    `plan` says in plain words what Run will post and push (report, auto_pr, close). A pin to a node that is no longer in the registry is `stale: true` and ignored."""
    known = {r["handle"] for r in nodes.registry(db)}
    names = {x["handle"]: x["name"] for x in node_list(records)}
    st = load_settings(db, owner, repo, n)
    return {"pins": {k: _pin_view(h, "page", known, names) for k, h in st["pins"].items()},
            "epic_pin": _pin_view(st["epic_pin"], "page", known, names) if st["epic_pin"] else None,
            "paused": st["paused"], "report": st["report"], "auto_pr": st["auto_pr"], "close": st["close"], "plan": work_plan(st), "nodes": node_list(records),
            "max_window_pct": max_window_pct(st), "max_window_pct_set": st["max_window_pct"] is not None, "group": [g for g in st["group"] if all(m in known for m in g)],
            "mcp_run": st["mcp_run"]}


def _alias_map(records, display: str) -> dict[str, str]:
    """{lower-case name or handle: handle} for this board (`local`) and the paired nodes."""
    m = {display.lower(): nodes.LOCAL}
    for r in records or []:
        if isinstance(r, dict) and nodes.valid_handle(r.get("handle")) and not r.get("legacy"):
            m.setdefault(r["handle"].lower(), r["handle"])
            if isinstance(r.get("name"), str):
                m.setdefault(r["name"].lower(), r["handle"])
    return m


def _pin_view(handle, source: str, known: set[str], names: dict[str, str]) -> dict:
    stale = not (handle == nodes.LOCAL or handle in known)
    return {"handle": handle, "node_name": names.get(handle), "source": source, "stale": stale}


def _label_pin(labels: list[str], amap: dict[str, str]) -> dict | None:
    for lb in labels:
        if lb.startswith(NODE_LABEL):
            alias = lb[len(NODE_LABEL):]
            if _NODE_ALIAS.fullmatch(alias):
                h = amap.get(alias.lower())
                return {"alias": alias, "handle": h, "stale": h is None}
    return None


# ---------------------------------------------------------------- progress

def _bucket(closed: bool, claim: dict | None, task: dict | None, claim_state: str = "none") -> tuple[str | None, str]:
    """(node alias | None, done|running|queued|failed|closed|open|unknown) of one child. A closed child is done (by the node that held it, when known); an open child
    is what its claim and its task say; a claim that is not live and has no task behind it counts for nobody: `open` (unclaimed, only when every comment was read)
    or `unknown` (its claim is not known: never counted as unclaimed)."""
    phase = task["phase"] if task else None
    if closed:
        who = (claim and claim["alias"]) or (task and task["node_name"])
        return (who, "done") if who else (None, "closed")
    if claim and claim["status"] == "done":
        return claim["alias"], "done"
    if claim and claim["status"] == "failed":
        return claim["alias"], "failed"
    if task and phase == "failed":
        return task["node_name"], "failed"
    if task and phase == "done":
        return task["node_name"], "done"
    if task and phase == "running":
        return task["node_name"], "running"
    if claim and claim.get("live") is True:
        return claim["alias"], "running" if claim["status"] == "running" else "queued"
    if task and phase in ("queued", "backlog"):
        return task["node_name"], "queued"
    return None, "unknown" if claim_state == "unknown" else "open"


def progress_of(rows: list[tuple[str | None, str]], amap: dict[str, str]) -> dict:
    """{by_node: {alias: {done, running, queued, failed, handle}}, unclaimed: {open, closed}, unknown, open, closed, total}; the counts add up to the children.
    `unclaimed` is only what was read whole and has no claim; `unknown` (claim not known) is kept apart and never added to it."""
    by: dict[str, dict] = {}
    unclaimed = {"open": 0, "closed": 0}
    unknown = 0
    for who, b in rows:
        if who is None:
            if b == "unknown":
                unknown += 1
            else:
                unclaimed["closed" if b == "closed" else "open"] += 1
            continue
        row = by.setdefault(who, {"done": 0, "running": 0, "queued": 0, "failed": 0, "handle": amap.get(who.lower())})
        row[b] += 1
    return {"by_node": by, "unclaimed": unclaimed, "unknown": unknown, "total": len(rows)}


# ---------------------------------------------------------------- the answers

def _gh_login() -> str | None:
    from . import prpoll
    cwd = Path(settings.projects_dir)
    return prpoll.gh_login(cwd if cwd.is_dir() else Path(settings.data_dir))      # a directory that exists: the probe needs a cwd, never the home


def _empty_epic(owner: str, repo: str, n: int) -> dict:
    return {"repo": f"{owner}/{repo}", "owner": owner, "name": repo, "number": n, "title": None, "url": issue_url(owner, repo, n), "state": None,
            "labelled": None, "updated_at": None}


def _claim_state(c: dict, claim: dict | None, authors_ok: bool, cached: bool = False) -> tuple[str, str | None]:
    """(claimed | none | unknown, the reason code when unknown) of one child. `none` (free) is a statement about a moment: it needs every comment read, every page
    confirmed by GitHub in THIS refresh (fetched, or revalidated with a 304), the reading consistent (the issue's comments count the same before and after) and no
    allowed claim; a thread read from the cache, from a stale entry or from the last good answer (`cached`) is `unknown: not read recently` instead. A claim found in
    a thread that was only read from its two ends counts only while it is live (a hidden live claim in the unread middle could otherwise be overridden). A reading
    that moved twice while it was taken is `unknown: changed`."""
    if not authors_ok:
        return "unknown", "authors_unknown"
    if c["reason"] == "changed":
        return "unknown", "changed"
    if c["thread"] == "complete":
        if claim:
            return "claimed", None
        return ("none", None) if c["fresh"] and not cached else ("unknown", "not_recent")
    if c["thread"] == "incomplete" and claim is not None and claim.get("live") is True:
        return "claimed", None
    return "unknown", c["reason"] or "budget"


def detail(db, owner: str, repo: str, n: int, records, *, login=None, repos=None) -> dict:
    """GET /api/epics/{owner}/{repo}/{n}. `records` is hub.records() ([] on a single board); `repos` is the list of GitHub repositories of this board's own projects
    (`known_repos`): the epic's repository must be one of them (projects.NotFound otherwise, before any request) and comments are read for children of those
    only. GitHub problems never raise: the answer carries `error` ({reason, message, wait_s}) and, when an earlier read exists, its data with `stale_age_s`.
    The caller has validated owner, repo and n."""
    readable = frozenset(s.lower() for s in (repos or ()))
    if f"{owner}/{repo}".lower() not in readable:
        raise projects.NotFound(f"this board has no project for {owner}/{repo}")
    for s in repos:
        if s.lower() == f"{owner}/{repo}".lower():
            owner, _, repo = s.partition("/")                      # the spelling of this board's own remote
            break
    key = (owner.lower(), repo.lower(), int(n))
    allowed = allowed_authors(None if settings.epic_authors else (login if login is not None else _gh_login()))
    part, error, stale_age = None, None, None
    try:
        part = _read_epic(owner, repo, int(n), readable, bool(allowed))
        with _lock:
            _last_good[key] = (mono(), part)
            while len(_last_good) > GOOD_MAX:
                _last_good.pop(next(iter(_last_good)))             # the oldest read goes first: a person asking for many epics cannot grow the memory
        error = part["partial_error"]
    except github.GhReadError as e:
        error = e.as_dict()
        with _lock:
            lg = _last_good.get(key)
        if lg is not None:
            part, stale_age = lg[1], int(max(0.0, mono() - lg[0]))
    except Exception as e:                                                  # never a 5xx screen: an unexpected shape is "GitHub sent something unreadable"
        log.warning("epic read failed: %s", e.__class__.__name__)
        error = github.GhReadError("bad_answer").as_dict()
    display = _display()
    amap = _alias_map(records, display)
    known = {r["handle"] for r in nodes.registry(db)}
    names = {x["handle"]: x["name"] for x in node_list(records)}
    st = load_settings(db, owner, repo, n)
    out = {"epic": part["epic"] if part else _empty_epic(owner, repo, int(n)), "source": part["source"] if part else None, "note": part["note"] if part else None,
           "children": [], "progress": {"by_node": {}, "unclaimed": {"open": 0, "closed": 0}, "unknown": 0, "open": 0, "closed": 0, "total": 0},
           "claims_note": None if allowed else AUTHORS_NOTE,
           **settings_view(db, owner, repo, n, records), "rate": github.reader.rate(), "server_now": github.epoch_iso(part["server_now"]) if part else None, "error": error, "stale_age_s": stale_age,
           "truncated": bool(part and part["truncated"]), "partial": bool(part and part["partial"]), "comments_pending": part["comments_pending"] if part else 0,
           "empty": bool(part is not None and not part["children"]), "writes": github_write.writes_enabled(), "own_claims": [],
           "authors": {"allowed": sorted(allowed), "epic": None, "epic_ok": None, "blocked": [], "text": None}}
    if part is None:
        return out
    out["own_claims"] = own_claims(db, [(c["repo"], c["number"]) for c in part["children"]])
    from . import nodes_work
    works = nodes_work.work_views(db, [(c["repo"], c["number"]) for c in part["children"]])          # this board's own epic work: reads the database only
    blocked = [{"number": c["number"], "author": c.get("author")} for c in part["children"] if c["state"] == "open" and allowed and not author_ok(c.get("author"), allowed)]
    epic_ok = author_ok(part["epic"].get("author"), allowed) if allowed else None
    out["authors"] = {"allowed": sorted(allowed), "epic": part["epic"].get("author"), "epic_ok": epic_ok, "blocked": blocked,
                      "text": (None if not allowed else "The epic is not by an allowed author, so nothing in it will run." if not epic_ok else
                               ("These issues are not by an allowed author and will be skipped: " + ", ".join(f"#{b['number']}" for b in blocked) + ".") if blocked else None)}
    views = _task_views(db, records, display)
    rows = []
    own = f"{owner}/{repo}".lower()
    cached = stale_age is not None                                          # the last good answer, not a reading of this call: nothing in it is `none`
    now = part["server_now"] + stale_age if cached and part["server_now"] is not None else part["server_now"]          # GitHub's clock as best known NOW: a cached live claim ages out by its lease
    for c in part["children"]:
        claim, rejected = pick_claim([dict(x) for x in c["claims"]], allowed, now)
        cstate, reason = _claim_state(c, claim, bool(allowed), cached)
        if cstate == "unknown" and c["state"] != "closed":
            claim = None                                           # a claim seen in a read that was not whole is not presented as the answer (a closed issue only says who did it)
        tv = _join_task(views, c, readable)
        task = _task_out(tv)
        who, bucket = _bucket(c["state"] == "closed", claim, task, cstate)
        rows.append((who, bucket))
        page_pin = st["pins"].get(str(c["number"])) if c["repo"].lower() == own else None          # a pin is by issue number: only an issue of the epic's own repository can carry one
        lp = _label_pin(c["labels"], amap)
        pin = None
        if page_pin and page_pin in known | {nodes.LOCAL}:
            pin = _pin_view(page_pin, "page", known, names)
        elif lp and not lp["stale"]:
            pin = _pin_view(lp["handle"], "label", known, names)
        elif page_pin:
            pin = _pin_view(page_pin, "page", known, names)
        elif lp:
            pin = {"handle": None, "node_name": None, "source": "label", "stale": True}
        pr = None
        if tv and tv["pr"]:
            pr = {"number": tv["pr"]["number"], "url": pull_url(c["owner"], c["name"], tv["pr"]["number"]), "state": tv["pr"]["state"], "draft": tv["pr"]["draft"]}
        out["children"].append({
            "repo": c["repo"], "number": c["number"], "title": c["title"], "state": c["state"], "url": c["url"], "labels": c["labels"], "depends_on": c["depends_on"],
            "blocked_by": c["blocked_by"], "dependency_cycle": c["dependency_cycle"], "order": c["order"], "updated_at": c["updated_at"], "agent": c.get("agent") or "claude",
            "claim": None if claim is None else {"alias": claim["alias"], "node": amap.get(claim["alias"].lower()), "status": claim["status"], "since": claim["created_at"],
                                                  "renewed_at": claim["updated_at"], "live": claim["live"], "attempt": claim["attempt"], "seq": claim["seq"],
                                                  "task_ref": claim["task_ref"], "branch": claim["branch"]},
            "claim_rejected": rejected, "claim_note": STALE_NOTE if rejected else None,
            "claim_state": cstate, "claims_read_at": github.epoch_iso(c["read_at"]), "claim_unknown": {"reason": reason, "text": UNKNOWN_TEXT[reason]} if cstate == "unknown" else None,
            "task": task, "pr": pr, "pin": pin, "author": c.get("author"), "author_ok": author_ok(c.get("author"), allowed) if allowed else None,
            "work": works.get((c["repo"].lower(), c["number"])),
            "label_pin": lp, "bucket": {"node": who, "state": bucket}})
        out["children"][-1]["phase"] = (out["children"][-1]["work"] or {}).get("phase") or bucket                 # the progress strip's own bucket of this child: the page groups rows by it, so rows and strip agree
    prog = progress_of(rows, amap)
    prog["open"] = sum(1 for c in part["children"] if c["state"] == "open")
    prog["closed"] = sum(1 for c in part["children"] if c["state"] == "closed")
    out["progress"] = prog
    return out


def _repo_pairs(projects_list) -> list[tuple[str, str]]:
    """[(slug, project name)] of every GitHub repository of this board's own projects, deduplicated without regard to case, at most REPO_SET_MAX (see `known_repos`)."""
    seen: dict[str, tuple[str, str]] = {}
    for p in projects_list or []:
        for r in (p.get("repos") or []) if isinstance(p, dict) else []:
            if isinstance(r, dict) and r.get("state") == "ok" and r.get("path"):
                slug = node_state.repo_slug(r["path"])
                if isinstance(slug, str) and "/" in slug:
                    o, _, name = slug.partition("/")
                    if github.valid_slug(o, name) and slug.lower() not in seen and len(seen) < REPO_SET_MAX:
                        seen[slug.lower()] = (slug, p.get("name") if isinstance(p.get("name"), str) else "")
    return list(seen.values())


def known_repos(projects_list) -> list[str]:
    """`owner/name` of every GitHub repository of THIS board's own projects (the `origin` remote of each repository on this disk, read with git; validated against
    GitHub's rules): the one set of repositories this board may ask GitHub about. Nothing another node sends is in it. Deduplicated without regard to case, at
    most REPO_SET_MAX. The list view reads the first REPO_CAP of them and says when there are more."""
    return [s for s, _ in _repo_pairs(projects_list)]


def new_options(projects_list, project: str | None = None) -> dict:
    """What the New epic sheet starts from (issue #158): {repos: [{repo, project}], writes_on, limits}. The repositories are `known_repos` with the project each belongs to; those of `project`
    (when named) come first. Local remotes only: nothing is asked of GitHub."""
    pairs = _repo_pairs(projects_list)
    if project:
        pairs.sort(key=lambda x: x[1] != project)                  # stable: the named project's repositories first, the rest in their order
    return {"repos": [{"repo": s, "project": pn} for s, pn in pairs], "writes_on": github_write.writes_enabled(),
            "limits": {"children": CHILDREN_MAX, "title": TITLE_MAX, "body": BODY_MAX}}


def _counts(owner: str, repo: str, issue: dict) -> dict | None:
    """{open, closed, total, more} children of an epic: from the parent's own sub_issues_summary when GitHub sends it (no extra call), else by reading the list.
    `more` is True when the list had more issues than were read (CHILD_CAP): the numbers are then a floor, and the page says "more than"."""
    s = issue.get("sub_issues_summary")
    if isinstance(s, dict) and all(isinstance(s.get(k), int) and not isinstance(s.get(k), bool) and s.get(k) >= 0 for k in ("total", "completed")) and s["completed"] <= s["total"]:
        return {"open": s["total"] - s["completed"], "closed": s["completed"], "total": s["total"], "more": False}
    n = issue["number"]
    try:
        items, more, *_ = _paged(lambda p: github.sub_issues_path(owner, repo, n, p), CHILD_CAP, LIST_TTL)
    except github.GhReadError as e:
        if e.reason != "not_found":
            raise
        items, more, *_ = _paged(lambda p: github.labeled_path(owner, repo, f"{FALLBACK_LABEL}{n}", "all", p), CHILD_CAP, LIST_TTL)
    kids = [i for i in items if isinstance(i, dict) and "pull_request" not in i and i.get("number") != n]
    closed = sum(1 for i in kids if i.get("state") == "closed")
    return {"open": len(kids) - closed, "closed": closed, "total": len(kids), "more": bool(more)}


def _read_list(repos: list[str]) -> dict:
    epics, errors, top = [], [], None
    srv = None
    cut = False                                                                      # more epics than are listed (EPIC_CAP in all, or more than one page in a repository)
    for slug in repos:
        owner, _, repo = slug.partition("/")
        try:
            items, ptrunc, s, *_ = _paged(lambda p, o=owner, r=repo: github.labeled_path(o, r, EPIC_LABEL, "open", p), EPIC_CAP, LIST_TTL)
        except github.GhReadError as e:
            if e.reason in ("no_gh", "no_login", "rate_limited", "timeout"):
                top = e.as_dict()
                break
            errors.append({"repo": slug, **e.as_dict()})
            continue
        cut = cut or ptrunc
        srv = s if s is not None and (srv is None or s > srv) else srv
        for it in items:
            if not isinstance(it, dict) or "pull_request" in it or not isinstance(it.get("number"), int) or not github.valid_number(it["number"]):
                continue
            if len(epics) >= EPIC_CAP:
                cut = True
                break
            try:
                counts = _counts(owner, repo, it)
            except github.GhReadError as e:
                counts = None
                if e.reason in ("no_gh", "no_login", "rate_limited", "timeout"):
                    top = e.as_dict()
            epics.append({"repo": slug, "owner": owner, "name": repo, "number": it["number"], "title": github.clean_text(it.get("title"), 200) or f"Issue {it['number']}",
                          "url": issue_url(owner, repo, it["number"]), "state": "open", "children": counts, "updated_at": github.clean_ts(it.get("updated_at")),
                          "labels": [github.clean_text(lb.get("name"), 50) for lb in (it.get("labels") or [])[:LABEL_CAP] if isinstance(lb, dict) and github.clean_text(lb.get("name"), 50)]})
            if top:
                break
        if top:
            break
    epics.sort(key=lambda e: e["updated_at"] or "", reverse=True)
    return {"epics": epics, "errors": errors, "error": top, "server_now": srv, "truncated": cut}


def list_epics(db, repos: list[str]) -> dict:
    """GET /api/epics: the open issues labelled ccboard:epic in `repos` (the first REPO_CAP of them: `repos_truncated` says when there are more), newest update
    first, each with its children's counts and whether this board paused it. `truncated` says there are more epics than listed. Cached 60 s (a failed read keeps the
    last list with its age). With no repository to look in nothing is asked of GitHub. `repos` is this board's own set (`known_repos`)."""
    more_repos = len(repos) > REPO_CAP
    repos = list(repos)[:REPO_CAP]
    key = tuple(r.lower() for r in repos)
    out = {"epics": [], "repos": list(repos), "repos_truncated": more_repos, "truncated": False, "errors": [], "error": None, "stale_age_s": None, "server_now": None,
           "rate": github.reader.rate()}
    if not repos:
        out["note"] = "No project here has a GitHub remote, so there is nowhere to look for epics."
        return out
    with _lock:
        hit = _list_good.get(key)
    if hit is not None and mono() - hit[0] < LIST_TTL:
        res = hit[1]
    else:
        try:
            res = _read_list(list(repos))
        except Exception as e:
            log.warning("epic list failed: %s", e.__class__.__name__)
            res = {"epics": [], "errors": [], "error": github.GhReadError("bad_answer").as_dict(), "server_now": None, "truncated": False}
        if res["error"] and hit is not None:
            out["stale_age_s"] = int(max(0.0, mono() - hit[0]))
            out["error"] = res["error"]
            res = hit[1]
        elif not res["error"]:
            with _lock:
                _list_good[key] = (mono(), res)
                while len(_list_good) > GOOD_MAX:
                    _list_good.pop(next(iter(_list_good)))
    out.update(epics=[], errors=res["errors"], server_now=github.epoch_iso(res["server_now"]), rate=github.reader.rate(), truncated=bool(res.get("truncated")))
    out["error"] = out["error"] or res["error"]
    for e in res["epics"]:
        st = load_settings(db, e["owner"], e["name"], e["number"])
        out["epics"].append({**e, "paused": st["paused"]})
    return out


# ---------------------------------------------------------------- the doctor row

def doctor_facts(repos: list[str], limit: int = 3) -> dict:
    """What the doctor's `epic-gh` row reports, read only: {state, scopes, remaining, limit, failed: {repo: reason}, checked}. `state` is ok, no_gh,
    no_login or error:<reason>. One /rate_limit call (free) and one tiny label query per repository (at most `limit`)."""
    out = {"state": "ok", "scopes": None, "remaining": None, "limit": None, "failed": {}, "checked": 0}
    try:
        rep = github.reader.get(github.RATE_PATH, 0.0)
    except github.GhReadError as e:
        out["state"] = e.reason if e.reason in ("no_gh", "no_login") else f"error:{e.reason}"
        out["wait_s"] = e.wait_s
        return out
    core = ((rep.data or {}).get("resources") or {}).get("core") if isinstance(rep.data, dict) else None
    if isinstance(core, dict):
        for k in ("remaining", "limit"):
            out[k] = core[k] if isinstance(core.get(k), int) and not isinstance(core.get(k), bool) else None
    out["scopes"] = sorted({s.strip() for s in rep.scopes.split(",") if s.strip()}) if isinstance(rep.scopes, str) else None          # of the /rate_limit answer itself
    for slug in repos[:limit]:
        o, _, r = slug.partition("/")
        try:
            github.reader.get(github.labeled_path(o, r, EPIC_LABEL, "open", 1, 1), 0.0)
            out["checked"] += 1
        except github.GhReadError as e:
            out["failed"][slug] = e.reason
            if e.reason == "rate_limited":
                break
    return out


def claim_doctor_facts(repos: list[str], limit: int = 3) -> dict:
    """What the doctor's `epic-claim` row reports, read only (GET /rate_limit and GET /repos/{o}/{r}; nothing is written): {state (ok, no_gh, no_login, error:<reason>), scopes,
    push: {repo: True | False | None}, private: {repo: bool | None}, failed: {repo: reason}, checked}. Called only when CCBOARD_EPIC_WRITES is on."""
    out = {"state": "ok", "scopes": None, "push": {}, "private": {}, "failed": {}, "checked": 0}
    try:
        rep = github.reader.get(github.RATE_PATH, 0.0)
    except github.GhReadError as e:
        out["state"] = e.reason if e.reason in ("no_gh", "no_login") else f"error:{e.reason}"
        out["wait_s"] = e.wait_s
        return out
    out["scopes"] = sorted({s.strip() for s in rep.scopes.split(",") if s.strip()}) if isinstance(rep.scopes, str) else None
    for slug in repos[:limit]:
        o, _, r = slug.partition("/")
        try:
            data = github.reader.get(github.repo_path(o, r), 0.0).data
        except github.GhReadError as e:
            out["failed"][slug] = e.reason
            if e.reason == "rate_limited":
                break
            continue
        perms = data.get("permissions") if isinstance(data, dict) else None
        push = perms.get("push") if isinstance(perms, dict) else None
        out["push"][slug] = push if isinstance(push, bool) else None
        priv = data.get("private") if isinstance(data, dict) else None
        out["private"][slug] = priv if isinstance(priv, bool) else None
        out["checked"] += 1
    return out


# ---------------------------------------------------------------- the claim (nodes epic, issue #148)
#
# Several nodes work one epic; each open issue must be taken by exactly one of them, a node that dies gives its issue back, and a node that was slow must not write after it lost.
# GitHub is the only arbiter and GitHub's clock the only clock: a claim is a comment on the issue, the order of the claims is (created_at, id) as GitHub stamped them, a claim is
# live while GitHub's own Date is less than the lease after the comment's updated_at, and the comment's id is the fencing token. The assignee and the label are visible state,
# never the lock.
#
#   0  guards   writes on (CCBOARD_EPIC_WRITES), a valid repository that is one of this board's own, an issue that is a child of an epic this board has read, a gh login that
#               counts as an author. With writes off nothing at all is asked of GitHub.
#   1  check    `fresh_claim_state`: the issue and every page of its comments revalidated now. unknown is never claimable; a live claim of anyone is `taken`; a closed issue
#               is never claimed; a claim that is not live is ignored by the ordering and noted as `reclaims=<id>`; the attempt is the highest seen plus one, and after attempt 3
#               the node stops and asks for a person.
#   2  post     the claim comment (status claimed, seq 1). Its id is the claim_ref's last part. A POST whose outcome is unknown is never repeated before the thread is read back.
#   3  decide   wait, read without cache, wait, read again: the node wins only when its claim is the first live one of BOTH reads. Otherwise it deletes its own comment and
#               nothing else.
#   4  mark     (after the win; the issue's order puts this before the decision, but a loser must leave nothing to undo and two claimers must not both think they added the
#               label) the assignee (this gh login, only if it was not one) and the label (created if missing), and notes, never a refusal, when GitHub does not take them.
#   5  hold     `renew` (every RENEW_S, by editing the own comment with seq + 1) and `start` (status running); both through the fence.
#   6  release  `release(ref, status)`: the comment's status, then the label and the assignee, but only what this board added.
#   *  fence    `fence_ok(ref)` before every later write: the comment exists, is the board's own (author, marker, nid), is live and is first. A definite loss marks the claim
#               `lost`, writes an audit row, calls LOST_HOOKS and ends every write for it; any read problem answers False (fail closed) without marking a loss.
#
# The board's own claims are kept in kv (`claim:<owner/repo#n>`, listed in `claim-index`): the comment id the board created, its nid, the seq, the attempt and what it added, so a
# restart can renew, release or drop them. A comment is edited or deleted only when its id is in that record AND a read just before showed it is still the board's own.

WAIT_S = 1.5                            # the two waits of the decide step (tests patch `sleep`)
RENEW_S = 300.0                         # the heartbeat
RENEW_MAX = 288                         # renewals one claim gets (24 h); then it is left to lapse
MAX_ATTEMPTS = 3
HELD_MAX = 20
CLAIM_KV = "claim:"
INDEX_KV = "claim-index"
PENDING_KV = "claim-pending:"
LOST_KEEP_S = 7 * 86400
RENEWER = True                          # tests that run many claims switch the timer off
sleep = time.sleep                      # the protocol's waits: tests patch it
CLAIMS_IN_FLIGHT = 4                    # claims a peer may ask of this board at once; one more is refused as `busy`
claim_slots = threading.BoundedSemaphore(CLAIMS_IN_FLIGHT)           # taken without waiting by the peer route (nodes_relay.peer_epic_claim)
LOST_HOOKS: list = []                   # callables (claim view, reason) run once when a claim is definitely lost: the run phase parks the task and raises the toast
RENEW_HOOKS: list = []                  # callables (actor, claim view) run on every beat of the renew timer for each held claim (issue #149: waiting for a person, a comment that had to wait, a start to retry)
CLAIM_TEXT = {
    "won": "This node holds the issue.",
    "already_held": "This board already holds that issue.",
    "writes_off": github_write.MESSAGES["writes_off"],
    "bad_request": "That is not an issue this board will claim.",
    "other_repo": "That repository is not one of this board's own projects.",
    "not_epic_child": "That issue is not one of the issues of an epic this board has read.",
    "authors_unknown": AUTHORS_NOTE,
    "login_not_allowed": "The gh login of this board is not among the logins whose claims count (CCBOARD_EPIC_AUTHORS).",
    "claim_unknown": "Who holds that issue is not known right now, so it is not claimed.",
    "taken": "Another node holds that issue.",
    "lost": "Another node was first, so this board withdrew its claim.",
    "closed": "That issue is closed.",
    "done": "That issue is already done.",
    "attempts_exhausted": "Three attempts ended without a result. This board asked for a person and stopped.",
    "too_many_claims": "This board already holds as many claims as it will keep.",
    "rate_limited": "GitHub asked us to wait before writing.",
    "busy": "This board is busy with other claims right now. Nothing was claimed; try again in a moment.",
    "no_access": "This gh login cannot push to that repository, so a claim from it would not count. Nothing was written.",
    "no_login": "gh is not logged in on this board.",
    "error": "GitHub did not take the claim, so nothing is claimed.",
    "outcome_unknown": "GitHub did not say whether the claim was written; the board will look before it writes again.",
    "withdraw_failed": "Another node was first and this board could not remove its comment yet; it will try again.",
}


# ---- who this board is in a claim: the alias and the nid

_WORDS = ("amber", "birch", "cedar", "delta", "ember", "fjord", "glade", "harbor", "iris", "juniper", "kelp", "lumen", "maple", "nimbus", "onyx", "pebble", "quartz", "river",
          "sage", "tundra", "umber", "vale", "willow", "yarrow", "zephyr", "acorn", "basalt", "cobalt", "dune", "fern", "grove", "heron", "indigo", "jade", "linden", "marsh",
          "nectar", "opal", "pine", "reed", "slate", "thistle", "violet", "wren", "alder", "brook", "clover", "dawn", "egret", "flint", "garnet", "hazel", "ivory", "jasper",
          "lark", "moss", "north", "orchid", "plover", "quill", "robin", "sorrel", "teal", "ursa")


def node_nid(node_id: str) -> str:
    """The 8 hex characters a claim names this board by: the start of sha256 of its node id (the id itself never leaves the board)."""
    return hashlib.sha256(str(node_id).encode("utf-8")).hexdigest()[:8]


def derived_alias(node_id: str) -> str:
    """A short neutral word and two hex characters from the hash of the node id, e.g. `willow-3f`: the default alias, never the host name."""
    h = hashlib.sha256(("alias:" + str(node_id)).encode("utf-8")).digest()
    return f"{_WORDS[h[0] % len(_WORDS)]}-{h[1]:02x}"


def host_names() -> set[str]:
    """The short names this machine goes by (lower case): its host name, its MagicDNS name and, when no CCBOARD_NODE_NAME is set, the name the board shows. A claim alias
    equal to one of them, or with a dot in it, looks like a host name."""
    out: set[str] = set()
    try:
        out.add(socket.gethostname().split(".")[0].lower())
    except OSError:
        pass
    try:
        dns = nodes.dns_name(nodes._ts())
        if dns:
            out.add(dns.split(".")[0].lower())
        if not settings.node_name:
            out.add(nodes.display_name().lower())
    except Exception:
        pass
    return {n for n in out if n}


def looks_like_host(alias: str) -> bool:
    return "." in alias or alias.lower() in host_names()


def alias_report(node_id: str | None = None) -> dict:
    """{alias (the one comments use), configured (CCBOARD_EPIC_ALIAS or None), source (setting | derived), host_like (the configured name has a dot or is this machine's short
    name), invalid (it is not letters, digits, - and _), ignored (configured but not used), nid}. A configured alias that looks like a host name or is not valid is NOT used:
    the derived word is, so a comment never carries a host name."""
    nid_src = node_id if node_id is not None else nodes.node_id()
    conf = settings.epic_alias or None
    invalid = bool(conf) and not github_write.ALIAS_RE.fullmatch(conf or "")
    host_like = bool(conf) and looks_like_host(conf)
    use = conf if conf and not invalid and not host_like else None
    return {"alias": use or derived_alias(nid_src), "configured": conf, "source": "setting" if use else "derived", "host_like": host_like, "invalid": invalid,
            "ignored": bool(conf) and use is None, "nid": node_nid(nid_src), "lease_min": settings.epic_lease_min, "writes": github_write.writes_enabled()}


class Actor(NamedTuple):
    """Who writes: the database the claim records live in, the node id (hashed into the nid), the gh login, the alias, the write queue (None: this board's) and the repositories
    this board may write to (None: `board_repos()`, this board's own projects, asked each time). A board has one; the tests make several in one process."""
    db: Any
    node_id: str
    login: str | None
    alias: str
    wq: Any = None
    repos: Any = None


def _repos(actor: "Actor", repos=None) -> list[str]:
    """The repositories a write may go to: the caller's list, else the actor's, else this board's own projects. Always a list."""
    return list(repos if repos is not None else actor.repos if actor.repos is not None else board_repos())


def board_actor(db=None) -> Actor:
    """This running board as the writer. Asks gh for its login (cached), so call it only after the writes switch has been checked."""
    from . import main
    nid_src = nodes.node_id()
    return Actor(db if db is not None else main.db, nid_src, _gh_login(), alias_report(nid_src)["alias"])


def board_repos() -> list[str]:
    from . import main
    return main._epic_repos()


# ---- claim_ref and the kv records

_CLAIM_REF = re.compile(r"([^/#:\s]{1,39})/([^/#:\s]{1,100})#([1-9][0-9]{0,8}):([1-9][0-9]{0,15})")
_klock = threading.RLock()
_locks: dict[tuple, threading.RLock] = {}
_renewers: dict[int, tuple[threading.Thread, threading.Event]] = {}


def make_ref(owner: str, repo: str, n, cid) -> str:
    return f"{owner}/{repo}#{int(n)}:{int(cid)}"


def parse_ref(ref) -> tuple[str, str, int, int] | None:
    """(owner, repo, issue number, comment id) of a claim_ref, or None when it is not one (any text a peer or a payload sends is checked here before it names anything)."""
    m = _CLAIM_REF.fullmatch(ref) if isinstance(ref, str) and len(ref) <= 200 else None
    return (m.group(1), m.group(2), int(m.group(3)), int(m.group(4))) if m and github.valid_slug(m.group(1), m.group(2)) else None


def _key(owner: str, repo: str, n) -> str:
    return f"{CLAIM_KV}{owner.lower()}/{repo.lower()}#{int(n)}"


def _lock_for(actor: Actor, owner: str, repo: str, n) -> threading.RLock:
    k = (id(actor.db), _key(owner, repo, n))
    with _klock:
        if k not in _locks:
            _locks[k] = threading.RLock()                       # one per issue ever touched, never dropped: a second lock for an issue would be two claims at once
        return _locks[k]


_REC_STATES = ("claiming", "held", "lost")


def _rec_load(actor: Actor, owner: str, repo: str, n) -> dict | None:
    """The stored claim record of this board for an issue, re-validated (a hand-edited row cannot put a wrong type in it), or None."""
    try:
        row = actor.db.kv_get(_key(owner, repo, n))
    except Exception:
        return None
    v = row.get("value") if isinstance(row, dict) else None
    if not isinstance(v, dict) or v.get("state") not in _REC_STATES:
        return None
    try:
        if not (isinstance(v.get("comment_id"), int) and not isinstance(v.get("comment_id"), bool) and v["comment_id"] > 0 and v.get("number") == int(n)
                and isinstance(v.get("nid"), str) and github_write.NID_RE.fullmatch(v["nid"]) and isinstance(v.get("alias"), str) and isinstance(v.get("seq"), int)
                and isinstance(v.get("attempt"), int) and v.get("status") in github_write.STATUSES and str(v.get("owner", "")).lower() == owner.lower()
                and str(v.get("repo", "")).lower() == repo.lower()):
            return None
    except (TypeError, ValueError):
        return None
    out = dict(v)
    out["added_assignee"], out["added_label"] = v.get("added_assignee") is True, v.get("added_label") is True
    out["renewals"] = v["renewals"] if isinstance(v.get("renewals"), int) and not isinstance(v.get("renewals"), bool) else 0
    out["ref"] = make_ref(out["owner"], out["repo"], n, out["comment_id"])
    return out


def _index(actor: Actor) -> list[str]:
    try:
        row = actor.db.kv_get(INDEX_KV)
    except Exception:
        return []
    v = row.get("value") if isinstance(row, dict) else None
    return [k for k in v if isinstance(k, str) and k.startswith(CLAIM_KV)][:HELD_MAX * 3] if isinstance(v, list) else []


def _trim_index(actor: Actor, idx: list[str]) -> list[str]:
    """`idx` cut to HELD_MAX * 3 keys: the oldest lost or finished records go first (their rows too), and a record that is held or being claimed is never dropped (it still has to
    be renewed, released or recovered). Only when every key is one of those can it stay longer than the cap."""
    cap = HELD_MAX * 3
    for key in list(idx):
        if len(idx) <= cap:
            break
        m = re.fullmatch(CLAIM_KV + r"([^/]+)/([^#]+)#([0-9]+)", key)
        rec = _rec_load(actor, m.group(1), m.group(2), int(m.group(3))) if m else None
        if rec is not None and rec["state"] in ("claiming", "held"):
            continue
        actor.db.kv_del(key)
        idx.remove(key)
    return idx


def _rec_save(actor: Actor, rec: dict) -> None:
    with _klock:
        key = _key(rec["owner"], rec["repo"], rec["number"])
        actor.db.kv_set(key, {k: v for k, v in rec.items() if k != "ref"})
        idx = _index(actor)
        if key not in idx:
            actor.db.kv_set(INDEX_KV, _trim_index(actor, idx + [key]))


def _rec_drop(actor: Actor, owner: str, repo: str, n) -> None:
    with _klock:
        key = _key(owner, repo, n)
        actor.db.kv_del(key)
        idx = [k for k in _index(actor) if k != key]
        actor.db.kv_set(INDEX_KV, idx) if idx else actor.db.kv_del(INDEX_KV)


def _pending_key(owner, repo, n) -> str:
    return PENDING_KV + _key(owner, repo, n)[len(CLAIM_KV):]


def claim_view(rec: dict) -> dict:
    """What a claim record shows a caller: no login, no node id, only the alias that is public anyway."""
    return {"claim_ref": rec["ref"], "repo": f"{rec['owner']}/{rec['repo']}", "issue": rec["number"], "comment_id": rec["comment_id"], "alias": rec["alias"], "status": rec["status"],
            "state": rec["state"], "seq": rec["seq"], "attempt": rec["attempt"], "task_ref": rec.get("task_ref"), "branch": rec.get("branch"), "task_id": rec.get("task_id"),
            "added_assignee": rec["added_assignee"], "added_label": rec["added_label"], "notes": list(rec.get("notes") or []), "lost_reason": rec.get("lost_reason"),
            "renewals": rec["renewals"]}


def held_claims(actor: Actor | None = None) -> list[dict]:
    """The claims this board keeps a record of (claiming, held, lost), as claim_view. Reads only the database."""
    actor = actor or board_actor_lazy()
    if actor is None:
        return []
    out = []
    for key in _index(actor):
        m = re.fullmatch(CLAIM_KV + r"([^/]+)/([^#]+)#([0-9]+)", key)
        if m:
            rec = _rec_load(actor, m.group(1), m.group(2), int(m.group(3)))
            if rec:
                out.append(claim_view(rec))
    return out


def own_claims(db, pairs) -> list[dict]:
    """This board's own claim records (claim_view, so no login, no node id, no claim_ref) for the given [(repo "owner/name", issue number)] pairs. Reads the database only, asks
    GitHub nothing and starts nothing: a board that never claimed answers []. The epic page shows these as held, renewing and lost."""
    want = {(str(r).lower(), int(n)) for r, n in pairs or [] if isinstance(r, str) and github.valid_number(n)}
    if not want:
        return []
    out = []
    for v in held_claims(Actor(db, "", None, "")):
        if (v["repo"].lower(), v["issue"]) in want:
            v = {k: v[k] for k in ("repo", "issue", "alias", "status", "state", "seq", "attempt", "task_ref", "branch", "task_id", "added_assignee", "added_label", "notes", "lost_reason", "renewals")}
            out.append(v)
    return out


def switches_view() -> dict:
    """The two settings of the claim as Settings shows them, read only: {writes, writes_env, alias, alias_source, alias_env, alias_note, lease_min, lease_env}. `alias_note` is a short
    word when the configured alias is not used (`host name` or `not valid`), else None. No node id, no hash and no host name."""
    r = alias_report()
    note = "host name" if r["host_like"] else "not valid" if r["invalid"] else None
    return {"writes": github_write.writes_enabled(), "writes_env": "CCBOARD_EPIC_WRITES", "alias": r["alias"], "alias_source": r["source"], "alias_env": "CCBOARD_EPIC_ALIAS",
            "alias_note": note, "lease_min": r["lease_min"], "lease_env": "CCBOARD_EPIC_LEASE_MIN"}


def has_records(db) -> bool:
    """True when this board keeps any claim record. A board that never claimed has none, so nothing at startup or shutdown needs to run for claims."""
    return bool(_index(Actor(db, "", None, "")))


def board_actor_lazy() -> Actor | None:
    """The board's actor without asking gh for anything (no login): enough to read and keep records. None when there is no database yet."""
    try:
        from . import main
        if main.db is None:
            return None
        return Actor(main.db, nodes.node_id(), None, alias_report()["alias"])
    except Exception:
        return None


def _audit(actor: Actor, action: str, ok: bool, owner: str, repo: str, n, detail: str | None = None) -> None:
    """One audit row for a claim step: the action, the repository and the issue number, and a reason word. Nothing else is written (no login, no comment body)."""
    nodes.audit("out", "", action, ok, detail, target=f"{owner}/{repo}#{int(n)}", status="ok" if ok else "failed", db=actor.db)


@_claim_step("write")
def _send(actor: Actor, req) -> tuple[int, dict, Any]:
    return github_write.send(req, actor.wq)


def _target(repo, issue) -> tuple[str, str, int] | None:
    if not isinstance(repo, str) or len(repo) > 140 or repo.count("/") != 1 or not github.valid_number(issue):
        return None
    owner, _, name = repo.partition("/")
    return (owner, name, int(issue)) if github.valid_slug(owner, name) else None


def is_read_child(owner: str, repo: str, n) -> bool:
    """True when issue n of owner/repo is a child of an epic this board has read (its last good answer, in memory) as an epic: the parent carries the `ccboard:epic` label and the
    reading is younger than READ_CHILD_MAX_AGE. An epic is not a child of itself, so it is not claimable; nothing another node or GitHub names makes an issue claimable."""
    slug = f"{owner}/{repo}".lower()
    now = mono()
    with _lock:
        parts = [v[1] for v in _last_good.values() if 0 <= now - v[0] <= READ_CHILD_MAX_AGE and (v[1].get("epic") or {}).get("labelled") is True]
    return any(c.get("number") == int(n) and str(c.get("repo", "")).lower() == slug for part in parts for c in part.get("children") or [])


def epic_has_child(epic: tuple, owner: str, repo: str, n) -> bool:
    """True when the epic `epic` = (owner, repo, number), as this board last read it from GitHub (memory only, younger than READ_CHILD_MAX_AGE), is labelled `ccboard:epic` and lists issue
    n of owner/repo as a child. Unlike `is_read_child` it names the epic: the author check of the work route needs THIS epic to be the issue's parent, not any epic."""
    now = mono()
    with _lock:
        hit = _last_good.get((str(epic[0]).lower(), str(epic[1]).lower(), int(epic[2])))
    if hit is None or not 0 <= now - hit[0] <= READ_CHILD_MAX_AGE or (hit[1].get("epic") or {}).get("labelled") is not True:
        return False
    slug = f"{owner}/{repo}".lower()
    return any(c.get("number") == int(n) and str(c.get("repo", "")).lower() == slug for c in hit[1].get("children") or [])


def epics_of_child(owner: str, repo: str, n) -> list[tuple]:
    """The epics (owner, repo, number) this board has read that list issue n of owner/repo as a child (see `epic_has_child`): the way a work request that names no epic finds it."""
    now = mono()
    slug = f"{owner}/{repo}".lower()
    with _lock:
        items = list(_last_good.items())
    return [(part["epic"].get("owner") or _o, part["epic"].get("name") or _r, _n) for (_o, _r, _n), (at, part) in items
            if 0 <= now - at <= READ_CHILD_MAX_AGE and (part.get("epic") or {}).get("labelled") is True
            and any(c.get("number") == int(n) and str(c.get("repo", "")).lower() == slug for c in part.get("children") or [])]


def _read(actor: Actor, owner: str, repo: str, n, repos) -> dict:
    return fresh_claim_state(owner, repo, n, repos, actor.login, detail=True)


def _mine(actor: Actor, comments, cid: int) -> tuple[dict | None, dict | None, str | None]:
    """(raw comment, its parse, a reason it is not the board's own) for comment `cid` in a list just read: the author must be this board's gh login (a user), the marker must
    parse and its nid must be this board's. A comment that is gone, edited into something else or somebody else's gives the reason. When this board does not know its own
    login (`login_unknown`) nothing can be said about whose the comment is: that is not a loss, and the callers treat it as not settled."""
    raw = next((c for c in comments or [] if isinstance(c, dict) and c.get("id") == cid), None)
    if raw is None:
        return None, None, "deleted"
    user = raw.get("user")
    login = user.get("login") if isinstance(user, dict) and user.get("type") == "User" else None
    if not actor.login:
        return raw, None, "login_unknown"
    if not isinstance(login, str) or login.lower() != actor.login.lower():
        return raw, None, "not_ours"
    parsed = parse_claim(raw.get("body"))
    if parsed is None:
        return raw, None, "unparseable"
    if parsed["nid"] != node_nid(actor.node_id):
        return raw, parsed, "edited"
    return raw, parsed, None


def _winner(claims: list[dict]) -> dict | None:
    live = [c for c in claims if c.get("live") is True]
    return live[0] if live else None


def _fence(actor: Actor, rec: dict, repos) -> dict:
    """The fence's reading of a held claim: {ok, lost (a definite loss), reason, mine (the claim as GitHub holds it), comment (the raw own comment), now, read}. Fails closed:
    anything the reading could not settle is ok False with lost False."""
    out = {"ok": False, "lost": False, "reason": None, "mine": None, "comment": None, "now": None, "read": None, "winner": None}
    d = _read(actor, rec["owner"], rec["repo"], rec["number"], repos)
    out["read"] = d
    if d["state"] == "unknown":
        out["reason"] = d["reason"] or "unknown"
        return out
    out["now"] = d.get("now")
    raw, parsed, why = _mine(actor, d.get("comments"), rec["comment_id"])
    if why == "deleted" and not d.get("complete"):
        out["reason"] = "too_long"                                      # the comment may sit in the part of a long thread that was not read: not a loss
        return out
    if why == "login_unknown":
        out["reason"] = why                                             # this board cannot tell whose the comment is right now: nothing is written, and nothing is lost
        return out
    if why:
        out.update(lost=True, reason=why)
        return out
    mine = next((c for c in d.get("claims") or [] if c["id"] == rec["comment_id"]), None)
    if mine is None:
        out.update(lost=True, reason="not_counted")
        return out
    out.update(mine=mine, comment=raw)
    if parsed["status"] not in ("claimed", "running"):
        out.update(lost=True, reason="status_changed")
    elif mine.get("live") is None:
        out["reason"] = "clock_unknown"
    elif mine["live"] is not True:
        out.update(lost=True, reason="expired")
    else:
        first = _winner(d["claims"])
        if first is None or first["id"] != rec["comment_id"]:
            out.update(lost=True, reason="not_first", winner=first["alias"] if first else None)
        elif not d.get("complete"):
            out["reason"] = "too_long"                                  # first among what was read, but a longer thread may hide an earlier live claim: not settled
        else:
            out["ok"] = True
    return out


def _mark_lost(actor: Actor, rec: dict, reason: str) -> None:
    """The claim is definitely lost: keep the record as `lost` (every later write for it is refused), write an audit row and call the hooks (the run phase parks the task
    `lost-claim` and raises the toast). Writes nothing to GitHub: the worktree and the comments stay for the owner."""
    rec = dict(rec, state="lost", lost_reason=reason, lost_at=time.time())
    try:
        _rec_save(actor, rec)
    except Exception:
        log.warning("could not keep a lost claim record")
    _audit(actor, "epic-claim-lost", False, rec["owner"], rec["repo"], rec["number"], reason)
    view = claim_view(_rec_load(actor, rec["owner"], rec["repo"], rec["number"]) or {**rec, "ref": rec["ref"]})
    for hook in list(LOST_HOOKS):
        try:
            hook(view, reason)
        except Exception as e:
            log.warning("claim lost hook failed: %s", e.__class__.__name__)


def fence(claim_ref, *, actor: Actor | None = None, repos=None) -> dict:
    """{ok, lost, reason, winner} for a claim_ref: re-reads the thread (revalidated, no cache) and requires the claim to exist, be the board's own, be live and be first. A
    definite loss is recorded once (see `_mark_lost`). Nothing is written to GitHub. Not a held claim of this board, or writes off: ok False."""
    ref = parse_ref(claim_ref)
    bad = {"ok": False, "lost": False, "reason": "unknown_claim", "winner": None}
    if ref is None or not github_write.writes_enabled():
        return {**bad, "reason": "writes_off" if ref is not None else "unknown_claim"}
    owner, repo, n, cid = ref
    actor = actor or board_actor()
    with _lock_for(actor, owner, repo, n):
        rec = _rec_load(actor, owner, repo, n)
        if rec is None or rec["comment_id"] != cid:
            return bad
        if rec["state"] == "lost":
            return {"ok": False, "lost": True, "reason": rec.get("lost_reason") or "lost", "winner": None}
        if rec["state"] != "held":
            return {**bad, "reason": "not_held"}
        f = _fence(actor, rec, _repos(actor, repos))
        if f["lost"]:
            _mark_lost(actor, rec, f["reason"])
        return {"ok": f["ok"], "lost": f["lost"], "reason": f["reason"], "winner": f["winner"]}


def fence_ok(claim_ref, *, actor: Actor | None = None, repos=None) -> bool:
    """True only when the claim still exists, is the board's own, is live and is first, read just now. Every later write by the holder (a progress comment, a label, a pull
    request, the release) is skipped unless this is True."""
    return fence(claim_ref, actor=actor, repos=repos)["ok"]


# ---- the steps

def _stop(res: dict, reason: str, **kw) -> dict:
    res.update(reason=reason, message=CLAIM_TEXT.get(reason), **kw)
    return res


def _err_reason(e: github_write.GhWriteError) -> str:
    return e.reason if e.reason in ("rate_limited", "no_access", "no_login", "busy") else "error"


@_claim_step("withdraw")
def _withdraw(actor: Actor, rec: dict, comments=None) -> tuple[bool, str | None]:
    """Delete the board's own claim comment `rec` named, nothing else, and forget the record. `comments` (a thread just read) must show it is still the board's own; without
    a reading the record alone is the proof (the id was created by this board in this run and checked against the POST's answer). Returns (done, a reason when not)."""
    if comments is not None:
        raw, _, why = _mine(actor, comments, rec["comment_id"])
        if why and why != "deleted":
            return False, why                                   # there, and not the board's own any more: never delete it
        # "deleted": a reading that lacks the comment does not prove it is gone (GitHub may not show a new comment yet); the id is the board's own record, so ask for the delete, a 404 is fine
    try:
        _send(actor, github_write.comment_delete(rec["owner"], rec["repo"], rec["comment_id"]))
    except github_write.GhWriteError as e:
        if e.reason == "not_found":
            _rec_drop(actor, rec["owner"], rec["repo"], rec["number"])
            return True, None
        return False, e.reason
    _rec_drop(actor, rec["owner"], rec["repo"], rec["number"])
    return True, None


def _ask_person(actor: Actor, owner: str, repo: str, n, comments) -> bool:
    """After the third attempt: one fixed comment (no marker, so no parser counts it) asking for a person. Not posted twice."""
    if any(isinstance(c, dict) and c.get("body") == github_write.HELP_BODY for c in comments or []):
        return False
    try:
        _send(actor, github_write.comment_create(owner, repo, n, github_write.HELP_BODY))
    except github_write.GhWriteError:
        return False
    return True


@_claim_step("read_back")
def _readback(actor: Actor, owner: str, repo: str, n, repos, before_ids: set, attempt: int) -> tuple[str, dict | None]:
    """After a POST whose outcome is unknown: read the thread (twice, WAIT_S apart) for a claim comment of this board that was not there before, with this attempt and seq 1.
    ("found", the raw comment) when it is there; ("absent", None) when two whole readings show it is not; ("unknown", None) when the readings could not settle it."""
    whole = 0
    for _ in range(2):
        sleep(WAIT_S)
        d = _read(actor, owner, repo, n, repos)
        if d["state"] == "unknown" or not d.get("complete"):
            continue
        whole += 1
        for raw in d.get("comments") or []:
            if not isinstance(raw, dict) or raw.get("id") in before_ids:
                continue
            _, parsed, why = _mine(actor, [raw], raw.get("id")) if isinstance(raw.get("id"), int) and not isinstance(raw.get("id"), bool) else (None, None, "x")
            if why is None and parsed and parsed["attempt"] == attempt and parsed["seq"] == 1 and parsed["status"] == "claimed":
                return "found", raw
    return ("absent", None) if whole == 2 else ("unknown", None)


def _sweep_orphans(actor: Actor, owner: str, repo: str, n, d: dict) -> int:
    """A pending marker says a POST of this board may have created a comment that was never recorded. Delete such comments (this board's own, by author and nid, with the
    pending attempt and seq 1) found in a complete reading, then forget the marker. Without a complete reading the marker stays."""
    try:
        row = actor.db.kv_get(_pending_key(owner, repo, n))
    except Exception:
        return 0
    pend = row.get("value") if isinstance(row, dict) else None
    if not isinstance(pend, dict) or d.get("state") == "unknown" or not d.get("complete"):
        return 0
    gone = 0
    mine_rec = _rec_load(actor, owner, repo, n)
    for raw in d.get("comments") or []:
        cid = raw.get("id") if isinstance(raw, dict) else None
        if not isinstance(cid, int) or isinstance(cid, bool) or (mine_rec and mine_rec["comment_id"] == cid):
            continue
        _, parsed, why = _mine(actor, [raw], cid)
        if why is None and parsed and parsed["attempt"] == pend.get("attempt") and parsed["seq"] == 1 and parsed["status"] == "claimed":
            try:
                _send(actor, github_write.comment_delete(owner, repo, cid))
                gone += 1
            except github_write.GhWriteError:
                return gone
    actor.db.kv_del(_pending_key(owner, repo, n))
    return gone


@_claim_step("read_back")
def _decide(actor: Actor, rec: dict, repos) -> tuple[str, str | None, dict | None]:
    """Step 3. (verdict, winner alias, the last reading): `won` only when the claim is the first live one of both of two uncached readings taken WAIT_S apart; `lost` (another
    live claim is first, or this one is not to be seen); `closed`; `error` (a reading could not settle it, so the claim is not believed)."""
    d = None
    for _ in range(2):
        sleep(WAIT_S)
        d = _read(actor, rec["owner"], rec["repo"], rec["number"], repos)
        if d["issue_state"] == "closed":
            return "closed", None, d
        if d["state"] == "unknown" or not d.get("complete"):
            return "error", None, d
        first = _winner(d["claims"])
        if first is None:
            return "error", None, d                             # this board's own claim is not to be seen (not visible yet, or not counted): it does not believe it won
        if first["id"] != rec["comment_id"]:
            return "lost", first["alias"], d
        if any(c.get("live") is None for c in d["claims"]):
            return "error", None, d
    return "won", rec["alias"], d


def _mark(actor: Actor, rec: dict, issue: dict, reclaiming: bool) -> dict:
    """Step 4 (after the win): the assignee and the label, as visible state. Returns the record with added_assignee, added_label and notes. Never raises: a refusal is a note."""
    notes: list[str] = []
    login = (actor.login or "").lower()
    o, r, n = rec["owner"], rec["repo"], rec["number"]
    added_assignee = added_label = False
    if login in (issue.get("assignees") or []):
        pass                                                    # already an assignee (a person, or a node of this login): this board did not add it and will not remove it
    else:
        try:
            _, _, data = _send(actor, github_write.assignee_add(o, r, n, actor.login))
            got = {u.get("login", "").lower() for u in (data.get("assignees") or []) if isinstance(u, dict)} if isinstance(data, dict) else set()
            if login in got:
                added_assignee = True
            else:
                notes.append("no_push_access")                  # GitHub ignores an assignee from a login that cannot be assigned, and says nothing
        except github_write.GhWriteError as e:
            notes.append("assignee_" + _err_reason(e))
    if github_write.LABEL in (issue.get("labels") or []):
        added_label = reclaiming                                # a dead holder's label: the board that takes over also takes over the clean-up
    else:
        try:
            try:
                _send(actor, github_write.label_add(o, r, n))
            except github_write.GhWriteError as e:
                if e.reason not in ("invalid", "not_found"):
                    raise
                try:
                    _send(actor, github_write.label_create(o, r))
                except github_write.GhWriteError as e2:
                    if e2.reason != "invalid":                  # 422: it exists already (someone made it in between)
                        notes.append("label_create_refused")
                        raise
                _send(actor, github_write.label_add(o, r, n))
            added_label = True
        except github_write.GhWriteError as e:
            if "label_create_refused" not in notes:
                notes.append("label_" + _err_reason(e))
    return dict(rec, added_assignee=added_assignee, added_label=added_label, notes=notes)


def read_epic_now(owner: str, repo: str, n, repos) -> bool:
    """Read the epic `owner/repo#n` from GitHub now (its children only, no comments) and keep it as this board's last good answer, so its children are issues of an epic this
    board has read. True only when the epic is one of this board's own repositories' issues, is labelled `ccboard:epic` and was read. Used by `claim_issue(epic=...)`: the
    node that runs a relayed claim has usually not had the epic page open, but it reads the epic itself; what a caller says about the epic is a number to read, never a
    list of children."""
    readable = frozenset(s.lower() for s in repos if isinstance(s, str))
    if f"{owner}/{repo}".lower() not in readable or not github.valid_number(n):
        return False
    try:
        part = _read_epic(owner, repo, int(n), readable, read_comments=False)
    except github.GhReadError:
        return False
    if not part["epic"].get("labelled"):
        return False
    with _lock:
        _last_good[(owner.lower(), repo.lower(), int(n))] = (mono(), part)
        while len(_last_good) > GOOD_MAX:
            _last_good.pop(next(iter(_last_good)))
    return True


def _epic_ref(epic, owner: str, name: str):
    """(owner, repo, number) of the `epic` argument of claim_issue: a number (an issue of the claimed issue's own repository) or "owner/name#number"; None when it is neither."""
    if epic is None:
        return None
    if github.valid_number(epic):
        return owner, name, int(epic)
    m = re.fullmatch(r"([^/#\s]{1,39})/([^/#\s]{1,100})#([1-9][0-9]{0,8})", epic) if isinstance(epic, str) and len(epic) <= 160 else None
    return (m.group(1), m.group(2), int(m.group(3))) if m and github.valid_slug(m.group(1), m.group(2)) else None


@_claim_step("total")
def claim_issue(repo, issue, *, actor: Actor | None = None, repos=None, epic=None, task_ref: str | None = None, branch: str | None = None) -> dict:
    """Claim issue `issue` of repository `repo` ("owner/name") for this board. Returns {claimed, reason, claim_ref, winner, message, ...}.

    `claimed` is True only for `won` (the board holds the issue: keep claim_ref, renew it, fence every later write with it) and `already_held` (this board already did; claim_ref
    is the same). Otherwise `reason` is one of CLAIM_TEXT: writes_off, bad_request, other_repo, not_epic_child, authors_unknown, login_not_allowed, claim_unknown (never
    claimable), taken (a live claim of another node, `winner` is its alias), lost (this board posted, was not first and deleted its own comment), closed, done,
    attempts_exhausted (after attempt 3), rate_limited / no_access / no_login / error (a GitHub write failed: `claimed` False and no claim comment is left), outcome_unknown (a
    write may have happened: the next call reads the thread before it writes), withdraw_failed. With writes off nothing at all is sent to GitHub, not even a read. Safe to
    call from several threads: one claim of one issue runs at a time on a board.
    The issue must be a child of an epic this board has READ. `epic` (a number in the issue's repository, or "owner/name#number") lets a board that has not had the epic open
    (a node running a relayed claim, or any board after a restart) read it first, on demand; the epic must be in this board's own repositories and labelled ccboard:epic.
    Without `epic` a child of an epic this board has not read is `not_epic_child`, and nothing is asked of GitHub."""
    res: dict = {"claimed": False, "reason": None, "claim_ref": None, "winner": None, "message": None}
    if not github_write.writes_enabled():
        return _stop(res, "writes_off")
    tgt = _target(repo, issue)
    if tgt is None:
        return _stop(res, "bad_request")
    owner, name, n = tgt
    repos = list(repos if repos is not None else actor.repos if actor is not None and actor.repos is not None else board_repos())
    spelled = next((s for s in repos if isinstance(s, str) and s.lower() == f"{owner}/{name}".lower()), None)
    if spelled is None or not github.valid_slug(*spelled.partition("/")[::2]):
        return _stop(res, "other_repo")
    owner, _, name = spelled.partition("/")
    if not is_read_child(owner, name, n):
        ep = _epic_ref(epic, owner, name)
        if epic is not None and ep is None:
            return _stop(res, "bad_request")
        if ep is None:
            return _stop(res, "not_epic_child")
        if f"{ep[0]}/{ep[1]}".lower() not in {s.lower() for s in repos if isinstance(s, str)}:
            return _stop(res, "other_repo")
        if not read_epic_now(ep[0], ep[1], ep[2], repos) or not is_read_child(owner, name, n):
            return _stop(res, "not_epic_child")
    actor = actor or board_actor()                              # asks gh who it is logged in as: only after the cheap refusals above
    if not actor.login:
        return _stop(res, "authors_unknown")
    if actor.login.lower() not in allowed_authors(None if settings.epic_authors else actor.login):
        return _stop(res, "login_not_allowed")
    alias = actor.alias if github_write.ALIAS_RE.fullmatch(actor.alias or "") and not looks_like_host(actor.alias) else derived_alias(actor.node_id)
    nid = node_nid(actor.node_id)
    with _lock_for(actor, owner, name, n):
        return _claim_locked(actor, repos, owner, name, n, alias, nid, res, task_ref, branch)


def _may_push(owner: str, repo: str) -> bool | None:
    """Can this board's gh login push to the repository? Read now (GET /repos/{o}/{r}, revalidated): True or False as GitHub says in `permissions.push`, None when the read failed or
    the field is missing. A claim comment counts only from a login that can write (allowed_claims wants OWNER, MEMBER or COLLABORATOR), so a login that cannot push would post a
    public comment that never counts and then delete it."""
    try:
        data = github.reader.get(github.repo_path(owner, repo), 0.0).data
    except github.GhReadError:
        return None
    perms = data.get("permissions") if isinstance(data, dict) else None
    push = perms.get("push") if isinstance(perms, dict) else None
    return push if isinstance(push, bool) else None


def _claim_locked(actor: Actor, repos: list, owner: str, name: str, n: int, alias: str, nid: str, res: dict, task_ref, branch) -> dict:
    prior = _rec_load(actor, owner, name, n)
    if prior is None and sum(1 for v in held_claims(actor) if v["state"] in ("claiming", "held")) >= HELD_MAX:
        return _stop(res, "too_many_claims")
    if prior is not None and prior["state"] == "held":
        f = fence(prior["ref"], actor=actor, repos=repos)
        if f["ok"]:
            return _stop(res, "already_held", claim_ref=prior["ref"], claimed=True, winner=prior["alias"])
        prior = _rec_load(actor, owner, name, n)
    if prior is not None and prior["state"] == "claiming":
        ok, why = _withdraw(actor, prior)                       # an earlier call died between the post and the decision: take the unconfirmed comment back first
        if not ok:
            return _stop(res, "withdraw_failed")
    elif prior is not None and prior["state"] == "lost":
        _rec_drop(actor, owner, name, n)                        # the loss was handled when it happened; this is a new attempt
    # step 1: check, fresh
    d = fresh_claim_state(owner, name, n, repos, actor.login, detail=True)
    if d["issue_state"] == "closed":
        return _stop(res, "closed")
    if d["state"] == "unknown":
        return _stop(res, "claim_unknown", unknown_reason=d["reason"])
    if _sweep_orphans(actor, owner, name, n, d):
        d = fresh_claim_state(owner, name, n, repos, actor.login, detail=True)           # the orphans were this board's own live claims: read again without them
        if d["issue_state"] == "closed":
            return _stop(res, "closed")
        if d["state"] == "unknown":
            return _stop(res, "claim_unknown", unknown_reason=d["reason"])
    claims = d.get("claims") or []
    if any(c.get("live") is None for c in claims):
        return _stop(res, "claim_unknown", unknown_reason="clock_unknown")
    live = _winner(claims)
    if live is not None:
        return _stop(res, "taken", winner=live["alias"])
    newest = claims[-1] if claims else None
    if newest is not None and newest["status"] == "done":
        return _stop(res, "done", winner=newest["alias"])
    push = _may_push(owner, name)                               # before the first POST: a comment from a login that cannot push never counts, so none is posted
    if push is not True:
        why = "no_access" if push is False else "error"
        _audit(actor, "epic-claim", False, owner, name, n, why if push is False else "access_unknown")
        return _stop(res, why, **({} if push is False else {"unknown_reason": "access_unknown"}))
    attempt = max([c["attempt"] for c in claims] + [0]) + 1
    if attempt > MAX_ATTEMPTS:
        asked = _ask_person(actor, owner, name, n, d.get("comments"))
        _audit(actor, "epic-claim", False, owner, name, n, "attempts_exhausted")
        return _stop(res, "attempts_exhausted", asked_person=asked, attempt=attempt - 1)
    reclaims = newest["id"] if newest is not None else None
    try:
        text = github_write.claim_body(alias, nid, settings.epic_lease_min, 1, "claimed", attempt, reclaims=reclaims, task=task_ref, branch=branch)
    except ValueError:
        return _stop(res, "bad_request")
    # step 2: post. A marker first, so a crash between the POST and the record leaves a trace the next call can sweep.
    before_ids = {c.get("id") for c in d.get("comments") or [] if isinstance(c, dict)}
    actor.db.kv_set(_pending_key(owner, name, n), {"nid": nid, "attempt": attempt, "seq": 1})
    try:
        _, _, data = _send(actor, github_write.comment_create(owner, name, n, text))
        raw = data if isinstance(data, dict) and isinstance(data.get("id"), int) and not isinstance(data.get("id"), bool) else None
        if raw is None:
            raise github_write.GhWriteError("bad_answer", unknown=True)
    except github_write.GhWriteError as e:
        if not e.unknown:
            actor.db.kv_del(_pending_key(owner, name, n))
            _audit(actor, "epic-claim", False, owner, name, n, e.reason)
            return _stop(res, _err_reason(e), wait_s=e.wait_s)
        found, raw = _readback(actor, owner, name, n, repos, before_ids, attempt)
        if raw is None:
            if found == "absent":
                actor.db.kv_del(_pending_key(owner, name, n))            # two whole readings show nothing was written: no half claim, nothing to sweep later
            _audit(actor, "epic-claim", False, owner, name, n, "error" if found == "absent" else "outcome_unknown")
            return _stop(res, "error" if found == "absent" else "outcome_unknown", unknown_outcome=found != "absent", status_code=e.status)
    actor.db.kv_del(_pending_key(owner, name, n))
    rec = {"v": 1, "owner": owner, "repo": name, "number": n, "comment_id": raw["id"], "nid": nid, "alias": alias, "login": actor.login, "lease_min": settings.epic_lease_min,
           "seq": 1, "attempt": attempt, "status": "claimed", "state": "claiming", "added_assignee": False, "added_label": False, "reclaims": reclaims, "task_ref": task_ref,
           "branch": branch, "task_id": None, "renewals": 0, "notes": [], "created_at": raw.get("created_at") if isinstance(raw.get("created_at"), str) else None}
    _rec_save(actor, rec)
    rec["ref"] = make_ref(owner, name, n, rec["comment_id"])
    # step 3: decide
    verdict, winner, dd = _decide(actor, rec, repos)
    if verdict != "won":
        ok, why = _withdraw(actor, rec, (dd or {}).get("comments") if dd and dd.get("state") != "unknown" else None)
        _audit(actor, "epic-claim", False, owner, name, n, verdict if ok else "withdraw_failed")
        if not ok:
            return _stop(res, "withdraw_failed", winner=winner)
        if verdict == "error":
            return _stop(res, "claim_unknown", unknown_reason=(dd or {}).get("reason") or "not_settled")
        return _stop(res, verdict, winner=winner)
    rec = dict(rec, state="held")
    _rec_save(actor, rec)
    # step 4: mark, then hold
    rec = _mark(actor, rec, d.get("issue") or {}, reclaiming=newest is not None)
    rec["ref"] = make_ref(owner, name, n, rec["comment_id"])
    _rec_save(actor, rec)
    _audit(actor, "epic-claim", True, owner, name, n, f"attempt {attempt}")
    _ensure_renewer(actor)
    res.update(claimed=True, claim_ref=rec["ref"], winner=alias, attempt=attempt, notes=rec["notes"], reclaims=reclaims)
    return _stop(res, "won", claimed=True)


def _held(actor: Actor, claim_ref) -> tuple[dict | None, tuple | None]:
    ref = parse_ref(claim_ref)
    if ref is None:
        return None, None
    rec = _rec_load(actor, ref[0], ref[1], ref[2])
    return (rec if rec and rec["comment_id"] == ref[3] else None), ref


def renew(claim_ref, *, actor: Actor | None = None, repos=None, status: str | None = None, task_ref: str | None = None, branch: str | None = None, bump: bool = True,
          stop: threading.Event | None = None) -> dict:
    """Step 5: the heartbeat, and the way a status or a task is written into the claim. Fenced: the claim must still be the board's, live and first (otherwise nothing is
    written; a definite loss is recorded). Edits the board's OWN comment, keeping its marker and nid, with seq + 1 (`bump=False` keeps seq: the status change to running), so GitHub
    moves updated_at. Returns {renewed, reason, seq, lost}. `stop` is the renew timer's stop flag: once set, the database is not touched again."""
    out = {"renewed": False, "reason": None, "seq": None, "lost": False}
    if stop is not None and stop.is_set():
        return {**out, "reason": "stopped"}                                      # before anything: not a database read, not a request
    if status is not None and status not in ("claimed", "running"):
        return {**out, "reason": "bad_request"}
    if not github_write.writes_enabled():
        return {**out, "reason": "writes_off"}
    actor = actor or board_actor()
    rec, ref = _held(actor, claim_ref)
    if ref is None or rec is None or rec["state"] != "held":
        return {**out, "reason": "unknown_claim" if rec is None else ("lost" if rec["state"] == "lost" else "not_held"), "lost": bool(rec and rec["state"] == "lost")}
    with _lock_for(actor, rec["owner"], rec["repo"], rec["number"]):
        rec = _rec_load(actor, rec["owner"], rec["repo"], rec["number"])
        if rec is None or rec["state"] != "held" or rec["comment_id"] != ref[3]:
            return {**out, "reason": "not_held"}
        if stop is not None and stop.is_set():
            return {**out, "reason": "stopped"}
        f = _fence(actor, rec, _repos(actor, repos))
        if f["lost"]:
            if stop is None or not stop.is_set():
                _mark_lost(actor, rec, f["reason"])
            return {**out, "reason": f["reason"], "lost": True}
        if not f["ok"]:
            return {**out, "reason": f["reason"] or "unknown"}
        cur = parse_claim(f["comment"].get("body"))                              # the comment as it is now: the seq GitHub holds, never a stale record's
        seq = max(rec["seq"], cur["seq"]) + (1 if bump else 0)
        new_status = status or cur["status"]
        try:
            text = github_write.claim_body(rec["alias"], rec["nid"], rec["lease_min"], seq, new_status, rec["attempt"], reclaims=rec.get("reclaims"),
                                           task=task_ref if task_ref is not None else rec.get("task_ref"), branch=branch if branch is not None else rec.get("branch"))
            _send(actor, github_write.comment_edit(rec["owner"], rec["repo"], rec["comment_id"], text))
        except ValueError:
            return {**out, "reason": "bad_request"}
        except github_write.GhWriteError as e:
            return {**out, "reason": _err_reason(e), "wait_s": e.wait_s, "unknown": e.unknown}
        if stop is not None and stop.is_set():
            return {**out, "renewed": True, "reason": "stopped", "seq": seq}         # sent; the record is left for the next start to correct from GitHub's copy
        rec = dict(rec, seq=seq, status=new_status, renewals=rec["renewals"] + (1 if bump else 0),
                   task_ref=task_ref if task_ref is not None else rec.get("task_ref"), branch=branch if branch is not None else rec.get("branch"))
        _rec_save(actor, rec)
        return {**out, "renewed": True, "seq": seq, "reason": "ok"}


def start(claim_ref, task_ref: str | None = None, branch: str | None = None, *, actor: Actor | None = None, repos=None) -> dict:
    """The claim's status becomes `running` (seq kept), with the task ref and branch when given: the run phase calls it when the task starts. Fenced like `renew`."""
    return renew(claim_ref, actor=actor, repos=repos, status="running", task_ref=task_ref, branch=branch, bump=False)


def release(claim_ref, status: str = "released", *, actor: Actor | None = None, repos=None) -> dict:
    """Step 6: done, failed, cancelled or released. Fenced once; then the comment's status is edited (seq + 1) and ONLY what this board added is taken back: the label if it
    added it, itself as assignee if it added itself. The record is forgotten afterwards. Returns {released, reason, cleanup: {label, assignee}}. A lost claim writes nothing."""
    out = {"released": False, "reason": None, "cleanup": {"label": None, "assignee": None}}
    if status not in ("done", "failed", "cancelled", "released"):
        return {**out, "reason": "bad_request"}
    if not github_write.writes_enabled():
        return {**out, "reason": "writes_off"}
    actor = actor or board_actor()
    rec, ref = _held(actor, claim_ref)
    if ref is None or rec is None:
        return {**out, "reason": "unknown_claim"}
    with _lock_for(actor, rec["owner"], rec["repo"], rec["number"]):
        rec = _rec_load(actor, rec["owner"], rec["repo"], rec["number"])
        if rec is None or rec["comment_id"] != ref[3]:
            return {**out, "reason": "unknown_claim"}
        if rec["state"] != "held":
            return {**out, "reason": "lost" if rec["state"] == "lost" else "not_held"}
        f = _fence(actor, rec, _repos(actor, repos))
        if f["lost"]:
            _mark_lost(actor, rec, f["reason"])
            return {**out, "reason": f["reason"], "lost": True}
        if not f["ok"]:
            return {**out, "reason": f["reason"] or "unknown"}
        cur = parse_claim(f["comment"].get("body"))
        try:
            text = github_write.claim_body(rec["alias"], rec["nid"], rec["lease_min"], max(rec["seq"], cur["seq"]) + 1, status, rec["attempt"], reclaims=rec.get("reclaims"),
                                           task=rec.get("task_ref"), branch=rec.get("branch"))
            _send(actor, github_write.comment_edit(rec["owner"], rec["repo"], rec["comment_id"], text))
        except ValueError:
            return {**out, "reason": "bad_request"}
        except github_write.GhWriteError as e:
            return {**out, "reason": _err_reason(e), "wait_s": e.wait_s, "unknown": e.unknown}
        if rec["added_label"]:
            try:
                _send(actor, github_write.label_remove(rec["owner"], rec["repo"], rec["number"]))
                out["cleanup"]["label"] = "removed"
            except github_write.GhWriteError as e:
                out["cleanup"]["label"] = "not_found" if e.reason == "not_found" else "failed"
        if rec["added_assignee"] and actor.login:
            try:
                _send(actor, github_write.assignee_remove(rec["owner"], rec["repo"], rec["number"], actor.login))
                out["cleanup"]["assignee"] = "removed"
            except github_write.GhWriteError as e:
                out["cleanup"]["assignee"] = "failed"
        _rec_drop(actor, rec["owner"], rec["repo"], rec["number"])
        _audit(actor, "epic-claim-release", True, rec["owner"], rec["repo"], rec["number"], status)
        return {**out, "released": True, "reason": "ok"}


def attach_task(claim_ref, task_id: int, *, actor: Actor | None = None) -> bool:
    """Remember which local task works the claim, so the lost hook can park it. Database only."""
    actor = actor or board_actor_lazy()
    if actor is None or isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
        return False
    rec, ref = _held(actor, claim_ref)
    if rec is None:
        return False
    with _lock_for(actor, rec["owner"], rec["repo"], rec["number"]):
        rec = _rec_load(actor, rec["owner"], rec["repo"], rec["number"])
        if rec is None:
            return False
        _rec_save(actor, dict(rec, task_id=task_id))
    return True


def drop_claim(claim_ref, *, actor: Actor | None = None) -> bool:
    """Forget a claim record without writing anything to GitHub (a lost claim once the task is parked, or a record nobody will renew). The comment, if any, stays and lapses."""
    actor = actor or board_actor_lazy()
    rec, ref = _held(actor, claim_ref) if actor is not None else (None, None)
    if rec is None:
        return False
    with _lock_for(actor, rec["owner"], rec["repo"], rec["number"]):
        _rec_drop(actor, rec["owner"], rec["repo"], rec["number"])
    return True


# ---- the renew timer: only while this board holds a claim, bounded, stopped on shutdown

def _renew_loop(actor: Actor, stop: threading.Event) -> None:
    while not stop.wait(RENEW_S):
        if stop.is_set():
            return
        live = [v for v in held_claims(actor) if v["state"] == "held"]
        if not live:
            break
        for v in live:
            if stop.is_set():
                return
            if v["renewals"] < RENEW_MAX:                         # bounded: a claim renewed for 24 h is left to lapse
                try:
                    renew(v["claim_ref"], actor=actor, stop=stop)
                except Exception as e:
                    log.warning("claim renew failed: %s", e.__class__.__name__)
            for hook in list(RENEW_HOOKS):                        # the work of a held claim (issue #149) is looked at on the same beat; only while a claim is held
                if stop.is_set():
                    return
                try:
                    hook(actor, v)
                except Exception as e:
                    log.warning("claim beat hook failed: %s", e.__class__.__name__)
    with _klock:
        cur = _renewers.get(id(actor.db))
        if cur is not None and cur[1] is stop:
            _renewers.pop(id(actor.db), None)


def _ensure_renewer(actor: Actor) -> None:
    """Start this board's renew thread if it is not running. Nothing starts at import or on a board that holds no claim."""
    if not RENEWER:
        return
    with _klock:
        cur = _renewers.get(id(actor.db))
        if cur is not None and cur[0].is_alive() and not cur[1].is_set():
            return
        stop = threading.Event()
        t = threading.Thread(target=_renew_loop, args=(actor, stop), name="ccboard-epic-renew", daemon=True)
        _renewers[id(actor.db)] = (t, stop)
        t.start()


def renewer_running() -> bool:
    with _klock:
        return any(t.is_alive() for t, _ in _renewers.values())


def stop_renewals(timeout: float = 30.0) -> None:
    """Shutdown: set every renew thread's stop flag and wait for it. After this returns no timer touches the database again."""
    with _klock:
        items = list(_renewers.values())
        _renewers.clear()
    for _, stop in items:
        stop.set()
    for t, _ in items:
        t.join(timeout)


def recover(actor: Actor | None = None, repos=None) -> list[dict]:
    """After a restart: take back the unconfirmed claims (state `claiming`: delete the board's own comment and forget it), start the renew timer when a claim is held, and forget
    lost records older than a week. Returns what it did, as claim_view plus `did`. Reads the database first and asks GitHub only for a record that needs it."""
    actor = actor or board_actor()
    did = []
    for key in _index(actor):
        m = re.fullmatch(CLAIM_KV + r"([^/]+)/([^#]+)#([0-9]+)", key)
        if not m:
            continue
        rec = _rec_load(actor, m.group(1), m.group(2), int(m.group(3)))
        if rec is None:
            continue
        if rec["state"] == "claiming" and github_write.writes_enabled():
            with _lock_for(actor, rec["owner"], rec["repo"], rec["number"]):
                ok, _ = _withdraw(actor, rec)
            did.append({**claim_view(rec), "did": "withdrawn" if ok else "withdraw_failed"})
        elif rec["state"] == "lost" and time.time() - float(rec.get("lost_at") or 0) > LOST_KEEP_S:
            _rec_drop(actor, rec["owner"], rec["repo"], rec["number"])
            did.append({**claim_view(rec), "did": "forgotten"})
        elif rec["state"] == "held":
            did.append({**claim_view(rec), "did": "held"})
    if any(d["did"] == "held" for d in did) and github_write.writes_enabled():
        _ensure_renewer(actor)
    return did


# ================================================================ start an epic from the board (issue #158)
#
# The owner types an epic (a title, a goal and up to 20 rows); the board shows a PREVIEW (the exact titles, labels and bodies and the number of calls; it reads one fact from GitHub, whether the
# repository is private, and writes nothing) and CREATE writes it: the labels (once), the parent, each child, each child attached to the parent as a sub-issue, then ONE edit of the parent's
# body that lists the children as `- [ ] #N` lines. Every write goes through github_write's queue (one at a time, at least 1.1 s apart) as a request of the closed list, behind CCBOARD_EPIC_WRITES.
#
# The person's text is DATA. It is cleaned (control, format and bidi characters dropped, titles on one line), refused when it names a bypass or holds a secret, a personal path, an email address or
# a host name, and put into the template with `<` and a line-leading `#` made harmless, so it can never become a heading of the template (the "Who should do it" block above all), a marker, a
# comment, a label, a flag or a command. The "Who should do it" block comes only from the tier table below, which is the table of CONTRIBUTING.md and never names a bypass.
#
# RESUME: a hidden line `<!-- ccboard:epic <key> -->` in the parent and `<!-- ccboard:epic-child <key>-<row> -->` in each child. A create looks FIRST at the issues with the two labels (by the
# gh login of this board, so a line a stranger typed means nothing), and only writes what is not there; so a retry after any failure (an unknown outcome included) never makes a duplicate.
# The key is made from the repository, the epic title and the child titles; a retry with the same ones is the same epic.

CHILD_LABEL = github_write.EPIC_CHILD_LABEL
CHILDREN_MAX = 20
TITLE_MAX = github_write.EPIC_TITLE_MAX
BODY_MAX = github_write.EPIC_BODY_MAX
TEXT_IN_MAX = 100_000                    # characters of one field the cleaner reads at all; more is refused first
SIZES = ("S", "M", "L")
LIST_MARK = "<!-- ccboard:epic-list -->"
# the tier table: CONTRIBUTING.md's, line for line (a test compares them). `why` and `cheaper` are the two fixed sentences of the block.
TIERS = {
    "A": {"claude": "claude --model opus --effort high --permission-mode acceptEdits",
          "codex": 'codex -m gpt-6.1-sol -c model_reasoning_effort="high" -s workspace-write -a on-request',
          "why": "open design, risky or cross-cutting work.", "cheaper": "mechanical parts may go to a cheaper tier."},
    "B": {"claude": "claude --model sonnet --effort medium --permission-mode acceptEdits",
          "codex": 'codex -m gpt-6.1-sol -c model_reasoning_effort="medium" -s workspace-write -a on-request',
          "why": "standard feature work: code, pages and the tests with them.", "cheaper": "text, docs and mechanical edits may go to tier C."},
    "C": {"claude": "claude --model haiku --effort low --permission-mode acceptEdits",
          "codex": 'codex -m gpt-6-luna -c model_reasoning_effort="low" -s workspace-write -a on-request',
          "why": "mechanical work with a clear recipe.", "cheaper": "none."},
    "check": {"claude": "claude --model sonnet --effort medium --permission-mode acceptEdits",
              "codex": "codex -m gpt-6.1-sol -s workspace-write -a on-request",
              "why": "a check or review of finished work, started on the machine that holds the branch.", "cheaper": "none."},
}
CHILD_HEADINGS = ("Goal", "Why / evidence", "Scope", "Files", "Tests", "Acceptance", "Who should do it", "Rules", "References")
PARENT_HEADINGS = ("Goal", "Issues", "How this epic runs")
PROBLEM_TEXT = {
    "bypass": "this text names a permission bypass, which the board never writes into an issue",
    "secret": "this text looks like a secret or a token",
    "email": "this text holds an email address",
    "path": "this text holds a personal path",
    "host": "this text holds a host name or an address",
}
_CHILD_END = re.compile(r"\n\n<!-- ccboard:epic-child ([0-9a-f]{12}-[0-9]{2}) -->\Z")
_PARENT_END = re.compile(r"\n\n<!-- ccboard:epic ([0-9a-f]{12}) -->\Z")
_LIST_RE = re.compile(re.escape(LIST_MARK) + r"\n((?:- \[ \] #[1-9][0-9]{0,8}\n)*)\n## How this epic runs\n")
_WHO_BLOCK = re.compile(r"^## Who should do it\n\n(.*?)\n\n## Rules\n", re.M | re.S)
_REFS_RE = re.compile(r"## References\n\n(- none yet|- depends on #[1-9][0-9]{0,8})\n\n<!-- ccboard:epic-child")
_LEAD_HASH = re.compile(r"^[ \t]*#")
_LEAD_DEPENDS = re.compile(r"^[ \t>*+-]*depends[ \t]+on", re.I)


class EpicInvalid(Exception):
    """The epic the person typed is refused, before anything is read or written. `errors` is a list of {field, index, message}: `field` is repo, title, goal, children, child_title,
    child_description, child_tier, child_size or depends_on; `index` is the row (0 is the first) for a child field, else None."""

    def __init__(self, errors: list):
        self.errors = errors
        super().__init__(errors[0]["message"] if errors else "invalid epic")


class EpicRefused(Exception):
    """Create does not start: `reason` is writes_off, no_login or in_progress; `status` is the HTTP code the route answers with."""

    def __init__(self, reason: str, text: str, status: int = 409):
        self.reason, self.text, self.status = reason, text, status
        super().__init__(text)


def who_block(tier: str) -> str:
    """The text under `## Who should do it` for a tier: the two commands of the table and the two fixed sentences."""
    t = TIERS[tier]
    return f"- Claude: `{t['claude']}`\n- Codex: `{t['codex']}`\n- Why this tier: {t['why']}\n- Cheaper parts: {t['cheaper']}"


def _drop_chars(s: str, multiline: bool) -> str:
    out = []
    for c in unicodedata.normalize("NFC", s.replace("\r\n", "\n").replace("\r", "\n")):
        cat = unicodedata.category(c)
        if c == "\n" or c in "  ":
            out.append("\n" if multiline else " ")
        elif c == "\t":
            out.append(" ")
        elif cat in ("Cc", "Cf", "Cs", "Co", "Cn") or c == "\x7f":
            continue
        else:
            out.append(c)
    return "".join(out)


def clean_title(s) -> str:
    """One plain line: control, format and bidi characters dropped, every run of white space one space, no space at either end."""
    return re.sub(r"\s+", " ", _drop_chars(s if isinstance(s, str) else "", False)).strip()


def clean_text(s) -> str:
    """Several lines of plain text: control, format and bidi characters dropped, no trailing space, at most one empty line in a row, none at either end."""
    lines = [ln.rstrip() for ln in _drop_chars(s if isinstance(s, str) else "", True).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")


def _esc(text: str) -> str:
    """The person's cleaned text made safe to stand inside the template. `<` becomes `&lt;` (no HTML comment, so no forged marker and no comment that hides the rest), a line that starts with
    `#` (a heading, "Who should do it" included) or with `depends on` gets a backslash, and a code fence is broken up; everything else stays as typed."""
    out = []
    for ln in text.replace("<", "&lt;").replace("```", "` ` `").replace("~~~", "~ ~ ~").split("\n"):
        out.append("\\" + ln if _LEAD_HASH.match(ln) or _LEAD_DEPENDS.match(ln) else ln)
    return "\n".join(out)


def epic_key(spec: dict) -> str:
    """12 hex characters made from the repository, the epic title and the child titles in order: the same epic typed again has the same key and so the same markers."""
    raw = "\n".join([spec["repo"].lower(), spec["title"]] + [c["title"] for c in spec["children"]])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def child_slug(key: str, row: int) -> str:
    return f"{key}-{row:02d}"


def parent_marker(key: str) -> str:
    return f"<!-- ccboard:epic {key} -->"


def child_marker(slug: str) -> str:
    return f"<!-- ccboard:epic-child {slug} -->"


def parent_body(spec: dict, key: str) -> str:
    """The parent's body as it is created: the goal, an empty list of issues and the hidden marker. The list is filled by `fill_list` in the one edit."""
    goal = _esc(spec["goal"] or spec["title"])
    return (f"## Goal\n\n{goal}\n\n## Issues\n\n{LIST_MARK}\n\n## How this epic runs\n\n"
            "The issues listed above are the sub-issues of this epic, in order. The board's Epics tab shows who works on which.\n\n" + parent_marker(key))


def fill_list(body: str, lines: list[str]) -> str | None:
    """`body` with the list between the list marker and the next heading replaced by `lines` (`- [ ] #N`), or None when the body has no such list (someone removed it)."""
    m = _LIST_RE.search(body)
    if not m:
        return None
    return body[:m.start()] + LIST_MARK + "\n" + "".join(ln + "\n" for ln in lines) + "\n## How this epic runs\n" + body[m.end():]


def list_lines(count: int, numbers: dict | None = None) -> list[str]:
    """The `- [ ] #N` lines of the parent. Without `numbers` (the preview) N is `[row k]`: GitHub gives the number when the issue is created."""
    return [f"- [ ] #{numbers[i]}" if numbers and i in numbers else f"- [ ] #[row {i}]" for i in range(1, count + 1)]


def child_body(spec: dict, row: int, key: str, numbers: dict | None = None) -> str:
    """The body of child `row` (1 is the first): the nine sections of the repository's issue template, the child's text in Goal, empty lists for the person to complete, the tier's block
    and, for a `depends on` row, one line with that row's issue number (`#[row k]` in the preview, the number GitHub gave it when written)."""
    c = spec["children"][row - 1]
    dep = c["depends_on"]
    if dep:
        refs = "- depends on " + (f"#{numbers[dep]}" if numbers and dep in numbers else f"#[row {dep}]")
    else:
        refs = "- none yet"
    return "\n".join([
        "## Goal", "", _esc(c["description"] or c["title"]), "", f"Size: {c['size']}", "",
        "## Why / evidence", "", "<!-- What was seen, where, and on which commit. -->", "",
        "## Scope", "", "- [ ]", "",
        "## Files", "", '<!-- "Read first" and "Change" lists. -->', "",
        "## Tests", "", "<!-- The tests to add or change, and the two suites staying green. -->", "",
        "## Acceptance", "", "- [ ]", "",
        "## Who should do it", "", who_block(c["tier"]), "",
        "## Rules", "", '- Keep emails, tokens, host names and personal paths out of anything you write here. Say "the box".',
        "- Run both test suites before opening the pull request. Work lands through a pull request.", "",
        "## References", "", refs, "",
        child_marker(child_slug(key, row))])


def valid_generated_body(body: str, kind: str) -> bool:
    """The structure check github_write.valid_epic_body calls: `body` has exactly the headings of the template for `kind` (the person's text cannot add one: a line that starts with `#` was given
    a backslash), exactly one hidden marker line at its end, no other `<!-- ccboard:` and, for a child, the "Who should do it" block of one of the four tiers, one References line and nothing
    that issues.parse_who warns about."""
    if not isinstance(body, str):
        return False
    text = issues._comment_free(body)
    heads = tuple(re.findall(r"^## (.+?)[ \t]*$", text, flags=re.M))
    if kind == "child":
        if heads != CHILD_HEADINGS or not _CHILD_END.search(body) or body.count("<!-- ccboard:") != 1 or not _REFS_RE.search(body):
            return False
        m = _WHO_BLOCK.search(text)                                   # the real heading at the start of a line, never the person's own `\## Who should do it`
        if not m or not any(m.group(1) == who_block(t) for t in TIERS):
            return False
        who = issues.parse_who(body)
        return not who["warnings"] and who["claude"] is not None and who["codex"] is not None
    if kind == "parent":
        return heads == PARENT_HEADINGS and bool(_PARENT_END.search(body)) and body.count("<!-- ccboard:") == 2 and bool(_LIST_RE.search(body))
    return False


# ---- the typed epic: cleaned, checked and numbered

def _problem(errors: list, field: str, index, text: str) -> None:
    why = github_write.epic_text_problem(text)
    if why:
        errors.append({"field": field, "index": index, "message": PROBLEM_TEXT[why]})


def normalize(raw, repo: str) -> dict:
    """The epic as typed -> the epic as it will be written: {repo, title, goal, children: [{title, description, tier, size, depends_on}]}. Raises EpicInvalid with every problem found
    (a message for each field, so the sheet can show it beside the field and keep every value). `repo` is `owner/name`, already one of this board's own."""
    errors: list = []
    if not isinstance(raw, dict):
        raise EpicInvalid([{"field": "children", "index": None, "message": "send the epic as an object"}])
    o, _, r = repo.partition("/")
    if not github.valid_slug(o, r):
        errors.append({"field": "repo", "index": None, "message": "that is not a GitHub repository"})

    def text_in(v, field, index, what):
        if not isinstance(v, str):
            errors.append({"field": field, "index": index, "message": f"{what} must be text"})
            return ""
        if len(v) > TEXT_IN_MAX:
            errors.append({"field": field, "index": index, "message": f"{what} is far too long (over {TEXT_IN_MAX:,} characters)"})
            return ""
        return v

    title = clean_title(text_in(raw.get("title", ""), "title", None, "the epic title"))
    if not title:
        errors.append({"field": "title", "index": None, "message": "give the epic a title"})
    elif len(title) > TITLE_MAX:
        errors.append({"field": "title", "index": None, "message": f"the title is {len(title)} characters; the most is {TITLE_MAX}"})
    else:
        _problem(errors, "title", None, title)
    goal = clean_text(text_in(raw.get("goal", ""), "goal", None, "the goal"))
    _problem(errors, "goal", None, goal)
    kids_in = raw.get("children")
    kids: list = []
    if not isinstance(kids_in, list) or not kids_in:
        errors.append({"field": "children", "index": None, "message": "add at least one issue"})
        kids_in = []
    elif len(kids_in) > CHILDREN_MAX:
        errors.append({"field": "children", "index": None, "message": f"an epic has at most {CHILDREN_MAX} issues; this one has {len(kids_in)}"})
        kids_in = kids_in[:CHILDREN_MAX]
    for i, k in enumerate(kids_in):
        if not isinstance(k, dict):
            errors.append({"field": "children", "index": i, "message": "this row is not an issue"})
            continue
        ct = clean_title(text_in(k.get("title", ""), "child_title", i, "the title"))
        if not ct:
            errors.append({"field": "child_title", "index": i, "message": "give this issue a title"})
        elif len(ct) > TITLE_MAX:
            errors.append({"field": "child_title", "index": i, "message": f"the title is {len(ct)} characters; the most is {TITLE_MAX}"})
        else:
            _problem(errors, "child_title", i, ct)
        desc = clean_text(text_in(k.get("description", ""), "child_description", i, "the description"))
        _problem(errors, "child_description", i, desc)
        tier = k.get("tier")
        if tier not in TIERS:
            errors.append({"field": "child_tier", "index": i, "message": "the tier is A, B, C or check"})
            tier = "B"
        size = k.get("size")
        if size not in SIZES:
            errors.append({"field": "child_size", "index": i, "message": "the size is S, M or L"})
            size = "M"
        dep = k.get("depends_on")
        if dep is not None and (isinstance(dep, bool) or not isinstance(dep, int) or not 1 <= dep <= i):
            errors.append({"field": "depends_on", "index": i, "message": "depends on must be the number of an earlier row"})
            dep = None
        kids.append({"title": ct, "description": desc, "tier": tier, "size": size, "depends_on": dep})
    spec = {"repo": repo, "title": title, "goal": goal, "children": kids}
    if not errors:                                                        # the length of the bodies, with the template around the text
        key = epic_key(spec)
        if len(parent_body(spec, key)) > BODY_MAX:
            errors.append({"field": "goal", "index": None, "message": f"the goal makes the issue longer than {BODY_MAX:,} characters; shorten it"})
        for i in range(1, len(kids) + 1):
            if len(child_body(spec, i, key, {j: 999_999_999 for j in range(1, i)})) > BODY_MAX:
                errors.append({"field": "child_description", "index": i - 1, "message": f"the description makes the issue longer than {BODY_MAX:,} characters; shorten it"})
    if errors:
        raise EpicInvalid(errors)
    return spec


def check_bodies(spec: dict, key: str) -> None:
    """Raise EpicInvalid when github_write would refuse a title or a body of this epic (`valid_epic_title`, `valid_epic_body`): Preview and Create both call it, so the sheet refuses, beside the
    field, what Create would refuse, and Create never dies in the middle of the writes. The numbers GitHub gives are stood in for by a number of full length."""
    k = len(spec["children"])
    nums = {j: 999_999_999 for j in range(1, k + 1)}
    errors: list = []
    pb = parent_body(spec, key)
    if not github_write.valid_epic_title(spec["title"]):
        errors.append({"field": "title", "index": None, "message": "the board cannot write this title into an issue; change it"})
    if not (github_write.valid_epic_body(pb, "parent") and github_write.valid_epic_body(fill_list(pb, list_lines(k, nums)) or "", "parent")):
        errors.append({"field": "goal", "index": None, "message": "the board cannot write this text into the epic issue; change it"})
    for i, c in enumerate(spec["children"], 1):
        if not github_write.valid_epic_title(c["title"]):
            errors.append({"field": "child_title", "index": i - 1, "message": "the board cannot write this title into an issue; change it"})
        if not github_write.valid_epic_body(child_body(spec, i, key, nums), "child"):
            errors.append({"field": "child_description", "index": i - 1, "message": "the board cannot write this text into an issue; change it"})
    if errors:
        raise EpicInvalid(errors)


def est_seconds(writes: int) -> float:
    """About how long `writes` writes take on the queue: a gap of MIN_GAP between two, and never more than CEILING in any CEILING_WINDOW seconds (30 a minute)."""
    t: list[float] = []
    for _ in range(writes):
        at = (t[-1] + github_write.MIN_GAP) if t else 0.0
        if len(t) >= github_write.CEILING:
            at = max(at, t[-github_write.CEILING] + github_write.CEILING_WINDOW)
        t.append(at)
    return round(t[-1], 1) if t else 0.0


def writes_count(k: int) -> int:
    """The most writes a create makes for k children: the two labels, the parent, k children, k attachments and the one edit of the parent."""
    return 2 + 1 + k + k + 1


def preview(raw, repo: str, private=None) -> dict:
    """What Create will write; nothing is written (the caller read `private` from GitHub, or passes None). Every text of the answer is what the queue sends, except that a `depends on`
    line of a child and the list of the parent say `#[row k]` where GitHub's number for row k goes in.
    {repo, private, writes_on, key, calls: {writes, labels, issues, attachments, edits, min_gap_s, min_seconds, est_seconds}, parent: {title, labels, body, edit_body, marker},
    children: [{index, row, slug, title, labels, body, marker, tier, size, depends_on, claude, codex}], limits: {children, title, body}}"""
    spec = normalize(raw, repo)
    key = epic_key(spec)
    check_bodies(spec, key)
    k = len(spec["children"])
    pb = parent_body(spec, key)
    n = writes_count(k)
    return {
        "repo": repo, "private": private, "writes_on": github_write.writes_enabled(), "key": key,
        "calls": {"writes": n, "labels": 2, "issues": 1 + k, "attachments": k, "edits": 1, "min_gap_s": github_write.MIN_GAP, "min_seconds": round((n - 1) * github_write.MIN_GAP, 1), "est_seconds": est_seconds(n)},
        "parent": {"title": spec["title"], "labels": [EPIC_LABEL], "body": pb, "edit_body": fill_list(pb, list_lines(k)), "marker": parent_marker(key)},
        "children": [{"index": i - 1, "row": i, "slug": child_slug(key, i), "title": c["title"], "labels": [CHILD_LABEL], "body": child_body(spec, i, key), "marker": child_marker(child_slug(key, i)),
                      "tier": c["tier"], "size": c["size"], "depends_on": c["depends_on"], "claude": TIERS[c["tier"]]["claude"], "codex": TIERS[c["tier"]]["codex"]}
                     for i, c in enumerate(spec["children"], 1)],
        "limits": {"children": CHILDREN_MAX, "title": TITLE_MAX, "body": BODY_MAX},
    }


# ---- create

class _ReadFail(Exception):
    def __init__(self, reason: str):
        self.reason = reason if reason in github.MESSAGES else "unavailable"
        super().__init__(self.reason)


class _StepFail(Exception):
    def __init__(self, step: str, reason: str, text: str, unknown: bool = False):
        self.step, self.reason, self.text, self.unknown = step, reason, text, unknown
        super().__init__(text)


_STATUS_REASON = {401: "no_login", 403: "no_access", 404: "not_found", 429: "rate_limited"}
_running: set[str] = set()
_running_lock = threading.Lock()


def _get(path: str):
    try:
        status, _headers, data = github.api(path)
    except github.GhReadError as e:
        raise _ReadFail(e.reason) from None
    return status, data


def _read_labelled(o: str, r: str, label: str) -> list[dict]:
    """The issues (all states, newest first, pull requests left out) that carry `label`, at most MAX_PAGES pages."""
    out: list[dict] = []
    for page in range(1, github.MAX_PAGES + 1):
        status, data = _get(github.labeled_path(o, r, label, "all", page, github.PER_PAGE))
        if status != 200 or not isinstance(data, list):
            raise _ReadFail(_STATUS_REASON.get(status, "unavailable"))
        out += [x for x in data if isinstance(x, dict) and "pull_request" not in x]
        if len(data) < github.PER_PAGE:
            break
    return out


def _read_attached(o: str, r: str, n: int) -> set[int] | None:
    """The numbers attached to the parent as sub-issues; None when GitHub has no sub-issues endpoint (404) so nothing is known."""
    out: set[int] = set()
    for page in range(1, github.MAX_PAGES + 1):
        status, data = _get(github.sub_issues_path(o, r, n, page, github.PER_PAGE))
        if status == 404:
            return None
        if status != 200 or not isinstance(data, list):
            raise _ReadFail(_STATUS_REASON.get(status, "unavailable"))
        out |= {x["number"] for x in data if isinstance(x, dict) and github.valid_number(x.get("number"))}
        if len(data) < github.PER_PAGE:
            break
    return out


def _own_issue(item: dict, login: str) -> bool:
    u = item.get("user")
    return isinstance(u, dict) and isinstance(u.get("login"), str) and u["login"].lower() == login.lower() and github.valid_number(item.get("number")) and isinstance(item.get("body"), str)


def _lines(body: str) -> list[str]:
    return [ln.rstrip() for ln in body.replace("\r\n", "\n").split("\n")]


def _find(items: list[dict], login: str, markers: list) -> dict:
    """{marker: the issue} for the board's own issues whose body holds the marker as a line of its own; the lowest number when there are several (an earlier mistake)."""
    out: dict = {}
    for it in sorted((x for x in items if _own_issue(x, login)), key=lambda x: x["number"]):
        have = set(_lines(it["body"]))
        for m in markers:
            if m in have and m not in out:
                out[m] = it
    return out


_LIST_LINES = re.compile(re.escape(LIST_MARK) + r"\n(?:- \[[ xX]\] #[0-9]+\n)*")
_DEP_LINE = re.compile(r"(?m)^(- depends on )#(?:[0-9]+|\[row [0-9]+\])$")


def _text_of(body: str, *, parent: bool = False, mask_dep: bool = False) -> str:
    """A body made comparable: line breaks as `\n`, no space at the end of a line or of the body. Of a parent the list of issues is left out (the board fills it in, and a person ticks it);
    of a child, when `mask_dep`, the number in the `depends on` line (it is not known yet)."""
    text = "\n".join(_lines(body)).strip()
    if parent:
        text = _LIST_LINES.sub("", text)
    if mask_dep:
        text = _DEP_LINE.sub(r"\1#N", text)
    return text


def _audit_row(db, user, action: str, ok: bool, o: str, r: str, n, detail: str) -> None:
    nodes.audit("out", "", action, ok, detail, target=f"{o}/{r}#{int(n)}" if n else f"{o}/{r}", status="ok" if ok else "failed", user=user, db=db)


def create(raw, *, repo: str, db, user: str | None, wq=None) -> dict:
    """Write the epic. Raises EpicRefused (writes_off, no_login, in_progress: nothing was read or written) or EpicInvalid (nothing was read or written). Otherwise answers
    {ok, repo, key, resumed, writes, parent: {number, url, state} | None, children: [{index, row, slug, number, url, state, attached}], done, notes,
     error, reason, unknown, step}  where state is created or existing; on a failure `ok` is false and `step` says where it stopped (read, label, parent, child 3, attach 3, edit)."""
    if not github_write.writes_enabled():
        raise EpicRefused("writes_off", github_write.MESSAGES["writes_off"] + ". Preview still works and writes nothing.")
    spec = normalize(raw, repo)
    check_bodies(spec, epic_key(spec))
    o, _, r = repo.partition("/")
    login = _gh_login()
    if not login:
        raise EpicRefused("no_login", github_write.MESSAGES["no_login"])
    key = epic_key(spec)
    with _running_lock:
        if key in _running:
            raise EpicRefused("in_progress", "this epic is being created right now; wait for it to finish")
        _running.add(key)
    try:
        return _create(spec, key, o, r, login, db, user, wq)
    finally:
        with _running_lock:
            _running.discard(key)


def _create(spec: dict, key: str, o: str, r: str, login: str, db, user, wq) -> dict:
    kids = spec["children"]
    k = len(kids)
    slugs = [child_slug(key, i) for i in range(1, k + 1)]
    res: dict = {"ok": False, "repo": f"{o}/{r}", "key": key, "resumed": False, "writes": 0, "parent": None, "children": [], "done": False, "notes": [],
                 "error": None, "reason": None, "unknown": False, "step": None}
    numbers: dict[int, int] = {}
    ids: dict[int, int] = {}
    kid_state: dict[int, str] = {}
    attached_now: dict[int, bool] = {}
    reg_numbers: set[int] = set()                 # the issues this run created or adopted: the only ones an attach or an edit may name (the request carries them)
    reg_ids: set[int] = set()

    def send(step: str, build, *args, **kw):
        """Build the request and send it. The request is built HERE, inside the try: a builder that refuses (GhWriteError) is a failed step like a write that fails, so the answer says which
        step it was and lists what exists."""
        try:
            req = build(*args, **kw)
            _s, _h, data = github_write.send(req, wq)
        except github_write.GhWriteError as e:
            raise _StepFail(step, e.reason, e.message, e.unknown) from None
        res["writes"] += 1
        return data

    try:
        try:
            status, repo_info = _get(github.repo_path(o, r))
            if status != 200:
                raise _ReadFail(_STATUS_REASON.get(status, "unavailable"))
            if isinstance(repo_info, dict) and isinstance(repo_info.get("permissions"), dict) and repo_info["permissions"].get("push") is False:
                # GitHub drops the labels of an issue made by a login that cannot push, and an unlabelled issue could not be found again: no duplicate-proof resume, so nothing is written
                raise _StepFail("read", "no_access", "this gh login cannot push to that repository, so the board cannot label the issues it would create there")
            epic_items = _read_labelled(o, r, EPIC_LABEL)
            child_items = _read_labelled(o, r, CHILD_LABEL)
            p_hit = _find(epic_items, login, [parent_marker(key)]).get(parent_marker(key))
            c_hits = _find(child_items, login, [child_marker(s) for s in slugs])
            attached = _read_attached(o, r, p_hit["number"]) if p_hit else set()
        except _ReadFail as e:
            raise _StepFail("read", e.reason, github.MESSAGES[e.reason]) from None
        res["resumed"] = bool(p_hit or c_hits)
        # an issue found by its marker is adopted only when its text is what would be written now (the marker holds the repository, the epic title and the child titles; the rest could differ)
        known = {i: int(c_hits[child_marker(slugs[i - 1])]["number"]) for i in range(1, k + 1) if child_marker(slugs[i - 1]) in c_hits}
        differ: list[str] = []
        if p_hit and _text_of(p_hit["body"], parent=True) != _text_of(parent_body(spec, key), parent=True):
            differ.append(f"The epic issue (#{int(p_hit['number'])}) is already on GitHub with different text: edit it there or start a new epic.")
        for i in sorted(known):
            hit = c_hits[child_marker(slugs[i - 1])]
            dep = kids[i - 1]["depends_on"]
            mask = bool(dep) and dep not in known
            if _text_of(hit["body"], mask_dep=mask) != _text_of(child_body(spec, i, key, known), mask_dep=mask):
                differ.append(f"Issue {i} (#{known[i]}) is already on GitHub with different text: edit it there or start a new epic.")
        if differ:
            if p_hit:
                res["parent"] = {"number": int(p_hit["number"]), "url": issue_url(o, r, int(p_hit["number"])), "state": "existing"}
            numbers.update(known)
            kid_state.update({i: "existing" for i in known})
            res["notes"] += differ
            raise _StepFail("read", "differs", f"{len(differ)} of the issues from an earlier try {'is' if len(differ) == 1 else 'are'} already on GitHub with different text, so the board wrote nothing more")
        # the labels: only when no issue carries them yet; a 422 on the fixed definition means it exists already
        for name, known in ((EPIC_LABEL, bool(epic_items)), (CHILD_LABEL, bool(child_items))):
            if known:
                continue
            try:
                send("label", github_write.epic_label_create, o, r, name)
            except _StepFail as e:
                if e.reason != "invalid":
                    _audit_row(db, user, "epic_label", False, o, r, None, e.reason)
                    raise
            else:
                _audit_row(db, user, "epic_label", True, o, r, None, name)
        # the parent
        pbody = parent_body(spec, key)
        if p_hit:
            p_num, p_body = int(p_hit["number"]), p_hit["body"].replace("\r\n", "\n")          # GitHub's web editor saves line breaks as CRLF
            res["parent"] = {"number": p_num, "url": issue_url(o, r, p_num), "state": "existing"}
            reg_numbers.add(p_num)
        else:
            try:
                data = send("parent", github_write.epic_issue_create, o, r, spec["title"], pbody, parent=True)
            except _StepFail as e:
                _audit_row(db, user, "epic_issue_create", False, o, r, None, "parent " + e.reason)
                raise
            p_num = data.get("number") if isinstance(data, dict) else None
            if not github.valid_number(p_num):
                raise _StepFail("parent", "bad_answer", github_write.MESSAGES["bad_answer"] + "; look for the epic by its label on GitHub, then press Create again")
            p_num, p_body = int(p_num), pbody
            reg_numbers.add(p_num)
            res["parent"] = {"number": p_num, "url": issue_url(o, r, p_num), "state": "created"}
            _audit_row(db, user, "epic_issue_create", True, o, r, p_num, "parent")
            log.info("epic issue created %s/%s#%d (parent)", o, r, p_num)
        # the children, in order
        for i in range(1, k + 1):
            hit = c_hits.get(child_marker(slugs[i - 1]))
            if hit:
                numbers[i], kid_state[i] = int(hit["number"]), "existing"
                if isinstance(hit.get("id"), int) and not isinstance(hit.get("id"), bool):
                    ids[i] = hit["id"]
                    reg_ids.add(hit["id"])
                reg_numbers.add(numbers[i])
                continue
            dep = kids[i - 1]["depends_on"]
            if dep and dep not in numbers:
                raise _StepFail(f"child {i}", "bad_request", github_write.MESSAGES["bad_request"])
            body = child_body(spec, i, key, numbers)
            try:
                data = send(f"child {i}", github_write.epic_issue_create, o, r, kids[i - 1]["title"], body, parent=False)
            except _StepFail as e:
                _audit_row(db, user, "epic_issue_create", False, o, r, None, f"child {i} {e.reason}")
                raise
            n = data.get("number") if isinstance(data, dict) else None
            if not github.valid_number(n):
                raise _StepFail(f"child {i}", "bad_answer", github_write.MESSAGES["bad_answer"] + "; look for the issue by its label on GitHub, then press Create again")
            numbers[i], kid_state[i] = int(n), "created"
            reg_numbers.add(numbers[i])
            if isinstance(data.get("id"), int) and not isinstance(data.get("id"), bool):
                ids[i] = data["id"]
                reg_ids.add(data["id"])
            _audit_row(db, user, "epic_issue_create", True, o, r, n, f"child {i}")
            log.info("epic issue created %s/%s#%d (child %d)", o, r, int(n), i)
        # the attachments
        for i in range(1, k + 1):
            if attached is not None and numbers[i] in attached:
                attached_now[i] = True
                continue
            if i not in ids:
                raise _StepFail(f"attach {i}", "bad_answer", github_write.MESSAGES["bad_answer"])
            try:
                send(f"attach {i}", github_write.epic_sub_issue_attach, o, r, p_num, ids[i], numbers=frozenset(reg_numbers), ids=frozenset(reg_ids))
            except _StepFail as e:
                _audit_row(db, user, "epic_attach", False, o, r, numbers[i], f"child {i} {e.reason}")
                raise
            attached_now[i] = True
            _audit_row(db, user, "epic_attach", True, o, r, numbers[i], f"child {i}")
        # the one edit of the parent
        new_body = fill_list(p_body, list_lines(k, numbers))
        if new_body is None:
            res["notes"].append("the parent's list of issues was removed on GitHub, so the board did not fill it in")
        elif new_body != p_body:
            try:
                send("edit", github_write.epic_issue_edit, o, r, p_num, new_body, numbers=frozenset(reg_numbers))
            except _StepFail as e:
                _audit_row(db, user, "epic_edit", False, o, r, p_num, e.reason)
                raise
            _audit_row(db, user, "epic_edit", True, o, r, p_num, "list")
        res["ok"], res["done"] = True, True
    except _StepFail as e:
        res.update(error=e.text, reason=e.reason, unknown=e.unknown, step=e.step)
        if e.unknown:
            res["notes"].append("GitHub may have applied the last write; press Create again and the board looks before it writes")
    res["children"] = [{"index": i - 1, "row": i, "slug": slugs[i - 1], "number": numbers[i], "url": issue_url(o, r, numbers[i]), "state": kid_state[i], "attached": attached_now.get(i, False)}
                       for i in range(1, k + 1) if i in numbers]
    return res
