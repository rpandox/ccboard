#!/usr/bin/env python3
"""A command line over app/tailscale.py for the macOS (and later WSL) installer; the Linux install.sh keeps its own shell functions.

    tailscale_serve.py status [--json]
    tailscale_serve.py check --https P --path /X --target http://127.0.0.1:N [--board-https P] [--fqdn NAME]
    tailscale_serve.py apply --https P --path /X --target http://127.0.0.1:N [--board-https P] [--fqdn NAME]
    tailscale_serve.py off   --https P --path /X --target http://127.0.0.1:N [--board-https P] [--fqdn NAME]

status   prints one JSON object {ok, installed, cli, variant, capabilities, state, logged_in, fqdn, login, certs, serve} and exits 0 when
         Tailscale is installed, running and has a MagicDNS name and HTTPS certificates, else 1 (the installer's pre-flight checks).
check    prints one word and exits 0: ours | missing | stale | funnel | foreign:<why>; or `unreadable` and exits 3 when the serve
         configuration cannot be read (not running, denied): never read as "missing". install.sh's serve_check, rule for rule.
apply    makes <path> on <https> point at <target>: ours is left alone, missing is added, stale (a ccboard handler with an old backend
         port) is replaced. A foreign handler, a TCP forward or a Funnel on that port is refused (exit 1) unless CCBOARD_REPLACE_SERVE=1,
         which turns that one port off and applies again. Exit 0 when the mapping is in place, 1 when refused or failed.
off      turns off only a mapping that is ours or stale (the path, not the port); anything else is left alone.

Safety, the same as install.sh: `tailscale serve reset` is not a verb (the parser has no such command and nothing here runs it), a
foreign or Funnel port is never replaced without CCBOARD_REPLACE_SERVE=1, and port 443 is never touched unless it is the board's own
HTTPS port (--board-https, default CCBOARD_HTTPS_PORT; with neither set 443 is always refused); even then a foreign or Funnel mapping on 443 is never replaced. The target
must be http://127.0.0.1:<port>. No sudo is run off Linux; a refusal names the Tailscale variant and the fix.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import tailscale as ts  # noqa: E402

TARGET_RE = re.compile(r"^http://127\.0\.0\.1:\d{1,5}(/[A-Za-z0-9._~/-]*)?$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")
UNREADABLE = 3


def _say(text: str) -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _port(text: str) -> int:
    if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= 65535:
        raise argparse.ArgumentTypeError(f"not a port from 1 to 65535: {text!r}")
    return int(text)


def _board_default() -> int | None:
    """The board's HTTPS port as configured (CCBOARD_HTTPS_PORT), or None when it is not set: 443 is then not the board's, so it is refused."""
    raw = (os.environ.get("CCBOARD_HTTPS_PORT") or "").strip()
    return int(raw) if raw.isascii() and raw.isdigit() and 1 <= int(raw) <= 65535 else None


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tailscale_serve.py", description="ccboard's tailscale serve helper (check, apply, off, status)")
    sub = ap.add_subparsers(dest="verb", required=True)
    st = sub.add_parser("status", help="installed, running, signed in, MagicDNS name, HTTPS certificates, as JSON")
    st.add_argument("--json", action="store_true", help="accepted for symmetry; the output is always JSON")
    for name, text in (("check", "print ours|missing|stale|funnel|foreign:<why>"), ("apply", "make the mapping exist"), ("off", "turn off our own mapping")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--https", required=True, type=_port, help="the tailnet HTTPS port")
        p.add_argument("--path", required=True, help="the URL path, / or /tty")
        p.add_argument("--target", required=True, help="http://127.0.0.1:<port>")
        p.add_argument("--board-https", type=_port, default=None, help="the board's own HTTPS port (default CCBOARD_HTTPS_PORT; unset means 443 is not the board's)")
        p.add_argument("--fqdn", default="", help="the node's MagicDNS name (default: read from tailscale status)")
    return ap


def cmd_status() -> int:
    cli = ts.find_cli()
    out: dict = {"ok": False, "installed": cli is not None, "cli": cli.exe if cli else None, "variant": ts.variant(cli),
                 "state": "", "logged_in": False, "fqdn": "", "login": "", "certs": 0, "serve": "unknown", "reason": ""}
    out["capabilities"] = ts.capabilities(out["variant"])
    if cli is None:
        out["reason"] = ts.missing_reason()
    else:
        r = ts.call(["status", "--json"])
        d = ts._json(r.out) if r.rc == 0 else None
        if not isinstance(d, dict) or not d:
            out["reason"] = "tailscale status failed; is tailscaled running and logged in?"
        else:
            out.update(ts.summarize_status(d))
            out["logged_in"] = out["state"] == "Running"
            if not out["logged_in"]:
                out["reason"] = f"tailscale is not running/logged in (BackendState={out['state'] or '-'})"
            elif not out["fqdn"]:
                out["reason"] = "this node has no MagicDNS name; enable MagicDNS in the admin console"
            elif not out["certs"]:
                out["reason"] = "HTTPS certificates are not enabled for this tailnet: turn on 'HTTPS Certificates' at https://login.tailscale.com/admin/dns"
            else:
                out["ok"] = True
        if out["logged_in"]:
            out["serve"] = "readable" if ts.serve_status() is not None else "unreadable"
    _say(json.dumps(out))
    return 0 if out["ok"] else 1


def _fqdn(args) -> str:
    return args.fqdn or ts.summarize_status(ts.status())["fqdn"]


def _validate(args) -> str | None:
    if not PATH_RE.match(args.path):
        return f"--path must start with / and hold only letters, digits and . _ ~ / - (got {args.path!r})"
    if not TARGET_RE.match(args.target):
        return f"--target must be http://127.0.0.1:<port> (got {args.target!r})"
    return None


def _state(args, board: int | None, fqdn: str) -> str | None:
    """The mapping's state, or None when the serve configuration cannot be read."""
    if not fqdn:
        return None
    d = ts.serve_status()
    if d is None:
        return None
    return ts.mapping_state(d, fqdn, args.https, args.path, args.target, board)


def _refuse_443(args, board: int | None) -> str | None:
    if args.https == 443 and board != 443:
        return "port 443 is never touched: it is not the board's HTTPS port (--board-https / CCBOARD_HTTPS_PORT)"
    return None


def cmd_check(args) -> int:
    board = args.board_https if args.board_https is not None else _board_default()
    st = _state(args, board, _fqdn(args))
    if st is None:
        _say("unreadable")
        _err("could not read the tailscale serve configuration (is Tailscale running, signed in and allowed to run serve?)")
        return UNREADABLE
    _say(st)
    return 0


def _run(serve_args: list[str]) -> str | None:
    """Run one `tailscale serve` change; the error sentence, or None when it worked."""
    try:
        ts.run_serve(serve_args)
    except ts.TailscaleError as e:
        return str(e)
    return None


def cmd_apply(args) -> int:
    board = args.board_https if args.board_https is not None else _board_default()
    why = _refuse_443(args, board)
    if why:
        _err(why)
        return 1
    fqdn = _fqdn(args)
    st = _state(args, board, fqdn)
    if st is None:
        _err("could not read the tailscale serve configuration; nothing changed (is Tailscale running, signed in and allowed to run serve?)")
        return UNREADABLE
    where = f"https://{fqdn}:{args.https}{args.path}"
    if st == "ours":
        _say(f"{where} -> {args.target} (already set)")
        return 0
    if st in ("missing", "stale"):
        if st == "stale":
            _say(f"replacing the old ccboard handler at {where}")
            _run(ts.serve_path_off_args(args.https, args.path))        # a failure here shows up when the mapping is added
        err = _run(ts.serve_path_on_args(args.https, args.path, args.target))
        if err:
            _err(err)
            return 1
        _say(f"{where} -> {args.target}")
        return 0
    # funnel or foreign:<why>
    if os.environ.get("CCBOARD_REPLACE_SERVE") != "1":
        _err(f"tailscale serve port {args.https} is already used by something else ({st}). Pick another port, or rerun with "
             "CCBOARD_REPLACE_SERVE=1 to replace it.")
        return 1
    if args.https == 443:
        _err(f"port 443 carries something else ({st}); it is never replaced, even with CCBOARD_REPLACE_SERVE=1")
        return 1
    _err(f"port {args.https} has other handlers ({st}); replacing because CCBOARD_REPLACE_SERVE=1")
    err = _run(ts.serve_off_args(args.https))                          # --https=P --yes off: that one port, never a reset
    if err:
        _err(err)
        return 1
    st = _state(args, board, fqdn)
    if st != "missing":
        _err(f"port {args.https} still holds something else after turning it off ({st}); nothing more was changed")
        return 1
    err = _run(ts.serve_path_on_args(args.https, args.path, args.target))
    if err:
        _err(err)
        return 1
    _say(f"{where} -> {args.target}")
    return 0


def cmd_off(args) -> int:
    board = args.board_https if args.board_https is not None else _board_default()
    why = _refuse_443(args, board)
    if why:
        _err(why)
        return 1
    st = _state(args, board, _fqdn(args))
    if st is None:
        _err("could not read the tailscale serve configuration; nothing changed")
        return UNREADABLE
    if st not in ("ours", "stale"):
        _say(f"port {args.https} path {args.path} is not a ccboard mapping ({st}); left alone")
        return 0
    err = _run(ts.serve_path_off_args(args.https, args.path))
    if err:
        _err(err)
        return 1
    _say(f"turned off https port {args.https} path {args.path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.verb == "status":
        return cmd_status()
    bad = _validate(args)
    if bad:
        _err(bad)
        return 2
    return {"check": cmd_check, "apply": cmd_apply, "off": cmd_off}[args.verb](args)


if __name__ == "__main__":
    sys.exit(main())
