"""IST-aware time helpers and market-hours predicates."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.config import settings

IST: ZoneInfo = ZoneInfo(settings.DEFAULT_TIMEZONE)
UTC: timezone = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_ist() -> datetime:
    return datetime.now(IST)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IST)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(UTC)


def parse_hhmm(value: str) -> time:
    h, m = value.split(":", 1)
    return time(int(h), int(m))


def market_open_time() -> time:
    return parse_hhmm(settings.MARKET_OPEN_TIME)


def market_close_time() -> time:
    return parse_hhmm(settings.MARKET_CLOSE_TIME)


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # Sat=5, Sun=6


def is_market_open(at: datetime | None = None) -> bool:
    """Naive check — does NOT consider holidays. The HolidayService overlays that."""
    now = to_ist(at or now_ist())
    if is_weekend(now.date()):
        return False
    return market_open_time() <= now.time() <= market_close_time()


def start_of_day_ist(d: date | None = None) -> datetime:
    d = d or now_ist().date()
    return datetime.combine(d, time.min, tzinfo=IST)


def end_of_day_ist(d: date | None = None) -> datetime:
    d = d or now_ist().date()
    return datetime.combine(d, time.max, tzinfo=IST)


def add_business_days(d: date, n: int) -> date:
    """Naive — does not consider holidays. For T+1/T+2 settlement."""
    out = d
    added = 0
    while added < n:
        out += timedelta(days=1)
        if not is_weekend(out):
            added += 1
    return out


# ── Segment-aware market-close helpers ───────────────────────────────
# Drives the auto MIS→NRML / carry-forward rollover loop (the minute the
# per-position Carry-Forward decision runs: ON → carry, OFF → square off).
# Operator-set rollover times for this platform: NSE/BSE 16:00 IST, MCX
# 23:40 IST. Forex (CDS) is 24/5 and crypto 24/7 — no daily rollover, so
# those segments are excluded from the loop entirely.
# NOTE: these ONLY affect the rollover + `is_after_close` (pending-order
# firing / risk-EOD) — NEW order acceptance is gated by live-feed staleness,
# so moving these does not open a stale-price trading window.
NSE_BSE_CLOSE: time = time(16, 0)
MCX_CLOSE: time = time(23, 40)
# Session OPEN times — symmetric to the close times above. Needed so the
# risk enforcer recognises the PRE-OPEN window (weekday midnight → open)
# as "market closed". Without these, is_after_close() alone returns False
# before 15:30, so 08:00 IST on a weekday looked "open" and the enforcer
# fired SL/TP/stop-out against yesterday's STALE closing tick — the
# "market band hai phir bhi trade close ho gaya" phantom-close bug that
# hit weekday mornings (most visibly Monday) between 00:00 and 09:15 IST.
NSE_BSE_OPEN: time = time(9, 15)
MCX_OPEN: time = time(9, 0)

INDIAN_EQUITY_FNO_SEGMENTS: frozenset[str] = frozenset({
    # Names that show up as `instrument.segment` in actual Position
    # docs — these are what the matching engine writes (driven by the
    # Zerodha CSV / Infoway mapping). Operator-flagged 22-May: NIFTY +
    # BHARTIARTL etc. positions never auto-closed at 15:31 IST
    # rollover even when the user couldn't cover overnight, because
    # this set only had the "NSE_*" names; positions on NFO / BFO
    # (which is where futures + options actually live) were silently
    # invisible to the convert_intraday_to_carry loop.
    "NSE_EQUITY", "NSE_EQ",
    "NSE_FUTURE", "NSE_INDEX_FUTURE",
    # Abbreviated variants from _LAST_THURSDAY_SEGMENTS / older instrument seeds
    "NSE_FUT", "NSE_OPT",
    "NSE_STOCK_OPTION_BUY", "NSE_STOCK_OPTION_SELL",
    "NSE_INDEX_OPTION_BUY", "NSE_INDEX_OPTION_SELL",
    # NFO is the segment NSE futures / options trade ON — every
    # NIFTY / BANKNIFTY / FUT / CE / PE position has
    # instrument.segment == "NFO_FUTURE" or "NFO_OPTION".
    "NFO_FUTURE", "NFO_OPTION",
    # Abbreviated / plain variants that appear when instruments are seeded via
    # admin panel or older import paths ("NFO_FUT", "NFO_OPT", bare "NFO").
    # Without these, market_close_time_for_segment returns None → is_after_close
    # returns False → risk enforcer fires SL/TP/stop-out at stale post-market
    # prices (root cause of the 15-Jun 18:03 phantom NFO close bug).
    "NFO_FUT", "NFO_OPT", "NFO",
    "BSE_EQUITY", "BSE_EQ",
    "BSE_FUTURE", "BSE_INDEX_FUTURE",
    "BSE_FUT", "BSE_OPT",
    "BSE_OPTION_BUY", "BSE_OPTION_SELL",
    # BFO is the analogous BSE futures/options segment.
    "BFO_FUTURE", "BFO_OPTION",
    "BFO_FUT", "BFO_OPT",
})
MCX_SEGMENTS: frozenset[str] = frozenset({
    "MCX_FUTURE", "MCX_OPTION_BUY", "MCX_OPTION_SELL",
    # Same Zerodha-CSV-driven shape — actual MCX position docs use
    # "MCX_FUT" / "MCX_OPT" in `instrument.segment`. Operator's
    # CRUDEOIL / SILVERMIC positions are stored with these names.
    "MCX_FUT", "MCX_OPT",
    # Standalone MCX_OPTION variant covers a few rows that the
    # CSV-import normaliser produced under that single name.
    "MCX_OPTION",
})
ROLLOVER_EXEMPT_SEGMENTS: frozenset[str] = frozenset({
    # Forex: 24/5 — no intraday close, MIS stays MIS across days until weekend.
    "CDS_FUTURE", "CDS_OPTION_BUY", "CDS_OPTION_SELL",
    # Crypto: 24/7 — never converts.
    "CRYPTO_SPOT", "CRYPTO_FUTURE", "CRYPTO_PERPETUAL",
})


def market_close_time_for_segment(segment: str | None) -> time | None:
    """IST close time for the segment's exchange group, or None when the
    segment doesn't have a daily close (forex / crypto)."""
    if not segment:
        return None
    if segment in INDIAN_EQUITY_FNO_SEGMENTS:
        return NSE_BSE_CLOSE
    if segment in MCX_SEGMENTS:
        return MCX_CLOSE
    # Prefix-based fallback: handles any future variant not yet in the sets above.
    # Forex (CDS_*) starts with "CDS"; crypto starts with "CRYPTO" — neither
    # matches the NSE/BSE/NFO/BFO/MCX prefixes below, so they still get None.
    seg_up = segment.upper()
    if seg_up.startswith(("NSE", "BSE", "NFO", "BFO")):
        return NSE_BSE_CLOSE
    if seg_up.startswith("MCX"):
        return MCX_CLOSE
    return None  # ROLLOVER_EXEMPT_SEGMENTS or anything unknown


def is_after_close(segment: str, at: datetime | None = None) -> bool:
    """True if the given IST instant is at or past the segment's close.
    Returns False for rollover-exempt segments (forex / crypto)."""
    close_t = market_close_time_for_segment(segment)
    if close_t is None:
        return False
    now = to_ist(at) if at else now_ist()
    return now.time() >= close_t


def market_open_time_for_segment(segment: str | None) -> time | None:
    """IST open time for the segment's exchange group, or None when the
    segment has no daily open (forex / crypto = 24×5 / 24×7).

    Mirror of ``market_close_time_for_segment`` — same membership sets and
    prefix fallback so both ends of the session window stay in lockstep."""
    if not segment:
        return None
    if segment in INDIAN_EQUITY_FNO_SEGMENTS:
        return NSE_BSE_OPEN
    if segment in MCX_SEGMENTS:
        return MCX_OPEN
    seg_up = segment.upper()
    if seg_up.startswith(("NSE", "BSE", "NFO", "BFO")):
        return NSE_BSE_OPEN
    if seg_up.startswith("MCX"):
        return MCX_OPEN
    return None  # ROLLOVER_EXEMPT_SEGMENTS or anything unknown


def is_before_open(segment: str, at: datetime | None = None) -> bool:
    """True if the given IST instant is before the segment's daily open.
    Returns False for 24×5 / 24×7 segments (forex / crypto).

    Together with ``is_after_close`` this brackets the FULL out-of-session
    window. The risk enforcer needs both: ``is_after_close`` covers
    15:30→midnight, ``is_before_open`` covers midnight→09:15. Before this
    helper existed the pre-open morning gap was treated as "open", letting
    the enforcer phantom-close positions against yesterday's stale tick."""
    open_t = market_open_time_for_segment(segment)
    if open_t is None:
        return False
    now = to_ist(at) if at else now_ist()
    return now.time() < open_t


def is_within_open_grace(
    segment: str, grace_seconds: int, at: datetime | None = None
) -> bool:
    """True if the IST instant is inside the first ``grace_seconds`` AFTER the
    segment's daily open.

    At the exact open bell the live feed has often not delivered a fresh tick
    yet (WS reconnect / on-demand resubscribe lag). Marking a position against
    the LAST cached tick in that gap uses an overnight/stale price the new
    session never traded — which fired phantom stop-outs at market open
    (2026-07-01 09:00 MCX: CRUDEOIL long stopped out at a stale 6523 while the
    session low was 6631). Callers treat this like ``is_before_open`` — skip
    actioning stop-out / SL / TP and re-evaluate once real prices flow.
    Returns False for 24×5 / 24×7 segments (forex / crypto)."""
    if grace_seconds <= 0:
        return False
    open_t = market_open_time_for_segment(segment)
    if open_t is None:
        return False
    now = to_ist(at) if at else now_ist()
    open_dt = now.replace(
        hour=open_t.hour, minute=open_t.minute, second=open_t.second, microsecond=0
    )
    return open_dt <= now < open_dt + timedelta(seconds=grace_seconds)


def iso_week_key(at: datetime | None = None) -> str:
    """Stable per-week identifier in IST, e.g. ``"2026-W24"``.

    Used by the weekly-settlement engine as the unique batch key so at most
    ONE settlement batch can exist per calendar week regardless of how many
    workers / restarts try to start one."""
    now = to_ist(at) if at else now_ist()
    iso = now.isocalendar()  # (year, week, weekday)
    return f"{iso[0]}-W{iso[1]:02d}"


def is_saturday_settlement_window(at: datetime | None = None) -> bool:
    """True during the weekly-settlement firing window: Saturday 11:00–11:59
    IST (Saturday 11 AM — operator-chosen). The settlement loop wakes every
    minute, so a one-hour window gives ample slack for the leader to pick up
    and fire exactly once per week (the unique ``week_key`` batch guards
    against a double fire). Saturday is weekday() == 5."""
    now = to_ist(at) if at else now_ist()
    return now.weekday() == 5 and now.hour == 11
