"""app.platform processes and host numbers (issue #116 slices 5 and 6): the Linux readers on a fake /proc tree (one seam, platform.PROC_ROOT),
the non-Linux branch on a fake psutil module in sys.modules, and the empty answers when psutil or lsof is missing. Temp dirs only: no real
/proc, no real process table, no real ss or lsof."""
from __future__ import annotations

import ast
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from packaging.requirements import Requirement

from app import doctor, health, memory, platform
from app import codex_accounts as cx
from app.agents import codex as codex_agent
from tests.proc_fake import FakePsutil, fake_proc, no_psutil, use_fake_proc, use_fake_psutil

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def proc(tmp_path, monkeypatch):
    return use_fake_proc(monkeypatch, tmp_path / "proc")


@pytest.fixture(autouse=True)
def _fresh_cpu(monkeypatch):
    monkeypatch.setattr(platform, "_cpu_prev", {})


# ---------------------------------------------------------------- Linux: the /proc readers

def test_ppid_reads_proc_stat_with_a_comm_that_holds_spaces_and_parens(proc):
    proc.mkdir()
    (proc / "123").mkdir()
    (proc / "123" / "stat").write_text("123 (claude (v2) x) S 456 123 123 0 -1 4194560\n")
    (proc / "124").mkdir()
    (proc / "124" / "stat").write_text("garbage")
    assert platform.ppid(123) == 456
    assert platform.ppid(124) is None and platform.ppid(999) is None


def test_ancestors_nearest_first_bounded_and_loop_safe(proc, monkeypatch):
    for pid, parent in ((10, 20), (20, 30), (30, 40), (40, 1), (1, 0)):
        fake_proc(proc, pid, f"p{pid}", ppid=parent)
    assert platform.ancestors(10, 20) == [20, 30, 40], "stops at pid 1 without listing it"
    assert platform.ancestors(10, 2) == [20, 30], "at most `hops` long"
    assert platform.ancestors(999, 5) == [] and platform.ancestors(10, 0) == []
    monkeypatch.setattr(platform, "ppid", lambda pid: pid + 1)                # never reaches 1
    assert len(platform.ancestors(10, 7)) == 7
    monkeypatch.setattr(platform, "ppid", lambda pid: {10: 11, 11: 10}.get(pid))
    assert platform.ancestors(10, 20) == [11], "a loop ends the walk"
    monkeypatch.setattr(platform, "ppid", lambda pid: {10: 11}.get(pid))
    assert platform.ancestors(10, 20) == [11], "an unreadable parent ends the walk"


def test_children_reads_every_task_and_skips_what_it_cannot_parse(proc):
    fake_proc(proc, 77, "dev")
    for tid, text in ((77, "11 12\n"), (78, "13\n"), (79, "x\n")):
        d = proc / "77" / "task" / str(tid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "children").write_text(text)
    (proc / "77" / "task" / "80").mkdir()                                      # a task without a children file
    assert sorted(platform.children(77)) == [11, 12, 13]
    assert platform.children(78) == [] and platform.children(0) == []


def test_comm_is_stripped_and_none_when_gone(proc):
    fake_proc(proc, 5, "codex")
    assert platform.comm(5) == "codex" and platform.comm(6) is None


def test_iter_processes_lists_numeric_entries_and_is_none_without_proc(proc, tmp_path, monkeypatch):
    fake_proc(proc, 100, "a")
    fake_proc(proc, 7, "b")
    (proc / "self").mkdir()
    (proc / "meminfo").write_text("x")
    assert sorted(platform.iter_processes()) == [7, 100]
    monkeypatch.setattr(platform, "PROC_ROOT", tmp_path / "nowhere")
    assert platform.iter_processes() is None, "unreadable is None, never an empty list"


def test_environ_names_returns_names_never_values(proc):
    fake_proc(proc, 10, "w", environ=b"PATH=/usr/bin\0CCBOARD_SESSION=proj--repo--s1\0TMUX_PANE=%0\0HOME=/home/u\0TMUX_PANE=%1\0")
    names = platform.environ_names(10)
    assert names == ["CCBOARD_SESSION", "HOME", "PATH", "TMUX_PANE"], "sorted, once each"
    assert not any("=" in n or "proj--repo" in n or "/usr/bin" in n for n in names)
    fake_proc(proc, 13, "w", environ=b"FOO\0BAR=\0=odd\0\0")
    assert platform.environ_names(13) == ["BAR", "FOO"], "a nameless entry is dropped"


def test_environ_names_is_none_when_it_cannot_read(proc):
    fake_proc(proc, 14, "w", environ=b"")
    assert platform.environ_names(14) is None, "an empty environ (a zombie) reads as unreadable"
    assert platform.environ_names(99) is None
    for bad in (None, 0, -1, "10", True, 1.5):
        assert platform.environ_names(bad) is None


SS = ('LISTEN 0 511 127.0.0.1:5173 0.0.0.0:* users:(("node",pid=4242,fd=23))\n'
      'LISTEN 0 4096 *:3000 *:* users:(("python3",pid=77,fd=5),("python3",pid=78,fd=5))\n'
      'LISTEN 0 4096 [::]:22 [::]:* users:(("sshd",pid=900,fd=3))\n'
      'LISTEN 0 4096 *:80 *:*\n')


class _Run:
    def __init__(self, out="", err=None):
        self.out, self.err, self.argv = out, err, None

    def __call__(self, argv, **kw):
        self.argv, self.kw = argv, kw
        if self.err:
            raise self.err
        return SimpleNamespace(stdout=self.out, stderr="", returncode=0)


def test_listening_ports_on_linux_is_ss_ltnpH_parsed_as_before(monkeypatch):
    monkeypatch.setattr(platform, "IS_LINUX", True)
    run = _Run(SS)
    monkeypatch.setattr(platform.shutil, "which", lambda name: "/usr/bin/ss" if name == "ss" else None)
    monkeypatch.setattr(platform.subprocess, "run", run)
    assert platform.listening_ports(None) == {4242: {5173}, 77: {3000}, 78: {3000}, 900: {22}}
    assert run.argv == ["/usr/bin/ss", "-ltnpH"] and run.kw["timeout"] == 5 and run.kw["capture_output"] and run.kw["text"]
    assert platform.listening_ports([77, 900, 5]) == {77: {3000}, 900: {22}}, "only the pids asked for"
    assert platform.listening_ports([]) == {}


def test_listening_ports_on_linux_never_raises(monkeypatch):
    monkeypatch.setattr(platform, "IS_LINUX", True)
    monkeypatch.setattr(platform.shutil, "which", lambda name: None)
    assert platform.listening_ports([1]) == {}, "no ss"
    monkeypatch.setattr(platform.shutil, "which", lambda name: "/usr/bin/ss")
    for err in (subprocess.TimeoutExpired("ss", 5), OSError("nope")):
        monkeypatch.setattr(platform.subprocess, "run", _Run(err=err))
        assert platform.listening_ports([1]) == {}
    monkeypatch.setattr(platform.subprocess, "run", _Run("LISTEN 0 1 nonsense\nshort line\nLISTEN 0 1 *:x *:* users:((\"a\",pid=5,fd=1))\n"))
    assert platform.listening_ports(None) == {}


# ---------------------------------------------------------------- Linux: host numbers (values pinned from the code that was moved)

def test_mem_on_a_fake_meminfo_keeps_the_shape_and_numbers(proc):
    proc.mkdir()
    (proc / "meminfo").write_text("MemTotal:       16384000 kB\nMemFree:         1000000 kB\nMemAvailable:    4096000 kB\nBuffers: 5 kB\n")
    assert platform.mem() == {"total": 16777216000, "used": 12582912000, "pct": 75.0}
    assert health._mem() == platform.mem()
    (proc / "meminfo").write_text("MemTotal:       2048000 kB\nMemFree:          512000 kB\n")
    assert platform.mem() == {"total": 2097152000, "used": 1572864000, "pct": 75.0}, "no MemAvailable: MemFree stands in"
    (proc / "meminfo").write_text("MemTotal: x kB\n")
    assert platform.mem() is None
    (proc / "meminfo").unlink()
    assert platform.mem() is None


def test_uptime_on_a_fake_proc_uptime(proc):
    proc.mkdir()
    (proc / "uptime").write_text("12345.67 9999.0\n")
    assert platform.uptime() == 12345.67 and health._uptime() == 12345.67
    (proc / "uptime").write_text("garbage\n")
    assert platform.uptime() is None
    (proc / "uptime").unlink()
    assert platform.uptime() is None


def test_cpu_pct_on_a_fake_proc_stat_is_per_consumer(proc):
    proc.mkdir()

    def feed(idle, busy, iowait=0):
        (proc / "stat").write_text(f"cpu  {busy} 0 0 {idle} {iowait} 0 0 0 0 0\ncpu0 1 1 1 1\n")
    feed(100, 100)
    assert platform.cpu_pct("a") is None and platform.cpu_pct("b") is None
    feed(100, 200)
    assert platform.cpu_pct("a") == 100.0
    feed(200, 200)
    assert platform.cpu_pct("a") == 0.0 and platform.cpu_pct("b") == 50.0
    feed(200, 200)
    assert platform.cpu_pct("a") is None, "no time passed: nothing to divide by"
    feed(300, 200, iowait=100)                       # iowait counts as idle
    assert platform.cpu_pct("a") == 0.0
    (proc / "stat").unlink()
    assert platform.cpu_pct("a") is None


# ---------------------------------------------------------------- not Linux: psutil

def test_psutil_is_never_imported_at_module_level():
    tree = ast.parse((ROOT / "app" / "platform.py").read_text())
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not any(a.name.split(".")[0] == "psutil" for n in top if isinstance(n, ast.Import) for a in n.names)
    assert not any((n.module or "").split(".")[0] == "psutil" for n in top if isinstance(n, ast.ImportFrom))
    assert not any(n.level for n in top if isinstance(n, ast.ImportFrom)), "app/platform.py imports nothing of the app"


def test_the_process_functions_over_a_fake_psutil(monkeypatch):
    fake = use_fake_psutil(monkeypatch, FakePsutil({
        1: {"name": "launchd", "ppid": 0},
        500: {"name": "zsh", "ppid": 1},
        501: {"name": "claude", "ppid": 500},
        502: {"name": "node", "ppid": 501, "environ": {"PATH": "/usr/bin", "CCBOARD_SESSION": "secret-value", "TMUX_PANE": "%1"}},
        503: {"name": "codex", "ppid": 501},
        900: {"name": "other", "ppid": 1, "environ": {"A": "1"}},
    }, denied={900}))
    assert platform.ppid(502) == 501 and platform.ppid(1234) is None
    assert platform.ancestors(502, 20) == [501, 500] and platform.ancestors(502, 1) == [501]
    assert sorted(platform.children(501)) == [502, 503] and platform.children(1234) == []
    assert platform.comm(503) == "codex" and platform.comm(1234) is None and platform.comm(900) is None, "gone and denied are None"
    assert platform.iter_processes() == [1, 500, 501, 502, 503, 900]
    names = platform.environ_names(502)
    assert names == ["CCBOARD_SESSION", "PATH", "TMUX_PANE"] and "secret-value" not in str(names), "keys only"
    assert platform.environ_names(900) is None, "another user's process"
    assert platform.environ_names(503) is None, "no environment readable: unreadable, not clean"
    assert platform.environ_names(1234) is None
    assert ("children", 501, False) in fake.calls


def test_listening_ports_over_psutil_asks_each_process_and_never_the_system_wide_list(monkeypatch):
    fake = use_fake_psutil(monkeypatch, FakePsutil({
        10: {"name": "zsh", "ppid": 1, "conns": []},
        11: {"name": "node", "ppid": 10, "conns": [(5173, "LISTEN"), (54000, "ESTABLISHED")]},
        12: {"name": "py", "ppid": 10, "conns": [(3000, "LISTEN"), (3001, "LISTEN")]},
        13: {"name": "root", "ppid": 1, "conns": [(22, "LISTEN")]},
        14: {"name": "other", "ppid": 1, "conns": [(9999, "LISTEN")]},
    }, denied={14}))
    assert platform.listening_ports([10, 11, 12, 14, 777]) == {11: {5173}, 12: {3000, 3001}}, "gone and denied pids are skipped"
    assert platform.listening_ports(None)[13] == {22}, "None: every process that can be read"
    assert ("net_connections", 11, "tcp") in fake.calls
    assert ("system_net_connections",) not in fake.calls
    assert platform.listening_ports([]) == {}


LSOF = ("COMMAND   PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
        "node    4242 me   23u  IPv4 0x1      0t0  TCP 127.0.0.1:5173 (LISTEN)\n"
        "Python    77 me    5u  IPv6 0x2      0t0  TCP *:3000 (LISTEN)\n"
        "Python    77 me    6u  IPv4 0x3      0t0  TCP *:3001 (LISTEN)\n"
        "Code\\x20Hel 90 me   7u  IPv6 0x4      0t0  TCP [::1]:8080 (LISTEN)\n"
        "curl      91 me    8u  IPv4 0x5      0t0  TCP 1.2.3.4:5->5.6.7.8:80 (ESTABLISHED)\n"
        "weird     92 me    9u  IPv4 0x6      0t0  TCP *:notaport (LISTEN)\n")


def test_without_psutil_lsof_is_the_last_resort(monkeypatch):
    no_psutil(monkeypatch)
    run = _Run(LSOF)
    monkeypatch.setattr(platform.shutil, "which", lambda name: "/usr/sbin/lsof" if name == "lsof" else None)
    monkeypatch.setattr(platform.subprocess, "run", run)
    assert platform.listening_ports(None) == {4242: {5173}, 77: {3000, 3001}, 90: {8080}}
    assert run.argv == ["/usr/sbin/lsof", "-nP", "-iTCP", "-sTCP:LISTEN"] and run.kw["timeout"] == 5
    assert platform.listening_ports([77, 91]) == {77: {3000, 3001}}


def test_lsof_is_not_used_while_psutil_works(monkeypatch):
    use_fake_psutil(monkeypatch, FakePsutil({10: {"name": "x", "ppid": 1, "conns": [(1234, "LISTEN")]}}))
    monkeypatch.setattr(platform.shutil, "which", lambda name: pytest.fail("lsof or ss looked up"))
    assert platform.listening_ports([10]) == {10: {1234}}


def test_missing_psutil_and_missing_lsof_give_empty_answers_and_never_raise(monkeypatch):
    no_psutil(monkeypatch)
    monkeypatch.setattr(platform.shutil, "which", lambda name: None)
    assert platform.ppid(1) is None and platform.ancestors(5, 9) == [] and platform.children(1) == []
    assert platform.comm(1) is None and platform.iter_processes() is None and platform.environ_names(1) is None
    assert platform.listening_ports([1, 2]) == {} and platform.listening_ports(None) == {}
    assert platform.cpu_pct("x") is None and platform.mem() is None and platform.uptime() is None
    monkeypatch.setattr(platform.shutil, "which", lambda name: "/usr/sbin/lsof")
    for err in (subprocess.TimeoutExpired("lsof", 5), OSError("no")):
        monkeypatch.setattr(platform.subprocess, "run", _Run(err=err))
        assert platform.listening_ports([1]) == {}


def test_a_psutil_that_blows_up_is_an_unknown_answer_not_a_crash(monkeypatch):
    fake = use_fake_psutil(monkeypatch, FakePsutil({1: {"name": "x", "ppid": 0}}))

    def boom(*a, **k):
        raise RuntimeError("psutil broke")
    for name in ("pids", "cpu_times", "virtual_memory", "boot_time", "Process"):
        monkeypatch.setattr(fake, name, boom)
    assert platform.iter_processes() is None and platform.ppid(1) is None and platform.children(1) == []
    assert platform.cpu_pct("z") is None and platform.mem() is None and platform.uptime() is None
    assert platform.listening_ports([1]) == {}


def test_cpu_mem_uptime_over_a_fake_psutil(monkeypatch):
    fake = use_fake_psutil(monkeypatch, FakePsutil(cpu=(100.0, 50.0), vm=(1000, 250), boot=time.time() - 5000))
    assert platform.cpu_pct("a") is None and platform.cpu_pct("b") is None, "the first reading has nothing to diff"
    fake.cpu = (100.0, 150.0)                         # +100 s busy, +0 idle
    assert platform.cpu_pct("a") == 100.0
    fake.cpu = (200.0, 150.0)                         # a: +100 idle since its last reading; b: 100 idle, 100 busy since its first
    assert platform.cpu_pct("a") == 0.0 and platform.cpu_pct("b") == 50.0
    assert platform.cpu_pct("a") is None, "no time passed"
    assert platform.mem() == {"total": 1000, "used": 750, "pct": 75.0}
    assert set(platform.mem()) == {"total", "used", "pct"}, "the shape health has always returned"
    assert 5000 <= platform.uptime() < 5010
    assert health._cpu_pct("a") is None and health._mem() == platform.mem()


def test_psutil_is_a_requirement_off_linux_only():
    reqs = {}
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        if line.strip():
            r = Requirement(line)
            reqs[r.name] = r
    r = reqs["psutil"]
    assert {str(x) for x in r.specifier} == {">=6", "<8"} and r.marker is not None
    assert r.marker.evaluate({"sys_platform": "linux"}) is False, "the Linux image and venv do not change"
    assert r.marker.evaluate({"sys_platform": "darwin"}) is True and r.marker.evaluate({"sys_platform": "win32"}) is True
    for f in ("requirements.in", "requirements.lock", "requirements-dev.lock"):
        assert "psutil" not in (ROOT / f).read_text().lower(), f


# ---------------------------------------------------------------- the callers

def test_foreign_processes_counts_over_a_fake_psutil(monkeypatch):
    use_fake_psutil(monkeypatch, FakePsutil({
        1: {"name": "launchd", "ppid": 0},
        100: {"name": "codex", "ppid": 1},                                   # a daemon the board did not start
        101: {"name": "codex", "ppid": 500},                                 # one in the board's pane 500 ...
        500: {"name": "zsh", "ppid": 400},
        102: {"name": "codex", "ppid": 101},                                 # ... and its child
        103: {"name": "node", "ppid": 1},
    }, denied=()))
    assert codex_agent.foreign_process_count([]) == 3
    assert codex_agent.foreign_process_count([500]) == 1
    assert codex_agent.foreign_process_count([500, 100]) == 0
    assert codex_agent.foreign_processes([]) == 3


def test_an_unreadable_process_list_is_unknown_never_none(monkeypatch):
    no_psutil(monkeypatch)
    assert codex_agent.foreign_process_count([]) is None
    assert codex_agent.foreign_processes([]) == 0, "the int form keeps its old answer"


def test_the_codex_warning_never_claims_none_when_the_count_is_unknown(monkeypatch):
    monkeypatch.setattr(cx.tmux, "list_sessions", lambda: {})
    for count, want in ((None, [cx.WARN_OTHER_UNKNOWN]), (0, []), (2, [cx.WARN_OTHER])):
        monkeypatch.setattr(cx.codex_agent, "foreign_process_count", lambda panes, c=count: c)
        assert cx._warnings() == want
    assert "previous login" in cx.WARN_OTHER_UNKNOWN and "could not" in cx.WARN_OTHER_UNKNOWN
    monkeypatch.setattr(cx.codex_agent, "foreign_process_count", lambda panes: 1 / 0)
    assert cx._warnings() == [], "an error is not a claim either way"


def test_env_leaks_over_a_fake_psutil_names_only(monkeypatch):
    use_fake_psutil(monkeypatch, FakePsutil({
        10: {"name": "w", "ppid": 1, "environ": {"PATH": "/usr/bin", "CCBOARD_SESSION": "proj--repo--s1", "TMUX_PANE": "%0"}},
        11: {"name": "w", "ppid": 1, "environ": {"PATH": "/usr/bin", "HOME": "/home/x"}},
        12: {"name": "w", "ppid": 1, "environ": {"A": "1"}},
    }, denied={12}))
    assert memory.env_leaks(10) == ["CCBOARD_SESSION", "TMUX_PANE"]
    assert memory.env_leaks(11) == []
    assert memory.env_leaks(12) is None and memory.env_leaks(99) is None
    for bad in (None, 0, -1, "10", True):
        assert memory.env_leaks(bad) is None


def test_memory_env_check_skips_and_says_psutil_when_it_cannot_read(monkeypatch, tmp_path):
    from app.config import settings
    use_fake_psutil(monkeypatch, FakePsutil({31337: {"name": "w", "ppid": 1, "environ": {"A": "1"}}}, denied={31337}))
    monkeypatch.setattr(doctor, "_mem_health", lambda ctx: {"pid": 31337})
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(settings, "runtime", "host")
    out = doctor._mem_env(None, {})
    assert out.status == "skip" and "psutil" in out.detail and "/proc" not in out.detail
    monkeypatch.setattr(platform, "IS_LINUX", True)
    monkeypatch.setattr(platform, "PROC_ROOT", tmp_path / "no-proc")
    out = doctor._mem_env(None, {})
    assert out.status == "skip" and out.detail == "could not read the worker's environment (another user's process, or no /proc here)"


def test_previews_find_dev_server_ports_through_the_platform_seam(monkeypatch):
    from app import previews
    use_fake_psutil(monkeypatch, FakePsutil({
        100: {"name": "zsh", "ppid": 1, "conns": []},
        200: {"name": "npm", "ppid": 100, "conns": []},
        300: {"name": "node", "ppid": 200, "conns": [(5173, "LISTEN")]},
        999: {"name": "stray", "ppid": 1, "conns": [(3000, "LISTEN")]},
    }))
    monkeypatch.setattr(previews, "infra_ports", lambda: {8000})              # no look at a real claude-mem worker
    assert previews.descendants(100) == {100, 200, 300}
    assert previews.ports_under(100) == [5173], "a listener outside the pane's tree is not offered"
    assert previews.ports_under(0) == []


def test_registry_walks_ancestors_over_a_fake_psutil(monkeypatch):
    from app.agents import registry
    use_fake_psutil(monkeypatch, FakePsutil({
        1: {"name": "launchd", "ppid": 0}, 500: {"name": "zsh", "ppid": 1}, 777: {"name": "sh", "ppid": 500}, 10004: {"name": "claude", "ppid": 777}}))
    assert registry._ancestors(10004, {}) == [777, 500]
    cache = {10004: [1]}
    assert registry._ancestors(10004, cache) == [1], "the per-scan cache is still used"


# ---------------------------------------------------------------- health: the WSL2 note

def test_health_snapshot_notes_wsl2_and_nothing_else_changes(monkeypatch):
    monkeypatch.setattr(platform, "is_wsl", lambda: False)
    plain = health.snapshot()
    assert "host_note" not in plain and set(plain) == {"host", "cpu_pct", "load1", "mem", "disk", "uptime_s", "cores", "at"}
    monkeypatch.setattr(platform, "is_wsl", lambda: True)
    wsl = health.snapshot()
    assert wsl["host_note"] == "wsl2" and wsl["host"] == plain["host"], "the host name is not overwritten"
    assert set(wsl) - set(plain) == {"host_note"}
