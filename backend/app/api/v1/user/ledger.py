"""User ledger — wallet transactions formatted as a running ledger.

Each row carries:
  • `type` — machine-readable transaction_type (DEPOSIT, CHARGES,
    PNL, SETTLEMENT_OUTSTANDING_BOOKED, …) for the UI to colour-code
  • `is_settlement` — fast flag so the row stands out in the table
  • `label` — human-readable category (e.g. "Brokerage", "Trade loss",
    "Settlement booked") that the user reads at a glance
  • `particulars` — long-form description with the underlying narration
  • `debit` / `credit` — split signed amount into the two columns
  • `balance` — available_balance after this txn (continuous across rows)

The aggregate fields the dashboard cards lean on:
  • `opening_balance` — first row's balance_before
  • `closing_balance` — last row's balance_after
  • `total_settlement_booked` — sum of magnitudes of every
    SETTLEMENT_OUTSTANDING_BOOKED row in this window, so the user
    sees the SETTLEMENT total prominently even if the ledger spans
    multiple trades.

Sanitisation: admin-internal transactions (REVERSAL of reopens,
SETTLEMENT_OUTSTANDING_RECOVERY from reopen unwinds, manual ADJUSTMENT
corrections) get their narrations stripped of internal terminology
("Reopen", "admin user code", "STOP_OUT", etc.) and shown to the user
as generic "Trade adjustment" rows so the user doesn't see when an
admin has corrected a position. Symbol is preserved so the user can
identify which instrument the row relates to.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query

from app.core.dependencies import CurrentUser
from app.models.transaction import TransactionType, WalletTransaction
from app.schemas.common import APIResponse

router = APIRouter(prefix="/ledger", tags=["user-ledger"])


# Human-readable label for each transaction type. Kept locally so the
# wording can iterate without a frontend deploy.
_LABELS: dict[TransactionType, str] = {
    TransactionType.DEPOSIT: "Deposit",
    TransactionType.WITHDRAWAL: "Withdrawal",
    TransactionType.TRADE: "Trade",
    TransactionType.BROKERAGE: "Brokerage",
    TransactionType.CHARGES: "Brokerage / charges",
    TransactionType.PNL: "Realised P&L",
    TransactionType.ADJUSTMENT: "Adjustment",
    TransactionType.BONUS: "Bonus credit",
    TransactionType.PENALTY: "Penalty debit",
    TransactionType.PROMO: "Promo credit",
    TransactionType.INTER_USER: "Inter-user transfer",
    # REVERSAL gets a neutral label on the user side. Internally a
    # REVERSAL row is written when an admin reopens a closed position,
    # but the user doesn't need to know that — to them it's just a
    # trade-side adjustment that brings the ledger back in line.
    TransactionType.REVERSAL: "Trade adjustment",
    TransactionType.PNL_SHARING_PAYOUT: "P&L sharing payout",
    TransactionType.PNL_SHARING_RECEIPT: "P&L sharing receipt",
    TransactionType.SETTLEMENT_OUTSTANDING_BOOKED: "Settlement booked",
    TransactionType.SETTLEMENT_OUTSTANDING_RECOVERY: "Settlement recovered",
}

# Manual balance changes an ADMIN makes on a user's wallet (the admin
# `wallet-adjust` action defaults to ADJUSTMENT and also offers BONUS / PENALTY
# / PROMO). To the user these are simply money the admin put in or took out, so
# the ledger relabels them by direction — "Deposit by admin" for a credit,
# "Withdrawal by admin" for a debit — instead of the internal type name.
_ADMIN_MANUAL_TYPES: frozenset[TransactionType] = frozenset({
    TransactionType.ADJUSTMENT,
    TransactionType.BONUS,
    TransactionType.PENALTY,
    TransactionType.PROMO,
})


# Words / patterns that leak internal admin actions into the user's
# ledger. When any of these appears in a row's narration on a sensitive
# transaction type (REVERSAL / ADJUSTMENT / SETTLEMENT_OUTSTANDING_*),
# we strip the original narration and emit a generic replacement.
_LEAK_PATTERNS = re.compile(
    r"(reopen|stop[\s_-]?out|reopened by|admin |unwind|over-credit|"
    r"double-count|counter-reversal|by ADM\d+|by SUP\d+|by BRK\d+|"
    r"ADM\d+|SUP\d+|BRK\d+|closed by [A-Z_]+)",
    re.IGNORECASE,
)

# Extracts an instrument symbol from a narration like
# "Reopen DIVISLAB26MAYFUT — reverse cash portion only ..." or
# "Realized loss on NIFTY26MAY23950CE close" — anything in CAPS with
# digits, optionally ending in CE/PE/FUT. Used to preserve the
# user-visible identifier when we strip the rest of the line.
_SYMBOL_RE = re.compile(r"\b([A-Z]+\d[A-Z0-9]*(?:CE|PE|FUT)?)\b")


def _sanitize_narration(t: WalletTransaction) -> str:
    """Return a user-safe narration for the ledger row.

    Most transaction types pass through unchanged — DEPOSIT, CHARGES,
    PNL, SETTLEMENT_OUTSTANDING_BOOKED all carry information the user
    legitimately needs. Only the admin-internal types get rewritten:

      • REVERSAL — used both for SL/TP-bracket close reversals AND for
        admin reopens. We can't distinguish them from the row alone,
        so both get the generic "Trade adjustment" line.
      • ADJUSTMENT — used for manual admin corrections (wallet credits,
        bug fixes). Show as generic "Adjustment" unless it's a
        user-facing deposit narration (which keeps DEPOSIT type).
      • SETTLEMENT_OUTSTANDING_RECOVERY — written when settlement debt
        clears either against a deposit OR via a reopen unwind. The
        deposit-driven recovery is fine to surface; the reopen-driven
        one leaks the admin action and gets scrubbed.
    """
    raw = t.narration or ""
    ttype = t.transaction_type

    if ttype not in (
        TransactionType.REVERSAL,
        TransactionType.ADJUSTMENT,
        TransactionType.SETTLEMENT_OUTSTANDING_RECOVERY,
    ):
        return raw

    if not _LEAK_PATTERNS.search(raw):
        # No admin-action terminology in the narration; safe to keep.
        return raw

    # Pull the symbol so the user can still identify which instrument
    # the adjustment relates to (otherwise the row looks orphan).
    sym_match = _SYMBOL_RE.search(raw)
    sym = sym_match.group(1) if sym_match else None

    if ttype == TransactionType.REVERSAL:
        return f"Trade adjustment — {sym}" if sym else "Trade adjustment"
    if ttype == TransactionType.SETTLEMENT_OUTSTANDING_RECOVERY:
        return "Settlement adjustment"
    # ADJUSTMENT
    return "Adjustment"


@router.get("", response_model=APIResponse[dict])
async def ledger(
    user: CurrentUser,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    limit: int = Query(default=200, le=1000),
    types: str | None = Query(
        default=None,
        description="Comma-separated transaction_type filter (e.g. "
        "DEPOSIT,WITHDRAWAL,SETTLEMENT_OUTSTANDING_BOOKED). Omit for all types.",
    ),
):
    q: dict[str, Any] = {"user_id": user.id}
    if from_date or to_date:
        q["created_at"] = {}
        if from_date:
            q["created_at"]["$gte"] = from_date
        if to_date:
            q["created_at"]["$lte"] = to_date
    # Optional server-side type filter. The user ledger page requests only
    # DEPOSIT / WITHDRAWAL / SETTLEMENT_* so those rows can't be crowded out of
    # the `limit` window by a busy account's TRADE / BROKERAGE / PNL rows.
    # Backward-compatible: no `types` param → every transaction type as before.
    if types:
        wanted = [x.strip() for x in types.split(",") if x.strip()]
        if wanted:
            q["transaction_type"] = {"$in": wanted}
    rows = await WalletTransaction.find(q).sort("+created_at").limit(limit).to_list()

    out = []
    opening = None
    closing = None
    total_settlement_booked = 0.0
    for t in rows:
        d = float(str(t.amount))
        if opening is None:
            opening = float(str(t.balance_before))
        closing = float(str(t.balance_after))

        is_settlement = (
            t.transaction_type == TransactionType.SETTLEMENT_OUTSTANDING_BOOKED
        )
        if is_settlement:
            # Magnitude of the booking — d is always negative on
            # SETTLEMENT_OUTSTANDING_BOOKED rows, so abs() picks up
            # the right number for the summary card.
            total_settlement_booked += abs(d)

        label = _LABELS.get(t.transaction_type, t.transaction_type.value)
        # Admin manual wallet changes → show as a plain admin-initiated
        # deposit / withdrawal, chosen by the sign of the amount.
        if t.transaction_type in _ADMIN_MANUAL_TYPES:
            label = "Deposit by admin" if d > 0 else "Withdrawal by admin"
        particulars = _sanitize_narration(t)

        out.append(
            {
                "id": str(t.id),
                "date": t.created_at,
                "type": t.transaction_type.value,
                "label": label,
                "is_settlement": is_settlement,
                "particulars": particulars,
                "debit": -d if d < 0 else 0.0,
                "credit": d if d > 0 else 0.0,
                "balance": float(str(t.balance_after)),
                "reference_type": t.reference_type,
                "reference_id": t.reference_id,
            }
        )
    return APIResponse(
        data={
            "rows": out,
            "opening_balance": opening or 0.0,
            "closing_balance": closing or 0.0,
            "total_settlement_booked": total_settlement_booked,
            "count": len(out),
        }
    )
