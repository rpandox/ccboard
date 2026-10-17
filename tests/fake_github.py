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
import time
from pathlib import Path

from app import github, github_write, nodes_epics, prpoll, team_github
from tests import gh_server

REAL_API = github.api
REAL_GRAPHQL = github.graphql
REAL_GRAPHQL_EDITORS = github.graphql_editors
REAL_GRAPHQL_EDITOR = github.graphql_editor
REAL_GRAPHQL_EDIT_HISTORY = github.graphql_edit_history
REAL_IDENTITY = team_github.identity
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
        monkeypatch.setattr(github, "graphql", REAL_GRAPHQL)
        monkeypatch.setattr(github, "graphql_editors", REAL_GRAPHQL_EDITORS)
        monkeypatch.setattr(github, "graphql_editor", REAL_GRAPHQL_EDITOR)
        monkeypatch.setattr(github, "graphql_edit_history", REAL_GRAPHQL_EDIT_HISTORY)
        monkeypatch.setattr(team_github, "identity", REAL_IDENTITY)
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
                self.mem_calls[-1]["status"] = resp.status
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
        def graphql(owner, repo):
            if hook is not None:
                hook("read", "POST", "graphql")
            with self.mem_lock:
                self.mem_calls.append({"argv": github.graphql_argv(owner, repo, "gh")[1:], "stdin": ""})
                resp, _ = gh_server.handle_graphql(self.world, owner, repo)
                self.mem_calls[-1]["status"] = resp.status
            headers = {k.lower(): v for k, v in resp.headers.items() if k.lower() in github.SAFE_HEADERS}
            return resp.status, headers, resp.body
        def graphql_editors(owner, repo, n):
            if hook is not None:
                hook("read", "POST", "graphql")
            with self.mem_lock:
                self.mem_calls.append({"argv": github.graphql_editors_argv(owner, repo, n, "gh")[1:], "stdin": ""})
                resp, _ = gh_server.handle_graphql_editors(self.world, owner, repo, n)
                self.mem_calls[-1]["status"] = resp.status
            headers = {k.lower(): v for k, v in resp.headers.items() if k.lower() in github.SAFE_HEADERS}
            return resp.status, headers, resp.body
        def graphql_editor(node_id):
            if hook is not None:
                hook("read", "POST", "graphql")
            with self.mem_lock:
                self.mem_calls.append({"argv": github.graphql_editor_argv(node_id, "gh")[1:], "stdin": ""})
                resp, _ = gh_server.handle_graphql_editor(self.world, node_id)
                self.mem_calls[-1]["status"] = resp.status
            headers = {k.lower(): v for k, v in resp.headers.items() if k.lower() in github.SAFE_HEADERS}
            return resp.status, headers, resp.body
        def graphql_edit_history(node_id, cursor=None):
            if hook:
                hook("read", "POST", "graphql")
            with self.mem_lock:
                self.mem_calls.append({"argv": github.graphql_edit_history_argv(node_id, cursor, "gh")[1:], "stdin": ""})
                resp, _ = gh_server.handle_graphql_edit_history(self.world, node_id, cursor)
                return resp.status, resp.headers, resp.body

        monkeypatch.setattr(github, "graphql_edit_history", graphql_edit_history)
        monkeypatch.setattr(github, "api", api)
        monkeypatch.setattr(github, "graphql", graphql)
        monkeypatch.setattr(github, "graphql_editors", graphql_editors)
        monkeypatch.setattr(github, "graphql_editor", graphql_editor)
        monkeypatch.setattr(github_write, "transport", transport)

        def identity():
            """Like the real probe: the answer, and prpoll's cache filled with it (the node card reads only that cache)."""
            login = self.world.get("login")
            got = (login, gh_server.uid(self.world, login)) if login else (None, None)
            prpoll._gh_login = (time.monotonic() + prpoll.GH_LOGIN_TTL, got[0], got[1])          # conftest restores it after the test
            return got
        monkeypatch.setattr(team_github, "identity", identity)
        monkeypatch.setattr(nodes_epics, "_gh_login", lambda: self.world.get("login") if prpoll.gh_identity_cached()[0] == self.world.get("login") else identity()[0])
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
        """The path of every `gh api` call, in order (a GraphQL call is listed as `graphql:<owner>/<name>`)."""
        out = []
        for c in self.calls():
            a = c["argv"]
            if a[:2] == ["api", "graphql"]:
                f = {x.partition("=")[0]: x.partition("=")[2] for x in a if "=" in x and not x.startswith("query=")}
                out.append("graphql:%s/%s" % (f.get("owner"), f.get("name")))
            elif a[:1] == ["api"] and a[-1] != "user":
                out.append(a[-1])
        return out

    def graph_calls(self) -> list[str]:
        return [p for p in self.paths() if p.startswith("graphql:")]

    def points(self) -> int:
        """The points the calls so far cost: one for every REST call (a 304 counted too, because whether it is free is unverified) and the world's `gql_cost` for each GraphQL call."""
        cost = int(self.world.get("gql_cost", 1))
        return sum(cost if p.startswith("graphql:") else 1 for p in self.paths())

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

    # ---- the team world (issue #197)
    def assign(self, issue: str, *logins: str) -> None:
        self.reload()
        self.world["issues"][issue]["assignees"] = list(logins)
        self.save()

    def repo(self, slug: str, **info) -> None:
        """The repository record: private, push, full_name (a rename), fork, parent ("org/repo"), default_branch."""
        self.reload()
        self.world.setdefault("repos", {}).setdefault(slug, {}).update(info)
        self.save()

    def pull(self, repo: str, number: int, title: str = "", *, user="alice", draft=False, head_ref="feature", head_repo=None, closes=(), assignees=(), updated_at=None, assoc="COLLABORATOR") -> str:
        """An open pull request. `head_repo` is the repository its branch is on (a fork: "bob/shop"; default: the repository itself). `assoc` is GitHub's author_association."""
        self.reload()
        k = f"{repo}#{number}"
        self.world.setdefault("pulls", {})[k] = {"title": title or f"Pull {number}", "user": user, "draft": draft, "head_ref": head_ref, "head_repo": repo if head_repo is None else head_repo,
                                                "closes": list(closes), "assignees": list(assignees), "updated_at": updated_at or "2026-10-10T09:00:00Z", "assoc": assoc}
        self.save()
        return k

    def pull_files(self, repo: str, number: int, files) -> None:
        """The changed files of an open pull request (issue #199): names, or {filename, previous_filename} records. A pull request with no entry answers 404."""
        self.reload()
        self.world.setdefault("pull_files", {})[f"{repo}#{number}"] = list(files)
        self.save()

    def tip(self, repo: str, name: str, author: str | None = "bob", at: str | None = None) -> None:
        """A branch of the repository: its tip commit's GitHub author (None: a commit email GitHub cannot match to an account) and time."""
        self.reload()
        lst = self.world.setdefault("tips", {}).setdefault(repo, [])
        lst[:] = [t for t in lst if t["name"] != name] + [{"name": name, "author": author, "at": at or iso(self.world["now"] - 3600)}]
        self.save()

    def linked(self, issue: str, name: str, author: str | None = None, at: str | None = None) -> None:
        """A branch linked to an issue (gh issue develop)."""
        self.reload()
        self.world.setdefault("linked", {}).setdefault(issue, []).append({"name": name, "author": author, "at": at or iso(self.world["now"] - 1800)})
        self.save()

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
        gh_server._touch(self.world, issue)
        self.save()
        return cid

    def event(self, issue: str, event: str = "labeled", label: str | None = "ccboard:ready", *, by: str | None = "alice", actor_type: str = "User", actor_id: int | None = None,
              at: float | None = None, rename: dict | None = None) -> int:
        """One event of an issue (issue #157): `labeled` / `unlabeled` with the label's name, `renamed`, ... by `by` (None: a deleted account), `actor_type` User or Bot. Ids grow;
        `at` is the server time it happened (default: a minute before the clock). Nothing adds events by itself: an issue created with a label has no `labeled` event."""
        self.reload()
        lst = self.world.setdefault("events", {}).setdefault(issue, [])
        eid = 7000 + sum(len(v) for v in self.world["events"].values())
        lst.append({"id": eid, "event": event, "label": label if event in ("labeled", "unlabeled") else None, "actor": by, "actor_type": actor_type, "actor_id": actor_id,
                    "created_at": iso(self.world["now"] - 60 if at is None else at), "rename": rename})
        self.save()
        return eid

    def edit_issue(self, issue: str, body: str | None = None, *, by: str, title: str | None = None) -> None:
        """Somebody (`by`) edits the issue in place: its body (or title) changes and GraphQL's `editor` of the issue names them (issue #157)."""
        self.reload()
        it = self.world["issues"][issue]
        if body is not None:
            it["body"] = body
        if title is not None:
            it["title"] = title
        it.setdefault("history", []).append({"editor": by, "at": iso(self.world["now"])})
        it["editor"], it["last_edited_at"], it["updated_at"] = by, iso(self.world["now"]), iso(self.world["now"])
        self.save()

    def rename(self, issue: str, title: str, *, by: str) -> None:
        self.reload()
        old = self.world["issues"][issue].get("title", "")
        self.world["issues"][issue]["title"] = title
        self.world["issues"][issue]["updated_at"] = iso(self.world["now"])
        self.save()
        self.event(issue, "renamed", None, by=by, rename={"from": old, "to": title}, at=self.world["now"])

    def edit_as(self, issue: str, cid: int, body: str, by: str) -> None:
        """Somebody (`by`, e.g. a maintainer) edits comment `cid` in place: the body changes, `updated_at` moves and GraphQL's `editor` names them (issue #198)."""
        self.reload()
        for c in self.world["comments"].get(issue, []):
            if c["id"] == cid:
                c["body"], c["updated_at"], c["editor"], c["last_edited_at"] = body, iso(self.world["now"]), by, iso(self.world["now"])
        self.save()

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
