"""Zerodha dual-account feed routing + HA failover config — singleton doc.

Separate from ``ZerodhaSettings`` (which is per-account credentials/subscriptions)
because routing is a GLOBAL policy across both accounts, not a per-account field.

Normal routing (operator SANWARIYASETH):
  • Account A (account_index 0) = NSE + BSE (+ their F&O: NFO / BFO)
  • Account B (account_index 1) = MCX (+ MCX F&O)

Failover (data must NEVER stop, bidirectional):
  • If the desired account for an exchange is UNHEALTHY, that exchange's tokens
    stream from the OTHER (surviving) account until the desired one recovers.

Health is tick-based, not just the WS ``connected`` flag: a socket that is
connected-but-silent (Kite half-open / token throttle) counts as UNHEALTHY so
failover still fires.
"""

from __future__ import annotations

from pydantic import Field

from app.models._base import TimestampMixin

# Kite exchange codes → default account_index. Map by KITE EXCHANGE CODE
# (not our SegmentType): NIFTY options are NFO, SENSEX options are BFO, all
# MCX futures/options are MCX.
DEFAULT_EXCHANGE_ACCOUNT_MAP: dict[str, int] = {
    "NSE": 0,   # NSE cash            → A
    "BSE": 0,   # BSE cash            → A
    "NFO": 0,   # NSE F&O (NIFTY etc) → A
    "BFO": 0,   # BSE F&O (SENSEX)    → A
    "CDS": 0,   # currency derivs     → A
    "MCX": 1,   # commodities         → B
}


class ZerodhaFeedRouting(TimestampMixin):
    """Singleton config document (only one row is ever used)."""

    # Exchange (Kite code) → desired account_index (0 = A, 1 = B).
    exchange_account_map: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_EXCHANGE_ACCOUNT_MAP)
    )

    # Master kill-switch. When False, routing collapses to "single account (A)
    # carries everything" — instantly reverts to the pre-failover behaviour.
    failover_enabled: bool = True

    # Anti-flap debounce. An account must stay DOWN for this long before we
    # fail its exchanges over, and stay UP for this long before we fail them
    # back — so a brief blip doesn't thrash re-subscriptions.
    failover_confirm_down_sec: int = 5
    failback_confirm_up_sec: int = 25

    # Tick-based health: a connected socket that has produced NO tick within
    # this window (during market hours) is treated as UNHEALTHY.
    health_stale_sec: int = 15

    class Settings:
        name = "zerodha_feed_routing"
