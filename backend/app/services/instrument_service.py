"""Instrument service — search, list, get-by-token."""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from beanie.operators import Or
from pymongo import ASCENDING

from app.core.exceptions import NotFoundError
from app.models._base import Exchange
from app.models.instrument import Instrument

logger = logging.getLogger(__name__)


def infer_instrument_type_from_symbol(symbol: str | None) -> str | None:
    """Best-effort guess of FUT / CE / PE from a Kite-style tradingsymbol.

    Zerodha symbols for derivatives follow the conventions:
        FUT  → "GOLD26JUNFUT", "NIFTY26JANFUT" (ends in "FUT")
        CE   → "NIFTY26JAN22500CE" (digit before "CE")
        PE   → "BANKNIFTY26JAN48000PE" (digit before "PE")
    Returns None when the suffix doesn't match any of those patterns —
    caller should treat it as EQ / unknown.
    """
    s = (symbol or "").upper()
    if not s:
        return None
    if s.endswith("FUT"):
        return "FUT"
    if len(s) >= 3 and s[-3].isdigit():
        if s.endswith("CE"):
            return "CE"
        if s.endswith("PE"):
            return "PE"
    return None


_MONTH_BY_ABBR = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# Approximate per-segment expiry-day-of-month, used only as a fallback when
# the stored Instrument.expiry is None. NSE/BSE F&O expire on the last
# Thursday of the month — derived dynamically (any other day in that month
# is wrong). MCX commodities vary by underlying (CRUDEOIL/NATGAS on 19th,
# SILVER/GOLD end-of-month-ish, COPPER 25th), so we pick a generic "near
# end of month" date — better than `None` for the expiry-day rule even
# if off by a few days.
_LAST_THURSDAY_SEGMENTS = ("NSE_FUT", "NSE_OPT", "BSE_FUT", "BSE_OPT", "NFO_FUT", "NFO_OPT", "BFO_FUT", "BFO_OPT")


def _last_thursday_of_month(year: int, month: int) -> date:
    from calendar import monthrange
    _, last_day = monthrange(year, month)
    d = date(year, month, last_day)
    # Thursday weekday() == 3
    while d.weekday() != 3:
        d = d.replace(day=d.day - 1)
    return d


_SYMBOL_EXPIRY_RE = re.compile(r"^([A-Z]+?)(\d{2})([A-Z]{3})(\d+(?:CE|PE)?|FUT)$")


def derive_expiry_from_symbol(symbol: str | None, segment: str | None = None) -> date | None:
    """Best-effort recovery of an instrument's expiry date from its
    Kite-style trading symbol. Only used as a runtime fallback when the
    stored `Instrument.expiry` is `None` (data-quality gap from the
    Zerodha sync) — otherwise the stored field always wins.

    Examples:
        CRUDEOIL26JULFUT          → 2026-07-{last-thursday/19} (MCX)
        NIFTY26JANFUT             → 2026-01-{last-thursday}    (NFO)
        NIFTY26JAN22500CE         → 2026-01-{last-thursday}    (NFO)

    Returns `None` when the symbol doesn't match the YY-MMM pattern or
    the month abbreviation is unknown. Callers must treat a non-None
    return as approximate (off by ≤ a week for MCX) and prefer the
    stored expiry whenever it's populated.
    """
    s = (symbol or "").upper()
    m = _SYMBOL_EXPIRY_RE.match(s)
    if not m:
        return None
    _, yy, mmm, _ = m.groups()
    month = _MONTH_BY_ABBR.get(mmm)
    if month is None:
        return None
    try:
        year = 2000 + int(yy)
    except ValueError:
        return None
    seg_up = (segment or "").upper()
    if any(seg_up.startswith(p) for p in _LAST_THURSDAY_SEGMENTS) or "_FNO" in seg_up:
        return _last_thursday_of_month(year, month)
    # MCX_FUT / MCX_OPT (and any unknown segment): use the 19th — close to
    # CRUDEOIL/NATGAS expiry (19th) and within a week of GOLD/SILVER (end
    # of month). Worst case the expiry-day rule fires a few days off.
    return date(year, month, 19)


def effective_expiry(instrument) -> date | None:
    """Return `instrument.expiry` when populated, else a best-effort
    derivation from the symbol. Centralises the fallback so callers
    (validator, segment-settings preview, expiry-cleanup loop) all see
    the same date for instruments with missing data."""
    stored = getattr(instrument, "expiry", None)
    if stored:
        return stored
    symbol = getattr(instrument, "symbol", None)
    segment = getattr(instrument, "segment", None)
    seg_val = getattr(segment, "value", segment)
    return derive_expiry_from_symbol(symbol, seg_val)


def display_name(
    *,
    instrument_type: Any,
    underlying: str,
    expiry: Any = None,
    strike: Any = None,
) -> str:
    """Build a human-friendly contract name.

    Zerodha's CSV `name` field for derivatives is the bare underlying
    ("GOLDM", "CRUDEOIL", "NIFTY") which renders as a useless duplicate of
    the symbol on listings. For FUT/CE/PE, compose `"{underlying} {expiry}
    [{strike}] {type}"` instead. Equity / index rows pass through.
    """
    it = instrument_type.value if hasattr(instrument_type, "value") else str(instrument_type or "")
    it = (it or "").upper()
    if it not in ("FUT", "CE", "PE"):
        return underlying or ""

    parts: list[str] = [underlying or ""]
    if expiry:
        try:
            parts.append(expiry.strftime("%d-%b-%Y").upper())
        except AttributeError:
            try:
                from datetime import datetime as _dt

                parts.append(_dt.fromisoformat(str(expiry)[:10]).strftime("%d-%b-%Y").upper())
            except Exception:
                pass
    if it in ("CE", "PE") and strike is not None:
        try:
            sv = float(str(strike))
            parts.append(str(int(sv)) if sv == int(sv) else f"{sv:g}")
        except Exception:
            pass
    parts.append(it)
    return " ".join(p for p in parts if p)


async def search(
    q: str | None,
    *,
    exchange: str | None = None,
    segment: str | list[str] | None = None,
    instrument_type: str | list[str] | None = None,
    limit: int = 30,
) -> list[Instrument]:
    """Case-insensitive prefix/contains search on symbol+name.

    `segment` and `instrument_type` accept either a single value or a list —
    the side panel's bucket chips (e.g. "NSE OPT") need to match BOTH
    `NSE_INDEX_OPTION_BUY` and `NSE_INDEX_OPTION_SELL`, so a single string is
    not enough. Lists become `$in` filters in the underlying Mongo query.
    """
    query: dict[str, Any] = {"is_active": True}
    if exchange:
        query["exchange"] = exchange
    if segment:
        query["segment"] = {"$in": list(segment)} if isinstance(segment, list) else segment
    if instrument_type:
        query["instrument_type"] = (
            {"$in": list(instrument_type)} if isinstance(instrument_type, list) else instrument_type
        )

    if q:
        # ANCHORED prefix match on the lowercased, indexed shadow fields. The
        # old code used a case-insensitive, un-anchored regex which no index
        # can serve, so every keystroke scanned the entire (~15k-row) catalog
        # (~150 ms CPU each) and starved the worker event loop at market open.
        # A `^prefix` regex on an indexed field is an index range-scan, so this
        # stays fast regardless of catalog size. Behaviour change: search is
        # now "starts-with" (standard autocomplete) instead of "contains";
        # users search by the START of a symbol / name (e.g. "nif" → NIFTY…,
        # "rel" → RELIANCE). Exact-token lookup is preserved for direct hits.
        prefix = {"$regex": f"^{re.escape(q.lower())}"}
        query["$or"] = [
            {"symbol_lc": prefix},
            {"trading_symbol_lc": prefix},
            {"name_lc": prefix},
            {"token": q},
        ]

    cursor = Instrument.find(query).sort([("symbol", ASCENDING)]).limit(limit)
    return await cursor.to_list()


async def get_by_token(token: str) -> Instrument:
    """Resolve an instrument by token. Falls back to the Zerodha CSV cache
    so that option chain legs (and any other Kite instrument the user clicks
    via search) get auto-mirrored into our `instruments` collection on first
    use — and on-demand-subscribed to the live ticker so prices flow.

    This is what makes "click an option strike → chart opens with live data
    → trades work" possible without admin pre-seeding every contract."""
    inst = await Instrument.find_one(Instrument.token == token)
    if inst is not None:
        # Heal stubs created with token-as-symbol (e.g. on-demand mirror that
        # ran while the Zerodha CSV cache was cold — clearing subscriptions
        # wipes that cache, so a fresh resolve fell back to `str(token)` for
        # symbol/name). Re-resolve ONCE from the catalog so the user stops
        # seeing the bare token on the positions / market list. No-op (returns
        # the stub unchanged) when the catalog still can't resolve it.
        if (inst.symbol or "") == str(inst.token):
            try:
                healed = await _mirror_from_zerodha(token, existing=inst)
                if healed is not None:
                    return healed
            except Exception:
                pass
        return inst

    # Try to mirror from the Zerodha in-memory CSV cache.
    inst = await _mirror_from_zerodha(token)
    if inst is not None:
        return inst

    # Not Zerodha's — the user may have picked a CFD symbol straight out of
    # the broker's universe in search, which has no row until now.
    inst = await _mirror_from_metaapi(token)
    if inst is not None:
        return inst

    raise NotFoundError(f"Instrument {token} not found")


async def _mirror_from_metaapi(token: str) -> "Instrument | None":
    """Create the catalogue row for a symbol the MT broker offers but we have
    never stored. Returns None when the broker doesn't carry it, so unknown
    tokens still fail loudly."""
    try:
        from app.services.metaapi_service import clean_broker_symbol, metaapi

        symbol = clean_broker_symbol(token)
        if not symbol or not await metaapi.offers(symbol):
            return None
        from app.services.infoway_service import upsert_instrument_for_code

        await upsert_instrument_for_code(symbol)
        return await Instrument.find_one(Instrument.token == symbol)
    except Exception:
        logger.exception("metaapi_instrument_mirror_failed", extra={"token": token})
        return None


async def _mirror_from_zerodha(token: str, existing: "Instrument | None" = None) -> Instrument | None:
    """Look up `token` across the cached Kite instrument dumps (NSE/NFO/BFO/
    MCX/BSE) and create a local Instrument doc on the fly. Also fires an
    on-demand WS subscribe so the next quote/tick request finds live data.
    Returns None if Zerodha doesn't know this token either.

    When `existing` is passed (a stub doc whose symbol == token), the resolved
    fields are written back onto THAT doc instead of inserting a duplicate —
    this is the self-heal path for `get_by_token`."""
    from datetime import datetime

    from bson import Decimal128

    from app.models._base import Exchange, InstrumentType, OptionType
    from app.services.zerodha_service import zerodha

    # Token must be numeric to belong to Zerodha; Infoway/synthetic tokens
    # like "CRYPTO_BTCUSD" should fail loudly via the original NotFoundError.
    try:
        token_int = int(token)
    except (TypeError, ValueError):
        return None

    catalog_row: dict | None = None
    catalog_exchange: str | None = None
    for ex in ("NSE", "NFO", "BFO", "MCX", "BSE"):
        try:
            rows = await zerodha.fetch_instruments(ex)
        except Exception:
            continue
        for row in rows:
            try:
                if int(row.get("token") or 0) == token_int:
                    catalog_row = row
                    catalog_exchange = ex
                    break
            except (TypeError, ValueError):
                continue
        if catalog_row is not None:
            break

    if catalog_row is None:
        return None

    sym = catalog_row.get("symbol") or str(token_int)
    name = catalog_row.get("name") or sym
    exch_str = (catalog_row.get("exchange") or catalog_exchange or "NSE").upper()
    try:
        exchange = Exchange(exch_str)
    except Exception:
        exchange = Exchange.NSE

    it_str = (catalog_row.get("instrumentType") or "EQ").upper()
    if it_str in ("CE", "PE"):
        instrument_type = InstrumentType.CE if it_str == "CE" else InstrumentType.PE
        option_type = OptionType.CE if it_str == "CE" else OptionType.PE
    elif it_str == "FUT":
        instrument_type = InstrumentType.FUT
        option_type = None
    else:
        instrument_type = InstrumentType.EQ
        option_type = None

    # Canonical segment name. The exchange string from Kite's CSV is the
    # exchange CODE ("NFO" / "BFO" / "MCX") — naming the segment after
    # that produces strings (`BFO_FUT`, `BFO_OPT`, `MCX_OPT`) that don't
    # appear anywhere in `_SEGMENT_NAME_MAP`, so the netting resolver
    # falls through to permissive defaults and the admin's row settings
    # are silently ignored. Use the same canonical segment-type table
    # `_auto_create_instrument` uses so all instrument-creation paths
    # agree on the segment naming.
    sym_up = (sym or "").upper()
    _idx_prefixes = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "MIDCAPNIFTY", "SENSEX", "BANKEX")
    is_index_underlying = sym_up.startswith(_idx_prefixes)
    seg_map: dict[str, dict[str, str]] = {
        "NFO": {
            "CE": "NSE_INDEX_OPTION_BUY" if is_index_underlying else "NSE_STOCK_OPTION_BUY",
            "PE": "NSE_INDEX_OPTION_SELL" if is_index_underlying else "NSE_STOCK_OPTION_SELL",
            "FUT": "NSE_INDEX_FUTURE" if is_index_underlying else "NSE_FUTURE",
        },
        "NSE": {"EQ": "NSE_EQUITY", "INDEX": "NSE_EQUITY"},
        "BSE": {"EQ": "BSE_EQUITY"},
        # BSE F&O (SENSEX / BANKEX / stock F&O). The MAIN seed files every BFO
        # CE+PE into a single `BFO_OPTION` segment and futures into
        # `BFO_FUTURE` (382 BANKEX + 1345 SENSEX options live there). This
        # on-demand mirror previously split them into BSE_OPTION_BUY /
        # BSE_OPTION_SELL / BSE_FUTURE — segments the admin never configured —
        # so any strike NOT in the main seed (created on the fly when a user
        # opened it) landed in the wrong segment and resolved a wrong/default
        # margin (operator-caught: BANKEX26JUN66900CE got ₹50 instead of the
        # ₹7,000 its BFO_OPTION sibling got). Keep this in lock-step with the
        # seed so on-the-fly strikes get the SAME segment → SAME margin.
        "BFO": {
            "CE": "BFO_OPTION",
            "PE": "BFO_OPTION",
            "FUT": "BFO_FUTURE",
        },
        "MCX": {
            "CE": "MCX_OPTION_BUY",
            "PE": "MCX_OPTION_SELL",
            "FUT": "MCX_FUTURE",
        },
        "CDS": {
            "CE": "CDS_OPTION_BUY",
            "PE": "CDS_OPTION_SELL",
            "FUT": "CDS_FUTURE",
        },
    }
    segment = seg_map.get(exch_str, {}).get(it_str, f"{exch_str}_{it_str}")

    expiry_d = None
    exp_raw = catalog_row.get("expiry")
    if exp_raw:
        try:
            expiry_d = datetime.fromisoformat(str(exp_raw).replace("Z", "+00:00")).date()
        except Exception:
            try:
                expiry_d = datetime.strptime(str(exp_raw)[:10], "%Y-%m-%d").date()
            except Exception:
                expiry_d = None

    strike_val = catalog_row.get("strike")
    strike_money = None
    if strike_val is not None:
        try:
            strike_money = Decimal128(str(float(strike_val)))
        except Exception:
            strike_money = None

    tick_size_val = catalog_row.get("tickSize") or 0.05
    try:
        tick_money = Decimal128(str(float(tick_size_val)))
    except Exception:
        tick_money = Decimal128("0.05")

    # Canonical lot table wins over the Zerodha CSV — see index_lots.py.
    # For Indian index F&O, fresh contracts may arrive with lotSize=0 in the
    # cache and stick at 1 forever. For MCX, Zerodha reports lot_size in raw
    # units (kg, g, mmBtu, barrels) which doesn't match `qty = lots ×
    # lot_size`. The canonical table overrides both cases.
    from app.services.index_lots import get_canonical_lot_size

    csv_lot = int(catalog_row.get("lotSize") or 0)
    is_fno = instrument_type in (InstrumentType.CE, InstrumentType.PE, InstrumentType.FUT)
    if is_fno:
        canonical_lot = get_canonical_lot_size(
            sym, name, exchange=exch_str, instrument_type=instrument_type.value
        )
        lot_size_final = canonical_lot or csv_lot or 1
    else:
        # Equity / index spot: 1 share = 1 lot. Don't echo Kite's
        # `marketlot` for ETFs (often 10 or 100) — that would inflate
        # quantity downstream.
        lot_size_final = 1

    friendly_name = display_name(
        instrument_type=instrument_type, underlying=name, expiry=expiry_d, strike=strike_val
    )

    # Self-heal path: update the existing stub in place rather than insert a
    # duplicate. Only overwrite when we actually resolved a REAL (non-numeric)
    # symbol so we never clobber a good row — or replace one stub with another.
    if existing is not None:
        if sym and sym != str(token_int):
            existing.symbol = sym
            existing.trading_symbol = catalog_row.get("tradingSymbol") or sym
            existing.name = friendly_name
            existing.exchange = exchange
            existing.segment = segment
            existing.instrument_type = instrument_type
            existing.option_type = option_type
            if expiry_d is not None:
                existing.expiry = expiry_d
            if strike_money is not None:
                existing.strike = strike_money
            existing.lot_size = lot_size_final
            existing.tick_size = tick_money
            existing.is_active = True
            existing.is_tradable = True
            try:
                await existing.save()
            except Exception:
                pass
        return existing

    inst = Instrument(
        token=str(token_int),
        symbol=sym,
        trading_symbol=catalog_row.get("tradingSymbol") or sym,
        name=friendly_name,
        exchange=exchange,
        segment=segment,
        instrument_type=instrument_type,
        isin=catalog_row.get("isin"),
        expiry=expiry_d,
        strike=strike_money,
        option_type=option_type,
        lot_size=lot_size_final,
        tick_size=tick_money,
        is_active=True,
        is_tradable=True,
    )
    try:
        await inst.insert()
    except Exception:
        # Race condition: another request mirrored it first. Re-fetch.
        existing = await Instrument.find_one(Instrument.token == str(token_int))
        if existing is not None:
            return existing
        raise

    # Subscribe to ticker so the chart / order panel see live ticks.
    try:
        await zerodha.subscribe_tokens_on_demand(
            [token_int],
            symbols={token_int: {"symbol": sym, "exchange": exch_str}},
        )
    except Exception:
        pass

    return inst


async def list_paginated(
    *,
    page: int = 1,
    page_size: int = 50,
    exchange: str | None = None,
    segment: str | None = None,
    netting_segment: str | None = None,
    q: str | None = None,
) -> tuple[list[Instrument], int]:
    query: dict[str, Any] = {}
    if exchange:
        query["exchange"] = exchange
    if segment:
        query["segment"] = segment
    elif netting_segment:
        # Resolve an ADMIN segment-row name (NSE_EQ / FOREX / MCX_FUT …) to the
        # set of instrument `segment` values that map to it, so the admin's
        # script-override picker can search any segment — including the
        # Infoway-fed ones (FOREX/STOCKS/INDICES/COMMODITIES) — uniformly.
        from app.services.netting_service import instrument_segments_for

        seg_values = instrument_segments_for(netting_segment)
        if seg_values:
            query["segment"] = {"$in": seg_values}
    if q:
        # Same index-backed anchored prefix match as `search()` — avoids the
        # full-collection scan the old un-anchored regex forced.
        prefix = {"$regex": f"^{re.escape(q.lower())}"}
        query["$or"] = [
            {"symbol_lc": prefix},
            {"trading_symbol_lc": prefix},
            {"name_lc": prefix},
            {"token": q},
        ]
    total = await Instrument.find(query).count()
    items = (
        await Instrument.find(query)
        .sort([("exchange", ASCENDING), ("symbol", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
        .to_list()
    )
    return items, total


async def backfill_search_keys() -> int:
    """Boot-time, idempotent migration: populate the lowercased prefix-search
    fields (``symbol_lc`` / ``trading_symbol_lc`` / ``name_lc``) on any
    Instrument row missing them.

    The ``Instrument._sync_search_keys`` before-save hook keeps these in sync
    for single-doc inserts/saves (the on-demand mirror path), but seed
    ``insert_many`` and any raw-motor write bypass document events — so those
    rows, plus everything created before these fields existed, need one pass.
    Scoped to rows where ``symbol_lc`` is absent/empty, so it's a cheap no-op
    on every boot after the first. Uses an aggregation-pipeline update so the
    lowercasing happens server-side in a single command (no cursor of 15k+
    docs pulled into the app)."""
    coll = Instrument.get_motor_collection()
    res = await coll.update_many(
        {"$or": [{"symbol_lc": {"$exists": False}}, {"symbol_lc": ""}]},
        [
            {
                "$set": {
                    "symbol_lc": {"$toLower": {"$ifNull": ["$symbol", ""]}},
                    "trading_symbol_lc": {"$toLower": {"$ifNull": ["$trading_symbol", ""]}},
                    "name_lc": {"$toLower": {"$ifNull": ["$name", ""]}},
                }
            }
        ],
    )
    n = int(getattr(res, "modified_count", 0) or 0)
    if n:
        logger.info("instrument_search_keys_backfilled", extra={"count": n})
    return n
