"""A fake /proc for the tests that read process facts through app.platform (one seam: platform.PROC_ROOT, read when platform.IS_LINUX).

`use_fake_proc(monkeypatch, root)` points the Linux branch at `root` (so it works on a Mac too); `fake_proc(root, pid, comm, ppid)` writes one
process the way the kernel shows it; `FakePsutil` is a stand-in psutil module for the non-Linux branch (install it with `use_fake_psutil`)."""
from __future__ import annotations

import sys
import types
from pathlib import Path


def use_fake_proc(monkeypatch, root) -> Path:
    from app import platform
    root = Path(root)
    monkeypatch.setattr(platform, "IS_LINUX", True)
    monkeypatch.setattr(platform, "PROC_ROOT", root)
    return root


def fake_proc(root: Path, pid: int, comm: str, ppid: int = 1, environ: bytes | None = None, children: list[int] | None = None) -> None:
    d = Path(root) / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "comm").write_text(comm + "\n")
    (d / "stat").write_text(f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1\n")
    if environ is not None:
        (d / "environ").write_bytes(environ)
    if children is not None:
        (d / "task" / str(pid)).mkdir(parents=True, exist_ok=True)
        (d / "task" / str(pid) / "children").write_text(" ".join(str(c) for c in children) + "\n")


class FakePsutil(types.ModuleType):
    """psutil as far as app.platform uses it. `table` is {pid: {name, ppid, environ, conns[(port, status)]}}; a pid missing from it is gone,
    `denied` is a set of pids whose reads raise AccessDenied, `calls` records what was asked (the system-wide net_connections is a trap)."""
    CONN_LISTEN = "LISTEN"

    class Error(Exception):
        pass

    class NoSuchProcess(Error):
        pass

    class AccessDenied(Error):
        pass

    def __init__(self, table=None, denied=(), cpu=(100.0, 50.0), vm=(1000, 250), boot=0.0):
        super().__init__("psutil")
        self.table = table or {}
        self.denied = set(denied)
        self.calls: list = []
        self.cpu, self.vm, self.boot = cpu, vm, boot
        outer = self

        class Process:
            def __init__(self, pid):
                outer.calls.append(("Process", pid))
                if pid not in outer.table:
                    raise outer.NoSuchProcess(pid)
                self.pid = pid

            def _row(self):
                if self.pid in outer.denied:
                    raise outer.AccessDenied(self.pid)
                return outer.table[self.pid]

            def ppid(self):
                return outer.table[self.pid]["ppid"]

            def name(self):
                return self._row()["name"]

            def environ(self):
                return dict(self._row().get("environ", {}))

            def children(self, recursive=False):
                outer.calls.append(("children", self.pid, recursive))
                return [Process(p) for p, r in outer.table.items() if r["ppid"] == self.pid]

            def net_connections(self, kind="inet"):
                outer.calls.append(("net_connections", self.pid, kind))
                row = self._row()
                return [types.SimpleNamespace(status=st, laddr=types.SimpleNamespace(ip="127.0.0.1", port=port)) for port, st in row.get("conns", [])]

        self.Process = Process

    def pids(self):
        return sorted(self.table)

    def net_connections(self, *a, **k):
        self.calls.append(("system_net_connections",))       # needs root on macOS: app.platform must never call it
        return []

    def cpu_times(self):
        idle, busy = self.cpu
        return types.SimpleNamespace(user=busy, system=0.0, idle=idle, _fields=("user", "system", "idle"))

    def virtual_memory(self):
        total, avail = self.vm
        return types.SimpleNamespace(total=total, available=avail)

    def boot_time(self):
        return self.boot


def use_fake_psutil(monkeypatch, fake: FakePsutil | None = None) -> FakePsutil:
    """Install `fake` as the psutil module and switch app.platform to its non-Linux branch."""
    from app import platform
    fake = fake or FakePsutil()
    monkeypatch.setitem(sys.modules, "psutil", fake)
    monkeypatch.setattr(platform, "IS_LINUX", False)
    return fake


def no_psutil(monkeypatch) -> None:
    """`import psutil` fails (setting the sys.modules entry to None makes the import raise ImportError) and app.platform takes its non-Linux branch."""
    from app import platform
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(platform, "IS_LINUX", False)
