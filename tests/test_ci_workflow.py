"""The GitHub Actions workflow builds the image only after the tests pass, with package write rights on that job only."""
import pathlib
import re

import pytest

CI = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def _job(text, name):
    m = re.search(rf"^  {name}:\n(.*?)(?=^  [\w-]+:\n|\Z)", text, re.S | re.M)
    assert m, f"job {name} not found"
    return m.group(1)


def test_image_job_is_gated_by_tests_and_scoped():
    text = CI.read_text()
    assert "\npermissions:\n  contents: read\n" in text or re.search(r"^permissions:\n  contents: read", text, re.M)
    image = _job(text, "image")
    assert re.search(r"needs:\s*\[?\s*test", image)
    assert "packages: write" in image
    assert "packages: write" not in _job(text, "test")
    assert "ghcr.io" in image and "GITHUB_TOKEN" in image
    for tag in ("type=raw,value=latest", "type=sha", "prefix=sha-"):
        assert tag in image, tag
    assert "([[:space:]]|$)" in image   # v0.5.1-docker must not re-tag v0.5.1


def test_image_build_args_match_the_dockerfile():
    text = CI.read_text()
    docker = (CI.parent.parent.parent / "Dockerfile").read_text()
    for arg in ("CCBOARD_VERSION", "CCBOARD_REVISION"):
        assert arg in text and f"ARG {arg}" in docker, arg


def test_the_image_carries_its_source_repository_and_commit_for_the_doctor():
    """#50: the doctor's GitHub Actions check needs the repository and the commit the image was built from; the image job passes both
    (never a name written in the source), the Dockerfile turns them into environment variables, and a local build leaves them empty."""
    text = CI.read_text()
    docker = (CI.parent.parent.parent / "Dockerfile").read_text()
    image = _job(text, "image")
    assert image.count("CCBOARD_SOURCE_REPO=${{ github.repository }}") == 2        # the smoke build and the pushed build
    assert image.count("CCBOARD_IMAGE_REVISION=${{ github.sha }}") == 2
    assert "ARG CCBOARD_SOURCE_REPO=\n" in docker and "ARG CCBOARD_IMAGE_REVISION=\n" in docker      # empty by default
    assert "CCBOARD_SOURCE_REPO=${CCBOARD_SOURCE_REPO}" in docker and "CCBOARD_IMAGE_REVISION=${CCBOARD_IMAGE_REVISION}" in docker
    assert "ENV CCBOARD_IMAGE_VERSION=${CCBOARD_VERSION}" in docker                  # unchanged


# ---------------------------------------------------------------- v0.5.x: pinned actions, smoke before push, arm64
def _uses(text):
    return re.findall(r"^\s*(?:- )?uses:\s*(\S+)(.*)$", text, re.M)


def test_every_action_is_pinned_to_a_commit_with_the_version_in_a_comment():
    uses = _uses(CI.read_text())
    assert len(uses) >= 10
    for ref, rest in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), f"{ref} must be pinned by full commit sha"
        assert re.fullmatch(r"\s+# v\d+\.\d+\.\d+", rest), f"{ref} needs its version in a trailing comment, got {rest!r}"


def _run_blocks(text):
    """Every `run:` value (single line or block scalar) of the workflow."""
    lines, out, i = text.splitlines(), [], 0
    while i < len(lines):
        m = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", lines[i])
        if not m:
            i += 1
            continue
        indent, first, block = len(m.group(1)), m.group(2), []
        if first in ("|", ">", "|-", ">-"):
            i += 1
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
                block.append(lines[i])
                i += 1
        else:
            block.append(first)
            i += 1
        out.append("\n".join(block))
    return out


def test_run_steps_interpolate_no_expressions():
    """Untrusted input (commit message, ref names) reaches a shell only through `env:`, never through ${{ }} in run."""
    runs = _run_blocks(CI.read_text())
    assert len(runs) >= 4
    for block in runs:
        assert "${{" not in block, block


def test_installs_use_the_hashed_locks():
    text = CI.read_text()
    assert "pip install --require-hashes -r requirements-dev.lock" in text
    for block in _run_blocks(text):
        for m in re.finditer(r"pip install ([^\n]*)", block):
            line = m.group(1)
            assert "--require-hashes" in line or re.search(r"\S==\d", line), f"unpinned install: {line}"
    assert "requirements.txt pytest httpx" not in text


def test_image_job_smoke_tests_before_any_push_and_publishes_both_platforms():
    image = _job(CI.read_text(), "image")
    load = image.index("load: true")
    smoke = image.index("scripts/ci-smoke.sh")
    push = image.index("push: true")
    assert load < smoke < push, "build+load, smoke, then push"
    assert image.count("push: true") == 1 and "push: false" in image[:smoke]
    first_build = image[:smoke]
    assert "platforms: linux/amd64\n" in first_build and "tags: ccboard:ci" in first_build
    assert "platforms: linux/amd64,linux/arm64" in image[smoke:]
    assert "docker/setup-qemu-action@" in image[:smoke]
    # the smoke step must fail the job: no continue-on-error, no `|| true`
    step = image[smoke - 200:smoke + 60]
    assert "continue-on-error" not in step and "|| true" not in image[smoke:smoke + 80]
    # the metadata (all three tags) goes only to the push step
    assert image.index("tags: ${{ steps.meta.outputs.tags }}") > smoke
    assert image.count("CCBOARD_REVISION=${{ github.sha }}") == 2   # both builds carry the same build args


def test_pull_requests_build_without_pushing_and_stay_amd64():
    text = CI.read_text()
    check = _job(text, "image-check")
    assert "github.event_name == 'pull_request'" in check
    assert "push: false" in check and "push: true" not in check and "packages: write" not in check
    assert "platforms: linux/amd64\n" in check and "arm64" not in check and "login-action" not in check
    # a PR proves the image starts: loaded locally and smoke-tested, as the push job does before its tags move
    assert "load: true" in check and "tags: ccboard:ci" in check
    assert check.index("tags: ccboard:ci") < check.index("run: scripts/ci-smoke.sh ccboard:ci")
    image = _job(text, "image")
    assert "github.event_name == 'push'" in image
    assert "packages: write" not in _job(text, "image-check") and "packages: write" not in _job(text, "test")


def test_dependabot_is_weekly_and_grouped():
    path = CI.parent.parent / "dependabot.yml"
    text = path.read_text()
    for eco in ("pip", "github-actions", "docker"):
        assert f"package-ecosystem: {eco}" in text, eco
    assert text.count("interval: weekly") == 3 and text.count("groups:") == 3 and text.count("labels:") == 3


# ---------------------------------------------------------------- issue #123: the advisory macOS job and the deploy gate
def _needs(job_text):
    m = re.search(r"^    needs:\s*(.+)$", job_text, re.M)
    return m.group(1).strip() if m else None


def test_the_image_jobs_keep_exactly_their_needs():
    """The macOS job is advisory: the image the box pulls must wait for the Ubuntu tests and for nothing else."""
    text = CI.read_text()
    assert _needs(_job(text, "image")) == "test"
    assert _needs(_job(text, "image-check")) == "test"
    for name in ("image", "image-check"):
        assert "test-macos" not in _job(text, name) and "changes" not in _job(text, name), name


def test_packages_write_stays_on_the_image_job_alone():
    text = CI.read_text()
    jobs = re.findall(r"^  ([\w-]+):\n", text[text.index("\njobs:\n"):], re.M)
    assert {"test", "test-macos", "changes", "image", "image-check"} <= set(jobs)
    assert [j for j in jobs if "packages: write" in _job(text, j)] == ["image"]
    assert [j for j in jobs if re.search(r"\$\{\{\s*secrets\.", _job(text, j))] == ["image"]


def test_macos_job_is_advisory_read_only_and_secret_free():
    text = CI.read_text()
    mac = _job(text, "test-macos")
    assert re.search(r"^    runs-on: macos-latest$", mac, re.M) and re.search(r"^    timeout-minutes: 45$", mac, re.M)
    assert re.search(r"^    continue-on-error: true$", mac, re.M)           # until the promotion commit edits this and the pin
    assert re.search(r"^    permissions:\n      contents: read\n(?!      )", mac, re.M)      # contents: read and nothing else
    assert "packages:" not in mac and not re.search(r"\$\{\{\s*secrets\.", mac) and "GITHUB_TOKEN" not in mac and "login-action" not in mac
    assert _needs(mac) == "changes"


def test_macos_job_installs_and_runs_the_suite_in_a_sandbox():
    mac = _job(CI.read_text(), "test-macos")
    assert "brew install tmux shellcheck" in mac
    assert 'python-version: "3.14"' in mac and 'node-version: "22"' in mac
    assert "pip install --require-hashes -r requirements-dev.lock" in mac
    assert 'CCBOARD_TEST_NO_CLAUDE: "1"' in mac
    assert "HOME: ${{ runner.temp }}/home" in mac and "TMUX_TMPDIR: ${{ runner.temp }}/tmux" in mac
    assert re.search(r"^          CCBOARD_TMUX_SOCKET: \S*ccboard\S*$", mac, re.M)       # test_doctor expects the word in the socket name
    assert "python -m pytest -q" in mac
    for check in ("sh -n", "bash -n"):
        assert check in mac, check
    assert "bin/*" in mac and "scripts/*.sh" in mac


def test_macos_job_skips_documentation_only_changes():
    text = CI.read_text()
    assert re.search(r"^    if: needs\.changes\.outputs\.code == 'true'$", _job(text, "test-macos"), re.M)
    scope = _job(text, "changes")
    assert "fetch-depth: 0" in scope and "code=$code" in scope
    for case in ("|scripts/*|bin/*|install.sh) code=true", "*.md|docs/*) ;;"):
        assert case in scope, case
    assert scope.index("scripts/*") < scope.index("*.md|docs/*"), "a workflow or script under docs/ still counts"
    assert "${{" not in "\n".join(_run_blocks(scope))          # the shas arrive through env


def _scope_after(tmp_path, changed, event="push"):
    """Run the `changes` job's script in a throwaway repository whose second commit touches `changed`; the value it writes for `code`."""
    import os
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

    def git(*a):
        return subprocess.run(["git", *a], cwd=repo, env=env, capture_output=True, text=True, check=True).stdout.strip()
    git("init", "-q", "-b", "main")
    (repo / "README.md").write_text("one\n")
    git("add", ".")
    git("commit", "-q", "-m", "one")
    first = git("rev-parse", "HEAD")
    for rel in changed:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("two\n")
    git("add", ".")
    git("commit", "-q", "-m", "two")
    out = tmp_path / "out"
    script = _run_blocks(_job(CI.read_text(), "changes"))[0]
    run_env = {**env, "EVENT": event, "PR_BASE": first if event == "pull_request" else "", "PUSH_BEFORE": first if event == "push" else "",
               "GITHUB_OUTPUT": str(out)}
    subprocess.run(["bash", "-e", "-c", script], cwd=repo, env=run_env, check=True, capture_output=True)
    return out.read_text().strip()


@pytest.mark.parametrize("event", ["push", "pull_request"])
@pytest.mark.parametrize("changed,code", [
    (["README.md"], "code=false"), (["docs/guide.md", "docs/img/a.png"], "code=false"), (["README.md", "CHANGELOG.md"], "code=false"),
    (["app/main.py"], "code=true"), (["docs/run.sh"], "code=false"), (["scripts/x.sh"], "code=true"), (["bin/ccboard-hook"], "code=true"),
    ([".github/workflows/ci.yml"], "code=true"), (["install.sh"], "code=true"), (["README.md", "tests/test_x.py"], "code=true"),
])
def test_the_changes_job_runs_macos_for_everything_but_documents(tmp_path, event, changed, code):
    assert _scope_after(tmp_path, changed, event) == code


def test_the_changes_job_runs_macos_when_it_cannot_tell(tmp_path):
    """A first push (no `before` commit) or an unknown base: run the macOS job rather than skip it."""
    import os
    import subprocess
    out = tmp_path / "out"
    script = _run_blocks(_job(CI.read_text(), "changes"))[0]
    subprocess.run(["bash", "-e", "-c", script], cwd=tmp_path, check=True, capture_output=True,
                   env={**os.environ, "EVENT": "push", "PR_BASE": "", "PUSH_BEFORE": "0" * 40, "GITHUB_OUTPUT": str(out)})
    assert out.read_text().strip() == "code=true"


def test_the_linux_test_job_installs_shellcheck_too():
    assert "apt-get install -y tmux shellcheck" in _job(CI.read_text(), "test")
