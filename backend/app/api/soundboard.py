from __future__ import annotations

import asyncio
import math
import secrets
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import structlog
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
from pydantic import ConfigDict, Field, ValidationError, model_validator
from redis.asyncio import Redis
from sqlalchemy import exists, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.api.bots import (
    installation_for_channel,
    installation_for_guild,
    require_installation_scope,
    require_owned_attachments_for_installation,
)
from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.api.guilds import local_guild
from app.auth.instance_restrictions import require_remote_user_creation_allowed
from app.bots.auth import BotPrincipal, require_bot
from app.bots.installations import installation_allows_channel, usable_guild_installation
from app.chat.audit import add_audit_entry, normalize_audit_reason
from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.chat.expression_events import publish_guild_soundboard_sounds_update
from app.chat.guild_revision import (
    build_guild_authority_envelope,
    guild_authority_owner,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.payloads import soundboard_sound_payload, user_payload
from app.chat.permissions import (
    get_permissions,
    require_can_manage_expression,
    require_permissions,
)
from app.core.channel_types import is_soundboard_channel_type
from app.core.model_validation import UnambiguousInputModel
from app.core.permission_contract import required_permissions
from app.core.permissions import Permission
from app.core.rate_limits import ClientRateLimit, enforce_keyed_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef, Snowflake
from app.db.bot_models import BotInstallation
from app.db.models import (
    Attachment,
    Channel,
    Emoji,
    Guild,
    GuildMember,
    MediaTombstoneSource,
    SoundboardSound,
    User,
)
from app.federation.actor_intents import (
    build_human_actor_intent,
    validate_human_actor_intent,
    validate_worker_actor_intent,
)
from app.federation.client import signed_request
from app.federation.guild_management import (
    GuildManagementOperation,
    GuildManagementResult,
    proxy_remote_guild_management,
)
from app.federation.network import FederationNetworkError, decode_federation_response_json
from app.federation.replication import client_user_payload_from_profile, profile_from_user
from app.federation.schemas import FederationDomain, RemoteUserProfile, SnowflakeString
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
    require_guild_federation_access,
    validated_event_envelope,
)
from app.media.digest_revocation import DIGEST_REVOCATION_STATUSES, lock_asset_digest
from app.media.schemas import UploadTicketRequest
from app.media.service import (
    attachment_payload,
    bind_asset,
    create_upload_ticket,
    finalize_attachment,
    is_federated_human_authority_upload,
    ticket_payload,
)
from app.media.storage import (
    S3Storage,
    StorageError,
    media_url_origin,
    validate_media_url_origin,
)
from app.tasks import media_local_purge, media_process
from app.voice.rooms import guild_room_name, participant_identity
from app.voice.schemas import (
    SoundboardPlayRequest,
    SoundboardSoundCreate,
    SoundboardSoundUpdate,
)
from app.voice.service import load_voice_channel
from app.voice.state import Occupant, room_occupants

router = APIRouter(prefix="/api/v1/bots", tags=["bot soundboard"])
human_router = APIRouter(prefix="/api/v1", tags=["soundboard"])
federation_router = APIRouter(tags=["soundboard federation"])

log = structlog.get_logger(__name__)

SOUNDBOARD_CONTENT_TYPES = frozenset({"audio/mpeg", "audio/ogg"})
SOUNDBOARD_MAX_BYTES = 512 * 1024
SOUNDBOARD_MAX_DURATION_MS = 5_200
SOUNDBOARD_MAX_SOUNDS = 48
SOUNDBOARD_UPLOAD_LIMIT = ClientRateLimit("bot-soundboard-upload", 10, 60)
SOUNDBOARD_PLAY_LIMIT = ClientRateLimit("bot-soundboard-play", 5, 5)
SOUNDBOARD_FEDERATION_CAPABILITY = "guild-soundboard/1"
SOUNDBOARD_FEDERATION_QUERY_EVENT = "guild.soundboard.query"
SOUNDBOARD_FEDERATION_PLAY_EVENT = "guild.soundboard.play"
SOUNDBOARD_SOURCE_CAPABILITY_EVENT = "guild.soundboard.source-capability"
SOUNDBOARD_ACTOR_INTENT_ACTION = "soundboard.play"
SOUNDBOARD_FEDERATION_DEADLINE_SECONDS = 10
SOUNDBOARD_FEDERATION_MAX_RESPONSE_BYTES = 128 * 1024
SOUNDBOARD_EFFECT_TTL_SECONDS = 120


async def _proxy_human_management(
    session: AsyncSession,
    settings: Settings,
    guild_ref: EntityRef,
    auth: AuthenticatedUser,
    operation: GuildManagementOperation,
    payload: dict[str, Any],
) -> GuildManagementResult | None:
    return await proxy_remote_guild_management(
        session,
        settings,
        guild_ref,
        auth.user,
        operation,
        payload,
    )


class StrictSoundboardModel(UnambiguousInputModel):
    pass


class SoundboardFederationRef(StrictSoundboardModel):
    model_config = ConfigDict(extra="forbid")

    id: SnowflakeString
    domain: FederationDomain


class SoundboardFederationCaller(StrictSoundboardModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["human", "bot"]
    user: SoundboardFederationRef
    application: SoundboardFederationRef | None = None

    @model_validator(mode="after")
    def valid_application(self) -> SoundboardFederationCaller:
        if (self.kind == "bot") != (self.application is not None):
            raise ValueError("a bot caller must include exactly one application reference")
        return self


class SoundboardFederationRequest(StrictSoundboardModel):
    """Short-lived, requester-bound authorization for a private soundboard RPC."""

    model_config = ConfigDict(extra="forbid")

    guild: SoundboardFederationRef
    caller: SoundboardFederationCaller
    requesting_instance: FederationDomain
    request_id: str = Field(pattern=r"^kasb_[A-Za-z0-9_-]{32}$")
    issued_at: int = Field(ge=0)
    deadline: int = Field(ge=1)
    operation: Literal["list", "get", "play"]
    sound: SoundboardFederationRef | None = None
    source_guild: SoundboardFederationRef | None = None
    channel: SoundboardFederationRef | None = None
    sound_version: SnowflakeString | None = None
    volume: float | None = Field(default=None, ge=0, le=1)
    actor_intent: dict[str, object] | None = None

    @model_validator(mode="after")
    def operation_shape(self) -> SoundboardFederationRequest:
        if self.deadline <= self.issued_at or (
            self.deadline - self.issued_at > SOUNDBOARD_FEDERATION_DEADLINE_SECONDS
        ):
            raise ValueError("soundboard request deadline is invalid")
        if self.operation == "list":
            valid = (
                self.sound is None
                and self.source_guild is None
                and self.channel is None
                and self.sound_version is None
                and self.volume is None
                and self.actor_intent is None
            )
        elif self.operation == "get":
            valid = (
                self.sound is not None
                and self.source_guild is None
                and self.channel is None
                and self.sound_version is None
                and self.volume is None
                and self.actor_intent is None
            )
        else:
            valid = (
                self.sound is not None
                and self.channel is not None
                and self.sound_version is not None
                and self.actor_intent is not None
            )
        if not valid:
            raise ValueError("soundboard request fields do not match its operation")
        return self


class FederatedSoundboardSound(StrictSoundboardModel):
    model_config = ConfigDict(extra="forbid")

    id: SnowflakeString
    origin_domain: FederationDomain
    guild_id: SnowflakeString | None = None
    guild_domain: FederationDomain | None = None
    name: str = Field(min_length=2, max_length=32)
    media_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: Literal["audio/mpeg", "audio/ogg"]
    volume: float = Field(ge=0, le=1)
    emoji_id: SnowflakeString | None = None
    emoji_domain: FederationDomain | None = None
    emoji_name: str | None = Field(default=None, min_length=1, max_length=64)
    available: bool
    duration_ms: int = Field(ge=1, le=SOUNDBOARD_MAX_DURATION_MS)
    created_by_id: SnowflakeString | None = None
    created_by_domain: FederationDomain | None = None
    user: RemoteUserProfile | None = None
    version: SnowflakeString

    @model_validator(mode="after")
    def complete_emoji_reference(self) -> FederatedSoundboardSound:
        if (self.emoji_id is None) != (self.emoji_domain is None):
            raise ValueError("soundboard emoji reference is incomplete")
        if self.emoji_id is not None and self.emoji_name is not None:
            raise ValueError("soundboard emoji id and unicode name are mutually exclusive")
        if (self.guild_id is None) != (self.guild_domain is None):
            raise ValueError("soundboard guild reference is incomplete")
        if (self.created_by_id is None) != (self.created_by_domain is None):
            raise ValueError("soundboard creator reference is incomplete")
        return self


class DefaultSoundboardSoundConfig(StrictSoundboardModel):
    model_config = ConfigDict(extra="forbid")

    sound_id: SnowflakeString
    name: str = Field(min_length=2, max_length=32)
    media_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: Literal["audio/mpeg", "audio/ogg"]
    download_url: str = Field(min_length=8, max_length=4096)
    volume: float = Field(default=1, ge=0, le=1)
    emoji_name: str | None = Field(default=None, min_length=1, max_length=64)
    duration_ms: int = Field(ge=1, le=SOUNDBOARD_MAX_DURATION_MS)


class SoundboardFederationPage(StrictSoundboardModel):
    model_config = ConfigDict(extra="forbid")

    request: SoundboardFederationRequest
    sounds: list[FederatedSoundboardSound] = Field(max_length=SOUNDBOARD_MAX_SOUNDS)


class SoundboardFederationCapability(StrictSoundboardModel):
    model_config = ConfigDict(extra="forbid")

    sound: FederatedSoundboardSound
    download_url: str = Field(min_length=8, max_length=4096)
    media_authority: FederationDomain
    # The exact public object-storage origin chosen by the media authority.
    # It is covered by the authority signature together with ``download_url``;
    # clients compare the two before making any unauthenticated request.
    media_origin: str = Field(min_length=8, max_length=2048)
    effective_volume: float = Field(ge=0, le=1)
    expires_in: int = Field(ge=1, le=60)


class SoundboardSourceCapabilityRequest(StrictSoundboardModel):
    """Target-authority request for media hosted by a third guild authority."""

    model_config = ConfigDict(extra="forbid")

    source_guild: SoundboardFederationRef
    target_guild: SoundboardFederationRef
    target_channel: SoundboardFederationRef
    sound: SoundboardFederationRef
    sound_version: SnowflakeString
    caller: SoundboardFederationCaller
    requesting_instance: FederationDomain
    request_id: str = Field(pattern=r"^kasc_[A-Za-z0-9_-]{32}$")
    issued_at: int = Field(ge=0)
    deadline: int = Field(ge=1)
    volume: float | None = Field(default=None, ge=0, le=1)
    target_installation_revision: SnowflakeString | None = None
    actor_intent: dict[str, object]

    @model_validator(mode="after")
    def authority_shape(self) -> SoundboardSourceCapabilityRequest:
        if self.deadline <= self.issued_at or (
            self.deadline - self.issued_at > SOUNDBOARD_FEDERATION_DEADLINE_SECONDS
        ):
            raise ValueError("soundboard source request deadline is invalid")
        if (
            self.sound.domain != self.source_guild.domain
            or self.target_guild.domain != self.requesting_instance
            or self.target_channel.domain != self.requesting_instance
            or self.source_guild.domain == self.requesting_instance
        ):
            raise ValueError("soundboard source request authority binding is invalid")
        if self.caller.kind == "bot":
            if self.target_installation_revision is None:
                raise ValueError("bot soundboard source request lacks runtime revisions")
        elif self.target_installation_revision is not None:
            raise ValueError("human soundboard source request carries bot revisions")
        return self


class SoundboardSourceCapability(StrictSoundboardModel):
    model_config = ConfigDict(extra="forbid")

    request: SoundboardSourceCapabilityRequest
    capability: SoundboardFederationCapability


class SoundboardFederationPlay(StrictSoundboardModel):
    model_config = ConfigDict(extra="forbid")

    request: SoundboardFederationRequest
    capability: SoundboardFederationCapability
    guild: SoundboardFederationRef
    channel: SoundboardFederationRef
    user: SoundboardFederationRef
    delivery_id: str = Field(pattern=r"^kase_[A-Za-z0-9_-]{32}$")


def _federation_ref(entity_id: int, domain: str) -> SoundboardFederationRef:
    return SoundboardFederationRef(id=str(entity_id), domain=domain)


def _federation_caller(
    user: User,
    *,
    application_id: int | None = None,
    application_domain: str | None = None,
) -> SoundboardFederationCaller:
    if (application_id is None) != (application_domain is None):
        raise RuntimeError("application federation reference is incomplete")
    return SoundboardFederationCaller(
        kind="bot" if application_id is not None else "human",
        user=_federation_ref(user.id, user.origin_domain),
        application=(
            _federation_ref(application_id, application_domain)
            if application_id is not None and application_domain is not None
            else None
        ),
    )


def _new_federation_request(
    settings: Settings,
    guild: Guild,
    caller: SoundboardFederationCaller,
    operation: Literal["list", "get", "play"],
    *,
    sound: tuple[int, str] | None = None,
    source_guild: tuple[int, str] | None = None,
    channel: tuple[int, str] | None = None,
    sound_version: int | str | None = None,
    volume: float | None = None,
    actor_intent: dict[str, object] | None = None,
) -> SoundboardFederationRequest:
    issued_at = int(time.time())
    return SoundboardFederationRequest(
        guild=_federation_ref(guild.id, guild.origin_domain),
        caller=caller,
        requesting_instance=settings.domain,
        request_id=f"kasb_{secrets.token_urlsafe(24)}",
        issued_at=issued_at,
        deadline=issued_at + SOUNDBOARD_FEDERATION_DEADLINE_SECONDS,
        operation=operation,
        sound=(_federation_ref(*sound) if sound is not None else None),
        source_guild=(_federation_ref(*source_guild) if source_guild is not None else None),
        channel=(_federation_ref(*channel) if channel is not None else None),
        sound_version=(str(sound_version) if sound_version is not None else None),
        volume=volume,
        actor_intent=actor_intent,
    )


def _new_source_capability_request(
    settings: Settings,
    source_guild: tuple[int, str],
    target_guild: Guild,
    target_channel: Channel,
    caller: SoundboardFederationCaller,
    sound: tuple[int, str],
    *,
    sound_version: int | str,
    volume: float | None,
    target_installation_revision: int | None,
    actor_intent: dict[str, object],
) -> SoundboardSourceCapabilityRequest:
    issued_at = int(time.time())
    return SoundboardSourceCapabilityRequest(
        source_guild=_federation_ref(*source_guild),
        target_guild=_federation_ref(target_guild.id, target_guild.origin_domain),
        target_channel=_federation_ref(target_channel.id, target_channel.origin_domain),
        sound=_federation_ref(*sound),
        sound_version=str(sound_version),
        caller=caller,
        requesting_instance=settings.domain,
        request_id=f"kasc_{secrets.token_urlsafe(24)}",
        issued_at=issued_at,
        deadline=issued_at + SOUNDBOARD_FEDERATION_DEADLINE_SECONDS,
        volume=volume,
        target_installation_revision=(
            str(target_installation_revision) if target_installation_revision is not None else None
        ),
        actor_intent=actor_intent,
    )


def _soundboard_volume_binding(volume: float | None) -> str:
    return "default" if volume is None else float(volume).hex()


def _soundboard_actor_resources(
    *,
    source_guild: tuple[int, str] | None,
    target_guild: tuple[int, str],
    target_channel: tuple[int, str],
    sound: tuple[int, str],
    sound_version: int | str,
    volume: float | None,
    target_installation_revision: int | None,
) -> dict[str, str]:
    """Canonical exact bindings shared by A, T, and the sound authority S."""

    return {
        "operation": SOUNDBOARD_ACTOR_INTENT_ACTION,
        "sound_ref": f"{sound[0]}@{sound[1]}",
        "sound_version": str(sound_version),
        "source_guild_ref": (
            f"{source_guild[0]}@{source_guild[1]}" if source_guild is not None else "default"
        ),
        "target_channel_ref": f"{target_channel[0]}@{target_channel[1]}",
        "target_guild_ref": f"{target_guild[0]}@{target_guild[1]}",
        "target_installation_revision": (
            str(target_installation_revision)
            if target_installation_revision is not None
            else "human"
        ),
        "volume": _soundboard_volume_binding(volume),
    }


async def _validate_soundboard_actor_intent(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    caller: SoundboardFederationCaller,
    raw_intent: object,
    *,
    audience: str,
    runtime_target_domain: str,
    source_guild: tuple[int, str] | None,
    target_guild: tuple[int, str],
    target_channel: tuple[int, str],
    sound: tuple[int, str],
    sound_version: int | str,
    volume: float | None,
    target_installation_revision: int | None,
) -> None:
    """Validate the portable actor proof and current bot runtime revision."""

    actor_ref = (int(caller.user.id), caller.user.domain)
    if caller.kind == "human":
        resources = _soundboard_actor_resources(
            source_guild=source_guild,
            target_guild=target_guild,
            target_channel=target_channel,
            sound=sound,
            sound_version=sound_version,
            volume=volume,
            target_installation_revision=None,
        )
        await validate_human_actor_intent(
            session,
            settings,
            raw_intent,
            expected_action=SOUNDBOARD_ACTOR_INTENT_ACTION,
            expected_audience=audience,
            expected_actor_ref=actor_ref,
            expected_resources=resources,
            redis=redis,
        )
        return

    application = caller.application
    if application is None or target_installation_revision is None:
        raise ValueError("bot soundboard actor intent lacks installation lineage")
    resources = _soundboard_actor_resources(
        source_guild=source_guild,
        target_guild=target_guild,
        target_channel=target_channel,
        sound=sound,
        sound_version=sound_version,
        volume=volume,
        target_installation_revision=target_installation_revision,
    )
    await validate_worker_actor_intent(
        session,
        settings.domain,
        raw_intent,
        expected_action=SOUNDBOARD_ACTOR_INTENT_ACTION,
        expected_audience=audience,
        expected_application_ref=(int(application.id), application.domain),
        expected_actor_ref=actor_ref,
        expected_resources=resources,
        runtime_target_domain=runtime_target_domain,
        redis=redis,
    )


async def _build_human_soundboard_actor_intent(
    session: AsyncSession,
    settings: Settings,
    actor: User,
    *,
    audience: str,
    source_guild: tuple[int, str] | None,
    target_guild: tuple[int, str],
    target_channel: tuple[int, str],
    sound: tuple[int, str],
    sound_version: int | str,
    volume: float | None,
) -> dict[str, object]:
    return await build_human_actor_intent(
        session,
        settings,
        actor,
        action=SOUNDBOARD_ACTOR_INTENT_ACTION,
        audience=audience,
        resources=_soundboard_actor_resources(
            source_guild=source_guild,
            target_guild=target_guild,
            target_channel=target_channel,
            sound=sound,
            sound_version=sound_version,
            volume=volume,
            target_installation_revision=None,
        ),
    )


def _require_fresh_soundboard_request(
    payload: SoundboardFederationRequest,
    *,
    now: int,
    clock_skew_seconds: int,
) -> None:
    if payload.issued_at > now + clock_skew_seconds or payload.deadline <= now:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "KAED_FED_SOUNDBOARD_REQUEST_EXPIRED",
                "message": "The soundboard authorization request has expired.",
            },
        )


def _validate_federated_download_url(
    settings: Settings,
    url: str,
    *,
    media_origin: str,
) -> None:
    try:
        validate_media_url_origin(
            url,
            media_origin,
            allow_http=settings.environment != "production",
        )
    except ValueError as exc:
        raise ValueError("soundboard response contains an unsafe media capability") from exc


def _default_soundboard_catalog(
    settings: Settings,
) -> list[DefaultSoundboardSoundConfig]:
    try:
        catalog = [
            DefaultSoundboardSoundConfig.model_validate(item)
            for item in getattr(settings, "soundboard_default_sounds", [])
        ]
        ids = [item.sound_id for item in catalog]
        if len(ids) != len(set(ids)):
            raise ValueError("default sound ids must be unique")
        for item in catalog:
            _validate_federated_download_url(
                settings,
                item.download_url,
                media_origin=media_url_origin(
                    item.download_url,
                    allow_http=settings.environment != "production",
                ),
            )
            if item.media_hash not in item.download_url:
                raise ValueError("default sound URL must contain its immutable media digest")
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "DEFAULT_SOUNDBOARD_CATALOG_INVALID"},
        ) from exc
    return catalog


def _default_sound_payload(
    settings: Settings,
    sound: DefaultSoundboardSoundConfig,
) -> dict[str, object]:
    return {
        "id": sound.sound_id,
        "origin_domain": settings.domain,
        "guild_id": None,
        "guild_domain": None,
        "name": sound.name,
        "media_hash": sound.media_hash,
        "content_type": sound.content_type,
        "volume": sound.volume,
        "emoji_id": None,
        "emoji_domain": None,
        "emoji_name": sound.emoji_name,
        "available": True,
        "duration_ms": sound.duration_ms,
        "created_by_id": None,
        "created_by_domain": None,
        "version": "1",
    }


def _default_sound(
    settings: Settings,
    sound_id: int,
    sound_domain: str,
) -> DefaultSoundboardSoundConfig | None:
    if sound_domain != settings.domain:
        return None
    return next(
        (item for item in _default_soundboard_catalog(settings) if int(item.sound_id) == sound_id),
        None,
    )


def _validate_federation_page(
    page: SoundboardFederationPage,
    request: SoundboardFederationRequest,
) -> list[dict[str, object]]:
    if page.request.model_dump(mode="json") != request.model_dump(mode="json"):
        raise ValueError("soundboard response is bound to a different request")
    if request.operation not in {"list", "get"}:
        raise ValueError("soundboard query response has the wrong operation")
    seen: set[tuple[str, str]] = set()
    for sound in page.sounds:
        sound_ref = (sound.id, sound.origin_domain)
        if sound_ref in seen:
            raise ValueError("soundboard response contains a duplicate sound")
        seen.add(sound_ref)
        if (sound.guild_id, sound.guild_domain) != (
            request.guild.id,
            request.guild.domain,
        ) or sound.origin_domain != request.guild.domain:
            raise ValueError("soundboard response contains a sound from another guild")
    if request.operation == "get":
        if request.sound is None or len(page.sounds) != 1:
            raise ValueError("soundboard get response must contain exactly one sound")
        sound = page.sounds[0]
        if (sound.id, sound.origin_domain) != (request.sound.id, request.sound.domain):
            raise ValueError("soundboard get response contains a substituted sound")
    rendered = [sound.model_dump(mode="json") for sound in page.sounds]
    for item, sound in zip(rendered, page.sounds, strict=True):
        if sound.user is not None:
            item["user"] = client_user_payload_from_profile(sound.user)
    return rendered


def _validate_federation_play(
    play: SoundboardFederationPlay,
    request: SoundboardFederationRequest,
    settings: Settings,
) -> dict[str, object]:
    if play.request.model_dump(mode="json") != request.model_dump(mode="json"):
        raise ValueError("soundboard play response is bound to a different request")
    if request.operation != "play" or request.sound is None or request.channel is None:
        raise ValueError("soundboard play response has the wrong operation")
    if play.guild != request.guild or play.channel != request.channel:
        raise ValueError("soundboard play response has a substituted room")
    if play.user != request.caller.user:
        raise ValueError("soundboard play response has a substituted actor")
    sound = play.capability.sound
    if (sound.id, sound.origin_domain) != (request.sound.id, request.sound.domain):
        raise ValueError("soundboard play response has a substituted sound")
    if sound.version != request.sound_version:
        raise ValueError("soundboard play response has a substituted sound revision")
    if request.source_guild is not None and (
        sound.guild_id,
        sound.guild_domain,
    ) != (request.source_guild.id, request.source_guild.domain):
        raise ValueError("soundboard play response has a substituted source guild")
    if not sound.available:
        raise ValueError("soundboard play response contains an unavailable sound")
    if play.capability.media_authority != sound.origin_domain:
        raise ValueError("soundboard play response has a substituted media authority")
    _validate_federated_download_url(
        settings,
        play.capability.download_url,
        media_origin=play.capability.media_origin,
    )
    return play.capability.model_dump(mode="json")


async def _validated_soundboard_envelope(
    session: AsyncSession,
    settings: Settings,
    request: SoundboardFederationRequest,
    raw: object,
    *,
    event_type: str,
) -> dict[str, Any]:
    envelope = await validated_event_envelope(
        session,
        settings,
        request.guild.domain,
        raw,
        allow_authority_attested_actor=True,
    )
    if envelope.type != event_type:
        raise ValueError("soundboard response has the wrong signed event type")
    expected_context: dict[str, object] = {
        "guild_id": request.guild.id,
        "guild_domain": request.guild.domain,
    }
    if request.channel is not None:
        expected_context.update(
            {
                "channel_id": request.channel.id,
                "channel_domain": request.channel.domain,
            }
        )
    if envelope.context != expected_context:
        raise ValueError("soundboard response has the wrong signed context")
    guild = await session.get(Guild, (int(request.guild.id), request.guild.domain))
    if guild is None or (
        int(envelope.actor.id),
        envelope.actor.domain,
    ) != (guild.owner_id, guild.owner_domain):
        raise ValueError("soundboard response was not signed for the current guild owner")
    timestamp_floor = (request.issued_at - settings.federation_clock_skew_seconds) * 1_000
    timestamp_ceiling = (request.deadline + settings.federation_clock_skew_seconds) * 1_000
    if not timestamp_floor <= envelope.ts < timestamp_ceiling:
        raise ValueError("soundboard response was signed outside its request window")
    if int(time.time()) >= request.deadline:
        raise ValueError("soundboard response arrived after its request deadline")
    return envelope.content


def _remote_soundboard_error(upstream_status: int, *, operation: str) -> HTTPException:
    if upstream_status == 403:
        return HTTPException(
            status_code=403,
            detail={
                "code": "MISSING_PERMISSIONS",
                "message": "You do not have permission to use this guild's soundboard.",
            },
        )
    if upstream_status == 404:
        return HTTPException(
            status_code=404,
            detail={
                "code": ("SOUNDBOARD_SOUND_NOT_FOUND" if operation == "get" else "GUILD_NOT_FOUND")
            },
        )
    if upstream_status == 409:
        return HTTPException(
            status_code=409,
            detail={
                "code": "FEDERATED_SOUNDBOARD_CONFLICT",
                "message": "The soundboard request no longer matches current guild state.",
            },
        )
    if upstream_status == 429:
        return HTTPException(
            status_code=429,
            detail={
                "code": "SOUNDBOARD_RATE_LIMITED",
                "message": "The remote guild is receiving too many soundboard requests.",
            },
            headers={"Retry-After": "1"},
        )
    return HTTPException(
        status_code=503,
        detail={
            "code": "FEDERATED_SOUNDBOARD_UNAVAILABLE",
            "message": "The guild home could not serve its soundboard. Try again shortly.",
        },
    )


async def _request_remote_soundboard(
    session: AsyncSession,
    settings: Settings,
    request: SoundboardFederationRequest,
) -> SoundboardFederationPage | SoundboardFederationPlay:
    path = (
        f"/_kaede/v1/guilds/{request.guild.id}/soundboard/play"
        if request.operation == "play"
        else f"/_kaede/v1/guilds/{request.guild.id}/soundboard/query"
    )
    try:
        upstream = await signed_request(
            session,
            settings,
            "POST",
            request.guild.domain,
            path,
            payload=request.model_dump(mode="json"),
            request_timeout=SOUNDBOARD_FEDERATION_DEADLINE_SECONDS,
            max_response_bytes=SOUNDBOARD_FEDERATION_MAX_RESPONSE_BYTES,
        )
    except (FederationNetworkError, RuntimeError):
        raise _remote_soundboard_error(503, operation=request.operation) from None
    if upstream.status_code != 200:
        raise _remote_soundboard_error(upstream.status_code, operation=request.operation)
    try:
        raw = decode_federation_response_json(
            upstream,
            max_response_bytes=SOUNDBOARD_FEDERATION_MAX_RESPONSE_BYTES,
        )
        event_type = (
            SOUNDBOARD_FEDERATION_PLAY_EVENT
            if request.operation == "play"
            else SOUNDBOARD_FEDERATION_QUERY_EVENT
        )
        content = await _validated_soundboard_envelope(
            session,
            settings,
            request,
            raw,
            event_type=event_type,
        )
        if request.operation == "play":
            play = SoundboardFederationPlay.model_validate(content)
            _validate_federation_play(play, request, settings)
            return play
        page = SoundboardFederationPage.model_validate(content)
        _validate_federation_page(page, request)
        return page
    except (FederationNetworkError, ValidationError, TypeError, ValueError):
        raise HTTPException(
            status_code=502,
            detail={
                "code": "FEDERATED_SOUNDBOARD_RESPONSE_INVALID",
                "message": "The guild home returned an invalid signed soundboard response.",
            },
        ) from None


async def _validated_source_capability_envelope(
    session: AsyncSession,
    settings: Settings,
    request: SoundboardSourceCapabilityRequest,
    raw: object,
) -> SoundboardFederationCapability:
    envelope = await validated_event_envelope(
        session,
        settings,
        request.source_guild.domain,
        raw,
        allow_authority_attested_actor=True,
    )
    if envelope.type != SOUNDBOARD_SOURCE_CAPABILITY_EVENT:
        raise ValueError("soundboard source response has the wrong signed event type")
    if envelope.context != {
        "guild_id": request.source_guild.id,
        "guild_domain": request.source_guild.domain,
        "target_guild_id": request.target_guild.id,
        "target_guild_domain": request.target_guild.domain,
        "target_channel_id": request.target_channel.id,
        "target_channel_domain": request.target_channel.domain,
    }:
        raise ValueError("soundboard source response has the wrong signed context")
    timestamp_floor = (request.issued_at - settings.federation_clock_skew_seconds) * 1_000
    timestamp_ceiling = (request.deadline + settings.federation_clock_skew_seconds) * 1_000
    if not timestamp_floor <= envelope.ts < timestamp_ceiling:
        raise ValueError("soundboard source response was signed outside its request window")
    if int(time.time()) >= request.deadline:
        raise ValueError("soundboard source response arrived after its request deadline")
    proof = SoundboardSourceCapability.model_validate(envelope.content)
    if proof.request.model_dump(mode="json") != request.model_dump(mode="json"):
        raise ValueError("soundboard source response is bound to a different request")
    sound = proof.capability.sound
    if (
        (sound.id, sound.origin_domain) != (request.sound.id, request.sound.domain)
        or (sound.guild_id, sound.guild_domain)
        != (request.source_guild.id, request.source_guild.domain)
        or not sound.available
        or sound.version != request.sound_version
        or proof.capability.media_authority != request.source_guild.domain
    ):
        raise ValueError("soundboard source response substituted its sound binding")
    _validate_federated_download_url(
        settings,
        proof.capability.download_url,
        media_origin=proof.capability.media_origin,
    )
    return proof.capability


async def _request_remote_sound_capability(
    session: AsyncSession,
    settings: Settings,
    request: SoundboardSourceCapabilityRequest,
) -> dict[str, object]:
    try:
        upstream = await signed_request(
            session,
            settings,
            "POST",
            request.source_guild.domain,
            f"/_kaede/v1/guilds/{request.source_guild.id}/soundboard/capability",
            payload=request.model_dump(mode="json"),
            request_timeout=SOUNDBOARD_FEDERATION_DEADLINE_SECONDS,
            max_response_bytes=SOUNDBOARD_FEDERATION_MAX_RESPONSE_BYTES,
        )
    except (FederationNetworkError, RuntimeError):
        raise HTTPException(
            status_code=503,
            detail={"code": "FEDERATED_SOUNDBOARD_SOURCE_UNAVAILABLE"},
        ) from None
    if upstream.status_code == 403:
        raise HTTPException(
            status_code=403,
            detail={"code": "EXTERNAL_SOUNDBOARD_SOURCE_ACCESS_REQUIRED"},
        )
    if upstream.status_code == 404:
        raise HTTPException(status_code=404, detail={"code": "SOUNDBOARD_SOUND_NOT_FOUND"})
    if upstream.status_code != 200:
        raise HTTPException(
            status_code=503,
            detail={"code": "FEDERATED_SOUNDBOARD_SOURCE_UNAVAILABLE"},
        )
    try:
        raw = decode_federation_response_json(
            upstream,
            max_response_bytes=SOUNDBOARD_FEDERATION_MAX_RESPONSE_BYTES,
        )
        capability = await _validated_source_capability_envelope(
            session,
            settings,
            request,
            raw,
        )
    except (FederationNetworkError, ValidationError, TypeError, ValueError):
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_SOUNDBOARD_SOURCE_RESPONSE_INVALID"},
        ) from None
    return capability.model_dump(mode="json")


def _missing_permissions(required: Permission) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={
            "code": "MISSING_PERMISSIONS",
            "message": "You do not have permission to use the soundboard here.",
            "permissions": str(int(required)),
        },
    )


def _require_sound_content_type(content_type: str | None) -> str:
    if content_type not in SOUNDBOARD_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={
                "code": "SOUNDBOARD_AUDIO_TYPE_REQUIRED",
                "message": "Choose an MP3 or Ogg audio file for the soundboard.",
            },
        )
    return content_type


async def _local_locked_guild(session: AsyncSession, settings: Settings, guild: Guild) -> Guild:
    if guild.origin_domain != settings.domain:
        raise HTTPException(status_code=409, detail={"code": "GUILD_NOT_HOME"})
    locked = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == settings.domain)
        .with_for_update()
    )
    if locked is None or locked.unavailable:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return locked


async def _require_guild_permission(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    principal: BotPrincipal,
    required: Permission,
) -> None:
    permissions = await get_permissions(session, redis, guild, principal.user)
    if permissions & required != required:
        raise _missing_permissions(required)


async def _require_guild_membership(
    session: AsyncSession,
    guild: Guild,
    actor: User,
) -> None:
    member = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, actor.id, actor.origin_domain),
    )
    if member is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})


async def _creatable_human_guild(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild_ref: EntityRef,
    auth: AuthenticatedUser,
    *,
    for_update: bool = False,
) -> Guild:
    guild = await local_guild(session, settings, guild_ref, for_update=for_update)
    await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        required_permissions("guild.expression.create"),
    )
    return guild


async def _sound_for_guild(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    sound_ref: EntityRef,
    *,
    for_update: bool = False,
) -> SoundboardSound:
    sound_id, sound_domain = sound_ref.resolve(settings.domain)
    query = select(SoundboardSound).where(
        SoundboardSound.id == sound_id,
        SoundboardSound.origin_domain == sound_domain,
        SoundboardSound.guild_id == guild.id,
        SoundboardSound.guild_domain == guild.origin_domain,
    )
    if for_update:
        query = query.with_for_update()
    sound = await session.scalar(query)
    if sound is None:
        raise HTTPException(status_code=404, detail={"code": "SOUNDBOARD_SOUND_NOT_FOUND"})
    return sound


def _can_view_soundboard_creators(permissions: int) -> bool:
    return bool(
        Permission(permissions)
        & (
            Permission.ADMINISTRATOR
            | Permission.CREATE_GUILD_EXPRESSIONS
            | Permission.MANAGE_GUILD_EXPRESSIONS
        )
    )


async def _render_guild_sounds(
    session: AsyncSession,
    sounds: list[SoundboardSound],
    *,
    include_creators: bool,
    federated: bool = False,
) -> list[dict[str, object]]:
    creators: dict[tuple[int, str], User] = {}
    if include_creators:
        refs = {(item.created_by_id, item.created_by_domain) for item in sounds}
        if refs:
            creators = {
                (user.id, user.origin_domain): user
                for user in await session.scalars(
                    select(User).where(tuple_(User.id, User.origin_domain).in_(refs))
                )
            }
    rendered: list[dict[str, object]] = []
    for sound in sounds:
        item = soundboard_sound_payload(sound)
        if include_creators:
            creator = creators.get((sound.created_by_id, sound.created_by_domain))
            if creator is not None:
                item["user"] = profile_from_user(creator) if federated else user_payload(creator)
        else:
            item["created_by_id"] = None
            item["created_by_domain"] = None
        rendered.append(item)
    return rendered


async def _list_guild_sounds(
    session: AsyncSession,
    guild: Guild,
    *,
    include_creators: bool,
    federated: bool = False,
) -> list[dict[str, object]]:
    sounds = list(
        await session.scalars(
            select(SoundboardSound)
            .where(
                SoundboardSound.guild_id == guild.id,
                SoundboardSound.guild_domain == guild.origin_domain,
            )
            .order_by(SoundboardSound.name, SoundboardSound.id)
            .limit(SOUNDBOARD_MAX_SOUNDS)
        )
    )
    return await _render_guild_sounds(
        session,
        sounds,
        include_creators=include_creators,
        federated=federated,
    )


async def gateway_soundboard_sounds(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild: Guild,
    actor: User,
    *,
    application_id: int | None = None,
    application_domain: str | None = None,
) -> list[dict[str, object]]:
    """Render one authorized guild sound set for REST and Gateway callers."""

    if guild.origin_domain != settings.domain:
        request = _new_federation_request(
            settings,
            guild,
            _federation_caller(
                actor,
                application_id=application_id,
                application_domain=application_domain,
            ),
            "list",
        )
        page = await _request_remote_soundboard(session, settings, request)
        if not isinstance(page, SoundboardFederationPage):
            raise RuntimeError("soundboard query returned a play response")
        return _validate_federation_page(page, request)
    include_creators = _can_view_soundboard_creators(
        await get_permissions(session, redis, guild, actor)
    )
    return await _list_guild_sounds(
        session,
        guild,
        include_creators=include_creators,
    )


@human_router.get("/soundboard-default-sounds")
async def list_human_default_soundboard_sounds(
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    del auth
    return [
        _default_sound_payload(settings, sound) for sound in _default_soundboard_catalog(settings)
    ]


@router.get("/soundboard-default-sounds")
async def list_default_soundboard_sounds(
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    del principal
    return [
        _default_sound_payload(settings, sound) for sound in _default_soundboard_catalog(settings)
    ]


async def probe_sound_duration_ms(data: bytes, content_type: str) -> int:
    """Probe already-scanned local bytes without enabling network protocols."""

    suffix = ".mp3" if content_type == "audio/mpeg" else ".ogg"
    with tempfile.TemporaryDirectory(prefix="kaede-soundboard-") as directory:
        input_path = Path(directory) / f"sound{suffix}"
        input_path.write_bytes(data)
        try:
            process = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,crypto",
                "-probesize",
                str(SOUNDBOARD_MAX_BYTES),
                "-analyzeduration",
                "6000000",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(input_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=503, detail={"code": "SOUNDBOARD_PROCESSING_UNAVAILABLE"}
            ) from exc
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=8)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise HTTPException(
                status_code=400, detail={"code": "SOUNDBOARD_AUDIO_INVALID"}
            ) from None
    try:
        seconds = float(stdout[:128].strip())
    except ValueError:
        seconds = math.nan
    duration_ms = math.ceil(seconds * 1000) if math.isfinite(seconds) and seconds > 0 else 0
    if process.returncode != 0 or not 1 <= duration_ms <= SOUNDBOARD_MAX_DURATION_MS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SOUNDBOARD_DURATION_INVALID",
                "max_duration_ms": SOUNDBOARD_MAX_DURATION_MS,
            },
        )
    return duration_ms


async def _emoji_fields(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    emoji_id: int | None,
    emoji_name: str | None,
) -> tuple[int | None, str | None, str | None]:
    if emoji_id is None:
        return None, None, emoji_name
    emoji = await session.get(Emoji, (emoji_id, settings.domain))
    if emoji is None or (emoji.guild_id, emoji.guild_domain) != (guild.id, guild.origin_domain):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SOUNDBOARD_EMOJI_INVALID",
                "message": "The selected emoji does not belong to this guild.",
            },
        )
    return emoji.id, emoji.origin_domain, None


def _validate_soundboard_upload(payload: UploadTicketRequest) -> None:
    if payload.encryption_mode != "plaintext" or payload.encryption_protocol is not None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "SOUNDBOARD_PLAINTEXT_REQUIRED",
                "message": "Soundboard audio must be uploaded without message encryption.",
            },
        )
    _require_sound_content_type(payload.content_type)
    if payload.size > SOUNDBOARD_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "SOUNDBOARD_SOUND_TOO_LARGE",
                "message": "Soundboard audio can be at most 512 KiB.",
                "max_bytes": SOUNDBOARD_MAX_BYTES,
            },
        )


@human_router.post("/guilds/{guild_ref}/soundboard-sounds/tickets", status_code=201)
async def create_human_soundboard_ticket(
    guild_ref: EntityRef,
    payload: UploadTicketRequest,
    response: Response,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    _validate_soundboard_upload(payload)
    remote = await _proxy_human_management(
        session,
        settings,
        guild_ref,
        auth,
        "soundboard.ticket",
        {"data": payload.model_dump(mode="json")},
    )
    if remote is not None:
        response.status_code = remote.status_code
        return cast(dict[str, object], remote.body)
    await _creatable_human_guild(session, redis, settings, guild_ref, auth)
    await enforce_keyed_rate_limit(
        redis,
        response,
        SOUNDBOARD_UPLOAD_LIMIT,
        identity=f"human:{auth.user.origin_domain}:{auth.user.id}",
    )
    attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        auth.user,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
        purpose="soundboard",
        federated_guild_upload=is_federated_human_authority_upload(auth.user, settings),
    )
    await session.commit()
    return ticket_payload(attachment, upload_url)


@human_router.get("/guilds/{guild_ref}/soundboard-sounds")
async def list_human_soundboard_sounds(
    guild_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, list[dict[str, object]]]:
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    if guild_domain != settings.domain:
        guild = await session.get(Guild, (guild_id, guild_domain))
        member = await session.get(
            GuildMember,
            (guild_id, guild_domain, auth.user.id, auth.user.origin_domain),
        )
        if guild is None or guild.unavailable or member is None:
            raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
        return {
            "items": await gateway_soundboard_sounds(session, redis, settings, guild, auth.user)
        }
    guild = await local_guild(session, settings, guild_ref)
    await _require_guild_membership(session, guild, auth.user)
    return {"items": await gateway_soundboard_sounds(session, redis, settings, guild, auth.user)}


@human_router.get("/guilds/{guild_ref}/soundboard-sounds/{sound_ref}")
async def get_human_soundboard_sound(
    guild_ref: EntityRef,
    sound_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    if guild_domain != settings.domain:
        guild = await session.get(Guild, (guild_id, guild_domain))
        member = await session.get(
            GuildMember,
            (guild_id, guild_domain, auth.user.id, auth.user.origin_domain),
        )
        if guild is None or guild.unavailable or member is None:
            raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
        sound_id, sound_domain = sound_ref.resolve(guild.origin_domain)
        request = _new_federation_request(
            settings,
            guild,
            _federation_caller(auth.user),
            "get",
            sound=(sound_id, sound_domain),
        )
        page = await _request_remote_soundboard(session, settings, request)
        if not isinstance(page, SoundboardFederationPage):
            raise RuntimeError("soundboard query returned a play response")
        sounds = _validate_federation_page(page, request)
        return sounds[0]
    guild = await local_guild(session, settings, guild_ref)
    await _require_guild_membership(session, guild, auth.user)
    sound = await _sound_for_guild(session, settings, guild, sound_ref)
    include_creators = _can_view_soundboard_creators(
        await get_permissions(session, redis, guild, auth.user)
    )
    return (
        await _render_guild_sounds(
            session,
            [sound],
            include_creators=include_creators,
        )
    )[0]


@router.post("/guilds/{guild_ref}/soundboard-sounds/tickets", status_code=201)
async def create_soundboard_ticket(
    guild_ref: EntityRef,
    payload: UploadTicketRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    _validate_soundboard_upload(payload)
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "soundboard.manage"
    )
    require_installation_scope(principal, installation, "attachments.write")
    guild = await _local_locked_guild(session, settings, guild)
    await _require_guild_permission(
        session, redis, guild, principal, Permission.CREATE_GUILD_EXPRESSIONS
    )
    await enforce_keyed_rate_limit(
        redis,
        response,
        SOUNDBOARD_UPLOAD_LIMIT,
        identity=f"{installation.id}:{principal.user.origin_domain}:{principal.user.id}",
    )
    attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        principal.user,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
        purpose="soundboard",
        bot_installation=installation,
    )
    await session.commit()
    return ticket_payload(attachment, upload_url)


@router.get("/guilds/{guild_ref}/soundboard-sounds")
async def list_soundboard_sounds(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, list[dict[str, object]]]:
    guild, _ = await installation_for_guild(
        session, settings, principal, guild_ref, "soundboard.read"
    )
    return {
        "items": await gateway_soundboard_sounds(
            session,
            redis,
            settings,
            guild,
            principal.user,
            application_id=principal.application.id,
            application_domain=principal.application.origin_domain,
        )
    }


@router.get("/guilds/{guild_ref}/soundboard-sounds/{sound_ref}")
async def get_soundboard_sound(
    guild_ref: EntityRef,
    sound_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild, _ = await installation_for_guild(
        session, settings, principal, guild_ref, "soundboard.read"
    )
    if guild.origin_domain != settings.domain:
        sound_id, sound_domain = sound_ref.resolve(guild.origin_domain)
        request = _new_federation_request(
            settings,
            guild,
            _federation_caller(
                principal.user,
                application_id=principal.application.id,
                application_domain=principal.application.origin_domain,
            ),
            "get",
            sound=(sound_id, sound_domain),
        )
        page = await _request_remote_soundboard(session, settings, request)
        if not isinstance(page, SoundboardFederationPage):
            raise RuntimeError("soundboard query returned a play response")
        sounds = _validate_federation_page(page, request)
        return sounds[0]
    sound = await _sound_for_guild(session, settings, guild, sound_ref)
    include_creators = _can_view_soundboard_creators(
        await get_permissions(session, redis, guild, principal.user)
    )
    return (
        await _render_guild_sounds(
            session,
            [sound],
            include_creators=include_creators,
        )
    )[0]


async def _authorize_federated_soundboard_request(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    principal: FederationPrincipal,
    guild_id: int,
    payload: SoundboardFederationRequest,
    *,
    scopes: dict[str, str],
) -> tuple[Guild, User, BotInstallation | None]:
    require_guild_federation_access(principal)
    if (
        payload.requesting_instance != principal.origin
        or payload.caller.user.domain != principal.origin
        or (
            payload.caller.application is not None
            and payload.caller.application.domain != principal.origin
        )
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "KAED_FED_SOUNDBOARD_CALLER_MISMATCH"},
        )
    if (
        int(payload.guild.id) != guild_id
        or payload.guild.domain != settings.domain
        or payload.operation not in scopes
    ):
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    _require_fresh_soundboard_request(
        payload,
        now=int(time.time()),
        clock_skew_seconds=settings.federation_clock_skew_seconds,
    )
    if (
        payload.operation == "play"
        and payload.caller.kind == "human"
        and payload.sound is not None
        and payload.sound.domain == settings.domain
    ):
        if (
            payload.sound is None
            or payload.channel is None
            or payload.sound_version is None
            or payload.actor_intent is None
        ):
            raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})
        try:
            await _validate_soundboard_actor_intent(
                session,
                redis,
                settings,
                payload.caller,
                payload.actor_intent,
                audience=settings.domain,
                runtime_target_domain=settings.domain,
                source_guild=(
                    (int(payload.source_guild.id), payload.source_guild.domain)
                    if payload.source_guild is not None
                    else None
                ),
                target_guild=(int(payload.guild.id), payload.guild.domain),
                target_channel=(int(payload.channel.id), payload.channel.domain),
                sound=(int(payload.sound.id), payload.sound.domain),
                sound_version=payload.sound_version,
                volume=payload.volume,
                target_installation_revision=None,
            )
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=403,
                detail={"code": "KAED_FED_SOUNDBOARD_ACTOR_INTENT_INVALID"},
            ) from None
    accepted = await redis.set(
        f"federation:soundboard-request:{principal.origin}:{payload.request_id}",
        "1",
        ex=settings.federation_clock_skew_seconds + SOUNDBOARD_FEDERATION_DEADLINE_SECONDS,
        nx=True,
    )
    if not accepted:
        raise HTTPException(
            status_code=409,
            detail={"code": "KAED_FED_SOUNDBOARD_REQUEST_REPLAYED"},
        )
    guild = await session.get(Guild, (guild_id, settings.domain))
    actor = await session.get(
        User,
        (int(payload.caller.user.id), payload.caller.user.domain),
    )
    if guild is None or guild.unavailable or actor is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    if (payload.caller.kind == "bot") != (actor.account_type == "bot"):
        raise HTTPException(
            status_code=403,
            detail={"code": "KAED_FED_SOUNDBOARD_CALLER_KIND_MISMATCH"},
        )
    if payload.caller.kind == "human":
        await _require_guild_membership(session, guild, actor)
        if payload.operation == "play":
            await require_remote_user_creation_allowed(session, actor)
        return guild, actor, None
    application = payload.caller.application
    if application is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    installation = await session.scalar(
        select(BotInstallation).where(
            BotInstallation.application_id == int(application.id),
            BotInstallation.application_domain == application.domain,
            BotInstallation.guild_id == guild.id,
            BotInstallation.guild_domain == guild.origin_domain,
            BotInstallation.bot_user_id == actor.id,
            BotInstallation.bot_user_domain == actor.origin_domain,
            usable_guild_installation(),
        )
    )
    required_scope = scopes[payload.operation]
    if installation is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    if required_scope not in (installation.granted_scopes or []):
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_SCOPE_REQUIRED", "scope": required_scope},
        )
    if (
        payload.operation == "play"
        and payload.sound is not None
        and payload.sound.domain == settings.domain
    ):
        if (
            payload.sound is None
            or payload.channel is None
            or payload.sound_version is None
            or payload.actor_intent is None
        ):
            raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})
        try:
            await _validate_soundboard_actor_intent(
                session,
                redis,
                settings,
                payload.caller,
                payload.actor_intent,
                audience=settings.domain,
                runtime_target_domain=settings.domain,
                source_guild=(
                    (int(payload.source_guild.id), payload.source_guild.domain)
                    if payload.source_guild is not None
                    else None
                ),
                target_guild=(int(payload.guild.id), payload.guild.domain),
                target_channel=(int(payload.channel.id), payload.channel.domain),
                sound=(int(payload.sound.id), payload.sound.domain),
                sound_version=payload.sound_version,
                volume=payload.volume,
                target_installation_revision=installation.grant_revision,
            )
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=403,
                detail={"code": "KAED_FED_SOUNDBOARD_ACTOR_INTENT_INVALID"},
            ) from None
    return guild, actor, installation


async def _soundboard_federation_signer(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
) -> User:
    try:
        return await guild_authority_owner(session, settings, guild)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "KAED_FED_SOUNDBOARD_SIGNER_UNAVAILABLE"},
        ) from exc


@federation_router.post("/_kaede/v1/guilds/{guild_id}/soundboard/query")
async def federation_soundboard_query(
    guild_id: Snowflake,
    payload: SoundboardFederationRequest,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "guild-soundboard-query",
        capacity=120,
        refill_per_minute=120,
    )
    guild, actor, _ = await _authorize_federated_soundboard_request(
        session,
        redis,
        settings,
        principal,
        guild_id,
        payload,
        scopes={"list": "soundboard.read", "get": "soundboard.read"},
    )
    include_creators = _can_view_soundboard_creators(
        await get_permissions(session, redis, guild, actor)
    )
    if payload.operation == "list":
        sounds = await _list_guild_sounds(
            session,
            guild,
            include_creators=include_creators,
            federated=True,
        )
    else:
        if payload.sound is None:
            raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})
        sound = await _sound_for_guild(
            session,
            settings,
            guild,
            EntityRef(f"{payload.sound.id}@{payload.sound.domain}"),
        )
        sounds = await _render_guild_sounds(
            session,
            [sound],
            include_creators=include_creators,
            federated=True,
        )
    page = SoundboardFederationPage.model_validate({"request": payload, "sounds": sounds})
    signer = await _soundboard_federation_signer(session, settings, guild)
    return await build_guild_authority_envelope(
        session,
        settings,
        guild,
        SOUNDBOARD_FEDERATION_QUERY_EVENT,
        signer,
        page.model_dump(mode="json"),
        context={"guild_id": str(guild.id), "guild_domain": guild.origin_domain},
    )


@federation_router.post("/_kaede/v1/guilds/{guild_id}/soundboard/capability")
async def federation_soundboard_source_capability(
    guild_id: Snowflake,
    payload: SoundboardSourceCapabilityRequest,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Mint media only after the source authority rechecks the caller's entitlement."""

    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "guild-soundboard-source-capability",
        capacity=120,
        refill_per_minute=120,
    )
    if (
        payload.requesting_instance != principal.origin
        or payload.target_guild.domain != principal.origin
        or payload.target_channel.domain != principal.origin
        or int(payload.source_guild.id) != guild_id
        or payload.source_guild.domain != settings.domain
        or payload.sound.domain != settings.domain
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "KAED_FED_SOUNDBOARD_SOURCE_CALLER_MISMATCH"},
        )
    try:
        await _validate_soundboard_actor_intent(
            session,
            redis,
            settings,
            payload.caller,
            payload.actor_intent,
            audience=settings.domain,
            runtime_target_domain=payload.target_guild.domain,
            source_guild=(int(payload.source_guild.id), payload.source_guild.domain),
            target_guild=(int(payload.target_guild.id), payload.target_guild.domain),
            target_channel=(
                int(payload.target_channel.id),
                payload.target_channel.domain,
            ),
            sound=(int(payload.sound.id), payload.sound.domain),
            sound_version=payload.sound_version,
            volume=payload.volume,
            target_installation_revision=(
                int(payload.target_installation_revision)
                if payload.target_installation_revision is not None
                else None
            ),
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=403,
            detail={"code": "KAED_FED_SOUNDBOARD_ACTOR_INTENT_INVALID"},
        ) from None
    now = int(time.time())
    if payload.issued_at > now + settings.federation_clock_skew_seconds or payload.deadline <= now:
        raise HTTPException(
            status_code=401,
            detail={"code": "KAED_FED_SOUNDBOARD_REQUEST_EXPIRED"},
        )
    accepted = await redis.set(
        f"federation:soundboard-source:{principal.origin}:{payload.request_id}",
        "1",
        ex=max(
            1,
            payload.deadline - now + settings.federation_clock_skew_seconds,
        ),
        nx=True,
    )
    if not accepted:
        raise HTTPException(
            status_code=409,
            detail={"code": "KAED_FED_SOUNDBOARD_REQUEST_REPLAYED"},
        )
    guild = await session.get(Guild, (guild_id, settings.domain))
    actor = await session.get(
        User,
        (int(payload.caller.user.id), payload.caller.user.domain),
    )
    if guild is None or guild.unavailable or actor is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    if (payload.caller.kind == "bot") != (actor.account_type == "bot"):
        raise HTTPException(
            status_code=403,
            detail={"code": "KAED_FED_SOUNDBOARD_CALLER_KIND_MISMATCH"},
        )
    await _require_source_sound_entitlement(
        session,
        (guild.id, guild.origin_domain),
        payload.caller,
    )
    if payload.caller.kind == "human":
        await require_remote_user_creation_allowed(session, actor)
    sound = await session.scalar(
        select(SoundboardSound).where(
            SoundboardSound.id == int(payload.sound.id),
            SoundboardSound.origin_domain == settings.domain,
            SoundboardSound.guild_id == guild.id,
            SoundboardSound.guild_domain == guild.origin_domain,
            SoundboardSound.available.is_(True),
            SoundboardSound.version == int(payload.sound_version),
        )
    )
    if sound is None:
        raise HTTPException(status_code=404, detail={"code": "SOUNDBOARD_SOUND_NOT_FOUND"})
    capability = SoundboardFederationCapability.model_validate(
        await _local_soundboard_media_capability(
            session,
            settings,
            sound,
            requested_volume=payload.volume,
            expected_version=int(payload.sound_version),
        )
    )
    proof = SoundboardSourceCapability(request=payload, capability=capability)
    signer = await _soundboard_federation_signer(session, settings, guild)
    return await build_guild_authority_envelope(
        session,
        settings,
        guild,
        SOUNDBOARD_SOURCE_CAPABILITY_EVENT,
        signer,
        proof.model_dump(mode="json"),
        context={
            "guild_id": payload.source_guild.id,
            "guild_domain": payload.source_guild.domain,
            "target_guild_id": payload.target_guild.id,
            "target_guild_domain": payload.target_guild.domain,
            "target_channel_id": payload.target_channel.id,
            "target_channel_domain": payload.target_channel.domain,
        },
    )


async def _queue_soundboard_mutations(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    actor: User,
    operation: Literal["create", "update", "delete"],
    rendered: dict[str, object],
) -> None:
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        f"guild.soundboard.sound.{operation}",
        {"sound": rendered},
    )
    sounds = list(
        await session.scalars(
            select(SoundboardSound)
            .where(
                SoundboardSound.guild_id == guild.id,
                SoundboardSound.guild_domain == guild.origin_domain,
            )
            .order_by(SoundboardSound.origin_domain, SoundboardSound.id)
        )
    )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.soundboard.sounds.update",
        {
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
            "soundboard_sounds": [soundboard_sound_payload(item) for item in sounds],
        },
    )


async def _commit_soundboard_sound(
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    guild: Guild,
    actor: User,
    payload: SoundboardSoundCreate,
    response: Response,
    *,
    reason: str | None,
) -> dict[str, object]:
    reason = normalize_audit_reason(reason)
    attachment = await finalize_attachment(
        session,
        settings,
        actor,
        int(payload.attachment_id),
        required_purpose="soundboard",
        federated_guild_upload=is_federated_human_authority_upload(actor, settings),
    )
    if attachment.size > SOUNDBOARD_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "SOUNDBOARD_SOUND_TOO_LARGE",
                "message": "Soundboard audio can be at most 512 KiB.",
                "max_bytes": SOUNDBOARD_MAX_BYTES,
            },
        )
    if attachment.scan_status != "clean":
        await session.commit()
        await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
        response.status_code = status.HTTP_202_ACCEPTED
        return attachment_payload(attachment)
    content_type = _require_sound_content_type(attachment.detected_content_type)
    if not attachment.content_sha256 or not attachment.object_key:
        raise HTTPException(status_code=409, detail={"code": "MEDIA_NOT_AVAILABLE"})
    existing = list(
        await session.scalars(
            select(SoundboardSound).where(
                SoundboardSound.guild_id == guild.id,
                SoundboardSound.guild_domain == guild.origin_domain,
            )
        )
    )
    if len(existing) >= SOUNDBOARD_MAX_SOUNDS:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SOUNDBOARD_LIMIT_REACHED",
                "message": f"A guild can have at most {SOUNDBOARD_MAX_SOUNDS} sounds.",
            },
        )
    if any(sound.name.casefold() == payload.name.casefold() for sound in existing):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SOUNDBOARD_NAME_TAKEN",
                "message": "That soundboard name is already in use.",
            },
        )
    try:
        data = await S3Storage(settings).get(
            settings.media_attachments_bucket,
            attachment.object_key,
            max_bytes=SOUNDBOARD_MAX_BYTES,
        )
    except StorageError as exc:
        raise HTTPException(status_code=503, detail={"code": "MEDIA_STORAGE_UNAVAILABLE"}) from exc
    if len(data) != attachment.size:
        raise HTTPException(status_code=409, detail={"code": "MEDIA_NOT_AVAILABLE"})
    duration_ms = await probe_sound_duration_ms(data, content_type)
    emoji_id, emoji_domain, emoji_name = await _emoji_fields(
        session,
        settings,
        guild,
        int(payload.emoji_id) if payload.emoji_id is not None else None,
        payload.emoji_name,
    )
    sound = SoundboardSound(
        id=await snowflake.mint(),
        origin_domain=settings.domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name=payload.name,
        media_hash=attachment.content_sha256,
        object_key=attachment.object_key,
        content_type=content_type,
        volume=payload.volume,
        emoji_id=emoji_id,
        emoji_domain=emoji_domain,
        emoji_name=emoji_name,
        available=True,
        duration_ms=duration_ms,
        created_by_id=actor.id,
        created_by_domain=actor.origin_domain,
        version=1,
    )
    await bind_asset(session, attachment, f"soundboard:{sound.origin_domain}:{sound.id}")
    session.add(sound)
    await session.flush()
    rendered = soundboard_sound_payload(sound)
    await add_audit_entry(
        session,
        snowflake,
        guild,
        actor,
        130,
        target_type="soundboard_sound",
        target_ref={
            "id": str(sound.id),
            "origin_domain": sound.origin_domain,
            "name": sound.name,
        },
        reason=reason,
        changes=[
            {"key": "name", "new_value": sound.name},
            {"key": "volume", "new_value": sound.volume},
            {"key": "emoji_id", "new_value": sound.emoji_id},
            {"key": "emoji_name", "new_value": sound.emoji_name},
        ],
    )
    await _queue_soundboard_mutations(
        session,
        settings,
        guild,
        actor,
        "create",
        rendered,
    )
    await session.commit()
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_SOUNDBOARD_SOUND_CREATE",
        rendered,
    )
    await publish_guild_soundboard_sounds_update(session, redis, guild)
    await wake_queued_guild_federation(guild)
    return rendered


@human_router.post("/guilds/{guild_ref}/soundboard-sounds", status_code=201)
async def create_human_soundboard_sound(
    guild_ref: EntityRef,
    payload: SoundboardSoundCreate,
    response: Response,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> dict[str, object]:
    remote = await _proxy_human_management(
        session,
        settings,
        guild_ref,
        auth,
        "soundboard.create",
        {
            "data": payload.model_dump(mode="json"),
            "reason": reason,
        },
    )
    if remote is not None:
        response.status_code = remote.status_code
        return cast(dict[str, object], remote.body)
    guild = await _creatable_human_guild(session, redis, settings, guild_ref, auth, for_update=True)
    return await _commit_soundboard_sound(
        session,
        redis,
        snowflake,
        settings,
        guild,
        auth.user,
        payload,
        response,
        reason=reason,
    )


@router.post("/guilds/{guild_ref}/soundboard-sounds", status_code=201)
async def create_soundboard_sound(
    guild_ref: EntityRef,
    payload: SoundboardSoundCreate,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> dict[str, object]:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "soundboard.manage"
    )
    guild = await _local_locked_guild(session, settings, guild)
    await _require_guild_permission(
        session, redis, guild, principal, Permission.CREATE_GUILD_EXPRESSIONS
    )
    await require_owned_attachments_for_installation(
        session, settings, principal, installation, [int(payload.attachment_id)]
    )
    return await _commit_soundboard_sound(
        session,
        redis,
        snowflake,
        settings,
        guild,
        principal.user,
        payload,
        response,
        reason=reason,
    )


async def _update_soundboard_sound(
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    guild: Guild,
    actor: User,
    sound_ref: EntityRef,
    payload: SoundboardSoundUpdate,
    *,
    reason: str | None,
) -> dict[str, object]:
    reason = normalize_audit_reason(reason)
    sound = await _sound_for_guild(session, settings, guild, sound_ref, for_update=True)
    await require_can_manage_expression(
        session,
        redis,
        guild,
        actor,
        creator_id=sound.created_by_id,
        creator_domain=sound.created_by_domain,
    )
    changes: list[dict[str, object]] = []
    if "name" in payload.model_fields_set:
        if payload.name is None:
            raise RuntimeError("validated soundboard name was null")
        existing = list(
            await session.scalars(
                select(SoundboardSound).where(
                    SoundboardSound.guild_id == guild.id,
                    SoundboardSound.guild_domain == guild.origin_domain,
                    SoundboardSound.id != sound.id,
                )
            )
        )
        if any(item.name.casefold() == payload.name.casefold() for item in existing):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "SOUNDBOARD_NAME_TAKEN",
                    "message": "That soundboard name is already in use.",
                },
            )
        if payload.name != sound.name:
            changes.append({"key": "name", "old_value": sound.name, "new_value": payload.name})
            sound.name = payload.name
    if "volume" in payload.model_fields_set:
        if payload.volume is None:
            raise RuntimeError("validated soundboard volume was null")
        if payload.volume != sound.volume:
            changes.append(
                {"key": "volume", "old_value": sound.volume, "new_value": payload.volume}
            )
            sound.volume = payload.volume
    if "emoji_id" in payload.model_fields_set or "emoji_name" in payload.model_fields_set:
        old_emoji_id = sound.emoji_id
        old_emoji_name = sound.emoji_name
        next_emoji_id, next_emoji_domain, next_emoji_name = await _emoji_fields(
            session,
            settings,
            guild,
            int(payload.emoji_id) if payload.emoji_id is not None else None,
            payload.emoji_name,
        )
        if (next_emoji_id, next_emoji_name) != (old_emoji_id, old_emoji_name):
            changes.extend(
                [
                    {
                        "key": "emoji_id",
                        "old_value": old_emoji_id,
                        "new_value": next_emoji_id,
                    },
                    {
                        "key": "emoji_name",
                        "old_value": old_emoji_name,
                        "new_value": next_emoji_name,
                    },
                ]
            )
        sound.emoji_id = next_emoji_id
        sound.emoji_domain = next_emoji_domain
        sound.emoji_name = next_emoji_name
    sound.version += 1
    rendered = soundboard_sound_payload(sound)
    await add_audit_entry(
        session,
        snowflake,
        guild,
        actor,
        131,
        target_type="soundboard_sound",
        target_ref={
            "id": str(sound.id),
            "origin_domain": sound.origin_domain,
            "name": sound.name,
        },
        reason=reason,
        changes=changes or [{"key": "configuration", "new_value": "unchanged"}],
    )
    await _queue_soundboard_mutations(
        session,
        settings,
        guild,
        actor,
        "update",
        rendered,
    )
    await session.commit()
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_SOUNDBOARD_SOUND_UPDATE",
        rendered,
    )
    await publish_guild_soundboard_sounds_update(session, redis, guild)
    await wake_queued_guild_federation(guild)
    return rendered


@human_router.patch("/guilds/{guild_ref}/soundboard-sounds/{sound_ref}")
async def update_human_soundboard_sound(
    guild_ref: EntityRef,
    sound_ref: EntityRef,
    payload: SoundboardSoundUpdate,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> dict[str, object]:
    remote = await _proxy_human_management(
        session,
        settings,
        guild_ref,
        auth,
        "soundboard.update",
        {
            "resource_ref": str(sound_ref),
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "reason": reason,
        },
    )
    if remote is not None:
        return cast(dict[str, object], remote.body)
    guild = await local_guild(session, settings, guild_ref, for_update=True)
    return await _update_soundboard_sound(
        session,
        redis,
        snowflake,
        settings,
        guild,
        auth.user,
        sound_ref,
        payload,
        reason=reason,
    )


@router.patch("/guilds/{guild_ref}/soundboard-sounds/{sound_ref}")
async def update_soundboard_sound(
    guild_ref: EntityRef,
    sound_ref: EntityRef,
    payload: SoundboardSoundUpdate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> dict[str, object]:
    guild, _ = await installation_for_guild(
        session, settings, principal, guild_ref, "soundboard.manage"
    )
    guild = await _local_locked_guild(session, settings, guild)
    return await _update_soundboard_sound(
        session,
        redis,
        snowflake,
        settings,
        guild,
        principal.user,
        sound_ref,
        payload,
        reason=reason,
    )


async def _delete_soundboard_sound(
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    guild: Guild,
    actor: User,
    sound_ref: EntityRef,
    *,
    reason: str | None,
) -> Response:
    reason = normalize_audit_reason(reason)
    sound = await _sound_for_guild(session, settings, guild, sound_ref, for_update=True)
    await require_can_manage_expression(
        session,
        redis,
        guild,
        actor,
        creator_id=sound.created_by_id,
        creator_domain=sound.created_by_domain,
    )
    rendered = soundboard_sound_payload(sound)
    attachment = await session.scalar(
        select(Attachment)
        .where(Attachment.asset_binding == f"soundboard:{sound.origin_domain}:{sound.id}")
        .with_for_update()
    )
    await session.delete(sound)
    if attachment is not None:
        attachment.asset_binding = None
    await add_audit_entry(
        session,
        snowflake,
        guild,
        actor,
        132,
        target_type="soundboard_sound",
        target_ref={
            "id": str(sound.id),
            "origin_domain": sound.origin_domain,
            "name": sound.name,
        },
        reason=reason,
        changes=[{"key": "name", "old_value": sound.name}],
    )
    await _queue_soundboard_mutations(
        session,
        settings,
        guild,
        actor,
        "delete",
        rendered,
    )
    await session.commit()
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_SOUNDBOARD_SOUND_DELETE",
        rendered,
    )
    await publish_guild_soundboard_sounds_update(session, redis, guild)
    await wake_queued_guild_federation(guild)
    if attachment is not None:
        await enqueue_best_effort(media_local_purge, attachment.id, attachment.origin_domain)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@human_router.delete(
    "/guilds/{guild_ref}/soundboard-sounds/{sound_ref}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_human_soundboard_sound(
    guild_ref: EntityRef,
    sound_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    remote = await _proxy_human_management(
        session,
        settings,
        guild_ref,
        auth,
        "soundboard.delete",
        {"resource_ref": str(sound_ref), "reason": reason},
    )
    if remote is not None:
        return Response(status_code=remote.status_code)
    guild = await local_guild(session, settings, guild_ref, for_update=True)
    return await _delete_soundboard_sound(
        session,
        redis,
        snowflake,
        settings,
        guild,
        auth.user,
        sound_ref,
        reason=reason,
    )


@router.delete(
    "/guilds/{guild_ref}/soundboard-sounds/{sound_ref}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_soundboard_sound(
    guild_ref: EntityRef,
    sound_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    guild, _ = await installation_for_guild(
        session, settings, principal, guild_ref, "soundboard.manage"
    )
    guild = await _local_locked_guild(session, settings, guild)
    return await _delete_soundboard_sound(
        session,
        redis,
        snowflake,
        settings,
        guild,
        principal.user,
        sound_ref,
        reason=reason,
    )


async def _soundboard_play_capability(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    channel: Channel,
    guild: Guild,
    actor: User,
    payload: SoundboardPlayRequest,
    response: Response,
    *,
    caller: SoundboardFederationCaller,
    target_installation_revision: int | None = None,
) -> dict[str, object]:
    sound, default_sound, source_guild_ref = await _resolve_play_sound(
        session,
        settings,
        payload,
    )
    sound_id, sound_domain = payload.sound_id.resolve(settings.domain)
    external = source_guild_ref is not None and source_guild_ref != (
        guild.id,
        guild.origin_domain,
    )
    required = _soundboard_play_permissions(external=external)
    permissions = await get_permissions(session, redis, guild, actor, channel=channel)
    if permissions & required != required:
        raise _missing_permissions(required)
    identity = await _require_soundboard_speaker(
        redis,
        settings,
        guild,
        channel,
        actor,
        caller,
    )
    await enforce_keyed_rate_limit(
        redis,
        response,
        SOUNDBOARD_PLAY_LIMIT,
        identity=f"{guild.origin_domain}:{guild.id}:{identity}",
    )
    if default_sound is not None:
        return {
            "sound": _default_sound_payload(settings, default_sound),
            "download_url": default_sound.download_url,
            "media_authority": settings.domain,
            "media_origin": media_url_origin(
                default_sound.download_url,
                allow_http=settings.environment != "production",
            ),
            "effective_volume": default_sound.volume
            * (payload.volume if payload.volume is not None else 1),
            "expires_in": 60,
        }
    if source_guild_ref is None:
        raise RuntimeError("soundboard source resolution was lost")
    if source_guild_ref[1] != settings.domain:
        if payload.actor_intent is None:
            raise HTTPException(
                status_code=403,
                detail={"code": "SOUNDBOARD_ACTOR_INTENT_REQUIRED"},
            )
        request = _new_source_capability_request(
            settings,
            source_guild_ref,
            guild,
            channel,
            caller,
            (sound_id, sound_domain),
            sound_version=payload.sound_version,
            volume=payload.volume,
            target_installation_revision=target_installation_revision,
            actor_intent=payload.actor_intent,
        )
        return await _request_remote_sound_capability(session, settings, request)
    if sound is None or (sound.guild_id, sound.guild_domain) != source_guild_ref:
        raise HTTPException(status_code=404, detail={"code": "SOUNDBOARD_SOUND_NOT_FOUND"})
    if external:
        await _require_source_sound_entitlement(session, source_guild_ref, caller)
    return await _local_soundboard_media_capability(
        session,
        settings,
        sound,
        requested_volume=payload.volume,
        expected_version=int(payload.sound_version),
    )


async def _resolve_play_sound(
    session: AsyncSession,
    settings: Settings,
    payload: SoundboardPlayRequest,
) -> tuple[
    SoundboardSound | None,
    DefaultSoundboardSoundConfig | None,
    tuple[int, str] | None,
]:
    """Resolve one requested sound without mixing lookup and authorization."""

    sound_id, sound_domain = payload.sound_id.resolve(settings.domain)
    requested_source = (
        payload.source_guild_id.resolve(settings.domain)
        if payload.source_guild_id is not None
        else None
    )
    sound = await session.scalar(
        select(SoundboardSound).where(
            SoundboardSound.id == sound_id,
            SoundboardSound.origin_domain == sound_domain,
            SoundboardSound.available.is_(True),
        )
    )
    if sound is not None:
        if sound.version != int(payload.sound_version):
            raise HTTPException(
                status_code=409,
                detail={"code": "SOUNDBOARD_SOUND_REVISION_MISMATCH"},
            )
        projected_source = (sound.guild_id, sound.guild_domain)
        if requested_source is not None and requested_source != projected_source:
            raise HTTPException(
                status_code=409,
                detail={"code": "SOUNDBOARD_SOURCE_GUILD_MISMATCH"},
            )
        requested_source = projected_source
    default_sound = (
        _default_sound(settings, sound_id, sound_domain)
        if requested_source is None and sound is None
        else None
    )
    if default_sound is not None and payload.sound_version != "1":
        raise HTTPException(
            status_code=409,
            detail={"code": "SOUNDBOARD_SOUND_REVISION_MISMATCH"},
        )
    if requested_source is None and default_sound is None:
        raise HTTPException(status_code=404, detail={"code": "SOUNDBOARD_SOUND_NOT_FOUND"})
    return sound, default_sound, requested_source


def _soundboard_play_permissions(*, external: bool) -> Permission:
    required = (
        Permission.VIEW_CHANNEL | Permission.CONNECT | Permission.SPEAK | Permission.USE_SOUNDBOARD
    )
    if external:
        required |= Permission.USE_EXTERNAL_SOUNDS
    return required


async def _require_soundboard_speaker(
    redis: Redis,
    settings: Settings,
    guild: Guild,
    channel: Channel,
    actor: User,
    caller: SoundboardFederationCaller,
) -> str:
    """Return the live participant identity after enforcing speak state."""

    identity = participant_identity(actor.id, actor.origin_domain)
    room = guild_room_name(guild.id, channel.id)
    occupant = next(
        (
            item
            for item in await room_occupants(redis, settings.domain, room)
            if item.identity == identity
        ),
        None,
    )
    if occupant is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": ("BOT_NOT_IN_VOICE" if caller.kind == "bot" else "USER_NOT_IN_VOICE"),
                "message": "Join this voice channel before playing a sound.",
            },
        )
    if (
        not occupant.can_speak
        or not occupant.allow_speak
        or occupant.self_mute
        or occupant.self_deaf
        or occupant.server_mute
        or occupant.server_deaf
        or occupant.suppressed
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": (
                    "BOT_VOICE_SPEAK_REQUIRED" if caller.kind == "bot" else "VOICE_SPEAK_REQUIRED"
                ),
                "message": "You need permission to speak and must not be server muted.",
            },
        )
    return identity


async def _require_source_sound_entitlement(
    session: AsyncSession,
    source_guild_ref: tuple[int, str],
    caller: SoundboardFederationCaller,
) -> None:
    membership = await session.get(
        GuildMember,
        (
            source_guild_ref[0],
            source_guild_ref[1],
            int(caller.user.id),
            caller.user.domain,
        ),
    )
    if membership is None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "EXTERNAL_SOUNDBOARD_SOURCE_ACCESS_REQUIRED",
                "message": "Join the sound's guild before using it in another guild.",
            },
        )
    if caller.kind != "bot":
        return
    application = caller.application
    if application is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    installation = await session.scalar(
        select(BotInstallation).where(
            BotInstallation.application_id == int(application.id),
            BotInstallation.application_domain == application.domain,
            BotInstallation.guild_id == source_guild_ref[0],
            BotInstallation.guild_domain == source_guild_ref[1],
            BotInstallation.bot_user_id == int(caller.user.id),
            BotInstallation.bot_user_domain == caller.user.domain,
            usable_guild_installation(),
        )
    )
    if installation is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    if "soundboard.use" not in (installation.granted_scopes or []):
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_SCOPE_REQUIRED", "scope": "soundboard.use"},
        )


async def _local_soundboard_media_capability(
    session: AsyncSession,
    settings: Settings,
    sound: SoundboardSound,
    *,
    requested_volume: float | None,
    expected_version: int | None = None,
) -> dict[str, object]:
    # Serialize capability minting with terminal media verdicts. Revalidate
    # the exact binding under the digest fence so a malware/digest tombstone
    # can never race a new soundboard download capability.
    await lock_asset_digest(session, sound.media_hash)
    locked_sound = await session.scalar(
        select(SoundboardSound)
        .where(
            SoundboardSound.id == sound.id,
            SoundboardSound.origin_domain == sound.origin_domain,
            SoundboardSound.media_hash == sound.media_hash,
            SoundboardSound.available.is_(True),
            *(
                (SoundboardSound.version == expected_version,)
                if expected_version is not None
                else ()
            ),
        )
        .execution_options(populate_existing=True)
    )
    if locked_sound is None:
        raise HTTPException(status_code=404, detail={"code": "SOUNDBOARD_SOUND_NOT_FOUND"})
    sound = locked_sound
    terminal_duplicate = aliased(Attachment)
    attachment = await session.scalar(
        select(Attachment)
        .where(
            Attachment.asset_binding == f"soundboard:{sound.origin_domain}:{sound.id}",
            Attachment.origin_domain == settings.domain,
            Attachment.content_sha256 == sound.media_hash,
            Attachment.scan_status == "clean",
            Attachment.deleted_at.is_(None),
            ~exists(
                select(terminal_duplicate.id).where(
                    terminal_duplicate.origin_domain == settings.domain,
                    terminal_duplicate.content_sha256 == Attachment.content_sha256,
                    terminal_duplicate.scan_status.in_(DIGEST_REVOCATION_STATUSES),
                )
            ),
        )
        .with_for_update(read=True)
    )
    if (
        attachment is None
        or await session.get(MediaTombstoneSource, (attachment.id, attachment.origin_domain))
        is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "SOUNDBOARD_SOUND_NOT_FOUND"})
    try:
        download_url = S3Storage(settings).presign(
            "GET",
            settings.media_attachments_bucket,
            attachment.object_key,
            expires=60,
        )
    except StorageError as exc:
        raise HTTPException(status_code=503, detail={"code": "MEDIA_STORAGE_UNAVAILABLE"}) from exc
    await session.commit()
    return {
        "sound": soundboard_sound_payload(sound),
        "download_url": download_url,
        "media_authority": sound.origin_domain,
        "media_origin": media_url_origin(
            download_url,
            allow_http=settings.environment != "production",
        ),
        "effective_volume": sound.volume
        * (requested_volume if requested_volume is not None else 1),
        "expires_in": 60,
    }


async def _installation_allows_soundboard_channel(
    session: AsyncSession,
    installation: BotInstallation,
    channel: Channel,
) -> None:
    if not await installation_allows_channel(session, installation, channel):
        raise HTTPException(status_code=403, detail={"code": "BOT_CHANNEL_RESTRICTED"})


def _soundboard_play_model(
    request: SoundboardFederationRequest,
    result: dict[str, object],
) -> SoundboardFederationPlay:
    if request.channel is None:
        raise RuntimeError("soundboard play request has no channel")
    return SoundboardFederationPlay.model_validate(
        {
            "request": request,
            "capability": result,
            "guild": request.guild,
            "channel": request.channel,
            "user": request.caller.user,
            "delivery_id": f"kase_{secrets.token_urlsafe(24)}",
        }
    )


def _soundboard_gateway_event(play: SoundboardFederationPlay) -> dict[str, object]:
    return {
        **play.capability.model_dump(mode="json"),
        "guild_id": play.guild.id,
        "guild_domain": play.guild.domain,
        "channel_id": play.channel.id,
        "channel_domain": play.channel.domain,
        "user_id": play.user.id,
        "user_domain": play.user.domain,
        "delivery_id": play.delivery_id,
    }


def _soundboard_effect_routes(
    occupants: list[Occupant],
    local_domain: str,
) -> tuple[list[tuple[int, str]], list[str]]:
    """Route target-connected bots locally and federated humans through their homes."""

    local_gateway_users = sorted(
        {
            (int(occupant.user_id), occupant.user_domain)
            for occupant in occupants
            if occupant.user_domain == local_domain or occupant.client_kind == "bot"
        }
    )
    federation_destinations = sorted(
        {
            occupant.user_domain
            for occupant in occupants
            if occupant.user_domain != local_domain and occupant.client_kind != "bot"
        }
    )
    return local_gateway_users, federation_destinations


async def _deliver_soundboard_effect_to_local_occupants(
    redis: Redis,
    settings: Settings,
    play: SoundboardFederationPlay,
    *,
    authority_domain: str,
    consume_replay: bool,
) -> bool:
    if play.guild.domain != authority_domain or play.channel.domain != authority_domain:
        raise ValueError("soundboard effect room does not belong to its authority")
    occupants = await room_occupants(
        redis,
        authority_domain,
        guild_room_name(int(play.guild.id), int(play.channel.id)),
    )
    local_users, _destinations = _soundboard_effect_routes(occupants, settings.domain)
    if not local_users:
        return False
    if consume_replay:
        accepted = await redis.set(
            f"federation:soundboard-effect:{authority_domain}:{play.delivery_id}",
            "1",
            ex=SOUNDBOARD_EFFECT_TTL_SECONDS,
            nx=True,
        )
        if not accepted:
            return True
    event = _soundboard_gateway_event(play)
    for user_id, user_domain in local_users:
        await publish_dispatch(
            redis,
            user_topic(user_domain, user_id),
            "VOICE_CHANNEL_EFFECT_SEND",
            event,
        )
    return True


async def _propagate_soundboard_effect(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    destinations: list[str],
    envelope: dict[str, Any],
) -> None:
    async def deliver(destination: str) -> None:
        try:
            async with sessionmaker() as session:
                upstream = await signed_request(
                    session,
                    settings,
                    "POST",
                    destination,
                    "/_kaede/v1/voice/soundboard-effect",
                    payload=envelope,
                    request_timeout=5,
                    max_response_bytes=8 * 1024,
                )
                if upstream.status_code == 204:
                    await session.commit()
                else:
                    await session.rollback()
                    log.warning(
                        "soundboard_effect_rejected",
                        destination=destination,
                        status_code=upstream.status_code,
                    )
        except (FederationNetworkError, RuntimeError):
            log.warning("soundboard_effect_unreachable", destination=destination)

    for offset in range(0, len(destinations), 8):
        await asyncio.gather(*(deliver(item) for item in destinations[offset : offset + 8]))


async def _authoritative_soundboard_play_envelope(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild: Guild,
    request: SoundboardFederationRequest,
    result: dict[str, object],
    background_tasks: BackgroundTasks,
    http_request: Request,
) -> tuple[SoundboardFederationPlay, dict[str, Any]]:
    play = _soundboard_play_model(request, result)
    occupants = await room_occupants(
        redis,
        settings.domain,
        guild_room_name(int(play.guild.id), int(play.channel.id)),
    )
    _local_users, destinations = _soundboard_effect_routes(occupants, settings.domain)
    if request.requesting_instance != settings.domain or destinations:
        try:
            _validate_federation_play(play, request, settings)
        except ValueError:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "FEDERATED_SOUNDBOARD_CAPABILITY_INVALID",
                    "message": "The sound media authority returned an invalid capability.",
                },
            ) from None
    signer = await _soundboard_federation_signer(session, settings, guild)
    envelope = await build_guild_authority_envelope(
        session,
        settings,
        guild,
        SOUNDBOARD_FEDERATION_PLAY_EVENT,
        signer,
        play.model_dump(mode="json"),
        context={
            "guild_id": play.guild.id,
            "guild_domain": play.guild.domain,
            "channel_id": play.channel.id,
            "channel_domain": play.channel.domain,
        },
    )
    await _deliver_soundboard_effect_to_local_occupants(
        redis,
        settings,
        play,
        authority_domain=settings.domain,
        consume_replay=False,
    )
    if destinations:
        sessionmaker = cast(
            async_sessionmaker[AsyncSession],
            http_request.app.state.sessionmaker,
        )
        background_tasks.add_task(
            _propagate_soundboard_effect,
            sessionmaker,
            settings,
            destinations,
            envelope,
        )
    return play, envelope


@human_router.post(
    "/channels/{channel_ref}/send-soundboard-sound",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def authorize_human_soundboard_play(
    channel_ref: EntityRef,
    payload: SoundboardPlayRequest,
    response: Response,
    background_tasks: BackgroundTasks,
    http_request: Request,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    channel, guild = await load_voice_channel(session, channel_id, channel_domain)
    if not is_soundboard_channel_type(channel.type) or channel.guild_id is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "VOICE_CHANNEL_NOT_FOUND",
                "message": "That voice channel is unavailable.",
            },
        )
    sound_id, sound_domain = payload.sound_id.resolve(settings.domain)
    source_guild_ref = (
        payload.source_guild_id.resolve(settings.domain)
        if payload.source_guild_id is not None
        else None
    )
    actor_intent = await _build_human_soundboard_actor_intent(
        session,
        settings,
        auth.user,
        audience=sound_domain,
        source_guild=source_guild_ref,
        target_guild=(guild.id, guild.origin_domain),
        target_channel=(channel.id, channel.origin_domain),
        sound=(sound_id, sound_domain),
        sound_version=payload.sound_version,
        volume=payload.volume,
    )
    payload = payload.model_copy(update={"actor_intent": actor_intent})
    if channel.origin_domain != settings.domain or channel.guild_domain != settings.domain:
        member = await session.get(
            GuildMember,
            (guild.id, guild.origin_domain, auth.user.id, auth.user.origin_domain),
        )
        if guild.unavailable or member is None:
            raise HTTPException(status_code=404, detail={"code": "VOICE_CHANNEL_NOT_FOUND"})
        request = _new_federation_request(
            settings,
            guild,
            _federation_caller(auth.user),
            "play",
            sound=(sound_id, sound_domain),
            source_guild=source_guild_ref,
            channel=(channel.id, channel.origin_domain),
            sound_version=payload.sound_version,
            volume=payload.volume,
            actor_intent=actor_intent,
        )
        remote = await _request_remote_soundboard(session, settings, request)
        if not isinstance(remote, SoundboardFederationPlay):
            raise RuntimeError("soundboard play returned a query response")
        _validate_federation_play(remote, request, settings)
        await _deliver_soundboard_effect_to_local_occupants(
            redis,
            settings,
            remote,
            authority_domain=guild.origin_domain,
            consume_replay=True,
        )
        response.status_code = status.HTTP_204_NO_CONTENT
        return response
    caller = _federation_caller(auth.user)
    if sound_domain == settings.domain:
        try:
            await _validate_soundboard_actor_intent(
                session,
                redis,
                settings,
                caller,
                actor_intent,
                audience=settings.domain,
                runtime_target_domain=settings.domain,
                source_guild=source_guild_ref,
                target_guild=(guild.id, guild.origin_domain),
                target_channel=(channel.id, channel.origin_domain),
                sound=(sound_id, sound_domain),
                sound_version=payload.sound_version,
                volume=payload.volume,
                target_installation_revision=None,
            )
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=403,
                detail={"code": "SOUNDBOARD_ACTOR_INTENT_INVALID"},
            ) from None
    result = await _soundboard_play_capability(
        session,
        redis,
        settings,
        channel,
        guild,
        auth.user,
        payload,
        response,
        caller=caller,
        target_installation_revision=None,
    )
    request = _new_federation_request(
        settings,
        guild,
        caller,
        "play",
        sound=(sound_id, sound_domain),
        source_guild=source_guild_ref,
        channel=(channel.id, channel.origin_domain),
        sound_version=payload.sound_version,
        volume=payload.volume,
        actor_intent=actor_intent,
    )
    await _authoritative_soundboard_play_envelope(
        session,
        redis,
        settings,
        guild,
        request,
        result,
        background_tasks,
        http_request,
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


async def _authorize_bot_soundboard_play(
    channel_ref: EntityRef,
    payload: SoundboardPlayRequest,
    response: Response,
    background_tasks: BackgroundTasks,
    http_request: Request,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    channel, installation = await installation_for_channel(
        session, settings, principal, channel_ref, "soundboard.use"
    )
    if (
        not isinstance(installation, BotInstallation)
        or not is_soundboard_channel_type(channel.type)
        or channel.guild_id is None
    ):
        raise HTTPException(status_code=404, detail={"code": "VOICE_CHANNEL_NOT_FOUND"})
    guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
    if guild is None or guild.unavailable:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    sound_id, sound_domain = payload.sound_id.resolve(settings.domain)
    source_guild_ref = (
        payload.source_guild_id.resolve(settings.domain)
        if payload.source_guild_id is not None
        else None
    )
    caller = _federation_caller(
        principal.user,
        application_id=principal.application.id,
        application_domain=principal.application.origin_domain,
    )
    if payload.actor_intent is None:
        raise HTTPException(
            status_code=403,
            detail={"code": "SOUNDBOARD_ACTOR_INTENT_REQUIRED"},
        )
    if sound_domain == settings.domain:
        try:
            await _validate_soundboard_actor_intent(
                session,
                redis,
                settings,
                caller,
                payload.actor_intent,
                audience=settings.domain,
                runtime_target_domain=settings.domain,
                source_guild=source_guild_ref,
                target_guild=(guild.id, guild.origin_domain),
                target_channel=(channel.id, channel.origin_domain),
                sound=(sound_id, sound_domain),
                sound_version=payload.sound_version,
                volume=payload.volume,
                target_installation_revision=installation.grant_revision,
            )
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=403,
                detail={"code": "SOUNDBOARD_ACTOR_INTENT_INVALID"},
            ) from None
    request = _new_federation_request(
        settings,
        guild,
        caller,
        "play",
        sound=(sound_id, sound_domain),
        source_guild=source_guild_ref,
        channel=(channel.id, channel.origin_domain),
        sound_version=payload.sound_version,
        volume=payload.volume,
        actor_intent=payload.actor_intent,
    )
    if channel.origin_domain != settings.domain or channel.guild_domain != settings.domain:
        remote = await _request_remote_soundboard(session, settings, request)
        if not isinstance(remote, SoundboardFederationPlay):
            raise RuntimeError("soundboard play returned a query response")
        capability = _validate_federation_play(remote, request, settings)
        await _deliver_soundboard_effect_to_local_occupants(
            redis,
            settings,
            remote,
            authority_domain=guild.origin_domain,
            consume_replay=True,
        )
        return capability
    result = await _soundboard_play_capability(
        session,
        redis,
        settings,
        channel,
        guild,
        principal.user,
        payload,
        response,
        caller=caller,
        target_installation_revision=installation.grant_revision,
    )
    await _authoritative_soundboard_play_envelope(
        session,
        redis,
        settings,
        guild,
        request,
        result,
        background_tasks,
        http_request,
    )
    return result


@router.post(
    "/channels/{channel_ref}/send-soundboard-sound",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def authorize_soundboard_play(
    channel_ref: EntityRef,
    payload: SoundboardPlayRequest,
    response: Response,
    background_tasks: BackgroundTasks,
    http_request: Request,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await _authorize_bot_soundboard_play(
        channel_ref,
        payload,
        response,
        background_tasks,
        http_request,
        principal,
        session,
        redis,
        settings,
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post("/channels/{channel_ref}/soundboard-playback-grants")
async def create_soundboard_playback_grant(
    channel_ref: EntityRef,
    payload: SoundboardPlayRequest,
    response: Response,
    background_tasks: BackgroundTasks,
    http_request: Request,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    return await _authorize_bot_soundboard_play(
        channel_ref,
        payload,
        response,
        background_tasks,
        http_request,
        principal,
        session,
        redis,
        settings,
    )


@federation_router.post("/_kaede/v1/guilds/{guild_id}/soundboard/play")
async def federation_soundboard_play(
    guild_id: Snowflake,
    payload: SoundboardFederationRequest,
    response: Response,
    background_tasks: BackgroundTasks,
    http_request: Request,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "guild-soundboard-play",
        capacity=60,
        refill_per_minute=60,
    )
    guild, actor, installation = await _authorize_federated_soundboard_request(
        session,
        redis,
        settings,
        principal,
        guild_id,
        payload,
        scopes={"play": "soundboard.use"},
    )
    if payload.channel is None or payload.sound is None:
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})
    channel = await session.get(
        Channel,
        (int(payload.channel.id), payload.channel.domain),
    )
    if (
        channel is None
        or channel.unavailable
        or not is_soundboard_channel_type(channel.type)
        or channel.origin_domain != settings.domain
        or channel.guild_id != guild.id
        or channel.guild_domain != guild.origin_domain
    ):
        raise HTTPException(status_code=404, detail={"code": "VOICE_CHANNEL_NOT_FOUND"})
    if installation is not None:
        await _installation_allows_soundboard_channel(session, installation, channel)
    capability = await _soundboard_play_capability(
        session,
        redis,
        settings,
        channel,
        guild,
        actor,
        SoundboardPlayRequest(
            sound_id=EntityRef(f"{payload.sound.id}@{payload.sound.domain}"),
            source_guild_id=(
                EntityRef(f"{payload.source_guild.id}@{payload.source_guild.domain}")
                if payload.source_guild is not None
                else None
            ),
            sound_version=payload.sound_version,
            volume=payload.volume,
            actor_intent=payload.actor_intent,
        ),
        response,
        caller=payload.caller,
        target_installation_revision=(
            installation.grant_revision if installation is not None else None
        ),
    )
    _, envelope = await _authoritative_soundboard_play_envelope(
        session,
        redis,
        settings,
        guild,
        payload,
        capability,
        background_tasks,
        http_request,
    )
    return envelope


@federation_router.post("/_kaede/v1/voice/soundboard-effect", status_code=204)
async def federation_soundboard_effect(
    payload: dict[str, Any],
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "voice-soundboard-effect",
        capacity=180,
        refill_per_minute=180,
    )
    try:
        envelope = await validated_event_envelope(
            session,
            settings,
            principal.origin,
            payload,
            allow_authority_attested_actor=True,
        )
        if envelope.type != SOUNDBOARD_FEDERATION_PLAY_EVENT:
            raise ValueError("soundboard effect has the wrong signed event type")
        play = SoundboardFederationPlay.model_validate(envelope.content)
        request = play.request
        if request.guild.domain != principal.origin or request.operation != "play":
            raise ValueError("soundboard effect has the wrong authority")
        expected_context = {
            "guild_id": play.guild.id,
            "guild_domain": play.guild.domain,
            "channel_id": play.channel.id,
            "channel_domain": play.channel.domain,
        }
        if envelope.context != expected_context:
            raise ValueError("soundboard effect has the wrong signed context")
        timestamp_floor = (request.issued_at - settings.federation_clock_skew_seconds) * 1_000
        timestamp_ceiling = (request.deadline + settings.federation_clock_skew_seconds) * 1_000
        if not timestamp_floor <= envelope.ts < timestamp_ceiling:
            raise ValueError("soundboard effect was signed outside its request window")
        if int(time.time()) >= request.deadline + settings.federation_clock_skew_seconds:
            raise ValueError("soundboard effect has expired")
        _validate_federation_play(play, request, settings)
    except (FederationNetworkError, ValidationError, TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "KAED_FED_SOUNDBOARD_EFFECT_INVALID",
                "message": "The signed soundboard effect is invalid or expired.",
            },
        ) from None
    guild = await session.get(Guild, (int(play.guild.id), play.guild.domain))
    channel = await session.get(Channel, (int(play.channel.id), play.channel.domain))
    if (
        guild is None
        or guild.unavailable
        or channel is None
        or channel.unavailable
        or not is_soundboard_channel_type(channel.type)
        or channel.guild_id != guild.id
        or channel.guild_domain != guild.origin_domain
    ):
        raise HTTPException(status_code=404, detail={"code": "VOICE_CHANNEL_NOT_FOUND"})
    if (int(envelope.actor.id), envelope.actor.domain) != (
        guild.owner_id,
        guild.owner_domain,
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "KAED_FED_SOUNDBOARD_EFFECT_INVALID",
                "message": "The signed soundboard effect is invalid or expired.",
            },
        )
    delivered = await _deliver_soundboard_effect_to_local_occupants(
        redis,
        settings,
        play,
        authority_domain=principal.origin,
        consume_replay=True,
    )
    if not delivered:
        raise HTTPException(status_code=403, detail={"code": "KAED_VOICE_NOT_SUBSCRIBED"})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
