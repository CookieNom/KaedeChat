from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import HTTPException
from sqlalchemy import delete, exists, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from app.automod.schemas import AutoModActionInput, AutoModRuleCreate
from app.chat.custom_emojis import (
    canonical_reaction_emoji,
    canonical_unicode_reaction_emoji,
)
from app.chat.e2ee import (
    channel_encryption_policy_payload,
    validate_channel_encryption_policy,
    validate_channel_encryption_policy_transition,
    validate_e2ee_envelope,
    validate_e2ee_message_projection,
    validate_e2ee_message_revision,
    validate_message_encryption_policy,
)
from app.chat.e2ee_controls import apply_e2ee_control_metadata
from app.chat.e2ee_membership import (
    GUILD_E2EE_ACCESS_MUTATION_EVENTS,
    pause_guild_e2ee_for_membership_change,
)
from app.chat.message_flags import (
    MESSAGE_FLAG_HAS_SNAPSHOT,
    MESSAGE_FLAG_IS_COMPONENTS_V2,
    MESSAGE_FLAG_IS_CROSSPOST,
    MESSAGE_FLAG_SOURCE_MESSAGE_DELETED,
    MESSAGE_FLAG_SUPPRESS_EMBEDS,
)
from app.chat.message_references import (
    validate_channel_follow_message_fields,
    validate_message_reference_projection,
)
from app.chat.payloads import member_payload, render_message_payload, rich_thread_member_payload
from app.chat.permissions import calculate_permissions
from app.chat.pins import (
    CHANNEL_PIN_LIMIT,
    PIN_NOTICE_MESSAGE_TYPE,
    channel_pin_count,
    channel_pins_update_payload,
    message_is_pinnable,
)
from app.chat.poll_results import (
    POLL_RESULT_MESSAGE_TYPE,
    validate_poll_result_wire_body,
)
from app.chat.reaction_payloads import reaction_emoji_payload, reaction_event_payload
from app.core.channel_types import GUILD_CHANNEL_TYPES, GUILD_VOICE_CHANNEL_TYPES
from app.core.federation import GUILD_MUTATION_EVENT_TYPES
from app.core.permissions import ALL_PERMISSIONS, Permission
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.models import (
    Attachment,
    Ban,
    Channel,
    ChannelOverwrite,
    Emoji,
    EmojiRoleRestriction,
    Guild,
    GuildEvent,
    GuildMember,
    MemberRole,
    Message,
    MessageProjection,
    MessageView,
    Pin,
    Poll,
    PollAnswer,
    PollVote,
    Reaction,
    ReadState,
    RemoteGuildMembershipIntent,
    RemoteMediaCache,
    Role,
    Sticker,
    TerminalRoomDeletion,
    ThreadMember,
    TrackerBoard,
    User,
)
from app.federation.client import signed_request
from app.federation.events import message_attachment_refs
from app.federation.identity_storage import FederationIdentityQuotaExceeded
from app.federation.message_content import (
    validate_replicated_rich_projection,
    validate_webhook_attribution,
)
from app.federation.network import (
    FederationInstanceQuotaExceeded,
    FederationNetworkError,
    decode_federation_response_json,
    normalize_domain,
)
from app.federation.relationships import (
    GUILD_PROFILE_RELAY_EVENT,
    guild_profile_member_payload,
    validated_guild_profile_source,
)
from app.federation.replica_storage import (
    FederationReplicaQuotaExceeded,
    admit_replica_storage,
    mark_replica_capacity_paused,
    mark_replica_quota_paused,
    reconcile_replica_storage,
)
from app.federation.replication import (
    advance_channel_cursor,
    database_snowflake,
    profile_from_user,
    replicate_message_attachments,
    replicated_message_create_fingerprint,
    resolve_delegated_profile,
    upsert_remote_user,
    validate_snowflake_timestamp,
)
from app.federation.schemas import RemoteUserProfile
from app.federation.security import validated_event_envelope
from app.federation.terminal_rooms import lock_terminal_room
from app.federation.tracker import apply_tracker_invalidation
from app.media.digest_revocation import valid_content_digest
from app.media.tombstones import lock_media_tombstone_ref
from app.scheduled_events.recurrence import validate_recurrence_projection


class GuildSequenceGap(RuntimeError):
    def __init__(self, expected: int, received: int) -> None:
        self.expected = expected
        self.received = received
        super().__init__(f"guild sequence gap: expected {expected}, received {received}")


HISTORY_ACCESS_MUTATION_EVENT_TYPES = frozenset(
    {
        "guild.update",
        "guild.channel.create",
        "guild.channel.update",
        "guild.channel.delete",
        "guild.thread.member.upsert",
        "guild.thread.member.delete",
        "guild.role.create",
        "guild.role.update",
        "guild.role.delete",
        "guild.overwrite.upsert",
        "guild.overwrite.delete",
        "guild.member.update",
        "guild.member.remove",
        "guild.members.origin.remove",
        "guild.member.role.add",
        "guild.member.role.remove",
        "guild.ban.add",
    }
)

SNAPSHOT_NEUTRAL_GUILD_EVENTS = frozenset(
    {
        "guild.message.create",
        "guild.message.committed",
        "guild.message.update",
        "guild.message.delete",
        "guild.message.bulk_delete",
        "guild.message.purge",
        "guild.reaction.add",
        "guild.reaction.remove",
        "guild.reaction.clear",
        "guild.poll.vote.add",
        "guild.poll.vote.remove",
        "guild.poll.finalize",
        "guild.pin.add",
        "guild.pin.remove",
        "guild.stage.instance.create",
        "guild.stage.instance.update",
        "guild.stage.instance.delete",
        "guild.scheduled_event.create",
        "guild.scheduled_event.update",
        "guild.scheduled_event.delete",
        "guild.scheduled_event.user.add",
        "guild.scheduled_event.user.remove",
        "guild.soundboard.sound.create",
        "guild.soundboard.sound.update",
        "guild.voice_channel_status.update",
        "guild.voice_channel_start_time.update",
        "guild.soundboard.sound.delete",
        "guild.soundboard.sounds.update",
        "guild.automod.rule.create",
        "guild.automod.rule.update",
        "guild.automod.rule.delete",
        "guild.automod.execution",
        # Tracker content has its own version-fenced snapshot protocol and is
        # deliberately absent from the structural guild snapshot. Ordered
        # invalidations must not restart unrelated member-page snapshots.
        "guild.tracker.board.invalidate",
    }
)

MAX_SNAPSHOT_PAGES = 100
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_MEMBERS = 100_000
MAX_SNAPSHOT_MEMBER_ROLES = 500_000
MAX_SNAPSHOT_OVERWRITES = 100_000
MAX_SNAPSHOT_THREAD_MEMBERS = 100_000
MAX_ORPHANED_REPLICA_PURGE = 100
MAX_GUILD_SYNC_PAGES = 100
MAX_GUILD_SYNC_EVENTS = 100_000
MAX_GUILD_SYNC_BYTES = 64 * 1024 * 1024
MAX_GUILD_SYNC_SECONDS = 25.0
REMOTE_GUILD_DEPARTED = "departed"
REMOTE_GUILD_JOINING = "joining"
REMOTE_GUILD_JOIN_INTENT_LIMIT_PER_USER = 1_000
REMOTE_GUILD_JOIN_INTENT_TTL_HOURS = 24
REMOTE_GUILD_JOIN_INTENT_GC_BATCH_SIZE = 10_000


def expected_channel_parent_types(channel_type: int) -> frozenset[int]:
    """Return the only parent channel types accepted by Discord's channel model."""

    if channel_type == 10:
        return frozenset({5})
    if channel_type == 11:
        return frozenset({0, 15})
    if channel_type == 12:
        return frozenset({0})
    return frozenset({4})


def local_guild_membership_exists(local_domain: str) -> ColumnElement[bool]:
    """Return a correlated predicate proving this instance can access a guild."""

    return exists().where(
        GuildMember.guild_id == Guild.id,
        GuildMember.guild_domain == Guild.origin_domain,
        GuildMember.user_domain == local_domain,
    )


def _remote_membership_intent_key(
    settings: Settings,
    guild_id: int,
    guild_domain: str,
    user_id: int,
    user_domain: str,
) -> tuple[int, str, int, str]:
    normalized_guild_domain = normalize_domain(guild_domain)
    normalized_user_domain = normalize_domain(user_domain)
    if normalized_guild_domain == settings.domain:
        raise ValueError("remote guild membership intent references a local guild")
    if normalized_user_domain != settings.domain:
        raise ValueError("remote guild membership intent must target a local user")
    return guild_id, normalized_guild_domain, user_id, normalized_user_domain


async def mark_remote_guild_departed(
    session: AsyncSession,
    settings: Settings,
    *,
    guild_id: int,
    guild_domain: str,
    user_id: int,
    user_domain: str,
) -> None:
    """Persist a local departure before removing the replicated membership."""

    key = _remote_membership_intent_key(settings, guild_id, guild_domain, user_id, user_domain)
    existing = await session.get(RemoteGuildMembershipIntent, key)
    if existing is not None:
        existing.state = REMOTE_GUILD_DEPARTED
        await session.flush()
        return
    await session.execute(
        pg_insert(RemoteGuildMembershipIntent)
        .values(
            guild_id=key[0],
            guild_domain=key[1],
            user_id=key[2],
            user_domain=key[3],
            user_is_local=True,
            state=REMOTE_GUILD_DEPARTED,
        )
        .on_conflict_do_update(
            index_elements=(
                RemoteGuildMembershipIntent.guild_id,
                RemoteGuildMembershipIntent.guild_domain,
                RemoteGuildMembershipIntent.user_id,
                RemoteGuildMembershipIntent.user_domain,
            ),
            set_={
                "state": REMOTE_GUILD_DEPARTED,
                "updated_at": func.now(),
            },
        )
    )


async def begin_remote_guild_join(
    session: AsyncSession,
    settings: Settings,
    *,
    guild_id: int,
    guild_domain: str,
    user_id: int,
    user_domain: str,
) -> bool:
    """Record an explicit local join before asking the remote authority.

    Existing memberships do not need an intent.  New joins and rejoins do, so
    an unrelated background snapshot cannot be mistaken for user consent.
    The caller commits this marker before making the remote request.
    """

    key = _remote_membership_intent_key(settings, guild_id, guild_domain, user_id, user_domain)
    member = await session.get(GuildMember, key)
    intent = await session.get(RemoteGuildMembershipIntent, key)
    if member is not None and intent is None:
        return False
    if intent is None or intent.state != REMOTE_GUILD_JOINING:
        # A local account can deliberately join many remote guilds, but a
        # broken/malicious invite authority must not turn failed attempts into
        # unbounded no-Guild-FK state. Serialize the per-user admission check;
        # the caller commits this short transaction before peer I/O.
        await session.scalar(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(
                        f"kaede-remote-guild-join-intents:{key[3]}:{key[2]}",
                        0,
                    )
                )
            )
        )
        active_join_intents = int(
            await session.scalar(
                select(func.count())
                .select_from(RemoteGuildMembershipIntent)
                .where(
                    RemoteGuildMembershipIntent.user_id == key[2],
                    RemoteGuildMembershipIntent.user_domain == key[3],
                    RemoteGuildMembershipIntent.state == REMOTE_GUILD_JOINING,
                )
            )
            or 0
        )
        if active_join_intents >= REMOTE_GUILD_JOIN_INTENT_LIMIT_PER_USER:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "KAED_FED_REMOTE_GUILD_JOIN_LIMIT",
                    "message": (
                        "Too many remote guild joins are still pending. "
                        "Wait for an earlier attempt to finish, then retry."
                    ),
                },
            )
    if intent is not None:
        intent.state = REMOTE_GUILD_JOINING
        await session.flush()
        return True
    await session.execute(
        pg_insert(RemoteGuildMembershipIntent)
        .values(
            guild_id=key[0],
            guild_domain=key[1],
            user_id=key[2],
            user_domain=key[3],
            user_is_local=True,
            state=REMOTE_GUILD_JOINING,
        )
        .on_conflict_do_update(
            index_elements=(
                RemoteGuildMembershipIntent.guild_id,
                RemoteGuildMembershipIntent.guild_domain,
                RemoteGuildMembershipIntent.user_id,
                RemoteGuildMembershipIntent.user_domain,
            ),
            set_={
                "state": REMOTE_GUILD_JOINING,
                "updated_at": func.now(),
            },
        )
    )
    return True


async def _locked_remote_membership_intents(
    session: AsyncSession,
    settings: Settings,
    *,
    guild_id: int,
    guild_domain: str,
) -> dict[tuple[int, str], RemoteGuildMembershipIntent]:
    if normalize_domain(guild_domain) == settings.domain:
        raise ValueError("membership intents do not apply to local guilds")
    rows = list(
        await session.scalars(
            select(RemoteGuildMembershipIntent)
            .where(
                RemoteGuildMembershipIntent.guild_id == guild_id,
                RemoteGuildMembershipIntent.guild_domain == guild_domain,
                RemoteGuildMembershipIntent.user_domain == settings.domain,
            )
            .with_for_update()
        )
    )
    return {(row.user_id, row.user_domain): row for row in rows}


def filter_remote_snapshot_memberships(
    snapshot: dict[str, Any],
    intents: dict[tuple[int, str], RemoteGuildMembershipIntent],
    *,
    local_domain: str,
    required_member: tuple[int, str] | None,
    existing_required_member: bool,
    existing_local_members: set[tuple[int, str]] | None = None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    RemoteGuildMembershipIntent | None,
]:
    """Apply local membership intent to a validated remote snapshot."""

    required_intent: RemoteGuildMembershipIntent | None = None
    if required_member is not None:
        if normalize_domain(required_member[1]) != local_domain:
            raise ValueError("joining member does not belong to this local instance")
        required_member = (required_member[0], local_domain)
        required_intent = intents.get(required_member)
        if required_intent is None and not existing_required_member:
            raise ValueError("guild snapshot join lacks an explicit local join intent")
        if required_intent is not None and required_intent.state != REMOTE_GUILD_JOINING:
            raise ValueError("departed remote guild membership requires an explicit rejoin")

    existing_local_members = existing_local_members or set()
    permitted_local_refs = set(existing_local_members)
    permitted_local_refs.update(
        ref for ref, intent in intents.items() if intent.state == REMOTE_GUILD_JOINING
    )
    blocked_member_refs = {
        (int(raw["user"]["id"]), str(raw["user"]["origin_domain"]))
        for raw in snapshot["members"]
        if str(raw["user"]["origin_domain"]) == local_domain
        and (int(raw["user"]["id"]), str(raw["user"]["origin_domain"])) not in permitted_local_refs
    }
    blocked_member_refs.update(
        ref for ref, intent in intents.items() if intent.state == REMOTE_GUILD_DEPARTED
    )
    members = [
        raw
        for raw in snapshot["members"]
        if (int(raw["user"]["id"]), str(raw["user"]["origin_domain"])) not in blocked_member_refs
    ]
    member_roles = [
        raw
        for raw in snapshot["member_roles"]
        if (int(raw["user_id"]), str(raw["user_domain"])) not in blocked_member_refs
    ]
    overwrites = [
        raw
        for raw in snapshot["overwrites"]
        if raw["target_type"] != "member"
        or (int(raw["target_id"]), str(raw["target_domain"])) not in blocked_member_refs
    ]
    return members, member_roles, overwrites, required_intent


async def complete_remote_guild_join(
    session: AsyncSession,
    intent: RemoteGuildMembershipIntent,
) -> None:
    """Clear a pending rejoin only after its authoritative snapshot applied."""

    await session.delete(intent)


def stale_remote_guild_membership_intent_candidates(
    *,
    now: datetime | None = None,
    limit: int = REMOTE_GUILD_JOIN_INTENT_GC_BATCH_SIZE,
) -> Select[tuple[RemoteGuildMembershipIntent]]:
    """Select bounded, expired local consent markers for deletion.

    Missing intent is fail-closed for every new local membership, so neither a
    departed marker nor a failed join attempt needs to live indefinitely.
    """

    if limit < 1 or limit > REMOTE_GUILD_JOIN_INTENT_GC_BATCH_SIZE:
        raise ValueError("remote guild membership intent cleanup limit is invalid")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("remote guild membership intent cleanup time must be timezone-aware")
    cutoff = current - timedelta(hours=REMOTE_GUILD_JOIN_INTENT_TTL_HOURS)
    return (
        select(RemoteGuildMembershipIntent)
        .where(RemoteGuildMembershipIntent.updated_at < cutoff)
        .order_by(
            RemoteGuildMembershipIntent.updated_at,
            RemoteGuildMembershipIntent.user_domain,
            RemoteGuildMembershipIntent.user_id,
            RemoteGuildMembershipIntent.guild_domain,
            RemoteGuildMembershipIntent.guild_id,
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


async def purge_stale_remote_guild_membership_intents(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = REMOTE_GUILD_JOIN_INTENT_GC_BATCH_SIZE,
) -> int:
    """Remove one bounded batch of expired join/departure decisions."""

    intents = list(
        await session.scalars(stale_remote_guild_membership_intent_candidates(now=now, limit=limit))
    )
    for intent in intents:
        await session.delete(intent)
    return len(intents)


def replicated_guild_sync_candidates(
    local_domain: str,
    *,
    limit: int = 100,
) -> Select[tuple[str, int]]:
    """Select stale replicas that still serve at least one local account."""

    return (
        select(Guild.origin_domain, Guild.id)
        .where(
            Guild.origin_domain != local_domain,
            Guild.sync_status.in_(("stale", "failed")),
            local_guild_membership_exists(local_domain),
        )
        .order_by(Guild.origin_domain, Guild.id)
        .limit(limit)
    )


def guild_event_requires_snapshot(event: dict[str, Any]) -> bool:
    context = event.get("context")
    return isinstance(context, dict) and context.get("snapshot_required") is True


def _advance_snapshot_generation(
    guild: Guild,
    event: dict[str, Any],
    *,
    event_type: str,
) -> None:
    """Advance the structural watermark carried by an ordered guild event.

    Older peers do not send this watermark, so a receiver derives one locally.
    Newer peers bind it into the signed event context. Message-only mutations do
    not alter snapshot structure and therefore must not invalidate a member page.
    """

    if event_type in SNAPSHOT_NEUTRAL_GUILD_EVENTS:
        return
    context = event.get("context")
    raw_generation = context.get("snapshot_generation") if isinstance(context, dict) else None
    current_generation = int(getattr(guild, "snapshot_generation", 1) or 1)
    if raw_generation is None:
        guild.snapshot_generation = current_generation + 1
        return
    generation = database_snowflake(raw_generation, "snapshot generation")
    if generation != current_generation + 1:
        raise ValueError("snapshot generation is not the next structural revision")
    guild.snapshot_generation = generation


def guild_snapshot_rate_scope(guild_id: int, snapshot_generation: int, *, paginated: bool) -> str:
    """Limit repeated reads without throttling a newly-required revision."""

    mode = "page" if paginated else "start"
    return f"guild-snapshot-{mode}:{guild_id}:{snapshot_generation}"


async def mark_guild_replica_stale(
    session: AsyncSession,
    settings: Settings,
    guild_id: int,
    guild_domain: str,
    required_seq: int,
) -> bool:
    """Persist a resync marker unless another worker already caught up."""

    if guild_domain == settings.domain:
        return False
    guild = await session.scalar(
        select(Guild)
        .where(Guild.id == guild_id, Guild.origin_domain == guild_domain)
        .with_for_update()
    )
    if guild is None or guild.last_event_seq >= required_seq:
        return False
    guild.sync_status = "stale"
    return True


def guild_event_channel_ref(event: dict[str, Any]) -> tuple[int, str] | None:
    """Extract a channel scope used for permission-filtered delivery."""

    context = event.get("context")
    if isinstance(context, dict) and context.get("channel_id") is not None:
        try:
            return (
                database_snowflake(context.get("channel_id"), "guild event channel id"),
                normalize_domain(str(context.get("channel_domain", ""))),
            )
        except (FederationNetworkError, TypeError, ValueError):
            return None
    content = event.get("content")
    raw_message = content.get("message") if isinstance(content, dict) else None
    if isinstance(raw_message, dict) and raw_message.get("channel_id") is not None:
        try:
            return (
                database_snowflake(raw_message.get("channel_id"), "guild event channel id"),
                normalize_domain(str(raw_message.get("channel_domain", ""))),
            )
        except (FederationNetworkError, TypeError, ValueError):
            return None
    return None


def guild_history_requires_snapshot(
    *, after_seq: int, latest_seq: int, first_retained_seq: int | None
) -> bool:
    """Return whether retained events cannot continue the requester's cursor."""

    if after_seq > latest_seq:
        return True
    if first_retained_seq is not None:
        return first_retained_seq != after_seq + 1
    return after_seq < latest_seq


async def remote_destinations_with_channel_access(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    channel: Channel,
) -> set[str]:
    remote_users = list(
        await session.scalars(
            select(User)
            .join(
                GuildMember,
                (GuildMember.user_id == User.id) & (GuildMember.user_domain == User.origin_domain),
            )
            .where(
                GuildMember.guild_id == guild.id,
                GuildMember.guild_domain == guild.origin_domain,
                User.origin_domain != settings.domain,
            )
        )
    )
    destinations: set[str] = set()
    for user in remote_users:
        permissions, _member = await calculate_permissions(session, guild, user, channel=channel)
        if permissions & Permission.VIEW_CHANNEL:
            destinations.add(user.origin_domain)
    return destinations


async def lock_current_guild(session: AsyncSession, guild: Guild) -> Guild:
    """Flush caller changes, then lock and refresh the authoritative guild row."""

    await session.flush()
    locked = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise RuntimeError("guild disappeared while locking its authoritative state")
    return locked


async def assign_guild_sequence(session: AsyncSession, guild: Guild) -> int:
    locked = await lock_current_guild(session, guild)
    seq = locked.next_event_seq
    locked.next_event_seq += 1
    locked.last_event_seq = seq
    return seq


def new_guild_event_id() -> str:
    return f"kcge_{secrets.token_urlsafe(24)}"


def store_guild_event(
    session: AsyncSession,
    guild: Guild,
    seq: int,
    event_id: str,
    envelope: dict[str, Any],
) -> None:
    session.add(
        GuildEvent(
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            seq=seq,
            event_id=event_id,
            envelope=envelope,
        )
    )


def _validated_guild_message_mentions(
    raw: dict[str, Any],
    guild: Guild,
) -> tuple[list[tuple[int, str]], list[dict[str, str]], list[dict[str, str]], bool]:
    """Validate the complete authoritative mention projection once."""

    raw_users = raw.get("mention_user_refs", [])
    if not isinstance(raw_users, list) or len(raw_users) > 5_000:
        raise ValueError("guild message mention list is invalid")
    user_pairs: list[tuple[int, str]] = []
    for item in raw_users:
        if not isinstance(item, dict):
            raise ValueError("guild message mention reference is invalid")
        user_pairs.append(
            (
                database_snowflake(item.get("id"), "mentioned user id"),
                normalize_domain(str(item.get("origin_domain", ""))),
            )
        )
    if len(user_pairs) != len(set(user_pairs)):
        raise ValueError("guild message mentions must be unique")

    raw_roles = raw.get("mention_role_refs", [])
    if not isinstance(raw_roles, list) or len(raw_roles) > 100:
        raise ValueError("guild message role mention list is invalid")
    role_pairs: list[tuple[int, str]] = []
    for item in raw_roles:
        if not isinstance(item, dict):
            raise ValueError("guild message role mention reference is invalid")
        role_pairs.append(
            (
                database_snowflake(item.get("id"), "mentioned role id"),
                normalize_domain(str(item.get("origin_domain", ""))),
            )
        )
    if len(role_pairs) != len(set(role_pairs)) or any(
        domain != guild.origin_domain for _, domain in role_pairs
    ):
        raise ValueError("guild message role mentions are invalid")
    everyone = raw.get("mention_everyone", False)
    if not isinstance(everyone, bool):
        raise ValueError("guild message everyone mention marker is invalid")
    return (
        user_pairs,
        [{"id": str(user_id), "origin_domain": domain} for user_id, domain in user_pairs],
        [{"id": str(role_id), "origin_domain": domain} for role_id, domain in role_pairs],
        everyone,
    )


async def apply_guild_message_event(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    event: dict[str, Any],
) -> Message | None:
    locked = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
    )
    if locked is None:
        raise ValueError("replicated guild disappeared")
    guild = locked
    context = event.get("context")
    if not isinstance(context, dict):
        raise ValueError("guild message context is invalid")
    authority_guild_version = _event_datetime(
        context.get("guild_version"),
        "guild version",
        optional=True,
    )
    seq = database_snowflake(event.get("seq") or context.get("seq"), "guild sequence")
    expected = guild.last_event_seq + 1
    stale_replay = seq <= guild.last_event_seq
    if not stale_replay and seq != expected:
        guild.sync_status = "stale"
        raise GuildSequenceGap(expected, seq)
    raw = event["content"]["message"]
    # Authority events carry a strict federation profile alongside the rendered
    # client message. The embedded client author intentionally serializes
    # revision counters as decimal strings, so it is not a RemoteUserProfile.
    # Retain the embedded fallback only for legacy events without the sibling
    # federation projection.
    author_raw = event["content"].get("author") or raw.get("author")
    if not isinstance(author_raw, dict):
        raise ValueError("guild message author profile is missing")
    author = await resolve_delegated_profile(
        session,
        settings,
        RemoteUserProfile.model_validate(author_raw),
        authority_origin=guild.origin_domain,
    )
    if (str(author.id), author.origin_domain) != (
        str(raw["author_id"]),
        str(raw["author_domain"]),
    ):
        raise ValueError("guild event author mismatch")
    message_id = database_snowflake(raw.get("id"), "message id")
    channel_id = database_snowflake(raw.get("channel_id"), "channel id")
    message_origin = str(raw.get("origin_domain"))
    channel_domain = str(raw.get("channel_domain"))
    if message_origin != guild.origin_domain or channel_domain != guild.origin_domain:
        raise ValueError("guild message references a non-authoritative origin")
    channel = await session.get(Channel, (channel_id, channel_domain))
    if (
        channel is None
        or channel.unavailable
        or (channel.guild_id, channel.guild_domain)
        != (
            guild.id,
            guild.origin_domain,
        )
    ):
        raise ValueError("guild message channel does not belong to the guild")
    message_type = raw.get("message_type", 0)
    if isinstance(message_type, bool) or not isinstance(message_type, int) or message_type < 0:
        raise ValueError("guild message type is invalid")
    flags = raw.get("flags", 0)
    if isinstance(flags, bool) or not isinstance(flags, int) or flags < 0:
        raise ValueError("guild message flags are invalid")
    is_crosspost = bool(flags & MESSAGE_FLAG_IS_CROSSPOST)
    is_poll_result = message_type == POLL_RESULT_MESSAGE_TYPE and raw.get("poll_result") is not None
    is_pin_notice = message_type == PIN_NOTICE_MESSAGE_TYPE
    is_follow_notice = message_type == 12
    if is_crosspost and message_type != 0:
        raise ValueError("announcement crosspost message type is invalid")
    membership = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, author.id, author.origin_domain),
    )
    if membership is None and not stale_replay and not is_crosspost and not is_poll_result:
        raise ValueError("guild message author is not a guild member")
    event_type = event.get("type")
    event_actor = event.get("actor")
    if not isinstance(event_actor, dict):
        raise ValueError("guild message event actor is missing")
    event_actor_ref = (
        database_snowflake(event_actor.get("id"), "guild event actor id"),
        str(event_actor.get("domain")),
    )
    if event_type == "guild.message.create":
        if is_crosspost:
            if event_actor_ref[1] != guild.origin_domain and event_actor_ref != (
                guild.owner_id,
                guild.owner_domain,
            ):
                raise ValueError("announcement event actor is not authoritative")
        elif event_actor_ref not in {
            (author.id, author.origin_domain),
            (guild.owner_id, guild.owner_domain),
        }:
            raise ValueError("guild message event actor does not match its author")
    elif event_type == "guild.message.committed":
        if event_actor_ref != (guild.owner_id, guild.owner_domain):
            raise ValueError("guild commit event actor does not match its owner")
    else:
        raise ValueError("unsupported guild message event type")
    content = raw.get("content")
    e2ee = validate_e2ee_envelope(raw.get("e2ee"))
    raw_attachments = raw.get("attachments", [])
    if not isinstance(raw_attachments, list):
        raise ValueError("guild message attachment list is invalid")
    if content is not None and (not isinstance(content, str) or not 1 <= len(content) <= 4000):
        raise ValueError("guild message content is invalid")
    if content is not None and e2ee is not None:
        raise ValueError("guild message mixes plaintext and encrypted content")
    if (
        content is None
        and e2ee is None
        and not raw_attachments
        and not raw.get("embeds")
        and not raw.get("components")
        and not raw.get("sticker_items")
        and raw.get("poll") is None
        and raw.get("forwarded_message_id") is None
        and not is_pin_notice
    ):
        raise ValueError(
            "guild message requires content, an attachment, rich content, or a forward"
        )
    if not is_poll_result and not is_pin_notice:
        validate_message_encryption_policy(
            channel.encryption_mode or "plaintext",
            content=content,
            e2ee=e2ee,
            attachment_count=len(raw_attachments),
            policy_generation=channel.encryption_policy_generation or 0,
            policy_epoch=channel.encryption_epoch,
            policy_group_id=channel.encryption_group_id,
        )
    if e2ee is not None and e2ee.get("operation") not in {"welcome", "commit"}:
        validate_e2ee_message_projection(
            e2ee,
            message_id=message_id,
            message_domain=message_origin,
            edited=False,
        )
    tts = raw.get("tts", False)
    client_nonce = raw.get("client_nonce")
    if not isinstance(tts, bool):
        raise ValueError("guild message TTS marker is invalid")
    if client_nonce is not None and (
        not isinstance(client_nonce, str) or not 1 <= len(client_nonce) <= 64
    ):
        raise ValueError("guild message client nonce is invalid")
    if is_pin_notice and (
        content is not None
        or e2ee is not None
        or raw_attachments
        or raw.get("embeds", []) != []
        or raw.get("components", []) != []
        or raw.get("sticker_items", []) != []
        or raw.get("poll") is not None
        or raw.get("message_snapshots", []) != []
        or raw.get("application_id") is not None
        or raw.get("application_domain") is not None
        or raw.get("interaction_metadata") is not None
        or raw.get("forwarded_message_id") is not None
        or raw.get("forwarded_message_domain") is not None
        or flags != 0
        or tts
        or client_nonce is not None
    ):
        raise ValueError("guild pin notice fields are invalid")
    webhook = validate_webhook_attribution(
        raw.get("webhook"),
        message_type=message_type,
        message_origin=message_origin,
        label="guild message",
    )
    webhook_id = webhook.webhook_ref[0] if webhook is not None else None
    webhook_domain = webhook.webhook_ref[1] if webhook is not None else None
    webhook_name = webhook.name if webhook is not None else None
    webhook_avatar_hash = webhook.avatar_hash if webhook is not None else None
    webhook_avatar_url = webhook.avatar_url if webhook is not None else None
    if raw.get("edited_at") is not None or raw.get("deleted_at") is not None:
        raise ValueError("guild create event contains mutation timestamps")
    mention_pairs, mention_refs, mention_role_refs, mention_everyone = (
        _validated_guild_message_mentions(raw, guild)
    )
    if is_pin_notice and (mention_pairs or mention_role_refs or mention_everyone):
        raise ValueError("guild pin notice cannot contain mentions")
    if not stale_replay and not is_poll_result:
        for user_id, user_domain in mention_pairs:
            mentioned_member = await session.get(
                GuildMember,
                (guild.id, guild.origin_domain, user_id, user_domain),
            )
            if mentioned_member is None:
                raise ValueError("guild message mentions a user outside the guild")
    referenced_id_raw = raw.get("referenced_message_id")
    referenced_domain_raw = raw.get("referenced_message_domain")
    if (referenced_id_raw is None) != (referenced_domain_raw is None):
        raise ValueError("guild message reference is incomplete")
    referenced: Message | None = None
    referenced_id: int | None = None
    referenced_domain: str | None = None
    wire_referenced_ref: tuple[int, str] | None = None
    if referenced_id_raw is not None:
        candidate_id = database_snowflake(referenced_id_raw, "referenced message id")
        candidate_domain = normalize_domain(str(referenced_domain_raw))
        wire_referenced_ref = (candidate_id, candidate_domain)
        if candidate_domain != guild.origin_domain:
            raise ValueError("guild message reference has a non-authoritative origin")
        referenced = await session.get(Message, (candidate_id, candidate_domain))
        # Structural snapshots intentionally do not backfill pre-join history.
        # Preserve the FK invariant by omitting an otherwise-valid unavailable
        # historical reference; live references are retained normally.
        if referenced is not None:
            if (referenced.channel_id, referenced.channel_domain) != (
                channel.id,
                channel.origin_domain,
            ):
                raise ValueError("guild message reference is outside the channel")
            referenced_id = candidate_id
            referenced_domain = candidate_domain
        elif is_poll_result:
            referenced_id = candidate_id
            referenced_domain = candidate_domain
    if message_type == 19 and referenced_id_raw is None:
        raise ValueError("guild reply message is missing its reference")
    if is_pin_notice and referenced_id_raw is None:
        raise ValueError("guild pin notice is missing its source message")
    if is_pin_notice and referenced is None and not stale_replay:
        raise ValueError("guild pin notice source binding is invalid")
    if is_pin_notice and referenced is not None and not message_is_pinnable(referenced):
        raise ValueError("guild pin notice source is not pinnable")
    if is_poll_result and referenced_id_raw is None:
        raise ValueError("guild poll result is missing its reference")
    created_at = datetime.fromisoformat(str(raw["created_at"]))
    validate_snowflake_timestamp(
        message_id,
        created_at,
        "guild message",
        event_timestamp_ms=int(event["ts"]),
    )
    rich = _validated_message_rich_projection(
        raw,
        message_id=message_id,
        message_origin=message_origin,
        message_created_at=created_at,
        e2ee=e2ee,
        message_type=message_type,
        flags=flags,
    )
    if is_poll_result:
        projection, _embed = validate_poll_result_wire_body(
            raw,
            author_ref=(author.id, author.origin_domain),
            channel_ref=(channel.id, channel.origin_domain),
        )
        source_poll = (
            await session.get(Poll, (referenced.id, referenced.origin_domain))
            if referenced is not None
            else None
        )
        if referenced is not None and (
            source_poll is None
            or (referenced.author_id, referenced.author_domain) != (author.id, author.origin_domain)
            or ("e2ee" if referenced.e2ee is not None else "plaintext")
            != projection["source_encryption_mode"]
        ):
            raise ValueError("guild poll result source binding is invalid")
        if source_poll is not None:
            if source_poll.finalized_at is None:
                source_poll.finalized_at = created_at
            elif source_poll.finalized_at > created_at:
                raise ValueError("guild poll result predates source finalization")
    application_ref = cast(tuple[int, str] | None, rich["application_ref"])
    forwarded_ref = cast(tuple[int, str] | None, rich["forwarded_ref"])
    forwarded_channel_ref = cast(tuple[int, str] | None, rich["forwarded_channel_ref"])
    forward_snapshot = cast(dict[str, Any] | None, rich["forward_snapshot"])
    message_reference = validate_message_reference_projection(
        raw.get("message_reference"),
        message_type=message_type,
        channel_ref=(channel.id, channel.origin_domain),
        guild_ref=(guild.id, guild.origin_domain),
        referenced_message_ref=wire_referenced_ref,
        forwarded_message_ref=forwarded_ref,
        forwarded_channel_ref=forwarded_channel_ref,
        has_forward_snapshot=bool(
            forward_snapshot is not None or rich.get("has_encrypted_forward")
        ),
        is_crosspost=is_crosspost,
        label="guild message",
    )
    validate_channel_follow_message_fields(
        raw,
        rich,
        message_type=message_type,
        channel_type=channel.type,
        content=content,
        e2ee=e2ee,
        attachments=raw_attachments,
        webhook=webhook,
        mention_user_refs=mention_pairs,
        mention_role_refs=mention_role_refs,
        mention_everyone=mention_everyone,
        flags=flags,
        tts=tts,
        client_nonce=client_nonce,
        referenced_message_ref=wire_referenced_ref,
    )
    if is_follow_notice:
        if message_reference is None:
            raise RuntimeError("validated channel follow notice lost its source")
        source_channel_ref = (
            int(cast(str, message_reference["channel_id"])),
            cast(str, message_reference["channel_domain"]),
        )
        source_guild_ref = (
            int(cast(str, message_reference["guild_id"])),
            cast(str, message_reference["guild_domain"]),
        )
        known_source = await session.get(Channel, source_channel_ref)
        if (
            known_source is not None
            and not known_source.unavailable
            and (
                known_source.type != 5
                or (known_source.guild_id, known_source.guild_domain) != source_guild_ref
            )
        ):
            raise ValueError("channel follow notice source does not match its channel")
    if bool(flags & MESSAGE_FLAG_HAS_SNAPSHOT) != (
        forward_snapshot is not None or bool(rich.get("has_encrypted_forward"))
    ):
        raise ValueError("guild message snapshot flag does not match its forward projection")
    if is_crosspost and (forwarded_ref is None or forwarded_channel_ref is None):
        raise ValueError("announcement crosspost projection is invalid")
    poll_projection = cast(
        tuple[
            dict[str, object],
            list[tuple[int, str | None, dict[str, object] | None]],
            bool,
            int,
            datetime,
        ]
        | None,
        rich["poll"],
    )
    if poll_projection is not None:
        raw_poll = raw.get("poll")
        raw_results = raw_poll.get("results") if isinstance(raw_poll, dict) else None
        raw_counts = raw_results.get("answer_counts") if isinstance(raw_results, dict) else None
        if (
            not isinstance(raw_results, dict)
            or raw_results.get("is_finalized") is not False
            or raw_poll.get("finalized_at") is not None
            or not isinstance(raw_counts, list)
            or any(
                not isinstance(item, dict)
                or item.get("count") != 0
                or item.get("me_voted") is not False
                for item in raw_counts
            )
        ):
            raise ValueError("guild message create contains mutable poll results")
    inserted = await session.scalar(
        pg_insert(Message)
        .values(
            id=message_id,
            origin_domain=message_origin,
            channel_id=channel_id,
            channel_domain=channel_domain,
            author_id=author.id,
            author_domain=author.origin_domain,
            content=content,
            e2ee=e2ee,
            embeds=cast(list[dict[str, Any]], rich["embeds"]),
            components=cast(list[dict[str, Any]], rich["components"]),
            sticker_items=cast(list[dict[str, Any]], rich["sticker_items"]),
            application_id=application_ref[0] if application_ref is not None else None,
            application_domain=application_ref[1] if application_ref is not None else None,
            interaction_metadata=cast(
                dict[str, object] | None,
                rich["interaction_metadata"],
            ),
            view_version=cast(int, rich["view_version"]),
            forwarded_message_id=forwarded_ref[0] if forwarded_ref is not None else None,
            forwarded_message_domain=forwarded_ref[1] if forwarded_ref is not None else None,
            forwarded_channel_id=(
                forwarded_channel_ref[0] if forwarded_channel_ref is not None else None
            ),
            forwarded_channel_domain=(
                forwarded_channel_ref[1] if forwarded_channel_ref is not None else None
            ),
            forward_snapshot=forward_snapshot,
            poll_result=cast(dict[str, Any] | None, rich["poll_result"]),
            encryption_policy_generation=channel.encryption_policy_generation,
            encryption_epoch=channel.encryption_epoch,
            message_type=message_type,
            tts=tts,
            flags=flags,
            client_nonce=client_nonce,
            referenced_message_id=referenced_id,
            referenced_message_domain=referenced_domain,
            message_reference=message_reference,
            mention_user_refs=mention_refs,
            mention_role_refs=mention_role_refs,
            mention_everyone=mention_everyone,
            webhook_id=webhook_id,
            webhook_domain=webhook_domain,
            webhook_name=webhook_name,
            webhook_avatar_hash=webhook_avatar_hash,
            webhook_avatar_url=webhook_avatar_url,
            created_at=created_at,
        )
        .on_conflict_do_nothing(index_elements=["id", "origin_domain"])
        .returning(Message.id)
    )
    if not stale_replay:
        guild.last_event_seq = seq
        guild.next_event_seq = seq + 1
        guild.sync_status = "ready"
    if inserted is None:
        existing = await session.get(Message, (message_id, message_origin))
        if (
            existing is None
            or replicated_message_create_fingerprint(
                channel_id=existing.channel_id,
                channel_domain=existing.channel_domain,
                author_id=existing.author_id,
                author_domain=existing.author_domain,
                content=existing.content,
                e2ee=existing.e2ee,
                message_type=existing.message_type,
                tts=bool(existing.tts),
                flags=existing.flags,
                client_nonce=existing.client_nonce,
                referenced_message_id=existing.referenced_message_id,
                referenced_message_domain=existing.referenced_message_domain,
                message_reference=existing.message_reference,
                mention_user_refs=existing.mention_user_refs,
                mention_role_refs=existing.mention_role_refs,
                mention_everyone=bool(existing.mention_everyone),
                webhook_id=existing.webhook_id,
                webhook_domain=existing.webhook_domain,
                webhook_name=existing.webhook_name,
                webhook_avatar_hash=existing.webhook_avatar_hash,
                webhook_avatar_url=existing.webhook_avatar_url,
                embeds=list(existing.embeds or []),
                components=list(existing.components or []),
                sticker_items=list(existing.sticker_items or []),
                application_id=existing.application_id,
                application_domain=existing.application_domain,
                interaction_metadata=existing.interaction_metadata,
                view_version=int(existing.view_version or 0),
                forwarded_message_id=existing.forwarded_message_id,
                forwarded_message_domain=existing.forwarded_message_domain,
                forwarded_channel_id=existing.forwarded_channel_id,
                forwarded_channel_domain=existing.forwarded_channel_domain,
                forward_snapshot=existing.forward_snapshot,
                poll_result=existing.poll_result,
                created_at=existing.created_at,
            )
            != replicated_message_create_fingerprint(
                channel_id=channel.id,
                channel_domain=channel.origin_domain,
                author_id=author.id,
                author_domain=author.origin_domain,
                content=content,
                e2ee=e2ee,
                message_type=message_type,
                tts=tts,
                flags=flags,
                client_nonce=client_nonce,
                referenced_message_id=referenced_id,
                referenced_message_domain=referenced_domain,
                message_reference=message_reference,
                mention_user_refs=mention_refs,
                mention_role_refs=mention_role_refs,
                mention_everyone=mention_everyone,
                webhook_id=webhook_id,
                webhook_domain=webhook_domain,
                webhook_name=webhook_name,
                webhook_avatar_hash=webhook_avatar_hash,
                webhook_avatar_url=webhook_avatar_url,
                embeds=cast(list[dict[str, Any]], rich["embeds"]),
                components=cast(list[dict[str, Any]], rich["components"]),
                sticker_items=cast(list[dict[str, Any]], rich["sticker_items"]),
                application_id=application_ref[0] if application_ref is not None else None,
                application_domain=application_ref[1] if application_ref is not None else None,
                interaction_metadata=cast(
                    dict[str, object] | None,
                    rich["interaction_metadata"],
                ),
                view_version=cast(int, rich["view_version"]),
                forwarded_message_id=forwarded_ref[0] if forwarded_ref is not None else None,
                forwarded_message_domain=forwarded_ref[1] if forwarded_ref is not None else None,
                forwarded_channel_id=(
                    forwarded_channel_ref[0] if forwarded_channel_ref is not None else None
                ),
                forwarded_channel_domain=(
                    forwarded_channel_ref[1] if forwarded_channel_ref is not None else None
                ),
                forward_snapshot=forward_snapshot,
                poll_result=cast(dict[str, Any] | None, rich["poll_result"]),
                created_at=created_at,
            )
            or not await _stored_poll_matches_projection(session, existing, poll_projection)
            or not await _stored_encrypted_view_matches_projection(session, existing, rich)
        ):
            raise ValueError("guild message snowflake conflicts with another message")
        await apply_e2ee_control_metadata(
            session,
            existing,
            event["content"].get("e2ee_control"),
            expected_authority=guild.origin_domain,
        )
        await replicate_message_attachments(
            session,
            settings,
            existing,
            author,
            raw_attachments,
            allowed_attachment_origins=(
                {author.origin_domain, message_origin} if is_crosspost else {author.origin_domain}
            ),
        )
        await advance_channel_cursor(session, channel, message_id, message_origin)
        if not stale_replay and authority_guild_version is not None:
            guild.updated_at = authority_guild_version
        return None
    message = await session.get(Message, (message_id, message_origin))
    if message is None:
        raise RuntimeError("replicated guild message disappeared")
    if poll_projection is not None:
        question, answers, allow_multiselect, layout_type, expiry = poll_projection
        session.add(
            Poll(
                message_id=message.id,
                message_domain=message.origin_domain,
                question=question,
                allow_multiselect=allow_multiselect,
                layout_type=layout_type,
                expires_at=expiry,
                created_at=created_at,
            )
        )
        for answer_id, text, emoji in answers:
            session.add(
                PollAnswer(
                    message_id=message.id,
                    message_domain=message.origin_domain,
                    answer_id=answer_id,
                    text=text,
                    emoji=emoji,
                )
            )
    if bool(rich.get("has_encrypted_controls")):
        if application_ref is None:
            raise ValueError("encrypted guild message view is missing its application")
        installation_ref = cast(
            tuple[int, str] | None,
            rich.get("interaction_installation_ref"),
        )
        if installation_ref is None:
            raise ValueError("encrypted guild message view is missing its installation")
        session.add(
            MessageView(
                message_id=message.id,
                message_domain=message.origin_domain,
                application_id=application_ref[0],
                application_domain=application_ref[1],
                integration_type=cast(str, rich["interaction_integration_type"]),
                installation_id=installation_ref[0],
                installation_domain=installation_ref[1],
                installation_revision=cast(
                    int,
                    rich["interaction_installation_revision"],
                ),
                version=cast(int, rich["view_version"]),
                persistent=cast(bool, rich["view_persistent"]),
                expires_at=cast(datetime | None, rich["view_expires_at"]),
            )
        )
    await apply_e2ee_control_metadata(
        session,
        message,
        event["content"].get("e2ee_control"),
        expected_authority=guild.origin_domain,
    )
    await replicate_message_attachments(
        session,
        settings,
        message,
        author,
        raw_attachments,
        allowed_attachment_origins=(
            {author.origin_domain, message_origin} if is_crosspost else {author.origin_domain}
        ),
    )
    session.add(
        MessageProjection(
            message_id=message.id,
            message_domain=message.origin_domain,
            channel_id=message.channel_id,
            channel_domain=message.channel_domain,
            mention_user_refs=mention_refs,
        )
    )
    raw_thread_starter = event["content"].get("thread_starter", False)
    if not isinstance(raw_thread_starter, bool):
        raise ValueError("guild message thread starter marker is invalid")
    if channel.type in {10, 11, 12}:
        if raw_thread_starter:
            if channel.starter_message_id is not None and (
                channel.starter_message_id,
                channel.starter_message_domain,
            ) != (message.id, message.origin_domain):
                raise ValueError("thread starter identity conflicts with channel state")
            channel.starter_message_id = message.id
            channel.starter_message_domain = message.origin_domain
        else:
            channel.message_count = int(channel.message_count or 0) + 1
            channel.total_message_sent = int(channel.total_message_sent or 0) + 1
        channel.last_activity_at = created_at
        thread_member = await session.get(
            ThreadMember,
            (channel.id, channel.origin_domain, author.id, author.origin_domain),
        )
        if thread_member is None and int(channel.member_count or 0) < 1000:
            session.add(
                ThreadMember(
                    thread_id=channel.id,
                    thread_domain=channel.origin_domain,
                    guild_id=guild.id,
                    guild_domain=guild.origin_domain,
                    user_id=author.id,
                    user_domain=author.origin_domain,
                    joined_at=created_at,
                    flags=0,
                    notification_level="inherit",
                )
            )
            channel.member_count = int(channel.member_count or 0) + 1
    await advance_channel_cursor(session, channel, message.id, message.origin_domain)
    if not stale_replay and authority_guild_version is not None:
        guild.updated_at = authority_guild_version
    return message


async def refresh_replicated_thread_cursor(
    session: AsyncSession,
    thread: Channel,
) -> None:
    latest = await session.scalar(
        select(Message)
        .where(
            Message.channel_id == thread.id,
            Message.channel_domain == thread.origin_domain,
            Message.deleted_at.is_(None),
        )
        .order_by(Message.created_at.desc(), Message.id.desc(), Message.origin_domain.desc())
        .limit(1)
    )
    if latest is not None:
        thread.last_message_id = latest.id
        thread.last_message_domain = latest.origin_domain
        return
    # Parent source messages are type-21 projections, never FK-backed child
    # cursors.
    thread.last_message_id = None
    thread.last_message_domain = None


async def apply_guild_member_event(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    event: dict[str, Any],
) -> tuple[User, bool] | None:
    locked = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
    )
    if locked is None:
        raise ValueError("replicated guild disappeared")
    context = event.get("context")
    if not isinstance(context, dict):
        raise ValueError("guild member event context is invalid")
    authority_guild_version = _event_datetime(
        context.get("guild_version"),
        "guild version",
        optional=True,
    )
    seq = database_snowflake(event.get("seq") or context.get("seq"), "guild sequence")
    if seq <= locked.last_event_seq:
        return None
    if seq != locked.last_event_seq + 1:
        locked.sync_status = "stale"
        raise GuildSequenceGap(locked.last_event_seq + 1, seq)
    content = event.get("content")
    if not isinstance(content, dict):
        raise ValueError("guild member event content is invalid")
    profile = RemoteUserProfile.model_validate(content.get("user"))
    event_actor = event.get("actor")
    if not isinstance(event_actor, dict) or (
        database_snowflake(event_actor.get("id"), "guild member event actor id"),
        str(event_actor.get("domain")),
    ) != (locked.owner_id, locked.owner_domain):
        raise ValueError("guild member event actor does not match its owner")
    joined_at = datetime.fromisoformat(str(content.get("joined_at")))
    if joined_at.tzinfo is None:
        raise ValueError("guild member join timestamp must include a timezone")
    temporary = content.get("temporary", False)
    if not isinstance(temporary, bool):
        raise ValueError("guild member temporary flag is invalid")
    raw_role_refs = content.get("role_ids", [])
    if not isinstance(raw_role_refs, list) or len(raw_role_refs) > 100:
        raise ValueError("guild member role references are invalid")
    role_refs = [_event_ref(raw, "member role") for raw in raw_role_refs]
    if len(role_refs) != len(set(role_refs)):
        raise ValueError("guild member role references contain duplicates")
    if any(
        role_domain != locked.origin_domain or role_id == locked.id
        for role_id, role_domain in role_refs
    ):
        raise ValueError("guild member role does not belong to the guild")
    if role_refs:
        role_rows = await session.execute(
            select(Role.id, Role.origin_domain).where(
                Role.guild_id == locked.id,
                Role.guild_domain == locked.origin_domain,
                tuple_(Role.id, Role.origin_domain).in_(role_refs),
            )
        )
        existing_role_refs = {(role_id, role_domain) for role_id, role_domain in role_rows}
        if existing_role_refs != set(role_refs):
            raise ValueError("guild member role is unknown")
        if temporary:
            raise ValueError("guild member with invite roles cannot be temporary")
    member_ref = (int(profile.id), profile.origin_domain)
    member = await session.get(
        GuildMember,
        (locked.id, locked.origin_domain, member_ref[0], member_ref[1]),
    )
    joining_intent: RemoteGuildMembershipIntent | None = None
    if member_ref[1] == settings.domain:
        intent = await session.scalar(
            select(RemoteGuildMembershipIntent)
            .where(
                RemoteGuildMembershipIntent.guild_id == locked.id,
                RemoteGuildMembershipIntent.guild_domain == locked.origin_domain,
                RemoteGuildMembershipIntent.user_id == member_ref[0],
                RemoteGuildMembershipIntent.user_domain == member_ref[1],
            )
            .with_for_update()
        )
        if member is None and (intent is None or intent.state != REMOTE_GUILD_JOINING):
            # This is still a valid ordered home event. Consume its sequence so
            # an unsolicited add cannot wedge subsequent guild replication,
            # while the authority can never invent local-user consent. Absence
            # is already fail-closed, so do not create attacker-chosen rows.
            _advance_snapshot_generation(locked, event, event_type="guild.member.add")
            locked.last_event_seq = seq
            locked.next_event_seq = seq + 1
            locked.sync_status = "ready"
            if authority_guild_version is not None:
                locked.updated_at = authority_guild_version
            return None
        if member is None:
            joining_intent = intent
    user = await resolve_delegated_profile(
        session,
        settings,
        profile,
        authority_origin=locked.origin_domain,
    )
    created = member is None
    if member is None:
        session.add(
            GuildMember(
                guild_id=locked.id,
                guild_domain=locked.origin_domain,
                user_id=user.id,
                user_domain=user.origin_domain,
                joined_at=joined_at,
                temporary=temporary,
            )
        )
    if role_refs:
        await session.execute(
            pg_insert(MemberRole)
            .values(
                [
                    {
                        "guild_id": locked.id,
                        "guild_domain": locked.origin_domain,
                        "user_id": user.id,
                        "user_domain": user.origin_domain,
                        "role_id": role_id,
                        "role_domain": role_domain,
                    }
                    for role_id, role_domain in role_refs
                ]
            )
            .on_conflict_do_nothing()
        )
    if joining_intent is not None:
        await complete_remote_guild_join(session, joining_intent)
    _advance_snapshot_generation(locked, event, event_type="guild.member.add")
    locked.last_event_seq = seq
    locked.next_event_seq = seq + 1
    locked.sync_status = "ready"
    if authority_guild_version is not None:
        locked.updated_at = authority_guild_version
    return user, created


async def apply_guild_redaction_event(
    session: AsyncSession,
    guild: Guild,
    event: dict[str, Any],
) -> None:
    """Advance a replica across an event it was never authorized to inspect."""

    locked = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
    )
    if locked is None:
        raise ValueError("replicated guild disappeared")
    if event.get("type") != "guild.event.redacted":
        raise ValueError("unsupported guild redaction event type")
    context = event.get("context")
    actor = event.get("actor")
    if not isinstance(context, dict) or (
        database_snowflake(context.get("guild_id"), "guild id"),
        str(context.get("guild_domain")),
    ) != (locked.id, locked.origin_domain):
        raise ValueError("guild redaction references the wrong guild")
    authority_guild_version = _event_datetime(
        context.get("guild_version"),
        "guild version",
        optional=True,
    )
    if not isinstance(actor, dict) or (
        database_snowflake(actor.get("id"), "guild redaction actor id"),
        str(actor.get("domain")),
    ) != (locked.owner_id, locked.owner_domain):
        raise ValueError("guild redaction actor does not match its owner")
    seq = database_snowflake(context.get("seq"), "guild sequence")
    if seq <= locked.last_event_seq:
        return
    if seq != locked.last_event_seq + 1:
        locked.sync_status = "stale"
        raise GuildSequenceGap(locked.last_event_seq + 1, seq)
    content = event.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("original_type"), str):
        raise ValueError("guild redaction content is invalid")
    _advance_snapshot_generation(
        locked,
        event,
        event_type=str(content["original_type"]),
    )
    locked.last_event_seq = seq
    locked.next_event_seq = seq + 1
    locked.sync_status = "ready"
    if authority_guild_version is not None:
        locked.updated_at = authority_guild_version


def _event_ref(
    raw: object,
    label: str,
    *,
    default_origin_domain: str | None = None,
) -> tuple[int, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} reference is invalid")
    raw_origin = raw.get("origin_domain")
    if "origin_domain" not in raw and default_origin_domain is not None:
        raw_origin = default_origin_domain
    return (
        database_snowflake(raw.get("id"), f"{label} id"),
        normalize_domain(str(raw_origin or "")),
    )


def _event_datetime(raw: object, label: str, *, optional: bool = False) -> datetime | None:
    if raw is None and optional:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{label} timestamp is invalid")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"{label} timestamp is invalid") from None
    if value.tzinfo is None:
        raise ValueError(f"{label} timestamp lacks a timezone")
    return value


def _event_resource_version(raw: dict[str, Any], label: str) -> datetime | None:
    """Validate a federated resource token while accepting legacy omissions."""

    return _event_datetime(raw.get("version"), f"{label} version", optional=True)


def _apply_event_resource_version(
    resource: Guild | Role | Channel | Emoji | Sticker,
    raw: dict[str, Any],
    label: str,
) -> datetime | None:
    version = _event_resource_version(raw, label)
    if version is not None:
        resource.updated_at = version
    return version


def _bounded_event_int(
    raw: object,
    label: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
    optional: bool = False,
) -> int | None:
    if raw is None and optional:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{label} is invalid")
    if raw < minimum or (maximum is not None and raw > maximum):
        raise ValueError(f"{label} is invalid")
    return raw


async def _validated_stage_instance(
    session: AsyncSession,
    guild: Guild,
    raw: object,
) -> dict[str, object]:
    """Validate a live Stage projection without retaining ephemeral replica state."""

    if not isinstance(raw, dict):
        raise ValueError("Stage instance mutation is invalid")
    allowed_fields = {
        "id",
        "origin_domain",
        "guild_id",
        "guild_domain",
        "channel_id",
        "channel_domain",
        "topic",
        "privacy_level",
        "discoverable_disabled",
        "guild_scheduled_event_id",
        "guild_scheduled_event_domain",
    }
    if not set(raw).issubset(allowed_fields):
        raise ValueError("Stage instance mutation contains unknown fields")
    instance_ref = _event_ref(raw, "Stage instance")
    if instance_ref[1] != guild.origin_domain:
        raise ValueError("Stage instance authority is invalid")
    if (
        database_snowflake(raw.get("guild_id"), "Stage instance guild id"),
        normalize_domain(str(raw.get("guild_domain", ""))),
    ) != (guild.id, guild.origin_domain):
        raise ValueError("Stage instance references the wrong guild")
    channel_ref = (
        database_snowflake(raw.get("channel_id"), "Stage channel id"),
        normalize_domain(str(raw.get("channel_domain", ""))),
    )
    channel = await session.get(Channel, channel_ref)
    if (
        channel is None
        or channel.unavailable
        or channel.type != 13
        or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
    ):
        raise ValueError("Stage instance references an invalid Stage channel")
    topic = raw.get("topic")
    if (
        not isinstance(topic, str)
        or topic != topic.strip()
        or not 1 <= len(topic) <= 120
        or "\x00" in topic
    ):
        raise ValueError("Stage instance topic is invalid")
    if raw.get("privacy_level") != 2 or raw.get("discoverable_disabled") is not True:
        raise ValueError("Stage instance privacy is invalid")
    scheduled_id = raw.get("guild_scheduled_event_id")
    scheduled_domain = raw.get("guild_scheduled_event_domain")
    if (scheduled_id is None) != (scheduled_domain is None):
        raise ValueError("Stage scheduled event reference is incomplete")
    if scheduled_id is not None:
        database_snowflake(scheduled_id, "Stage scheduled event id")
        if normalize_domain(str(scheduled_domain)) != guild.origin_domain:
            raise ValueError("Stage scheduled event authority is invalid")
    return dict(raw)


def _require_projection_guild(
    raw: dict[str, Any],
    guild: Guild,
    label: str,
) -> None:
    if (
        database_snowflake(raw.get("guild_id"), f"{label} guild id"),
        normalize_domain(str(raw.get("guild_domain", ""))),
    ) != (guild.id, guild.origin_domain):
        raise ValueError(f"{label} references the wrong guild")


def _projection_ref_fields(
    raw: dict[str, Any],
    id_field: str,
    domain_field: str,
    label: str,
    *,
    optional: bool = False,
) -> tuple[int, str] | None:
    raw_id = raw.get(id_field)
    raw_domain = raw.get(domain_field)
    if raw_id is None and raw_domain is None and optional:
        return None
    if (raw_id is None) != (raw_domain is None):
        raise ValueError(f"{label} reference is incomplete")
    return (
        database_snowflake(raw_id, f"{label} id"),
        normalize_domain(str(raw_domain or "")),
    )


def _projection_text(
    raw: object,
    label: str,
    *,
    maximum: int,
    optional: bool = False,
) -> str | None:
    if raw is None and optional:
        return None
    if (
        not isinstance(raw, str)
        or raw != raw.strip()
        or not 1 <= len(raw) <= maximum
        or "\x00" in raw
    ):
        raise ValueError(f"{label} is invalid")
    return raw


async def _validated_scheduled_event(
    session: AsyncSession,
    guild: Guild,
    raw: object,
) -> dict[str, object]:
    """Validate a live scheduled-event projection without storing authority state."""

    if not isinstance(raw, dict):
        raise ValueError("scheduled event mutation is invalid")
    allowed_fields = {
        "id",
        "origin_domain",
        "guild_id",
        "guild_domain",
        "channel_id",
        "channel_domain",
        "creator_id",
        "creator_domain",
        "name",
        "description",
        "scheduled_start_time",
        "scheduled_end_time",
        "privacy_level",
        "status",
        "entity_type",
        "entity_id",
        "entity_domain",
        "entity_metadata",
        "recurrence_rule",
        "image",
        "created_at",
        "updated_at",
        "version",
        "creator",
        "user_count",
    }
    if not set(raw).issubset(allowed_fields):
        raise ValueError("scheduled event mutation contains unknown fields")
    event_ref = _event_ref(raw, "scheduled event")
    if event_ref[1] != guild.origin_domain:
        raise ValueError("scheduled event authority is invalid")
    _require_projection_guild(raw, guild, "scheduled event")
    creator_ref = _projection_ref_fields(
        raw,
        "creator_id",
        "creator_domain",
        "scheduled event creator",
    )
    if creator_ref is None:
        raise ValueError("scheduled event creator is invalid")
    channel_ref = _projection_ref_fields(
        raw,
        "channel_id",
        "channel_domain",
        "scheduled event channel",
        optional=True,
    )
    entity_ref = _projection_ref_fields(
        raw,
        "entity_id",
        "entity_domain",
        "scheduled event entity",
        optional=True,
    )
    _projection_text(raw.get("name"), "scheduled event name", maximum=100)
    _projection_text(
        raw.get("description"),
        "scheduled event description",
        maximum=1_000,
        optional=True,
    )
    start = _event_datetime(raw.get("scheduled_start_time"), "scheduled event start")
    end = _event_datetime(
        raw.get("scheduled_end_time"),
        "scheduled event end",
        optional=True,
    )
    created = _event_datetime(raw.get("created_at"), "scheduled event creation")
    updated = _event_datetime(raw.get("updated_at"), "scheduled event update")
    if start is None or created is None or updated is None:
        raise ValueError("scheduled event timestamps are incomplete")
    if end is not None and end <= start:
        raise ValueError("scheduled event end does not follow its start")
    if updated < created:
        raise ValueError("scheduled event update predates its creation")
    if raw.get("privacy_level") != 2:
        raise ValueError("scheduled event privacy is invalid")
    status_value = _bounded_event_int(
        raw.get("status"), "scheduled event status", minimum=1, maximum=4
    )
    entity_type = _bounded_event_int(
        raw.get("entity_type"), "scheduled event entity type", minimum=1, maximum=3
    )
    if status_value is None or entity_type is None:
        raise ValueError("scheduled event type is invalid")
    if (entity_type in {1, 2}) != (channel_ref is not None):
        raise ValueError("scheduled event channel does not match its entity type")
    if channel_ref is not None:
        channel = await session.get(Channel, channel_ref)
        expected_type = 13 if entity_type == 1 else 2
        if (
            channel is None
            or channel.unavailable
            or channel.type != expected_type
            or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
        ):
            raise ValueError("scheduled event references an invalid channel")
    if entity_ref is not None and entity_ref[1] != guild.origin_domain:
        raise ValueError("scheduled event entity authority is invalid")
    metadata = raw.get("entity_metadata")
    if entity_type == 3:
        if not isinstance(metadata, dict):
            raise ValueError("external scheduled event metadata is invalid")
        _projection_text(
            metadata.get("location"),
            "scheduled event location",
            maximum=100,
        )
        if end is None:
            raise ValueError("external scheduled event end is required")
    elif metadata is not None:
        raise ValueError("channel scheduled event metadata is invalid")
    recurrence = validate_recurrence_projection(
        raw.get("recurrence_rule"),
        scheduled_start_time=start,
    )
    image = raw.get("image")
    if image is not None and (not isinstance(image, str) or not valid_content_digest(image)):
        raise ValueError("scheduled event image digest is invalid")
    version = raw.get("version")
    if not isinstance(version, str) or not 1 <= len(version) <= 256:
        raise ValueError("scheduled event version is invalid")
    creator = raw.get("creator")
    if creator is not None and (
        not isinstance(creator, dict)
        or _event_ref(creator, "scheduled event creator") != creator_ref
    ):
        raise ValueError("scheduled event creator profile is invalid")
    _bounded_event_int(
        raw.get("user_count"),
        "scheduled event user count",
        maximum=100_000_000,
        optional=True,
    )
    return {**raw, "recurrence_rule": recurrence}


def _validated_scheduled_event_subscription(
    guild: Guild,
    raw: object,
) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != {
        "guild_scheduled_event_id",
        "guild_scheduled_event_domain",
        "user_id",
        "user_domain",
        "guild_id",
        "guild_domain",
    }:
        raise ValueError("scheduled event subscription mutation is invalid")
    _require_projection_guild(raw, guild, "scheduled event subscription")
    event_ref = _projection_ref_fields(
        raw,
        "guild_scheduled_event_id",
        "guild_scheduled_event_domain",
        "scheduled event subscription event",
    )
    _projection_ref_fields(
        raw,
        "user_id",
        "user_domain",
        "scheduled event subscriber",
    )
    if event_ref is None or event_ref[1] != guild.origin_domain:
        raise ValueError("scheduled event subscription authority is invalid")
    return dict(raw)


def _validated_soundboard_sound(guild: Guild, raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != {
        "id",
        "origin_domain",
        "guild_id",
        "guild_domain",
        "name",
        "media_hash",
        "content_type",
        "volume",
        "emoji_id",
        "emoji_domain",
        "emoji_name",
        "available",
        "duration_ms",
        "created_by_id",
        "created_by_domain",
        "version",
    }:
        raise ValueError("soundboard sound mutation is invalid")
    sound_ref = _event_ref(raw, "soundboard sound")
    if sound_ref[1] != guild.origin_domain:
        raise ValueError("soundboard sound authority is invalid")
    _require_projection_guild(raw, guild, "soundboard sound")
    _projection_text(raw.get("name"), "soundboard sound name", maximum=32)
    media_hash = raw.get("media_hash")
    if not isinstance(media_hash, str) or not valid_content_digest(media_hash):
        raise ValueError("soundboard media digest is invalid")
    if raw.get("content_type") not in {"audio/mpeg", "audio/ogg"}:
        raise ValueError("soundboard content type is invalid")
    volume = raw.get("volume")
    if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not 0 <= volume <= 1:
        raise ValueError("soundboard volume is invalid")
    emoji_ref = _projection_ref_fields(
        raw,
        "emoji_id",
        "emoji_domain",
        "soundboard emoji",
        optional=True,
    )
    if emoji_ref is not None and emoji_ref[1] != guild.origin_domain:
        raise ValueError("soundboard emoji authority is invalid")
    _projection_text(
        raw.get("emoji_name"),
        "soundboard emoji name",
        maximum=64,
        optional=True,
    )
    if not isinstance(raw.get("available"), bool):
        raise ValueError("soundboard availability is invalid")
    _bounded_event_int(
        raw.get("duration_ms"),
        "soundboard duration",
        minimum=1,
        maximum=5_200,
    )
    _projection_ref_fields(
        raw,
        "created_by_id",
        "created_by_domain",
        "soundboard creator",
    )
    version = database_snowflake(raw.get("version"), "soundboard version")
    if version < 1:
        raise ValueError("soundboard version is invalid")
    return dict(raw)


def _validated_soundboard_collection(guild: Guild, raw: dict[str, Any]) -> dict[str, object]:
    if set(raw) != {"guild_id", "guild_domain", "soundboard_sounds"}:
        raise ValueError("soundboard collection mutation is invalid")
    _require_projection_guild(raw, guild, "soundboard collection")
    sounds = raw.get("soundboard_sounds")
    if not isinstance(sounds, list) or len(sounds) > 48:
        raise ValueError("soundboard collection is invalid")
    rendered = [_validated_soundboard_sound(guild, item) for item in sounds]
    refs = {(item["id"], item["origin_domain"]) for item in rendered}
    names = {str(item["name"]).casefold() for item in rendered}
    if len(refs) != len(rendered) or len(names) != len(rendered):
        raise ValueError("soundboard collection contains duplicates")
    return {**raw, "soundboard_sounds": rendered}


def _validated_automod_rule(guild: Guild, raw: object) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != {
        "id",
        "origin_domain",
        "guild_id",
        "guild_domain",
        "name",
        "creator_id",
        "creator_domain",
        "event_type",
        "trigger_type",
        "trigger_metadata",
        "actions",
        "enabled",
        "exempt_roles",
        "exempt_channels",
        "version",
        "created_at",
        "updated_at",
    }:
        raise ValueError("AutoMod rule mutation is invalid")
    rule_ref = _event_ref(raw, "AutoMod rule")
    if rule_ref[1] != guild.origin_domain:
        raise ValueError("AutoMod rule authority is invalid")
    _require_projection_guild(raw, guild, "AutoMod rule")
    _projection_ref_fields(raw, "creator_id", "creator_domain", "AutoMod rule creator")
    actions = raw.get("actions")
    if not isinstance(actions, list) or not 1 <= len(actions) <= 3:
        raise ValueError("AutoMod rule actions are invalid")
    flattened_actions: list[dict[str, object]] = []
    for action in actions:
        if not isinstance(action, dict) or set(action) != {"type", "metadata"}:
            raise ValueError("AutoMod rule action is invalid")
        metadata = action.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("AutoMod rule action metadata is invalid")
        flattened_actions.append({"type": action.get("type"), **metadata})
    try:
        AutoModRuleCreate.model_validate(
            {
                "name": raw.get("name"),
                "event_type": raw.get("event_type"),
                "trigger_type": raw.get("trigger_type"),
                "trigger_metadata": raw.get("trigger_metadata"),
                "actions": flattened_actions,
                "enabled": raw.get("enabled"),
                "exempt_roles": raw.get("exempt_roles"),
                "exempt_channels": raw.get("exempt_channels"),
            }
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("AutoMod rule configuration is invalid") from exc
    for field, label in (
        ("exempt_roles", "AutoMod exempt role"),
        ("exempt_channels", "AutoMod exempt channel"),
    ):
        values = raw.get(field)
        if not isinstance(values, list):
            raise ValueError(f"{label} list is invalid")
        try:
            refs = [EntityRef(str(item)) for item in values]
        except ValueError as exc:
            raise ValueError(f"{label} reference is invalid") from exc
        if any(item.domain != guild.origin_domain for item in refs):
            raise ValueError(f"{label} authority is invalid")
    _bounded_event_int(
        raw.get("version"),
        "AutoMod rule version",
        minimum=1,
    )
    created = _event_datetime(raw.get("created_at"), "AutoMod rule creation")
    updated = _event_datetime(raw.get("updated_at"), "AutoMod rule update")
    if created is None or updated is None or updated < created:
        raise ValueError("AutoMod rule timestamps are invalid")
    return dict(raw)


async def _validated_automod_execution(
    session: AsyncSession,
    guild: Guild,
    raw: object,
) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != {
        "guild_id",
        "guild_domain",
        "channel_id",
        "channel_domain",
        "rule_id",
        "rule_domain",
        "rule_trigger_type",
        "user_id",
        "user_domain",
        "action",
        "outcome",
        "content",
        "matched_keyword",
        "matched_content",
        "alert_system_message_id",
        "alert_system_message_domain",
        "content_digest",
    }:
        raise ValueError("AutoMod execution mutation is invalid")
    _require_projection_guild(raw, guild, "AutoMod execution")
    channel_ref = _projection_ref_fields(
        raw,
        "channel_id",
        "channel_domain",
        "AutoMod execution channel",
        optional=True,
    )
    if channel_ref is not None:
        channel = await session.get(Channel, channel_ref)
        if channel is None or (channel.guild_id, channel.guild_domain) != (
            guild.id,
            guild.origin_domain,
        ):
            raise ValueError("AutoMod execution channel is invalid")
    rule_ref = _projection_ref_fields(
        raw,
        "rule_id",
        "rule_domain",
        "AutoMod execution rule",
    )
    if rule_ref is None or rule_ref[1] != guild.origin_domain:
        raise ValueError("AutoMod execution rule authority is invalid")
    _projection_ref_fields(
        raw,
        "user_id",
        "user_domain",
        "AutoMod execution user",
    )
    if raw.get("rule_trigger_type") not in {
        "keyword",
        "spam",
        "keyword_preset",
        "mention_spam",
        "member_profile",
    }:
        raise ValueError("AutoMod execution trigger is invalid")
    action = raw.get("action")
    if not isinstance(action, dict) or set(action) != {"type", "metadata"}:
        raise ValueError("AutoMod execution action is invalid")
    metadata = action.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("AutoMod execution action metadata is invalid")
    try:
        AutoModActionInput.model_validate({"type": action.get("type"), **metadata})
    except (TypeError, ValueError) as exc:
        raise ValueError("AutoMod execution action is invalid") from exc
    if raw.get("outcome") not in {"blocked", "alerted", "failed", "timed_out"}:
        raise ValueError("AutoMod execution action result is invalid")
    alert_ref = _projection_ref_fields(
        raw,
        "alert_system_message_id",
        "alert_system_message_domain",
        "AutoMod alert system message",
        optional=True,
    )
    if alert_ref is not None and alert_ref[1] != guild.origin_domain:
        raise ValueError("AutoMod alert message authority is invalid")
    content = raw.get("content")
    if not isinstance(content, str) or len(content) > 4_000 or "\x00" in content:
        raise ValueError("AutoMod execution content is invalid")
    matched_keyword = raw.get("matched_keyword")
    if matched_keyword is not None and (
        not isinstance(matched_keyword, str)
        or len(matched_keyword) > 260
        or "\x00" in matched_keyword
    ):
        raise ValueError("AutoMod execution matched keyword is invalid")
    matched_content = raw.get("matched_content")
    if matched_content is not None and (
        not isinstance(matched_content, str)
        or len(matched_content) > 4_000
        or "\x00" in matched_content
    ):
        raise ValueError("AutoMod execution matched content is invalid")
    digest = raw.get("content_digest")
    if digest is not None and (not isinstance(digest, str) or not valid_content_digest(digest)):
        raise ValueError("AutoMod execution digest is invalid")
    return dict(raw)


def _validated_voice_channel_state(raw: dict[str, Any], channel_type: int) -> dict[str, Any]:
    """Validate opaque voice settings without interpreting provider region IDs."""

    fields = ("bitrate", "user_limit", "rtc_region", "video_quality_mode")
    if channel_type not in GUILD_VOICE_CHANNEL_TYPES:
        if any(raw.get(field) is not None for field in fields):
            raise ValueError("voice metadata is invalid for this channel type")
        return {field: None for field in fields}

    # Defaults keep rolling federation compatible with peers that predate these
    # fields. Once present, every bound is checked before replica state changes.
    bitrate = _bounded_event_int(
        raw.get("bitrate", 64_000),
        "voice bitrate",
        minimum=8_000,
        maximum=64_000 if channel_type == 13 else 384_000,
    )
    user_limit = _bounded_event_int(
        raw.get("user_limit", 0),
        "voice user limit",
        minimum=0,
        maximum=10_000 if channel_type == 13 else 99,
    )
    quality = _bounded_event_int(
        raw.get("video_quality_mode", 1),
        "voice video quality mode",
        minimum=1,
        maximum=2,
    )
    rtc_region = raw.get("rtc_region")
    if rtc_region is not None and (
        not isinstance(rtc_region, str)
        or rtc_region != rtc_region.strip()
        or not 1 <= len(rtc_region) <= 64
    ):
        raise ValueError("voice RTC region is invalid")
    return {
        "bitrate": bitrate,
        "user_limit": user_limit,
        "rtc_region": rtc_region,
        "video_quality_mode": quality,
    }


def _validated_channel_extension_state(
    raw: dict[str, Any], channel_type: int, origin: str
) -> dict[str, Any]:
    """Validate channel/thread fields shared by live mutations and snapshots."""

    thread = channel_type in {10, 11, 12}
    forum = channel_type == 15
    nsfw = raw.get("nsfw", False)
    if not isinstance(nsfw, bool):
        raise ValueError("channel NSFW state is invalid")
    flags = database_snowflake(raw.get("flags", "0"), "channel flags")
    if (forum and flags & ~(1 << 4)) or (thread and flags & ~(1 << 1)):
        raise ValueError("channel flags contain unsupported bits")
    if not forum and not thread and flags:
        raise ValueError("channel flags are invalid for its type")

    owner_id_raw = raw.get("owner_id")
    owner_domain_raw = raw.get("owner_domain")
    if (owner_id_raw is None) != (owner_domain_raw is None):
        raise ValueError("channel owner identity is incomplete")
    owner_id = (
        database_snowflake(owner_id_raw, "thread owner id") if owner_id_raw is not None else None
    )
    owner_domain = normalize_domain(str(owner_domain_raw)) if owner_id is not None else None
    if thread and (owner_id is None or owner_domain is None or owner_domain == ""):
        raise ValueError("thread owner identity is missing")
    if not thread and owner_id is not None:
        raise ValueError("non-thread channel contains thread owner metadata")

    archived = raw.get("archived")
    locked = raw.get("locked")
    invitable = raw.get("invitable")
    if thread:
        if not isinstance(archived, bool) or not isinstance(locked, bool):
            raise ValueError("thread lifecycle flags are invalid")
        if channel_type == 12:
            if not isinstance(invitable, bool):
                raise ValueError("private thread invite policy is invalid")
        elif invitable is not None:
            raise ValueError("public thread contains a private invite policy")
    elif archived is not None or locked is not None or invitable is not None:
        raise ValueError("non-thread channel contains thread lifecycle metadata")

    auto_archive_duration = _bounded_event_int(
        raw.get("auto_archive_duration"),
        "thread auto archive duration",
        optional=not thread,
    )
    if thread and auto_archive_duration not in {60, 1440, 4320, 10080}:
        raise ValueError("thread auto archive duration is invalid")
    archive_timestamp = _event_datetime(
        raw.get("archive_timestamp"), "thread archive", optional=not thread
    )
    last_activity_at = _event_datetime(
        raw.get("last_activity_at"), "thread activity", optional=not thread
    )
    message_count = _bounded_event_int(
        raw.get("message_count"), "thread message count", optional=not thread
    )
    total_message_sent = _bounded_event_int(
        raw.get("total_message_sent"), "thread total messages", optional=not thread
    )
    member_count = _bounded_event_int(
        raw.get("member_count"),
        "thread member count",
        maximum=1000,
        optional=not thread,
    )
    if not thread and any(
        value is not None
        for value in (
            auto_archive_duration,
            archive_timestamp,
            last_activity_at,
            message_count,
            total_message_sent,
            member_count,
        )
    ):
        raise ValueError("non-thread channel contains thread counters")

    starter_id_raw = raw.get("starter_message_id")
    starter_domain_raw = raw.get("starter_message_domain")
    if (starter_id_raw is None) != (starter_domain_raw is None):
        raise ValueError("thread starter identity is incomplete")
    starter_message_id = (
        database_snowflake(starter_id_raw, "thread starter id")
        if starter_id_raw is not None
        else None
    )
    starter_message_domain = (
        normalize_domain(str(starter_domain_raw)) if starter_message_id is not None else None
    )
    if starter_message_domain is not None and starter_message_domain != origin:
        raise ValueError("thread starter is not authoritative at the guild home")
    if not thread and starter_message_id is not None:
        raise ValueError("non-thread channel contains a starter identity")

    last_thread_id_raw = raw.get("last_thread_id")
    last_thread_domain_raw = raw.get("last_thread_domain")
    if (last_thread_id_raw is None) != (last_thread_domain_raw is None):
        raise ValueError("forum last thread identity is incomplete")
    last_thread_id = (
        database_snowflake(last_thread_id_raw, "forum last thread id")
        if last_thread_id_raw is not None
        else None
    )
    last_thread_domain = (
        normalize_domain(str(last_thread_domain_raw)) if last_thread_id is not None else None
    )
    if last_thread_domain is not None and last_thread_domain != origin:
        raise ValueError("forum last thread is not authoritative at the guild home")
    if not forum and last_thread_id is not None:
        raise ValueError("non-forum channel contains a last thread identity")

    raw_applied_tags = raw.get("applied_tag_ids", [])
    if not isinstance(raw_applied_tags, list) or len(raw_applied_tags) > 5:
        raise ValueError("thread applied tags are invalid")
    applied_tag_ids = [
        str(database_snowflake(value, "applied tag id")) for value in raw_applied_tags
    ]
    if len(set(applied_tag_ids)) != len(applied_tag_ids):
        raise ValueError("thread applied tags contain duplicates")
    if not thread and applied_tag_ids:
        raise ValueError("non-thread channel contains applied tags")

    default_auto_archive_duration = _bounded_event_int(
        raw.get("default_auto_archive_duration"),
        "default auto archive duration",
        optional=True,
    )
    if default_auto_archive_duration is not None and default_auto_archive_duration not in {
        60,
        1440,
        4320,
        10080,
    }:
        raise ValueError("default auto archive duration is invalid")
    if forum and default_auto_archive_duration is None:
        raise ValueError("forum default auto archive duration is missing")
    if default_auto_archive_duration is not None and channel_type not in {0, 5, 15}:
        raise ValueError("default auto archive duration is invalid for its channel type")
    default_thread_rate = _bounded_event_int(
        raw.get("default_thread_rate_limit_per_user"),
        "default thread slowmode",
        maximum=21_600,
        optional=True,
    )
    if forum and default_thread_rate is None:
        raise ValueError("forum default thread slowmode is missing")
    if default_thread_rate is not None and channel_type not in {0, 15}:
        raise ValueError("default thread slowmode is invalid for its channel type")

    raw_tags = raw.get("available_tags", [])
    if not isinstance(raw_tags, list) or len(raw_tags) > 20:
        raise ValueError("forum available tags are invalid")
    available_tags: list[dict[str, object]] = []
    tag_ids: set[str] = set()
    tag_names: set[str] = set()
    for item in raw_tags:
        if not isinstance(item, dict):
            raise ValueError("forum available tag is invalid")
        tag_id = str(database_snowflake(item.get("id"), "forum tag id"))
        name = item.get("name")
        moderated = item.get("moderated")
        emoji_id_raw = item.get("emoji_id")
        emoji_name = item.get("emoji_name")
        if not isinstance(name, str) or len(name) > 20:
            raise ValueError("forum tag name is invalid")
        if not isinstance(moderated, bool):
            raise ValueError("forum tag moderation flag is invalid")
        if emoji_id_raw is not None and emoji_name is not None:
            raise ValueError("forum tag emoji identity is ambiguous")
        emoji_id = (
            str(database_snowflake(emoji_id_raw, "forum tag emoji id"))
            if emoji_id_raw is not None
            else None
        )
        if emoji_name is not None and (
            not isinstance(emoji_name, str) or not 1 <= len(emoji_name) <= 64
        ):
            raise ValueError("forum tag emoji name is invalid")
        if tag_id in tag_ids or name.casefold() in tag_names:
            raise ValueError("forum tags contain duplicate identities")
        tag_ids.add(tag_id)
        tag_names.add(name.casefold())
        available_tags.append(
            {
                "id": tag_id,
                "name": name,
                "moderated": moderated,
                "emoji_id": emoji_id,
                "emoji_name": emoji_name,
            }
        )
    if not forum and available_tags:
        raise ValueError("non-forum channel contains available tags")

    default_reaction = raw.get("default_reaction_emoji")
    if default_reaction is not None:
        if not forum or not isinstance(default_reaction, dict):
            raise ValueError("default forum reaction is invalid")
        reaction_id_raw = default_reaction.get("emoji_id")
        reaction_name = default_reaction.get("emoji_name")
        if (reaction_id_raw is None) == (reaction_name is None):
            raise ValueError("default forum reaction identity is invalid")
        if reaction_name is not None:
            if not isinstance(reaction_name, str) or not 1 <= len(reaction_name) <= 64:
                raise ValueError("default forum reaction name is invalid")
            try:
                canonical_reaction_name = canonical_unicode_reaction_emoji(reaction_name)
            except ValueError:
                raise ValueError("default forum reaction name is invalid") from None
            reaction_name = canonical_reaction_name
        default_reaction = {
            "emoji_id": (
                str(database_snowflake(reaction_id_raw, "default reaction emoji id"))
                if reaction_id_raw is not None
                else None
            ),
            "emoji_name": reaction_name,
        }

    default_sort_order = raw.get("default_sort_order")
    if default_sort_order is not None and (
        isinstance(default_sort_order, bool) or default_sort_order not in {0, 1}
    ):
        raise ValueError("forum sort order is invalid")
    default_forum_layout = raw.get("default_forum_layout")
    if forum:
        if isinstance(default_forum_layout, bool) or default_forum_layout not in {0, 1, 2}:
            raise ValueError("forum layout is invalid")
    elif default_sort_order is not None or default_forum_layout is not None:
        raise ValueError("non-forum channel contains forum display defaults")
    e2ee_required = raw.get("e2ee_required", False)
    if not isinstance(e2ee_required, bool) or (e2ee_required and not (forum or thread)):
        raise ValueError("channel E2EE requirement is invalid")

    return {
        "nsfw": nsfw,
        "flags": flags,
        "owner_id": owner_id,
        "owner_domain": owner_domain,
        "archived": archived,
        "locked": locked,
        "invitable": invitable,
        "auto_archive_duration": auto_archive_duration,
        "archive_timestamp": archive_timestamp,
        "last_activity_at": last_activity_at,
        "message_count": message_count,
        "total_message_sent": total_message_sent,
        "member_count": member_count,
        "starter_message_id": starter_message_id,
        "starter_message_domain": starter_message_domain,
        "last_thread_id": last_thread_id,
        "last_thread_domain": last_thread_domain,
        "default_auto_archive_duration": default_auto_archive_duration,
        "default_thread_rate_limit_per_user": default_thread_rate,
        "available_tags": available_tags,
        "applied_tag_ids": applied_tag_ids,
        "default_reaction_emoji": default_reaction,
        "default_sort_order": default_sort_order,
        "default_forum_layout": default_forum_layout,
        "e2ee_required": e2ee_required,
    }


def _validated_message_rich_projection(
    raw: dict[str, Any],
    *,
    message_id: int,
    message_origin: str,
    message_created_at: datetime,
    e2ee: dict[str, Any] | None,
    message_type: int,
    flags: int = 0,
) -> dict[str, object]:
    is_crosspost = bool(flags & MESSAGE_FLAG_IS_CROSSPOST)
    # Webhook attribution has already been validated independently. Rich
    # content, interaction lineage and encrypted bindings share one strict
    # validator with DMs so federation cannot drift between channel kinds.
    projection = validate_replicated_rich_projection(
        {**raw, "webhook": None},
        message_id=message_id,
        message_origin=message_origin,
        message_created_at=message_created_at,
        e2ee=e2ee,
        message_type=message_type,
        label="guild message",
        is_crosspost=is_crosspost,
    )
    poll_projection = (
        (
            projection.poll.question,
            list(projection.poll.answers),
            projection.poll.allow_multiselect,
            projection.poll.layout_type,
            projection.poll.expires_at,
        )
        if projection.poll is not None
        else None
    )
    return {
        "embeds": projection.embeds,
        "components": projection.components,
        "sticker_items": projection.sticker_items,
        "application_ref": projection.application_ref,
        "interaction_metadata": projection.interaction_metadata,
        "view_version": projection.view_version,
        "view_persistent": projection.view_persistent,
        "view_expires_at": projection.view_expires_at,
        "interaction_integration_type": projection.interaction_integration_type,
        "interaction_installation_ref": projection.interaction_installation_ref,
        "interaction_installation_revision": projection.interaction_installation_revision,
        "has_encrypted_controls": projection.has_encrypted_controls,
        "has_encrypted_forward": projection.has_encrypted_forward,
        "forwarded_ref": projection.forwarded_ref,
        "forwarded_channel_ref": projection.forwarded_channel_ref,
        "forward_snapshot": projection.forward_snapshot,
        "poll": poll_projection,
        "poll_result": projection.poll_result,
    }


async def _stored_poll_matches_projection(
    session: AsyncSession,
    message: Message,
    projection: tuple[
        dict[str, object],
        list[tuple[int, str | None, dict[str, object] | None]],
        bool,
        int,
        datetime,
    ]
    | None,
) -> bool:
    poll = await session.get(Poll, (message.id, message.origin_domain))
    if projection is None:
        return poll is None
    if poll is None:
        return False
    question, expected_answers, allow_multiselect, layout_type, expiry = projection
    if (
        poll.question != question
        or poll.allow_multiselect != allow_multiselect
        or poll.layout_type != layout_type
        or poll.expires_at != expiry
    ):
        return False
    answers = list(
        await session.scalars(
            select(PollAnswer)
            .where(
                PollAnswer.message_id == message.id,
                PollAnswer.message_domain == message.origin_domain,
            )
            .order_by(PollAnswer.answer_id)
        )
    )
    return [(answer.answer_id, answer.text, answer.emoji) for answer in answers] == expected_answers


async def _stored_encrypted_view_matches_projection(
    session: AsyncSession,
    message: Message,
    projection: dict[str, object],
) -> bool:
    """Match the durable dispatch view to its authenticated MLS projection."""

    if not bool(projection.get("has_encrypted_controls")):
        return await session.get(MessageView, (message.id, message.origin_domain)) is None
    application_ref = cast(tuple[int, str] | None, projection.get("application_ref"))
    installation_ref = cast(
        tuple[int, str] | None,
        projection.get("interaction_installation_ref"),
    )
    if application_ref is None or installation_ref is None:
        return False
    view = await session.get(MessageView, (message.id, message.origin_domain))
    return bool(
        view is not None
        and (view.application_id, view.application_domain) == application_ref
        and view.version == projection.get("view_version")
        and view.persistent is projection.get("view_persistent")
        and view.expires_at == projection.get("view_expires_at")
        and view.integration_type == projection.get("interaction_integration_type")
        and (view.installation_id, view.installation_domain) == installation_ref
        and view.installation_revision == projection.get("interaction_installation_revision")
    )


async def _apply_message_application_projection(
    session: AsyncSession,
    message: Message,
    rich: dict[str, object],
    e2ee: dict[str, Any] | None,
) -> None:
    """Apply the immutable application lineage and mutable encrypted view projection."""

    if message.interaction_metadata != rich["interaction_metadata"]:
        raise ValueError("message update changed immutable interaction metadata")
    application_ref = cast(
        tuple[int, str] | None,
        rich["application_ref"],
    )
    encrypted_rich_update = isinstance(e2ee, dict) and "rich_payload_digest" in e2ee
    stored_application_ref = (
        (message.application_id, message.application_domain)
        if message.application_id is not None and message.application_domain is not None
        else None
    )
    if encrypted_rich_update and application_ref != stored_application_ref:
        raise ValueError("message update changed its application identity")
    if message.application_id is not None and application_ref != (
        message.application_id,
        message.application_domain,
    ):
        raise ValueError("message update changed its application identity")
    message.application_id = application_ref[0] if application_ref is not None else None
    message.application_domain = application_ref[1] if application_ref is not None else None

    incoming_view_version = cast(int, rich["view_version"])
    if encrypted_rich_update:
        stored_view = await session.scalar(
            select(MessageView)
            .where(
                MessageView.message_id == message.id,
                MessageView.message_domain == message.origin_domain,
            )
            .with_for_update()
        )
        has_encrypted_controls = bool(rich.get("has_encrypted_controls"))
        expected_view_version = (
            int(message.view_version or 0) + 1
            if has_encrypted_controls or stored_view is not None
            else 0
        )
        if incoming_view_version != expected_view_version:
            raise ValueError("encrypted message view revision is not monotonic")
        if has_encrypted_controls:
            installation_ref = cast(
                tuple[int, str] | None,
                rich.get("interaction_installation_ref"),
            )
            if application_ref is None or installation_ref is None:
                raise ValueError("encrypted message view lineage is incomplete")
            if stored_view is None:
                stored_view = MessageView(
                    message_id=message.id,
                    message_domain=message.origin_domain,
                    application_id=application_ref[0],
                    application_domain=application_ref[1],
                    integration_type=cast(
                        str,
                        rich["interaction_integration_type"],
                    ),
                    installation_id=installation_ref[0],
                    installation_domain=installation_ref[1],
                    installation_revision=cast(
                        int,
                        rich["interaction_installation_revision"],
                    ),
                    version=incoming_view_version,
                    persistent=cast(bool, rich["view_persistent"]),
                    expires_at=cast(
                        datetime | None,
                        rich["view_expires_at"],
                    ),
                )
                session.add(stored_view)
            else:
                stored_view.application_id = application_ref[0]
                stored_view.application_domain = application_ref[1]
                stored_view.integration_type = cast(
                    str,
                    rich["interaction_integration_type"],
                )
                stored_view.installation_id = installation_ref[0]
                stored_view.installation_domain = installation_ref[1]
                stored_view.installation_revision = cast(
                    int,
                    rich["interaction_installation_revision"],
                )
                stored_view.version = incoming_view_version
                stored_view.persistent = cast(bool, rich["view_persistent"])
                stored_view.expires_at = cast(
                    datetime | None,
                    rich["view_expires_at"],
                )
        elif stored_view is not None:
            await session.delete(stored_view)
    message.view_version = incoming_view_version


_STAGE_INSTANCE_DISPATCH_TYPES = {
    "guild.stage.instance.create": "STAGE_INSTANCE_CREATE",
    "guild.stage.instance.update": "STAGE_INSTANCE_UPDATE",
    "guild.stage.instance.delete": "STAGE_INSTANCE_DELETE",
}
_SCHEDULED_EVENT_DISPATCH_TYPES = {
    "guild.scheduled_event.create": "GUILD_SCHEDULED_EVENT_CREATE",
    "guild.scheduled_event.update": "GUILD_SCHEDULED_EVENT_UPDATE",
    "guild.scheduled_event.delete": "GUILD_SCHEDULED_EVENT_DELETE",
}
_SOUNDBOARD_SOUND_DISPATCH_TYPES = {
    "guild.soundboard.sound.create": "GUILD_SOUNDBOARD_SOUND_CREATE",
    "guild.soundboard.sound.update": "GUILD_SOUNDBOARD_SOUND_UPDATE",
    "guild.soundboard.sound.delete": "GUILD_SOUNDBOARD_SOUND_DELETE",
}
_AUTOMOD_RULE_DISPATCH_TYPES = {
    "guild.automod.rule.create": "AUTO_MODERATION_RULE_CREATE",
    "guild.automod.rule.update": "AUTO_MODERATION_RULE_UPDATE",
    "guild.automod.rule.delete": "AUTO_MODERATION_RULE_DELETE",
}
_PROJECTED_GUILD_FEATURE_EVENT_TYPES = frozenset().union(
    _STAGE_INSTANCE_DISPATCH_TYPES,
    _SCHEDULED_EVENT_DISPATCH_TYPES,
    _SOUNDBOARD_SOUND_DISPATCH_TYPES,
    _AUTOMOD_RULE_DISPATCH_TYPES,
    {
        "guild.scheduled_event.user.add",
        "guild.scheduled_event.user.remove",
        "guild.soundboard.sounds.update",
        "guild.voice_channel_status.update",
        "guild.voice_channel_start_time.update",
        "guild.automod.execution",
    },
)


async def _apply_stage_instance_mutation(
    session: AsyncSession,
    guild: Guild,
    event_type: str,
    content: dict[str, Any],
    actor_ref: tuple[int, str],
) -> tuple[str, dict[str, object]]:
    dispatch = await _validated_stage_instance(
        session,
        guild,
        content.get("stage_instance"),
    )
    if event_type == "guild.stage.instance.create":
        notify = content.get("send_start_notification", False)
        if not isinstance(notify, bool):
            raise ValueError("Stage start notification marker is invalid")
        dispatch["send_start_notification"] = notify
        if notify:
            dispatch["notification_id"] = str(dispatch["id"])
            dispatch["notification_author"] = {
                "id": str(actor_ref[0]),
                "origin_domain": actor_ref[1],
            }
    return _STAGE_INSTANCE_DISPATCH_TYPES[event_type], dispatch


async def _apply_scheduled_event_mutation(
    session: AsyncSession,
    guild: Guild,
    event_type: str,
    content: dict[str, Any],
) -> tuple[str, dict[str, object]]:
    if event_type in _SCHEDULED_EVENT_DISPATCH_TYPES:
        dispatch = await _validated_scheduled_event(
            session,
            guild,
            content.get("scheduled_event"),
        )
        return _SCHEDULED_EVENT_DISPATCH_TYPES[event_type], dispatch
    dispatch = _validated_scheduled_event_subscription(
        guild,
        content.get("subscription"),
    )
    dispatch_type = (
        "GUILD_SCHEDULED_EVENT_USER_ADD"
        if event_type.endswith("add")
        else "GUILD_SCHEDULED_EVENT_USER_REMOVE"
    )
    return dispatch_type, dispatch


def _apply_soundboard_mutation(
    guild: Guild,
    event_type: str,
    content: dict[str, Any],
) -> tuple[str, dict[str, object]]:
    if event_type == "guild.soundboard.sounds.update":
        return "GUILD_SOUNDBOARD_SOUNDS_UPDATE", _validated_soundboard_collection(
            guild,
            content,
        )
    return (
        _SOUNDBOARD_SOUND_DISPATCH_TYPES[event_type],
        _validated_soundboard_sound(guild, content.get("sound")),
    )


async def _apply_voice_channel_info_mutation(
    session: AsyncSession,
    guild: Guild,
    event_type: str,
    content: dict[str, Any],
) -> tuple[str, dict[str, object]]:
    channel_ref = (
        database_snowflake(content.get("channel_id"), "voice channel info id"),
        normalize_domain(str(content.get("channel_domain"))),
    )
    channel = await session.get(Channel, channel_ref)
    if (
        channel is None
        or channel.guild_id != guild.id
        or channel.guild_domain != guild.origin_domain
        or channel.type not in {2, 13}
    ):
        raise ValueError("voice channel info references an invalid channel")
    if event_type == "guild.voice_channel_status.update":
        value = content.get("status")
        if channel.type != 2 or (
            value is not None
            and (not isinstance(value, str) or value != value.strip() or not 1 <= len(value) <= 500)
        ):
            raise ValueError("voice channel status projection is invalid")
        field = "status"
        dispatch_type = "VOICE_CHANNEL_STATUS_UPDATE"
    else:
        value = content.get("voice_start_time")
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError("voice channel start time projection is invalid")
        field = "voice_start_time"
        dispatch_type = "VOICE_CHANNEL_START_TIME_UPDATE"
    return dispatch_type, {
        "id": str(channel.id),
        "guild_id": str(guild.id),
        "origin_domain": channel.origin_domain,
        "guild_domain": guild.origin_domain,
        field: value,
    }


async def _apply_automod_mutation(
    session: AsyncSession,
    guild: Guild,
    event_type: str,
    content: dict[str, Any],
) -> tuple[str, dict[str, object]]:
    if event_type == "guild.automod.execution":
        dispatch = await _validated_automod_execution(
            session,
            guild,
            content.get("execution"),
        )
        return "AUTO_MODERATION_ACTION_EXECUTION", dispatch
    return (
        _AUTOMOD_RULE_DISPATCH_TYPES[event_type],
        _validated_automod_rule(guild, content.get("rule")),
    )


async def _apply_projected_guild_feature_mutation(
    session: AsyncSession,
    guild: Guild,
    event_type: str,
    content: dict[str, Any],
    actor_ref: tuple[int, str],
) -> tuple[str, dict[str, object]]:
    """Apply ephemeral Discord-style projections without bloating sequence handling."""

    if event_type in _STAGE_INSTANCE_DISPATCH_TYPES:
        return await _apply_stage_instance_mutation(
            session,
            guild,
            event_type,
            content,
            actor_ref,
        )
    if event_type.startswith("guild.scheduled_event."):
        return await _apply_scheduled_event_mutation(session, guild, event_type, content)
    if event_type.startswith("guild.soundboard."):
        return _apply_soundboard_mutation(guild, event_type, content)
    if event_type.startswith("guild.voice_channel_"):
        return await _apply_voice_channel_info_mutation(session, guild, event_type, content)
    return await _apply_automod_mutation(session, guild, event_type, content)


def _event_context_channel_ref(
    context: dict[str, Any],
    resource: str,
) -> tuple[int, str]:
    """Parse the channel scope signed into a granular guild event."""

    return (
        database_snowflake(context.get("channel_id"), f"{resource} channel id"),
        normalize_domain(str(context.get("channel_domain", ""))),
    )


async def _apply_message_bulk_delete_mutation(
    session: AsyncSession,
    guild: Guild,
    content: dict[str, Any],
    context: dict[str, Any],
) -> tuple[str, dict[str, object]]:
    raw_messages = content.get("messages")
    if not isinstance(raw_messages, list) or not 2 <= len(raw_messages) <= 100:
        raise ValueError("message bulk deletion references an invalid message list")
    message_refs = [_event_ref(item, "bulk deleted message") for item in raw_messages]
    if len(message_refs) != len(set(message_refs)):
        raise ValueError("message bulk deletion contains duplicate messages")
    deleted_at = _event_datetime(content.get("deleted_at"), "message bulk deletion")
    if deleted_at is None:
        raise ValueError("message bulk deletion timestamp is invalid")
    channel_ref = _event_context_channel_ref(context, "message bulk deletion")
    channel = await session.get(Channel, channel_ref)
    if channel is None or (channel.guild_id, channel.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        raise ValueError("message bulk deletion references the wrong guild")
    messages = list(
        await session.scalars(
            select(Message).where(tuple_(Message.id, Message.origin_domain).in_(message_refs))
        )
    )
    if any((message.channel_id, message.channel_domain) != channel_ref for message in messages):
        raise ValueError("message bulk deletion references the wrong channel")
    active_deleted_count = 0
    for message in messages:
        if message.deleted_at is None and (
            channel.type not in {10, 11, 12}
            or (message.id, message.origin_domain)
            != (channel.starter_message_id, channel.starter_message_domain)
        ):
            active_deleted_count += 1
        message.content = None
        message.e2ee = None
        message.deleted_at = deleted_at
    if channel.type in {10, 11, 12}:
        if active_deleted_count:
            channel.message_count = max(
                0,
                int(channel.message_count or 0) - active_deleted_count,
            )
        if (channel.last_message_id, channel.last_message_domain) in set(message_refs):
            await session.flush()
            await refresh_replicated_thread_cursor(session, channel)
    return "MESSAGE_DELETE_BULK", {
        "ids": [
            {"id": str(message_id), "origin_domain": message_domain}
            for message_id, message_domain in message_refs
        ],
        "channel_id": str(channel_ref[0]),
        "channel_domain": channel_ref[1],
        "guild_id": str(guild.id),
        "guild_domain": guild.origin_domain,
    }


async def _apply_reaction_mutation(
    session: AsyncSession,
    guild: Guild,
    event_type: str,
    content: dict[str, Any],
    context: dict[str, Any],
) -> tuple[str, dict[str, object]]:
    message_ref = _event_ref(content.get("message"), "reaction message")
    user_ref = _event_ref(content.get("user"), "reaction user")
    channel_ref = _event_context_channel_ref(context, "reaction")
    raw_emoji = content.get("emoji")
    if not isinstance(raw_emoji, str) or not 1 <= len(raw_emoji) <= 320:
        raise ValueError("reaction emoji is invalid")
    try:
        emoji = canonical_reaction_emoji(raw_emoji)
    except ValueError:
        raise ValueError("reaction emoji is invalid") from None
    message = await session.get(Message, message_ref)
    user = await session.get(User, user_ref)
    added = event_type.endswith("add")
    if message is not None and user is not None:
        channel = await session.get(Channel, (message.channel_id, message.channel_domain))
        if (
            channel is None
            or (channel.id, channel.origin_domain) != channel_ref
            or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
        ):
            raise ValueError("reaction mutation references the wrong guild")
        if added and message.deleted_at is not None:
            raise ValueError("reaction mutation references a deleted message")
        if added:
            await session.execute(
                pg_insert(Reaction)
                .values(
                    message_id=message_ref[0],
                    message_domain=message_ref[1],
                    user_id=user_ref[0],
                    user_domain=user_ref[1],
                    emoji_key=emoji,
                )
                .on_conflict_do_nothing()
            )
        else:
            await session.execute(
                delete(Reaction).where(
                    Reaction.message_id == message_ref[0],
                    Reaction.message_domain == message_ref[1],
                    Reaction.user_id == user_ref[0],
                    Reaction.user_domain == user_ref[1],
                    Reaction.emoji_key == emoji,
                )
            )
    removed = not added
    return (
        "MESSAGE_REACTION_REMOVE" if removed else "MESSAGE_REACTION_ADD",
        reaction_event_payload(
            message_id=message_ref[0],
            message_domain=message_ref[1],
            channel_id=channel_ref[0],
            channel_domain=channel_ref[1],
            user_id=user_ref[0],
            user_domain=user_ref[1],
            emoji=emoji,
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            message_author_id=message.author_id if message is not None else None,
            message_author_domain=message.author_domain if message is not None else None,
            removed=removed,
        ),
    )


async def _apply_reaction_clear_mutation(
    session: AsyncSession,
    guild: Guild,
    content: dict[str, Any],
    context: dict[str, Any],
) -> tuple[str, dict[str, object]]:
    message_ref = _event_ref(content.get("message"), "reaction clear message")
    channel_ref = _event_context_channel_ref(context, "reaction clear")
    raw_emoji = content.get("emoji")
    try:
        emoji = canonical_reaction_emoji(raw_emoji) if isinstance(raw_emoji, str) else None
    except ValueError:
        raise ValueError("reaction clear emoji is invalid") from None
    if raw_emoji is not None and emoji is None:
        raise ValueError("reaction clear emoji is invalid")
    message = await session.get(Message, message_ref)
    if message is not None:
        channel = await session.get(Channel, (message.channel_id, message.channel_domain))
        if (
            channel is None
            or (channel.id, channel.origin_domain) != channel_ref
            or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
        ):
            raise ValueError("reaction clear references the wrong guild")
        conditions = [
            Reaction.message_id == message_ref[0],
            Reaction.message_domain == message_ref[1],
        ]
        if emoji is not None:
            conditions.append(Reaction.emoji_key == emoji)
        await session.execute(delete(Reaction).where(*conditions))
    dispatch: dict[str, object] = {
        "message_id": str(message_ref[0]),
        "message_domain": message_ref[1],
        "channel_id": str(channel_ref[0]),
        "channel_domain": channel_ref[1],
        "guild_id": str(guild.id),
        "guild_domain": guild.origin_domain,
    }
    if emoji is not None:
        dispatch["reaction"] = emoji
        dispatch["emoji"] = reaction_emoji_payload(emoji)
    return (
        "MESSAGE_REACTION_REMOVE_EMOJI" if emoji is not None else "MESSAGE_REACTION_REMOVE_ALL",
        dispatch,
    )


async def _apply_poll_vote_mutation(
    session: AsyncSession,
    guild: Guild,
    event_type: str,
    content: dict[str, Any],
    context: dict[str, Any],
) -> tuple[str, dict[str, object]]:
    message_ref = _event_ref(content.get("message"), "poll message")
    user_ref = _event_ref(content.get("user"), "poll voter")
    answer_id = content.get("answer_id")
    if isinstance(answer_id, bool) or not isinstance(answer_id, int) or not 1 <= answer_id <= 10:
        raise ValueError("poll vote answer is invalid")
    channel_ref = _event_context_channel_ref(context, "poll vote")
    message = await session.get(Message, message_ref)
    user = await session.get(User, user_ref)
    added = event_type.endswith("add")
    if message is not None:
        channel = await session.get(Channel, (message.channel_id, message.channel_domain))
        if (
            channel is None
            or (channel.id, channel.origin_domain) != channel_ref
            or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
        ):
            raise ValueError("poll vote mutation references the wrong guild or channel")
    if message is not None and user is not None:
        poll = await session.get(Poll, message_ref)
        answer = await session.get(PollAnswer, (*message_ref, answer_id))
        membership = await session.get(
            GuildMember,
            (guild.id, guild.origin_domain, user_ref[0], user_ref[1]),
        )
        if poll is None or answer is None or added and membership is None:
            raise ValueError("poll vote mutation references the wrong guild or answer")
        if added:
            if message.deleted_at is not None or poll.finalized_at is not None:
                raise ValueError("poll vote mutation references a closed poll")
            if not poll.allow_multiselect and await session.scalar(
                select(
                    exists().where(
                        PollVote.message_id == message_ref[0],
                        PollVote.message_domain == message_ref[1],
                        PollVote.user_id == user_ref[0],
                        PollVote.user_domain == user_ref[1],
                        PollVote.answer_id != answer_id,
                    )
                )
            ):
                raise ValueError("single-select poll vote was not replaced atomically")
            await session.execute(
                pg_insert(PollVote)
                .values(
                    message_id=message_ref[0],
                    message_domain=message_ref[1],
                    answer_id=answer_id,
                    user_id=user_ref[0],
                    user_domain=user_ref[1],
                )
                .on_conflict_do_nothing()
            )
        else:
            await session.execute(
                delete(PollVote).where(
                    PollVote.message_id == message_ref[0],
                    PollVote.message_domain == message_ref[1],
                    PollVote.answer_id == answer_id,
                    PollVote.user_id == user_ref[0],
                    PollVote.user_domain == user_ref[1],
                )
            )
    return (
        "MESSAGE_POLL_VOTE_ADD" if added else "MESSAGE_POLL_VOTE_REMOVE",
        {
            "message_id": str(message_ref[0]),
            "message_domain": message_ref[1],
            "channel_id": str(channel_ref[0]),
            "channel_domain": channel_ref[1],
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
            "user_id": str(user_ref[0]),
            "user_domain": user_ref[1],
            "answer_id": answer_id,
        },
    )


async def _apply_poll_finalize_mutation(
    session: AsyncSession,
    guild: Guild,
    content: dict[str, Any],
    context: dict[str, Any],
    actor: User,
) -> tuple[str, dict[str, object]] | None:
    message_ref = _event_ref(content.get("message"), "poll message")
    finalized_at = _event_datetime(content.get("finalized_at"), "poll finalization")
    if finalized_at is None:
        raise ValueError("poll finalization timestamp is invalid")
    channel_ref = _event_context_channel_ref(context, "poll finalization")
    message = await session.get(Message, message_ref)
    poll = await session.get(Poll, message_ref)
    if message is not None:
        channel = await session.get(Channel, (message.channel_id, message.channel_domain))
        if (
            channel is None
            or (channel.id, channel.origin_domain) != channel_ref
            or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
        ):
            raise ValueError("poll finalization references the wrong guild")
    if message is None or poll is None:
        # History-disabled replicas can legitimately lack the source. Do not
        # invent a sparse MESSAGE_UPDATE that clients cannot apply as a Message.
        return None
    if poll.finalized_at is not None and finalized_at < poll.finalized_at:
        raise ValueError("poll finalization regressed authoritative state")
    if finalized_at < message.created_at:
        raise ValueError("poll finalization predates its message")
    poll.finalized_at = finalized_at
    return "MESSAGE_UPDATE", await render_message_payload(session, message, viewer=actor)


async def _apply_pin_mutation(
    session: AsyncSession,
    guild: Guild,
    event_type: str,
    content: dict[str, Any],
    event: dict[str, Any],
    actor_ref: tuple[int, str],
) -> tuple[str, dict[str, object]]:
    message_ref = _event_ref(content.get("message"), "pinned message")
    channel_ref = _event_ref(content.get("channel"), "pin channel")
    # New events carry the semantic pinner separately because an authoritative
    # event may be signed by the local owner for an authenticated remote member.
    pinner_ref = (
        _event_ref(content.get("user"), "pin user")
        if content.get("user") is not None
        else actor_ref
    )
    channel = await session.get(Channel, channel_ref)
    if channel is None or (channel.guild_id, channel.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        raise ValueError("pin mutation references the wrong channel")
    message = await session.get(Message, message_ref)
    added = event_type.endswith("add")
    if message is not None:
        if (message.channel_id, message.channel_domain) != channel_ref:
            raise ValueError("pin mutation references the wrong channel")
        if added:
            if not message_is_pinnable(message):
                raise ValueError("pin mutation references a non-pinnable message")
            existing_pin = await session.get(
                Pin,
                (channel_ref[0], channel_ref[1], message_ref[0], message_ref[1]),
            )
            if (
                existing_pin is None
                and await channel_pin_count(session, channel) >= CHANNEL_PIN_LIMIT
            ):
                raise ValueError("pin mutation exceeds the channel pin limit")
            await session.execute(
                pg_insert(Pin)
                .values(
                    channel_id=channel_ref[0],
                    channel_domain=channel_ref[1],
                    message_id=message_ref[0],
                    message_domain=message_ref[1],
                    pinned_by_id=pinner_ref[0],
                    pinned_by_domain=pinner_ref[1],
                )
                .on_conflict_do_nothing()
            )
        else:
            await session.execute(
                delete(Pin).where(
                    Pin.channel_id == channel_ref[0],
                    Pin.channel_domain == channel_ref[1],
                    Pin.message_id == message_ref[0],
                    Pin.message_domain == message_ref[1],
                )
            )
    dispatch = await channel_pins_update_payload(session, channel, guild)
    dispatch.update(
        {
            "message_id": str(message_ref[0]),
            "message_domain": message_ref[1],
            "pinned": added,
        }
    )
    if added and message is None:
        dispatch["last_pin_timestamp"] = datetime.fromtimestamp(
            int(event["ts"]) / 1000,
            tz=UTC,
        ).isoformat()
    return "CHANNEL_PINS_UPDATE", dispatch


async def apply_guild_mutation_event(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    event: dict[str, Any],
    *,
    e2ee_policy_channels: list[Channel] | None = None,
) -> tuple[str, dict[str, object]] | None:
    """Apply one retained granular guild mutation to a remote replica."""

    event_type = str(event.get("type", ""))
    if event_type not in GUILD_MUTATION_EVENT_TYPES:
        raise ValueError("unsupported granular guild event type")
    locked = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
    )
    if locked is None or locked.origin_domain == settings.domain:
        raise ValueError("granular event references an invalid replicated guild")
    context = event.get("context")
    if not isinstance(context, dict) or (
        database_snowflake(context.get("guild_id"), "guild id"),
        normalize_domain(str(context.get("guild_domain", ""))),
    ) != (locked.id, locked.origin_domain):
        raise ValueError("granular event references the wrong guild")
    authority_guild_version = _event_datetime(
        context.get("guild_version"),
        "guild version",
        optional=True,
    )
    seq = database_snowflake(context.get("seq"), "guild sequence")
    if seq <= locked.last_event_seq:
        return None
    if seq != locked.last_event_seq + 1:
        locked.sync_status = "stale"
        raise GuildSequenceGap(locked.last_event_seq + 1, seq)
    content = event.get("content")
    if not isinstance(content, dict):
        raise ValueError("granular guild event content is invalid")
    raw_actor = event.get("actor")
    if not isinstance(raw_actor, dict):
        raise ValueError("granular guild event actor is invalid")
    actor_ref = (
        database_snowflake(raw_actor.get("id"), "guild event actor id"),
        normalize_domain(str(raw_actor.get("domain", ""))),
    )
    actor = await session.get(User, actor_ref)
    if actor is None or (
        actor_ref[1] != locked.origin_domain and actor_ref != (locked.owner_id, locked.owner_domain)
    ):
        raise ValueError("granular guild event actor is unknown or not authoritative")
    dispatch_type = "GUILD_UPDATE"
    dispatch: dict[str, object] = {
        "guild_id": str(locked.id),
        "guild_domain": locked.origin_domain,
    }
    suppress_dispatch = False
    versioned_resources: list[tuple[Guild | Role | Channel | Emoji | Sticker, datetime]] = []
    if authority_guild_version is not None:
        versioned_resources.append((locked, authority_guild_version))

    if event_type == "guild.update":
        raw = content.get("guild")
        # Early asset-only guild updates omitted ``origin_domain``. The signed
        # envelope and context have already bound this mutation to ``locked``,
        # so allow that legacy omission while keeping explicit domains strict.
        if not isinstance(raw, dict) or _event_ref(
            raw,
            "guild",
            default_origin_domain=locked.origin_domain,
        ) != (
            locked.id,
            locked.origin_domain,
        ):
            raise ValueError("guild update identity is invalid")
        guild_version = _event_resource_version(raw, "guild")
        if (
            guild_version is not None
            and authority_guild_version is not None
            and guild_version != authority_guild_version
        ):
            raise ValueError("guild event versions do not match")
        if guild_version is not None and authority_guild_version is None:
            versioned_resources.append((locked, guild_version))
        name = raw.get("name")
        if "name" in raw:
            if not isinstance(name, str) or not 2 <= len(name) <= 100:
                raise ValueError("guild update name is invalid")
            locked.name = name
        for field, maximum in (("description", 500), ("icon_hash", 128), ("banner_hash", 128)):
            if field not in raw:
                continue
            value = raw.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > maximum):
                raise ValueError(f"guild update {field} is invalid")
            setattr(locked, field, value)
        if "federated_history_policy" in raw:
            history_policy = raw.get("federated_history_policy")
            if history_policy not in {"disabled", "full_retained"}:
                raise ValueError("guild history policy is invalid")
            locked.federated_history_policy = str(history_policy)
        if "history_policy_generation" in raw:
            locked.history_policy_generation = database_snowflake(
                raw.get("history_policy_generation"), "history policy generation"
            )
        if "owner_id" in raw or "owner_domain" in raw:
            if "owner_id" not in raw:
                raise ValueError("guild owner identity is incomplete")
            owner_ref = (
                database_snowflake(raw.get("owner_id"), "guild owner id"),
                normalize_domain(str(raw.get("owner_domain", locked.origin_domain))),
            )
            owner = await session.get(User, owner_ref)
            owner_membership = await session.get(
                GuildMember,
                (locked.id, locked.origin_domain, owner_ref[0], owner_ref[1]),
            )
            if owner is None or owner_membership is None:
                raise ValueError("guild owner is not a guild member")
            locked.owner_id, locked.owner_domain = owner_ref
        if "permission_generation" in raw:
            locked.permission_generation = database_snowflake(
                raw.get("permission_generation"), "permission generation"
            )
        dispatch = {**dispatch, **raw}
    elif event_type == "guild.tracker.board.invalidate":
        await apply_tracker_invalidation(session, locked, content, context)
        dispatch = {}
    elif event_type == "guild.forum.cursor.update":
        forum_ref = _event_ref(content.get("forum"), "forum")
        forum = await session.get(Channel, forum_ref)
        if (
            forum is None
            or forum.type != 15
            or (forum.guild_id, forum.guild_domain) != (locked.id, locked.origin_domain)
        ):
            raise ValueError("forum cursor mutation is invalid")
        raw_last_id = content.get("last_thread_id")
        raw_last_domain = content.get("last_thread_domain")
        if (raw_last_id is None) != (raw_last_domain is None):
            raise ValueError("forum cursor identity is incomplete")
        if raw_last_id is None:
            forum.last_thread_id = None
            forum.last_thread_domain = None
        else:
            last_ref = (
                database_snowflake(raw_last_id, "forum cursor thread id"),
                normalize_domain(str(raw_last_domain)),
            )
            last_thread = await session.get(Channel, last_ref)
            if (
                last_thread is None
                or last_thread.type != 11
                or last_thread.unavailable
                or (last_thread.parent_id, last_thread.parent_domain) != forum_ref
            ):
                raise ValueError("forum cursor thread is invalid")
            forum.last_thread_id, forum.last_thread_domain = last_ref
        dispatch_type = "CHANNEL_UPDATE"
        dispatch = {}
    elif event_type in {"guild.channel.create", "guild.channel.update"}:
        raw = content.get("channel")
        channel_ref = _event_ref(raw, "channel")
        if not isinstance(raw, dict) or channel_ref[1] != locked.origin_domain:
            raise ValueError("channel mutation identity is invalid")
        if (
            database_snowflake(raw.get("guild_id"), "channel guild id"),
            normalize_domain(str(raw.get("guild_domain", ""))),
        ) != (locked.id, locked.origin_domain):
            raise ValueError("channel mutation references the wrong guild")
        channel_type = raw.get("type")
        name = raw.get("name")
        topic = raw.get("topic")
        position = raw.get("position")
        slowmode = raw.get("rate_limit_per_user")
        history_policy = raw.get("federated_history_policy", "inherit")
        raw_encryption_policy = raw.get("encryption_policy")
        if raw_encryption_policy is None:
            legacy_mode = raw.get("encryption_mode", "plaintext")
            raw_encryption_policy = {
                "mode": legacy_mode,
                "state": "legacy" if legacy_mode == "e2ee" else "plaintext",
                "generation": "0",
            }
        encryption_policy = validate_channel_encryption_policy(raw_encryption_policy)
        if isinstance(channel_type, bool) or channel_type not in GUILD_CHANNEL_TYPES:
            raise ValueError("channel mutation type is invalid")
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise ValueError("channel mutation name is invalid")
        topic_limit = 4096 if channel_type == 15 else 1024
        if topic is not None and (not isinstance(topic, str) or len(topic) > topic_limit):
            raise ValueError("channel mutation topic is invalid")
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise ValueError("channel mutation position is invalid")
        if (
            isinstance(slowmode, bool)
            or not isinstance(slowmode, int)
            or not 0 <= slowmode <= 21600
        ):
            raise ValueError("channel mutation slowmode is invalid")
        if history_policy not in {"inherit", "disabled", "full_retained"}:
            raise ValueError("channel history policy is invalid")
        voice_state = _validated_voice_channel_state(raw, int(channel_type))
        parent_id = (
            database_snowflake(raw.get("parent_id"), "parent channel id")
            if raw.get("parent_id") is not None
            else None
        )
        parent_domain = (
            normalize_domain(str(raw.get("parent_domain", ""))) if parent_id is not None else None
        )
        permissions_synced = raw.get("permissions_synced", parent_id is not None)
        if (
            not isinstance(permissions_synced, bool)
            or permissions_synced
            and (parent_id is None or channel_type in {4, 10, 11, 12})
            or channel_type in {10, 11, 12}
            and permissions_synced
        ):
            raise ValueError("channel permission sync state is invalid")
        if parent_id is not None:
            parent = await session.get(Channel, (parent_id, parent_domain))
            expected_parent_types = expected_channel_parent_types(int(channel_type))
            if (
                parent is None
                or (parent.guild_id, parent.guild_domain) != (locked.id, locked.origin_domain)
                or parent.type not in expected_parent_types
            ):
                raise ValueError("channel mutation parent is invalid")
        elif channel_type in {10, 11, 12}:
            raise ValueError("thread mutation is missing its parent")
        created_floor_id = database_snowflake(raw.get("created_floor_id"), "channel history floor")
        created_at = _event_datetime(raw.get("created_at"), "channel creation", optional=True)
        extension_state = _validated_channel_extension_state(
            raw, int(channel_type), locked.origin_domain
        )
        last_thread_id = extension_state.get("last_thread_id")
        last_thread_domain = extension_state.get("last_thread_domain")
        if last_thread_id is not None and last_thread_domain is not None:
            last_thread = await session.get(Channel, (last_thread_id, last_thread_domain))
            if (
                last_thread is None
                or last_thread.type not in {10, 11, 12}
                or (last_thread.parent_id, last_thread.parent_domain) != channel_ref
                or (last_thread.guild_id, last_thread.guild_domain)
                != (locked.id, locked.origin_domain)
            ):
                raise ValueError("forum last thread identity is invalid")
        channel = await session.get(Channel, channel_ref)
        if channel is None:
            channel = Channel(
                id=channel_ref[0],
                origin_domain=channel_ref[1],
                guild_id=locked.id,
                guild_domain=locked.origin_domain,
                type=int(channel_type),
                name=name,
                topic=topic,
                position=position,
                parent_id=parent_id,
                parent_domain=parent_domain,
                permissions_synced=permissions_synced,
                rate_limit_per_user=slowmode,
                **voice_state,
                federated_history_policy=str(history_policy),
                encryption_mode=str(encryption_policy["mode"]),
                encryption_state=str(encryption_policy["state"]),
                encryption_policy_generation=int(encryption_policy["generation"]),
                encryption_protocol=encryption_policy["protocol"],
                encryption_suite=encryption_policy["suite"],
                encryption_group_id=encryption_policy["group_id"],
                encryption_epoch=encryption_policy["epoch"],
                created_floor_id=created_floor_id,
                **extension_state,
            )
            if created_at is not None:
                channel.created_at = created_at
            session.add(channel)
        elif (channel.guild_id, channel.guild_domain) != (locked.id, locked.origin_domain):
            raise ValueError("channel mutation conflicts with another channel")
        else:
            channel.type = int(channel_type)
            channel.name = name
            channel.topic = topic
            channel.position = position
            channel.parent_id = parent_id
            channel.parent_domain = parent_domain
            channel.permissions_synced = permissions_synced
            channel.rate_limit_per_user = slowmode
            for field, value in voice_state.items():
                setattr(channel, field, value)
            channel.federated_history_policy = str(history_policy)
            incoming_generation = int(encryption_policy["generation"])
            validate_channel_encryption_policy_transition(
                channel,
                encryption_policy,
                label="channel",
            )
            channel.encryption_mode = str(encryption_policy["mode"])
            channel.encryption_state = str(encryption_policy["state"])
            channel.encryption_policy_generation = incoming_generation
            channel.encryption_protocol = encryption_policy["protocol"]
            channel.encryption_suite = encryption_policy["suite"]
            channel.encryption_group_id = encryption_policy["group_id"]
            channel.encryption_epoch = encryption_policy["epoch"]
            channel.unavailable = False
            if created_at is not None and channel.created_at != created_at:
                # Creation time is immutable. A replica learned before this
                # field was federated must refresh from a snapshot rather than
                # accept a mutable Date Posted cursor from a later update.
                raise ValueError("channel mutation changed its creation timestamp")
            for field, value in extension_state.items():
                setattr(channel, field, value)
        channel_version = _apply_event_resource_version(channel, raw, "channel")
        if channel_version is not None:
            versioned_resources.append((channel, channel_version))
        if channel.type in {10, 11, 12} and event_type.endswith("create"):
            owner_member = await session.get(
                GuildMember,
                (
                    locked.id,
                    locked.origin_domain,
                    channel.owner_id,
                    channel.owner_domain,
                ),
            )
            owner_user = await session.get(User, (channel.owner_id, channel.owner_domain))
            if owner_member is None or owner_user is None:
                raise ValueError("thread owner is not a known guild member")
            await session.execute(
                pg_insert(ThreadMember)
                .values(
                    thread_id=channel.id,
                    thread_domain=channel.origin_domain,
                    guild_id=locked.id,
                    guild_domain=locked.origin_domain,
                    user_id=channel.owner_id,
                    user_domain=channel.owner_domain,
                    joined_at=channel.archive_timestamp,
                    flags=0,
                    notification_level="inherit",
                )
                .on_conflict_do_nothing()
            )
        if channel.type in {10, 11, 12}:
            dispatch_type = "THREAD_CREATE" if event_type.endswith("create") else "THREAD_UPDATE"
        else:
            dispatch_type = "CHANNEL_CREATE" if event_type.endswith("create") else "CHANNEL_UPDATE"
        dispatch = dict(raw)
        if "default_reaction_emoji" in raw:
            dispatch["default_reaction_emoji"] = extension_state["default_reaction_emoji"]
        if dispatch_type == "THREAD_CREATE":
            dispatch["newly_created"] = True
    elif event_type == "guild.channel.delete":
        raw_deleted_channel = content.get("channel")
        if not isinstance(raw_deleted_channel, dict):
            raise ValueError("channel deletion payload is invalid")
        channel_ref = _event_ref(raw_deleted_channel, "channel")
        channel = await session.get(Channel, channel_ref)
        raw_deleted_type = raw_deleted_channel.get("type")
        if raw_deleted_type is not None and (
            isinstance(raw_deleted_type, bool) or raw_deleted_type not in GUILD_CHANNEL_TYPES
        ):
            raise ValueError("channel deletion type is invalid")
        raw_guild_id = raw_deleted_channel.get("guild_id")
        raw_guild_domain = raw_deleted_channel.get("guild_domain")
        if (raw_guild_id is not None or raw_guild_domain is not None) and (
            database_snowflake(raw_guild_id, "deleted channel guild id"),
            normalize_domain(str(raw_guild_domain)),
        ) != (locked.id, locked.origin_domain):
            raise ValueError("channel deletion references the wrong guild")
        raw_parent_id = raw_deleted_channel.get("parent_id")
        raw_parent_domain = raw_deleted_channel.get("parent_domain")
        if (raw_parent_id is None) != (raw_parent_domain is None):
            raise ValueError("deleted channel parent identity is incomplete")
        deleted_parent_ref = (
            (
                database_snowflake(raw_parent_id, "deleted channel parent id"),
                normalize_domain(str(raw_parent_domain)),
            )
            if raw_parent_id is not None
            else None
        )
        deleted_type = channel.type if channel is not None else raw_deleted_type
        if channel is not None:
            if (channel.guild_id, channel.guild_domain) != (locked.id, locked.origin_domain):
                raise ValueError("channel deletion references the wrong guild")
            if raw_deleted_type is not None and raw_deleted_type != channel.type:
                raise ValueError("channel deletion type conflicts with replica state")
            if deleted_parent_ref is not None and deleted_parent_ref != (
                channel.parent_id,
                channel.parent_domain,
            ):
                raise ValueError("channel deletion parent conflicts with replica state")
            if (
                deleted_parent_ref is None
                and channel.parent_id is not None
                and channel.parent_domain is not None
            ):
                deleted_parent_ref = (channel.parent_id, channel.parent_domain)
            await purge_replicated_channel_cache(session, settings, channel)
        dispatch_type = "THREAD_DELETE" if deleted_type in {10, 11, 12} else "CHANNEL_DELETE"
        dispatch = {
            **dispatch,
            "id": str(channel_ref[0]),
            "origin_domain": channel_ref[1],
            "guild_id": str(locked.id),
            "guild_domain": locked.origin_domain,
            "type": deleted_type,
            "parent_id": (str(deleted_parent_ref[0]) if deleted_parent_ref is not None else None),
            "parent_domain": (deleted_parent_ref[1] if deleted_parent_ref is not None else None),
        }
    elif event_type in {"guild.thread.member.upsert", "guild.thread.member.delete"}:
        raw_member = content.get("member")
        if event_type.endswith("upsert"):
            if not isinstance(raw_member, dict):
                raise ValueError("thread member mutation is invalid")
            thread_ref = (
                database_snowflake(raw_member.get("id"), "thread id"),
                normalize_domain(str(raw_member.get("thread_domain", ""))),
            )
            user_ref = (
                database_snowflake(raw_member.get("user_id"), "thread member user id"),
                normalize_domain(str(raw_member.get("user_domain", ""))),
            )
            if (
                database_snowflake(raw_member.get("guild_id"), "thread member guild id"),
                normalize_domain(str(raw_member.get("guild_domain", ""))),
            ) != (locked.id, locked.origin_domain):
                raise ValueError("thread member mutation references the wrong guild")
            joined_at = _event_datetime(raw_member.get("join_timestamp"), "thread member join")
            flags = _bounded_event_int(raw_member.get("flags", 0), "thread member flags")
            notification_level = raw_member.get("notification_level", "inherit")
            if notification_level not in {"inherit", "all", "mentions", "none"}:
                raise ValueError("thread member notification level is invalid")
        else:
            thread_ref = (
                database_snowflake(content.get("thread_id"), "thread id"),
                normalize_domain(str(content.get("thread_domain", ""))),
            )
            user_ref = (
                database_snowflake(content.get("user_id"), "thread member user id"),
                normalize_domain(str(content.get("user_domain", ""))),
            )
            joined_at = None
            flags = 0
            notification_level = "inherit"
        thread_channel = await session.get(Channel, thread_ref)
        guild_member = await session.get(
            GuildMember,
            (locked.id, locked.origin_domain, user_ref[0], user_ref[1]),
        )
        if (
            thread_channel is None
            or thread_channel.type not in {10, 11, 12}
            or (thread_channel.guild_id, thread_channel.guild_domain)
            != (locked.id, locked.origin_domain)
            or (event_type.endswith("upsert") and guild_member is None)
        ):
            raise ValueError("thread member mutation is invalid")
        if event_type.endswith("upsert"):
            await session.execute(
                pg_insert(ThreadMember)
                .values(
                    thread_id=thread_ref[0],
                    thread_domain=thread_ref[1],
                    guild_id=locked.id,
                    guild_domain=locked.origin_domain,
                    user_id=user_ref[0],
                    user_domain=user_ref[1],
                    joined_at=joined_at,
                    flags=flags,
                    notification_level=notification_level,
                )
                .on_conflict_do_update(
                    index_elements=[
                        "thread_id",
                        "thread_domain",
                        "user_id",
                        "user_domain",
                    ],
                    set_={
                        "flags": flags,
                        "notification_level": notification_level,
                    },
                )
            )
        else:
            await session.execute(
                delete(ThreadMember).where(
                    ThreadMember.thread_id == thread_ref[0],
                    ThreadMember.thread_domain == thread_ref[1],
                    ThreadMember.user_id == user_ref[0],
                    ThreadMember.user_domain == user_ref[1],
                )
            )
        await session.flush()
        reported_member_count = _bounded_event_int(
            content.get("member_count"),
            "thread member count",
            maximum=1000,
            optional=True,
        )
        thread_channel.member_count = (
            reported_member_count
            if reported_member_count is not None
            else int(
                await session.scalar(
                    select(func.count())
                    .select_from(ThreadMember)
                    .where(
                        ThreadMember.thread_id == thread_ref[0],
                        ThreadMember.thread_domain == thread_ref[1],
                    )
                )
                or 0
            )
        )
        if thread_channel.type == 12:
            locked.permission_generation += 1
        added_members: list[dict[str, object]] = []
        if event_type.endswith("upsert"):
            persisted_member = await session.get(
                ThreadMember,
                (thread_ref[0], thread_ref[1], user_ref[0], user_ref[1]),
            )
            if persisted_member is None:
                raise ValueError("thread member mutation did not persist")
            added_members.append(await rich_thread_member_payload(session, persisted_member))
        dispatch_type = "THREAD_MEMBERS_UPDATE"
        dispatch = {
            "id": str(thread_ref[0]),
            "thread_domain": thread_ref[1],
            "guild_id": str(locked.id),
            "guild_domain": locked.origin_domain,
            "member_count": min(50, thread_channel.member_count),
            "added_members": added_members,
            "removed_member_ids": [str(user_ref[0])] if event_type.endswith("delete") else [],
            "removed_member_refs": (
                [{"id": str(user_ref[0]), "origin_domain": user_ref[1]}]
                if event_type.endswith("delete")
                else []
            ),
        }
    elif event_type in {"guild.role.create", "guild.role.update"}:
        raw = content.get("role")
        role_ref = _event_ref(raw, "role")
        if not isinstance(raw, dict) or role_ref[1] != locked.origin_domain:
            raise ValueError("role mutation identity is invalid")
        if (
            database_snowflake(raw.get("guild_id"), "role guild id"),
            normalize_domain(str(raw.get("guild_domain", ""))),
        ) != (locked.id, locked.origin_domain):
            raise ValueError("role mutation references the wrong guild")
        name = raw.get("name")
        color = raw.get("color")
        position = raw.get("position")
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise ValueError("role mutation name is invalid")
        if isinstance(color, bool) or not isinstance(color, int) or not 0 <= color <= 0xFFFFFF:
            raise ValueError("role mutation color is invalid")
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise ValueError("role mutation position is invalid")
        permissions = database_snowflake(raw.get("permissions"), "role permissions")
        if permissions & ~ALL_PERMISSIONS:
            raise ValueError("role mutation contains unknown permissions")
        icon_hash = raw.get("icon_hash")
        if icon_hash is not None and (
            not isinstance(icon_hash, str) or not valid_content_digest(icon_hash)
        ):
            raise ValueError("role mutation icon hash is invalid")
        if not isinstance(raw.get("hoist"), bool) or not isinstance(raw.get("mentionable"), bool):
            raise ValueError("role mutation flags are invalid")
        role = await session.get(Role, role_ref)
        if role is None:
            role = Role(
                id=role_ref[0],
                origin_domain=role_ref[1],
                guild_id=locked.id,
                guild_domain=locked.origin_domain,
                name=name,
                icon_hash=icon_hash,
                color=color,
                permissions=permissions,
                position=position,
                hoist=bool(raw["hoist"]),
                mentionable=bool(raw["mentionable"]),
            )
            session.add(role)
        elif (role.guild_id, role.guild_domain) != (locked.id, locked.origin_domain):
            raise ValueError("role mutation conflicts with another role")
        else:
            role.name = name
            role.icon_hash = icon_hash
            role.color = color
            role.permissions = permissions
            role.position = position
            role.hoist = bool(raw["hoist"])
            role.mentionable = bool(raw["mentionable"])
        role_version = _apply_event_resource_version(role, raw, "role")
        if role_version is not None:
            versioned_resources.append((role, role_version))
        dispatch = dict(raw)
    elif event_type == "guild.role.delete":
        role_ref = _event_ref(content.get("role"), "role")
        role = await session.get(Role, role_ref)
        if role is not None:
            if role.id == locked.id or (role.guild_id, role.guild_domain) != (
                locked.id,
                locked.origin_domain,
            ):
                raise ValueError("role deletion is invalid")
            await session.delete(role)
        dispatch["deleted_role_id"] = str(role_ref[0])
    elif event_type in {
        "guild.emoji.create",
        "guild.emoji.update",
        "guild.emoji.delete",
    }:
        raw = content.get("emoji")
        emoji_ref = _event_ref(raw, "emoji")
        if not isinstance(raw, dict) or emoji_ref[1] != locked.origin_domain:
            raise ValueError("emoji mutation identity is invalid")
        if (
            database_snowflake(raw.get("guild_id"), "emoji guild id"),
            normalize_domain(str(raw.get("guild_domain", ""))),
        ) != (locked.id, locked.origin_domain):
            raise ValueError("emoji mutation references the wrong guild")
        if not event_type.endswith("delete"):
            name = raw.get("name")
            media_hash = raw.get("media_hash")
            available = raw.get("available", True)
            role_refs = raw.get("roles", [])
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z0-9_]{2,32}", name) is None
                or not isinstance(media_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", media_hash) is None
                or not isinstance(raw.get("animated"), bool)
                or not isinstance(available, bool)
                or not isinstance(role_refs, list)
                or len(role_refs) > 100
            ):
                raise ValueError("emoji mutation fields are invalid")
            parsed_roles: list[tuple[int, str]] = []
            for role_ref in role_refs:
                if not isinstance(role_ref, str) or "@" not in role_ref:
                    raise ValueError("emoji mutation role reference is invalid")
                raw_id, raw_domain = role_ref.rsplit("@", 1)
                resolved_role = (
                    database_snowflake(raw_id, "emoji role id"),
                    normalize_domain(raw_domain),
                )
                if resolved_role[1] != locked.origin_domain or resolved_role[0] == locked.id:
                    raise ValueError("emoji mutation role is outside the guild")
                role = await session.get(Role, resolved_role)
                if role is None or (role.guild_id, role.guild_domain) != (
                    locked.id,
                    locked.origin_domain,
                ):
                    raise ValueError("emoji mutation role does not exist")
                parsed_roles.append(resolved_role)
            if len(parsed_roles) != len(set(parsed_roles)):
                raise ValueError("emoji mutation roles must be unique")
            duplicate_name = await session.scalar(
                select(Emoji.id).where(
                    Emoji.guild_id == locked.id,
                    Emoji.guild_domain == locked.origin_domain,
                    func.lower(Emoji.name) == name.casefold(),
                    (Emoji.id != emoji_ref[0]) | (Emoji.origin_domain != emoji_ref[1]),
                )
            )
            if duplicate_name is not None:
                raise ValueError("emoji mutation name conflicts with another emoji")
            emoji = await session.get(Emoji, emoji_ref)
            if emoji is None:
                emoji = Emoji(
                    id=emoji_ref[0],
                    origin_domain=emoji_ref[1],
                    guild_id=locked.id,
                    guild_domain=locked.origin_domain,
                    name=name,
                    object_key=f"remote:{emoji_ref[1]}:{emoji_ref[0]}",
                    creator_id=actor.id,
                    creator_domain=actor.origin_domain,
                )
                session.add(emoji)
            elif (emoji.guild_id, emoji.guild_domain) != (locked.id, locked.origin_domain):
                raise ValueError("emoji mutation conflicts with another guild")
            emoji.name = name
            emoji.animated = bool(raw["animated"])
            emoji.available = available
            emoji.media_hash = media_hash
            emoji_version = _apply_event_resource_version(emoji, raw, "emoji")
            if emoji_version is not None:
                versioned_resources.append((emoji, emoji_version))
            await session.execute(
                delete(EmojiRoleRestriction).where(
                    EmojiRoleRestriction.emoji_id == emoji.id,
                    EmojiRoleRestriction.emoji_domain == emoji.origin_domain,
                )
            )
            for role_id, role_domain in parsed_roles:
                session.add(
                    EmojiRoleRestriction(
                        emoji_id=emoji.id,
                        emoji_domain=emoji.origin_domain,
                        role_id=role_id,
                        role_domain=role_domain,
                        guild_id=locked.id,
                        guild_domain=locked.origin_domain,
                    )
                )
            dispatch_type = (
                "GUILD_EMOJI_CREATE" if event_type.endswith("create") else "GUILD_EMOJI_UPDATE"
            )
        else:
            emoji = await session.get(Emoji, emoji_ref)
            if emoji is not None:
                if (emoji.guild_id, emoji.guild_domain) != (
                    locked.id,
                    locked.origin_domain,
                ):
                    raise ValueError("emoji deletion references the wrong guild")
                await session.delete(emoji)
            dispatch_type = "GUILD_EMOJI_DELETE"
        dispatch = dict(raw)
    elif event_type in {
        "guild.sticker.create",
        "guild.sticker.update",
        "guild.sticker.delete",
    }:
        raw = content.get("sticker")
        sticker_ref = _event_ref(raw, "sticker")
        if not isinstance(raw, dict) or sticker_ref[1] != locked.origin_domain:
            raise ValueError("sticker mutation identity is invalid")
        if (
            database_snowflake(raw.get("guild_id"), "sticker guild id"),
            normalize_domain(str(raw.get("guild_domain", ""))),
        ) != (locked.id, locked.origin_domain):
            raise ValueError("sticker mutation references the wrong guild")
        if not event_type.endswith("delete"):
            name = raw.get("name")
            description = raw.get("description")
            media_hash = raw.get("media_hash")
            available = raw.get("available", True)
            tags = raw.get("tags", [])
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z0-9_]{2,32}", name) is None
                or (
                    description is not None
                    and (not isinstance(description, str) or len(description) > 100)
                )
                or not isinstance(media_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", media_hash) is None
                or not isinstance(raw.get("animated"), bool)
                or not isinstance(available, bool)
                or not isinstance(tags, list)
                or not 1 <= len(tags) <= 10
                or any(not isinstance(tag, str) or not tag or len(tag) > 100 for tag in tags)
                or len(tags) != len(set(tags))
            ):
                raise ValueError("sticker mutation fields are invalid")
            duplicate_name = await session.scalar(
                select(Sticker.id).where(
                    Sticker.guild_id == locked.id,
                    Sticker.guild_domain == locked.origin_domain,
                    func.lower(Sticker.name) == name.casefold(),
                    (Sticker.id != sticker_ref[0]) | (Sticker.origin_domain != sticker_ref[1]),
                )
            )
            if duplicate_name is not None:
                raise ValueError("sticker mutation name conflicts with another sticker")
            sticker = await session.get(Sticker, sticker_ref)
            if sticker is None:
                sticker = Sticker(
                    id=sticker_ref[0],
                    origin_domain=sticker_ref[1],
                    guild_id=locked.id,
                    guild_domain=locked.origin_domain,
                    name=name,
                    creator_id=actor.id,
                    creator_domain=actor.origin_domain,
                )
                session.add(sticker)
            elif (sticker.guild_id, sticker.guild_domain) != (locked.id, locked.origin_domain):
                raise ValueError("sticker mutation conflicts with another guild")
            sticker.name = name
            sticker.description = description
            sticker.animated = bool(raw["animated"])
            sticker.available = available
            sticker.tags = tags
            sticker.media_hash = media_hash
            sticker_version = _apply_event_resource_version(sticker, raw, "sticker")
            if sticker_version is not None:
                versioned_resources.append((sticker, sticker_version))
            dispatch_type = (
                "GUILD_STICKER_CREATE" if event_type.endswith("create") else "GUILD_STICKER_UPDATE"
            )
        else:
            sticker = await session.get(Sticker, sticker_ref)
            if sticker is not None:
                if (sticker.guild_id, sticker.guild_domain) != (locked.id, locked.origin_domain):
                    raise ValueError("sticker deletion references the wrong guild")
                await session.delete(sticker)
            dispatch_type = "GUILD_STICKER_DELETE"
        dispatch = dict(raw)
    elif event_type in {"guild.overwrite.upsert", "guild.overwrite.delete"}:
        raw = content.get("overwrite")
        if not isinstance(raw, dict):
            raise ValueError("overwrite mutation is invalid")
        channel_ref = _event_ref(raw.get("channel"), "channel")
        target_ref = _event_ref(raw.get("target"), "overwrite target")
        target_type = raw.get("target_type")
        allow = (
            database_snowflake(raw.get("allow"), "overwrite allow mask")
            if event_type.endswith("upsert")
            else 0
        )
        deny = (
            database_snowflake(raw.get("deny"), "overwrite deny mask")
            if event_type.endswith("upsert")
            else 0
        )
        if target_type not in {"role", "member"} or allow & deny:
            raise ValueError("overwrite mutation masks or target type are invalid")
        if (allow | deny) & ~ALL_PERMISSIONS:
            raise ValueError("overwrite mutation contains unknown permissions")
        channel = await session.get(Channel, channel_ref)
        if channel is None or (channel.guild_id, channel.guild_domain) != (
            locked.id,
            locked.origin_domain,
        ):
            raise ValueError("overwrite channel is invalid")
        if target_type == "role":
            role_target = await session.get(Role, target_ref)
            valid_target = role_target is not None and (
                role_target.guild_id,
                role_target.guild_domain,
            ) == (
                locked.id,
                locked.origin_domain,
            )
        else:
            member_target = await session.get(
                GuildMember,
                (locked.id, locked.origin_domain, target_ref[0], target_ref[1]),
            )
            valid_target = member_target is not None
        if not valid_target:
            raise ValueError("overwrite target is invalid")
        # Treat legacy zero-mask upserts as deletes. Empty overwrite rows have
        # no semantic value and previously made deny -> inherit transitions
        # unnecessarily dependent on cache invalidation behavior.
        if event_type.endswith("upsert") and (allow or deny):
            await session.execute(
                pg_insert(ChannelOverwrite)
                .values(
                    channel_id=channel.id,
                    channel_domain=channel.origin_domain,
                    guild_id=locked.id,
                    guild_domain=locked.origin_domain,
                    target_id=target_ref[0],
                    target_domain=target_ref[1],
                    target_type=target_type,
                    allow=allow,
                    deny=deny,
                )
                .on_conflict_do_update(
                    index_elements=[
                        "channel_id",
                        "channel_domain",
                        "target_id",
                        "target_domain",
                        "target_type",
                    ],
                    set_={"allow": allow, "deny": deny},
                )
            )
        else:
            await session.execute(
                delete(ChannelOverwrite).where(
                    ChannelOverwrite.channel_id == channel.id,
                    ChannelOverwrite.channel_domain == channel.origin_domain,
                    ChannelOverwrite.target_id == target_ref[0],
                    ChannelOverwrite.target_domain == target_ref[1],
                    ChannelOverwrite.target_type == target_type,
                )
            )
        dispatch_type = "CHANNEL_UPDATE"
        dispatch = {"id": str(channel.id), "origin_domain": channel.origin_domain}
    elif event_type == "guild.member.update":
        raw = content.get("member")
        if not isinstance(raw, dict):
            raise ValueError("member mutation is invalid")
        user_ref = _event_ref(raw.get("user"), "member user")
        member = await session.get(
            GuildMember, (locked.id, locked.origin_domain, user_ref[0], user_ref[1])
        )
        if member is None:
            raise ValueError("member mutation references an unknown member")
        nickname = raw.get("nickname")
        if nickname is not None and (not isinstance(nickname, str) or len(nickname) > 100):
            raise ValueError("member nickname is invalid")
        member.nickname = nickname
        member.timeout_until = _event_datetime(
            raw.get("timeout_until"), "member timeout", optional=True
        )
        timeout_indefinite = raw.get("timeout_indefinite", False)
        if not isinstance(timeout_indefinite, bool):
            raise ValueError("member indefinite timeout is invalid")
        if timeout_indefinite and member.timeout_until is not None:
            raise ValueError("member timeout modes conflict")
        member.timeout_indefinite = timeout_indefinite
        # Moderation reasons and persistent voice moderation flags are private
        # authority state. Older peers may still include them, but replicas do
        # not retain or fan them out to every participating instance.
        timeout_reason = raw.get("timeout_reason")
        if timeout_reason is not None and (
            not isinstance(timeout_reason, str) or len(timeout_reason) > 512
        ):
            raise ValueError("member timeout reason is invalid")
        if timeout_reason is not None and not (timeout_indefinite or member.timeout_until):
            raise ValueError("member timeout reason exists without an active timeout")
        member.timeout_reason = None
        voice_flags = raw.get("voice_flags")
        if voice_flags is not None and (
            isinstance(voice_flags, bool) or not isinstance(voice_flags, int) or voice_flags < 0
        ):
            raise ValueError("member voice flags are invalid")
        member.voice_flags = 0
        member_version = database_snowflake(raw.get("member_version"), "member version")
        if member_version < member.member_version:
            raise ValueError("member version regressed")
        member.member_version = member_version
        dispatch_type = "GUILD_MEMBER_UPDATE"
        dispatch = {
            "user": {"id": str(user_ref[0]), "origin_domain": user_ref[1]},
            "nickname": member.nickname,
            "timeout_until": (
                member.timeout_until.isoformat() if member.timeout_until is not None else None
            ),
            "timeout_indefinite": member.timeout_indefinite,
            "member_version": str(member.member_version),
        }
    elif event_type == GUILD_PROFILE_RELAY_EVENT:
        if set(content) != {"source"}:
            raise ValueError("guild profile relay body is invalid")
        profile = await validated_guild_profile_source(
            session,
            settings,
            content["source"],
            guild_ref=(locked.id, locked.origin_domain),
        )
        user_ref = (database_snowflake(profile.id, "profile user id"), profile.origin_domain)
        member = await session.get(
            GuildMember,
            (locked.id, locked.origin_domain, user_ref[0], user_ref[1]),
        )
        if member is None:
            raise ValueError("guild profile relay references a non-member")
        profile_user = await upsert_remote_user(session, settings, profile)
        dispatch_type = "GUILD_MEMBER_UPDATE"
        dispatch = await guild_profile_member_payload(session, locked, profile_user)
    elif event_type == "guild.member.remove":
        user_ref = _event_ref(content.get("user"), "member user")
        member = await session.get(
            GuildMember, (locked.id, locked.origin_domain, user_ref[0], user_ref[1])
        )
        if member is not None:
            if (member.user_id, member.user_domain) == (locked.owner_id, locked.owner_domain):
                raise ValueError("guild owner cannot be removed")
            await session.delete(member)
        if member is not None and user_ref[1] == settings.domain:
            await mark_remote_guild_departed(
                session,
                settings,
                guild_id=locked.id,
                guild_domain=locked.origin_domain,
                user_id=user_ref[0],
                user_domain=user_ref[1],
            )
        dispatch_type = "GUILD_MEMBER_REMOVE"
        dispatch = {**dispatch, "user_id": str(user_ref[0]), "user_domain": user_ref[1]}
    elif event_type == "guild.members.origin.remove":
        origin_domain = normalize_domain(str(content.get("origin_domain", "")))
        if origin_domain == locked.owner_domain:
            raise ValueError("guild owner origin cannot be removed")
        local_user_ids: list[int] = []
        if origin_domain == settings.domain:
            local_user_ids = list(
                await session.scalars(
                    select(GuildMember.user_id).where(
                        GuildMember.guild_id == locked.id,
                        GuildMember.guild_domain == locked.origin_domain,
                        GuildMember.user_domain == settings.domain,
                    )
                )
            )
        await session.execute(
            delete(GuildMember).where(
                GuildMember.guild_id == locked.id,
                GuildMember.guild_domain == locked.origin_domain,
                GuildMember.user_domain == origin_domain,
            )
        )
        if origin_domain == settings.domain:
            for local_user_id in local_user_ids:
                await mark_remote_guild_departed(
                    session,
                    settings,
                    guild_id=locked.id,
                    guild_domain=locked.origin_domain,
                    user_id=local_user_id,
                    user_domain=settings.domain,
                )
        dispatch_type = "GUILD_UPDATE"
        dispatch = {**dispatch, "members_removed_origin": origin_domain}
    elif event_type in {"guild.member.role.add", "guild.member.role.remove"}:
        user_ref = _event_ref(content.get("user"), "member user")
        role_ref = _event_ref(content.get("role"), "role")
        member = await session.get(
            GuildMember, (locked.id, locked.origin_domain, user_ref[0], user_ref[1])
        )
        role = await session.get(Role, role_ref)
        if (
            member is None
            or role is None
            or (role.guild_id, role.guild_domain)
            != (
                locked.id,
                locked.origin_domain,
            )
        ):
            raise ValueError("member-role mutation is invalid")
        if event_type.endswith("add"):
            member.temporary = False
            await session.execute(
                pg_insert(MemberRole)
                .values(
                    guild_id=locked.id,
                    guild_domain=locked.origin_domain,
                    user_id=user_ref[0],
                    user_domain=user_ref[1],
                    role_id=role_ref[0],
                    role_domain=role_ref[1],
                )
                .on_conflict_do_nothing()
            )
        else:
            await session.execute(
                delete(MemberRole).where(
                    MemberRole.guild_id == locked.id,
                    MemberRole.guild_domain == locked.origin_domain,
                    MemberRole.user_id == user_ref[0],
                    MemberRole.user_domain == user_ref[1],
                    MemberRole.role_id == role_ref[0],
                    MemberRole.role_domain == role_ref[1],
                )
            )
        member_version = database_snowflake(content.get("member_version"), "member version")
        if member_version < member.member_version:
            raise ValueError("member version regressed")
        member.member_version = member_version
        user = await session.get(User, user_ref)
        if user is None:
            raise ValueError("member-role mutation user is unknown")
        role_ids = list(
            await session.scalars(
                select(MemberRole.role_id).where(
                    MemberRole.guild_id == locked.id,
                    MemberRole.guild_domain == locked.origin_domain,
                    MemberRole.user_id == user_ref[0],
                    MemberRole.user_domain == user_ref[1],
                )
            )
        )
        dispatch_type = "GUILD_MEMBER_UPDATE"
        dispatch = member_payload(
            member,
            user,
            role_ids,
            include_private_authority_state=False,
        )
    elif event_type in {"guild.ban.add", "guild.ban.remove"}:
        user_ref = _event_ref(content.get("user"), "banned user")
        user = await session.get(User, user_ref)
        if event_type.endswith("add") and user is not None:
            expires_at = _event_datetime(content.get("expires_at"), "ban expiry", optional=True)
            await session.execute(
                pg_insert(Ban)
                .values(
                    guild_id=locked.id,
                    guild_domain=locked.origin_domain,
                    user_id=user_ref[0],
                    user_domain=user_ref[1],
                    reason=None,
                    actor_id=actor_ref[0],
                    actor_domain=actor_ref[1],
                    expires_at=expires_at,
                )
                .on_conflict_do_update(
                    index_elements=["guild_id", "guild_domain", "user_id", "user_domain"],
                    set_={
                        "actor_id": actor_ref[0],
                        "actor_domain": actor_ref[1],
                        "expires_at": expires_at,
                    },
                )
            )
        else:
            await session.execute(
                delete(Ban).where(
                    Ban.guild_id == locked.id,
                    Ban.guild_domain == locked.origin_domain,
                    Ban.user_id == user_ref[0],
                    Ban.user_domain == user_ref[1],
                )
            )
        dispatch = {**dispatch, "user_id": str(user_ref[0]), "user_domain": user_ref[1]}
    elif event_type == "guild.message.bulk_delete":
        dispatch_type, dispatch = await _apply_message_bulk_delete_mutation(
            session,
            locked,
            content,
            context,
        )
    elif event_type in {"guild.message.update", "guild.message.delete"}:
        message_ref = _event_ref(content.get("message"), "message")
        message = await session.get(Message, message_ref)
        if message is not None:
            channel = await session.get(Channel, (message.channel_id, message.channel_domain))
            if channel is None or (channel.guild_id, channel.guild_domain) != (
                locked.id,
                locked.origin_domain,
            ):
                raise ValueError("message mutation references the wrong guild")
            if event_type.endswith("update"):
                raw_message = content.get("message")
                if not isinstance(raw_message, dict):
                    raise ValueError("message update is invalid")
                thread_attached = content.get("thread_attached", False)
                thread_detached = content.get("thread_detached", False)
                if (
                    not isinstance(thread_attached, bool)
                    or not isinstance(thread_detached, bool)
                    or thread_attached
                    and thread_detached
                ):
                    raise ValueError("message thread attachment marker is invalid")
                if thread_attached:
                    incoming_flags = _bounded_event_int(raw_message.get("flags"), "message flags")
                    attached_thread = await session.get(Channel, message_ref)
                    if (
                        incoming_flags is None
                        or not incoming_flags & (1 << 5)
                        or incoming_flags & ~(message.flags | (1 << 5))
                        or attached_thread is None
                        or attached_thread.type not in {10, 11, 12}
                        or (attached_thread.parent_id, attached_thread.parent_domain)
                        != (message.channel_id, message.channel_domain)
                    ):
                        raise ValueError("message thread attachment is invalid")
                    message.flags = incoming_flags
                elif thread_detached:
                    incoming_flags = _bounded_event_int(raw_message.get("flags"), "message flags")
                    if (
                        incoming_flags is None
                        or incoming_flags & (1 << 5)
                        or not message.flags & (1 << 5)
                        or incoming_flags & ~message.flags
                    ):
                        raise ValueError("message thread detachment is invalid")
                    message.flags = incoming_flags
                elif content.get("announcement_published", False):
                    incoming_flags = _bounded_event_int(raw_message.get("flags"), "message flags")
                    raw_published_at = raw_message.get("published_at")
                    try:
                        published_at = datetime.fromisoformat(str(raw_published_at))
                    except ValueError:
                        raise ValueError("announcement publish timestamp is invalid") from None
                    if (
                        incoming_flags is None
                        or not incoming_flags & 1
                        or incoming_flags & ~int(message.flags | 1)
                        or published_at.tzinfo is None
                        or published_at < message.created_at
                    ):
                        raise ValueError("announcement publish flags are invalid")
                    message.flags = incoming_flags
                    message.published_at = published_at
                else:
                    value = raw_message.get("content")
                    announcement_copy_updated = content.get("announcement_copy_updated", False)
                    if not isinstance(announcement_copy_updated, bool):
                        raise ValueError("announcement copy update marker is invalid")
                    incoming_flags = _bounded_event_int(raw_message.get("flags"), "message flags")
                    if announcement_copy_updated and (
                        incoming_flags is None
                        or not int(message.flags or 0) & MESSAGE_FLAG_IS_CROSSPOST
                        or not incoming_flags & MESSAGE_FLAG_IS_CROSSPOST
                        or incoming_flags & MESSAGE_FLAG_HAS_SNAPSHOT
                        or (
                            incoming_flags & MESSAGE_FLAG_SOURCE_MESSAGE_DELETED
                            and value != "[Original Message Deleted]"
                        )
                    ):
                        raise ValueError("announcement copy update flags are invalid")
                    if raw_message.get("tts", False) is not bool(message.tts):
                        raise ValueError("message update changed its TTS marker")
                    incoming_flags = _bounded_event_int(
                        raw_message.get("flags"),
                        "message flags",
                    )
                    if incoming_flags is None:
                        raise ValueError("message update flags are invalid")
                    editable_flags = MESSAGE_FLAG_SUPPRESS_EMBEDS | MESSAGE_FLAG_IS_COMPONENTS_V2
                    if not announcement_copy_updated and (
                        incoming_flags & ~editable_flags
                        != int(message.flags or 0) & ~editable_flags
                    ):
                        raise ValueError("message update changed immutable flags")
                    e2ee = validate_e2ee_envelope(raw_message.get("e2ee"))
                    if value is not None and (
                        not isinstance(value, str) or not 1 <= len(value) <= 4000
                    ):
                        raise ValueError("message update content is invalid")
                    rich = _validated_message_rich_projection(
                        raw_message,
                        message_id=message.id,
                        message_origin=message.origin_domain,
                        message_created_at=message.created_at,
                        e2ee=e2ee,
                        message_type=message.message_type,
                        flags=int(raw_message.get("flags", message.flags)),
                    )
                    (
                        incoming_mention_pairs,
                        incoming_mention_refs,
                        incoming_role_refs,
                        incoming_everyone,
                    ) = _validated_guild_message_mentions(raw_message, locked)
                    for user_id, user_domain in incoming_mention_pairs:
                        if (
                            await session.get(
                                GuildMember,
                                (
                                    locked.id,
                                    locked.origin_domain,
                                    user_id,
                                    user_domain,
                                ),
                            )
                            is None
                        ):
                            raise ValueError("guild message mentions a user outside the guild")
                    if (
                        value is None
                        and e2ee is None
                        and not raw_message.get("attachments")
                        and not cast(list[dict[str, Any]], rich["embeds"])
                        and not cast(list[dict[str, Any]], rich["components"])
                        and not cast(list[dict[str, Any]], rich["sticker_items"])
                        and rich["poll"] is None
                        and rich["forwarded_ref"] is None
                    ):
                        raise ValueError("message update contains no body")
                    validate_message_encryption_policy(
                        channel.encryption_mode,
                        content=value,
                        e2ee=e2ee,
                        attachment_count=len(raw_message.get("attachments", [])),
                        policy_generation=channel.encryption_policy_generation,
                        policy_epoch=channel.encryption_epoch,
                        policy_group_id=channel.encryption_group_id,
                    )
                    validate_e2ee_message_projection(
                        e2ee,
                        message_id=message_ref[0],
                        message_domain=message_ref[1],
                        edited=True,
                    )
                    validate_e2ee_message_revision(e2ee, message.e2ee)
                    edited_at = _event_datetime(raw_message.get("edited_at"), "message edit")
                    if edited_at is None:
                        raise ValueError("message edit timestamp is invalid")
                    if message.deleted_at is not None or (
                        message.edited_at is not None and edited_at < message.edited_at
                    ):
                        raise ValueError("message edit regressed authoritative state")
                    message.content = value
                    message.e2ee = e2ee
                    message.mention_user_refs = incoming_mention_refs
                    message.mention_role_refs = incoming_role_refs
                    message.mention_everyone = incoming_everyone
                    projection = await session.get(
                        MessageProjection,
                        (message.id, message.origin_domain),
                        with_for_update=True,
                    )
                    if projection is None:
                        session.add(
                            MessageProjection(
                                message_id=message.id,
                                message_domain=message.origin_domain,
                                channel_id=message.channel_id,
                                channel_domain=message.channel_domain,
                                mention_user_refs=incoming_mention_refs,
                            )
                        )
                    else:
                        projection.mention_user_refs = incoming_mention_refs
                    message.embeds = cast(list[dict[str, Any]], rich["embeds"])
                    message.components = cast(list[dict[str, Any]], rich["components"])
                    if not announcement_copy_updated and list(message.sticker_items or []) != cast(
                        list[dict[str, Any]], rich["sticker_items"]
                    ):
                        raise ValueError("message update changed immutable sticker items")
                    await _apply_message_application_projection(session, message, rich, e2ee)
                    forwarded_ref = cast(tuple[int, str] | None, rich["forwarded_ref"])
                    if (
                        message.forwarded_message_id,
                        message.forwarded_message_domain,
                    ) != (forwarded_ref if forwarded_ref is not None else (None, None)):
                        raise ValueError("message update changed its live forward")
                    forwarded_channel_ref = cast(
                        tuple[int, str] | None,
                        rich["forwarded_channel_ref"],
                    )
                    if (
                        message.forwarded_channel_id,
                        message.forwarded_channel_domain,
                    ) != (
                        forwarded_channel_ref if forwarded_channel_ref is not None else (None, None)
                    ) or message.forward_snapshot != rich["forward_snapshot"]:
                        raise ValueError("message update changed its immutable forward snapshot")
                    poll_projection = cast(
                        tuple[
                            dict[str, object],
                            list[tuple[int, str | None, dict[str, object] | None]],
                            bool,
                            int,
                            datetime,
                        ]
                        | None,
                        rich["poll"],
                    )
                    if not await _stored_poll_matches_projection(
                        session,
                        message,
                        poll_projection,
                    ):
                        raise ValueError("message update changed its poll definition")
                    author = await session.get(
                        User,
                        (message.author_id, message.author_domain),
                    )
                    if author is None:
                        raise ValueError("message update author is unavailable")
                    replicated_attachments = await replicate_message_attachments(
                        session,
                        settings,
                        message,
                        author,
                        raw_message.get("attachments", []),
                        allowed_attachment_origins=(
                            {author.origin_domain, message.origin_domain}
                            if int(message.flags or 0) & MESSAGE_FLAG_IS_CROSSPOST
                            else {author.origin_domain}
                        ),
                    )
                    incoming_attachment_refs = {
                        (item.id, item.origin_domain) for item in replicated_attachments
                    }
                    stored_attachments = list(
                        await session.scalars(
                            select(Attachment).where(
                                Attachment.message_id == message.id,
                                Attachment.message_domain == message.origin_domain,
                                Attachment.deleted_at.is_(None),
                            )
                        )
                    )
                    for stored_attachment in stored_attachments:
                        if (
                            stored_attachment.id,
                            stored_attachment.origin_domain,
                        ) not in incoming_attachment_refs:
                            stored_attachment.deleted_at = edited_at
                    message.encryption_policy_generation = channel.encryption_policy_generation
                    message.encryption_epoch = channel.encryption_epoch
                    message.flags = incoming_flags
                    message.edited_at = edited_at
            else:
                deleted_at = _event_datetime(content.get("deleted_at"), "message deletion")
                if deleted_at is None:
                    raise ValueError("message deletion timestamp is invalid")
                was_counted_thread_reply = (
                    message.deleted_at is None
                    and channel.type in {10, 11, 12}
                    and (channel.starter_message_id, channel.starter_message_domain)
                    != (message.id, message.origin_domain)
                )
                message.content = None
                message.e2ee = None
                message.deleted_at = deleted_at
                if was_counted_thread_reply:
                    channel.message_count = max(0, int(channel.message_count or 0) - 1)
                if (
                    channel.type in {10, 11, 12}
                    and (
                        channel.last_message_id,
                        channel.last_message_domain,
                    )
                    == message_ref
                ):
                    await session.flush()
                    await refresh_replicated_thread_cursor(session, channel)
        dispatch_type = "MESSAGE_UPDATE" if event_type.endswith("update") else "MESSAGE_DELETE"
        dispatch = {
            "id": str(message_ref[0]),
            "origin_domain": message_ref[1],
            "channel_id": str(context.get("channel_id", "")),
            "channel_domain": str(context.get("channel_domain", "")),
        }
        if event_type.endswith("update") and isinstance(content.get("message"), dict):
            dispatch.update(content["message"])
    elif event_type == "guild.message.purge":
        author_ref = _event_ref(content.get("author"), "purged author")
        cutoff = _event_datetime(content.get("created_after"), "message purge cutoff")
        deleted_at = _event_datetime(content.get("deleted_at"), "message purge")
        if cutoff is None or deleted_at is None:
            raise ValueError("message purge timestamps are invalid")
        channel_ids = select(Channel.id).where(
            Channel.guild_id == locked.id, Channel.guild_domain == locked.origin_domain
        )
        affected_threads = list(
            await session.scalars(
                select(Channel)
                .join(
                    Message,
                    (Message.channel_id == Channel.id)
                    & (Message.channel_domain == Channel.origin_domain),
                )
                .where(
                    Channel.guild_id == locked.id,
                    Channel.guild_domain == locked.origin_domain,
                    Channel.type.in_({10, 11, 12}),
                    Message.author_id == author_ref[0],
                    Message.author_domain == author_ref[1],
                    Message.created_at >= cutoff,
                    Message.deleted_at.is_(None),
                )
                .distinct()
            )
        )
        purged_thread_counts = list(
            await session.execute(
                select(Channel, func.count(Message.id))
                .join(
                    Message,
                    (Message.channel_id == Channel.id)
                    & (Message.channel_domain == Channel.origin_domain),
                )
                .where(
                    Channel.guild_id == locked.id,
                    Channel.guild_domain == locked.origin_domain,
                    Channel.type.in_({10, 11, 12}),
                    Message.author_id == author_ref[0],
                    Message.author_domain == author_ref[1],
                    Message.created_at >= cutoff,
                    Message.deleted_at.is_(None),
                    ~(
                        (Message.id == Channel.starter_message_id)
                        & (Message.origin_domain == Channel.starter_message_domain)
                    ),
                )
                .group_by(Channel.id, Channel.origin_domain)
            )
        )
        await session.execute(
            update(Message)
            .where(
                Message.channel_id.in_(channel_ids),
                Message.channel_domain == locked.origin_domain,
                Message.author_id == author_ref[0],
                Message.author_domain == author_ref[1],
                Message.created_at >= cutoff,
                Message.deleted_at.is_(None),
            )
            .values(content=None, e2ee=None, deleted_at=deleted_at)
        )
        await session.flush()
        purged_count_by_ref = {
            (thread.id, thread.origin_domain): int(purged_count)
            for thread, purged_count in purged_thread_counts
        }
        for thread in affected_threads:
            thread.message_count = max(
                0,
                int(thread.message_count or 0)
                - purged_count_by_ref.get((thread.id, thread.origin_domain), 0),
            )
            await refresh_replicated_thread_cursor(session, thread)
        # The authority does not publish a singular message-delete dispatch for
        # a ban purge. Replicas must mirror that behavior instead of inventing
        # a malformed MESSAGE_DELETE without a message or channel identity.
    elif event_type in {"guild.reaction.add", "guild.reaction.remove"}:
        dispatch_type, dispatch = await _apply_reaction_mutation(
            session,
            locked,
            event_type,
            content,
            context,
        )
    elif event_type == "guild.reaction.clear":
        dispatch_type, dispatch = await _apply_reaction_clear_mutation(
            session,
            locked,
            content,
            context,
        )
    elif event_type in {"guild.poll.vote.add", "guild.poll.vote.remove"}:
        dispatch_type, dispatch = await _apply_poll_vote_mutation(
            session,
            locked,
            event_type,
            content,
            context,
        )
    elif event_type == "guild.poll.finalize":
        poll_finalize_dispatch = await _apply_poll_finalize_mutation(
            session,
            locked,
            content,
            context,
            actor,
        )
        if poll_finalize_dispatch is None:
            suppress_dispatch = True
        else:
            dispatch_type, dispatch = poll_finalize_dispatch
    elif event_type in _PROJECTED_GUILD_FEATURE_EVENT_TYPES:
        dispatch_type, dispatch = await _apply_projected_guild_feature_mutation(
            session,
            locked,
            event_type,
            content,
            actor_ref,
        )
    elif event_type in {"guild.pin.add", "guild.pin.remove"}:
        dispatch_type, dispatch = await _apply_pin_mutation(
            session,
            locked,
            event_type,
            content,
            event,
            actor_ref,
        )

    raw_permission_generation = context.get("permission_generation")
    if raw_permission_generation is not None:
        permission_generation = database_snowflake(
            raw_permission_generation, "permission generation"
        )
        if permission_generation < locked.permission_generation:
            raise ValueError("permission generation regressed")
        locked.permission_generation = permission_generation
    _advance_snapshot_generation(locked, event, event_type=event_type)
    locked.last_event_seq = seq
    locked.next_event_seq = seq + 1
    locked.sync_status = "ready"
    if event_type in HISTORY_ACCESS_MUTATION_EVENT_TYPES:
        from app.federation.history import purge_ineligible_federated_history

        await purge_ineligible_federated_history(session, settings, locked)
    pause_e2ee = event_type in GUILD_E2EE_ACCESS_MUTATION_EVENTS
    if pause_e2ee and event_type in {
        "guild.member.remove",
        "guild.member.role.add",
        "guild.member.role.remove",
    }:
        raw_user = content.get("user")
        if isinstance(raw_user, dict):
            try:
                changed_user = await session.get(
                    User,
                    (
                        database_snowflake(raw_user.get("id"), "member user id"),
                        normalize_domain(str(raw_user.get("origin_domain", ""))),
                    ),
                )
            except ValueError:
                changed_user = None
            if changed_user is not None and changed_user.account_type == "bot":
                pause_e2ee = False
    if pause_e2ee:
        paused = await pause_guild_e2ee_for_membership_change(session, locked)
        if e2ee_policy_channels is not None:
            known = {(item.id, item.origin_domain) for item in e2ee_policy_channels}
            e2ee_policy_channels.extend(
                item for item in paused if (item.id, item.origin_domain) not in known
            )
    if versioned_resources:
        # History/E2EE reconciliation may flush or touch the same row. Restore
        # the signed authority token as the final write in this transaction.
        for resource, version in versioned_resources:
            resource.updated_at = version
    return (
        None
        if suppress_dispatch
        or event_type
        in {
            "guild.forum.cursor.update",
            "guild.message.purge",
            "guild.tracker.board.invalidate",
        }
        else (dispatch_type, dispatch)
    )


async def lock_proxy_nonce(
    session: AsyncSession, guild: Guild, actor: User, channel: Channel, client_nonce: str
) -> None:
    lock_key = (
        f"kaede-proxy:{guild.origin_domain}:{guild.id}:{channel.id}:"
        f"{actor.origin_domain}:{actor.id}:{client_nonce}"
    )
    await session.scalar(select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0))))


async def guild_event_for_message(
    session: AsyncSession, guild: Guild, message: Message
) -> GuildEvent | None:
    result: GuildEvent | None = await session.scalar(
        select(GuildEvent)
        .where(
            GuildEvent.guild_id == guild.id,
            GuildEvent.guild_domain == guild.origin_domain,
            GuildEvent.envelope["content"]["message"]["id"].as_string() == str(message.id),
        )
        .order_by(GuildEvent.seq)
        .limit(1)
    )
    return result


def proxy_event(
    event_type: str, seq: int, actor: User, content: dict[str, object]
) -> dict[str, object]:
    return {
        "event_id": new_guild_event_id(),
        "type": event_type,
        "seq": str(seq),
        "ts": int(datetime.now(UTC).timestamp() * 1000),
        "actor": {"id": str(actor.id), "domain": actor.origin_domain},
        "content": content,
    }


async def fetch_guild_snapshot(
    session: AsyncSession,
    settings: Settings,
    origin: str,
    guild_id: int,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    stop_at = deadline or (time.monotonic() + MAX_GUILD_SYNC_SECONDS)
    combined: dict[str, Any] | None = None
    member_cursor: tuple[str, int] | None = None
    member_snapshot_at: str | None = None
    snapshot_seq: str | None = None
    snapshot_generation: str | None = None
    total_bytes = 0
    for _page in range(MAX_SNAPSHOT_PAGES):
        remaining_time = stop_at - time.monotonic()
        remaining_bytes = MAX_SNAPSHOT_BYTES - total_bytes
        if remaining_time <= 0:
            raise RuntimeError("guild snapshot exceeded its duration limit")
        if remaining_bytes <= 0:
            raise RuntimeError("guild snapshot exceeded its aggregate byte limit")
        query: dict[str, str] = {}
        if member_cursor is not None:
            if member_snapshot_at is None or snapshot_seq is None:
                raise RuntimeError("guild snapshot cursor state is incomplete")
            query = {
                "member_after_domain": member_cursor[0],
                "member_after_id": str(member_cursor[1]),
                "member_snapshot_at": member_snapshot_at,
                "member_snapshot_seq": snapshot_seq,
            }
            if snapshot_generation is not None:
                query["member_snapshot_generation"] = snapshot_generation
        response = await signed_request(
            session,
            settings,
            "GET",
            origin,
            f"/_kaede/v1/guilds/{guild_id}/snapshot",
            query=query,
            request_timeout=min(10.0, remaining_time),
            max_response_bytes=remaining_bytes,
        )
        if time.monotonic() >= stop_at:
            raise RuntimeError("guild snapshot exceeded its duration limit")
        if response.status_code != 200:
            raise RuntimeError("guild full resynchronization failed")
        payload = decode_federation_response_json(response)
        if not isinstance(payload, dict):
            raise RuntimeError("guild snapshot payload is invalid")
        total_bytes += len(response.content)
        if total_bytes > MAX_SNAPSHOT_BYTES:
            raise RuntimeError("guild snapshot exceeds its aggregate byte limit")
        members = payload.get("members")
        member_roles = payload.get("member_roles")
        if not isinstance(members, list) or not isinstance(member_roles, list):
            raise RuntimeError("guild snapshot member page is invalid")
        if len(members) > 1_000 or len(member_roles) > 100_000:
            raise RuntimeError("guild snapshot member page exceeds its protocol bound")
        page_snapshot_at = payload.get("member_snapshot_at")
        if not isinstance(page_snapshot_at, str):
            raise RuntimeError("guild snapshot is missing its membership watermark")
        raw_snapshot_seq = payload.get("snapshot_seq")
        if not isinstance(raw_snapshot_seq, str):
            raise RuntimeError("guild snapshot sequence is invalid")
        try:
            parsed_snapshot_at = datetime.fromisoformat(page_snapshot_at)
            database_snowflake(raw_snapshot_seq, "snapshot sequence")
            raw_snapshot_generation = payload.get("snapshot_generation")
            if (
                raw_snapshot_generation is not None
                and database_snowflake(raw_snapshot_generation, "snapshot generation") < 1
            ):
                raise ValueError("snapshot generation must be positive")
        except ValueError:
            raise RuntimeError("guild snapshot watermark or sequence is invalid") from None
        if parsed_snapshot_at.tzinfo is None:
            raise RuntimeError("guild snapshot membership watermark lacks a timezone")
        if combined is None:
            combined = payload
            combined["members"] = list(members)
            combined["member_roles"] = list(member_roles)
            member_snapshot_at = page_snapshot_at
            snapshot_seq = raw_snapshot_seq
            snapshot_generation = (
                raw_snapshot_generation if isinstance(raw_snapshot_generation, str) else None
            )
        else:
            if payload.get("snapshot_seq") != combined.get("snapshot_seq"):
                raise RuntimeError("guild changed while its snapshot was paged")
            if page_snapshot_at != member_snapshot_at:
                raise RuntimeError(
                    "guild membership watermark changed while its snapshot was paged"
                )
            if payload.get("snapshot_generation") != snapshot_generation:
                raise RuntimeError(
                    "guild structural generation changed while its snapshot was paged"
                )
            for field in ("guild", "roles", "channels", "overwrites", "emojis", "stickers"):
                if payload.get(field) != combined.get(field):
                    raise RuntimeError("guild structure changed while its snapshot was paged")
            combined["members"].extend(members)
            combined["member_roles"].extend(member_roles)
        if (
            len(combined["members"]) > MAX_SNAPSHOT_MEMBERS
            or len(combined["member_roles"]) > MAX_SNAPSHOT_MEMBER_ROLES
        ):
            raise RuntimeError("guild snapshot exceeds its aggregate record limit")
        next_cursor = payload.get("next_member_cursor")
        if next_cursor is None:
            combined["next_member_cursor"] = None
            return combined
        if not isinstance(next_cursor, dict):
            raise RuntimeError("guild snapshot returned an invalid member cursor")
        try:
            next_ref = (
                normalize_domain(str(next_cursor.get("user_domain", ""))),
                database_snowflake(next_cursor.get("user_id"), "member cursor id"),
            )
            last_profile = RemoteUserProfile.model_validate(members[-1]["user"])
        except (IndexError, TypeError, ValueError):
            raise RuntimeError("guild snapshot returned an invalid member cursor") from None
        if next_ref != (last_profile.origin_domain, int(last_profile.id)):
            raise RuntimeError("guild snapshot member cursor does not match its page")
        if member_cursor is not None and next_ref <= member_cursor:
            raise RuntimeError("guild snapshot member cursor did not advance")
        member_cursor = next_ref
    raise RuntimeError("guild snapshot exceeded the member page limit")


async def synchronize_guild(
    session: AsyncSession, settings: Settings, guild: Guild
) -> list[Message]:
    """Bring a replica current within one bounded background work quantum.

    A signed but semantically invalid retained event cannot be skipped safely.
    Quarantine the incremental stream and recover from a fresh signed snapshot
    instead of retrying the same poison event forever.
    """

    deadline = time.monotonic() + MAX_GUILD_SYNC_SECONDS
    guild_id = guild.id
    guild_origin = guild.origin_domain
    applied: list[Message] = []

    async def pause_for_quota(exc: FederationReplicaQuotaExceeded) -> list[Message]:
        await session.rollback()
        await mark_replica_quota_paused(
            session,
            settings,
            guild_id,
            guild_origin,
            exc,
        )
        await session.commit()
        return []

    async def pause_for_identity_capacity(
        exc: FederationIdentityQuotaExceeded | FederationInstanceQuotaExceeded,
    ) -> list[Message]:
        await session.rollback()
        await mark_replica_capacity_paused(
            session,
            settings,
            guild_id,
            guild_origin,
            error_code=exc.code,
            internal_error=str(exc),
        )
        await session.commit()
        return []

    async def recover_from_snapshot() -> list[Message]:
        snapshot = await fetch_guild_snapshot(
            session,
            settings,
            guild_origin,
            guild_id,
            deadline=deadline,
        )
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                await apply_guild_snapshot(
                    session,
                    settings,
                    snapshot,
                    expected_origin=guild_origin,
                    expected_guild_id=guild_id,
                )
        except FederationReplicaQuotaExceeded as exc:
            return await pause_for_quota(exc)
        except (FederationIdentityQuotaExceeded, FederationInstanceQuotaExceeded) as exc:
            return await pause_for_identity_capacity(exc)
        except TimeoutError:
            await session.rollback()
            timed_out = await session.get(Guild, (guild_id, guild_origin))
            if timed_out is not None:
                timed_out.sync_status = "failed"
                timed_out.unavailable = True
                timed_out.sync_error_code = "KAED_FED_SNAPSHOT_WORK_LIMIT"
                timed_out.sync_error = "snapshot application exceeded its work deadline"
                await session.commit()
            return []
        return []

    async def quarantine_and_recover() -> list[Message]:
        # An event applier may have changed several rows before rejecting the
        # event. Roll the page back completely, persist the quarantine marker,
        # then start the independently verifiable snapshot transaction.
        await session.rollback()
        quarantined = await session.get(Guild, (guild_id, guild_origin))
        if quarantined is None:
            raise RuntimeError("replicated guild disappeared during recovery")
        quarantined.sync_status = "failed"
        quarantined.unavailable = True
        quarantined.sync_error_code = None
        quarantined.sync_error = None
        await session.commit()
        try:
            return await recover_from_snapshot()
        except Exception:
            await session.rollback()
            quarantined = await session.get(Guild, (guild_id, guild_origin))
            if quarantined is not None:
                quarantined.sync_status = "failed"
                quarantined.unavailable = True
                quarantined.sync_error_code = None
                quarantined.sync_error = None
                await session.commit()
            raise

    if guild.sync_status == "quota_paused":
        return []
    if guild.sync_status == "failed":
        return await recover_from_snapshot()

    total_events = 0
    total_bytes = 0
    advertised_latest = guild.last_event_seq
    for _page in range(MAX_GUILD_SYNC_PAGES):
        remaining_time = deadline - time.monotonic()
        remaining_bytes = MAX_GUILD_SYNC_BYTES - total_bytes
        if remaining_time <= 0:
            return await quarantine_and_recover()
        if remaining_bytes <= 0:
            return await quarantine_and_recover()
        page_start_seq = guild.last_event_seq
        response = await signed_request(
            session,
            settings,
            "GET",
            guild_origin,
            f"/_kaede/v1/guilds/{guild_id}/events",
            query={"after_seq": str(guild.last_event_seq)},
            request_timeout=min(10.0, remaining_time),
            max_response_bytes=remaining_bytes,
        )
        if time.monotonic() >= deadline:
            return await quarantine_and_recover()
        total_bytes += len(response.content)
        if total_bytes > MAX_GUILD_SYNC_BYTES:
            return await quarantine_and_recover()
        if response.status_code == 410:
            return await quarantine_and_recover()
        if response.status_code != 200:
            raise RuntimeError("guild gap fill failed")
        # A remote response is not itself a serialization point. Re-enter the
        # retained terminal-room fence after every network page and hold it
        # through the caller's commit so a stale gap response cannot recreate a
        # guild after its exact authority deletion was applied locally.
        await lock_terminal_room(session, "guild", guild_id, guild_origin)
        terminal_receipt = await session.get(
            TerminalRoomDeletion,
            ("guild", guild_id, guild_origin, settings.domain),
        )
        live_guild = await session.get(
            Guild,
            (guild_id, guild_origin),
            populate_existing=True,
        )
        # ``unavailable`` is also used while an otherwise valid replica is
        # stale/quota-paused. A successful gap fill is precisely what clears
        # that state; only the retained terminal receipt is a deletion fence.
        if terminal_receipt is not None or live_guild is None:
            return []
        guild = live_guild
        requires_snapshot = False
        try:
            payload = decode_federation_response_json(response)
            if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
                raise ValueError("guild gap fill returned an invalid payload")
            events = payload["events"]
            if len(events) > 1_000:
                raise ValueError("guild gap fill page exceeds its protocol event limit")
            total_events += len(events)
            if total_events > MAX_GUILD_SYNC_EVENTS:
                raise ValueError("guild synchronization exceeded its aggregate event limit")
            latest_seq = database_snowflake(payload.get("latest_seq"), "latest guild sequence")
            if latest_seq < advertised_latest or latest_seq < page_start_seq:
                raise ValueError("guild gap fill latest sequence regressed")
            advertised_latest = latest_seq
            validated_events: list[dict[str, Any]] = []
            page_attachment_refs: set[tuple[int, str]] = set()
            for raw_event in events:
                envelope = await validated_event_envelope(
                    session,
                    settings,
                    guild_origin,
                    raw_event,
                    allow_authority_attested_actor=True,
                )
                event = envelope.model_dump(mode="json")
                validated_events.append(event)
                page_attachment_refs.update(message_attachment_refs(event))
            for attachment_id, attachment_domain in sorted(
                page_attachment_refs, key=lambda ref: (ref[1], ref[0])
            ):
                await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
            for event in validated_events:
                if event.get("type") in {"guild.message.create", "guild.message.committed"}:
                    message = await apply_guild_message_event(session, settings, guild, event)
                    if message is not None:
                        applied.append(message)
                elif event.get("type") == "guild.member.add":
                    await apply_guild_member_event(session, settings, guild, event)
                elif event.get("type") == "guild.event.redacted":
                    if guild_event_requires_snapshot(event):
                        requires_snapshot = True
                        break
                    await apply_guild_redaction_event(session, guild, event)
                elif event.get("type") in GUILD_MUTATION_EVENT_TYPES:
                    if guild_event_requires_snapshot(event):
                        requires_snapshot = True
                        break
                    await apply_guild_mutation_event(session, settings, guild, event)
                else:
                    raise ValueError("guild gap fill returned an unsupported event type")
        except (
            HTTPException,
            FederationNetworkError,
            KeyError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            return await quarantine_and_recover()
        if requires_snapshot:
            return await quarantine_and_recover()
        if guild.last_event_seq >= latest_seq:
            guild.sync_status = "ready"
            guild.unavailable = False
            try:
                await admit_replica_storage(session, settings, guild)
            except FederationReplicaQuotaExceeded as exc:
                return await pause_for_quota(exc)
            return applied
        if not events or guild.last_event_seq <= page_start_seq:
            return await quarantine_and_recover()
    return await quarantine_and_recover()


def validate_guild_snapshot(
    snapshot: dict[str, Any],
    *,
    expected_origin: str,
    expected_guild_id: int,
    required_member: tuple[int, str] | None = None,
) -> None:
    raw_guild = snapshot.get("guild")
    if not isinstance(raw_guild, dict):
        raise ValueError("guild snapshot is missing guild metadata")
    guild_id = database_snowflake(raw_guild.get("id"), "guild id")
    origin = str(raw_guild.get("origin_domain"))
    if (guild_id, origin) != (expected_guild_id, expected_origin):
        raise ValueError("guild snapshot identity does not match the requested guild")
    _event_resource_version(raw_guild, "guild")
    guild_name = raw_guild.get("name")
    if not isinstance(guild_name, str) or not 2 <= len(guild_name) <= 100:
        raise ValueError("guild snapshot name is invalid")
    for field, maximum in (("description", 500), ("icon_hash", 128), ("banner_hash", 128)):
        value = raw_guild.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > maximum):
            raise ValueError(f"guild snapshot {field} is invalid")
    database_snowflake(raw_guild.get("permission_generation"), "permission generation")
    if database_snowflake(snapshot.get("snapshot_generation", "1"), "snapshot generation") < 1:
        raise ValueError("guild snapshot generation must be positive")
    history_policy = raw_guild.get("federated_history_policy", "disabled")
    if history_policy not in {"disabled", "full_retained"}:
        raise ValueError("guild snapshot history policy is invalid")
    database_snowflake(raw_guild.get("history_policy_generation", "1"), "history policy generation")
    snapshot_seq = database_snowflake(snapshot.get("snapshot_seq"), "snapshot sequence")
    del snapshot_seq
    roles = snapshot.get("roles")
    channels = snapshot.get("channels")
    members = snapshot.get("members")
    member_roles = snapshot.get("member_roles")
    overwrites = snapshot.get("overwrites")
    emojis = snapshot.get("emojis", [])
    stickers = snapshot.get("stickers", [])
    thread_members = snapshot.get("thread_members", [])
    if (
        not isinstance(roles, list)
        or not isinstance(channels, list)
        or not isinstance(members, list)
        or not isinstance(member_roles, list)
        or not isinstance(overwrites, list)
        or not isinstance(emojis, list)
        or not isinstance(stickers, list)
        or not isinstance(thread_members, list)
    ):
        raise ValueError("guild snapshot collections are invalid")
    if (
        len(roles) > 10_000
        or len(channels) > 10_000
        or len(members) > MAX_SNAPSHOT_MEMBERS
        or len(member_roles) > MAX_SNAPSHOT_MEMBER_ROLES
        or len(overwrites) > MAX_SNAPSHOT_OVERWRITES
        or len(emojis) > 1000
        or len(stickers) > 1000
        or len(thread_members) > MAX_SNAPSHOT_THREAD_MEMBERS
    ):
        raise ValueError("guild snapshot collection exceeds its protocol bound")
    role_refs: set[tuple[int, str]] = set()
    for raw in roles:
        if not isinstance(raw, dict):
            raise ValueError("guild snapshot role is invalid")
        ref = (database_snowflake(raw.get("id"), "role id"), str(raw.get("origin_domain")))
        if ref[1] != origin or ref in role_refs:
            raise ValueError("guild snapshot contains an invalid role identity")
        name = raw.get("name")
        color = raw.get("color")
        position = raw.get("position")
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise ValueError("guild snapshot role name is invalid")
        if isinstance(color, bool) or not isinstance(color, int) or not 0 <= color <= 0xFFFFFF:
            raise ValueError("guild snapshot role color is invalid")
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise ValueError("guild snapshot role position is invalid")
        permissions = database_snowflake(raw.get("permissions"), "role permissions")
        if permissions & ~ALL_PERMISSIONS:
            raise ValueError("guild snapshot role contains unknown permissions")
        icon_hash = raw.get("icon_hash")
        if icon_hash is not None and (
            not isinstance(icon_hash, str) or not valid_content_digest(icon_hash)
        ):
            raise ValueError("guild snapshot role icon hash is invalid")
        if not isinstance(raw.get("hoist"), bool) or not isinstance(raw.get("mentionable"), bool):
            raise ValueError("guild snapshot role flags are invalid")
        _event_resource_version(raw, "role")
        role_refs.add(ref)
    channel_refs: set[tuple[int, str]] = set()
    channel_types: dict[tuple[int, str], int] = {}
    channels_by_ref: dict[tuple[int, str], dict[str, Any]] = {}
    for raw in channels:
        if not isinstance(raw, dict):
            raise ValueError("guild snapshot channel is invalid")
        ref = (
            database_snowflake(raw.get("id"), "channel id"),
            str(raw.get("origin_domain")),
        )
        if ref[1] != origin or ref in channel_refs:
            raise ValueError("guild snapshot contains an invalid channel identity")
        channel_type = raw.get("type")
        name = raw.get("name")
        topic = raw.get("topic")
        position = raw.get("position")
        slowmode = raw.get("rate_limit_per_user")
        history_policy = raw.get("federated_history_policy", "inherit")
        raw_encryption_policy = raw.get("encryption_policy")
        if raw_encryption_policy is None:
            legacy_mode = raw.get("encryption_mode", "plaintext")
            raw_encryption_policy = {
                "mode": legacy_mode,
                "state": "legacy" if legacy_mode == "e2ee" else "plaintext",
                "generation": "0",
            }
        validate_channel_encryption_policy(raw_encryption_policy)
        parent_id = raw.get("parent_id")
        permissions_synced = raw.get("permissions_synced", parent_id is not None)
        if isinstance(channel_type, bool) or channel_type not in GUILD_CHANNEL_TYPES:
            raise ValueError("guild snapshot channel type is invalid")
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise ValueError("guild snapshot channel name is invalid")
        topic_limit = 4096 if channel_type == 15 else 1024
        if topic is not None and (not isinstance(topic, str) or len(topic) > topic_limit):
            raise ValueError("guild snapshot channel topic is invalid")
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise ValueError("guild snapshot channel position is invalid")
        if (
            isinstance(slowmode, bool)
            or not isinstance(slowmode, int)
            or not 0 <= slowmode <= 21_600
        ):
            raise ValueError("guild snapshot channel slowmode is invalid")
        if history_policy not in {"inherit", "disabled", "full_retained"}:
            raise ValueError("guild snapshot channel history policy is invalid")
        if (
            not isinstance(permissions_synced, bool)
            or permissions_synced
            and (parent_id is None or channel_type in {4, 10, 11, 12})
        ):
            raise ValueError("guild snapshot channel permission sync state is invalid")
        if channel_type in {10, 11, 12} and (parent_id is None or permissions_synced):
            raise ValueError("guild snapshot thread parent state is invalid")
        _validated_voice_channel_state(raw, int(channel_type))
        _validated_channel_extension_state(raw, int(channel_type), origin)
        _event_datetime(raw.get("created_at"), "channel creation", optional=True)
        _event_resource_version(raw, "channel")
        database_snowflake(raw.get("created_floor_id"), "channel history floor")
        channel_refs.add(ref)
        channel_types[ref] = int(channel_type)
        channels_by_ref[ref] = raw
    for raw in channels:
        parent_id = raw.get("parent_id")
        if parent_id is not None:
            parent_ref = (
                database_snowflake(parent_id, "parent channel id"),
                str(raw.get("parent_domain")),
            )
            if parent_ref not in channel_refs:
                raise ValueError("guild snapshot channel parent is outside the guild")
            expected_parent_types = expected_channel_parent_types(
                channel_types[(int(raw["id"]), str(raw["origin_domain"]))]
            )
            if channel_types[parent_ref] not in expected_parent_types:
                raise ValueError("guild snapshot channel parent type is invalid")
        if raw.get("last_thread_id") is not None:
            last_thread_ref = (
                database_snowflake(raw.get("last_thread_id"), "forum last thread id"),
                str(raw.get("last_thread_domain")),
            )
            current_ref = (int(raw["id"]), str(raw["origin_domain"]))
            if channel_types.get(current_ref) != 15 or channel_types.get(last_thread_ref) not in {
                10,
                11,
                12,
            }:
                raise ValueError("guild snapshot forum last thread is invalid")
            target = channels_by_ref.get(last_thread_ref)
            if (
                target is None
                or (
                    database_snowflake(target.get("parent_id"), "forum post parent id"),
                    str(target.get("parent_domain")),
                )
                != current_ref
            ):
                raise ValueError("guild snapshot forum last thread parent is invalid")
    member_refs: set[tuple[int, str]] = set()
    for raw in members:
        if not isinstance(raw, dict) or not isinstance(raw.get("user"), dict):
            raise ValueError("guild snapshot member is invalid")
        profile = RemoteUserProfile.model_validate(raw["user"])
        ref = (int(profile.id), profile.origin_domain)
        if ref in member_refs:
            raise ValueError("guild snapshot contains a duplicate member")
        nickname = raw.get("nickname")
        if nickname is not None and (not isinstance(nickname, str) or len(nickname) > 100):
            raise ValueError("guild snapshot member nickname is invalid")
        try:
            joined_at = datetime.fromisoformat(str(raw.get("joined_at")))
            timeout_until = (
                datetime.fromisoformat(str(raw["timeout_until"]))
                if raw.get("timeout_until") is not None
                else None
            )
        except ValueError:
            raise ValueError("guild snapshot member timestamp is invalid") from None
        if joined_at.tzinfo is None or (timeout_until is not None and timeout_until.tzinfo is None):
            raise ValueError("guild snapshot member timestamp lacks a timezone")
        timeout_indefinite = raw.get("timeout_indefinite", False)
        if not isinstance(timeout_indefinite, bool):
            raise ValueError("guild snapshot member indefinite timeout is invalid")
        if not isinstance(raw.get("temporary", False), bool):
            raise ValueError("guild snapshot member temporary flag is invalid")
        if timeout_indefinite and timeout_until is not None:
            raise ValueError("guild snapshot member timeout modes conflict")
        timeout_reason = raw.get("timeout_reason")
        if timeout_reason is not None and (
            not isinstance(timeout_reason, str) or len(timeout_reason) > 512
        ):
            raise ValueError("guild snapshot member timeout reason is invalid")
        if timeout_reason is not None and not (timeout_indefinite or timeout_until):
            raise ValueError(
                "guild snapshot member timeout reason exists without an active timeout"
            )
        voice_flags = raw.get("voice_flags")
        if voice_flags is not None and (
            isinstance(voice_flags, bool) or not isinstance(voice_flags, int) or voice_flags < 0
        ):
            raise ValueError("guild snapshot member voice flags are invalid")
        database_snowflake(raw.get("member_version"), "member version")
        member_refs.add(ref)
    owner_ref = (
        database_snowflake(raw_guild.get("owner_id"), "guild owner id"),
        str(raw_guild.get("owner_domain")),
    )
    if owner_ref not in member_refs:
        raise ValueError("guild snapshot does not contain its owner")
    if required_member is not None and required_member not in member_refs:
        raise ValueError("guild snapshot does not contain the joining local member")
    member_role_refs: set[tuple[tuple[int, str], tuple[int, str]]] = set()
    for raw in member_roles:
        if not isinstance(raw, dict):
            raise ValueError("guild snapshot member-role assignment is invalid")
        user_ref = (
            database_snowflake(raw.get("user_id"), "member id"),
            str(raw.get("user_domain")),
        )
        role_ref = (
            database_snowflake(raw.get("role_id"), "role id"),
            str(raw.get("role_domain")),
        )
        if user_ref not in member_refs or role_ref not in role_refs:
            raise ValueError("guild snapshot member-role reference is outside the guild")
        assignment = (user_ref, role_ref)
        if assignment in member_role_refs:
            raise ValueError("guild snapshot contains a duplicate member-role assignment")
        member_role_refs.add(assignment)
    overwrite_refs: set[tuple[tuple[int, str], str, tuple[int, str]]] = set()
    for raw in overwrites:
        if not isinstance(raw, dict):
            raise ValueError("guild snapshot overwrite is invalid")
        channel_ref = (
            database_snowflake(raw.get("channel_id"), "channel id"),
            str(raw.get("channel_domain")),
        )
        target_type = raw.get("target_type")
        target_ref = (
            database_snowflake(raw.get("target_id"), "overwrite target id"),
            str(raw.get("target_domain")),
        )
        if channel_ref not in channel_refs or target_type not in {"role", "member"}:
            raise ValueError("guild snapshot overwrite is outside the guild")
        if channel_types[channel_ref] in {10, 11, 12}:
            raise ValueError("guild snapshot thread contains a local overwrite")
        expected_targets = role_refs if target_type == "role" else member_refs
        if target_ref not in expected_targets:
            raise ValueError("guild snapshot overwrite target is outside the guild")
        allow = database_snowflake(raw.get("allow"), "overwrite allow mask")
        deny = database_snowflake(raw.get("deny"), "overwrite deny mask")
        if allow & deny or (allow | deny) & ~ALL_PERMISSIONS:
            raise ValueError("guild snapshot overwrite masks overlap")
        overwrite_ref = (channel_ref, str(target_type), target_ref)
        if overwrite_ref in overwrite_refs:
            raise ValueError("guild snapshot contains a duplicate overwrite")
        overwrite_refs.add(overwrite_ref)
    thread_member_refs: set[tuple[tuple[int, str], tuple[int, str]]] = set()
    for raw in thread_members:
        if not isinstance(raw, dict):
            raise ValueError("guild snapshot thread member is invalid")
        thread_ref = (
            database_snowflake(raw.get("id"), "thread id"),
            normalize_domain(str(raw.get("thread_domain", ""))),
        )
        user_ref = (
            database_snowflake(raw.get("user_id"), "thread member user id"),
            normalize_domain(str(raw.get("user_domain", ""))),
        )
        if (
            thread_ref not in channel_refs
            or channel_types[thread_ref] not in {10, 11, 12}
            or user_ref not in member_refs
            or (
                database_snowflake(raw.get("guild_id"), "thread member guild id"),
                normalize_domain(str(raw.get("guild_domain", ""))),
            )
            != (guild_id, origin)
        ):
            raise ValueError("guild snapshot thread member reference is invalid")
        if _event_datetime(raw.get("join_timestamp"), "thread member join") is None:
            raise ValueError("guild snapshot thread member join timestamp is invalid")
        _bounded_event_int(raw.get("flags", 0), "thread member flags")
        if raw.get("notification_level", "inherit") not in {
            "inherit",
            "all",
            "mentions",
            "none",
        }:
            raise ValueError("guild snapshot thread member notification level is invalid")
        thread_membership_ref = (thread_ref, user_ref)
        if thread_membership_ref in thread_member_refs:
            raise ValueError("guild snapshot contains a duplicate thread member")
        thread_member_refs.add(thread_membership_ref)
    emoji_refs: set[tuple[int, str]] = set()
    emoji_names: set[str] = set()
    for raw in emojis:
        if not isinstance(raw, dict):
            raise ValueError("guild snapshot emoji is invalid")
        ref = _event_ref(raw, "emoji")
        if ref[1] != origin or ref in emoji_refs:
            raise ValueError("guild snapshot emoji identity is invalid")
        if (
            database_snowflake(raw.get("guild_id"), "emoji guild id"),
            normalize_domain(str(raw.get("guild_domain", ""))),
        ) != (guild_id, origin):
            raise ValueError("guild snapshot emoji references the wrong guild")
        name = raw.get("name")
        media_hash = raw.get("media_hash")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9_]{2,32}", name) is None
            or name.casefold() in emoji_names
        ):
            raise ValueError("guild snapshot emoji name is invalid")
        if not isinstance(media_hash, str) or re.fullmatch(r"[0-9a-f]{64}", media_hash) is None:
            raise ValueError("guild snapshot emoji media identity is invalid")
        if not isinstance(raw.get("animated"), bool):
            raise ValueError("guild snapshot emoji animation flag is invalid")
        _event_resource_version(raw, "emoji")
        emoji_refs.add(ref)
        emoji_names.add(name.casefold())
    sticker_refs: set[tuple[int, str]] = set()
    sticker_names: set[str] = set()
    for raw in stickers:
        if not isinstance(raw, dict):
            raise ValueError("guild snapshot sticker is invalid")
        ref = _event_ref(raw, "sticker")
        if ref[1] != origin or ref in sticker_refs:
            raise ValueError("guild snapshot sticker identity is invalid")
        if (
            database_snowflake(raw.get("guild_id"), "sticker guild id"),
            normalize_domain(str(raw.get("guild_domain", ""))),
        ) != (guild_id, origin):
            raise ValueError("guild snapshot sticker references the wrong guild")
        name = raw.get("name")
        description = raw.get("description")
        media_hash = raw.get("media_hash")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9_]{2,32}", name) is None
            or name.casefold() in sticker_names
            or (
                description is not None
                and (not isinstance(description, str) or len(description) > 100)
            )
            or not isinstance(media_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", media_hash) is None
            or not isinstance(raw.get("animated"), bool)
        ):
            raise ValueError("guild snapshot sticker fields are invalid")
        _event_resource_version(raw, "sticker")
        sticker_refs.add(ref)
        sticker_names.add(name.casefold())


def tombstone_omitted_replicated_channel(channel: Channel) -> None:
    """Keep only the channel identity needed to restore a later snapshot."""

    channel.unavailable = True
    if channel.type in {10, 11, 12}:
        channel.parent_id = None
        channel.parent_domain = None
        channel.last_message_id = None
        channel.last_message_domain = None
        return
    channel.name = None
    channel.topic = None
    channel.position = 0
    channel.parent_id = None
    channel.parent_domain = None
    channel.rate_limit_per_user = 0
    channel.last_message_id = None
    channel.last_message_domain = None


async def purge_replicated_channel_cache(
    session: AsyncSession,
    settings: Settings,
    channel: Channel,
    *,
    reconcile: bool = True,
) -> None:
    """Logically and physically evict inaccessible replicated channel data.

    Access is revoked immediately by tombstoning the channel and deleting its
    replaceable message rows. A non-visible message shadow is retained when it
    anchors media hosted by this instance: deleting that row would cascade the
    authoritative attachment before its scan/report/tombstone lifecycle has
    completed. Cached remote object bytes are marked expired in the same
    transaction; the storage GC performs retryable physical deletion.
    """

    if (
        channel.guild_id is None
        or channel.guild_domain is None
        or channel.origin_domain != channel.guild_domain
    ):
        raise ValueError("only replicated guild channels may be purged")
    message_refs = select(Message.id, Message.origin_domain).where(
        Message.channel_id == channel.id,
        Message.channel_domain == channel.origin_domain,
    )
    attachment_refs = select(Attachment.id, Attachment.origin_domain).where(
        tuple_(Attachment.message_id, Attachment.message_domain).in_(message_refs)
    )
    refs = list(await session.execute(attachment_refs))
    if refs:
        await session.execute(
            update(RemoteMediaCache)
            .where(
                tuple_(
                    RemoteMediaCache.attachment_id,
                    RemoteMediaCache.origin_domain,
                ).in_(refs)
            )
            .values(expires_at=datetime.now(UTC))
        )
    await session.execute(
        update(ReadState)
        .where(
            ReadState.channel_id == channel.id,
            ReadState.channel_domain == channel.origin_domain,
        )
        .values(last_message_id=None, last_message_domain=None, mention_count=0)
    )
    await session.execute(
        update(Message)
        .where(
            Message.channel_id == channel.id,
            Message.channel_domain == channel.origin_domain,
            Message.referenced_message_id.is_not(None),
        )
        .values(referenced_message_id=None, referenced_message_domain=None)
    )
    channel.last_message_id = None
    channel.last_message_domain = None
    await session.flush()
    await session.execute(
        delete(Message).where(
            Message.channel_id == channel.id,
            Message.channel_domain == channel.origin_domain,
            ~exists(
                select(Attachment.id).where(
                    Attachment.message_id == Message.id,
                    Attachment.message_domain == Message.origin_domain,
                    Attachment.origin_domain == settings.domain,
                )
            ),
        )
    )
    await session.execute(
        delete(TrackerBoard).where(
            TrackerBoard.channel_id == channel.id,
            TrackerBoard.channel_domain == channel.origin_domain,
        )
    )
    tombstone_omitted_replicated_channel(channel)
    await session.flush()
    if reconcile:
        await reconcile_replica_storage(
            session,
            channel.guild_id,
            channel.guild_domain,
        )


async def purge_orphaned_replicated_guilds(
    session: AsyncSession,
    settings: Settings,
    *,
    limit: int = MAX_ORPHANED_REPLICA_PURGE,
) -> int:
    """Evict remote guild replicas that no local account can access.

    Candidate guild rows are locked before a second membership check. The lock
    prevents a concurrent membership insert from racing the deletion, while
    ``SKIP LOCKED`` keeps the retention job from waiting on an active join or
    reconciliation transaction. The caller owns the transaction and commit.
    """

    local_attachment_anchor = exists(
        select(Attachment.id)
        .join(
            Message,
            (Message.id == Attachment.message_id)
            & (Message.origin_domain == Attachment.message_domain),
        )
        .join(
            Channel,
            (Channel.id == Message.channel_id) & (Channel.origin_domain == Message.channel_domain),
        )
        .where(
            Attachment.origin_domain == settings.domain,
            Channel.guild_id == Guild.id,
            Channel.guild_domain == Guild.origin_domain,
        )
    )
    candidates = list(
        await session.scalars(
            select(Guild)
            .where(
                Guild.origin_domain != settings.domain,
                ~local_guild_membership_exists(settings.domain),
                ~local_attachment_anchor,
            )
            .order_by(Guild.origin_domain, Guild.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    removed = 0
    for guild in candidates:
        # Recheck after acquiring the guild lock. A membership could have been
        # committed after the candidate snapshot but before this transaction
        # acquired its lock.
        has_local_member = await session.scalar(
            select(local_guild_membership_exists(settings.domain)).where(
                Guild.id == guild.id,
                Guild.origin_domain == guild.origin_domain,
            )
        )
        if bool(has_local_member):
            continue
        has_local_attachment = await session.scalar(
            select(
                exists(
                    select(Attachment.id)
                    .join(
                        Message,
                        (Message.id == Attachment.message_id)
                        & (Message.origin_domain == Attachment.message_domain),
                    )
                    .join(
                        Channel,
                        (Channel.id == Message.channel_id)
                        & (Channel.origin_domain == Message.channel_domain),
                    )
                    .where(
                        Attachment.origin_domain == settings.domain,
                        Channel.guild_id == guild.id,
                        Channel.guild_domain == guild.origin_domain,
                    )
                )
            )
        )
        if bool(has_local_attachment):
            guild.unavailable = True
            guild.sync_status = "stale"
            continue
        channels = list(
            await session.scalars(
                select(Channel).where(
                    Channel.guild_id == guild.id,
                    Channel.guild_domain == guild.origin_domain,
                )
            )
        )
        for channel in channels:
            await purge_replicated_channel_cache(session, settings, channel, reconcile=False)
        await session.delete(guild)
        removed += 1
    return removed


async def apply_guild_access_revocation(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    *,
    user_id: int,
    user_domain: str,
) -> bool:
    """Remove local access even when the home will no longer serve a snapshot."""

    locked = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
    )
    if locked is None or locked.origin_domain == settings.domain:
        raise ValueError("access revocation references an invalid replicated guild")
    if normalize_domain(user_domain) != settings.domain:
        raise ValueError("access revocation target does not belong to this instance")
    membership = await session.get(
        GuildMember,
        (locked.id, locked.origin_domain, user_id, user_domain),
    )
    removed = membership is not None
    if membership is not None:
        await session.delete(membership)
        await session.flush()
    remaining_local_member = await session.scalar(
        select(GuildMember.user_id)
        .where(
            GuildMember.guild_id == locked.id,
            GuildMember.guild_domain == locked.origin_domain,
            GuildMember.user_domain == settings.domain,
        )
        .limit(1)
    )
    if remaining_local_member is None:
        locked.unavailable = True
        locked.sync_status = "stale"
        channels = list(
            await session.scalars(
                select(Channel).where(
                    Channel.guild_id == locked.id,
                    Channel.guild_domain == locked.origin_domain,
                )
            )
        )
        for channel in channels:
            await purge_replicated_channel_cache(session, settings, channel, reconcile=False)
        await session.execute(
            delete(ChannelOverwrite).where(
                ChannelOverwrite.channel_id.in_([channel.id for channel in channels]),
                ChannelOverwrite.channel_domain == locked.origin_domain,
            )
        )
    from app.federation.history import purge_ineligible_federated_history

    await purge_ineligible_federated_history(session, settings, locked)
    await reconcile_replica_storage(session, locked.id, locked.origin_domain)
    return removed


async def apply_guild_instance_access_revocation(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    *,
    target_domain: str,
) -> list[int]:
    """Apply an authority-signed revocation for every local guild member."""

    if normalize_domain(target_domain) != settings.domain:
        raise ValueError("instance access revocation was addressed to another instance")
    locked = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
    )
    if locked is None or locked.origin_domain == settings.domain:
        raise ValueError("instance revocation references an invalid replicated guild")
    removed = list(
        await session.scalars(
            delete(GuildMember)
            .where(
                GuildMember.guild_id == locked.id,
                GuildMember.guild_domain == locked.origin_domain,
                GuildMember.user_domain == settings.domain,
            )
            .returning(GuildMember.user_id)
        )
    )
    locked.unavailable = True
    locked.sync_status = "stale"
    channels = list(
        await session.scalars(
            select(Channel).where(
                Channel.guild_id == locked.id,
                Channel.guild_domain == locked.origin_domain,
            )
        )
    )
    for channel in channels:
        await purge_replicated_channel_cache(session, settings, channel, reconcile=False)
    if channels:
        await session.execute(
            delete(ChannelOverwrite).where(
                ChannelOverwrite.channel_id.in_([channel.id for channel in channels]),
                ChannelOverwrite.channel_domain == locked.origin_domain,
            )
        )
    from app.federation.history import purge_ineligible_federated_history

    await purge_ineligible_federated_history(session, settings, locked)
    await reconcile_replica_storage(session, locked.id, locked.origin_domain)
    return removed


def guild_snapshot_payload(
    guild: Guild,
    roles: list[Role],
    channels: list[Channel],
    members: Sequence[tuple[GuildMember, User]],
    member_roles: list[MemberRole],
    overwrites: list[ChannelOverwrite],
    *,
    emojis: list[Emoji] | None = None,
    stickers: list[Sticker] | None = None,
    thread_members: list[ThreadMember] | None = None,
    member_snapshot_at: datetime,
    next_member_cursor: tuple[str, int] | None = None,
    snapshot_seq: int | None = None,
) -> dict[str, Any]:
    visible_channel_refs = {(channel.id, channel.origin_domain) for channel in channels}
    return {
        "snapshot_seq": str(guild.next_event_seq - 1 if snapshot_seq is None else snapshot_seq),
        "snapshot_generation": str(getattr(guild, "snapshot_generation", 1) or 1),
        "member_snapshot_at": member_snapshot_at.isoformat(),
        "next_member_cursor": (
            {
                "user_domain": next_member_cursor[0],
                "user_id": str(next_member_cursor[1]),
            }
            if next_member_cursor is not None
            else None
        ),
        "guild": {
            "id": str(guild.id),
            "origin_domain": guild.origin_domain,
            "name": guild.name,
            "description": guild.description,
            "icon_hash": guild.icon_hash,
            "banner_hash": guild.banner_hash,
            "owner_id": str(guild.owner_id),
            "owner_domain": guild.owner_domain,
            "permission_generation": str(guild.permission_generation),
            "federated_history_policy": guild.federated_history_policy,
            "history_policy_generation": str(guild.history_policy_generation),
            "version": guild.updated_at.isoformat(),
        },
        "roles": [
            {
                "id": str(role.id),
                "origin_domain": role.origin_domain,
                "name": role.name,
                "icon_hash": role.icon_hash,
                "color": role.color,
                "permissions": str(role.permissions),
                "position": role.position,
                "hoist": role.hoist,
                "mentionable": role.mentionable,
                "version": role.updated_at.isoformat(),
            }
            for role in roles
        ],
        "channels": [
            {
                "id": str(channel.id),
                "origin_domain": channel.origin_domain,
                "type": channel.type,
                "nsfw": bool(getattr(channel, "nsfw", False)),
                "name": channel.name,
                "topic": channel.topic,
                "position": channel.position,
                # A child may be visible through an explicit overwrite while its
                # category is hidden. Flatten that child in the peer snapshot
                # instead of leaking the hidden parent or emitting a dangling FK.
                "parent_id": (
                    str(channel.parent_id)
                    if channel.parent_id is not None
                    and (channel.parent_id, channel.parent_domain) in visible_channel_refs
                    else None
                ),
                "parent_domain": (
                    channel.parent_domain
                    if channel.parent_id is not None
                    and (channel.parent_id, channel.parent_domain) in visible_channel_refs
                    else None
                ),
                "permissions_synced": bool(
                    getattr(channel, "permissions_synced", False)
                    and channel.parent_id is not None
                    and (channel.parent_id, channel.parent_domain) in visible_channel_refs
                ),
                "rate_limit_per_user": channel.rate_limit_per_user,
                "bitrate": channel.bitrate,
                "user_limit": channel.user_limit,
                "rtc_region": channel.rtc_region,
                "video_quality_mode": channel.video_quality_mode,
                "flags": str(channel.flags),
                "owner_id": str(channel.owner_id) if channel.owner_id is not None else None,
                "owner_domain": channel.owner_domain,
                "archived": channel.archived,
                "locked": channel.locked,
                "invitable": channel.invitable,
                "auto_archive_duration": channel.auto_archive_duration,
                "archive_timestamp": (
                    channel.archive_timestamp.isoformat()
                    if channel.archive_timestamp is not None
                    else None
                ),
                "last_activity_at": (
                    channel.last_activity_at.isoformat()
                    if channel.last_activity_at is not None
                    else None
                ),
                "message_count": channel.message_count,
                "total_message_sent": channel.total_message_sent,
                "member_count": channel.member_count,
                "starter_message_id": (
                    str(channel.starter_message_id)
                    if channel.starter_message_id is not None
                    else None
                ),
                "starter_message_domain": channel.starter_message_domain,
                "last_thread_id": (
                    str(channel.last_thread_id) if channel.last_thread_id is not None else None
                ),
                "last_thread_domain": channel.last_thread_domain,
                "default_auto_archive_duration": channel.default_auto_archive_duration,
                "default_thread_rate_limit_per_user": (channel.default_thread_rate_limit_per_user),
                "available_tags": channel.available_tags,
                "applied_tag_ids": channel.applied_tag_ids,
                "default_reaction_emoji": channel.default_reaction_emoji,
                "default_sort_order": channel.default_sort_order,
                "default_forum_layout": channel.default_forum_layout,
                "e2ee_required": channel.e2ee_required,
                "created_at": channel.created_at.isoformat(),
                "federated_history_policy": channel.federated_history_policy,
                "encryption_mode": getattr(channel, "encryption_mode", "plaintext"),
                "encryption_policy": channel_encryption_policy_payload(channel),
                "created_floor_id": str(channel.created_floor_id),
                "version": channel.updated_at.isoformat(),
            }
            for channel in channels
        ],
        "members": [
            {
                "user": profile_from_user(user),
                "nickname": member.nickname,
                "joined_at": member.joined_at.isoformat(),
                "temporary": member.temporary,
                "timeout_until": (
                    member.timeout_until.isoformat() if member.timeout_until else None
                ),
                "timeout_indefinite": member.timeout_indefinite,
                "member_version": str(member.member_version),
            }
            for member, user in members
        ],
        "member_roles": [
            {
                "user_id": str(item.user_id),
                "user_domain": item.user_domain,
                "role_id": str(item.role_id),
                "role_domain": item.role_domain,
            }
            for item in member_roles
        ],
        "overwrites": [
            {
                "channel_id": str(item.channel_id),
                "channel_domain": item.channel_domain,
                "target_id": str(item.target_id),
                "target_domain": item.target_domain,
                "target_type": item.target_type,
                "allow": str(item.allow),
                "deny": str(item.deny),
            }
            for item in overwrites
        ],
        "thread_members": [
            {
                "id": str(item.thread_id),
                "thread_domain": item.thread_domain,
                "guild_id": str(item.guild_id),
                "guild_domain": item.guild_domain,
                "user_id": str(item.user_id),
                "user_domain": item.user_domain,
                "join_timestamp": item.joined_at.isoformat(),
                "flags": item.flags,
                "notification_level": item.notification_level,
            }
            for item in (thread_members or [])
            if (item.thread_id, item.thread_domain) in visible_channel_refs
        ],
        "emojis": [
            {
                "id": str(item.id),
                "origin_domain": item.origin_domain,
                "guild_id": str(item.guild_id),
                "guild_domain": item.guild_domain,
                "name": item.name,
                "animated": item.animated,
                "media_hash": item.media_hash,
                "version": item.updated_at.isoformat(),
            }
            for item in (emojis or [])
            if item.media_hash is not None
        ],
        "stickers": [
            {
                "id": str(item.id),
                "origin_domain": item.origin_domain,
                "guild_id": str(item.guild_id),
                "guild_domain": item.guild_domain,
                "name": item.name,
                "description": item.description,
                "animated": item.animated,
                "media_hash": item.media_hash,
                "version": item.updated_at.isoformat(),
            }
            for item in (stickers or [])
            if item.media_hash is not None
        ],
    }


async def apply_guild_snapshot(
    session: AsyncSession,
    settings: Settings,
    snapshot: dict[str, Any],
    *,
    expected_origin: str,
    expected_guild_id: int,
    required_member: tuple[int, str] | None = None,
) -> Guild:
    validate_guild_snapshot(
        snapshot,
        expected_origin=expected_origin,
        expected_guild_id=expected_guild_id,
        required_member=required_member,
    )
    raw_guild = snapshot["guild"]
    guild_version = _event_resource_version(raw_guild, "guild")
    origin = str(raw_guild["origin_domain"])
    if origin == settings.domain:
        raise HTTPException(status_code=409, detail={"code": "GUILD_IS_LOCAL"})
    guild_id = int(raw_guild["id"])
    from app.federation.terminal_rooms import lock_terminal_room

    await lock_terminal_room(session, "guild", guild_id, origin)
    if (
        await session.get(
            TerminalRoomDeletion,
            ("guild", guild_id, origin, settings.domain),
            populate_existing=True,
        )
        is not None
    ):
        raise HTTPException(status_code=410, detail={"code": "GUILD_DELETED"})
    guild = await session.get(Guild, (guild_id, origin))
    if guild is None:
        guild = Guild(
            id=guild_id,
            origin_domain=origin,
            name=str(raw_guild["name"]),
            owner_id=int(raw_guild["owner_id"]),
            owner_domain=str(raw_guild["owner_domain"]),
        )
        session.add(guild)
        await session.flush()
    else:
        guild = await session.scalar(
            select(Guild)
            .where(Guild.id == guild_id, Guild.origin_domain == origin)
            .with_for_update()
        )
        if guild is None:
            raise RuntimeError("replicated guild disappeared during snapshot application")
    intents = await _locked_remote_membership_intents(
        session,
        settings,
        guild_id=guild_id,
        guild_domain=origin,
    )
    existing_local_members = set(
        (
            await session.execute(
                select(GuildMember.user_id, GuildMember.user_domain).where(
                    GuildMember.guild_id == guild_id,
                    GuildMember.guild_domain == origin,
                    GuildMember.user_domain == settings.domain,
                )
            )
        ).tuples()
    )
    existing_required_member = False
    if required_member is not None:
        required_member = (required_member[0], normalize_domain(required_member[1]))
        loaded_required_member = await session.get(
            GuildMember,
            (guild_id, origin, required_member[0], required_member[1]),
        )
        existing_required_member = loaded_required_member is not None
    (
        snapshot_members,
        snapshot_member_roles,
        snapshot_overwrites,
        required_intent,
    ) = filter_remote_snapshot_memberships(
        snapshot,
        intents,
        local_domain=settings.domain,
        required_member=required_member,
        existing_required_member=existing_required_member,
        existing_local_members=existing_local_members,
    )
    users = {
        (
            int(raw["user"]["id"]),
            str(raw["user"]["origin_domain"]),
        ): await resolve_delegated_profile(
            session,
            settings,
            RemoteUserProfile.model_validate(raw["user"]),
            authority_origin=origin,
        )
        for raw in snapshot_members
    }
    guild.name = str(raw_guild["name"])
    guild.owner_id = int(raw_guild["owner_id"])
    guild.owner_domain = str(raw_guild["owner_domain"])
    guild.description = raw_guild.get("description")
    guild.icon_hash = raw_guild.get("icon_hash")
    guild.banner_hash = raw_guild.get("banner_hash")
    guild.permission_generation = int(raw_guild.get("permission_generation", 1))
    guild.snapshot_generation = int(snapshot.get("snapshot_generation", 1))
    guild.federated_history_policy = str(raw_guild.get("federated_history_policy", "disabled"))
    guild.history_policy_generation = int(raw_guild.get("history_policy_generation", 1))
    guild.last_event_seq = int(snapshot["snapshot_seq"])
    guild.next_event_seq = guild.last_event_seq + 1
    guild.sync_status = "ready"
    guild.unavailable = False
    versioned_resources: list[tuple[Guild | Role | Channel | Emoji | Sticker, datetime]] = []
    if guild_version is not None:
        versioned_resources.append((guild, guild_version))
    role_refs = {(int(raw["id"]), str(raw["origin_domain"])) for raw in snapshot["roles"]}
    channel_refs = {(int(raw["id"]), str(raw["origin_domain"])) for raw in snapshot["channels"]}
    member_refs = {
        (int(raw["user"]["id"]), str(raw["user"]["origin_domain"])) for raw in snapshot_members
    }
    snapshot_thread_members = [
        raw
        for raw in snapshot.get("thread_members", [])
        if (int(raw["user_id"]), str(raw["user_domain"])) in member_refs
    ]
    existing_roles = list(
        await session.scalars(
            select(Role).where(Role.guild_id == guild.id, Role.guild_domain == origin)
        )
    )
    for role in existing_roles:
        if (role.id, role.origin_domain) not in role_refs:
            await session.delete(role)
    # Tracker content is hydrated through its independently paginated,
    # permission-filtered endpoint. A full structural snapshot invalidates all
    # cached boards atomically so no old task survives a permission or channel
    # topology recovery.
    await session.execute(
        delete(TrackerBoard).where(
            TrackerBoard.guild_id == guild.id,
            TrackerBoard.guild_domain == origin,
        )
    )
    existing_channels = list(
        await session.scalars(
            select(Channel).where(Channel.guild_id == guild.id, Channel.guild_domain == origin)
        )
    )
    omitted_channel_ids: list[int] = []
    for channel in existing_channels:
        if (channel.id, channel.origin_domain) not in channel_refs:
            omitted_channel_ids.append(channel.id)
            await purge_replicated_channel_cache(session, settings, channel, reconcile=False)
    if omitted_channel_ids:
        await session.execute(
            delete(ChannelOverwrite).where(
                ChannelOverwrite.channel_id.in_(omitted_channel_ids),
                ChannelOverwrite.channel_domain == origin,
            )
        )
    existing_members = list(
        await session.scalars(
            select(GuildMember).where(
                GuildMember.guild_id == guild.id, GuildMember.guild_domain == origin
            )
        )
    )
    for member in existing_members:
        if (member.user_id, member.user_domain) not in member_refs:
            if member.user_domain == settings.domain:
                await mark_remote_guild_departed(
                    session,
                    settings,
                    guild_id=guild.id,
                    guild_domain=origin,
                    user_id=member.user_id,
                    user_domain=member.user_domain,
                )
            await session.delete(member)
    emoji_refs = {(int(raw["id"]), str(raw["origin_domain"])) for raw in snapshot.get("emojis", [])}
    existing_emojis = list(
        await session.scalars(
            select(Emoji).where(Emoji.guild_id == guild.id, Emoji.guild_domain == origin)
        )
    )
    for emoji in existing_emojis:
        if (emoji.id, emoji.origin_domain) not in emoji_refs:
            await session.delete(emoji)
    sticker_refs = {
        (int(raw["id"]), str(raw["origin_domain"])) for raw in snapshot.get("stickers", [])
    }
    existing_stickers = list(
        await session.scalars(
            select(Sticker).where(Sticker.guild_id == guild.id, Sticker.guild_domain == origin)
        )
    )
    for sticker in existing_stickers:
        if (sticker.id, sticker.origin_domain) not in sticker_refs:
            await session.delete(sticker)
    for raw in snapshot["roles"]:
        loaded_role = await session.get(Role, (int(raw["id"]), str(raw["origin_domain"])))
        if loaded_role is None:
            loaded_role = Role(
                id=int(raw["id"]),
                origin_domain=str(raw["origin_domain"]),
                guild_id=guild.id,
                guild_domain=guild.origin_domain,
                name=str(raw["name"]),
                position=int(raw["position"]),
            )
            session.add(loaded_role)
        elif (loaded_role.guild_id, loaded_role.guild_domain) != (guild.id, guild.origin_domain):
            raise ValueError("snapshot role identity conflicts with another guild")
        loaded_role.name = str(raw["name"])
        loaded_role.icon_hash = str(raw["icon_hash"]) if raw.get("icon_hash") is not None else None
        loaded_role.color = int(raw["color"])
        loaded_role.permissions = int(raw["permissions"])
        loaded_role.position = int(raw["position"])
        loaded_role.hoist = bool(raw["hoist"])
        loaded_role.mentionable = bool(raw["mentionable"])
        role_version = _apply_event_resource_version(loaded_role, raw, "role")
        if role_version is not None:
            versioned_resources.append((loaded_role, role_version))
    for raw in snapshot["channels"]:
        loaded_channel = await session.get(Channel, (int(raw["id"]), str(raw["origin_domain"])))
        if loaded_channel is None:
            loaded_channel = Channel(
                id=int(raw["id"]),
                origin_domain=str(raw["origin_domain"]),
                guild_id=guild.id,
                guild_domain=guild.origin_domain,
                type=int(raw["type"]),
                created_floor_id=int(raw["created_floor_id"]),
            )
            session.add(loaded_channel)
        elif (loaded_channel.guild_id, loaded_channel.guild_domain) != (
            guild.id,
            guild.origin_domain,
        ):
            raise ValueError("snapshot channel identity conflicts with another guild")
        loaded_channel.unavailable = False
        loaded_channel.type = int(raw["type"])
        loaded_channel.created_floor_id = int(raw["created_floor_id"])
        created_at = _event_datetime(raw.get("created_at"), "channel creation", optional=True)
        if created_at is not None:
            loaded_channel.created_at = created_at
        loaded_channel.name = raw.get("name")
        loaded_channel.topic = raw.get("topic")
        loaded_channel.position = int(raw["position"])
        loaded_channel.parent_id = int(raw["parent_id"]) if raw.get("parent_id") else None
        loaded_channel.parent_domain = raw.get("parent_domain")
        loaded_channel.permissions_synced = bool(raw.get("permissions_synced", False))
        loaded_channel.rate_limit_per_user = int(raw["rate_limit_per_user"])
        voice_state = _validated_voice_channel_state(raw, loaded_channel.type)
        for field, value in voice_state.items():
            setattr(loaded_channel, field, value)
        loaded_channel.federated_history_policy = str(
            raw.get("federated_history_policy", "inherit")
        )
        raw_encryption_policy = raw.get("encryption_policy")
        if raw_encryption_policy is None:
            legacy_mode = raw.get("encryption_mode", "plaintext")
            raw_encryption_policy = {
                "mode": legacy_mode,
                "state": "legacy" if legacy_mode == "e2ee" else "plaintext",
                "generation": "0",
            }
        encryption_policy = validate_channel_encryption_policy(raw_encryption_policy)
        incoming_generation = int(encryption_policy["generation"])
        validate_channel_encryption_policy_transition(
            loaded_channel,
            encryption_policy,
            label="snapshot channel",
        )
        loaded_channel.encryption_mode = str(encryption_policy["mode"])
        loaded_channel.encryption_state = str(encryption_policy["state"])
        loaded_channel.encryption_policy_generation = incoming_generation
        loaded_channel.encryption_protocol = encryption_policy["protocol"]
        loaded_channel.encryption_suite = encryption_policy["suite"]
        loaded_channel.encryption_group_id = encryption_policy["group_id"]
        loaded_channel.encryption_epoch = encryption_policy["epoch"]
        extension_state = _validated_channel_extension_state(raw, loaded_channel.type, origin)
        for field, value in extension_state.items():
            setattr(loaded_channel, field, value)
        channel_version = _apply_event_resource_version(loaded_channel, raw, "channel")
        if channel_version is not None:
            versioned_resources.append((loaded_channel, channel_version))
    for raw in snapshot.get("emojis", []):
        loaded_emoji = await session.get(Emoji, (int(raw["id"]), str(raw["origin_domain"])))
        if loaded_emoji is None:
            loaded_emoji = Emoji(
                id=int(raw["id"]),
                origin_domain=str(raw["origin_domain"]),
                guild_id=guild.id,
                guild_domain=guild.origin_domain,
                name=str(raw["name"]),
                object_key=f"remote:{origin}:{raw['id']}",
                creator_id=guild.owner_id,
                creator_domain=guild.owner_domain,
            )
            session.add(loaded_emoji)
        elif (loaded_emoji.guild_id, loaded_emoji.guild_domain) != (guild.id, origin):
            raise ValueError("snapshot emoji identity conflicts with another guild")
        loaded_emoji.name = str(raw["name"])
        loaded_emoji.animated = bool(raw["animated"])
        loaded_emoji.media_hash = str(raw["media_hash"])
        emoji_version = _apply_event_resource_version(loaded_emoji, raw, "emoji")
        if emoji_version is not None:
            versioned_resources.append((loaded_emoji, emoji_version))
    for raw in snapshot.get("stickers", []):
        loaded_sticker = await session.get(Sticker, (int(raw["id"]), str(raw["origin_domain"])))
        if loaded_sticker is None:
            loaded_sticker = Sticker(
                id=int(raw["id"]),
                origin_domain=str(raw["origin_domain"]),
                guild_id=guild.id,
                guild_domain=guild.origin_domain,
                name=str(raw["name"]),
                creator_id=guild.owner_id,
                creator_domain=guild.owner_domain,
            )
            session.add(loaded_sticker)
        elif (loaded_sticker.guild_id, loaded_sticker.guild_domain) != (guild.id, origin):
            raise ValueError("snapshot sticker identity conflicts with another guild")
        loaded_sticker.name = str(raw["name"])
        loaded_sticker.description = raw.get("description")
        loaded_sticker.animated = bool(raw["animated"])
        loaded_sticker.media_hash = str(raw["media_hash"])
        sticker_version = _apply_event_resource_version(loaded_sticker, raw, "sticker")
        if sticker_version is not None:
            versioned_resources.append((loaded_sticker, sticker_version))
    await session.flush()
    if channel_refs:
        await session.execute(
            delete(ChannelOverwrite).where(
                ChannelOverwrite.channel_id.in_([item[0] for item in channel_refs]),
                ChannelOverwrite.channel_domain == origin,
            )
        )
    for raw in snapshot_overwrites:
        session.add(
            ChannelOverwrite(
                channel_id=int(raw["channel_id"]),
                channel_domain=str(raw["channel_domain"]),
                guild_id=guild.id,
                guild_domain=guild.origin_domain,
                target_id=int(raw["target_id"]),
                target_domain=str(raw["target_domain"]),
                target_type=str(raw["target_type"]),
                allow=int(raw["allow"]),
                deny=int(raw["deny"]),
            )
        )
    for raw in snapshot_members:
        user_ref = (int(raw["user"]["id"]), str(raw["user"]["origin_domain"]))
        user = users[user_ref]
        loaded_member = await session.get(
            GuildMember, (guild.id, origin, user.id, user.origin_domain)
        )
        if loaded_member is None:
            loaded_member = GuildMember(
                guild_id=guild.id,
                guild_domain=origin,
                user_id=user.id,
                user_domain=user.origin_domain,
                joined_at=datetime.fromisoformat(str(raw["joined_at"])),
            )
            session.add(loaded_member)
        loaded_member.nickname = raw.get("nickname")
        loaded_member.temporary = bool(raw.get("temporary", False))
        loaded_member.timeout_until = (
            datetime.fromisoformat(str(raw["timeout_until"])) if raw.get("timeout_until") else None
        )
        loaded_member.timeout_indefinite = bool(raw.get("timeout_indefinite", False))
        loaded_member.timeout_reason = None
        loaded_member.voice_flags = 0
        loaded_member.member_version = int(raw.get("member_version", 1))
    await session.flush()
    if member_refs:
        incoming_thread_member_refs = {
            (
                int(raw["id"]),
                str(raw["thread_domain"]),
                int(raw["user_id"]),
                str(raw["user_domain"]),
            )
            for raw in snapshot_thread_members
        }
        existing_page_thread_members = list(
            await session.scalars(
                select(ThreadMember).where(
                    ThreadMember.guild_id == guild.id,
                    ThreadMember.guild_domain == origin,
                    tuple_(ThreadMember.user_id, ThreadMember.user_domain).in_(member_refs),
                )
            )
        )
        for loaded_thread_member in existing_page_thread_members:
            ref = (
                loaded_thread_member.thread_id,
                loaded_thread_member.thread_domain,
                loaded_thread_member.user_id,
                loaded_thread_member.user_domain,
            )
            if ref not in incoming_thread_member_refs:
                await session.delete(loaded_thread_member)
        for raw in snapshot_thread_members:
            key = (
                int(raw["id"]),
                str(raw["thread_domain"]),
                int(raw["user_id"]),
                str(raw["user_domain"]),
            )
            current_thread_member = await session.get(ThreadMember, key)
            if current_thread_member is None:
                current_thread_member = ThreadMember(
                    thread_id=key[0],
                    thread_domain=key[1],
                    guild_id=guild.id,
                    guild_domain=origin,
                    user_id=key[2],
                    user_domain=key[3],
                    joined_at=datetime.fromisoformat(str(raw["join_timestamp"])),
                )
                session.add(current_thread_member)
            current_thread_member.flags = int(raw.get("flags", 0))
            current_thread_member.notification_level = str(raw.get("notification_level", "inherit"))
        await session.flush()
    await session.execute(
        delete(MemberRole).where(MemberRole.guild_id == guild.id, MemberRole.guild_domain == origin)
    )
    for raw in snapshot_member_roles:
        loaded_member = await session.get(
            GuildMember,
            (
                guild.id,
                origin,
                int(raw["user_id"]),
                str(raw["user_domain"]),
            ),
        )
        if loaded_member is None:
            raise ValueError("guild snapshot role references an unknown member")
        loaded_member.temporary = False
        session.add(
            MemberRole(
                guild_id=guild.id,
                guild_domain=origin,
                user_id=int(raw["user_id"]),
                user_domain=str(raw["user_domain"]),
                role_id=int(raw["role_id"]),
                role_domain=str(raw["role_domain"]),
            )
        )
    await session.flush()
    if required_intent is not None:
        await complete_remote_guild_join(session, required_intent)
    from app.federation.history import purge_ineligible_federated_history

    await purge_ineligible_federated_history(session, settings, guild)
    for resource, version in versioned_resources:
        # Reconciliation can update cached channel cursors after the initial
        # structural upsert. Authority versions are the final public tokens.
        resource.updated_at = version
    await reconcile_replica_storage(session, guild.id, guild.origin_domain)
    await admit_replica_storage(session, settings, guild)
    return guild
