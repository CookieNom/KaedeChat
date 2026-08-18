from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from app.core.types import EntityRef

USERNAME_RE = re.compile(r"^[a-z0-9_.]{2,32}$")
PASSWORD_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class PasswordKdfBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[2]
    algorithm: Literal["PBKDF2-SHA256"]
    iterations: Literal[600_000]
    auth_salt: str = Field(pattern=r"^[A-Za-z0-9_-]{22}$")


class PasswordKdfRegistration(PasswordKdfBase):
    vault_salt: str = Field(pattern=r"^[A-Za-z0-9_-]{22}$")


class PasswordUpgrade(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    password_kdf: PasswordKdfBase


class PasswordKdfLookupRequest(BaseModel):
    identifier: str = Field(min_length=2, max_length=320)


class RegisterRequest(BaseModel):
    username: str
    email: EmailStr | None = None
    password: str = Field(min_length=10, max_length=256)
    password_kdf: PasswordKdfRegistration
    turnstile_token: str | None = Field(default=None, min_length=1, max_length=2048)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        username = value.lower()
        if not USERNAME_RE.fullmatch(username):
            raise ValueError("must match ^[a-z0-9_.]{2,32}$")
        return username

    @model_validator(mode="after")
    def validate_derived_password(self) -> RegisterRequest:
        if PASSWORD_SECRET_RE.fullmatch(self.password) is None:
            raise ValueError("password must be a derived authentication secret")
        return self


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=2, max_length=320)
    password: str = Field(min_length=1, max_length=256)
    password_kdf_version: Literal[0, 2] = 2
    password_upgrade: PasswordUpgrade | None = None
    device_name: str | None = Field(default=None, max_length=100)
    turnstile_token: str | None = Field(default=None, min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_password_protocol(self) -> LoginRequest:
        if self.password_kdf_version == 2 and PASSWORD_SECRET_RE.fullmatch(self.password) is None:
            raise ValueError("password must be a version 2 derived authentication secret")
        if self.password_kdf_version == 2 and self.password_upgrade is not None:
            raise ValueError("password upgrade is only valid for legacy credentials")
        return self


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
    password_kdf: PasswordKdfBase

    @model_validator(mode="after")
    def validate_derived_password(self) -> PasswordResetRequest:
        if PASSWORD_SECRET_RE.fullmatch(self.password) is None:
            raise ValueError("password must be a derived authentication secret")
        return self


class MfaCodeRequest(BaseModel):
    code: str = Field(min_length=6, max_length=32)


class MfaSetupRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    password_kdf_version: Literal[0, 2] = 2
    current_code: str | None = Field(default=None, min_length=6, max_length=32)


class MfaDisableRequest(MfaCodeRequest):
    password: str = Field(min_length=1, max_length=256)
    password_kdf_version: Literal[0, 2] = 2


class EmailChangeRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    password_kdf_version: Literal[0, 2] = 2


class SessionSummary(BaseModel):
    id: str
    device_name: str | None
    user_agent: str | None
    ip_address: str | None
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    current: bool


def guild_navigation_reference(value: object) -> object:
    if isinstance(value, dict):
        identifier = value.get("id")
        domain = value.get("origin_domain", value.get("domain"))
        if identifier is None or domain is None:
            raise ValueError("must include id and origin_domain")
        return f"{identifier}@{domain}"
    return value


GuildNavigationRef = Annotated[EntityRef, BeforeValidator(guild_navigation_reference)]


class GuildNavigationGuildItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["guild"]
    guild: GuildNavigationRef


class GuildNavigationGroupItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["group"]
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,36}$")
    name: str = Field(min_length=1, max_length=32)
    guilds: list[GuildNavigationRef] = Field(min_length=1, max_length=100)
    collapsed: bool = False

    @field_validator("name")
    @classmethod
    def clean_group_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must contain a non-whitespace character")
        return cleaned


GuildNavigationItem = Annotated[
    GuildNavigationGuildItem | GuildNavigationGroupItem,
    Field(discriminator="kind"),
]


class GuildNavigationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[GuildNavigationItem] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def require_unique_items(self) -> GuildNavigationUpdate:
        group_ids: set[str] = set()
        guilds: set[str] = set()
        for item in self.items:
            refs: tuple[str, ...]
            if isinstance(item, GuildNavigationGuildItem):
                refs = (str(item.guild),)
            else:
                if item.id in group_ids:
                    raise ValueError("group IDs must be unique")
                group_ids.add(item.id)
                refs = tuple(str(guild) for guild in item.guilds)
                if len(refs) != len(set(refs)):
                    raise ValueError("a group cannot contain the same guild more than once")
            for guild in refs:
                if guild in guilds:
                    raise ValueError("each guild can appear only once")
                guilds.add(guild)
        return self


class SettingsPatch(BaseModel):
    locale: str | None = Field(default=None, min_length=2, max_length=16)
    theme: Literal["system", "light", "dark"] | None = None
    dm_privacy: Literal["everyone", "shared_guild", "friends"] | None = None
    presence_preference: Literal["online", "idle", "dnd", "invisible"] | None = None
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
