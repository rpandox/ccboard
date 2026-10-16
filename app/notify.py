"""Push notifications through a self-hosted ntfy server on the tailnet (loopback from the board) and Web Push.

build() turns one session event into a Notice (pure: title, body lines, click, ntfy actions, priority); send() delivers it on
both channels; notify_session() and notify_rate_limit() are the hook-path entry points and add the throttles."""
from __future__ import annotations

import inspect
import json
import logging
import threading
import time
import urllib.request
from urllib.parse import quote
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .config import settings

from . import push

log = logging.getLogger("ccboard.notify")
_db = None
_clock = time.monotonic          # tests patch this to walk the cooldowns without sleeping


def set_db(db) -> None:
    global _db
    _db = db


def _send_all_takes_extra() -> bool:
    try:
        return "extra" in inspect.signature(push.send_all).parameters
    except (TypeError, ValueError):
        return False


def web_push(title: str, body: str, url: str, tag: str | None = None, extra: dict | None = None) -> int:
    """Web Push to every stored subscription. `extra` (agent, state, tmux, perm_id, actions) is merged into the payload when
    push.send_all accepts it, and dropped otherwise (an older push.py still delivers title/body/url/tag)."""
    if _db is None:
        return 0
    try:
        if extra and _send_all_takes_extra():
            return push.send_all(_db, title, body, url, tag, extra=extra)
        return push.send_all(_db, title, body, url, tag)
    except Exception as e:
        log.warning("web push failed: %s", e)
        return 0


THROTTLE_SECONDS = 60
STATE_COOLDOWN = {"waiting": 30, "done": 120, "errored": 60}      # per (session, state); the longer of this and THROTTLE_SECONDS wins
_last: dict[tuple[str, str], float] = {}                        # (tmux name, state) -> clock of the last notice sent
_rl_mem: set[str] = set()                                       # rate-limit windows told while there is no DB to remember them
RATE_LIMIT_EVERY = 15 * 60                                      # a limit notice for one agent repeats at most this often, whatever the window says
_rl_last: dict[str, float] = {}                                 # agent -> clock of the last limit notice
LOGIN_EVERY = 15 * 60                                           # one "login not valid" notice per account per this long
_login_told: dict[str, float] = {}                              # '<agent>:<account>' -> clock of the last such notice
_team_told: dict[tuple, float] = {}                             # (repository, login, issue) -> clock of the notice: a teammate is told about once per issue
TEAM_TOLD_MAX = 500
_lock = threading.Lock()

TITLES = {"waiting": "needs you", "done": "done", "errored": "error"}
PRIORITY = {"waiting": 4, "errored": 4, "done": 3}
TAGS = {"waiting": ["bell"], "done": ["white_check_mark"], "errored": ["rotating_light"]}
GLYPHS = {"claude": "◆", "codex": "◇", "shell": "▸"}
AGENT_LABELS = {"claude": "Claude", "codex": "Codex", "shell": "Shell"}
PROMPT_CHARS = 120          # the `› last prompt` line
ASKED_CHARS = 200           # the `? what is asked` line (a permission summary keeps its own 300)
# A prompt a harness typed (a finished background task, a pasted wrapper, a slash command echo) is not what the person asked.
# hooks.is_system_turn is the board's one definition; this copy only runs when that module does not have it.
SYSTEM_TURN_PREFIXES = ("<task-notification>", "<system-reminder>", "[SYSTEM NOTIFICATION", "<pasted_content", "<command-name>",
                        "<local-command-stdout>", "<bash-input>")


def enabled() -> bool:
    return bool(settings.ntfy_url and settings.ntfy_topic)


def any_channel() -> bool:
    return enabled() or (_db is not None and bool(_db.push_subs()))


# ---------------------------------------------------------------- per-kind toggles (Settings > Notifications)

PREF_KEYS = ("needs", "done", "limit", "error", "login")        # needs-you, done, rate limit, error / crash, login problem: all on until switched off
TEAM_KEY = "team"                                               # issue #200: "a teammate started on an issue I hold". The one toggle that is OFF until the person switches it on
PREF_KIND = {"permission": "needs", "idle": "needs", "elicitation": "needs", "waiting": "needs", "done": "done",
             "rate_limit": "limit", "error": "error", "login": "login", "team_started": TEAM_KEY}      # Notice.kind -> toggle


def _stored_prefs() -> dict:
    """The raw kv notify_prefs value (a dict), or {} when there is none, it is damaged or the DB is dead. Never raises."""
    if _db is None:
        return {}
    try:
        rec = _db.kv_get("notify_prefs")
        v = rec.get("value") if isinstance(rec, dict) else None
        return v if isinstance(v, dict) else {}
    except Exception as e:
        log.warning("notify prefs: %s", e)
        return {}


def team_enabled() -> bool:
    """Did the person switch "a teammate started on an issue I hold" on? Off by default, and anything but a stored True (no row, a damaged row, a dead DB, a string) is off."""
    return _stored_prefs().get(TEAM_KEY) is True


def team_visible() -> bool:
    """Is some repository shared with the team? The team toggle is listed (and sampled) only then: a board that shares nothing answers exactly what it always answered."""
    if _db is None:
        return False
    try:
        return bool(_db.team_shared())
    except Exception:
        return False


def prefs() -> dict:
    """The five toggles, all on until the person switches one off (kv notify_prefs), plus `team` (off until switched on) while some repository is shared. Never raises: a dead DB
    means the five on and team off."""
    out = {k: True for k in PREF_KEYS}
    v = _stored_prefs()
    for k in PREF_KEYS:
        if isinstance(v.get(k), bool):
            out[k] = v[k]
    if team_visible():
        out[TEAM_KEY] = v.get(TEAM_KEY) is True
    return out


def set_prefs(patch: dict) -> dict:
    """Merge booleans for the known toggles into kv notify_prefs and return the full set. Unknown keys and non-booleans are ignored."""
    cur = prefs()
    stored = _stored_prefs()
    new = {k: cur[k] for k in PREF_KEYS}
    for k in PREF_KEYS:
        if isinstance(patch.get(k), bool):
            new[k] = patch[k]
    if isinstance(patch.get(TEAM_KEY), bool):
        new[TEAM_KEY] = patch[TEAM_KEY]
    elif TEAM_KEY in stored:
        new[TEAM_KEY] = stored[TEAM_KEY] is True
    if _db is not None:
        _db.kv_set("notify_prefs", new)
    return prefs() if _db is not None else new


def kind_enabled(kind) -> bool:
    """Is the toggle behind this Notice.kind on? A kind with no toggle (a test notice) is always on; the team kind is on only when the person switched it on."""
    key = PREF_KIND.get(str(kind or ""))
    if key == TEAM_KEY:
        return team_enabled()
    return True if key is None else prefs()[key]


def subscribe_url() -> str | None:
    if not enabled():
        return None
    base = settings.ntfy_public_url or settings.ntfy_url
    return f"{base.rstrip('/')}/{settings.ntfy_topic}"


def publish(title: str, message: str, *, click: str | None = None, actions: list[dict] | None = None,
            priority: int = 3, tags: list[str] | None = None) -> bool:
    """ntfy publish (no-op when ntfy is not configured)."""
    if not enabled():
        return False
    body = {"topic": settings.ntfy_topic, "title": title[:250], "message": message[:3000], "priority": priority}
    if click:
        body["click"] = click
    if actions:
        body["actions"] = actions[:3]
    if tags:
        body["tags"] = tags
    req = urllib.request.Request(settings.ntfy_url.rstrip("/") + "/", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return 200 <= r.status < 300
    except Exception as e:  # never break a hook because ntfy is down
        log.warning("ntfy publish failed: %s", e)
        return False


# ---------------------------------------------------------------- the notice

@dataclass
class Notice:
    """One notification, channel-neutral. `click`/`url` are the board link ({public_url}/#/s/<tmux>; `url` falls back to the
    relative route when no public URL is set), `path` is always the relative route Web Push opens. `actions` are ntfy buttons
    (<= 3); `web_actions` are the same choices for a Web Push notification (the service worker shows the first two)."""
    title: str
    body: str
    click: str | None
    url: str
    tag: str
    priority: int
    tags: list
    actions: list
    web_actions: list
    kind: str                       # permission | idle | elicitation | waiting | done | error | rate_limit
    state: str = ""
    tmux: str = ""
    agent: str = "claude"
    perm_id: int | None = None
    path: str = "/"
    web: bool = True                # False: ntfy only (a chain step that hands over to the next one is not worth a buzz on the phone)
    renotify: bool = True           # a notice that replaces its session's earlier one (same tag) buzzes again
    ts: int = 0                     # epoch milliseconds the event happened (the notification's own time); 0 = now
    badge: int | None = None        # attention count for the app icon; send() fills it

    def web_extra(self) -> dict:
        extra = {"agent": self.agent, "state": self.state, "tmux": self.tmux, "perm_id": self.perm_id,
                 "actions": self.web_actions, "renotify": bool(self.renotify and self.tag), "ts": self.ts or int(time.time() * 1000)}
        if self.badge is not None:
            extra["badge"] = self.badge
        return extra


def _one_line(s, limit: int) -> str:
    return " ".join(str(s or "").split())[:limit]


def _is_system_turn(prompt) -> bool:
    """True for a prompt no person typed. Prefers hooks.is_system_turn (imported late: hooks imports this module)."""
    if not isinstance(prompt, str):
        return False
    fn = None
    try:
        from . import hooks
        fn = getattr(hooks, "is_system_turn", None)
    except Exception:
        fn = None
    if fn is not None:
        try:
            return bool(fn(prompt))
        except Exception:
            pass
    head = prompt.lstrip()
    return not head or head.startswith(SYSTEM_TURN_PREFIXES)


def is_rate_limit(kind) -> bool:
    k = str(kind or "").lower()
    return "rate" in k and "limit" in k


def category(state: str, kind, perm=None) -> str:
    """What the notice is about: the key of its priority and tags, and (through PREF_KIND) of the per-kind toggle that switches it."""
    k = str(kind or "").lower()
    if state == "errored":
        return "rate_limit" if is_rate_limit(k) else "error"
    if state == "done":
        return "done"
    if state == "waiting":
        if perm is not None or k in ("permission", "permission_prompt"):
            return "permission"
        if k in ("idle", "idle_prompt"):
            return "idle"
        if k in ("elicitation", "elicitation_dialog", "elicitation_url_dialog"):
            return "elicitation"
        return "waiting"
    return state


# category -> (ntfy priority, ntfy tags)
LOOK = {"permission": (4, ["bell", "key"]), "idle": (4, ["bell"]), "elicitation": (4, ["bell"]), "waiting": (4, ["bell"]),
        "error": (4, ["rotating_light"]), "done": (3, ["white_check_mark"]), "rate_limit": (5, ["no_entry"])}


# ---------------------------------------------------------------- the node in the notice (issue #151)

NODE_SEP = " · "            # a middle dot with spaces; never a dash


def _node_name() -> str | None:
    """This board's own name when at least one node is paired with it (either direction), else None. The name is the board's own configured one
    (nodes.display_name), never a string a peer sent. The pairing is looked up before the name is read, so a board with no pair never asks Tailscale
    on the hook path. Never raises."""
    if _db is None:
        return None
    try:
        from . import nodes
        return nodes.display_name() if nodes.any_paired(_db) else None
    except Exception as e:
        log.debug("notify node name: %s", e.__class__.__name__)
        return None


def _prefixed(title: str, node: str | None) -> str:
    return f"{node}{NODE_SEP}{title}" if node else title


def _tagged(tag: str, node: str | None) -> str:
    return f"{node}:{tag}" if node and tag else tag


def titled(title: str) -> str:
    """`title` as this board sends it: `<node> · title` while a node is paired, the title itself otherwise."""
    return _prefixed(title, _node_name())


def tagged(tag: str) -> str:
    """`tag` as this board sends it: `<node>:tag` while a node is paired (so two nodes with a session of one name do not replace each other's notice
    on one phone or one ntfy topic), the tag itself otherwise. An empty tag stays empty (a notice without a tag never renotifies)."""
    return _tagged(tag, _node_name())


def legacy_label(tmux_name: str) -> str:
    return tmux_name.replace("--", " / ", 1).replace("--", " · ")


def _agent_of(row: dict) -> str:
    if row.get("agent") == "shell" or row.get("launcher") in ("shell", "clone"):
        return "shell"
    return str(row.get("agent") or "claude")


def _limit_agent(row: dict | None) -> str:
    """Whose quota a limit hit belongs to: the row's agent, and Claude's for a shell row (the account is Claude's)."""
    a = _agent_of(row or {})
    return "claude" if a == "shell" else a


def _where(row: dict, agent: str, tmux_name: str) -> str:
    """`◆ shop/api · s1`: glyph, project/repo (repo 'root' is the project folder itself: the project alone), session."""
    project, repo, session = row.get("project"), row.get("repo"), row.get("name")
    if not (project and session):
        return legacy_label(tmux_name)             # no row: the label the board always used
    place = str(project) if not repo or repo == "root" else f"{project}/{repo}"
    return f"{GLYPHS.get(agent, '▸')} {place} · {session}"


def _close_in(row: dict, task: dict, now: datetime | None = None) -> int | None:
    """Seconds until the close taskflow has PLANNED for this task, from the row's flags.autoclose = {task, due}, rounded to 5 s; None when
    no close is planned (no stamp, another task's stamp, a question hold, a postponed close, one already closing, a due that cannot be
    read). The settings' grace is not consulted: the promise is the stamped due, or nothing."""
    af = (row.get("flags") or {}).get("autoclose") if isinstance(row.get("flags"), dict) else None
    if not isinstance(af, dict) or not af.get("due") or af.get("held") or af.get("waiting") or af.get("closing"):
        return None
    if af.get("task") is not None and task.get("id") is not None and af.get("task") != task.get("id"):
        return None
    try:
        due = datetime.fromisoformat(str(af["due"]))
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    left = (due - (now or datetime.now(timezone.utc))).total_seconds()
    return max(5, int(5 * round(left / 5))) if left > 0 else None


def _asks_back(message) -> bool:
    """A final message that ends with a question holds the auto-close (taskflow), so no 'closes in' promise is made for it."""
    try:
        from . import taskflow
        return bool(taskflow.ends_with_question(message))
    except Exception:
        return str(message or "").rstrip(" \t*_`\"'’”)]>\n").endswith("?")


def build(row: dict | None, task: dict | None, state: str, kind: str | None, message: str | None,
          perm: dict | None = None, now: datetime | None = None) -> Notice:
    """The Notice for a session entering `state`. `row` is a session_view dict, `task` {title, phase} or None, `perm` {id, summary}
    for a permission waiting on an answer, `now` the clock for the '(session closes in Ns)' line (default: the wall clock). The close
    line comes from the row's flags.autoclose, which taskflow stamps before the notice is built. The only read of the DB is whether a node
    is paired: then the title starts with this board's name and the tag carries it (_node_name); with none paired the Notice is what it
    always was."""
    row = row if isinstance(row, dict) else {}
    perm = perm if isinstance(perm, dict) else None
    tmux_name = str(row.get("tmux_name") or "")
    agent = _agent_of(row)
    cat = category(state, kind, perm)
    pub = (settings.public_url or "").rstrip("/")
    path = f"/#/s/{tmux_name}" if tmux_name else "/"
    node = _node_name()
    title = _prefixed(f"{_where(row, agent, tmux_name)}: {TITLES.get(state, state)}", node)

    lines: list[str] = []
    if isinstance(task, dict) and task.get("title"):
        lines.append(_one_line(task["title"], PROMPT_CHARS))
    prompt = row.get("last_prompt")
    if isinstance(prompt, str) and prompt.strip() and not _is_system_turn(prompt):
        lines.append("› " + _one_line(prompt, PROMPT_CHARS))
    if cat == "permission" and perm and perm.get("summary"):
        asked = _one_line(perm["summary"], 300)
    else:
        asked = _one_line(message, ASKED_CHARS)
    if asked:
        lines.append("? " + asked)
    if cat == "done" and isinstance(task, dict) and task.get("auto_close") and not _asks_back(message):
        left = _close_in(row, task, now)                           # only a close taskflow has planned (flags.autoclose.due), never the setting
        if left is not None:
            lines.append(f"(session closes in {left}s)")
    body = "\n".join(lines) or str(kind or "") or TITLES.get(state, state)       # ntfy shows "triggered" for an empty message

    pid = perm.get("id") if perm else None
    actions: list[dict] = []
    web_actions: list[dict] = []
    if pid is not None:
        web_actions = [{"action": "allow", "title": "Allow"}, {"action": "deny", "title": "Deny"},
                       {"action": "terminal", "title": "Terminal"}]
    else:
        web_actions = [{"action": "terminal", "title": "Terminal"}, {"action": "ack", "title": "Ack"}]
    if pub and tmux_name:
        terminal = {"action": "view", "label": "Terminal", "url": f"{pub}/term/{tmux_name}"}
        if pid is not None:
            http = {"method": "POST", "headers": {"X-CCBoard": "1"}, "clear": True}
            actions = [{"action": "http", "label": "Allow", "url": f"{pub}/api/permission/{pid}/allow", **http},
                       {"action": "http", "label": "Deny", "url": f"{pub}/api/permission/{pid}/deny", **http}, terminal]
        else:
            actions = [terminal, {"action": "http", "label": "Ack", "url": f"{pub}/api/sessions/{tmux_name}/ack", "method": "POST",
                                  "headers": {"X-CCBoard": "1"}, "clear": True}]
    priority, tags = LOOK.get(cat, (PRIORITY.get(state, 3), TAGS.get(state, [])))
    handover = cat == "done" and isinstance(task, dict) and bool(task.get("chain_next"))     # a chain step with another one queued behind it
    if handover:
        priority = 2
    link = f"{pub}{path}" if pub else None
    return Notice(title=title, body=body, click=link, url=link or path, tag=_tagged(tmux_name, node), priority=priority, tags=list(tags),
                  actions=actions[:3], web_actions=web_actions, kind=cat, state=state, tmux=tmux_name, agent=agent,
                  perm_id=pid, path=path, web=not handover)


def _attention_count() -> int | None:
    """Sessions that need a look right now (waiting, done or errored, not acknowledged): the number on the app icon. None without a DB."""
    if _db is None:
        return None
    try:
        from . import hooks
        return sum(1 for r in _db.open_rows().values() if r.get("state") in hooks.ATTENTION_STATES and not r.get("acked_at"))
    except Exception as e:
        log.warning("notify badge count: %s", e)
        return None


def send(n: Notice) -> bool:
    """Deliver a Notice on both channels (Web Push first, as always). True when either took it. The one gate every notice passes
    (a permission push skips the throttles but not this): a kind the person switched off in Settings goes nowhere."""
    if not kind_enabled(n.kind):
        return False
    sent = 0
    if n.web:
        if n.badge is None:
            n = replace(n, badge=_attention_count())
        sent = web_push(n.title, n.body, n.path, tag=n.tag or None, extra=n.web_extra())
    ok = publish(n.title, n.body, click=n.click, actions=n.actions, priority=n.priority, tags=n.tags)
    return bool(sent) or ok


# ---------------------------------------------------------------- context and throttles

def context(name: str) -> tuple[dict | None, dict | None]:
    """(session row, its task) for a tmux name, each None when unknown. Never raises: a notice goes out without its context."""
    if _db is None:
        return None, None
    try:
        row = _db.open_row(name)
    except Exception as e:
        log.warning("notify context: %s", e)
        return None, None
    if not row:
        return None, None
    task = None
    try:
        found = _db.active_tasks_by_session().get(row.get("id") if row.get("id") is not None else row.get("row_id")) or []
        task = found[0] if found else None
        if task:                                                # is another step queued behind this one? (a chain's non-final step)
            task = {**task, "chain_next": any(c.get("phase") == "queued" for c in _db.children_of(task["id"]))}
    except Exception as e:
        log.warning("notify task lookup: %s", e)
    return row, task


def _admit(name: str, state: str) -> bool:
    """One notice per (session, state) per window: THROTTLE_SECONDS, or STATE_COOLDOWN[state] when that is longer."""
    window = max(THROTTLE_SECONDS, STATE_COOLDOWN.get(state, 0))
    now = _clock()
    with _lock:
        prev = _last.get((name, state))
        if prev is not None and now - prev < window:
            return False
        _last[(name, state)] = now
        if len(_last) > 512:                         # forget sessions that have long been quiet
            for k in [k for k, t in _last.items() if now - t > 2 * max(THROTTLE_SECONDS, *STATE_COOLDOWN.values())]:
                del _last[k]
    return True


def _rl_blocked(agent: str) -> bool:
    """Was a limit notice for this agent sent within RATE_LIMIT_EVERY (15 minutes), whichever session reported it and whatever its reset
    time says? Read-only: a window that is held back here is not claimed, so the next report after the quarter hour tells it."""
    with _lock:
        prev = _rl_last.get(agent)
    return prev is not None and _clock() - prev < RATE_LIMIT_EVERY


def _rl_stamp(agent: str) -> None:
    with _lock:
        _rl_last[agent] = _clock()


def mark_sent(name: str, state: str = "waiting") -> None:
    """Count a notice that bypassed the throttle (a permission push) as sent, so the hook notice that follows it is the throttled one."""
    with _lock:
        _last[(name, state)] = _clock()


def _recent_resets_at(name: str):
    """The reset time of the limit episode this session just reported (hooks writes kv 'rate_limited' right before it calls us), or None."""
    if _db is None:
        return None
    try:
        rec = _db.kv_get("rate_limited")
        v = rec.get("value") if isinstance(rec, dict) else None
        if not isinstance(v, dict) or v.get("session") != name or v.get("resets_at") in (None, ""):
            return None
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(rec.get("at")))).total_seconds()
        return v["resets_at"] if 0 <= age <= 300 else None
    except Exception:
        return None


def _claim_window(agent: str, resets_at, name: str) -> bool:
    """True the first time a (agent, resets_at) limit window is announced; kv `rl_notified:<agent>:<resets_at>` remembers it across
    restarts (in memory while there is no DB)."""
    key = f"rl_notified:{agent}:{resets_at}"
    with _lock:
        if _db is not None:
            try:
                if _db.kv_get(key) is not None:
                    return False
                _db.kv_set(key, {"session": name})
                return True
            except Exception as e:
                log.warning("rate-limit window kv: %s", e)
        if key in _rl_mem:
            return False
        _rl_mem.add(key)
        return True


# ---------------------------------------------------------------- hook-path entry points

def _fresh_perm(p: dict) -> bool:
    """Undecided permission rows are never swept after a restart or deploy: only one younger than the approve horizon is the prompt
    the session is showing now (mirrors main._permission_pending)."""
    from datetime import datetime, timezone
    raw = p.get("created_at")
    if not isinstance(raw, str) or not raw:
        return True
    try:
        at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - at).total_seconds() <= float(getattr(settings, "approve_timeout", 90) or 90) + 15


def notify_session(name: str, state: str, message: str | None, kind: str | None = None) -> bool:
    """Push for a session entering waiting / done / errored: loads the row and its task, builds the Notice, throttled per
    (session, state). Without a row the notice keeps the old plain label."""
    if state not in TITLES or not any_channel():
        return False
    if not kind_enabled(category(state, kind)):
        return False                                       # switched off in Settings: do not use up the cooldown either
    row, task = context(name)
    if state == "errored" and is_rate_limit(kind):
        # a limit is the account's, not the session's: one notice per agent until the window resets (and never twice in 15 minutes),
        # not the 60 s errored cooldown (one session reported the same limit 482 times). notify_rate_limit has usually told it already.
        agent = _limit_agent(row)
        resets_at = _recent_resets_at(name)
        if _rl_blocked(agent) or (resets_at is not None and not _claim_window(agent, resets_at, name)):
            return False
        _rl_stamp(agent)
    elif not _admit(name, state):
        return False
    perm = None
    if state == "waiting" and category(state, kind) == "permission" and _db is not None:
        try:
            pending = [p for p in _db.perm_pending() if p.get("tmux_name") == name and _fresh_perm(p)]   # a row a restart left undecided is not this prompt
            perm = {"id": pending[-1]["id"], "summary": pending[-1].get("summary")} if pending else None
        except Exception:
            perm = None
    return send(build(row or {"tmux_name": name}, task, state, kind, message, perm))


def _limit_notice(row: dict, task: dict | None, name: str, agent: str, message: str | None) -> Notice:
    n = build(row, task, "errored", "rate_limit", message or "rate limit hit")
    # the title names the account, so the first body line names the session that hit it
    node = _node_name()
    return replace(n, title=_prefixed(f"{AGENT_LABELS.get(agent, str(agent).title())} rate limited", node), tag=_tagged("rate-limit", node),
                   body=f"{_where(row, _agent_of(row), name)}\n{n.body}")


def notify_rate_limit(name: str, message: str | None, agent: str | None = None, resets_at=None) -> bool:
    """The account-level 'rate limited' notice, once per (agent, resets_at) window (kv rl_notified:<agent>:<resets_at>) and never twice for
    one agent within RATE_LIMIT_EVERY; without a reset time that 15-minute floor and hooks' same-episode check are the gates."""
    if not any_channel() or not kind_enabled("rate_limit"):
        return False
    row, task = context(name)
    agent = agent or _limit_agent(row)
    if resets_at is None:
        resets_at = _recent_resets_at(name)
    if _rl_blocked(agent) or (resets_at is not None and not _claim_window(agent, resets_at, name)):
        return False
    _rl_stamp(agent)
    return send(_limit_notice(row or {"tmux_name": name}, task, name, agent, message))


def notify_login_problem(account: str | None, label: str | None, name: str, message: str | None, agent: str = "claude") -> bool:
    """The account-level 'login not valid' notice (a session reported an authentication failure), at most once per account per LOGIN_EVERY
    seconds whichever session reports it. Taps open Settings > Accounts on that account's row."""
    if not any_channel() or not kind_enabled("login"):
        return False
    k = f"{agent}:{account or '-'}"
    now = _clock()
    with _lock:
        prev = _login_told.get(k)
        if prev is not None and now - prev < LOGIN_EVERY:
            return False
        _login_told[k] = now
        if len(_login_told) > 64:
            for old in [o for o, t in _login_told.items() if now - t > 2 * LOGIN_EVERY]:
                del _login_told[old]
    row, _task = context(name)
    return send(_login_notice(row or {}, account, label, name, message, agent))


def _login_notice(row: dict, account: str | None, label: str | None, name: str, message: str | None, agent: str) -> Notice:
    who = AGENT_LABELS.get(agent, str(agent).title())
    path = "/#/settings?sec=accounts" + (f"&acct={quote(account, safe='')}" if account else "")
    pub = (settings.public_url or "").rstrip("/")
    link = f"{pub}{path}" if pub else None
    where = _where(row, _agent_of(row), name)
    body = f"{where}\n{_one_line(message, ASKED_CHARS)}\nLog in again in Settings > Accounts." if message else f"{where}\nLog in again in Settings > Accounts."
    actions = [{"action": "view", "label": "Log in again", "url": link}] if link else []
    node = _node_name()
    return Notice(title=_prefixed(f"{who} login not valid" + (f": {_one_line(label, 60)}" if label else ""), node), body=body, click=link, url=link or path,
                  tag=_tagged("login-problem", node), priority=4, tags=["warning", "key"], actions=actions, web_actions=[], kind="login", state="errored",
                  tmux=name, agent=agent, path=path)


# ---------------------------------------------------------------- a teammate started on an issue the owner holds (issue #200)

def notify_team_start(repo_slug, login, issue) -> bool:
    """"<login> started on #<issue>", for an issue the owner holds. Off until the person switches the kind on (Settings > Notifications); then ntfy and Web Push, once per repository,
    person and issue (the same pair never twice, whatever calls this). Only a clean login, an issue number and the board's own repository slug reach the text: no title, no
    comment and no branch name from GitHub. The caller (app/team.py `Watch`) has already left out the owner's own logins and boards. Returns True when a channel took the notice."""
    from . import github, team
    lg = team.login_of(login) if isinstance(login, str) else None
    o, _, n = repo_slug.partition("/") if isinstance(repo_slug, str) else ("", "", "")
    if lg is None or lg.lower().endswith("[bot]") or isinstance(issue, bool) or not isinstance(issue, int) or not 0 < issue < 10 ** 9 or not github.valid_slug(o, n):
        return False
    if not any_channel() or not kind_enabled("team_started"):
        return False                                           # switched off (the default) or nowhere to send: nothing is remembered either
    k = (f"{o}/{n}".lower(), lg.lower(), issue)
    with _lock:
        if k in _team_told:
            return False
        _team_told[k] = _clock()
        while len(_team_told) > TEAM_TOLD_MAX:
            _team_told.pop(next(iter(_team_told)))
    return send(_team_notice(f"{o}/{n}", lg, issue))


def _team_notice(slug: str, login: str, issue: int) -> Notice:
    path = "/#/team"
    pub = (settings.public_url or "").rstrip("/")
    link = f"{pub}{path}" if pub else None
    return Notice(title=titled(f"{login} started on #{issue}"), body=f"{slug}\n{login} started on issue #{issue}, which you hold.\nOpen Team to see who is working on what.",
                  click=link, url=link or path, tag=tagged(f"team-{slug}-{login}-{issue}".lower()), priority=3, tags=["busts_in_silhouette"],
                  actions=[{"action": "view", "label": "Open Team", "url": link}] if link else [], web_actions=[], kind="team_started", state="", agent="", path=path, renotify=False)


# ---------------------------------------------------------------- Settings preview

SAMPLE_ROW = {"tmux_name": "shop--api--s1", "project": "shop", "repo": "api", "name": "s1", "agent": "claude",
              "last_prompt": "fix the login redirect"}
SAMPLE_TASK = {"title": "Fix login redirect", "phase": "running"}


def samples() -> dict:
    """One example notice per toggle, built by the same code that builds the real ones, for the Settings preview card:
    {needs|done|limit|error|login[|team]: {title, body, priority, buttons: [titles Web Push shows], kind}} (`team` only while a repository is shared)."""
    row, task, tmux = SAMPLE_ROW, SAMPLE_TASK, SAMPLE_ROW["tmux_name"]
    made = {
        "needs": build(row, task, "waiting", "permission_prompt", "Claude needs your permission to use Bash", {"id": 0, "summary": "Bash: npm test"}),
        "done": build(row, task, "done", None, "Fixed the redirect and the tests pass."),
        "limit": _limit_notice(row, task, tmux, "claude", "5-hour limit reached, resets 14:00"),
        "error": build(row, task, "errored", "error", "API Error: 500 Internal server error"),
        "login": _login_notice(row, "work", "work", tmux, "Please run /login", "claude"),
    }
    if team_visible():                                           # issue #200: the team notice is sampled only while some repository is shared
        made["team"] = _team_notice("acme/shop", "bob", 12)
    return {k: {"title": n.title, "body": n.body, "priority": n.priority, "kind": n.kind,
                "buttons": [a["title"] for a in n.web_actions][:2]} for k, n in made.items()}
