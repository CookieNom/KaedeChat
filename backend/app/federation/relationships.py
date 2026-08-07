from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.privacy import lock_relationship_pair, relationship
from app.core.settings import Settings
from app.db.models import Relationship, User
from app.federation.events import build_envelope, queue_event
from app.federation.replication import profile_from_user, upsert_remote_user
from app.federation.schemas import EventEnvelope, RelationshipEventContent


@dataclass(slots=True)
class RelationshipApplication:
    recipient: User
    actor: User
    relation_type: str | None = None
    wake_destination: str | None = None


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


async def queue_friend_profile_updates(
    session: AsyncSession,
    settings: Settings,
    actor: User,
) -> set[str]:
    """Queue an authoritative profile update for every accepted remote friend."""

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
    return destinations


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
