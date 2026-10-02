"""Trading-domain request/response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PlaceOrderRequest(BaseModel):
    token: str
    action: str  # BUY / SELL
    order_type: str  # MARKET / LIMIT / SL / SL_M
    product_type: str  # MIS / CNC / NRML
    lots: float = Field(ge=0.001, le=100000)  # fractional for crypto/forex
    # Exact contract quantity, used when the client's order panel is in
    # QTY (units) mode. When > 0 it wins over `lots × lot_size` in
    # order_service so a typed "1 qty" is stored verbatim instead of being
    # round-tripped qty→lots→qty (1/30 × 30 = 0.99). Validation still runs
    # on the derived lots, so it can't bypass min-lot / margin / max caps.
    force_quantity: float | None = Field(default=None, ge=0, le=1_000_000_000)
    price: float | None = 0
    trigger_price: float | None = 0
    validity: str = "DAY"
    is_amo: bool = False
    # Bracket order legs — auto-place opposite-side SL & target after entry
    stop_loss: float | None = None
    target: float | None = None
    # The bid/ask the user saw on the order panel when they clicked. The
    # matching engine uses this as the MARKET fill price so ENTRY exactly
    # matches what the trader saw — eliminating the few-tick mismatch from
    # bid/ask drift between click and server-side fill. Capped against
    # current bid/ask to prevent tampering; see matching_engine.
    expected_price: float | None = None


class ModifyOrderRequest(BaseModel):
    lots: float | None = None
    price: float | None = None
    trigger_price: float | None = None


class OrderOut(BaseModel):
    id: str
    order_number: str
    user_id: str
    symbol: str
    exchange: str
    segment: str
    # Real contract-expiry date (ISO) from the Instrument collection.
    expiry: str | None = None
    token: str | None = None
    instrument_token: str | None = None
    action: str
    order_type: str
    product_type: str
    validity: str
    lots: float
    quantity: float
    filled_quantity: float
    pending_quantity: float
    price: str
    trigger_price: str
    average_price: str
    status: str
    rejection_reason: str | None = None
    is_amo: bool
    margin_blocked: str
    brokerage: str
    other_charges: str
    bracket_stop_loss: str | None = None
    bracket_target: str | None = None
    # Realized P&L in INR for this order, frozen at fill time. Populated
    # only on closing fills (the History tab reads it to render fixed-in-INR
    # P&L instead of floating against live LTP). None for opening fills,
    # cancelled, or pending orders.
    pnl_inr: str | None = None
    created_at: datetime
    executed_at: datetime | None = None
    cancelled_at: datetime | None = None
    updated_at: datetime | None = None


class TradeOut(BaseModel):
    id: str
    trade_number: str
    order_id: str
    user_id: str
    symbol: str
    exchange: str
    action: str
    quantity: float
    price: str
    value: str
    total_charges: str
    net_amount: str
    executed_at: datetime


class PositionOut(BaseModel):
    id: str
    user_id: str
    symbol: str
    # Full exchange contract identifier (e.g. `SENSEX25MAY75000CE`).
    # Closed-tab mobile cards show this on the sub-line so users can see
    # which option contract a closed row belonged to without tapping in.
    trading_symbol: str | None = None
    exchange: str
    # Real contract-expiry date (ISO `YYYY-MM-DD`) from the Instrument
    # collection, so the UI shows the true expiry (SILVER26SEPFUT → 2026-09-04)
    # instead of parsing the symbol (whose "26" is the YEAR, not the day).
    expiry: str | None = None
    # Per-position Carry-Forward toggle (feature gated per-admin).
    carry_forward_enabled: bool = False
    user_carry_forward: bool = False
    segment_type: str
    product_type: str
    quantity: float
    # Peak abs(quantity) recorded over this position's lifecycle. Preserved
    # across full close so the Closed/History tab can show the size the
    # user actually held (where ``quantity`` is 0 on a flat position).
    opening_quantity: float | None = None
    # Original direction the user took. Stable across a full close (where
    # ``quantity`` flips to 0) so the Closed-tab card can render "BUY ..."
    # vs "SELL ..." correctly. None for legacy rows without the field.
    opened_side: str | None = None
    # Lot accounting echoed from the embedded instrument snapshot. Without
    # these declared on the response model, FastAPI's response filter
    # strips them from the JSON even though the serializer dict includes
    # them — and the positions table then divides by 1 and renders MCX
    # rows as e.g. "3 lots" when the real lot count is 0.1.
    lots: float | None = None
    lot_size: int | None = None
    avg_price: str
    ltp: str
    realized_pnl: str
    unrealized_pnl: str
    margin_used: str
    # Carry-forward (overnight) margin requirement for THIS position,
    # computed in `/positions/open` via the segment-settings cascade
    # (notional ÷ overnight_leverage, or lots × overnight_fixed_margin
    # in fixed mode). FastAPI's response_model filter strips any field
    # that isn't declared here, even when the serializer dict has it —
    # which is exactly what masked this for two days: backend computed
    # holding=2882.10 for an OPT BUY but the response shipped without
    # the field, so the user-card's `holdingMarginFor` fell back to
    # `margin_used` and rendered USED == HOLDING. Declare it.
    holding_margin: str | None = None
    stop_loss: str | None = None
    target: str | None = None
    # Snapshot of SL/TP at close time — present only on CLOSED rows.
    # The Closed-tab card on the user side reads these so the user
    # can still see "trade had SL ₹X / TP ₹Y" even though the live
    # stop_loss / target fields were wiped at close to keep future
    # reopens clean. Same response-model-stripping rule that bit
    # holding_margin: declared here explicitly so FastAPI doesn't drop
    # them on serialisation.
    close_stop_loss: str | None = None
    close_target: str | None = None
    status: str
    opened_at: str | None = None
    closed_at: str | None = None
    # Compact tag explaining how a position was flattened. Set by the
    # squareoff path that actually flips status → CLOSED. Known values:
    # SL_HIT / TP_HIT / STOP_OUT / USER / AUTO. Rendered on the Closed
    # tab so users see "Closed by SL" for bracket auto-fires that
    # happened while they were away from the app.
    close_reason: str | None = None
    instrument_token: str | None = None
    # Sum of brokerage across every trade that's part of this open
    # position. Without this declaration FastAPI's response_model filter
    # strips the field the positions endpoint already computes — the COMM
    # column then renders ₹0.00 even for a charged trade. Same pattern as
    # `lots` / `lot_size` / `pnl_inr` above.
    charges: str | None = None
    # USD-quoted segments (Infoway: crypto / forex / metals / energy)
    # — these are populated in /positions/open's serialiser dict but
    # were silently stripped by the response_model filter, so the
    # mobile cards on those segments never showed the FX rate banner.
    # Declaring them lets the frontend render "USD/INR @ 83.21" next to
    # the row again.
    currency_quote: str | None = None
    open_usd_inr_rate: str | None = None
    current_usd_inr_rate: str | None = None


class HoldingOut(BaseModel):
    id: str
    user_id: str
    symbol: str
    exchange: str
    quantity: float
    avg_price: str
    ltp: str
    invested_value: str
    current_value: str
    pnl: str
    pnl_percentage: float


class WalletSummary(BaseModel):
    # ── Dabba / CFD KPIs (optional — wallet_service.summary() always
    #    computes these, but they were previously stripped here because the
    #    schema didn't declare them. Both the web WalletStrip and the APK
    #    TradeSheet read `bal/equity/margin/free` directly, so declaring them
    #    lets the authoritative server values pass through instead of each
    #    client falling back to its own (and the APK's was wrong) formula).
    #    `bal = available + used`, `equity = bal + float_pnl`,
    #    `free = bal - margin`, `margin_level_pct = equity / margin * 100`.
    bal: str | None = None
    equity: str | None = None
    margin: str | None = None
    free: str | None = None
    margin_level_pct: float | None = None
    open_unrealized_pnl: str | None = None
    # ── Legacy fields (kept for older clients) ───────────────────────
    available_balance: str
    used_margin: str
    realized_pnl: str
    unrealized_pnl: str
    credit_limit: str
    credit: str = "0"  # bonus credit pool (Bonus Management); "0" when off
    bonus_free: str = "0"      # bonus available to trade (raw credit − locked)
    bonus_locked: str = "0"    # bonus tied up in open margin
    available_free: str = "0"  # real free cash, floored at 0 (never negative)
    settlement_outstanding: str = "0"
    total_deposits: str
    total_withdrawals: str
    total_brokerage: str
    total_charges: str


class InstrumentOut(BaseModel):
    token: str
    symbol: str
    trading_symbol: str
    name: str
    exchange: str
    segment: str
    instrument_type: str
    lot_size: int
    tick_size: str
    expiry: str | None = None
    strike: str | None = None
    option_type: str | None = None
    is_active: bool
    is_tradable: bool


class QuoteOut(BaseModel):
    token: str
    ltp: float
    change: float
    change_pct: float
    open: float
    high: float
    low: float
    prev_close: float
    volume: float  # crypto/forex have fractional contract volumes (e.g. 5.21241 BTC)
    bid: float
    ask: float
    depth: dict[str, Any] | None = None
    # "zerodha" / "infoway" / None (mock). Helps the UI show a provider badge
    # so users can verify whether they're seeing live exchange data or fallback.
    source: str | None = None
    # Last-known price for DISPLAY when the live feed is down / market closed
    # (e.g. spot gold over the weekend). `ltp`/`bid`/`ask` above stay 0 in
    # that case — `last_ltp` is a reference only and is NEVER used for order
    # execution (the matching engine reads `ltp`, which stays 0 → no fill).
    last_ltp: float | None = None
    last_ts: int | None = None
    stale: bool | None = None


class WatchlistOut(BaseModel):
    id: str
    name: str
    sort_order: int
    is_default: bool
    items: list[dict[str, Any]] = []


class WatchlistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class WatchlistAddItem(BaseModel):
    token: str


class DepositCreate(BaseModel):
    amount: float = Field(gt=0)
    payment_mode: str = "UPI"
    utr_number: str | None = None
    screenshot_url: str | None = None
    user_remark: str | None = None
    bank_account_id: str | None = None
    # Crypto deposit (payment_mode == "CRYPTO"). The on-chain tx hash is the
    # proof the admin approves on (screenshot optional in that mode).
    crypto_asset: str | None = None
    crypto_network: str | None = None
    crypto_address: str | None = None
    crypto_tx_hash: str | None = None
    # Client-supplied idempotency token — the server dedups on it so a
    # double / triple click or a retried-after-timeout request creates only
    # ONE deposit. Optional; the server falls back to a fresh UUID.
    idempotency_key: str | None = None


class WithdrawalCreate(BaseModel):
    amount: float = Field(gt=0)
    bank: dict[str, Any]
    remarks: str | None = None
    # See DepositCreate — guarantees one request per intended withdrawal.
    idempotency_key: str | None = None


class AlertCreate(BaseModel):
    token: str
    alert_type: str = "LTP_ABOVE"
    target_price: float | None = None
    target_percent: float | None = None
    note: str | None = None
