"""Claude Code adapter: launch argv, option validation, headless runs, hooks, doctor checks.

Parity rule for v0.5.4: `launch_plan` reproduces, byte for byte, what main.api_create_session (sessions), tasks.build_command
(worktree tasks), recover.plan (resume/continue) and scheduler.build_command (headless) produced at v0.5.3b. tests/test_agents_claude.py
pins every branch as a literal.

Import rule: config, projects and claude_auth only. scheduler.py imports this module (it re-exports parse_result and LIMIT_MSG_RE),
tasks.py never imports it, and hooks.py must not be imported here (hooks v2 imports the adapter).
"""
from __future__ import annotations

import functools
import importlib.util
import json
import math
import re
import shlex
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import claude_auth, projects
from ..config import settings
from .base import Agent, Check, HookNorm, LaunchPlan, LaunchReq, OptField, SlashSpec

MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
# V19 (box check): `claude --effort ultracode -p 'say ok'`. The installed binary's --help lists low..max only, so ultracode joins
# EFFORTS only when that command is accepted on ubu2. Until then the launcher's ultracode switch is `/effort ultracode on` sent through
# POST /command right after SessionStart (v0.5.13), never through --settings (the board's own override rule blocks that).
EFFORTS = ("low", "medium", "high", "xhigh", "max")
PERMISSION_MODES = ("manual", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")   # the CLI's own names
HEADLESS_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk")   # bypassPermissions only inside a devcontainer (v0.4.5)
TOOL_RE = re.compile(r"^[A-Za-z0-9_*.:/ ()\-]{1,120}$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
WORKTREE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
MAX_APPEND = 4000
WORKTREE_DIR = ".claude/worktrees"                # where `claude --worktree <name>` puts its worktrees (tasks.WORKTREES is the same)

# Settings overrides can change the permission mode (and hooks, tools) behind the board's back: always rejected in extra args, use
# the controls instead. The bypass flags themselves are an explicit choice on interactive sessions.
OVERRIDE_PARTS = ("--settings", "--setting-sources", "--permission-prompt")
BYPASS_PARTS = ("dangerously", "bypasspermissions")
# Unattended runs: the permission mode is set by the job itself and may never be escalated through the args (any spelling).
FORBIDDEN_ARG_PARTS = ("dangerously", "bypasspermissions", "--permission-mode", "--settings", "--setting-sources",
                       "--permission-prompt", "--allow-dangerously")
TASK_REFUSAL = ("bypassPermissions (or a settings override) is not allowed for tasks; start a session and choose bypass there "
                "if you really want it")

# Notification types that mean "the session is waiting for you" (v0.5.7 drops the dead agent_needs_input / input_needed), and what
# kind of wait each is.
WAITING_NOTIFICATIONS = {"permission_prompt", "idle_prompt", "elicitation_dialog", "elicitation_url_dialog"}
WAIT_KIND = {"permission_prompt": "permission", "idle_prompt": "idle", "elicitation_dialog": "elicitation",
             "elicitation_url_dialog": "elicitation"}
# The notifications that close an elicitation wait (hooks.apply clears wait_kind only when the row was waiting on an elicitation).
ELICITATION_DONE = frozenset({"elicitation_complete", "elicitation_response"})
RESULT_MAX = 20000                                # flags.last_result (the full text of a finished turn)
KIND_MAX = 200
PATH_MAX = 1024                                   # a transcript path is stored on the row: an oversized one is dropped
# A session id is stored on the row and compared across rows: UUID-shaped in practice, but only the charset and length are enforced.
SESSION_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._:-]{0,127}$")
# SessionStart sources that start a fresh conversation (no subagent of the old one can still be running); `compact` keeps the session.
FRESH_SOURCES = ("startup", "clear", "resume", "fork")
# SessionEnd reasons followed at once by a SessionStart on the same process: the session goes on, it is not over.
CONTINUING_END = ("clear", "resume")

RATE_RE = re.compile(r"rate.?limit|usage limit|limit reached|too many requests", re.I)
# The way Claude Code words a hit limit ("You've hit your session limit · resets 10:05pm (Asia/Kathmandu)", "hit your usage limit");
# checked against the END of a result so a long run cut short mid-way is caught without flagging a run that merely discussed rate
# limiting somewhere in its summary.
LIMIT_MSG_RE = re.compile(r"hit your [^.\n]{0,30}limit|usage limit|limit (has been |was )?reached|out of (usage|credits)|"
                          r"too many requests|\brate.?limited\b", re.I)
RESET_RE = re.compile(r"resets?\s+(?:at\s+)?(?:(?P<mon>[A-Za-z]{3,9})\s+(?P<day>\d{1,2}),?\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*"
                      r"(?P<ap>[ap]m)\b(?:\s*\((?P<tz>[^)]+)\))?", re.I)


# ---------- helpers shared with scheduler.py ----------

def _arg_matching(extra: list[str], parts: tuple[str, ...]) -> str | None:
    for a in extra:
        low = a.lower()
        if any(b in low for b in parts):
            return a
    return None


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


def parse_limit_message(text: str | None, now: datetime | None = None) -> dict:
    """Classify a rate-limit message: {kind: '5h'|'7d'|'other', resets_at: epoch seconds | None}. The reset time is the next
    occurrence of the quoted wall-clock time ('resets 10:05pm (Asia/Kathmandu)', 'resets Oct 9, 3pm') in the quoted zone (the box's
    zone when none is quoted). Never raises: an unparseable time is None."""
    t = text if isinstance(text, str) else ""
    low = t.lower()
    if "session" in low or "5-hour" in low or "5 hour" in low or "five hour" in low:
        kind = "5h"
    elif "week" in low or "7-day" in low or "7 day" in low:
        kind = "7d"
    else:
        kind = "other"
    resets_at = None
    m = RESET_RE.search(t)
    if m:
        try:
            resets_at = _resolve_reset(m, now or datetime.now(timezone.utc))
        except Exception:
            resets_at = None
    return {"kind": kind, "resets_at": resets_at}


def _resolve_reset(m: re.Match, now: datetime) -> int:
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(m.group("tz").strip()) if m.group("tz") else datetime.now().astimezone().tzinfo
    local = now.astimezone(tz)
    h, minute = int(m.group("h")), int(m.group("m") or 0)
    if not (1 <= h <= 12 and 0 <= minute < 60):
        raise ValueError("bad time")
    hour = h % 12 + (12 if m.group("ap").lower() == "pm" else 0)
    if m.group("mon"):
        month = datetime.strptime(m.group("mon")[:3].title(), "%b").month
        at = datetime(local.year, month, int(m.group("day")), hour, minute, tzinfo=tz)
        if at < local - timedelta(days=1):
            at = at.replace(year=local.year + 1)
    else:
        at = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if at <= local:
            at += timedelta(days=1)
    return int(at.timestamp())


@functools.lru_cache(maxsize=1)
def _claude_settings_mod():
    """scripts/claude_settings.py is a script, not a package: load it by path (it is the one place that knows how to merge and strip
    ccboard's hook entries). Loaded once (hooks_status runs on every /api/state poll). None when the file is missing (a stripped
    checkout)."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "claude_settings.py"
    try:
        spec = importlib.util.spec_from_file_location("ccboard_claude_settings", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except (OSError, AttributeError, ImportError, SyntaxError):
        return None


# Must equal scripts/claude_settings.py EVENTS (tests/test_settings_install.py pins it): used only when that file is missing.
FALLBACK_EVENTS = ["SessionStart", "UserPromptSubmit", "Notification", "Stop", "StopFailure", "SubagentStart", "SubagentStop",
                   "PreCompact", "PostCompact", "PostModelSwitch", "TaskCreated", "TaskCompleted", "PostToolBatch", "ConfigChange",
                   "SessionEnd"]


def _is_ours(hook) -> bool:
    cmd = str(hook.get("command", "")) if isinstance(hook, dict) else ""
    return "ccboard-hook" in cmd or "ccboard-permission" in cmd


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _str(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _kind(*vals) -> str | None:
    """The first non-empty string of `vals`, capped: a hook's type/matcher/source field. A payload field of the wrong shape (a list,
    a number) is ignored instead of becoming a stored 'kind' (or raising on the set lookups)."""
    for v in vals:
        if isinstance(v, str) and v:
            return v[:KIND_MAX]
    return None


def _scalar(v, cap: int = 120):
    """A statusline value that goes on the row: bool, finite number or a short string; anything else (a dict, a list, NaN) is None."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v if math.isfinite(v) else None
    if isinstance(v, str):
        v = v.strip()
        return v[:cap] or None
    return None


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def statusline_stats(payload) -> dict:
    """The row's `stats` from one statusline payload (the JSON Claude Code feeds bin/ccboard-statusline). Every key is optional in
    the payload and every value is type-checked here (the body is untrusted): a missing or malformed key is None, never an error.
    `rate_limits` keeps its own shape (the usage page and samples.record_statusline read it) but is dropped when absurdly large."""
    p = _dict(payload)
    model, cw, cost, ws = _dict(p.get("model")), _dict(p.get("context_window")), _dict(p.get("cost")), _dict(p.get("workspace"))
    eff, thinking, pc, pr = p.get("effort"), _dict(p.get("thinking")), _dict(p.get("prompt_cache")), _dict(p.get("pr"))
    repo = ws.get("repo")
    rl = _dict(p.get("rate_limits")) or None
    if rl is not None and len(json.dumps(rl, default=str)) > 4096:
        rl = None
    cache = {k: _scalar(pc.get(k)) for k in ("warm", "hit_ratio", "expires_at", "ttl")} if pc else None
    prn = {"number": _num(pr.get("number")), "url": _scalar(pr.get("url"), 300), "review_state": _scalar(pr.get("review_state"), 40)} \
        if pr else None
    return {
        "model": _scalar(model.get("display_name")), "model_id": _scalar(model.get("id")),
        "context_pct": _scalar(cw.get("used_percentage")), "context_size": _scalar(cw.get("context_window_size")),
        "cost_usd": _scalar(cost.get("total_cost_usd")), "lines_added": _scalar(cost.get("total_lines_added")),
        "lines_removed": _scalar(cost.get("total_lines_removed")),
        "version": _scalar(p.get("version"), 40),
        "rate_limits": rl,
        "effort": _scalar(_dict(eff).get("level") if isinstance(eff, dict) else eff, 20),
        "fast": p.get("fast_mode") if isinstance(p.get("fast_mode"), bool) else None,
        "thinking": thinking.get("enabled") if isinstance(thinking.get("enabled"), bool) else None,
        "session_name": _scalar(p.get("session_name"), 120),
        "prompt_cache": cache if cache and any(v is not None for v in cache.values()) else None,
        "pr": prn if prn and any(v is not None for v in prn.values()) else None,
        "worktree": _scalar(ws.get("git_worktree") or _dict(p.get("worktree")).get("name"), 120),
        "repo": _scalar(repo.get("name") if isinstance(repo, dict) else repo, 120),
        "exceeds_200k": p.get("exceeds_200k_tokens") if isinstance(p.get("exceeds_200k_tokens"), bool) else None,
    }


class ClaudeAgent(Agent):
    name = "claude"
    label = "Claude"
    glyph = "◆"
    PERMISSION_MODES = PERMISSION_MODES
    EFFORTS = EFFORTS
    MODELS = ("opus", "fable", "sonnet", "haiku", "opusplan", "best")

    # ---- detection and auth: thin over claude_auth (it owns the 60 s cache, so no extra binary calls) ----
    def bin(self) -> str | None:
        return settings.claude_bin()

    def version(self) -> str | None:
        return claude_auth.version()

    def auth_status(self) -> dict:
        return claude_auth.status()

    def login_start(self) -> None:
        claude_auth.start_login()

    def login_state(self) -> dict:
        return claude_auth.login_state()

    def login_submit_code(self, code: str) -> None:
        claude_auth.submit_code(code)

    def logout(self) -> dict:
        return claude_auth.logout()

    # ---- option schema ----
    def option_schema(self) -> list[OptField]:
        return [
            OptField("model", "Model", "combo", list(self.MODELS), "opus",
                     "An alias or a full model id. opusplan and best are kept for completeness; [1m] variants join in v0.5.13.", "basic"),
            OptField("effort", "Effort", "select", list(EFFORTS), "high",
                     "--effort. ultracode is not offered here: it joins this list only after V19 shows the installed binary accepts "
                     "`--effort ultracode`; until then it is applied after start with /effort ultracode on.", "basic"),
            OptField("permission_mode", "Permission mode", "select", list(PERMISSION_MODES), None,
                     "--permission-mode. bypassPermissions skips every prompt: use the bypass acknowledgement, never for tasks.", "basic"),
            OptField("fast", "Fast mode", "bool", None, False,
                     "There is no CLI flag: the board sends /fast after the session starts.", "basic"),
            OptField("bypass", "Skip all permission prompts", "bool", None, False,
                     "--dangerously-skip-permissions. Needs an explicit acknowledgement; never offered for tasks, dispatch or scheduled runs, "
                     "and never stored for resume.", "advanced", True),
            OptField("allowed_tools", "Allowed tools", "textarea", None, None,
                     "--allowedTools: one pattern per line or comma separated, e.g. Bash(git *).", "advanced"),
            OptField("disallowed_tools", "Disallowed tools", "textarea", None, None, "--disallowedTools, same format.", "advanced"),
            OptField("append_system_prompt", "Append to system prompt", "textarea", None, None,
                     f"--append-system-prompt ({MAX_APPEND} characters at most).", "advanced"),
            OptField("add_dirs", "Extra directories", "dirs", None, None,
                     "--add-dir: sibling repos (project/repo) the session may read and edit.", "advanced"),
            OptField("devcontainer", "Run in the devcontainer", "bool", None, False,
                     "devcontainer up, then claude inside it (its own login).", "advanced", False, {"repo.devcontainer": True}),
            OptField("worktree", "Start in a new git worktree", "bool", None, False,
                     "claude --worktree <name>: a branch and folder of its own under .claude/worktrees.", "advanced"),
            OptField("extra", "Extra arguments", "args", None, None,
                     "Raw CLI arguments. --settings, --setting-sources and --permission-prompt* are rejected: use the controls.", "advanced"),
        ]

    # ---- validation ----
    def forbidden_extra(self, extra: list[str], *, interactive: bool, task: bool = False) -> str | None:
        """The first offending token, or None. interactive: settings overrides only. task (interactive too): overrides, then bypass
        spellings (--permission-mode itself stays allowed). Not interactive (scheduled runs): FORBIDDEN_ARG_PARTS."""
        extra = [str(a) for a in (extra or [])]
        if not interactive:
            return _arg_matching(extra, FORBIDDEN_ARG_PARTS)
        bad = _arg_matching(extra, OVERRIDE_PARTS)
        if bad or not task:
            return bad
        return _arg_matching(extra, BYPASS_PARTS)

    @staticmethod
    def _extra_list(raw) -> list[str]:
        if raw is None or raw == "":
            return []
        if isinstance(raw, str):
            try:
                return shlex.split(raw)
            except ValueError as e:
                raise projects.BadRequest(f"extra args: {e}")
        if isinstance(raw, (list, tuple)) and all(isinstance(a, str) for a in raw):
            return list(raw)
        raise projects.BadRequest("extra args must be a list of strings")

    @staticmethod
    def _tools(raw) -> list[str]:
        text = "\n".join(str(t) for t in raw) if isinstance(raw, (list, tuple)) else str(raw or "")
        tools = [t.strip() for t in re.split(r"[,\n]+", text) if t.strip()]
        for t in tools:
            if not TOOL_RE.match(t):
                raise projects.BadRequest(f"tool pattern not allowed: {t!r}")
        return tools

    def _validate(self, raw: dict | None, *, interactive: bool, tasks_or_headless: bool) -> tuple[dict, dict, list[str]]:
        """-> (full, clean, extra). `full` drives argv (it keeps permission_mode=bypassPermissions); `clean` is what may be stored and
        re-passed on resume (never a bypass, never the one-off extra args)."""
        raw = raw if isinstance(raw, dict) else {}
        extra = self._extra_list(raw.get("extra"))
        pm = raw.get("permission_mode")
        if not interactive:                                   # scheduled / headless run
            if pm and pm not in HEADLESS_MODES:
                raise projects.BadRequest(f"permission_mode must be one of {', '.join(HEADLESS_MODES)} (bypass only inside a devcontainer)")
            bad = self.forbidden_extra(extra, interactive=False)
            if bad:
                raise projects.BadRequest(f"argument not allowed for unattended runs: {bad}")
        elif tasks_or_headless:                               # a task: no bypass, no settings override
            bad = self.forbidden_extra(extra, interactive=True, task=True)
            if bad or pm == "bypassPermissions":
                raise projects.BadRequest(f"{bad or 'bypassPermissions'}: {TASK_REFUSAL}")
        else:                                                 # an interactive session
            bad = self.forbidden_extra(extra, interactive=True)
            if bad:
                raise projects.BadRequest(f"{bad}: settings overrides are not allowed in extra args; use the model / effort / permission / "
                                          "tools controls")
        full: dict = {}
        model = raw.get("model")
        if model is not None and not isinstance(model, str):
            raise projects.BadRequest("model: use an alias (fable, opus, sonnet, haiku) or a full model id")
        if model and model.strip():
            m = model.strip()
            if not MODEL_RE.match(m):
                raise projects.BadRequest("model: use an alias (fable, opus, sonnet, haiku) or a full model id")
            full["model"] = m
        effort = raw.get("effort")
        if effort:
            if effort not in EFFORTS:
                raise projects.BadRequest(f"effort must be one of {', '.join(EFFORTS)}")
            full["effort"] = effort
        if pm:
            if interactive and pm not in PERMISSION_MODES:    # a scheduled run was checked against HEADLESS_MODES above
                raise projects.BadRequest(f"permission_mode must be one of {', '.join(PERMISSION_MODES)}")
            full["permission_mode"] = pm
        for key in ("allowed_tools", "disallowed_tools"):
            tools = self._tools(raw.get(key))
            if tools:
                full[key] = tools
        asp = raw.get("append_system_prompt")
        if isinstance(asp, str) and asp.strip():
            if len(asp) > MAX_APPEND:
                raise projects.BadRequest(f"append_system_prompt is too long ({MAX_APPEND} chars max)")
            full["append_system_prompt"] = asp.strip()
        if _truthy(raw.get("fast")):
            full["fast"] = True
        if _truthy(raw.get("devcontainer")):
            full["devcontainer"] = True
        clean = {k: v for k, v in full.items() if not (k == "permission_mode" and v == "bypassPermissions")}
        return full, clean, extra

    def validate_opts(self, raw: dict | None, *, interactive: bool = True, tasks_or_headless: bool = False) -> dict:
        return self._validate(raw, interactive=interactive, tasks_or_headless=tasks_or_headless)[1]

    # ---- launching ----
    @staticmethod
    def _opt_args(full: dict) -> list[str]:
        """The launch controls as argv, in the order main._launch_args always used."""
        out: list[str] = []
        if "model" in full:
            out += ["--model", full["model"]]
        if "effort" in full:
            out += ["--effort", full["effort"]]
        if "permission_mode" in full:
            out += ["--permission-mode", full["permission_mode"]]
        for key, flag in (("allowed_tools", "--allowedTools"), ("disallowed_tools", "--disallowedTools")):
            if full.get(key):
                out += [flag, *full[key]]
        if "append_system_prompt" in full:
            out += ["--append-system-prompt", full["append_system_prompt"]]
        return out

    def launch_opt_args(self, raw: dict | None, *, interactive: bool = True, tasks_or_headless: bool = False) -> list[str]:
        """Just the launch controls as argv (what main._launch_args returned), validated with the same messages."""
        return self._opt_args(self._validate(raw, interactive=interactive, tasks_or_headless=tasks_or_headless)[0])

    def launch_plan(self, req: LaunchReq) -> LaunchPlan:
        if req.kind not in ("new", "resume", "continue"):
            raise projects.BadRequest("launch kind must be one of new, resume, continue")
        full, clean, extra = self._validate(req.opts, interactive=True, tasks_or_headless=bool(req.task))
        if req.task:
            first = req.prompt.split(None, 1)[0] if req.prompt and req.prompt.strip() else ""
            bad = self.forbidden_extra([first], interactive=True, task=True) if first.startswith("-") else None
            if req.bypass or bad:                              # a prompt that would parse as a flag must not smuggle one in
                raise projects.BadRequest(f"{bad or 'bypassPermissions'}: {TASK_REFUSAL}")
        devc = bool(clean.get("devcontainer"))
        cwd = req.cwd or ""
        if devc and not projects.has_devcontainer(Path(cwd)):
            raise projects.BadRequest("this repo has no .devcontainer/devcontainer.json")
        add_dirs = [] if devc else [str(d) for d in (req.add_dirs or [])]
        bypass = ["--dangerously-skip-permissions"] if req.bypass and not _arg_matching(extra, BYPASS_PARTS) else []
        tail = [*self._opt_args(full), *extra]
        more = ["--add-dir", *add_dirs] if add_dirs else []
        worktree = None
        agent_sid: str | None = None
        if req.kind == "new":
            agent_sid = req.session_id or str(uuid.uuid4())
            if not UUID_RE.match(agent_sid):
                raise projects.BadRequest("session id must be a UUID")
            naming = ["--name", req.session_name] if req.session_name else []
            if req.worktree:
                if not WORKTREE_RE.match(req.worktree) or ".." in req.worktree:
                    raise projects.BadRequest("worktree name: use letters, digits, '.', '_' or '-'")
                # tasks.build_command order: variadic options first, then --worktree / --session-id, `--`, the prompt last
                # (`--` so a prompt that starts with '-', a markdown bullet, is a prompt and not an unknown option)
                argv = ["claude", *bypass, *tail, *more, "--worktree", req.worktree, "--session-id", agent_sid]
                if req.prompt:
                    argv += ["--", req.prompt]
                worktree = str(Path(cwd) / WORKTREE_DIR / req.worktree)
            elif req.prompt:
                # --add-dir and --allowedTools are variadic and would swallow a trailing positional: they go first, and the
                # non-variadic --session-id / --name end the list
                argv = ["claude", *bypass, *tail, *more, "--session-id", agent_sid, *naming, "--", req.prompt]   # `--`: a prompt starting with - is a prompt
            else:
                argv = ["claude", "--session-id", agent_sid, *naming, *bypass, *tail, *more]
        else:
            if req.prompt:
                raise projects.BadRequest("a first prompt can only start a new session; resume and continue take none")
            if req.worktree:
                raise projects.BadRequest("worktree: only a new session can start in a native worktree")
            if req.kind == "resume":
                if req.resume_id and not UUID_RE.match(req.resume_id):
                    raise projects.BadRequest("resume id must be a UUID")
                agent_sid = req.resume_id or None
                argv = ["claude", "--resume", *([req.resume_id] if req.resume_id else []), *bypass, *tail, *more]
            else:
                argv = ["claude", "--continue", *bypass, *tail, *more]
        cmd_line = shlex.join(argv)
        if devc:
            # devcontainer CLI: build/start the container, then run claude inside it (its own ~/.claude; log in once there)
            cmd_line = (shlex.join(["devcontainer", "up", "--workspace-folder", cwd]) + " && "
                        + shlex.join(["devcontainer", "exec", "--workspace-folder", cwd, "--"]) + " " + cmd_line)
        return LaunchPlan(argv=argv, cmd_line=cmd_line, agent_session_id=agent_sid, cwd=cwd, opts_clean=clean, worktree=worktree)

    def resume_argv(self, session_id: str | None = None, *, name: str | None = None, opts: dict | None = None, add_dirs=()) -> list[str]:
        if session_id and not UUID_RE.match(session_id):
            raise projects.BadRequest("resume id must be a UUID")
        full = self._validate(opts, interactive=True, tasks_or_headless=False)[0]
        target = session_id or name
        argv = ["claude", "--resume", *([target] if target else []), *self._opt_args(full)]
        dirs = [str(d) for d in (add_dirs or ())]
        return argv + (["--add-dir", *dirs] if dirs else [])

    def continue_argv(self, cwd: str, opts: dict | None = None, add_dirs=()) -> list[str]:
        full = self._validate(opts, interactive=True, tasks_or_headless=False)[0]
        dirs = [str(d) for d in (add_dirs or ())]
        return ["claude", "--continue", *self._opt_args(full), *(["--add-dir", *dirs] if dirs else [])]

    def headless_argv(self, prompt: str, *, mode: str, max_turns: int, budget: float | None, extra: list[str], cwd: str | None,
                      slug: str, last_message_file: str | None) -> list[str]:
        # cwd and last_message_file are Codex's: claude -p runs in the process cwd and prints its result as JSON
        cmd = ["claude", "-p", prompt, "--worktree", slug, "--output-format", "json", "--permission-mode", mode,
               "--max-turns", str(max_turns)]
        if budget:
            cmd += ["--max-budget-usd", f"{budget:.2f}"]
        return cmd + list(extra)

    def parse_headless(self, stdout: str, stderr: str, rc: int) -> dict:
        res = parse_result(stdout or "")
        if rc != 0 and not res["text"]:
            res["text"] = (stderr or "").strip()[-4000:]
        if not res["rate_limited"] and rc != 0 and RATE_RE.search(stderr or ""):
            res["rate_limited"] = True                       # the CLI reported the limit on stderr and gave up
        return res

    # ---- hooks ----
    @staticmethod
    def _settings_file() -> Path:
        return settings.claude_config_dir / "settings.json"

    def _required_events(self) -> list[str]:
        mod = _claude_settings_mod()
        events = getattr(mod, "EVENTS", None)
        return list(events) if isinstance(events, list) and events else list(FALLBACK_EVENTS)

    def _read_settings(self, strict: bool) -> dict:
        p = self._settings_file()
        try:
            text = p.read_text()
        except FileNotFoundError:
            return {}
        except OSError as e:
            if strict:
                raise RuntimeError(f"{p} cannot be read ({e.__class__.__name__}); fix it first")
            return {}
        try:
            data = json.loads(text or "{}")
            if not isinstance(data, dict):
                raise ValueError("the top level is not an object")
        except ValueError as e:
            if strict:
                raise RuntimeError(f"{p} is not valid JSON ({e}); fix it first")
            return {}
        return data

    def hooks_status(self) -> dict:
        data = self._read_settings(strict=False)
        hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
        present = []
        for ev, groups in hooks.items():
            if not isinstance(groups, list):
                continue
            if any(_is_ours(h) for g in groups if isinstance(g, dict) for h in (g.get("hooks") or []) if isinstance(h, dict)):
                present.append(ev)
        required = self._required_events()
        events = [e for e in required if e in present] + sorted(e for e in present if e not in required)
        return {"installed": all(e in present for e in required), "events": events, "trust": None}

    def install_hooks(self, app_dir: Path, *, remote_approve: bool = True, approve_timeout: int = 90) -> None:
        mod = _claude_settings_mod()
        if mod is None:
            raise RuntimeError("scripts/claude_settings.py is missing from this checkout")
        data = self._read_settings(strict=True)
        new = mod.install(data, Path(app_dir).resolve(), remote_approve, int(approve_timeout))
        mod.save(self._settings_file(), new)

    def uninstall_hooks(self) -> None:
        mod = _claude_settings_mod()
        if mod is None:
            raise RuntimeError("scripts/claude_settings.py is missing from this checkout")
        if not self._settings_file().exists():
            return
        mod.save(self._settings_file(), mod.strip_ours(self._read_settings(strict=True)))

    def normalise_hook(self, event: str, payload: dict) -> HookNorm:
        """One hook payload in the board's terms; hooks.apply feeds EVERY event through it (Stop's screen-line fallback, the
        last_prompt hygiene and the rules that need the row stay in apply). Malformed payloads never raise: a field of the wrong shape
        is ignored. Besides the state it patches the row's flags: hook_seen, transcript_path, wait_kind, compacting, last_result,
        resumed and the subagent counter (`incr`)."""
        p = payload if isinstance(payload, dict) else {}
        if event == "statusline":
            return HookNorm(event=event, ignored="statusline")           # not a hook: hooks.apply handles it before this
        n = HookNorm(event=event)
        n.flags["hook_seen"] = True
        tp = p.get("transcript_path")
        if isinstance(tp, str) and tp and len(tp) <= PATH_MAX:
            n.flags["transcript_path"] = tp
        if event == "SessionStart":
            n.kind = _kind(p.get("source"), p.get("matcher"))
            n.state = None if n.kind == "compact" else "idle"             # a compaction is not a new session: a turn may go on after it
            # a resume says how long the conversation sat and how big it is (the prompt cache is probably cold): the row keeps it
            since, tokens, cold = _num(p.get("seconds_since_last_response")), _num(p.get("context_tokens")), p.get("prompt_cache_likely_expired")
            if since is not None or tokens is not None:
                n.flags["resumed"] = {k: v for k, v in (("context_tokens", tokens), ("since_s", since),
                                                        ("cache_cold", cold if isinstance(cold, bool) else None)) if v is not None}
            else:
                n.flags["resumed"] = None
            if n.kind is None or n.kind in FRESH_SOURCES:
                n.flags["subagents"] = None                               # a new conversation: counts from the old one are stale
            n.flags["compacting"] = None
        elif event == "UserPromptSubmit":
            n.state, n.prompt = "working", p.get("prompt") if isinstance(p.get("prompt"), str) else None
        elif event == "Notification":
            n.kind = _kind(p.get("notification_type"), p.get("type"), p.get("matcher"))
            n.message = p.get("message") if isinstance(p.get("message"), str) else None
            if n.kind in WAITING_NOTIFICATIONS:
                n.state, n.attention = "waiting", True
                n.flags["wait_kind"] = WAIT_KIND[n.kind]
        elif event == "Stop":
            last = _str(p.get("last_assistant_message"))
            last = last[:RESULT_MAX] if last else None
            n.state, n.attention, n.message, n.result = "done", True, last, last
            n.flags["last_result"] = last                                 # None deletes a previous turn's text
            n.flags["compacting"] = None
        elif event == "StopFailure":
            # live shape: {"error": "rate_limit", "last_assistant_message": "You've hit your session limit · resets 10:05pm (...)"}:
            # the TYPE is `error`. Older shapes carry error_type / error_category / matcher with `error` as the text.
            typ = _kind(p.get("error_type"), p.get("error_category"), p.get("matcher"))
            err = p.get("error")
            kind = typ or (_kind(err) or "error")
            legacy = (err if typ else None) or p.get("message")
            last = _str(p.get("last_assistant_message"))
            n.state, n.attention, n.kind = "errored", True, kind
            n.message = last or (legacy if isinstance(legacy, str) and legacy else kind)
            n.result = last
            n.flags["last_result"] = None                                 # a failed turn has no result
            n.flags["compacting"] = None
            if ("rate" in kind.lower() and "limit" in kind.lower()) or LIMIT_MSG_RE.search(n.message[-500:]):
                n.limit = {**parse_limit_message(n.message), "message": n.message}
        elif event == "SessionEnd":
            n.kind = _kind(p.get("reason"), p.get("matcher"))
            # /clear and /resume end the conversation and start the next one in the same process: the state must not flap to 'ended'
            n.state = None if n.kind in CONTINUING_END else "ended"
            n.flags["subagents"] = None
            n.flags["compacting"] = None
        elif event == "SubagentStart":
            n.incr["subagents"], n.kind = 1, _kind(p.get("matcher"))
        elif event == "SubagentStop":
            n.incr["subagents"], n.kind = -1, _kind(p.get("matcher"))
        elif event in ("PreCompact", "PostCompact"):
            n.flags["compacting"] = event == "PreCompact"
            n.kind = _kind(p.get("trigger"), p.get("matcher"))
        else:
            n.kind = _kind(p.get("matcher"))
        if n.state and n.state != "waiting":
            n.flags["wait_kind"] = None                                   # whatever it waited for is over
        return n

    # ---- data sources ----
    def transcript_path(self, row_or_payload: dict | None) -> Path | None:
        x = row_or_payload if isinstance(row_or_payload, dict) else {}
        flags = x.get("flags")
        if isinstance(flags, str):
            try:
                flags = json.loads(flags)
            except ValueError:
                flags = None
        given = x.get("transcript_path") or (flags.get("transcript_path") if isinstance(flags, dict) else None)
        if given:
            return self._under_config(given)
        sid = x.get("session_id") or x.get("agent_session_id") or x.get("claude_session_id")
        cwd = x.get("cwd")
        if isinstance(sid, str) and UUID_RE.match(sid) and isinstance(cwd, str) and cwd:
            f = settings.claude_config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{sid}.jsonl"
            return f if f.is_file() else None
        return None

    @staticmethod
    def _under_config(p) -> Path | None:
        """A transcript path from a hook payload is only trusted inside the Claude config dir."""
        if not isinstance(p, str) or not p.endswith(".jsonl"):
            return None
        try:
            Path(p).resolve().relative_to(settings.claude_config_dir.resolve())
        except (ValueError, OSError, RuntimeError):
            return None
        return Path(p)

    def usage_sources(self) -> list[str]:
        return ["statusline", "ccusage"]

    def cost_join_key(self, ccusage_row: dict | None) -> str | None:
        row = ccusage_row if isinstance(ccusage_row, dict) else {}
        sid = str(row.get("period") or row.get("sessionId") or "")
        return sid.lower() if re.fullmatch(r"[0-9a-fA-F-]{36}", sid) else None

    # ---- control surface ----
    def slash_commands(self) -> dict[str, SlashSpec]:
        # weights are measured use counts from 8,605 prompts (plan, usage analysis F6); verified flips per command after V8
        rows = [("clear", "Clear", False, False, 155, True), ("compact", "Compact", False, False, 120, False),
                ("usage", "Usage", False, True, 100, False), ("effort", "Effort", True, False, 58, False),
                ("model", "Model", True, False, 39, False), ("rename", "Rename", True, False, 10, False),
                ("context", "Context", False, True, 9, False), ("status", "Status", False, True, 3, False),
                ("cost", "Cost", False, True, 0, False), ("fast", "Fast", False, False, 0, False)]
        return {k: SlashSpec(cmd="/" + k, label=label, arg=arg, read=read, verified=False, weight=w, destructive=d)
                for k, label, arg, read, w, d in rows}

    def exit_command(self) -> str:
        return "/exit"

    def worktree_strategy(self) -> str:
        return "native"

    def mcp_register_cmd(self, python: str, script: str, env: dict | None) -> list[str]:
        # install.sh: claude mcp add --scope user ccboard -- <python> <script>. -e is variadic: it goes after the name and before `--`.
        # Never put the hook token or any secret in env.
        pairs = [x for k, v in sorted((env or {}).items()) for x in ("-e", f"{k}={v}")]
        return ["claude", "mcp", "add", "--scope", "user", "ccboard", *pairs, "--", python, script]

    # ---- doctor ----
    def doctor_checks(self) -> list[Check]:
        out: list[Check] = []
        exe = self.bin()
        if not exe:
            out.append(Check("claude-bin", "claude", "Claude Code CLI", "fail", "claude is not on PATH or in ~/.local/bin",
                             {"text": "Install Claude Code: curl -fsSL https://claude.ai/install.sh | bash",
                              "cmd": "curl -fsSL https://claude.ai/install.sh | bash"}))
            out.append(Check("claude-auth", "claude", "Claude login", "skip", "claude is not installed"))
            out.append(Check("claude-hooks", "claude", "Claude hooks", "skip", "claude is not installed"))
            return out
        ver = self.version()
        if ver:
            out.append(Check("claude-bin", "claude", "Claude Code CLI", "pass", f"{exe} ({ver})"))
        else:
            out.append(Check("claude-bin", "claude", "Claude Code CLI", "warn", f"{exe} (version unknown)",
                             {"text": "`claude --version` printed nothing; run claude once in a shell to see why."}))
        st = self.auth_status() or {}
        if st.get("loggedIn"):
            bits = [str(st[k]) for k in ("authMethod", "email", "subscriptionType") if st.get(k)]
            out.append(Check("claude-auth", "claude", "Claude login", "pass", " · ".join(bits) or "logged in"))
        elif st.get("error"):
            out.append(Check("claude-auth", "claude", "Claude login", "warn", str(st["error"]),
                             {"text": "The login state could not be read; try again, or run `claude auth status` on the box.",
                              "cmd": "claude auth status"}))
        else:
            out.append(Check("claude-auth", "claude", "Claude login", "fail", "not logged in",
                             {"text": "Log in from Settings > Agents, or run `claude auth login` on the box.",
                              "cmd": "claude auth login", "action": "claude_login"}))
        hs = self.hooks_status()
        required = self._required_events()
        missing = [e for e in required if e not in hs["events"]]
        fix = {"text": "Re-run ./install.sh from the ccboard checkout: it merges ccboard's hooks into ~/.claude/settings.json.",
               "cmd": "python3 scripts/claude_settings.py install --app-dir ."}
        if hs["installed"]:
            out.append(Check("claude-hooks", "claude", "Claude hooks", "pass", f"{len(hs['events'])} events registered"))
        elif hs["events"]:
            out.append(Check("claude-hooks", "claude", "Claude hooks", "warn", "missing: " + ", ".join(missing), fix))
        else:
            out.append(Check("claude-hooks", "claude", "Claude hooks", "fail", "no ccboard hooks in settings.json", fix))
        return out
