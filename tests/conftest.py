import contextlib
import datetime
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
os.environ.setdefault("CCBOARD_PAGE_FILE", "")


# ---- the real-home canary (issue #101) --------------------------------------------------------------------------------------------
# The real home is taken once, at import, before any fixture moves HOME. CCBOARD_GUARD_REAL_HOME lets tests/test_home_guard.py run this
# very file against a fake "real" home in a subprocess.
REAL_HOME = pathlib.Path(os.environ.get("CCBOARD_GUARD_REAL_HOME") or os.path.expanduser("~"))
GUARDED = (".claude/settings.json", ".claude/settings.local.json", ".claude/.credentials.json", ".codex/config.toml",
           ".codex/hooks.json", ".codex/auth.json")
# Written all the time by the owner's own running tools (a live Claude Code session rewrites ~/.claude.json, the claude-mem worker
# touches ~/.claude-mem), so size/mtime would fail every run made beside them: only their existence is compared.
LIVE = (".claude.json", ".claude-mem")
# Issue #235: the ccboard install on the same machine (its auto-update restarts the board, and the hook installer rewrites these two files)
# touches an empty `.ccboard-hook-sync` beside each file it writes (scripts/claude_settings.py, scripts/codex_hooks.py). A change to one of
# these two files that lands within SYNC_WINDOW_NS of a stamp that moved during the run is that install, not a test. Stat only, as ever.
HOOK_STAMP = {".claude/settings.json": ".claude/.ccboard-hook-sync", ".codex/hooks.json": ".codex/.ccboard-hook-sync"}
SYNC_WINDOW_NS = 15 * 1_000_000_000
SYNC_NOTES: list[str] = []          # one line per change explained by the install; printed in the terminal summary


def home_snapshot(home=None):
    """(exists, size, mtime_ns) of each guarded path under the real home. Stat only: no file is ever opened or read."""
    snap = {}
    for rel in GUARDED + LIVE + tuple(HOOK_STAMP.values()):
        try:
            st = os.stat(pathlib.Path(home or REAL_HOME) / rel)
            snap[rel] = (True, 0, 0) if rel in LIVE else (True, st.st_size, st.st_mtime_ns)
        except OSError:
            snap[rel] = (False, 0, 0)
    return snap


def home_changes(before, after):
    """The guarded paths (relative to home, never absolute) whose stat differs."""
    return [rel for rel in GUARDED + LIVE if before[rel] != after[rel]]


def split_changes(before, after, window_ns=SYNC_WINDOW_NS):
    """(touched, synced): the changed guarded paths, split. `synced` holds (rel, stamp_mtime_ns) for a hook file whose stamp moved during the run
    and whose new mtime is within window_ns of the stamp's new mtime (the ccboard install synced its hooks); `touched` is everything else, which
    fails the run as before. Pure: it only compares the two snapshots."""
    touched, synced = [], []
    for rel in home_changes(before, after):
        stamp = HOOK_STAMP.get(rel)
        if (stamp and after[rel][0] and after[stamp][0] and before[stamp] != after[stamp]
                and abs(after[rel][2] - after[stamp][2]) <= window_ns):
            synced.append((rel, after[stamp][2]))
        else:
            touched.append(rel)
    return touched, synced


def pytest_configure(config):
    config.addinivalue_line("markers", "real_home(reason): the test legitimately reads the real home, so it keeps HOME; give the reason")
    config.addinivalue_line("markers", "real_tool(*names): the test deliberately runs these machine tools (names from MACHINE_TOOLS, e.g. "
                                       "real_tool(\"docker\")) for real; the hermetic guard (issue #291) lets only those names through for this test")


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
    changed, synced = split_changes(before, home_snapshot())
    for rel, stamp_ns in synced:
        when = datetime.datetime.fromtimestamp(stamp_ns / 1e9).astimezone().isoformat(timespec="seconds")
        SYNC_NOTES.append(f"~/{rel} changed during the run: the ccboard install on this machine synced its hooks (stamp {when}); not a test")
    if changed:
        pytest.fail("a test touched the real home: " + ", ".join(f"~/{rel}" for rel in changed)
                    + " changed during the run (stat size/mtime; the content was never read)", pytrace=False)


def pytest_terminal_summary(terminalreporter):
    for line in SYNC_NOTES:
        terminalreporter.write_line("warning: " + line, yellow=True)


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
def _no_real_codex_pane(monkeypatch):
    """The Codex rollout Tailer reads a row's pane for a rejected login through one seam (codex_rollout._pane_capture): no test captures a real
    tmux pane through it. tests/test_codex_auth.py replaces the seam with a fake screen."""
    from app.agents import codex_rollout
    monkeypatch.setattr(codex_rollout, "_pane_capture", lambda name: "")


@pytest.fixture(autouse=True)
def _no_real_tailscale(monkeypatch):
    """The node id and name read `tailscale status --json` through one seam (app/nodes.py _ts_status): no test runs the real command, and each test
    starts with no id, no Tailscale reading and an empty hello limiter. tests/test_nodes_card.py patches the seam with a fake reading."""
    from app import nodes
    monkeypatch.setattr(nodes, "_ts_status", lambda: None)
    # Synthetic clocks may predate this real process. Restart/replay tests set their own start boundary.
    monkeypatch.setattr(nodes.node_keys, "STARTED", 0)
    nodes.reset()
    yield
    nodes.reset()


# ---- subprocess timeouts, lifted for tests (issue #293) -----------------------------------------------------------------------------
# The fakes answer in milliseconds, but a short production timeout around one (wslinfo 3 s, the doctor's 1.5 s Tailscale command, ...) expires
# when a whole-suite run stalls the machine for a moment, and the code under test reads that as "unknown" or "did not answer". So every named
# subprocess timeout in app/ is raised for each test (autouse fixture below); a test about the timeout itself sets its own short value with
# monkeypatch, which runs after the fixture. tests/test_subprocess_timeouts.py fails when a constant in app/ is in neither table.
TEST_TIMEOUT = 30.0
RAISED_TIMEOUTS = {                               # module -> constants lifted to TEST_TIMEOUT for every test
    "app.platform": ("WSLINFO_TIMEOUT", "LISTEN_TIMEOUT", "LOGIN_SHELL_TIMEOUT", "LAUNCHCTL_TIMEOUT"),
    "app.claude_auth": ("KEYCHAIN_TIMEOUT", "VERSION_TIMEOUT", "STATUS_TIMEOUT", "LOGOUT_TIMEOUT"),
    "app.doctor": ("CHECK_TIMEOUT", "CMD_TIMEOUT", "TS_TIMEOUT", "HELPER_TIMEOUT", "LOGIN_PATH_TIMEOUT", "GH_WAIT"),
    "app.doctor_wsl": ("PS_TIMEOUT",),
    "app.tailscale": ("CALL_TIMEOUT", "WHOIS_TIMEOUT"),
    "app.nodes": ("TS_TIMEOUT",),
    "app.agents.codex": ("CMD_TIMEOUT", "MODELS_TIMEOUT", "LOGOUT_TIMEOUT"),
    "app.agents.claude": ("CAPS_TIMEOUT",),
    "app.tree": ("STATUS_TIMEOUT", "LS_TIMEOUT"),
    "app.projects": ("GIT_TIMEOUT",),
    "app.github": ("TIMEOUT", "CONFIG_TIMEOUT"),
    "app.github_write": ("TIMEOUT",),
    "app.preflight": ("TIMEOUT",),
    "app.tmux": ("TIMEOUT",),
    "app.bare": ("RUNNER_TIMEOUT",),
}
EXEMPT_TIMEOUTS = {                               # (module, constant) -> why it is not lifted
    ("app.backup", "PUSH_TIMEOUT"): "300 s: already far longer than any stall, and no fake is told to take that long",
    ("app.backup", "FETCH_TIMEOUT"): "180 s: already far longer than any stall",
    ("app.cost", "CODEX_FALLBACK_TIMEOUT"): "240 s: already far longer than any stall",
    ("app.scheduler", "RUN_TIMEOUT"): "3600 s default for a whole agent run, taken from the job (timeout_s) in tests",
    ("app.tailscale", "SERVE_TIMEOUT"): "60 s: already longer than any stall (as install.sh's `timeout 60`)",
    ("app.nodes_relay", "RELAY_TIMEOUT"): "an HTTP relay to a peer, not a subprocess",
    ("app.nodes_relay", "CLAIM_TIMEOUT"): "an HTTP relay to a peer (90 s), not a subprocess",
    ("app.nodes_relay", "WORK_TIMEOUT"): "an HTTP relay to a peer (120 s), not a subprocess",
    ("app.nodes_stream", "OPEN_TIMEOUT"): "an HTTP request to a peer, not a subprocess",
    ("app.nodes", "CALL_TIMEOUT"): "an HTTP request to a peer, not a subprocess",
    ("app.nodes_hub", "TIMEOUT"): "an HTTP request to a node, not a subprocess",
    ("app.nodes_discovery", "PROBE_TIMEOUT"): "an HTTPS probe over a socket, not a subprocess",
    ("app.nodes_discovery", "RESOLVE_TIMEOUT"): "a name lookup in a thread, not a subprocess",
    ("app.projects", "RESOLVE_TIMEOUT"): "a name lookup in a thread, not a subprocess",
    ("app.memory", "TIMEOUT"): "an HTTP request to the claude-mem worker, not a subprocess",
    ("app.memory_proxy", "FETCH_TIMEOUT"): "an HTTP request to the claude-mem worker, not a subprocess",
    ("app.github_write", "LOCK_TIMEOUT"): "how long a write waits for a lock (45 s), not a subprocess",
}
# Modules with a short literal timeout in a subprocess call that no test fakes: git on a repository the test built in its own temp directory
# (a real git, answering in milliseconds) or a tool the suite never reaches. tests/test_subprocess_timeouts.py allows a literal under 30 s only here.
LITERAL_TIMEOUT_EXEMPT = {
    "app/tasks.py": "git on a temp repository the test built, never a fake",
    "app/scheduler.py": "git worktree unlock on a temp repository, never a fake",
    "app/preflight.py": "git --version and git ls-remote against a local temp remote, never a fake",
    "app/projects.py": "git init on a temp directory, never a fake",
    "app/prpoll.py": "git and gh calls the poller's tests replace whole (gitops.run, _gh_json), never a fake binary",
}
PRODUCTION_TIMEOUTS: dict[tuple[str, str], float] = {}     # (module, constant) -> the production value, read before the first test lifts it


def production_timeout(module: str, name: str) -> float:
    """The value app/<module> ships for `name`, for a test that asserts on the production limit itself (the fixture has lifted the live one)."""
    return PRODUCTION_TIMEOUTS[(module, name)]


@pytest.fixture(autouse=True)
def _raised_subprocess_timeouts(monkeypatch):
    """Issue #293: lift every timeout in RAISED_TIMEOUTS to TEST_TIMEOUT (see the table above)."""
    import importlib
    for module, names in RAISED_TIMEOUTS.items():
        mod = importlib.import_module(module)
        for name in names:
            PRODUCTION_TIMEOUTS.setdefault((module, name), getattr(mod, name))
            monkeypatch.setattr(mod, name, TEST_TIMEOUT)


TAILSCALE_READER_THREADS = ("doctor-", "bare-poll", "ccboard-lookup")     # app threads that end by themselves (a stop event or a command timeout) and can run a Tailscale or doctor read


def forget_tailscale_and_doctor_state():
    """Empty every in-memory Tailscale and doctor reading: the whois and version caches and the rate window (app/tailscale.py), the node id and
    status reading (app/nodes.py), the doctor's result cache, background probes and last good answers (app/doctor.py), the login-shell lookups."""
    from app import doctor, nodes, platform, tailscale
    tailscale.whois_forget()
    nodes.reset()
    doctor.invalidate()
    platform._login_lookups.clear()


@pytest.fixture(autouse=True)
def _fresh_tailscale_and_doctor_state():
    """Issue #289. Each test starts and ends with no cached Tailscale or doctor reading, and a thread the test started that can still read
    one (a doctor check that ran past its cap, a probe, a poller) is given a moment to end, so it never fills a cache or spends the time
    of the next test. tests/test_tailscale.py's doctor tests answer from the fake command and nothing else."""
    import threading
    forget_tailscale_and_doctor_state()
    before = set(threading.enumerate())
    yield
    deadline = time.monotonic() + 3.0
    for t in threading.enumerate():
        if t not in before and t is not threading.current_thread() and t.name.startswith(TAILSCALE_READER_THREADS):
            t.join(max(0.0, deadline - time.monotonic()))
    forget_tailscale_and_doctor_state()


@pytest.fixture(autouse=True)
def _no_real_github_reader(monkeypatch):
    """The epic reader (app/github.py `api`, which runs `gh api`) and the login lookup behind the claim author rule never reach the real gh: both answer "no gh"
    until a test installs tests/fake_github.py, which puts a fake gh first on PATH and gives them back. The reader's cache and back-off start empty."""
    from app import github, github_write, nodes_epics, nodes_work, prpoll, team_github, team_presence
    nodes_work._edit_cache.clear()

    def refuse(path, etag=None):
        raise github.GhReadError("no_gh")

    def refuse_graph(owner, repo):
        raise github.GhReadError("no_gh")

    def refuse_write(req):
        raise github_write.GhWriteError("no_gh")
    monkeypatch.setattr(github, "api", refuse)
    monkeypatch.setattr(github, "graphql", refuse_graph)                   # the team reader's one GraphQL seam (issue #197), refused the same way
    monkeypatch.setattr(github, "graphql_editors", lambda owner, repo, n: refuse_graph(owner, repo))      # the presence editor check (issue #198), refused the same way
    monkeypatch.setattr(github, "graphql_editor", lambda node_id: refuse_graph(node_id, None))             # ... and its one-comment form
    monkeypatch.setattr(github, "graphql_edit_history", lambda node_id, cursor=None: refuse_graph(node_id, cursor))
    monkeypatch.setattr(team_github, "identity", lambda: (None, None))     # `gh api user` for the node card and the doctor: unknown until tests/fake_github.py is installed
    team_github.reset()
    team_presence.reset()
    monkeypatch.setattr(github_write, "transport", refuse_write)           # the writer's one seam to gh (issue #148): a write reaches a gh only through tests/fake_github.py
    monkeypatch.setattr(github_write, "queue", github_write.Queue())       # each test starts with a fresh write queue (no pacing, no back-off left over)
    monkeypatch.setattr(nodes_epics, "_gh_login", lambda: None)
    monkeypatch.setattr(prpoll, "_gh_login", None)
    nodes_epics.reset()
    yield
    nodes_epics.reset()
    team_github.reset()
    team_presence.reset()


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
def _short_hub_stop(monkeypatch):
    """NodeHub.stop() waits for the polls in flight (so none reads a database that is being closed); a test that parks a poll on purpose
    must not make every stop() wait the production six seconds."""
    from app import nodes_hub
    monkeypatch.setattr(nodes_hub, "STOP_WAIT", 0.5)


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
    """The chaos harness (issue #153): wraps a peer transport (nodes.peer_transport, usually `two_nodes.transport`) and decides, call by call, what the
    network does. It never opens a socket and sleeps only for a `delay` shorter than the call's timeout (keep those to a few milliseconds).

    The first form stays: `fail_first` calls whose path holds `match` fail with `error` before they reach the board (`seen`, `failed` count them).

    Actions (a string, or a tuple with its argument):
      "drop"            the request never arrives (ConnectionRefusedError); the board did nothing
      "timeout"         the request never arrives and the caller times out (TimeoutError); the board did nothing
      "lost"            the request ARRIVES and the board acts, then the answer is lost (TimeoutError): the ambiguous write
      ("delay", s)      the answer comes after s seconds; s at or over the call's timeout is "lost" (the board acted, the caller gave up)
      ("status", 503)   a proxy in front of the board answers that status; the board did nothing
      ("truncate", n)   the board acts and answers, and only the first n bytes of the body arrive
      ("call", fn)      fn() runs first (something happens while the request is on its way, say the pair is removed there), then the call goes through
      "pass"            the call goes through untouched
    Where they come from, in this order: a partition (`partition("node-a", "node-b")`: every call between the two named boards, either way, is "drop"
    until `heal()`), the scripted sequence (`sequence(*actions, match=, method=)`: one action per matching call, in order, then nothing), the rules
    (`on(action, match=, method=, times=1)`; times -1 = for ever) and `fail_first`. The caller is the board whose code makes the call (settings.node_name,
    as TwoNodes.transport reads it); the target is the board at the address.
    Clock offsets: `offset("node-b", 600)` makes node-b's clock 600 s ahead as other boards see it: its card's `now`, its state's `node.now` and the
    X-CCBoard-Now header of a 304 are shifted in the answer. `attach(two_nodes)` also shifts the clocks of code that runs ON that board (nodes._now and
    nodes_work.clock while inside `board.enter()`), for claim-ordering tests; `detach()` undoes it (the `chaos` fixture does both).
    `log` holds one (caller, method, board, path, action) per call, never a header or a body."""

    def __init__(self, inner, fail_first=0, error=ConnectionRefusedError, match=None):
        self.inner, self.fail_first, self.error, self.match = inner, fail_first, error, match
        self.seen = 0
        self.failed = 0
        self.rules: list[dict] = []
        self.script: list[dict] = []
        self.partitions: set[frozenset] = set()
        self.offsets: dict[str, float] = {}
        self.log: list[tuple] = []
        self._attached: list = []

    # ---- the plan
    def on(self, action, match=None, method=None, times=1):
        self.rules.append({"action": action, "match": match, "method": method, "times": times})
        return self

    def sequence(self, *actions, match=None, method=None):
        self.script.extend({"action": a, "match": match, "method": method} for a in actions)
        return self

    def partition(self, a: str, b: str):
        self.partitions.add(frozenset((a, b)))
        return self

    def heal(self):
        self.partitions.clear()
        return self

    def offset(self, board: str, seconds: float):
        self.offsets[board] = float(seconds)
        return self

    def clear(self):
        self.rules.clear()
        self.script.clear()
        self.partitions.clear()
        return self

    # ---- the clocks of code running on a board
    def attach(self, two):
        """Shift nodes._now and nodes_work.clock by the board's offset while code runs inside that board's enter() (the offsets are read at each enter)."""
        from app import nodes, nodes_work
        for board in (two.a, two.b):
            orig = board.enter

            @contextlib.contextmanager
            def enter(_orig=orig, _name=board.name):
                off = self.offsets.get(_name, 0.0)
                saved = (nodes._now, nodes_work.clock)
                if off:
                    nodes._now = lambda _f=saved[0], _o=off: _f() + _o
                    nodes_work.clock = lambda _f=saved[1], _o=off: _f() + _o
                try:
                    with _orig() as b:
                        yield b
                finally:
                    nodes._now, nodes_work.clock = saved
            board.enter = enter
            self._attached.append((board, orig))
        return self

    def detach(self):
        for board, orig in self._attached:
            board.enter = orig
        self._attached.clear()

    # ---- one call
    def _board_name(self, target) -> str | None:
        obj, boards = self.inner, None
        for _ in range(5):                                   # the inner transport may itself wrap TwoNodes.transport (a counter)
            boards = getattr(getattr(obj, "__self__", None), "boards", None)
            if boards is not None or obj is None:
                break
            obj = getattr(obj, "inner", None)
        b = (boards or {}).get((target.host, target.port))
        return b.name if b is not None else None

    @staticmethod
    def _fits(r: dict, method: str, path: str) -> bool:
        return (r["match"] is None or r["match"] in path) and (r["method"] is None or r["method"] == method)

    def _pick(self, caller, board, method, path):
        if board is not None and frozenset((caller, board)) in self.partitions:
            return "drop"
        for i, r in enumerate(self.script):
            if self._fits(r, method, path):
                return self.script.pop(i)["action"]
        for r in self.rules:
            if r["times"] != 0 and self._fits(r, method, path):
                if r["times"] > 0:
                    r["times"] -= 1
                return r["action"]
        if self.match is None or self.match in path:
            self.seen += 1
            if self.failed < self.fail_first:
                self.failed += 1
                return "fail_first"
        return "pass"

    def _shift(self, board, path, status, headers, data):
        off = self.offsets.get(board or "", 0.0)
        if not off:
            return headers, data
        import json as _json
        from datetime import datetime, timedelta

        def move(v):
            try:
                return (datetime.fromisoformat(v) + timedelta(seconds=off)).isoformat(timespec="milliseconds")
            except (TypeError, ValueError):
                return v
        headers = dict(headers)
        for k in list(headers):
            if k.lower() == "x-ccboard-now":
                headers[k] = move(headers[k])
        if status == 200 and path.split("?")[0] in ("/api/node", "/api/node/state"):
            try:
                j = _json.loads(data)
            except ValueError:
                return headers, data
            if isinstance(j, dict):
                if isinstance(j.get("now"), str):
                    j["now"] = move(j["now"])
                if isinstance(j.get("node"), dict) and isinstance(j["node"].get("now"), str):
                    j["node"]["now"] = move(j["node"]["now"])
                data = _json.dumps(j).encode()
        return headers, data

    def __call__(self, target, method, path, headers, body, timeout):
        from app.config import settings
        caller, board = settings.node_name or "?", self._board_name(target)
        action = self._pick(caller, board, method, path)
        kind, arg = (action, None) if isinstance(action, str) else (action[0], action[1])
        if kind == "delay" and arg is not None and arg >= timeout:
            kind = "lost"
        self.log.append((caller, method, board, path, kind))
        if kind == "fail_first":
            raise self.error("flaky transport: planned failure")
        if kind == "drop":
            raise ConnectionRefusedError("flaky transport: dropped")
        if kind == "timeout":
            raise TimeoutError("flaky transport: no answer")
        if kind == "status":
            return int(arg), {"content-type": "application/json"}, b'{"error":"planned"}'
        if kind == "delay" and arg:
            time.sleep(arg)
        if kind == "call":
            arg()
        status, rh, data = self.inner(target, method, path, headers, body, timeout)
        if kind == "lost":
            raise TimeoutError("flaky transport: the answer was lost")
        if kind == "truncate":
            data = data[: int(arg)]
        rh, data = self._shift(board, path, status, rh, data)
        return status, rh, data


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
        self.signer = None
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
        # pair_up opts integration tests into real signatures. Raw two_nodes stays unsigned for auth/tamper tests.
        if self.signer is not None and h.get("Authorization", "").startswith("Bearer ccbnode_") and not h.get("X-CCBoard-Signature"):
            import httpx
            from app import nodes, node_keys
            wire = httpx.Request(method, path, **{k: v for k, v in kw.items() if k in ("content", "json", "data", "params")})
            with self.enter():
                recipient = nodes.node_id()
                pair = nodes.verify_token(h["Authorization"][7:], db=self.db)
            with self.signer.enter():
                if pair and pair["node_id"] != nodes.node_id():
                    with self.enter():
                        return self.parent.client.request(method, path, headers=h, **kw)
                h["X-CCBoard-Node"] = nodes.node_id()
                h.update(node_keys.sign_headers(method, wire.url.raw_path.decode("ascii"), wire.content,
                                               recipient_id=recipient, token=h["Authorization"][7:], acting_user=h.get("X-CCBoard-Acting-User")))
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
    t.a.db.close()
    t.b.db.close()


def _sockets() -> int:
    """How many of this process's file descriptors are sockets now (0 where /dev/fd cannot be listed)."""
    import stat
    fd_dir = "/dev/fd" if os.path.isdir("/dev/fd") else f"/proc/{os.getpid()}/fd"
    n = 0
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return 0
    for name in names:
        try:
            if stat.S_ISSOCK(os.fstat(int(name)).st_mode):
                n += 1
        except (OSError, ValueError):
            pass
    return n


@pytest.fixture
def no_leaks(tmp_path, monkeypatch):
    """Asserts at teardown that the test left no thread, no socket and no temp file behind (issue #153): the threads alive at the end must have been alive
    at the start (a thread still finishing gets two seconds), the count of open sockets is back where it was, and the temp dir every `tempfile` call uses
    during the test (a fresh one under tmp_path) is empty."""
    import tempfile
    import threading
    tdir = tmp_path / "tempfiles"
    tdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tdir))
    before = set(threading.enumerate())
    socks = _sockets()
    yield
    deadline = time.monotonic() + 2.0
    while True:
        extra = [t for t in threading.enumerate() if t not in before and t.is_alive()]
        if not extra or time.monotonic() > deadline:
            break
        time.sleep(0.02)
    assert not extra, f"threads left behind: {[t.name for t in extra]}"
    assert _sockets() <= socks, "a socket was left open"
    assert list(tdir.iterdir()) == [], "a temp file was left behind"


@pytest.fixture
def chaos(two_nodes, no_leaks, monkeypatch):
    """`two_nodes` behind a FlakyTransport (the chaos harness, issue #153): `chaos.net` is the FlakyTransport set as nodes.peer_transport and attached to
    both boards' clocks; `chaos.a`, `chaos.b` are the boards. Teardown detaches it and checks that nothing (thread, socket, temp file) was left."""
    from types import SimpleNamespace
    from app import nodes
    net = FlakyTransport(two_nodes.transport).attach(two_nodes)
    monkeypatch.setattr(nodes, "peer_transport", net)
    yield SimpleNamespace(two=two_nodes, a=two_nodes.a, b=two_nodes.b, net=net)
    net.detach()


@pytest.fixture
def probe_rows():
    """Two synthetic relay rows on the running app for one test (this phase ships only read rows): `probe`, a write row on scope `sessions` that starts an
    agent (guard on, a body model with the launch fields), and `probe_answer`, a human-only write row on scope `permissions`. They go through the same
    install() and register_scopes() as the real table; the routes and the NODE_ROUTES entries are taken out again afterwards. `calls` holds the clean body
    each peer handler saw."""
    from types import SimpleNamespace
    from pydantic import BaseModel, ConfigDict
    from app import main, nodes_relay as nr

    class LaunchProbe(BaseModel):
        model_config = ConfigDict(extra="forbid")
        agent: str | None = None
        title: str | None = None
        prompt: str | None = None
        permission_mode: str | None = None
        mode: str | None = None
        sandbox: str | None = None
        approval: str | None = None
        opts: dict | None = None

    calls: list = []
    rows = [nr.Row("probe", "POST", "/api/nodes/{handle}/probe", "POST", "/api/node/probe", "sessions", LaunchProbe, nr.WRITE, "probe_launch", guard=True,
                   target=lambda p: "probe"),
            nr.Row("probe_answer", "POST", "/api/nodes/{handle}/probe-answer", "POST", "/api/node/probe-answer", "permissions", nr.NoFields, nr.WRITE,
                   "probe_answer", human_only=True, target=lambda p: "probe answer")]
    handlers = {"probe": lambda db, params, body: calls.append(body) or {"ok": True}, "probe_answer": lambda db, params, body: calls.append(body) or {"ok": True}}
    added = nr.install(main.app, handlers, lambda: main.db, lambda: main._hub(), rows=rows)
    nr.register_scopes(rows)
    try:
        yield SimpleNamespace(rows=rows, calls=calls, launch=rows[0], answer=rows[1])
    finally:
        nr.forget_scopes(rows)
        for r in added:
            main.app.router.routes.remove(r)


@pytest.fixture
def pair_up(two_nodes):
    """`pair_up(scopes)` pairs board `a` (the hub, the one that calls) with board `b` through the real routes and answers a's registry row view; the per-pair
    buckets and the in-memory readings are reset first. Every file and database stays in the two boards' temp data dirs (issue #140)."""
    from app import nodes

    def go(scopes=("read", "tasks", "sessions", "permissions")):
        nodes.reset()
        code = two_nodes.b.post("/api/nodes/pair-code", json={"scopes": list(scopes), "minutes": 10}).json()["code"]
        made = two_nodes.a.post("/api/nodes", json={"url": two_nodes.b.url, "code": code})
        assert made.status_code == 201, made.text
        two_nodes.b.signer = two_nodes.a
        return made.json()
    return go


@pytest.fixture
def split_boards(two_nodes, fake_tmux, tmp_path):
    """The two boards of `two_nodes` with their own projects dir and their own fake tmux sessions (issue #141): what board `b` creates is under
    `split_boards.b_projects` and in `split_boards.tmux_b`, never on `a`, and the other way round. `Board.enter()` swaps `settings.projects_dir` and the
    containers of the shared fake tmux store (`sessions`, `created`, `sent`, ...) for the length of a `with`, and puts them back, nested or not. Returns
    the TwoNodes object with `a_projects`, `b_projects`, `tmux_a` and `tmux_b` added (each tmux_x is that board's store: the same keys as `fake_tmux`)."""
    from types import SimpleNamespace
    from app import main
    from app.config import settings

    fake_tmux.setdefault("screen", "")                     # a key a test sets later would otherwise leak out of the board's store when the swap is undone

    def fresh():
        return {k: (type(v)() if isinstance(v, (dict, list)) else v) for k, v in fake_tmux.items()}

    def wrap(board, pdir, store):
        orig = board.enter

        @contextlib.contextmanager
        def enter():
            saved_dir, saved_store = settings.projects_dir, dict(fake_tmux)
            settings.projects_dir = pdir
            fake_tmux.update(store)
            main._invalidate_scan()
            try:
                with orig():
                    yield board
            finally:
                fake_tmux.update(saved_store)
                settings.projects_dir = saved_dir
                main._invalidate_scan()
        board.enter = enter

    pa, pb = tmp_path / "projects-a", tmp_path / "projects-b"
    pa.mkdir()
    pb.mkdir()
    sa, sb = fresh(), fresh()
    wrap(two_nodes.a, pa, sa)
    wrap(two_nodes.b, pb, sb)
    two_nodes.a_projects, two_nodes.b_projects, two_nodes.tmux_a, two_nodes.tmux_b = pa, pb, sa, sb
    return two_nodes


@pytest.fixture(autouse=True)
def _no_real_bare(monkeypatch):
    from app import bare

    def refuse(*args, **kwargs):
        raise AssertionError("A bare-device test must install the scripted runner")

    monkeypatch.setattr(bare, "runner", refuse)


@pytest.fixture
def bare_runner(monkeypatch, projects_dir):
    """Scripted process answers and exact argv, with no process or network access."""
    from types import SimpleNamespace
    from app import bare
    fake = SimpleNamespace(answers=[], calls=[])

    def run(argv, *, stdin=None, stdout_limit=32768):
        fake.calls.append((list(argv), stdin))
        assert fake.answers, "unexpected device command"
        answer = fake.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        code, out, err = answer
        return SimpleNamespace(returncode=code, stdout=out, stderr=err)

    monkeypatch.setattr(bare, "runner", run)
    return fake


# ---- hermetic binaries (issue #291) -----------------------------------------------------------------------------------------------
# The full doctor (GET /api/doctor, doctor.run()) asks about the machine's own tools, and several have absolute fallbacks
# (/usr/local/bin/tailscale, /Applications/Tailscale.app, /usr/local/bin/ttyd, the Homebrew folders), so on a developer's Mac a test that
# ran the full doctor ran the REAL tailscale, gh, ttyd ... and its answer depended on that machine's state. Rule for the suite: a program
# in MACHINE_TOOLS is found only on a PATH directory under the temp dirs (a fake the test wrote), never anywhere else; asking for any
# other copy reads as "not installed" (FileNotFoundError, what a missing program raises). tmux is guarded only while the doctor runs,
# because tests marked needs_tmux start the real one on a private socket.
# Two ways through the guard, both narrow: (1) the repository's own dev fakes (scripts/dev: fake_agents/claude, fake_agents/codex, ...) count
# as temp dirs, because they are test doubles that ship in the repo; (2) a test that deliberately needs a real machine tool says so with
# @pytest.mark.real_tool("docker"), which lets that one named tool through for that one test and nothing else.
MACHINE_TOOLS = frozenset({
    "tailscale", "tailscaled", "ttyd", "claude", "codex", "gh", "restic", "docker", "systemctl", "launchctl", "bun", "code-server",
    "ccusage", "security", "devcontainer", "pmset", "sudo",
})
DOCTOR_ONLY_TOOLS = frozenset({"tmux", "ps"})      # ps: the process list the tmux socket check reads
SPAWNED: list[str] = []             # the executable of every process a test started (reset per test); tests/test_doctor_hermetic.py reads it
_doctor_runs = 0
_real_which = shutil.which


def temp_roots() -> tuple[str, ...]:
    import tempfile
    return tuple(dict.fromkeys(os.path.realpath(p) for p in (tempfile.gettempdir(), "/tmp")))


def under_temp(path) -> bool:
    real = os.path.realpath(os.fspath(path))
    return any(real == r or real.startswith(r + os.sep) for r in temp_roots())


DEV_FAKE_ROOTS = (os.path.realpath(ROOT / "scripts" / "dev"),)      # the repo's own test doubles (fake_agents, fake_tty, ...)


def trusted_location(path) -> bool:
    """A temp dir, or one of the repo's own dev fakes: where the guard lets a MACHINE_TOOLS name run from."""
    if under_temp(path):
        return True
    real = os.path.realpath(os.fspath(path))
    return any(real == r or real.startswith(r + os.sep) for r in DEV_FAKE_ROOTS)


REAL_TOOLS: set[str] = set()        # names the running test opted into with @pytest.mark.real_tool(...); reset per test


def _guarded(name: str) -> bool:
    if name in REAL_TOOLS:
        return False
    return name in MACHINE_TOOLS or (_doctor_runs > 0 and name in DOCTOR_ONLY_TOOLS)


def _hermetic_which(cmd, mode=os.F_OK | os.X_OK, path=None):
    name = os.fspath(cmd)
    if not _guarded(os.path.basename(name)):
        return _real_which(cmd, mode, path)
    if os.sep in name:
        return _real_which(cmd, mode, path) if trusted_location(name) else None
    kept = [d for d in (path if path is not None else os.environ.get("PATH", os.defpath)).split(os.pathsep) if d and trusted_location(d)]
    return _real_which(cmd, mode, os.pathsep.join(kept)) if kept else None


@pytest.fixture(autouse=True)
def _hermetic_binaries(request, monkeypatch):
    """See the block comment above: PATH lookups, the doctor's absolute fallbacks and every process start obey MACHINE_TOOLS. A test that
    wants a program writes a fake into a temp dir and puts that on PATH (tests/test_tailscale.py's box fixture); a test-level monkeypatch
    of any of these names runs after this one and wins. @pytest.mark.real_tool("docker") lets that named tool run for real in that test only.
    Yields SPAWNED."""
    import subprocess
    import tempfile
    from app import doctor, platform as plat, tailscale as ts
    nowhere = pathlib.Path(tempfile.gettempdir()) / "ccboard-test-no-such-install"
    monkeypatch.setattr(shutil, "which", _hermetic_which)
    monkeypatch.setattr(plat, "BREW_BIN_DIRS", ())
    monkeypatch.setattr(ts, "MAC_CLI_CANDIDATES", (nowhere / "tailscale",))
    monkeypatch.setattr(ts, "MAC_DAEMON_CANDIDATES", (nowhere / "tailscaled",))
    for name in ("MAC_APP", "MAC_APP_CLI", "MAS_RECEIPT", "MAC_DAEMON_SOCKET"):
        monkeypatch.setattr(ts, name, nowhere / name)
    monkeypatch.setattr(doctor, "TTYD_BIN", str(nowhere / "ttyd"))
    monkeypatch.setattr(doctor, "_login_path", lambda: "")              # a login shell would run the owner's profile
    real_executable = doctor._executable
    monkeypatch.setattr(doctor, "_executable", lambda p: bool(p) and trusted_location(p) and real_executable(p))
    SPAWNED.clear()
    REAL_TOOLS.clear()
    for mark in request.node.iter_markers("real_tool"):
        named = set(mark.args)
        unknown = sorted(n for n in named if not isinstance(n, str) or n not in MACHINE_TOOLS | DOCTOR_ONLY_TOOLS)
        if not named or unknown:
            pytest.fail("real_tool needs names from MACHINE_TOOLS or DOCTOR_ONLY_TOOLS (tests/conftest.py); got "
                        + (", ".join(map(repr, unknown)) if unknown else "none"), pytrace=False)
        REAL_TOOLS.update(named)
    real_init = subprocess.Popen.__init__

    def guarded_init(self, args, *a, **kw):
        if not kw.get("shell"):
            argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
            exe = os.fsdecode(kw.get("executable") or (argv[0] if argv else ""))
            env = kw.get("env")
            if os.sep in exe:
                found = exe if os.access(exe, os.X_OK) else None
            else:
                found = _real_which(exe, path=(env if env is not None else os.environ).get("PATH", os.defpath)) if exe else None
            if exe and _guarded(os.path.basename(exe)) and not (found and trusted_location(found)):
                raise FileNotFoundError(2, "not installed (hermetic test run, issue #291)", exe)
            if found:
                SPAWNED.append(found)
        real_init(self, args, *a, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_init)
    real_run = doctor.run

    def counted_run(*a, **kw):
        global _doctor_runs
        _doctor_runs += 1
        try:
            return real_run(*a, **kw)
        finally:
            _doctor_runs -= 1

    monkeypatch.setattr(doctor, "run", counted_run)
    yield SPAWNED
    REAL_TOOLS.clear()


@pytest.fixture(autouse=True)
def _node_keys_typed(monkeypatch):
    """The suite runs as if CCBOARD_NODE_KEYS were typed (issue #271): `settings.node_keys` is then the mode in force, whatever pairs exist. A test of the
    default, which looks at the pairs, sets node_keys_explicit to False itself (tests/test_nodes_keys_default.py)."""
    from app.config import settings
    monkeypatch.setattr(settings, "node_keys_explicit", True, raising=False)


@pytest.fixture
def unsigned_node_keys(monkeypatch):
    """Opt token/scope tests with deliberately unkeyed pairs into compatibility mode."""
    from app.config import settings
    monkeypatch.setattr(settings, "node_keys", "prefer")
