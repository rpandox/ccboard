"""app/workflows.py: does a push to ccboard-backup/<node>/<branch> start this workflow? True, False, or None when the scan cannot tell."""
import pytest

from app import workflows
from app.workflows import push_runs_on_backup_branches as runs

T, F, N = True, False, None

CASES = [
    # (id, workflow text, expected)
    ("on-push-scalar", "name: ci\non: push\njobs:\n  a:\n    runs-on: x\n", T),
    ("on-flow-list", "on: [push, pull_request]\njobs: {}\n", T),
    ("on-flow-list-quoted", "on: ['push', \"pull_request\"]\n", T),
    ("on-pr-only", "on: pull_request\n", F),
    ("on-block-list", "on:\n  - push\n  - pull_request\n", T),
    ("on-block-list-flush", "on:\n- push\n- workflow_dispatch\njobs: {}\n", T),
    ("on-list-no-push", "on:\n  - pull_request\n  - workflow_dispatch\n", F),
    ("push-mapping-no-filters", "on:\n  push:\n  pull_request:\n", T),
    ("push-null", "on:\n  push: null\n", T),
    ("push-empty-flow-map", "on:\n  push: {}\n", T),
    ("allow-list-main", "on:\n  push:\n    branches: [main]\n", F),
    ("allow-list-block", "on:\n  push:\n    branches:\n      - main\n      - 'release/*'\n", F),
    ("allow-list-block-flush", "on:\n  push:\n    branches:\n    - main\n", F),
    ("allow-everything", "on:\n  push:\n    branches: ['**']\n", T),
    ("allow-star-does-not-cross-slash", "on:\n  push:\n    branches: ['*']\n", F),
    ("allow-prefix-glob", "on:\n  push:\n    branches:\n      - 'ccboard-backup/**'\n", T),
    ("allow-one-level-glob", "on:\n  push:\n    branches: ['ccboard-backup/*']\n", F),
    ("allow-then-negate", "on:\n  push:\n    branches: ['**', '!ccboard-backup/**']\n", F),
    ("negate-then-allow-again", "on:\n  push:\n    branches: ['!ccboard-backup/**', '**']\n", T),
    ("ignore-backup", "on:\n  push:\n    branches-ignore: ['ccboard-backup/**']\n", F),
    ("ignore-block", "on:\n  push:\n    branches-ignore:\n      - 'ccboard-backup/**'\n", F),
    ("ignore-something-else", "on:\n  push:\n    branches-ignore:\n      - 'dependabot/**'\n", T),
    ("ignore-star-only", "on:\n  push:\n    branches-ignore: ['*']\n", T),
    ("paths-filter-does-not-apply-to-branch-names", "on:\n  push:\n    paths:\n      - 'src/**'\n", T),
    ("paths-ignore", "on:\n  push:\n    paths-ignore: ['docs/**']\n", T),
    ("paths-and-ignore-branches", "on:\n  push:\n    branches-ignore: ['ccboard-backup/**']\n    paths: ['src/**']\n", F),
    ("tags-only", "on:\n  push:\n    tags: ['v*']\n", F),
    ("tags-with-branches", "on:\n  push:\n    tags: ['v*']\n    branches: ['**']\n", T),
    ("quoted-on", "\"on\":\n  push:\n    branches: [main]\n", F),
    ("single-quoted-on", "'on': push\n", T),
    ("quoted-push-key", "on:\n  \"push\":\n    branches: ['**']\n", T),
    ("inline-flow-mapping", "on: {push: {branches: [main]}, pull_request: {}}\n", F),
    ("inline-flow-mapping-open", "on: {push: {}, workflow_dispatch: {}}\n", T),
    ("inline-flow-push-ignore", "on: { push: { branches-ignore: ['ccboard-backup/**'] } }\n", F),
    ("comments-everywhere", "# the CI\non: # when\n  push: # every push\n    branches: # allow-list\n      - main # only main\n  # pull_request:\n", F),
    ("hash-inside-quotes", "on:\n  push:\n    branches: ['feat#1', 'ccboard-backup/**']\n", T),
    ("crlf", "name: ci\r\non:\r\n  push:\r\n    branches: [main]\r\n  pull_request:\r\njobs:\r\n  a: {}\r\n", F),
    ("crlf-open", "on:\r\n  push:\r\njobs: {}\r\n", T),
    ("other-event-blocks-are-not-read", "on:\n  workflow_dispatch:\n    inputs:\n      who:\n        description: x\n        required: false\n  push:\n    branches: [main]\n", F),
    ("workflow-call-only", "on:\n  workflow_call:\n    inputs:\n      a:\n        type: string\n", F),
    ("on-after-other-top-keys", "name: x\nenv:\n  A: 1\non:\n  push:\n    branches:\n      - '**'\njobs: {}\n", T),
    ("run-block-mentions-on", "on:\n  pull_request:\njobs:\n  a:\n    steps:\n      - run: |\n          echo 'on: push'\n", F),
    # cannot tell: never True
    ("empty-file", "", N),
    ("no-on-key", "name: ci\njobs: {}\n", N),
    ("on-empty", "on:\njobs: {}\n", N),
    ("two-on-keys", "on: push\non: pull_request\n", N),
    ("anchor", "on:\n  push: &p\n    branches: [main]\n", N),
    ("alias-value", "on:\n  push: *p\n", N),
    ("template", "on:\n  push:\n    branches: ['${{ vars.BRANCHES }}']\n", N),
    ("tab-indent", "on:\n\tpush:\n\t\tbranches: [main]\n", N),
    ("block-scalar-filter", "on:\n  push:\n    branches: |\n      main\n", N),
    ("both-filters", "on:\n  push:\n    branches: [main]\n    branches-ignore: [dev]\n", N),
    ("empty-allow-list", "on:\n  push:\n    branches: []\n", N),
    ("unclosed-flow", "on: {push: {branches: [main]\n", N),
    ("unbalanced-list", "on: [push\n", N),
    ("nested-flow-list", "on: [[push]]\n", N),
    ("on-list-item-with-colon", "on:\n  - push: {}\n", N),
    ("garbage", "}{ not yaml at all ::: [\n", N),
    ("filter-layout", "on:\n  push:\n      branches: [a]\n    paths: [b]\n", N),
]


@pytest.mark.parametrize("name,text,want", CASES, ids=[c[0] for c in CASES])
def test_push_runs_on_backup_branches(name, text, want):
    assert runs(text) is want


def test_the_pattern_is_a_parameter_and_the_default_is_a_backup_branch():
    text = "on:\n  push:\n    branches: ['release/**']\n"
    assert runs(text) is False
    assert runs(text, pattern="release/1.0") is True
    assert workflows.BACKUP_BRANCH == "ccboard-backup/node/branch"


@pytest.mark.parametrize("value", [None, 5, b"on: push", ["on: push"], {"on": "push"}])
def test_a_non_string_is_unknown_and_nothing_raises(value):
    assert runs(value) is None


def test_the_real_shapes_of_two_repos_that_motivated_the_check():
    both = "name: CI\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-24.04\n    steps:\n      - uses: actions/checkout@v4\n"
    fixed = "name: CI\non:\n  push:\n    branches-ignore: ['ccboard-backup/**']\n  pull_request:\njobs:\n  test:\n    runs-on: ubuntu-24.04\n"
    assert runs(both) is True and runs(fixed) is False
