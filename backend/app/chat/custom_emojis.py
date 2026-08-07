from __future__ import annotations

import re
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission
from app.core.types import MAX_SNOWFLAKE
from app.db.models import Emoji, Guild, GuildMember, User
from app.federation.network import FederationNetworkError, normalize_domain

CUSTOM_EMOJI_PATTERN = re.compile(
    r"<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{2,32}):"
    r"(?P<id>[1-9][0-9]{0,18})@(?P<domain>[A-Za-z0-9.-]{1,253})>"
)


@dataclass(frozen=True, slots=True)
class CustomEmojiRef:
    id: int
    origin_domain: str
    name: str
    animated: bool

    @property
    def token(self) -> str:
        prefix = "a" if self.animated else ""
        return f"<{prefix}:{self.name}:{self.id}@{self.origin_domain}>"


def custom_emoji_refs(content: str | None) -> tuple[CustomEmojiRef, ...]:
    if not content:
        return ()
    refs: list[CustomEmojiRef] = []
    seen: set[tuple[int, str]] = set()
    for match in CUSTOM_EMOJI_PATTERN.finditer(content):
        try:
            domain = normalize_domain(match.group("domain"))
        except FederationNetworkError:
            continue
        emoji_id = int(match.group("id"))
        if emoji_id > MAX_SNOWFLAKE:
            continue
        ref = CustomEmojiRef(
            id=emoji_id,
            origin_domain=domain,
            name=match.group("name"),
            animated=match.group("animated") == "a",
        )
        identity = (ref.id, ref.origin_domain)
        if identity not in seen:
            seen.add(identity)
            refs.append(ref)
    return tuple(refs)


async def validate_custom_emoji_use(
    session: AsyncSession,
    actor: User,
    content: str | None,
    *,
    target_guild: Guild | None,
    target_permissions: Permission | int,
    trust_unknown_external: bool = False,
) -> None:
    """Enforce source membership and the target's external-emoji permission."""

    permissions = Permission(int(target_permissions))
    for ref in custom_emoji_refs(content):
        emoji = await session.get(Emoji, (ref.id, ref.origin_domain))
        is_target_emoji = (
            target_guild is not None and ref.origin_domain == target_guild.origin_domain
        )
        if emoji is None:
            # A guild authority does not replicate every third-party guild its
            # remote members belong to. The authenticated actor home vouches
            # for that source entitlement; target policy remains authoritative.
            if trust_unknown_external and not is_target_emoji:
                if not permissions & Permission.USE_EXTERNAL_EMOJIS:
                    raise HTTPException(
                        status_code=403, detail={"code": "USE_EXTERNAL_EMOJIS_REQUIRED"}
                    )
                continue
            raise HTTPException(status_code=400, detail={"code": "CUSTOM_EMOJI_NOT_FOUND"})
        if emoji.name != ref.name or emoji.animated != ref.animated:
            raise HTTPException(status_code=400, detail={"code": "CUSTOM_EMOJI_INVALID"})
        if (
            target_guild is not None
            and (emoji.guild_id, emoji.guild_domain)
            != (target_guild.id, target_guild.origin_domain)
            and not permissions & Permission.USE_EXTERNAL_EMOJIS
        ):
            raise HTTPException(status_code=403, detail={"code": "USE_EXTERNAL_EMOJIS_REQUIRED"})
        membership = await session.scalar(
            select(GuildMember.user_id).where(
                GuildMember.guild_id == emoji.guild_id,
                GuildMember.guild_domain == emoji.guild_domain,
                GuildMember.user_id == actor.id,
                GuildMember.user_domain == actor.origin_domain,
            )
        )
        if membership is None:
            raise HTTPException(
                status_code=403, detail={"code": "CUSTOM_EMOJI_SOURCE_ACCESS_REQUIRED"}
            )
