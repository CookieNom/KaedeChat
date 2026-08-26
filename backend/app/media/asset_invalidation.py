from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from sqlalchemy import and_, exists, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.guild_revision import queue_guild_mutation
from app.chat.payloads import emoji_payload, role_payload, sticker_payload
from app.core.settings import Settings
from app.core.types import MAX_SNOWFLAKE
from app.db.models import Attachment, Emoji, Guild, MediaTombstoneSource, Role, Sticker, User
from app.federation.relationships import queue_friend_profile_updates
from app.media.digest_revocation import (
    DIGEST_REVOCATION_STATUSES,
    lock_asset_digest,
    valid_content_digest,
)


@dataclass(slots=True)
class TerminalAssetInvalidation:
    """Post-commit notifications prepared with one terminal media verdict."""

    user: User | None = None
    guild: Guild | None = None
    dispatch_type: str | None = None
    dispatch_payload: dict[str, object] | None = None
    friend_destinations: set[str] = field(default_factory=set)


def _binding_id(raw: str) -> int | None:
    if not raw or not raw.isascii() or not raw.isdecimal():
        return None
    parsed = int(raw)
    if not 0 <= parsed <= MAX_SNOWFLAKE or str(parsed) != raw:
        return None
    return parsed


async def _locked_user_nowait(session: AsyncSession, user_id: int, user_domain: str) -> User | None:
    return cast(
        User | None,
        await session.scalar(
            select(User)
            .where(User.id == user_id, User.origin_domain == user_domain)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        ),
    )


async def _locked_guild_nowait(
    session: AsyncSession, guild_id: int, guild_domain: str
) -> Guild | None:
    return cast(
        Guild | None,
        await session.scalar(
            select(Guild)
            .where(Guild.id == guild_id, Guild.origin_domain == guild_domain)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        ),
    )


async def _guild_owner(session: AsyncSession, settings: Settings, guild: Guild) -> User:
    owner = await session.get(User, (guild.owner_id, guild.owner_domain))
    if owner is None or not owner.is_local or owner.origin_domain != settings.domain:
        raise RuntimeError("local guild asset invalidation has no authoritative signer")
    return owner


async def _invalidate_user_asset(
    session: AsyncSession,
    settings: Settings,
    attachment: Attachment,
    parts: list[str],
) -> TerminalAssetInvalidation | None:
    if len(parts) != 4 or parts[1] != settings.domain or parts[3] not in {"avatar", "banner"}:
        return None
    user_id = _binding_id(parts[2])
    if user_id is None:
        return None
    user = await _locked_user_nowait(session, user_id, parts[1])
    if user is None:
        return None
    if not user.is_local:
        raise RuntimeError("local asset binding references a non-local user")
    field_name = "avatar_hash" if parts[3] == "avatar" else "banner_hash"
    if attachment.content_sha256 is None or getattr(user, field_name) != attachment.content_sha256:
        return None
    setattr(user, field_name, None)
    user.profile_version += 1
    destinations = await queue_friend_profile_updates(session, settings, user)
    return TerminalAssetInvalidation(user=user, friend_destinations=destinations)


async def _invalidate_guild_asset(
    session: AsyncSession,
    settings: Settings,
    attachment: Attachment,
    parts: list[str],
) -> TerminalAssetInvalidation | None:
    if len(parts) != 4 or parts[1] != settings.domain or parts[3] not in {"icon", "banner"}:
        return None
    guild_id = _binding_id(parts[2])
    if guild_id is None:
        return None
    guild = await _locked_guild_nowait(session, guild_id, parts[1])
    if guild is None:
        return None
    field_name = "icon_hash" if parts[3] == "icon" else "banner_hash"
    if attachment.content_sha256 is None or getattr(guild, field_name) != attachment.content_sha256:
        return None
    actor = await _guild_owner(session, settings, guild)
    setattr(guild, field_name, None)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.update",
        {
            "guild": {
                "id": str(guild.id),
                "origin_domain": guild.origin_domain,
                field_name: None,
            }
        },
    )
    return TerminalAssetInvalidation(
        guild=guild,
        dispatch_type="GUILD_UPDATE",
    )


async def _invalidate_emoji_asset(
    session: AsyncSession,
    settings: Settings,
    attachment: Attachment,
    parts: list[str],
) -> TerminalAssetInvalidation | None:
    if len(parts) != 3 or parts[1] != settings.domain:
        return None
    emoji_id = _binding_id(parts[2])
    if emoji_id is None:
        return None
    # Read the parent reference without a lock, then lock in the same
    # Guild-before-Emoji order used by ordinary emoji deletion. The attachment
    # row is already locked, so NOWAIT on the Guild prevents an inverse-order
    # asset mutation from turning this retryable worker collision into a
    # deadlock.
    candidate = await session.get(Emoji, (emoji_id, parts[1]))
    if candidate is None:
        return None
    guild = await _locked_guild_nowait(session, candidate.guild_id, candidate.guild_domain)
    if guild is None:
        return None
    emoji = await session.scalar(
        select(Emoji)
        .where(Emoji.id == emoji_id, Emoji.origin_domain == parts[1])
        .with_for_update(nowait=True)
        .execution_options(populate_existing=True)
    )
    if emoji is None or (emoji.guild_id, emoji.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        return None
    if attachment.content_sha256 is None or emoji.media_hash != attachment.content_sha256:
        return None
    actor = await _guild_owner(session, settings, guild)
    rendered = emoji_payload(emoji)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.emoji.delete",
        {"emoji": rendered},
    )
    await session.delete(emoji)
    return TerminalAssetInvalidation(
        guild=guild,
        dispatch_type="GUILD_EMOJI_DELETE",
        dispatch_payload=rendered,
    )


async def _invalidate_role_asset(
    session: AsyncSession,
    settings: Settings,
    attachment: Attachment,
    parts: list[str],
) -> TerminalAssetInvalidation | None:
    if len(parts) != 4 or parts[1] != settings.domain or parts[3] != "icon":
        return None
    role_id = _binding_id(parts[2])
    if role_id is None:
        return None
    candidate = await session.get(Role, (role_id, parts[1]))
    if candidate is None:
        return None
    guild = await _locked_guild_nowait(session, candidate.guild_id, candidate.guild_domain)
    if guild is None:
        return None
    role = await session.scalar(
        select(Role)
        .where(Role.id == role_id, Role.origin_domain == parts[1])
        .with_for_update(nowait=True)
        .execution_options(populate_existing=True)
    )
    if role is None or (role.guild_id, role.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        return None
    if attachment.content_sha256 is None or role.icon_hash != attachment.content_sha256:
        return None
    actor = await _guild_owner(session, settings, guild)
    role.icon_hash = None
    rendered = role_payload(role)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.role.update",
        {"role": rendered},
        snapshot_required=True,
    )
    return TerminalAssetInvalidation(
        guild=guild,
        dispatch_type="GUILD_ROLE_UPDATE",
        dispatch_payload=rendered,
    )


async def _invalidate_sticker_asset(
    session: AsyncSession,
    settings: Settings,
    attachment: Attachment,
    parts: list[str],
) -> TerminalAssetInvalidation | None:
    if len(parts) != 3 or parts[1] != settings.domain:
        return None
    sticker_id = _binding_id(parts[2])
    if sticker_id is None:
        return None
    candidate = await session.get(Sticker, (sticker_id, parts[1]))
    if candidate is None:
        return None
    guild = await _locked_guild_nowait(session, candidate.guild_id, candidate.guild_domain)
    if guild is None:
        return None
    sticker = await session.scalar(
        select(Sticker)
        .where(Sticker.id == sticker_id, Sticker.origin_domain == parts[1])
        .with_for_update(nowait=True)
        .execution_options(populate_existing=True)
    )
    if sticker is None or (sticker.guild_id, sticker.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        return None
    if attachment.content_sha256 is None or sticker.media_hash != attachment.content_sha256:
        return None
    actor = await _guild_owner(session, settings, guild)
    rendered = sticker_payload(sticker)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.sticker.delete",
        {"sticker": rendered},
    )
    await session.delete(sticker)
    return TerminalAssetInvalidation(
        guild=guild,
        dispatch_type="GUILD_STICKER_DELETE",
        dispatch_payload=rendered,
    )


async def _invalidate_one_terminal_asset_binding(
    session: AsyncSession,
    settings: Settings,
    attachment: Attachment,
) -> TerminalAssetInvalidation | None:
    binding = getattr(attachment, "asset_binding", None)
    if not isinstance(binding, str) or not binding:
        return None
    parts = binding.split(":")
    if parts[0] == "user":
        result = await _invalidate_user_asset(session, settings, attachment, parts)
    elif parts[0] == "guild":
        result = await _invalidate_guild_asset(session, settings, attachment, parts)
    elif parts[0] == "role":
        result = await _invalidate_role_asset(session, settings, attachment, parts)
    elif parts[0] == "emoji":
        result = await _invalidate_emoji_asset(session, settings, attachment, parts)
    elif parts[0] == "sticker":
        result = await _invalidate_sticker_asset(session, settings, attachment, parts)
    else:
        result = None
    # A terminal attachment must never remain publicly bound, including when a
    # legacy/malformed binding no longer resolves to an active projection.
    attachment.asset_binding = None
    return result


async def invalidate_terminal_asset_binding(
    session: AsyncSession,
    settings: Settings,
    attachment: Attachment,
) -> TerminalAssetInvalidation | None:
    """Remove the exact attachment projection in its terminal transaction.

    The caller owns this Attachment and the digest fence. Entity locks remain
    NOWAIT because ordinary asset mutation takes entity -> Attachment -> digest;
    a collision must roll back the retryable verdict rather than deadlock.
    """

    return await _invalidate_one_terminal_asset_binding(session, settings, attachment)


async def invalidate_terminal_digest_binding_batch(
    session: AsyncSession,
    settings: Settings,
    digest: str,
    *,
    limit: int = 25,
) -> tuple[list[TerminalAssetInvalidation], list[tuple[int, str]], int, bool]:
    """Repair a bounded batch of projections for retained terminal evidence.

    This post-verdict repair takes digest -> Attachment SKIP LOCKED -> entity
    NOWAIT. The terminal row and cleanup guards are the durable queue: callers
    re-enqueue while ``more`` is true, and the hourly tombstone sweep discovers
    a lost wake without making the safety verdict fanout transaction unbounded.

    Clean duplicate refs are neutrally terminalized and returned for a
    separate post-commit task. The terminal state is a durable claimed marker
    that prevents a fast retry from selecting the same first page forever.
    The follow-up task creates the signed deletion source in canonical
    media-ref -> Attachment -> try-digest order; doing it here would invert
    that order.
    """

    if not valid_content_digest(digest):
        raise ValueError("terminal asset digest is invalid")
    if not 1 <= limit <= 100:
        raise ValueError("terminal asset invalidation batch limit is invalid")
    await lock_asset_digest(session, digest)
    terminal_evidence = await session.scalar(
        select(Attachment.id)
        .where(
            Attachment.origin_domain == settings.domain,
            Attachment.content_sha256 == digest,
            Attachment.scan_status.in_(DIGEST_REVOCATION_STATUSES),
        )
        .limit(1)
    )
    if terminal_evidence is None:
        return [], [], 0, False
    missing_deletion_source = ~exists(
        select(MediaTombstoneSource.attachment_id).where(
            MediaTombstoneSource.attachment_id == Attachment.id,
            MediaTombstoneSource.attachment_domain == Attachment.origin_domain,
        )
    )
    repair_needed = or_(
        Attachment.asset_binding.is_not(None),
        and_(
            Attachment.purpose != "attachment",
            Attachment.scan_status == "clean",
            Attachment.deleted_at.is_(None),
            missing_deletion_source,
        ),
    )
    bound_attachments = list(
        await session.scalars(
            select(Attachment)
            .where(
                Attachment.origin_domain == settings.domain,
                Attachment.content_sha256 == digest,
                repair_needed,
            )
            .order_by(Attachment.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    )
    invalidations: list[TerminalAssetInvalidation] = []
    duplicate_purge_refs: list[tuple[int, str]] = []
    for bound_attachment in bound_attachments:
        invalidation = await _invalidate_one_terminal_asset_binding(
            session,
            settings,
            bound_attachment,
        )
        if invalidation is not None:
            invalidations.append(invalidation)
        if (
            bound_attachment.purpose != "attachment"
            and bound_attachment.scan_status == "clean"
            and bound_attachment.deleted_at is None
        ):
            # Do not copy the original moderation category. Every internal
            # terminal status renders to the same public ``rejected`` state;
            # this neutral marker says only that a retained same-digest verdict
            # made this bind-capable duplicate unsafe.
            bound_attachment.scan_status = "rejected"
            duplicate_purge_refs.append((bound_attachment.id, bound_attachment.origin_domain))
    await session.flush()
    remaining = select(Attachment.id).where(
        Attachment.origin_domain == settings.domain,
        Attachment.content_sha256 == digest,
        repair_needed,
    )
    processed_refs = [
        (bound_attachment.id, bound_attachment.origin_domain)
        for bound_attachment in bound_attachments
    ]
    if processed_refs:
        remaining = remaining.where(
            ~tuple_(Attachment.id, Attachment.origin_domain).in_(processed_refs)
        )
    more = await session.scalar(remaining.limit(1)) is not None
    return invalidations, duplicate_purge_refs, len(bound_attachments), more
