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


def test_state_exposes_block_and_clear(lite_client, fake_tmux):
    from app import main
    H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
    main.db.kv_set(usage.KV_BLOCK, usage.parse_blocks(json.dumps(SAMPLE)))
    main.db.kv_set("rate_limited", {"session": "a--b--c", "message": "limit hit"})
    st = lite_client.get("/api/state", headers=H).json()
    assert st["block"]["value"]["burn_cost_per_hour"] == 30.98 and st["rate_limited"]["value"]["session"] == "a--b--c"
    assert lite_client.post("/api/usage/rate-limit/clear", headers=H).status_code == 200
    assert lite_client.get("/api/state", headers=H).json()["rate_limited"] is None


def test_clearing_the_banner_also_re_arms_the_codex_notice_gate(lite_client, fake_tmux):
    """Issue #32 (b): the Claude gate (rl_notified:) and the Codex gate (codex_rl_notified_<resets_at>) are both once-per-window; a cleared
    banner re-arms both, and keys of other families stay."""
    from app import main
    from app.agents import codex_rollout
    H = {"Tailscale-User-Login": "alice@example.com", "X-CCBoard": "1"}
    main.db.kv_set(codex_rollout.NOTIFIED + "1790000000", True)
    main.db.kv_set(codex_rollout.NOTIFIED + "x491000", True)
    main.db.kv_set("rl_notified:claude:1790000000", True)
    main.db.kv_set("rate_limits_codex", {"reached": True})                      # a measured window is not a gate: clearing changes no reading
    assert lite_client.post("/api/usage/rate-limit/clear", headers=H).status_code == 200
    assert main.db.kv_get(codex_rollout.NOTIFIED + "1790000000") is None and main.db.kv_get(codex_rollout.NOTIFIED + "x491000") is None
    assert main.db.kv_get("rl_notified:claude:1790000000") is None
    assert main.db.kv_get("rate_limits_codex")["value"] == {"reached": True}
