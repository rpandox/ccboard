"""Team presence, the publishing side (issue #198, Team P2): this board tells teammates what its owner works on in a shared repository, in one comment per person on one status
issue per repository. Opt-in per repository, OFF by default, separate from the Team P1 read switch.

OFF MEANS OFF. With no repository switched on and no record of an earlier comment, nothing here runs: no thread, no request to GitHub, no row. A presence write is only ever built
while this module's publisher is sending it AND the repository's publish switch allows it (github_write's `presence_gate`, installed below): with the switch off, the global pause
on or the repository private, the gate refuses before a command line exists. The one exception is taking this board's own line back out (purpose presence_remove), allowed only
for a repository this board holds a record of having published to.

What is published is ONE line per board (app/team.py PRESENCE_KEYS, a whitelist pinned by a test): the alias and the 8 hex hash of the node (#148), the repository key, a sequence
number, the board's timestamp, the lease, and for the one session this board shows (needs you, then running, then idle, then ended; then the newest): its state word, its agent,
when it started (to the minute), its last activity (rounded down to 15 minutes) and, for a task, the issue number, the task phase and the branch. The branch only when GitHub has
it (a pull request, the team read's branch list, or GET /repos/{o}/{r}/branches/{name}) and only in a repository GitHub says is private: a public repository publishes no branch
(redaction), a local-only branch never leaves the box. Never a title, prompt, reply, result, transcript, terminal text, env value, token, account label, e-mail address, usage,
cost, host name, tailnet name, path or folder name.

Only the board's own comment is ever edited or deleted: before every edit or delete the status issue's comments are read again and the comment must be there, written by this
board's own gh login (numeric id and login, a User), start with the marker and (to delete it) hold this board's own line. A comment id from the database or from GitHub alone is
never enough. Several boards of one person keep their own lines in one comment: a board replaces only its own line (read, change, re-read before the write, under a lock), never
removes a sibling's line, and deletes the comment only when its line was the last one.

Cadence: a write when the line changed (a state change), else a heartbeat every 10 to 15 minutes (jittered per repository) against the 30 minute lease; nothing at all when
nothing changed and the heartbeat is not due. A state change is written at most once in STATE_GAP_S per repository and not once RESERVE of the budget is all that is left (the
heartbeat carries it). A status issue thread longer than THREAD_PAGES pages is never written to, or taken down from (state `too_long`). Writes go through github_write's one queue (1.1 s apart, 30 a minute, back-off on 403 and 429) and a budget per gh login across all
repositories and all of that person's boards (LOGIN_BUDGET writes an hour, shared by the boards seen in the person's own comments). A write whose outcome is unknown is never
sent again: the next turn reads the thread first.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone

from . import github, github_write, nodes, nodes_epics, team, team_github

log = logging.getLogger("ccboard.team.presence")

LOGIN_BUDGET = 240                  # presence writes an hour for one gh login, all its boards and repositories together (GitHub's content limit is 500 an hour)
HEARTBEAT_SHARE = 0.75              # of a board's share, what heartbeats may use; the rest is for state changes
HEARTBEAT_MIN_S = 600.0             # 10 minutes ...
HEARTBEAT_MAX_S = 900.0             # ... to 15, jittered per repository
HEARTBEAT_STRETCH_S = 25 * 60.0     # the budget may stretch a heartbeat to this, never further (the lease is 30)
LEASE_MIN = team.PRESENCE_LEASE_MIN
STEP_GAP = 2.0
CHECK_S = 30.0                      # the local state of a published repository is compared with what was published this often (no GitHub call unless it changed)
RETRY_S = 120.0                     # a write that failed for a passing reason (a 5xx, a timeout)
BLOCKED_RETRY_S = 900.0             # a blocked repository (archived, closed issue, no rights, a refusal by GitHub) is looked at again this often
STATE_GAP_S = 90.0                  # the least time between two writes of one repository for a state change (a flapping state is written once, as it is by then)
RESERVE = 0.25                      # of the hourly budget, kept for heartbeats: a state change is not written once this little is left
REPO_TTL = 3600.0                   # the repository record (private, archived, Issues on)
ISSUE_TTL = 600.0
BRANCH_TTL = 600.0
THREAD_PAGES = 3                    # pages of 100 comments of the status issue read before a write
BLOCKED_REASONS = ("no_access", "invalid", "limited", "not_found")      # a refusal by GitHub (403, 422, interaction limits, 404): looked at again every BLOCKED_RETRY_S, not every RETRY_S
LAST_ROUND_S = 15 * 60
AUDIT_SHOWN = 20

STATES = ("off", "paused", "private", "needs_confirm", "waiting", "publishing", "removing", "issues_disabled", "archived", "locked", "closed", "deleted", "no_create",
          "no_label", "forbidden", "limited", "rate_limited", "too_long", "budget", "error")
MESSAGES = {
    "off": "Not published. Nothing is written to GitHub for this repository.",
    "paused": "Team presence is paused on this board. Nothing is written.",
    "private": "Private: this board does not publish here and has taken its line down.",
    "needs_confirm": "This repository is public. Publishing waits for the second confirm.",
    "waiting": "Waiting to learn which GitHub login gh uses on this board.",
    "publishing": "",
    "removing": "Taking this board's line off GitHub.",
    "issues_disabled": "Issues are turned off in this repository, so there is nowhere to publish.",
    "archived": "The repository is archived, so nothing can be written to it.",
    "locked": "The presence issue is locked, so the comment cannot be changed.",
    "closed": "The presence issue is closed, so this board stopped publishing. Reopen it on GitHub to go on.",
    "deleted": "The presence issue is gone, so this board stopped publishing. Switch publishing off and on again to make a new one.",
    "no_create": "This GitHub login may not open issues here, so the presence issue cannot be made.",
    "no_label": "This GitHub login may not create or add labels here (that needs triage rights), so the presence issue cannot be marked.",
    "forbidden": "GitHub refused the write for this login.",
    "limited": "The repository limits who may comment right now (interaction limits).",
    "rate_limited": "GitHub asked this board to wait.",
    "too_long": "The presence issue has more than 300 comments, so this board cannot be sure which one is its own. It writes nothing there, and cannot take its line down either.",
    "budget": "This login's hourly presence budget is spent; the next write waits.",
    "error": "GitHub could not be written just now.",
}
OPT_IN_TEXT = ("Publishing writes one comment for you on a ccboard presence issue in this repository: your GitHub login, the issue and branch you work on, your session's state "
               "and agent, when it started and when you were last active (to 15 minutes). Anyone who can read the repository can read it. GitHub keeps the edit history of a "
               "comment, and everyone who can read the repository can open it while the comment exists. Some organizations do not allow tools to write to their issues.")
PUBLIC_LISTS = ("your GitHub login", "the issue number you work on", "the state and agent of your session", "when it started and when you were last active (to 15 minutes)")
SESSION_RUNNING = ("working", "running", "busy", "starting", "compacting", "thinking")
SESSION_ENDED = ("ended", "exited", "dead")


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minute(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _epoch(v) -> float | None:
    """A time from a session or task row (epoch seconds, or ISO text with or without a zone) as epoch seconds; None when it is not one."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v) if 0 < v < 4e9 else None
    if not isinstance(v, str) or len(v) > 40:
        return None
    if v.isascii() and v.isdigit():
        return _epoch(int(v)) if len(v) <= 10 else None                    # tmux prints a session's start as epoch seconds
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    t = d.timestamp()
    return t if 0 < t < 4e9 else None


def jitter(key: str) -> float:
    """The heartbeat of one repository: 10 to 15 minutes, fixed per repository (so two repositories do not beat together)."""
    h = int(hashlib.sha256(("presence:" + key).encode("utf-8")).hexdigest()[:8], 16)
    return HEARTBEAT_MIN_S + (h % 1000) / 1000.0 * (HEARTBEAT_MAX_S - HEARTBEAT_MIN_S)


# ---------------------------------------------------------------- what this board shows for one repository

def _session_word(s: dict) -> str:
    st = s.get("state") if isinstance(s.get("state"), str) else ""
    if s.get("needs_you") is True:
        return "needs_you"
    if st in SESSION_RUNNING:
        return "running"
    if st in SESSION_ENDED:
        return "ended"
    return "idle"


def _agent(v) -> str | None:
    if not isinstance(v, str) or not v:
        return None
    return v if v in team.PRESENCE_AGENTS else "other"


def pick(rows: dict, key: str) -> dict:
    """The one piece of work this board shows for repository `key`: {state, agent, started, last, issue, phase, branch, pr} from the local rows {sessions[], tasks[]} (node_state
    shaped: sessions {tmux, repo_slug, agent, state, needs_you, since, created}, tasks {tmux, repo_slug, issue_number, phase, branch, pr_number, updated_at}). Order: needs you,
    running, idle, ended, then the newest. A task without a live session counts only while its phase is running. No session and no task: an idle heartbeat with no agent."""
    sessions = [s for s in (rows.get("sessions") or [])[:400] if isinstance(s, dict) and s.get("repo_slug") == key]
    tasks = [t for t in (rows.get("tasks") or [])[:400] if isinstance(t, dict) and t.get("repo_slug") == key]
    by_tmux = {t.get("tmux"): t for t in tasks if t.get("tmux")}
    cands = []
    rank = {"needs_you": 0, "running": 1, "idle": 2, "ended": 3}
    for s in sessions:
        word = _session_word(s)
        t = by_tmux.get(s.get("tmux"))
        cands.append((rank[word], -(_epoch(s.get("since")) or 0), {"state": word, "agent": _agent(s.get("agent")), "started": _epoch(s.get("created")) or _epoch(s.get("since")),
                                                                 "last": _epoch(s.get("since")), "task": t}))
    for t in tasks:
        if t.get("tmux") in {s.get("tmux") for s in sessions} or t.get("phase") != "running":
            continue
        cands.append((rank["idle"], -(_epoch(t.get("updated_at")) or 0), {"state": "idle", "agent": _agent(t.get("agent")), "started": None, "last": _epoch(t.get("updated_at")), "task": t}))
    if not cands:
        return {"state": "idle", "agent": None, "started": None, "last": None, "issue": None, "phase": None, "branch": None, "pr": None}
    cands.sort(key=lambda c: (c[0], c[1]))
    c = cands[0][2]
    t = c.pop("task") or {}
    n = t.get("issue_number")
    phase = t.get("phase") if t.get("phase") in team.PRESENCE_PHASES else None
    br = t.get("branch") if isinstance(t.get("branch"), str) else None
    pr = t.get("pr_number") if isinstance(t.get("pr_number"), int) and not isinstance(t.get("pr_number"), bool) and t.get("pr_number") > 0 else None
    return {**c, "issue": n if isinstance(n, int) and not isinstance(n, bool) and 0 < n < 10 ** 9 else None, "phase": phase, "branch": br, "pr": pr}


def line_fields(work: dict, *, node: str, nid: str, key: str, branch_ok: bool) -> dict:
    """The fields of this board's line, from `pick()` (no seq and at yet). Every key is on the whitelist; the branch is kept only when `branch_ok` (GitHub has it and the repository
    is private) and it looks like nothing secret."""
    repo = key[len(team.HOST) + 1:] if key.startswith(team.HOST + "/") else key
    out = {"node": node, "nid": nid, "repo": repo, "lease": f"{LEASE_MIN}m", "state": work["state"], "agent": work.get("agent"),
           "since": _minute(work["started"]) if work.get("started") else None,
           "last": _minute(work["last"] - (work["last"] % LAST_ROUND_S)) if work.get("last") else None,
           "issue": work.get("issue"), "phase": work.get("phase"), "branch": None}
    br = work.get("branch")
    if branch_ok and github.valid_branch(br) and not github_write.SECRETISH.search(br) and len(br) <= 100:
        out["branch"] = br
    return out


def digest(fields: dict) -> str:
    """What changes the line besides the sequence number and the board's timestamp: a new digest is a state change."""
    f = {k: v for k, v in fields.items() if k not in ("seq", "at")}
    return hashlib.sha256(json.dumps(f, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def own_comments(comments, me_login: str, me_id: int) -> list[dict]:
    """The comments that are this board's person's own presence comments: a User whose numeric id AND login are the board's gh login, a body that starts with the marker and
    parses. Each is {id, updated_at, created_at, lines}. Anything else (another person, an app, no id, a body that is not a presence body) is not ours and is never touched."""
    out = []
    for c in comments[:THREAD_PAGES * github.PER_PAGE] if isinstance(comments, list) else []:
        if not isinstance(c, dict):
            continue
        u = c.get("user") if isinstance(c.get("user"), dict) else None
        cid = c.get("id")
        if u is None or u.get("type") != "User" or isinstance(cid, bool) or not isinstance(cid, int) or cid <= 0:
            continue
        if team.user_id(u.get("id")) != me_id or not isinstance(u.get("login"), str) or u["login"].lower() != (me_login or "").lower() or me_id is None:
            continue
        lines = team.parse_body(c.get("body"))
        if lines is None:
            continue
        out.append({"id": cid, "updated_at": github.clean_ts(c.get("updated_at")), "created_at": github.clean_ts(c.get("created_at")), "lines": lines, "body": c.get("body"),
                    "node_id": github.clean_node_id(c.get("node_id"))})
    out.sort(key=lambda c: c["id"])
    return out


class _Blocked(Exception):
    def __init__(self, state: str, wait: float = BLOCKED_RETRY_S):
        self.state, self.wait = state, wait


_active = threading.local()


def _gate(owner, repo, purpose) -> bool:
    """github_write's presence gate: a presence write may be built only while a publisher is sending it and that publisher allows it for this repository."""
    pub = getattr(_active, "pub", None)
    return pub is not None and pub.allows(owner, repo, purpose)


github_write.presence_gate = _gate


# ---------------------------------------------------------------- the publisher

class Publisher:
    """The presence writer of one board: one thread, only while some repository publishes or a record of an earlier comment is left to clean up. `step()` does one repository's
    turn and returns how long to wait (None: nothing to do; the thread ends). Tests drive it with their own clocks and several boards in one process (each with its own database,
    node id and queue)."""

    def __init__(self, db, projects_fn, rows_fn, *, mono=time.monotonic, wall=time.time, ident=None, reader=None, wq=None, node_id=None, alias=None, branches_fn=None,
                 pause=None):
        self.db, self.projects_fn, self.rows_fn = db, projects_fn, rows_fn
        self.mono, self.wall = mono, wall
        self.ident = ident
        self.reader = reader or github.reader
        self.wq = wq
        self.branches_fn = branches_fn                         # key -> names GitHub already showed the team read (branch tips, pull request heads), or None
        nid_src = node_id if node_id is not None else nodes.node_id()
        self.nid = nodes_epics.node_nid(nid_src)
        a = alias if alias is not None else nodes_epics.alias_report(nid_src)["alias"]
        self.alias = a if github_write.ALIAS_RE.fullmatch(a or "") and not nodes_epics.looks_like_host(a) else nodes_epics.derived_alias(nid_src)
        self.budget = team_github.Budget(per_hour=LOGIN_BUDGET)
        self.lock = threading.RLock()                          # read, change, re-read and write of one comment happen under it
        self.next: dict[str, float] = {}
        self.status: dict[str, dict] = {}
        self.kinds: dict[str, tuple] = {}                     # repository key -> (the kind of its last turn, the state after a removal)
        self.boards: dict[str, int] = {}                       # repository key -> boards seen in this person's own comments there
        self.branch_seen: dict[tuple, tuple[float, bool]] = {}
        self.me: tuple[str | None, int | None] = (None, None)
        self.writes = 0
        self.stop_ev = threading.Event()
        self.wakeup = threading.Event()
        self.thread: threading.Thread | None = None
        self.pause_fn = pause
        self.memo: tuple | None = None                         # (when, projects, rows): the local scan is asked at most once per CHECK_S, however many repositories publish

    # ---- the switches (kv rows: a publish row exists only while on; the pause row only while paused; a record while this board may have a line on GitHub)
    def pubs(self) -> dict[str, dict]:
        return self.db.team_publish()

    def recs(self) -> dict[str, dict]:
        return self.db.team_presence_recs()

    def paused(self) -> bool:
        return self.db.team_presence_paused()

    def _row_for(self, key: str, pubs: dict | None = None) -> dict | None:
        pubs = self.pubs() if pubs is None else pubs
        rows = [v for k, v in pubs.items() if k == key or team.hinted(k) == key]
        if not rows:
            return None
        return {"on": True, "public_ok": any(r.get("public_ok") is True for r in rows), "private": any(r.get("private") is True for r in rows)}

    def allows(self, owner, repo, purpose) -> bool:
        """May a presence write with this purpose go to owner/repo now? publish: the switch is on, the board is not paused, the repository is not private, a public repository
        has had its second confirm. remove: this board holds a record of having published there. Anything else: no."""
        if not github.valid_slug(owner, repo):
            return False
        key = team.key_of(owner, repo)
        if purpose == "presence_remove":
            return key in self.recs()
        if purpose != "presence" or self.paused():
            return False
        row = self._row_for(key)
        if row is None or row["private"]:
            return False
        st = self.status.get(key) or {}
        return st.get("repo_private") is True or row["public_ok"]

    def any_work(self) -> bool:
        return bool(self.pubs()) or bool(self.recs())

    # ---- the thread
    def ensure_running(self) -> bool:
        if not self.any_work():
            return False
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.stop_ev.clear()
                self.thread = threading.Thread(target=self._run, name="team-presence", daemon=True)
                self.thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_ev.set()
        self.wakeup.set()
        t = self.thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)

    def local(self) -> tuple[list, dict]:
        """(projects, rows) of this board, remembered for CHECK_S so a burst of turns does not scan the projects once per repository."""
        now = self.mono()
        if self.memo is None or now - self.memo[0] >= CHECK_S:
            self.memo = (now, self.projects_fn(), self.rows_fn() or {})
        return self.memo[1], self.memo[2]

    def wake(self, key: str | None = None) -> None:
        """Do this repository's turn (or every one's) now: a switch changed, Pause or Private was pressed. The local state is read afresh."""
        self.memo = None
        with self.lock:
            for k in ([key] if key else list(self.next)):
                self.next[k] = 0.0
        self.wakeup.set()

    def _run(self) -> None:
        while not self.stop_ev.is_set():
            try:
                w = self.step()
            except Exception as e:                              # never let the thread die on a surprise
                log.warning("presence step failed: %s", e.__class__.__name__)
                w = 60.0
            if w is None:
                return
            self.wakeup.wait(min(max(w, 0.5), 120.0))
            self.wakeup.clear()

    # ---- helpers
    def _me(self) -> tuple[str | None, int | None]:
        try:
            got = (self.ident or team_github.identity)()
        except Exception as e:
            log.debug("login probe failed: %s", e.__class__.__name__)
            got = (None, None)
        login = team.login_of(got[0]) if got and got[0] else None
        uid = team.user_id(got[1]) if got and login else None
        self.me = (login, uid)
        return self.me

    def _set(self, key: str, state: str, **extra) -> None:
        prev = (self.status.get(key) or {}).get("state")
        cur = dict(self.status.get(key) or {})
        cur.update(state=state, message=MESSAGES.get(state, ""), **extra)
        self.status[key] = cur
        if prev != state and state in ("issues_disabled", "archived", "locked", "closed", "deleted", "no_create", "no_label", "forbidden", "limited", "needs_confirm", "too_long"):
            self._audit("presence_blocked", False, key, state)

    def _audit(self, action: str, ok: bool, key: str, detail: str | None = None) -> None:
        """One audit row: the action, the repository and a reason word. Never a login, a body or a comment's text."""
        split = team.split_key(key)
        nodes.audit("out", "", action, ok, detail, target="/".join(split) if split else None, status="ok" if ok else "failed", db=self.db)

    def per_board(self) -> float:
        return LOGIN_BUDGET / max(1, max(self.boards.values(), default=1))

    def heartbeat_s(self, key: str, publishing: int) -> float:
        """10 to 15 minutes, stretched (up to 25) when this board's share of the login's budget cannot hold one heartbeat per repository per interval."""
        need = publishing * 3600.0 / max(1.0, self.per_board() * HEARTBEAT_SHARE)
        return min(HEARTBEAT_STRETCH_S, max(jitter(key), need))

    def _room(self) -> float:
        now = self.mono()
        self.budget.per_hour = self.per_board()
        return 0.0 if self.budget.room(now, 1) else self.budget.wait(now, 1) + 1.0

    def _send(self, req) -> tuple:
        _active.pub = self
        sent = True
        try:
            return github_write.send(req, self.wq)
        except github_write.GhWriteError as e:
            sent = e.reason not in ("presence_off", "bad_request", "busy", "writes_off") and not (e.reason == "rate_limited" and e.status is None)
            raise
        finally:
            _active.pub = None
            if sent:
                self.budget.add(self.mono(), 1)
                self.writes += 1

    def _get(self, path: str, ttl: float = 0.0):
        rep = self.reader.get(path, ttl)
        if rep.stale:
            raise github.GhReadError("rate_limited", self.reader.wait_left() or github.BACKOFF_DEFAULT)
        return rep

    def _repo(self, o: str, n: str, ttl: float = REPO_TTL) -> dict:
        rep = self._get(github.repo_path(o, n), ttl)
        d = rep.data if isinstance(rep.data, dict) else {}
        return {"private": d.get("private") if isinstance(d.get("private"), bool) else None, "archived": d.get("archived") is True, "issues": d.get("has_issues") is not False}

    def _branch_ok(self, key: str, o: str, n: str, work: dict, private: bool) -> bool:
        """Is the branch of this work on GitHub? Only asked in a repository GitHub says is private (a public one publishes no branch). A pull request, a name the team read already
        saw on GitHub, or one GET of the branch (remembered BRANCH_TTL; a 404 is "not pushed"). Not known: no branch."""
        br = work.get("branch")
        if not private or not github.valid_branch(br):
            return False
        if work.get("pr"):
            return True
        try:
            seen = self.branches_fn(key) if self.branches_fn else None
        except Exception:
            seen = None
        if seen and br in seen:
            return True
        hit = self.branch_seen.get((key, br))
        if hit and self.mono() - hit[0] < BRANCH_TTL:
            return hit[1]
        try:
            self._get(github.branch_path(o, n, br), BRANCH_TTL)
            ok = True
        except github.GhReadError as e:
            if e.reason not in ("not_found",):
                return False                                       # not known: no branch, and nothing remembered
            ok = False
        self.branch_seen[(key, br)] = (self.mono(), ok)
        return ok

    def _thread(self, o: str, n: str, issue: int) -> list[dict]:
        """The status issue's comments, THREAD_PAGES pages at most. A longer thread is not read in part: this board could not see its own comment past the last page, and would make
        a new one (or leave an old one) every time, so it stops with `too_long` and writes nothing."""
        out = []
        for page in range(1, THREAD_PAGES + 1):
            rep = self._get(github.comments_path(o, n, issue, page, 100), 0.0)
            data = rep.data if isinstance(rep.data, list) else []
            out += data
            if len(data) < github.PER_PAGE or 'rel="next"' not in (rep.headers.get("link") or ""):
                break
        else:
            raise _Blocked("too_long")
        return out

    def _labelled(self, o: str, n: str, ttl: float = 0.0) -> list[tuple]:
        """The issues with the presence label as (closed, number, locked): the open ones first, the lowest number first."""
        data = self._get(github.labeled_path(o, n, team.PRESENCE_LABEL, "all", 1, 30), ttl).data
        found = []
        for it in data if isinstance(data, list) else []:
            if not isinstance(it, dict) or "pull_request" in it:
                continue
            num = team.number_of(it.get("number"))
            labels = [lb.get("name") if isinstance(lb, dict) else lb for lb in (it.get("labels") or [])[:30]] if isinstance(it.get("labels"), list) else []
            if num and team.PRESENCE_LABEL in labels:
                found.append((it.get("state") != "open", num, it.get("locked") is True))
        found.sort()
        return found

    def _status_issue(self, key: str, o: str, n: str, rec: dict) -> int:
        """The status issue's number: the lowest open issue with the label (the one the readers use; looked at again on every write, so two boards that made one each at the same
        time end up on the same one, and the other is left alone, never closed or deleted), else the recorded one (closed, locked or gone each stop publishing and say why),
        else a new one (this is reached only with the publish switch on: creating it is the first opt-in)."""
        found = self._labelled(o, n)
        low = found[0] if found and not found[0][0] else None
        if low is not None and low[1] != rec.get("issue"):
            if low[2]:
                raise _Blocked("locked")
            return low[1]                                             # _publish moves this board's line there from the recorded issue, if it had one
        if rec.get("issue"):
            try:
                d = self._get(github.issue_path(o, n, rec["issue"]), ISSUE_TTL).data
            except github.GhReadError as e:
                if e.reason == "not_found":
                    raise _Blocked("deleted") from None
                raise
            d = d if isinstance(d, dict) else {}
            if d.get("state") == "closed":
                raise _Blocked("closed")
            if d.get("locked") is True:
                raise _Blocked("locked")
            labels = [lb.get("name") if isinstance(lb, dict) else lb for lb in (d.get("labels") or [])[:30]] if isinstance(d.get("labels"), list) else []
            if team.PRESENCE_LABEL not in labels:
                raise _Blocked("no_label")                            # an issue nobody can find by its label is not where teammates look
            return int(rec["issue"])
        if found:
            raise _Blocked("closed")
        try:
            self._send(github_write.presence_label_create(o, n))
            self._audit("presence_label", True, key)
        except github_write.GhWriteError as e:
            if e.reason == "no_access":
                raise _Blocked("no_label") from None
            if e.reason not in ("invalid",):                          # 422: the label exists already
                raise
        try:
            _, _, d = self._send(github_write.presence_issue_create(o, n))
        except github_write.GhWriteError as e:
            if e.reason == "no_access":
                raise _Blocked("no_create") from None
            if e.reason == "not_found" and e.status == 410:
                raise _Blocked("issues_disabled") from None
            raise
        num = team.number_of(d.get("number")) if isinstance(d, dict) else None
        if num is None:
            raise github_write.GhWriteError("bad_answer", unknown=True)
        labels = [lb.get("name") for lb in (d.get("labels") or []) if isinstance(lb, dict)] if isinstance(d.get("labels"), list) else []
        self._save(key, {**rec, "issue": num})
        self._audit("presence_issue", True, key)
        if team.PRESENCE_LABEL not in labels:
            raise _Blocked("no_label")                                # GitHub dropped the label: nobody would find the issue
        again = self._labelled(o, n)                                  # another board may have made one in the same minute: the lowest open one is the status issue
        if again and not again[0][0] and again[0][1] != num:
            if again[0][2]:
                raise _Blocked("locked")
            return again[0][1]
        return num

    def _save(self, key: str, rec: dict | None) -> None:
        self.db.team_presence_rec_set(key, rec)

    def _editor_ok(self, o: str, n: str, issue: int, c: dict, me_id: int) -> bool:
        """Was comment `c` last edited by this person (or never edited)? GraphQL `editor`: the last comments of the issue in one call, and a comment older than that window by its own
        node id. Unknown counts as no (the sibling lines are then not kept)."""
        try:
            status, _, data = github.graphql_editors(o, n, issue)
            ed = github.parse_editors(data) if status == 200 else None
            v = (ed or {}).get(c["id"])
            if v is None and ed is not None and c.get("node_id"):
                status, _, data = github.graphql_editor(c["node_id"])
                v = github.parse_editor(data, c["id"]) if status == 200 else None
        except github.GhReadError:
            return False
        return v is not None and (not v["edited"] or v["editor"] == me_id)

    # ---- one turn
    def step(self) -> float | None:
        pubs, recs = self.pubs(), self.recs()
        if not pubs and not recs:
            return None                                            # off means off: nothing is asked, nothing runs
        now = self.mono()
        login, uid = self._me()
        paused = self.paused()
        projs, _ = self.local()
        entries = team_github.own_repos(projs, pubs.keys())
        want = {e["key"]: e for e in entries if e["shared"]}
        if not login or uid is None:
            for k in set(want) | set(recs):
                self._set(k, "waiting")
            return 60.0                                            # who this board is, is not known: nothing is written (fail closed)
        jobs = []

        def add(key, kind, after, e):
            if self.kinds.get(key) != (kind, after):
                self.kinds[key] = (kind, after)
                self.next[key] = 0.0                               # Pause, Private, off or a repository gone: the line comes down at once, not at the next heartbeat
            jobs.append((self.next.get(key, 0.0), 0 if kind == "remove" else 1, kind, key, e, after))
        for key, e in want.items():
            row = self._row_for(key, pubs) or {}
            if paused or row.get("private"):
                after = "paused" if paused else "private"
                if key in recs:
                    add(key, "remove", after, e)
                else:
                    self.kinds[key] = ("idle", after)
                    self._set(key, after)
                continue
            add(key, "publish", None, e)
        for key in recs:
            if key not in want:
                add(key, "remove", "off", None)                     # switched off, or no longer one of this board's repositories: clean up
        for k in [k for k in self.status if k not in want and k not in recs]:
            self.status.pop(k, None)
        if not jobs:
            return 60.0
        jobs.sort(key=lambda j: (j[0], j[1], j[3]))
        due, _, kind, key, e, after = jobs[0]
        if due > now:
            return due - now
        publishing = sum(1 for j in jobs if j[2] == "publish")
        try:
            if kind == "remove":
                self._remove(key, recs[key], login, uid, after=after)
            else:
                self._publish(key, e, recs.get(key) or {}, login, uid, publishing)
        except _Blocked as b:
            self._set(key, b.state)
            self.next[key] = now + b.wait
        except github_write.GhWriteError as err:
            self._write_failed(key, err, now)
        except github.GhReadError as err:
            self._set(key, "rate_limited" if err.reason == "rate_limited" else "forbidden" if err.reason in ("no_access", "sso") else "error", reason=err.reason)
            self.next[key] = now + (float(err.wait_s) if err.reason == "rate_limited" and err.wait_s else RETRY_S)
        return STEP_GAP

    def _write_failed(self, key: str, err, now: float) -> None:
        if err.reason == "presence_off":
            self.next[key] = now + RETRY_S                          # the switch changed under the turn: the next turn sees it
            return
        state = {"rate_limited": "rate_limited", "no_access": "forbidden", "limited": "limited", "not_found": "error"}.get(err.reason, "error")
        self._set(key, state, reason=err.reason, unknown=err.unknown)
        if err.reason in BLOCKED_REASONS:                           # a refusal (403, 422, interaction limits) does not pass in two minutes: look again as for any blocked repository
            self.next[key] = now + max(float(err.wait_s or 0), BLOCKED_RETRY_S)
        else:                                                       # a 5xx or a timeout, or GitHub asked for a wait (429 with its Retry-After): the short retry
            self.next[key] = now + (float(err.wait_s) if err.wait_s else RETRY_S)
        if err.unknown:
            rec = self.recs().get(key)
            if rec is not None:
                self._save(key, {**rec, "unknown": True})          # never sent again as it was: the next turn reads the thread before anything

    def _check_repo(self, key: str, o: str, n: str, rec: dict, login: str, uid: int, facts: dict) -> bool:
        """What GitHub says about the repository decides whether this turn goes on: archived, Issues off, or public without the second confirm (then this board's line is taken down
        first) each stop it. True when the repository is private."""
        cur = dict(self.status.get(key) or {})
        cur["repo_private"] = facts["private"]
        self.status[key] = cur
        if facts["archived"]:
            raise _Blocked("archived")
        if not facts["issues"]:
            raise _Blocked("issues_disabled")
        row = self._row_for(key) or {}
        private = facts["private"] is True
        if not private and not row.get("public_ok"):
            if rec.get("published") or rec.get("unknown"):
                self._remove(key, rec, login, uid, keep_rec=True)  # the repository turned public: take the line down until the person confirms
            raise _Blocked("needs_confirm")
        return private

    def _siblings(self, target: dict) -> list[dict]:
        """The other boards' lines of this person's comment that are kept when this board rewrites it: not a line whose `at` is more than a day older than the comment's last change
        (a board that died), and when there are still too many, the newest."""
        sibs = [ln for ln in target["lines"] if ln["nid"] != self.nid]
        upd = _epoch(target.get("updated_at"))
        if upd is not None:
            sibs = [ln for ln in sibs if (_epoch(ln["at"]) or 0) >= upd - team.PRESENCE_GONE_S]
        sibs.sort(key=lambda ln: ln["at"], reverse=True)
        return sibs[:team.PRESENCE_LINES_MAX - 1]

    def _publish(self, key: str, e: dict, rec: dict, login: str, uid: int, publishing: int) -> None:
        o, _, n = e["slug"].partition("/")
        facts = self._repo(o, n)
        private = self._check_repo(key, o, n, rec, login, uid, facts)
        work = pick(self.local()[1], key)
        fields = line_fields(work, node=self.alias, nid=self.nid, key=key, branch_ok=self._branch_ok(key, o, n, work, private))
        h = digest(fields)
        hb = self.heartbeat_s(key, publishing)
        now_w = self.wall()
        last_w = rec.get("wall") if isinstance(rec.get("wall"), (int, float)) else None
        if rec.get("published") and rec.get("hash") == h and not rec.get("unknown") and last_w is not None and now_w - last_w < hb and rec.get("issue"):
            self.next[key] = self.mono() + min(CHECK_S, hb - (now_w - last_w))
            self._set(key, "publishing")
            if rec.get("private") != facts["private"]:
                self._save(key, {**rec, "private": facts["private"]})          # what the Doctor reads
            return                                                    # nothing changed and the heartbeat is not due: no write (the local state is looked at again soon)
        since = now_w - last_w if last_w is not None else None
        if rec.get("published") and not rec.get("unknown") and rec.get("issue") and since is not None and since < hb:
            # a state change, not a heartbeat: written at most once in STATE_GAP_S (what it says by then is what is written), and not at all once the hourly budget is down to its
            # reserve, which is for heartbeats (the change goes out with the next one)
            if 0 <= since < STATE_GAP_S:
                self.next[key] = self.mono() + STATE_GAP_S - since
                self._set(key, "publishing")
                return
            if self.budget.spent(self.mono()) >= (1.0 - RESERVE) * self.per_board():
                self.next[key] = self.mono() + max(1.0, hb - since)
                self._set(key, "publishing")
                return
        wait = self._room()
        if wait:
            self._set(key, "budget")
            self.next[key] = self.mono() + wait
            return
        facts = self._repo(o, n, 0.0)                               # the repository may have turned public since: asked again (conditionally) before every write
        self._check_repo(key, o, n, self.recs().get(key) or rec, login, uid, facts)
        issue = self._status_issue(key, o, n, rec)
        old_issue = rec.get("issue") if rec.get("published") and rec.get("issue") and rec["issue"] != issue else None      # a lower open status issue was found: the line moves there
        rec = {**(self.recs().get(key) or rec), "issue": issue, "owner": o, "repo": n}

        def read():
            return own_comments(self._thread(o, n, issue), login, uid)

        def build(mine):
            """(target comment, comments holding this board's line, sequence number, body) from the comments of this person as read."""
            with_me = [c for c in mine if any(ln["nid"] == self.nid for ln in c["lines"])]
            target = with_me[0] if with_me else (mine[0] if mine else None)
            seq = max([int(rec.get("seq") or 0)] + [ln["seq"] for c in with_me for ln in c["lines"] if ln["nid"] == self.nid]) + 1
            line = team.presence_line({**fields, "seq": seq, "at": _iso(now_w)})
            sibs = self._siblings(target) if target is not None else []
            if sibs and (target["updated_at"] != rec.get("seen_updated") or target["body"] != rec.get("body")) and not self._editor_ok(o, n, issue, target, uid):
                sibs = []                                          # somebody else edited this comment last: their lines are not kept
            return target, with_me, seq, team.presence_body([ln["raw"] for ln in sibs] + [line])

        def sig(cs):
            return [(c["id"], c["updated_at"], c["body"]) for c in cs]
        with self.lock:
            mine = read()
            built = build(mine)
            again = read()                                          # read once more right before the write: another board of this person may have changed the comment meanwhile
            if sig(again) != sig(mine):
                mine, built = again, build(again)
            target, with_me, seq, body = built
            self.boards[key] = max(1, len({ln["nid"] for c in mine for ln in c["lines"]} | {self.nid}))
            if target is not None:
                if body == target["body"]:
                    _, _, d = 200, {}, None
                else:
                    _, _, d = self._send(github_write.presence_comment_edit(o, n, target["id"], body))
                cid = target["id"]
            else:
                before = self.recs().get(key)
                self._save(key, {**rec, "unknown": True})          # a line may exist on GitHub from here on, whatever happens to the answer: a record lets Off and Pause find it
                try:
                    _, _, d = self._send(github_write.presence_comment_create(o, n, issue, body))
                except github_write.GhWriteError as err:
                    if not err.unknown:
                        self._save(key, before)                    # refused: nothing was made
                    raise
                cid = d.get("id") if isinstance(d, dict) and isinstance(d.get("id"), int) and not isinstance(d.get("id"), bool) else None
                if cid is None:
                    raise github_write.GhWriteError("bad_answer", unknown=True)
            for c in with_me:                                       # a second comment of this person that also holds this board's line (two boards raced): take it out there
                if c["id"] != cid:
                    self._take_out(o, n, issue, c, login, uid)
        upd = github.clean_ts(d.get("updated_at")) if isinstance(d, dict) else None
        first = not rec.get("published")
        changed = rec.get("hash") != h
        self._save(key, {**rec, "cid": cid, "seq": seq, "body": body, "hash": h, "wall": now_w, "at": _iso(now_w), "published": True, "unknown": False,
                         "seen_updated": upd, "private": facts["private"]})
        self._set(key, "publishing", reason=None, unknown=False)
        self.next[key] = self.mono() + min(CHECK_S, hb)
        if old_issue:
            try:
                with self.lock:
                    for c in own_comments(self._thread(o, n, old_issue), login, uid):
                        self._take_out(o, n, old_issue, c, login, uid)
            except (_Blocked, github.GhReadError):
                pass                                                # best effort: the old line ages out
        if first or changed:
            self._audit("presence_publish", True, key, "first" if first else "state")

    def _take_out(self, o: str, n: str, issue: int, c: dict, login: str, uid: int) -> None:
        """Take this board's line out of comment `c` (already checked to be this person's own presence comment holding this board's line): the comment is read again right before the
        write, so a line another board added since stays. Edit when other boards' lines stay, delete when it was the last line."""
        if not any(ln["nid"] == self.nid for ln in c["lines"]):
            return                                                  # not this board's line: never touched
        fresh = next((x for x in own_comments(self._thread(o, n, issue), login, uid) if x["id"] == c["id"]), None)
        if fresh is None or not any(ln["nid"] == self.nid for ln in fresh["lines"]):
            return                                                  # gone already, or this board's line is
        rest = [ln["raw"] for ln in fresh["lines"] if ln["nid"] != self.nid]
        if rest:
            self._send(github_write.presence_comment_edit(o, n, c["id"], team.presence_body(rest), removal=True, nid=self.nid, before=fresh["body"]))
        else:
            self._send(github_write.presence_comment_delete(o, n, c["id"]))

    def _remove(self, key: str, rec: dict, login: str, uid: int, keep_rec: bool = False, after: str = "off") -> None:
        """Take this board's line off GitHub (Pause, Private, switched off, the repository gone from this board, or turned public): the thread is read again and only this person's
        own comments holding this board's own line are changed. Then the record goes (unless `keep_rec`). A thread too long to read in full is left alone (`too_long`), record and all."""
        o, n, issue = rec.get("owner"), rec.get("repo"), rec.get("issue")
        if not github.valid_slug(o, n) or not issue or not rec.get("published") and not rec.get("unknown"):
            if not keep_rec:
                self._save(key, None)
                self._set(key, after)
            return
        self._set(key, "removing")
        with self.lock:
            try:
                mine = own_comments(self._thread(o, n, issue), login, uid)
            except github.GhReadError as e:
                if e.reason == "not_found":
                    mine = []                                       # the issue is gone, and the comment with it
                else:
                    raise
            for c in mine:
                self._take_out(o, n, issue, c, login, uid)
        if keep_rec:
            self._save(key, {**rec, "published": False, "unknown": False, "cid": None})
        else:
            self._save(key, None)
        self._set(key, after if not keep_rec else "needs_confirm")
        self._audit("presence_delete", True, key)

    # ---- what Settings > Team shows
    def preview(self, key: str, slug: str, private: bool | None) -> str | None:
        """The literal body this board would publish now in repository `key` (no request is made: a branch is shown only when the team read already saw it on GitHub)."""
        rec = self.recs().get(key) or {}
        work = pick(self.rows_fn() or {}, key)
        seen = None
        try:
            seen = self.branches_fn(key) if self.branches_fn else None
        except Exception:
            seen = None
        ok = private is True and github.valid_branch(work.get("branch")) and (bool(work.get("pr")) or bool(seen and work.get("branch") in seen))
        try:
            fields = line_fields(work, node=self.alias, nid=self.nid, key=key, branch_ok=ok)
            return team.presence_body([team.presence_line({**fields, "seq": int(rec.get("seq") or 0) + 1, "at": _iso(self.wall())})])
        except ValueError:
            return None

    def privacy(self, slug: str) -> bool | None:
        """What GitHub last said about the repository being private (the publisher's own read, else the reader's cache); None when not known."""
        o, _, n = slug.partition("/")
        st = self.status.get(team.key_of(o, n)) if github.valid_slug(o, n) else None
        if st and isinstance(st.get("repo_private"), bool):
            return st["repo_private"]
        rec = self.recs().get(team.key_of(o, n)) if github.valid_slug(o, n) else None
        if rec and isinstance(rec.get("private"), bool):
            return rec["private"]
        return None

    def block(self, e: dict, read_on: bool, read_private=None) -> dict:
        key = e["key"]
        row = self._row_for(key) or {}
        rec = self.recs().get(key) or {}
        st = self.status.get(key) or {}
        paused = self.paused()
        private_known = st.get("repo_private") if isinstance(st.get("repo_private"), bool) else rec.get("private") if isinstance(rec.get("private"), bool) else read_private
        state = st.get("state") if st.get("state") in STATES else None
        if not row:
            state = "removing" if rec.get("published") else "off"
        elif paused:
            state = "paused" if not rec.get("published") else "removing"
        elif row.get("private"):
            state = "private" if not rec.get("published") else "removing"
        elif state is None:
            state = "needs_confirm" if private_known is not True and not row.get("public_ok") else "publishing"
        if st.get("state") == "too_long" and (rec.get("published") or rec.get("unknown")):
            state = "too_long"                                      # a line this board cannot find stays whatever the switches say: the page says so rather than "removing"
        o, _, n = e["slug"].partition("/")
        issue = rec.get("issue") if isinstance(rec.get("issue"), int) else None
        return {"repo_slug": key, "slug": e["slug"], "local": [{"project": team.text(x.get("project"), 100), "repo": team.text(x.get("repo"), 100)} for x in (e.get("local") or [])[:20]],
                "read": bool(read_on), "publish": bool(row), "private": bool(row.get("private")), "public": None if private_known is None else private_known is False,
                "public_confirmed": bool(row.get("public_ok")), "state": state, "message": MESSAGES.get(state, ""),
                "issue": issue, "issue_url": team.issue_url(o, n, issue) if issue and github.valid_slug(o, n) else None,
                "published": bool(rec.get("published")), "last_body": rec.get("body") if isinstance(rec.get("body"), str) and rec.get("published") else None,
                "last_at": rec.get("at") if rec.get("published") else None, "boards": self.boards.get(key, 1)}

    def view(self, entries: list[dict], read_keys, read_private: dict | None = None) -> dict:
        login, uid = self.me if self.me[0] else team_github.identity_cached()
        now = self.mono()
        return {"api": 1, "paused": self.paused(), "me": {"login": login, "id": uid} if login else None, "node": self.alias, "lease_min": LEASE_MIN,
                "heartbeat_s": [int(HEARTBEAT_MIN_S), int(HEARTBEAT_MAX_S)],
                "budget": {"per_hour": LOGIN_BUDGET, "boards": max(self.boards.values(), default=1), "board_share": int(self.per_board()), "spent_hour": int(self.budget.spent(now))},
                "repos": [self.block(e, e["key"] in read_keys, (read_private or {}).get(e["key"])) for e in entries],
                "opt_in_text": OPT_IN_TEXT, "public_lists": list(PUBLIC_LISTS),
                "audit": [{k: r[k] for k in ("at", "action", "target", "status", "detail")} for r in nodes.audit_list(AUDIT_SHOWN, db=self.db) if str(r.get("action", "")).startswith("presence")]}


# ---------------------------------------------------------------- the board's publisher

publisher: Publisher | None = None
_plock = threading.Lock()


def get_publisher(db, projects_fn, rows_fn, branches_fn=None) -> Publisher:
    """The publisher of this board's database (a new database, as a test makes, gets a new one; the old one is stopped). Holds no thread until a repository publishes."""
    global publisher
    with _plock:
        if publisher is None or publisher.db is not db:
            if publisher is not None:
                publisher.stop(1.0)
            publisher = Publisher(db, projects_fn, rows_fn, branches_fn=branches_fn)
        else:
            publisher.projects_fn, publisher.rows_fn, publisher.branches_fn = projects_fn, rows_fn, branches_fn
        return publisher


def reset() -> None:
    global publisher
    with _plock:
        if publisher is not None:
            publisher.stop(2.0)
        publisher = None
