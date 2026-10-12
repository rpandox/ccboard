"""The request handler of tests/fake_github.py: an in-memory GitHub, stdlib only (the fake `gh` script imports it on every call, so it must start fast).

`handle(world, method, path, inm, body)` serves one request against the plain-data `world` dict and returns a `Resp`. Reads: issues, sub-issues, comments (paged, ETag and 304),
the rate limit. Writes (issue #148): create, edit and delete a comment (GitHub-like ids that grow, `created_at` and `updated_at` with one-second resolution, `updated_at` moving
only when the body changes, the author the world's login), assignees (additive, silently ignored for a repository in `world["no_push"]`), issue labels (a label that does not
exist on the repository is a 422), label creation. Faults: one-shot overrides (`apply` applies the write and then answers with the fault, which is a created comment whose
answer was lost; `hang` is a gh that never answers), between-call mutations that add or delete a comment (another node's comment interleaved), and a comment that stays
hidden from reads until a server time (`hidden_until`, eventual consistency).

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


def key(o, r, n):
    return "%s/%s#%s" % (o, r, n)


def visible(world: dict, k: str) -> list[dict]:
    """The comments of an issue that reads show now (those with a `hidden_until` in the future are not there yet)."""
    now = world["now"]
    return [c for c in world.get("comments", {}).get(k, []) if c.get("hidden_until") is None or now >= c["hidden_until"]]


def issue_obj(world: dict, k: str) -> dict:
    it = world["issues"][k]
    repo = k.split("#")[0]
    n = int(k.split("#")[1])
    o = {"number": n, "title": it.get("title", "Issue %d" % n), "state": it.get("state", "open"), "body": it.get("body", ""),
         "labels": [{"name": x} for x in it.get("labels", [])], "user": {"login": it.get("user", "alice"), "type": it.get("user_type", "User")},
         "assignees": [{"login": x, "type": "User"} for x in it.get("assignees", [])],
         "comments": len(visible(world, k)),
         "repository_url": "https://api.github.com/repos/" + repo, "html_url": "https://github.com/%s/issues/%d" % (repo, n),
         "created_at": it.get("created_at", "2026-10-01T09:00:00Z"), "updated_at": it.get("updated_at", "2026-10-01T09:00:00Z")}
    if it.get("pull_request"):
        o["pull_request"] = {"url": "https://api.github.com/repos/%s/pulls/%d" % (repo, n)}
    if it.get("summary") is not None:
        o["sub_issues_summary"] = it["summary"]
    o.update(it.get("raw", {}))
    return o


def comment_obj(c: dict) -> dict:
    out = dict(c)
    out["user"] = None if c["user"] is None else {"login": c["user"], "type": c.get("user_type", "User")}
    out["author_association"] = c.get("assoc", "COLLABORATOR")
    for x in ("user_type", "assoc", "hidden_until"):
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
            return Resp(200, {}, comment_obj(c))
        return Resp(405, {}, {"message": "method not allowed here: " + method})
    m = re.fullmatch(r"/repos/([^/]+)/([^/]+)/issues/([0-9]+)/(comments|assignees|labels)(?:/(.+))?", path)
    if m:
        o, r, n, what, rest = m.groups()
        k = key(o, r, n)
        if k not in world["issues"]:
            return Resp(404, {}, {"message": "Not Found"})
        it = world["issues"][k]
        if what == "comments" and method == "POST":
            if not isinstance(body, dict) or not isinstance(body.get("body"), str) or not body["body"].strip():
                return Resp(422, {}, {"message": "Validation Failed"})
            lst = world.setdefault("comments", {}).setdefault(k, [])
            cid = max([c["id"] for c in lst] + [world.get("next_cid", 100000)]) + 1
            world["next_cid"] = cid
            c = {"id": cid, "body": body["body"], "user": login, "created_at": iso(now), "updated_at": iso(now), "assoc": world.get("write_assoc", "COLLABORATOR"), "user_type": "User"}
            if world.get("hide_new"):
                c["hidden_until"] = now + world["hide_new"]
            lst.append(c)
            _touch(world, k)
            return Resp(201, {}, comment_obj(c))
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
    m = re.fullmatch(r"/repos/([^/]+)/([^/?]+)/issues/([0-9]+)(?:/(sub_issues|comments))?(?:\?(.*))?", path or "")
    rp = re.fullmatch(r"/repos/([^/]+)/([^/?]+)", path or "")
    if rp:
        slug = "%s/%s" % rp.groups()
        if slug in world.get("forbidden", []):
            return Resp(403, {}, {"message": "Resource not accessible"})
        info = (world.get("repos") or {}).get(slug)
        if info is None and not world.get("repos_default", True):
            return Resp(404, {}, {"message": "Not Found"})
        info = info or {}
        return Resp(200, {}, {"full_name": slug, "private": bool(info.get("private", False)), "permissions": {"admin": False, "push": bool(info.get("push", True)), "pull": True}})
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
        items = [comment_obj(c) for c in visible(world, k)]
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
        out.write(json.dumps({"login": world["login"]}) + "\n")
        return 0
    if args[:1] != ["api"]:
        return die(1, "unknown command")
    if not world.get("login"):
        return die(4, "To get started with GitHub CLI, please run:  gh auth login")
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
