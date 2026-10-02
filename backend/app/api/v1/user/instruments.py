"""Instrument endpoints — search, detail, quote, depth."""

from __future__ import annotations

import logging
from datetime import date as _date
from typing import Any

from bson import Decimal128
from fastapi import APIRouter, Query

from app.core.dependencies import CurrentUser
from app.models._base import Exchange, InstrumentType, OptionType
from app.models.instrument import Instrument
from app.schemas.common import APIResponse
from app.schemas.trading import InstrumentOut, QuoteOut
from app.services import instrument_service, market_data_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/instruments", tags=["user-instruments"])

# History-endpoint cache. The chart datafeed calls `/history` on every
# token switch + every resolution change; before this cache, each call
# round-tripped to Zerodha (~200-800 ms on cold cache) even for a token
# the user had just been viewing 10 seconds earlier. 60 s TTL is short
# enough that the latest candle is never more than a minute stale (the
# live LTP is overlaid on the streaming bar by the datafeed anyway) and
# long enough to absorb the common "user clicks back" navigation pattern.
import time as _t_hist

_HISTORY_CACHE_TTL_MS = 60_000
_history_cache: dict[tuple[str, str, int], tuple[int, list[dict]]] = {}


def _serialize(i) -> dict:
    # Self-heal display names for derivatives — older rows were stored with
    # Zerodha's raw `name` (just the underlying), so build a friendly variant
    # on the fly when the stored name isn't already in the composed form.
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
        "token": i.token,
        "symbol": i.symbol,
        "trading_symbol": i.trading_symbol,
        "name": display,
        "exchange": i.exchange.value if hasattr(i.exchange, "value") else str(i.exchange),
        "segment": i.segment,
        "instrument_type": it_val,
        "lot_size": i.lot_size,
        "tick_size": str(i.tick_size),
        "expiry": str(i.expiry) if i.expiry else None,
        "strike": str(i.strike) if i.strike else None,
        "option_type": i.option_type.value if i.option_type and hasattr(i.option_type, "value") else None,
        "is_active": i.is_active,
        "is_tradable": i.is_tradable,
    }


# Maps UI segment values (NSE_FUTURE, MCX_OPTION_BUY, …) onto Zerodha cache
# rows. NSE futures live on Kite's `NFO` exchange — that's why filtering the
# admin /instruments page by exchange=NSE returns no futures, and why the
# user side panel's NSE FUT chip can't find anything without this mapping.
# Index underlyings on NFO — used to route NSE F&O rows to the STOCK vs INDEX
# admin settings row (NSE_STK_* / NSE_IDX_*). Same prefix table the instrument
# creator + migration use so all paths agree on stock-vs-index classification.
_NSE_INDEX_ROOTS = (
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "MIDCAPNIFTY", "SENSEX", "BANKEX",
)


def _segment_matches_kite_row(segment_value: str, row: dict) -> bool:
    ex = (row.get("exchange") or "").upper()
    it = (row.get("instrumentType") or "").upper()
    s = segment_value
    if s == "NSE_EQUITY":
        return ex == "NSE" and it in ("EQ", "")
    if s == "BSE_EQUITY":
        return ex == "BSE" and it in ("EQ", "")
    if s in ("NSE_FUTURE", "NSE_INDEX_FUTURE"):
        return ex == "NFO" and it == "FUT"
    if s in (
        "NSE_INDEX_OPTION_BUY",
        "NSE_INDEX_OPTION_SELL",
        "NSE_STOCK_OPTION_BUY",
        "NSE_STOCK_OPTION_SELL",
    ):
        return ex == "NFO" and it in ("CE", "PE")
    if s in ("BSE_FUTURE", "BSE_INDEX_FUTURE"):
        return ex == "BFO" and it == "FUT"
    if s in ("BSE_OPTION_BUY", "BSE_OPTION_SELL"):
        return ex == "BFO" and it in ("CE", "PE")
    if s == "MCX_FUTURE":
        return ex == "MCX" and it == "FUT"
    if s in ("MCX_OPTION_BUY", "MCX_OPTION_SELL"):
        return ex == "MCX" and it in ("CE", "PE")
    # COMMODITIES / STOCKS / INDICES / FOREX never have a Zerodha-cache
    # counterpart — those segments exist only on Infoway-mirrored rows in
    # the local Instrument collection. Returning False here is correct;
    # the search endpoint then falls through to MongoDB which finds the
    # Infoway-tagged rows. Without this, the COMMODITIES chip used to
    # leak every Indian MCX symbol into the user's Infoway view.
    return False


def _kite_row_to_payload(r: dict) -> dict:
    """Shape a Zerodha cache row into the public /instruments/search response
    payload, applying the friendly-name helper for derivatives so listings
    don't show the bare underlying for FUT/CE/PE."""
    from app.services.index_lots import get_canonical_lot_size

    it = (r.get("instrumentType") or "EQ").upper()
    sym = r.get("symbol") or ""
    underlying = r.get("name") or sym
    ex = (r.get("exchange") or "").upper()
    display = instrument_service.display_name(
        instrument_type=it,
        underlying=underlying,
        expiry=r.get("expiry"),
        strike=r.get("strike"),
    )

    if it in ("CE", "PE", "FUT"):
        canonical = get_canonical_lot_size(
            sym, underlying, exchange=ex, instrument_type=it
        )
        lot = canonical or int(r.get("lotSize") or 1)
    else:
        # Equity / indices / ETFs trade 1 share = 1 lot. Kite's CSV
        # occasionally reports `lotSize` > 1 for ETFs as a "marketlot"
        # convention, but our order pipeline treats lot as the F&O
        # multiplier, so for EQ we want it strictly 1.
        lot = 1

    return {
        "token": str(r.get("token") or ""),
        "symbol": sym,
        "trading_symbol": r.get("tradingSymbol") or sym,
        "name": display,
        "exchange": r.get("exchange") or "",
        "segment": r.get("segment") or "",
        "instrument_type": it,
        "lot_size": lot,
        "tick_size": str(r.get("tickSize") or "0.05"),
        "expiry": r.get("expiry"),
        "strike": r.get("strike"),
        "option_type": it if it in ("CE", "PE") else None,
        "is_active": True,
        "is_tradable": True,
    }


def _row_expiry_date(exp_raw) -> _date | None:
    """Parse whatever the row carries (ISO string, date, datetime) into a date."""
    from datetime import datetime as _dt

    if not exp_raw:
        return None
    if isinstance(exp_raw, _dt):  # datetime subclasses date — narrow it first
        return exp_raw.date()
    if isinstance(exp_raw, _date):
        return exp_raw
    try:
        return _date.fromisoformat(str(exp_raw)[:10])
    except Exception:  # noqa: BLE001
        return None


def _drop_expired_rows(rows: list, *, get_exp) -> list:
    """Drop already-expired contracts from a result set.

    The Kite instrument catalog is cached for up to 4 DAYS (see
    `_INSTRUMENTS_TTL_SEC`), so it keeps serving contracts that expired days
    ago — that's how an expired July MCX crude kept turning up in search and
    could be re-added to a watchlist right after the sweep removed it. IST
    date boundary: a contract stays searchable through its own expiry day.
    """
    from app.utils.time_utils import now_ist

    today = now_ist().date()
    out = []
    for r in rows:
        exp = _row_expiry_date(get_exp(r))
        if exp is not None and exp < today:
            continue
        out.append(r)
    return out


def _cap_futures_by_expiry(rows: list, *, get_it, get_root, get_exp, get_ex, cap_for) -> list:
    """Trim FUTURES so only the nearest N expiries per underlying survive,
    where N = cap_for(root, exchange) — per-underlying "Show expiry month",
    else the per-exchange (NSE/BSE/MCX) fallback. Mirrors the option-chain
    picker cap on the instrument / futures search panel. Non-FUT rows pass
    through untouched and order is preserved. No-op when there are no futures.
    """
    from collections import defaultdict

    from app.utils.time_utils import now_ist

    today_s = str(now_ist().date())
    exps_by_root: dict[str, set] = defaultdict(set)
    ex_by_root: dict[str, str] = {}
    for r in rows:
        if (get_it(r) or "").upper() == "FUT":
            exp = get_exp(r)
            # PAST expiries must not consume a slot of the nearest-N window —
            # with a cap of 2 an expired near month used to hide the real
            # far month while itself still showing.
            if exp and str(exp)[:10] >= today_s:
                root = (get_root(r) or "").upper()
                exps_by_root[root].add(str(exp)[:10])
                ex_by_root.setdefault(root, get_ex(r) or "")
    if not exps_by_root:
        return rows
    allowed: dict[str, set] = {}
    for root, exps in exps_by_root.items():
        n = max(1, int(cap_for(root, ex_by_root.get(root, ""))))
        allowed[root] = set(sorted(exps)[:n])  # nearest N distinct expiries
    out = []
    for r in rows:
        if (get_it(r) or "").upper() == "FUT":
            exp = get_exp(r)
            exp_s = str(exp)[:10] if exp else None
            root = (get_root(r) or "").upper()
            if exp_s is not None and root in allowed and exp_s not in allowed[root]:
                continue  # FUT beyond the per-underlying / per-exchange cap
        out.append(r)
    return out


@router.get("/search", response_model=APIResponse[list])
async def search(
    user: CurrentUser,
    q: str | None = None,
    exchange: str | None = None,
    segment: str | None = None,
    instrument_type: str | None = None,
    # Cap raised 100 → 200: the mobile APK requests limit=120 for the
    # Forex/Crypto/Commodities browse feed; with le=100 every such request
    # 422'd, so Infoway instruments silently vanished from the app while
    # Indian (Zerodha-cache) chips kept working. 200 covers the APK with
    # headroom and still bounds the result set.
    limit: int = Query(default=30, le=200),
):
    """Fast instrument search — tries in-memory Zerodha cache first (instant),
    falls back to MongoDB if Zerodha is not connected.

    `segment` and `instrument_type` accept comma-separated lists so the side
    panel's compound buckets (e.g. "NSE OPT" = NSE_INDEX_OPTION_BUY +
    NSE_INDEX_OPTION_SELL + NSE_STOCK_OPTION_BUY + NSE_STOCK_OPTION_SELL)
    can be queried in a single round-trip.
    """
    seg_list = [s.strip() for s in (segment or "").split(",") if s.strip()]
    it_list = [t.strip().upper() for t in (instrument_type or "").split(",") if t.strip()]
    seg_arg: str | list[str] | None = seg_list[0] if len(seg_list) == 1 else (seg_list or None)
    it_arg: str | list[str] | None = it_list[0] if len(it_list) == 1 else (it_list or None)

    from app.services.zerodha_service import zerodha as _zerodha
    from app.services.netting_service import (
        _SEGMENT_NAME_MAP,
        get_user_blocked_symbols,
        inactive_admin_rows,
        inactive_instrument_segments,
        is_symbol_blocked_for,
        resolve_admin_row,
    )

    # Admin-side "Block → isActive = No" → segment is hidden from user
    # search entirely. Resolved once per request; the netting service
    # caches the set for 30 s per user so this is cheap. `user.id` is
    # passed so a sub-admin's pool-tier block reaches their members'
    # search (super-admin / global only would miss sub-admin overrides).
    inactive_admin = await inactive_admin_rows(user_id=user.id)
    inactive_segs = await inactive_instrument_segments(user_id=user.id)
    # Per-symbol blocks (script-level + user-specific). Hides exact
    # symbols (e.g. SBIN) plus pattern hits (NIFTYFUT, NIFTYCE) so
    # the user's search never returns instruments their admin has
    # disabled for them. User-flagged: "agar koi script block hai
    # to user ke search me dikhe hi mat".
    blocked = await get_user_blocked_symbols(user.id)

    # Per-underlying expiry cap — same setting as the option-chain picker,
    # resolved through USER → BROKER → ADMIN → GLOBAL. The futures search
    # panel shows only the nearest N expiries per underlying (N = the
    # underlying's "Show expiry month", else the fallback).
    from app.api.v1.user.option_chain import (
        _effective_max_expiries,
        _resolve_expiry_settings_for_user,
    )

    try:
        _exp_settings = await _resolve_expiry_settings_for_user(user.id)
    except Exception:
        # Never let the expiry cap break search — fall back to "no cap".
        _exp_settings = {"underlyings": [], "max_expiries": 999, "max_expiries_by_exchange": {}}

    def _cap_for(root: str, exchange: str) -> int:
        return _effective_max_expiries(_exp_settings, root, exchange)

    def _cap_kite(rows: list) -> list:
        return _cap_futures_by_expiry(
            rows,
            get_it=lambda r: r.get("instrumentType"),
            get_root=lambda r: r.get("name"),
            get_exp=lambda r: r.get("expiry"),
            get_ex=lambda r: r.get("exchange"),
            cap_for=_cap_for,
        )

    def _kite_row_admin_row(row: dict) -> str | None:
        ex = (row.get("exchange") or "").upper()
        it = (row.get("instrumentType") or "").upper()
        if ex == "NSE":
            return "NSE_EQ"
        if ex == "BSE":
            return "BSE_EQ"
        if ex == "NFO":
            # NSE F&O is split stock-vs-index — resolve the granular admin row
            # by the underlying root so a blocked "Stock Option" / "Index
            # Future" actually hides from search. `name` is the underlying
            # (e.g. "SBIN" / "NIFTY"); fall back to the tradingsymbol/symbol.
            root = (
                row.get("name") or row.get("tradingsymbol") or row.get("symbol") or ""
            ).upper()
            is_index = root.startswith(_NSE_INDEX_ROOTS)
            if it == "FUT":
                return "NSE_IDX_FUT" if is_index else "NSE_STK_FUT"
            if it in ("CE", "PE"):
                return "NSE_IDX_OPT" if is_index else "NSE_STK_OPT"
            return None
        if ex == "BFO":
            return "BSE_FUT" if it == "FUT" else ("BSE_OPT" if it in ("CE", "PE") else None)
        if ex == "MCX":
            return "MCX_FUT" if it == "FUT" else ("MCX_OPT" if it in ("CE", "PE") else None)
        return None

    def _kite_row_active(row: dict) -> bool:
        admin_row = _kite_row_admin_row(row)
        return admin_row is None or admin_row not in inactive_admin

    def _mongo_inst_active(inst) -> bool:
        seg_val = inst.segment.value if hasattr(inst.segment, "value") else str(inst.segment)
        # Symbol-aware ONLY — do NOT use the raw `seg_val in inactive_segs`
        # check: `inactive_instrument_segments` reverse-maps the whole
        # NFO_OPTION segment to the stock row, so a raw check hid index
        # options (BANKNIFTY/NIFTY) the instant Stock Options were blocked.
        # Resolving segment + symbol → the real admin row keeps them separate.
        admin_row = resolve_admin_row(seg_val, getattr(inst, "symbol", None))
        return admin_row not in inactive_admin

    # Fast path: scan the Zerodha in-memory cache. Two modes:
    #   1) No segment/type filter → defer to search_instruments_fast which
    #      handles scoring (exact > prefix > contains).
    #   2) With segment/type filter → scan cache ourselves, applying the
    #      UI's segment values via _segment_matches_kite_row so the side
    #      panel's NSE FUT / MCX FUT chips return data without needing
    #      pre-mirrored rows in MongoDB.
    if q and q.strip() and not seg_list and not it_list:
        try:
            fast_results = await _zerodha.search_instruments_fast(q, exchange=exchange, limit=limit)
            fast_results = [r for r in (fast_results or []) if _kite_row_active(r)]
            # Drop rows whose symbol is blocked by an admin / broker /
            # user-level override for this caller.
            fast_results = [
                r for r in fast_results
                if not is_symbol_blocked_for(r.get("symbol") or "", blocked)
            ]
            # Expired contracts never surface — the Kite catalog cache is up
            # to 4 days stale, so it still lists last week's dead F&O rows.
            fast_results = _drop_expired_rows(
                fast_results, get_exp=lambda r: r.get("expiry")
            )
            fast_results = _cap_kite(fast_results)
            if fast_results:
                return APIResponse(data=[_kite_row_to_payload(r) for r in fast_results])
        except Exception:
            pass  # fall through to MongoDB

    if seg_list or it_list:
        try:
            # Ensure cache is warm before scanning.
            if not _zerodha._instruments_cache:
                for ex in ("NSE", "NFO", "MCX", "BFO", "BSE"):
                    try:
                        await _zerodha.fetch_instruments(ex)
                    except Exception:
                        pass

            from app.utils.time_utils import now_ist as _now_ist

            _today_ist = _now_ist().date()
            q_upper = (q or "").strip().upper()
            collected: list[dict] = []
            for ex_key, cache in _zerodha._instruments_cache.items():
                if exchange and ex_key.upper() != exchange.upper():
                    continue
                for inst in cache:
                    if exchange and (inst.get("exchange") or "").upper() != exchange.upper():
                        continue
                    if seg_list and not any(_segment_matches_kite_row(s, inst) for s in seg_list):
                        continue
                    if it_list:
                        kite_it = (inst.get("instrumentType") or "").upper()
                        if kite_it not in it_list:
                            continue
                    # Hide instruments whose admin row is currently isActive=false.
                    if not _kite_row_active(inst):
                        continue
                    # Per-symbol block check — drops script-level and
                    # user-specific blocked rows before they reach the
                    # search results.
                    if is_symbol_blocked_for(inst.get("symbol") or "", blocked):
                        continue
                    if q_upper:
                        sym = (inst.get("symbol") or "").upper()
                        name = (inst.get("name") or "").upper()
                        if q_upper not in sym and q_upper not in name:
                            continue
                    # Drop expired contracts so the browse chips don't show
                    # stale options/futures dated last month. IST boundary —
                    # a UTC "today" cut MCX contracts a day early after 18:30.
                    exp_d = _row_expiry_date(inst.get("expiry"))
                    if exp_d is not None and exp_d < _today_ist:
                        continue
                    collected.append(inst)
                    if len(collected) >= limit:
                        break
                if len(collected) >= limit:
                    break
            collected = _cap_kite(collected)
            if collected:
                return APIResponse(data=[_kite_row_to_payload(r) for r in collected])
        except Exception:
            logger.exception("instruments_fast_path_with_filter_failed")

    # Slow path: MongoDB
    results = await instrument_service.search(
        q,
        exchange=exchange,
        segment=seg_arg,
        instrument_type=it_arg,
        limit=limit,
    )
    # Final filter: drop instruments whose admin row is currently disabled.
    # Done post-fetch (after `limit`) so the filter is cheap; if it ever
    # noticeably trims a 100-row page we can push it into the Mongo query.
    results = [i for i in results if _mongo_inst_active(i)]
    # And drop per-symbol blocks (script + user overrides) the same way.
    results = [
        i for i in results
        if not is_symbol_blocked_for(getattr(i, "symbol", "") or "", blocked)
    ]
    # Expired rows never surface, even if the sweep hasn't flipped
    # `is_active` yet (or the doc's expiry was backfilled late).
    results = _drop_expired_rows(results, get_exp=lambda i: i.expiry)
    results = _cap_futures_by_expiry(
        results,
        get_it=lambda i: (i.instrument_type.value if hasattr(i.instrument_type, "value") else str(i.instrument_type)),
        get_root=lambda i: i.name,
        get_exp=lambda i: i.expiry,
        get_ex=lambda i: (i.exchange.value if hasattr(i.exchange, "value") else str(i.exchange)),
        cap_for=_cap_for,
    )
    payload = [_serialize(i) for i in results]

    # Nothing (or little) in the catalogue? Offer what the CFD broker carries.
    external: list[dict] = []
    if q and len(payload) < limit:
        try:
            external = await _metaapi_search_external(
                q,
                seg_list,
                limit - len(payload),
                have={(p.get("symbol") or "").upper() for p in payload},
                blocked=blocked,
                inactive_segs=inactive_segs,
            )
        except Exception:
            logger.exception("metaapi_external_search_failed")
    return APIResponse(data=payload + external)


# Segments whose instruments come from the MT/CFD broker rather than Zerodha.
_GLOBAL_SEGMENTS = {"FOREX", "STOCKS", "INDICES", "COMMODITIES"}


async def _metaapi_search_external(
    q: str,
    seg_list: list[str],
    limit: int,
    *,
    have: set[str],
    blocked,
    inactive_segs,
) -> list[dict]:
    """Search the broker's own symbol universe for symbols we have no row for.

    The MT account offers thousands of CFD symbols and a row only exists once
    someone has picked one, so searching "AAPL" or "GER40" used to come back
    empty. These payloads are addable: picking one creates the catalogue row
    (see `instrument_service.get_by_token`) and the feed subscribes it once it
    lands in a watchlist.
    """
    if limit <= 0 or not q.strip():
        return []
    if seg_list and not (set(seg_list) & _GLOBAL_SEGMENTS):
        return []

    from app.services.infoway_lots import get_infoway_lot_size
    from app.services.infoway_service import _classify_infoway_code
    from app.services.metaapi_service import metaapi
    from app.services.netting_service import is_symbol_blocked_for

    names = await metaapi.search_symbols(q, limit=limit * 4)
    out: list[dict] = []
    for sym in names:
        if sym in have:
            continue
        meta = _classify_infoway_code(sym)
        seg = meta["segment"]
        if seg not in _GLOBAL_SEGMENTS:
            continue  # crypto keeps its own seeded rows
        if seg_list and seg not in seg_list:
            continue
        if seg in inactive_segs or is_symbol_blocked_for(sym, blocked):
            continue
        out.append(
            {
                "token": sym,
                "symbol": sym,
                "trading_symbol": sym,
                "name": meta["name"],
                "exchange": meta["exchange"],
                "segment": seg,
                "instrument_type": meta["instrument_type"],
                "lot_size": get_infoway_lot_size(sym, seg),
                "tick_size": "0.0001",
                "expiry": None,
                "strike": None,
                "option_type": None,
                "is_active": True,
                "is_tradable": True,
            }
        )
        if len(out) >= limit:
            break
    return out


async def _find_or_create_from_zerodha(token: str) -> Instrument | None:
    """Look up an instrument by Zerodha token in MongoDB. If missing, try the
    Zerodha in-memory cache, auto-create in MongoDB, and return it.
    This ensures option chain instruments (which live in Zerodha cache) are
    always tradable without a manual backfill step.

    Also self-heals `lot_size` on every read: an Instrument row may have
    been auto-created earlier with a wrong lot (e.g. 1, when the Zerodha
    CSV cache hadn't populated lotSize yet for a fresh contract). For
    index F&O the exchange-canonical lot wins, so the next order placed
    will use the right multiplier without waiting on the startup backfill.
    """
    inst: Instrument | None = None
    try:
        inst = await instrument_service.get_by_token(token)
    except Exception:
        inst = None

    if inst is None:
        # Fall back to Zerodha in-memory instrument cache
        from app.services.zerodha_service import zerodha as _zerodha

        for ex in ("NFO", "NSE", "MCX", "BFO", "BSE"):
            cache = _zerodha._instruments_cache.get(ex, [])
            for z in cache:
                if str(z.get("token")) == str(token):
                    inst = await _auto_create_instrument(z, ex)
                    break
            if inst is not None:
                break

    if inst is not None:
        from app.models._base import InstrumentType
        from app.services.index_lots import get_canonical_lot_size

        if inst.instrument_type in (InstrumentType.CE, InstrumentType.PE, InstrumentType.FUT):
            ex_val = inst.exchange.value if hasattr(inst.exchange, "value") else str(inst.exchange)
            canonical_lot = get_canonical_lot_size(inst.symbol, inst.name, exchange=ex_val)
            if canonical_lot and int(inst.lot_size or 0) != canonical_lot:
                inst.lot_size = canonical_lot
                try:
                    await inst.save()
                except Exception:
                    pass

            # Stock F&O fallback — the `get_canonical_lot_size` table only
            # covers INDEX futures (NIFTY / BANKNIFTY / FINNIFTY / …). Stock
            # contracts (BOSCHLTD, HDFCBANK, RELIANCE, …) live entirely in
            # the Zerodha CSV cache, with quarterly SEBI-driven lot
            # revisions that the cache reflects. Without this lookup a
            # contract first viewed BEFORE the CSV cache was warmed got
            # stuck at fallback `lot_size = 1`, which then sent the trade
            # panel's margin to ~₹74 instead of ~₹1,858 for a BOSCHLTD25
            # lot — the user-reported "ek hi underlying ke alag expiry me
            # lot_size alag a rahe hain" bug. Trust whatever the CSV says
            # (including legitimate revisions across expiries).
            if int(inst.lot_size or 0) <= 1:
                try:
                    token_int_csv = int(token)
                except (TypeError, ValueError):
                    token_int_csv = None
                if token_int_csv is not None:
                    from app.services.zerodha_service import zerodha as _zerodha_csv

                    csv_lot = 0
                    for _ex_key, _rows in _zerodha_csv._instruments_cache.items():
                        for _r in _rows:
                            try:
                                if int(_r.get("token") or 0) == token_int_csv:
                                    csv_lot = int(_r.get("lotSize") or 0)
                                    break
                            except Exception:
                                continue
                        if csv_lot > 0:
                            break
                    if csv_lot > 1:
                        inst.lot_size = csv_lot
                        try:
                            await inst.save()
                        except Exception:
                            pass

        # On-demand Zerodha WS subscribe for derivatives. Without this an
        # instrument that exists in MongoDB (auto-mirrored from a CSV sync
        # or admin bulk subscribe) but isn't actively on a WS connection
        # returns no LTP — the user sees "—" on the market list and the
        # trade panel, even though the contract IS tradable. Fire-and-
        # forget so we don't block the detail/effective-settings response;
        # the panel's next poll (1-2 s) picks up live ticks. Skips
        # Infoway-mirrored tokens (non-numeric) — those carry their own
        # feed lifecycle.
        if inst.instrument_type in (InstrumentType.CE, InstrumentType.PE, InstrumentType.FUT):
            try:
                token_int = int(token)
            except (TypeError, ValueError):
                token_int = None
            if token_int is not None:
                import asyncio as _asyncio

                from app.services.zerodha_service import zerodha as _zerodha

                ex_val_sub = (
                    inst.exchange.value if hasattr(inst.exchange, "value") else str(inst.exchange)
                )
                try:
                    _asyncio.create_task(
                        _zerodha.subscribe_tokens_on_demand(
                            [token_int],
                            symbols={token_int: {"symbol": inst.symbol, "exchange": ex_val_sub}},
                        )
                    )
                except Exception:
                    logger.debug("zerodha_on_demand_subscribe_failed", extra={"token": token})
    return inst


async def _auto_create_instrument(z: dict[str, Any], exchange_hint: str) -> Instrument:
    """Create an Instrument document from a Zerodha cache dict."""
    tok = str(z.get("token") or 0)
    sym = z.get("symbol") or ""
    name = z.get("name") or sym
    exch_str = (z.get("exchange") or exchange_hint).upper()
    it_str = (z.get("instrumentType") or "EQ").upper()

    # Symbol-suffix safety net. Zerodha's CSV cache sometimes returns an empty
    # `instrumentType` for fresh F&O contracts; without this, the symbol
    # `NIFTY2651223250CE` would be persisted as `EQ` and become invisible to
    # every option filter / option-chain view downstream.
    sym_up = sym.upper()
    if sym_up.endswith("CE") and it_str not in ("CE", "PE", "FUT"):
        it_str = "CE"
    elif sym_up.endswith("PE") and it_str not in ("CE", "PE", "FUT"):
        it_str = "PE"
    elif sym_up.endswith("FUT") and it_str not in ("CE", "PE", "FUT"):
        it_str = "FUT"

    # Map exchange string → enum
    exch = getattr(Exchange, exch_str, None) or Exchange.NSE

    # Map instrument type
    it_map = {"CE": InstrumentType.CE, "PE": InstrumentType.PE, "FUT": InstrumentType.FUT,
              "EQ": InstrumentType.EQ, "INDEX": InstrumentType.INDEX}
    instr_type = it_map.get(it_str, InstrumentType.EQ)

    # Underlying detection so an NFO row routes to INDEX_OPTION_* vs
    # STOCK_OPTION_* correctly. Anything whose symbol starts with one of
    # the canonical index names is an index contract.
    _idx_prefixes = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX")
    is_index_underlying = sym_up.startswith(_idx_prefixes)

    # Derive segment
    seg_map: dict[str, dict[str, str]] = {
        "NFO": {
            "CE": "NSE_INDEX_OPTION_BUY" if is_index_underlying else "NSE_STOCK_OPTION_BUY",
            "PE": "NSE_INDEX_OPTION_SELL" if is_index_underlying else "NSE_STOCK_OPTION_SELL",
            "FUT": "NSE_INDEX_FUTURE" if is_index_underlying else "NSE_FUTURE",
            "EQ": "NSE_EQUITY",
        },
        "NSE": {"EQ": "NSE_EQUITY", "INDEX": "NSE_EQUITY"},
        "BSE": {"EQ": "BSE_EQUITY"},
        "BFO": {"CE": "BSE_OPTION_BUY", "PE": "BSE_OPTION_SELL", "FUT": "BSE_FUTURE"},
        "MCX": {"CE": "MCX_OPTION_BUY", "PE": "MCX_OPTION_SELL", "FUT": "MCX_FUTURE"},
    }
    segment = seg_map.get(exch_str, {}).get(it_str, f"{exch_str}_{it_str}")

    # Expiry
    expiry = None
    if z.get("expiry"):
        try:
            expiry = _date.fromisoformat(str(z["expiry"])[:10])
        except Exception:
            pass

    opt_type = None
    if it_str in ("CE", "PE"):
        opt_type = OptionType.CE if it_str == "CE" else OptionType.PE

    strike = None
    if z.get("strike") is not None:
        try:
            strike = Decimal128(str(z["strike"]))
        except Exception:
            pass

    # Upsert to avoid duplicate key on concurrent requests
    existing = await Instrument.find_one(Instrument.token == tok)
    if existing:
        return existing

    # Lot size: trust the canonical table over the Zerodha CSV.
    # Index F&O — CSV cache may return 0 / stale for fresh contracts.
    # MCX — CSV returns raw units (kg/g/mmBtu/barrels) which doesn't match
    # `quantity = lots × lot_size` semantics used throughout the platform.
    from app.services.index_lots import get_canonical_lot_size

    canonical_lot = (
        get_canonical_lot_size(sym, name, exchange=exch_str)
        if instr_type in (InstrumentType.CE, InstrumentType.PE, InstrumentType.FUT)
        else None
    )
    csv_lot = int(z.get("lotSize") or 0)
    lot_size_final = canonical_lot or csv_lot or 1

    friendly_name = instrument_service.display_name(
        instrument_type=instr_type, underlying=name, expiry=expiry, strike=z.get("strike")
    )
    doc = Instrument(
        token=tok,
        symbol=sym,
        trading_symbol=sym,
        name=friendly_name,
        exchange=exch,
        segment=segment,
        instrument_type=instr_type,
        lot_size=lot_size_final,
        tick_size=Decimal128(str(z.get("tickSize") or 0.05)),
        expiry=expiry,
        strike=strike,
        option_type=opt_type,
        is_active=True,
        is_tradable=True,
    )
    try:
        await doc.insert()
        logger.info("auto_created_instrument_from_zerodha", extra={"token": tok, "symbol": sym})
    except Exception:
        # Duplicate key race — fetch the one that won
        existing = await Instrument.find_one(Instrument.token == tok)
        if existing:
            return existing
    return doc


@router.get("/{token}", response_model=APIResponse[InstrumentOut])
async def get_instrument(token: str, user: CurrentUser):
    i = await _find_or_create_from_zerodha(token)
    if i is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Instrument {token} not found")

    # Segment-block gate: even direct-link / favourite-card access must
    # be denied when the admin has paused the segment. User explicitly
    # asked: "admin block kare to chart bhi open na ho, favourite me
    # bhi remove ho jaye." Returning 403 here gives the terminal/chart
    # a clear signal to redirect back to home; the watchlist
    # endpoint filters items with the same gate so the entry disappears
    # from the favourites list in the same poll window.
    from fastapi import HTTPException

    from app.services.netting_service import inactive_admin_rows, resolve_admin_row

    # SYMBOL-AWARE block gate. The raw segment (e.g. legacy "NFO_OPTION")
    # can't tell stock from index, so a raw-segment block check paused ALL
    # NFO_OPTION instruments — including index options (BANKNIFTY / NIFTY) —
    # the moment Stock Options were blocked, and the terminal/chart couldn't
    # load them (showed the bare token / "—"). Resolve the instrument's actual
    # admin row from segment + symbol and only 403 when THAT specific row is
    # paused, keeping Stock vs Index options independent.
    _seg = i.segment.value if hasattr(i.segment, "value") else str(i.segment)
    _admin_row = resolve_admin_row(_seg, getattr(i, "symbol", None))
    inactive_admin = await inactive_admin_rows(user_id=user.id)
    if inactive_admin and _admin_row in inactive_admin:
        raise HTTPException(
            status_code=403,
            detail="Segment is paused by admin — instrument unavailable",
        )

    return APIResponse(data=_serialize(i))


@router.get("/{token}/quote", response_model=APIResponse[QuoteOut])
async def get_quote(token: str, user: CurrentUser):
    q = await market_data_service.get_quote(token)
    return APIResponse(data=q)


@router.get("/quotes/batch", response_model=APIResponse[list[QuoteOut]])
async def quotes_batch(user: CurrentUser, tokens: str = Query(description="comma-separated tokens")):
    tlist = [t.strip() for t in tokens.split(",") if t.strip()]
    return APIResponse(data=await market_data_service.get_quotes(tlist))


_CRYPTO_BASES = {
    "BTC", "ETH", "BNB", "XRP", "ADA", "SOL", "DOGE", "DOT", "AVAX", "MATIC",
    "LINK", "LTC", "TRX", "SHIB", "PEPE", "APT", "ARB", "NEAR", "ATOM", "BCH",
    "UNI", "XLM", "ETC", "FIL", "ICP", "VET", "ALGO", "AAVE",
}

# Zerodha-style interval strings → Binance kline intervals. Binance accepts
# 1m / 5m / 15m / 30m / 1h / 4h / 1d / 1w. Defaults to 5m for unknowns.
_BINANCE_INTERVAL_MAP = {
    "minute": "1m",
    "3minute": "3m",
    "5minute": "5m",
    "15minute": "15m",
    "30minute": "30m",
    "60minute": "1h",
    "hour": "1h",
    "day": "1d",
}


def _binance_symbol_for(token: str) -> str | None:
    """Map our internal crypto token to a Binance trading pair.

    Examples:
      "BTCUSD"  → "BTCUSDT"   (we suffix-swap USD → USDT; spot pair on Binance)
      "BTCUSDT" → "BTCUSDT"   (pass through)
      "ETHUSD"  → "ETHUSDT"
      "CRYPTO_BTCUSD" → "BTCUSDT"  (strip legacy prefix)
      "NIFTY"   → None        (not crypto — caller falls back to Zerodha)
    """
    if not token:
        return None
    t = token.upper().strip()
    # Strip legacy/explicit prefixes.
    for pref in ("CRYPTO_", "BINANCE_", "BINANCE:"):
        if t.startswith(pref):
            t = t[len(pref):]
            break
    # Reject anything that obviously isn't a crypto pair.
    if not t.isalnum() or len(t) < 5 or len(t) > 12:
        return None
    # Detect base → if it isn't a known crypto, bail.
    base = None
    for b in _CRYPTO_BASES:
        if t.startswith(b):
            base = b
            break
    if base is None:
        return None
    quote = t[len(base):]
    if quote == "USD":
        quote = "USDT"
    elif quote not in ("USDT", "BUSD", "USDC", "FDUSD", "TUSD"):
        return None
    return base + quote


# Forex (and selected commodity) tokens come in as "FX_EURUSD" / "FX_USDJPY"
# / "FX_USDINR" etc. — Zerodha doesn't carry these and Binance is crypto-only,
# so the chart used to render empty. Yahoo Finance's public chart endpoint
# returns OHLC for ANY supported pair without auth, so it's our third
# fallback source. Mapping turns the internal token into Yahoo's symbol
# convention: forex pairs use "{PAIR}=X" (EURUSD=X), spot metals use the
# COMEX/futures notation, indices use "^NSEI" / "^NSEBANK".
_YAHOO_FX_PAIRS = {
    "EURUSD", "GBPUSD", "USDJPY", "USDINR", "AUDUSD", "NZDUSD", "USDCAD",
    "USDCHF", "EURGBP", "EURJPY", "GBPJPY", "EURINR", "GBPINR", "JPYINR",
    "AUDJPY", "EURAUD", "EURCHF", "USDCNH",
}


# International instruments Yahoo carries under a name of its own. Energy and
# index CFDs quote a contract/index whose absolute level differs from ours by a
# small offset; `_align_candles_to_live` shifts it onto our live scale, so only
# the SHAPE of the history has to match.
#
# These used to render TradingView's free embed widget instead of our own
# chart. That widget is a cross-origin iframe: admin chart lines can't be drawn
# on it and the price it shows isn't the price on our BUY/SELL buttons. Now
# that every one of them has history here, the licensed chart serves them all.
_YAHOO_DIRECT: dict[str, str] = {
    # Spot metals → the COMEX/NYMEX front-month future. Yahoo's "XAUUSD=X"
    # form exists but serves NO intraday candles (measured: 0 bars, while
    # EURUSD=X gives 1295) — that, not the tick, is why metals had no chart
    # of their own.
    "XAUUSD": "GC=F", "XAGUSD": "SI=F", "XPTUSD": "PL=F", "XPDUSD": "PA=F",
    # Energy CFDs → front-month future.
    "USOIL": "CL=F", "WTI": "CL=F", "UKOIL": "BZ=F", "BRENT": "BZ=F",
    "NATGAS": "NG=F",
    # Index CFDs → the underlying index.
    "US30": "^DJI", "NAS100": "^NDX", "SPX500": "^GSPC", "UK100": "^FTSE",
    "DE40": "^GDAXI", "JPN225": "^N225", "HK50": "^HSI",
    # US stocks we list — Yahoo uses the plain ticker. Listed explicitly
    # rather than "any short alpha token" so a stray symbol-style Indian
    # token can never be charted off the wrong market. Add new listings here.
    "AAPL": "AAPL", "AMZN": "AMZN", "GOOGL": "GOOGL", "META": "META",
    "MSFT": "MSFT", "NFLX": "NFLX", "NVDA": "NVDA", "TSLA": "TSLA",
}


def _yahoo_symbol_for(token: str) -> str | None:
    """Map our internal forex / spot-commodity token to a Yahoo Finance
    chart symbol. Returns None for tokens that should not hit Yahoo
    (Indian instruments handled by Zerodha, crypto by Binance).

    Examples:
      "FX_EURUSD"  → "EURUSD=X"
      "FX_USDINR"  → "USDINR=X"
      "EURUSD"     → "EURUSD=X"   (bare forex pair)
      "XAUUSD"     → "GC=F"       (spot gold → COMEX front month)
      "NIFTY"      → None         (handled by Zerodha)
    """
    if not token:
        return None
    t = token.upper().strip()
    for pref in ("FX_", "FOREX_", "OANDA:", "OANDA_"):
        if t.startswith(pref):
            t = t[len(pref):]
            break
    # Named instruments first: XAUUSD is six alpha characters but it is NOT an
    # FX pair on Yahoo — "XAUUSD=X" resolves yet serves no intraday candles.
    direct = _YAHOO_DIRECT.get(t)
    if direct is not None:
        return direct
    # Yahoo forex symbols are 6 alpha chars + "=X".
    if len(t) == 6 and t.isalpha():
        if t in _YAHOO_FX_PAIRS or t.endswith("USD") or t.startswith("USD") or t.endswith("INR"):
            return f"{t}=X"
    return None


# Yahoo's chart endpoint accepts interval=1m/2m/5m/15m/30m/60m/90m/1h/1d/...
# and a matching range token (1d/5d/1mo/3mo/6mo/1y/2y/5y/10y/ytd/max).
# 1m data is capped at 7 days lookback by Yahoo; we honor that below.
_YAHOO_INTERVAL_MAP = {
    "minute": "1m",
    "3minute": "3m",  # not officially supported, falls through to 5m
    "5minute": "5m",
    "15minute": "15m",
    "30minute": "30m",
    "60minute": "60m",
    "hour": "60m",
    "day": "1d",
}


def _yahoo_range_for(interval_ya: str, days: int) -> str:
    """Pick the smallest Yahoo `range` token that covers the requested
    lookback at the given interval. Yahoo caps intraday history strictly
    (1m → 7d, 5m → 60d, 15m/30m/60m → 730d, 1d → years), so we always
    clamp to what the endpoint will actually serve."""
    if interval_ya == "1m":
        return "7d" if days >= 5 else f"{max(1, days)}d"
    if interval_ya in {"5m", "15m"}:
        if days <= 5:
            return "5d"
        if days <= 30:
            return "1mo"
        return "3mo"
    if interval_ya in {"30m", "60m"}:
        if days <= 30:
            return "1mo"
        if days <= 90:
            return "3mo"
        return "6mo"
    # daily
    if days <= 30:
        return "1mo"
    if days <= 90:
        return "3mo"
    if days <= 180:
        return "6mo"
    if days <= 365:
        return "1y"
    return "2y"


async def _fetch_yahoo_chart(
    symbol: str, interval: str, days: int
) -> list[dict]:
    """Pull OHLC from Yahoo Finance's free chart endpoint. No auth, but
    Yahoo throttles aggressively so the same 60-s in-process cache as
    Binance applies (handled by the caller). Returns Zerodha-shaped
    dicts ({date, open, high, low, close, volume}) so the chart frontend
    needs no per-source handling."""
    import httpx
    from datetime import datetime, timezone

    ya_interval = _YAHOO_INTERVAL_MAP.get(interval, "5m")
    ya_range = _yahoo_range_for(ya_interval, days)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": ya_interval, "range": ya_range, "includePrePost": "false"}
    headers = {
        # Yahoo blocks the default httpx UA — any real-looking UA passes.
        "User-Agent": "Mozilla/5.0 (compatible; MarginWealthBot/1.0)",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0, headers=headers) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            body = r.json()
    except Exception:
        return []

    try:
        result = body["chart"]["result"][0]
        timestamps: list[int] = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []
    except (KeyError, IndexError, TypeError):
        return []

    out: list[dict] = []
    for i, ts in enumerate(timestamps):
        try:
            o = opens[i]; h = highs[i]; l = lows[i]; c = closes[i]
            if o is None or h is None or l is None or c is None:
                continue
            iso = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
            out.append({
                "date": iso,
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(volumes[i]) if i < len(volumes) and volumes[i] is not None else 0.0,
            })
        except (ValueError, IndexError, TypeError):
            continue
    return out


async def _fetch_binance_klines(
    symbol: str, interval: str, days: int
) -> list[dict]:
    """Pull OHLC from Binance's public klines endpoint. No API key needed
    for spot klines — they're free and rate-limited per IP (we cache the
    response 60 s in-process to stay well inside the limit). Returns
    candles in the Zerodha-shaped dict ({date, open, high, low, close,
    volume}) the chart frontend already understands."""
    import httpx
    from datetime import datetime, timezone

    bi = _BINANCE_INTERVAL_MAP.get(interval, "5m")
    # Binance hard-caps `limit` at 1000. Pick a window that comfortably
    # fills the chart for the requested days at the requested interval.
    per_day = {"1m": 1440, "3m": 480, "5m": 288, "15m": 96, "30m": 48,
               "1h": 24, "4h": 6, "1d": 1, "1w": 1}.get(bi, 288)
    limit = min(1000, max(50, per_day * days))
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": bi, "limit": limit}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            raw = r.json()
    except Exception:
        return []

    out: list[dict] = []
    for row in raw:
        # Binance shape: [openTime_ms, open, high, low, close, volume, ...]
        try:
            ts_ms = int(row[0])
            iso = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
            out.append({
                "date": iso,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })
        except (ValueError, IndexError, TypeError):
            continue
    return out


async def _align_candles_to_live(token: str, candles: list[dict]) -> list[dict]:
    """Shift Yahoo candles onto the price scale we actually trade.

    Spot metals / energies chart off Yahoo FUTURES symbols (XAUUSD → the
    COMEX contract), which trade ~50 pts away from the SPOT feed the user
    buys and sells. The chart then showed one scale while BUY/SELL showed
    another, and injecting the live spot LTP into a futures-scaled candle
    made the last bar flick ~50 pts back and forth between the two feeds.

    Offsetting every candle by (live - last_close) keeps the real intraday
    SHAPE but puts it on the tradeable scale. No-ops when the two feeds are
    already the same scale (forex "=X" pairs) so we don't smear normal drift.

    NOTE: must stay ABOVE the ``/{token}/history`` decorator — defined
    between the decorator and its coroutine, FastAPI would bind the route to
    this helper and every history call would 422.
    """
    if not candles:
        return candles
    try:
        quote = await market_data_service.get_quote(token)
        live = float((quote or {}).get("ltp") or 0)
        last_close = float(candles[-1]["close"])
    except (TypeError, ValueError, KeyError, AttributeError):
        return candles
    except Exception:  # pragma: no cover — never break the chart over this
        logger.debug("align_candles_quote_failed token=%s", token, exc_info=True)
        return candles
    if live <= 0 or last_close <= 0:
        return candles
    off = live - last_close
    # Same-scale feed (forex) or ordinary staleness → leave it alone.
    if abs(off) / last_close < 0.0015:
        return candles
    for c in candles:
        for k in ("open", "high", "low", "close"):
            try:
                c[k] = float(c[k]) + off
            except (TypeError, ValueError, KeyError):
                pass
    return candles


@router.get("/{token}/chart-levels", response_model=APIResponse[list[dict]])
async def chart_levels(token: str, user: CurrentUser):
    """Admin-defined horizontal lines for this instrument.

    Resolved through the user's ownership cascade, so a broker's clients see
    that broker's lines and nobody sees another tenant's. Empty list when the
    admin has set none — the chart simply draws nothing.
    """
    from app.services import chart_level_service

    return APIResponse(data=await chart_level_service.resolve_for_user(user, token))


@router.get("/{token}/history", response_model=APIResponse[list[dict]])
async def history(
    token: str,
    user: CurrentUser,
    interval: str = Query(default="5minute"),
    days: int = Query(default=5, ge=1, le=365),
):
    """OHLC candles for a chart.

    Tries data sources in priority order:
      1. Binance public klines — any crypto pair (BTCUSDT, ETHUSDT, …)
      2. Yahoo Finance chart — forex / spot metals (EURUSD=X, XAUUSD=X, …)
      3. Zerodha — Indian indices / equity / F&O / MCX

    Returns an empty list when none of the sources have data so the chart
    UI shows a clean "no candles yet" state instead of fake random-walk OHLC.
    """
    from app.services.zerodha_service import zerodha

    # Serve from the 60 s in-process cache when fresh. Keyed by
    # (token, interval, days) — different timeframes share the same
    # underlying instrument but yield different candle series.
    cache_key = (token, interval, days)
    now_ms = int(_t_hist.time() * 1000)
    cached = _history_cache.get(cache_key)
    if cached and (now_ms - cached[0]) < _HISTORY_CACHE_TTL_MS:
        return APIResponse(data=cached[1])

    # ── Source 1: Binance (crypto) ────────────────────────────────────
    # Check this FIRST so a Zerodha-disabled environment still gets
    # crypto charts. Bails immediately for non-crypto tokens, so there's
    # no penalty for Indian instruments.
    bn_symbol = _binance_symbol_for(token)
    if bn_symbol is not None:
        candles = await _fetch_binance_klines(bn_symbol, interval, days)
        if candles:
            _history_cache[cache_key] = (now_ms, candles)
            return APIResponse(data=candles)
        # Binance silently failed — fall through to the Zerodha branch
        # so an admin who's mapped a crypto token to Zerodha (rare) still
        # gets data instead of an empty chart.

    # ── Source 2: Yahoo Finance (forex / spot metals) ─────────────────
    # Bridges the gap for FX_EURUSD / FX_USDINR / XAUUSD / etc. — these
    # have a live tick stream from Infoway but no historical data on the
    # platform, so the chart used to bootstrap from a single live tick
    # and show no candles at all. Yahoo's free chart endpoint covers
    # every major forex pair and spot metal.
    ya_symbol = _yahoo_symbol_for(token)
    if ya_symbol is not None:
        candles = await _fetch_yahoo_chart(ya_symbol, interval, days)
        if candles:
            # Yahoo serves the FUTURES contract for spot metals/energy —
            # shift onto the live tradeable scale before caching so the
            # chart matches the BUY/SELL buttons (and the live tick the
            # datafeed injects into the last bar doesn't flick ~50 pts).
            candles = await _align_candles_to_live(token, candles)
            _history_cache[cache_key] = (now_ms, candles)
            return APIResponse(data=candles)
        # Yahoo failed (block / rate limit) — fall through. For forex we
        # have no Zerodha mapping so this will return empty downstream.

    inst = await _find_or_create_from_zerodha(token)
    if inst is None:
        # For crypto / forex tokens we'd never have created a Zerodha
        # instrument — that's expected, just return empty rather than 404
        # so the chart UI shows the "no candles" state.
        if bn_symbol is not None or ya_symbol is not None:
            _history_cache[cache_key] = (now_ms, [])
            return APIResponse(data=[])
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Instrument {token} not found")
    z_status = await zerodha.get_status()

    if z_status["isConnected"]:
        # After admin subscribes via Zerodha Connect we mirror the Kite
        # instrument_token straight into Instrument.token, so we can pass it
        # to the historical API directly. As a safety net, also try the
        # subscribed list (handles seeded Instruments that pre-date the mirror).
        from app.models.zerodha_settings import ZerodhaSettings
        from datetime import datetime, timedelta, timezone

        kite_token: int | None = None
        try:
            kite_token = int(inst.token)
        except (TypeError, ValueError):
            kite_token = None
        if kite_token is None:
            settings = await ZerodhaSettings.find_one()
            match = next(
                (i for i in (settings.subscribedInstruments if settings else []) if i.symbol == inst.symbol),
                None,
            )
            if match is not None:
                kite_token = match.token

        if kite_token is not None:
            try:
                to_dt = datetime.now(timezone.utc)
                from_dt = to_dt - timedelta(days=days)
                candles = await zerodha.get_historical(kite_token, from_dt, to_dt, interval)
                if candles:
                    _history_cache[cache_key] = (now_ms, candles)
                    return APIResponse(data=candles)
            except Exception:
                pass

    # No real candles available — return empty so the chart shows an
    # empty state instead of fabricated random-walk OHLC. Cache the
    # empty result briefly so we don't hammer upstream APIs for the same
    # missing instrument every poll.
    _history_cache[cache_key] = (now_ms, [])
    return APIResponse(data=[])
