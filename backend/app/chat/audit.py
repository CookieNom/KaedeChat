from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.audit_payloads import audit_log_payload
from app.chat.events import guild_topic
from app.chat.postcommit import queue_postcommit_dispatch
from app.core.snowflake import SnowflakeGenerator
from app.db.models import AuditLogEntry, Guild, User


def normalize_audit_reason(value: str | None) -> str | None:
    """Normalize an audit reason and return a clear Discord-compatible error."""

    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > 512:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "AUDIT_REASON_TOO_LONG",
                "message": "Audit log reasons cannot exceed 512 characters.",
            },
        )
    return cleaned


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
    reason = normalize_audit_reason(reason)
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
        created_at=datetime.now(UTC),
    )
    session.add(entry)
    queue_postcommit_dispatch(
        session,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_AUDIT_LOG_ENTRY_CREATE",
        audit_log_payload(entry).model_dump(mode="json"),
    )
    return entry
