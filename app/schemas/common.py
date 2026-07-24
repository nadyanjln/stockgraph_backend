"""Generic response envelopes."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class APIResponse(BaseModel, Generic[T]):
    success: bool = True
    message: str = "OK"
    data: T | None = None


class ErrorResponse(BaseModel):
    success: bool = False
    message: str
    code: str = Field(default="error")
    details: dict | None = None
