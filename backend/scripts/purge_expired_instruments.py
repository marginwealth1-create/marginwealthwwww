"""One-shot expiry purge — run the hourly sweep by hand, right now.

Use when expired contracts are still visible to users and you don't want to
wait for the `expiry_cleanup` loop (or the loop's leader worker is down).
Does exactly what the loop does:

  • settles OPEN positions in expired contracts (books P&L at last-known
    price, releases margin, flips the row CLOSED)
  • deletes the contract from EVERY user's watchlist / favourites
  • unsubscribes its token from the Zerodha ticker
  • marks the Instrument inactive so search stops returning it

The Kite catalog is warmed first, because the "delisted" rule (a derivative
that Kite no longer lists at all — how a mid-month MCX contract like
CRUDEOIL…JULFUT is detected) only fires on a warm catalog.

    cd /root/marginwealth/backend && source .venv/bin/activate
    python -m scripts.purge_expired_instruments --dry-run   # preview
    python -m scripts.purge_expired_instruments             # purge

Idempotent — running it twice is a no-op the second time.
"""

from __future__ import annotations

import asyncio
import sys

from app.core.database import close_database, init_database
from app.core.redis_client import close_redis, init_redis


async def _warm_catalog() -> None:
    from app.services.zerodha_service import zerodha

    for ex_key in ("NSE", "NFO", "MCX", "BFO", "BSE"):
        try:
            rows = await zerodha.fetch_instruments(ex_key)
            print(f"  catalog {ex_key:<4} {len(rows or [])} rows")
        except Exception as e:  # noqa: BLE001
            print(f"  catalog {ex_key:<4} FAILED — {e}")


async def _preview() -> None:
    """List what a real run would remove, without touching anything."""
    from app.models.instrument import Instrument
    from app.models.watchlist import WatchlistItem
    from app.services.expiry_cleanup import (
        _ist_today_date,
        catalog_index,
        is_dead_contract,
    )

    today = _ist_today_date()
    idx, warm = await catalog_index(force=True)
    print(f"\ntoday (IST) = {today}   warm catalogs = {sorted(warm) or 'NONE'}")
    if not warm:
        print("  ! Zerodha catalog is cold — only date-based expiry will be caught")

    dead_tokens: set[str] = set()
    insts = await Instrument.find(
        {"is_active": True, "instrument_type": {"$in": ["FUT", "CE", "PE"]}}
    ).to_list()
    for i in insts:
        if is_dead_contract(
            token=i.token,
            symbol=i.symbol,
            exchange=str(i.exchange),
            expiry=i.expiry,
            today=today,
            idx=idx,
            warm=warm,
        ):
            dead_tokens.add(i.token)
            print(f"  DEAD  {i.symbol:<28} {str(i.exchange):<4} expiry={i.expiry}")

    print(f"\n{len(dead_tokens)} dead contract(s) of {len(insts)} active F&O rows")

    wl = await WatchlistItem.find({}).to_list()
    hits = [w for w in wl if str(w.instrument_token) in dead_tokens]
    print(f"{len(hits)} watchlist row(s) would be removed:")
    for w in hits[:50]:
        print(f"  - {w.symbol} ({w.instrument_token})")
    if len(hits) > 50:
        print(f"  … +{len(hits) - 50} more")


async def main(dry_run: bool) -> None:
    await init_database()
    try:
        await init_redis()
    except Exception as e:  # noqa: BLE001
        print(f"redis unavailable ({e}) — continuing, sweep is Mongo-driven")

    print("warming Kite instrument catalog…")
    await _warm_catalog()

    if dry_run:
        await _preview()
    else:
        from app.services.expiry_cleanup import cleanup_expired_once

        res = await cleanup_expired_once()
        print("\nswept:")
        for k, v in res.items():
            print(f"  {k:<20} {v}")

    try:
        await close_redis()
    except Exception:  # noqa: BLE001
        pass
    await close_database()


if __name__ == "__main__":
    asyncio.run(main(dry_run="--dry-run" in sys.argv))
