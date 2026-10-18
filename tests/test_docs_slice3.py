"""The pages of issue #244, slice 3: upgrade, troubleshooting, security, SECURITY.md, CONTRIBUTING.md and the deploying note.

Text checks only: no network, no tools. The uninstall page has its own file, tests/test_docs_uninstall.py."""
from tests.docs_util import SECURITY, read_doc


# ---------------------------------------------------------------- upgrade

def test_upgrade_page_covers_every_kind_of_install_and_keeps_the_linked_anchors():
    page = read_doc("docs/upgrade.md")
    for heading in ("## Updating a Linux install", "## Updating a Mac", "## Updating on WSL2", "## Updating the container", "## Going back",
                    "### Changelog", "### Back up first", "## Updating Claude Code"):
        assert heading + "\n" in page, heading
    assert "Upgrade: nothing to do." in page and "Upgrade: rerun ./install.sh." in page
    assert "git diff HEAD origin/main -- CHANGELOG.md" in page
    assert "Updating Codex on the box" in page


def test_update_text_moved_into_upgrade_is_not_kept_in_two_places():
    macos = read_doc("docs/install/macos.md")
    assert "is neither rewritten nor reloaded" not in macos and "docs/upgrade.md" not in macos and "(../upgrade.md#updating-a-mac)" in macos
    assert "is neither rewritten nor reloaded" in read_doc("docs/upgrade.md")
    deploying = read_doc("docs/maintainers/deploying.md")
    assert "--force-recreate" not in deploying and "(../upgrade.md#updating-the-container)" in deploying
    assert "--force-recreate" in read_doc("docs/upgrade.md")


# ---------------------------------------------------------------- troubleshooting

def test_troubleshooting_starts_with_the_symptom_table_then_the_details_then_the_faq():
    page = read_doc("docs/troubleshooting.md")
    table = page.index("| Symptom | Likely cause | What to do |")
    assert page.index("## Troubleshooting\n") < table < page.index("### Details") < page.index("## FAQ")
    rows = [ln for ln in page[table:page.index("### Where the logs are")].splitlines() if ln.startswith("| ") and not ln.startswith("| Symptom")]
    assert len(rows) >= 15 and all(ln.count(" | ") >= 2 for ln in rows), len(rows)
    first_column = " ".join(ln.split(" | ")[0].lower() for ln in rows)
    for need in ("403", "phone", "terminal page is blank", "permission prompts", "push notifications", "doctor", "tailscale serve port",
                 "tmux socket", "not logged in"):
        assert need in first_column, need
    for question in ("Do I need Tailscale?", "Can I use it from the machine it runs on?", "Does it need an API key?", "Is it multi-user?",
                     "What leaves my machine?", "Does it work offline?"):
        assert f"### {question}\n" in page, question


def test_the_troubleshooting_table_quotes_messages_the_code_really_prints():
    page = read_doc("docs/troubleshooting.md")
    install = read_doc("install.sh")
    doctor = read_doc("app/doctor.py")
    for message in ("tailscale serve port", "is already used by something else", "is in use and", "run interactively",
                    "Ubuntu 22.04 or 24.04 required", "HTTPS certificates are not enabled for this tailnet", "has no MagicDNS name"):
        assert message in install, message
        assert message in page, message
    for message in ("nothing listens on 127.0.0.1", "the board's hooks are not installed in Claude's settings.json", "no device is subscribed to Web Push",
                    "does not answer at", "no tmux server on socket", "is not logged in"):
        assert message in doctor, message
        assert message in page, message


# ---------------------------------------------------------------- security

def test_security_page_opens_with_who_can_reach_what_and_keeps_the_model_below():
    page = read_doc(SECURITY)
    head, model = page.split("\n## Security model\n", 1)
    assert "\n## Who can reach what\n" in head
    assert "| Surface | Who can reach it | How it is checked |" in head
    for surface in ("`/tty`", "code-server", "ntfy", "`/mcp`", "Local sign-in", "Paired boards", "Pairing", "Hooks and the statusline", "/healthz"):
        assert surface in head, surface
    assert "do not check the list of allowed logins" in head and "tailnet access rules only" in head.lower()
    assert model.startswith("\n- **Optional local sign-in") or model.startswith("- **Optional local sign-in")
    assert "../SECURITY.md" in head


def test_security_md_is_a_private_route_with_no_address():
    text = read_doc("SECURITY.md")
    assert "security/advisories/new" in text and "Report a vulnerability" in text
    assert "@" not in text, "no e-mail address in SECURITY.md"
    for heading in ("## Supported versions", "## How to report a vulnerability", "## What to include", "## What to expect"):
        assert heading in text, heading
    assert "docs/security.md" in text


# ---------------------------------------------------------------- contributing and deploying

def test_contributing_opens_with_the_plain_path_before_the_agent_tables():
    text = read_doc("CONTRIBUTING.md")
    path = text.index("## The short path")
    assert path < text.index("## For agents and the maintainer's workflow") < text.index("## Pick an issue") < text.index("| Tier | Claude | Codex |")
    human = text[path:text.index("## For agents and the maintainer's workflow")]
    assert not [ln for ln in human.splitlines() if ln.startswith("| ")], "the tier table stays the only table (tests/test_issue_template.py counts its rows)"
    for need in ("CCBOARD_TEST_NO_CLAUDE=1 .venv/bin/pytest -q", "node --test tests/js/*.test.mjs", "requirements-dev.lock", "?demo=1",
                 "CCBOARD_DEV_BYPASS_USER=dev", "## vX.Y.Z - YYYY-MM-DD", "Upgrade: nothing to do.", "pull request"):
        assert need in human, need
    assert read_doc("docs/development.md").count("CCBOARD_DEV_BYPASS_USER=dev .venv/bin/uvicorn app.main:app") >= 1


def test_deploying_says_first_that_ordinary_users_do_not_need_it():
    lines = read_doc("docs/maintainers/deploying.md").splitlines()
    assert lines[2].startswith("For the maintainer of a public deployment")
    assert "Ordinary users do not need this page" in "\n".join(lines[:10])
