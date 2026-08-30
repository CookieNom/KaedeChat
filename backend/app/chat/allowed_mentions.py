from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.channel_access import ChannelAccess
from app.chat.mention_policy import AllowedMentions
from app.chat.mentions import (
    MAX_ROLE_MENTION_RECIPIENTS,
    ROLE_MENTION,
    USER_MENTION,
    role_mention_recipients,
)
from app.chat.permissions import get_permissions
from app.chat.rich_content import message_automod_text
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.models import GuildMember, User

EVERYONE_MENTION = re.compile(r"(?<![A-Za-z0-9_])@(?:everyone|here)\b", re.IGNORECASE)


def allowed_mention_texts(
    content: str | None,
    components: Sequence[object],
) -> list[str]:
    """Return all visible mention-bearing text for models or stored JSON."""

    visible = message_automod_text(content, components=components)
    return [visible] if visible is not None else []


def contains_mention_tokens(
    content: str | None,
    components: Sequence[object],
) -> bool:
    """Return whether visible application text can notify any recipient."""

    return any(
        USER_MENTION.search(text) is not None
        or ROLE_MENTION.search(text) is not None
        or EVERYONE_MENTION.search(text) is not None
        for text in allowed_mention_texts(content, components)
    )


@dataclass(frozen=True, slots=True)
class MentionSelection:
    users: frozenset[tuple[int, str]]
    roles: frozenset[tuple[int, str]]
    everyone: bool


@dataclass(frozen=True, slots=True)
class ResolvedMentions:
    """Authenticated notification recipients plus searchable mention intent."""

    recipients: tuple[tuple[int, str], ...]
    roles: tuple[tuple[int, str], ...]
    everyone: bool
    role_recipients: tuple[tuple[int, str], ...] = ()
    user_recipients: tuple[tuple[int, str], ...] = ()


def visible_mention_refs(
    texts: Sequence[str],
    settings: Settings,
    *,
    default_domain: str | None = None,
) -> tuple[set[tuple[int, str]], set[tuple[int, str]]]:
    domain = default_domain or settings.domain
    users: set[tuple[int, str]] = set()
    roles: set[tuple[int, str]] = set()
    for text in texts:
        users.update(
            EntityRef(f"{match.group('id')}@{match.group('domain') or domain}").resolve(domain)
            for match in USER_MENTION.finditer(text)
        )
        roles.update(
            EntityRef(f"{match.group('id')}@{match.group('domain')}").resolve(domain)
            for match in ROLE_MENTION.finditer(text)
        )
    return users, roles


def selected_allowed_mentions(
    allowed: AllowedMentions | None,
    texts: Sequence[str],
    settings: Settings,
    *,
    default_domain: str | None = None,
) -> MentionSelection:
    domain = default_domain or settings.domain
    parse = {"users"} if allowed is None else set(allowed.parse)
    explicit_users = set() if allowed is None else {item.resolve(domain) for item in allowed.users}
    explicit_roles = set() if allowed is None else {item.resolve(domain) for item in allowed.roles}
    visible_users, visible_roles = visible_mention_refs(
        texts,
        settings,
        default_domain=domain,
    )
    users = visible_users if "users" in parse else visible_users & explicit_users
    roles = visible_roles if "roles" in parse else visible_roles & explicit_roles
    if len(users) > 100 or len(roles) > 100:
        raise HTTPException(status_code=400, detail={"code": "TOO_MANY_MENTIONS"})
    return MentionSelection(
        users=frozenset(users),
        roles=frozenset(roles),
        everyone="everyone" in parse
        and any(EVERYONE_MENTION.search(text) is not None for text in texts),
    )


async def resolve_selected_users(
    session: AsyncSession,
    access: ChannelAccess,
    selected: frozenset[tuple[int, str]],
) -> set[tuple[int, str]]:
    if not selected:
        return set()
    if access.guild is None:
        participant_refs = {(item.id, item.origin_domain) for item in access.participants}
        if not selected <= participant_refs:
            raise HTTPException(status_code=400, detail={"code": "INVALID_MENTION"})
        return set(selected)
    member_refs = set(
        (
            await session.execute(
                select(GuildMember.user_id, GuildMember.user_domain).where(
                    GuildMember.guild_id == access.guild.id,
                    GuildMember.guild_domain == access.guild.origin_domain,
                    tuple_(GuildMember.user_id, GuildMember.user_domain).in_(selected),
                )
            )
        ).tuples()
    )
    if member_refs != selected:
        raise HTTPException(status_code=400, detail={"code": "INVALID_MENTION"})
    return member_refs


async def selected_mention_permissions(
    session: AsyncSession,
    redis: Redis,
    access: ChannelAccess,
    actor: User,
    selection: MentionSelection,
    actor_permissions: int | None,
) -> Permission:
    if not selection.roles and not selection.everyone:
        return Permission(0)
    guild = access.guild
    if guild is None:
        raise HTTPException(status_code=409, detail={"code": "MENTION_CONTEXT_INVALID"})
    if actor_permissions is not None:
        return Permission(actor_permissions)
    return Permission(
        await get_permissions(
            session,
            redis,
            guild,
            actor,
            channel=access.channel,
        )
    )


async def everyone_mention_recipients(
    session: AsyncSession,
    access: ChannelAccess,
    permissions: Permission,
) -> set[tuple[int, str]]:
    if not permissions & (Permission.ADMINISTRATOR | Permission.MENTION_EVERYONE):
        # Discord renders the text but suppresses the notification when the
        # sender lacks the broad-mention permission.
        return set()
    guild = access.guild
    if guild is None:
        raise HTTPException(status_code=409, detail={"code": "MENTION_CONTEXT_INVALID"})
    everyone = set(
        (
            await session.execute(
                select(GuildMember.user_id, GuildMember.user_domain).where(
                    GuildMember.guild_id == guild.id,
                    GuildMember.guild_domain == guild.origin_domain,
                )
            )
        ).tuples()
    )
    if len(everyone) > MAX_ROLE_MENTION_RECIPIENTS:
        raise HTTPException(status_code=400, detail={"code": "ROLE_MENTION_TOO_LARGE"})
    return everyone


async def resolve_allowed_mentions_projection(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    allowed: AllowedMentions | None,
    content: str | None,
    components: Sequence[object],
    *,
    actor_permissions: int | None = None,
    replied_user_ref: tuple[int, str] | None = None,
) -> ResolvedMentions:
    """Resolve visible recipients under one webhook/interaction policy.

    Discord defaults application-authored messages to parsing user mentions
    only. Role and broad mentions require an explicit policy plus the
    corresponding channel permission. An invocation-time permission snapshot
    may be supplied for a user-installed application that has no guild member.
    """

    texts = allowed_mention_texts(content, components)
    selection = selected_allowed_mentions(allowed, texts, settings)
    user_recipients = await resolve_selected_users(session, access, selection.users)
    if allowed is not None and allowed.replied_user and replied_user_ref is not None:
        user_recipients.update(
            await resolve_selected_users(
                session,
                access,
                frozenset({replied_user_ref}),
            )
        )
    resolved = set(user_recipients)
    if access.guild is None:
        return ResolvedMentions(
            tuple(sorted(resolved)),
            (),
            False,
            user_recipients=tuple(sorted(user_recipients)),
        )
    guild = access.guild
    permissions = await selected_mention_permissions(
        session,
        redis,
        access,
        actor,
        selection,
        actor_permissions,
    )
    resolved_role_recipients: set[tuple[int, str]] = set()
    if selection.roles:
        selected_role_text = " ".join(
            f"<@&{role_id}@{domain}>" for role_id, domain in sorted(selection.roles)
        )
        resolved_role_recipients.update(
            await role_mention_recipients(session, guild, selected_role_text, int(permissions))
        )
        resolved.update(resolved_role_recipients)
    if selection.everyone:
        resolved.update(await everyone_mention_recipients(session, access, permissions))
    return ResolvedMentions(
        recipients=tuple(sorted(resolved)),
        roles=tuple(sorted(selection.roles)),
        everyone=selection.everyone,
        role_recipients=tuple(sorted(resolved_role_recipients)),
        user_recipients=tuple(sorted(user_recipients)),
    )


async def resolve_allowed_mentions(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    allowed: AllowedMentions | None,
    content: str | None,
    components: Sequence[object],
    *,
    actor_permissions: int | None = None,
    replied_user_ref: tuple[int, str] | None = None,
) -> tuple[tuple[int, str], ...]:
    """Compatibility projection for callers that need notification recipients only."""

    resolved = await resolve_allowed_mentions_projection(
        session,
        redis,
        settings,
        access,
        actor,
        allowed,
        content,
        components,
        actor_permissions=actor_permissions,
        replied_user_ref=replied_user_ref,
    )
    return resolved.recipients
