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
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

from .. import claude_auth, projects
from ..config import settings
from .base import Agent, Check, HookNorm, LaunchPlan, LaunchReq, OptField, SlashSpec
from .pickers import PROVEN

MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}(\[1m\])?$")           # an alias or id, optionally the 1M-context variant (opus[1m])
# V19 (box check): `claude --effort ultracode -p 'say ok'`. The installed binary's --help lists low..max only, so ultracode joins
# the accepted efforts only when this box's claude takes it: its `--help` names it on the --effort option, or CCBOARD_CLAUDE_ULTRACODE_FLAG=1
# records that V19 passed (0 records that it did not). Until then the launcher's ultracode switch is `/effort ultracode on` sent through
# POST /command right after SessionStart, never through --settings (the board's own override rule blocks that).
EFFORTS = ("low", "medium", "high", "xhigh", "max")
ULTRACODE = "ultracode"
# What `/effort` accepts typed inline (2.1.290: "Valid options are: low, medium, high, xhigh, max, auto, ultracode [on|off]"). Ultracode is
# a setting, not a level: `/effort ultracode` (or `... on`) turns it on and `/effort ultracode off` off, both for this session only and
# without touching the effort level (the docs: needs v2.1.284 or newer). The levels typed inline also save the default (see slash_commands).
ULTRACODE_ON, ULTRACODE_OFF = "ultracode on", "ultracode off"
EFFORT_ARGS = EFFORTS + ("auto", ULTRACODE, ULTRACODE_ON, ULTRACODE_OFF)
ULTRACODE_ENV = "CCBOARD_CLAUDE_ULTRACODE_FLAG"
PERMISSION_MODES = ("manual", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")   # the CLI's own names
# The launcher's mode names (the same five words the Codex picker uses) as Claude permission modes: `custom` is Codex's sandbox + approval pair.
MODE_ALIASES = {"default": "manual", "read-only": "plan", "bypass": "bypassPermissions"}
LAUNCH_KINDS = ("new", "resume", "continue", "from_pr")                  # LaunchReq.kind; from_pr is `claude --from-pr <n|url>`


# ---------- version gates (issue #113): what the Claude Code docs say needs a newer Claude Code ----------
# Dated evidence, not a permanent default: every row was read from the Claude Code documentation on 2026-10-07 and none was run against an old
# binary. Where a `--help` probe exists (ultracode: capabilities()) the probe wins over the number. Claude Code updates itself, so the answer
# changes over time. `docs` is the page family the row came from (CLI reference, model configuration, permission modes, sub-agents).
class Gate(NamedTuple):
    key: str
    feature: str            # the short words the doctor and the launcher use
    min_version: str
    what_breaks: str        # plain words: what happens on an older Claude Code
    docs: str
    read: str = "2026-10-07"


FEATURE_GATES: tuple[Gate, ...] = (
    Gate("ultracode_effort", "--effort ultracode", "2.1.203", "the flag is rejected (the launcher then applies it after start with /effort ultracode on)", "Model configuration"),
    Gate("ultracode_off", "/effort ultracode off", "2.1.284", "ultracode cannot be switched off inside a session", "Model configuration"),
    Gate("permission_prompts_none", "--permission-prompts none", "2.1.259", "headless runs cannot turn permission prompts off", "CLI reference"),
    Gate("subagent_model_force", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "2.1.257", "the subagent model variable is ignored", "Sub-agents"),
    Gate("manual_mode", "the manual permission mode name", "2.1.200", "--permission-mode manual is rejected (the board sends default)", "Permission modes"),
    Gate("opusplan_1m", "/model opusplan[1m]", "2.1.265", "the opusplan[1m] model alias is unknown", "Model configuration"),
    Gate("mcp_sse_fallback", "SSE fallback for HTTP MCP servers", "2.1.265", "an HTTP MCP server that only speaks SSE fails to connect", "CLI reference"),
    Gate("auto_start_mode", "auto as the starting permission mode", "2.1.283", "--permission-mode auto is rejected in an interactive session", "Permission modes"),
)
GATES_BY_KEY = {g.key: g for g in FEATURE_GATES}
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(text) -> tuple[int, int, int] | None:
    """'2.1.288', '2.1.288 (Claude Code)', 'claude 2.1.288' -> (2, 1, 288); '2.1' -> (2, 1, 0); anything without a dotted number -> None."""
    m = _VERSION_RE.search(text if isinstance(text, str) else "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else None


def gate_met(key: str, version) -> bool | None:
    """Is the gate `key` met by `version` (a string or a parsed tuple)? None when the version cannot be read: an unknown version never blocks."""
    have = version if isinstance(version, tuple) else parse_version(version)
    if have is None:
        return None
    return have >= parse_version(GATES_BY_KEY[key].min_version)


def unmet_gates(version) -> list[Gate]:
    """The gates an installed `version` is too old for, in table order ([] when it is new enough or unknown)."""
    return [g for g in FEATURE_GATES if gate_met(g.key, version) is False]


def gate_message(key: str, version) -> str:
    g = GATES_BY_KEY[key]
    have = ".".join(map(str, parse_version(version) or ())) if not isinstance(version, tuple) else ".".join(map(str, version))
    return f"{g.feature} needs Claude Code {g.min_version} or newer (this box has {have}): {g.what_breaks}"
CAPS_TIMEOUT = 5.0                                # `claude --help` is read once per binary; a failed probe is retried after FAIL_TTL
FAIL_TTL = 60.0
PR_RE = re.compile(r"^(#?\d{1,7}|https://[A-Za-z0-9.-]{1,100}(:\d{1,5})?/[A-Za-z0-9._~%@:+/-]{1,300})$")   # a PR number or its URL
AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,79}$")      # --agent <name> (a subagent definition)
AUTOCOMPACT_RE = re.compile(r"^(auto|\d{4,9})$")                         # --autocompact auto | <tokens>
MAX_FALLBACKS = 3                                 # --fallback-model takes a chain, capped at three
MAX_MCP_PATH = 400
HEADLESS_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk")   # bypassPermissions only inside a devcontainer (v0.4.5)
TOOL_RE = re.compile(r"^[A-Za-z0-9_*.:/ ()\-]{1,120}$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
WORKTREE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")              # \Z: names a directory, so no trailing newline (#44 F-03)
MAX_APPEND = 4000
MAX_PROMPT = 8000                                 # a first prompt is typed into the pane with the command: longer ones belong in a task
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


# ---------- what this box's claude can do (one `claude --help` per binary, never on a hot path twice) ----------

_clock = time.monotonic                           # patched by tests (failure retry age)
_caps_lock = threading.Lock()
_caps_items: dict = {}                            # (exe, mtime_ns) -> (at, {ultracode_flag}, ok)


def reset_caches() -> None:
    with _caps_lock:
        _caps_items.clear()


def help_lists_ultracode(text: str) -> bool:
    """Does `claude --help` name `ultracode` on its --effort option? The option's own lines only (it may wrap onto indented lines): a word
    in another option's text does not count."""
    block: list[str] = []
    inside = False
    for ln in (text or "").splitlines():
        stripped = ln.strip()
        if "--effort" in ln and ln.lstrip().startswith(("-", "--")):
            inside, block = True, [ln]
            continue
        if inside:
            if not stripped or stripped.startswith("-"):
                break
            block.append(ln)
    return ULTRACODE in " ".join(block).lower()


def _bin_key(exe: str) -> tuple:
    try:
        return (exe, os.stat(exe).st_mtime_ns)
    except OSError:
        return (exe, 0)


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
    MODELS = ("opus", "fable", "sonnet", "haiku", "opusplan", "best", "opus[1m]", "sonnet[1m]")

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

    # ---- capabilities: what this box's claude takes ----
    def capabilities(self) -> dict:
        """{ultracode_flag}: does `claude --effort ultracode` work here? CCBOARD_CLAUDE_ULTRACODE_FLAG=1|0 records the V19 box check and
        wins; otherwise the --effort option of `claude --help` (read once per binary mtime, a failed probe retried after a minute, no
        binary or an unreadable help = False). Never raises."""
        env = os.environ.get(ULTRACODE_ENV, "").strip().lower()
        if env in ("1", "true", "yes", "on"):
            return {"ultracode_flag": True}
        if env in ("0", "false", "no", "off"):
            return {"ultracode_flag": False}
        exe = self.bin()
        if not exe:
            return {"ultracode_flag": False}
        key = _bin_key(exe)
        with _caps_lock:
            hit = _caps_items.get(key)
            if hit and (hit[2] or _clock() - hit[0] < FAIL_TTL):
                return dict(hit[1])
        try:
            cp = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=CAPS_TIMEOUT)
            ok = cp.returncode == 0 and "--effort" in (cp.stdout or "")
            caps = {"ultracode_flag": help_lists_ultracode(cp.stdout) if ok else False}
        except (subprocess.SubprocessError, OSError, ValueError):
            ok, caps = False, {"ultracode_flag": False}
        with _caps_lock:
            _caps_items[key] = (_clock(), caps, ok)
        return dict(caps)

    def launch_caps(self) -> dict:
        return self.capabilities()

    def peek_capabilities(self) -> dict | None:
        """capabilities() without a probe: the recorded answer (env switch, or the cached `--help` of this binary), None when none exists yet.
        The doctor uses it so a version check never starts a process."""
        env = os.environ.get(ULTRACODE_ENV, "").strip().lower()
        if env in ("1", "true", "yes", "on"):
            return {"ultracode_flag": True}
        if env in ("0", "false", "no", "off"):
            return {"ultracode_flag": False}
        exe = self.bin()
        if not exe:
            return None
        with _caps_lock:
            hit = _caps_items.get(_bin_key(exe))
        return dict(hit[1]) if hit and hit[2] else None

    def gate_unmet(self, key: str) -> str | None:
        """The plain reason when the installed claude is too old for FEATURE_GATES[key]; None when the gate is met or the version cannot be read
        (an unknown version never hides an option)."""
        v = self.version()
        return gate_message(key, v) if gate_met(key, v) is False else None

    def permission_modes(self) -> list[str]:
        """PERMISSION_MODES as this box's claude takes them: `manual` is shown as its older name `default` below 2.1.200, and `auto` is left
        out below 2.1.283 (it cannot start an interactive session there)."""
        modes = list(PERMISSION_MODES)
        if self.gate_unmet("manual_mode"):
            modes = ["default" if m == "manual" else m for m in modes]
        if self.gate_unmet("auto_start_mode"):
            modes.remove("auto")
        return modes

    def efforts(self) -> tuple:
        """The efforts `--effort` takes on this box: low..max, plus ultracode when the box's claude accepts it."""
        return EFFORTS + ((ULTRACODE,) if self.capabilities()["ultracode_flag"] else ())

    @property
    def EFFORTS(self) -> tuple:                          # type: ignore[override]
        return self.efforts()

    # ---- option schema ----
    def option_schema(self) -> list[OptField]:
        """Every launcher control, in the order the sheet shows them (basic first). `launcher` is the launch kind; fields that only apply to
        some kinds carry a `when` ({"launcher": [...]} = any of those kinds). The launcher reads this once per page load and keeps an
        embedded copy for when the request fails."""
        efforts = list(self.efforts())
        ultra = ULTRACODE in efforts
        launch_when = lambda *kinds: {"launcher": list(kinds)}   # noqa: E731
        return [
            OptField("launcher", "Start", "select", list(LAUNCH_KINDS), "new",
                     "new: a fresh session. resume: pick up an earlier one (--resume, blank id = the picker). continue: the latest in this "
                     "folder (--continue). from_pr: the session linked to a pull request (--from-pr).", "basic"),
            OptField("resume_id", "Session to resume", "text", None, None,
                     "An id (UUID); blank opens Claude's picker.", "basic", False, launch_when("resume")),
            OptField("from_pr", "Pull request", "text", None, None,
                     "--from-pr: a PR number (123 or #123) or its URL.", "basic", False, launch_when("from_pr")),
            OptField("name", "Session name", "text", None, None,
                     "--name, and the board's own label. Blank takes the next free one (s1, s2...).", "basic"),
            OptField("model", "Model", "combo", list(self.MODELS), "opus",
                     "An alias or a full model id; add [1m] for the 1M-context variant. opus, fable, sonnet and haiku lead; opusplan, best and "
                     "the [1m] variants follow.", "basic"),
            OptField("effort", "Effort", "select", efforts, "high",
                     "--effort." + (" ultracode is accepted by this box's claude." if ultra else
                                    " ultracode is not a flag on this box (V19): it is applied after start with /effort ultracode on."),
                     "basic"),
            OptField("fast", "Fast mode", "bool", None, False,
                     "There is no CLI flag: the board sends /fast after the session starts.", "basic"),
            OptField("permission_mode", "Permission mode", "select", self.permission_modes(), None,
                     "--permission-mode. bypassPermissions skips every prompt: use the bypass acknowledgement, never for tasks."
                     + "".join(" " + why + "." for why in filter(None, (self.gate_unmet("manual_mode"), self.gate_unmet("auto_start_mode")))), "basic"),
            OptField("prompt", "First prompt", "textarea", None, None,
                     f"Typed as the first message of a new session ({MAX_PROMPT} characters at most).", "basic", False, launch_when("new")),
            OptField("bypass", "Skip all permission prompts", "bool", None, False,
                     "--dangerously-skip-permissions. Needs an explicit acknowledgement; never offered for tasks, dispatch or scheduled runs, "
                     "and never stored for resume.", "advanced", True),
            OptField("allowed_tools", "Allowed tools", "textarea", None, None,
                     "--allowedTools: one pattern per line or comma separated, e.g. Bash(git *).", "advanced"),
            OptField("disallowed_tools", "Disallowed tools", "textarea", None, None, "--disallowedTools, same format.", "advanced"),
            OptField("tools", "Available tools", "textarea", None, None,
                     "--tools: the built-in tools the session has at all, comma or one per line (Bash, Edit, Read).", "advanced"),
            OptField("append_system_prompt", "Append to system prompt", "textarea", None, None,
                     f"--append-system-prompt ({MAX_APPEND} characters at most).", "advanced"),
            OptField("agent_name", "Agent", "text", None, None, "--agent: run as one of your subagent definitions.", "advanced"),
            OptField("fallback_model", "Fallback model", "text", None, None,
                     f"--fallback-model: up to {MAX_FALLBACKS} models, comma separated, tried when the main one is overloaded.", "advanced"),
            OptField("autocompact", "Auto-compact", "combo", ["auto"], None,
                     "--autocompact: auto, or the context size (a number such as 150000) at which the conversation is compacted. Blank = Claude's own setting.",
                     "advanced"),
            OptField("worktree", "Start in a new git worktree", "bool", None, False,
                     "claude --worktree <name>: a branch and folder of its own under .claude/worktrees.", "advanced", False,
                     launch_when("new")),
            OptField("worktree_name", "Worktree name", "text", None, None,
                     "Letters, digits, '.', '_' and '-'. Blank: the session name plus a short suffix.", "advanced", False, {"worktree": True}),
            OptField("fork_session", "Fork instead of continuing", "bool", None, False,
                     "--fork-session: a copy of the old conversation under a new id; the original stays as it was.", "advanced", False,
                     launch_when("resume", "continue")),
            OptField("add_dirs", "Extra directories", "dirs", None, None,
                     "--add-dir: sibling repos (project/repo) the session may read and edit.", "advanced"),
            OptField("devcontainer", "Run in the devcontainer", "bool", None, False,
                     "devcontainer up, then claude inside it (its own login).", "advanced", False, {"repo.devcontainer": True}),
            OptField("mcp_config", "MCP config file", "text", None, None,
                     "--mcp-config: the path of a JSON file inside the projects folder or the Claude config folder.", "advanced"),
            OptField("extra", "Extra arguments", "args", None, None,
                     "Raw CLI arguments. --settings, --setting-sources and --permission-prompt* are rejected: use the controls.", "advanced"),
        ]

    # ---- validation ----
    def forbidden_extra(self, extra: list[str], *, interactive: bool, task: bool = False) -> str | None:
        """The first offending token, or None. interactive: settings overrides only. task (interactive too): overrides, then bypass
        spellings (--permission-mode itself stays allowed). Not interactive (scheduled runs): FORBIDDEN_ARG_PARTS."""
        extra = [str(a) for a in (extra or [])]
        if "--" in extra:                    # a bare `--` turns the board's own --session-id / --name / --worktree into positionals (#44 F-07)
            return "--"
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

    @staticmethod
    def _with_mode(raw: dict) -> dict:
        """The launcher's `mode` word as a permission_mode (it wins over a permission_mode sent beside it): default = manual, read-only =
        plan, bypass = bypassPermissions, or any Claude permission mode by its own name. `custom` is Codex's sandbox + approval pair."""
        mode = raw.get("mode")
        if mode in (None, ""):
            return raw
        pm = MODE_ALIASES.get(mode, mode) if isinstance(mode, str) else None
        if pm not in PERMISSION_MODES:
            raise projects.BadRequest(f"mode must be one of default, read-only, bypass, {', '.join(m for m in PERMISSION_MODES if m != 'manual')}")
        return {**raw, "permission_mode": pm}

    @staticmethod
    def _model_list(raw, what: str, cap: int) -> list[str]:
        items = [t for t in re.split(r"[,\s]+", raw) if t] if isinstance(raw, str) else (list(raw) if isinstance(raw, (list, tuple)) else None)
        if items is None or not all(isinstance(m, str) and MODEL_RE.match(m) for m in items) or len(items) > cap:
            raise projects.BadRequest(f"{what}: use up to {cap} aliases or model ids, comma separated")
        return list(dict.fromkeys(items))

    @staticmethod
    def _mcp_config(raw, *, must_exist: bool) -> str:
        """The path of an MCP config file: absolute (or ~/...), a file under the projects folder or the Claude config folder. A stored
        path that has since gone is not an error on resume (must_exist False): Claude says so itself."""
        what = "mcp_config: use the absolute path of a JSON file inside the projects folder or the Claude config folder"
        if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_MCP_PATH or "\0" in raw or raw.strip().startswith("-"):
            raise projects.BadRequest(what)
        path = Path(raw.strip()).expanduser()
        if not path.is_absolute():
            raise projects.BadRequest(what)
        roots = [Path(settings.projects_dir), Path(settings.claude_config_dir)]
        real = Path(os.path.realpath(path))
        if not any(real == r or r.resolve() in real.parents for r in roots):
            raise projects.BadRequest(what)
        if must_exist and not real.is_file():
            raise projects.BadRequest("mcp_config: no such file")
        return str(path)

    def _validate(self, raw: dict | None, *, interactive: bool, tasks_or_headless: bool, files: bool = True) -> tuple[dict, dict, list[str]]:
        """-> (full, clean, extra). `full` drives argv (it keeps permission_mode=bypassPermissions and the one-launch from_pr /
        fork_session); `clean` is what may be stored and re-passed on resume (never a bypass, never the one-off extra args, never a
        from_pr or fork_session). `files` False skips the existence check of an MCP config file (a stored option on resume)."""
        raw = raw if isinstance(raw, dict) else {}
        raw = self._with_mode(raw)
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
        effort = raw.get("effort") or raw.get("reasoning")           # `reasoning` is the other agents' word for it
        if effort:
            allowed = self.efforts()
            if effort not in allowed:
                raise projects.BadRequest(f"effort must be one of {', '.join(allowed)}")
            if effort == ULTRACODE and not interactive:          # #78: the docs: ultracode has no effect under -p (a scheduled or headless run)
                raise projects.BadRequest(f"effort must be one of {', '.join(EFFORTS)} for a scheduled run: ultracode has no effect under -p")
            full["effort"] = effort
        if pm:
            if interactive and pm == "default" and self.gate_unmet("manual_mode"):
                pm = "manual"                                 # the older name, which the launcher offers on a claude below 2.1.200
            if interactive and pm not in PERMISSION_MODES:    # a scheduled run was checked against HEADLESS_MODES above
                raise projects.BadRequest(f"permission_mode must be one of {', '.join(self.permission_modes())}")
            if interactive and pm == "auto" and self.gate_unmet("auto_start_mode"):
                raise projects.BadRequest(f"permission_mode auto: {self.gate_unmet('auto_start_mode')}; pick plan or acceptEdits, or update Claude Code")
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
        tools = self._tools(raw.get("tools"))
        if tools:
            full["tools"] = tools
        name = raw.get("agent_name")
        if name not in (None, ""):
            if not isinstance(name, str) or not AGENT_NAME_RE.match(name.strip()):
                raise projects.BadRequest("agent_name: use letters, digits, '.', '_', ':', '@', '/' or '-'")
            full["agent_name"] = name.strip()
        if raw.get("fallback_model") not in (None, "", []):
            full["fallback_model"] = self._model_list(raw["fallback_model"], "fallback_model", MAX_FALLBACKS)
        ac = raw.get("autocompact")
        if ac not in (None, ""):
            ac = str(ac).strip().lower() if isinstance(ac, (str, int)) and not isinstance(ac, bool) else None
            if not ac or not AUTOCOMPACT_RE.match(ac):
                raise projects.BadRequest("autocompact: use auto or a number of tokens (4 to 9 digits)")
            full["autocompact"] = ac
        if raw.get("mcp_config") not in (None, ""):
            full["mcp_config"] = self._mcp_config(raw["mcp_config"], must_exist=files)
        if _truthy(raw.get("fast")):
            full["fast"] = True
        if _truthy(raw.get("devcontainer")):
            full["devcontainer"] = True
        pr = raw.get("from_pr")
        if pr not in (None, ""):
            if not isinstance(pr, str) or not PR_RE.match(pr.strip()):
                raise projects.BadRequest("from_pr: use a pull request number (123 or #123) or its URL")
            full["from_pr"] = pr.strip().lstrip("#")
        if _truthy(raw.get("fork_session")):
            full["fork_session"] = True
        ONE_LAUNCH = ("from_pr", "fork_session")
        clean = {k: v for k, v in full.items() if k not in ONE_LAUNCH and not (k == "permission_mode" and v == "bypassPermissions")}
        return full, clean, extra

    def validate_opts(self, raw: dict | None, *, interactive: bool = True, tasks_or_headless: bool = False) -> dict:
        return self._validate(raw, interactive=interactive, tasks_or_headless=tasks_or_headless)[1]

    # ---- launching ----
    def _opt_args(self, full: dict) -> list[str]:
        """The launch controls as argv, in the order main._launch_args always used. `manual` goes out as `default` on a claude below 2.1.200
        (the stored option keeps `manual`: the argv never carries a name the CLI rejects)."""
        out: list[str] = []
        if "model" in full:
            out += ["--model", full["model"]]
        if "effort" in full:
            out += ["--effort", full["effort"]]
        if "permission_mode" in full:
            pm = full["permission_mode"]
            out += ["--permission-mode", "default" if pm == "manual" and self.gate_unmet("manual_mode") else pm]
        for key, flag in (("allowed_tools", "--allowedTools"), ("disallowed_tools", "--disallowedTools")):
            if full.get(key):
                out += [flag, *full[key]]
        if "append_system_prompt" in full:
            out += ["--append-system-prompt", full["append_system_prompt"]]
        if full.get("tools"):
            out += ["--tools", ",".join(full["tools"])]                  # one token (the flag is variadic): nothing after it is swallowed
        if "agent_name" in full:
            out += ["--agent", full["agent_name"]]
        if full.get("fallback_model"):
            out += ["--fallback-model", ",".join(full["fallback_model"])]
        if "autocompact" in full:
            out += ["--autocompact", full["autocompact"]]
        if "mcp_config" in full:
            out += ["--mcp-config", full["mcp_config"]]
        return out

    def launch_opt_args(self, raw: dict | None, *, interactive: bool = True, tasks_or_headless: bool = False) -> list[str]:
        """Just the launch controls as argv (what main._launch_args returned), validated with the same messages."""
        return self._opt_args(self._validate(raw, interactive=interactive, tasks_or_headless=tasks_or_headless)[0])

    def launch_plan(self, req: LaunchReq) -> LaunchPlan:
        if req.kind not in LAUNCH_KINDS:
            raise projects.BadRequest(f"launch kind must be one of {', '.join(LAUNCH_KINDS)}")
        full, clean, extra = self._validate(req.opts, interactive=True, tasks_or_headless=bool(req.task))
        if not req.task and req.prompt is not None and len(req.prompt) > MAX_PROMPT:
            raise projects.BadRequest(f"prompt is too long ({MAX_PROMPT} characters at most); a task takes a longer one")
        if req.kind == "from_pr":
            if not full.get("from_pr"):
                raise projects.BadRequest("from_pr: give the pull request number or URL")
        elif full.get("from_pr"):
            raise projects.BadRequest("from_pr: only the from_pr launch takes a pull request")
        if full.get("fork_session") and req.kind not in ("resume", "continue"):
            raise projects.BadRequest("fork_session: only a resume or continue launch can fork")
        if req.task:
            first = req.prompt.split(None, 1)[0] if req.prompt and req.prompt.strip() else ""
            # a prompt word `--` is text (argv puts the prompt after its own `--`), as in the Codex adapter; only extra args refuse it (#44 F-07)
            bad = self.forbidden_extra([first], interactive=True, task=True) if first.startswith("-") and first != "--" else None
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
                argv = ["claude", *bypass, *tail, *more, "--worktree", req.worktree, "--session-id", agent_sid,
                        *([] if req.task else naming)]               # a task has no session name of its own to show in /resume
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
                raise projects.BadRequest("a first prompt can only start a new session; resume, continue and from_pr take none")
            if req.worktree:
                raise projects.BadRequest("worktree: only a new session can start in a native worktree")
            fork = ["--fork-session"] if full.get("fork_session") else []
            if req.kind == "resume":
                if req.resume_id and not UUID_RE.match(req.resume_id):
                    raise projects.BadRequest("resume id must be a UUID")
                agent_sid = req.resume_id or None
                argv = ["claude", "--resume", *([req.resume_id] if req.resume_id else []), *fork, *bypass, *tail, *more]
            elif req.kind == "from_pr":
                # the conversation linked to that PR: its id is learned from the first hook, there is nothing to pass or name
                argv = ["claude", "--from-pr", full["from_pr"], *bypass, *tail, *more]
            else:
                argv = ["claude", "--continue", *fork, *bypass, *tail, *more]
        cmd_line = shlex.join(argv)
        if devc:
            # devcontainer CLI: build/start the container, then run claude inside it (its own ~/.claude; log in once there)
            cmd_line = (shlex.join(["devcontainer", "up", "--workspace-folder", cwd]) + " && "
                        + shlex.join(["devcontainer", "exec", "--workspace-folder", cwd, "--"]) + " " + cmd_line)
        return LaunchPlan(argv=argv, cmd_line=cmd_line, agent_session_id=agent_sid, cwd=cwd, opts_clean=clean, worktree=worktree)

    def resume_argv(self, session_id: str | None = None, *, name: str | None = None, opts: dict | None = None, add_dirs=()) -> list[str]:
        if session_id and not UUID_RE.match(session_id):
            raise projects.BadRequest("resume id must be a UUID")
        full = self._validate(opts, interactive=True, tasks_or_headless=False, files=False)[0]
        target = session_id or name
        argv = ["claude", "--resume", *([target] if target else []), *self._opt_args(full)]
        dirs = [str(d) for d in (add_dirs or ())]
        return argv + (["--add-dir", *dirs] if dirs else [])

    def continue_argv(self, cwd: str, opts: dict | None = None, add_dirs=()) -> list[str]:
        full = self._validate(opts, interactive=True, tasks_or_headless=False, files=False)[0]
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
        # weights are measured use counts from 8,605 prompts (plan, usage analysis F6). `verified` per the V8 box check (Claude Code
        # 2.1.290, issue #28): every row below was typed the way the board types it. /effort <level> and /model <name> typed inline also
        # SAVE the person's default for new sessions (saves_default): the Tune's Effort goes through the /effort picker and `s` (POST
        # /tune, session only); the model picker's list is version-specific, so Model stays inline and says that it saves the default.
        # Bare /fast opens a dialog that swallows keys: the board sends `/fast on` or `/fast off`. /context prints inline; /usage, /cost
        # and /status open a dialog one Escape closes. /effort ultracode [on|off] is session-only.
        rows = [("clear", "Clear", False, False, 155, True, {}), ("compact", "Compact", False, False, 120, False, {}),
                ("usage", "Usage", False, True, 100, False, {"dialog": True}),
                ("effort", "Effort", True, False, 58, False, {"tune": "effort", "saves_default": True, "choices": list(EFFORT_ARGS),
                                                             "tune_verified": PROVEN[("claude", "effort")]}),
                ("model", "Model", True, False, 39, False, {"saves_default": True}), ("rename", "Rename", True, False, 10, False, {}),
                ("context", "Context", False, True, 9, False, {}), ("status", "Status", False, True, 3, False, {"dialog": True}),
                ("cost", "Cost", False, True, 0, False, {"dialog": True}), ("fast", "Fast", True, False, 0, False, {"choices": ["on", "off"]})]
        return {k: SlashSpec(cmd="/" + k, label=label, arg=arg, read=read, verified=True, weight=w, destructive=d, **more)
                for k, label, arg, read, w, d, more in rows}

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
