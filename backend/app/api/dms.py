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
from app.chat.group_conversations import (
    apply_authoritative_group_mutation,
    group_conversation_content,
    load_authoritative_group,
    require_group_invite_friend,
    require_group_member,
)
from app.chat.payloads import dm_channel_payload
from app.chat.privacy import blocked_between, require_can_direct_message
from app.chat.schemas import DMGroupCreate, DMGroupMemberAdd, DMGroupUpdate, DMOpenRequest
from app.core.dm import (
    MAX_GROUP_DM_PARTICIPANTS,
    dm_authority_domain,
    dm_pair_key,
    group_dm_key,
)
from app.core.errors import parse_upstream_error
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef
from app.db.models import Channel, DMConversation, DMParticipant, User
from app.federation.client import signed_request
from app.federation.dm_storage import (
    FederatedDMQuotaExceeded,
    admit_federated_dm_conversation,
    dm_authority_history_available,
    dm_history_metadata,
    register_federated_dm_conversation,
)
from app.federation.events import build_envelope, queue_event
from app.federation.network import (
    FederationNetworkError,
    decode_federation_response_json,
)
from app.federation.replication import profile_from_user, replicate_conversation
from app.federation.schemas import RemoteUserProfile
from app.federation.users import resolve_handle
from app.tasks import federation_deliver

router = APIRouter(prefix="/api/v1/users/@me/channels", tags=["direct messages"])


async def authorize_group_invitee(
    session: AsyncSession,
    settings: Settings,
    inviter: User,
    invitee: User,
) -> None:
    await require_group_invite_friend(session, inviter, invitee)
    if invitee.origin_domain == settings.domain:
        return
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            invitee.origin_domain,
            "/_kaede/v1/dm/groups/authorize",
            payload={
                "inviter": profile_from_user(inviter),
                "invitee": profile_from_user(invitee),
            },
            request_timeout=8,
            max_response_bytes=16 * 1024,
        )
    except FederationNetworkError as exc:
        raise HTTPException(
            status_code=503, detail={"code": "GROUP_DM_INVITEE_HOME_UNREACHABLE"}
        ) from exc
    if response.status_code != 204:
        raise HTTPException(
            status_code=403 if response.status_code == 403 else 502,
            detail={
                "code": (
                    "GROUP_DM_INVITE_NOT_FRIEND"
                    if response.status_code == 403
                    else "GROUP_DM_INVITEE_HOME_REJECTED"
                )
            },
        )


async def queue_group_state(
    session: AsyncSession,
    settings: Settings,
    actor: User,
    conversation: DMConversation,
    channel: Channel,
    participants: list[User],
    *,
    extra_domains: set[str] | None = None,
    deleted: bool = False,
) -> set[str]:
    envelope = await build_envelope(
        session,
        settings,
        "dm.group.state",
        actor,
        group_conversation_content(conversation, channel, participants, deleted=deleted),
    )
    destinations = {
        participant.origin_domain
        for participant in participants
        if participant.origin_domain != settings.domain
    } | (extra_domains or set())
    destinations.discard(settings.domain)
    for destination in destinations:
        await queue_event(session, settings, destination, envelope)
    return destinations


async def publish_local_group_state(
    redis: Redis,
    session: AsyncSession,
    settings: Settings,
    conversation: DMConversation,
    channel: Channel,
    before: list[User],
    after: list[User],
    *,
    created: bool = False,
) -> None:
    before_refs = {(user.id, user.origin_domain) for user in before}
    after_refs = {(user.id, user.origin_domain) for user in after}
    for user in after:
        if user.origin_domain != settings.domain or not user.is_local:
            continue
        event = (
            "CHANNEL_CREATE"
            if created or (user.id, user.origin_domain) not in before_refs
            else "CHANNEL_UPDATE"
        )
        await publish_dispatch(
            redis,
            user_topic(settings.domain, user.id),
            event,
            await rendered_dm_channel(session, settings, channel, user, after),
        )
    for user in before:
        if (
            user.origin_domain == settings.domain
            and user.is_local
            and (user.id, user.origin_domain) not in after_refs
        ):
            await publish_dispatch(
                redis,
                user_topic(settings.domain, user.id),
                "CHANNEL_DELETE",
                {"id": str(channel.id), "origin_domain": channel.origin_domain},
            )


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


async def rendered_dm_channel(
    session: AsyncSession,
    settings: Settings,
    channel: Channel,
    actor: User,
    participants: list[User] | None = None,
) -> dict[str, object]:
    if participants is None:
        participants = await conversation_participants(session, channel.id, channel.origin_domain)
    conversation = await session.get(
        DMConversation,
        (channel.id, channel.origin_domain),
    )
    return dm_channel_payload(
        channel,
        recipients_for(actor, participants),
        conversation=conversation,
        history=dm_history_metadata(
            conversation,
            local_domain=settings.domain,
            remote_available=await dm_authority_history_available(
                session, conversation, local_domain=settings.domain
            ),
        ),
    )


@router.get("")
async def list_direct_messages(
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
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
        result.append(
            await rendered_dm_channel(session, settings, channel, auth.user, participants)
        )
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
        if response.status_code == 507:
            try:
                error_body = decode_federation_response_json(response)
            except FederationNetworkError:
                error_body = None
            raise HTTPException(
                status_code=507,
                detail=parse_upstream_error(error_body, "FEDERATED_DM_STORAGE_QUOTA_EXCEEDED"),
            )
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
            response_payload = decode_federation_response_json(response)
            if not isinstance(response_payload, dict):
                raise ValueError("DM authority response must be an object")
            response_profiles = [
                RemoteUserProfile.model_validate(item) for item in response_payload["participants"]
            ]
        except (FederationNetworkError, KeyError, TypeError, ValueError):
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
        except FederatedDMQuotaExceeded as exc:
            raise HTTPException(status_code=507, detail=exc.detail()) from exc
        except (KeyError, TypeError, ValueError, RuntimeError):
            raise HTTPException(
                status_code=502, detail={"code": "FEDERATION_DM_RESPONSE_INVALID"}
            ) from None
        await session.commit()
        participants = await conversation_participants(session, channel.id, channel.origin_domain)
        result = await rendered_dm_channel(session, settings, channel, auth.user, participants)
        await publish_dispatch(
            redis,
            user_topic(settings.domain, auth.user.id),
            "CHANNEL_CREATE",
            result,
        )
        return result
    conversation_id = await snowflake.mint()
    participant_domains = {auth.user.origin_domain, target.origin_domain}
    try:
        federated = await admit_federated_dm_conversation(
            session,
            settings,
            authority_domain=authority,
            pair_key=pair_key,
            participant_domains=participant_domains,
        )
    except FederatedDMQuotaExceeded as exc:
        raise HTTPException(status_code=507, detail=exc.detail()) from exc
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
        conversation = await session.get(
            DMConversation,
            (conversation_id, settings.domain),
        )
        if conversation is None:
            raise RuntimeError("new direct-message conversation disappeared")
        if federated:
            await register_federated_dm_conversation(
                session,
                settings,
                conversation,
                participant_domains=participant_domains,
            )
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
        if federated:
            await register_federated_dm_conversation(
                session,
                settings,
                conversation,
                participant_domains=participant_domains,
            )
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
    result = await rendered_dm_channel(session, settings, channel, auth.user, participants)
    if created:
        for participant in participants:
            if participant.origin_domain != settings.domain:
                continue
            await publish_dispatch(
                redis,
                user_topic(participant.origin_domain, participant.id),
                "CHANNEL_CREATE",
                await rendered_dm_channel(session, settings, channel, participant, participants),
            )
        if target.origin_domain != settings.domain:
            await enqueue_best_effort(federation_deliver, target.origin_domain)
    return result


@router.post("/group", status_code=status.HTTP_201_CREATED)
async def create_group_direct_message(
    payload: DMGroupCreate,
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
        CLIENT_RATE_LIMITS["dm_group_create"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    requester_key = f"{auth.user.origin_domain}:{auth.user.id}"
    targets: list[User] = []
    seen = {(auth.user.id, auth.user.origin_domain)}
    for handle in payload.handles:
        target = await resolve_handle(session, settings, redis, requester_key, handle)
        ref = (target.id, target.origin_domain)
        if ref in seen:
            raise HTTPException(status_code=400, detail={"code": "GROUP_DM_DUPLICATE_MEMBER"})
        seen.add(ref)
        await authorize_group_invitee(session, settings, auth.user, target)
        targets.append(target)
    if len(targets) + 1 > MAX_GROUP_DM_PARTICIPANTS:
        raise HTTPException(status_code=409, detail={"code": "GROUP_DM_FULL"})
    conversation_id = await snowflake.mint()
    lookup_key = group_dm_key(settings.domain, conversation_id)
    participant_domains = {auth.user.origin_domain, *(item.origin_domain for item in targets)}
    try:
        federated = await admit_federated_dm_conversation(
            session,
            settings,
            authority_domain=settings.domain,
            pair_key=lookup_key,
            participant_domains=participant_domains,
        )
    except FederatedDMQuotaExceeded as exc:
        raise HTTPException(status_code=507, detail=exc.detail()) from exc
    conversation = DMConversation(
        id=conversation_id,
        origin_domain=settings.domain,
        pair_key=lookup_key,
        type="group",
        authority_domain=settings.domain,
        owner_id=auth.user.id,
        owner_domain=auth.user.origin_domain,
        state_version=1,
    )
    channel = Channel(
        id=conversation_id,
        origin_domain=settings.domain,
        guild_id=None,
        guild_domain=None,
        type=1,
        name=payload.name,
        topic=None,
        position=0,
        parent_id=None,
        parent_domain=None,
        rate_limit_per_user=0,
        created_floor_id=conversation_id,
    )
    session.add_all([conversation, channel])
    await session.flush()
    participants = [auth.user, *targets]
    session.add_all(
        [
            DMParticipant(
                conversation_id=conversation_id,
                conversation_domain=settings.domain,
                user_id=user.id,
                user_domain=user.origin_domain,
            )
            for user in participants
        ]
    )
    if federated:
        await register_federated_dm_conversation(
            session,
            settings,
            conversation,
            participant_domains=participant_domains,
        )
    destinations = await queue_group_state(
        session, settings, auth.user, conversation, channel, participants
    )
    await session.commit()
    await publish_local_group_state(
        redis, session, settings, conversation, channel, [], participants, created=True
    )
    for destination in destinations:
        await enqueue_best_effort(federation_deliver, destination)
    return await rendered_dm_channel(session, settings, channel, auth.user, participants)


async def proxy_group_mutation(
    session: AsyncSession,
    settings: Settings,
    conversation: DMConversation,
    actor: User,
    *,
    action: str,
    target: User | None = None,
    name: str | None = None,
) -> dict[str, object]:
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            conversation.authority_domain,
            "/_kaede/v1/dm/groups/mutate",
            payload={
                "action": action,
                "conversation_id": str(conversation.id),
                "conversation_domain": conversation.origin_domain,
                "actor": profile_from_user(actor),
                "target": profile_from_user(target) if target is not None else None,
                "name": name,
            },
            request_timeout=10,
            max_response_bytes=256 * 1024,
        )
    except FederationNetworkError as exc:
        raise HTTPException(status_code=503, detail={"code": "GROUP_DM_HOME_UNREACHABLE"}) from exc
    if response.status_code != 200:
        try:
            error = decode_federation_response_json(response)
        except FederationNetworkError:
            error = None
        raise HTTPException(
            status_code=response.status_code,
            detail=parse_upstream_error(error, "GROUP_DM_MUTATION_REJECTED"),
        )
    try:
        decoded = decode_federation_response_json(response)
    except FederationNetworkError as exc:
        raise HTTPException(
            status_code=502, detail={"code": "GROUP_DM_HOME_INVALID_RESPONSE"}
        ) from exc
    if not isinstance(decoded, dict):
        raise HTTPException(status_code=502, detail={"code": "GROUP_DM_HOME_INVALID_RESPONSE"})
    return decoded


async def apply_group_mutation_response(
    session: AsyncSession,
    settings: Settings,
    payload: dict[str, object],
) -> tuple[Channel, DMConversation, list[User]]:
    raw_conversation = payload.get("conversation")
    raw_participants = payload.get("participants")
    if not isinstance(raw_conversation, dict) or not isinstance(raw_participants, list):
        raise HTTPException(status_code=502, detail={"code": "GROUP_DM_HOME_INVALID_RESPONSE"})
    try:
        profiles = [RemoteUserProfile.model_validate(item) for item in raw_participants]
        channel = await replicate_conversation(session, settings, raw_conversation, profiles)
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise HTTPException(
            status_code=502, detail={"code": "GROUP_DM_HOME_INVALID_RESPONSE"}
        ) from exc
    conversation = await session.get(DMConversation, (channel.id, channel.origin_domain))
    if conversation is None or conversation.type != "group":
        raise HTTPException(status_code=502, detail={"code": "GROUP_DM_HOME_INVALID_RESPONSE"})
    participants = await conversation_participants(session, channel.id, channel.origin_domain)
    return channel, conversation, participants


async def mutate_group(
    channel_ref: EntityRef,
    action: str,
    response_status: Response,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    target: User | None = None,
    name: str | None = None,
) -> dict[str, object]:
    await enforce_client_rate_limit(
        redis,
        response_status,
        CLIENT_RATE_LIMITS["dm_group_mutate"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    conversation_id, conversation_domain = channel_ref.resolve(settings.domain)
    existing = await session.get(DMConversation, (conversation_id, conversation_domain))
    if existing is None or existing.type != "group":
        raise HTTPException(status_code=404, detail={"code": "GROUP_DM_NOT_FOUND"})
    before = await conversation_participants(session, conversation_id, conversation_domain)
    if action == "add" and target is not None:
        await require_group_member(session, existing, auth.user)
        await authorize_group_invitee(session, settings, auth.user, target)
    if existing.authority_domain != settings.domain:
        remote = await proxy_group_mutation(
            session,
            settings,
            existing,
            auth.user,
            action=action,
            target=target,
            name=name,
        )
        remote_conversation = remote.get("conversation")
        remote_participants = remote.get("participants")
        if not isinstance(remote_participants, list):
            raise HTTPException(status_code=502, detail={"code": "GROUP_DM_HOME_INVALID_RESPONSE"})
        try:
            remote_refs = {
                (int(profile.id), profile.origin_domain)
                for profile in (
                    RemoteUserProfile.model_validate(item) for item in remote_participants
                )
            }
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=502, detail={"code": "GROUP_DM_HOME_INVALID_RESPONSE"}
            ) from exc
        before_refs = {(user.id, user.origin_domain) for user in before}
        expected_refs = set(before_refs)
        if action == "add" and target is not None:
            expected_refs.add((target.id, target.origin_domain))
        elif action == "leave":
            expected_refs.discard((auth.user.id, auth.user.origin_domain))
        elif action == "remove" and target is not None:
            expected_refs.discard((target.id, target.origin_domain))
        if remote_refs != expected_refs:
            raise HTTPException(status_code=502, detail={"code": "GROUP_DM_HOME_INVALID_RESPONSE"})
        remote_deleted = (
            bool(remote_conversation.get("deleted"))
            if isinstance(remote_conversation, dict)
            else False
        )
        if remote_deleted:
            if action != "leave" or expected_refs:
                raise HTTPException(
                    status_code=502, detail={"code": "GROUP_DM_HOME_INVALID_RESPONSE"}
                )
            membership = await session.get(
                DMParticipant,
                (conversation_id, conversation_domain, auth.user.id, auth.user.origin_domain),
            )
            if membership is not None:
                await session.delete(membership)
            await session.commit()
            await publish_dispatch(
                redis,
                user_topic(settings.domain, auth.user.id),
                "CHANNEL_DELETE",
                {"id": str(conversation_id), "origin_domain": conversation_domain},
            )
            return {"status": "left"}
        channel, conversation, participants = await apply_group_mutation_response(
            session, settings, remote
        )
        await session.commit()
        await publish_local_group_state(
            redis, session, settings, conversation, channel, before, participants
        )
        if action in {"leave", "remove"} and not any(
            (user.id, user.origin_domain) == (auth.user.id, auth.user.origin_domain)
            for user in participants
        ):
            return {"status": "left"}
        return await rendered_dm_channel(session, settings, channel, auth.user, participants)
    conversation, channel = await load_authoritative_group(
        session,
        settings,
        conversation_id,
        conversation_domain,
        for_update=True,
    )
    before, participants, deleted = await apply_authoritative_group_mutation(
        session,
        settings,
        conversation,
        channel,
        auth.user,
        action=action,
        target=target,
        name=name,
    )
    destinations = await queue_group_state(
        session,
        settings,
        auth.user,
        conversation,
        channel,
        participants,
        extra_domains={user.origin_domain for user in before},
        deleted=deleted,
    )
    await session.commit()
    await publish_local_group_state(
        redis, session, settings, conversation, channel, before, participants
    )
    for destination in destinations:
        await enqueue_best_effort(federation_deliver, destination)
    if deleted or not any(
        (user.id, user.origin_domain) == (auth.user.id, auth.user.origin_domain)
        for user in participants
    ):
        return {"status": "left"}
    return await rendered_dm_channel(session, settings, channel, auth.user, participants)


@router.patch("/{channel_ref}/group")
async def rename_group_direct_message(
    channel_ref: EntityRef,
    payload: DMGroupUpdate,
    response_status: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    return await mutate_group(
        channel_ref,
        "rename",
        response_status,
        auth,
        session,
        redis,
        settings,
        name=payload.name,
    )


@router.post("/{channel_ref}/group/recipients")
async def add_group_direct_message_member(
    channel_ref: EntityRef,
    payload: DMGroupMemberAdd,
    response_status: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    requester_key = f"{auth.user.origin_domain}:{auth.user.id}"
    target = await resolve_handle(session, settings, redis, requester_key, payload.handle)
    return await mutate_group(
        channel_ref,
        "add",
        response_status,
        auth,
        session,
        redis,
        settings,
        target=target,
    )


@router.post("/{channel_ref}/group/leave")
async def leave_group_direct_message(
    channel_ref: EntityRef,
    response_status: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    return await mutate_group(
        channel_ref,
        "leave",
        response_status,
        auth,
        session,
        redis,
        settings,
    )


@router.delete("/{channel_ref}/group/recipients/{user_ref}")
async def remove_group_direct_message_member(
    channel_ref: EntityRef,
    user_ref: EntityRef,
    response_status: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    target_id, target_domain = user_ref.resolve(settings.domain)
    target = await session.get(User, (target_id, target_domain))
    if target is None:
        raise HTTPException(status_code=404, detail={"code": "GROUP_DM_MEMBER_NOT_FOUND"})
    return await mutate_group(
        channel_ref,
        "remove",
        response_status,
        auth,
        session,
        redis,
        settings,
        target=target,
    )
