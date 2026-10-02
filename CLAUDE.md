# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository shape

Three-app monorepo. They are deployed independently behind separate hostnames; the two frontends share **one** API but have different CORS origins, bundles, and security postures.

- [backend/](backend/) — FastAPI + MongoDB (Motor + Beanie) + Redis + Celery. Single API serving both user and admin under `/api/v1/user/*` and `/api/v1/admin/*`. WS at `/ws/user/{user_id}`, `/ws/marketdata`, `/ws/admin`.
- [frontend-user/](frontend-user/) — Next.js 14 App Router, port 3000. Customer-facing.
- [frontend-admin/](frontend-admin/) — Next.js 14 App Router, port 3001. Super-admin panel, indexed `noindex,nofollow`, axios client always sends `X-Admin-Api-Key`.

## Commands

### Mac quickstart (daily run)

The README's run instructions are Windows/PowerShell. Mac equivalents — three terminals:

```bash
# Terminal 1: Redis (Homebrew)
redis-server --port 6379                              # or: brew services start redis

# Terminal 2: MongoDB (if not already running)
brew services start mongodb-community                 # only if installed via brew

# Terminal 3: Backend
cd /Users/tarundewangan/Downloads/Projects/marginwealth/backend
source .venv/bin/activate
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# Sanity check:
redis-cli ping                                        # → PONG
curl http://127.0.0.1:8000/health                     # → {"data":{"status":"ok",...}}
```

### Backend ([backend/](backend/))

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # then edit JWT_SECRET, ADMIN_API_KEY
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Workers (separate terminals):
celery -A app.workers.celery_app worker -l info
celery -A app.workers.celery_app beat -l info

# Lint / format / type-check (config in pyproject.toml):
ruff check app/
ruff format app/
mypy app/

# Tests — `backend/tests/` currently holds one end-to-end test (segment settings).
# CI runs pytest only if backend/tests/**/*.py is present.
pytest -q
pytest -q tests/test_segment_settings_end_to_end.py::test_name
```

### Operational scripts ([backend/scripts/](backend/scripts/))

One-shot maintenance scripts, run with `python -m scripts.<name>` from `backend/` after `source .venv/bin/activate`:

- `reseed_super_admin.py` — re-create / reset the super admin account.
- `create_test_user.py` — bootstrap a user for local UX checks.
- `reset_user_margins.py` — clear margin state on a user (manual recovery).
- `backfill_position_opened_side.py`, `fix_bogus_proceeds_credits.py` — historical data-fix migrations; check the docstring before running.

MongoDB **replica set is required for transactions** in Phase 4+; a single node works for Phase 1. Default super admin: `admin@marginwealth.live` / `Admin@123` — admin login requires 2FA, so enroll via the user app first.

### Frontends ([frontend-user/](frontend-user/), [frontend-admin/](frontend-admin/))

Both use identical scripts (only the port differs — 3000 vs 3001):

```bash
cp .env.example .env.local      # admin: set NEXT_PUBLIC_ADMIN_KEY = backend ADMIN_API_KEY
npm install
npm run dev          # next dev
npm run build        # next build
npm run lint         # next lint (non-blocking in CI)
npm run type-check   # tsc --noEmit
```

### Deploy

[scripts/deploy.sh](scripts/deploy.sh) is the on-server deploy script — it diffs `HEAD` against `origin/main` and only restarts the affected piece (backend systemd unit, PM2 frontend, nginx). Force everything with `FORCE_FULL=1 bash scripts/deploy.sh`. See [deploy/README.md](deploy/README.md) for one-time EC2 setup (passwordless sudo for systemctl/nginx). The matching `.github/workflows/deploy.yml` (Actions → SSH → EC2 → `deploy.sh`) is referenced in [deploy/README.md](deploy/README.md) but is not currently checked into this tree — verify on the deployment host before assuming CI auto-deploys.

## Architecture: things that aren't obvious from the file tree

### B-Book broker model (load-bearing)

All trades match **internally**. External APIs (Zerodha Kite, Infoway, Angel One) are price-feed only — orders are never routed out. Anywhere you see logic that resembles "send order to exchange," it's wrong. [backend/app/services/matching_engine.py](backend/app/services/matching_engine.py) is the source of truth.

- Market orders: fill immediately at LTP via `execute_market_order`.
- Limit / SL / SL-M orders: parked `OPEN`, picked up by `pending_order_poller` (1.5 s interval, started in [main.py](backend/app/main.py) lifespan).
- Position close: FIFO-match opposite fills (see commit `c510da2`).

### Money is Decimal128 end-to-end

`bson.Decimal128` on the wire and in MongoDB. [backend/app/utils/decimal_utils.py](backend/app/utils/decimal_utils.py) is the **only** place floats are tolerated, and only at the I/O boundary. Do not introduce float math in services.

### Settings hierarchy

`GLOBAL → TEMPLATE → USER OVERRIDE` — highest wins. Resolver lives in `services/segment_settings_service.py` and caches per `(user_id, segment_type)` in Redis with 5-min TTL. When changing settings logic, invalidate that cache key.

### Background loops (started in `main.py` lifespan, not Celery)

These are long-running asyncio tasks held on `app._*_task`. They must start and stop cleanly:

| Task | File | Interval | What it does |
|------|------|----------|--------------|
| `market_tick` | `services/market_data_service.py` | 1 s | Mock/feed tick loop |
| `pending_order_poller` | `services/matching_engine.py` | 1.5 s | Fires LIMIT/SL-M when trigger hits |
| `risk_enforcer_loop` | `services/risk_enforcer.py` | 1 s | Margin-call / stop-out / ledger breach → notify or auto-squareoff |
| `expiry_cleanup_loop` | `services/expiry_cleanup.py` | 1 h | Drops day-after-expiry instruments from watchlists, Zerodha ticker, Instrument collection |
| `pnl_sharing_scheduler` | `services/pnl_sharing_service.py` | 5 min | Auto-settles ACTIVE+AUTO P&L sharing agreements at period close (DAILY/WEEKLY/MONTHLY); MANUAL agreements unaffected. Handles both `PNL_AND_BROKERAGE` (default) and `BROKERAGE_ONLY` (`sharing_pnl=0`) agreement types. |
| Zerodha boot | inline `_zerodha_boot` | once | Cache-warm NSE/NFO/MCX + connect WS pool |
| Infoway start | inline | once | Forex/crypto/metals/energy feed; off unless `INFOWAY_API_KEY` set and `INFOWAY_AUTO_CONNECT=true` |

Without `risk_enforcer_loop`, the Risk Management settings on the admin page do nothing automatically.

### Two-token auth

User JWT and admin JWT are **separate audiences** with separate login endpoints ([backend/app/core/dependencies.py](backend/app/core/dependencies.py)). Admin requests must satisfy **all**: valid JWT with admin role + `X-Admin-Api-Key` header + IP in `ADMIN_IP_WHITELIST` (when set) + 2FA. Tokens carry role inside the JWT but the user is always re-fetched from DB per request, so blocking an account takes effect immediately.

- 15-min access / 7-day refresh; refresh tokens use a Redis allow-list keyed by JTI; logout deletes the JTI; refresh rotates JTI.
- Login lockout was disabled (commit `a923202`) — `auth_service` always allows attempts.

### WebSocket fanout

Multiple FastAPI instances stay in sync via Redis pub/sub channels: `user:{id}`, `market:tick`, `admin:events`. If you add a new realtime event, publish through Redis — don't broadcast direct from one instance.

Known `admin:events` event types (consumed by [frontend-admin/components/common/AdminWsBridge.tsx](frontend-admin/components/common/AdminWsBridge.tsx)):

- `position_update` / `order_update` / `wallet_update` / `deposit_update` / `withdrawal_update` / `kyc_update` — existing events; each invalidates the matching admin React Query keys.
- `pnl_sharing_update` — published from the matching-engine close path when a closed Position has a user whose broker has an active sharing agreement. Bridge invalidates `["pnl-sharing"]` query keys so the SharingCard refetches live.

### Frontend axios + React Query patterns

- Both apps use a shared axios client ([frontend-user/lib/api.ts](frontend-user/lib/api.ts), [frontend-admin/lib/api.ts](frontend-admin/lib/api.ts)) with single-flight refresh on 401 and uniform `ApiError` unwrap. The admin client always attaches `X-Admin-Api-Key` from `NEXT_PUBLIC_ADMIN_KEY`.
- Trade UI has hard-won flicker handling: optimistic merge on order placement, polling paused for ~3 s after an optimistic update (commits `6da10cb`, `0be5a0d`, `891d668`). Don't add a post-success `queryClient.invalidateQueries` for trades — it re-introduces the 1 s flicker.

### Sharding-ready collections

`orders`, `trades`, `wallet_transactions`, `audit_logs`, `notifications` carry compound `(user_id, …)` indexes appropriate for a `user_id` shard key. `audit_logs` has a 1-year TTL via `expires_at`.

### Bootstrap seeding

`RUN_SEED_ON_STARTUP=true` (default) runs idempotent seed on every boot: super admin, 20 segment-settings rows, 4 templates (Bronze/Silver/Gold/VIP), default brokerage plan, company bank, deposit/withdrawal rules, platform settings, NSE holidays. Safe to disable in tests via env.

### Settlement outstanding (negative-balance recovery)

When a stop-out force-close cannot fully debit the realized loss from the user's wallet (loss exceeds `available_balance + credit_limit`), the unrecoverable shortfall accrues to `Wallet.settlement_outstanding`. Wallet's `available_balance` floors at 0 — never goes negative. Recovered automatically against the user's next `DEPOSIT` (deducted before crediting available_balance).

- Force-close path uses [`wallet_service.force_debit`](backend/app/services/wallet_service.py); regular trades still respect `InsufficientFundsError`.
- Force-close is distinguished by `Order.is_squareoff = True` (set by `risk_enforcer._squareoff_position` and propagated through to the matching engine).
- Two new audit transaction types: `SETTLEMENT_OUTSTANDING_BOOKED` (accrual) and `SETTLEMENT_OUTSTANDING_RECOVERY` (recovered from deposit).
- User wallet UI shows the outstanding amount in red when > 0; admin user-detail page mirrors it.

## Conventions

- Use `app.utils.decimal_utils.quantize_money` / `to_decimal` for money — never `float()`.
- Indian-format validators (PAN, IFSC, Aadhaar with Verhoeff, mobile, GST) live in [backend/app/utils/validators.py](backend/app/utils/validators.py). Reuse rather than re-implement.
- Market-hours / IST helpers in [backend/app/utils/time_utils.py](backend/app/utils/time_utils.py). Use `now_utc()` for timestamps; UI converts to IST at the edge.
- Frontend tokens are stored in `localStorage` (see `STORAGE_KEYS` in `lib/constants.ts`); the auth Zustand store persists and hydrates from there.
- Tailwind theme is locked: `#0a0a0a` bg, `#10b981` buy/profit, `#ef4444` sell/loss. Don't drift.
- Platform brand is **MarginWealth** (`marginwealth.live`): royal-blue `brand` scale (`#3B5BDB`, both `tailwind.config.ts`) + gold `#E6B839` accent. The mark (blue "M" + gold gem) and every logo/icon/favicon are generated by [frontend-user/public/icons/_gen_brand_assets.py](frontend-user/public/icons/_gen_brand_assets.py) — edit the polygons there, re-run, and copy the printed 24x24 coords into both `BrandGlyph.tsx`. Never hand-edit the PNG/SVGs.
