"""The docs say what the box checks of 2026-10-09 found (issues #12, #26, #62, #111; #104 is in tests/test_devcontainer.py).

Text checks only: no git, no network. A rewrite of these paragraphs has to keep the facts, or change the test with the new evidence.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def section(text, heading):
    """The text from `heading` to the next heading of the same or a higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    start = text.index(heading + "\n")
    body = start + len(heading)
    m = re.search(rf"^#{{1,{level}}} ", text[body:], re.M)
    return text[start:body + (m.start() if m else len(text))]


# ------------------------------------------------------------------ #111: the restore runbook

def test_the_readme_has_a_restore_runbook_with_the_drill_date_and_timings():
    text = read("README.md")
    assert "The restore runbook (issue #111) is not written yet" not in text
    s = section(text, "### Restoring from a backup")
    assert "2026-10-09" in s and "10 s" in s and "3 s" in s and "13 s" in s and "33 commits" in s and "758 MiB" in s
    for needle in ("restic-password", "restic check", "restic restore latest --target", "integrity_check", "Disaster restore",
                   "ccboard-backup/<node>/<branch>", "never over live data", "same disk"):
        assert needle in s, needle
    assert "Not drilled" in s, "the disaster-restore steps replace live data and were not run"
    assert "[Restoring from a backup](#restoring-from-a-backup)" in text


def test_the_shadow_section_no_longer_promises_what_the_old_overlay_did_not_keep():
    text = read("README.md")
    s = section(text, "### B. Shadow run (no sudo, nothing of the live board touched)")
    assert "nothing else touched" not in text
    for needle in ("deploy/docker-compose.shadow.yml", "alone", "bridge network with no published port", "no host pid", "not the tmux socket",
                   "docker exec ccboard-shadow curl"):
        assert needle in s, needle
    assert "-f deploy/docker-compose.yml -f deploy/docker-compose.shadow.yml" not in text, "it is a file of its own now"
    assert "-f docker-compose.yml -f docker-compose.shadow.yml" not in read("deploy/README.md")


# ------------------------------------------------------------------ #12: the memory API doc

def test_the_memory_api_doc_states_the_13_35_findings():
    doc = read("docs/memory-api.md")
    assert "UNVERIFIED" not in doc, "the palace's filter-only search was verified on 13.35.0"
    for needle in ('TESTED_WORKER = "13.35.0"', "ignores `q=` and searches for the text \"undefined\"", "`project` and `projects` both work",
                   "`format=json`", "`anchor`", "never both", "**100 at most**", "`<repo>/<slug>`", "`observations.agent_type`",
                   "HTTP 200 with `{projects, sources, projectsBySource}`", "7.4 s", "26.0 s", "6 s budget", "claude_mem_13_35"):
        assert needle in doc, needle


# ------------------------------------------------------------------ #26 and #62: the hooks section

def test_the_readme_says_why_the_worktree_hooks_stay_unregistered():
    text = read("README.md")
    hooks = section(text, "### Hooks v2 (v0.5.7)")
    assert "WorktreeCreate and WorktreeRemove are deliberately not registered, and they stay that way" in hooks
    assert "hook succeeded but returned no worktree path" in hooks and "**replaces**" in hooks and "never fired" in hooks and "2.1.294" in hooks
    assert "until V9" not in text and "until V9" not in read("scripts/claude_settings.py")


def test_the_guards_table_has_the_permission_hook_and_the_probe_result():
    text = read("README.md")
    guards = section(text, "### Which hooks the board ignores")
    table = [ln for ln in guards.splitlines() if ln.startswith("|")]
    assert len(table) == 2 + 5, "header, rule, four guards and the permission hook"
    assert table[-1].startswith("| Permission hook |") and "/api/permission" in table[-1] and '"behavior": null' in table[-1]
    assert "Probe result (box, Claude Code 2.1.294, 2026-10-09)" in guards
    assert "does fire the PermissionRequest hook" in guards and "`ignored: child`" in guards and "0.00 s" in guards
    assert "has not been verified on the box yet" not in text
