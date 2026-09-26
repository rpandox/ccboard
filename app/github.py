"""GitHub via the gh CLI on the box (already logged in by the user)."""
from __future__ import annotations

import json
import shutil
import subprocess

FIELDS = "name,url,sshUrl,isArchived,isFork,isPrivate,updatedAt,description"


class GhError(Exception):
    pass


def _gh(*args: str, timeout: int = 60) -> str:
    exe = shutil.which("gh")
    if not exe:
        raise GhError("gh is not installed on the box")
    try:
        cp = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GhError("gh timed out")
    if cp.returncode != 0:
        err = (cp.stderr or cp.stdout).strip()
        if "auth login" in err or "not logged" in err.lower():
            raise GhError("gh is not logged in on the box: run `gh auth login` there")
        raise GhError(err[-300:] or "gh failed")
    return cp.stdout


def git_protocol() -> str:
    try:
        return _gh("config", "get", "git_protocol", timeout=10).strip() or "https"
    except GhError:
        return "https"


def list_repos(owner: str | None = None, limit: int = 200, include_archived: bool = False) -> list[dict]:
    args = ["repo", "list"]
    if owner:
        args.append(owner)
    args += ["--json", FIELDS, "--limit", str(min(max(limit, 1), 1000))]
    if not include_archived:
        args.append("--no-archived")
    try:
        data = json.loads(_gh(*args) or "[]")
    except ValueError:
        raise GhError("unexpected gh output")
    proto = git_protocol()
    out = []
    for r in data:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        url = r.get("sshUrl") if proto == "ssh" and r.get("sshUrl") else (r.get("url") or "") + ".git"
        out.append({"name": r["name"], "url": url, "private": bool(r.get("isPrivate")), "fork": bool(r.get("isFork")),
                    "archived": bool(r.get("isArchived")), "updated": r.get("updatedAt"), "description": r.get("description") or ""})
    return out
