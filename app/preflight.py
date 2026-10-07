"""Clone preflight (v0.5.19): ask a remote what it has before a clone is queued, so the new-project wizard can say 'reachable, default
branch main, needs credentials' under the URL field. `git ls-remote --symref <url>` never prompts (GIT_TERMINAL_PROMPT=0, GIT_ASKPASS=true,
ssh in batch mode) and is cut off after TIMEOUT seconds. preflight_clone() validates with projects.check_url (the same rule a clone obeys: https, ssh and git@host:
only, never an IP literal, localhost or a private, link-local or tailnet name, unless CCBOARD_CLONE_ALLOWED_HOSTS lists the host); ls_remote() is the probe alone.
The probe only speaks https and ssh (GIT_ALLOW_PROTOCOL) and does not follow an https redirect, so a public host cannot bounce it to an internal one; git runs in its
own process group and the whole group is killed at the timeout, so no git-remote-https or ssh child outlives the call."""
from __future__ import annotations

import os
import re
import signal
import subprocess

from . import projects

TIMEOUT = 12                       # seconds: a remote that does not answer by then is reported as unreachable
MAX_HEADS = 50                     # branch names returned (the wizard shows a few)
ALLOW_PROTOCOL = "https:ssh"       # GIT_ALLOW_PROTOCOL for the probe and for the clone: no git://, no file, no ext
_AUTH_RE = re.compile(
    r"could not read (?:username|password)|authentication failed|terminal prompts disabled|permission denied \(publickey|"
    r"repository not found|invalid username or password|http basic: access denied|returned error: 40[13]|"
    r"host key verification failed|no supported authentication", re.I)
_SYMREF_RE = re.compile(r"^ref:\s+refs/heads/(\S+)\s+HEAD\s*$")
_HEAD_RE = re.compile(r"^[0-9a-f]{7,64}\s+refs/heads/(\S+)\s*$")


def _env() -> dict:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "SSH_ASKPASS": "true", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oConnectTimeout=8",
                "GIT_ALLOW_PROTOCOL": ALLOW_PROTOCOL, "LC_ALL": "C"})
    return env


def _run(argv: list[str], *, env: dict, timeout: float) -> subprocess.CompletedProcess:
    """Run argv with a hard ceiling: in its own process group, and at the timeout the whole group is killed (subprocess.run would kill git alone and could then
    wait on a pipe that a git-remote-https or ssh child still holds). Raises subprocess.TimeoutExpired."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        raise subprocess.TimeoutExpired(argv, timeout) from None
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait()
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


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
        cp = _run(["git", "-c", "http.followRedirects=false", "ls-remote", "--symref", "--", url], env=_env(), timeout=timeout)
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
