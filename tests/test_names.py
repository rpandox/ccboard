import pytest

from app import projects, tmux


@pytest.mark.parametrize("name", ["a", "shop", "my_repo", "a-b", "A1", "x" * 64])
def test_valid_names(name):
    assert tmux.valid_name(name)


@pytest.mark.parametrize("name", ["", "-a", "a-", "_a", "a_", "a--b", "a.b", "a:b", "a b", "a#b", "x" * 65, "../x"])
def test_invalid_names(name):
    assert not tmux.valid_name(name)


def test_split_name_roundtrip():
    n = tmux.tmux_name("shop", "api", "s1")
    assert n == "shop--api--s1"
    assert tmux.split_name(n) == ("shop", "api", "s1")


@pytest.mark.parametrize("bad", ["shop--s1", "shop--api--s1--x", "p---s", "_ccboard-login", "a--b--"])
def test_split_name_rejects(bad):
    with pytest.raises(ValueError):
        tmux.split_name(bad)


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/org/my.repo.git", "my-repo"),
    ("https://github.com/org/Repo/", "Repo"),
    ("git@github.com:org/tig.git", "tig"),
    ("git@github.com:repo.git", "repo"),
    ("ssh://git@host/x/y/z.git", "z"),
    ("https://h/a/--weird--", "weird"),
    ("https://h/a/...", None),
])
def test_derive_repo_name(url, expected):
    assert projects.derive_repo_name(url) == expected


@pytest.mark.parametrize("url", ["https://github.com/o/r", "git@github.com:o/r.git", "ssh://git@h/o/r", "git://h/o/r"])
def test_check_url_ok(url):
    assert projects.check_url(url) == url


@pytest.mark.parametrize("url", ["--upload-pack=/tmp/x", "-c", "file:///etc", "ext::sh -c id", "https://h/a b", "ftp://h/x", ""])
def test_check_url_rejects(url):
    with pytest.raises(projects.BadRequest):
        projects.check_url(url)
