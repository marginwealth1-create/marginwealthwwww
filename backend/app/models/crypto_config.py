"""Per-admin crypto deposit config (opt-in).

Mirrors CompanyBankAccount's ownership so it resolves through the SAME cascade
(broker → parent-broker → admin → platform-default). A user only ever sees a
Crypto deposit option when their owning tier has an ENABLED config here — an
admin who hasn't set crypto up leaves their users on the plain INR flow.

Two independent channels, either/both/neither:
  • manual  → a wallet address the user pays into; QR is rendered client-side.
  • gateway → an oxapay.com merchant; the API key is stored ENCRYPTED at rest
              (never returned to any client) and the webhook auto-credits.
"""

from typing import Literal

from beanie import PydanticObjectId
from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.models._base import TimestampMixin


class AdminCryptoConfig(TimestampMixin):
    # Ownership — same shape as CompanyBankAccount so the resolver cascade is
    # identical. Both null → platform default (super-admin pool).
    owner_admin_id: PydanticObjectId | None = None
    owner_broker_id: PydanticObjectId | None = None

    enabled: bool = False
    # Which channel(s) are live. "manual" = address+QR, "gateway" = oxapay,
    # "both" = show both to the user.
    mode: Literal["manual", "gateway", "both"] = "manual"

    # ── Manual channel ────────────────────────────────────────────────
    wallet_address: str | None = None
    network: str | None = None  # TRC20 / ERC20 / BEP20 / …
    asset: str | None = None    # USDT / BTC / …

    # ── Gateway channel (oxapay) ──────────────────────────────────────
    gateway: Literal["oxapay", "none"] = "none"
    # Fernet-encrypted oxapay merchant API key. NEVER serialized to a client.
    oxapay_api_key_enc: str | None = None

    class Settings:
        name = "admin_crypto_configs"
        indexes = [
            # One config per owner tier. Sparse-safe: multiple rows with both
            # owner ids null (the single platform default) must not collide —
            # but there is only ever one such row, enforced in the service.
            IndexModel(
                [("owner_admin_id", ASCENDING), ("owner_broker_id", ASCENDING)],
                unique=True,
            ),
        ]
