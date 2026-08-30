from __future__ import annotations

import re

from fastapi import HTTPException
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission
from app.db.models import Guild, GuildMember, MemberRole, Role
from app.federation.network import normalize_domain

ROLE_MENTION = re.compile(
    r"<@&(?P<id>[1-9][0-9]{0,18})@(?P<domain>[a-z0-9.-]{1,253})>", re.IGNORECASE
)
USER_MENTION = re.compile(
    r"<@(?P<id>[1-9][0-9]{0,18})(?:@(?P<domain>[a-z0-9.-]{1,253}))?>",
    re.IGNORECASE,
)
MAX_ROLE_MENTION_RECIPIENTS = 5_000


def role_mention_refs(content: object) -> list[tuple[int, str]]:
    """Return canonical, ordered role tokens from visible message text."""

    if not isinstance(content, str):
        return []
    return list(
        dict.fromkeys(
            (int(match.group("id")), normalize_domain(match.group("domain")))
            for match in ROLE_MENTION.finditer(content)
        )
    )


def syntactic_mention_count(content: object, *, default_domain: str) -> int:
    """Count visible unique user and role mention tokens for AutoMod.

    Role expansion is deliberately excluded: one ``@role`` is one mention,
    regardless of how many members receive the notification.
    """

    if not isinstance(content, str):
        return 0
    users = {
        (
            int(match.group("id")),
            normalize_domain(match.group("domain") or default_domain),
        )
        for match in USER_MENTION.finditer(content)
    }
    roles = {
        (int(match.group("id")), normalize_domain(match.group("domain")))
        for match in ROLE_MENTION.finditer(content)
    }
    return len(users) + len(roles)


async def role_mention_recipients(
    session: AsyncSession,
    guild: Guild,
    content: object,
    actor_permissions: int,
) -> list[tuple[int, str]]:
    """Validate role tokens and resolve only roles the actor may notify.

    Discord accepts a message containing an unmentionable role but does not
    notify that role unless the actor can mention everyone. Keeping the token
    valid while returning no recipients preserves that behavior.
    """

    requested = role_mention_refs(content)
    if not requested:
        return []
    if len(requested) > 25:
        raise HTTPException(status_code=400, detail={"code": "TOO_MANY_ROLE_MENTIONS"})
    if any(domain != guild.origin_domain for _, domain in requested):
        raise HTTPException(status_code=400, detail={"code": "INVALID_ROLE_MENTION"})

    roles = list(
        await session.scalars(
            select(Role).where(
                Role.guild_id == guild.id,
                Role.guild_domain == guild.origin_domain,
                tuple_(Role.id, Role.origin_domain).in_(requested),
            )
        )
    )
    role_by_ref = {(role.id, role.origin_domain): role for role in roles}
    if any(reference not in role_by_ref for reference in requested):
        raise HTTPException(status_code=400, detail={"code": "INVALID_ROLE_MENTION"})
    may_mention_all_roles = bool(
        actor_permissions & (Permission.ADMINISTRATOR | Permission.MENTION_EVERYONE)
    )
    notify_roles = [
        reference
        for reference in requested
        if may_mention_all_roles or role_by_ref[reference].mentionable
    ]

    recipients: set[tuple[int, str]] = set()
    if (guild.id, guild.origin_domain) in notify_roles:
        recipients.update(
            (
                await session.execute(
                    select(GuildMember.user_id, GuildMember.user_domain).where(
                        GuildMember.guild_id == guild.id,
                        GuildMember.guild_domain == guild.origin_domain,
                    )
                )
            ).tuples()
        )
    assigned_refs = [reference for reference in notify_roles if reference[0] != guild.id]
    if assigned_refs:
        recipients.update(
            (
                await session.execute(
                    select(MemberRole.user_id, MemberRole.user_domain).where(
                        MemberRole.guild_id == guild.id,
                        MemberRole.guild_domain == guild.origin_domain,
                        tuple_(MemberRole.role_id, MemberRole.role_domain).in_(assigned_refs),
                    )
                )
            ).tuples()
        )
    if len(recipients) > MAX_ROLE_MENTION_RECIPIENTS:
        raise HTTPException(status_code=400, detail={"code": "ROLE_MENTION_TOO_LARGE"})
    return sorted(recipients)


def merge_mention_recipients(
    explicit: list[tuple[int, str]], role_recipients: list[tuple[int, str]]
) -> list[tuple[int, str]]:
    return list(dict.fromkeys([*explicit, *role_recipients]))
