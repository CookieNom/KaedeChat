from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


async def materialize_updated_at(session: AsyncSession, *rows: object) -> None:
    """Load database-managed update timestamps before synchronous rendering.

    PostgreSQL evaluates ``TimestampMixin.updated_at`` during UPDATE. SQLAlchemy
    therefore expires that attribute after a flush, and accessing it through a
    synchronous payload helper would otherwise attempt async lazy I/O.
    """

    if not rows:
        return
    await session.flush()
    for row in rows:
        await session.refresh(row, attribute_names=("updated_at",))
