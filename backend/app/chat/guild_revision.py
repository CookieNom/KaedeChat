from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.e2ee import channel_encryption_policy_payload
from app.chat.e2ee_membership import (
    GUILD_E2EE_ACCESS_MUTATION_EVENTS,
    pause_guild_e2ee_for_membership_change,
)
from app.chat.payloads import materialize_channel_created_at
from app.core.settings import Settings
from app.core.task_wake import enqueue_best_effort
from app.db.models import Channel, Guild, GuildMember, User
from app.federation.events import build_envelope, queue_event
from app.federation.guilds import (
    SNAPSHOT_NEUTRAL_GUILD_EVENTS,
    assign_guild_sequence,
    remote_destinations_with_channel_access,
    store_guild_event,
)


def remember_guild_delivery_wakes(guild: Guild, destinations: Iterable[str]) -> None:
    pending = set(getattr(guild, "_federation_delivery_wakes", set()))
    pending.update(destinations)
    guild._federation_delivery_wakes = pending  # type: ignore[attr-defined]


async def wake_queued_guild_federation(guild: Guild) -> None:
    """Wake committed outbox destinations; the minute sweep remains a fallback."""

    destinations = set(getattr(guild, "_federation_delivery_wakes", set()))
    guild._federation_delivery_wakes = set()  # type: ignore[attr-defined]
    if not destinations:
        return
    from app.tasks import federation_deliver

    for destination in sorted(destinations):
        await enqueue_best_effort(federation_deliver, destination)


async def remote_guild_destinations(
    session: AsyncSession, settings: Settings, guild: Guild
) -> set[str]:
    return set(
        await session.scalars(
            select(GuildMember.user_domain)
            .where(
                GuildMember.guild_id == guild.id,
                GuildMember.guild_domain == guild.origin_domain,
                GuildMember.user_domain != settings.domain,
            )
            .distinct()
        )
    )


def federation_channel_state(channel: Channel) -> dict[str, object]:
    """Render the complete authoritative channel state used by guild events."""

    return {
        "id": str(channel.id),
        "origin_domain": channel.origin_domain,
        "guild_id": str(channel.guild_id) if channel.guild_id is not None else None,
        "guild_domain": channel.guild_domain,
        "type": channel.type,
        "name": channel.name,
        "topic": channel.topic,
        "position": channel.position,
        "parent_id": str(channel.parent_id) if channel.parent_id is not None else None,
        "parent_domain": channel.parent_domain,
        "permissions_synced": bool(channel.permissions_synced),
        "rate_limit_per_user": channel.rate_limit_per_user,
        # SQLAlchemy applies these defaults during INSERT. Guild mutations are
        # rendered before queue_guild_mutation flushes a newly-created channel,
        # so materialize the wire defaults here instead of emitting null.
        "flags": str(channel.flags or 0),
        "owner_id": str(channel.owner_id) if channel.owner_id is not None else None,
        "owner_domain": channel.owner_domain,
        "archived": channel.archived,
        "locked": channel.locked,
        "invitable": channel.invitable,
        "auto_archive_duration": channel.auto_archive_duration,
        "archive_timestamp": (
            channel.archive_timestamp.isoformat() if channel.archive_timestamp is not None else None
        ),
        "last_activity_at": (
            channel.last_activity_at.isoformat() if channel.last_activity_at is not None else None
        ),
        "message_count": channel.message_count,
        "total_message_sent": channel.total_message_sent,
        "member_count": channel.member_count,
        "starter_message_id": (
            str(channel.starter_message_id) if channel.starter_message_id is not None else None
        ),
        "starter_message_domain": channel.starter_message_domain,
        "last_thread_id": (
            str(channel.last_thread_id) if channel.last_thread_id is not None else None
        ),
        "last_thread_domain": channel.last_thread_domain,
        "default_auto_archive_duration": channel.default_auto_archive_duration,
        "default_thread_rate_limit_per_user": channel.default_thread_rate_limit_per_user,
        "available_tags": channel.available_tags or [],
        "applied_tag_ids": channel.applied_tag_ids or [],
        "default_reaction_emoji": channel.default_reaction_emoji,
        "default_sort_order": channel.default_sort_order,
        "default_forum_layout": channel.default_forum_layout,
        "e2ee_required": bool(channel.e2ee_required),
        "created_at": materialize_channel_created_at(channel).isoformat(),
        # Server defaults are populated during flush, but channel-create events
        # are rendered before queue_guild_mutation performs that flush.
        "federated_history_policy": channel.federated_history_policy or "inherit",
        "encryption_policy": channel_encryption_policy_payload(channel),
        "created_floor_id": str(channel.created_floor_id),
    }


async def queue_guild_mutation(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    actor: User,
    event_type: str,
    content: dict[str, Any],
    *,
    channel: Channel | None = None,
    snapshot_required: bool = False,
    extra_destinations: Iterable[str] = (),
    e2ee_policy_channels: list[Channel] | None = None,
    pause_e2ee: bool = True,
) -> int:
    """Sequence, retain, and durably fan out one authoritative guild mutation.

    The full envelope is retained at the home for permission-filtered gap fill.
    Live destinations that cannot currently inspect a channel receive a signed
    sequence-only redaction. Permission-sensitive mutations carry a snapshot
    fence; the receiver fetches a permission-filtered snapshot before accepting
    the retried event.
    """

    if guild.origin_domain != settings.domain:
        raise RuntimeError("only a guild home may emit guild mutations")
    signer = await guild_mutation_signer(session, settings, guild, actor)
    if pause_e2ee and event_type in GUILD_E2EE_ACCESS_MUTATION_EVENTS:
        paused = await pause_guild_e2ee_for_membership_change(session, guild)
        if e2ee_policy_channels is not None:
            known = {(item.id, item.origin_domain) for item in e2ee_policy_channels}
            e2ee_policy_channels.extend(
                item for item in paused if (item.id, item.origin_domain) not in known
            )
    snapshot_changed = event_type not in SNAPSHOT_NEUTRAL_GUILD_EVENTS
    if snapshot_changed:
        guild.snapshot_generation = int(getattr(guild, "snapshot_generation", 1) or 1) + 1
    await session.flush()
    seq = await assign_guild_sequence(session, guild)
    context: dict[str, Any] = {
        "guild_id": str(guild.id),
        "guild_domain": guild.origin_domain,
        "seq": str(seq),
    }
    if snapshot_changed:
        context["snapshot_generation"] = str(guild.snapshot_generation)
    if channel is not None:
        context.update(
            {
                "channel_id": str(channel.id),
                "channel_domain": channel.origin_domain,
            }
        )
    if snapshot_required:
        context["snapshot_required"] = True
        # Permission-sensitive receivers normally refresh from a snapshot.
        # Carry the durable generation as well so direct replay and older
        # peers cannot retain a permission cache under the previous fence.
        context["permission_generation"] = str(guild.permission_generation)
    envelope = await build_envelope(
        session,
        settings,
        event_type,
        signer,
        content,
        context=context,
    )
    store_guild_event(session, guild, seq, str(envelope["event_id"]), envelope)

    destinations = await remote_guild_destinations(session, settings, guild)
    destinations.update(
        destination for destination in extra_destinations if destination != settings.domain
    )
    visible_destinations = (
        await remote_destinations_with_channel_access(session, settings, guild, channel)
        if channel is not None and not channel.unavailable
        else destinations
    )
    hidden_destinations = destinations - visible_destinations
    for destination in sorted(visible_destinations & destinations):
        await queue_event(
            session,
            settings,
            destination,
            envelope,
            discover_destination=False,
        )
    if hidden_destinations:
        owner = await session.get(User, (guild.owner_id, guild.owner_domain))
        if owner is None or not owner.is_local or owner.origin_domain != settings.domain:
            raise RuntimeError("local guild owner cannot sign a redacted mutation")
        redacted_context = dict(context)
        redacted = await build_envelope(
            session,
            settings,
            "guild.event.redacted",
            owner,
            {"original_type": event_type},
            context=redacted_context,
        )
        for destination in sorted(hidden_destinations):
            await queue_event(
                session,
                settings,
                destination,
                redacted,
                discover_destination=False,
            )
    remember_guild_delivery_wakes(guild, destinations)
    return seq


async def guild_mutation_signer(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    actor: User,
) -> User:
    """Return the local signer for an authoritative guild mutation.

    Ordinary mutations are signed by their local actor. A signed federation
    proxy may act for a remote guild member, but an instance must never sign as
    that remote identity. In that case the local guild owner attests the
    authoritative result while the service keeps the remote actor for
    permission checks and audit attribution.
    """

    if guild.origin_domain != settings.domain:
        raise RuntimeError("only a guild home may sign guild mutations")
    if actor.is_local and actor.origin_domain == settings.domain:
        return actor
    owner = await session.get(User, (guild.owner_id, guild.owner_domain))
    if owner is None or not owner.is_local or owner.origin_domain != settings.domain:
        raise RuntimeError("local guild owner cannot sign a proxied mutation")
    return owner


async def queue_guild_access_revocation(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    *,
    user_id: int,
    user_domain: str,
    reason: str,
) -> set[str]:
    """Send a direct removal that remains usable after the last member leaves."""

    if user_domain == settings.domain:
        return set()
    owner = await session.get(User, (guild.owner_id, guild.owner_domain))
    if owner is None or not owner.is_local or owner.origin_domain != settings.domain:
        raise RuntimeError("local guild owner cannot sign an access revocation")
    revoked = await build_envelope(
        session,
        settings,
        "guild.access.revoked",
        owner,
        {
            "target": {"id": str(user_id), "domain": user_domain},
            "reason": reason,
        },
        context={
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
        },
    )
    await queue_event(
        session,
        settings,
        user_domain,
        revoked,
        discover_destination=False,
    )
    remember_guild_delivery_wakes(guild, (user_domain,))
    return {user_domain}


async def queue_guild_instance_access_revocation(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    *,
    instance_domain: str,
    reason: str,
) -> set[str]:
    """Revoke every cached membership held by one remote instance."""

    if instance_domain == settings.domain:
        raise ValueError("the local instance cannot be revoked from its own guild")
    owner = await session.get(User, (guild.owner_id, guild.owner_domain))
    if owner is None or not owner.is_local or owner.origin_domain != settings.domain:
        raise RuntimeError("local guild owner cannot sign an instance access revocation")
    revoked = await build_envelope(
        session,
        settings,
        "guild.instance_access.revoked",
        owner,
        {"target_domain": instance_domain, "reason": reason},
        context={"guild_id": str(guild.id), "guild_domain": guild.origin_domain},
    )
    await queue_event(
        session,
        settings,
        instance_domain,
        revoked,
        discover_destination=False,
    )
    remember_guild_delivery_wakes(guild, (instance_domain,))
    return {instance_domain}
