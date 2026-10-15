"""The run loop of an epic: send each ready issue to the node routing picks (nodes epic, issues #150 and #152).

What it is: a small loop on the hub that, for an epic a person pressed Run on (or an agent started with `epic_run`, when the owner allowed it), looks at the epic's issues every poll cycle,
asks app/nodes_route.pick which node can take each ready one, and sends it through the EXISTING relay row `epic_work` (POST /api/nodes/<handle>/epics/work), whose node side runs the
claim, the author check and the guard. Nothing here starts an agent, builds a launch option or writes to GitHub: it chooses and it asks.

When it runs: only with GitHub writes on (CCBOARD_EPIC_WRITES, the work route refuses otherwise), at least one paired node and at least one run that is active and not paused. A board with
no paired node has no thread, no kv row and no key in /api/state from this module; Run is refused and says why. The thread starts on Run (or at start-up for a run that was active when the
board stopped), ends when no run is active, and a failure in a cycle is logged and counted, never raised into the hub's poller (a separate thread).

The rules of a cycle (each pinned by tests/test_epics_route.py):
  READY       an open child with no claim whose claim state is KNOWN (`none`; an unknown claim is never free), no unfinished `depends on` (a dependency outside the epic is read and must be closed;
              a dependency in a repository this board does not have is not ready), no dependency cycle, by an allowed author, not assigned in the last two cycles, not held.
  ONE AT A TIME one work call at a time, started at least a second after the last one ended (GitHub's write rule), never more than the lanes the node has free after the ones this board
              reserved (nodes_route reservations, which expire after two poll cycles by themselves).
  PAUSE       a paused epic gets no new assignment, checked again before every call; claims already made go on by themselves on their nodes.
  OUTCOMES    won: the node holds it, a lane is reserved, why and the node are written on the epic's run row and an `out` audit row (action epic_route) names who started the run.
              taken / lost: another node has it; it is read again next cycle. lanes full / writes off / not reachable: that node is not asked again for two cycles. A call whose outcome is
              not known (a timeout, a broken connection) is not repeated for ten minutes unless the issue's claim shows up. An issue the node refused for its own reasons (author, bypass,
              attempts) is held for five minutes and the reason is shown.
  WITHDRAWN   a run an agent started needs, at the top of every cycle and again before every call, what let it start: the epic's `mcp_run` on, the board's MCP level `tasks`, GitHub writes
              on, and the device token that started it still there. When a person takes one back the run ends as `stopped`, an audit row names the fixed word (mcp_run_off, mcp_off, mcp_level,
              writes_off, token_revoked), nothing more is sent, and a restart does not bring it back. A run a person started needs only the writes (it waits, it does not end). The shim's
              `local-token` has nothing to revoke, and a token is matched by its name (a new token of the same name counts as the same device).
  FINISH      the run ends when no issue of the epic is open and unclaimed any more (all closed, held for a person, or claimed and running), or when the number of issues an agent allowed ran out.

THE AGENT'S WAY (`epic_run`, issue #152) is an action tool: it starts this loop for an epic, and only when (all of them, in this order) the board's MCP level allows tasks, the caller's own scope
holds tasks, the epic's setting `mcp_run` is on (default off; only a signed-in person sets it, on the epic page), the epic is not paused, it has 10 issues or fewer in the call, and the
caller has made fewer than 6 calls in the hour. The run is audited with who asked (`mcp:<token name>` or `local-token`). Nothing an agent read anywhere else can start work without that
switch having been set by a person.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from . import github, github_write, mcp_tokens, nodes, nodes_epics, nodes_relay, nodes_route, projects
from .config import settings

log = logging.getLogger("ccboard.nodes.run")

KV = "epic-run:"
INDEX = "epic-run-index"
CALL_GAP_S = 1.0                      # seconds from the end of one work call to the start of the next
HELD_S = 300.0                        # an issue a node refused for its own reasons is not sent again for this long
UNCLEAR_S = 600.0                     # an issue whose outcome is not known is not sent again for this long
BLOCK_CYCLES = 2                      # poll cycles a node that refused or could not be reached is left alone
CYCLE_MIN_S = 5.0
SYNC_BUDGET_S = 20.0                  # the first cycle of an agent's epic_run runs on its request for this long at most; the loop goes on in the background
ASSIGNED_MAX = 200
DEP_READS = 10                        # GitHub reads of dependencies outside the epic one cycle may make
ISSUES_PER_CALL = 10                  # epic_run: the most issues one call may hand out
CALLS_PER_HOUR = 6                    # epic_run: calls per token per hour
EPIC_ROW = "epic_work"

sleep = time.sleep                    # tests patch it (and nodes_route.clock) to run a clock of their own

agent_limiter = nodes._Limiter(CALLS_PER_HOUR, 3600.0, 64)

# the reasons of a refused work call, by who they are about: the NODE (leave it alone for a while and try another), the ISSUE (hold it and show why), or the move on case (taken, lost)
NODE_WHY = {"lanes_full", "writes_off", "authors_unknown", "login_not_allowed", "no_login", "no_access", "unreadable", "busy", "too_many_claims", "rate_limited", "launch_failed",
            "repo_not_here", "no_gh", "claim_unknown"}
MOVE_ON = {"taken", "lost"}
STILL_OPEN = {"running", "pending", "unclear", "depends", "claim_unknown", "authors_unknown"}      # an open issue with one of these reasons keeps the run going (the others need a person)
WITHDRAWN = {"mcp_run_off": "A person turned off 'Let MCP run this epic', so the run stopped.", "mcp_off": "This board's MCP access for agents was turned off, so the run stopped.",
             "mcp_level": "This board's MCP level no longer lets agents start tasks, so the run stopped.", "writes_off": "GitHub writes were turned off, so the run stopped.",
             "token_revoked": "The device token that started the run was revoked or expired, so the run stopped."}
NODE_REASON = {"needs_repair", "unpaired", "offline", "not_read_yet", "scope", "legacy", "unknown_node", "unreachable", "peer_error", "redirect", "bad_address", "self", "repo_not_here",
              "bad_answer", "wrong_node"}


class RunRefused(Exception):
    """A run that was not started or a call that was not allowed: `code` is a stable word, `text` a plain sentence, `status` the HTTP code."""

    def __init__(self, code: str, text: str, status: int = 409, **extra):
        super().__init__(text)
        self.code, self.text, self.status, self.extra = code, text, status, extra


def now() -> float:
    return nodes_route.clock()


def iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def key_of(owner: str, repo: str, n) -> str:
    return f"{owner.lower()}/{repo.lower()}#{int(n)}"


# ---------------------------------------------------------------- the run rows (kv `epic-run:<owner/repo#n>`, the active ones listed in `epic-run-index`)

def _value(db, key: str):
    try:
        rec = db.kv_get(key)
    except Exception:
        return None
    return rec.get("value") if isinstance(rec, dict) else None


def active_keys(db) -> list[str]:
    v = _value(db, INDEX)
    return [k for k in v if isinstance(k, str)][:50] if isinstance(v, list) else []


def load(db, key: str) -> dict | None:
    """The run row of an epic, re-validated field by field (a hand-edited row cannot put a wrong type in an answer)."""
    v = _value(db, KV + key)
    if not isinstance(v, dict) or not isinstance(v.get("owner"), str) or not isinstance(v.get("repo"), str):
        return None
    n, left = v.get("n"), v.get("left")
    if isinstance(n, bool) or not isinstance(n, int) or not github.valid_slug(v["owner"], v["repo"]) or not github.valid_number(n):
        return None
    assigned = {}
    for k, a in (v.get("assigned") if isinstance(v.get("assigned"), dict) else {}).items():
        if isinstance(k, str) and k.isdigit() and isinstance(a, dict) and len(assigned) < ASSIGNED_MAX:
            assigned[k] = {"handle": a.get("handle") if isinstance(a.get("handle"), str) else None, "node": str(a.get("node") or "")[:60], "why": str(a.get("why") or "")[:300],
                           "at": a.get("at") if isinstance(a.get("at"), str) else None, "task_ref": a.get("task_ref") if isinstance(a.get("task_ref"), str) else None,
                           "reason": a.get("reason") if isinstance(a.get("reason"), str) else None}
    return {"key": key, "owner": v["owner"], "repo": v["repo"], "n": n, "active": v.get("active") is True, "by": str(v.get("by") or "")[:80], "via": "mcp" if v.get("via") == "mcp" else "page",
            "left": left if isinstance(left, int) and not isinstance(left, bool) and left >= 0 else None, "started_at": v.get("started_at") if isinstance(v.get("started_at"), str) else None,
            "ended": v.get("ended") if isinstance(v.get("ended"), str) else None, "ended_at": v.get("ended_at") if isinstance(v.get("ended_at"), str) else None, "assigned": assigned}


def save(db, rec: dict) -> None:
    """Write the row and keep the index in step: only an active run is in it, so the loop reads one small list."""
    while len(rec["assigned"]) > ASSIGNED_MAX:
        rec["assigned"].pop(next(iter(rec["assigned"])))
    db.kv_set(KV + rec["key"], {k: rec[k] for k in ("owner", "repo", "n", "active", "by", "via", "left", "started_at", "ended", "ended_at", "assigned")})
    idx = [k for k in active_keys(db) if k != rec["key"]]
    if rec["active"]:
        idx.append(rec["key"])
    if idx:
        db.kv_set(INDEX, idx)
    elif _value(db, INDEX) is not None:
        db.kv_del(INDEX)


# ---------------------------------------------------------------- the runner

class Runner:
    """The run loop of one board. `hub_fn()` gives the NodeHub, `repos_fn()` the GitHub repositories of this board's own projects (what the epic routes may ask about)."""

    def __init__(self, db, *, hub_fn, repos_fn):
        self.db, self._hub_fn, self._repos_fn = db, hub_fn, repos_fn
        self.reservations = nodes_route.Reservations()
        self._lock = threading.RLock()            # the thread's state
        self._cycle = threading.RLock()           # one cycle at a time (the loop's and an agent's first one)
        self._call = threading.Lock()             # one work call at a time
        self._last_end: float | None = None       # when the last work call ended
        self._not_before = 0.0                    # a rate limit said: no call before this
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.mem: dict[str, dict] = {}            # key -> what the last cycle saw (waiting, held, assigned now, error)
        self.last_assigned: dict[str, float] = {}  # handle -> when this board last sent it an issue (the tie-break)
        self.recent: dict[tuple, float] = {}      # (key, issue) -> when a call for it was last made
        self.unclear: dict[tuple, float] = {}     # (key, issue) -> when a call whose outcome is not known was made
        self.held: dict[tuple, dict] = {}         # (key, issue) -> {until, code, text}
        self.blocked: dict[str, dict] = {}        # handle -> {until, text, code}

    # ---- what is allowed
    def _hub(self):
        return self._hub_fn()

    def records(self) -> list[dict]:
        try:
            return [r for r in self._hub().records() if isinstance(r, dict) and not r.get("legacy")]
        except Exception as e:
            log.debug("hub records could not be read: %s", e.__class__.__name__)
            return []

    def can_run(self) -> tuple[bool, str | None, str | None]:
        """(ok, code, sentence): Run needs GitHub writes on (the work route refuses without them) and at least one paired node."""
        if not github_write.writes_enabled():
            return False, "writes_off", "GitHub writes are off on this board (CCBOARD_EPIC_WRITES), so nothing can be sent to a node. Set CCBOARD_EPIC_WRITES=1 on the box to allow it."
        try:
            rows = [r for r in self._hub().rows() if not r.get("legacy")]
        except Exception:
            rows = []
        if not rows:
            return False, "no_nodes", "No node is paired with this board, so there is nowhere to send the work."
        return True, None, None

    def cycle_s(self) -> float:
        return max(CYCLE_MIN_S, float(getattr(settings, "nodes_poll", 20.0) or 20.0))

    # ---- the thread
    def ensure(self) -> bool:
        """Start the loop thread when a run is active and Run is allowed. True when it started. Nothing else ever starts it."""
        with self._lock:
            if self._stop.is_set() or (self._thread is not None and self._thread.is_alive()):
                return False
            if not active_keys(self.db) or not self.can_run()[0]:
                return False
            self._thread = threading.Thread(target=self._run, name="epic-run", daemon=True)
            self._thread.start()
            return True

    def running(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive())

    def resume(self) -> bool:
        """At start-up: carry on with the runs that were active when the board stopped. Reads one key; starts nothing on a board that has none."""
        self._check_withdrawn(active_keys(self.db))                # a run whose permission was taken back while the board was down does not start again
        return self.ensure()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=timeout)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    more = self.tick()
                except Exception as e:                                  # a cycle never ends the thread, and never reaches the hub's poller
                    log.warning("epic run cycle failed: %s", e.__class__.__name__)
                    more = True
                if not more:
                    with self._lock:
                        if not (active_keys(self.db) and self.can_run()[0]):
                            self._thread = None
                            return
                self._wake.wait(self.cycle_s())
                self._wake.clear()
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    # ---- one cycle
    def tick(self, only: str | None = None, deadline: float | None = None, wait: float | None = None) -> bool:
        """One cycle over the active runs (or just `only`). True when a run is still active afterwards. `wait` is how long to wait for a cycle that is already going (None: as long as it takes);
        when it does not end in time nothing is done here and the loop thread, which was woken, carries on."""
        if not self._cycle.acquire(timeout=-1 if wait is None else wait):
            return True
        try:
            self._prune()
            keys = active_keys(self.db)
            if only is not None:
                keys = [k for k in keys if k == only]
            keys = self._check_withdrawn(keys)
            if not keys:
                return bool(active_keys(self.db))
            ok, code, text = self.can_run()
            if not ok:
                for k in keys:
                    self.mem[k] = {"cycle_at": iso(now()), "blocked": {"code": code, "text": text}, "waiting": {}, "held": {}, "assigned_now": [], "error": None}
                return False
            records = self.records()
            self.reservations.release_seen(records)
            for k in keys:
                if self._stop.is_set():
                    break
                try:
                    self._epic_cycle(k, records, deadline)
                except Exception as e:
                    log.warning("epic run cycle for one epic failed: %s", e.__class__.__name__)
                    self.mem.setdefault(k, {"waiting": {}, "held": {}, "assigned_now": []})["error"] = "The last cycle failed: " + e.__class__.__name__
            return bool(active_keys(self.db))
        finally:
            self._cycle.release()

    def _prune(self) -> None:
        """Forget what is old: the cooldowns, holds and blocks are minutes long, so nothing older than an hour is worth its memory."""
        t = now()
        for table, age in ((self.recent, 3600.0), (self.unclear, 3600.0)):
            for k in [k for k, v in table.items() if t - v > age]:
                table.pop(k, None)
        for table in (self.held, self.blocked):
            for k in [k for k, v in table.items() if float(v.get("until") or 0) < t - 3600.0]:
                table.pop(k, None)
        for k in [k for k in self.mem if k not in set(active_keys(self.db)) and t - (nodes_route._epoch((self.mem[k] or {}).get("cycle_at")) or 0) > 3600.0]:
            self.mem.pop(k, None)

    def _end(self, rec: dict, how: str) -> None:
        rec["active"], rec["ended"], rec["ended_at"] = False, how, iso(now())
        save(self.db, rec)

    def _withdrawn(self, rec: dict) -> str | None:
        """For a run an agent started (`via` mcp): the fixed word of the permission a person has taken back since, else None. Any other run has none to lose."""
        if rec["via"] != "mcp":
            return None
        if not nodes_epics.load_settings(self.db, rec["owner"], rec["repo"], rec["n"])["mcp_run"]:
            return "mcp_run_off"
        level = nodes_relay.mcp_level()
        if not level:
            return "mcp_off"
        if "tasks" not in nodes_relay.MCP_LEVELS[level]:
            return "mcp_level"
        if not github_write.writes_enabled():
            return "writes_off"
        if rec["by"].startswith("mcp:") and not any(t["name"] == rec["by"][4:] and not t["expired"] for t in mcp_tokens.listing(self.db)):
            return "token_revoked"
        return None

    def _withdraw(self, rec: dict, word: str) -> None:
        """End an agent's run whose permission was taken back, say why in the audit and on the run's view, and send nothing."""
        self._end(rec, "stopped")
        self.mem[rec["key"]] = {"cycle_at": iso(now()), "blocked": None, "waiting": {}, "held": {}, "assigned_now": [], "error": WITHDRAWN[word]}
        nodes.audit("out", "", "epic_run", False, f"stopped: {word}", user=rec["by"], target=f"run {rec['owner']}/{rec['repo']}#{rec['n']}", status="stopped", db=self.db)

    def _check_withdrawn(self, keys: list[str]) -> list[str]:
        """`keys` without the agent runs that were just ended because a person took their permission back."""
        left = []
        for k in keys:
            rec = load(self.db, k)
            word = self._withdrawn(rec) if rec is not None and rec["active"] else None
            if word:
                self._withdraw(rec, word)
            else:
                left.append(k)
        return left

    def _epic_cycle(self, key: str, records: list[dict], deadline: float | None) -> None:
        rec = load(self.db, key)
        if rec is None or not rec["active"]:
            return
        owner, repo, n = rec["owner"], rec["repo"], rec["n"]
        st = nodes_epics.load_settings(self.db, owner, repo, n)
        mem = {"cycle_at": iso(now()), "blocked": None, "waiting": {}, "held": {}, "assigned_now": [], "error": None, "paused": st["paused"]}
        self.mem[key] = mem
        if st["paused"]:
            return
        try:
            d = nodes_epics.detail(self.db, owner, repo, n, self._hub().records(), repos=self._repos_fn())
        except projects.NotFound:
            mem["error"] = "This board no longer has a project for that repository."
            self._end(rec, "stopped")
            return
        if d["error"]:
            mem["error"] = d["error"].get("message") or "GitHub could not be read just now."
            if d["error"].get("reason") == "not_found" and not d["children"]:        # an issue that is not there (or that the gh login cannot see) is not worth asking about every cycle
                self._end(rec, "stopped")
                return
        ep = d["epic"]
        if d["children"] and ep.get("state") == "closed":
            self._end(rec, "closed")
            return
        if ep.get("labelled") is False:
            mem["error"] = "The issue is not labelled ccboard:epic any more."
            self._end(rec, "stopped")
            return
        t = now()
        for h in [h for h, b in self.blocked.items() if float(b.get("until") or 0) <= t]:
            self.blocked.pop(h, None)
        known = {f"{c['repo']}#{c['number']}".lower() for c in d["children"]}
        reads = {"left": DEP_READS, "memo": {}}
        own = f"{owner}/{repo}".lower()
        ready, remaining = [], 0
        for c in sorted(d["children"], key=lambda x: x["order"]):
            if c["state"] != "open":
                continue
            why = self._not_ready(key, c, d, known, reads)
            if why is None:
                ready.append(c)
                remaining += 1
                continue
            if why["code"] not in ("running", "pending"):
                mem["held"][str(c["number"])] = {"code": why["code"], "text": why["text"], "until": iso(why.get("until"))}
            if why["code"] in STILL_OPEN:
                remaining += 1                                        # someone is on it or it clears by itself: the run keeps watching (a lapsed lease frees it again)
        mem["ready"] = [c["number"] for c in ready]
        for c in ready:
            if self._stop.is_set() or (deadline is not None and now() >= deadline):
                break
            st = nodes_epics.load_settings(self.db, owner, repo, n)       # Pause is read again before every call
            if st["paused"]:
                mem["paused"] = True
                break
            rec = load(self.db, key) or rec
            if not rec["active"]:
                break
            if rec["left"] is not None and rec["left"] <= 0:
                break
            res = self.pick_for(c, st, own)
            if res["handle"] is None:
                mem["waiting"][str(c["number"])] = {**res["waiting"], "refused": [{k: r[k] for k in ("handle", "name", "code", "text")} for r in res["refused"]]}
                continue
            out = self.dispatch(rec, c, res, f"{owner}/{repo}#{n}")
            if out["outcome"] == "assigned":
                mem["assigned_now"].append({"issue": c["number"], "node": res["handle"], "name": res["name"], "why": res["why"]})
                rec = load(self.db, key) or rec
            elif out["outcome"] == "abort":
                break
            elif out["outcome"] in ("held", "unclear"):
                mem["held"][str(c["number"])] = {"code": out["code"], "text": out["text"], "until": iso(out.get("until"))}
        rec = load(self.db, key) or rec
        if rec["active"]:
            if rec["left"] is not None and rec["left"] <= 0:
                self._end(rec, "limit")
            elif not mem["error"] and not remaining and not mem["waiting"]:
                self._end(rec, "finished")

    # ---- which issues are ready, and the pick for one
    def _not_ready(self, key: str, c: dict, d: dict, known: set[str], reads: dict) -> dict | None:
        """None when child `c` (open) may be sent now, else {code, text}: `running` / `pending` mean someone is on it; every other code is shown to the person."""
        n = c["number"]
        bucket = (c.get("bucket") or {})
        if c["claim_state"] == "unknown":
            return {"code": "claim_unknown", "text": (c.get("claim_unknown") or {}).get("text") or "Its claim could not be read, so it is not treated as free."}
        if c.get("claim") is not None or c["claim_state"] != "none":
            if bucket.get("state") in ("running", "queued"):
                return {"code": "running", "text": f"held by {bucket.get('node') or 'a node'}"}
            if bucket.get("state") == "failed":
                return {"code": "failed", "text": f"failed on {bucket.get('node') or 'a node'}: it needs a person"}
            if bucket.get("state") == "done":
                return {"code": "done_open", "text": f"done by {bucket.get('node') or 'a node'}, but the issue is still open"}
            return {"code": "claim_old", "text": "it has an earlier claim: run it by hand"}
        allowed = d["authors"]["allowed"]
        if not allowed:
            return {"code": "authors_unknown", "text": "Who may claim is not known (gh login unknown), so nothing is sent."}
        if c.get("author_ok") is False:
            return {"code": "author", "text": f"not by an allowed author ({c.get('author') or 'unknown'})"}
        if d["authors"].get("epic_ok") is False:
            return {"code": "author", "text": "the epic is not by an allowed author"}
        if c.get("dependency_cycle"):
            return {"code": "cycle", "text": "its dependencies go in a circle"}
        if c.get("blocked_by"):
            return {"code": "depends", "text": "waits for " + ", ".join(c["blocked_by"])}
        for dep in c.get("depends_on") or []:
            ref = f"{c['repo']}#{dep[1:]}".lower() if dep.startswith("#") else dep.lower()
            if ref in known or dep in (c.get("blocked_by") or []):
                continue                                              # a child that is not in blocked_by is closed
            state = self._dep_state(ref, reads)
            if state is not True:
                return {"code": "depends", "text": f"waits for {dep}" + ("" if state is False else ", which could not be checked")}
        h = self.held.get((key, n))
        if h and h["until"] > now():
            return {"code": h["code"], "text": h["text"], "until": h["until"]}
        t = now()
        if t - self.recent.get((key, n), -1e9) < nodes_route.RESERVE_CYCLES * self.cycle_s():
            return {"code": "pending", "text": "just sent"}
        u = self.unclear.get((key, n))
        if u is not None and t - u < UNCLEAR_S:
            return {"code": "unclear", "text": "a request for it was not confirmed, so it is not sent again until its claim shows or ten minutes pass", "until": u + UNCLEAR_S}
        return None

    def _dep_state(self, ref: str, reads: dict):
        """True when a dependency outside the epic is closed, False when it is open, None when it cannot be known (another repository than this board's own, no read left, unreadable)."""
        if ref in reads["memo"]:
            return reads["memo"][ref]
        slug, _, num = ref.partition("#")
        owner, _, name = slug.partition("/")
        out = None
        if github.valid_slug(owner, name) and github.valid_number(num) and slug in {s.lower() for s in self._repos_fn()} and reads["left"] > 0:
            reads["left"] -= 1
            try:
                data = github.reader.get(github.issue_path(owner, name, int(num)), nodes_epics.ISSUE_TTL).data
                if isinstance(data, dict) and data.get("number") == int(num) and "pull_request" not in data:
                    out = data.get("state") == "closed"
            except github.GhReadError:
                out = None
        reads["memo"][ref] = out
        return out

    def pick_for(self, c: dict, st: dict, own: str) -> dict:
        """The routing answer for one child under the epic's settings: the pins in their order (the page's, the label's, the epic's), the epic's limit and groups, this board's reservations."""
        lp = c.get("label_pin")
        label = None if not lp else (nodes_route.UNKNOWN_PIN if lp.get("stale") or not lp.get("handle") else lp["handle"])
        ui = st["pins"].get(str(c["number"])) if c["repo"].lower() == own else None
        return self.pick(c, {"ui": ui, "label": label, "epic": st["epic_pin"]}, st)

    def pick(self, c: dict, pins: dict, st: dict) -> dict:
        t = now()
        cfg = {"max_window_pct": nodes_epics.max_window_pct(st), "group": st["group"], "assigned": dict(self.last_assigned), "blocked": dict(self.blocked), "now": t}
        return nodes_route.pick({"repo": c["repo"], "number": c["number"], "agent": c.get("agent") or "claude"}, self.records(), pins, self.reservations.items(t), cfg)

    # ---- sending one issue
    def _acting(self, rec: dict) -> str:
        """The login the relay sees. A person's own login for a run a person started; for a run an agent started, a label that names the token (never `mcp:` and never `local-token`, which the relay
        treats as an agent, and the epic_work row is a person's)."""
        by = rec["by"]
        return f"run for {by}"[:60] if rec["via"] == "mcp" or by == "local-token" or by.startswith(("mcp:", "node:")) else by

    def _gap(self) -> None:
        """Wait until a second has passed since the last work call ended (and any rate-limit wait)."""
        while not self._stop.is_set():                                 # a board that is stopping does not wait out a rate limit that can last an hour
            t = now()
            wait = max(self._not_before - t, (self._last_end + CALL_GAP_S - t) if self._last_end is not None else 0.0)
            if wait <= 0:
                return
            sleep(min(wait, CALL_GAP_S))

    def dispatch(self, rec: dict, c: dict, res: dict, epic_ref: str) -> dict:
        """Send child `c` to the node `res` chose through the relay row epic_work. Returns {outcome: assigned | held | unclear | abort | refused, code, text, until, data, error}. One call at a time."""
        handle, key, n = res["handle"], rec["key"], c["number"]
        with self._call:
            self._gap()
            if self._stop.is_set() or nodes_epics.load_settings(self.db, rec["owner"], rec["repo"], rec["n"])["paused"] and rec["via"] != "manual":
                return {"outcome": "abort", "code": "paused", "text": "paused"}
            live = (load(self.db, key) or rec) if rec["via"] == "mcp" else rec         # a person may have taken the run over, or taken the agent's permission back, during the wait
            word = self._withdrawn(live)
            if word:
                self._withdraw(live, word)
                return {"outcome": "abort", "code": word, "text": WITHDRAWN[word]}
            if not github_write.writes_enabled():                                      # a person's run waits for the writes to come back; it sends nothing meanwhile
                return {"outcome": "abort", "code": "writes_off", "text": "GitHub writes are off"}
            t = now()
            recs = {r.get("handle"): r for r in self.records()}
            ok = nodes_route._epoch((recs.get(handle) or {}).get("last_ok_at"))
            item = self.reservations.add(handle, t + nodes_route.RESERVE_CYCLES * self.cycle_s(), repo=c["repo"], issue=n, ok=ok, at=t)
            self.recent[(key, n)] = t
            body = {"repo": c["repo"], "issue": n, "epic": epic_ref}
            standin = SimpleNamespace(state=SimpleNamespace(user=self._acting(rec)), headers={"x-ccboard": "1"})
            try:
                out = nodes_relay.relay(handle, nodes_relay.BY_NAME[EPIC_ROW], {}, body, standin, db=self.db, hub=self._hub())
            except nodes_relay.RelayError as e:
                self._last_end = now()
                return self._refused(rec, c, res, item, e)
            except Exception as e:                                    # the call may have gone out: the lane stays reserved and the issue is not sent again soon
                self._last_end = now()
                log.warning("epic run: a work call failed in an unexpected way: %s", e.__class__.__name__)
                self.unclear[(key, n)] = now()
                return {"outcome": "unclear", "code": "unclear", "text": "the request failed in a way that does not say whether the node started it", "until": now() + UNCLEAR_S}
            self._last_end = now()
        return self._assigned(rec, c, res, out)

    def _assigned(self, rec: dict, c: dict, res: dict, out: dict) -> dict:
        data = out.get("data") if isinstance(out, dict) and isinstance(out.get("data"), dict) else {}
        t = now()
        self.last_assigned[res["handle"]] = t
        cur = load(self.db, rec["key"]) or {**rec, "assigned": dict(rec["assigned"])}
        cur["assigned"][str(c["number"])] = {"handle": res["handle"], "node": res["name"], "why": res["why"], "at": iso(t), "task_ref": data.get("task_ref"), "reason": data.get("reason")}
        if cur["left"] is not None and rec["via"] != "manual":                 # one issue a person sent by hand does not spend the issues an agent was allowed
            cur["left"] = max(0, cur["left"] - 1)
        save(self.db, cur)
        rec["assigned"], rec["left"] = cur["assigned"], cur["left"]
        peer = next((r.get("peer_id") for r in self.records() if r.get("handle") == res["handle"]), "") or ""
        nodes.audit("out", peer, "epic_route", True, res["why"], node_name=res["name"], user=rec["by"], target=f"route {c['repo']}#{c['number']}", status="ok", db=self.db)
        return {"outcome": "assigned", "code": "assigned", "text": res["why"], "data": data}

    def _refused(self, rec: dict, c: dict, res: dict, item: dict, e: "nodes_relay.RelayError") -> dict:
        """What a refused or failed call means for the next one. Returns the outcome (see dispatch)."""
        handle, key, n = res["handle"], rec["key"], c["number"]
        why = e.extra.get("why") if isinstance(e.extra, dict) else None
        t = now()
        until = t + BLOCK_CYCLES * self.cycle_s()
        text = f"{res['name']}: {e.message}"[:240]
        if e.reason == "rate_limited":
            self.reservations.drop(item)
            wait = e.headers.get("Retry-After") if isinstance(e.headers, dict) else None
            wait = float(wait) if isinstance(wait, str) and wait.isdigit() else 5.0
            if e.kind == "failed":                                    # the NODE said wait: only that node is left alone until then, the others go on
                self.blocked[handle] = {"until": t + wait, "text": text, "code": "rate_limited"}
                return {"outcome": "refused", "code": "rate_limited", "text": text, "until": t + wait}
            self._not_before = t + wait                               # this board's own relay limit (a few seconds, the same for every node): nobody is asked until it is over
            return {"outcome": "abort", "code": "rate_limited", "text": text}
        if e.reason == "unconfirmed":                                 # the node may have started it: keep the lane reserved and do not repeat the call
            self.unclear[(key, n)] = t
            return {"outcome": "unclear", "code": "unclear", "text": text, "until": t + UNCLEAR_S}
        self.reservations.drop(item)
        if why in MOVE_ON:
            self.held[(key, n)] = {"until": until, "code": why, "text": text}
            return {"outcome": "held", "code": why, "text": text, "until": until}
        if why == "lanes_full":                                       # the node's own count says full: believe it until its next readings
            self.reservations.add(handle, until, n=nodes_route.NO_LIMIT, kind="full")
        if why in NODE_WHY or e.reason in NODE_REASON or e.status >= 500:
            self.blocked[handle] = {"until": until, "text": text, "code": why or e.reason}
            return {"outcome": "refused", "code": why or e.reason, "text": text, "until": until}
        self.held[(key, n)] = {"until": t + HELD_S, "code": why or e.reason, "text": text}                 # the issue is what was refused (author, bypass, attempts, closed ...)
        return {"outcome": "held", "code": why or e.reason, "text": text, "until": t + HELD_S}

    # ---- starting and the views
    def start(self, owner: str, repo: str, n: int, by: str, via: str = "page", max_issues: int | None = None, unpause: bool = False) -> dict:
        """Mark the epic's run active (Run on the page, or an agent's epic_run) and start the loop. Raises RunRefused when Run is not allowed. Writes nothing on a board with no paired node."""
        ok, code, text = self.can_run()
        if not ok:
            raise RunRefused(code, text)
        key = key_of(owner, repo, n)
        rec = load(self.db, key) or {"key": key, "owner": owner, "repo": repo, "n": int(n), "active": False, "by": by, "via": via, "left": None, "started_at": None, "ended": None, "ended_at": None, "assigned": {}}
        if unpause:
            nodes_epics.save_settings(self.db, owner, repo, n, {"paused": False})
        if rec["active"]:
            if via == "page":                                             # a person's Run takes the run over: it is theirs now, and not limited to the issues an agent was allowed
                rec.update(by=by, via="page", left=None)
            elif rec["left"] is not None and max_issues is not None:
                rec["left"] = min(ISSUES_PER_CALL * 5, rec["left"] + max_issues)
        else:
            rec.update(active=True, by=by, via=via, left=max_issues, started_at=iso(now()), ended=None, ended_at=None)
        save(self.db, rec)
        self.ensure()
        self.wake()
        return self.view(owner, repo, n)

    def view(self, owner: str, repo: str, n: int) -> dict:
        key = key_of(owner, repo, n)
        rec = load(self.db, key)
        st = nodes_epics.load_settings(self.db, owner, repo, n)
        ok, code, text = self.can_run()
        mem = self.mem.get(key) or {}
        return {"can_run": ok, "blocked": None if ok else {"code": code, "text": text}, "running": bool(rec and rec["active"]), "paused": st["paused"], "by": rec["by"] if rec else None,
                "via": rec["via"] if rec else None, "left": rec["left"] if rec else None, "started_at": rec["started_at"] if rec else None, "ended": rec["ended"] if rec else None,
                "ended_at": rec["ended_at"] if rec else None, "cycle_at": mem.get("cycle_at"), "assigned": rec["assigned"] if rec else {}, "waiting": mem.get("waiting") or {},
                "held": mem.get("held") or {}, "error": mem.get("error"), "loop": self.running(), "max_window_pct": nodes_epics.max_window_pct(st), "mcp_run": st["mcp_run"]}

    def annotate(self, out: dict, owner: str, repo: str, n: int) -> dict:
        """Add `route` to an epic answer (the run view) and `route` to each child: {state: assigned | waiting | held | running | null, text, node?, until?}."""
        v = self.view(owner, repo, n)
        out["route"] = v
        for c in out.get("children") or []:
            k = str(c["number"])
            r = None
            if k in v["waiting"] and c.get("state") == "open":
                w = v["waiting"][k]
                r = {"state": "waiting", "text": w.get("text"), "until": w.get("until_iso"), "code": w.get("code")}
            elif k in v["held"] and c.get("state") == "open":
                h = v["held"][k]
                r = {"state": "held", "text": h.get("text"), "until": h.get("until"), "code": h.get("code")}
            elif k in v["assigned"]:
                a = v["assigned"][k]
                r = {"state": "assigned", "text": a["why"], "node": a["node"], "handle": a["handle"], "until": None, "code": None, "at": a["at"]}
            c["route"] = r
        return out

    def preview(self, issue: dict, pins: dict | None = None, st: dict | None = None) -> dict:
        """The routing answer for one issue without sending anything (the launcher's Auto): a dry run of pick."""
        st = st or {**nodes_epics.DEFAULTS, **nodes_epics.ROUTING_DEFAULTS, "pins": {}, "group": []}
        return self.pick(issue, pins or {}, st)

    def run_issue(self, owner: str, repo: str, n: int, issue: int, node: str, by: str) -> dict:
        """Run ONE issue of an epic on `node` (a handle) or on the node routing picks (`auto`), for a person who asked. The same filters apply to a chosen node (it is a pin of this one call). Returns
        {ok, handle, name, why, data} when it was sent, or {ok: False, waiting, why, refused} when no node can take it now; raises RunRefused when it cannot be tried, RelayError from the call."""
        ok, code, text = self.can_run()
        if not ok:
            raise RunRefused(code, text)
        if node != "auto" and not nodes.valid_handle(node):
            raise RunRefused("bad_node", "node is auto or the handle of a paired node", 400)
        records = self._hub().records()
        d = nodes_epics.detail(self.db, owner, repo, n, records, repos=self._repos_fn())
        c = next((x for x in d["children"] if x["number"] == issue and x["repo"].lower() == f"{owner}/{repo}".lower()), None)
        if c is None:
            raise RunRefused("not_found", "that issue is not one of the epic's issues this board has read", 404)
        key = key_of(owner, repo, n)
        if not self._cycle.acquire(timeout=30):
            raise RunRefused("busy", "The run is busy sending other issues. Try again in a moment.")
        try:
            known = {f"{x['repo']}#{x['number']}".lower() for x in d["children"]}
            why = self._not_ready(key, c, d, known, {"left": DEP_READS, "memo": {}})
            if why is not None and why["code"] not in ("pending",):
                raise RunRefused(why["code"], "That issue cannot be sent now: " + why["text"] + ".")
            st = nodes_epics.load_settings(self.db, owner, repo, n)
            own = f"{owner}/{repo}".lower()
            if node == "auto":
                res = self.pick_for(c, st, own)
            else:
                res = self.pick(c, {"ui": node}, st)
            if res["handle"] is None:
                return {"ok": False, "handle": None, "why": res["why"], "waiting": res["waiting"], "refused": res["refused"]}
            rec = load(self.db, key) or {"key": key, "owner": owner, "repo": repo, "n": int(n), "active": False, "by": by, "via": "manual", "left": None, "started_at": None, "ended": None,
                                         "ended_at": None, "assigned": {}}
            out = self.dispatch({**rec, "by": by, "via": "manual"}, c, res, f"{owner}/{repo}#{n}")
        finally:
            self._cycle.release()
        if out["outcome"] == "assigned":
            return {"ok": True, "handle": res["handle"], "name": res["name"], "why": res["why"], "data": out["data"]}
        raise RunRefused(out["code"], out["text"], 409, outcome=out["outcome"])


# ---------------------------------------------------------------- what an agent may do (issue #152)

def _caller(request) -> tuple[str, dict]:
    """(who, identity) of an agent behind `request`: `mcp:<token name>` or `local-token`. RunRefused for anyone else (a person uses the epic page's own routes)."""
    try:
        ident = nodes_relay.mcp_identity(request)
    except nodes_relay.RelayError as e:                                   # a name that only looks like a device token
        raise RunRefused("forbidden", e.message, 403) from None
    if ident is None:
        raise RunRefused("agents_only", "this route is for an agent's MCP tool; the epic page has its own routes for a person", 403)
    return ident["by"], ident


def agent_run(runner: Runner, request, owner: str, repo: str, n: int, max_issues=None) -> dict:
    """epic_run: the guard, in this order, then a first cycle on this request (a few seconds at most), then the loop goes on by itself.
    1 the board's MCP level allows tasks (CCBOARD_MCP_NODES=tasks)  2 the caller's own scope holds tasks  3 fewer than 6 calls this hour from this caller  4 the epic's own switch `mcp_run` is on
    (only a person sets it)  5 the epic is not paused  6 at most 10 issues  7 Run is allowed at all (writes on, a node paired). Every refusal is audited with who asked."""
    by, ident = _caller(request)
    target = f"run {owner}/{repo}#{n}"

    def refuse(code: str, text: str, status: int = 403, **extra):
        nodes.audit("out", "", "epic_run", False, f"{code}: {text}"[:200], user=by, target=target, status="refused", db=runner.db)
        raise RunRefused(code, text, status, **extra)
    level = nodes_relay.mcp_level()
    if not level:
        refuse("mcp_off", nodes_relay.MCP_OFF)
    if "tasks" not in nodes_relay.MCP_LEVELS[level]:
        refuse("mcp_level", nodes_relay.MCP_READ_ONLY)
    if "tasks" not in ident["scopes"]:
        refuse("mcp_scope", "this device token does not hold the tasks scope: mint one that does in Settings > Agents")
    allowed, wait = agent_limiter.allow(by, now=now())
    if not allowed:
        refuse("rate_limited", f"epic_run is limited to {CALLS_PER_HOUR} calls an hour for each device; try again in {wait} s", 429, retry_after=wait)
    if max_issues is None:
        max_issues = ISSUES_PER_CALL
    if isinstance(max_issues, bool) or not isinstance(max_issues, int) or not 1 <= max_issues <= ISSUES_PER_CALL:
        refuse("bad_request", f"max_issues is a whole number from 1 to {ISSUES_PER_CALL}", 400)
    st = nodes_epics.load_settings(runner.db, owner, repo, n)
    if not st["mcp_run"]:
        refuse("mcp_run_off", "the owner has not allowed agents to run this epic: turn on 'Let agents run this epic' on the epic page first")
    if st["paused"]:
        refuse("paused", "this epic is paused by a person: resume it on the epic page first", 409)
    ok, code, text = runner.can_run()
    if not ok:
        refuse(code, text, 409)
    key = key_of(owner, repo, n)
    runner.mem.pop(key, None)
    runner.start(owner, repo, n, by, via="mcp", max_issues=max_issues)
    runner.tick(only=key, deadline=now() + SYNC_BUDGET_S, wait=SYNC_BUDGET_S)
    v = runner.view(owner, repo, n)
    mem = runner.mem.get(key) or {}
    nodes.audit("out", "", "epic_run", True, f"started, {len(mem.get('assigned_now') or [])} sent, {len(v['waiting'])} waiting", user=by, target=target, status="ok", db=runner.db)
    return {"started": True, "max_issues": max_issues, "left": v["left"], "running": v["running"], "assigned": mem.get("assigned_now") or [],
            "waiting": [{"issue": int(k), "reason": w.get("reason"), "until": w.get("until_iso")} for k, w in sorted(v["waiting"].items(), key=lambda kv: int(kv[0]))],
            "held": [{"issue": int(k), "reason": h.get("text")} for k, h in sorted(v["held"].items(), key=lambda kv: int(kv[0]))], "error": v["error"]}


STATUS_CHILDREN_MAX = 100


def agent_status(runner: Runner, owner: str, repo: str, n: int, repos: list[str]) -> dict:
    """epic_status: the epic page's data trimmed to plain fields, for an agent. Read only. Anything that is not known is null (never 0, never free). GitHub's words are cut by
    github.clean_text already; the tool layer cleans them again for the agent."""
    d = nodes_epics.detail(runner.db, owner, repo, n, runner._hub().records(), repos=repos)
    runner.annotate(d, owner, repo, n)
    r = d["route"]
    ep = d["epic"]
    kids = []
    for c in d["children"][:STATUS_CHILDREN_MAX]:
        claim, task, pr = c.get("claim"), c.get("task"), c.get("pr")
        kids.append({"repo": c["repo"], "number": c["number"], "title": c["title"], "state": c["state"], "phase": c.get("phase"), "agent": c.get("agent"),
                     "node": (c.get("bucket") or {}).get("node"), "claim_state": c.get("claim_state"), "claim_status": claim["status"] if claim else None,
                     "claim_live": claim["live"] if claim else None, "task_ref": (task or {}).get("ref"), "pr": {"number": pr["number"], "state": pr["state"]} if pr else None,
                     "depends_on": list(c.get("depends_on") or []), "blocked_by": list(c.get("blocked_by") or []), "pin": (c.get("pin") or {}).get("handle"),
                     "route": None if not c.get("route") else {k: c["route"].get(k) for k in ("state", "text", "until", "node")}})
    prog = d["progress"]
    return {"repo": ep.get("repo"), "number": ep.get("number"), "title": ep.get("title"), "state": ep.get("state"), "source": d.get("source"),
            "error": (d.get("error") or {}).get("message") if d.get("error") else None, "truncated": bool(d.get("truncated")) or len(d["children"]) > STATUS_CHILDREN_MAX,
            "run": {"running": r["running"], "paused": r["paused"], "mcp_run": r["mcp_run"], "left": r["left"], "can_run": r["can_run"], "ended": r["ended"], "cycle_at": r["cycle_at"],
                    "waiting": len(r["waiting"]), "held": len(r["held"]), "assigned": len(r["assigned"])},
            "progress": {"total": prog["total"], "open": prog["open"], "closed": prog["closed"], "unknown": prog["unknown"], "unclaimed": prog["unclaimed"],
                         "by_node": {k: {s: v[s] for s in ("done", "running", "queued", "failed")} for k, v in prog["by_node"].items()}},
            "children": kids}
