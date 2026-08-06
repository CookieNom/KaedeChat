from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import SnowflakeGenerator
from app.db.models import AuditLogEntry, Guild, User


async def add_audit_entry(
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    guild: Guild,
    actor: User,
    action_type: int,
    *,
    target_type: str | None = None,
    target_ref: dict[str, Any] | None = None,
    reason: str | None = None,
    changes: list[dict[str, Any]] | None = None,
) -> AuditLogEntry:
    entry = AuditLogEntry(
        id=await snowflake.mint(),
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        actor_id=actor.id,
        actor_domain=actor.origin_domain,
        action_type=action_type,
        target_type=target_type,
        target_ref=target_ref,
        reason=reason,
        changes=changes or [],
    )
    session.add(entry)
    return entry
