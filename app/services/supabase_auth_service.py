"""Validate Supabase access tokens and normalize their user identity."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import requests

from app.utils.exceptions import AppError


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class AuthProviderUnavailableError(AppError):
    status_code = 503
    code = "auth_provider_unavailable"


@dataclass(frozen=True, slots=True)
class SupabaseIdentity:
    user_id: str
    email: str
    full_name: str
    avatar_url: str | None
    provider: str


class SupabaseAuthService:
    def __init__(self) -> None:
        self.url = os.getenv("SUPABASE_URL", "").rstrip("/")
        self.anon_key = os.getenv("SUPABASE_ANON_KEY", "")
        self.timeout = float(os.getenv("SUPABASE_AUTH_TIMEOUT_SECONDS", "8"))

    def authenticate(self, access_token: str) -> SupabaseIdentity:
        if not self.url or not self.anon_key:
            raise AuthProviderUnavailableError(
                "Supabase Auth is not configured on the server"
            )
        if not access_token:
            raise UnauthorizedError("authentication required")

        try:
            response = requests.get(
                f"{self.url}/auth/v1/user",
                headers={
                    "apikey": self.anon_key,
                    "Authorization": f"Bearer {access_token}",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AuthProviderUnavailableError(
                "authentication provider is temporarily unavailable"
            ) from exc

        if response.status_code in {401, 403}:
            raise UnauthorizedError("session is invalid or expired")
        if not response.ok:
            raise AuthProviderUnavailableError(
                "authentication provider could not validate the session"
            )

        payload: dict[str, Any] = response.json()
        metadata = payload.get("user_metadata") or {}
        app_metadata = payload.get("app_metadata") or {}
        email = str(payload.get("email") or "").strip().lower()
        user_id = str(payload.get("id") or "").strip()
        if not user_id or not email:
            raise UnauthorizedError("authenticated account has no usable email")

        identities = payload.get("identities") or []
        identity_provider = ""
        if identities and isinstance(identities[0], dict):
            identity_provider = str(identities[0].get("provider") or "")

        provider = str(app_metadata.get("provider") or identity_provider or "email")
        full_name = str(
            metadata.get("full_name")
            or metadata.get("name")
            or email.split("@", 1)[0]
        ).strip()
        avatar_url = str(
            metadata.get("avatar_url") or metadata.get("picture") or ""
        ).strip()

        return SupabaseIdentity(
            user_id=user_id,
            email=email,
            full_name=full_name[:100],
            avatar_url=avatar_url or None,
            provider=provider[:32],
        )
