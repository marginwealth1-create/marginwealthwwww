# MarginWealth Backend

Production-grade FastAPI backend for the **MarginWealth** Indian B-Book stock trading platform.

## Stack
- Python 3.11+ / FastAPI (async)
- MongoDB 7+ (Motor + Beanie ODM, Decimal128 money)
- Redis 7+ (cache, pub/sub, rate-limit, sessions)
- JWT auth (15 min access / 7 day refresh) + 2FA (TOTP)
- Celery + APScheduler (workers, EOD, auto-squareoff)
- WebSockets fanned-out via Redis pub/sub

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate   # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt
cp .env.example .env

# Start MongoDB (replica set required for transactions) and Redis, then:
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Workers (separate terminals):
celery -A app.workers.celery_app worker -l info
celery -A app.workers.celery_app beat -l info
```

Docs: http://localhost:8000/docs · Health: http://localhost:8000/health

## API surface
- `/api/v1/user/...`  — user-facing endpoints (web/mobile app)
- `/api/v1/admin/...` — super-admin panel (separate auth, IP whitelist, 2FA mandatory)
- `/ws/user/{user_id}`, `/ws/marketdata`, `/ws/admin` — real-time channels

## Phase status
- [x] **Phase 1** Foundation — auth, models, core infra, base scaffolding
- [ ] Phase 2 Admin core — segment settings, user-creation wizard, payin-out
- [ ] Phase 3 Market data — instruments, WS feed, marketwatch, charts
- [ ] Phase 4 Trading — validator, internal matching, positions, holdings
- [ ] Phase 5 Wallet — manual deposit/withdrawal flow, ledger
- [ ] Phase 6 Reports — PDF/Excel/CSV
- [ ] Phase 7 Polish — notifications, audit, backup, platform settings

See `../README.md` (root) for product spec.
