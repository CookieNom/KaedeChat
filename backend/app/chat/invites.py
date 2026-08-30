from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.types import EntityRef
from app.db.models import Guild, GuildMember, Invite, MemberRole, Role


def invite_allows_user(invite: Invite, user_id: int, user_domain: str) -> bool:
    """Apply a targeted invite's exact composite-user allowlist."""

    return not invite.target_user_ids or f"{user_id}@{user_domain}" in invite.target_user_ids


def invite_target_payload(invite: Invite) -> dict[str, object]:
    """Render public invite targeting metadata without exposing its user allowlist."""

    return {
        "target_type": invite.target_type,
        "target_user_id": (
            f"{invite.target_user_id}@{invite.target_user_domain}"
            if invite.target_user_id is not None
            else None
        ),
        "scheduled_event_id": (
            f"{invite.scheduled_event_id}@{invite.scheduled_event_domain}"
            if invite.scheduled_event_id is not None
            else None
        ),
        "role_ids": list(invite.role_ids),
        "target_user_count": len(invite.target_user_ids),
    }


async def invite_roles(session: AsyncSession, guild: Guild, invite: Invite) -> list[Role]:
    """Resolve still-existing invite roles and fail closed on malformed stored refs."""

    role_ids: list[int] = []
    for raw_ref in invite.role_ids:
        try:
            role_id, role_domain = EntityRef(raw_ref).resolve(guild.origin_domain)
        except ValueError:
            return []
        if role_domain != guild.origin_domain or role_id == guild.id:
            return []
        role_ids.append(role_id)
    if not role_ids:
        return []
    roles = list(
        await session.scalars(
            select(Role).where(
                Role.guild_id == guild.id,
                Role.guild_domain == guild.origin_domain,
                Role.id.in_(role_ids),
                Role.origin_domain == guild.origin_domain,
            )
        )
    )
    by_id = {role.id: role for role in roles}
    return [by_id[role_id] for role_id in role_ids if role_id in by_id]


async def grant_invite_roles(
    session: AsyncSession,
    guild: Guild,
    member: GuildMember,
    invite: Invite,
) -> tuple[list[Role], list[Role]]:
    """Grant an invite's surviving roles and return ``(all, newly_granted)``."""

    roles = await invite_roles(session, guild, invite)
    if not roles:
        return [], []
    existing_ids = set(
        await session.scalars(
            select(MemberRole.role_id).where(
                MemberRole.guild_id == guild.id,
                MemberRole.guild_domain == guild.origin_domain,
                MemberRole.user_id == member.user_id,
                MemberRole.user_domain == member.user_domain,
                MemberRole.role_id.in_([role.id for role in roles]),
                MemberRole.role_domain == guild.origin_domain,
            )
        )
    )
    missing = [role for role in roles if role.id not in existing_ids]
    if missing:
        await session.execute(
            pg_insert(MemberRole)
            .values(
                [
                    {
                        "guild_id": guild.id,
                        "guild_domain": guild.origin_domain,
                        "user_id": member.user_id,
                        "user_domain": member.user_domain,
                        "role_id": role.id,
                        "role_domain": role.origin_domain,
                    }
                    for role in missing
                ]
            )
            .on_conflict_do_nothing()
        )
        # Discord-style temporary invite memberships become permanent as soon
        # as they receive any explicit role, including through a later invite.
        member.temporary = False
    return roles, missing
