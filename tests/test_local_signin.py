"""Direct loopback sign-in: generated credentials, temporary homes, no real services."""
import hashlib
import logging
import secrets
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app import local_signin as local
from app.config import Settings, settings

ROOT = Path(__file__).resolve().parents[1]
CSRF = {"X-CCBoard": "1"}


@pytest.fixture
def browser(lite_client, monkeypatch):
    monkeypatch.setattr(settings, "local_signin", True)
    monkeypatch.setattr(settings, "local_signin_user", "owner")
    monkeypatch.setattr(settings, "allowed_users", {"owner", "second"})
    monkeypatch.setattr(settings, "local_signin_hours", 12)
    monkeypatch.setattr(lite_client._transport, "client", ("127.0.0.1", 50000))
    lite_client.base_url = f"http://127.0.0.1:{settings.port}"
    local.clear()
    yield lite_client
    local.clear()


def mint(c):
    from app import hooks
    r = c.post('/auth/local/token', headers={"X-CCBoard-Token": hooks.ensure_token()})
    assert r.status_code == 204
    assert r.content == b"" and r.headers['cache-control'] == 'no-store'
    return r.headers[local.TOKEN_HEADER]


def redeem(c, token, headers=None):
    return c.post('/auth/local/redeem', content=token, headers={**CSRF, **(headers or {})})


def login(c):
    token = mint(c)
    r = redeem(c, token)
    assert r.status_code == 204
    return token, c.cookies.get(local.COOKIE), r


def test_single_use_and_hash_only_storage(browser):
    token, cookie, r = login(browser)
    assert redeem(browser, token).status_code == 403
    assert len(cookie) == 43 and len(token) == 43
    assert token not in repr(local._tokens) and cookie not in repr(local._sessions)
    assert hashlib.sha256(cookie.encode()).hexdigest() in local._sessions
    header = r.headers['set-cookie']
    for flag in ('HttpOnly', 'SameSite=strict', 'Path=/', 'Max-Age=43200'):
        assert flag in header
    assert 'Domain=' not in header
    assert browser.get('/api/auth/local/sessions').status_code == 200
    assert browser.get('/api/state').json()['user'] == 'owner'


@pytest.fixture
def clock(monkeypatch):
    clocks = [10000.0, 100.0]  # wall, monotonic; fake only local sign-in's clock
    monkeypatch.setattr(local, '_clock', lambda: tuple(clocks))
    return clocks


@pytest.mark.parametrize('elapsed', [(60, 60), (61, 0), (-3600, 61)],
                         ids=['deadline', 'sleep', 'wall-clock-rollback'])
def test_link_expires_at_sixty_seconds(browser, clock, elapsed):
    token = mint(browser)
    clock[0] += 59
    with local._lock:
        local._prune(local._clock())
    assert local.digest(token) in local._tokens
    clock[:] = [10000.0 + elapsed[0], 100.0 + elapsed[1]]
    assert redeem(browser, token).status_code == 403
    assert not local._tokens


@pytest.mark.parametrize('elapsed', [(3600, 3600), (3601, 10), (-3600, 3601)],
                         ids=['deadline', 'sleep', 'wall-clock-rollback'])
def test_session_expires_and_seen_updates(browser, monkeypatch, clock, elapsed):
    monkeypatch.setattr(settings, 'local_signin_hours', 1)
    _, cookie, r = login(browser)
    assert 'Max-Age=3600' in r.headers['set-cookie']
    before = browser.get('/api/auth/local/sessions').json()['sessions'][0]
    assert before == {'created': 10000.0, 'last_seen': 10000.0}
    clock[0] += 10
    clock[1] += 10
    after = browser.get('/api/auth/local/sessions').json()['sessions'][0]
    assert after['created'] == before['created'] and after['last_seen'] > before['last_seen']
    clock[:] = [10000.0 + elapsed[0], 100.0 + elapsed[1]]
    # Send the cookie explicitly: test server expiry independently of the cookie jar.
    assert browser.get('/api/auth/local/sessions', headers={'Cookie': f'{local.COOKIE}={cookie}'}).status_code == 403
    assert not local._sessions


BAD_HEADERS = [
    {'Host': '192.0.2.1:{port}'}, {'Host': 'evil.example:{port}'},
    {'Host': '127.0.0.1'}, {'Host': 'localhost:1'}, {'Host': 'localhost.:{port}'},
    {'X-Forwarded-For': '127.0.0.1'}, {'X-Forwarded-Host': 'localhost'},
    {'X-Forwarded-Anything': ''}, {'Forwarded': 'for=127.0.0.1'},
    {'Tailscale-User-Login': ''}, {'Tailscale-User-Login': 'unknown'},
    {'Tailscale-User-Name': 'owner'}, {'Tailscale-Anything': ''},
    {'Origin': 'http://evil.example'}, {'Origin': 'null'},
]


@pytest.mark.parametrize('headers', BAD_HEADERS)
def test_redeem_and_cookie_refuse_foreign_or_proxy_headers(browser, headers):
    headers = {k: v.format(port=settings.port) for k, v in headers.items()}
    login(browser)
    token = mint(browser)
    assert redeem(browser, token, headers).status_code == 403
    assert browser.get('/api/auth/local/sessions', headers=headers).status_code == 403
    assert redeem(browser, token).status_code == 204, 'a denied context must not consume the link'


@pytest.mark.parametrize('condition', ['off', 'remote', 'empty', 'missing-peer', 'removed-user'])
def test_redeem_and_cookie_refuse_invalid_context(browser, monkeypatch, condition):
    login(browser)
    token = mint(browser)
    if condition == 'off':
        monkeypatch.setattr(settings, 'local_signin', False)
    elif condition == 'empty':
        monkeypatch.setattr(settings, 'allowed_users', set())
    elif condition == 'removed-user':
        monkeypatch.setattr(settings, 'allowed_users', {'second'})
    else:
        monkeypatch.setattr(browser._transport, 'client', None if condition == 'missing-peer' else ('192.0.2.1', 50000))
    assert redeem(browser, token).status_code == 403
    assert browser.get('/api/auth/local/sessions').status_code == 403


def test_cookie_post_needs_csrf_even_same_origin(browser):
    login(browser)
    for headers in ({}, {'Origin': 'http://evil.example'}):
        assert browser.delete('/api/auth/local/sessions', headers=headers).status_code == 403
        assert browser.post('/api/projects', headers=headers, json={'name': 'denied'}).status_code == 403
    assert browser.get('/api/auth/local/sessions').json()['sessions']


def test_sign_out_all_revokes_every_cookie_and_pending_link(browser):
    _, first, _ = login(browser)
    _, second, _ = login(browser)
    pending = mint(browser)
    assert len(browser.get('/api/auth/local/sessions').json()['sessions']) == 2
    assert browser.delete('/api/auth/local/sessions', headers=CSRF).status_code == 200
    for cookie in (first, second):
        assert browser.get('/api/auth/local/sessions', headers={'Cookie': f'{local.COOKIE}={cookie}'}).status_code == 403
    assert redeem(browser, pending).status_code == 403


def test_only_signed_in_person_can_list_or_revoke(browser):
    from app import hooks
    login(browser)
    h = {'X-CCBoard-Token': hooks.ensure_token(), **CSRF}
    assert browser.get('/api/auth/local/sessions', headers=h).status_code == 403
    assert browser.delete('/api/auth/local/sessions', headers=h).status_code == 403
    assert browser.get('/api/auth/local/sessions').json()['sessions']


def test_mint_needs_local_hook_token_not_browser_identity(browser):
    assert browser.post('/auth/local/token', headers={'Tailscale-User-Login': 'owner'}).status_code == 403
    login(browser)
    assert browser.post('/auth/local/token', headers=CSRF).status_code == 403
    from app import hooks
    for bad in BAD_HEADERS:
        h = {k: v.format(port=settings.port) for k, v in bad.items()}
        assert browser.post('/auth/local/token', headers={**h, 'X-CCBoard-Token': hooks.ensure_token()}).status_code == 403


def test_duplicates_are_refused(browser):
    token = mint(browser)
    for headers in ([('Host', f'127.0.0.1:{settings.port}'), ('Host', f'localhost:{settings.port}'), ('X-CCBoard', '1')],
                    [('Origin', f'http://127.0.0.1:{settings.port}')] * 2 + [('X-CCBoard', '1')]):
        assert browser.post('/auth/local/redeem', content=token, headers=headers).status_code == 403


@pytest.mark.parametrize('host,peer', [('127.0.0.1', '127.0.0.1'), ('localhost', '127.0.0.1'), ('[::1]', '::1')])
def test_exact_loopback_names_work(browser, monkeypatch, host, peer):
    browser.base_url = f'http://{host}:{settings.port}'
    monkeypatch.setattr(browser._transport, 'client', (peer, 50000))
    token = mint(browser)
    assert redeem(browser, token, {'Origin': f'http://{host}:{settings.port}'}).status_code == 204
    assert browser.get('/api/auth/local/sessions').status_code == 200


def test_tailscale_provider_still_wins(browser):
    login(browser)
    from app.auth import identify
    from starlette.requests import Request
    r = Request({'type': 'http', 'headers': [(b'cookie', f'{local.COOKIE}={browser.cookies.get(local.COOKIE)}'.encode())]})
    assert identify({'tailscale-user-login': 'second'}, settings, r) == 'second'
    assert identify({'tailscale-user-login': 'unknown'}, settings, r) is None


def test_simultaneous_redemptions_only_one_succeeds(browser):
    token = mint(browser)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: redeem(browser, token).status_code, range(2)))
    assert sorted(results) == [204, 403]


@pytest.mark.parametrize('runtime', ['systemd', 'docker', 'host', 'launchd'])
def test_explicit_enable_only_and_first_login(runtime):
    base = {'CCBOARD_RUNTIME': runtime, 'CCBOARD_ALLOWED_USERS': ' , Second, first', 'CCBOARD_ENV_FILE': ''}
    assert Settings(base).local_signin is False
    assert Settings({**base, 'CCBOARD_LOCAL_SIGNIN': '0'}).local_signin is False
    config = Settings({**base, 'CCBOARD_LOCAL_SIGNIN': '1'})
    assert config.local_signin is True and config.local_signin_user == 'second'
    assert config.local_signin_hours == 12


@pytest.mark.parametrize('hours', ['0', '169', '-1', 'bad', '1.5'])
def test_hours_invalid_fail_closed(hours):
    with pytest.raises(ValueError):
        Settings({'CCBOARD_LOCAL_SIGNIN_HOURS': hours, 'CCBOARD_ENV_FILE': ''})


def test_no_credential_in_logs_bodies_or_audit(browser, caplog):
    caplog.set_level(logging.DEBUG)
    token, cookie, response = login(browser)
    bodies = [response.text, redeem(browser, token).text]
    for path in ('/api/auth/local/sessions', '/api/state', '/api/nodes/audit'):
        bodies.append(browser.get(path).text)
    bodies.append(browser.delete('/api/auth/local/sessions', headers=CSRF).text)
    blob = '\n'.join(bodies) + caplog.text
    for secret in (token, cookie, local.digest(token), local.digest(cookie)):
        assert secret not in blob
    assert 'local' in caplog.text  # HTTP requests were actually captured
    import sqlite3
    with sqlite3.connect(settings.db_path) as con:
        dump = '\n'.join(con.iterdump())
    assert token not in dump and cookie not in dump


def test_doctor_warning_and_page_csp(browser, monkeypatch):
    from app import doctor
    assert doctor._c_local_signin(None).status == 'warn'
    assert any(row[0] == 'local-signin' for row in doctor.CHECKS)
    for path in ('/auth/local', '/auth/local/script.js'):
        r = browser.get(path)
        assert r.status_code == 200
        assert "default-src 'self'" in r.headers['content-security-policy']
        assert "frame-ancestors 'none'" in r.headers['content-security-policy']
        assert r.headers['cache-control'] == 'no-store'
    monkeypatch.setattr(settings, 'local_signin', False)
    assert doctor._c_local_signin(None).status == 'pass'
    assert browser.get('/auth/local').status_code == 403


@pytest.fixture
def enabled_start(monkeypatch, caplog):
    monkeypatch.setattr(settings, 'local_signin', True)
    caplog.set_level(logging.WARNING, logger='ccboard')


def test_startup_warns_once(enabled_start, client, caplog):
    rows = caplog.get_records('setup') + caplog.records
    assert sum('local browser sign-in enabled' in r.getMessage() for r in rows) == 1


def test_redeem_needs_csrf_and_never_reflects_bad_input(browser):
    token = mint(browser)
    r = browser.post('/auth/local/redeem', content=token)
    assert r.status_code == 403 and token not in r.text
    for body in (secrets.token_urlsafe(32), secrets.token_urlsafe(160), b'\xff'):
        r = redeem(browser, body)
        assert r.status_code == 403
    assert redeem(browser, token).status_code == 204


@pytest.mark.parametrize('platform,opener', [('Darwin', 'open'), ('Linux', 'xdg-open'), ('WSL', 'wslview'), ('WSL', 'cmd.exe'), ('Linux', None)])
def test_launcher_uses_loopback_fragment_and_platform_opener(tmp_path, platform, opener):
    fake = tmp_path / 'bin'
    fake.mkdir()
    token = secrets.token_urlsafe(32)
    hook = secrets.token_urlsafe(32)
    (tmp_path / 'hook-token').write_text(hook)
    for command in ('cat', 'awk'):
        import shutil
        (fake / command).symlink_to(shutil.which(command))
    def exe(name, body):
        p = fake / name
        p.write_text('#!/bin/sh\n' + body)
        p.chmod(0o755)
    exe('uname', f"printf '%s\\n' '{platform}'\n")
    exe('curl', 'cat > "$TEST_DIR/request-headers"\nprintf "%s\\n" "$@" > "$TEST_DIR/args"\nprintf "HTTP/1.1 204 No Content\\r\\nX-CCBoard-Local-Link: %s\\r\\n\\r\\n" "$TEST_TOKEN"\n')
    if opener:
        exe(opener, 'printf "%s\\n" "$@" > "$TEST_DIR/opened"\n')
    env = {'HOME': str(tmp_path), 'PATH': str(fake), 'CCBOARD_PORT': '8123', 'CCBOARD_HOOK_TOKEN_FILE': str(tmp_path / 'hook-token'),
           'TEST_DIR': str(tmp_path), 'TEST_TOKEN': token}
    if platform == 'WSL':
        env['WSL_DISTRO_NAME'] = 'test'
    r = subprocess.run(['/bin/sh', str(ROOT / 'bin/ccboard-open')], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    args = (tmp_path / 'args').read_text()
    assert 'http://127.0.0.1:8123/auth/local/token' in args
    assert hook not in args and '--noproxy' in args and '-q' in args
    assert hook in (tmp_path / 'request-headers').read_text()
    url = f'http://127.0.0.1:8123/auth/local#{token}'
    if opener:
        assert url in (tmp_path / 'opened').read_text() and not r.stdout
    else:
        assert r.stdout.strip() == url


def test_config_cli_open_uses_loaded_settings(monkeypatch):
    from app import config
    seen = []
    monkeypatch.setattr(subprocess, 'call', lambda args, env: seen.append((args, env)) or 0)
    assert config._main(['open']) == 0
    args, env = seen[0]
    assert args == [str(ROOT / 'bin/ccboard-open')]
    assert env['CCBOARD_PORT'] == str(settings.port)
    assert env['CCBOARD_DATA_DIR'] == str(settings.data_dir)
