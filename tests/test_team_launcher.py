"""Team P3, issue #199: the launcher reads the cached team view only on a board that shares a repository.

`GET /api/state` carries `team: {shared: true}` while some repository is switched on and has no `team` key otherwise (off means off), so the launcher knows whether to ask the board
for the team view at all. Temp dirs and the fake GitHub only.
"""
from __future__ import annotations

from tests.test_team_github import H, ID, KEY, seed, tw  # noqa: F401 (tw is a fixture)


def test_the_state_has_no_team_key_until_a_repository_is_shared_and_loses_it_when_the_last_is_switched_off(tw):
    seed(tw.gh)
    assert "team" not in tw.c.get("/api/state", headers=ID).json()
    assert tw.c.put("/api/team/repos/acme/shop", headers=H, json={"shared": True}).status_code == 200
    tw.poller.stop(2.0)
    assert tw.c.get("/api/state", headers=ID).json()["team"] == {"shared": True}
    assert tw.c.put("/api/team/repos/acme/shop", headers=H, json={"shared": False}).status_code == 200
    assert "team" not in tw.c.get("/api/state", headers=ID).json(), "switching the last repository off takes the key away"
    assert tw.db.team_shared() == {} and KEY not in tw.db.team_shared()
