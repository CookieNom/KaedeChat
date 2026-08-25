from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import InstanceUserRestriction, User


async def active_instance_user_restriction(
    session: AsyncSession,
    user: User,
    *,
    now: datetime | None = None,
    for_update: bool = False,
) -> InstanceUserRestriction | None:
    """Load active moderation state owned by this instance for a remote user."""

    current = now or datetime.now(UTC)
    statement = select(InstanceUserRestriction).where(
        InstanceUserRestriction.user_id == user.id,
        InstanceUserRestriction.user_domain == user.origin_domain,
        or_(
            InstanceUserRestriction.restriction_type == "banned",
            InstanceUserRestriction.expires_at > current,
        ),
    )
    if for_update:
        statement = statement.with_for_update()
    return cast(InstanceUserRestriction | None, await session.scalar(statement))


async def require_remote_user_creation_allowed(
    session: AsyncSession,
    user: User,
) -> None:
    restriction = await active_instance_user_restriction(session, user)
    if restriction is None:
        return
    raise HTTPException(
        status_code=403,
        detail={
            "code": (
                "USER_BANNED_FROM_INSTANCE"
                if restriction.restriction_type == "banned"
                else "USER_SUSPENDED_FROM_INSTANCE"
            ),
            "expires_at": (
                restriction.expires_at.isoformat() if restriction.expires_at is not None else None
            ),
        },
    )


async def require_remote_user_join_allowed(
    session: AsyncSession,
    user: User,
) -> None:
    restriction = await active_instance_user_restriction(session, user)
    if restriction is not None and restriction.restriction_type == "banned":
        raise HTTPException(
            status_code=403,
            detail={"code": "USER_BANNED_FROM_INSTANCE"},
        )
