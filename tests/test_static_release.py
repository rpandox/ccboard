"""Release identities are external-script/data-attribute only, and fail closed."""
import json
import re

from app import main

H = {"Tailscale-User-Login": "alice@example.com"}


def test_index_and_boot_and_worker_share_the_board_release(lite_client):
    release = main.ASSET_VERSION
    index = lite_client.get("/", headers=H)
    assert f'data-release="{release}"' in index.text
    urls = re.findall(r'(?:src|href)="(/static/[^\"]+)"', index.text)
    assert urls and all(u.endswith(".woff2") or u.endswith("?v=" + release) for u in urls)
    assert all("?" not in u for u in urls if ".woff2" in u), "font preloads must match CSS references"
    assert "__ASSET_VERSION__" not in index.text
    assert not re.search(r'<script(?![^>]*\bsrc=)[^>]*>', index.text)
    assert index.headers["content-security-policy"] == "default-src 'self'; frame-ancestors 'none'"
    boot = lite_client.get(f"/static/boot.js?v={release}", headers=H)
    assert f"const BOOT_RELEASE = '{release}'" in boot.text
    assert "__ASSET_VERSION__" not in boot.text
    sw = lite_client.get("/sw.js", headers=H).text
    shell = json.loads(re.search(r"JSON.parse\('([^']+)'\)", sw)[1])
    assert set(shell) == {u + "?v=" + release for u in main.shell_paths()}
    assert all(lite_client.get(u, headers=H).status_code == 200 for u in shell)


def test_old_release_urls_never_return_current_release_bytes(lite_client):
    for path in ("/", "/static/boot.js", "/static/lazy.js", "/static/pages/home.css"):
        response = lite_client.get(path + "?v=previous-release", headers=H)
        assert response.status_code == 409
        assert response.headers["cache-control"] == "no-store"
