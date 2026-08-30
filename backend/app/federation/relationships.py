from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.events import guild_topic
from app.chat.payloads import member_payload
from app.chat.postcommit import queue_postcommit_dispatch
from app.chat.privacy import lock_relationship_pair, relationship
from app.core.settings import Settings
from app.db.models import Guild, GuildMember, MemberRole, Relationship, User
from app.federation.events import build_envelope, queue_event
from app.federation.replication import database_snowflake, profile_from_user, upsert_remote_user
from app.federation.schemas import (
    EventEnvelope,
    RelationshipEventContent,
    RemoteUserProfile,
)
from app.federation.security import validated_event_envelope

GUILD_PROFILE_RELAY_EVENT = "guild.member.profile.relay"


class RelationshipQuotaExceeded(ValueError):
    """A peer exhausted a bounded pending-request allowance.

    The public federation result is deliberately a single stable code.  It
    does not reveal whether the recipient, the actor's origin, or the
    recipient/origin pair hit its limit, because those distinctions would let
    a remote peer probe a local user's relationship state.
    """

    code = "KAED_FED_RELATIONSHIP_REQUEST_QUOTA_EXCEEDED"


@dataclass(slots=True)
class RelationshipApplication:
    recipient: User
    actor: User
    relation_type: str | None = None
    wake_destination: str | None = None


async def admit_pending_relationship_request(
    session: AsyncSession,
    settings: Settings,
    recipient: User,
    *,
    actor_id: int,
    actor_domain: str,
) -> None:
    """Serialize and bound a new inbound request before caching its actor."""

    lock_scopes = sorted(
        {
            f"kaede-relationship-pending-origin:{actor_domain}",
            (f"kaede-relationship-pending-recipient:{recipient.id}@{recipient.origin_domain}"),
        }
    )
    for scope in lock_scopes:
        await session.scalar(select(func.pg_advisory_xact_lock(func.hashtextextended(scope, 0))))

    existing = await session.scalar(
        select(Relationship.type).where(
            Relationship.user_id == recipient.id,
            Relationship.user_domain == recipient.origin_domain,
            Relationship.target_id == actor_id,
            Relationship.target_domain == actor_domain,
        )
    )
    if existing is not None:
        return

    recipient_pending = int(
        await session.scalar(
            select(func.count())
            .select_from(Relationship)
            .where(
                Relationship.user_id == recipient.id,
                Relationship.user_domain == recipient.origin_domain,
                Relationship.type == "pending_in",
            )
        )
        or 0
    )
    if recipient_pending >= settings.federation_pending_relationships_per_recipient:
        raise RelationshipQuotaExceeded("recipient pending relationship request quota exceeded")

    recipient_origin_pending = int(
        await session.scalar(
            select(func.count())
            .select_from(Relationship)
            .where(
                Relationship.user_id == recipient.id,
                Relationship.user_domain == recipient.origin_domain,
                Relationship.target_domain == actor_domain,
                Relationship.type == "pending_in",
            )
        )
        or 0
    )
    if recipient_origin_pending >= settings.federation_pending_relationships_per_recipient_origin:
        raise RelationshipQuotaExceeded(
            "origin pending relationship quota for this recipient exceeded"
        )

    origin_pending = int(
        await session.scalar(
            select(func.count())
            .select_from(Relationship)
            .where(
                Relationship.target_domain == actor_domain,
                Relationship.type == "pending_in",
            )
        )
        or 0
    )
    if origin_pending >= settings.federation_pending_relationships_per_origin:
        raise RelationshipQuotaExceeded("origin pending relationship request quota exceeded")


def acceptance_matches(
    relation_type: str | None,
    stored_request_id: str | None,
    received_request_id: str,
) -> bool:
    return relation_type == "pending_out" and stored_request_id == received_request_id


def relationship_event_content(
    actor: User,
    target: User,
    request_id: str | None,
) -> dict[str, object]:
    content: dict[str, object] = {
        "actor": profile_from_user(actor),
        "target": {"id": str(target.id), "domain": target.origin_domain},
    }
    if request_id is not None:
        content["request_id"] = request_id
    return content


async def queue_profile_updates(
    session: AsyncSession,
    settings: Settings,
    actor: User,
) -> set[str]:
    """Queue one authoritative profile revision to every entitled remote peer."""

    targets = (
        await session.execute(
            select(Relationship.target_id, Relationship.target_domain).where(
                Relationship.user_id == actor.id,
                Relationship.user_domain == actor.origin_domain,
                Relationship.type == "friend",
                Relationship.target_domain != settings.domain,
            )
        )
    ).all()
    destinations: set[str] = set()
    for target_id, target_domain in targets:
        envelope = await build_envelope(
            session,
            settings,
            "relationship.profile",
            actor,
            {
                "actor": profile_from_user(actor),
                "target": {"id": str(target_id), "domain": target_domain},
            },
        )
        await queue_event(
            session,
            settings,
            target_domain,
            envelope,
            discover_destination=False,
        )
        destinations.add(target_domain)

    guilds = (
        await session.execute(
            select(Guild.id, Guild.origin_domain)
            .join(
                GuildMember,
                (GuildMember.guild_id == Guild.id)
                & (GuildMember.guild_domain == Guild.origin_domain),
            )
            .where(
                GuildMember.user_id == actor.id,
                GuildMember.user_domain == actor.origin_domain,
                Guild.origin_domain != settings.domain,
            )
            .order_by(Guild.origin_domain, Guild.id)
        )
    ).all()
    for guild_id, guild_domain in guilds:
        envelope = await build_envelope(
            session,
            settings,
            "guild.member.profile",
            actor,
            {"actor": profile_from_user(actor)},
            context={"guild_id": str(guild_id), "guild_domain": guild_domain},
        )
        await queue_event(
            session,
            settings,
            guild_domain,
            envelope,
            discover_destination=False,
        )
        destinations.add(guild_domain)

    local_memberships = (
        await session.execute(
            select(Guild, GuildMember)
            .join(
                GuildMember,
                (GuildMember.guild_id == Guild.id)
                & (GuildMember.guild_domain == Guild.origin_domain),
            )
            .where(
                GuildMember.user_id == actor.id,
                GuildMember.user_domain == actor.origin_domain,
                Guild.origin_domain == settings.domain,
                Guild.unavailable.is_(False),
            )
            .order_by(Guild.id)
        )
    ).all()
    for guild, member in local_memberships:
        source = await build_envelope(
            session,
            settings,
            "guild.member.profile",
            actor,
            {"actor": profile_from_user(actor)},
            context={"guild_id": str(guild.id), "guild_domain": guild.origin_domain},
        )
        remote_destinations = set(
            await session.scalars(
                select(distinct(GuildMember.user_domain)).where(
                    GuildMember.guild_id == guild.id,
                    GuildMember.guild_domain == guild.origin_domain,
                    GuildMember.user_domain != settings.domain,
                )
            )
        )
        for destination in sorted(remote_destinations):
            await queue_event(
                session,
                settings,
                destination,
                source,
                discover_destination=False,
            )
        destinations.update(remote_destinations)
        role_ids = list(
            await session.scalars(
                select(MemberRole.role_id)
                .where(
                    MemberRole.guild_id == guild.id,
                    MemberRole.guild_domain == guild.origin_domain,
                    MemberRole.user_id == actor.id,
                    MemberRole.user_domain == actor.origin_domain,
                )
                .order_by(MemberRole.role_id)
            )
        )
        queue_postcommit_dispatch(
            session,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_MEMBER_UPDATE",
            member_payload(member, actor, role_ids),
        )
    return destinations


async def validated_guild_profile_source(
    session: AsyncSession,
    settings: Settings,
    raw_source: object,
    *,
    guild_ref: tuple[int, str],
) -> RemoteUserProfile:
    """Verify the preserved user-home signature inside an authority relay."""

    try:
        preliminary = EventEnvelope.model_validate(raw_source)
    except ValueError as exc:
        raise ValueError("guild profile relay source is invalid") from exc
    source = await validated_event_envelope(
        session,
        settings,
        preliminary.origin,
        raw_source,
    )
    if (
        source.type != "guild.member.profile"
        or set(source.context) != {"guild_id", "guild_domain"}
        or set(source.content) != {"actor"}
        or source.context.get("guild_id") != str(guild_ref[0])
        or source.context.get("guild_domain") != guild_ref[1]
    ):
        raise ValueError("guild profile relay source scope is invalid")
    profile = RemoteUserProfile.model_validate(source.content["actor"])
    if source.origin != profile.origin_domain or (source.actor.id, source.actor.domain) != (
        profile.id,
        profile.origin_domain,
    ):
        raise ValueError("guild profile relay source actor is invalid")
    return profile


async def guild_profile_member_payload(
    session: AsyncSession,
    guild: Guild,
    user: User,
) -> dict[str, object]:
    member = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, user.id, user.origin_domain),
    )
    if member is None:
        raise ValueError("guild profile update references a non-member")
    role_ids = list(
        await session.scalars(
            select(MemberRole.role_id)
            .where(
                MemberRole.guild_id == guild.id,
                MemberRole.guild_domain == guild.origin_domain,
                MemberRole.user_id == user.id,
                MemberRole.user_domain == user.origin_domain,
            )
            .order_by(MemberRole.role_id)
        )
    )
    return member_payload(member, user, role_ids)


async def apply_relationship_event(
    session: AsyncSession,
    settings: Settings,
    envelope: EventEnvelope,
) -> RelationshipApplication:
    content = RelationshipEventContent.model_validate(envelope.content)
    if (
        content.actor.id != envelope.actor.id
        or content.actor.origin_domain != envelope.actor.domain
    ):
        raise ValueError("relationship actor profile does not match the envelope actor")
    if content.target.domain != settings.domain:
        raise ValueError("relationship event target is not local")
    recipient = await session.get(User, (int(content.target.id), settings.domain))
    if recipient is None or not recipient.is_local:
        raise ValueError("relationship event target does not exist")

    if envelope.type == "relationship.profile":
        actor = await session.get(User, (int(content.actor.id), content.actor.origin_domain))
        if actor is None or actor.is_local:
            raise ValueError("relationship profile references an unknown remote user")
        await lock_relationship_pair(session, recipient, actor)
        current = await relationship(session, recipient, actor)
        if current is None or current.type != "friend":
            # A delayed event cannot restore profile state after friendship ends.
            return RelationshipApplication(recipient, actor)
        actor = await upsert_remote_user(session, settings, content.actor)
        return RelationshipApplication(recipient, actor, "friend")

    if envelope.type == "relationship.request":
        await admit_pending_relationship_request(
            session,
            settings,
            recipient,
            actor_id=database_snowflake(content.actor.id, "relationship actor id"),
            actor_domain=content.actor.origin_domain,
        )
    actor = await upsert_remote_user(session, settings, content.actor)
    if (actor.id, actor.origin_domain) == (recipient.id, recipient.origin_domain):
        raise ValueError("a user cannot create a relationship with itself")
    await lock_relationship_pair(session, recipient, actor)
    current = await relationship(session, recipient, actor)

    if envelope.type == "relationship.request":
        if content.request_id is None:
            raise ValueError("relationship request is missing its correlation ID")
        if current is not None and current.type == "blocked":
            # A block is intentionally not disclosed to the remote actor.
            return RelationshipApplication(recipient, actor)
        if current is None:
            current = Relationship(
                user_id=recipient.id,
                user_domain=recipient.origin_domain,
                user_is_local=True,
                target_id=actor.id,
                target_domain=actor.origin_domain,
                type="pending_in",
                request_id=content.request_id,
            )
            session.add(current)
            return RelationshipApplication(recipient, actor, "pending_in")
        if current.type == "pending_in":
            current.request_id = content.request_id
            return RelationshipApplication(recipient, actor, "pending_in")
        if current.type in {"pending_out", "friend"}:
            current.type = "friend"
            current.request_id = None
            accepted = await build_envelope(
                session,
                settings,
                "relationship.accept",
                recipient,
                relationship_event_content(recipient, actor, content.request_id),
            )
            await queue_event(
                session,
                settings,
                actor.origin_domain,
                accepted,
                discover_destination=False,
            )
            return RelationshipApplication(recipient, actor, "friend", actor.origin_domain)
        raise ValueError("unsupported relationship request transition")

    if envelope.type == "relationship.accept":
        if content.request_id is None:
            raise ValueError("relationship acceptance is missing its correlation ID")
        # Accept only the exact outstanding request. A late or forged acceptance
        # cannot resurrect a request that the local user cancelled or blocked.
        if current is not None and acceptance_matches(
            current.type,
            current.request_id,
            content.request_id,
        ):
            current.type = "friend"
            current.request_id = None
            return RelationshipApplication(recipient, actor, "friend")
        return RelationshipApplication(recipient, actor)

    if envelope.type == "relationship.remove":
        # The local block is authoritative and private. Remote removals may clear
        # friendship state but can never weaken it.
        if current is not None and current.type != "blocked":
            await session.delete(current)
            return RelationshipApplication(recipient, actor, "none")
        return RelationshipApplication(recipient, actor)

    raise ValueError("unsupported relationship event type")
