"""Devices without a board: explicit checks only, through one replaceable runner."""
from __future__ import annotations

import hashlib
import os
import selectors
import time
import re
import shlex
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from . import nodes, nodes_discovery as discovery
from .auth import normalize
from .config import settings

PREFIX = "bare:device:"
LOCK = threading.Lock()
CHECKS = ("ssh", "user", "groups", "system", "tmux", "claude", "codex", "repos")
REPOS = (r'cd "$HOME/$1" || exit 1; find . -maxdepth 4 '
         r'\( -name node_modules -o -name .venv \) -prune -o -name .git -prune -print')


def runner(argv: list[str], *, stdin: str | None = None):
    """The only process seam. Tests replace it; no shell on the board and no prompts in argv."""
    output = [bytearray(), bytearray()]
    limits = (32768, 8192)
    deadline = time.monotonic() + 12
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
                    raise subprocess.TimeoutExpired(argv, 12)
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
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) or user == "root":
        raise ValueError("Choose a dedicated non-root user, such as ccagent.")
    if (not folder or len(folder) > 200 or folder.startswith("/")
            or any(p in ("", ".", "..") for p in folder.split("/"))
            or not re.fullmatch(r"[A-Za-z0-9_./ -]+", folder)):
        raise ValueError("Use a projects folder relative to that user's home, without dot or parent segments.")


def _host_ok(host):
    return (isinstance(host, str) and 0 < len(host) <= 253
            and all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label) for label in host.split(".")))


def _control_path(host, user):
    key = hashlib.sha256((host + user).encode()).hexdigest()[:16]
    return settings.data_dir / "bare-ssh" / key


def forget_host_key(host, user):
    """Only local trust state and the local master socket; never open a connection."""
    validate(user, "projects")
    if not _host_ok(host):
        raise ValueError("Refresh the device list before forgetting its host key.")
    try:
        runner(["ssh", "-F", "/dev/null", "-o", "ControlPath=" + shlex.quote(str(_control_path(host, user))),
                "-O", "exit", user + "@" + host], stdin=None)
    except (OSError, subprocess.TimeoutExpired):
        pass  # The master may already have exited or SSH may have been uninstalled.
    known = settings.data_dir / "bare-ssh" / "known_hosts"
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


def argv(device: dict, user: str, check: str, folder: str = "projects") -> list[str]:
    """Fixed verbs, a validated tailnet destination, and one quoted remote command."""
    validate(user, folder)
    host = device["dns_name"]
    if not _host_ok(host):
        raise ValueError("The device has no valid Tailscale name. Refresh the device list.")
    addr = next((a for a in device.get("ips", []) if nodes.in_tailnet(a)), None)
    if not addr:
        raise ValueError("The device has no Tailscale address. Refresh the device list.")
    commands = {"ssh": ["true"], "user": ["id", "-u"], "groups": ["id", "-Gn"], "system": ["uname"],
                "tmux": ["tmux", "-V"], "agents": ["command", "-v", "claude", "codex"],
                "repos": ["sh", "-c", REPOS, "bare-repos", folder]}
    remote = shlex.join(commands[check])
    directory = settings.data_dir / "bare-ssh"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    options = ["BatchMode=yes", "ConnectTimeout=8", "StrictHostKeyChecking=accept-new",
               "UserKnownHostsFile=" + shlex.quote(str(directory / "known_hosts")), "GlobalKnownHostsFile=/dev/null",
               "ControlMaster=auto", "ControlPersist=30", "ControlPath=" + shlex.quote(str(_control_path(host, user))),
               "ForwardAgent=no", "ClearAllForwardings=yes", "RequestTTY=no", "PubkeyAuthentication=no",
               "PasswordAuthentication=no", "KbdInteractiveAuthentication=no", "HostName=" + addr, "HostKeyAlias=" + host]
    return ["ssh", "-F", "/dev/null", *[part for opt in options for part in ("-o", opt)], user + "@" + host, remote]


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
        fix = ("See README → Devices without a board: use an SSH ACL check rule (12-hour check period) "
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
    status, info = discovery.read_tailscale(fresh=True)
    devices = [v for _, v, _ in db.kv_list_prefix(PREFIX) if v.get("owner") == owner]
    return {"candidates": candidates(status, owner), "devices": devices, "tailscale": info}


def action(db, owner: str, verb: str, tid: str, user="ccagent", folder="projects") -> dict:
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
                if saved:
                    host = saved.get("dns_name")
                    if not host:  # Records made before aliases were saved.
                        status, _ = discovery.read_tailscale(fresh=True)
                        host = next((p.get("DNSName", "").rstrip(".") for p in discovery._peers(status)
                                     if p.get("ID") == tid), None)
                    if host:
                        forget_host_key(host, saved["user"])
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
            if permitted or saved:
                db.kv_set(key, record)
            ok = permitted
            return {"added": bool(permitted or saved), "device": record, "check": result}
        finally:
            if verb != "check" or not locals().get("result"):
                nodes.audit("out", tid, "bare_" + verb, ok, user=owner, db=db)
