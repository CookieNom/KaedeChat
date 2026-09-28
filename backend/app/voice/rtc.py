"""Instance RTC configuration and durable room placement (no provider defaults)."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal, cast
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException
from pydantic import ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.model_validation import UnambiguousInputModel
from app.core.settings import Settings
from app.db.models import RTCConfiguration, RTCRoomPlacement
from app.db.session import create_engine_and_sessionmaker

WEBHOOK_PATH = "/api/v1/voice/cinnamon/webhook"


def endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "wss"
        or not parsed.hostname
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or "?" in value
        or "#" in value
        or any(c.isspace() for c in value)
    ):
        raise ValueError("Use a public wss:// hostname without a path, credentials, or query")
    host = parsed.hostname
    try:
        public = ipaddress.ip_address(host).is_global
    except ValueError:
        public = "." in host and not host.endswith((".localhost", ".local", ".internal"))
    if not public:
        raise ValueError("RTC endpoints must use a public hostname")
    return "wss://" + parsed.netloc.lower()


class RTCRegion(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", max_length=63)
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(max_length=2048)
    enabled: bool = True

    _url = field_validator("url")(endpoint)

    @field_validator("id")
    @classmethod
    def reserved_id(cls, value: str) -> str:
        if value == "automatic":
            raise ValueError("automatic is reserved for automatic routing")
        return value

    @field_validator("name")
    @classmethod
    def display_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("Enter a region display name")
        return value


class RTCConfig(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["builtin", "cinnamon"] = "builtin"
    api_key: str = Field(default="", max_length=128, pattern=r"^[A-Za-z0-9_-]*$")
    api_secret: SecretStr | None = Field(default=None, max_length=256)
    automatic_url: str = Field(default="", max_length=2048)
    regions: list[RTCRegion] = Field(default_factory=list, max_length=64)
    default_region: str | None = None
    allow_region_selection: bool = True

    @field_validator("automatic_url")
    @classmethod
    def automatic_endpoint(cls, value: str) -> str:
        return endpoint(value) if value else ""

    @field_validator("api_secret")
    @classmethod
    def valid_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            raw = value.get_secret_value()
            if not 32 <= len(raw) <= 256 or not raw.isascii() or not raw.isprintable():
                raise ValueError("The project secret must be 32–256 printable ASCII characters")
        return value

    @model_validator(mode="after")
    def valid_regions(self) -> RTCConfig:
        if len({r.id for r in self.regions}) != len(self.regions):
            raise ValueError("Region IDs must be unique")
        if self.default_region is not None and self.default_region not in {
            r.id for r in self.regions if r.enabled
        }:
            raise ValueError("Choose Automatic or an enabled region as the default")
        return self


class RoutingHints(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")
    region: str | None = Field(default=None, max_length=63)
    latency: dict[str, Annotated[float, Field(strict=True)]] = Field(
        default_factory=dict, max_length=64
    )
    probe_ticket: str | None = Field(default=None, max_length=64)

    @field_validator("latency")
    @classmethod
    def valid_latency(cls, value: dict[str, float]) -> dict[str, float]:
        if any(not 0 < ms <= 60_000 for ms in value.values()) or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", key) for key in value
        ):
            raise ValueError("Latency must contain positive finite milliseconds up to 60000")
        return value


def seal(settings: Settings, value: dict[str, object]) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(settings.secret_key_bytes).encrypt(
        nonce, json.dumps(value).encode(), b"kaede-rtc-v1"
    )


def unseal(settings: Settings, value: bytes) -> dict[str, object]:
    decoded = json.loads(
        AESGCM(settings.secret_key_bytes).decrypt(value[:12], value[12:], b"kaede-rtc-v1")
    )
    if not isinstance(decoded, dict):
        raise ValueError("Invalid stored RTC credentials")
    return decoded


_session_factory: async_sessionmaker[AsyncSession] | None = None


def configure_rtc_sessions(factory: async_sessionmaker[AsyncSession] | None) -> None:
    global _session_factory
    _session_factory = factory


@asynccontextmanager
async def rtc_session(settings: Settings) -> AsyncIterator[AsyncSession]:
    # Isolated commits: room placement must survive a failed token request and
    # must not commit unrelated caller work. Workers use this same path.
    if _session_factory is not None:
        async with _session_factory() as session:
            yield session
        return
    engine, factory = create_engine_and_sessionmaker(settings.database_url.get_secret_value())
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


async def configuration(session: AsyncSession, settings: Settings) -> RTCConfig:
    row = await session.get(RTCConfiguration, settings.domain)
    if row is None:
        return RTCConfig()
    data = dict(row.configuration)
    if row.credentials:
        data.update(unseal(settings, row.credentials))
    return RTCConfig.model_validate(data)


def provider_settings(settings: Settings, config: RTCConfig, url: str) -> Settings:
    if config.provider == "builtin":
        return settings
    resolved = settings.model_copy(
        update={
            "voice_livekit_url": "https://" + url.removeprefix("wss://"),
            "voice_public_url": url,
            "voice_api_key": SecretStr(config.api_key),
            "voice_api_secret": config.api_secret,
        }
    )
    resolved._rtc_provider = "cinnamon"
    return resolved


async def room_settings(settings: Settings, room: str) -> Settings:
    async with rtc_session(settings) as session:
        row = await session.get(RTCRoomPlacement, (settings.domain, room))
        if row is None:
            return settings  # Rooms created before the integration are local.
        return placement_settings(settings, row)


def placement_settings(settings: Settings, row: RTCRoomPlacement) -> Settings:
    data = unseal(settings, row.credentials)
    resolved = settings.model_copy(
        update={
            "voice_livekit_url": row.control_url,
            "voice_public_url": row.connection_url,
            "voice_api_key": SecretStr(str(data["api_key"])),
            "voice_api_secret": SecretStr(str(data["api_secret"])),
        }
    )
    resolved._rtc_provider = cast(Literal["builtin", "cinnamon"], row.provider)
    return resolved


async def placement_lock(session: AsyncSession, domain: str, room: str) -> None:
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"rtc:{domain}:{room}", 0)))
    )


def select_endpoint(config: RTCConfig, hints: RoutingHints | None) -> str:
    region = config.default_region
    if hints is not None and hints.region is not None:
        if not config.allow_region_selection:
            raise HTTPException(403, detail={"code": "RTC_REGION_SELECTION_DISABLED"})
        region = None if hints.region == "automatic" else hints.region
    if region is None:
        if not config.automatic_url:
            raise HTTPException(409, detail={"code": "RTC_AUTOMATIC_ENDPOINT_REQUIRED"})
        return config.automatic_url
    for entry in config.regions:
        if entry.id == region and entry.enabled:
            return entry.url
    raise HTTPException(400, detail={"code": "RTC_REGION_INVALID"})
