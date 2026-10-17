"""Restart only the supervised board. No session or companion service is stopped."""
from __future__ import annotations

import asyncio
import os
import shlex
import signal

from . import backup, clonequeue, platform as plat, tmux
from .config import settings

RESTART_EXIT_CODE = 75  # systemd Restart=on-failure; never a successful stop
_restart_requested = False


def method() -> str:
    runtime = settings.runtime
    if runtime == "systemd" and plat.is_wsl() and plat.service_manager() == "none":
        return "none"
    if runtime == "docker" and settings._input_env.get("CCBOARD_RESTART_POLICY") != "unless-stopped":
        return "none"  # only the supplied Compose deployment promises to bring this process back
    return runtime if runtime in ("systemd", "docker", "launchd") else "none"


def command(via: str) -> list[str]:
    if via == "launchd":
        return ["launchctl", "kickstart", "-k", plat.launchd_target("board")]
    return []


def info() -> dict:
    via = method()
    hint = ("./install.sh && docker compose up -d --force-recreate ccboard" if settings.runtime == "docker" and via == "none" else
            "docker compose up -d --force-recreate ccboard" if settings.runtime == "docker" else
            "systemctl restart ccboard.service" if via == "systemd" else
            shlex.join(command(via)) if via != "none" else
            ".venv/bin/uvicorn app.main:app --host 127.0.0.1 --port " + str(settings.port))
    why = ("Container restart supervision is not declared. On the host, refresh the installed compose file first: "
           "rerun ./install.sh (which copies deploy/docker-compose.yml), then force-recreate ccboard." if settings.runtime == "docker" else
           "No supervisor runs this board. Stop this board in its shell, then run the command.") if via == "none" else ""
    launch_log = str(settings.data_dir / "restart.log") if via == "launchd" else ""
    return {"launch_log": launch_log, "method": via, "hint": hint, "why": why, "expect_seconds": 10,
            "logs": "docker compose logs --tail 100 ccboard" if settings.runtime == "docker" else
                    "tail -n 100 " + shlex.quote(launch_log) if launch_log else plat.hint("logs", "ccboard"),
            "docker_notice": ("A restart does not reread /etc/ccboard/env; to apply changes made there, "
                              "recreate the container with the command above.") if settings.runtime == "docker" else ""}


def readiness(db) -> list[str]:
    out = []
    n = len(db.perm_pending())
    if n:
        out.append(f"{n} permission{' is' if n == 1 else 's are'} waiting for an answer and will be lost")
    try:
        n = sum(int(v.get("full") or 0) for v in tmux.viewers().values())
    except tmux.TmuxError:
        n = 0
    if n:
        out.append(f"{n} terminal{' is' if n == 1 else 's are'} open; ttyd and tmux sessions keep running")
    if clonequeue.running_clones() or clonequeue.status()["queued"] or clonequeue._inflight:
        out.append("a clone is running")
    if backup.running():
        out.append("a backup is running")
    return out


def finish_shutdown() -> None:
    """Called only after lifespan cleanup, so systemd brings the board back."""
    if _restart_requested:
        os._exit(RESTART_EXIT_CODE)


async def _launch(via: str) -> None:
    """Response background task; every process-control operation lives behind this seam."""
    global _restart_requested
    if via == "systemd":
        _restart_requested = True
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            _restart_requested = False
            raise
    elif via == "docker":
        await asyncio.sleep(1.0)
        os.kill(os.getpid(), signal.SIGTERM)
    elif via == "launchd":
        with (settings.data_dir / "restart.log").open("ab") as logf:
            plat.spawn_detached(["sh", "-c", 'sleep 1; exec "$@"', "ccboard-restart", *command(via)], logf)
