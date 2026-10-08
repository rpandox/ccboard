"""Reproducible Python builds: requirements.lock (runtime) and requirements-dev.lock (runtime + pytest + httpx) pin exact
versions with hashes and must satisfy the ranges in requirements.in. Regenerate with the commands in the README."""
from __future__ import annotations

import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent


def _ranges(path: Path) -> dict[str, Requirement]:
    out = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            r = Requirement(line)
            out[canonicalize_name(r.name)] = r
    return out


def _pins(path: Path) -> dict[str, tuple[str, int]]:
    """name -> (version, number of --hash entries) from a uv/pip-compile lock."""
    out, cur = {}, None
    for line in path.read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", line)
        if m:
            cur = canonicalize_name(m.group(1))
            out[cur] = (m.group(2), 0)
        elif cur and line.strip().startswith("--hash=sha256:"):
            v, n = out[cur]
            out[cur] = (v, n + 1)
    return out


def test_lock_satisfies_requirements_in():
    pins = _pins(ROOT / "requirements.lock")
    assert pins, "requirements.lock has no pins"
    for name, req in _ranges(ROOT / "requirements.in").items():
        assert name in pins, f"{name} is in requirements.in but not locked"
        assert req.specifier.contains(Version(pins[name][0]), prereleases=True), f"{name}=={pins[name][0]} violates {req.specifier}"


def test_every_lock_entry_has_an_exact_version_and_hashes():
    for lock in ("requirements.lock", "requirements-dev.lock"):
        for name, (version, hashes) in _pins(ROOT / lock).items():
            assert re.fullmatch(r"\d+(\.\d+)*([.-]?\w+)*", version), (lock, name, version)
            assert hashes >= 1, f"{lock}: {name} has no --hash entry"
        text = (ROOT / lock).read_text()
        assert "--universal" in text and "--python-version 3.14" in text and "--generate-hashes" in text, f"{lock}: generated on 3.14 (the image's Python) with hashes"


def test_dev_lock_extends_the_runtime_lock_without_changing_it():
    run, dev = _pins(ROOT / "requirements.lock"), _pins(ROOT / "requirements-dev.lock")
    for name, (version, _) in run.items():
        assert dev.get(name, (None,))[0] == version, f"{name}: the dev lock must pin the runtime version"
    for name in _ranges(ROOT / "requirements-dev.in"):
        assert name in dev, f"{name} missing from requirements-dev.lock"
    assert {"pytest", "httpx"} <= set(dev)


def test_requirements_txt_mirrors_requirements_in():
    """requirements.txt (ranges) stays for install.sh's systemd venv; it must not drift from requirements.in. The one allowed extra is a
    requirement that never applies on Linux (psutil for macOS and Windows, issue #116): the Linux venv and the image (requirements.lock)
    stay exactly as they were."""
    def norm(p):
        return [ln.strip() for ln in p.read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")]
    off_linux = [ln for ln in norm(ROOT / "requirements.txt") if Requirement(ln).marker is not None]
    assert [Requirement(ln).name for ln in off_linux] == ["psutil"] and not any(Requirement(ln).marker.evaluate({"sys_platform": "linux"}) for ln in off_linux)
    assert [ln for ln in norm(ROOT / "requirements.txt") if ln not in off_linux] == norm(ROOT / "requirements.in")
