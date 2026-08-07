from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

USERNAME_RE = re.compile(r"^[a-z0-9_.]{2,32}$")


class RegisterRequest(BaseModel):
    username: str
    email: EmailStr | None = None
    password: str = Field(min_length=10, max_length=256)
    turnstile_token: str | None = Field(default=None, min_length=1, max_length=2048)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        username = value.lower()
        if not USERNAME_RE.fullmatch(username):
            raise ValueError("must match ^[a-z0-9_.]{2,32}$")
        return username


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=2, max_length=320)
    password: str = Field(min_length=1, max_length=256)
    device_name: str | None = Field(default=None, max_length=100)
    turnstile_token: str | None = Field(default=None, min_length=1, max_length=2048)


class TokenResponse(BaseModel):
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: Literal["opaque"] = "opaque"  # noqa: S105 - protocol label, not a secret
    expires_in: int | None = None
    mfa_required: bool = False
    mfa_ticket: str | None = None


class TokenRequest(BaseModel):
    token: str = Field(min_length=16, max_length=256)


class VerificationResendRequest(BaseModel):
    email: EmailStr


class RefreshRequest(BaseModel):
    refresh_token: str | None = Field(default=None, min_length=16, max_length=256)


class MfaLoginRequest(BaseModel):
    ticket: str = Field(min_length=16, max_length=256)
    code: str = Field(min_length=6, max_length=32)
    device_name: str | None = Field(default=None, max_length=100)


class PasswordForgotRequest(BaseModel):
    email: EmailStr


class PasswordResetRequest(BaseModel):
    token: str = Field(min_length=16, max_length=256)
    password: str = Field(min_length=10, max_length=256)


class MfaCodeRequest(BaseModel):
    code: str = Field(min_length=6, max_length=32)


class MfaSetupRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    current_code: str | None = Field(default=None, min_length=6, max_length=32)


class MfaDisableRequest(MfaCodeRequest):
    password: str = Field(min_length=1, max_length=256)


class EmailChangeRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class SettingsPatch(BaseModel):
    locale: str | None = Field(default=None, min_length=2, max_length=16)
    theme: Literal["system", "light", "dark"] | None = None
    dm_privacy: Literal["everyone", "shared_guild", "friends"] | None = None
    notification_settings: dict[str, object] | None = None

    @model_validator(mode="after")
    def require_nonnull_change(self) -> SettingsPatch:
        if not self.model_fields_set:
            raise ValueError("at least one settings field is required")
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"settings {field} cannot be null")
        return self

    @field_validator("locale")
    @classmethod
    def validate_locale(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", value):
            raise ValueError("must be a valid language tag")
        return value

    @field_validator("notification_settings")
    @classmethod
    def limit_notification_settings(
        cls, value: dict[str, object] | None
    ) -> dict[str, object] | None:
        if value is not None:
            try:
                encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
            except (TypeError, ValueError) as exc:
                raise ValueError("must contain JSON-compatible values") from exc
            if len(encoded.encode("utf-8")) > 16 * 1024:
                raise ValueError("must be at most 16 KiB when encoded")
        return value
