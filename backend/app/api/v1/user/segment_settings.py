"""User-side segment-settings preview.

The order panel calls this when the user picks an instrument so the UI can
show the EXACT lot/quantity/value/margin/brokerage the server is going to
enforce — same numbers the validator and matching engine resolve. Surfacing
them up-front means users see what's enforced before they submit, instead of
discovering it when their order gets rejected.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.core.config import settings
from app.core.dependencies import CurrentUser
from app.schemas.common import APIResponse
from app.services import instrument_service, market_data_service, netting_service

router = APIRouter(prefix="/segment-settings", tags=["user-segment-settings"])


@router.get("/effective", response_model=APIResponse[dict])
async def get_effective_for_instrument(
    user: CurrentUser,
    token: str = Query(..., description="Instrument token"),
    action: str = Query(default="BUY", regex="^(BUY|SELL)$"),
    product_type: str = Query(default="MIS", regex="^(MIS|NRML|CNC)$"),
):
    """Resolve the same netting/segment-settings stack the server will apply
    when an order with these inputs is placed. Returns lot limits, the
    margin % that'll actually be charged, the commission scheme, and the
    static caps (per-order qty, per-order value, max-each-lot, etc.)."""
    # Try MongoDB first, then auto-create from Zerodha cache (same helper
    # the instrument-detail endpoint uses).
    from app.api.v1.user.instruments import _find_or_create_from_zerodha

    instrument = await _find_or_create_from_zerodha(token)
    if instrument is None:
        raise HTTPException(status_code=404, detail=f"Instrument {token} not found")

    # option_type selects the admin's per-side Opt Buy / Opt Sell margin in
    # the resolver. `instrument.option_type` is None for option rows whose
    # field wasn't populated during the Zerodha sync, so fall back to
    # deriving CE/PE from the symbol suffix — same as the order validator /
    # matching engine. Without this the option-chain card's margin preview
    # showed the generic segment (Times) margin (e.g. ₹273) instead of the
    # configured Opt-Sell Fixed ₹/lot, so the mobile SELL button didn't
    # reflect the real (much higher) margin.
    option_type = (
        instrument.option_type.value
        if instrument.option_type and hasattr(instrument.option_type, "value")
        else None
    )
    if option_type is None:
        _derived = instrument_service.infer_instrument_type_from_symbol(instrument.symbol)
        if _derived in ("CE", "PE"):
            option_type = _derived

    resolved: dict[str, Any] = await netting_service.get_effective_settings(
        user.id,  # type: ignore[arg-type]
        instrument.segment,
        action=action,
        option_type=option_type,
        product_type=product_type,
        symbol=instrument.symbol,
    )

    s = dict(resolved["settings"])

    # Expiry-day override — mirror the mode-aware translation the validator
    # does at order time so the APK / web KPI tile + pre-flight check see
    # the SAME margin number the server will enforce. Without this the
    # panel showed e.g. ₹2,068 for CRUDEOIL26MAYFUT on its expiry day
    # while the server actually enforced ~₹51L (notional × 5 from the
    # old expiry-bug). Even after the validator fix this endpoint still
    # needs to surface the expiry-day numbers so admins who set a
    # stricter `expiryDayIntradayMargin` see it reflected on the panel.
    from app.services.instrument_service import effective_expiry as _effective_expiry
    from app.utils.time_utils import now_ist as _now_ist

    _expiry = _effective_expiry(instrument)
    is_expiry_today = bool(_expiry and _expiry == _now_ist().date())
    if is_expiry_today:
        expiry_value = float(
            s.get("expiry_intraday_margin")
            or s.get("margin_percentage")
            or s.get("leverage")
            or 100.0
        )
        expiry_as_percent = bool(s.get("expiry_margin_as_percent", True))
        seg_mode = (s.get("margin_calc_mode") or "").lower()
        if not expiry_as_percent:
            s["margin_calc_mode"] = "fixed"
            s["fixed_margin_per_lot"] = expiry_value
            s["margin_percentage"] = 0.0
            s["leverage"] = 1.0
        elif seg_mode == "times":
            s["leverage"] = max(1.0, expiry_value)
            s["margin_percentage"] = 100.0
            s["fixed_margin_per_lot"] = 0.0
        elif seg_mode == "fixed":
            s["margin_calc_mode"] = "fixed"
            s["fixed_margin_per_lot"] = expiry_value
            s["margin_percentage"] = 0.0
            s["leverage"] = 1.0
        else:
            s["margin_percentage"] = expiry_value
            s["leverage"] = 1.0
            s["fixed_margin_per_lot"] = 0.0

    # Lot size resolution — canonical tables win over whatever's on the
    # Instrument row. Two sources depending on segment family:
    #   • Index F&O (CE/PE/FUT)  → `index_lots.get_index_lot_size`
    #       (NIFTY=75, BANKNIFTY=35, SENSEX=20 …; revises quarterly)
    #   • Infoway-fed instruments (crypto / forex / spot metals /
    #     spot energy)         → `infoway_lots.get_infoway_lot_size`
    #       (BTCUSD=100, EURUSD=100000, XAUUSD=100, USOIL=1000 …)
    #
    # Previously only the F&O branch ran, so the `lot_size` returned for
    # crypto/forex was whatever stale value sat on Instrument (often 1
    # for legacy auto-created rows). That left the APK's LOT⇄QTY toggle
    # as a no-op on BTCUSD / EURUSD because conversion read this field
    # and got `1`. Now both paths resolve so the toggle has a real
    # multiplier to work with.
    from app.models._base import InstrumentType
    from app.services.index_lots import get_index_lot_size
    from app.services.infoway_lots import get_infoway_lot_size
    from app.services.market_data_service import is_infoway_lot_segment

    canonical_lot: int | None = None
    if instrument.instrument_type in (InstrumentType.CE, InstrumentType.PE, InstrumentType.FUT):
        canonical_lot = get_index_lot_size(instrument.symbol, instrument.name)
    elif is_infoway_lot_segment(instrument.segment):
        canonical_lot = get_infoway_lot_size(
            instrument.symbol, instrument.segment
        )
    effective_lot_size = canonical_lot or instrument.lot_size or 1

    # Lazy self-heal: if the stored lot is wrong, persist the canonical
    # value so subsequent reads (order placement, positions enrichment,
    # admin views) all see the right number without depending on the
    # startup backfill having run.
    if canonical_lot and int(instrument.lot_size or 0) != canonical_lot:
        try:
            instrument.lot_size = canonical_lot
            await instrument.save()
        except Exception:
            pass

    # ── Daily price band (upper / lower circuit) ─────────────────────
    # Day-cached per instrument, so this poll costs a Redis GET at most.
    # Fails open to (None, None) — a band lookup problem must never make the
    # ticket look locked. `circuit_locked_side` is derived from the LIVE
    # price against the band: at the upper edge only SELL may OPEN, at the
    # lower edge only BUY. The UI still has to apply the exit exemption on
    # top (a reducing order is always allowed) — see the ticket.
    _circ_lower = _circ_upper = None
    _circ_side: str | None = None
    if settings.CIRCUIT_BANDS_ENABLED:
        try:
            from app.services import circuit_service as _circ

            _circ_lower, _circ_upper = await _circ.get_circuit_band(instrument)
            if _circ_lower is not None or _circ_upper is not None:
                _ltp = await market_data_service.get_ltp(instrument.token)
                if _ltp and _ltp > 0:
                    if _circ_upper is not None and _ltp >= _circ_upper:
                        _circ_side = "UPPER"
                    elif _circ_lower is not None and _ltp <= _circ_lower:
                        _circ_side = "LOWER"
        except Exception:
            _circ_lower = _circ_upper = None
            _circ_side = None

    # Trim down to the fields the OrderPanel actually displays — keeps the
    # response payload small for a 3× / second poll.
    out = {
        "segment_type": instrument.segment,
        "lot_size": effective_lot_size,
        "allow": s.get("allow"),
        # Lot limits
        "min_lot": s.get("min_lot"),
        "max_lot": s.get("max_lot"),
        "order_lot": s.get("order_lot"),
        "intraday_lot_limit": s.get("intraday_lot_limit"),
        "holding_lot_limit": s.get("holding_lot_limit"),
        "max_each_lot": s.get("max_each_lot"),
        "otm_max_each_lot": s.get("otm_max_each_lot"),
        # Quantity caps (mainly for equity)
        "min_qty": s.get("min_qty"),
        "per_order_qty": s.get("per_order_qty"),
        "max_qty_per_script": s.get("max_qty_per_script"),
        # Notional caps
        "max_value": s.get("max_value"),
        # Margin / leverage that'll actually be charged for this side+product
        "margin_percentage": s.get("margin_percentage"),
        "leverage": s.get("leverage"),
        # Mode + flat-rupees-per-lot drive the OrderPanel's margin tile.
        # When mode is "fixed" and fixed_margin_per_lot > 0, the UI shows
        # the flat value directly and skips its lot_size × price math.
        "margin_calc_mode": s.get("margin_calc_mode"),
        "fixed_margin_per_lot": s.get("fixed_margin_per_lot"),
        # Carry-forward (overnight) equivalents — let the OrderPanel show
        # both Intraday + Carry-forward margin tiles side-by-side without
        # the frontend having to guess (the old hardcoded `intraday × 1.4`
        # was wrong for every non-NSE-equity segment). For intraday-only
        # segments (Forex / Crypto / spot Commodity) these match the
        # intraday numbers — see netting_service for the source-of-truth
        # rules.
        "overnight_margin_percentage": s.get("overnight_margin_percentage"),
        "overnight_leverage": s.get("overnight_leverage"),
        "overnight_fixed_margin_per_lot": s.get("overnight_fixed_margin_per_lot"),
        # Commission preview
        "commission_type": s.get("commission_type"),
        "commission_value": s.get("commission_value"),
        "min_brokerage": s.get("min_brokerage"),
        "charge_on": s.get("charge_on"),
        # Risk gates
        "limit_percentage": s.get("limit_percentage"),
        "limit_within_day_range": s.get("limit_within_day_range"),
        "block_pending_orders": s.get("block_pending_orders"),
        "stop_loss_mandatory": s.get("stop_loss_mandatory"),
        # Per-segment bracket toggles — let the OrderPanel hide / disable
        # the SL / TP inputs when the admin has turned a leg off for this
        # segment. Backend still enforces (validator + update endpoint);
        # this is purely a UX convenience so the field is greyed out.
        "sl_enabled": s.get("sl_enabled", True),
        "tp_enabled": s.get("tp_enabled", True),
        "selling_overnight": s.get("selling_overnight"),
        # Broker spread (per-user, pool-aware) — the OrderPanel re-derives
        # the displayed BUY/SELL from the mid using these, because the live
        # market feed (`/ws/marketdata`) is a single PUBLIC broadcast and
        # can't carry a per-admin spread. `s` came from get_effective_settings
        # which already walks USER → SUB-ADMIN → BROKER → SUPER-ADMIN → base,
        # so this is THIS user's pool spread. Mirrors the server-side
        # execution markup in matching_engine.execute_market_order.
        "spread_pips": s.get("spread_pips"),
        "spread_type": s.get("spread_type"),
        # ── Daily price band (upper / lower circuit) ─────────────────
        # Piggy-backed on this call — the order ticket already fetches it
        # per instrument, so the band costs no extra round-trip. Either
        # side may be null (no band, non-band exchange, or the feed didn't
        # report one) and the UI MUST treat null as "no limit", never as 0.
        # `circuit_locked_side` is the derived directional lock the banner
        # renders: at UPPER only SELL may open, at LOWER only BUY.
        # The backend validator remains authoritative; this is the
        # pre-flight mirror so the user sees the lock before tapping.
        "lower_circuit": (float(_circ_lower) if _circ_lower is not None else None),
        "upper_circuit": (float(_circ_upper) if _circ_upper is not None else None),
        "circuit_locked_side": _circ_side,
        # Source attribution so the UI can show "Override applied"
        "sources": resolved.get("sources", {}),
        # Expiry-day flag — frontend can tag the margin tile with a
        # warning when today's number differs from the usual tier.
        "is_expiry_today": is_expiry_today,
        # ── Diagnostic: prove which build/resolution is running ───────
        # Lets the user (or us) inspect in DevTools whether the backend
        # actually applied the Times-mode-symmetric-leverage patch. If
        # `times_mode_symmetric_leverage` is missing from the payload, the
        # running process is on an OLD build — frontend reload alone won't
        # fix the margin, the Python service has to be restarted.
        "_resolver_build": "times_mode_symmetric_leverage_v3_expiry_aware",
    }
    return APIResponse(data=out)


@router.get("/overview", response_model=APIResponse[list[dict]])
async def segments_overview(user: CurrentUser):
    """Per-segment brokerage + leverage the way THIS user is set up — the same
    resolved (GLOBAL → TEMPLATE → BROKER → USER) stack the engine enforces.

    Feeds the profile page's "Brokerage details" + "Leverage" cards: each row
    carries the commission scheme and the intraday / carry-forward (overnight)
    leverage for one segment. Resolved in parallel across all 20 segments.
    """
    import asyncio

    from app.models._base import ALL_SEGMENTS

    async def _one(seg) -> dict:
        seg_name = seg.value
        action = "SELL" if seg_name.endswith("_SELL") else "BUY"
        product = "MIS"
        try:
            resolved = await netting_service.get_effective_settings(
                user.id,  # type: ignore[arg-type]
                seg_name,
                action=action,
                product_type=product,
            )
            s = resolved.get("settings", {})
        except Exception:
            s = {}

        def _num(v):
            try:
                return float(v) if v is not None else None
            except Exception:
                return None

        return {
            "segment": seg_name,
            "allow": bool(s.get("allow", True)),
            # Brokerage
            "commission_type": s.get("commission_type"),
            "commission_value": _num(s.get("commission_value")),
            "min_brokerage": _num(s.get("min_brokerage")),
            "charge_on": s.get("charge_on"),
            # Leverage / margin
            "margin_calc_mode": s.get("margin_calc_mode"),
            "leverage": _num(s.get("leverage")),
            "overnight_leverage": _num(s.get("overnight_leverage")),
            "margin_percentage": _num(s.get("margin_percentage")),
            "overnight_margin_percentage": _num(s.get("overnight_margin_percentage")),
            "fixed_margin_per_lot": _num(s.get("fixed_margin_per_lot")),
            "overnight_fixed_margin_per_lot": _num(s.get("overnight_fixed_margin_per_lot")),
        }

    rows = await asyncio.gather(*[_one(seg) for seg in ALL_SEGMENTS])
    return APIResponse(data=list(rows))


@router.get("/inactive", response_model=APIResponse[list[str]])
async def list_inactive_admin_rows(user: CurrentUser):
    """Names of admin-matrix rows currently flagged `Block → isActive = No`.

    The user-side InstrumentsPanel uses this to hide whole asset-class
    chips (NSE EQ, MCX FUT, …) for segments the broker has paused — so
    the trader never even sees the chip, let alone an empty results
    list.

    Pool-aware: passes `user.id` so a sub-admin's "Block → No" reaches
    their pool members. Without this, the function returned only the
    base + super-admin overrides, and a sub-admin's block was invisible
    to the user-side chip filter.
    """
    rows = await netting_service.inactive_admin_rows(user_id=user.id)
    return APIResponse(data=sorted(rows))
