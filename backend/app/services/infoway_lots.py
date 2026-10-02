"""Standard contract / lot sizes for Infoway-mirrored instruments.

Forex, spot metals, energy and international indices have *retail-CFD*
convention contract sizes that don't match the "1 share = 1 lot" pattern
Indian equity uses. For margin and notional math to come out right
(`notional = lots × contract_size × price`), the platform needs a table
of these multipliers.

Conventions (matching what every major retail forex/CFD broker uses —
verify against IC Markets, OANDA, Exness contract specs):

  • Forex majors / minors: 1 standard lot = 100,000 units of the base
    currency. So 1 lot of EURUSD at 1.08 → $108,000 notional. The
    platform also allows fractional 0.01 / 0.1 lots (mini / micro).
  • Spot gold (XAUUSD): 1 lot = 100 troy oz.
  • Spot silver (XAGUSD): 1 lot = 5,000 troy oz.
  • Spot platinum (XPTUSD): 1 lot = 50 troy oz.
  • Spot palladium (XPDUSD): 1 lot = 100 troy oz.
  • WTI / Brent crude (USOIL / UKOIL): 1 lot = 1,000 barrels.
  • Natural gas (NATGAS): 1 lot = 10,000 mmBtu.
  • International indices (SPX500 / NAS100 / US30 / UK100 / DE40 …):
    1 lot = 1 contract × index value (retail-CFD convention; for the
    platform's `qty × price = notional` math, this works out to a
    `contract_size = 1`).
  • International stocks (AAPL / MSFT / …): 1 lot = 1 share.
  • Crypto (ETHUSD / SOLUSD / …): 1 lot = 100 units (per the broker
    spec — match the user's mental model of "1 lot button = 100 coins
    of exposure"). Mini/micro exposure is sized via fractional lots
    (down to 0.001 / 0.0001) gated by the `min_lot` admin setting.
    EXCEPTION: BTCUSD = 1 unit / lot (operator request 2026-06-08).

When updating: keep the keys in canonical Infoway-symbol form (no `T`
suffix on crypto — `BTCUSD` not `BTCUSDT`; that translation lives in
`_translate_user_to_provider`).
"""

from __future__ import annotations

# Specific overrides keyed by symbol. Anything not in this table falls
# back to the category default in `_default_for_segment`.
INFOWAY_LOT_SIZES: dict[str, int] = {
    # ── Spot metals ──────────────────────────────────────────────
    "XAUUSD": 100,     # gold — 100 troy oz / lot
    "XAGUSD": 5000,    # silver — 5,000 troy oz / lot
    "XPTUSD": 50,      # platinum — 50 troy oz / lot
    "XPDUSD": 100,     # palladium — 100 troy oz / lot
    # ── Energy ───────────────────────────────────────────────────
    "USOIL": 1000,     # WTI crude — 1,000 barrels / lot
    "UKOIL": 1000,     # Brent crude — 1,000 barrels / lot
    "NATGAS": 10000,   # natural gas — 10,000 mmBtu / lot
    # ── Forex majors / minors ────────────────────────────────────
    # All standard forex contracts are 100,000 base-currency units.
    "EURUSD": 100000,
    "GBPUSD": 100000,
    "USDJPY": 100000,
    "AUDUSD": 100000,
    "USDCAD": 100000,
    "USDCHF": 100000,
    "NZDUSD": 100000,
    "EURJPY": 100000,
    "GBPJPY": 100000,
    "EURGBP": 100000,
    "AUDJPY": 100000,
    "EURAUD": 100000,
    "EURCHF": 100000,
    "AUDCAD": 100000,
    "AUDNZD": 100000,
    "CADCHF": 100000,
    "CADJPY": 100000,
    "CHFJPY": 100000,
    "EURCAD": 100000,
    "EURNZD": 100000,
    "GBPAUD": 100000,
    "GBPCAD": 100000,
    "GBPCHF": 100000,
    "GBPNZD": 100000,
    "NZDCAD": 100000,
    "NZDCHF": 100000,
    "NZDJPY": 100000,
    # ── Indices (CFD contract size = 1 — qty × index value = USD/EUR
    # notional directly). Listing them explicitly so the resolver
    # doesn't fall through to a different default by accident.
    "SPX500": 1,
    "NAS100": 1,
    "US30": 1,
    "UK100": 1,
    "DE40": 1,
    "GER40": 1,  # alias used by some Infoway feeds
    "JPN225": 1,
    "HK50": 1,
    "FRA40": 1,
    "AUS200": 1,
    "EU50": 1,
    # Crypto: 1 lot = 100 units. Tap "+1 lot" and the order goes out
    # for 100 coins of notional; fractional lots are how mini/micro
    # exposure is sized below 100.
    "BTCUSD": 100,
    "ETHUSD": 100,
    "SOLUSD": 100,
    "XRPUSD": 100,
    "DOGEUSD": 100,
    "BNBUSD": 100,
    "LTCUSD": 100,
    "ADAUSD": 100,
    "DOTUSD": 100,
    "AVAXUSD": 100,
    "MATICUSD": 100,
    "LINKUSD": 100,
}


def _default_for_segment(segment: str | None) -> int:
    """Category default when the exact symbol isn't in the table.

    • FOREX → 100,000 (standard forex lot).
    • COMMODITIES → 100 (sensible for an unlisted spot metal; admin
      can override per-symbol for energy / softs).
    • CRYPTO → 100 (1 lot = 100 coins per the broker spec).
    • INDICES / STOCKS → 1.
    Falls through to 1 for anything else so the math still works
    (multiplying by 1 is a no-op).
    """
    s = (segment or "").upper()
    if s == "FOREX":
        return 100000
    if s == "COMMODITIES":
        return 100
    if "CRYPTO" in s:
        return 100
    return 1


def get_infoway_lot_size(symbol: str | None, segment: str | None = None) -> int:
    """Return the canonical contract size for an Infoway-mirrored
    instrument. Symbol lookup is case-insensitive; falls back to the
    segment default when the exact symbol isn't listed.
    """
    if not symbol:
        return _default_for_segment(segment)
    key = symbol.strip().upper()
    # Strip trailing `T` so BTCUSDT / ETHUSDT resolve like BTCUSD / ETHUSD
    # — Infoway lists most crypto pairs with the USDT suffix but our
    # platform stores them with the trimmed `USD` form.
    if key.endswith("USDT"):
        key = key[:-1]
    if key in INFOWAY_LOT_SIZES:
        return INFOWAY_LOT_SIZES[key]
    return _default_for_segment(segment)
