"""Issue #100: launch-type routes are inert in dev mode unless the board is sandboxed.

Under the dev auth bypass (CCBOARD_DEV_BYPASS_USER) any local process, a QA browser agent included, can call the routes that start real
agent processes. They refuse with 409 until the data, projects, Claude config and Codex home directories are all temp paths
(settings.dev_sandboxed()). Without the dev bypass nothing changes: production never takes this branch.

The text names the settings, never their values, so no home path reaches a log or a screenshot.
"""
from __future__ import annotations

from . import projects
from .config import settings

REFUSAL = ("dev mode: set the four directories to temp paths "
           "(CCBOARD_DATA_DIR, PROJECTS_DIR, CLAUDE_CONFIG_DIR, CODEX_HOME) before starting an agent")


def launch_ok() -> bool:
    return not settings.dev_bypass_user or settings.dev_sandboxed()


def require_real_launch_ok() -> None:
    """Raise projects.Conflict (the board's 409) when this is a dev board that could reach the real home. No-op in production."""
    if not launch_ok():
        raise projects.Conflict(REFUSAL)
