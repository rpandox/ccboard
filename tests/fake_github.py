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
import threading
from pathlib import Path

from app import github, github_write, nodes_epics
from tests import gh_server

REAL_API = github.api
REAL_TRANSPORT = github_write.transport
REAL_GH_LOGIN = nodes_epics._gh_login
SECRET = "ghp_" + "A1b2C3d4" * 4                    # shaped like a classic token, built by concatenation; the fake leaks it on stderr, the board must never show it
START = 1_800_000_000                               # the server clock of a new world (2027-01-15), epoch seconds

SCRIPT = r'''#!__PY__
import os, sys
sys.path.insert(0, os.environ["FAKE_GH_TESTS"])
import gh_server
sys.exit(gh_server.main(sys.argv[1:], os.environ, sys.stdin, os.environ["FAKE_GH_WORLD"], os.environ["FAKE_GH_LOG"], sys.stdout, sys.stderr))
'''


def claim_body(alias="node-a", status="claimed", *, lease="30m", seq=1, attempt=1, nid="a1b2c3d4", task=None, branch=None, version="v1", extra="", text="Claimed by a node.") -> str:
    """A claim comment as the claim phase (issue #148) writes it."""
    line = f"ccboard claim: alias={alias} nid={nid} lease={lease} seq={seq} status={status} attempt={attempt}"
    if task is not None:
        line += f" task={task}"
    if branch is not None:
        line += f" branch={branch}"
    return f"<!-- ccboard:claim {version} -->\n{line}{extra}\n\n{text}"


iso = gh_server.iso


class FakeGitHub:
    def __init__(self, tmp_path: Path):
        self.dir = Path(tmp_path) / "fake-gh"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.world_path = self.dir / "world.json"
        self.log_path = self.dir / "calls.jsonl"
        self.world: dict = {"login": "alice", "now": START, "rate": {"limit": 5000, "remaining": 4990, "reset": START + 1800}, "scopes": "repo, read:org",
                            "issues": {}, "subs": {}, "comments": {}, "sub_issues_missing": False, "page_size": 100, "forbidden": [], "overrides": [], "salt": ""}
        self._next_comment = 1000
        self.mem = False
        self.save()

    # ---- wiring
    def install(self, monkeypatch) -> "FakeGitHub":
        script = self.dir / "gh"
        script.write_text(SCRIPT.replace("__PY__", sys.executable))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", str(self.dir) + os.pathsep + os.environ.get("PATH", ""))
        monkeypatch.setenv("FAKE_GH_WORLD", str(self.world_path))
        monkeypatch.setenv("FAKE_GH_LOG", str(self.log_path))
        monkeypatch.setenv("FAKE_GH_TESTS", str(Path(__file__).resolve().parent))
        monkeypatch.setenv("GH_TOKEN", SECRET)
        monkeypatch.setattr(github, "api", REAL_API)
        monkeypatch.setattr(github_write, "transport", REAL_TRANSPORT)
        monkeypatch.setattr(nodes_epics, "_gh_login", REAL_GH_LOGIN)
        return self

    def use_memory(self, monkeypatch, hook=None) -> "FakeGitHub":
        """Serve the same GitHub from this process instead of through the fake gh script: `github.api` (reads) and `github_write.transport` (writes) call
        tests/gh_server.py on the live `world`, one request at a time, and log the call like the script does. No process is started, so hundreds of races are cheap.
        `hook(kind, method, path)` runs before each request (a test's scheduler can yield there). The world is the live dict: save() and reload() do nothing."""
        self.mem = True
        self.mem_lock = threading.RLock()
        self.mem_calls: list[dict] = []

        def serve(method, path, inm=None, body=None):
            if hook is not None:
                hook("read" if method == "GET" else "write", method, path)
            with self.mem_lock:
                self.mem_calls.append({"argv": ["api", "--method", method, path], "stdin": json.dumps(body) if body is not None else ""})
                resp, _ = gh_server.handle(self.world, method, path, inm, body)
                return resp

        def api(path, etag=None):
            resp = serve("GET", path, etag)
            if resp.hang:
                raise github.GhReadError("timeout")
            headers = {k.lower(): v for k, v in resp.headers.items() if k.lower() in github.SAFE_HEADERS}
            return resp.status, headers, resp.body

        def transport(req):
            resp = serve(req.method, req.path, None, req.body)
            if resp.hang:
                raise github_write.GhWriteError("timeout", unknown=True)
            headers = {k.lower(): v for k, v in resp.headers.items() if k.lower() in github.SAFE_HEADERS}
            return resp.status, headers, resp.body
        monkeypatch.setattr(github, "api", api)
        monkeypatch.setattr(github_write, "transport", transport)
        monkeypatch.setattr(nodes_epics, "_gh_login", lambda: self.world.get("login"))
        return self

    def save(self) -> None:
        """Write the world. The file's overrides are kept, not this copy's: the script counts them down in the file, and a save from here must not bring a used one back."""
        if self.mem:
            return
        if self.world_path.exists():
            on_disk = json.loads(self.world_path.read_text())
            for k in ("overrides", "mutations"):
                self.world[k] = on_disk.get(k, [])
        self.world_path.write_text(json.dumps(self.world))

    def reload(self) -> None:
        """Take from the file what the script changes on its own: the one-shot overrides, the between-call mutations and the comments they add or delete."""
        if self.mem:
            return
        on_disk = json.loads(self.world_path.read_text())
        for k in ("overrides", "mutations", "comments"):
            self.world[k] = on_disk.get(k, [] if k != "comments" else {})

    def refresh(self) -> None:
        """Take EVERYTHING from the file: after writes through the fake gh (comments, assignees, labels, ids), the world as GitHub now holds it."""
        if self.mem:
            return
        self.world.clear()
        self.world.update(json.loads(self.world_path.read_text()))

    def calls(self) -> list[dict]:
        if self.mem:
            return list(self.mem_calls)
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines() if line.strip()]

    def paths(self) -> list[str]:
        """The path of every `gh api` call, in order."""
        return [c["argv"][-1] for c in self.calls() if c["argv"][:1] == ["api"] and c["argv"][-1] != "user"]

    def clear_calls(self) -> None:
        if self.mem:
            self.mem_calls.clear()
        elif self.log_path.exists():
            self.log_path.unlink()

    def writes(self) -> list[tuple]:
        """(method, path, body) of every call that was not a GET, in order (body parsed from the stdin the call carried, None when it had none)."""
        out = []
        for c in self.calls():
            a = c["argv"]
            if a[:1] != ["api"] or "--method" not in a:
                continue
            method = a[a.index("--method") + 1]
            if method != "GET":
                out.append((method, a[-1], json.loads(c["stdin"]) if c.get("stdin") else None))
        return out

    # ---- the world as GitHub now holds it, for assertions
    def thread(self, issue: str) -> list[dict]:
        """The comments of an issue, all of them (hidden ones too), as stored."""
        self.refresh()
        return [dict(c) for c in self.world.get("comments", {}).get(issue, [])]

    def claims_on(self, issue: str) -> list[dict]:
        """The comments of an issue that carry the claim marker."""
        return [c for c in self.thread(issue) if str(c["body"]).startswith("<!-- ccboard:claim")]

    def labels_of(self, issue: str) -> list[str]:
        self.refresh()
        return list(self.world["issues"][issue].get("labels", []))

    def assignees_of(self, issue: str) -> list[str]:
        self.refresh()
        return list(self.world["issues"][issue].get("assignees", []))

    def hide(self, cid: int, seconds: float) -> None:
        """Eventual consistency: reads do not show comment `cid` until `seconds` of server time have passed."""
        self.refresh()
        for lst in self.world["comments"].values():
            for c in lst:
                if c["id"] == cid:
                    c["hidden_until"] = self.world["now"] + seconds
        self.save()

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

    def junk(self, issue: str, count: int, *, start: float | None = None, user="bob", first_id: int | None = None) -> None:
        """`count` plain comments (no claim), one second apart from `start` (default: a day before the server clock), in one save. `first_id` makes their ids start there
        (a thread whose ids must keep growing after comments GitHub's writes created)."""
        self.reload()
        t0 = (self.world["now"] - 86400) if start is None else start
        lst = self.world["comments"].setdefault(issue, [])
        if first_id is not None:
            self._next_comment = first_id - 1
        for i in range(count):
            self._next_comment += 1
            lst.append({"id": self._next_comment, "body": f"chatter {i}", "user": user, "created_at": iso(t0 + i), "updated_at": iso(t0 + i), "assoc": "NONE", "user_type": "User"})
        self.save()

    def mutate(self, match: str, issue: str, op: str, *, comment: str | None = None, index: int = 0, skip: int = 0, times: int = 1, user="alice", assoc="COLLABORATOR",
               created_at=None, method: str | None = None) -> None:
        """Change the world just before the next `times` calls whose path contains `match` (or is exactly the rest of it after a leading "=") (after skipping `skip` of them): op "add" appends a comment (`comment` is
        its body; its id follows the last comment's, so ids keep increasing) to `issue`, op "delete" removes the comment at `index`. This is how a test makes a thread
        move between two page reads."""
        self.reload()
        rec = None
        if op == "add":
            last = max([c["id"] for c in self.world["comments"].get(issue, [])] + [self._next_comment, self.world.get("next_cid", 0)])
            self._next_comment = last + 1000
            rec = {"id": last + 500, "body": comment or "later", "user": user, "created_at": created_at or iso(self.world["now"] - 30), "updated_at": created_at or iso(self.world["now"] - 30),
                   "assoc": assoc, "user_type": "User"}
        self.world.setdefault("mutations", []).append({"match": match, "issue": issue, "op": op, "comment": rec, "index": index, "skip": skip, "times": times, "method": method})
        self._flush()

    def clear_mutations(self) -> None:
        self.reload()
        self.world["mutations"] = []
        self._flush()

    def _flush(self) -> None:
        if not self.mem:
            self.world_path.write_text(json.dumps(self.world))

    def fail(self, match: str, status: int, *, headers=None, body=None, times: int = 1, method: str | None = None, apply: bool = False, hang: bool = False) -> None:
        """The next `times` (-1: always) calls whose path contains `match` (and whose method is `method`, when given) get this answer. `apply=True` lets a write happen first
        and then answers with the fault: a comment created whose answer was lost. `hang=True` is a gh that never answers (a timeout; with `apply` the write happened)."""
        self.reload()
        self.world["overrides"].append({"match": match, "status": status, "headers": headers or {}, "body": {"message": "override"} if body is None else body, "times": times,
                                        "method": method, "apply": apply, "hang": hang})
        self._flush()
