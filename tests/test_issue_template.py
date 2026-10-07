"""The issue template keeps the nine sections and the dispatch block shapes."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / ".github" / "ISSUE_TEMPLATE" / "task.md"
HEADINGS = ["Goal", "Why / evidence", "Scope", "Files", "Tests", "Acceptance",
            "Who should do it", "Rules", "References"]


def _body():
    return TEMPLATE.read_text()


def _outside_comments(text):
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def test_nine_headings_in_order():
    heads = re.findall(r"^## (.+)$", _outside_comments(_body()), flags=re.M)
    assert heads[:9] == HEADINGS
    assert heads[9] == "UI/UX acceptance: ccboard 10x"


def test_who_block_line_prefixes():
    text = _outside_comments(_body())
    block = text.split("## Who should do it")[1].split("\n## ")[0]
    assert re.search(r"^- Claude: `claude .+`$", block, flags=re.M)
    assert re.search(r"^- Codex: `codex .+`$", block, flags=re.M)
    assert re.search(r"^- Why this tier:", block, flags=re.M)
    assert re.search(r"^- Cheaper parts:", block, flags=re.M)


def test_comments_do_not_confuse_the_parser():
    for c in re.findall(r"<!--.*?-->", _body(), flags=re.S):
        assert not re.search(r"^- (Claude|Codex):", c, flags=re.M)
        assert "## Who should do it" not in c


def test_uiux_skeleton():
    raw = _body()
    assert "<!-- ccboard-uiux-v1:start -->" in raw
    assert "<!-- ccboard-uiux-v1:end -->" in raw
    for f in ["Applicability", "Current state", "User outcome", "Surface and scope",
              "Dependencies", "Source of truth"]:
        assert f"**{f}:**" in raw
    checks = re.findall(r"^- \[ \] \*\*(\w+):\*\*", raw.split("ccboard-uiux-v1:start")[1], flags=re.M)
    assert checks[-1] == "Evidence"


def test_no_emails_or_home_paths():
    for f in [TEMPLATE, ROOT / ".github" / "pull_request_template.md", ROOT / "CONTRIBUTING.md"]:
        t = f.read_text()
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", t), f
        assert not re.search(r"/(Users|home)/\w+", t), f


def test_config_and_pr_template():
    assert "blank_issues_enabled: true" in (ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text()
    pr = (ROOT / ".github" / "pull_request_template.md").read_text()
    for needle in ["pytest -q", "node --test", "1280", "390", "One PR per phase", "No personal data"]:
        assert needle in pr


def test_who_block_parses_with_the_board_parser():
    from app import issues
    who = issues.parse_who(_body())            # the template's own comments (tier table) must not leak in
    assert who["claude"] == {"model": "sonnet", "effort": "medium", "permission_mode": "acceptEdits"}
    assert who["codex"] == {"model": "gpt-6.1-sol", "reasoning": "medium", "sandbox": "workspace-write", "approval": "on-request"}
    assert who["default_agent"] == "claude" and who["warnings"] == []


def test_tier_table_matches_contributing():
    rows = [l for l in (ROOT / "CONTRIBUTING.md").read_text().splitlines() if l.startswith("| ")]
    assert len(rows) == 5
    tpl = _body()
    for r in rows:
        assert r in tpl
