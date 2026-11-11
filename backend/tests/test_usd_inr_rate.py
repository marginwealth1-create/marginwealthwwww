"""Operator-set USD→INR rate: the classifier and the cached read.

    pytest -q backend/tests/test_usd_inr_rate.py

A wrong rate or a mis-scoped segment produces a plausible number, never an
error — so these pin the rules instead of trusting them.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import market_data_service as mds


@pytest.mark.parametrize(
    "seg",
    [
        "NSE_EQUITY", "NSE_INDEX_OPTION_BUY", "BSE_EQUITY", "MCX_FUTURE", "MCX_OPTION",
        "NFO_OPTION", "BFO_OPTION", "NCDEX_FUTURE",
        # INR prefixes win even when the rest would match the USD whitelist
        "NSE_INDICES", "NSE_STOCKS", "MCX_COMMODITIES", "NSE_CRYPTO_ETF", "BSE_FOREX_X",
    ],
)
def test_every_indian_segment_stays_inr(seg):
    assert mds.is_usd_quoted_segment(seg) is False


@pytest.mark.parametrize(
    "seg", ["FOREX", "COMMODITIES", "STOCKS", "INDICES", "CRYPTO_SPOT", "CRYPTO_PERPETUAL", "FOREX_MAJOR", "CDS_FUTURE", "commodities"]
)
def test_usd_segments_convert(seg):
    assert mds.is_usd_quoted_segment(seg) is True


@pytest.mark.parametrize("seg", [None, "", "  ", "CRYPTO", "UNKNOWN"])
def test_unknown_or_empty_segments_do_not_convert(seg):
    # "CRYPTO" alone is not a prefix match ("CRYPTO_") — exact/prefix rules only.
    assert mds.is_usd_quoted_segment(seg) is False


def test_rate_falls_back_to_1_only_before_the_first_read(monkeypatch):
    monkeypatch.setitem(mds._usd_inr_cache, "rate", 0.0)
    assert mds.get_usd_inr_rate() == 1.0
    monkeypatch.setitem(mds._usd_inr_cache, "rate", 88.5)
    assert mds.get_usd_inr_rate() == 88.5


def test_a_failed_read_keeps_the_last_good_rate(monkeypatch):
    monkeypatch.setitem(mds._usd_inr_cache, "rate", 92.0)

    class _Boom:
        setting_key = None

        @staticmethod
        async def find_one(*_a, **_k):
            raise RuntimeError("db down")

    import app.models.platform_setting as ps

    monkeypatch.setattr(ps, "PlatformSetting", _Boom)
    assert asyncio.run(mds.refresh_usd_inr_rate()) == 92.0
    assert mds.get_usd_inr_rate() == 92.0  # not snapped onto the fallback
