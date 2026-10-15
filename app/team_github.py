"""The team reader (issue #197, Team P1): what GitHub already shows about teammates in a shared repository, read through the box's own gh. Read only.

Off means off. Nothing in this module runs, starts a thread, asks GitHub or writes a row for a repository the owner has not switched on, and a repository can be switched on
only if it is one of this board's own projects (their `origin` and `upstream` remotes). A peer, a GitHub answer and a request never choose which repository is read.

What is read for a shared repository (a GET each; GraphQL is the one POST on the wire gh sends for a query and it writes nothing, see app/github.py):
  - /repos/{o}/{r}                         once an hour, with an ETag: the repository's full name, whether it is a fork and its parent (a fork is read as its parent)
  - open issues with an assignee           REST, 100 a page, ETag (a 304 changes nothing and is the cheap case)
  - open pull requests                     REST, ETag; this is where a teammate's fork shows up (head repository)
  - claim comments of #148                 REST, ETag, only for issues labelled ccboard:in-progress, parsed by nodes_epics.parse_claim; absent is fine
  - linked branches, closing issues, recent branch tips with their author: one GraphQL query per repository, slowly (GQL_S), earlier only when the REST lists changed.
No `git fetch`: the owner's checkout is never touched. No write of any kind.

The poll has the lowest priority: one thread, one call at a time with MIN_GAP between calls, a budget of BUDGET_PER_HOUR points an hour for the whole team poll (a 304 is counted as
a point: whether it is free is UNVERIFIED), longer intervals as X-RateLimit-Remaining falls, no calls at all below STOP_BELOW (until the limit resets), and a yield to the epic reader
while GitHub has asked that one to wait. The 5,000 points an hour belong to the login, and prpoll, the backup push and every other gh call spend them too.

What it reads is kept in memory (and a fork's parent or a renamed repository's new name in one kv row per shared repository, `team:meta:`); nothing else is stored. Everything a read
returns is text written by other people: it is cut, stripped of control characters and kept as text before it goes anywhere (app/github.py `clean_text`, app/team.py).
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from pathlib import Path

from . import github, node_state, nodes_epics, prpoll, team
from .config import settings

log = logging.getLogger("ccboard.team")

REST_S = 120.0                    # seconds between two REST reads of one repository (conditional: a 304 changes nothing)
GQL_S = 600.0                     # seconds between two GraphQL reads of one repository
GQL_MIN_S = 150.0                 # but a changed REST list brings the GraphQL read forward, not closer than this to the last one
META_S = 3600.0                   # the repository's own record (fork, parent, new name)
CLAIMS_S = 600.0                  # claim comments are looked at this often, or when the issue list changed
MIN_GAP = 1.5                     # seconds between two gh calls of the team poll
BUDGET_PER_HOUR = 600             # points an hour the whole team poll may spend, however many repositories are shared
BUDGET_WINDOW = 3600.0
STOP_BELOW = 300                  # X-RateLimit-Remaining under this: no call until the limit resets
SLOW_BELOW = ((1500, 4), (3000, 2))        # remaining under N: every interval is k times longer
ERROR_RETRY_S = 600.0             # a repository GitHub refuses (SSO, no access, not found) is tried again this often, not every poll
SOFT_RETRY_S = 120.0
REST_PAGES = 3
ISSUES_MAX = 200
PULLS_MAX = 100
TIPS_KEEP = 60
CLAIM_ISSUES = 5                  # issues whose comments one cycle reads
REPO_CAP = 100
WAIT_MAX = 3600
KV_META = "team:meta:"
IN_PROGRESS = nodes_epics.IN_PROGRESS_LABEL
REFRESH_MIN_S = 60.0              # the refresh button is answered at most this often
GQL_COST_GUESS = 2                # points reserved for a GraphQL read before GitHub says what it cost

REASON_STATE = {"sso": "sso", "no_access": "no_access", "not_found": "not_found", "rate_limited": "rate_limited", "no_login": "no_login", "no_gh": "no_gh"}
HARD = ("sso", "no_access", "not_found", "no_login", "no_gh")
_spelled: dict[str, str] = {}               # canonical key -> `owner/name` as GitHub spells it


def _person(pair) -> tuple[str | None, int | None]:
    login = team.login_of(pair[0])
    return (login, team.user_id(pair[1])) if login else (None, None)


def identity() -> tuple[str | None, int | None]:
    """(login, numeric user id) the board's gh acts as: `gh api user`, cached 600 s in app/prpoll.py, the login checked against GitHub's rules. The one seam to it (tests replace it,
    as they do nodes_epics._gh_login). (None, None) is "not known"."""
    cwd = Path(settings.projects_dir)
    return _person(prpoll.gh_identity(cwd if cwd.is_dir() else Path(settings.data_dir)))


def identity_cached() -> tuple[str | None, int | None]:
    """What the last probe said, with no gh call."""
    return _person(prpoll.gh_identity_cached())


_ident_lock = threading.Lock()
_ident_thread: threading.Thread | None = None


def _probe_identity() -> None:
    try:
        identity()
    except Exception as e:                                  # the probe fills prpoll's cache; a failure is retried by the next request after the cache's short failure memory
        log.debug("login probe failed: %s", e.__class__.__name__)


def identity_nowait() -> tuple[str | None, int | None]:
    """The identity for a request that must not wait on gh (the node card, which a hub polls with a 5 s timeout): what is cached. A cold cache starts ONE background probe (any
    number of callers share it) and answers (None, None) now; the next card has the login."""
    global _ident_thread
    got = identity_cached()
    if got[0]:
        return got
    with _ident_lock:
        if _ident_thread is None or not _ident_thread.is_alive():
            _ident_thread = threading.Thread(target=_probe_identity, name="team-identity", daemon=True)
            _ident_thread.start()
    return got


# ---------------------------------------------------------------- the board's own repositories

def own_repos(projects_list, shared_keys=()) -> list[dict]:
    """One entry per repository of THIS board's own projects (their `upstream`, else `origin`, remote on github.com), merged by canonical key:
    {key, slug (as spelled), local[{project, repo}], shared, members[local keys], names[lower-case slugs the entry answers to]}. `shared_keys` are the switch rows (local keys).
    Local git facts only: no gh call. Nothing a peer or GitHub says is in it."""
    shared = set(shared_keys)
    by: dict[str, dict] = {}
    for p in projects_list or []:
        if not isinstance(p, dict):
            continue
        for r in p.get("repos") or []:
            if not (isinstance(r, dict) and r.get("state") == "ok" and r.get("path")):
                continue
            origin, upstream = node_state.remote_slugs(r["path"])
            base = upstream or origin
            o, _, n = (base or "").partition("/")
            if not base or not github.valid_slug(o, n):
                continue
            local_key = team.key_of(o, n)
            key = team.hinted(local_key)
            e = by.get(key)
            if e is None:
                if len(by) >= REPO_CAP:
                    continue
                slug = _spelled.get(key) or (base if key == local_key else "/".join(team.split_key(key) or ()))
                e = by[key] = {"key": key, "slug": slug, "local": [], "shared": False, "members": [], "names": set()}
            if local_key not in e["members"]:
                e["members"].append(local_key)
            e["names"].update({base.lower(), (origin or base).lower(), key[len(team.HOST) + 1:]})
            e["shared"] = e["shared"] or local_key in shared
            if len(e["local"]) < 20:
                e["local"].append({"project": p.get("name"), "repo": r.get("name")})
    return list(by.values())


def find_entry(entries: list[dict], owner, repo) -> dict | None:
    """The entry a route's `owner/repo` names, among this board's own repositories (case does not matter); None if this board has no such project."""
    if not github.valid_slug(owner, repo):
        return None
    want = f"{owner}/{repo}".lower()
    return next((e for e in entries if want in e["names"]), None)


# ---------------------------------------------------------------- a rolling hour of points

class Budget:
    """The points the team poll spent in the last BUDGET_WINDOW seconds."""

    def __init__(self, per_hour: float = BUDGET_PER_HOUR, window: float = BUDGET_WINDOW):
        self.per_hour, self.window = per_hour, window
        self.events: collections.deque = collections.deque()

    def _trim(self, now: float) -> None:
        while self.events and now - self.events[0][0] >= self.window:
            self.events.popleft()

    def spent(self, now: float) -> float:
        self._trim(now)
        return sum(c for _, c in self.events)

    def add(self, now: float, cost: float) -> None:
        self.events.append((now, cost))

    def room(self, now: float, cost: float) -> bool:
        return self.spent(now) + cost <= self.per_hour

    def wait(self, now: float, cost: float) -> float:
        """Seconds until `cost` more points fit (the oldest spending ages out of the window)."""
        self._trim(now)
        total = sum(c for _, c in self.events) + cost - self.per_hour
        if total <= 0:
            return 0.0
        for t, c in self.events:
            total -= c
            if total <= 0:
                return max(0.0, t + self.window - now)
        return self.window


class _Defer(Exception):
    def __init__(self, wait: float):
        self.wait = wait


class _Low(Exception):
    def __init__(self, wait: float):
        self.wait = wait


# ---------------------------------------------------------------- parsing what GitHub sent (untrusted)

def _labels(it) -> list[str]:
    out = []
    for lb in (it.get("labels") or [])[:30] if isinstance(it.get("labels"), list) else []:
        name = lb.get("name") if isinstance(lb, dict) else lb
        t = team.text(name, 50)
        if t:
            out.append(t)
    return out


def parse_issues(items) -> list[dict]:
    """The open issues somebody is assigned to: a pull request GitHub lists among the issues is left out (it comes from the pull list)."""
    out = []
    for it in items[:ISSUES_MAX * 2] if isinstance(items, list) else []:
        if not isinstance(it, dict) or "pull_request" in it:
            continue
        n = team.number_of(it.get("number"))
        who = [u for u in (team.user_of(a) for a in (it.get("assignees") or [])[:10]) if u] if isinstance(it.get("assignees"), list) else []
        if n is None or not who:
            continue
        out.append({"number": n, "title": team.text(it.get("title")), "labels": _labels(it), "assignees": who, "updated_at": team.iso_of(it.get("updated_at"))})
        if len(out) >= ISSUES_MAX:
            break
    return out


def parse_pulls(items, target: str, closes: dict | None = None) -> list[dict]:
    out = []
    for pr in items[:PULLS_MAX * 2] if isinstance(items, list) else []:
        if not isinstance(pr, dict):
            continue
        n = team.number_of(pr.get("number"))
        if n is None:
            continue
        head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        repo = head.get("repo") if isinstance(head.get("repo"), dict) else None
        full = team.text(repo.get("full_name"), 140) if repo else ""
        o, _, r = full.partition("/")
        full = full if github.valid_slug(o, r) else ""
        out.append({"number": n, "title": team.text(pr.get("title")), "draft": pr.get("draft") is True, "author": team.user_of(pr.get("user")),
                    "head_ref": team.branch_of(head.get("ref")), "head_repo": full or None, "fork": bool(full) and full.lower() != target.lower(),
                    "updated_at": team.iso_of(pr.get("updated_at")), "closes": (closes or {}).get(n)})
        if len(out) >= PULLS_MAX:
            break
    return out


def _gql_user(u) -> dict | None:
    return team.user_of({"id": u.get("databaseId"), "login": u.get("login")}) if isinstance(u, dict) else None


def _tip(ref) -> dict | None:
    if not isinstance(ref, dict):
        return None
    name = team.branch_of(ref.get("name"))
    tgt = ref.get("target") if isinstance(ref.get("target"), dict) else {}
    au = tgt.get("author") if isinstance(tgt.get("author"), dict) else {}
    return {"name": name, "at": team.iso_of(tgt.get("committedDate")), "author": _gql_user(au.get("user"))} if name else None


def parse_graph(data) -> dict | None:
    """What the GraphQL answer gives: {linked{issue: [branch]}, closes{pull: [issue]}, tips[...], tips_truncated, cost, remaining}, or None when `repository` is missing."""
    d = data.get("data") if isinstance(data, dict) else None
    repo = d.get("repository") if isinstance(d, dict) else None
    if not isinstance(repo, dict):
        return None
    linked, closes, tips = {}, {}, []
    for it in ((repo.get("issues") or {}).get("nodes") or [])[:60]:
        n = team.number_of(it.get("number")) if isinstance(it, dict) else None
        if n is None:
            continue
        for lb in ((it.get("linkedBranches") or {}).get("nodes") or [])[:5]:
            t = _tip((lb or {}).get("ref"))
            if t:
                linked.setdefault(n, []).append(t)
    for pr in ((repo.get("pullRequests") or {}).get("nodes") or [])[:60]:
        n = team.number_of(pr.get("number")) if isinstance(pr, dict) else None
        if n is not None:
            closes[n] = [c for c in (team.number_of(x.get("number")) for x in ((pr.get("closingIssuesReferences") or {}).get("nodes") or [])[:5] if isinstance(x, dict)) if c]
    refs = repo.get("refs") if isinstance(repo.get("refs"), dict) else {}
    for ref in (refs.get("nodes") or [])[:100]:
        t = _tip(ref)
        if t:
            tips.append(t)
    tips.sort(key=lambda t: t["at"] or "", reverse=True)
    rl = d.get("rateLimit") if isinstance(d.get("rateLimit"), dict) else {}
    cost = rl.get("cost") if isinstance(rl.get("cost"), int) and not isinstance(rl.get("cost"), bool) and 0 < rl.get("cost") < 1000 else 1
    rem = rl.get("remaining") if isinstance(rl.get("remaining"), int) and not isinstance(rl.get("remaining"), bool) and rl.get("remaining") >= 0 else None
    return {"linked": linked, "closes": closes, "tips": tips[:TIPS_KEEP], "tips_truncated": bool((refs.get("pageInfo") or {}).get("hasNextPage")) or len(tips) > TIPS_KEEP,
            "cost": cost, "remaining": rem}


def parse_claims(comments, issue: int, server_now: float | None) -> list[dict]:
    """The #148 claim comments of one issue, one per person (the latest), read side only. A comment that is not a claim, whose user is not a User, or whose author is not an owner,
    member or collaborator (GitHub's author_association, as the epic reader asks) is ignored: on a public repository anybody can comment, and presence is advice that must not be forged."""
    best: dict = {}
    for c in comments[:100] if isinstance(comments, list) else []:
        if not isinstance(c, dict):
            continue
        claim = nodes_epics.parse_claim(c.get("body")) if c.get("author_association") in nodes_epics.ASSOCIATIONS else None
        user = c.get("user") if isinstance(c.get("user"), dict) and c["user"].get("type") == "User" else None
        who = team.user_of(user)
        upd = team.iso_of(c.get("updated_at"))
        if not claim or not who or not upd:
            continue
        k = who["id"] if who["id"] is not None else who["login"]
        if k in best and (best[k]["updated_at"] or "") > upd:
            continue
        live = nodes_epics.is_live({"status": claim["status"], "updated_at": upd, "lease_min": claim["lease_min"]}, server_now)
        best[k] = {"issue": issue, "user": who, "status": claim["status"], "branch": claim.get("branch"), "live": live, "updated_at": upd}
    return list(best.values())


def new_read(slug: str) -> dict:
    return {"slug": slug, "status": "pending", "reason": None, "message": None, "wait_s": None, "read_at": None, "gql_at": None, "private": None, "fork": None,
            "default_branch": None, "issues": [], "pulls": [], "linked": {}, "tips": [], "claims": [], "closes": {}, "claims_at": 0.0, "graph_error": None,
            "truncated": {"issues": False, "pulls": False, "tips": False}}


# ---------------------------------------------------------------- the doctor's facts

def doctor_facts(slugs: list[str], limit: int = 3) -> dict:
    """What the Doctor row "Team GitHub access" reports, read only: {state, scopes, remaining, limit, failed{slug: reason}, checked}. `state` is ok, no_gh, no_login or error:<reason>.
    One /rate_limit call and one repository read per shared repository (at most `limit`), through the same GET-only reader the epics use. Nothing is written."""
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
    out["scopes"] = sorted({x.strip() for x in rep.scopes.split(",") if x.strip()}) if isinstance(rep.scopes, str) else None
    for slug in slugs[:limit]:
        o, _, r = slug.partition("/")
        try:
            github.reader.get(github.repo_path(o, r), 0.0)
            out["checked"] += 1
        except github.GhReadError as e:
            out["failed"][slug] = e.reason
            if e.reason == "rate_limited":
                break
    return out


# ---------------------------------------------------------------- the poller

class Poller:
    """The team poll of one board: one thread, only while some repository is shared. `step()` does at most one repository's cycle and returns how long to wait (None: nothing is
    shared, nothing to do); tests drive it with their own clocks. Nothing here runs at import and nothing is started by a board with no shared repository."""

    def __init__(self, db, projects_fn, *, mono=time.monotonic, wall=time.time, reader=None, ident=None, pause=None):
        self.db = db
        self.projects_fn = projects_fn
        self.mono, self.wall = mono, wall
        self.reader = reader or github.Reader()
        self.ident = ident
        self.budget = Budget()
        self.lock = threading.RLock()
        self.reads: dict[str, dict] = {}
        self.sched: dict[str, dict] = {}
        self.me: tuple[str | None, int | None] = (None, None)
        self.paused_until = 0.0
        self.gql_remaining: int | None = None
        self.force = False
        self.refreshed_at: float | None = None
        self.next_call = 0.0
        self.calls = 0
        self.stop_ev = threading.Event()
        self.wakeup = threading.Event()
        self.thread: threading.Thread | None = None
        self.pause_fn = pause or (lambda s: self.stop_ev.wait(s))
        self.loaded = False

    # ---- what the page reads
    def snapshot(self) -> dict[str, dict]:
        with self.lock:
            return {k: {**v, "issues": list(v["issues"]), "pulls": list(v["pulls"]), "tips": list(v["tips"]), "claims": list(v["claims"]), "linked": dict(v["linked"]),
                        "truncated": dict(v["truncated"])} for k, v in self.reads.items()}

    def entries(self) -> list[dict]:
        shared = self.db.team_shared()
        return own_repos(self.projects_fn(), shared)

    # ---- control
    def ensure_running(self) -> bool:
        """Start the thread if some repository is shared and it is not running. A board with nothing shared starts nothing."""
        if not self.db.team_shared():
            return False
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.stop_ev.clear()
                self.thread = threading.Thread(target=self._run, name="team-poll", daemon=True)
                self.thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_ev.set()
        self.wakeup.set()
        t = self.thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)

    def wake(self, force: bool = False) -> None:
        """Read again as soon as GitHub allows: every repository is due now. A back-off, a low remainder and a refusal still hold; `force` skips only the hourly budget, once."""
        with self.lock:
            for sc in self.sched.values():
                sc["rest"] = sc["gql"] = 0.0
            self.force = self.force or force
            self.next_call = 0.0
        self.wakeup.set()

    def refresh_allowed(self) -> int:
        """0 and the moment is noted when a refresh the person pressed may go ahead; else the seconds until one may (at most one a minute: the button is not a way round the budget)."""
        with self.lock:
            now = self.mono()
            if self.refreshed_at is not None and now - self.refreshed_at < REFRESH_MIN_S:
                return max(1, int(REFRESH_MIN_S - (now - self.refreshed_at)) + 1)
            self.refreshed_at = now
            return 0

    def forget(self, keys=()) -> None:
        """A repository was switched off: its read, its schedule and its stored record go. Nothing of it stays in memory or in the database."""
        with self.lock:
            for k in keys:
                self.reads.pop(k, None)
                self.sched.pop(k, None)

    def drop_meta(self, members) -> None:
        for m in members:
            team.set_hint(m, None)
            try:
                self.db.kv_del(KV_META + m)
            except Exception as e:
                log.debug("team meta row could not be removed: %s", e.__class__.__name__)

    def load_hints(self) -> None:
        """Take the fork and rename hints of the shared repositories from the database (a restart keeps the keys steady)."""
        shared = set(self.db.team_shared())
        for k, v, _ in self.db.kv_list_prefix(KV_META):
            local = k[len(KV_META):]
            canon = v.get("canonical") if isinstance(v, dict) else None
            o, _, n = canon.partition("/") if isinstance(canon, str) else ("", "", "")
            if local in shared and github.valid_slug(o, n):
                team.set_hint(local, team.key_of(o, n))
                _spelled[team.key_of(o, n)] = canon
        self.loaded = True

    # ---- the thread
    def _run(self) -> None:
        while not self.stop_ev.is_set():
            try:
                w = self.step()
            except Exception as e:                      # never let the thread die on a surprise
                log.warning("team poll step failed: %s", e.__class__.__name__)
                w = 30.0
            if w is None:
                return
            self.wakeup.wait(min(max(w, 0.2), 60.0))
            self.wakeup.clear()

    # ---- one step
    def step(self) -> float | None:
        now = self.mono()
        if not self.db.team_shared():
            return None                                  # off means off: not even the project scan is asked for
        if not self.loaded:
            self.load_hints()
        if self.paused_until > now:
            return self.paused_until - now
        yielded = github.reader.wait_left()                # lowest priority: while GitHub has asked the epic reader to wait, the team poll stays away too
        if yielded > 0:
            return float(min(yielded, 30))
        if self.next_call > now:
            return self.next_call - now
        entries = [e for e in self.entries() if e["shared"]]
        with self.lock:
            live = {e["key"] for e in entries}
            for k in [k for k in self.reads if k not in live]:
                self.reads.pop(k, None)
                self.sched.pop(k, None)
            if not entries:
                return 30.0
            due = []
            for e in entries:
                sc = self.sched.setdefault(e["key"], {"rest": 0.0, "gql": 0.0, "gql_last": -1e9})
                due.append((sc["rest"], e["key"], e))
            due.sort(key=lambda x: (x[0], x[1]))
            t, _, e = due[0]
        if t > now:
            return t - now
        try:
            self._cycle(e, now)
        finally:
            with self.lock:                                  # "once": a forced read ends when every repository has had its turn, however the turns went (a refusal included)
                if self.force and not any(sc["rest"] <= self.mono() for sc in self.sched.values()):
                    self.force = False
        self.next_call = self.mono() + MIN_GAP
        with self.lock:
            nxt = min((sc["rest"] for sc in self.sched.values()), default=self.mono() + 30.0)
        return max(MIN_GAP, nxt - self.mono())

    def _remaining(self) -> int | None:
        vals = [x for x in (self.reader.remaining, self.gql_remaining) if isinstance(x, int)]
        return min(vals) if vals else None

    def _factor(self) -> int:
        rem = self._remaining()
        if rem is None:
            return 1
        for below, k in SLOW_BELOW:
            if rem < below:
                return k
        return 1

    def _gate(self, cost: float) -> None:
        now = self.mono()
        left = self.reader.wait_left()
        if left > 0:
            raise github.GhReadError("rate_limited", left)
        rem = self._remaining()
        if rem is not None and rem < STOP_BELOW:
            reset = self.reader.reset
            wait = reset - self.wall() if isinstance(reset, int) else 300
            raise _Low(max(60.0, min(float(WAIT_MAX), float(wait))))
        if not self.force and not self.budget.room(now, cost):
            raise _Defer(self.budget.wait(now, cost) + 1.0)

    def _hold(self, until: float) -> None:
        """Share a back-off with the epic reader: both spend the one login's limit, so GitHub's "wait" binds both (the epic reader's already binds this poll, see step)."""
        self.reader.defer_to(until)
        if self.reader is not github.reader:
            github.reader.defer_to(until)

    def _get(self, path: str, ttl: float = 0.0):
        before = self.reader.calls
        try:
            rep = self.reader.get(path, ttl)
        finally:
            if self.reader.until > self.mono():
                self._hold(self.reader.until)
            spent = self.reader.calls - before
            if spent:
                self.budget.add(self.mono(), spent)
                self.calls += spent
                self.pause_fn(MIN_GAP)
        if rep.stale:
            raise github.GhReadError("rate_limited", self.reader.wait_left() or github.BACKOFF_DEFAULT)
        return rep

    def _list(self, make, o: str, n: str):
        """All pages (REST_PAGES at most) of a list: (items, changed, truncated). `changed` is True when GitHub sent a new body (a 304 is not a change)."""
        items, changed, truncated = [], False, False
        for page in range(1, REST_PAGES + 1):
            rep = self._get(make(o, n, page))
            changed = changed or rep.source == "network"
            data = rep.data if isinstance(rep.data, list) else []
            items += data
            if len(data) < github.PER_PAGE or 'rel="next"' not in (rep.headers.get("link") or ""):
                break
        else:
            truncated = True
        return items, changed, truncated

    def _graph(self, o: str, n: str):
        self._gate(GQL_COST_GUESS)
        self.budget.add(self.mono(), GQL_COST_GUESS)                  # charged before the call: a query that fails still cost the login points
        self.calls += 1
        try:
            status, headers, data = github.graphql(o, n)
        finally:
            self.pause_fn(MIN_GAP)
        now = self.mono()
        rem = headers.get("x-ratelimit-remaining") if isinstance(headers, dict) else None
        self.gql_remaining = int(rem) if isinstance(rem, str) and rem.isascii() and rem.isdigit() else self.gql_remaining
        wait = github.backoff_wait(status, headers, github.server_time(headers), data.get("message") if isinstance(data, dict) else None)
        if status == 403 or status == 429:
            if wait:
                self._hold(now + wait)
                raise github.GhReadError("rate_limited", wait)
            raise github.GhReadError("sso" if github.sso_signal(headers, data) else "no_access")
        if status == 401:
            raise github.GhReadError("no_login")
        if status in (404, 410):
            raise github.GhReadError("not_found")
        if status >= 500:
            raise github.GhReadError("unavailable")
        reason = github.graphql_reason(data)
        if reason:
            if reason == "rate_limited":
                self._hold(now + github.BACKOFF_DEFAULT)
            raise github.GhReadError(reason, github.BACKOFF_DEFAULT if reason == "rate_limited" else None)
        g = parse_graph(data)
        if g is None:
            raise github.GhReadError("bad_answer")
        if g["cost"] != GQL_COST_GUESS:
            self.budget.add(now, g["cost"] - GQL_COST_GUESS)              # GitHub's own figure replaces the guess
        return g

    def _meta(self, e: dict, o: str, n: str, rd: dict) -> str | None:
        """The repository's own record. Returns the canonical `owner/name` when it is not the one being read (a fork's parent, a renamed repository): the caller retargets."""
        rep = self._get(github.repo_path(o, n), META_S)
        d = rep.data if isinstance(rep.data, dict) else {}
        full = team.text(d.get("full_name"), 140)
        po = d.get("parent") if isinstance(d.get("parent"), dict) else None
        parent = team.text(po.get("full_name"), 140) if po and d.get("fork") is True else ""
        canon = parent or full
        co, _, cn = canon.partition("/")
        rd["private"] = d.get("private") if isinstance(d.get("private"), bool) else None
        rd["fork"] = d.get("fork") if isinstance(d.get("fork"), bool) else None
        db_ = d.get("default_branch")
        rd["default_branch"] = team.branch_of(db_) if isinstance(db_, str) else None
        if canon and github.valid_slug(co, cn):
            if canon.lower() != f"{o}/{n}".lower():
                return canon
            _spelled[e["key"]] = canon
            e["slug"] = canon
        return None

    def _retarget(self, e: dict, canon: str) -> None:
        if not set(self.db.team_shared()) & set(e["members"]):
            return                                       # the owner switched it off while GitHub was being asked: no hint, no row, no schedule for it
        ck = team.key_of(*canon.split("/", 1))
        for m in e["members"]:
            team.set_hint(m, ck)
            try:
                self.db.kv_set(KV_META + m, {"canonical": canon})
            except Exception as ex:
                log.debug("team meta row could not be written: %s", ex.__class__.__name__)
        _spelled[ck] = canon
        with self.lock:
            self.reads.pop(e["key"], None)
            self.sched.pop(e["key"], None)
            self.sched[ck] = {"rest": 0.0, "gql": 0.0, "gql_last": -1e9}

    def _cycle(self, e: dict, now: float) -> None:
        key = e["key"]
        o, _, n = e["slug"].partition("/")
        with self.lock:
            rd = self.reads.get(key)
            if rd is None:
                rd = self.reads[key] = new_read(e["slug"])
            sc = self.sched.setdefault(key, {"rest": 0.0, "gql": 0.0, "gql_last": -1e9})
        try:
            try:
                self.me = (self.ident or identity)()
            except Exception as ex:
                log.debug("login probe failed: %s", ex.__class__.__name__)
            want_graph = now >= sc["gql"]
            self._gate(2 + (GQL_COST_GUESS if want_graph else 0))
            canon = self._meta(e, o, n, rd)
            if canon:
                self._retarget(e, canon)
                return
            issues_raw, ch_i, tr_i = self._list(github.assigned_issues_path, o, n)
            first = not rd["read_at"]
            if ch_i or first:                                                                     # a 304 changes nothing: nothing is parsed again
                with self.lock:                                                                   # kept at once: if a later call of this cycle fails, the next cycle's 304 must not bring the old list back
                    rd.update(issues=parse_issues(issues_raw), claims_at=0.0)                     # a new list: the claim comments are read again (until that has worked)
                    rd["truncated"]["issues"] = tr_i
            pulls_raw, ch_p, tr_p = self._list(github.open_pulls_path, o, n)
            if ch_p or first:
                with self.lock:
                    rd["pulls"] = parse_pulls(pulls_raw, f"{o}/{n}", rd["closes"])
                    rd["truncated"]["pulls"] = tr_p
            changed = ch_i or ch_p
            claims = rd["claims"]
            if changed or now - rd["claims_at"] >= CLAIMS_S or not rd["claims_at"]:
                claims = []
                for it in [i for i in rd["issues"] if IN_PROGRESS in i["labels"]][:CLAIM_ISSUES]:
                    rep = self._get(github.comments_path(o, n, it["number"], 1, 100))
                    claims += parse_claims(rep.data, it["number"], github.server_time(rep.headers))
                rd["claims_at"] = now
            with self.lock:                                                                       # the REST part is shown whatever the GraphQL read does next
                rd.update(status="ok", reason=None, message=None, wait_s=None, read_at=self.wall(), claims=claims, slug=e["slug"])
                sc["rest"] = now + REST_S * self._factor()
            graph = gerr = None
            if want_graph or (changed and now - sc["gql_last"] >= GQL_MIN_S):
                try:
                    graph = self._graph(o, n)
                except github.GhReadError as err:
                    gerr = err
            elif changed:
                sc["gql"] = min(sc["gql"], sc["gql_last"] + GQL_MIN_S)             # changed too soon after the last GraphQL read: it comes forward to the earliest allowed moment
            if gerr is not None:
                with self.lock:
                    rd["graph_error"] = gerr.reason                                # the page says the branch links were not read; the rest stays current
                if gerr.reason == "rate_limited" or gerr.reason in HARD:
                    raise gerr                                                     # an access problem or a wait is the repository's state, as for any read
                sc["gql"] = now + SOFT_RETRY_S                                     # a hiccup of the GraphQL read alone: ask again soon, the repository stays ok
            if graph is not None:
                with self.lock:
                    rd.update(gql_at=self.wall(), linked=graph["linked"], tips=graph["tips"], closes=graph["closes"], graph_error=None)
                    rd["truncated"]["tips"] = graph["tips_truncated"]
                    rd["pulls"] = parse_pulls(pulls_raw, f"{o}/{n}", graph["closes"]) if pulls_raw else rd["pulls"]
                    sc["gql_last"] = now
                    sc["gql"] = now + GQL_S * self._factor()
        except _Defer as d:
            sc["rest"] = now + d.wait
        except _Low as low:
            self._low(now, low.wait)
        except github.GhReadError as err:
            self._fail(key, err, now)

    def _low(self, now: float, wait: float) -> None:
        with self.lock:
            self.paused_until = now + wait
            for rd in self.reads.values():
                rd.update(status="low_budget", reason="low_budget", wait_s=int(wait))
            self.reader.remaining = None                 # asked again when the pause is over
            self.gql_remaining = None
            self.force = False                           # a pause for a low remainder ends a forced read too: the cap stays off only for one round

    def _fail(self, key: str, err: github.GhReadError, now: float) -> None:
        state = REASON_STATE.get(err.reason, "error")
        with self.lock:
            rd = self.reads.get(key)
            if rd is not None:
                rd.update(status=state, reason=err.reason, wait_s=err.wait_s)               # the last good data stays, marked stale by the model
            retry = float(err.wait_s or SOFT_RETRY_S) if state == "rate_limited" else ERROR_RETRY_S if err.reason in HARD else SOFT_RETRY_S
            if state == "rate_limited":
                for sc in self.sched.values():
                    sc["rest"] = max(sc["rest"], now + retry)
                    sc["gql"] = max(sc["gql"], now + retry)
            else:
                sc = self.sched.setdefault(key, {"rest": 0.0, "gql": 0.0, "gql_last": -1e9})
                sc["rest"] = now + retry
                sc["gql"] = max(sc["gql"], now + retry)


poller: Poller | None = None
_plock = threading.Lock()


def get_poller(db, projects_fn) -> Poller:
    """The poller of this board's database (a new database, as a test makes, gets a new poller; the old one is stopped). Holds no thread until a repository is shared."""
    global poller
    with _plock:
        if poller is None or poller.db is not db:
            if poller is not None:
                poller.stop(1.0)
            poller = Poller(db, projects_fn)
        else:
            poller.projects_fn = projects_fn
        return poller


def reset() -> None:
    """Forget the poller, the hints and the spellings (tests)."""
    global poller
    with _plock:
        if poller is not None:
            poller.stop(2.0)
        poller = None
    team.clear_hints()
    _spelled.clear()
