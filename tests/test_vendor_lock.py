"""Every file under app/static/vendor is served with `Cache-Control: public, max-age=31536000, immutable` under its plain
URL (app/main.py is_immutable_static). The browser's HTTP cache is keyed by URL alone, so a vendor file whose bytes change
under the same name would stay stale for a year in any tab the service worker does not control (first visit, private
window). The rule: a changed vendor file gets a new name (and its references are updated); tests/vendor_lock.json pins
the bytes of every name that is in use. To add or rename a file, update the lock on purpose:

    python3 -c "import json,hashlib,pathlib; r=pathlib.Path('app/static/vendor'); print(json.dumps({p.relative_to(r).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(r.rglob('*')) if p.is_file()}, indent=1))" > tests/vendor_lock.json
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "app" / "static" / "vendor"
LOCK = ROOT / "tests" / "vendor_lock.json"


def _current() -> dict[str, str]:
    return {p.relative_to(VENDOR).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(VENDOR.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def test_vendor_files_never_change_bytes_under_the_same_name():
    lock = json.loads(LOCK.read_text())
    cur = _current()
    changed = sorted(n for n in cur if n in lock and lock[n] != cur[n])
    assert not changed, f"vendor files changed under the same name (served immutable for a year): {changed}; rename them and update tests/vendor_lock.json"
    new = sorted(n for n in cur if n not in lock)
    assert not new, f"new vendor files are not in tests/vendor_lock.json: {new}"
    gone = sorted(n for n in lock if n not in cur)
    assert not gone, f"tests/vendor_lock.json lists files that no longer exist: {gone}"


def test_lock_covers_the_served_immutable_set():
    from app import main
    assert main.is_immutable_static("/static/vendor/blueprint/blueprint.css")
    for name in json.loads(LOCK.read_text()):
        assert main.is_immutable_static("/static/vendor/" + name), name
