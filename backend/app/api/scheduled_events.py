from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import ConfigDict, Field, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.api.guilds import local_guild
from app.chat.audit import add_audit_entry
from app.chat.events import guild_topic
from app.chat.guild_revision import queue_guild_mutation, wake_queued_guild_federation
from app.chat.payloads import member_payload, user_payload
from app.chat.permissions import get_permissions, require_permissions
from app.chat.postcommit import publish_committed_dispatches, queue_postcommit_dispatch
from app.chat.schemas import RequestModel, cleaned_nonempty
from app.core.permissions import Permission
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef
from app.db.bot_models import BotInstallation
from app.db.materialization import materialize_updated_at
from app.db.models import (
    Attachment,
    Channel,
    Guild,
    GuildMember,
    GuildScheduledEvent,
    GuildScheduledEventSubscription,
    Invite,
    MemberRole,
    User,
)
from app.federation.guild_management import (
    GuildManagementOperation,
    GuildManagementResult,
    proxy_remote_guild_management,
)
from app.media.schemas import AssetCommitRequest, UploadTicketRequest
from app.media.service import (
    attachment_payload,
    bind_asset,
    create_upload_ticket,
    finalize_attachment,
    is_federated_human_authority_upload,
    require_image_type,
    ticket_payload,
)
from app.scheduled_events.recurrence import (
    ScheduledEventNWeekday,
    ScheduledEventRecurrenceRule,
)
from app.scheduled_events.service import (
    ACTIVE,
    CANCELED,
    CHANNEL_EVENT_TYPES,
    COMPLETED,
    EXTERNAL,
    SCHEDULED,
    STAGE_INSTANCE,
    advance_scheduled_event_lifecycle,
    materialize_next_recurrence,
    scheduled_event_image_binding,
    scheduled_event_lifecycle_status,
    scheduled_event_payload,
)
from app.scheduled_events.service import (
    VOICE as VOICE,
)
from app.scheduled_events.service import (
    event_creator as _creator,
)
from app.scheduled_events.service import (
    subscriber_count as _subscriber_count,
)
from app.scheduled_events.service import (
    viewer_subscribed as _viewer_subscribed,
)
from app.tasks import media_local_purge, media_process
from app.voice.permissions import (
    STAGE_INSTANCE_MODERATOR_PERMISSIONS,
    STAGE_INSTANCE_VIEW_PERMISSIONS,
    VOICE_CHANNEL_ACCESS_PERMISSIONS,
)

router = APIRouter(prefix="/api/v1", tags=["scheduled events"])

__all__ = (
    "ScheduledEventNWeekday",
    "ScheduledEventRecurrenceRule",
    "advance_scheduled_event_lifecycle",
    "scheduled_event_lifecycle_status",
)

TERMINAL_STATUSES = frozenset({COMPLETED, CANCELED})
VALID_STATUS_TRANSITIONS = {
    SCHEDULED: frozenset({ACTIVE, CANCELED}),
    ACTIVE: frozenset({COMPLETED}),
    COMPLETED: frozenset(),
    CANCELED: frozenset(),
}
CHANNEL_EVENT_VIEW_PERMISSION = STAGE_INSTANCE_VIEW_PERMISSIONS
CHANNEL_EVENT_CREATE_PERMISSIONS = Permission.CREATE_EVENTS | VOICE_CHANNEL_ACCESS_PERMISSIONS
MAX_ACTIVE_SCHEDULED_EVENTS = 100
SCHEDULED_EVENT_IMAGE_MAX_BYTES = 10 * 1024 * 1024


async def _proxy_human(
    session: AsyncSession,
    settings: Settings,
    guild_ref: EntityRef,
    auth: AuthenticatedUser,
    operation: GuildManagementOperation,
    payload: dict[str, Any],
) -> tuple[bool, GuildManagementResult | None]:
    result = await proxy_remote_guild_management(
        session, settings, guild_ref, auth.user, operation, payload
    )
    return result is not None, result


def _event_image_staging_binding(event: GuildScheduledEvent, attachment_id: int) -> str:
    return f"scheduled_event_staging:{event.origin_domain}:{event.id}:{attachment_id}"


class ScheduledEventEntityMetadata(RequestModel):
    model_config = ConfigDict(extra="forbid")

    location: str = Field(min_length=1, max_length=100)

    @field_validator("location")
    @classmethod
    def clean_location(cls, value: str) -> str:
        return cleaned_nonempty(value)


class _ScheduledEventRequest(RequestModel):
    @field_validator("privacy_level", "entity_type", "status", mode="before", check_fields=False)
    @classmethod
    def strict_integer_enums(cls, value: object) -> object:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("scheduled event enum values must be integers")
        return value


class ScheduledEventCreate(_ScheduledEventRequest):
    model_config = ConfigDict(extra="forbid")

    channel_id: EntityRef | None = None
    entity_metadata: ScheduledEventEntityMetadata | None = None
    name: str = Field(min_length=1, max_length=100)
    privacy_level: Literal[2] = 2
    scheduled_start_time: datetime
    scheduled_end_time: datetime | None = None
    description: str | None = Field(default=None, min_length=1, max_length=1000)
    entity_type: Literal[1, 2, 3]
    recurrence_rule: ScheduledEventRecurrenceRule | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return cleaned_nonempty(value)

    @field_validator("description")
    @classmethod
    def clean_description(cls, value: str | None) -> str | None:
        return cleaned_nonempty(value) if value is not None else None


class ScheduledEventPatch(_ScheduledEventRequest):
    model_config = ConfigDict(extra="forbid")

    channel_id: EntityRef | None = None
    entity_metadata: ScheduledEventEntityMetadata | None = None
    name: str | None = Field(default=None, min_length=1, max_length=100)
    privacy_level: Literal[2] | None = None
    scheduled_start_time: datetime | None = None
    scheduled_end_time: datetime | None = None
    description: str | None = Field(default=None, min_length=1, max_length=1000)
    entity_type: Literal[1, 2, 3] | None = None
    status: Literal[1, 2, 3, 4] | None = None
    recurrence_rule: ScheduledEventRecurrenceRule | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        return cleaned_nonempty(value) if value is not None else None

    @field_validator("description")
    @classmethod
    def clean_description(cls, value: str | None) -> str | None:
        return cleaned_nonempty(value) if value is not None else None

    @model_validator(mode="after")
    def validate_patch(self) -> ScheduledEventPatch:
        if not self.model_fields_set:
            raise ValueError("at least one scheduled event field is required")
        required_fields = (
            "name",
            "privacy_level",
            "scheduled_start_time",
            "entity_type",
            "status",
        )
        for field_name in required_fields:
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"scheduled event {field_name} cannot be null")
        return self


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _normalized_time(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise _error(
            400,
            "SCHEDULED_EVENT_TIMEZONE_REQUIRED",
            f"{field_name} must include a timezone offset.",
        )
    return value.astimezone(UTC)


def _recurrence_payload(
    rule: ScheduledEventRecurrenceRule | None,
    *,
    scheduled_start_time: datetime,
) -> dict[str, object] | None:
    if rule is None:
        return None
    if rule.start.astimezone(UTC) != scheduled_start_time:
        raise _error(
            400,
            "SCHEDULED_EVENT_RECURRENCE_START_INVALID",
            "The recurrence start must match the scheduled event start time.",
        )
    return rule.model_dump(mode="json")


def _event_ref(event: GuildScheduledEvent) -> dict[str, str]:
    return {"id": str(event.id), "origin_domain": event.origin_domain}


async def scheduled_event_for_guild(
    session: AsyncSession,
    guild: Guild,
    event_ref: EntityRef,
    *,
    for_update: bool = False,
) -> GuildScheduledEvent:
    event_id, event_domain = event_ref.resolve(guild.origin_domain)
    statement = select(GuildScheduledEvent).where(
        GuildScheduledEvent.id == event_id,
        GuildScheduledEvent.origin_domain == event_domain,
        GuildScheduledEvent.guild_id == guild.id,
        GuildScheduledEvent.guild_domain == guild.origin_domain,
    )
    if for_update:
        statement = statement.with_for_update()
    event = await session.scalar(statement)
    if event is None:
        raise _error(
            404,
            "SCHEDULED_EVENT_NOT_FOUND",
            "That scheduled event no longer exists in this guild.",
        )
    return event


async def active_scheduled_event_for_invite(
    session: AsyncSession,
    invite: Invite,
) -> GuildScheduledEvent | None:
    if invite.scheduled_event_id is None or invite.scheduled_event_domain is None:
        return None
    event = await session.get(
        GuildScheduledEvent,
        (invite.scheduled_event_id, invite.scheduled_event_domain),
    )
    if (
        event is None
        or (event.guild_id, event.guild_domain) != (invite.guild_id, invite.guild_domain)
        or event.status in TERMINAL_STATUSES
    ):
        return None
    return event


async def active_scheduled_event_by_ref(
    session: AsyncSession,
    guild: Guild,
    event_ref: EntityRef,
) -> GuildScheduledEvent:
    event = await scheduled_event_for_guild(session, guild, event_ref)
    if event.status in TERMINAL_STATUSES:
        raise _error(
            400,
            "INVITE_TARGET_EVENT_INVALID",
            "Choose a scheduled or active event from this guild.",
        )
    return event


async def scheduled_event_invite_payload(
    session: AsyncSession,
    invite: Invite,
) -> dict[str, object] | None:
    event = await active_scheduled_event_for_invite(session, invite)
    if event is None:
        return None
    return scheduled_event_payload(
        event,
        creator=await _creator(session, event),
        user_count=await _subscriber_count(session, event),
    )


async def _require_member(session: AsyncSession, guild: Guild, actor: User) -> GuildMember:
    member = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, actor.id, actor.origin_domain),
    )
    if member is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return member


async def _event_channel(
    session: AsyncSession,
    guild: Guild,
    entity_type: int,
    channel_ref: EntityRef | None,
) -> Channel:
    if channel_ref is None:
        raise _error(
            400,
            "SCHEDULED_EVENT_CHANNEL_REQUIRED",
            "Voice and Stage events require a matching channel in this guild.",
        )
    channel_id, channel_domain = channel_ref.resolve(guild.origin_domain)
    channel = await session.get(Channel, (channel_id, channel_domain))
    if (
        channel is None
        or channel.unavailable
        or channel.type != (13 if entity_type == STAGE_INSTANCE else 2)
        or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
    ):
        raise _error(
            400,
            "SCHEDULED_EVENT_CHANNEL_INVALID",
            "Choose an available channel matching the scheduled event type.",
        )
    return channel


async def _require_event_view(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    event: GuildScheduledEvent,
) -> None:
    await _require_member(session, guild, actor)
    if event.entity_type in CHANNEL_EVENT_TYPES:
        channel = await session.get(Channel, (event.channel_id, event.channel_domain))
        if channel is None:
            raise _error(
                404,
                "SCHEDULED_EVENT_NOT_FOUND",
                "That scheduled event no longer exists in this guild.",
            )
        await require_permissions(
            session,
            redis,
            guild,
            actor,
            CHANNEL_EVENT_VIEW_PERMISSION,
            channel=channel,
        )


async def require_scheduled_event_view(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    event: GuildScheduledEvent,
) -> None:
    """Authorize an event read for routes that embed scheduled events."""

    await _require_event_view(session, redis, guild, actor, event)


async def _can_view_event(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    event: GuildScheduledEvent,
    channels: dict[tuple[int, str], Channel],
) -> bool:
    if event.entity_type not in CHANNEL_EVENT_TYPES:
        return True
    if event.channel_id is None or event.channel_domain is None:
        return False
    channel = channels.get((event.channel_id, event.channel_domain))
    if channel is None:
        return False
    permissions = await get_permissions(session, redis, guild, actor, channel=channel)
    return bool(permissions & Permission.ADMINISTRATOR) or (
        permissions & CHANNEL_EVENT_VIEW_PERMISSION == CHANNEL_EVENT_VIEW_PERMISSION
    )


async def _require_event_management(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    event: GuildScheduledEvent,
) -> None:
    own_event = (event.creator_id, event.creator_domain) == (actor.id, actor.origin_domain)
    channel: Channel | None = None
    if event.entity_type in CHANNEL_EVENT_TYPES:
        channel = await session.get(Channel, (event.channel_id, event.channel_domain))
        if channel is None:
            raise _error(
                409,
                "SCHEDULED_EVENT_CHANNEL_INVALID",
                "The voice channel for this scheduled event is unavailable.",
            )
    permissions = await get_permissions(session, redis, guild, actor, channel=channel)
    management_permissions = (
        Permission.CREATE_EVENTS | Permission.MANAGE_EVENTS
        if own_event
        else Permission.MANAGE_EVENTS
    )
    allowed = bool(permissions & management_permissions)
    if not allowed:
        raise _error(
            403,
            "MISSING_PERMISSIONS",
            "You need permission to manage this scheduled event.",
        ) from None
    if channel is not None and event.entity_type == STAGE_INSTANCE:
        await require_permissions(
            session,
            redis,
            guild,
            actor,
            STAGE_INSTANCE_MODERATOR_PERMISSIONS,
            channel=channel,
        )
    elif channel is not None:
        await require_permissions(
            session,
            redis,
            guild,
            actor,
            VOICE_CHANNEL_ACCESS_PERMISSIONS,
            channel=channel,
        )


def _validate_entity_fields(
    *,
    entity_type: int,
    channel: Channel | None,
    entity_metadata: dict[str, object] | None,
    scheduled_start_time: datetime,
    scheduled_end_time: datetime | None,
) -> None:
    if entity_type in CHANNEL_EVENT_TYPES:
        if channel is None or entity_metadata is not None:
            raise _error(
                400,
                "SCHEDULED_EVENT_ENTITY_FIELDS_INVALID",
                "Voice and Stage events require channel_id and do not accept entity_metadata.",
            )
    elif entity_type == EXTERNAL:
        location = entity_metadata.get("location") if entity_metadata else None
        if channel is not None or not isinstance(location, str) or not location:
            raise _error(
                400,
                "SCHEDULED_EVENT_ENTITY_FIELDS_INVALID",
                "External events require entity_metadata.location and must not have a channel.",
            )
        if scheduled_end_time is None:
            raise _error(
                400,
                "SCHEDULED_EVENT_END_TIME_REQUIRED",
                "External events require a scheduled end time.",
            )
    if scheduled_end_time is not None and scheduled_end_time <= scheduled_start_time:
        raise _error(
            400,
            "SCHEDULED_EVENT_END_TIME_INVALID",
            "The scheduled end time must be later than the scheduled start time.",
        )


def _audit_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Channel):
        return f"{value.id}@{value.origin_domain}"
    return value


async def create_scheduled_event_image_ticket_for(
    session: AsyncSession,
    redis: Redis,
    response: Response,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    guild: Guild,
    event: GuildScheduledEvent,
    actor: User,
    payload: UploadTicketRequest,
    *,
    bot_installation: BotInstallation | None = None,
) -> dict[str, object]:
    await _require_event_management(session, redis, guild, actor, event)
    if event.status in TERMINAL_STATUSES:
        raise _error(
            400,
            "SCHEDULED_EVENT_TERMINAL",
            "Completed and canceled events can no longer be changed.",
        )
    require_image_type(payload.content_type)
    if payload.size > SCHEDULED_EVENT_IMAGE_MAX_BYTES:
        raise _error(
            413,
            "SCHEDULED_EVENT_IMAGE_TOO_LARGE",
            "Scheduled event cover images can be at most 10 MiB.",
        )
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["upload_ticket"],
        user_id=actor.id,
        user_domain=actor.origin_domain,
    )
    attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        actor,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
        purpose="scheduled_event_image",
        bot_installation=bot_installation,
        federated_guild_upload=is_federated_human_authority_upload(actor, settings),
    )
    attachment.asset_binding = _event_image_staging_binding(event, attachment.id)
    await session.commit()
    return ticket_payload(attachment, upload_url)


async def _scheduled_event_projection_channel(
    session: AsyncSession,
    guild: Guild,
    event: GuildScheduledEvent,
) -> Channel | None:
    channel = (
        await session.get(Channel, (event.channel_id, event.channel_domain))
        if event.channel_id is not None and event.channel_domain is not None
        else None
    )
    if channel is not None and (channel.guild_id, channel.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        raise RuntimeError("scheduled event channel belongs to another guild")
    return channel


async def _queue_scheduled_event_projection(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    actor: User,
    event: GuildScheduledEvent,
    operation: Literal["create", "update", "delete"],
    rendered: dict[str, object],
) -> None:
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        f"guild.scheduled_event.{operation}",
        {"scheduled_event": rendered},
        channel=await _scheduled_event_projection_channel(session, guild, event),
    )


async def _queue_scheduled_event_subscription_projection(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    actor: User,
    event: GuildScheduledEvent,
    operation: Literal["add", "remove"],
    rendered: dict[str, object],
) -> None:
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        f"guild.scheduled_event.user.{operation}",
        {"subscription": rendered},
        channel=await _scheduled_event_projection_channel(session, guild, event),
    )


async def _publish_scheduled_event_image_change(
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    guild: Guild,
    event: GuildScheduledEvent,
    actor: User,
    *,
    old_hash: str | None,
    reason: str | None,
) -> dict[str, object]:
    await materialize_updated_at(session, event)
    creator = await _creator(session, event)
    rendered = scheduled_event_payload(
        event,
        creator=creator,
        user_count=await _subscriber_count(session, event),
    )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        actor,
        101,
        target_type="scheduled_event",
        target_ref=_event_ref(event),
        reason=reason,
        changes=[
            {
                "key": "image_hash",
                "old_value": old_hash,
                "new_value": event.image_hash,
            }
        ],
    )
    await _queue_scheduled_event_projection(
        session,
        settings,
        guild,
        actor,
        event,
        "update",
        rendered,
    )
    queue_postcommit_dispatch(
        session,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_SCHEDULED_EVENT_UPDATE",
        rendered,
    )
    await session.commit()
    await publish_committed_dispatches(session, redis)
    await wake_queued_guild_federation(guild)
    return rendered


async def create_scheduled_event_image_ticket(
    guild_ref: EntityRef,
    event_ref: EntityRef,
    payload: UploadTicketRequest,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    proxied, result = await _proxy_human(
        session,
        settings,
        guild_ref,
        auth,
        "scheduled_event.image.ticket",
        {
            "resource_ref": str(event_ref),
            "data": payload.model_dump(mode="json"),
        },
    )
    if proxied:
        response.status_code = result.status_code if result is not None else 201
        return cast(dict[str, object], result.body if result is not None else None)
    guild = await local_guild(session, settings, guild_ref)
    event = await scheduled_event_for_guild(session, guild, event_ref)
    return await create_scheduled_event_image_ticket_for(
        session,
        redis,
        response,
        settings,
        snowflake,
        guild,
        event,
        auth.user,
        payload,
    )


router.add_api_route(
    "/guilds/{guild_ref}/scheduled-events/{event_ref}/image/tickets",
    create_scheduled_event_image_ticket,
    methods=["POST"],
    status_code=201,
)


async def commit_scheduled_event_image(
    guild_ref: EntityRef,
    event_ref: EntityRef,
    payload: AssetCommitRequest,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    proxied, result = await _proxy_human(
        session,
        settings,
        guild_ref,
        auth,
        "scheduled_event.image.commit",
        {
            "resource_ref": str(event_ref),
            "data": payload.model_dump(mode="json"),
            "reason": reason,
        },
    )
    if proxied:
        response.status_code = result.status_code if result is not None else 200
        return cast(dict[str, object], result.body if result is not None else None)
    guild = await local_guild(session, settings, guild_ref)
    event = await scheduled_event_for_guild(session, guild, event_ref, for_update=True)
    await _require_event_management(session, redis, guild, auth.user, event)
    if event.status in TERMINAL_STATUSES:
        raise _error(
            400,
            "SCHEDULED_EVENT_TERMINAL",
            "Completed and canceled events can no longer be changed.",
        )
    attachment = await finalize_attachment(
        session,
        settings,
        auth.user,
        int(payload.attachment_id),
        required_purpose="scheduled_event_image",
        federated_guild_upload=is_federated_human_authority_upload(auth.user, settings),
    )
    if attachment.asset_binding != _event_image_staging_binding(event, attachment.id):
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    if attachment.scan_status != "clean":
        await session.commit()
        await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": "processing", "attachment": attachment_payload(attachment)}
    if attachment.content_sha256 is None:
        raise RuntimeError("clean scheduled event image is missing its digest")
    require_image_type(attachment.detected_content_type)
    old_hash = event.image_hash
    attachment.asset_binding = None
    previous = await bind_asset(session, attachment, scheduled_event_image_binding(event))
    event.image_hash = attachment.content_sha256
    rendered = await _publish_scheduled_event_image_change(
        session,
        redis,
        snowflake,
        settings,
        guild,
        event,
        auth.user,
        old_hash=old_hash,
        reason=reason,
    )
    if previous is not None:
        await enqueue_best_effort(media_local_purge, previous.id, previous.origin_domain)
    return rendered


router.add_api_route(
    "/guilds/{guild_ref}/scheduled-events/{event_ref}/image",
    commit_scheduled_event_image,
    methods=["PUT"],
)


async def delete_scheduled_event_image(
    guild_ref: EntityRef,
    event_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    proxied, result = await _proxy_human(
        session,
        settings,
        guild_ref,
        auth,
        "scheduled_event.image.delete",
        {"resource_ref": str(event_ref), "reason": reason},
    )
    if proxied:
        return cast(dict[str, object], result.body if result is not None else None)
    guild = await local_guild(session, settings, guild_ref)
    event = await scheduled_event_for_guild(session, guild, event_ref, for_update=True)
    await _require_event_management(session, redis, guild, auth.user, event)
    if event.status in TERMINAL_STATUSES:
        raise _error(
            400,
            "SCHEDULED_EVENT_TERMINAL",
            "Completed and canceled events can no longer be changed.",
        )
    old_hash = event.image_hash
    previous = await session.scalar(
        select(Attachment)
        .where(Attachment.asset_binding == scheduled_event_image_binding(event))
        .with_for_update()
    )
    if previous is not None:
        previous.asset_binding = None
    event.image_hash = None
    if old_hash is None:
        return scheduled_event_payload(
            event,
            creator=await _creator(session, event),
            user_count=await _subscriber_count(session, event),
        )
    rendered = await _publish_scheduled_event_image_change(
        session,
        redis,
        snowflake,
        settings,
        guild,
        event,
        auth.user,
        old_hash=old_hash,
        reason=reason,
    )
    if previous is not None:
        await enqueue_best_effort(media_local_purge, previous.id, previous.origin_domain)
    return rendered


router.add_api_route(
    "/guilds/{guild_ref}/scheduled-events/{event_ref}/image",
    delete_scheduled_event_image,
    methods=["DELETE"],
)


async def list_scheduled_events(
    guild_ref: EntityRef,
    with_user_count: bool = Query(default=False),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    proxied, result = await _proxy_human(
        session,
        settings,
        guild_ref,
        auth,
        "scheduled_event.list",
        {"with_user_count": with_user_count},
    )
    if proxied:
        return cast(list[dict[str, object]], result.body if result is not None else None)
    guild = await local_guild(session, settings, guild_ref)
    await _require_member(session, guild, auth.user)
    count_query = (
        select(func.count())
        .select_from(GuildScheduledEventSubscription)
        .where(
            GuildScheduledEventSubscription.event_id == GuildScheduledEvent.id,
            GuildScheduledEventSubscription.event_domain == GuildScheduledEvent.origin_domain,
        )
        .correlate(GuildScheduledEvent)
        .scalar_subquery()
    )
    rows = (
        await session.execute(
            select(GuildScheduledEvent, count_query.label("user_count"))
            .where(
                GuildScheduledEvent.guild_id == guild.id,
                GuildScheduledEvent.guild_domain == guild.origin_domain,
                GuildScheduledEvent.status.in_((SCHEDULED, ACTIVE)),
            )
            .order_by(
                GuildScheduledEvent.scheduled_start_time,
                GuildScheduledEvent.id,
                GuildScheduledEvent.origin_domain,
            )
        )
    ).all()
    subscribed_refs = {
        (event_id, event_domain)
        for event_id, event_domain in (
            await session.execute(
                select(
                    GuildScheduledEventSubscription.event_id,
                    GuildScheduledEventSubscription.event_domain,
                ).where(
                    GuildScheduledEventSubscription.guild_id == guild.id,
                    GuildScheduledEventSubscription.guild_domain == guild.origin_domain,
                    GuildScheduledEventSubscription.user_id == auth.user.id,
                    GuildScheduledEventSubscription.user_domain == auth.user.origin_domain,
                )
            )
        ).all()
    }
    creator_refs = {(event.creator_id, event.creator_domain) for event, _ in rows}
    creators = {
        (creator.id, creator.origin_domain): creator
        for creator in (
            list(
                await session.scalars(
                    select(User).where(tuple_(User.id, User.origin_domain).in_(creator_refs))
                )
            )
            if creator_refs
            else []
        )
    }
    channel_refs = {
        (event.channel_id, event.channel_domain)
        for event, _ in rows
        if event.channel_id is not None and event.channel_domain is not None
    }
    channels = {
        (channel.id, channel.origin_domain): channel
        for channel in (
            list(
                await session.scalars(
                    select(Channel).where(
                        tuple_(Channel.id, Channel.origin_domain).in_(channel_refs)
                    )
                )
            )
            if channel_refs
            else []
        )
    }
    rendered: list[dict[str, object]] = []
    for event, user_count in rows:
        if not await _can_view_event(session, redis, guild, auth.user, event, channels):
            continue
        rendered.append(
            scheduled_event_payload(
                event,
                creator=creators.get((event.creator_id, event.creator_domain)),
                user_count=int(user_count) if with_user_count else None,
                me_subscribed=(event.id, event.origin_domain) in subscribed_refs,
            )
        )
    return rendered


router.add_api_route(
    "/guilds/{guild_ref}/scheduled-events",
    list_scheduled_events,
    methods=["GET"],
)


async def create_scheduled_event(
    guild_ref: EntityRef,
    payload: ScheduledEventCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    proxied, result = await _proxy_human(
        session,
        settings,
        guild_ref,
        auth,
        "scheduled_event.create",
        {
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "reason": reason,
        },
    )
    if proxied:
        return cast(dict[str, object], result.body if result is not None else None)
    guild = await local_guild(session, settings, guild_ref, for_update=True)
    active_count = int(
        await session.scalar(
            select(func.count())
            .select_from(GuildScheduledEvent)
            .where(
                GuildScheduledEvent.guild_id == guild.id,
                GuildScheduledEvent.guild_domain == guild.origin_domain,
                GuildScheduledEvent.status.in_((SCHEDULED, ACTIVE)),
            )
        )
        or 0
    )
    if active_count >= MAX_ACTIVE_SCHEDULED_EVENTS:
        raise _error(
            400,
            "SCHEDULED_EVENT_LIMIT_REACHED",
            "This guild already has 100 scheduled or active events.",
        )
    start_time = _normalized_time(payload.scheduled_start_time, field_name="scheduled_start_time")
    if start_time <= datetime.now(UTC):
        raise _error(
            400,
            "SCHEDULED_EVENT_START_TIME_INVALID",
            "The scheduled start time must be in the future.",
        )
    end_time = (
        _normalized_time(payload.scheduled_end_time, field_name="scheduled_end_time")
        if payload.scheduled_end_time is not None
        else None
    )
    if payload.entity_type == EXTERNAL and payload.channel_id is not None:
        raise _error(
            400,
            "SCHEDULED_EVENT_ENTITY_FIELDS_INVALID",
            "External events must not have a channel.",
        )
    channel = (
        await _event_channel(session, guild, payload.entity_type, payload.channel_id)
        if payload.entity_type in CHANNEL_EVENT_TYPES
        else None
    )
    if channel is not None:
        required = (
            Permission.CREATE_EVENTS | STAGE_INSTANCE_MODERATOR_PERMISSIONS
            if payload.entity_type == STAGE_INSTANCE
            else CHANNEL_EVENT_CREATE_PERMISSIONS
        )
        await require_permissions(
            session,
            redis,
            guild,
            auth.user,
            required,
            channel=channel,
        )
    else:
        await require_permissions(session, redis, guild, auth.user, Permission.CREATE_EVENTS)
    metadata = (
        payload.entity_metadata.model_dump(mode="json")
        if payload.entity_metadata is not None
        else None
    )
    _validate_entity_fields(
        entity_type=payload.entity_type,
        channel=channel,
        entity_metadata=metadata,
        scheduled_start_time=start_time,
        scheduled_end_time=end_time,
    )
    event = GuildScheduledEvent(
        id=await snowflake.mint(),
        origin_domain=guild.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        channel_id=channel.id if channel is not None else None,
        channel_domain=channel.origin_domain if channel is not None else None,
        creator_id=auth.user.id,
        creator_domain=auth.user.origin_domain,
        name=payload.name,
        description=payload.description,
        scheduled_start_time=start_time,
        scheduled_end_time=end_time,
        privacy_level=payload.privacy_level,
        status=SCHEDULED,
        entity_type=payload.entity_type,
        entity_metadata=metadata,
        recurrence_rule=(
            _recurrence_payload(
                payload.recurrence_rule,
                scheduled_start_time=start_time,
            )
        ),
    )
    session.add(event)
    await session.flush()
    rendered = scheduled_event_payload(event, creator=auth.user, user_count=0)
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        100,
        target_type="scheduled_event",
        target_ref=_event_ref(event),
        reason=reason,
        changes=[
            {"key": "name", "new_value": event.name},
            {"key": "entity_type", "new_value": event.entity_type},
            {"key": "scheduled_start_time", "new_value": start_time.isoformat()},
        ],
    )
    await _queue_scheduled_event_projection(
        session,
        settings,
        guild,
        auth.user,
        event,
        "create",
        rendered,
    )
    queue_postcommit_dispatch(
        session,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_SCHEDULED_EVENT_CREATE",
        rendered,
    )
    await session.commit()
    await publish_committed_dispatches(session, redis)
    await wake_queued_guild_federation(guild)
    return rendered


router.add_api_route(
    "/guilds/{guild_ref}/scheduled-events",
    create_scheduled_event,
    methods=["POST"],
    status_code=200,
)


async def get_scheduled_event(
    guild_ref: EntityRef,
    event_ref: EntityRef,
    with_user_count: bool = Query(default=False),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    proxied, result = await _proxy_human(
        session,
        settings,
        guild_ref,
        auth,
        "scheduled_event.get",
        {"resource_ref": str(event_ref), "with_user_count": with_user_count},
    )
    if proxied:
        return cast(dict[str, object], result.body if result is not None else None)
    guild = await local_guild(session, settings, guild_ref)
    event = await scheduled_event_for_guild(session, guild, event_ref)
    await _require_event_view(session, redis, guild, auth.user, event)
    return scheduled_event_payload(
        event,
        creator=await _creator(session, event),
        user_count=await _subscriber_count(session, event) if with_user_count else None,
        me_subscribed=await _viewer_subscribed(session, event, auth.user),
    )


router.add_api_route(
    "/guilds/{guild_ref}/scheduled-events/{event_ref}",
    get_scheduled_event,
    methods=["GET"],
)


async def patch_scheduled_event(
    guild_ref: EntityRef,
    event_ref: EntityRef,
    payload: ScheduledEventPatch,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    proxied, result = await _proxy_human(
        session,
        settings,
        guild_ref,
        auth,
        "scheduled_event.update",
        {
            "resource_ref": str(event_ref),
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "reason": reason,
        },
    )
    if proxied:
        return cast(dict[str, object], result.body if result is not None else None)
    guild = await local_guild(session, settings, guild_ref)
    event = await scheduled_event_for_guild(session, guild, event_ref, for_update=True)
    await _require_event_management(session, redis, guild, auth.user, event)
    if event.status in TERMINAL_STATUSES:
        raise _error(
            400,
            "SCHEDULED_EVENT_TERMINAL",
            "Completed and canceled events can no longer be changed.",
        )
    fields = payload.model_fields_set
    entity_type = payload.entity_type if "entity_type" in fields else event.entity_type
    if entity_type is None:
        raise RuntimeError("validated scheduled event patch lost its entity type")
    if "entity_type" in fields and entity_type != event.entity_type and event.status != SCHEDULED:
        raise _error(
            400,
            "SCHEDULED_EVENT_ENTITY_TYPE_LOCKED",
            "Only events that have not started can change their entity type.",
        )
    start_time = (
        _normalized_time(payload.scheduled_start_time, field_name="scheduled_start_time")
        if "scheduled_start_time" in fields and payload.scheduled_start_time is not None
        else event.scheduled_start_time
    )
    if "scheduled_start_time" in fields and start_time <= datetime.now(UTC):
        raise _error(
            400,
            "SCHEDULED_EVENT_START_TIME_INVALID",
            "A changed scheduled start time must be in the future.",
        )
    end_time = event.scheduled_end_time
    if "scheduled_end_time" in fields:
        end_time = (
            _normalized_time(payload.scheduled_end_time, field_name="scheduled_end_time")
            if payload.scheduled_end_time is not None
            else None
        )
    metadata = event.entity_metadata
    if "entity_metadata" in fields:
        metadata = (
            payload.entity_metadata.model_dump(mode="json")
            if payload.entity_metadata is not None
            else None
        )
    channel: Channel | None
    if entity_type in CHANNEL_EVENT_TYPES:
        if "channel_id" in fields:
            channel = await _event_channel(session, guild, entity_type, payload.channel_id)
        elif event.channel_id is not None and event.channel_domain is not None:
            channel = await _event_channel(
                session,
                guild,
                entity_type,
                EntityRef(f"{event.channel_id}@{event.channel_domain}"),
            )
        else:
            channel = None
    else:
        if "channel_id" in fields and payload.channel_id is not None:
            raise _error(
                400,
                "SCHEDULED_EVENT_ENTITY_FIELDS_INVALID",
                "External events must set channel_id to null.",
            )
        channel = None
    _validate_entity_fields(
        entity_type=entity_type,
        channel=channel,
        entity_metadata=metadata,
        scheduled_start_time=start_time,
        scheduled_end_time=end_time,
    )
    if channel is not None and (channel.id, channel.origin_domain) != (
        event.channel_id,
        event.channel_domain,
    ):
        own_event = (event.creator_id, event.creator_domain) == (
            auth.user.id,
            auth.user.origin_domain,
        )
        management_permissions = (
            Permission.CREATE_EVENTS | Permission.MANAGE_EVENTS
            if own_event
            else Permission.MANAGE_EVENTS
        )
        permissions = await get_permissions(session, redis, guild, auth.user, channel=channel)
        if not permissions & management_permissions:
            raise _error(
                403,
                "MISSING_PERMISSIONS",
                "You need permission to manage events in the new voice channel.",
            )
        if entity_type == STAGE_INSTANCE:
            await require_permissions(
                session,
                redis,
                guild,
                auth.user,
                STAGE_INSTANCE_MODERATOR_PERMISSIONS,
                channel=channel,
            )
        else:
            await require_permissions(
                session,
                redis,
                guild,
                auth.user,
                VOICE_CHANNEL_ACCESS_PERMISSIONS,
                channel=channel,
            )
    new_status = (
        payload.status if "status" in fields and payload.status is not None else event.status
    )
    if new_status != event.status and new_status not in VALID_STATUS_TRANSITIONS[event.status]:
        raise _error(
            400,
            "SCHEDULED_EVENT_STATUS_TRANSITION_INVALID",
            "Use SCHEDULED to ACTIVE to COMPLETED, or cancel an event before it starts.",
        )
    changes: list[dict[str, object]] = []
    assignments: dict[str, object] = {
        "name": payload.name if "name" in fields else event.name,
        "description": payload.description if "description" in fields else event.description,
        "privacy_level": (
            payload.privacy_level if "privacy_level" in fields else event.privacy_level
        ),
        "scheduled_start_time": start_time,
        "scheduled_end_time": end_time,
        "entity_type": entity_type,
        "entity_metadata": metadata,
        "recurrence_rule": (
            _recurrence_payload(
                payload.recurrence_rule,
                scheduled_start_time=start_time,
            )
            if "recurrence_rule" in fields
            else (
                {**event.recurrence_rule, "start": start_time.isoformat()}
                if event.recurrence_rule is not None and start_time != event.scheduled_start_time
                else event.recurrence_rule
            )
        ),
        "channel_id": channel.id if channel is not None else None,
        "channel_domain": channel.origin_domain if channel is not None else None,
        "status": new_status,
    }
    for field_name, new_value in assignments.items():
        old_value = getattr(event, field_name)
        if old_value != new_value:
            changes.append(
                {
                    "key": field_name,
                    "old_value": _audit_value(old_value),
                    "new_value": _audit_value(new_value),
                }
            )
            setattr(event, field_name, new_value)
    if not changes:
        return scheduled_event_payload(
            event,
            creator=await _creator(session, event),
            user_count=await _subscriber_count(session, event),
        )
    await materialize_updated_at(session, event)
    creator = await _creator(session, event)
    rendered = scheduled_event_payload(
        event,
        creator=creator,
        user_count=await _subscriber_count(session, event),
    )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        101,
        target_type="scheduled_event",
        target_ref=_event_ref(event),
        reason=reason,
        changes=changes,
    )
    await _queue_scheduled_event_projection(
        session,
        settings,
        guild,
        auth.user,
        event,
        "update",
        rendered,
    )
    queue_postcommit_dispatch(
        session,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_SCHEDULED_EVENT_UPDATE",
        rendered,
    )
    if event.status in TERMINAL_STATUSES and event.recurrence_rule is not None:
        if creator is None:
            raise RuntimeError("scheduled event creator is unavailable")
        await materialize_next_recurrence(
            session,
            settings,
            snowflake,
            guild=guild,
            event=event,
            creator=creator,
            channel=channel,
            after=datetime.now(UTC),
        )
    await session.commit()
    await publish_committed_dispatches(session, redis)
    await wake_queued_guild_federation(guild)
    return rendered


router.add_api_route(
    "/guilds/{guild_ref}/scheduled-events/{event_ref}",
    patch_scheduled_event,
    methods=["PATCH"],
)


async def delete_scheduled_event(
    guild_ref: EntityRef,
    event_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> Response:
    proxied, _ = await _proxy_human(
        session,
        settings,
        guild_ref,
        auth,
        "scheduled_event.delete",
        {"resource_ref": str(event_ref), "reason": reason},
    )
    if proxied:
        return Response(status_code=204)
    guild = await local_guild(session, settings, guild_ref)
    event = await scheduled_event_for_guild(session, guild, event_ref, for_update=True)
    await _require_event_management(session, redis, guild, auth.user, event)
    rendered = scheduled_event_payload(
        event,
        creator=await _creator(session, event),
        user_count=await _subscriber_count(session, event),
    )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        102,
        target_type="scheduled_event",
        target_ref=_event_ref(event),
        reason=reason,
    )
    await _queue_scheduled_event_projection(
        session,
        settings,
        guild,
        auth.user,
        event,
        "delete",
        rendered,
    )
    queue_postcommit_dispatch(
        session,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_SCHEDULED_EVENT_DELETE",
        rendered,
    )
    image_attachment = await session.scalar(
        select(Attachment)
        .where(Attachment.asset_binding == scheduled_event_image_binding(event))
        .with_for_update()
    )
    if image_attachment is not None:
        image_attachment.asset_binding = None
    await session.delete(event)
    await session.commit()
    await publish_committed_dispatches(session, redis)
    await wake_queued_guild_federation(guild)
    if image_attachment is not None:
        await enqueue_best_effort(
            media_local_purge,
            image_attachment.id,
            image_attachment.origin_domain,
        )
    return Response(status_code=204)


router.add_api_route(
    "/guilds/{guild_ref}/scheduled-events/{event_ref}",
    delete_scheduled_event,
    methods=["DELETE"],
    status_code=204,
)


async def list_scheduled_event_users(
    guild_ref: EntityRef,
    event_ref: EntityRef,
    limit: int = Query(default=100, ge=1, le=100),
    before: EntityRef | None = None,
    after: EntityRef | None = None,
    with_member: bool = Query(default=False),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    proxied, result = await _proxy_human(
        session,
        settings,
        guild_ref,
        auth,
        "scheduled_event.users",
        {
            "resource_ref": str(event_ref),
            "limit": limit,
            "before": str(before) if before is not None else None,
            "after": str(after) if after is not None else None,
            "with_member": with_member,
        },
    )
    if proxied:
        return cast(list[dict[str, object]], result.body if result is not None else None)
    guild = await local_guild(session, settings, guild_ref)
    event = await scheduled_event_for_guild(session, guild, event_ref)
    await _require_event_view(session, redis, guild, auth.user, event)
    conditions: list[ColumnElement[bool]] = [
        GuildScheduledEventSubscription.event_id == event.id,
        GuildScheduledEventSubscription.event_domain == event.origin_domain,
    ]
    descending = False
    # Discord gives `before` precedence when both cursors are supplied.
    if before is not None:
        descending = True
        conditions.append(
            tuple_(
                GuildScheduledEventSubscription.user_id,
                GuildScheduledEventSubscription.user_domain,
            )
            < before.resolve(settings.domain)
        )
    elif after is not None:
        conditions.append(
            tuple_(
                GuildScheduledEventSubscription.user_id,
                GuildScheduledEventSubscription.user_domain,
            )
            > after.resolve(settings.domain)
        )
    order = (
        (
            GuildScheduledEventSubscription.user_id.desc(),
            GuildScheduledEventSubscription.user_domain.desc(),
        )
        if descending
        else (
            GuildScheduledEventSubscription.user_id,
            GuildScheduledEventSubscription.user_domain,
        )
    )
    rows = list(
        (
            await session.execute(
                select(GuildScheduledEventSubscription, User, GuildMember)
                .join(
                    User,
                    (User.id == GuildScheduledEventSubscription.user_id)
                    & (User.origin_domain == GuildScheduledEventSubscription.user_domain),
                )
                .join(
                    GuildMember,
                    (GuildMember.guild_id == GuildScheduledEventSubscription.guild_id)
                    & (GuildMember.guild_domain == GuildScheduledEventSubscription.guild_domain)
                    & (GuildMember.user_id == GuildScheduledEventSubscription.user_id)
                    & (GuildMember.user_domain == GuildScheduledEventSubscription.user_domain),
                )
                .where(*conditions)
                .order_by(*order)
                .limit(limit)
            )
        ).all()
    )
    if descending:
        rows.reverse()
    role_ids: dict[tuple[int, str], list[int]] = {}
    if with_member and rows:
        user_refs = {(row.user_id, row.user_domain) for row, _, _ in rows}
        for user_id, user_domain, role_id in await session.execute(
            select(MemberRole.user_id, MemberRole.user_domain, MemberRole.role_id).where(
                MemberRole.guild_id == guild.id,
                MemberRole.guild_domain == guild.origin_domain,
                tuple_(MemberRole.user_id, MemberRole.user_domain).in_(user_refs),
            )
        ):
            role_ids.setdefault((user_id, user_domain), []).append(role_id)
    return [
        {
            "guild_scheduled_event_id": str(event.id),
            "guild_scheduled_event_domain": event.origin_domain,
            "user": user_payload(user),
            "member": (
                member_payload(
                    member,
                    user,
                    role_ids.get((user.id, user.origin_domain), []),
                )
                if with_member
                else None
            ),
            "subscribed_at": subscription.created_at.isoformat(),
        }
        for subscription, user, member in rows
    ]


router.add_api_route(
    "/guilds/{guild_ref}/scheduled-events/{event_ref}/users",
    list_scheduled_event_users,
    methods=["GET"],
)


@router.put(
    "/guilds/{guild_ref}/scheduled-events/{event_ref}/users/@me",
    status_code=204,
)
async def subscribe_scheduled_event(
    guild_ref: EntityRef,
    event_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    proxied, _ = await _proxy_human(
        session,
        settings,
        guild_ref,
        auth,
        "scheduled_event.subscribe",
        {"resource_ref": str(event_ref)},
    )
    if proxied:
        return Response(status_code=204)
    guild = await local_guild(session, settings, guild_ref)
    # Serialize the terminal-state check with start/cancel/delete transitions.
    # Without the row lock, a concurrent transition could commit first and this
    # request could still add a subscriber to an already terminal event.
    event = await scheduled_event_for_guild(session, guild, event_ref, for_update=True)
    await _require_event_view(session, redis, guild, auth.user, event)
    if event.status in TERMINAL_STATUSES:
        raise _error(
            400,
            "SCHEDULED_EVENT_TERMINAL",
            "Completed and canceled events no longer accept subscriptions.",
        )
    created = await session.scalar(
        insert(GuildScheduledEventSubscription)
        .values(
            event_id=event.id,
            event_domain=event.origin_domain,
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            user_id=auth.user.id,
            user_domain=auth.user.origin_domain,
        )
        .on_conflict_do_nothing()
        .returning(GuildScheduledEventSubscription.user_id)
    )
    if created is None:
        return Response(status_code=204)
    rendered: dict[str, object] = {
        "guild_scheduled_event_id": str(event.id),
        "guild_scheduled_event_domain": event.origin_domain,
        "user_id": str(auth.user.id),
        "user_domain": auth.user.origin_domain,
        "guild_id": str(guild.id),
        "guild_domain": guild.origin_domain,
    }
    await _queue_scheduled_event_subscription_projection(
        session,
        settings,
        guild,
        auth.user,
        event,
        "add",
        rendered,
    )
    queue_postcommit_dispatch(
        session,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_SCHEDULED_EVENT_USER_ADD",
        rendered,
    )
    await session.commit()
    await publish_committed_dispatches(session, redis)
    await wake_queued_guild_federation(guild)
    return Response(status_code=204)


@router.delete(
    "/guilds/{guild_ref}/scheduled-events/{event_ref}/users/@me",
    status_code=204,
)
async def unsubscribe_scheduled_event(
    guild_ref: EntityRef,
    event_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    proxied, _ = await _proxy_human(
        session,
        settings,
        guild_ref,
        auth,
        "scheduled_event.unsubscribe",
        {"resource_ref": str(event_ref)},
    )
    if proxied:
        return Response(status_code=204)
    guild = await local_guild(session, settings, guild_ref)
    event = await scheduled_event_for_guild(session, guild, event_ref, for_update=True)
    await _require_event_view(session, redis, guild, auth.user, event)
    removed = await session.scalar(
        delete(GuildScheduledEventSubscription)
        .where(
            GuildScheduledEventSubscription.event_id == event.id,
            GuildScheduledEventSubscription.event_domain == event.origin_domain,
            GuildScheduledEventSubscription.user_id == auth.user.id,
            GuildScheduledEventSubscription.user_domain == auth.user.origin_domain,
        )
        .returning(GuildScheduledEventSubscription.user_id)
    )
    if removed is None:
        return Response(status_code=204)
    rendered: dict[str, object] = {
        "guild_scheduled_event_id": str(event.id),
        "guild_scheduled_event_domain": event.origin_domain,
        "user_id": str(auth.user.id),
        "user_domain": auth.user.origin_domain,
        "guild_id": str(guild.id),
        "guild_domain": guild.origin_domain,
    }
    await _queue_scheduled_event_subscription_projection(
        session,
        settings,
        guild,
        auth.user,
        event,
        "remove",
        rendered,
    )
    queue_postcommit_dispatch(
        session,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_SCHEDULED_EVENT_USER_REMOVE",
        rendered,
    )
    await session.commit()
    await publish_committed_dispatches(session, redis)
    await wake_queued_guild_federation(guild)
    return Response(status_code=204)
