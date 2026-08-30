from __future__ import annotations

# Discord channel type values used by more than one backend subsystem.
GUILD_VOICE_CHANNEL = 2
GUILD_STAGE_VOICE_CHANNEL = 13
GUILD_CHANNEL_TYPES = frozenset({0, 2, 4, 5, 10, 11, 12, 13, 15, 17})
GUILD_VOICE_CHANNEL_TYPES = frozenset(
    {
        GUILD_VOICE_CHANNEL,
        GUILD_STAGE_VOICE_CHANNEL,
    }
)

# Forum channels (15) create messages only through their post/thread starter
# workflow. Voice and Stage channels expose their embedded text chat directly.
GUILD_MESSAGE_CHANNEL_TYPES = frozenset({0, 2, 5, 10, 11, 12, 13})
# Permissions that govern text-like message fields also apply to forum
# channels, even though forum messages are admitted through post starters.
GUILD_TEXT_PERMISSION_CHANNEL_TYPES = GUILD_MESSAGE_CHANNEL_TYPES | {15}
# Discord marks PIN_MESSAGES as text-only. Forum/media parents inherit the
# permission for their post threads, while embedded voice and Stage chats do
# not expose pin operations despite otherwise supporting messages.
GUILD_PINNABLE_CHANNEL_TYPES = frozenset({0, 5, 10, 11, 12, 15})
# Channel types whose text permission dependency is SEND_MESSAGES. Threads use
# SEND_MESSAGES_IN_THREADS instead; forums use SEND_MESSAGES to create posts.
GUILD_SEND_MESSAGES_CHANNEL_TYPES = frozenset({0, 2, 5, 13, 15})
DIRECT_MESSAGE_CHANNEL_TYPES = frozenset({1, 3})


def is_message_capable_channel_type(channel_type: int, *, guild_channel: bool) -> bool:
    return channel_type in (
        GUILD_MESSAGE_CHANNEL_TYPES if guild_channel else DIRECT_MESSAGE_CHANNEL_TYPES
    )


def is_pinnable_guild_channel_type(channel_type: int) -> bool:
    return channel_type in GUILD_PINNABLE_CHANNEL_TYPES


def is_soundboard_channel_type(channel_type: int) -> bool:
    """Discord permits Soundboard playback only in ordinary guild voice channels."""

    return channel_type == GUILD_VOICE_CHANNEL


def validate_voice_channel_limits(
    channel_type: int,
    *,
    bitrate: int | None,
    user_limit: int | None,
) -> None:
    """Validate Discord's type-specific Voice and Stage capacity bounds."""

    if channel_type not in GUILD_VOICE_CHANNEL_TYPES:
        return
    max_bitrate = 64_000 if channel_type == GUILD_STAGE_VOICE_CHANNEL else 384_000
    max_users = 10_000 if channel_type == GUILD_STAGE_VOICE_CHANNEL else 99
    if bitrate is not None and not 8_000 <= bitrate <= max_bitrate:
        raise ValueError(f"bitrate must be between 8000 and {max_bitrate}")
    if user_limit is not None and not 0 <= user_limit <= max_users:
        raise ValueError(f"user_limit must be between 0 and {max_users}")
