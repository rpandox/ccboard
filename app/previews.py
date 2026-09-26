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


def ports_under(pane_pid: int) -> list[int]:
    """Listening ports of any process under the session's pane (dev servers Claude started)."""
    if not pane_pid:
        return []
    tree = descendants(pane_pid)
    ports: set[int] = set()
    for pid, ps in listening().items():
        if pid in tree:
            ports |= ps
    return sorted(p for p in ports if p not in (settings.port, settings.ttyd_port, settings.code_server_port))


def public_host() -> str:
    return urlsplit(settings.public_url).hostname or "" if settings.public_url else ""


def serve_cmd(*args: str) -> list[str]:
    """tailscale as the operator if allowed, else through the sudoers rule installed by install.sh."""
    exe = shutil.which("tailscale") or "/usr/bin/tailscale"
    return [exe, "serve", *args]


def _run_serve(args: list[str]) -> None:
    cmd = serve_cmd(*args)
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if cp.returncode != 0 and "denied" in (cp.stderr + cp.stdout).lower():
            cp = subprocess.run(["sudo", "-n", *cmd], capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise PreviewError(f"tailscale serve failed: {e.__class__.__name__}")
    if cp.returncode != 0:
        raise PreviewError((cp.stderr or cp.stdout).strip()[-300:] or "tailscale serve failed")


def serve_on(https_port: int, local_port: int) -> str:
    _run_serve(["--bg", f"--https={https_port}", f"http://127.0.0.1:{local_port}"])
    return f"https://{public_host()}:{https_port}/"


def serve_off(https_port: int) -> None:
    try:
        _run_serve([f"--https={https_port}", "--yes", "off"])
    except PreviewError as e:
        log.info("serve off %s: %s", https_port, e)


def allocate_https_port(used: set[int]) -> int:
    reserved = {settings.ccboard_https_port, settings.code_https_port}
    p = settings.preview_https_base
    while p in used or p in reserved:
        p += 1
        if p > settings.preview_https_base + 200:
            raise PreviewError("no free preview port")
    return p
