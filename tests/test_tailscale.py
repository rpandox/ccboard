"""app/tailscale.py, scripts/tailscale_serve.py and the Doctor's `tailscale` check (issue #126).

Nothing here touches a real Tailscale, its sockets, sudo or a tailnet. The commands are fake shell scripts in a temp directory that is the whole of
PATH (a fake `tailscale` that logs its argv and answers `status --json` and `serve status --json` from files, a fake `sudo` that logs and
refuses), the application bundles and prefixes are temp directories patched over the module's path constants, and the system flags are patched.
Linux is pinned against a copy of the code app/previews.py ran before the move.
"""
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import doctor, previews
from app import platform as plat
from app import tailscale as ts
from app.config import settings

pytestmark = pytest.mark.posix_sh

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import tailscale_serve  # noqa: E402

FQDN = "node.example.ts.net"
BOARD = 8443
TARGET = "http://127.0.0.1:8000"


# ------------------------------------------------------------------ fixtures and fakes

def flags(monkeypatch, system):
    """Patch the system flags to exactly one of linux / macos / windows."""
    monkeypatch.setattr(plat, "IS_LINUX", system == "linux")
    monkeypatch.setattr(plat, "IS_MACOS", system == "macos")
    monkeypatch.setattr(plat, "IS_WINDOWS", system == "windows")
    monkeypatch.setattr(plat, "is_wsl", lambda: False)


def status_json(state="Running", fqdn=FQDN, certs=1, login="user@example.com"):
    return {"BackendState": state, "Self": {"DNSName": fqdn + ".", "UserID": 7}, "User": {"7": {"LoginName": login}},
            "CertDomains": [fqdn] * certs}


def web(port=BOARD, handlers=None, funnel=False, tcp=None):
    key = f"{FQDN}:{port}"
    d = {"Web": {key: {"Handlers": handlers if handlers is not None else {}}}}
    if funnel:
        d["AllowFunnel"] = {key: True}
    if tcp:
        d["TCP"] = {str(port): tcp}
    return d


def proxy(url=TARGET):
    return {"Proxy": url}


class Box:
    """A directory that is the whole of PATH, holding the fake commands. `calls` is every tailscale and sudo argv the fakes saw."""

    def __init__(self, root: Path):
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "calls.log"
        self.serve_file = root / "serve.json"
        self.status_file = root / "status.json"
        self.deny_flag = root / "deny-mutation"
        self.operator_denied = root / "operator-denied"
        self.sudo_flag = root / "sudo-called"
        self.serve_file.write_text("{}")
        self.status_file.write_text(json.dumps(status_json()))
        for tool in ("sleep", "touch"):                       # the only real programs the fakes use
            (self.bin / tool).symlink_to(shutil.which(tool))

    def script(self, name, body, directory=None):
        p = (directory or self.bin) / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)
        return p

    def tailscale(self, name="tailscale", directory=None, serve_rc=0, serve_err="", status_rc=0, status_err="", off_clears=True):
        """A fake CLI. Mutating `serve` calls fail with a denial while `deny-mutation` exists; `serve ... off` empties the serve file;
        while `operator-denied` exists `serve status` is denied to anybody but the root the fake sudo stands in for."""
        q = shlex.quote
        return self.script(name, f"""echo "$@" >> {q(str(self.log))}
[ -z "$TAILSCALE_BE_CLI" ] || echo "be-cli=$TAILSCALE_BE_CLI" >> {q(str(self.log))}
case "$*" in
  "status --json")
    [ {status_rc} -eq 0 ] || {{ echo {q(status_err)} >&2; exit {status_rc}; }}
    printf '%s' "$(while IFS= read -r l || [ -n "$l" ]; do printf '%s' "$l"; done < {q(str(self.status_file))})"; exit 0;;
  "serve status --json")
    [ {serve_rc} -eq 0 ] || {{ echo {q(serve_err)} >&2; exit {serve_rc}; }}
    if [ -e {q(str(self.operator_denied))} ] && [ -z "$TS_AS_ROOT" ]; then echo "Access denied: serve config denied" >&2; exit 1; fi
    printf '%s' "$(while IFS= read -r l || [ -n "$l" ]; do printf '%s' "$l"; done < {q(str(self.serve_file))})"; exit 0;;
esac
if [ -e {q(str(self.deny_flag))} ]; then echo "Access denied: serve config denied" >&2; exit 1; fi
case "$*" in *" off") [ {int(off_clears)} -eq 0 ] || printf '{{}}' > {q(str(self.serve_file))};; esac
exit 0
""", directory)

    def sudo(self, ok=False):
        """A fake sudo: logs, and either refuses or runs the rest of its arguments as the root the fake CLI knows (TS_AS_ROOT)."""
        q = shlex.quote
        run = 'shift; TS_AS_ROOT=1; export TS_AS_ROOT; exec "$@"' if ok else "echo 'sudo: a password is required' >&2; exit 1"
        return self.script("sudo", f'echo "sudo $@" >> {q(str(self.log))}\ntouch {q(str(self.sudo_flag))}\n[ "$1" = -n ] || exit 2\n{run}\n')

    def set_serve(self, d):
        self.serve_file.write_text(json.dumps(d))

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def mutations(self):
        return [c for c in self.calls() if c.startswith("serve ") and not c.startswith("serve status") or c.startswith("sudo ")]


@pytest.fixture
def box(tmp_path, monkeypatch):
    b = Box(tmp_path)
    monkeypatch.setenv("PATH", str(b.bin))
    monkeypatch.setattr(settings, "runtime", "host")
    monkeypatch.delenv("CCBOARD_REPLACE_SERVE", raising=False)
    monkeypatch.delenv("CCBOARD_HTTPS_PORT", raising=False)
    monkeypatch.delenv("TAILSCALE_BE_CLI", raising=False)
    # the layout constants point at nothing; tests that need a layout write it under tmp_path and patch these again
    monkeypatch.setattr(ts, "LINUX_FALLBACK", str(tmp_path / "no-usr-bin-tailscale"))
    for name in ("MAC_APP", "MAC_APP_CLI", "MAS_RECEIPT", "MAC_DAEMON_SOCKET"):
        monkeypatch.setattr(ts, name, tmp_path / "absent" / name)
    monkeypatch.setattr(ts, "MAC_CLI_CANDIDATES", (tmp_path / "absent" / "usr-local-tailscale", tmp_path / "absent" / "brew-tailscale"))
    monkeypatch.setattr(ts, "MAC_DAEMON_CANDIDATES", (tmp_path / "absent" / "usr-local-tailscaled", tmp_path / "absent" / "brew-tailscaled"))
    return b


def mac_layout(tmp_path, monkeypatch, *, bundle=True, receipt=False, daemon=False, daemon_socket=False, bundle_cli=True):
    """Temp directories standing in for /Applications/Tailscale.app (with or without _MASReceipt) and a Homebrew prefix with tailscaled."""
    for leftover in ("Applications", "opt-homebrew-bin", "var-run-tailscaled.socket"):          # callable again within one test
        shutil.rmtree(tmp_path / leftover, ignore_errors=True)
        (tmp_path / leftover).unlink(missing_ok=True) if (tmp_path / leftover).is_file() else None
    app = tmp_path / "Applications" / "Tailscale.app"
    cli = app / "Contents" / "MacOS" / "Tailscale"
    if bundle:
        cli.parent.mkdir(parents=True)
        if bundle_cli:
            cli.write_text("#!/bin/sh\nexit 0\n")
            cli.chmod(0o755)
    if receipt:
        (app / "Contents" / "_MASReceipt").mkdir(parents=True)
    brew = tmp_path / "opt-homebrew-bin"
    brew.mkdir(exist_ok=True)
    if daemon:
        d = brew / "tailscaled"
        d.write_text("#!/bin/sh\nexit 0\n")
        d.chmod(0o755)
    sock = tmp_path / "var-run-tailscaled.socket"
    if daemon_socket:
        sock.touch()
    monkeypatch.setattr(ts, "MAC_APP", app)
    monkeypatch.setattr(ts, "MAC_APP_CLI", cli)
    monkeypatch.setattr(ts, "MAS_RECEIPT", app / "Contents" / "_MASReceipt")
    monkeypatch.setattr(ts, "MAC_DAEMON_CANDIDATES", (brew / "tailscaled", tmp_path / "absent" / "usr-local-tailscaled"))
    monkeypatch.setattr(ts, "MAC_CLI_CANDIDATES", (brew / "tailscale", tmp_path / "absent" / "usr-local-tailscale"))
    monkeypatch.setattr(ts, "MAC_DAEMON_SOCKET", sock)
    return SimpleNamespace(app=app, cli=cli, brew=brew)


# ------------------------------------------------------------------ find_cli

def old_exe(which_result):
    return which_result or "/usr/bin/tailscale"         # the expression app/previews.py had: shutil.which("tailscale") or "/usr/bin/tailscale"


@pytest.mark.parametrize("found", [None, "/usr/bin/tailscale", "/usr/local/bin/tailscale", "/snap/bin/tailscale", "/home/u/bin/tailscale"])
def test_linux_find_cli_equals_the_old_expression(monkeypatch, found):
    flags(monkeypatch, "linux")
    monkeypatch.setattr(ts.shutil, "which", lambda name: found if name == "tailscale" else None)
    cli = ts.find_cli()
    assert cli.argv == [old_exe(found)] and cli.env == {} and cli.exe == old_exe(found)
    assert ts.serve_cmd("--bg", "--https=9100", "http://127.0.0.1:5173") == [old_exe(found), "serve", "--bg", "--https=9100", "http://127.0.0.1:5173"]
    assert previews.serve_cmd("status", "--json") == [old_exe(found), "serve", "status", "--json"]
    assert ts.socket_args() == []                         # the Linux --socket default is left alone


def test_linux_find_cli_never_checks_that_the_file_exists(monkeypatch, tmp_path):
    flags(monkeypatch, "linux")
    monkeypatch.setattr(ts.shutil, "which", lambda name: None)
    monkeypatch.setattr(ts, "LINUX_FALLBACK", "/usr/bin/tailscale")
    assert ts.find_cli().argv == ["/usr/bin/tailscale"]


def test_wsl_without_a_distro_tailscale_uses_the_windows_exe(monkeypatch, tmp_path):
    flags(monkeypatch, "linux")
    monkeypatch.setattr(plat, "is_wsl", lambda: True)
    monkeypatch.setattr(ts, "LINUX_FALLBACK", str(tmp_path / "none"))
    both = {"tailscale": None, "tailscale.exe": "/mnt/c/Program Files/Tailscale/tailscale.exe"}
    monkeypatch.setattr(ts.shutil, "which", lambda name: both.get(name))
    cli = ts.find_cli()
    assert cli.argv == [both["tailscale.exe"]] and ts.variant(cli) == "wsl-host"
    both["tailscale"] = "/usr/bin/tailscale"                          # the distro's own tailscale wins
    assert ts.find_cli().argv == ["/usr/bin/tailscale"] and ts.variant() == "linux"
    both["tailscale"] = None
    (tmp_path / "usrbin").write_text("")
    monkeypatch.setattr(ts, "LINUX_FALLBACK", str(tmp_path / "usrbin"))   # a file at the fallback path: the old answer, as on any Linux
    assert ts.find_cli().argv == [str(tmp_path / "usrbin")]


def test_macos_path_wins_then_prefixes_then_the_app_binary(box, tmp_path, monkeypatch):
    flags(monkeypatch, "macos")
    lay = mac_layout(tmp_path, monkeypatch, bundle=True, receipt=True)
    cli = ts.find_cli()                                               # nothing on PATH, no launcher: the bundle's binary, as the CLI
    assert cli.argv == [str(lay.cli)] and cli.env == {"TAILSCALE_BE_CLI": "1"}
    assert cli.cmd("serve", "status", "--json") == ["/usr/bin/env", "TAILSCALE_BE_CLI=1", str(lay.cli), "serve", "status", "--json"]
    brew_cli = lay.brew / "tailscale"
    brew_cli.write_text("#!/bin/sh\n")
    brew_cli.chmod(0o755)
    assert ts.find_cli().argv == [str(brew_cli)] and ts.find_cli().env == {}      # a prefix beats the bundle
    on_path = box.tailscale()
    cli = ts.find_cli()
    assert cli.argv == [str(on_path)] and cli.env == {}                           # PATH beats both
    assert cli.cmd("status") == [str(on_path), "status"]


def test_macos_nothing_found_returns_none_and_says_why(box, tmp_path, monkeypatch):
    flags(monkeypatch, "macos")
    mac_layout(tmp_path, monkeypatch, bundle=False)
    assert ts.find_cli() is None
    why = ts.missing_reason()
    assert "/Applications/Tailscale.app" in why and "App Store" in why and "tailscale.com/download" in why
    assert ts.variant() == "unknown"
    # a bundle without an executable binary is not a command either
    mac_layout(tmp_path, monkeypatch, bundle=True, bundle_cli=False)
    assert ts.find_cli() is None


def test_windows_looks_on_path_then_under_program_files(box, tmp_path, monkeypatch):
    flags(monkeypatch, "windows")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    assert ts.find_cli() is None and "tailscale.exe" in ts.missing_reason() and "to verify" in ts.missing_reason()
    exe = tmp_path / "pf" / "Tailscale" / "tailscale.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    exe.chmod(0o755)
    assert ts.find_cli().argv == [str(exe)] and ts.variant() == "windows"


# ------------------------------------------------------------------ variant and capabilities

def test_variant_from_file_layout(box, tmp_path, monkeypatch):
    flags(monkeypatch, "macos")
    mac_layout(tmp_path, monkeypatch, bundle=True, receipt=True)
    assert ts.variant() == "macos-appstore"                                       # a receipt: the App Store build
    mac_layout(tmp_path, monkeypatch, bundle=True, receipt=False)
    assert ts.variant() == "macos-standalone"
    mac_layout(tmp_path, monkeypatch, bundle=False, daemon=True)
    assert ts.variant() == "macos-opensource"                                     # a Homebrew prefix with tailscaled, no app
    mac_layout(tmp_path, monkeypatch, bundle=False)
    assert ts.variant() == "unknown"                                              # never assumed to be Standalone
    # the app and a Homebrew tailscaled together: the daemon's socket says which one runs
    mac_layout(tmp_path, monkeypatch, bundle=True, receipt=True, daemon=True, daemon_socket=False)
    assert ts.variant() == "macos-appstore"
    mac_layout(tmp_path, monkeypatch, bundle=True, receipt=True, daemon=True, daemon_socket=True)
    assert ts.variant() == "macos-opensource"
    flags(monkeypatch, "linux")
    assert ts.variant() == "linux"
    flags(monkeypatch, "windows")
    assert ts.variant() == "windows"
    monkeypatch.setattr(plat, "IS_WINDOWS", False)
    assert ts.variant() == "unknown"                                              # no system flag at all


def test_variant_makes_no_command_call(box, tmp_path, monkeypatch):
    flags(monkeypatch, "macos")
    mac_layout(tmp_path, monkeypatch, bundle=True, receipt=True)
    monkeypatch.setattr(ts.subprocess, "run", lambda *a, **k: pytest.fail("variant() must not run a command"))
    assert ts.variant() == "macos-appstore"


def test_capabilities_table():
    assert ts.capabilities("linux") == {"serve_ports": True, "serve_files": True, "funnel": True, "before_login": True}
    for v in ("macos-appstore", "macos-standalone"):                                # Funnel unsupported: the two Tailscale pages disagree
        assert ts.capabilities(v) == {"serve_ports": True, "serve_files": False, "funnel": False, "before_login": False}
    assert ts.capabilities("macos-opensource") == {"serve_ports": True, "serve_files": True, "funnel": True, "before_login": True}
    for v in ("windows", "wsl-host", "unknown", "something-else"):                  # nothing measured: None (to verify), never a guess
        assert ts.capabilities(v) == {"serve_ports": True, "serve_files": None, "funnel": None, "before_login": None}
    assert set(ts.VARIANTS) == set(ts.LABELS)
    ts.capabilities("linux")["funnel"] = False
    assert ts.capabilities("linux")["funnel"] is True                               # a copy each time


# ------------------------------------------------------------------ the run helper

def old_run_serve(args, *, which, runtime, sock_exists, run, home="/home/example"):
    """app/previews.py's _run_serve before issue #126, verbatim in behaviour; returns (error message or None)."""
    cmd = [which or "/usr/bin/tailscale", "serve", *args]
    docker = runtime == "docker"
    if docker and not sock_exists:
        return "tailscaled socket SOCK is not mounted; is tailscale running on the box?"
    try:
        cp = run(cmd)
        if cp.returncode != 0 and not docker and "denied" in (cp.stderr + cp.stdout).lower():
            cp = run(["sudo", "-n", *cmd])
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"tailscale serve failed: {e.__class__.__name__}"
    if cp.returncode != 0:
        text = (cp.stderr + cp.stdout).lower()
        if docker and ("denied" in text or "permission" in text or "operator" in text):
            return f"tailscale serve failed; on the box run: sudo tailscale set --operator={Path(home).name}"
        return (cp.stderr or cp.stdout).strip()[-300:] or "tailscale serve failed"
    return None


CP = lambda rc, out="", err="": SimpleNamespace(returncode=rc, stdout=out, stderr=err)   # noqa: E731
ANSWERS = [CP(0), CP(1, "", "Access denied: serve config denied"), CP(1, "", "open /var/run/tailscale/tailscaled.sock: permission denied"),
           CP(1, "", "Serve is not enabled on your tailnet."), CP(1, "denied by ACL", ""), CP(1), CP(1, "", "must be run as the operator"),
           CP(1, "", "x" * 400)]


@pytest.mark.parametrize("runtime", ["host", "systemd", "launchd", "docker"])
@pytest.mark.parametrize("which", [None, "/usr/bin/tailscale", "/usr/local/bin/tailscale"])
@pytest.mark.parametrize("sock_exists", [True, False])
def test_linux_run_serve_equals_the_old_code_for_every_answer(monkeypatch, tmp_path, runtime, which, sock_exists):
    flags(monkeypatch, "linux")
    monkeypatch.setenv("HOME", "/home/example")
    monkeypatch.setattr(settings, "runtime", runtime)
    monkeypatch.setattr(ts.shutil, "which", lambda name: which if name == "tailscale" else None)
    sock = tmp_path / "tailscaled.sock"
    if sock_exists:
        sock.touch()
    args = ["--bg", "--https=9100", "http://127.0.0.1:5173"]
    for first in ANSWERS:
        for second in ANSWERS:
            seq_old, seq_new = [first, second], [first, second]
            old_calls, new_calls = [], []
            err_old = old_run_serve(args, which=which, runtime=runtime, sock_exists=sock_exists,
                                    run=lambda c: old_calls.append(c) or seq_old.pop(0))
            monkeypatch.setattr(subprocess, "run", lambda argv, **kw: new_calls.append(argv) or seq_new.pop(0))
            err_new = None
            try:
                ts.run_serve(args, sock=sock)
            except ts.TailscaleError as e:
                err_new = str(e)
            assert new_calls == old_calls
            assert (err_new or "").replace(str(sock), "SOCK") == (err_old or "")


def test_linux_denied_serve_is_retried_once_through_sudo(box, monkeypatch):
    flags(monkeypatch, "linux")
    monkeypatch.setattr(ts.shutil, "which", lambda name: str(box.bin / name) if (box.bin / name).exists() else None)
    box.tailscale()
    box.sudo(ok=False)
    box.deny_flag.touch()
    with pytest.raises(ts.TailscaleError) as ei:
        ts.run_serve(ts.serve_on_args(9100, 5173))
    assert str(ei.value) == "sudo: a password is required"                          # the last answer, as before: the raw error, no variant text
    exe = str(box.bin / "tailscale")
    assert box.calls() == ["serve --bg --https=9100 http://127.0.0.1:5173",
                           f"sudo -n {exe} serve --bg --https=9100 http://127.0.0.1:5173"]       # one try as the user, one retry, no more


def test_linux_sudo_success_ends_it_and_a_second_denial_is_not_retried_again(box, monkeypatch):
    flags(monkeypatch, "linux")
    monkeypatch.setattr(ts.shutil, "which", lambda name: str(box.bin / name) if (box.bin / name).exists() else None)
    box.tailscale()
    box.sudo(ok=True)
    box.operator_denied.touch()                                                      # serve status is denied to the user, fine for root
    assert ts.serve_status() == {}                                                   # read through the same retry
    assert sum(1 for c in box.calls() if c.startswith("sudo ")) == 1


def test_container_never_retries_and_gives_the_operator_hint(box, tmp_path, monkeypatch):
    flags(monkeypatch, "linux")
    monkeypatch.setattr(ts.shutil, "which", lambda name: str(box.bin / name) if (box.bin / name).exists() else None)
    monkeypatch.setattr(settings, "runtime", "docker")
    monkeypatch.setenv("HOME", "/home/example")
    sock = tmp_path / "tailscaled.sock"
    sock.touch()
    box.tailscale()
    box.sudo(ok=True)
    box.deny_flag.touch()
    with pytest.raises(ts.TailscaleError) as ei:
        ts.run_serve(["--https=9100", "--yes", "off"], sock=sock)
    assert str(ei.value) == "tailscale serve failed; on the box run: sudo tailscale set --operator=example"
    assert not box.sudo_flag.exists() and len(box.calls()) == 1
    with pytest.raises(ts.TailscaleError, match="socket .* is not mounted"):
        ts.run_serve(["--https=9100", "--yes", "off"], sock=tmp_path / "missing.sock")
    assert len(box.calls()) == 1                                                      # no command at all without the socket


@pytest.mark.parametrize("system,v", [("macos", "macos-appstore"), ("macos", "macos-standalone"), ("macos", "macos-opensource"),
                                      ("windows", "windows")])
@pytest.mark.parametrize("runtime", ["host", "launchd"])
def test_no_sudo_off_linux_and_a_denial_names_the_variant_and_the_fix(box, tmp_path, monkeypatch, system, v, runtime):
    flags(monkeypatch, system)
    monkeypatch.setattr(settings, "runtime", runtime)
    if system == "macos":
        mac_layout(tmp_path, monkeypatch, bundle=v != "macos-opensource", receipt=v == "macos-appstore", daemon=v == "macos-opensource")
    else:
        monkeypatch.setattr(ts, "variant", lambda cli=None: "windows")
    box.tailscale()
    box.sudo(ok=True)                                                                 # present, would work, and must never be called
    box.deny_flag.touch()
    with pytest.raises(ts.TailscaleError) as ei:
        ts.run_serve(ts.serve_on_args(9100, 5173))
    msg = str(ei.value)
    assert msg.startswith("tailscale serve failed; ") and ts.LABELS[v] in msg
    if v == "macos-opensource":
        assert f"sudo tailscale set --operator={Path.home().name}" in msg           # the words to run once, never run by the board
    if v in ("macos-appstore", "macos-standalone"):
        assert "Open the Tailscale app" in msg and "to verify" in msg
    assert not box.sudo_flag.exists()
    assert box.calls() == ["serve --bg --https=9100 http://127.0.0.1:5173"]          # one attempt
    box.operator_denied.touch()
    assert ts.serve_status() is None and not box.sudo_flag.exists()                  # a denied read is not retried through sudo either


def test_other_failures_off_linux_pass_through_and_a_missing_command_is_reported(box, tmp_path, monkeypatch):
    flags(monkeypatch, "macos")
    mac_layout(tmp_path, monkeypatch, bundle=False)
    with pytest.raises(ts.TailscaleError, match="was not found on PATH"):
        ts.run_serve(["--https=9100", "--yes", "off"])
    box.tailscale()
    box.script("tailscale", 'echo "Serve is not enabled on your tailnet." >&2; exit 1\n')
    with pytest.raises(ts.TailscaleError) as ei:
        ts.run_serve(["--https=9100", "--yes", "off"])
    assert str(ei.value) == "Serve is not enabled on your tailnet."


def test_the_app_bundle_binary_runs_with_the_cli_environment(box, tmp_path, monkeypatch):
    flags(monkeypatch, "macos")
    lay = mac_layout(tmp_path, monkeypatch, bundle=True, receipt=True)
    box.tailscale(name="Tailscale", directory=lay.cli.parent)
    ts.run_serve(ts.serve_on_args(9100, 5173))
    assert box.calls() == ["serve --bg --https=9100 http://127.0.0.1:5173", "be-cli=1"]


def test_serve_on_and_off_arguments_are_the_old_ones(monkeypatch):
    assert ts.serve_on_args(9100, 5173) == ["--bg", "--https=9100", "http://127.0.0.1:5173"]
    assert ts.serve_off_args(9100) == ["--https=9100", "--yes", "off"]
    got = []
    monkeypatch.setattr(ts, "run_serve", lambda args, **kw: got.append(args))
    ts.serve_on(9100, 5173)
    ts.serve_off(9100)
    assert got == [["--bg", "--https=9100", "http://127.0.0.1:5173"], ["--https=9100", "--yes", "off"]]
    got.clear()
    with pytest.raises(ts.TailscaleError):
        ts.serve_on(443, 5173)
    ts.serve_off(443)                                                                 # port 443 is never touched
    assert got == []


def test_previews_goes_through_the_module_with_the_same_commands(box, monkeypatch):
    flags(monkeypatch, "linux")
    monkeypatch.setattr(ts.shutil, "which", lambda name: str(box.bin / name) if (box.bin / name).exists() else None)
    monkeypatch.setattr(settings, "public_url", "https://box.ts.net:8443")
    box.tailscale()
    assert previews.serve_on(9100, 5173) == "https://box.ts.net:9100/"
    previews.serve_off(9100)
    assert box.calls() == ["serve --bg --https=9100 http://127.0.0.1:5173", "serve --https=9100 --yes off"]
    box.deny_flag.touch()
    box.sudo(ok=False)
    with pytest.raises(previews.PreviewError):
        previews.serve_on(9101, 5173)
    previews.serve_off(9101)                                                          # a failed off is logged, never raised


# ------------------------------------------------------------------ reading status

def test_status_and_serve_status_never_raise(box, monkeypatch):
    flags(monkeypatch, "linux")
    monkeypatch.setattr(ts.shutil, "which", lambda name: str(box.bin / name) if (box.bin / name).exists() else None)
    assert ts.status() is None and ts.serve_status() is None                          # nothing installed (the fallback path is absent)
    box.tailscale()
    assert ts.summarize_status(ts.status()) == {"state": "Running", "fqdn": FQDN, "login": "user@example.com", "certs": 1}
    assert ts.serve_status() == {}
    box.set_serve(web(handlers={"/": proxy()}))
    assert ts.serve_status()["Web"][f"{FQDN}:{BOARD}"]["Handlers"]["/"] == proxy()
    box.serve_file.write_text("null")
    assert ts.serve_status() == {}
    box.serve_file.write_text("not json")
    assert ts.serve_status() is None
    box.script("tailscale", 'echo "No serve config"\n')
    assert ts.serve_status() == {}
    box.script("tailscale", "exec sleep 5\n")
    monkeypatch.setattr(ts, "CALL_TIMEOUT", 0.2)
    assert ts.status() is None
    assert ts.call(["status", "--json"]).kind == "timeout"
    assert ts.summarize_status(None) == {"state": "", "fqdn": "", "login": "", "certs": 0}


@pytest.mark.parametrize("rc,out,err,kind", [
    (0, "{}", "", "ok"),
    (1, "", "Access denied: serve config denied", "denied"),
    (1, "", "open /var/run/tailscale/tailscaled.sock: permission denied", "denied"),
    (1, "", "use 'sudo tailscale set --operator=$USER' once", "denied"),
    (1, "", "failed to connect to local tailscaled; it doesn't appear to be running", "not-running"),
    (1, "", "Tailscale is stopped.", "not-running"),
    (1, "", "something else", "failed"),
])
def test_classify(rc, out, err, kind):
    assert ts.classify(rc, out, err) == kind


# ------------------------------------------------------------------ the mapping rules == install.sh's serve_check

def shell_serve_check():
    """The python3 program inside install.sh's serve_check, extracted from the script itself."""
    text = (ROOT / "install.sh").read_text()
    m = re.search(r"serve_check\(\) \{.*?python3 -c '\n(.*?)\n' \"\$TS_FQDN\"", text, re.S)
    assert m, "serve_check's embedded python was not found in install.sh"
    return m.group(1)


def run_shell_check(d, port, path, target, board):
    cp = subprocess.run([sys.executable, "-c", shell_serve_check(), FQDN, str(port), path, target, str(board)],
                        input=json.dumps(d), capture_output=True, text=True, check=True)
    return cp.stdout.strip()


MAPPING_CASES = {
    "missing": web(handlers={}),
    "nothing at all": {},
    "ours": web(handlers={"/": proxy()}),
    "ours with tty": web(handlers={"/": proxy(), "/tty": proxy("http://127.0.0.1:7681")}),
    "stale": web(handlers={"/": proxy("http://127.0.0.1:9999")}),
    "foreign proxy": web(handlers={"/": proxy("http://10.0.0.5:80")}),
    "foreign path": web(handlers={"/": proxy(), "/other": proxy("http://127.0.0.1:1")}),
    "foreign static": web(handlers={"/": {"Path": "/srv/site"}}),
    "tcp forward": web(handlers={}, tcp={"TCPForward": "127.0.0.1:22"}),
    "https tcp": web(handlers={"/": proxy()}, tcp={"HTTPS": True}),
    "funnel": web(handlers={"/": proxy()}, funnel=True),
    "other port only": web(port=9000, handlers={"/": proxy()}),
}


@pytest.mark.parametrize("name", sorted(MAPPING_CASES))
@pytest.mark.parametrize("path", ["/", "/tty"])
@pytest.mark.parametrize("board", [BOARD, 9000])
def test_mapping_state_equals_the_installers_serve_check(name, path, board):
    d = MAPPING_CASES[name]
    target = TARGET if path == "/" else "http://127.0.0.1:7681"
    assert ts.mapping_state(d, FQDN, BOARD, path, target, board) == run_shell_check(d, BOARD, path, target, board)


def test_the_mapping_states_by_name():
    st = lambda d, path="/", target=TARGET, board=BOARD: ts.mapping_state(d, FQDN, BOARD, path, target, board)   # noqa: E731
    assert st(web(handlers={})) == "missing" and st({}) == "missing" and st(None) == "missing"
    assert st(web(handlers={"/": proxy()})) == "ours"
    assert st(web(handlers={"/": proxy("http://127.0.0.1:9999")})) == "stale"
    assert st(web(handlers={"/": proxy("http://10.0.0.5:80")})).startswith("foreign:handler / -> ")
    assert st(web(handlers={"/": proxy(), "/tty": proxy()}), board=9000).startswith("foreign:handler /tty")   # /tty is ours on the board port only
    assert st(web(handlers={}, tcp={"TCPForward": "x"})) == "foreign:port is used for TCP forwarding"
    assert st(web(handlers={"/": proxy()}, funnel=True)) == "funnel"


# ------------------------------------------------------------------ scripts/tailscale_serve.py

@pytest.fixture
def cli(box, tmp_path, monkeypatch, capsys):
    """tailscale_serve.main on a macOS-flagged host whose PATH holds the fake tailscale and a fake sudo that must never run."""
    flags(monkeypatch, "macos")
    mac_layout(tmp_path, monkeypatch, bundle=False)
    box.tailscale()
    box.sudo(ok=True)

    def run(*argv, **env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        capsys.readouterr()
        rc = tailscale_serve.main(list(argv))
        cap = capsys.readouterr()
        return rc, cap.out.strip(), cap.err.strip()
    return run


def args(port=BOARD, path="/", target=TARGET, *extra):
    return ("--https", str(port), "--path", path, "--target", target, *extra)


@pytest.mark.parametrize("name,expect", [
    ("missing", "missing"), ("ours", "ours"), ("stale", "stale"), ("funnel", "funnel"),
    ("foreign proxy", "foreign:handler / -> {'Proxy': 'http://10.0.0.5:80'}"), ("tcp forward", "foreign:port is used for TCP forwarding"),
])
def test_check_prints_one_word_per_state(box, cli, name, expect):
    box.set_serve(MAPPING_CASES[name])
    assert cli("check", *args()) == (0, expect, "")
    assert box.mutations() == []                                                      # a check changes nothing


def test_check_of_the_tty_path_and_of_an_unreadable_configuration(box, cli):
    box.set_serve(web(handlers={"/": proxy(), "/tty": proxy("http://127.0.0.1:7681")}))
    assert cli("check", *args(BOARD, "/tty", "http://127.0.0.1:7681", "--board-https", str(BOARD)))[:2] == (0, "ours")
    assert cli("check", *args(BOARD, "/tty", "http://127.0.0.1:7681", "--board-https", "9000"))[1].startswith("foreign:handler /tty")
    box.operator_denied.touch()
    box.sudo_flag.unlink(missing_ok=True)
    rc, out, err = cli("check", *args())
    assert (rc, out) == (3, "unreadable") and "could not read" in err                 # never read as "missing"
    assert not box.sudo_flag.exists()


def test_apply_ours_changes_nothing(box, cli):
    box.set_serve(web(handlers={"/": proxy()}))
    rc, out, err = cli("apply", *args())
    assert (rc, err) == (0, "") and out.endswith(f"{FQDN}:{BOARD}/ -> {TARGET} (already set)")
    assert box.mutations() == []


def test_apply_missing_adds_the_mapping_in_the_installers_forms(box, cli):
    rc, out, err = cli("apply", *args())
    assert rc == 0 and out.endswith(f"{BOARD}/ -> {TARGET}")
    rc, out, err = cli("apply", *args(BOARD, "/tty", "http://127.0.0.1:7681", "--board-https", str(BOARD)))
    assert rc == 0
    assert box.mutations() == [f"serve --bg --https={BOARD} {TARGET}", f"serve --bg --https={BOARD} --set-path /tty http://127.0.0.1:7681"]


def test_apply_stale_replaces_only_that_path_first(box, cli):
    box.set_serve(web(handlers={"/": proxy("http://127.0.0.1:9999")}))
    rc, out, err = cli("apply", *args())
    assert rc == 0 and "replacing the old ccboard handler" in out
    assert box.mutations() == [f"serve --https={BOARD} --set-path / off", f"serve --bg --https={BOARD} {TARGET}"]


@pytest.mark.parametrize("name", ["foreign proxy", "foreign path", "tcp forward", "funnel"])
def test_apply_refuses_a_foreign_or_funnel_port_without_the_replace_setting(box, cli, name):
    box.set_serve(MAPPING_CASES[name])
    rc, out, err = cli("apply", *args())
    assert rc == 1 and out == "" and "already used by something else" in err and "CCBOARD_REPLACE_SERVE=1" in err
    assert box.mutations() == []
    rc, out, err = cli("apply", *args(), CCBOARD_REPLACE_SERVE="0")                    # only the exact value 1 counts
    assert rc == 1 and box.mutations() == []


@pytest.mark.parametrize("name", ["foreign proxy", "foreign path", "tcp forward", "funnel"])
def test_apply_replaces_with_the_setting_by_turning_that_one_port_off(box, cli, name):
    box.set_serve(MAPPING_CASES[name])
    rc, out, err = cli("apply", *args(), CCBOARD_REPLACE_SERVE="1")
    assert rc == 0 and "replacing because CCBOARD_REPLACE_SERVE=1" in err
    assert box.mutations() == [f"serve --https={BOARD} --yes off", f"serve --bg --https={BOARD} {TARGET}"]


def test_apply_stops_when_the_port_still_holds_something_after_off(box, cli):
    box.set_serve(MAPPING_CASES["funnel"])
    box.tailscale(off_clears=False)
    rc, out, err = cli("apply", *args(), CCBOARD_REPLACE_SERVE="1")
    assert rc == 1 and "still holds something else" in err
    assert box.mutations() == [f"serve --https={BOARD} --yes off"]


def test_port_443_is_refused_unless_it_is_the_boards_own_and_never_replaced(box, cli):
    # not the board's port: refused outright, whatever the state, even missing and even with the replace setting
    rc, out, err = cli("apply", *args(443), CCBOARD_REPLACE_SERVE="1")
    assert rc == 1 and "port 443 is never touched" in err
    rc, out, err = cli("off", *args(443))
    assert rc == 1 and "port 443 is never touched" in err
    rc, out, err = cli("apply", *args(443, "/", TARGET, "--board-https", "8443"))
    assert rc == 1 and box.mutations() == []
    # the board's own port: a free 443 is set up like any other, with the board's /tty
    web443 = lambda handlers, **kw: web(port=443, handlers=handlers, **kw)                          # noqa: E731
    box.set_serve(web443({}))
    assert cli("apply", *args(443, "/", TARGET, "--board-https", "443"))[0] == 0
    assert box.mutations() == [f"serve --bg --https=443 {TARGET}"]
    # the board's own 443 with something foreign or a Funnel on it: never replaced, even with the setting
    box.log.unlink()
    for state in (web443({"/": proxy("http://10.0.0.5:80")}), web443({"/": proxy()}, funnel=True)):
        box.set_serve(state)
        rc, out, err = cli("apply", *args(443, "/", TARGET, "--board-https", "443"), CCBOARD_REPLACE_SERVE="1")
        assert rc == 1 and "never replaced" in err
    assert box.mutations() == []
    # the default board port comes from CCBOARD_HTTPS_PORT
    box.set_serve(web443({}))
    assert cli("apply", *args(443), CCBOARD_HTTPS_PORT="443")[0] == 0


def test_reset_is_not_a_verb_and_no_run_ever_issues_one(box, cli, capsys):
    for verb in ("reset", "funnel", "serve"):
        with pytest.raises(SystemExit) as ei:
            tailscale_serve.main([verb, "--https", "8443"])
        assert ei.value.code == 2
    capsys.readouterr()
    box.set_serve(MAPPING_CASES["foreign proxy"])
    for v in ("check", "apply", "off"):
        cli(v, *args(), CCBOARD_REPLACE_SERVE="1")
    cli("status")
    assert box.calls() and not any("reset" in c.split() or "funnel" in c.split() for c in box.calls())
    src = (ROOT / "scripts" / "tailscale_serve.py").read_text() + (ROOT / "app" / "tailscale.py").read_text()
    code = re.sub(r'"""(.*?)"""|#[^\n]*', "", src, flags=re.S)
    assert "reset" not in code                                                           # no code path builds a reset


def test_bad_path_target_and_port_are_refused_before_any_call(box, cli):
    for bad in (args(8443, "tty"), args(8443, "/a b"), args(8443, "/", "http://10.0.0.1:80"), args(8443, "/", "https://127.0.0.1:80"),
                args(8443, "/", "http://127.0.0.1:80; rm -rf x"), args(8443, "/", "http://127.0.0.1")):
        rc, out, err = cli("apply", *bad)
        assert rc == 2 and err
    for port in ("0", "65536", "x", "-1"):
        with pytest.raises(SystemExit):
            tailscale_serve.main(["apply", *args(port)])
    assert box.calls() == []


def test_apply_denied_names_the_variant_and_never_runs_sudo(box, cli, tmp_path, monkeypatch):
    mac_layout(tmp_path, monkeypatch, bundle=True, receipt=True)
    box.deny_flag.touch()
    rc, out, err = cli("apply", *args())
    assert rc == 1 and "the App Store Tailscale app refused it" in err and "Open the Tailscale app" in err
    assert not box.sudo_flag.exists()
    assert box.mutations() == [f"serve --bg --https={BOARD} {TARGET}"]


def test_off_turns_off_only_our_own_mapping_and_only_that_path(box, cli):
    box.set_serve(web(handlers={"/": proxy(), "/tty": proxy("http://127.0.0.1:7681")}))
    rc, out, err = cli("off", *args(BOARD, "/tty", "http://127.0.0.1:7681", "--board-https", str(BOARD)))
    assert rc == 0 and "turned off" in out
    assert box.mutations() == [f"serve --https={BOARD} --set-path /tty off"]
    box.log.unlink()
    box.set_serve(MAPPING_CASES["foreign proxy"])
    rc, out, err = cli("off", *args())
    assert rc == 0 and "left alone" in out and box.mutations() == []
    box.set_serve(MAPPING_CASES["funnel"])
    assert cli("off", *args(), CCBOARD_REPLACE_SERVE="1")[0] == 0 and box.mutations() == []      # off never uses the replace setting


def test_status_reports_each_state(box, cli, tmp_path, monkeypatch):
    rc, out, err = cli("status")
    d = json.loads(out)
    assert rc == 0 and d["ok"] is True and d["fqdn"] == FQDN and d["login"] == "user@example.com" and d["certs"] == 1
    assert d["state"] == "Running" and d["logged_in"] is True and d["serve"] == "readable" and d["installed"] is True
    assert d["cli"] == str(box.bin / "tailscale") and d["variant"] == "unknown" and d["capabilities"]["serve_ports"] is True
    for state, key, why in ((status_json(state="NeedsLogin"), "logged_in", "not running/logged in"),
                            (status_json(fqdn=""), "fqdn", "no MagicDNS name"), (status_json(certs=0), "certs", "HTTPS certificates are not enabled")):
        box.status_file.write_text(json.dumps(state))
        rc, out, err = cli("status")
        d = json.loads(out)
        assert rc == 1 and d["ok"] is False and why in d["reason"]
    box.status_file.write_text(json.dumps(status_json()))
    box.operator_denied.touch()
    assert json.loads(cli("status")[1])["serve"] == "unreadable"
    box.tailscale(status_rc=1, status_err="failed to connect to local tailscaled")
    rc, out, err = cli("status")
    assert rc == 1 and "tailscaled running" in json.loads(out)["reason"]
    mac_layout(tmp_path, monkeypatch, bundle=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    rc, out, err = cli("status")
    d = json.loads(out)
    assert rc == 1 and d["installed"] is False and d["cli"] is None and "install Tailscale" in d["reason"]
    assert not box.sudo_flag.exists()


def test_the_script_runs_as_a_program(box, tmp_path):
    """Run for real: the path bootstrap works and --help and an unknown verb behave."""
    env = {"PATH": str(box.bin), "HOME": str(tmp_path), "CCBOARD_ENV_FILE": ""}
    cp = subprocess.run([sys.executable, str(ROOT / "scripts" / "tailscale_serve.py"), "--help"], capture_output=True, text=True, env=env)
    assert cp.returncode == 0 and "check" in cp.stdout and "apply" in cp.stdout and "reset" not in cp.stdout
    cp = subprocess.run([sys.executable, str(ROOT / "scripts" / "tailscale_serve.py"), "reset"], capture_output=True, text=True, env=env)
    assert cp.returncode == 2 and "invalid choice" in cp.stderr


# ------------------------------------------------------------------ the Doctor's tailscale check

@pytest.fixture
def doc(box, monkeypatch):
    """doctor._c_tailscale on a Linux-flagged host with a fake CLI on PATH; the hint family pinned so the fix commands are Linux's."""
    flags(monkeypatch, "linux")
    monkeypatch.setattr(ts.shutil, "which", lambda name: str(box.bin / name) if (box.bin / name).exists() else None)
    monkeypatch.setattr(doctor.plat, "_hint_family", lambda: "linux")
    box.tailscale()
    return lambda: doctor._c_tailscale(None)


def test_doctor_registers_the_check_in_the_box_group():
    spec = [c for c in doctor.CHECKS if c[0] == "tailscale"]
    assert len(spec) == 1 and spec[0][1] == "box" and spec[0][3] is doctor._c_tailscale


def test_doctor_passes_when_everything_is_readable(box, doc):
    o = doc()
    assert o.status == "pass" and o.fix is None
    assert str(box.bin / "tailscale") in o.detail and "(linux)" in o.detail and "signed in" in o.detail and "MagicDNS and HTTPS certificates on" in o.detail
    assert "serve status readable" in o.detail and FQDN not in o.detail
    assert box.mutations() == [] and not box.sudo_flag.exists()                      # read only: status and serve status, nothing else
    assert set(box.calls()) == {"status --json", "serve status --json"}


def test_doctor_not_installed(box, doc):
    (box.bin / "tailscale").unlink()
    o = doc()
    assert o.status == "fail" and "not installed" in o.detail and o.fix["text"].startswith("Install Tailscale")


def test_doctor_not_running(box, doc):
    box.tailscale(status_rc=1, status_err="failed to connect to local tailscaled; it doesn't appear to be running")
    o = doc()
    assert o.status == "fail" and "not running or cannot be reached" in o.detail and o.fix["cmd"] == "sudo systemctl start tailscaled"


@pytest.mark.parametrize("state", ["NeedsLogin", "Stopped", "NoState", ""])
def test_doctor_not_signed_in(box, doc, state):
    box.status_file.write_text(json.dumps(status_json(state=state)))
    o = doc()
    assert o.status == "fail" and "not signed in" in o.detail and o.fix["cmd"] == "sudo tailscale up"
    assert not box.sudo_flag.exists() and "serve status --json" not in box.calls()   # nothing else is asked of a daemon that is not up


def test_doctor_warns_for_no_magicdns_and_no_certificates(box, doc):
    box.status_file.write_text(json.dumps(status_json(fqdn="", certs=0)))
    o = doc()
    assert o.status == "warn" and "no MagicDNS name" in o.detail and "HTTPS certificates are off" in o.detail
    box.status_file.write_text(json.dumps(status_json(certs=0)))
    o = doc()
    assert o.status == "warn" and "HTTPS certificates are off" in o.detail and "MagicDNS" not in o.detail.split(";", 1)[1]
    assert "HTTPS Certificates" in o.fix["text"]


def test_doctor_linux_denied_but_sudo_works_passes_and_says_so(box, doc):
    box.operator_denied.touch()
    box.sudo(ok=True)
    o = doc()
    assert o.status == "pass" and "harmless" in o.detail and "sudo -n" in o.detail
    assert box.calls().count("serve status --json") == 2 and any(c.startswith("sudo -n ") for c in box.calls())


def test_doctor_linux_denied_and_sudo_refused_warns_with_the_installer_as_the_fix(box, doc):
    box.operator_denied.touch()
    box.sudo(ok=False)
    o = doc()
    assert o.status == "warn" and "sudo -n retry failed" in o.detail and o.fix["cmd"] == "./install.sh"


def test_doctor_container_denied_is_a_warning_with_the_operator_fix_and_no_sudo(box, doc, monkeypatch):
    monkeypatch.setattr(settings, "runtime", "docker")
    box.operator_denied.touch()
    box.sudo(ok=True)
    o = doc()
    assert o.status == "warn" and "no sudo" in o.detail and o.fix["cmd"] == f"sudo tailscale set --operator={Path.home().name}"
    assert not box.sudo_flag.exists()


def test_doctor_serve_unreadable_for_another_reason(box, doc):
    box.tailscale(serve_rc=1, serve_err="Serve is not enabled on your tailnet.")
    o = doc()
    assert o.status == "warn" and "serve status could not be read" in o.detail


@pytest.mark.parametrize("v,label,cmd", [("macos-appstore", "the App Store Tailscale app", None), ("macos-standalone", "the standalone Tailscale app", None),
                                         ("macos-opensource", "the open-source tailscaled (Homebrew)", "operator")])
def test_doctor_on_a_mac_names_the_variant_and_never_calls_sudo(box, tmp_path, monkeypatch, v, label, cmd):
    flags(monkeypatch, "macos")
    mac_layout(tmp_path, monkeypatch, bundle=v != "macos-opensource", receipt=v == "macos-appstore", daemon=v == "macos-opensource")
    box.tailscale()
    box.sudo(ok=True)
    box.operator_denied.touch()
    o = doctor._c_tailscale(None)
    assert o.status == "warn" and label in o.detail and f"({v})" in o.detail
    assert not box.sudo_flag.exists()
    if cmd:
        assert o.fix["cmd"] == f"sudo tailscale set --operator={Path.home().name}"
    else:
        assert "cmd" not in o.fix and "Open the Tailscale app" in o.fix["text"]
    box.operator_denied.unlink()
    o = doctor._c_tailscale(None)
    assert o.status == "pass" and f"({v})" in o.detail


def test_doctor_on_a_mac_with_only_the_app_bundle_runs_it_as_a_cli(box, tmp_path, monkeypatch):
    flags(monkeypatch, "macos")
    lay = mac_layout(tmp_path, monkeypatch, bundle=True, receipt=True)
    box.tailscale(name="Tailscale", directory=lay.cli.parent)
    o = doctor._c_tailscale(None)
    assert o.status == "pass" and str(lay.cli) in o.detail and "(macos-appstore)" in o.detail
    assert "be-cli=1" in box.calls()


def test_doctor_on_a_mac_with_nothing_installed_says_where_it_looked(box, tmp_path, monkeypatch):
    flags(monkeypatch, "macos")
    mac_layout(tmp_path, monkeypatch, bundle=False)
    o = doctor._c_tailscale(None)
    assert o.status == "fail" and "/Applications/Tailscale.app" in o.detail


def test_doctor_in_wsl_without_tailscale_says_what_is_needed(box, monkeypatch):
    flags(monkeypatch, "linux")
    monkeypatch.setattr(plat, "is_wsl", lambda: True)
    monkeypatch.setattr(ts.shutil, "which", lambda name: None)
    o = doctor._c_tailscale(None)
    assert o.status == "fail" and "tailscale.exe" in o.detail


def test_doctor_a_hung_command_is_a_warning_not_a_hang(box, doc, monkeypatch):
    box.script("tailscale", "exec sleep 5\n")
    monkeypatch.setattr(doctor, "TS_TIMEOUT", 0.2)
    o = doc()
    assert o.status == "warn" and "did not answer" in o.detail


def test_doctor_detail_stays_one_line_and_leaks_nothing(box, doc):
    o = doc()
    assert "\n" not in doctor._clean(o.detail) and "@" not in o.detail
