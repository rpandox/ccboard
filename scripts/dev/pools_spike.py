#!/usr/bin/env python3
"""Pools P0 spike (issue #219): does a session written under login A resume under login B, for Claude Code and for Codex?

    python3 scripts/dev/pools_spike.py [--dry-run] [--only claude|codex|both] [--base DIR] [--codex-sqlite shared|separate]

What it does, in order (docs/spikes/pools-p0.md has the questions this answers and how to read the result):

  1. Builds scratch profiles under a fresh temp directory it prints: Claude A and B (`projects/` symlinked to one shared directory),
     Codex A and B (`sessions/` symlinked to one shared directory, one shared CODEX_SQLITE_HOME, the file credential store pinned in
     each config.toml). It never uses the real ~/.claude, ~/.claude.json or ~/.codex, and refuses to run if a profile would resolve to them.
  2. Prints the login command for each profile and waits for Enter while you log in from another terminal. It checks that the login
     worked through the CLI's own status command; it never opens a credential file (it only asks whether the expected file exists).
  3. Runs three turns in A and a recall question in B, non-interactively (`claude -p --session-id/--resume --output-format json`,
     `codex exec` / `codex exec resume`), and records: B answered with the word from turn 1, the account B reports (masked), whether the
     transcript landed in the shared directory, and whether the real home directories changed.
  4. Records the usage check through the CLIs' own commands only (no authenticated HTTP from this script).
  5. Writes results.json, summary.md and issue-comment.md into the scratch directory and prints the paste-ready comment for the issue.

Secrets: no credential file is read, no token, id or e-mail address is printed (an address is shown masked, `a***@b***.com`). The scratch
directory holds real logins until you remove it: the last lines print how.

`--dry-run` prints every command and runs no CLI. Standard library only, Python 3.11 or newer.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import selectors
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable

# Variables that change which credential, identity or storage a CLI uses (docs/spikes/pools-p0.md). Every child process runs without them.
SCRUB = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
    "CLAUDE_SECURESTORAGE_CONFIG_DIR", "CLAUDE_CODE_ACCOUNT_UUID", "CLAUDE_CODE_USER_EMAIL", "CLAUDE_CODE_ORGANIZATION_UUID",
    "CLAUDE_CODE_PROJECT_DIR_NAME", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_HOME", "CODEX_SQLITE_HOME",
)
SCRUB_PREFIXES = ("ANTHROPIC_FEDERATION_",)
PIN_VARS = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "CODEX_SQLITE_HOME")      # the variables this script sets; none may point at a real home

WORDS = ("kumquat", "marmalade", "tangerine", "saffron", "juniper", "quince", "walnut", "bergamot")
TURN_TIMEOUT = 240
CONFIG_TOML = ('# written by scripts/dev/pools_spike.py: the login stays in a file inside this profile and never goes to the Keychain\n'
               'cli_auth_credentials_store = "file"\n')

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
LONG_TOKEN = re.compile(r"\b(?=[A-Za-z0-9_.\-]*\d)[A-Za-z0-9_.\-]{32,}\b")
KEY_PREFIX = re.compile(r"\b(?:sk-|eyJ|Bearer\s+)[A-Za-z0-9_.\-]+")


class Refused(Exception):
    """The run would touch a real home directory."""


# ------------------------------------------------------------------------------------------------ masking
def mask_email(address: str) -> str:
    local, _, domain = address.partition("@")
    host, _, tld = domain.rpartition(".")
    return f"{local[:1]}***@{host[:1]}***.{tld}" if host else f"{local[:1]}***"


def mask_word(text: str) -> str:
    return f"{text[:1]}***" if text else ""


def scrub(text: str, paths: dict[str, str] | None = None) -> str:
    """Make a line safe to print or paste: addresses masked, ids and token-shaped strings removed, known paths named by role."""
    out = text
    for real, name in sorted((paths or {}).items(), key=lambda kv: -len(kv[0])):
        if real:
            out = out.replace(real, name)
    out = KEY_PREFIX.sub("<redacted>", out)
    out = EMAIL.sub(lambda m: mask_email(m.group(0)), out)
    out = UUID.sub(lambda m: m.group(0)[:8] + "...", out)
    return LONG_TOKEN.sub("<redacted>", out)


def short_id(value: str | None) -> str | None:
    return value[:8] + "..." if value else None


# ------------------------------------------------------------------------------------------------ real homes and layout
def protected_paths() -> dict[str, Path]:
    """The real Claude and Codex locations: the defaults under the user's home, and whatever the caller's environment already points at."""
    home = Path(os.path.expanduser("~"))
    found = {"~/.claude": home / ".claude", "~/.claude.json": home / ".claude.json", "~/.codex": home / ".codex"}
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "CODEX_SQLITE_HOME"):
        if os.environ.get(var):
            found[f"${var}"] = Path(os.environ[var])
    return found


def within(path: str | os.PathLike, root: str | os.PathLike) -> bool:
    """`path` is `root` or inside it, symlinks resolved on both sides."""
    a, b = os.path.realpath(path), os.path.realpath(root)
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


def refuse_real(label: str, path: str | os.PathLike) -> None:
    """Raise Refused when `path` is, is inside, or contains a real Claude or Codex home (symlinks resolved)."""
    for name, real in protected_paths().items():
        if within(path, real):
            raise Refused(f"refusing to run: {label} ({path}) resolves to or inside {name}. This spike only uses scratch profiles.")


def build_layout(root: Path, codex_sqlite: str) -> dict[str, Path]:
    """The scratch tree. Every path is a realpath, because Claude hashes the exact string it is given for its Keychain item and Codex canonicalizes."""
    root = Path(os.path.realpath(root))
    p = {
        "root": root, "work": root / "work", "results": root / "results",
        "claude_A": root / "claude" / "A", "claude_B": root / "claude" / "B", "claude_shared": root / "shared" / "claude" / "projects",
        "codex_A": root / "codex" / "A", "codex_B": root / "codex" / "B", "codex_shared": root / "shared" / "codex" / "sessions",
        "codex_sqlite": root / "shared" / "codex" / "sqlite",
    }
    for name, path in p.items():
        if name not in ("root",):
            refuse_real(name, path)
            path.mkdir(parents=True, exist_ok=True)
    for side in "AB":
        (p[f"claude_{side}"] / "projects").symlink_to(p["claude_shared"], target_is_directory=True)
        (p[f"codex_{side}"] / "sessions").symlink_to(p["codex_shared"], target_is_directory=True)
        (p[f"codex_{side}"] / "config.toml").write_text(CONFIG_TOML, encoding="utf-8")
    if codex_sqlite == "separate":
        p["codex_sqlite"].rmdir()
    return p


def claude_env(layout: dict[str, Path], side: str) -> dict[str, str]:
    return {"CLAUDE_CONFIG_DIR": str(layout[f"claude_{side}"])}


def codex_env(layout: dict[str, Path], side: str, codex_sqlite: str) -> dict[str, str]:
    env = {"CODEX_HOME": str(layout[f"codex_{side}"])}
    if codex_sqlite == "shared":
        env["CODEX_SQLITE_HOME"] = str(layout["codex_sqlite"])
    return env


def keychain_names(layout: dict[str, Path]) -> dict[str, dict[str, str]]:
    """The Keychain items the research expects (docs/spikes/pools-p0.md): shown so the owner can look, never read by this script."""
    out: dict[str, dict[str, str]] = {"claude": {}, "codex": {}}
    for side in "AB":
        d = str(layout[f"claude_{side}"])
        out["claude"][side] = "Claude Code-credentials-" + hashlib.sha256(d.encode()).hexdigest()[:8]
        c = str(layout[f"codex_{side}"])
        out["codex"][side] = "cli|" + hashlib.sha256(c.encode()).hexdigest()[:16]
    return out


def snapshot(paths: list[Path]) -> dict[str, dict[str, list[int]]]:
    """stat only, one level deep: name -> [mtime_ns, size]. Nothing is opened."""
    snap: dict[str, dict[str, list[int]]] = {}
    for path in paths:
        entries: dict[str, list[int]] = {}
        try:
            st = os.stat(path)
            entries["."] = [st.st_mtime_ns, st.st_size]
            if path.is_dir():
                with os.scandir(path) as it:
                    for e in it:
                        try:
                            s = e.stat(follow_symlinks=False)
                            entries[e.name] = [s.st_mtime_ns, s.st_size]
                        except OSError:
                            pass
        except OSError:
            pass
        snap[str(path)] = entries
    return snap


def changed(before: dict, after: dict) -> list[str]:
    out = []
    for key in sorted(set(before) | set(after)):
        b, a = before.get(key, {}), after.get(key, {})
        for name in sorted(set(a) | set(b)):
            if a.get(name) != b.get(name):
                out.append(f"{Path(key).name}/{name}" if name != "." else Path(key).name)
    return out


def real_watch_paths() -> list[Path]:
    return list(dict.fromkeys(protected_paths().values()))


# ------------------------------------------------------------------------------------------------ running things
class Runner:
    """The one place a process is started. With dry=True it only prints."""

    def __init__(self, dry: bool, say: Callable[[str], None]):
        self.dry, self.say = dry, say

    @staticmethod
    def child_env(extra: dict[str, str]) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in SCRUB and not k.startswith(SCRUB_PREFIXES)}
        env.update(extra)
        return env

    @staticmethod
    def command_text(argv: list[str], extra: dict[str, str], cwd: Path | None = None) -> str:
        pairs = " ".join(f"{k}={shlex.quote(v)}" for k, v in extra.items())
        line = (pairs + " " if pairs else "") + shlex.join(argv)
        return f"(cd {shlex.quote(str(cwd))} && {line})" if cwd else line

    def _guard(self, extra: dict[str, str]) -> None:
        for var in PIN_VARS:
            if var in extra:
                refuse_real(var, extra[var])

    def run(self, argv: list[str], extra: dict[str, str], cwd: Path | None = None, timeout: int = TURN_TIMEOUT) -> dict | None:
        self._guard(extra)
        self.say("  $ " + self.command_text(argv, extra, cwd))
        if self.dry:
            return None
        try:
            r = subprocess.run(argv, env=self.child_env(extra), cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
            return {"rc": r.returncode, "stdout": r.stdout, "stderr": r.stderr, "timeout": False}
        except subprocess.TimeoutExpired:
            return {"rc": None, "stdout": "", "stderr": f"timed out after {timeout} s", "timeout": True}
        except OSError as e:
            return {"rc": None, "stdout": "", "stderr": str(e), "timeout": False}

    def rpc(self, argv: list[str], extra: dict[str, str], messages: list[dict], want_id: int, timeout: int = 30) -> dict | None:
        """Line-delimited JSON over a CLI's stdio (Codex's app-server). Returns the response with id `want_id`, or {"error": ...}."""
        self._guard(extra)
        self.say("  $ " + self.command_text(argv, extra) + "    # JSON-RPC over stdio: " + ", ".join(m.get("method", "?") for m in messages))
        if self.dry:
            return None
        try:
            p = subprocess.Popen(argv, env=self.child_env(extra), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError as e:
            return {"error": str(e)}
        try:
            for m in messages:
                p.stdin.write(json.dumps(m) + "\n")
            p.stdin.flush()
            sel = selectors.DefaultSelector()
            sel.register(p.stdout, selectors.EVENT_READ)
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if not sel.select(timeout=1.0):
                    continue
                line = p.stdout.readline()
                if not line:
                    return {"error": "the process closed its output before answering"}
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == want_id:
                    return msg
            return {"error": f"no answer within {timeout} s"}
        finally:
            p.kill()


# ------------------------------------------------------------------------------------------------ parsing what the CLIs print
def parse_json(text: str):
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        data = None
        for line in reversed(text.splitlines()):
            try:
                data = json.loads(line)
                break
            except ValueError:
                continue
    if isinstance(data, list):
        results = [d for d in data if isinstance(d, dict) and d.get("type") == "result"]
        data = results[-1] if results else (data[-1] if data else None)
    return data if isinstance(data, dict) else None


def find_key(obj, names: tuple[str, ...]):
    """The first value stored under any of `names`, searching nested dicts and lists."""
    if isinstance(obj, dict):
        for n in names:
            if isinstance(obj.get(n), str):
                return obj[n]
        for v in obj.values():
            hit = find_key(v, names)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = find_key(v, names)
            if hit:
                return hit
    return None


def jsonl_thread_id(stdout: str) -> str | None:
    for line in (stdout or "").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        hit = find_key(obj, ("thread_id", "threadId", "session_id", "conversation_id"))
        if hit and UUID.fullmatch(hit):
            return hit
    return None


def newest_rollout_id(sessions: Path) -> str | None:
    """File names only: the thread id in the newest rollout-<time>-<id>.jsonl under the shared sessions directory."""
    best = None
    for path in sessions.rglob("rollout-*.jsonl"):
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", path.name)
        if m and (best is None or path.stat().st_mtime > best[0]):
            best = (path.stat().st_mtime, m.group(1))
    return best[1] if best else None


def transcript_present(root: Path, pattern: str) -> bool:
    return any(True for _ in root.glob(pattern)) if root.is_dir() else False


def int_fields(obj, names: tuple[str, ...]) -> dict[str, int]:
    return {n: obj[n] for n in names if isinstance(obj, dict) and isinstance(obj.get(n), int) and not isinstance(obj.get(n), bool)}


def numbers_only(obj, keep_text: tuple[str, ...] = ("planType", "plan_type", "limitId", "limit_id", "limitName", "limit_name")):
    """A usage answer reduced to its numbers and plan labels, so nothing identifying is stored."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                out[k] = numbers_only(v, keep_text)
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = v
            elif isinstance(v, str) and k in keep_text:
                out[k] = scrub(v)
        return out
    if isinstance(obj, list):
        return [numbers_only(v, keep_text) for v in obj]
    return None


# ------------------------------------------------------------------------------------------------ the flows
class Spike:
    def __init__(self, args, layout: dict[str, Path], runner: Runner, say, ask):
        self.args, self.layout, self.r, self.say, self.ask = args, layout, runner, say, ask
        self.dry = args.dry_run
        self.paths = {str(layout["root"]): "<scratch>"}
        self.results: dict = {"schema": 1, "tool": "scripts/dev/pools_spike.py", "dry_run": self.dry,
                              "started_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                              "platform": {"system": platform.system(), "machine": platform.machine()}, "versions": {}}

    # -- helpers
    def clean(self, text: str, limit: int = 300) -> str:
        return scrub((text or "").strip(), self.paths)[:limit]

    def wait_login(self, label: str, command: str) -> None:
        self.say(f"\n  Log in {label}. In ANOTHER terminal (with ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, OPENAI_API_KEY and CODEX_* unset), run:\n")
        self.say(f"      {command}\n")
        if self.dry:
            self.say("  (dry run: not waiting)")
            return
        self.ask(f"  Press Enter here once {label} is logged in... ")

    def version(self, tool: str) -> str | None:
        res = self.r.run([tool, "--version"], {})
        if res and res["rc"] == 0:
            return self.clean(res["stdout"].splitlines()[0] if res["stdout"] else "", 80)
        return None

    def identity(self, raw: str, parsed: dict | None) -> tuple[str | None, dict]:
        """(identity used only to compare A with B in memory, what may be shown)."""
        shown: dict = {}
        emails = EMAIL.findall(raw or "")
        ident_parts = []
        if parsed:
            for key in ("email",):
                if isinstance(parsed.get(key), str):
                    ident_parts.append(parsed[key])
                    shown["account"] = mask_email(parsed[key])
            if isinstance(parsed.get("orgName"), str):
                ident_parts.append(parsed["orgName"])
                shown["org"] = mask_word(parsed["orgName"])
            for key in ("loggedIn", "authMethod", "apiProvider", "subscriptionType"):
                if isinstance(parsed.get(key), (str, bool)):
                    shown[key] = parsed[key]
        elif emails:
            ident_parts.append(emails[0])
            shown["account"] = mask_email(emails[0])
        return ("|".join(ident_parts) or None), shown

    def compare(self, a: str | None, b: str | None) -> bool | None:
        return None if not a or not b else a != b

    # -- Claude
    def claude(self) -> dict:
        res: dict = {"status": "dry-run" if self.dry else "ok"}
        word = f"{WORDS[uuid.uuid4().int % len(WORDS)]}-{uuid.uuid4().int % 900 + 100}"
        sid = str(uuid.uuid4())
        work = self.layout["work"]
        res["session"] = short_id(sid)
        self.say("\n== Claude Code: two profiles, one shared projects/ ==")
        self.say(f"  A: CLAUDE_CONFIG_DIR={self.layout['claude_A']}")
        self.say(f"  B: CLAUDE_CONFIG_DIR={self.layout['claude_B']}")
        res["login"], idents = {}, {}
        for side in "AB":
            env = claude_env(self.layout, side)
            self.wait_login(f"Claude profile {side}", self.r.command_text(["claude", "auth", "login"], env))
            st = self.r.run(["claude", "auth", "status"], env)
            parsed = parse_json(st["stdout"]) if st else None
            ident, shown = self.identity(st["stdout"] if st else "", parsed)
            idents[side] = ident
            shown["credentials_file_exists"] = None if self.dry else os.path.exists(self.layout[f"claude_{side}"] / ".credentials.json")
            res["login"][side] = shown
            if st and not (parsed and parsed.get("loggedIn") is True):
                res["status"] = "failed: profile %s is not logged in" % side
                self.say(f"  ! Claude profile {side} reports no login; skipping the Claude run.")
                return res
        res["accounts_differ"] = self.compare(idents.get("A"), idents.get("B"))
        turns = [
            ("A", ["--session-id", sid], f"Remember this secret word for later: {word}. Reply with just OK."),
            ("A", ["--resume", sid], "What is 2 plus 2? Reply with just the number."),
            ("A", ["--resume", sid], "Say hello in one word."),
            ("B", ["--resume", sid], "What was the word I gave you in turn 1? Reply with just the word."),
        ]
        res["turns"] = []
        answer_b = ""
        for n, (side, flag, prompt) in enumerate(turns, 1):
            argv = ["claude", "-p", prompt, *flag, "--output-format", "json", "--tools", ""]
            out = self.r.run(argv, claude_env(self.layout, side), cwd=work)
            rec: dict = {"turn": n, "profile": side}
            if out:
                j = parse_json(out["stdout"])
                rec.update(rc=out["rc"], is_error=bool(j.get("is_error")) if j else None)
                if j:
                    rec["session_matches"] = j.get("session_id") == sid
                    rec["usage"] = int_fields(j.get("usage"), ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                    if isinstance(j.get("total_cost_usd"), (int, float)):
                        rec["cost_usd"] = j["total_cost_usd"]
                    if n == 4:
                        answer_b = str(j.get("result") or "")
                if out["rc"] != 0 or not j or j.get("is_error"):
                    rec["error"] = self.clean((j or {}).get("result") if j else (out["stderr"] or out["stdout"]))
            res["turns"].append(rec)
            if rec.get("error") and n <= 3:
                res["status"] = f"failed: turn {n} in A"
                break
        if not self.dry and res["status"] == "ok":
            res["context_present"] = word.lower() in answer_b.lower()
            res["answer_b"] = self.clean(answer_b, 120)
            res["shared_transcript_present"] = transcript_present(self.layout["claude_shared"], f"*/{sid}.jsonl")
            res["real_home_transcript_present"] = transcript_present(Path(os.path.expanduser("~")) / ".claude" / "projects", f"*/{sid}.jsonl")
            st = self.r.run(["claude", "auth", "status"], claude_env(self.layout, "B"))
            parsed = parse_json(st["stdout"]) if st else None
            res["account_b_after_resume"] = self.identity(st["stdout"] if st else "", parsed)[1]
        elif self.dry:
            self.say(f"  (the recall word is generated per run; turn 4 passes if B's answer contains it)")
        # usage, through the CLI only
        self.say("\n  Usage through the CLI's own command (print mode may answer nothing; that is a finding):")
        u = self.r.run(["claude", "-p", "/usage", "--output-format", "json", "--tools", ""], claude_env(self.layout, "B"), cwd=work)
        if u:
            j = parse_json(u["stdout"])
            res["usage_print_mode"] = {"rc": u["rc"], "text": self.clean(str((j or {}).get("result") or u["stdout"] or u["stderr"]), 400)}
        self.say("  By hand, in profile B: run `claude`, then /status and /usage, and type what they show below.")
        res["usage_by_hand"] = self.observe("what /status (account) and /usage (5h %, weekly %) show in Claude B")
        res["keychain"] = self.keychain("claude")
        return res

    # -- Codex
    def codex(self) -> dict:
        res: dict = {"status": "dry-run" if self.dry else "ok", "sqlite": self.args.codex_sqlite}
        word = f"{WORDS[uuid.uuid4().int % len(WORDS)]}-{uuid.uuid4().int % 900 + 100}"
        work, mode = self.layout["work"], self.args.codex_sqlite
        self.say("\n== Codex: two profiles, shared sessions/ and %s SQLite home ==" % mode)
        self.say(f"  A: CODEX_HOME={self.layout['codex_A']}")
        self.say(f"  B: CODEX_HOME={self.layout['codex_B']}")
        res["login"], idents = {}, {}
        for side in "AB":
            env = codex_env(self.layout, side, mode)
            self.wait_login(f"Codex profile {side}", self.r.command_text(["codex", "login", "--device-auth"], {"CODEX_HOME": env["CODEX_HOME"]}))
            st = self.r.run(["codex", "login", "status"], env)
            ident, shown = self.identity(((st["stdout"] + "\n" + st["stderr"]) if st else ""), None)
            idents[side] = ident
            first = ((st["stdout"] or st["stderr"]).strip().splitlines() or [""])[0] if st else ""
            shown["status_line"] = self.clean(first, 120)
            shown["credentials_file_exists"] = None if self.dry else os.path.exists(self.layout[f"codex_{side}"] / "auth.json")
            res["login"][side] = shown
            if st and st["rc"] != 0:
                res["status"] = "failed: profile %s is not logged in" % side
                self.say(f"  ! Codex profile {side} reports no login; skipping the Codex run.")
                return res
        res["accounts_differ"] = self.compare(idents.get("A"), idents.get("B"))
        common = ["--skip-git-repo-check", "--json", "-c", 'sandbox_mode="read-only"']
        tid = None
        res["turns"] = []
        answer_b = ""
        prompts = [f"Remember this secret word for later: {word}. Reply with just OK.", "What is 2 plus 2? Reply with just the number.",
                   "Say hello in one word.", "What was the word I gave you in turn 1? Reply with just the word."]
        for n, prompt in enumerate(prompts, 1):
            side = "B" if n == 4 else "A"
            last = self.layout["results"] / f"codex-turn{n}.txt"
            argv = (["codex", "exec", *common, "-o", str(last), prompt] if n == 1
                    else ["codex", "exec", "resume", *common, "-o", str(last), tid or "<thread id from turn 1>", prompt])
            out = self.r.run(argv, codex_env(self.layout, side, mode), cwd=work)
            rec: dict = {"turn": n, "profile": side}
            if out:
                rec["rc"] = out["rc"]
                if n == 1:
                    tid = jsonl_thread_id(out["stdout"]) or newest_rollout_id(self.layout["codex_shared"])
                    res["thread"] = short_id(tid)
                    if not tid:
                        res["status"] = "failed: no thread id after turn 1"
                if n == 4:
                    answer_b = last.read_text(encoding="utf-8", errors="replace") if last.exists() else out["stdout"]
                if out["rc"] != 0:
                    rec["error"] = self.clean(out["stderr"] or out["stdout"][-300:])
            res["turns"].append(rec)
            if (rec.get("error") and n <= 3) or res["status"].startswith("failed"):
                res["status"] = res["status"] if res["status"].startswith("failed") else f"failed: turn {n} in A"
                break
        if not self.dry and res["status"] == "ok":
            res["context_present"] = word.lower() in answer_b.lower()
            res["answer_b"] = self.clean(answer_b, 120)
            res["shared_transcript_present"] = transcript_present(self.layout["codex_shared"], f"**/rollout-*-{tid}.jsonl")
            res["real_home_transcript_present"] = transcript_present(Path(os.path.expanduser("~")) / ".codex" / "sessions", f"**/rollout-*-{tid}.jsonl")
            st = self.r.run(["codex", "login", "status"], codex_env(self.layout, "B", mode))
            res["account_b_after_resume"] = self.identity((st["stdout"] + st["stderr"]) if st else "", None)[1]
        self.say("\n  Window usage outside the TUI: the app-server method account/rateLimits/read (the CLI makes the call, this script does not):")
        msgs = [{"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "pools_spike", "title": "pools spike", "version": "0"}}},
                {"method": "initialized"}, {"id": 2, "method": "account/rateLimits/read"}]
        rpc = self.r.rpc(["codex", "app-server"], codex_env(self.layout, "B", mode), msgs, want_id=2)
        if rpc is not None:
            res["usage_app_server"] = ({"error": self.clean(str(rpc.get("error")))} if "error" in rpc and "result" not in rpc
                                       else {"result": numbers_only(rpc.get("result"))})
        self.say("  By hand, in profile B: run `codex`, then /status, and type what it shows below.")
        res["usage_by_hand"] = self.observe("what Codex /status shows (account line, 5h %, weekly %)")
        res["keychain"] = self.keychain("codex")
        return res

    def observe(self, what: str) -> str | None:
        if self.dry:
            return None
        try:
            text = self.ask(f"  Type {what}, or press Enter to skip: ")
        except EOFError:
            return None
        return self.clean(text, 300) or None

    def keychain(self, which: str) -> dict:
        names = keychain_names(self.layout)[which]
        if which == "claude":
            cmds = {s: f'security find-generic-password -s "{names[s]}"' for s in "AB"}
            hint = "attributes only, no -w, so no secret is shown"
            expect = "an item per profile named Claude Code-credentials-<8 hex> (the hash of the profile path)"
        else:
            cmds = {s: f'security find-generic-password -s "Codex Auth" -a "{names[s]}"' for s in "AB"}
            hint = "attributes only, no -w, so no secret is shown"
            expect = "NO item (the file credential store is pinned in each config.toml)"
        self.say(f"\n  macOS Keychain ({hint}). Expected: {expect}. Run in any terminal; this script never queries the Keychain:")
        for s in "AB":
            self.say(f"      {cmds[s]}")
        if self.dry or platform.system() != "Darwin":
            return {"asked": False}
        answer = {}
        for s in "AB":
            answer[s] = self.clean(self.ask(f"  Item found for {s}? (y/n/skip): "), 10) or "skip"
        return {"asked": True, "found": answer}


# ------------------------------------------------------------------------------------------------ reports
def yes_no(value) -> str:
    return "unknown" if value is None else ("yes" if value else "no")


def provider_lines(name: str, r: dict) -> list[str]:
    if not r:
        return [f"- {name}: not run"]
    lines = [f"### {name}", "", f"- status: {r.get('status')}"]
    if r.get("sqlite"):
        lines.append(f"- SQLite home: {r['sqlite']}")
    for side in "AB":
        l = r.get("login", {}).get(side)
        if l:
            lines.append(f"- login {side}: " + ", ".join(f"{k}={v}" for k, v in l.items()))
    if "accounts_differ" in r:
        lines.append(f"- A and B are different accounts: {yes_no(r['accounts_differ'])}")
    if "context_present" in r:
        lines += [f"- B answered with the word from turn 1 (context present): {yes_no(r['context_present'])}",
                  f"- B's answer, scrubbed: {r.get('answer_b')!r}",
                  f"- transcript landed in the shared directory: {yes_no(r.get('shared_transcript_present'))}",
                  f"- the session's transcript also exists in the real home: {yes_no(r.get('real_home_transcript_present'))}",
                  f"- account B reports after the resume: " + ", ".join(f"{k}={v}" for k, v in (r.get("account_b_after_resume") or {}).items())]
    for t in r.get("turns", []):
        extra = ", ".join(f"{k}={v}" for k, v in t.items() if k not in ("turn", "profile") and v not in (None, {}))
        lines.append(f"- turn {t['turn']} in {t['profile']}: {extra}")
    for key, label in (("usage_print_mode", "usage in print mode"), ("usage_app_server", "usage through the app-server"), ("usage_by_hand", "usage by hand")):
        if r.get(key) is not None:
            lines.append(f"- {label}: {json.dumps(r[key]) if not isinstance(r[key], str) else r[key]}")
    if r.get("keychain", {}).get("asked"):
        lines.append(f"- Keychain item found (A/B): {r['keychain']['found']}")
    return lines + [""]


def summary_md(res: dict) -> str:
    head = ["# Pools P0 spike results", "", f"- run (UTC): {res['started_utc']}" + ("  (DRY RUN: no CLI was started)" if res["dry_run"] else ""),
            f"- platform: {res['platform']['system']} {res['platform']['machine']}",
            f"- versions: " + (", ".join(f"{k} {v}" for k, v in res["versions"].items()) or "not recorded"),
            f"- real home entries that changed during the run: " + (", ".join(res.get("real_home_changed", [])) or "none")
            + " (another running session of yours can change them; look at the names)", ""]
    return "\n".join(head + provider_lines("Claude Code", res.get("claude")) + provider_lines("Codex", res.get("codex")))


def issue_comment(res: dict) -> str:
    body = ["Results of `scripts/dev/pools_spike.py` (pools P0, #219)" + (" - DRY RUN, nothing was run" if res["dry_run"] else ""), "",
            "Platform: " + "; ".join([f"{res['platform']['system']} {res['platform']['machine']}"] + [f"{k} {v}" for k, v in res["versions"].items()]), ""]
    for name, key in (("Claude Code", "claude"), ("Codex", "codex")):
        r = res.get(key)
        if not r:
            continue
        body += [f"**{name}** ({r.get('status')})"]
        if "context_present" in r:
            body += [f"- resume under B kept the context: {yes_no(r['context_present'])}",
                     f"- A and B were different accounts: {yes_no(r.get('accounts_differ'))}",
                     f"- transcript in the shared dir: {yes_no(r.get('shared_transcript_present'))}; transcript in the real home: {yes_no(r.get('real_home_transcript_present'))}",
                     "- B reports: " + ", ".join(f"{k}={v}" for k, v in (r.get("account_b_after_resume") or {}).items())]
        for key2, label in (("usage_print_mode", "usage in print mode"), ("usage_app_server", "usage via app-server"), ("usage_by_hand", "usage by hand")):
            if r.get(key2) is not None:
                body.append(f"- {label}: {json.dumps(r[key2]) if not isinstance(r[key2], str) else r[key2]}")
        if r.get("keychain", {}).get("asked"):
            body.append(f"- Keychain item (A/B): {r['keychain']['found']}")
        body.append("")
    body.append("Real home entries changed during the run: " + (", ".join(res.get("real_home_changed", [])) or "none"))
    return "\n".join(body)


# ------------------------------------------------------------------------------------------------ main
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Pools P0 spike: resume a session under a second login (issue #219).")
    ap.add_argument("--dry-run", action="store_true", help="print every command, build the scratch tree, run no CLI")
    ap.add_argument("--only", choices=("claude", "codex", "both"), default="both")
    ap.add_argument("--base", help="directory to create the scratch tree in (default: the system temp dir)")
    ap.add_argument("--codex-sqlite", choices=("shared", "separate"), default="shared",
                    help="one CODEX_SQLITE_HOME for both Codex profiles (default), or one per profile to test the empty-database case")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None, say: Callable[[str], None] | None = None, ask: Callable[[str], str] | None = None) -> int:
    args = parse_args(argv)
    say = say or (lambda s: print(s, flush=True))
    ask = ask or input
    base = args.base or tempfile.gettempdir()
    try:
        refuse_real("--base", base)
        root = Path(tempfile.mkdtemp(prefix="ccboard-pools-spike-", dir=base))
        layout = build_layout(root, args.codex_sqlite)
    except Refused as e:
        say(str(e))
        return 2
    before = snapshot(real_watch_paths())
    say("Pools P0 spike" + (" (DRY RUN: nothing below is executed)" if args.dry_run else ""))
    say(f"Scratch tree (holds real logins until you delete it): {layout['root']}")
    leftovers = sorted(v for v in SCRUB if os.environ.get(v)) + sorted(k for k in os.environ if k.startswith(SCRUB_PREFIXES))
    if leftovers:
        say("Set in this shell, removed for every command the script runs (unset them in the login terminals too): " + ", ".join(leftovers))
    runner = Runner(args.dry_run, say)
    spike = Spike(args, layout, runner, say, ask)
    missing = [t for t in (("claude",) if args.only != "codex" else ()) + (("codex",) if args.only != "claude" else ()) if not shutil.which(t)]
    if missing and not args.dry_run:
        say("Not on PATH: " + ", ".join(missing))
        return 2
    for tool in ("claude", "codex"):
        if args.only in ("both", tool) and not args.dry_run:
            spike.results["versions"][tool] = spike.version(tool) or "unknown"
    if args.only in ("both", "claude"):
        spike.results["claude"] = spike.claude()
    if args.only in ("both", "codex"):
        spike.results["codex"] = spike.codex()
    spike.results["real_home_changed"] = changed(before, snapshot(real_watch_paths()))
    spike.results["real_home_note"] = "stat only, one level deep; a session of yours running at the same time also changes these"
    out = layout["results"]
    summary, comment = summary_md(spike.results), issue_comment(spike.results)
    (out / "results.json").write_text(json.dumps(spike.results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "summary.md").write_text(scrub(summary, spike.paths) + "\n", encoding="utf-8")
    (out / "issue-comment.md").write_text(scrub(comment, spike.paths) + "\n", encoding="utf-8")
    say(f"\nResults: {out / 'results.json'}\nSummary: {out / 'summary.md'}\nIssue comment: {out / 'issue-comment.md'}")
    say("\n----- paste into issue #219 (results only) -----\n" + scrub(comment, spike.paths) + "\n----- end -----")
    say("\nWhen you are done, remove the scratch tree (it holds the two logins' files):\n    rm -rf " + shlex.quote(str(layout["root"]))
        + "\nOptionally sign the scratch profiles out first: `claude auth logout` / `codex logout` with the same variables as the login commands.")
    return 1 if any(str((spike.results.get(k) or {}).get("status", "")).startswith("failed") for k in ("claude", "codex")) else 0


if __name__ == "__main__":
    sys.exit(main())
