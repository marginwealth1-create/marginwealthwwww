"""Admin operations on EMPLOYEE (staff sub-user) accounts.

Every admin (super or sub) manages their OWN employees. An employee reuses the
``AdminPermissions`` boolean-per-section model; the granted sections are capped
at the creating admin's own permissions (super-admin → uncapped). Employees are
bound to their creator via ``assigned_admin_id`` (the pool they operate on) and
``created_by`` (ownership for management). All mutations write an audit entry.

HTTP shaping lives in app.api.v1.admin.employees.
"""

from __future__ import annotations

import re
from typing import Any

from beanie import PydanticObjectId

from app.core.exceptions import (
    InsufficientPermissionsError,
    NotFoundError,
    ValidationFailedError,
)
from app.models.audit_log import AuditAction
from app.models.user import AdminPermissions, User, UserRole, UserStatus
from app.services import user_service
from app.services.audit_service import log_event


def cap_permissions(actor: User, requested: AdminPermissions) -> AdminPermissions:
    """Clamp requested sections to what the creating admin actually holds.

    SUPER_ADMIN grants anything. A sub-ADMIN can only grant a section they
    themselves have (``granted[k] = requested[k] AND actor.admin_permissions[k]``)
    so an employee can never out-power its creator.
    """
    if actor.role == UserRole.SUPER_ADMIN:
        return requested
    ceiling = actor.admin_permissions or AdminPermissions()
    capped: dict[str, Any] = {}
    for field in AdminPermissions.model_fields:
        capped[field] = bool(
            getattr(requested, field, False) and getattr(ceiling, field, False)
        )
    return AdminPermissions(**capped)


async def _get_employee_owned_or_404(
    employee_id: str | PydanticObjectId, actor: User
) -> User:
    """Load an EMPLOYEE and 404/403 unless the actor owns it.

    Ownership: super-admin manages any employee; a sub-admin manages only the
    employees they created (``created_by == actor.id``)."""
    try:
        oid = PydanticObjectId(employee_id)
    except Exception as e:
        raise ValidationFailedError("Invalid employee id") from e
    emp = await User.get(oid)
    if emp is None or emp.role != UserRole.EMPLOYEE:
        raise NotFoundError("Employee not found")
    if actor.role != UserRole.SUPER_ADMIN and emp.created_by != actor.id:
        raise InsufficientPermissionsError("Employee not in your scope")
    return emp


async def create_employee(
    *,
    actor: User,
    email: str,
    mobile: str,
    password: str,
    full_name: str,
    permissions: AdminPermissions,
) -> User:
    granted = cap_permissions(actor, permissions)
    emp = await user_service.create_user(
        email=email,
        mobile=mobile,
        password=password,
        full_name=full_name,
        role=UserRole.EMPLOYEE,
        status=UserStatus.ACTIVE,
        created_by=actor.id,
        # The employee operates on THIS admin's pool.
        assigned_admin_id=actor.id,
    )
    emp.admin_permissions = granted
    await emp.save()
    await log_event(
        action=AuditAction.EMPLOYEE_CREATE,
        entity_type="User",
        entity_id=emp.id,
        actor_id=actor.id,
        target_user_id=emp.id,
        new_values={"permissions": granted.model_dump(), "employer_admin_id": str(actor.id)},
    )
    return emp


async def update_permissions(
    employee_id: str | PydanticObjectId,
    permissions: AdminPermissions,
    *,
    actor: User,
) -> User:
    emp = await _get_employee_owned_or_404(employee_id, actor)
    granted = cap_permissions(actor, permissions)
    old = emp.admin_permissions.model_dump() if emp.admin_permissions else None
    emp.admin_permissions = granted
    await emp.save()
    # New perms take effect on the employee's next request (the admin app
    # calls refreshMe on mount); no session bump needed for a grant/revoke.
    await log_event(
        action=AuditAction.EMPLOYEE_PERMS_UPDATE,
        entity_type="User",
        entity_id=emp.id,
        actor_id=actor.id,
        target_user_id=emp.id,
        old_values={"permissions": old},
        new_values={"permissions": granted.model_dump()},
    )
    return emp


async def block_employee(
    employee_id: str | PydanticObjectId, *, actor: User
) -> User:
    emp = await _get_employee_owned_or_404(employee_id, actor)
    emp.status = UserStatus.BLOCKED
    await emp.save()
    from app.services import auth_service as _auth

    await _auth.revoke_user_sessions(emp)  # force-logout immediately
    await log_event(
        action=AuditAction.BLOCK,
        entity_type="User",
        entity_id=emp.id,
        actor_id=actor.id,
        target_user_id=emp.id,
        metadata={"kind": "EMPLOYEE"},
    )
    return emp


async def unblock_employee(
    employee_id: str | PydanticObjectId, *, actor: User
) -> User:
    emp = await _get_employee_owned_or_404(employee_id, actor)
    emp.status = UserStatus.ACTIVE
    emp.failed_login_count = 0
    emp.locked_until = None
    await emp.save()
    await log_event(
        action=AuditAction.UNBLOCK,
        entity_type="User",
        entity_id=emp.id,
        actor_id=actor.id,
        target_user_id=emp.id,
        metadata={"kind": "EMPLOYEE"},
    )
    return emp


async def reset_password(
    employee_id: str | PydanticObjectId,
    new_password: str,
    *,
    actor: User,
) -> User:
    from app.core.security import hash_password

    emp = await _get_employee_owned_or_404(employee_id, actor)
    emp.password_hash = hash_password(new_password)
    await emp.save()
    from app.services import auth_service as _auth

    await _auth.revoke_user_sessions(emp)  # kill old-password sessions
    await log_event(
        action=AuditAction.PASSWORD_RESET,
        entity_type="User",
        entity_id=emp.id,
        actor_id=actor.id,
        target_user_id=emp.id,
        metadata={"kind": "EMPLOYEE"},
    )
    return emp


async def delete_employee(
    employee_id: str | PydanticObjectId, *, actor: User
) -> None:
    emp = await _get_employee_owned_or_404(employee_id, actor)
    from app.services import auth_service as _auth

    await _auth.revoke_user_sessions(emp)
    await log_event(
        action=AuditAction.EMPLOYEE_DELETE,
        entity_type="User",
        entity_id=emp.id,
        actor_id=actor.id,
        target_user_id=emp.id,
    )
    # Employees hold no financial/trading data — safe to hard-delete.
    await emp.delete()


async def list_employees(
    *,
    actor: User,
    status: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[User], int]:
    query: dict[str, Any] = {"role": UserRole.EMPLOYEE.value}
    # Sub-admins see only their own employees; super-admin sees all.
    if actor.role != UserRole.SUPER_ADMIN:
        query["created_by"] = actor.id
    if status:
        query["status"] = status
    if q:
        regex = re.compile(re.escape(q.strip()), re.IGNORECASE)
        query["$or"] = [
            {"email": regex},
            {"mobile": regex},
            {"user_code": regex},
            {"full_name": regex},
        ]
    total = await User.find(query).count()
    rows = (
        await User.find(query)
        .sort("-created_at")
        .skip((page - 1) * page_size)
        .limit(page_size)
        .to_list()
    )
    return rows, total
