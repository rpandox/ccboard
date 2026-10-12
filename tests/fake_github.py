"""A fake `gh` for the tests of the epic view (nodes epic, issue #147): an executable script on a temp PATH that serves a small in-memory GitHub.

`FakeGitHub(tmp_path).install(monkeypatch)` writes the script, puts its folder first on PATH, sets a secret-shaped GH_TOKEN (the fake leaks it on stderr on every
error, so a test can prove the board never passes gh's stderr on) and gives `app.github` its real `api` back (tests/conftest.py replaces it with a refusal, so no
test ever reaches the real gh). Every call is logged to a file; `calls()` returns them as dicts.

The world (`fake.world`, saved after each helper) is plain data: issues, sub-issue links, comments, a server clock (`now`, the Date header), rate-limit numbers,
a login (or none), scopes, a page size, forbidden repositories, a missing sub-issues endpoint and one-shot overrides (`fail(...)`). Answers carry an ETag; a call
with `If-None-Match` for the current ETag gets a 304. The script exits 1 on every answer that is not 2xx (as the real gh does), and 4 with no login.
No host name, e-mail address or real account appears here: acme/shop, node-a, example.com.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from app import github, nodes_epics

REAL_API = github.api
REAL_GH_LOGIN = nodes_epics._gh_login
SECRET = "ghp_" + "A1b2C3d4" * 4                    # shaped like a classic token, built by concatenation; the fake leaks it on stderr, the board must never show it
START = 1_800_000_000                               # the server clock of a new world (2027-01-15), epoch seconds

SCRIPT = r'''#!__PY__
import email.utils, hashlib, json, os, re, sys
W = os.environ["FAKE_GH_WORLD"]
LOG = os.environ["FAKE_GH_LOG"]
args = sys.argv[1:]
with open(LOG, "a") as fh:
    fh.write(json.dumps({"argv": args}) + "\n")
world = json.load(open(W))
TOKEN = os.environ.get("GH_TOKEN", "")


def die(rc, text):
    sys.stderr.write(text + " " + TOKEN + "\n")        # a leaky gh: the token rides on stderr
    sys.exit(rc)


if args[:1] == ["--version"]:
    print("gh version 2.99.0 (2026-09-01)")
    print("https://github.com/cli/cli/releases/tag/v2.99.0")
    sys.exit(0)
if args[:2] == ["auth", "status"]:
    sys.exit(0 if world.get("login") else 1)
if args[:2] == ["api", "user"]:
    if not world.get("login"):
        die(4, "To get started with GitHub CLI, please run:  gh auth login")
    print(json.dumps({"login": world["login"]}))
    sys.exit(0)
if args[:1] != ["api"]:
    die(1, "unknown command")
if not world.get("login"):
    die(4, "To get started with GitHub CLI, please run:  gh auth login")
method, inm, path, include = "GET", None, None, False
i = 1
while i < len(args):
    a = args[i]
    if a == "--include":
        include = True
    elif a in ("--method", "-X"):
        i += 1
        method = args[i]
    elif a in ("-H", "--header"):
        i += 1
        k, _, v = args[i].partition(":")
        if k.strip().lower() == "if-none-match":
            inm = v.strip()
    elif a.startswith("-"):
        method = "BAD-FLAG " + a
    else:
        path = a
    i += 1
now = world["now"]
rate = world.get("rate")
headers = {"Date": email.utils.formatdate(now, usegmt=True), "Content-Type": "application/json; charset=utf-8"}
if rate:
    headers.update({"x-ratelimit-limit": str(rate["limit"]), "x-ratelimit-remaining": str(rate["remaining"]), "x-ratelimit-reset": str(rate["reset"])})
if world.get("scopes") is not None:
    headers["x-oauth-scopes"] = world["scopes"]


def ts(v):
    return v


def respond(status, body, extra=None):
    h = dict(headers)
    h.update(extra or {})
    text = json.dumps(body) if body is not None else ""
    if status < 300:
        tag = 'W/"' + hashlib.sha1((text + world.get("salt", "")).encode()).hexdigest()[:16] + '"'
        if status == 200 and inm == tag:
            status, text = 304, ""
        h["ETag"] = tag
    reason = {200: "OK", 304: "Not Modified", 403: "Forbidden", 404: "Not Found", 429: "Too Many Requests", 500: "Internal Server Error", 502: "Bad Gateway"}.get(status, "X")
    out = "HTTP/2.0 %d %s\r\n" % (status, reason) + "".join("%s: %s\r\n" % kv for kv in h.items()) + "\r\n" + text
    sys.stdout.write(out if include else text)
    if status >= 300:
        sys.stderr.write("gh: %s (HTTP %d) %s\n" % (reason, status, TOKEN))
        sys.exit(1)
    sys.exit(0)


if method != "GET":
    respond(405, {"message": "only GET is served here: " + method})
for ov in world.get("overrides", []):
    if ov["match"] in path and ov.get("times", 1) != 0:
        if ov.get("times", 1) > 0:
            ov["times"] -= 1
            json.dump(world, open(W, "w"))
        respond(ov["status"], ov.get("body", {"message": "override"}), ov.get("headers"))


for hk in world.get("mutations", []):                    # the world changes between two calls: just before a call whose path contains `match` (after `skip` such calls)
    hit = (path == hk["match"][1:]) if hk["match"].startswith("=") else (hk["match"] in (path or ""))                # "=<path>": that path exactly
    if hit and hk.get("times", 1) != 0:
        if hk.get("skip", 0) > 0:
            hk["skip"] -= 1
        else:
            if hk.get("times", 1) > 0:
                hk["times"] -= 1
            lst = world.setdefault("comments", {}).setdefault(hk["issue"], [])
            if hk["op"] == "add":
                lst.append(hk["comment"])
                hk["comment"] = dict(hk["comment"], id=hk["comment"]["id"] + 1)           # a repeated add posts a new comment, not the same one again
            elif hk["op"] == "delete" and lst:
                lst.pop(hk["index"])
        json.dump(world, open(W, "w"))


def key(o, r, n):
    return "%s/%s#%s" % (o, r, n)


def issue_obj(k):
    it = world["issues"][k]
    repo = k.split("#")[0]
    n = int(k.split("#")[1])
    o = {"number": n, "title": it.get("title", "Issue %d" % n), "state": it.get("state", "open"), "body": it.get("body", ""),
         "labels": [{"name": x} for x in it.get("labels", [])], "user": {"login": it.get("user", "alice")},
         "comments": len(world.get("comments", {}).get(k, [])),
         "repository_url": "https://api.github.com/repos/" + repo, "html_url": "https://github.com/%s/issues/%d" % (repo, n),
         "created_at": it.get("created_at", "2026-10-01T09:00:00Z"), "updated_at": it.get("updated_at", "2026-10-01T09:00:00Z")}
    if it.get("pull_request"):
        o["pull_request"] = {"url": "https://api.github.com/repos/%s/pulls/%d" % (repo, n)}
    if it.get("summary") is not None:
        o["sub_issues_summary"] = it["summary"]
    o.update(it.get("raw", {}))
    return o


def paged(items, query):
    q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
    per = min(int(q.get("per_page", "30")), world.get("page_size", 100))
    page = int(q.get("page", "1"))
    chunk = items[(page - 1) * per: page * per]
    more = page * per < len(items)
    return chunk, ({"Link": '<https://api.github.com/next>; rel="next"'} if more else {})


m = re.fullmatch(r"/repos/([^/]+)/([^/?]+)/issues/([0-9]+)(?:/(sub_issues|comments))?(?:\?(.*))?", path or "")
if path == "/rate_limit":
    r = rate or {"limit": 5000, "remaining": 5000, "reset": int(now) + 3600}
    respond(200, {"resources": {"core": {"limit": r["limit"], "remaining": r["remaining"], "reset": r["reset"]}}})
if m:
    o, r, n, sub, query = m.groups()
    if "%s/%s" % (o, r) in world.get("forbidden", []):
        respond(403, {"message": "Resource not accessible"})
    k = key(o, r, n)
    if k not in world["issues"]:
        respond(404, {"message": "Not Found"})
    if sub is None:
        respond(200, issue_obj(k))
    if sub == "sub_issues":
        if world.get("sub_issues_missing"):
            respond(404, {"message": "Not Found"})
        items = [issue_obj(c) for c in world.get("subs", {}).get(k, [])]
        chunk, extra = paged(items, query or "")
        respond(200, chunk, extra)
    items = []
    for c in world.get("comments", {}).get(k, []):
        c = dict(c, user=None if c["user"] is None else {"login": c["user"], "type": c.get("user_type", "User")}, author_association=c.get("assoc", "COLLABORATOR"))
        c.pop("user_type", None)
        c.pop("assoc", None)
        items.append(c)
    chunk, extra = paged(items, query or "")
    respond(200, chunk, extra)
m = re.fullmatch(r"/repos/([^/]+)/([^/?]+)/issues\?(.*)", path or "")
if m:
    o, r, query = m.groups()
    if "%s/%s" % (o, r) in world.get("forbidden", []):
        respond(403, {"message": "Resource not accessible"})
    q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
    label = q.get("labels", "").replace("%3A", ":")
    state = q.get("state", "open")
    ks = [k for k, it in world["issues"].items() if k.startswith("%s/%s#" % (o, r)) and label in it.get("labels", []) and (state == "all" or it.get("state", "open") == state)]
    ks.sort(key=lambda k: int(k.split("#")[1]), reverse=True)
    chunk, extra = paged([issue_obj(k) for k in ks], query)
    respond(200, chunk, extra)
respond(404, {"message": "Not Found"})
'''


def claim_body(alias="node-a", status="claimed", *, lease="30m", seq=1, attempt=1, nid="a1b2c3d4", task=None, branch=None, version="v1", extra="", text="Claimed by a node.") -> str:
    """A claim comment as the claim phase (issue #148) writes it."""
    line = f"ccboard claim: alias={alias} nid={nid} lease={lease} seq={seq} status={status} attempt={attempt}"
    if task is not None:
        line += f" task={task}"
    if branch is not None:
        line += f" branch={branch}"
    return f"<!-- ccboard:claim {version} -->\n{line}{extra}\n\n{text}"


def iso(epoch: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    def __init__(self, tmp_path: Path):
        self.dir = Path(tmp_path) / "fake-gh"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.world_path = self.dir / "world.json"
        self.log_path = self.dir / "calls.jsonl"
        self.world: dict = {"login": "alice", "now": START, "rate": {"limit": 5000, "remaining": 4990, "reset": START + 1800}, "scopes": "repo, read:org",
                            "issues": {}, "subs": {}, "comments": {}, "sub_issues_missing": False, "page_size": 100, "forbidden": [], "overrides": [], "salt": ""}
        self._next_comment = 1000
        self.save()

    # ---- wiring
    def install(self, monkeypatch) -> "FakeGitHub":
        script = self.dir / "gh"
        script.write_text(SCRIPT.replace("__PY__", sys.executable))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", str(self.dir) + os.pathsep + os.environ.get("PATH", ""))
        monkeypatch.setenv("FAKE_GH_WORLD", str(self.world_path))
        monkeypatch.setenv("FAKE_GH_LOG", str(self.log_path))
        monkeypatch.setenv("GH_TOKEN", SECRET)
        monkeypatch.setattr(github, "api", REAL_API)
        monkeypatch.setattr(nodes_epics, "_gh_login", REAL_GH_LOGIN)
        return self

    def save(self) -> None:
        """Write the world. The file's overrides are kept, not this copy's: the script counts them down in the file, and a save from here must not bring a used one back."""
        if self.world_path.exists():
            on_disk = json.loads(self.world_path.read_text())
            for k in ("overrides", "mutations"):
                self.world[k] = on_disk.get(k, [])
        self.world_path.write_text(json.dumps(self.world))

    def reload(self) -> None:
        """Take from the file what the script changes on its own: the one-shot overrides, the between-call mutations and the comments they add or delete."""
        on_disk = json.loads(self.world_path.read_text())
        for k in ("overrides", "mutations", "comments"):
            self.world[k] = on_disk.get(k, [] if k != "comments" else {})

    def calls(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines() if line.strip()]

    def paths(self) -> list[str]:
        """The path of every `gh api` call, in order."""
        return [c["argv"][-1] for c in self.calls() if c["argv"][:1] == ["api"] and c["argv"][-1] != "user"]

    def clear_calls(self) -> None:
        if self.log_path.exists():
            self.log_path.unlink()

    # ---- the world
    def set(self, **kw) -> None:
        self.reload()
        self.world.update(kw)
        self.save()

    def tick(self, seconds: float) -> None:
        self.reload()
        self.world["now"] += seconds
        self.save()

    def add_issue(self, repo: str, number: int, title: str = "", *, state="open", labels=(), body="", user="alice", pull_request=False, summary=None, raw=None,
                  updated_at=None) -> str:
        k = f"{repo}#{number}"
        self.reload()
        self.world["issues"][k] = {"title": title or f"Issue {number}", "state": state, "labels": list(labels), "body": body, "user": user, "pull_request": pull_request,
                                   "summary": summary, "raw": raw or {}, "updated_at": updated_at or "2026-10-01T09:00:00Z"}
        self.save()
        return k

    def link(self, parent: str, child: str) -> None:
        self.reload()
        self.world["subs"].setdefault(parent, []).append(child)
        self.save()

    def comment(self, issue: str, body: str, *, user="alice", created_at=None, updated_at=None, cid=None, assoc="COLLABORATOR", user_type="User") -> int:
        """One comment. `assoc` is GitHub's author_association (OWNER, MEMBER, COLLABORATOR, CONTRIBUTOR, NONE...), `user_type` the user's type (User, Bot); user=None
        is a comment with no user object (a deleted account)."""
        self.reload()
        self._next_comment += 1
        cid = cid or self._next_comment
        created = created_at or iso(self.world["now"] - 60)
        self.world["comments"].setdefault(issue, []).append({"id": cid, "body": body, "user": user, "created_at": created, "updated_at": updated_at or created,
                                                             "assoc": assoc, "user_type": user_type})
        self.save()
        return cid

    def junk(self, issue: str, count: int, *, start: float | None = None, user="bob") -> None:
        """`count` plain comments (no claim), one second apart from `start` (default: a day before the server clock), in one save."""
        self.reload()
        t0 = (self.world["now"] - 86400) if start is None else start
        lst = self.world["comments"].setdefault(issue, [])
        for i in range(count):
            self._next_comment += 1
            lst.append({"id": self._next_comment, "body": f"chatter {i}", "user": user, "created_at": iso(t0 + i), "updated_at": iso(t0 + i), "assoc": "NONE", "user_type": "User"})
        self.save()

    def mutate(self, match: str, issue: str, op: str, *, comment: str | None = None, index: int = 0, skip: int = 0, times: int = 1, user="alice", assoc="COLLABORATOR",
               created_at=None) -> None:
        """Change the world just before the next `times` calls whose path contains `match` (or is exactly the rest of it after a leading "=") (after skipping `skip` of them): op "add" appends a comment (`comment` is
        its body; its id follows the last comment's, so ids keep increasing) to `issue`, op "delete" removes the comment at `index`. This is how a test makes a thread
        move between two page reads."""
        self.reload()
        rec = None
        if op == "add":
            last = max([c["id"] for c in self.world["comments"].get(issue, [])] + [self._next_comment])
            self._next_comment = last + 1000
            rec = {"id": last + 500, "body": comment or "later", "user": user, "created_at": created_at or iso(self.world["now"] - 30), "updated_at": created_at or iso(self.world["now"] - 30),
                   "assoc": assoc, "user_type": "User"}
        self.world.setdefault("mutations", []).append({"match": match, "issue": issue, "op": op, "comment": rec, "index": index, "skip": skip, "times": times})
        self.world_path.write_text(json.dumps(self.world))

    def clear_mutations(self) -> None:
        self.reload()
        self.world["mutations"] = []
        self.world_path.write_text(json.dumps(self.world))

    def fail(self, match: str, status: int, *, headers=None, body=None, times: int = 1) -> None:
        """The next `times` (-1: always) calls whose path contains `match` get this answer."""
        self.reload()
        self.world["overrides"].append({"match": match, "status": status, "headers": headers or {}, "body": {"message": "override"} if body is None else body, "times": times})
        self.world_path.write_text(json.dumps(self.world))
