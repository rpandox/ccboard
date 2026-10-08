"""app/deploy.py + /api/deploy/*: a Watchtower swap waits while someone is at a terminal (user report: the board 'not running'
for a minute after every Watchtower pull on the loaded box)."""
import os
import stat
import subprocess
from pathlib import Path

import pytest

from app import deploy, hooks
from app.db import DB

ROOT = Path(__file__).resolve().parents[1]
H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


def test_decide_holds_for_terminals_permissions_and_clones_then_caps(db, monkeypatch):
    T = 1_800_000_000
    assert deploy.decide(db, viewers={}, pending=[], at=T) == {"hold": False, "reasons": [], "since": T, "until": T + deploy.HOLD_MAX, "forced": False}
    assert deploy.view(db, T + 1) is not None and deploy.view(db, T + 1)["installing"] is True
    deploy.clear(db)
    r = deploy.decide(db, viewers={"a": {"full": 1, "grid": 2, "ro": 0}}, pending=[], at=T)
    assert r["hold"] is True and r["reasons"] == ["1 terminal open"]
    r = deploy.decide(db, viewers={"a": {"full": 0}}, pending=[{"id": 1}, {"id": 2}], clones={"queued": [{"x": 1}]}, at=T + 300)
    assert r["hold"] is True and r["reasons"] == ["2 permissions pending", "1 clone running"] and r["since"] == T, "since = the first ask"
    v = deploy.view(db, T + 301)
    assert v["pending"] and v["hold"] and v["minutes_left"] == 24 and v["installing"] is False
    r = deploy.decide(db, viewers={"a": {"full": 1}}, pending=[], at=T + deploy.HOLD_MAX + 1)
    assert r["hold"] is False, "30 min after the first ask the update goes ahead whatever is open"
    assert db.kv_get(deploy.KV_PENDING)["value"]["asks"] == 3, "counted since clear()"


def test_install_now_wins_once_and_stale_records_vanish(db):
    T = 1_800_000_000
    deploy.decide(db, viewers={"a": {"full": 1}}, pending=[], at=T)
    deploy.force(db)
    assert deploy.view(db, T + 5)["forced"] is True
    r = deploy.decide(db, viewers={"a": {"full": 1}}, pending=[], at=T + 60)
    assert r["hold"] is False and r["forced"] is True and db.kv_get(deploy.KV_FORCE) is None, "consumed by the go"
    assert deploy.view(db, T + 61)["installing"] is True
    assert deploy.view(db, T + 60 + deploy.STALE + 1) is None, "a swap that never came back is not 'installing' forever"
    deploy.clear(db)
    assert deploy.view(db, T + 62) is None and db.kv_get(deploy.KV_PENDING) is None


def test_gate_route_needs_the_hook_token_and_reads_tmux_viewers(lite_client, fake_tmux, monkeypatch):
    from app import main
    deploy.clear(main.db)
    assert lite_client.post("/api/deploy/gate").status_code == 403
    tok = {"X-CCBoard-Token": hooks.ensure_token()}
    r = lite_client.post("/api/deploy/gate", headers=tok)
    assert r.status_code == 200 and r.json()["hold"] is False
    fake_tmux["sessions"]["shop--api--s1"] = {"created": 1, "attached": 1, "windows": 1, "pane_id": "%1", "command": "claude", "path": "/tmp", "pid": 1, "env": {}}
    fake_tmux["clients"] = [{"session": "shop--api--s1", "flags": {"attached", "focused"}}]
    r = lite_client.post("/api/deploy/gate", headers=tok).json()
    assert r["hold"] is True and r["reasons"] == ["1 terminal open"]
    st = lite_client.get("/api/state", headers=H).json()
    assert st["deploy"]["pending"] is True and st["deploy"]["hold"] is True and st["deploy"]["reasons"] == ["1 terminal open"]
    fake_tmux["clients"] = [{"session": "shop--api--s1", "flags": {"attached", "ignore-size"}}]      # a grid tile is not a typist
    assert lite_client.post("/api/deploy/gate", headers=tok).json()["hold"] is False
    fake_tmux["clients"] = [{"session": "shop--api--s1", "flags": {"attached"}}]
    assert lite_client.post("/api/deploy/gate", headers=tok).json()["hold"] is True
    assert lite_client.post("/api/deploy/now", headers=H).json()["deploy"]["forced"] is True
    assert lite_client.post("/api/deploy/gate", headers=tok).json()["hold"] is False
    assert lite_client.get("/api/state", headers=H).json()["deploy"]["installing"] is True
    deploy.clear(main.db)
    assert lite_client.get("/api/state", headers=H).json()["deploy"] is None


@pytest.mark.posix_sh
def test_gate_script_exit_codes(tmp_path):
    """scripts/ccboard-deploy-gate: 75 on hold, 0 on go, 0 when the board does not answer or the token is missing."""
    script = ROOT / "scripts" / "ccboard-deploy-gate"
    assert script.stat().st_mode & stat.S_IXUSR, "the image runs it by path: it must be executable"
    bindir = tmp_path / "bin"; bindir.mkdir()
    tokfile = tmp_path / "hook-token"; tokfile.write_text("secret\n")

    def run(answer: str, *, rc: int = 0, token: bool = True):
        (bindir / "curl").write_text("#!/bin/sh\n" + (f"printf '%s' '{answer}'\nexit {rc}\n" if rc == 0 else f"exit {rc}\n"))
        (bindir / "curl").chmod(0o755)
        env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "CCBOARD_HOOK_TOKEN_FILE": str(tokfile if token else tmp_path / "none")}
        return subprocess.run(["sh", str(script)], env=env, capture_output=True, text=True)

    assert run('{"hold": true, "reasons": ["1 terminal open"]}').returncode == 75
    assert run('{"hold":true}').returncode == 75
    assert run('{"hold": false, "reasons": []}').returncode == 0
    assert run("", rc=7).returncode == 0, "no board: let the update replace it"
    assert run('{"hold": true}', token=False).returncode == 0, "no token file: never block"
