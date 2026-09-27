"""Nightly backup: a consistent SQLite snapshot plus the Claude transcripts go into a restic repository, then
every repo under PROJECTS_DIR gets `git push --all origin` so WIP branches survive the box.

Run by ccboard-backup.timer (`python -m app.backup`) or by "Back up now" on the board. The outcome is written
to <data dir>/backup-status.json, which the board shows in the usage strip and the 🔔 panel."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import notify
from .config import settings

log = logging.getLogger("ccboard.backup")
STATUS_FILE = "backup-status.json"
LOCK_FILE = "backup.lock"
LOG_FILE = "backup.log"
TAG = "ccboard"
KEEP = ["--keep-daily", "14", "--keep-weekly", "8", "--keep-monthly", "6"]
PUSH_TIMEOUT = 300
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}   # never hang on a credential prompt; ssh gets BatchMode below
SSH_BATCH = "ssh -oBatchMode=yes -oConnectTimeout=20"
LOCK_RE = re.compile(r"unable to create lock|already locked", re.I)
SYSTEMD_UNIT = Path("/etc/systemd/system/ccboard-backup.service")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def snapshot_db(src: Path, dest: Path) -> Path:
    """Consistent copy of a live (WAL) SQLite file via the backup API; copying the raw file could tear."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    s = sqlite3.connect(str(src))
    d = sqlite3.connect(str(dest))
    try:
        with d:
            s.backup(d)
    finally:
        s.close()
        d.close()
    return dest


# ---------------------------------------------------------------- restic

def restic_enabled() -> bool:
    return settings.restic_repo.lower() not in ("", "off", "none", "0")


def restic_env() -> dict:
    env = dict(os.environ)
    env["RESTIC_REPOSITORY"] = settings.restic_repo
    env["RESTIC_PASSWORD_FILE"] = str(settings.restic_password_file)
    env.pop("RESTIC_PASSWORD", None)
    return env


def _sftp_target(repo: str) -> tuple[str, int | None] | None:
    """'sftp:user@host:/path' or 'sftp://user@host[:port]/path' -> (user@host, port)."""
    if repo.startswith("sftp://"):
        u = urllib.parse.urlsplit(repo)
        if not u.hostname:
            return None
        return ((f"{u.username}@" if u.username else "") + u.hostname, u.port)
    host, sep, _ = repo[len("sftp:"):].partition(":")
    return (host, None) if sep and host and "/" not in host else None


def restic_opts() -> list[str]:
    """For an sftp: repository make restic's own ssh non-interactive too (BatchMode, connect timeout), so a missing
    key or an unreachable host fails fast instead of hanging until the run's timeout and leaving a lock behind."""
    if not settings.restic_repo.startswith("sftp:"):
        return []
    t = _sftp_target(settings.restic_repo)
    if not t:
        return []
    host, port = t
    return ["-o", f"sftp.command={SSH_BATCH}{' -p ' + str(port) if port else ''} {host} -s sftp"]


def _restic(args: list[str], env: dict, timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(["restic", *restic_opts(), *args], env=env, capture_output=True, text=True, timeout=timeout)


def _restic_unlocking(args: list[str], env: dict, timeout: int = 3600) -> subprocess.CompletedProcess:
    """restic never clears the lock of a run that was killed; on a lock error remove stale locks (`restic unlock`
    only drops locks whose process is gone) and retry once."""
    r = _restic(args, env, timeout)
    if r.returncode != 0 and LOCK_RE.search((r.stderr or "") + (r.stdout or "")):
        u = _restic(["unlock"], env, timeout=300)
        log.warning("restic repository was locked; unlock %s", "ok" if u.returncode == 0 else _err(u))
        r = _restic(args, env, timeout)
    return r


def _err(r: subprocess.CompletedProcess) -> str:
    return ((r.stderr or "").strip() or (r.stdout or "").strip() or f"exit {r.returncode}")[-500:]


def parse_summary(stdout: str) -> dict:
    """`restic backup --json` prints one JSON object per line; the last one is the summary."""
    out: dict = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("message_type") == "summary":
            out = {k: m.get(k) for k in ("snapshot_id", "files_new", "files_changed", "files_unmodified",
                                         "data_added", "total_files_processed", "total_bytes_processed", "total_duration")}
    return out


def restic_backup(paths: list[Path]) -> dict:
    if not shutil.which("restic"):
        raise RuntimeError("restic is not installed (rerun install.sh)")
    if not settings.restic_password_file.exists():
        raise RuntimeError(f"no restic password file at {settings.restic_password_file} (rerun install.sh)")
    if not paths:
        raise RuntimeError("nothing to back up")
    env = restic_env()
    if settings.restic_repo.startswith("/"):
        Path(settings.restic_repo).mkdir(parents=True, exist_ok=True)
    if _restic(["cat", "config", "-q"], env, timeout=120).returncode != 0:
        r = _restic(["init", "-q"], env, timeout=300)
        if r.returncode != 0:
            raise RuntimeError("restic init failed: " + _err(r))
        log.info("initialised restic repository %s", settings.restic_repo)
    r = _restic_unlocking(["backup", "--json", "--tag", TAG, "--exclude-caches", *[str(p) for p in paths]], env)
    if r.returncode not in (0, 3):     # 3 = some source files could not be read; the snapshot still exists
        raise RuntimeError("restic backup failed: " + _err(r))
    summary = parse_summary(r.stdout)
    summary["repo"] = settings.restic_repo
    if r.returncode == 3:
        summary["warning"] = "some files could not be read: " + _err(r)
    f = _restic_unlocking(["forget", "--tag", TAG, *KEEP, "--prune", "-q"], env)
    if f.returncode != 0:
        summary["forget_error"] = _err(f)
    return summary


# ---------------------------------------------------------------- git push --all

def repos() -> list[Path]:
    """Main worktrees of every repo (<projects>/<project>/<repo>/.git is a directory). Linked worktrees share the
    ref database, so their branches ride along with the main checkout."""
    out: list[Path] = []
    root = settings.projects_dir
    if not root.is_dir():
        return out
    for proj in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for r in sorted(x for x in proj.iterdir() if x.is_dir() and not x.name.startswith(".")):
            if (r / ".git").is_dir():
                out.append(r)
    return out


def push_all(repo: Path) -> dict:
    rel = str(repo.relative_to(settings.projects_dir)) if repo.is_relative_to(settings.projects_dir) else str(repo)
    env = {**os.environ, **GIT_ENV}
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes -oConnectTimeout=20")
    r = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        return {"repo": rel, "skipped": "no origin"}
    try:
        r = subprocess.run(["git", "-C", str(repo), "push", "--all", "--porcelain", "origin"],
                           capture_output=True, text=True, env=env, timeout=PUSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"repo": rel, "pushed": [], "rejected": [], "error": f"git push timed out after {PUSH_TIMEOUT}s"}
    pushed, rejected = [], []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue                     # "To <url>" / "Done"
        flag, refspec, summary = parts[0], parts[1], parts[2]
        name = refspec.split(":", 1)[0].removeprefix("refs/heads/")
        if flag == "!":
            rejected.append(f"{name}: {summary}")
        elif flag in (" ", "+", "*"):
            pushed.append(name)
    out = {"repo": rel, "pushed": pushed, "rejected": rejected}
    if r.returncode != 0 and not rejected:
        out["error"] = ((r.stderr or "").strip() or "git push failed")[-300:]
    return out


# ---------------------------------------------------------------- run

def status_path() -> Path:
    return settings.data_dir / STATUS_FILE


def write_status(st: dict) -> None:
    p = status_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    os.replace(tmp, p)


def try_lock():
    """Non-blocking exclusive lock on <data dir>/backup.lock; returns the open file (keep it) or None if held."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    f = open(settings.data_dir / LOCK_FILE, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def running() -> bool:
    f = try_lock()
    if f is None:
        return True
    f.close()
    return False


def run(push: bool | None = None, restic: bool | None = None) -> dict:
    """One backup pass. Never raises for a failing step: every problem lands in status['errors']."""
    started = time.time()
    st: dict = {"at": now_iso(), "status": "ok", "restic": None, "push": [], "errors": []}
    lock = try_lock()
    if lock is None:
        raise RuntimeError("a backup is already running")
    do_restic = restic_enabled() if restic is None else restic
    do_push = settings.backup_push if push is None else push
    stage = settings.data_dir / "backup-stage"
    try:
        if do_restic:
            shutil.rmtree(stage, ignore_errors=True)
            stage.mkdir(parents=True)
            paths: list[Path] = []
            try:
                if settings.db_path.exists():
                    paths.append(snapshot_db(settings.db_path, stage / "ccboard.db"))
            except sqlite3.Error as e:
                st["errors"].append(f"db snapshot: {e}")
            transcripts = settings.claude_config_dir / "projects"
            if transcripts.is_dir():
                paths.append(transcripts)
            for extra in settings.backup_extra:
                if Path(extra).exists():
                    paths.append(Path(extra))
            st["paths"] = [str(p) for p in paths]
            try:
                st["restic"] = restic_backup(paths)
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
                st["errors"].append(f"restic: {e}")
        else:
            st["restic"] = {"skipped": "CCBOARD_RESTIC_REPO is off"}
        if do_push:
            for repo in repos():
                try:
                    res = push_all(repo)
                except OSError as e:
                    res = {"repo": str(repo), "pushed": [], "rejected": [], "error": str(e)[:300]}
                st["push"].append(res)
                if res.get("error") or res.get("rejected"):
                    st["errors"].append(f"push {res['repo']}: {res.get('error') or '; '.join(res['rejected'])}")
        else:
            st["push"] = []
            st["push_skipped"] = "CCBOARD_BACKUP_PUSH=0"
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        lock.close()
    st["duration_s"] = round(time.time() - started, 1)
    if st["errors"]:
        st["status"] = "failed"
    write_status(st)
    if st["errors"]:
        notify.publish("ccboard backup failed", "\n".join(st["errors"])[:1500], priority=4, tags=["warning"],
                       click=(settings.public_url + "/") if settings.public_url else None)
    return st


def start_detached() -> str:
    """Start a backup pass outside the request. Through the systemd unit when installed (its own cgroup: a ccboard
    restart cannot kill it mid-run; log in the journal), else a detached process logging to <data dir>/backup.log."""
    if SYSTEMD_UNIT.exists() and shutil.which("systemctl") and shutil.which("sudo"):
        try:
            r = subprocess.run(["sudo", "-n", "systemctl", "start", "--no-block", "ccboard-backup.service"],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            r = None
            log.warning("systemctl start ccboard-backup failed: %s", e)
        if r is not None and r.returncode == 0:
            return "systemd"
        if r is not None:
            log.warning("systemctl start ccboard-backup failed (%s); running in-process instead", (r.stderr or "").strip()[-200:])
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with open(settings.data_dir / LOG_FILE, "ab") as logf:
        subprocess.Popen([sys.executable, "-m", "app.backup"], cwd=str(Path(__file__).resolve().parent.parent),
                         stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    return "process"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        st = run(push=False if "--no-push" in argv else None, restic=False if "--no-restic" in argv else None)
    except RuntimeError as e:
        print(f"backup: {e}", file=sys.stderr)
        return 2
    r = st.get("restic") or {}
    pushed = sum(len(p.get("pushed", [])) for p in st["push"])
    print(f"backup {st['status']} in {st['duration_s']}s: snapshot {r.get('snapshot_id') or r.get('skipped') or '-'}, "
          f"{pushed} branch(es) pushed across {len(st['push'])} repo(s)" + ("; " + "; ".join(st["errors"]) if st["errors"] else ""))
    return 1 if st["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
