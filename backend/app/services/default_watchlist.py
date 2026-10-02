"""The instruments every user's marketwatch starts with.

Operator ask: a fresh account should open on a usable screen, not an
empty one — the six MCX commodities, NIFTY / BANKNIFTY, and the four
international spots below are added for every user, old and new.

Two things make this more than a hardcoded token list:

* **The Indian rows are contracts, not symbols.** "CRUDEOIL" resolves to
  the nearest UNEXPIRED monthly future, so the seed rolls with the market
  instead of pinning a contract that dies in three weeks. The regex is
  anchored on purpose: `CRUDEOIL26OCTFUT` matches, the mini / micro
  variants (`CRUDEOILM…`, `SILVERMIC…`, `ZINCMINI…`) do not.

* **A removal has to stick.** Every token seeded is remembered on the
  User (`default_watchlist_seeded`), so a user who deletes GOLD doesn't
  find it back on the next page load — while NEXT month's GOLD contract,
  being a token nobody has seen yet, still arrives on its own.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, time as _dtime

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from app.models._base import Exchange
from app.models.instrument import Instrument
from app.models.watchlist import Watchlist, WatchlistItem
from app.utils.time_utils import now_ist

logger = logging.getLogger(__name__)

# Indian futures — seeded at their nearest unexpired monthly contract.
DEFAULT_FUTURE_BASES: tuple[str, ...] = (
    "CRUDEOIL",
    "NATURALGAS",
    "GOLD",
    "SILVER",
    "COPPER",
    "ZINC",
    "NIFTY",
    "BANKNIFTY",
)
# International spots — one fixed row each, no expiry to chase.
DEFAULT_SPOT_SYMBOLS: tuple[str, ...] = ("XAUUSD", "XAGUSD", "BTCUSD", "ETHUSD")

# The resolution is identical for every user, so do it once per process
# per TTL instead of 12 queries on every marketwatch load.
_TTL_SEC = 600.0
_cache: tuple[float, list[Instrument]] = (0.0, [])

# How many seeded tokens to remember per user. Eight contracts roll per
# month, so this is a couple of years of history — enough that a rolled
# contract is never re-seeded, bounded so the doc can't grow forever.
_SEEDED_CAP = 200


def front_month_pattern(base: str) -> str:
    """BASE + 2-digit year + 3-letter month + FUT, anchored both ends.

    The anchoring is the whole point: a loose `^CRUDEOIL` also matches
    CRUDEOILM (the mini), `^SILVER` matches SILVERM / SILVERMIC /
    SILVER100, and `^ZINC` matches ZINCMINI — all different contracts
    with different lot sizes.
    """
    return f"^{base}\\d{{2}}[A-Z]{{3}}FUT$"


async def _front_month(base: str) -> Instrument | None:
    """Nearest monthly future for a base symbol that hasn't expired yet."""
    today = datetime.combine(now_ist().date(), _dtime.min)
    rows = (
        await Instrument.find(
            {
                "symbol": {"$regex": front_month_pattern(base)},
                "expiry": {"$gte": today},
                "is_active": True,
            }
        )
        .sort("+expiry")
        .limit(1)
        .to_list()
    )
    return rows[0] if rows else None


async def resolve_defaults(force: bool = False) -> list[Instrument]:
    """The default instruments as live Instrument docs. Cached for 10 min.

    Anything that can't be resolved (catalogue not mirrored yet, every
    contract expired) is simply left out — a missing row must never stop
    the other eleven from being seeded.
    """
    global _cache
    cached_at, cached = _cache
    if not force and cached and (time.monotonic() - cached_at) < _TTL_SEC:
        return cached

    out: list[Instrument] = []
    try:
        for base in DEFAULT_FUTURE_BASES:
            inst = await _front_month(base)
            if inst is not None:
                out.append(inst)
        for sym in DEFAULT_SPOT_SYMBOLS:
            inst = await Instrument.find_one(
                {"symbol": sym, "is_active": True}
            )
            if inst is not None:
                out.append(inst)
    except Exception:  # noqa: BLE001 — never break a marketwatch load
        logger.exception("default_watchlist_resolve_failed")
        return cached

    _cache = (time.monotonic(), out)
    return out


async def _default_watchlist(user_id: PydanticObjectId) -> Watchlist:
    """The user's favourites list, created on first use.

    Mirrors what `list_watchlists` does, minus the system `__seg_` rows.
    """
    wl = await Watchlist.find_one(
        Watchlist.user_id == user_id,
        {"name": {"$not": {"$regex": "^__seg_"}}},
    )
    if wl is not None:
        return wl
    wl = Watchlist(user_id=user_id, name="My Watchlist", sort_order=0, is_default=True)
    await wl.insert()
    return wl


async def _add(wl: Watchlist, inst: Instrument) -> bool:
    """Append one instrument to a watchlist. False when it was already in."""
    count = await WatchlistItem.find(WatchlistItem.watchlist_id == wl.id).count()
    try:
        await WatchlistItem(
            watchlist_id=wl.id,
            instrument_token=inst.token,
            symbol=inst.symbol,
            exchange=Exchange(inst.exchange),
            sort_order=count,
        ).insert()
    except DuplicateKeyError:
        return False
    return True


async def ensure_defaults(user) -> int:
    """Seed whatever this user is still missing. Returns the count added.

    The fast path — the usual one — costs no queries at all: the resolved
    tokens are already in memory and compared against the set stored on
    the user.
    """
    resolved = await resolve_defaults()
    if not resolved:
        return 0
    seeded: list[str] = list(getattr(user, "default_watchlist_seeded", None) or [])
    known = set(seeded)
    missing = [i for i in resolved if i.token not in known]
    if not missing:
        return 0

    from app.api.v1.user.marketwatch import (
        _ALLOWED_SEG_NAMES,
        _get_or_create_segment_watchlist,
        _zerodha_subscribe,
    )
    from app.services.netting_service import resolve_admin_row

    added = 0
    try:
        fav = await _default_watchlist(user.id)
        for inst in missing:
            if await _add(fav, inst):
                added += 1
            # The Indian chips (MCX FUT, Index Future, …) list only what
            # the user has added, so seed those too — otherwise the
            # instrument is in favourites but its own tab looks empty.
            row = resolve_admin_row(str(inst.segment), getattr(inst, "symbol", None))
            if row in _ALLOWED_SEG_NAMES:
                seg_wl = await _get_or_create_segment_watchlist(user.id, row)
                await _add(seg_wl, inst)
            # Ticks for a contract nobody had subscribed yet. No-op for the
            # Infoway / MetaAPI symbols, which stream on their own.
            await _zerodha_subscribe(inst.token, inst.symbol, str(inst.exchange))
            seeded.append(inst.token)

        user.default_watchlist_seeded = seeded[-_SEEDED_CAP:]
        await user.save()
    except Exception:  # noqa: BLE001 — a half-seeded list still renders
        logger.exception("default_watchlist_seed_failed", extra={"uid": str(user.id)})
    return added
