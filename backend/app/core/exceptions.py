"""Domain exception hierarchy + global handlers.

Every exception carries a machine-readable `code` so clients can react
deterministically. The global handler converts any AppError to a JSON
response; unexpected exceptions are logged and returned as 500.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base class for all domain errors."""

    code: str = "APP_ERROR"
    status_code: int = status.HTTP_400_BAD_REQUEST
    message: str = "An error occurred"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


# ── Auth ─────────────────────────────────────────────────────────────
class AuthError(AppError):
    code = "AUTH_ERROR"
    status_code = status.HTTP_401_UNAUTHORIZED
    message = "Authentication failed"


class InvalidCredentialsError(AuthError):
    code = "INVALID_CREDENTIALS"
    message = "Invalid email/mobile or password"


class TokenExpiredError(AuthError):
    code = "TOKEN_EXPIRED"
    message = "Your session has expired. Please log in again."


class TokenInvalidError(AuthError):
    code = "TOKEN_INVALID"
    message = "Invalid or malformed token"


class TwoFARequiredError(AuthError):
    code = "TWO_FA_REQUIRED"
    status_code = status.HTTP_401_UNAUTHORIZED
    message = "Two-factor authentication code required"


class TwoFAInvalidError(AuthError):
    code = "TWO_FA_INVALID"
    message = "Invalid two-factor authentication code"


class AccountBlockedError(AuthError):
    code = "ACCOUNT_BLOCKED"
    status_code = status.HTTP_403_FORBIDDEN
    message = "Your account has been blocked. Contact support."


class AccountInactiveError(AuthError):
    code = "ACCOUNT_INACTIVE"
    status_code = status.HTTP_403_FORBIDDEN
    message = "Your account is not active"


class TenantLoginNotAllowedError(AuthError):
    # Multi-tenant login isolation: the credentials are valid, but the
    # branded domain the login arrived on belongs to a DIFFERENT admin
    # than the one this user is registered under. Raised only after the
    # password/2FA verify, so only the account's real owner ever sees it
    # (no cross-tenant account enumeration).
    code = "TENANT_LOGIN_NOT_ALLOWED"
    status_code = status.HTTP_403_FORBIDDEN
    message = (
        "This account isn't registered on this site. "
        "Please use your own login link."
    )


class MaintenanceModeError(AuthError):
    # The user's owning admin has turned maintenance mode ON for their whole
    # pool. Blocks new logins AND kicks existing sessions (raised from the
    # per-request auth dependency). 503 so it reads as "platform temporarily
    # down" rather than an account problem. The frontend matches on the code
    # to show a maintenance popup and log the user out.
    code = "MAINTENANCE_MODE"
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    message = "The platform is under maintenance. Please try again later."


class InsufficientPermissionsError(AppError):
    code = "FORBIDDEN"
    status_code = status.HTTP_403_FORBIDDEN
    message = "You don't have permission to perform this action"


# ── Resource / validation ────────────────────────────────────────────
class NotFoundError(AppError):
    code = "NOT_FOUND"
    status_code = status.HTTP_404_NOT_FOUND
    message = "Resource not found"


class ConflictError(AppError):
    code = "CONFLICT"
    status_code = status.HTTP_409_CONFLICT
    message = "Resource conflict"


class ValidationFailedError(AppError):
    code = "VALIDATION_FAILED"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    message = "Validation failed"


class RateLimitExceededError(AppError):
    code = "RATE_LIMIT_EXCEEDED"
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    message = "Too many requests, please slow down"


# ── Trading-domain (Phase 4 surfaces these; declared here once) ─────
class OrderRejectedError(AppError):
    code = "ORDER_REJECTED"
    status_code = status.HTTP_400_BAD_REQUEST
    message = "Order rejected"


class InsufficientFundsError(AppError):
    code = "INSUFFICIENT_FUNDS"
    status_code = status.HTTP_400_BAD_REQUEST
    message = "Insufficient funds in wallet"


class SegmentNotAllowedError(AppError):
    code = "SEGMENT_NOT_ALLOWED"
    status_code = status.HTTP_403_FORBIDDEN
    message = "Trading is not allowed in this segment"


class MarketClosedError(AppError):
    code = "MARKET_CLOSED"
    status_code = status.HTTP_400_BAD_REQUEST
    message = "Market is closed"


# ── Global handlers ──────────────────────────────────────────────────
async def _app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.to_dict()})


async def _http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": "HTTP_ERROR", "message": str(exc.detail), "details": {}}},
    )


async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": "VALIDATION_FAILED",
                "message": "Request validation failed",
                "details": {"errors": exc.errors()},
            }
        },
    )


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_exception", extra={"path": request.url.path})
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": "INTERNAL_SERVER_ERROR",
                "message": "An unexpected error occurred",
                "details": {},
            }
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_handler)
