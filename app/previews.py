"""Preview links: expose a dev server running inside a task's session on its own tailnet HTTPS port
(`tailscale serve --bg --https=<port> http://127.0.0.1:<devport>`), one port per worktree."""
from __future__ import annotations

import logging
import os
import shutil      # noqa: F401  (tests patch previews.shutil.which and previews.subprocess.run: the stdlib modules, shared with app.tailscale)
import subprocess  # noqa: F401
import time
from urllib.parse import urlsplit

from . import platform as plat
from . import tailscale as ts
from .config import settings

log = logging.getLogger("ccboard.previews")
TAILSCALE_SOCK = ts.TAILSCALE_SOCK   # mounted into the container by compose; the socket check below reads THIS name (tests patch it here)


class PreviewError(Exception):
    pass


def children_of(pid: int) -> list[int]:
    """Direct children (app.platform.children: /proc on Linux, psutil elsewhere)."""
    return plat.children(pid)


def descendants(pid: int, limit: int = 500) -> set[int]:
    seen, todo = set(), [pid]
    while todo and len(seen) < limit:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        todo.extend(children_of(p))
    return seen


def listening(pids=None) -> dict[int, set[int]]:
    """pid -> {ports} for TCP listeners on loopback/any address (app.platform.listening_ports: ss -ltnpH on Linux), of `pids` or of all."""
    return plat.listening_ports(pids)


def _url_port(url: str) -> int | None:
    try:
        return urlsplit(url).port if url else None
    except ValueError:
        return None


def infra_ports() -> set[int]:
    """The box's own services on loopback, never published as a preview (issue #44, F-01): the board, ttyd (a shell), code-server, the ntfy
    server and the claude-mem worker (no sign-in, a writable API). Each of them already has its own mapping, or deliberately none."""
    ports = {settings.port, settings.ttyd_port, settings.code_server_port}
    p = _url_port(settings.ntfy_url)
    if p:
        ports.add(p)
    try:
        from . import memory
        p = memory.discover().get("port")
    except Exception:                    # a broken claude-mem settings file must not block a preview
        p = None
    if isinstance(p, int) and p > 0:
        ports.add(p)
    if settings.mem_port:
        ports.add(settings.mem_port)
    return ports


def check_port(port: int) -> None:
    """PreviewError(400 in the route) for a port that is one of the box's own services."""
    if port in infra_ports():
        raise PreviewError(f"port {port} is one of the board's own services (the board, terminal, code-server, ntfy or claude-mem); "
                           "a preview publishes a dev server running in the task's session")


def ports_under(pane_pid: int) -> list[int]:
    """Listening ports of any process under the session's pane (dev servers Claude started)."""
    if not pane_pid:
        return []
    tree = descendants(pane_pid)
    ports: set[int] = set()
    for pid, ps in listening(tree).items():
        if pid in tree:
            ports |= ps
    infra = infra_ports()
    return sorted(p for p in ports if p not in infra)


def public_host() -> str:
    return urlsplit(settings.public_url).hostname or "" if settings.public_url else ""


DENIED_TTL = 300        # seconds a refused `serve` keeps the control off; after that one more try is allowed (nothing else could clear it)
_refused = {"at": 0.0, "why": ""}


def capability() -> dict:
    """#126: can a preview be made here at all, and if not why, so the control says so BEFORE the click: {available, code, reason}. In the order a
    person would fix it: CCBOARD_PUBLIC_URL, a WSL2 distro whose Tailscale is on the Windows side (no route), no Tailscale command, Tailscale not
    running, not signed in, and a `serve` that was refused a moment ago (denied; only known after one real attempt, so it expires after DENIED_TTL).
    Reads the board's cached Tailscale reading (app/nodes.py, two minutes); runs no `serve`, sends no signal, never raises."""
    def off(code: str, reason: str) -> dict:
        return {"available": False, "code": code, "reason": reason}
    try:
        from . import nodes
        if not public_host():
            return off("no_public_url", "CCBOARD_PUBLIC_URL is not set (rerun install.sh)")
        if nodes.windows_side():
            return off("wsl_host", "Tailscale runs on the Windows side of this WSL2 distro, which has no route to expose a preview")
        cli = ts.find_cli()
        if cli is None or (shutil.which(cli.exe) is None and not os.path.exists(cli.exe)):      # Linux always names a command; it may not be there
            return off("no_cli", ts.missing_reason())
        d = nodes._ts()
        if not isinstance(d, dict):
            return off("not_running", "Tailscale is not running or cannot be reached from the board")
        if str(d.get("BackendState") or "") != "Running":
            return off("not_signed_in", "Tailscale is not logged in on this device")
        if _refused["why"] and time.monotonic() - _refused["at"] < DENIED_TTL:
            return off("denied", _refused["why"])
    except Exception as e:
        log.debug("preview capability failed: %s", e.__class__.__name__)
        return {"available": True, "code": "unknown", "reason": None}
    return {"available": True, "code": "ok", "reason": None}


def _refusal(text: str) -> bool:
    """Is this `serve` failure a refusal of the user (operator not set, the app refused) rather than a port or network problem?"""
    t = text.lower()
    return "operator" in t or "refused it" in t or "denied" in t


def serve_cmd(*args: str) -> list[str]:
    """tailscale as the operator if allowed (always so in the container), else through the sudoers rule installed by install.sh (app/tailscale.py)."""
    return ts.serve_cmd(*args)


def _denied(cp) -> bool:
    return ts._denied(cp)


def _operator_hint() -> str:
    return ts.operator_hint()


def _run_serve(args: list[str]) -> None:
    """app.tailscale.run_serve with this module's socket constant; its sentences become PreviewError."""
    try:
        ts.run_serve(args, sock=TAILSCALE_SOCK)
    except ts.TailscaleError as e:
        if _refusal(str(e)):
            _refused.update(at=time.monotonic(), why=str(e)[:300])         # capability() reports it, so the control says "denied" before the next click
        raise PreviewError(str(e)) from None
    _refused["why"] = ""


def serve_on(https_port: int, local_port: int) -> str:
    _run_serve(ts.serve_on_args(https_port, local_port))
    return f"https://{public_host()}:{https_port}/"


def serve_off(https_port: int) -> None:
    try:
        _run_serve(ts.serve_off_args(https_port))
    except PreviewError as e:
        log.info("serve off %s: %s", https_port, e)


def reserved_https_ports() -> set[int]:
    """Tailnet HTTPS ports a preview never takes: 443 (another service's Funnel on the owner's box; never touched), the board's,
    code-server's, ntfy's (NTFY_HTTPS_PORT, default 8444) and the claude-mem viewer's (issue #44, F-01)."""
    out = {443, settings.ccboard_https_port, settings.code_https_port, settings.ntfy_https_port}
    if settings.mem_https_port:
        out.add(settings.mem_https_port)
    return out


def allocate_https_port(used: set[int]) -> int:
    reserved = reserved_https_ports()
    p = settings.preview_https_base
    while p in used or p in reserved:
        p += 1
        if p > settings.preview_https_base + 200:
            raise PreviewError("no free preview port")
    return p
