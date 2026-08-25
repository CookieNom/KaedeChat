from __future__ import annotations

import re
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Permission
from app.core.types import MAX_SNOWFLAKE
from app.db.models import Guild, GuildMember, Sticker, User
from app.federation.network import FederationNetworkError, normalize_domain

CUSTOM_STICKER_PATTERN = re.compile(
    r"<sticker:(?P<name>[A-Za-z0-9_]{2,32}):"
    r"(?P<id>[1-9][0-9]{0,18})@(?P<domain>[A-Za-z0-9.-]{1,253})>"
)


@dataclass(frozen=True, slots=True)
class CustomStickerRef:
    id: int
    origin_domain: str
    name: str

    @property
    def token(self) -> str:
        return f"<sticker:{self.name}:{self.id}@{self.origin_domain}>"


def custom_sticker_refs(content: str | None) -> tuple[CustomStickerRef, ...]:
    if not content:
        return ()
    refs: list[CustomStickerRef] = []
    seen: set[tuple[int, str]] = set()
    for match in CUSTOM_STICKER_PATTERN.finditer(content):
        try:
            domain = normalize_domain(match.group("domain"))
        except FederationNetworkError:
            continue
        sticker_id = int(match.group("id"))
        if sticker_id > MAX_SNOWFLAKE:
            continue
        ref = CustomStickerRef(sticker_id, domain, match.group("name"))
        if (ref.id, ref.origin_domain) not in seen:
            seen.add((ref.id, ref.origin_domain))
            refs.append(ref)
    return tuple(refs)


async def validate_custom_sticker_use(
    session: AsyncSession,
    actor: User,
    content: str | None,
    *,
    target_guild: Guild | None,
    target_permissions: Permission | int,
    trust_unknown_external: bool = False,
) -> None:
    permissions = Permission(int(target_permissions))
    refs = custom_sticker_refs(content)
    if refs and (len(refs) != 1 or content is None or content.strip() != refs[0].token):
        raise HTTPException(status_code=400, detail={"code": "CUSTOM_STICKER_INVALID"})
    for ref in refs:
        sticker = await session.get(Sticker, (ref.id, ref.origin_domain))
        is_target_sticker = (
            target_guild is not None and ref.origin_domain == target_guild.origin_domain
        )
        if sticker is None:
            if trust_unknown_external and not is_target_sticker:
                if not permissions & Permission.USE_EXTERNAL_EMOJIS:
                    raise HTTPException(
                        status_code=403, detail={"code": "USE_EXTERNAL_EMOJIS_REQUIRED"}
                    )
                continue
            raise HTTPException(status_code=400, detail={"code": "CUSTOM_STICKER_NOT_FOUND"})
        if sticker.name != ref.name:
            raise HTTPException(status_code=400, detail={"code": "CUSTOM_STICKER_INVALID"})
        if (
            target_guild is not None
            and (sticker.guild_id, sticker.guild_domain)
            != (target_guild.id, target_guild.origin_domain)
            and not permissions & Permission.USE_EXTERNAL_EMOJIS
        ):
            raise HTTPException(status_code=403, detail={"code": "USE_EXTERNAL_EMOJIS_REQUIRED"})
        membership = await session.scalar(
            select(GuildMember.user_id).where(
                GuildMember.guild_id == sticker.guild_id,
                GuildMember.guild_domain == sticker.guild_domain,
                GuildMember.user_id == actor.id,
                GuildMember.user_domain == actor.origin_domain,
            )
        )
        if membership is None:
            raise HTTPException(
                status_code=403, detail={"code": "CUSTOM_STICKER_SOURCE_ACCESS_REQUIRED"}
            )
