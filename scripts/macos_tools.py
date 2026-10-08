#!/usr/bin/env python3
"""Small helpers for the macOS installer's tools step (issue #128). Standard library only (the system python3 of a Mac, 3.9 or newer; no jq).

    macos_tools.py bottle --formula ttyd --macos 14.7.3 --arch x86_64 [--file info.json] [--tag sonoma] [--explain]
        Reads `brew info --json=v2 <formula>` (stdin, or --file) and says whether Homebrew has a bottle for this macOS and processor:
        prints "bottle available ...", "builds from source ..." or "could not tell ...". Exit 0, 10 or 20 in that order. --explain adds the
        "takes several minutes" line and, on Intel, the Tier 3 sentence. It never claims a bottle it could not find.
    macos_tools.py ttyd (--bin PATH | --version-text FILE --help-text FILE)
        Acceptance for ttyd: version 1.7.4 or newer and -W, -O and -a in its --help. Exit 0, or 1 with one line per problem naming the flag.
    macos_tools.py code-server-config --port N
        The managed code-server config.yaml, byte for byte what install.sh writes on Linux (tests pin the two equal).
    macos_tools.py optional-bundle [--pick code-server,restic,bun]
        The Brewfile.optional lines (and the tap they need) for the picked tools, for `brew bundle --file=-`. Without --pick, every one.

    macos_tools.py backup-schedule "<spec>"
        The nightly backup's time on a Mac, from CCBOARD_BACKUP_ONCALENDAR: `*-*-* HH:MM[:SS]` or `HH:MM` (daily). Prints "HOUR MINUTE" (whole numbers,
        no leading zero) and exits 0; anything else exits 2 with one line on stderr naming the accepted forms. The installer asks this before it changes anything.

Everything here only reads; nothing installs.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MACOS_DIR = HERE / "macos"
TTYD_MIN = (1, 7, 4)
TTYD_FLAGS = (("-W", "writable terminal"), ("-O", "check origin"), ("-a", "client arguments in the URL"))
# Homebrew's tag names, oldest first. A macOS major newer than the last one cannot be named here: pass --tag, or the answer is "could not tell".
MACOS_NAMES = {11: "big_sur", 12: "monterey", 13: "ventura", 14: "sonoma", 15: "sequoia", 26: "tahoe"}
ORDER = list(MACOS_NAMES.values())
RELOCATABLE = {":any", ":any_skip_relocation", "any", "any_skip_relocation"}
INTEL_NOTE = ("Homebrew supports Intel Macs only at Tier 3 (problems may be closed without investigation) and not at all from about "
              "September 2027: a date to plan around, not a reason to stop.")
SEVERAL_MINUTES = "Building takes several minutes."
EXIT_BOTTLE, EXIT_SOURCE, EXIT_UNKNOWN = 0, 10, 20


# ---------------------------------------------------------------- bottles


def bottle_tag(macos_version: str, arch: str) -> str | None:
    """Homebrew's bottle tag for this macOS and processor ('sonoma' on Intel, 'arm64_sonoma' on Apple silicon), or None for a macOS major
    this table does not know."""
    try:
        major = int(str(macos_version).split(".")[0])
    except ValueError:
        return None
    name = MACOS_NAMES.get(major)
    if name is None:
        return None
    if arch in ("arm64", "aarch64"):
        return f"arm64_{name}"
    if arch in ("x86_64", "i386"):
        return name
    return None


def bottle_status(info: object, formula: str | None, macos_version: str, arch: str, tag: str | None = None) -> tuple[str, str]:
    """(status, detail) from the parsed `brew info --json=v2` body: status is 'bottle', 'source' or 'unknown'.
    A bottle counts when the formula has one for this tag or for 'all'; an older macOS bottle counts only when it is relocatable
    (cellar :any or :any_skip_relocation), which is how Homebrew falls back (to verify on a Mac)."""
    formulae = info.get("formulae") if isinstance(info, dict) else None
    if not isinstance(formulae, list) or not formulae:
        return "unknown", "brew info returned no formula"
    pick = None
    for f in formulae:
        if isinstance(f, dict) and (formula is None or formula in (f.get("name"), f.get("full_name"))):
            pick = f
            break
    if pick is None:
        return "unknown", f"brew info has no formula named {formula}"
    name = pick.get("name") or formula or "the formula"
    want = tag or bottle_tag(macos_version, arch)
    if want is None:
        return "unknown", f"cannot name the bottle tag for macOS {macos_version} on {arch}"
    stable = (pick.get("bottle") or {}).get("stable") if isinstance(pick.get("bottle"), dict) else None
    files = stable.get("files") if isinstance(stable, dict) else None
    if not isinstance(files, dict) or not files:
        return "source", f"{name} has no bottles"
    if want in files:
        return "bottle", f"{name} has a bottle for {want}"
    if "all" in files:
        return "bottle", f"{name} has a bottle for every system"
    arm = want.startswith("arm64_")
    base = want[len("arm64_"):] if arm else want
    if base in ORDER:
        for older in reversed(ORDER[:ORDER.index(base)]):
            key = f"arm64_{older}" if arm else older
            if key in files and isinstance(files[key], dict) and files[key].get("cellar") in RELOCATABLE:
                return "bottle", f"{name} has a bottle for {key}, which an older-macOS fallback can use (to verify)"
    return "source", f"{name} has no bottle for {want}"


# ---------------------------------------------------------------- ttyd


def check_ttyd(version_text: str, help_text: str) -> list[str]:
    """The problems with this ttyd (empty list: accepted): a version below 1.7.4, or a flag missing from --help (1.6.3 lacks -W)."""
    problems: list[str] = []
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", version_text or "")
    want = ".".join(str(n) for n in TTYD_MIN)
    if not m:
        problems.append(f"could not read ttyd's version from `ttyd --version` (ccboard needs {want} or newer)")
    elif tuple(int(g) for g in m.groups()) < TTYD_MIN:
        problems.append(f"ttyd {m.group(0)} is older than {want}, which ccboard needs")
    for flag, why in TTYD_FLAGS:
        if not re.search(rf"(?m)^\s*{re.escape(flag)}(?=[,\s]|$)", help_text or ""):
            problems.append(f"this ttyd does not list {flag} ({why}) in `ttyd --help`; ccboard's terminal needs {flag}")
    return problems


def _run_text(argv: list[str]) -> str:
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SystemExit(f"macos_tools: could not run {' '.join(argv)}: {e}") from None
    return (cp.stdout or "") + (cp.stderr or "")


# ---------------------------------------------------------------- code-server and Brewfiles


def code_server_config(port: int) -> str:
    """The managed config.yaml for code-server on `port`: the template under scripts/macos, whose bytes equal the Linux installer's."""
    if not 1 <= int(port) <= 65535:
        raise ValueError(f"{port} is not a port number")
    return (MACOS_DIR / "code-server-config.yaml.in").read_text(encoding="utf-8").replace("__CODE_SERVER_PORT__", str(int(port)))


BREW_RE = re.compile(r'^brew "([^"]+)"', re.M)
TAP_RE = re.compile(r'^tap "([^"]+)"', re.M)


def brewfile_names(text: str) -> tuple[list[str], list[str]]:
    """(formula names, tap names) of a Brewfile, in file order."""
    return BREW_RE.findall(text), TAP_RE.findall(text)


def short_name(formula: str) -> str:
    """'oven-sh/bun/bun' -> 'bun': the name CCBOARD_MACOS_OPTIONAL uses."""
    return formula.rsplit("/", 1)[-1]


def optional_bundle(pick: list[str] | None, text: str | None = None) -> str:
    """Brewfile text for the picked optional tools (all when `pick` is None), each tap before its formula."""
    text = text if text is not None else (MACOS_DIR / "Brewfile.optional").read_text(encoding="utf-8")
    formulae, _ = brewfile_names(text)
    known = [short_name(f) for f in formulae]
    chosen = known if pick is None else pick
    unknown = [p for p in chosen if p not in known]
    if unknown:
        raise ValueError("unknown optional tool " + ", ".join(unknown) + " (known: " + ", ".join(known) + ")")
    out: list[str] = []
    taps: list[str] = []                    # tap lines seen since the last formula: they belong to the formula that follows
    for line in text.splitlines():
        m = BREW_RE.match(line)
        if TAP_RE.match(line):
            taps.append(line)
        elif m:
            if short_name(m.group(1)) in chosen:
                out += taps + [line]
            taps = []
    return "\n".join(out) + ("\n" if out else "")


# ---------------------------------------------------------------- backup schedule

BACKUP_SCHEDULE_DEFAULT = "*-*-* 02:30:00"
BACKUP_FORMS = "*-*-* HH:MM[:SS] or HH:MM (every day)"
_CLOCK_RE = re.compile(r"(?:\*-\*-\*\s+)?(\d{1,2}):(\d{2})(?::(\d{2}))?")


def backup_schedule(spec: str) -> tuple[int, int]:
    """(hour, minute) of a daily CCBOARD_BACKUP_ONCALENDAR spec: `*-*-* HH:MM[:SS]` or `HH:MM`. launchd has no systemd calendar language, so
    nothing else is understood (`Mon *-*-* 02:30`, `hourly`, `*:0/15`, empty): ValueError names the accepted forms. Seconds are accepted and
    dropped (a calendar job fires on the minute); hour 0 to 23, minute 0 to 59, seconds 0 to 59."""
    text = (spec or "").strip()
    m = _CLOCK_RE.fullmatch(text)
    if not m or not text.isascii():
        raise ValueError(f"CCBOARD_BACKUP_ONCALENDAR={text!r} is not supported on macOS: use {BACKUP_FORMS}")
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59 or (m.group(3) is not None and int(m.group(3)) > 59):
        raise ValueError(f"CCBOARD_BACKUP_ONCALENDAR={text!r} is not a time of day: use {BACKUP_FORMS} with hour 0 to 23 and minute 0 to 59")
    return hour, minute


# ---------------------------------------------------------------- command line


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Helpers for the macOS installer's tools step.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bottle")
    b.add_argument("--formula")
    b.add_argument("--macos", required=True, help="sw_vers -productVersion")
    b.add_argument("--arch", required=True, help="uname -m")
    b.add_argument("--file", help="the brew info --json=v2 output (default: stdin)")
    b.add_argument("--tag", help="the bottle tag, when the macOS version is newer than this table")
    b.add_argument("--explain", action="store_true")
    t = sub.add_parser("ttyd")
    t.add_argument("--bin")
    t.add_argument("--version-text")
    t.add_argument("--help-text")
    c = sub.add_parser("code-server-config")
    c.add_argument("--port", required=True, type=int)
    o = sub.add_parser("optional-bundle")
    o.add_argument("--pick", help="comma list, e.g. code-server,restic,bun")
    k = sub.add_parser("backup-schedule")
    k.add_argument("spec", help="the CCBOARD_BACKUP_ONCALENDAR value")
    a = ap.parse_args(argv)

    if a.cmd == "backup-schedule":
        try:
            hour, minute = backup_schedule(a.spec)
        except ValueError as e:
            print(f"macos_tools: {e}", file=sys.stderr)
            return 2
        print(f"{hour} {minute}")
        return 0
    if a.cmd == "bottle":
        try:
            raw = Path(a.file).read_text(encoding="utf-8") if a.file else sys.stdin.read()
            info = json.loads(raw)
            status, detail = bottle_status(info, a.formula, a.macos, a.arch, a.tag)
        except (OSError, ValueError) as e:
            status, detail = "unknown", f"could not read brew info: {e}"
        word = {"bottle": "bottle available", "source": "builds from source", "unknown": "could not tell whether a bottle exists"}[status]
        print(f"{word} ({detail})")
        if a.explain and status == "source":
            print(SEVERAL_MINUTES)
            if a.arch in ("x86_64", "i386"):
                print(INTEL_NOTE)
        if a.explain and status == "unknown":
            print("Assume it builds from source and takes several minutes.")
        return {"bottle": EXIT_BOTTLE, "source": EXIT_SOURCE, "unknown": EXIT_UNKNOWN}[status]
    if a.cmd == "ttyd":
        if a.bin:
            ver, hlp = _run_text([a.bin, "--version"]), _run_text([a.bin, "--help"])
        elif a.version_text and a.help_text:
            ver, hlp = Path(a.version_text).read_text(encoding="utf-8"), Path(a.help_text).read_text(encoding="utf-8")
        else:
            print("macos_tools: ttyd wants --bin PATH, or --version-text and --help-text", file=sys.stderr)
            return 2
        problems = check_ttyd(ver, hlp)
        for p in problems:
            print(f"ttyd: {p}", file=sys.stderr)
        return 1 if problems else 0
    if a.cmd == "code-server-config":
        try:
            sys.stdout.write(code_server_config(a.port))
        except ValueError as e:
            print(f"macos_tools: {e}", file=sys.stderr)
            return 2
        return 0
    if a.cmd == "optional-bundle":
        pick = [p.strip() for p in a.pick.split(",") if p.strip()] if a.pick is not None else None
        try:
            sys.stdout.write(optional_bundle(pick))
        except ValueError as e:
            print(f"macos_tools: {e}", file=sys.stderr)
            return 2
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
