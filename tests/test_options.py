"""Read-only option coverage, unchanged parsing, provenance and the person-only boundary."""
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from app import config, options, platform as plat

PERSON = {"Tailscale-User-Login": "alice"}
URL = "/api/settings/options"


def test_registry_covers_the_file_once():
    rows = options.registry()
    keys = [o.key for o in rows]
    assert len(keys) == len(set(keys))
    assert set(keys) == set(config.ENV_FILE_KEYS) | options.EXTRAS
    assert len(options.GROUPS) == 9
    for o in rows:
        assert o.group in dict(options.GROUPS), o.key
        assert o.cls in ("page", "confirm", "file", "installer"), o.key
        assert o.applies in ("restart", "installer"), o.key
        assert o.help.endswith(".") and "—" not in o.help, o.key
        assert not o.attr or o.applies == "restart", o.key
    with pytest.raises(FrozenInstanceError):
        rows[0].key = "changed"


@pytest.mark.parametrize("key", [o.key for o in options.registry() if o.attr])
def test_defaults_match_existing_parsers(key):
    option = next(o for o in options.registry() if o.key == key)
    assert getattr(config.Settings({}), option.attr) == getattr(config.Settings({key: option.default}), option.attr), key


@pytest.fixture
def endpoint(lite_client, monkeypatch):
    from app import main
    monkeypatch.setattr(config.settings, "allowed_users", {"alice"})

    def get(env):
        described = config.Settings(env)
        described.allowed_users = {"alice"}
        monkeypatch.setattr(main, "settings", described)
        response = lite_client.get(URL, headers=PERSON)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        return response, {o["key"]: o for o in response.json()["options"]}
    return get


def env_file(tmp_path, text):
    p = tmp_path / "env"
    p.write_text(text)
    p.chmod(0o600)
    return str(p)


def test_file_environment_default_and_rejected_value(endpoint, tmp_path):
    path = env_file(tmp_path, "CCBOARD_NODE_LANES=8\nCCBOARD_EPIC_WORKER_POLL=5\nCCBOARD_EPIC_ALIAS=from-file\nPRIVATE_LINE=never-list-this\n# a comment\n\n")
    response, rows = endpoint({"CCBOARD_ENV_FILE": path, "CCBOARD_EPIC_ALIAS": "from-environment"})
    assert rows["CCBOARD_NODE_LANES"]["source"] == "file"
    assert rows["CCBOARD_NODE_LANES"]["value"] == 8
    assert rows["CCBOARD_EPIC_ALIAS"]["source"] == "environment"
    assert rows["CCBOARD_EPIC_ALIAS"]["value"] == "from-environment"
    assert rows["CCBOARD_EPIC_WORKER_MAX_PER_HOUR"]["source"] == "default"
    assert rows["CCBOARD_EPIC_WORKER_POLL"]["value"] == 90
    assert rows["CCBOARD_EPIC_WORKER_POLL"]["problem"].endswith(".")
    assert response.json()["other_lines"] == 3
    assert "never-list-this" not in response.text
    assert "PRIVATE_LINE" not in response.text


def test_secret_and_url_credentials_never_leave_the_process(endpoint):
    token = "fixture-only-hub-secret"
    response, rows = endpoint({"CCBOARD_HUB_TOKEN": token, "CCBOARD_PUBLIC_URL": "https://user:pass@example.invalid/", "CCBOARD_NODES": "demo=https://user:pass@example.invalid/"})
    assert token not in response.text
    assert "user:pass" not in response.text
    assert rows["CCBOARD_HUB_TOKEN"]["value"] is None
    assert rows["CCBOARD_HUB_TOKEN"]["set"] is True
    assert rows["CCBOARD_PUBLIC_URL"]["value"] == "https://***@example.invalid/"
    _, empty = endpoint({})
    assert empty["CCBOARD_HUB_TOKEN"]["set"] is False


def test_settings_keeps_only_registry_raw_and_does_not_mutate_a_mapping(tmp_path):
    path = env_file(tmp_path, "CCBOARD_NODE_LANES=7\n")
    env = {"CCBOARD_ENV_FILE": path, "UNRELATED": "private"}
    before = dict(env)
    settings = config.Settings(env)
    assert env == before
    assert settings.raw == {"CCBOARD_ENV_FILE": path, "CCBOARD_NODE_LANES": "7"}
    assert settings.file_keys == {"CCBOARD_NODE_LANES"}


@pytest.mark.parametrize("raw", ["", "   "])
def test_empty_environment_uses_default_source(endpoint, raw):
    _, rows = endpoint({"CCBOARD_EPIC_ALIAS": raw})
    assert rows["CCBOARD_EPIC_ALIAS"]["source"] == "default"
    assert rows["CCBOARD_EPIC_ALIAS"]["set"] is False


def test_file_source_takes_precedence_over_empty_raw():
    settings = config.Settings({"CCBOARD_EPIC_ALIAS": ""})
    settings.file_keys.add("CCBOARD_EPIC_ALIAS")
    row = next(o for o in options.view(settings)["options"] if o["key"] == "CCBOARD_EPIC_ALIAS")
    assert row["source"] == "file"


def test_linux_process_matching_explicit_file_is_attributed(tmp_path, monkeypatch):
    import os
    path = env_file(tmp_path, "CCBOARD_NODE_LANES=7\nCCBOARD_EPIC_ALIAS=file-alias\nCCBOARD_EPIC_WORKER_POLL=120\n")
    monkeypatch.setattr(plat, "IS_LINUX", True)
    monkeypatch.setenv("CCBOARD_ENV_FILE", path)
    monkeypatch.setenv("CCBOARD_NODE_LANES", "7")
    monkeypatch.setenv("CCBOARD_EPIC_ALIAS", "environment-alias")
    settings = config.Settings()
    assert {"CCBOARD_NODE_LANES", "CCBOARD_EPIC_WORKER_POLL"} <= settings.file_keys
    assert "CCBOARD_EPIC_ALIAS" not in settings.file_keys
    assert os.environ["CCBOARD_EPIC_ALIAS"] == "environment-alias"


def test_unsafe_file_is_not_read(tmp_path, monkeypatch):
    path = Path(env_file(tmp_path, "CCBOARD_NODE_LANES=7\nPRIVATE=value\n"))
    path.chmod(0o640)
    monkeypatch.setattr(plat, "current_uid", lambda: path.stat().st_uid)
    settings = config.Settings({"CCBOARD_ENV_FILE": str(path), "CCBOARD_NODE_LANES": "7"})
    assert not settings.file_keys
    assert settings.other_lines == 0
    assert next(o for o in options.view(settings)["options"] if o["key"] == "CCBOARD_NODE_LANES")["source"] == "environment"


def test_all_non_person_callers_are_refused(lite_client, monkeypatch):
    from app import hooks, main, mcp_tokens, nodes
    monkeypatch.setattr(config.settings, "allowed_users", {"alice"})
    monkeypatch.setattr(config.settings, "hub_token", "fixture-hub")
    assert lite_client.get(URL).status_code in (401, 403)
    assert lite_client.get(URL, headers=PERSON).status_code == 200
    headers = [{"X-CCBoard-Token": hooks.ensure_token()}, {"X-CCBoard-Hub": "fixture-hub"}]
    for scope in nodes.SCOPES:
        _, token = nodes.add_incoming(scopes=[scope], db=main.db)
        headers.append({"Authorization": "Bearer " + token})
    token, _ = mcp_tokens.mint("fixture-device")
    headers.append({"Authorization": "Bearer " + token})
    for header in headers:
        assert lite_client.get(URL, headers=header).status_code == 403


@pytest.mark.parametrize("user", ["local-token", "hub", "node:fixture", "mcp:fixture", None])
def test_handler_itself_refuses_agent_identities(user):
    from types import SimpleNamespace
    from app import main, projects
    with pytest.raises(projects.Forbidden):
        main.api_settings_options(SimpleNamespace(state=SimpleNamespace(user=user)))


def test_fixture_matches_registry():
    fixture = json.loads((Path(__file__).resolve().parents[1] / "app/static/demo/options.json").read_text())
    assert [o["key"] for o in fixture["options"]] == [o.key for o in options.registry()]
    node_keys = next(o for o in fixture["options"] if o["key"] == "CCBOARD_NODE_KEYS")
    assert node_keys["default"] == node_keys["value"] == "require"
    assert node_keys["line"] == "CCBOARD_NODE_KEYS=require"
    assert fixture["groups"] == [{"id": k, "label": v} for k, v in options.GROUPS]
    assert all(o["value"] is None for o in fixture["options"] if o["secret"])
    lanes = next(o for o in fixture["options"] if o["key"] == "CCBOARD_NODE_LANES")
    assert lanes["value"] == int(lanes["edit_value"])
    assert lanes["line"] == "CCBOARD_NODE_LANES=" + lanes["edit_value"]
    assert lanes["pending"] == (lanes["value"] != lanes["running"])
    assert lanes["pending"] == (lanes["key"] in fixture["pending"])



def test_existing_mcp_page_switch_is_reported(endpoint):
    from app import main, mcp_tokens
    main.db.kv_set(mcp_tokens.KV_REMOTE, {"enabled": False})
    _, rows = endpoint({"CCBOARD_MCP_REMOTE": "1"})
    assert rows["CCBOARD_MCP_REMOTE"]["value"] is False
    assert rows["CCBOARD_MCP_REMOTE"]["source"] == "page"


def test_file_merge_keeps_all_parsed_attributes_identical(tmp_path):
    env = {"CCBOARD_EPIC_WORKER_POLL": "5", "CCBOARD_NODE_LANES": "12", "CCBOARD_NODE_PORTS": "443,8443,invalid",
           "CCBOARD_EPIC_AUTHORS": "alice:123,bob:456", "CCBOARD_BACKUP_EXTRA": "one:two", "CCBOARD_AUTO_CONTINUE": "0"}
    path = env_file(tmp_path, "".join(f"{k}={v}\n" for k, v in env.items()))
    direct = vars(config.Settings(env))
    merged = vars(config.Settings({"CCBOARD_ENV_FILE": path}))
    for attr in direct.keys() - {"raw", "file_keys", "other_lines", "_input_env", "page_file", "page_info"}:
        assert merged[attr] == direct[attr], attr


def test_nonfinite_approval_timeout_displays_safe_default(endpoint):
    _, rows = endpoint({"CCBOARD_APPROVE_TIMEOUT": "nan"})
    assert rows["CCBOARD_APPROVE_TIMEOUT"]["value"] == 90
    assert "default is in use" in rows["CCBOARD_APPROVE_TIMEOUT"]["problem"]


@pytest.mark.parametrize("value", ["https://user:pass@example.invalid/", "https://u:p/ss@example.invalid/", "https://u:p?ss#x@y@example.invalid/"])
def test_url_userinfo_is_masked_even_when_malformed(value):
    assert options.mask_urls(value) == "https://***@example.invalid/"


@pytest.mark.parametrize("field", ["value", "line"])
@pytest.mark.parametrize("key,raw,masked", [
    ("NTFY_URL", "https://ntfy.example.invalid/?auth=SECRETQ", "https://ntfy.example.invalid/?auth=***"),
    ("CCBOARD_RESTIC_REPO", "rest:https://h.invalid/?pw=SECRETQ", "rest:https://h.invalid/?pw=***"),
    ("CCBOARD_NODES", "a=https://h.invalid:443?token=SECRETQ,b=https://u:pw@h.invalid", "a=https://h.invalid:443?token=***,b=https://***@h.invalid"),
    ("NTFY_URL", "https://u:p/ss@h.invalid/", "https://***@h.invalid/"),
])
def test_url_credentials_are_masked_in_value_and_copy_line(endpoint, key, raw, masked, field):
    _, rows = endpoint({key: raw})
    assert rows[key][field] == (key + "=" + masked if field == "line" else masked)


@pytest.mark.parametrize("query_key", ["token", "AUTH", "Key", "pass", "Passphrase", "PASSWORD", "pw", "secret", "sig", "signature"])
@pytest.mark.parametrize("field", ["value", "line"])
def test_credential_query_keys_are_case_insensitive(endpoint, query_key, field):
    raw = f"https://h.invalid/?keep=yes&{query_key}=SECRETQ&token=SECRETQ#section"
    _, rows = endpoint({"NTFY_URL": raw})
    masked = f"https://h.invalid/?keep=yes&{query_key}=***&token=***#section"
    assert rows["NTFY_URL"][field] == ("NTFY_URL=" + masked if field == "line" else masked)


@pytest.mark.parametrize("separator", [",", " ", "\n"])
def test_userinfo_masking_stops_at_entry_boundaries(separator):
    assert options.mask_urls("https://a.invalid/" + separator + "https://u:pw@b.invalid/") == (
        "https://a.invalid/" + separator + "https://***@b.invalid/")


def test_linux_service_file_is_inspected_but_never_merged(tmp_path, monkeypatch):
    path = env_file(tmp_path, "CCBOARD_NODE_LANES=7\nCCBOARD_EPIC_ALIAS=file-alias\nCCBOARD_EPIC_WORKER_POLL=120\n")
    reader = config.read_env_file
    checker = config.env_file_problem

    def read_service_file(requested, **kwargs):
        assert requested == Path("/etc/ccboard/env")
        return reader(path, **kwargs)

    monkeypatch.setattr(config, "read_env_file", read_service_file)
    monkeypatch.setattr(config, "env_file_problem", lambda requested: checker(path))
    monkeypatch.setattr(plat, "IS_LINUX", True)
    monkeypatch.delenv("CCBOARD_ENV_FILE", raising=False)
    monkeypatch.delenv("CCBOARD_EPIC_WORKER_POLL", raising=False)
    monkeypatch.setenv("CCBOARD_NODE_LANES", "7")
    monkeypatch.setenv("CCBOARD_EPIC_ALIAS", "environment-alias")
    settings = config.Settings()
    assert settings.file_keys == {"CCBOARD_NODE_LANES"}
    assert settings.epic_worker_poll == 90
    assert "CCBOARD_EPIC_WORKER_POLL" not in settings.raw
    assert settings.epic_alias == "environment-alias"


@pytest.mark.parametrize("problem", ["unreadable", "foreign-owner", "group-readable"])
def test_linux_service_file_problem_is_silent(tmp_path, monkeypatch, caplog, problem):
    from types import SimpleNamespace
    path = Path(env_file(tmp_path, "CCBOARD_EPIC_ALIAS=from-file\n"))
    st = path.stat()
    lstat = config.os.lstat
    reads = []
    reader = config.read_env_file

    def service_stat(requested, *args, **kwargs):
        if requested != Path("/etc/ccboard/env"):
            return lstat(requested, *args, **kwargs)
        if problem == "unreadable":
            raise PermissionError(13, "Permission denied")
        return SimpleNamespace(st_uid=st.st_uid + (problem == "foreign-owner"),
                               st_mode=st.st_mode | (0o040 if problem == "group-readable" else 0))

    def track_read(requested, **kwargs):
        reads.append(requested)
        return reader(requested, **kwargs)

    monkeypatch.setattr(config.os, "lstat", service_stat)
    monkeypatch.setattr(plat, "current_uid", lambda: st.st_uid)
    monkeypatch.setattr(config, "read_env_file", track_read)
    monkeypatch.setattr(plat, "IS_LINUX", True)
    monkeypatch.delenv("CCBOARD_ENV_FILE", raising=False)
    monkeypatch.setenv("CCBOARD_EPIC_ALIAS", "")
    monkeypatch.setenv("CCBOARD_NODE_LANES", "7")
    with caplog.at_level("WARNING", logger="ccboard"):
        settings = config.Settings()
    assert not caplog.records
    assert reads == []
    assert settings.file_keys == set()
    assert settings.other_lines == 0
    rows = {o["key"]: o for o in options.view(settings)["options"]}
    assert rows["CCBOARD_EPIC_ALIAS"]["source"] == "default"
    assert rows["CCBOARD_NODE_LANES"]["source"] == "environment"


def test_node_keys_default_is_require_on_options_page(endpoint):
    _, rows = endpoint({})
    row = rows["CCBOARD_NODE_KEYS"]
    assert row["default"] == row["value"] == "require"
    assert row["source"] == "default"
    assert row["line"] == "CCBOARD_NODE_KEYS=require"
    _, rows = endpoint({"CCBOARD_NODE_KEYS": "prefer"})
    assert rows["CCBOARD_NODE_KEYS"]["value"] == "prefer"
