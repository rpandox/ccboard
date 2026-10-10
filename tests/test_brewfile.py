"""Issue #128: the Brewfiles, the README "macOS tools" table, scripts/macos_tools.py (bottle preflight, ttyd acceptance, optional picks) and the
code-server config that must equal the one install.sh writes on Linux.

Nothing installs anything: `brew info` bodies are canned, ttyd is a fake script in a temp directory, and brew is never run.
"""
import importlib.util
import json
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAC = ROOT / "scripts" / "macos"
SCRIPT = ROOT / "scripts" / "macos_tools.py"
spec = importlib.util.spec_from_file_location("macos_tools", SCRIPT)
mt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mt)

REQUIRED = ["git", "tmux", "python@3.12", "node", "gh", "ttyd"]
OPTIONAL = ["code-server", "restic", "oven-sh/bun/bun", "shellcheck"]


# ---------------------------------------------------------------- the Brewfiles


def lines(path):
    return [ln for ln in path.read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def tools_section():
    """The README's "macOS tools" subsection only (other macOS sections and tables may sit beside it)."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    sec = text[text.index("### macOS tools"):]
    return sec[3:][:re.search(r"\n#{2,3} ", sec[3:]).start()]


def test_the_required_and_optional_sets_are_exactly_the_documented_lists():
    assert mt.brewfile_names((MAC / "Brewfile").read_text()) == (REQUIRED, [])
    assert mt.brewfile_names((MAC / "Brewfile.optional").read_text()) == (OPTIONAL, ["oven-sh/bun"])


def test_brewfiles_hold_only_brew_and_tap_lines_no_casks_no_duplicates():
    seen = []
    for name in ("Brewfile", "Brewfile.optional"):
        for ln in lines(MAC / name):
            assert re.fullmatch(r'(brew|tap) "[A-Za-z0-9@._/+-]+"', ln), f"{name}: {ln!r}"
            assert not re.match(r"\s*(cask|mas|vscode|whalebrew)\b", ln)
            seen.append(ln)
    assert len(seen) == len(set(seen)), "no duplicate line, in one file or across the two"
    assert not set(mt.brewfile_names((MAC / "Brewfile").read_text())[0]) & set(mt.brewfile_names((MAC / "Brewfile.optional").read_text())[0])


def test_the_bun_tap_comes_before_its_formula():
    ls = lines(MAC / "Brewfile.optional")
    assert ls.index('tap "oven-sh/bun"') < ls.index('brew "oven-sh/bun/bun"')


def test_the_readme_table_names_the_same_formulae_as_the_brewfiles():
    sec = tools_section()
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in sec.splitlines() if ln.startswith("| ") and not ln.startswith("| Tool") and "---" not in ln]
    assert [r[1].split(" ")[0].strip("`") for r in rows if r[2] == "required"] == REQUIRED
    assert [r[1].split(" ")[0].strip("`") for r in rows if r[2] == "optional"] == OPTIONAL
    assert len(rows) == len(REQUIRED) + len(OPTIONAL) and all(len(r) == 6 for r in rows)


def test_the_readme_says_ntfy_is_not_installed_and_web_push_works_without_it():
    sec = tools_section()
    assert "ntfy is not installed on a Mac" in sec and "Web Push" in sec and "NTFY_URL" in sec and "NTFY_PUBLIC_URL" in sec
    assert "any local user of the Mac can open the editor" in sec, "auth: none on loopback is said out loud"
    assert "September 2027" in sec and "Tier 3" in sec


# ---------------------------------------------------------------- the bottle preflight


def formula(name, files):
    return {"formulae": [{"name": name, "full_name": name, "bottle": {"stable": {"files": files}} if files is not None else {}}], "casks": []}


def f(cellar=":any_skip_relocation"):
    return {"cellar": cellar, "url": "https://example.invalid/x.tar.gz", "sha256": "0" * 64}


# the research bodies: ttyd has bottles for arm64 on Sequoia and Tahoe and for Linux only; tmux has no Intel macOS bottle
TTYD = formula("ttyd", {"arm64_tahoe": f(), "arm64_sequoia": f(), "arm64_linux": f(), "x86_64_linux": f()})
TMUX = formula("tmux", {"arm64_tahoe": f(), "arm64_sequoia": f(), "arm64_sonoma": f(), "arm64_linux": f(), "x86_64_linux": f()})


@pytest.mark.parametrize("info,formula_name,macos,arch,status", [
    (TTYD, "ttyd", "15.2", "arm64", "bottle"),            # Apple silicon on Sequoia
    (TTYD, "ttyd", "26.0", "arm64", "bottle"),
    (TTYD, "ttyd", "14.7.3", "x86_64", "source"),         # the Intel Mac on Sonoma
    (TTYD, "ttyd", "14.7.3", "arm64", "source"),          # Apple silicon on Sonoma: no ttyd bottle
    (TTYD, "ttyd", "15.2", "x86_64", "source"),           # Linux bottles do not count
    (TMUX, "tmux", "14.7.3", "arm64", "bottle"),
    (TMUX, "tmux", "14.7.3", "x86_64", "source"),
    (formula("none", {}), "none", "15.2", "arm64", "source"),
    (formula("none", None), "none", "15.2", "arm64", "source"),
    (formula("pure", {"all": f()}), "pure", "14.7.3", "x86_64", "bottle"),
    ({"formulae": [], "casks": []}, "ttyd", "15.2", "arm64", "unknown"),
    (TTYD, "tmux", "15.2", "arm64", "unknown"),            # the body is for another formula
    ([], "ttyd", "15.2", "arm64", "unknown"),
    (TTYD, "ttyd", "99.0", "arm64", "unknown"),            # a macOS this table cannot name
    (TTYD, "ttyd", "banana", "arm64", "unknown"),
    (TTYD, "ttyd", "15.2", "riscv", "unknown"),
])
def test_bottle_status(info, formula_name, macos, arch, status):
    assert mt.bottle_status(info, formula_name, macos, arch)[0] == status


def test_an_older_macos_bottle_counts_only_when_it_is_relocatable():
    older = formula("x", {"arm64_sonoma": f(":any_skip_relocation")})
    assert mt.bottle_status(older, "x", "15.2", "arm64")[0] == "bottle"
    fixed = formula("x", {"arm64_sonoma": f("/opt/homebrew/Cellar")})
    assert mt.bottle_status(fixed, "x", "15.2", "arm64")[0] == "source"
    newer = formula("x", {"arm64_tahoe": f()})
    assert mt.bottle_status(newer, "x", "15.2", "arm64")[0] == "source", "a bottle for a newer macOS is no use"
    intel = formula("x", {"arm64_sonoma": f()})
    assert mt.bottle_status(intel, "x", "15.2", "x86_64")[0] == "source", "an arm64 bottle is no use on Intel"


def test_a_tag_can_be_given_for_a_macos_newer_than_the_table():
    assert mt.bottle_status(TTYD, "ttyd", "99.0", "arm64", "arm64_tahoe")[0] == "bottle"
    assert mt.bottle_tag("14.7.3", "x86_64") == "sonoma" and mt.bottle_tag("14.7.3", "arm64") == "arm64_sonoma"


def bottle_cli(tmp_path, info, *args, raw=None):
    p = tmp_path / "info.json"
    p.write_text(raw if raw is not None else json.dumps(info))
    return subprocess.run([sys.executable, str(SCRIPT), "bottle", "--file", str(p), *args], capture_output=True, text=True)


def test_cli_apple_silicon_on_sequoia_says_bottle_available(tmp_path):
    r = bottle_cli(tmp_path, TTYD, "--formula", "ttyd", "--macos", "15.2", "--arch", "arm64", "--explain")
    assert r.returncode == 0 and r.stdout.startswith("bottle available (")
    assert "Tier 3" not in r.stdout and "several minutes" not in r.stdout


def test_cli_intel_on_sonoma_says_builds_from_source_with_the_tier_3_sentence(tmp_path):
    r = bottle_cli(tmp_path, TTYD, "--formula", "ttyd", "--macos", "14.7.3", "--arch", "x86_64", "--explain")
    assert r.returncode == 10 and r.stdout.startswith("builds from source (")
    assert "several minutes" in r.stdout and "Tier 3" in r.stdout and "September 2027" in r.stdout
    r = bottle_cli(tmp_path, TTYD, "--formula", "ttyd", "--macos", "14.7.3", "--arch", "arm64", "--explain")
    assert r.returncode == 10 and "several minutes" in r.stdout and "Tier 3" not in r.stdout, "the Intel sentence is for Intel only"


def test_cli_explains_the_measured_ttyd_source_build_and_what_no_upgrade_does_not_stop(tmp_path):
    """Issue #128, device row M8: on the Intel Mac with macOS 14 there is no ttyd bottle, cmake ran for more than ten minutes and the install
    would have upgraded 11 packages (python@3.14 among them). `brew bundle --no-upgrade` is documented as not stopping that."""
    r = bottle_cli(tmp_path, TTYD, "--formula", "ttyd", "--macos", "14.7.3", "--arch", "x86_64", "--explain")
    assert r.returncode == 10
    assert "Measured on an Intel Mac with macOS 14" in r.stdout and "no bottle" in r.stdout and "cmake" in r.stdout
    assert "more than 10 minutes" in r.stdout and "11 installed packages" in r.stdout and "python@3.14" in r.stdout
    assert "--no-upgrade" in r.stdout and "may still be upgraded" in r.stdout
    # the ttyd finding belongs to ttyd; the upgrade warning belongs to every source build
    r = bottle_cli(tmp_path, TMUX, "--formula", "tmux", "--macos", "14.7.3", "--arch", "x86_64", "--explain")
    assert r.returncode == 10 and "Measured on an Intel Mac" not in r.stdout and "python@3.14" not in r.stdout
    assert "--no-upgrade" in r.stdout and "several minutes" in r.stdout
    # a bottle needs neither
    r = bottle_cli(tmp_path, TTYD, "--formula", "ttyd", "--macos", "15.2", "--arch", "arm64", "--explain")
    assert "Measured" not in r.stdout and "--no-upgrade" not in r.stdout
    # without --explain only the verdict line is printed
    r = bottle_cli(tmp_path, TTYD, "--formula", "ttyd", "--macos", "14.7.3", "--arch", "x86_64")
    assert r.stdout.count("\n") == 1 and "Measured" not in r.stdout


def test_cli_never_claims_a_bottle_when_brew_info_failed(tmp_path):
    for raw in ("", "not json", "{}", '{"formulae": "x"}'):
        r = bottle_cli(tmp_path, None, "--formula", "ttyd", "--macos", "15.2", "--arch", "arm64", "--explain", raw=raw)
        assert r.returncode == 20, raw
        assert r.stdout.startswith("could not tell") and "bottle available" not in r.stdout
    r = subprocess.run([sys.executable, str(SCRIPT), "bottle", "--file", str(tmp_path / "missing.json"), "--macos", "15.2", "--arch", "arm64"], capture_output=True, text=True)
    assert r.returncode == 20 and r.stdout.startswith("could not tell")


def test_cli_reads_stdin_when_no_file_is_given():
    r = subprocess.run([sys.executable, str(SCRIPT), "bottle", "--formula", "tmux", "--macos", "14.7.3", "--arch", "x86_64"],
                       input=json.dumps(TMUX), capture_output=True, text=True)
    assert r.returncode == 10 and r.stdout.startswith("builds from source")


# ---------------------------------------------------------------- ttyd acceptance

HELP = """ttyd is a tool for sharing terminal over the web

USAGE:
    ttyd [options] <command> [<arguments...>]

OPTIONS:
    -p, --port              Port to listen (default: 7681, use `0` for random port)
    -i, --interface         Network interface to bind (eg: eth0), or UNIX domain socket path (eg: /var/run/ttyd.sock)
    -a, --url-arg           Allow client to send command line arguments in URL (eg: http://localhost:7681?arg=foo&arg=bar)
    -W, --writable          Allow clients to write to the TTY (readonly by default)
    -t, --client-option     Send option to client (format: key=value), repeat to add more options
    -O, --check-origin      Do not allow websocket connection from different origin
    -h, --help              Print this text and exit
"""


def drop(text, flag):
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith(flag + ",")) + "\n"


def test_a_good_ttyd_is_accepted():
    assert mt.check_ttyd("ttyd version 1.7.7-f14ce6d\n", HELP) == []
    assert mt.check_ttyd("ttyd version 1.7.4\n", HELP) == []
    assert mt.check_ttyd("ttyd version 1.10.0\n", HELP) == [], "1.10 is newer than 1.7, not older"


def test_an_old_ttyd_fails_with_its_version_named():
    out = mt.check_ttyd("ttyd version 1.7.3\n", HELP)
    assert len(out) == 1 and "1.7.3" in out[0] and "1.7.4" in out[0]
    assert mt.check_ttyd("ttyd version 1.6.3\n", HELP)


@pytest.mark.parametrize("flag", ["-W", "-O", "-a"])
def test_a_missing_flag_is_named(flag):
    out = mt.check_ttyd("ttyd version 1.7.7\n", drop(HELP, flag))
    assert len(out) == 1 and f"does not list {flag} " in out[0]


def test_a_flag_mentioned_in_prose_is_not_a_listed_flag():
    prose = drop(HELP, "-W") + "    note: use -W to write\n"
    assert any("-W" in p for p in mt.check_ttyd("ttyd version 1.7.7\n", prose))


def test_unreadable_version_and_empty_help_report_everything():
    out = mt.check_ttyd("", "")
    assert len(out) == 4 and "version" in out[0] and all(f in " ".join(out) for f in ("-W", "-O", "-a"))


def fake_ttyd(tmp_path, version, help_text):
    exe = tmp_path / "ttyd"
    exe.write_text(f'#!/bin/sh\ncase "$1" in --version) printf "%s\\n" {json.dumps(version)};; --help) cat <<\'EOF\'\n{help_text}EOF\n;; esac\n')
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def test_cli_ttyd_runs_the_binary_and_names_the_failure(tmp_path):
    good = fake_ttyd(tmp_path, "ttyd version 1.7.7", HELP)
    r = subprocess.run([sys.executable, str(SCRIPT), "ttyd", "--bin", str(good)], capture_output=True, text=True)
    assert r.returncode == 0 and r.stderr == ""
    (tmp_path / "bad").mkdir()
    bad = fake_ttyd(tmp_path / "bad", "ttyd version 1.6.3", drop(HELP, "-W"))
    r = subprocess.run([sys.executable, str(SCRIPT), "ttyd", "--bin", str(bad)], capture_output=True, text=True)
    assert r.returncode == 1 and "ttyd 1.6.3 is older than 1.7.4" in r.stderr and "does not list -W" in r.stderr
    r = subprocess.run([sys.executable, str(SCRIPT), "ttyd", "--bin", str(tmp_path / "nothing-here")], capture_output=True, text=True)
    assert r.returncode != 0 and "could not run" in (r.stderr + str(r.stdout))


def test_cli_ttyd_takes_captured_text_files(tmp_path):
    v, h = tmp_path / "v.txt", tmp_path / "h.txt"
    v.write_text("ttyd version 1.7.7\n")
    h.write_text(drop(HELP, "-O"))
    r = subprocess.run([sys.executable, str(SCRIPT), "ttyd", "--version-text", str(v), "--help-text", str(h)], capture_output=True, text=True)
    assert r.returncode == 1 and "does not list -O" in r.stderr


# ---------------------------------------------------------------- optional picks


def test_optional_bundle_picks_the_named_tools_and_brings_the_tap_along():
    assert mt.optional_bundle(["restic"]) == 'brew "restic"\n'
    assert mt.optional_bundle(["bun"]) == 'tap "oven-sh/bun"\nbrew "oven-sh/bun/bun"\n'
    assert mt.optional_bundle(["code-server", "restic", "bun"]) == 'brew "code-server"\nbrew "restic"\ntap "oven-sh/bun"\nbrew "oven-sh/bun/bun"\n'
    assert mt.optional_bundle([]) == ""
    assert mt.optional_bundle(None).splitlines() == lines(MAC / "Brewfile.optional")
    with pytest.raises(ValueError, match="unknown optional tool nope"):
        mt.optional_bundle(["restic", "nope"])


def test_cli_optional_bundle_default_pick_is_every_tool_and_unknown_is_refused():
    r = subprocess.run([sys.executable, str(SCRIPT), "optional-bundle", "--pick", "code-server,bun"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == 'brew "code-server"\ntap "oven-sh/bun"\nbrew "oven-sh/bun/bun"\n'
    r = subprocess.run([sys.executable, str(SCRIPT), "optional-bundle", "--pick", "ntfy"], capture_output=True, text=True)
    assert r.returncode == 2 and "unknown optional tool ntfy" in r.stderr and r.stdout == ""


# ---------------------------------------------------------------- code-server config equals the Linux one


def linux_config(port):
    """What install.sh writes to ~/.config/code-server/config.yaml: its printf format with the port, one trailing newline."""
    m = re.search(r"cs_want=\$\(printf '((?:[^'\\]|\\.)*)' \"\$CODE_SERVER_PORT\"\)", (ROOT / "install.sh").read_text())
    assert m, "install.sh no longer builds cs_want with printf: update this test with it"
    fmt = m.group(1)
    assert fmt.count("%s") == 1
    return fmt.replace("%s", str(port)).replace("\\n", "\n").rstrip("\n") + "\n"


def test_the_linux_template_is_pinned():
    assert linux_config(8080) == ("# managed by ccboard\nbind-addr: 127.0.0.1:8080\nauth: none\ncert: false\ndisable-telemetry: true\n"
                                  "disable-update-check: true\ndisable-workspace-trust: true\ndisable-getting-started-override: true\n")


@pytest.mark.parametrize("port", [8080, 8081, 1, 65535])
def test_the_macos_code_server_config_equals_the_linux_one_byte_for_byte(port):
    assert mt.code_server_config(port).encode() == linux_config(port).encode()
    assert (MAC / "code-server-config.yaml.in").read_text().splitlines()[0] == "# managed by ccboard", "install.sh's overwrite guard looks at the first line"


def test_cli_prints_the_config_and_refuses_a_bad_port():
    r = subprocess.run([sys.executable, str(SCRIPT), "code-server-config", "--port", "8081"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == linux_config(8081)
    for bad in ("0", "70000", "x"):
        r = subprocess.run([sys.executable, str(SCRIPT), "code-server-config", "--port", bad], capture_output=True, text=True)
        assert r.returncode == 2 and r.stdout == "", bad


def test_the_code_server_job_template_has_no_port_because_the_config_has_it():
    text = (ROOT / "launchd" / "dev.ccboard.code-server.plist.in").read_text()
    assert "PORT" not in text and "bind-addr" not in text
