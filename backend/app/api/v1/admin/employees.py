"""Employee (staff sub-user) management surface.

Available to EVERY admin (super + sub-admin) via the `AdminTier` dependency —
each manages their OWN employees within their own scope. Granted sections are
capped at the creating admin's own permissions (super-admin uncapped). Employees
themselves (and brokers) are rejected by `AdminTier`, so an employee can never
create employees.
"""

from __future__ import annotations

from beanie import PydanticObjectId
from fastapi import APIRouter

from app.core.dependencies import AdminTier
from app.models.user import User
from app.schemas.admin.employee import (
    CreateEmployeeRequest,
    EmployeeDTO,
    ResetEmployeePasswordRequest,
    UpdateEmployeePermissionsRequest,
)
from app.schemas.common import APIResponse
from app.services import employee_service as emp_svc

router = APIRouter(prefix="/employees", tags=["admin-employees"])


async def _ser_employee(emp: User) -> EmployeeDTO:
    employer_name = None
    if emp.assigned_admin_id is not None:
        parent = await User.get(emp.assigned_admin_id)
        employer_name = parent.full_name if parent is not None else None
    return EmployeeDTO(
        id=str(emp.id),
        user_code=emp.user_code,
        full_name=emp.full_name,
        email=emp.email,
        mobile=emp.mobile,
        status=emp.status.value,
        permissions=emp.admin_permissions,
        employer_admin_id=str(emp.assigned_admin_id) if emp.assigned_admin_id else None,
        employer_name=employer_name,
        created_at=emp.created_at,
    )


@router.get("", response_model=APIResponse[dict])
async def list_employees(
    admin: AdminTier,
    q: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    rows, total = await emp_svc.list_employees(
        actor=admin, status=status, q=q, page=page, page_size=page_size
    )
    items = [await _ser_employee(e) for e in rows]
    return APIResponse(
        data={
            "items": items,
            "meta": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (total + page_size - 1) // page_size,
            },
        }
    )


@router.post("", response_model=APIResponse[EmployeeDTO])
async def create_employee(payload: CreateEmployeeRequest, admin: AdminTier):
    emp = await emp_svc.create_employee(
        actor=admin,
        email=payload.email,
        mobile=payload.mobile,
        password=payload.password,
        full_name=payload.full_name,
        permissions=payload.permissions,
    )
    return APIResponse(data=await _ser_employee(emp))


@router.get("/{employee_id}", response_model=APIResponse[EmployeeDTO])
async def get_employee(employee_id: str, admin: AdminTier):
    emp = await emp_svc._get_employee_owned_or_404(employee_id, admin)
    return APIResponse(data=await _ser_employee(emp))


@router.put("/{employee_id}/permissions", response_model=APIResponse[EmployeeDTO])
async def update_employee_permissions(
    employee_id: str, payload: UpdateEmployeePermissionsRequest, admin: AdminTier
):
    emp = await emp_svc.update_permissions(
        employee_id, payload.permissions, actor=admin
    )
    return APIResponse(data=await _ser_employee(emp))


@router.post("/{employee_id}/block", response_model=APIResponse[EmployeeDTO])
async def block_employee(employee_id: str, admin: AdminTier):
    emp = await emp_svc.block_employee(employee_id, actor=admin)
    return APIResponse(data=await _ser_employee(emp))


@router.post("/{employee_id}/unblock", response_model=APIResponse[EmployeeDTO])
async def unblock_employee(employee_id: str, admin: AdminTier):
    emp = await emp_svc.unblock_employee(employee_id, actor=admin)
    return APIResponse(data=await _ser_employee(emp))


@router.post("/{employee_id}/reset-password", response_model=APIResponse[dict])
async def reset_employee_password(
    employee_id: str, payload: ResetEmployeePasswordRequest, admin: AdminTier
):
    await emp_svc.reset_password(employee_id, payload.new_password, actor=admin)
    return APIResponse(data={"ok": True})


@router.delete("/{employee_id}", response_model=APIResponse[dict])
async def delete_employee(employee_id: str, admin: AdminTier):
    await emp_svc.delete_employee(employee_id, actor=admin)
    return APIResponse(data={"ok": True})
