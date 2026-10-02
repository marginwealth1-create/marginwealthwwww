"""Default Zerodha subscription set — auto-loaded on first admin connect.

Resolves a curated list of equities + indices + the current/next-expiry option
chain for NIFTY/BANKNIFTY/FINNIFTY against the **live Zerodha instruments CSV**
fetched from the Kite API. This avoids the chicken-and-egg problem where the
local Instrument DB is empty on first connect.

Returns dicts shaped for ``SubscribedInstrument``, so all lot sizes / tick sizes
come straight from the authoritative Kite catalog.

Total instruments: ~600 (well under Kite's 3000-per-session WS cap).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ── Curated equity + index symbol set ────────────────────────────────
# Top NSE F&O / popular cash equities — covers most user demand.
NSE_TOP_EQUITIES: list[str] = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HINDUNILVR", "ITC",
    "LT", "SBIN", "BHARTIARTL", "KOTAKBANK", "AXISBANK", "BAJFINANCE",
    "MARUTI", "ASIANPAINT", "HCLTECH", "SUNPHARMA", "NTPC", "TITAN",
    "ULTRACEMCO", "WIPRO", "NESTLEIND", "POWERGRID", "ONGC", "COALINDIA",
    "ADANIENT", "ADANIPORTS", "M&M", "TATAMOTORS", "BAJAJFINSV", "JSWSTEEL",
    "TECHM", "DRREDDY", "GRASIM", "DIVISLAB", "CIPLA", "EICHERMOT", "BPCL",
    "HEROMOTOCO", "BAJAJ-AUTO", "BRITANNIA", "TATASTEEL", "INDUSINDBK",
    "HINDALCO", "TATACONSUM", "SBILIFE", "HDFCLIFE", "APOLLOHOSP", "UPL",
    "SHRIRAMFIN", "LTIM",
    # Additional popular F&O scrips
    "DLF", "GODREJCP", "PIDILITIND", "DABUR", "MARICO", "COLPAL", "BERGEPAINT",
    "BIOCON", "LUPIN", "CADILAHC", "AUROPHARMA", "GLENMARK", "TORNTPHARM",
    "VEDL", "NATIONALUM", "SAIL", "JINDALSTEL", "RECLTD", "PFC", "ICICIPRULI",
    "BANKBARODA", "PNB", "CANBK", "FEDERALBNK", "IDFCFIRSTB", "AUBANK",
    "DMART", "ZOMATO", "PAYTM", "POLICYBZR", "NYKAA", "DELHIVERY", "PVRINOX",
    "HAL", "BEL", "GAIL", "IOC", "PETRONET", "BOSCHLTD", "MOTHERSON",
    "ABCAPITAL", "ABFRL", "TRENT", "PAGEIND", "VOLTAS", "HAVELLS", "AMBUJACEM",
    "ACC", "JKCEMENT", "BHARATFORG", "ASHOKLEY", "TVSMOTOR", "MRF",
    "BALKRISIND", "EXIDEIND", "MFSL", "MUTHOOTFIN", "MANAPPURAM", "CHOLAFIN",
    "PIIND", "BATAINDIA", "RELAXO", "JUBLFOOD", "SRF",
]

# Index spot tickers we always subscribe to
INDEX_SYMBOLS: list[tuple[str, str]] = [
    ("NIFTY 50", "NSE"),
    ("NIFTY BANK", "NSE"),
    ("NIFTY FIN SERVICE", "NSE"),
    ("NIFTY MIDCAP 50", "NSE"),
    ("NIFTY IT", "NSE"),
    ("INDIA VIX", "NSE"),
    ("SENSEX", "BSE"),
    ("BANKEX", "BSE"),
]

# Option-chain underlyings + strike spread + step
# (underlying, kite_symbol_prefix, strike_step, atm_radius)
OPTION_UNDERLYINGS = [
    ("NIFTY", 50, 25),       # 25 strikes either side × 2 sides × 2 expiries = 200
    ("BANKNIFTY", 100, 20),  # 20 strikes × 2 × 2 = 160
    ("FINNIFTY", 50, 15),    # 15 × 2 × 2 = 120
]


def _csv_to_sub(inst: dict[str, Any]) -> dict[str, Any]:
    """Convert a normalised Kite CSV row into the shape SubscribedInstrument expects."""
    token = int(inst.get("token") or 0)
    if not token:
        return {}
    return {
        "token": token,
        "symbol": inst.get("symbol") or "",
        "exchange": inst.get("exchange") or "",
        "segment": inst.get("segment") or "",
        "name": inst.get("name") or inst.get("symbol") or "",
        "lotSize": int(inst.get("lotSize") or 1),
        "tickSize": float(inst.get("tickSize") or 0.05),
        "expiry": inst.get("expiry"),
        "strike": inst.get("strike"),
        "instrumentType": inst.get("instrumentType") or "EQ",
    }


def _resolve_equities_from_csv(
    nse_instruments: list[dict[str, Any]], symbols: list[str]
) -> list[dict[str, Any]]:
    """Resolve curated equity symbols against the live Zerodha NSE CSV."""
    sym_set = {s.upper() for s in symbols}
    out: list[dict[str, Any]] = []
    for inst in nse_instruments:
        sym = (inst.get("symbol") or "").upper()
        itype = (inst.get("instrumentType") or "").upper()
        if sym in sym_set and itype in ("EQ", ""):
            row = _csv_to_sub(inst)
            if row:
                out.append(row)
    return out


def _resolve_indices_from_csv(
    nse_instruments: list[dict[str, Any]],
    bse_instruments: list[dict[str, Any]],
    items: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Resolve index names against the Zerodha CSV (NSE + BSE)."""
    out: list[dict[str, Any]] = []
    name_set = {n.upper() for n, _ in items}
    for inst in nse_instruments + bse_instruments:
        name = (inst.get("name") or "").upper().strip()
        sym = (inst.get("symbol") or "").upper().strip()
        if name in name_set or sym in name_set:
            row = _csv_to_sub(inst)
            if row:
                out.append(row)
    return out


def _resolve_option_chain_from_csv(
    nfo_instruments: list[dict[str, Any]],
    underlying: str,
    atm_radius: int,
    strike_step: int,
) -> list[dict[str, Any]]:
    """Pick current + next expiry for the underlying, ATM ± atm_radius strikes
    on both CE and PE. Resolves from the live NFO CSV."""
    today = datetime.now(timezone.utc).date()
    underlying_upper = underlying.upper()

    # Filter to this underlying's options with valid expiry
    options: list[dict[str, Any]] = []
    for inst in nfo_instruments:
        itype = (inst.get("instrumentType") or "").upper()
        if itype not in ("CE", "PE"):
            continue
        sym = (inst.get("symbol") or "").upper()
        if not sym.startswith(underlying_upper):
            continue
        expiry_str = inst.get("expiry")
        if not expiry_str:
            continue
        try:
            exp_date = datetime.fromisoformat(expiry_str).date()
        except (ValueError, TypeError):
            continue
        if exp_date < today:
            continue
        strike = inst.get("strike")
        if strike is None:
            continue
        try:
            strike_f = float(strike)
        except (TypeError, ValueError):
            continue
        options.append({**inst, "_exp_date": exp_date, "_strike": strike_f})

    if not options:
        return []

    # Group by expiry
    by_expiry: dict[date, list[dict[str, Any]]] = {}
    for opt in options:
        by_expiry.setdefault(opt["_exp_date"], []).append(opt)

    # Pick the two soonest expiries
    sorted_exps = sorted(by_expiry.keys())[:2]

    out: list[dict[str, Any]] = []
    for exp in sorted_exps:
        rows = by_expiry[exp]
        strikes_sorted = sorted({r["_strike"] for r in rows})
        if not strikes_sorted:
            continue
        atm = strikes_sorted[len(strikes_sorted) // 2]
        lo = atm - atm_radius * strike_step
        hi = atm + atm_radius * strike_step
        for r in rows:
            if lo <= r["_strike"] <= hi:
                row = _csv_to_sub(r)
                if row:
                    out.append(row)
    return out


async def build_default_subscriptions(
    fetcher=None,
) -> list[dict[str, Any]]:
    """Build the curated default subscription set by fetching instruments
    directly from the Zerodha API. ``fetcher`` must be a callable that
    takes an exchange string and returns ``list[dict]`` (the zerodha
    service's ``fetch_instruments`` method).

    Falls back to the local Instrument DB if no fetcher is provided (legacy
    path — works only when instruments are already seeded)."""

    nse_instruments: list[dict[str, Any]] = []
    bse_instruments: list[dict[str, Any]] = []
    nfo_instruments: list[dict[str, Any]] = []

    if fetcher is not None:
        # Fetch from live Zerodha API — this is the primary path on first connect
        for exchange, target in [("NSE", "nse"), ("BSE", "bse"), ("NFO", "nfo")]:
            try:
                data = await fetcher(exchange)
                if exchange == "NSE":
                    nse_instruments = data
                elif exchange == "BSE":
                    bse_instruments = data
                elif exchange == "NFO":
                    nfo_instruments = data
                logger.info(
                    "zerodha_defaults_fetched_csv",
                    extra={"exchange": exchange, "count": len(data)},
                )
            except Exception:
                logger.exception("zerodha_defaults_fetch_failed", extra={"exchange": exchange})
    else:
        # Legacy fallback — resolve from local Instrument collection
        logger.warning("zerodha_defaults_no_fetcher — falling back to local DB (may be empty)")
        return await _build_from_local_db()

    eq = _resolve_equities_from_csv(nse_instruments, NSE_TOP_EQUITIES)
    idx = _resolve_indices_from_csv(nse_instruments, bse_instruments, INDEX_SYMBOLS)

    opts: list[dict[str, Any]] = []
    for underlying, step, radius in OPTION_UNDERLYINGS:
        chunk = _resolve_option_chain_from_csv(nfo_instruments, underlying, radius, step)
        opts.extend(chunk)

    # De-dup by token
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for row in eq + idx + opts:
        t = row.get("token")
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(row)

    logger.info(
        "zerodha_defaults_built",
        extra={
            "equities": len(eq),
            "indices": len(idx),
            "options": len(opts),
            "total": len(out),
        },
    )
    return out


async def _build_from_local_db() -> list[dict[str, Any]]:
    """Legacy path: resolve from local Instrument collection. Only works when
    instruments have already been seeded/mirrored."""
    from app.models.instrument import Instrument

    def _to_dict(i: Instrument) -> dict[str, Any]:
        try:
            token_int = int(i.token)
        except (TypeError, ValueError):
            return {}
        instr_type = i.instrument_type.value if hasattr(i.instrument_type, "value") else str(i.instrument_type)
        exch = i.exchange.value if hasattr(i.exchange, "value") else str(i.exchange)
        expiry_str: str | None = None
        if i.expiry:
            expiry_str = i.expiry.isoformat() if hasattr(i.expiry, "isoformat") else str(i.expiry)
        strike_f: float | None = None
        if i.strike is not None:
            try:
                strike_f = float(str(i.strike))
            except (TypeError, ValueError):
                strike_f = None
        try:
            tick_f = float(str(i.tick_size))
        except (TypeError, ValueError):
            tick_f = 0.05
        return {
            "token": token_int,
            "symbol": i.symbol,
            "exchange": exch,
            "segment": i.segment,
            "name": i.name,
            "lotSize": int(i.lot_size or 1),
            "tickSize": tick_f,
            "expiry": expiry_str,
            "strike": strike_f,
            "instrumentType": instr_type,
        }

    # Equities
    eq_docs = await Instrument.find(
        {"symbol": {"$in": NSE_TOP_EQUITIES}, "segment": {"$in": ["NSE_EQUITY", "NSE", "BSE_EQUITY", "BSE"]}}
    ).to_list()
    eq = [r for d in eq_docs if (r := _to_dict(d))]

    # Indices
    idx_names = [n for n, _ in INDEX_SYMBOLS]
    idx_docs = await Instrument.find({"name": {"$in": idx_names}}).to_list()
    idx = [r for d in idx_docs if (r := _to_dict(d))]

    # Options
    today = datetime.now(timezone.utc).date()
    opts: list[dict[str, Any]] = []
    for underlying, step, radius in OPTION_UNDERLYINGS:
        docs = await Instrument.find(
            {
                "instrument_type": {"$in": ["CE", "PE"]},
                "$or": [
                    {"name": {"$regex": f"^{underlying}", "$options": "i"}},
                    {"trading_symbol": {"$regex": f"^{underlying}", "$options": "i"}},
                ],
                "expiry": {"$gte": today},
            }
        ).to_list()
        by_expiry: dict[date, list] = {}
        for d in docs:
            if d.expiry:
                by_expiry.setdefault(d.expiry, []).append(d)
        for exp in sorted(by_expiry.keys())[:2]:
            rows = by_expiry[exp]
            strikes = sorted({float(str(r.strike)) for r in rows if r.strike is not None})
            if not strikes:
                continue
            atm = strikes[len(strikes) // 2]
            lo, hi = atm - radius * step, atm + radius * step
            for r in rows:
                try:
                    k = float(str(r.strike))
                except (TypeError, ValueError):
                    continue
                if lo <= k <= hi:
                    row = _to_dict(r)
                    if row:
                        opts.append(row)

    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for row in eq + idx + opts:
        t = row.get("token")
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(row)
    return out
