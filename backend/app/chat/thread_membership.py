from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.events import guild_topic, publish_dispatch
from app.chat.guild_revision import federation_channel_state, queue_guild_mutation
from app.chat.payloads import channel_payload
from app.core.settings import Settings
from app.db.materialization import materialize_updated_at
from app.db.models import Channel, Guild, ThreadMember, User


@dataclass(slots=True)
class RemovedThreadMembers:
    thread: Channel
    removed_refs: list[tuple[int, str]]
    rekeyed: bool
    member_count: int
    rendered_thread: dict[str, object] | None


async def cleanup_guild_member_threads(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    actor: User,
    user_refs: list[tuple[int, str]],
) -> list[RemovedThreadMembers]:
    """Remove explicit thread memberships before a GuildMember cascade.

    Callers already hold the authoritative guild mutation lock. Explicitly
    deleting these rows keeps denormalized counts, federation and private MLS
    access state synchronized instead of relying on an invisible FK cascade.
    """

    exact_refs = list(dict.fromkeys(user_refs))
    if not exact_refs:
        return []
    rows = list(
        (
            await session.execute(
                select(ThreadMember, Channel)
                .join(
                    Channel,
                    (Channel.id == ThreadMember.thread_id)
                    & (Channel.origin_domain == ThreadMember.thread_domain),
                )
                .where(
                    ThreadMember.guild_id == guild.id,
                    ThreadMember.guild_domain == guild.origin_domain,
                    tuple_(ThreadMember.user_id, ThreadMember.user_domain).in_(exact_refs),
                    Channel.type.in_({10, 11, 12}),
                    Channel.unavailable.is_(False),
                )
                .order_by(
                    Channel.origin_domain,
                    Channel.id,
                    ThreadMember.user_domain,
                    ThreadMember.user_id,
                )
                .with_for_update()
            )
        ).all()
    )
    grouped: dict[tuple[int, str], tuple[Channel, list[ThreadMember]]] = {}
    for member, thread in rows:
        grouped.setdefault((thread.id, thread.origin_domain), (thread, []))[1].append(member)
    removals: list[RemovedThreadMembers] = []
    for thread, members in grouped.values():
        removed_refs = [(member.user_id, member.user_domain) for member in members]
        for member in members:
            await session.delete(member)
        thread.member_count = max(0, int(thread.member_count or 0) - len(members))
        rekeyed = False
        if thread.type == 12:
            guild.permission_generation += 1
            if thread.encryption_mode == "e2ee" and thread.encryption_state == "active":
                thread.encryption_state = "rekeying"
                rekeyed = True
        if rekeyed:
            await queue_guild_mutation(
                session,
                settings,
                guild,
                actor,
                "guild.channel.update",
                {"channel": federation_channel_state(thread)},
                channel=thread,
            )
        for index, (user_id, user_domain) in enumerate(removed_refs):
            await queue_guild_mutation(
                session,
                settings,
                guild,
                actor,
                "guild.thread.member.delete",
                {
                    "thread_id": str(thread.id),
                    "thread_domain": thread.origin_domain,
                    "user_id": str(user_id),
                    "user_domain": user_domain,
                    "member_count": thread.member_count,
                },
                channel=thread,
                snapshot_required=thread.type == 12 and index == len(removed_refs) - 1,
            )
        rendered_thread: dict[str, object] | None = None
        if rekeyed:
            await materialize_updated_at(session, thread)
            rendered_thread = channel_payload(thread)
        removals.append(
            RemovedThreadMembers(
                thread,
                removed_refs,
                rekeyed,
                int(thread.member_count or 0),
                rendered_thread,
            )
        )
    return removals


async def publish_guild_thread_member_cleanup(
    redis: Redis,
    guild: Guild,
    removals: list[RemovedThreadMembers],
) -> None:
    topic = guild_topic(guild.origin_domain, guild.id)
    for removal in removals:
        if removal.rekeyed:
            if removal.rendered_thread is None:
                raise RuntimeError("rekeyed thread projection was not materialized")
            await publish_dispatch(
                redis,
                topic,
                "THREAD_UPDATE",
                removal.rendered_thread,
            )
        await publish_dispatch(
            redis,
            topic,
            "THREAD_MEMBERS_UPDATE",
            {
                "id": str(removal.thread.id),
                "thread_domain": removal.thread.origin_domain,
                "guild_id": str(guild.id),
                "guild_domain": guild.origin_domain,
                "member_count": min(50, removal.member_count),
                "added_members": [],
                "removed_member_ids": [str(item[0]) for item in removal.removed_refs],
                "removed_member_refs": [
                    {"id": str(user_id), "origin_domain": user_domain}
                    for user_id, user_domain in removal.removed_refs
                ],
            },
        )
