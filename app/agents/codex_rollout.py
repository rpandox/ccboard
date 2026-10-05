"""Codex rollouts as a data source (v0.5.12, slice A): what a running Codex session reports about itself, read from the file Codex writes.

A rollout is `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl`, one JSON object per line. The first line is a `session_meta`
(thread id, cwd, originator, source, cli_version, creator_account_id ... and a large `base_instructions` that is never read out); later lines
are `event_msg` items (`token_count`: last/total token usage, the context window and the account's `rate_limits`) and `turn_context` /
`thread_settings_applied` items (model, effort, approval, sandbox). Everything here is READ ONLY and display only: nothing in this module
decides a session's state (the hooks do), and nothing opens `auth.json` or any other file of the Codex home except rollouts.

    parse_tail(text)       pure: the newest token usage, context window, rate limits and settings among the last lines of a rollout
    read_tail(path)        the last TAIL_BYTES of a file (a seek, never a whole read)
    session_meta(path)     the first line only, whitelisted keys (never base_instructions); None for a file that is not a rollout
    bind_unbound_rows(db)  the v0.5.11 seam: an open codex row with no conversation id yet binds to its rollout (cwd + originator + time)
    Tailer / tick(db, now) the Sampler's hook (samples.TICK_HOOKS, so every 15 s tick; the Tailer keeps its own 5 s / 60 s gaps): per open
                           codex row stats / ctx / ctx_tok / stok / confirmation of a typed /model or /reasoning, and every minute the
                           account rate limits (kv rate_limits_codex, series rl_5h / rl_7d keys `codex` and `cacct:<account key>`, one notice
                           per reached window through kv codex_rl_notified_<resets_at>)

Rate limits (plan v0.5.12, F10). A rollout's token_count events carry the account-wide windows; which rollout is newest says nothing about
which limit it reports (a second metered limit such as `codex_bengalfox` sits at 0 and would flip the pill between about 17 % and 0 %). So:
the newest reading per `limit_id` is kept, an id other than `codex` whose windows are all zero is ignored, the `codex` limit is preferred and,
per window (window_minutes <= 360 is the 5 h one, >= 7200 the weekly one, anything else is shown but never charted), the highest used_percent
among the live readings wins. A window whose resets_at has passed has rolled over and is not a reading any more. The account a reading belongs
to is the Codex account whose kv `codex_accounts` record has the same `account_id` as the rollout's `creator_account_id`, else the current
account (kv `codex_account_current`): `cacct:<key>` always gets the reading, the CURRENT account's also goes to key `codex`, to the kv
`rate_limits_codex` and from there to state.usage_codex. Samples are stamped with the event's own time, so a dead rollout stays flat instead
of being re-stamped every minute.

Import rule (agents/): config, projects and the agents registry only at module level; `samples` and `notify` are imported inside the functions that
need them, and the accounts' kv is read directly (this module never imports codex_accounts, db, hooks or main). Writes go through the `db`
object the caller passes. Nothing here logs or stores rollout text: the only fields that leave a file are the whitelisted ones named below.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import projects
from ..config import settings
from . import get as get_agent

log = logging.getLogger("ccboard.codex_rollout")

TAIL_BYTES = 192 * 1024              # the end of a rollout that parse_tail looks at (the plan says 128-256 KB)
META_MAX = 1024 * 1024               # the first line (session_meta carries base_instructions, tens of KB) is read up to this
TAIL_EVERY = 5.0                     # the Tailer's own minimum gap between row passes (the Sampler hook calls every 15 s)
RL_EVERY = 60.0                      # rate limits are looked at this often
RL_LOOKBACK_DAYS = 8                 # the longest window is 7 days: a rollout idle for longer says nothing about the current one
MAX_ROLLOUTS = 40                    # rollouts read per rate-limit pass (newest first by mtime)
MAX_STAT = 3000                      # files stat'ed per pass at most (a day directory can be large on a box with an agent writing all day)
BIND_SLACK = 5.0                     # a rollout may be stamped this many seconds before the row it belongs to (clock rounding)
PATH_RETRY = 30.0                    # a row whose rollout is not found yet is looked up again this often
CMD_PENDING_TTL = 20.0               # main.CMD_PENDING_TTL: seconds a typed /model or /reasoning waits for its confirmation
CACHE_MAX = 512
MAIN_LIMIT = "codex"                 # the limit_id of the plan's own limit; others are separately metered features
TUI = "codex-tui"                    # the originator of a session the board (or a person at a terminal) started; Hermes etc. say otherwise
SUB_MARKERS = ("subagent", "thread_spawn", "guardian")      # in a session_meta's source / thread_source: not a conversation of its own
KV_RATE = "rate_limits_codex"
KV_ACCOUNTS = "codex_accounts"
KV_CURRENT = "codex_account_current"
NOTIFIED = "codex_rl_notified_"      # + resets_at: the once-per-window notice gate

ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")
_DAY_DIR = re.compile(r"^\d{2}$")
_YEAR_DIR = re.compile(r"^\d{4}$")

_lock = threading.RLock()
_meta_cache: dict[str, dict] = {}    # path -> session_meta (successes only: the first line never changes once written)
_rl_cache: dict[str, tuple] = {}     # path -> ((mtime_ns, size), {rl, at, account_id}): an unchanged file is not read again


def reset() -> None:
    """Forget every cache and the module Tailer's clocks (tests)."""
    with _lock:
        _meta_cache.clear()
        _rl_cache.clear()
    _tailer.reset()


# ------------------------------------------------------------------ small readers
def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _int(v) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


def _text(v, n: int = 120) -> str | None:
    if not isinstance(v, str):
        return None
    t = v.strip()
    return t[:n] if t and t.isprintable() else None


def _epoch(v) -> float | None:
    """Epoch seconds from an ISO text ('Z', an offset, fractional seconds and naive-as-UTC are fine) or a number; None otherwise."""
    if isinstance(v, str):
        t = v.strip()
        if not t:
            return None
        try:
            d = datetime.fromisoformat(t[:-1] + "+00:00" if t[-1] in "Zz" else t)
        except ValueError:
            return None
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    n = _num(v)
    return n


def _iso(ts: float) -> str:
    """The board's one timestamp form ('2026-10-05T03:37:00+00:00': UTC, whole seconds; the same text db.iso writes)."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _dig(d, *path):
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _first(d, *paths):
    """The first non-None value found along any of the key paths."""
    for p in paths:
        v = _dig(d, *p)
        if v is not None:
            return v
    return None


def _word(v, n: int = 60) -> str | None:
    """A short label from a settings value: a string as it is, an object by its `type` (or its only key): sandbox_policy
    {"type": "workspace-write", ...} -> "workspace-write", approval_policy {"granular": {...}} -> "granular"."""
    if isinstance(v, str):
        return _text(v, n)
    if isinstance(v, dict):
        t = v.get("type")
        if isinstance(t, str):
            return _text(t, n)
        if len(v) == 1:
            return _text(next(iter(v)), n)
    return None


# ------------------------------------------------------------------ parse_tail
USAGE_KEYS = (("input_tokens", "input"), ("cached_input_tokens", "cached"), ("output_tokens", "output"),
              ("reasoning_output_tokens", "reasoning"), ("total_tokens", "total"))


def _usage(d) -> dict | None:
    """A token_count usage object as {input, cached, output, reasoning, total} ints, None when it has none of them. A missing total is the
    input plus the output."""
    if not isinstance(d, dict):
        return None
    out = {short: _int(d.get(long)) for long, short in USAGE_KEYS}
    if all(v is None for v in out.values()):
        return None
    if out["total"] is None and (out["input"] is not None or out["output"] is not None):
        out["total"] = (out["input"] or 0) + (out["output"] or 0)
    return {k: v for k, v in out.items() if v is not None}


def _window(w, at: float | None) -> dict | None:
    """One rate-limit window as {used_percent, window_minutes, resets_at}; None for anything without a usable used_percent. resets_at is
    epoch seconds (a build that reports resets_in_seconds instead gets the event's time plus that)."""
    if not isinstance(w, dict):
        return None
    used = _num(w.get("used_percent"))
    if used is None:
        used = _num(w.get("used_percentage"))
    if used is None:
        return None
    reset = _int(w.get("resets_at"))
    if reset is None and at is not None and _num(w.get("resets_in_seconds")) is not None:
        reset = int(at + _num(w.get("resets_in_seconds")))
    return {"used_percent": round(max(0.0, min(100.0, used)), 2), "window_minutes": _int(w.get("window_minutes")), "resets_at": reset}


def _credits(c) -> dict | None:
    if not isinstance(c, dict):
        return None
    bal = c.get("balance")
    out = {"has_credits": c.get("has_credits") if isinstance(c.get("has_credits"), bool) else None,
           "unlimited": c.get("unlimited") if isinstance(c.get("unlimited"), bool) else None,
           "balance": _text(bal, 40) if isinstance(bal, str) else _num(bal)}
    return out if any(v is not None for v in out.values()) else None


def _rate_limits(rl, at: float | None) -> dict | None:
    """A token_count's rate_limits as {limit_id, plan_type, primary, secondary, credits, reached}; None when neither window is usable."""
    if not isinstance(rl, dict):
        return None
    primary, secondary = _window(rl.get("primary"), at), _window(rl.get("secondary"), at)
    if primary is None and secondary is None:
        return None
    return {"limit_id": _text(rl.get("limit_id"), 80), "plan_type": _text(rl.get("plan_type"), 32), "primary": primary, "secondary": secondary,
            "credits": _credits(rl.get("credits")), "reached": bool(rl.get("rate_limit_reached_type")) or any(
                w is not None and w["used_percent"] >= 100 for w in (primary, secondary))}


def _config(p: dict) -> dict:
    """The settings one turn_context / thread_settings_applied payload names: model, effort, approval, sandbox (each None when absent).
    The key names differ between Codex builds, so each is looked up along the places it has been seen."""
    return {"model": _text(_first(p, ("model",), ("settings", "model"), ("collaboration_mode", "settings", "model"), ("info", "model")), 80),
            "effort": _word(_first(p, ("effort",), ("reasoning_effort",), ("model_reasoning_effort",), ("settings", "reasoning_effort"),
                                   ("settings", "effort"), ("collaboration_mode", "settings", "reasoning_effort")), 20),
            "approval": _word(_first(p, ("approval_policy",), ("approval",), ("settings", "approval_policy")), 40),
            "sandbox": _word(_first(p, ("sandbox_policy",), ("sandbox",), ("sandbox_mode",), ("settings", "sandbox_policy")), 40)}


CONFIG_KINDS = ("turn_context", "thread_settings_applied")


def parse_tail(text: str) -> dict:
    """What the end of a rollout says, from the text of its last lines (read_tail: the first line of that text may be cut in half and is
    simply not valid JSON, so it is skipped). Scans from the newest line back, parsing only lines that name a token_count, turn_context or
    thread_settings_applied, and stops when it has everything:

        {last_token_usage: {input, cached, output, reasoning, total} | None,      # the newest call, NOT cumulative
         total_token_usage: {...} | None, model_context_window: int | None, token_at: ISO | None,
         rate_limits: {limit_id, plan_type, primary, secondary, credits, reached} | None, rate_limits_at: ISO | None,
         config: {model, effort, approval, sandbox, at} | None}                    # fields filled newest-first across the config events

    A token_count may keep its numbers under `info` (a real rollout) or beside `rate_limits` (flat); `info` is null before the first model
    call, and such an event still carries rate_limits, so the usage and the rate limits come from the newest event that has each. Pure."""
    out: dict = {"last_token_usage": None, "total_token_usage": None, "model_context_window": None, "token_at": None,
                 "rate_limits": None, "rate_limits_at": None, "config": None}
    if not isinstance(text, str) or not text:
        return out
    cfg: dict | None = None
    for line in reversed(text.split("\n")):
        counts = "token_count" in line
        cfgline = any(k in line for k in CONFIG_KINDS)
        if not (counts or cfgline):
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            continue
        payload = obj.get("payload") if isinstance(obj, dict) else None
        if not isinstance(payload, dict):
            continue
        at = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else payload.get("timestamp")
        kind = payload.get("type") if isinstance(payload.get("type"), str) else obj.get("type")
        if kind == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            if out["last_token_usage"] is None:
                last = _usage(info.get("last_token_usage") or payload.get("last_token_usage"))
                if last:
                    out["last_token_usage"] = last
                    out["total_token_usage"] = _usage(info.get("total_token_usage") or payload.get("total_token_usage"))
                    out["model_context_window"] = _int(info.get("model_context_window") if "model_context_window" in info
                                                       else payload.get("model_context_window"))
                    out["token_at"] = at
            if out["rate_limits"] is None:
                rl = _rate_limits(payload.get("rate_limits") or info.get("rate_limits"), _epoch(at))
                if rl:
                    out["rate_limits"], out["rate_limits_at"] = rl, at
        elif kind in CONFIG_KINDS or obj.get("type") in CONFIG_KINDS:
            got = _config(payload)
            if cfg is None:
                cfg = {**got, "at": at}
            else:
                for k, v in got.items():
                    if cfg.get(k) is None and v is not None:
                        cfg[k] = v
        if out["last_token_usage"] is not None and out["rate_limits"] is not None and cfg is not None \
                and all(cfg.get(k) is not None for k in ("model", "effort", "approval", "sandbox")):
            break
    out["config"] = cfg
    return out


def read_tail(path, nbytes: int = TAIL_BYTES) -> str:
    """The last `nbytes` of a file as text (undecodable bytes are replaced; the cut may split the first line, which parse_tail skips).
    A seek, never a whole read: a rollout of a long session is tens of MB. '' for an unreadable file."""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            f.seek(max(0, size - nbytes))
            return f.read(nbytes).decode("utf-8", "replace")
    except OSError:
        return ""


# ------------------------------------------------------------------ session_meta
def _source_text(v) -> str | None:
    if isinstance(v, str):
        return _text(v, 120)
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v, separators=(",", ":"), ensure_ascii=False)[:300]
        except (TypeError, ValueError):
            return None
    return None


def _ident(v) -> str | None:
    return v if isinstance(v, str) and ID_RE.match(v) else None


def is_subthread(meta: dict | None) -> bool:
    """Is this session_meta a guardian or thread_spawn child (a thread of its own in the same pane, never the person's conversation)?"""
    m = meta if isinstance(meta, dict) else {}
    hay = f"{m.get('source') or ''} {m.get('thread_source') or ''}".lower()
    return any(k in hay for k in SUB_MARKERS)


def session_meta(path) -> dict | None:
    """The whitelisted fields of a rollout's FIRST line (its session_meta): {id, cwd, originator, source, thread_source, cli_version,
    model_provider, context_window, account_id (creator_account_id), user_id (creator_user_id), forked_from_id, timestamp, created (epoch)},
    each None when absent. Never `base_instructions` or anything else of the line. None for a file that cannot be read, whose first line is
    not a session_meta (or is cut off / longer than META_MAX), so a rollout still being written is simply tried again later."""
    try:
        with open(path, "rb") as f:
            line = f.readline(META_MAX)
        obj = json.loads(line)
    except (OSError, ValueError, RecursionError, MemoryError):
        return None
    p = obj.get("payload") if isinstance(obj, dict) and obj.get("type") == "session_meta" else None
    if not isinstance(p, dict):
        return None
    ts = _text(p.get("timestamp") or obj.get("timestamp"), 40)
    return {"id": _ident(p.get("id")) or _ident(p.get("session_id")), "cwd": _text(p.get("cwd"), 4096),
            "originator": _text(p.get("originator"), 80), "source": _source_text(p.get("source")),
            "thread_source": _source_text(p.get("thread_source")), "cli_version": _text(p.get("cli_version"), 40),
            "model_provider": _text(p.get("model_provider"), 40), "context_window": _int(p.get("context_window")),
            "account_id": _ident(p.get("creator_account_id")), "user_id": _ident(p.get("creator_user_id")),
            "forked_from_id": _ident(p.get("forked_from_id")), "timestamp": ts, "created": _epoch(ts)}


def _meta(path: Path) -> dict | None:
    """session_meta(path) remembered per path (the first line never changes once it parses)."""
    key = str(path)
    with _lock:
        hit = _meta_cache.get(key)
    if hit is not None:
        return hit
    m = session_meta(path)
    if m is not None:
        with _lock:
            if len(_meta_cache) >= CACHE_MAX * 2:
                for k in list(_meta_cache)[:CACHE_MAX]:
                    del _meta_cache[k]
            _meta_cache[key] = m
    return m


# ------------------------------------------------------------------ finding rollouts
def sessions_root() -> Path:
    return Path(settings.codex_home) / "sessions"


def recent_rollouts(since: float, limit: int = MAX_STAT) -> list[tuple[float, int, Path]]:
    """(mtime, size, path) of the rollouts modified at or after `since` (epoch), newest first by mtime. Walks the YYYY/MM/DD directories
    newest first and stops at the first day directory more than a day older than `since` (the directories are named for the session's start
    day, local time, so one day of margin covers every zone), so a year of history is never listed."""
    root = sessions_root()
    floor = (datetime.fromtimestamp(since, tz=timezone.utc) - timedelta(days=1)).date()
    out: list[tuple[float, int, Path]] = []
    try:
        years = sorted((d for d in root.iterdir() if _YEAR_DIR.match(d.name) and d.is_dir()), key=lambda d: d.name, reverse=True)
    except OSError:
        return []
    done = False
    for y in years:
        try:
            months = sorted((d for d in y.iterdir() if _DAY_DIR.match(d.name) and d.is_dir()), key=lambda d: d.name, reverse=True)
        except OSError:
            continue
        for m in months:
            try:
                days = sorted((d for d in m.iterdir() if _DAY_DIR.match(d.name) and d.is_dir()), key=lambda d: d.name, reverse=True)
            except OSError:
                continue
            for d in days:
                try:
                    day = datetime(int(y.name), int(m.name), int(d.name)).date()
                except ValueError:
                    continue
                if day < floor:
                    done = True
                    break
                try:
                    for f in d.iterdir():
                        if f.name.startswith("rollout-") and f.name.endswith(".jsonl"):
                            try:
                                st = f.stat()
                            except OSError:
                                continue
                            if st.st_mtime >= since:
                                out.append((st.st_mtime, st.st_size, f))
                except OSError:
                    continue
                if len(out) >= limit:
                    done = True
                    break
            if done:
                break
        if done:
            break
    out.sort(key=lambda t: t[0], reverse=True)
    return out


def _real(p) -> str | None:
    if not isinstance(p, (str, os.PathLike)) or not str(p):
        return None
    try:
        return os.path.realpath(str(p))
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ binding an open row to its rollout
def _row_cwd(row: dict) -> str | None:
    cwd = row.get("cwd")
    if isinstance(cwd, str) and cwd:
        return _real(cwd)
    try:
        return _real(projects.repo_path(row.get("project"), row.get("repo")))
    except Exception:
        return None


def _bound_ids(db) -> set[str]:
    """Every conversation id a session row (open or ended) already has: a rollout one of them owns is not a candidate."""
    with db.lock:
        rows = db.conn.execute("SELECT claude_session_id FROM sessions WHERE claude_session_id IS NOT NULL").fetchall()
    return {str(r[0]).lower() for r in rows}


def _flagged(row: dict, bound: set[str]) -> tuple[str, Path] | None:
    """(thread id, path) of the rollout a row's own hook named (flags.transcript_path, trusted only under CODEX_HOME/sessions) when that
    file is a conversation of its own whose id nobody has: the one case where no guessing is needed."""
    tp = (row.get("flags") or {}).get("transcript_path")
    path = get_agent("codex")._under_sessions(tp) if tp else None
    meta = _meta(path) if path is not None else None
    if not meta or not meta["id"] or is_subthread(meta) or meta["id"].lower() in bound:
        return None
    return meta["id"], path


def _bind(db, row: dict, sid: str, path: Path) -> bool:
    with db.lock:
        cur = db.conn.execute("UPDATE sessions SET claude_session_id=? WHERE id=? AND ended_at IS NULL AND claude_session_id IS NULL",
                              (sid, row["row_id"]))
    if not cur.rowcount:
        return False
    if not (row.get("flags") or {}).get("transcript_path"):
        db.update_flags(row["tmux_name"], {"transcript_path": str(path)})
    return True


def bind_unbound_rows(db) -> int:
    """Identity step 3 for Codex (plan v0.5.11 'Identity join order'): bind open codex rows that have no conversation id yet to their rollout.
    A row whose hook already named its rollout (flags.transcript_path) takes that file's thread id. For the others, a rollout is a candidate for a row when its session_meta cwd is the row's cwd, its originator is codex-tui (Hermes, Codex Desktop and
    `codex exec` runs are somebody else's), it is not a guardian / thread_spawn child, its id is not any row's yet and it was created within
    the row's life (not before the row, minus a few seconds). Per cwd the unbound rows, oldest first, take the candidates, oldest first
    (FIFO); when the rows and the candidates of a cwd are not the same number (an idle row beside a typing one, a /new, a child that was not
    recognised) nobody knows which is whose and nothing is bound. The bind is `claude_session_id IS NULL` guarded, so a hook that
    binds the same row first wins; flags.transcript_path is filled in. Returns the rows bound. Never raises into the caller."""
    try:
        if db is None:
            return 0
        rows = [r for r in db.open_rows().values()
                if r.get("agent") == "codex" and not r.get("agent_session_id") and r.get("created_at")]
        if not rows:
            return 0
        now = time.time()
        bound = _bound_ids(db)
        n = 0
        by_cwd: dict[str, list[dict]] = {}
        for r in rows:
            got = _flagged(r, bound)
            if got is not None:
                if _bind(db, r, *got):
                    n += 1
                    bound.add(got[0].lower())
                continue
            cwd = _row_cwd(r)
            if cwd and _epoch(r["created_at"]) is not None:
                by_cwd.setdefault(cwd, []).append(r)
        if not by_cwd:
            return n
        floor = {cwd: min(_epoch(r["created_at"]) for r in rs) - BIND_SLACK for cwd, rs in by_cwd.items()}
        oldest = min(floor.values())
        cands: dict[str, list[tuple[float, str, Path]]] = {}
        for mtime, _size, path in recent_rollouts(oldest):
            meta = _meta(path)
            if not meta or meta["originator"] != TUI or is_subthread(meta) or not meta["id"] or meta["id"].lower() in bound:
                continue
            created, cwd = meta["created"], _real(meta["cwd"])
            if created is None or cwd not in by_cwd or created > now + BIND_SLACK or created < floor[cwd]:
                continue                                          # not this folder's, or from before every row there
            cands.setdefault(cwd, []).append((created, meta["id"], path))
        for cwd, rs in by_cwd.items():
            cs = sorted(cands.get(cwd, []), key=lambda c: (c[0], c[2].name))
            rs = sorted(rs, key=lambda r: (_epoch(r["created_at"]), r.get("row_id") or 0))
            if not cs or len(cs) != len(rs):
                continue
            if any(created < _epoch(r["created_at"]) - BIND_SLACK for r, (created, _sid, _p) in zip(rs, cs)):
                continue                                          # a rollout older than the row it would go to: the pairing is wrong
            for r, (_created, sid, path) in zip(rs, cs):
                n += 1 if _bind(db, r, sid, path) else 0
        return n
    except Exception as e:
        log.warning("codex rollout bind failed: %s", e.__class__.__name__)
        return 0


# ------------------------------------------------------------------ the row stats
def build_stats(meta: dict | None, parsed: dict, known: dict | None = None) -> dict:
    """A codex row's `stats` (the keys the statusline gives a Claude row, the ones Codex has): model, model_id, context_pct (100 x the LAST
    call's total tokens / the window, never the cumulative figure; capped at 100), context_size, cost_usd (None: ccusage prices it),
    version, effort, approval, sandbox, tokens {input, cached, output, reasoning, total (cumulative), last (the last call's total)}.
    `known` holds the settings seen earlier on this rollout, for a tail that no longer reaches a turn_context."""
    meta = meta or {}
    cfg = {**(known or {}), **{k: v for k, v in (parsed.get("config") or {}).items() if k != "at" and v is not None}}
    last, total = parsed.get("last_token_usage"), parsed.get("total_token_usage")
    size = parsed.get("model_context_window") or meta.get("context_window")
    pct = None
    if last and last.get("total") is not None and size:
        pct = round(min(100.0, 100.0 * last["total"] / size), 1)
    tokens = None
    if last or total:
        tokens = {**(total or {}), "last": (last or {}).get("total")}
        tokens = {k: v for k, v in tokens.items() if v is not None}
    return {"model": cfg.get("model"), "model_id": cfg.get("model"), "context_pct": pct, "context_size": size, "cost_usd": None,
            "version": meta.get("cli_version"), "effort": cfg.get("effort"), "approval": cfg.get("approval"),
            "sandbox": cfg.get("sandbox"), "tokens": tokens}


def _outcome(pending: dict, stats: dict, cfg: dict | None, now: float) -> bool | None:
    """Did the rollout confirm the pending /model or /reasoning? True confirmed, False unconfirmed (CMD_PENDING_TTL passed), None keep
    waiting. Only a turn_context / thread_settings_applied stamped AFTER the command was typed counts (an old one would confirm every
    command at once): the model contains the argument or differs from the one at send time; the effort equals it or differs."""
    at = _epoch(pending.get("at"))
    if at is None:
        return False
    event_at = _epoch((cfg or {}).get("at"))
    if event_at is not None and event_at >= at - 1:
        cmd, arg = pending.get("cmd"), str(pending.get("arg") or "").strip().lower()
        before = pending.get("before") if isinstance(pending.get("before"), dict) else {}
        if cmd == "model":
            want = arg.replace("[1m]", "").strip()
            hay = str(stats.get("model") or "").lower()
            known = before.get("model") is not None or before.get("model_id") is not None
            changed = known and stats.get("model") != before.get("model") and stats.get("model") != before.get("model_id")
            if changed or (want and want in hay):
                return True
        elif cmd in ("reasoning", "effort"):
            eff = stats.get("effort")
            if eff is not None and (eff != before.get("effort") or (arg and str(eff).lower() == arg)):
                return True
        else:
            return True
    return False if now - at > CMD_PENDING_TTL else None


# ------------------------------------------------------------------ the Tailer
class Tailer:
    """Per open codex row: the rollout's newest stats into the row (display only: db.set_stats, never a state), the samples ctx / ctx_tok /
    stok, and the confirmation of a pending /model or /reasoning; every RL_EVERY seconds the account rate limits. `step(db, now)` is one
    pass (the Sampler's hook calls it every tick; it keeps its own TAIL_EVERY / RL_EVERY gaps, so a faster tick does not make it faster).
    Every part is guarded: a failing row or a failing poll never stops the rest or the Sampler."""

    def __init__(self):
        self._rows: dict = {}                    # row id -> {path, sig, parsed, meta, cfg, retry}
        self._last_tail = 0.0
        self._last_rl = 0.0

    def reset(self) -> None:
        self._rows.clear()
        self._last_tail = self._last_rl = 0.0

    def step(self, db, now: float | None = None) -> None:
        now = time.time() if now is None else float(now)
        if now - self._last_tail >= TAIL_EVERY or now < self._last_tail:
            self._last_tail = now
            try:
                self.tail_rows(db, now)
            except Exception as e:
                log.warning("codex rollout tail failed: %s", e.__class__.__name__)
        if now - self._last_rl >= RL_EVERY or now < self._last_rl:
            self._last_rl = now
            try:
                self.rate_limits(db, now)
            except Exception as e:
                log.warning("codex rate limits failed: %s", e.__class__.__name__)

    # ---- rows
    def tail_rows(self, db, now: float) -> int:
        """Bind what can be bound, then refresh every open codex row. Returns the rows whose stats were written."""
        bind_unbound_rows(db)
        rows = {n: r for n, r in db.open_rows().items() if r.get("agent") == "codex"}
        wrote = 0
        for name, row in rows.items():
            try:
                wrote += 1 if self._tail_row(db, name, row, now) else 0
            except Exception as e:
                log.warning("codex rollout tail of a session failed: %s", e.__class__.__name__)
        live = {r.get("row_id") for r in rows.values()}
        for k in [k for k in self._rows if k not in live]:
            del self._rows[k]
        return wrote

    def _path(self, st: dict, row: dict, now: float) -> Path | None:
        if st.get("path") is not None:
            return st["path"]
        if now - st.get("retry", -PATH_RETRY) < PATH_RETRY:
            return None
        st["retry"] = now
        path = get_agent("codex").transcript_path(row)
        if path is not None:
            st["path"] = path
        return path

    def _tail_row(self, db, name: str, row: dict, now: float) -> bool:
        st = self._rows.setdefault(row.get("row_id"), {})
        path = self._path(st, row, now)
        if path is None:
            return False
        try:
            fs = os.stat(path)
        except OSError:
            st.pop("path", None)
            return False
        sig = (fs.st_mtime_ns, fs.st_size)
        if st.get("sig") != sig or "parsed" not in st:
            st["parsed"] = parse_tail(read_tail(path))
            st["sig"] = sig
            if st.get("meta") is None:
                st["meta"] = _meta(path)
            cfg = st["parsed"].get("config") or {}
            st["cfg"] = {**st.get("cfg", {}), **{k: v for k, v in cfg.items() if k != "at" and v is not None}}
        parsed = st["parsed"]
        stats = build_stats(st.get("meta"), parsed, st.get("cfg"))
        wrote = False
        if row.get("stats") != stats:
            db.set_stats(name, stats)
            wrote = True
        self._samples(db, name, stats)
        self._confirm(db, name, row, stats, parsed, now)
        return wrote

    @staticmethod
    def _samples(db, name: str, stats: dict) -> None:
        from .. import samples
        tok = stats.get("tokens") or {}
        pct = stats.get("context_pct")
        if pct is not None:
            meta = {k: v for k, v in (("model", stats.get("model")), ("window", stats.get("context_size"))) if v is not None} or None
            if samples.record(db, "ctx", name, pct, meta) and tok.get("last") is not None:
                samples.record(db, "ctx_tok", name, tok["last"], None, force=True)
        if tok.get("total") is not None:
            samples.record(db, "stok", name, tok["total"])

    @staticmethod
    def _confirm(db, name: str, row: dict, stats: dict, parsed: dict, now: float) -> None:
        pending = (row.get("flags") or {}).get("pending_cmd")
        if not isinstance(pending, dict):
            return
        done = _outcome(pending, stats, parsed.get("config"), now)
        if done is not None:
            db.update_flags(name, {"last_cmd": {"cmd": pending.get("cmd"), "arg": pending.get("arg"), "at": pending.get("at"),
                                                "confirmed": done}, "pending_cmd": None})

    # ---- rate limits
    def rate_limits(self, db, now: float) -> dict:
        """One poll of the account's rate limits (see the module docstring). Returns {account key | None: the reading written for it}."""
        accts, current = _accounts(db)
        grouped: dict = {}
        for rl, at, account_id in _readings(now):
            grouped.setdefault(_owner(accts, current, account_id), []).append((rl, at))
        results = {}
        for key, readings in grouped.items():
            sel = _select(readings, now)
            if sel is None:
                continue
            results[key] = sel
            mine = key is None or key == current
            _write_samples(db, key, sel, now, with_codex=mine)
            if mine:
                _write_kv(db, key, sel)
            _announce(db, key, sel, accts, now)
        return {k: v["value"] for k, v in results.items()}


# ---- the rate-limit pass, as functions so a test can drive each step
def _accounts(db) -> tuple[dict, str | None]:
    """({key: record} from kv codex_accounts, the current key from kv codex_account_current): read directly, never through codex_accounts."""
    def val(key):
        rec = db.kv_get(key)
        return rec.get("value") if isinstance(rec, dict) else None
    accts, cur = val(KV_ACCOUNTS), val(KV_CURRENT)
    accts = {str(k): r for k, r in accts.items() if isinstance(r, dict)} if isinstance(accts, dict) else {}
    key = cur.get("key") if isinstance(cur, dict) else None
    return accts, key if isinstance(key, str) and key else None


def _owner(accts: dict, current: str | None, account_id: str | None) -> str | None:
    """The account key a reading belongs to: the record whose account_id the rollout's creator_account_id equals, else the current account
    (None when none is known: the reading then only feeds key `codex` and the kv)."""
    if account_id:
        for key, rec in accts.items():
            if rec.get("account_id") == account_id:
                return key
    return current


def _rollout_reading(path: Path, mtime: float, size: int) -> dict | None:
    """{rl, at, account_id} of one rollout: its newest rate_limits, when they were reported and whose session it is. Remembered while the
    file is unchanged."""
    sig, key = (mtime, size), str(path)
    with _lock:
        hit = _rl_cache.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    parsed = parse_tail(read_tail(path))
    out = None
    if parsed["rate_limits"]:
        meta = _meta(path) or {}
        out = {"rl": parsed["rate_limits"], "at": _epoch(parsed["rate_limits_at"]) or mtime, "account_id": meta.get("account_id")}
    with _lock:
        if len(_rl_cache) >= CACHE_MAX:
            for k in list(_rl_cache)[:CACHE_MAX // 2]:
                del _rl_cache[k]
        _rl_cache[key] = (sig, out)
    return out


def _readings(now: float) -> list[tuple[dict, float, str | None]]:
    """(rate_limits, reported at, creator_account_id) of the newest MAX_ROLLOUTS rollouts modified within RL_LOOKBACK_DAYS."""
    out = []
    for mtime, size, path in recent_rollouts(now - RL_LOOKBACK_DAYS * 86400)[:MAX_ROLLOUTS]:
        r = _rollout_reading(path, mtime, size)
        if r:
            out.append((r["rl"], min(r["at"], now), r["account_id"]))
    return out


def window_kind(minutes) -> str:
    """'5h' (window_minutes <= 360), '7d' (>= 7200), else '<minutes>m' (shown by the pill and the kv, never charted)."""
    m = _int(minutes)
    if m is None:
        return "other"
    return "5h" if m <= 360 else "7d" if m >= 7200 else f"{m}m"


def _select(readings: list[tuple[dict, float]], now: float) -> dict | None:
    """One account's readings -> {value: the kv shape, windows: {kind: (window, at)}} or None. The newest reading per limit_id, an id other
    than `codex` with every window at zero dropped (when nothing else is left the zero stands), windows that have reset dropped (when
    every window has, the newest reading stands as it was and is never `reached`), then the highest used_percent per window kind."""
    newest: dict = {}
    for rl, at in readings:
        lid = rl.get("limit_id") or MAIN_LIMIT
        if lid not in newest or at > newest[lid][1]:
            newest[lid] = (rl, at)

    def wins(rl):
        return [w for w in (rl.get("primary"), rl.get("secondary")) if w]
    usable = {lid: v for lid, v in newest.items() if lid == MAIN_LIMIT or any(w["used_percent"] > 0 for w in wins(v[0]))} or newest
    live = {}
    for lid, (rl, at) in usable.items():
        ws = [w for w in wins(rl) if w.get("resets_at") is None or w["resets_at"] > now]
        if ws:
            live[lid] = (rl, at, ws)
    expired = not live
    if expired:
        lid, (rl, at) = max(usable.items(), key=lambda kv: kv[1][1])
        live = {lid: (rl, at, wins(rl))}
    best: dict = {}
    for lid, (rl, at, ws) in live.items():
        for w in ws:
            kind = window_kind(w.get("window_minutes"))
            cur = best.get(kind)
            if cur is None or (w["used_percent"], at) > (cur[0]["used_percent"], cur[1]):
                best[kind] = (w, at)
    main_id = MAIN_LIMIT if MAIN_LIMIT in live else max(live, key=lambda k: live[k][1])
    main_rl = live[main_id][0]
    order = sorted(best, key=lambda k: (0, 0) if k == "5h" else (0, 1) if k == "7d" else (1, _int(best[k][0].get("window_minutes")) or 0))
    shown = [best[k][0] for k in order[:2]]
    observed = max([live[main_id][1]] + [best[k][1] for k in order[:2]])
    plan = main_rl.get("plan_type") or next((live[k][0].get("plan_type") for k in live if live[k][0].get("plan_type")), None)
    value = {"limit_id": main_id, "plan_type": plan, "primary": shown[0] if shown else None, "secondary": shown[1] if len(shown) > 1 else None,
             "credits": main_rl.get("credits"), "reached": (not expired) and any(w["used_percent"] >= 100 for w in shown),
             "observed_at": _iso(observed), "account": None}
    return {"value": value, "windows": {k: best[k] for k in order}, "expired": expired}


def _write_samples(db, key: str | None, sel: dict, now: float, *, with_codex: bool) -> list[str]:
    """rl_5h / rl_7d samples of one account's selected windows: key `cacct:<key>` (when the account is known) and, for the current account,
    `codex`. Stamped with the event's time and never older than the last sample of the key. Returns the 'series:key' written."""
    from .. import samples
    keys = ([f"cacct:{key}"] if key else []) + (["codex"] if with_codex else [])
    wrote = []
    for kind, series in (("5h", "rl_5h"), ("7d", "rl_7d")):
        hit = sel["windows"].get(kind)
        if hit is None:
            continue
        w, at = hit
        for k in keys:
            last = db.sample_last(series, k)
            when = at
            if last is not None and _iso(at) < last["at"]:
                if k != "codex" or (last.get("meta") or {}).get("acct") == key:
                    continue                                      # nothing older than what the series already has
                when = _epoch(last["at"])                         # the current account changed: its newest reading, stamped no earlier than the series' last point
            meta = {"resets_at": w.get("resets_at"), "acct": key if k == "codex" else None}
            meta = {a: b for a, b in meta.items() if b is not None} or None
            if samples.record(db, series, k, w["used_percent"], meta, at=when):
                wrote.append(f"{series}:{k}")
    return wrote


def _write_kv(db, key: str | None, sel: dict) -> bool:
    """kv rate_limits_codex (state.usage_codex) for the current account's reading. An older reading of the same account never replaces a
    newer one; nothing is written when the value would be the one already there."""
    value = {**sel["value"], "account": key}
    rec = db.kv_get(KV_RATE)
    old = rec.get("value") if isinstance(rec, dict) else None
    if isinstance(old, dict):
        if old == value:
            return False
        if old.get("account") == key and str(old.get("observed_at") or "") > value["observed_at"]:
            return False
    db.kv_set(KV_RATE, value)
    return True


def _announce(db, key: str | None, sel: dict, accts: dict, now: float) -> bool:
    """The one notice per reached window: kv codex_rl_notified_<resets_at> remembers it (claimed only when a channel exists, so a box that
    adds ntfy later is still told). Only a window that has not reset counts. Never raises."""
    try:
        if sel["expired"]:
            return False
        reached = [w for w in (sel["value"]["primary"], sel["value"]["secondary"]) if w and w["used_percent"] >= 100]
        if not reached:
            return False
        from .. import notify
        if not notify.any_channel():
            return False
        label = (accts.get(key) or {}).get("label") if key else None
        sent = False
        for w in reached:
            marker = f"{NOTIFIED}{w['resets_at'] if w.get('resets_at') is not None else 'x' + str(int(now // 3600))}"
            if db.kv_get(marker) is not None:
                continue
            db.kv_set(marker, {"account": key, "minutes": w.get("window_minutes")})
            sent = _send_notice(notify, w, sel["value"].get("plan_type"), label) or sent
        return sent
    except Exception as e:
        log.warning("codex rate-limit notice failed: %s", e.__class__.__name__)
        return False


def _send_notice(notify, w: dict, plan: str | None, label: str | None) -> bool:
    kind = window_kind(w.get("window_minutes"))
    name = {"5h": "5-hour", "7d": "weekly"}.get(kind, kind)
    lines = [f"{name} window used {w['used_percent']:.0f}%"]
    if w.get("resets_at") is not None:
        lines.append("resets " + datetime.fromtimestamp(w["resets_at"], tz=timezone.utc).strftime("%a %H:%M UTC"))
    if label or plan:
        lines.append(" · ".join(x for x in (label, plan) if x))
    pub = (settings.public_url or "").rstrip("/")
    link = f"{pub}/#/usage" if pub else None
    n = notify.Notice(title="Codex rate limited", body="\n".join(lines), click=link, url=link or "/#/usage", tag="rate-limit-codex", priority=4,
                      tags=["rotating_light"], actions=[], web_actions=[], kind="rate_limit", state="errored", tmux="", agent="codex",
                      path="/#/usage")
    return bool(notify.send(n))


_tailer = Tailer()


def tick(db, now: float | None = None) -> None:
    """The Sampler's TICK_HOOKS entry (samples._register_hooks): one Tailer pass. Never raises."""
    try:
        _tailer.step(db, now)
    except Exception as e:
        log.warning("codex rollout tick failed: %s", e.__class__.__name__)


# ------------------------------------------------------------------ discovery (slice B)
def discover(*args, **kwargs) -> list[dict]:
    """The Codex threads on this box that the board did not start (state_*.sqlite read-only, plus the rollouts it missed): see
    agents/codex_discovery.py, which owns the implementation (its own first-line reader, the 14-day window, the Desktop-import and
    subagent filters, the `badge` for originators other than codex-tui). Kept here as the plan names it."""
    from . import codex_discovery
    return codex_discovery.discover(*args, **kwargs)
