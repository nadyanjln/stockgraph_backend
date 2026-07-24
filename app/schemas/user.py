"""User profile schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UserProfileUpdate(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("display name must contain at least 2 characters")
        return normalized


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    name: str
    email: str
    avatar_url: str | None = None
    provider: str = "email"
    is_verified: bool = False
    created_at: datetime
