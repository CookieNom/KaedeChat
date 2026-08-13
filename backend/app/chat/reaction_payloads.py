from __future__ import annotations

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Reaction, User


async def reaction_payloads_for_messages(
    session: AsyncSession,
    message_refs: set[tuple[int, str]],
    *,
    viewer: User | None = None,
) -> dict[tuple[int, str], tuple[dict[str, int], list[str]]]:
    """Load compact reaction summaries without exposing the reacting user list."""

    if not message_refs:
        return {}
    rows = (
        await session.execute(
            select(
                Reaction.message_id,
                Reaction.message_domain,
                Reaction.emoji_key,
                func.count(),
            )
            .where(tuple_(Reaction.message_id, Reaction.message_domain).in_(message_refs))
            .group_by(Reaction.message_id, Reaction.message_domain, Reaction.emoji_key)
            .order_by(Reaction.message_id, Reaction.message_domain, Reaction.emoji_key)
        )
    ).all()
    mine: dict[tuple[int, str], list[str]] = {}
    if viewer is not None:
        viewer_rows = (
            await session.execute(
                select(Reaction.message_id, Reaction.message_domain, Reaction.emoji_key).where(
                    tuple_(Reaction.message_id, Reaction.message_domain).in_(message_refs),
                    Reaction.user_id == viewer.id,
                    Reaction.user_domain == viewer.origin_domain,
                )
            )
        ).all()
        for message_id, message_domain, emoji in viewer_rows:
            mine.setdefault((message_id, message_domain), []).append(emoji)
    result: dict[tuple[int, str], tuple[dict[str, int], list[str]]] = {}
    for message_id, message_domain, emoji, count in rows:
        reference = (message_id, message_domain)
        counts, reacted = result.setdefault(reference, ({}, mine.get(reference, [])))
        counts[emoji] = int(count)
    for reference, reacted in mine.items():
        result.setdefault(reference, ({}, reacted))
    return result
