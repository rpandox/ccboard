"""The doctor's node checks for a Mac and for WSL2 (issue #154), registered by app/doctor.py on those two systems only; a Linux board lists none.
Also `nodes-health` (issue #153, HEALTH below), which app/doctor.py registers on EVERY board, because the hub is usually the Linux box.

What these four add to the macOS and WSL checks of issues #117 and #118 is the node's point of view: a sleeping Mac or a distro without its keep-alive
is not broken, it is a node that other boards see as stale. Every check skips with a plain reason off its system, so a caller that runs one by name
elsewhere gets "skip", never a wrong answer. Every probe goes through the doctor's own `_run` (tests patch it; no real command runs there), a check
only reads, a check never raises, and an answer it cannot read is a skip that says "unknown", never a pass.

  node-mac-public-url   macOS: CCBOARD_PUBLIC_URL is set (other boards are told this address when they pair)
  node-sleep            macOS: something holds idle sleep off (`pmset -g assertions`), else the Mac goes stale while it sleeps
  node-wsl-keepalive    WSL2: the Windows keep-alive of issue #118 is there (the wsl-keepalive probe, worded for the node)
  node-gh               macOS and WSL2: `gh auth status` succeeds (its exit code only: the output can name the account)

The names are looked up on d.* at call time, because app/doctor.py imports this module while it is still being defined (and this module may be
imported first, which imports the doctor from here: the registration at the bottom works in either order).
"""
from __future__ import annotations

import functools
import re

from . import doctor as d
from . import platform as plat
from .config import settings

CAFFEINATE = "caffeinate -s"          # held for the length of a long run; -s keeps the Mac awake on AC power only. To verify on the target macOS
KEEPALIVE_STALE = "the node goes stale within minutes of the last terminal closing"
_IDLE_KINDS = ("PreventUserIdleSystemSleep", "PreventSystemSleep", "NoIdleSleepAssertion")
_SUMMARY_RE = re.compile(r"^\s*(" + "|".join(_IDLE_KINDS) + r")\s+(\d+)\s*$", re.M)
_HOLDER_RE = re.compile(r"pid \d+\(([^)]+)\):.*?\b(" + "|".join(_IDLE_KINDS) + r")\b")


def _safe(fn):
    """A check never raises and never hangs: a tool that is missing or slow is a skip or a warning, anything unforeseen a plain warning."""
    @functools.wraps(fn)
    def run(db):
        try:
            return fn(db)
        except d.ToolMissing as e:
            return d._skip(f"{e.name} is not available here")
        except d.ToolTimeout as e:
            return d._warn(f"{e.name or 'a tool'} did not answer in time")
        except Exception as e:                        # the last net: a doctor row must always come back
            return d._warn(f"check error: {type(e).__name__}")
    return run


def _not_mac():
    return None if plat.IS_MACOS else d._skip("macOS only: this board is not running on a Mac")


def _not_wsl():
    return None if plat.is_wsl() else d._skip("WSL only: this board is not running inside WSL")


# ------------------------------------------------------------------ the Mac's address

@_safe
def _c_mac_public_url(db):
    if (skip := _not_mac()):
        return skip
    url = settings.public_url
    if url.startswith("https://"):
        return d._pass(f"CCBOARD_PUBLIC_URL is set ({url}); other boards are told this address when they pair")
    line = "CCBOARD_PUBLIC_URL=https://<this-mac>.<tailnet>.ts.net"
    if url:
        return d._warn("CCBOARD_PUBLIC_URL is not an https address, so other boards cannot reach this Mac by it",
                       d.fix("Set CCBOARD_PUBLIC_URL in the settings file (<data dir>/env, chmod 600) to this Mac's https tailnet address, then restart the board", line))
    return d._warn("CCBOARD_PUBLIC_URL is empty: this Mac's address is guessed from Tailscale (the installer's port is 443) and other boards are not told it",
                   d.fix("Set CCBOARD_PUBLIC_URL in the settings file (<data dir>/env, chmod 600) to this Mac's https tailnet address, then restart the board "
                         f"({plat.hint('restart', 'ccboard')})", line))


# ------------------------------------------------------------------ sleep

@_safe
def _c_sleep(db):
    if (skip := _not_mac()):
        return skip
    p = d._run(["pmset", "-g", "assertions"])
    if p.rc != 0:
        return d._skip("unknown: pmset -g assertions failed, so it is not known whether this Mac sleeps")
    holders = sorted({m.group(1) for m in _HOLDER_RE.finditer(p.out)})
    held = bool(holders) or any(int(n) > 0 for _k, n in _SUMMARY_RE.findall(p.out))
    if held:
        who = f" by {', '.join(holders)}" if holders else ""
        return d._pass(f"idle sleep is held off{who}, so this node stays online (a closed lid on battery still sleeps the Mac: to verify)")
    return d._warn("nothing holds idle sleep off: while this Mac sleeps other boards show this node as stale (not broken); it is online again on its first poll after wake",
                   d.fix(f"For a long run keep the Mac awake with `{CAFFEINATE}` in a terminal (it holds sleep off on AC power while it runs; to verify on this macOS), "
                         "or re-run the macOS installer with CCBOARD_KEEP_AWAKE=1", CAFFEINATE))


# ------------------------------------------------------------------ the keep-alive

@_safe
def _c_wsl_keepalive(db):
    if (skip := _not_wsl()):
        return skip
    from . import doctor_wsl as dw
    out = dw._c_keepalive(db)
    if out.status == "pass":
        return d._pass("the Windows keep-alive is running, so this node stays online while no terminal is open")
    if out.status == "warn":
        return d._warn(f"{out.detail}; {KEEPALIVE_STALE}", out.fix)
    return out                                       # unknown (interop off, no powershell.exe) stays unknown, with its own fix


# ------------------------------------------------------------------ gh

@_safe
def _c_gh(db):
    if not (plat.IS_MACOS or plat.is_wsl()):
        return d._skip("macOS and WSL only: the Linux box has the gh row")
    try:
        p = d._run(["gh", "auth", "status"], d.GH_AUTH_LIMIT)       # exit code only: the output can name the account and where the token is kept
    except d.ToolMissing:
        return d._warn("gh is not installed on this machine, so tasks cannot open pull requests and a repo cannot be cloned from GitHub here",
                       d.fix("Install the GitHub CLI, then log in", plat.hint("install", "gh")))
    except d.ToolTimeout:
        return d._skip("unknown: gh auth status did not answer in time")
    if p.rc == 0:
        return d._pass("a gh login exists on this machine")
    return d._warn("no gh login on this machine: pull requests and cloning from GitHub fail here",
                   d.fix("Log gh in on this machine (the board only reads the exit code of gh auth status)", "gh auth login"))


# ------------------------------------------------------------------ the paired nodes, as the hub last read them (issue #153; every platform, registered by app/doctor.py)

SKEW_TEXT = "turn on automatic time (Linux: timedatectl set-ntp true; macOS: System Settings, General, Date & Time, Set time automatically)"


def _age(s) -> str:
    s = int(s or 0)
    return f"{s} s" if s < 90 else f"{s // 60} min" if s < 5400 else f"{s // 3600} h" if s < 172800 else f"{s // 86400} days"


def _health_rows(regs: list[dict], recs: list[dict]) -> list[tuple[int, str, str, str]]:
    """(rank, name, problem, fix) for every paired node that is not well, worst first: 0 unauthorized or needs a new pairing, 1 another device at its address
    (or a node that no longer knows this board), 2 another node API, 3 offline, 4 a clock more than 5 s off. A node that is online (or stale) with a good clock
    is not listed."""
    by_id = {r.get("peer_id"): r for r in recs if isinstance(r, dict)}
    out = []
    for reg in regs:
        rec = by_id.get(reg.get("peer_id")) or {}
        name = reg.get("name") or reg.get("handle") or "a node"
        status = rec.get("status")
        if rec.get("error_kind") == "clock" or reg.get("last_error") == "clock":
            out.append((0, name, f"{name} refused the signature because the clocks differ by more than 120 seconds", SKEW_TEXT))
        elif (rec.get("error_kind") or reg.get("last_error")) in ("stale", "replay", "bad_signature", "signature_required"):
            reason = rec.get("error_kind") or reg.get("last_error")
            words = {"stale": "the signature is too old", "replay": "the signature was already used",
                     "bad_signature": "the signature is not valid", "signature_required": "the signature is missing"}
            fix = "Wait for the next check; if it persists, check automatic time on both boards" if reason in ("stale", "replay") else "Check that both boards have the key update and signing enabled"
            out.append((0, name, f"{name} refused the call: {words[reason]}", fix))
        elif rec.get("error_kind") == "own_key_changed" or reg.get("last_error") == "own_key_changed":
            out.append((0, name, f"{name} refused this board's signing key: it differs from the key pinned there",
                        f"Remove this board's pair on {name} first, then pair again and compare the key ids"))
        elif rec.get("error_kind") == "key_changed" or reg.get("last_error") == "key_changed":
            out.append((0, name, f"{name} has a different signing key", "Remove the pair and pair again with a new code; compare the key ids"))
        elif rec.get("error_kind") == "key_required" or reg.get("last_error") == "key_required":
            out.append((0, name, f"{name}: this pair has no signing key and is refused",
                        "Pair again with signing enabled on both boards, or set CCBOARD_NODE_KEYS=prefer"))
        elif reg.get("needs_repair") or status == "unauthorized":
            out.append((0, name, f"{name} no longer takes this board's token (the pairing was removed there, or a rotation answer was lost)",
                        f"Open Settings > Nodes and press Re-pair on {name} (Remove, then Add node with a new code made on {name})"))
        elif status == "unpaired" and rec.get("error_kind") == "identity_changed":
            out.append((1, name, f"the address of {name} now belongs to another device (it answers with another node id); nothing of it is shown",
                        f"Open Settings > Nodes and press Remove on {name}, or pair it again if the device was set up anew"))
        elif status == "unpaired":
            out.append((1, name, f"{name} no longer knows this board", f"Open Settings > Nodes and press Remove on {name}, then pair it again"))
        elif status == "incompatible":
            theirs = rec.get("peer_api")
            theirs = theirs if isinstance(theirs, int) and not isinstance(theirs, bool) else "unknown"
            out.append((2, name, f"{name} speaks node API {theirs}, this board speaks {rec.get('api') or 1}: its card is shown, its actions are refused",
                        "Update the older board so both run the same ccboard release"))
        elif status == "offline":
            age = rec.get("age_s")
            seen = f"last seen {_age(age)} ago" if isinstance(age, int) and not isinstance(age, bool) else "never answered"
            out.append((3, name, f"{name} is offline, {seen}",
                        f"Check that {name} is awake and its board is running (a sleeping Mac, or WSL2 without its keep-alive, goes offline), then wait for the next poll"))
        ms = rec.get("skew_ms")
        if rec.get("skew_warn") and status in ("online", "stale") and isinstance(ms, int) and not isinstance(ms, bool):
            off = f"{abs(ms) // 1000} s {'ahead' if ms > 0 else 'behind'}"
            out.append((4, name, f"the clock of {name} is {off} (claims use GitHub's clock, so nothing runs twice, but times on {name} read wrong)",
                        f"On {name}, {SKEW_TEXT}"))
    return sorted(out, key=lambda x: (x[0], x[1].lower()))


def _c_nodes_health(db):
    """nodes-health: the paired nodes that are offline, unauthorized, at another device, on another node API or with a clock off, worst first, each with its fix.
    Reads only what the hub already holds (no call to any node, so an offline node cannot make the doctor slow). A board with no paired node skips."""
    from . import main, nodes
    try:
        regs = [r for r in nodes.peers(db=db) if r.get("direction") == "out" and not r.get("legacy")]
    except Exception as e:
        return d._warn(f"the paired nodes could not be read ({e.__class__.__name__})", d.fix("Check that the data directory is writable by the board"))
    if not regs:
        return d._skip("no node is paired with this board")
    try:
        recs = main._hub().records()
    except Exception as e:
        return d._skip(f"unknown: the hub's readings could not be read ({e.__class__.__name__})")
    rows = _health_rows(regs, recs)
    if not rows:
        return d._pass(f"{len(regs)} paired node{'s' if len(regs) != 1 else ''}, each answering, on this board's node API and with a clock within 5 s")
    detail = "; ".join(f"{p} (fix: {f})" for _r, _n, p, f in rows)
    return d._warn(detail[:1500], d.fix(rows[0][3]))


HEALTH = ("nodes-health", "box", "Paired nodes answer", _c_nodes_health)      # registered by app/doctor.py on every board (a wrapper, so either module may be imported first)


# ------------------------------------------------------------------ registry

CHECKS = (
    ("node-mac-public-url", "box", "This Mac's address for other boards", _c_mac_public_url),
    ("node-sleep", "box", "Sleep keeps this node online (macOS)", _c_sleep),
    ("node-wsl-keepalive", "box", "Keep-alive keeps this node online (WSL)", _c_wsl_keepalive),
    ("node-gh", "box", "GitHub login on this node", _c_gh),
)


def register_all() -> None:
    """Add (or replace) the node checks in the doctor's registry."""
    for check_id, group, label, fn in CHECKS:
        d.register(check_id, group, label, fn)


if plat.IS_MACOS or plat.is_wsl():     # imported by app/doctor.py on a Mac or in WSL, or first by anyone: either order ends with the checks registered once
    register_all()
