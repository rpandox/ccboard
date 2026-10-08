"""Issue #9: the optional claude-mem viewer through `tailscale serve` (CCBOARD_MEM_HTTPS_PORT).

install.sh's viewer helpers (the blocks between their marker lines, plus serve_apply) run in a throwaway bash with a stubbed sudo and a
fake `tailscale` that records every call and answers `serve status --json` from a file. Nothing here touches a real tailnet.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
FQDN = "box.example.ts.net"

FAKE_TAILSCALE = r"""#!/bin/sh
echo "$*" >> "$T/tailscale.log"
if [ "$1 $2 $3" = "serve status --json" ]; then cat "$T/serve.json" 2>/dev/null || echo null; exit 0; fi
exit 0
"""

PREAMBLE = r"""
set -euo pipefail
log()  { echo "== $*"; }
note() { echo "   $*"; }
warn() { echo "warning: $*" >&2; }
die()  { echo "error: $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
HOME_DIR=$HOME
TS_FQDN=box.example.ts.net
CCBOARD_HTTPS_PORT=8443 CODE_HTTPS_PORT=10000 NTFY_HTTPS_PORT=8444
sudo() { echo "sudo $*" >> "$T/sudo.log"; "$@"; }
timeout() { shift; "$@"; }
"""


def _blocks():
    t = INSTALL.read_text()
    out = ""
    for name in ("serve and claude-mem viewer helpers", "claude-mem viewer step"):
        m = re.search(rf"^# >>> {name}\n(.*?)^# <<< {name}\n", t, re.S | re.M)
        assert m, f"install.sh lost its '{name}' markers"
        out += m.group(1)
    fn = re.search(r"^serve_apply\(\) \{.*?^\}\n", t, re.S | re.M)
    assert fn
    return out + fn.group(0)


class Box:
    def __init__(self, tmp):
        self.tmp = tmp
        self.home = tmp / "home"
        (self.home / ".claude-mem").mkdir(parents=True)
        self.fakebin = tmp / "fakebin"
        self.fakebin.mkdir()
        (self.fakebin / "python3").symlink_to(sys.executable)
        ts = self.fakebin / "tailscale"
        ts.write_text(FAKE_TAILSCALE)
        ts.chmod(0o755)

    def serving(self, web):
        (self.tmp / "serve.json").write_text(json.dumps({"TCP": {p: {"HTTPS": True} for p in {k.split(":")[1] for k in web}}, "Web": web}))

    def bash(self, body, **env):
        e = {"PATH": f"{self.fakebin}:/usr/bin:/bin", "HOME": str(self.home), "T": str(self.tmp)}
        e.update(env)
        return subprocess.run(["bash", "-c", PREAMBLE + _blocks() + "\n" + body + "\n"], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=60)

    def ts_calls(self):
        f = self.tmp / "tailscale.log"
        return f.read_text().splitlines() if f.exists() else []

    def mutating(self):
        return [c for c in self.ts_calls() if not c.startswith("serve status")]


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def _handler(port, target):
    return {f"{FQDN}:{port}": {"Handlers": {"/": {"Proxy": target}}}}


# ------------------------------------------------------------------ the refusal list (before any change, no tailscale command)
@pytest.mark.parametrize("value", ["443", "8443", "10000", "8444"])
def test_a_port_the_board_or_another_service_holds_is_refused_with_no_tailscale_call(box, value):
    r = box.bash(f'mem_viewer_port_check {value} 8443 10000 8444; echo SURVIVED')
    assert r.returncode == 1 and "SURVIVED" not in r.stdout and "is taken" in r.stdout and value in r.stdout
    assert box.ts_calls() == [] and not (box.tmp / "sudo.log").exists()


@pytest.mark.parametrize("value", ["abc", "0", "65536", "-5", "80 90", "1e3", "０"])
def test_a_value_that_is_not_a_port_is_refused(box, value):
    r = box.bash(f'mem_viewer_port_check "{value}" 8443 10000 8444; echo SURVIVED')
    assert r.returncode == 1 and "SURVIVED" not in r.stdout and "must be empty" in r.stdout
    assert box.ts_calls() == []


@pytest.mark.parametrize("value", ["", "10443", "1", "65535"])
def test_empty_and_a_free_port_pass(box, value):
    r = box.bash(f'mem_viewer_port_check "{value}" 8443 10000 8444; echo SURVIVED')
    assert r.returncode == 0 and "SURVIVED" in r.stdout, r.stdout


def test_a_port_served_by_something_else_stops_before_any_change_unless_replacing_is_allowed(box):
    box.serving({f"{FQDN}:10443": {"Handlers": {"/": {"Proxy": "http://example.invalid:80"}}}})
    r = box.bash('CCBOARD_MEM_HTTPS_PORT=10443; mem_viewer_precheck; echo SURVIVED')
    assert r.returncode == 1 and "already served by something else" in r.stdout and "SURVIVED" not in r.stdout
    assert box.mutating() == [] and not (box.tmp / "sudo.log").exists()
    r = box.bash('CCBOARD_MEM_HTTPS_PORT=10443; CCBOARD_REPLACE_SERVE=1; mem_viewer_precheck; echo SURVIVED')
    assert r.returncode == 0 and "SURVIVED" in r.stdout


def test_funnel_on_the_port_is_a_refusal(box):
    box.serving(_handler(10443, "http://127.0.0.1:37700"))
    j = json.loads((box.tmp / "serve.json").read_text())
    j["AllowFunnel"] = {f"{FQDN}:10443": True}
    (box.tmp / "serve.json").write_text(json.dumps(j))
    r = box.bash('CCBOARD_MEM_HTTPS_PORT=10443; mem_viewer_precheck')
    assert r.returncode == 1 and "funnel" in r.stdout


def test_precheck_does_nothing_when_unset(box):
    r = box.bash('CCBOARD_MEM_HTTPS_PORT=; mem_viewer_precheck; echo SURVIVED')
    assert "SURVIVED" in r.stdout and box.ts_calls() == []


# ------------------------------------------------------------------ the worker port, in the order of app/memory.py
def test_worker_port_order_env_then_pid_file_then_settings_then_default(box):
    mem = box.home / ".claude-mem"
    uid_default = 37700 + int(subprocess.run(["id", "-u"], capture_output=True, text=True).stdout) % 100
    assert box.bash("mem_worker_port").stdout.strip() == str(uid_default)
    (mem / "settings.json").write_text('{"CLAUDE_MEM_WORKER_PORT": "37811"}')
    assert box.bash("mem_worker_port").stdout.strip() == "37811"
    (mem / "worker.pid").write_text('{"pid": 4242, "port": 37822}')
    assert box.bash("mem_worker_port").stdout.strip() == "37822"
    assert box.bash("CCBOARD_MEM_PORT=37833; mem_worker_port").stdout.strip() == "37833"
    assert box.bash("CCBOARD_MEM_PORT=70000; mem_worker_port").stdout.strip() == "37822", "an invalid override falls through, as the app does"
    (mem / "worker.pid").write_text("not json")
    assert box.bash("mem_worker_port").stdout.strip() == "37811"


# ------------------------------------------------------------------ the mapping
def test_unset_changes_nothing(box):
    r = box.bash('CCBOARD_MEM_HTTPS_PORT=; PREV_MEM_HTTPS_PORT=; mem_viewer_step')
    assert r.returncode == 0 and box.ts_calls() == [] and not (box.tmp / "sudo.log").exists()


def test_a_free_port_gets_exactly_one_serve_call_to_the_worker(box):
    (box.home / ".claude-mem" / "worker.pid").write_text('{"pid": 1, "port": 37850}')
    r = box.bash('CCBOARD_MEM_HTTPS_PORT=10443; PREV_MEM_HTTPS_PORT=; mem_viewer_step')
    assert r.returncode == 0, r.stdout
    assert box.mutating() == ["serve --bg --https=10443 http://127.0.0.1:37850"]
    assert "every device on your tailnet can open and change the claude-mem worker" in r.stdout
    assert not [c for c in box.ts_calls() if "funnel" in c or "reset" in c]


def test_an_existing_mapping_is_left_alone_and_a_stale_worker_port_is_replaced(box):
    (box.home / ".claude-mem" / "worker.pid").write_text('{"pid": 1, "port": 37850}')
    box.serving(_handler(10443, "http://127.0.0.1:37850"))
    box.bash('CCBOARD_MEM_HTTPS_PORT=10443; PREV_MEM_HTTPS_PORT=10443; mem_viewer_step')
    assert box.mutating() == [], "already ours: no call"
    box.serving(_handler(10443, "http://127.0.0.1:37700"))
    box.bash('CCBOARD_MEM_HTTPS_PORT=10443; PREV_MEM_HTTPS_PORT=10443; mem_viewer_step')
    assert box.mutating() == ["serve --https=10443 --set-path / off", "serve --bg --https=10443 http://127.0.0.1:37850"]


def test_clearing_turns_off_only_the_port_the_previous_run_set(box):
    (box.home / ".claude-mem" / "worker.pid").write_text('{"pid": 1, "port": 37850}')
    web = {**_handler(10443, "http://127.0.0.1:37850"), **_handler(8443, "http://127.0.0.1:8000"), **_handler(10000, "http://127.0.0.1:8080")}
    box.serving(web)
    r = box.bash('CCBOARD_MEM_HTTPS_PORT=; PREV_MEM_HTTPS_PORT=10443; mem_viewer_step')
    assert r.returncode == 0, r.stdout
    assert box.mutating() == ["serve --https=10443 off"]


def test_changing_the_port_turns_off_the_old_one_and_maps_the_new_one(box):
    (box.home / ".claude-mem" / "worker.pid").write_text('{"pid": 1, "port": 37850}')
    box.serving(_handler(10443, "http://127.0.0.1:37850"))
    box.bash('CCBOARD_MEM_HTTPS_PORT=10444; PREV_MEM_HTTPS_PORT=10443; mem_viewer_step')
    assert box.mutating() == ["serve --https=10443 off", "serve --bg --https=10444 http://127.0.0.1:37850"]


def test_a_remembered_port_that_is_not_a_ccboard_mapping_is_never_turned_off(box):
    box.serving({f"{FQDN}:10443": {"Handlers": {"/": {"Proxy": "http://example.invalid:80"}}}})
    r = box.bash('CCBOARD_MEM_HTTPS_PORT=; PREV_MEM_HTTPS_PORT=10443; mem_viewer_step')
    assert r.returncode == 0 and box.mutating() == [] and "left alone" in r.stdout
    box.serving(_handler(443, "http://127.0.0.1:37850"))
    r = box.bash('CCBOARD_MEM_HTTPS_PORT=; PREV_MEM_HTTPS_PORT=443; mem_viewer_step')
    assert box.mutating() == [], "443 is never touched, even if a file says so"


def test_a_missing_old_mapping_is_not_an_error(box):
    r = box.bash('CCBOARD_MEM_HTTPS_PORT=; PREV_MEM_HTTPS_PORT=10443; mem_viewer_step')
    assert r.returncode == 0 and box.mutating() == []


# ------------------------------------------------------------------ install.sh wiring
def test_install_sh_wiring():
    t = INSTALL.read_text()
    assert subprocess.run(["bash", "-n", str(INSTALL)]).returncode == 0
    assert t.index("\nmem_viewer_precheck\n") < t.index("\nlog \"config and systemd units\""), "refused before the env file or any unit is written"
    assert t.index("\nmem_viewer_step\n") > t.index('serve_apply "$CODE_HTTPS_PORT" /'), "the viewer is mapped after the board's own mappings"
    assert "mem_viewer_port_check \"$CCBOARD_MEM_HTTPS_PORT\"" in t
    assert "[ \"$CCBOARD_MEM_HTTPS_PORT\" != off ] || CCBOARD_MEM_HTTPS_PORT=" in t
    assert not re.search(r"tailscale funnel|serve reset", re.sub(r"#[^\n]*", "", t.replace("`tailscale funnel`", ""))), "never funnel, never reset"
