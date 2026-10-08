"""Everything the board knows about the `tailscale` command (issue #126): which binary, which variant, `serve` as the operator first.

Linux keeps exactly what app/previews.py did before: `shutil.which("tailscale") or "/usr/bin/tailscale"`, the tailscaled socket mounted into
the container (`TAILSCALE_SOCK`), `serve` run as the logged-in user, one retry through `sudo -n` (the sudoers rule install.sh writes) when it
was denied, and no retry in the container, where a denial is answered with the one-time `tailscale set --operator` fix. Off Linux there is no
sudoers rule, so there is no sudo retry on macOS or Windows: a denial becomes an error that names the Tailscale variant and the fix for it.

Variants on macOS (https://tailscale.com/kb/1065/macos-variants): the App Store app is sandboxed, the Standalone package uses a system
extension, the open-source `tailscaled` (Homebrew) has no GUI and runs as root. The CLI exists in all three. Funnel and running before
login are documented for the open-source variant only; an older page says the App Store and Standalone variants can Funnel ports (not
files), and the two pages disagree, so Funnel is treated as unsupported on the first two. What `tailscale set --operator` does on the two
GUI variants, and whether a `unix:` serve target works from the sandboxed app, are to verify (issue #130); the board always serves
`http://127.0.0.1:<port>`. Every Windows and WSL host assumption below is to verify too.

This module imports only app.platform and app.config, so the doctor, the previews module and scripts/tailscale_serve.py can all use it.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

from . import platform as plat
from .config import settings

log = logging.getLogger("ccboard.tailscale")

TAILSCALE_SOCK = Path("/var/run/tailscale/tailscaled.sock")   # mounted into the container by compose
LINUX_FALLBACK = "/usr/bin/tailscale"                          # the path the sudoers rule names
MAC_APP = Path("/Applications/Tailscale.app")
MAC_APP_CLI = MAC_APP / "Contents" / "MacOS" / "Tailscale"     # run with TAILSCALE_BE_CLI=1 it is the CLI
MAS_RECEIPT = MAC_APP / "Contents" / "_MASReceipt"             # only the App Store build carries a receipt
MAC_CLI_CANDIDATES = (Path("/usr/local/bin/tailscale"), Path("/opt/homebrew/bin/tailscale"))
MAC_DAEMON_CANDIDATES = (Path("/usr/local/bin/tailscaled"), Path("/opt/homebrew/bin/tailscaled"))   # the open-source variant's daemon
MAC_DAEMON_SOCKET = Path("/var/run/tailscaled.socket")         # exists while the open-source daemon runs
WINDOWS_CLI_NAME = "tailscale.exe"                             # under %ProgramFiles%\Tailscale (to verify)
CALL_TIMEOUT = 10                                              # seconds for a read (status, serve status)
SERVE_TIMEOUT = 60                                             # seconds for a change (as install.sh's `timeout 60`)

VARIANTS = ("linux", "macos-appstore", "macos-standalone", "macos-opensource", "windows", "wsl-host", "unknown")
LABELS = {
    "linux": "Tailscale on Linux",
    "macos-appstore": "the App Store Tailscale app",
    "macos-standalone": "the standalone Tailscale app",
    "macos-opensource": "the open-source tailscaled (Homebrew)",
    "windows": "Tailscale on Windows",
    "wsl-host": "Tailscale on the Windows host (through tailscale.exe)",
    "unknown": "an unrecognised Tailscale install",
}


class TailscaleError(Exception):
    """A tailscale command that could not be run or was refused; str() is the sentence to show."""


# ---------------------------------------------------------------- finding the command


class Cli(NamedTuple):
    """The command to run: `argv` is the prefix (one element today) and `env` the extra environment it needs ({} except for the app bundle)."""
    argv: list
    env: dict

    @property
    def exe(self) -> str:
        return self.argv[0]

    def cmd(self, *args: str) -> list[str]:
        """The full argv for `args`, for a runner that cannot set an environment: with extra variables it goes through /usr/bin/env."""
        base = [*self.argv, *socket_args()]
        if self.env:
            base = ["/usr/bin/env", *[f"{k}={v}" for k, v in self.env.items()], *base]
        return [*base, *args]


def _executable(p) -> bool:
    return os.path.isfile(p) and os.access(p, os.X_OK)


def _windows_candidates() -> list[Path]:
    root = os.environ.get("ProgramFiles") or r"C:\Program Files"
    return [Path(root) / "Tailscale" / WINDOWS_CLI_NAME]


def find_cli() -> Cli | None:
    """The tailscale command for this system, or None (missing_reason() says why). Linux: exactly the old rule, never None. Elsewhere PATH
    first, then /usr/local/bin and /opt/homebrew/bin (the Standalone launcher, Homebrew), then the App Store app's own binary, which is the CLI
    when TAILSCALE_BE_CLI=1 is set. In WSL with no `tailscale` in the distro at all, `tailscale.exe` through Windows interop (to verify)."""
    if plat.IS_LINUX:
        exe = shutil.which("tailscale")
        if exe is None and plat.is_wsl() and not os.path.exists(LINUX_FALLBACK):
            win = shutil.which(WINDOWS_CLI_NAME)
            if win:
                return Cli([win], {})
        return Cli([exe or LINUX_FALLBACK], {})
    exe = shutil.which("tailscale")
    if exe:
        return Cli([exe], {})
    if plat.IS_MACOS:
        for p in MAC_CLI_CANDIDATES:
            if _executable(p):
                return Cli([str(p)], {})
        if _executable(MAC_APP_CLI):
            return Cli([str(MAC_APP_CLI)], {"TAILSCALE_BE_CLI": "1"})
        return None
    if plat.IS_WINDOWS:
        for p in _windows_candidates():
            if _executable(p):
                return Cli([str(p)], {})
    return None


def missing_reason() -> str:
    """The sentence for a system where find_cli() found nothing."""
    if plat.IS_MACOS:
        return ("tailscale was not found on PATH, in /usr/local/bin, in /opt/homebrew/bin or in /Applications/Tailscale.app; "
                "install Tailscale from the App Store or from https://tailscale.com/download")
    if plat.IS_WINDOWS:
        return "tailscale.exe was not found on PATH or under Program Files; install Tailscale from https://tailscale.com/download (the layout is to verify)"
    if plat.is_wsl():
        return ("tailscale is not installed in this distro; with Tailscale on the Windows host the board needs tailscale.exe on PATH "
                "through Windows interop (to verify)")
    return "tailscale is not installed (https://tailscale.com/download)"


def socket_args() -> list[str]:
    """Global flags that point the CLI at a daemon socket. Empty on every system: the CLI finds its own daemon (the Linux default socket is
    left alone, the container's is the one compose mounts at TAILSCALE_SOCK)."""
    return []


# ---------------------------------------------------------------- variant and capabilities


def variant(cli: Cli | None = None) -> str:
    """Which Tailscale this is, from the file layout alone (no network, no command): 'linux', 'macos-appstore' (the app bundle has an
    App Store receipt), 'macos-standalone' (the bundle has none), 'macos-opensource' (a tailscaled binary and no bundle, or both with the
    daemon's socket present), 'windows', 'wsl-host' (the command found is tailscale.exe), else 'unknown'. A layout it cannot place is
    'unknown', never assumed to be Standalone. `tailscale version` is not parsed: what it prints per variant is to verify."""
    if plat.IS_WINDOWS:
        return "windows"
    if plat.IS_MACOS:
        bundle = MAC_APP.is_dir()
        daemon = any(_executable(p) for p in MAC_DAEMON_CANDIDATES) or shutil.which("tailscaled") is not None
        if bundle and daemon:
            return "macos-opensource" if MAC_DAEMON_SOCKET.exists() else ("macos-appstore" if MAS_RECEIPT.exists() else "macos-standalone")
        if bundle:
            return "macos-appstore" if MAS_RECEIPT.exists() else "macos-standalone"
        return "macos-opensource" if daemon else "unknown"
    if plat.IS_LINUX:
        found = cli if cli is not None else find_cli()
        if found is not None and found.exe.lower().endswith(".exe"):
            return "wsl-host"
        return "linux"
    return "unknown"


_CAPS = {
    # serve_ports, serve_files, funnel, before_login. None means "to verify" (nothing measured yet), never a guess.
    "linux": (True, True, True, True),
    "macos-appstore": (True, False, False, False),
    "macos-standalone": (True, False, False, False),
    "macos-opensource": (True, True, True, True),
    "windows": (True, None, None, None),
    "wsl-host": (True, None, None, None),
    "unknown": (True, None, None, None),
}


def capabilities(v: str) -> dict:
    """{serve_ports, serve_files, funnel, before_login} for a variant (https://tailscale.com/kb/1065/macos-variants, /1242/tailscale-serve).
    Serve ports work everywhere. Files, Funnel and running before login: the open-source variant (and Linux) only. The Funnel page says
    macOS needs an open-source variant while an older page says the App Store and Standalone ones can Funnel ports, so it stays False
    for those two. None = to verify (Windows, WSL host, unrecognised)."""
    row = _CAPS.get(v, _CAPS["unknown"])
    return dict(zip(("serve_ports", "serve_files", "funnel", "before_login"), row))


def operator_hint() -> str:
    """The container's answer to a denied `serve`: the one-time fix on the host (the container has no sudo). Home's name stands in for the
    user: the uid-1000 user is named differently in the container, and the host home is mounted at its own path."""
    user = Path.home().name or "<user>"
    return f"tailscale serve failed; on the box run: sudo tailscale set --operator={user}"


def denial_fix(v: str) -> str:
    """The fix for a refused `serve`, per variant. Only the root-daemon forms name sudo, and only as words for the person to run once."""
    user = Path.home().name or "<user>"
    if v in ("macos-opensource", "linux"):
        return f"Run once: sudo tailscale set --operator={user}"
    if v in ("macos-appstore", "macos-standalone"):
        return ("Open the Tailscale app and check that it is running and signed in as this Mac user, then try again "
                "(whether `tailscale set --operator` applies to this variant is to verify)")
    if v in ("windows", "wsl-host"):
        return "Open Tailscale from the Windows tray and check that it is running and signed in (the exact fix is to verify)"
    return ("Check that Tailscale is running and signed in, and that this user may run `tailscale serve` "
            "(the variant could not be recognised, so the fix is unknown)")


def denial_message(v: str) -> str:
    """The sentence for a `serve` that was refused off Linux: the variant by name and the fix for it. No sudo is ever run for it."""
    return f"tailscale serve failed; {LABELS.get(v, LABELS['unknown'])} refused it. {denial_fix(v)}"


# ---------------------------------------------------------------- running it


def _denied(cp) -> bool:
    text = (cp.stderr + cp.stdout).lower()
    return "denied" in text or "permission" in text or "operator" in text


def sudo_retry_allowed() -> bool:
    """A denied `serve` is retried once through `sudo -n` on Linux outside the container only: the sudoers rule exists there and nowhere else."""
    return plat.IS_LINUX and settings.runtime != "docker"


def _exec(argv: list[str], env: dict, timeout: float):
    kw = {"capture_output": True, "text": True, "timeout": timeout}
    if env:
        kw["env"] = {**os.environ, **env}
    return subprocess.run(argv, **kw)


def _invoke(cli: Cli, args: list[str], timeout: float, sudo: bool):
    """Run the command as the user; when `sudo` and sudo_retry_allowed(), a call whose output says "denied" is retried once as `sudo -n`.
    Raises subprocess.TimeoutExpired or OSError like subprocess.run."""
    cmd = [*cli.argv, *socket_args(), *args]
    cp = _exec(cmd, cli.env, timeout)
    if sudo and cp.returncode != 0 and sudo_retry_allowed() and "denied" in (cp.stderr + cp.stdout).lower():
        cp = _exec(["sudo", "-n", *cmd], cli.env, timeout)
    return cp


def serve_cmd(*args: str) -> list[str]:
    """tailscale serve <args> as an argv (the user's own command, no sudo): for runners with their own timeout, as the doctor. With no
    command found the bare name, so the runner reports a missing program."""
    cli = find_cli()
    return cli.cmd("serve", *args) if cli is not None else ["tailscale", "serve", *args]


def run_serve(args: list[str], *, sock: Path | None = None) -> None:
    """Run `tailscale serve <args>`; TailscaleError (a sentence) when it cannot be done. Linux, byte for byte as app/previews.py did: one try as
    the user, one `sudo -n` retry when denied outside the container, in the container no sudo and a denial answered with operator_hint(). Off
    Linux a denial is denial_message(variant). `sock` is the container's tailscaled socket (default TAILSCALE_SOCK)."""
    cli = find_cli()
    if cli is None:
        raise TailscaleError(missing_reason())
    docker = settings.runtime == "docker"
    sock = TAILSCALE_SOCK if sock is None else sock
    if docker and not sock.exists():
        raise TailscaleError(f"tailscaled socket {sock} is not mounted; is tailscale running on the box?")
    try:
        cp = _invoke(cli, ["serve", *args], SERVE_TIMEOUT, sudo=True)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise TailscaleError(f"tailscale serve failed: {e.__class__.__name__}")
    if cp.returncode != 0:
        if docker and _denied(cp):      # no sudo inside the container: the operator flag, set once on the box, is the way
            raise TailscaleError(operator_hint())
        if not plat.IS_LINUX and _denied(cp):
            raise TailscaleError(denial_message(variant(cli)))
        raise TailscaleError((cp.stderr or cp.stdout).strip()[-300:] or "tailscale serve failed")


def serve_on_args(https_port: int, local_port: int) -> list[str]:
    return ["--bg", f"--https={https_port}", f"http://127.0.0.1:{local_port}"]


def serve_off_args(https_port: int) -> list[str]:
    return [f"--https={https_port}", "--yes", "off"]


def serve_on(https_port: int, local_port: int) -> None:
    """Publish http://127.0.0.1:<local_port> on tailnet HTTPS port <https_port>. Port 443 is never a preview port (another service's)."""
    if int(https_port) == 443:
        raise TailscaleError("port 443 is never used for a preview")
    run_serve(serve_on_args(https_port, local_port))


def serve_off(https_port: int) -> None:
    """Turn <https_port> off (only that port, never a reset). A failure is logged and swallowed, as previews always did."""
    if int(https_port) == 443:
        log.info("serve off 443: refused (never touched)")
        return
    try:
        run_serve(serve_off_args(https_port))
    except TailscaleError as e:
        log.info("serve off %s: %s", https_port, e)


# ---------------------------------------------------------------- reading it (never raises)


class Result(NamedTuple):
    rc: int
    out: str
    err: str
    kind: str          # ok | not-installed | timeout | denied | not-running | failed


def classify(rc: int, out: str, err: str) -> str:
    """'ok', 'denied' (the operator state: denied, permission, operator), 'not-running' (no daemon to talk to) or 'failed'."""
    if rc == 0:
        return "ok"
    text = f"{err}\n{out}".lower()
    if "denied" in text or "permission" in text or "operator" in text:
        return "denied"
    if any(s in text for s in ("failed to connect", "is tailscaled running", "tailscale is stopped", "not running", "connection refused",
                               "no such file", "not mounted")):
        return "not-running"
    return "failed"


def call(args: list[str], *, timeout: float | None = None, sudo: bool = False) -> Result:
    """Run `tailscale <args>` and classify the answer; never raises. `sudo` allows the Linux sudo retry (for `serve` reads the sudoers rule covers)."""
    timeout = CALL_TIMEOUT if timeout is None else timeout
    cli = find_cli()
    if cli is None:
        return Result(-1, "", missing_reason(), "not-installed")
    if settings.runtime == "docker" and not TAILSCALE_SOCK.exists():
        return Result(-1, "", f"tailscaled socket {TAILSCALE_SOCK} is not mounted", "not-running")
    try:
        cp = _invoke(cli, args, timeout, sudo)
    except subprocess.TimeoutExpired:
        return Result(-1, "", "timed out", "timeout")
    except FileNotFoundError:
        return Result(-1, "", f"{cli.exe} is not installed", "not-installed")
    except OSError as e:
        return Result(-1, "", e.__class__.__name__, "failed")
    return Result(cp.returncode, cp.stdout or "", cp.stderr or "", classify(cp.returncode, cp.stdout or "", cp.stderr or ""))


def _json(text: str):
    """Parsed JSON; {} for the empty answer and for `null` (nothing configured); None when it is not JSON."""
    raw = (text or "").strip()
    if not raw or raw == "null":
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return None


def status() -> dict | None:
    """`tailscale status --json` parsed; None when it cannot be read (no command, no daemon, not JSON). Never raises."""
    r = call(["status", "--json"])
    if r.rc != 0:
        return None
    d = _json(r.out)
    return d if isinstance(d, dict) and d else None


def serve_status() -> dict | None:
    """`tailscale serve status --json` parsed; {} when nothing is served ("null", empty, or "No serve config"); None when it cannot be
    read (not installed, not running, denied, not JSON). Read only. Never raises."""
    r = call(["serve", "status", "--json"], sudo=True)
    if r.rc != 0:
        return None
    if r.out.strip().lower().startswith("no serve config"):
        return {}
    d = _json(r.out)
    return d if isinstance(d, dict) else None


def summarize_status(d: dict | None) -> dict:
    """What install.sh reads out of `status --json`: {state, fqdn, login, certs}. fqdn is Self.DNSName without its dot, login the owner's
    LoginName, certs how many CertDomains (HTTPS certificates enabled when above 0). Missing parts are '' / 0."""
    d = d if isinstance(d, dict) else {}
    s = d.get("Self") if isinstance(d.get("Self"), dict) else {}
    users = d.get("User") if isinstance(d.get("User"), dict) else {}
    u = users.get(str(s.get("UserID")))
    return {
        "state": str(d.get("BackendState") or ""),
        "fqdn": str(s.get("DNSName") or "").rstrip("."),
        "login": str((u or {}).get("LoginName") or "") if isinstance(u, dict) else "",
        "certs": len(d.get("CertDomains") or []),
    }


# ---------------------------------------------------------------- the mapping rules (the same as install.sh's serve_check)


def mapping_state(d: dict | None, fqdn: str, port, path: str, target: str, board_port: int | str | None) -> str:
    """Is <path> on tailnet HTTPS port <port> ours? 'ours' | 'missing' | 'stale' (a ccboard handler with an old backend port) | 'funnel' |
    'foreign:<why>'. Mirrors install.sh's serve_check: a Funnel on the port wins, a TCP forward is foreign, on the board's own port the
    handlers "/" and "/tty" are ours and on every other port only "/", any other handler is foreign."""
    d = d if isinstance(d, dict) else {}
    port, board_port = str(port), str(board_port)
    key = f"{fqdn}:{port}"
    if (d.get("AllowFunnel") or {}).get(key):
        return "funnel"
    tcp = (d.get("TCP") or {}).get(port) or {}
    if tcp and not tcp.get("HTTPS"):
        return "foreign:port is used for TCP forwarding"
    handlers = ((d.get("Web") or {}).get(key) or {}).get("Handlers") or {}
    ours = {"/", "/tty"} if port == board_port else {"/"}
    for p, h in handlers.items():
        if p not in ours:
            return f"foreign:handler {p} -> {h}"
    h = handlers.get(path)
    if h is None:
        return "missing"
    proxy = h.get("Proxy") if isinstance(h, dict) else None
    if proxy == target:
        return "ours"
    if isinstance(proxy, str) and proxy.startswith("http://127.0.0.1:"):
        return "stale"
    return f"foreign:handler {path} -> {h}"


def serve_path_on_args(https_port: int, path: str, target: str) -> list[str]:
    """install.sh's forms: `--bg --https=P target` for "/", else `--bg --https=P --set-path PATH target`."""
    if path == "/":
        return ["--bg", f"--https={https_port}", target]
    return ["--bg", f"--https={https_port}", "--set-path", path, target]


def serve_path_off_args(https_port: int, path: str) -> list[str]:
    return [f"--https={https_port}", "--set-path", path, "off"]
