"""Shared helper to enrich list rows with owner info (user name + which
admin/broker pool owns them).

Used by the admin Users / Deposits / Withdrawals / Positions endpoints so
the table can render an "Owner" column showing Self vs. Broker: <name>
without each row needing a separate User lookup.
"""

from __future__ import annotations

from beanie import PydanticObjectId

from app.models.user import User


async def build_owner_map(user_ids: list) -> dict[str, dict]:
    """Given a list of user ids (str or ObjectId), fetch each user plus
    their assigned admin / broker in two extra batched queries, and
    return a map keyed by user_id (str) of:
        {
          "user_name": str | None,
          "user_code": str | None,
          "assigned_admin_id": str | None,
          "assigned_admin_name": str | None,
          "assigned_broker_id": str | None,
          "assigned_broker_name": str | None,
        }
    """
    if not user_ids:
        return {}
    oids = [PydanticObjectId(str(uid)) for uid in user_ids]
    users = await User.find({"_id": {"$in": oids}}).to_list()
    if not users:
        return {}

    admin_oids = list({u.assigned_admin_id for u in users if u.assigned_admin_id})
    broker_oids = list({u.assigned_broker_id for u in users if u.assigned_broker_id})

    admins = (
        await User.find({"_id": {"$in": admin_oids}}).to_list() if admin_oids else []
    )
    brokers = (
        await User.find({"_id": {"$in": broker_oids}}).to_list() if broker_oids else []
    )
    admin_name = {str(a.id): a.full_name for a in admins}
    broker_name = {str(b.id): b.full_name for b in brokers}
    # A broker is a "sub-broker" iff it was itself assigned under another
    # broker. Surface this flag so the Owner badge can render
    # "Sub-broker: vinod" instead of just "Broker: vinod".
    broker_is_sub = {
        str(b.id): bool(b.assigned_broker_id) for b in brokers
    }
    # Map sub-broker → parent broker so the UI can show the full chain
    # "Sub-broker: <sub> → Broker: <parent>" for nested cases.
    sub_to_parent_id = {
        str(b.id): str(b.assigned_broker_id)
        for b in brokers
        if b.assigned_broker_id
    }
    parent_broker_oids = list({PydanticObjectId(pid) for pid in sub_to_parent_id.values()})
    parent_brokers = (
        await User.find({"_id": {"$in": parent_broker_oids}}).to_list()
        if parent_broker_oids
        else []
    )
    parent_broker_name = {str(b.id): b.full_name for b in parent_brokers}

    return {
        str(u.id): {
            "user_name": u.full_name,
            "user_code": u.user_code,
            "assigned_admin_id": str(u.assigned_admin_id) if u.assigned_admin_id else None,
            "assigned_admin_name": admin_name.get(str(u.assigned_admin_id))
            if u.assigned_admin_id
            else None,
            "assigned_broker_id": str(u.assigned_broker_id) if u.assigned_broker_id else None,
            "assigned_broker_name": broker_name.get(str(u.assigned_broker_id))
            if u.assigned_broker_id
            else None,
            "assigned_broker_is_sub": broker_is_sub.get(str(u.assigned_broker_id), False)
            if u.assigned_broker_id
            else False,
            "parent_broker_id": (
                sub_to_parent_id.get(str(u.assigned_broker_id))
                if u.assigned_broker_id
                else None
            ),
            "parent_broker_name": (
                parent_broker_name.get(sub_to_parent_id.get(str(u.assigned_broker_id), ""))
                if u.assigned_broker_id
                else None
            ),
        }
        for u in users
    }


def owner_fields(info: dict | None) -> dict:
    """Project the owner fields onto a row dict, defaulting safely if
    the user lookup missed (deleted user, etc.)."""
    if info is None:
        return {
            "user_name": None,
            "user_code": None,
            "assigned_admin_id": None,
            "assigned_admin_name": None,
            "assigned_broker_id": None,
            "assigned_broker_name": None,
            "assigned_broker_is_sub": False,
            "parent_broker_id": None,
            "parent_broker_name": None,
        }
    return {k: info.get(k) for k in (
        "user_name",
        "user_code",
        "assigned_admin_id",
        "assigned_admin_name",
        "assigned_broker_id",
        "assigned_broker_name",
        "assigned_broker_is_sub",
        "parent_broker_id",
        "parent_broker_name",
    )}
