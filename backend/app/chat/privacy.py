from __future__ import annotations

import hashlib

from fastapi import HTTPException
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import GuildMember, Relationship, User, UserSettings


def relationship_pair_lock_id(first: User, second: User) -> int:
    identities = sorted(
        (f"{first.id}@{first.origin_domain}", f"{second.id}@{second.origin_domain}")
    )
    return int.from_bytes(
        hashlib.blake2b("\n".join(identities).encode(), digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )


def dm_privacy_lock_id(user: User) -> int:
    return int.from_bytes(
        hashlib.blake2b(
            f"kaede-dm-privacy:{user.id}@{user.origin_domain}".encode(),
            digest_size=8,
        ).digest(),
        byteorder="big",
        signed=True,
    )


async def acquire_policy_locks(session: AsyncSession, lock_ids: set[int]) -> None:
    # A global numeric order prevents two opposite-direction DM requests from
    # acquiring the relationship and recipient-privacy locks in opposite order.
    for lock_id in sorted(lock_ids):
        await session.execute(select(func.pg_advisory_xact_lock(lock_id)))


async def lock_relationship_pair(session: AsyncSession, first: User, second: User) -> None:
    await acquire_policy_locks(session, {relationship_pair_lock_id(first, second)})


async def lock_dm_policy(session: AsyncSession, sender: User, recipient: User) -> None:
    await acquire_policy_locks(
        session,
        {
            relationship_pair_lock_id(sender, recipient),
            dm_privacy_lock_id(recipient),
        },
    )


async def lock_dm_privacy(session: AsyncSession, user: User) -> None:
    await acquire_policy_locks(session, {dm_privacy_lock_id(user)})


async def relationship(session: AsyncSession, owner: User, target: User) -> Relationship | None:
    result: Relationship | None = await session.scalar(
        select(Relationship).where(
            Relationship.user_id == owner.id,
            Relationship.user_domain == owner.origin_domain,
            Relationship.target_id == target.id,
            Relationship.target_domain == target.origin_domain,
        )
    )
    return result


async def blocked_between(session: AsyncSession, first: User, second: User) -> bool:
    return bool(
        await session.scalar(
            select(
                exists().where(
                    or_(
                        (Relationship.user_id == first.id)
                        & (Relationship.user_domain == first.origin_domain)
                        & (Relationship.target_id == second.id)
                        & (Relationship.target_domain == second.origin_domain)
                        & (Relationship.type == "blocked"),
                        (Relationship.user_id == second.id)
                        & (Relationship.user_domain == second.origin_domain)
                        & (Relationship.target_id == first.id)
                        & (Relationship.target_domain == first.origin_domain)
                        & (Relationship.type == "blocked"),
                    )
                )
            )
        )
    )


async def share_guild(session: AsyncSession, first: User, second: User) -> bool:
    first_membership = aliased(GuildMember)
    second_membership = aliased(GuildMember)
    return bool(
        await session.scalar(
            select(first_membership.guild_id)
            .join(
                second_membership,
                (second_membership.guild_id == first_membership.guild_id)
                & (second_membership.guild_domain == first_membership.guild_domain),
            )
            .where(
                first_membership.user_id == first.id,
                first_membership.user_domain == first.origin_domain,
                second_membership.user_id == second.id,
                second_membership.user_domain == second.origin_domain,
            )
            # A shared membership is part of the authorization decision. Keep
            # both rows alive through the caller's commit so kick/ban/snapshot
            # removal cannot land between this check and the DM write.
            .with_for_update(
                read=True,
                of=(first_membership, second_membership),
            )
            .limit(1)
        )
    )


async def can_direct_message(session: AsyncSession, sender: User, recipient: User) -> bool:
    if await blocked_between(session, sender, recipient):
        return False
    settings = await session.scalar(
        select(UserSettings).where(
            UserSettings.user_id == recipient.id,
            UserSettings.user_domain == recipient.origin_domain,
        )
    )
    if settings is None:
        return False
    if settings.dm_privacy == "everyone":
        return True
    relation = await relationship(session, recipient, sender)
    if relation is not None and relation.type == "friend":
        return True
    if settings.dm_privacy == "friends":
        return False
    return await share_guild(session, sender, recipient)


async def require_can_direct_message(session: AsyncSession, sender: User, recipient: User) -> None:
    # Hold both policy locks through the caller's commit. A block/friendship or
    # recipient privacy change therefore occurs wholly before or after the DM
    # open/send/ingest transaction, never between authorization and its write.
    await lock_dm_policy(session, sender, recipient)
    if not await can_direct_message(session, sender, recipient):
        raise HTTPException(status_code=403, detail={"code": "DM_PRIVACY_REJECTED"})
