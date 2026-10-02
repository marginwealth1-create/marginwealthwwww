"""User positions + holdings endpoints."""

from __future__ import annotations

import asyncio
from typing import Any

from beanie import PydanticObjectId
from fastapi import APIRouter, HTTPException, Query, Request

from app.core.dependencies import CurrentUser
from app.models._base import OrderAction, OrderType, ProductType
from app.models.audit_log import AuditAction
from app.models.position import Position, PositionStatus
from app.models.trade import Trade
from app.schemas.common import APIResponse
from app.schemas.trading import HoldingOut, PositionOut
from app.services import audit_service, market_data_service, netting_service, order_service, order_validator, position_service
from app.utils.decimal_utils import to_decimal

router = APIRouter(prefix="/positions", tags=["user-positions"])


def _recompute_overnight_holding(product_type, mode: str | None) -> bool:
    """Should the carry-forward ("Holding" / CF Required) margin be recomputed
    from the OVERNIGHT settings instead of just equalling the locked used margin?

    Yes for both MIS and NRML; no for CNC.
      • Every position now locks the INTRADAY margin at entry (700× in Times
        mode) and KEEPS it through the overnight carry — the EOD MIS→NRML flip
        is margin-neutral (see convert_intraday_to_carry). So USED = intraday
        lock for MIS and NRML alike, and the carry (CF Required) tile must be
        recomputed from the OVERNIGHT settings for BOTH to surface the true
        overnight requirement (operator config: NSE_EQ intraday 700× /
        overnight 70×, so a lot locked at ~700× needs ~70× — 10× more — to
        carry). Returning `margin_used` for NRML would reprint the intraday
        lock and hide the CF gate the owner asked for.
      • USED (intraday, small) ≤ CF (overnight, large) always in this model, so
        the old "Used > CF" all-NRML artefact cannot reappear.

    When the admin set overnight == intraday the recompute simply returns the
    same number, so this is always safe."""
    try:
        pt = product_type.value if hasattr(product_type, "value") else str(product_type)
    except Exception:
        pt = str(product_type or "")
    return pt.upper() in {"MIS", "NRML"}


def _opt_type_from_symbol(symbol: str | None) -> str | None:
    """Derive option type (CE/PE) from a trading symbol suffix.

    The netting resolver only applies the admin's per-side option overrides
    (Opt Buy / Opt Sell mode + Fixed ₹-per-lot / % values) when it KNOWS the
    leg is an option — i.e. when `option_type` ∈ {"CE","PE"}. The Position's
    `InstrumentRef` snapshot carries no option_type field, so we recover it
    from the symbol the same way the matching engine does. Passing this into
    `get_effective_settings` makes the Used/Holding margin on the positions
    page honour the Opt Sell/Buy Fixed setting instead of silently falling
    back to the generic segment Times/% (the ₹795.80-vs-Fixed-15000 bug).
    """
    s = (symbol or "").upper()
    # Option symbols end in the strike + CE/PE, so the char right before the
    # CE/PE suffix is always a digit (…24000PE). This guards against equities
    # that merely END in those letters (e.g. RELIAN-CE, persistent-PE) being
    # misread as options.
    if len(s) >= 3 and s[-3].isdigit():
        if s.endswith("CE"):
            return "CE"
        if s.endswith("PE"):
            return "PE"
    return None


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def _is_segment_market_open_now(segment_type: str | None) -> bool:
    """Server-side mirror of the apk's `isInstrumentMarketOpen`. Reject
    user-initiated squareoff calls when the segment's market is closed
    — bypasses are only available via admin force-close. Crypto + Forex
    always return True (24/7 / 24×5 segments).
    """
    from datetime import datetime as _dt
    from app.utils.time_utils import now_ist

    seg = (segment_type or "").upper()
    if "CRYPTO" in seg:
        return True
    if (
        seg == "FOREX"
        or seg == "STOCKS"
        or seg == "INDICES"
        or seg == "COMMODITIES"
        or "FOREX" in seg
        or seg.startswith("CDS")
    ):
        now: _dt = now_ist()
        wd = now.weekday()  # Mon=0 … Sun=6
        if wd == 5:  # Saturday
            return False
        if wd == 6 and now.hour < 21:  # Sunday before 21:00 IST
            return False
        return True
    now2 = now_ist()
    wd2 = now2.weekday()
    if wd2 >= 5:  # Weekend
        return False
    mins = now2.hour * 60 + now2.minute
    if seg.startswith("MCX"):
        return 9 * 60 <= mins <= 23 * 60 + 30
    # NSE / BSE equity + F&O fallback
    return 9 * 60 + 15 <= mins <= 15 * 60 + 30


def _segment_market_label(segment_type: str | None) -> str:
    seg = (segment_type or "").upper()
    if "CRYPTO" in seg:
        return "Crypto"
    if seg == "FOREX" or "FOREX" in seg or seg.startswith("CDS"):
        return "Forex"
    if seg == "COMMODITIES":
        return "Commodities"
    if seg == "STOCKS":
        return "Global stocks"
    if seg == "INDICES":
        return "Global indices"
    if seg.startswith("MCX"):
        return "MCX"
    if seg.startswith("BSE"):
        return "BSE"
    return "NSE"


def _parse_position_id(position_id: str) -> PydanticObjectId:
    """Convert the URL path param into a Mongo ObjectId, raising a clean
    HTTP 404 if it isn't a valid 24-char hex id.

    Without this guard, the frontend's optimistic synthetic IDs
    (`optimistic_<ts>`) would bubble `bson.errors.InvalidId` out of the
    route handler as a 500 — and 500s skip CORS headers, which makes the
    browser show a misleading "CORS blocked" error in the console
    (real issue: 500 from the backend). 404 lets the frontend handle it
    cleanly.
    """
    try:
        return PydanticObjectId(position_id)
    except Exception:  # bson.errors.InvalidId
        raise HTTPException(status_code=404, detail="Position not found")


def _effective_qty(p: Position) -> tuple[float, float, int]:
    """Resolve (qty_in_contracts, lots, lot_size) from a Position row.

    The stored ``p.quantity`` is the canonical contract count written at
    fill time — `order_service.place_order` resolves the lot size from
    Zerodha's CSV (NSE/BSE F&O) or the MCX_LOT_SIZES table (MCX) and
    multiplies before persisting. Trust that here; do not re-derive
    from a hardcoded table that may disagree with the exchange's
    current revision.

    The stored ``p.instrument.lot_size`` is the snapshot taken at fill
    time. For MTM display we keep it as the displayed `lot_size` /
    `lots` denominator so legacy positions opened before a lot revision
    still report their original ratio.
    """
    stored_lot = int(getattr(p.instrument, "lot_size", 0) or 1) or 1
    qty = float(p.quantity)
    lots = qty / stored_lot if stored_lot > 0 else qty
    return qty, lots, stored_lot


def _pos(p: Position, expiry: str | None = None, carry_enabled: bool = False) -> dict:
    """Position view.

    ``expiry`` is the instrument's REAL contract-expiry date (ISO ``YYYY-MM-DD``),
    resolved by the caller from the Instrument collection and threaded in so the
    UI shows the true expiry (e.g. SILVER26SEPFUT → 2026-09-04) instead of
    guessing it from the symbol (which encodes the YEAR, not the day, for
    monthly contracts — the old symbol-parse showed "26 SEP" for a 4-SEP future).

    For USD-quoted instruments (crypto / forex) the live feed quotes in
    USD, so we keep ``avg_price`` and ``ltp`` in dollars — the UI renders
    them with a ``$`` prefix based on ``currency_quote``. Only realised
    and unrealised P&L (and margin used) are converted to INR, since
    those flow into the user's rupee wallet.
    """
    avg_native = float(str(p.avg_price))
    ltp_native = float(str(p.ltp))
    realized = float(str(p.realized_pnl))
    margin = float(str(p.margin_used))

    is_usd = market_data_service.is_usd_quoted_segment(p.segment_type) or \
        market_data_service.is_usd_quoted_segment(p.instrument.segment)
    current_rate = market_data_service.get_usd_inr_rate() if is_usd else 1.0
    open_rate = (
        float(str(p.open_usd_inr_rate))
        if (is_usd and p.open_usd_inr_rate is not None)
        else current_rate
    )

    # Canonical-lot self-heal: legacy positions opened before the canonical
    # lot tables existed got stored with `quantity = lots × stored_lot` where
    # `stored_lot` was 1 (auto-created from a half-warm Zerodha CSV cache).
    # The frontend already self-heals via `resolveQty` using the canonical
    # NIFTY=75 / BANKNIFTY=35 / SENSEX=20 etc tables, so the row shows the
    # right size and P/L. The header total — which sums `unrealized_pnl`
    # straight from this serializer — was the only place still using the
    # broken stored qty, producing a 75× understatement. Apply the same
    # canonical resolution here so the header agrees with the rows.
    effective_qty, lots_value, effective_lot = _effective_qty(p)

    # Stale-feed guard: if the LTP feed flatlined (Zerodha WS dropped,
    # Infoway timeout, REST fallback returning 0) we MUST NOT compute
    # M2M against a zero price.  `(0 - 8631) × 300 = -25,90,007` is
    # what dashboards rendered before this guard — pure phantom loss.
    # When the price is unusable, surface unrealized = 0 so the card
    # reads "₹0.00 · LTP 0.00" and the trader knows the feed is dead
    # rather than panicking at a ₹-25 lakh M2M.
    _stale_feed = ltp_native is None or ltp_native <= 0
    if is_usd:
        unrealized_pnl_inr = (
            0
            if _stale_feed
            else (ltp_native - avg_native) * effective_qty * current_rate
        )
        realized_pnl_inr = realized * open_rate
        # margin_used is already stored as the wallet-currency number that
        # was actually locked at order time (validator computes it in INR via
        # block_margin), so we DON'T re-multiply by FX rate here. Otherwise
        # this view would disagree with wallet.used_margin by ~80×.
        margin_inr = margin
    else:
        unrealized_pnl_inr = (
            0
            if _stale_feed
            else (ltp_native - avg_native) * effective_qty
        )
        realized_pnl_inr = realized
        margin_inr = margin

    # Lot size echoed back so the UI can show "Long 2 lots (150 qty)" style
    # labels without re-fetching the instrument. Prefer the canonical lot
    # so the UI shows the same value the math above used.
    pos_lot_size = effective_lot

    # Peak |qty| recorded by apply_fill — preserved across full close so
    # the Closed/History tab can show the size the user actually held
    # (where ``quantity`` has been zeroed). For OPEN rows the current
    # signed `quantity` is the source of truth.
    opening_qty_raw = getattr(p, "opening_quantity", None)
    opening_qty = float(opening_qty_raw) if opening_qty_raw is not None else abs(effective_qty)

    return {
        "id": str(p.id),
        "user_id": str(p.user_id),
        "symbol": p.instrument.symbol,
        "trading_symbol": getattr(p.instrument, "trading_symbol", None),
        "exchange": str(p.instrument.exchange),
        "instrument_token": p.instrument.token,
        # Real contract expiry (ISO date) from the Instrument collection — the
        # UI prefers this over parsing the symbol so monthly futures show the
        # true expiry day, not the year read as a day.
        "expiry": expiry,
        # Per-position Carry-Forward toggle. Shown only when the pool's admin
        # enabled the feature AND the segment actually has an EOD rollover —
        # 24-hour forex / commodity / crypto never square off at EOD, so the
        # toggle would be a no-op there and is hidden.
        "carry_forward_enabled": bool(carry_enabled) and _segment_carryable(p.segment_type),
        "user_carry_forward": bool(getattr(p, "user_carry_forward", False)),
        "segment_type": p.segment_type,
        "product_type": p.product_type.value,
        # Quantity reported in CONTRACTS (the number the exchange would
        # see), not lots. For legacy positions where the stored quantity
        # was lots × stale lot_size, the canonical resolution above turns
        # it into the right contracts count so this matches what the
        # frontend's `resolveQty` derives.
        "quantity": effective_qty,
        "opening_quantity": opening_qty,
        "lot_size": pos_lot_size,
        "lots": lots_value,
        # Prices in source currency — UI prefixes $ when currency_quote=USD.
        "avg_price": f"{avg_native:.4f}" if is_usd else f"{avg_native:.2f}",
        "ltp": f"{ltp_native:.4f}" if is_usd else f"{ltp_native:.2f}",
        # P&L + margin always in INR — that's the wallet currency.
        "realized_pnl": f"{realized_pnl_inr:.2f}",
        "unrealized_pnl": f"{unrealized_pnl_inr:.2f}",
        "margin_used": f"{margin_inr:.2f}",
        # FX context so the UI can show e.g. "USD/INR @ 83.21" next to the row
        "currency_quote": "USD" if is_usd else "INR",
        "open_usd_inr_rate": f"{open_rate:.4f}" if is_usd else None,
        "current_usd_inr_rate": f"{current_rate:.4f}" if is_usd else None,
        "stop_loss": str(p.stop_loss) if p.stop_loss is not None else None,
        "target": str(p.target) if p.target is not None else None,
        # Snapshot of SL/TP captured at close-time. apply_fill wipes
        # `stop_loss` / `target` on full close so they don't leak into
        # reopens, but the user-facing Closed tab wants to surface
        # "Trade had SL ₹X, TP ₹Y" — these copies hold that info.
        "close_stop_loss": str(p.close_stop_loss) if getattr(p, "close_stop_loss", None) is not None else None,
        "close_target": str(p.close_target) if getattr(p, "close_target", None) is not None else None,
        "status": p.status.value,
        "opened_at": p.opened_at.isoformat() if p.opened_at else None,
        "closed_at": p.closed_at.isoformat() if p.closed_at else None,
        # Compact tag — see Position.close_reason for the legal set.
        "close_reason": p.close_reason,
        # Original direction the user took. Stays "BUY" / "SELL" even after
        # a full close flattens `quantity` to 0 — the Closed-tab card uses
        # this so a closed long doesn't get mis-rendered as a short.
        # Falls back to inferring from `quantity` for legacy rows written
        # before this field existed.
        "opened_side": (
            p.opened_side.value if p.opened_side is not None
            else ("BUY" if p.quantity > 0 else ("SELL" if p.quantity < 0 else None))
        ),
    }


def _segment_carryable(segment: str | None) -> bool:
    """True only for segments with a daily EOD rollover (Indian NSE / BSE / MCX
    F&O). 24-hour markets — forex / commodity (XAGUSD, XAUUSD) / crypto (BTCUSD)
    — are rollover-exempt (no EOD close), so `convert_intraday_to_carry` never
    fires for them and the per-position Carry-Forward toggle would do nothing.
    Gate `carry_forward_enabled` on this so the toggle is hidden there."""
    from app.utils.time_utils import market_close_time_for_segment

    return market_close_time_for_segment(segment) is not None


async def _expiry_map(tokens: list[str]) -> dict[str, str]:
    """Batch-resolve REAL contract expiries (ISO `YYYY-MM-DD`) for instrument
    tokens from the Instrument collection. The position's embedded
    InstrumentRef carries no expiry, so we look it up here rather than let the
    UI mis-parse it from the symbol. Missing / non-derivative rows are omitted."""
    toks = list({t for t in tokens if t})
    if not toks:
        return {}
    try:
        from app.models.instrument import Instrument

        docs = await Instrument.find({"token": {"$in": toks}}).to_list()
    except Exception:
        return {}
    out: dict[str, str] = {}
    for d in docs:
        exp = getattr(d, "expiry", None)
        if exp is not None:
            out[d.token] = exp.isoformat() if hasattr(exp, "isoformat") else str(exp)
    return out


@router.get("/open", response_model=APIResponse[list[PositionOut]])
async def open_positions(user: CurrentUser):
    rows = await position_service.list_open(user.id)
    if not rows:
        return APIResponse(data=[])

    # Heal positions whose instrument snapshot stored the bare token as the
    # symbol (stub instrument created while the Zerodha CSV cache was cold —
    # e.g. right after admin cleared all subscriptions). Resolve the real
    # symbol ONCE via the catalog and patch the embedded snapshot so the
    # position card stops showing the numeric token. Guarded on `isdigit()`
    # so normal positions incur zero extra work / DB writes.
    from app.services import instrument_service as _instr_svc
    for r in rows:
        try:
            if (r.instrument.symbol or "").isdigit():
                healed = await _instr_svc.get_by_token(r.instrument.token)
                if healed is not None and not (healed.symbol or "").isdigit():
                    r.instrument.symbol = healed.symbol
                    r.instrument.trading_symbol = healed.trading_symbol or healed.symbol
                    await r.save()
        except Exception:
            pass

    # Refresh LTP and unrealized PnL for the response (best-effort)
    # Also fetch total brokerage per position from associated trades.
    from datetime import timedelta
    tokens = [r.instrument.token for r in rows]

    # Per-position maps: opened_at (for opening fill lower bound) and
    # cycle start (reopened_at or opened_at, for closing fill filter).
    position_opens: dict[tuple[str, str], Any] = {}
    position_cycle_starts: dict[tuple[str, str], Any] = {}
    for r in rows:
        k = (r.instrument.token, str(r.product_type.value))
        position_opens[k] = r.opened_at
        position_cycle_starts[k] = r.reopened_at or r.opened_at

    oldest_open = min((r.opened_at for r in rows if r.opened_at), default=None)
    trade_q: dict[str, Any] = {
        "user_id": user.id,
        "instrument.token": {"$in": tokens},
    }
    if oldest_open is not None:
        trade_q["executed_at"] = {"$gte": oldest_open - timedelta(seconds=5)}
    trades = await Trade.find(trade_q).to_list()

    # Sum brokerage per (token, product_type).
    # Opening fills (pnl_inr=None): count from opened_at onwards (lower bound
    #   prevents old cycles from bleeding in; no upper bound since position is
    #   still OPEN and more lots can be added at any time).
    # Closing fills (pnl_inr set): only count from the current reopen cycle
    #   (>= reopened_at) so stale closing fills don't accumulate.
    charges_map: dict[tuple[str, str], float] = {}
    for t in trades:
        k = (t.instrument.token, str(t.product_type.value))
        is_closing = getattr(t, "pnl_inr", None) is not None
        if is_closing:
            cycle_start = position_cycle_starts.get(k)
            if cycle_start is not None and t.executed_at < cycle_start - timedelta(seconds=5):
                continue  # stale closing fill from a previous cycle — skip
        else:
            pos_open = position_opens.get(k)
            if pos_open is not None and t.executed_at < pos_open - timedelta(seconds=5):
                continue  # opening fill from before this position opened — skip
        charges_map[k] = charges_map.get(k, 0.0) + float(str(t.brokerage))

    # Parallelise LTP fetch + unrealised P&L refresh across every open
    # position with asyncio.gather. Sequential awaits made this O(N) on
    # market_data latency — typically 50 ms × 10 positions = 500 ms wall
    # time. Gathered, the whole batch finishes in ~one network roundtrip.
    ltps = await asyncio.gather(
        *[market_data_service.get_ltp(r.instrument.token) for r in rows],
        return_exceptions=True,
    )
    await asyncio.gather(
        *[
            position_service.refresh_unrealized_pnl(r, ltp if not isinstance(ltp, Exception) else 0)
            for r, ltp in zip(rows, ltps)
        ],
        return_exceptions=True,
    )

    # Resolve each row's effective overnight margin in parallel so we can
    # stamp a real `holding_margin` field on every position. Used to be
    # computed frontend-side as `intraday × 1.4` for MIS (and as-is for
    # NRML), which was a guess that only matched NSE equity tiers — and
    # diverged badly on MCX FUT where the operator had set Intraday=500×,
    # Overnight=70× (carry-forward needs ~7× the locked intraday).
    # Resolver result is cached 5 min per (user, segment, symbol, side,
    # product), so this stays cheap on subsequent reloads.
    ovn_resolved = await asyncio.gather(
        *[
            netting_service.get_effective_settings(
                r.user_id,
                r.instrument.segment,
                action="BUY" if r.quantity >= 0 else "SELL",
                option_type=_opt_type_from_symbol(r.instrument.symbol),
                product_type="NRML",
                symbol=r.instrument.symbol,
            )
            for r in rows
        ],
        return_exceptions=True,
    )

    emap = await _expiry_map(tokens)
    from app.services import user_service as _usvc
    carry_enabled = await _usvc.carry_toggle_enabled_for(user)
    out = []
    for r, resolved in zip(rows, ovn_resolved):
        d = _pos(r, expiry=emap.get(r.instrument.token), carry_enabled=carry_enabled)
        k = (r.instrument.token, str(r.product_type.value))
        charges_amt = charges_map.get(k, 0.0)
        d["charges"] = f"{charges_amt:.2f}"
        # Net the displayed P&L with the commission the admin charges.
        # The user reported the broker brokerage being deducted from
        # their wallet but NOT showing in the position-card P&L number
        # — they wanted the card to read post-commission. The Position
        # document still stores RAW realized for accounting (so admin
        # reports / ledgers can decompose), but the user-facing card
        # subtracts brokerage so what they see matches what hit their
        # wallet.
        if charges_amt > 0:
            try:
                d["unrealized_pnl"] = f"{float(d['unrealized_pnl']) - charges_amt:.2f}"
                d["realized_pnl"] = f"{float(d['realized_pnl']) - charges_amt:.2f}"
            except (TypeError, ValueError):
                pass

        # ── Carry-forward margin (the "Holding Margin" tile) ──
        # Compute against the same notional currently locked, using the
        # OVERNIGHT triple from the resolver. The resolver keeps the
        # product-aware `leverage` on the INTRADAY value in Times mode
        # (symmetric-Times patch in netting_service), so we MUST read
        # the explicit overnight fields, otherwise an MCX FUT row with
        # 500× intraday / 70× overnight reports holding = intraday and
        # the user has no warning before the EOD rollover force-closes.
        # Default: carry margin = the locked used margin (CNC only). MIS AND
        # NRML now lock the INTRADAY margin (700× in Times mode) and keep it
        # through the carry — the EOD flip is margin-neutral — so both recompute
        # the genuine OVERNIGHT requirement below to surface the CF gate. See
        # _recompute_overnight_holding.
        holding_margin = float(d.get("margin_used") or 0.0)
        if not isinstance(resolved, BaseException) and resolved is not None:
            s = (resolved.get("settings") if isinstance(resolved, dict) else None) or {}
            try:
                mode = s.get("margin_calc_mode") or "times"
                if _recompute_overnight_holding(r.product_type, mode):
                    avg_native = float(str(r.avg_price))
                    qty_abs = abs(float(r.quantity))
                    notional = avg_native * qty_abs
                    ovn_fixed = float(s.get("overnight_fixed_margin_per_lot") or 0)
                    if mode == "fixed" and ovn_fixed > 0:
                        lot_size = max(1, int(getattr(r.instrument, "lot_size", 1) or 1))
                        lots = qty_abs / lot_size
                        cf = ovn_fixed * lots
                    else:
                        ovn_pct = float(s.get("overnight_margin_percentage") or 100.0) / 100.0
                        ovn_lev = float(s.get("overnight_leverage") or 1.0) or 1.0
                        cf = notional * ovn_pct / ovn_lev
                        # USD-quoted instruments — convert to INR like the
                        # validator does at order time (fixed-per-lot is
                        # already admin-entered in INR so it's skipped).
                        if market_data_service.is_usd_quoted_segment(r.segment_type) or \
                                market_data_service.is_usd_quoted_segment(r.instrument.segment):
                            cf = cf * market_data_service.get_usd_inr_rate()
                    holding_margin = round(cf, 2)
            except Exception:
                pass
        d["holding_margin"] = f"{holding_margin:.2f}"

        out.append(d)
    return APIResponse(data=out)


@router.get("/closed", response_model=APIResponse[list[PositionOut]])
async def closed_positions(
    user: CurrentUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
):
    # FIFO per-opening-fill closed blotter.
    # Each row represents one (opening-fill × closing-fill) pairing, so the
    # entry price shown is the specific opening fill's price (not a running avg).
    skip = (page - 1) * page_size
    fifo_events, total = await position_service.list_closed_trade_events_fifo(
        user.id, skip=skip, limit=page_size
    )
    if not fifo_events and page == 1:
        return APIResponse(data=[], total=0)

    emap = await _expiry_map([ev["instrument"].token for ev in fifo_events])
    out: list[dict] = []
    for ev in fifo_events:
        inst = ev["instrument"]
        seg = inst.segment
        is_usd = market_data_service.is_usd_quoted_segment(seg)
        lot_size = int(getattr(inst, "lot_size", 0) or 0)
        qty = ev["qty"]
        entry_px = ev["entry_price"]
        close_px = ev["close_price"]
        gross = ev["gross_pnl"]
        brk = ev["brokerage"]
        closed_dt = ev["closed_at"]
        opened_dt = ev["opened_at"]
        out.append({
            "id": ev["_row_id"],
            "position_id": ev["_row_id"],
            "user_id": str(user.id),
            "symbol": inst.symbol,
            "trading_symbol": getattr(inst, "trading_symbol", None) or inst.symbol,
            "exchange": str(inst.exchange),
            "expiry": emap.get(inst.token),
            "segment_type": seg,
            "product_type": ev["product_type"].value,
            "quantity": 0.0,
            "opening_quantity": qty,
            "opened_side": ev["opened_side"],
            "lots": (qty / lot_size) if lot_size else qty,
            "lot_size": lot_size,
            "avg_price": f"{entry_px:.4f}" if is_usd else f"{entry_px:.2f}",
            "ltp": f"{close_px:.4f}" if is_usd else f"{close_px:.2f}",
            "realized_pnl": f"{gross:.2f}",
            "unrealized_pnl": "0.00",
            "margin_used": "0.00",
            "charges": f"{brk:.2f}",
            "currency_quote": "USD" if is_usd else "INR",
            "status": "CLOSED",
            "instrument_token": ev["instrument_token"],
            "opened_at": opened_dt.isoformat() if opened_dt else None,
            "closed_at": closed_dt.isoformat() if closed_dt else None,
            "close_reason": ev.get("close_reason", "USER"),
        })

    return APIResponse(data=out, total=total)


@router.post("/{position_id}/squareoff", response_model=APIResponse[dict])
async def squareoff(
    position_id: str,
    user: CurrentUser,
    request: Request,
    lots: float = Query(default=0.0, ge=0.0, description="Partial close size in lots; 0 = close full position"),
):
    # Resolve the target position. Normally `position_id` is the Position
    # ObjectId. But a "buy then INSTANTLY close" taps Close before the
    # client's optimistic row has reconciled to its real id — so the client
    # sends "token:<instrument_token>" and we square off whatever OPEN
    # position the user holds for that token. This kills the client-side
    # id-resolve poll (the "Closing… settling the order" wait) entirely.
    if position_id.startswith("token:"):
        _tok = position_id.split(":", 1)[1]
        # Short retry: on a VERY fast buy->close the opening order may still
        # be mid-commit, so the OPEN row isn't queryable for a beat. Single-
        # node Mongo commits in ~tens of ms, so the 1st-2nd try almost always
        # finds it; the extra tries just cover the rare race.
        p = None
        for _attempt in range(4):
            p = await Position.find_one(
                Position.user_id == user.id,
                Position.instrument.token == _tok,  # type: ignore[union-attr]
                Position.status == PositionStatus.OPEN,
            )
            if p is not None:
                break
            await asyncio.sleep(0.2)
    else:
        p = await Position.get(_parse_position_id(position_id))
    if p is None or p.user_id != user.id:
        raise HTTPException(status_code=404, detail="Position not found")
    if p.status != PositionStatus.OPEN or p.quantity == 0:
        raise HTTPException(status_code=400, detail="Position already closed")

    # ── Single-flight lock ─────────────────────────────────────────────
    # Prevents rapid double-tap or concurrent API calls from firing two
    # market orders for the same position. Keyed by the RESOLVED position id
    # so a by-token and a by-id close for the SAME position still collide.
    from app.core.redis_client import idempotency_check_and_set as _idem

    _lock_key = f"squareoff_position:{user.id}:{p.id}"
    if not await _idem(_lock_key, ttl_sec=10):
        raise HTTPException(
            status_code=409,
            detail="A close for this position is already in flight — try again in a moment.",
        )

    # ── Market-hours gate ──────────────────────────────────────────────
    # Block a USER-initiated close when the segment's market is closed and
    # return a clear "market is closed" message the client renders as a
    # popup — instead of letting the close fall through to the matching
    # engine and fail with the cryptic "Market data feed is stale (price
    # unavailable)" error (the close can never fill on a dead feed anyway).
    # Crypto (24×7) and forex (24×5) always pass. Admin force-close
    # (admin/trading.py) and the risk enforcer keep their own bypass — this
    # gate is user-only and mirrors the squareoff-all endpoint's guard.
    _seg = getattr(p, "segment_type", None) or getattr(p.instrument, "segment", None)
    if not _is_segment_market_open_now(_seg):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{_segment_market_label(_seg)} market is closed. "
                f"You can close this position once the market reopens."
            ),
        )

    # ── Risk: hold-time minimum ─────────────────────────────────────
    # Admin's Risk Management page sets a floor on how quickly a profitable
    # OR losing position may be closed. Stops scalpers from hammering the
    # backend / abusing latency arbitrage. Skip for MIS auto-squareoff
    # (no `placed_from`); fire only on user-initiated closes.
    from datetime import datetime as _dt, timezone as _tz
    from app.services import netting_service as _ns

    risk = (await _ns.get_effective_risk(str(user.id)))["settings"]
    profit_min = int(risk.get("profitTradeHoldMinSeconds") or 0)
    loss_min = int(risk.get("lossTradeHoldMinSeconds") or 0)
    if (profit_min or loss_min) and p.opened_at:
        opened = p.opened_at if p.opened_at.tzinfo else p.opened_at.replace(tzinfo=_tz.utc)
        held = (_dt.now(_tz.utc) - opened).total_seconds()
        # Decide in-profit vs in-loss with the SAME close-side mark the
        # position card shows: refresh_unrealized_pnl marks a long against the
        # BID and a short against the ASK (the price actually realised on
        # close). The stored `unrealized_pnl` lags the feed, AND the plain
        # last-traded LTP can sit ABOVE the bid — both made a losing long read
        # as break-even/profit, so the PROFIT hold wrongly gated a LOSS trade
        # (06-Jun: CRUDEOIL long −₹200 blocked with "profitable trade …").
        cur_pnl = 0.0
        try:
            _ltp = await market_data_service.get_ltp(p.instrument.token)
            await position_service.refresh_unrealized_pnl(p, to_decimal(_ltp or 0))
            cur_pnl = float(str(p.unrealized_pnl or 0))
        except Exception:
            try:
                cur_pnl = float(str(p.unrealized_pnl or 0))
            except Exception:
                cur_pnl = 0.0
        floor = profit_min if cur_pnl >= 0 else loss_min
        if floor and held < floor:
            remaining = int(floor - held)
            kind = "profitable" if cur_pnl >= 0 else "losing"
            raise HTTPException(
                status_code=400,
                detail=f"Hold-time guard: {kind} trade must be held for {floor}s "
                       f"(wait {remaining}s more before closing).",
            )

    # Place an opposite-side market order. When `lots` is provided we close
    # exactly that slice of the position (clamped to <= total). Otherwise we
    # close everything.
    action = OrderAction.SELL if p.quantity > 0 else OrderAction.BUY
    full_qty = abs(p.quantity)
    full_lots = max(0.01, full_qty / max(1, p.instrument.lot_size or 1))
    close_lots = full_lots if lots <= 0 else min(float(lots), full_lots)
    # `force_quantity` flattens exactly what's open — closes the actual
    # stored quantity regardless of whether `lot_size` has drifted (legacy
    # positions stored as `lots × 1`).  For partial closes we scale the
    # force-qty by the requested lots / full-lots ratio so partial closes
    # still work proportionally.
    close_qty = full_qty if close_lots >= full_lots else full_qty * (close_lots / full_lots)
    o = await order_service.place_order(
        user=user,
        payload={
            "token": p.instrument.token,
            "action": action.value,
            "order_type": OrderType.MARKET.value,
            "product_type": p.product_type.value,
            "lots": close_lots,
            "force_quantity": close_qty,
            "placed_from": "WEB",
            "is_squareoff": True,
        },
    )

    # Post-close bookkeeping — close_reason stamp + audit — runs AFTER the
    # response. The close itself is already done & persisted by place_order;
    # both of these are display/history only (the Closed tab refresh is even
    # debounced ~1.5 s client-side), so awaiting their 3 DB round-trips here
    # only added latency to the trader's "Close" tap for no benefit.
    _pid = p.id
    _sym = p.instrument.symbol
    _uid = user.id
    _ip = _client_ip(request)
    _ua = request.headers.get("user-agent")

    async def _post_close() -> None:
        # If this squareoff flattened the position, stamp close_reason so the
        # Closed tab shows "Closed by User". (The matching engine mutated the
        # row in place inside place_order, so we re-read it.)
        try:
            fresh = await Position.get(_pid)
            if (
                fresh is not None
                and fresh.status == PositionStatus.CLOSED
                and not fresh.close_reason
            ):
                fresh.close_reason = "USER"
                await fresh.save()
        except Exception:
            import logging as _lg

            _lg.getLogger(__name__).exception("squareoff_close_reason_stamp_failed")
        await audit_service.log_event(
            action=AuditAction.SQUAREOFF,
            entity_type="Position",
            entity_id=_pid,
            actor_id=_uid,
            target_user_id=_uid,
            ip_address=_ip,
            user_agent=_ua,
            metadata={
                "symbol": _sym,
                "closed_lots": close_lots,
                "closed_qty": close_qty,
            },
        )

    # Bulletproof: the close already succeeded inside place_order — a
    # background-scheduling error here must NEVER turn a successful close
    # into a 500 that the client reads as "close failed".
    try:
        from app.utils.background import fire_and_forget

        fire_and_forget(_post_close(), label="squareoff_post_close")
    except Exception:
        import logging as _lg

        _lg.getLogger(__name__).exception("squareoff_post_close_schedule_failed")
    return APIResponse(data={"order_id": str(o.id), "status": o.status.value, "closed_lots": close_lots})


@router.put("/{position_id}/carry-forward", response_model=APIResponse[dict])
async def set_position_carry_forward(position_id: str, payload: dict, user: CurrentUser):
    """Per-position Carry-Forward opt-in. Works only when the user's admin has
    enabled the feature (`carry_forward_toggle_enabled`). ON ⇒ the position
    carries overnight at EOD (still subject to the usual affordability + segment
    checks); OFF (default) ⇒ the EOD rollover squares it off."""
    from app.services import user_service as _usvc

    if not await _usvc.carry_toggle_enabled_for(user):
        raise HTTPException(
            status_code=403,
            detail="Carry-Forward toggle is not enabled for your account.",
        )
    p = await Position.get(_parse_position_id(position_id))
    if p is None or p.user_id != user.id:
        raise HTTPException(status_code=404, detail="Position not found")
    if p.status != PositionStatus.OPEN:
        raise HTTPException(status_code=400, detail="Only open positions can carry forward.")
    if not _segment_carryable(p.segment_type):
        raise HTTPException(
            status_code=400,
            detail="This is a 24-hour market with no EOD rollover — carry-forward doesn't apply here.",
        )
    enabled = bool(payload.get("enabled"))
    p.user_carry_forward = enabled
    await p.save()
    return APIResponse(
        data={"id": str(p.id), "user_carry_forward": enabled},
        message=("Carry Forward ON — this position will hold overnight." if enabled else "Carry Forward OFF — squares off at EOD."),
    )


@router.put("/{position_id}/sl-tp", response_model=APIResponse[dict])
async def update_sl_tp(position_id: str, payload: dict, user: CurrentUser):
    """Edit the stop-loss and target on an open position. Pass null/0 to clear."""
    from bson import Decimal128

    p = await Position.get(_parse_position_id(position_id))
    if p is None or p.user_id != user.id:
        raise HTTPException(status_code=404, detail="Position not found")
    if p.status != PositionStatus.OPEN:
        raise HTTPException(status_code=400, detail="Position is not open")

    def _to_float(v: Any) -> float | None:
        if v in (None, "", 0, "0"):
            return None
        try:
            return float(str(v))
        except (TypeError, ValueError):
            return None

    sl_val = _to_float(payload.get("stop_loss")) if "stop_loss" in payload else None
    tp_val = _to_float(payload.get("target")) if "target" in payload else None

    # Fetch live LTP + resolved settings (for limit_away) in parallel.
    from app.services import market_data_service as _mds
    from app.services import netting_service as _ns
    import asyncio as _asyncio

    async def _get_ltp() -> float:
        try:
            return float(await _mds.get_ltp(p.instrument.token))
        except Exception:
            return 0.0

    async def _get_settings() -> dict:
        try:
            return await _ns.get_effective_settings(
                user.id,
                p.segment_type,
                action=str(p.opened_side.value if hasattr(p.opened_side, "value") else p.opened_side or "BUY"),
                option_type=_opt_type_from_symbol(p.instrument.symbol),
                product_type=str(p.product_type.value if hasattr(p.product_type, "value") else p.product_type),
                symbol=p.instrument.symbol,
            )
        except Exception:
            return {}

    _ltp, _seg_settings = await _asyncio.gather(_get_ltp(), _get_settings())
    _ref = _ltp if _ltp > 0 else float(str(p.avg_price or 0))
    _side = str(p.opened_side or "BUY").upper()
    _eff = _seg_settings.get("settings") or {}
    _limit_pct = float(_eff.get("limit_percentage") or 0)

    # ── Per-segment SL / TP enable gate ───────────────────────────────
    # Admin can turn OFF SL and/or TP for a segment (Limit away → SL/TP
    # on-off). Block SETTING the disabled leg. Clearing (null/0) is always
    # allowed so a user can still remove a leg placed before the toggle.
    if sl_val is not None and not _eff.get("sl_enabled", True):
        raise HTTPException(status_code=400, detail="Stop Loss is disabled for this segment.")
    if tp_val is not None and not _eff.get("tp_enabled", True):
        raise HTTPException(status_code=400, detail="Target (Take Profit) is disabled for this segment.")

    if _ref > 0:
        # 1. Directional check (shared helper — same guard as place-order + the
        #    active-trade endpoint, so all three writers can't drift apart).
        _dir_err = order_validator.bracket_direction_error(_side, _ref, sl=sl_val, tp=tp_val)
        if _dir_err:
            raise HTTPException(status_code=400, detail=_dir_err)

        # 2. Limit-away min-distance check
        if _limit_pct > 0:
            _upper = _ref * (1 + _limit_pct / 100)
            _lower = _ref * (1 - _limit_pct / 100)
            if sl_val is not None and _lower < sl_val < _upper:
                raise HTTPException(
                    status_code=400,
                    detail=f"Stop Loss ₹{sl_val} is too close to current price ₹{_ref:.2f}. Must be at least {_limit_pct:.0f}% away (≤ ₹{_lower:.2f}).",
                )
            if tp_val is not None and _lower < tp_val < _upper:
                raise HTTPException(
                    status_code=400,
                    detail=f"Target ₹{tp_val} is too close to current price ₹{_ref:.2f}. Must be at least {_limit_pct:.0f}% away (≥ ₹{_upper:.2f}).",
                )

    if "stop_loss" in payload:
        sl = payload["stop_loss"]
        p.stop_loss = (
            Decimal128(str(sl))
            if sl not in (None, "", 0, "0")
            else None
        )
    if "target" in payload:
        tp = payload["target"]
        p.target = (
            Decimal128(str(tp))
            if tp not in (None, "", 0, "0")
            else None
        )
    await p.save()
    return APIResponse(data=_pos(p))


@router.get("/active-trades", response_model=APIResponse[list])
async def list_active_trades(user: CurrentUser):
    """Per-fill view of currently-open exposure.

    Returns one row per Trade record where:
      • the user's matching Position is still OPEN, AND
      • the trade's action matches the position direction (a BUY contributes
        to a long, a SELL to a short — opposite-side fills are closing legs
        and don't represent ongoing exposure).

    The aggregation model means closing one row partially closes the whole
    position at its weighted-average price (FIFO/avg accounting). P&L per row
    is computed against the row's own fill price so the trader sees the
    unrealised gain on each individual entry.
    """
    open_positions = await Position.find(
        Position.user_id == user.id, Position.status == PositionStatus.OPEN
    ).to_list()
    if not open_positions:
        return APIResponse(data=[])

    # Primary lookup: (token, product_type). Secondary lookup: token-only,
    # for trades whose product_type enum has drifted in casing from the
    # position's. The secondary map only kicks in when the primary misses
    # — symptom we previously saw: "trade position me dikh raha par
    # active me nahi" which was actually a key-mismatch, not data loss.
    pos_by_key: dict[tuple[str, str], Position] = {
        (p.instrument.token, str(p.product_type.value)): p for p in open_positions
    }
    pos_by_token: dict[str, Position] = {p.instrument.token: p for p in open_positions}
    tokens = [p.instrument.token for p in open_positions]
    emap = await _expiry_map(tokens)
    from app.services import user_service as _usvc_at
    carry_enabled = await _usvc_at.carry_toggle_enabled_for(user)

    # Pull every trade for these (user, instrument) pairs — no date
    # filter. Earlier the query used `executed_at >= oldest_open - 5s`
    # for performance, but that broke on **flipped / reopened
    # positions**: when a user closes a long and re-shorts the same
    # instrument the new position's `opened_at` is reset to "now", so
    # any opening trade older than that vanished from the FIFO match
    # → opposite-side total mis-counted → wrong number of active-trade
    # rows surfaced (user-reported: "position me 4 dikh raha, active
    # me sirf 2"). Without the date filter the query is still scoped
    # to (user_id, token) so the result set stays small even for
    # high-frequency traders.
    trade_q: dict[str, Any] = {
        "user_id": user.id,
        "instrument.token": {"$in": tokens},
    }
    trades = await Trade.find(trade_q).sort("-executed_at").to_list()

    # Fallback: if Beanie raw-dict query returns nothing but positions exist,
    # try with explicit ObjectId cast (guards against type mismatch).
    if not trades and open_positions:
        from bson import ObjectId as _OID
        trade_q_fallback: dict[str, Any] = {
            "user_id": _OID(str(user.id)),
            "instrument.token": {"$in": tokens},
        }
        trades = await Trade.find(trade_q_fallback).sort("-executed_at").to_list()

    # ── Weekly-settlement carry seed ─────────────────────────────────────
    # A position REOPENED by weekly settlement carries its qty at the
    # SETTLEMENT price — its pre-settlement P&L was already realised to the
    # wallet and the old lot CLOSED. That carried lot has NO Trade row, so the
    # per-fill reconstruction below would miss it and value the open lots at
    # raw PRE-settlement trade prices — double-counting the already-banked P&L
    # and diverging from the (correct) netted Position M2M. Inject a synthetic
    # opening fill at the settlement price for each settlement-reopened
    # position so Σ(Active per-fill P&L) == Position M2M. Display-only: no new
    # Trade doc, no money movement. Best-effort — never break the tab.
    try:
        from types import SimpleNamespace as _NS

        from app.models.position_settlement import PositionSettlement as _PSett

        _pos_by_id = {p.id: p for p in open_positions}
        _setts = await _PSett.find(
            {"new_position_id": {"$in": list(_pos_by_id.keys())}}
        ).to_list()
        for _s in _setts:
            _p = _pos_by_id.get(_s.new_position_id)
            if _p is None or not _s.quantity or _s.quantity <= 0:
                continue
            _side = str(getattr(_s, "side", "") or "").upper()
            trades.append(
                _NS(
                    id=_s.id,
                    trade_number=f"CARRY-{str(_s.id)[-6:]}",
                    executed_at=_p.opened_at,
                    action=OrderAction.BUY if _side == "BUY" else OrderAction.SELL,
                    quantity=float(_s.quantity),
                    price=_s.settlement_price,
                    product_type=_p.product_type,
                    instrument=_p.instrument,
                    brokerage=None,
                    cost_basis_override=None,
                )
            )
    except Exception:  # noqa: BLE001
        import logging as _lg

        _lg.getLogger(__name__).exception("active_trades_carry_seed_failed")

    # Live LTP per token — parallelised with gather so N tokens cost ~1
    # network round-trip instead of N sequential awaits (was ~50 ms × N).
    unique_toks = list(set(tokens))
    _ltp_results = await asyncio.gather(
        *[market_data_service.get_display_ltp(tok) for tok in unique_toks],
        return_exceptions=True,
    )
    ltp_by_token: dict[str, float] = {
        tok: (float(v) if not isinstance(v, Exception) and v else 0.0)
        for tok, v in zip(unique_toks, _ltp_results)
    }
    usd_inr = market_data_service.get_usd_inr_rate()

    # Batch-resolve effective overnight settings per unique
    # (segment, product_type, symbol, action) so each Active-tab card can
    # show the REAL carry-forward margin instead of the old `used × 1.4`
    # heuristic. Operator-flagged 22-May: TCS card on Active tab read
    # ₹1,127 (805.28 × 1.4) while the trade dialog correctly showed
    # ₹5,752 from segment-settings — two different numbers for the same
    # position. Resolver cache (5 min) makes repeat calls cheap even
    # with dozens of positions.
    ovn_settings_by_key: dict[tuple[str, str, str, str], dict] = {}
    unique_keys = list({
        (
            p.instrument.segment,
            str(p.product_type.value),
            p.instrument.symbol,
            "BUY" if p.quantity >= 0 else "SELL",
        )
        for p in open_positions
    })
    if unique_keys:
        resolved_list = await asyncio.gather(
            *[
                netting_service.get_effective_settings(
                    user.id,
                    seg,
                    action=action,
                    option_type=_opt_type_from_symbol(sym),
                    product_type="NRML",
                    symbol=sym,
                )
                for seg, _prod, sym, action in unique_keys
            ],
            return_exceptions=True,
        )
        for k, r in zip(unique_keys, resolved_list):
            if isinstance(r, BaseException) or not isinstance(r, dict):
                ovn_settings_by_key[k] = {}
            else:
                ovn_settings_by_key[k] = r.get("settings") or {}

    # ── Per-position lifecycle-scoped FIFO ──────────────────────────
    # Scope trades to the current position lifecycle using `opened_at`
    # as the boundary — trades before that belong to a previous CLOSED
    # lifecycle and must not contaminate the current FIFO. Walk
    # oldest-first within the lifecycle, accumulate same-side fills,
    # consume opposite-side fills FIFO against them. What survives is
    # the Active row set.
    #
    # Fallback: if `opened_at` is missing or produces zero matching
    # trades for a position with non-zero qty, fall back to claiming
    # the most recent same-side fills equal to |position.quantity| —
    # this handles admin-edited / legacy positions gracefully without
    # the stale `opening_quantity` dependency.
    from datetime import datetime as _datetime
    from datetime import timedelta as _timedelta

    trade_owner: dict[str, Position] = {}
    remaining_qty: dict[str, float] = {}

    # One open position per (user, token) is the invariant now — order_service
    # makes a fresh order ADOPT an existing open position's product_type, and
    # historical MIS+NRML splits on the same stock were merged. So attribute
    # trades to a position by TOKEN, NOT by product_type: a position that was
    # carried MIS->NRML at EOD keeps MIS-typed backing fills, so a
    # product_type match would attract ZERO same-side trades and the Active
    # tab would under-show (or drop) it — the exact LINDEINDIA "position 7,
    # active 1" symptom. We ONLY keep the product_type gate for a token that
    # genuinely still holds MULTIPLE open positions (legacy fragmentation),
    # where mixing books would double-count.
    from collections import Counter as _Counter

    _tok_counts = _Counter(p.instrument.token for p in open_positions)
    _multi_pos_tokens = {tok for tok, c in _tok_counts.items() if c > 1}

    for p in open_positions:
        if p.quantity == 0:
            continue
        is_long = p.quantity > 0

        # Lifecycle-scoped trades: only trades since this position opened,
        # for the same instrument + product_type.
        lifecycle_trades: list[Any] = []
        for t in trades:
            if t.instrument.token != p.instrument.token:
                continue
            if (
                p.instrument.token in _multi_pos_tokens
                and str(t.product_type.value) != str(p.product_type.value)
            ):
                continue
            t_time = t.executed_at or t.created_at
            # Small grace on the lifecycle boundary. `opened_at` is stamped in
            # apply_fill a few ms AFTER the opening fill's `executed_at` (they
            # run in the same call, trade insert first). Without the grace the
            # opening fill can fall a few ms BEFORE opened_at and get dropped —
            # the lifecycle then under-counts, the fallback below fires, and the
            # fallback (which grabs the newest N same-side fills) re-surfaces a
            # fill the user just closed via specific-lot Exit (operator: exited
            # 107.75 but Active still showed it). 2 s is ~600× the observed skew
            # yet nowhere near the previous, minutes-old closed lifecycle.
            if (
                p.opened_at
                and t_time
                and t_time < p.opened_at - _timedelta(seconds=2)
            ):
                continue
            lifecycle_trades.append(t)

        # Sort oldest-first for FIFO walk.
        lifecycle_trades.sort(key=lambda tr: tr.executed_at or _datetime.min)

        # Simple FIFO: same-side fills accumulate, opposite-side
        # consumes oldest same-side first. Leftover = active rows.
        same_side_fifo: list[tuple[Any, float]] = []  # (trade, remaining_qty)
        for t in lifecycle_trades:
            tq = float(t.quantity)
            is_same = (is_long and t.action == OrderAction.BUY) or \
                      (not is_long and t.action == OrderAction.SELL)
            if is_same:
                same_side_fifo.append((t, tq))
            else:
                remain = tq
                # Specific-lot close: a closing trade tagged with
                # cost_basis_override (Active-tab Exit) consumes the same-side
                # fill whose ENTRY PRICE matches the tapped lot FIRST — so the
                # Active tab drops the exact fill the user exited (and the
                # Closed blotter shows the same one), not the FIFO oldest. Any
                # remainder, and every non-override close, falls to FIFO below.
                _cbo = getattr(t, "cost_basis_override", None)
                _basis = None
                if _cbo is not None:
                    try:
                        _basis = float(str(_cbo))
                    except Exception:
                        _basis = None
                if _basis is not None:
                    for i, (st, sq) in enumerate(same_side_fifo):
                        if remain <= 0:
                            break
                        try:
                            match = sq > 1e-9 and abs(float(str(st.price)) - _basis) < 1e-6
                        except Exception:
                            match = False
                        if match:
                            consume = min(sq, remain)
                            same_side_fifo[i] = (st, sq - consume)
                            remain -= consume
                # FIFO: consume from oldest same-side fills (the remainder, or
                # the whole quantity for a normal avg-price close).
                for i, (st, sq) in enumerate(same_side_fifo):
                    if remain <= 0:
                        break
                    consume = min(sq, remain)
                    same_side_fifo[i] = (st, sq - consume)
                    remain -= consume

        # Surviving same-side fills with leftover > 0 are active rows.
        for t, leftover in same_side_fifo:
            if leftover > 1e-9:
                trade_owner[str(t.id)] = p
                remaining_qty[str(t.id)] = leftover

        # Fallback: if lifecycle scoping produced fewer active qty than
        # the position's current |quantity| (opened_at corrupted or
        # shifted by admin edits), claim the MOST RECENT same-side
        # fills that sum to |position.qty|. This never pulls in old
        # closed-lifecycle trades — it grabs exactly the newest fills
        # that add up to the current position size.
        active_for_pos = sum(
            v for tid, v in remaining_qty.items()
            if trade_owner.get(tid) is p
        )
        pos_qty_abs = abs(float(p.quantity))
        if active_for_pos < pos_qty_abs - 1e-9:
            # Clear any partial results from the lifecycle-scoped pass.
            for tid in list(remaining_qty):
                if trade_owner.get(tid) is p:
                    del remaining_qty[tid]
                    del trade_owner[tid]

            # Grab most recent same-side fills summing to |qty|.
            need = pos_qty_abs
            all_same = [
                t for t in trades
                if t.instrument.token == p.instrument.token
                and (
                    p.instrument.token not in _multi_pos_tokens
                    or str(t.product_type.value) == str(p.product_type.value)
                )
                and ((is_long and t.action == OrderAction.BUY) or
                     (not is_long and t.action == OrderAction.SELL))
            ]
            all_same.sort(key=lambda tr: tr.executed_at or _datetime.min, reverse=True)

            # Specific-lot Exits (opposite-side closing trades carrying a
            # cost_basis_override) tell us WHICH opening fill the user meant to
            # close. This recency grab is otherwise override-blind and would
            # re-surface a just-exited lot (the "exit kiya par Active se hata
            # nahi" bug) — so consume each override against the matching-price
            # opening fill first and drop that qty from the view. No override
            # closes → this list is empty and behaviour is unchanged.
            cbo_remaining: list[list[float]] = []
            for ct in trades:
                if (
                    ct.instrument.token == p.instrument.token
                    and (
                        p.instrument.token not in _multi_pos_tokens
                        or str(ct.product_type.value) == str(p.product_type.value)
                    )
                    and ((is_long and ct.action == OrderAction.SELL) or
                         (not is_long and ct.action == OrderAction.BUY))
                ):
                    _c = getattr(ct, "cost_basis_override", None)
                    if _c is not None:
                        try:
                            cbo_remaining.append([float(str(_c)), float(ct.quantity)])
                        except Exception:  # noqa: BLE001
                            pass

            accum = 0.0
            for t in all_same:
                if accum >= need:
                    break
                tq = float(t.quantity)
                try:
                    pr = float(str(t.price))
                except Exception:  # noqa: BLE001
                    pr = None
                # Peel off any part of this fill already closed by a specific-lot
                # Exit at the same price, so the exited lot doesn't reappear.
                if pr is not None and cbo_remaining:
                    for cb in cbo_remaining:
                        if cb[1] <= 1e-9:
                            continue
                        if abs(cb[0] - pr) < 1e-4:
                            take = min(cb[1], tq)
                            cb[1] -= take
                            tq -= take
                            if tq <= 1e-9:
                                break
                if tq <= 1e-9:
                    continue
                show = min(tq, need - accum)
                trade_owner[str(t.id)] = p
                remaining_qty[str(t.id)] = show
                accum += show

    # Per-fill SL/TP (pending-order model): map each opening fill → its parked
    # exit orders' stop / target so the Active tab shows the SL/TP that belongs
    # to THAT fill only, not the whole position's bracket.
    from app.models.order import Order as _OrderM, OrderStatus as _OSM

    _fill_sltp: dict[str, dict] = {}
    try:
        _exit_orders = await _OrderM.find(
            {
                "user_id": user.id,
                "sl_tp_source_trade_id": {"$ne": None},
                "status": {"$in": [_OSM.OPEN.value, _OSM.PARTIAL.value, _OSM.PENDING.value]},
            }
        ).to_list()
        for _eo in _exit_orders:
            _k = str(_eo.sl_tp_source_trade_id)
            _d = _fill_sltp.setdefault(_k, {"stop_loss": None, "target": None})
            _ot = str(getattr(_eo.order_type, "value", _eo.order_type) or "").upper()
            if _ot == "LIMIT":
                _d["target"] = str(_eo.price) if _eo.price is not None else None
            elif _ot in ("SL", "SL_M"):
                _d["stop_loss"] = str(_eo.trigger_price) if _eo.trigger_price is not None else None
    except Exception:  # noqa: BLE001
        _fill_sltp = {}

    rows: list[dict[str, Any]] = []
    for t in trades:
        # Per-position attribution from the windowed-FIFO pass above.
        # `trade_owner` only contains trades that landed inside an OPEN
        # position's time window — closed-cycle trades are excluded
        # automatically. The pre-existing direction filter is now
        # redundant (a trade is in `same_side_by_pos` only if it
        # matched direction) but we keep it as a defensive guard.
        p = trade_owner.get(str(t.id))
        if p is None:
            continue
        if p.quantity > 0 and t.action != OrderAction.BUY:
            continue
        if p.quantity < 0 and t.action != OrderAction.SELL:
            continue

        # Skip trades whose qty has been fully closed by opposite-side fills.
        qty = remaining_qty.get(str(t.id), 0.0)
        if qty <= 0:
            continue

        price = float(str(t.price))
        ltp = ltp_by_token.get(t.instrument.token, 0.0)
        is_usd = market_data_service.is_usd_quoted_segment(p.segment_type) or \
            market_data_service.is_usd_quoted_segment(p.instrument.segment)
        fx = usd_inr if is_usd else 1.0
        direction = 1 if t.action == OrderAction.BUY else -1
        avg_price = price  # individual fill's execution price (not position avg)
        pos_direction = 1 if p.quantity >= 0 else -1
        gross_pnl_inr = pos_direction * (ltp - avg_price) * qty * fx if ltp > 0 else 0.0
        # Subtract this fill's commission so the per-trade row shows
        # the user's true booked P&L — same correction we apply on the
        # /open and /closed position endpoints. The user wants the
        # commission the admin set to be reflected in the displayed P&L
        # rather than only hidden in the wallet ledger.
        try:
            brokerage_inr = float(str(t.brokerage)) if t.brokerage is not None else 0.0
        except (TypeError, ValueError):
            brokerage_inr = 0.0
        pnl_inr = gross_pnl_inr - brokerage_inr

        # Per-fill margin attribution. Position.margin_used is the
        # aggregate locked margin for the whole position; we apportion
        # it across each still-open same-side trade proportional to
        # this trade's remaining qty. Without this the frontend's
        # `r.margin_used / r.margin` keys both fall through to 0 and
        # the Used / Holding columns render as "₹0.00" for every row.
        pos_total_qty = abs(float(p.quantity)) or 1.0
        pos_margin = float(str(p.margin_used or 0))
        trade_share = qty / pos_total_qty if pos_total_qty > 0 else 0.0
        used_margin_inr = round(pos_margin * trade_share, 2)

        # `holding_margin` — true carry-forward requirement, NOT the old
        # `intraday × 1.4` guess. Read the effective overnight settings
        # for this user's pool (resolver cascades broker → admin →
        # super-admin → global), then compute notional × pct ÷ leverage
        # for this trade's slice. Same formula `order_validator` runs at
        # order-placement time, so the per-fill Holding tile now agrees
        # with the OrderPanel's "Carry-forward margin" preview the user
        # saw before placing the trade.
        sett_key = (
            p.instrument.segment,
            str(p.product_type.value),
            p.instrument.symbol,
            "BUY" if p.quantity >= 0 else "SELL",
        )
        s = ovn_settings_by_key.get(sett_key) or {}
        # Default: carry margin = locked used margin (NRML/CNC + Times mode).
        holding_margin_inr = used_margin_inr
        try:
            mode = s.get("margin_calc_mode") or "times"
            if _recompute_overnight_holding(p.product_type, mode):
                lot_size = max(1, int(p.instrument.lot_size or 1))
                trade_lots = qty / lot_size if lot_size > 0 else qty
                ovn_fixed = float(s.get("overnight_fixed_margin_per_lot") or 0)
                if mode == "fixed" and ovn_fixed > 0:
                    holding_native = ovn_fixed * trade_lots
                else:
                    trade_notional = qty * price
                    ovn_pct = float(s.get("overnight_margin_percentage") or 100.0) / 100.0
                    ovn_lev = float(s.get("overnight_leverage") or 1.0) or 1.0
                    holding_native = trade_notional * ovn_pct / ovn_lev
                # USD → INR same as the order validator does. Skip for
                # fixed mode where ₹/lot is already admin-entered in INR.
                if is_usd and not (mode == "fixed" and ovn_fixed > 0):
                    holding_native *= fx
                holding_margin_inr = round(holding_native, 2)
        except Exception:
            holding_margin_inr = used_margin_inr

        rows.append({
            "id": str(t.id),
            "trade_number": t.trade_number,
            "executed_at": t.executed_at.isoformat() if t.executed_at else None,
            "position_id": str(p.id),
            "symbol": p.instrument.symbol,
            "trading_symbol": getattr(p.instrument, "trading_symbol", None),
            "exchange": str(p.instrument.exchange),
            "expiry": emap.get(p.instrument.token),
            "carry_forward_enabled": carry_enabled and _segment_carryable(p.segment_type),
            "user_carry_forward": bool(getattr(p, "user_carry_forward", False)),
            "segment": p.segment_type,
            "instrument_token": p.instrument.token,
            "currency_quote": "USD" if is_usd else "INR",
            "action": t.action.value,
            "side": t.action.value,  # alias for the UI
            "product_type": p.product_type.value,
            "quantity": qty,
            "lots": qty / max(1, p.instrument.lot_size or 1),
            "lot_size": p.instrument.lot_size or 1,
            "price": f"{price:.4f}" if is_usd else f"{price:.2f}",
            "avg_price": f"{avg_price:.4f}" if is_usd else f"{avg_price:.2f}",
            "ltp": f"{ltp:.4f}" if is_usd else f"{ltp:.2f}",
            # Per-fill exit-order SL/TP wins; fall back to the position bracket
            # (legacy / synthetic pos- rows that have no per-fill exit order).
            "stop_loss": (
                _fill_sltp.get(str(t.id), {}).get("stop_loss")
                or (str(p.stop_loss) if p.stop_loss is not None else None)
            ),
            "target": (
                _fill_sltp.get(str(t.id), {}).get("target")
                or (str(p.target) if p.target is not None else None)
            ),
            "pnl": f"{pnl_inr:.2f}",
            "brokerage": str(t.brokerage),
            # Per-fill margin (INR). `used_margin` = currently locked;
            # `holding_margin` = what would lock if rolled overnight.
            "used_margin": f"{used_margin_inr:.2f}",
            "margin_used": f"{used_margin_inr:.2f}",  # alias for FE
            "margin": f"{used_margin_inr:.2f}",       # alias for FE
            "holding_margin": f"{holding_margin_inr:.2f}",
        })

    # ── Fallback for positions with no matching trades ─────────────
    # Some open positions land here without ANY same-side Trade rows
    # to anchor against — usually because they were reopened by admin
    # (Reopen restores Position.quantity but does not re-emit Trade
    # rows) or are legacy data from before Trade tracking existed.
    # Without this fallback those positions silently disappear from
    # the Active tab even though they hold real exposure (operator
    # case CL59347510: 46 OPEN positions, only 5 had same-side
    # trades, so the Active tab showed ~19 rows out of an expected
    # 46+).  Synthesize one row from the Position document so every
    # OPEN position surfaces here regardless of trade history state.
    positions_with_rows = {r["position_id"] for r in rows}
    for p in open_positions:
        if str(p.id) in positions_with_rows:
            continue
        if p.quantity == 0:
            continue
        qty = abs(float(p.quantity))
        price = float(str(p.avg_price))
        ltp = ltp_by_token.get(p.instrument.token, 0.0)
        is_usd = market_data_service.is_usd_quoted_segment(p.segment_type) or \
            market_data_service.is_usd_quoted_segment(p.instrument.segment)
        fx = usd_inr if is_usd else 1.0
        direction = 1 if p.quantity > 0 else -1
        gross_pnl_inr = direction * (ltp - price) * qty * fx if ltp > 0 else 0.0
        # No trade row → no per-fill brokerage to subtract. Use the
        # stored unrealized_pnl (already net of charges) when present
        # so the synthesized row matches what the Positions tab shows.
        try:
            stored_unreal = float(str(p.unrealized_pnl)) if p.unrealized_pnl is not None else None
        except (TypeError, ValueError):
            stored_unreal = None
        pnl_inr = stored_unreal if stored_unreal not in (None, 0) else gross_pnl_inr

        pos_margin = float(str(p.margin_used or 0))
        used_margin_inr = round(pos_margin, 2)
        sett_key = (
            p.instrument.segment,
            str(p.product_type.value),
            p.instrument.symbol,
            "BUY" if p.quantity >= 0 else "SELL",
        )
        s = ovn_settings_by_key.get(sett_key) or {}
        # Default: carry margin = locked used margin (NRML/CNC + Times mode).
        holding_margin_inr = used_margin_inr
        try:
            mode = s.get("margin_calc_mode") or "times"
            if _recompute_overnight_holding(p.product_type, mode):
                lot_size = max(1, int(p.instrument.lot_size or 1))
                pos_lots = qty / lot_size if lot_size > 0 else qty
                ovn_fixed = float(s.get("overnight_fixed_margin_per_lot") or 0)
                if mode == "fixed" and ovn_fixed > 0:
                    holding_native = ovn_fixed * pos_lots
                else:
                    notional = qty * price
                    ovn_pct = float(s.get("overnight_margin_percentage") or 100.0) / 100.0
                    ovn_lev = float(s.get("overnight_leverage") or 1.0) or 1.0
                    holding_native = notional * ovn_pct / ovn_lev
                if is_usd and not (mode == "fixed" and ovn_fixed > 0):
                    holding_native *= fx
                holding_margin_inr = round(holding_native, 2)
        except Exception:
            holding_margin_inr = used_margin_inr

        synthetic_action = "BUY" if p.quantity > 0 else "SELL"
        rows.append({
            # Synthetic id prefix lets the FE distinguish these rows
            # from real trades — close/edit calls on them target the
            # position rather than a trade record.
            "id": f"pos-{p.id}",
            "trade_number": None,
            "executed_at": p.opened_at.isoformat() if p.opened_at else None,
            "position_id": str(p.id),
            "symbol": p.instrument.symbol,
            "trading_symbol": getattr(p.instrument, "trading_symbol", None),
            "exchange": str(p.instrument.exchange),
            "expiry": emap.get(p.instrument.token),
            "carry_forward_enabled": carry_enabled and _segment_carryable(p.segment_type),
            "user_carry_forward": bool(getattr(p, "user_carry_forward", False)),
            "segment": p.segment_type,
            "instrument_token": p.instrument.token,
            "currency_quote": "USD" if is_usd else "INR",
            "action": synthetic_action,
            "side": synthetic_action,
            "product_type": p.product_type.value,
            "quantity": qty,
            "lots": qty / max(1, p.instrument.lot_size or 1),
            "lot_size": p.instrument.lot_size or 1,
            "price": f"{price:.4f}" if is_usd else f"{price:.2f}",
            "ltp": f"{ltp:.4f}" if is_usd else f"{ltp:.2f}",
            "stop_loss": str(p.stop_loss) if p.stop_loss is not None else None,
            "target": str(p.target) if p.target is not None else None,
            "pnl": f"{pnl_inr:.2f}",
            "brokerage": "0",
            "used_margin": f"{used_margin_inr:.2f}",
            "margin_used": f"{used_margin_inr:.2f}",
            "margin": f"{used_margin_inr:.2f}",
            "holding_margin": f"{holding_margin_inr:.2f}",
            "synthetic": True,
        })

    return APIResponse(data=rows)


@router.post("/active-trades/{trade_id}/close", response_model=APIResponse[dict])
async def close_active_trade(trade_id: str, user: CurrentUser):
    """Close ONLY the still-open slice of this trade — issues an opposite
    market order for the trade's remaining (FIFO-leftover) quantity, NOT
    the trade's original fill quantity.

    Why this is critical (was a production bug):
      The earlier implementation used `min(t.quantity, |p.quantity|)`,
      which over-closed whenever the trade had been partially consumed
      by a prior closing leg AND the user later pyramided more lots on
      top. Example:
          BUY 5 (T1) → LONG 5
          SELL 2     → partial close, LONG 3 (T1 leftover = 3 via FIFO)
          BUY 4 (T3) → LONG 7 (active rows: T1=3, T3=4)
          User clicks "Close" on T1 (UI shows 3 lots)
              old code: close_qty = min(5, 7) = 5 → over-closed by 2
              new code: close_qty = min(leftover_for_T1=3, 7) = 3 ✓
      The visible symptom was "ek active close kiya, T3 bhi shrink ho
      gaya, position bhi 5 ki bajay 4 lots kam gayi" — exactly the
      "close one → all close" report.

    Also adds a Redis idempotency lock so a double-click (or a retry
    after a network blip) can't fire two opposite-side orders against
    the same trade in the same window — that would over-close the
    parent position, leaving a phantom short / settlement_outstanding
    shortfall that has to be cleaned up by hand.
    """
    # Synthetic row support — same prefix convention as the SL/TP
    # endpoint. When list_active_trades emits a row with id="pos-<pid>"
    # (no matching Trade record), closing it means flattening the
    # parent position directly. Without this branch, mobile users hit
    # "Trade not found" when tapping Exit on those rows.
    if trade_id.startswith("pos-"):
        try:
            pid = PydanticObjectId(trade_id[4:])
        except Exception:
            raise HTTPException(status_code=404, detail="Position not found")
        p_synth = await Position.get(pid)
        if (
            p_synth is None
            or p_synth.user_id != user.id
            or p_synth.status != PositionStatus.OPEN
            or p_synth.quantity == 0
        ):
            raise HTTPException(status_code=400, detail="No open position to close")
        action = OrderAction.SELL if p_synth.quantity > 0 else OrderAction.BUY
        full_qty = abs(p_synth.quantity)
        full_lots = max(0.01, full_qty / max(1, p_synth.instrument.lot_size or 1))
        o = await order_service.place_order(
            user=user,
            payload={
                "token": p_synth.instrument.token,
                "action": action.value,
                "order_type": OrderType.MARKET.value,
                "product_type": p_synth.product_type.value,
                "lots": full_lots,
                "force_quantity": full_qty,
                "placed_from": "WEB",
                "is_squareoff": True,
            },
        )
        try:
            fresh = await Position.get(p_synth.id)
            if (
                fresh is not None
                and fresh.status == PositionStatus.CLOSED
                and not fresh.close_reason
            ):
                fresh.close_reason = "USER"
                await fresh.save()
        except Exception:
            pass
        return APIResponse(
            data={
                "order_id": str(o.id),
                "status": o.status.value,
                "closed_lots": full_lots,
                "closed_qty": full_qty,
            }
        )

    try:
        oid = PydanticObjectId(trade_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Trade not found")
    t = await Trade.get(oid)
    if t is None or t.user_id != user.id:
        raise HTTPException(status_code=404, detail="Trade not found")

    # ── Single-flight lock ────────────────────────────────────────────
    # 10 s TTL covers a slow market round-trip; key includes user + trade
    # so different users / different trades never collide. Released on
    # exception by the TTL — no manual cleanup needed.
    from app.core.redis_client import idempotency_check_and_set

    lock_key = f"close_active_trade:{user.id}:{trade_id}"
    if not await idempotency_check_and_set(lock_key, ttl_sec=10):
        raise HTTPException(
            status_code=409,
            detail="A close for this trade is already in flight — try again in a moment.",
        )

    # Find the matching open position. Match by (user, token, product_type)
    # first; if that misses (e.g. product_type enum vs string casing drift
    # between when the trade vs the position was written), fall back to
    # (user, token) alone among OPEN positions. Prevents the "trade in
    # positions but not in active" symptom which was actually a lookup
    # miss, not missing data.
    p = await Position.find_one(
        Position.user_id == user.id,
        Position.instrument.token == t.instrument.token,
        Position.product_type == t.product_type,
        Position.status == PositionStatus.OPEN,
    )
    if p is None:
        p = await Position.find_one(
            Position.user_id == user.id,
            Position.instrument.token == t.instrument.token,
            Position.status == PositionStatus.OPEN,
        )
    if p is None or p.quantity == 0:
        raise HTTPException(
            status_code=400,
            detail="No open position to close this trade against",
        )

    # ── Lifecycle-scoped FIFO leftover for THIS trade ───────────────
    # Same algorithm as list_active_trades: scope to trades since the
    # position's opened_at, walk oldest-first, opposite-side fills
    # consume same-side FIFO. The leftover for the target trade is what
    # we should close.
    from datetime import datetime as _dt

    all_trades = await Trade.find(
        Trade.user_id == user.id,
        Trade.instrument.token == t.instrument.token,
    ).to_list()

    # Attribute by TOKEN unless this token genuinely holds MULTIPLE open
    # positions (legacy fragmentation). One-position-per-token is the invariant
    # now, and a carried MIS->NRML position keeps MIS-typed backing fills — so
    # a product_type-scoped pool would miss them and FIFO the wrong leftover.
    # Mirrors the token-based attribution in list_active_trades above.
    other_open = await Position.find(
        Position.user_id == user.id,
        Position.instrument.token == t.instrument.token,
        Position.status == PositionStatus.OPEN,
    ).count()
    if other_open > 1:
        same_pt = [tr for tr in all_trades if tr.product_type == p.product_type]
        pool = same_pt if same_pt else all_trades
    else:
        pool = all_trades

    is_long = p.quantity > 0

    # Normalize every datetime to aware-UTC before comparing. `executed_at`
    # is sometimes stored naive (older fills) and sometimes aware, and the
    # sentinel `_dt.min/_dt.max` are naive — mixing them raised
    # "can't compare offset-naive and offset-aware datetimes" and 500'd the
    # whole close (operator-flagged: XAUUSD active-trade Exit). `_aware()`
    # makes the comparison total and crash-proof.
    from datetime import timezone as _tz_close

    def _aware(dt):
        if dt is None:
            return _dt.min.replace(tzinfo=_tz_close.utc)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=_tz_close.utc)

    _AWARE_MAX = _dt.max.replace(tzinfo=_tz_close.utc)

    def _fifo_leftover(trade_pool: list, target_id: str) -> float:
        """Walk trades oldest-first, FIFO-consume opposite-side from
        same-side, return the leftover qty for target_id."""
        trade_pool.sort(key=lambda tr: _aware(tr.executed_at))
        fifo: list[tuple[str, float]] = []
        for tr in trade_pool:
            tq = float(tr.quantity)
            is_same = (is_long and tr.action == OrderAction.BUY) or \
                      (not is_long and tr.action == OrderAction.SELL)
            if is_same:
                fifo.append((str(tr.id), tq))
            else:
                remain = tq
                for i, (tid, sq) in enumerate(fifo):
                    if remain <= 0:
                        break
                    consume = min(sq, remain)
                    fifo[i] = (tid, sq - consume)
                    remain -= consume
        for tid, lo in fifo:
            if tid == target_id:
                return max(0.0, lo)
        return 0.0

    # Try lifecycle-scoped first, fall back to all trades if result
    # is zero (opened_at corrupted / admin-edited).
    lifecycle = [
        tr for tr in pool
        if not p.opened_at
        or _aware(tr.executed_at or tr.created_at) >= _aware(p.opened_at)
    ]
    leftover_for_target = _fifo_leftover(lifecycle, trade_id)

    if leftover_for_target <= 1e-9 and abs(float(p.quantity)) > 1e-9:
        # Retry with full history — opened_at may exclude valid trades.
        # Scope to most recent same-side fills summing to |position.qty|
        # to avoid pulling old closed-lifecycle trades.
        need = abs(float(p.quantity))
        recent_same = [
            tr for tr in pool
            if (is_long and tr.action == OrderAction.BUY) or
               (not is_long and tr.action == OrderAction.SELL)
        ]
        recent_same.sort(key=lambda tr: _aware(tr.executed_at), reverse=True)
        accum = 0.0
        recent_ids: set[str] = set()
        for tr in recent_same:
            if accum >= need:
                break
            recent_ids.add(str(tr.id))
            accum += float(tr.quantity)
        # Also include opposite-side trades that are NEWER than the
        # oldest claimed same-side (they are the closing legs for this
        # lifecycle).
        oldest_claimed_time = _AWARE_MAX
        for tr in recent_same:
            if str(tr.id) in recent_ids:
                t_time = _aware(tr.executed_at)
                if t_time < oldest_claimed_time:
                    oldest_claimed_time = t_time
        scoped = [
            tr for tr in pool
            if str(tr.id) in recent_ids or
               _aware(tr.executed_at) >= oldest_claimed_time
        ]
        leftover_for_target = _fifo_leftover(scoped, trade_id)

    # FIFO attribution can read 0 for the tapped trade even though the parent
    # position is genuinely OPEN — so blindly 400-ing here blocks the user
    # from exiting a live position. Root cause (CL73972774 SENSEX26JUN76700PE,
    # 2026-06-24): the position's `opened_at` (07:22:29.598) was stamped a few
    # ms AFTER the opening fill's `executed_at` (07:22:29.584), so the
    # `executed_at >= opened_at` lifecycle filter DROPPED the true opening lot.
    # The lifecycle pool then held only a later same-side BUY 40 + an equal
    # SELL 40, which FIFO-net to zero for the *visible* trade — while the real
    # open 40 (the excluded opener) still stands. Result: "1 open, qty 40" on
    # screen but the Active-tab Exit 400'd with "already closed".
    #
    # We reach this line ONLY after the `p.quantity == 0` guard above, so the
    # position is definitely open. Fall back to closing the tapped trade's own
    # quantity instead of erroring. `close_qty` is clamped to the open position
    # size just below, so this can NEVER over-close, and is_squareoff routes it
    # through the reduce-only validator guard.
    if leftover_for_target <= 1e-9:
        leftover_for_target = abs(float(t.quantity))

    close_qty = min(leftover_for_target, abs(float(p.quantity)))
    close_lots = max(0.01, close_qty / max(1, p.instrument.lot_size or 1))
    action = OrderAction.SELL if p.quantity > 0 else OrderAction.BUY

    import logging as _lg
    _lg.getLogger(__name__).info(
        "close_active_trade",
        extra={
            "user_id": str(user.id),
            "trade_id": trade_id,
            "position_id": str(p.id),
            "trade_original_qty": float(t.quantity),
            "leftover_for_target": leftover_for_target,
            "position_qty": float(p.quantity),
            "close_qty": close_qty,
            "close_lots": close_lots,
        },
    )

    o = await order_service.place_order(
        user=user,
        payload={
            "token": p.instrument.token,
            "action": action.value,
            "order_type": OrderType.MARKET.value,
            "product_type": p.product_type.value,
            "lots": close_lots,
            "force_quantity": close_qty,
            "placed_from": "WEB",
            "is_squareoff": True,
            # Specific-lot close: realize this exit against the TAPPED fill's
            # own entry price, not the position average (operator: "active me
            # 105 close karu to 105 ki hi entry close ho, FIFO/avg mat lagao").
            # Native fill price; only this Active-tab path sets it.
            "cost_basis_override": str(t.price),
        },
    )

    # Stamp USER close_reason if the trade close actually flattened the
    # parent position. Same pattern as the /squareoff endpoint — deferred to
    # AFTER the response (display-only re-fetch + save; not worth 2 DB
    # round-trips on the exit hot path).
    _pid2 = p.id

    async def _stamp_close_reason() -> None:
        try:
            fresh = await Position.get(_pid2)
            if (
                fresh is not None
                and fresh.status == PositionStatus.CLOSED
                and not fresh.close_reason
            ):
                fresh.close_reason = "USER"
                await fresh.save()
        except Exception:
            import logging as _lg

            _lg.getLogger(__name__).exception("close_active_close_reason_stamp_failed")

    # Bulletproof — see /squareoff: never let post-close bookkeeping turn a
    # successful close into a 500 the client reads as "close failed".
    try:
        from app.utils.background import fire_and_forget

        fire_and_forget(_stamp_close_reason(), label="close_active_post")
    except Exception:
        import logging as _lg

        _lg.getLogger(__name__).exception("close_active_post_schedule_failed")

    return APIResponse(
        data={
            "order_id": str(o.id),
            "status": o.status.value,
            "closed_lots": close_lots,
            "closed_qty": close_qty,
        }
    )


async def _fill_open_qty(user_id, t) -> float:
    """Best-effort open (un-closed) qty of the opening fill `t`, via FIFO over
    the token's trades. The exit order's fire-time guard re-checks anyway, so
    an approximate value here is safe."""
    from datetime import datetime as _d, timezone as _tz

    trades = await Trade.find(
        Trade.user_id == user_id, Trade.instrument.token == t.instrument.token
    ).to_list()
    is_long = t.action == OrderAction.BUY

    def _aw(dt):
        if dt is None:
            return _d.min.replace(tzinfo=_tz.utc)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=_tz.utc)

    trades.sort(key=lambda tr: _aw(tr.executed_at))
    fifo: list[list] = []
    for tr in trades:
        same = (is_long and tr.action == OrderAction.BUY) or (
            not is_long and tr.action == OrderAction.SELL
        )
        if same:
            fifo.append([str(tr.id), float(tr.quantity)])
        else:
            remain = float(tr.quantity)
            for pair in fifo:
                if remain <= 0:
                    break
                c = min(pair[1], remain)
                pair[1] -= c
                remain -= c
    for tid, lo in fifo:
        if tid == str(t.id):
            return max(0.0, lo)
    return 0.0


async def _ensure_fill_sl_tp(user, p, t, sl_val, tp_val, set_sl: bool, set_tp: bool) -> None:
    """Per-fill SL/TP as REAL pending exit orders (one per leg) that close ONLY
    this fill's qty via cost_basis_override. Replaces the leg(s) the payload
    addressed: cancels any existing exit order for that leg, then parks a fresh
    LIMIT (target) / SL-M (stop) exit. Clears the position-level SL/TP so the
    risk enforcer never double-fires the whole position."""
    from app.models._base import OrderAction as _OA, OrderType as _OT
    from app.models.order import Order, OrderStatus
    from app.services import order_service as _os

    exit_action = _OA.SELL if p.quantity > 0 else _OA.BUY
    qty = await _fill_open_qty(user.id, t)
    if qty <= 1e-9:
        qty = abs(float(t.quantity))
    lot_size = max(1, int(p.instrument.lot_size or 1))
    lots = qty / lot_size if lot_size else qty
    basis = float(str(t.price))
    group = str(t.id)

    existing = await Order.find(
        Order.user_id == user.id,
        Order.sl_tp_source_trade_id == t.id,
        {"status": {"$in": [OrderStatus.OPEN.value, OrderStatus.PENDING.value, OrderStatus.PARTIAL.value]}},
    ).to_list()
    for o in existing:
        ot = str(getattr(o.order_type, "value", o.order_type) or "").upper()
        is_tp_leg = ot == "LIMIT"
        is_sl_leg = ot in ("SL", "SL_M")
        if (is_tp_leg and set_tp) or (is_sl_leg and set_sl):
            try:
                await _os.cancel(user.id, str(o.id))
            except Exception:  # noqa: BLE001
                pass

    base = {
        "token": p.instrument.token,
        "action": exit_action.value,
        "product_type": str(getattr(p.product_type, "value", p.product_type)),
        "lots": lots,
        "force_quantity": qty,
        "is_squareoff": True,
        "cost_basis_override": str(basis),
        "sl_tp_source_trade_id": str(t.id),
        "oco_group_id": group,
        "placed_from": "WEB",
    }
    if set_tp and tp_val is not None:
        await _os.place_order(
            user=user, payload={**base, "order_type": _OT.LIMIT.value, "price": tp_val}
        )
    if set_sl and sl_val is not None:
        await _os.place_order(
            user=user, payload={**base, "order_type": _OT.SL_M.value, "trigger_price": sl_val}
        )

    # Per-fill mode owns SL/TP now — drop any whole-position bracket so the
    # risk enforcer can't ALSO fire it and double-close.
    if p.stop_loss is not None or p.target is not None:
        p.stop_loss = None
        p.target = None
        await p.save()


@router.put("/active-trades/{trade_id}/sl-tp", response_model=APIResponse[dict])
async def update_active_trade_sl_tp(trade_id: str, payload: dict, user: CurrentUser):
    """SL/TP lives at the position level (FIFO/avg accounting — we don't track
    per-fill stops), so this delegates to the parent position's SL/TP.

    Accepts BOTH a real trade_id AND the synthetic ``pos-<position_id>`` id
    that ``list_active_trades`` emits when an open position has no matching
    trade record (orphaned by an old Reopen, etc.). Without the synthetic
    branch, tapping TP on those rows raised "Trade not found" because
    ``PydanticObjectId("pos-…")`` throws — operator-reported as a mobile
    "TP nahi lag rahi" regression on VOLTAS.
    """
    from bson import Decimal128

    p: Position | None = None
    t: Trade | None = None
    if trade_id.startswith("pos-"):
        # Synthetic row from list_active_trades — operate on the parent
        # position directly. The id payload after the prefix is the real
        # PositionId.
        try:
            pid = PydanticObjectId(trade_id[4:])
        except Exception:
            raise HTTPException(status_code=404, detail="Position not found")
        p = await Position.get(pid)
        if p is None or p.user_id != user.id or p.status != PositionStatus.OPEN:
            raise HTTPException(status_code=404, detail="Position not found")
    else:
        try:
            oid = PydanticObjectId(trade_id)
        except Exception:
            raise HTTPException(status_code=404, detail="Trade not found")
        t = await Trade.get(oid)
        if t is None or t.user_id != user.id:
            raise HTTPException(status_code=404, detail="Trade not found")
        p = await Position.find_one(
            Position.user_id == user.id,
            Position.instrument.token == t.instrument.token,
            Position.product_type == t.product_type,
            Position.status == PositionStatus.OPEN,
        )
        if p is None:
            raise HTTPException(status_code=400, detail="Parent position not open")

    # NOTE: an earlier direction-validation block here referenced two helpers
    # (`_live_ref_price`, `_validate_sl_tp_direction`) that don't exist in this
    # module — so EVERY active-trade SL/TP update 500'd with a NameError
    # (operator-flagged "SL Add" failing). Removed; the SL/TP is set on the
    # parent position below and the risk enforcer treats out-of-direction
    # brackets as immediately-eligible anyway, so a bad value self-corrects.
    # ── Per-segment SL / TP enable gate ───────────────────────────────
    # Mirror the position-level /sl-tp endpoint (update_sl_tp): admin can turn
    # OFF SL and/or TP for a segment (Segment Settings → Limit away → SL/TP
    # on-off). This active-trade endpoint is what the Positions page "SL/TP
    # Add" button calls; it previously bypassed the gate entirely, so a user
    # could still attach an SL/TP on a disabled segment — a NIFTY Index option
    # with "SL enabled = No" got an SL added here and the risk enforcer then
    # auto-fired it (CL30720788 / YOGRAJ, 2026-07-08). Clearing (null/0) stays
    # allowed so a leg set before the toggle was turned off can still be
    # removed. Fails OPEN on resolver error (same as update_sl_tp) so a
    # settings-lookup hiccup never blocks a legitimate SL/TP.
    _setting_sl = "stop_loss" in payload and payload["stop_loss"] not in (None, "", 0, "0")
    _setting_tp = "target" in payload and payload["target"] not in (None, "", 0, "0")
    if _setting_sl or _setting_tp:
        try:
            _eff_wrap = await netting_service.get_effective_settings(
                user.id,
                p.segment_type,
                option_type=_opt_type_from_symbol(p.instrument.symbol),
                symbol=p.instrument.symbol,
            )
            _eff = _eff_wrap.get("settings") or {}
        except Exception:
            _eff = {}
        if _setting_sl and not _eff.get("sl_enabled", True):
            raise HTTPException(status_code=400, detail="Stop Loss is disabled for this segment.")
        if _setting_tp and not _eff.get("tp_enabled", True):
            raise HTTPException(status_code=400, detail="Target (Take Profit) is disabled for this segment.")

    # ── Wrong-side direction guard (shared helper) ────────────────────────
    # THE fix for this endpoint: an SL/TP on the profitable side of the market
    # fills at EXACTLY the untraded leg price → fake P&L (the SILVERM 1-lakh
    # incident). The earlier removed block left this unguarded on the belief
    # that a bad leg "self-corrects" — it self-DESTRUCTS. Reject it here, same
    # guard used by place-order + update_sl_tp. ref = live LTP, else entry;
    # fails open when neither exists. Clearing (null/0) is never blocked.
    _sl_raw = payload.get("stop_loss") if "stop_loss" in payload else None
    _tp_raw = payload.get("target") if "target" in payload else None
    _sl_set = _sl_raw not in (None, "", 0, "0")
    _tp_set = _tp_raw not in (None, "", 0, "0")
    if _sl_set or _tp_set:
        try:
            _dref = float(await market_data_service.get_ltp(p.instrument.token))
        except Exception:
            _dref = 0.0
        if _dref <= 0:
            _dref = float(str(p.avg_price or 0))
        _dir_err = order_validator.bracket_direction_error(
            p.opened_side, _dref,
            sl=_sl_raw if _sl_set else None,
            tp=_tp_raw if _tp_set else None,
        )
        if _dir_err:
            raise HTTPException(status_code=400, detail=_dir_err)

    # ── Per-fill SL/TP via pending exit orders ────────────────────────────
    # A REAL fill gets its own exit order(s) that close ONLY its qty, so an
    # SL/TP set on one lot no longer applies to every lot of the symbol. A
    # synthetic `pos-` row has no backing fill, so it falls back to the
    # whole-position bracket (the only meaningful target there).
    def _to_f(v: Any) -> float | None:
        if v in (None, "", 0, "0"):
            return None
        try:
            return float(str(v))
        except (TypeError, ValueError):
            return None

    if t is not None:
        await _ensure_fill_sl_tp(
            user,
            p,
            t,
            sl_val=_to_f(payload.get("stop_loss")) if "stop_loss" in payload else None,
            tp_val=_to_f(payload.get("target")) if "target" in payload else None,
            set_sl="stop_loss" in payload,
            set_tp="target" in payload,
        )
        _fresh = await Position.get(p.id)
        return APIResponse(data=_pos(_fresh or p))

    if "stop_loss" in payload:
        sl = payload["stop_loss"]
        p.stop_loss = Decimal128(str(sl)) if sl not in (None, "", 0, "0") else None
    if "target" in payload:
        tp = payload["target"]
        p.target = Decimal128(str(tp)) if tp not in (None, "", 0, "0") else None
    await p.save()
    return APIResponse(data=_pos(p))


import time as _pnl_time

# Per-user short-TTL cache for the (heavy) pnl-summary compute. The dashboard
# polls this every 5 s and the positions strip / multiple tabs can hit it
# concurrently — without this, each call re-ran 3 windowed Trade aggregations
# plus an open-position LTP fan-out. A 1.5 s TTL collapses concurrent hits into
# one compute while staying well inside the 5 s poll cadence, so the card never
# looks stale. IMPORTANT: this caches ONLY the aggregate card numbers — the
# live per-row / header M2M on the positions page is recomputed CLIENT-SIDE
# from WebSocket ticks, so it is completely unaffected and stays real-time.
_PNL_SUMMARY_CACHE: dict[str, tuple[float, dict]] = {}
_PNL_SUMMARY_TTL = 1.5


@router.get("/pnl-summary", response_model=APIResponse[dict])
async def positions_pnl_summary(user: CurrentUser):
    """Per-user PnL windows for the dashboard cards (Today / Week / Last week).

    today_pnl     — realised P&L since IST midnight + current open unrealised.
    week_pnl      — same, since the most recent IST Sunday 00:00.
    last_week_pnl — total realised P&L of the previous Sun→Sat window.

    NOTE on FX: ``Position.realized_pnl`` and ``unrealized_pnl`` are stored in
    the instrument's NATIVE currency (USD for crypto/forex). We convert each
    USD-quoted position to INR using the position's locked-at-open USD/INR
    rate (realised) or the live rate (unrealised), matching what ``_pos()``
    sends to the live-positions strip.
    """
    # Short-TTL cache hit — skip the whole heavy recompute (see note above).
    _ck = str(user.id)
    _hit = _PNL_SUMMARY_CACHE.get(_ck)
    if _hit is not None and _hit[0] > _pnl_time.monotonic():
        return APIResponse(data=_hit[1])

    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    IST = _tz(_td(hours=5, minutes=30))
    now_ist = _dt.now(IST)
    today_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    days_back = (now_ist.weekday() + 1) % 7
    week_start_ist = today_start_ist - _td(days=days_back)
    last_week_start_ist = week_start_ist - _td(days=7)
    last_week_end_ist = week_start_ist  # exclusive

    today_start = today_start_ist.astimezone(_tz.utc)
    week_start = week_start_ist.astimezone(_tz.utc)
    last_week_start = last_week_start_ist.astimezone(_tz.utc)
    last_week_end = last_week_end_ist.astimezone(_tz.utc)

    current_usd_inr = market_data_service.get_usd_inr_rate()

    def _is_usd(p: Position) -> bool:
        return market_data_service.is_usd_quoted_segment(p.segment_type) or \
            market_data_service.is_usd_quoted_segment(p.instrument.segment)

    def _realised_inr(p: Position) -> float:
        raw = float(str(p.realized_pnl))
        if not _is_usd(p):
            return raw
        rate = (
            float(str(p.open_usd_inr_rate))
            if p.open_usd_inr_rate is not None
            else current_usd_inr
        )
        return raw * rate

    async def _realised_in(window_start, window_end=None) -> float:
        # Use trades (closing fills with pnl_inr set) as the source of truth —
        # same as the admin trading.py pnl-summary endpoint so user and admin
        # numbers always agree.  The old position-based approach (summing
        # position.realized_pnl where closed_at OR updated_at fell in window)
        # had two bugs: (1) it was gross P&L without brokerage deduction, and
        # (2) the updated_at branch dragged in realized P&L from open positions
        # that were touched by the risk enforcer / tick, causing massive
        # over-counting on accounts with many open positions.
        rng: dict[str, Any] = {"$gte": window_start}
        if window_end is not None:
            rng["$lt"] = window_end
        trades = await Trade.find(
            {
                "user_id": user.id,
                "executed_at": rng,
                "pnl_inr": {"$ne": None},
                "price": {"$gt": 0},
            }
        ).to_list()
        gross = sum(float(str(t.pnl_inr)) for t in trades if t.pnl_inr is not None)
        charges = sum(
            float(str(getattr(t, "total_charges", None) or t.brokerage or 0))
            for t in trades
        )
        return gross - charges

    today_realised = await _realised_in(today_start)
    week_realised = await _realised_in(week_start)
    last_week_realised = await _realised_in(last_week_start, last_week_end)

    open_positions = await Position.find(
        {"user_id": user.id, "status": PositionStatus.OPEN.value}
    ).to_list()

    # Parallel LTP + unrealised refresh — same optimisation as /open above.
    # Sequential awaits across N open positions added linear latency to a
    # 10-second-polled endpoint; gather keeps total wall time ≈ slowest leg.
    if open_positions:
        ltps = await asyncio.gather(
            *[market_data_service.get_ltp(p.instrument.token) for p in open_positions],
            return_exceptions=True,
        )
        await asyncio.gather(
            *[
                position_service.refresh_unrealized_pnl(
                    p, ltp if not isinstance(ltp, Exception) else 0
                )
                for p, ltp in zip(open_positions, ltps)
            ],
            return_exceptions=True,
        )

    total_unrealised = 0.0
    for p in open_positions:
        # Recompute from canonical-lot qty rather than reading the stored
        # `unrealized_pnl`. That stored value was written by
        # `refresh_unrealized_pnl` using `p.quantity` directly, which is
        # wrong for legacy positions where qty was saved as lots. The
        # frontend rows show the canonical number; this summary must agree.
        eff_qty, _, _ = _effective_qty(p)
        avg = float(str(p.avg_price))
        ltp_native = float(str(p.ltp))
        raw = (ltp_native - avg) * eff_qty
        if _is_usd(p):
            raw *= current_usd_inr
        total_unrealised += raw

    _data = {
        "today_pnl": round(today_realised + total_unrealised, 2),
        "today_realised": round(today_realised, 2),
        "open_unrealised": round(total_unrealised, 2),
        "week_pnl": round(week_realised + total_unrealised, 2),
        "week_realised": round(week_realised, 2),
        "last_week_pnl": round(last_week_realised, 2),
        "today_start": today_start.isoformat(),
        "week_start": week_start.isoformat(),
        "last_week_start": last_week_start.isoformat(),
        "last_week_end": last_week_end.isoformat(),
        "usd_inr_rate": round(current_usd_inr, 4),
    }
    # Cache for the next 1.5 s so concurrent / rapid polls reuse this compute.
    _PNL_SUMMARY_CACHE[_ck] = (_pnl_time.monotonic() + _PNL_SUMMARY_TTL, _data)
    return APIResponse(data=_data)


@router.post("/squareoff-all", response_model=APIResponse[dict])
async def squareoff_all(user: CurrentUser):
    from datetime import datetime as _dt, timezone as _tz
    from app.services import netting_service as _ns
    from app.core.redis_client import idempotency_check_and_set as _idem

    # User-level lock: prevent two simultaneous close-all calls (e.g. double-tap
    # on the "Close All" button while the first request is still iterating).
    _all_lock = f"squareoff_all:{user.id}"
    if not await _idem(_all_lock, ttl_sec=20):
        raise HTTPException(
            status_code=409,
            detail="A close-all is already in progress — wait a moment.",
        )

    risk = (await _ns.get_effective_risk(str(user.id)))["settings"]
    profit_min = int(risk.get("profitTradeHoldMinSeconds") or 0)
    loss_min = int(risk.get("lossTradeHoldMinSeconds") or 0)

    rows = await position_service.list_open(user.id)
    placed = 0
    blocked = 0
    blocked_by_market_closed = 0
    for r in rows:
        if r.quantity == 0:
            continue
        # ── Market-hours gate ──────────────────────────────────────
        # Defence-in-depth: the apk pre-filters positions whose market
        # is closed before issuing per-position squareoff calls (so the
        # bulk endpoint mostly receives only tradable rows). The server
        # still enforces it here for web / direct-API callers and so
        # the user can't bypass via curl. Crypto + Forex always pass
        # (24/7 / 24x5).
        if not _is_segment_market_open_now(r.segment_type):
            blocked_by_market_closed += 1
            continue
        # Per-row hold-time gate: skip (don't fail the whole batch) when the
        # row is too young. The user gets a count of how many were blocked.
        if (profit_min or loss_min) and r.opened_at:
            opened = r.opened_at if r.opened_at.tzinfo else r.opened_at.replace(tzinfo=_tz.utc)
            held = (_dt.now(_tz.utc) - opened).total_seconds()
            # Same close-side mark as the position card (see single-close
            # path) — bid for a long, ask for a short — so the profit/loss
            # decision matches what the user sees, not a lagging LTP.
            cur_pnl = 0.0
            try:
                _ltp = await market_data_service.get_ltp(r.instrument.token)
                await position_service.refresh_unrealized_pnl(r, to_decimal(_ltp or 0))
                cur_pnl = float(str(r.unrealized_pnl or 0))
            except Exception:
                try:
                    cur_pnl = float(str(r.unrealized_pnl or 0))
                except Exception:
                    cur_pnl = 0.0
            floor = profit_min if cur_pnl >= 0 else loss_min
            if floor and held < floor:
                blocked += 1
                continue
        # Per-position lock inside the loop: if the same position is also being
        # closed by a direct /squareoff call in parallel, skip it here so we
        # don't fire two sell orders for the same row.
        _pos_lock = f"squareoff_position:{user.id}:{r.id}"
        if not await _idem(_pos_lock, ttl_sec=10):
            blocked += 1
            continue

        action = OrderAction.SELL if r.quantity > 0 else OrderAction.BUY
        qty = abs(r.quantity)
        lots = max(1, qty // max(1, r.instrument.lot_size or 1))
        try:
            await order_service.place_order(
                user=user,
                payload={
                    "token": r.instrument.token,
                    "action": action.value,
                    "order_type": OrderType.MARKET.value,
                    "product_type": r.product_type.value,
                    "lots": lots,
                    "force_quantity": qty,
                    "is_squareoff": True,
                    "placed_from": "WEB",
                },
            )
            placed += 1
            # Stamp USER close_reason on every row that actually closed.
            # Done per-row so partial flatten failures don't break the rest.
            try:
                fresh = await Position.get(r.id)
                if (
                    fresh is not None
                    and fresh.status == PositionStatus.CLOSED
                    and not fresh.close_reason
                ):
                    fresh.close_reason = "USER"
                    await fresh.save()
            except Exception:
                pass
        except Exception:
            continue
    return APIResponse(
        data={
            "squared_off": placed,
            "total": len(rows),
            "blocked_by_hold_time": blocked,
            "blocked_by_market_closed": blocked_by_market_closed,
        }
    )


# ── Holdings ──────────────────────────────────────────────────────────
holdings_router = APIRouter(prefix="/holdings", tags=["user-holdings"])


@holdings_router.get("", response_model=APIResponse[list[HoldingOut]])
async def list_holdings(user: CurrentUser):
    rows = await position_service.list_holdings(user.id)
    out = []
    for r in rows:
        ltp = await market_data_service.get_ltp(r.instrument.token)
        from bson import Decimal128
        r.ltp = Decimal128(str(ltp))
        out.append(
            {
                "id": str(r.id),
                "user_id": str(r.user_id),
                "symbol": r.instrument.symbol,
                "exchange": str(r.instrument.exchange),
                "instrument_token": r.instrument.token,
                "quantity": r.quantity,
                "avg_price": str(r.avg_price),
                "ltp": str(r.ltp),
                "invested_value": str(r.invested_value),
                "current_value": str(r.current_value),
                "pnl": str(r.pnl),
                "pnl_percentage": r.pnl_percentage,
            }
        )
    return APIResponse(data=out)
