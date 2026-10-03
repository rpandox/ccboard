import contextlib
import os
import pathlib
import sys
import time

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
    """Replace tmux calls with an in-memory fake that records commands.

    store keys (all plain data, safe to read and to set from a test):
      sessions   name -> {created, attached, windows, pane_id, command, path, pid, env[, window_width, window_height | win]};
                 `win: [w, h]` is the same window size as one pair (window_width/height win when both are given)
      sent       send_line calls: (name, text)           created  new_session calls: (name, cwd, env)
      texts      send_text calls: (name, text, enter)    pasted   paste_text calls: (name, text, enter); send_text of
                 multi-line text lands in both
      keys       send_keys calls: (name, [keys])         left_copy  leave_copy_mode calls: [name, ...]
      scrolled   scroll calls: (name, dir, n, agent)     run      raw tmux.run argv tuples that reached the benign run below
      resized    resize_window calls: (name, cols, rows); the real implementation runs over the benign run (so `resize-window` and
                 `set-window-option -u` reach `run`), then the session's window_width/height become the new size, the way tmux would
      clients    [{"session": name, "flags": {"attached", "ignore-size", ...}}]; while it is empty every session's `attached`
                 count is taken as that many full clients, so `attached = 1` alone means one person at the terminal. Set it to
                 describe grid tiles / read-only views (a session with attached=1 and clients=[a grid tile] has no real client)
      clients_error  truthy: clients()/real_clients()/viewers() raise TmuxError (list-clients failed)
      pane       name -> overrides of pane_info's defaults {alt, in_mode, scroll_pos, history, cols, rows, win_cols, win_rows,
                 cmd}; scroll() and leave_copy_mode() update it the way tmux would (up/top enter copy-mode, bottom/exit leave it)
      screen     what capture() returns
    send_text, send_keys and paste_text record and then run the real implementation, whose tmux.run is the benign fake below
    (records argv in store["run"], returns an empty success), so a test that patches tmux.run itself still sees the real argv.
    """
    import subprocess
    from app import tmux
    store = {"sessions": {}, "sent": [], "created": [], "texts": [], "pasted": [], "keys": [], "left_copy": [],
             "scrolled": [], "resized": [], "run": [], "clients": [], "clients_error": None, "pane": {}}

    def run(*args, timeout=5, check=True, input=None):
        store["run"].append(args if input is None else args + (input,))
        return subprocess.CompletedProcess(args, 0, "", "")

    def _win(s):
        """(width, height) of a fake session's window: window_width/height when both are set, else the `win` pair, else (0, 0)."""
        if s.get("window_width") and s.get("window_height"):
            return int(s["window_width"]), int(s["window_height"])
        w = s.get("win")
        return (int(w[0]), int(w[1])) if isinstance(w, (list, tuple)) and len(w) == 2 else (0, 0)

    def list_sessions():
        out = {}
        for n, s in store["sessions"].items():
            d = dict(s)
            d["window_width"], d["window_height"] = _win(s)
            out[n] = d
        return out

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

    # -- clients: the real parsing and classification over store["clients"]
    def clients(name=None):
        if store["clients_error"]:
            raise tmux.TmuxError("list-clients failed")
        cl = store["clients"] or [{"session": n, "flags": {"attached"}}
                                  for n, s in store["sessions"].items() for _ in range(int(s.get("attached") or 0))]
        cl = [{"session": c["session"], "flags": set(c["flags"])} for c in cl]
        return [c for c in cl if c["session"] == name] if name is not None else cl

    def real_clients(name):
        return tmux.count_real(clients(name), name)

    def viewers():
        return tmux.classify_viewers(clients())

    # -- pane state
    def pane_info(name):
        if name not in store["sessions"]:
            raise tmux.TmuxError(f"can't find session: ={name}")
        s = store["sessions"][name]
        info = {"alt": False, "in_mode": False, "scroll_pos": 0, "history": 0, "cols": 80, "rows": 24,
                "win_cols": _win(s)[0] or 80, "win_rows": _win(s)[1] or 24, "cmd": s.get("command") or ""}
        info.update(store["pane"].get(name, {}))
        return info

    def leave_copy_mode(name):
        store["left_copy"].append(name)
        was = pane_info(name)["in_mode"]
        if was:
            store["pane"].setdefault(name, {}).update(in_mode=False, scroll_pos=0)
        return was

    def scroll(name, dir, n=1, agent="claude"):
        store["scrolled"].append((name, dir, n, agent))
        if dir not in tmux.SCROLL_DIRS or isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= tmux.SCROLL_MAX:
            raise ValueError("bad scroll arguments")
        info = pane_info(name)
        if info["alt"] and not info["in_mode"]:
            return {"mode": "app", "alt": True, "pos": info["scroll_pos"]}
        p = store["pane"].setdefault(name, {})
        if dir == "up":
            p.update(in_mode=True, scroll_pos=info["scroll_pos"] + n * info["rows"])
        elif dir == "top":
            p.update(in_mode=True, scroll_pos=max(info["history"], info["scroll_pos"]))
        elif dir == "down" and info["in_mode"]:
            pos = max(0, info["scroll_pos"] - n * info["rows"])
            p.update(in_mode=pos > 0, scroll_pos=pos)
        elif dir in ("bottom", "exit"):
            p.update(in_mode=False, scroll_pos=0)
        after = pane_info(name)
        return {"mode": "copy" if after["in_mode"] else "normal", "alt": after["alt"], "pos": after["scroll_pos"]}

    # -- window size: record, run the real two-call implementation over the benign run, then size the fake window like tmux
    real_resize_window = tmux.resize_window

    def resize_window(name, cols, rows):
        store["resized"].append((name, cols, rows))
        real_resize_window(name, cols, rows)                     # raises the real ValueError outside the allowed range
        if name not in store["sessions"]:
            raise tmux.TmuxError(f"can't find window: ={name}:")
        s = store["sessions"][name]
        s.pop("win", None)
        s.update(window_width=cols, window_height=rows)

    # -- sending: record, then the real code over the benign run
    real_send_text, real_send_keys, real_paste_text = tmux.send_text, tmux.send_keys, tmux.paste_text

    def send_text(name, text, enter=False):
        store["texts"].append((name, text, enter))
        return real_send_text(name, text, enter=enter)

    def send_keys(name, keys):
        if any(k not in tmux.KEY_ALLOW for k in keys):
            return real_send_keys(name, keys)                  # raises the real ValueError
        store["keys"].append((name, list(keys)))
        return real_send_keys(name, keys)

    def paste_text(name, text, enter=True):
        store["pasted"].append((name, text, enter))
        return real_paste_text(name, text, enter=enter)

    for fn in (run, list_sessions, has_session, new_session, send_line, kill_session, capture, clients, real_clients, viewers,
               pane_info, leave_copy_mode, scroll, resize_window, send_text, send_keys, paste_text):
        monkeypatch.setattr(tmux, fn.__name__, fn)
    class NoSleepTime:
        """tmux.py's own view of `time` without sleep: send_text/paste_text pauses never slow a test. A shim on the module attribute,
        because patching time.sleep itself would also turn every other polling loop in the process into a busy loop."""
        def __getattr__(self, attr):
            return getattr(time, attr)

        @staticmethod
        def sleep(*_):
            pass
    monkeypatch.setattr(tmux, "time", NoSleepTime())
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
