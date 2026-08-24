from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Channel, Guild

THREAD_TYPES = frozenset({10, 11, 12})
MAX_ACTIVE_THREADS = 1000


async def require_active_thread_capacity(
    session: AsyncSession,
    guild: Guild,
    *,
    excluding: tuple[int, str] | None = None,
) -> None:
    """Serialize active-thread admissions at the authoritative guild row."""

    await session.execute(
        select(Guild.id)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
    )
    statement = select(func.count(Channel.id)).where(
        Channel.guild_id == guild.id,
        Channel.guild_domain == guild.origin_domain,
        Channel.type.in_(THREAD_TYPES),
        Channel.unavailable.is_(False),
        Channel.archived.is_(False),
    )
    if excluding is not None:
        statement = statement.where(
            ~((Channel.id == excluding[0]) & (Channel.origin_domain == excluding[1]))
        )
    if int(await session.scalar(statement) or 0) >= MAX_ACTIVE_THREADS:
        raise HTTPException(status_code=409, detail={"code": "ACTIVE_THREAD_LIMIT"})
