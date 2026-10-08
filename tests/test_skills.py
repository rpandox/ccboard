"""The skills the palette lists (issue #102, app/skills.py): front matter only, inside the skills roots, capped, cached, ordered by use.
Everything runs against a throwaway Claude config folder; nothing reads the real ~/.claude."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from app import skills
from app.config import settings

H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    d = tmp_path / "claude-config"
    d.mkdir()
    monkeypatch.setattr(settings, "claude_config_dir", d)
    skills.reset()
    yield d
    skills.reset()


def skill(root: Path, folder: str, name=None, desc="Does a thing.", body="# body\n"):
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    fm = "---\n" + (f"name: {name}\n" if name is not None else "") + f"description: {desc}\n---\n"
    (d / "SKILL.md").write_text(fm + body)
    return d


def plugin(cfg: Path, name: str, version: str, folders, installed=True, market="mk"):
    root = cfg / "plugins" / "cache" / market / name / version
    for f in folders:
        skill(root / "skills", f, desc=f"{f} from {name}")
    if installed:
        reg = cfg / "plugins" / "installed_plugins.json"
        data = json.loads(reg.read_text()) if reg.exists() else {"version": 2, "plugins": {}}
        data["plugins"][f"{name}@{market}"] = [{"scope": "user", "installPath": str(root), "version": version}]
        reg.write_text(json.dumps(data))
    return root


# ---------------------------------------------------------------- front matter

def test_front_matter_plain_quoted_folded_and_literal():
    assert skills.parse_front("---\nname: browse\ndescription: Drive a browser.\n---\n# x") == {"name": "browse", "description": "Drive a browser."}
    assert skills.parse_front('---\nname: "loop"\ndescription: \'Run on an interval\'\n---\n') == {"name": "loop", "description": "Run on an interval"}
    assert skills.parse_front("---\nname: a\ndescription: >\n  First line\n  second line\n\n  third\nother: 1\n---\n") == {"name": "a", "description": "First line second line third"}
    assert skills.parse_front("---\ndescription: |-\n  one\n  two\n---\n") == {"description": "one two"}
    assert skills.parse_front("---\nname: only\n---\n") == {"name": "only"}


def test_front_matter_that_is_missing_or_never_closes_is_nothing():
    assert skills.parse_front("# no front matter\nname: x") == {}
    assert skills.parse_front("---\nname: x\ndescription: never closed") == {}
    assert skills.parse_front(b"\xff\xfe") == {}
    assert skills.parse_front("") == {}
    body_only = "---\nname: a\n---\nname: evil\ndescription: from the body\n"
    assert skills.parse_front(body_only) == {"name": "a"}, "the body is never read as front matter"


def test_a_huge_file_is_read_at_most_front_max_bytes_and_never_its_body(cfg, monkeypatch):
    d = skill(cfg / "skills", "big", desc="small", body="x" * (5 * 1024 * 1024))
    sizes = []
    real_open = open

    def spy(path, mode="r", *a, **k):
        f = real_open(path, mode, *a, **k)
        if str(path).endswith("SKILL.md"):
            real_read = f.read
            f.read = lambda n=-1: (sizes.append(n), real_read(n))[1]
        return f
    monkeypatch.setattr("builtins.open", spy)
    assert [s["name"] for s in skills.scan()] == ["big"]
    assert sizes == [skills.FRONT_MAX]
    assert (d / "SKILL.md").stat().st_size > 5 * 1024 * 1024


def test_a_front_matter_longer_than_the_read_is_listed_by_folder_name_with_no_description(cfg):
    d = cfg / "skills" / "long"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: long\ndescription: " + "y" * 9000 + "\n---\n")
    assert skills.scan() == [{"name": "long", "description": "", "source": "user"}], "no closing --- inside the first 8 KB: listed by its folder name, nothing half-read"


# ---------------------------------------------------------------- the scan

def test_user_and_installed_plugin_skills_are_listed_with_their_source_and_namespaced_name(cfg):
    skill(cfg / "skills", "browse", name="browse", desc="Drive a real browser through Aside.")
    skill(cfg / "skills", "loop", desc="Run a prompt on an interval")                        # no name: the folder's
    plugin(cfg, "claude-mem", "13.34.2", ["mem-search", "do"])
    plugin(cfg, "claude-mem", "13.31.0", ["old-skill"], installed=False)
    got = {s["name"]: s for s in skills.scan()}
    assert set(got) == {"browse", "loop", "claude-mem:mem-search", "claude-mem:do"}, "an old version in the cache is not installed"
    assert got["browse"]["source"] == "user" and got["claude-mem:do"]["source"] == "plugin:claude-mem"
    assert got["browse"]["description"] == "Drive a real browser through Aside."
    assert [s["name"] for s in skills.scan()] == sorted(got)


def test_descriptions_are_cut_at_160_characters(cfg):
    skill(cfg / "skills", "wordy", desc="w" * 400)
    assert len(skills.scan()[0]["description"]) == 160


@pytest.mark.parametrize("folder,name", [("Upper", None), ("has space", None), ("../x", None), ("x" * 65, None), ("ok", "Bad Name"), ("ok2", "a/b"), ("-lead", None), ("", None)])
def test_names_that_do_not_match_the_pattern_are_rejected(cfg, folder, name):
    try:
        skill(cfg / "skills", folder or "emptyname", name=name)
    except OSError:
        pytest.skip("not a folder name here")
    want = [] if (folder or "emptyname") != "emptyname" or name else ["emptyname"]
    names = [s["name"] for s in skills.scan()]
    assert not any(n in names for n in ("Upper", "has space", "Bad Name", "a/b", "-lead")), names
    assert len(names) <= 1


def test_a_symlinked_skill_folder_or_skill_file_is_not_followed(cfg, tmp_path):
    outside = tmp_path / "outside"
    skill(outside, "secret", desc="not yours")
    (cfg / "skills").mkdir()
    (cfg / "skills" / "linked").symlink_to(outside / "secret", target_is_directory=True)
    folder = cfg / "skills" / "filelink"
    folder.mkdir()
    (folder / "SKILL.md").symlink_to(outside / "secret" / "SKILL.md")
    skill(cfg / "skills", "fine")
    assert [s["name"] for s in skills.scan()] == ["fine"]


def test_a_skills_folder_that_is_a_symlink_out_is_not_scanned(cfg, tmp_path):
    outside = tmp_path / "elsewhere"
    skill(outside, "stolen")
    (cfg / "skills").symlink_to(outside, target_is_directory=True)
    assert skills.scan() == []


def test_a_plugin_whose_install_path_escapes_the_plugins_folder_is_dropped(cfg, tmp_path):
    outside = tmp_path / "evil-plugin"
    skill(outside / "skills", "pwn")
    (cfg / "plugins").mkdir()
    (cfg / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {"evil@mk": [{"installPath": str(outside)}], "bad name@mk": [{"installPath": str(cfg)}]}}))
    assert skills.scan() == []


def test_a_broken_registry_or_missing_folders_give_an_empty_list_not_an_error(cfg):
    assert skills.scan() == []
    (cfg / "plugins").mkdir()
    (cfg / "plugins" / "installed_plugins.json").write_text("{not json")
    skills.reset()
    assert skills.scan() == []
    (cfg / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": ["nope"]}))
    skills.reset()
    assert skills.scan() == []


def test_the_list_is_capped_at_300(cfg):
    for i in range(skills.LIST_CAP + 25):
        skill(cfg / "skills", f"s{i:04d}")
    assert len(skills.scan()) == skills.LIST_CAP


def test_the_scan_is_cached_for_60_seconds_and_follows_the_config_folder(cfg, monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(skills, "clock", lambda: now["t"])
    skill(cfg / "skills", "one")
    assert [s["name"] for s in skills.scan()] == ["one"]
    skill(cfg / "skills", "two")
    assert [s["name"] for s in skills.scan()] == ["one"], "served from the cache"
    now["t"] += 59
    assert [s["name"] for s in skills.scan()] == ["one"]
    now["t"] += 2
    assert [s["name"] for s in skills.scan()] == ["one", "two"], "rescanned after 60 s"
    other = cfg.parent / "other-config"
    skill(other / "skills", "elsewhere")
    monkeypatch.setattr(settings, "claude_config_dir", other)
    assert [s["name"] for s in skills.scan()] == ["elsewhere"]


# ---------------------------------------------------------------- uses

class FakeDb:
    def __init__(self):
        self.kv = {}

    def kv_get(self, k):
        return {"value": self.kv[k], "at": "x"} if k in self.kv else None

    def kv_set(self, k, v, at=None):
        self.kv[k] = json.loads(json.dumps(v))


def test_a_typed_skill_command_counts_one_use_and_nothing_else_does(cfg):
    skill(cfg / "skills", "browse")
    plugin(cfg, "claude-mem", "1.0.0", ["mem-search"])
    db = FakeDb()
    for p in ("/browse open the page", "  /browse", "/browse\nsecond line", "/claude-mem:mem-search x", "/mem-search bare name", "/compact", "/unknown thing", "browse without a slash",
              "please /browse", "/browse-extra", None, 5, "", "/"):
        skills.bump_use(db, p)
    assert db.kv["skill_uses"] == {"browse": 3, "claude-mem:mem-search": 2}


def test_listing_orders_by_uses_then_name_and_shows_no_count_for_an_unused_skill(cfg):
    for n in ("alpha", "bravo", "charlie", "delta"):
        skill(cfg / "skills", n)
    db = FakeDb()
    db.kv["skill_uses"] = {"charlie": 5, "bravo": 5, "delta": 1, "ghost": 9, "alpha": 0, "bad": "x", "neg": -3, "flag": True}
    got = skills.listing(db)
    assert got["agent"] == "claude"
    assert [s["name"] for s in got["skills"]] == ["bravo", "charlie", "delta", "alpha"], "uses first (ties by name), then the unused by name; a skill that is gone is not listed"
    assert [s.get("uses") for s in got["skills"]] == [5, 5, 1, None]
    assert "uses" not in got["skills"][-1], "never 0 uses"


def test_the_use_counter_is_capped(cfg):
    skill(cfg / "skills", "keep")
    db = FakeDb()
    db.kv["skill_uses"] = {f"old{i}": 1 for i in range(skills.USES_CAP)}
    skills.bump_use(db, "/keep")
    assert len(db.kv["skill_uses"]) == skills.USES_CAP and db.kv["skill_uses"]["keep"] == 1


# ---------------------------------------------------------------- the route and the hook

def git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init", "-q", "-b", "main"], check=True)


def test_get_api_skills_answers_the_listing(lite_client, cfg):
    skill(cfg / "skills", "browse", desc="Drive a browser")
    r = lite_client.get("/api/skills", headers=H)
    assert r.status_code == 200 and r.json() == {"agent": "claude", "skills": [{"name": "browse", "description": "Drive a browser", "source": "user"}]}
    assert lite_client.get("/api/skills").status_code == 403, "identity required like every other read"


def test_a_user_prompt_submit_of_a_skill_command_bumps_its_counter(lite_client, cfg, projects_dir, fake_tmux):
    from app import hooks, main
    skill(cfg / "skills", "browse")
    git_init(projects_dir / "shop" / "api")
    name = lite_client.post("/api/projects/shop/repos/api/sessions", headers=H, json={"launcher": "shell"}).json()["tmux"]

    def fire(prompt):
        return lite_client.post("/api/hook", headers={"X-CCBoard-Token": hooks.ensure_token(), "X-CCBoard-Session": name}, content=json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": prompt}))
    assert fire("/browse go to the docs").status_code == 200
    assert fire("fix the login").status_code == 200
    assert fire("<system-reminder>/browse</system-reminder>").status_code == 200
    assert fire("/browse again").status_code == 200
    assert main.db.kv_get("skill_uses")["value"] == {"browse": 2}
    assert lite_client.get("/api/skills", headers=H).json()["skills"][0]["uses"] == 2
