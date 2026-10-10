import contextlib
import os
import pathlib
import shutil
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# The board reads a settings file at start (app/config.py apply_env_file, issue #117); an empty CCBOARD_ENV_FILE switches that off, so the
# module-level `settings` of a test run never picks up a real <data dir>/env from the machine the suite runs on.
os.environ.setdefault("CCBOARD_ENV_FILE", "")


# ---- the real-home canary (issue #101) --------------------------------------------------------------------------------------------
# The real home is taken once, at import, before any fixture moves HOME. CCBOARD_GUARD_REAL_HOME lets tests/test_home_guard.py run this
# very file against a fake "real" home in a subprocess.
REAL_HOME = pathlib.Path(os.environ.get("CCBOARD_GUARD_REAL_HOME") or os.path.expanduser("~"))
GUARDED = (".claude/settings.json", ".claude/settings.local.json", ".claude/.credentials.json", ".codex/config.toml",
           ".codex/hooks.json", ".codex/auth.json")
# Written all the time by the owner's own running tools (a live Claude Code session rewrites ~/.claude.json, the claude-mem worker
# touches ~/.claude-mem), so size/mtime would fail every run made beside them: only their existence is compared.
LIVE = (".claude.json", ".claude-mem")


def home_snapshot(home=None):
    """(exists, size, mtime_ns) of each guarded path under the real home. Stat only: no file is ever opened or read."""
    snap = {}
    for rel in GUARDED + LIVE:
        try:
            st = os.stat(pathlib.Path(home or REAL_HOME) / rel)
            snap[rel] = (True, 0, 0) if rel in LIVE else (True, st.st_size, st.st_mtime_ns)
        except OSError:
            snap[rel] = (False, 0, 0)
    return snap


def home_changes(before, after):
    """The guarded paths (relative to home, never absolute) whose stat differs."""
    return [rel for rel in GUARDED + LIVE if before[rel] != after[rel]]


def pytest_configure(config):
    config.addinivalue_line("markers", "real_home(reason): the test legitimately reads the real home, so it keeps HOME; give the reason")


# ---- platform markers (issue #123) --------------------------------------------------------------------------------------------------
# pytest.ini registers the markers (and --strict-markers makes a typo an error); this hook turns each into a skip with the reason in the
# report. Flags are read when the hook runs, so a test can patch app.platform and ask platform_skip_reason() again.
def platform_skip_reason(marks) -> str | None:
    """Why a test carrying the marker names in `marks` cannot run on this host, or None when it can."""
    from app import platform
    if "linux_only" in marks and not platform.IS_LINUX:
        return f"linux_only: needs a Linux host (this is {sys.platform})"
    if "needs_macos" in marks and not platform.IS_MACOS:
        return f"needs_macos: needs a macOS host (this is {sys.platform})"
    if "needs_systemd" in marks:
        if not platform.IS_LINUX:
            return f"needs_systemd: needs a Linux host running systemd (this is {sys.platform})"
        if not (shutil.which("systemctl") and os.path.isdir("/run/systemd/system")):
            return "needs_systemd: systemd is not running on this host"
    if "needs_proc" in marks and not (platform.PROC_ROOT / "self").exists():
        return f"needs_proc: {platform.PROC_ROOT} is not readable on this host"
    if "needs_tmux" in marks and not shutil.which("tmux"):
        return "needs_tmux: tmux is not on PATH"
    if "posix_sh" in marks and (platform.IS_WINDOWS or not shutil.which("sh")):
        return "posix_sh: no POSIX sh on this host"
    return None


def pytest_collection_modifyitems(config, items):
    for item in items:
        reason = platform_skip_reason({m.name for m in item.iter_markers()})
        if reason:
            item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(scope="session", autouse=True)
def _real_home_canary():
    """Fail the run, naming the file, if any test or code under test changed the real home's Claude or Codex configuration."""
    before = home_snapshot()
    yield
    changed = home_changes(before, home_snapshot())
    if changed:
        pytest.fail("a test touched the real home: " + ", ".join(f"~/{rel}" for rel in changed)
                    + " changed during the run (stat size/mtime; the content was never read)", pytrace=False)


@pytest.fixture(autouse=True)
def _sandbox_home(request, tmp_path_factory, monkeypatch):
    """HOME and USERPROFILE point at a fresh temp directory, so a stray Path.home() or ~ lands in the sandbox. Opt out with
    @pytest.mark.real_home("reason")."""
    if request.node.get_closest_marker("real_home"):
        yield
        return
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    yield


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


@pytest.fixture(autouse=True)
def _quiet_box(monkeypatch):
    """The box is never busy unless a test says so: a loaded CI runner's real load average must not turn on the board's back-off
    (#31: reused git answers, skipped cost and indexer passes). tests/test_health.py keeps a reference to the real under_load."""
    from app import health
    monkeypatch.setattr(health, "under_load", lambda: False)


@pytest.fixture(autouse=True)
def _no_real_tailscale(monkeypatch):
    """The node id and name read `tailscale status --json` through one seam (app/nodes.py _ts_status): no test runs the real command, and each test
    starts with no id, no Tailscale reading and an empty hello limiter. tests/test_nodes_card.py patches the seam with a fake reading."""
    from app import nodes
    monkeypatch.setattr(nodes, "_ts_status", lambda: None)
    nodes.reset()
    yield
    nodes.reset()


@pytest.fixture(autouse=True)
def _stub_clone_dns(monkeypatch):
    """The real resolver is never used: every clone host name 'resolves' to one public address (the clone URL guard resolves names,
    issue #22), and the answer cache starts empty. tests/test_clone_url_guard.py patches projects.getaddrinfo with its own tables."""
    import socket
    from app import projects
    monkeypatch.setattr(projects, "getaddrinfo",
                        lambda host, port=None, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port or 0))])
    projects.dns_cache_clear()


@pytest.fixture
def mem_home(tmp_path, monkeypatch):
    """claude-mem's data dir, Claude's config dir and /proc all point at temp dirs; no CCBOARD_MEM_PORT; the default port is a dead one.
    Not autouse: the memory test modules opt in (a module-level autouse wrapper), the rest of the suite is untouched."""
    from app import memory
    from app.config import settings
    from tests.mem_fake import closed_port
    d = tmp_path / "claude-mem"
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "claude_mem_dir", d)
    monkeypatch.setattr(settings, "mem_port", None)
    monkeypatch.setattr(settings, "claude_mem", True)
    monkeypatch.setattr(settings, "claude_config_dir", tmp_path / "claude")
    from tests.proc_fake import use_fake_proc
    use_fake_proc(monkeypatch, tmp_path / "proc")
    monkeypatch.setattr(memory, "DEFAULT_PORT", closed_port())     # a dev box may run a real worker on 37701
    return d


@pytest.fixture
def mem_worker(mem_home):
    """The fake claude-mem worker (tests/mem_fake.py) on a loopback port, named by worker.pid in the temp claude-mem home."""
    import json
    from tests.mem_fake import MemWorker
    w = MemWorker()
    (mem_home / "worker.pid").write_text(json.dumps({"pid": os.getpid(), "port": w.port, "startedAt": "2026-10-04T00:00:00Z", "startToken": "t"}))
    yield w
    w.stop()


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
        elif text.startswith("codex"):
            store["sessions"][name]["command"] = "codex"

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


@pytest.fixture(autouse=True)
def _ci_like_no_claude(monkeypatch):
    """CCBOARD_TEST_NO_CLAUDE=1 makes the suite run like CI's runner, which has no `claude` binary: settings.claude_bin() answers None
    unless a test fakes it itself (a test-level monkeypatch runs after this autouse one and wins). The local full suite is run with it
    before every push (memory: feedback-run-tests-before-push), so a session-creating test that forgot the fake fails here, not on CI."""
    import os
    if os.environ.get("CCBOARD_TEST_NO_CLAUDE") == "1":
        from app.config import settings
        monkeypatch.setattr(settings, "claude_bin", lambda: None)
    yield


@pytest.fixture(autouse=True)
def _no_login_shell_lookup(monkeypatch):
    """platform.resolve_bin never starts the real login shell (it would run the machine owner's profile and find the owner's real tools):
    the lookup answers None unless a test patches platform._login_shell_lookup itself (tests/test_macos_board.py does)."""
    from app import platform
    monkeypatch.setattr(platform, "_login_shell_lookup", lambda name: None)
    platform._login_lookups.clear()
    yield
    platform._login_lookups.clear()


@pytest.fixture(autouse=True)
def _saved_logins_off_by_default(monkeypatch):
    """Saved logins (app/account_store.py) exist on Linux only, so what the suite does must not depend on the platform it runs on: they are
    off here, a test that is about them turns them on (monkeypatch account_store.supported) and sandboxes settings.data_dir and
    settings.claude_config_dir itself. The module's login state, finalize threads and accounts.hold are reset around every test."""
    from app import account_store, accounts
    monkeypatch.setattr(account_store, "supported", lambda: False)
    monkeypatch.setattr(account_store, "support_reason", lambda: None)       # issue #119: the refusal text is then the generic REASON on every host, not this host's own
    monkeypatch.setattr(accounts, "_hold", None)
    account_store._reset_login()
    account_store._set_result(None, 0.0)
    yield
    account_store.stop_watchers()
    account_store._reset_login()
    account_store._set_result(None, 0.0)


@pytest.fixture(autouse=True)
def _codex_accounts_reset():
    """The Codex accounts module's login state, watcher threads and learning clock are reset around every test (it is on only where a codex
    binary exists, and no test has one unless it fakes it: _isolate_codex)."""
    from app import codex_accounts
    codex_accounts._reset_login()
    codex_accounts._set_result(None, 0.0)
    codex_accounts._learn_at = 0.0
    yield
    codex_accounts.stop_watchers()
    codex_accounts._reset_login()
    codex_accounts._set_result(None, 0.0)
    codex_accounts._learn_at = 0.0


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    """A temp CODEX_HOME: settings.codex_home and the CODEX_HOME env both point at it, so nothing a test does (hooks.json, config.toml,
    sessions/, state_*.sqlite) can reach the real ~/.codex. Every test gets one (see _isolate_codex); ask for it to read or fill it."""
    from app.config import settings
    home = tmp_path / "codex"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "codex_home", home)
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _isolate_codex(codex_home, monkeypatch):
    """No test runs a real codex or reads the real ~/.codex: settings.claude_bin's twin answers None (like CI's runner, which has no
    codex) until a test fakes the binary itself (fake_codex), the codex home is the temp one, and the adapter's probe caches start
    and end empty. A test-level monkeypatch runs after this autouse one and wins."""
    from app.agents import codex
    from app.config import settings
    monkeypatch.setattr(settings, "codex_bin", lambda: None)
    monkeypatch.setattr(settings, "codex_hook_trust", "review")
    monkeypatch.delenv("CCBOARD_REMOTE_APPROVE", raising=False)      # the adapter's required-events rule reads it: unset means remote approve on
    codex.reset_caches()
    yield
    codex.reset_caches()


FAKE_CODEX_SCRIPT = """#!/bin/sh
# a stand-in for codex-cli 0.145.0: answers the probes the adapter and the doctor make, nothing else. With device_auth=True (write_fake_codex)
# it is a 0.160: `login --help` lists --device-auth, and `login --device-auth` prints a link and a one-time code and writes a fake login
# file into $CODEX_HOME (the tests run it by hand; the board's own tests never start it: the fake tmux does not run what is typed).
case "$1" in
  --version) echo "codex-cli {version}" ;;
  --help) cat "{help}" ;;
  login)
    case "$2" in
      status) echo "{login}"; [ "{login}" != "Not logged in" ] ;;
      --help) [ -n "{login_help}" ] && cat "{login_help}"; exit 0 ;;
      --device-auth)
        echo "Welcome to Codex [v{version}]"
        echo ""
        echo "Follow these steps to sign in with ChatGPT using device code authorization:"
        echo ""
        echo "1. Open this link in your browser and sign in to your account"
        echo "   {device_url}"
        echo ""
        echo "2. Enter this one-time code (expires in 15 minutes)"
        echo "   {device_code}"
        echo ""
        mkdir -p "${{CODEX_HOME:?CODEX_HOME is not set}}"
        printf '%s' '{auth_blob}' > "$CODEX_HOME/auth.json"
        chmod 600 "$CODEX_HOME/auth.json"
        echo "Successfully logged in" ;;
      *) exit 0 ;;
    esac ;;
  features) printf 'hooks    stable    true\nmulti_agent    stable    true\n' ;;
  mcp) [ "$2" = "get" ] && exit {mcp_rc}; exit 0 ;;
  debug) echo '{{"models": []}}'; exit 1 ;;
  exec) cat "{exec_help}" ;;
  *) exit 0 ;;
esac
"""


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    """A fake `codex` binary that behaves like 0.145.0 for the probes (--version, --help from tests/fixtures/codex_help_0145_real.txt,
    login status, features list, mcp get). Sets settings.codex_bin to it and returns its path; write_fake_codex(path, login=...,
    mcp_rc=..., help_name=..., device_auth=True) rewrites it for another behaviour (a logged-out codex, a newer --help, a 0.160 that has
    `login --device-auth`)."""
    from app.agents import codex
    from app.config import settings
    path = write_fake_codex(tmp_path / "fakebin" / "codex")
    monkeypatch.setattr(settings, "codex_bin", lambda: str(path))
    codex.reset_caches()
    return path


FAKE_AUTH_BLOB = '{"tokens":{"access_token":"SECRET-FAKE-LOGIN","refresh_token":"SECRET-FAKE-REFRESH"}}'


def write_fake_codex(path, *, login="Logged in using ChatGPT", mcp_rc=0, help_name="codex_help_0145_real.txt",
                     exec_help_name="codex_exec_help_0145_real.txt", device_auth=False, device_code="ABCD-12345",
                     device_url="https://auth.openai.com/codex/device", auth_blob=FAKE_AUTH_BLOB):
    """Write an executable fake codex at `path` (see fake_codex) and return it. device_auth=True makes it a 0.160 (see FAKE_CODEX_SCRIPT);
    its login prints device_url and device_code and writes auth_blob into $CODEX_HOME/auth.json."""
    fx = ROOT / "tests" / "fixtures"
    path.parent.mkdir(parents=True, exist_ok=True)
    login_help = fx / ("codex_login_help_0160.txt" if device_auth else "codex_login_help_0145.txt")
    path.write_text(FAKE_CODEX_SCRIPT.format(help=fx / help_name, exec_help=fx / exec_help_name, login=login, mcp_rc=mcp_rc,
                                             version="0.160.0" if device_auth else "0.145.0", login_help=login_help,
                                             device_url=device_url, device_code=device_code, auth_blob=auth_blob))
    path.chmod(0o755)
    return path


# ---- two boards in one process (nodes epic, issue #135) -----------------------------------------------------------------------------
class FlakyTransport:
    """Wraps a peer transport (nodes.peer_transport) and fails the first `fail_first` calls it sees with `error` before it lets calls through; a
    `match` substring limits it to paths holding that text. Counts every call it saw (`seen`) and every failure it made (`failed`). Later phases use it
    for the offline and slow-peer cases; it never sleeps."""

    def __init__(self, inner, fail_first=0, error=ConnectionRefusedError, match=None):
        self.inner, self.fail_first, self.error, self.match = inner, fail_first, error, match
        self.seen = 0
        self.failed = 0

    def __call__(self, target, method, path, headers, body, timeout):
        if self.match is None or self.match in path:
            self.seen += 1
            if self.failed < self.fail_first:
                self.failed += 1
                raise self.error("flaky transport: planned failure")
        return self.inner(target, method, path, headers, body, timeout)


class Board:
    """One ccboard in the process. app.main, settings, the hook token and the notify DB are process-wide, so a Board owns the pieces that differ
    (data dir, SQLite file, public address, node name, hook token) and `enter()` swaps them in for the length of a `with`, then puts the previous ones
    back. Calls are strictly one after another (a request and the peer call it makes nest, they never overlap), so nothing is locked."""

    def __init__(self, parent, name, url, data_dir):
        from app import hooks
        from app.db import DB
        self.parent, self.name, self.url, self.data_dir = parent, name, url, data_dir
        self.db_path = data_dir / "ccboard.db"
        self.hub_token = ""
        self.hook_token = ""
        self.db = DB(self.db_path)
        with self.enter():
            self.hook_token = hooks.ensure_token()

    @contextlib.contextmanager
    def enter(self):
        from app import hooks, main, notify
        from app.config import settings
        names = ("data_dir", "db_path", "public_url", "node_name", "hub_token")
        saved = ({n: getattr(settings, n) for n in names}, main.db, hooks._token, notify._db)
        settings.data_dir, settings.db_path, settings.public_url = self.data_dir, self.db_path, self.url
        settings.node_name, settings.hub_token = self.name, self.hub_token
        main.db, hooks._token = self.db, self.hook_token or None
        notify.set_db(self.db)
        try:
            yield self
        finally:
            for n, v in saved[0].items():
                setattr(settings, n, v)
            main.db, hooks._token = saved[1], saved[2]
            notify.set_db(saved[3])

    def call(self, method, path, *, owner=True, headers=None, **kw):
        """A request to this board's app. `owner=True` carries the owner's identity header and X-CCBoard (a browser on this board); False sends
        only `headers`, the way a node's server process arrives on a tagged tailnet (no identity)."""
        h = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"} if owner else {}
        h.update(headers or {})
        with self.enter():
            return self.parent.client.request(method, path, headers=h, **kw)

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, **kw):
        return self.call("POST", path, **kw)

    def delete(self, path, **kw):
        return self.call("DELETE", path, **kw)


class TwoNodes:
    """Two boards, `a` and `b`, with separate data dirs and databases, joined by an in-process transport: an outgoing call of nodes.PeerClient or
    nodes.peer_call goes to the board whose address it names (https://100.64.0.1 is a, https://100.64.0.2 is b) through the app's test client, with
    no socket. `identity` is the header an owner's server process would carry in user-owned mode (None = a tagged node, no identity). `offline` holds
    the names of boards that refuse connections. `log` has one (caller, method, board, path, status) per call and no header, so no token.
    The limiters in app/nodes.py (pair attempts, per-pair buckets) are process-wide, so the two boards share them, and a request made through the test
    client comes from the address "testclient": a test that redeems more than 20 codes in a minute meets a 429 that a real tailnet would not."""

    def __init__(self, tmp_path):
        from fastapi.testclient import TestClient
        from app import main
        self.client = TestClient(main.app)
        self.identity = None
        self.offline: set[str] = set()
        self.log: list[tuple] = []
        self.a = Board(self, "node-a", "https://100.64.0.1", tmp_path / "node-a")
        self.b = Board(self, "node-b", "https://100.64.0.2", tmp_path / "node-b")
        self.boards = {(b.url.split("//")[1], 443): b for b in (self.a, self.b)}

    def transport(self, target, method, path, headers, body, timeout):
        from app.config import settings
        board = self.boards.get((target.host, target.port))
        if board is None or board.name in self.offline:
            raise ConnectionRefusedError("no board there")
        caller = settings.node_name or "?"
        h = dict(headers)
        if self.identity:
            h.setdefault("Tailscale-User-Login", self.identity)
        with board.enter():
            r = self.client.request(method, path, headers=h, content=body)
        self.log.append((caller, method, board.name, path, r.status_code))
        return r.status_code, dict(r.headers), r.content


@pytest.fixture
def two_nodes(tmp_path, monkeypatch, projects_dir):
    """Two in-process boards (`two_nodes.a`, `two_nodes.b`), each with its own data dir and database, and nodes.peer_transport routing outgoing
    calls between them. Run board code inside `with board.enter():` or through `board.get/post/delete`. Wrap `two_nodes.transport` in a
    FlakyTransport and set nodes.peer_transport to it for failure cases."""
    from app import main, nodes
    monkeypatch.setattr(main, "sched", None)
    monkeypatch.setattr(main, "indexer", None)
    saved_db = main.db
    t = TwoNodes(tmp_path)
    monkeypatch.setattr(nodes, "peer_transport", t.transport)
    yield t
    main.db = saved_db
    t.a.db.conn.close()
    t.b.db.conn.close()
