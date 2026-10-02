"""Canonical lot sizes for Indian index F&O contracts.

**Platform-owned, not exchange-tracking.** We're a B-book broker (see
CLAUDE.md → "B-Book broker model") so the contract size we charge users
is a business decision, not whatever Zerodha's instruments CSV reports
for the current exchange revision. This table is the authoritative
source for NSE/BSE F&O lot sizes and OVERRIDES the live CSV.

Used in:
  • Auto-create of an Instrument from the Zerodha cache.
  • Startup backfill (`backfill_index_lot_sizes`) that rewrites existing
    rows to match this table.
  • Admin repair endpoint `/admin/instruments/repair-index-lots`.

Order matters: longer prefixes first so "MIDCPNIFTY…" doesn't match as
"NIFTY", "BANKNIFTY…" doesn't match as "NIFTY", etc.
"""

from __future__ import annotations

INDEX_LOT_SIZES: list[tuple[str, int]] = [
    ("MIDCPNIFTY", 120),
    ("FINNIFTY", 65),
    ("NIFTYNXT50", 25),
    # BANKNIFTY: platform-set to 30 (Zerodha CSV reports 35 after the
    # Oct-2024 SEBI revision — we keep the older quantum for user-facing
    # quoting).
    ("BANKNIFTY", 30),
    ("BANKEX", 30),
    ("SENSEX50", 25),
    ("SENSEX", 20),
    # NIFTY: platform-set to 65 (Zerodha CSV reports 75 after the
    # Nov-2024 SEBI revision — we keep a custom contract size).
    ("NIFTY", 65),
]


# ── MCX commodity lot sizes ──────────────────────────────────────────
# Zerodha's instruments CSV reports MCX lot_size in *raw units* (kg, g, mmBtu,
# barrels) which does not match how the rest of the platform multiplies into
# notional (`quantity = lots × lot_size`, where lot_size is the price-quote
# multiplier). This table is the source of truth and overrides the CSV for
# every MCX FUT / CE / PE. Same table is used for options because MCX option
# contract sizes mirror the underlying future.
#
# Order matters — longer prefixes first so "GOLDPETAL" doesn't match "GOLD",
# "SILVERMIC" doesn't match "SILVER", "CRUDEOILM" doesn't match "CRUDEOIL".
# Values reviewed against MCX contract specs (current revision). When the
# exchange revises a contract size, update this table — the running
# /admin/instruments/repair-index-lots endpoint will rewrite stale rows.
MCX_LOT_SIZES: list[tuple[str, int]] = [
    # Gold family
    ("GOLDPETAL", 1),
    ("GOLDGUINEA", 1),
    ("GOLDM", 10),
    ("GOLD", 100),
    # Silver family
    ("SILVERMIC", 1),
    ("SILVERM", 5),
    ("SILVER", 30),
    # Crude oil
    ("CRUDEOILM", 10),
    ("CRUDEOIL", 100),
    # Natural gas
    ("NATURALGASMINI", 250),
    ("NATGASMINI", 250),
    ("NATURALGAS", 1250),
    ("NATGAS", 1250),
    # Base metals
    ("ZINCMINI", 1000),
    ("ZINC", 5000),
    ("LEADMINI", 1000),
    ("LEAD", 5000),
    ("ALUMINI", 1000),
    ("ALUMINIUM", 5000),
    ("NICKELM", 250),
    ("NICKEL", 1500),
    ("COPPER", 2500),
    # Soft commodities
    ("MENTHAOIL", 360),
    ("COTTON", 25),
    ("CARDAMOM", 100),
    ("KAPAS", 200),
]


def _match_prefix(table: list[tuple[str, int]], *candidates: str | None) -> int | None:
    for raw in candidates:
        if not raw:
            continue
        s = raw.upper().replace(" ", "")
        for prefix, lot in table:
            if s.startswith(prefix):
                return lot
    return None


def get_index_lot_size(*candidates: str | None) -> int | None:
    """Return the canonical lot size for the first candidate whose
    normalised form starts with a known index prefix. Returns None when
    nothing matches — caller should keep whatever lot size it already has.
    """
    return _match_prefix(INDEX_LOT_SIZES, *candidates)


def get_mcx_lot_size(*candidates: str | None) -> int | None:
    """Return the canonical MCX commodity lot size, or None on no match."""
    return _match_prefix(MCX_LOT_SIZES, *candidates)


def get_canonical_lot_size(
    *candidates: str | None,
    exchange: str | None = None,
    instrument_type: str | None = None,
) -> int | None:
    """Unified canonical-lot lookup. **Platform-owned tables are the
    source of truth.** We deliberately OVERRIDE Zerodha's CSV here
    because we're a B-book broker and the contract size we charge users
    is a product decision, not an exchange constant.

    • **MCX** → in-process `MCX_LOT_SIZES` table.
    • **NSE / BSE F&O (NFO / BFO)** → `INDEX_LOT_SIZES` table when the
      symbol matches a known index prefix (NIFTY / BANKNIFTY / SENSEX /
      FINNIFTY / MIDCPNIFTY / NIFTYNXT50 / BANKEX / SENSEX50). When no
      prefix matches (e.g. a stock option like RELIANCE25NOVCE),
      returns ``None`` so the caller falls back to the live CSV lot.
    • **EQ / INDEX spot** → returns ``None``; equity trades 1 share = 1
      lot regardless of any index-prefix coincidence (NIFTYBEES etc.).
    """
    it = (instrument_type or "").upper()
    if it and it not in ("FUT", "CE", "PE"):
        return None
    ex = (exchange or "").upper()
    if ex == "MCX":
        return get_mcx_lot_size(*candidates)
    if ex in ("NFO", "BFO", "NSE", "BSE"):
        # Returns None when no index prefix matches → caller's CSV
        # fallback kicks in for stock options.
        return get_index_lot_size(*candidates)
    return None
