"""Frozen P1 launch guard from v0.5.114/115 (cx-220/cx-221 sources).
Only scheduler.run_job and the profile guard it calls; execution is supplied by tests.
"""

import json
import re
from pathlib import Path
from app import projects
from app.config import settings
from app.profiles import _contained, _root
PROVIDERS = ("claude", "codex")
ACCOUNT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")

def job_opts(job: dict) -> dict:
    """The agent's own options a job stores as JSON in jobs.opts (Codex: model, reasoning_effort); {} when there are none or the text is bad."""
    raw = job.get("opts")
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}

def run_job(db, job: dict, run_id: int) -> dict:
    from . import profiles
    bound = job.get("account_id") or job_opts(job).get("account_id")
    if bound is None:
        return _run_job(db, job, run_id)
    profiles.require_account(db, job.get("agent") or "claude", bound)
    context = {"account_id": bound, "attempt": None, "db": db, "run_id": run_id}
    summary = {}
    try:
        summary = _run_job(db, job, run_id, binding=context)
        return summary
    finally:
        profiles.finish_attempt(db, context["attempt"], summary.get("status", "error"), summary.get("session_id"))

def require_enabled():
    if not settings.pools:
        raise projects.BadRequest("pools are off")

def validate_account_id(account_id: str) -> str:
    if not isinstance(account_id, str) or not ACCOUNT_ID.fullmatch(account_id):
        raise projects.BadRequest("invalid account id")
    return account_id

def profile_dir(provider: str, account_id: str) -> Path:
    if provider not in PROVIDERS:
        raise projects.BadRequest("invalid profile provider")
    return _contained(_root() / provider / validate_account_id(account_id))

def require_account(db, provider, account_id):
    if account_id is None:
        return
    require_enabled()
    profile_dir(provider, account_id)
    with db.lock:
        row = db.conn.execute("SELECT provider FROM pool_account WHERE id=?", (account_id,)).fetchone()
    if row is None or row[0] != provider:
        raise projects.BadRequest("no such account profile")
