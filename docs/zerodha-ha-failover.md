# Zerodha Dual-Account HA Failover — Implementation Notes

Branch: `zerodha-ha-failover` (kept OFF `main` — feed-core, deploy OFF-MARKET only).

**What it guarantees:** *uptime / data never stops*, NOT speed. During a failover
one account carries all tokens (~1350 today, well under the 3000/WS cap) — prices
may be slightly laggier but keep flowing. Recovers automatically when the down
account returns. (CPU/latency is the separate Phase-2 multi-process track.)

## Behaviour
- Normal: **Account A = NSE / BSE (+ NFO / BFO / CDS)**, **Account B = MCX**.
- If B goes unhealthy → MCX streams from A. If A goes unhealthy → NSE/BSE + MCX
  stream from B. Bidirectional, automatic, debounced (anti-flap).
- Health is **tick-based**: a connected-but-silent socket (Kite half-open / token
  throttle) counts UNHEALTHY when the feed is demonstrably live on the other
  account, so failover still fires.
- **Kill-switch:** `failover_enabled = false` → Account A carries everything
  (instant revert to pre-feature behaviour).

## Files changed
| File | Change |
|---|---|
| `backend/app/models/zerodha_feed_routing.py` | **NEW** singleton config: `exchange_account_map`, `failover_enabled`, debounce knobs. |
| `backend/app/core/database.py` | Register `ZerodhaFeedRouting` in Beanie. |
| `backend/app/services/zerodha_service.py` | Per-account tick heartbeat; `account_healthy`; `resolve_target_account`; `disconnect_account_ws` (account-scoped teardown); exchange-aware `_ws_subscribe`; `feed_failover_loop`; independent A/B self-heal; `get_failover_status`. |
| `backend/app/main.py` | Register `feed_failover_loop` as a supervised `leader:feed` subtask (3 s). |
| `backend/app/api/v1/admin/zerodha.py` | `GET/PUT /zerodha/routing` + `POST /zerodha/routing/test-disconnect` (super-admin). |
| `frontend-admin/lib/api.ts` | `ZerodhaAPI.routing / updateRouting / failoverTestDisconnect`. |
| `frontend-admin/components/zerodha/FeedRoutingCard.tsx` | **NEW** Routing & Failover card (live health pills, per-exchange dropdowns, active-failover banner, kill-switch, off-market test buttons). |
| `frontend-admin/app/(admin)/zerodha/page.tsx` | Mount card; relabel tabs → "Account A — NSE/BSE feed" / "Account B — MCX feed". |

## Rollout
1. Operator creates a 2nd Kite Connect app (Account B) → api_key/secret; redirect `…/admin/zerodha/callback`.
2. Deploy this branch **off-market**: `git checkout zerodha-ha-failover && git pull`, then
   `sudo systemctl restart marginwealth-feed marginwealth-backend` and admin `npm run build && pm2 restart marginwealth-admin`.
3. Configure routing in the new admin card (A=NSE/BSE, B=MCX); connect BOTH accounts (manual login).
4. Run the test plan below.
5. Only after all tests pass → merge to `main`.

## Off-market test plan (mandatory — weekend / after 15:30 IST, both accounts connected)
1. **Normal routing:** NSE/BSE tokens land on A's WS, MCX on B's (`GET /zerodha/routing` → `live.effective_route`, ws-pool). Prices flow on both.
2. **B fails → A takeover:** click **Drop B** (or revoke B token). Within `failover_confirm_down_sec`, MCX re-subscribes onto A and MCX prices keep updating (banner shows "MCX → Account A"). Verify open MCX positions keep live P&L + stop-out.
3. **A fails → B takeover:** click **Drop A**. NSE/BSE + MCX all flow via B. Verify.
4. **Recovery / failback:** bring the dropped account back (self-heal reconnects). After `failback_confirm_up_sec` its exchanges route back. Toggle health rapidly → routing must NOT thrash.
5. **Both down:** graceful — no crash, self-heal retries, pills show UNHEALTHY.
6. **Restart safety:** restart `marginwealth-feed` → both accounts reconnect, routing restores.
7. **Regression:** stop-out timing, option-chain freshness, order fills — unchanged in normal mode.

## Notes / limits
- 3000-tokens-per-WS Kite cap: fine today (~1350). If total ever exceeds 3000, a full
  failover keeps what fits and logs `zerodha_feed_failover_capacity_skip` (never silent).
  Priority-ordering (positions/watchlist first) is a future refinement.
- Both accounts down = Zerodha-side outage (unavoidable); self-heal keeps retrying both.
- Daily token rotation ×2 — both accounts need a fresh token daily (~08:00 IST). The
  admin card's health pills make a forgotten Account B obvious.
