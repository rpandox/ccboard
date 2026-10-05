"""Nightly backup: a consistent SQLite snapshot plus the Claude transcripts and the Codex rollouts go into a restic repository, then
every repo under PROJECTS_DIR gets its unpushed work copied to backup branches on origin
(`ccboard-backup/<node>/<branch>`) so WIP survives the box. The backup never pushes to `main` or to any other
branch people work on.

Run by ccboard-backup.timer (`python -m app.backup`) or by "Back up now" on the board. The outcome is written
to <data dir>/backup-status.json, which the board shows in the usage strip and the 🔔 panel."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
import socket
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
FETCH_TIMEOUT = 180
BACKUP_PREFIX = "ccboard-backup"         # refs/heads/ccboard-backup/<node>/<branch> on origin: the only refs the backup ever writes
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


# ---------------------------------------------------------------- backup branches on origin

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


def backup_ns() -> str:
    """The branch namespace this box writes on every origin: ccboard-backup/<node>. One per box, so two boxes
    that share a remote never overwrite each other's copies."""
    node = re.sub(r"[^A-Za-z0-9_-]+", "-", settings.node_name or socket.gethostname().split(".")[0]).strip("-")
    return f"{BACKUP_PREFIX}/{node or 'box'}"


def _git(repo: Path, args: list[str], env: dict, timeout: int = 60) -> subprocess.CompletedProcess:
    # gc.auto=0: a fetch from the nightly run must not start a background gc next to the sessions working in the repo
    return subprocess.run(["git", "-C", str(repo), "-c", "gc.auto=0", *args], capture_output=True, text=True, env=env, timeout=timeout)


def _unpushed(repo: Path, sha: str, env: dict) -> bool:
    """True unless every commit reachable from sha is already on a branch of origin that people work on (the
    backup's own namespaces do not count). Anything that cannot be decided counts as unpushed: when in doubt, copy."""
    try:
        r = _git(repo, ["rev-list", "--count", sha, "--not", f"--exclude=origin/{BACKUP_PREFIX}/*", "--remotes=origin"], env)
    except subprocess.TimeoutExpired:
        return True
    return not (r.returncode == 0 and r.stdout.strip() == "0")


def push_all(repo: Path) -> dict:
    """Copy this repo's unpushed work to origin without touching a branch anyone works on.

    1. `git fetch --prune origin`, so "is it on GitHub already?" is answered from the remote's state now, not from the
       last time somebody fetched (a `main` that is merely behind is then simply up to date: nothing to do).
    2. Every local branch that has commits which are on no real branch of origin is pushed to
       refs/heads/ccboard-backup/<node>/<branch>. That push is forced: the namespace belongs to this box alone and WIP
       gets rebased. `main`, a PR branch, any branch under its own name: never written by the backup.
    3. A backup branch whose commits have since reached a real branch (pushed, merged) is deleted again, so the
       namespace only holds work that exists nowhere else on the remote. A backup branch whose local branch was deleted
       on the box stays (that copy may be the only one left); it goes once its commits are merged.

    Returns {repo, ns, pushed: [branch], cleaned: [branch], rejected: ["branch: reason"], error?} or {repo, skipped}."""
    rel = str(repo.relative_to(settings.projects_dir)) if repo.is_relative_to(settings.projects_dir) else str(repo)
    env = {**os.environ, **GIT_ENV}
    env.setdefault("GIT_SSH_COMMAND", SSH_BATCH)
    if _git(repo, ["remote", "get-url", "origin"], env).returncode != 0:
        return {"repo": rel, "skipped": "no origin"}
    ns = backup_ns()
    out: dict = {"repo": rel, "ns": ns, "pushed": [], "cleaned": [], "rejected": []}
    try:
        f = _git(repo, ["fetch", "--prune", "--no-tags", "--quiet", "origin"], env, timeout=FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {**out, "error": f"git fetch timed out after {FETCH_TIMEOUT}s"}
    if f.returncode != 0:
        return {**out, "error": "git fetch: " + ((f.stderr or "").strip() or "failed")[-300:]}
    specs: list[str] = []
    heads = _git(repo, ["for-each-ref", "--format=%(refname)%09%(objectname)", "refs/heads"], env)
    for line in heads.stdout.splitlines():
        ref, _, sha = line.partition("\t")
        name = ref.removeprefix("refs/heads/")
        if not sha or name.startswith(BACKUP_PREFIX + "/"):
            continue                     # a backup branch someone checked out on the box is not backed up again
        if _unpushed(repo, sha, env):
            specs.append(f"+refs/heads/{name}:refs/heads/{ns}/{name}")
    writing = {x.split(":", 1)[1] for x in specs}
    mine = _git(repo, ["for-each-ref", "--format=%(refname)%09%(objectname)", f"refs/remotes/origin/{ns}/"], env)
    for line in mine.stdout.splitlines():
        ref, _, sha = line.partition("\t")
        dst = "refs/heads/" + ref.removeprefix("refs/remotes/origin/")
        if sha and dst not in writing and not _unpushed(repo, sha, env):
            specs.append(":" + dst)      # its commits are on a real branch now
    if not specs:
        return out
    try:
        r = _git(repo, ["push", "--porcelain", "origin", *specs], env, timeout=PUSH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {**out, "error": f"git push timed out after {PUSH_TIMEOUT}s"}
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue                     # "To <url>" / "Done"
        flag, refspec, summary = parts[0], parts[1], parts[2]
        name = refspec.split(":", 1)[1].removeprefix(f"refs/heads/{ns}/")
        if flag == "!":
            out["rejected"].append(f"{name}: {summary}")
        elif flag == "-":
            out["cleaned"].append(name)
        elif flag in (" ", "+", "*"):
            out["pushed"].append(name)   # "=" (up to date) is neither
    if r.returncode != 0 and not out["rejected"]:
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


def _holds_saved_logins(p: Path) -> bool:
    """Would backing up `p` take the saved logins (<data dir>/accounts: Claude's, app/account_store.py; <data dir>/codex-accounts: Codex's,
    app/codex_accounts.py)? True for the data dir itself, either store dir, anything inside them, and any directory above them (a snapshot of
    the home dir contains them too). The nightly paths are the DB snapshot, the transcripts, the Codex rollouts and CCBOARD_BACKUP_EXTRA: credentials are in none
    of them, whatever CCBOARD_BACKUP_EXTRA says."""
    try:
        target = p.expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    for name in ("accounts", "codex-accounts"):
        store = (settings.data_dir / name).resolve()
        if target == store or store.is_relative_to(target) or target.is_relative_to(store):
            return True
    return False


def run(push: bool | None = None, restic: bool | None = None) -> dict:
    """One backup pass. Never raises for a failing step: every problem lands in status['errors']."""
    started = time.time()
    st: dict = {"at": now_iso(), "status": "ok", "restic": None, "push": [], "errors": [], "warnings": []}
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
            rollouts = settings.codex_home / "sessions"          # only sessions/: CODEX_HOME itself holds auth.json
            if rollouts.is_dir():
                paths.append(rollouts)
            for extra in settings.backup_extra:
                if _holds_saved_logins(Path(extra)):
                    st["warnings"].append(f"skipped {extra}: saved logins are never backed up")
                    continue
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
            st["push_ns"] = backup_ns()
            for repo in repos():
                try:
                    res = push_all(repo)
                except OSError as e:
                    res = {"repo": str(repo), "pushed": [], "rejected": [], "error": str(e)[:300]}
                st["push"].append(res)
                if res.get("error") or res.get("rejected"):
                    # a backup branch the remote refused (a branch rule that also covers ccboard-backup/*, no write access) or a
                    # fetch/push that could not run is a warning: the snapshot is fine, the run is 'partial'. A branch that is
                    # merely behind its remote is not a warning any more: there is nothing of it to copy.
                    st["warnings"].append(f"push {res['repo']}: {res.get('error') or '; '.join(res['rejected'])}")
        else:
            st["push"] = []
            st["push_skipped"] = "CCBOARD_BACKUP_PUSH=0"
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        lock.close()
    st["duration_s"] = round(time.time() - started, 1)
    if st["errors"]:
        st["status"] = "failed"
    elif st["warnings"]:
        st["status"] = "partial"                      # snapshot fine, some backup branches not written: shown on the board, not paged
    write_status(st)
    if st["errors"]:
        notify.publish("ccboard backup failed", "\n".join(st["errors"])[:1500], priority=4, tags=["warning"],
                       click=(settings.public_url + "/") if settings.public_url else None)
    return st


def start_detached() -> str:
    """Start a backup pass outside the request. Through the systemd unit when installed (its own cgroup: a ccboard
    restart cannot kill it mid-run; log in the journal), else a detached process logging to <data dir>/backup.log.
    Inside the container there is no systemd to ask: always the detached process."""
    if settings.runtime != "docker" and SYSTEMD_UNIT.exists() and shutil.which("systemctl") and shutil.which("sudo"):
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
          f"{pushed} branch(es) copied to {st.get('push_ns') or backup_ns()}/ across {len(st['push'])} repo(s)" + ("; " + "; ".join(st["errors"]) if st["errors"] else "")
          + ("; warnings: " + "; ".join(st["warnings"]) if st.get("warnings") else ""))
    return 1 if st["errors"] else 0                   # a partial run exits 0: the unit must not fail every night over a refused backup branch


if __name__ == "__main__":
    sys.exit(main())
