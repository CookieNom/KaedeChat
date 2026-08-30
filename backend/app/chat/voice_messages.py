from __future__ import annotations

from pydantic import ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.model_validation import UnambiguousInputModel
from app.db.models import Guild


class VoiceMessageCapability(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    available: bool = True


async def guild_voice_message_capability(
    session: AsyncSession,
    guild: Guild,
) -> VoiceMessageCapability:
    """Return the stable capability shape; guild size is not a voice-message limit."""

    del session, guild
    return VoiceMessageCapability()


async def require_voice_message_guild_capacity(
    session: AsyncSession,
    guild: Guild | None,
    *,
    voice_message: bool,
) -> None:
    """Compatibility hook retained for callers; guild size is never consulted."""

    del session, guild, voice_message
