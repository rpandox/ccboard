import json
import os
import stat
import subprocess

import pytest

from app import backup
from app.db import DB

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}

FAKE_RESTIC = r'''#!/bin/sh
# fake restic: records every call in the repo dir; `cat config` fails until `init` ran; backup prints a JSON summary
mkdir -p "$RESTIC_REPOSITORY"
printf '%s\n' "$*" >> "$RESTIC_REPOSITORY/calls.log"
[ -n "$RESTIC_PASSWORD_FILE" ] && [ -f "$RESTIC_PASSWORD_FILE" ] || { echo "Fatal: no password file" >&2; exit 1; }
case "$1" in
  cat) [ -f "$RESTIC_REPOSITORY/config" ] || exit 1 ;;
  init) touch "$RESTIC_REPOSITORY/config" ;;
  backup)
    if [ "${FAKE_RESTIC_FAIL:-0}" = 1 ]; then echo "Fatal: boom" >&2; exit 1; fi
    if [ -f "$RESTIC_REPOSITORY/locked" ]; then echo "Fatal: unable to create lock in backend: repository is already locked by PID 123" >&2; exit 1; fi
    echo '{"message_type":"status","percent_done":1}'
    echo '{"message_type":"summary","snapshot_id":"abcdef1234567890","files_new":3,"files_changed":0,"data_added":1024,"total_files_processed":3,"total_bytes_processed":2048,"total_duration":0.5}' ;;
  forget) : ;;
  unlock) rm -f "$RESTIC_REPOSITORY/locked" ;;
esac
exit 0
'''


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@x", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def backup_env(projects_dir, tmp_path, monkeypatch):
    """Fake restic on PATH, a temp restic repo + password file, a DB, transcripts, and repos with/without origin."""
    from app.config import settings
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    fr = bin_dir / "restic"; fr.write_text(FAKE_RESTIC); fr.chmod(fr.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("FAKE_RESTIC_FAIL", raising=False)
    monkeypatch.setattr(settings, "restic_repo", str(tmp_path / "restic-repo"))
    monkeypatch.setattr(settings, "restic_password_file", tmp_path / "restic-password")
    (tmp_path / "restic-password").write_text("pw\n")
    monkeypatch.setattr(settings, "backup_push", True)
    monkeypatch.setattr(settings, "backup_extra", [])
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    DB(settings.db_path).kv_set("hello", {"x": 1})
    tr = settings.claude_config_dir / "projects" / "-srv-projects-shop-api"; tr.mkdir(parents=True)
    (tr / "s.jsonl").write_text('{"type":"user"}\n')
    # repo with an origin (bare) and a linked worktree branch
    origin = tmp_path / "origin.git"; subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    api = projects_dir / "shop" / "api"; api.mkdir(parents=True)
    git(api, "init", "-q", "-b", "main"); git(api, "commit", "-q", "--allow-empty", "-m", "init")
    git(api, "remote", "add", "origin", str(origin))
    git(api, "worktree", "add", "-q", "-b", "worktree-x", str(api / ".claude" / "worktrees" / "x"), "HEAD")
    git(api / ".claude" / "worktrees" / "x", "commit", "-q", "--allow-empty", "-m", "wip")
    # repo without origin
    nor = projects_dir / "shop" / "local"; nor.mkdir()
    git(nor, "init", "-q", "-b", "main"); git(nor, "commit", "-q", "--allow-empty", "-m", "init")
    return {"api": api, "origin": origin, "local": nor, "repo": tmp_path / "restic-repo"}


def test_snapshot_db(tmp_path):
    src = tmp_path / "a.db"
    DB(src).kv_set("k", [1, 2])
    dest = backup.snapshot_db(src, tmp_path / "out" / "a.db")
    assert DB(dest).kv_get("k")["value"] == [1, 2]


def test_parse_summary():
    out = backup.parse_summary('junk\n{"message_type":"status"}\n{"message_type":"summary","snapshot_id":"ab12","files_new":1}\n')
    assert out["snapshot_id"] == "ab12" and out["files_new"] == 1 and "data_added" in out
    assert backup.parse_summary("") == {}


def test_repos_and_push(backup_env, projects_dir):
    api, origin, local = backup_env["api"], backup_env["origin"], backup_env["local"]
    assert backup.repos() == [api, local]          # the linked worktree under api/.claude/worktrees is not listed
    r = backup.push_all(api)
    assert r["repo"] == "shop/api" and sorted(r["pushed"]) == ["main", "worktree-x"] and r["rejected"] == [] and "error" not in r
    assert sorted(git(origin, "branch", "--format=%(refname:short)").split()) == ["main", "worktree-x"]
    assert backup.push_all(api)["pushed"] == []    # up to date
    assert backup.push_all(local) == {"repo": "shop/local", "skipped": "no origin"}
    # origin moves ahead on main -> that branch is rejected, nothing is forced, the other branch still goes
    other = projects_dir.parent / "other"; subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    git(other, "commit", "-q", "--allow-empty", "-m", "remote work"); git(other, "push", "-q", "origin", "main")
    git(api, "commit", "-q", "--allow-empty", "-m", "local work")
    git(api / ".claude" / "worktrees" / "x", "commit", "-q", "--allow-empty", "-m", "more wip")
    r = backup.push_all(api)
    assert r["pushed"] == ["worktree-x"] and len(r["rejected"]) == 1 and r["rejected"][0].startswith("main:") and "error" not in r
    assert "remote work" in git(origin, "log", "-1", "--format=%s", "main")


def test_run_ok_then_failed(backup_env, monkeypatch):
    from app import notify
    from app.config import settings
    sent = []
    monkeypatch.setattr(notify, "publish", lambda title, message, **kw: sent.append((title, message)) or True)
    st = backup.run()
    assert st["status"] == "ok" and st["errors"] == [] and st["restic"]["snapshot_id"].startswith("abcdef")
    assert st["restic"]["repo"] == settings.restic_repo
    assert any(p.endswith("backup-stage/ccboard.db") for p in st["paths"]) and any(p.endswith("projects") for p in st["paths"])
    calls = (backup_env["repo"] / "calls.log").read_text().splitlines()
    assert calls[0].startswith("cat config") and calls[1].startswith("init") and calls[2].startswith("backup --json --tag ccboard")
    assert calls[3].startswith("forget --tag ccboard --keep-daily 14")
    assert [p["repo"] for p in st["push"]] == ["shop/api", "shop/local"] and sorted(st["push"][0]["pushed"]) == ["main", "worktree-x"]
    assert not (settings.data_dir / "backup-stage").exists()          # staging dir cleaned up
    saved = json.loads(backup.status_path().read_text())
    assert saved["status"] == "ok" and saved["at"] == st["at"] and sent == []
    # second run: repo already initialised, nothing new to push
    st2 = backup.run()
    assert st2["status"] == "ok" and st2["push"][0]["pushed"] == []
    assert "init" not in [c.split()[0] for c in (backup_env["repo"] / "calls.log").read_text().splitlines()[4:]]
    # restic failure: recorded, status failed, ntfy warned, push still happened
    monkeypatch.setenv("FAKE_RESTIC_FAIL", "1")
    st3 = backup.run()
    assert st3["status"] == "failed" and st3["errors"] == ["restic: restic backup failed: Fatal: boom"] and len(st3["push"]) == 2
    assert sent and sent[0][0] == "ccboard backup failed" and "boom" in sent[0][1]
    assert json.loads(backup.status_path().read_text())["status"] == "failed"


def test_run_flags_and_lock(backup_env, monkeypatch, capsys):
    from app.config import settings
    monkeypatch.setattr(settings, "backup_push", False)
    st = backup.run()
    assert st["push"] == [] and st["push_skipped"] and st["status"] == "ok"
    monkeypatch.setattr(settings, "restic_repo", "off")
    assert not backup.restic_enabled()
    st = backup.run(push=True)
    assert st["restic"] == {"skipped": "CCBOARD_RESTIC_REPO is off"} and len(st["push"]) == 2 and st["status"] == "ok"
    assert backup.main(["--no-push"]) == 0 and "backup ok" in capsys.readouterr().out
    held = backup.try_lock()
    try:
        assert backup.running()
        with pytest.raises(RuntimeError, match="already running"):
            backup.run()
        assert backup.main([]) == 2
    finally:
        held.close()
    assert not backup.running()


def test_missing_restic_or_password(backup_env, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "restic_password_file", settings.data_dir / "nope")
    st = backup.run(push=False)
    assert st["status"] == "failed" and "no restic password file" in st["errors"][0]
    monkeypatch.setattr(settings, "restic_password_file", backup_env["repo"].parent / "restic-password")
    monkeypatch.setenv("PATH", "/nonexistent")
    st = backup.run(push=False)
    assert st["status"] == "failed" and "restic is not installed" in st["errors"][0]


def test_api_backup_run_and_state(client, backup_env, monkeypatch):
    from app import main
    spawned = []
    class FakePopen:
        def __init__(self, args, **kw):
            spawned.append((args, kw))
    real_popen = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", FakePopen)          # main.py shares the subprocess module
    assert client.post("/api/backup/run", headers={"Tailscale-User-Login": "alice@example.com"}).status_code == 403   # no X-CCBoard
    r = client.post("/api/backup/run", headers=H)
    assert r.status_code == 202 and r.json()["started"] and spawned[0][0][-2:] == ["-m", "app.backup"]
    assert spawned[0][1]["start_new_session"] is True
    held = backup.try_lock()
    try:
        assert client.post("/api/backup/run", headers=H).status_code == 409
    finally:
        held.close()
    monkeypatch.setattr(subprocess, "Popen", real_popen)        # git needs the real one again
    backup.run()
    st = client.get("/api/state", headers=H).json()
    assert st["backup"]["status"] == "ok" and st["backup"]["restic"]["snapshot_id"]
    assert st["config"]["backup"]["restic"] and st["config"]["backup"]["push"] and st["config"]["backup"]["restic_installed"]


def test_restic_stale_lock_is_cleared(backup_env):
    backup_env["repo"].mkdir(exist_ok=True)
    (backup_env["repo"] / "locked").touch()          # a killed earlier run left restic's lock behind
    st = backup.run(push=False)
    assert st["status"] == "ok" and st["restic"]["snapshot_id"]
    calls = [c.split()[0] for c in (backup_env["repo"] / "calls.log").read_text().splitlines()]
    assert calls == ["cat", "init", "backup", "unlock", "backup", "forget"]


def test_restic_opts_sftp(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "restic_repo", "/local/repo")
    assert backup.restic_opts() == []
    monkeypatch.setattr(settings, "restic_repo", "sftp:bk@nas:/srv/restic")
    assert backup.restic_opts() == ["-o", "sftp.command=ssh -oBatchMode=yes -oConnectTimeout=20 bk@nas -s sftp"]
    monkeypatch.setattr(settings, "restic_repo", "sftp://bk@nas:2222/srv/restic")
    assert backup.restic_opts() == ["-o", "sftp.command=ssh -oBatchMode=yes -oConnectTimeout=20 -p 2222 bk@nas -s sftp"]
    monkeypatch.setattr(settings, "restic_repo", "sftp:")
    assert backup.restic_opts() == []


def test_start_detached_prefers_systemd(tmp_path, monkeypatch, projects_dir):
    unit = tmp_path / "ccboard-backup.service"; unit.write_text("[Unit]\n")
    monkeypatch.setattr(backup, "SYSTEMD_UNIT", unit)
    monkeypatch.setattr(backup.shutil, "which", lambda x: f"/usr/bin/{x}")
    calls = []

    class Ok:
        returncode = 0; stderr = ""
    monkeypatch.setattr(backup.subprocess, "run", lambda argv, **kw: calls.append(argv) or Ok())
    assert backup.start_detached() == "systemd"
    assert calls == [["sudo", "-n", "systemctl", "start", "--no-block", "ccboard-backup.service"]]

    class Denied:
        returncode = 1; stderr = "sudo: a password is required"
    monkeypatch.setattr(backup.subprocess, "run", lambda argv, **kw: Denied())
    spawned = []
    monkeypatch.setattr(backup.subprocess, "Popen", lambda argv, **kw: spawned.append(argv))
    assert backup.start_detached() == "process" and spawned[0][-2:] == ["-m", "app.backup"]
