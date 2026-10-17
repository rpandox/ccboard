"""The request handler of tests/fake_github.py: an in-memory GitHub, stdlib only (the fake `gh` script imports it on every call, so it must start fast).

`handle(world, method, path, inm, body)` serves one request against the plain-data `world` dict and returns a `Resp`. Reads: issues, sub-issues, comments (paged, ETag and 304),
the rate limit. Writes (issue #148): create, edit and delete a comment (GitHub-like ids that grow, `created_at` and `updated_at` with one-second resolution, `updated_at` moving
only when the body changes, the author the world's login), assignees (additive, silently ignored for a repository in `world["no_push"]`), issue labels (a label that does not
exist on the repository is a 422), label creation. Faults: one-shot overrides (`apply` applies the write and then answers with the fault, which is a created comment whose
answer was lost; `hang` is a gh that never answers), between-call mutations that add or delete a comment (another node's comment interleaved), and a comment that stays
hidden from reads until a server time (`hidden_until`, eventual consistency).

Team reads (issue #197, Team P1): the open issues that have an assignee (`assignee=*`, user ids included, a pull request listed among the issues when it has assignees; issue #200 adds
`assignee=none`: the open issues with nobody assigned, bodies included, and the pull requests with nobody assigned), the open pull
requests (world["pulls"]: `repo#n` -> title, draft, user, head_ref, head_repo, updated_at, closes), the repository record (`full_name` for a rename, `fork` and `parent`, `default_branch`),
the access faults of an organisation (world["sso"]: 403 with X-GitHub-SSO and the SAML text; world["forbidden"]: 403; world["hidden"]: 404), a gh that prints only `gh: HTTP 304`
and exits 1 on a conditional hit (world["bare_304"]), and `gh api graphql` (world["tips"], world["linked"], world["gql_cost"], world["gql_rate"]): the one query of app/github.py answered from the
same world, with a GraphQL rate limit of its own. Users get a numeric id from world["ids"] or a stable one made from the login (`uid`).

No host name, e-mail address or real account appears here: acme/shop, node-a, example.com.
"""
from __future__ import annotations

import email.utils
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, NamedTuple
from urllib.parse import unquote


class Resp(NamedTuple):
    status: int
    headers: dict
    body: Any
    hang: bool = False


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def uid(world: dict, login) -> int | None:
    """The numeric id of a login in the fake world: world["ids"][login], else a stable number made from the name."""
    if not isinstance(login, str):
        return None
    return (world.get("ids") or {}).get(login) or 1000 + sum(ord(c) * (i + 1) for i, c in enumerate(login))


SSO_BODY = {"message": "Resource protected by organization SAML enforcement. You must grant your Personal Access token access to this organization."}


def access_fault(world: dict, slug: str):
    """A Resp when this login may not read the repository (SSO, forbidden, hidden), else None."""
    if slug in world.get("sso", []):
        return Resp(403, {"X-GitHub-SSO": "required; url=https://github.com/orgs/acme/sso?authorization_request=" + "A1B2C3"}, dict(SSO_BODY))
    if slug in world.get("forbidden", []):
        return Resp(403, {}, {"message": "Resource not accessible"})
    if slug in world.get("hidden", []):
        return Resp(404, {}, {"message": "Not Found"})
    return None


def key(o, r, n):
    return "%s/%s#%s" % (o, r, n)


def visible(world: dict, k: str) -> list[dict]:
    """The comments of an issue that reads show now (those with a `hidden_until` in the future are not there yet)."""
    now = world["now"]
    return [c for c in world.get("comments", {}).get(k, []) if c.get("hidden_until") is None or now >= c["hidden_until"]]


def issue_id(k: str) -> int:
    """The REST id of an issue in the fake world (stable, made from its key); its GraphQL node id is `I_kw<id>` (issue #157: the worker's editor check of an issue body)."""
    return 10 ** 7 + int(hashlib.sha1(k.encode()).hexdigest()[:7], 16)


def issue_obj(world: dict, k: str) -> dict:
    it = world["issues"][k]
    repo = k.split("#")[0]
    n = int(k.split("#")[1])
    login = it.get("user", "alice")
    o = {"id": issue_id(k), "node_id": "I_kw%d" % issue_id(k), "number": n, "title": it.get("title", "Issue %d" % n), "state": it.get("state", "open"), "body": it.get("body", ""),
         "labels": [{"name": x} for x in it.get("labels", [])], "user": {"login": login, "id": uid(world, login), "type": it.get("user_type", "User")},
         "assignees": [{"login": x, "type": "User"} for x in it.get("assignees", [])],
         "comments": len(visible(world, k)), "locked": bool(it.get("locked", False)),
         "repository_url": "https://api.github.com/repos/" + repo, "html_url": "https://github.com/%s/issues/%d" % (repo, n),
         "created_at": it.get("created_at", "2026-10-01T09:00:00Z"), "updated_at": it.get("updated_at", "2026-10-01T09:00:00Z")}
    if it.get("pull_request"):
        o["pull_request"] = {"url": "https://api.github.com/repos/%s/pulls/%d" % (repo, n)}
    if it.get("summary") is not None:
        o["sub_issues_summary"] = it["summary"]
    o.update(it.get("raw", {}))
    return o


def comment_obj(c: dict, world: dict | None = None) -> dict:
    out = dict(c)
    out["node_id"] = "IC_kw%d" % c["id"]                                         # GraphQL's id of the comment (issue #198: the editor check of a comment out of the window)
    out["user"] = None if c["user"] is None else {"login": c["user"], "id": uid(world or {}, c["user"]), "type": c.get("user_type", "User")}
    out["author_association"] = c.get("assoc", "COLLABORATOR")
    for x in ("user_type", "assoc", "hidden_until", "editor", "last_edited_at"):
        out.pop(x, None)
    return out


def paged(world: dict, items, query):
    q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
    per = min(int(q.get("per_page", "30")), world.get("page_size", 100))
    page = int(q.get("page", "1"))
    chunk = items[(page - 1) * per: page * per]
    more = page * per < len(items)
    return chunk, ({"Link": '<https://api.github.com/next>; rel="next"'} if more else {})


def _touch(world: dict, k: str) -> None:
    world["issues"][k]["updated_at"] = iso(world["now"])


def _find_comment(world: dict, o: str, r: str, cid: int):
    for k, lst in world.get("comments", {}).items():
        if k.startswith("%s/%s#" % (o, r)):
            for c in lst:
                if c["id"] == cid:
                    return k, lst, c
    return None, None, None


def _labels_of(world: dict, k: str) -> list[dict]:
    return [{"name": x} for x in world["issues"][k].get("labels", [])]


def _label_exists(world: dict, o: str, r: str, name: str) -> bool:
    if name in world.get("repo_labels", {}).get("%s/%s" % (o, r), []):
        return True
    return any(name in it.get("labels", []) for k, it in world["issues"].items() if k.startswith("%s/%s#" % (o, r)))


def write(world: dict, method: str, path: str, body) -> Resp:
    """One write. `body` is the parsed JSON the gh stdin carried (or None)."""
    login = world.get("login")
    now = world["now"]
    if not login:
        return Resp(401, {}, {"message": "Requires authentication"})
    no_push = "%s/%s" % tuple(path.split("/")[2:4]) in world.get("no_push", [])
    m = re.fullmatch(r"/repos/([^/]+)/([^/]+)/issues/comments/([0-9]+)", path)
    if m:
        o, r, cid = m.group(1), m.group(2), int(m.group(3))
        k, lst, c = _find_comment(world, o, r, cid)
        if c is None:
            return Resp(404, {}, {"message": "Not Found"})
        if c["user"] != login:
            return Resp(403, {}, {"message": "Resource not accessible by integration"})
        if method == "DELETE":
            lst.remove(c)
            _touch(world, k)
            return Resp(204, {}, None)
        if method == "PATCH":
            if not isinstance(body, dict) or not isinstance(body.get("body"), str):
                return Resp(422, {}, {"message": "Validation Failed"})
            if body["body"] != c["body"]:
                c["body"], c["updated_at"] = body["body"], iso(now)
                c["editor"], c["last_edited_at"] = login, iso(now)         # GraphQL `editor` (issue #198)
            return Resp(200, {}, comment_obj(c, world))
        return Resp(405, {}, {"message": "method not allowed here: " + method})
    m = re.fullmatch(r"/repos/([^/]+)/([^/]+)/issues/([0-9]+)/(comments|assignees|labels|sub_issues)(?:/(.+))?", path)
    if m:
        o, r, n, what, rest = m.groups()
        k = key(o, r, n)
        if k not in world["issues"]:
            return Resp(404, {}, {"message": "Not Found"})
        it = world["issues"][k]
        if what == "comments" and method == "POST":
            if not isinstance(body, dict) or not isinstance(body.get("body"), str) or not body["body"].strip():
                return Resp(422, {}, {"message": "Validation Failed"})
            if it.get("locked"):
                return Resp(403, {}, {"message": "Unable to create comment because issue is locked."})
            if "%s/%s" % (o, r) in world.get("interaction_limited", []):
                return Resp(422, {}, {"message": "An interaction limit is in effect for this repository."})
            lst = world.setdefault("comments", {}).setdefault(k, [])
            cid = max([c["id"] for c in lst] + [world.get("next_cid", 100000)]) + 1
            world["next_cid"] = cid
            c = {"id": cid, "body": body["body"], "user": login, "created_at": iso(now), "updated_at": iso(now), "assoc": world.get("write_assoc", "COLLABORATOR"), "user_type": "User"}
            if world.get("hide_new"):
                c["hidden_until"] = now + world["hide_new"]
            lst.append(c)
            _touch(world, k)
            return Resp(201, {}, comment_obj(c, world))
        if what == "sub_issues" and method == "POST":           # issue #158: attach a child to the parent by the child's REST id (a duplicate or the parent itself is a 422)
            if world.get("sub_issues_missing"):
                return Resp(404, {}, {"message": "Not Found"})
            sid = body.get("sub_issue_id") if isinstance(body, dict) else None
            child = next((c for c in world["issues"] if isinstance(sid, int) and not isinstance(sid, bool) and issue_id(c) == sid), None)
            if child is None:
                return Resp(404, {}, {"message": "Not Found"})
            if child == k or any(child in v for v in world.setdefault("subs", {}).values()):
                return Resp(422, {}, {"message": "Validation Failed"})
            world["subs"].setdefault(k, []).append(child)
            _touch(world, k)
            return Resp(201, {}, issue_obj(world, k))
        if what == "assignees" and method in ("POST", "DELETE"):
            names = body.get("assignees") if isinstance(body, dict) else None
            if not isinstance(names, list) or not all(isinstance(x, str) for x in names):
                return Resp(422, {}, {"message": "Validation Failed"})
            have = it.setdefault("assignees", [])
            if method == "POST" and not no_push:
                for x in names:
                    if x not in have:
                        have.append(x)
            elif method == "DELETE":
                it["assignees"] = [x for x in have if x not in names]
            _touch(world, k)
            return Resp(201 if method == "POST" else 200, {}, issue_obj(world, k))
        if what == "labels" and method == "POST":
            names = body.get("labels") if isinstance(body, dict) else None
            if not isinstance(names, list) or not all(isinstance(x, str) for x in names):
                return Resp(422, {}, {"message": "Validation Failed"})
            if no_push:
                return Resp(403, {}, {"message": "Must have push access to add labels"})
            if not all(_label_exists(world, o, r, x) for x in names):
                return Resp(422, {}, {"message": "Validation Failed", "errors": [{"resource": "Label", "code": "invalid"}]})
            for x in names:
                if x not in it.setdefault("labels", []):
                    it["labels"].append(x)
            _touch(world, k)
            return Resp(200, {}, _labels_of(world, k))
        if what == "labels" and method == "DELETE" and rest:
            name = unquote(rest)
            if no_push:
                return Resp(403, {}, {"message": "Must have push access to remove labels"})
            if name not in it.get("labels", []):
                return Resp(404, {}, {"message": "Label does not exist"})
            it["labels"].remove(name)
            _touch(world, k)
            return Resp(200, {}, _labels_of(world, k))
        return Resp(405, {}, {"message": "method not allowed here: " + method})
    m = re.fullmatch(r"/repos/([^/]+)/([^/]+)/issues/([0-9]+)", path)
    if m and method == "PATCH":                                 # issue #158: edit an issue (the epic's parent body); only the title and the body change
        k = key(*m.groups())
        if k not in world["issues"]:
            return Resp(404, {}, {"message": "Not Found"})
        if not isinstance(body, dict) or not set(body) <= {"title", "body"} or not all(isinstance(v, str) for v in body.values()):
            return Resp(422, {}, {"message": "Validation Failed"})
        world["issues"][k].update(body)
        _touch(world, k)
        return Resp(200, {}, issue_obj(world, k))
    m = re.fullmatch(r"/repos/([^/]+)/([^/]+)/issues", path)
    if m and method == "POST":                                  # a new issue (issue #198: the presence status issue); labels are dropped without push access, as GitHub does
        o, r = m.groups()
        info = (world.get("repos") or {}).get("%s/%s" % (o, r)) or {}
        if info.get("has_issues") is False:
            return Resp(410, {}, {"message": "Issues are disabled for this repo"})
        if world.get("issue_create_forbidden") or info.get("archived"):
            return Resp(403, {}, {"message": "Resource not accessible by integration"})
        if not isinstance(body, dict) or not isinstance(body.get("title"), str):
            return Resp(422, {}, {"message": "Validation Failed"})
        nums = [int(k.split("#")[1]) for k in world["issues"] if k.startswith("%s/%s#" % (o, r))] + [int(k.split("#")[1]) for k in (world.get("pulls") or {}) if k.startswith("%s/%s#" % (o, r))]
        n = max(nums + [world.get("next_issue", 900)]) + 1
        world["next_issue"] = n
        labels = [x for x in (body.get("labels") or []) if isinstance(x, str)] if not (no_push or world.get("label_add_forbidden")) else []
        k = key(o, r, n)
        world["issues"][k] = {"title": body["title"], "state": "open", "labels": labels, "body": body.get("body", ""), "user": login, "pull_request": False, "summary": None,
                              "raw": {}, "updated_at": iso(now), "created_at": iso(now)}
        return Resp(201, {}, issue_obj(world, k))
    m = re.fullmatch(r"/repos/([^/]+)/([^/]+)/labels", path)
    if m and method == "POST":
        o, r = m.groups()
        if no_push or world.get("label_create_forbidden"):
            return Resp(403, {}, {"message": "Must have push access to create labels"})
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name:
            return Resp(422, {}, {"message": "Validation Failed"})
        if _label_exists(world, o, r, name):
            return Resp(422, {}, {"message": "Validation Failed", "errors": [{"resource": "Label", "code": "already_exists"}]})
        world.setdefault("repo_labels", {}).setdefault("%s/%s" % (o, r), []).append(name)
        return Resp(201, {}, {"name": name})
    return Resp(404, {}, {"message": "Not Found"})


def read(world: dict, path: str) -> Resp:
    now = world["now"]
    rate = world.get("rate")
    m = re.fullmatch(r"/repos/([^/]+)/([^/?]+)/issues/([0-9]+)(?:/(sub_issues|comments|events))?(?:\?(.*))?", path or "")
    rp = re.fullmatch(r"/repos/([^/]+)/([^/?]+)", path or "")
    if rp:
        slug = "%s/%s" % rp.groups()
        fault = access_fault(world, slug)
        if fault is not None:
            return fault
        info = (world.get("repos") or {}).get(slug)
        if info is None and not world.get("repos_default", True):
            return Resp(404, {}, {"message": "Not Found"})
        info = info or {}
        body = {"full_name": info.get("full_name", slug), "private": bool(info.get("private", False)), "permissions": {"admin": False, "push": bool(info.get("push", True)), "pull": True}}
        if "fork" in info:
            body["fork"] = bool(info["fork"])
            if info.get("parent"):
                body["parent"] = {"full_name": info["parent"]}
        if "default_branch" in info:
            body["default_branch"] = info["default_branch"]
        body["has_issues"] = info.get("has_issues", True) is not False
        body["archived"] = bool(info.get("archived", False))
        return Resp(200, {}, body)
    br = re.fullmatch(r"/repos/([^/]+)/([^/?]+)/branches/(.+)", path or "")
    if br:                                                      # one branch (issue #198): 200 when it is among the repository's branch tips, else 404
        slug = "%s/%s" % br.groups()[:2]
        fault = access_fault(world, slug)
        if fault is not None:
            return fault
        if any(t["name"] == br.group(3) for t in (world.get("tips") or {}).get(slug, [])):
            return Resp(200, {}, {"name": br.group(3), "commit": {"sha": "0" * 40}})
        return Resp(404, {}, {"message": "Branch not found"})
    pf = re.fullmatch(r"/repos/([^/]+)/([^/?]+)/pulls/([0-9]+)/files\?(.*)", path or "")
    if pf:                                                      # the files of one pull request (issue #199): world["pull_files"]["repo#n"] -> names or {filename, previous_filename}
        o, r, n, query = pf.groups()
        slug = "%s/%s" % (o, r)
        fault = access_fault(world, slug)
        if fault is not None:
            return fault
        files = (world.get("pull_files") or {}).get("%s#%s" % (slug, n))
        if files is None:
            return Resp(404, {}, {"message": "Not Found"})
        items = [{"filename": f, "status": "modified"} if isinstance(f, str) else dict(f) for f in files]
        chunk, extra = paged(world, items, query)
        return Resp(200, extra, chunk)
    tl = re.fullmatch(r"/repos/([^/]+)/([^/?]+)/(issues|pulls)\?(.*)", path or "")
    if tl and (tl.group(3) == "pulls" or "assignee=*" in tl.group(4) or "assignee=none" in tl.group(4)):
        o, r, what, query = tl.groups()
        slug = "%s/%s" % (o, r)
        fault = access_fault(world, slug)
        if fault is not None:
            return fault
        if what == "issues" and "assignee=none" in query:           # issue #200: the open issues nobody is assigned to, with their bodies (the reader drops them), a pull request among them
            ks = [k for k, it in world["issues"].items() if k.startswith(slug + "#") and it.get("state", "open") == "open" and not it.get("assignees")]
            ks.sort(key=lambda k: int(k.split("#")[1]))
            items = [issue_obj(world, k) for k in ks]
            for pk, pr in sorted((world.get("pulls") or {}).items()):
                if pk.startswith(slug + "#") and not pr.get("assignees"):
                    items.append({"number": int(pk.split("#")[1]), "title": pr.get("title", ""), "state": "open", "body": "pull request body", "labels": [], "pull_request": {"url": "x"},
                                  "assignees": [], "updated_at": pr.get("updated_at", "2026-10-01T09:00:00Z")})
            chunk, extra = paged(world, items, query)
            return Resp(200, extra, chunk)
        if what == "issues":
            ks = [k for k, it in world["issues"].items() if k.startswith(slug + "#") and it.get("state", "open") == "open" and it.get("assignees")]
            ks.sort(key=lambda k: int(k.split("#")[1]))
            items = []
            for k in ks:
                obj = issue_obj(world, k)
                obj["assignees"] = [{"login": a["login"], "type": "User", "id": uid(world, a["login"])} for a in obj["assignees"]]
                items.append(obj)
            for pk, pr in sorted((world.get("pulls") or {}).items()):
                if pk.startswith(slug + "#") and pr.get("assignees"):
                    items.append({"number": int(pk.split("#")[1]), "title": pr.get("title", ""), "state": "open", "labels": [], "pull_request": {"url": "x"},
                                  "assignees": [{"login": a, "type": "User", "id": uid(world, a)} for a in pr["assignees"]], "updated_at": pr.get("updated_at", "2026-10-01T09:00:00Z")})
            chunk, extra = paged(world, items, query)
            return Resp(200, extra, chunk)
        items = []
        for pk, pr in sorted(((k, v) for k, v in (world.get("pulls") or {}).items() if k.startswith(slug + "#")), key=lambda kv: int(kv[0].split("#")[1])):
            head_repo = pr.get("head_repo", slug)
            items.append({"number": int(pk.split("#")[1]), "title": pr.get("title", "Pull request"), "state": "open", "draft": bool(pr.get("draft", False)),
                          "user": {"login": pr.get("user", "alice"), "id": uid(world, pr.get("user", "alice")), "type": "User"},
                          "author_association": pr.get("assoc", "COLLABORATOR"),
                          "head": {"ref": pr.get("head_ref", "feature"), "repo": None if head_repo is None else {"full_name": head_repo, "fork": head_repo.lower() != slug.lower()}},
                          "base": {"repo": {"full_name": slug}}, "html_url": "https://github.com/%s/pull/%s" % (slug, pk.split("#")[1]),
                          "updated_at": pr.get("updated_at", "2026-10-01T09:00:00Z")})
        chunk, extra = paged(world, items, query)
        return Resp(200, extra, chunk)
    if path == "/rate_limit":
        r = rate or {"limit": 5000, "remaining": 5000, "reset": int(now) + 3600}
        return Resp(200, {}, {"resources": {"core": {"limit": r["limit"], "remaining": r["remaining"], "reset": r["reset"]}}})
    if m:
        o, r, n, sub, query = m.groups()
        if "%s/%s" % (o, r) in world.get("forbidden", []):
            return Resp(403, {}, {"message": "Resource not accessible"})
        k = key(o, r, n)
        if k not in world["issues"]:
            return Resp(404, {}, {"message": "Not Found"})
        if sub is None:
            return Resp(200, {}, issue_obj(world, k))
        if sub == "sub_issues":
            if world.get("sub_issues_missing"):
                return Resp(404, {}, {"message": "Not Found"})
            items = [issue_obj(world, c) for c in world.get("subs", {}).get(k, [])]
            chunk, extra = paged(world, items, query or "")
            return Resp(200, extra, chunk)
        if sub == "events":                                     # issue #157: the issue's events, oldest first (world["events"]["repo#n"]: event, label, actor, actor_type, created_at)
            items = [{"id": e["id"], "event": e["event"], "created_at": e["created_at"],
                      "actor": None if e.get("actor") is None else {"login": e["actor"], "id": e.get("actor_id") or uid(world, e["actor"]), "type": e.get("actor_type", "User")},
                      **({"label": {"name": e["label"]}} if e.get("label") is not None else {}), **({"rename": e["rename"]} if e.get("rename") else {})}
                     for e in (world.get("events") or {}).get(k, [])]
            chunk, extra = paged(world, items, query or "")
            return Resp(200, extra, chunk)
        items = [comment_obj(c, world) for c in visible(world, k)]
        chunk, extra = paged(world, items, query or "")
        return Resp(200, extra, chunk)
    m = re.fullmatch(r"/repos/([^/]+)/([^/?]+)/issues\?(.*)", path or "")
    if m:
        o, r, query = m.groups()
        if "%s/%s" % (o, r) in world.get("forbidden", []):
            return Resp(403, {}, {"message": "Resource not accessible"})
        q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        label = q.get("labels", "").replace("%3A", ":")
        state = q.get("state", "open")
        ks = [k for k, it in world["issues"].items() if k.startswith("%s/%s#" % (o, r)) and label in it.get("labels", []) and (state == "all" or it.get("state", "open") == state)]
        ks.sort(key=lambda k: int(k.split("#")[1]), reverse=True)
        chunk, extra = paged(world, [issue_obj(world, k) for k in ks], query)
        return Resp(200, extra, chunk)
    return Resp(404, {}, {"message": "Not Found"})


def handle_graphql(world: dict, owner: str, name: str) -> tuple[Resp, bool]:
    """The one team query (app/github.py GQL_TEAM) answered from the world: the linked branches of the assigned issues, the closing issues of the open pull requests and the branch
    tips. The GraphQL rate limit is its own (world["gql_rate"] {limit, remaining}); every call costs world["gql_cost"] (default 1) points. Always status 200: GraphQL puts errors in the body."""
    slug = "%s/%s" % (owner, name)
    rate = world.setdefault("gql_rate", {"limit": 5000, "remaining": 5000, "reset": int(world["now"]) + 3600})
    cost = int(world.get("gql_cost", 1))
    headers = {"Date": email.utils.formatdate(world["now"], usegmt=True), "Content-Type": "application/json; charset=utf-8", "x-ratelimit-resource": "graphql",
               "x-ratelimit-limit": str(rate["limit"]), "x-ratelimit-remaining": str(rate["remaining"]), "x-ratelimit-reset": str(rate["reset"])}
    if not world.get("login"):
        return Resp(401, headers, {"message": "Requires authentication"}), False
    if rate["remaining"] <= 0:
        return Resp(200, headers, {"data": None, "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded for user ID 1."}]}), False
    fault = access_fault(world, slug)
    if fault is not None:
        if fault.status == 404:
            return Resp(200, headers, {"data": {"repository": None}, "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository with the name '%s'." % slug}]}), False
        msg = SSO_BODY["message"] if slug in world.get("sso", []) else "Resource not accessible by personal access token"
        return Resp(200, {**headers, **({"X-GitHub-SSO": fault.headers["X-GitHub-SSO"]} if "X-GitHub-SSO" in fault.headers else {})},
                    {"data": {"repository": None}, "errors": [{"type": "FORBIDDEN", "message": msg}]}), False
    rate["remaining"] = max(0, rate["remaining"] - cost)
    headers["x-ratelimit-remaining"] = str(rate["remaining"])

    def user(login):
        return None if login is None else {"login": login, "databaseId": uid(world, login)}

    def tip(t):
        return {"name": t["name"], "target": {"committedDate": t.get("at", "2026-10-10T09:00:00Z"), "author": {"user": user(t.get("author"))}}}
    issues = []
    for k in sorted((k for k, it in world["issues"].items() if k.startswith(slug + "#") and it.get("state", "open") == "open" and it.get("assignees")), key=lambda k: int(k.split("#")[1])):
        issues.append({"number": int(k.split("#")[1]), "linkedBranches": {"nodes": [{"ref": tip(t)} for t in (world.get("linked") or {}).get(k, [])]}})
    prs = []
    for pk, pr in sorted(((k, v) for k, v in (world.get("pulls") or {}).items() if k.startswith(slug + "#")), key=lambda kv: int(kv[0].split("#")[1])):
        prs.append({"number": int(pk.split("#")[1]), "isDraft": bool(pr.get("draft")), "isCrossRepository": pr.get("head_repo", slug) != slug, "headRefName": pr.get("head_ref", "feature"),
                    "author": {"login": pr.get("user", "alice"), "databaseId": uid(world, pr.get("user", "alice"))},
                    "closingIssuesReferences": {"nodes": [{"number": c} for c in pr.get("closes", [])]}})
    refs = [tip(t) for t in (world.get("tips") or {}).get(slug, [])][:100]
    data = {"rateLimit": {"cost": cost, "remaining": rate["remaining"], "resetAt": "2027-01-15T10:00:00Z"},
            "repository": {"nameWithOwner": slug, "isPrivate": False, "isFork": False, "parent": None, "issues": {"nodes": issues}, "pullRequests": {"nodes": prs},
                           "refs": {"pageInfo": {"hasNextPage": bool(world.get("tips_more"))}, "nodes": refs}}}
    return Resp(200, headers, {"data": data}), True


def handle_graphql_editors(world: dict, owner: str, name: str, number) -> tuple[Resp, bool]:
    """The editor query of app/github.py (GQL_EDITORS, issue #198): the last 50 comments of one issue with the author's and the last editor's ids. A comment's editor is set by an
    edit (gh_server.write) or by a test (`editor` on the stored comment). world["editors_fail"] answers with a GraphQL error."""
    slug = "%s/%s" % (owner, name)
    rate = world.setdefault("gql_rate", {"limit": 5000, "remaining": 5000, "reset": int(world["now"]) + 3600})
    headers = {"Date": email.utils.formatdate(world["now"], usegmt=True), "Content-Type": "application/json; charset=utf-8", "x-ratelimit-resource": "graphql",
               "x-ratelimit-limit": str(rate["limit"]), "x-ratelimit-remaining": str(rate["remaining"])}
    world["editor_calls"] = world.get("editor_calls", 0) + 1
    if world.get("editors_fail"):
        return Resp(200, headers, {"data": None, "errors": [{"type": "SOMETHING", "message": "failed"}]}), True
    k = key(owner, name, number)
    if k not in world["issues"]:
        return Resp(200, headers, {"data": {"repository": {"issue": None}}}), True

    def user(login):
        return None if login is None else {"login": login, "databaseId": uid(world, login)}
    nodes = [{"databaseId": c["id"], "lastEditedAt": c.get("last_edited_at"), "author": user(c.get("user")), "editor": user(c.get("editor"))}
             for c in visible(world, k)][-50:]
    return Resp(200, headers, {"data": {"rateLimit": {"cost": 1, "remaining": rate["remaining"], "resetAt": "2027-01-15T10:00:00Z"}, "repository": {"issue": {"comments": {"nodes": nodes}}}}}), True


def handle_graphql_editor(world: dict, node_id: str) -> tuple[Resp, bool]:
    """The one-comment editor query of app/github.py (GQL_EDITOR, issue #198): the comment whose node id is `node_id`, wherever it sits in its issue. world["editors_fail"] answers
    with a GraphQL error, as for the window query."""
    rate = world.setdefault("gql_rate", {"limit": 5000, "remaining": 5000, "reset": int(world["now"]) + 3600})
    headers = {"Date": email.utils.formatdate(world["now"], usegmt=True), "Content-Type": "application/json; charset=utf-8", "x-ratelimit-resource": "graphql",
               "x-ratelimit-limit": str(rate["limit"]), "x-ratelimit-remaining": str(rate["remaining"])}
    world["editor_node_calls"] = world.get("editor_node_calls", 0) + 1
    if world.get("editors_fail"):
        return Resp(200, headers, {"data": None, "errors": [{"type": "SOMETHING", "message": "failed"}]}), True

    def user(login):
        return None if login is None else {"login": login, "databaseId": uid(world, login)}
    node = None
    for k, it in world.get("issues", {}).items():                 # issue #157: an issue's own node id (its body's last editor: `editor` on the stored issue)
        if "I_kw%d" % issue_id(k) == node_id:
            node = {"databaseId": issue_id(k), "lastEditedAt": it.get("last_edited_at"), "author": user(it.get("user", "alice")), "editor": user(it.get("editor"))}
    for lst in world.get("comments", {}).values():
        for c in lst:
            if "IC_kw%d" % c["id"] == node_id:
                node = {"databaseId": c["id"], "lastEditedAt": c.get("last_edited_at"), "author": user(c.get("user")), "editor": user(c.get("editor"))}
    return Resp(200, headers, {"data": {"rateLimit": {"cost": 1, "remaining": rate["remaining"], "resetAt": "2027-01-15T10:00:00Z"}, "node": node}}), True


def handle_graphql_edit_history(world: dict, node_id: str, cursor=None) -> tuple[Resp, bool]:
    world["history_calls"] = world.get("history_calls", 0) + 1
    rate = world.setdefault("gql_rate", {"limit": 5000, "remaining": 5000, "reset": int(world["now"]) + 3600})
    cost = world.get("gql_cost", 1)
    data = {"rateLimit": {"cost": cost, "remaining": rate["remaining"], "resetAt": iso(rate["reset"])}, "node": None}
    def user(login, kind="User", identity=None):
        return None if login is None else {"__typename": kind, "login": login, **({"databaseId": identity or uid(world, login)} if kind == "User" else {})}
    for k, it in world.get("issues", {}).items():
        if "I_kw%d" % issue_id(k) != node_id:
            continue
        edits = list(it.get("history", []))
        if world.get("history_pages"):
            edits = [{"editor": "alice", "at": iso(world["now"])}] * 301
        last = it.get("last_edited_at") or (edits[-1]["at"] if edits else None)
        if world.get("history_hidden"):
            last, edits = iso(world["now"]), []
        offset = int(cursor or 0)
        chunk = edits[offset:offset + 100]
        renames = [e for e in world.get("events", {}).get(k, []) if e["event"] == "renamed"]
        data["node"] = {"__typename": "PullRequest" if it.get("pull_request") else "Issue", "id": node_id, "number": int(k.split("#")[1]), "databaseId": issue_id(k),
                        "repository": {"nameWithOwner": k.split("#")[0]}, "updatedAt": issue_obj(world, k)["updated_at"], "lastEditedAt": last,
                        "author": user(it.get("user", "alice")), "userContentEdits": {"nodes": [
                            {"editedAt": e["at"], "deletedAt": e.get("deleted"), "editor": user(e.get("editor"), e.get("type", "User"), e.get("id"))} for e in chunk],
                            "pageInfo": {"hasNextPage": offset + 100 < len(edits), "endCursor": str(offset + 100) if offset + 100 < len(edits) else None}},
                        "timelineItems": {"nodes": [{"createdAt": e["created_at"], "actor": user(e.get("actor"), e.get("actor_type", "User"), e.get("actor_id"))} for e in renames[:50]],
                                          "pageInfo": {"hasNextPage": len(renames) > 50}}}
    body = {"data": data}
    if world.get("history_fail"):
        body["errors"] = [{"type": "FORBIDDEN", "message": "history unavailable"}]
    return Resp(world.get("history_status", 200), {}, body), True


def handle(world: dict, method: str, path: str, inm: str | None = None, body=None) -> tuple[Resp, bool]:
    """Serve one request: (the response, whether the world changed in a way the caller must save). Status, headers (Date, ETag, rate limit, scopes) and body as GitHub would
    send them; a 2xx read with a matching If-None-Match is a 304."""
    dirty = False
    now = world["now"]
    headers = {"Date": email.utils.formatdate(now, usegmt=True), "Content-Type": "application/json; charset=utf-8"}
    rate = world.get("rate")
    if rate:
        headers.update({"x-ratelimit-limit": str(rate["limit"]), "x-ratelimit-remaining": str(rate["remaining"]), "x-ratelimit-reset": str(rate["reset"])})
    if world.get("scopes") is not None:
        headers["x-oauth-scopes"] = world["scopes"]
    fault = None
    for ov in world.get("overrides", []):
        if ov["match"] in path and ov.get("times", 1) != 0 and ov.get("method") in (None, method):
            if ov.get("times", 1) > 0:
                ov["times"] -= 1
                dirty = True
            fault = ov
            break
    if fault is not None and not fault.get("apply"):          # an override answers at once, before any between-call change (as the first fake did)
        return Resp(fault["status"], {**headers, **(fault.get("headers") or {})}, fault.get("body", {"message": "override"}), bool(fault.get("hang"))), dirty
    for hk in world.get("mutations", []):                    # the world changes between two calls: just before a call whose path contains `match` (after `skip` such calls)
        hit = (path == hk["match"][1:]) if hk["match"].startswith("=") else (hk["match"] in (path or ""))                # "=<path>": that path exactly
        if hit and hk.get("times", 1) != 0 and hk.get("method") in (None, method):
            dirty = True
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
    if method == "GET":
        resp = read(world, path)
    else:
        resp = write(world, method, path, body)
        dirty = True
    if fault is not None:                                    # apply: the write happened, the answer is the fault (a created comment whose answer was lost)
        return Resp(fault["status"], {**headers, **(fault.get("headers") or {})}, fault.get("body", {"message": "override"}), bool(fault.get("hang"))), dirty
    status, h = resp.status, {**headers, **resp.headers}
    text = json.dumps(resp.body) if resp.body is not None else ""
    if status < 300 and method == "GET":
        tag = 'W/"' + hashlib.sha1((text + world.get("salt", "")).encode()).hexdigest()[:16] + '"'
        if status == 200 and inm == tag:
            status, text = 304, ""
        h["ETag"] = tag
    return Resp(status, h, None if status == 304 else resp.body), dirty


REASONS = {200: "OK", 201: "Created", 204: "No Content", 304: "Not Modified", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
           422: "Unprocessable Entity", 429: "Too Many Requests", 500: "Internal Server Error", 502: "Bad Gateway"}


def main(args: list[str], env: dict, stdin, world_path: str, log_path: str, out, err) -> int:
    """The fake `gh`: argv in, `gh api --include` output out. Exit 0 for a 2xx, 1 for any other status (as the real gh does), 4 with no login."""
    token = env.get("GH_TOKEN", "")
    stdin_text = stdin.read() if "--input" in args else ""          # stdin is read only when the command says the body is there
    with open(log_path, "a") as fh:
        fh.write(json.dumps({"argv": args, "stdin": stdin_text}) + "\n")
    world = json.load(open(world_path))

    def die(rc, text):
        err.write(text + " " + token + "\n")            # a leaky gh: the token rides on stderr
        return rc
    if args[:1] == ["--version"]:
        out.write("gh version 2.99.0 (2026-09-01)\nhttps://github.com/cli/cli/releases/tag/v2.99.0\n")
        return 0
    if args[:2] == ["auth", "status"]:
        return 0 if world.get("login") else 1
    if args[:2] == ["api", "user"]:
        if not world.get("login"):
            return die(4, "To get started with GitHub CLI, please run:  gh auth login")
        out.write(json.dumps({"login": world["login"], "id": uid(world, world["login"])}) + "\n")
        return 0
    if args[:1] != ["api"]:
        return die(1, "unknown command")
    if not world.get("login"):
        return die(4, "To get started with GitHub CLI, please run:  gh auth login")
    if args[:2] == ["api", "graphql"]:
        fields, include = {}, False
        for j, a in enumerate(args[2:], 2):
            if a == "--include":
                include = True
            elif a in ("-f", "-F") and j + 1 < len(args):
                k, _, v = args[j + 1].partition("=")
                fields[k] = v
        if "userContentEdits" in fields.get("query", ""):
            resp, dirty = handle_graphql_edit_history(world, fields.get("id", ""), fields.get("cursor"))
        elif "node(id" in fields.get("query", ""):
            resp, dirty = handle_graphql_editor(world, fields.get("id", ""))
        elif "editor{" in fields.get("query", ""):
            resp, dirty = handle_graphql_editors(world, fields.get("owner", ""), fields.get("name", ""), fields.get("number", ""))
        else:
            resp, dirty = handle_graphql(world, fields.get("owner", ""), fields.get("name", ""))
        if dirty:
            json.dump(world, open(world_path, "w"))
        text = json.dumps(resp.body) if resp.body is not None else ""
        head = "HTTP/2.0 %d %s\r\n" % (resp.status, REASONS.get(resp.status, "X")) + "".join("%s: %s\r\n" % kv for kv in resp.headers.items()) + "\r\n"
        out.write(head + text if include else text)
        return 0 if resp.status < 300 else 1
    method, inm, path, include, has_input = "GET", None, None, False, False
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
        elif a == "--input":
            i += 1
            has_input = args[i] == "-"
        elif a.startswith("-"):
            method = "BAD-FLAG " + a
        else:
            path = a
        i += 1
    body = None
    if has_input and stdin_text.strip():
        try:
            body = json.loads(stdin_text)
        except ValueError:
            body = "unparseable"
    if method not in ("GET", "POST", "PATCH", "DELETE"):
        resp, dirty = Resp(405, {}, {"message": "method not served here: " + method}), False
    else:
        resp, dirty = handle(world, method, path, inm, body)
    if dirty:
        json.dump(world, open(world_path, "w"))
    if resp.status == 304 and world.get("bare_304"):          # gh 2.87: the 304 is on stderr only, with exit 1, and nothing on stdout
        err.write("gh: HTTP 304\n")
        return 1
    if resp.hang:
        import time
        time.sleep(30)
    text = json.dumps(resp.body) if resp.body is not None else ""
    reason = REASONS.get(resp.status, "X")
    head = "HTTP/2.0 %d %s\r\n" % (resp.status, reason) + "".join("%s: %s\r\n" % kv for kv in resp.headers.items()) + "\r\n"
    out.write(head + text if include else text)
    if resp.status >= 300:
        err.write("gh: %s (HTTP %d) %s\n" % (reason, resp.status, token))
        return 1
    return 0
