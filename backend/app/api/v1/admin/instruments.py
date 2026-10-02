"""Admin instrument management."""

from __future__ import annotations

from datetime import date

from beanie import PydanticObjectId
from bson import Decimal128
from fastapi import APIRouter, HTTPException, Query

from app.core.dependencies import CurrentAdmin
from app.models._base import Exchange, InstrumentType
from app.models.instrument import Instrument
from app.schemas.common import APIResponse
from app.services import instrument_service, market_data_service

router = APIRouter(prefix="/instruments", tags=["admin-instruments"])


def _ser(i: Instrument) -> dict:
    # Self-heal display names for older derivatives rows whose `name` was
    # stored as the bare underlying (Zerodha CSV behaviour). See
    # instrument_service.display_name for the composition rule.
    stored_name = i.name or ""
    it_val = i.instrument_type.value if hasattr(i.instrument_type, "value") else str(i.instrument_type)
    if (it_val or "").upper() in ("FUT", "CE", "PE") and " " not in stored_name:
        display = instrument_service.display_name(
            instrument_type=i.instrument_type,
            underlying=stored_name,
            expiry=i.expiry,
            strike=i.strike,
        )
    else:
        display = stored_name
    return {
        "id": str(i.id),
        "token": i.token,
        "symbol": i.symbol,
        "trading_symbol": i.trading_symbol,
        "name": display,
        "exchange": str(i.exchange),
        "segment": i.segment,
        "instrument_type": it_val,
        "lot_size": i.lot_size,
        "tick_size": str(i.tick_size),
        "expiry": str(i.expiry) if i.expiry else None,
        "strike": str(i.strike) if i.strike else None,
        "option_type": str(i.option_type) if i.option_type else None,
        "is_active": i.is_active,
        "is_tradable": i.is_tradable,
        "is_halted": i.is_halted,
    }


@router.get("", response_model=APIResponse[dict])
async def list_instruments(
    admin: CurrentAdmin,
    q: str | None = None,
    exchange: str | None = None,
    segment: str | None = None,
    netting_segment: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    items, total = await instrument_service.list_paginated(
        page=page,
        page_size=page_size,
        exchange=exchange,
        segment=segment,
        netting_segment=netting_segment,
        q=q,
    )
    return APIResponse(
        data={
            "items": [_ser(i) for i in items],
            "meta": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (total + page_size - 1) // page_size,
            },
        }
    )


@router.post("", response_model=APIResponse[dict])
async def create_instrument(payload: dict, admin: CurrentAdmin):
    inst = Instrument(
        token=payload["token"],
        symbol=payload.get("symbol", payload["token"]),
        trading_symbol=payload.get("trading_symbol", payload["token"]),
        name=payload.get("name", payload["token"]),
        exchange=Exchange(payload.get("exchange", "NSE")),
        segment=payload.get("segment", "NSE_EQUITY"),
        instrument_type=InstrumentType(payload.get("instrument_type", "EQ")),
        lot_size=int(payload.get("lot_size", 1)),
        tick_size=Decimal128(str(payload.get("tick_size", "0.05"))),
        is_active=bool(payload.get("is_active", True)),
        is_tradable=bool(payload.get("is_tradable", True)),
    )
    await inst.insert()
    return APIResponse(data={"id": str(inst.id)})


@router.put("/{instrument_id}", response_model=APIResponse[dict])
async def update_instrument(instrument_id: str, payload: dict, admin: CurrentAdmin):
    i = await Instrument.get(PydanticObjectId(instrument_id))
    if i is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    for k in ("symbol", "trading_symbol", "name", "segment", "lot_size", "is_active", "is_tradable", "is_halted", "halt_reason"):
        if k in payload:
            setattr(i, k, payload[k])
    if "tick_size" in payload:
        i.tick_size = Decimal128(str(payload["tick_size"]))
    await i.save()
    return APIResponse(data={"id": str(i.id)})


@router.post("/{instrument_id}/halt", response_model=APIResponse[dict])
async def halt(instrument_id: str, payload: dict, admin: CurrentAdmin):
    i = await Instrument.get(PydanticObjectId(instrument_id))
    if i is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    i.is_halted = True
    i.halt_reason = payload.get("reason")
    await i.save()
    return APIResponse(data={"id": str(i.id), "is_halted": True})


@router.post("/{instrument_id}/resume", response_model=APIResponse[dict])
async def resume(instrument_id: str, admin: CurrentAdmin):
    i = await Instrument.get(PydanticObjectId(instrument_id))
    if i is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    i.is_halted = False
    i.halt_reason = None
    await i.save()
    return APIResponse(data={"id": str(i.id), "is_halted": False})


@router.delete("/{instrument_id}", response_model=APIResponse[dict])
async def delete_instrument(instrument_id: str, admin: CurrentAdmin):
    i = await Instrument.get(PydanticObjectId(instrument_id))
    if i is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    await i.delete()
    return APIResponse(data={"ok": True})


@router.post("/expiry-cleanup", response_model=APIResponse[dict])
async def run_expiry_cleanup(admin: CurrentAdmin):
    """Run the expiry sweep NOW instead of waiting for the hourly loop.

    Same routine the background loop runs: settles open positions in expired
    contracts, deletes the contract from every user's watchlist, unsubscribes
    it from the Zerodha ticker and marks it inactive so search stops returning
    it. Idempotent — safe to hit twice.
    """
    from app.services.expiry_cleanup import cleanup_expired_once
    from app.services.zerodha_service import zerodha as _zerodha

    # The delisted-contract rule only fires on a WARM catalog, so pre-warm
    # here — otherwise a manual run right after a restart would skip it.
    for ex_key in ("NSE", "NFO", "MCX", "BFO", "BSE"):
        try:
            await _zerodha.fetch_instruments(ex_key)
        except Exception:  # noqa: BLE001
            pass

    return APIResponse(data=await cleanup_expired_once())


@router.post("/repair-index-lots", response_model=APIResponse[dict])
async def repair_index_lots(admin: CurrentAdmin):
    """Re-syncs F&O lot sizes and heals equity rows.

    Source of truth per exchange (platform-owned tables WIN over CSV):
      • MCX → in-process `MCX_LOT_SIZES` table.
      • NSE / BSE F&O index contracts (NIFTY / BANKNIFTY / SENSEX / …)
        → `INDEX_LOT_SIZES` platform table. We override Zerodha's CSV
        here because contract size is a business decision in a B-book
        broker, not an exchange constant.
      • NSE / BSE F&O stock options → live Zerodha CSV `lotSize`
        (no platform override for individual stocks).
      • Equity / spot → forced to 1 (1 share = 1 lot).

    Response surfaces enough state to verify the deploy + DB are aligned
    without shell access.
    """
    from app.seed.instruments import backfill_index_lot_sizes
    from app.services.index_lots import (
        INDEX_LOT_SIZES,
        MCX_LOT_SIZES,
        get_canonical_lot_size,
    )
    from app.services.zerodha_service import zerodha as _zerodha

    # Pre-warm Zerodha CSV caches so the diff sample below sees fresh data.
    for ex_key in ("NFO", "BFO", "MCX"):
        try:
            await _zerodha.fetch_instruments(ex_key)
        except Exception:
            pass
    csv_by_token: dict[int, dict] = {}
    for ex_key in ("NFO", "BFO", "MCX"):
        for r in _zerodha._instruments_cache.get(ex_key, []):
            tok = r.get("token")
            if tok:
                try:
                    csv_by_token[int(tok)] = r
                except (TypeError, ValueError):
                    continue

    rows = await Instrument.find(
        {"instrument_type": {"$in": [InstrumentType.CE.value, InstrumentType.PE.value, InstrumentType.FUT.value]}}
    ).limit(2000).to_list()
    sample_before: list[dict] = []
    for inst in rows:
        ex_val = inst.exchange.value if hasattr(inst.exchange, "value") else str(inst.exchange)
        target: int | None = None
        source = ""
        target = get_canonical_lot_size(
            inst.symbol,
            inst.name,
            exchange=ex_val,
            instrument_type=inst.instrument_type.value,
        )
        if target is not None:
            source = "platform_canonical"
        elif ex_val != "MCX":
            # Stock options / non-index F&O fall back to the Zerodha CSV
            # because there's no platform-set lot for them.
            try:
                csv_row = csv_by_token.get(int(inst.token))
            except (TypeError, ValueError):
                csv_row = None
            if csv_row is not None:
                csv_lot = int(csv_row.get("lotSize") or 0)
                if csv_lot > 0:
                    target = csv_lot
                    source = "zerodha_csv"
        if target and int(inst.lot_size or 0) != target:
            sample_before.append({
                "symbol": inst.symbol,
                "exchange": ex_val,
                "current_lot": inst.lot_size,
                "target_lot": target,
                "source": source,
            })
            if len(sample_before) >= 8:
                break

    fixed = await backfill_index_lot_sizes()

    # Second pass: equity rows should have lot_size = 1. Anything else
    # was either a stale ETF marketlot import or a corrupt seeded value
    # that would silently inflate `qty = lots × lot_size` on the next
    # equity order. Heal in-place; reports `eq_rows_fixed` so the admin
    # can confirm the count after the deploy.
    eq_rows = await Instrument.find(
        {"instrument_type": {"$nin": [
            InstrumentType.CE.value,
            InstrumentType.PE.value,
            InstrumentType.FUT.value,
        ]}}
    ).to_list()
    eq_fixed = 0
    for inst in eq_rows:
        if int(inst.lot_size or 0) != 1:
            inst.lot_size = 1
            try:
                await inst.save()
                eq_fixed += 1
            except Exception:
                pass

    # Third pass: heal stale segment strings on already-mirrored rows.
    # Pre-2026-05 mirror code stored Kite exchange codes as segments
    # (`BFO_FUT`, `BFO_OPT`, `MCX_OPT`, `NFO_FUT`, `NFO_OPT`) which
    # don't appear in the netting resolver's segment map — so the
    # admin's per-segment settings were silently ignored for those
    # rows. Map them to the canonical SegmentType values now so the
    # next quote/order pulls the right margin/leverage/limits.
    SEG_REMAP: dict[str, dict[str, str]] = {
        "BFO_FUT": {"FUT": "BSE_FUTURE"},
        "BFO_OPT": {"CE": "BSE_OPTION_BUY", "PE": "BSE_OPTION_SELL"},
        "NFO_FUT": {"FUT": "NSE_FUTURE"},
        "NFO_OPT": {"CE": "NSE_STOCK_OPTION_BUY", "PE": "NSE_STOCK_OPTION_SELL"},
        "MCX_FUT": {"FUT": "MCX_FUTURE"},
        "MCX_OPT": {"CE": "MCX_OPTION_BUY", "PE": "MCX_OPTION_SELL"},
    }
    stale_segs = list(SEG_REMAP.keys())
    seg_stale_rows = await Instrument.find(
        {"segment": {"$in": stale_segs}}
    ).to_list()
    seg_fixed = 0
    _idx_prefixes = (
        "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "MIDCAPNIFTY",
        "SENSEX", "BANKEX",
    )
    for inst in seg_stale_rows:
        current = inst.segment
        it_val = inst.instrument_type.value if hasattr(inst.instrument_type, "value") else str(inst.instrument_type)
        target = SEG_REMAP.get(current, {}).get(it_val)
        if not target:
            continue
        # NSE F&O: stock vs index routing depends on the underlying
        # symbol prefix. Indices get *_INDEX_*, everything else stays
        # at *_STOCK_* / *_FUTURE*.
        if target.startswith("NSE_STOCK_OPTION_") and (inst.symbol or "").upper().startswith(_idx_prefixes):
            target = target.replace("NSE_STOCK_OPTION_", "NSE_INDEX_OPTION_")
        if target == "NSE_FUTURE" and (inst.symbol or "").upper().startswith(_idx_prefixes):
            target = "NSE_INDEX_FUTURE"
        if inst.segment == target:
            continue
        inst.segment = target
        try:
            await inst.save()
            seg_fixed += 1
        except Exception:
            pass

    # Fourth pass: heal Infoway-mirrored instruments (FOREX / COMMODITIES /
    # INDICES / STOCKS / CRYPTO segments) so their `lot_size` reflects
    # the retail-CFD contract size from `infoway_lots.py`. Pre-table
    # rows were all seeded at lot_size=1 which silently understated
    # forex notional by 100,000× and spot-gold by 100×.
    from app.services.infoway_lots import (
        INFOWAY_LOT_SIZES,
        get_infoway_lot_size,
    )

    infoway_segments = ("FOREX", "STOCKS", "INDICES", "COMMODITIES")
    crypto_like = await Instrument.find({"segment": {"$regex": "CRYPTO"}}).to_list()
    infoway_rows = await Instrument.find(
        {"segment": {"$in": list(infoway_segments)}}
    ).to_list()
    infoway_rows = infoway_rows + crypto_like
    infoway_fixed = 0
    for inst in infoway_rows:
        target = get_infoway_lot_size(inst.symbol, inst.segment)
        if int(inst.lot_size or 0) != target:
            inst.lot_size = target
            try:
                await inst.save()
                infoway_fixed += 1
            except Exception:
                pass

    return APIResponse(data={
        "index_canonical_table": [{"prefix": p, "lot": l} for p, l in INDEX_LOT_SIZES],
        "mcx_canonical_table": [{"prefix": p, "lot": l} for p, l in MCX_LOT_SIZES],
        "infoway_lot_table": INFOWAY_LOT_SIZES,
        "rows_scanned": len(rows),
        "rows_fixed": fixed,
        "eq_rows_scanned": len(eq_rows),
        "eq_rows_fixed": eq_fixed,
        "segment_remap_scanned": len(seg_stale_rows),
        "segment_remap_fixed": seg_fixed,
        "infoway_rows_scanned": len(infoway_rows),
        "infoway_rows_fixed": infoway_fixed,
        "sample_before_fix": sample_before,
    })


# ── F&O underlyings dedupe ──────────────────────────────────────────
# Cache the deduped underlyings per exchange for 5 min. The Zerodha cache
# itself doesn't change intraday, so a 5 min TTL is plenty and avoids
# rescanning ~50k NFO rows on every keystroke of the script-add typeahead.
import time as _time

_UNDERLYINGS_CACHE: dict[str, tuple[list[str], float]] = {}
_UNDERLYINGS_TTL = 300.0


def _extract_underlying(symbol: str) -> str | None:
    """Strip the expiry / strike / type suffix from a derivative trading
    symbol and return just the underlying name.

    Rule: take everything before the first digit. Works because every
    real Indian derivative symbol encodes the expiry (or strike) as a
    digit chunk right after the underlying (NIFTY26MAYFUT,
    BANKNIFTY26MAY52500CE, M&M26MAYFUT, GOLD26MAYFUT). Returns None for
    symbols that don't contain a digit at all — those aren't derivatives.
    """
    s = (symbol or "").upper()
    for i, c in enumerate(s):
        if c.isdigit():
            return s[:i] if i > 0 else None
    return None


@router.get("/underlyings", response_model=APIResponse[list[str]])
async def list_underlyings(
    admin: CurrentAdmin,
    exchange: str = Query(..., description="NFO / BFO / MCX"),
    contract_type: str | None = Query(
        default=None, description="FUT | CE | PE — restrict to futures or one option side"
    ),
    q: str | None = Query(default=None, description="Prefix filter, case-insensitive"),
    limit: int = Query(default=20, ge=1, le=100),
):
    """Deduped list of derivative underlyings for the segment matrix's
    script-add typeahead.

    Returns underlying names (NIFTY, BANKNIFTY, SBIN, …) — never
    individual contracts. Combined with the resolver's pattern matching,
    one selection here applies the override to every contract of that
    underlying.

    For OPT segments the frontend asks for both `contract_type=CE` and
    `contract_type=PE` and renders the underlying twice (once per side).
    """
    ex = exchange.strip().upper()
    cache_key = f"{ex}|{(contract_type or '').upper()}"
    now = _time.time()
    cached = _UNDERLYINGS_CACHE.get(cache_key)
    if cached and (now - cached[1]) < _UNDERLYINGS_TTL:
        names = cached[0]
    else:
        from app.services.zerodha_service import zerodha as _zerodha

        try:
            rows = await _zerodha.fetch_instruments(ex)
        except Exception:
            rows = _zerodha._instruments_cache.get(ex, [])

        ct = (contract_type or "").upper()
        seen: set[str] = set()
        names_list: list[str] = []
        for row in rows:
            it = (row.get("instrumentType") or "").upper()
            if ct and it != ct:
                continue
            if not ct and it not in ("FUT", "CE", "PE"):
                continue
            und = _extract_underlying(row.get("symbol"))
            if not und or und in seen:
                continue
            seen.add(und)
            names_list.append(und)
        names_list.sort()
        _UNDERLYINGS_CACHE[cache_key] = (names_list, now)
        names = names_list

    if q:
        qu = q.strip().upper()
        names = [n for n in names if n.startswith(qu)]
    return APIResponse(data=names[:limit])


# ── Read-only Market Watch support for the admin panel ────────────────
# Lets an admin browse the same live bid/ask the trader sees, without
# placing orders. Pure proxy onto `market_data_service.get_quotes` —
# zero new domain logic.


@router.get("/quotes/batch", response_model=APIResponse[list])
async def quotes_batch(
    admin: CurrentAdmin,
    tokens: str = Query(description="comma-separated instrument tokens"),
):
    """Batch quote fetch for the admin Market Watch page.

    The user-facing version lives at /user/instruments/quotes/batch and
    the panel was previously calling that with an admin token, which
    works but pollutes audit logs with /user/* calls from admin
    accounts. This is the same payload shape, on the admin prefix, so
    the Market Watch page can reuse the existing useMarketStream
    seeding pattern without crossing role boundaries.
    """
    tlist = [t for t in (tokens or "").split(",") if t]
    if not tlist:
        return APIResponse(data=[])
    quotes = await market_data_service.get_quotes(tlist)
    # Attach the token onto each row so the frontend doesn't need a
    # parallel `tokens` array to index by — match the user endpoint's
    # shape exactly.
    return APIResponse(
        data=[{"token": tok, **q} for tok, q in zip(tlist, quotes)]
    )
