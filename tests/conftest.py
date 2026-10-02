import contextlib
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
    monkeypatch.setattr(settings, "claude_config_dir", tmp_path / "claude")   # keep the transcript indexer off the real ~/.claude
    monkeypatch.setattr(settings, "allowed_users", {"alice@example.com"})
    return pdir


@pytest.fixture(autouse=True)
def _stub_ccusage(monkeypatch):
    """No test shells out to ccusage: usage and cost report 'unavailable' unless a test patches them again."""
    from app import cost, usage
    monkeypatch.setattr(usage, "fetch_block", lambda: {"available": False})
    monkeypatch.setattr(cost, "fetch_sessions", lambda: None)


@pytest.fixture
def client(projects_dir, monkeypatch):
    """The board with its full lifespan (pollers, indexer, scheduler worker, recovery). Slow: use lite_client unless a test needs them."""
    from fastapi.testclient import TestClient
    from app import main
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def lite_client(projects_dir, monkeypatch):
    """The board without background workers: only the lifespan's synchronous setup (settings, DB, hook token, notify DB).

    The TestClient is still entered as a context manager, because permissions._waiters needs the running event loop.
    Tests that exercise the pollers, the transcript indexer, the scheduler or startup recovery use `client` instead.
    """
    from fastapi.testclient import TestClient
    from app import hooks, main, notify
    from app.config import settings
    from app.db import DB

    @contextlib.asynccontextmanager
    async def lite_lifespan(app):
        settings.validate()
        main.db = DB(settings.db_path)
        hooks.ensure_token()
        notify.set_db(main.db)
        yield

    monkeypatch.setattr(main.app.router, "lifespan_context", lite_lifespan)
    monkeypatch.setattr(main, "sched", None)         # a worker left over from a full-lifespan test must not leak into this one
    monkeypatch.setattr(main, "indexer", None)
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


@pytest.fixture(autouse=True)
def _fresh_claude_auth():
    """claude_auth.status() caches by the credentials file's stat, which is (False, 0, 0) under every test's temp
    config dir; a not-logged-in result cached by one test would otherwise leak into the next (seen on CI, where
    no real `claude` masks it)."""
    from app import claude_auth
    claude_auth.invalidate()
    yield
    claude_auth.invalidate()
