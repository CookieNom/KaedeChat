from __future__ import annotations

from contextlib import suppress

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.audit import add_audit_entry, normalize_audit_reason
from app.chat.events import guild_topic, publish_dispatch
from app.chat.guild_revision import queue_guild_mutation, wake_queued_guild_federation
from app.chat.permissions import require_permissions
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.snowflake import SnowflakeGenerator
from app.db.models import Channel, Guild, User
from app.voice.channel_info import (
    voice_channel_status,
    voice_channel_status_key,
    voice_channel_update_payload,
)
from app.voice.rooms import guild_room_name, participant_identity
from app.voice.state import voice_user_room


async def set_voice_channel_status(
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    guild: Guild,
    channel: Channel,
    actor: User,
    status: str | None,
    *,
    reason: str | None = None,
) -> dict[str, object]:
    """Apply Discord's dedicated, ordinary-voice-only status mutation."""

    if channel.type != 2:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail={"code": "VOICE_STATUS_VOICE_ONLY"})
    identity = participant_identity(actor.id, actor.origin_domain)
    current_room = await voice_user_room(
        redis,
        guild.origin_domain,
        identity,
        guild_id=guild.id,
    )
    connected = current_room == guild_room_name(guild.id, channel.id)
    required = Permission.SET_VOICE_CHANNEL_STATUS
    if not connected:
        required |= Permission.MANAGE_CHANNELS
    await require_permissions(session, redis, guild, actor, required, channel=channel)

    normalized = status.strip() if status is not None else None
    normalized = normalized or None
    previous = await voice_channel_status(
        redis,
        guild.origin_domain,
        guild.id,
        channel.id,
    )
    if previous == normalized:
        payload = voice_channel_status_payload(guild, channel, normalized)
        # A prior committed request may have lost only its best-effort local
        # Gateway publish. Replaying an idempotent PUT repairs that projection.
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "VOICE_CHANNEL_STATUS_UPDATE",
            payload,
        )
        return payload
    status_key = voice_channel_status_key(
        guild.origin_domain,
        guild.id,
        channel.id,
    )
    if normalized is None:
        await redis.delete(status_key)
    else:
        await redis.set(status_key, normalized)
    try:
        await queue_guild_mutation(
            session,
            settings,
            guild,
            actor,
            "guild.voice_channel_status.update",
            {
                "channel_id": str(channel.id),
                "channel_domain": channel.origin_domain,
                "status": normalized,
            },
            channel=channel,
        )
        await add_audit_entry(
            session,
            snowflake,
            guild,
            actor,
            193 if normalized is None else 192,
            target_type="channel",
            target_ref={"id": str(channel.id)},
            changes=[{"key": "status", "old_value": previous, "new_value": normalized}],
            reason=normalize_audit_reason(reason),
        )
        await session.commit()
    except Exception:
        with suppress(Exception):
            await session.rollback()
        with suppress(Exception):
            if previous is None:
                await redis.delete(status_key)
            else:
                await redis.set(status_key, previous)
        raise
    await wake_queued_guild_federation(guild)
    payload = voice_channel_status_payload(guild, channel, normalized)
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "VOICE_CHANNEL_STATUS_UPDATE",
        payload,
    )
    return payload


def voice_channel_status_payload(
    guild: Guild,
    channel: Channel,
    status: str | None,
) -> dict[str, object]:
    return voice_channel_update_payload(guild, channel, "status", status)


async def clear_voice_channel_status_after_room_end(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild: Guild,
    channel: Channel,
) -> bool:
    """Clear ephemeral status when its voice session ends, without a user audit row."""

    previous = await voice_channel_status(
        redis,
        guild.origin_domain,
        guild.id,
        channel.id,
    )
    if previous is None:
        return False
    await redis.delete(voice_channel_status_key(guild.origin_domain, guild.id, channel.id))
    from app.chat.guild_revision import guild_authority_owner

    owner = await guild_authority_owner(session, settings, guild, for_update=False)
    try:
        await queue_guild_mutation(
            session,
            settings,
            guild,
            owner,
            "guild.voice_channel_status.update",
            {
                "channel_id": str(channel.id),
                "channel_domain": channel.origin_domain,
                "status": None,
            },
            channel=channel,
            pause_e2ee=False,
        )
        await session.commit()
    except Exception:
        with suppress(Exception):
            await session.rollback()
        with suppress(Exception):
            await redis.set(
                voice_channel_status_key(guild.origin_domain, guild.id, channel.id),
                previous,
            )
        raise
    await wake_queued_guild_federation(guild)
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "VOICE_CHANNEL_STATUS_UPDATE",
        voice_channel_status_payload(guild, channel, None),
    )
    return True
