"""Durable scheduler: cron (or one-off) headless `claude -p` runs, each in a fresh worktree, with a
concurrency cap and quota awareness. Results become task cards (worktree + branch), resumable in a terminal."""
from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from croniter import croniter

from . import claude_auth, notify, projects, tasks
from .config import settings
from .db import now as db_now

log = logging.getLogger("ccboard.scheduler")
TICK_SECONDS = 30
CAP = 2                    # concurrent headless runs
QUOTA_MAX_PCT = 85.0       # skip launching when the 5-hour window is above this
DEFER_MINUTES = 15
RUN_TIMEOUT = 3600
MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk")   # bypassPermissions only inside a devcontainer (v0.4.5)
RATE_RE = re.compile(r"rate.?limit|usage limit|limit reached|too many requests", re.I)
# The way Claude Code words a hit limit; checked against the END of a result so a long run cut short mid-way is
# caught without flagging a run that merely discussed rate limiting somewhere in its summary.
LIMIT_MSG_RE = re.compile(r"hit your [^.\n]{0,30}limit|usage limit|limit (has been |was )?reached|out of (usage|credits)|"
                          r"too many requests|\brate.?limited\b", re.I)
BACKOFF_MINUTES = 30       # after a run comes back rate-limited, defer everything this long (or until the window resets)
BACKOFF_MAX_HOURS = 5
KV_BACKOFF = "sched_backoff_until"
KV_LOGIN_ALERT = "sched_login_alerted"


FORBIDDEN_ARG_PARTS = ("dangerously", "bypasspermissions", "--permission-mode", "--settings", "--setting-sources",
                       "--permission-prompt", "--allow-dangerously")


def check_extra_args(args: str | None) -> list[str]:
    """Extra CLI args for unattended runs: the permission mode is set by the job itself and may never be
    escalated through the args (any spelling, '=' forms, settings overrides)."""
    if not args:
        return []
    try:
        parts = shlex.split(args)
    except ValueError as e:
        raise ValueError(f"args: {e}")
    for p in parts:
        low = p.lower()
        if any(f in low for f in FORBIDDEN_ARG_PARTS):
            raise ValueError(f"argument not allowed for unattended runs: {p}")
    return parts


def valid_cron(expr: str) -> bool:
    try:
        return croniter.is_valid(expr)
    except Exception:
        return False


def next_fire(expr: str, base: datetime | None = None) -> str:
    base = base or datetime.now(timezone.utc)
    return croniter(expr, base).get_next(datetime).astimezone(timezone.utc).isoformat(timespec="seconds")


def quota_state(db) -> dict:
    """What the scheduler knows about the 5-hour window: pct is None until an interactive session's statusline has
    reported (headless runs never do), plus any back-off set after a rate-limited run."""
    rl = db.kv_get("rate_limits")
    pct = resets_at = None
    try:
        w = ((rl or {}).get("value") or {}).get("five_hour") or {}
        pct = float(w.get("used_percentage"))
        resets_at = w.get("resets_at")
    except (TypeError, ValueError, AttributeError):
        pct = None
    until = ((db.kv_get(KV_BACKOFF) or {}).get("value")) or None
    if until and until <= db_now():
        until = None
    return {"pct": pct, "at": (rl or {}).get("at"), "resets_at": resets_at, "backoff_until": until, "known": pct is not None}


def quota_blocked(db) -> str | None:
    q = quota_state(db)
    if q["backoff_until"]:
        return f"backing off until {q['backoff_until']} after a rate-limited run"
    if q["pct"] is not None and q["pct"] >= QUOTA_MAX_PCT:
        return f"5-hour window at {q['pct']:.0f}% (limit {QUOTA_MAX_PCT:.0f}%)"
    # An unknown reading (no interactive session yet) does not block: a headless-only box would otherwise never
    # run anything. The UI shows "quota unknown", and a rate-limited run triggers set_backoff().
    return None


def login_blocked() -> str | None:
    """Headless runs use the box's own Claude login (a subscription login, on a box without an API key). While it is
    missing or expired every run would fail at once, so defer instead and let the board say 'Log in'."""
    st = claude_auth.status()
    if not st.get("installed"):
        return "claude is not installed on this box"
    if not st.get("loggedIn"):
        return "claude is not logged in on this box (click Log in on the board)"
    return None


def set_backoff(db, resets_at=None) -> str:
    """Defer all runs for BACKOFF_MINUTES, or until the window resets when the statusline told us when (capped)."""
    now_dt = datetime.now(timezone.utc)
    until = now_dt + timedelta(minutes=BACKOFF_MINUTES)
    try:
        r = datetime.fromtimestamp(float(resets_at), tz=timezone.utc)
        if until < r <= now_dt + timedelta(hours=BACKOFF_MAX_HOURS):
            until = r
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    s = until.isoformat(timespec="seconds")
    db.kv_set(KV_BACKOFF, s)
    return s


def build_command(prompt: str, slug: str, mode: str, max_turns: int, budget: float | None, extra: list[str]) -> list[str]:
    cmd = ["claude", "-p", prompt, "--worktree", slug, "--output-format", "json", "--permission-mode", mode,
           "--max-turns", str(max_turns)]
    if budget:
        cmd += ["--max-budget-usd", f"{budget:.2f}"]
    return cmd + extra


def parse_result(stdout: str) -> dict:
    """claude -p --output-format json -> {text, session_id, cost, turns, is_error, subtype, rate_limited}."""
    data = None
    for line in reversed([ln for ln in stdout.splitlines() if ln.strip()]):
        try:
            data = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(data, dict):
        return {"text": stdout.strip()[-20000:], "session_id": None, "cost": None, "turns": None, "is_error": True,
                "subtype": "no_json", "rate_limited": bool(RATE_RE.search(stdout or ""))}
    text = data.get("result") if isinstance(data.get("result"), str) else json.dumps(data.get("result"))
    err = bool(data.get("is_error")) or str(data.get("subtype", "")).startswith("error")
    turns = data.get("num_turns")
    mentions_limit = bool(RATE_RE.search(text or ""))
    # A rate-limited run can come back as "success" with the limit text as its only output (claude-code #79500),
    # so a short single-turn result that talks about limits counts too.
    rate_limited = (mentions_limit and err) or str(data.get("subtype", "")) == "error_rate_limit" or \
        (mentions_limit and (turns is None or turns <= 1) and len(text or "") < 300) or \
        bool(LIMIT_MSG_RE.search((text or "")[-500:]))     # a limit hit after several turns ends the output with its message
    return {"text": (text or "")[:20000], "session_id": data.get("session_id"), "cost": data.get("total_cost_usd"),
            "turns": turns, "is_error": err, "subtype": data.get("subtype"), "rate_limited": rate_limited}


def run_job(db, job: dict, run_id: int) -> dict:
    """Execute one run synchronously (called in a worker thread). Returns the run summary."""
    exe = settings.claude_bin()
    rpath = projects.repo_path(job["project"], job["repo"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    slug = f"{tasks.slugify(job['name'])[:30]}-{stamp}".strip("-")
    slug = tasks.unique_slug(rpath, slug, db.task_slugs(job["project"], job["repo"]))
    try:
        extra = check_extra_args(job.get("args"))
    except ValueError:
        extra = []
    cmd = build_command(job["prompt"], slug, job.get("permission_mode") or "acceptEdits", int(job.get("max_turns") or 30),
                        job.get("max_budget_usd"), extra)
    if exe:
        cmd[0] = exe
    summary: dict = {"status": "error", "error": None}
    try:
        tasks.ensure_excluded(rpath)
        cp = subprocess.run(cmd, cwd=str(rpath), capture_output=True, text=True, timeout=int(job.get("timeout_s") or RUN_TIMEOUT),
                            stdin=subprocess.DEVNULL)
        res = parse_result(cp.stdout)
        if cp.returncode != 0 and not res["text"]:
            res["text"] = (cp.stderr or "").strip()[-4000:]
        if not res["rate_limited"] and cp.returncode != 0 and RATE_RE.search(cp.stderr or ""):
            res["rate_limited"] = True                       # the CLI reported the limit on stderr and gave up
        status = "rate_limited" if res["rate_limited"] else ("error" if res["is_error"] or cp.returncode != 0 else "ok")
        if status == "rate_limited":
            log.warning("job %s hit a rate limit; deferring all runs until %s", job["id"], set_backoff(db, quota_state(db).get("resets_at")))
        summary.update(status=status, result=res["text"], session_id=res["session_id"], cost_usd=res["cost"],
                       num_turns=res["turns"], error=None if status == "ok" else (res["subtype"] or f"exit {cp.returncode}"))
    except subprocess.TimeoutExpired:
        summary.update(status="error", error="timed out", result="")
    except (OSError, projects.BadRequest) as e:
        summary.update(status="error", error=str(e)[:300], result="")
    wt = tasks.worktree_path(rpath, slug)
    task_id = None
    if wt.is_dir():
        # unlock the worktree -p left locked so archive/merge can remove it later
        subprocess.run(["git", "-C", str(rpath), "worktree", "unlock", str(wt)], capture_output=True, timeout=10)
        tmux_name = f"{job['project']}--{job['repo']}--j-{slug}"
        title = f"[{job['name']}] {stamp}"
        task_id = db.task_add(project=job["project"], repo=job["repo"], slug=slug, title=title, prompt=job["prompt"],
                              branch=f"worktree-{slug}", base=tasks.default_branch(rpath), worktree=str(wt),
                              tmux_name=tmux_name, claude_session_id=summary.get("session_id"))
        if summary.get("cost_usd") is not None:
            db.task_update(task_id, cost_usd=summary["cost_usd"])
    db.run_finish(run_id, **summary, worktree=str(wt) if wt.is_dir() else None, branch=f"worktree-{slug}", task_id=task_id)
    db.job_update(job["id"], last_status=summary["status"], last_run_at=db_now())
    log.info("job %s run %s: %s", job["id"], run_id, summary["status"])
    return summary


class Worker(threading.Thread):
    def __init__(self, db):
        super().__init__(name="scheduler", daemon=True)
        self.db = db
        self.stop = threading.Event()
        self.running: dict[int, threading.Thread] = {}
        self.lock = threading.Lock()         # guards self.running
        self.tick_lock = threading.Lock()    # one tick at a time: the worker thread and request handlers both call tick()

    def tick(self) -> list[int]:
        """Start due jobs within the cap. Returns the run ids started."""
        with self.tick_lock:
            return self._tick()

    def _tick(self) -> list[int]:
        with self.lock:
            self.running = {rid: t for rid, t in self.running.items() if t.is_alive()}
            slots = CAP - len(self.running)
        if slots <= 0:
            return []
        due = self.db.jobs_due(db_now())
        if not due:
            return []                    # idle ticks cost nothing: no `claude auth status`, no quota lookup
        blocked = login_blocked() or quota_blocked(self.db)
        self._login_alert(blocked, len(due))
        started = []
        for job in due:
            if blocked:
                self.db.job_update(job["id"], next_run_at=(datetime.now(timezone.utc) + timedelta(minutes=DEFER_MINUTES)).isoformat(timespec="seconds"),
                                   last_status=f"deferred: {blocked}")
                continue
            if slots <= 0:
                break
            if job.get("cron"):
                self.db.job_update(job["id"], next_run_at=next_fire(job["cron"]))
            else:
                self.db.job_update(job["id"], next_run_at=None, enabled=0)  # one-off
            rid = self.db.run_start(job["id"])
            t = threading.Thread(target=self._run, args=(job, rid), name=f"job-{job['id']}", daemon=True)
            with self.lock:
                self.running[rid] = t
            t.start()
            started.append(rid)
            slots -= 1
        return started

    def _login_alert(self, blocked: str | None, due: int) -> None:
        """Push once when the box's Claude login is gone (a subscription login can expire); reset when it is back."""
        alerted = bool((self.db.kv_get(KV_LOGIN_ALERT) or {}).get("value"))
        logged_out = bool(blocked and "not logged in" in blocked)
        if logged_out and not alerted:
            notify.publish("ccboard: Claude is logged out", f"{due} scheduled run(s) are waiting. Open the board and click Log in.",
                           click=(settings.public_url + "/") if settings.public_url else None, priority=4, tags=["warning"])
            self.db.kv_set(KV_LOGIN_ALERT, True)
        elif not logged_out and alerted:
            self.db.kv_set(KV_LOGIN_ALERT, False)

    def _run(self, job: dict, rid: int) -> None:
        try:
            run_job(self.db, job, rid)
        except Exception as e:
            log.warning("job %s failed: %s", job["id"], e)
            try:
                self.db.run_finish(rid, status="error", error=str(e)[:300], result="")
            except Exception:
                pass

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as e:
                log.warning("scheduler tick failed: %s", e)
            self.stop.wait(TICK_SECONDS)
