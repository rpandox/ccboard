"""Every app-owned script must parse. `node --check` is the cheapest guard against shipping a syntax error to the phone."""
import pathlib
import shutil
import subprocess

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "app" / "static"
EXCLUDED_TOP = {"vendor", "demo"}


def app_js():
    return [p for p in sorted(STATIC.rglob("*.js"))
            if p.relative_to(STATIC).parts[0] not in EXCLUDED_TOP and "__pycache__" not in p.parts]


@pytest.mark.parametrize("path", app_js(), ids=lambda p: p.relative_to(STATIC).as_posix())
def test_node_check(path):
    """sw.js counts: it is a template, but must parse before /sw.js substitutes the build id and the shell list."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"{path.relative_to(STATIC.parent.parent)} does not parse:\n{r.stderr}"


def test_the_scan_finds_the_scripts():
    names = {p.relative_to(STATIC).as_posix() for p in app_js()}
    assert {"core.js", "sw.js"} <= names, names
