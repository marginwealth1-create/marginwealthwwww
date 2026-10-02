"""Daily instrument-expiry cleanup.

Background loop that runs every hour and applies the same rule the user
asked for:

    Expiry day  → instrument still shows / trades normally
    Day after   → instrument is removed from every user's watchlist,
                  unsubscribed from Zerodha, and marked inactive in the
                  Instrument collection so search stops returning it.

Idempotent — running twice in a row is a no-op once everything has been
swept. The loop exists so admins don't have to remember to nuke yesterday's
expired option chain manually.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone

from app.models.instrument import Instrument
from app.models.watchlist import WatchlistItem

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

_running = False

# ── Symbol-derived expiry (fallback for null Instrument.expiry) ──────────
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
# Zerodha weekly single-char month codes for Oct/Nov/Dec (Jan-Sep use digit).
_WEEK_MONTH = {"O": 10, "N": 11, "D": 12}
_MONTHLY_FUT_RE = re.compile(
    r"^[A-Z&]+?(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)FUT$"
)
_MONTHLY_OPT_RE = re.compile(
    r"^[A-Z&]+?(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d+)(CE|PE)$"
)
_WEEKLY_OPT_RE = re.compile(r"^[A-Z&]+?(\d{2})([1-9OND])(\d{2})(\d+)(CE|PE)$")


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def parse_symbol_expiry(symbol: str | None) -> date | None:
    """Best-effort expiry date from an F&O trading symbol — used ONLY as a
    fallback when Instrument.expiry is null. Returns None unless the symbol
    matches a known high-confidence pattern, so a parse miss NEVER removes an
    instrument (it just leaves it untouched).

    Formats:
      • Monthly future  CRUDEOIL26JULFUT   → month-end (safe upper bound)
      • Monthly option  NIFTY26JUL24000CE  → month-end
      • Weekly option   SENSEX2670276900CE → exact 2026-07-02
        (UND · YY · M · DD · STRIKE · CE/PE; M = 1-9 or O/N/D)
    """
    if not symbol:
        return None
    s = symbol.upper().strip()
    m = _MONTHLY_FUT_RE.match(s)
    if m:
        return _month_end(2000 + int(m.group(1)), _MONTHS[m.group(2)])
    m = _MONTHLY_OPT_RE.match(s)
    if m:
        return _month_end(2000 + int(m.group(1)), _MONTHS[m.group(2)])
    m = _WEEKLY_OPT_RE.match(s)
    if m:
        year = 2000 + int(m.group(1))
        mc = m.group(2)
        month = int(mc) if mc.isdigit() else _WEEK_MONTH[mc]
        day = int(m.group(3))
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def _ist_today_date():
    """Indian trading-day boundary. We compare against IST midnight, not
    UTC, so a contract expiring on Thursday 'survives' through to Friday
    morning 00:00 IST regardless of the host machine's timezone."""
    return datetime.now(IST).date()


# ── Live-contract resolver (shared by the sweep AND every read path) ─────
#
# `parse_symbol_expiry` deliberately returns MONTH-END for monthly contracts,
# which is a safe upper bound on NSE (last Thursday) but badly wrong on MCX:
# CRUDEOIL expires ~19th, so CRUDEOIL26JULFUT looked "alive until Jul 31" and
# kept showing in favourites for ~10 days after it actually died (operator
# report: "july mcx ka crude expire ho gaya hai fir bhi dikh raha hai").
#
# The Kite instrument dump is the real source of truth: it lists every LIVE
# contract and drops expired ones. So a derivative symbol that is ABSENT from
# a warm catalog is delisted → treat as expired. Equities never match
# `parse_symbol_expiry`, so they can never be caught by that rule.
# Don't trust a suspiciously small catalog (half-written / truncated fetch).
_CATALOG_MIN_ROWS = 100
_CATALOG_TTL_SEC = 600.0

_catalog_idx: dict[str, date | None] = {}
_catalog_warm: set[str] = set()
_catalog_at: float = 0.0
_catalog_lock = asyncio.Lock()


def _build_catalog_index(
    caches: list[tuple[str, list[dict]]],
) -> tuple[dict[str, date | None], set[str]]:
    """Pure CPU pass over the Kite catalogs — runs in a THREAD (the dumps are
    ~200K rows across exchanges; folding them on the event loop stalls the
    worker, same reason the option-chain scan is threaded)."""
    idx: dict[str, date | None] = {}
    warm: set[str] = set()
    for ex_key, rows in caches:
        if not rows or len(rows) < _CATALOG_MIN_ROWS:
            continue
        warm.add(str(ex_key).upper())
        for r in rows:
            tok = str(r.get("token") or "")
            if not tok:
                continue
            exp_raw = r.get("expiry")
            exp: date | None = None
            if exp_raw:
                try:
                    exp = datetime.fromisoformat(str(exp_raw)[:10]).date()
                except Exception:  # noqa: BLE001
                    exp = None
            idx[tok] = exp
    return idx, warm


async def catalog_index(force: bool = False) -> tuple[dict[str, date | None], set[str]]:
    """``(token -> expiry, warm_exchange_keys)`` built from the in-process Kite
    instrument cache. Memoised for 10 min because read paths call it per
    request; the sweep passes ``force=True`` for a fresh view.

    An exchange only lands in `warm` when its catalog actually has rows, so a
    cold / unauthenticated Zerodha can never make us judge live contracts dead.
    """
    global _catalog_idx, _catalog_warm, _catalog_at
    import time as _time

    def _fresh() -> bool:
        return bool(_catalog_warm) and (_time.time() - _catalog_at) < _CATALOG_TTL_SEC

    if not force and _fresh():
        return _catalog_idx, _catalog_warm

    async with _catalog_lock:
        # A sibling request may have rebuilt it while we waited on the lock.
        if not force and _fresh():
            return _catalog_idx, _catalog_warm
        try:
            from app.services.zerodha_service import zerodha

            caches = [(k, v) for k, v in list(zerodha._instruments_cache.items())]
            idx, warm = await asyncio.to_thread(_build_catalog_index, caches)
        except Exception:  # noqa: BLE001 — a cold feed must not break read paths
            logger.warning("expiry_catalog_index_failed")
            return _catalog_idx, _catalog_warm

        _catalog_idx, _catalog_warm, _catalog_at = idx, warm, _time.time()
        return _catalog_idx, _catalog_warm


def is_dead_contract(
    *,
    token: str | None,
    symbol: str | None,
    exchange: str | None,
    expiry: date | None,
    today: date,
    idx: dict[str, date | None],
    warm: set[str],
) -> bool:
    """True when this contract must not be shown / traded any more.

    Order of evidence (first hit wins):
      1. a real expiry date (doc, else the catalog row) already in the past
      2. the symbol-derived expiry (month-end upper bound) already in the past
      3. an F&O symbol that is GONE from a warm Kite catalog → delisted

    Anything we can't confidently classify is left alone — a parse miss or a
    cold catalog never removes an instrument.
    """
    tok = str(token or "")
    exp = expiry if expiry is not None else idx.get(tok)
    if isinstance(exp, datetime):  # Mongo can hand back a datetime for a date
        exp = exp.date()
    if exp is not None and exp < today:
        return True
    parsed = parse_symbol_expiry(symbol)
    if parsed is None:
        return False  # equity / unknown format → never touched
    if parsed < today:
        return True
    ex_key = str(exchange or "").upper()
    return bool(tok and ex_key in warm and tok not in idx)


async def dead_tokens(rows) -> set[str]:
    """Read-path guard. ``rows`` = iterable of ``(token, symbol, exchange)``.

    Returns the subset of tokens whose contract has expired or been delisted,
    so watchlists / quotes / search can hide them the instant they're read —
    without waiting for the hourly sweep, and regardless of whether the
    Instrument doc carries a correct `expiry` (or exists at all).
    """
    rows = [(str(t or ""), s, e) for t, s, e in rows if t]
    if not rows:
        return set()

    today = _ist_today_date()
    idx, warm = await catalog_index()

    by_token: dict[str, Instrument] = {}
    try:
        insts = await Instrument.find(
            {"token": {"$in": [t for t, _s, _e in rows]}}
        ).to_list()
        by_token = {i.token: i for i in insts}
    except Exception:  # noqa: BLE001
        logger.warning("dead_tokens_instrument_lookup_failed")

    out: set[str] = set()
    for tok, sym, exch in rows:
        inst = by_token.get(tok)
        if is_dead_contract(
            token=tok,
            symbol=(getattr(inst, "symbol", None) or sym),
            exchange=(str(getattr(inst, "exchange", "") or "") or exch),
            expiry=getattr(inst, "expiry", None),
            today=today,
            idx=idx,
            warm=warm,
        ):
            out.add(tok)
    return out


async def cleanup_expired_once() -> dict[str, int]:
    """Single sweep. Returns counts so the caller can log what changed.

    Strategy:
      • cutoff_date = today_IST - 1 day. Anything with `expiry < today_IST`
        is "yesterday or earlier" → cleanup target.
      • For each expired Instrument:
          - delete every WatchlistItem that references its token (across all
            users — there's no per-user opt-out for an expired contract)
          - unsubscribe the token from the Zerodha live ticker (skipped for
            non-Kite tokens)
          - mark the Instrument is_active=False so /instruments/search stops
            returning it. We DON'T hard-delete — historical orders / trades
            still reference these tokens.
    """
    today = _ist_today_date()
    idx, warm = await catalog_index(force=True)

    # A) Instruments that already carry a real expiry date.
    expired = await Instrument.find(
        {"expiry": {"$ne": None, "$lt": today}, "is_active": True}
    ).to_list()

    # B) Fallback for F&O rows whose `expiry` was never populated (seed gap):
    #    derive it from the trading symbol (SENSEX2670276900CE → 2026-07-02).
    #    Without this a null-expiry weekly option lingers in search + every
    #    user's favorites forever, showing a stale index-fallback price and a
    #    "no live session" toast (operator-flagged). We also backfill the
    #    parsed date onto the doc so search/expiry filters + the next sweep
    #    see a real value. A symbol we can't confidently parse is left alone.
    seen_ids = {i.id for i in expired}
    null_expiry_fno = await Instrument.find(
        {
            "expiry": None,
            "is_active": True,
            "segment": {"$regex": "OPT|FUT", "$options": "i"},
        }
    ).to_list()
    backfilled = 0
    for inst in null_expiry_fno:
        # The Kite catalog carries the EXACT date; the symbol parse is only a
        # month-end upper bound (wrong by ~10 days for MCX, whose contracts
        # expire mid-month). Prefer the catalog whenever it still lists the
        # token, so the backfilled value can be trusted by the < today test.
        parsed = idx.get(str(inst.token or "")) or parse_symbol_expiry(inst.symbol)
        if parsed is None:
            continue
        try:
            inst.expiry = parsed
            await inst.save()
            backfilled += 1
        except Exception:
            logger.exception(
                "expiry_cleanup_backfill_failed", extra={"symbol": inst.symbol}
            )
        if parsed < today and inst.id not in seen_ids:
            expired.append(inst)
            seen_ids.add(inst.id)

    # B2) Delisted derivatives — rows whose stored `expiry` still looks future
    #     (typically a month-end guess) but that Kite no longer lists at all.
    #     This is what finally kills a mid-month MCX contract (CRUDEOIL July
    #     expires ~19th, month-end guess said Jul 31 → it lingered ~10 days).
    #     Only runs for exchanges whose catalog is actually warm, so a cold /
    #     disconnected Zerodha can never mass-deactivate live contracts.
    delisted = 0
    if warm:
        fno_active = await Instrument.find(
            {"is_active": True, "instrument_type": {"$in": ["FUT", "CE", "PE"]}}
        ).to_list()
        for inst in fno_active:
            if inst.id in seen_ids:
                continue
            if is_dead_contract(
                token=inst.token,
                symbol=inst.symbol,
                exchange=str(inst.exchange),
                expiry=inst.expiry,
                today=today,
                idx=idx,
                warm=warm,
            ):
                expired.append(inst)
                seen_ids.add(inst.id)
                delisted += 1

    # C) Watchlist rows whose Instrument doc is missing entirely (added
    #    straight off the Kite catalog before the mirror ran, or the doc was
    #    deleted) — the instrument-driven sweep above can never see them, so
    #    they survived every previous sweep. Resolve them from the symbol +
    #    catalog directly and delete.
    orphan_wl_removed = await _sweep_watchlist_orphans(today, idx, warm)

    if not expired:
        return {
            "instruments": 0,
            "watchlist_items": orphan_wl_removed,
            "unsubscribed": 0,
            "positions_settled": 0,
            "expiry_backfilled": backfilled,
            "delisted": 0,
        }

    expired_tokens = [str(i.token) for i in expired if i.token]
    expired_symbols = [i.symbol for i in expired if i.symbol]

    # 0) Settle any OPEN positions in these expired contracts FIRST — before
    #    we unsubscribe their tokens below. An expired contract no longer
    #    trades; once its token is unsubscribed the risk-enforcer can never
    #    price it, so it silently skips SL/TP/stop-out and the position would
    #    sit OPEN forever holding the user's margin (the risk_ltp_fetch_failed
    #    "zombie position" flood). settle_expired_position books realized P&L
    #    at the last-known price, releases the margin and flips the row CLOSED.
    #    Settling here (token still subscribed on the first sweep) gives the
    #    best chance of a fresh live price; it falls back to the position's
    #    frozen `ltp` otherwise.
    settled = 0
    from app.models.position import Position, PositionStatus
    from app.services import position_service

    open_in_expired = await Position.find(
        {
            "status": PositionStatus.OPEN.value,
            "instrument.token": {"$in": expired_tokens},
        }
    ).to_list()
    for _pos in open_in_expired:
        try:
            if await position_service.settle_expired_position(_pos) == "settled":
                settled += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "expiry_cleanup_settle_failed", extra={"position_id": str(_pos.id)}
            )

    # 1) Yank from every user's watchlist — by token AND by symbol. Symbol is
    #    the belt-and-braces path: a favorite whose stored instrument_token
    #    drifted from the Instrument doc's token (duplicate/re-seeded rows)
    #    would survive a token-only delete and keep showing the dead contract.
    wl_removed = 0
    if expired_tokens:
        wl_result = await WatchlistItem.find(
            {"instrument_token": {"$in": expired_tokens}}
        ).delete()
        wl_removed += getattr(wl_result, "deleted_count", 0) or 0
    if expired_symbols:
        wl_result_sym = await WatchlistItem.find(
            {"symbol": {"$in": expired_symbols}}
        ).delete()
        wl_removed += getattr(wl_result_sym, "deleted_count", 0) or 0

    # 2) Unsubscribe from Zerodha — only for numeric Kite tokens
    int_tokens: list[int] = []
    for t in expired_tokens:
        try:
            int_tokens.append(int(t))
        except (TypeError, ValueError):
            pass
    unsubbed = 0
    if int_tokens:
        try:
            from app.services.zerodha_service import zerodha
            unsubbed = await zerodha.unsubscribe_tokens_on_demand(int_tokens)
        except Exception:
            logger.exception("expiry_cleanup_zerodha_unsubscribe_failed")

    # 3) Mark inactive so search stops returning them
    for inst in expired:
        try:
            inst.is_active = False
            inst.is_tradable = False
            await inst.save()
        except Exception:
            logger.exception(
                "expiry_cleanup_instrument_save_failed", extra={"token": inst.token}
            )

    wl_removed += orphan_wl_removed

    logger.info(
        "expiry_cleanup_swept",
        extra={
            "instruments": len(expired),
            "watchlist_items_removed": wl_removed,
            "tokens_unsubscribed": unsubbed,
            "positions_settled": settled,
            "expiry_backfilled": backfilled,
            "delisted": delisted,
            "cutoff_date": str(today),
        },
    )
    return {
        "instruments": len(expired),
        "watchlist_items": wl_removed,
        "unsubscribed": unsubbed,
        "positions_settled": settled,
        "expiry_backfilled": backfilled,
        "delisted": delisted,
    }


async def _sweep_watchlist_orphans(
    today: date, idx: dict[str, date | None], warm: set[str]
) -> int:
    """Delete watchlist rows for dead contracts that the instrument-driven
    sweep can't reach — i.e. rows whose token has NO Instrument doc. Judged
    purely from the stored symbol + the Kite catalog, so an equity row (which
    never parses as F&O) is never touched. Returns rows deleted."""
    try:
        items = await WatchlistItem.find({}).to_list()
    except Exception:  # noqa: BLE001
        logger.exception("expiry_cleanup_watchlist_scan_failed")
        return 0
    if not items:
        return 0

    tokens = list({str(it.instrument_token) for it in items if it.instrument_token})
    known: set[str] = set()
    try:
        known = {
            i.token
            for i in await Instrument.find({"token": {"$in": tokens}}).to_list()
        }
    except Exception:  # noqa: BLE001
        logger.exception("expiry_cleanup_watchlist_instrument_lookup_failed")
        return 0

    dead: list[WatchlistItem] = []
    for it in items:
        tok = str(it.instrument_token or "")
        if not tok or tok in known:
            continue  # covered by the Instrument-driven sweep above
        if is_dead_contract(
            token=tok,
            symbol=it.symbol,
            exchange=str(it.exchange),
            expiry=None,
            today=today,
            idx=idx,
            warm=warm,
        ):
            dead.append(it)

    removed = 0
    for it in dead:
        try:
            await it.delete()
            removed += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "expiry_cleanup_watchlist_orphan_delete_failed",
                extra={"token": it.instrument_token},
            )
    return removed


async def expiry_cleanup_loop(interval_sec: float = 3600.0) -> None:
    """Hourly sweep. An hourly cadence is enough because expiry happens at
    instrument granularity (date), not minute — but it's frequent enough
    that users never see day-old contracts after the boundary. Idempotent
    — second call returns immediately."""
    global _running
    if _running:
        return
    _running = True
    logger.info("expiry_cleanup_loop_started", extra={"interval_sec": interval_sec})
    try:
        # First sweep happens immediately on boot — picks up anything that
        # expired while the server was down.
        try:
            await cleanup_expired_once()
        except Exception:
            logger.exception("expiry_cleanup_initial_sweep_failed")
        while _running:
            await asyncio.sleep(interval_sec)
            try:
                await cleanup_expired_once()
            except Exception:
                logger.exception("expiry_cleanup_tick_failed")
    finally:
        _running = False
        logger.info("expiry_cleanup_loop_stopped")


def stop_expiry_cleanup() -> None:
    global _running
    _running = False
