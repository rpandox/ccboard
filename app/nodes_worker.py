"""Worker mode: an idle node takes the next ready issue of the listed epics by itself, with no hub involved (nodes epic, issue #157).

THE RISK. With a hub a person presses Run. With the worker, the text of an issue starts an agent (acceptEdits, in a worktree, on this machine) with nobody watching. Anyone who can
create or edit issues in a listed repository can steer it. So the worker is OFF by default, refuses to start without its allowlists, and every rule below FAILS CLOSED: anything
that is not known is not eligible.

OFF  CCBOARD_EPIC_WORKER is not exactly "1": no thread, no GitHub call, no kv row, no key in /api/state or in the node card. Nothing in this module runs.
REFUSED  on, but CCBOARD_EPIC_WRITES is off (the work route refuses without it), CCBOARD_EPIC_REPOS is empty, or no allowed author is known (CCBOARD_EPIC_AUTHORS empty and the gh
     login unknown): the thread does not start, the state says why and the doctor's `epic-worker` row fails. A thread that is already running and finds itself refused (gh
     failed for a moment) stays alive, looks again after the poll wait and ends only by a person's Stop or at shutdown.

ELIGIBLE (every one, in this order; the first that fails is the skip reason, a fixed word of SKIP_TEXT):
  repo         the child's repository is on CCBOARD_EPIC_REPOS (and cloned here: the epic is read only for a repository of this board's own projects)
  closed       the child is open                                        claimed / claim_unknown   its claim state is `none` (read whole, now); unknown is never free
  epic_author  the epic's author is an allowed login                    author        the child's author is an allowed login (a User, never a bot)
  not_ready    it carries the label ccboard:ready                       cycle / depends  no dependency cycle, no open `depends on` (one outside the epic counts as open)
  pinned       no ccboard:node:<alias> label for another node           held          this node did not refuse it in the last hour
  lanes_unset  this node has a lane cap (CCBOARD_NODE_LANES > 0): never more than the free lanes, and "no cap" is not a number of free lanes
  lane, window, backoff, agent, not_cloned   the shared `nodes_route.pick` against THIS node only (its own card and state): a free lane, the agent's window under the epic's limit, no
               back-off, the agent installed and logged in, the repository cloned
  then, read fresh from GitHub for the one or two candidates of the cycle (DEEP_PER_CYCLE):
  author / id_changed   the issue's author from the API: type User, a numeric id, an allowed login; an id is pinned by `login:id` in CCBOARD_EPIC_AUTHORS, else it is the id gh has
               for this board's own login, else the first id this node saw for the login (trust on first use); another id is refused
  label_by / label_unknown  the NEWEST labeled/unlabeled event of ccboard:ready in the issue's events is `labeled`, by a User with a numeric id whose login is allowed; events that
               cannot be read, too many to read (EVENT_PAGES), or no such event: not eligible. A label removed and set again by someone else is therefore not eligible.
  renamed      no `renamed` event by a login that is not allowed (the title goes into the prompt)
  edited / edit_unknown  the body's LAST editor (GraphQL `editor`, the same constant query as the presence check) is an allowed login, or the body was never edited. LIMIT: GitHub
               names the last editor only; an earlier edit by someone else followed by an allowed person's edit passes (that person edited the text as it stood).
  and the text judged is the text that runs: the work route reads the issue again before and after its claim and refuses (`changed`) when the title or body is not the same.

THE LOOP: every CCBOARD_EPIC_WORKER_POLL seconds (default 90, at least 30) plus or minus 10 percent jitter. One cycle reads the open ccboard:epic issues of the listed
repositories (nodes_epics.list_epics, ETags, 60 s cache) and up to EPICS_PER_CYCLE of them in turn (nodes_epics.detail), inside CALL_BUDGET reader calls. At most ONE start per
cycle, through the local work route `nodes_work.work_issue` (its author check, model-line guard with no bypass, redaction, fence and attempt limit): no other launch path, no
other write. The per-hour cap (CCBOARD_EPIC_WORKER_MAX_PER_HOUR, 6) and the per-day cap (CCBOARD_EPIC_WORKER_MAX_PER_DAY, 30) count the starts kept in the kv row, written
BEFORE the call, so a restart does not reset them (a call the route refused gives its start back). GitHub trouble (a rate limit, gh logged out, an outage) rests the worker for
REST_S or the wait GitHub named. A task the worker started that has waited for a person for more than PARK_AFTER_S (30 minutes) is PARKED: one fixed comment (fenced), the claim
released, the session and its worktree kept with a flag and an event. A parked issue is held for PARKED_HELD_S (24 h) and is skipped (`parked`) while its record says parked, until a
person acts (a new run of the issue replaces the record).

STOP (POST /api/epic-worker/stop): a signed-in person only (403 for the hook token, MCP and device tokens, a node token and the hub; CSRF). Immediate: no new start from that
moment (also for a start past the loop's check: the start re-reads the stop under the lock Stop takes); a work call already in flight finishes first (the claim it made then has a task, or the route released it). A claim the worker made that has no task is released. The
stop is kept in the kv row: a restart stays stopped until a person presses Start (POST /api/epic-worker/start).

THE STATE (`view()`, GET /api/epic-worker, and `epic_worker` in the node card and state.node only while the worker is on):
  {"on": true, "state": "off" | "refused" | "stopped" | "paused" | "resting" | "working" | "waiting" | "idle", "reason": <word> | null, "text": <plain sentence>, "until": <ISO> | null,
   "issues": ["owner/name#n", ...] (tasks this worker started that still run), "repos": [...], "authors": [...], "poll_s": 90, "running": <thread alive>,
   "caps": {"hour": {"used": 2, "max": 6, "clears_at": <ISO> | null}, "day": {"used": 2, "max": 30, "clears_at": <ISO> | null}},
   "cycle_at": <ISO> | null, "next_at": <ISO> | null, "at": <ISO, when this view was made>, "stopped_at": <ISO> | null,
   "skipped": {"owner/name#n": <word>, ...} (the last cycle, at most 20; an epic by a login that is not allowed is one entry for the epic), "decisions": [DECISION, ...] (newest first, at most 20)}
  DECISION: {"at": <ISO>, "kind": "start" | "skip" | "refused" | "cap" | "rest" | "wait" | "park" | "stop" | "resume" | "error", "issue": "owner/name#n" | null, "reason": <word>,
             "text": <plain sentence of the word>}
  No login, token, title, body or path is in either: an issue is named by its repository and number only. The view never runs gh: it reads the cached login only.
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from datetime import datetime, timezone

from . import github, github_write, node_state, nodes, nodes_epics, nodes_route, nodes_work, prpoll
from .config import settings

log = logging.getLogger("ccboard.nodes.worker")

KV = "epic-worker"
LOG_MAX = 100                         # decisions kept in the row
SHOWN = 20                            # decisions a view shows
STARTS_MAX = 600                      # start times kept (a day's worth at the highest cap)
HELD_MAX = 100
IDS_MAX = 50
HELD_S = 3600.0                       # an issue the work route refused is not tried again for this long
PARKED_HELD_S = 86400.0               # an issue whose task was parked is not started again for this long (and not while its record says parked)
SEEN_MAX = 500                        # issues whose last skip reason is remembered (the oldest goes first)
SKIPPED_SHOWN = 20                    # skips a view shows
PARK_AFTER_S = 1800.0                 # a task waiting for a person this long is parked and its claim released
REST_S = 300.0                        # GitHub trouble: the worker rests at least this long
EPICS_PER_CYCLE = 3
DEEP_PER_CYCLE = 2                    # candidates whose events and editor are read in one cycle
CALL_BUDGET = 80                      # reader calls one cycle may make
EVENT_PAGES = 3                       # pages of 100 events read at most; a full last page is "too many to read"
JITTER = 0.1
HOUR, DAY = 3600.0, 86400.0
TROUBLE = ("rate_limited", "no_login", "no_gh", "timeout", "unavailable", "sso")
GITHUB_REFUSALS = ("rate_limited", "no_login", "error", "unreadable", "outcome_unknown")

clock = time.time                     # epoch seconds; tests patch it (the fake GitHub's clock)
jitter = random.random                # tests patch it

SKIP_TEXT = {
    "repo": "its repository is not on CCBOARD_EPIC_REPOS",
    "closed": "the issue is closed",
    "claimed": "a node holds it (a claim comment)",
    "claim_unknown": "who holds it is not known right now, so it is not free",
    "epic_author": "the epic is not by an allowed author",
    "author": "the issue is not by an allowed author",
    "id_changed": "an allowed login now belongs to another GitHub account",
    "not_ready": "it does not carry the label ccboard:ready",
    "label_by": "the label ccboard:ready was set by a login that is not allowed",
    "label_unknown": "who set the label ccboard:ready could not be read",
    "renamed": "the title was changed by a login that is not allowed",
    "edited": "the text was last edited by a login that is not allowed",
    "edit_unknown": "who last edited the text could not be read",
    "unreadable": "the issue could not be read just now",
    "cycle": "its dependencies go in a circle",
    "depends": "it waits for another issue",
    "pinned": "it is pinned to another node",
    "held": "this node refused it in the last hour",
    "parked": "a task for it waited for a person and was parked; it stays out until a person acts",
    "lanes_unset": "this node has no lane cap (CCBOARD_NODE_LANES), so its free lanes are not known",
    "lane": "this node has no free lane",
    "window": "the agent's rate-limit window is at the limit",
    "backoff": "the agent is in a back-off after a rate limit",
    "agent": "the agent is not installed or not logged in here",
    "not_cloned": "the repository is not cloned here",
    "node": "this node cannot take it now",
    "budget": "not read in this cycle (the read budget ran out)",
}
STATE_TEXT = {
    "writes_off": "GitHub writes are off (CCBOARD_EPIC_WRITES), and the worker starts work only through the work route, which needs them. Set CCBOARD_EPIC_WRITES=1 too.",
    "repos_empty": "No repository is listed (CCBOARD_EPIC_REPOS is empty), so the worker refuses to start.",
    "authors_empty": "No allowed author is known (CCBOARD_EPIC_AUTHORS is empty and the gh login is not known), so the worker refuses to start.",
    "login_pending": "Waiting to learn the gh login of this board, which decides the allowed author.",
    "repos_not_here": "None of the listed repositories is cloned on this node.",
    "stopped": "A person stopped the worker. It starts nothing until Start is pressed.",
    "hour": "Paused by the hourly cap.",
    "day": "Paused by the daily cap.",
    "github": "GitHub is not available just now, so the worker rests.",
    "waiting": "Waiting: no ready issue can start here now.",
    "idle": "Idle: no ready issue in the listed epics.",
    "working": "Working.",
    "person": "Stopped by a person.",
    "started": "Started the issue as a task.",
    "waiting_person": "Waited for a person for more than 30 minutes: the claim was released, the task is kept.",
    "resumed": "Started again by a person.",
    "error": "The cycle failed in an unexpected way.",
}
NODE_CODE = {"lane": "lane", "lane_unknown": "lane", "window": "window", "backoff": "backoff", "agent_unknown": "agent", "agent_missing": "agent", "agent_login": "agent",
             "repo": "not_cloned", "repo_unknown": "not_cloned"}


def iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def enabled() -> bool:
    return bool(getattr(settings, "epic_worker", False))


def reason_text(word: str) -> str:
    return SKIP_TEXT.get(word) or STATE_TEXT.get(word) or nodes_work.WORK_TEXT.get(word) or nodes_epics.CLAIM_TEXT.get(word) or word


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ---------------------------------------------------------------- the row (kv `epic-worker`): only ever written while the worker is on

def _load(db) -> dict:
    try:
        rec = db.kv_get(KV)
    except Exception:
        rec = None
    v = rec.get("value") if isinstance(rec, dict) else None
    v = v if isinstance(v, dict) else {}
    starts = [float(x) for x in v.get("starts") or [] if isinstance(x, (int, float)) and not isinstance(x, bool)][-STARTS_MAX:] if isinstance(v.get("starts"), list) else []
    decisions = [d for d in v.get("decisions") or [] if isinstance(d, dict) and isinstance(d.get("kind"), str)][-LOG_MAX:] if isinstance(v.get("decisions"), list) else []
    held = {k: float(u) for k, u in (v.get("held") if isinstance(v.get("held"), dict) else {}).items() if isinstance(k, str) and isinstance(u, (int, float)) and not isinstance(u, bool)}
    mine = {k: m for k, m in (v.get("mine") if isinstance(v.get("mine"), dict) else {}).items() if isinstance(k, str) and isinstance(m, dict) and _int(m.get("task_id"))}
    ids = {k: i for k, i in (v.get("ids") if isinstance(v.get("ids"), dict) else {}).items() if isinstance(k, str) and _int(i)}
    trying = v.get("trying") if isinstance(v.get("trying"), str) else None
    return {"stopped": v.get("stopped") is True, "stopped_at": v.get("stopped_at") if isinstance(v.get("stopped_at"), (int, float)) else None, "starts": starts,
            "decisions": decisions, "held": held, "mine": mine, "ids": ids, "trying": trying}


def _save(db, row: dict) -> None:
    row["starts"] = row["starts"][-STARTS_MAX:]
    row["decisions"] = row["decisions"][-LOG_MAX:]
    while len(row["held"]) > HELD_MAX:
        row["held"].pop(min(row["held"], key=row["held"].get))
    while len(row["ids"]) > IDS_MAX:
        row["ids"].pop(next(iter(row["ids"])))
    db.kv_set(KV, row)


def _key(repo: str, n: int) -> str:
    return f"{repo.lower()}#{int(n)}"


def _parse_key(k: str):
    m = re.fullmatch(r"([^/#]{1,39})/([^/#]{1,100})#([1-9][0-9]{0,8})", k or "")
    return (m.group(1), m.group(2), int(m.group(3))) if m and github.valid_slug(m.group(1), m.group(2)) else None


# ---------------------------------------------------------------- the worker

class Worker:
    """The worker of one board. Holds no thread until `ensure()` starts one, which it does only when the worker is on and allowed."""

    def __init__(self, db, *, repos_fn=None):
        self.db = db
        self._repos_fn = repos_fn
        self._lock = threading.RLock()            # the row and the thread's state
        self._cycle = threading.Lock()            # one cycle at a time
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.state: dict = {"state": "idle", "reason": None, "until": None}
        self.cycle_at: float | None = None
        self.next_at: float | None = None
        self.rest_until = 0.0
        self.skipped: dict[str, str] = {}
        self._seen: dict[str, str] = {}            # issue key -> the last skip reason logged (a skip is logged when it changes); at most SEEN_MAX, the oldest goes first
        self._own: tuple[str | None, int | None] = (None, None)      # the gh login of this board and its numeric id, as the worker thread last learnt them
        self._probed = False                      # the worker thread has asked gh for the login at least once
        self._last_state_log: tuple | None = None
        self._rr = 0                              # where the next cycle starts in the list of epics
        self._cur: dict | None = None             # the row of the cycle in flight (its decisions and ids are saved once, at the end)

    # ---- settings and the gate
    def board_repos(self) -> list[str]:
        return list(self._repos_fn() if self._repos_fn is not None else nodes_epics.board_repos())

    def own_login(self, probe: bool = True) -> tuple[str | None, int | None]:
        """(login, numeric id) gh is authenticated as on this board. `probe` may run `gh api user` (up to 20 s on a cold cache): only the worker thread, the doctor and Start do.
        Without it only what is cached is used (the shared gh cache, else what the thread last learnt), so a page poll never waits on gh."""
        if probe:
            login = nodes_epics._gh_login()
            cached, uid = prpoll.gh_identity_cached()                      # the probe has just filled it; the id is trusted only for the login it was read for
            self._own = (login, uid if login and cached and cached.lower() == login.lower() else None)
            self._probed = True
            return self._own
        cached, uid = prpoll.gh_identity_cached()
        return (cached, uid) if cached else self._own

    def allowed(self, probe: bool = True) -> set[str]:
        return nodes_epics.allowed_authors(None if settings.epic_authors else self.own_login(probe)[0])

    def refusal(self, probe: bool = True) -> str | None:
        """Why the worker may not run (a word of STATE_TEXT), or None. Off is not a refusal: an off worker is simply not there. `probe=False` (a view) never asks gh for the
        login: with none known yet the word is `login_pending` until the worker thread has asked once."""
        if not github_write.writes_enabled():
            return "writes_off"
        if not settings.epic_repos:
            return "repos_empty"
        if not self.allowed(probe):
            return "authors_empty" if probe or self._probed else "login_pending"
        return None

    def repos_here(self) -> list[str]:
        """The listed repositories that are cloned here, spelled as this board's own remote spells them."""
        listed = {r.lower() for r in settings.epic_repos}
        return [s for s in self.board_repos() if s.lower() in listed]

    def interval(self) -> float:
        poll = max(30.0, float(settings.epic_worker_poll or 90))
        return max(30.0, poll * (1.0 - JITTER + 2 * JITTER * jitter()))

    # ---- the row
    def row(self) -> dict:
        return _load(self.db)

    def _change(self, fn) -> dict:
        with self._lock:
            row = _load(self.db)
            fn(row)
            _save(self.db, row)
            return row

    def decide(self, kind: str, reason: str, issue: str | None = None, row: dict | None = None) -> None:
        """Record one decision: a fixed kind, a fixed reason word, the issue's repository and number. Nothing else."""
        d = {"at": iso(clock()), "kind": kind, "issue": issue, "reason": reason}
        if row is None and self._cur is not None:
            self._cur["decisions"].append(d)
        elif row is not None:
            row["decisions"].append(d)
        else:
            self._change(lambda r: r["decisions"].append(d))
        log.info("epic worker: %s %s %s", kind, issue or "-", reason)

    def _state(self, state: str, reason: str | None = None, until: float | None = None, kind: str | None = None, row: dict | None = None) -> None:
        self.state = {"state": state, "reason": reason, "until": until}
        if kind is not None and (kind, reason) != self._last_state_log:
            self._last_state_log = (kind, reason)
            self.decide(kind, reason or state, None, row)
        elif kind is None and state in ("idle", "working"):
            self._last_state_log = None

    def _see(self, k: str, why: str | None) -> bool:
        """Remember the last skip reason of an issue (or forget it with None). True when `why` is new for `k`, i.e. the skip is to be logged. The memory holds SEEN_MAX issues at most."""
        if why is None:
            self._seen.pop(k, None)
            return False
        if self._seen.get(k) == why:
            return False
        self._seen.pop(k, None)
        self._seen[k] = why
        while len(self._seen) > SEEN_MAX:
            self._seen.pop(next(iter(self._seen)))
        return True

    # ---- the thread
    def ensure(self) -> bool:
        """Start the loop when the worker is on, not stopped by a person and allowed. True when a thread started. With the worker off nothing at all is read or written."""
        if not enabled():
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            why = self.refusal()
            if why:
                if self.state.get("reason") != why:
                    self._state("refused", why, kind="refused")
                return False
            if self.row()["stopped"]:
                self.state = {"state": "stopped", "reason": "stopped", "until": None}
                return False
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._stop,), name="epic-worker", daemon=True)
            self._thread.start()
            return True

    def running(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive())

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 15.0) -> None:
        """Shutdown (not a person's Stop: nothing is kept). The loop ends after the cycle in flight."""
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=timeout)

    def _run(self, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    self.tick(stop)
                except Exception as e:                                  # a cycle never ends the thread
                    log.warning("epic worker cycle failed: %s", e.__class__.__name__)
                    self.decide("error", "error")
                if stop.is_set() or self.state.get("state") == "stopped":          # refused is not the end: gh may only have failed for a moment, so it looks again after the wait
                    break
                wait = self.interval()
                self.next_at = clock() + wait
                self._wake.wait(wait)
                self._wake.clear()
        finally:
            self.next_at = None
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    # ---- a person's Stop and Start
    def stop_worker(self) -> dict:
        """Stop worker: kept in the row (a restart stays stopped), the loop ends at once, a claim the worker made that has no task is released."""
        if not enabled():
            return self.view()

        def mark(r):
            r["stopped"], r["stopped_at"] = True, clock()
            self.decide("stop", "person", None, r)
        self._change(mark)
        self._stop.set()
        self._wake.set()
        self.state = {"state": "stopped", "reason": "stopped", "until": None}
        self.release_untasked(wait=5.0)
        return self.view()

    def start_worker(self) -> dict:
        """Start again after a Stop (a person). Refused (the state says why) when the worker is off or not allowed."""
        if not enabled():
            return self.view()

        was = []

        def mark(r):
            if r["stopped"]:
                was.append(True)
                r["stopped"], r["stopped_at"] = False, None
                self.decide("resume", "resumed", None, r)
        self._change(mark)
        if was:
            self.state = {"state": "idle", "reason": None, "until": None}
            self._last_state_log = None
        self.ensure()
        if was:
            self.wake()                                                     # only a stopped worker is woken: Start on a running one must not force cycles faster than the poll
        return self.view()

    def release_untasked(self, wait: float = 5.0) -> list[str]:
        """Release the claim of the issue the worker was starting when it has no task (a crash or a stop in the middle). Under the issue's work lock, so a work call in flight
        finishes first (and then has its task, or released the claim itself). Returns the issues released."""
        row = self.row()
        k = row.get("trying")
        if not k:
            return []
        p = _parse_key(k)
        done: list[str] = []
        if p is not None:
            lk = nodes_work._lock_for(self.db, p[0], p[1], p[2])
            if not lk.acquire(timeout=wait):
                return []                                                   # the call is still going: the thread sweeps when it ends
            try:
                actor = nodes_epics.board_actor(self.db)
                for v in nodes_epics.held_claims(actor):
                    if _key(v["repo"], v["issue"]) == k and v["state"] == "held" and not v.get("task_id"):
                        r = nodes_epics.release(v["claim_ref"], "released", actor=actor)
                        if r.get("released"):
                            done.append(k)
                            self.decide("stop", "released", k)
            finally:
                lk.release()
        self._change(lambda r: r.update(trying=None))
        return done

    # ---- one cycle
    def caps(self, row: dict, now: float) -> dict:
        out = {}
        for name, span, cap in (("hour", HOUR, settings.epic_worker_max_hour), ("day", DAY, settings.epic_worker_max_day)):
            inside = sorted(t for t in row["starts"] if now - t < span)
            clears = (inside[len(inside) - cap] + span) if len(inside) >= cap else None
            out[name] = {"used": len(inside), "max": cap, "clears_at": clears}
        return out

    def tick(self, stop: threading.Event | None = None) -> dict:
        """One cycle. Returns {started: "owner/name#n" | None}. Never more than one start."""
        stop = stop or threading.Event()
        if not enabled():
            return {"started": None}
        with self._cycle:
            now = clock()
            self.cycle_at = now
            why = self.refusal()
            if why:
                self._state("refused", why, kind="refused")
                return {"started": None}
            row = self.row()
            if row["trying"]:
                self.release_untasked(wait=0.5)
            if row["stopped"] or self._stop.is_set() and stop is self._stop:
                self.state = {"state": "stopped", "reason": "stopped", "until": None}
                return {"started": None}
            self._park_due(now)
            row = self.row()
            caps = self.caps(row, now)
            for name in ("day", "hour"):
                c = caps[name]
                if c["used"] >= c["max"]:
                    self._state("paused", name, c["clears_at"], kind="cap")
                    return {"started": None}
            if now < self.rest_until:
                self._state("resting", "github", self.rest_until)
                return {"started": None}
            here = self.repos_here()
            if not here:
                self._state("waiting", "repos_not_here", kind="wait")
                return {"started": None}
            self._cur = {"decisions": [], "ids": {}}
            try:
                return self._look(row, here, now, stop)
            finally:
                self._flush()

    def _flush(self) -> None:
        """Merge what the cycle learnt (its decisions, the ids it saw) into the row as it is NOW: a Stop pressed during the cycle is never written over."""
        cur, self._cur = self._cur, None
        if cur is None or not (cur["decisions"] or cur["ids"]):
            return

        def merge(r):
            r["decisions"].extend(cur["decisions"])
            for k, v in cur["ids"].items():
                r["ids"].setdefault(k, v)
        self._change(merge)

    def _rest(self, wait_s) -> None:
        w = float(wait_s) if isinstance(wait_s, (int, float)) and not isinstance(wait_s, bool) and 0 < wait_s <= 86400 else 0.0
        self.rest_until = clock() + max(REST_S, w)
        self._state("resting", "github", self.rest_until, kind="rest")

    def _look(self, row: dict, here: list[str], now: float, stop: threading.Event) -> dict:
        rd = github.reader
        budget = rd.calls + CALL_BUDGET
        lst = nodes_epics.list_epics(self.db, here)
        err = lst.get("error")
        if err and err.get("reason") in TROUBLE:
            self._rest(err.get("wait_s"))
            return {"started": None}
        epics = lst.get("epics") or []
        if epics:
            start = self._rr % len(epics)
            epics = (epics[start:] + epics[:start])[:EPICS_PER_CYCLE]
            self._rr += EPICS_PER_CYCLE
        allowed = self.allowed()
        listed = {r.lower() for r in settings.epic_repos}
        board = self.board_repos()
        record = None
        candidates, skipped, waits = [], {}, []
        for e in epics:
            if stop.is_set():
                break
            if rd.calls >= budget:
                break
            d = nodes_epics.detail(self.db, e["owner"], e["name"], e["number"], [], repos=board)
            derr = d.get("error")
            if derr and derr.get("reason") in TROUBLE:
                self._rest(derr.get("wait_s"))
                return {"started": None}
            if d["epic"].get("labelled") is not True or d["epic"].get("state") != "open":
                continue
            if (d.get("authors") or {}).get("epic_ok") is not True:           # an epic by a login that is not allowed: ONE decision for the epic, none for its children
                ek = _key(f"{e['owner']}/{e['name']}", e["number"])
                skipped[ek] = "epic_author"
                continue
            kids = {f"{c['repo']}#{c['number']}".lower(): c for c in d["children"]}
            st = nodes_epics.load_settings(self.db, e["owner"], e["name"], e["number"])
            for c in sorted(d["children"], key=lambda x: x["order"]):
                k = _key(c["repo"], c["number"])
                if c["state"] != "open":
                    continue                                                   # a closed issue is done: not logged, never a candidate
                why = self._cheap(c, d, kids, listed, row, now)
                if why is None:
                    if record is None:
                        record = self.self_record()
                    why, until = self._pick(c, record, st)
                    if why is not None:
                        waits.append((why, until))
                if why is not None:
                    skipped[k] = why
                    continue
                candidates.append((c, d, st))
        self.skipped = skipped
        for k, why in skipped.items():
            if self._see(k, why):
                self.decide("skip", why, k)
        started = None
        deep = 0
        for c, d, st in candidates:
            if stop.is_set() or deep >= DEEP_PER_CYCLE or rd.calls >= budget + CALL_BUDGET // 2:
                break
            deep += 1
            k = _key(c["repo"], c["number"])
            why, digest = self._deep(c, allowed, row)
            if why is not None:
                self.skipped[k] = why
                if self._see(k, why):
                    self.decide("skip", why, k)
                if why == "unreadable" and self.rest_until > clock():
                    break
                continue
            self._flush()                                                      # the decisions so far and the ids seen, before the call
            started = self._start(c, d, st, digest, stop)
            break                                                              # ONE start (or one refused try) per cycle
        self._flush()
        if self.rest_until > clock():
            return {"started": started}
        mine = self._active_mine()
        if started or mine:
            self._state("working", None)
        elif waits:
            why, until = sorted(waits, key=lambda w: (w[1] is None, w[1] or 0))[0]
            self._state("waiting", why, until)
        else:
            self._state("idle", None)
        return {"started": started}

    def _cheap(self, c: dict, d: dict, kids: dict, listed: set, row: dict, now: float) -> str | None:
        """The checks the epic's reading already answers, in the module's order: the first that fails, or None."""
        if c["repo"].lower() not in listed:
            return "repo"
        if c.get("claim_state") == "unknown":
            return "claim_unknown"
        if c.get("claim") is not None or c.get("claim_state") != "none":
            return "claimed"
        if (d.get("authors") or {}).get("epic_ok") is not True:
            return "epic_author"
        if c.get("author_ok") is not True:
            return "author"
        if nodes_epics.READY_LABEL not in (c.get("labels") or []):
            return "not_ready"
        if c.get("dependency_cycle"):
            return "cycle"
        if c.get("blocked_by"):
            return "depends"
        for dep in c.get("depends_on") or []:
            ref = f"{c['repo']}#{dep[1:]}".lower() if dep.startswith("#") else dep.lower()
            if ref not in kids:
                return "depends"                                               # outside the epic: not read here, so not known to be closed (fail closed)
        mine = nodes_epics.alias_report()["alias"].lower()
        if any(lb.startswith(nodes_epics.NODE_LABEL) and lb[len(nodes_epics.NODE_LABEL):].lower() != mine for lb in c.get("labels") or []):
            return "pinned"
        if row["held"].get(_key(c["repo"], c["number"]), 0) > now:
            return "held"
        o, r = c["repo"].split("/", 1)
        rec = nodes_work._load(self.db, o, r, c["number"])
        if rec is not None and rec.get("parked") is True and rec["state"] == "abandoned":
            return "parked"                                                    # until a person acts: a new run of the issue writes a new record
        lanes = nodes.lanes_view(self.db)
        if not lanes.get("cap"):
            return "lanes_unset"
        return None

    def self_record(self) -> dict:
        """This node as a hub record, for the shared pick: online, the tasks scope, its own card (agents) and state (projects, usage, lanes, login problems)."""
        from . import main
        try:
            state = node_state.build(self.db, main._scan_entry())
        except Exception as e:
            log.debug("own state for the worker failed: %s", e.__class__.__name__)
            state = None
        card = {"agents": nodes.agents_view(self.db), "accounts": {"items": []}, "load": {}}
        return {"handle": nodes.LOCAL, "name": nodes.display_name(), "status": "online", "scopes": ["tasks"], "card": card, "state": state,
                "last_ok_at": iso(nodes_route.clock()), "legacy": False}

    def _pick(self, c: dict, record: dict, st: dict) -> tuple[str | None, float | None]:
        res = nodes_route.pick({"repo": c["repo"], "number": c["number"], "agent": c.get("agent") or "claude"}, [record], None, [],
                               {"max_window_pct": nodes_epics.max_window_pct(st), "now": nodes_route.clock()})
        if res["handle"] is not None:
            return None, None
        w = res.get("waiting") or {}
        return NODE_CODE.get(w.get("code"), "node"), w.get("until")

    def _id_ok(self, row: dict, login: str, uid: int) -> bool:
        """An allowed login is bound to a numeric id: the one CCBOARD_EPIC_AUTHORS pins (`login:id`), else the id gh has for this board's own login, else the first id this node saw
        for it (trust on first use): a renamed account whose login someone else took is not the same person."""
        k = login.lower()
        own_login, own_id = self._own
        want = settings.epic_author_ids.get(k) or (own_id if own_login and own_login.lower() == k else None)      # a pinned id, else the id gh has for this board's own login
        if want is not None and want != uid:
            return False
        if k in row["ids"] and row["ids"][k] != uid:
            return False
        row["ids"][k] = uid
        if self._cur is not None:
            self._cur["ids"].setdefault(k, uid)
        return True

    def _user_ok(self, u, allowed: set[str], row: dict) -> str | None:
        """None when `u` (a GitHub user object) is a User with a numeric id and an allowed login; else the failing part: `who` or `id_changed`."""
        if not isinstance(u, dict) or u.get("type") != "User" or not _int(u.get("id")) or u["id"] <= 0:
            return "who"
        login = u.get("login")
        if not isinstance(login, str) or not github.OWNER_RE.fullmatch(login) or login.lower() not in allowed:
            return "who"
        return None if self._id_ok(row, login, u["id"]) else "id_changed"

    def _deep(self, c: dict, allowed: set[str], row: dict) -> tuple[str | None, str | None]:
        """The fresh reads of one candidate: the issue, its events, its body's last editor. (None, digest of the text judged) when eligible, else (the reason word, None)."""
        o, r = c["repo"].split("/", 1)
        n = c["number"]
        try:
            rep = github.reader.get(github.issue_path(o, r, n), 0.0)
        except github.GhReadError as e:
            if e.reason in TROUBLE:
                self._rest(e.wait_s)
            return "unreadable", None
        issue = rep.data
        if not rep.fresh or not isinstance(issue, dict) or issue.get("number") != n or "pull_request" in issue:
            return "unreadable", None
        if issue.get("state") != "open":
            return "closed", None
        bad = self._user_ok(issue.get("user"), allowed, row)
        if bad:
            return ("author" if bad == "who" else bad), None
        labels = [lb.get("name") for lb in issue.get("labels") or [] if isinstance(lb, dict)]
        if nodes_epics.READY_LABEL not in labels:
            return "not_ready", None
        events, complete = [], False
        try:
            for page in range(1, EVENT_PAGES + 1):
                got = github.reader.get(github.events_path(o, r, n, page), 0.0)
                if not got.fresh or not isinstance(got.data, list):
                    return "label_unknown", None
                events += [e for e in got.data if isinstance(e, dict)]
                if len(got.data) < github.PER_PAGE:
                    complete = True
                    break
        except github.GhReadError as e:
            if e.reason in TROUBLE:
                self._rest(e.wait_s)
            return "label_unknown", None
        if not complete:
            return "label_unknown", None
        why = self._label_verdict(events, allowed, row)
        if why:
            return why, None
        why = self._editor_verdict(issue, allowed, row)
        if why:
            return why, None
        return None, nodes_work.text_digest(issue)

    def _label_verdict(self, events: list[dict], allowed: set[str], row: dict) -> str | None:
        def order(e):
            return (github.clean_ts(e.get("created_at")) or "", e.get("id") if _int(e.get("id")) else 0)
        ready = [e for e in events if e.get("event") in ("labeled", "unlabeled") and isinstance(e.get("label"), dict) and e["label"].get("name") == nodes_epics.READY_LABEL]
        if not ready:
            return "label_unknown"
        last = max(ready, key=order)
        if last.get("event") != "labeled" or not github.clean_ts(last.get("created_at")):
            return "label_unknown"
        bad = self._user_ok(last.get("actor"), allowed, row)
        if bad:
            return "label_by" if bad == "who" else bad
        for e in events:
            if e.get("event") == "renamed" and self._user_ok(e.get("actor"), allowed, row):
                return "renamed"
        return None

    def _editor_verdict(self, issue: dict, allowed: set[str], row: dict) -> str | None:
        node_id = github.clean_node_id(issue.get("node_id"))
        iid = issue.get("id")
        if node_id is None or not _int(iid):
            return "edit_unknown"
        try:
            status, _h, data = github.graphql_editor(node_id)
        except github.GhReadError as e:
            if e.reason in TROUBLE:
                self._rest(e.wait_s)
            return "edit_unknown"
        if status != 200 or github.graphql_reason(data):
            return "edit_unknown"
        v = github.parse_editor(data, iid)
        if v is None:
            return "edit_unknown"
        if not v["edited"]:
            return None
        login, uid = v.get("editor_login"), v.get("editor")
        if not login or uid is None or login.lower() not in allowed:
            return "edited"
        return None if self._id_ok(row, login, uid) else "id_changed"

    def _start(self, c: dict, d: dict, st: dict, digest: str, stop: threading.Event | None = None) -> str | None:
        """The one start of a cycle, through the local work route. The start is counted in the row BEFORE the call (a restart keeps it); a refusal gives it back and holds the issue.
        A Stop (or a shutdown) that came while the candidate was being read is seen here, under the lock Stop takes: then nothing is counted, called or logged."""
        k = _key(c["repo"], c["number"])
        t0 = clock()
        go = []

        def before(r):
            if r["stopped"] or (stop is not None and stop.is_set()):
                return
            go.append(True)
            r["starts"].append(t0)
            r["trying"] = k
        self._change(before)
        if not go:
            return None
        ep = d["epic"]
        try:
            out = nodes_work.work_issue(c["repo"], c["number"], epic=f"{ep['repo']}#{ep['number']}", report=st["report"], auto_pr=st["auto_pr"], close=st["close"],
                                        expect=digest)
        except nodes_work.Refused as e:
            def refused(r):
                if t0 in r["starts"]:
                    r["starts"].remove(t0)                                     # nothing started: the start is given back
                r["held"][k] = clock() + HELD_S
                r["trying"] = None
                self.decide("refused", e.reason, k, r)
            self._change(refused)
            self._seen[k] = "held"
            if e.reason in GITHUB_REFUSALS:
                self._rest(e.facts.get("wait_s"))
            return None
        except Exception as e:                                                  # the outcome is not known: the start stays counted, the issue is held
            log.warning("epic worker: the work call failed: %s", e.__class__.__name__)

            def failed(r):
                r["held"][k] = clock() + HELD_S
                self.decide("error", "error", k, r)
            self._change(failed)
            self.release_untasked(wait=0.5)
            return None

        def won(r):
            if _int(out.get("id")):
                r["mine"][k] = {"task_id": out["id"], "at": t0}
            r["trying"] = None
            self.decide("start", "started", k, r)
        self._change(won)
        self._seen.pop(k, None)
        nodes_epics._audit(nodes_epics.Actor(self.db, "", None, ""), "epic-worker-start", True, *k.split("#")[0].split("/"), c["number"], "started")
        return k

    # ---- the park rule and the tasks this worker started
    def _active_mine(self) -> list[str]:
        out = []
        for k, m in self.row()["mine"].items():
            p = _parse_key(k)
            rec = nodes_work._load(self.db, p[0], p[1], p[2]) if p else None
            if rec is not None and rec["task_id"] == m["task_id"] and rec["state"] == "active":
                out.append(k)
        return sorted(out)

    def _park_due(self, now: float) -> None:
        """Park every task this worker started that has waited for a person for more than PARK_AFTER_S; forget the ones that ended."""
        row = self.row()
        gone = []
        for k, m in row["mine"].items():
            p = _parse_key(k)
            rec = nodes_work._load(self.db, p[0], p[1], p[2]) if p else None
            if rec is None or rec["task_id"] != m["task_id"] or rec["state"] != "active":
                gone.append(k)
                continue
            t = self.db.task_get(rec["task_id"])
            since = nodes_work.asking_since(self.db, t) if t else None
            if since is not None and now - since >= PARK_AFTER_S:
                if self._park(p, rec["task_id"]):
                    gone.append(k)
        if gone:
            self._change(lambda r: [r["mine"].pop(k, None) for k in gone])

    def _park(self, p: tuple, task_id: int) -> bool:
        """One fixed comment (fenced, the same comment write as every phase comment), the claim released, the record set aside; the session and the worktree stay as they are.
        True when it is done (or nothing is left to do); False to try again next cycle."""
        owner, repo, n = p
        actor = nodes_epics.board_actor(self.db)
        with nodes_work._lock_for(self.db, owner, repo, n):
            rec = nodes_work._load(self.db, owner, repo, n)
            if rec is None or rec["state"] != "active" or rec["task_id"] != task_id:
                return True
            if rec.get("posted_phase") != "parked":
                try:
                    body = github_write.progress_body(rec["alias"], "parked")
                except ValueError:
                    return False
                out = nodes_work._send_comment(actor, rec, body)
                if out != "posted":
                    return out == "fence" and nodes_work._load(self.db, owner, repo, n) is None
                rec.update(posted_phase="parked", posted_at=nodes_work.clock())
                nodes_work._save(self.db, rec)
            r = nodes_epics.release(rec["claim_ref"], "released", actor=actor)
            if not r.get("released") and not r.get("lost"):
                return False
            rec.update(state="abandoned", finished_at=nodes_work.clock(), parked=True)
            nodes_work._save(self.db, rec)
        t = self.db.task_get(task_id) or {}
        if t.get("tmux_name"):
            try:
                self.db.update_flags(t["tmux_name"], {"epic_parked": "parked"})
                self.db.add_event(t["tmux_name"], "EpicParked", "parked", "waited for a person for more than 30 minutes; the claim was released, this task and its worktree are kept",
                                  {"task": task_id}, agent=t.get("agent"))
            except Exception as e:
                log.debug("park flag failed: %s", e.__class__.__name__)
        self._change(lambda r: r["held"].__setitem__(_key(f"{owner}/{repo}", n), clock() + PARKED_HELD_S))          # not started again by the next cycle while its session still waits
        self.decide("park", "waiting_person", _key(f"{owner}/{repo}", n))
        return True

    # ---- the view
    def view(self) -> dict:
        """The worker's state for Settings > Nodes and the hub's node page (the shape is in the module text). Reads the row only; asks GitHub nothing and never runs gh: the
        allowed authors come from the cached login (an unknown one reads as no author and the state says it is waiting to learn it)."""
        if not enabled():
            return off_view()
        why = self.refusal(probe=False)
        row = self.row()
        now = clock()
        st = dict(self.state)
        if why:
            st = {"state": "refused", "reason": why, "until": None}
        elif row["stopped"]:
            st = {"state": "stopped", "reason": "stopped", "until": None}
        caps = self.caps(row, now)
        reason = st.get("reason")
        if st["state"] == "working":
            text = "Working on " + ", ".join(self._active_mine()) + "." if self._active_mine() else STATE_TEXT["working"]
        elif st["state"] == "paused":
            text = STATE_TEXT[reason] + (f" It clears at {nodes_route.hhmm(st['until'])}." if st.get("until") else "")
        elif st["state"] == "waiting" and reason in SKIP_TEXT:
            text = "Waiting: " + SKIP_TEXT[reason] + (f", until {nodes_route.hhmm(st['until'])}." if st.get("until") else ".")
        elif st["state"] == "resting":
            text = STATE_TEXT["github"] + (f" It looks again at {nodes_route.hhmm(st['until'])}." if st.get("until") else "")
        else:
            text = STATE_TEXT.get(reason or "") or STATE_TEXT.get(st["state"], "")
        try:
            authors = sorted(self.allowed(probe=False))
        except Exception:
            authors = []
        decisions = [{"at": d.get("at"), "kind": d.get("kind"), "issue": d.get("issue"), "reason": d.get("reason"), "text": reason_text(str(d.get("reason") or ""))}
                     for d in reversed(row["decisions"][-SHOWN:])]
        return {"on": True, "state": st["state"], "reason": reason, "text": text, "until": iso(st.get("until")), "issues": self._active_mine(),
                "repos": list(settings.epic_repos), "authors": authors, "poll_s": max(30, int(settings.epic_worker_poll or 90)), "running": self.running(),
                "caps": {k: {"used": v["used"], "max": v["max"], "clears_at": iso(v["clears_at"])} for k, v in caps.items()},
                "cycle_at": iso(self.cycle_at), "next_at": iso(self.next_at), "at": iso(now), "stopped_at": iso(row["stopped_at"]),
                "skipped": dict(list(self.skipped.items())[:SKIPPED_SHOWN]), "decisions": decisions}


def off_view() -> dict:
    return {"on": False, "state": "off", "reason": None, "text": "The worker is off (CCBOARD_EPIC_WORKER).", "until": None}


# ---------------------------------------------------------------- a peer's view, as the hub keeps it

PEER_STATES = ("off", "refused", "stopped", "paused", "resting", "working", "waiting", "idle")
PEER_KINDS = ("start", "skip", "refused", "cap", "rest", "wait", "park", "stop", "resume", "error")
PEER_ISSUES_MAX = 5
PEER_LIST_MAX = 25
_ISO_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def _word(v) -> str | None:
    """A reason word this module knows (one of its fixed sentences), else None."""
    return v if isinstance(v, str) and (v in SKIP_TEXT or v in STATE_TEXT or v in nodes_work.WORK_TEXT or v in nodes_epics.CLAIM_TEXT) else None


def _when(v) -> str | None:
    return v if isinstance(v, str) and _ISO_RE.fullmatch(v) else None


def _count(v, hi: int) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= hi else 0


def _ref(v) -> str | None:
    p = _parse_key(v) if isinstance(v, str) else None
    return f"{p[0]}/{p[1]}#{p[2]}" if p else None


def _peer_text(state: str, reason: str | None) -> str:
    """The sentence of a peer's state, built here from the state word and the reason word: nothing the peer wrote."""
    if state == "off":
        return off_view()["text"]
    if state == "paused":
        return STATE_TEXT[reason] if reason in ("hour", "day") else ""
    if state == "waiting" and reason in SKIP_TEXT:
        return "Waiting: " + SKIP_TEXT[reason] + "."
    if state == "resting":
        return STATE_TEXT["github"]
    if state == "refused":
        return STATE_TEXT[reason] if reason in ("writes_off", "repos_empty", "authors_empty", "login_pending") else ""
    return STATE_TEXT.get(state, "")


def peer_view(v) -> dict | None:
    """The worker view a PAIRED node reported in its card, rebuilt field by field for the hub's node page: a state word from PEER_STATES, a reason word of this module, numbers,
    ISO times, issue references checked as owner/name#n, at most 20 decisions with a fixed kind and a reason word, and every sentence built here from those words. Nothing a peer
    wrote as text is kept. None when it is not an object or its state is not one of ours (the page then shows no worker)."""
    if not isinstance(v, dict) or v.get("state") not in PEER_STATES:
        return None
    state = v["state"]
    reason = _word(v.get("reason"))
    caps = v.get("caps") if isinstance(v.get("caps"), dict) else {}

    def cap(c):
        c = c if isinstance(c, dict) else {}
        return {"used": _count(c.get("used"), 100000), "max": _count(c.get("max"), 100000), "clears_at": _when(c.get("clears_at"))}
    repos = [x for x in (v.get("repos") if isinstance(v.get("repos"), list) else [])[:PEER_LIST_MAX]
             if isinstance(x, str) and re.fullmatch(r"[^/]{1,39}/[^/]{1,100}", x) and github.valid_slug(*x.split("/", 1))]
    authors = [x for x in (v.get("authors") if isinstance(v.get("authors"), list) else [])[:PEER_LIST_MAX] if isinstance(x, str) and github.OWNER_RE.fullmatch(x)]
    decisions = []
    for d in (v.get("decisions") if isinstance(v.get("decisions"), list) else [])[:SHOWN]:
        if not isinstance(d, dict) or d.get("kind") not in PEER_KINDS:
            continue
        w = _word(d.get("reason"))
        decisions.append({"at": _when(d.get("at")) or "", "kind": d["kind"], "issue": _ref(d.get("issue")), "reason": w or "", "text": reason_text(w) if w else ""})
    poll = v.get("poll_s")
    return {"on": state != "off", "state": state, "reason": reason, "text": _peer_text(state, reason), "until": _when(v.get("until")),
            "issues": [r for r in (_ref(x) for x in (v.get("issues") if isinstance(v.get("issues"), list) else [])[:PEER_ISSUES_MAX]) if r],
            "repos": repos, "authors": authors, "poll_s": poll if isinstance(poll, int) and not isinstance(poll, bool) and 30 <= poll <= 3600 else None,
            "running": v.get("running") is True, "caps": {"hour": cap(caps.get("hour")), "day": cap(caps.get("day"))},
            "cycle_at": _when(v.get("cycle_at")), "next_at": _when(v.get("next_at")), "at": _when(v.get("at")), "stopped_at": _when(v.get("stopped_at")),
            "decisions": decisions}


# ---------------------------------------------------------------- the doctor row

def doctor_row(db) -> tuple[str, str]:
    """(status, detail) of the doctor's `epic-worker` row: skip when off; fail when on and refused (an empty allowlist, writes off); warn whenever it is on, listing the
    allowlists. Reads the settings and the gh login only (no GitHub call beyond the cached login)."""
    if not enabled():
        return "skip", "the worker is off (CCBOARD_EPIC_WORKER): this node starts nothing by itself"
    repos = ", ".join(settings.epic_repos) or "none"
    try:
        allowed = sorted(nodes_epics.allowed_authors(None if settings.epic_authors else nodes_epics._gh_login()))
    except Exception:
        allowed = []
    authors = ", ".join(allowed) or "none"
    lists = f"repositories: {repos}; authors: {authors}"
    if not settings.epic_repos:
        return "fail", f"the worker is on but CCBOARD_EPIC_REPOS is empty, so it refuses to start ({lists})"
    if not allowed:
        return "fail", f"the worker is on but no allowed author is known (CCBOARD_EPIC_AUTHORS is empty and the gh login is not known), so it refuses to start ({lists})"
    if not github_write.writes_enabled():
        return "fail", f"the worker is on but CCBOARD_EPIC_WRITES is off, and it starts work only through the work route, which needs it ({lists})"
    bad = [k for k, v in (("CCBOARD_EPIC_REPOS", settings.epic_repos_bad), ("CCBOARD_EPIC_WORKER_POLL", settings.epic_worker_poll_bad),
                          ("CCBOARD_EPIC_WORKER_MAX_PER_HOUR", settings.epic_worker_max_hour_bad), ("CCBOARD_EPIC_WORKER_MAX_PER_DAY", settings.epic_worker_max_day_bad)) if v]
    note = f"; {', '.join(bad)} had a value that was not used" if bad else ""
    return "warn", (f"the worker is on: text in an issue of these repositories by these authors, labelled ccboard:ready by one of them, starts an agent with acceptEdits on this "
                    f"machine with nobody watching ({lists}); at most {settings.epic_worker_max_hour} an hour and {settings.epic_worker_max_day} a day{note}")
