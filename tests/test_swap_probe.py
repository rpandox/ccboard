"""scripts/swap-probe.sh (#39), run for real against a stub curl and a stub date on PATH: no network, no box, nothing outside tmp_path."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.posix_sh      # runs the shell scripts under sh

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "scripts" / "swap-probe.sh"


def _exe(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def run_probe(tmp_path: Path, codes: list[str], step: float = 0.5, url: str | None = None) -> subprocess.CompletedProcess:
    """curl answers `codes` in turn (000 = no answer, exit 28 like a timeout); date ticks `step` seconds per call from 100.0."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (tmp_path / "codes").write_text("\n".join(codes) + "\n")
    (tmp_path / "clock").write_text("100.0\n")
    calls = tmp_path / "curl.log"
    _exe(bin_ / "curl", f'''echo "$*" >> "{calls}"
n=$(cat "{tmp_path}/n" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "{tmp_path}/n"
code=$(sed -n "${{n}}p" "{tmp_path}/codes")
if [ "$code" = 200 ]; then printf '200 0.004'; exit 0; fi
printf '000 1.001'; exit 28
''')
    _exe(bin_ / "date", f'''t=$(cat "{tmp_path}/clock")
awk -v t="$t" -v s="{step}" 'BEGIN {{ printf "%.1f\\n", t + s }}' > "{tmp_path}/clock"
printf '%s\\n' "$t"
''')
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "PROBE_INTERVAL": "0", "PROBE_COUNT": str(len(codes)), "LANG": "C"}
    args = [str(PROBE), "60"] + ([url] if url else [])
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=30)


def test_probe_reports_the_longest_gap_between_two_200_answers(tmp_path):
    r = run_probe(tmp_path, ["200", "000", "000", "200"])
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    rows = [ln.split() for ln in lines[:4]]
    assert [c for _t, c, _took in rows] == ["200", "000", "000", "200"], r.stdout
    assert "samples 4, 200 answers 2, other 2" in r.stdout
    # the clock moves 0.5 s per date call; the probe reads it once at the start and once per request: requests at 100.5 .. 102.0
    assert "longest gap between two 200 answers: 1.5 s (from 100.5 to 102.0)" in r.stdout, r.stdout
    curl_args = (tmp_path / "curl.log").read_text().splitlines()
    assert curl_args and all("-m 1" in a and a.endswith("http://127.0.0.1:8000/healthz") for a in curl_args), "1 s cap, the local /healthz by default"


def test_probe_with_no_outage_and_with_no_answer_at_all(tmp_path):
    up = tmp_path / "up"
    up.mkdir()
    r = run_probe(up, ["200", "200", "200"], url="http://127.0.0.1:9999/healthz")
    assert "samples 3, 200 answers 3, other 0" in r.stdout and "longest gap between two 200 answers: 0.5 s" in r.stdout, r.stdout
    assert all(a.endswith("http://127.0.0.1:9999/healthz") for a in (up / "curl.log").read_text().splitlines())
    down = tmp_path / "down"
    down.mkdir()
    r = run_probe(down, ["000", "000"])
    assert r.returncode == 0 and "longest gap between two 200 answers: none" in r.stdout, r.stdout


def test_probe_is_posix_sh_and_executable():
    assert os.access(PROBE, os.X_OK), "chmod +x scripts/swap-probe.sh"
    assert PROBE.read_text().startswith("#!/bin/sh\n")
    assert subprocess.run(["sh", "-n", str(PROBE)], capture_output=True).returncode == 0
    text = PROBE.read_text()
    assert "[[" not in text and "function " not in text, "no bashisms"
