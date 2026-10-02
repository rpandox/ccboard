"""The GitHub Actions workflow builds the image only after the tests pass, with package write rights on that job only."""
import pathlib
import re

CI = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def _job(text, name):
    m = re.search(rf"^  {name}:\n(.*?)(?=^  \w+:\n|\Z)", text, re.S | re.M)
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
