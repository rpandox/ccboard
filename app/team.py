"""The team model (issue #197, Team P1): who is working on what in a shared repository, built from what GitHub already shows. No network and no subprocess in this module.

`app/team_github.py` reads GitHub through the box's own gh and hands this module plain data (a "read", below); `build` groups it per person and returns the body of
`GET /api/team/state`. Everything here is read only: nothing about a teammate is ever a control, a link is built from a validated owner, repository and number (never copied
from GitHub), and every string that came from GitHub is cut, stripped of control, bidi and zero-width characters, and kept as text.

A person is keyed on GitHub's numeric user id and shown as the login (a login can be renamed; an account can be deleted). Two logins that share an id are one person; a
user without an id is keyed on the lower-case login. The owner's own login (the board's gh login) is marked `you`.

The read of one repository (what team_github.py fills, what this module takes):

  {slug, status, reason, message, wait_s, read_at, gql_at, graph_error, private, fork, default_branch,
   issues[{number, title, labels[], assignees[{id, login}], updated_at}],                     the open issues somebody is assigned to (pull requests left out)
   pulls[{number, title, draft, author{id, login}|None, association, head_ref, head_repo, fork, updated_at, closes[n]|None}],          closes is None until the GraphQL read has run
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
import unicodedata
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


# ---------------------------------------------------------------- presence (issue #198, Team P2): the comment format, the whitelist and the live, idle, stale and gone rules
#
# One status issue per shared repository (label ccboard:presence) holds one comment per person; the comment is edited in place. Its first line is the marker, then ONE line per board
# of that person (the #148 alias and the 8 hex characters of the node's hash), in the #148 claim-line style:
#
#   <!-- ccboard:presence v1 -->
#   ccboard presence: node=willow-3f nid=1a2b3c4d repo=acme/shop seq=12 at=2027-01-15T10:05:00Z lease=30m state=running agent=claude since=2027-01-15T09:00Z last=2027-01-15T10:00Z issue=212 branch=fix-login phase=running
#
# The keys of a line are PRESENCE_KEYS and nothing else (a test pins the set): `build_line` refuses any other key, and every value must fit its own pattern, so no title, prompt,
# reply, path, host name, account label or other free text can reach a body. The line's order is fixed and the parser is strict: a line that does not match exactly is dropped.

PRESENCE_MARKER = "<!-- ccboard:presence v1 -->"
PRESENCE_LABEL = "ccboard:presence"
PRESENCE_PREFIX = "ccboard presence: "
PRESENCE_KEYS = ("node", "nid", "repo", "seq", "at", "lease", "state", "agent", "since", "last", "issue", "branch", "phase")          # THE whitelist
PRESENCE_REQUIRED = ("node", "nid", "repo", "seq", "at", "lease", "state")
PRESENCE_STATES = ("running", "needs_you", "idle", "ended")
PRESENCE_AGENTS = ("claude", "codex", "shell", "other")
PRESENCE_PHASES = ("backlog", "queued", "running", "done", "failed", "cancelled")
PRESENCE_LEASE_MIN = 30
PRESENCE_GONE_S = 24 * 3600
PRESENCE_BODY_MAX = 3000                    # characters of a presence comment: a longer one is not read (the cap comes before any pattern)
PRESENCE_LINES_MAX = 8                      # boards (lines) one comment may hold
LIVE_WORDS = ("live", "idle", "unconfirmed", "stale", "gone", "unknown")
_ISO_S = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
_ISO_M = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}Z"
_P_VALUE = {
    "node": r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", "nid": r"[0-9a-f]{8}", "repo": r"[a-z0-9][a-z0-9-]{0,38}/[a-z0-9_.-]{1,100}", "seq": r"[0-9]{1,9}", "at": _ISO_S, "lease": r"[0-9]{2,3}m",
    "state": "|".join(PRESENCE_STATES), "agent": "|".join(PRESENCE_AGENTS), "since": _ISO_M, "last": _ISO_M, "issue": r"[1-9][0-9]{0,8}",
    "branch": r"(?![-.])[A-Za-z0-9._/-]{1,100}", "phase": "|".join(PRESENCE_PHASES)}
_P_LINE = re.compile(re.escape(PRESENCE_PREFIX) + " ".join(f"{k}=(?P<{k}>{_P_VALUE[k]})" for k in PRESENCE_REQUIRED)
                     + "".join(f"(?: {k}=(?P<{k}>{_P_VALUE[k]}))?" for k in PRESENCE_KEYS if k not in PRESENCE_REQUIRED))


def _valid_iso(v: str, minute: bool) -> bool:
    try:
        datetime.strptime(v, "%Y-%m-%dT%H:%MZ" if minute else "%Y-%m-%dT%H:%M:%SZ")
        return True
    except ValueError:
        return False


def presence_line(fields: dict) -> str:
    """One board's line. `fields` may name only PRESENCE_KEYS (ValueError for any other key: the whitelist) and every value must fit its pattern (ValueError otherwise). Optional keys
    with the value None are left out. The line is checked by the parser before it is returned."""
    if not isinstance(fields, dict):
        raise ValueError("not a presence line")
    extra = set(fields) - set(PRESENCE_KEYS)
    if extra:
        raise ValueError("key not on the presence whitelist: " + ", ".join(sorted(map(str, extra))))
    parts = []
    for k in PRESENCE_KEYS:
        v = fields.get(k)
        if v is None:
            if k in PRESENCE_REQUIRED:
                raise ValueError(f"presence line without {k}")
            continue
        s = str(v) if isinstance(v, (str, int)) and not isinstance(v, bool) else None
        if s is None or not re.fullmatch(_P_VALUE[k], s):
            raise ValueError(f"bad presence value for {k}")
        parts.append(f"{k}={s}")
    line = PRESENCE_PREFIX + " ".join(parts)
    if parse_line(line) is None:
        raise ValueError("bad presence line")
    return line


def parse_line(line) -> dict | None:
    """The fields of one presence line, or None when it is not exactly one (strict: fixed order, known keys only, every value in its pattern, times that are real times)."""
    if not isinstance(line, str) or len(line) > 600:
        return None
    m = _P_LINE.fullmatch(line)
    if not m:
        return None
    f = {k: v for k, v in m.groupdict().items() if v is not None}
    if not _valid_iso(f["at"], False) or any(k in f and not _valid_iso(f[k], True) for k in ("since", "last")):
        return None
    lease = int(f["lease"][:-1])
    if not 10 <= lease <= 120 or ("branch" in f and (".." in f["branch"] or f["branch"].endswith(("/", ".lock")))):
        return None
    o, _, n = f["repo"].partition("/")
    if not github.valid_slug(o, n):
        return None
    out: dict = dict(f)
    out["seq"], out["lease_min"] = int(f["seq"]), lease
    if "issue" in f:
        out["issue"] = int(f["issue"])
    return out


def presence_body(lines: list[str]) -> str:
    """The comment: the marker, then the lines (one per board, at most PRESENCE_LINES_MAX, sorted by nid so two boards build the same text). ValueError when a line is not one."""
    if not lines or len(lines) > PRESENCE_LINES_MAX or any(parse_line(x) is None for x in lines):
        raise ValueError("bad presence body")
    nids = [parse_line(x)["nid"] for x in lines]
    if len(set(nids)) != len(nids):
        raise ValueError("two lines of one board")
    body = PRESENCE_MARKER + "\n" + "\n".join(sorted(lines, key=lambda x: parse_line(x)["nid"]))
    if len(body) > PRESENCE_BODY_MAX:
        raise ValueError("presence body too long")
    return body


def parse_body(body) -> list[dict] | None:
    """The lines of a presence comment, or None when it is not one: longer than PRESENCE_BODY_MAX (checked before anything else), or the first line is not the marker. A line that
    does not parse is dropped; a second line of the same board is dropped; at most PRESENCE_LINES_MAX lines. Each line dict keeps its literal text in `raw`."""
    if not isinstance(body, str) or len(body) > PRESENCE_BODY_MAX:
        return None
    head, _, rest = body.replace("\r\n", "\n").partition("\n")
    if head != PRESENCE_MARKER:
        return None
    out, seen = [], set()
    for raw in rest.split("\n")[:PRESENCE_LINES_MAX * 2]:
        f = parse_line(raw)
        if f is None or f["nid"] in seen:
            continue
        seen.add(f["nid"])
        f["raw"] = raw
        out.append(f)
        if len(out) >= PRESENCE_LINES_MAX:
            break
    return out


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


def presence_live(seq_seen_at, moved: bool, updated_at, server_now, lease_min, active: bool) -> tuple[str, int | None]:
    """(word, seconds since last seen) of one board's line, on GitHub's clock only (never this board's clock).

    `seq_seen_at` is the Date header of the read that first saw the line's CURRENT sequence number, `moved` is True when an earlier read saw another number for that board (the
    number advanced between two reads), `updated_at` the comment's own time, `server_now` the Date of the latest read. The line changed no later than both `updated_at` and
    `seq_seen_at`, so its age is `server_now - min(updated_at, seq_seen_at)`.
      live         the number advanced AND the line is younger than the lease (idle: the same, with no session running or waiting on a person)
      unconfirmed  younger than the lease, but the number has not been seen to move yet (a first read): not live
      stale        older than the lease (a moved `updated_at` alone, which a sibling board's line or a maintainer's edit also moves, keeps nothing live)
      gone         older than 24 h (hidden, not removed)
      unknown      no clock from GitHub: not live (fail closed)"""
    now, upd, seen = _num(server_now), _num(updated_at), _num(seq_seen_at)
    if now is None or upd is None:
        return "unknown", None
    try:
        lease = max(10, min(120, int(lease_min))) * 60
    except (TypeError, ValueError):
        lease = PRESENCE_LEASE_MIN * 60
    basis = upd if seen is None else min(upd, seen)
    age = max(0, int(now - basis))
    if moved is True and seen is not None and age < lease:
        return ("live" if active else "idle"), age
    if age >= PRESENCE_GONE_S:
        return "gone", age
    if age >= lease:
        return "stale", age
    return "unconfirmed", age


def _presence_person(read: dict, owner: str, name: str, keep_gone: bool = False) -> list[tuple[dict, dict]]:
    """(user, board) of every presence line of a read, with its live word; gone lines are left out (gone hides a line, it does not remove it). `keep_gone` is for the warnings of
    issue #199, which apply their own rule to every word (a gone line warns nobody)."""
    pres = read.get("presence") if isinstance(read.get("presence"), dict) else None
    if not pres:
        return []
    server_now = pres.get("server_now") if isinstance(pres.get("server_now"), (int, float)) and not isinstance(pres.get("server_now"), bool) else None
    out = []
    for ln in (pres.get("lines") or [])[:100]:
        if not isinstance(ln, dict):
            continue
        user = user_of(ln.get("user"))
        state = ln.get("state") if ln.get("state") in PRESENCE_STATES else None
        if user is None or state is None:
            continue
        active = state in ("running", "needs_you")
        word, age = presence_live(ln.get("seq_seen_at"), ln.get("moved") is True, _epoch(ln.get("updated_at")), server_now, ln.get("lease_min") or PRESENCE_LEASE_MIN, active)
        if word == "gone" and not keep_gone:
            continue
        n = number_of(ln.get("issue"))
        b = branch_of(ln.get("branch")) if isinstance(ln.get("branch"), str) else None
        out.append((user, {"node": text(ln.get("node"), 40), "live": word, "last_seen_s": age, "seq": ln.get("seq") if isinstance(ln.get("seq"), int) else None,
                           "state": state, "agent": ln.get("agent") if ln.get("agent") in PRESENCE_AGENTS else None,
                           "issue": n, "issue_url": issue_url(owner, name, n) if n else None, "branch": b, "branch_url": branch_url(owner, name, b) if b else None,
                           "phase": ln.get("phase") if ln.get("phase") in PRESENCE_PHASES else None,
                           "since": iso_of((ln.get("since") or "")[:-1] + ":00Z") if isinstance(ln.get("since"), str) else None,
                           "last": iso_of((ln.get("last") or "")[:-1] + ":00Z") if isinstance(ln.get("last"), str) else None,
                           "updated_at": iso_of(ln.get("updated_at"))}))
    return out


_LIVE_RANK = {"live": 0, "idle": 1, "unconfirmed": 2, "stale": 3, "unknown": 4, "gone": 5}
READ_PRESENCE = {                            # what the reader found of the status issue of a shared repository (the `presence` block of a repository)
    "ok": "",
    "none": "Nobody publishes presence in this repository yet.",
    "closed": "The presence issue of this repository is closed, so presence is not read.",
    "error": "The presence comments could not be read just now.",
}


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
            p = self.by[key] = {"id": uid, "login": login, "you": False, "issues": [], "branches": [], "prs": [], "last_active": None, "presence": None}
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
    for user, board in _presence_person(read, owner, name):                # issue #198: the live line of each board that publishes presence
        p = ppl.person(user)
        if p is None:
            continue
        pres = p["presence"] or {"live": "gone", "last_seen_s": None, "boards": []}
        if len(pres["boards"]) < PRESENCE_LINES_MAX:
            pres["boards"].append(board)
        best = min(pres["boards"], key=lambda b: (_LIVE_RANK.get(b["live"], 9), b["last_seen_s"] if b["last_seen_s"] is not None else 10 ** 9))
        pres["live"], pres["last_seen_s"] = best["live"], best["last_seen_s"]
        p["presence"] = pres
    people = ppl.finish()
    counts = {"people": len(people), "teammates": sum(1 for p in people if not p["you"]), "issues": sum(len(p["issues"]) for p in people),
              "branches": sum(len(p["branches"]) for p in people), "prs": sum(len(p["prs"]) for p in people), "unattributed_branches": unattributed,
              "live": sum(1 for p in people if not p["you"] and p["presence"] and p["presence"]["live"] in ("live", "idle"))}
    return people, counts


# ---------------------------------------------------------------- warnings (issue #199, Team P3): advice before the owner starts what a teammate already holds
#
# Computed from the read Phases 1 and 2 already made (no network, no clock but the one passed in), keyed so the launcher can look a warning up:
#   warnings.issues[<issue number>]  -> [warning]        one per person: the signals that person has on the issue (assigned, claim, presence, branch, pr)
#   warnings.branches[<branch name>] -> [branch warning]  one per person: the branch exists on the remote under a teammate, or a teammate linked it to an issue
#   warnings.tasks[<local task id>]  -> {issue, branch, files}   the same, looked up for one of this board's own open tasks, plus the files an open pull request also changes
# A warning is advice and nothing else: it carries `advice: true`, no key that could drive a button, a dispatch or a write (`_advice` refuses one), and the owner's own login (and
# every board under it) never produces one. A stale presence line warns with its age, a gone one and an unknown one warn nobody, and an identity this board does not know yet warns
# nobody (it could not tell the owner's own work from a teammate's). The local file names come from the owner's own worktrees and go only to the signed-in owner.

WARN_LIVE = ("live", "idle", "unconfirmed", "stale")          # a presence line in any other word (gone, unknown) claims nothing
FILES_SHOWN = 20                # file names named in one warning
PATH_MAX = 300
FILES_PER_PR = 200              # names of one pull request this module takes
PULLS_FILES_MAX = 100
LOCAL_TASKS_MAX = 100
LOCAL_FILES_MAX = 1000
BRANCH_WARN_MAX = 100
ISSUE_WARN_MAX = 200
ADVICE_FORBIDDEN = ("block", "blocks", "blocked", "disable", "disabled", "assign", "assignee", "label", "comment", "write", "action", "lock", "claim_it", "command")


def _advice(d: dict) -> dict:
    """Seal a warning as advice: refuses any key that could drive a control, a dispatch or a write (ValueError), and marks it `advice: true`."""
    bad = [k for k in d if k in ADVICE_FORBIDDEN] + [k for s in (d.get("signals") or []) if isinstance(s, dict) for k in s if k in ADVICE_FORBIDDEN]
    if bad:
        raise ValueError("a warning is advice only: " + ", ".join(sorted(set(bad))))
    return {**d, "advice": True}


def path_of(v) -> str | None:
    """A repository-relative file path from GitHub or a worktree, or None. Cut before anything else, no control, bidi, zero-width or private-use character, no white space but a plain
    space inside, not absolute and no `..` segment: it is shown as text and compared as text, never opened or passed to a command."""
    if not isinstance(v, str) or not v or len(v) > PATH_MAX:
        return None
    for ch in v:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp") or (cat == "Zs" and ch != " "):
            return None
    if v.startswith("/") or v != v.strip() or ".." in v.split("/"):
        return None
    return v


def trusted_pull(pr) -> bool:
    """Does a pull request count for a warning? Only when GitHub says its author may write to the repository (author_association in nodes_epics.ASSOCIATIONS, as for claims and
    presence) and the author is not a bot. On a public repository anybody can open a pull request, so any other one is shown in the Team block but warns nobody and is not read."""
    from . import nodes_epics                                  # not at the top: nodes_epics reaches this module through node_state
    if not isinstance(pr, dict) or pr.get("association") not in nodes_epics.ASSOCIATIONS:
        return False
    login = (pr.get("author") or {}).get("login") if isinstance(pr.get("author"), dict) else None
    return isinstance(login, str) and not login.lower().endswith("[bot]")


def same_person(user, me_login: str | None, me_id: int | None) -> bool:
    """Is `user` ({id, login}) the owner of this board? By the numeric id when both sides have one, else by the login without case."""
    if not isinstance(user, dict):
        return False
    uid, login = user.get("id"), user.get("login")
    if me_id is not None and uid is not None:
        return uid == me_id
    return bool(me_login and login and login.lower() == me_login.lower())


def _local_clean(local) -> list[dict]:
    """This board's own open tasks of one repository, cleaned: [{id, title, issue, branch, files[]|None}] (files None: not known, which is not the same as no files)."""
    out = []
    for t in (local if isinstance(local, list) else [])[:LOCAL_TASKS_MAX]:
        tid = t.get("id") if isinstance(t, dict) else None
        if isinstance(tid, bool) or not isinstance(tid, int) or tid <= 0:
            continue
        files = t.get("files")
        fl = sorted({q for q in (path_of(x) for x in list(files)[:LOCAL_FILES_MAX]) if q}) if isinstance(files, (list, tuple, set)) else None
        n = t.get("issue_number")
        out.append({"id": tid, "title": text(t.get("title"), 100), "issue": number_of(n) if isinstance(n, int) else None,
                    "branch": branch_of(t.get("branch")) if isinstance(t.get("branch"), str) else None, "files": fl})
    return out


def _users_of(read: dict) -> list[dict]:
    """Every user object of a read, so one person seen with an id in one place and without in another is one person."""
    out = []
    for it in (read.get("issues") or [])[:ISSUE_WARN_MAX] if isinstance(read.get("issues"), list) else []:
        out += list((it.get("assignees") or [])[:10]) if isinstance(it, dict) else []
    for key in ("pulls", "tips", "claims"):
        for x in (read.get(key) or [])[:200] if isinstance(read.get(key), list) else []:
            if isinstance(x, dict):
                out.append(x.get("user") if key == "claims" else x.get("author"))
    for lst in (read.get("linked") or {}).values() if isinstance(read.get("linked"), dict) else []:
        out += [lb.get("author") for lb in lst[:5] if isinstance(lb, dict)] if isinstance(lst, list) else []
    pres = read.get("presence") if isinstance(read.get("presence"), dict) else {}
    out += [ln.get("user") for ln in (pres.get("lines") or [])[:100] if isinstance(ln, dict)]
    return [u for u in (user_of(x) for x in out) if u]


def _epoch_or(v, d=0.0) -> float:
    e = _epoch(v)
    return d if e is None else e


def _pr_files(read: dict) -> dict:
    """{pull number: {files[], more}} of the changed-file lists the reader made (cleaned again here; at most FILES_PER_PR names a pull request)."""
    out = {}
    pf = read.get("pull_files") if isinstance(read.get("pull_files"), dict) else {}
    for n, v in list(pf.items())[:PULLS_FILES_MAX]:
        num = number_of(n) if isinstance(n, int) else number_of(int(n)) if isinstance(n, str) and n.isdigit() else None
        if num is None or not isinstance(v, dict) or not isinstance(v.get("files"), list):
            continue
        out[num] = {"files": sorted({q for q in (path_of(x) for x in v["files"][:FILES_PER_PR]) if q}), "more": v.get("more") is True}
    return out


def conflicts(read: dict, me_login: str | None, me_id: int | None, owner: str, name: str, now: float, lc: list[dict] | None = None) -> tuple[dict, dict]:
    """(warnings, hits) of one read. `warnings` is what the block carries; `hits` is {pull number: [(task, common files, more)]} for marking the rows of the Team block."""
    lc = lc or []
    out = {"api": 1, "identity_known": bool(me_login) or me_id is not None, "issues": {}, "branches": {}, "tasks": {},
           "files": {"checked": 0, "pending": 0, "capped": False}}
    hits: dict = {}
    if not out["identity_known"] or not isinstance(read, dict):
        return out, hits
    ppl = _People(me_login, me_id)
    for u in _users_of(read):
        if u["id"] is not None and u["login"] is not None:
            ppl.ids[u["login"].lower()] = u["id"]

    def who(user) -> dict | None:
        """The teammate a user is, or None: nobody this board can name, or the owner (own boards never warn each other)."""
        p = ppl.person(user_of(user))
        if p is None or same_person(p, me_login, me_id):
            return None
        return p

    # ---- same issue: one entry per (issue, person), the best of each kind of signal
    sig: dict = {}

    def add(n: int, p: dict, kind: str, data: dict, rank) -> None:
        e = sig.setdefault((n, id(p)), {"p": p, "n": n, "kinds": {}})
        cur = e["kinds"].get(kind)
        if cur is None or rank(data) < rank(cur):
            e["kinds"][kind] = data

    issues = {}
    for it in (read.get("issues") or [])[:ISSUE_WARN_MAX] if isinstance(read.get("issues"), list) else []:
        n = number_of(it.get("number")) if isinstance(it, dict) else None
        if n is None:
            continue
        issues[n] = it
        for a in (it.get("assignees") or [])[:10]:
            p = who(a)
            if p is not None:
                add(n, p, "assigned", {"kind": "assigned"}, lambda d: 0)
    for c in (read.get("claims") or [])[:100] if isinstance(read.get("claims"), list) else []:
        n = number_of(c.get("issue")) if isinstance(c, dict) else None
        p = who(c.get("user")) if n is not None else None
        if p is not None and c.get("live") is True:                  # only a live claim holds the issue; an expired one or one with no clock claims nothing
            add(n, p, "claim", {"kind": "claim", "status": text(c.get("status"), 20), "branch": branch_of(c.get("branch")), "at": iso_of(c.get("updated_at"))}, lambda d: 0)
    default = branch_of(read.get("default_branch"))
    tip_by = {}
    for tip in (read.get("tips") or [])[:200] if isinstance(read.get("tips"), list) else []:
        b = branch_of(tip.get("name")) if isinstance(tip, dict) else None
        if b:
            tip_by[b] = tip
    bmap: dict = {}                                                      # (branch, person) -> branch warning data

    def add_branch(b: str, p: dict, source: str, at, issue=None, pr=None) -> None:
        if not b or b == default or b.startswith(BACKUP_PREFIX):
            return
        e = bmap.setdefault((b, id(p)), {"p": p, "name": b, "sources": set(), "issue": None, "pr": None, "at": None})
        e["sources"].add(source)
        e["issue"] = e["issue"] or issue
        e["pr"] = e["pr"] or pr
        if _epoch_or(at, -1) > _epoch_or(e["at"], -1):
            e["at"] = iso_of(at)

    for n, branches in (read.get("linked") or {}).items() if isinstance(read.get("linked"), dict) else []:
        n = number_of(n) if isinstance(n, int) else number_of(int(n)) if isinstance(n, str) and n.isdigit() else None
        if n is None:
            continue
        for lb in branches[:5] if isinstance(branches, list) else []:
            b = branch_of(lb.get("name")) if isinstance(lb, dict) else None
            if not b:
                continue
            author = user_of(lb.get("author")) or _tip_author(tip_by.get(b) or {})
            owners = [author] if author else [user_of(a) for a in ((issues.get(n) or {}).get("assignees") or [])[:10]]
            at = lb.get("at") or (tip_by.get(b) or {}).get("at")
            for u in owners:
                p = who(u)
                if p is None:
                    continue
                add_branch(b, p, "linked", at, issue=n)
                add(n, p, "branch", {"kind": "branch", "name": b, "url": branch_url(owner, name, b), "at": iso_of(at)}, lambda d: -_epoch_or(d["at"]))
    for b, tip in tip_by.items():
        p = who(_tip_author(tip))
        if p is not None:
            add_branch(b, p, "pushed", tip.get("at"))
    prs = {}
    for pr in (read.get("pulls") or [])[:200] if isinstance(read.get("pulls"), list) else []:
        n = number_of(pr.get("number")) if isinstance(pr, dict) else None
        p = who(pr.get("author")) if n is not None and trusted_pull(pr) else None
        if p is None:
            continue
        head = branch_of(pr.get("head_ref")) if isinstance(pr.get("head_ref"), str) else None
        prs[n] = (pr, p)
        if head and pr.get("fork") is not True:                               # a branch of a fork is not a branch of this repository
            add_branch(head, p, "pr", pr.get("updated_at"), pr=n)
        info = {"kind": "pr", "number": n, "title": text(pr.get("title")), "url": pull_url(owner, name, n), "draft": pr.get("draft") is True, "branch": head,
                "at": iso_of(pr.get("updated_at"))}
        for c in (pr.get("closes") or [])[:5] if isinstance(pr.get("closes"), list) else []:
            c = number_of(c)
            if c is not None:
                add(c, p, "pr", info, lambda d: (-_epoch_or(d["at"]), d["number"]))
    for user, board in _presence_person(read, owner, name, keep_gone=True):
        p = who(user)
        n = board["issue"]
        if p is None or n is None or board["live"] not in WARN_LIVE or board["state"] == "ended":          # gone and unknown claim nothing; an ended session holds nothing
            continue
        add(n, p, "presence", {"kind": "presence", "live": board["live"], "last_seen_s": board["last_seen_s"], "state": board["state"], "agent": board["agent"],
                               "since": board["since"], "last": board["last"], "branch": board["branch"], "at": board["updated_at"]},
            lambda d: (_LIVE_RANK.get(d["live"], 9), d["last_seen_s"] if d["last_seen_s"] is not None else 10 ** 9))
    order = ("assigned", "claim", "presence", "branch", "pr")
    by_issue: dict = {}
    for (n, _), e in sig.items():
        ks = e["kinds"]
        pres = ks.get("presence")
        solid = any(k in ks for k in ("assigned", "claim", "branch", "pr")) or (pres is not None and pres["live"] in ("live", "idle"))
        status = "taken" if solid else "recent" if pres is not None and pres["live"] == "unconfirmed" else "stale"
        since = pres["since"] if pres is not None and pres["live"] != "stale" else None          # a stale line says when the session began, not that it still runs: no "since" from it
        w = _advice({"issue": n, "url": issue_url(owner, name, n), "person": {"id": e["p"]["id"], "login": e["p"]["login"]}, "status": status,
                     "signals": [ks[k] for k in order if k in ks], "since": since, "since_from": "presence" if since else None,
                     "age_s": pres["last_seen_s"] if pres else None,
                     "stale_age_s": pres["last_seen_s"] if status == "taken" and pres is not None and pres["live"] == "stale" else None})          # taken by another signal, its status line old: the age is said
        by_issue.setdefault(n, []).append(w)
    rank = {"taken": 0, "recent": 1, "stale": 2}
    for n in sorted(by_issue)[:ISSUE_WARN_MAX]:
        out["issues"][str(n)] = sorted(by_issue[n], key=lambda w: (rank[w["status"]], (w["person"]["login"] or "").lower(), w["person"]["id"] or 0))[:10]

    # ---- same branch
    blist = sorted(bmap.values(), key=lambda e: (-_epoch_or(e["at"]), e["name"], (e["p"]["login"] or "").lower()))[:BRANCH_WARN_MAX]
    cutoff = now - TIP_DAYS * 86400
    bw: dict = {}
    for e in blist:
        at = _epoch(e["at"])
        bw.setdefault(e["name"], []).append(_advice({"name": e["name"], "url": branch_url(owner, name, e["name"]), "person": {"id": e["p"]["id"], "login": e["p"]["login"]},
                                                     "sources": sorted(e["sources"]), "issue": e["issue"], "pr": e["pr"], "at": e["at"], "old": at is not None and at < cutoff}))
    out["branches"] = bw

    # ---- same files: a teammate's open or draft pull request changes files this board's own open tasks also touch
    files = _pr_files(read)
    out["files"] = {"checked": sum(1 for n in prs if n in files), "pending": sum(1 for n in prs if n not in files), "capped": any(files[n]["more"] for n in prs if n in files)}
    fw: dict = {}
    for t in lc:
        if t["files"] is None:
            continue
        mine = set(t["files"])
        for n in sorted(prs):
            pr, p = prs[n]
            pf = files.get(n)
            common = sorted(mine & set(pf["files"])) if pf else []
            if not common:
                continue
            hits.setdefault(n, []).append((t, common, pf["more"]))
            fw.setdefault(t["id"], []).append(_advice({"task": t["id"], "files": common[:FILES_SHOWN], "count": len(common), "more": pf["more"],
                                                       "person": {"id": p["id"], "login": p["login"]},
                                                       "pr": {"number": n, "title": text(pr.get("title")), "url": pull_url(owner, name, n), "draft": pr.get("draft") is True,
                                                              "branch": branch_of(pr.get("head_ref")) if isinstance(pr.get("head_ref"), str) else None}}))

    # ---- one lookup by local task id
    for t in lc:
        ent = {"task": t["id"], "title": t["title"], "issue": out["issues"].get(str(t["issue"]), []) if t["issue"] else [],
               "branch": bw.get(t["branch"], []) if t["branch"] else [], "files": fw.get(t["id"], [])}
        if ent["issue"] or ent["branch"] or ent["files"]:
            out["tasks"][str(t["id"])] = ent
    return out, hits


def mark_rows(people: list[dict], lc: list[dict], hits: dict) -> None:
    """Mark the rows of the Team block that collide with this board's own open tasks (issue #199): `collides` [{task, title, kind: issue|branch|files, files[], count}] on a
    teammate's issue, branch or pull request row. Only teammates' rows are marked, and only when there is a collision (no key otherwise)."""
    if not lc:
        return
    for p in people:
        if p.get("you"):
            continue
        for row in p["issues"]:
            c = [{"task": t["id"], "title": t["title"], "kind": "issue"} for t in lc if t["issue"] == row["number"]]
            if c:
                row["collides"] = c
        for row in p["branches"]:
            c = [{"task": t["id"], "title": t["title"], "kind": "branch"} for t in lc if t["branch"] and t["branch"] == row["name"]]
            if c:
                row["collides"] = c
        for row in p["prs"]:
            c = [{"task": t["id"], "title": t["title"], "kind": "issue"} for t in lc if t["issue"] and t["issue"] in (row.get("closes") or [])]
            c += [{"task": t["id"], "title": t["title"], "kind": "files", "files": common[:FILES_SHOWN], "count": len(common), "more": more} for t, common, more in hits.get(row["number"], [])]
            if c:
                row["collides"] = c


# ---------------------------------------------------------------- free issues and the one view of the whole team (issue #200, Team P4)
#
# Both are worked out from the reads the poll already made (nothing new is read from GitHub, nothing is written) and from this board's own tasks.
#
# `free` is the block of a shared repository's open issues that nobody holds, by the rules of Team P3: a teammate holds an issue when `conflicts` has a warning for it (assigned, a live
# claim, a linked branch, a trusted pull request that closes it, or a presence line that is not gone or unknown; a stale one still counts: somebody touched it lately), and the
# owner holds an issue assigned to them, claimed by them, linked by them, closed by their pull request, named by their presence line or carried by one of their open tasks.
#   {state, reason, message, held[issue numbers], issues[{number, title, url, labels[], updated_at}], count, truncated}
#   state   ok       `issues` are the open issues nobody holds
#           unread   the holders are known but the read has no list of open issues yet (the reader asks GitHub for the open issues nobody is assigned to once in a while, after the
#                    repository's own read, and only while the hour's budget has room; a failed read clears the list): `issues` is empty and `held` is the list of issue numbers that
#                    are NOT free. An empty list here never means "everything is taken".
#           unknown  a holder check is not known (the read failed, is old, the login is unknown, the GraphQL read has not run or failed, presence could not be read, a list was cut):
#                    `issues` is empty, `reason` says which. Unknown is never free.
# `reason` is the repository's own failure (sso, no_access, not_found, rate_limited, low_budget, no_login, no_gh, error), loading, stale, identity, graph_pending, graph_failed,
# presence or truncated.

FREE_MAX = 50
OPEN_OLD_S = 3600               # a list of open issues older than this is not used: free is unknown, never guessed from an old answer
OPEN_ISSUES_MAX = 300
FREE_WORDS = {
    "loading": MESSAGES["loading"],
    "stale": "The last read of GitHub is old, so nobody can say which issues are free.",
    "identity": "This board does not know its GitHub login yet, so it cannot tell your work from a teammate's.",
    "graph_pending": "Linked branches and pull request links are not read yet, so nobody can say which issues are free.",
    "graph_failed": "Linked branches and pull request links could not be read, so nobody can say which issues are free.",
    "presence": "The presence comments could not be read, so nobody can say which issues are free.",
    "truncated": "This repository has more issues, pull requests or branches than the read keeps, so nobody can say which issues are free.",
    "unread": "The open issues of this repository have not been read yet, so free issues are not listed.",
    "open_old": "The list of open issues is old, so nobody can say which issues are free.",
    "graph_window": "This repository has more assigned issues or open pull requests than the link read covers, so nobody can say which issues are free.",
}
GQL_WINDOW = 30                 # the GraphQL read looks at this many assigned issues and this many open pull requests (app/github.py GQL, `first:30`)
GQL_OPEN_WINDOW = 100           # and at this many unassigned open issues, for their linked branches (`unassigned:issues(first:100)`); the free list is as wide as the REST page of them


def free_unknown(reason: str, held: list) -> dict:
    """A free block that lists nothing because a holder check is not known (or the repository is not readable): unknown is never free."""
    why = reason if reason in FREE_WORDS or reason in MESSAGES else "error"
    return {"state": "unknown", "reason": why, "message": FREE_WORDS.get(why) or MESSAGES.get(why, ""), "held": held, "issues": [], "count": 0, "truncated": False}


def _window_short(read: dict) -> bool:
    """Does the link read (linked branches, the issues a pull request closes) miss some work? True when either GraphQL window was full, when the pull list holds more pull requests than
    the window covers, or when a pull request of somebody who may write has no closing links after the first GraphQL read (it is outside the window, or newer than that read)."""
    full = read.get("gql_full") if isinstance(read.get("gql_full"), dict) else {}
    if full.get("issues") is True or full.get("pulls") is True:
        return True
    pulls = read.get("pulls") if isinstance(read.get("pulls"), list) else []
    return len(pulls) > GQL_WINDOW or any(trusted_pull(pr) and pr.get("closes") is None for pr in pulls[:200])


def _unclocked(read: dict, owner: str, name: str) -> set[int]:
    """The issues named by a presence line that has no clock from GitHub (word unknown) and is not ended: such a line may be live, so its issue is held, never free."""
    return {board["issue"] for _, board in _presence_person(read, owner, name, keep_gone=True) if board["issue"] and board["live"] == "unknown" and board["state"] != "ended"}


def _linked_issues(read: dict) -> set[int]:
    """The issues that have a branch linked to them, whoever linked it and whether or not the branch's author is known: such an issue is somebody's work, so it is held, never free.
    (A linked branch whose author is known already warns through `conflicts`; this also holds the one whose author is not known, which names nobody.)"""
    out: set[int] = set()
    linked = read.get("linked") if isinstance(read, dict) and isinstance(read.get("linked"), dict) else {}
    for n, branches in linked.items():
        num = number_of(n) if isinstance(n, int) else number_of(int(n)) if isinstance(n, str) and n.isdigit() else None
        if num is not None and isinstance(branches, list) and any(isinstance(lb, dict) and branch_of(lb.get("name")) for lb in branches[:5]):
            out.add(num)
    return out


def holds(read: dict, me_login: str | None, me_id: int | None, owner: str, name: str, lc: list[dict] | None = None) -> set[int]:
    """The issue numbers the owner holds: assigned to them, a live claim of theirs, a branch of theirs linked to the issue, a pull request of theirs that closes it, a presence line of
    theirs (any board of theirs, not ended, not gone or unknown) and the issue of each of this board's own open tasks (`lc`, from `_local_clean`)."""
    out: set[int] = set()
    if not isinstance(read, dict):
        return out
    for t in lc or []:
        if t.get("issue"):
            out.add(t["issue"])
    for it in (read.get("issues") or [])[:ISSUE_WARN_MAX] if isinstance(read.get("issues"), list) else []:
        n = number_of(it.get("number")) if isinstance(it, dict) else None
        if n is not None and any(same_person(user_of(a), me_login, me_id) for a in (it.get("assignees") or [])[:10]):
            out.add(n)
    for c in (read.get("claims") or [])[:100] if isinstance(read.get("claims"), list) else []:
        n = number_of(c.get("issue")) if isinstance(c, dict) else None
        if n is not None and c.get("live") is True and same_person(user_of(c.get("user")), me_login, me_id):
            out.add(n)
    for n, branches in (read.get("linked") or {}).items() if isinstance(read.get("linked"), dict) else []:
        num = number_of(n) if isinstance(n, int) else number_of(int(n)) if isinstance(n, str) and n.isdigit() else None
        if num is not None and any(same_person(user_of(lb.get("author")), me_login, me_id) for lb in (branches[:5] if isinstance(branches, list) else []) if isinstance(lb, dict)):
            out.add(num)
    for pr in (read.get("pulls") or [])[:200] if isinstance(read.get("pulls"), list) else []:
        if isinstance(pr, dict) and isinstance(pr.get("closes"), list) and same_person(user_of(pr.get("author")), me_login, me_id):
            out.update(c for c in (number_of(x) for x in pr["closes"][:5]) if c is not None)
    for user, board in _presence_person(read, owner, name, keep_gone=True):
        if same_person(user, me_login, me_id) and board["issue"] and board["live"] in WARN_LIVE and board["state"] != "ended":
            out.add(board["issue"])
    return out


def free_of(read: dict, status: str, stale: bool, warnings: dict | None, owner: str, name: str, me_login: str | None, me_id: int | None, lc: list[dict], now: float | None = None) -> dict:
    """The `free` block of one shared repository that has been read at least once (see above). `warnings` is the block's own (None when nothing was read). `read["open_issues"]` is
    the first page of the open issues nobody is assigned to; `read["open_truncated"]` says that page was full, so `truncated` is true and the list may be longer than it shows."""
    mine = holds(read, me_login, me_id, owner, name, lc)
    theirs = {int(k) for k in ((warnings or {}).get("issues") or {}) if str(k).isdigit()}
    held = sorted(mine | theirs | _unclocked(read, owner, name) | _linked_issues(read))
    pres = read.get("presence") if isinstance(read.get("presence"), dict) else None
    tr = read.get("truncated") if isinstance(read.get("truncated"), dict) else {}
    if status != "ok":
        reason = status
    elif stale:
        reason = "stale"
    elif not warnings or not warnings.get("identity_known"):
        reason = "identity"
    elif not isinstance(read.get("gql_at"), (int, float)) or isinstance(read.get("gql_at"), bool):
        reason = "graph_pending"
    elif read.get("graph_error"):
        reason = "graph_failed"
    elif pres is not None and (pres.get("status") == "error" or pres.get("editor_check") == "failed"):
        reason = "presence"
    elif any(tr.get(k) is True for k in ("issues", "pulls", "tips")) or (pres is not None and pres.get("truncated") is True):
        reason = "truncated"
    elif _window_short(read):
        reason = "graph_window"
    else:
        reason = None
    if reason is not None:
        return free_unknown(reason, held)
    raw = read.get("open_issues")
    if not isinstance(raw, list):
        return {"state": "unread", "reason": "unread", "message": FREE_WORDS["unread"], "held": held, "issues": [], "count": 0, "truncated": False}
    at = read.get("open_at")
    if now is not None and isinstance(at, (int, float)) and not isinstance(at, bool) and now - at > OPEN_OLD_S:
        return free_unknown("open_old", held)
    taken = set(held)
    cover = read.get("open_cover") if isinstance(read.get("open_cover"), dict) else None          # the unassigned issues the link read looked at (issue #200)
    covered = {c for c in (cover.get("numbers") or []) if isinstance(c, int) and not isinstance(c, bool)} if cover and isinstance(cover.get("numbers"), list) else None
    cut = False
    rows = []
    for it in raw[:OPEN_ISSUES_MAX]:
        n = number_of(it.get("number")) if isinstance(it, dict) else None
        if n is None or n in taken or "pull_request" in it or (isinstance(it.get("assignees"), list) and it["assignees"]):
            continue
        if covered is not None and cover.get("full") is True and n not in covered:
            cut = True                                                     # the link read's page of unassigned issues was full and did not reach this one: its linked branches are not known, so it is not listed
            continue
        labels = [text(x, 50) for x in (it.get("labels") or [])[:_LABEL_MAX] if text(x, 50)] if isinstance(it.get("labels"), list) else []
        if PRESENCE_LABEL in labels:
            continue                                                       # the presence status issue is machinery, never work to pick
        rows.append({"number": n, "title": text(it.get("title")), "url": issue_url(owner, name, n), "labels": labels, "updated_at": iso_of(it.get("updated_at"))})
    rows.sort(key=lambda r: (-(_epoch(r["updated_at"]) or 0), r["number"]))
    return {"state": "ok", "reason": None, "message": "", "held": held, "issues": rows[:FREE_MAX], "count": len(rows), "truncated": len(rows) > FREE_MAX or read.get("open_truncated") is True or cut}


# ---------------------------------------------------------------- the whole team in one list (issue #200)
#
# `overview(repos)` takes the blocks `repo_state` made and returns the one view the sidebar group, the Home line and the Team page read:
#   {api, counts{shared, ok, loading, problem, teammates, live, active_repos, rows, free{count, ok, unread, unknown}, truncated},
#    people[{key, id, login, you, live, last_seen_s, repos[slug], rows}],
#    rows[{person, login, you, repo_slug, slug, repo_url, read_at, stale, kind, issue, issue_url, title, branch, branch_url, pr, pr_url, draft, agent, state, live, since, last,
#          last_seen_s, claim, collides}]}
# A person is one person across repositories (the numeric id, else the lower-case login). `you` is the owner: listed, never counted as a teammate. A row is one piece of work:
# a board's presence line (kind presence), an assigned issue (issue), a pull request (pr), a pushed or linked branch (branch) or, when GitHub names a person for nothing else, kind seen.
# `state` says it in one word: working and waiting (a live board), idle, unconfirmed (a first sighting), stale, ended, unknown (no clock from GitHub), or github (GitHub shows the work,
# no board of that person publishes presence). A repository whose read is old or failed shows its rows as stale and counts nobody as live: an old answer is never "live". Nothing in
# the overview depends on the clock, so the ETag does not move between two reads.

ROW_STATES = ("working", "waiting", "idle", "unconfirmed", "stale", "ended", "unknown", "github")
_ROW_RANK = {s: i for i, s in enumerate(ROW_STATES)}
ROW_KINDS = ("presence", "issue", "pr", "branch", "seen")
ROWS_MAX = 600
OVERVIEW_PEOPLE_MAX = 200
_LIVEISH = ("live", "idle", "unconfirmed")


def _row_state(word: str | None, pstate: str | None) -> str:
    if word is None:
        return "github"
    if pstate == "ended" and word in (*_LIVEISH, "stale"):
        return "ended"
    if word == "live":
        return "working" if pstate == "running" else "waiting"
    return word if word in ("idle", "unconfirmed", "stale", "unknown") else "stale"


def _pkey(p: dict, ids: dict) -> str:
    uid = p.get("id")
    if uid is None and p.get("login"):
        uid = ids.get(p["login"].lower())
    return f"id:{uid}" if uid is not None else f"login:{(p.get('login') or '').lower()}"


def _empty_row(base: dict, kind: str, **kw) -> dict:
    r = {**base, "kind": kind, "issue": None, "issue_url": None, "title": None, "branch": None, "branch_url": None, "pr": None, "pr_url": None, "draft": None, "agent": None,
         "state": "github", "live": None, "since": None, "last": None, "last_seen_s": None, "claim": None, "collides": False}
    r.update(kw)
    return r


def _rows_of(b: dict, p: dict, pk: str) -> list[dict]:
    """The rows one person has in one repository block."""
    so, _, sn = b["slug"].partition("/")
    stale = bool(b.get("stale"))
    base = {"person": pk, "login": p["login"], "you": bool(p["you"]), "repo_slug": b["repo_slug"], "slug": b["slug"], "repo_url": b["url"], "read_at": b["read_at"], "stale": stale}
    issues = {i["number"]: i for i in p["issues"]}
    prs_for: dict = {}
    for pr in p["prs"]:
        for c in pr.get("closes") or []:
            prs_for.setdefault(c, pr)
    used_issue, used_branch, used_pr = set(), set(), set()
    out: list[dict] = []

    def attach_pr(row: dict, n) -> None:
        pr = prs_for.get(n) if n else None
        if pr is None:
            return
        row.update(pr=pr["number"], pr_url=pr["url"], draft=pr["draft"])
        row["collides"] = row["collides"] or bool(pr.get("collides"))
        used_pr.add(pr["number"])
        if pr.get("head"):
            used_branch.add(pr["head"])

    def claim_of(it) -> dict | None:
        return {"status": it["claim"]["status"], "live": it["claim"]["live"]} if it and it.get("claim") else None

    pres = p.get("presence")
    seen_board = set()
    for bd in sorted((pres or {}).get("boards") or [], key=lambda x: (_LIVE_RANK.get(x["live"], 9), x["last_seen_s"] if x["last_seen_s"] is not None else 10 ** 9)):
        k = (bd["issue"], bd["branch"])
        if k in seen_board:
            continue                                                       # two boards of one person on the same work: one row, the livelier one
        seen_board.add(k)
        word = "stale" if stale and bd["live"] in _LIVEISH else bd["live"]
        it = issues.get(bd["issue"]) if bd["issue"] else None
        row = _empty_row(base, "presence", issue=bd["issue"], issue_url=bd["issue_url"], title=it["title"] if it else None, branch=bd["branch"], branch_url=bd["branch_url"],
                         agent=bd["agent"], state=_row_state(word, bd["state"]), live=word, since=bd["since"], last=bd["last"], last_seen_s=bd["last_seen_s"],
                         claim=claim_of(it), collides=bool(it and it.get("collides")))
        if it:
            used_issue.add(it["number"])
            used_branch.update(it["branches"])
        if bd["branch"]:
            used_branch.add(bd["branch"])
        attach_pr(row, bd["issue"])
        out.append(row)
    for it in p["issues"]:
        if it["number"] in used_issue:
            continue
        br = it["branches"][0] if it["branches"] else None
        row = _empty_row(base, "issue", issue=it["number"], issue_url=it["url"], title=it["title"], branch=br, branch_url=branch_url(so, sn, br) if br else None,
                         last=it["updated_at"], claim=claim_of(it), collides=bool(it.get("collides")))
        used_issue.add(it["number"])
        used_branch.update(it["branches"])
        attach_pr(row, it["number"])
        out.append(row)
    for pr in p["prs"]:
        if pr["number"] in used_pr:
            continue
        n = pr["closes"][0] if pr.get("closes") else None
        out.append(_empty_row(base, "pr", issue=n, issue_url=issue_url(so, sn, n) if n else None, branch=pr["head"], branch_url=branch_url(so, sn, pr["head"]) if pr.get("head") else None,
                              pr=pr["number"], pr_url=pr["url"], title=pr["title"], draft=pr["draft"], last=pr["updated_at"], collides=bool(pr.get("collides"))))
        used_pr.add(pr["number"])
        if pr.get("head"):
            used_branch.add(pr["head"])
    for br in p["branches"]:
        if br["name"] in used_branch:
            continue
        used_branch.add(br["name"])
        n = br.get("issue")
        out.append(_empty_row(base, "branch", issue=n, issue_url=issue_url(so, sn, n) if n else None, branch=br["name"], branch_url=br["url"], last=br["at"], collides=bool(br.get("collides"))))
    if not out:
        out.append(_empty_row(base, "seen", last=p["last_active"]))
    return out


def overview(repos: list[dict]) -> dict:
    """The one view of every shared repository (see above). `repos` are the blocks `repo_state` made; a repository that is not shared adds nothing."""
    shared = [b for b in repos if b.get("shared")]
    ids: dict = {}
    for b in shared:
        for p in b.get("people") or []:
            if p.get("id") is not None and p.get("login"):
                ids[p["login"].lower()] = p["id"]
    rows: list[dict] = []
    people: dict = {}
    live_people: set = set()
    active_repos = 0
    for b in shared:
        counted = b.get("state") == "ok" and not b.get("stale")
        repo_live = False
        for p in b.get("people") or []:
            pk = _pkey(p, ids)
            mine = _rows_of(b, p, pk)
            rows += mine
            ent = people.setdefault(pk, {"key": pk, "id": p.get("id") if p.get("id") is not None else ids.get((p.get("login") or "").lower()), "login": p.get("login"), "you": False,
                                         "live": "none", "last_seen_s": None, "repos": [], "rows": 0})
            ent["you"] = ent["you"] or bool(p["you"])
            ent["login"] = ent["login"] or p.get("login")
            ent["rows"] += len(mine)
            if b["slug"] not in ent["repos"]:
                ent["repos"].append(b["slug"])
            words = [r for r in mine if r["live"] is not None]
            if words:
                best = min(words, key=lambda r: (_LIVE_RANK.get(r["live"], 9), r["last_seen_s"] if r["last_seen_s"] is not None else 10 ** 9))
                if _LIVE_RANK.get(best["live"], 9) < _LIVE_RANK.get(ent["live"], 9):
                    ent["live"], ent["last_seen_s"] = best["live"], best["last_seen_s"]
            if counted and not p["you"] and any(r["live"] in ("live", "idle") for r in mine):
                live_people.add(pk)
                repo_live = True
        active_repos += 1 if repo_live else 0
    rows.sort(key=lambda r: (not r["you"], _ROW_RANK[r["state"]], -(_epoch(r["last"]) or 0), (r["login"] or "").lower(), r["slug"].lower(), r["issue"] or 0, r["branch"] or "", r["pr"] or 0))
    plist = sorted(people.values(), key=lambda e: (not e["you"], _LIVE_RANK.get(e["live"], 9), (e["login"] or "").lower(), e["id"] or 0))
    cut = len(rows) > ROWS_MAX or len(plist) > OVERVIEW_PEOPLE_MAX
    frees = [b["free"] for b in shared if isinstance(b.get("free"), dict)]
    counts = {"shared": len(shared), "ok": sum(1 for b in shared if b.get("state") == "ok"), "loading": sum(1 for b in shared if b.get("state") == "loading"),
              "problem": sum(1 for b in shared if b.get("state") not in ("ok", "loading")),
              "teammates": sum(1 for e in people.values() if not e["you"]), "live": len(live_people), "active_repos": active_repos, "rows": len(rows),
              "free": {"count": sum(f["count"] for f in frees if f["state"] == "ok"), "ok": sum(1 for f in frees if f["state"] == "ok"),
                       "unread": sum(1 for f in frees if f["state"] == "unread"), "unknown": sum(1 for f in frees if f["state"] == "unknown")},
              "truncated": cut}
    return {"api": 1, "counts": counts, "people": plist[:OVERVIEW_PEOPLE_MAX], "rows": rows[:ROWS_MAX]}


# ---------------------------------------------------------------- a teammate started on an issue the owner holds (issue #200): change detection between two reads
#
# `starts_pairs` lists the teammates who are on each issue in one read; `Watch` keeps the previous list per repository and the pairs already told. The first read of a repository (and
# the first one after an outage of an hour, or after the repository was switched off) only becomes the baseline: nothing is told for what was already there. A pair (teammate, issue)
# counts as present while the Team P3 rules have a taken or recent warning for it, which already leaves out the owner's own logins and boards, a pull request of somebody who may not
# write to the repository, a gone or unknown presence line and an unnamed user. The notice is told for a pair that is new and whose issue the owner holds.
# Nothing here reads GitHub or sends anything: the events go to app/notify.py, which sends only when the owner switched the kind on.

START_MAX = 5                    # notices one read may bring
START_GAP_S = 3600.0             # two reads further apart than this are not "successive"
TOLD_MAX = 2000
START_STATUS = ("taken", "recent")


def starts_pairs(read: dict, me_login: str | None, me_id: int | None, owner: str, name: str, now: float) -> tuple[bool, dict]:
    """(identity known, {(person key, issue): login}) of the teammates present on an issue in this read, by the Team P3 rules."""
    if not isinstance(read, dict):
        return False, {}
    warnings, _ = conflicts(read, me_login, me_id, owner, name, now, [])
    if not warnings["identity_known"]:
        return False, {}
    out: dict = {}
    for k, ws in warnings["issues"].items():
        for w in ws:
            if w.get("status") not in START_STATUS:
                continue
            who = w.get("person") or {}
            pk = ("id", who["id"]) if who.get("id") is not None else ("login", (who.get("login") or "").lower())
            out[(pk, int(k))] = who.get("login")
    return True, out


def starts_from(prev: set | None, cur: dict, mine: set, me_login: str | None, me_id: int | None) -> list[dict]:
    """The new pairs of `cur` (a {(person key, issue): login} map) that are not in `prev`, on an issue in `mine`: [{login, issue, person}]. Nothing for the first read (`prev` None),
    for the owner's own login or id, for an app (a [bot] login) or for a person with no login to name."""
    if prev is None:
        return []
    out = []
    for (pk, n), login in sorted(cur.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        if (pk, n) in prev or n not in mine:
            continue
        who = {"id": pk[1] if pk[0] == "id" else None, "login": login}
        if same_person(who, me_login, me_id) or (me_login and isinstance(login, str) and login.lower() == me_login.lower()):
            continue                                                       # the owner and every board under the owner's login never tell the owner about themselves
        lg = login_of(login)
        if lg is None or lg.lower().endswith("[bot]"):
            continue
        out.append({"login": lg, "issue": n, "person": pk})
    return out


class Watch:
    """The memory of the change detection: the pairs of the last read of each repository and the pairs already told. In memory only: a restart starts from a new baseline."""

    def __init__(self):
        self.lock = threading.Lock()
        self.prev: dict = {}
        self.at: dict = {}
        self.told: dict = {}

    def forget(self, key: str | None = None) -> None:
        with self.lock:
            if key is None:
                self.prev.clear()
                self.at.clear()
                self.told.clear()
            else:
                self.prev.pop(key, None)
                self.at.pop(key, None)
                for k in [k for k in self.told if k[0] == key]:
                    del self.told[k]

    @staticmethod
    def _incomplete(read: dict) -> bool:
        """Is a part of this read missing? Presence that could not be read, its editors not checked or its comments cut, a cut list, or a link read that misses work."""
        pres = read.get("presence") if isinstance(read.get("presence"), dict) else None
        if pres is not None and (pres.get("status") not in ("ok", "none", "closed") or pres.get("editor_check") == "failed" or pres.get("truncated") is True):
            return True
        tr = read.get("truncated") if isinstance(read.get("truncated"), dict) else {}
        return any(tr.get(k) is True for k in ("issues", "pulls", "tips")) or _window_short(read)

    def step(self, key: str, read: dict, me_login: str | None, me_id: int | None, owner: str, name: str, now: float, lc: list[dict] | None = None) -> list[dict]:
        """The pairs to tell for one finished read of `key` (usually []). The baseline moves on at every read that is ok, has been through the GraphQL read and knows the owner's
        login, whatever the kind's switch says, so switching the kind on later tells only what starts afterwards."""
        if not isinstance(read, dict) or read.get("status") != "ok" or not isinstance(read.get("read_at"), (int, float)) or isinstance(read.get("read_at"), bool):
            return []
        if not isinstance(read.get("gql_at"), (int, float)) or isinstance(read.get("gql_at"), bool) or read.get("graph_error"):
            return []                                                      # linked branches and pull request links are not known yet: what they add later is not a start
        if self._incomplete(read):
            return []                                                      # a part of the read is missing (presence lines dropped, a list cut): it is not a baseline, and the good read after it is not a start
        known, cur = starts_pairs(read, me_login, me_id, owner, name, now)
        if not known:
            return []                                                      # the owner is unknown: nobody can be told from the owner's own work
        at = float(read["read_at"])
        with self.lock:
            last = self.at.get(key)
            if last is not None and at <= last:
                return []                                                  # the same read again
            prev = self.prev.get(key)
            if prev is not None and last is not None and at - last > START_GAP_S:
                prev = None
            self.prev[key], self.at[key] = set(cur), at
        events = starts_from(prev, cur, holds(read, me_login, me_id, owner, name, lc), me_login, me_id)
        out = []
        with self.lock:
            for ev in events:
                tk = (key, ev["person"], ev["issue"])
                if tk in self.told:
                    continue
                self.told[tk] = at
                while len(self.told) > TOLD_MAX:
                    self.told.pop(next(iter(self.told)))
                out.append(ev)
        return out[:START_MAX]


watch = Watch()


# ---------------------------------------------------------------- the body

def repo_state(entry: dict, read: dict | None, me_login: str | None, me_id: int | None, now: float, local=None) -> dict:
    """The block of one repository: `entry` is what this board knows of its own repository ({key, slug, local[{project, repo}], shared}), `read` what GitHub said (None: not read).
    `local` is this board's own open tasks of the repository (issue #199: [{id, title, issue_number, branch, files[]|None}], from app/main.py): it only marks rows and fills the
    task lookup of `warnings`; it is shown to the signed-in person who owns the board and is never published."""
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
            "truncated": {"issues": False, "pulls": False, "tips": False}, "warnings": None, "free": None}
    if not out["shared"]:
        return {**out, **base, "presence": None}
    if read is None or read.get("status") == "pending" and not read.get("read_at"):
        return {**out, **base, "presence": None, "state": "loading", "message": MESSAGES["loading"], "free": free_unknown("loading", [])}
    status = read.get("status") if read.get("status") in STATES else "error"
    read_at = read.get("read_at") if isinstance(read.get("read_at"), (int, float)) and not isinstance(read.get("read_at"), bool) else None
    age = max(0, int(now - read_at)) if read_at is not None else None
    people, counts = people_of(read, me_login, me_id, so, sn, now) if read_at is not None else ([], base["counts"])
    warnings, lc = None, _local_clean(local)
    if read_at is not None:                                                  # issue #199: the advice for the launcher, from the same read; no network
        warnings, hits = conflicts(read, me_login, me_id, so, sn, now, lc)
        mark_rows(people, lc, hits)
    stale = status != "ok" or age is None or age > STALE_S
    free = free_of(read, status, stale, warnings, so, sn, me_login, me_id, lc, now) if read_at is not None else free_unknown(status if status != "ok" else "loading", [])          # issue #200: from the same read, no network
    wait = read.get("wait_s") if isinstance(read.get("wait_s"), int) and not isinstance(read.get("wait_s"), bool) and read.get("wait_s") > 0 else None
    msg = MESSAGES[status] if status != "rate_limited" or not wait else f"GitHub asked this board to wait {wait} s."
    gql_at = read.get("gql_at") if isinstance(read.get("gql_at"), (int, float)) and not isinstance(read.get("gql_at"), bool) else None
    tr = read.get("truncated") if isinstance(read.get("truncated"), dict) else {}
    pres = read.get("presence") if isinstance(read.get("presence"), dict) else None
    pn = number_of(pres.get("issue")) if pres else None
    presence = {"state": pres.get("status") if pres.get("status") in READ_PRESENCE else "error", "issue": pn, "issue_url": issue_url(so, sn, pn) if pn and so else None,
                "truncated": pres.get("truncated") is True, "editor_check": pres.get("editor_check") if pres.get("editor_check") in ("ok", "failed", "none") else "none",
                "message": READ_PRESENCE.get(pres.get("status"), READ_PRESENCE["error"])} if pres else None
    return {**out, "presence": presence, "state": status, "message": msg, "wait_s": wait, "stale": stale,
            "read_at": _iso_now(read_at) if read_at is not None else None, "age_s": age, "gql_at": _iso_now(gql_at) if gql_at is not None else None,
            "graph_pending": gql_at is None, "graph_failed": bool(read.get("graph_error")), "private": read.get("private") if isinstance(read.get("private"), bool) else None, "people": people, "counts": counts,
            "truncated": {k: tr.get(k) is True for k in ("issues", "pulls", "tips")}, "warnings": warnings, "free": free}


def build(entries: list[dict], reads: dict, me_login: str | None, me_id: int | None, now: float, local=None) -> dict:
    """The body of GET /api/team/state: {api, enabled, me, repos[...], overview, at} (`overview`: the people of every shared repository in one list, issue #200). `enabled` is true when at least one repository is shared. `me` is null while the board does not know
    which GitHub login it acts as (unknown, not "nobody")."""
    loc = local if isinstance(local, dict) else {}
    repos = [repo_state(e, reads.get(e.get("key")), me_login, me_id, now, loc.get(e.get("key"))) for e in entries]
    repos.sort(key=lambda r: (not r["shared"], r["slug"].lower()))
    return {"api": API, "enabled": any(r["shared"] for r in repos),
            "me": {"login": me_login, "id": me_id} if me_login else None, "repos": repos, "overview": overview(repos), "at": _iso_now(now)}


def digest(body: dict) -> str:
    """The ETag base: the canonical JSON of the body without `at` and without each repository's `age_s` (the page works the age out from `read_at`)."""
    b = {k: v for k, v in body.items() if k != "at"}
    b["repos"] = [{k: v for k, v in r.items() if k != "age_s"} for r in body.get("repos") or []]
    return hashlib.sha256(json.dumps(b, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()[:32]


def etag(body: dict) -> str:
    return f'W/"{digest(body)}"'
