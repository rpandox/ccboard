"""The doctor's WSL2 checks (issue #118), registered by app/doctor.py on WSL only (register_all); a board anywhere else lists none.

Every check skips with a plain reason off WSL, so a caller that runs one by name elsewhere gets "skip", never a wrong answer. Every probe goes
through the doctor's own `_run` (a timeout, ToolMissing and ToolTimeout, patched by tests the same way) and a check never raises. Nothing here
changes anything: the probes only read (systemctl is-system-running, ss, the mount table, one Get-ScheduledTask through Windows interop).

What a doctor inside the distro cannot do: report that the distro is stopped, because the board is stopped with it. `wsl-restarts` tells you
afterwards, from a record the Sampler's tick keeps (kv `wsl_starts`: one row per start of PID 1, told apart by the kernel boot id and PID 1's
start tick, so neither a board restart nor a clock that drifted while Windows slept counts as a restart of the distro; in the docker runtime
PID 1 is the container's entrypoint, so only a restart of the virtual machine is told apart there).

The names are looked up on d.* at call time, because app/doctor.py imports this module while it is still being defined (and this module may be
imported first, which imports the doctor from here: the registration at the bottom works in either order).
"""
from __future__ import annotations

import functools
import os
import re
import shutil
from pathlib import Path

from . import doctor as d
from . import platform as plat
from .config import settings

TASK_NAME = "ccboard-wsl-keepalive"                # the Scheduled Task scripts/windows/ccboard-wsl-keepalive.ps1 registers
POWERSHELL_FALLBACK = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")   # when [interop] appendWindowsPath=false keeps Windows off PATH
PS_TIMEOUT = 4.0                                   # below the doctor's 5 s cap on a check; the first call after a long idle can be slow
KV_STARTS = "wsl_starts"                           # {"starts": [{"id", "at"}...]}, oldest first (see record_start)
KEEP_STARTS = 60                                   # entries kept
RESTART_WINDOW = 24 * 3600                         # seconds the restart count looks back
RESTART_WARN = 2                                   # this many restarts in the window is a warning
LOOPBACK_PREFIXES = ("127.", "[::1]", "::1")
SURVIVAL_TEXT = "a distro that stopped cannot report its own stop (the board stops with it); the restarts show here afterwards"
KEEPALIVE_CMD = "powershell -NoProfile -ExecutionPolicy Bypass -File .\\ccboard-wsl-keepalive.ps1 -Distro {distro}"


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


def _not_wsl():
    return None if plat.is_wsl() else d._skip("WSL only: this board is not running inside WSL")


def _distro() -> str:
    return os.environ.get("WSL_DISTRO_NAME") or "<distro>"


# ------------------------------------------------------------------ systemd

@_safe
def _c_systemd(db):
    if (skip := _not_wsl()):
        return skip
    p = d._run(["systemctl", "is-system-running"])
    word = (p.out.strip().splitlines() or [""])[0].strip().lower()
    if word == "running":
        return d._pass("systemd is running")
    if word == "degraded":
        return d._pass("systemd is running but degraded (a unit failed: systemctl --failed lists it)")
    recipe = d.fix("Add the two lines [boot] and systemd=true to /etc/wsl.conf, then run `wsl --shutdown` in Windows and open the distro again "
                   "(needs WSL 0.67.6 or newer; [boot] works on Windows 11 and Server 2022, on Windows 10 it is UNVERIFIED)")
    if word in ("starting", "initializing", "stopping"):
        return d._warn(f"systemd reports '{word}': it is still coming up or going down", d.fix("Wait a minute and check again", "systemctl is-system-running"))
    return d._fail(f"systemd is not running here ({word or 'no answer'}); the board's services, the tmux server and the timers need it", recipe)


# ------------------------------------------------------------------ files

@_safe
def _c_files(db):
    if (skip := _not_wsl()):
        return skip
    where = (("the projects folder", settings.projects_dir), ("the data folder", settings.data_dir),
             ("the Claude config folder", settings.claude_config_dir), ("the Codex folder", settings.codex_home))
    bad = []
    for label, path in where:
        mount = plat.windows_mount(path)
        if plat.under_drvfs(path) or mount is not None:
            bad.append(f"{label} ({path})")
    if not bad:
        return d._pass("the projects, data, Claude and Codex folders are on the distro's own file system")
    return d._warn(f"on a Windows drive: {', '.join(bad)}. There file owners and modes are not real, change stamps are weak and it is slow",
                   d.fix("Move them into the distro's home (ext4), for example under /home/<user>, and point the setting at the new place; "
                         "keep the checkout there too"))


# ------------------------------------------------------------------ agents on the distro's PATH

def _under_mnt(path: str | None) -> bool:
    if not path:
        return False
    return any(str(x).startswith("/mnt/") for x in (path, os.path.realpath(path)))


@_safe
def _c_path(db):
    if (skip := _not_wsl()):
        return skip
    found = {"claude": settings.claude_bin(), "codex": settings.codex_bin()}
    present = {n: p for n, p in found.items() if p}
    if not present:
        return d._skip("neither claude nor codex was found (the Claude and Codex checks say what to install)")
    bad = [n for n, p in present.items() if _under_mnt(p)]
    if bad:
        return d._warn(f"{' and '.join(bad)} resolve to a Windows program under /mnt/ ({present[bad[0]]}); the board would start the Windows build, not one inside the distro",
                       d.fix("Install the agent inside the distro, and set [interop] appendWindowsPath=false in /etc/wsl.conf so Windows programs stop shadowing Linux ones "
                             "(then `wsl --shutdown`). Check with: which claude codex",
                             "which claude codex"))
    return d._pass("; ".join(f"{n} at {p}" for n, p in present.items()) + " (inside the distro)")


# ------------------------------------------------------------------ network

def _local_addresses(text: str) -> list[str]:
    """The local address column of `ss -ltnH`."""
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 4 and f[0].upper().startswith("LISTEN"):
            out.append(f[3])
    return out


def _exposed(addresses: list[str], ports) -> list[str]:
    """'<address>' for each listener on one of the board's ports that is not on loopback."""
    out = []
    for a in addresses:
        host, _, port = a.rpartition(":")
        if port.isdigit() and int(port) in ports and not host.startswith(LOOPBACK_PREFIXES):
            out.append(a)
    return out


@_safe
def _c_network(db):
    if (skip := _not_wsl()):
        return skip
    mode = plat.wsl_networking_mode()
    ports = {settings.port, settings.ttyd_port, settings.code_server_port}
    try:
        p = d._run(["ss", "-ltnH"])
        addresses = _local_addresses(p.out) if p.rc == 0 else None
    except (d.ToolMissing, d.ToolTimeout):
        addresses = None
    mode_text = {"nat": "NAT networking", "mirrored": "mirrored networking", "unknown": "the networking mode is not reported (wslinfo is missing: update WSL with `wsl --update`)"}.get(mode, f"networking mode {mode}")
    if addresses is not None:
        loose = _exposed(addresses, ports)
        if loose:
            return d._warn(f"{mode_text}; {', '.join(loose)} listens beyond loopback. The board, ttyd and code-server are meant for 127.0.0.1 only, "
                           "and with mirrored networking the LAN can reach a port like that",
                           d.fix("Bind it back to 127.0.0.1 (the installer's units do) and restart the unit; open a port to the LAN only on purpose, with a Hyper-V firewall rule"))
    seen = "the board, ttyd and code-server listen on loopback only" if addresses is not None else "the listeners could not be read (ss)"
    if mode in ("bridged", "virtioproxy", "none"):
        return d._warn(f"{mode_text}: the recipe covers NAT and mirrored only; {seen}",
                       d.fix("Use NAT (the default) or mirrored networking (Windows 11 22H2 or newer: networkingMode=mirrored in .wslconfig)"))
    return d._pass(f"{mode_text}; {seen}")


# ------------------------------------------------------------------ Windows side: the keep-alive task

def _powershell() -> str | None:
    return shutil.which("powershell.exe") or (str(POWERSHELL_FALLBACK) if POWERSHELL_FALLBACK.exists() else None)


STATE_RE = re.compile(r"\b(Running|Ready|Disabled|Queued|Unknown)\b", re.I)


@_safe
def _c_keepalive(db):
    if (skip := _not_wsl()):
        return skip
    fix_text = ("Run scripts/windows/ccboard-wsl-keepalive.ps1 from Windows PowerShell (copy it out of the distro first; no administrator rights are needed). "
                "Without it WSL stops the virtual machine, and every tmux session with it, minutes after the last terminal closes")
    fix_cmd = KEEPALIVE_CMD.format(distro=_distro())
    ps = _powershell() if plat.wsl_interop() else None
    if ps is None:
        return d._skip("unknown: Windows interop is off or powershell.exe cannot be reached, so the board cannot see the keep-alive task",
                       d.fix("Turn on [interop] enabled=true in /etc/wsl.conf (then `wsl --shutdown`), or check the task by hand in Windows: "
                             f"Get-ScheduledTask -TaskName {TASK_NAME}"))
    p = d._run([ps, "-NoProfile", "-Command", f"Get-ScheduledTask -TaskName {TASK_NAME}"], timeout=PS_TIMEOUT)
    line = next((ln for ln in p.out.splitlines() if TASK_NAME in ln), "")
    if p.rc != 0 or not line:
        return d._warn(f"no Scheduled Task named {TASK_NAME} on Windows: nothing keeps the distro running when the last terminal closes", d.fix(fix_text, fix_cmd))
    m = STATE_RE.search(line[line.index(TASK_NAME) + len(TASK_NAME):])
    state = m.group(1).capitalize() if m else "Unknown"
    if state == "Running":
        return d._pass(f"the {TASK_NAME} task is registered and running on Windows")
    if state == "Disabled":
        return d._warn(f"the {TASK_NAME} task is registered but disabled", d.fix("Enable it in Task Scheduler, or run the script again", fix_cmd))
    return d._warn(f"the {TASK_NAME} task is registered but not running now (state {state}); with the default sleep method it should read Running",
                   d.fix("Start it from Windows: Start-ScheduledTask -TaskName " + TASK_NAME + " (the dbus method exits at once and reads Ready: then this is expected)"))


# ------------------------------------------------------------------ restarts

def _starts(db) -> list[dict]:
    row = db.kv_get(KV_STARTS) if db is not None and hasattr(db, "kv_get") else None
    val = row.get("value") if isinstance(row, dict) else None
    items = val.get("starts") if isinstance(val, dict) else None
    out = []
    for e in items if isinstance(items, list) else []:
        if isinstance(e, dict) and isinstance(e.get("id"), str) and isinstance(e.get("at"), (int, float)):
            out.append({"id": e["id"], "at": float(e["at"])})
    return sorted(out, key=lambda e: e["at"])


_seen_id: str | None = None        # the init identity this process already recorded (no database read on a quiet tick)


def record_start(db, now=None) -> bool:
    """Record the current start of PID 1 in kv `wsl_starts` when it is not the newest one there. True when a row was added. Runs on the
    Sampler's 15 s tick (TICK_HOOKS), so it costs one /proc read per tick and a database write only when the distro or the VM restarted."""
    global _seen_id
    ident = plat.init_identity()
    if ident is None or ident[0] == _seen_id or db is None:
        return False
    sid, started = ident
    if settings.runtime == "docker":            # in the container PID 1 is the entrypoint, which starts again at every image swap: only the VM's boot id counts there
        sid = sid.split(":")[0]
    rows = _starts(db)
    _seen_id = sid
    if rows and rows[-1]["id"] == sid:
        return False
    rows.append({"id": sid, "at": round(started)})
    db.kv_set(KV_STARTS, {"starts": rows[-KEEP_STARTS:]})
    return True


def _tick(db, now) -> None:
    """The Sampler hook: WSL only, and it never raises into the tick."""
    if plat.is_wsl():
        try:
            record_start(db, now)
        except Exception:
            pass


def _ago(seconds: float) -> str:
    return f"{int(seconds // 3600)} h" if seconds >= 3600 else f"{max(1, int(seconds // 60))} min"


@_safe
def _c_restarts(db):
    if (skip := _not_wsl()):
        return skip
    rows = _starts(db)
    if not rows:
        return d._skip(f"no start history yet: the board records the distro's start on its first tick. {SURVIVAL_TEXT}")
    now = d._utcnow().timestamp()
    recent = [e for e in rows[1:] if e["at"] >= now - RESTART_WINDOW]       # the oldest row is the first sighting, not a restart
    since = _ago(now - rows[0]["at"]) if now - rows[0]["at"] >= 0 else "?"
    if len(recent) >= RESTART_WARN:
        last = _ago(max(0, now - recent[-1]["at"]))
        return d._warn(f"the distro or its virtual machine restarted {len(recent)} times in the last 24 hours (the last {last} ago). Every restart ends the tmux sessions; "
                       f"the board relaunches them. {SURVIVAL_TEXT}",
                       d.fix("Register the keep-alive task on Windows (it stops WSL from shutting the VM down when the last terminal closes), and look for a Windows reboot, "
                             "`wsl --shutdown` or an update behind the restarts", KEEPALIVE_CMD.format(distro=_distro())))
    if recent:
        return d._pass(f"1 restart in the last 24 hours (the last {_ago(max(0, now - recent[-1]['at']))} ago); history starts {since} ago. {SURVIVAL_TEXT}")
    return d._pass(f"no restart in the last 24 hours; history starts {since} ago. {SURVIVAL_TEXT}")


# ------------------------------------------------------------------ Tailscale placement and previews

@_safe
def _c_placement(db):
    if (skip := _not_wsl()):
        return skip
    placement = plat.tailscale_placement()
    if placement != "host":
        return d._pass("Tailscale runs inside this distro (placement wsl); Tailscale advises running it on the Windows host instead, never both")
    from . import tailscale as ts
    cli = ts.find_cli()
    if cli is not None and ts.variant(cli) == "wsl-host":
        return d._pass(f"Tailscale runs on the Windows host (placement host); previews would go through {cli.exe} over Windows interop (to verify on a device)")
    return d._skip("previews are unavailable: Tailscale runs on the Windows host (placement host) and the board cannot reach it from the distro, "
                   "so it cannot open a preview port. Everything else works; serve the board from Windows with the commands install.sh printed",
                   d.fix("To get previews, make tailscale.exe reachable on the distro's PATH through Windows interop ([interop] enabled=true), or move Tailscale into the distro "
                         "(CCBOARD_TAILSCALE_PLACEMENT=wsl ./install.sh)"))


# ------------------------------------------------------------------ registry

CHECKS = (
    ("wsl-systemd", "box", "systemd (WSL)", _c_systemd),
    ("wsl-files", "box", "Files on the distro (WSL)", _c_files),
    ("wsl-path", "box", "Agents inside the distro (WSL)", _c_path),
    ("wsl-network", "box", "Networking (WSL)", _c_network),
    ("wsl-keepalive", "box", "Keep-alive task on Windows (WSL)", _c_keepalive),
    ("wsl-restarts", "box", "Distro restarts (WSL)", _c_restarts),
    ("wsl-placement", "box", "Tailscale placement (WSL)", _c_placement),
)


def register_all() -> None:
    """Add (or replace) the WSL checks in the doctor's registry, and the Sampler hook that records the distro's starts."""
    from . import samples            # imported late: samples registers hooks that import most of the board, and this module is imported while the doctor is still being defined
    for check_id, group, label, fn in CHECKS:
        d.register(check_id, group, label, fn)
    if _tick not in samples.TICK_HOOKS:
        samples.TICK_HOOKS.append(_tick)


if plat.is_wsl():            # imported by app/doctor.py on WSL, or first by anyone: either order ends with the checks registered once
    register_all()
