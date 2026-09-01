from __future__ import annotations

import asyncio
import hashlib
import math
import secrets
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import reduce
from operator import or_ as bit_or
from typing import TYPE_CHECKING, cast

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.moderation_status import member_timeout_error_detail
from app.core.channel_types import (
    GUILD_SEND_MESSAGES_CHANNEL_TYPES,
    GUILD_VOICE_CHANNEL_TYPES,
)
from app.core.permissions import ALL_PERMISSIONS, PERMISSION_METADATA, Permission
from app.db.models import (
    Channel,
    ChannelOverwrite,
    Guild,
    GuildMember,
    MemberRole,
    Role,
    ThreadMember,
    User,
)

if TYPE_CHECKING:
    from app.db.bot_models import BotInstallation


@dataclass(frozen=True, slots=True)
class PermissionOverwrite:
    target_id: int
    target_domain: str
    target_type: str
    allow: int
    deny: int


@dataclass(frozen=True, slots=True)
class BotGuildPermissionGrant:
    """Active installation state that caps one bot member's live permissions."""

    installation_id: int | None
    grant_revision: int
    granted_permissions: int
    channel_restrictions: tuple[str, ...]

    def cache_identity(self) -> str:
        restrictions_hash = hashlib.sha256(
            "\n".join(self.channel_restrictions).encode()
        ).hexdigest()
        return (
            f"bot:{self.installation_id or 'missing'}:{self.grant_revision}:"
            f"{self.granted_permissions}:{restrictions_hash}"
        )

    async def allows_channel(self, session: AsyncSession, channel: Channel) -> bool:
        from app.bots.installations import channel_restrictions_allow

        return await channel_restrictions_allow(session, self.channel_restrictions, channel)

    async def apply(
        self,
        session: AsyncSession,
        live_permissions: int,
        channel: Channel | None,
    ) -> int:
        if self.installation_id is None:
            return 0
        from app.bots.installations import effective_installation_permissions

        if channel is not None and not await self.allows_channel(session, channel):
            return 0
        effective = effective_installation_permissions(self.granted_permissions, live_permissions)
        if channel is None:
            return effective
        return normalize_permission_dependencies(
            effective,
            channel_type=channel.type,
            timed_out=False,
        )


def bot_guild_permission_grant_from_installation(
    installation: BotInstallation,
) -> BotGuildPermissionGrant:
    """Snapshot the fields that determine a bot installation permission grant."""

    from app.bots.installations import normalize_channel_restrictions

    return BotGuildPermissionGrant(
        installation_id=installation.id,
        grant_revision=installation.grant_revision,
        granted_permissions=installation.granted_permissions,
        channel_restrictions=normalize_channel_restrictions(
            installation.channel_restrictions or []
        ),
    )


async def bot_guild_permission_grant(
    session: AsyncSession,
    guild: Guild,
    actor: User,
) -> BotGuildPermissionGrant | None:
    """Resolve the active installation ceiling for a guild bot actor.

    Humans return ``None`` and preserve the ordinary role calculation.  A bot
    without an exact active installation receives an explicit zero ceiling so
    stale membership or role state can never grant residual REST authority.
    """

    if getattr(actor, "account_type", None) != "bot":
        return None
    if getattr(actor, "disabled_at", None) is not None:
        return BotGuildPermissionGrant(None, 0, 0, ())
    from app.bots.installations import bot_actor_active_installations_statement

    installations = list(
        await session.scalars(bot_actor_active_installations_statement(guild, actor).limit(2))
    )
    if len(installations) != 1:
        return BotGuildPermissionGrant(None, 0, 0, ())
    return bot_guild_permission_grant_from_installation(installations[0])


async def require_bot_channel_grant(
    session: AsyncSession,
    guild: Guild,
    actor: User,
    *channels: Channel,
) -> None:
    """Keep nested guild channels inside a bot installation's exact grant.

    Ordinary channel permission checks already apply the installation ceiling
    to the primary resource.  Guild-scoped mutations can also reference a
    parent category, however, so those nested resources need the same boundary
    without imposing an additional Discord permission check on human actors.
    """

    if getattr(actor, "account_type", None) != "bot":
        return
    grant = await bot_guild_permission_grant(session, guild, actor)
    if grant is None or grant.installation_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "BOT_NOT_INSTALLED"},
        )

    for channel in channels:
        if (
            getattr(channel, "unavailable", False)
            or channel.origin_domain != guild.origin_domain
            or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
            or not await grant.allows_channel(session, channel)
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "BOT_CHANNEL_RESTRICTED"},
            )


TEXT_DEPENDENT = int(
    Permission.SEND_MESSAGES
    | Permission.EMBED_LINKS
    | Permission.ATTACH_FILES
    | Permission.MENTION_EVERYONE
)
THREAD_TEXT_DEPENDENT = int(
    Permission.EMBED_LINKS | Permission.ATTACH_FILES | Permission.MENTION_EVERYONE
)
VOICE_DEPENDENT = int(
    Permission.PRIORITY_SPEAKER
    | Permission.SPEAK
    | Permission.STREAM
    | Permission.USE_VAD
    | Permission.MOVE_MEMBERS
)
TIMEOUT_ALLOWED = int(Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY)
PERMISSION_CACHE_NAMESPACE = "perm:v2"
RELEASE_PERMISSION_LOCK = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""


def normalize_permission_dependencies(
    permissions: int,
    *,
    channel_type: int | None,
    timed_out: bool,
) -> int:
    """Strip permission bits whose prerequisite did not survive resolution."""

    # Guild-scoped capabilities do not depend on the channel-only VIEW_CHANNEL
    # bit, and member timeouts restrict channel interaction rather than guild
    # administration.  Channel dependency and timeout masks therefore have no
    # place in a guild-only calculation.
    if channel_type is None:
        return permissions
    if not permissions & Permission.VIEW_CHANNEL:
        return 0
    if (
        channel_type in GUILD_SEND_MESSAGES_CHANNEL_TYPES
        and not permissions & Permission.SEND_MESSAGES
    ):
        permissions &= ~TEXT_DEPENDENT
    if channel_type in {10, 11, 12} and not permissions & Permission.SEND_MESSAGES_IN_THREADS:
        permissions &= ~THREAD_TEXT_DEPENDENT
    if channel_type in GUILD_VOICE_CHANNEL_TYPES and not permissions & Permission.CONNECT:
        permissions &= ~VOICE_DEPENDENT
    if channel_type == 2 and not permissions & Permission.SPEAK:
        permissions &= ~Permission.PRIORITY_SPEAKER
    # Metadata is the published dependency contract.  Iterate to a fixpoint so
    # chains such as CONNECT -> SPEAK -> USE_SOUNDBOARD -> USE_EXTERNAL_SOUNDS
    # cannot leave a transitive dependent bit behind.  The channel-type-specific
    # masks above remain necessary for Discord's SEND_MESSAGES vs
    # SEND_MESSAGES_IN_THREADS conditional semantics, which metadata cannot
    # express as one static dependency tuple.
    changed = True
    while changed:
        changed = False
        for metadata in PERMISSION_METADATA:
            if not permissions & metadata.permission:
                continue
            missing_dependency = any(
                (
                    not permissions & Permission.SEND_MESSAGES_IN_THREADS
                    if dependency == Permission.SEND_MESSAGES and channel_type in {10, 11, 12}
                    else permissions & dependency != dependency
                )
                for dependency in metadata.dependencies
            )
            if missing_dependency:
                permissions &= ~metadata.permission
                changed = True
    if timed_out:
        permissions &= TIMEOUT_ALLOWED
    return permissions


def require_permission_channel_guild(guild: Guild, channel: Channel | None) -> None:
    """Reject a channel context outside the exact guild authority composite."""

    if channel is not None and (channel.guild_id, channel.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        raise HTTPException(status_code=409, detail={"code": "CHANNEL_GUILD_INVALID"})


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
        everyone_ref = (everyone_role_id, everyone_role_domain)
        for item in overwrites:
            if (
                item.target_type == "role"
                and (item.target_id, item.target_domain) != everyone_ref
                and (item.target_id, item.target_domain) in role_ids
            ):
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

    return normalize_permission_dependencies(
        permissions,
        channel_type=channel_type,
        timed_out=timed_out,
    )


async def calculate_permissions(
    session: AsyncSession,
    guild: Guild,
    actor: User,
    *,
    channel: Channel | None = None,
    bot_grant: BotGuildPermissionGrant | None = None,
    evaluated_at: datetime | None = None,
) -> tuple[int, GuildMember]:
    require_permission_channel_guild(guild, channel)
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
        if channel.type in {10, 11, 12}:
            parent = await session.get(Channel, (channel.parent_id, channel.parent_domain))
            if (
                parent is None
                or parent.type not in {0, 5, 15}
                or (
                    parent.guild_id,
                    parent.guild_domain,
                )
                != (guild.id, guild.origin_domain)
            ):
                raise HTTPException(status_code=409, detail={"code": "CHANNEL_PARENT_INVALID"})
            overwrite_channel = parent
        if overwrite_channel.parent_id is not None and overwrite_channel.permissions_synced:
            parent = await session.get(
                Channel,
                (overwrite_channel.parent_id, overwrite_channel.parent_domain),
            )
            if (
                parent is None
                or parent.type != 4
                or (parent.guild_id, parent.guild_domain) != (guild.id, guild.origin_domain)
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
    current = evaluated_at or datetime.now(UTC)
    timed_out = member.timeout_indefinite or (
        member.timeout_until is not None and member.timeout_until > current
    )
    permissions = resolve_permissions(
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
    )
    if channel is not None and channel.type == 12 and not (permissions & Permission.MANAGE_THREADS):
        thread_member = await session.get(
            ThreadMember,
            (channel.id, channel.origin_domain, actor.id, actor.origin_domain),
        )
        if thread_member is None:
            permissions = 0
    if bot_grant is None:
        bot_grant = await bot_guild_permission_grant(session, guild, actor)
    if bot_grant is not None:
        permissions = await bot_grant.apply(session, permissions, channel)
    return permissions, member


def permission_cache_ttl(member: GuildMember, *, evaluated_at: datetime) -> int:
    """Bound cached timeout permissions to the exact evaluation window."""

    if member.timeout_indefinite or member.timeout_until is None:
        return 300
    remaining = (member.timeout_until - evaluated_at).total_seconds()
    if remaining <= 0:
        return 300
    # Rounding down can expire the cache just before the timeout, recalculate
    # the still-restricted mask, and then retain it for the normal five-minute
    # TTL.  At most one extra second of denial is safer than that stale window.
    return max(1, min(300, math.ceil(remaining)))


async def get_permissions(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    *,
    channel: Channel | None = None,
    bot_grant: BotGuildPermissionGrant | None = None,
) -> int:
    require_permission_channel_guild(guild, channel)
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
    if bot_grant is None:
        bot_grant = await bot_guild_permission_grant(session, guild, actor)
    if (
        bot_grant is not None
        and channel is not None
        and not await bot_grant.allows_channel(session, channel)
    ):
        # Revalidate hierarchy before consulting Redis. A cached positive grant
        # must not survive a missing, moved, or cross-guild thread parent.
        return 0
    scope = f"{channel.origin_domain}:{channel.id}" if channel is not None else "guild"
    cache_scope = (
        f"{guild.origin_domain}:{guild.id}:{guild.permission_generation}:"
        f"{actor.origin_domain}:{actor.id}:{member.member_version}:{scope}"
    )
    if bot_grant is not None:
        cache_scope = f"{cache_scope}:{bot_grant.cache_identity()}"
    key = f"{PERMISSION_CACHE_NAMESPACE}:{cache_scope}"
    cached = await redis.get(key)
    if cached is not None:
        try:
            return int(cast(str, cached))
        except ValueError:
            await redis.delete(key)
    stable_scope = f"{guild.origin_domain}:{guild.id}:{actor.origin_domain}:{actor.id}:{scope}"
    stale_key = f"{PERMISSION_CACHE_NAMESPACE}:stale:{stable_scope}"
    lock_key = f"{PERMISSION_CACHE_NAMESPACE}:lock:{cache_scope}"
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
        permissions, _ = await calculate_permissions(
            session,
            guild,
            actor,
            channel=channel,
            bot_grant=bot_grant,
        )
        return permissions
    try:
        evaluated_at = datetime.now(UTC)
        permissions, calculated_member = await calculate_permissions(
            session,
            guild,
            actor,
            channel=channel,
            bot_grant=bot_grant,
            evaluated_at=evaluated_at,
        )
        cache_ttl = permission_cache_ttl(calculated_member, evaluated_at=evaluated_at)
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
        timeout_detail = (
            member_timeout_error_detail(member, now=datetime.now(UTC))
            if member is not None
            else None
        )
        if timeout_detail is not None and int(needed) & ~TIMEOUT_ALLOWED:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=timeout_detail,
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "MISSING_PERMISSIONS",
                "message": "You do not have permission to perform this action.",
                "permissions": str(int(needed)),
            },
        )
    # Import lazily so the permission calculator stays usable by AutoMod's
    # own exemption checks without a module cycle. Profile quarantine applies
    # only after the ordinary permission check succeeds, avoiding disclosure
    # of moderation state for an action the member could not perform anyway.
    from app.automod.service import require_member_interactions_allowed

    await require_member_interactions_allowed(session, guild, actor, needed)
    return permissions


async def require_can_manage_expression(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    *,
    creator_id: int,
    creator_domain: str,
) -> int:
    """Apply Discord's creator-aware guild-expression permission rule.

    Creators may edit or delete their own emoji, sticker, or sound with
    ``CREATE_GUILD_EXPRESSIONS``. Managing an expression created by someone
    else still requires ``MANAGE_GUILD_EXPRESSIONS`` (Kaede's published
    ``MANAGE_EMOJIS`` alias uses the same bit).
    """

    needed = (
        Permission.CREATE_GUILD_EXPRESSIONS
        if (creator_id, creator_domain) == (actor.id, actor.origin_domain)
        else Permission.MANAGE_GUILD_EXPRESSIONS
    )
    return await require_permissions(session, redis, guild, actor, needed)
