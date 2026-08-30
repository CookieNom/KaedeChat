from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.bot_models import BotApplication

ProjectionRef = tuple[int, str]


async def lock_bot_projection_identities(
    session: AsyncSession,
    *,
    application_refs: Iterable[ProjectionRef] = (),
    bot_user_refs: Iterable[ProjectionRef] = (),
    team_refs: Iterable[ProjectionRef] = (),
) -> None:
    """Serialize remote bot projections in one deadlock-safe global order.

    The scope names are a compatibility contract with existing manifest
    materialization.  Applications sort before bot identities, which sort
    before developer teams; callers must not acquire a later category and then
    return for an earlier one.
    """

    scopes = (
        *(f"bot-manifest:{item_id}@{domain}" for item_id, domain in sorted(set(application_refs))),
        *(
            f"bot-application-user:{item_id}@{domain}"
            for item_id, domain in sorted(set(bot_user_refs))
        ),
        *(
            f"developer-team-projection:{item_id}@{domain}"
            for item_id, domain in sorted(set(team_refs))
        ),
    )
    for scope in scopes:
        await session.scalar(select(func.pg_advisory_xact_lock(func.hashtextextended(scope, 0))))


async def bot_application_identity_owner(
    session: AsyncSession,
    bot_user_ref: ProjectionRef,
) -> BotApplication | None:
    """Return and row-lock the application already bound to one bot user."""

    return cast(
        BotApplication | None,
        await session.scalar(
            select(BotApplication)
            .where(
                BotApplication.bot_user_id == bot_user_ref[0],
                BotApplication.bot_user_domain == bot_user_ref[1],
            )
            .with_for_update()
            .limit(1)
        ),
    )
