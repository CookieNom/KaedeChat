from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Guild, GuildMember, MemberRole, Role, User


async def guild_member(
    session: AsyncSession, guild: Guild, user_id: int, user_domain: str
) -> GuildMember:
    member = await session.scalar(
        select(GuildMember)
        .where(
            GuildMember.guild_id == guild.id,
            GuildMember.guild_domain == guild.origin_domain,
            GuildMember.user_id == user_id,
            GuildMember.user_domain == user_domain,
        )
        .with_for_update()
    )
    if member is None:
        raise HTTPException(status_code=404, detail={"code": "MEMBER_NOT_FOUND"})
    return member


async def guild_role(session: AsyncSession, guild: Guild, role_id: int) -> Role:
    role = await session.scalar(
        select(Role).where(
            Role.id == role_id,
            Role.origin_domain == guild.origin_domain,
            Role.guild_id == guild.id,
            Role.guild_domain == guild.origin_domain,
        )
    )
    if role is None:
        raise HTTPException(status_code=404, detail={"code": "ROLE_NOT_FOUND"})
    return role


async def member_roles(
    session: AsyncSession, guild: Guild, user_id: int, user_domain: str
) -> list[Role]:
    return list(
        await session.scalars(
            select(Role)
            .outerjoin(
                MemberRole,
                (MemberRole.role_id == Role.id)
                & (MemberRole.role_domain == Role.origin_domain)
                & (MemberRole.guild_id == guild.id)
                & (MemberRole.guild_domain == guild.origin_domain)
                & (MemberRole.user_id == user_id)
                & (MemberRole.user_domain == user_domain),
            )
            .where(
                Role.guild_id == guild.id,
                Role.guild_domain == guild.origin_domain,
                or_(Role.id == guild.id, MemberRole.role_id.is_not(None)),
            )
        )
    )


def role_rank(role: Role) -> tuple[int, int]:
    return role.position, -role.id


async def highest_role(session: AsyncSession, guild: Guild, user_id: int, user_domain: str) -> Role:
    roles = await member_roles(session, guild, user_id, user_domain)
    return max(roles, key=role_rank)


async def require_can_manage_role(
    session: AsyncSession, guild: Guild, actor: User, role: Role
) -> None:
    if (guild.owner_id, guild.owner_domain) == (actor.id, actor.origin_domain):
        return
    actor_role = await highest_role(session, guild, actor.id, actor.origin_domain)
    if role_rank(actor_role) <= role_rank(role):
        raise HTTPException(status_code=403, detail={"code": "ROLE_HIERARCHY"})


async def require_can_manage_member(
    session: AsyncSession, guild: Guild, actor: User, target_id: int, target_domain: str
) -> GuildMember:
    target = await guild_member(session, guild, target_id, target_domain)
    if (target_id, target_domain) == (guild.owner_id, guild.owner_domain):
        raise HTTPException(status_code=403, detail={"code": "OWNER_IMMUNE"})
    if (target_id, target_domain) == (actor.id, actor.origin_domain):
        raise HTTPException(status_code=403, detail={"code": "CANNOT_MANAGE_SELF"})
    if (guild.owner_id, guild.owner_domain) == (actor.id, actor.origin_domain):
        return target
    actor_role = await highest_role(session, guild, actor.id, actor.origin_domain)
    target_role = await highest_role(session, guild, target_id, target_domain)
    if role_rank(actor_role) <= role_rank(target_role):
        raise HTTPException(status_code=403, detail={"code": "ROLE_HIERARCHY"})
    return target
