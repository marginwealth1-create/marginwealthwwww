"""Watchlist CRUD."""

from __future__ import annotations

import logging

from beanie import PydanticObjectId
from fastapi import APIRouter, HTTPException

from app.core.dependencies import CurrentUser
from app.core.redis_client import publish
from app.models._base import Exchange, InstrumentType
from app.models.watchlist import Watchlist, WatchlistItem
from app.schemas.common import APIResponse
from app.schemas.trading import WatchlistAddItem, WatchlistCreate
from app.services import instrument_service, market_data_service

logger = logging.getLogger(__name__)


async def _drop_items_beyond_expiry_cap(
    user_id: PydanticObjectId, items: list[WatchlistItem]
) -> list[WatchlistItem]:
    """Hide watchlist FUTURES whose expiry is beyond the user's "Show expiry
    month" window.

    Mirrors the instrument-search cap (instruments.py ``_cap_futures_by_expiry``)
    so that when the admin LOWERS the expiry setting the favourites list trims
    itself on the very next read — the operator's "expiry 2 month set kiya par
    pehle add kiye instruments favourites/add-list me dikhte rahe" bug. It is
    read-time only: nothing is deleted, so raising the setting back brings the
    rows straight back with no data loss.

    Only FUT is capped (option legs are added through the option-chain picker,
    which is already expiry-capped at add time). The allowed expiries are taken
    from the FULL futures calendar per underlying — NOT just the user's picks —
    so a lone far-month favourite is still correctly judged "beyond N".
    """
    if not items:
        return items
    from app.api.v1.user.option_chain import (
        _effective_max_expiries,
        _resolve_expiry_settings_for_user,
    )
    from app.models.instrument import Instrument
    from app.utils.time_utils import now_ist

    tokens = [it.instrument_token for it in items]
    insts = await Instrument.find({"token": {"$in": tokens}}).to_list()
    by_token = {i.token: i for i in insts}

    # Underlyings (keyed by spot token) that have a FUT favourite to cap.
    fut_unds: dict[str, tuple[str, str]] = {}  # underlying_token -> (exchange, root)
    for i in insts:
        if i.instrument_type == InstrumentType.FUT and i.expiry and i.underlying_token:
            fut_unds[i.underlying_token] = (str(i.exchange), i.name)
    if not fut_unds:
        return items

    settings = await _resolve_expiry_settings_for_user(user_id)
    today = now_ist().date()
    allowed: dict[str, set] = {}
    for ut, (exch, root) in fut_unds.items():
        n = max(1, int(_effective_max_expiries(settings, root, exch)))
        docs = await Instrument.find(
            {"underlying_token": ut, "instrument_type": InstrumentType.FUT.value}
        ).to_list()
        exps = sorted({d.expiry for d in docs if d.expiry and d.expiry >= today})
        allowed[ut] = set(exps[:n])  # nearest N distinct expiries

    out: list[WatchlistItem] = []
    for it in items:
        inst = by_token.get(it.instrument_token)
        if (
            inst is not None
            and inst.instrument_type == InstrumentType.FUT
            and inst.expiry
            and allowed.get(inst.underlying_token)  # non-empty calendar only
            and inst.expiry not in allowed[inst.underlying_token]
        ):
            continue  # FUT beyond the per-underlying expiry cap → hide
        out.append(it)
    return out


async def _drop_expired_items(items: list[WatchlistItem]) -> list[WatchlistItem]:
    """Hide every watchlist row whose contract has already expired / been
    delisted — read-time safety net over the hourly `expiry_cleanup` sweep.

    Operator report: "july mcx ka crude expire ho gaya hai fir bhi dikh raha
    hai". The sweep alone can miss a row (Instrument doc absent, `expiry`
    never populated, or a month-end guess that outlives a mid-month MCX
    contract), and until the next sweep the user kept seeing a dead contract
    with a frozen price. `dead_tokens` resolves doc expiry → Kite catalog →
    symbol, so anything not provably live disappears on the very next read.
    Never deletes here — the sweep owns removal, this only hides.
    """
    if not items:
        return items
    try:
        from app.services.expiry_cleanup import dead_tokens

        dead = await dead_tokens(
            [(it.instrument_token, it.symbol, str(it.exchange)) for it in items]
        )
    except Exception:  # noqa: BLE001 — a resolver hiccup must not empty the list
        logger.warning("watchlist_expiry_filter_failed")
        return items
    if not dead:
        return items
    return [it for it in items if it.instrument_token not in dead]


async def _instrument_meta(tokens: list[str]) -> dict[str, dict]:
    """Contract metadata for watchlist rows — keyed by token.

    Favourites used to come back as bare {id, token, symbol, exchange}, so the
    client had no expiry to render: the "26-JUN-2026" line that shows while
    SEARCHING vanished the moment the same contract was added to a watchlist
    or a segment chip (operator-flagged — a trader can't tell which month's
    CRUDEOIL they're holding). Resolved from the Instrument doc, falling back
    to the Kite catalog for tokens that were never mirrored (the catalog date
    is exact — a symbol parse would only give a month-end guess).
    """
    tokens = [str(t) for t in tokens if t]
    if not tokens:
        return {}
    from app.models.instrument import Instrument

    out: dict[str, dict] = {}
    try:
        docs = await Instrument.find({"token": {"$in": list(set(tokens))}}).to_list()
    except Exception:  # noqa: BLE001
        logger.warning("watchlist_instrument_meta_failed")
        return {}
    for i in docs:
        it_val = (
            i.instrument_type.value
            if hasattr(i.instrument_type, "value")
            else str(i.instrument_type)
        )
        out[i.token] = {
            "expiry": str(i.expiry) if i.expiry else None,
            "instrument_type": it_val,
            "segment": str(i.segment),
            "name": i.name,
            "lot_size": i.lot_size,
            "strike": str(i.strike) if i.strike else None,
        }

    missing = [t for t in set(tokens) if t not in out]
    if missing:
        try:
            from app.services.expiry_cleanup import catalog_index

            idx, _warm = await catalog_index()
            for t in missing:
                exp = idx.get(t)
                if exp is not None:
                    out[t] = {"expiry": str(exp)}
        except Exception:  # noqa: BLE001
            pass
    return out


async def _assert_live_contract(inst) -> None:
    """Reject adding an already-expired contract. The Kite catalog is cached
    for up to 4 days, so a stale search hit could otherwise re-add exactly
    what the expiry sweep just removed."""
    try:
        from app.services.expiry_cleanup import dead_tokens

        dead = await dead_tokens([(inst.token, inst.symbol, str(inst.exchange))])
    except Exception:  # noqa: BLE001 — never block an add on a resolver hiccup
        return
    if inst.token in dead:
        raise HTTPException(
            status_code=400, detail=f"{inst.symbol} has expired and can't be added"
        )


async def _notify_marketwatch_changed(
    user_id: PydanticObjectId, action: str, payload: dict | None = None,
) -> None:
    """Fan a `marketwatch` event to every open WS session of this user so
    the other client (apk / web / mobile-web) invalidates its watchlist
    cache instantly — no waiting for the next REST poll. Best-effort: a
    Redis hiccup never rolls back the DB write that just succeeded.
    """
    try:
        await publish(
            f"user:{user_id}:marketwatch",
            {"type": "marketwatch", "payload": {"action": action, **(payload or {})}},
        )
    except Exception:  # noqa: BLE001 — best-effort
        logger.warning("watchlist_publish_failed", extra={"user_id": str(user_id)})


async def _zerodha_subscribe(token: str, symbol: str, exchange: str) -> None:
    """Best-effort: subscribe one instrument to the live Zerodha ticker. Only
    runs for numeric Kite tokens (Indian segments); skips Infoway-mirrored
    forex/crypto/metal tokens which are handled separately."""
    try:
        token_int = int(token)
    except (TypeError, ValueError):
        return  # Infoway / synthetic token — Zerodha doesn't know it
    try:
        from app.services.zerodha_service import zerodha
        await zerodha.subscribe_tokens_on_demand(
            [token_int],
            symbols={token_int: {"symbol": symbol, "exchange": exchange}},
        )
    except Exception:
        logger.warning("watchlist_zerodha_subscribe_failed", extra={"token": token})


async def _zerodha_unsubscribe_if_orphan(token: str) -> None:
    """Unsubscribe from Zerodha only if NO user has this instrument in any
    watchlist anymore — saves WS slots without breaking other traders."""
    try:
        token_int = int(token)
    except (TypeError, ValueError):
        return
    still_used = await WatchlistItem.find_one(WatchlistItem.instrument_token == token)
    if still_used is not None:
        return  # someone else still wants ticks for this instrument
    try:
        from app.services.zerodha_service import zerodha
        await zerodha.unsubscribe_tokens_on_demand([token_int])
    except Exception:
        logger.warning("watchlist_zerodha_unsubscribe_failed", extra={"token": token})

router = APIRouter(prefix="/marketwatch", tags=["user-marketwatch"])

MAX_WATCHLISTS = 10


# ── Per-segment managed instrument lists ──────────────────────────────
# Indian-segment chips (NSE EQ / NSE FUT / NSE OPT / BSE * / MCX *) are
# user-managed: instead of showing every Kite-cached instrument under the
# chip, the panel only shows what THIS user has explicitly added. We reuse
# the Watchlist model with a reserved name convention ``__seg_<NAME>`` so
# the regular favourites watchlist list isn't polluted with system rows.
_SEG_WL_PREFIX = "__seg_"

# Whitelist of admin-row names that can have a managed list. Keeps a user
# from creating an arbitrary watchlist under any string.
_ALLOWED_SEG_NAMES = frozenset(
    {
        "NSE_EQ", "NSE_FUT", "NSE_OPT",
        # NSE F&O split rows — accept the granular names too so a watchlist
        # chip / add call using the new stock-vs-index segment codes isn't
        # rejected with "Unsupported segment". The legacy NSE_FUT / NSE_OPT
        # keys stay for storage-key stability of existing managed lists.
        "NSE_STK_FUT", "NSE_IDX_FUT", "NSE_STK_OPT", "NSE_IDX_OPT",
        "BSE_EQ", "BSE_FUT", "BSE_OPT",
        "MCX_FUT", "MCX_OPT",
    }
)


async def _get_or_create_segment_watchlist(
    user_id: PydanticObjectId, segment_name: str
) -> Watchlist:
    """Auto-create the system watchlist for this user×segment. Idempotent."""
    name = _SEG_WL_PREFIX + segment_name
    wl = await Watchlist.find_one(
        Watchlist.user_id == user_id, Watchlist.name == name
    )
    if wl is not None:
        return wl
    wl = Watchlist(user_id=user_id, name=name, sort_order=999, is_default=False)
    await wl.insert()
    return wl


@router.get("/segment/{segment_name}/items", response_model=APIResponse[list])
async def list_segment_items(segment_name: str, user: CurrentUser):
    """Return only the instruments THIS user has explicitly added under
    the given Indian-segment chip (NSE_EQ, MCX_OPT, etc.). Empty list on
    first access — the user adds items via the search-and-add flow.

    Block-aware: when the admin has paused the segment, returns an empty
    list so the favourite tile renders empty + the chip itself is hidden
    by the InstrumentsPanel filter. Also auto-hides any stored items
    whose instrument's CURRENT segment is in the inactive set (covers
    cross-segment items added under a different bucket).
    """
    seg = segment_name.upper()
    if seg not in _ALLOWED_SEG_NAMES:
        raise HTTPException(status_code=400, detail=f"Unsupported segment: {segment_name}")

    # If the bucket itself is paused, return nothing — same effect as
    # the search filter hiding the chip. User-scoped so sub-admin pool
    # blocks reach their members.
    from app.services.netting_service import inactive_admin_rows, resolve_admin_row

    inactive_admin = await inactive_admin_rows(user_id=user.id)
    if seg in inactive_admin:
        return APIResponse(data=[])

    wl = await _get_or_create_segment_watchlist(user.id, seg)
    items = (
        await WatchlistItem.find(WatchlistItem.watchlist_id == wl.id)
        .sort("sort_order")
        .to_list()
    )

    # Cross-segment hide — SYMBOL-AWARE. Drop stored items whose instrument's
    # resolved admin row is paused. Must NOT use the raw
    # `inactive_instrument_segments` set: it reverse-maps the WHOLE NFO_OPTION
    # segment to the stock row, so it hid index options (BANKNIFTY / NIFTY)
    # the moment Stock Options were blocked.
    if inactive_admin:
        from app.models.instrument import Instrument

        tokens = [it.instrument_token for it in items]
        token_row: dict[str, str] = {}
        if tokens:
            insts = await Instrument.find({"token": {"$in": tokens}}).to_list()
            for inst in insts:
                token_row[inst.token] = resolve_admin_row(
                    str(inst.segment), getattr(inst, "symbol", None)
                )
        items = [
            it
            for it in items
            if token_row.get(it.instrument_token, "") not in inactive_admin
        ]

    # Expired / delisted contracts never show, in any segment chip.
    items = await _drop_expired_items(items)

    # Expiry cap — hide FUT favourites beyond the user's "Show expiry month"
    # window so lowering the admin setting trims the list immediately.
    items = await _drop_items_beyond_expiry_cap(user.id, items)

    meta = await _instrument_meta([it.instrument_token for it in items])
    return APIResponse(
        data=[
            {
                "id": str(it.id),
                "instrument_token": it.instrument_token,
                "symbol": it.symbol,
                "exchange": str(it.exchange),
                # Contract metadata so the chip row can render the expiry
                # under the symbol, exactly like the search results do.
                **meta.get(it.instrument_token, {}),
            }
            for it in items
        ]
    )


@router.post("/segment/{segment_name}/items", response_model=APIResponse[dict])
async def add_segment_item(
    segment_name: str, payload: WatchlistAddItem, user: CurrentUser
):
    seg = segment_name.upper()
    if seg not in _ALLOWED_SEG_NAMES:
        raise HTTPException(status_code=400, detail=f"Unsupported segment: {segment_name}")
    wl = await _get_or_create_segment_watchlist(user.id, seg)
    inst = await instrument_service.get_by_token(payload.token)
    await _assert_live_contract(inst)
    existing = await WatchlistItem.find_one(
        WatchlistItem.watchlist_id == wl.id,
        WatchlistItem.instrument_token == inst.token,
    )
    if existing is not None:
        return APIResponse(data={"id": str(existing.id), "duplicate": True})
    count = await WatchlistItem.find(WatchlistItem.watchlist_id == wl.id).count()
    item = WatchlistItem(
        watchlist_id=wl.id,
        instrument_token=inst.token,
        symbol=inst.symbol,
        exchange=Exchange(inst.exchange),
        sort_order=count,
    )
    await item.insert()
    await _zerodha_subscribe(inst.token, inst.symbol, str(inst.exchange))
    await _notify_marketwatch_changed(
        user.id, "segment_add", {"segment": seg, "token": inst.token},
    )
    return APIResponse(data={"id": str(item.id)})


@router.delete(
    "/segment/{segment_name}/items/{token}", response_model=APIResponse[dict]
)
async def remove_segment_item(segment_name: str, token: str, user: CurrentUser):
    seg = segment_name.upper()
    if seg not in _ALLOWED_SEG_NAMES:
        raise HTTPException(status_code=400, detail=f"Unsupported segment: {segment_name}")
    wl = await _get_or_create_segment_watchlist(user.id, seg)
    item = await WatchlistItem.find_one(
        WatchlistItem.watchlist_id == wl.id,
        WatchlistItem.instrument_token == token,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    await item.delete()
    await _zerodha_unsubscribe_if_orphan(token)
    await _notify_marketwatch_changed(
        user.id, "segment_remove", {"segment": seg, "token": token},
    )
    return APIResponse(data={"ok": True})


@router.get("", response_model=APIResponse[list])
async def list_watchlists(user: CurrentUser):
    # Top up the operator's default instruments first, so a new account
    # (or a rolled monthly contract) shows up in this very response.
    from app.services.default_watchlist import ensure_defaults

    await ensure_defaults(user)
    # Filter out system segment watchlists — they're served by the
    # /segment/* endpoints above and shouldn't pollute the regular
    # favourites list rendering.
    wls = (
        await Watchlist.find(
            Watchlist.user_id == user.id,
            {"name": {"$not": {"$regex": f"^{_SEG_WL_PREFIX}"}}},
        )
        .sort("sort_order", "name")
        .to_list()
    )
    if not wls:
        wl = Watchlist(user_id=user.id, name="My Watchlist", sort_order=0, is_default=True)
        await wl.insert()
        wls = [wl]
    out = []
    per_wl: list[tuple] = []
    for wl in wls:
        items = await WatchlistItem.find(WatchlistItem.watchlist_id == wl.id).sort("sort_order").to_list()
        # Expired / delisted contracts are never returned to the client.
        items = await _drop_expired_items(items)
        per_wl.append((wl, items))

    # One metadata lookup for every watchlist's tokens (expiry / type / lot),
    # so favourites render the same expiry line the search results do.
    meta = await _instrument_meta(
        [it.instrument_token for _wl, items in per_wl for it in items]
    )
    for wl, items in per_wl:
        out.append(
            {
                "id": str(wl.id),
                "name": wl.name,
                "sort_order": wl.sort_order,
                "is_default": wl.is_default,
                "items": [
                    {
                        "id": str(it.id),
                        "instrument_token": it.instrument_token,
                        "symbol": it.symbol,
                        "exchange": str(it.exchange),
                        **meta.get(it.instrument_token, {}),
                    }
                    for it in items
                ],
            }
        )
    return APIResponse(data=out)


@router.post("", response_model=APIResponse[dict])
async def create(payload: WatchlistCreate, user: CurrentUser):
    count = await Watchlist.find(Watchlist.user_id == user.id).count()
    if count >= MAX_WATCHLISTS:
        raise HTTPException(status_code=400, detail=f"Limit of {MAX_WATCHLISTS} watchlists reached")
    wl = Watchlist(user_id=user.id, name=payload.name.strip(), sort_order=count)
    await wl.insert()
    return APIResponse(data={"id": str(wl.id), "name": wl.name})


@router.delete("/{watchlist_id}", response_model=APIResponse[dict])
async def delete(watchlist_id: str, user: CurrentUser):
    wl = await Watchlist.get(PydanticObjectId(watchlist_id))
    if wl is None or wl.user_id != user.id:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    items = await WatchlistItem.find(WatchlistItem.watchlist_id == wl.id).to_list()
    tokens = [it.instrument_token for it in items]
    await WatchlistItem.find(WatchlistItem.watchlist_id == wl.id).delete()
    await wl.delete()
    # Try to free WS slots for every removed instrument that nobody else holds.
    for tok in tokens:
        await _zerodha_unsubscribe_if_orphan(tok)
    return APIResponse(data={"ok": True})


@router.post("/{watchlist_id}/items", response_model=APIResponse[dict])
async def add_item(watchlist_id: str, payload: WatchlistAddItem, user: CurrentUser):
    wl = await Watchlist.get(PydanticObjectId(watchlist_id))
    if wl is None or wl.user_id != user.id:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    inst = await instrument_service.get_by_token(payload.token)
    await _assert_live_contract(inst)
    existing = await WatchlistItem.find_one(
        WatchlistItem.watchlist_id == wl.id, WatchlistItem.instrument_token == inst.token
    )
    if existing is not None:
        return APIResponse(data={"id": str(existing.id), "duplicate": True})
    count = await WatchlistItem.find(WatchlistItem.watchlist_id == wl.id).count()
    item = WatchlistItem(
        watchlist_id=wl.id,
        instrument_token=inst.token,
        symbol=inst.symbol,
        exchange=Exchange(inst.exchange),
        sort_order=count,
    )
    await item.insert()
    # On-demand Zerodha subscribe — fire ticks for this instrument now that
    # someone wants them. No-op for Infoway-quoted symbols.
    await _zerodha_subscribe(inst.token, inst.symbol, str(inst.exchange))
    await _notify_marketwatch_changed(
        user.id, "add", {"watchlist_id": str(wl.id), "token": inst.token},
    )
    return APIResponse(data={"id": str(item.id)})


@router.delete("/{watchlist_id}/items/{item_id}", response_model=APIResponse[dict])
async def remove_item(watchlist_id: str, item_id: str, user: CurrentUser):
    wl = await Watchlist.get(PydanticObjectId(watchlist_id))
    if wl is None or wl.user_id != user.id:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    # `item_id` is normally the WatchlistItem ObjectId, but some clients send
    # the instrument token instead (e.g. "13145346"). Wrapping a non-ObjectId
    # in PydanticObjectId() used to raise bson.InvalidId → unhandled 500. Parse
    # as ObjectId only when valid, otherwise (or if that misses) fall back to a
    # (watchlist, token) lookup so a bad path param returns a clean 404.
    item = None
    if PydanticObjectId.is_valid(item_id):
        item = await WatchlistItem.get(PydanticObjectId(item_id))
    if item is None:
        item = await WatchlistItem.find_one(
            WatchlistItem.watchlist_id == wl.id,
            WatchlistItem.instrument_token == item_id,
        )
    if item is None or item.watchlist_id != wl.id:
        raise HTTPException(status_code=404, detail="Item not found")
    token = item.instrument_token
    await item.delete()
    # If no other user still has this instrument in any watchlist, free up
    # the Zerodha WS slot.
    await _zerodha_unsubscribe_if_orphan(token)
    await _notify_marketwatch_changed(
        user.id, "remove", {"watchlist_id": str(wl.id), "token": token},
    )
    return APIResponse(data={"ok": True})


@router.get("/{watchlist_id}/quotes", response_model=APIResponse[list])
async def quotes(watchlist_id: str, user: CurrentUser):
    wl = await Watchlist.get(PydanticObjectId(watchlist_id))
    if wl is None or wl.user_id != user.id:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    items = await WatchlistItem.find(WatchlistItem.watchlist_id == wl.id).to_list()

    # Per-symbol block — hide watchlist rows whose symbol has been
    # disabled for this user by an admin / broker / user-level
    # override. Matches the same filter applied on search + option
    # chain so a blocked symbol disappears from EVERY browse surface,
    # not just one.
    from app.services.netting_service import (
        get_user_blocked_symbols,
        is_symbol_blocked_for,
    )

    blocked = await get_user_blocked_symbols(user.id)
    items = [it for it in items if not is_symbol_blocked_for(it.symbol or "", blocked)]

    # …and expired / delisted contracts, so no dead row ever gets a quote row
    # (they'd render with a frozen last price).
    items = await _drop_expired_items(items)

    quotes = await market_data_service.get_quotes([it.instrument_token for it in items])
    meta = await _instrument_meta([it.instrument_token for it in items])
    return APIResponse(
        data=[
            {
                "instrument_token": it.instrument_token,
                "symbol": it.symbol,
                "exchange": str(it.exchange),
                # Expiry / lot metadata rides along so a favourite row shows
                # WHICH contract it is, not just the symbol + price.
                **meta.get(it.instrument_token, {}),
                **q,
            }
            for it, q in zip(items, quotes)
        ]
    )
