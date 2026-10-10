"""Nodes epic P6, issue #154 (part): the Mac and WSL2 as nodes. The card on each, discovery on each, the four node-* Doctor rows, the README support table.

Fakes only: the system flags are patched (this suite runs on a Mac and on Linux alike), `/proc/version` is a temp file, Tailscale's command is
`app.tailscale._exec` replaced by a function that answers from a fixture file (and fails the test where no command may run), the Doctor's
subprocess helper is a table, every data dir is a temp dir and nothing opens a connection to another host. Linux is pinned: no `wsl` key on its
card, no command run by the WSL gate, no node-* row.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from app import account_store, doctor, doctor_nodes as dn, main, nodes, nodes_discovery as nd, nodes_hub
from app import platform as plat
from app import tailscale as ts
from app.config import settings
from app.db import DB
from tests.test_nodes_card import ID, MAC_SNAP, HOME_SNAP, cardenv, ts as ts_reading  # noqa: F401 (fixtures)

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
README = ROOT / "README.md"
REAL_IS_WSL = plat.is_wsl                 # the lru_cached reader, before a test patches it
REAL_TS_STATUS = nodes._ts_status          # captured at import, before conftest's autouse fixture replaces it
WSL_VERSION = "Linux version 5.15.167.4-microsoft-standard-WSL2 (root@build) (gcc 11.2.0) #1 SMP Tue Nov 5 00:21:55 UTC 2024\n"
HELLO = (200, json.dumps({"app": "ccboard", "api": 1, "node_id": "ts:nPEER1CNTRL"}).encode())


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def proc_version(monkeypatch, tmp_path, text: str | None) -> None:
    """Point the real is_wsl() at a temp file instead of /proc/version (it reads that fixed path through plat.Path and caches the answer)."""
    real = plat.Path
    f = tmp_path / "version"
    if text is None:
        f.unlink(missing_ok=True)
    else:
        f.write_text(text)
    monkeypatch.setattr(plat, "Path", lambda p, *a: real(f) if p == "/proc/version" else real(p, *a))
    monkeypatch.setattr(plat, "IS_LINUX", True)
    REAL_IS_WSL.cache_clear()


def flags(monkeypatch, system: str) -> None:
    monkeypatch.setattr(plat, "IS_LINUX", system in ("linux", "wsl"))
    monkeypatch.setattr(plat, "IS_MACOS", system == "macos")
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    monkeypatch.setattr(plat, "is_wsl", lambda: system == "wsl")


class Exec:
    """app.tailscale._exec: answers `status --json` from a fixture and records every call; with no fixture any call fails the test."""

    def __init__(self, fixture: str | None):
        self.fixture = fixture
        self.calls: list[tuple] = []

    def __call__(self, argv, env, timeout):
        self.calls.append((list(argv), dict(env)))
        if self.fixture is None:
            raise AssertionError(f"no Tailscale command may run here: {argv}")
        return subprocess.CompletedProcess(argv, 0, (FIX / self.fixture).read_text(), "")


@pytest.fixture
def wsl_box(tmp_path, projects_dir, monkeypatch):
    """A distro with no Tailscale in it: no `tailscale` on PATH, none at the fallback path, no placement setting, and a command runner that fails the test."""
    flags(monkeypatch, "wsl")
    monkeypatch.setattr(ts.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(ts, "LINUX_FALLBACK", str(tmp_path / "usr-bin-tailscale-is-not-here"))
    monkeypatch.delenv("CCBOARD_TAILSCALE_PLACEMENT", raising=False)
    ex = Exec(None)
    monkeypatch.setattr(ts, "_exec", ex)
    monkeypatch.setattr(nodes, "_ts_status", REAL_TS_STATUS)
    monkeypatch.setattr(nodes, "_resolve_all", lambda host, port: ["100.100.1.5"])
    nodes.reset()
    return ex


def interop_exe(tmp_path, monkeypatch) -> str:
    """tailscale.exe is reachable on PATH through Windows interop (a file in a temp dir)."""
    exe = tmp_path / "winbin" / "tailscale.exe"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(ts.shutil, "which", lambda name, *a, **k: str(exe) if name == "tailscale.exe" else None)
    return str(exe)


@pytest.fixture
def mac_app(tmp_path, projects_dir, monkeypatch):
    """A Mac with only the Tailscale app: the CLI is the app bundle's own binary (run with TAILSCALE_BE_CLI=1), nothing on PATH, no Homebrew."""
    flags(monkeypatch, "macos")
    app = tmp_path / "Tailscale.app"
    cli = app / "Contents" / "MacOS" / "Tailscale"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    monkeypatch.setattr(ts, "MAC_APP", app)
    monkeypatch.setattr(ts, "MAC_APP_CLI", cli)
    monkeypatch.setattr(ts, "MAS_RECEIPT", app / "Contents" / "_MASReceipt")
    monkeypatch.setattr(ts, "MAC_CLI_CANDIDATES", ())
    monkeypatch.setattr(ts, "MAC_DAEMON_CANDIDATES", ())
    monkeypatch.setattr(ts, "MAC_DAEMON_SOCKET", tmp_path / "no-daemon-socket")
    monkeypatch.setattr(ts.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(settings, "runtime", "launchd")
    monkeypatch.setattr(nodes, "_ts_status", REAL_TS_STATUS)
    nodes.reset()
    return cli


def fake_agents(monkeypatch):
    from app import agents
    monkeypatch.setattr(agents, "status_all", lambda: {
        "claude": {"installed": True, "version": "2.1.288", "loggedIn": True, "hooks": {"installed": True}},
        "codex": {"installed": False, "version": None, "loggedIn": False, "hooks": {"installed": False}}})


def sans_clock(card: dict) -> dict:
    return {k: v for k, v in card.items() if k != "now"}


# ---------------------------------------------------------------- the card on a Mac

def test_the_mac_card_has_null_load_launchd_and_no_saved_logins(lite_client, monkeypatch, mac_app):
    """health.snapshot() of None values (a Mac with no psutil and no /proc), the accounts gate false: the card validates and says so, never 0."""
    fake_agents(monkeypatch)
    monkeypatch.setattr(nodes._pf, "system", lambda: "Darwin")
    monkeypatch.setattr(nodes._pf, "release", lambda: "23.6.0")
    monkeypatch.setattr(account_store, "supported", lambda: False)
    monkeypatch.setattr(settings, "image_version", "")
    monkeypatch.setattr(settings, "public_url", "https://alice-mac.example.ts.net")
    monkeypatch.setattr(settings, "node_name", "")
    monkeypatch.setattr(ts, "_exec", Exec("ts_status_mac.json"))
    c = nodes.card(main.db, health_snap=MAC_SNAP, sessions=None)
    assert sans_clock(c) == load("node_card_mac.json")
    assert c["os"]["system"] == "Darwin" and c["runtime"] == "launchd" and "wsl" not in c["os"]
    assert set(c["load"].values()) == {None}, "unknown is null, never 0"
    assert c["accounts"]["supported"] is False and c["lanes"]["cap"] == 3
    clean = nodes_hub.clean_card(json.loads(json.dumps(c)), {"node_id": c["node_id"], "name": c["name"], "url": c["url"]})
    assert clean["load"] == c["load"] and clean["os"] == c["os"] and clean["accounts"]["supported"] is False, "a hub keeps all of it when it reads this card"


def test_the_mac_reads_tailscale_through_the_app_bundle_binary(mac_app, monkeypatch):
    """The reading goes through app/tailscale.py: the app's own binary with TAILSCALE_BE_CLI=1, nothing else on the Mac is asked."""
    ex = Exec("ts_status_mac.json")
    monkeypatch.setattr(ts, "_exec", ex)
    st, info = nd.read_tailscale(fresh=True)
    assert info["ok"] is True and info["variant"] == "macos-standalone" and nd.magic_suffix(st) == "example.ts.net"
    assert ex.calls == [([str(mac_app), "status", "--json"], {"TAILSCALE_BE_CLI": "1"})]
    assert nodes.node_id() == "ts:nMACX1", "the Mac's stable id from the same reading"


def test_a_mac_finds_the_linux_box_and_the_windows_machine_and_probes_both(mac_app, monkeypatch, tmp_path):
    monkeypatch.setattr(ts, "_exec", Exec("ts_status_mac.json"))
    monkeypatch.setattr(nodes, "_resolve_all", lambda host, port: ["100.100.1.1"])
    asked = []

    def transport(host, addr, port, timeout):
        asked.append((host, port))
        return HELLO
    db = DB(tmp_path / "d.db")
    try:
        out = nd.discover(db, refresh=True, transport=transport, resolver=lambda h, p: ["100.100.1.1"])
    finally:
        db.conn.close()
    assert out["tailscale"]["ok"] is True
    rows = {r["name"]: r for r in out["rows"]}
    assert set(rows) == {"node-a", "desk-wsl"}, "never this Mac"
    assert rows["desk-wsl"]["os"] == "windows" and rows["node-a"]["os"] == "linux"
    assert {r["state"] for r in out["rows"]} == {"found"}
    assert rows["desk-wsl"]["url"] == "https://desk-wsl.example.ts.net", "the probe covers 443 first (a Mac's default) and then 8443"
    assert ("desk-wsl.example.ts.net", 443) in asked


# ---------------------------------------------------------------- the card on WSL2

def test_wsl_is_told_by_proc_version_naming_microsoft(tmp_path, monkeypatch):
    try:
        proc_version(monkeypatch, tmp_path, WSL_VERSION)
        assert REAL_IS_WSL() is True
        proc_version(monkeypatch, tmp_path, "Linux version 6.8.0-45-generic (buildd@lcy02) (gcc 13.2.0) #45-Ubuntu SMP\n")
        assert REAL_IS_WSL() is False
        proc_version(monkeypatch, tmp_path, None)
        assert REAL_IS_WSL() is False, "no file, no WSL"
    finally:
        monkeypatch.undo()
        REAL_IS_WSL.cache_clear()


def test_the_wsl_card_says_wsl_has_a_random_id_and_takes_its_address_from_the_setting(lite_client, monkeypatch, tmp_path, wsl_box):
    """WSL is told by the real reader on a patched /proc/version; the distro has no Tailscale client, so no command runs and the id is random."""
    fake_agents(monkeypatch)
    monkeypatch.setattr(plat, "is_wsl", REAL_IS_WSL)
    proc_version(monkeypatch, tmp_path, WSL_VERSION)
    try:
        monkeypatch.setattr(nodes._pf, "system", lambda: "Linux")
        monkeypatch.setattr(nodes._pf, "release", lambda: "5.15.167.4-microsoft-standard-WSL2")
        monkeypatch.setattr(account_store, "supported", lambda: True)
        monkeypatch.setattr(settings, "image_version", "")
        monkeypatch.setattr(settings, "runtime", "systemd")
        monkeypatch.setattr(settings, "node_name", "desk-wsl")
        monkeypatch.setattr(settings, "public_url", "https://desk-wsl.example.ts.net:8443")
        c = nodes.card(main.db, health_snap=HOME_SNAP, sessions=[])
    finally:
        REAL_IS_WSL.cache_clear()
    assert wsl_box.calls == [], "no Tailscale command ran: the client is on the Windows side"
    assert re.fullmatch(r"n_[0-9a-f]{16}", c["node_id"])
    assert sans_clock({**c, "node_id": "n_<16 hex>"}) == load("node_card_wsl.json")
    assert c["os"]["wsl"] is True and c["url"] == "https://desk-wsl.example.ts.net:8443" and c["name"] == "desk-wsl"
    nodes_hub.clean_card(json.loads(json.dumps(c)), {"node_id": c["node_id"], "name": c["name"], "url": c["url"]})


def test_a_linux_box_is_unchanged(lite_client, monkeypatch, tmp_path):
    """No `wsl` key on the card, the WSL gate runs no command and looks at nothing, and the status reading is the one it always was."""
    flags(monkeypatch, "linux")
    monkeypatch.setattr(plat, "is_wsl", lambda: False)
    monkeypatch.setattr(ts, "find_cli", lambda: (_ for _ in ()).throw(AssertionError("the WSL gate must not look for the command on a plain Linux box")))
    assert nodes.windows_side() is False
    assert "wsl" not in nodes._os_view({"Self": {"OS": "linux"}})
    assert nodes._os_view({"Self": {"OS": "linux"}}).keys() == {"system", "release", "tailscale_os"}
    monkeypatch.setattr(ts, "find_cli", lambda: ts.Cli(["/usr/bin/tailscale"], {}))
    ex = Exec("ts_status_mac.json")
    monkeypatch.setattr(ts, "_exec", ex)
    assert REAL_TS_STATUS()["Self"]["ID"] == "nMACX1" and ex.calls == [(["/usr/bin/tailscale", "status", "--json"], {})]


# ---------------------------------------------------------------- discovery on WSL2

def test_wsl_without_a_tailscale_client_says_so_in_one_sentence_and_runs_nothing(wsl_box, tmp_path):
    db = DB(tmp_path / "d.db")
    try:
        out = nd.discover(db, refresh=True, transport=lambda *a: (_ for _ in ()).throw(AssertionError("nothing is probed")))
    finally:
        db.conn.close()
    assert out["tailscale"]["ok"] is False and out["rows"] == []
    assert out["tailscale"]["reason"] == "Tailscale runs on the Windows side: type the other node's address in Pair a node"
    assert wsl_box.calls == []


def test_the_discover_route_answers_200_with_the_windows_side_reason_on_wsl(lite_client, wsl_box):
    r = lite_client.get("/api/nodes/discover?refresh=1", headers={**ID, "X-CCBoard": "1"})
    assert r.status_code == 200 and r.json()["tailscale"]["reason"] == nodes.WINDOWS_SIDE and r.json()["rows"] == []
    assert wsl_box.calls == []


def test_tailscale_exe_through_interop_is_off_by_default(wsl_box, tmp_path, monkeypatch):
    interop_exe(tmp_path, monkeypatch)
    assert ts.find_cli().exe.endswith("tailscale.exe"), "the command is there; the board is what leaves it alone"
    assert nodes.windows_side() is True
    st, info = nd.read_tailscale(fresh=True)
    assert st is None and info["reason"] == nodes.WINDOWS_SIDE and wsl_box.calls == []
    assert nodes._ts_status() is None and nodes.node_id().startswith("n_")
    monkeypatch.setenv("CCBOARD_TAILSCALE_PLACEMENT", "wsl")              # saying Tailscale is in the distro does not turn the Windows one on
    assert nodes.windows_side() is True


def test_tailscale_exe_is_used_only_with_the_host_placement_setting(wsl_box, tmp_path, monkeypatch):
    exe = interop_exe(tmp_path, monkeypatch)
    monkeypatch.setenv("CCBOARD_TAILSCALE_PLACEMENT", "host")
    ex = Exec("ts_status_wsl.json")
    monkeypatch.setattr(ts, "_exec", ex)
    assert nodes.windows_side() is False
    st, info = nd.read_tailscale(fresh=True)
    assert info["ok"] is True and info["variant"] == "wsl-host" and ex.calls == [([exe, "status", "--json"], {})]
    rows = {r["name"]: r for r in nd.candidates(st, settings.node_tags, nd.self_user(st))}
    assert set(rows) == {"node-a", "alice-mac"} and rows["alice-mac"]["os"] == "macOS"
    assert nodes.node_id() == "ts:nWINX1"


def test_a_distro_with_its_own_tailscale_is_not_on_the_windows_side(wsl_box, tmp_path, monkeypatch):
    own = tmp_path / "bin" / "tailscale"
    own.parent.mkdir()
    own.write_text("#!/bin/sh\n")
    own.chmod(0o755)
    monkeypatch.setattr(ts.shutil, "which", lambda name, *a, **k: str(own) if name == "tailscale" else None)
    assert nodes.windows_side() is False


# ---------------------------------------------------------------- the Doctor rows

class Table:
    """The Doctor's subprocess helper as a table: {program name: Proc | Exception}; a program not in it is missing."""

    def __init__(self, monkeypatch, **cmds):
        self.cmds, self.calls = cmds, []
        monkeypatch.setattr(doctor, "_run", self)

    def __call__(self, argv, timeout=doctor.CMD_TIMEOUT):
        self.calls.append(list(argv))
        r = self.cmds.get(Path(argv[0]).name)
        if isinstance(r, Exception):
            raise r
        if r is None:
            raise doctor.ToolMissing(Path(argv[0]).name)
        return r


PMSET_HELD = """Assertion status system-wide:
   PreventUserIdleSystemSleep     1
Listed by owning process:
   pid 4242(caffeinate): [0x0000000100000001] 00:12:01 PreventUserIdleSystemSleep named: "caffeinate command-line tool"
"""
PMSET_FREE = """Assertion status system-wide:
   PreventUserIdleSystemSleep     0
   PreventSystemSleep             0
Listed by owning process:
   pid 77(powerd): [0x0000000100000002] 00:00:03 NoDisplaySleepAssertion named: "x"
"""
TASK = "\nTaskPath   TaskName                  State\n--------   --------                  -----\n\\          ccboard-wsl-keepalive     {state}\n\n"


@pytest.fixture
def mac_doctor(monkeypatch):
    flags(monkeypatch, "macos")
    monkeypatch.setattr(plat, "_hint_family", lambda: "macos")
    monkeypatch.setattr(settings, "runtime", "launchd")


@pytest.fixture
def wsl_doctor(monkeypatch):
    flags(monkeypatch, "wsl")
    monkeypatch.setattr(plat, "wsl_interop", lambda: True)
    from app import doctor_wsl
    monkeypatch.setattr(doctor_wsl, "_powershell", lambda: "powershell.exe")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")


def test_the_four_rows_are_registered_once_in_group_box(monkeypatch):
    monkeypatch.setattr(doctor, "CHECKS", [])
    monkeypatch.setattr(doctor, "GROUPS", ["box"])
    dn.register_all()
    dn.register_all()
    assert [c[0] for c in doctor.CHECKS] == ["node-mac-public-url", "node-sleep", "node-wsl-keepalive", "node-gh"]
    assert {c[1] for c in doctor.CHECKS} == {"box"} and all(c[2] for c in doctor.CHECKS)
    assert [c[0] for c in dn.CHECKS] == [c[0] for c in doctor.CHECKS]


def test_a_linux_box_lists_none_of_them_and_every_check_skips_there(monkeypatch):
    flags(monkeypatch, "linux")
    plat_src = (ROOT / "app" / "doctor.py").read_text()
    assert re.search(r"if plat\.IS_MACOS or plat\.is_wsl\(\):[^\n]*\n\s+from \. import doctor_nodes", plat_src), "imported on a Mac and in WSL only"
    Table(monkeypatch)
    out = {cid: fn(None) for cid, _g, _l, fn in dn.CHECKS}
    assert {cid: o.status for cid, o in out.items()} == {"node-mac-public-url": "skip", "node-sleep": "skip", "node-wsl-keepalive": "skip", "node-gh": "skip"}
    assert "Linux box has the gh row" in out["node-gh"].detail


def test_mac_public_url_names_the_setting_and_where_it_goes(mac_doctor, monkeypatch):
    monkeypatch.setattr(settings, "public_url", "")
    out = dn._c_mac_public_url(None)
    assert out.status == "warn" and "CCBOARD_PUBLIC_URL is empty" in out.detail
    assert "<data dir>/env" in out.fix["text"] and out.fix["cmd"] == "CCBOARD_PUBLIC_URL=https://<this-mac>.<tailnet>.ts.net"
    monkeypatch.setattr(settings, "public_url", "http://alice-mac:8000")
    out = dn._c_mac_public_url(None)
    assert out.status == "warn" and "not an https address" in out.detail and out.fix["text"]
    monkeypatch.setattr(settings, "public_url", "https://alice-mac.example.ts.net")
    out = dn._c_mac_public_url(None)
    assert out.status == "pass" and out.fix is None
    monkeypatch.setattr(plat, "IS_MACOS", False)
    assert dn._c_mac_public_url(None).status == "skip"


def test_node_sleep_warns_that_a_sleeping_mac_goes_stale_and_names_caffeinate(mac_doctor, monkeypatch):
    t = Table(monkeypatch, pmset=doctor.Proc(0, PMSET_FREE, ""))
    out = dn._c_sleep(None)
    assert out.status == "warn" and "stale" in out.detail and "not broken" in out.detail
    assert "caffeinate -s" in out.fix["text"] and "to verify" in out.fix["text"] and out.fix["cmd"] == "caffeinate -s"
    assert t.calls == [["pmset", "-g", "assertions"]], "read only"
    t.cmds["pmset"] = doctor.Proc(0, PMSET_HELD, "")
    out = dn._c_sleep(None)
    assert out.status == "pass" and "caffeinate" in out.detail and "to verify" in out.detail
    t.cmds["pmset"] = doctor.Proc(1, "", "boom")
    assert dn._c_sleep(None).status == "skip", "an unreadable answer is unknown, not a pass"
    t.cmds["pmset"] = doctor.ToolTimeout("pmset", 4.0)
    assert dn._c_sleep(None).status == "warn"
    del t.cmds["pmset"]
    assert dn._c_sleep(None).status == "skip"


def test_node_wsl_keepalive_words_the_existing_probe_for_the_node(wsl_doctor, monkeypatch):
    t = Table(monkeypatch, **{"powershell.exe": doctor.Proc(0, TASK.format(state="Running"), "")})
    out = dn._c_wsl_keepalive(None)
    assert out.status == "pass" and "stays online" in out.detail
    assert t.calls[-1] == ["powershell.exe", "-NoProfile", "-Command", "Get-ScheduledTask -TaskName ccboard-wsl-keepalive"], "read only"
    t.cmds["powershell.exe"] = doctor.Proc(1, "", "No MSFT_ScheduledTask objects found")
    out = dn._c_wsl_keepalive(None)
    assert out.status == "warn" and "goes stale" in out.detail
    assert "ccboard-wsl-keepalive.ps1" in out.fix["text"] and "-Distro Ubuntu" in out.fix["cmd"]
    monkeypatch.setattr(plat, "wsl_interop", lambda: False)
    out = dn._c_wsl_keepalive(None)
    assert out.status == "skip" and "unknown" in out.detail and out.fix["text"], "unknown stays unknown"
    monkeypatch.setattr(plat, "is_wsl", lambda: False)
    assert dn._c_wsl_keepalive(None).status == "skip"


def test_node_gh_reads_only_the_exit_code_and_names_the_fix(mac_doctor, monkeypatch):
    t = Table(monkeypatch, gh=doctor.Proc(0, "Logged in to github.com account someone (keyring)\n", ""))
    out = dn._c_gh(None)
    assert out.status == "pass" and "someone" not in out.detail and out.fix is None
    assert t.calls == [["gh", "auth", "status"]]
    t.cmds["gh"] = doctor.Proc(1, "", "You are not logged into any GitHub hosts. To log in, run: gh auth login")
    out = dn._c_gh(None)
    assert out.status == "warn" and out.fix["cmd"] == "gh auth login"
    t.cmds.clear()
    out = dn._c_gh(None)
    assert out.status == "warn" and "not installed" in out.detail and out.fix["cmd"] == "brew install gh"
    t.cmds["gh"] = doctor.ToolTimeout("gh", 15.0)
    assert dn._c_gh(None).status == "skip"


def test_node_gh_also_runs_in_wsl(wsl_doctor, monkeypatch):
    Table(monkeypatch, gh=doctor.Proc(1, "", "not logged in"))
    out = dn._c_gh(None)
    assert out.status == "warn" and out.fix["cmd"] == "gh auth login"


def test_the_tailscale_status_row_is_a_skip_with_the_sentence_on_the_windows_side(wsl_box, tmp_path):
    out = doctor._c_tailscale_status(None)
    assert out.status == "skip" and out.detail == nodes.WINDOWS_SIDE and wsl_box.calls == []


# ---------------------------------------------------------------- the README table

def readme_table() -> tuple[list[str], list[list[str]]]:
    text = README.read_text(encoding="utf-8")
    start = text.index("#### Several devices")
    rows = []
    for ln in text[start:].splitlines()[1:]:
        if ln.startswith("#"):
            break
        if ln.startswith("|"):
            rows.append([c.strip() for c in ln.strip().strip("|").split("|")])
    assert len(rows) > 2, "the table is missing"
    return rows[0], [r for r in rows[2:]]


def test_the_support_table_rows_are_the_platform_list_in_the_code():
    head, body = readme_table()
    assert [r[0] for r in body] == list(nodes.SUPPORT_PLATFORMS), "the README table and app/nodes.py SUPPORT_PLATFORMS drifted apart"
    assert nodes.SUPPORT_PLATFORMS == ("Linux box", "Linux container", "Mac", "WSL2", "Native Windows")


def test_the_support_table_answers_every_question_for_every_platform():
    head, body = readme_table()
    want = ["Platform", "Runs a node", "Discovery", "Pairing", "Relay targets", "Terminals", "Tail tiles", "Epic work", "Notifications", "Saved accounts", "Caveats"]
    assert head == want
    for row in body:
        assert len(row) == len(want) and all(c for c in row), row
    by = {r[0]: dict(zip(want, r)) for r in body}
    assert "Web Push only" in by["Mac"]["Notifications"] and "not available" in by["Mac"]["Saved accounts"]
    assert "Windows side" in by["WSL2"]["Discovery"] and "CCBOARD_NODE_NAME" in by["WSL2"]["Caveats"] and "keep-alive" in by["WSL2"]["Caveats"]
    assert by["Native Windows"]["Runs a node"].lower().startswith("no")
    assert "arm64" not in json.dumps(by) or "Docker Desktop" in json.dumps(by), "an arm64 image does not imply Docker Desktop support"


def test_the_readme_names_the_mac_browser_limit_and_the_decision_without_a_bypass():
    text = README.read_text(encoding="utf-8")
    sec = text[text.index("#### Several devices"):]
    sec = sec[:sec.index("\n## ")]
    assert "#127" in sec and "another device" in sec and "caffeinate -s" in sec and "to verify" in sec
    assert "CCBOARD_DEV_BYPASS_USER" not in sec


def test_where_ccboard_runs_has_one_line_per_platform_of_the_support_table():
    """Issue #115: the short support section and the node platform table share the platform list, so they cannot disagree."""
    text = README.read_text(encoding="utf-8")
    sec = text[text.index("## Where ccboard runs"):]
    sec = sec[3:sec.index("\n## ", 3)]
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in sec.splitlines() if ln.startswith("|")][2:]
    assert [r[0] for r in rows] == list(nodes.SUPPORT_PLATFORMS)
    by = {r[0]: r[1] for r in rows}
    assert by["Native Windows"].startswith("Not supported")
    assert "arm64 image is not Docker Desktop support" in by["Linux container"]
    assert "another device" in by["Mac"] and "to verify" in by["Mac"] and "to verify" in by["WSL2"]
    top = text[:text.index("## What v0.1 does")]
    assert "(#where-ccboard-runs-issue-115)" in top, "linked from the top of the README"
    assert "(#where-ccboard-runs-issue-115)" in text[text.index("## Install"):text.index("### Settings")], "and from Install"
