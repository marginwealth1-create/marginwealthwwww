"""Zerodha Kite Connect integration settings — single-row collection.

Stores admin-supplied API credentials, the day's access token (Kite flushes
it ~06:00 IST; the app treats it as expired at 07:00 IST), enabled segments, and the list of subscribed
instruments that the WebSocket ticker is following.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from beanie import Indexed, PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import IndexModel

from app.models._base import StrEnum, TimestampMixin


class WsStatus(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class EnabledSegments(BaseModel):
    nseEq: bool = True
    bseEq: bool = True
    nseFut: bool = True
    nseOpt: bool = True
    mcxFut: bool = True
    mcxOpt: bool = True
    bseFut: bool = False
    bseOpt: bool = False


class SubscribedInstrument(BaseModel):
    """Mirrors the bharat schema 1:1 so existing UIs map cleanly."""

    token: int
    symbol: str
    exchange: str
    segment: str | None = None
    name: str | None = None
    lotSize: int = 1
    tickSize: float = 0.05
    expiry: str | None = None  # ISO date or null
    strike: float | None = None
    instrumentType: str | None = None  # EQ / FUT / CE / PE


class ZerodhaSettings(TimestampMixin):
    """One document per Zerodha API account (account_index 0 = primary, 1 = secondary)."""

    account_index: int = 0

    apiKey: str = ""
    apiSecret: str = ""
    accessToken: str | None = None
    refreshToken: str | None = None
    tokenExpiry: datetime | None = None
    isConnected: bool = False
    lastConnected: datetime | None = None

    enabledSegments: EnabledSegments = Field(default_factory=EnabledSegments)
    subscribedInstruments: list[SubscribedInstrument] = Field(default_factory=list)

    instrumentsLastFetched: datetime | None = None

    autoSyncEnabled: bool = True
    autoRemoveExpired: bool = True

    wsStatus: WsStatus = WsStatus.DISCONNECTED
    wsLastError: str | None = None

    # Default points at the local backend; admin UI lets super-admin override.
    # Keep this in sync with `Settings.zerodha_redirect_url` in core.config.
    redirectUrl: str = "http://localhost:8000/api/v1/admin/zerodha/callback"

    class Settings:
        name = "zerodha_settings"
