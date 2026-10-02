"""Repair the identity faults that leave Indian instruments unpriced.

Measured on production 2026-09-17: crypto, forex, metals and indices price
fine, while Indian rows split into two half-broken halves.

1. Subscription rows whose `symbol` is the token number — 435 of 576. FULL
   mode is re-asserted BY SYMBOL, so those rows receive no OHLC: day high/low
   stay 0, the day-range order gate can't apply, and the admin panel shows a
   wall of bare numbers.
2. Catalog rows with a numeric token but `symbol == token` — 430 NSE_EQUITY
   rows read as "341249" in search instead of HDFCBANK.
3. Catalog rows with a symbol-style token (NSE_EQ_RELIANCE, NSE_IDX_*) — the
   Kite feed is keyed by numeric token, so these can never tick. Most are the
   named half of a row whose numeric half is (2); those get deactivated once
   the numeric row carries the real symbol. The rest are re-pointed.

Everything is resolved against Kite's own catalog, so Zerodha must be
connected. International segments (FOREX / STOCKS / INDICES / COMMODITIES /
CRYPTO) are deliberately excluded: they are served by MetaAPI and Binance,
which key off the SYMBOL, so `symbol == token` is correct there.

    cd backend && source .venv/bin/activate
    python -m scripts.repair_feed_identity            # dry run, changes nothing
    python -m scripts.repair_feed_identity --apply
"""

from __future__ import annotations

import asyncio
import sys

from app.core.database import close_database, init_database
from app.models.instrument import Instrument
from app.models.zerodha_settings import ZerodhaSettings
from app.services.zerodha_service import zerodha

APPLY = "--apply" in sys.argv

INDIAN_SEGMENTS = {
    "NSE_EQUITY", "BSE_EQUITY", "MCX_FUTURE", "NFO_FUTURE", "NFO_OPTION",
    "BFO_OPTION", "NSE_INDEX_OPTION_BUY", "NSE_INDEX_OPTION_SELL",
    "NSE_FUTURE", "NSE_INDEX_FUTURE", "MCX_OPTION_BUY", "MCX_OPTION_SELL",
}

# Kite spells the indices out; our catalog uses the F&O root.
INDEX_ALIASES = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "FINNIFTY": "NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NIFTY MID SELECT",
    "NIFTYNXT50": "NIFTY NEXT 50",
    "SENSEX": "SENSEX",
    "BANKEX": "BANKEX",
}


def _tag() -> str:
    return "APPLY" if APPLY else "DRY RUN"


async def load_catalog() -> tuple[dict[int, dict], dict[str, dict]]:
    """token -> row and SYMBOL -> row, from Kite's catalog."""
    by_token: dict[int, dict] = {}
    by_symbol: dict[str, dict] = {}
    for ex in ("NSE", "NFO", "MCX", "BSE", "BFO"):
        try:
            rows = await zerodha.fetch_instruments(ex)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {ex}: catalog fetch failed ({exc})")
            continue
        print(f"  {ex}: {len(rows)} rows")
        for r in rows:
            tok = r.get("token")
            sym = (r.get("symbol") or "").upper()
            if tok:
                by_token[int(tok)] = r
            # First spelling wins: the NSE cash row for RELIANCE, not a future.
            if sym and sym not in by_symbol:
                by_symbol[sym] = r
    return by_token, by_symbol


def _kite_row_for_symbol(symbol: str, by_symbol: dict[str, dict]) -> dict | None:
    s = (symbol or "").upper()
    return by_symbol.get(s) or by_symbol.get(INDEX_ALIASES.get(s, ""))


# ── 1. subscriptions ─────────────────────────────────────────────────
async def fix_subscriptions(by_token: dict[int, dict]) -> None:
    s = await ZerodhaSettings.find_one(ZerodhaSettings.apiKey != "")
    if s is None:
        print("  no connected Zerodha settings row")
        return
    subs = s.subscribedInstruments or []
    symless = [i for i in subs if str(i.symbol) == str(i.token)]
    print(f"  symbol-less rows: {len(symless)} of {len(subs)}")
    fixed = 0
    for i in symless:
        row = by_token.get(int(i.token))
        if not row or not row.get("symbol"):
            continue
        if APPLY:
            i.symbol = row["symbol"]
            i.exchange = row.get("exchange") or i.exchange
        fixed += 1
        if fixed <= 4:
            print(f"    {i.token} -> {row['symbol']} ({row.get('exchange')})")
    print(f"  {'repaired' if APPLY else 'would repair'}: {fixed}; unresolved: {len(symless) - fixed}")
    if APPLY and fixed:
        await s.save()


# ── 2. numeric catalog rows with no real symbol ──────────────────────
async def name_numeric_rows(by_token: dict[int, dict]) -> int:
    rows = [
        i
        for i in await Instrument.find().to_list()
        if str(i.segment) in INDIAN_SEGMENTS
        and str(i.token).isdigit()
        and (i.symbol or "") == str(i.token)
    ]
    print(f"  unnamed numeric rows: {len(rows)}")
    named = 0
    for inst in rows:
        row = by_token.get(int(inst.token))
        if not row or not row.get("symbol"):
            continue
        if named < 4:
            print(f"    {inst.token} -> {row['symbol']}")
        if APPLY:
            inst.symbol = row["symbol"]
            if row.get("name"):
                inst.name = row["name"]
            if row.get("exchange"):
                inst.exchange = row["exchange"]
            await inst.save()
        named += 1
    print(f"  {'named' if APPLY else 'would name'}: {named}; unresolved: {len(rows) - named}")
    return named


# ── 3. symbol-style Indian rows ──────────────────────────────────────
async def fix_ghost_instruments(by_symbol: dict[str, dict]) -> None:
    ghosts = [
        i
        for i in await Instrument.find(Instrument.is_active == True).to_list()  # noqa: E712
        if str(i.segment) in INDIAN_SEGMENTS and not str(i.token).isdigit()
    ]
    print(f"  symbol-style Indian rows: {len(ghosts)}")
    repointed = deactivated = skipped = 0
    for g in ghosts:
        row = _kite_row_for_symbol(g.symbol or "", by_symbol)
        if not row:
            skipped += 1
            print(f"    ? {g.token}: Kite has no symbol '{g.symbol}' — left alone (needs a manual mapping)")
            continue
        new_token = str(row["token"])
        twin = await Instrument.find_one(Instrument.token == new_token)
        if twin is not None:
            # The numeric half already exists (and step 2 just gave it the real
            # symbol), so this row is the duplicate. Deactivate rather than
            # delete: nothing references it, but a soft flag is reversible.
            if deactivated < 4:
                print(f"    - {g.token}: duplicate of {new_token} — deactivating")
            if APPLY:
                g.is_active = False
                g.is_tradable = False
                await g.save()
            deactivated += 1
            continue
        if repointed < 4:
            print(f"    {g.token} -> {new_token}  ({g.symbol}, {row.get('exchange')})")
        if APPLY:
            g.token = new_token
            if row.get("exchange"):
                g.exchange = row["exchange"]
            await g.save()
        repointed += 1
    verb = "" if APPLY else "would "
    print(f"  {verb}re-point: {repointed}; {verb}deactivate: {deactivated}; left alone: {skipped}")


async def main() -> None:
    print(f"=== repair_feed_identity ({_tag()}) ===")
    await init_database()
    try:
        print("Kite catalog:")
        by_token, by_symbol = await load_catalog()
        if not by_token:
            print("  catalog empty — is Zerodha connected? aborting")
            return
        print("1. Subscriptions:")
        await fix_subscriptions(by_token)
        print("2. Numeric catalog rows:")
        await name_numeric_rows(by_token)
        print("3. Symbol-style catalog rows:")
        await fix_ghost_instruments(by_symbol)
    finally:
        await close_database()


asyncio.run(main())
