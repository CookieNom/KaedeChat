from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import AdminPrincipal
from app.core.snowflake import SnowflakeGenerator
from app.db.bot_models import InstanceAuditEvent


async def audit(
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    principal: AdminPrincipal,
    action: str,
    target_type: str,
    target_ref: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    session.add(
        InstanceAuditEvent(
            id=await snowflake.mint(),
            actor_id=principal.user.id,
            actor_domain=principal.user.origin_domain,
            actor_kind="admin",
            action=action,
            target_type=target_type,
            target_ref=target_ref,
            detail=metadata or {},
        )
    )
