from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.chat.events import publish_dispatch, user_topic
from app.chat.payloads import dm_channel_payload
from app.chat.privacy import blocked_between, require_can_direct_message
from app.chat.schemas import DMOpenRequest
from app.core.dm import dm_authority_domain, dm_pair_key
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.db.models import Channel, DMConversation, DMParticipant, User
from app.federation.client import signed_request
from app.federation.events import build_envelope, queue_event
from app.federation.network import FederationNetworkError
from app.federation.replication import profile_from_user, replicate_conversation
from app.federation.schemas import RemoteUserProfile
from app.federation.users import resolve_handle
from app.tasks import federation_deliver

router = APIRouter(prefix="/api/v1/users/@me/channels", tags=["direct messages"])


async def conversation_participants(
    session: AsyncSession, conversation_id: int, conversation_domain: str
) -> list[User]:
    return list(
        await session.scalars(
            select(User)
            .join(
                DMParticipant,
                (DMParticipant.user_id == User.id)
                & (DMParticipant.user_domain == User.origin_domain),
            )
            .where(
                DMParticipant.conversation_id == conversation_id,
                DMParticipant.conversation_domain == conversation_domain,
            )
            .order_by(User.origin_domain, User.username)
        )
    )


def recipients_for(actor: User, participants: list[User]) -> list[User]:
    return [
        participant
        for participant in participants
        if (participant.id, participant.origin_domain) != (actor.id, actor.origin_domain)
    ]


@router.get("")
async def list_direct_messages(
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    channels = list(
        await session.scalars(
            select(Channel)
            .join(
                DMParticipant,
                (DMParticipant.conversation_id == Channel.id)
                & (DMParticipant.conversation_domain == Channel.origin_domain),
            )
            .where(
                DMParticipant.user_id == auth.user.id,
                DMParticipant.user_domain == auth.user.origin_domain,
                Channel.type == 1,
                Channel.guild_id.is_(None),
                Channel.unavailable.is_(False),
            )
            .order_by(Channel.updated_at.desc(), Channel.id.desc())
        )
    )
    result: list[dict[str, object]] = []
    for channel in channels:
        participants = await conversation_participants(session, channel.id, channel.origin_domain)
        result.append(dm_channel_payload(channel, recipients_for(auth.user, participants)))
    return result


@router.post("", status_code=status.HTTP_201_CREATED)
async def open_direct_message(
    payload: DMOpenRequest,
    response_status: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await enforce_client_rate_limit(
        redis,
        response_status,
        CLIENT_RATE_LIMITS["dm_open"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    requester_key = f"{auth.user.origin_domain}:{auth.user.id}"
    target = await resolve_handle(session, settings, redis, requester_key, payload.handle)
    if (target.id, target.origin_domain) == (auth.user.id, auth.user.origin_domain):
        raise HTTPException(status_code=400, detail={"code": "CANNOT_DM_SELF"})
    # A local block is authoritative even when the other participant and the
    # conversation authority are remote. Remote privacy is then confirmed by
    # the peer's authorization endpoint below.
    if await blocked_between(session, auth.user, target):
        raise HTTPException(status_code=403, detail={"code": "CANNOT_DM_USER"})
    if target.origin_domain == settings.domain:
        await require_can_direct_message(session, auth.user, target)
    first_handle = f"{auth.user.username}@{auth.user.origin_domain}"
    second_handle = f"{target.username}@{target.origin_domain}"
    pair_key = dm_pair_key(first_handle, second_handle)
    authority = dm_authority_domain(first_handle, second_handle)
    if authority == settings.domain and target.origin_domain != settings.domain:
        try:
            authorization = await signed_request(
                session,
                settings,
                "POST",
                target.origin_domain,
                "/_kaede/v1/dm/authorize",
                payload={"participants": [profile_from_user(user) for user in (auth.user, target)]},
            )
        except (httpx.HTTPError, FederationNetworkError, RuntimeError):
            raise HTTPException(
                status_code=503, detail={"code": "FEDERATION_DM_AUTHORIZATION_FAILED"}
            ) from None
        if authorization.status_code == 403:
            raise HTTPException(status_code=403, detail={"code": "CANNOT_DM_USER"})
        if authorization.status_code != 200:
            raise HTTPException(
                status_code=502, detail={"code": "FEDERATION_DM_AUTHORIZATION_FAILED"}
            )
    if authority != settings.domain:
        participant_payload = [profile_from_user(user) for user in (auth.user, target)]
        try:
            response = await signed_request(
                session,
                settings,
                "POST",
                authority,
                "/_kaede/v1/dm/open",
                payload={"participants": participant_payload},
            )
        except (httpx.HTTPError, FederationNetworkError, RuntimeError):
            request_envelope = await build_envelope(
                session,
                settings,
                "dm.open.request",
                auth.user,
                {"participants": participant_payload, "pair_key": pair_key},
            )
            await queue_event(session, settings, authority, request_envelope)
            await session.commit()
            await enqueue_best_effort(federation_deliver, authority)
            response_status.status_code = status.HTTP_202_ACCEPTED
            return {
                "status": "queued",
                "operation_id": str(request_envelope["event_id"]),
                "pair_key": pair_key,
            }
        if response.status_code == 403:
            raise HTTPException(status_code=403, detail={"code": "CANNOT_DM_USER"})
        if response.status_code == 429 or response.status_code >= 500:
            request_envelope = await build_envelope(
                session,
                settings,
                "dm.open.request",
                auth.user,
                {"participants": participant_payload, "pair_key": pair_key},
            )
            await queue_event(session, settings, authority, request_envelope)
            await session.commit()
            await enqueue_best_effort(federation_deliver, authority)
            response_status.status_code = status.HTTP_202_ACCEPTED
            return {
                "status": "queued",
                "operation_id": str(request_envelope["event_id"]),
                "pair_key": pair_key,
            }
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail={"code": "FEDERATION_DM_OPEN_FAILED"})
        try:
            response_payload = response.json()
            if not isinstance(response_payload, dict):
                raise ValueError("DM authority response must be an object")
            response_profiles = [
                RemoteUserProfile.model_validate(item) for item in response_payload["participants"]
            ]
        except (KeyError, TypeError, ValueError):
            raise HTTPException(
                status_code=502, detail={"code": "FEDERATION_DM_RESPONSE_INVALID"}
            ) from None
        expected_refs = {
            (auth.user.id, auth.user.origin_domain),
            (target.id, target.origin_domain),
        }
        if {(int(item.id), item.origin_domain) for item in response_profiles} != expected_refs:
            raise HTTPException(status_code=502, detail={"code": "FEDERATION_DM_IDENTITY_MISMATCH"})
        conversation_payload = response_payload.get("conversation")
        if not isinstance(conversation_payload, dict) or (
            conversation_payload.get("pair_key") != pair_key
            or conversation_payload.get("authority_domain") != authority
            or conversation_payload.get("origin_domain") != authority
        ):
            raise HTTPException(
                status_code=502, detail={"code": "FEDERATION_DM_AUTHORITY_MISMATCH"}
            )
        try:
            channel = await replicate_conversation(
                session,
                settings,
                conversation_payload,
                response_profiles,
            )
        except (KeyError, TypeError, ValueError, RuntimeError):
            raise HTTPException(
                status_code=502, detail={"code": "FEDERATION_DM_RESPONSE_INVALID"}
            ) from None
        await session.commit()
        participants = await conversation_participants(session, channel.id, channel.origin_domain)
        result = dm_channel_payload(channel, recipients_for(auth.user, participants))
        await publish_dispatch(
            redis,
            user_topic(settings.domain, auth.user.id),
            "CHANNEL_CREATE",
            result,
        )
        return result
    conversation_id = await snowflake.mint()
    inserted_id = await session.scalar(
        pg_insert(DMConversation)
        .values(
            id=conversation_id,
            origin_domain=settings.domain,
            pair_key=pair_key,
            type="direct",
            authority_domain=authority,
        )
        .on_conflict_do_nothing(index_elements=["pair_key"])
        .returning(DMConversation.id)
    )
    created = inserted_id is not None
    if created:
        channel = Channel(
            id=conversation_id,
            origin_domain=settings.domain,
            guild_id=None,
            guild_domain=None,
            type=1,
            name=None,
            topic=None,
            position=0,
            parent_id=None,
            parent_domain=None,
            rate_limit_per_user=0,
            created_floor_id=conversation_id,
        )
        session.add(channel)
        await session.flush()
        session.add_all(
            [
                DMParticipant(
                    conversation_id=conversation_id,
                    conversation_domain=settings.domain,
                    user_id=user.id,
                    user_domain=user.origin_domain,
                )
                for user in (auth.user, target)
            ]
        )
        if target.origin_domain != settings.domain:
            envelope = await build_envelope(
                session,
                settings,
                "dm.conversation.create",
                auth.user,
                {
                    "conversation": {
                        "id": str(conversation_id),
                        "origin_domain": settings.domain,
                        "pair_key": pair_key,
                        "authority_domain": authority,
                    },
                    "participants": [profile_from_user(user) for user in (auth.user, target)],
                },
            )
            await queue_event(session, settings, target.origin_domain, envelope)
        await session.commit()
    else:
        conversation = await session.scalar(
            select(DMConversation).where(DMConversation.pair_key == pair_key)
        )
        if conversation is None:
            raise RuntimeError("direct-message conflict did not resolve")
        conversation_id = conversation.id
        existing_channel = await session.scalar(
            select(Channel).where(
                Channel.id == conversation.id,
                Channel.origin_domain == conversation.origin_domain,
                Channel.unavailable.is_(False),
            )
        )
        if existing_channel is None:
            raise RuntimeError("direct-message channel is missing")
        channel = existing_channel
    participants = await conversation_participants(session, channel.id, channel.origin_domain)
    result = dm_channel_payload(channel, recipients_for(auth.user, participants))
    if created:
        for participant in participants:
            if participant.origin_domain != settings.domain:
                continue
            await publish_dispatch(
                redis,
                user_topic(participant.origin_domain, participant.id),
                "CHANNEL_CREATE",
                dm_channel_payload(channel, recipients_for(participant, participants)),
            )
        if target.origin_domain != settings.domain:
            await enqueue_best_effort(federation_deliver, target.origin_domain)
    return result
