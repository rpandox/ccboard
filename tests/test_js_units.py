"""Run the node unit tests under tests/js (node:test over a vm harness, no jsdom, no build step)."""
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_node_unit_tests_pass():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    # Files are listed explicitly: `node --test tests/js` (a bare directory) is no longer accepted by node 22+.
    files = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "tests" / "js").glob("*.test.mjs"))
    assert files, "no tests/js/*.test.mjs found"
    r = subprocess.run([node, "--test", *files], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"node --test tests/js failed (rc {r.returncode}):\n{r.stdout[-6000:]}\n{r.stderr[-2000:]}"
