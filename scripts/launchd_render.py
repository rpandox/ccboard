#!/usr/bin/env python3
"""Render one launchd job template (launchd/dev.ccboard.<job>.plist.in) into a plist the installer can bootstrap (issue #117).

Standard library only, so it runs under the system python3 of a Mac (3.9 or newer) before the board's venv exists. The templates use
`__NAME__` tokens like the systemd units, but every value is XML-escaped (`&`, `<`, `>`), so a home directory such as `/data/my & co`
survives; the result is parsed with plistlib before anything is written, and a template with a token that has no value is an error, never
a plist with a literal `__NAME__` in it.

    python3 scripts/launchd_render.py --template launchd/dev.ccboard.board.plist.in --out <file> --set HOME=<home> --set APP_DIR=<app dir> ...

`--out -` prints to stdout; `--list-tokens` prints the tokens the template uses, one per line; `--session-type Background` adds
LimitLoadToSessionType (the experimental CCBOARD_LAUNCHD_DOMAIN=user layout). Exit 0 on success, 2 with one `launchd_render: <reason>` line
on stderr when a value is missing or refused. Importable: render(template_text, values) and render_file(template, out, values).

Tokens (a value given with --set always wins over a default):
  HOME             the account's home (absolute). Required by every template.
  APP_DIR          the checkout. Required by board, tmux. APP_BIN defaults to <APP_DIR>/bin (ttyd, mem).
  SHELL            the account's login shell (absolute). Required by board, tmux, ttyd, code-server.
  CCBOARD_PORT, TTYD_PORT  loopback ports (1..65535). ENV_FILE  the settings file the board reads (board only; the installer passes <data dir>/env).
  TMUX_BIN, TTYD_BIN, CODE_SERVER_BIN  absolute paths of the tools (from `command -v` or `brew --prefix`).
  TMUX_SOCKET      defaults to ccboard. TMUX_TMPDIR defaults to /tmp and is the same value in the board, tmux and ttyd jobs (issue #125);
                   a value that makes <realpath(TMUX_TMPDIR)>/tmux-<uid>/<TMUX_SOCKET> longer than 100 bytes is refused (UID defaults to this
                   process's uid). TTYD_IFACE defaults to lo0 (the macOS loopback interface; `lo` does not exist there).
  LOG_DIR          defaults to <HOME>/Library/Logs/ccboard (create it first: launchd does not).
  PATH             defaults to <HOME>/.local/bin, <BREW_PREFIX>/bin, /opt/homebrew/bin, /usr/local/bin, /usr/bin, /bin, /usr/sbin, /sbin
                   (duplicates dropped; BREW_PREFIX is optional) and, for the mem job, <HOME>/.bun/bin first: expanded, never a `~`.
"""
from __future__ import annotations

import argparse
import os
import plistlib
import re
import sys
from xml.sax.saxutils import escape

TOKEN_RE = re.compile(r"__([A-Z][A-Z0-9_]*)__")
MAX_SOCKET_PATH = 100               # bytes; sun_path holds 104 on macOS and 108 on Linux, with room to spare (issue #125)
SESSION_TYPES = ("Aqua", "Background", "LoginWindow", "System")
ABSOLUTE = {"HOME", "SHELL", "APP_DIR", "APP_BIN", "LOG_DIR", "ENV_FILE", "TMUX_BIN", "TTYD_BIN", "CODE_SERVER_BIN", "TMUX_TMPDIR", "BREW_PREFIX"}
PORTS = {"CCBOARD_PORT", "TTYD_PORT"}


class RenderError(ValueError):
    """A value is missing or refused; the message is the one line the command prints."""


def tokens(text: str) -> list[str]:
    """The distinct token names in `text`, in first-seen order."""
    seen: list[str] = []
    for m in TOKEN_RE.finditer(text):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def default_path(home: str, brew_prefix: str | None = None, *, bun: bool = False) -> str:
    """The PATH a job gets, expanded (launchd's own is /usr/bin:/bin:/usr/sbin:/sbin and a plist does not expand `~`)."""
    parts = []
    if bun:
        parts.append(f"{home}/.bun/bin")
    parts.append(f"{home}/.local/bin")
    if brew_prefix:
        parts.append(f"{brew_prefix.rstrip('/')}/bin")
    parts += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    out: list[str] = []
    for p in parts:
        if p not in out:
            out.append(p)
    return ":".join(out)


def socket_path_length(tmpdir: str, uid: int, socket: str) -> int:
    """Bytes in the path tmux will bind: <realpath(tmpdir)>/tmux-<uid>/<socket>. The directory is resolved the way tmux resolves it, so on
    a Mac /tmp counts as /private/tmp."""
    base = os.path.realpath(tmpdir)
    return len(os.fsencode(f"{base.rstrip('/')}/tmux-{uid}/{socket}"))


def _check_value(name: str, value: str) -> None:
    if value == "":
        raise RenderError(f"{name} is empty")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise RenderError(f"{name} holds a control character (a newline or tab cannot go into a plist value)")
    if value.startswith("~"):
        raise RenderError(f"{name}={value!r} starts with ~: launchd does not expand it, pass the full path")
    if name in ABSOLUTE and not value.startswith("/"):
        raise RenderError(f"{name}={value!r} is not an absolute path")
    if name in PORTS and not (value.isdigit() and 1 <= int(value) <= 65535):
        raise RenderError(f"{name}={value!r} is not a port number (1 to 65535)")
    if name == "PATH":
        for part in value.split(":"):
            if not part.startswith("/"):
                raise RenderError(f"PATH holds {part!r}, which is not an absolute directory (no ~, no relative entry)")


def with_defaults(text: str, values: dict[str, str]) -> dict[str, str]:
    """`values` plus the defaults the template's tokens allow (see the module docstring). Explicit values always win."""
    out = dict(values)
    need = set(tokens(text))
    home = out.get("HOME")
    if home:
        if "APP_BIN" in need and "APP_BIN" not in out and out.get("APP_DIR"):
            out["APP_BIN"] = out["APP_DIR"].rstrip("/") + "/bin"
        if "LOG_DIR" in need:
            out.setdefault("LOG_DIR", home.rstrip("/") + "/Library/Logs/ccboard")
        if "PATH" in need and "PATH" not in out:
            # the mem job is recognised by its program (its template has no brew-prefix tool), not by a name passed in
            out["PATH"] = default_path(home, out.get("BREW_PREFIX"), bun="ccboard-mem-run" in text)
    if "TMUX_TMPDIR" in need:
        out.setdefault("TMUX_TMPDIR", "/tmp")
    if "TMUX_SOCKET" in need:
        out.setdefault("TMUX_SOCKET", "ccboard")
    if "TTYD_IFACE" in need:
        out.setdefault("TTYD_IFACE", "lo0")
    return out


def validate(text: str, values: dict[str, str]) -> None:
    """Every token has a value and every value is acceptable; raises RenderError otherwise."""
    missing = [t for t in tokens(text) if t not in values]
    if missing:
        raise RenderError("no value for " + ", ".join(missing) + " (pass --set NAME=VALUE)")
    for name in tokens(text):
        _check_value(name, values[name])
    if "TMUX_TMPDIR" in values and "TMUX_TMPDIR" in tokens(text):
        sock = values.get("TMUX_SOCKET", "ccboard")
        if "/" in sock:
            raise RenderError(f"TMUX_SOCKET={sock!r} must be a socket name, not a path")
        try:
            uid = int(values.get("UID") or os.getuid())
        except (ValueError, AttributeError):
            raise RenderError("UID must be a number") from None
        n = socket_path_length(values["TMUX_TMPDIR"], uid, sock)
        if n > MAX_SOCKET_PATH:
            raise RenderError(f"TMUX_TMPDIR={values['TMUX_TMPDIR']!r} makes the tmux socket path {n} bytes long, over the {MAX_SOCKET_PATH} byte limit: "
                              f"choose a shorter directory (CCBOARD_TMUX_TMPDIR)")


def render(text: str, values: dict[str, str], *, session_type: str | None = None) -> str:
    """The plist text for template `text`: defaults added, values checked, each value XML-escaped, and the result parsed with plistlib."""
    vals = with_defaults(text, values)
    validate(text, vals)
    out = TOKEN_RE.sub(lambda m: escape(vals[m.group(1)]), text)
    if session_type is not None:
        if session_type not in SESSION_TYPES:
            raise RenderError(f"--session-type must be one of {', '.join(SESSION_TYPES)}")
        end = out.rindex("</dict>\n</plist>")
        out = out[:end] + f"\t<key>LimitLoadToSessionType</key>\n\t<string>{session_type}</string>\n" + out[end:]
    try:
        plistlib.loads(out.encode("utf-8"))
    except Exception as e:                      # expat's ExpatError, plistlib's InvalidFileException: a template or value broke the XML
        raise RenderError(f"the rendered plist does not parse: {e}") from None
    return out


def render_file(template: str, out: str, values: dict[str, str], *, session_type: str | None = None) -> str:
    """Render the template file; write `out` (mode 0644, replaced atomically; "-" prints) and return the text."""
    with open(template, encoding="utf-8") as f:
        text = render(f.read(), values, session_type=session_type)
    if out == "-":
        sys.stdout.write(text)
        return text
    tmp = f"{out}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render a ccboard launchd plist template.")
    ap.add_argument("--template", required=True, help="launchd/dev.ccboard.<job>.plist.in")
    ap.add_argument("--out", help="the plist to write (- for stdout)")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="a token value (repeatable)")
    ap.add_argument("--session-type", choices=SESSION_TYPES, help="add LimitLoadToSessionType (Background for the experimental user domain)")
    ap.add_argument("--list-tokens", action="store_true", help="print the template's tokens and exit")
    a = ap.parse_args(argv)
    values: dict[str, str] = {}
    for item in a.set:
        name, eq, value = item.partition("=")
        if not eq or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            print(f"launchd_render: --set wants NAME=VALUE with an upper-case NAME, got {item!r}", file=sys.stderr)
            return 2
        values[name] = value
    try:
        if a.list_tokens:
            with open(a.template, encoding="utf-8") as f:
                text = f.read()
            for t in tokens(text):
                print(t)
            return 0
        if not a.out:
            print("launchd_render: --out is required (a file, or - for stdout)", file=sys.stderr)
            return 2
        render_file(a.template, a.out, values, session_type=a.session_type)
    except RenderError as e:
        print(f"launchd_render: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"launchd_render: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
