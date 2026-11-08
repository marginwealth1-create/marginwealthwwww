"""Configurable market trading hours service (IST).

Backed by `PlatformSetting` (`setting_key = "trading.market_hours"`) with a
short in-memory TTL cache per worker so order validation reads it in O(1)
without hitting MongoDB on every order.
"""

from __future__ import annotations

import copy
import time as _time
from datetime import datetime
from typing import Any

from app.models.platform_setting import PlatformSetting, SettingType
from app.utils.time_utils import now_ist, to_ist

MARKET_HOURS_SETTING_KEY = "trading.market_hours"
_CACHE_TTL_SEC = 10.0

MARKET_KEYS = ("NSE", "BSE", "MCX", "FOREX", "CRYPTO")

DEFAULT_MARKET_HOURS: dict[str, dict[str, Any]] = {
    "NSE": {
        "enabled": True,
        "start": "09:15",
        "end": "15:30",
        "days": "Mon - Fri",
    },
    "BSE": {
        "enabled": True,
        "start": "09:15",
        "end": "15:30",
        "days": "Mon - Fri",
    },
    "MCX": {
        "enabled": True,
        "start": "09:00",
        "end": "23:55",
        "days": "Mon - Fri",
    },
    "FOREX": {
        "enabled": True,
        "start": "00:00",
        "end": "23:59",
        "days": "Mon - Fri",
    },
    "CRYPTO": {
        "enabled": True,
        "start": "00:00",
        "end": "23:59",
        "days": "24/7 (Mon - Sun)",
    },
}

_cached_hours: dict[str, dict[str, Any]] | None = None
_cached_at: float = 0.0


def _valid_hhmm(val: Any, fallback: str) -> str:
    if not isinstance(val, str):
        return fallback
    s = val.strip()
    parts = s.split(":")
    if len(parts) < 2:
        return fallback
    try:
        h = int(parts[0])
        m = int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    except ValueError:
        pass
    return fallback


def normalize_market_hours(raw: Any) -> dict[str, dict[str, Any]]:
    """Merge arbitrary stored/input dict onto `DEFAULT_MARKET_HOURS`."""
    out = copy.deepcopy(DEFAULT_MARKET_HOURS)
    if not isinstance(raw, dict):
        return out
    for key in MARKET_KEYS:
        item = raw.get(key)
        if not isinstance(item, dict):
            continue
        default_item = DEFAULT_MARKET_HOURS[key]
        out[key] = {
            "enabled": bool(item.get("enabled", default_item["enabled"])),
            "start": _valid_hhmm(item.get("start"), default_item["start"]),
            "end": _valid_hhmm(item.get("end"), default_item["end"]),
            "days": str(item.get("days") or default_item["days"]),
        }
    return out


async def get_market_hours(*, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    """Return the active market hours dict (cached for `_CACHE_TTL_SEC`)."""
    global _cached_hours, _cached_at
    now_mono = _time.monotonic()
    if (
        not force_refresh
        and _cached_hours is not None
        and (now_mono - _cached_at) < _CACHE_TTL_SEC
    ):
        return _cached_hours

    try:
        row = await PlatformSetting.find_one(
            PlatformSetting.setting_key == MARKET_HOURS_SETTING_KEY
        )
        raw = row.setting_value if row is not None else None
        _cached_hours = normalize_market_hours(raw)
        _cached_at = now_mono
        return _cached_hours
    except Exception:
        if _cached_hours is not None:
            return _cached_hours
        return copy.deepcopy(DEFAULT_MARKET_HOURS)


async def save_market_hours(payload: Any) -> dict[str, dict[str, Any]]:
    """Validate and persist market hours into `PlatformSetting`."""
    global _cached_hours, _cached_at
    normalized = normalize_market_hours(payload)
    row = await PlatformSetting.find_one(
        PlatformSetting.setting_key == MARKET_HOURS_SETTING_KEY
    )
    if row is None:
        row = PlatformSetting(
            setting_key=MARKET_HOURS_SETTING_KEY,
            setting_value=normalized,
            setting_type=SettingType.JSON,
            category="trading",
            is_public=True,
            description="Configurable market-wise trading hours (IST).",
        )
        await row.insert()
    else:
        row.setting_value = normalized
        row.setting_type = SettingType.JSON
        row.is_public = True
        await row.save()

    _cached_hours = normalized
    _cached_at = _time.monotonic()
    return normalized


def resolve_market_key(
    segment_type: str | None = None,
    exchange: str | None = None,
) -> str:
    """Map an instrument's `(segment_type, exchange)` to one of the 5 market keys:
    `CRYPTO`, `FOREX`, `MCX`, `BSE`, or `NSE`.
    """
    seg = (segment_type or "").upper()
    exch = (exchange or "").upper()
    if "CRYPTO" in seg or exch in ("CRYPTO", "BINANCE"):
        return "CRYPTO"
    if (
        seg in ("FOREX", "STOCKS", "INDICES", "COMMODITIES")
        or "FOREX" in seg
        or "FX" in seg
        or seg.startswith("CDS")
        or exch == "CDS"
    ):
        return "FOREX"
    if seg.startswith("MCX") or exch == "MCX":
        return "MCX"
    if seg.startswith(("BSE", "BFO")) or exch in ("BSE", "BFO"):
        return "BSE"
    return "NSE"


def _hhmm_to_minutes(hhmm: str) -> int:
    parts = hhmm.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def is_market_open_by_config(
    hours_map: dict[str, dict[str, Any]],
    segment_type: str | None = None,
    exchange: str | None = None,
    at: datetime | None = None,
) -> tuple[bool, str]:
    """Evaluate whether the resolved market is currently open in IST.

    Returns `(is_open, rejection_message)`.
    """
    key = resolve_market_key(segment_type, exchange)
    cfg = hours_map.get(key) or DEFAULT_MARKET_HOURS[key]
    enabled = bool(cfg.get("enabled", True))
    start_str = str(cfg.get("start") or DEFAULT_MARKET_HOURS[key]["start"])
    end_str = str(cfg.get("end") or DEFAULT_MARKET_HOURS[key]["end"])
    days_str = str(cfg.get("days") or DEFAULT_MARKET_HOURS[key]["days"])

    if not enabled:
        return (
            False,
            f"{key} market is currently closed by admin. Orders are not accepted right now.",
        )

    ist = to_ist(at) if at is not None else now_ist()
    from app.utils.time_utils import muhurat_open, muhurat_window

    mu = muhurat_open(key, ist)
    if mu is not None:
        if mu:
            return True, ""
        o, c = muhurat_window(ist.date()) or (ist.time(), ist.time())
        return False, f"{key}: today only the Muhurat session trades, {o:%H:%M} – {c:%H:%M} IST."
    wd = ist.weekday()  # Mon=0 .. Fri=4, Sat=5, Sun=6
    cur_min = ist.hour * 60 + ist.minute
    start_min = _hhmm_to_minutes(start_str)
    end_min = _hhmm_to_minutes(end_str)

    is_24x7_days = key == "CRYPTO" or "24/7" in days_str or "Sun" in days_str

    if start_min <= end_min:
        # Normal same-day session (e.g. 09:15 -> 15:30 or 00:00 -> 23:59)
        if not is_24x7_days and wd >= 5:
            return (
                False,
                f"{key} market is closed for the weekend ({days_str}, {start_str} – {end_str} IST).",
            )
        if start_min <= cur_min <= end_min:
            return True, ""
        return (
            False,
            f"{key} market is closed. Trading hours: {start_str} – {end_str} IST ({days_str}).",
        )
    else:
        # Overnight session crossing midnight (e.g. 03:30 -> 02:30 IST)
        if is_24x7_days:
            if cur_min >= start_min or cur_min <= end_min:
                return True, ""
        else:
            # Mon-Fri overnight:
            # - Mon..Fri (wd 0..4): open from start_min..23:59
            # - Tue..Sat (wd 1..5): open from 00:00..end_min (tail of previous weekday)
            if (0 <= wd <= 4 and cur_min >= start_min) or (1 <= wd <= 5 and cur_min <= end_min):
                return True, ""
        return (
            False,
            f"{key} market is closed. Trading hours: {start_str} – {end_str} IST ({days_str}).",
        )
