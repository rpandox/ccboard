"""Devices without a board: explicit checks only, through one replaceable runner."""
from __future__ import annotations

import hashlib
import os
import selectors
import secrets
import time
import re
import shlex
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from . import nodes, nodes_discovery as discovery, nodes_relay
from .auth import normalize
from .config import settings

PREFIX = "bare:device:"
LOCK = threading.Lock()


class DeviceBusy(ValueError):
    """A device cannot be removed while it still owns open sessions."""


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


def argv(device: dict, user: str, check: str, folder: str = "projects", *, session=None, agent=None, mode=None, repo=None, model="", effort="") -> list[str]:
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
    if check in ("start", "list", "stop", "kill"):
        commands[check] = session_command(check, session=session, agent=agent, mode=mode, folder=folder, repo=repo, model=model, effort=effort)
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


def action(db, owner: str, verb: str, tid: str, user="ccagent", folder="projects", *, confirm=False, discard_sessions=False) -> dict:
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


def session_command(verb, *, session=None, agent=None, mode=None, folder="projects", repo=None, model="", effort=""):
    line = launch_line(agent, mode, model, effort) if verb == "start" else None
    if verb != "list" and (not isinstance(session, str) or not re.fullmatch(r"bare-[a-f0-9]{32}", session)):
        raise ValueError("Choose a session started by this board.")
    if verb == "start":
        validate("ccagent", folder)
        if repo != ".":
            validate("ccagent", repo or "")
    socket = "ccboard-bare-" + hashlib.sha256(nodes.node_id().encode()).hexdigest()[:8]
    prefix = ["tmux", "-L", socket]
    if verb == "start":
        command = shlex.join(["sh", "-c", START, "bare-start", (folder if repo == "." else folder + "/" + repo), *shlex.split(line)])
        return prefix + ["new-session", "-d", "-s", session, command]
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
    result_status = "failed"
    with LOCK:
        try:
            if verb == "start":
                launch_line(agent, mode, model, effort)  # gate before any device argv
            saved, device = _ready(db, owner, tid)
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
                        status=result_status if result_status == "unconfirmed" else None, db=db)


def finish_stop(db, owner, tid, session):
    """One delayed check, never a poller. A failed read never authorizes a kill."""
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
