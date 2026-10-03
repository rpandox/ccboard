"""scripts/code_server_settings.py: merge-only, idempotent, reversible, never rewrites a file it cannot parse."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("code_server_settings", ROOT / "scripts" / "code_server_settings.py")
css = importlib.util.module_from_spec(spec)
spec.loader.exec_module(css)


def test_install_writes_only_missing_keys_and_merges_maps(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"editor.minimap.enabled": True, "files.watcherExclude": {"**/mine/**": True, "**/node_modules/**": False}}))
    assert css.main(["install", "--settings", str(p)]) == 0
    d = json.loads(p.read_text())
    assert d["editor.minimap.enabled"] is True, "the person's choice wins"
    assert d["files.watcherExclude"]["**/mine/**"] is True and d["files.watcherExclude"]["**/node_modules/**"] is False, "existing entries are kept"
    assert d["files.watcherExclude"]["**/.venv/**"] is True and d["search.exclude"]["**/node_modules/**"] is True
    assert d["telemetry.telemetryLevel"] == "off" and d["security.workspace.trust.enabled"] is False
    assert "editor.minimap.enabled" not in d[css.MANAGED_FLAG] and "telemetry.telemetryLevel" in d[css.MANAGED_FLAG]
    before = p.read_text()
    assert css.main(["install", "--settings", str(p)]) == 0
    assert p.read_text() == before, "idempotent"


def test_remove_takes_back_exactly_what_ccboard_wrote(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"git.autofetch": True, "files.watcherExclude": {"**/mine/**": True}}))
    css.main(["install", "--settings", str(p)])
    css.main(["remove", "--settings", str(p)])
    d = json.loads(p.read_text())
    assert d == {"git.autofetch": True, "files.watcherExclude": {"**/mine/**": True}}


def test_missing_file_is_created_and_non_json_is_left_alone(tmp_path, capsys):
    p = tmp_path / "User" / "settings.json"
    assert css.main(["install", "--settings", str(p)]) == 0
    assert json.loads(p.read_text())["update.mode"] == "none"
    q = tmp_path / "commented.json"
    q.write_text('{\n  // my comment\n  "a": 1\n}\n')
    assert css.main(["install", "--settings", str(q)]) == 0
    assert q.read_text().startswith("{\n  // my comment"), "a file with comments is never rewritten"
    assert "left untouched" in capsys.readouterr().out


def test_excludes_cover_the_usual_heavy_directories():
    for d in ("**/node_modules/**", "**/.venv/**", "**/dist/**", "**/.next/**", "**/.claude/worktrees/**", "**/.ccboard/**"):
        assert css.SETTINGS["files.watcherExclude"][d] is True and css.SETTINGS["search.exclude"][d] is True
    assert "**/.git/objects/**" in css.SETTINGS["files.watcherExclude"] and "**/.git/objects/**" not in css.SETTINGS["search.exclude"]
