from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.custom_expressions import normalize_custom_expression_domain
from app.core.permissions import Permission
from app.core.settings import DOMAIN_RE
from app.core.types import MAX_SNOWFLAKE, EntityRef
from app.db.models import Guild, GuildMember, Sticker, User

CUSTOM_STICKER_PATTERN = re.compile(
    r"<sticker:(?P<name>[A-Za-z0-9_]{2,30}):"
    r"(?P<id>[1-9][0-9]{0,18})@(?P<domain>[A-Za-z0-9.-]{1,253})>"
)


def _valid_snapshot_name(value: str) -> bool:
    return (
        2 <= len(value) <= 30
        and value == value.strip()
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


@dataclass(frozen=True, slots=True)
class CustomStickerRef:
    id: int
    origin_domain: str
    name: str

    @property
    def token(self) -> str:
        return f"<sticker:{self.name}:{self.id}@{self.origin_domain}>"


def sticker_item_payload(sticker: Sticker) -> dict[str, object]:
    if not sticker.available or sticker.media_hash is None:
        raise HTTPException(status_code=400, detail={"code": "CUSTOM_STICKER_UNAVAILABLE"})
    return {
        "id": str(sticker.id),
        "origin_domain": sticker.origin_domain,
        "name": sticker.name,
        "format_type": 2 if sticker.animated else 1,
        "media_hash": sticker.media_hash,
    }


def validate_sticker_items(
    value: object,
    *,
    maximum: int = 3,
) -> list[dict[str, object]]:
    if maximum < 0 or maximum > 9:
        raise ValueError("sticker_items maximum is invalid")
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"sticker_items must be an array with at most {maximum} items")
    normalized: list[dict[str, object]] = []
    seen: set[tuple[int, str]] = set()
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {
            "id",
            "origin_domain",
            "name",
            "format_type",
            "media_hash",
        }:
            raise ValueError("sticker item has an invalid shape")
        try:
            sticker_id = int(str(raw["id"]))
            domain = str(raw["origin_domain"])
            name = str(raw["name"])
            format_type = int(raw["format_type"])
            media_hash = str(raw["media_hash"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("sticker item has invalid fields") from exc
        if (
            not 0 < sticker_id <= MAX_SNOWFLAKE
            or domain != domain.rstrip(".").lower()
            or not DOMAIN_RE.fullmatch(domain)
            or not _valid_snapshot_name(name)
            or format_type not in {1, 2, 3, 4}
            or not re.fullmatch(r"[0-9a-f]{64}", media_hash)
            or (sticker_id, domain) in seen
        ):
            raise ValueError("sticker item has invalid fields")
        seen.add((sticker_id, domain))
        normalized.append(
            {
                "id": str(sticker_id),
                "origin_domain": domain,
                "name": name,
                "format_type": format_type,
                "media_hash": media_hash,
            }
        )
    return normalized


async def resolve_sticker_items(
    session: AsyncSession,
    actor: User,
    sticker_ids: Sequence[EntityRef],
    *,
    default_domain: str,
    target_guild: Guild | None,
    target_permissions: Permission | int,
    maximum: int = 3,
) -> list[dict[str, object]]:
    permissions = Permission(int(target_permissions))
    resolved = [reference.resolve(default_domain) for reference in sticker_ids]
    if len(resolved) != len(set(resolved)) or len(resolved) > maximum:
        raise HTTPException(status_code=400, detail={"code": "CUSTOM_STICKER_INVALID"})
    items: list[dict[str, object]] = []
    for sticker_ref in resolved:
        sticker = await session.get(Sticker, sticker_ref)
        if sticker is None:
            raise HTTPException(status_code=400, detail={"code": "CUSTOM_STICKER_NOT_FOUND"})
        if (
            target_guild is not None
            and (sticker.guild_id, sticker.guild_domain)
            != (target_guild.id, target_guild.origin_domain)
            and not permissions & Permission.USE_EXTERNAL_STICKERS
        ):
            raise HTTPException(
                status_code=403,
                detail={"code": "USE_EXTERNAL_STICKERS_REQUIRED"},
            )
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
                status_code=403,
                detail={"code": "CUSTOM_STICKER_SOURCE_ACCESS_REQUIRED"},
            )
        items.append(sticker_item_payload(sticker))
    return validate_sticker_items(items, maximum=maximum)


def custom_sticker_refs(content: str | None) -> tuple[CustomStickerRef, ...]:
    if not content:
        return ()
    refs: list[CustomStickerRef] = []
    seen: set[tuple[int, str]] = set()
    for match in CUSTOM_STICKER_PATTERN.finditer(content):
        try:
            domain = normalize_custom_expression_domain(match.group("domain"))
        except ValueError:
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
                if not permissions & Permission.USE_EXTERNAL_STICKERS:
                    raise HTTPException(
                        status_code=403, detail={"code": "USE_EXTERNAL_STICKERS_REQUIRED"}
                    )
                continue
            raise HTTPException(status_code=400, detail={"code": "CUSTOM_STICKER_NOT_FOUND"})
        if sticker.name != ref.name:
            raise HTTPException(status_code=400, detail={"code": "CUSTOM_STICKER_INVALID"})
        if (
            target_guild is not None
            and (sticker.guild_id, sticker.guild_domain)
            != (target_guild.id, target_guild.origin_domain)
            and not permissions & Permission.USE_EXTERNAL_STICKERS
        ):
            raise HTTPException(status_code=403, detail={"code": "USE_EXTERNAL_STICKERS_REQUIRED"})
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
