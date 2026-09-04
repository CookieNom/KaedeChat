from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.auth.instance_restrictions import require_remote_user_creation_allowed
from app.bots.dm_capability import usable_dm_capability
from app.chat.channel_access import load_channel_access
from app.chat.events import publish_dispatch, publish_ephemeral, user_topic
from app.chat.payloads import public_user_display_name
from app.chat.privacy import blocked_between, require_can_direct_message
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef
from app.db.bot_models import BotDMCapability, BotWorker
from app.db.models import Channel, DMConversation, DMParticipant, User
from app.federation.client import signed_request
from app.federation.events import build_envelope, queue_event
from app.federation.network import (
    FederationNetworkError,
    decode_federation_response_json,
)
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
)
from app.voice.cleanup import delete_terminal_call_room
from app.voice.e2ee import bot_voice_lineage_metadata
from app.voice.livekit import LiveKitControl, LiveKitError, mint_join_token
from app.voice.rooms import dm_room_name, parse_participant_identity, participant_identity
from app.voice.schemas import (
    ActiveCallResponse,
    BotCallResponse,
    CallAction,
    CallCreate,
    CallFederationRequest,
    CallResponse,
    CallStateFederationRequest,
    DMVoiceBrokerRequest,
    DMVoiceSelfStateFederationRequest,
    DMVoiceStateFederationRequest,
    VoiceOccupantState,
    VoiceSelfStateFederationResponse,
    VoiceTokenRequest,
    VoiceTokenResponse,
)
from app.voice.service import (
    federated_voice_grant_matches,
    require_e2ee_voice_device,
    require_voice_enabled,
    update_authoritative_occupant_self_state,
    voice_e2ee_context,
)
from app.voice.state import (
    BOT_CAPABILITY_BINDINGS_FIELD,
    FederatedVoiceSession,
    Occupant,
    activate_federated_dm_voice_home_session,
    apply_authoritative_call,
    begin_federated_voice_home_session,
    call_bot_capability_bindings,
    claim_voice_connection,
    confirm_federated_voice_home_session,
    create_call,
    discard_all_federated_voice_home_sessions,
    discard_federated_voice_session,
    discard_pending_federated_voice_home_session,
    get_active_call,
    get_call,
    get_federated_voice_session,
    is_call_accepted,
    occupant_from_federation_state,
    occupant_in_room,
    public_occupant_state,
    release_voice_connection,
    remove_occupant,
    remove_occupant_connection,
    replace_occupancy,
    room_occupants,
    room_state_key,
    sync_federated_voice_session_generation,
    transition_call,
    voice_room_registry_key,
)

router = APIRouter(tags=["calls"])
log = structlog.get_logger()

CALL_IDENTITY_FIELDS = (
    "id",
    "channel_id",
    "channel_domain",
    "authority_domain",
    "room",
    "created_at",
    "caller",
    "participants",
)
CALL_PUBLIC_FIELDS = frozenset((*CALL_IDENTITY_FIELDS, "state", "ended_at"))


def call_response(record: dict[str, Any]) -> CallResponse:
    # Installation capabilities are an authority-only fence. They must remain
    # in the Redis call record, but never be disclosed in the public call
    # resource or copied to a participant home.
    return CallResponse.model_validate(
        {key: value for key, value in record.items() if key in CALL_PUBLIC_FIELDS}
    )


def capability_binding(capability: BotDMCapability) -> dict[str, object]:
    return {"grant_id": capability.grant_id, "revision": capability.revision}


def require_call_bot_capability(
    record: dict[str, Any],
    capability: BotDMCapability,
) -> None:
    identity = participant_identity(capability.bot_user_id, capability.bot_user_domain)
    if call_bot_capability_bindings(record).get(identity) != capability_binding(capability):
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_CALL_GRANT_MISMATCH"})


def bot_call_response(record: dict[str, Any], capability: BotDMCapability) -> BotCallResponse:
    """Project a call for one exact bot capability without exposing Redis internals."""

    require_call_bot_capability(record, capability)
    return BotCallResponse.model_validate(
        call_response(record).model_dump(mode="json")
        | {
            "bot_dm_capability_id": capability.grant_id,
            "bot_dm_capability_revision": str(capability.revision),
            "bot_installation_ref": (
                f"{capability.source_installation_id}@{capability.source_installation_domain}"
            ),
            "bot_installation_type": capability.source_kind,
        }
    )


async def active_bot_call_capability_bindings(
    session: AsyncSession,
    settings: Settings,
    channel: Channel,
    participant_identities: set[str],
    *,
    preferred: BotDMCapability | None = None,
    bot_participant_identities: set[str] | None = None,
) -> dict[str, dict[str, object]]:
    """Select exactly one current install proof for each bot in a DM call.

    A conversation may be reachable through multiple guild/user installs. A
    call deliberately pins one proof per bot identity so later transitions,
    media grants, and revocation cannot compose or fall back across grants.
    """

    eligible_identities = bot_participant_identities or set()
    if preferred is not None:
        eligible_identities.add(
            participant_identity(preferred.bot_user_id, preferred.bot_user_domain)
        )
    if not eligible_identities:
        return {}
    capabilities = list(
        await session.scalars(
            select(BotDMCapability)
            .where(
                BotDMCapability.conversation_id == channel.id,
                BotDMCapability.conversation_domain == channel.origin_domain,
                BotDMCapability.authority_domain == settings.domain,
                usable_dm_capability(at=datetime.now(UTC)),
            )
            .order_by(BotDMCapability.grant_id)
        )
    )
    if preferred is not None:
        capabilities = [preferred, *(item for item in capabilities if item.id != preferred.id)]
    bindings: dict[str, dict[str, object]] = {}
    for capability in capabilities:
        identity = participant_identity(capability.bot_user_id, capability.bot_user_domain)
        if identity in participant_identities and identity in eligible_identities:
            bindings.setdefault(identity, capability_binding(capability))
    return bindings


def same_call_identity(current: dict[str, Any], incoming: dict[str, Any]) -> bool:
    return all(current.get(field) == incoming.get(field) for field in CALL_IDENTITY_FIELDS)


def authoritative_call_record(
    current: dict[str, Any], response: CallResponse, action: str
) -> dict[str, Any]:
    incoming = response.model_dump(mode="json")
    if not same_call_identity(current, incoming):
        raise ValueError("call authority changed immutable call identity")
    required_state = {
        "create": "ringing",
        "accept": "active",
        "decline": "ended",
        "end": "ended",
    }.get(action)
    if required_state is None:
        raise ValueError("call authority response used an unsupported action")
    if incoming["state"] != required_state:
        raise ValueError("call authority returned a state inconsistent with the action")
    return incoming


def exact_call_projection(record: dict[str, Any], raw: object) -> dict[str, Any]:
    """Validate a peer acknowledgement as the exact public call projection."""

    incoming = CallResponse.model_validate(raw).model_dump(mode="json")
    expected = call_response(record).model_dump(mode="json")
    if incoming != expected:
        raise ValueError("peer acknowledged a different call projection")
    return incoming


async def notify_call(
    session: AsyncSession,
    redis: Redis,
    participants: list[str],
    event: str,
    record: dict[str, Any],
    settings: Settings,
) -> None:
    from app.tasks import mobile_push_activity

    bindings = call_bot_capability_bindings(record)
    current_by_grant = (
        {
            capability.grant_id: capability
            for capability in await session.scalars(
                select(BotDMCapability).where(
                    BotDMCapability.grant_id.in_(
                        [str(binding["grant_id"]) for binding in bindings.values()]
                    ),
                    BotDMCapability.authority_domain == settings.domain,
                    usable_dm_capability(at=datetime.now(UTC)),
                )
            )
        }
        if bindings
        else {}
    )
    remote_capability_identities = {
        identity
        for identity, binding in bindings.items()
        if (
            (capability := current_by_grant.get(str(binding["grant_id"]))) is not None
            and capability.revision == binding["revision"]
            and participant_identity(capability.bot_user_id, capability.bot_user_domain) == identity
        )
    }
    for identity in participants:
        try:
            user_id, domain = parse_participant_identity(identity)
            if domain == settings.domain or identity in remote_capability_identities:
                rendered = call_response(record).model_dump(mode="json")
                binding = bindings.get(identity)
                if identity in remote_capability_identities and binding is not None:
                    capability = current_by_grant[str(binding["grant_id"])]
                    rendered = bot_call_response(record, capability).model_dump(mode="json")
                await publish_dispatch(redis, user_topic(domain, user_id), event, rendered)
                if event == "CALL_RING" and domain == settings.domain:
                    await enqueue_best_effort(
                        mobile_push_activity,
                        user_id,
                        domain,
                        int(record["id"]),
                        str(record["authority_domain"]),
                        "call",
                        "Incoming Kaede call",
                        "Answer or decline the call.",
                        f"{record['id']}@{record['authority_domain']}",
                        f"{record['channel_id']}@{record['channel_domain']}",
                    )
        except Exception:
            # Call state is authoritative; gateway delivery is a recoverable
            # projection and must not turn a committed transition into a 5xx.
            log.exception("call_dispatch_failed", identity=identity, call_event=event)


async def clear_terminal_call_voice_projection(
    redis: Redis,
    settings: Settings,
    record: dict[str, Any],
) -> None:
    """Clear exact DM room/session state once its authoritative call ends."""

    if record.get("state") != "ended":
        return
    authority = str(record["authority_domain"])
    room = str(record["room"])
    for occupant in await room_occupants(redis, authority, room):
        await remove_occupant(redis, authority, room, occupant.identity)
        if occupant.connection_id:
            await release_voice_connection(
                redis,
                authority,
                occupant.identity,
                occupant.connection_id,
                room=room,
                client_kind=occupant.client_kind,
            )
    for identity in cast(list[str], record.get("participants", [])):
        _user_id, user_domain = parse_participant_identity(identity)
        active_authority = await get_federated_voice_session(redis, "authority", identity)
        if active_authority is not None and active_authority.call_id == str(record["id"]):
            await discard_federated_voice_session(
                redis,
                "authority",
                identity,
                move_session_id=active_authority.move_session_id,
                room=room,
                authority_domain=authority,
                connection_id=active_authority.connection_id,
                generation=active_authority.generation,
            )
        if user_domain != settings.domain:
            continue
        active_home = await get_federated_voice_session(redis, "home", identity)
        if active_home is not None and active_home.call_id == str(record["id"]):
            await discard_federated_voice_session(
                redis,
                "home",
                identity,
                move_session_id=active_home.move_session_id,
                room=room,
                authority_domain=authority,
                connection_id=active_home.connection_id,
                generation=active_home.generation,
            )
    await redis.delete(
        room_state_key("occupancy", authority, room),
        room_state_key("heartbeat", authority, room),
        room_state_key("snapshot-version", authority, room),
    )
    await cast(Any, redis.srem(voice_room_registry_key(authority), room))


async def propagate_terminal_call(
    session: AsyncSession,
    settings: Settings,
    record: dict[str, Any],
) -> None:
    response = call_response(record)
    if response.authority_domain != settings.domain or response.state != "ended":
        return
    destinations = {
        domain
        for identity in response.participants
        if (domain := parse_participant_identity(identity)[1]) != settings.domain
    }
    for destination in sorted(destinations):
        try:
            acknowledgement = await signed_request(
                session,
                settings,
                "POST",
                destination,
                "/_kaede/v1/calls/state",
                payload={"call": response.model_dump(mode="json")},
                request_timeout=5,
                max_response_bytes=16 * 1024,
            )
            if acknowledgement.status_code != 200:
                raise ValueError("call terminal projection was not acknowledged")
            exact_call_projection(
                response.model_dump(mode="json"),
                decode_federation_response_json(acknowledgement),
            )
        except FederationNetworkError:
            # Calls are deliberately ephemeral. A later idempotent action can
            # still retrieve the terminal authority state if this push drops.
            continue
        except Exception:
            # Federation propagation is also an ephemeral projection. Retrying
            # the terminal action will make another idempotent delivery attempt.
            log.exception("call_terminal_propagation_failed", destination=destination)


async def propagate_call_create(
    session: AsyncSession,
    settings: Settings,
    record: dict[str, Any],
    *,
    actor: User,
    state_version: int | None,
    exclude_domains: set[str] | None = None,
) -> None:
    """Project an authoritative call after its group membership fence is committed."""

    excluded = {settings.domain, *(exclude_domains or set())}
    destinations = {
        parse_participant_identity(identity)[1]
        for identity in cast(list[str], record["participants"])
    } - excluded
    if state_version is not None:
        envelope = await build_envelope(
            session,
            settings,
            "dm.group.call.create",
            actor,
            {"call": record},
            context={
                "conversation_id": str(record["channel_id"]),
                "conversation_domain": str(record["channel_domain"]),
                "state_version": str(state_version),
            },
            authority_attested_actor=actor.origin_domain != settings.domain,
        )
        for destination in sorted(destinations):
            await queue_event(session, settings, destination, envelope)
        await session.commit()
        from app.tasks import federation_deliver

        for destination in sorted(destinations):
            await enqueue_best_effort(federation_deliver, destination)
        return
    for destination in sorted(destinations):
        for attempt in range(3):
            try:
                response = await signed_request(
                    session,
                    settings,
                    "POST",
                    destination,
                    "/_kaede/v1/calls",
                    payload={
                        "call_id": str(record["id"]),
                        "channel_id": str(record["channel_id"]),
                        "channel_domain": str(record["channel_domain"]),
                        "authority_domain": str(record["authority_domain"]),
                        "actor_id": str(actor.id),
                        "actor_domain": actor.origin_domain,
                        "action": "create",
                        "created_at": int(record["created_at"]),
                        "state_version": (
                            str(state_version) if state_version is not None else None
                        ),
                    },
                    request_timeout=5,
                    max_response_bytes=16 * 1024,
                )
            except FederationNetworkError:
                break
            except Exception:
                log.exception("call_create_projection_failed", destination=destination)
                break
            if response.status_code == 200:
                try:
                    exact_call_projection(record, decode_federation_response_json(response))
                except (FederationNetworkError, ValueError):
                    log.info(
                        "call_create_projection_invalid_response",
                        destination=destination,
                    )
                break
            if response.status_code != 409 or attempt == 2:
                log.info(
                    "call_create_projection_rejected",
                    destination=destination,
                    status=response.status_code,
                )
                break
            await asyncio.sleep(0.25 * (attempt + 1))


async def project_call_transition(
    redis: Redis,
    session: AsyncSession,
    settings: Settings,
    record: dict[str, Any],
    event: str,
    *,
    changed: bool,
) -> None:
    if changed or record.get("state") == "ended":
        try:
            await notify_call(
                session,
                redis,
                cast(list[str], record["participants"]),
                event,
                record,
                settings,
            )
        except Exception:
            log.exception("call_transition_dispatch_failed", call_event=event)
    if record.get("state") == "ended":
        await clear_terminal_call_voice_projection(redis, settings, record)
    if record.get("state") == "ended" and record.get("authority_domain") == settings.domain:
        # These operations are safe to repeat. In particular, a replay must
        # retry them after a lost response or a transient control-plane outage.
        try:
            await delete_terminal_call_room(settings, record)
        except Exception:
            log.exception("call_terminal_cleanup_failed", call_id=record.get("id"))
        try:
            await propagate_terminal_call(session, settings, record)
        except Exception:
            log.exception("call_terminal_projection_failed", call_id=record.get("id"))


async def local_dm_participants(
    session: AsyncSession, channel_id: int, channel_domain: str
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
                DMParticipant.conversation_id == channel_id,
                DMParticipant.conversation_domain == channel_domain,
            )
            .order_by(User.origin_domain, User.id)
        )
    )


async def require_call_policy(
    session: AsyncSession,
    settings: Settings,
    record: dict[str, Any],
    actor: User,
    participants: list[User] | None = None,
) -> list[User]:
    """Recheck DM membership, blocks, and recipient privacy at effect time."""
    if participants is None:
        participants = await local_dm_participants(
            session, int(record["channel_id"]), str(record["channel_domain"])
        )
    identities = {
        participant_identity(participant.id, participant.origin_domain): participant
        for participant in participants
    }
    actor_identity = participant_identity(actor.id, actor.origin_domain)
    if actor_identity not in identities or actor_identity not in record.get("participants", []):
        raise HTTPException(status_code=403, detail={"code": "CALL_FORBIDDEN"})
    caller_identity = str(record.get("caller", ""))
    caller = identities.get(caller_identity)
    if caller is None:
        raise HTTPException(status_code=403, detail={"code": "CALL_FORBIDDEN"})
    conversation = await session.get(
        DMConversation, (int(record["channel_id"]), str(record["channel_domain"]))
    )
    if conversation is not None and conversation.type == "group":
        return participants
    peers = [
        participant for identity, participant in identities.items() if identity != actor_identity
    ]
    for peer in peers:
        if await blocked_between(session, actor, peer):
            raise HTTPException(status_code=403, detail={"code": "DM_PRIVACY_REJECTED"})
    if actor_identity == caller_identity:
        # The caller must satisfy each locally authoritative recipient policy.
        for peer in peers:
            if peer.is_local and peer.origin_domain == settings.domain:
                await require_can_direct_message(session, actor, peer)
    elif actor.is_local and actor.origin_domain == settings.domain:
        # Accepting/joining rechecks the local recipient's current policy.
        await require_can_direct_message(session, caller, actor)
    return participants


@router.post("/api/v1/channels/{channel_ref}/calls", response_model=CallResponse)
async def start_call(
    channel_ref: EntityRef,
    payload: CallCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> CallResponse:
    require_voice_enabled(settings)
    access = await load_channel_access(session, settings, auth.user, channel_ref)
    if access.guild is not None or access.channel.type != 1:
        raise HTTPException(status_code=400, detail={"code": "CALL_REQUIRES_DM"})
    participants = await local_dm_participants(
        session, access.channel.id, access.channel.origin_domain
    )
    conversation = await session.get(
        DMConversation,
        (access.channel.id, access.channel.origin_domain),
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    identities = {participant_identity(item.id, item.origin_domain) for item in participants}
    caller = participant_identity(auth.user.id, auth.user.origin_domain)
    call_id = await snowflake.mint()
    created_at = int(time.time())
    # Direct calls share the conversation's deterministic authority. Hosting
    # them at the human caller's home makes a remote bot connect to a replica
    # that cannot validate its exact DM capability.
    call_authority = conversation.authority_domain
    state_version = conversation.state_version if conversation.type == "group" else None
    record: dict[str, Any] = {
        "id": str(call_id),
        "channel_id": str(access.channel.id),
        "channel_domain": access.channel.origin_domain,
        "authority_domain": call_authority,
        "room": dm_room_name(access.channel.id, call_id),
        "state": "ringing",
        "created_at": created_at,
        "ended_at": None,
        "caller": caller,
        "participants": sorted(identities),
    }
    bindings = await active_bot_call_capability_bindings(
        session,
        settings,
        access.channel,
        identities,
        bot_participant_identities={
            participant_identity(item.id, item.origin_domain)
            for item in participants
            if item.account_type == "bot"
        },
    )
    if bindings:
        record[BOT_CAPABILITY_BINDINGS_FIELD] = bindings
    await require_call_policy(session, settings, record, auth.user, participants)
    if call_authority != settings.domain:
        try:
            response = await signed_request(
                session,
                settings,
                "POST",
                call_authority,
                "/_kaede/v1/calls",
                payload={
                    "call_id": str(call_id),
                    "channel_id": str(access.channel.id),
                    "channel_domain": access.channel.origin_domain,
                    "authority_domain": call_authority,
                    "actor_id": str(auth.user.id),
                    "actor_domain": auth.user.origin_domain,
                    "action": "create",
                    "created_at": created_at,
                    "state_version": str(state_version),
                    "ring": payload.ring,
                },
                request_timeout=5,
                max_response_bytes=16 * 1024,
            )
        except FederationNetworkError as exc:
            raise HTTPException(status_code=503, detail={"code": "CALL_HOME_UNREACHABLE"}) from exc
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail={"code": "CALL_REJECTED"})
        try:
            raw_authoritative = decode_federation_response_json(response)
            if not isinstance(raw_authoritative, dict):
                raise ValueError("call authority response is not an object")
            authoritative_response = CallResponse.model_validate(raw_authoritative)
            authoritative = authoritative_call_record(
                record,
                authoritative_response,
                "create",
            )
        except (FederationNetworkError, ValueError) as exc:
            raise HTTPException(
                status_code=502, detail={"code": "CALL_HOME_INVALID_RESPONSE"}
            ) from exc
        if not await create_call(redis, authoritative, identities, settings, accepted={caller}):
            raise HTTPException(status_code=409, detail={"code": "CALL_ALREADY_ACTIVE"})
        await notify_call(
            session, redis, sorted(identities), "CALL_CREATE", authoritative, settings
        )
        if payload.ring:
            await notify_call(
                session,
                redis,
                sorted(identities - {caller}),
                "CALL_RING",
                authoritative,
                settings,
            )
        return call_response(authoritative)
    if not await create_call(redis, record, identities, settings, accepted={caller}):
        raise HTTPException(status_code=409, detail={"code": "CALL_ALREADY_ACTIVE"})
    await notify_call(session, redis, sorted(identities), "CALL_CREATE", record, settings)
    if payload.ring:
        await notify_call(
            session, redis, sorted(identities - {caller}), "CALL_RING", record, settings
        )
    await propagate_call_create(
        session,
        settings,
        record,
        actor=auth.user,
        state_version=state_version,
    )
    return call_response(record)


@router.get(
    "/api/v1/channels/{channel_ref}/calls/active",
    response_model=ActiveCallResponse,
)
async def active_call(
    channel_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> ActiveCallResponse:
    access = await load_channel_access(session, settings, auth.user, channel_ref)
    if access.guild is not None or access.channel.type != 1:
        raise HTTPException(status_code=400, detail={"code": "CALL_REQUIRES_DM"})
    record = await get_active_call(redis, access.channel.origin_domain, access.channel.id)
    if record is None or record.get("state") == "ended":
        return ActiveCallResponse(call=None)
    identity = participant_identity(auth.user.id, auth.user.origin_domain)
    participants = cast(list[str], record.get("participants", []))
    if identity not in participants:
        return ActiveCallResponse(call=None)
    await require_call_policy(session, settings, record, auth.user)
    authority = str(record["authority_domain"])
    call_id = int(record["id"])
    joined = identity == record.get("caller") or await is_call_accepted(
        redis, authority, call_id, identity
    )
    return ActiveCallResponse(call=call_response(record), joined=joined)


@router.post("/api/v1/calls/{call_ref}", response_model=CallResponse)
async def act_on_call(
    call_ref: EntityRef,
    payload: CallAction,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> CallResponse:
    call_id, authority = call_ref.resolve(settings.domain)
    record = await get_call(redis, authority, call_id)
    if record is None or record.get("authority_domain") != authority:
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    identity = participant_identity(auth.user.id, auth.user.origin_domain)
    await require_call_policy(session, settings, record, auth.user)
    if authority != settings.domain:
        try:
            response = await signed_request(
                session,
                settings,
                "POST",
                authority,
                "/_kaede/v1/calls",
                payload={
                    "call_id": str(call_id),
                    "channel_id": str(record["channel_id"]),
                    "channel_domain": str(record["channel_domain"]),
                    "authority_domain": authority,
                    "actor_id": str(auth.user.id),
                    "actor_domain": auth.user.origin_domain,
                    "action": payload.action,
                    "created_at": int(record["created_at"]),
                },
                request_timeout=5,
                max_response_bytes=16 * 1024,
            )
        except FederationNetworkError as exc:
            raise HTTPException(status_code=503, detail={"code": "CALL_HOME_UNREACHABLE"}) from exc
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail={"code": "CALL_REJECTED"})
        try:
            authority_response = CallResponse.model_validate(
                decode_federation_response_json(response)
            )
            incoming = authoritative_call_record(record, authority_response, payload.action)
        except (FederationNetworkError, ValueError) as exc:
            raise HTTPException(
                status_code=502, detail={"code": "CALL_HOME_INVALID_RESPONSE"}
            ) from exc
        accepted, changed, result = await apply_authoritative_call(
            redis,
            incoming,
            settings,
            action=payload.action,
            identity=identity,
        )
        if not accepted:
            code = "CALL_NOT_FOUND" if result == "missing" else "CALL_HOME_INVALID_RESPONSE"
            raise HTTPException(
                status_code=404 if result == "missing" else 502, detail={"code": code}
            )
    else:
        accepted, changed, result = await transition_call(
            redis, authority, call_id, identity, payload.action, settings
        )
        if not accepted:
            code = "CALL_NOT_FOUND" if result == "missing" else "CALL_INVALID_TRANSITION"
            raise HTTPException(
                status_code=404 if result == "missing" else 409, detail={"code": code}
            )
    updated = cast(dict[str, Any], result)
    event = {"accept": "CALL_ACCEPT", "decline": "CALL_DECLINE", "end": "CALL_END"}[payload.action]
    await project_call_transition(
        redis,
        session,
        settings,
        updated,
        event,
        changed=changed,
    )
    return call_response(updated)


async def mint_dm_call_token(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    record: dict[str, Any],
    user: User,
    *,
    sender_device_id: str | None,
    remote_device_attested: bool = False,
    connection_id: str,
    takeover: bool = False,
    client_kind: str = "web",
    move_session_id: str | None = None,
    bot_capability: BotDMCapability | None = None,
    bot_worker: BotWorker | None = None,
    allow_listen: bool = True,
    allow_speak: bool = True,
    allow_stream: bool = True,
) -> VoiceTokenResponse:
    if bot_capability is not None:
        require_call_bot_capability(record, bot_capability)
    await require_call_policy(session, settings, record, user)
    identity = participant_identity(user.id, user.origin_domain)
    participants = cast(list[str], record.get("participants", []))
    if identity not in participants or record.get("state") == "ended":
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    call_id = int(record["id"])
    authority = str(record["authority_domain"])
    accepted = await is_call_accepted(redis, authority, call_id, identity)
    if identity != record.get("caller") and not accepted:
        raise HTTPException(status_code=409, detail={"code": "CALL_NOT_ACCEPTED"})
    room = str(record["room"])
    channel = await session.get(
        Channel,
        (int(record["channel_id"]), str(record["channel_domain"])),
    )
    if channel is None:
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    await require_e2ee_voice_device(
        session,
        settings,
        channel,
        user,
        sender_device_id,
        remote_device_attested=remote_device_attested,
        bot_installation=bot_capability,
        bot_worker_id=bot_worker.id if bot_worker is not None else None,
    )
    bot_lineage: dict[str, object] | None = None
    if user.account_type == "bot":
        if bot_capability is None or bot_worker is None:
            raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
        bot_lineage = bot_voice_lineage_metadata(bot_worker, bot_capability)
        if sender_device_id is not None:
            bot_lineage["bot_e2ee_device_id"] = sender_device_id
    try:
        await LiveKitControl(settings).ensure_room(room)
    except LiveKitError as exc:
        raise HTTPException(status_code=503, detail={"code": "VOICE_HOME_UNREACHABLE"}) from exc
    claimed, generation, previous_room, previous_client = await claim_voice_connection(
        redis,
        authority,
        identity,
        connection_id=connection_id,
        room=room,
        client_kind=client_kind,
        takeover=takeover,
        bot_lineage=bot_lineage,
    )
    if not claimed:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VOICE_ACTIVE_ELSEWHERE",
                "message": "Voice is already active for your account on another device.",
                "active_client": previous_client,
                "active_room": previous_room,
            },
        )
    if takeover and previous_room:
        with suppress(LiveKitError):
            await LiveKitControl(settings).remove_participant(previous_room, identity)
        await remove_occupant(redis, authority, previous_room, identity)
    e2ee_context = voice_e2ee_context(channel, room)
    metadata: dict[str, object] = {
        "version": 1,
        "generation": generation,
        "connection_id": connection_id,
        "client_kind": client_kind,
        "user_id": str(user.id),
        "user_domain": user.origin_domain,
        "channel_id": str(record["channel_id"]),
        "channel_domain": str(record["channel_domain"]),
        "call_id": str(call_id),
        "e2ee": bool(e2ee_context),
        "server_mute": False,
        "server_deaf": False,
        "can_speak": allow_speak,
        "can_stream": allow_stream,
        "can_priority_speak": False,
        "can_listen": allow_listen,
        "can_use_vad": True,
    }
    if bot_lineage is not None:
        metadata.update(bot_lineage)
    if move_session_id is not None:
        metadata["move_session_id"] = move_session_id
    if e2ee_context:
        metadata.update({"e2ee": True, **e2ee_context})
    try:
        token, expires_at = mint_join_token(
            settings,
            room=room,
            identity=identity,
            display_name=public_user_display_name(user),
            metadata=metadata,
            can_speak=allow_speak,
            can_stream=allow_stream,
            can_subscribe=allow_listen,
            can_publish_data=False,
        )
    except LiveKitError as exc:
        await release_voice_connection(
            redis,
            authority,
            identity,
            connection_id,
            room=room,
            generation=generation,
            client_kind=client_kind,
        )
        raise HTTPException(status_code=503, detail={"code": "VOICE_HOME_UNREACHABLE"}) from exc
    return VoiceTokenResponse(
        token=token,
        url=cast(str, settings.voice_public_url),
        room=room,
        generation=generation,
        connection_id=connection_id,
        expires_at=expires_at.isoformat(),
        can_speak=allow_speak,
        can_stream=allow_stream,
        can_priority_speak=False,
        can_listen=allow_listen,
        can_use_vad=True,
        e2ee=channel.encryption_mode == "e2ee",
        channel_id=str(channel.id),
        channel_domain=channel.origin_domain,
        encryption_policy_generation=(
            str(channel.encryption_policy_generation) if channel.encryption_mode == "e2ee" else None
        ),
        encryption_epoch=(
            str(channel.encryption_epoch)
            if channel.encryption_mode == "e2ee" and channel.encryption_epoch is not None
            else None
        ),
        media_protocol=e2ee_context.get("media_protocol"),
        media_suite=e2ee_context.get("media_suite"),
        media_session_id=e2ee_context.get("media_session_id"),
        media_epoch=e2ee_context.get("media_epoch"),
        move_session_id=move_session_id,
    )


@router.post("/api/v1/calls/{call_ref}/voice/token", response_model=VoiceTokenResponse)
async def call_voice_token(
    call_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    payload: VoiceTokenRequest | None = None,
) -> VoiceTokenResponse:
    require_voice_enabled(settings)
    call_id, authority = call_ref.resolve(settings.domain)
    record = await get_call(redis, authority, call_id)
    if record is None or record.get("authority_domain") != authority:
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    identity = participant_identity(auth.user.id, auth.user.origin_domain)
    connection_id = (
        payload.connection_id
        if payload is not None and payload.connection_id
        else secrets.token_urlsafe(32)
    )
    takeover = payload.takeover if payload is not None else False
    client_kind = payload.client_kind if payload is not None else "web"
    if authority == settings.domain:
        grant = await mint_dm_call_token(
            session,
            redis,
            settings,
            record,
            auth.user,
            sender_device_id=payload.sender_device_id if payload is not None else None,
            connection_id=connection_id,
            takeover=takeover,
            client_kind=client_kind,
        )
        await discard_all_federated_voice_home_sessions(redis, identity)
        return grant
    await require_call_policy(session, settings, record, auth.user)
    channel = await session.get(
        Channel,
        (int(record["channel_id"]), str(record["channel_domain"])),
    )
    if channel is None:
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    await require_e2ee_voice_device(
        session,
        settings,
        channel,
        auth.user,
        payload.sender_device_id if payload is not None else None,
    )
    move_session_id = secrets.token_urlsafe(32)
    await begin_federated_voice_home_session(
        redis,
        identity,
        FederatedVoiceSession(
            authority_domain=authority,
            guild_id="",
            room=str(record["room"]),
            generation=0,
            move_session_id=move_session_id,
            call_id=str(call_id),
            channel_id=str(record["channel_id"]),
            connection_id=connection_id,
            client_kind=client_kind,
        ),
    )
    succeeded = False
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            authority,
            "/_kaede/v1/voice/dm-token",
            payload={
                "call_id": str(call_id),
                "actor_id": str(auth.user.id),
                "actor_domain": auth.user.origin_domain,
                "move_session_id": move_session_id,
                "sender_device_id": payload.sender_device_id if payload is not None else None,
                "connection_id": connection_id,
                "takeover": takeover,
                "client_kind": client_kind,
            },
            request_timeout=5,
            max_response_bytes=16 * 1024,
        )
        if response.status_code != 200:
            if response.status_code == 409:
                try:
                    body = decode_federation_response_json(response)
                    detail = body.get("detail", {}) if isinstance(body, dict) else {}
                except (FederationNetworkError, ValueError, json.JSONDecodeError):
                    detail = {}
                if isinstance(detail, dict) and detail.get("code") == "VOICE_ACTIVE_ELSEWHERE":
                    raise HTTPException(status_code=409, detail=detail)
            raise HTTPException(status_code=response.status_code, detail={"code": "VOICE_DENIED"})
        try:
            grant = VoiceTokenResponse.model_validate(decode_federation_response_json(response))
        except (FederationNetworkError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=502, detail={"code": "VOICE_HOME_INVALID_RESPONSE"}
            ) from exc
        if grant.move_session_id != move_session_id or not federated_voice_grant_matches(
            grant,
            channel,
            expected_room=str(record["room"]),
            authority_domain=authority,
        ):
            raise HTTPException(status_code=502, detail={"code": "VOICE_HOME_INVALID_RESPONSE"})
        if not await activate_federated_dm_voice_home_session(
            redis,
            identity,
            move_session_id=move_session_id,
            authority_domain=authority,
            call_id=str(call_id),
            channel_id=str(record["channel_id"]),
            room=grant.room,
            generation=grant.generation,
            connection_id=grant.connection_id,
            client_kind=client_kind,
        ):
            raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_SUPERSEDED"})
        succeeded = True
        return grant
    except FederationNetworkError as exc:
        raise HTTPException(status_code=503, detail={"code": "VOICE_HOME_UNREACHABLE"}) from exc
    finally:
        if not succeeded:
            with suppress(Exception):
                await discard_pending_federated_voice_home_session(redis, identity, move_session_id)


@router.post("/_kaede/v1/voice/dm-token", response_model=VoiceTokenResponse)
async def federation_dm_voice_token(
    payload: DMVoiceBrokerRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> VoiceTokenResponse:
    require_voice_enabled(settings)
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "dm-voice-token", capacity=60, refill_per_minute=60
    )
    if payload.actor_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    record = await get_call(redis, settings.domain, int(payload.call_id))
    user = await session.get(User, (int(payload.actor_id), payload.actor_domain))
    if record is None or user is None or record.get("authority_domain") != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    await require_call_policy(session, settings, record, user)
    await require_remote_user_creation_allowed(session, user)
    return await mint_dm_call_token(
        session,
        redis,
        settings,
        record,
        user,
        sender_device_id=payload.sender_device_id,
        remote_device_attested=True,
        connection_id=payload.connection_id,
        takeover=payload.takeover,
        client_kind=payload.client_kind,
        move_session_id=payload.move_session_id,
    )


@router.post("/_kaede/v1/voice/dm-state", status_code=204, response_class=Response)
async def federation_dm_voice_state(
    payload: DMVoiceStateFederationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    require_voice_enabled(settings)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "dm-voice-state",
        capacity=180,
        refill_per_minute=180,
    )
    record = await get_call(redis, principal.origin, int(payload.call_id))
    if (
        record is None
        or record.get("authority_domain") != principal.origin
        or record.get("room") != payload.room
        or str(record.get("channel_id")) != payload.channel_id
        or record.get("state") == "ended"
    ):
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    now = int(time.time())
    if abs(payload.generated_at - now) > settings.federation_clock_skew_seconds:
        raise HTTPException(status_code=400, detail={"code": "KAED_VOICE_INVALID_STATE"})
    participant_identities = set(cast(list[str], record.get("participants", [])))
    local_identities = {
        identity
        for identity in participant_identities
        if parse_participant_identity(identity)[1] == settings.domain
    }
    if not local_identities:
        raise HTTPException(status_code=403, detail={"code": "KAED_VOICE_NOT_SUBSCRIBED"})
    occupants: list[Occupant] = []
    by_identity: dict[str, tuple[Occupant, int, str | None]] = {}
    try:
        for item in payload.participants:
            occupant = occupant_from_federation_state(item.model_dump(mode="python"))
            identity_user_id, identity_domain = parse_participant_identity(occupant.identity)
            if (
                occupant.identity not in participant_identities
                or occupant.room != payload.room
                or occupant.guild_id is not None
                or occupant.channel_id != payload.channel_id
                or (occupant.user_id, occupant.user_domain)
                != (str(identity_user_id), identity_domain)
            ):
                raise ValueError
            occupants.append(occupant)
            by_identity[occupant.identity] = (
                occupant,
                item.generation,
                item.move_session_id,
            )
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail={"code": "KAED_VOICE_INVALID_STATE"}) from None

    # A signed but delayed snapshot must not confirm an older connection over
    # a newer local token claim for the same call and room.
    for identity in local_identities:
        active = await get_federated_voice_session(redis, "home", identity)
        projected = by_identity.get(identity)
        if active is None or projected is None:
            continue
        occupant, generation, item_session_id = projected
        if (
            not active.ready
            or active.call_id != payload.call_id
            or active.channel_id != payload.channel_id
            or active.authority_domain != principal.origin
            or active.room != payload.room
            or active.move_session_id != item_session_id
            or active.connection_id != occupant.connection_id
            or generation < active.generation
        ):
            raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})

    replaced = await replace_occupancy(
        redis,
        principal.origin,
        payload.room,
        occupants,
        generated_at=payload.generated_at,
        snapshot_version=payload.snapshot_version,
    )
    if not replaced:
        return Response(status_code=204)
    for identity in local_identities:
        active = await get_federated_voice_session(redis, "home", identity)
        projected = by_identity.get(identity)
        if projected is not None:
            occupant, generation, _item_session_id = projected
            confirmed = await confirm_federated_voice_home_session(
                redis,
                identity,
                authority_domain=principal.origin,
                room=payload.room,
                generation=generation,
                connection_id=occupant.connection_id,
            )
            if not confirmed:
                await remove_occupant_connection(
                    redis,
                    principal.origin,
                    payload.room,
                    identity,
                    occupant.connection_id,
                )
                raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})
        elif active is not None and (
            active.call_id == payload.call_id
            and active.authority_domain == principal.origin
            and active.room == payload.room
        ):
            await discard_federated_voice_session(
                redis,
                "home",
                identity,
                move_session_id=active.move_session_id,
                room=active.room,
                authority_domain=active.authority_domain,
            )
        user_id, user_domain = parse_participant_identity(identity)
        await publish_ephemeral(
            redis,
            user_topic(user_domain, user_id),
            "VOICE_STATE_UPDATE",
            {
                "room": payload.room,
                "guild_id": None,
                "channel_id": payload.channel_id,
                "channel_domain": str(record["channel_domain"]),
                "call_id": payload.call_id,
                "participants": [public_occupant_state(item) for item in occupants],
                "generated_at": payload.generated_at,
                "heartbeat": True,
            },
        )
    return Response(status_code=204)


@router.post(
    "/_kaede/v1/voice/dm-self-state",
    response_model=VoiceSelfStateFederationResponse,
)
async def federation_dm_voice_self_state(
    payload: DMVoiceSelfStateFederationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> VoiceSelfStateFederationResponse:
    require_voice_enabled(settings)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "dm-voice-self-state",
        capacity=180,
        refill_per_minute=180,
    )
    record = await get_call(redis, settings.domain, int(payload.call_id))
    identity = participant_identity(int(payload.actor_id), principal.origin)
    if (
        record is None
        or record.get("authority_domain") != settings.domain
        or record.get("room") != payload.room
        or str(record.get("channel_id")) != payload.channel_id
        or record.get("state") == "ended"
        or identity not in cast(list[str], record.get("participants", []))
    ):
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    active = await get_federated_voice_session(redis, "authority", identity)
    occupant = await occupant_in_room(redis, settings.domain, payload.room, identity)
    if (
        active is None
        or occupant is None
        or not active.ready
        or not active.active
        or active.call_id != payload.call_id
        or active.channel_id != payload.channel_id
        or active.authority_domain != settings.domain
        or active.room != payload.room
        or active.move_session_id != payload.move_session_id
        or active.connection_id != payload.connection_id
        or occupant.connection_id != payload.connection_id
    ):
        raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})
    occupant_generation = occupant.participant_metadata.get("generation")
    if type(occupant_generation) is not int or occupant_generation != active.generation:
        raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})
    actor = await session.get(User, (int(payload.actor_id), principal.origin))
    if actor is None or actor.account_type != "human":
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    await require_remote_user_creation_allowed(session, actor)
    requested_self_mute = payload.self_mute or payload.self_deaf
    if payload.generation == active.generation:
        try:
            updated = await update_authoritative_occupant_self_state(
                redis,
                settings,
                occupant,
                self_mute=requested_self_mute,
                self_deaf=payload.self_deaf,
            )
        except LiveKitError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "VOICE_MEDIA_UNAVAILABLE", "retry_after_ms": 2000},
                headers={"Retry-After": "2"},
            ) from exc
        updated_generation = updated.participant_metadata.get("generation")
        if type(updated_generation) is not int or not await sync_federated_voice_session_generation(
            redis,
            "authority",
            identity,
            move_session_id=active.move_session_id,
            authority_domain=settings.domain,
            room=active.room,
            connection_id=active.connection_id,
            expected_generation=payload.generation,
            generation=updated_generation,
        ):
            raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})
    elif (
        payload.generation + 1 == active.generation
        and occupant.self_mute == requested_self_mute
        and occupant.self_deaf == payload.self_deaf
    ):
        updated = occupant
        updated_generation = active.generation
    else:
        raise HTTPException(status_code=409, detail={"code": "VOICE_SESSION_STALE"})
    from app.tasks import voice_replicate_room

    await enqueue_best_effort(voice_replicate_room, payload.room)
    return VoiceSelfStateFederationResponse(
        state=VoiceOccupantState.model_validate(public_occupant_state(updated)),
        generation=updated_generation,
    )


@router.post("/_kaede/v1/calls", response_model=CallResponse)
async def federation_call_signal(
    payload: CallFederationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> CallResponse:
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "dm-call", capacity=120, refill_per_minute=120
    )
    if payload.action != "create" and (
        payload.actor_domain != principal.origin
        or payload.authority_domain != settings.domain
        or payload.action == "ring"
    ):
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    if (
        payload.action == "create"
        and abs(payload.created_at - int(time.time())) > settings.federation_clock_skew_seconds
    ):
        raise HTTPException(status_code=400, detail={"code": "KAED_CALL_INVALID_TIMESTAMP"})
    call_id = int(payload.call_id)
    existing_call: dict[str, Any] | None = None
    channel_id = payload.channel_id
    channel_domain = payload.channel_domain
    if payload.action != "create":
        existing_call = await get_call(redis, settings.domain, call_id)
        if existing_call is None or existing_call.get("authority_domain") != settings.domain:
            raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
        if (
            str(existing_call.get("channel_id")) != payload.channel_id
            or existing_call.get("channel_domain") != payload.channel_domain
            or existing_call.get("created_at") != payload.created_at
        ):
            raise HTTPException(status_code=409, detail={"code": "CALL_CONTEXT_MISMATCH"})
        channel_id = str(existing_call["channel_id"])
        channel_domain = str(existing_call["channel_domain"])
    channel = await session.get(Channel, (int(channel_id), channel_domain))
    actor = await session.get(User, (int(payload.actor_id), payload.actor_domain))
    if channel is None or channel.type != 1 or actor is None:
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    conversation = await session.get(DMConversation, (channel.id, channel.origin_domain))
    if conversation is None:
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    authority_proposal = False
    if payload.action == "create":
        if conversation.type == "group":
            if (
                payload.authority_domain != conversation.authority_domain
                or principal.origin not in {payload.actor_domain, conversation.authority_domain}
            ):
                raise HTTPException(
                    status_code=403,
                    detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"},
                )
            if payload.state_version is None:
                raise HTTPException(status_code=409, detail={"code": "GROUP_DM_STATE_REQUIRED"})
            requested_state_version = int(payload.state_version)
            if conversation.state_version < requested_state_version:
                raise HTTPException(status_code=409, detail={"code": "GROUP_DM_STATE_BEHIND"})
            authority_proposal = (
                settings.domain == conversation.authority_domain
                and principal.origin == payload.actor_domain
                and payload.actor_domain != conversation.authority_domain
            )
            if authority_proposal and conversation.state_version != requested_state_version:
                raise HTTPException(status_code=409, detail={"code": "GROUP_DM_STATE_STALE"})
        else:
            if (
                payload.actor_domain != principal.origin
                or payload.authority_domain != conversation.authority_domain
                or settings.domain != conversation.authority_domain
                or payload.state_version is not None
            ):
                raise HTTPException(
                    status_code=403,
                    detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"},
                )
            authority_proposal = True
    participants = await local_dm_participants(session, channel.id, channel.origin_domain)
    identities = {participant_identity(item.id, item.origin_domain) for item in participants}
    identity = participant_identity(actor.id, actor.origin_domain)
    if identity not in identities or (
        existing_call is not None and identity not in existing_call.get("participants", [])
    ):
        raise HTTPException(status_code=403, detail={"code": "CALL_FORBIDDEN"})
    if payload.action == "create":
        record = {
            "id": payload.call_id,
            "channel_id": payload.channel_id,
            "channel_domain": payload.channel_domain,
            "authority_domain": payload.authority_domain,
            "room": dm_room_name(channel.id, call_id),
            "state": "ringing",
            "created_at": payload.created_at,
            "ended_at": None,
            "caller": identity,
            "participants": sorted(identities),
        }
        # Validate the public resource before attaching authority-private grant
        # bindings used only by the call and media control planes.
        call_response(record)
        bindings = await active_bot_call_capability_bindings(
            session,
            settings,
            channel,
            identities,
            bot_participant_identities={
                participant_identity(item.id, item.origin_domain)
                for item in participants
                if item.account_type == "bot"
            },
        )
        if bindings:
            record[BOT_CAPABILITY_BINDINGS_FIELD] = bindings
        await require_call_policy(session, settings, record, actor, participants)
        await require_remote_user_creation_allowed(session, actor)
        created = await create_call(redis, record, identities, settings, accepted={identity})
        if not created:
            existing = await get_call(redis, payload.authority_domain, call_id)
            if existing is None or not same_call_identity(existing, record):
                raise HTTPException(status_code=409, detail={"code": "CALL_ALREADY_ACTIVE"})
            return call_response(existing)
        await notify_call(session, redis, sorted(identities), "CALL_CREATE", record, settings)
        if payload.ring:
            await notify_call(
                session,
                redis,
                sorted(identities - {identity}),
                "CALL_RING",
                record,
                settings,
            )
        if authority_proposal:
            await propagate_call_create(
                session,
                settings,
                record,
                actor=actor,
                state_version=conversation.state_version,
                exclude_domains={principal.origin},
            )
        return call_response(record)
    if existing_call is None:
        raise RuntimeError("non-create call action lost its validated authority record")
    await require_call_policy(session, settings, existing_call, actor, participants)
    if payload.action == "accept":
        await require_remote_user_creation_allowed(session, actor)
    accepted, changed, result = await transition_call(
        redis, settings.domain, call_id, identity, payload.action, settings
    )
    if not accepted:
        code = "CALL_NOT_FOUND" if result == "missing" else "CALL_INVALID_TRANSITION"
        raise HTTPException(status_code=404 if result == "missing" else 409, detail={"code": code})
    updated = cast(dict[str, Any], result)
    event = {"accept": "CALL_ACCEPT", "decline": "CALL_DECLINE", "end": "CALL_END"}[payload.action]
    await project_call_transition(
        redis,
        session,
        settings,
        updated,
        event,
        changed=changed,
    )
    return call_response(updated)


@router.post("/_kaede/v1/calls/state", response_model=CallResponse)
async def federation_call_state(
    payload: CallStateFederationRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> CallResponse:
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "dm-call-state", capacity=120, refill_per_minute=120
    )
    incoming = payload.call.model_dump(mode="json")
    if payload.call.authority_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_AUTHOR_ORIGIN_MISMATCH"})
    call_id = int(payload.call.id)
    existing = await get_call(redis, principal.origin, call_id)
    if existing is None:
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    if not same_call_identity(existing, incoming):
        raise HTTPException(status_code=409, detail={"code": "CALL_CONTEXT_MISMATCH"})
    accepted, _changed, result = await apply_authoritative_call(
        redis,
        incoming,
        settings,
        action="sync",
    )
    if not accepted:
        code = "CALL_NOT_FOUND" if result == "missing" else "CALL_INVALID_TRANSITION"
        raise HTTPException(status_code=404 if result == "missing" else 409, detail={"code": code})
    updated = cast(dict[str, Any], result)
    await clear_terminal_call_voice_projection(redis, settings, updated)
    await notify_call(
        session,
        redis,
        cast(list[str], updated["participants"]),
        "CALL_END",
        updated,
        settings,
    )
    return call_response(updated)
