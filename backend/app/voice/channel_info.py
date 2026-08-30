from __future__ import annotations

from collections.abc import Iterable

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.permissions import get_permissions
from app.core.permissions import Permission
from app.core.types import validate_entity_reference
from app.db.models import Channel, Guild, User
from app.voice.rooms import guild_room_name
from app.voice.state import room_state_key

CHANNEL_INFO_FIELDS = frozenset({"status", "voice_start_time"})
VOICE_CHANNEL_TYPES = frozenset({2, 13})


def voice_channel_status_key(guild_domain: str, guild_id: int, channel_id: int) -> str:
    return f"voice:channel-status:{guild_domain}:{guild_id}:{channel_id}"


def voice_channel_start_time_key(guild_domain: str, guild_id: int, channel_id: int) -> str:
    return room_state_key(
        "start-time",
        guild_domain,
        guild_room_name(guild_id, channel_id),
    )


def _decoded(value: object) -> str | None:
    if isinstance(value, bytes):
        return value.decode()
    return value if isinstance(value, str) else None


async def voice_channel_status(
    redis: Redis,
    guild_domain: str,
    guild_id: int,
    channel_id: int,
) -> str | None:
    return _decoded(await redis.get(voice_channel_status_key(guild_domain, guild_id, channel_id)))


async def voice_channel_start_time(
    redis: Redis,
    guild_domain: str,
    guild_id: int,
    channel_id: int,
) -> int | None:
    raw = _decoded(
        await redis.get(voice_channel_start_time_key(guild_domain, guild_id, channel_id))
    )
    if raw is None or not raw.isascii() or not raw.isdecimal():
        return None
    value = int(raw)
    return value if value > 0 else None


async def channel_info_item(
    redis: Redis,
    guild: Guild,
    channel: Channel,
    fields: Iterable[str],
) -> dict[str, object]:
    """Render only explicitly requested ephemeral fields for one voice channel."""

    requested = frozenset(fields)
    item: dict[str, object] = {
        "id": str(channel.id),
        "origin_domain": channel.origin_domain,
        "guild_id": str(guild.id),
        "guild_domain": guild.origin_domain,
    }
    if "status" in requested:
        item["status"] = (
            await voice_channel_status(redis, guild.origin_domain, guild.id, channel.id)
            if channel.type == 2
            else None
        )
    if "voice_start_time" in requested:
        item["voice_start_time"] = await voice_channel_start_time(
            redis,
            guild.origin_domain,
            guild.id,
            channel.id,
        )
    return item


async def visible_guild_channel_info(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    fields: Iterable[str],
) -> dict[str, object]:
    """Return Discord's opcode-43 projection for channels visible to an actor."""

    requested = tuple(dict.fromkeys(fields))
    if not requested or any(field not in CHANNEL_INFO_FIELDS for field in requested):
        raise ValueError("invalid channel info fields")
    channels = list(
        await session.scalars(
            select(Channel)
            .where(
                Channel.guild_id == guild.id,
                Channel.guild_domain == guild.origin_domain,
                Channel.type.in_(VOICE_CHANNEL_TYPES),
                Channel.unavailable.is_(False),
            )
            .order_by(Channel.position, Channel.id)
        )
    )
    rendered: list[dict[str, object]] = []
    for channel in channels:
        permissions = await get_permissions(session, redis, guild, actor, channel=channel)
        if permissions & Permission.VIEW_CHANNEL:
            rendered.append(await channel_info_item(redis, guild, channel, requested))
    return {"guild_id": str(guild.id), "channels": rendered}


def voice_channel_update_payload(
    guild: Guild,
    channel: Channel,
    field: str,
    value: object,
) -> dict[str, object]:
    """Render the shared Discord event shape plus Kaede authority domains."""

    if field not in CHANNEL_INFO_FIELDS:
        raise ValueError("invalid voice channel info field")
    return {
        "id": str(channel.id),
        "guild_id": str(guild.id),
        "origin_domain": channel.origin_domain,
        "guild_domain": guild.origin_domain,
        field: value,
    }


def validate_channel_info_request(data: object) -> tuple[str, tuple[str, ...]]:
    """Strictly validate opcode 43 without JSON bool/int coercion."""

    if not isinstance(data, dict) or set(data) != {"guild_id", "fields"}:
        raise ValueError("invalid channel info request")
    guild_id = data.get("guild_id")
    if not isinstance(guild_id, str):
        raise ValueError("invalid channel info guild")
    try:
        if str(validate_entity_reference(guild_id)) != guild_id:
            raise ValueError("noncanonical channel info guild")
    except ValueError:
        raise ValueError("invalid channel info guild") from None
    fields = data.get("fields")
    if (
        not isinstance(fields, list)
        or not 1 <= len(fields) <= len(CHANNEL_INFO_FIELDS)
        or any(not isinstance(field, str) for field in fields)
    ):
        raise ValueError("invalid channel info fields")
    requested = tuple(dict.fromkeys(fields))
    if len(requested) != len(fields) or any(
        field not in CHANNEL_INFO_FIELDS for field in requested
    ):
        raise ValueError("invalid channel info fields")
    return guild_id, requested
