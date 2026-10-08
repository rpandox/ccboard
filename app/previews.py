"""Preview links: expose a dev server running inside a task's session on its own tailnet HTTPS port
(`tailscale serve --bg --https=<port> http://127.0.0.1:<devport>`), one port per worktree."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .config import settings

log = logging.getLogger("ccboard.previews")
SS_RE = re.compile(r"pid=(\d+)")
TAILSCALE_SOCK = Path("/var/run/tailscale/tailscaled.sock")   # mounted into the container by compose


class PreviewError(Exception):
    pass


def children_of(pid: int) -> list[int]:
    """Direct children from /proc (Linux); empty elsewhere."""
    out: list[int] = []
    task_dir = Path(f"/proc/{pid}/task")
    try:
        for t in task_dir.iterdir():
            try:
                out += [int(x) for x in (t / "children").read_text().split()]
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return out


def descendants(pid: int, limit: int = 500) -> set[int]:
    seen, todo = set(), [pid]
    while todo and len(seen) < limit:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        todo.extend(children_of(p))
    return seen


def listening() -> dict[int, set[int]]:
    """pid -> {ports} for TCP listeners on loopback/any address (ss -ltnpH)."""
    exe = shutil.which("ss")
    if not exe:
        return {}
    try:
        cp = subprocess.run([exe, "-ltnpH"], capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return {}
    out: dict[int, set[int]] = {}
    for line in cp.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        addr = parts[3]
        try:
            port = int(addr.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        for m in SS_RE.finditer(line):
            out.setdefault(int(m.group(1)), set()).add(port)
    return out


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
    for pid, ps in listening().items():
        if pid in tree:
            ports |= ps
    infra = infra_ports()
    return sorted(p for p in ports if p not in infra)


def public_host() -> str:
    return urlsplit(settings.public_url).hostname or "" if settings.public_url else ""


def serve_cmd(*args: str) -> list[str]:
    """tailscale as the operator if allowed (always so in the container), else through the sudoers rule installed by install.sh."""
    exe = shutil.which("tailscale") or "/usr/bin/tailscale"
    return [exe, "serve", *args]


def _denied(cp: subprocess.CompletedProcess) -> bool:
    text = (cp.stderr + cp.stdout).lower()
    return "denied" in text or "permission" in text or "operator" in text


def _operator_hint() -> str:
    # In the container the uid-1000 user is named differently than on the box; the host home is mounted at its own path.
    user = Path.home().name or "<user>"
    return f"tailscale serve failed; on the box run: sudo tailscale set --operator={user}"


def _run_serve(args: list[str]) -> None:
    cmd = serve_cmd(*args)
    docker = settings.runtime == "docker"
    if docker and not TAILSCALE_SOCK.exists():
        raise PreviewError(f"tailscaled socket {TAILSCALE_SOCK} is not mounted; is tailscale running on the box?")
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if cp.returncode != 0 and not docker and "denied" in (cp.stderr + cp.stdout).lower():
            cp = subprocess.run(["sudo", "-n", *cmd], capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise PreviewError(f"tailscale serve failed: {e.__class__.__name__}")
    if cp.returncode != 0:
        if docker and _denied(cp):      # no sudo inside the container: the operator flag, set once on the box, is the way
            raise PreviewError(_operator_hint())
        raise PreviewError((cp.stderr or cp.stdout).strip()[-300:] or "tailscale serve failed")


def serve_on(https_port: int, local_port: int) -> str:
    _run_serve(["--bg", f"--https={https_port}", f"http://127.0.0.1:{local_port}"])
    return f"https://{public_host()}:{https_port}/"


def serve_off(https_port: int) -> None:
    try:
        _run_serve([f"--https={https_port}", "--yes", "off"])
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
