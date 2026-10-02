"""Platform-wide settings — flat key-value store backed by MongoDB.

For settings that change infrequently and need an admin UI. Cached in Redis.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from beanie import Indexed
from pymongo import ASCENDING, IndexModel

from app.models._base import StrEnum, TimestampMixin


class SettingType(StrEnum):
    STRING = "STRING"
    INTEGER = "INTEGER"
    FLOAT = "FLOAT"
    BOOL = "BOOL"
    JSON = "JSON"


class PlatformSetting(TimestampMixin):
    setting_key: Indexed(str, unique=True)  # type: ignore[valid-type]
    setting_value: Any
    setting_type: SettingType = SettingType.STRING
    description: str = ""
    category: str = "general"  # general / trading / risk / notifications / payment / api / security
    is_public: bool = False  # if True, exposed to user app via /platform-settings/public

    class Settings:
        name = "platform_settings"
        indexes = [
            IndexModel([("setting_key", ASCENDING)], unique=True),
            IndexModel([("category", ASCENDING)]),
        ]
