"""GitHub issues as dispatch input: the "Who should do it" block parser, the origin owner and the trust check. Pure, no I/O.

Issue text is untrusted. The parser only reads a few tokens, never passes an unknown flag on, and drops every bypass spelling
(the caller shows the warning). The launcher validates what is left against the installed agent's schema."""
from __future__ import annotations

import re
import shlex

HEADING = re.compile(r"^##[ \t]+Who should do it[ \t]*$", re.I | re.M)
LINE = re.compile(r"^-[ \t]+(Claude Code|Claude|Codex):(.*)$", re.I)
SPAN = re.compile(r"`([^`\n]+)`")
VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,79}$")
BYPASS_WORDS = {"bypasspermissions", "danger-full-access"}
WARNING = "ignored a bypass setting"
COMMENT_HEAD = 800


def empty_who() -> dict:
    return {"claude": None, "codex": None, "default_agent": None, "warnings": []}


def _comment_free(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return re.sub(r"<!--.*\Z", "", text, flags=re.S)       # an unterminated comment hides the rest, as GitHub renders it


def _section(body: str) -> str:
    text = _comment_free(body or "")
    m = HEADING.search(text)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"^##[ \t]+", rest, flags=re.M)
    return rest[:nxt.start()] if nxt else rest


def _is_bypass(tok: str) -> bool:
    low = tok.lower()
    return "dangerously" in low or low in ("--yolo", "yolo") or low in BYPASS_WORDS


def _clean(v: str | None) -> str | None:
    v = (v or "").strip().strip("\"'")
    return v if VALUE.match(v) else None


def _flag(tokens: list[str], i: int, names: tuple[str, ...]) -> tuple[str, str | None, int] | None:
    """If tokens[i] is one of `names` (as `--flag value` or `--flag=value`): (name, raw value | None, tokens consumed)."""
    t = tokens[i]
    for n in names:
        if t == n:
            nxt = tokens[i + 1] if i + 1 < len(tokens) else None
            if nxt is not None and not nxt.startswith("-"):
                return n, nxt, 2
            return n, None, 1
        if n.startswith("--") and t.startswith(n + "="):
            return n, t[len(n) + 1:], 1
    return None


def _tokens(line_rest: str) -> list[str]:
    m = SPAN.search(line_rest)
    if not m:
        return []
    try:
        toks = shlex.split(m.group(1))
    except ValueError:
        return []
    if toks and toks[0].rsplit("/", 1)[-1].lower() in ("claude", "codex"):
        toks = toks[1:]
    return toks


def _parse_claude(toks: list[str], warns: list[str]) -> dict:
    out = {"model": None, "effort": None, "permission_mode": None}
    keys = {"--model": "model", "--effort": "effort", "--permission-mode": "permission_mode"}
    i = 0
    while i < len(toks):
        t = toks[i]
        if _is_bypass(t):
            warns.append(WARNING)
        hit = _flag(toks, i, tuple(keys))
        if hit:
            name, raw, used = hit
            if raw is not None and _is_bypass(raw.strip("\"'")):
                warns.append(WARNING)
            elif keys[name] and out[keys[name]] is None:
                out[keys[name]] = _clean(raw)
            i += used
            continue
        i += 1
    return out


def _parse_codex(toks: list[str], warns: list[str]) -> dict:
    out = {"model": None, "reasoning": None, "sandbox": None, "approval": None}
    keys = {"-m": "model", "--model": "model", "-s": "sandbox", "--sandbox": "sandbox",
            "-a": "approval", "--ask-for-approval": "approval", "-c": "config", "--config": "config"}
    i = 0
    while i < len(toks):
        t = toks[i]
        if _is_bypass(t):
            warns.append(WARNING)
        hit = _flag(toks, i, tuple(keys))
        if hit:
            name, raw, used = hit
            field = keys[name]
            raw = (raw or "").strip()
            if field == "config":
                k, _, v = raw.partition("=")
                v = v.strip().strip("\"'")
                if k.strip() == "model_reasoning_effort" and out["reasoning"] is None:
                    out["reasoning"] = _clean(v)
            elif _is_bypass(raw.strip("\"'")):
                warns.append(WARNING)
            elif out[field] is None:
                out[field] = _clean(raw)
            i += used
            continue
        i += 1
    return out


def parse_who(body: str | None) -> dict:
    """{claude: {model, effort, permission_mode} | None, codex: {model, reasoning, sandbox, approval} | None,
    default_agent: 'claude' | 'codex' | None, warnings: [...]} from the first "## Who should do it" section."""
    who = empty_who()
    warns: list[str] = []
    seen: set[str] = set()
    for line in _section(body or "").splitlines():
        m = LINE.match(line.strip())
        if not m:
            continue
        agent = "codex" if m.group(1).lower() == "codex" else "claude"
        if agent in seen:
            continue                                   # the first line of an agent wins
        toks = _tokens(m.group(2))
        if not toks:
            continue
        seen.add(agent)
        who[agent] = _parse_claude(toks, warns) if agent == "claude" else _parse_codex(toks, warns)
    who["default_agent"] = "claude" if who["claude"] else "codex" if who["codex"] else None
    who["warnings"] = [WARNING] if warns else []
    return who


_REMOTE = (re.compile(r"^(?:https?|ssh|git)://(?:[^/@]+@)?[^/:]+(?::\d+)?/([^/]+)/[^/]+?(?:\.git)?/?$"),
           re.compile(r"^(?:[^@/\s]+@)?[^:/\s]+:([^/\s]+)/[^/\s]+?(?:\.git)?/?$"))


def origin_owner(url: str | None) -> str | None:
    """The owner (lower case) of an https, ssh:// or scp-style remote URL; None when it is none of those."""
    u = (url or "").strip()
    for rx in _REMOTE:
        m = rx.match(u)
        if m:
            return m.group(1).lower()
    return None


def trusted(author: str | None, owner: str | None, me: str | None = None) -> bool:
    """An issue is the owner's own when its author is the origin's owner or the GitHub login gh uses on this box (`me`; an
    organisation's repo has an org as its owner, so the board's own user is the one that counts there). Case-insensitive;
    an unknown author is untrusted, and so is everyone when neither the owner nor `me` is known."""
    a = (author or "").strip().lower()
    return bool(a) and any(a == (x or "").strip().lower() for x in (owner, me))


def comment_body(task: dict, pr_url: str | None = None) -> str:
    """The short comment a done task posts on its issue: the first 800 characters of the result, the branch, the PR link."""
    result = (task.get("result") or "").strip()
    head = result[:COMMENT_HEAD] + ("…" if len(result) > COMMENT_HEAD else "")
    lines = [f"ccboard task finished: {task.get('title') or ''}".rstrip(), "", head or "(no result captured)", ""]
    if task.get("branch"):
        lines.append(f"Branch: `{task['branch']}`")
    pr = pr_url or task.get("pr_url")
    if pr:
        lines.append(f"PR: {pr}")
    lines += ["", "Posted from ccboard"]
    return "\n".join(lines)
