"""Container delivery files (v0.5.1-docker): Dockerfile, .dockerignore, entrypoint, compose templates.

No Docker daemon is needed. The entrypoint is executed for real in a sandbox (fake id/tmux/claude/uvicorn on PATH, a
temp HOME), so the sync, the hook merge, the shadow mode and the root refusal are tested by what they do."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

try:
    import yaml
except ImportError:  # PyYAML is not a runtime dependency; the textual checks below always run
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
ENTRYPOINT = ROOT / "scripts" / "docker-entrypoint.sh"
COMPOSE = ROOT / "deploy" / "docker-compose.yml"
SHADOW = ROOT / "deploy" / "docker-compose.shadow.yml"
DEPLOY_README = ROOT / "deploy" / "README.md"


# ---------------------------------------------------------------- Dockerfile
def instructions() -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments) with line continuations joined and comments dropped."""
    out, buf = [], ""
    for raw in DOCKERFILE.read_text().splitlines():
        line = raw.strip()
        if line.startswith("#") or (not line and not buf):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        word, _, rest = buf.partition(" ")
        out.append((word.upper(), rest.strip()))
        buf = ""
    return out


def of(kind: str) -> list[str]:
    return [rest for word, rest in instructions() if word == kind]


def test_dockerfile_base_and_final_user():
    ins = instructions()
    assert [r for w, r in ins if w == "FROM"] == ["ubuntu:24.04"]
    users = of("USER")
    assert users and users[-1] == "1000:1000"
    last_user = max(i for i, (w, _) in enumerate(ins) if w == "USER")
    # nothing after the last USER line may do root work
    assert not [w for w, _ in ins[last_user + 1:] if w in ("RUN", "COPY", "ADD")]


def test_dockerfile_healthcheck_labels_entrypoint():
    hc = of("HEALTHCHECK")
    assert len(hc) == 1 and "curl" in hc[0] and "/healthz" in hc[0] and "${CCBOARD_PORT:-8000}" in hc[0]
    labels = " ".join(of("LABEL"))
    assert 'org.opencontainers.image.source="https://github.com/rpandox/ccboard"' in labels
    assert "org.opencontainers.image.version=" in labels and "org.opencontainers.image.revision=" in labels
    assert of("ENTRYPOINT") == ['["/opt/ccboard/scripts/docker-entrypoint.sh"]']
    assert of("WORKDIR") == ["/opt/ccboard"]
    assert "CCBOARD_VERSION=dev" in of("ARG") and "CCBOARD_REVISION=unknown" in of("ARG")
    assert "CCBOARD_IMAGE_VERSION=${CCBOARD_VERSION}" in of("ENV")


def test_dockerfile_environment():
    text = DOCKERFILE.read_text()
    assert "DEBIAN_FRONTEND=noninteractive" in text
    env = " ".join(of("ENV"))
    for want in ("PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1", "LANG=C.UTF-8", "TMUX_TMPDIR=/tmp"):
        assert want in env, want
    assert not re.search(r"(^|\s)HOME=", env), "HOME is the host home path: compose sets it, the image must not"


def test_dockerfile_packages_and_users():
    runs = "\n".join(of("RUN"))
    for pkg in ("python3", "python3-venv", "python3-pip", "git", "tmux", "curl", "ca-certificates", "gnupg", "openssh-client",
                "iproute2", "procps", "restic", "sqlite3", "less"):
        assert re.search(rf"(\s){re.escape(pkg)}(\s|$)", runs), pkg
    assert "https://cli.github.com/packages" in runs and "githubcli-archive-keyring" in runs
    assert "deb.nodesource.com/node_22.x" in runs and re.search(r"ccusage@20\.\d+\.\d+(\s|$)", runs)
    assert "pkgs.tailscale.com/stable/ubuntu/noble" in runs and "install -y --no-install-recommends tailscale" in runs
    assert "userdel" in runs and "groupadd -g 1000 ccboard" in runs
    assert re.search(r"useradd -M -u 1000 -g 1000 -s /bin/bash -d /home/rpandox ccboard", runs)
    assert "python3 -m venv .venv" in runs and "pip install" in runs and "--require-hashes -r requirements.lock" in runs


def test_dockerfile_copies_and_cache_order():
    ins = instructions()
    copies = [r for w, r in ins if w == "COPY"]
    for src in ("requirements.lock", "app", "bin", "scripts", "tmux.conf"):
        assert any(c.startswith("--chown=1000:1000 ") and c.split()[1] == src for c in copies), src
    pip = next(i for i, (w, r) in enumerate(ins) if w == "RUN" and "pip install" in r)
    first_app = next(i for i, (w, r) in enumerate(ins) if w == "COPY" and r.split()[1] in ("app", "bin", "scripts", "tmux.conf"))
    apt = [i for i, (w, r) in enumerate(ins) if w == "RUN" and "apt-get" in r]
    assert max(apt) < pip < first_app, "apt layers, then python deps, then the app (cache order)"
    meta = next(i for i, (w, r) in enumerate(ins) if w == "ARG" and r.startswith("CCBOARD_VERSION"))
    assert meta > first_app, "the per-build version must not invalidate the layers above it"


def test_dockerignore():
    lines = {ln.strip() for ln in DOCKERIGNORE.read_text().splitlines() if ln.strip() and not ln.startswith("#")}
    for want in (".git", ".venv", "tests", ".gstack", ".claude", ".pytest_cache", "__pycache__", "**/__pycache__", "*.tar.gz",
                 "deploy", ".github", "ccboard.tar.gz"):
        assert want in lines, want
    for needed in ("app", "bin", "scripts", "tmux.conf", "requirements.lock", "scripts/docker-entrypoint.sh"):
        assert needed not in lines, f"{needed} is copied into the image and must not be ignored"
    assert "scripts/ci-smoke.sh" in lines, "the CI smoke script is a dev helper: it must not be synced to the host with scripts/"


def test_dockerfile_installs_are_pinned():
    """No unpinned install line: pip uses the hashed lock, npm an exact version."""
    runs = "\n".join(of("RUN"))
    for m in re.finditer(r"pip install[^&]*", runs):
        assert "--require-hashes" in m.group(0) and "-r requirements.lock" in m.group(0), m.group(0)
    for m in re.finditer(r"npm install[^&]*", runs):
        pkgs = [w for w in m.group(0).split()[2:] if not w.startswith("-")]
        assert pkgs and all(re.search(r"@\d+\.\d+\.\d+$", w) for w in pkgs), m.group(0)


# ---------------------------------------------------------------- entrypoint (static)
def test_entrypoint_static():
    text = ENTRYPOINT.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert os.access(ENTRYPOINT, os.X_OK), "git keeps the executable bit; chmod +x scripts/docker-entrypoint.sh"
    assert re.search(r"^set -eu$", text, re.M)
    assert re.search(r'\[ "\$\(id -u\)" -eq 0 \]', text) and "refusing to run as root" in text
    assert re.search(r"^exec \"?\S*uvicorn\"? app\.main:app --host 127\.0\.0\.1 --port \"\$CCBOARD_PORT\" --timeout-graceful-shutdown 2$", text, re.M)
    assert "CCBOARD_SHADOW" in text and "claude_settings.py" in text and "source-file" in text and "mcp add" in text
    assert "[[" not in text, "dash has no [[ ]]"


def test_entrypoint_unsets_an_empty_codex_home_before_any_codex_call():
    """/etc/ccboard/env supplies `CODEX_HOME=` (empty): host_codex must hand codex an unset variable, not an empty path, before it runs
    `codex mcp get/add/remove` (and the hooks installer). The guard is the first line of host_codex."""
    text = ENTRYPOINT.read_text()
    guard = '[ -n "${CODEX_HOME:-}" ] || unset CODEX_HOME'
    assert text.count(guard) == 1
    body = text.split("host_codex() {\n", 1)[1]
    assert body.lstrip().startswith(guard), "the first thing host_codex does"
    for use in ("command -v codex", "host_codex_hooks ||", "host_codex_mcp ||", "\n  host_codex\n"):   # every way into codex comes after it
        assert text.index(use, text.index("host_codex() {")) > text.index(guard), use


@pytest.mark.skipif(not shutil.which("bash"), reason="bash not installed")
def test_entrypoint_bash_n():
    r = subprocess.run(["bash", "-n", str(ENTRYPOINT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not shutil.which("sh"), reason="sh not installed")
def test_entrypoint_sh_n():
    r = subprocess.run(["sh", "-n", str(ENTRYPOINT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed")
def test_entrypoint_shellcheck():
    r = subprocess.run(["shellcheck", "-s", "sh", "--severity=warning", str(ENTRYPOINT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


# ---------------------------------------------------------------- entrypoint (executed in a sandbox)
def _exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


class Box:
    """A fake image dir (/opt/ccboard), a fake host home, and fake id/tmux/claude/uvicorn that log their calls."""

    def __init__(self, tmp: Path, uid: str = "1000", claude_registered: bool = False, tmux_rc: int = 0, claude_rc: int = 0):
        self.home = tmp / "home"
        self.home.mkdir()
        self.app = tmp / "image"
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(ROOT / "bin", self.app / "bin", ignore=ignore)
        shutil.copytree(ROOT / "scripts", self.app / "scripts", ignore=ignore)
        shutil.copy(ROOT / "tmux.conf", self.app / "tmux.conf")
        self.log = tmp / "calls.log"
        q = shlex.quote(str(self.log))
        fakes = tmp / "fakes"
        _exe(fakes / "id", f"echo {uid}\n")
        _exe(fakes / "tmux", f'echo "tmux $*" >> {q}\nexit {tmux_rc}\n')
        _exe(fakes / "timeout", 'shift\nexec "$@"\n')   # GNU timeout is not on macOS; the entrypoint's use of it is covered either way
        get_rc = 0 if claude_registered else 1
        _exe(fakes / "claude", f'echo "claude $*" >> {q}\n[ "$1 $2" = "mcp get" ] && exit {get_rc}\nexit {claude_rc}\n')
        _exe(self.app / ".venv" / "bin" / "uvicorn",
             f'echo "uvicorn $* | cwd=$(pwd) port=$CCBOARD_PORT data=$CCBOARD_DATA_DIR" >> {q}\n')
        self.fakes = fakes
        self.data = self.home / ".local" / "share" / "ccboard"

    def run(self, **extra: str) -> subprocess.CompletedProcess:
        env = {"PATH": f"{self.fakes}:/usr/bin:/bin", "HOME": str(self.home), "CCBOARD_APP_ROOT": str(self.app),
               "CCBOARD_HOST_PYTHON": sys.executable, "CCBOARD_PORT": "8123", "LANG": "C"}
        env.update(extra)
        return subprocess.run([str(ENTRYPOINT)], env=env, capture_output=True, text=True, timeout=60)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def settings(self) -> dict:
        return json.loads((self.home / ".claude" / "settings.json").read_text())


def hook_commands(settings: dict) -> set[str]:
    return {h["command"] for groups in settings.get("hooks", {}).values() for g in groups for h in g["hooks"]}


def test_entrypoint_syncs_merges_registers_and_execs(tmp_path):
    box = Box(tmp_path)
    stale = box.data / "app" / "bin" / "ccboard-old-wrapper"
    stale.parent.mkdir(parents=True)
    stale.write_text("old")
    r = box.run()
    assert r.returncode == 0, r.stderr
    app = box.data / "app"
    for rel in ("bin/ccboard-hook", "bin/ccboard-hook-fast", "bin/ccboard-attach", "bin/ccboard-permission", "bin/ccboard-statusline"):
        assert (app / rel).stat().st_mode & 0o777 == 0o755, rel
    assert (app / "scripts" / "claude_settings.py").is_file() and (app / "scripts" / "ccboard_mcp.py").is_file()
    assert (app / "tmux.conf").read_text() == (ROOT / "tmux.conf").read_text()
    assert not stale.exists(), "files the image no longer ships are pruned"
    assert not [p for p in app.rglob(".*.new")], "no half-copied temp files left behind"
    cmds = hook_commands(box.settings())
    assert str(app / "bin" / "ccboard-hook") in cmds and str(app / "bin" / "ccboard-permission") in cmds
    assert box.settings()["statusLine"]["command"] == str(app / "bin" / "ccboard-statusline")
    calls = box.calls()
    assert f"tmux -L ccboard source-file {app / 'tmux.conf'}" in calls
    assert "claude mcp get ccboard" in calls
    assert f"claude mcp add --scope user ccboard -- {sys.executable} {app / 'scripts' / 'ccboard_mcp.py'}" in calls
    uv = [c for c in calls if c.startswith("uvicorn ")]
    assert len(uv) == 1
    argv, _, ctx = uv[0].partition(" | ")
    assert argv == "uvicorn app.main:app --host 127.0.0.1 --port 8123 --timeout-graceful-shutdown 2"
    cwd = re.search(r"cwd=(\S+)", ctx).group(1)
    assert os.path.samefile(cwd, box.app) and f"port=8123 data={box.data}" in ctx


def test_entrypoint_is_idempotent_and_skips_registered_mcp(tmp_path):
    box = Box(tmp_path, claude_registered=True)
    assert box.run().returncode == 0
    first = (box.home / ".claude" / "settings.json").read_text()
    assert box.run().returncode == 0
    assert (box.home / ".claude" / "settings.json").read_text() == first
    assert not [c for c in box.calls() if c.startswith("claude mcp add")]


def test_entrypoint_remote_approve_off(tmp_path):
    off = tmp_path / "off"
    off.mkdir()
    box = Box(off)
    assert box.run(CCBOARD_REMOTE_APPROVE="0").returncode == 0
    assert "PermissionRequest" not in box.settings()["hooks"]
    on = tmp_path / "on"
    on.mkdir()
    box2 = Box(on)
    assert box2.run(CCBOARD_APPROVE_TIMEOUT="45").returncode == 0
    perm = [h for g in box2.settings()["hooks"]["PermissionRequest"] for h in g["hooks"]]
    assert perm and perm[0]["timeout"] == 75


def test_entrypoint_shadow_touches_no_host_state(tmp_path):
    box = Box(tmp_path)
    shadow = box.home / ".local" / "share" / "ccboard-shadow"
    r = box.run(CCBOARD_SHADOW="1", CCBOARD_DATA_DIR=str(shadow))
    assert r.returncode == 0, r.stderr
    assert not (box.home / ".claude").exists(), "shadow must not merge hooks into ~/.claude"
    calls = box.calls()
    assert not [c for c in calls if c.startswith(("tmux ", "claude "))], calls
    assert (shadow / "app" / "bin" / "ccboard-hook").is_file() and (shadow / "app" / "tmux.conf").is_file()
    assert [c for c in calls if c.startswith("uvicorn ")] and f"data={shadow}" in calls[-1]
    assert not box.data.exists(), "the live data dir is not created or written"


def test_entrypoint_failures_never_block_the_board(tmp_path):
    box = Box(tmp_path, tmux_rc=1, claude_rc=1)
    r = box.run(CCBOARD_HOST_PYTHON=str(tmp_path / "no-such-python"))
    assert r.returncode == 0, r.stderr
    assert "warning" in r.stderr
    assert [c for c in box.calls() if c.startswith("uvicorn ")]


def test_entrypoint_starts_the_board_before_the_slow_host_steps_finish(tmp_path):
    """#39: the host steps (hooks merge, tmux.conf, `claude mcp get/add`, Codex) run beside the board, so a swap's outage does not
    wait on a Node CLI start under disk pressure. The board execs first; the steps still run to the end and log when done."""
    box = Box(tmp_path)
    q = shlex.quote(str(box.log))
    _exe(box.fakes / "claude", f'echo "claude $* (start)" >> {q}\nsleep 1\necho "claude $* (end)" >> {q}\n'
                               f'[ "$1 $2" = "mcp get" ] && exit 1\nexit 0\n')
    r = box.run()
    assert r.returncode == 0, r.stderr
    calls = box.calls()
    uv = next(i for i, c in enumerate(calls) if c.startswith("uvicorn "))
    get_end = calls.index("claude mcp get ccboard (end)")
    assert uv < get_end, f"uvicorn is started before the 1 s `claude mcp get` ends: {calls}"
    assert any(c.startswith("claude mcp add --scope user ccboard") and c.endswith("(end)") for c in calls), "and the registration still completes"
    assert "host steps done" in r.stdout
    assert str(box.data / "app" / "bin" / "ccboard-hook") in hook_commands(box.settings()), "the hooks merge still lands"
    sync = r.stdout.index("synced bin, scripts and tmux.conf")
    assert sync < r.stdout.index("host steps done"), "step 1 (the sync the host's hooks run from) still comes first"


def test_entrypoint_refuses_root(tmp_path):
    box = Box(tmp_path, uid="0")
    r = box.run()
    assert r.returncode != 0
    assert "refusing to run as root" in r.stderr
    assert box.calls() == [] and not box.data.exists(), "nothing runs and nothing is written as root"


# ---------------------------------------------------------------- compose
def texts() -> dict[str, str]:
    return {p.name: p.read_text() for p in (COMPOSE, SHADOW)}


def test_no_https_443_anywhere():
    # 443 is the CouchDB Funnel on the box: nothing in the delivery files may name it (8443 and friends included).
    for p in (COMPOSE, SHADOW, DOCKERFILE, ENTRYPOINT):
        assert "443" not in p.read_text(), p.name


def test_compose_files_are_plain_yaml_text():
    for name, text in texts().items():
        assert "\t" not in text and text.startswith("#") and "\nservices:\n" in text, name


@pytest.mark.skipif(yaml is None, reason="PyYAML not installed; the textual and docker compose checks cover the same ground")
def test_compose_parses_with_pyyaml():
    base = yaml.safe_load(COMPOSE.read_text())
    shadow = yaml.safe_load(SHADOW.read_text())
    assert base["name"] == "ccboard" and set(base["services"]) == {"ccboard", "watchtower"}
    assert shadow["name"] == "ccboard-shadow" and set(shadow["services"]) == {"ccboard"}
    svc = base["services"]["ccboard"]
    assert svc["network_mode"] == "host" and svc["pid"] == "host" and svc["restart"] == "unless-stopped"
    env_files = [e["path"] if isinstance(e, dict) else e for e in svc["env_file"]]
    assert env_files == ["/etc/ccboard/env"]
    wt = base["services"]["watchtower"]
    assert wt["profiles"] == ["prod"] and wt["image"] == "nickfedor/watchtower:1"
    assert "com.centurylinklabs.watchtower.enable" not in wt["labels"], "watchtower must never update itself"
    assert wt["labels"]["com.centurylinklabs.watchtower.scope"] == "ccboard"


def test_compose_ccboard_service_text():
    text = COMPOSE.read_text()
    assert re.search(r"^\s+network_mode: host$", text, re.M) and re.search(r"^\s+pid: host$", text, re.M)
    assert 'com.centurylinklabs.watchtower.enable: "true"' in text
    assert "com.centurylinklabs.watchtower.scope: ccboard" in text
    assert "path: /etc/ccboard/env" in text
    assert "image: ghcr.io/rpandox/ccboard:${CCBOARD_IMAGE_TAG:-latest}" in text
    assert 'user: "${CCBOARD_UID:-1000}:${CCBOARD_GID:-1000}"' in text
    assert "CCBOARD_RUNTIME: docker" in text and "TMUX_TMPDIR: /tmp" in text
    for var in ("CCBOARD_HOME:-/home/rpandox", "PROJECTS_DIR:-/srv/projects"):
        assert "${" + var + "}" in text, var
    for target in ("/tmp/tmux-${CCBOARD_UID:-1000}", "/var/run/tailscale", "/etc/ccboard"):
        assert f"target: {target}" in text, target
    assert re.search(r"target: /etc/ccboard\n\s+read_only: true", text)
    assert text.count("create_host_path: false") >= 5, "a missing source must fail, not become a root-owned directory"
    # $$ so the container's own CCBOARD_PORT (env_file, shadow override) is used, not compose's
    assert "curl -fs http://127.0.0.1:$${CCBOARD_PORT:-8000}/healthz" in text
    assert "ports:" not in text, "host network: no published ports"


def test_compose_watchtower_is_label_and_scope_limited():
    text = COMPOSE.read_text()
    m = re.search(r"^  watchtower:\n(.*)", text, re.M | re.S)
    assert m, "watchtower service missing"
    block = m.group(1)
    assert "com.centurylinklabs.watchtower.enable" not in block
    assert "com.centurylinklabs.watchtower.scope: ccboard" in block
    cmd = re.search(r"^    command: (.+)$", block, re.M).group(1)
    assert "--label-enable" in cmd and "--scope ccboard" in cmd and "--interval 300" in cmd and "--cleanup" in cmd
    assert 'profiles: ["prod"]' in block
    assert "/var/run/docker.sock:/var/run/docker.sock" in block and "target: /config.json" in block


def test_shadow_override_text():
    text = SHADOW.read_text()
    assert "container_name: ccboard-shadow" in text
    assert "CCBOARD_PORT: ${CCBOARD_SHADOW_PORT:-8010}" in text
    assert 'CCBOARD_SHADOW: "1"' in text
    assert "CCBOARD_DATA_DIR: ${CCBOARD_HOME:-/home/rpandox}/.local/share/ccboard-shadow" in text
    assert re.search(r"target: \$\{PROJECTS_DIR:-/srv/projects\}\n\s+read_only: true", text)
    assert 'com.centurylinklabs.watchtower.enable: "false"' in text
    assert not re.search(r"^  watchtower:", text, re.M), "no watchtower service in the shadow"
    assert 'restart: "no"' in text and "name: ccboard-shadow" in text


def _compose_version() -> tuple[int, int] | None:
    docker = shutil.which("docker")
    if not docker:
        return None
    try:
        r = subprocess.run([docker, "compose", "version", "--short"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"(\d+)\.(\d+)", r.stdout)
    return (int(m.group(1)), int(m.group(2))) if r.returncode == 0 and m else None


def _compose_config(*files: Path, profile: str | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "CCBOARD_")) and k != "PROJECTS_DIR"}
    env["CCBOARD_HOME"] = "/tmp/h"
    cmd = ["docker", "compose"]
    for f in files:
        cmd += ["-f", str(f)]
    if profile:
        cmd += ["--profile", profile]
    r = subprocess.run(cmd + ["config", "--format", "json"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.skipif((_compose_version() or (0, 0)) < (2, 24), reason="docker compose >= 2.24 not available")
@pytest.mark.real_home("the docker CLI finds its compose plugin under ~/.docker/cli-plugins; it only reads config, writes nothing")
def test_docker_compose_config_validates_and_merges():
    base = _compose_config(COMPOSE, profile="prod")
    assert set(base["services"]) == {"ccboard", "watchtower"}
    assert base["services"]["ccboard"]["network_mode"] == "host" and base["services"]["ccboard"]["pid"] == "host"
    assert base["services"]["ccboard"]["environment"]["CCBOARD_RUNTIME"] == "docker"
    assert set(_compose_config(COMPOSE)["services"]) == {"ccboard"}, "watchtower only runs with --profile prod"
    merged = _compose_config(COMPOSE, SHADOW)
    assert merged["name"] == "ccboard-shadow" and set(merged["services"]) == {"ccboard"}
    svc = merged["services"]["ccboard"]
    assert svc["container_name"] == "ccboard-shadow"
    assert svc["environment"]["CCBOARD_PORT"] == "8010" and svc["environment"]["CCBOARD_SHADOW"] == "1"
    assert svc["environment"]["CCBOARD_DATA_DIR"] == "/tmp/h/.local/share/ccboard-shadow"
    assert svc["labels"]["com.centurylinklabs.watchtower.enable"] == "false"
    projects = [v for v in svc["volumes"] if v["target"] == "/srv/projects"]
    assert len(projects) == 1 and projects[0].get("read_only") is True
    assert "$${CCBOARD_PORT" in " ".join(svc["healthcheck"]["test"])


def test_deploy_readme_names_every_file():
    text = DEPLOY_README.read_text()
    for p in sorted((ROOT / "deploy").glob("docker-compose*.yml")):
        assert p.name in text, p.name
    assert "Dockerfile" in text and "docker-entrypoint.sh" in text


def test_compose_deploy_gate_and_fast_stop():
    """A swap waits for the terminals (Watchtower pre-update hook -> scripts/ccboard-deploy-gate) and the stop is short."""
    text = COMPOSE.read_text()
    assert "com.centurylinklabs.watchtower.lifecycle.pre-update: /opt/ccboard/scripts/ccboard-deploy-gate" in text
    assert 'com.centurylinklabs.watchtower.lifecycle.pre-update-timeout: "1"' in text
    assert re.search(r"command: .*--enable-lifecycle-hooks", text)
    assert "stop_grace_period: 6s" in text and "stop_grace_period: 15s" not in text
    ep = (ROOT / "scripts" / "docker-entrypoint.sh").read_text()
    assert "--timeout-graceful-shutdown 2" in ep
    gate = ROOT / "scripts" / "ccboard-deploy-gate"
    assert gate.exists() and "exit 75" in gate.read_text()
