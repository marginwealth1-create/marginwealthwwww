# MarginWealth — Scaling Notes (1000–2000 concurrent users)

> Purpose: honest mental model of where the platform scales, where it
> bottlenecks, and the correct order of fixes. This is a planning doc,
> NOT a change to deploy. Last reviewed: 2026-06-25.

## TL;DR
- A **load balancer is NOT the first answer.** The HTTP/WS serving layer is
  already horizontally scalable (workers read live prices from Redis).
- The real ceiling is the **single-worker `risk_enforcer`** (SL/TP/stop-out)
  and the single-worker live feed. These do NOT scale by adding boxes.
- Cost scales with **total concurrent OPEN positions**, not user count.
- The correct fix when we grow is **risk-loop sharding**, then a dedicated
  feed worker, then MongoDB tuning — and only THEN multi-box + load balancer.

## Two layers
### Layer 1 — Serving (stateless, scalable)
Login, watchlist, quotes, page-load, order placement. Runs across all
gunicorn UvicornWorker workers behind nginx. Non-leader workers serve quotes
from Redis `mdlive:{token}` (written by the feed leader). Adding workers /
servers / a load balancer scales this layer linearly. **Not the bottleneck.**

### Layer 2 — Engine (stateful, single leader worker)
Co-located under ONE Redis lock `leader:feed` (`app/main.py`
`_feed_leader_main`):
- Zerodha WS feed (in-process `ticks_by_token`)
- Infoway feed
- 250 ms market tick fanout (`tick_loop`)
- `feed:subscribe` listener
- **`risk_enforcer`** (SL/TP/margin-call/stop-out)
- `pending_order_poller`, `zerodha_auto_login`

## Why a single worker (the core constraint)
**Live prices live in process RAM, not everywhere.** The Zerodha WS can only
be held by ONE worker (two would duplicate/confuse the broker session). Its
ticks land in that worker's memory. The risk loop is co-located there so
price lookups are **zero-network, instant** (`get_ltp_instant` reads in-process
`_state` / `ticks_by_token`).

Moving the risk loop to another worker means that worker has cold in-process
state and must read prices from Redis `mdlive` instead — feasible (mdlive is
execution-safe, 30 s TTL) and is exactly the path that makes sharding possible.

## What breaks first
The `risk_enforcer` loop walks **every open position each cycle**. Cost is
driven by total open positions, e.g. 2000 users × ~4 open = ~8000 positions.
As that grows, `risk_enforcer_perf.total_ms` rises; when it consistently
crosses **1–2 s**, SL/TP/stop-out fire late → financial risk.

**Trigger metric:** `risk_enforcer_tick_overrun` consistently > 1–2 s.

Reference (2026-06-25, just after deploy): 86 positions / 25 users /
total_ms 405 (cold cache; warms to ~100–200 ms).

## What scales vs what doesn't (@ ~2000 users)
| Component | Verdict | Note |
|---|---|---|
| HTTP API / login / quotes | Scales | Redis `mdlive`, add workers/boxes |
| WS price fanout | Scales | Distributed per-worker to its own clients |
| `risk_enforcer` (SL/TP) | **Bottleneck** | Single worker, single-threaded |
| Order placement | OK | Per-request, single-worker, Redis-dedup'd |
| MongoDB | Needs tuning | Indexes, pool size, later read-replica |
| Redis | OK | Very fast, fine at this scale |

## Roadmap (correct order)
1. **Risk-loop sharding** — partition users into N shards
   (`leader:risk:shard:0..N-1`), each shard on a different worker, reading
   prices from Redis `mdlive` instead of in-process `_state`. Makes the
   engine scale horizontally. (Highest-value future change.)
2. **Dedicated feed worker** — isolate Zerodha/Infoway + tick_loop so the feed
   never competes with HTTP traffic.
3. **MongoDB tuning** — compound index on open-positions query, larger
   connection pool, read-replica for reporting/analytics.
4. **Multi-box + load balancer** — only once a single 8-core box runs out of
   cores/connections. nginx/LB in front of multiple app boxes (Redis + Mongo
   stay shared/central).

## Now vs later
- **Now:** nothing to change. Current box (~356 connections @ 3.1/8 cores) has
  large headroom.
- **At ~500+ concurrent users:** design + test risk-loop sharding.
- **Decision signal:** sustained `risk_enforcer_tick_overrun` > 1–2 s.

## Operational rules (already learned the hard way)
- **Never restart/deploy during market hours** — use `systemctl reload`
  (zero-downtime SIGHUP rolling) for code-only changes; full restart only when
  `requirements.txt` changes, and ideally off-market.
- Watch `risk_enforcer_perf` and `zerodha_overlay_timeout` counts during peak.

---

# Hardware sizing, capacity & provider FAQ

## "Bahut trades per day" is NOT the load
Risk-loop cost scales with **concurrent OPEN positions** and **concurrent
connected users (WS)**, NOT total daily trade count. A user placing 500 trades
that close quickly = few concurrent open positions = low load. Daily trade
volume only stresses **MongoDB writes + the order endpoint** (the scalable
serving layer). So size for *peak concurrent open positions + active users*.

## Capacity estimates (rough — confirm with a load test)
Assumes ~4 open positions per active user.
| Setup | ~Concurrent active users | First bottleneck |
|---|---|---|
| KVM8, sharding OFF (today) | ~500–750 | single risk worker |
| KVM8, sharding ON (4 shards) | ~1500–2000 | risk loop + MongoDB |
| KVM16, sharding ON (8 shards) | ~3000–4000 | MongoDB |
| Beyond | load balancer + multi-box | — |

Reference live numbers (2026-06-25, Hostinger KVM8, India-Mumbai): CPU ~31%,
RAM ~13%, ~356 connections @ ~3.1/8 cores. **Large headroom today.**

## Scaling order (cheapest/safest first)
1. **Risk-loop sharding** (software, free) → ~1500–2000 users.
2. **Vertical: KVM8 → KVM16** when CPU is *sustained* > 60–70% or overruns
   persist after sharding. More cores → more workers → more shards → ~2× risk
   capacity. Much simpler than horizontal scaling.
3. **MongoDB tuning** — indexes on the open-positions query, larger connection
   pool, read-replica for reporting — needed at high trade volume.
4. **Load balancer + multi-box** — LAST. Only when one box maxes out.

## Provider FAQ — Hostinger vs DigitalOcean/AWS
- **Today's incidents were NOT the provider.** They were (a) the Zerodha REST
  timeout bug (fixed) and (b) restarts during market hours (operational). A
  KVM8 is a KVM8 — DigitalOcean is not magically faster.
- **Changing provider does NOT fix SL/TP/stop-out correctness.** That depends
  on the risk-loop architecture (sharding) + the existing atomic dedup — not on
  hosting. A naively-configured load balancer can actually BREAK things,
  because live prices live in one worker's RAM.
- **Mumbai location is an advantage.** Zerodha/NSE/MCX are in Mumbai; lower
  latency = better fills. DigitalOcean's nearest region is Bangalore (farther).
  Prefer staying in Mumbai for trading latency.
- **When DO/AWS makes sense:** much larger scale where *managed services*
  (managed MongoDB, managed LB, autoscaling, easy snapshots) ease operations —
  a future optional convenience, NOT a fix. Don't migrate to "solve" current
  issues.
- **Verdict:** 4k positions + 5k users is achievable **on Hostinger** via
  sharding → KVM16 → MongoDB tuning. Spend effort there, not on migration.

---

# Risk-Loop Sharding — Detailed Implementation Plan

> STATUS: PLAN ONLY. No code written. Implement carefully when the market
> is CLOSED. Goal: scale SL/TP/stop-out to 1000–2000+ concurrent users
> WITHOUT ever breaking live SL/TP. Every step is grounded in the current
> code (read 2026-06-25).
>
> GOLDEN RULE: ship behind a flag whose DEFAULT reproduces today's behaviour
> EXACTLY. Turning sharding on is then a one-line env change you can revert
> instantly.

## A. How it works TODAY (the exact code we change)

### A.1 The loop
`backend/app/services/risk_enforcer.py`
- `risk_enforcer_loop(interval_sec=0.5)` (~line 1106): `while _running:` →
  `await enforce_once()` → drift-corrected sleep. Logs
  `risk_enforcer_tick_overrun` when a tick > interval.
- `enforce_once()` (~line 927):
  1. `all_open = await Position.find(status == OPEN).to_list()` — ALL open
     positions in one scan.
  2. Demo slow-lane: on 4 of 5 ticks, drop demo positions.
  3. Group by `user_id` → `positions_by_user`; collect unique tokens.
  4. **Prices (CRITICAL):**
     `shared_ltp = {tok: market_data_service.get_ltp_instant(tok) ...}` /
     `shared_quotes = {tok: get_quote_instant(tok) ...}` — read **in-process**
     `_state` / `ticks_by_token`, only warm on the feed-leader worker. ← this
     is why risk currently MUST sit on `leader:feed`.
  5. Zero-LTP numeric tokens → `zerodha.subscribe_tokens_on_demand(...)` (also
     only works on the feed-leader where the WS lives).
  6. Resolve users (short-TTL `_user_cache`) + batch-fetch wallets (`$in`).
  7. Sweep users in parallel: `_enforce_one_user_safe` → `_enforce_for_user`.

### A.2 The gating
`backend/app/main.py` (~line 374–401) inside `_feed_leader_main()` under the
`leader:feed` lock, co-located with Zerodha WS, Infoway, `tick_loop` (writes
`mdlive`), `feed_subscribe_listener`, `zerodha_ws_self_heal`,
`pending_order_poller`, `zerodha_auto_login`.

### A.3 The safety net that already exists (do NOT remove)
`_enforce_for_user` (~line 531) squareoff uses an **atomic Mongo claim** to
dedup a close across workers / handoffs → a position can close only ONCE even
if two shards transiently touch it. This is our seatbelt for the whole change.

### A.4 The price mirror that makes sharding possible
`backend/app/services/market_data_service.py`: `tick_loop` (feed leader) writes
every live quote to Redis `mdlive:{token}` via `_write_mdlive_batch` (30 s TTL,
execution-safe); `_read_mdlive(token)` reads one back. → A risk shard on a
NON-feed-leader worker reads prices from `mdlive` instead of in-process
`_state`. We add a BATCH reader (Step C.3).

## B. Target architecture
```
leader:feed         (ONE worker)  → Zerodha WS, Infoway, tick_loop (writes mdlive),
                                     feed_subscribe_listener, ws_self_heal,
                                     pending_order_poller, zerodha_auto_login  [UNCHANGED]
leader:risk:shard:0 (a worker)    → risk for users where shard_of(uid)==0
leader:risk:shard:1 (a worker)    → ... ==1                  (prices from mdlive)
...
leader:risk:shard:N-1
```
- `RISK_SHARDS=1` (DEFAULT): NO new locks, risk stays on `leader:feed`, reads
  in-process prices — **byte-for-byte today's behaviour**.
- `RISK_SHARDS>1`: risk moves OFF `leader:feed` into N shard locks, each reads
  prices from `mdlive`. With 4 workers + 4 shards they fan out naturally.

**Partition by USER, not position:** stop-out/margin is computed at user level
(sum of all that user's positions), so every position of a user must be in the
same shard. `positions_by_user` already groups this way → the change is a filter.

## C. Code changes (small, ordered, independently verifiable)

### C.1 Config flag (no behaviour change)
`backend/app/core/config.py`: add `RISK_SHARDS: int = 1`. Clamp `>=1`; enforce
`RISK_SHARDS <= WEB_CONCURRENCY`. Document in `.env.example`.
ACCEPTANCE: app boots, `settings.RISK_SHARDS == 1`, nothing else changes.

### C.2 Stable shard function (pure, unit-testable)
`risk_enforcer.py` module level:
```
import hashlib
def shard_of(user_id: str, num_shards: int) -> int:
    if num_shards <= 1:
        return 0
    return int(hashlib.sha1(str(user_id).encode()).hexdigest(), 16) % num_shards
```
TEST: deterministic; num_shards==1 → 0; every uid maps to exactly one shard;
roughly even distribution.

### C.3 Batch mdlive price readers (for sharded mode)
`market_data_service.py`: add
`async def get_ltp_batch_mdlive(tokens) -> dict[str, Decimal|None]` and
`async def get_quote_batch_mdlive(tokens) -> dict[str, dict|None]` — ONE Redis
pipeline/MGET over `mdlive:{token}`, reuse `_read_mdlive` parsing (ltp<=0 →
None). NEW functions; do NOT touch `get_ltp_instant`/`get_quote_instant`.

### C.4 Make `enforce_once` shard-aware (default = today)
`risk_enforcer.py`: signature → `enforce_once(shard_id=0, num_shards=1)`.
- After grouping, if `num_shards > 1`: filter `positions_by_user` to
  `shard_of(uid, num_shards) == shard_id`; recompute tokens/user lists; return
  0 if empty.
- Price source switch:
  ```
  if num_shards > 1:
      shared_ltp = await market_data_service.get_ltp_batch_mdlive(all_tokens)
      shared_quotes = await market_data_service.get_quote_batch_mdlive(all_tokens)
  else:
      shared_ltp = {tok: market_data_service.get_ltp_instant(tok) for tok in all_tokens}      # today's exact code
      shared_quotes = {tok: market_data_service.get_quote_instant(tok) for tok in all_tokens} # today's exact code
  ```
- Zero-LTP subscribe trigger: in sharded mode the WS is not on this worker →
  use the cross-worker `await market_data_service.subscribe(zero_numeric)`
  (publishes to `feed:subscribe`). Keep the direct call only in `num_shards==1`.
- CRITICAL: when `num_shards==1` EVERY branch is the ORIGINAL code → zero
  behavioural change. Verify by reading the diff.

### C.5 Pass shard params through the loop
`risk_enforcer_loop(interval_sec=0.5, shard_id=0, num_shards=1)` → calls
`enforce_once(shard_id, num_shards)`. Add `shard_id`/`num_shards` to
`risk_enforcer_started` / `_tick_overrun` / `_perf` log `extra`. Keep
`RISK_SHARDS <= WEB_CONCURRENCY` so each worker holds at most one shard and the
module-global `_running` stays correct.

### C.6 Wire the gating in main.py
- If `RISK_SHARDS == 1`: leave the existing registration under `leader:feed`
  EXACTLY as-is (default, zero change).
- If `RISK_SHARDS > 1`: REMOVE `risk_enforcer_loop` from the `leader:feed`
  subtasks; add a NEW top-level block (next to other independent `leader:*`
  loops like `intraday_to_carry`) that, for each `k in range(n)`, starts a
  supervised task guarded by its own `leader:risk:shard:{k}` lock, running
  `risk_enforcer_loop(interval_sec=0.5, shard_id=k, num_shards=n)`. Use the
  EXISTING leader-gate wrapper (`app/core/leader_lock.py`), do NOT invent a new
  lock API. Keep `pending_order_poller` + `zerodha_auto_login` on `leader:feed`.

### C.7 Tests (write BEFORE flipping the flag in prod)
`backend/tests/test_risk_sharding.py`: shard_of properties; `enforce_once`
partitioning (union == all users, intersection == empty); mdlive batch parsing;
dedup (same position to two shards → atomic claim closes once); regression
(`enforce_once()` no-args identical to before).

## D. Rollout (production, market CLOSED)
1. Deploy with `RISK_SHARDS=1` → identical to today. Verify one
   `risk_enforcer_started shard=0 num_shards=1`. Run a full session.
2. Off-market: `RISK_SHARDS=2`, `systemctl reload`. Verify two shards on two
   workers, each ~half users, no overruns, lower per-shard `total_ms`, and a
   real (small) SL fires.
3. Stable → `RISK_SHARDS=4`. Re-verify.

## E. Rollback (instant, no data migration)
Set `RISK_SHARDS=1` → `systemctl reload`. Back to single-worker on
`leader:feed`. No schema change. Atomic claim prevents any double-close during
the flip.

## F. Risks & mitigations
| Risk | Why | Mitigation |
|---|---|---|
| Shard on cold worker → no prices → SL/TP skipped | non-leader has cold `_state` | sharded mode reads `mdlive`; zero-LTP guard already SKIPS safely (never wrong-closes); add null-mdlive metric |
| Double close during rebalance | lock moves between workers | EXISTING atomic Mongo claim closes once |
| mdlive stale (feed leader down) | tick_loop stopped | 30 s TTL → null → guard skips; feed self-heal + supervise restart |
| `_running` collision (2 shards/worker) | module-global flag | enforce `RISK_SHARDS <= WEB_CONCURRENCY` |
| N× `Position.find(OPEN)` scans | each shard scans then filters | OK v1 (scans run in PARALLEL on different workers); FUTURE: projection / coordinator |
| User moves shard if count changes | hash modulo changes | only change shard count OFF-MARKET; atomic claim covers the transition tick |

## G. OUT OF SCOPE (do NOT touch)
Zerodha/Infoway WS + `tick_loop` (stay on `leader:feed`); `pending_order_poller`
+ `zerodha_auto_login` (stay on `leader:feed`); order placement/matching;
the SL/TP/stop-out DECISION logic inside `_enforce_for_user`. No schema change,
no migration. We only change WHICH users a loop handles and WHERE it reads prices.

## H. Pre-flight checklist (before >1 in prod)
- [ ] `RISK_SHARDS=1` deployed, verified identical for a session.
- [ ] Unit tests (C.7) green.
- [ ] Local 2-shard run verified (partitioning + dedup + a real SL fire).
- [ ] mdlive batch reader returns live prices on a non-leader worker.
- [ ] `RISK_SHARDS <= WEB_CONCURRENCY` enforced.
- [ ] Rollback rehearsed (flip to 1 mid-session on staging).
- [ ] Market CLOSED for the prod flip; one operator watching logs live.
