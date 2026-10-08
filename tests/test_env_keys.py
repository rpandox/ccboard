"""One list of settings (issue #55): install.sh's ENV_KEYS, the README settings tables and the names the code reads must agree.

    every ENV_KEYS name has a README row;
    every README row is an ENV_KEYS name or sits in NOT_REMEMBERED with a reason;
    every name the code reads is an ENV_KEYS name or sits in INTERNAL with a reason;
    install.sh binds every ENV_KEYS name before the env file is written (it runs under `set -u`).

Adding a setting therefore touches the reader, ENV_KEYS, a README row and, when the installer does not remember it, one line with a
reason in the dicts below. A failure names the setting.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
README = ROOT / "README.md"
PREFIXES = ("CCBOARD_", "NTFY_", "CODE_", "CODEX_", "TTYD_", "PREVIEW_", "PROJECTS_")

# README rows that install.sh does not remember in /etc/ccboard/env, each with the reason.
NOT_REMEMBERED = {
    "CCBOARD_DEVCONTAINER": "a one-time install step (installs the devcontainer CLI), nothing to remember",
    "CCBOARD_RECOVER": "read at each start of the board; set it in the service environment",
    "CCBOARD_IMAGE_TAG": "docker mode only: kept in the compose .env, not in the env file",
    "CCBOARD_REMOTE_APPROVE": "install.sh keeps it with its own preserve block (docker_remote_approve), as a hand-added line",
    "CCBOARD_URL": "set by the hook wrappers for the hook scripts; not a board setting",
    "CCBOARD_HOOK_TOKEN_FILE": "set for the hook scripts and the MCP shim; not a board setting",
    "CCBOARD_AGENT": "set by the hook commands themselves (codex sets it); not a board setting",
    "CCBOARD_TMUX_SOCKET": "ccboard-tmux.service, bin/ccboard-attach and the entrypoint all name the socket; an empty remembered value would break the app's default",
    "CCBOARD_MEM_BUN": "for ccboard-mem.service (systemctl edit); an empty remembered value is not the same as unset",
    "CCBOARD_MEM_BUN_DIRS": "for ccboard-mem.service (systemctl edit); an empty value REPLACES the default list, so it is never written empty",
    "CCBOARD_MEM_PLUGIN_DIR": "for ccboard-mem.service (systemctl edit)",
    "CCBOARD_CONTAINER": "read by the watchdog script on the box; set in its environment",
    "CCBOARD_SHADOW": "container entrypoint switch for a side-by-side run; never a permanent setting",
    "CCBOARD_APP_ROOT": "where the image keeps the app; tests point it elsewhere",
    "CLAUDE_MEM_PROJECT_ENVIRONMENTS": "claude-mem's own variable, read by the board; not a ccboard setting",
    "CCBOARD_ENV_FILE": "names the settings file the board reads itself on macOS (launchd has no EnvironmentFile); the installer sets it in each job's plist, not in /etc/ccboard/env",
    "CCBOARD_LAUNCHD_DOMAIN": "macOS launchd layout (gui or user); an installer-time choice written into the plists, not remembered in /etc/ccboard/env",
}

# Names the code reads that are neither ENV_KEYS nor a user setting, each with the reason. Everything in NOT_REMEMBERED counts too.
EXTRA_INTERNAL = {
    "CCBOARD_IMAGE_VERSION": "baked into the image by the build; shown in the footer",
    "CCBOARD_IMAGE_REVISION": "baked into the image by the build; the doctor's build check compares it with the repository",
    "CCBOARD_SOURCE_REPO": "baked into the image by the build (owner/name); the doctor's build check reads it",
    "CCBOARD_DEV_BYPASS_USER": "development only (README, Development); never a production setting",
    "CCBOARD_SESSION": "set by the board for a session's own processes",
    "CCBOARD_CURL_MAX": "an optional cap (seconds) on the hook wrappers' request, described in the wrapper's own header",
    "CCBOARD_HOST_PYTHON": "the container entrypoint's choice of a host-valid python for the MCP shim; the default is right on the supported hosts",
    "CCBOARD_SMOKE_PORT": "scripts/ci-smoke.sh only: the port of the CI smoke test",
    "CCBOARD_SMOKE_WAIT": "scripts/ci-smoke.sh only: how long the CI smoke test waits",
    "CCBOARD_REPLACE_SERVE": "an installer-run switch (install.sh, scripts/tailscale_serve.py) that allows replacing another Tailscale serve handler; never remembered",
    "CCBOARD_BACKUP": "an installer switch (0 = no nightly backup job; install.sh and scripts/install-macos.sh); the job's absence is the record, nothing to remember",
    "CCBOARD_KEEP_AWAKE": "macOS installer switch: 1 adds the dev.ccboard.awake job (caffeinate); the job's presence is the record, nothing to remember",
    "CCBOARD_MACOS_OPTIONAL": "macOS installer: which optional Homebrew tools to install (default code-server,restic,bun; none skips them); installer-time only",
    "CCBOARD_MACOS_INSTALL_TOOLS": "macOS installer: 1 installs missing Homebrew tools without asking, 0 never; installer-time only",
    "CCBOARD_MACOS_VENV_READY": "macOS installer test seam: the venv is already built, so pip is never run; documented in the script header",
    "CCBOARD_MACOS_HEALTH_TRIES": "macOS installer: seconds to wait for /healthz after the jobs start (default 30); installer-time only",
    "CCBOARD_REPLACE_CODE_SERVER_CONFIG": "an installer-run switch (install.sh, scripts/install-macos.sh) that backs up and replaces a code-server config ccboard did not write; never remembered",
    "CCBOARD_TMUX_TMPDIR": "macOS installer: the TMUX_TMPDIR written into every launchd job (default /tmp); it lives in the plists, not in the settings file",
    "CODE_SERVER_SETTINGS": "points the code-server settings merge (and its doctor check) at another file; tests and unusual installs only",
}
INTERNAL = {**NOT_REMEMBERED, **EXTRA_INTERNAL}

# a shell variable that is not a setting: the language of the script itself
SHELL_IGNORE = set()


def env_keys():
    m = re.search(r"^ENV_KEYS=\((.*)\)$", INSTALL.read_text(), re.M)
    assert m, "install.sh lost its ENV_KEYS line"
    return m.group(1).split()


def readme_rows(text=None):
    """Names that open a README row in the settings section (### Settings up to ### What install.sh sets up)."""
    text = text if text is not None else README.read_text()
    a, b = text.index("### Settings"), text.index("### What install.sh sets up")
    return set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)` \|", text[a:b], re.M))


PY_READ = re.compile(r"""(?:environ|env)\s*\.\s*get\(\s*["']([A-Z][A-Z0-9_]+)["']|getenv\(\s*["']([A-Z][A-Z0-9_]+)["']|environ\[\s*["']([A-Z][A-Z0-9_]+)["']\s*\]"""
                     r"""|^[A-Z_]*ENV\s*=\s*["']([A-Z][A-Z0-9_]+)["']""", re.M)
SH_READ = re.compile(r"\$\{(" + "|".join(p + r"[A-Z0-9_]*" for p in PREFIXES) + r")")


def code_reads():
    names = set()
    files = [p for p in (ROOT / "app").rglob("*.py")] + sorted((ROOT / "scripts").glob("*.py"))
    for p in files:
        for m in PY_READ.finditer(p.read_text(errors="replace")):
            names.add(next(g for g in m.groups() if g))
    shells = [p for p in (ROOT / "bin").iterdir() if p.is_file()] + sorted((ROOT / "scripts").glob("*.sh"))
    for p in shells:
        try:
            text = p.read_text()
        except UnicodeDecodeError:
            continue
        names |= set(SH_READ.findall(text))
    return {n for n in names if n.startswith(PREFIXES)}


def problems(keys, rows, reads, not_remembered=NOT_REMEMBERED, internal=INTERNAL):
    out = []
    out += [f"{k} is in ENV_KEYS but has no README row" for k in sorted(set(keys) - rows)]
    out += [f"README row {r} is neither in ENV_KEYS nor in NOT_REMEMBERED (give it a reason)" for r in sorted(rows - set(keys) - set(not_remembered))]
    out += [f"the code reads {n} but it is neither in ENV_KEYS nor in INTERNAL (give it a reason)" for n in sorted(reads - set(keys) - set(internal))]
    return out


def test_the_three_lists_agree():
    assert problems(env_keys(), readme_rows(), code_reads()) == []


def test_the_parsers_see_something():
    keys, rows, reads = env_keys(), readme_rows(), code_reads()
    assert len(keys) > 30 and "CCBOARD_PORT" in keys and "CCBOARD_MEM_HTTPS_PORT" in keys
    assert {"PROJECTS_DIR", "CCBOARD_TMUX_SOCKET", "CCBOARD_AUTOCLOSE_GRACE"} <= rows
    assert {"CCBOARD_PORT", "CCBOARD_TMUX_SOCKET", "CCBOARD_CLAUDE_ULTRACODE_FLAG", "CCBOARD_MEM_BUN_DIRS", "CCBOARD_CODEX_HOOKS_ASYNC",
            "CCBOARD_AUTOCLOSE_GRACE", "CCBOARD_REMOTE_APPROVE"} <= reads, "python and shell reads are both collected"


def test_a_missing_env_key_a_missing_row_and_an_unlisted_read_each_fail_naming_the_setting():
    keys, rows, reads = env_keys(), readme_rows(), code_reads()
    assert "CCBOARD_AUTOCLOSE_GRACE" in keys
    gone = [k for k in keys if k != "CCBOARD_AUTOCLOSE_GRACE"]
    p = problems(gone, rows, reads)
    assert any("CCBOARD_AUTOCLOSE_GRACE" in x and "README row" in x for x in p), "a README row without an ENV_KEY"
    assert any("CCBOARD_AUTOCLOSE_GRACE" in x and "the code reads" in x for x in p), "a read without an ENV_KEY"
    p = problems(keys, rows - {"CCBOARD_AUTOCLOSE_GRACE"}, reads)
    assert p == ["CCBOARD_AUTOCLOSE_GRACE is in ENV_KEYS but has no README row"]
    p = problems(keys, rows, reads | {"CCBOARD_NEW_THING"})
    assert p == ["the code reads CCBOARD_NEW_THING but it is neither in ENV_KEYS nor in INTERNAL (give it a reason)"]
    p = problems(keys, rows | {"CCBOARD_OTHER_THING"}, reads)
    assert len(p) == 1 and "CCBOARD_OTHER_THING" in p[0]


def test_every_exception_carries_a_reason_and_none_is_stale():
    keys = set(env_keys())
    for d in (NOT_REMEMBERED, EXTRA_INTERNAL):
        assert all(isinstance(v, str) and len(v) > 10 for v in d.values()), "a reason, in words"
    assert not keys & set(NOT_REMEMBERED), "a remembered key is not an exception: " + ", ".join(sorted(keys & set(NOT_REMEMBERED)))
    assert not keys & set(INTERNAL), "a remembered key is not internal: " + ", ".join(sorted(keys & set(INTERNAL)))
    rows = readme_rows()
    assert not set(EXTRA_INTERNAL) - code_reads(), "EXTRA_INTERNAL names something the code no longer reads: " + ", ".join(sorted(set(EXTRA_INTERNAL) - code_reads()))
    assert not set(NOT_REMEMBERED) - rows, "NOT_REMEMBERED names a README row that no longer exists: " + ", ".join(sorted(set(NOT_REMEMBERED) - rows))


def test_internal_and_development_names_stay_out_of_the_installer_list():
    keys = set(env_keys())
    for n in ("CCBOARD_DEV_BYPASS_USER", "CCBOARD_HOOK_TOKEN_FILE", "CCBOARD_IMAGE_VERSION", "INVOCATION_ID", "PYTEST_CURRENT_TEST", "CCBOARD_URL"):
        assert n not in keys, f"{n} is development-only or internal and must not be remembered as a setting"
    text = README.read_text()
    assert "CCBOARD_DEV_BYPASS_USER" not in text[text.index("### Settings"):text.index("### What install.sh sets up")], "a development bypass is not a production setting"


# ------------------------------------------------------------------ install.sh binds every key before the env file is written
def test_every_env_key_is_bound_before_the_env_file_is_written():
    t = INSTALL.read_text()
    bind_end = t.index('env_body=""')
    head = "\n".join(l for l in t[:bind_end].splitlines() if not l.startswith("ENV_KEYS="))
    for k in env_keys():
        assert re.search(rf'(: "\$\{{{k}:[=-]|^\s*{k}=|\b{k}=\$\(|\bif \[ -z "\$\{{{k}:-\}}" \]; then {k}=)', head, re.M), f"{k} is not bound before the env file is written (set -u)"


def test_a_set_u_harness_prints_every_env_key_from_the_binding_section():
    """Run the binding lines themselves under `set -u` the way the script does, with nothing set, and read every ENV_KEYS name back."""
    t = INSTALL.read_text()
    lines = t.splitlines()
    first = next(i for i, l in enumerate(lines) if l.startswith(': "${PROJECTS_DIR:='))
    last = max(i for i, l in enumerate(lines) if re.match(r': "\$\{[A-Z_]+:=', l[:60]))
    binding = "\n".join(l for l in lines[first:last + 1] if re.match(r': "\$\{[A-Z_]+:=', l) and "$(" not in l)
    keys = env_keys()
    # three names are bound by their own lines (a generated token, `hostname -s`, the tailnet address), not by a `: "${X:=...}"` default
    script = ("set -u\nHOME_DIR=/tmp/home\nCCBOARD_HUB_TOKEN=x CCBOARD_NODE_NAME=n CCBOARD_PUBLIC_URL=u\n" + binding + "\n"
              + "".join(f': "${{{k}}}"\n' for k in keys) + "echo bound\n")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0 and "bound" in r.stdout, r.stderr


@pytest.mark.parametrize("name", ["CCBOARD_AUTOCLOSE_GRACE", "CCBOARD_CODEX_HOOKS_ASYNC", "CCBOARD_CLAUDE_ULTRACODE_FLAG"])
def test_the_names_added_by_this_issue_keep_the_defaults_the_app_already_used(name):
    t = INSTALL.read_text()
    default = {"CCBOARD_AUTOCLOSE_GRACE": "45", "CCBOARD_CODEX_HOOKS_ASYNC": "1", "CCBOARD_CLAUDE_ULTRACODE_FLAG": ""}[name]
    assert f': "${{{name}:={default}}}"' in t
    assert name in env_keys()


# ------------------------------------------------------------------ the installer's config section remembers a new key and a rerun keeps it
def _config_section():
    t = INSTALL.read_text()
    a = t.index("declare -A CALLER")
    b = t.index('systemd-analyze calendar "$CCBOARD_BACKUP_ONCALENDAR"')      # needs systemd; everything bound before it is what the env file takes
    return t[a:b]


def _bash4():
    """install.sh uses an associative array, so it needs bash 4 or newer (Ubuntu has it; the macOS /bin/bash is 3.2)."""
    import shutil
    for cand in (shutil.which("bash"), "/opt/homebrew/bin/bash", "/usr/local/bin/bash", "/bin/bash"):
        if cand and Path(cand).exists():
            r = subprocess.run([cand, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip().isdigit() and int(r.stdout) >= 4:
                return cand
    return None


needs_bash4 = pytest.mark.skipif(_bash4() is None, reason="needs bash 4 (associative arrays)")


def _run_config(tmp_path, env):
    """The config section (explicit env beats the env file beats defaults) followed by the env-file body the script writes, in a temp dir."""
    envfile = tmp_path / "etc-ccboard-env"
    keys = env_keys()
    script = ("set -euo pipefail\nnote() { :; }\ndie() { echo \"die: $*\" >&2; exit 1; }\n"
              f"ENV_FILE={envfile}\nHOME_DIR={tmp_path}/home\nENV_KEYS=({' '.join(keys)})\n" + _config_section()
              + 'CCBOARD_PUBLIC_URL=${CCBOARD_PUBLIC_URL:-https://box.example.ts.net}\n'
              + 'env_body=""\nfor k in "${ENV_KEYS[@]}"; do env_body+="$k=${!k}"$\'\\n\'; done\n'
              + 'for line in "${EXTRA_ENV[@]:-}"; do [ -n "$line" ] && env_body+="$line"$\'\\n\'; done\n'
              + f'printf %s "$env_body" > {envfile}\n')
    r = subprocess.run([_bash4(), "-c", script], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", **env})
    assert r.returncode == 0, r.stderr
    return dict(l.split("=", 1) for l in envfile.read_text().splitlines() if "=" in l)


@needs_bash4
def test_a_new_key_is_remembered_and_a_rerun_without_it_keeps_it(tmp_path):
    first = _run_config(tmp_path, {})
    assert first["CCBOARD_AUTOCLOSE_GRACE"] == "45" and first["CCBOARD_CODEX_HOOKS_ASYNC"] == "1" and first["CCBOARD_CLAUDE_ULTRACODE_FLAG"] == ""
    second = _run_config(tmp_path, {"CCBOARD_AUTOCLOSE_GRACE": "90", "CCBOARD_CODEX_HOOKS_ASYNC": "0", "CCBOARD_CLAUDE_ULTRACODE_FLAG": "1"})
    assert (second["CCBOARD_AUTOCLOSE_GRACE"], second["CCBOARD_CODEX_HOOKS_ASYNC"], second["CCBOARD_CLAUDE_ULTRACODE_FLAG"]) == ("90", "0", "1")
    third = _run_config(tmp_path, {})
    assert (third["CCBOARD_AUTOCLOSE_GRACE"], third["CCBOARD_CODEX_HOOKS_ASYNC"], third["CCBOARD_CLAUDE_ULTRACODE_FLAG"]) == ("90", "0", "1"), "a rerun keeps it"
    fourth = _run_config(tmp_path, {"CCBOARD_AUTOCLOSE_GRACE": "30"})
    assert fourth["CCBOARD_AUTOCLOSE_GRACE"] == "30" and fourth["CCBOARD_CODEX_HOOKS_ASYNC"] == "0", "an explicit value beats the remembered one, the rest stay"


@needs_bash4
def test_no_existing_default_changed(tmp_path):
    got = _run_config(tmp_path, {})
    for k, v in {"PROJECTS_DIR": "/srv/projects", "CCBOARD_PORT": "8000", "TTYD_PORT": "7681", "CODE_SERVER_PORT": "8080", "CCBOARD_HTTPS_PORT": "443",
                 "CODE_HTTPS_PORT": "8443", "NTFY_PORT": "2586", "NTFY_HTTPS_PORT": "8444", "NTFY_TOPIC": "ccboard", "CCBOARD_APPROVE_TIMEOUT": "90",
                 "PREVIEW_HTTPS_BASE": "9100", "CCBOARD_RUNTIME": "systemd", "CCBOARD_AUTO_CONTINUE": "1", "CCBOARD_CLAUDE_MEM": "1",
                 "CCBOARD_MEM_PORT": "", "CCBOARD_MEM_HTTPS_PORT": "", "CCBOARD_MEM_SERVICE": "0", "CCBOARD_CODEX_HOOK_TRUST": "review",
                 "CCBOARD_MCP_REMOTE": "0", "CODEX_HOME": "", "CODE_SERVER_VERSION": "4.139.1", "CCBOARD_BACKUP_PUSH": "1"}.items():
        assert got[k] == v, k


@needs_bash4
def test_the_mem_viewer_off_word_clears_the_remembered_port(tmp_path):
    assert _run_config(tmp_path, {"CCBOARD_MEM_HTTPS_PORT": "10443"})["CCBOARD_MEM_HTTPS_PORT"] == "10443"
    assert _run_config(tmp_path, {})["CCBOARD_MEM_HTTPS_PORT"] == "10443", "remembered"
    assert _run_config(tmp_path, {"CCBOARD_MEM_HTTPS_PORT": "off"})["CCBOARD_MEM_HTTPS_PORT"] == "", "off clears it"
