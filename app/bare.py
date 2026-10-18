"""Devices without a board: validated transport, explicit session actions and terminal tickets."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import selectors
import secrets
import time
import re
import shlex
import stat
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

# Keep transport and ticket reading stdlib-only: ttyd also runs on the Docker host.
def _data_dir(value=None):
    if value is not None:
        return Path(value)
    from .config import settings
    return settings.data_dir


log = logging.getLogger(__name__)
PREFIX = "bare:device:"
LOCK = threading.Lock()


class DeviceBusy(ValueError):
    """A device cannot be removed while it still owns open sessions."""


CHECKS = ("ssh", "user", "groups", "system", "tmux", "claude", "codex", "repos")
REPOS = (r'cd "$HOME/$1" || exit 1; find . -maxdepth 4 '
         r'\( -name node_modules -o -name .venv \) -prune -o -name .git -prune -print')


RUNNER_TIMEOUT = 12                  # seconds one command on a device may take


def runner(argv: list[str], *, stdin: str | None = None, stdout_limit: int = 32768):
    """The only process seam. Tests replace it; no shell on the board and no prompts in argv."""
    output = [bytearray(), bytearray()]
    limits = (stdout_limit, 8192)
    deadline = time.monotonic() + RUNNER_TIMEOUT
    pending = memoryview((stdin or "").encode())
    p = subprocess.Popen(argv, stdin=subprocess.PIPE if pending else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    capped = False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(p.stdout, selectors.EVENT_READ, 0)
            selector.register(p.stderr, selectors.EVENT_READ, 1)
            if pending:
                selector.register(p.stdin, selectors.EVENT_WRITE, 2)
            while selector.get_map() and not capped:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, RUNNER_TIMEOUT)
                for key, _ in selector.select(remaining):
                    if key.data == 2:
                        try:
                            pending = pending[os.write(p.stdin.fileno(), pending[:4096]):]
                        except BrokenPipeError:
                            pending = pending[:0]
                        if not pending:
                            selector.unregister(p.stdin)
                            p.stdin.close()
                        continue
                    buf, limit = output[key.data], limits[key.data]
                    chunk = key.fileobj.read(min(4096, limit - len(buf) + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    available = limit - len(buf)
                    buf.extend(chunk[:available])
                    if len(chunk) > available:
                        capped = True
                        p.kill()
                        break
        p.wait(timeout=max(0, deadline - time.monotonic()))
    except BaseException:
        p.kill()
        p.wait()
        raise
    finally:
        for pipe in (p.stdin, p.stdout, p.stderr):
            if pipe is not None:
                pipe.close()
    return subprocess.CompletedProcess(argv, 125 if capped else p.returncode,
                                       *(bytes(buf).decode(errors="replace") for buf in output))


def candidates(status, owner: str, *, online=True) -> list[dict]:
    from . import nodes_discovery as discovery
    from .auth import normalize
    if not isinstance(status, dict):
        return []
    users = status.get("User")
    users = users if isinstance(users, dict) else {}
    uid = next((p.get("ID") for p in users.values() if isinstance(p, dict)
                and isinstance(p.get("LoginName"), str) and normalize(p["LoginName"]) == normalize(owner)), None)
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        uid = None
    tags = {t for p in discovery._peers(status) for t in discovery._tags(p.get("Tags"))}
    suffix = discovery.magic_suffix(status)
    if not suffix:
        return []
    rows = discovery.candidates(status, tags, uid)
    return [r for r in rows if r["dns_name"] and r["dns_name"].lower().endswith("." + suffix)
            and (r["online"] or not online) and r["ips"]]


def validate(user: str, folder: str) -> None:
    if not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) or user == "root":
        raise ValueError("Choose a dedicated non-root user, such as ccagent.")
    if (not folder or len(folder) > 200 or folder.startswith("/")
            or any(p in ("", ".", "..") for p in folder.split("/"))
            or not re.fullmatch(r"[A-Za-z0-9_./ -]+", folder)):
        raise ValueError("Use a projects folder relative to that user's home, without dot or parent segments.")


def _host_ok(host):
    return (isinstance(host, str) and 0 < len(host) <= 253
            and all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label) for label in host.split(".")))


def _control_path(host, user, data_dir=None):
    key = hashlib.sha256((host + user).encode()).hexdigest()[:16]
    return _data_dir(data_dir) / "bare-ssh" / key


def forget_host_key(host, user, *, close_master=True):
    """Only local trust state and the local master socket; never open a connection."""
    validate(user, "projects")
    if not _host_ok(host):
        raise ValueError("Refresh the device list before forgetting its host key.")
    try:
        if close_master:
            runner(["ssh", "-F", "/dev/null", "-o", "ControlPath=" + shlex.quote(str(_control_path(host, user))),
                    "-O", "exit", user + "@" + host], stdin=None)
    except (OSError, subprocess.TimeoutExpired):
        pass  # The master may already have exited or SSH may have been uninstalled.
    known = _data_dir() / "bare-ssh" / "known_hosts"
    if known.exists():
        lines = []
        for line in known.read_text().splitlines(keepends=True):
            fields = line.split()
            index = 1 if fields and fields[0].startswith("@") else 0
            if len(fields) > index and host in fields[index].split(","):
                aliases = [a for a in fields[index].split(",") if a != host]
                if not aliases:
                    continue
                line = line.replace(fields[index], ",".join(aliases), 1)
            lines.append(line)
        known.write_text("".join(lines))


def argv(device: dict, user: str, check: str, folder: str = "projects", *, session=None, agent=None, mode=None, repo=None, model="", effort="") -> list[str]:
    """Fixed verbs, a validated tailnet destination, and one quoted remote command."""
    validate(user, folder)
    commands = {"ssh": ["true"], "user": ["id", "-u"], "groups": ["id", "-Gn"], "system": ["uname"],
                "tmux": ["tmux", "-V"], "agents": ["command", "-v", "claude", "codex"],
                "repos": ["sh", "-c", REPOS, "bare-repos", folder]}
    if check in ("start", "list", "stop", "kill"):
        commands[check] = session_command(check, session=session, agent=agent, mode=mode, folder=folder, repo=repo, model=model, effort=effort)
    remote = shlex.join(commands[check])
    return ssh_argv(device, user) + [remote]


def ssh_argv(device, user, *, data_dir=None, tty=False):
    """Shared pinned transport. Attach has separate, validated remote argv words."""
    validate(user, "projects")
    if not isinstance(device, dict) or not _host_ok(device.get("dns_name")):
        raise ValueError("The device has no valid Tailscale name. Refresh the device list.")
    host = device["dns_name"]
    addresses = device.get("ips")
    if not isinstance(addresses, list):
        raise ValueError("The device has no Tailscale address. Refresh the device list.")
    addr = next((a for a in addresses if tailnet_address(a)), None)
    if not addr:
        raise ValueError("The device has no Tailscale address. Refresh the device list.")
    directory = _data_dir(data_dir) / "bare-ssh"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    options = ["BatchMode=yes", "ConnectTimeout=8", "StrictHostKeyChecking=" + ("yes" if tty else "accept-new"),
               "UserKnownHostsFile=" + shlex.quote(str(directory / "known_hosts")), "GlobalKnownHostsFile=/dev/null",
               "ControlMaster=auto", "ControlPersist=30", "ControlPath=" + shlex.quote(str(_control_path(host, user, data_dir))),
               "ForwardAgent=no", "ClearAllForwardings=yes", "RequestTTY=" + ("yes" if tty else "no"), "PubkeyAuthentication=no",
               "PasswordAuthentication=no", "KbdInteractiveAuthentication=no", "HostName=" + addr, "HostKeyAlias=" + host]
    return ["ssh", "-F", "/dev/null", *[part for opt in options for part in ("-o", opt)],
            *(["-t"] if tty else []), user + "@" + host]


def tailnet_address(addr):
    if not isinstance(addr, str) or "%" in addr:
        return False
    try:
        ip = ipaddress.ip_address(addr)
        return ip in ipaddress.ip_network("100.64.0.0/10") or ip in ipaddress.ip_network("fd7a:115c:a1e0::/48")
    except ValueError:
        return False


def _run(device, user, check, folder):
    try:
        p = runner(argv(device, user, check, folder), stdin=None)
        return p.returncode, p.stdout[:32768], p.stderr[:8192]
    except subprocess.TimeoutExpired as e:
        err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return 124, "", err[:8192]
    except FileNotFoundError:
        return 127, "", "board ssh executable not found"
    except OSError:
        return 1, "", ""


def login_link(text: str) -> str | None:
    for link in re.findall(r"https://[^\s<>\"']+", text):
        u = urlsplit(link)
        if u.hostname == "login.tailscale.com" and u.netloc == "login.tailscale.com" and not u.fragment:
            return link
    return None


def _last_seen_age(value):
    try:
        seen = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        seconds = max(0, int(time.time() - seen.timestamp()))
    except (ValueError, TypeError, AttributeError, OverflowError):
        return "unknown"
    for duration, unit in ((86400, "d"), (3600, "h"), (60, "min"), (1, "sec")):
        if seconds >= duration or duration == 1:
            return f"{seconds // duration} {unit} ago"


def check(device: dict, user: str, folder: str) -> dict:
    validate(user, folder)
    name = device["name"]
    rows = []
    result = {"ok": False, "rows": rows, "repos": [], "login_url": None}

    def row(key, ok, message, fix=""):
        rows.append({"key": key, "ok": bool(ok), "message": message, "fix": "" if ok else fix})

    def stop():
        for key in CHECKS:
            if not any(r["key"] == key for r in rows):
                row(key, False, f"The {key} check did not run.", "Fix the failed check, then Check again.")
                rows[-1]["skipped"] = True
        return result

    if not device["online"]:
        seen = f" (last seen {_last_seen_age(device['last_seen'])})" if device.get("last_seen") else ""
        row("ssh", False, f"{name} is offline{seen}. Nothing was started.", "Wake it and try again.")
        rows[-1]["last_seen"] = device.get("last_seen")
        return stop()
    rc, _, err = _run(device, user, "ssh", folder)
    result["login_url"] = login_link(err) if rc else None
    diagnostic = err.lower()
    action = None
    message = f"SSH could not reach {name} as {user}. Nothing was started."
    fix = "Check Tailscale SSH and the ACL for this user, then Check again."
    if "remote host identification has changed" in diagnostic or ("host key for" in diagnostic and "has changed" in diagnostic):
        message = f"The host key has changed for {name}. SSH was stopped."
        fix = "Verify the device's new host key, then use Forget host key and Check again."
        action = "forget_host_key"
        result["login_url"] = None
    elif rc == 127 and diagnostic == "board ssh executable not found":
        message = "SSH is not installed on the board host."
        fix = "Install the OpenSSH client on the board host, then Check again."
    elif rc == 124 or "timed out" in diagnostic or "timeout" in diagnostic:
        message = f"The SSH connection timed out for {name}; it was stopped."
        fix = "Wake the device and check its Tailscale connection, then Check again."
    elif "permission denied" in diagnostic or "access denied" in diagnostic or "does not permit" in diagnostic:
        message = f"The tailnet rule denied SSH access to {name} as {user}."
        fix = ("See docs/nodes/device-check.md: use an SSH ACL check rule (12-hour check period) "
               "for an untagged board, or accept for a tagged board. Grant only this board access to the dedicated user.")
    elif result["login_url"]:
        message = f"{name} asks for a Tailscale check."
        fix = "Open the link, sign in, press Retry."
    row("ssh", not rc, f"SSH works for {user} on {name}." if not rc else message, fix)
    if rc:
        if action:
            rows[-1]["action"] = action
        return stop()
    rc, out, _ = _run(device, user, "user", folder)
    safe = not rc and out.strip().isdigit() and int(out.strip()) > 0
    row("user", safe, f"{user} is a non-root user on {name}." if safe else f"{user} could not be verified as a non-root user on {name}.",
        "Choose a dedicated non-root account, then Check again.")
    if not safe:
        return stop()
    rc, out, _ = _run(device, user, "groups", folder)
    groups = out.split()
    safe = not rc and bool(groups) and all(re.fullmatch(r"[A-Za-z0-9_.-]+", g) for g in groups) and not {g.lower() for g in groups} & {"sudo", "wheel", "admin"}
    row("groups", safe, f"{user}'s groups: {', '.join(groups)}." if safe else f"{user} is refused on {name}: groups are unreadable or include sudo, wheel or admin.",
        "Use an account outside sudo, wheel and admin, then Check again.")
    if not safe:
        return stop()
    rc, out, _ = _run(device, user, "system", folder)
    system = out.strip()
    ok = not rc and system in ("Linux", "Darwin")
    row("system", ok, f"{name} runs {system}." if ok else f"A supported system could not be verified on {name}.", "Use Linux or macOS with Tailscale SSH, then Check again.")
    rc, out, _ = _run(device, user, "tmux", folder)
    ok = not rc and bool(re.fullmatch(r"tmux [0-9][A-Za-z0-9. -]{0,40}", out.strip()))
    row("tmux", ok, f"{out.strip()} is installed for {user} on {name}." if ok else f"tmux is not installed for {user} on {name}.", "Install it, then Check again.")
    rc, out, _ = _run(device, user, "agents", folder)
    paths = out.splitlines() if rc in (0, 1) else []
    for agent in ("claude", "codex"):
        ok = any(p.startswith("/") and PurePosixPath(p).name == agent for p in paths)
        row(agent, ok, f"{agent} is on {user}'s PATH on {name}. Sign-in has not been checked." if ok else f"{agent} is not on {user}'s PATH on {name}.",
            f"Install {agent} for {user}, then Check again.")
    rc, out, _ = _run(device, user, "repos", folder)
    repos = []
    for line in out.splitlines():
        if line.startswith("./") and line.endswith("/.git") and all(c.isprintable() for c in line):
            rel = line[2:-5]
            if rel and not any(p in ("", ".", "..") for p in rel.split("/")):
                repos.append(rel)
        elif line == "./.git":
            repos.append(".")
    readable = rc == 0 or (rc == 1 and bool(repos))
    result["repos"] = sorted(set(repos))[:200] if readable else []
    if rc == 124:
        row("repos", False, "Listing the projects folder took too long; it was stopped.",
            "Try Check again, or use a smaller projects folder.")
    else:
        row("repos", readable, f"Found {len(result['repos'])} repositories under ~/{folder}." if readable else f"The projects folder ~/{folder} could not be read on {name}.",
            "Create that folder for this user, or remove and add the device with another folder.")
    result["ok"] = all(r["ok"] for r in rows if r["key"] not in ("claude", "codex"))
    return result


def listing(db, owner: str) -> dict:
    from . import nodes_discovery as discovery
    status, info = discovery.read_tailscale(fresh=True)
    devices = [v for _, v, _ in db.kv_list_prefix(PREFIX) if v.get("owner") == owner]
    return {"candidates": candidates(status, owner), "devices": devices, "tailscale": info}


def action(db, owner: str, verb: str, tid: str, user="ccagent", folder="projects", *, confirm=False, discard_sessions=False) -> dict:
    from . import nodes, nodes_discovery as discovery
    ok = False
    with LOCK:
        try:
            if not discovery.nodes_id_ok(tid):
                raise ValueError("Refresh the list and choose a device from it.")
            key = PREFIX + tid
            saved = db.kv_get(key)
            saved = saved["value"] if saved else None
            if saved and saved.get("owner") != owner:
                raise ValueError("This device was added by another person.")
            if verb == "remove":
                if discard_sessions:
                    if not saved or not confirm:
                        raise ValueError("Confirm removing this device and forgetting its sessions here with a second tap.")
                    db.bare_forget(tid)
                    nodes.audit("out", tid, "bare_forget", True, user=owner, db=db)
                elif db.bare_open_rows(tid):
                    raise DeviceBusy("Stop its sessions first, or refresh the list, or remove it and forget its sessions here (they keep running on the device).")
                if saved:
                    host = saved.get("dns_name")
                    if not host and not discard_sessions:  # Records made before aliases were saved.
                        status, _ = discovery.read_tailscale(fresh=True)
                        host = next((p.get("DNSName", "").rstrip(".") for p in discovery._peers(status)
                                     if p.get("ID") == tid), None)
                    if host:
                        forget_host_key(host, saved["user"], close_master=not discard_sessions)
                db.kv_del(key)
                ok = True
                return {"ok": True}
            if verb == "check":
                if not saved:
                    raise ValueError("Add this device before checking it again.")
                user, folder = saved["user"], saved["folder"]
            if verb == "forget_host_key" and saved:
                user, folder = saved["user"], saved["folder"]
            validate(user, folder)
            status, info = discovery.read_tailscale(fresh=True)
            device = next((r for r in candidates(status, owner, online=False) if r["ts_id"] == tid), None)
            if not device:
                raise ValueError(info.get("reason") or "This device is no longer yours or tagged on your tailnet. Refresh the list.")
            if verb == "forget_host_key":
                forget_host_key(device["dns_name"], user)
                if saved:
                    saved["check"] = {"ok": False, "rows": [{"key": "ssh", "ok": False,
                        "message": "Host key forgotten. Check again to pin the device's current key.", "fix": "Check again."}],
                        "repos": [], "login_url": None}
                    db.kv_set(key, saved)
                ok = True
                return {"ok": True}
            result = check(device, user, folder)
            nodes.audit("out", tid, "bare_check", result["ok"], user=owner, db=db)
            permitted = all(r["ok"] for r in result["rows"] if r["key"] in ("ssh", "user", "groups"))
            record = {"ts_id": tid, "name": device["name"], "dns_name": device["dns_name"], "os": device["os"], "tags": device["tags"], "user": user,
                      "folder": folder, "owner": owner, "check": {**result, "login_url": None}}
            if saved and saved.get("state_log") and saved.get("user") == user:   # hooks live under one device user's home
                record["state_log"] = saved["state_log"]
            if permitted or saved:
                db.kv_set(key, record)
            ok = permitted
            return {"added": bool(permitted or saved), "device": record, "check": result}
        finally:
            if verb != "check" or not locals().get("result"):
                nodes.audit("out", tid, "bare_" + verb, ok, user=owner, db=db)


# All device session commands, including the nested login-shell command, live here.
START = 'cd "$HOME/$1" || exit 1; shift; exec "$@"'
MODES = {"claude": ("default", "manual", "acceptEdits", "plan"), "codex": ("workspace-write", "read-only")}


def launch_line(agent, mode, model="", effort=""):
    from . import nodes_relay
    # Check the mode before constructing even the agent argv. Defaults belong at the API boundary.
    if agent not in (*MODES, "shell"):
        raise ValueError("Choose Claude, Codex or a shell.")
    if agent == "shell":
        if mode is not None or model or effort:
            raise ValueError("Shell sessions do not take agent options.")
        return "sh -l"
    if mode not in MODES[agent]:
        raise ValueError("Choose an explicit permission mode for this agent.")
    if model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,79}", model):
        raise ValueError("Choose a valid model name.")
    if effort and effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ValueError("Choose a supported effort.")
    parts = (["claude", "--permission-mode", mode] if agent == "claude" else
             ["codex", "-s", mode, "-a", "on-request", "-c", "check_for_update_on_startup=false"])
    if model:
        parts += ["--model" if agent == "claude" else "-m", model]
    if effort:
        parts += (["--effort", effort] if agent == "claude" else ["-c", 'model_reasoning_effort="' + effort + '"'])
    line = shlex.join(parts)
    why = nodes_relay.launch_line_refusal(agent, line)
    if why:
        raise ValueError("This launch was refused: " + why + ".")
    return line


def session_socket():
    from . import nodes
    return "ccboard-bare-" + hashlib.sha256(nodes.node_id().encode()).hexdigest()[:8]


def session_command(verb, *, session=None, agent=None, mode=None, folder="projects", repo=None, model="", effort="", socket=None):
    line = launch_line(agent, mode, model, effort) if verb == "start" else None
    if verb != "list" and (not isinstance(session, str) or not re.fullmatch(r"bare-[a-f0-9]{32}", session)):
        raise ValueError("Choose a session started by this board.")
    if verb == "start":
        validate("ccagent", folder)
        if repo != ".":
            validate("ccagent", repo or "")
    socket = session_socket() if socket is None else socket
    if not isinstance(socket, str) or not re.fullmatch(r"ccboard-bare-[a-f0-9]{8}", socket):
        raise ValueError("Invalid device session socket.")
    prefix = ["tmux", "-L", socket]
    if verb == "start":
        command = shlex.join(["sh", "-c", START, "bare-start", (folder if repo == "." else folder + "/" + repo), *shlex.split(line)])
        return prefix + ["new-session", "-d", "-s", session, command]
    if verb == "attach":
        return prefix + ["attach-session", "-t", "=" + session]
    if verb == "list":
        return prefix + ["list-sessions", "-F", "#{session_name}"]
    if verb == "stop":
        if agent not in ("claude", "codex", "shell"):
            raise ValueError("Unknown session agent.")
        exit_line = {"claude": "/exit", "codex": "/quit", "shell": "exit"}[agent]
        return prefix + ["send-keys", "-t", "=" + session + ":", "-l", exit_line, ";", "send-keys", "-t", "=" + session + ":", "Enter"]
    if verb == "kill":
        return prefix + ["kill-session", "-t", "=" + session]
    raise ValueError("Unknown device operation.")


def stored_devices(db, owner):
    """Local-only view: the ordinary board refresh never contacts a device."""
    return [{**v, "sessions": list(db.bare_open_rows(host=v["ts_id"]).values())}
            for _, v, _ in db.kv_list_prefix(PREFIX) if v.get("owner") == owner]


def _ready(db, owner, tid):
    from . import nodes_discovery as discovery
    saved = db.kv_get(PREFIX + tid)
    saved = saved["value"] if saved else None
    if not saved or saved.get("owner") != owner:
        raise ValueError("Add and check your device first.")
    if not saved.get("check", {}).get("ok"):
        raise ValueError("Check this device successfully before using it.")
    status, _ = discovery.read_tailscale(fresh=True)
    device = next((r for r in candidates(status, owner, online=False) if r["ts_id"] == tid), None)
    if not device:
        raise ValueError("This device is no longer yours or tagged on your tailnet.")
    if not device["online"]:
        raise ValueError("This device is offline. Wake it and try again.")
    return saved, device


def _session_run(saved, device, verb, **kw):
    return runner(argv(device, saved["user"], verb, saved["folder"], **kw), stdin=None)


def _alive(saved, device):
    p = _session_run(saved, device, "list")
    if p.returncode == 1 and ("no server running on" in p.stderr or "No such file or directory" in p.stderr):
        return set()
    if p.returncode or any(not re.fullmatch(r"[A-Za-z0-9_-]+", n) for n in p.stdout.splitlines()):
        raise ValueError("The device could not be read; its sessions may still run.")
    return set(p.stdout.splitlines())


def session_action(db, owner, tid, verb, *, session=None, repo=None, agent="claude", mode=None, model="", effort="", confirm=False):
    from . import nodes
    result_status = "failed"
    with LOCK:
        try:
            if verb == "start":
                launch_line(agent, mode, model, effort)  # gate before any device argv
            saved, device = _ready(db, owner, tid)
            if verb == "attach":
                if session not in db.bare_open_rows(host=tid):
                    raise ValueError("Choose an open session on this device.")
                try:
                    ticket = issue_ticket(device, saved["user"], session)
                except OSError:  # nothing reached the device: say so, not "did not answer"
                    raise ValueError("The board could not prepare the terminal ticket. Check that its data directory is writable.") from None
                result_status = "ok"
                return {"attach_url": "/term/" + ticket}
            if verb == "list":
                before = datetime.now(timezone.utc).isoformat()
                alive = _alive(saved, device)
                db.bare_reconcile(tid, alive, before)
                for name, row in db.bare_open_rows(host=tid).items():
                    if name in alive and row["state"] in ("alive", "unconfirmed"):
                        db.bare_set_state(tid, name, "alive", "bare_list")
                result_status = "ok"
                return {"sessions": list(db.bare_open_rows(host=tid).values())}
            if verb == "start":
                if len(db.bare_open_rows(host=tid)) >= 20:
                    raise DeviceBusy("This device already has 20 open sessions; stop some first.")
                if repo not in saved["check"].get("repos", []):
                    raise ValueError("Choose a repository from this device's check.")
                if agent != "shell" and not any(r["key"] == agent and r["ok"] for r in saved["check"]["rows"]):
                    raise ValueError("The agent is not on this device's PATH. Install it and check again.")
                # Recheck privilege immediately before a start, even if the saved check passed.
                uid = _session_run(saved, device, "user")
                groups = _session_run(saved, device, "groups")
                if (uid.returncode or not uid.stdout.strip().isdigit() or int(uid.stdout.strip()) == 0 or groups.returncode
                        or not groups.stdout.strip() or not re.fullmatch(r"[A-Za-z0-9_ .-]+", groups.stdout.strip())
                        or set(groups.stdout.split()) & {"sudo", "wheel", "admin"}):
                    raise ValueError("Use a non-root account outside sudo, wheel and admin; check again.")
                session = "bare-" + secrets.token_hex(16)
                command = argv(device, saved["user"], "start", saved["folder"], session=session, agent=agent,
                               mode=mode, repo=repo, model=model, effort=effort)
                db.add_bare_session(tmux_name=session, repo=repo, agent=agent,
                               host=tid, cmd=launch_line(agent, mode, model, effort))
                db.bare_set_state(tid, session, "unconfirmed", "bare_start")  # durable before sending; never replayed
                try:
                    p = runner(command, stdin=None)
                except subprocess.TimeoutExpired:
                    result_status = "unconfirmed"
                except OSError:
                    db.bare_end(tid, session, "start_failed")
                    raise ValueError("SSH could not start. Nothing was started.") from None
                else:
                    if p.returncode == 0:
                        result_status = "alive"
                    elif p.returncode == 255:
                        result_status = "unconfirmed"
                    else:
                        db.bare_end(tid, session, "start_failed")
                        raise ValueError("The device refused the start. Check it and try again.")
                db.bare_set_state(tid, session, result_status, "bare_start")
                return {"session": db.bare_open_rows(host=tid)[session], "status": result_status}
            row = db.bare_open_rows(host=tid).get(session)
            if verb != "stop" or not row:
                raise ValueError("Choose an open session on this device.")
            if not confirm:
                raise ValueError("Confirm Stop on this device with a second tap.")
            if row.get("state") == "stopping":
                return {"status": "stopping"}
            try:
                p = _session_run(saved, device, "stop", session=session, agent=row["agent"])
            except subprocess.TimeoutExpired:
                rc = 255  # The exit command may have arrived despite the lost acknowledgement.
            else:
                rc = p.returncode
            if rc not in (0, 255):
                raise ValueError("Stop failed; read the device before trying again.")
            db.bare_set_state(tid, session, "stopping", "bare_stop")
            timer = threading.Timer(10, finish_stop, args=(db, owner, tid, session))
            timer.daemon = True
            timer.start()
            if rc == 255:
                raise ValueError("Stop was not confirmed; read the device before trying again.")
            result_status = "ok"
            return {"status": "stopping"}
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("The device did not answer; its session may still run.") from None
        finally:
            nodes.audit("out", tid, "bare_" + verb, result_status in ("ok", "alive"), user=owner,
                        status=result_status if result_status == "unconfirmed" else None,
                        target=session if verb == "attach" else None, db=db)


def finish_stop(db, owner, tid, session):
    """One delayed check, never a poller. A failed read never authorizes a kill."""
    from . import nodes
    attempted = False
    ok = False
    status = None
    try:
        with LOCK:
            if session not in db.bare_open_rows(host=tid):
                return
            attempted = True
            saved, device = _ready(db, owner, tid)
            if session in _alive(saved, device):
                if _session_run(saved, device, "kill", session=session).returncode:
                    return
                ok = True
            else:
                status = "already_ended"
            db.bare_end(tid, session, "bare_stop")
    except (ValueError, OSError, subprocess.TimeoutExpired):
        pass  # Leave stopping visible for the next explicit read.
    finally:
        if attempted:
            nodes.audit("out", tid, "bare_kill", ok, user=owner, status=status, db=db)


TICKET_TTL = 600
TICKET_PATTERN = r"bare-ticket-[a-f0-9]{64}"


def ticket_id_ok(value):
    return isinstance(value, str) and re.fullmatch(TICKET_PATTERN, value) is not None


def ticket_argv(ticket, *, data_dir=None, now=None):
    """Validate the complete credential before building any process arguments."""
    fields = {"device", "user", "socket", "session", "issued_at", "expires_at"}
    if not isinstance(ticket, dict) or set(ticket) != fields:
        raise ValueError("Invalid terminal ticket. Open the terminal again from the board.")
    issued, expires = ticket["issued_at"], ticket["expires_at"]
    now = time.time() if now is None else now
    if (any(type(v) not in (int, float) or not 0 <= v <= 1e12 for v in (issued, expires))
            or not issued <= now < expires or not 0 < expires - issued <= TICKET_TTL):
        raise ValueError("Terminal ticket expired. Open the terminal again from the board.")
    device = ticket["device"]
    if (not isinstance(device, dict) or set(device) != {"dns_name", "ips"}
            or not isinstance(device["ips"], list) or len(device["ips"]) != 1
            or not tailnet_address(device["ips"][0])):
        raise ValueError("Invalid device in terminal ticket.")
    if not isinstance(ticket["socket"], str) or not re.fullmatch(r"ccboard-bare-[a-f0-9]{8}", ticket["socket"]):
        raise ValueError("Invalid device session socket.")
    remote = session_command("attach", session=ticket["session"], socket=ticket["socket"])
    return ssh_argv(device, ticket["user"], data_dir=data_dir, tty=True) + remote


def _ticket_directory(data_dir=None):
    directory = _data_dir(data_dir) / "bare-tickets"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = directory.lstat()
    if not stat.S_ISDIR(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o700 or st.st_uid != os.getuid():
        raise ValueError("Terminal ticket directory must be private to the board user.")
    return directory


def issue_ticket(device, user, session):
    now = time.time()
    ticket = {"device": {"dns_name": device["dns_name"], "ips": [next(a for a in device["ips"] if tailnet_address(a))]},
              "user": user, "session": session, "socket": session_socket(), "issued_at": now, "expires_at": now + TICKET_TTL}
    ticket_argv(ticket, now=now)
    directory = _ticket_directory()
    # Expired credentials have no further use. Do not follow links while pruning.
    for path in directory.iterdir():
        if ticket_id_ok(path.name) and path.lstat().st_mtime < now - TICKET_TTL:
            path.unlink(missing_ok=True)
    name = "bare-ticket-" + secrets.token_hex(32)
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(ticket, f)
    except BaseException:
        (directory / name).unlink(missing_ok=True)
        raise
    return name


def read_ticket(name, *, data_dir=None):
    if not ticket_id_ok(name):
        raise ValueError("Invalid terminal ticket. Open the terminal from the board.")
    try:
        directory = _ticket_directory(data_dir)
        fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600 or st.st_uid != os.getuid() or st.st_size > 4096:
                raise ValueError("Terminal ticket must be a private file owned by the board user.")
            ticket = json.loads(f.read(4097))
        return ticket_argv(ticket, data_dir=data_dir)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Terminal ticket is missing or unreadable. Open the terminal again from the board.") from None


# Poll output is NUL-framed: tmux's printable capture cannot manufacture a record boundary.
# Each requested session is passed as a separate validated shell argument, never interpolated.
POLL = r'''set -f
sock=$1; want=$2; shift 2
alive=$(tmux -L "$sock" list-sessions -F '#{session_name}') || exit $?
printf '%s\000' "$alive"
now=; log=
if [ "$want" = 1 ]; then
  now=$(date +%s)
  log=$(tail -c 16384 "$HOME/.ccboard-bare/state.log" 2>/dev/null | tr -d '\000')
fi
printf '%s\000%s\000' "$now" "$log"
for name do
  found=false
  for live in $alive; do [ "$live" != "$name" ] || found=true; done
  "$found" || continue
  cmd=$(tmux -L "$sock" display -p -t "=$name:" '#{pane_current_command}') || exit $?
  screen=$(tmux -L "$sock" capture-pane -p -S -40 -t "=$name:") || exit $?
  printf '%s\000%s\000%s\000' "$name" "$cmd" "$screen"
done
'''

_prompts = WeakKeyDictionary()
_prompt_lock = threading.Lock()


def _screen_read(row, command, text):
    from .agents import claude_pane, codex_pane
    agent = row['agent']
    parser = (claude_pane if agent == 'claude' and claude_pane.foreground(command) else
              codex_pane if agent == 'codex' and command == 'codex' else None)
    if parser is None:
        return ('waiting' if agent == 'shell' and codex_pane.shell_foreground(command) else 'unknown'), None
    dialog = parser.approval_dialog(text)
    fingerprint = hashlib.sha256(json.dumps([row['id'], command, text]).encode()).hexdigest()
    return parser.screen_state(text), (dialog, fingerprint) if dialog else None


def _screens(saved, device, rows, want_log=False):
    """(alive names, {name: (screen state, dialog)}, (device clock, log tail) or None). The log is read only when asked for."""
    names = list(rows)
    for name in names:
        session_command('attach', session=name)  # validate before constructing transport argv
    remote = shlex.join(['sh', '-c', POLL, 'bare-poll', session_socket(), '1' if want_log else '0', *names])
    p = runner(ssh_argv(device, saved['user']) + [remote], stdin=None, stdout_limit=2 * 1024 * 1024)
    if p.returncode == 1 and not p.stdout and ('no server running on' in p.stderr or 'No such file or directory' in p.stderr):
        return set(), {}, None
    error = 'The device could not be read; its sessions may still run.'
    if p.returncode or not p.stdout.endswith('\0'):
        raise ValueError(error)
    parts = p.stdout[:-1].split('\0')
    if len(parts) < 3 or (len(parts) - 3) % 3:
        raise ValueError(error)
    alive = set(parts[0].splitlines())
    if any(not re.fullmatch(r'[A-Za-z0-9_-]+', n) for n in alive):
        raise ValueError(error)
    screens = {}
    for i in range(3, len(parts), 3):
        name, command, text = parts[i:i + 3]
        if name not in rows or name not in alive or name in screens:
            raise ValueError(error)
        screens[name] = _screen_read(rows[name], command, text)
    if screens.keys() != alive.intersection(rows):
        raise ValueError(error)
    log = (int(parts[1]), parts[2]) if want_log and re.fullmatch(r'[0-9]{1,12}', parts[1]) else None
    return alive, screens, log


# ---- the optional state log on a device (issue #277) ----
# A tiny hook, installed only by the explicit Install state log button, appends one line per state change to ~/.ccboard-bare/state.log:
# {"state":"working","session":"bare-<32 hex>","time":<epoch seconds>}. It never reads the hook's input, so no prompt text, tool input or
# path can reach the file. The script is this fixed text; nothing from a request is ever part of it.
STATE_DIR = ".ccboard-bare"
HOOK_REL = STATE_DIR + "/hook"
HOOK_FILES = {"claude": ".claude/settings.json", "codex": ".codex/hooks.json"}
HOOK_EVENTS = {"SessionStart": "waiting", "UserPromptSubmit": "working", "Stop": "waiting", "PermissionRequest": "dialog"}
LOG_STATES = ("working", "waiting", "dialog")
LOG_STALE = 3600       # a log line older than this is no evidence (seconds)
LOG_FIRST_SIGHT = 30   # after a board restart only a line this young is believed over the screen
LOG_MAX_LINES = 400

HOOK_SCRIPT = r'''#!/bin/sh
# ccboard state log for a device without a board. Arguments: <agent> <event>.
# Appends {"state":...,"session":...,"time":...} to ~/.ccboard-bare/state.log when a ccboard-bare tmux session changes state.
# It drains the hook's input without reading it: prompt text, tool input and paths never reach the log.
# The log is capped at 64 KB: past that it moves to state.log.1 and the newest 50 lines carry over.
umask 077
cat >/dev/null 2>&1
case "$2" in
  SessionStart|Stop) state=waiting ;;
  UserPromptSubmit) state=working ;;
  PermissionRequest) state=dialog ;;
  *) exit 0 ;;
esac
case "$TMUX" in */ccboard-bare-*,*) ;; *) exit 0 ;; esac
[ -n "$TMUX_PANE" ] || exit 0
session=$(tmux display-message -p -t "$TMUX_PANE" '#{session_name}' 2>/dev/null) || exit 0
case "$session" in bare-*) ;; *) exit 0 ;; esac
id=${session#bare-}
case "$id" in *[!a-f0-9]*) exit 0 ;; esac
[ "${#id}" -eq 32 ] || exit 0
dir=$HOME/.ccboard-bare
log=$dir/state.log
mkdir -p "$dir" || exit 0
last=$(grep -F "\"session\":\"$session\"" "$log" 2>/dev/null | tail -n 1)
case "$last" in *"\"state\":\"$state\""*) exit 0 ;; esac
if [ -f "$log" ] && [ "$(($(wc -c < "$log") + 0))" -gt 65536 ]; then
  mv -f "$log" "$log.1" && tail -n 50 "$log.1" > "$log"
fi
printf '{"state":"%s","session":"%s","time":%s}\n' "$state" "$session" "$(date +%s)" >> "$log"
exit 0
'''

# Read step: HOME, then for each settings file its kind (file, missing or link), its cksum and its text, NUL-framed.
HOOK_READ = r'''set -f
printf '%s\000' "$HOME"
for f in .claude/settings.json .codex/hooks.json; do
  p=$HOME/$f
  if [ -L "$p" ]; then printf 'link\000\000\000'
  elif [ -e "$p" ]; then
    sum=$(cksum < "$p") || exit 1
    text=$(cat "$p") || exit 1
    printf 'file\000%s\000%s\000' "$sum" "$text"
  else printf 'missing\000missing\000\000'
  fi
done
'''

# Write step: stdin is the new content. The file is replaced only if it still has the checksum the board read (exit 3 if not), after one
# backup of the previous version (.ccboard-bak), through a temporary file in the same folder and an atomic rename. A symlink is refused (4).
# The board passes the byte count it sends: a stream that ended early (a timeout, a kill or a broken pipe still gives the device a clean
# EOF) leaves a short temporary file, which is removed with the original untouched (exit 5).
HOOK_WRITE = r'''set -f
umask 077
rel=$1; sum=$2; mode=$3; size=$4
f=$HOME/$rel; tmp=$f.ccboard-tmp
mkdir -p "${f%/*}" || exit 1
[ ! -L "$f" ] || exit 4
if [ -e "$f" ]; then
  if [ "$sum" != any ]; then
    [ "$(cksum < "$f")" = "$sum" ] || exit 3
    cp -p "$f" "$f.ccboard-bak" || exit 1
  fi
  cp -p "$f" "$tmp" || exit 1
else
  [ "$sum" = missing ] || [ "$sum" = any ] || exit 3
  : > "$tmp" && chmod "$mode" "$tmp" || exit 1
fi
cat > "$tmp" || { rm -f "$tmp"; exit 1; }
[ "$(($(wc -c < "$tmp") + 0))" -eq "$size" ] || { rm -f "$tmp"; exit 5; }
mv -f "$tmp" "$f" || { rm -f "$tmp"; exit 1; }
'''

HOOK_CLEAN = r'''set -f
rm -f "$HOME/.ccboard-bare/hook" "$HOME/.ccboard-bare/state.log" "$HOME/.ccboard-bare/state.log.1"
rmdir "$HOME/.ccboard-bare" 2>/dev/null
exit 0
'''


class HookRefused(ValueError):
    """A plain sentence for the person; the file it names was not written."""


def hook_argv(device, user, step, *, rel=None, expected=None, mode=None, size=None):
    """The three fixed state-log scripts, each one quoted command. Only a path from the fixed table, a checksum, a file mode and a byte count vary."""
    validate(user, "projects")
    if step == "read":
        command = ["sh", "-c", HOOK_READ, "bare-hook-read"]
    elif step == "clean":
        command = ["sh", "-c", HOOK_CLEAN, "bare-hook-clean"]
    elif step == "write":
        if rel not in (HOOK_REL, *HOOK_FILES.values()):
            raise ValueError("Unknown state log file.")
        if not isinstance(expected, str) or not re.fullmatch(r"any|missing|[0-9]{1,10} [0-9]{1,12}", expected):
            raise ValueError("Unreadable file checksum.")
        if not isinstance(mode, str) or not re.fullmatch(r"[0-7]{3}", mode):
            raise ValueError("Unknown file mode.")
        if type(size) is not int or not 0 <= size <= 99999999:
            raise ValueError("Unknown file size.")
        command = ["sh", "-c", HOOK_WRITE, "bare-hook-write", rel, expected, mode, str(size)]
    else:
        raise ValueError("Unknown state log step.")
    return ssh_argv(device, user) + [shlex.join(command)]


def hook_command(home, agent, event):
    if agent not in HOOK_FILES or event not in HOOK_EVENTS:
        raise ValueError("Unknown hook.")
    return shlex.join([home + "/" + HOOK_REL, agent, event])


def _our_hook(hook):
    return isinstance(hook, dict) and hook.get("type") == "command" and HOOK_REL in str(hook.get("command", ""))


def _drop_ours(groups):
    out = []
    for g in groups:
        if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
            out.append(g)
            continue
        kept = [h for h in g["hooks"] if not _our_hook(h)]
        if kept:
            out.append({**g, "hooks": kept})
    return out


def _upsert(groups, hook):
    """Replace our entry where it already sits, drop any second one, else append a group of our own after the person's."""
    out, placed = [], False
    for g in groups:
        if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
            out.append(g)
            continue
        hooks = []
        for h in g["hooks"]:
            if not _our_hook(h):
                hooks.append(h)
            elif not placed:
                hooks.append(dict(hook))
                placed = True
        if hooks:
            out.append({**g, "hooks": hooks})
    if not placed:
        out.append({"hooks": [dict(hook)]})
    return out


def hook_merge(text, label, agent, home, *, install):
    """The settings text with our hooks added (install) or taken out (remove), or None when nothing would change. Everything of the
    person's stays; a file this cannot understand raises HookRefused, and the caller writes nothing at all."""
    try:
        data = json.loads(text) if text.strip() else {}
    except (ValueError, RecursionError):
        raise HookRefused(f"{label} is not valid JSON, so nothing was changed. Fix or remove that file on the device, then try again.") from None
    if not isinstance(data, dict):
        raise HookRefused(f"{label} is not a JSON object, so nothing was changed. Fix it on the device, then try again.")
    hooks = data.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise HookRefused(f"{label} has a hooks entry that is not an object, so nothing was changed. Fix it on the device, then try again.")
    for event in HOOK_EVENTS:
        if hooks and event in hooks and not isinstance(hooks[event], list):
            raise HookRefused(f"{label} has hooks.{event} that is not a list, so nothing was changed. Fix it on the device, then try again.")
    new = json.loads(json.dumps(data))
    hooks = new.setdefault("hooks", {}) if install else new.get("hooks")
    if install:
        for event in HOOK_EVENTS:
            entry = {"type": "command", "command": hook_command(home, agent, event), "timeout": 5}
            if event != "PermissionRequest":
                entry["async"] = True
            hooks[event] = _upsert(hooks.get(event, []), entry)
    elif hooks:
        for event in list(hooks):
            if isinstance(hooks[event], list):
                groups = _drop_ours(hooks[event])
                if groups:
                    hooks[event] = groups
                else:
                    del hooks[event]
        if not hooks:
            new.pop("hooks")
    return None if new == data else json.dumps(new, indent=2) + "\n"


def _hook_read(device, user):
    p = runner(hook_argv(device, user, "read"), stdin=None, stdout_limit=600000)
    parts = p.stdout[:-1].split("\0") if p.stdout.endswith("\0") else []
    unreadable = HookRefused("The device's settings could not be read, so nothing was changed. Check the device and try again.")
    if p.returncode or len(parts) != 7 or not re.fullmatch(r"/[A-Za-z0-9_.+@/-]{1,200}", parts[0]) or ".." in parts[0].split("/"):
        raise unreadable
    files = {}
    for agent, i in (("claude", 1), ("codex", 4)):
        kind, sum_, text = parts[i:i + 3]
        if kind == "link":
            raise HookRefused(f"~/{HOOK_FILES[agent]} is a link, so nothing was changed. Edit it by hand on the device.")
        if kind not in ("file", "missing") or not re.fullmatch(r"missing|[0-9]{1,10} [0-9]{1,12}", sum_):
            raise unreadable
        files[agent] = (kind, sum_, text)
    return parts[0], files


def _hook_write(device, user, rel, expected, content, mode):
    p = runner(hook_argv(device, user, "write", rel=rel, expected=expected, mode=mode, size=len(content.encode())), stdin=content)
    if p.returncode == 3:
        raise HookRefused(f"~/{rel} changed on the device while the board worked, so it was not overwritten. Try again.")
    if p.returncode == 4:
        raise HookRefused(f"~/{rel} is a link, so it was not changed. Edit it by hand on the device.")
    if p.returncode == 5:
        raise HookRefused(f"The device received only part of ~/{rel}, so it was not changed. Try again.")
    if p.returncode:
        raise HookRefused(f"The device would not save ~/{rel}. Check the device and try again; the change is safe to repeat.")


def hook_action(db, owner, tid, verb, *, confirm=False):
    """Install or remove the state log hooks on one checked device, only on an explicit second tap. Both settings files are read, merged
    on the board and written back only when they change (checksum-guarded, backed up, atomic). Idempotent. A refusal writes nothing."""
    from . import nodes
    ok = False
    with LOCK:
        try:
            if verb not in ("install", "remove"):
                raise ValueError("Choose Install state log or Remove state log.")
            if not confirm:
                raise ValueError(f"Confirm {'installing' if verb == 'install' else 'removing'} the state log with a second tap.")
            saved, device = _ready(db, owner, tid)
            user, install = saved["user"], verb == "install"
            agents = ([a for a in HOOK_FILES if any(r["key"] == a and r["ok"] for r in saved["check"]["rows"])]
                      if install else list(HOOK_FILES))
            if install and not agents:
                raise ValueError("Neither Claude nor Codex is on this device's PATH. Install one, check the device again, then install the state log.")
            written, refusal = [], None
            try:
                home, files = _hook_read(device, user)
                plan = []
                for agent in agents:
                    kind, sum_, text = files[agent]
                    if kind == "missing" and not install:
                        continue
                    new = hook_merge(text, "~/" + HOOK_FILES[agent], agent, home, install=install)
                    if new is not None:
                        plan.append((agent, sum_, new))
                if install:
                    _hook_write(device, user, HOOK_REL, "any", HOOK_SCRIPT, "700")
                for agent, sum_, new in plan:
                    _hook_write(device, user, HOOK_FILES[agent], sum_, new, "600")
                    written.append(agent)
                if not install:
                    runner(hook_argv(device, user, "clean"), stdin=None)
            except HookRefused as e:
                refusal = e
            if install and (written or refusal is None):
                saved["state_log"] = {"installed": True, "agents": agents if refusal is None else written,
                                      "at": datetime.now(timezone.utc).isoformat()}
                db.kv_set(PREFIX + tid, saved)
            if refusal:
                raise ValueError(str(refusal)) from None
            if not install:
                saved.pop("state_log", None)
                db.kv_set(PREFIX + tid, saved)
            ok = True
            return {"ok": True, "state_log": saved.get("state_log"), "changed": written}
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("The device did not answer. The change is safe to repeat; check the device, then try again.") from None
        finally:
            nodes.audit("out", tid, "bare_hook_" + verb, ok, user=owner, db=db)


def parse_log(now, text):
    """{session: (state, time)}: the newest valid line of each session. A line that is not exactly {state, session, time} with a known state,
    a board-made session name and a plausible time is skipped, so a truncated first line or a corrupt log can only cost evidence."""
    out = {}
    for line in text.splitlines()[-LOG_MAX_LINES:]:
        try:
            item = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(item, dict) or set(item) != {"state", "session", "time"}:
            continue
        state, session, at = item["state"], item["session"], item["time"]
        if (state not in LOG_STATES or not isinstance(session, str) or not re.fullmatch(r"bare-[a-f0-9]{32}", session)
                or type(at) is not int or not 0 < at <= now + 300):
            continue
        if session not in out or at >= out[session][1]:
            out[session] = (state, at)
    return out


def merged_state(memory, key, entry, screen, now):
    """(state, event label). The screen is always read. A log line wins when it is a new event since the last poll (after a board restart:
    a young one) and holds only until the screen itself changes, so a stale line (Esc sends no hook) can not outlive what the pane shows.
    A visible dialog is hard evidence and always wins, and a log 'dialog' never overrides a screen that shows no dialog (answering a
    permission sends no hook). While a dialog shows, the newest line's time stays in memory as a floor, so a later first sight of that
    line or an older one cannot bring the dialog back. No line, or a stale one, means the screen read alone."""
    if entry is None or now - entry[1] > LOG_STALE:
        memory.pop(key, None)
        return screen, "bare_screen"
    state, at = entry
    seen = memory.get(key)
    if screen == "dialog":
        memory[key] = {"at": max(at, seen["at"]) if seen else at, "state": "dialog", "screen": "dialog"}
        return screen, "bare_screen"
    if seen is None or at > seen["at"]:
        if (seen is None and now - at > LOG_FIRST_SIGHT) or state == "dialog":
            memory[key] = {"at": at, "state": screen, "screen": screen}
            return screen, "bare_screen"
        memory[key] = {"at": at, "state": state, "screen": screen}
        return state, "bare_log" if state != screen else "bare_screen"
    if screen == seen["screen"]:
        return seen["state"], "bare_log" if seen["state"] != screen else "bare_screen"
    memory[key] = {"at": seen["at"], "state": screen, "screen": screen}
    return screen, "bare_screen"


_log_memory = WeakKeyDictionary()


def poll_device(db, owner, tid):
    """One SSH batch; validate the entire reading before changing any row. No per-tick audit."""
    from . import permissions
    with LOCK:
        rows = db.bare_open_rows(tid)
        if not rows:
            return
        saved, device = _ready(db, owner, tid)
        want_log = bool((saved.get('state_log') or {}).get('installed'))
        before = datetime.now(timezone.utc).isoformat()
        alive, screens, log = _screens(saved, device, rows, want_log)
        entries = parse_log(*log) if log else {}
        memory = _log_memory.setdefault(db, {})
        for key in [k for k in memory if k[0] == tid and k[1] not in rows]:
            del memory[key]
        db.bare_reconcile(tid, alive, before)
        notices = []
        with _prompt_lock:
            cache = _prompts.setdefault(db, {})
            for name, row in rows.items():
                state, read = screens.get(name, ('unknown', None))
                if row['state'] == 'stopping':
                    read = None
                elif name in alive:
                    event = 'bare_screen'
                    if log and name in screens and row['agent'] in ('claude', 'codex'):
                        state, event = merged_state(memory, (tid, name), entries.get(name), state, log[0])
                    if row['state'] != state:
                        db.bare_set_state(tid, name, state, event)
                key = (tid, name)
                old_id = cache.get(key, {}).get('public', {}).get('id')
                public = permissions.terminal_observe(cache, key, name, read, rearm=True, bare_device=tid)
                if public and public['id'] != old_id:
                    notices.append((public, row))
    for public, row in notices:
        permissions.push_request(public['id'], row['tmux_name'], public['summary'],
                                 context_row={**row, 'project': saved['name'], 'bare_device': tid})


def pending(db, owner):
    """Cached screen requests for this person only; never part of the node relay's permissions."""
    with _prompt_lock:
        out = []
        cache = _prompts.get(db, {})
        for key in list(cache):
            tid, name = key
            saved = db.kv_get(PREFIX + tid)
            if not saved or name not in db.bare_open_rows(tid):
                del cache[key]
                continue
            old = cache[key]
            if saved['value'].get('owner') == owner and old['active'] and not old['answered']:
                out.append(dict(old['public']))
        return out


def prompt_get(db, pid):
    with _prompt_lock:
        return next((dict(p['public']) for p in _prompts.get(db, {}).values() if p['public']['id'] == pid), None)


def answer(db, owner, tid, session, decision, *, pid=None, text=None):
    from . import nodes, nodes_relay, permissions, projects
    status = 'failed'
    with LOCK:
        try:
            if decision not in ('allow', 'deny', 'reply'):
                raise ValueError('Choose Allow, Deny or Reply.')
            if decision == 'reply':
                text = nodes_relay.free_text(text, 16000)
                if not text.strip():
                    raise ValueError('Write a reply first.')
            saved, device = _ready(db, owner, tid)
            row = db.bare_open_rows(tid).get(session)
            if not row or row['state'] == 'stopping':
                raise ValueError('Choose an open session on this device.')
            _, screens, _ = _screens(saved, device, {session: row})
            state, read = screens.get(session, ('unknown', None))
            prefix = session_command('attach', session=session)[:3]
            target = '=' + session + ':'
            if decision == 'reply':
                if state != 'waiting' or read:
                    raise projects.Conflict('The session is not at its input prompt. Open the terminal; nothing was sent.')
                # A named buffer avoids another person's paste buffer. Enter only after a good paste; the buffer is deleted
                # whatever happens (a failed paste would leave the reply in the device's tmux server) and the chain's status is kept.
                buffer = 'bare-' + secrets.token_hex(16)
                commands = [prefix + ['load-buffer', '-b', buffer, '-'],
                            prefix + ['paste-buffer', '-d', '-p', '-b', buffer, '-t', target],
                            prefix + ['send-keys', '-t', target, 'Enter']]
                cleanup = shlex.join(prefix + ['delete-buffer', '-b', buffer])
            else:
                with _prompt_lock:
                    old = _prompts.get(db, {}).get((tid, session))
                    if not old or old['public']['id'] != pid:
                        raise projects.Conflict(permissions.TERMINAL_CHANGED)
                    dialog = permissions.terminal_consume(old, read)
                commands = [prefix + ['send-keys', '-t', target, '-l', dialog[decision]]]
                if dialog.get('enter'):
                    commands.append(prefix + ['send-keys', '-t', target, 'Enter'])
            remote = ' && '.join(shlex.join(c) for c in commands)
            if decision == 'reply':
                remote = shlex.join(['sh', '-c', f'{remote}; rc=$?; {cleanup} 2>/dev/null; exit $rc'])
            try:
                p = runner(ssh_argv(device, saved['user']) + [remote], stdin=text if decision == 'reply' else None)
            except subprocess.TimeoutExpired:
                status = 'unconfirmed'
                raise ValueError('The answer is unconfirmed. Open the terminal before trying again.') from None
            if p.returncode:
                status = 'unconfirmed'
                raise ValueError('The answer is unconfirmed. Open the terminal before trying again.')
            status = 'ok'
            db.bare_set_state(tid, session, 'working', 'bare_answer')
            return {'ok': True, 'status': status}
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('The device did not answer; nothing more was sent. Open the terminal before trying again.') from None
        finally:
            nodes.audit('out', tid, 'bare_answer', status == 'ok', decision if decision in ('allow', 'deny', 'reply') else None,
                        user=owner, target=session, status=status, db=db)


class Poller:
    """One worker, one batch per due device. Empty devices never reach discovery or SSH."""
    DELAYS = (10, 30, 60, 120, 300)

    def __init__(self, db):
        self.db = db
        self.due = {}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='bare-poll', daemon=True)

    def tick(self, now=None):
        from .auth import normalize
        from .config import settings
        real_clock = now is None
        now = time.monotonic() if real_clock else now
        active = set()
        for _, saved, _ in self.db.kv_list_prefix(PREFIX):
            try:
                tid, owner = saved['ts_id'], saved['owner']
            except (KeyError, TypeError) as e:       # a malformed saved record must not stop the other devices
                log.warning('bare poll skipped a malformed device record: %r', e)
                continue
            try:
                if normalize(owner) not in {normalize(u) for u in settings.allowed_users} or not self.db.bare_open_rows(tid):
                    continue
                active.add(tid)
                due, failures = self.due.get(tid, (0, 0))
                if now < due or self.stop.is_set():
                    continue
                try:
                    poll_device(self.db, owner, tid)
                except (ValueError, OSError, subprocess.TimeoutExpired):
                    delay = self.DELAYS[min(failures, len(self.DELAYS) - 1)]
                    failures += 1
                else:
                    delay, failures = 10, 0
            except Exception as e:                   # never die: a locked database or bad record only backs this device off
                log.warning('bare poll of %s failed: %s: %s', tid, type(e).__name__, e)
                active.add(tid)
                due, failures = self.due.get(tid, (0, 0))
                delay = self.DELAYS[min(failures, len(self.DELAYS) - 1)]
                failures += 1
            self.due[tid] = ((time.monotonic() if real_clock else now) + delay, failures)
        self.due = {k: v for k, v in self.due.items() if k in active}

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as e:                   # the listing itself can fail (locked database); keep the thread alive
                log.warning('bare poll tick failed: %s: %s', type(e).__name__, e)
            self.stop.wait(1)
