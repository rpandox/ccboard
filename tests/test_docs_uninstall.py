"""The uninstall page held against the installers (issue #244, slice 3): every unit, timer, file and job that install.sh,
scripts/install-macos.sh and scripts/uninstall-macos.sh make or remove is named in docs/uninstall.md, with how to remove it.
Text checks only: no tools, no network."""
import re

from tests.docs_util import ROOT, UNINSTALL, read_doc


def fenced(text):
    """The lines inside ``` fences."""
    out, inside = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
        elif inside:
            out.append(line)
    return out




# ---------------------------------------------------------------- uninstall

def test_every_unit_and_timer_the_installer_writes_is_in_the_uninstall_page():
    page = read_doc(UNINSTALL)
    install = read_doc("install.sh")
    names = set(re.findall(r"\bccboard[a-z-]*\.(?:service|timer)\b", install))
    names |= {p.name.removesuffix(".in") for p in (ROOT / "systemd").glob("*.in")}
    assert {"ccboard.service", "ccboard-ttyd.service", "ccboard-tmux.service", "ccboard-backup.service", "ccboard-backup.timer",
            "ccboard-mem.service"} <= names
    missing = sorted(n for n in names if n not in page)
    assert not missing, f"docs/uninstall.md does not name: {missing}"


def test_the_files_the_installer_writes_are_in_the_uninstall_page():
    page = read_doc(UNINSTALL)
    install = read_doc("install.sh")
    written = {                     # the page's spelling -> the spelling in install.sh
        "/etc/sudoers.d/ccboard": "/etc/sudoers.d/ccboard",
        "/etc/ccboard/env": "/etc/ccboard/env",
        "/etc/ntfy/server.yml": "/etc/ntfy/server.yml",
        "/etc/apt/sources.list.d/ntfy.list": "/etc/apt/sources.list.d/ntfy.list",
        "/etc/apt/keyrings/ntfy.gpg": "/etc/apt/keyrings/ntfy.gpg",
        "/usr/local/bin/ttyd": "/usr/local/bin/ttyd",
        "docker.service.d/ccboard.conf": "docker.service.d/ccboard.conf",
        "code-server@$USER.service.d/ccboard.conf": 'code-server@$USER_NAME.service.d"',
    }
    for path, in_install in written.items():
        assert in_install in install, f"install.sh no longer writes {path}"
        assert path in page, f"docs/uninstall.md does not name {path}"
    assert "~/.config/code-server/config.yaml" in page and "# managed by ccboard" in page


def test_every_script_the_page_runs_has_a_remove_action_and_the_page_uses_it():
    page = read_doc(UNINSTALL)
    for script in ("claude_settings.py", "codex_hooks.py", "code_server_settings.py"):
        assert '"remove"' in read_doc(f"scripts/{script}"), script
        assert f"scripts/{script} remove" in page, script
    assert "claude mcp remove --scope user ccboard" in page and "codex mcp remove ccboard" in page
    assert "claude mcp add --scope user ccboard" in read_doc("install.sh")


def test_the_serve_mappings_are_turned_off_by_path_and_never_reset():
    page = read_doc(UNINSTALL)
    assert "tailscale serve status" in page.split("**Before step 1**", 1)[1]
    commands = [ln for ln in fenced(page) if "tailscale serve" in ln]
    assert commands and not [ln for ln in commands if re.search(r"\breset\b", ln)], commands
    assert all("--set-path" in ln or "status" in ln for ln in commands if "--https" in ln or "status" in ln), commands
    assert "never run `tailscale serve reset`" in page
    for ports in ("CCBOARD_HTTPS_PORT", "CODE_HTTPS_PORT", "NTFY_HTTPS_PORT", "CCBOARD_MEM_HTTPS_PORT", "PREVIEW_HTTPS_BASE"):
        assert ports in page, ports


def test_ending_the_tmux_server_is_a_warned_last_step():
    page = read_doc(UNINSTALL)
    systemd = page.split("## Linux with the systemd runtime", 1)[1].split("## Linux with the container runtime", 1)[0]
    tmux_step = systemd.index("sudo systemctl disable --now ccboard-tmux")
    assert "ends every agent session" in systemd[:tmux_step].rsplit("\n12. ", 1)[-1]
    assert systemd.index("sudo rm /etc/sudoers.d/ccboard") < tmux_step < systemd.index("rm -r ~/.local/share/ccboard")
    assert "disable --now ccboard-tmux" not in systemd[:tmux_step]
    assert "kill-server" in read_doc("systemd/ccboard-tmux.service.in")


def test_every_launchd_job_is_named_with_what_the_mac_script_does():
    page = read_doc(UNINSTALL)
    macos = page.split("\n## macOS\n", 1)[1].split("\n## Windows with WSL2", 1)[0]
    labels = {p.name.removesuffix(".plist.in") for p in (ROOT / "launchd").glob("dev.ccboard.*.plist.in")}
    assert len(labels) == 7
    assert not [label for label in labels if label not in macos], labels
    script = read_doc("scripts/uninstall-macos.sh")
    for flag in ("--yes", "--keep-tmux"):
        assert flag in script and flag in macos
    assert "scripts/uninstall-macos.sh" in macos and "tailscale_serve.py off" in macos


def test_docker_down_is_without_volumes_and_the_page_says_why():
    page = read_doc(UNINSTALL)
    docker = page.split("## Linux with the container runtime", 1)[1].split("\n## macOS", 1)[0]
    down = [ln for ln in fenced(docker) if " down" in ln]
    assert down and all(" -v" not in ln and "--volumes" not in ln for ln in down), down
    assert "`-v`" in docker and "no named volume" in docker
    assert not re.search(r"^volumes:", read_doc("deploy/docker-compose.yml"), re.M)
    assert "VOLUME" not in read_doc("Dockerfile")
    assert "ccboard-watchdog" in docker and "ccboard-watchdog" in read_doc("install.sh")


def test_the_uninstall_page_has_the_ids_the_other_pages_link_to():
    page = read_doc(UNINSTALL)
    assert "\n## Uninstall\n" in page
    assert page.splitlines()[0] == "# Uninstall ccboard"


def test_docker_backup_comes_before_down_because_it_runs_in_the_container():
    page = read_doc(UNINSTALL)
    install = read_doc("install.sh")
    assert 'BACKUP_EXEC="$DOCKER_BIN exec ccboard' in install and "-m app.backup" in install
    top = page.split("### What the installer made", 1)[0]
    assert "back up" in top.lower() and "before anything else" in top and "docker exec ccboard" in top
    assert top.index("Linux step 1") < top.index("the Docker section")
    systemd = page.split("## Linux with the systemd runtime", 1)[1].split("## Linux with the container runtime", 1)[0]
    assert "do step 1 now, then the Docker section" in systemd
    assert "do the Docker section first" not in page
    docker = page.split("## Linux with the container runtime", 1)[1].split("\n## macOS", 1)[0]
    assert docker.index("Linux step 1") < docker.index("docker compose")
    assert "# before `down`" in docker and docker.index("# before `down`") < docker.index("docker compose")
    assert "restic-password" in docker


def test_code_server_is_stopped_only_when_ccboard_owns_it_and_the_old_config_is_restored():
    page = read_doc(UNINSTALL)
    install = read_doc("install.sh")
    assert 'sudo systemctl enable --now "code-server@$USER_NAME"' in install
    assert "$cs_cfg.ccboard-bak-$(date +%s)" in install and "CCBOARD_REPLACE_CODE_SERVER_CONFIG" in install
    systemd = page.split("## Linux with the systemd runtime", 1)[1]
    step2 = systemd.split("\n2. ", 1)[1].split("\n3. ", 1)[0]
    assert "installing code-server" in step2 and "sudo systemctl enable --now code-server@$USER" in step2
    step5 = systemd.split("\n5. ", 1)[1].split("\n6. ", 1)[0]
    assert "config.yaml.ccboard-bak-" in step5 and "restore" in step5 and "rm ~/.config/code-server/config.yaml" in step5
    assert step5.index("config.yaml.ccboard-bak-") < step5.index("rm ~/.config/code-server/config.yaml")


def test_the_tailscale_operator_is_restored_not_just_cleared():
    page = read_doc(UNINSTALL)
    docker = page.split("## Linux with the container runtime", 1)[1].split("\n## macOS", 1)[0]
    assert "tailscale set --operator=" in read_doc("install.sh")
    assert "overwrote any operator" in docker and "--operator=<that user>" in docker and "to verify" in docker
