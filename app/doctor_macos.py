"""The doctor's macOS checks (issue #117), registered by app/doctor.py on a Mac only (register_all).

Every check skips with a plain reason off macOS, so a caller that runs one by name on another system gets "skip", never a wrong answer.
Every probe goes through the doctor's own `_run` (a timeout, ToolMissing and ToolTimeout, patched by tests the same way) and a check never
raises: a tool that is missing, slow or odd gives a skip or a warning that says so. Fix texts name this system's commands through
platform.hint. Nothing here changes the Mac: the probes only read (launchctl print, fdesetup status, defaults read, pmset -g assertions).
The names are looked up on d.* at call time, because app/doctor.py imports this module while it is still being defined (and this module may
be imported first, which imports the doctor from here: the registration at the bottom works in either order).
"""
from __future__ import annotations

import functools
import os
import re
import shlex
import shutil
from pathlib import Path

from . import doctor as d
from . import platform as plat
from .config import settings

JOBS = ("board", "tmux", "ttyd")                  # the jobs the installer always creates (awake, code-server and mem are optional)
LOG_WARN_BYTES = 50 * 1024 * 1024                 # launchd never rotates a log: over this the doctor warns
PATH_TOOLS = (("claude", True), ("codex", False), ("tmux", True), ("git", True), ("gh", False))    # (name, must exist)
BARE_NAME_TOOLS = ("tmux", "git", "gh")           # the board starts these by bare name, so they must be on ITS PATH; claude and codex are started by full path
SURVIVAL_SENTENCE = "after a reboot nothing starts until you log in"


def _safe(fn):
    """A check never raises and never hangs: a tool that is missing or slow is a skip or a warning, anything unforeseen a plain warning."""
    @functools.wraps(fn)
    def run(db):
        try:
            return fn(db)
        except d.ToolMissing as e:
            return d._skip(f"{e.name} is not available on this system")
        except d.ToolTimeout as e:
            return d._warn(f"{e.name or 'a macOS tool'} did not answer in time")
        except Exception as e:                        # the last net: a doctor row must always come back
            return d._warn(f"check error: {type(e).__name__}")
    return run


def _not_mac():
    return None if plat.IS_MACOS else d._skip("macOS only: this board is not running on a Mac")


def _not_launchd():
    return None if settings.runtime == "launchd" else d._skip(f"the board is not run by launchd here (runtime {settings.runtime})")


# ------------------------------------------------------------------ launchd jobs

_STATE_RE = re.compile(r"^\s*state = (.+?)\s*$", re.M)


def _job_state(job: str) -> str:
    """'running', 'missing' (launchctl does not know the job), or the word launchctl gives for a loaded job that is not running ('waiting', 'not running'...)."""
    target = plat.launchd_target(job)
    p = d._run(["launchctl", "print", target])
    if p.rc != 0:
        return "missing"
    m = _STATE_RE.search(p.out)
    return m.group(1).lower() if m else "loaded"


@_safe
def _c_jobs(db):
    skip = _not_mac() or _not_launchd()
    if skip:
        return skip
    if plat.current_uid() is None:
        return d._skip("this system has no user id to name the launchd domain with")
    states = {job: _job_state(job) for job in JOBS}
    detail = ", ".join(f"{job} {state if state != 'missing' else 'not loaded'}" for job, state in states.items())
    missing = [j for j, s in states.items() if s == "missing"]
    if missing:
        job = missing[0]
        target = plat.launchd_target(job)
        return d._fail(f"{plat.launchd_label(job)} is not loaded ({detail})",
                       d.fix(f"Load the job again; the macOS installer writes its plist to ~/Library/LaunchAgents ({plat.hint('logs', 'ccboard-' + job)} shows its log)",
                             f"launchctl bootstrap {target.rsplit('/', 1)[0]} ~/Library/LaunchAgents/{plat.launchd_label(job)}.plist"))
    idle = [j for j, s in states.items() if s != "running"]
    if idle:
        return d._warn(f"{plat.launchd_label(idle[0])} is loaded but not running ({detail})",
                       d.fix(f"Start it, then read its log ({plat.hint('logs', 'ccboard-' + idle[0])})", plat.hint("start", f"ccboard-{idle[0]}")))
    return d._pass(f"{detail} in the {plat.launchd_domain()} domain")


# ------------------------------------------------------------------ what survives a logout and a reboot

def _filevault() -> str:
    """'on', 'off' or 'unknown' from `fdesetup status` ("FileVault is On." / "FileVault is Off.")."""
    p = d._run(["fdesetup", "status"])
    text = (p.out or p.err).lower()
    if "filevault is on" in text:
        return "on"
    if "filevault is off" in text:
        return "off"
    return "unknown"


def _auto_login() -> str:
    """'set', 'not set' or 'unknown' from `defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser` (the name it prints is never kept)."""
    p = d._run(["defaults", "read", "/Library/Preferences/com.apple.loginwindow", "autoLoginUser"])
    if p.rc == 0 and p.out.strip():
        return "set"
    if p.rc != 0 and "does not exist" in (p.err + p.out).lower():
        return "not set"
    return "unknown"


@_safe
def _c_survival(db):
    skip = _not_mac() or _not_launchd()
    if skip:
        return skip
    fv, auto, domain = _filevault_safe(), _auto_login_safe(), plat.launchd_domain()
    detail = f"FileVault {fv}; automatic login {auto}; the jobs run in the {domain} domain"
    if domain == "user":
        detail += " (experimental Background layout: whether they outlive a logout is UNVERIFIED)"
    if auto == "set" and fv != "on" and domain == "gui":
        return d._pass(f"{detail}: they start after a reboot, and they stop when that user logs out")
    return d._warn(f"{detail}: {SURVIVAL_SENTENCE}",
                   d.fix("Keep this Mac logged in (a locked screen is fine). Automatic login is available only with FileVault off. The README's macOS section "
                         "has the table of what a logout, a sleep and a reboot do"))


def _filevault_safe() -> str:
    try:
        return _filevault()
    except (d.ToolMissing, d.ToolTimeout):
        return "unknown"


def _auto_login_safe() -> str:
    try:
        return _auto_login()
    except (d.ToolMissing, d.ToolTimeout):
        return "unknown"


# ------------------------------------------------------------------ PATH seen by the board

@_safe
def _c_path(db):
    if (skip := _not_mac()):
        return skip
    missing_hard, missing_soft, off_path, ok = [], [], [], []
    for name, required in PATH_TOOLS:
        found = plat.resolve_bin(name)
        if not found:
            (missing_hard if required else missing_soft).append(name)
        elif name in BARE_NAME_TOOLS and not shutil.which(name):
            off_path.append(name)
        else:
            ok.append(name)
    parts = []
    if missing_hard or missing_soft:
        parts.append("not found: " + ", ".join(missing_hard + missing_soft))
    if off_path:
        parts.append("found only outside the board's PATH: " + ", ".join(off_path))
    if not parts:
        return d._pass("the board finds " + ", ".join(ok))
    detail = "; ".join(parts) + (f"; found: {', '.join(ok)}" if ok else "")
    first_missing = (missing_hard + missing_soft)[:1]
    if first_missing:
        text, cmd = d._install(first_missing[0])
        out = d._fail if missing_hard else d._warn
        return out(detail, d.fix(text, cmd))
    return d._warn(detail, d.fix("Put that folder on the PATH of the board's launchd job (re-run the macOS installer, which lists the Homebrew folders), then restart the board",
                                 plat.hint("restart", "ccboard")))


# ------------------------------------------------------------------ folders macOS guards

def _under(path: Path, folder: Path) -> bool:
    for p in {path, _resolved(path)}:
        for f in {folder, _resolved(folder)}:
            if p == f or f in p.parents:
                return True
    return False


def _resolved(p: Path) -> Path:
    try:
        return p.resolve()
    except OSError:
        return p


@_safe
def _c_privacy(db):
    if (skip := _not_mac()):
        return skip
    projects = Path(settings.projects_dir)
    for label, folder in plat.macos_protected_folders():
        if not _under(projects, folder):
            continue
        fix = d.fix(f"Move PROJECTS_DIR out of {label} (for example to ~/projects), or give the board's processes access in System Settings > "
                    "Privacy & Security > Files and Folders (or Full Disk Access); a Mac with nobody at the screen cannot answer the prompt")
        try:
            _listdir(projects)
        except PermissionError:
            return d._fail(f"{projects} is inside {label} and macOS refuses the board access to it", fix)
        except OSError:
            return d._warn(f"{projects} is inside {label}, and it could not be listed", fix)
        return d._warn(f"{projects} is inside {label}: macOS asks for a Files and Folders grant there, and a headless Mac cannot answer the prompt", fix)
    return d._pass(f"{projects} is outside Desktop, Documents, Downloads and iCloud Drive")


def _listdir(folder: Path) -> list[str]:
    """os.listdir; a PermissionError here is macOS refusing a protected folder. Tests patch this."""
    return os.listdir(folder)


def _names(folder: Path) -> list[str]:
    """The entry names of `folder`; [] when it cannot be listed. Tests patch this."""
    try:
        return sorted(_listdir(folder))
    except OSError:
        return []


def _collisions(folder: Path, depth: int = 2) -> list[tuple[str, str]]:
    """Pairs of entries (in `folder`, and in each of its sub-folders when depth is 2) that differ only by letter case."""
    found: list[tuple[str, str]] = []
    names = _names(folder)
    seen: dict[str, str] = {}
    for n in names:
        key = n.casefold()
        if key in seen:
            found.append((seen[key], n))
        else:
            seen[key] = n
    if depth > 1:
        for n in names:
            sub = folder / n
            if sub.is_dir() and not sub.is_symlink():
                found += [(f"{n}/{a}", f"{n}/{b}") for a, b in _collisions(sub, depth - 1)]
    return found


@_safe
def _c_case(db):
    if (skip := _not_mac()):
        return skip
    projects = Path(settings.projects_dir)
    if not projects.is_dir():
        return d._skip(f"{projects} is not a directory")
    if not plat.fs_case_insensitive(str(projects)):
        return d._pass("this volume tells Foo from foo, so names cannot collide")
    pairs = _collisions(projects)
    if not pairs:
        return d._pass("this volume ignores letter case, and no two entries in PROJECTS_DIR differ only by case")
    a, b = pairs[0]
    more = f" (and {len(pairs) - 1} more)" if len(pairs) > 1 else ""
    return d._warn(f"{a!r} and {b!r} differ only by letter case{more}, which this volume treats as one name",
                   d.fix("Rename or merge one of each pair; the board refuses new names that differ only by case"))


# ------------------------------------------------------------------ sleep and logs

_IDLE_KINDS = ("PreventUserIdleSystemSleep", "PreventSystemSleep", "NoIdleSleepAssertion")
_HOLDER_RE = re.compile(r"pid \d+\(([^)]+)\):.*?\b(" + "|".join(_IDLE_KINDS) + r")\b")
_SUMMARY_RE = re.compile(r"^\s*(" + "|".join(_IDLE_KINDS) + r")\s+(\d+)\s*$", re.M)


@_safe
def _c_sleep(db):
    if (skip := _not_mac()):
        return skip
    p = d._run(["pmset", "-g", "assertions"])
    if p.rc != 0:
        return d._warn("pmset -g assertions failed, so the sleep state is unknown")
    holders = sorted({m.group(1) for m in _HOLDER_RE.finditer(p.out)})
    summary = any(int(n) > 0 for _k, n in _SUMMARY_RE.findall(p.out))
    if "caffeinate" in holders:
        return d._pass("caffeinate holds idle sleep off (a closed lid on battery still sleeps the Mac: UNVERIFIED)")
    if holders or summary:
        who = f" by {', '.join(holders)}" if holders else ""
        return d._pass(f"idle sleep is held off{who}")
    return d._warn("nothing holds idle sleep off: the Mac sleeps when idle and the board is unreachable meanwhile",
                   d.fix("Re-run the macOS installer with CCBOARD_KEEP_AWAKE=1 (it adds the dev.ccboard.awake job that runs caffeinate), or turn on "
                         "System Settings > Energy > Prevent automatic sleeping"))


@_safe
def _c_logs(db):
    if (skip := _not_mac()):
        return skip
    folder = plat.macos_log_dir()
    if not folder.is_dir():
        return d._skip(f"{folder} does not exist yet (the macOS installer creates it)")
    sizes = []
    for f in folder.iterdir():
        try:
            if f.is_file():
                sizes.append((f.stat().st_size, f.name))
        except OSError:
            continue
    total = sum(s for s, _n in sizes)
    mb = total / (1024 * 1024)
    if total <= LOG_WARN_BYTES:
        return d._pass(f"{folder} holds {mb:.1f} MB")
    biggest = max(sizes)[1]
    return d._warn(f"{folder} holds {mb:.0f} MB and launchd never rotates it",
                   d.fix(f"Empty the largest log ({biggest}); the jobs keep writing to it", f": > {shlex.quote(str(folder / biggest))}"))


# ------------------------------------------------------------------ registry

CHECKS = (
    ("macos-jobs", "box", "launchd jobs (macOS)", _c_jobs),
    ("macos-survival", "box", "Logout and reboot (macOS)", _c_survival),
    ("macos-path", "box", "Tools on the board's PATH (macOS)", _c_path),
    ("macos-privacy", "box", "Projects folder privacy (macOS)", _c_privacy),
    ("macos-case", "box", "Projects folder name case (macOS)", _c_case),
    ("macos-sleep", "box", "Sleep (macOS)", _c_sleep),
    ("macos-logs", "box", "Log folder size (macOS)", _c_logs),
)


def register_all() -> None:
    """Add (or replace) the macOS checks in the doctor's registry."""
    for check_id, group, label, fn in CHECKS:
        d.register(check_id, group, label, fn)


if plat.IS_MACOS:            # imported by app/doctor.py on a Mac, or first by anyone: either order ends with the checks registered once
    register_all()
