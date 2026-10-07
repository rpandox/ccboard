"""Clone preflight (v0.5.19): ask a remote what it has before a clone is queued, so the new-project wizard can say 'reachable, default
branch main, needs credentials' under the URL field. `git ls-remote --symref <url>` never prompts (GIT_TERMINAL_PROMPT=0, GIT_ASKPASS=true,
ssh in batch mode) and is cut off after TIMEOUT seconds. preflight_clone() validates with projects.check_url (the same rule a clone obeys: https, ssh and git@host:
only, never an IP literal, localhost or a private, link-local or tailnet name, unless CCBOARD_CLONE_ALLOWED_HOSTS lists the host); ls_remote() is the probe alone.
The probe only speaks https and ssh (GIT_ALLOW_PROTOCOL) and does not follow an https redirect, so a public host cannot bounce it to an internal one; git runs in its
own process group and the whole group is killed at the timeout, so no git-remote-https or ssh child outlives the call.
check_url also resolves the host and refuses an internal answer (projects.resolve_public); for https the probe is then pinned to the addresses that were
checked (`-c http.curloptResolve=<host>:<port>:<addresses>`), so git cannot look the name up again and be sent elsewhere (DNS rebinding), on a git that has
the option (PIN_MIN_GIT); an older git gets no pin and the probe relies on the check alone (the doctor's git check says which case applies)."""
from __future__ import annotations

import functools
import os
import re
import signal
import subprocess
import time

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
# http.curloptResolve came with git 2.37.0. Support is read from `git --version`, not from `git help config | grep curloptResolve`: the
# container's git (2.43) has the option but no man pages, so the help text would say no. A git that lacks it gets no pin.
PIN_MIN_GIT = (2, 37, 0)
_GIT_VER_RE = re.compile(r"git version (\d+)\.(\d+)(?:\.(\d+))?")


def parse_git_version(text: str) -> tuple[int, int, int] | None:
    m = _GIT_VER_RE.search(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else None


@functools.lru_cache(maxsize=1)
def git_version() -> tuple[int, int, int] | None:
    """The box's git version, read once per process; None when git is missing or says something else."""
    try:
        cp = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_git_version(cp.stdout) if cp.returncode == 0 else None


def pin_supported(version: tuple[int, int, int] | None = None) -> bool:
    v = version if version is not None else git_version()
    return v is not None and tuple(v) >= PIN_MIN_GIT


def pin_note(version: tuple[int, int, int] | None) -> str:
    """The doctor's sentence about the clone probe's address pin for this git version."""
    if pin_supported(version):
        return "the clone probe pins the checked address (http.curloptResolve)"
    return ("no http.curloptResolve (it needs git 2.37), so the clone probe is not pinned to the checked address and relies on the check alone")


def pin_option(pin: tuple[str, int, tuple[str, ...]]) -> str:
    """`http.curloptResolve=<host>:<port>:<addr>[,<addr>...]` (curl's CURLOPT_RESOLVE syntax; an IPv6 address goes in brackets). Every checked
    address is listed, so one that does not answer from the box does not fail the probe."""
    host, port, addrs = pin
    return "http.curloptResolve=" + f"{host}:{port}:" + ",".join(f"[{a}]" if ":" in a else a for a in addrs)


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


def ls_remote(url: str, timeout: float = TIMEOUT, pin: tuple[str, int, tuple[str, ...]] | None = None) -> dict:
    """{reachable, default_branch, needs_auth, heads, error} for one remote. Never raises for a remote that is merely down. `pin` (host, port,
    addresses) adds `-c http.curloptResolve=...` so git connects to those addresses only."""
    out = {"reachable": False, "default_branch": None, "needs_auth": False, "heads": [], "error": None}
    argv = ["git", "-c", "http.followRedirects=false"] + (["-c", pin_option(pin)] if pin else []) + ["ls-remote", "--symref", "--", url]
    try:
        cp = _run(argv, env=_env(), timeout=timeout)
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
    t0 = time.monotonic()
    url = projects.check_url(url)                                   # the rule and the DNS check (an answer is cached for the clone that follows)
    pin = projects.clone_pin(url) if pin_supported() else None      # https only, from that cached answer
    left = max(1.0, TIMEOUT - (time.monotonic() - t0))              # the lookup's time comes off git's share: the probe stays in its 12 s
    res = ls_remote(url, timeout=left, pin=pin) if pin else ls_remote(url, timeout=left)
    res["name"] = projects.derive_repo_name(url)
    return res
