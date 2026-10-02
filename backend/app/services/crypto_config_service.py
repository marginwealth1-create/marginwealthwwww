"""Per-admin crypto deposit config — resolution + mutations.

Ownership + cascade mirror company banks exactly (see user/wallet.py): a user
resolves through broker → parent-broker → admin → platform-default, and a user
under a sub-admin NEVER falls through to the super-admin. So an admin who hasn't
enabled crypto simply leaves their users on the plain INR flow.
"""

from __future__ import annotations

from typing import Any

from beanie import PydanticObjectId

from app.models.crypto_config import AdminCryptoConfig
from app.models.user import User, UserRole
from app.utils.crypto_secrets import decrypt_secret, encrypt_secret


def owner_filter_for_admin(admin: User) -> dict[str, PydanticObjectId | None]:
    """The (owner_admin_id, owner_broker_id) pair identifying THIS actor's
    config row. Super-admin → both null (platform default)."""
    if admin.role == UserRole.SUPER_ADMIN:
        return {"owner_admin_id": None, "owner_broker_id": None}
    if admin.role == UserRole.BROKER:
        return {"owner_admin_id": None, "owner_broker_id": admin.id}
    # ADMIN (default)
    return {"owner_admin_id": admin.id, "owner_broker_id": None}


async def get_config(owner_admin_id: PydanticObjectId | None,
                     owner_broker_id: PydanticObjectId | None) -> AdminCryptoConfig | None:
    return await AdminCryptoConfig.find_one(
        AdminCryptoConfig.owner_admin_id == owner_admin_id,
        AdminCryptoConfig.owner_broker_id == owner_broker_id,
    )


async def get_for_admin(admin: User) -> AdminCryptoConfig | None:
    f = owner_filter_for_admin(admin)
    return await get_config(f["owner_admin_id"], f["owner_broker_id"])


async def upsert_for_admin(admin: User, patch: dict[str, Any]) -> AdminCryptoConfig:
    """Create or update THIS actor's crypto config. `patch` may contain:
    enabled, mode, wallet_address, network, asset, gateway, and a RAW
    `oxapay_api_key` (encrypted here; empty string clears it)."""
    cfg = await get_for_admin(admin)
    if cfg is None:
        f = owner_filter_for_admin(admin)
        cfg = AdminCryptoConfig(
            owner_admin_id=f["owner_admin_id"], owner_broker_id=f["owner_broker_id"]
        )

    if "enabled" in patch:
        cfg.enabled = bool(patch["enabled"])
    if patch.get("mode") in ("manual", "gateway", "both"):
        cfg.mode = patch["mode"]
    for k in ("wallet_address", "network", "asset"):
        if k in patch:
            v = (patch[k] or "").strip() if isinstance(patch[k], str) else patch[k]
            setattr(cfg, k, v or None)
    if "gateway" in patch and patch["gateway"] in ("oxapay", "none"):
        cfg.gateway = patch["gateway"]
    if "oxapay_api_key" in patch:
        raw = (patch["oxapay_api_key"] or "").strip()
        cfg.oxapay_api_key_enc = encrypt_secret(raw) if raw else None

    await cfg.save()
    return cfg


async def resolve_for_user(user: User) -> AdminCryptoConfig | None:
    """First ENABLED config in the user's ownership cascade, else None.

    Cascade (closest wins): immediate broker → parent brokers (tip→root) →
    assigned admin → platform-default (ONLY when the user has no admin)."""
    tried: list[dict[str, PydanticObjectId | None]] = []
    if user.assigned_broker_id is not None:
        tried.append({"owner_admin_id": None, "owner_broker_id": user.assigned_broker_id})
    for parent in reversed(list(user.broker_ancestry or [])):
        if parent == user.assigned_broker_id:
            continue
        tried.append({"owner_admin_id": None, "owner_broker_id": parent})
    if user.assigned_admin_id is not None:
        tried.append({"owner_admin_id": user.assigned_admin_id, "owner_broker_id": None})
    else:
        # Direct super-admin user only — never leak the platform default to a
        # sub-admin's users (same rule as company banks).
        tried.append({"owner_admin_id": None, "owner_broker_id": None})

    for f in tried:
        cfg = await get_config(f["owner_admin_id"], f["owner_broker_id"])
        if cfg is not None and cfg.enabled:
            return cfg
    return None


def to_admin_dict(cfg: AdminCryptoConfig | None) -> dict[str, Any]:
    """For the ADMIN settings screen. NEVER returns the raw key — only whether
    one is stored, so the UI can show 'key set' without exposing it."""
    if cfg is None:
        return {
            "enabled": False, "mode": "manual",
            "wallet_address": None, "network": None, "asset": None,
            "gateway": "none", "has_gateway_key": False,
        }
    return {
        "enabled": cfg.enabled,
        "mode": cfg.mode,
        "wallet_address": cfg.wallet_address,
        "network": cfg.network,
        "asset": cfg.asset,
        "gateway": cfg.gateway,
        "has_gateway_key": bool(cfg.oxapay_api_key_enc),
    }


def to_user_dict(cfg: AdminCryptoConfig | None) -> dict[str, Any] | None:
    """For the USER deposit screen. Returns None when crypto isn't available,
    so the frontend hides the Crypto option entirely. Never leaks any key."""
    if cfg is None or not cfg.enabled:
        return None
    manual_ready = bool(cfg.wallet_address) and cfg.mode in ("manual", "both")
    gateway_ready = bool(cfg.oxapay_api_key_enc) and cfg.mode in ("gateway", "both")
    if not manual_ready and not gateway_ready:
        return None
    return {
        "manual": (
            {"wallet_address": cfg.wallet_address, "network": cfg.network, "asset": cfg.asset}
            if manual_ready else None
        ),
        "gateway": ({"provider": "oxapay"} if gateway_ready else None),
    }


def decrypted_oxapay_key(cfg: AdminCryptoConfig | None) -> str | None:
    """Backend-only accessor for the gateway call. Never expose the result."""
    return decrypt_secret(cfg.oxapay_api_key_enc) if cfg else None


async def owner_user_for_config(cfg: AdminCryptoConfig) -> User | None:
    """The admin/broker/super-admin who owns a config — used to build the
    per-owner webhook path (their user_code) and the per-admin return domain."""
    if cfg.owner_admin_id:
        return await User.get(cfg.owner_admin_id)
    if cfg.owner_broker_id:
        return await User.get(cfg.owner_broker_id)
    return await User.find_one(User.role == UserRole.SUPER_ADMIN)


async def get_by_owner_code(user_code: str) -> AdminCryptoConfig | None:
    """Resolve a config from its owner's user_code — the reverse of the
    webhook path. Returns None for unknown codes / non-owner users."""
    owner = await User.find_one(User.user_code == user_code)
    if owner is None or owner.role not in (UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.BROKER):
        return None
    return await get_for_admin(owner)
