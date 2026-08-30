from __future__ import annotations

from collections import defaultdict

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.events import guild_topic, publish_dispatch
from app.chat.payloads import emoji_payload, soundboard_sound_payload, sticker_payload
from app.db.models import Emoji, EmojiRoleRestriction, Guild, SoundboardSound, Sticker


async def publish_guild_emojis_update(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
) -> None:
    """Publish Discord's complete emoji collection after a committed mutation."""

    emojis = list(
        await session.scalars(
            select(Emoji)
            .where(
                Emoji.guild_id == guild.id,
                Emoji.guild_domain == guild.origin_domain,
            )
            .order_by(Emoji.origin_domain, Emoji.id)
        )
    )
    roles_by_emoji: dict[tuple[int, str], list[str]] = defaultdict(list)
    role_rows = (
        await session.execute(
            select(
                EmojiRoleRestriction.emoji_id,
                EmojiRoleRestriction.emoji_domain,
                EmojiRoleRestriction.role_id,
                EmojiRoleRestriction.role_domain,
            )
            .where(
                EmojiRoleRestriction.guild_id == guild.id,
                EmojiRoleRestriction.guild_domain == guild.origin_domain,
            )
            .order_by(
                EmojiRoleRestriction.emoji_domain,
                EmojiRoleRestriction.emoji_id,
                EmojiRoleRestriction.role_domain,
                EmojiRoleRestriction.role_id,
            )
        )
    ).tuples()
    for emoji_id, emoji_domain, role_id, role_domain in role_rows:
        roles_by_emoji[(emoji_id, emoji_domain)].append(f"{role_id}@{role_domain}")
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_EMOJIS_UPDATE",
        {
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
            "emojis": [
                emoji_payload(item, roles_by_emoji[(item.id, item.origin_domain)])
                for item in emojis
            ],
        },
    )


async def publish_guild_stickers_update(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
) -> None:
    """Publish Discord's complete sticker collection after a committed mutation."""

    stickers = list(
        await session.scalars(
            select(Sticker)
            .where(
                Sticker.guild_id == guild.id,
                Sticker.guild_domain == guild.origin_domain,
            )
            .order_by(Sticker.origin_domain, Sticker.id)
        )
    )
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_STICKERS_UPDATE",
        {
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
            "stickers": [sticker_payload(item) for item in stickers],
        },
    )


async def publish_guild_soundboard_sounds_update(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
) -> None:
    """Publish Discord's complete soundboard collection after a committed mutation."""

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
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_SOUNDBOARD_SOUNDS_UPDATE",
        {
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
            "soundboard_sounds": [soundboard_sound_payload(item) for item in sounds],
        },
    )
