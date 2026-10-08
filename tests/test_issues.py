"""Issues as dispatch input: the pure "Who should do it" parser, the origin owner and trust check, and the routes (a fake gh on PATH)."""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from app import issues, prpoll

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
FIX = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIX / name).read_text())


def block(*lines):
    return "## Who should do it\n\n" + "\n".join(lines) + "\n\n## Rules\n"


# ---- the parser -----------------------------------------------------------------------------------------------------
def test_full_block():
    who = issues.parse_who(fixture("issue_full.json")["body"])
    assert who["claude"] == {"model": "sonnet", "effort": "medium", "permission_mode": "acceptEdits"}
    assert who["codex"] == {"model": "gpt-6.1-sol", "reasoning": "medium", "sandbox": "workspace-write", "approval": "on-request"}
    assert who["default_agent"] == "claude" and who["warnings"] == []


def test_only_one_agent():
    who = issues.parse_who(block("- Codex: `codex -m gpt-6-luna -c model_reasoning_effort=\"low\" -s read-only`"))
    assert who["claude"] is None and who["default_agent"] == "codex"
    assert who["codex"] == {"model": "gpt-6-luna", "reasoning": "low", "sandbox": "read-only", "approval": None}
    who = issues.parse_who(block("- Claude Code: `claude --model haiku` in a worktree"))
    assert who["claude"]["model"] == "haiku" and who["codex"] is None


def test_equals_and_quoted_forms():
    who = issues.parse_who(block("- Claude: `claude --model=opus --effort \"high\" --permission-mode='plan'`"))
    assert who["claude"] == {"model": "opus", "effort": "high", "permission_mode": "plan"}
    who = issues.parse_who(block("- Codex: `codex --model=gpt-6.1-sol -c 'model_reasoning_effort=high' --sandbox=workspace-write --ask-for-approval=never`"))
    assert who["codex"] == {"model": "gpt-6.1-sol", "reasoning": "high", "sandbox": "workspace-write", "approval": "never"}


def test_unknown_flags_are_ignored():
    who = issues.parse_who(block("- Claude: `claude --model sonnet --add-dir /etc --mcp-config x.json --append-system-prompt hi -p`"))
    assert who["claude"] == {"model": "sonnet", "effort": None, "permission_mode": None}
    assert "/etc" not in json.dumps(who) and "mcp" not in json.dumps(who)
    who = issues.parse_who(block("- Codex: `codex -m x1 -c sandbox_mode=danger -c foo=bar --search`"))
    assert who["codex"] == {"model": "x1", "reasoning": None, "sandbox": None, "approval": None}
    assert who["warnings"] == []


@pytest.mark.parametrize("line", [
    "- Claude: `claude --model opus --permission-mode bypassPermissions`",
    "- Claude: `claude --model opus --permission-mode=bypassPermissions`",
    "- Claude: `claude --model opus --dangerously-skip-permissions`",
    "- Claude: `claude --model opus --allow-dangerously-skip-permissions`",
    "- Codex: `codex -m x1 -s danger-full-access`",
    "- Codex: `codex -m x1 --sandbox=danger-full-access`",
    "- Codex: `codex -m x1 --dangerously-bypass-approvals-and-sandbox`",
    "- Codex: `codex -m x1 --yolo`",
])
def test_bypass_spellings_are_dropped_with_a_warning(line):
    who = issues.parse_who(block(line))
    assert who["warnings"] == ["ignored a bypass setting"]
    text = json.dumps(who).lower()
    for bad in ("bypass", "dangerous", "danger-full", "yolo"):
        assert bad not in text.replace("ignored a bypass setting", "")


def test_no_block_is_empty():
    for body in (None, "", "no headings here", "## Goal\n\n- Claude: `claude --model opus`\n"):
        assert issues.parse_who(body) == issues.empty_who()
    assert issues.parse_who("## Who should do it\n\nprose only\n") == issues.empty_who()


def test_first_block_and_first_line_win():
    body = (block("- Claude: `claude --model sonnet`", "- Claude: `claude --model opus`")
            + "\n## Who should do it\n\n- Claude: `claude --model haiku`\n- Codex: `codex -m late`\n")
    who = issues.parse_who(body)
    assert who["claude"]["model"] == "sonnet" and who["codex"] is None


def test_comments_and_other_sections_do_not_count():
    body = "<!--\n## Who should do it\n- Claude: `claude --model haiku`\n-->\n" + block("- Claude: `claude --model sonnet`")
    assert issues.parse_who(body)["claude"]["model"] == "sonnet"
    assert issues.parse_who("<!-- unterminated\n" + block("- Claude: `claude --model opus`")) == issues.empty_who()


def test_first_backticked_span_only():
    who = issues.parse_who(block("- Claude: `claude --model sonnet` (not `--model opus`)"))
    assert who["claude"]["model"] == "sonnet"


def test_origin_owner_and_trust():
    for url in ("https://github.com/Acme/board.git", "git@github.com:acme/board.git", "ssh://git@github.com/acme/board",
                "https://user:tok@github.com/ACME/board/"):
        assert issues.origin_owner(url) == "acme", url
    assert issues.origin_owner("") is None and issues.origin_owner("/local/path/repo") is None and issues.origin_owner(None) is None
    assert issues.trusted("Acme", "acme") and issues.trusted("acme", "ACME")
    assert not issues.trusted("mallory", "acme") and not issues.trusted(None, "acme") and not issues.trusted("acme", None)
    assert issues.trusted("Me", "some-org", "me"), "an org repo: the board's own gh login is trusted"
    assert not issues.trusted("mallory", "some-org", "me") and not issues.trusted("", None, "") and not issues.trusted(None, None, None)


def test_an_org_repo_trusts_the_boards_own_gh_login_and_nobody_else(lite_client, gh, projects_dir, tmp_path):
    d = projects_dir / "shop" / "api"
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "remote", "add", "origin", "git@github.com:Some-Org/board.git"], check=True)
    rows = lite_client.get("/api/projects/shop/repos/api/issues", headers=H).json()["issues"]
    assert [r["trusted"] for r in rows] == [False, False], "without gh's login an org repo trusts nobody"
    (gh / "me").write_text("acme")
    prpoll._gh_login = None
    rows = lite_client.get("/api/projects/shop/repos/api/issues", headers=H).json()["issues"]
    assert [(r["author"], r["trusted"]) for r in rows] == [("Acme", True), ("mallory", False)]
    r = lite_client.get("/api/projects/shop/repos/api/issues/47", headers=H).json()
    assert r["trusted"] and r["who"]["claude"]["model"] == "sonnet"
    assert sum(c == "api user" for c in calls(tmp_path)) == 2, "one lookup per cache fill: the failed one, then the login"


def test_comment_body():
    b = issues.comment_body({"title": "T", "result": "r" * 900, "branch": "worktree-t", "pr_url": "https://x/pull/1"})
    assert "r" * 800 in b and "r" * 801 not in b and "worktree-t" in b and "https://x/pull/1" in b and b.endswith("Posted from ccboard")


# ---- routes with a fake gh -----------------------------------------------------------------------------------------
GH = r'''#!/bin/sh
echo "$@" >> "$GH_LOG"
case "$1 $2" in
  "issue list") cat "$GH_FIX/list.json";;
  "issue view") cat "$GH_FIX/view_$3.json" || exit 1;;
  "issue comment") cat > "$GH_FIX/stdin.txt"; if [ -f "$GH_FIX/fail" ]; then echo "HTTP 403: nope" >&2; exit 1; fi;;
  "api user") if [ -f "$GH_FIX/me" ]; then printf '{"login":"%s"}' "$(cat "$GH_FIX/me")"; else exit 1; fi;;
esac
'''


@pytest.fixture
def gh(tmp_path, monkeypatch):
    d = tmp_path / "ghbin"; d.mkdir()
    p = d / "gh"; p.write_text(GH); p.chmod(p.stat().st_mode | stat.S_IEXEC)
    fx = tmp_path / "ghfix"; fx.mkdir()
    full, out = fixture("issue_full.json"), fixture("issue_outsider.json")
    (fx / "list.json").write_text(json.dumps([full, out]))
    (fx / "view_47.json").write_text(json.dumps(full))
    (fx / "view_48.json").write_text(json.dumps(out))
    monkeypatch.setenv("GH_FIX", str(fx))
    monkeypatch.setenv("GH_LOG", str(tmp_path / "gh.log"))
    monkeypatch.setenv("PATH", str(d) + os.pathsep + os.environ["PATH"])
    (tmp_path / "gh.log").write_text("")
    monkeypatch.setattr(prpoll, "_gh_login", None)          # the login cache is per process
    return fx


@pytest.fixture
def repo(projects_dir):
    subprocess.run(["git", "init", "-q", "-b", "main", str(projects_dir / "shop" / "api")], check=True)
    subprocess.run(["git", "-C", str(projects_dir / "shop" / "api"), "remote", "add", "origin", "git@github.com:Acme/board.git"], check=True)
    return projects_dir / "shop" / "api"


def calls(tmp_path):
    return [l for l in (tmp_path / "gh.log").read_text().splitlines() if l]


def test_list_has_author_and_trusted(lite_client, gh, repo):
    rows = lite_client.get("/api/projects/shop/repos/api/issues", headers=H).json()["issues"]
    assert [(r["number"], r["author"], r["trusted"]) for r in rows] == [(47, "Acme", True), (48, "mallory", False)]


def test_view_trusted_has_who_untrusted_does_not(lite_client, gh, repo, tmp_path):
    r = lite_client.get("/api/projects/shop/repos/api/issues/47", headers=H).json()
    assert r["trusted"] and r["who"]["claude"]["model"] == "sonnet" and r["who"]["codex"]["reasoning"] == "medium"
    assert any(c.startswith("issue view 47 --json") for c in calls(tmp_path))
    r = lite_client.get("/api/projects/shop/repos/api/issues/48", headers=H).json()
    assert not r["trusted"] and r["who"] == issues.empty_who() and r["author"] == "mallory"


def test_view_path_must_be_an_integer(lite_client, gh, repo, tmp_path):
    assert lite_client.get("/api/projects/shop/repos/api/issues/4x", headers=H).status_code in (404, 405, 422)
    assert lite_client.get("/api/projects/shop/repos/api/issues/0", headers=H).status_code == 400
    assert not any("issue view" in c for c in calls(tmp_path))


def test_view_failure_is_a_clear_error(lite_client, gh, repo):
    r = lite_client.get("/api/projects/shop/repos/api/issues/99", headers=H)
    assert r.status_code >= 400 and r.json().get("error") or r.json().get("detail")


def _done_task(main, repo, **kw):
    fields = dict(project="shop", repo="api", slug="fix", title="Fix it", prompt="p", branch="worktree-fix", base="main",
                  tmux_name="shop--api--t-fix", phase="done", result="All fixed.", issue_number=47,
                  issue_url="https://github.com/acme/board/issues/47")
    fields.update(kw)
    return main.db.task_add(**fields)


def test_comment_posts_once_with_stdin(lite_client, gh, repo, tmp_path):
    from app import main
    tid = _done_task(main, repo, pr_url="https://github.com/acme/board/pull/9")
    pre = lite_client.get(f"/api/tasks/{tid}/issue-comment", headers=H).json()
    assert "All fixed." in pre["body"] and "worktree-fix" in pre["body"] and "pull/9" in pre["body"] and pre["body"].endswith("Posted from ccboard")
    assert not any("issue comment" in c for c in calls(tmp_path))          # a preview posts nothing
    r = lite_client.post(f"/api/tasks/{tid}/issue-comment", headers=H)
    assert r.status_code == 200 and r.json()["ok"]
    assert calls(tmp_path)[-1] == "issue comment 47 --body-file -"
    assert (gh / "stdin.txt").read_text() == pre["body"]
    assert main.db.task_get(tid)["issue_commented_at"]
    assert lite_client.post(f"/api/tasks/{tid}/issue-comment", headers=H).status_code == 409
    assert sum("issue comment" in c for c in calls(tmp_path)) == 1


def test_comment_conflicts_and_failure(lite_client, gh, repo, tmp_path):
    from app import main
    no_issue = _done_task(main, repo, slug="a", issue_number=None, issue_url=None)
    running = _done_task(main, repo, slug="b", phase="running")
    for tid in (no_issue, running):
        assert lite_client.post(f"/api/tasks/{tid}/issue-comment", headers=H).status_code == 409
    assert lite_client.post("/api/tasks/9999/issue-comment", headers=H).status_code == 404
    assert not any("issue comment" in c for c in calls(tmp_path))
    ok = _done_task(main, repo, slug="c")
    (gh / "fail").write_text("1")
    r = lite_client.post(f"/api/tasks/{ok}/issue-comment", headers=H)
    assert r.status_code >= 400 and "403" in json.dumps(r.json())
    assert main.db.task_get(ok)["issue_commented_at"] is None             # still postable; nothing replays it
    (gh / "fail").unlink()
    assert lite_client.post(f"/api/tasks/{ok}/issue-comment", headers=H).status_code == 200


def test_comment_needs_the_csrf_header(lite_client, gh, repo):
    from app import main
    tid = _done_task(main, repo)
    r = lite_client.post(f"/api/tasks/{tid}/issue-comment", headers={"Tailscale-User-Login": "alice@example.com"})
    assert r.status_code in (400, 403)


def test_task_create_keeps_the_issue_link(lite_client, gh, repo):
    r = lite_client.post("/api/tasks", headers=H, json={"project": "shop", "repo": "api", "title": "T", "prompt": "p", "when": "later",
                                                         "issue_number": 47, "issue_url": "https://github.com/acme/board/issues/47"})
    assert r.status_code == 201
    t = r.json()["task"]
    assert t["issue_number"] == 47 and t["issue_url"].endswith("/issues/47")
    got = lite_client.get(f"/api/tasks/{r.json()['id']}", headers=H).json()
    assert got["issue_number"] == 47
    for bad in ({"issue_number": 0}, {"issue_number": 3, "issue_url": "javascript:alert(1)"}, {"issue_number": 3, "issue_url": "http://x/1"}):
        r = lite_client.post("/api/tasks", headers=H, json={"project": "shop", "repo": "api", "title": "T2", "prompt": "p", "when": "later", **bad})
        assert r.status_code == 400, bad
    plain = lite_client.post("/api/tasks", headers=H, json={"project": "shop", "repo": "api", "title": "T3", "prompt": "p", "when": "later"})
    assert plain.json()["task"]["issue_number"] is None
