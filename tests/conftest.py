import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def projects_dir(tmp_path, monkeypatch):
    """Point settings at a temp PROJECTS_DIR and data dir; no dev bypass unless a test sets it."""
    from app.config import settings
    pdir = tmp_path / "projects"
    pdir.mkdir()
    monkeypatch.setattr(settings, "projects_dir", pdir)
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "db_path", tmp_path / "data" / "ccboard.db")
    monkeypatch.setattr(settings, "dev_bypass_user", None)
    monkeypatch.setattr(settings, "allowed_users", {"alice@example.com"})
    return pdir


@pytest.fixture
def client(projects_dir, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def fake_tmux(monkeypatch):
    """Replace tmux calls with an in-memory fake that records commands."""
    from app import tmux
    store = {"sessions": {}, "sent": [], "created": []}

    def list_sessions():
        return {n: dict(s) for n, s in store["sessions"].items()}

    def has_session(name):
        return name in store["sessions"]

    def new_session(name, cwd, env=None, width=220, height=50):
        store["sessions"][name] = {"created": 1, "attached": 0, "windows": 1, "pane_id": "%1",
                                   "command": "zsh", "path": cwd, "pid": 1, "env": env or {}}
        store["created"].append((name, cwd, env or {}))
        return name

    def send_line(name, text, pause=0.0):
        store["sent"].append((name, text))
        if text.startswith("claude"):
            store["sessions"][name]["command"] = "claude"

    def kill_session(name):
        return store["sessions"].pop(name, None) is not None

    def capture(name, lines=200, join=True, escapes=False):
        return store.get("screen", "")

    for fn in (list_sessions, has_session, new_session, send_line, kill_session, capture):
        monkeypatch.setattr(tmux, fn.__name__, fn)
    return store
