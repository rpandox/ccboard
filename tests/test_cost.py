import json
from datetime import datetime, timezone

from app import cost

RAW = {"session": [
    {"period": "11111111-1111-4111-8111-111111111111", "totalCost": 1.5, "totalTokens": 100, "metadata": {"lastActivity": "2026-09-27T01:00:00.000Z"}},
    {"period": "22222222-2222-4222-8222-222222222222", "totalCost": 2.0, "totalTokens": 50, "metadata": {"lastActivity": "2026-09-20T01:00:00.000Z"}},
    {"period": "2026/09/03/rollout-codex", "totalCost": 9.0, "totalTokens": 1},
    {"period": "33333333-3333-4333-8333-333333333333", "totalCost": 0.25, "totalTokens": 5, "metadata": {"lastActivity": "2026-09-25T01:00:00.000Z"}},
], "totals": {"totalCost": 12.75}}


def test_parse_and_attribute():
    costs = cost.parse_sessions(json.dumps(RAW))
    assert set(costs) == {"11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333"}
    now = datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)
    sessions = [{"project": "shop", "repo": "api", "claude_session_id": "11111111-1111-4111-8111-111111111111"},
                {"project": "shop", "repo": "web", "claude_session_id": "22222222-2222-4222-8222-222222222222"},
                {"project": "shop", "repo": "web", "claude_session_id": "22222222-2222-4222-8222-222222222222"},  # relaunched row, same id
                {"project": "blog", "repo": "site", "claude_session_id": "33333333-3333-4333-8333-333333333333"},
                {"project": "blog", "repo": "site", "claude_session_id": "44444444-4444-4444-8444-444444444444"}]  # unknown to ccusage
    tasks = [{"id": 5, "claude_session_id": "33333333-3333-4333-8333-333333333333"}, {"id": 6, "claude_session_id": None}]
    r = cost.attribute(costs, sessions, tasks, now)
    assert r["projects"]["shop"] == {"total": 3.5, "today": 1.5, "week": 1.5, "repos": {"api": 1.5, "web": 2.0}}
    assert r["projects"]["blog"]["total"] == 0.25 and r["projects"]["blog"]["week"] == 0.25 and r["projects"]["blog"]["today"] == 0
    assert r["tasks"] == {5: 0.25} and r["attributed"] == 3 and r["total_all"] == 3.75
    assert cost.parse_sessions("garbage") == {}


def test_refresh_route(client, fake_tmux, monkeypatch):
    from app import main
    monkeypatch.setattr(cost, "fetch_sessions", lambda: cost.parse_sessions(json.dumps(RAW)))
    H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
    tid = main.db.task_add(project="shop", repo="api", slug="s", title="T", prompt="p", branch="b", base="main", worktree="/nope",
                           tmux_name="shop--api--t-s", claude_session_id="11111111-1111-4111-8111-111111111111")
    r = client.post("/api/cost/refresh", headers=H).json()
    assert r["tasks"][str(tid)] == 1.5 or r["tasks"][tid] == 1.5
    st = client.get("/api/state", headers=H).json()
    assert st["cost"]["value"]["sessions_known"] == 3
    assert next(t for t in st["tasks"] if t["id"] == tid)["cost_usd"] == 1.5
    monkeypatch.setattr(cost, "fetch_sessions", lambda: None)
    assert client.post("/api/cost/refresh", headers=H).status_code == 400
