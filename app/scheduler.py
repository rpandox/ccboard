"""Durable scheduler: cron (or one-off) headless runs (`claude -p`, or `codex exec --json` since v0.5.16), each in a fresh worktree, with a
concurrency cap and quota awareness per agent. Results become task cards (worktree + branch), resumable in a terminal."""
from __future__ import annotations

import json
import logging
import shlex
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from croniter import croniter

from . import agents, claude_auth, notify, projects, tasks
from .agents import codex as codex_agent
from .agents.claude import FORBIDDEN_ARG_PARTS, HEADLESS_MODES, LIMIT_MSG_RE, RATE_RE, parse_result   # noqa: F401 (re-exported: they live in the adapter now)
from .config import settings
from .db import now as db_now

log = logging.getLogger("ccboard.scheduler")
TICK_SECONDS = 30
CAP = 2                    # concurrent headless runs
QUOTA_MAX_PCT = 85.0       # skip launching when the 5-hour window is above this
DEFER_MINUTES = 15
RUN_TIMEOUT = 3600
MODES = HEADLESS_MODES     # bypassPermissions only inside a devcontainer (v0.4.5); defined in agents/claude.py with the rest of the argv rules
BACKOFF_MINUTES = 30       # after a run comes back rate-limited, defer everything this long (or until the window resets)
BACKOFF_MAX_HOURS = 5
KV_BACKOFF = "sched_backoff_until"
KV_LOGIN_ALERT = "sched_login_alerted"
KV_RATE_CODEX = "rate_limits_codex"                    # agents/codex_rollout.py: {value: {primary: {used_percent, window_minutes, resets_at}, secondary, ...}, at}
# Per-agent keys: Claude keeps the v0.4 names, Codex gets its own so one agent's limit or logout never parks the other's runs.
KV_BACKOFF_BY_AGENT = {"claude": KV_BACKOFF, "codex": "sched_backoff_until_codex"}
KV_LOGIN_ALERT_BY_AGENT = {"claude": KV_LOGIN_ALERT, "codex": "sched_login_alerted_codex"}
AGENT_LABEL = {"claude": "Claude", "codex": "Codex"}


# Unattended-run argument rules per agent. FORBIDDEN_ARG_PARTS (above, re-exported) is Claude's list of substrings, any spelling. Codex's
# are the substrings dangerously / yolo / bypass plus exact tokens that change configuration or the model's profile behind the board's
# back (-c, --config, -p, --profile, --enable, --disable, --strict-config) and anything --remote*. app/agents/codex.py enforces them
# through forbidden_extra(); these are the mirror (tests pin both against each other).
CODEX_FORBIDDEN_PARTS = codex_agent.FORBIDDEN_ARG_PARTS            # ("dangerously", "yolo", "bypass"): one list, the adapter's
CODEX_FORBIDDEN_EXACT = ("-c", "--config", "-p", "--profile", "--enable", "--disable", "--strict-config")
CODEX_FORBIDDEN_PREFIX = ("--remote",)
FORBIDDEN_ARG_PARTS_BY_AGENT = {"claude": FORBIDDEN_ARG_PARTS, "codex": CODEX_FORBIDDEN_PARTS}


def forbidden_arg(extra: list[str], agent: str = "claude") -> str | None:
    """The first argument an unattended run of `agent` may not carry, by the scheduler's own mirror of the rules (None when clean).
    check_extra_args asks the adapter; this is the adapter-free check, for callers that must not import it and for the tests."""
    parts = FORBIDDEN_ARG_PARTS_BY_AGENT.get(agent, FORBIDDEN_ARG_PARTS)
    for a in extra:
        low = str(a).lower()
        if any(p in low for p in parts):
            return a
        tok = str(a)
        if agent == "codex" and (tok in CODEX_FORBIDDEN_EXACT or low.startswith(CODEX_FORBIDDEN_PREFIX)
                                 or any(tok.startswith(x + "=") for x in CODEX_FORBIDDEN_EXACT if x.startswith("--"))
                                 or (tok.startswith(("-c", "-p")) and not tok.startswith("--"))):   # -cmodel=x: a short flag with its value attached
            return a
    return None


def check_extra_args(args: str | None, agent: str = "claude") -> list[str]:
    """Extra CLI args for unattended runs: the permission mode is set by the job itself and may never be
    escalated through the args (any spelling, '=' forms, settings overrides). The rules live in the agent adapter."""
    if not args:
        return []
    try:
        parts = shlex.split(args)
    except ValueError as e:
        raise ValueError(f"args: {e}")
    bad = agents.get(agent).forbidden_extra(parts, interactive=False) or forbidden_arg(parts, agent)
    if bad:
        raise ValueError(f"argument not allowed for unattended runs: {bad}")
    return parts


def valid_cron(expr: str) -> bool:
    try:
        return croniter.is_valid(expr)
    except Exception:
        return False


def next_fire(expr: str, base: datetime | None = None) -> str:
    base = base or datetime.now(timezone.utc)
    return croniter(expr, base).get_next(datetime).astimezone(timezone.utc).isoformat(timespec="seconds")


def _codex_window(db, now: float | None = None) -> tuple[float | None, object]:
    """(used percent, resets_at epoch) of the Codex window the guard watches: kv rate_limits_codex `primary`, or a secondary window that
    has reached 100 %. A window whose reset time has passed has rolled over and is not a reading any more. (None, None) when unknown."""
    val = ((db.kv_get(KV_RATE_CODEX) or {}).get("value")) or {}
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    best: tuple[float | None, object] = (None, None)
    for key in ("primary", "secondary"):
        w = val.get(key) if isinstance(val, dict) else None
        if not isinstance(w, dict):
            continue
        try:
            pct = float(w.get("used_percent"))
        except (TypeError, ValueError):
            continue
        try:
            if w.get("resets_at") is not None and float(w["resets_at"]) <= now:
                continue
        except (TypeError, ValueError):
            pass
        if key == "primary" or pct >= 100:
            if best[0] is None or pct > best[0]:
                best = (pct, w.get("resets_at"))
    return best


def quota_state(db, agent: str = "claude") -> dict:
    """What the scheduler knows about the agent's usage window: pct is None until a reading exists (Claude: an interactive session's
    statusline, headless runs never report; Codex: the rollout tailer's rate_limits_codex), plus any back-off set after a rate-limited
    run of that agent."""
    if agent == "codex":
        rl = db.kv_get(KV_RATE_CODEX)
        pct, resets_at = _codex_window(db)
    else:
        rl = db.kv_get("rate_limits")
        pct = resets_at = None
        try:
            w = ((rl or {}).get("value") or {}).get("five_hour") or {}
            pct = float(w.get("used_percentage"))
            resets_at = w.get("resets_at")
        except (TypeError, ValueError, AttributeError):
            pct = None
    until = ((db.kv_get(KV_BACKOFF_BY_AGENT.get(agent, KV_BACKOFF)) or {}).get("value")) or None
    if until and until <= db_now():
        until = None
    return {"pct": pct, "at": (rl or {}).get("at"), "resets_at": resets_at, "backoff_until": until, "known": pct is not None}


def quota_blocked(db, agent: str = "claude") -> str | None:
    q = quota_state(db, agent)
    if q["backoff_until"]:
        return f"backing off until {q['backoff_until']} after a rate-limited run"
    if q["pct"] is not None and q["pct"] >= QUOTA_MAX_PCT:
        return (f"Codex usage window at {q['pct']:.0f}% (limit {QUOTA_MAX_PCT:.0f}%)" if agent == "codex"
                else f"5-hour window at {q['pct']:.0f}% (limit {QUOTA_MAX_PCT:.0f}%)")
    # An unknown reading (no interactive session yet) does not block: a headless-only box would otherwise never
    # run anything. The UI shows "quota unknown", and a rate-limited run triggers set_backoff().
    return None


def login_blocked(agent: str = "claude") -> str | None:
    """Headless runs use the box's own login for the agent (Claude: a subscription login, on a box without an API key; Codex: the ChatGPT
    login in $CODEX_HOME). While it is missing or expired every run would fail at once, so defer instead and let the board say 'Log in'."""
    if agent == "codex":
        st = agents.get("codex").auth_status() or {}
        if not st.get("installed"):
            return "codex is not installed on this box"
        if not st.get("loggedIn"):
            return "codex is not logged in on this box (open Settings and log in)"
        return None
    st = claude_auth.status()
    if not st.get("installed"):
        return "claude is not installed on this box"
    if not st.get("loggedIn"):
        return "claude is not logged in on this box (click Log in on the board)"
    return None


def set_backoff(db, resets_at=None, agent: str = "claude") -> str:
    """Defer the agent's runs for BACKOFF_MINUTES, or until its window resets when we were told when (capped)."""
    now_dt = datetime.now(timezone.utc)
    until = now_dt + timedelta(minutes=BACKOFF_MINUTES)
    try:
        r = datetime.fromtimestamp(float(resets_at), tz=timezone.utc)
        if until < r <= now_dt + timedelta(hours=BACKOFF_MAX_HOURS):
            until = r
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    s = until.isoformat(timespec="seconds")
    db.kv_set(KV_BACKOFF_BY_AGENT.get(agent, KV_BACKOFF), s)
    return s


def build_command(prompt: str, slug: str, mode: str, max_turns: int, budget: float | None, extra: list[str]) -> list[str]:
    return agents.get("claude").headless_argv(prompt, mode=mode, max_turns=max_turns, budget=budget, extra=extra, cwd=None,
                                              slug=slug, last_message_file=None)


def job_opts(job: dict) -> dict:
    """The agent's own options a job stores as JSON in jobs.opts (Codex: model, reasoning_effort); {} when there are none or the text is bad."""
    raw = job.get("opts")
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def codex_command(job: dict, wt: Path, out_file: Path, extra: list[str]) -> list[str]:
    """`codex exec --json -s <sandbox> [-a never] -o <out_file> [-m model] [-c model_reasoning_effort=...] [extra] --skip-git-repo-check -- <prompt>`.
    The sandbox comes from the job's permission mode (plan = read-only, everything else workspace-write; a headless run never bypasses and
    never prompts); the working directory is the managed worktree, set on the subprocess, so no -C. Never --ephemeral: the rollout stays
    on disk and `codex resume <thread id>` opens it."""
    extra = list(extra)
    if "--skip-git-repo-check" not in extra:
        extra.append("--skip-git-repo-check")              # a worktree is a repo; the flag only removes a way for the run to refuse to start
    return agents.get("codex").headless_argv(job["prompt"], mode=job.get("permission_mode") or "acceptEdits", max_turns=0, budget=None, extra=extra,
                                             cwd=None, slug="", last_message_file=str(out_file), opts=job_opts(job))


def _last_message(path: Path) -> str:
    """The -o file: Codex writes the final agent message there, so it is the authoritative result (the JSON stream's last agent_message is the
    fallback). '' when the run did not get as far as writing one."""
    try:
        return path.read_text(errors="replace").strip()[:20000]
    except OSError:
        return ""


def _finish(db, job: dict, run_id: int, summary: dict, rpath: Path, slug: str, stamp: str, wt: Path | None, agent: str) -> dict:
    """The tail every run shares: the worktree it left becomes a task card (agent, branch, worktree, the agent's session id), the run row
    is closed and the job's last status stored."""
    task_id = None
    if wt is not None and wt.is_dir():
        if agent == "claude":
            # unlock the worktree -p left locked so archive/merge can remove it later
            subprocess.run(["git", "-C", str(rpath), "worktree", "unlock", str(wt)], capture_output=True, timeout=10)
        tmux_name = f"{job['project']}--{job['repo']}--j-{slug}"
        title = f"[{job['name']}] {stamp}"
        task_id = db.task_add(project=job["project"], repo=job["repo"], slug=slug, title=title, prompt=job["prompt"],
                              branch=f"worktree-{slug}", base=tasks.default_branch(rpath), worktree=str(wt),
                              tmux_name=tmux_name, claude_session_id=summary.get("session_id"), agent=agent)
        if summary.get("cost_usd") is not None:
            db.task_update(task_id, cost_usd=summary["cost_usd"])
    have = wt is not None and wt.is_dir()
    db.run_finish(run_id, **summary, worktree=str(wt) if have else None, branch=f"worktree-{slug}" if have or agent == "claude" else None,
                  task_id=task_id)
    db.job_update(job["id"], last_status=summary["status"], last_run_at=db_now())
    log.info("job %s run %s (%s): %s", job["id"], run_id, agent, summary["status"])
    return summary


def run_job(db, job: dict, run_id: int) -> dict:
    """Execute one run synchronously (called in a worker thread). Returns the run summary. Claude: `claude -p --worktree <slug>` (Claude makes
    the worktree); Codex: a managed worktree, then `codex exec --json` in it."""
    agent = job.get("agent") or "claude"
    rpath = projects.repo_path(job["project"], job["repo"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    slug = f"{tasks.slugify(job['name'])[:30]}-{stamp}".strip("-")
    slug = tasks.unique_slug(rpath, slug, db.task_slugs(job["project"], job["repo"]))
    try:
        extra = check_extra_args(job.get("args"), agent)
    except (ValueError, KeyError):
        extra = []
    summary: dict = {"status": "error", "error": None}
    timeout = int(job.get("timeout_s") or RUN_TIMEOUT)
    if agent == "codex":
        ag = agents.get("codex")
        wt = None
        ran = False
        try:
            tasks.ensure_excluded(rpath)
            wt = tasks.create_managed_worktree(rpath, slug, tasks.default_branch(rpath), "codex")
            tasks.apply_worktreeinclude(rpath, wt)
            out_file = Path(settings.data_dir) / "runs" / f"{run_id}.txt"
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.unlink(missing_ok=True)
            cmd = codex_command(job, wt, out_file, extra)
            exe = ag.bin()
            if exe:
                cmd[0] = exe
            env = {**codex_agent._child_env(), "CCBOARD_SESSION": "none"}      # the hook resolver ignores this run: it is not a board session
            cp = subprocess.run(cmd, cwd=str(wt), capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
            ran = True
            res = ag.parse_headless(cp.stdout, cp.stderr, cp.returncode)
            last = _last_message(out_file)
            status = "rate_limited" if res["rate_limited"] else ("error" if res["is_error"] or cp.returncode != 0 else "ok")
            if status == "rate_limited":
                log.warning("codex job %s hit a rate limit; deferring its runs until %s", job["id"],
                            set_backoff(db, quota_state(db, "codex").get("resets_at"), "codex"))
            summary.update(status=status, result=last or res["text"], session_id=res["session_id"], cost_usd=None, num_turns=res["turns"],
                           error=None if status == "ok" else (res["subtype"] or f"exit {cp.returncode}"))
        except subprocess.TimeoutExpired:
            summary.update(status="error", error="timed out", result="")
        except tasks.WorktreeError as e:
            summary.update(status="error", error=f"could not make the worktree: {e}"[:300], result="")
        except (OSError, projects.BadRequest) as e:
            summary.update(status="error", error=str(e)[:300], result="")
            if not ran and wt is not None:                  # codex never started (not executable, bad options): no card for an empty worktree
                tasks.discard_managed_worktree(rpath, slug, wt)
                wt = None
        return _finish(db, job, run_id, summary, rpath, slug, stamp, wt, "codex")
    exe = settings.claude_bin()
    cmd = build_command(job["prompt"], slug, job.get("permission_mode") or "acceptEdits", int(job.get("max_turns") or 30),
                        job.get("max_budget_usd"), extra)
    if exe:
        cmd[0] = exe
    try:
        tasks.ensure_excluded(rpath)
        cp = subprocess.run(cmd, cwd=str(rpath), capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        res = agents.get("claude").parse_headless(cp.stdout, cp.stderr, cp.returncode)
        status = "rate_limited" if res["rate_limited"] else ("error" if res["is_error"] or cp.returncode != 0 else "ok")
        if status == "rate_limited":
            log.warning("job %s hit a rate limit; deferring all runs until %s", job["id"], set_backoff(db, quota_state(db).get("resets_at")))
        summary.update(status=status, result=res["text"], session_id=res["session_id"], cost_usd=res["cost"],
                       num_turns=res["turns"], error=None if status == "ok" else (res["subtype"] or f"exit {cp.returncode}"))
    except subprocess.TimeoutExpired:
        summary.update(status="error", error="timed out", result="")
    except (OSError, projects.BadRequest) as e:
        summary.update(status="error", error=str(e)[:300], result="")
    return _finish(db, job, run_id, summary, rpath, slug, stamp, tasks.worktree_path(rpath, slug), "claude")


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
        gates: dict[str, str | None] = {}

        def blocked_for(agent: str) -> str | None:
            if agent not in gates:
                try:
                    gates[agent] = login_blocked(agent) or quota_blocked(self.db, agent)
                except KeyError:
                    gates[agent] = f"unknown agent {agent!r}"
            return gates[agent]
        for agent in sorted({j.get("agent") or "claude" for j in due}):
            self._login_alert(blocked_for(agent), sum(1 for j in due if (j.get("agent") or "claude") == agent), agent)
        started = []
        for job in due:
            blocked = blocked_for(job.get("agent") or "claude")
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

    def _login_alert(self, blocked: str | None, due: int, agent: str = "claude") -> None:
        """Push once when the box's login for the agent is gone (a subscription login can expire); reset when it is back."""
        key = KV_LOGIN_ALERT_BY_AGENT.get(agent, KV_LOGIN_ALERT)
        alerted = bool((self.db.kv_get(key) or {}).get("value"))
        logged_out = bool(blocked and "not logged in" in blocked)
        name = AGENT_LABEL.get(agent, agent)
        if logged_out and not alerted:
            notify.publish(f"ccboard: {name} is logged out", f"{due} scheduled run(s) are waiting. Open the board and log in to {name}.",
                           click=(settings.public_url + "/") if settings.public_url else None, priority=4, tags=["warning"])
            self.db.kv_set(key, True)
        elif not logged_out and alerted:
            self.db.kv_set(key, False)

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
