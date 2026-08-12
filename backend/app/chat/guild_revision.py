from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
        # Server defaults are populated during flush, but channel-create events
        # are rendered before queue_guild_mutation performs that flush.
        "federated_history_policy": channel.federated_history_policy or "inherit",
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
) -> int:
    """Sequence, retain, and durably fan out one authoritative guild mutation.

    The full envelope is retained at the home for permission-filtered gap fill.
    Live destinations that cannot currently inspect a channel receive a signed
    sequence-only redaction. Permission-sensitive mutations carry a snapshot
    fence; the receiver fetches a permission-filtered snapshot before accepting
    the retried event.
    """

    if guild.origin_domain != settings.domain or actor.origin_domain != settings.domain:
        raise RuntimeError("only a guild home and local actor may emit guild mutations")
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
        actor,
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
