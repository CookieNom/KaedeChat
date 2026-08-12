from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import reduce
from operator import or_ as bit_or
from typing import cast

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.moderation_status import sanitize_timeout_reason
from app.core.permissions import ALL_PERMISSIONS, Permission
from app.db.models import Channel, ChannelOverwrite, Guild, GuildMember, MemberRole, Role, User


@dataclass(frozen=True, slots=True)
class PermissionOverwrite:
    target_id: int
    target_domain: str
    target_type: str
    allow: int
    deny: int


TEXT_DEPENDENT = int(
    Permission.SEND_MESSAGES
    | Permission.EMBED_LINKS
    | Permission.ATTACH_FILES
    | Permission.MENTION_EVERYONE
)
VOICE_DEPENDENT = int(
    Permission.SPEAK | Permission.STREAM | Permission.USE_VAD | Permission.MOVE_MEMBERS
)
TIMEOUT_ALLOWED = int(Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY)
RELEASE_PERMISSION_LOCK = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""


def resolve_permissions(
    *,
    owner: bool,
    user_id: int,
    user_domain: str,
    everyone_role_id: int,
    everyone_role_domain: str,
    role_ids: set[tuple[int, str]],
    base_permissions: int,
    overwrites: list[PermissionOverwrite],
    channel_type: int | None,
    timed_out: bool,
) -> int:
    if owner:
        return ALL_PERMISSIONS
    permissions = base_permissions
    administrator = bool(permissions & Permission.ADMINISTRATOR)
    if administrator:
        permissions = ALL_PERMISSIONS
    else:
        everyone = next(
            (
                item
                for item in overwrites
                if item.target_type == "role"
                and item.target_id == everyone_role_id
                and item.target_domain == everyone_role_domain
            ),
            None,
        )
        if everyone is not None:
            permissions = (permissions & ~everyone.deny) | everyone.allow

        role_deny = 0
        role_allow = 0
        for item in overwrites:
            if item.target_type == "role" and (item.target_id, item.target_domain) in role_ids:
                role_deny |= item.deny
                role_allow |= item.allow
        permissions = (permissions & ~role_deny) | role_allow

        member = next(
            (
                item
                for item in overwrites
                if item.target_type == "member"
                and item.target_id == user_id
                and item.target_domain == user_domain
            ),
            None,
        )
        if member is not None:
            permissions = (permissions & ~member.deny) | member.allow

    if not permissions & Permission.VIEW_CHANNEL:
        return 0
    if channel_type in {0, 5} and not permissions & Permission.SEND_MESSAGES:
        permissions &= ~TEXT_DEPENDENT
    if channel_type == 2 and not permissions & Permission.CONNECT:
        permissions &= ~VOICE_DEPENDENT
    if timed_out:
        permissions &= TIMEOUT_ALLOWED
    return permissions


async def calculate_permissions(
    session: AsyncSession,
    guild: Guild,
    actor: User,
    *,
    channel: Channel | None = None,
) -> tuple[int, GuildMember]:
    member = await session.scalar(
        select(GuildMember).where(
            GuildMember.guild_id == guild.id,
            GuildMember.guild_domain == guild.origin_domain,
            GuildMember.user_id == actor.id,
            GuildMember.user_domain == actor.origin_domain,
        )
    )
    if member is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    roles = list(
        await session.scalars(
            select(Role)
            .outerjoin(
                MemberRole,
                (MemberRole.role_id == Role.id)
                & (MemberRole.role_domain == Role.origin_domain)
                & (MemberRole.guild_id == guild.id)
                & (MemberRole.guild_domain == guild.origin_domain)
                & (MemberRole.user_id == actor.id)
                & (MemberRole.user_domain == actor.origin_domain),
            )
            .where(
                Role.guild_id == guild.id,
                Role.guild_domain == guild.origin_domain,
                or_(Role.id == guild.id, MemberRole.role_id.is_not(None)),
            )
        )
    )
    role_ids = {(role.id, role.origin_domain) for role in roles}
    overwrites: list[PermissionOverwrite] = []
    if channel is not None:
        overwrite_channel = channel
        if channel.parent_id is not None and channel.permissions_synced:
            parent = await session.get(Channel, (channel.parent_id, channel.parent_domain))
            if (
                parent is None
                or parent.type != 4
                or (
                    parent.guild_id,
                    parent.guild_domain,
                )
                != (guild.id, guild.origin_domain)
            ):
                raise HTTPException(status_code=409, detail={"code": "CHANNEL_PARENT_INVALID"})
            overwrite_channel = parent
        rows = await session.scalars(
            select(ChannelOverwrite).where(
                ChannelOverwrite.channel_id == overwrite_channel.id,
                ChannelOverwrite.channel_domain == overwrite_channel.origin_domain,
            )
        )
        overwrites = [
            PermissionOverwrite(
                item.target_id,
                item.target_domain,
                item.target_type,
                item.allow,
                item.deny,
            )
            for item in rows
        ]
    timed_out = member.timeout_indefinite or (
        member.timeout_until is not None and member.timeout_until > datetime.now(UTC)
    )
    return (
        resolve_permissions(
            owner=guild.owner_id == actor.id and guild.owner_domain == actor.origin_domain,
            user_id=actor.id,
            user_domain=actor.origin_domain,
            everyone_role_id=guild.id,
            everyone_role_domain=guild.origin_domain,
            role_ids=role_ids,
            base_permissions=reduce(bit_or, (role.permissions for role in roles), 0),
            overwrites=overwrites,
            channel_type=channel.type if channel else None,
            timed_out=timed_out,
        ),
        member,
    )


async def get_permissions(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    *,
    channel: Channel | None = None,
) -> int:
    member = await session.scalar(
        select(GuildMember).where(
            GuildMember.guild_id == guild.id,
            GuildMember.guild_domain == guild.origin_domain,
            GuildMember.user_id == actor.id,
            GuildMember.user_domain == actor.origin_domain,
        )
    )
    if member is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    scope = str(channel.id) if channel else "guild"
    key = (
        f"perm:{guild.origin_domain}:{guild.id}:{guild.permission_generation}:"
        f"{actor.origin_domain}:{actor.id}:{member.member_version}:{scope}"
    )
    cached = await redis.get(key)
    if cached is not None:
        try:
            return int(cast(str, cached))
        except ValueError:
            await redis.delete(key)
    stable_scope = f"{guild.origin_domain}:{guild.id}:{actor.origin_domain}:{actor.id}:{scope}"
    stale_key = f"perm-stale:{stable_scope}"
    lock_key = f"perm-lock:{key}"
    owner = secrets.token_urlsafe(12)
    acquired = await redis.set(lock_key, owner, ex=5, nx=True)
    if not acquired:
        # A stale denial can never grant a permission that has just been
        # revoked. Positive stale values are deliberately not served on an
        # authorization path.
        stale = await redis.get(stale_key)
        if stale is not None:
            try:
                if int(cast(str, stale)) == 0:
                    return 0
            except ValueError:
                await redis.delete(stale_key)
        for _ in range(20):
            await asyncio.sleep(0.025)
            cached = await redis.get(key)
            if cached is not None:
                try:
                    return int(cast(str, cached))
                except ValueError:
                    await redis.delete(key)
        permissions, _ = await calculate_permissions(session, guild, actor, channel=channel)
        return permissions
    try:
        permissions, calculated_member = await calculate_permissions(
            session, guild, actor, channel=channel
        )
        cache_ttl = 300
        if calculated_member.timeout_until is not None and not calculated_member.timeout_indefinite:
            remaining = int((calculated_member.timeout_until - datetime.now(UTC)).total_seconds())
            if remaining > 0:
                cache_ttl = max(1, min(cache_ttl, remaining))
        pipeline = redis.pipeline(transaction=True)
        pipeline.set(key, str(permissions), ex=cache_ttl)
        pipeline.set(stale_key, str(permissions), ex=cache_ttl + 5)
        await pipeline.execute()
        return permissions
    finally:
        await cast(Awaitable[object], redis.eval(RELEASE_PERMISSION_LOCK, 1, lock_key, owner))


async def require_permissions(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    needed: Permission,
    *,
    channel: Channel | None = None,
) -> int:
    permissions = await get_permissions(session, redis, guild, actor, channel=channel)
    if permissions & needed != needed:
        member = await session.get(
            GuildMember,
            (guild.id, guild.origin_domain, actor.id, actor.origin_domain),
        )
        now = datetime.now(UTC)
        if member is not None and (
            member.timeout_indefinite
            or (member.timeout_until is not None and member.timeout_until > now)
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "MEMBER_TIMED_OUT",
                    "message": "You are currently timed out in this guild.",
                    "timeout_until": (
                        member.timeout_until.isoformat() if member.timeout_until else None
                    ),
                    "timeout_indefinite": member.timeout_indefinite,
                    "reason": sanitize_timeout_reason(member.timeout_reason),
                },
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "MISSING_PERMISSIONS",
                "message": "You do not have permission to perform this action.",
                "permissions": str(int(needed)),
            },
        )
    return permissions
