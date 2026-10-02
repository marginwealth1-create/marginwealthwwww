"""Daily price band (upper / lower circuit) lookup.

Every Indian-exchange scrip trades inside a daily band. When price reaches an
edge the scrip is "circuit-locked", and the lock is DIRECTIONAL rather than a
halt:

  * at the UPPER circuit everybody wants to buy and nobody sells → only SELL
    can execute;
  * at the LOWER circuit everybody wants to sell and nobody buys → only BUY
    can execute.

The band %% depends on the scrip's surveillance category, which is NOT in the
instrument master — computing it as ``previous_close × pct`` is wrong on
exactly the scrips that matter (the ones actually hitting a circuit). So the
band is taken from the broker feed: Kite's ``quote()`` returns
``lower_circuit_limit`` / ``upper_circuit_limit``.

Design rules baked in here:

1. **Cached per instrument per DAY.** The band only changes at session start,
   and the order validator runs this on every order — a Kite REST call per
   order would be slow and rate-limited. Uses the existing Redis helpers
   (``cache_get`` / ``cache_set``); no new cache layer.
2. **Exchanges without a band return immediately.** Crypto / forex / metals
   come from 24×7 international feeds and have no circuit at all, so they are
   never even looked up.
3. **FAIL OPEN, always.** Quote raises, session dead, cache down, key wrong →
   ``(None, None)``. A missing band must NEVER block trading; fail-closed
   would mean one broker hiccup halts the whole platform.
4. **0 is normalised to None.** Feeds send 0 for "unknown", and ``price >= 0``
   is true for every price — treating a 0 ceiling as real would lock EVERY
   instrument at the upper circuit. This is the single nastiest bug in this
   area, hence :func:`_norm`.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from app.core.redis_client import cache_get, cache_set
from app.utils.time_utils import now_ist

logger = logging.getLogger(__name__)

# Exchanges that HAVE a daily price band. Anything else (CRYPTO, and the
# Infoway / Binance / MetaAPI symbol-style instruments for forex, metals,
# energy, global indices) is 24×7 / international and has no circuit.
BAND_EXCHANGES: frozenset[str] = frozenset({"NSE", "BSE", "NFO", "BFO", "MCX", "CDS"})

# The band is NOT fixed for the session: MCX (4%→6%→9%) and NSE (5%→10%→20%)
# EXPAND the band intraday once price reaches an edge. A 24 h cache pinned the
# narrow morning value all day, so the moment price legitimately traded past the
# old edge (band widened) the stale ceiling false-locked the scrip for the rest
# of the session (CRUDEOIL: cached upper ₹8266 vs live 8473 → BUY stuck). So the
# band is refreshed on a short TTL — long enough to shield the per-order path
# from a Kite call storm, short enough to pick up an expansion within ~a minute.
# The staleness guard in evaluate() is the second line of defence for the window
# between refreshes.
_TTL_SEC = 90
# A "no band" answer is cached for much less time so a transient feed outage
# (expired token at 08:00, WS reconnect) doesn't suppress the band for the
# rest of the day — but still shields the API from a per-order retry storm.
_TTL_EMPTY_SEC = 300

# How far ABOVE the upper (or below the lower) the live price may sit before the
# cached band is judged stale and ignored. A genuine lock pins price exactly at
# the edge (0% past it); this small slack only tolerates the final tick / decimal
# rounding, while comfortably catching a real expansion (edges jump whole %s).
_STALE_TOL = Decimal("0.005")  # 0.5%


def _norm(v: Any) -> Decimal | None:
    """Feed value → Decimal, or None when it carries no information.

    0 / negative / null / unparseable all collapse to None. Feeds use 0 to
    mean "unknown"; treating that as a real limit would make ``cur >= upper``
    true for every instrument and lock the entire platform at the upper
    circuit.
    """
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except Exception:
        return None
    return d if d > 0 else None


def _cache_key(token: str) -> str:
    # Day-scoped (IST): the band is re-published at session start.
    return f"circuit:{token}:{now_ist().date().isoformat()}"


async def get_circuit_band(instrument) -> tuple[Decimal | None, Decimal | None]:
    """Return ``(lower, upper)`` for an instrument; either may be None.

    NEVER raises. Any failure — unknown exchange, cold cache, dead Kite
    session, malformed payload — resolves to ``(None, None)`` so the caller
    treats it as "no band" and lets the order through.
    """
    try:
        exchange = str(getattr(instrument, "exchange", "") or "").upper()
        token = str(getattr(instrument, "token", "") or "")
        if not token or exchange not in BAND_EXCHANGES:
            return (None, None)

        key = _cache_key(token)
        try:
            cached = await cache_get(key)
        except Exception:
            cached = None  # cache down → fall through to a live fetch
        if isinstance(cached, dict):
            return (_norm(cached.get("lower")), _norm(cached.get("upper")))

        lower, upper = await _fetch_from_broker(exchange, instrument)

        try:
            await cache_set(
                key,
                {"lower": str(lower) if lower else None,
                 "upper": str(upper) if upper else None},
                ttl_sec=_TTL_SEC if (lower or upper) else _TTL_EMPTY_SEC,
            )
        except Exception:
            pass  # caching is best-effort; the band we just fetched is still good
        return (lower, upper)
    except Exception:  # pragma: no cover — belt-and-braces fail-open
        logger.debug("circuit_band_lookup_failed", exc_info=True)
        return (None, None)


async def _fetch_from_broker(
    exchange: str, instrument
) -> tuple[Decimal | None, Decimal | None]:
    """One Kite REST ``/quote`` read of the two circuit fields. Fails open."""
    symbol = (
        getattr(instrument, "symbol", None)
        or getattr(instrument, "trading_symbol", None)
        or ""
    )
    if not symbol:
        return (None, None)
    try:
        from app.services.zerodha_service import zerodha

        # get_quote() returns Kite's RAW payload — get_quote_snapshot()
        # normalises it and drops the circuit fields, so go direct.
        data = await zerodha.get_quote([f"{exchange}:{symbol}"])
    except Exception:
        # Not connected / token expired / network — no band, no block.
        return (None, None)

    snap = data.get(f"{exchange}:{symbol}") if isinstance(data, dict) else None
    if not isinstance(snap, dict):
        return (None, None)
    return (_norm(snap.get("lower_circuit_limit")), _norm(snap.get("upper_circuit_limit")))


def evaluate(
    *,
    action: str,
    order_type: str,
    lower: Decimal | None,
    upper: Decimal | None,
    cur: Decimal | None,
    ref_price: Decimal | None,
) -> tuple[str, str] | None:
    """Pure band check. Returns ``(error_code, message)`` or None when allowed.

    Callers MUST have already applied the exit exemption (reducing /
    square-off orders skip this entirely) — this function only knows about
    the band itself.

    Rule 1 — directional lock (based on the LIVE price):
        cur >= upper and BUY  → UPPER_CIRCUIT_BUY
        cur <= lower and SELL → LOWER_CIRCUIT_SELL
    Rule 2 — the order's own price sits outside the band:
        ref > upper → UPPER_CIRCUIT
        ref < lower → LOWER_CIRCUIT

    Every comparison is guarded on the limit being non-null AND the price
    being > 0. Outside market hours LTP is frequently 0, and ``0 <= lower``
    is true — an unguarded lower check would block every order the moment the
    feed goes quiet.

    The two rule families use DISTINCT codes on purpose: the UI renders a
    directional hint ("only SELL is allowed right now") differently from a
    price complaint ("your limit is above the ceiling") — same band, but a
    different fix for the user.
    """
    act = (action or "").upper()

    # Staleness guard — drop a band edge the live price has already broken past.
    # A REAL circuit is a pinned price: at the upper circuit the LTP sits AT the
    # ceiling and physically cannot trade above it, so `cur` can only ever equal
    # the upper (never exceed it). If `cur` is materially ABOVE the cached upper
    # (or BELOW the cached lower), the cache is STALE — the exchange widened the
    # band intraday and our value lagged — and locking on it would freeze a scrip
    # that is trading perfectly legally. Only a band the price is sitting at (or
    # just under, within tolerance for the last tick / rounding) is a live lock.
    if cur is not None and cur > 0:
        if upper is not None and cur > upper * (Decimal(1) + _STALE_TOL):
            upper = None  # price broke above → cached ceiling is stale, ignore it
        if lower is not None and cur < lower * (Decimal(1) - _STALE_TOL):
            lower = None  # price broke below → cached floor is stale, ignore it

    # Rule 1 — directional lock.
    if cur is not None and cur > 0:
        if upper is not None and act == "BUY" and cur >= upper:
            return (
                "UPPER_CIRCUIT_BUY",
                f"{_sym()}{upper} upper circuit hit — only SELL is allowed right now.",
            )
        if lower is not None and act == "SELL" and cur <= lower:
            return (
                "LOWER_CIRCUIT_SELL",
                f"{_sym()}{lower} lower circuit hit — only BUY is allowed right now.",
            )

    # Rule 2 — the order's own price is outside the band. Catches a LIMIT
    # parked beyond the band, which the exchange rejects at entry rather than
    # letting it sit pending forever.
    if ref_price is not None and ref_price > 0:
        if upper is not None and ref_price > upper:
            return (
                "UPPER_CIRCUIT",
                f"Price {_sym()}{ref_price} is above the upper circuit {_sym()}{upper}.",
            )
        if lower is not None and ref_price < lower:
            return (
                "LOWER_CIRCUIT",
                f"Price {_sym()}{ref_price} is below the lower circuit {_sym()}{lower}.",
            )

    _ = order_type  # accepted for symmetry with the caller; not used today
    return None


def _sym() -> str:
    return "₹"
