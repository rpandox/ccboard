"""The task runtime (v0.5.14b): what happens to a task after it was dispatched.

A task is backlog -> queued -> running -> done | failed | cancelled. This module owns the transitions that nobody triggers by hand:

  - a turn ends (the Stop hook, through hooks.TURN_HOOKS): the active task of that session row, the newest `running` one bound to it
    whose own turn has come (queued prompts below), stores the turn's closing text as its `result`, goes `done`, hands the result to the chain steps queued behind it, and, when it
    asked to be closed, starts the auto-close countdown (flags.autoclose = {task, due}); StopFailure sends it to `failed` (a rate limit
    only parks it: the session continues after the reset, autoresume types `continue`) and never closes anything;
  - queued prompts (#64): a task handed to a WORKING Claude session (dispatch with queue) runs as a second turn with a Stop of its own,
    but its UserPromptSubmit fires when the text is queued and the first Stop carries its prompt_id, so only the Stop count can say which
    Stop is whose. The dispatch stores spec.turns_ahead (the Stops that come first: 1 for the turn in flight, one more per task already
    queued ahead); every Stop of the row skips one: a task with turns_ahead above zero is not credited, gets no countdown, and its card
    reads Queued; the Stop that brings the counter to zero is the one before its own turn, the next one is its result;
  - auto-close: CCBOARD_AUTOCLOSE_GRACE seconds after the Stop (45), every guard re-checked at the deadline (same row, state done with
    the same state_at, no permission pending, no subagent, not compacting, nobody attached, claude-mem not busy), then the agent's own
    exit command, 8 s of patience (during which Claude's "uncommitted file: keep or remove the worktree" dialog is answered with Keep, so the
    agent really exits and SessionEnd is recorded), then main._end_session(name, 'auto_close'). The idle-prompt Notification about a
    minute after the Stop does not cancel a close that is waiting for a client or for claude-mem. A final message whose last line asks a question never
    closes: flags.autoclose = {task, held: 'question'} and the card shows 'needs you'. A new prompt, a permission or a dialog, a subagent,
    a tool batch, a compaction or an interrupt cancels the countdown. Pending closes live in memory and are dropped on restart;
  - chains: a queued step waits for `parent_id` (the plan's after_id); once the parent is done its result goes into the step's prompt
    (`{{result}}`, else appended) and the step starts in a lane of its own; a failed or cancelled parent cancels its children ("blocked:
    step failed"). Steps are held while their own agent's window is at 85 % (Claude: the 5 h or 7 d window or a limit episode; Codex: its usage window) (limit_gate), never a
    hand dispatch;
  - the sweep: a running task whose session row has ended without a result is cancelled.

Time is injected (`clock`) and nothing sleeps: every wait is a deadline that run_due(now) acts on, which the 2 s ticker thread calls
and the tests drive by hand. The session callables are injected like recover.run's: start_session(task, prompt=, auto_close=) starts a
queued task in a new lane session (main._taskflow_start); end_session(name, reason) kills it (main._end_session)."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

from . import agents, scheduler
from .config import settings
from .db import now as db_now

log = logging.getLogger("ccboard.taskflow")

TICK = 2.0                      # seconds between run_due calls of the ticker thread
SWEEP_EVERY = 10.0              # seconds between sweeps of running tasks whose session row ended
EXIT_WAIT = 8.0                 # seconds the agent gets to quit after its exit command before the session is killed
EXIT_POLL = 2.0                 # seconds between looks at the pane while the agent quits (its worktree dialog must be answered)
EXIT_DIALOG_MAX = 2             # answers the exit dialog gets per close (a stuck pane is killed, not fed Enter for ever)
RECHECK = 15.0                  # seconds between re-checks while a guard defers the close (a person attached, a subagent, a compaction)
GIVE_UP = 600.0                 # after this long of deferring, the session is kept and the close is dropped
MEM_RECHECK = 5.0               # seconds between looks at claude-mem's queue
SETTLE = 3.0                    # seconds after a Stop before the next held queued prompt is typed (Claude takes the queued turn within a second)
MEM_WAITING = "waiting for claude-mem"    # the reason on the card while a busy claude-mem defers the close
MEM_WAIT = 60.0                 # how long a busy claude-mem is waited for before the session is closed anyway
RETRY_START = 30.0              # seconds before a chain step whose launch hit a tmux hiccup is tried again
ENDED_GRACE = 30.0              # seconds a session may sit in state 'ended' before its running task counts as cancelled
RESULT_MAX = 20000              # tasks.result (the same cap as db.RESULT_MAX)
CHAIN_RESULT_MAX = 12000        # characters of a parent's result that go into the next step's prompt
PROMPT_MAX = 20000              # a task's prompt (main.TASK_PROMPT_MAX)
LIMIT_PCT = 85.0                # the scheduler's QUOTA_MAX_PCT: a window at or above this holds auto-dispatch
PLACEHOLDER = "{{result}}"
ACTIVITY_EVENTS = frozenset({"UserPromptSubmit", "PermissionRequest", "SubagentStart", "PostToolBatch", "Interrupt", "PreCompact"})
DIALOG_WAITS = ("permission", "elicitation")      # the wait kinds of a Notification that cancel a pending close (idle does not)
BLOCKED = "blocked: step failed"
EXIT_DIALOG_KEEP = "keep worktree"       # Claude's /exit dialog for a worktree with changes: "1. Keep worktree" and "2. Remove worktree"
EXIT_DIALOG_REMOVE = "remove worktree"
TRANSCRIPT_TAIL = 256 * 1024


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _epoch(v) -> float:
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v:
        try:
            return float(v)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ends_with_question(text: str | None) -> bool:
    """Does the last non-empty line of a final message end with a question mark (closing markdown or quotes after it are ignored)?
    Such a message waits for an answer: the session is never auto-closed on it."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line:
            return line.rstrip(" \t*_`\"'’”)]>").endswith("?")
    return False


def _spec_of(t: dict | None) -> dict:
    try:
        spec = json.loads(t["spec"]) if t and t.get("spec") else {}
    except (ValueError, TypeError):
        spec = {}
    return spec if isinstance(spec, dict) else {}


def turns_ahead(t: dict | None) -> int:
    """How many Stops of the task's session come before the one that ends this task's own turn (spec.turns_ahead; 0 = its turn is the
    one in flight, or none was queued)."""
    n = _spec_of(t).get("turns_ahead")
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0


def set_turns_ahead(db, tid: int, n: int) -> None:
    """Store (n > 0) or clear (n <= 0) the task's Stop counter in its spec, leaving the rest of the spec as it was."""
    t = db.task_get(tid)
    spec = _spec_of(t)
    if n > 0:
        spec["turns_ahead"] = int(n)
    elif "turns_ahead" in spec:
        spec.pop("turns_ahead")
    else:
        return
    db.task_update(tid, spec=spec)


def unsent_prompt(t: dict | None) -> str | None:
    """The prompt a queued task still has to type into its session (spec.unsent), or None once it is typed. A task is held like this
    while another board-queued prompt sits in Claude's queue: two queued prompts that Claude holds together are run as ONE turn (issue
    #64, seen on the box), so the board keeps at most one of them in Claude's queue per session and the rest wait here."""
    v = _spec_of(t).get("unsent")
    return v if isinstance(v, str) and v else None


def set_unsent(db, tid: int, text: str | None) -> None:
    """Store (text) or clear (None) the prompt a held task has still to type, leaving the rest of the spec as it was."""
    t = db.task_get(tid)
    spec = _spec_of(t)
    if text:
        spec["unsent"] = text
    elif "unsent" in spec:
        spec.pop("unsent")
    else:
        return
    db.task_update(tid, spec=spec)


def must_hold(db, row_id: int | None) -> bool:
    """Is a prompt queued for this WORKING Claude row now to be held in the board? True when a task of the row already waits for an earlier
    turn (its counter is above zero): its prompt is in Claude's queue, or is itself held."""
    return row_id is not None and any(turns_ahead(o) > 0 for o in db.tasks_for_session(row_id, ("running",)))


def forget_queue(db, t: dict | None) -> None:
    """A task leaves its session's queue without running (detached): its counter and held prompt go, and the tasks behind it have one
    Stop less to wait for (the Stop it would have used never comes)."""
    if not t:
        return
    mine = turns_ahead(t)
    row_id = t.get("session_row")
    set_turns_ahead(db, t["id"], 0)
    set_unsent(db, t["id"], None)
    if mine and row_id is not None:
        for o in db.tasks_for_session(row_id, ("running",)):
            if o["id"] != t["id"] and turns_ahead(o) > mine:
                set_turns_ahead(db, o["id"], turns_ahead(o) - 1)


def next_turns_ahead(db, row_id: int | None) -> int:
    """The counter for a prompt queued behind a WORKING Claude row now: the turn in flight is one Stop, and every task already queued on
    the row adds its own (its counter + 1), so the new one waits behind the last of them."""
    ahead = 1
    if row_id is not None:
        for o in db.tasks_for_session(row_id, ("running",)):
            ahead = max(ahead, turns_ahead(o) + 1)
    return ahead


def compose_prompt(template: str, parent: dict | None) -> str:
    """A chain step's prompt: the template with every {{result}} replaced by the parent's result (its first 12000 characters), or,
    without the placeholder, the template + a rule + 'Result of the previous step (<title>):' + the result. Never longer than a task
    prompt may be (the result is cut to fit, the template itself is always kept whole)."""
    res = ((parent or {}).get("result") or "")[:CHAIN_RESULT_MAX]
    n = template.count(PLACEHOLDER)
    if n:
        base = len(template) - n * len(PLACEHOLDER)
        room = max(0, (PROMPT_MAX - base) // n)
        return template.replace(PLACEHOLDER, res[:min(CHAIN_RESULT_MAX, room)])
    prefix = f"{template}\n\n---\nResult of the previous step ({(parent or {}).get('title') or 'previous step'}):\n"
    return prefix + res[:max(0, min(CHAIN_RESULT_MAX, PROMPT_MAX - len(prefix)))]


# ------------------------------------------------------------------ the limit gate

def _active_episodes(db, now: float) -> list[dict]:
    """Limit episodes ('lim' samples) whose window has not reset yet and that belong to the account in use (an episode recorded for
    another account says nothing about this one's room): [{kind, resets_at}]."""
    from . import accounts
    since = datetime.fromtimestamp(now - 8 * 86400, tz=timezone.utc).isoformat(timespec="seconds")
    try:
        cur = accounts.current(db)
    except Exception:
        cur = None
    out = []
    for _at, key, _value, meta_json in db.samples_query("lim", None, since, None):
        try:
            meta = json.loads(meta_json) if isinstance(meta_json, str) else (meta_json or {})
        except ValueError:
            continue
        if not isinstance(meta, dict):
            continue
        resets = _epoch(meta.get("resets_at"))
        if resets <= now:
            continue
        acct = meta.get("acct")
        if acct and cur and acct != cur:
            continue
        out.append({"kind": key, "resets_at": resets})
    return out


def limit_gate(db, now: float | None = None, agent: str = "claude") -> dict | None:
    """Why auto-dispatch (queued chain steps) must wait, or None when it may go. Claude: the 5 h or 7 d window at 85 % or more (the
    current account's reading; a window whose reset time has passed is open again), an active limit episode, or the scheduler's
    back-off after a rate-limited run. Codex (v0.5.16): its own usage window at 85 % or more (kv rate_limits_codex) or its own back-off;
    a Claude limit never holds a Codex step and the other way round. {kind: '5h' | '7d' | 'limit' | 'backoff' | 'codex', resets_at: epoch
    | None, pct: float | None}: when several apply, the one that clears last. A hand dispatch is never held by it, only warned."""
    now = time.time() if now is None else now
    holds: list[dict] = []
    if agent == "codex":
        pct, resets = scheduler._codex_window(db, now)
        if pct is not None and pct >= LIMIT_PCT:
            holds.append({"kind": "codex", "resets_at": _num(resets), "pct": pct})
    else:
        rl = ((db.kv_get("rate_limits") or {}).get("value")) or {}
        for key, kind in (("five_hour", "5h"), ("seven_day", "7d")):
            w = rl.get(key) if isinstance(rl, dict) else None
            if not isinstance(w, dict):
                continue
            pct, resets = _num(w.get("used_percentage")), _num(w.get("resets_at"))
            if pct is not None and pct >= LIMIT_PCT and not (resets and resets <= now):
                holds.append({"kind": kind, "resets_at": resets, "pct": pct})
        for ep in _active_episodes(db, now):
            holds.append({"kind": "limit", "resets_at": ep["resets_at"], "pct": None})
    until = scheduler.quota_state(db, agent if agent == "codex" else "claude").get("backoff_until")
    if until and _epoch(until) > now:
        holds.append({"kind": "backoff", "resets_at": _epoch(until), "pct": None})
    if not holds:
        return None
    holds.sort(key=lambda h: h["resets_at"] if h["resets_at"] is not None else float("inf"))
    return dict(holds[-1])


def chain_positions(db, rows: list[dict]) -> dict[int, dict]:
    """{task id: {i, n}} for the tasks of `rows` that belong to a chain: i is the step's depth in its chain (1 for the first, a step queued
    behind step 1 is 2; fan-out siblings share a number), n the deepest step of the chain. Siblings that are not in `rows` count (one query)."""
    members = db.tasks_in_chains({t.get("chain_id") for t in rows})
    groups: dict[str, dict[int, int | None]] = {}
    for m in members:
        groups.setdefault(m["chain_id"], {})[m["id"]] = m.get("parent_id")
    out: dict[int, dict] = {}
    for chain_id, parents in groups.items():
        depth: dict[int, int] = {}
        for tid in parents:
            d, cur, seen = 1, tid, {tid}
            while parents.get(cur) in parents and parents[cur] not in seen:      # a parent outside the chain (deleted) ends the walk
                cur = parents[cur]
                seen.add(cur)
                d += 1
            depth[tid] = d
        n = max(depth.values())
        for tid, d in depth.items():
            out[tid] = {"i": d, "n": n}
    return out


def mem_processing() -> bool:
    """Is claude-mem's worker busy with its queue (GET /api/processing-status isProcessing)? An auto-close right then could SIGHUP the
    summarize hook the Stop just started. False when claude-mem is off, unreachable or answers something else: waiting for a worker
    that is not there would only delay the close."""
    if not settings.claude_mem:
        return False
    try:
        from . import memory
        status, body = memory.fetch(memory.worker_base(), "/api/processing-status", timeout=2.0)
    except Exception as e:
        log.debug("claude-mem processing-status unavailable: %s", e)
        return False
    return status == 200 and isinstance(body, dict) and body.get("isProcessing") is True


def transcript_tail(row: dict | None) -> str | None:
    """The closing text of a Claude conversation read from the tail of its transcript (the newest assistant entry that has any text):
    the fallback for a Stop payload without last_assistant_message. None for another agent, a missing or unreadable file."""
    row = row or {}
    if (row.get("agent") or "claude") != "claude":
        return None
    try:
        path = agents.get("claude").transcript_path(row)
        if path is None or not path.is_file():
            return None
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - TRANSCRIPT_TAIL))
            data = f.read().decode("utf-8", "replace")
    except (OSError, KeyError, ValueError):
        return None
    lines = data.splitlines()
    if size > TRANSCRIPT_TAIL:
        lines = lines[1:]                                    # the first line of the window is cut in half
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        content = (obj.get("message") or {}).get("content") if isinstance(obj.get("message"), dict) else None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(c["text"] for c in content if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str))
        else:
            continue
        if text.strip():
            return text.strip()[:RESULT_MAX]
    return None


# ------------------------------------------------------------------ the runtime

class Runtime:
    """See the module docstring. `start_session(task, *, prompt=None, auto_close=None)` starts a queued task in a new lane session and
    returns its response (None: someone else already did); `end_session(name, reason)` kills a session and closes its row. The rest are
    seams for tests and default to the real thing: send_text(name, text) types a line into the pane, real_clients(name) counts people
    attached, perm_pending(name) says whether a permission request waits, mem_processing() whether claude-mem is busy, capture(name)
    reads the pane and send_keys(name, keys) presses named keys (the exit dialog), paste_prompt(name, text) types a held queued prompt,
    clock() is epoch seconds."""

    def __init__(self, db, *, start_session, end_session, send_text=None, real_clients=None, perm_pending=None, mem_processing=None,
                 capture=None, send_keys=None, paste_prompt=None, clock=time.time, grace: float | None = None, tick: float = TICK):
        from . import tmux
        self.db = db
        self.start_session, self.end_session = start_session, end_session
        self.send_text = send_text or (lambda name, text: tmux.send_text(name, text, enter=True))
        self.real_clients = real_clients or (lambda name: tmux.real_clients(name))
        self.perm_pending = perm_pending or (lambda name: any(p.get("tmux_name") == name for p in db.perm_pending()))
        self.mem_processing = mem_processing or globals()["mem_processing"]
        self.capture = capture or (lambda name: tmux.capture(name, 40))
        self.send_keys = send_keys or (lambda name, keys: tmux.send_keys(name, keys))
        self.paste_prompt = paste_prompt or (lambda name, text: tmux.paste_text(name, text, enter=True))
        self._turn_end: dict[int, float] = {}                # session row -> when its last Stop was seen (the settle time before a held prompt is typed)
        self.clock = clock
        self.grace = settings.autoclose_grace if grace is None else float(grace)
        self.tick = tick
        self.pending: dict[str, dict] = {}                   # tmux name -> {task, row_id, state_at, stage, due, started, ...}
        self._lock = threading.RLock()                       # guards pending and _starting; never held across a launch
        self._starting: set[int] = set()
        self._retry: dict[int, float] = {}
        self._last_sweep = 0.0
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- lifecycle
    def start(self) -> None:
        """Forget the countdowns of the previous process (they lived in memory) and start the ticker thread."""
        self.clear_stale_flags()
        if self._thread is None or not self._thread.is_alive():
            self._halt.clear()
            self._thread = threading.Thread(target=self._loop, name="taskflow", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(3)

    def _loop(self) -> None:
        while not self._halt.is_set():
            try:
                self.run_due()
            except Exception as e:                           # a bad tick must never end the thread
                log.warning("taskflow tick failed: %s", e)
            self._halt.wait(self.tick)

    def clear_stale_flags(self) -> int:
        """Clear flags.autoclose on every open row: a countdown the previous process was running is gone with it."""
        n = 0
        for name, row in self.db.open_rows().items():
            if isinstance((row.get("flags") or {}).get("autoclose"), dict):
                self.db.update_flags(name, {"autoclose": None})
                n += 1
        return n

    # ---- one tick
    def run_due(self, now: float | None = None) -> dict:
        """Everything that is due at `now`: pending closes, queued chain steps, and (every SWEEP_EVERY seconds) the sweep. Returns what
        happened: {closed: [names], exit_sent: [names], dropped: [names], started: [task ids], blocked: [task ids], swept: [task ids]}."""
        now = self.clock() if now is None else now
        out = {"closed": [], "exit_sent": [], "dropped": [], "started": [], "blocked": [], "swept": [], "typed": []}
        for name, p in list(self.pending.items()):
            if now < p["due"]:
                continue
            try:
                self._step(name, p, now, out)
            except Exception as e:
                log.warning("auto-close of %s failed: %s", name, e)
                with self._lock:
                    if self.pending.get(name) is p:
                        p["due"] = now + RECHECK
                        if now - p["started"] >= GIVE_UP:
                            self.pending.pop(name, None)
        try:
            out["typed"] = self.release_held(now)
        except Exception as e:
            log.warning("taskflow held-prompt release failed: %s", e)
        try:
            self._advance_queued(now, out)
        except Exception as e:
            log.warning("taskflow chain advance failed: %s", e)
        if now - self._last_sweep >= SWEEP_EVERY:
            self._last_sweep = now
            try:
                out["swept"] = self.sweep(now)
            except Exception as e:
                log.warning("taskflow sweep failed: %s", e)
        return out

    # ---- a turn ends
    def active_task(self, name: str, row: dict | None) -> dict | None:
        """The task a Stop of this session row belongs to: the newest `running` task whose session_row is the row and whose own turn has
        come (turns_ahead 0; a prompt queued behind a turn in flight waits for the Stop after it). A task whose session
        was relaunched without it (a legacy task with no session_row; a task handed to a session that a reboot recovery resumed in a
        fresh row) is found by its tmux name and bound to the row on the way."""
        row_id = (row or {}).get("row_id")
        if row_id is None:
            return None
        bound = self.db.tasks_for_session(row_id, ("running",))
        ready = [t for t in bound if not turns_ahead(t) and not unsent_prompt(t)]       # a held prompt is not typed yet: no Stop is its own
        if ready:
            return ready[0]
        if bound:
            return None                                                             # every running task still waits for an earlier turn
        if (row or {}).get("launcher") in ("task", "recovered"):
            for t in reversed(self.db.tasks_by_phase(("running",))):
                if t.get("tmux_name") == name and self._continues(t, row):
                    self.db.task_update(t["id"], session_row=row_id)
                    return {**t, "session_row": row_id}
        return None

    def _pass_turn(self, row: dict | None) -> list[int]:
        """A turn of this row ended: every running task queued behind a turn (turns_ahead above zero) has one Stop less to wait for.
        Returns their ids."""
        row_id = (row or {}).get("row_id")
        if row_id is None:
            return []
        out = []
        for t in self.db.tasks_for_session(row_id, ("running",)):
            n = turns_ahead(t)
            if n:
                set_turns_ahead(self.db, t["id"], n - 1)
                out.append(t["id"])
        return out

    def release_held(self, now: float | None = None) -> list[int]:
        """Type the next held queued prompt of each Claude session (issue #64). Claude folds the prompts it holds in its queue into ONE turn,
        so the board lets at most one of its prompts wait in Claude's queue: a task queued behind another queued task is only stored
        (spec.unsent) and is typed here once no other prompt of the board waits in the queue (the Stop before it has come), SETTLE seconds
        after that Stop so that Claude has taken the queued turn off its queue. Waits while the session is in a permission prompt or a
        dialog. Returns the ids of the tasks typed."""
        now = self.clock() if now is None else now
        held: dict[int, list[dict]] = {}
        for t in self.db.tasks_by_phase(("running",)):
            if unsent_prompt(t) and t.get("session_row") is not None:
                held.setdefault(t["session_row"], []).append(t)
        typed = []
        for row_id, tasks in held.items():
            name = tasks[0].get("tmux_name") or ""
            row = self.db.open_row(name) if name else None
            if row is None or row.get("row_id") != row_id:
                continue                                                            # the session ended: the sweep cancels these tasks
            if any(turns_ahead(o) > 0 and not unsent_prompt(o) for o in self.db.tasks_for_session(row_id, ("running",))):
                continue                                                            # a prompt of the board is still waiting in Claude's queue
            st, flags = row.get("state"), row.get("flags") or {}
            if not (st in ("working", "done", "idle") or (st == "waiting" and flags.get("wait_kind") == "idle")) or self.perm_pending(name):
                continue
            since = self._turn_end.get(row_id)
            if since is None and st in ("done", "idle"):
                since = _epoch(row.get("state_at"))
            if since is not None and now - since < SETTLE:
                continue
            nxt = min(tasks, key=lambda x: (turns_ahead(x), x["id"]))
            text = unsent_prompt(nxt)
            set_unsent(self.db, nxt["id"], None)                                    # first: a second tick must never type it twice
            try:
                self.paste_prompt(name, text)
            except Exception as e:
                set_unsent(self.db, nxt["id"], text)
                log.warning("typing the held prompt of task %s failed: %s", nxt["id"], e)
                continue
            if st != "working":
                self.db.set_state(name, "working", "TaskDispatch", prompt=text[:500])
            self.db.add_event(name, "TaskDispatch", nxt.get("title"), f"task {nxt['id']} typed into the session (it was held for the prompt before it)",
                              {"task": nxt["id"], "queued": True, "held": True}, agent=row.get("agent"))
            self._turn_end[row_id] = now                                            # the next held prompt waits for this one's turn to begin
            typed.append(nxt["id"])
        return typed

    def _continues(self, t: dict, row: dict) -> bool:
        """Is `row` (open, in the task's tmux session) the task's own session carried on? A legacy task (no session_row) of a task
        session, or a task whose old row has ended and that a reboot recovery relaunched in this one (launcher 'recovered')."""
        sr = t.get("session_row")
        if sr is None:
            return row.get("launcher") == "task"
        if row.get("launcher") != "recovered" or sr >= (row.get("row_id") or 0):
            return False
        return bool(self.db.sessions_ended([sr]).get(sr, {}).get("ended_at"))

    def on_turn_end(self, name: str, row: dict | None, result: str | None, *, failed: bool = False, message: str | None = None,
                    limit: dict | None = None) -> dict | None:
        """Stop (or, with failed, StopFailure) of the session `name` whose open row was `row`. Returns what it did ({task, phase, ...})
        or None when no task was running on the row. A Stop that belongs to an earlier turn of the row (a task queued behind it, issue
        #64) credits nobody and starts no countdown: {task: None, skipped: [the queued task ids]}."""
        t = self.active_task(name, row)
        if (row or {}).get("row_id") is not None and not (failed and limit is not None):
            self._turn_end[row["row_id"]] = self.clock()
        parked = failed and limit is not None                    # a rate limit does not end the turn: the queue behind it waits on
        skipped = [] if parked else self._pass_turn(row)
        if t is None:
            return {"task": None, "skipped": skipped} if skipped else None
        now_iso = db_now()
        if failed:
            self.cancel_close(name)
            if limit is not None:
                return {"task": t["id"], "phase": "running", "parked": True}      # autoresume continues the session after the reset
            text = (message or "the turn failed").strip()[:RESULT_MAX]
            self.db.task_update(t["id"], phase="failed", result=text, result_at=now_iso, done_at=now_iso)
            self.advance(t["id"])                                                   # cancels the steps queued behind it
            return {"task": t["id"], "phase": "failed"}
        text = (result or "").strip() or transcript_tail(self.db.open_row(name) or row) or ""
        self.db.task_update(t["id"], phase="done", result=text[:RESULT_MAX] or None, result_at=now_iso, done_at=now_iso)
        out = {"task": t["id"], "phase": "done", "close": None}
        try:
            self.advance(t["id"])
        except Exception as e:                                                      # a chain problem must not cost the result its close
            log.warning("chain advance after task %s failed: %s", t["id"], e)
        if text:
            try:                                                                    # claude-mem write-back: off by default, a daemon thread, never fails the task
                from . import memory_proxy
                memory_proxy.writeback_task(self.db, t, text[:RESULT_MAX])
            except Exception as e:
                log.debug("memory write-back not started: %s", e.__class__.__name__)
        out["close"] = self._schedule_close({**t, "phase": "done"}, name, row, text)
        return out

    def _schedule_close(self, t: dict, name: str, row: dict | None, text: str) -> str | None:
        """After a finished turn: the countdown ('pending'), a hold ('held': the message asks a question), or nothing (None)."""
        fresh = self.db.task_get(t["id"]) or t
        if not fresh.get("auto_close"):
            return None
        row_id = (row or {}).get("row_id")
        if any(o["id"] != t["id"] for o in self.db.tasks_for_session(row_id, ("queued", "running"))):
            return None                                                              # another task still needs this session
        if ends_with_question(text):
            self.db.update_flags(name, {"autoclose": {"task": t["id"], "held": "question"}})
            return "held"
        cur = self.db.open_row(name) or row or {}
        now = self.clock()
        due = now + self.grace
        with self._lock:
            self.pending[name] = {"task": t["id"], "row_id": row_id, "state_at": cur.get("state_at"), "stage": "grace", "due": due,
                                  "started": now}
        self.db.update_flags(name, {"autoclose": {"task": t["id"], "due": _iso(due)}})
        return "pending"

    # ---- activity that cancels a countdown
    def on_activity(self, name: str, event: str, row: dict | None = None, norm=None) -> bool:
        """A hook that shows the session is not finished: a new prompt, a permission request or dialog, a subagent, a tool batch, a
        compaction or an interrupt. Cancels a pending close (and clears a question hold: the person answered). True when it did."""
        if event == "Notification":
            wait = (getattr(norm, "flags", None) or {}).get("wait_kind")
            if wait not in DIALOG_WAITS:
                return False                                                         # idle_prompt and the like change nothing
        elif event not in ACTIVITY_EVENTS:
            return False
        marked = isinstance((((row or {}).get("flags")) or {}).get("autoclose"), dict)
        if name not in self.pending and not marked:
            return False                                                             # the common case: nothing to cancel, no DB work
        self.cancel_close(name)
        return True

    def cancel_close(self, name: str) -> bool:
        """Drop a pending close of this session and clear flags.autoclose. True when a countdown was running."""
        with self._lock:
            p = self.pending.pop(name, None)
        self.db.update_flags(name, {"autoclose": None})
        return p is not None

    def keep_open(self, task_id: int) -> dict:
        """'Keep open': cancel the pending close of the task's session (or its question hold) and switch the task's auto_close off, so a
        later Stop of the same session does not start another countdown. {kept: bool (a countdown was running), held: bool}."""
        t = self.db.task_get(task_id)
        if t is None:
            return {"kept": False, "held": False}
        name = self._open_name(t)
        kept = held = False
        if name:
            row = self.db.open_row(name) or {}
            af = (row.get("flags") or {}).get("autoclose")
            held = isinstance(af, dict) and af.get("task") == task_id and af.get("held") is not None
            with self._lock:
                p = self.pending.get(name)
                mine = p is not None and p["task"] == task_id and p["stage"] != "exiting"
            if mine:
                kept = self.cancel_close(name)
            elif isinstance(af, dict) and af.get("task") == task_id and not af.get("closing"):
                self.db.update_flags(name, {"autoclose": None})
        self.db.task_update(task_id, auto_close=0)
        return {"kept": kept, "held": held}

    def _open_name(self, t: dict) -> str | None:
        """The tmux name of the task's open session row, or None when it has none (never started, or the row ended)."""
        sr = t.get("session_row")
        if sr is None:
            return None
        for name, row in self.db.open_rows().items():
            if row.get("row_id") == sr:
                return name
        return None

    def close_now(self, task_id: int) -> dict | None:
        """'Close session': the graceful close at once. Sends the agent's exit command now and kills the session EXIT_WAIT seconds later
        (a tick does it). Returns {name, kill_at} or None when the task has no open session. The caller checked that the session is not busy."""
        t = self.db.task_get(task_id)
        name = self._open_name(t) if t else None
        row = self.db.open_row(name) if name else None
        if not row:
            return None
        now = self.clock()
        self._send_exit(name, row)
        with self._lock:
            self.pending[name] = {"task": task_id, "row_id": row["row_id"], "state_at": row.get("state_at"), "stage": "exiting",
                                  "due": now + EXIT_POLL, "kill_at": now + EXIT_WAIT, "started": now}
        self.db.update_flags(name, {"autoclose": {"task": task_id, "closing": True}})
        return {"name": name, "kill_at": _iso(now + EXIT_WAIT)}

    def _send_exit(self, name: str, row: dict) -> str:
        cmd = agents.get(row.get("agent") or "claude").exit_command()
        self.send_text(name, cmd)
        return cmd

    # ---- the close, step by step
    def _drop(self, name: str, p: dict, out: dict) -> None:
        with self._lock:
            if self.pending.get(name) is p:
                self.pending.pop(name, None)
        out["dropped"].append(name)

    def _step(self, name: str, p: dict, now: float, out: dict) -> None:
        row = self.db.open_row(name)
        if row is None or row.get("row_id") != p["row_id"]:
            self._drop(name, p, out)                                                   # the session ended (or was replaced) on its own
            return
        if p["stage"] == "exiting":
            if row.get("agent") in (None, "claude") and self._answer_exit_dialog(name, row, p, now):
                p["kill_at"] = now + EXIT_WAIT                                          # the agent was told to keep the worktree: let it finish quitting
            kill_at = p.get("kill_at", p["due"])
            if now < kill_at:
                p["due"] = min(kill_at, now + EXIT_POLL)
                return
            task = self.db.task_get(p["task"]) or {}
            self.db.add_event(name, "AutoClose", task.get("title"), "closed after the task's turn ended", {"task": p["task"]},
                              agent=row.get("agent"))
            self.end_session(name, "auto_close")
            with self._lock:
                self.pending.pop(name, None)
            out["closed"].append(name)
            return
        flags = row.get("flags") or {}
        af = flags.get("autoclose")
        t = self.db.task_get(p["task"])
        idle_wait = row.get("state") == "waiting" and flags.get("wait_kind") == "idle"      # Claude's idle-prompt Notification, a minute after the Stop
        if idle_wait and row.get("state_at") != p["state_at"]:
            p["state_at"] = row.get("state_at")                # not a new turn: anything that is one (a prompt, a dialog) has cancelled the close already
        gone = ("the close was cancelled" if not isinstance(af, dict) or af.get("task") != p["task"]
                else "the task changed" if t is None or t.get("phase") != "done" or not t.get("auto_close") or t.get("archived_at")
                else "the session moved on" if not (row.get("state") == "done" or idle_wait) or row.get("state_at") != p["state_at"]
                else "a permission request is waiting" if self.perm_pending(name) else None)
        if gone:
            self._drop(name, p, out)                                                   # the session moved on: it is the person's again
            self.db.update_flags(name, {"autoclose": None})
            self.db.add_event(name, "AutoCloseSkipped", gone, f"did not close the session: {gone}", {"task": p["task"]}, agent=row.get("agent"))
            return
        why = ("compacting" if flags.get("compacting") else "a subagent is running" if (flags.get("subagents") or 0) > 0
               else "someone is at the terminal" if self.real_clients(name) > 0 else None)
        if why:
            if now - p["started"] >= GIVE_UP:
                self._drop(name, p, out)
                self.db.update_flags(name, {"autoclose": None, "autoclose_skipped": {"task": p["task"], "reason": why, "at": _iso(now)}})
                self.db.add_event(name, "AutoCloseSkipped", why, f"kept the session open: {why}", {"task": p["task"]}, agent=row.get("agent"))
                return
            p["stage"], p["due"] = "clients", now + RECHECK
            self.db.update_flags(name, {"autoclose": {"task": p["task"], "due": _iso(p["due"]), "waiting": why}})
            return
        if self.mem_processing():
            p.setdefault("mem_since", now)
            if now - p["mem_since"] < MEM_WAIT:
                p["stage"], p["due"] = "mem", now + MEM_RECHECK
                self.db.update_flags(name, {"autoclose": {"task": p["task"], "due": _iso(p["due"]), "waiting": MEM_WAITING}})   # #88: the card says why
                return
        if self.pending.get(name) is not p:                                            # cancelled while this pass was looking
            return
        out["exit_sent"].append(name)
        self._send_exit(name, row)
        p["stage"], p["due"], p["kill_at"] = "exiting", now + EXIT_POLL, now + EXIT_WAIT
        self.db.update_flags(name, {"autoclose": {"task": p["task"], "closing": True}})

    def _answer_exit_dialog(self, name: str, row: dict, p: dict, now: float) -> bool:
        """Claude's /exit in a worktree with changes asks "You have N uncommitted files. These will be lost if you remove the worktree",
        "1. Keep worktree" (preselected) / "2. Remove worktree", and nothing answers it: the board used to kill the session after
        EXIT_WAIT, with no SessionEnd, so claude-mem never summarised it. Look at the pane while the agent quits; when the dialog is up,
        pick Keep (the safe choice: the work stays on disk, a Remove is never sent) and let the agent end itself. True when a key was sent."""
        if p.get("answered", 0) >= EXIT_DIALOG_MAX:
            return False
        try:
            screen = (self.capture(name) or "").lower()
        except Exception as e:
            log.debug("could not read the pane of %s: %s", name, e)
            return False
        if EXIT_DIALOG_KEEP not in screen or EXIT_DIALOG_REMOVE not in screen:
            return False
        keep = next((ln for ln in screen.splitlines() if EXIT_DIALOG_KEEP in ln), "")
        remove = next((ln for ln in screen.splitlines() if EXIT_DIALOG_REMOVE in ln), "")
        marks = ("❯", "›", ">", "→")
        on_remove = remove.lstrip().startswith(marks) and not keep.lstrip().startswith(marks)
        self.send_keys(name, ["Up", "Enter"] if on_remove else ["Enter"])
        p["answered"] = p.get("answered", 0) + 1
        self.db.add_event(name, "AutoCloseDialog", "keep worktree", "the exit dialog was answered: keep the worktree",
                          {"task": p["task"]}, agent=row.get("agent"))
        return True

    # ---- chains
    def _gate_once(self, now: float):
        """A callable that looks the limit gate up per agent on first use and remembers the answer for the rest of one pass (False = open)."""
        box: dict = {}

        def gate(agent: str = "claude"):
            agent = "codex" if agent == "codex" else "claude"
            if agent not in box:
                box[agent] = limit_gate(self.db, now, agent) or False
            return box[agent]
        return gate

    def advance(self, parent_id: int) -> list[int]:
        """Act on the steps queued behind a task now: start each when the parent is done (unless the limit gate holds), cancel each when it
        failed or was cancelled. Returns the ids of the steps started. The ticker repeats this for held steps."""
        out = {"started": [], "blocked": []}
        now = self.clock()
        gate = self._gate_once(now)
        parent = self.db.task_get(parent_id)
        for child in self.db.children_of(parent_id):
            if (child.get("phase") or "running") == "queued":
                self._advance_one(child, parent, gate, now, out)
        return out["started"]

    def _advance_queued(self, now: float, out: dict) -> None:
        for _ in range(12):                                   # a blocked step blocks its own children on the next pass
            changed = False
            gate = self._gate_once(now)
            for child in self.db.tasks_by_phase(("queued",)):
                pid = child.get("parent_id")
                if pid is None:
                    continue
                changed = self._advance_one(child, self.db.task_get(pid), gate, now, out) or changed
            if not changed:
                return

    def _advance_one(self, child: dict, parent: dict | None, gate, now: float, out: dict) -> bool:
        """One queued step against its parent. True when the step left `queued` (started, failed or cancelled)."""
        phase = (parent or {}).get("phase")
        if parent is None or phase in ("failed", "cancelled"):
            self._block(child)
            out["blocked"].append(child["id"])
            return True
        if phase != "done" or self._retry.get(child["id"], 0) > now or gate(child.get("agent") or "claude"):
            return False
        with self._lock:
            if child["id"] in self._starting:
                return False
            self._starting.add(child["id"])
        try:
            prompt = compose_prompt(child["prompt"], parent)
            try:
                # True when the step asked for the close; None leaves it to the board (an explicit 'off' kept in the step's spec, else
                # the lane default: on, a chain step is a session of its own)
                started = self.start_session(child, prompt=prompt, auto_close=True if child.get("auto_close") else None)
            except Exception as e:
                if e.__class__.__name__ in ("TmuxDown", "TmuxError"):
                    self._retry[child["id"]] = now + RETRY_START
                    log.warning("chain step %s could not start yet: %s", child["id"], e)
                    return False
                stamp = db_now()
                self.db.task_update(child["id"], phase="failed", result=f"could not start: {str(e)[:300]}", result_at=stamp, done_at=stamp)
                log.warning("chain step %s failed to start: %s", child["id"], e)
                out["blocked"].append(child["id"])
                return True
            if started is not None:
                out["started"].append(child["id"])
            return started is not None
        finally:
            with self._lock:
                self._starting.discard(child["id"])

    def _block(self, child: dict) -> None:
        stamp = db_now()
        self.db.task_update(child["id"], phase="cancelled", result=BLOCKED, result_at=stamp, done_at=stamp)

    # ---- the sweep
    def sweep(self, now: float | None = None) -> list[int]:
        """Running tasks whose session is over without a result become cancelled: its row ended (the session was killed, or the box
        rebooted and nothing relaunched it), or the agent quit (state 'ended' for ENDED_GRACE seconds: a SessionEnd that no
        SessionStart followed). Returns their ids. A task whose session a reboot recovery resumed in a fresh row is bound to that row
        instead; a legacy task with no session_row is left to the board's lazy name binding."""
        now = self.clock() if now is None else now
        opened = self.db.open_rows()
        open_ids = {r.get("row_id") for r in opened.values()}
        quit_ids = {r.get("row_id") for r in opened.values()
                    if r.get("state") == "ended" and now - _epoch(r.get("state_at")) >= ENDED_GRACE}
        swept = []
        for t in self.db.tasks_by_phase(("running",)):
            sr = t.get("session_row")
            if sr is None or t.get("result") or (sr in open_ids and sr not in quit_ids):
                continue
            cont = opened.get(t.get("tmux_name") or "")
            if sr not in open_ids and cont is not None and self._continues(t, cont):
                self.db.task_update(t["id"], session_row=cont["row_id"])
                continue
            stamp = db_now()
            self.db.task_update(t["id"], phase="cancelled", done_at=stamp)
            swept.append(t["id"])
        return swept
