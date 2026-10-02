"""Reusable response envelopes and pagination schemas."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class APIResponse(BaseModel, Generic[T]):
    """Uniform success envelope returned by every endpoint."""

    success: bool = True
    data: T | None = None
    message: str | None = None
    total: int | None = None  # populated by paginated endpoints


class PageMeta(BaseModel):
    page: int = 1
    page_size: int = 20
    total: int = 0
    total_pages: int = 0


class CursorMeta(BaseModel):
    next_cursor: str | None = None
    has_more: bool = False
    page_size: int = 20


class Page(BaseModel, Generic[T]):
    items: list[T] = Field(default_factory=list)
    meta: PageMeta = Field(default_factory=PageMeta)


class CursorPage(BaseModel, Generic[T]):
    items: list[T] = Field(default_factory=list)
    meta: CursorMeta = Field(default_factory=CursorMeta)


class ErrorEnvelope(BaseModel):
    code: str
    message: str
    details: dict = Field(default_factory=dict)


class IdResponse(BaseModel):
    id: str


class OkResponse(BaseModel):
    ok: bool = True
    message: str | None = None


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    status: str
    version: str
    db: bool
    redis: bool
