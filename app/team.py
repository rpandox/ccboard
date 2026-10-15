"""The team model (issue #197, Team P1): who is working on what in a shared repository, built from what GitHub already shows. No network and no subprocess in this module.

`app/team_github.py` reads GitHub through the box's own gh and hands this module plain data (a "read", below); `build` groups it per person and returns the body of
`GET /api/team/state`. Everything here is read only: nothing about a teammate is ever a control, a link is built from a validated owner, repository and number (never copied
from GitHub), and every string that came from GitHub is cut, stripped of control, bidi and zero-width characters, and kept as text.

A person is keyed on GitHub's numeric user id and shown as the login (a login can be renamed; an account can be deleted). Two logins that share an id are one person; a
user without an id is keyed on the lower-case login. The owner's own login (the board's gh login) is marked `you`.

The read of one repository (what team_github.py fills, what this module takes):

  {slug, status, reason, message, wait_s, read_at, gql_at, graph_error, private, fork, default_branch,
   issues[{number, title, labels[], assignees[{id, login}], updated_at}],                     the open issues somebody is assigned to (pull requests left out)
   pulls[{number, title, draft, author{id, login}|None, head_ref, head_repo, fork, updated_at, closes[n]|None}],          closes is None until the GraphQL read has run
   linked{issue number: [{name, at, author{id, login}|None}]},                                 branches linked to an issue (gh issue develop)
   tips[{name, at, author{id, login}|None}],                                                   recent branch tips with the commit's GitHub author
   claims[{issue, user{id, login}, status, branch, live, updated_at}],                         #148 claim comments, read side only (absent: an empty list)
   graph_error                                                                                the reason the last GraphQL read failed, None when it worked or has not run
   truncated{issues, pulls, tips}}

`status` is ok, or the fixed reason a read failed (sso, no_access, not_found, rate_limited, low_budget, error); `pending` before the first read. A failed read keeps the
last good data and marks it stale: an access problem, a rate limit or an old read is never shown as "no teammates".
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from urllib.parse import quote

from . import github

API = 1
HOST = "github.com"
PEOPLE_MAX = 50
ITEMS_MAX = 30                  # issues, branches or pull requests shown per person
TEXT_MAX = 200
BRANCH_MAX = 100
TIP_DAYS = 14                   # a branch tip older than this is not "recent"
STALE_S = 900                   # a read older than this is shown as stale
BACKUP_PREFIX = "ccboard-backup/"          # the nightly backup branches of this tool are machinery, not work

STATES = ("off", "loading", "ok", "sso", "no_access", "not_found", "rate_limited", "low_budget", "no_login", "no_gh", "error")
MESSAGES = {
    "off": "Not shared with your team. Nothing is read from GitHub for this repository.",
    "loading": "Reading GitHub for the first time.",
    "ok": "",
    "sso": "GitHub needs this token authorised for the organization. Authorise it there, then check again.",
    "no_access": "This GitHub login cannot read this repository.",
    "not_found": "GitHub says there is no such repository, or this login cannot see it.",
    "rate_limited": "GitHub asked this board to wait.",
    "low_budget": "This login has few GitHub requests left, so team reads wait.",
    "no_login": "gh is not logged in on this board. Log gh in there, then check again.",
    "no_gh": "gh is not installed on this board, so GitHub cannot be read.",
    "error": "GitHub could not be read just now.",
}

LOGIN_RE = re.compile(r"(?=[A-Za-z0-9-]{1,39}(?:\[bot\])?\Z)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:\[bot\])?")          # GitHub's rule: letters, digits and single dashes, 39 at most; an app ends in [bot]
_BRANCH_LINK = re.compile(r"(?![-.])[A-Za-z0-9._/-]{1,100}")
_LABEL_MAX = 8


# ---------------------------------------------------------------- the repository key and what GitHub told us about it

def key_of(owner: str, name: str) -> str:
    """`github.com/<owner>/<name>` in lower case: the one key two boards can join on."""
    return f"{HOST}/{owner}/{name}".lower()


def split_key(key) -> tuple[str, str] | None:
    """(owner, name) of a repository key, or None when it is not one (GitHub's own rules decide)."""
    if not isinstance(key, str) or not key.startswith(HOST + "/"):
        return None
    owner, _, name = key[len(HOST) + 1:].partition("/")
    return (owner, name) if github.valid_slug(owner, name) else None


_hints: dict[str, str] = {}
_hint_lock = threading.Lock()


def hinted(key: str) -> str:
    """The canonical key for a local one: a fork resolves to its parent, a renamed repository to its new name, once GitHub said so (only for a shared repository)."""
    with _hint_lock:
        return _hints.get(key, key)


def set_hint(local: str, canonical: str | None) -> None:
    with _hint_lock:
        if canonical and canonical != local:
            _hints[local] = canonical
        else:
            _hints.pop(local, None)


def hints() -> dict[str, str]:
    with _hint_lock:
        return dict(_hints)


def clear_hints() -> None:
    with _hint_lock:
        _hints.clear()


# ---------------------------------------------------------------- untrusted text

def text(v, n: int = TEXT_MAX) -> str:
    """GitHub text kept as plain text: cut before any pattern, control, bidi, zero-width and private-use characters dropped, white space collapsed, at most `n` characters."""
    return github.clean_text(v, n)


def login_of(v) -> str | None:
    """A login GitHub could have: letters, digits and single dashes (a bot ends in [bot]); anything else is not a name this board puts on a person."""
    t = github.clean_text(v, 60) if isinstance(v, str) else ""
    return t if t and LOGIN_RE.fullmatch(t) else None


def user_id(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 < v < 10 ** 12 else None


def user_of(u) -> dict | None:
    """{id, login} of a read's user object, or None when it names nobody this board can name."""
    if not isinstance(u, dict):
        return None
    uid, login = user_id(u.get("id")), login_of(u.get("login"))
    return {"id": uid, "login": login} if uid is not None or login is not None else None


def branch_of(v) -> str | None:
    t = text(v, BRANCH_MAX)
    return t or None


def iso_of(v) -> str | None:
    return github.clean_ts(v)


def number_of(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 < v < 10 ** 9 else None


def _epoch(v) -> float | None:
    return github.ts_epoch(v)


def _iso_now(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- links, built from validated parts

def repo_url(owner: str, name: str) -> str:
    return f"https://github.com/{owner}/{name}"


def issue_url(owner: str, name: str, n: int) -> str:
    return f"{repo_url(owner, name)}/issues/{int(n)}"


def pull_url(owner: str, name: str, n: int) -> str:
    return f"{repo_url(owner, name)}/pull/{int(n)}"


def branch_url(owner: str, name: str, branch: str) -> str | None:
    """A link to a branch, or None for a branch name this board will not put in a URL (the name is shown as text either way)."""
    return f"{repo_url(owner, name)}/tree/{quote(branch, safe='/')}" if _BRANCH_LINK.fullmatch(branch) and not branch.endswith(("/", ".lock")) and ".." not in branch else None


# ---------------------------------------------------------------- the people

class _People:
    def __init__(self, me_login: str | None, me_id: int | None):
        self.me_login, self.me_id = (me_login or "").lower() or None, me_id
        self.by: dict[tuple, dict] = {}
        self.ids: dict[str, int] = {}

    def person(self, user: dict | None) -> dict | None:
        if not user:
            return None
        uid, login = user.get("id"), user.get("login")
        if uid is None and login is not None and login.lower() in self.ids:
            uid = self.ids[login.lower()]                       # the same login seen with its id elsewhere in the read: one person
        key = ("id", uid) if uid is not None else ("login", login.lower())
        p = self.by.get(key)
        if p is None:
            if len(self.by) >= PEOPLE_MAX:
                return None
            p = self.by[key] = {"id": uid, "login": login, "you": False, "issues": [], "branches": [], "prs": [], "last_active": None}
        if p["login"] is None and login is not None:
            p["login"] = login
        if uid is not None and login is not None:
            self.ids[login.lower()] = uid
        return p

    def seen(self, p: dict, when) -> None:
        a, b = _epoch(when), _epoch(p["last_active"])
        if a is not None and (b is None or a > b):
            p["last_active"] = iso_of(when)

    def finish(self) -> list[dict]:
        for p in self.by.values():
            if self.me_id is not None and p["id"] is not None:
                p["you"] = p["id"] == self.me_id
            elif self.me_login and p["login"]:
                p["you"] = p["login"].lower() == self.me_login
        for p in self.by.values():
            p["issues"].sort(key=lambda i: i["number"])
            p["prs"].sort(key=lambda i: i["number"])
            p["branches"].sort(key=lambda b: b["at"] or "", reverse=True)
            for k in ("issues", "branches", "prs"):
                p[k] = p[k][:ITEMS_MAX]
        return sorted(self.by.values(), key=lambda p: (not p["you"], -(_epoch(p["last_active"]) or 0), (p["login"] or "").lower(), p["id"] or 0))


def _tip_author(tip: dict) -> dict | None:
    return user_of(tip.get("author"))


def people_of(read: dict, me_login: str | None, me_id: int | None, owner: str, name: str, now: float) -> tuple[list[dict], dict]:
    """(people, counts) of one read. Every item is attached to the person GitHub names for it: an issue to its assignees, a pull request to its author, a pushed branch
    to the author of its tip commit, a linked branch to the author of its tip or, when that is not known, to the assignees of its issue, a claim to the commenter."""
    ppl = _People(me_login, me_id)
    issues = {}
    for it in (read.get("issues") or [])[:200]:
        n = number_of(it.get("number")) if isinstance(it, dict) else None
        if n is None:
            continue
        issues[n] = it
        entry = {"number": n, "title": text(it.get("title")), "url": issue_url(owner, name, n), "updated_at": iso_of(it.get("updated_at")),
                 "labels": [text(x, 50) for x in (it.get("labels") or [])[:_LABEL_MAX] if text(x, 50)], "branches": [], "claim": None}
        for a in (it.get("assignees") or [])[:10]:
            p = ppl.person(user_of(a))
            if p is not None:
                p["issues"].append(dict(entry))
                ppl.seen(p, it.get("updated_at"))
    default = branch_of(read.get("default_branch"))
    tip_at = {}
    for tip in (read.get("tips") or [])[:200]:
        b = branch_of(tip.get("name")) if isinstance(tip, dict) else None
        if b:
            tip_at[b] = tip
    linked_names = set()
    for n, branches in (read.get("linked") or {}).items():
        n = number_of(n) if isinstance(n, int) else number_of(int(n)) if isinstance(n, str) and n.isdigit() else None
        if n is None:
            continue
        for lb in branches[:5] if isinstance(branches, list) else []:
            b = branch_of(lb.get("name")) if isinstance(lb, dict) else None
            if not b:
                continue
            linked_names.add(b)
            author = user_of(lb.get("author")) or _tip_author(tip_at.get(b) or {})
            owners = [ppl.person(author)] if author else [ppl.person(user_of(a)) for a in ((issues.get(n) or {}).get("assignees") or [])[:10]]
            for p in filter(None, owners):
                p["branches"].append({"name": b, "url": branch_url(owner, name, b), "at": iso_of(lb.get("at")) or iso_of((tip_at.get(b) or {}).get("at")), "source": "linked", "issue": n})
                for e in p["issues"]:
                    if e["number"] == n and b not in e["branches"]:
                        e["branches"].append(b)
                ppl.seen(p, lb.get("at") or (tip_at.get(b) or {}).get("at"))
    unattributed = 0
    cutoff = now - TIP_DAYS * 86400
    for b, tip in tip_at.items():
        if b == default or b.startswith(BACKUP_PREFIX) or b in linked_names:
            continue
        at = _epoch(tip.get("at"))
        if at is None or at < cutoff:
            continue
        p = ppl.person(_tip_author(tip))
        if p is None:
            unattributed += 1
            continue
        p["branches"].append({"name": b, "url": branch_url(owner, name, b), "at": iso_of(tip.get("at")), "source": "pushed", "issue": None})
        ppl.seen(p, tip.get("at"))
    for pr in (read.get("pulls") or [])[:200]:
        n = number_of(pr.get("number")) if isinstance(pr, dict) else None
        p = ppl.person(user_of(pr.get("author"))) if n is not None else None
        if p is None:
            continue
        head_repo = text(pr.get("head_repo"), 140) or None
        if head_repo is not None and not github.valid_slug(*head_repo.partition("/")[::2]):
            head_repo = None                                        # a head repository that is not a repository name is not shown
        closes = [c for c in (pr.get("closes") or [])[:5] if number_of(c)] if isinstance(pr.get("closes"), list) else None
        p["prs"].append({"number": n, "title": text(pr.get("title")), "url": pull_url(owner, name, n), "draft": pr.get("draft") is True,
                         "state": "draft" if pr.get("draft") is True else "open", "head": branch_of(pr.get("head_ref")),
                         "from_fork": head_repo if pr.get("fork") is True and head_repo else None, "closes": closes, "updated_at": iso_of(pr.get("updated_at"))})
        ppl.seen(p, pr.get("updated_at"))
    for c in (read.get("claims") or [])[:100]:
        n = number_of(c.get("issue")) if isinstance(c, dict) else None
        p = ppl.person(user_of(c.get("user"))) if n is not None else None
        if p is None:
            continue
        claim = {"status": text(c.get("status"), 20), "branch": branch_of(c.get("branch")), "live": c.get("live") if isinstance(c.get("live"), bool) else None}
        hit = next((e for e in p["issues"] if e["number"] == n), None)
        if hit is None and n in issues:
            it = issues[n]
            hit = {"number": n, "title": text(it.get("title")), "url": issue_url(owner, name, n), "updated_at": iso_of(it.get("updated_at")), "labels": [], "branches": [], "claim": None}
            p["issues"].append(hit)
        if hit is not None:
            hit["claim"] = claim
        ppl.seen(p, c.get("updated_at"))
    people = ppl.finish()
    counts = {"people": len(people), "teammates": sum(1 for p in people if not p["you"]), "issues": sum(len(p["issues"]) for p in people),
              "branches": sum(len(p["branches"]) for p in people), "prs": sum(len(p["prs"]) for p in people), "unattributed_branches": unattributed}
    return people, counts


# ---------------------------------------------------------------- the body

def repo_state(entry: dict, read: dict | None, me_login: str | None, me_id: int | None, now: float) -> dict:
    """The block of one repository: `entry` is what this board knows of its own repository ({key, slug, local[{project, repo}], shared}), `read` what GitHub said (None: not read)."""
    split = split_key(entry.get("key"))
    owner, name = (split if split else ("", ""))
    spelled = entry.get("slug") if isinstance(entry.get("slug"), str) and "/" in entry["slug"] else f"{owner}/{name}"
    so, _, sn = spelled.partition("/")
    if not github.valid_slug(so, sn):
        so, sn = owner, name
    out = {"repo_slug": entry.get("key"), "slug": f"{so}/{sn}", "url": repo_url(so, sn) if so else None,
           "local": [{"project": text(x.get("project"), 100), "repo": text(x.get("repo"), 100)} for x in (entry.get("local") or [])[:20] if isinstance(x, dict)],
           "shared": bool(entry.get("shared"))}
    base = {"state": "off", "message": MESSAGES["off"], "wait_s": None, "stale": False, "read_at": None, "age_s": None, "gql_at": None, "graph_pending": False,
            "graph_failed": False, "private": None, "people": [], "counts": {"people": 0, "teammates": 0, "issues": 0, "branches": 0, "prs": 0, "unattributed_branches": 0},
            "truncated": {"issues": False, "pulls": False, "tips": False}}
    if not out["shared"]:
        return {**out, **base}
    if read is None or read.get("status") == "pending" and not read.get("read_at"):
        return {**out, **base, "state": "loading", "message": MESSAGES["loading"]}
    status = read.get("status") if read.get("status") in STATES else "error"
    read_at = read.get("read_at") if isinstance(read.get("read_at"), (int, float)) and not isinstance(read.get("read_at"), bool) else None
    age = max(0, int(now - read_at)) if read_at is not None else None
    people, counts = people_of(read, me_login, me_id, so, sn, now) if read_at is not None else ([], base["counts"])
    wait = read.get("wait_s") if isinstance(read.get("wait_s"), int) and not isinstance(read.get("wait_s"), bool) and read.get("wait_s") > 0 else None
    msg = MESSAGES[status] if status != "rate_limited" or not wait else f"GitHub asked this board to wait {wait} s."
    gql_at = read.get("gql_at") if isinstance(read.get("gql_at"), (int, float)) and not isinstance(read.get("gql_at"), bool) else None
    tr = read.get("truncated") if isinstance(read.get("truncated"), dict) else {}
    return {**out, "state": status, "message": msg, "wait_s": wait, "stale": status != "ok" or age is None or age > STALE_S,
            "read_at": _iso_now(read_at) if read_at is not None else None, "age_s": age, "gql_at": _iso_now(gql_at) if gql_at is not None else None,
            "graph_pending": gql_at is None, "graph_failed": bool(read.get("graph_error")), "private": read.get("private") if isinstance(read.get("private"), bool) else None, "people": people, "counts": counts,
            "truncated": {k: tr.get(k) is True for k in ("issues", "pulls", "tips")}}


def build(entries: list[dict], reads: dict, me_login: str | None, me_id: int | None, now: float) -> dict:
    """The body of GET /api/team/state: {api, enabled, me, repos[...], at}. `enabled` is true when at least one repository is shared. `me` is null while the board does not know
    which GitHub login it acts as (unknown, not "nobody")."""
    repos = [repo_state(e, reads.get(e.get("key")), me_login, me_id, now) for e in entries]
    repos.sort(key=lambda r: (not r["shared"], r["slug"].lower()))
    return {"api": API, "enabled": any(r["shared"] for r in repos),
            "me": {"login": me_login, "id": me_id} if me_login else None, "repos": repos, "at": _iso_now(now)}


def digest(body: dict) -> str:
    """The ETag base: the canonical JSON of the body without `at` and without each repository's `age_s` (the page works the age out from `read_at`)."""
    b = {k: v for k, v in body.items() if k != "at"}
    b["repos"] = [{k: v for k, v in r.items() if k != "age_s"} for r in body.get("repos") or []]
    return hashlib.sha256(json.dumps(b, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()[:32]


def etag(body: dict) -> str:
    return f'W/"{digest(body)}"'
