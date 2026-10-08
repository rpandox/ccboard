"""scripts/qa-ui.sh Lighthouse strict mode (issue #103). Nothing here runs a real browser or a real Lighthouse: the browse CLI is absent (so the script
takes the screenshots-only path with a fake Chrome) and Lighthouse is a shell script that writes the JSON a real one would.

What is pinned: a strict run that cannot find Lighthouse FAILS and says it measured nothing, a non-strict one skips; the scores are medians of
QA_LIGHTHOUSE_RUNS runs for every page and both presets; a median under target FAILS in strict mode only; a run that writes no report FAILS in strict mode;
the pwa category is dropped gracefully (Lighthouse 12); the table is written. The real scores stay unmeasured until someone installs Lighthouse."""
import http.server
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "qa-ui.sh"


class _Board(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"dev": {"sandboxed": True}}).encode() if self.path.startswith("/api/state") else b"<html></html>"
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if self.path.startswith("/api/") else "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


FAKE_LH = r'''#!/bin/sh
# a stand-in for lighthouse: --version, or write the JSON report a real one would. Scores come from FAKE_LH_<PRESET>="perf pwa a11y" (default all 95/96/97),
# FAKE_LH_NOPWA=1 refuses the pwa category (Lighthouse 12), FAKE_LH_NOREPORT=1 writes nothing, FAKE_LH_SEQ is a file of per-run performance scores.
[ "$1" = "--version" ] && { echo "12.6.1"; exit 0; }
out=""; cats=""; preset=mobile
for a in "$@"; do
  case "$a" in
    --output-path=*) out="${a#--output-path=}";;
    --only-categories=*) cats="${a#--only-categories=}";;
    --preset=desktop) preset=desktop;;
  esac
done
echo "$*" >> "${FAKE_LH_LOG:-/dev/null}"
[ "${FAKE_LH_NOREPORT:-0}" = 1 ] && exit 1
case "$cats" in *pwa*) [ "${FAKE_LH_NOPWA:-0}" = 1 ] && exit 1;; esac
if [ "$preset" = desktop ]; then s="${FAKE_LH_DESKTOP:-95 96 97}"; else s="${FAKE_LH_MOBILE:-92 96 97}"; fi
set -- $s
perf=$1; pwa=$2; a11y=$3
if [ -n "${FAKE_LH_SEQ:-}" ] && [ "$preset" = mobile ]; then
  n=$(cat "$FAKE_LH_SEQ.n" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "$FAKE_LH_SEQ.n"
  perf=$(sed -n "${n}p" "$FAKE_LH_SEQ")
fi
pwa_json=""; case "$cats" in *pwa*) pwa_json=",\"pwa\":{\"score\":$(awk "BEGIN{print $pwa/100}")}";; esac
printf '{"categories":{"performance":{"score":%s},"accessibility":{"score":%s}%s}}' "$(awk "BEGIN{print $perf/100}")" "$(awk "BEGIN{print $a11y/100}")" "$pwa_json" > "$out"
'''


@pytest.fixture
def qa(tmp_path):
    for tool in ("bash", "curl", "python3", "awk"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} is not installed")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Board)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    chrome = tmp_path / "chrome"
    chrome.write_text('#!/bin/sh\nfor a in "$@"; do case "$a" in --screenshot=*) printf png > "${a#--screenshot=}";; esac; done\nexit 0\n')
    chrome.chmod(0o755)
    lh = tmp_path / "lighthouse"
    lh.write_text(FAKE_LH)
    lh.chmod(0o755)

    def run(**env_over):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("QA_", "FAKE_LH", "LIGHTHOUSE"))}
        env.update(B=str(tmp_path / "no-browse"), CHROME=str(chrome), QA_WIDTHS="390", QA_SETTLE="0", LIGHTHOUSE_BIN=str(lh), FAKE_LH_LOG=str(tmp_path / "lh.log"))
        env.update({k: str(v) for k, v in env_over.items()})
        out = tmp_path / "out"
        r = subprocess.run(["bash", str(SCRIPT), f"http://127.0.0.1:{srv.server_address[1]}", str(out)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=240)
        return r, out

    try:
        yield SimpleNs(run=run, lh=lh, log=tmp_path / "lh.log", tmp=tmp_path)
    finally:
        srv.shutdown()


class SimpleNs:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_strict_mode_without_lighthouse_fails_and_says_nothing_was_measured(qa):
    r, out = qa.run(QA_LIGHTHOUSE_STRICT=1, LIGHTHOUSE_BIN=str(qa.tmp / "missing"))
    assert r.returncode == 1, r.stdout[-1500:]
    assert "NOT FOUND" in r.stdout and "nothing was measured" in r.stdout and "Lighthouse was not found" in r.stdout
    assert not (out / "lighthouse-summary.tsv").exists()


def test_without_strict_a_missing_lighthouse_is_skipped_out_loud(qa):
    r, out = qa.run(LIGHTHOUSE_BIN=str(qa.tmp / "missing"))
    assert r.returncode == 0, r.stdout[-1500:]
    assert "lighthouse:" in r.stdout and "skipped" in r.stdout and "NOT FOUND" not in r.stdout


def test_qa_lighthouse_0_skips_even_in_strict_mode(qa):
    r, _ = qa.run(QA_LIGHTHOUSE=0, QA_LIGHTHOUSE_STRICT=1, LIGHTHOUSE_BIN=str(qa.tmp / "missing"))
    assert r.returncode == 0 and "lighthouse:" not in r.stdout and "NOT FOUND" not in r.stdout and "lighthouse table" not in r.stdout


def test_strict_mode_runs_every_page_and_preset_three_times_and_passes_at_the_targets(qa):
    r, out = qa.run(QA_LIGHTHOUSE_STRICT=1)
    assert r.returncode == 0, r.stdout[-2500:]
    assert "lighthouse 12.6.1" in r.stdout and "3 run(s) per page and preset" in r.stdout
    rows = (out / "lighthouse-summary.tsv").read_text().strip().splitlines()
    assert rows[0].startswith("page\tpreset") and len(rows) == 1 + 6 * 2, rows
    assert {r.split("\t")[0] for r in rows[1:]} == {"home", "agents", "usage", "project", "settings", "terminal"}
    desktop = next(x for x in rows if x.startswith("home\tdesktop")).split("\t")
    assert desktop[2:5] == ["95/95", "96/96", "97/97"] and desktop[6] == "3"
    calls = qa.log.read_text().splitlines()
    assert len(calls) == 6 * 2 * 3, "one call per page, preset and run (the pwa category was accepted)"
    assert any("--preset=desktop" in c for c in calls) and any("#/p/phasezero" in c for c in calls) and any("/term/" in c for c in calls)


def test_a_median_under_target_fails_in_strict_mode_and_is_advisory_otherwise(qa):
    r, _ = qa.run(QA_LIGHTHOUSE_STRICT=1, FAKE_LH_MOBILE="70 96 97", QA_LIGHTHOUSE_PAGES="home|http://x/a")
    assert r.returncode == 1 and "lighthouse below target: home/mobile:performance=70<90" in r.stdout, r.stdout[-1500:]
    assert "lighthouse scores under target" in r.stdout
    r, _ = qa.run(FAKE_LH_MOBILE="70 96 97", QA_LIGHTHOUSE_PAGES="home|http://x/a")
    assert r.returncode == 0 and "lighthouse below target:" in r.stdout, "advisory without strict"


def test_desktop_accessibility_and_pwa_targets_are_held_too(qa):
    r, _ = qa.run(QA_LIGHTHOUSE_STRICT=1, FAKE_LH_DESKTOP="95 90 93", QA_LIGHTHOUSE_PAGES="home|http://x/a", QA_LIGHTHOUSE_RUNS=1)
    assert r.returncode == 1
    assert "home/desktop:pwa=90<95" in r.stdout and "home/desktop:accessibility=93<95" in r.stdout and "performance=95" not in r.stdout


def test_the_median_decides_not_one_bad_run(qa):
    seq = qa.tmp / "seq"
    seq.write_text("60\n95\n96\n")                      # three mobile runs: 60 95 96 -> median 95, worst 60
    r, out = qa.run(QA_LIGHTHOUSE_STRICT=1, QA_LIGHTHOUSE_PAGES="home|http://x/a", FAKE_LH_SEQ=seq)
    assert r.returncode == 0, r.stdout[-1500:]
    mobile = next(x for x in (out / "lighthouse-summary.tsv").read_text().splitlines() if x.startswith("home\tmobile")).split("\t")
    assert mobile[2] == "95/60", "the median is held to the target, the worst is recorded"


def test_a_lighthouse_without_the_pwa_category_is_asked_for_the_other_two(qa):
    r, out = qa.run(QA_LIGHTHOUSE_STRICT=1, FAKE_LH_NOPWA=1, QA_LIGHTHOUSE_PAGES="home|http://x/a", QA_LIGHTHOUSE_RUNS=1)
    assert r.returncode == 0, r.stdout[-1500:]
    row = next(x for x in (out / "lighthouse-summary.tsv").read_text().splitlines() if x.startswith("home\tdesktop")).split("\t")
    assert row[3] == "n/a", "no pwa score, and n/a is never a miss"
    assert "--only-categories=performance,accessibility" in qa.log.read_text()


def test_strict_mode_fails_when_a_run_writes_no_report(qa):
    r, _ = qa.run(QA_LIGHTHOUSE_STRICT=1, FAKE_LH_NOREPORT=1, QA_LIGHTHOUSE_PAGES="home|http://x/a", QA_LIGHTHOUSE_RUNS=1)
    assert r.returncode == 1 and "no report was written, nothing was measured" in r.stdout
    r, _ = qa.run(FAKE_LH_NOREPORT=1, QA_LIGHTHOUSE_PAGES="home|http://x/a")
    assert r.returncode == 0 and "no report written" in r.stdout, "advisory without strict"
