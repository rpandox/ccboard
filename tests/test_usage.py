import json

from app import usage

SAMPLE = {"blocks": [{"id": "x", "isActive": False, "costUSD": 1}, {"id": "2026-09-26T11:00:00.000Z", "startTime": "2026-09-26T11:00:00.000Z",
           "endTime": "2026-09-26T16:00:00.000Z", "isActive": True, "isGap": False, "totalTokens": 87219690, "costUSD": 95.765,
           "burnRate": {"costPerHour": 30.98, "tokensPerMinute": 470257.9}, "projection": {"remainingMinutes": 103, "totalCost": 148.95, "totalTokens": 135656254},
           "models": ["claude-opus-5-5"]}]}


def test_parse_blocks():
    b = usage.parse_blocks(json.dumps(SAMPLE))
    assert b["active"] and b["cost_usd"] == 95.765 and b["burn_cost_per_hour"] == 30.98 and b["remaining_minutes"] == 103
    assert usage.parse_blocks('{"blocks": []}') == {"available": True, "active": False}
    assert usage.parse_blocks("garbage")["error"]


def test_state_exposes_block_and_clear(client, fake_tmux):
    from app import main
    H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
    main.db.kv_set(usage.KV_BLOCK, usage.parse_blocks(json.dumps(SAMPLE)))
    main.db.kv_set("rate_limited", {"session": "a--b--c", "message": "limit hit"})
    st = client.get("/api/state", headers=H).json()
    assert st["block"]["value"]["burn_cost_per_hour"] == 30.98 and st["rate_limited"]["value"]["session"] == "a--b--c"
    assert client.post("/api/usage/rate-limit/clear", headers=H).status_code == 200
    assert client.get("/api/state", headers=H).json()["rate_limited"] is None
