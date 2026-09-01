from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import ConfigDict, Field, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.bots import (
    installation_for_channel,
    installation_for_guild,
)
from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.automod.service import require_member_interactions_allowed
from app.bots.auth import BotPrincipal, require_bot
from app.bots.installations import (
    installation_allows_channel,
    installation_grants_permissions,
)
from app.chat.audit import add_audit_entry
from app.chat.events import guild_topic, publish_ephemeral
from app.chat.guild_revision import queue_guild_mutation, wake_queued_guild_federation
from app.chat.hierarchy import require_can_manage_member
from app.chat.permissions import (
    get_permissions,
    require_bot_channel_grant,
    require_permissions,
)
from app.chat.postcommit import publish_committed_dispatches, queue_postcommit_dispatch
from app.chat.schemas import RequestModel, cleaned_nonempty
from app.core.permissions import Permission
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef
from app.db.bot_models import BotInstallation
from app.db.materialization import materialize_updated_at
from app.db.models import Channel, Guild, GuildMember, GuildScheduledEvent, StageInstance, User
from app.federation.guild_management import (
    GuildManagementOperation,
    GuildManagementResult,
    proxy_remote_guild_management,
)
from app.scheduled_events.service import (
    event_creator,
    materialize_next_recurrence,
    scheduled_event_payload,
    subscriber_count,
)
from app.voice.permissions import (
    STAGE_INSTANCE_MODERATOR_PERMISSIONS,
    STAGE_INSTANCE_VIEW_PERMISSIONS,
    STAGE_VOICE_STATE_MODERATOR_PERMISSIONS,
    current_stage_voice_state_permissions,
    stage_voice_state_read_permissions,
)
from app.voice.rooms import guild_room_name, parse_room_name, participant_identity
from app.voice.schemas import CurrentUserVoiceStateUpdate, UserVoiceStateUpdate
from app.voice.service import (
    load_voice_channel,
    update_authoritative_occupant_grant,
    voice_speaking_allowed,
)
from app.voice.stage_lifecycle import (
    STAGE_END_MESSAGE,
    STAGE_SPEAKER_MESSAGE,
    STAGE_START_MESSAGE,
    STAGE_TOPIC_MESSAGE,
    persist_stage_system_message,
)
from app.voice.state import (
    Occupant,
    public_occupant_state,
    room_occupants,
    voice_user_room,
)

router = APIRouter(prefix="/api/v1", tags=["stage instances"])
bot_router = APIRouter(prefix="/api/v1/bots", tags=["bot stage instances"])

STAGE_CHANNEL_TYPE = 13
STAGE_ENTITY_TYPE = 1
STAGE_PRIVACY_GUILD_ONLY = 2
STAGE_CREATE_AUDIT_ACTION = 83
STAGE_UPDATE_AUDIT_ACTION = 84
STAGE_DELETE_AUDIT_ACTION = 85


class StageInstanceCreate(RequestModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: EntityRef
    topic: str = Field(min_length=1, max_length=120)
    privacy_level: Literal[2] = 2
    send_start_notification: bool = False
    guild_scheduled_event_id: EntityRef | None = None

    @field_validator("privacy_level", mode="before")
    @classmethod
    def strict_privacy_level(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Stage privacy_level must be an integer")
        return value

    @field_validator("topic")
    @classmethod
    def clean_topic(cls, value: str) -> str:
        return cleaned_nonempty(value)


class StageInstancePatch(RequestModel):
    model_config = ConfigDict(extra="forbid")

    topic: str | None = Field(default=None, min_length=1, max_length=120)
    privacy_level: Literal[2] | None = None

    @field_validator("privacy_level", mode="before")
    @classmethod
    def strict_privacy_level(cls, value: object) -> object:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError("Stage privacy_level must be an integer")
        return value

    @field_validator("topic")
    @classmethod
    def clean_topic(cls, value: str | None) -> str | None:
        return cleaned_nonempty(value) if value is not None else None

    @model_validator(mode="after")
    def has_change(self) -> StageInstancePatch:
        if not self.model_fields_set:
            raise ValueError("at least one Stage instance field is required")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Stage instance fields cannot be null")
        return self


def stage_instance_payload(instance: StageInstance) -> dict[str, object]:
    return {
        "id": str(instance.id),
        "origin_domain": instance.origin_domain,
        "guild_id": str(instance.guild_id),
        "guild_domain": instance.guild_domain,
        "channel_id": str(instance.channel_id),
        "channel_domain": instance.channel_domain,
        "topic": instance.topic,
        "privacy_level": instance.privacy_level,
        "discoverable_disabled": instance.discoverable_disabled,
        "guild_scheduled_event_id": (
            str(instance.scheduled_event_id) if instance.scheduled_event_id is not None else None
        ),
        "guild_scheduled_event_domain": instance.scheduled_event_domain,
    }


async def stage_channel_and_guild(
    session: AsyncSession,
    settings: Settings,
    actor: User,
    channel_ref: EntityRef,
) -> tuple[Channel, Guild]:
    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    channel = await session.get(Channel, (channel_id, channel_domain))
    if channel is None or channel.unavailable or channel.type != STAGE_CHANNEL_TYPE:
        raise HTTPException(status_code=404, detail={"code": "STAGE_CHANNEL_NOT_FOUND"})
    if channel.guild_id is None or channel.guild_domain is None:
        raise HTTPException(status_code=404, detail={"code": "STAGE_CHANNEL_NOT_FOUND"})
    guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
    member = (
        await session.get(
            GuildMember,
            (guild.id, guild.origin_domain, actor.id, actor.origin_domain),
        )
        if guild is not None
        else None
    )
    if guild is None or guild.unavailable or member is None:
        raise HTTPException(status_code=404, detail={"code": "STAGE_CHANNEL_NOT_FOUND"})
    return channel, guild


async def proxy_human_stage(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    actor: User,
    operation: GuildManagementOperation,
    payload: dict[str, Any],
) -> GuildManagementResult | None:
    return await proxy_remote_guild_management(
        session,
        settings,
        EntityRef(f"{guild.id}@{guild.origin_domain}"),
        actor,
        operation,
        payload,
    )


async def local_stage_instance(
    session: AsyncSession,
    settings: Settings,
    channel_ref: EntityRef,
    *,
    for_update: bool = False,
) -> tuple[StageInstance, Channel, Guild]:
    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    statement = select(StageInstance).where(
        StageInstance.channel_id == channel_id,
        StageInstance.channel_domain == channel_domain,
    )
    if for_update:
        statement = statement.with_for_update()
    instance = await session.scalar(statement)
    channel = await session.get(Channel, (channel_id, channel_domain))
    guild = (
        await session.get(Guild, (channel.guild_id, channel.guild_domain))
        if channel is not None and channel.guild_id is not None and channel.guild_domain is not None
        else None
    )
    if (
        instance is None
        or channel is None
        or channel.type != STAGE_CHANNEL_TYPE
        or guild is None
        or guild.origin_domain != settings.domain
    ):
        raise HTTPException(status_code=404, detail={"code": "STAGE_INSTANCE_NOT_FOUND"})
    return instance, channel, guild


async def create_local_stage_instance(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    actor: User,
    payload: StageInstanceCreate,
    *,
    reason: str | None,
) -> dict[str, object]:
    channel, guild = await stage_channel_and_guild(session, settings, actor, payload.channel_id)
    if guild.origin_domain != settings.domain:
        raise HTTPException(status_code=409, detail={"code": "STAGE_NOT_HOME"})
    permissions = await require_permissions(
        session,
        redis,
        guild,
        actor,
        STAGE_INSTANCE_MODERATOR_PERMISSIONS,
        channel=channel,
    )
    if payload.send_start_notification:
        if not permissions & Permission.MENTION_EVERYONE:
            raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
        await require_member_interactions_allowed(
            session,
            guild,
            actor,
            Permission.MENTION_EVERYONE,
        )
    locked_channel = await session.scalar(
        select(Channel)
        .where(
            Channel.id == channel.id,
            Channel.origin_domain == channel.origin_domain,
        )
        .with_for_update()
    )
    if locked_channel is None or locked_channel.unavailable:
        raise HTTPException(status_code=404, detail={"code": "STAGE_CHANNEL_NOT_FOUND"})
    channel = locked_channel
    existing = await session.scalar(
        select(StageInstance)
        .where(
            StageInstance.channel_id == channel.id,
            StageInstance.channel_domain == channel.origin_domain,
        )
        .with_for_update()
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail={"code": "STAGE_INSTANCE_EXISTS"})
    scheduled_event: GuildScheduledEvent | None = None
    if payload.guild_scheduled_event_id is not None:
        event_ref = payload.guild_scheduled_event_id.resolve(settings.domain)
        scheduled_event = await session.get(GuildScheduledEvent, event_ref, with_for_update=True)
        if (
            scheduled_event is None
            or (scheduled_event.guild_id, scheduled_event.guild_domain)
            != (guild.id, guild.origin_domain)
            or (scheduled_event.channel_id, scheduled_event.channel_domain)
            != (channel.id, channel.origin_domain)
            or scheduled_event.entity_type != STAGE_ENTITY_TYPE
            or scheduled_event.status in {3, 4}
            or scheduled_event.entity_id is not None
        ):
            raise HTTPException(
                status_code=400,
                detail={"code": "STAGE_SCHEDULED_EVENT_INVALID"},
            )
    instance = StageInstance(
        id=await snowflake.mint(),
        origin_domain=settings.domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        channel_type=STAGE_CHANNEL_TYPE,
        creator_id=actor.id,
        creator_domain=actor.origin_domain,
        topic=payload.topic,
        privacy_level=payload.privacy_level,
        discoverable_disabled=True,
        scheduled_event_id=scheduled_event.id if scheduled_event is not None else None,
        scheduled_event_domain=(
            scheduled_event.origin_domain if scheduled_event is not None else None
        ),
    )
    session.add(instance)
    await session.flush()
    if scheduled_event is not None:
        scheduled_event.entity_id = instance.id
        scheduled_event.entity_domain = instance.origin_domain
    rendered = stage_instance_payload(instance)
    await add_audit_entry(
        session,
        snowflake,
        guild,
        actor,
        STAGE_CREATE_AUDIT_ACTION,
        target_type="stage_instance",
        target_ref={"id": str(instance.id), "origin_domain": instance.origin_domain},
        reason=reason,
        changes=[{"key": "topic", "new_value": instance.topic}],
    )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.stage.instance.create",
        {
            "stage_instance": rendered,
            "send_start_notification": payload.send_start_notification,
        },
        channel=channel,
    )
    await persist_stage_system_message(
        session,
        settings,
        snowflake,
        guild=guild,
        channel=channel,
        author=actor,
        message_type=STAGE_START_MESSAGE,
        topic=instance.topic,
    )
    if scheduled_event is not None:
        scheduled_event.status = 2
        await materialize_updated_at(session, scheduled_event)
        scheduled_rendered = scheduled_event_payload(
            scheduled_event,
            creator=await event_creator(session, scheduled_event),
            user_count=await subscriber_count(session, scheduled_event),
        )
        await queue_guild_mutation(
            session,
            settings,
            guild,
            actor,
            "guild.scheduled_event.update",
            {"scheduled_event": scheduled_rendered},
            channel=channel,
        )
        queue_postcommit_dispatch(
            session,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_SCHEDULED_EVENT_UPDATE",
            scheduled_rendered,
        )
    queue_postcommit_dispatch(
        session,
        guild_topic(guild.origin_domain, guild.id),
        "STAGE_INSTANCE_CREATE",
        {
            **rendered,
            "send_start_notification": payload.send_start_notification,
            **(
                {
                    "notification_id": str(instance.id),
                    "notification_author": {
                        "id": str(actor.id),
                        "origin_domain": actor.origin_domain,
                    },
                }
                if payload.send_start_notification
                else {}
            ),
        },
    )
    await session.commit()
    await publish_committed_dispatches(session, redis)
    await wake_queued_guild_federation(guild)
    return rendered


@router.post("/stage-instances")
async def create_stage_instance(
    payload: StageInstanceCreate,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> dict[str, object]:
    _, guild = await stage_channel_and_guild(session, settings, auth.user, payload.channel_id)
    proxied = await proxy_human_stage(
        session,
        settings,
        guild,
        auth.user,
        "stage_instance.create",
        {
            "data": payload.model_dump(mode="json"),
            "reason": reason,
        },
    )
    if proxied is not None:
        return dict(proxied.body) if isinstance(proxied.body, dict) else {}
    return await create_local_stage_instance(
        session, redis, settings, snowflake, auth.user, payload, reason=reason
    )


@router.get("/stage-instances/{channel_ref}")
async def get_stage_instance(
    channel_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    channel, guild = await stage_channel_and_guild(session, settings, auth.user, channel_ref)
    proxied = await proxy_human_stage(
        session,
        settings,
        guild,
        auth.user,
        "stage_instance.get",
        {"channel_id": f"{channel.id}@{channel.origin_domain}"},
    )
    if proxied is not None:
        return dict(proxied.body) if isinstance(proxied.body, dict) else {}
    await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        STAGE_INSTANCE_VIEW_PERMISSIONS,
        channel=channel,
    )
    instance, _, _ = await local_stage_instance(session, settings, channel_ref)
    return stage_instance_payload(instance)


async def patch_local_stage_instance(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    actor: User,
    channel_ref: EntityRef,
    payload: StageInstancePatch,
    *,
    reason: str | None,
) -> dict[str, object]:
    instance, channel, guild = await local_stage_instance(
        session, settings, channel_ref, for_update=True
    )
    await require_permissions(
        session,
        redis,
        guild,
        actor,
        STAGE_INSTANCE_MODERATOR_PERMISSIONS,
        channel=channel,
    )
    changes: list[dict[str, object]] = []
    previous_topic = instance.topic
    if payload.topic is not None and payload.topic != instance.topic:
        changes.append({"key": "topic", "old_value": instance.topic, "new_value": payload.topic})
        instance.topic = payload.topic
    if payload.privacy_level is not None and payload.privacy_level != instance.privacy_level:
        changes.append(
            {
                "key": "privacy_level",
                "old_value": instance.privacy_level,
                "new_value": payload.privacy_level,
            }
        )
        instance.privacy_level = payload.privacy_level
    rendered = stage_instance_payload(instance)
    if not changes:
        return rendered
    await add_audit_entry(
        session,
        snowflake,
        guild,
        actor,
        STAGE_UPDATE_AUDIT_ACTION,
        target_type="stage_instance",
        target_ref={"id": str(instance.id), "origin_domain": instance.origin_domain},
        reason=reason,
        changes=changes,
    )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.stage.instance.update",
        {"stage_instance": rendered},
        channel=channel,
    )
    if instance.topic != previous_topic:
        await persist_stage_system_message(
            session,
            settings,
            snowflake,
            guild=guild,
            channel=channel,
            author=actor,
            message_type=STAGE_TOPIC_MESSAGE,
            topic=instance.topic,
        )
    queue_postcommit_dispatch(
        session,
        guild_topic(guild.origin_domain, guild.id),
        "STAGE_INSTANCE_UPDATE",
        rendered,
    )
    await session.commit()
    await publish_committed_dispatches(session, redis)
    await wake_queued_guild_federation(guild)
    return rendered


@router.patch("/stage-instances/{channel_ref}")
async def patch_stage_instance(
    channel_ref: EntityRef,
    payload: StageInstancePatch,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> dict[str, object]:
    channel, guild = await stage_channel_and_guild(session, settings, auth.user, channel_ref)
    proxied = await proxy_human_stage(
        session,
        settings,
        guild,
        auth.user,
        "stage_instance.update",
        {
            "channel_id": f"{channel.id}@{channel.origin_domain}",
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "reason": reason,
        },
    )
    if proxied is not None:
        return dict(proxied.body) if isinstance(proxied.body, dict) else {}
    return await patch_local_stage_instance(
        session,
        redis,
        settings,
        snowflake,
        auth.user,
        channel_ref,
        payload,
        reason=reason,
    )


async def delete_local_stage_instance(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    actor: User,
    channel_ref: EntityRef,
    *,
    reason: str | None,
) -> None:
    instance, channel, guild = await local_stage_instance(
        session, settings, channel_ref, for_update=True
    )
    await require_permissions(
        session,
        redis,
        guild,
        actor,
        STAGE_INSTANCE_MODERATOR_PERMISSIONS,
        channel=channel,
    )
    rendered = stage_instance_payload(instance)
    await persist_stage_system_message(
        session,
        settings,
        snowflake,
        guild=guild,
        channel=channel,
        author=actor,
        message_type=STAGE_END_MESSAGE,
        topic=instance.topic,
    )
    if instance.scheduled_event_id is not None and instance.scheduled_event_domain is not None:
        event = await session.get(
            GuildScheduledEvent,
            (instance.scheduled_event_id, instance.scheduled_event_domain),
            with_for_update=True,
        )
        if event is not None and (event.entity_id, event.entity_domain) == (
            instance.id,
            instance.origin_domain,
        ):
            event.entity_id = None
            event.entity_domain = None
            completed_event = event.status == 2
            if completed_event:
                event.status = 3
            await materialize_updated_at(session, event)
            creator = await event_creator(session, event)
            scheduled_rendered = scheduled_event_payload(
                event,
                creator=creator,
                user_count=await subscriber_count(session, event),
            )
            await queue_guild_mutation(
                session,
                settings,
                guild,
                actor,
                "guild.scheduled_event.update",
                {"scheduled_event": scheduled_rendered},
                channel=channel,
            )
            queue_postcommit_dispatch(
                session,
                guild_topic(guild.origin_domain, guild.id),
                "GUILD_SCHEDULED_EVENT_UPDATE",
                scheduled_rendered,
            )
            if completed_event and event.recurrence_rule is not None:
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
    await add_audit_entry(
        session,
        snowflake,
        guild,
        actor,
        STAGE_DELETE_AUDIT_ACTION,
        target_type="stage_instance",
        target_ref={"id": str(instance.id), "origin_domain": instance.origin_domain},
        reason=reason,
        changes=[{"key": "topic", "old_value": instance.topic}],
    )
    await session.delete(instance)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.stage.instance.delete",
        {"stage_instance": rendered},
        channel=channel,
    )
    queue_postcommit_dispatch(
        session,
        guild_topic(guild.origin_domain, guild.id),
        "STAGE_INSTANCE_DELETE",
        rendered,
    )
    await session.commit()
    await publish_committed_dispatches(session, redis)
    await wake_queued_guild_federation(guild)


@router.delete("/stage-instances/{channel_ref}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_stage_instance(
    channel_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    channel, guild = await stage_channel_and_guild(session, settings, auth.user, channel_ref)
    proxied = await proxy_human_stage(
        session,
        settings,
        guild,
        auth.user,
        "stage_instance.delete",
        {
            "channel_id": f"{channel.id}@{channel.origin_domain}",
            "reason": reason,
        },
    )
    if proxied is None:
        await delete_local_stage_instance(
            session,
            redis,
            settings,
            snowflake,
            auth.user,
            channel_ref,
            reason=reason,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def stage_voice_state_payload(
    occupant: Occupant,
    authority_domain: str,
) -> dict[str, object]:
    """Render a Discord-like voice state without exposing connection authority."""

    public = public_occupant_state(occupant)
    session_seed = (
        occupant.connection_id or f"{occupant.identity}:{occupant.room}:{occupant.joined_at}"
    )
    return {
        **public,
        "guild_domain": authority_domain,
        "channel_domain": authority_domain,
        "session_id": hashlib.sha256(session_seed.encode()).hexdigest(),
        "deaf": occupant.server_deaf,
        "mute": occupant.server_mute,
        "self_stream": False,
        "self_video": False,
        "suppress": occupant.suppressed,
    }


async def stage_guild_for_actor(
    session: AsyncSession,
    settings: Settings,
    guild_ref: EntityRef,
    actor: User,
) -> Guild:
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    guild = await session.get(Guild, (guild_id, guild_domain))
    member = await session.get(
        GuildMember,
        (guild_id, guild_domain, actor.id, actor.origin_domain),
    )
    if guild is None or guild.unavailable or member is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return guild


async def connected_stage_occupant(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild: Guild,
    target_ref: EntityRef,
    channel_ref: EntityRef | None,
) -> tuple[Occupant, Channel, User]:
    if guild.origin_domain != settings.domain:
        raise HTTPException(status_code=409, detail={"code": "VOICE_NOT_HOME"})
    target_id, target_domain = target_ref.resolve(settings.domain)
    target = await session.get(User, (target_id, target_domain))
    membership = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, target_id, target_domain),
    )
    if target is None or membership is None:
        raise HTTPException(status_code=404, detail={"code": "VOICE_STATE_NOT_FOUND"})
    identity = participant_identity(target_id, target_domain)
    room = await voice_user_room(redis, settings.domain, identity, guild_id=guild.id)
    if room is None:
        raise HTTPException(status_code=404, detail={"code": "VOICE_STATE_NOT_FOUND"})
    try:
        kind, room_guild_id, room_channel_id = parse_room_name(room)
    except ValueError:
        raise HTTPException(status_code=404, detail={"code": "VOICE_STATE_NOT_FOUND"}) from None
    if kind != "g" or room_guild_id != guild.id:
        raise HTTPException(status_code=404, detail={"code": "VOICE_STATE_NOT_FOUND"})
    if channel_ref is not None:
        requested_channel = channel_ref.resolve(settings.domain)
        if requested_channel != (room_channel_id, settings.domain):
            raise HTTPException(status_code=400, detail={"code": "VOICE_CHANNEL_MISMATCH"})
    channel, voice_guild = await load_voice_channel(
        session,
        room_channel_id,
        settings.domain,
    )
    if channel.type != STAGE_CHANNEL_TYPE or (voice_guild.id, voice_guild.origin_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        raise HTTPException(status_code=400, detail={"code": "STAGE_CHANNEL_REQUIRED"})
    occupant = next(
        (
            item
            for item in await room_occupants(redis, settings.domain, room)
            if item.identity == identity
        ),
        None,
    )
    if occupant is None:
        raise HTTPException(status_code=404, detail={"code": "VOICE_STATE_NOT_FOUND"})
    return occupant, channel, target


async def get_local_stage_voice_state(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild_ref: EntityRef,
    actor: User,
    target_ref: EntityRef,
    *,
    acting_installation: BotInstallation | None = None,
) -> dict[str, object]:
    guild = await stage_guild_for_actor(session, settings, guild_ref, actor)
    occupant, channel, _ = await connected_stage_occupant(
        session,
        redis,
        settings,
        guild,
        target_ref,
        None,
    )
    if acting_installation is None:
        await require_bot_channel_grant(session, guild, actor, channel)
    elif (acting_installation.guild_id, acting_installation.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ) or not await installation_allows_channel(session, acting_installation, channel):
        raise HTTPException(status_code=404, detail={"code": "VOICE_STATE_NOT_FOUND"})
    required = stage_voice_state_read_permissions(
        actor_id=actor.id,
        actor_domain=actor.origin_domain,
        target_ref=target_ref,
        default_domain=settings.domain,
    )
    if required:
        await require_permissions(
            session,
            redis,
            guild,
            actor,
            required,
            channel=channel,
        )
    return stage_voice_state_payload(occupant, guild.origin_domain)


async def update_local_stage_voice_state(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild_ref: EntityRef,
    actor: User,
    target_ref: EntityRef,
    payload: CurrentUserVoiceStateUpdate | UserVoiceStateUpdate,
    *,
    current_user: bool,
    snowflake: SnowflakeGenerator | None = None,
    acting_installation: BotInstallation | None = None,
) -> dict[str, object]:
    guild = await stage_guild_for_actor(session, settings, guild_ref, actor)
    occupant, channel, target = await connected_stage_occupant(
        session,
        redis,
        settings,
        guild,
        target_ref,
        payload.channel_id,
    )
    if acting_installation is None:
        await require_bot_channel_grant(session, guild, actor, channel)
    elif (acting_installation.guild_id, acting_installation.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ) or not await installation_allows_channel(session, acting_installation, channel):
        raise HTTPException(status_code=403, detail={"code": "BOT_CHANNEL_RESTRICTED"})
    if current_user:
        if (target.id, target.origin_domain) != (actor.id, actor.origin_domain):
            raise HTTPException(status_code=403, detail={"code": "VOICE_ACTOR_MISMATCH"})
        current_payload = CurrentUserVoiceStateUpdate.model_validate(
            payload.model_dump(mode="json", exclude_unset=True)
        )
        if current_payload.suppress is False:
            await require_permissions(
                session,
                redis,
                guild,
                actor,
                STAGE_VOICE_STATE_MODERATOR_PERMISSIONS,
                channel=channel,
            )
        if current_payload.request_to_speak_timestamp is not None:
            await require_permissions(
                session,
                redis,
                guild,
                actor,
                Permission.REQUEST_TO_SPEAK,
                channel=channel,
            )
            requested_at = datetime.fromisoformat(current_payload.request_to_speak_timestamp)
            if requested_at < datetime.now(UTC) - timedelta(
                seconds=settings.federation_clock_skew_seconds
            ):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "REQUEST_TO_SPEAK_TIMESTAMP_PAST"},
                )
        suppressed = (
            occupant.suppressed
            if "suppress" not in current_payload.model_fields_set
            else bool(current_payload.suppress)
        )
        update_request = "request_to_speak_timestamp" in current_payload.model_fields_set
        request_timestamp = current_payload.request_to_speak_timestamp
        if "suppress" in current_payload.model_fields_set:
            update_request = True
            request_timestamp = None
    else:
        user_payload = UserVoiceStateUpdate.model_validate(
            payload.model_dump(mode="json", exclude_unset=True)
        )
        await require_permissions(
            session,
            redis,
            guild,
            actor,
            STAGE_VOICE_STATE_MODERATOR_PERMISSIONS,
            channel=channel,
        )
        await require_can_manage_member(
            session,
            guild,
            actor,
            target.id,
            target.origin_domain,
        )
        suppressed = user_payload.suppress
        update_request = True
        request_timestamp = (
            datetime.now(UTC).isoformat()
            if not suppressed and target.account_type != "bot"
            else None
        )
    target_permissions = await get_permissions(
        session,
        redis,
        guild,
        target,
        channel=channel,
    )
    can_speak = (
        occupant.allow_speak
        and voice_speaking_allowed(channel.type, Permission(target_permissions))
        and not occupant.server_mute
        and not suppressed
    )
    can_stream = (
        occupant.allow_stream and bool(target_permissions & Permission.STREAM) and not suppressed
    )
    updated = await update_authoritative_occupant_grant(
        redis,
        settings,
        occupant,
        can_speak=can_speak,
        can_stream=can_stream,
        can_priority_speak=False,
        suppressed=suppressed,
        request_to_speak_timestamp=request_timestamp,
        update_request_timestamp=update_request,
    )
    rendered = stage_voice_state_payload(updated, guild.origin_domain)
    if occupant.suppressed and not updated.suppressed:
        stage = await session.scalar(
            select(StageInstance).where(
                StageInstance.channel_id == channel.id,
                StageInstance.channel_domain == channel.origin_domain,
            )
        )
        if stage is not None:
            if snowflake is None:
                raise RuntimeError("Stage speaker transitions require a snowflake generator")
            await persist_stage_system_message(
                session,
                settings,
                snowflake,
                guild=guild,
                channel=channel,
                author=target,
                message_type=STAGE_SPEAKER_MESSAGE,
                topic=stage.topic,
            )
            await session.commit()
            await publish_committed_dispatches(session, redis)
            await wake_queued_guild_federation(guild)
    await publish_ephemeral(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "VOICE_STATE_UPDATE",
        {**rendered, "connected": True, "state": public_occupant_state(updated)},
    )
    from app.tasks import voice_replicate_room

    await enqueue_best_effort(voice_replicate_room, guild_room_name(guild.id, channel.id))
    return rendered


async def proxy_stage_voice_state(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    actor: User,
    operation: GuildManagementOperation,
    payload: dict[str, Any],
) -> GuildManagementResult | None:
    return await proxy_human_stage(
        session,
        settings,
        guild,
        actor,
        operation,
        payload,
    )


@router.get("/guilds/{guild_ref}/voice-states/@me")
async def get_current_user_stage_voice_state(
    guild_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild = await stage_guild_for_actor(session, settings, guild_ref, auth.user)
    target_ref = EntityRef(f"{auth.user.id}@{auth.user.origin_domain}")
    proxied = await proxy_stage_voice_state(
        session,
        settings,
        guild,
        auth.user,
        "stage_voice_state.get",
        {"user_ref": str(target_ref)},
    )
    if proxied is not None:
        return dict(proxied.body) if isinstance(proxied.body, dict) else {}
    return await get_local_stage_voice_state(
        session,
        redis,
        settings,
        guild_ref,
        auth.user,
        target_ref,
    )


@router.get("/guilds/{guild_ref}/voice-states/{user_ref}")
async def get_user_stage_voice_state(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild = await stage_guild_for_actor(session, settings, guild_ref, auth.user)
    user_id, user_domain = user_ref.resolve(settings.domain)
    qualified_user_ref = EntityRef(f"{user_id}@{user_domain}")
    proxied = await proxy_stage_voice_state(
        session,
        settings,
        guild,
        auth.user,
        "stage_voice_state.get",
        {"user_ref": str(qualified_user_ref)},
    )
    if proxied is not None:
        return dict(proxied.body) if isinstance(proxied.body, dict) else {}
    return await get_local_stage_voice_state(
        session,
        redis,
        settings,
        guild_ref,
        auth.user,
        qualified_user_ref,
    )


@router.patch(
    "/guilds/{guild_ref}/voice-states/@me",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def update_current_user_stage_voice_state(
    guild_ref: EntityRef,
    payload: CurrentUserVoiceStateUpdate,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    guild = await stage_guild_for_actor(session, settings, guild_ref, auth.user)
    target_ref = EntityRef(f"{auth.user.id}@{auth.user.origin_domain}")
    proxied = await proxy_stage_voice_state(
        session,
        settings,
        guild,
        auth.user,
        "stage_voice_state.self",
        {"data": payload.model_dump(mode="json", exclude_unset=True)},
    )
    if proxied is None:
        await update_local_stage_voice_state(
            session,
            redis,
            settings,
            guild_ref,
            auth.user,
            target_ref,
            payload,
            current_user=True,
            snowflake=snowflake,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/guilds/{guild_ref}/voice-states/{user_ref}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def update_user_stage_voice_state(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    payload: UserVoiceStateUpdate,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    guild = await stage_guild_for_actor(session, settings, guild_ref, auth.user)
    user_id, user_domain = user_ref.resolve(settings.domain)
    qualified_user_ref = EntityRef(f"{user_id}@{user_domain}")
    proxied = await proxy_stage_voice_state(
        session,
        settings,
        guild,
        auth.user,
        "stage_voice_state.user",
        {
            "user_ref": str(qualified_user_ref),
            "data": payload.model_dump(mode="json", exclude_unset=True),
        },
    )
    if proxied is None:
        await update_local_stage_voice_state(
            session,
            redis,
            settings,
            guild_ref,
            auth.user,
            qualified_user_ref,
            payload,
            current_user=False,
            snowflake=snowflake,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def require_stage_installation_permissions(
    installation: BotInstallation,
    required: Permission,
) -> None:
    """Enforce the selected bot installation's Stage permission ceiling."""

    if installation_grants_permissions(installation.granted_permissions, required):
        return
    raise HTTPException(
        status_code=403,
        detail={
            "code": "MISSING_PERMISSIONS",
            "permissions": str(int(required)),
        },
    )


async def bot_stage_context(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    channel_ref: EntityRef,
    scope: str,
) -> tuple[Channel, Guild]:
    channel, installation = await installation_for_channel(
        session, settings, principal, channel_ref, scope
    )
    if (
        not isinstance(installation, BotInstallation)
        or channel.type != STAGE_CHANNEL_TYPE
        or channel.guild_id is None
    ):
        raise HTTPException(status_code=404, detail={"code": "STAGE_CHANNEL_NOT_FOUND"})
    guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return channel, guild


@bot_router.get("/guilds/{guild_ref}/voice-states/@me")
async def bot_get_current_stage_voice_state(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild, installation = await installation_for_guild(
        session,
        settings,
        principal,
        guild_ref,
        "voice.states.read",
    )
    target_ref = EntityRef(f"{principal.user.id}@{principal.user.origin_domain}")
    proxied = await proxy_stage_voice_state(
        session,
        settings,
        guild,
        principal.user,
        "stage_voice_state.get",
        {"user_ref": str(target_ref)},
    )
    if proxied is not None:
        return dict(proxied.body) if isinstance(proxied.body, dict) else {}
    return await get_local_stage_voice_state(
        session,
        redis,
        settings,
        guild_ref,
        principal.user,
        target_ref,
        acting_installation=installation,
    )


@bot_router.get("/guilds/{guild_ref}/voice-states/{user_ref}")
async def bot_get_stage_voice_state(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild, installation = await installation_for_guild(
        session,
        settings,
        principal,
        guild_ref,
        "voice.states.read",
    )
    user_id, user_domain = user_ref.resolve(settings.domain)
    qualified_user_ref = EntityRef(f"{user_id}@{user_domain}")
    require_stage_installation_permissions(
        installation,
        stage_voice_state_read_permissions(
            actor_id=principal.user.id,
            actor_domain=principal.user.origin_domain,
            target_ref=qualified_user_ref,
            default_domain=settings.domain,
        ),
    )
    proxied = await proxy_stage_voice_state(
        session,
        settings,
        guild,
        principal.user,
        "stage_voice_state.get",
        {"user_ref": str(qualified_user_ref)},
    )
    if proxied is not None:
        return dict(proxied.body) if isinstance(proxied.body, dict) else {}
    return await get_local_stage_voice_state(
        session,
        redis,
        settings,
        guild_ref,
        principal.user,
        qualified_user_ref,
        acting_installation=installation,
    )


@bot_router.patch(
    "/guilds/{guild_ref}/voice-states/@me",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def bot_update_current_stage_voice_state(
    guild_ref: EntityRef,
    payload: CurrentUserVoiceStateUpdate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    guild, installation = await installation_for_guild(
        session,
        settings,
        principal,
        guild_ref,
        "voice.connect",
    )
    require_stage_installation_permissions(
        installation,
        current_stage_voice_state_permissions(payload),
    )
    target_ref = EntityRef(f"{principal.user.id}@{principal.user.origin_domain}")
    proxied = await proxy_stage_voice_state(
        session,
        settings,
        guild,
        principal.user,
        "stage_voice_state.self",
        {"data": payload.model_dump(mode="json", exclude_unset=True)},
    )
    if proxied is None:
        await update_local_stage_voice_state(
            session,
            redis,
            settings,
            guild_ref,
            principal.user,
            target_ref,
            payload,
            current_user=True,
            snowflake=snowflake,
            acting_installation=installation,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@bot_router.patch(
    "/guilds/{guild_ref}/voice-states/{user_ref}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def bot_update_stage_voice_state(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    payload: UserVoiceStateUpdate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    guild, installation = await installation_for_guild(
        session,
        settings,
        principal,
        guild_ref,
        "voice.moderate",
    )
    require_stage_installation_permissions(
        installation,
        STAGE_VOICE_STATE_MODERATOR_PERMISSIONS,
    )
    user_id, user_domain = user_ref.resolve(settings.domain)
    qualified_user_ref = EntityRef(f"{user_id}@{user_domain}")
    proxied = await proxy_stage_voice_state(
        session,
        settings,
        guild,
        principal.user,
        "stage_voice_state.user",
        {
            "user_ref": str(qualified_user_ref),
            "data": payload.model_dump(mode="json", exclude_unset=True),
        },
    )
    if proxied is None:
        await update_local_stage_voice_state(
            session,
            redis,
            settings,
            guild_ref,
            principal.user,
            qualified_user_ref,
            payload,
            current_user=False,
            snowflake=snowflake,
            acting_installation=installation,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@bot_router.post("/stage-instances")
async def bot_create_stage_instance(
    payload: StageInstanceCreate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    _, guild = await bot_stage_context(
        session, settings, principal, payload.channel_id, "channels.manage"
    )
    proxied = await proxy_human_stage(
        session,
        settings,
        guild,
        principal.user,
        "stage_instance.create",
        {
            "data": payload.model_dump(mode="json"),
            "reason": reason,
        },
    )
    if proxied is not None:
        return dict(proxied.body) if isinstance(proxied.body, dict) else {}
    return await create_local_stage_instance(
        session, redis, settings, snowflake, principal.user, payload, reason=reason
    )


@bot_router.get("/stage-instances/{channel_ref}")
async def bot_get_stage_instance(
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    channel, guild = await bot_stage_context(
        session, settings, principal, channel_ref, "channels.read"
    )
    proxied = await proxy_human_stage(
        session,
        settings,
        guild,
        principal.user,
        "stage_instance.get",
        {"channel_id": f"{channel.id}@{channel.origin_domain}"},
    )
    if proxied is not None:
        return dict(proxied.body) if isinstance(proxied.body, dict) else {}
    await require_permissions(
        session,
        redis,
        guild,
        principal.user,
        STAGE_INSTANCE_VIEW_PERMISSIONS,
        channel=channel,
    )
    instance, _, _ = await local_stage_instance(session, settings, channel_ref)
    return stage_instance_payload(instance)


@bot_router.patch("/stage-instances/{channel_ref}")
async def bot_patch_stage_instance(
    channel_ref: EntityRef,
    payload: StageInstancePatch,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    channel, guild = await bot_stage_context(
        session, settings, principal, channel_ref, "channels.manage"
    )
    proxied = await proxy_human_stage(
        session,
        settings,
        guild,
        principal.user,
        "stage_instance.update",
        {
            "channel_id": f"{channel.id}@{channel.origin_domain}",
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "reason": reason,
        },
    )
    if proxied is not None:
        return dict(proxied.body) if isinstance(proxied.body, dict) else {}
    return await patch_local_stage_instance(
        session,
        redis,
        settings,
        snowflake,
        principal.user,
        channel_ref,
        payload,
        reason=reason,
    )


@bot_router.delete("/stage-instances/{channel_ref}", status_code=status.HTTP_204_NO_CONTENT)
async def bot_delete_stage_instance(
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> Response:
    channel, guild = await bot_stage_context(
        session, settings, principal, channel_ref, "channels.manage"
    )
    proxied = await proxy_human_stage(
        session,
        settings,
        guild,
        principal.user,
        "stage_instance.delete",
        {
            "channel_id": f"{channel.id}@{channel.origin_domain}",
            "reason": reason,
        },
    )
    if proxied is None:
        await delete_local_stage_instance(
            session,
            redis,
            settings,
            snowflake,
            principal.user,
            channel_ref,
            reason=reason,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
