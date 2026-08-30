from __future__ import annotations

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.custom_emojis import canonical_reaction_emoji, custom_emoji_refs
from app.db.models import Reaction, User


def reaction_emoji_payload(emoji: str) -> dict[str, object]:
    """Project one canonical federation emoji into Discord's partial shape."""

    canonical = canonical_reaction_emoji(emoji)
    custom = custom_emoji_refs(canonical)
    if custom:
        ref = custom[0]
        return {
            "id": str(ref.id),
            "origin_domain": ref.origin_domain,
            "name": ref.name,
            "animated": ref.animated,
        }
    return {
        "id": None,
        "origin_domain": None,
        "name": canonical,
        "animated": False,
    }


def reaction_event_payload(
    *,
    message_id: int,
    message_domain: str,
    channel_id: int,
    channel_domain: str,
    user_id: int,
    user_domain: str,
    emoji: str,
    guild_id: int | None = None,
    guild_domain: str | None = None,
    message_author_id: int | None = None,
    message_author_domain: str | None = None,
    removed: bool = False,
) -> dict[str, object]:
    """Build one shared human/bot reaction gateway projection."""

    if (guild_id is None) != (guild_domain is None):
        raise ValueError("reaction guild reference must be complete")
    if (message_author_id is None) != (message_author_domain is None):
        raise ValueError("reaction message author reference must be complete")
    canonical = canonical_reaction_emoji(emoji)
    payload: dict[str, object] = {
        # Composite aliases retain compatibility with Kaede's existing human
        # clients while the Discord-style fields provide one bot contract.
        "id": str(message_id),
        "origin_domain": message_domain,
        "message_id": str(message_id),
        "message_domain": message_domain,
        "channel_id": str(channel_id),
        "channel_domain": channel_domain,
        "user_id": str(user_id),
        "user_domain": user_domain,
        "reaction": canonical,
        "emoji": reaction_emoji_payload(canonical),
        "burst": False,
        "burst_colors": [],
        "type": 0,
        "removed": removed,
    }
    if guild_id is not None and guild_domain is not None:
        payload["guild_id"] = str(guild_id)
        payload["guild_domain"] = guild_domain
    if message_author_id is not None and message_author_domain is not None:
        payload["message_author_id"] = str(message_author_id)
        payload["message_author_domain"] = message_author_domain
    return payload


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
            try:
                canonical = canonical_reaction_emoji(emoji)
            except (TypeError, ValueError):
                continue
            reacted = mine.setdefault((message_id, message_domain), [])
            if canonical not in reacted:
                reacted.append(canonical)
    result: dict[tuple[int, str], tuple[dict[str, int], list[str]]] = {}
    for message_id, message_domain, emoji, count in rows:
        try:
            canonical = canonical_reaction_emoji(emoji)
        except (TypeError, ValueError):
            continue
        reference = (message_id, message_domain)
        counts, reacted = result.setdefault(reference, ({}, mine.get(reference, [])))
        counts[canonical] = counts.get(canonical, 0) + int(count)
    for reference, reacted in mine.items():
        result.setdefault(reference, ({}, reacted))
    for counts, reacted in result.values():
        reacted[:] = [emoji for emoji in reacted if emoji in counts]
    return result
