"""An existing dashboard takes precedence over the optional local viewer mapping."""
import pytest

from app import config, doctor, options

KEY = "CCBOARD_MEM_VIEWER_URL"
DASHBOARD = "https://memory.example.ts.net:8446/viewer?q=project#recent"


@pytest.mark.parametrize("url", [DASHBOARD, "http://localhost:37700/", "http://127.0.0.1:37700/",
                                 "http://127.0.0.2/", "http://[::1]:37700/", ""])
def test_valid_viewer_url(url):
    settings = config.Settings({KEY: url})
    assert settings.mem_viewer_url_bad is False
    assert settings.mem_viewer_url() == (url or None)


@pytest.mark.parametrize("url", ["junk", "https://", "http://memory.example.ts.net/", "http://localhost.example.ts.net/",
                                 "http://192.168.1.1/", "https://user:secret@example.ts.net/", "https://@example.ts.net/",
                                 "javascript:alert(1)", "https://example.ts.net:bad/", "https://example.ts.net:65536/",
                                 "https://example.ts.net/\npath", "https://example.ts.net\\@localhost/",
                                 "https://example.ts.net/?token=secret"])
def test_invalid_viewer_url_is_ignored_and_reported(url, monkeypatch, caplog):
    settings = config.Settings({KEY: url})
    assert settings.mem_viewer_url_bad is True
    assert settings.mem_viewer_url() is None
    row = next(row for row in options.view(settings)["options"] if row["key"] == KEY)
    assert row["value"] == "" and row["problem"]
    monkeypatch.setattr(doctor, "settings", settings)
    result = doctor._mem_viewer(None, {})
    assert result[0] == "warn" and KEY in result[1]
    assert "secret" not in caplog.text + str(result)
    assert "user:secret" not in str(row) and "token=secret" not in str(row)


def test_state_viewer_precedence(lite_client, monkeypatch):
    from app import main
    base = {"CCBOARD_ALLOWED_USERS": "alice", "CCBOARD_PUBLIC_URL": "https://board.example.ts.net/"}
    cases = [({}, None), ({"CCBOARD_MEM_HTTPS_PORT": "10443"}, "https://board.example.ts.net:10443/"),
             ({KEY: DASHBOARD}, DASHBOARD), ({KEY: DASHBOARD, "CCBOARD_MEM_HTTPS_PORT": "10443"}, DASHBOARD),
             ({KEY: "junk", "CCBOARD_MEM_HTTPS_PORT": "10443"}, "https://board.example.ts.net:10443/")]
    for extra, expected in cases:
        monkeypatch.setattr(main, "settings", config.Settings({**base, **extra}))
        answer = lite_client.get("/api/state", headers={"Tailscale-User-Login": "alice"})
        assert answer.status_code == 200
        assert answer.json()["config"]["mem_viewer_url"] == expected


def test_doctor_existing_dashboard_needs_no_mapping(monkeypatch):
    monkeypatch.setattr(doctor, "settings", config.Settings({KEY: DASHBOARD}))
    monkeypatch.setattr(doctor, "_mem_serve_web", lambda: pytest.fail("no mapping needed"))
    result = doctor._mem_viewer(None, {})
    assert result[0] == "skip" and KEY in result[1]
