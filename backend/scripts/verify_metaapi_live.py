"""Verify the RUNNING backend is delivering MetaAPI prices end-to-end.

diag_metaapi.py proves the MetaAPI terminal_state has live bid/ask. This
script proves the LIVE pipeline downstream of that — it reads exactly what the
running backend's tick_loop has mirrored into Redis ``mdlive:{token}`` (and the
display-only ``mdlast:{token}``) for the commodity/index Instrument docs.

  - mdlive present + ltp>0  -> the frontend WILL show a live price.
  - mdlive missing but the Instrument exists -> the token never reached
    tick_loop's publish set (not in _subscribed, or _state never seeded).
  - mdlast present but mdlive missing -> it ticked before but the live mirror
    went stale (feed leader / subscription gap).

Read-only. Run on the server (reads the shared Redis the backend writes):

    cd /root/marginwealth/backend && source .venv/bin/activate
    python -m scripts.verify_metaapi_live
"""

from __future__ import annotations

import asyncio
import json

from app.core.database import close_database, init_database
from app.core.redis_client import close_redis, get_redis, init_redis
from app.models.instrument import Instrument

# Platform symbols reported frozen at 0.0000 + a forex/metal control set.
SYMBOLS = [
    "USOIL", "UKOIL", "NATGAS",
    "SPX500", "DE40", "UK100", "US30", "NAS100",
    "EURUSD", "XAUUSD",   # controls (known-working)
]


async def main() -> None:
    await init_database()
    try:
        await init_redis()
    except Exception as e:  # noqa: BLE001
        print(f"❌ Redis init failed: {str(e)[:120]}")
        await close_database()
        return

    redis = get_redis()

    # Resolve each platform symbol -> its Instrument doc / token.
    docs = await Instrument.find(
        {"symbol": {"$in": SYMBOLS}}
    ).to_list()
    by_sym = {d.symbol.upper(): d for d in docs}

    print(f"{'SYMBOL':10}{'TOKEN':16}{'ACTIVE':8}{'MDLIVE_LTP':>12}{'MDLAST_LTP':>12}  VERDICT")
    print("-" * 84)

    for sym in SYMBOLS:
        d = by_sym.get(sym.upper())
        if d is None:
            print(f"{sym:10}{'—':16}{'—':8}{'—':>12}{'—':>12}  Instrument doc MISSING from catalog")
            continue
        tok = str(d.token)
        active = "yes" if d.is_active else "NO"

        mdlive_ltp = mdlast_ltp = 0.0
        raw_live = await redis.get(f"mdlive:{tok}")
        if raw_live:
            try:
                mdlive_ltp = float(json.loads(raw_live).get("ltp") or 0)
            except Exception:  # noqa: BLE001
                pass
        raw_last = await redis.get(f"mdlast:{tok}")
        if raw_last:
            try:
                mdlast_ltp = float(json.loads(raw_last).get("ltp") or 0)
            except Exception:  # noqa: BLE001
                pass

        if mdlive_ltp > 0:
            verdict = "OK — live to frontend"
        elif mdlast_ltp > 0:
            verdict = "STALE — ticked before, live mirror dropped (sub gap)"
        else:
            verdict = "NEVER PUBLISHED — token not in tick_loop set (_subscribed/_state)"

        print(
            f"{sym:10}{tok:16}{active:8}{mdlive_ltp:>12.4f}{mdlast_ltp:>12.4f}  {verdict}"
        )

    print("\nIf commodities/indices show NEVER PUBLISHED while EURUSD/XAUUSD are")
    print("OK, the MetaAPI symbols are subscribed upstream but their Instrument")
    print("tokens never enter the tick_loop publish set — the fix is to seed +")
    print("subscribe them like the forex tokens (report this output back).")

    try:
        await close_redis()
    except Exception:  # noqa: BLE001
        pass
    await close_database()


if __name__ == "__main__":
    asyncio.run(main())
