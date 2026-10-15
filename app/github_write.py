"""The writer next to the reader (nodes epic, issue #148): the few GitHub writes the claim of an epic issue needs, and nothing else.

WRITES ARE OFF BY DEFAULT. `settings.epic_writes` (CCBOARD_EPIC_WRITES, exactly "1") turns them on. With it off, `argv()` refuses before an argv exists, `send()` and the queue refuse
before anything else, and no gh command with a method other than GET is ever built or run (the reader in app/github.py stays GET only).

The allow list is the ONLY thing this module can send. Each shape is built from a validated owner, repository, issue number, comment id, the one label and a login:

    POST   /repos/{o}/{r}/issues/{n}/comments        create the claim comment (or the fixed "a person is needed" comment)
    PATCH  /repos/{o}/{r}/issues/comments/{id}       edit the board's OWN claim comment
    DELETE /repos/{o}/{r}/issues/comments/{id}       delete the board's OWN claim comment
    POST   /repos/{o}/{r}/issues/{n}/assignees       add the board's own login
    DELETE /repos/{o}/{r}/issues/{n}/assignees       remove the board's own login
    POST   /repos/{o}/{r}/issues/{n}/labels          add `ccboard:in-progress`
    DELETE /repos/{o}/{r}/issues/{n}/labels/{name}   remove `ccboard:in-progress`
    POST   /repos/{o}/{r}/labels                     create `ccboard:in-progress`

Team presence (issue #198, Team P2) adds, with `purpose` "presence" or "presence_remove" on the request:

    POST   /repos/{o}/{r}/issues                     create the status issue: a fixed title, a fixed body and the one label `ccboard:presence`, nothing else
    POST   /repos/{o}/{r}/labels                     create `ccboard:presence` (its fixed definition)
    POST / PATCH / DELETE of a comment               the board's OWN presence comment, whose body is a presence body (app/team.py `presence_body`)

A presence write does NOT need CCBOARD_EPIC_WRITES: it is gated by the board's own per-repository publish switch instead (`presence_gate`, installed by app/team_presence.py; with no
gate installed, or the switch off for that repository, or the global pause on, nothing is built). An epic request can never carry a presence body or create an issue, and a presence
request can never carry a claim body.

No issue edit, no close, no pull request (a pull request is gitops', the one GitHub write that does not come through here), no repository setting, no other label, no comment that is not the board's own. Whose comment it is, is the caller's business (app/nodes_epics.py
records the ids it created and re-reads before every edit); this module only guarantees the SHAPE. A comment body is never free text: it is made by `claim_body()` from validated fields and a
fixed sentence, by `progress_body()` (issue #149: the alias, one phase sentence of a closed list, a branch name, a pull request link built from a validated repository and number and, only
when the epic asks for it, a result that went through `redact_result()` first), or it is HELP_BODY, and `argv()` refuses any other body. The body goes to gh on stdin as JSON (`--input -`), the command is an argv list (never a shell) with a bounded timeout
and a capped answer, and a gh failure is reduced to a fixed reason (no stderr text is ever passed on).

The QUEUE: one write at a time per board, at least 1.1 s apart, at most 30 a minute, a back-off on 403 and 429 (Retry-After, else the secondary limit's 60 s), and never a retry here: a write
whose outcome is unknown (a timeout, a 5xx, no answer) is raised as `unknown=True` and the caller reads back what exists before it does anything again.
"""
from __future__ import annotations

import collections
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, NamedTuple

from . import github, team
from .config import settings

LABEL = "ccboard:in-progress"
LABEL_COLOR = "fbca04"
LABEL_DESCRIPTION = "A ccboard node is working this issue"
MARKER = "<!-- ccboard:claim v1 -->"
CLAIM_TEXT = ("Claimed by a ccboard node. The line above is how the nodes tell who holds this issue. Please do not edit or delete this comment while the issue is being worked.")
HELP_BODY = ("ccboard: three nodes have taken this issue and each attempt ended without a result, so the nodes have stopped trying. A person needs to look at it. "
             "To let the nodes try again, delete the claim comments above.")
STATUSES = ("claimed", "running", "done", "failed", "released", "cancelled")
MIN_GAP = 1.1                       # seconds between two writes of one board
CEILING = 30                        # writes a minute
CEILING_WINDOW = 60.0
LOCK_TIMEOUT = 45.0                 # the longest a write waits for its turn in the queue; then it is refused as busy and nothing is sent
MAX_WAIT = 120.0                    # the longest the queue sleeps for one write; a longer back-off is raised as rate_limited instead
TIMEOUT = 20.0
OUT_CAP = 256 * 1024
MESSAGES = {
    "writes_off": "this board does not write to GitHub (CCBOARD_EPIC_WRITES is off)",
    "no_gh": "gh is not installed on this board",
    "no_login": "gh is not logged in on this board: run gh auth login there",
    "no_access": "this gh login may not do that on that repository",
    "not_found": "GitHub has no such issue or comment, or this login cannot see it",
    "invalid": "GitHub refused the request as invalid",
    "rate_limited": "GitHub asked us to wait",
    "busy": "too many GitHub writes are waiting on this board, so this one was not sent",
    "timeout": "gh did not answer in time, so the outcome of the write is not known",
    "unavailable": "GitHub did not answer (an error on its side), so the outcome of the write is not known",
    "bad_answer": "GitHub sent an answer this board could not read",
    "bad_request": "that is not a write this board will make",
    "presence_off": "this board does not publish presence to that repository (the switch is off, or presence is paused)",
    "limited": "the repository limits who may comment right now (interaction limits)",
}
# the status issue of team presence (issue #198): fixed words, never free text
PRESENCE_LABEL = team.PRESENCE_LABEL
PRESENCE_LABEL_COLOR = "c5def5"
PRESENCE_LABEL_DESCRIPTION = "ccboard team presence: one status comment per person"
PRESENCE_TITLE = "ccboard team presence"
PRESENCE_ISSUE_BODY = ("ccboard boards of the people working in this repository keep one comment each here, saying which issue and branch they work on and whether a session is running. "
                       "Only boards whose owner switched this on write here. Please leave this issue open; closing it stops presence for everyone.")
PURPOSES = ("epic", "presence", "presence_remove")
presence_gate = None                # app/team_presence.py installs fn(owner, repo, purpose) -> bool; None refuses every presence write (fail closed)

# a claim comment's fields, as the writer accepts them (ASCII only: a name with a dot looks like a host name and is refused)
ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")
NID_RE = re.compile(r"[0-9a-f]{8}")
TASKREF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:#-]{0,39}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,39}){0,4}")
CID_RE = re.compile(r"[1-9][0-9]{0,15}")
_NUM = re.compile(r"[0-9]{1,9}")
_BODY = re.compile(r"<!-- ccboard:claim v1 -->\nccboard claim: alias=(?P<alias>\S+) nid=(?P<nid>\S+) lease=(?P<lease>[0-9]{1,3})m seq=(?P<seq>[0-9]{1,9}) status=(?P<status>[a-z]+) "
                   r"attempt=(?P<attempt>[0-9]{1,2})(?: reclaims=(?P<reclaims>[0-9]{1,16}))?(?: task=(?P<task>\S+))?(?: branch=(?P<branch>\S+))?\n\n" + re.escape(CLAIM_TEXT))
_PATHS = (
    ("POST", re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/issues/[1-9][0-9]{0,8}/comments")),
    ("PATCH", re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/issues/comments/[1-9][0-9]{0,15}")),
    ("DELETE", re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/issues/comments/[1-9][0-9]{0,15}")),
    ("POST", re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/issues/[1-9][0-9]{0,8}/assignees")),
    ("DELETE", re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/issues/[1-9][0-9]{0,8}/assignees")),
    ("POST", re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/issues/[1-9][0-9]{0,8}/labels")),
    ("DELETE", re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/issues/[1-9][0-9]{0,8}/labels/ccboard%3Ain-progress")),
    ("POST", re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/labels")),
    ("POST", re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})/issues")),          # team presence: the status issue (fixed title, body and label only)
)
# ---------------------------------------------------------------- progress comments (issue #149): fixed sentences, validated fields, and a result that is redacted before it gets here
PROGRESS_MARKER = "<!-- ccboard:progress v1 -->"
PHASE_TEXT = {                                                  # the closed list of phases a progress comment can name, with the one sentence of each
    "waiting": "waiting for a person",
    "pr": "pull request opened",
    "done": "done",
    "failed": "failed",
    "cancelled": "cancelled",
    "refused_bypass": "refused: the issue asks for bypass",
    "refused_posture": "refused: the issue asks for a permission setting this node does not use",
}
RESULT_MAX = 1500                                               # characters of a result that may be posted, counted after the redaction
RESULT_IN = 4000                                                # characters of a result the redaction reads: the cap comes BEFORE any pattern runs
RESULT_HEAD = "Result (first 1500 characters, redacted):"
REMOVED = "[removed]"
NOTE_REMOVED = "Some text was removed before posting."
PR_URL_RE = re.compile(r"https://github\.com/[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?![-.])[A-Za-z0-9_.-]{1,100}/pull/[1-9][0-9]{0,8}")
_PHASE_ALT = "|".join(re.escape(v) for v in PHASE_TEXT.values())
_PROGRESS = re.compile(re.escape(PROGRESS_MARKER) + r"\nccboard (?P<alias>[A-Za-z0-9][A-Za-z0-9_-]{0,39}): (?P<phase>" + _PHASE_ALT + r")(?: (?P<url>https://\S+))?"
                       r"(?:\nBranch: (?P<branch>\S+))?"
                       r"(?:\n\n" + re.escape(RESULT_HEAD) + r"\n```text\n(?P<text>[^`]{0,1600})\n```(?P<note>\n" + re.escape(NOTE_REMOVED) + r")?)?")
# a home folder name may hold spaces ("Ann Lee"): up to four words, and only when a slash follows them, else the one word
_NAME = r"(?:[^/\\\s]+(?: [^/\\\s]+){0,3}(?=[/\\])|[^/\\\s]+)"
_HOME = re.compile(r"/(?:Users|home)/" + _NAME)
_WIN_HOME = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]Users[\\/]" + _NAME)
_ROOT_HOME = re.compile(r"(?<![\w./-])/root(?=[/\s'\")\],;:]|$)")
_TEMP = re.compile(r"(?<![\w./-])(?:/private)?/var/folders(?:/[^/\s]+){0,3}|(?<![\w./-])/private/var(?:/[^/\s]+)?")
_URL_CRED = re.compile(r"//[^/\s:@]+:[^/\s@]+@")
_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_ASSIGN = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)([ \t]{0,3}=[ \t]{0,3})(?:\"[^\"\n]{0,200}\"|'[^'\n]{0,200}'|[^\s\"',;&]{1,200})")
_DOTTED = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_HOST_END = (".ts.net", ".local", ".internal", ".lan")
_TOKENS = (
    re.compile(r"gh[a-z]_[A-Za-z0-9_]{8,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{8,}"),
    re.compile(r"xox[A-Za-z]-[A-Za-z0-9-]{8,}"),
    re.compile(r"(?i:bearer)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"[A-Fa-f0-9]{32,}"),
)
_RUN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_DROP = frozenset(("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"))


def _host_name(m: "re.Match") -> str:
    """A dotted name that ends in .ts.net, .local, .internal or .lan is a machine of someone's network."""
    t = m.group(0)
    return REMOVED if "." in t and t.lower().endswith(_HOST_END) else t


def _run_is_token(m: "re.Match") -> str:
    """A run of 32 or more base64 characters is a token when it holds a digit and a letter (a long word, a path or a hyphenated name does not)."""
    t = m.group(0)
    return REMOVED if any(c.isdigit() for c in t) and any(c.isalpha() for c in t) else t


def redact_result(raw, limit: int = RESULT_MAX) -> tuple[str, bool]:
    """(text, removed): a task result made safe to post in a public comment, by ONE pass. The text is capped FIRST (RESULT_IN characters; when it was longer it is cut at the last white
    space, so a token cut in two by the cap does not stay behind as a fragment), then URL credentials (`//user:pass@`) and email addresses become `[removed]`, the home directory and the
    macOS, Linux, Windows and /root home folder prefixes (the user folder and the name after it, spaces included) become `~`, the temp folders (/var/folders, /private/var) become `[tmp]`, and
    anything shaped like a token (GitHub gh*_ and github_pat_, sk-, AKIA, xox, Bearer, a run of 32 or more hex characters, a run of 32 or more base64 characters with a digit and a letter, the
    value of a password=, token=, secret= or api_key= assignment, a host name ending in .ts.net, .local, .internal or .lan) becomes `[removed]`. Control and format characters are dropped and a backtick becomes an apostrophe (the text is posted in a fenced block it cannot leave). The result is cut to `limit`
    characters. `removed` is True when a token was replaced. The function applied to its own output changes nothing, which is how `valid_body` knows a result is redacted."""
    if not isinstance(raw, str):
        return "", False
    text = raw[:RESULT_IN]
    if len(raw) > RESULT_IN:
        cut = max(text.rfind(" "), text.rfind("\n"), text.rfind("\t"))
        text = text[:cut] if cut > 0 else ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(c for c in text if c in "\n\t" or (unicodedata.category(c) not in _DROP and ord(c) >= 32 and c != "\x7f"))
    try:
        home = str(Path.home())
    except (RuntimeError, KeyError, OSError):
        home = ""
    text = _URL_CRED.sub("//" + REMOVED + "@", text)
    text = _EMAIL.sub(REMOVED, text)
    if len(home) > 1:
        text = text.replace(home, "~")
    for rx in (_WIN_HOME, _HOME, _ROOT_HOME):
        text = rx.sub("~", text)
    text = _TEMP.sub("[tmp]", text)
    removed = False
    for rx in _TOKENS:
        text, n = rx.subn(REMOVED, text)
        removed = removed or n > 0
    text = _ASSIGN.sub(lambda m: m.group(1) + m.group(2) + REMOVED, text)
    text = _DOTTED.sub(_host_name, text)
    text = _RUN.sub(_run_is_token, text)
    removed = removed or REMOVED in text
    text = text.replace("`", "'")
    return text[:limit].strip(), removed


SECRETISH = re.compile(r"gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|(?i:bearer\s+\S{8,})|[A-Za-z0-9_-]{41,}")


class GhWriteError(Exception):
    """A write that did not happen, or may not have. `reason` is a key of MESSAGES; `unknown` is True when GitHub may have applied it (a timeout, a 5xx, no answer): the caller
    must read back what exists before it writes again. `status` is the HTTP code when there was one; `wait_s` is set for rate_limited."""

    def __init__(self, reason: str, wait_s: int | None = None, unknown: bool = False, status: int | None = None):
        self.reason = reason if reason in MESSAGES else "bad_answer"
        self.wait_s, self.unknown, self.status = wait_s, bool(unknown), status
        super().__init__(self.message)

    @property
    def message(self) -> str:
        return f"{MESSAGES['rate_limited']} {self.wait_s} s" if self.reason == "rate_limited" and self.wait_s else MESSAGES[self.reason]

    def as_dict(self) -> dict:
        return {"reason": self.reason, "message": self.message, "wait_s": self.wait_s, "unknown": self.unknown}


class Request(NamedTuple):
    method: str
    path: str
    body: dict | None
    purpose: str = "epic"           # epic (the claim of #148, behind CCBOARD_EPIC_WRITES) or presence / presence_remove (team presence, behind the per-repository switch)


def writes_enabled() -> bool:
    return settings.epic_writes is True


_REPO_OF = re.compile(r"/repos/([^/?]{1,100})/([^/?]{1,100})(?:/|\Z)")


def _off(req=None) -> None:
    """Refuse before anything is built: an epic write while CCBOARD_EPIC_WRITES is off, a presence write the publish switch does not allow (no gate, switch off, paused)."""
    purpose = getattr(req, "purpose", "epic") if req is not None else "epic"
    if purpose == "epic":
        if not writes_enabled():
            raise GhWriteError("writes_off")
        return
    if purpose not in PURPOSES:
        raise GhWriteError("bad_request")
    m = _REPO_OF.match(req.path) if isinstance(getattr(req, "path", None), str) else None
    gate = presence_gate
    try:
        ok = bool(m) and gate is not None and gate(m.group(1), m.group(2), purpose) is True
    except Exception:
        ok = False
    if not ok:
        raise GhWriteError("presence_off")


# ---------------------------------------------------------------- the comment body: a fixed template of validated fields, never free text

def claim_body(alias: str, nid: str, lease_min: int, seq: int, status: str, attempt: int, *, reclaims: int | None = None, task: str | None = None, branch: str | None = None) -> str:
    """The claim comment. Every field is checked against its own pattern (ValueError otherwise): no free text from a peer, an agent, an issue or a caller can enter it. Nothing here
    names a path, a host, an address or a token."""
    def num(v, lo, hi):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValueError("bad number")
        return v
    if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias) or not isinstance(nid, str) or not NID_RE.fullmatch(nid) or status not in STATUSES:
        raise ValueError("bad claim field")
    if task is not None and not (isinstance(task, str) and TASKREF_RE.fullmatch(task)) or branch is not None and not (isinstance(branch, str) and BRANCH_RE.fullmatch(branch) and ".." not in branch):
        raise ValueError("bad claim field")
    line = f"ccboard claim: alias={alias} nid={nid} lease={num(lease_min, 10, 120)}m seq={num(seq, 0, 999_999_999)} status={status} attempt={num(attempt, 1, 99)}"
    if reclaims is not None:
        if not CID_RE.fullmatch(str(num(reclaims, 1, 10 ** 15))):
            raise ValueError("bad claim field")
        line += f" reclaims={reclaims}"
    if task is not None:
        line += f" task={task}"
    if branch is not None:
        line += f" branch={branch}"
    if SECRETISH.search(line):
        raise ValueError("bad claim field")                         # fits the patterns but looks like a token
    return f"{MARKER}\n{line}\n\n{CLAIM_TEXT}"


def progress_body(alias: str, phase: str, *, branch: str | None = None, pr: tuple | None = None, text: str | None = None, removed: bool = False) -> str:
    """A progress comment (issue #149): `ccboard <alias>: <the sentence of the phase> [<pull request link>]`, an optional `Branch: <name>` line and, only when `text` is given, the
    result in a fenced block. Every field is checked against its own pattern (ValueError otherwise): the phase is a key of PHASE_TEXT, the alias and the branch have the claim's rules, the
    link is built here from a validated owner, repository and number (`pr` = (owner, repo, number)), and `text` must be what `redact_result` returns (it is run again: a text that would
    change is refused). Nothing here names a path, a host or a token; one that still looks like a token is refused, never posted."""
    if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias) or phase not in PHASE_TEXT:
        raise ValueError("bad progress field")
    if branch is not None and not (isinstance(branch, str) and BRANCH_RE.fullmatch(branch) and ".." not in branch):
        raise ValueError("bad progress field")
    url = None
    if pr is not None:
        o, r, n = pr
        if not github.valid_slug(o, r) or not github.valid_number(n):
            raise ValueError("bad progress field")
        url = f"https://github.com/{o}/{r}/pull/{int(n)}"
    if (phase == "pr") != (url is not None and phase == "pr") or (url is not None and phase not in ("pr", "done")):
        raise ValueError("bad progress field")
    out = f"{PROGRESS_MARKER}\nccboard {alias}: {PHASE_TEXT[phase]}" + (f" {url}" if url else "")
    if branch is not None:
        out += f"\nBranch: {branch}"
    if text is not None:
        out += f"\n\n{RESULT_HEAD}\n```text\n{text}\n```" + (f"\n{NOTE_REMOVED}" if removed else "")
    if not valid_body(out):
        raise ValueError("bad progress field")
    return out


def _valid_progress(body: str) -> bool:
    if len(body) > 2400:
        return False
    m = _PROGRESS.fullmatch(body)
    if not m:
        return False
    if m["branch"] is not None and not (BRANCH_RE.fullmatch(m["branch"]) and ".." not in m["branch"]):
        return False
    phase = next(k for k, v in PHASE_TEXT.items() if v == m["phase"])
    if (phase == "pr") != (m["url"] is not None and phase == "pr") or (m["url"] is not None and (phase not in ("pr", "done") or not PR_URL_RE.fullmatch(m["url"]))):
        return False
    if m["text"] is not None:
        t = m["text"]
        if len(t) > RESULT_MAX or redact_result(t, 10 ** 6)[0] != t or SECRETISH.search(t) or (REMOVED in t and not m["note"]):
            return False
    return not SECRETISH.search(body.replace(PROGRESS_MARKER, "").replace(RESULT_HEAD, ""))


def valid_presence_body(body) -> bool:
    """True only for a body app/team.py `presence_body` makes: the marker and one to PRESENCE_LINES_MAX whitelisted lines, each one exactly as the builder writes it (strict parser,
    canonical order), and nothing secret-shaped in it."""
    if not isinstance(body, str) or len(body) > team.PRESENCE_BODY_MAX or not body.startswith(team.PRESENCE_MARKER + "\n"):
        return False
    lines = team.parse_body(body)
    if not lines:
        return False
    try:
        canon = team.presence_body([ln["raw"] for ln in lines])
    except ValueError:
        return False
    # the secret shape is looked for in the free value only (the branch): the repository is a checked slug, which may be 41 characters or more of letters and digits, and every
    # other value is a fixed word, a time or a number
    return canon == body and not any(ln.get("branch") and SECRETISH.search(ln["branch"]) for ln in lines)


def valid_presence_removal(before, body, nid) -> bool:
    """True when `body` is `before` without the line of board `nid` and with every other line exactly as it was (a removal edit may do nothing else)."""
    old, new = team.parse_body(before), team.parse_body(body)
    if old is None or new is None or not isinstance(nid, str) or not NID_RE.fullmatch(nid) or any(ln["nid"] == nid for ln in new):
        return False
    return sorted(ln["raw"] for ln in old if ln["nid"] != nid) == sorted(ln["raw"] for ln in new)


def valid_body(body) -> bool:
    """True only for HELP_BODY, a body `claim_body` could have made (its pattern, its fields checked again, nothing secret-shaped) or a body `progress_body` could have made (its
    pattern, its fields checked again, and a result that `redact_result` leaves as it is). A presence body is NOT one: it is valid only on a presence request (valid_presence_body)."""
    if body == HELP_BODY:
        return True
    if isinstance(body, str) and body.startswith(PROGRESS_MARKER):
        return _valid_progress(body)
    if not isinstance(body, str) or len(body) > 1200:
        return False
    m = _BODY.fullmatch(body)
    if not m or SECRETISH.search(body.replace(CLAIM_TEXT, "")):
        return False
    try:
        claim_body(m["alias"], m["nid"], int(m["lease"]), int(m["seq"]), m["status"], int(m["attempt"]), reclaims=int(m["reclaims"]) if m["reclaims"] else None, task=m["task"], branch=m["branch"])
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------- the request builders: every one validates what goes into a path

def _slug(owner, repo) -> str:
    if not github.valid_slug(owner, repo):
        raise GhWriteError("bad_request")
    return f"/repos/{owner}/{repo}"


def _num(n) -> int:
    if not github.valid_number(n):
        raise GhWriteError("bad_request")
    return int(n)


def _cid(cid) -> int:
    if isinstance(cid, bool) or not isinstance(cid, int) or not CID_RE.fullmatch(str(cid)):
        raise GhWriteError("bad_request")
    return cid


def _body(body) -> dict:
    if not valid_body(body):
        raise GhWriteError("bad_request")
    return {"body": body}


def _login(login) -> dict:
    if not isinstance(login, str) or not github.OWNER_RE.fullmatch(login):
        raise GhWriteError("bad_request")
    return {"assignees": [login]}


def comment_create(owner, repo, n, body) -> Request:
    return Request("POST", f"{_slug(owner, repo)}/issues/{_num(n)}/comments", _body(body))


def comment_edit(owner, repo, cid, body) -> Request:
    return Request("PATCH", f"{_slug(owner, repo)}/issues/comments/{_cid(cid)}", _body(body))


def comment_delete(owner, repo, cid) -> Request:
    return Request("DELETE", f"{_slug(owner, repo)}/issues/comments/{_cid(cid)}", None)


def assignee_add(owner, repo, n, login) -> Request:
    return Request("POST", f"{_slug(owner, repo)}/issues/{_num(n)}/assignees", _login(login))


def assignee_remove(owner, repo, n, login) -> Request:
    return Request("DELETE", f"{_slug(owner, repo)}/issues/{_num(n)}/assignees", _login(login))


def label_add(owner, repo, n) -> Request:
    return Request("POST", f"{_slug(owner, repo)}/issues/{_num(n)}/labels", {"labels": [LABEL]})


def label_remove(owner, repo, n) -> Request:
    return Request("DELETE", f"{_slug(owner, repo)}/issues/{_num(n)}/labels/ccboard%3Ain-progress", None)


def label_create(owner, repo) -> Request:
    return Request("POST", f"{_slug(owner, repo)}/labels", {"name": LABEL, "color": LABEL_COLOR, "description": LABEL_DESCRIPTION})


# ---- team presence (issue #198): the board's own presence comment, the status issue and its label. Each request carries its purpose; the gate decides before anything is built.

def _pbody(body) -> dict:
    if not valid_presence_body(body):
        raise GhWriteError("bad_request")
    return {"body": body}


def presence_comment_create(owner, repo, n, body) -> Request:
    return Request("POST", f"{_slug(owner, repo)}/issues/{_num(n)}/comments", _pbody(body), "presence")


def presence_comment_edit(owner, repo, cid, body, *, removal: bool = False, nid=None, before=None) -> Request:
    """Edit the board's own presence comment: to publish (purpose presence) or, with `removal`, to take this board's line out of it while other boards of the same person keep theirs.
    A removal must say whose line (`nid`) and what the comment held (`before`), and `body` must be exactly that without the line of `nid`; any other body is refused."""
    if removal and not valid_presence_removal(before, body, nid):
        raise GhWriteError("bad_request")
    return Request("PATCH", f"{_slug(owner, repo)}/issues/comments/{_cid(cid)}", _pbody(body), "presence_remove" if removal else "presence")


def presence_comment_delete(owner, repo, cid) -> Request:
    return Request("DELETE", f"{_slug(owner, repo)}/issues/comments/{_cid(cid)}", None, "presence_remove")


def presence_issue_create(owner, repo) -> Request:
    return Request("POST", f"{_slug(owner, repo)}/issues", {"title": PRESENCE_TITLE, "body": PRESENCE_ISSUE_BODY, "labels": [PRESENCE_LABEL]}, "presence")


def presence_label_create(owner, repo) -> Request:
    return Request("POST", f"{_slug(owner, repo)}/labels", {"name": PRESENCE_LABEL, "color": PRESENCE_LABEL_COLOR, "description": PRESENCE_LABEL_DESCRIPTION}, "presence")


def allowed_write(method, path) -> bool:
    """True for the method and path shapes of the list at the top of this module and nothing else (owner and repository checked again against GitHub's rules)."""
    if not isinstance(method, str) or not isinstance(path, str) or len(path) > 300:
        return False
    for m, rx in _PATHS:
        hit = rx.fullmatch(path) if m == method else None
        if hit and github.valid_slug(hit.group(1), hit.group(2)):
            return True
    return False


def _checked_body(req: Request) -> bytes | None:
    """The JSON the request carries, after checking that its KIND is the one its path takes (a comment path takes {body}, an assignees path {assignees: [login]}, the issue labels
    path {labels: [the one label]}, the repository labels path the one label's definition, and the delete paths of comments and labels take none) and every value in it."""
    b, p = req.body, req.path
    if req.purpose != "epic":
        if p.endswith("/comments") or "/issues/comments/" in p and req.method == "PATCH":
            ok = isinstance(b, dict) and set(b) == {"body"} and valid_presence_body(b["body"]) and (req.purpose == "presence" or req.method == "PATCH")
        elif p.endswith("/issues") and req.method == "POST":
            ok = req.purpose == "presence" and b == {"title": PRESENCE_TITLE, "body": PRESENCE_ISSUE_BODY, "labels": [PRESENCE_LABEL]}
        elif p.endswith("/labels") and "/issues/" not in p and req.method == "POST":
            ok = req.purpose == "presence" and b == {"name": PRESENCE_LABEL, "color": PRESENCE_LABEL_COLOR, "description": PRESENCE_LABEL_DESCRIPTION}
        else:
            ok = b is None and req.method == "DELETE" and "/issues/comments/" in p and req.purpose == "presence_remove"     # only the board's own comment, never a label or an assignee
        if not ok:
            raise GhWriteError("bad_request")
        return None if b is None else json.dumps(b, separators=(",", ":")).encode("utf-8")
    if p.endswith("/issues"):
        raise GhWriteError("bad_request")                                  # an epic write never creates an issue
    if p.endswith("/comments") or "/issues/comments/" in p and req.method == "PATCH":
        ok = isinstance(b, dict) and set(b) == {"body"} and valid_body(b["body"])
    elif p.endswith("/assignees"):
        ok = isinstance(b, dict) and set(b) == {"assignees"} and isinstance(b["assignees"], list) and len(b["assignees"]) == 1 and isinstance(b["assignees"][0], str) \
            and bool(github.OWNER_RE.fullmatch(b["assignees"][0]))
    elif p.endswith("/labels") and "/issues/" in p:
        ok = b == {"labels": [LABEL]}
    elif p.endswith("/labels"):
        ok = b == {"name": LABEL, "color": LABEL_COLOR, "description": LABEL_DESCRIPTION}
    else:
        ok = b is None                                              # DELETE of a comment or of the label
    if not ok:
        raise GhWriteError("bad_request")
    return None if b is None else json.dumps(b, separators=(",", ":")).encode("utf-8")


def argv(req: Request, exe: str = "gh") -> list[str]:
    """The one command line the writer builds: `gh api --include --method <M> -H ... --input - <path>` (the body goes on stdin). Refuses with writes_off BEFORE anything is
    built when CCBOARD_EPIC_WRITES is off, and with bad_request for any method and path that is not on the allow list or any body that is not made from validated fields."""
    _off(req)
    if not isinstance(req, Request) or not allowed_write(req.method, req.path):
        raise GhWriteError("bad_request")
    _checked_body(req)
    out = [exe, "api", "--include", "--method", req.method, "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28"]
    if req.body is not None:
        out += ["--input", "-"]
    return out + [req.path]


# ---------------------------------------------------------------- the transport: the one seam that runs gh (tests replace it; tests/conftest.py makes it refuse)

def transport(req: Request) -> tuple[int, dict, Any]:
    """Run one write: (status, headers, json). Any HTTP status is returned; a failure of gh itself is a GhWriteError (a timeout is unknown=True). Reached only through `Queue.run`."""
    _off(req)
    exe = shutil.which("gh")
    if not exe:
        raise GhWriteError("no_gh")
    args = argv(req, exe)
    data_in = _checked_body(req)
    try:
        cp = subprocess.run(args, input=data_in if data_in is not None else b"", capture_output=True, timeout=TIMEOUT,
                            env={**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "GH_NO_UPDATE_NOTIFIER": "1", "GH_SPINNER_DISABLED": "1"})
    except subprocess.TimeoutExpired:
        raise GhWriteError("timeout", unknown=True) from None
    except OSError:
        raise GhWriteError("no_gh") from None
    if len(cp.stdout) > OUT_CAP:
        raise GhWriteError("bad_answer", unknown=True)
    status, headers, body = github._parse_output(cp.stdout)
    if status is None:
        err = (cp.stderr or b"")[:4096].decode("utf-8", "replace").lower()
        if cp.returncode == 4 or "gh auth login" in err:
            raise GhWriteError("no_login")
        raise GhWriteError("unavailable", unknown=True)
    data = None
    if body.strip():
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            data = None
    return status, headers, data


# ---------------------------------------------------------------- the queue

class Queue:
    """One write at a time, MIN_GAP seconds apart, CEILING a minute, a back-off after 403/429. `mono` and `sleep` are injectable (tests pass a fake clock). Each board has one; the
    module's `queue` is this board's. Nothing here retries: a refusal or an unknown outcome is raised once and the caller decides."""

    def __init__(self, mono=None, sleep=None, gap: float = MIN_GAP, ceiling: int = CEILING, lock_timeout: float = LOCK_TIMEOUT):
        self.mono = mono or time.monotonic
        self.sleep = sleep or time.sleep
        self.gap, self.ceiling, self.lock_timeout = gap, ceiling, lock_timeout
        self.lock = threading.Lock()
        self.last: float | None = None
        self.until = 0.0
        self.stamps: "collections.deque[float]" = collections.deque(maxlen=ceiling)
        self.count = 0
        self.slept = 0.0

    def wait_left(self) -> int:
        return max(0, math.ceil(self.until - self.mono()))

    def _pace(self) -> None:
        now = self.mono()
        need = self.until - now
        if self.last is not None:
            need = max(need, self.last + self.gap - now)
        if len(self.stamps) >= self.ceiling:
            need = max(need, self.stamps[0] + CEILING_WINDOW - now)
        if need > MAX_WAIT:
            raise GhWriteError("rate_limited", wait_s=math.ceil(need))
        if need > 0:
            self.slept += need
            self.sleep(need)

    def run(self, req: Request, tport=None) -> tuple[int, dict, Any]:
        """Send one request through the allow list, the pacing and the transport: (status, headers, json) for a 2xx. A 4xx/5xx is a GhWriteError (5xx and unknown outcomes have
        unknown=True; 403/429 set the back-off and are rate_limited when GitHub asked us to wait)."""
        _off(req)
        argv(req)                                                       # the allow list and the body rule, before the lock and before any wait
        if not self.lock.acquire(timeout=self.lock_timeout):
            raise GhWriteError("busy")                              # another write holds the turn (or is waiting out a back-off): refused, nothing sent, never retried here
        try:
            _off(req)
            self._pace()
            now = self.mono()
            self.last = now
            self.stamps.append(now)
            self.count += 1
            status, headers, data = (tport or transport)(req)
            wait = github.backoff_wait(status, headers, github.server_time(headers), data.get("message") if isinstance(data, dict) else None)
            if wait:
                self.until = max(self.until, self.mono() + wait)
            if 200 <= status < 300:
                return status, headers, data
            if wait:
                raise GhWriteError("rate_limited", wait_s=wait, status=status)
            if status == 401:
                raise GhWriteError("no_login", status=status)
            msg = data.get("message") if isinstance(data, dict) else None
            if status in (403, 422) and isinstance(msg, str) and "interaction" in msg[:300].lower():
                raise GhWriteError("limited", status=status)                # the repository's interaction limits (a fixed reason; GitHub's text is not passed on)
            if status == 403:
                raise GhWriteError("no_access", status=status)
            if status in (404, 410):
                raise GhWriteError("not_found", status=status)
            if status in (409, 422):
                raise GhWriteError("invalid", status=status)
            if status >= 500:
                raise GhWriteError("unavailable", unknown=True, status=status)
            raise GhWriteError("bad_answer", unknown=True, status=status)
        finally:
            self.lock.release()


queue = Queue()


def send(req: Request, q: "Queue | None" = None) -> tuple[int, dict, Any]:
    """Send `req` through the board's queue (or `q`). Raises GhWriteError("writes_off") before anything else when writes are off."""
    _off(req)
    return (q or queue).run(req)
