"""Cost per project / repo / task from `ccusage session --json`, joined on the agent session ids ccboard recorded for its own
sessions and tasks (sessions started outside the board are not attributed). Claude and Codex both: ccusage's unified output lists a
row per session for each agent, and `ccusage codex session --json --offline` fills the Codex half in when the unified output has none.

The join key is a uuid in the last path component of the row's `period` / `sessionId` / `sessionFile`: a bare uuid (Claude) or
`YYYY/MM/DD/rollout-<ts>-<uuid>` (Codex). A workflow or subagent transcript (`<parent-uuid>/subagents/workflows/wf_<id>/...`) has no
uuid of its own: its cost rolls up into the parent session's entry, so the parent and its children are one session here."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

from pathlib import Path

from . import accounts, pricing, projects, samples
from .agents import registry
from .config import settings

log = logging.getLogger("ccboard.cost")
UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_UUID_FULL = re.compile(f"^{_UUID}$")
KV_COST = "cost"
AGENTS = ("claude", "codex")
UNPRICED_TOP = 20          # how many unpriced sessions the kv record names (the full count and token total ride along)
CODEX_FALLBACK_TIMEOUT = 240
_TOKEN_FIELDS = ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens", "cachedInputTokens", "reasoningOutputTokens")
_PATH_FIELDS = ("period", "sessionId", "sessionFile", "projectPath", "project", "directory", "cwd")

# Sessions started outside the board (issue #57): the folder a Claude session ran in comes from Claude Code's registry (<config dir>/sessions/<pid>.json)
# and from the `cwd` field of the first lines of its transcript (<config dir>/projects/<encoded folder>/<session id>.jsonl; the encoded name is lossy,
# so it is never decoded). Both are display and usage sources only, never a state source. The map lives in the session_cwd table.
OUTSIDE = "(outside projects)"       # a session whose folder is known but is not under the projects directory
HEAD_LINES = 20                      # lines of a transcript read for its cwd
HEAD_BYTES = 256 * 1024              # and never more than this many bytes of them
FILES_PER_PASS = 150                 # transcripts read per cost refresh, newest first
RETRY_EMPTY_S = 6 * 3600             # a transcript that named no folder is looked at again after this long


def join_key(value) -> tuple[str, bool] | None:
    """The session uuid a ccusage row name stands for: (uuid lower case, is_child). A uuid ending the name (bare, or after
    `rollout-<ts>-` / a path) is the session itself; a name that has a uuid path component followed by `subagents` or a `wf_*` run
    is a child of that session (its parent's uuid, is_child True). None when the name has neither."""
    s = str(value or "").strip().replace("\\", "/")
    if s.endswith(".jsonl"):
        s = s[:-6]
    s = s.rstrip("/")
    if len(s) >= 36 and _UUID_FULL.match(s[-36:]) and (len(s) == 36 or s[-37] in "/-_"):
        return s[-36:].lower(), False
    parts = s.split("/")
    for i, seg in enumerate(parts):
        if _UUID_FULL.match(seg):
            if any(p == "subagents" or p.startswith("wf_") for p in parts[i + 1:]):
                return seg.lower(), True
            break
    return None


def _is_observer(row: dict) -> bool:
    """claude-mem's observer sessions (a path segment `observer-sessions`, or the project slug of such a cwd) are not the user's work:
    kept out of the join and the totals, like the hooks and the registry do."""
    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    for src in (row, meta):
        for k in _PATH_FIELDS:
            v = src.get(k)
            if isinstance(v, str) and any(seg == "observer-sessions" or "claude-mem-observer-sessions" in seg
                                          for seg in v.replace("\\", "/").split("/")):
                return True
    return False


def _agent_of(row: dict, default: str | None) -> str | None:
    a = row.get("agent")
    if isinstance(a, str) and a.strip():
        a = a.strip().lower()
        return "codex" if "codex" in a else "claude" if "claude" in a else a
    if any("rollout-" in str(row.get(k) or "") for k in ("period", "sessionId", "sessionFile")):
        return "codex"
    return default or "claude"


def _num(*vals) -> float:
    for v in vals:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return 0.0


def _tokens(r: dict, meta: dict) -> int:
    t = r.get("totalTokens", meta.get("totalTokens"))
    if isinstance(t, (int, float)) and not isinstance(t, bool):
        return int(t)
    return int(sum(_num(r.get(k)) for k in _TOKEN_FIELDS))


def _models(r: dict, meta: dict) -> list[str]:
    models = r.get("modelsUsed") or meta.get("modelsUsed") or r.get("models") or meta.get("models") or []
    if isinstance(models, dict):                      # @ccusage/codex: {"gpt-5": {...usage}}
        models = list(models)
    return [m for m in models if isinstance(m, str)][:6] if isinstance(models, list) else []


BASIS_RANK = {None: 0, "list": 1, "sibling": 2, "rough": 3}      # the weakest basis among a session's estimated parts is the one it reports


def weaker(a: str | None, b: str | None) -> str | None:
    return a if BASIS_RANK.get(a, 0) >= BASIS_RANK.get(b, 0) else b


def _tok_split(src: dict) -> dict:
    return {"input": _num(src.get("inputTokens")), "output": _num(src.get("outputTokens")),
            "cache_creation": _num(src.get("cacheCreationTokens")), "cache_read": _num(src.get("cacheReadTokens"), src.get("cachedInputTokens"))}


def _breakdown(r: dict, meta: dict) -> list[dict]:
    """The per-model rows of a ccusage session row (`modelBreakdowns`: model name, the four token counts, the cost, `missingPricing`)."""
    raw = r.get("modelBreakdowns") if isinstance(r.get("modelBreakdowns"), list) else meta.get("modelBreakdowns")
    out = []
    for b in raw if isinstance(raw, list) else []:
        name = b.get("modelName") or b.get("model") if isinstance(b, dict) else None
        if isinstance(name, str) and name:
            out.append({"model": name, "cost": _num(b.get("cost"), b.get("totalCost")), "missing": bool(b.get("missingPricing")), "tok": _tok_split(b)})
    return out


def _estimate(r: dict, meta: dict, cost: float, tokens: int, models: list[str]) -> dict:
    """{est, est_basis, est_cwa}: the reported cost plus a list-price estimate for the parts ccusage prices at zero (issue #95). est == cost and est_basis None
    when nothing needed estimating. A part no price can be found for adds nothing and stays unpriced; it is never guessed."""
    out = {"est": cost, "est_basis": None, "est_cwa": False}
    if tokens <= 0:
        return out
    bd = _breakdown(r, meta)
    add, basis, cwa = 0.0, None, False
    if bd:
        for b in bd:
            if sum(b["tok"].values()) > 0 and (b["missing"] or b["cost"] <= 0):
                e = pricing.estimate_model(b["model"], b["tok"])
                if e:
                    add, basis, cwa = add + e["usd"], weaker(basis, e["basis"]), cwa or e["cache_write_assumed"]
    elif cost <= 0 and models:
        split = _tok_split(r)
        e = pricing.estimate_model(models[0], split) if len(models) == 1 and sum(split.values()) > 0 else None
        if e is None and sum(split.values()) > 0:
            e = pricing.estimate_rough(models, split)
        if e:
            add, basis, cwa = e["usd"], e["basis"], e["cache_write_assumed"]
    if basis:
        out.update(est=cost + add, est_basis=basis, est_cwa=cwa)
    return out


def parse_sessions(raw: str, default_agent: str | None = None) -> dict[str, dict]:
    """ccusage session JSON -> {session uuid: {agent, cost, tokens, last, models, unpriced, subs}}. Rows for other agents (ccusage knows
    more than Claude and Codex), observer sessions and rows with no uuid are left out; workflow / subagent rows add into their parent's
    entry (`subs` counts them); `unpriced` = tokens but a zero price (ccusage has no rate for the model). `default_agent` names the
    agent of rows that do not say (the `ccusage codex` fallback passes 'codex')."""
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {}
    if isinstance(data, dict):
        rows = data.get("session") if isinstance(data.get("session"), list) else data.get("sessions")
    else:
        rows = data
    out: dict[str, dict] = {}
    children: list[tuple[str, dict]] = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or _is_observer(r):
            continue
        agent = _agent_of(r, default_agent)
        if agent not in AGENTS:
            continue
        found = None
        for k in ("period", "sessionId", "sessionFile"):
            found = join_key(r.get(k))
            if found:
                break
        if not found:
            continue
        sid, child = found
        meta = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
        e = {"agent": agent, "cost": _num(r.get("totalCost"), r.get("costUSD"), r.get("cost"), r.get("total_cost")),
             "tokens": _tokens(r, meta), "last": meta.get("lastActivity") or r.get("lastActivity"),
             "models": _models(r, meta), "subs": 0}
        e.update(_estimate(r, meta, e["cost"], e["tokens"], e["models"]) if agent == "claude" else {"est": e["cost"], "est_basis": None, "est_cwa": False})
        if child:
            children.append((sid, e))
        else:
            out[sid] = e                                  # a repeated plain row: the later one stands, as before
    for sid, e in children:                               # after the plain rows, so a parent listed later never overwrites its children
        p = out.get(sid)
        if p is None:
            e["subs"] = 1
            out[sid] = e
            continue
        p["cost"] += e["cost"]
        p["est"] += e["est"]
        p["est_basis"] = weaker(p["est_basis"], e["est_basis"])
        p["est_cwa"] = p["est_cwa"] or e["est_cwa"]
        p["tokens"] += e["tokens"]
        p["subs"] += 1
        if e["last"] and (not p["last"] or str(e["last"]) > str(p["last"])):
            p["last"] = e["last"]
        p["models"] = (p["models"] + [m for m in e["models"] if m not in p["models"]])[:6]
    for e in out.values():
        e["unpriced"] = e["tokens"] > 0 and e["cost"] <= 0
    return out


def _run_ccusage(exe: str, args: list[str], env: dict | None = None) -> str | None:
    try:
        cp = subprocess.run([exe, *args], capture_output=True, text=True, timeout=CODEX_FALLBACK_TIMEOUT, env=env)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return cp.stdout if cp.returncode == 0 else None


def collect(run=None, which=None) -> dict[str, dict] | None:
    """The ccusage sessions of both agents, or None when ccusage is missing or its unified listing fails. When the unified listing has no
    Codex row but this box has Codex rollouts, `ccusage codex session --json --offline` (with CODEX_HOME set) is asked for them; a failing
    fallback only leaves the Codex half empty. `run(exe, args, env=None) -> stdout | None` and `which(name)` are the test seams."""
    run = run or _run_ccusage
    exe = (which or shutil.which)("ccusage")
    if not exe:
        return None
    out = run(exe, ["session", "--json"])
    if out is None:
        return None
    costs = parse_sessions(out)
    if not any(c["agent"] == "codex" for c in costs.values()) and (settings.codex_home / "sessions").is_dir():
        extra = run(exe, ["codex", "session", "--json", "--offline"], {**os.environ, "CODEX_HOME": str(settings.codex_home)})
        for sid, e in parse_sessions(extra or "", "codex").items():
            costs.setdefault(sid, e)
    return costs


def fetch_sessions() -> dict[str, dict] | None:
    return collect()


def transcript_files(config_dir: Path) -> dict[str, Path]:
    """{session id (lower case): transcript path} for the top-level transcripts `<config dir>/projects/<folder>/<uuid>.jsonl`. Names only: nothing is
    opened. Subagent and workflow transcripts live deeper and are not listed (their cost rolls up into the parent)."""
    out: dict[str, Path] = {}
    root = Path(config_dir) / "projects"
    try:
        dirs = [d for d in os.scandir(root) if d.is_dir(follow_symlinks=False)]
    except OSError:
        return out
    for d in dirs:
        try:
            with os.scandir(d.path) as it:
                for f in it:
                    n = f.name
                    if n.endswith(".jsonl") and _UUID_FULL.match(n[:-6]) and f.is_file(follow_symlinks=False):
                        out.setdefault(n[:-6].lower(), Path(f.path))
        except OSError:
            continue
    return out


def head_cwd(path: Path) -> str | None:
    """The `cwd` of the first transcript line that has one, reading at most HEAD_LINES lines and HEAD_BYTES bytes. A corrupt line is skipped."""
    try:
        with open(path, "rb") as f:
            raw = f.read(HEAD_BYTES)
    except OSError:
        return None
    for line in raw.split(b"\n")[:HEAD_LINES]:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        cwd = obj.get("cwd") if isinstance(obj, dict) else None
        if isinstance(cwd, str) and cwd.strip():
            return cwd.strip()
    return None


def learn_folders(db, costs: dict[str, dict], owned: set[str], config_dir: Path | None = None, now: datetime | None = None) -> dict:
    """Fill the session_cwd map for the Claude sessions ccusage lists that no board row owns: first from the registry snapshot (running and recent
    sessions; the files disappear when the process ends, the map keeps what it saw), then from the first lines of their transcripts, newest first,
    at most FILES_PER_PASS per pass. Returns {registry, scanned, remaining} for the log. Never raises."""
    cfg = Path(config_dir if config_dir is not None else settings.claude_config_dir)
    stats = {"registry": 0, "scanned": 0, "remaining": 0}
    try:
        known = db.session_cwds()
        reg = registry.session_cwds(cfg)
        fresh = [(sid, cwd) for sid, cwd in reg.items() if sid not in owned and not (known.get(sid) or ("", ""))[0]]
        if fresh:
            stats["registry"] = db.session_cwd_put(fresh, now)
            known = db.session_cwds()
        stamp = (now or _now()).timestamp()
        want = []
        for sid, c in costs.items():
            if (c.get("agent") or "claude") != "claude" or sid in owned:
                continue
            have = known.get(sid)
            if have and have[0]:
                continue
            if have:                                                    # looked before and found nothing: not again for a while
                try:
                    if stamp - datetime.fromisoformat(have[1].replace("Z", "+00:00")).timestamp() < RETRY_EMPTY_S:
                        continue
                except ValueError:
                    pass
            want.append(sid)
        if not want:
            return stats
        files = transcript_files(cfg)
        found = []
        for sid in want:
            f = files.get(sid)
            if f is None:
                continue
            try:
                found.append((f.stat().st_mtime, sid, f))
            except OSError:
                continue
        found.sort(reverse=True)
        batch = found[:FILES_PER_PASS]
        rows = [(sid, head_cwd(f) or "") for _m, sid, f in batch]
        rows += [(sid, "") for sid in want if sid not in files]         # no transcript on disk: remember that, retry later
        stats["scanned"] = len(batch)
        stats["remaining"] = max(0, len(found) - len(batch))
        if rows:
            db.session_cwd_put(rows, now)
    except Exception as e:
        log.warning("folder join failed: %s", e)
    return stats


def folder_rows(db, costs: dict[str, dict], owned: set[str]) -> tuple[list[dict], set[str]]:
    """(rows, outside): the sessions no board row owns whose folder is known. rows are session-row shaped ({project, repo, claude_session_id, agent,
    via: 'folder'}) for the ones under the projects directory; `outside` holds the ids whose folder is elsewhere. A session with no folder is in neither."""
    rows: list[dict] = []
    outside: set[str] = set()
    try:
        known = db.session_cwds()
    except Exception as e:
        log.warning("folder map unreadable: %s", e)
        return rows, outside
    for sid, c in costs.items():
        if sid in owned or (c.get("agent") or "claude") != "claude":
            continue
        cwd = (known.get(sid) or ("", ""))[0]
        if not cwd:
            continue
        pr = projects.repo_for_cwd(cwd)
        if pr:
            rows.append({"project": pr[0], "repo": pr[1], "claude_session_id": sid, "agent": "claude", "via": "folder"})
        else:
            outside.add(sid)
    return rows, outside


def _day(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).date().isoformat()
    except ValueError:
        return None


def _bucket() -> dict:
    return {"total": 0.0, "today": 0.0, "week": 0.0, "tokens": 0, "est": 0.0}


def _by_agent() -> dict[str, dict]:
    return {a: _bucket() for a in AGENTS}


def _add(buckets: dict[str, dict], c: dict, today: str, week_start: str) -> None:
    b = buckets.setdefault(c.get("agent") or "claude", _bucket())
    b["total"] += c["cost"]
    b["est"] += c.get("est", c["cost"])
    b["tokens"] += int(c.get("tokens") or 0)
    day = _day(c.get("last"))
    if day == today:
        b["today"] += c["cost"]
    if day and day > week_start:
        b["week"] += c["cost"]


def _rounded(buckets: dict[str, dict]) -> dict[str, dict]:
    """total / today / week / tokens per agent; `est` (the total with list-price estimates for models ccusage prices at zero) only where it differs from `total`."""
    out = {}
    for a, b in buckets.items():
        d = {"total": round(b["total"], 4), "today": round(b["today"], 4), "week": round(b["week"], 4), "tokens": int(b["tokens"])}
        if abs(b["est"] - b["total"]) > 5e-5:
            d["est"] = round(b["est"], 4)
        out[a] = d
    return out


def attribute(costs: dict[str, dict], session_rows: list[dict], task_rows: list[dict], now: datetime | None = None,
              folder: list[dict] | None = None, outside: set[str] | None = None) -> dict:
    """Join costs to projects/repos/tasks. session_rows: [{project, repo, claude_session_id (the agent session id, either agent),
    agent}] (all rows, open or ended); task_rows: [{id, claude_session_id}]. Returns {projects:{p:{total,today,week,repos:{r:total},
    by_agent}}, tasks:{id:cost}, attributed, sessions_known, total_all, by_agent}. by_agent is {claude, codex}: {total, today, week,
    tokens}, over every ccusage session globally (ccusage is the total: never a sum of per-thread counters) and over the attributed
    sessions of each project. today / week count a session whole by its last-activity day. `folder` (issue #57) are rows joined by the folder a session
    ran in: they count into the project's totals after the board's own rows (a board row wins for the same id) and never into `tasks`; `outside`
    holds ids whose folder is not under the projects directory (reported as `outside_sessions`). Only the grouping moves: total_all is unchanged."""
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    week_start = (now - timedelta(days=7)).date().isoformat()
    projects: dict[str, dict] = {}
    seen: set[str] = set()
    joined = 0
    for row in [*session_rows, *(folder or [])]:
        sid = (row.get("claude_session_id") or row.get("agent_session_id") or "").lower()
        c = costs.get(sid)
        if not c or sid in seen:
            continue
        seen.add(sid)
        joined += 1 if row.get("via") == "folder" else 0
        p = projects.setdefault(row["project"], {"total": 0.0, "today": 0.0, "week": 0.0, "est": 0.0, "repos": {}, "by_agent": _by_agent()})
        p["total"] += c["cost"]
        p["est"] += c.get("est", c["cost"])
        p["repos"][row["repo"]] = p["repos"].get(row["repo"], 0.0) + c["cost"]
        day = _day(c.get("last"))
        if day == today:
            p["today"] += c["cost"]
        if day and day > week_start:
            p["week"] += c["cost"]
        _add(p["by_agent"], c, today, week_start)
    tasks = {}
    for t in task_rows:
        sid = (t.get("claude_session_id") or "").lower()
        if sid in costs:
            tasks[t["id"]] = round(costs[sid]["cost"], 4)
    for p in projects.values():
        for k in ("total", "today", "week"):
            p[k] = round(p[k], 4)
        if abs(p["est"] - p["total"]) > 5e-5:
            p["est"] = round(p["est"], 4)
        else:
            del p["est"]
        p["repos"] = {r: round(v, 4) for r, v in p["repos"].items()}
        p["by_agent"] = _rounded(p["by_agent"])
    overall = _by_agent()
    for c in costs.values():
        _add(overall, c, today, week_start)
    return {"projects": projects, "tasks": tasks, "attributed": len(seen), "joined_by_folder": joined,
            "outside_sessions": len([s for s in (outside or ()) if s in costs and s not in seen]), "sessions_known": len(costs),
            "total_all": round(sum(c["cost"] for c in costs.values()), 2), "by_agent": _rounded(overall),
            **({"total_all_est": round(sum(c.get("est", c["cost"]) for c in costs.values()), 2)}
               if abs(sum(c.get("est", c["cost"]) - c["cost"] for c in costs.values())) > 5e-3 else {})}


def session_samples(costs: dict[str, dict], session_rows: list[dict], folder: list[dict] | None = None,
                    outside: set[str] | None = None) -> list[dict]:
    """One entry per ccusage session, for samples.record_cost: {agent, id, cost, tokens, last, project, repo, models, unpriced}. project
    and repo are None for a session the board did not start. Kept out of the kv record on purpose: /api/state serves that record on
    every poll and ccusage lists thousands of sessions. A session no board row owns takes its project and repo from the folder it ran in (`folder`,
    issue #57) and is marked `via: "folder"`; one that ran outside the projects directory (`outside`) is project OUTSIDE with no repo; one with no known
    folder stays None. The cost figures are never touched: only the grouping moves."""
    owner: dict[str, dict] = {}
    for row in session_rows:
        sid = (row.get("claude_session_id") or row.get("agent_session_id") or "").lower()
        if sid and sid not in owner:
            owner[sid] = row
    joined = {r["claude_session_id"]: r for r in folder or []}
    out = []
    for sid, c in costs.items():
        row = owner.get(sid) or {}
        via = None
        if not row and sid in joined:
            row, via = joined[sid], "folder"
        elif not row and sid in (outside or ()):
            row, via = {"project": OUTSIDE, "repo": None}, "folder"
        out.append({"agent": c.get("agent") or "claude", "id": sid, "cost": c["cost"], "tokens": c["tokens"], "last": c.get("last"),
                    "project": row.get("project"), "repo": row.get("repo"), "models": list(c.get("models") or []),
                    "unpriced": c["tokens"] > 0 and c["cost"] <= 0, "via": via,
                    "est": c.get("est", c["cost"]), "est_basis": c.get("est_basis"), "est_cwa": bool(c.get("est_cwa"))})
    return out


def unpriced(entries: list[dict]) -> dict:
    """Sessions with tokens but a zero price, either agent: ccusage has no rate for the model, so the dollars undercount.
    {sessions, tokens, top[]} (each top entry names its agent; the Usage page badges them 'unpriced')."""
    rows = sorted((e for e in entries if e["tokens"] > 0 and e["cost"] <= 0), key=lambda e: -e["tokens"])
    return {"sessions": len(rows), "tokens": sum(e["tokens"] for e in rows),
            "top": [{"id": e["id"], "agent": e["agent"], "project": e["project"], "repo": e["repo"], "tokens": e["tokens"],
                     "models": e["models"], **({"est_basis": e["est_basis"]} if e.get("est_basis") else {})} for e in rows[:UNPRICED_TOP]]}


def estimate_block(entries: list[dict]) -> dict:
    """What the Usage page says about its estimates (kv 'cost'.estimate): the price table's date and source, how many sessions carry an estimate, the USD
    it adds to the reported figure, the count per basis and whether a cache-write price had to be assumed. Present even when nothing is estimated (the
    footer shows the date)."""
    info = pricing.info()
    est = [e for e in entries if e.get("est_basis")]
    bases: dict[str, int] = {}
    for e in est:
        bases[e["est_basis"]] = bases.get(e["est_basis"], 0) + 1
    return {"date": info["date"], "source": info["source"], "sessions": len(est), "usd": round(sum(e["est"] - e["cost"] for e in est), 2),
            "bases": bases, "cache_write_assumed": any(e.get("est_cwa") for e in est), "cache_write_x": pricing.CACHE_WRITE_FALLBACK_X}


def _tag_accounts(db, entries: list[dict]) -> None:
    """Add `acct` (the subscription account key) to each Claude entry the cost samples will carry: the account its board session last
    ran under (sessions.account, kept current by the statusline attribution), else the account that is current now. Left off when
    neither is known (history from before account tracking). Only Claude entries are tagged: the accounts are Claude subscriptions
    and a Codex session is not billed to one. Best effort: a failure leaves the entries untagged."""
    try:
        by_sid, cur = db.session_accounts(), accounts.current(db)
        for e in entries:
            if (e.get("agent") or "claude") != "claude":
                continue
            acct = by_sid.get(str(e.get("id") or "").lower()) or cur
            if acct:
                e["acct"] = acct
    except Exception as e:
        log.warning("account tags for cost samples failed: %s", e)


def _now() -> datetime:
    return datetime.now(timezone.utc)                 # patched by tests


def refresh(db) -> dict | None:
    costs = fetch_sessions()
    if costs is None:
        return None
    now = _now()
    rows = db.session_ids()
    tasks = db.tasks(include_archived=True)
    owned = {(r.get("claude_session_id") or r.get("agent_session_id") or "").lower() for r in rows}
    owned |= {(t.get("claude_session_id") or "").lower() for t in tasks}
    owned.discard("")
    learn_folders(db, costs, owned, now=now)                      # the folder of sessions started outside the board (issue #57); never raises
    folder, outside = folder_rows(db, costs, owned)
    result = attribute(costs, rows, tasks, now, folder, outside)
    entries = session_samples(costs, rows, folder, outside)
    _tag_accounts(db, entries)
    result["unpriced"] = unpriced(entries)
    result["estimate"] = estimate_block(entries)
    db.kv_set(KV_COST, result)
    for tid, c in result["tasks"].items():
        db.task_update(tid, cost_usd=c)
    try:                                              # history is a bonus: a sampling failure never fails the refresh
        samples.record_cost(db, {**result, "sessions": entries}, now)
    except Exception as e:
        log.warning("cost samples failed: %s", e)
    return result
