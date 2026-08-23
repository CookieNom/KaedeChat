from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Response, status
from redis.asyncio import Redis
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthenticatedUser, get_redis, get_session, require_user
from app.chat.events import publish_dispatch, user_topic
from app.chat.payloads import relationship_payload, user_payload
from app.chat.privacy import blocked_between, lock_relationship_pair, relationship
from app.chat.schemas import RelationshipRequest
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import Settings, get_settings
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef, EntityReferenceLike
from app.db.models import Relationship, User
from app.federation.events import build_envelope, queue_event
from app.federation.relationships import relationship_event_content
from app.federation.users import resolve_handle
from app.tasks import federation_deliver, mobile_push_activity

router = APIRouter(prefix="/api/v1/users/@me/relationships", tags=["relationships"])


async def target_by_id(
    session: AsyncSession, settings: Settings, user_ref: EntityReferenceLike
) -> User:
    user_id, user_domain = user_ref.resolve(settings.domain)
    target = await session.get(User, (user_id, user_domain))
    if target is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    return target


def relationship_row(
    owner: User,
    target: User,
    relation_type: str,
    request_id: str | None = None,
) -> Relationship:
    return Relationship(
        user_id=owner.id,
        user_domain=owner.origin_domain,
        user_is_local=True,
        target_id=target.id,
        target_domain=target.origin_domain,
        type=relation_type,
        request_id=request_id,
    )


async def notify_relationship(redis: Redis, owner: User, target: User, relation_type: str) -> None:
    await publish_dispatch(
        redis,
        user_topic(owner.origin_domain, owner.id),
        "USER_UPDATE",
        {"relationship": {"type": relation_type, "user": user_payload(target)}},
    )
    if relation_type in {"pending_in", "friend"}:
        name = target.display_name or target.username
        await enqueue_best_effort(
            mobile_push_activity,
            owner.id,
            owner.origin_domain,
            secrets.randbits(63),
            owner.origin_domain,
            "relationship",
            "New friend request" if relation_type == "pending_in" else "Friend request accepted",
            (
                f"{name} sent you a friend request."
                if relation_type == "pending_in"
                else f"You and {name} are now friends."
            ),
            f"relationship:{target.id}@{target.origin_domain}:{relation_type}",
        )


async def queue_relationship_event(
    session: AsyncSession,
    settings: Settings,
    event_type: str,
    actor: User,
    target: User,
    request_id: str | None = None,
) -> None:
    envelope = await build_envelope(
        session,
        settings,
        event_type,
        actor,
        relationship_event_content(actor, target, request_id),
    )
    await queue_event(
        session,
        settings,
        target.origin_domain,
        envelope,
        discover_destination=False,
    )


async def reconcile_local_relationship_race(
    session: AsyncSession, owner: User, target: User
) -> Relationship:
    outgoing = await relationship(session, owner, target)
    incoming = await relationship(session, target, owner)
    if outgoing is None:
        raise HTTPException(status_code=409, detail={"code": "RELATIONSHIP_CONFLICT"})
    if outgoing.type == "pending_in" and incoming is not None and incoming.type == "pending_out":
        outgoing.type = "friend"
        outgoing.request_id = None
        incoming.type = "friend"
        incoming.request_id = None
        await session.commit()
    return outgoing


@router.get("")
async def list_relationships(
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    rows = (
        await session.execute(
            select(Relationship, User)
            .join(
                User,
                (User.id == Relationship.target_id)
                & (User.origin_domain == Relationship.target_domain),
            )
            .where(
                Relationship.user_id == auth.user.id,
                Relationship.user_domain == auth.user.origin_domain,
            )
            .order_by(Relationship.type, func.lower(User.username))
        )
    ).all()
    return [relationship_payload(item, target) for item, target in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def request_friendship(
    payload: RelationshipRequest,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["friend_request"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    requester_key = f"{auth.user.origin_domain}:{auth.user.id}"
    target = await resolve_handle(session, settings, redis, requester_key, payload.handle)
    if (target.id, target.origin_domain) == (auth.user.id, auth.user.origin_domain):
        raise HTTPException(status_code=400, detail={"code": "CANNOT_FRIEND_SELF"})
    await lock_relationship_pair(session, auth.user, target)
    if await blocked_between(session, auth.user, target):
        raise HTTPException(status_code=403, detail={"code": "RELATIONSHIP_BLOCKED"})
    outgoing = await relationship(session, auth.user, target)

    if target.origin_domain != settings.domain:
        if outgoing is not None and outgoing.type in {"pending_out", "friend"}:
            return relationship_payload(outgoing, target)
        if outgoing is not None and outgoing.type == "pending_in":
            request_id = outgoing.request_id
            if request_id is None:
                raise HTTPException(status_code=409, detail={"code": "RELATIONSHIP_CONFLICT"})
            outgoing.type = "friend"
            outgoing.request_id = None
            await queue_relationship_event(
                session, settings, "relationship.accept", auth.user, target, request_id
            )
            relation_type = "friend"
        elif outgoing is None:
            request_id = f"kcr_{secrets.token_urlsafe(24)}"
            outgoing = relationship_row(auth.user, target, "pending_out", request_id)
            session.add(outgoing)
            await queue_relationship_event(
                session, settings, "relationship.request", auth.user, target, request_id
            )
            relation_type = "pending_out"
        else:
            raise HTTPException(status_code=409, detail={"code": "RELATIONSHIP_CONFLICT"})
        await session.commit()
        await session.refresh(outgoing)
        await enqueue_best_effort(federation_deliver, target.origin_domain)
        await notify_relationship(redis, auth.user, target, relation_type)
        return relationship_payload(outgoing, target)

    incoming = await relationship(session, target, auth.user)
    if outgoing is not None:
        if outgoing.type in {"pending_out", "friend"}:
            return relationship_payload(outgoing, target)
        if (
            outgoing.type == "pending_in"
            and incoming is not None
            and incoming.type == "pending_out"
        ):
            outgoing.type = "friend"
            outgoing.request_id = None
            incoming.type = "friend"
            incoming.request_id = None
            await session.commit()
            await session.refresh(outgoing)
            await notify_relationship(redis, auth.user, target, "friend")
            await notify_relationship(redis, target, auth.user, "friend")
            return relationship_payload(outgoing, target)
        raise HTTPException(status_code=409, detail={"code": "RELATIONSHIP_CONFLICT"})
    request_id = f"kcr_{secrets.token_urlsafe(24)}"
    session.add(relationship_row(auth.user, target, "pending_out", request_id))
    session.add(relationship_row(target, auth.user, "pending_in", request_id))
    relation_type = "pending_out"
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        current = await reconcile_local_relationship_race(session, auth.user, target)
        relation_type = current.type
    await notify_relationship(redis, auth.user, target, relation_type)
    await notify_relationship(
        redis, target, auth.user, "friend" if relation_type == "friend" else "pending_in"
    )
    persisted = await relationship(session, auth.user, target)
    if persisted is None:
        raise RuntimeError("relationship transaction did not persist")
    return relationship_payload(persisted, target)


@router.put("/{user_id}")
async def accept_friendship(
    user_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    target = await target_by_id(session, settings, user_id)
    await lock_relationship_pair(session, auth.user, target)
    incoming = await relationship(session, auth.user, target)
    if incoming is None or incoming.type != "pending_in":
        raise HTTPException(status_code=404, detail={"code": "FRIEND_REQUEST_NOT_FOUND"})
    if target.origin_domain != settings.domain:
        request_id = incoming.request_id
        if request_id is None:
            raise HTTPException(status_code=409, detail={"code": "RELATIONSHIP_CONFLICT"})
        incoming.type = "friend"
        incoming.request_id = None
        await queue_relationship_event(
            session, settings, "relationship.accept", auth.user, target, request_id
        )
        await session.commit()
        await session.refresh(incoming)
        await enqueue_best_effort(federation_deliver, target.origin_domain)
        await notify_relationship(redis, auth.user, target, "friend")
        return relationship_payload(incoming, target)
    outgoing = await relationship(session, target, auth.user)
    if outgoing is None or outgoing.type != "pending_out":
        raise HTTPException(status_code=404, detail={"code": "FRIEND_REQUEST_NOT_FOUND"})
    incoming.type = "friend"
    incoming.request_id = None
    outgoing.type = "friend"
    outgoing.request_id = None
    await session.commit()
    await session.refresh(incoming)
    await notify_relationship(redis, auth.user, target, "friend")
    await notify_relationship(redis, target, auth.user, "friend")
    return relationship_payload(incoming, target)


@router.delete("/{user_id}", status_code=204)
async def remove_relationship(
    user_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    target = await target_by_id(session, settings, user_id)
    await lock_relationship_pair(session, auth.user, target)
    actor_relation = await relationship(session, auth.user, target)
    actor_keeps_block = actor_relation is not None and actor_relation.type == "blocked"
    if target.origin_domain != settings.domain:
        await session.execute(
            delete(Relationship).where(
                Relationship.user_id == auth.user.id,
                Relationship.user_domain == auth.user.origin_domain,
                Relationship.target_id == target.id,
                Relationship.target_domain == target.origin_domain,
                Relationship.type != "blocked",
            )
        )
        await queue_relationship_event(session, settings, "relationship.remove", auth.user, target)
        await session.commit()
        await enqueue_best_effort(federation_deliver, target.origin_domain)
        await notify_relationship(
            redis, auth.user, target, "blocked" if actor_keeps_block else "none"
        )
        return Response(status_code=204)
    target_relation = await relationship(session, target, auth.user)
    target_keeps_block = target_relation is not None and target_relation.type == "blocked"
    await session.execute(
        delete(Relationship).where(
            or_(
                (Relationship.user_id == auth.user.id)
                & (Relationship.user_domain == auth.user.origin_domain)
                & (Relationship.target_id == target.id)
                & (Relationship.target_domain == target.origin_domain)
                & (Relationship.type != "blocked"),
                (Relationship.user_id == target.id)
                & (Relationship.user_domain == target.origin_domain)
                & (Relationship.target_id == auth.user.id)
                & (Relationship.target_domain == auth.user.origin_domain)
                & (Relationship.type != "blocked"),
            )
        )
    )
    await session.commit()
    await notify_relationship(redis, auth.user, target, "blocked" if actor_keeps_block else "none")
    await notify_relationship(redis, target, auth.user, "blocked" if target_keeps_block else "none")
    return Response(status_code=204)


@router.put("/{user_id}/block", status_code=204)
async def block_user(
    user_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    target = await target_by_id(session, settings, user_id)
    if (target.id, target.origin_domain) == (auth.user.id, auth.user.origin_domain):
        raise HTTPException(status_code=400, detail={"code": "CANNOT_BLOCK_SELF"})
    await lock_relationship_pair(session, auth.user, target)
    target_relation = (
        await relationship(session, target, auth.user)
        if target.origin_domain == settings.domain
        else None
    )
    target_keeps_block = target_relation is not None and target_relation.type == "blocked"
    await session.execute(
        delete(Relationship).where(
            or_(
                (Relationship.user_id == auth.user.id)
                & (Relationship.user_domain == auth.user.origin_domain)
                & (Relationship.target_id == target.id)
                & (Relationship.target_domain == target.origin_domain),
                (Relationship.user_id == target.id)
                & (Relationship.user_domain == target.origin_domain)
                & (Relationship.target_id == auth.user.id)
                & (Relationship.target_domain == auth.user.origin_domain)
                & (Relationship.type != "blocked"),
            )
        )
    )
    await session.execute(
        pg_insert(Relationship)
        .values(
            user_id=auth.user.id,
            user_domain=auth.user.origin_domain,
            user_is_local=True,
            target_id=target.id,
            target_domain=target.origin_domain,
            type="blocked",
            request_id=None,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "user_domain", "target_id", "target_domain"],
            set_={"type": "blocked", "request_id": None, "updated_at": func.now()},
        )
    )
    if target.origin_domain != settings.domain:
        # Send only a generic removal; never disclose that the local user blocked the peer.
        await queue_relationship_event(session, settings, "relationship.remove", auth.user, target)
    await session.commit()
    if target.origin_domain != settings.domain:
        await enqueue_best_effort(federation_deliver, target.origin_domain)
    await notify_relationship(redis, auth.user, target, "blocked")
    if target.origin_domain == settings.domain:
        await notify_relationship(
            redis, target, auth.user, "blocked" if target_keeps_block else "none"
        )
    return Response(status_code=204)


@router.delete("/{user_id}/block", status_code=204)
async def unblock_user(
    user_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    target = await target_by_id(session, settings, user_id)
    await lock_relationship_pair(session, auth.user, target)
    await session.execute(
        delete(Relationship).where(
            Relationship.user_id == auth.user.id,
            Relationship.user_domain == auth.user.origin_domain,
            Relationship.target_id == target.id,
            Relationship.target_domain == target.origin_domain,
            Relationship.type == "blocked",
        )
    )
    await session.commit()
    await notify_relationship(redis, auth.user, target, "none")
    return Response(status_code=204)
