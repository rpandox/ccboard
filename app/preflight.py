"""Clone preflight (v0.5.19): ask a remote what it has before a clone is queued, so the new-project wizard can say 'reachable, default
branch main, needs credentials' under the URL field. `git ls-remote --symref <url>` never prompts (GIT_TERMINAL_PROMPT=0, GIT_ASKPASS=true,
ssh in batch mode) and is cut off after TIMEOUT seconds. preflight_clone() validates with projects.check_url (the same rule a clone obeys);
ls_remote() is the probe alone (the tests point it at a local bare repo and an unreachable path)."""
from __future__ import annotations

import os
import re
import subprocess

from . import projects

TIMEOUT = 12                       # seconds: a remote that does not answer by then is reported as unreachable
MAX_HEADS = 50                     # branch names returned (the wizard shows a few)
_AUTH_RE = re.compile(
    r"could not read (?:username|password)|authentication failed|terminal prompts disabled|permission denied \(publickey|"
    r"repository not found|invalid username or password|http basic: access denied|returned error: 40[13]|"
    r"host key verification failed|no supported authentication", re.I)
_SYMREF_RE = re.compile(r"^ref:\s+refs/heads/(\S+)\s+HEAD\s*$")
_HEAD_RE = re.compile(r"^[0-9a-f]{7,64}\s+refs/heads/(\S+)\s*$")


def _env() -> dict:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "SSH_ASKPASS": "true", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oConnectTimeout=8",
                "LC_ALL": "C"})
    return env


def _short(text: str) -> str:
    """One line of git's stderr for the chip: first non-empty line, URLs with embedded credentials scrubbed, capped."""
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln:
            ln = re.sub(r"(https?://)[^/@\s]+@", r"\1", ln)
            return ln[:200]
    return ""


def ls_remote(url: str, timeout: float = TIMEOUT) -> dict:
    """{reachable, default_branch, needs_auth, heads, error} for one remote. Never raises for a remote that is merely down."""
    out = {"reachable": False, "default_branch": None, "needs_auth": False, "heads": [], "error": None}
    try:
        cp = subprocess.run(["git", "ls-remote", "--symref", "--", url], capture_output=True, text=True, timeout=timeout, env=_env(), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        out["error"] = f"no answer within {int(timeout)} seconds"
        return out
    except FileNotFoundError:
        out["error"] = "git is not installed on the box"
        return out
    except OSError as e:
        out["error"] = f"could not run git ({e.__class__.__name__})"
        return out
    if cp.returncode != 0:
        err = cp.stderr or ""
        out["needs_auth"] = bool(_AUTH_RE.search(err))
        out["error"] = _short(err) or f"git ls-remote exited {cp.returncode}"
        return out
    heads: list[str] = []
    default = None
    for ln in (cp.stdout or "").splitlines():
        m = _SYMREF_RE.match(ln)
        if m:
            default = m.group(1)
            continue
        h = _HEAD_RE.match(ln)
        if h and h.group(1) not in heads:
            heads.append(h.group(1))
    if default is None and heads:                 # an older server sends no symref: main, then master, else the first branch
        default = next((b for b in ("main", "master") if b in heads), heads[0])
    out.update({"reachable": True, "default_branch": default, "heads": heads[:MAX_HEADS]})
    return out


def preflight_clone(url: str) -> dict:
    """POST /api/preflight/clone {url}: {reachable, default_branch, needs_auth, heads, name, error}. BadRequest for a URL a clone would refuse
    (so the wizard's field says the same thing the clone would); `name` is the repo name a clone would derive, or None."""
    url = projects.check_url(url)
    res = ls_remote(url)
    res["name"] = projects.derive_repo_name(url)
    return res
