"""End-to-end netting segment resolver tests.

Verifies the chain: admin row in DB → resolver → user-side legacy-dict
output. Guards against the segment-name regression where stale
instruments stored as ``MCX_OPTION`` / ``NFO_OPTION`` (singular suffix,
no _BUY/_SELL split) silently fell through to permissive defaults
because they weren't in ``_SEGMENT_NAME_MAP``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.netting import NettingFieldsRequired
from app.services.netting_service import _SEGMENT_NAME_MAP, _to_legacy_dict


def _fake_seg(name: str, **overrides):
    """Build a NettingSegment-shaped object without DB init."""
    seg = SimpleNamespace()
    for k, v in NettingFieldsRequired().model_dump().items():
        setattr(seg, k, v)
    seg.name = name
    seg.displayName = name
    for k, v in overrides.items():
        setattr(seg, k, v)
    return seg


# ---------- Segment-name map: every shape the mirror has ever used ----------

@pytest.mark.parametrize(
    "stored,expected_row",
    [
        # Canonical SegmentType enum values
        ("NSE_FUTURE", "NSE_FUT"),
        ("NSE_INDEX_FUTURE", "NSE_FUT"),
        ("NSE_STOCK_OPTION_BUY", "NSE_OPT"),
        ("NSE_INDEX_OPTION_SELL", "NSE_OPT"),
        ("BSE_FUTURE", "BSE_FUT"),
        ("BSE_OPTION_BUY", "BSE_OPT"),
        ("MCX_FUTURE", "MCX_FUT"),
        ("MCX_OPTION_BUY", "MCX_OPT"),
        ("MCX_OPTION_SELL", "MCX_OPT"),
        # Legacy mirror variants — Kite exchange code + suffix
        ("NFO_FUT", "NSE_FUT"),
        ("NFO_OPT", "NSE_OPT"),
        ("BFO_FUT", "BSE_FUT"),
        ("BFO_OPT", "BSE_OPT"),
        # Pre-2025 mirror variants — singular OPTION/FUTURE suffix
        # (these caused the COPPER26MAY*CE Fixed/₹100 regression)
        ("MCX_OPTION", "MCX_OPT"),
        ("MCX_FUTURE", "MCX_FUT"),
        ("NFO_OPTION", "NSE_OPT"),
        ("NFO_FUTURE", "NSE_FUT"),
        ("BFO_OPTION", "BSE_OPT"),
        ("BFO_FUTURE", "BSE_FUT"),
        # Infoway segments — instrument segment IS the admin row name
        ("FOREX", "FOREX"),
        ("STOCKS", "STOCKS"),
        ("INDICES", "INDICES"),
        ("COMMODITIES", "COMMODITIES"),
        # Crypto family folds into one row
        ("CRYPTO_SPOT", "CRYPTO"),
        ("CRYPTO_PERPETUAL", "CRYPTO"),
    ],
)
def test_segment_name_map_covers_every_known_shape(stored, expected_row):
    assert _SEGMENT_NAME_MAP[stored] == expected_row


# ---------- Resolver output: admin's saved values flow through ----------

def test_mcx_opt_times_leverage_inherits_to_option_buy():
    """Reproduces the COPPER26MAY1220CE Fixed/₹100 bug.

    Admin matrix:
      MCX_OPT row: marginCalcMode=times, intradayMargin=300,
                   optionBuyIntraday=0 (inherit signal)

    For an MCX option BUY (CE) MIS order, the resolver must return
    Times mode with leverage=300 (NOT Fixed/₹100 from synthetic seed
    defaults).
    """
    seg = _fake_seg(
        "MCX_OPT",
        marginCalcMode="times",
        intradayMargin=300.0,
        optionBuyIntraday=0,
        optionSellIntraday=0,
    )
    out = _to_legacy_dict(
        seg, None, action="BUY", option_type="CE", product_type="MIS"
    )
    assert out["margin_calc_mode"] == "times"
    assert out["leverage"] == 300.0
    assert out["margin_percentage"] == 100.0
    assert out["fixed_margin_per_lot"] == 0.0


def test_nse_opt_fixed_per_lot_inherits_segment_value():
    """NSE_OPT row: Fixed mode + intradayMargin=300, option columns at
    0 (inherit). NIFTY PE BUY MIS should resolve to Fixed · ₹300/lot.
    """
    seg = _fake_seg(
        "NSE_OPT",
        marginCalcMode="fixed",
        intradayMargin=300.0,
        optionBuyIntraday=0,
        optionSellIntraday=0,
    )
    out = _to_legacy_dict(
        seg, None, action="BUY", option_type="PE", product_type="MIS"
    )
    assert out["margin_calc_mode"] == "fixed"
    assert out["fixed_margin_per_lot"] == 300.0


def test_nse_fut_fixed_per_lot_no_option_path():
    """Non-option future row uses the segment-wide path directly."""
    seg = _fake_seg(
        "NSE_FUT",
        marginCalcMode="fixed",
        intradayMargin=200.0,
    )
    out = _to_legacy_dict(seg, None, action="BUY", product_type="MIS")
    assert out["margin_calc_mode"] == "fixed"
    assert out["fixed_margin_per_lot"] == 200.0


def test_option_buy_explicit_override_wins_over_segment():
    """When admin EXPLICITLY sets optionBuyIntraday to a non-zero
    value, it overrides the segment-wide intradayMargin."""
    seg = _fake_seg(
        "MCX_OPT",
        marginCalcMode="times",
        intradayMargin=300.0,
        optionBuyIntraday=50.0,  # explicit override — 50× for BUYs
    )
    out = _to_legacy_dict(
        seg, None, action="BUY", option_type="CE", product_type="MIS"
    )
    assert out["leverage"] == 50.0


def test_null_margin_mode_with_high_intraday_infers_times():
    """Defensive inference: row with marginCalcMode=None and
    intradayMargin > 100 is treated as Times-mode leverage."""
    seg = _fake_seg(
        "NSE_FUT",
        marginCalcMode=None,
        intradayMargin=700.0,
    )
    out = _to_legacy_dict(seg, None, action="BUY", product_type="MIS")
    assert out["margin_calc_mode"] == "times"
    assert out["leverage"] == 700.0


def test_null_margin_mode_with_low_intraday_infers_fixed():
    """Defensive inference: row with marginCalcMode=None and
    intradayMargin <= 100 is treated as Fixed ₹/lot."""
    seg = _fake_seg(
        "NSE_FUT",
        marginCalcMode=None,
        intradayMargin=50.0,
    )
    out = _to_legacy_dict(seg, None, action="BUY", product_type="MIS")
    assert out["margin_calc_mode"] == "fixed"
    assert out["fixed_margin_per_lot"] == 50.0


def test_times_mode_overrides_overnight_split():
    """Times mode always reads intraday (no overnight differentiation)
    even for NRML/CNC product types."""
    seg = _fake_seg(
        "NSE_FUT",
        marginCalcMode="times",
        intradayMargin=400.0,
        overnightMargin=100.0,
    )
    intraday_out = _to_legacy_dict(seg, None, action="BUY", product_type="MIS")
    overnight_out = _to_legacy_dict(seg, None, action="BUY", product_type="NRML")
    assert intraday_out["leverage"] == 400.0
    assert overnight_out["leverage"] == 400.0
