from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.custom_expressions import normalize_custom_expression_domain
from app.chat.rich_content import (
    Button,
    MessageLayoutComponent,
    PartialEmoji,
    PollCreate,
    StringSelect,
    walk_component_tree,
)
from app.core.permissions import Permission
from app.core.types import MAX_SNOWFLAKE
from app.db.bot_models import ApplicationEmoji, BotApplication
from app.db.models import Emoji, EmojiRoleRestriction, Guild, GuildMember, MemberRole, User

CUSTOM_EMOJI_PATTERN = re.compile(
    r"<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{2,32}):"
    r"(?P<id>[1-9][0-9]{0,18})@(?P<domain>[A-Za-z0-9.-]{1,253})>"
)

_VARIATION_SELECTORS = {"\ufe0e", "\ufe0f"}
_ZWJ = "\u200d"
_KEYCAP = "\u20e3"


def _is_emoji_base(codepoint: int) -> bool:
    # Unicode's emoji blocks plus the legacy BMP symbols which participate in
    # standardized emoji sequences. The surrounding sequence checks below
    # prevent arbitrary symbol/text runs from becoming a reaction key.
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or codepoint
        in {
            0x00A9,
            0x00AE,
            0x203C,
            0x2049,
            0x2122,
            0x2139,
            0x2194,
            0x2195,
            0x2196,
            0x2197,
            0x2198,
            0x2199,
            0x21A9,
            0x21AA,
            0x231A,
            0x231B,
            0x2328,
            0x23CF,
            0x24C2,
            0x25AA,
            0x25AB,
            0x25B6,
            0x25C0,
            0x25FB,
            0x25FC,
            0x25FD,
            0x25FE,
            0x3030,
            0x303D,
            0x3297,
            0x3299,
        }
    )


def _is_unicode_emoji_sequence(value: str) -> bool:
    codepoints = [ord(character) for character in value]
    if len(codepoints) == 2 and all(0x1F1E6 <= item <= 0x1F1FF for item in codepoints):
        return True
    if (
        len(codepoints) == 2
        and chr(codepoints[0]) in "#*0123456789"
        and codepoints[1] == ord(_KEYCAP)
    ):
        return True
    if (
        len(codepoints) >= 3
        and codepoints[0] == 0x1F3F4
        and codepoints[-1] == 0xE007F
        and all(0xE0061 <= item <= 0xE007A for item in codepoints[1:-1])
    ):
        return True
    segments = value.split(_ZWJ)
    if not segments or any(not segment for segment in segments):
        return False
    for segment in segments:
        points = [ord(character) for character in segment]
        if len(points) not in {1, 2} or not _is_emoji_base(points[0]):
            return False
        if len(points) == 2 and not 0x1F3FB <= points[1] <= 0x1F3FF:
            return False
    return True


def canonical_reaction_emoji(value: str) -> str:
    """Validate and canonicalize one Unicode or fully qualified custom emoji."""

    custom = CUSTOM_EMOJI_PATTERN.fullmatch(value)
    if custom is not None:
        try:
            domain = normalize_custom_expression_domain(custom.group("domain"))
        except ValueError as exc:
            raise ValueError("reaction custom emoji domain is invalid") from exc
        emoji_id = int(custom.group("id"))
        if emoji_id > MAX_SNOWFLAKE:
            raise ValueError("reaction custom emoji ID is invalid")
        animated = "a" if custom.group("animated") == "a" else ""
        return f"<{animated}:{custom.group('name')}:{emoji_id}@{domain}>"
    normalized = unicodedata.normalize("NFC", value)
    normalized = "".join(
        character for character in normalized if character not in _VARIATION_SELECTORS
    )
    if not normalized or not _is_unicode_emoji_sequence(normalized):
        raise ValueError("reaction must contain exactly one valid emoji")
    return normalized


def canonical_unicode_reaction_emoji(value: str) -> str:
    """Validate and canonicalize the Unicode-only reaction branch."""

    canonical = canonical_reaction_emoji(value)
    if CUSTOM_EMOJI_PATTERN.fullmatch(canonical) is not None:
        raise ValueError("reaction must use the custom emoji ID branch")
    return canonical


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
            domain = normalize_custom_expression_domain(match.group("domain"))
        except ValueError:
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
    trusted_external_domain: str | None = None,
) -> None:
    """Enforce source membership and the target's external-emoji permission."""

    for ref in custom_emoji_refs(content):
        name, animated = await _resolve_custom_emoji(
            session,
            actor,
            emoji_id=ref.id,
            emoji_domain=ref.origin_domain,
            supplied_name=ref.name,
            supplied_animated=ref.animated,
            target_guild=target_guild,
            target_permissions=target_permissions,
            trusted_external_domain=trusted_external_domain,
        )
        if name != ref.name or animated != ref.animated:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "CUSTOM_EMOJI_INVALID",
                    "message": "The custom emoji reference does not match the stored emoji.",
                },
            )


async def validate_custom_emoji_tokens(
    session: AsyncSession,
    actor: User,
    tokens: tuple[str, ...] | list[str],
    *,
    target_guild: Guild | None,
    target_permissions: Permission | int,
    trusted_external_domain: str | None = None,
) -> None:
    """Authorize an authenticated opaque-body routing projection."""

    for token in tokens:
        if CUSTOM_EMOJI_PATTERN.fullmatch(token) is None:
            raise HTTPException(status_code=400, detail={"code": "CUSTOM_EMOJI_INVALID"})
        await validate_custom_emoji_use(
            session,
            actor,
            token,
            target_guild=target_guild,
            target_permissions=target_permissions,
            trusted_external_domain=trusted_external_domain,
        )


async def _resolve_custom_emoji(
    session: AsyncSession,
    actor: User,
    *,
    emoji_id: int,
    emoji_domain: str,
    supplied_name: str | None,
    supplied_animated: bool | None,
    target_guild: Guild | None,
    target_permissions: Permission | int,
    trusted_external_domain: str | None,
) -> tuple[str, bool]:
    """Resolve one custom emoji and return authority-canonical metadata."""

    permissions = Permission(int(target_permissions))
    if getattr(actor, "account_type", "human") == "bot":
        application_emoji = await session.scalar(
            select(ApplicationEmoji)
            .join(
                BotApplication,
                (BotApplication.id == ApplicationEmoji.application_id)
                & (BotApplication.origin_domain == ApplicationEmoji.application_domain),
            )
            .where(
                ApplicationEmoji.id == emoji_id,
                ApplicationEmoji.application_domain == emoji_domain,
                BotApplication.bot_user_id == actor.id,
                BotApplication.bot_user_domain == actor.origin_domain,
            )
        )
        if application_emoji is not None:
            if not application_emoji.available:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "CUSTOM_EMOJI_UNAVAILABLE",
                        "message": "That application emoji is temporarily unavailable.",
                    },
                )
            if supplied_name is not None and application_emoji.name != supplied_name:
                raise HTTPException(status_code=400, detail={"code": "CUSTOM_EMOJI_INVALID"})
            if supplied_animated is not None and application_emoji.animated != supplied_animated:
                raise HTTPException(status_code=400, detail={"code": "CUSTOM_EMOJI_INVALID"})
            # Application-owned emoji are globally usable by their bot and do
            # not consume a guild's external-emoji permission.
            return application_emoji.name, bool(application_emoji.animated)

    emoji = await session.get(Emoji, (emoji_id, emoji_domain))
    is_target_emoji = target_guild is not None and emoji_domain == target_guild.origin_domain
    if emoji is None:
        # The actor's signed home may attest an emoji it owns but the guild
        # authority has not replicated. Never extend that trust to a third
        # instance supplied by the actor.
        if trusted_external_domain == emoji_domain and not is_target_emoji:
            if not permissions & Permission.USE_EXTERNAL_EMOJIS:
                raise HTTPException(
                    status_code=403, detail={"code": "USE_EXTERNAL_EMOJIS_REQUIRED"}
                )
            if supplied_name is None or supplied_animated is None:
                raise HTTPException(status_code=400, detail={"code": "CUSTOM_EMOJI_INVALID"})
            return supplied_name, supplied_animated
        raise HTTPException(status_code=400, detail={"code": "CUSTOM_EMOJI_NOT_FOUND"})
    if supplied_name is not None and emoji.name != supplied_name:
        raise HTTPException(status_code=400, detail={"code": "CUSTOM_EMOJI_INVALID"})
    if supplied_animated is not None and emoji.animated != supplied_animated:
        raise HTTPException(status_code=400, detail={"code": "CUSTOM_EMOJI_INVALID"})
    if not getattr(emoji, "available", True):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "CUSTOM_EMOJI_UNAVAILABLE",
                "message": "That custom emoji is temporarily unavailable.",
            },
        )
    if (
        target_guild is not None
        and (emoji.guild_id, emoji.guild_domain) != (target_guild.id, target_guild.origin_domain)
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
            status_code=403,
            detail={
                "code": "CUSTOM_EMOJI_SOURCE_ACCESS_REQUIRED",
                "message": "You must be a member of the emoji's guild to use it.",
            },
        )
    restricted = await session.scalar(
        select(EmojiRoleRestriction.role_id)
        .where(
            EmojiRoleRestriction.emoji_id == emoji.id,
            EmojiRoleRestriction.emoji_domain == emoji.origin_domain,
        )
        .limit(1)
    )
    if restricted is not None:
        entitled = await session.scalar(
            select(MemberRole.role_id)
            .join(
                EmojiRoleRestriction,
                (EmojiRoleRestriction.role_id == MemberRole.role_id)
                & (EmojiRoleRestriction.role_domain == MemberRole.role_domain)
                & (EmojiRoleRestriction.guild_id == MemberRole.guild_id)
                & (EmojiRoleRestriction.guild_domain == MemberRole.guild_domain),
            )
            .where(
                EmojiRoleRestriction.emoji_id == emoji.id,
                EmojiRoleRestriction.emoji_domain == emoji.origin_domain,
                MemberRole.user_id == actor.id,
                MemberRole.user_domain == actor.origin_domain,
            )
            .limit(1)
        )
        if entitled is None:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "CUSTOM_EMOJI_ROLE_REQUIRED",
                    "message": "One of this emoji's allowed roles is required to use it.",
                },
            )
    return emoji.name, bool(emoji.animated)


def rich_custom_emojis(
    components: list[MessageLayoutComponent] | None,
    poll: PollCreate | None,
) -> tuple[PartialEmoji, ...]:
    """Collect every emoji-bearing message component and poll answer."""

    result: list[PartialEmoji] = []
    for component in components or []:
        for node in walk_component_tree(component):
            if isinstance(node, Button) and node.emoji is not None:
                result.append(node.emoji)
            elif isinstance(node, StringSelect):
                result.extend(option.emoji for option in node.options if option.emoji is not None)
    if poll is not None:
        result.extend(
            answer.poll_media.emoji
            for answer in poll.answers
            if answer.poll_media.emoji is not None
        )
    return tuple(result)


async def resolve_rich_custom_emojis(
    session: AsyncSession,
    actor: User,
    *,
    components: list[MessageLayoutComponent] | None,
    poll: PollCreate | None,
    default_domain: str,
    target_guild: Guild | None,
    target_permissions: Permission | int,
    trusted_external_domain: str | None = None,
) -> None:
    """Authorize and canonicalize every custom emoji in a rich message body."""

    cache: dict[tuple[int, str, str | None, bool | None], tuple[str, bool]] = {}
    for partial in rich_custom_emojis(components, poll):
        if partial.id is None:
            continue
        emoji_id, emoji_domain = partial.id.resolve(default_domain)
        supplied_animated = partial.animated if "animated" in partial.model_fields_set else None
        cache_key = (emoji_id, emoji_domain, partial.name, supplied_animated)
        canonical = cache.get(cache_key)
        if canonical is None:
            canonical = await _resolve_custom_emoji(
                session,
                actor,
                emoji_id=emoji_id,
                emoji_domain=emoji_domain,
                supplied_name=partial.name,
                supplied_animated=supplied_animated,
                target_guild=target_guild,
                target_permissions=target_permissions,
                trusted_external_domain=trusted_external_domain,
            )
            cache[cache_key] = canonical
        partial.id = type(partial.id)(f"{emoji_id}@{emoji_domain}")
        partial.name, partial.animated = canonical
