from __future__ import annotations

from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.bots import installation_for_guild
from app.api.guilds import local_guild
from app.bots.auth import BotPrincipal
from app.chat.permissions import require_permissions
from app.core.permission_contract import required_permissions
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.bot_models import BotInstallation
from app.db.models import Guild, User
from app.federation.guild_management import (
    GuildManagementOperation,
    proxy_remote_guild_management_body,
)


async def proxy_human_guild_feature(
    session: AsyncSession,
    settings: Settings,
    guild_ref: EntityRef,
    actor: User,
    operation: GuildManagementOperation,
    payload: dict[str, Any] | None = None,
) -> tuple[bool, object]:
    """Proxy a human guild feature operation to the guild authority when needed."""

    return await proxy_remote_guild_management_body(
        session,
        settings,
        guild_ref,
        actor,
        operation,
        payload,
    )


async def authorize_human_guild_feature(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild_ref: EntityRef,
    actor: User,
    operation: str,
    *,
    for_update: bool = False,
) -> Guild:
    """Load a local guild and require the permission contract for one feature."""

    guild = await local_guild(session, settings, guild_ref, for_update=for_update)
    await require_permissions(
        session,
        redis,
        guild,
        actor,
        required_permissions(operation),
    )
    return guild


async def authorize_bot_guild_feature(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild_ref: EntityRef,
    principal: BotPrincipal,
    *,
    scope: str,
    operation: str,
) -> Guild:
    """Require both an installation scope and the bot member's guild permission."""

    guild, _ = await authorize_bot_guild_feature_grant(
        session,
        redis,
        settings,
        guild_ref,
        principal,
        scope=scope,
        operation=operation,
    )
    return guild


async def authorize_bot_guild_feature_grant(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild_ref: EntityRef,
    principal: BotPrincipal,
    *,
    scope: str,
    operation: str,
) -> tuple[Guild, BotInstallation]:
    """Return the exact installation when nested resources also need its grant."""

    guild, installation = await installation_for_guild(
        session,
        settings,
        principal,
        guild_ref,
        scope,
    )
    await require_permissions(
        session,
        redis,
        guild,
        principal.user,
        required_permissions(operation),
    )
    return guild, installation
