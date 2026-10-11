"""The epic view, server side (nodes epic, issue #147): list the epics of the repos this board knows, show one epic's issues with the node that holds each,
the live phase of its task and its pull request, and keep the owner's pins and pause. Read only towards GitHub; nothing here claims, runs or comments.

What an epic is (the decision of issue #146): a parent issue labelled `ccboard:epic` whose sub-issues are the children (REST .../issues/{n}/sub_issues, in the
queue order GitHub keeps). When that endpoint is not there (404) the children are the issues labelled `epic:<n>` in the same repository, in issue-number order,
and the answer says so (`source: labels`). Other labels the board reads: `ccboard:in-progress` (a child some node holds), `ccboard:node:<alias>` (a pin) and
`ccboard:ready`. A child's body may hold `depends on #N` or `depends on owner/name#N`; the child waits until N is closed.

GitHub is read through app/github.py (`gh api` GET only, ETag, a 60 s cache, a back-off kept in memory) and only when a person (or the hook token) asks:
nothing here starts a thread or a timer, so a board with no epic label anywhere and no paired node does nothing in the background.

EVERYTHING GitHub returns is untrusted text written by other people. Lists are capped (100 children, 100 comments per issue, 25 repositories), only whitelisted
fields are kept, strings go through github.clean_text, links are built from a validated owner, repository and number (never copied from the payload) and the
page renders all of it with textContent.

A claim comment (`<!-- ccboard:claim v1 -->`, written by the claim phase, issue #148) counts only when its author is the repository owner or an allowed login
(CCBOARD_EPIC_AUTHORS; empty means the login gh uses on this board). A claim-shaped comment from anyone else is counted as `claim_rejected` and never shown
as a claim. A claim is live when its status is claimed or running and GitHub's own clock (the Date header of the comments answer) is less than the claim's
lease after the comment's updated_at; the local clock decides nothing.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

from . import github, node_state, nodes, projects
from .config import settings

log = logging.getLogger("ccboard.nodes.epics")

EPIC_LABEL = "ccboard:epic"
IN_PROGRESS_LABEL = "ccboard:in-progress"
READY_LABEL = "ccboard:ready"
NODE_LABEL = "ccboard:node:"
FALLBACK_LABEL = "epic:"                 # + the parent's number, on each child, when the sub-issues endpoint is not there
CHILD_CAP = 100
COMMENT_CAP = 100
REPO_CAP = 25
EPIC_CAP = 50                            # epics listed at most
LABEL_CAP = 30
DEPENDS_CAP = 20
PINS_CAP = 200
GOOD_MAX = 32                            # last good answers kept in memory
LIST_TTL = 60.0
ISSUE_TTL = 60.0
COMMENTS_TTL = 30.0                      # an open child's comments: the claim may change
COMMENTS_TTL_CLOSED = 600.0              # a closed child's: its claim history is settled
COMMENT_CALLS = 40                       # gh calls for comments one refresh may make; the rest wait for the next one (answer.partial)
DEADLINE = 30.0                          # seconds one refresh may take
STALE_NOTE = "not from an allowed author"
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
    with _lock:
        _last_good.clear()
        _list_good.clear()
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
            "branch": f["branch"] if _BRANCH.fullmatch(f.get("branch", "")) else None}


def parse_claims(comments) -> list[dict]:
    """The claims in a list of GitHub comment objects, oldest first by (created_at, id): {id, author, created_at, updated_at, alias, status, ...}. Comments
    without the marker, with another version or without a usable id and timestamps are ignored; `author` is the login (validated) or None. No author check
    happens here: see `pick_claim`."""
    out = []
    for c in comments[:COMMENT_CAP] if isinstance(comments, list) else []:
        if not isinstance(c, dict):
            continue
        cid, created, updated = c.get("id"), github.clean_ts(c.get("created_at")), github.clean_ts(c.get("updated_at"))
        if isinstance(cid, bool) or not isinstance(cid, int) or cid <= 0 or not created or not updated:
            continue
        claim = parse_claim(c.get("body"))
        if claim is None:
            continue
        user = c.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        claim.update(id=cid, created_at=created, updated_at=updated, author=login if isinstance(login, str) and github.OWNER_RE.fullmatch(login) else None)
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


def allowed_authors(owner: str, gh_login: str | None, configured=None) -> set[str]:
    """The logins whose claim comments count on a repository of `owner`: the owner, and CCBOARD_EPIC_AUTHORS (empty: the login gh uses on this board)."""
    configured = settings.epic_authors if configured is None else configured
    allowed = {owner.lower()}
    if configured:
        allowed |= {a.lower() for a in configured}
    elif gh_login:
        allowed.add(gh_login.lower())
    return allowed


def pick_claim(claims: list[dict], allowed: set[str], server_now: float | None) -> tuple[dict | None, int]:
    """(the claim to show, how many claim comments were refused). The first live claim by (created_at, id) among the allowed authors', else the newest one of
    theirs (not live); a claim-shaped comment of anyone else is only counted."""
    ok = [c for c in claims if (c.get("author") or "").lower() in allowed]
    rejected = len(claims) - len(ok)
    if not ok:
        return None, rejected
    for c in ok:
        c["live"] = is_live(c, server_now)
    live = [c for c in ok if c["live"]]
    return (live[0] if live else ok[-1]), rejected


# ---------------------------------------------------------------- children

def _repo_of(item: dict, owner: str, repo: str) -> tuple[str, str]:
    u = item.get("repository_url")
    m = _REPO_URL.fullmatch(u) if isinstance(u, str) and len(u) < 300 else None
    return (m.group(1), m.group(2)) if m and github.valid_slug(m.group(1), m.group(2)) else (owner, repo)


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


def shape_child(item, owner: str, repo: str, index: int) -> dict | None:
    """One issue of a list as a child, whitelisted: None for a pull request or anything without a valid number. Nothing of the payload's URLs is kept."""
    if not isinstance(item, dict) or "pull_request" in item:
        return None
    num = item.get("number")
    if isinstance(num, bool) or not isinstance(num, int) or not github.valid_number(num):
        return None
    o, r = _repo_of(item, owner, repo)
    labels = [github.clean_text(lb.get("name"), 50) for lb in (item.get("labels") or [])[:LABEL_CAP] if isinstance(lb, dict)]
    labels = [lb for lb in labels if lb]
    cnt = item.get("comments")
    return {"repo": f"{o}/{r}", "owner": o, "name": r, "number": num, "title": github.clean_text(item.get("title"), 200) or f"Issue {num}",
            "state": "closed" if item.get("state") == "closed" else "open", "url": issue_url(o, r, num), "labels": labels,
            "depends_on_refs": parse_depends(item.get("body"), o, r, num), "order": index,
            "comments": cnt if isinstance(cnt, int) and not isinstance(cnt, bool) and cnt >= 0 else None,
            "updated_at": github.clean_ts(item.get("updated_at")), "claims": [], "server_now": None, "claims_read": False}


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


def _paged(builder, cap: int, ttl: float) -> tuple[list, bool, float | None, bool]:
    """(items, truncated, GitHub's clock, any page stale): pages 1.. of builder(page) until a short page, `cap` items or MAX_PAGES. Never follows a link."""
    items: list = []
    srv: float | None = None
    stale = truncated = False
    per = github.PER_PAGE
    for page in range(1, github.MAX_PAGES + 1):
        rep = github.reader.get(builder(page), ttl)
        data = rep.data
        if not isinstance(data, list):
            raise github.GhReadError("bad_answer")
        if rep.server_now is not None:
            srv = rep.server_now if srv is None else max(srv, rep.server_now)
        stale = stale or rep.stale
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
    return items, truncated, srv, stale


def _read_epic(owner: str, repo: str, n: int) -> dict:
    """The GitHub part of an epic's answer: the parent, its children (with the claim comments of each) and what was cut. Raises GhReadError when the parent
    cannot be read; a failure while reading comments ends that part early (`partial_error`) and keeps what was read."""
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
                     "updated_at": github.clean_ts(issue.get("updated_at"))},
            "source": "sub_issues", "note": None, "truncated": False, "partial": False, "comments_pending": 0, "comments_failed": 0, "partial_error": None,
            "server_now": head.server_now, "stale": head.stale}
    try:
        items, trunc, srv, stale = _paged(lambda p: github.sub_issues_path(owner, repo, n, p), CHILD_CAP, ISSUE_TTL)
    except github.GhReadError as e:
        if e.reason != "not_found":
            raise
        part["source"] = "labels"
        part["note"] = f"This repository has no sub-issues endpoint, so the children are the issues labelled {FALLBACK_LABEL}{n}."
        items, trunc, srv, stale = _paged(lambda p: github.labeled_path(owner, repo, f"{FALLBACK_LABEL}{n}", "all", p), CHILD_CAP, ISSUE_TTL)
        items = sorted((i for i in items if isinstance(i, dict) and isinstance(i.get("number"), int)), key=lambda i: i["number"])
    children = []
    for item in items:
        c = shape_child(item, owner, repo, len(children) + 1)
        if c is not None and c["number"] != n:
            children.append(c)
    _link_children(children)
    part.update(children=children, truncated=trunc, stale=part["stale"] or stale)
    srv_all = [t for t in (head.server_now, srv) if t is not None]
    calls0 = rd.calls
    for c in sorted(children, key=lambda c: (c["state"] != "open", c["order"])):          # open children first: they are the ones that change
        if c["comments"] == 0:
            c["claims_read"] = True
            continue
        if rd.calls - calls0 >= COMMENT_CALLS or mono() - started > DEADLINE:
            break
        try:
            raw, _, csrv, cstale = _paged(lambda p, c=c: github.comments_path(c["owner"], c["name"], c["number"], p), COMMENT_CAP,
                                          COMMENTS_TTL if c["state"] == "open" else COMMENTS_TTL_CLOSED)
        except github.GhReadError as e:
            part["comments_failed"] += 1
            if e.reason in ("rate_limited", "no_login", "no_gh", "timeout"):
                part["partial_error"] = e.as_dict()
                break
            continue
        c["claims"], c["server_now"], c["claims_read"] = parse_claims(raw), csrv, True
        part["stale"] = part["stale"] or cstale
        if csrv is not None:
            srv_all.append(csrv)
    part["comments_pending"] = sum(1 for c in children if not c["claims_read"])          # children whose claim comments were not read this time (budget, a failure)
    part["partial"] = bool(part["comments_pending"])
    part["server_now"] = max(srv_all) if srv_all else None
    return part


# ---------------------------------------------------------------- the hub's side: nodes, tasks, pull requests

def _display() -> str:
    try:
        return nodes.display_name()
    except Exception:
        return "node"


def node_list(records) -> list[dict]:
    """This board and every paired node, for the pin menu: {handle, name, status, age_s}."""
    out = [{"handle": nodes.LOCAL, "name": _display(), "status": "online", "age_s": 0}]
    for r in records or []:
        if isinstance(r, dict) and nodes.valid_handle(r.get("handle")) and not r.get("legacy"):
            out.append({"handle": r["handle"], "name": github.clean_text(r.get("name"), 60) or r["handle"], "status": r.get("status") or "offline", "age_s": r.get("age_s")})
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


def _join_task(views: dict[int, list[dict]], child: dict) -> dict | None:
    slug = child["repo"].lower()
    cands = [v for v in views.get(child["number"], []) if v["slug"] == slug or (v["slug"] is None and v["repo"] == child["name"].lower())]
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


DEFAULTS = {"pins": {}, "epic_pin": None, "paused": False, "report": False, "auto_pr": False}


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
            "report": v.get("report") is True, "auto_pr": v.get("auto_pr") is True}


def _handle_ok(h, known: set[str]) -> bool:
    return isinstance(h, str) and (h == nodes.LOCAL or (nodes.valid_handle(h) and h in known))


def save_settings(db, owner: str, repo: str, n, patch: dict) -> dict:
    """Merge `patch` ({pins, epic_pin, paused, report, auto_pr}; only the keys present) into the stored settings and return the new ones. `pins` merges per
    issue: a handle sets, null or '' removes. Every handle must be `local` or a node in the registry (BadRequest otherwise), issue keys are digits, flags are
    booleans. Nothing is sent to GitHub. When everything is back to its default the row is removed."""
    known = {r["handle"] for r in nodes.registry(db)}
    cur = load_settings(db, owner, repo, n)
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
    for k in ("paused", "report", "auto_pr"):
        if k in patch:
            if not isinstance(patch[k], bool):
                raise projects.BadRequest(f"{k} is true or false")
            cur[k] = patch[k]
    key = settings_key(owner, repo, n)
    if cur == DEFAULTS:
        db.kv_del(key)
    else:
        db.kv_set(key, cur)
    return cur


def settings_view(db, owner: str, repo: str, n, records) -> dict:
    """{pins: {issue: {handle, node_name, source, stale}}, epic_pin, paused, report, auto_pr, nodes}: the stored settings as the page shows them. A pin to a node
    that is no longer in the registry is `stale: true` and ignored."""
    known = {r["handle"] for r in nodes.registry(db)}
    names = {x["handle"]: x["name"] for x in node_list(records)}
    st = load_settings(db, owner, repo, n)
    return {"pins": {k: _pin_view(h, "page", known, names) for k, h in st["pins"].items()},
            "epic_pin": _pin_view(st["epic_pin"], "page", known, names) if st["epic_pin"] else None,
            "paused": st["paused"], "report": st["report"], "auto_pr": st["auto_pr"], "nodes": node_list(records)}


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

def _bucket(closed: bool, claim: dict | None, task: dict | None) -> tuple[str | None, str]:
    """(node alias | None, done|running|queued|failed|closed|open) of one child. A closed child is done (by the node that held it, when known); an open child is
    what its claim and its task say; a claim that is not live and has no task behind it counts for nobody."""
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
    return None, "open"


def progress_of(rows: list[tuple[str | None, str]], amap: dict[str, str]) -> dict:
    """{by_node: {alias: {done, running, queued, failed, handle}}, unclaimed: {open, closed}, open, closed, total}; the counts add up to the children."""
    by: dict[str, dict] = {}
    unclaimed = {"open": 0, "closed": 0}
    for who, b in rows:
        if who is None:
            unclaimed["closed" if b == "closed" else "open"] += 1
            continue
        row = by.setdefault(who, {"done": 0, "running": 0, "queued": 0, "failed": 0, "handle": amap.get(who.lower())})
        row[b] += 1
    return {"by_node": by, "unclaimed": unclaimed, "total": len(rows)}


# ---------------------------------------------------------------- the answers

def _gh_login() -> str | None:
    from . import prpoll
    cwd = Path(settings.projects_dir)
    return prpoll.gh_login(cwd if cwd.is_dir() else Path.home())


def _empty_epic(owner: str, repo: str, n: int) -> dict:
    return {"repo": f"{owner}/{repo}", "owner": owner, "name": repo, "number": n, "title": None, "url": issue_url(owner, repo, n), "state": None,
            "labelled": None, "updated_at": None}


def detail(db, owner: str, repo: str, n: int, records, *, login=None) -> dict:
    """GET /api/epics/{owner}/{repo}/{n}. `records` is hub.records() ([] on a single board). GitHub problems never raise: the answer carries `error`
    ({reason, message, wait_s}) and, when an earlier read exists, its data with `stale_age_s`. The caller has validated owner, repo and n."""
    key = (owner.lower(), repo.lower(), int(n))
    part, error, stale_age = None, None, None
    try:
        part = _read_epic(owner, repo, int(n))
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
           "children": [], "progress": {"by_node": {}, "unclaimed": {"open": 0, "closed": 0}, "open": 0, "closed": 0, "total": 0},
           **settings_view(db, owner, repo, n, records), "rate": github.reader.rate(), "server_now": github.epoch_iso(part["server_now"]) if part else None, "error": error, "stale_age_s": stale_age,
           "truncated": bool(part and part["truncated"]), "partial": bool(part and part["partial"]), "comments_pending": part["comments_pending"] if part else 0,
           "empty": bool(part is not None and not part["children"])}
    if part is None:
        return out
    allowed = allowed_authors(owner, login if login is not None else _gh_login())
    views = _task_views(db, records, display)
    rows = []
    for c in part["children"]:
        claim, rejected = pick_claim([dict(x) for x in c["claims"]], allowed, c["server_now"] if c["server_now"] is not None else part["server_now"])
        tv = _join_task(views, c)
        task = _task_out(tv)
        who, bucket = _bucket(c["state"] == "closed", claim, task)
        rows.append((who, bucket))
        page_pin = st["pins"].get(str(c["number"]))
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
            "blocked_by": c["blocked_by"], "dependency_cycle": c["dependency_cycle"], "order": c["order"], "updated_at": c["updated_at"],
            "claim": None if claim is None else {"alias": claim["alias"], "node": amap.get(claim["alias"].lower()), "status": claim["status"], "since": claim["created_at"],
                                                  "renewed_at": claim["updated_at"], "live": claim["live"], "attempt": claim["attempt"], "seq": claim["seq"],
                                                  "task_ref": claim["task_ref"], "branch": claim["branch"]},
            "claim_rejected": rejected, "claim_note": STALE_NOTE if rejected else None,
            "claim_known": c["claims_read"], "task": task, "pr": pr, "pin": pin,
            "label_pin": lp, "bucket": {"node": who, "state": bucket}})                 # the progress strip's own bucket of this child: the page groups rows by it, so rows and strip agree
    prog = progress_of(rows, amap)
    prog["open"] = sum(1 for c in part["children"] if c["state"] == "open")
    prog["closed"] = sum(1 for c in part["children"] if c["state"] == "closed")
    out["progress"] = prog
    return out


def known_repos(projects_list, records) -> list[str]:
    """`owner/name` of every GitHub repository this board knows: its projects' repos (their `origin`) and the slugs in the nodes' last readings. Deduplicated
    without regard to case, valid names only, at most 25."""
    seen: dict[str, str] = {}

    def add(slug):
        if isinstance(slug, str) and "/" in slug:
            o, _, r = slug.partition("/")
            if github.valid_slug(o, r) and slug.lower() not in seen and len(seen) < REPO_CAP:
                seen[slug.lower()] = slug
    for p in projects_list or []:
        for r in (p.get("repos") or []) if isinstance(p, dict) else []:
            if isinstance(r, dict) and r.get("state") == "ok" and r.get("path"):
                add(node_state.repo_slug(r["path"]))
    for rec in records or []:
        st = rec.get("state") if isinstance(rec, dict) else None
        for p in (st.get("projects") or []) if isinstance(st, dict) else []:
            for r in (p.get("repos") or []) if isinstance(p, dict) else []:
                if isinstance(r, dict):
                    add(r.get("slug"))
    return list(seen.values())


def _counts(owner: str, repo: str, issue: dict) -> dict | None:
    """{open, closed, total} children of an epic: from the parent's own sub_issues_summary when GitHub sends it (no extra call), else by reading the list."""
    s = issue.get("sub_issues_summary")
    if isinstance(s, dict) and all(isinstance(s.get(k), int) and not isinstance(s.get(k), bool) and s.get(k) >= 0 for k in ("total", "completed")) and s["completed"] <= s["total"]:
        return {"open": s["total"] - s["completed"], "closed": s["completed"], "total": s["total"]}
    n = issue["number"]
    try:
        items, _, _, _ = _paged(lambda p: github.sub_issues_path(owner, repo, n, p), CHILD_CAP, LIST_TTL)
    except github.GhReadError as e:
        if e.reason != "not_found":
            raise
        items, _, _, _ = _paged(lambda p: github.labeled_path(owner, repo, f"{FALLBACK_LABEL}{n}", "all", p), CHILD_CAP, LIST_TTL)
    kids = [i for i in items if isinstance(i, dict) and "pull_request" not in i and i.get("number") != n]
    closed = sum(1 for i in kids if i.get("state") == "closed")
    return {"open": len(kids) - closed, "closed": closed, "total": len(kids)}


def _read_list(repos: list[str]) -> dict:
    epics, errors, top = [], [], None
    srv = None
    for slug in repos:
        owner, _, repo = slug.partition("/")
        try:
            items, _, s, _ = _paged(lambda p, o=owner, r=repo: github.labeled_path(o, r, EPIC_LABEL, "open", p), EPIC_CAP, LIST_TTL)
        except github.GhReadError as e:
            if e.reason in ("no_gh", "no_login", "rate_limited", "timeout"):
                top = e.as_dict()
                break
            errors.append({"repo": slug, **e.as_dict()})
            continue
        srv = s if s is not None and (srv is None or s > srv) else srv
        for it in items:
            if not isinstance(it, dict) or "pull_request" in it or not isinstance(it.get("number"), int) or not github.valid_number(it["number"]):
                continue
            if len(epics) >= EPIC_CAP:
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
    return {"epics": epics, "errors": errors, "error": top, "server_now": srv}


def list_epics(db, repos: list[str]) -> dict:
    """GET /api/epics: the open issues labelled ccboard:epic in `repos`, newest update first, each with its children's counts and whether this board paused it.
    Cached 60 s (a failed read keeps the last list with its age). With no repository to look in nothing is asked of GitHub."""
    key = tuple(r.lower() for r in repos)
    out = {"epics": [], "repos": list(repos), "errors": [], "error": None, "stale_age_s": None, "server_now": None, "rate": github.reader.rate()}
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
            res = {"epics": [], "errors": [], "error": github.GhReadError("bad_answer").as_dict(), "server_now": None}
        if res["error"] and hit is not None:
            out["stale_age_s"] = int(max(0.0, mono() - hit[0]))
            out["error"] = res["error"]
            res = hit[1]
        elif not res["error"]:
            with _lock:
                _list_good[key] = (mono(), res)
                while len(_list_good) > GOOD_MAX:
                    _list_good.pop(next(iter(_list_good)))
    out.update(epics=[], errors=res["errors"], server_now=github.epoch_iso(res["server_now"]), rate=github.reader.rate())
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
